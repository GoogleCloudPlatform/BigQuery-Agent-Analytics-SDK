# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Unit tests for ``bigquery_agent_analytics.materialize_window``.

Covers the pure helpers (state-key, SQL builders, identifier
validation, the outcome counter, the result-shape helper) and the
orchestrator with mocked BigQuery + manager + materializer so the
test runs without live infrastructure.

Live BigQuery integration is covered separately (a follow-up).
"""

from __future__ import annotations

import datetime as _dt
from unittest import mock

import pytest

from bigquery_agent_analytics import materialize_window as mw

# ------------------------------------------------------------------ #
# compute_state_key                                                    #
# ------------------------------------------------------------------ #


class TestComputeStateKey:

  def test_deterministic(self):
    """Same inputs → same key, byte-for-byte."""
    args = dict(
        project_id="my-proj",
        dataset_id="my_ds",
        graph_name="my_graph",
        events_table="agent_events",
        ontology_fingerprint="sha256:abc",
        binding_fingerprint="sha256:def",
    )
    assert mw.compute_state_key(**args) == mw.compute_state_key(**args)

  def test_distinguishes_project(self):
    """Different ``project_id`` → different key — guards against
    one project's checkpoint advancing another's by accident."""
    base = dict(
        dataset_id="ds",
        graph_name="g",
        events_table="t",
        ontology_fingerprint="o",
        binding_fingerprint="b",
    )
    a = mw.compute_state_key(project_id="proj-a", **base)
    b = mw.compute_state_key(project_id="proj-b", **base)
    assert a != b

  def test_distinguishes_binding_fingerprint(self):
    """Binding edit (e.g., column rename) bumps the fingerprint →
    new key → fresh bootstrap, prior checkpoint isn't consulted.
    That's the right behavior: the new binding's row shape may
    not match what the prior checkpoint's materialize wrote."""
    base = dict(
        project_id="p",
        dataset_id="d",
        graph_name="g",
        events_table="t",
        ontology_fingerprint="o",
    )
    a = mw.compute_state_key(binding_fingerprint="b1", **base)
    b = mw.compute_state_key(binding_fingerprint="b2", **base)
    assert a != b

  def test_hex_format(self):
    """sha256 hex — 64 chars, lowercase, no prefix. Reviewers
    debugging a state table want a key that copies cleanly."""
    key = mw.compute_state_key(
        project_id="p",
        dataset_id="d",
        graph_name="g",
        events_table="t",
        ontology_fingerprint="o",
        binding_fingerprint="b",
    )
    assert len(key) == 64
    assert all(c in "0123456789abcdef" for c in key)


# ------------------------------------------------------------------ #
# Identifier validation                                                #
# ------------------------------------------------------------------ #


class TestValidatedTableRef:

  def test_clean_inputs(self):
    assert (
        mw.validated_table_ref("my-proj", "my_ds", "agent_events")
        == "my-proj.my_ds.agent_events"
    )

  @pytest.mark.parametrize(
      "bad",
      [
          ("proj space", "ds", "tbl"),
          ("proj", "ds;DROP", "tbl"),
          ("proj", "ds", "tbl`back"),
          ("proj", "ds", "tbl.dotted"),
          ("proj", "ds", ""),
      ],
  )
  def test_rejects_unsafe_segments(self, bad):
    """Whitespace / backticks / semicolons / dots-inside-segment
    / empty strings all rejected — the FQN is interpolated into
    SQL with backticks, so the validator is the choke point."""
    with pytest.raises(ValueError):
      mw.validated_table_ref(*bad)


class TestParseStateTableRef:

  def test_one_segment_fills_defaults(self):
    assert mw.parse_state_table_ref("state_tbl", "proj", "ds") == (
        "proj",
        "ds",
        "state_tbl",
    )

  def test_two_segments_fills_project(self):
    assert mw.parse_state_table_ref("other_ds.state_tbl", "proj", "ds") == (
        "proj",
        "other_ds",
        "state_tbl",
    )

  def test_three_segments_explicit(self):
    assert mw.parse_state_table_ref(
        "other_proj.other_ds.state_tbl", "proj", "ds"
    ) == ("other_proj", "other_ds", "state_tbl")

  def test_rejects_unsafe(self):
    with pytest.raises(ValueError):
      mw.parse_state_table_ref("proj.ds.state tbl", "p", "d")


# ------------------------------------------------------------------ #
# SQL builders                                                         #
# ------------------------------------------------------------------ #


class TestBuildDiscoverySql:

  def test_shape(self):
    sql = mw.build_discovery_sql(
        events_table_ref="p.d.agent_events",
        completion_event_type="AGENT_COMPLETED",
    )
    # Partition pruning depends on the predicate being directly
    # on the partition column. ``timestamp >= @scan_start`` is
    # what unlocks it; the param binding is at execute time but
    # the column name has to appear in the WHERE clause literally.
    assert "timestamp >= @scan_start" in sql
    assert "timestamp < @scan_end" in sql
    assert "event_type = @completion_event_type" in sql
    # FQN is backtick-quoted; the validator gates the segments
    # before we hit this builder.
    assert "`p.d.agent_events`" in sql
    # GROUP BY session_id + ORDER BY completion_timestamp is the
    # contract the orchestrator depends on for the watermark.
    assert "GROUP BY session_id" in sql
    assert "ORDER BY completion_timestamp" in sql

  def test_max_sessions_emits_limit(self):
    sql = mw.build_discovery_sql(
        events_table_ref="p.d.agent_events",
        completion_event_type="AGENT_COMPLETED",
        max_sessions=42,
    )
    assert "LIMIT 42" in sql

  def test_max_sessions_omitted_when_none(self):
    sql = mw.build_discovery_sql(
        events_table_ref="p.d.agent_events",
        completion_event_type="AGENT_COMPLETED",
    )
    assert "LIMIT" not in sql


# ------------------------------------------------------------------ #
# Outcome counter                                                      #
# ------------------------------------------------------------------ #


class TestMakeOutcomeCounter:

  def test_counts_known_decisions(self):
    """All three known C2 decisions accumulate independently.
    Names mirror ``runtime_fallback.py`` constants so a typo in
    one place fails this test fast."""
    cb, counts = mw.make_outcome_counter()
    cb("TOOL_COMPLETED", mock.Mock(decision="compiled_unchanged"))
    cb("TOOL_COMPLETED", mock.Mock(decision="compiled_unchanged"))
    cb("TOOL_COMPLETED", mock.Mock(decision="compiled_filtered"))
    cb("TOOL_COMPLETED", mock.Mock(decision="fallback_for_event"))
    assert counts == {
        "compiled_unchanged": 2,
        "compiled_filtered": 1,
        "fallback_for_event": 1,
    }

  def test_unknown_decision_gets_its_own_bucket(self):
    """A future SDK addition (new decision name) doesn't silently
    drop telemetry — it gets its own bucket. ``unknown`` is reserved
    for outcomes missing the ``decision`` field entirely."""
    cb, counts = mw.make_outcome_counter()
    cb("TOOL_COMPLETED", mock.Mock(decision="future_decision"))
    assert counts["future_decision"] == 1
    assert "unknown" not in counts

  def test_missing_decision_field_falls_to_unknown(self):
    """Outcome with no ``decision`` attribute / None value
    accrues to ``unknown`` so the count is non-silent."""
    cb, counts = mw.make_outcome_counter()
    bad = mock.Mock(spec=[])  # no .decision attribute at all
    cb("TOOL_COMPLETED", bad)
    assert counts["unknown"] == 1


# ------------------------------------------------------------------ #
# Helpers used by the orchestrator                                     #
# ------------------------------------------------------------------ #


class TestMaxSuccessCompletion:

  def test_returns_max_among_successes(self):
    results = [
        mw.SessionResult(
            session_id="a",
            ok=True,
            completion_timestamp=_dt.datetime(
                2026, 5, 15, 10, tzinfo=_dt.timezone.utc
            ),
        ),
        mw.SessionResult(
            session_id="b",
            ok=False,
            completion_timestamp=_dt.datetime(
                2026, 5, 15, 11, tzinfo=_dt.timezone.utc
            ),
            error_code="X",
        ),
        mw.SessionResult(
            session_id="c",
            ok=True,
            completion_timestamp=_dt.datetime(
                2026, 5, 15, 9, tzinfo=_dt.timezone.utc
            ),
        ),
    ]
    # ``b`` failed at 11h. The high-water mark must NOT include
    # it; otherwise the next run skips ``b`` even though it
    # never landed.
    assert mw._max_success_completion(results) == _dt.datetime(
        2026, 5, 15, 10, tzinfo=_dt.timezone.utc
    )

  def test_returns_none_when_no_successes(self):
    """Empty window or all-failed → no checkpoint advance."""
    assert mw._max_success_completion([]) is None


class TestBuildResult:

  def test_aggregates_row_counts(self):
    """rows_materialized sums across sessions — the per-session
    counts are what materialize_with_status returns."""
    results = [
        mw.SessionResult(
            session_id="a",
            ok=True,
            completion_timestamp=_dt.datetime(
                2026, 5, 15, 10, tzinfo=_dt.timezone.utc
            ),
            rows_materialized={"DecisionExecution": 2, "AgentSession": 1},
        ),
        mw.SessionResult(
            session_id="b",
            ok=True,
            completion_timestamp=_dt.datetime(
                2026, 5, 15, 11, tzinfo=_dt.timezone.utc
            ),
            rows_materialized={"DecisionExecution": 3, "Candidate": 5},
        ),
    ]
    r = mw._build_result(
        run_id="r",
        state_key="k",
        scan_start=_dt.datetime(2026, 5, 15, tzinfo=_dt.timezone.utc),
        scan_end=_dt.datetime(2026, 5, 15, 12, tzinfo=_dt.timezone.utc),
        checkpoint_read=None,
        checkpoint_written=_dt.datetime(
            2026, 5, 15, 11, tzinfo=_dt.timezone.utc
        ),
        sessions_discovered=2,
        session_results=results,
        compiled_outcomes={
            "compiled_unchanged": 7,
            "compiled_filtered": 0,
            "fallback_for_event": 0,
        },
        ok=True,
    )
    assert r.rows_materialized == {
        "DecisionExecution": 5,
        "AgentSession": 1,
        "Candidate": 5,
    }

  def test_failures_list_only_failed_sessions(self):
    results = [
        mw.SessionResult(
            session_id="a",
            ok=True,
            completion_timestamp=_dt.datetime(
                2026, 5, 15, 10, tzinfo=_dt.timezone.utc
            ),
        ),
        mw.SessionResult(
            session_id="b",
            ok=False,
            completion_timestamp=_dt.datetime(
                2026, 5, 15, 11, tzinfo=_dt.timezone.utc
            ),
            error_code="ValueError",
            error_detail="boom",
        ),
    ]
    r = mw._build_result(
        run_id="r",
        state_key="k",
        scan_start=_dt.datetime(2026, 5, 15, tzinfo=_dt.timezone.utc),
        scan_end=_dt.datetime(2026, 5, 15, 12, tzinfo=_dt.timezone.utc),
        checkpoint_read=None,
        checkpoint_written=_dt.datetime(
            2026, 5, 15, 10, tzinfo=_dt.timezone.utc
        ),
        sessions_discovered=2,
        session_results=results,
        compiled_outcomes={
            "compiled_unchanged": 0,
            "compiled_filtered": 0,
            "fallback_for_event": 0,
        },
        ok=False,
    )
    assert len(r.failures) == 1
    assert r.failures[0]["session_id"] == "b"
    assert r.failures[0]["error_code"] == "ValueError"


# ------------------------------------------------------------------ #
# to_json round-trip                                                   #
# ------------------------------------------------------------------ #


class TestToJson:

  def test_iso_timestamps_with_trailing_z(self):
    """JSON consumers expect ISO 8601 UTC. Trailing ``Z`` is the
    industry convention; ``+00:00`` is technically valid but
    irritates many parsers. We pick the strict form."""
    r = mw.MaterializeWindowResult(
        run_id="r",
        state_key="k",
        window_start=_dt.datetime(2026, 5, 15, 10, tzinfo=_dt.timezone.utc),
        window_end=_dt.datetime(2026, 5, 15, 16, tzinfo=_dt.timezone.utc),
        checkpoint_read=None,
        checkpoint_written=_dt.datetime(
            2026, 5, 15, 16, tzinfo=_dt.timezone.utc
        ),
        sessions_discovered=0,
        sessions_materialized=0,
        sessions_failed=0,
        rows_materialized={},
        table_statuses={},
        compiled_outcomes={
            "compiled_unchanged": 0,
            "compiled_filtered": 0,
            "fallback_for_event": 0,
        },
        failures=[],
        ok=True,
    )
    js = r.to_json()
    assert js["window_start"].endswith("Z")
    assert js["checkpoint_read"] is None
    assert js["ok"] is True


# ------------------------------------------------------------------ #
# Orchestrator (full path, with mocks)                                 #
# ------------------------------------------------------------------ #


class _FakeBQRow:
  """Mimics google.cloud.bigquery.Row attribute access for tests."""

  def __init__(self, **kwargs):
    for k, v in kwargs.items():
      setattr(self, k, v)


@pytest.fixture
def fixture_paths(tmp_path):
  """Write a minimal ontology + binding pair to disk so the
  orchestrator's ``load_ontology`` / ``load_binding`` calls don't
  go through I/O mock plumbing. The contents are valid enough to
  parse + fingerprint."""
  ontology_yaml = tmp_path / "ontology.yaml"
  ontology_yaml.write_text(
      "ontology: test_ont\n"
      "entities:\n"
      "  - name: Entity\n"
      "    keys: {primary: [id]}\n"
      "    properties:\n"
      "      - name: id\n"
      "        type: string\n"
  )
  binding_yaml = tmp_path / "binding.yaml"
  binding_yaml.write_text(
      "binding: test_binding\n"
      "ontology: test_ont\n"
      "target:\n"
      "  backend: bigquery\n"
      "  project: test-proj\n"
      "  dataset: test_ds\n"
      "entities:\n"
      "  - name: Entity\n"
      "    source: test-proj.test_ds.entity\n"
      "    properties:\n"
      "      - name: id\n"
      "        column: id\n"
  )
  return ontology_yaml, binding_yaml


def _stub_bq_client(discovered_rows):
  """A BigQuery client whose ``.query()`` returns:
  - the DDL response (anything; we just check it was called)
  - the state-table read (empty → bootstrap)
  - the discovery query (returns ``discovered_rows``)
  ``.insert_rows_json`` returns [] (no errors).
  """
  client = mock.Mock()

  # Each .query() call's .result() yields a different row set.
  # We sequence them via side_effect on the query() Mock.
  results = [
      mock.Mock(result=mock.Mock(return_value=[])),  # CREATE TABLE
      mock.Mock(result=mock.Mock(return_value=[])),  # state read
      mock.Mock(result=mock.Mock(return_value=discovered_rows)),  # discovery
  ]
  client.query = mock.Mock(side_effect=results)
  client.insert_rows_json = mock.Mock(return_value=[])
  return client


def test_dry_run_returns_discovered_sessions_without_extracting(
    fixture_paths, monkeypatch
):
  """``--dry-run`` proves the discovery query runs end-to-end
  (FQN validated, partition pruning intact) without spending
  AI.GENERATE tokens. The state row is *not* written on dry-run
  because the result isn't authoritative."""
  ontology_yaml, binding_yaml = fixture_paths
  now = _dt.datetime(2026, 5, 15, 14, 0, tzinfo=_dt.timezone.utc)
  discovered = [
      _FakeBQRow(
          session_id="sess-1",
          completion_timestamp=_dt.datetime(
              2026, 5, 15, 13, tzinfo=_dt.timezone.utc
          ),
      ),
      _FakeBQRow(
          session_id="sess-2",
          completion_timestamp=_dt.datetime(
              2026, 5, 15, 13, 30, tzinfo=_dt.timezone.utc
          ),
      ),
  ]
  client = _stub_bq_client(discovered)

  # ``bigquery.ScalarQueryParameter`` is imported lazily inside
  # ``run_materialize_window``; stub at module level so the call
  # goes through without a real BQ install.
  result = mw.run_materialize_window(
      project_id="test-proj",
      dataset_id="test_ds",
      ontology_path=str(ontology_yaml),
      binding_path=str(binding_yaml),
      lookback_hours=6.0,
      validate_binding=False,
      dry_run=True,
      bq_client=client,
      run_started_at=now,
  )
  assert result.sessions_discovered == 2
  assert result.sessions_materialized == 0
  assert result.sessions_failed == 0
  assert result.checkpoint_written is None
  # Dry-run skips the state-row append.
  assert client.insert_rows_json.call_count == 0


def test_partial_failure_advances_checkpoint_only_to_last_success(
    fixture_paths,
):
  """If session 2 of 3 fails, the checkpoint advances to session
  1's completion timestamp. Next run starts there; session 2 is
  retried (at-least-once)."""
  ontology_yaml, binding_yaml = fixture_paths
  now = _dt.datetime(2026, 5, 15, 14, 0, tzinfo=_dt.timezone.utc)
  ts1 = _dt.datetime(2026, 5, 15, 13, 0, tzinfo=_dt.timezone.utc)
  ts2 = _dt.datetime(2026, 5, 15, 13, 15, tzinfo=_dt.timezone.utc)
  ts3 = _dt.datetime(2026, 5, 15, 13, 30, tzinfo=_dt.timezone.utc)
  discovered = [
      _FakeBQRow(session_id="s1", completion_timestamp=ts1),
      _FakeBQRow(session_id="s2", completion_timestamp=ts2),
      _FakeBQRow(session_id="s3", completion_timestamp=ts3),
  ]
  client = _stub_bq_client(discovered)

  # Stub the manager + materializer so the test doesn't need real
  # BQ. Session ``s2`` raises; the orchestrator must catch + halt.
  fake_manager = mock.Mock()
  fake_manager.spec = mock.Mock()
  fake_manager.extract_graph = mock.Mock(
      side_effect=[
          mock.Mock(),  # s1 ok
          RuntimeError("simulated AI.GENERATE failure on s2"),
          mock.Mock(),  # s3 — should NOT be reached
      ]
  )

  fake_materializer_cls = mock.Mock()
  fake_materializer = fake_materializer_cls.return_value
  fake_mat_result = mock.Mock()
  fake_mat_result.row_counts = {"DecisionExecution": 1}
  # New: orchestrator iterates ``table_statuses`` after the
  # ``materialize_with_status`` call. Set to an empty dict so
  # the iteration doesn't blow up on a Mock object.
  fake_mat_result.table_statuses = {}
  fake_materializer.materialize_with_status = mock.Mock(
      return_value=fake_mat_result
  )

  with (
      mock.patch.object(mw, "_build_manager", return_value=fake_manager),
      mock.patch(
          "bigquery_agent_analytics.ontology_materializer.OntologyMaterializer",
          fake_materializer_cls,
      ),
  ):
    result = mw.run_materialize_window(
        project_id="test-proj",
        dataset_id="test_ds",
        ontology_path=str(ontology_yaml),
        binding_path=str(binding_yaml),
        lookback_hours=6.0,
        validate_binding=False,
        bq_client=client,
        run_started_at=now,
    )

  assert not result.ok
  assert result.sessions_materialized == 1  # only s1
  assert result.sessions_failed == 1  # s2
  # s3 never reached → not in failures list.
  assert len(result.failures) == 1
  assert result.failures[0]["session_id"] == "s2"
  # Checkpoint advanced to s1's timestamp — NOT s2's, NOT s3's.
  assert result.checkpoint_written == ts1
  # State row was appended (it's append-only: we always write,
  # even on failure).
  assert client.insert_rows_json.call_count == 1


def test_empty_window_writes_heartbeat_state_row(fixture_paths):
  """Zero discovered sessions → ok=True, no checkpoint
  advancement, but a state-row write so operators see the run."""
  ontology_yaml, binding_yaml = fixture_paths
  now = _dt.datetime(2026, 5, 15, 14, 0, tzinfo=_dt.timezone.utc)
  client = _stub_bq_client([])

  # The orchestrator constructs the materializer outside the
  # per-session loop, so even an empty window touches it.
  with (
      mock.patch.object(mw, "_build_manager", return_value=mock.Mock()),
      mock.patch(
          "bigquery_agent_analytics.ontology_materializer.OntologyMaterializer"
      ),
  ):
    result = mw.run_materialize_window(
        project_id="test-proj",
        dataset_id="test_ds",
        ontology_path=str(ontology_yaml),
        binding_path=str(binding_yaml),
        lookback_hours=6.0,
        validate_binding=False,
        bq_client=client,
        run_started_at=now,
    )

  assert result.ok
  assert result.sessions_discovered == 0
  assert result.checkpoint_written is None
  assert client.insert_rows_json.call_count == 1


# ------------------------------------------------------------------ #
# Round-2 regressions                                                  #
# ------------------------------------------------------------------ #


class TestStateReadSql:

  def test_filters_null_last_completion_at(self):
    """Heartbeat (empty window) and all-failed runs write a
    state row with ``last_completion_at = NULL``. Reading those
    as "the most recent checkpoint" would erase the prior real
    checkpoint and bootstrap from --lookback-hours on the next
    run — which can skip the failed session if lookback is short.
    The select MUST filter on non-NULL."""
    sql = mw.build_state_select_sql("p.d._bqaa_materialization_state")
    assert "last_completion_at IS NOT NULL" in sql


class TestPreScanBundleFingerprint:

  def test_single_bundle_returns_its_fingerprint(self, tmp_path):
    """One bundle in root → that bundle's fingerprint."""
    bundle = tmp_path / "bundle_abc"
    bundle.mkdir()
    (bundle / "manifest.json").write_text('{"fingerprint": "abc123def456"}')
    assert mw._pre_scan_bundle_fingerprint(tmp_path) == "abc123def456"

  def test_multiple_bundles_same_fingerprint_ok(self, tmp_path):
    """Two bundles, same fingerprint (e.g., re-compile cache
    hit produced an identical sibling) → returns the shared
    fingerprint without complaint."""
    for name in ("a", "b"):
      b = tmp_path / name
      b.mkdir()
      (b / "manifest.json").write_text('{"fingerprint": "shared-fp"}')
    assert mw._pre_scan_bundle_fingerprint(tmp_path) == "shared-fp"

  def test_mixed_fingerprints_rejected(self, tmp_path):
    """Two bundles with different fingerprints → fail fast with
    a summary that lists both. Mixed roots are a deployment bug
    (operator forgot to clean up); the SDK refuses to guess
    which is current."""
    for name, fp in (("old", "fp-old"), ("new", "fp-new")):
      b = tmp_path / name
      b.mkdir()
      (b / "manifest.json").write_text(f'{{"fingerprint": "{fp}"}}')
    with pytest.raises(ValueError, match="mixed fingerprints"):
      mw._pre_scan_bundle_fingerprint(tmp_path)

  def test_no_bundles_rejected(self, tmp_path):
    """Empty root → explicit error, not silent fallback."""
    with pytest.raises(ValueError, match="contains no bundles"):
      mw._pre_scan_bundle_fingerprint(tmp_path)

  def test_missing_manifest_field_rejected(self, tmp_path):
    """Malformed manifest (no ``fingerprint`` key) → clear error
    so the operator knows which bundle is broken."""
    bundle = tmp_path / "broken"
    bundle.mkdir()
    (bundle / "manifest.json").write_text('{"not_fingerprint": "x"}')
    with pytest.raises(ValueError, match="no ``fingerprint`` field"):
      mw._pre_scan_bundle_fingerprint(tmp_path)


class TestFirstRunLookback:

  def test_first_run_uses_lookback_hours_not_default_30min(self, fixture_paths):
    """First run (no checkpoint row) must scan ``--lookback-hours``
    back, not the 30-min ``DEFAULT_INITIAL_LOOKBACK_MINUTES``.
    Regression: previously the bootstrap path used 30min, so
    ``--lookback-hours 6`` actually only scanned 30 minutes —
    a customer requesting 6h coverage got 30min coverage."""
    ontology_yaml, binding_yaml = fixture_paths
    now = _dt.datetime(2026, 5, 15, 14, 0, tzinfo=_dt.timezone.utc)
    client = _stub_bq_client([])

    with (
        mock.patch.object(mw, "_build_manager", return_value=mock.Mock()),
        mock.patch(
            "bigquery_agent_analytics.ontology_materializer.OntologyMaterializer"
        ),
    ):
      result = mw.run_materialize_window(
          project_id="test-proj",
          dataset_id="test_ds",
          ontology_path=str(ontology_yaml),
          binding_path=str(binding_yaml),
          lookback_hours=6.0,
          validate_binding=False,  # we mock the manager; skip
          bq_client=client,
          run_started_at=now,
      )

    expected_window_start = now - _dt.timedelta(hours=6)
    assert result.window_start == expected_window_start, (
        f"first-run window_start should be {expected_window_start}; "
        f"got {result.window_start} (was the 30-min default applied?)"
    )


class TestEventsTableThreading:

  def test_events_table_passed_through_to_manager(self, fixture_paths):
    """``--events-table custom`` must reach
    ``OntologyGraphManager.from_ontology_binding(table_id=custom)``.
    A previous draft discovered from the configured table but
    extracted from the hard-coded default — silent split."""
    ontology_yaml, binding_yaml = fixture_paths
    now = _dt.datetime(2026, 5, 15, 14, 0, tzinfo=_dt.timezone.utc)
    client = _stub_bq_client([])

    captured: dict[str, str] = {}

    def _capture(**kwargs):
      captured["table_id"] = kwargs.get("table_id")
      return mock.Mock()

    with (
        mock.patch.object(mw, "_build_manager", side_effect=_capture),
        mock.patch(
            "bigquery_agent_analytics.ontology_materializer.OntologyMaterializer"
        ),
    ):
      mw.run_materialize_window(
          project_id="test-proj",
          dataset_id="test_ds",
          ontology_path=str(ontology_yaml),
          binding_path=str(binding_yaml),
          events_table="custom_events",
          lookback_hours=6.0,
          validate_binding=False,
          bq_client=client,
          run_started_at=now,
      )

    assert captured["table_id"] == "custom_events"


class TestCheckpointCarryForward:

  def test_failure_carries_forward_prior_checkpoint(self, fixture_paths):
    """When this run produces zero successful sessions but a
    prior checkpoint exists, the state row carries forward the
    prior watermark. Operators reading the most recent row see
    "still at X", not NULL."""
    ontology_yaml, binding_yaml = fixture_paths
    now = _dt.datetime(2026, 5, 15, 14, 0, tzinfo=_dt.timezone.utc)
    prior = _dt.datetime(2026, 5, 15, 12, 0, tzinfo=_dt.timezone.utc)
    fail_ts = _dt.datetime(2026, 5, 15, 13, 0, tzinfo=_dt.timezone.utc)

    # Client wired so state-read returns the prior checkpoint.
    client = mock.Mock()
    state_row = mock.Mock(last_completion_at=prior)
    discovered = [_FakeBQRow(session_id="s-fail", completion_timestamp=fail_ts)]
    client.query = mock.Mock(
        side_effect=[
            mock.Mock(result=mock.Mock(return_value=[])),  # CREATE TABLE
            mock.Mock(result=mock.Mock(return_value=[state_row])),  # state read
            mock.Mock(result=mock.Mock(return_value=discovered)),  # discovery
        ]
    )
    client.insert_rows_json = mock.Mock(return_value=[])

    fake_manager = mock.Mock(spec=["spec", "extract_graph"])
    fake_manager.spec = mock.Mock()
    fake_manager.extract_graph = mock.Mock(
        side_effect=RuntimeError("simulated")
    )
    with (
        mock.patch.object(mw, "_build_manager", return_value=fake_manager),
        mock.patch(
            "bigquery_agent_analytics.ontology_materializer.OntologyMaterializer"
        ),
    ):
      result = mw.run_materialize_window(
          project_id="test-proj",
          dataset_id="test_ds",
          ontology_path=str(ontology_yaml),
          binding_path=str(binding_yaml),
          lookback_hours=24.0,
          validate_binding=False,
          bq_client=client,
          run_started_at=now,
      )

    assert not result.ok
    assert result.checkpoint_read == prior
    # Carry-forward: prior watermark preserved in the report.
    # (The DB-side filter ``last_completion_at IS NOT NULL``
    # would also handle this on read; the carry-forward is the
    # belt-and-suspenders observability win.)
    assert result.checkpoint_written == prior


class TestTableStatusesSurfaced:

  def test_table_statuses_propagate_into_result(self, fixture_paths):
    """``materialize_with_status`` returns per-table cleanup /
    insert status. The orchestrator must surface that into
    ``result.table_statuses`` so the JSON report shows which
    tables hit streaming-buffer-pinned delete_failed states."""
    ontology_yaml, binding_yaml = fixture_paths
    now = _dt.datetime(2026, 5, 15, 14, 0, tzinfo=_dt.timezone.utc)
    ts = _dt.datetime(2026, 5, 15, 13, 0, tzinfo=_dt.timezone.utc)
    discovered = [_FakeBQRow(session_id="s-1", completion_timestamp=ts)]
    client = _stub_bq_client(discovered)

    # Fake a TableStatus dataclass-like object.
    fake_table_status = mock.Mock()
    fake_table_status.table_ref = "p.d.entity"
    fake_table_status.rows_attempted = 5
    fake_table_status.rows_inserted = 5
    fake_table_status.cleanup_status = "deleted"
    fake_table_status.insert_status = "inserted"
    fake_table_status.idempotent = True
    fake_mat_result = mock.Mock()
    fake_mat_result.row_counts = {"Entity": 5}
    fake_mat_result.table_statuses = {"Entity": fake_table_status}

    fake_manager = mock.Mock()
    fake_manager.spec = mock.Mock()
    fake_manager.extract_graph = mock.Mock(return_value=mock.Mock())

    fake_materializer_cls = mock.Mock()
    fake_materializer = fake_materializer_cls.return_value
    fake_materializer.materialize_with_status = mock.Mock(
        return_value=fake_mat_result
    )

    with (
        mock.patch.object(mw, "_build_manager", return_value=fake_manager),
        mock.patch(
            "bigquery_agent_analytics.ontology_materializer.OntologyMaterializer",
            fake_materializer_cls,
        ),
    ):
      result = mw.run_materialize_window(
          project_id="test-proj",
          dataset_id="test_ds",
          ontology_path=str(ontology_yaml),
          binding_path=str(binding_yaml),
          lookback_hours=6.0,
          validate_binding=False,
          bq_client=client,
          run_started_at=now,
      )

    assert "Entity" in result.table_statuses
    assert result.table_statuses["Entity"]["cleanup_status"] == "deleted"
    assert result.table_statuses["Entity"]["rows_inserted"] == 5
    assert result.table_statuses["Entity"]["idempotent"] is True


class TestValidateBindingShortCircuit:

  def test_failing_validation_raises_before_extraction(self, fixture_paths):
    """``--validate-binding`` runs before extraction. A failing
    report short-circuits with a ValueError; the materializer
    is never constructed. This is the "fail before AI.GENERATE
    spend" contract from #161."""
    ontology_yaml, binding_yaml = fixture_paths
    now = _dt.datetime(2026, 5, 15, 14, 0, tzinfo=_dt.timezone.utc)
    client = _stub_bq_client([])

    fake_failure = mock.Mock(
        code=mock.Mock(value="missing_column"),
        binding_path="binding.entities[0].properties[0].column",
    )
    fake_report = mock.Mock()
    fake_report.ok = False
    fake_report.failures = [fake_failure]

    with (
        mock.patch(
            "bigquery_agent_analytics.binding_validation.validate_binding_against_bigquery",
            return_value=fake_report,
        ),
        mock.patch.object(mw, "_build_manager") as mock_build,
    ):
      with pytest.raises(ValueError, match="binding-validate failed"):
        mw.run_materialize_window(
            project_id="test-proj",
            dataset_id="test_ds",
            ontology_path=str(ontology_yaml),
            binding_path=str(binding_yaml),
            lookback_hours=6.0,
            validate_binding=True,
            bq_client=client,
            run_started_at=now,
        )
      # Manager was never constructed — extraction never started.
      mock_build.assert_not_called()

  def test_skipped_on_dry_run(self, fixture_paths):
    """``--dry-run`` already opts out of side effects. Even with
    ``--validate-binding`` on, dry-run skips the BQ-side check
    (the binding may legitimately be ahead of the deployed
    tables during preview)."""
    ontology_yaml, binding_yaml = fixture_paths
    now = _dt.datetime(2026, 5, 15, 14, 0, tzinfo=_dt.timezone.utc)
    client = _stub_bq_client([])

    with mock.patch(
        "bigquery_agent_analytics.binding_validation.validate_binding_against_bigquery"
    ) as mock_validate:
      mw.run_materialize_window(
          project_id="test-proj",
          dataset_id="test_ds",
          ontology_path=str(ontology_yaml),
          binding_path=str(binding_yaml),
          lookback_hours=6.0,
          validate_binding=True,
          dry_run=True,
          bq_client=client,
          run_started_at=now,
      )
      mock_validate.assert_not_called()
