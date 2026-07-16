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

"""Issue #359 U2: identity-safe trace SQL, candidate resolution, and
selector-grouped trace construction.

Collision characterization: a reused ``session_id`` must never merge
rows across users, root agents, or evaluation passes, and singular
reads must fail closed with retry-ready ambiguity instead of picking
a winner.
"""

from datetime import datetime
from datetime import timezone
from unittest.mock import MagicMock

import pytest

from bigquery_agent_analytics.client import _build_traces_from_rows
from bigquery_agent_analytics.client import _candidates_matching_selector
from bigquery_agent_analytics.client import _GET_SESSION_TRACE_QUERY
from bigquery_agent_analytics.client import _LIST_TRACES_QUERY
from bigquery_agent_analytics.client import _minimal_payloads
from bigquery_agent_analytics.client import _parse_tag_payload
from bigquery_agent_analytics.client import _resolve_scope_candidates
from bigquery_agent_analytics.client import Client
from bigquery_agent_analytics.trace import AmbiguousSessionError
from bigquery_agent_analytics.trace import TraceFilter
from bigquery_agent_analytics.trace import TraceIdentity
from bigquery_agent_analytics.trace import TraceScope
from bigquery_agent_analytics.trace import TraceSelector

_TS = datetime(2026, 3, 12, 10, 0, 0, tzinfo=timezone.utc)


def _event_row(
    session_id="sess-1",
    user_id="alice",
    root_agent_name=None,
    experiment_id=None,
    custom_tags=None,
    span_id="sp-1",
    trace_id="trace-1",
):
  import json as json_mod

  attributes = {}
  if root_agent_name is not None:
    attributes["root_agent_name"] = root_agent_name
  if experiment_id is not None:
    attributes["experiment_id"] = experiment_id
  if custom_tags is not None:
    attributes["custom_tags"] = custom_tags
  return {
      "event_type": "LLM_RESPONSE",
      "agent": "agent",
      "timestamp": _TS,
      "session_id": session_id,
      "invocation_id": "inv-1",
      "user_id": user_id,
      "trace_id": trace_id,
      "span_id": span_id,
      "parent_span_id": None,
      "content": "{}",
      "content_parts": [],
      "attributes": json_mod.dumps(attributes),
      "latency_ms": None,
      "status": "OK",
      "error_message": None,
      "is_truncated": False,
  }


def _mock_row(data):
  mock = MagicMock()
  mock.__iter__ = MagicMock(return_value=iter(data.items()))
  mock.get = data.get
  mock.keys = data.keys
  mock.values = data.values
  mock.items = data.items
  mock.__getitem__ = lambda self, k: data[k]
  return mock


def _candidate_row(
    session_id="sess-1",
    user_id="alice",
    root_agent_name=None,
    experiment_id=None,
    tag_payload=None,
    row_count=1,
):
  return {
      "session_id": session_id,
      "user_id": user_id,
      "root_agent_name": root_agent_name,
      "experiment_id": experiment_id,
      "tag_payload": tag_payload,
      "row_count": row_count,
  }


class TestListTracesQueryShape:
  """The list query anchors rows to the complete identity."""

  def test_composite_null_safe_anchor_join(self):
    assert "GROUP BY session_id, user_id, root_agent_name" in (
        _LIST_TRACES_QUERY
    )
    assert "e.user_id IS NOT DISTINCT FROM ts.user_id" in _LIST_TRACES_QUERY
    assert "JSON_VALUE(e.attributes, '$.root_agent_name')" in _LIST_TRACES_QUERY
    assert "IS NOT DISTINCT FROM ts.root_agent_name" in _LIST_TRACES_QUERY
    assert "{row_where}" in _LIST_TRACES_QUERY

  def test_list_traces_renders_row_scope(self):
    mock_bq = MagicMock()
    mock_job = MagicMock()
    mock_job.result.return_value = []
    mock_bq.query.return_value = mock_job
    client = Client(
        project_id="proj",
        dataset_id="ds",
        verify_schema=False,
        bq_client=mock_bq,
    )
    client.list_traces(
        TraceFilter(custom_labels={"run": "v1"}, experiment_id="exp-1")
    )
    query = mock_bq.query.call_args[0][0]
    # Session selection keeps the label predicate...
    assert "CONCAT('$.custom_tags.', @label_key_0)" in query
    # ...and the outer row fetch re-applies it, conflict-excluding.
    assert (
        "(JSON_VALUE(e.attributes, CONCAT('$.custom_tags.', @label_key_0))"
        " = @label_val_0 OR JSON_VALUE(e.attributes,"
        " CONCAT('$.custom_tags.', @label_key_0)) IS NULL)" in query
    )
    assert (
        "(JSON_VALUE(e.attributes, '$.experiment_id') = @experiment_id"
        " OR JSON_VALUE(e.attributes, '$.experiment_id') IS NULL)" in query
    )

  def test_singular_fetch_query_anchors_identity(self):
    assert (
        "e.user_id IS NOT DISTINCT FROM @anchor_user_id"
        in _GET_SESSION_TRACE_QUERY
    )
    assert (
        "IS NOT DISTINCT FROM @anchor_root_agent_name"
        in _GET_SESSION_TRACE_QUERY
    )
    assert "{row_where}" in _GET_SESSION_TRACE_QUERY


class TestTagPayloadParsing:

  def test_parse_forms(self):
    assert _parse_tag_payload(None) is None
    assert _parse_tag_payload("null") is None
    assert _parse_tag_payload("{}") is None
    assert _parse_tag_payload('{"run": "v1"}') == {"run": "v1"}
    assert _parse_tag_payload({"run": "v1"}) == {"run": "v1"}
    # Non-string scalars canonicalize deterministically.
    assert _parse_tag_payload('{"n": 3}') == {"n": "3"}

  def test_minimal_payloads_superset_collapse(self):
    # Live-data evidence: additive enrichment keys collapse into the
    # base payload; conflicting pass labels stay distinct.
    base = {"assistant": "cc"}
    enriched = {"assistant": "cc", "subagent_id": "x"}
    assert _minimal_payloads([base, enriched, enriched]) == [base]
    v0 = {"run": "v0"}
    v1 = {"run": "v1"}
    result = _minimal_payloads([v0, v1])
    assert v0 in result and v1 in result


class TestCandidateResolution:
  """AE1/AE2/AE3 candidate characterization."""

  def test_cross_user_collision_two_candidates(self):
    rows = [
        _candidate_row(user_id="alice"),
        _candidate_row(user_id="bob"),
    ]
    candidates = _resolve_scope_candidates(rows)
    users = {c.identity.user_id for c in candidates}
    assert users == {"alice", "bob"}

  def test_null_identity_candidate_retained(self):
    rows = [
        _candidate_row(user_id=None, root_agent_name=None),
        _candidate_row(user_id="alice", root_agent_name="root"),
    ]
    candidates = _resolve_scope_candidates(rows)
    assert len(candidates) == 2
    assert any(
        c.identity.user_id is None and c.identity.root_agent_name is None
        for c in candidates
    )

  def test_v0_v1_passes_two_scope_candidates(self):
    rows = [
        _candidate_row(tag_payload='{"run": "v0"}'),
        _candidate_row(tag_payload='{"run": "v1"}'),
        _candidate_row(tag_payload=None),  # shared untagged rows
    ]
    candidates = _resolve_scope_candidates(rows)
    signatures = {c.scope_signature for c in candidates}
    assert len(signatures) == 2
    labels = {tuple(sorted(c.scope.labels_dict.items())) for c in candidates}
    assert (("run", "v0"),) in labels
    assert (("run", "v1"),) in labels

  def test_enrichment_payloads_single_candidate(self):
    rows = [
        _candidate_row(tag_payload='{"assistant": "cc"}'),
        _candidate_row(tag_payload='{"assistant": "cc", "subagent_id": "a1"}'),
        _candidate_row(tag_payload='{"assistant": "cc", "subagent_id": "a2"}'),
        _candidate_row(tag_payload=None),
    ]
    candidates = _resolve_scope_candidates(rows)
    assert len(candidates) == 1
    assert candidates[0].scope.labels_dict == {"assistant": "cc"}

  def test_untagged_only_session_single_empty_scope(self):
    candidates = _resolve_scope_candidates([_candidate_row()])
    assert len(candidates) == 1
    assert candidates[0].scope == TraceScope()

  def test_experiment_split(self):
    rows = [
        _candidate_row(experiment_id="e1"),
        _candidate_row(experiment_id="e2"),
    ]
    candidates = _resolve_scope_candidates(rows)
    assert {c.scope.experiment_id for c in candidates} == {"e1", "e2"}


class TestSelectorMatching:

  def _candidates(self):
    return _resolve_scope_candidates(
        [
            _candidate_row(user_id="alice", tag_payload='{"run": "v0"}'),
            _candidate_row(user_id="alice", tag_payload='{"run": "v1"}'),
            _candidate_row(user_id=None, tag_payload=None),
        ]
    )

  def test_user_pin_narrows(self):
    matching = _candidates_matching_selector(
        self._candidates(),
        TraceSelector(session_id="sess-1", user_id="alice"),
    )
    assert len(matching) == 2
    assert all(c.identity.user_id == "alice" for c in matching)

  def test_null_pin_selects_null_candidate_only(self):
    matching = _candidates_matching_selector(
        self._candidates(),
        TraceSelector(session_id="sess-1", user_id=None),
    )
    assert len(matching) == 1
    assert matching[0].identity.user_id is None

  def test_label_subset_pin_selects_pass(self):
    matching = _candidates_matching_selector(
        self._candidates(),
        TraceSelector(session_id="sess-1", custom_labels={"run": "v0"}),
    )
    assert len(matching) == 1
    assert matching[0].scope.labels_dict == {"run": "v0"}

  def test_scope_signature_pin_exact(self):
    candidates = self._candidates()
    target = next(c for c in candidates if c.scope.labels_dict == {"run": "v1"})
    matching = _candidates_matching_selector(
        candidates,
        TraceSelector(
            session_id="sess-1", scope_signature=target.scope_signature
        ),
    )
    assert matching == [target]


class TestSelectorGroupedConstruction:
  """_build_traces_from_rows groups by resolved selector."""

  def test_cross_user_rows_two_traces(self):
    rows = [
        _mock_row(_event_row(user_id="alice", span_id="a1")),
        _mock_row(_event_row(user_id="bob", span_id="b1")),
    ]
    traces = _build_traces_from_rows(rows)
    assert len(traces) == 2
    by_user = {t.identity.user_id: t for t in traces}
    assert set(by_user) == {"alice", "bob"}
    assert all(len(t.spans) == 1 for t in traces)
    # Legacy mirrors stay in sync with the attached identity.
    assert by_user["alice"].user_id == "alice"

  def test_cross_root_agent_rows_two_traces(self):
    rows = [
        _mock_row(_event_row(root_agent_name="root-a", span_id="a1")),
        _mock_row(_event_row(root_agent_name="root-b", span_id="b1")),
    ]
    traces = _build_traces_from_rows(rows)
    assert {t.identity.root_agent_name for t in traces} == {
        "root-a",
        "root-b",
    }

  def test_v0_v1_passes_split_with_shared_rows(self):
    rows = [
        _mock_row(_event_row(custom_tags={"run": "v0"}, span_id="p0")),
        _mock_row(_event_row(custom_tags={"run": "v1"}, span_id="p1")),
        _mock_row(_event_row(span_id="shared")),
    ]
    traces = _build_traces_from_rows(rows)
    assert len(traces) == 2
    by_run = {t.scope.labels_dict["run"]: t for t in traces}
    v0_spans = {s.span_id for s in by_run["v0"].spans}
    v1_spans = {s.span_id for s in by_run["v1"].spans}
    # Each pass keeps its own rows plus the shared conversation row.
    assert v0_spans == {"p0", "shared"}
    assert v1_spans == {"p1", "shared"}

  def test_enrichment_rows_stay_one_trace(self):
    rows = [
        _mock_row(_event_row(custom_tags={"assistant": "cc"}, span_id="s1")),
        _mock_row(
            _event_row(
                custom_tags={"assistant": "cc", "subagent_id": "x"},
                span_id="s2",
            )
        ),
    ]
    traces = _build_traces_from_rows(rows)
    assert len(traces) == 1
    assert traces[0].scope.labels_dict == {"assistant": "cc"}
    assert {s.span_id for s in traces[0].spans} == {"s1", "s2"}

  def test_legacy_rows_get_identity_and_empty_scope(self):
    traces = _build_traces_from_rows([_mock_row(_event_row())])
    assert len(traces) == 1
    trace = traces[0]
    assert trace.identity == TraceIdentity(session_id="sess-1", user_id="alice")
    assert trace.scope == TraceScope()


class TestSingularReadResolution:
  """get_session_trace resolves candidates and fails closed."""

  def _client(self, result_batches):
    mock_bq = MagicMock()
    jobs = []
    for batch in result_batches:
      job = MagicMock()
      job.result.return_value = batch
      jobs.append(job)
    mock_bq.query.side_effect = jobs
    client = Client(
        project_id="proj",
        dataset_id="ds",
        verify_schema=False,
        bq_client=mock_bq,
    )
    return client, mock_bq

  def test_bare_read_on_collision_raises_retryable_ambiguity(self):
    candidate_batch = [
        _mock_row(_candidate_row(user_id="alice")),
        _mock_row(_candidate_row(user_id="bob")),
    ]
    client, _ = self._client([candidate_batch])
    with pytest.raises(AmbiguousSessionError) as exc_info:
      client.get_session_trace("sess-1")
    payload = exc_info.value.to_dict()
    assert payload["candidate_count"] == 2
    assert "user_id" in payload["retry_dimensions"]
    # The payload selector is retry-ready in one step.
    selector = TraceSelector(**payload["candidates"][0]["selector"])
    assert selector.session_id == "sess-1"

  def test_pinned_read_fetches_anchored_rows(self):
    candidate_batch = [
        _mock_row(_candidate_row(user_id="alice")),
        _mock_row(_candidate_row(user_id="bob")),
    ]
    fetch_batch = [_mock_row(_event_row(user_id="alice"))]
    client, mock_bq = self._client([candidate_batch, fetch_batch])
    trace = client.get_session_trace("sess-1", user_id="alice")
    assert trace.identity.user_id == "alice"
    assert trace.session_id == "sess-1"
    fetch_query = mock_bq.query.call_args[0][0]
    assert "IS NOT DISTINCT FROM @anchor_user_id" in fetch_query
    fetch_params = {
        p.name: p.value
        for p in mock_bq.query.call_args[1]["job_config"].query_parameters
    }
    assert fetch_params["anchor_user_id"] == "alice"
    assert fetch_params["anchor_root_agent_name"] is None

  def test_null_identity_anchor_binds_null_parameters(self):
    candidate_batch = [_mock_row(_candidate_row(user_id=None))]
    fetch_batch = [_mock_row(_event_row(user_id=None))]
    client, mock_bq = self._client([candidate_batch, fetch_batch])
    trace = client.get_session_trace("sess-1")
    assert trace.identity.user_id is None
    fetch_params = {
        p.name: p.value
        for p in mock_bq.query.call_args[1]["job_config"].query_parameters
    }
    assert fetch_params["anchor_user_id"] is None

  def test_pass_selector_applies_row_scope(self):
    candidate_batch = [
        _mock_row(_candidate_row(tag_payload='{"run": "v0"}')),
        _mock_row(_candidate_row(tag_payload='{"run": "v1"}')),
    ]
    fetch_batch = [
        _mock_row(_event_row(custom_tags={"run": "v0"})),
    ]
    client, mock_bq = self._client([candidate_batch, fetch_batch])
    trace = client.get_session_trace("sess-1", custom_labels={"run": "v0"})
    assert trace.scope.labels_dict == {"run": "v0"}
    fetch_query = mock_bq.query.call_args[0][0]
    assert "CONCAT('$.custom_tags.', @label_key_0)" in fetch_query
    fetch_params = {
        p.name: p.value
        for p in mock_bq.query.call_args[1]["job_config"].query_parameters
    }
    assert fetch_params["label_key_0"] == '"run"'
    assert fetch_params["label_val_0"] == "v0"

  def test_duplicate_candidate_rows_stay_unambiguous(self):
    candidate_batch = [
        _mock_row(_candidate_row(user_id="alice", row_count=3)),
        _mock_row(_candidate_row(user_id="alice", row_count=1)),
    ]
    fetch_batch = [_mock_row(_event_row(user_id="alice"))]
    client, _ = self._client([candidate_batch, fetch_batch])
    trace = client.get_session_trace("sess-1")
    assert trace.identity.user_id == "alice"

  def test_empty_session_raises_value_error(self):
    client, _ = self._client([[]])
    with pytest.raises(ValueError, match="No events found"):
      client.get_session_trace("missing")

  def test_retry_payload_round_trip_selects_candidate(self):
    candidate_batch = [
        _mock_row(_candidate_row(user_id="alice")),
        _mock_row(_candidate_row(user_id="bob")),
    ]
    client, _ = self._client([candidate_batch])
    with pytest.raises(AmbiguousSessionError) as exc_info:
      client.get_session_trace("sess-1")
    chosen = exc_info.value.to_dict()["candidates"][0]["selector"]

    candidate_batch_2 = [
        _mock_row(_candidate_row(user_id="alice")),
        _mock_row(_candidate_row(user_id="bob")),
    ]
    fetch_batch = [_mock_row(_event_row(user_id="alice"))]
    client2, _ = self._client([candidate_batch_2, fetch_batch])
    trace = client2.get_trace_by_selector(TraceSelector(**chosen))
    assert trace.identity.user_id == "alice"
