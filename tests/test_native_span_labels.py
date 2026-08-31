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
from bigquery_agent_analytics import span_taxonomy
from bigquery_agent_analytics.evalbench import EvalScorePolicy
from bigquery_agent_analytics.evalbench import failed_sessions
from bigquery_agent_analytics.native_events import NATIVE_SPAN_LABEL_POLICY
from bigquery_agent_analytics.native_events import NativeAgentEventsRun
from bigquery_agent_analytics.span_taxonomy import label_native_run
from tests.test_evalbench_importer import _FakeJob
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
_PIN = (_JOB_ID, "v1")


class _SpanLabelsFake(_FakeWriteClient):
  """The importer fake plus a span-labels table honoring the keyed sync.

  The span-label publication is a keyed DELETE followed by a plain load
  into the final table (no staging, no transaction), so the fake applies
  exactly those two operations to its ``span_labels`` store and defers
  everything else to ``_FakeWriteClient``.
  """

  def __init__(self, **kwargs) -> None:
    super().__init__(**kwargs)
    self.span_labels: list[dict] = []
    self.span_deletes: list[tuple[str, dict]] = []

  def query(self, query: str, **kwargs) -> _FakeJob:
    if query.startswith("DELETE FROM `"):
      self.queries.append((query, kwargs))
      params = {p.name: p.value for p in kwargs["job_config"].query_parameters}
      ref = re.match(r"DELETE FROM `([^`]+)`", query).group(1)
      self.span_deletes.append((ref, params))
      self.span_labels = [
          row
          for row in self.span_labels
          if (row["job_id"], row["import_version"])
          != (params["job_id"], params["import_version"])
      ]
      return _FakeJob()
    return super().query(query, **kwargs)

  def load_table_from_json(self, rows, destination, job_config=None):
    job = super().load_table_from_json(rows, destination, job_config)
    if "_staging_" not in destination:
      self.span_labels.extend(dict(row) for row in rows)
    return job


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
