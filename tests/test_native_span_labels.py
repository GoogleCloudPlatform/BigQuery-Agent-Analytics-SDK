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
"""Tests for span-level G1 publication on the native snapshot (#469).

Everything is offline: the widget-stock silence session ``7e352c34`` is the
span-carrying in-memory fixture of ``test_span_taxonomy``, and publishing
runs against the fake BigQuery client of ``test_evalbench_importer``
extended with a span-labels store. Nothing reaches BigQuery, nothing writes
production ``agent_events``, and nothing starts the six-week clock.
"""

from __future__ import annotations

import json
import re

import pytest

from bigquery_agent_analytics import failure_taxonomy
from bigquery_agent_analytics import native_events
from bigquery_agent_analytics import span_taxonomy
from bigquery_agent_analytics.evalbench import _policy_column
from bigquery_agent_analytics.evalbench import _policy_from_column
from bigquery_agent_analytics.evalbench import _PUBLISH_BINDING_GUARD_MESSAGE
from bigquery_agent_analytics.evalbench import _SpanBindingState
from bigquery_agent_analytics.evalbench import EvalScorePolicy
from bigquery_agent_analytics.evalbench import failed_sessions
from bigquery_agent_analytics.native_events import NATIVE_SPAN_LABEL_POLICY
from bigquery_agent_analytics.native_events import NativeAgentEventsRun
from bigquery_agent_analytics.span_taxonomy import label_native_run
from tests.test_evalbench_importer import _FakeJob
from tests.test_evalbench_importer import _FakeManifestStore
from tests.test_evalbench_importer import _FakeSnapshot
from tests.test_evalbench_importer import _FakeTable
from tests.test_evalbench_importer import _FakeWriteClient
from tests.test_evalbench_importer import _scored_run
from tests.test_native_events_writer import _event
from tests.test_native_events_writer import _gold_events
from tests.test_native_events_writer import _IMPORTED_AT
from tests.test_native_events_writer import _JOB_ID
from tests.test_native_events_writer import _POLICY
from tests.test_native_events_writer import _SESSION_STUCK
from tests.test_native_events_writer import _SOURCE_PROJECT
from tests.test_native_events_writer import _SOURCE_TABLE
from tests.test_native_events_writer import _stuck_events
from tests.test_span_taxonomy import _AGENT_STARTING_SPAN
from tests.test_span_taxonomy import _gold_events_with_spans
from tests.test_span_taxonomy import _stuck_events_with_spans
from tests.test_span_taxonomy import _TRACE_STUCK
from tests.test_span_taxonomy import _with_spans

_SPAN_TABLE = "evalbench_span_labels"
_SPAN_REF = f"{_SOURCE_PROJECT}.bqaa_native.{_SPAN_TABLE}"
_SPAN_VIEW_REF = f"{_SPAN_REF}_pinned"
_BINDINGS_REF = (
    f"{_SOURCE_PROJECT}.bqaa_native.{native_events.SPAN_BINDINGS_TABLE}"
)
_FAILED_VIEW_REF = f"{_SOURCE_PROJECT}.bqaa_native.evalbench_failed_sessions"
_PIN = (_JOB_ID, "v1")
_G1_CATEGORIES = ("task/planning", "finalization", "tool blockers")


class _SpanLabelsFake(_FakeWriteClient):
  """The importer fake plus span-labels and span-binding tables.

  The span-label publication stages rows into an expiring staging table
  and then runs one lock-claiming transaction that checks the manifest
  generation AND view_policy, replaces the pin's slice, and upserts the
  job's binding row (``_SPAN_SYNC_SCRIPT``). The fake emulates that
  script with the same snapshot-isolation rules ``_FakeWriteClient`` uses
  for the publish transaction — pinned ``transaction_snapshot`` and the
  shared store included, so concurrent syncs can be modeled — and defers
  everything else (staging loads, view writes, the inherited publish) to
  the base fake. ``span_store`` / ``binding_store`` may be shared between
  fakes to model one dataset touched by two importers.
  ``span_load_error`` fails only the span staging load (the base publish
  still commits — the P1 #2 skew). ``doctor_native_manifest_read`` runs
  once, right before the span sync's own manifest re-read: a concurrent
  writer landing between derive and sync. ``stale_binding_reads`` makes
  every registry pre-read return nothing (a writer racing the first
  opt-in), and ``doctor_binding_read`` runs once right after a pre-read
  captured its rows: a concurrent binding landing in that window. Both
  model the r3 P1 races the in-boundary guards must fail closed.
  ``binding_transaction_snapshot`` pins the committed binding rows the
  in-transaction registry guards read (default: current committed
  state) — passing the rows captured before a concurrent commit models
  the r4 mutually-stale-snapshot overlap, where only the lock claim can
  serialize the policy recommit against the span-binding transaction.
  """

  def __init__(
      self,
      *,
      span_store: list[dict] | None = None,
      binding_store: list[dict] | None = None,
      span_load_error: Exception | None = None,
      doctor_native_manifest_read=None,
      stale_binding_reads: bool = False,
      doctor_binding_read=None,
      binding_transaction_snapshot: list[dict] | None = None,
      **kwargs,
  ) -> None:
    super().__init__(**kwargs)
    self.span_labels: list[dict] = span_store if span_store is not None else []
    self.span_bindings: list[dict] = (
        binding_store if binding_store is not None else []
    )
    self.span_deletes: list[tuple[str, dict]] = []
    self.span_load_error = span_load_error
    self.doctor_native_manifest_read = doctor_native_manifest_read
    self.stale_binding_reads = stale_binding_reads
    self.doctor_binding_read = doctor_binding_read
    self.binding_transaction_snapshot = binding_transaction_snapshot

  def get_table(self, table_ref: str):
    if table_ref.endswith("." + native_events.SPAN_BINDINGS_TABLE) and (
        self.span_bindings or table_ref in self.created
    ):
      self.get_table_calls.append(table_ref)
      return _FakeTable(table_ref, [], "bindings-etag")
    return super().get_table(table_ref)

  def load_table_from_json(self, rows, destination, job_config=None):
    if self.span_load_error is not None and destination.startswith(
        _SPAN_REF + "_staging_"
    ):
      self.loads.append((destination, list(rows), job_config))
      return _FakeJob(error=self.span_load_error)
    return super().load_table_from_json(rows, destination, job_config)

  def query(self, query: str, **kwargs) -> _FakeJob:
    if native_events._SPAN_STALE_PIN_MESSAGE in query:
      self.queries.append((query, kwargs))
      params = {p.name: p.value for p in kwargs["job_config"].query_parameters}
      snapshot = self.transaction_snapshot or self.store.snapshot()
      return self._span_sync(query, params, snapshot)
    marker = f".{native_events.SPAN_BINDINGS_TABLE}`"
    if marker in query and query.startswith("SELECT"):
      self.queries.append((query, kwargs))
      params = {p.name: p.value for p in kwargs["job_config"].query_parameters}
      rows = (
          []
          if self.stale_binding_reads
          else [
              dict(row)
              for row in self.span_bindings
              if row["job_id"] == params["job_id"]
          ]
      )
      hook, self.doctor_binding_read = self.doctor_binding_read, None
      if hook is not None:
        hook()
      return _FakeJob(rows)
    if (
        self.doctor_native_manifest_read is not None
        and ".evalbench_import_manifest`" in query
        and query.startswith("SELECT")
        and "ORDER BY" not in query
        and kwargs["job_config"].labels.get("sdk_feature")
        == "evalbench-native-import"
    ):
      hook, self.doctor_native_manifest_read = (
          self.doctor_native_manifest_read,
          None,
      )
      hook()
    return super().query(query, **kwargs)

  def _span_sync(self, script: str, params: dict, snapshot) -> _FakeJob:
    lock_table = re.search(r"UPDATE `([^`]+)`", script).group(1)
    assert lock_table.endswith(".evalbench_import_lock")
    if snapshot.lock_rows == 0:
      return _FakeJob(error=RuntimeError("400 lock sentinel is missing"))
    if snapshot.lock_claims != self.store.lock_claims:
      return _FakeJob(
          error=RuntimeError(
              "400 Transaction is aborted due to concurrent update against"
              f" table {lock_table}. Transaction ID: fake"
          )
      )
    pin = {
        "job_id": params["job_id"],
        "import_version": params["import_version"],
    }
    # The in-transaction guard: the manifest row must still carry the
    # exact generation AND canonical view_policy the rows derived under.
    current = [
        row
        for row in snapshot.rows
        if (row["job_id"], row["import_version"])
        == (pin["job_id"], pin["import_version"])
        and row.get("generation_id") == params["expected_generation_id"]
        and row.get("view_policy") == params["expected_view_policy"]
    ]
    if not current:
      return _FakeJob(
          error=RuntimeError(f"400 {native_events._SPAN_STALE_PIN_MESSAGE}")
      )
    # The in-transaction one-binding-per-job guard: a committed binding to
    # another span table fails this sync closed before any DML.
    assert "span_labels_table != @span_labels_table_name" in script
    assert script.index("@span_labels_table_name") < script.index(
        "DELETE FROM `"
    )
    if any(
        row["job_id"] == params["job_id"]
        and row["span_labels_table"] != params["span_labels_table_name"]
        for row in self.span_bindings
    ):
      return _FakeJob(
          error=RuntimeError(
              f"400 {native_events._SPAN_BINDING_CONFLICT_MESSAGE}"
          )
      )
    # The mirror in-transaction guard (P1 #469-r4-3): the requested span
    # table already belongs to ANOTHER job — rejected before any DML, so
    # the loser's rows and binding never commit.
    assert "job_id != @job_id" in script
    assert script.index("job_id != @job_id") < script.index("DELETE FROM `")
    if any(
        row["span_labels_table"] == params["span_labels_table_name"]
        and row["job_id"] != params["job_id"]
        for row in self.span_bindings
    ):
      return _FakeJob(
          error=RuntimeError(f"400 {native_events._SPAN_TABLE_OWNED_MESSAGE}")
      )
    ref = re.search(r"DELETE FROM `([^`]+)`", script).group(1)
    (staging_ref,) = re.findall(r"FROM `([^`]+_staging_[0-9a-f]+)`", script)
    staged = next(
        (rows for dest, rows, _ in reversed(self.loads) if dest == staging_ref),
        [],
    )
    assert f"DELETE FROM `{_BINDINGS_REF}`" in script
    self.store.lock_claims += 1
    self.span_deletes.append((ref, pin))
    self.span_labels[:] = [
        row
        for row in self.span_labels
        if (row["job_id"], row["import_version"])
        != (pin["job_id"], pin["import_version"])
    ] + [dict(row) for row in staged]
    self.span_bindings[:] = [
        row for row in self.span_bindings if row["job_id"] != params["job_id"]
    ] + [
        {
            "job_id": params["job_id"],
            "span_labels_table": params["span_labels_table_name"],
            "view_policy": params["expected_view_policy"],
            "import_version": params["import_version"],
            "generation_id": params["expected_generation_id"],
        }
    ]
    return _FakeJob()

  def _binding_guard_trips(self, predicate: str, params: dict) -> bool:
    """Evaluate a rendered registry predicate inside a transaction.

    The two fixed shapes ``_SpanBindingState.predicate`` renders:
    pre-read-none (EXISTS any row for the job) and pre-read-bound
    (NOT EXISTS the exact table+policy row). The expected values travel
    ONLY as query parameters — the P0 r4-1 contract — so the fake reads
    them from ``params`` and asserts no string literal ever reaches the
    predicate text. Reads see ``binding_transaction_snapshot`` when one
    is pinned, else the committed registry.
    """
    assert "'" not in predicate and '"' not in predicate
    bindings = (
        self.binding_transaction_snapshot
        if self.binding_transaction_snapshot is not None
        else self.span_bindings
    )
    rows = [r for r in bindings if r["job_id"] == params["job_id"]]
    if "@expected_span_labels_table" in predicate:
      assert predicate.lstrip().startswith("NOT EXISTS")
      assert "@expected_span_view_policy" in predicate
      expected = (
          params["expected_span_labels_table"],
          params["expected_span_view_policy"],
      )
      return not any(
          (r["span_labels_table"], r["view_policy"]) == expected for r in rows
      )
    assert predicate.lstrip().startswith("EXISTS")
    return bool(rows)

  def _span_binding_guard_result(self, script: str, params: dict):
    """The base publish transaction's in-boundary registry re-validation."""
    match = re.search(
        r"IF ((?:NOT )?EXISTS .+?) THEN\s*RAISE USING MESSAGE = '"
        + re.escape(_PUBLISH_BINDING_GUARD_MESSAGE),
        script,
        re.S,
    )
    if match is None:
      return None
    if self._binding_guard_trips(match.group(1), params):
      return _FakeJob(
          error=RuntimeError(f"400 {_PUBLISH_BINDING_GUARD_MESSAGE}")
      )
    return None

  def _recommit_binding_guard_trips(self, script: str, params: dict) -> bool:
    """The recommit's negated registry guard, inside its transaction."""
    predicate = re.search(
        r"AND NOT \((.+?)\);\s*COMMIT TRANSACTION", script, re.S
    )
    if predicate is None:
      return False
    return self._binding_guard_trips(predicate.group(1), params)


def _acceptance_run(extra_events=()):
  return NativeAgentEventsRun.from_agent_events(
      _stuck_events_with_spans()
      + _gold_events_with_spans()
      + list(extra_events),
      source_table=_SOURCE_TABLE,
      job_id=_JOB_ID,
  )


def _materialize(
    run,
    fake,
    *,
    import_version="v1",
    policy=_POLICY,
    span_labels_table=_SPAN_TABLE,
    **kwargs,
):
  return run.materialize(
      target_dataset="bqaa_native",
      import_version=import_version,
      imported_at=_IMPORTED_AT,
      policy=policy,
      span_labels_table=span_labels_table,
      bq_client=fake,
      **kwargs,
  )


# --- acceptance: the widget-stock silence is queryable as data ------------


def test_published_rows_are_exactly_the_three_library_labels() -> None:
  fake = _SpanLabelsFake()
  run = _acceptance_run()
  result = _materialize(run, fake)
  assert result.status == "imported"
  assert result.span_labels_table == _SPAN_REF
  assert result.span_label_row_count == 3
  assert result.span_labels_view == _SPAN_VIEW_REF

  expected = label_native_run(run, policy=_POLICY)
  assert len(expected) == 3
  assert fake.span_labels == [
      {
          "job_id": _JOB_ID,
          "import_version": "v1",
          "generation_id": result.manifest["generation_id"],
          "eval_id": label.eval_id,
          "session_id": label.session_id,
          "trace_id": label.trace_id,
          "span_id": label.span_id,
          "failure_category": label.failure_category,
          "evidence": label.evidence,
          "confidence": label.confidence,
          "target_kind": label.target_kind,
          "taxonomy_version": "0.1.0",
      }
      for label in expected
  ]


def test_widget_stock_rows_anchor_the_real_agent_starting_span() -> None:
  fake = _SpanLabelsFake()
  _materialize(_acceptance_run(), fake)
  rows = fake.span_labels
  assert [row["failure_category"] for row in rows] == [
      "task/planning",
      "finalization",
      "tool blockers",
  ]
  assert failure_taxonomy.TAXONOMY_VERSION == "0.1.0"
  for row in rows:
    assert (row["job_id"], row["import_version"]) == _PIN
    assert row["session_id"] == _SESSION_STUCK
    assert row["eval_id"] == "7e352c34"
    assert row["span_id"] == _AGENT_STARTING_SPAN
    assert row["trace_id"] == _TRACE_STUCK
    assert row["target_kind"] == span_taxonomy.TARGET_GAP_AFTER_SPAN
    assert row["confidence"] == span_taxonomy.MECHANICAL_CONFIDENCE
    assert row["taxonomy_version"] == "0.1.0"
  by_category = {row["failure_category"]: row["evidence"] for row in rows}
  assert "no TOOL_STARTING event follows" in by_category["tool blockers"]
  assert "check_inventory was never called" in by_category["tool blockers"]
  assert "no AGENT_COMPLETED event follows" in by_category["finalization"]
  assert "goes silent" in by_category["finalization"]
  assert "score gate failed" in by_category["task/planning"]


def test_session_level_g1_denominator_is_unchanged() -> None:
  fake = _SpanLabelsFake()
  _materialize(_acceptance_run(), fake)
  listing = failed_sessions(
      target_project=_SOURCE_PROJECT,
      target_dataset="bqaa_native",
      job_id=_JOB_ID,
      policy=_POLICY,
      bq_client=fake,
  )
  assert listing.session_count == 2
  assert listing.failed_count == 1
  (session,) = listing.sessions
  assert session.session_id == _SESSION_STUCK
  assert session.scenario_id == "7e352c34"
  assert session.taxonomy_categories == (
      "task/planning",
      "finalization",
      "tool blockers",
  )
  # Span rows join that verdict on the frozen eval_id, never replace it.
  assert {row["eval_id"] for row in fake.span_labels} == {session.scenario_id}


def test_first8_collision_publishes_the_full_session_id_eval_id() -> None:
  twin = "7e352c34-ffff-4fff-8fff-ffffffffffff"
  twin_events = _with_spans(
      [
          _event(twin, "USER_MESSAGE_RECEIVED", {"text_summary": "hi"}),
          _event(twin, "AGENT_STARTING", "You are a support agent.", offset=1),
      ],
      "77aa77aa77aa77aa77aa77aa77aa77aa",
      ["aaaa000011112222", "bbbb111122223333"],
  )
  fake = _SpanLabelsFake()
  _materialize(_acceptance_run(twin_events), fake)
  assert {row["eval_id"] for row in fake.span_labels} == {
      _SESSION_STUCK,
      twin,
  }


# --- no synthetic span identifiers ----------------------------------------


def test_rows_without_a_real_span_id_fail_the_publish_closed() -> None:
  # The span-free writer fixture: the stuck session's rows carry no
  # span_id, so span-label publication must refuse to invent one — and
  # must do so BEFORE anything is written anywhere.
  run = NativeAgentEventsRun.from_agent_events(
      _stuck_events() + _gold_events(),
      source_table=_SOURCE_TABLE,
      job_id=_JOB_ID,
  )
  fake = _SpanLabelsFake()
  with pytest.raises(ValueError, match="no span_id"):
    _materialize(run, fake)
  assert fake.queries == []
  assert fake.loads == []
  assert fake.created == []
  assert fake.span_labels == []
  assert fake.store.events == []
  assert fake.store.scores == []
  assert fake.store.rows == []


def test_every_published_span_id_comes_from_the_source_rows() -> None:
  fake = _SpanLabelsFake()
  _materialize(_acceptance_run(), fake)
  real_span_ids = {
      row["span_id"]
      for row in _stuck_events_with_spans() + _gold_events_with_spans()
  }
  for row in fake.span_labels:
    assert row["span_id"] in real_span_ids


# --- opt-in: the frozen #464 publish stays untouched by default -----------


def test_without_the_option_no_span_table_is_touched() -> None:
  fake = _SpanLabelsFake()
  result = _acceptance_run().materialize(
      target_dataset="bqaa_native",
      import_version="v1",
      imported_at=_IMPORTED_AT,
      policy=_POLICY,
      bq_client=fake,
  )
  assert result.status == "imported"
  assert result.span_labels_table is None
  assert result.span_label_row_count == 0
  assert fake.span_labels == []
  assert fake.span_deletes == []
  assert not any(_SPAN_TABLE in ref for ref in fake.created)


def test_span_free_corpora_still_publish_without_the_option() -> None:
  # The pre-#469 contract: a corpus whose rows carry no span_id columns
  # (the original writer fixture) publishes exactly as before.
  run = NativeAgentEventsRun.from_agent_events(
      _stuck_events() + _gold_events(),
      source_table=_SOURCE_TABLE,
      job_id=_JOB_ID,
  )
  fake = _SpanLabelsFake()
  result = run.materialize(
      target_dataset="bqaa_native",
      import_version="v1",
      imported_at=_IMPORTED_AT,
      policy=_POLICY,
      bq_client=fake,
  )
  assert result.status == "imported"
  assert result.span_labels_table is None


# --- pin isolation and idempotent resync ----------------------------------


def test_replace_and_unchanged_resync_without_duplicating_rows() -> None:
  fake = _SpanLabelsFake()
  _materialize(_acceptance_run(), fake)
  assert len(fake.span_labels) == 3

  # An unchanged re-import re-synchronizes the derived rows (like the
  # failed-sessions view) rather than duplicating them.
  result = _materialize(_acceptance_run(), fake)
  assert result.status == "unchanged"
  assert result.span_labels_table == _SPAN_REF
  assert result.span_label_row_count == 3
  assert len(fake.span_labels) == 3

  # A replace of the same version converges to the new derivation.
  replaced = _materialize(_acceptance_run(), fake, replace=True)
  assert replaced.status == "replaced"
  assert len(fake.span_labels) == 3
  assert {row["import_version"] for row in fake.span_labels} == {"v1"}


def test_two_versions_keep_their_own_span_rows() -> None:
  fake = _SpanLabelsFake()
  _materialize(_acceptance_run(), fake)
  changed = [
      _with_spans(
          [_event(_SESSION_STUCK, "AGENT_STARTING", "retry", offset=3)],
          _TRACE_STUCK,
          ["ffff0000ffff0000"],
      )[0]
  ]
  _materialize(_acceptance_run(changed), fake, import_version="v2")
  by_version: dict[str, set] = {}
  for row in fake.span_labels:
    by_version.setdefault(row["import_version"], set()).add(row["span_id"])
  # v1 anchors the original last span; v2 anchors the appended one. Both
  # pins keep their own rows — nothing merges retained versions.
  assert by_version["v1"] == {_AGENT_STARTING_SPAN}
  assert by_version["v2"] == {"ffff0000ffff0000"}
  assert all(len(rows) == 3 for rows in _group_rows(fake).values())


def _group_rows(fake) -> dict[str, list[dict]]:
  grouped: dict[str, list[dict]] = {}
  for row in fake.span_labels:
    grouped.setdefault(row["import_version"], []).append(row)
  return grouped


def test_a_session_that_passes_yields_no_span_rows() -> None:
  gold_only = NativeAgentEventsRun.from_agent_events(
      _gold_events_with_spans(),
      source_table=_SOURCE_TABLE,
      job_id=_JOB_ID,
  )
  fake = _SpanLabelsFake()
  result = _materialize(gold_only, fake)
  assert result.span_label_row_count == 0
  assert fake.span_labels == []
  # The table and the keyed delete still ran, so the contract is queryable
  # (and a replaced version cannot leave stale rows behind).
  assert any(ref == _SPAN_REF for ref in fake.created)
  assert fake.span_deletes == [
      (_SPAN_REF, {"job_id": _JOB_ID, "import_version": "v1"})
  ]


# --- frozen policy fallback (the #468 P1 finding) -------------------------


def test_no_policy_falls_back_to_the_frozen_goal_completion_gate() -> None:
  assert NATIVE_SPAN_LABEL_POLICY == EvalScorePolicy({"goal_completion": 1.0})
  fake = _SpanLabelsFake()
  _materialize(_acceptance_run(), fake, policy=None)
  categories = [row["failure_category"] for row in fake.span_labels]
  # An empty policy would drop task/planning; the frozen gate keeps it.
  assert categories == ["task/planning", "finalization", "tool blockers"]


# --- ONE effective policy for span rows AND the denominator ----------------


def _assert_session_and_span_rows_share_the_gate(fake, result) -> None:
  """The committed denominator and the span rows agree on the gate."""
  pinned = _policy_from_column(result.manifest["view_policy"])
  assert pinned == NATIVE_SPAN_LABEL_POLICY
  # The failed-sessions view renders the same gate the span rows used.
  assert "'goal_completion' AS comparator, 1.0 AS min_score" in (
      fake.store.views[_FAILED_VIEW_REF]
  )
  listing = failed_sessions(
      target_project=_SOURCE_PROJECT,
      target_dataset="bqaa_native",
      job_id=_JOB_ID,
      policy=pinned,
      bq_client=fake,
  )
  (session,) = listing.sessions
  assert session.session_id == _SESSION_STUCK
  assert session.taxonomy_categories == _G1_CATEGORIES
  assert [row["failure_category"] for row in fake.span_labels] == list(
      _G1_CATEGORIES
  )
  assert {row["eval_id"] for row in fake.span_labels} == {session.scenario_id}


def test_cli_default_none_policy_gates_the_denominator_too() -> None:
  # The thin CLI default (no --min-score => policy=None) must not publish
  # span rows from a gate the manifest / failed-sessions view never saw.
  fake = _SpanLabelsFake()
  result = _materialize(_acceptance_run(), fake, policy=None)
  _assert_session_and_span_rows_share_the_gate(fake, result)


def test_explicit_empty_policy_is_merged_to_the_frozen_gate() -> None:
  # EvalScorePolicy({}) is truthy: a `policy or FROZEN` fallback would keep
  # the empty gate and silently drop task/planning from the span rows
  # while the view records no score gate at all.
  fake = _SpanLabelsFake()
  result = _materialize(_acceptance_run(), fake, policy=EvalScorePolicy({}))
  _assert_session_and_span_rows_share_the_gate(fake, result)


def test_conflicting_goal_completion_threshold_fails_closed() -> None:
  fake = _SpanLabelsFake()
  with pytest.raises(ValueError, match="frozen gate"):
    _materialize(
        _acceptance_run(),
        fake,
        policy=EvalScorePolicy({"goal_completion": 0.5}),
    )
  assert fake.queries == [] and fake.loads == []
  assert fake.span_labels == [] and fake.store.rows == []


def test_resolver_merges_extra_comparators_and_keeps_the_gate() -> None:
  merged = native_events.resolve_span_label_policy(
      EvalScorePolicy({"accuracy": 0.9}, missing_score_fails=False)
  )
  assert merged.min_scores == {"accuracy": 0.9, "goal_completion": 1.0}
  assert merged.missing_score_fails is False
  assert native_events.resolve_span_label_policy(None) == (
      NATIVE_SPAN_LABEL_POLICY
  )
  assert native_events.resolve_span_label_policy(_POLICY) is _POLICY


# --- atomic, lock-serialized span replacement ------------------------------


def test_span_staging_load_failure_preserves_published_rows() -> None:
  store = _FakeManifestStore()
  shared: list[dict] = []
  _materialize(
      _acceptance_run(), _SpanLabelsFake(store=store, span_store=shared)
  )
  before = [dict(row) for row in shared]
  assert len(before) == 3

  broken = _SpanLabelsFake(
      store=store,
      span_store=shared,
      load_error=RuntimeError("staging load lost"),
  )
  with pytest.raises(ValueError, match="span labels could not be synchro"):
    _materialize(_acceptance_run(), broken)
  # The old rows survive intact — never a committed pin with zero span
  # rows — and the expiring staging table is still cleaned up.
  assert shared == before
  assert any("_staging_" in ref for ref in broken.deleted)


def test_concurrent_same_pin_syncs_cannot_duplicate_rows() -> None:
  store = _FakeManifestStore()
  shared: list[dict] = []
  _materialize(
      _acceptance_run(), _SpanLabelsFake(store=store, span_store=shared)
  )

  # Two unchanged re-imports whose span transactions both start from the
  # same committed snapshot (the offline race: DELETE(A), DELETE(B),
  # LOAD(A), LOAD(B) used to leave six rows). The lock claim serializes
  # them: BigQuery cancels the second instead of interleaving.
  snapshot = store.snapshot()
  first = _SpanLabelsFake(
      store=store, span_store=shared, transaction_snapshot=snapshot
  )
  second = _SpanLabelsFake(
      store=store, span_store=shared, transaction_snapshot=snapshot
  )
  result = _materialize(_acceptance_run(), first)
  assert result.status == "unchanged"
  with pytest.raises(ValueError, match="concurrent"):
    _materialize(_acceptance_run(), second)
  assert len(shared) == 3
  assert {row["import_version"] for row in shared} == {"v1"}


def test_replaced_generation_between_derive_and_sync_fails_closed() -> None:
  store = _FakeManifestStore()
  shared: list[dict] = []
  _materialize(
      _acceptance_run(), _SpanLabelsFake(store=store, span_store=shared)
  )
  stale_rows = [dict(row) for row in store.rows]
  _materialize(
      _acceptance_run(),
      _SpanLabelsFake(store=store, span_store=shared),
      replace=True,
  )
  before = [dict(row) for row in shared]

  # A sync whose transaction sees the pre-replace manifest generation (but
  # a current lock, so the generation guard — not the lock — decides).
  doctored = _FakeSnapshot(
      rows=stale_rows, lock_rows=1, lock_claims=store.lock_claims
  )
  late = _SpanLabelsFake(
      store=store, span_store=shared, transaction_snapshot=doctored
  )
  with pytest.raises(ValueError, match="no longer carries the generation"):
    _materialize(_acceptance_run(), late)
  assert shared == before


def test_span_replacement_is_staged_then_transactional() -> None:
  fake = _SpanLabelsFake()
  _materialize(_acceptance_run(), fake)
  (script,) = [
      sql
      for sql, _ in fake.queries
      if native_events._SPAN_STALE_PIN_MESSAGE in sql
  ]
  # One transaction claims the lock, checks the manifest generation, and
  # only then replaces the keyed slice from the staged rows.
  assert script.index("BEGIN TRANSACTION") < script.index("UPDATE `")
  assert script.index("UPDATE `") < script.index("generation_id")
  assert script.index("generation_id") < script.index("DELETE FROM `")
  assert script.index("DELETE FROM `") < script.index("INSERT INTO `")
  assert "ROLLBACK TRANSACTION" in script
  (staging_ref,) = re.findall(
      r"INSERT INTO `[^`]+` \([^)]+\)\s+SELECT [^\n]+ FROM"
      r" `([^`]+_staging_[0-9a-f]+)`",
      script,
  )
  assert staging_ref.startswith(_SPAN_REF + "_staging_")
  # The staged rows were loaded before the transaction and dropped after.
  assert any(dest == staging_ref for dest, _, _ in fake.loads)
  assert staging_ref in fake.deleted
  staged_table = next(
      table
      for table in fake.created_tables
      if f"{table.project}.{table.dataset_id}.{table.table_id}" == staging_ref
  )
  assert staged_table.expires is not None


# --- the pin-aware join boundary (retained versions never fan out) ---------


def test_pinned_view_joins_only_the_current_versions_rows() -> None:
  fake = _SpanLabelsFake()
  _materialize(_acceptance_run(), fake)
  changed = [
      _with_spans(
          [_event(_SESSION_STUCK, "AGENT_STARTING", "retry", offset=3)],
          _TRACE_STUCK,
          ["ffff0000ffff0000"],
      )[0]
  ]
  result = _materialize(_acceptance_run(changed), fake, import_version="v2")
  assert result.span_labels_view == _SPAN_VIEW_REF

  # The retained base table keeps both versions' rows, so a bare eval_id
  # join fans out (the documented six-rows-for-widget-stock hazard).
  fanned = [row for row in fake.span_labels if row["eval_id"] == "7e352c34"]
  assert len(fanned) == 6

  # The companion view is pinned to the latest publication: its rendered
  # WHERE carries the (job_id, import_version) literals, so joining
  # failed_sessions to it on eval_id returns exactly the current three.
  body = fake.store.views[_SPAN_VIEW_REF]
  first_line = body.splitlines()[0]
  assert first_line.startswith(native_events._SPAN_VIEW_PIN_MARKER)
  pin = json.loads(first_line[len(native_events._SPAN_VIEW_PIN_MARKER) :])
  assert (pin["job_id"], pin["import_version"]) == (_JOB_ID, "v2")
  assert f'WHERE job_id = "{_JOB_ID}"' in body
  assert 'AND import_version = "v2"' in body
  joined = [
      row
      for row in fake.span_labels
      if row["job_id"] == pin["job_id"]
      and row["import_version"] == pin["import_version"]
      and row["eval_id"] == "7e352c34"
  ]
  assert [row["failure_category"] for row in joined] == list(_G1_CATEGORIES)
  assert {row["span_id"] for row in joined} == {"ffff0000ffff0000"}


def test_foreign_object_at_the_pinned_view_name_is_refused() -> None:
  fake = _SpanLabelsFake(foreign_objects={_SPAN_VIEW_REF: object()})
  with pytest.raises(ValueError, match="not a pinned span-labels view"):
    _materialize(_acceptance_run(), fake)
  # Fail-fast: refused before the snapshot published anything.
  assert fake.store.rows == [] and fake.span_labels == []


def test_pinned_view_of_another_job_is_refused() -> None:
  store = _FakeManifestStore()
  shared: list[dict] = []
  _materialize(
      _acceptance_run(), _SpanLabelsFake(store=store, span_store=shared)
  )
  other = NativeAgentEventsRun.from_agent_events(
      _stuck_events_with_spans() + _gold_events_with_spans(),
      source_table=_SOURCE_TABLE,
      job_id="other-job",
  )
  with pytest.raises(ValueError, match="pinned to job"):
    _materialize(other, _SpanLabelsFake(store=store, span_store=shared))
  assert {row["job_id"] for row in shared} == {_JOB_ID}


def test_derived_pinned_view_name_must_not_shadow_the_failed_view() -> None:
  fake = _SpanLabelsFake()
  with pytest.raises(ValueError, match="would name an import table"):
    _acceptance_run().materialize(
        target_dataset="bqaa_native",
        import_version="v1",
        imported_at=_IMPORTED_AT,
        policy=_POLICY,
        span_labels_table="labels",
        failed_sessions_view="labels_pinned",
        bq_client=fake,
    )
  assert fake.queries == [] and fake.loads == []


def test_to_span_label_rows_is_pure_and_carries_the_pin() -> None:
  rows = _acceptance_run().to_span_label_rows(
      import_version="v1", policy=_POLICY
  )
  assert [
      (row["job_id"], row["import_version"], row["failure_category"])
      for row in rows
  ] == [
      (_JOB_ID, "v1", "task/planning"),
      (_JOB_ID, "v1", "finalization"),
      (_JOB_ID, "v1", "tool blockers"),
  ]
  with pytest.raises(ValueError, match="import_version"):
    _acceptance_run().to_span_label_rows(import_version="bad version!")


# --- the durable binding: policy and rows survive later native calls -------


def _dataset():
  """One shared dataset (manifest + span + binding stores) for two fakes."""
  return _FakeManifestStore(), [], []


def _fake(store, shared, bindings, **kwargs) -> _SpanLabelsFake:
  return _SpanLabelsFake(
      store=store, span_store=shared, binding_store=bindings, **kwargs
  )


def _plain_materialize(run, fake, *, import_version, policy=None, **kwargs):
  """A later ordinary native call: NO span_labels_table argument."""
  return run.materialize(
      target_dataset="bqaa_native",
      import_version=import_version,
      imported_at=_IMPORTED_AT,
      policy=policy,
      bq_client=fake,
      **kwargs,
  )


def _changed_events():
  return _with_spans(
      [_event(_SESSION_STUCK, "AGENT_STARTING", "retry", offset=3)],
      _TRACE_STUCK,
      ["ffff0000ffff0000"],
  )


def _active_view_rows(fake) -> list[dict]:
  """Emulate the pinned view's SQL: pin + the latest-generation guard.

  The rendered guard only admits rows while the pinned generation is
  still the job's latest manifest generation, so any base/span skew
  yields an empty result instead of stale labels.
  """
  body = fake.store.views.get(_SPAN_VIEW_REF)
  if body is None:
    return []
  first_line = body.splitlines()[0]
  assert first_line.startswith(native_events._SPAN_VIEW_PIN_MARKER)
  pin = json.loads(first_line[len(native_events._SPAN_VIEW_PIN_MARKER) :])
  assert f'AND generation_id = "{pin["generation_id"]}"' in body
  assert "ORDER BY imported_at DESC, import_version DESC" in body
  latest = sorted(
      (row for row in fake.store.rows if row["job_id"] == pin["job_id"]),
      key=lambda row: (row["imported_at"], row["import_version"]),
      reverse=True,
  )
  if not latest or latest[0]["generation_id"] != pin["generation_id"]:
    return []
  return [
      row
      for row in fake.span_labels
      if row["job_id"] == pin["job_id"]
      and row["import_version"] == pin["import_version"]
      and row["generation_id"] == pin["generation_id"]
  ]


def test_ordinary_same_pin_call_keeps_the_gate_and_the_span_rows() -> None:
  # P1 #469-r2-1: a span-enabled default publish followed by an ordinary
  # same-pin call (no span table, no --min-score) must NOT rewrite the
  # committed view_policy to NULL while the span rows stay behind.
  store, shared, bindings = _dataset()
  first = _materialize(
      _acceptance_run(), _fake(store, shared, bindings), policy=None
  )
  assert _policy_from_column(first.manifest["view_policy"]) == (
      NATIVE_SPAN_LABEL_POLICY
  )
  generation = first.manifest["generation_id"]

  later = _fake(store, shared, bindings)
  second = _plain_materialize(_acceptance_run(), later, import_version="v1")
  assert second.status == "unchanged"
  # The binding made the ordinary call maintain the span publication.
  assert second.span_labels_table == _SPAN_REF
  assert second.span_label_row_count == 3
  # The committed gate remains the frozen goal-completion policy under
  # the same generation (no NULL rewrite, no silent re-commit)...
  (row,) = store.rows
  assert _policy_from_column(row["view_policy"]) == NATIVE_SPAN_LABEL_POLICY
  assert row["generation_id"] == generation
  # ...the failed-sessions view still renders the gate...
  assert "'goal_completion' AS comparator, 1.0 AS min_score" in (
      store.views[_FAILED_VIEW_REF]
  )
  # ...and the span rows stay coherent behind the active join boundary.
  rows = _active_view_rows(later)
  assert [r["failure_category"] for r in rows] == list(_G1_CATEGORIES)
  assert {r["generation_id"] for r in rows} == {generation}


def test_bound_job_rejects_a_conflicting_gate_on_an_ordinary_call() -> None:
  store, shared, bindings = _dataset()
  _materialize(_acceptance_run(), _fake(store, shared, bindings), policy=None)
  before = [dict(row) for row in store.rows]
  with pytest.raises(ValueError, match="frozen gate"):
    _plain_materialize(
        _acceptance_run(),
        _fake(store, shared, bindings),
        import_version="v1",
        policy=EvalScorePolicy({"goal_completion": 0.5}),
    )
  assert store.rows == before


def test_bound_job_cannot_switch_span_tables() -> None:
  store, shared, bindings = _dataset()
  _materialize(_acceptance_run(), _fake(store, shared, bindings))
  with pytest.raises(ValueError, match="is bound to span_labels_table"):
    _materialize(
        _acceptance_run(),
        _fake(store, shared, bindings),
        span_labels_table="other_span_labels",
    )
  assert {row["span_labels_table"] for row in bindings} == {_SPAN_TABLE}


def test_gate_recommitted_between_derive_and_sync_fails_closed() -> None:
  # P1 #469-r2-1 (overlapping writer): a delayed sync must not adopt a
  # newer manifest generation whose committed view_policy is not the gate
  # its rows were derived under.
  store, shared, bindings = _dataset()
  _materialize(_acceptance_run(), _fake(store, shared, bindings))
  before = [dict(row) for row in shared]

  def concurrent_gate_commit() -> None:
    (row,) = [r for r in store.rows if r["import_version"] == "v1"]
    row["view_policy"] = _policy_column(EvalScorePolicy({"accuracy": 0.9}))
    row["generation_id"] = "f" * 32

  late = _fake(
      store,
      shared,
      bindings,
      doctor_native_manifest_read=concurrent_gate_commit,
  )
  with pytest.raises(ValueError, match="view_policy"):
    _materialize(_acceptance_run(), late)
  assert shared == before


def test_failed_span_sync_after_changed_source_replace_closes_the_view() -> (
    None
):
  # P1 #469-r2-2: a changed-source same-pin replace whose span sync fails
  # leaves the new base generation committed — the active view must then
  # expose NO span rows rather than the previous generation's.
  store, shared, bindings = _dataset()
  first = _materialize(_acceptance_run(), _fake(store, shared, bindings))
  old_generation = first.manifest["generation_id"]
  assert len(_active_view_rows(_fake(store, shared, bindings))) == 3

  broken = _fake(
      store,
      shared,
      bindings,
      span_load_error=RuntimeError("staging load lost"),
  )
  with pytest.raises(ValueError, match="could not be synchro"):
    _materialize(_acceptance_run(_changed_events()), broken, replace=True)
  # The base snapshot advanced to a new generation...
  (row,) = store.rows
  assert row["generation_id"] != old_generation
  # ...the retained span rows still carry the superseded generation...
  assert {r["generation_id"] for r in shared} == {old_generation}
  assert {r["span_id"] for r in shared} == {_AGENT_STARTING_SPAN}
  # ...and the active view exposes nothing (fail closed, not stale rows).
  assert _active_view_rows(broken) == []

  # Re-running heals: rows re-derive under the committed generation and
  # the view reopens on the synchronized snapshot.
  healed = _fake(store, shared, bindings)
  result = _materialize(_acceptance_run(_changed_events()), healed)
  assert result.status == "unchanged"
  rows = _active_view_rows(healed)
  assert {r["span_id"] for r in rows} == {"ffff0000ffff0000"}
  assert {r["generation_id"] for r in rows} == {row["generation_id"]}


def test_labelled_v1_then_unlabelled_v2_moves_the_boundary_together() -> None:
  # P1 #469-r2-3 (a): a later import WITHOUT the span option must not
  # advance failed_sessions while the span view stays pinned to v1.
  store, shared, bindings = _dataset()
  _materialize(_acceptance_run(), _fake(store, shared, bindings))

  later = _fake(store, shared, bindings)
  result = _plain_materialize(
      _acceptance_run(_changed_events()),
      later,
      import_version="v2",
      policy=_POLICY,
  )
  assert result.status == "imported"
  assert result.span_labels_table == _SPAN_REF
  # The active join boundary moved as one: view pinned to v2, rows v2.
  pin = json.loads(
      store.views[_SPAN_VIEW_REF].splitlines()[0][
          len(native_events._SPAN_VIEW_PIN_MARKER) :
      ]
  )
  assert (pin["job_id"], pin["import_version"]) == (_JOB_ID, "v2")
  rows = _active_view_rows(later)
  assert {r["import_version"] for r in rows} == {"v2"}
  assert {r["span_id"] for r in rows} == {"ffff0000ffff0000"}


def test_bound_job_with_unlabelable_corpus_fails_before_publishing() -> None:
  # The maintain-or-fail-closed rule: a bound job whose new corpus has no
  # real span ids must fail BEFORE the base snapshot or the denominator
  # advances past the span rows.
  store, shared, bindings = _dataset()
  _materialize(_acceptance_run(), _fake(store, shared, bindings))
  spanfree = NativeAgentEventsRun.from_agent_events(
      _stuck_events() + _gold_events(),
      source_table=_SOURCE_TABLE,
      job_id=_JOB_ID,
  )
  later = _fake(store, shared, bindings)
  with pytest.raises(ValueError, match="no span_id"):
    _plain_materialize(spanfree, later, import_version="v2", policy=_POLICY)
  assert {row["import_version"] for row in store.rows} == {"v1"}
  assert len(_active_view_rows(later)) == 3


def test_labelled_then_unlabelled_same_pin_replace_stays_coherent() -> None:
  # P1 #469-r2-3 (b): a changed-source same-pin replace WITHOUT the span
  # option must not keep the previous source's span rows visible.
  store, shared, bindings = _dataset()
  _materialize(_acceptance_run(), _fake(store, shared, bindings))

  later = _fake(store, shared, bindings)
  result = _plain_materialize(
      _acceptance_run(_changed_events()),
      later,
      import_version="v1",
      policy=_POLICY,
      replace=True,
  )
  assert result.status == "replaced"
  rows = _active_view_rows(later)
  assert {r["span_id"] for r in rows} == {"ffff0000ffff0000"}
  assert not any(r["span_id"] == _AGENT_STARTING_SPAN for r in shared)


def test_resyncing_an_older_version_cannot_repin_the_view() -> None:
  # P1 #469-r2-3: the view targets the exact generation whose rows were
  # just synchronized — a caller handling an older retained version must
  # not repin the view to a newer generation with no synchronized rows.
  store, shared, bindings = _dataset()
  first = _materialize(_acceptance_run(), _fake(store, shared, bindings))
  v1_generation = first.manifest["generation_id"]

  # v2 commits its base snapshot but its span sync fails: the latest
  # generation has NO synchronized span rows and the view stays on v1.
  broken = _fake(
      store,
      shared,
      bindings,
      span_load_error=RuntimeError("staging load lost"),
  )
  with pytest.raises(ValueError, match="could not be synchro"):
    _materialize(
        _acceptance_run(_changed_events()), broken, import_version="v2"
    )
  assert {r["import_version"] for r in shared} == {"v1"}

  # Re-synchronizing v1 stands down instead of repinning the view to the
  # rowless v2 generation; the guard keeps the stale v1 pin fail-closed.
  older = _fake(store, shared, bindings)
  result = _materialize(_acceptance_run(), older, import_version="v1")
  assert result.status == "unchanged"
  pin = json.loads(
      store.views[_SPAN_VIEW_REF].splitlines()[0][
          len(native_events._SPAN_VIEW_PIN_MARKER) :
      ]
  )
  assert (pin["import_version"], pin["generation_id"]) == (
      "v1",
      v1_generation,
  )
  assert _active_view_rows(older) == []

  # Re-running v2 (the version the boundary is waiting on) heals it.
  healed = _fake(store, shared, bindings)
  _materialize(_acceptance_run(_changed_events()), healed, import_version="v2")
  rows = _active_view_rows(healed)
  assert {r["import_version"] for r in rows} == {"v2"}


# --- r3 P1 regressions: the registry is authoritative under the lock -------


def test_bound_call_without_policy_keeps_the_stored_richer_gate() -> None:
  # P1 #469-r3-1: the binding stores the ONE canonical policy; a later
  # bound call that omits the policy must apply it, not replace it with
  # the fallback-only goal_completion gate and a reset
  # missing_score_fails.
  store, shared, bindings = _dataset()
  rich = EvalScorePolicy({"accuracy": 0.9}, missing_score_fails=False)
  first = _materialize(
      _acceptance_run(), _fake(store, shared, bindings), policy=rich
  )
  merged = native_events.resolve_span_label_policy(rich)
  assert merged.min_scores == {"accuracy": 0.9, "goal_completion": 1.0}
  assert merged.missing_score_fails is False
  assert _policy_from_column(first.manifest["view_policy"]) == merged
  generation = first.manifest["generation_id"]

  later = _fake(store, shared, bindings)
  second = _plain_materialize(_acceptance_run(), later, import_version="v1")
  assert second.status == "unchanged"
  assert second.span_labels_table == _SPAN_REF
  # Manifest, binding, and generation are untouched (no silent re-commit
  # of the fallback-only gate)...
  (row,) = store.rows
  assert _policy_from_column(row["view_policy"]) == merged
  assert row["generation_id"] == generation
  (binding,) = bindings
  assert binding["view_policy"] == row["view_policy"]
  assert binding["generation_id"] == generation
  # ...the failed-session view still renders the merged gate (extra
  # comparator kept, missing scores still passing)...
  view = store.views[_FAILED_VIEW_REF]
  assert "'accuracy' AS comparator, 0.9 AS min_score" in view
  assert "'goal_completion' AS comparator, 1.0 AS min_score" in view
  assert "sc.score IS NULL OR" not in view
  # ...and the span rows stay coherent behind the active join boundary.
  rows = _active_view_rows(later)
  assert [r["failure_category"] for r in rows] == list(_G1_CATEGORIES)
  assert {r["generation_id"] for r in rows} == {generation}


def test_bound_call_with_explicit_policy_is_a_deliberate_change() -> None:
  # An explicitly supplied compatible policy on a bound job is a policy
  # change: manifest, binding, and span rows move to it in lockstep under
  # a fresh generation.
  store, shared, bindings = _dataset()
  _materialize(_acceptance_run(), _fake(store, shared, bindings), policy=None)
  rich = EvalScorePolicy({"accuracy": 0.9}, missing_score_fails=False)
  later = _fake(store, shared, bindings)
  result = _plain_materialize(
      _acceptance_run(), later, import_version="v1", policy=rich
  )
  assert result.status == "unchanged"
  merged = native_events.resolve_span_label_policy(rich)
  (row,) = store.rows
  assert _policy_from_column(row["view_policy"]) == merged
  (binding,) = bindings
  assert binding["view_policy"] == row["view_policy"]
  assert binding["generation_id"] == row["generation_id"]
  rows = _active_view_rows(later)
  assert {r["generation_id"] for r in rows} == {row["generation_id"]}


def test_first_opt_in_requires_the_failed_sessions_view() -> None:
  # P1 #469-r3-2: active span publication with a skipped denominator view
  # would let the pinned join boundaries diverge; refused with nothing
  # written.
  fake = _SpanLabelsFake()
  with pytest.raises(ValueError, match="failed_sessions_view"):
    _materialize(_acceptance_run(), fake, failed_sessions_view=None)
  assert fake.queries == [] and fake.loads == []
  assert fake.created == []
  assert fake.store.rows == [] and fake.span_labels == []


def test_bound_call_cannot_skip_the_failed_sessions_view() -> None:
  # P1 #469-r3-2 (bound): a later call with --skip-failed-sessions-view
  # must not leave the failed-session view pinned to v1 while the span
  # boundary advances to v2 — rejected before any boundary moves.
  store, shared, bindings = _dataset()
  _materialize(_acceptance_run(), _fake(store, shared, bindings))
  before_rows = [dict(row) for row in store.rows]
  before_spans = [dict(row) for row in shared]

  later = _fake(store, shared, bindings)
  with pytest.raises(ValueError, match="failed_sessions_view"):
    _plain_materialize(
        _acceptance_run(_changed_events()),
        later,
        import_version="v2",
        policy=_POLICY,
        failed_sessions_view=None,
    )
  assert store.rows == before_rows
  assert shared == before_spans
  assert later.loads == [] and later.created == []


def test_racing_first_opt_ins_bind_only_one_span_table() -> None:
  # P1 #469-r3-3: two first opt-ins can both pre-read "no binding" and
  # pick different tables; the loser must fail closed inside the span
  # sync transaction instead of silently re-binding the job.
  store, shared, bindings = _dataset()

  def winner_lands() -> None:
    _materialize(_acceptance_run(), _fake(store, shared, bindings))

  loser = _fake(
      store, shared, bindings, doctor_native_manifest_read=winner_lands
  )
  with pytest.raises(ValueError, match="bound to a different"):
    _materialize(
        _acceptance_run(), loser, span_labels_table="other_span_labels"
    )
  # One binding wins, and no second live pinned view exists for the job.
  (binding,) = bindings
  assert binding["span_labels_table"] == _SPAN_TABLE
  other_view = f"{_SOURCE_PROJECT}.bqaa_native.other_span_labels_pinned"
  assert other_view not in store.views
  assert _SPAN_VIEW_REF in store.views
  assert {row["import_version"] for row in shared} == {"v1"}
  assert len(shared) == 3


def test_stale_no_binding_pre_read_cannot_publish_past_the_registry() -> None:
  # P1 #469-r3-4 (publish boundary): a delayed writer that pre-read "no
  # binding" must not commit a new base snapshot after the binding landed
  # — the publish transaction re-validates the registry under the lock.
  store, shared, bindings = _dataset()
  _materialize(_acceptance_run(), _fake(store, shared, bindings))
  before_rows = [dict(row) for row in store.rows]
  before_bindings = [dict(row) for row in bindings]

  stale = _fake(store, shared, bindings, stale_binding_reads=True)
  with pytest.raises(ValueError, match="span-binding registry"):
    _plain_materialize(
        _acceptance_run(_changed_events()),
        stale,
        import_version="v2",
        policy=_POLICY,
    )
  assert store.rows == before_rows
  assert bindings == before_bindings
  assert {row["import_version"] for row in shared} == {"v1"}
  # Nothing survives but the dropped staging loads.
  assert all("_staging_" in ref for ref in stale.deleted)


def test_recommit_after_binding_lands_cannot_null_the_gate() -> None:
  # P1 #469-r3-4 (policy boundary): the unchanged-path policy recommit of
  # a writer whose registry pre-read went stale lands on nothing instead
  # of rewriting the committed gate to NULL.
  store, shared, bindings = _dataset()
  bound = _materialize(
      _acceptance_run(), _fake(store, shared, bindings), policy=None
  )
  generation = bound.manifest["generation_id"]

  stale = _fake(store, shared, bindings, stale_binding_reads=True)
  result = _plain_materialize(
      _acceptance_run(), stale, import_version="v1", policy=None
  )
  assert result.status == "unchanged"
  (row,) = store.rows
  assert _policy_from_column(row["view_policy"]) == NATIVE_SPAN_LABEL_POLICY
  assert row["generation_id"] == generation
  # The denominator view still renders the gate and the span boundary is
  # still open on the synchronized generation.
  assert "'goal_completion' AS comparator, 1.0 AS min_score" in (
      store.views[_FAILED_VIEW_REF]
  )
  rows = _active_view_rows(stale)
  assert {r["generation_id"] for r in rows} == {generation}


def test_bound_pre_read_must_match_the_committed_binding() -> None:
  # P1 #469-r3-4 (consistency): a delayed writer that pre-read a binding
  # must see the same table AND policy at commit time; a concurrent
  # deliberate policy change fails it closed.
  store, shared, bindings = _dataset()
  _materialize(_acceptance_run(), _fake(store, shared, bindings), policy=None)

  def concurrent_policy_change() -> None:
    _materialize(
        _acceptance_run(),
        _fake(store, shared, bindings),
        policy=EvalScorePolicy({"accuracy": 0.9}, missing_score_fails=False),
    )

  late = _fake(
      store, shared, bindings, doctor_binding_read=concurrent_policy_change
  )
  with pytest.raises(ValueError, match="span-binding registry"):
    _plain_materialize(
        _acceptance_run(_changed_events()), late, import_version="v2"
    )
  # The deliberate change won; nothing of the stale v2 publish landed.
  (binding,) = bindings
  assert _policy_from_column(binding["view_policy"]).min_scores == {
      "accuracy": 0.9,
      "goal_completion": 1.0,
  }
  assert {row["import_version"] for row in store.rows} == {"v1"}


# --- r4 P0: the registry guard is structured state, never caller SQL --------


def test_materialize_rejects_caller_shaped_sql_binding_arguments() -> None:
  # P0 #469-r4-1: the raw-SQL ``binding_guard`` hook is gone, and its
  # structured replacement refuses anything that is not the private state
  # type — a statement-boundary payload can never reach ``client.query``.
  fake = _FakeWriteClient()
  payload = "TRUE); DROP TABLE `prod.telemetry.agent_events`; --"
  with pytest.raises(TypeError, match="binding_guard"):
    _scored_run().materialize(
        target_dataset="bqaa",
        import_version="v1",
        bq_client=fake,
        binding_guard=payload,
    )
  with pytest.raises(TypeError, match="_SpanBindingState"):
    _scored_run().materialize(
        target_dataset="bqaa",
        import_version="v1",
        bq_client=fake,
        span_binding=payload,
    )
  assert fake.queries == [] and fake.loads == [] and fake.created == []


def test_span_binding_state_validates_every_rendered_identifier() -> None:
  # The ONLY identifier the fixed predicates interpolate is the registry
  # reference, and every segment is validated before rendering.
  for evil in (
      "p.d.t`; DROP TABLE `prod.telemetry.agent_events`; --",
      "p.d.evalbench`bindings",
      "p.dataset",
      "p.d.agent_events",
  ):
    with pytest.raises(ValueError):
      _SpanBindingState(bindings_ref=evil)
  with pytest.raises(ValueError, match="expected_table"):
    _SpanBindingState(
        bindings_ref=_BINDINGS_REF,
        expected_table="t`; DROP TABLE x; --",
        expected_policy="{}",
    )
  with pytest.raises(ValueError, match="together"):
    _SpanBindingState(bindings_ref=_BINDINGS_REF, expected_table="t")


def test_binding_guard_renders_fixed_predicates_with_parameters() -> None:
  # End to end: the bound-shape guard the base publish carries is exactly
  # the fixed template, and the expected binding — whose canonical policy
  # JSON legitimately contains quote characters — travels only as query
  # parameters, never in the SQL text.
  store, shared, bindings = _dataset()
  _materialize(_acceptance_run(), _fake(store, shared, bindings), policy=None)
  (binding,) = bindings
  assert '"' in binding["view_policy"]  # would break out of any literal

  later = _fake(store, shared, bindings)
  _plain_materialize(
      _acceptance_run(_changed_events()), later, import_version="v2"
  )
  (script, kwargs) = next(
      (sql, kw)
      for sql, kw in later.queries
      if _PUBLISH_BINDING_GUARD_MESSAGE in sql
  )
  guard = re.search(r"IF (NOT EXISTS .+?) THEN", script, re.S).group(1)
  assert guard == (
      f"NOT EXISTS (SELECT 1 FROM `{_BINDINGS_REF}`"
      " WHERE job_id = @job_id"
      " AND span_labels_table = @expected_span_labels_table"
      " AND view_policy = @expected_span_view_policy)"
  )
  assert binding["view_policy"] not in script
  params = {p.name: p.value for p in kwargs["job_config"].query_parameters}
  assert params["expected_span_labels_table"] == _SPAN_TABLE
  assert params["expected_span_view_policy"] == binding["view_policy"]


# --- r4 P1: the policy recommit is lock-serialized --------------------------


def test_policy_recommit_serializes_with_the_span_binding_transaction() -> None:
  # P1 #469-r4-2: the recommit's snapshot and the span-binding
  # transaction's snapshot are BOTH taken before either commits. A
  # standalone manifest UPDATE mutates a table disjoint from the
  # span/binding tables, so snapshot isolation would let both commit —
  # manifest gate NULL under a new generation, binding gate frozen under
  # the old one, committed span rows, no live pinned view. Claiming the
  # import lock makes the two transactions conflict on the sentinel row,
  # so the recommit whose snapshot went stale is cancelled instead.
  store, shared, bindings = _dataset()
  overlap: dict = {}

  def capture_overlap() -> None:
    # Runs after the opt-in's base publish committed and before its span
    # sync claims the lock: the window the stale recommit overlaps.
    overlap["snapshot"] = store.snapshot()
    overlap["bindings"] = [dict(row) for row in bindings]

  first = _materialize(
      _acceptance_run(),
      _fake(
          store, shared, bindings, doctor_native_manifest_read=capture_overlap
      ),
      policy=None,
  )
  generation = first.manifest["generation_id"]
  assert overlap["bindings"] == []  # captured before the binding committed

  stale = _fake(
      store,
      shared,
      bindings,
      stale_binding_reads=True,
      transaction_snapshot=overlap["snapshot"],
      binding_transaction_snapshot=overlap["bindings"],
  )
  with pytest.raises(ValueError, match="could not be created or updated"):
    _plain_materialize(_acceptance_run(), stale, import_version="v1")
  # Neither half of the anomaly committed: the manifest still records the
  # frozen gate under the synchronized generation...
  (row,) = store.rows
  assert _policy_from_column(row["view_policy"]) == NATIVE_SPAN_LABEL_POLICY
  assert row["generation_id"] == generation
  # ...the binding agrees with it...
  (binding,) = bindings
  assert binding["generation_id"] == generation
  assert binding["view_policy"] == row["view_policy"]
  # ...and the pinned span view is live on that generation.
  rows = _active_view_rows(stale)
  assert [r["failure_category"] for r in rows] == list(_G1_CATEGORIES)
  assert {r["generation_id"] for r in rows} == {generation}

  # Re-running from committed state heals: the binding restores the ONE
  # policy, nothing needs recommitting, and the boundary stays coherent.
  healed = _fake(store, shared, bindings)
  result = _plain_materialize(_acceptance_run(), healed, import_version="v1")
  assert result.status == "unchanged"
  (row,) = store.rows
  assert row["generation_id"] == generation


# --- r4 P1: one job per span table, enforced before any DML -----------------


def test_concurrent_jobs_cannot_bind_one_span_table() -> None:
  # P1 #469-r4-3: two jobs with distinct failed-sessions views race into
  # the SAME span table. The table-derived pinned view admits one job
  # owner, so if both committed rows and bindings the loser would fail
  # view reconciliation forever — its durable binding blocking a retry of
  # either table. The in-transaction ownership check rejects the loser
  # BEFORE its rows or binding commit.
  store, shared, bindings = _dataset()

  def winner_lands() -> None:
    _materialize(_acceptance_run(), _fake(store, shared, bindings))

  run_b = NativeAgentEventsRun.from_agent_events(
      _stuck_events_with_spans() + _gold_events_with_spans(),
      source_table=_SOURCE_TABLE,
      job_id="job-b",
  )
  loser = _fake(
      store, shared, bindings, doctor_native_manifest_read=winner_lands
  )
  with pytest.raises(ValueError, match="already bound to another job"):
    _materialize(run_b, loser, failed_sessions_view="failed_b")
  # The loser stays unbound, its rows never landed, and the winner's
  # binding and single live pinned view are untouched.
  (binding,) = bindings
  assert binding["job_id"] == _JOB_ID
  assert {row["job_id"] for row in shared} == {_JOB_ID}
  pin = json.loads(
      store.views[_SPAN_VIEW_REF].splitlines()[0][
          len(native_events._SPAN_VIEW_PIN_MARKER) :
      ]
  )
  assert pin["job_id"] == _JOB_ID

  # Unbound means recoverable: the loser retries with its own span table
  # and binds it, leaving the winner's publication untouched.
  retry = _fake(store, shared, bindings)
  result = _materialize(
      run_b,
      retry,
      span_labels_table="span_labels_b",
      failed_sessions_view="failed_b",
  )
  assert result.span_labels_table == (
      f"{_SOURCE_PROJECT}.bqaa_native.span_labels_b"
  )
  assert result.span_label_row_count == 3
  assert {(b["job_id"], b["span_labels_table"]) for b in bindings} == {
      (_JOB_ID, _SPAN_TABLE),
      ("job-b", "span_labels_b"),
  }
  assert {row["job_id"] for row in shared} == {_JOB_ID, "job-b"}


# --- destination guards ---------------------------------------------------


def test_reserved_agent_events_span_table_is_rejected() -> None:
  fake = _SpanLabelsFake()
  with pytest.raises(ValueError, match="reserved ADK plugin table"):
    _materialize(_acceptance_run(), fake, span_labels_table="agent_events")
  assert fake.queries == [] and fake.loads == []


def test_span_table_must_not_shadow_an_import_table_or_the_view() -> None:
  fake = _SpanLabelsFake()
  for clash in (
      "evalbench_agent_events",
      "evalbench_scores_imported",
      "evalbench_import_manifest",
      "evalbench_import_lock",
      "evalbench_span_bindings",
      "evalbench_failed_sessions",
  ):
    with pytest.raises(ValueError, match="must not name an import table"):
      _materialize(_acceptance_run(), fake, span_labels_table=clash)
  assert fake.queries == [] and fake.loads == []


def test_span_sync_never_touches_source_or_production_agent_events() -> None:
  fake = _SpanLabelsFake()
  _materialize(_acceptance_run(), fake)
  written = (
      [ref for ref, _, _ in fake.loads]
      + fake.created
      + [query for query, _ in fake.queries if not query.startswith("SELECT")]
  )
  for target in written:
    assert _SOURCE_TABLE not in target
    for segment in target.replace("`", " ").replace("\n", " ").split():
      assert not segment.endswith(".agent_events")


# --- the six-week clock does not start ------------------------------------


def test_span_label_publication_does_not_start_the_clock() -> None:
  fake = _SpanLabelsFake()
  result = _materialize(_acceptance_run(), fake)
  payload = json.dumps(result.to_dict()).lower()
  assert "clock" not in payload
