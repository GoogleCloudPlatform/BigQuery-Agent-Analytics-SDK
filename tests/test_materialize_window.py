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
import pathlib
import subprocess
from unittest import mock

import pytest

from bigquery_agent_analytics import materialize_window as mw

# Stub reference-extractor module for tests that pass
# ``extraction_mode="compiled-only"``. The orchestrator's boundary
# check now requires ``reference_extractors_module`` (or
# ``bundles_root``) when compiled-only is set, so any test that
# would otherwise omit both has to pass this stub's dotted path.
# Tests using ``_patch_orchestrator_for_extraction`` never trigger
# the actual import (``_build_manager`` is mocked), but registering
# the module in ``sys.modules`` makes the test suite robust against
# any future refactor that lets the import path run.
_TEST_REF_MODULE = "bqaa_test_compiled_only_stub_ref"
_TEST_REF_DUMMY_EXTRACTOR = lambda event, spec: None  # noqa: E731

import sys as _sys
import types as _types

if _TEST_REF_MODULE not in _sys.modules:
  _stub_ref_mod = _types.ModuleType(_TEST_REF_MODULE)
  _stub_ref_mod.EXTRACTORS = {"TOOL_COMPLETED": _TEST_REF_DUMMY_EXTRACTOR}
  _sys.modules[_TEST_REF_MODULE] = _stub_ref_mod


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
        discovery_mode="terminal:AGENT_COMPLETED",
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
        discovery_mode="terminal:AGENT_COMPLETED",
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
        discovery_mode="terminal:AGENT_COMPLETED",
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
        discovery_mode="terminal:AGENT_COMPLETED",
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
  - the CREATE TABLE response (anything; we just check it was called)
  - one response per additive schema-migration ALTER
    (currently three: ``mode``, ``orphan_watermark``,
    ``flagged_session_ids``)
  - the state-table read (empty → bootstrap)
  - the discovery query (returns ``discovered_rows``)
  ``.insert_rows_json`` returns [] (no errors).
  """
  client = mock.Mock()

  # Each .query() call's .result() yields a different row set.
  # We sequence them via side_effect on the query() Mock. Keep the
  # ALTER-TABLE response count in sync with
  # ``_STATE_TABLE_MODE_MIGRATIONS`` in
  # ``src/bigquery_agent_analytics/materialize_window.py``.
  results = [
      mock.Mock(result=mock.Mock(return_value=[])),  # CREATE TABLE
      mock.Mock(result=mock.Mock(return_value=[])),  # ALTER mode
      mock.Mock(result=mock.Mock(return_value=[])),  # ALTER orphan_watermark
      mock.Mock(result=mock.Mock(return_value=[])),  # ALTER flagged_session_ids
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
            mock.Mock(result=mock.Mock(return_value=[])),  # ALTER ADD mode
            mock.Mock(
                result=mock.Mock(return_value=[])
            ),  # ALTER orphan_watermark
            mock.Mock(
                result=mock.Mock(return_value=[])
            ),  # ALTER flagged_session_ids
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

  def test_failing_validation_returns_structured_ok_false(self, fixture_paths):
    """``--validate-binding`` runs before extraction. A failing
    report short-circuits with a structured ``ok=False`` result;
    the materializer is never constructed and the CLI exits 1
    (expected failure), not exit 2 (unexpected internal error).

    Operators rely on the exit code shape: a binding drift is the
    failure mode this validator was added to catch, so it has to
    surface as a normal "this run did not succeed" — not as
    "the SDK itself blew up". This is the "fail before AI.GENERATE
    spend" contract from #161 with the right error-shape mapping."""
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
      result = mw.run_materialize_window(
          project_id="test-proj",
          dataset_id="test_ds",
          ontology_path=str(ontology_yaml),
          binding_path=str(binding_yaml),
          lookback_hours=6.0,
          validate_binding=True,
          bq_client=client,
          run_started_at=now,
      )
    # Result is structured ok=False, not an exception.
    assert result.ok is False
    assert result.sessions_discovered == 0
    assert len(result.failures) == 1
    assert result.failures[0]["error_code"] == "binding_validate_failed"
    assert "binding-validate failed" in result.failures[0]["error_detail"]
    # Manager was never constructed — extraction never started.
    mock_build.assert_not_called()
    # State row WAS appended — drift is recorded so the next run
    # sees the failure in the state table audit trail.
    assert client.insert_rows_json.call_count == 1

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


# ------------------------------------------------------------------ #
# Round-3 regressions                                                  #
# ------------------------------------------------------------------ #


class TestNumericGuardrails:
  """Operator-input guardrails. A typo like ``--lookback-hours=-6``
  produces a negative scan window; without these checks the
  arithmetic silently scans zero rows. The orchestrator must
  reject nonsense at the boundary before any BQ side effect."""

  @pytest.fixture
  def _paths(self, tmp_path):
    """Cheap fixture — the orchestrator should fail before any I/O,
    so we don't need a real ontology/binding pair. The numeric
    check runs at the top of the function, before file load."""
    return tmp_path / "ontology.yaml", tmp_path / "binding.yaml"

  def test_negative_lookback_hours_rejected(self, _paths):
    ontology_yaml, binding_yaml = _paths
    with pytest.raises(ValueError, match="--lookback-hours must be > 0"):
      mw.run_materialize_window(
          project_id="p",
          dataset_id="d",
          ontology_path=str(ontology_yaml),
          binding_path=str(binding_yaml),
          lookback_hours=-6.0,
          validate_binding=False,
      )

  def test_zero_lookback_hours_rejected(self, _paths):
    """Zero window is also nonsense — empty range, nothing to
    do. Reject for the same reason as negative."""
    ontology_yaml, binding_yaml = _paths
    with pytest.raises(ValueError, match="--lookback-hours must be > 0"):
      mw.run_materialize_window(
          project_id="p",
          dataset_id="d",
          ontology_path=str(ontology_yaml),
          binding_path=str(binding_yaml),
          lookback_hours=0.0,
          validate_binding=False,
      )

  def test_negative_overlap_minutes_rejected(self, _paths):
    """Overlap of zero is fine (no extra rewind). Negative is a
    typo — would compute a scan_start in the future and skip
    everything."""
    ontology_yaml, binding_yaml = _paths
    with pytest.raises(ValueError, match="--overlap-minutes must be >= 0"):
      mw.run_materialize_window(
          project_id="p",
          dataset_id="d",
          ontology_path=str(ontology_yaml),
          binding_path=str(binding_yaml),
          lookback_hours=6.0,
          overlap_minutes=-15.0,
          validate_binding=False,
      )

  def test_zero_max_sessions_rejected(self, _paths):
    """``--max-sessions 0`` would emit ``LIMIT 0`` and discover no
    sessions on every run — silent zero work. ``None`` is the
    "unlimited" sentinel; reject 0 and negative explicitly."""
    ontology_yaml, binding_yaml = _paths
    with pytest.raises(ValueError, match="--max-sessions must be unset or > 0"):
      mw.run_materialize_window(
          project_id="p",
          dataset_id="d",
          ontology_path=str(ontology_yaml),
          binding_path=str(binding_yaml),
          lookback_hours=6.0,
          max_sessions=0,
          validate_binding=False,
      )

  def test_negative_max_sessions_rejected(self, _paths):
    ontology_yaml, binding_yaml = _paths
    with pytest.raises(ValueError, match="--max-sessions must be unset or > 0"):
      mw.run_materialize_window(
          project_id="p",
          dataset_id="d",
          ontology_path=str(ontology_yaml),
          binding_path=str(binding_yaml),
          lookback_hours=6.0,
          max_sessions=-1,
          validate_binding=False,
      )

  def test_max_sessions_none_is_unlimited(self, fixture_paths):
    """``None`` is the unlimited sentinel — no LIMIT clause in the
    discovery SQL, no rejection. This is the load-bearing case
    for the default Cloud Run Job spec which doesn't pass
    ``--max-sessions``."""
    ontology_yaml, binding_yaml = fixture_paths
    now = _dt.datetime(2026, 5, 15, 14, 0, tzinfo=_dt.timezone.utc)
    client = _stub_bq_client([])
    with (
        mock.patch.object(mw, "_build_manager", return_value=mock.Mock()),
        mock.patch(
            "bigquery_agent_analytics.ontology_materializer.OntologyMaterializer"
        ),
    ):
      # No raise → the None path is accepted.
      mw.run_materialize_window(
          project_id="test-proj",
          dataset_id="test_ds",
          ontology_path=str(ontology_yaml),
          binding_path=str(binding_yaml),
          lookback_hours=6.0,
          max_sessions=None,
          validate_binding=False,
          bq_client=client,
          run_started_at=now,
      )


class TestBindingValidateDriftStructured:
  """P2.3: a binding-validate failure must return a structured
  ``ok=False`` result, not raise. The CLI maps ``not result.ok``
  to exit 1 (expected) — raising would map to exit 2 (unexpected
  internal error) and confuse the operator who's watching for the
  drift signal this validator was added to surface."""

  def test_drift_returns_ok_false_with_binding_validate_failed_code(
      self, fixture_paths
  ):
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

    with mock.patch(
        "bigquery_agent_analytics.binding_validation.validate_binding_against_bigquery",
        return_value=fake_report,
    ):
      result = mw.run_materialize_window(
          project_id="test-proj",
          dataset_id="test_ds",
          ontology_path=str(ontology_yaml),
          binding_path=str(binding_yaml),
          lookback_hours=6.0,
          validate_binding=True,
          bq_client=client,
          run_started_at=now,
      )

    assert result.ok is False
    assert result.failures[0]["error_code"] == "binding_validate_failed"
    assert "missing_column" in result.failures[0]["error_detail"]

  def test_drift_writes_state_row_for_audit_trail(self, fixture_paths):
    """Append-only state table is the audit log. A drift failure
    must be written there so the next run + downstream observability
    queries see it — silent failures are the worst kind."""
    ontology_yaml, binding_yaml = fixture_paths
    now = _dt.datetime(2026, 5, 15, 14, 0, tzinfo=_dt.timezone.utc)
    client = _stub_bq_client([])

    fake_report = mock.Mock()
    fake_report.ok = False
    fake_report.failures = [
        mock.Mock(
            code=mock.Mock(value="missing_table"),
            binding_path="binding.entities[0]",
        )
    ]

    with mock.patch(
        "bigquery_agent_analytics.binding_validation.validate_binding_against_bigquery",
        return_value=fake_report,
    ):
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

    # Exactly one state row written.
    assert client.insert_rows_json.call_count == 1
    written_payload = client.insert_rows_json.call_args[0][1][0]
    assert written_payload["ok"] is False
    assert "binding-validate failed" in written_payload["error_detail"]


class TestWorstStatusAggregation:
  """P2.4: when two sessions touch the same bound table, the
  aggregated ``table_statuses`` must NOT mask an earlier
  ``delete_failed`` with a later clean ``deleted``. Worst-status
  wins; operators rely on this surface to spot
  streaming-buffer-pinned tables."""

  def test_delete_failed_in_one_session_propagates_to_aggregate(self):
    """Session A fails to delete (streaming buffer pinned),
    session B's delete works clean. Final table_statuses must
    show ``delete_failed`` — the failure is the operator signal,
    not the success."""
    ts_a = _dt.datetime(2026, 5, 15, 10, tzinfo=_dt.timezone.utc)
    ts_b = _dt.datetime(2026, 5, 15, 11, tzinfo=_dt.timezone.utc)
    results = [
        mw.SessionResult(
            session_id="a",
            ok=True,
            completion_timestamp=ts_a,
            rows_materialized={"Entity": 3},
            table_statuses={
                "Entity": {
                    "table_ref": "p.d.entity",
                    "rows_attempted": 3,
                    "rows_inserted": 3,
                    "cleanup_status": "delete_failed",
                    "insert_status": "inserted",
                    "idempotent": False,
                }
            },
        ),
        mw.SessionResult(
            session_id="b",
            ok=True,
            completion_timestamp=ts_b,
            rows_materialized={"Entity": 2},
            table_statuses={
                "Entity": {
                    "table_ref": "p.d.entity",
                    "rows_attempted": 2,
                    "rows_inserted": 2,
                    "cleanup_status": "deleted",
                    "insert_status": "inserted",
                    "idempotent": True,
                }
            },
        ),
    ]
    r = mw._build_result(
        run_id="r",
        state_key="k",
        scan_start=_dt.datetime(2026, 5, 15, tzinfo=_dt.timezone.utc),
        scan_end=_dt.datetime(2026, 5, 15, 12, tzinfo=_dt.timezone.utc),
        checkpoint_read=None,
        checkpoint_written=ts_b,
        sessions_discovered=2,
        session_results=results,
        compiled_outcomes={
            "compiled_unchanged": 0,
            "compiled_filtered": 0,
            "fallback_for_event": 0,
        },
        ok=True,
    )
    # Worst-status wins: delete_failed beats deleted.
    assert r.table_statuses["Entity"]["cleanup_status"] == "delete_failed"
    # Rows sum across sessions.
    assert r.table_statuses["Entity"]["rows_attempted"] == 5
    assert r.table_statuses["Entity"]["rows_inserted"] == 5
    # Idempotent flag AND-ed — one non-idempotent session
    # contaminates the table's overall idempotency claim.
    assert r.table_statuses["Entity"]["idempotent"] is False

  def test_insert_failed_propagates_to_aggregate(self):
    """Insert-side parallel: ``insert_failed`` in one session must
    survive aggregation against ``inserted`` in another."""
    ts = _dt.datetime(2026, 5, 15, 10, tzinfo=_dt.timezone.utc)
    results = [
        mw.SessionResult(
            session_id="a",
            ok=True,
            completion_timestamp=ts,
            rows_materialized={"E": 1},
            table_statuses={
                "E": {
                    "table_ref": "p.d.e",
                    "rows_attempted": 1,
                    "rows_inserted": 0,
                    "cleanup_status": "deleted",
                    "insert_status": "insert_failed",
                    "idempotent": False,
                }
            },
        ),
        mw.SessionResult(
            session_id="b",
            ok=True,
            completion_timestamp=ts,
            rows_materialized={"E": 1},
            table_statuses={
                "E": {
                    "table_ref": "p.d.e",
                    "rows_attempted": 1,
                    "rows_inserted": 1,
                    "cleanup_status": "deleted",
                    "insert_status": "inserted",
                    "idempotent": True,
                }
            },
        ),
    ]
    r = mw._build_result(
        run_id="r",
        state_key="k",
        scan_start=ts,
        scan_end=ts,
        checkpoint_read=None,
        checkpoint_written=ts,
        sessions_discovered=2,
        session_results=results,
        compiled_outcomes={
            "compiled_unchanged": 0,
            "compiled_filtered": 0,
            "fallback_for_event": 0,
        },
        ok=True,
    )
    assert r.table_statuses["E"]["insert_status"] == "insert_failed"
    assert r.table_statuses["E"]["idempotent"] is False

  def test_all_clean_aggregate_remains_clean(self):
    """Sanity check — when nothing failed, the aggregate is clean
    too. Otherwise the worst-status logic would be over-
    pessimistic and the report would always look broken."""
    ts = _dt.datetime(2026, 5, 15, 10, tzinfo=_dt.timezone.utc)
    results = [
        mw.SessionResult(
            session_id="a",
            ok=True,
            completion_timestamp=ts,
            rows_materialized={"E": 1},
            table_statuses={
                "E": {
                    "table_ref": "p.d.e",
                    "rows_attempted": 1,
                    "rows_inserted": 1,
                    "cleanup_status": "deleted",
                    "insert_status": "inserted",
                    "idempotent": True,
                }
            },
        ),
        mw.SessionResult(
            session_id="b",
            ok=True,
            completion_timestamp=ts,
            rows_materialized={"E": 1},
            table_statuses={
                "E": {
                    "table_ref": "p.d.e",
                    "rows_attempted": 1,
                    "rows_inserted": 1,
                    "cleanup_status": "deleted",
                    "insert_status": "inserted",
                    "idempotent": True,
                }
            },
        ),
    ]
    r = mw._build_result(
        run_id="r",
        state_key="k",
        scan_start=ts,
        scan_end=ts,
        checkpoint_read=None,
        checkpoint_written=ts,
        sessions_discovered=2,
        session_results=results,
        compiled_outcomes={
            "compiled_unchanged": 0,
            "compiled_filtered": 0,
            "fallback_for_event": 0,
        },
        ok=True,
    )
    assert r.table_statuses["E"]["cleanup_status"] == "deleted"
    assert r.table_statuses["E"]["insert_status"] == "inserted"
    assert r.table_statuses["E"]["idempotent"] is True


class TestCompileBundleFingerprintInReport:
  """P3.1: when ``--bundles-root`` is set, the JSON report MUST
  carry the compiled-bundle fingerprint. Operators reading
  ``compiled_outcomes`` cross-reference with this to answer
  "which bundle actually ran in this window?" — the same question
  customers ask when telemetry from two adjacent runs looks
  different."""

  def test_fingerprint_resolved_and_surfaced(self, fixture_paths, tmp_path):
    """``--bundles-root`` set → fingerprint flows into both the
    dataclass field and the ``to_json()`` payload."""
    ontology_yaml, binding_yaml = fixture_paths
    now = _dt.datetime(2026, 5, 15, 14, 0, tzinfo=_dt.timezone.utc)

    bundles_root = tmp_path / "bundles"
    bundles_root.mkdir()
    bundle_dir = bundles_root / "bundle_v1"
    bundle_dir.mkdir()
    (bundle_dir / "manifest.json").write_text('{"fingerprint": "fp-abc123def"}')

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
          validate_binding=False,
          bundles_root=str(bundles_root),
          reference_extractors_module="bigquery_agent_analytics",
          bq_client=client,
          run_started_at=now,
      )

    assert result.compile_bundle_fingerprint == "fp-abc123def"
    js = result.to_json()
    assert js["compile_bundle_fingerprint"] == "fp-abc123def"

  def test_fingerprint_none_when_bundles_root_unset(self, fixture_paths):
    """Plain ``from_ontology_binding`` path (no compiled bundles)
    → ``compile_bundle_fingerprint`` is ``None``, not a string,
    not missing from the dict. Consumers can branch on `is None`
    without a KeyError."""
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
          validate_binding=False,
          bq_client=client,
          run_started_at=now,
      )

    assert result.compile_bundle_fingerprint is None
    js = result.to_json()
    assert "compile_bundle_fingerprint" in js
    assert js["compile_bundle_fingerprint"] is None


class TestStandaloneEntryHelp:
  """P2.1: ``bqaa-materialize-window --help`` must render
  ``Usage: bqaa-materialize-window [OPTIONS]`` — not the confusing
  ``bqaa-materialize-window materialize-window [OPTIONS]`` that an
  argv-injection hack would produce. The console-script alias is
  customer-facing (Cloud Run Job specs use it directly)."""

  def test_help_renders_clean_single_command_usage(self):
    """Invoke the entry point via Typer's ``CliRunner`` to capture
    ``--help`` output without spawning a subprocess. The check is
    on the literal Usage line — that's what shows up first when an
    operator runs the binary against ``--help``."""
    import typer
    from typer.testing import CliRunner

    from bigquery_agent_analytics.cli import materialize_window

    # Same construction the entry point uses.
    runner = CliRunner()
    # ``typer.run`` won't return; build the equivalent Typer app
    # explicitly and invoke ``--help`` against it.
    app = typer.Typer()
    app.command()(materialize_window)
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    # Usage line should start with the binary name + [OPTIONS],
    # NOT ``materialize-window materialize-window``.
    assert "materialize-window materialize-window" not in result.output


# ------------------------------------------------------------------ #
# Round-4 regressions                                                  #
# ------------------------------------------------------------------ #


class TestCheckpointNeverRegresses:
  """A later run must never write a ``last_completion_at`` older
  than the prior watermark. Two ways that could happen:

  * The ``--overlap-minutes`` window pulls in events older than
    the prior checkpoint. If a session inside the overlap succeeds
    but a *later* session fails, the loop's last success is the
    re-scanned (older) timestamp.
  * An out-of-order rerun re-discovers a stale window.

  In both cases, writing the older value would move the high-water
  mark backwards and re-process already-materialized rows on the
  next run (cost burn, not correctness — the materializer is
  idempotent — but the operator surface lies)."""

  def test_overlap_window_does_not_rewind_high_water_mark(self, fixture_paths):
    """Prior checkpoint at 14:00. Discovery returns a 13:50
    success (caught by ``--overlap-minutes``) followed by a 14:05
    failure. Without the max(), the orchestrator would write
    13:50 — regressing the watermark."""
    ontology_yaml, binding_yaml = fixture_paths
    now = _dt.datetime(2026, 5, 15, 14, 30, tzinfo=_dt.timezone.utc)
    prior = _dt.datetime(2026, 5, 15, 14, 0, tzinfo=_dt.timezone.utc)
    older_success = _dt.datetime(2026, 5, 15, 13, 50, tzinfo=_dt.timezone.utc)
    later_failure = _dt.datetime(2026, 5, 15, 14, 5, tzinfo=_dt.timezone.utc)

    client = mock.Mock()
    state_row = mock.Mock(last_completion_at=prior)
    discovered = [
        _FakeBQRow(session_id="s-old", completion_timestamp=older_success),
        _FakeBQRow(session_id="s-new", completion_timestamp=later_failure),
    ]
    client.query = mock.Mock(
        side_effect=[
            mock.Mock(result=mock.Mock(return_value=[])),  # DDL
            mock.Mock(result=mock.Mock(return_value=[])),  # ALTER mode
            mock.Mock(
                result=mock.Mock(return_value=[])
            ),  # ALTER orphan_watermark
            mock.Mock(
                result=mock.Mock(return_value=[])
            ),  # ALTER flagged_session_ids
            mock.Mock(result=mock.Mock(return_value=[state_row])),  # state read
            mock.Mock(result=mock.Mock(return_value=discovered)),  # discovery
        ]
    )
    client.insert_rows_json = mock.Mock(return_value=[])

    # First session succeeds (older timestamp); second raises.
    fake_manager = mock.Mock()
    fake_manager.spec = mock.Mock()
    fake_manager.extract_graph = mock.Mock(
        side_effect=[mock.Mock(), RuntimeError("simulated")]
    )
    fake_materializer_cls = mock.Mock()
    fake_mat_result = mock.Mock()
    fake_mat_result.row_counts = {"E": 1}
    fake_mat_result.table_statuses = {}
    fake_materializer_cls.return_value.materialize_with_status = mock.Mock(
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
          lookback_hours=24.0,
          overlap_minutes=30.0,
          validate_binding=False,
          bq_client=client,
          run_started_at=now,
      )

    # Critical: checkpoint did NOT regress to ``older_success``.
    # It stays at ``prior``, the higher watermark.
    assert result.checkpoint_written == prior
    assert result.checkpoint_written >= prior

  def test_advance_when_success_is_newer_than_prior(self, fixture_paths):
    """Sanity: when this run's last success IS newer than the
    prior watermark, the checkpoint must still advance. Otherwise
    the max-guard would freeze the watermark forever."""
    ontology_yaml, binding_yaml = fixture_paths
    now = _dt.datetime(2026, 5, 15, 14, 30, tzinfo=_dt.timezone.utc)
    prior = _dt.datetime(2026, 5, 15, 13, 0, tzinfo=_dt.timezone.utc)
    new_success = _dt.datetime(2026, 5, 15, 14, 0, tzinfo=_dt.timezone.utc)

    client = mock.Mock()
    state_row = mock.Mock(last_completion_at=prior)
    discovered = [
        _FakeBQRow(session_id="s-new", completion_timestamp=new_success),
    ]
    client.query = mock.Mock(
        side_effect=[
            mock.Mock(result=mock.Mock(return_value=[])),  # CREATE TABLE
            mock.Mock(result=mock.Mock(return_value=[])),  # ALTER mode
            mock.Mock(
                result=mock.Mock(return_value=[])
            ),  # ALTER orphan_watermark
            mock.Mock(
                result=mock.Mock(return_value=[])
            ),  # ALTER flagged_session_ids
            mock.Mock(result=mock.Mock(return_value=[state_row])),
            mock.Mock(result=mock.Mock(return_value=discovered)),
        ]
    )
    client.insert_rows_json = mock.Mock(return_value=[])

    fake_manager = mock.Mock()
    fake_manager.spec = mock.Mock()
    fake_manager.extract_graph = mock.Mock(return_value=mock.Mock())
    fake_materializer_cls = mock.Mock()
    fake_mat_result = mock.Mock()
    fake_mat_result.row_counts = {"E": 1}
    fake_mat_result.table_statuses = {}
    fake_materializer_cls.return_value.materialize_with_status = mock.Mock(
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

    assert result.checkpoint_written == new_success


class TestStateReadSqlOrdersByCompletion:
  """The state-read query must order by ``last_completion_at DESC,
  run_started_at DESC`` so an out-of-order run never shadows a
  higher watermark. Ordering by ``run_started_at`` alone would
  pick the most recent *run* even if it carried a lower watermark."""

  def test_orders_by_last_completion_then_run_started(self):
    sql = mw.build_state_select_sql("p.d._bqaa_materialization_state")
    order_idx = sql.index("ORDER BY")
    order_clause = sql[order_idx:]
    assert "last_completion_at DESC" in order_clause
    assert "run_started_at DESC" in order_clause
    # Non-NULL filter retained as defense-in-depth.
    assert "last_completion_at IS NOT NULL" in sql


class TestCompletionEventTypeGuardrail:
  """``--completion-event-type ""`` silently no-ops: every event
  fails the ``event_type = ""`` predicate, zero sessions are
  discovered, a clean heartbeat row is written, and the run looks
  healthy. Reject the typo at the boundary."""

  def test_empty_string_rejected(self, fixture_paths):
    ontology_yaml, binding_yaml = fixture_paths
    with pytest.raises(
        ValueError, match="--completion-event-type must be a non-empty string"
    ):
      mw.run_materialize_window(
          project_id="p",
          dataset_id="d",
          ontology_path=str(ontology_yaml),
          binding_path=str(binding_yaml),
          lookback_hours=6.0,
          completion_event_type="",
          validate_binding=False,
      )

  def test_whitespace_only_rejected(self, fixture_paths):
    """Whitespace-only is also nonsense — would bind ``event_type
    = "   "`` and match nothing. Treated identically to empty."""
    ontology_yaml, binding_yaml = fixture_paths
    with pytest.raises(
        ValueError, match="--completion-event-type must be a non-empty string"
    ):
      mw.run_materialize_window(
          project_id="p",
          dataset_id="d",
          ontology_path=str(ontology_yaml),
          binding_path=str(binding_yaml),
          lookback_hours=6.0,
          completion_event_type="   ",
          validate_binding=False,
      )

  def test_include_active_sessions_bypasses_check(self, fixture_paths):
    """``--include-active-sessions`` drops the event-type filter
    entirely (any session with at least one event in the window
    counts). The completion-event-type guard is irrelevant in
    that mode — don't false-reject an unused flag."""
    ontology_yaml, binding_yaml = fixture_paths
    now = _dt.datetime(2026, 5, 15, 14, 0, tzinfo=_dt.timezone.utc)
    client = _stub_bq_client([])
    with (
        mock.patch.object(mw, "_build_manager", return_value=mock.Mock()),
        mock.patch(
            "bigquery_agent_analytics.ontology_materializer.OntologyMaterializer"
        ),
    ):
      mw.run_materialize_window(
          project_id="test-proj",
          dataset_id="test_ds",
          ontology_path=str(ontology_yaml),
          binding_path=str(binding_yaml),
          lookback_hours=6.0,
          completion_event_type="",
          include_active_sessions=True,
          validate_binding=False,
          bq_client=client,
          run_started_at=now,
      )


class TestCliHelpExitCodeDocsMentionDrift:
  """The CLI help text must list binding-validation drift as one
  of the exit-1 failure modes. A previous draft only mentioned
  "session failure", which is misleading after the round-3 P2.3
  change that maps drift to exit 1 (was exit 2)."""

  def test_help_includes_drift_in_exit_1_description(self):
    """Render the materialize-window subcommand help and assert
    the exit-code section names drift explicitly."""
    from typer.testing import CliRunner

    from bigquery_agent_analytics.cli import app

    result = CliRunner().invoke(app, ["materialize-window", "--help"])
    assert result.exit_code == 0
    assert "drift" in result.output.lower()


# ------------------------------------------------------------------ #
# Round-5 regressions                                                  #
# ------------------------------------------------------------------ #


class TestStateKeyIncludesDiscoveryMode:
  """The checkpoint key must vary with the discovery predicate.
  Two regressions a missing mode component would allow:

  * Operator switches ``--completion-event-type`` from
    ``AGENT_COMPLETED`` to a custom event. The new predicate
    inherits the old high-water mark and skips historical
    completions for the new event type.
  * Debug ``--include-active-sessions`` run shares state with the
    production cron. The debug mode has no terminal-event filter
    and discovers different sessions; it could advance the
    production checkpoint past sessions production hasn't yet
    seen as completed."""

  def test_different_terminal_events_produce_different_keys(self):
    base = dict(
        project_id="p",
        dataset_id="d",
        graph_name="g",
        events_table="t",
        ontology_fingerprint="o",
        binding_fingerprint="b",
    )
    a = mw.compute_state_key(discovery_mode="terminal:AGENT_COMPLETED", **base)
    b = mw.compute_state_key(discovery_mode="terminal:CUSTOM_TERMINAL", **base)
    assert a != b

  def test_active_mode_differs_from_terminal_mode(self):
    """``--include-active-sessions`` debug mode must not share a
    state row with the production terminal-event predicate."""
    base = dict(
        project_id="p",
        dataset_id="d",
        graph_name="g",
        events_table="t",
        ontology_fingerprint="o",
        binding_fingerprint="b",
    )
    terminal = mw.compute_state_key(
        discovery_mode="terminal:AGENT_COMPLETED", **base
    )
    active = mw.compute_state_key(discovery_mode="active", **base)
    assert terminal != active


class TestStateKeyDiscoveryModeWiring:
  """End-to-end check that the orchestrator derives the right
  ``discovery_mode`` from its flags. A code review catches the
  string formula; this test catches a refactor that drops the
  threading."""

  def test_terminal_predicate_produces_terminal_mode_key(self, fixture_paths):
    """A normal cron run with ``--completion-event-type X`` →
    state_key matches a hand-computed
    ``compute_state_key(discovery_mode="terminal:X", ...)``."""
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
          completion_event_type="MY_TERMINAL",
          lookback_hours=6.0,
          validate_binding=False,
          bq_client=client,
          run_started_at=now,
      )

    # Same key derived from the same orchestrator inputs, with a
    # *different* discovery_mode, must differ. The contract under
    # test is the wiring, not the hash value itself.
    other = mw.run_materialize_window(
        project_id="test-proj",
        dataset_id="test_ds",
        ontology_path=str(ontology_yaml),
        binding_path=str(binding_yaml),
        completion_event_type="OTHER_TERMINAL",
        lookback_hours=6.0,
        validate_binding=False,
        bq_client=_stub_bq_client([]),
        run_started_at=now,
    )
    assert result.state_key != other.state_key

  def test_active_mode_state_key_differs_from_terminal(self, fixture_paths):
    """``--include-active-sessions`` produces a different
    state_key from a terminal-event run with the same other
    inputs, so debug runs don't share state with prod cron."""
    ontology_yaml, binding_yaml = fixture_paths
    now = _dt.datetime(2026, 5, 15, 14, 0, tzinfo=_dt.timezone.utc)

    with (
        mock.patch.object(mw, "_build_manager", return_value=mock.Mock()),
        mock.patch(
            "bigquery_agent_analytics.ontology_materializer.OntologyMaterializer"
        ),
    ):
      terminal = mw.run_materialize_window(
          project_id="test-proj",
          dataset_id="test_ds",
          ontology_path=str(ontology_yaml),
          binding_path=str(binding_yaml),
          lookback_hours=6.0,
          validate_binding=False,
          bq_client=_stub_bq_client([]),
          run_started_at=now,
      )
      active = mw.run_materialize_window(
          project_id="test-proj",
          dataset_id="test_ds",
          ontology_path=str(ontology_yaml),
          binding_path=str(binding_yaml),
          include_active_sessions=True,
          lookback_hours=6.0,
          validate_binding=False,
          bq_client=_stub_bq_client([]),
          run_started_at=now,
      )

    assert terminal.state_key != active.state_key


class TestCompletionEventTypeWhitespaceRejected:
  """``--completion-event-type " AGENT_COMPLETED "`` would bind a
  spaced value into the discovery predicate and produce a clean
  no-op heartbeat. Reject explicitly rather than stripping
  silently — silent normalization would diverge from what the
  operator typed."""

  def test_leading_whitespace_rejected(self, fixture_paths):
    ontology_yaml, binding_yaml = fixture_paths
    with pytest.raises(
        ValueError,
        match="--completion-event-type must not have leading or trailing",
    ):
      mw.run_materialize_window(
          project_id="p",
          dataset_id="d",
          ontology_path=str(ontology_yaml),
          binding_path=str(binding_yaml),
          lookback_hours=6.0,
          completion_event_type=" AGENT_COMPLETED",
          validate_binding=False,
      )

  def test_trailing_whitespace_rejected(self, fixture_paths):
    ontology_yaml, binding_yaml = fixture_paths
    with pytest.raises(
        ValueError,
        match="--completion-event-type must not have leading or trailing",
    ):
      mw.run_materialize_window(
          project_id="p",
          dataset_id="d",
          ontology_path=str(ontology_yaml),
          binding_path=str(binding_yaml),
          lookback_hours=6.0,
          completion_event_type="AGENT_COMPLETED ",
          validate_binding=False,
      )

  def test_inner_whitespace_accepted(self, fixture_paths):
    """Inner spaces are legal in BQ STRING values. Only outer
    whitespace is the operator-typo class; inner spacing is the
    operator's choice (e.g., a custom event named "user step")."""
    ontology_yaml, binding_yaml = fixture_paths
    now = _dt.datetime(2026, 5, 15, 14, 0, tzinfo=_dt.timezone.utc)
    client = _stub_bq_client([])
    with (
        mock.patch.object(mw, "_build_manager", return_value=mock.Mock()),
        mock.patch(
            "bigquery_agent_analytics.ontology_materializer.OntologyMaterializer"
        ),
    ):
      # No raise — inner whitespace passes.
      mw.run_materialize_window(
          project_id="test-proj",
          dataset_id="test_ds",
          ontology_path=str(ontology_yaml),
          binding_path=str(binding_yaml),
          lookback_hours=6.0,
          completion_event_type="user step",
          validate_binding=False,
          bq_client=client,
          run_started_at=now,
      )


# ------------------------------------------------------------------ #
# Round-7 regressions                                                  #
# ------------------------------------------------------------------ #


def _fake_mat_result(row_counts: dict[str, int]) -> mock.Mock:
  """Build a fake ``materialize_with_status`` return value with
  the given row_counts. Per-table statuses are auto-derived
  to match (clean ``deleted``/``inserted``, idempotent)."""
  result = mock.Mock()
  result.row_counts = dict(row_counts)
  result.table_statuses = {}
  for table, n in row_counts.items():
    ts = mock.Mock()
    ts.table_ref = f"p.d.{table}"
    ts.rows_attempted = n
    ts.rows_inserted = n
    ts.cleanup_status = "deleted"
    ts.insert_status = "inserted"
    ts.idempotent = True
    result.table_statuses[table] = ts
  return result


class TestEmptyExtractionNotOk:
  """Round-7 contract: a session that completes without raising
  but produces zero rows across every entity table is NOT a
  success. The live deploy in PR #166 surfaced the silent
  failure mode — ``AI.GENERATE`` failed per-event, the SDK
  swallowed the error, the graph was empty, and the orchestrator
  reported ``ok=true`` with empty ``rows_materialized``. The fix:
  treat zero-row extraction as a session failure with
  ``error_code="empty_extraction"`` and exit non-zero."""

  def test_all_sessions_zero_rows_reports_not_ok(self, fixture_paths):
    """All discovered sessions extract to empty graphs (e.g.,
    AI.GENERATE permission missing on the runtime SA). Expected:
    ``ok=false``, the first session reported as ``empty_extraction``
    failure, loop breaks (no waste of BQ quota on the rest)."""
    ontology_yaml, binding_yaml = fixture_paths
    now = _dt.datetime(2026, 5, 16, 12, 0, tzinfo=_dt.timezone.utc)
    discovered = [
        _FakeBQRow(
            session_id="s1",
            completion_timestamp=_dt.datetime(
                2026, 5, 16, 11, 0, tzinfo=_dt.timezone.utc
            ),
        ),
        _FakeBQRow(
            session_id="s2",
            completion_timestamp=_dt.datetime(
                2026, 5, 16, 11, 30, tzinfo=_dt.timezone.utc
            ),
        ),
    ]
    client = _stub_bq_client(discovered)

    fake_manager = mock.Mock()
    fake_manager.spec = mock.Mock()
    fake_manager.extract_graph = mock.Mock(return_value=mock.Mock())

    fake_materializer_cls = mock.Mock()
    fake_materializer = fake_materializer_cls.return_value
    fake_materializer.materialize_with_status = mock.Mock(
        return_value=_fake_mat_result({})  # zero rows, no tables
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

    assert not result.ok, (
        "empty extraction across every session must surface as "
        "result.ok=False"
    )
    assert result.sessions_discovered == 2
    assert result.sessions_materialized == 0
    assert result.sessions_failed == 1, (
        "loop should break on first empty session — second session "
        "not processed; reported failures count = 1"
    )
    assert result.failures, "failures list must include the empty session"
    assert result.failures[0]["error_code"] == "empty_extraction"
    assert "extraction" in result.failures[0]["error_detail"].lower()

  def test_partial_extraction_partial_failure_conservative_checkpoint(
      self, fixture_paths
  ):
    """Session 1 extracts non-empty rows; session 2 returns
    zero rows. Expected: partial-failure shape — ``ok=false``,
    session 1 counted as materialized, session 2 in failures
    with ``empty_extraction``, loop breaks at session 2,
    checkpoint advances ONLY to session 1's completion
    timestamp."""
    ontology_yaml, binding_yaml = fixture_paths
    now = _dt.datetime(2026, 5, 16, 12, 0, tzinfo=_dt.timezone.utc)
    ts1 = _dt.datetime(2026, 5, 16, 11, 0, tzinfo=_dt.timezone.utc)
    ts2 = _dt.datetime(2026, 5, 16, 11, 30, tzinfo=_dt.timezone.utc)
    ts3 = _dt.datetime(2026, 5, 16, 11, 45, tzinfo=_dt.timezone.utc)
    discovered = [
        _FakeBQRow(session_id="s1", completion_timestamp=ts1),
        _FakeBQRow(session_id="s2", completion_timestamp=ts2),
        _FakeBQRow(session_id="s3", completion_timestamp=ts3),
    ]
    client = _stub_bq_client(discovered)

    fake_manager = mock.Mock()
    fake_manager.spec = mock.Mock()
    fake_manager.extract_graph = mock.Mock(return_value=mock.Mock())

    fake_materializer_cls = mock.Mock()
    fake_materializer = fake_materializer_cls.return_value
    # Session 1: real rows. Session 2: empty extraction. Session
    # 3: would also have real rows but the loop should break
    # before reaching it.
    fake_materializer.materialize_with_status = mock.Mock(
        side_effect=[
            _fake_mat_result({"DecisionExecution": 1, "Candidate": 3}),
            _fake_mat_result({}),
            _fake_mat_result({"DecisionExecution": 1}),
        ]
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
    assert result.sessions_discovered == 3
    assert result.sessions_materialized == 1, "only s1 succeeded"
    assert result.sessions_failed == 1, "s2 failed; s3 never tried"
    assert result.failures[0]["session_id"] == "s2"
    assert result.failures[0]["error_code"] == "empty_extraction"
    # Conservative checkpoint: stop at s1's timestamp, NOT s2's
    # or s3's. Next run re-tries s2 (idempotent).
    assert result.checkpoint_written == ts1
    # Materialize was attempted exactly twice (s1 + s2), never
    # for s3 — the break-on-failure semantics save BQ quota.
    assert fake_materializer.materialize_with_status.call_count == 2

  def test_insert_failure_classified_as_materialization_failed(
      self, fixture_paths
  ):
    """The materializer can produce zero rows in
    ``row_counts`` for two distinct reasons: extraction
    returned an empty graph (``empty_extraction``) OR
    extraction produced rows but every insert failed
    (``materialization_failed``). The two failure modes need
    different operator response (AI/IAM vs dataset write-perm /
    schema) — classify them via ``table_statuses``.

    Insert-failure shape: ``rows_attempted > 0`` on some
    table, ``insert_status == "insert_failed"``, but
    ``row_counts == {}`` because only successful inserts
    populate row_counts (see
    ``ontology_materializer.py``)."""
    ontology_yaml, binding_yaml = fixture_paths
    now = _dt.datetime(2026, 5, 16, 12, 0, tzinfo=_dt.timezone.utc)
    discovered = [
        _FakeBQRow(
            session_id="s1",
            completion_timestamp=_dt.datetime(
                2026, 5, 16, 11, 0, tzinfo=_dt.timezone.utc
            ),
        ),
    ]
    client = _stub_bq_client(discovered)

    # Hand-build a ``materialize_with_status`` return value
    # that mimics insert-failure: row_counts empty,
    # table_statuses show real attempts that all failed.
    fake_mat = mock.Mock()
    fake_mat.row_counts = {}  # nothing succeeded → empty
    fake_mat.table_statuses = {}
    for table, n_attempted in (("DecisionExecution", 3), ("Candidate", 7)):
      ts = mock.Mock()
      ts.table_ref = f"p.d.{table}"
      ts.rows_attempted = n_attempted
      ts.rows_inserted = 0
      ts.cleanup_status = "deleted"
      ts.insert_status = "insert_failed"
      ts.idempotent = False
      fake_mat.table_statuses[table] = ts

    fake_manager = mock.Mock()
    fake_manager.spec = mock.Mock()
    fake_manager.extract_graph = mock.Mock(return_value=mock.Mock())

    fake_materializer_cls = mock.Mock()
    fake_materializer = fake_materializer_cls.return_value
    fake_materializer.materialize_with_status = mock.Mock(return_value=fake_mat)

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
    assert result.failures[0]["error_code"] == "materialization_failed", (
        f"insert-failed sessions must be classified as "
        f"materialization_failed, not empty_extraction; got "
        f"{result.failures[0]['error_code']}"
    )
    # The failure detail must name the specific tables that
    # failed, so operators don't have to dig through log
    # payloads.
    detail = result.failures[0]["error_detail"]
    assert "DecisionExecution" in detail
    assert "Candidate" in detail
    assert "insert_failed" in detail
    # Crucial: failed-session table_statuses must surface in
    # the aggregate report. Without this, an operator seeing
    # ``ok=false`` would have no per-table diagnostic at the
    # top level.
    assert "DecisionExecution" in result.table_statuses, (
        f"failed session's table_statuses must appear in the "
        f"aggregate report; got {sorted(result.table_statuses)}"
    )
    assert (
        result.table_statuses["DecisionExecution"]["insert_status"]
        == "insert_failed"
    )
    assert result.table_statuses["DecisionExecution"]["rows_attempted"] == 3

  def test_empty_window_remains_ok(self, fixture_paths):
    """``sessions_discovered == 0`` (no terminal events in the
    scan window) is a legitimate empty-window heartbeat. The
    empty-extraction guard MUST NOT flip this to ok=false —
    the new check is per-session and skipped when no sessions
    were discovered. The orchestrator's existing "empty
    session_results → ok=true" clause keeps holding."""
    ontology_yaml, binding_yaml = fixture_paths
    now = _dt.datetime(2026, 5, 16, 12, 0, tzinfo=_dt.timezone.utc)
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
          validate_binding=False,
          bq_client=client,
          run_started_at=now,
      )

    assert result.ok, (
        "empty window (zero sessions discovered) must remain "
        "ok=true — the empty-extraction guard is per-session and "
        "should not fire when no sessions were processed"
    )
    assert result.sessions_discovered == 0
    assert result.sessions_materialized == 0
    assert result.sessions_failed == 0
    assert result.failures == []


# ====================================================================== #
# Backfill mode + state-key suffix + ``mode`` column (PR A, issue #177)   #
# ====================================================================== #


class TestStateKeySuffixIsolation:
  """``state_key_suffix`` folds into the SHA so backfill / re-extraction
  runs occupy a distinct state-key namespace from the steady-state cron.
  Without the suffix the hash is byte-identical to the prior (suffix-less)
  computation, so existing checkpoints don't drift after the SDK upgrade.
  """

  _BASE = dict(
      project_id="proj",
      dataset_id="ds",
      graph_name="g",
      events_table="agent_events",
      ontology_fingerprint="ofp",
      binding_fingerprint="bfp",
      discovery_mode="terminal:AGENT_COMPLETED",
  )

  def test_suffix_unset_is_byte_identical_to_legacy_hash(self):
    legacy = mw.compute_state_key(**self._BASE)
    with_none = mw.compute_state_key(state_key_suffix=None, **self._BASE)
    with_empty = mw.compute_state_key(state_key_suffix="", **self._BASE)
    assert legacy == with_none, "suffix=None must not drift the hash"
    assert legacy == with_empty, (
        "suffix='' must not drift the hash either — env-var pass-through "
        "delivers empty strings for unset values; flipping the hash on "
        "those would silently invalidate every existing checkpoint"
    )

  def test_different_suffixes_produce_different_state_keys(self):
    week1 = mw.compute_state_key(
        state_key_suffix="backfill-may-w1", **self._BASE
    )
    week2 = mw.compute_state_key(
        state_key_suffix="backfill-may-w2", **self._BASE
    )
    steady = mw.compute_state_key(**self._BASE)
    assert week1 != week2
    assert week1 != steady
    assert week2 != steady


class TestBackfillValidation:
  """The orchestrator rejects misconfigurations at the boundary so an
  operator typo doesn't silently degrade to a no-op or, worse, pollute
  the steady-state checkpoint stream."""

  def test_backfill_requires_both_from_and_to(self, fixture_paths):
    ontology_yaml, binding_yaml = fixture_paths
    with pytest.raises(ValueError, match="--backfill requires both --from"):
      mw.run_materialize_window(
          project_id="p",
          dataset_id="d",
          ontology_path=str(ontology_yaml),
          binding_path=str(binding_yaml),
          lookback_hours=6.0,
          backfill=True,
          from_time=_dt.datetime(2026, 5, 1, tzinfo=_dt.timezone.utc),
          to_time=None,
      )

  def test_backfill_requires_from_less_than_to(self, fixture_paths):
    ontology_yaml, binding_yaml = fixture_paths
    with pytest.raises(ValueError, match="requires --from < --to"):
      mw.run_materialize_window(
          project_id="p",
          dataset_id="d",
          ontology_path=str(ontology_yaml),
          binding_path=str(binding_yaml),
          lookback_hours=6.0,
          backfill=True,
          # Pass a suffix so this test reaches the from<to check
          # instead of being short-circuited by the
          # suffix-required check.
          state_key_suffix="reversed-window",
          from_time=_dt.datetime(2026, 5, 8, tzinfo=_dt.timezone.utc),
          to_time=_dt.datetime(2026, 5, 1, tzinfo=_dt.timezone.utc),
      )

  def test_backfill_requires_state_key_suffix(self, fixture_paths):
    """Regression for PR #188 review (P1): backfill without
    ``--state-key-suffix`` would write a state row under the
    steady-state ``state_key`` and silently rewind the next
    steady-state cron's high-water mark. ``read_last_checkpoint``
    filters only by ``state_key``, so ``mode='backfill'`` on the
    row is an audit signal that does NOT protect the checkpoint
    stream — the suffix is what carves out a distinct namespace.

    Asserted before any BigQuery client interaction: the
    validation runs at the boundary so the failure mode is loud,
    fast, and cheap. The fake client's ``query`` raises on call
    so the test fails if anything tries to hit BigQuery before
    the suffix check fires."""
    ontology_yaml, binding_yaml = fixture_paths
    bq_client = mock.Mock()
    bq_client.query = mock.Mock(
        side_effect=AssertionError(
            "BigQuery work must NOT start before the suffix check fires"
        )
    )
    with pytest.raises(
        ValueError, match="--backfill requires --state-key-suffix"
    ):
      mw.run_materialize_window(
          project_id="p",
          dataset_id="d",
          ontology_path=str(ontology_yaml),
          binding_path=str(binding_yaml),
          lookback_hours=6.0,
          backfill=True,
          from_time=_dt.datetime(2026, 5, 1, tzinfo=_dt.timezone.utc),
          to_time=_dt.datetime(2026, 5, 8, tzinfo=_dt.timezone.utc),
          state_key_suffix=None,
          bq_client=bq_client,
      )
    # Empty string is treated as unset too, matching the env-var
    # pass-through semantics in ``_parse_backfill_timestamp`` and
    # the env-var reader in ``run_job.py``.
    with pytest.raises(
        ValueError, match="--backfill requires --state-key-suffix"
    ):
      mw.run_materialize_window(
          project_id="p",
          dataset_id="d",
          ontology_path=str(ontology_yaml),
          binding_path=str(binding_yaml),
          lookback_hours=6.0,
          backfill=True,
          from_time=_dt.datetime(2026, 5, 1, tzinfo=_dt.timezone.utc),
          to_time=_dt.datetime(2026, 5, 8, tzinfo=_dt.timezone.utc),
          state_key_suffix="",
          bq_client=bq_client,
      )
    # Whitespace-only suffix is treated as unset too. Without the
    # boundary strip, ``"   "`` is truthy in Python and would slip
    # past the missing-suffix check, then become an opaque
    # whitespace token in the state-key hash — an unreadable
    # namespace that's nearly impossible to debug. The boundary
    # normalization in ``run_materialize_window`` makes the
    # behavior here identical to the empty-string case.
    with pytest.raises(
        ValueError, match="--backfill requires --state-key-suffix"
    ):
      mw.run_materialize_window(
          project_id="p",
          dataset_id="d",
          ontology_path=str(ontology_yaml),
          binding_path=str(binding_yaml),
          lookback_hours=6.0,
          backfill=True,
          from_time=_dt.datetime(2026, 5, 1, tzinfo=_dt.timezone.utc),
          to_time=_dt.datetime(2026, 5, 8, tzinfo=_dt.timezone.utc),
          state_key_suffix="   ",
          bq_client=bq_client,
      )

  def test_from_to_without_backfill_rejected(self, fixture_paths):
    ontology_yaml, binding_yaml = fixture_paths
    with pytest.raises(
        ValueError, match="--from and --to are only valid with --backfill"
    ):
      mw.run_materialize_window(
          project_id="p",
          dataset_id="d",
          ontology_path=str(ontology_yaml),
          binding_path=str(binding_yaml),
          lookback_hours=6.0,
          backfill=False,
          from_time=_dt.datetime(2026, 5, 1, tzinfo=_dt.timezone.utc),
          to_time=_dt.datetime(2026, 5, 8, tzinfo=_dt.timezone.utc),
      )


class TestBackfillTimestampParser:
  """``_parse_backfill_timestamp`` handles the formats env-var
  pass-through actually delivers."""

  def test_accepts_z_suffix_iso8601(self):
    parsed = mw._parse_backfill_timestamp("--from", "2026-05-01T00:00:00Z")
    assert parsed == _dt.datetime(2026, 5, 1, tzinfo=_dt.timezone.utc)

  def test_accepts_explicit_utc_offset(self):
    parsed = mw._parse_backfill_timestamp("--from", "2026-05-01T00:00:00+00:00")
    assert parsed == _dt.datetime(2026, 5, 1, tzinfo=_dt.timezone.utc)

  def test_none_and_empty_string_are_unset(self):
    assert mw._parse_backfill_timestamp("--from", None) is None
    assert mw._parse_backfill_timestamp("--from", "") is None
    assert mw._parse_backfill_timestamp("--from", "   ") is None

  def test_invalid_input_raises_with_flag_name(self):
    with pytest.raises(ValueError, match="--from must be a UTC ISO 8601"):
      mw._parse_backfill_timestamp("--from", "not-a-date")


class TestBackfillScanWindow:
  """The backfill scan window is the operator-supplied [from, to)
  range, not the lookback-derived window. The lookback cap does NOT
  clip the backfill — an operator backfilling six weeks of history
  must not have their window silently truncated to ``lookback_hours``."""

  def test_backfill_window_uses_from_to_directly(self, fixture_paths):
    ontology_yaml, binding_yaml = fixture_paths
    # ``run_started_at`` is fixed in 2026-05-20; the backfill window
    # is one full week earlier (May 1 → May 8). A lookback-derived
    # window would scan May 19 → May 20; backfill must scan the
    # explicit range instead.
    now = _dt.datetime(2026, 5, 20, 12, tzinfo=_dt.timezone.utc)
    from_ts = _dt.datetime(2026, 5, 1, tzinfo=_dt.timezone.utc)
    to_ts = _dt.datetime(2026, 5, 8, tzinfo=_dt.timezone.utc)
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
          lookback_hours=24.0,
          validate_binding=False,
          bq_client=client,
          run_started_at=now,
          backfill=True,
          from_time=from_ts,
          to_time=to_ts,
          state_key_suffix="backfill-may-w1",
      )
    assert result.window_start == from_ts, (
        "backfill scan_start must be the supplied --from, not "
        "lookback-derived from run_started_at"
    )
    assert (
        result.window_end == to_ts
    ), "backfill scan_end must be the supplied --to, not run_started_at"


class TestStateRowModeColumn:
  """``mode`` round-trips through ``append_state_row`` and identifies
  whether a state row was written by a steady-state or backfill run.
  Default is ``'steady'`` so pre-existing callers continue to write
  the expected value."""

  def test_state_row_defaults_to_steady_mode(self):
    row = mw.StateRow(
        state_key="sk",
        run_id="rid",
        run_started_at=_dt.datetime(2026, 5, 20, tzinfo=_dt.timezone.utc),
        scan_start=_dt.datetime(2026, 5, 20, tzinfo=_dt.timezone.utc),
        scan_end=_dt.datetime(2026, 5, 20, tzinfo=_dt.timezone.utc),
        last_completion_at=None,
        sessions_discovered=0,
        sessions_materialized=0,
        sessions_failed=0,
        ok=True,
    )
    assert row.mode == mw.STATE_MODE_STEADY

  def test_append_state_row_includes_mode_in_payload(self):
    client = mock.Mock()
    client.insert_rows_json = mock.Mock(return_value=[])
    row = mw.StateRow(
        state_key="sk",
        run_id="rid",
        run_started_at=_dt.datetime(2026, 5, 20, tzinfo=_dt.timezone.utc),
        scan_start=_dt.datetime(2026, 5, 20, tzinfo=_dt.timezone.utc),
        scan_end=_dt.datetime(2026, 5, 20, tzinfo=_dt.timezone.utc),
        last_completion_at=None,
        sessions_discovered=0,
        sessions_materialized=0,
        sessions_failed=0,
        ok=True,
        mode=mw.STATE_MODE_BACKFILL,
    )
    mw.append_state_row(client, "p.d.t", row)
    call_args = client.insert_rows_json.call_args
    payload = call_args[0][1][0]
    assert payload["mode"] == "backfill"


class TestAppendStateRowOmitsEmptyArrays:
  """BigQuery's streaming-insert API rejects empty-array values
  for ARRAY<STRING> columns with "Field value of
  flagged_session_ids cannot be empty." Live verification of PR
  #224 surfaced this on the very first cron pass when the
  watchdog wrote an ``orphan_scan`` row with zero new orphans.
  The fix omits ``flagged_session_ids`` from the payload when
  there's nothing to write (storing NULL); BigQuery accepts
  that and operators can still distinguish row kinds via the
  ``mode`` column."""

  def _state_row(self, **overrides):
    base = dict(
        state_key="sk",
        run_id="rid",
        run_started_at=_dt.datetime(2026, 5, 20, tzinfo=_dt.timezone.utc),
        scan_start=_dt.datetime(2026, 5, 20, tzinfo=_dt.timezone.utc),
        scan_end=_dt.datetime(2026, 5, 20, tzinfo=_dt.timezone.utc),
        last_completion_at=None,
        sessions_discovered=0,
        sessions_materialized=0,
        sessions_failed=0,
        ok=True,
    )
    base.update(overrides)
    return mw.StateRow(**base)

  def test_steady_row_omits_array_field_when_none(self):
    """Default StateRow (steady-mode caller) leaves
    ``flagged_session_ids=None``. The serialized payload must
    omit the field entirely so BigQuery stores NULL — the
    pre-#224 row shape."""
    client = mock.Mock()
    client.insert_rows_json = mock.Mock(return_value=[])
    mw.append_state_row(client, "p.d.t", self._state_row())
    payload = client.insert_rows_json.call_args[0][1][0]
    assert "flagged_session_ids" not in payload
    # Same omit-when-None rule for the other nullable columns.
    assert "orphan_watermark" not in payload
    assert "last_completion_at" not in payload

  def test_orphan_scan_with_zero_orphans_omits_array(self):
    """The crash reproducer: ``run_orphan_watchdog`` builds an
    ``orphan_scan`` row with ``flagged_session_ids=()`` on a
    zero-orphan scan. Sending ``[]`` as the JSON value crashed
    BigQuery; omitting the field stores NULL and the row writes
    cleanly."""
    client = mock.Mock()
    client.insert_rows_json = mock.Mock(return_value=[])
    cutoff = _dt.datetime(2026, 5, 20, tzinfo=_dt.timezone.utc)
    mw.append_state_row(
        client,
        "p.d.t",
        self._state_row(
            mode=mw.STATE_MODE_ORPHAN_SCAN,
            orphan_watermark=cutoff,
            flagged_session_ids=(),  # the crash trigger
        ),
    )
    payload = client.insert_rows_json.call_args[0][1][0]
    assert "flagged_session_ids" not in payload
    # Watermark IS populated on orphan rows and must be sent.
    assert payload["orphan_watermark"] == cutoff.isoformat().replace(
        "+00:00", "Z"
    )
    assert payload["mode"] == mw.STATE_MODE_ORPHAN_SCAN

  def test_orphan_row_with_flagged_sessions_sends_array(self):
    """The non-empty path stays unchanged: a populated
    ``flagged_session_ids`` tuple serializes as a list."""
    client = mock.Mock()
    client.insert_rows_json = mock.Mock(return_value=[])
    mw.append_state_row(
        client,
        "p.d.t",
        self._state_row(
            mode=mw.STATE_MODE_ORPHAN_LEDGER,
            orphan_watermark=_dt.datetime(2026, 5, 20, tzinfo=_dt.timezone.utc),
            flagged_session_ids=("orphan-A", "orphan-B"),
        ),
    )
    payload = client.insert_rows_json.call_args[0][1][0]
    assert payload["flagged_session_ids"] == ["orphan-A", "orphan-B"]


class TestEnsureStateTableMigration:
  """``ensure_state_table`` runs the schema-evolution ALTERs every call.
  ``ADD COLUMN IF NOT EXISTS`` is idempotent in BigQuery; the test
  asserts the calls are issued, not their side effect."""

  def test_ensure_state_table_runs_create_and_alter(self):
    client = mock.Mock()
    client.query = mock.Mock(
        return_value=mock.Mock(result=mock.Mock(return_value=[]))
    )
    mw.ensure_state_table(client, "p.d._bqaa_materialization_state")
    queries = [args[0][0] for args in client.query.call_args_list]
    create = next(q for q in queries if q.startswith("CREATE TABLE"))
    # Fresh tables include every current column in the initial DDL —
    # ``mode`` (issue #177) plus the orphan-watchdog columns
    # (issue #180: ``orphan_watermark``, ``flagged_session_ids``).
    assert "mode STRING" in create
    assert "orphan_watermark TIMESTAMP" in create
    assert "flagged_session_ids ARRAY<STRING>" in create
    alters = [q for q in queries if q.startswith("ALTER TABLE")]
    # Every additive column has an idempotent ADD COLUMN IF NOT
    # EXISTS so pre-migration tables get patched up without a
    # destructive migration.
    assert any("ADD COLUMN IF NOT EXISTS mode STRING" in q for q in alters)
    assert any(
        "ADD COLUMN IF NOT EXISTS orphan_watermark TIMESTAMP" in q
        for q in alters
    )
    assert any(
        "ADD COLUMN IF NOT EXISTS" in q
        and "flagged_session_ids ARRAY<STRING>" in q
        for q in alters
    )


# ====================================================================== #
# Extraction mode (PR B2, issue #178 follow-up)                            #
# ====================================================================== #


def _diag(code, **kwargs):
  """Build an ExtractionDiagnostic from kwargs without a Pydantic import
  chain in every test (keeps the dict-construction explicit and lets
  tests pass plain values regardless of the Pydantic version's
  ``model_construct`` quirks)."""
  from bigquery_agent_analytics.extracted_models import ExtractionDiagnostic

  return ExtractionDiagnostic(diagnostic_code=code, **kwargs)


def _patch_orchestrator_for_extraction(
    monkeypatch,
    *,
    diagnostics_per_session=None,
    nodes_per_session=None,
):
  """Patch ``_build_manager`` and ``OntologyMaterializer`` so the
  orchestrator runs end-to-end without BQ. Returns a tracker dict
  with the call counts the tests assert on.
  """
  from bigquery_agent_analytics import materialize_window as mw_mod
  from bigquery_agent_analytics.extracted_models import ExtractedGraph
  from bigquery_agent_analytics.extracted_models import ExtractedNode

  diagnostics_per_session = diagnostics_per_session or {}
  nodes_per_session = nodes_per_session or {}

  tracker = {
      "extract_calls": [],  # list of (session_id, kwargs) tuples
  }

  class _FakeManager:

    def __init__(self):
      self.spec = mock.Mock()
      self.extractors = {"E": lambda *_args, **_kw: None}

    def extract_graph(self, session_ids, *args, **kwargs):
      tracker["extract_calls"].append((tuple(session_ids), dict(kwargs)))
      sid = session_ids[0]
      return ExtractedGraph(
          name="g",
          nodes=[
              ExtractedNode(node_id=f"n-{sid}", entity_name="E")
              for _ in range(nodes_per_session.get(sid, 0))
          ],
          diagnostics=diagnostics_per_session.get(sid, []),
      )

  class _FakeMaterializeStatus:

    def __init__(self, row_counts, table_statuses):
      self.row_counts = row_counts
      self.table_statuses = table_statuses

  class _FakeMaterializer:

    def __init__(self, *_args, **_kwargs):
      pass

    def materialize_with_status(self, graph, session_ids):
      # Mirror what the real materializer would return: one row per
      # node, status entries with rows_attempted = rows_inserted.
      n = len(graph.nodes)
      table_statuses = {}
      if n:
        table_statuses["E"] = mock.Mock(
            table_ref="t",
            rows_attempted=n,
            rows_inserted=n,
            cleanup_status="deleted",
            insert_status="inserted",
            idempotent=True,
        )
      return _FakeMaterializeStatus(
          row_counts={"E": n} if n else {},
          table_statuses=table_statuses,
      )

  monkeypatch.setattr(mw_mod, "_build_manager", lambda **_kw: _FakeManager())
  monkeypatch.setattr(
      "bigquery_agent_analytics.ontology_materializer.OntologyMaterializer",
      _FakeMaterializer,
  )
  return tracker


class TestExtractionModeFlag:
  """Boundary contract for the new ``--extraction-mode`` flag."""

  def test_unknown_extraction_mode_rejected(self, fixture_paths):
    ontology_yaml, binding_yaml = fixture_paths
    with pytest.raises(ValueError, match=r"--extraction-mode must be one of"):
      mw.run_materialize_window(
          project_id="p",
          dataset_id="d",
          ontology_path=str(ontology_yaml),
          binding_path=str(binding_yaml),
          lookback_hours=6.0,
          extraction_mode="LLM_ONLY",
      )

  def test_compiled_only_without_extractor_source_rejected(self, fixture_paths):
    """``compiled-only`` without ``bundles_root`` or
    ``reference_extractors_module`` must raise at the boundary.

    Without an extractor source, ``_build_manager`` takes the
    legacy no-extractors path and ``extract_graph(...,
    run_structured=True, use_ai_generate=False,
    on_unhandled_span='fail')`` silently emits an empty graph
    with no diagnostics — defeating the typed
    ``empty_extraction`` failure surface compiled-only mode is
    supposed to guarantee. The boundary check makes the
    misconfiguration loud instead of silent."""
    ontology_yaml, binding_yaml = fixture_paths
    with pytest.raises(
        ValueError,
        match=r"--extraction-mode=compiled-only requires either",
    ):
      mw.run_materialize_window(
          project_id="p",
          dataset_id="d",
          ontology_path=str(ontology_yaml),
          binding_path=str(binding_yaml),
          lookback_hours=6.0,
          extraction_mode="compiled-only",
          # No bundles_root, no reference_extractors_module —
          # the misconfiguration this guard protects against.
      )

  def test_compiled_only_with_reference_module_accepted(
      self, fixture_paths, monkeypatch
  ):
    """The mirror case: compiled-only is fine when at least one of
    ``bundles_root`` or ``reference_extractors_module`` is set. The
    boundary check only rejects the both-missing combination."""
    ontology_yaml, binding_yaml = fixture_paths
    tracker = _patch_orchestrator_for_extraction(monkeypatch)
    client = _stub_bq_client([])  # zero discovered sessions; fast path
    # Should NOT raise — reference_extractors_module satisfies the
    # boundary check. ``_TEST_REF_MODULE`` is the stub registered
    # at module load time.
    mw.run_materialize_window(
        project_id="p",
        dataset_id="d",
        ontology_path=str(ontology_yaml),
        binding_path=str(binding_yaml),
        lookback_hours=6.0,
        validate_binding=False,
        bq_client=client,
        run_started_at=_dt.datetime.now(_dt.timezone.utc),
        extraction_mode="compiled-only",
        reference_extractors_module=_TEST_REF_MODULE,
    )
    # No sessions discovered ⇒ no extract calls; the test just
    # asserts the boundary check let the call through.
    assert tracker["extract_calls"] == []

  def test_default_is_ai_fallback(self, fixture_paths, monkeypatch):
    """``extraction_mode`` defaults to ``ai-fallback`` — existing
    callers see the legacy ``extract_graph(..., use_ai_generate=True)``
    path with no behavior change."""
    ontology_yaml, binding_yaml = fixture_paths
    tracker = _patch_orchestrator_for_extraction(monkeypatch)
    client = _stub_bq_client(
        [
            _FakeBQRow(
                session_id="s1",
                completion_timestamp=_dt.datetime.now(_dt.timezone.utc),
            )
        ]
    )
    mw.run_materialize_window(
        project_id="test-proj",
        dataset_id="test_ds",
        ontology_path=str(ontology_yaml),
        binding_path=str(binding_yaml),
        lookback_hours=6.0,
        validate_binding=False,
        bq_client=client,
        run_started_at=_dt.datetime.now(_dt.timezone.utc),
    )
    assert len(tracker["extract_calls"]) == 1
    _, kwargs = tracker["extract_calls"][0]
    assert kwargs == {"use_ai_generate": True}, (
        "default path must use the legacy bool surface so existing "
        "callers see byte-identical extraction behavior"
    )


class TestCompiledOnlyMode:
  """``extraction_mode='compiled-only'`` routes through B1's
  orthogonal-flag surface AND translates diagnostics into typed
  ``empty_extraction`` failures."""

  def test_compiled_only_uses_orthogonal_flags(
      self, fixture_paths, monkeypatch
  ):
    """The actual ``extract_graph`` invocation in compiled-only
    mode uses ``run_structured=True, use_ai_generate=False,
    on_unhandled_span='fail'`` — the B1 contract."""
    ontology_yaml, binding_yaml = fixture_paths
    # Empty diagnostics → clean session.
    tracker = _patch_orchestrator_for_extraction(
        monkeypatch,
        nodes_per_session={
            "s1": 3
        },  # at least one row so empty-extraction guard doesn't trip
    )
    now = _dt.datetime(2026, 5, 20, tzinfo=_dt.timezone.utc)
    client = _stub_bq_client(
        [_FakeBQRow(session_id="s1", completion_timestamp=now)]
    )
    mw.run_materialize_window(
        project_id="test-proj",
        dataset_id="test_ds",
        ontology_path=str(ontology_yaml),
        binding_path=str(binding_yaml),
        lookback_hours=6.0,
        validate_binding=False,
        bq_client=client,
        run_started_at=now,
        extraction_mode="compiled-only",
        reference_extractors_module=_TEST_REF_MODULE,
    )
    assert len(tracker["extract_calls"]) == 1
    _, kwargs = tracker["extract_calls"][0]
    assert kwargs == {
        "use_ai_generate": False,
        "run_structured": True,
        "on_unhandled_span": "fail",
    }, "compiled-only must opt into B1's orthogonal-flag surface"

  def test_unhandled_diagnostic_surfaces_empty_extraction(
      self, fixture_paths, monkeypatch
  ):
    ontology_yaml, binding_yaml = fixture_paths
    tracker = _patch_orchestrator_for_extraction(
        monkeypatch,
        diagnostics_per_session={
            "s1": [
                _diag(
                    "structured_unhandled",
                    span_id="span-x",
                    event_type="UNKNOWN",
                ),
            ],
        },
    )
    now = _dt.datetime(2026, 5, 20, tzinfo=_dt.timezone.utc)
    client = _stub_bq_client(
        [_FakeBQRow(session_id="s1", completion_timestamp=now)]
    )
    result = mw.run_materialize_window(
        project_id="test-proj",
        dataset_id="test_ds",
        ontology_path=str(ontology_yaml),
        binding_path=str(binding_yaml),
        lookback_hours=6.0,
        validate_binding=False,
        bq_client=client,
        run_started_at=now,
        extraction_mode="compiled-only",
        reference_extractors_module=_TEST_REF_MODULE,
    )
    assert result.ok is False, (
        "an unhandled diagnostic in compiled-only mode must flip "
        "the session result to ok=False"
    )
    assert result.failures, "failures[] must surface the diagnostic"
    assert result.failures[0]["error_code"] == "empty_extraction"
    detail = result.failures[0]["error_detail"]
    assert "span-x" in detail and "UNKNOWN" in detail, (
        "error_detail must name the offending span_id + event_type so "
        "operators can grep Cloud Logging for the failing event shape"
    )

  def test_extractor_exception_diagnostic_surfaces_empty_extraction(
      self, fixture_paths, monkeypatch
  ):
    """``extractor_exception`` diagnostics (extractor raised, B1's
    ``capture_extractor_exceptions=True`` path) are also fatal in
    compiled-only mode."""
    ontology_yaml, binding_yaml = fixture_paths
    tracker = _patch_orchestrator_for_extraction(
        monkeypatch,
        diagnostics_per_session={
            "s1": [
                _diag(
                    "extractor_exception",
                    span_id="span-boom",
                    event_type="E",
                    detail="RuntimeError: extractor crashed",
                ),
            ],
        },
    )
    now = _dt.datetime(2026, 5, 20, tzinfo=_dt.timezone.utc)
    client = _stub_bq_client(
        [_FakeBQRow(session_id="s1", completion_timestamp=now)]
    )
    result = mw.run_materialize_window(
        project_id="test-proj",
        dataset_id="test_ds",
        ontology_path=str(ontology_yaml),
        binding_path=str(binding_yaml),
        lookback_hours=6.0,
        validate_binding=False,
        bq_client=client,
        run_started_at=now,
        extraction_mode="compiled-only",
        reference_extractors_module=_TEST_REF_MODULE,
    )
    assert result.ok is False
    assert result.failures[0]["error_code"] == "empty_extraction"
    detail = result.failures[0]["error_detail"]
    assert "span-boom" in detail
    assert "extractor crashed" in detail, (
        "the captured exception detail must surface in error_detail "
        "so operators can pinpoint the extractor bug"
    )

  def test_compiled_only_clean_session_passes(self, fixture_paths, monkeypatch):
    """When the diagnostic stream has only handled codes (no
    unhandled, no exception), compiled-only mode passes
    materialization normally."""
    ontology_yaml, binding_yaml = fixture_paths
    tracker = _patch_orchestrator_for_extraction(
        monkeypatch,
        diagnostics_per_session={
            "s1": [
                _diag("structured_fully_handled", span_id="span-1"),
                _diag("structured_partially_handled", span_id="span-2"),
            ],
        },
        nodes_per_session={"s1": 5},
    )
    now = _dt.datetime(2026, 5, 20, tzinfo=_dt.timezone.utc)
    client = _stub_bq_client(
        [_FakeBQRow(session_id="s1", completion_timestamp=now)]
    )
    result = mw.run_materialize_window(
        project_id="test-proj",
        dataset_id="test_ds",
        ontology_path=str(ontology_yaml),
        binding_path=str(binding_yaml),
        lookback_hours=6.0,
        validate_binding=False,
        bq_client=client,
        run_started_at=now,
        extraction_mode="compiled-only",
        reference_extractors_module=_TEST_REF_MODULE,
    )
    assert result.ok is True
    assert not result.failures
    assert result.sessions_materialized == 1

  def test_diagnostic_samples_capped_at_ten(self, fixture_paths, monkeypatch):
    """The error_detail caps diagnostic samples at 10 + says how
    many more exist — keeps Cloud Logging payloads readable when a
    customer's session has dozens of unhandled spans."""
    ontology_yaml, binding_yaml = fixture_paths
    many = [
        _diag(
            "structured_unhandled",
            span_id=f"span-{i}",
            event_type=f"TYPE_{i}",
        )
        for i in range(25)
    ]
    _patch_orchestrator_for_extraction(
        monkeypatch, diagnostics_per_session={"s1": many}
    )
    now = _dt.datetime(2026, 5, 20, tzinfo=_dt.timezone.utc)
    client = _stub_bq_client(
        [_FakeBQRow(session_id="s1", completion_timestamp=now)]
    )
    result = mw.run_materialize_window(
        project_id="test-proj",
        dataset_id="test_ds",
        ontology_path=str(ontology_yaml),
        binding_path=str(binding_yaml),
        lookback_hours=6.0,
        validate_binding=False,
        bq_client=client,
        run_started_at=now,
        extraction_mode="compiled-only",
        reference_extractors_module=_TEST_REF_MODULE,
    )
    detail = result.failures[0]["error_detail"]
    # Counts surface in the prose: "25 structured_unhandled" total.
    assert "25 structured_unhandled" in detail
    # The "(+N more not shown)" note appears (15 more = 25 - 10 cap).
    assert "+15 more" in detail


class TestCompiledOnlyMakesZeroLLMCalls:
  """The SDK contract that justifies dropping
  ``roles/aiplatform.user`` from the runtime SA's IAM in
  ``--extraction-mode=compiled-only`` mode. Asserts at the
  orchestrator boundary that compiled-only mode passes
  ``use_ai_generate=False`` to ``extract_graph`` and that the
  manager's ``_extract_via_ai_generate`` is never called. B1
  already pins that ``on_unhandled_span='fail'`` skips the AI
  branch; this test pins the materialize_window contract that
  routes through B1 correctly so a future regression is caught
  here before a customer's runtime SA starts billing Vertex AI.

  The deploy script (``deploy_cloud_run_job.sh``) uses this same
  contract: in compiled-only mode it skips the
  ``roles/aiplatform.user`` grant and idempotently removes any
  pre-existing grant on the same SA — see
  ``TestDeployScriptExtractionModeBoundary`` for the shell-side
  verification.
  """

  def test_compiled_only_extract_graph_use_ai_generate_is_false(
      self, fixture_paths, monkeypatch
  ):
    """The ``extract_graph`` kwargs in compiled-only mode include
    ``use_ai_generate=False``. Any future regression that flips
    this to True trips the test before a customer's runtime SA
    starts charging Vertex AI for calls it shouldn't be making."""
    ontology_yaml, binding_yaml = fixture_paths
    tracker = _patch_orchestrator_for_extraction(
        monkeypatch,
        nodes_per_session={"s1": 1},
    )
    now = _dt.datetime(2026, 5, 20, tzinfo=_dt.timezone.utc)
    client = _stub_bq_client(
        [_FakeBQRow(session_id="s1", completion_timestamp=now)]
    )
    mw.run_materialize_window(
        project_id="test-proj",
        dataset_id="test_ds",
        ontology_path=str(ontology_yaml),
        binding_path=str(binding_yaml),
        lookback_hours=6.0,
        validate_binding=False,
        bq_client=client,
        run_started_at=now,
        extraction_mode="compiled-only",
        reference_extractors_module=_TEST_REF_MODULE,
    )
    for _, kwargs in tracker["extract_calls"]:
      assert kwargs.get("use_ai_generate") is False, (
          "compiled-only must NOT pass use_ai_generate=True; that "
          "would route through _extract_via_ai_generate and bill "
          "Vertex AI on a deploy where roles/aiplatform.user has "
          "intentionally been dropped"
      )
      assert kwargs.get("on_unhandled_span") == "fail", (
          "compiled-only must pass on_unhandled_span='fail' so B1's "
          "_extract_graph_impl skips the AI branch (and the stub "
          "branch); any other value risks an LLM call"
      )

  def test_compiled_only_does_not_call_extract_via_ai_generate(self):
    """Inline B1-style integration: build a real
    ``OntologyGraphManager``-shaped object whose
    ``_extract_via_ai_generate`` raises if called, and verify
    that ``on_unhandled_span='fail'`` never reaches it. Belt-
    and-braces for the materializer→extract_graph contract."""
    from bigquery_agent_analytics.extracted_models import ExtractedGraph
    from bigquery_agent_analytics.ontology_graph import OntologyGraphManager

    mgr = OntologyGraphManager.__new__(OntologyGraphManager)
    mgr.extractors = {}
    mgr.spec = mock.Mock()
    mgr.spec.name = "g"
    mgr._fetch_raw_events = mock.Mock(return_value=[])
    mgr._extract_via_ai_generate = mock.Mock(
        side_effect=AssertionError(
            "_extract_via_ai_generate must NOT be called in compiled-only mode"
        )
    )
    mgr._extract_payloads = mock.Mock(
        side_effect=AssertionError(
            "_extract_payloads must NOT be called in compiled-only mode"
        )
    )
    result = mgr.extract_graph(
        ["s1"],
        use_ai_generate=False,
        run_structured=True,
        on_unhandled_span="fail",
    )
    # No assertion error fired — neither branch was called.
    assert isinstance(result, ExtractedGraph)
    assert mgr._extract_via_ai_generate.called is False
    assert mgr._extract_payloads.called is False


# ====================================================================== #
# _build_manager reference-only path                                      #
# ====================================================================== #


class TestBuildManagerReferenceOnly:
  """The deploy-path follow-up adds a third sub-path to
  ``_build_manager``: ``bundles_root=None`` AND
  ``reference_extractors_module=<dotted-path>``. The reference
  module's ``EXTRACTORS`` dict registers structured extractors on
  the manager directly — no compiled bundles, no AI.GENERATE.

  This is the compiled-only deploy story for customers who
  don't pre-compile fingerprint-stable bundles: ship the
  reference module, set ``BQAA_REFERENCE_EXTRACTORS_MODULE``,
  drop ``roles/aiplatform.user``. Customers who want compiled
  bundles still get the bundles path by additionally setting
  ``BQAA_BUNDLES_ROOT`` — the existing
  ``bundles_root is not None`` branch handles that case."""

  def test_reference_only_mode_loads_extractors_from_module(self, monkeypatch):
    """``_build_manager(bundles_root=None,
    reference_extractors_module='X')`` imports ``X`` and threads
    its ``EXTRACTORS`` dict into the manager via the
    ``extractors=`` kwarg on ``from_ontology_binding``."""
    import sys
    import types

    fake_mod = types.ModuleType("bqaa_test_ref_extractors")
    fake_mod.EXTRACTORS = {"TOOL_COMPLETED": lambda event, spec: None}
    monkeypatch.setitem(sys.modules, "bqaa_test_ref_extractors", fake_mod)

    captured = {}

    class _FakeManager:
      pass

    def _fake_from_ontology_binding(**kwargs):
      captured.update(kwargs)
      return _FakeManager()

    monkeypatch.setattr(
        "bigquery_agent_analytics.ontology_graph.OntologyGraphManager"
        ".from_ontology_binding",
        classmethod(lambda cls, **kw: _fake_from_ontology_binding(**kw)),
    )

    result = mw._build_manager(
        project_id="p",
        dataset_id="d",
        ontology=mock.Mock(),
        binding=mock.Mock(),
        location="US",
        bq_client=mock.Mock(),
        bundles_root=None,
        reference_extractors_module="bqaa_test_ref_extractors",
        outcome_callback=lambda *_a, **_k: None,
        table_id="agent_events",
    )
    assert isinstance(result, _FakeManager)
    assert captured["extractors"] is fake_mod.EXTRACTORS

  def test_reference_only_mode_rejects_empty_extractors(self, monkeypatch):
    """The ``EXTRACTORS`` dict must be a non-empty dict; the
    validator catches a stale / partially-wired reference module
    at the boundary rather than producing an empty-extractor
    manager that silently fails every span under
    ``on_unhandled_span='fail'``."""
    import sys
    import types

    fake_mod = types.ModuleType("bqaa_test_empty_ref")
    fake_mod.EXTRACTORS = {}
    monkeypatch.setitem(sys.modules, "bqaa_test_empty_ref", fake_mod)

    with pytest.raises(ValueError, match="non-empty EXTRACTORS dict"):
      mw._build_manager(
          project_id="p",
          dataset_id="d",
          ontology=mock.Mock(),
          binding=mock.Mock(),
          location="US",
          bq_client=mock.Mock(),
          bundles_root=None,
          reference_extractors_module="bqaa_test_empty_ref",
          outcome_callback=lambda *_a, **_k: None,
          table_id="agent_events",
      )

  def test_legacy_no_extractors_path_still_works(self, monkeypatch):
    """``bundles_root=None`` AND
    ``reference_extractors_module=None`` keeps the existing
    AI-only path: manager is constructed without extractors so
    ``extract_graph(..., use_ai_generate=True)`` falls through to
    ``AI.GENERATE``."""
    captured = {}

    class _FakeManager:
      pass

    def _fake_from_ontology_binding(**kwargs):
      captured.update(kwargs)
      return _FakeManager()

    monkeypatch.setattr(
        "bigquery_agent_analytics.ontology_graph.OntologyGraphManager"
        ".from_ontology_binding",
        classmethod(lambda cls, **kw: _fake_from_ontology_binding(**kw)),
    )

    mw._build_manager(
        project_id="p",
        dataset_id="d",
        ontology=mock.Mock(),
        binding=mock.Mock(),
        location="US",
        bq_client=mock.Mock(),
        bundles_root=None,
        reference_extractors_module=None,
        outcome_callback=lambda *_a, **_k: None,
        table_id="agent_events",
    )
    # ``extractors`` defaults to ``None`` (NOT an empty dict) so
    # ``from_ontology_binding`` doesn't register any structured
    # extractors and ``extract_graph`` routes through the
    # AI.GENERATE path.
    assert captured.get("extractors") is None


# ====================================================================== #
# Deploy-script boundary (PR B2 review P1)                                 #
# ====================================================================== #


class TestDeployScriptExtractionModeBoundary:
  """Mechanically verifies the deploy-script's ``--extraction-mode``
  contract. The contract is in shell, not Python, so each case
  shells out — but the contract still belongs in the test suite
  because the PR body advertises both modes as supported deploy
  paths and the conditional IAM grant as the user-visible behavior
  delta."""

  def _deploy_script_path(self) -> pathlib.Path:
    return (
        pathlib.Path(__file__).resolve().parents[1]
        / "examples"
        / "migration_v5"
        / "periodic_materialization"
        / "deploy_cloud_run_job.sh"
    )

  def _reference_extractor_path(self) -> pathlib.Path:
    return (
        pathlib.Path(__file__).resolve().parents[1]
        / "examples"
        / "migration_v5"
        / "reference_extractor.py"
    )

  def test_deploy_script_shell_syntax_clean(self):
    """``bash -n`` confirms the validator block + the conditional
    IAM grant parse cleanly."""
    script = self._deploy_script_path()
    if not script.exists():
      pytest.skip("deploy script not present in this checkout")
    result = subprocess.run(
        ["bash", "-n", str(script)], capture_output=True, text=True
    )
    assert (
        result.returncode == 0
    ), f"deploy script has a shell syntax error: {result.stderr}"

  def test_compiled_only_now_accepted_by_validator(self):
    """``--extraction-mode=compiled-only`` must pass the
    validator block (i.e., reach gcloud / actual deploy work
    before failing). The earlier ``B2``-era reject ("compiled-only
    is not yet supported") is gone — this test would have failed
    before the deploy-path follow-up, so it pins the lift."""
    script = self._deploy_script_path()
    if not script.exists():
      pytest.skip("deploy script not present in this checkout")
    # We don't have gcloud / a real GCP project in the test env,
    # so the script will fail later — but it must NOT fail with
    # the validator's compiled-only reject message.
    result = subprocess.run(
        [
            "bash",
            str(script),
            "--project",
            "p",
            "--region",
            "us-central1",
            "--events-dataset",
            "e",
            "--graph-dataset",
            "g",
            "--schedule",
            "0 */6 * * *",
            "--extraction-mode",
            "compiled-only",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    msg = result.stdout + result.stderr
    assert "is not yet supported" not in msg, (
        "deploy script still rejects compiled-only at the validator"
        f" block; full output:\n{msg}"
    )
    assert (
        "--extraction-mode must be 'ai-fallback' or 'compiled-only'" not in msg
    )

  def test_invalid_extraction_mode_value_rejected(self):
    """Operator typo path (``compiled_only`` with underscore, etc.)
    — the catch-all branch must reject with a clear error naming
    both supported modes."""
    script = self._deploy_script_path()
    if not script.exists():
      pytest.skip("deploy script not present in this checkout")
    result = subprocess.run(
        [
            "bash",
            str(script),
            "--project",
            "p",
            "--region",
            "us-central1",
            "--events-dataset",
            "e",
            "--graph-dataset",
            "g",
            "--schedule",
            "0 */6 * * *",
            "--extraction-mode",
            "compiled_only",
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert result.returncode != 0
    msg = result.stdout + result.stderr
    assert "ai-fallback" in msg
    assert "compiled-only" in msg

  def test_reference_extractor_present_for_compiled_only_staging(self):
    """Compiled-only mode imports
    ``BQAA_REFERENCE_EXTRACTORS_MODULE=reference_extractor`` at
    runtime; the module must exist next to the deploy artifact so
    the staging block can copy it into the Cloud Run image."""
    ref = self._reference_extractor_path()
    assert (
        ref.is_file()
    ), f"reference_extractor.py missing at {ref}; compiled-only deploy can't stage it"
    body = ref.read_text()
    assert (
        "EXTRACTORS = {" in body or "EXTRACTORS=" in body
    ), "reference_extractor.py must expose an EXTRACTORS dict"

  def test_compiled_only_wires_reference_extractor_env_var(self):
    """The deploy script's ENV_VARS array must set
    ``BQAA_REFERENCE_EXTRACTORS_MODULE=reference_extractor`` in
    compiled-only mode. Without this env var, ``_build_manager``
    would construct a manager with an empty extractor registry and
    ``on_unhandled_span='fail'`` would fail every session."""
    script = self._deploy_script_path()
    body = script.read_text()
    # Static check: the env var is wired inside an
    # ``if [[ "$EXTRACTION_MODE" == "compiled-only" ]]`` block.
    # We assert both the env var line and the guard appear in the
    # same script — a missing guard would set the env var
    # unconditionally, which is harmless but defeats the test of
    # the conditional path.
    assert "BQAA_REFERENCE_EXTRACTORS_MODULE=reference_extractor" in body
    assert '"$EXTRACTION_MODE" == "compiled-only"' in body

  def test_compiled_only_skips_aiplatform_user_grant(self):
    """The deploy script must guard the
    ``roles/aiplatform.user`` ``add-iam-policy-binding`` behind
    the ``ai-fallback`` branch, and idempotently remove the role
    on the ``compiled-only`` branch. Without the conditional, the
    "compiled-only ⇒ no Vertex AI dependency" story silently
    breaks for any customer who flips an existing ai-fallback
    deploy to compiled-only."""
    script = self._deploy_script_path()
    body = script.read_text()
    # Conditional gate present.
    assert '"$EXTRACTION_MODE" == "ai-fallback"' in body, (
        "deploy script must gate the aiplatform.user grant on"
        " --extraction-mode=ai-fallback"
    )
    # Idempotent remove on the compiled-only branch.
    assert "remove-iam-policy-binding" in body, (
        "deploy script must idempotently remove a pre-existing"
        " aiplatform.user grant when switching to compiled-only"
    )

  def test_compiled_only_iam_remove_distinguishes_absent_from_failure(self):
    """The compiled-only ``remove-iam-policy-binding`` must
    treat "binding doesn't exist" as success (idempotent first-
    deploy case) but surface every other failure
    (``PERMISSION_DENIED``, transient gcloud errors, org-policy
    rejects). The previous ``2>/dev/null || true`` swallowed all
    of them, so a real removal failure would print "Done." while
    the role silently stayed attached — contradicting the
    compiled-only "no Vertex AI IAM" guarantee.

    Static check: the remove block must capture stderr to a temp
    file, capture ``REMOVE_RC``, match on the
    "Policy binding not found" stderr signature for the
    absent-binding case, and ``exit "$REMOVE_RC"`` for every
    other non-zero exit."""
    script = self._deploy_script_path()
    body = script.read_text()
    assert "REMOVE_RC=" in body, (
        "deploy script must capture the remove-iam-policy-binding exit"
        " code instead of swallowing it"
    )
    assert "Policy binding not found" in body, (
        "deploy script must match gcloud's stable absent-binding"
        " stderr signature so other failures surface"
    )
    assert 'exit "$REMOVE_RC"' in body, (
        "deploy script must propagate a non-zero remove exit code"
        " for any failure other than the absent-binding case"
    )
    # The legacy "swallow everything" idiom must be gone from the
    # remove block. We allow ``|| true`` to appear elsewhere in
    # the script (the logging-tail block uses it for the harmless
    # "no logs yet" case), but we negatively assert against the
    # specific swallow shape the reviewer flagged.
    assert "--quiet 2>/dev/null || true" not in body, (
        "deploy script must not swallow remove-iam-policy-binding"
        " errors with `--quiet 2>/dev/null || true`; that hides"
        " PERMISSION_DENIED and transient gcloud failures behind"
        " a 'Done.' message"
    )

  def test_compiled_only_iam_remove_is_deferred_until_after_deploy(self):
    """The compiled-only ``remove-iam-policy-binding`` for
    ``roles/aiplatform.user`` must run AFTER ``gcloud run jobs
    deploy`` succeeds (and, if ``--smoke``, after the smoke
    execution). If we removed the role at the same point we
    grant the ai-fallback role, an early failure in the new
    deploy (buildpack failure, image registry hiccup, quota)
    would strip the existing schedule's Vertex AI access while
    the previous ai-fallback container is still scheduled —
    breaking the cron during a botched transition.

    This is the source-order check: the line offset of the
    compiled-only ``remove-iam-policy-binding`` must come AFTER
    ``gcloud run jobs deploy``. Without this assertion, a future
    refactor that moves the remove back next to the grant block
    would silently re-introduce the regression."""
    script = self._deploy_script_path()
    lines = script.read_text().splitlines()
    deploy_line = None
    remove_line = None
    for i, line in enumerate(lines):
      if deploy_line is None and "gcloud run jobs deploy" in line:
        deploy_line = i
      if "remove-iam-policy-binding" in line and remove_line is None:
        remove_line = i
    assert (
        deploy_line is not None
    ), "deploy script must contain a 'gcloud run jobs deploy' line"
    assert (
        remove_line is not None
    ), "deploy script must contain a 'remove-iam-policy-binding' line"
    assert remove_line > deploy_line, (
        "compiled-only remove-iam-policy-binding must come AFTER"
        " 'gcloud run jobs deploy' so a failed deploy doesn't strip"
        f" the existing schedule's IAM (remove at line {remove_line + 1},"
        f" deploy at line {deploy_line + 1})"
    )

  def test_smoke_failure_propagates_before_iam_remove(self):
    """If ``--smoke`` is requested and the smoke execution fails,
    the deploy script must exit BEFORE the compiled-only remove
    runs. Otherwise a smoke failure (new revision crashes on its
    first invocation) would still strip the existing schedule's
    Vertex AI access, leaving the customer with neither a working
    new deploy nor a recoverable old one.

    Static checks: the script must capture the smoke exit status
    and propagate a non-zero exit before the compiled-only remove
    block."""
    script = self._deploy_script_path()
    body = script.read_text()
    assert (
        "SMOKE_RC=" in body
    ), "deploy script must capture the smoke execution exit code"
    assert 'exit "$SMOKE_RC"' in body, (
        "deploy script must propagate a non-zero smoke exit code"
        " before the compiled-only IAM remove runs"
    )


# ====================================================================== #
# Orphan-session watchdog (PR D, issue #180)                              #
# ====================================================================== #


def _orphan_state_row(mode, *, flagged=(), watermark=None, run_started_at=None):
  """Build a fake BQ row matching the ``orphan_ledger`` /
  ``orphan_scan`` read shape."""
  row = mock.Mock()
  row.state_key = "sk"
  row.run_started_at = run_started_at or _dt.datetime(
      2026, 5, 20, tzinfo=_dt.timezone.utc
  )
  row.orphan_watermark = watermark
  row.flagged_session_ids = list(flagged)
  row.mode = mode
  return row


class TestOrphanWatchdogBuilders:
  """Pin the SQL shape the watchdog emits. Two reasons: (1) the
  discovery query has to use strict ``>`` on the watermark so the
  boundary session from the previous scan isn't reconsidered;
  (2) the cumulative ledger's running-set exclusion must be in
  the WHERE clause, not a post-filter, so BigQuery doesn't scan
  already-flagged sessions on every pass."""

  def test_orphan_select_uses_strict_gt_on_watermark(self):
    sql = mw.build_orphan_select_sql("p.d.agent_events")
    assert "timestamp > COALESCE(@orphan_watermark" in sql, (
        "discovery must use strict `>` on the watermark so the "
        "boundary session isn't reconsidered on the next scan"
    )

  def test_orphan_select_filters_terminal_event_count_zero(self):
    sql = mw.build_orphan_select_sql("p.d.agent_events")
    # ``completion_event_type`` must be parameter-bound, not a
    # literal — operators using ``--completion-event-type=MY_TERMINAL``
    # would otherwise see the watchdog flag sessions the steady
    # cron just successfully materialized (PR #224 P1).
    assert "event_type = @completion_event_type" in sql
    assert "'AGENT_COMPLETED'" not in sql
    assert "terminal_event_count = 0" in sql

  def test_orphan_select_excludes_already_flagged(self):
    sql = mw.build_orphan_select_sql("p.d.agent_events")
    assert "NOT IN UNNEST(@already_flagged)" in sql

  def test_orphan_ledger_select_filters_on_ledger_mode_only(self):
    sql = mw.build_orphan_ledger_select_sql("p.d._bqaa_materialization_state")
    # State table is shared across mode kinds; the ledger read
    # must filter on mode='orphan_ledger' so a fresh steady row
    # doesn't shadow the most recent ledger.
    assert "mode = 'orphan_ledger'" in sql

  def test_resolved_orphan_sql_bounded_to_flagged_set(self):
    sql = mw.build_resolved_orphan_sql("p.d.agent_events")
    # The probe must be parameterized on the previously-flagged set
    # rather than scanning the full events table.
    assert "IN UNNEST(@previously_flagged)" in sql
    # Completion event type is parameter-bound (PR #224 P1).
    assert "event_type = @completion_event_type" in sql
    assert "'AGENT_COMPLETED'" not in sql

  def test_resolved_orphan_sql_has_timestamp_partition_predicate(self):
    """PR #224 P2: without a timestamp predicate, the resolve probe
    can't prune partitions on the plugin's timestamp-partitioned
    events table — turning the watchdog into a query-byte
    regression as event history grows. Pin both bounds."""
    sql = mw.build_resolved_orphan_sql("p.d.agent_events")
    assert "timestamp > @resolve_lower_bound" in sql
    assert "timestamp <= @resolve_upper_bound" in sql


class TestOrphanWatchdogScan:
  """End-to-end scan via a stub BigQuery client. Covers the
  read-ledger / discover / probe-resolved / write-rows flow."""

  def _stub(self, *, ledger_rows, discovery_rows, resolved_rows):
    """Sequence: ledger select → discovery → resolved probe.
    ``ensure_state_table`` is NOT called by ``run_orphan_watchdog``
    itself (the caller handles that)."""
    client = mock.Mock()
    client.query = mock.Mock(
        side_effect=[
            mock.Mock(result=mock.Mock(return_value=ledger_rows)),
            mock.Mock(result=mock.Mock(return_value=discovery_rows)),
            mock.Mock(result=mock.Mock(return_value=resolved_rows)),
        ]
    )
    client.insert_rows_json = mock.Mock(return_value=[])
    return client

  def test_first_scan_flags_new_orphans_and_writes_both_rows(self):
    """Fresh state_key, no ledger row yet. Discovery returns two
    in-flight sessions. Both audit + ledger rows must be written
    with the new orphans + the cutoff watermark."""
    now = _dt.datetime(2026, 5, 20, 12, 0, tzinfo=_dt.timezone.utc)
    client = self._stub(
        ledger_rows=[],
        discovery_rows=[
            mock.Mock(
                session_id="orphan-A",
                first_event_at=now - _dt.timedelta(hours=12),
            ),
            mock.Mock(
                session_id="orphan-B",
                first_event_at=now - _dt.timedelta(hours=18),
            ),
        ],
        resolved_rows=[],
    )
    result = mw.run_orphan_watchdog(
        client,
        events_table_ref="p.d.agent_events",
        state_table_ref="p.d._bqaa_materialization_state",
        state_key="sk",
        run_id="run-1",
        run_started_at=now,
        max_session_age_hours=6.0,
    )
    assert result.new_orphans == ("orphan-A", "orphan-B")
    assert result.cumulative_flagged == ("orphan-A", "orphan-B")
    assert result.resolved_orphans == ()
    assert result.previous_watermark is None
    assert result.cutoff_at == now - _dt.timedelta(hours=6)

    # Two state rows written (audit + ledger).
    assert client.insert_rows_json.call_count == 2
    audit_payload = client.insert_rows_json.call_args_list[0][0][1][0]
    ledger_payload = client.insert_rows_json.call_args_list[1][0][1][0]
    assert audit_payload["mode"] == mw.STATE_MODE_ORPHAN_SCAN
    assert audit_payload["flagged_session_ids"] == ["orphan-A", "orphan-B"]
    assert ledger_payload["mode"] == mw.STATE_MODE_ORPHAN_LEDGER
    assert ledger_payload["flagged_session_ids"] == ["orphan-A", "orphan-B"]

  def test_carries_forward_previously_flagged_unless_resolved(self):
    """Second scan with previously-flagged sessions. One resolved
    (late terminal event arrived), one new orphan discovered.
    Cumulative ledger = (prior - resolved) ∪ new."""
    now = _dt.datetime(2026, 5, 20, 18, 0, tzinfo=_dt.timezone.utc)
    prior_watermark = now - _dt.timedelta(hours=6, minutes=30)
    client = self._stub(
        ledger_rows=[
            _orphan_state_row(
                "orphan_ledger",
                flagged=("orphan-A", "orphan-B", "orphan-C"),
                watermark=prior_watermark,
            )
        ],
        discovery_rows=[
            mock.Mock(
                session_id="orphan-D",
                first_event_at=now - _dt.timedelta(hours=10),
            )
        ],
        # Late terminal event for orphan-B → drop from ledger.
        resolved_rows=[mock.Mock(session_id="orphan-B")],
    )
    result = mw.run_orphan_watchdog(
        client,
        events_table_ref="p.d.agent_events",
        state_table_ref="p.d._bqaa_materialization_state",
        state_key="sk",
        run_id="run-2",
        run_started_at=now,
        max_session_age_hours=6.0,
    )
    assert result.new_orphans == ("orphan-D",)
    assert result.resolved_orphans == ("orphan-B",)
    # Order: carry-forward (in original ledger order, minus
    # resolved) + newly discovered. The append order matters for
    # operators reading the ledger as a stable diff target.
    assert result.cumulative_flagged == (
        "orphan-A",
        "orphan-C",
        "orphan-D",
    )
    assert result.previous_watermark == prior_watermark

  def test_empty_scan_still_writes_ledger_so_running_set_persists(self):
    """No new orphans this scan, but the ledger row must still be
    written so the cumulative set survives. Without this, a quiet
    scan would drop the running set and next scan would re-flag
    everything."""
    now = _dt.datetime(2026, 5, 20, 18, 0, tzinfo=_dt.timezone.utc)
    prior_watermark = now - _dt.timedelta(hours=6, minutes=30)
    client = self._stub(
        ledger_rows=[
            _orphan_state_row(
                "orphan_ledger",
                flagged=("orphan-A",),
                watermark=prior_watermark,
            )
        ],
        discovery_rows=[],
        resolved_rows=[],
    )
    result = mw.run_orphan_watchdog(
        client,
        events_table_ref="p.d.agent_events",
        state_table_ref="p.d._bqaa_materialization_state",
        state_key="sk",
        run_id="run-3",
        run_started_at=now,
        max_session_age_hours=6.0,
    )
    assert result.new_orphans == ()
    assert result.resolved_orphans == ()
    # Running set persists.
    assert result.cumulative_flagged == ("orphan-A",)
    # Ledger row written with the carry-forward set so next scan
    # reads the same running set even on a no-op scan.
    assert client.insert_rows_json.call_count == 2
    ledger_payload = client.insert_rows_json.call_args_list[1][0][1][0]
    assert ledger_payload["flagged_session_ids"] == ["orphan-A"]
    # Watermark advances every scan, regardless of outcome —
    # next scan's `>` boundary moves forward so cost stays bounded.
    # ``_iso`` shortens ``+00:00`` to ``Z`` in the serialized form.
    expected_cutoff = now - _dt.timedelta(hours=6)
    assert ledger_payload["orphan_watermark"] == (
        expected_cutoff.isoformat().replace("+00:00", "Z")
    )

  def test_rejects_non_positive_max_session_age_hours(self):
    with pytest.raises(ValueError, match="must be > 0"):
      mw.run_orphan_watchdog(
          mock.Mock(),
          events_table_ref="p.d.agent_events",
          state_table_ref="p.d._bqaa_materialization_state",
          state_key="sk",
          run_id="r",
          run_started_at=_dt.datetime.now(_dt.timezone.utc),
          max_session_age_hours=0,
      )

  def test_custom_completion_event_type_is_query_bound(self):
    """PR #224 P1: the watchdog must use the configured
    ``completion_event_type`` (matching the steady cron's
    ``--completion-event-type``) as a parameter binding, not the
    hardcoded ``'AGENT_COMPLETED'`` literal. Without this, deploys
    with a custom terminal event would see the watchdog falsely
    flag sessions that the steady cron just materialized."""
    from google.cloud.bigquery import ArrayQueryParameter
    from google.cloud.bigquery import ScalarQueryParameter

    now = _dt.datetime(2026, 5, 20, 18, 0, tzinfo=_dt.timezone.utc)
    client = self._stub(
        ledger_rows=[
            _orphan_state_row(
                "orphan_ledger",
                flagged=("orphan-A",),
                watermark=now - _dt.timedelta(hours=7),
            )
        ],
        discovery_rows=[],
        resolved_rows=[],
    )
    mw.run_orphan_watchdog(
        client,
        events_table_ref="p.d.agent_events",
        state_table_ref="p.d._bqaa_materialization_state",
        state_key="sk",
        run_id="run-custom",
        run_started_at=now,
        max_session_age_hours=6.0,
        completion_event_type="MY_TERMINAL",
    )
    # Extract every ScalarQueryParameter named completion_event_type
    # across the discovery + resolve calls and assert they all
    # carry the custom event name. Both queries must respect it.
    completion_params = []
    for call in client.query.call_args_list:
      job_config = call.kwargs.get("job_config")
      if job_config is None:
        continue
      for param in job_config.query_parameters:
        if (
            isinstance(param, ScalarQueryParameter)
            and param.name == "completion_event_type"
        ):
          completion_params.append(param.value)
    assert (
        len(completion_params) >= 2
    ), "completion_event_type must be bound on both discovery and resolve probes"
    assert all(
        v == "MY_TERMINAL" for v in completion_params
    ), f"watchdog must forward the configured event_type; saw {completion_params!r}"

  def test_resolved_probe_passes_timestamp_partition_bounds(self):
    """PR #224 P2: the resolve probe must carry
    ``resolve_lower_bound`` and ``resolve_upper_bound`` so BigQuery
    can prune partitions. Lower bound = previous ledger watermark,
    upper bound = run_started_at."""
    from google.cloud.bigquery import ScalarQueryParameter

    now = _dt.datetime(2026, 5, 20, 18, 0, tzinfo=_dt.timezone.utc)
    prior_watermark = now - _dt.timedelta(hours=6, minutes=30)
    client = self._stub(
        ledger_rows=[
            _orphan_state_row(
                "orphan_ledger",
                flagged=("orphan-A",),
                watermark=prior_watermark,
            )
        ],
        discovery_rows=[],
        resolved_rows=[],
    )
    mw.run_orphan_watchdog(
        client,
        events_table_ref="p.d.agent_events",
        state_table_ref="p.d._bqaa_materialization_state",
        state_key="sk",
        run_id="run-bounds",
        run_started_at=now,
        max_session_age_hours=6.0,
    )
    # The third query call is the resolve probe (ledger read,
    # discovery, resolve — in that order).
    resolve_call = client.query.call_args_list[2]
    params = {
        p.name: p
        for p in resolve_call.kwargs["job_config"].query_parameters
        if isinstance(p, ScalarQueryParameter)
    }
    assert "resolve_lower_bound" in params
    assert "resolve_upper_bound" in params
    assert params["resolve_lower_bound"].value == prior_watermark
    assert params["resolve_upper_bound"].value == now


class TestOrphanWatchdogOrchestratorIntegration:
  """The orchestrator threads orphan_result through
  MaterializeWindowResult and surfaces typed
  ``session_orphaned`` failures so existing #167 alerting fires."""

  def test_max_session_age_hours_none_skips_watchdog(
      self, fixture_paths, monkeypatch
  ):
    """Default off: no orphan-related queries, no `orphan` key in
    the JSON payload, ok stays True."""
    ontology_yaml, binding_yaml = fixture_paths
    _patch_orchestrator_for_extraction(monkeypatch)
    client = _stub_bq_client([])
    result = mw.run_materialize_window(
        project_id="p",
        dataset_id="d",
        ontology_path=str(ontology_yaml),
        binding_path=str(binding_yaml),
        lookback_hours=6.0,
        validate_binding=False,
        bq_client=client,
        run_started_at=_dt.datetime.now(_dt.timezone.utc),
    )
    assert result.orphan_result is None
    assert "orphan" not in result.to_json()
    assert result.ok is True

  def test_max_session_age_hours_skipped_in_backfill_mode(
      self, fixture_paths, monkeypatch
  ):
    """Backfill scans a fixed historical window where the
    ``what's still in-flight?`` question is undefined. The
    watchdog must skip even if the flag is set on a backfill run."""
    ontology_yaml, binding_yaml = fixture_paths
    _patch_orchestrator_for_extraction(monkeypatch)
    client = _stub_bq_client([])
    result = mw.run_materialize_window(
        project_id="p",
        dataset_id="d",
        ontology_path=str(ontology_yaml),
        binding_path=str(binding_yaml),
        lookback_hours=6.0,
        validate_binding=False,
        bq_client=client,
        run_started_at=_dt.datetime.now(_dt.timezone.utc),
        backfill=True,
        from_time=_dt.datetime(2026, 4, 1, tzinfo=_dt.timezone.utc),
        to_time=_dt.datetime(2026, 4, 8, tzinfo=_dt.timezone.utc),
        state_key_suffix="backfill-april",
        max_session_age_hours=6.0,
    )
    assert result.orphan_result is None

  def test_invalid_max_session_age_hours_rejected_at_boundary(
      self, fixture_paths
  ):
    """Negative / zero values are operator typos — fail fast at
    the boundary rather than producing a degenerate cutoff."""
    ontology_yaml, binding_yaml = fixture_paths
    for bad in (0, -1, -0.5):
      with pytest.raises(
          ValueError, match=r"--max-session-age-hours must be > 0"
      ):
        mw.run_materialize_window(
            project_id="p",
            dataset_id="d",
            ontology_path=str(ontology_yaml),
            binding_path=str(binding_yaml),
            lookback_hours=6.0,
            validate_binding=False,
            bq_client=_stub_bq_client([]),
            max_session_age_hours=bad,
        )


# ====================================================================== #
# Deploy-script boundary (issues #182 + #183 — split SAs + max-retries)   #
# ====================================================================== #


class TestDeployScriptHardening:
  """Static checks on ``deploy_cloud_run_job.sh`` for the
  production-posture defaults introduced by issues #182 + #183:
  split runtime + scheduler-caller SAs by default with
  ``--single-sa`` as the escape hatch, and ``--max-retries`` as
  a CLI-tunable knob (default 2) that propagates both into the
  ``gcloud run jobs deploy`` command and the runtime env."""

  def _deploy_script_path(self) -> pathlib.Path:
    return (
        pathlib.Path(__file__).resolve().parents[1]
        / "examples"
        / "migration_v5"
        / "periodic_materialization"
        / "deploy_cloud_run_job.sh"
    )

  def _script_body(self) -> str:
    return self._deploy_script_path().read_text()

  def test_deploy_script_shell_syntax_clean_after_hardening(self):
    script = self._deploy_script_path()
    if not script.exists():
      pytest.skip("deploy script not present in this checkout")
    result = subprocess.run(
        ["bash", "-n", str(script)], capture_output=True, text=True
    )
    assert (
        result.returncode == 0
    ), f"deploy script has a shell syntax error: {result.stderr}"

  def test_split_sa_is_default_runtime_and_scheduler_distinct(self):
    """The script must define the two SA names so the runtime
    holds BigQuery/Vertex roles and the scheduler-caller holds
    only ``roles/run.invoker`` — the least-privilege story
    issue #182 wants as the default."""
    body = self._script_body()
    assert (
        "bqaa-periodic-runtime-sa" in body
    ), "default deploy must create a dedicated runtime SA"
    assert (
        "bqaa-periodic-scheduler-sa" in body
    ), "default deploy must create a dedicated scheduler-caller SA"
    # Both SAs created via ``_ensure_sa`` calls.
    assert (
        '_ensure_sa "$RUNTIME_SA_NAME"' in body
        and '_ensure_sa "$SCHEDULER_SA_NAME"' in body
    ), "deploy script must create runtime + scheduler SAs"

  def test_single_sa_flag_falls_back_to_combined_identity(self):
    """``--single-sa`` flips back to the pre-#182 combined SA so
    customers who want the simpler identity model still have an
    explicit opt-in."""
    body = self._script_body()
    assert "--single-sa" in body
    assert (
        "SINGLE_SA=false" in body
    ), "the production default must be SINGLE_SA=false"
    # The combined-identity branch sets both names to the legacy
    # ``bqaa-periodic-sa`` so re-running with ``--single-sa``
    # on an existing combined deploy doesn't strand the old SA.
    assert (
        'if [[ "$SINGLE_SA" == "true" ]]; then' in body
        and 'RUNTIME_SA_NAME="bqaa-periodic-sa"' in body
        and 'SCHEDULER_SA_NAME="bqaa-periodic-sa"' in body
    )

  def test_cloud_run_uses_runtime_sa_scheduler_uses_scheduler_sa(self):
    """The two SAs must be wired to the right gcloud commands:
    ``--service-account`` on the Cloud Run Job points at the
    runtime SA (its BigQuery work needs the BQ roles); the
    scheduler's ``--oauth-service-account-email`` points at the
    scheduler-caller SA (only needs ``roles/run.invoker``)."""
    body = self._script_body()
    assert '--service-account "$RUNTIME_SA_EMAIL"' in body
    assert (
        '--oauth-service-account-email "$SCHEDULER_SA_EMAIL"' in body
    ), "scheduler must use scheduler-caller SA, not runtime SA"
    # Scheduler invoker grant lands on the scheduler-caller SA.
    assert '--member "serviceAccount:${SCHEDULER_SA_EMAIL}"' in body, (
        "roles/run.invoker on the Cloud Run Job must be granted "
        "to the scheduler-caller SA, not the runtime SA"
    )

  def test_scheduler_invoker_grant_uses_retry_iam(self):
    """PR #230 review P1: the same IAM-propagation race that
    ``_retry_iam`` defends against on the project-level grants
    also bites on the job-level ``roles/run.invoker`` grant —
    the scheduler-caller SA was created seconds earlier in the
    script's section 2, and ``gcloud run jobs add-iam-policy-
    binding`` can read from a different replica that hasn't
    seen it yet. Pre-fix the script ran the bare ``gcloud``
    invocation; first-time split-SA deploys could fail here
    after the Cloud Run Job was already deployed but before
    the scheduler trigger was wired."""
    body = self._script_body()
    # The invoker grant must be wrapped in ``_retry_iam`` (just
    # like the project-level grants), not run bare.
    assert "_retry_iam gcloud run jobs add-iam-policy-binding" in body, (
        "the scheduler-caller invoker grant must use _retry_iam to "
        "tolerate the IAM-propagation race on fresh split-SA deploys"
    )
    # And it must NOT keep the pre-fix bare invocation around
    # (defensive — a future refactor that re-introduces a
    # second un-wrapped grant would slip through otherwise).
    assert "\ngcloud run jobs add-iam-policy-binding" not in body, (
        "no bare ``gcloud run jobs add-iam-policy-binding`` left "
        "in the script — every invoker grant must route through "
        "_retry_iam"
    )

  def test_max_retries_flag_default_two_and_propagates(self):
    """``--max-retries`` defaults to 2 (issue #183 — was hard-
    coded 1) and propagates both into ``gcloud run jobs deploy``
    and the Cloud Run Job's env so the runtime's startup log
    surfaces it."""
    body = self._script_body()
    assert (
        'MAX_RETRIES="2"' in body
    ), "deploy script must default --max-retries to 2"
    assert (
        "--max-retries) " in body or "--max-retries)" in body
    ), "deploy script must accept --max-retries as a CLI flag"
    # The gcloud invocation must use the variable, not a literal.
    assert '--max-retries "$MAX_RETRIES"' in body, (
        "gcloud run jobs deploy must pass the configured retry "
        "count via the $MAX_RETRIES variable (was hard-coded 1)"
    )
    # And the runtime env must carry it for Cloud Logging
    # correlation.
    assert "BQAA_MAX_RETRIES=${MAX_RETRIES}" in body, (
        "deploy script must surface BQAA_MAX_RETRIES in the env "
        "var array so the runtime startup log can echo it"
    )

  def test_max_retries_rejects_non_integer(self):
    """Operator typo: ``--max-retries abc`` must fail at the
    deploy boundary, not surface as a less-obvious gcloud error
    after the build."""
    script = self._deploy_script_path()
    if not script.exists():
      pytest.skip("deploy script not present in this checkout")
    result = subprocess.run(
        [
            "bash",
            str(script),
            "--project",
            "p",
            "--region",
            "us-central1",
            "--events-dataset",
            "e",
            "--graph-dataset",
            "g",
            "--schedule",
            "0 */6 * * *",
            "--max-retries",
            "abc",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode != 0
    assert "non-negative integer" in (result.stdout + result.stderr)

  def test_run_job_reads_bqaa_max_retries_into_startup_log(self):
    """The runtime entrypoint must read ``BQAA_MAX_RETRIES``
    and include it in the structured startup log so operators
    correlating Cloud Monitoring alert noise with retry behaviour
    see the retry policy without ``gcloud run jobs describe``
    (issue #183)."""
    run_job = (
        pathlib.Path(__file__).resolve().parents[1]
        / "examples"
        / "migration_v5"
        / "periodic_materialization"
        / "run_job.py"
    )
    if not run_job.exists():
      pytest.skip("run_job.py not present in this checkout")
    body = run_job.read_text()
    assert "BQAA_MAX_RETRIES" in body
    # The startup log must thread the value through so it lands
    # in jsonPayload.
    assert "max_retries=" in body, (
        "run_job.py must pass max_retries into the _emit() call so "
        "the Cloud Logging payload carries it"
    )
