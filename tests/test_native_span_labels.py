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
from bigquery_agent_analytics.evalbench import _policy_from_column
from bigquery_agent_analytics.evalbench import EvalScorePolicy
from bigquery_agent_analytics.evalbench import failed_sessions
from bigquery_agent_analytics.native_events import NATIVE_SPAN_LABEL_POLICY
from bigquery_agent_analytics.native_events import NativeAgentEventsRun
from bigquery_agent_analytics.span_taxonomy import label_native_run
from tests.test_evalbench_importer import _FakeJob
from tests.test_evalbench_importer import _FakeManifestStore
from tests.test_evalbench_importer import _FakeSnapshot
from tests.test_evalbench_importer import _FakeWriteClient
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
_FAILED_VIEW_REF = f"{_SOURCE_PROJECT}.bqaa_native.evalbench_failed_sessions"
_PIN = (_JOB_ID, "v1")
_G1_CATEGORIES = ("task/planning", "finalization", "tool blockers")


class _SpanLabelsFake(_FakeWriteClient):
  """The importer fake plus a span-labels table honoring the staged sync.

  The span-label publication stages rows into an expiring staging table
  and then runs one lock-claiming, generation-checked transaction that
  replaces the pin's slice (``_SPAN_SYNC_SCRIPT``). The fake emulates that
  script with the same snapshot-isolation rules ``_FakeWriteClient`` uses
  for the publish transaction — pinned ``transaction_snapshot`` and the
  shared store included, so concurrent syncs can be modeled — and defers
  everything else (staging loads, view writes, the inherited publish) to
  the base fake. ``span_store`` may be shared between fakes to model one
  dataset touched by two importers.
  """

  def __init__(self, *, span_store: list[dict] | None = None, **kwargs) -> None:
    super().__init__(**kwargs)
    self.span_labels: list[dict] = span_store if span_store is not None else []
    self.span_deletes: list[tuple[str, dict]] = []

  def query(self, query: str, **kwargs) -> _FakeJob:
    if native_events._SPAN_STALE_PIN_MESSAGE in query:
      self.queries.append((query, kwargs))
      params = {p.name: p.value for p in kwargs["job_config"].query_parameters}
      snapshot = self.transaction_snapshot or self.store.snapshot()
      return self._span_sync(query, params, snapshot)
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
    current = [
        row
        for row in snapshot.rows
        if (row["job_id"], row["import_version"])
        == (pin["job_id"], pin["import_version"])
        and row.get("generation_id") == params["expected_generation_id"]
    ]
    if not current:
      return _FakeJob(
          error=RuntimeError(f"400 {native_events._SPAN_STALE_PIN_MESSAGE}")
      )
    ref = re.search(r"DELETE FROM `([^`]+)`", script).group(1)
    (staging_ref,) = re.findall(r"FROM `([^`]+_staging_[0-9a-f]+)`", script)
    staged = next(
        (rows for dest, rows, _ in reversed(self.loads) if dest == staging_ref),
        [],
    )
    self.store.lock_claims += 1
    self.span_deletes.append((ref, pin))
    self.span_labels[:] = [
        row
        for row in self.span_labels
        if (row["job_id"], row["import_version"])
        != (pin["job_id"], pin["import_version"])
    ] + [dict(row) for row in staged]
    return _FakeJob()


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
  with pytest.raises(ValueError, match="re-published concurrently"):
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
