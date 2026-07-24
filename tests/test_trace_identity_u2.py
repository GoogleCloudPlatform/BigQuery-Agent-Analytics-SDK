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
from bigquery_agent_analytics.client import _parse_tag_payload
from bigquery_agent_analytics.client import _RESOLVE_CANDIDATES_BATCH_QUERY
from bigquery_agent_analytics.client import _resolve_scope_candidates
from bigquery_agent_analytics.client import _RESOLVE_SESSION_CANDIDATES_QUERY
from bigquery_agent_analytics.client import _RESOLVE_SESSION_IDENTITIES_QUERY
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
    scope_trace_id=None,
    row_count=1,
):
  import json as json_mod

  # The candidates query reads TO_JSON_STRING forms so scalar types
  # survive to Python validation.
  return {
      "session_id": session_id,
      "user_id": user_id,
      "root_agent_name": (
          json_mod.dumps(root_agent_name)
          if root_agent_name is not None
          else None
      ),
      "experiment_id": (
          json_mod.dumps(experiment_id) if experiment_id is not None else None
      ),
      "tag_payload": tag_payload,
      "scope_trace_id": scope_trace_id,
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
    untagged = (
        "COALESCE(TO_JSON_STRING(JSON_QUERY(e.attributes,"
        " '$.custom_tags')), 'null') IN ('null', '{}')"
    )
    assert (
        "(JSON_VALUE(e.attributes, CONCAT('$.custom_tags.', @label_key_0))"
        f" = @label_val_0 OR {untagged})" in query
    )
    assert (
        "(JSON_VALUE(e.attributes, '$.experiment_id') = @experiment_id"
        " OR (JSON_VALUE(e.attributes, '$.experiment_id') IS NULL AND"
        f" {untagged}))" in query
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
    assert "{event_where}" in _GET_SESSION_TRACE_QUERY


class TestTagPayloadParsing:

  def test_parse_forms(self):
    assert _parse_tag_payload(None) is None
    assert _parse_tag_payload("null") is None
    assert _parse_tag_payload("{}") is None
    assert _parse_tag_payload('{"run": "v1"}') == {"run": "v1"}
    assert _parse_tag_payload({"run": "v1"}) == {"run": "v1"}

  def test_persisted_schema_enforced_fail_closed(self):
    # {"run": 3} must not canonicalize into the {"run": "3"} scope,
    # and malformed shapes must not become unscoped candidates.
    with pytest.raises(ValueError, match="string values"):
      _parse_tag_payload('{"run": 3}')
    with pytest.raises(ValueError, match="JSON object"):
      _parse_tag_payload('["run", "v1"]')
    with pytest.raises(ValueError, match="Malformed"):
      _parse_tag_payload("{not json")


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

  def test_exact_payload_splitting_no_superset_merge(self):
    # Fail-closed pass splitting (U1 scope_signature contract): a
    # strict payload superset is its own scope candidate, never
    # silently merged as "enrichment".
    rows = [
        _candidate_row(tag_payload='{"run": "v1"}'),
        _candidate_row(tag_payload='{"run": "v1", "slice": "3"}'),
    ]
    candidates = _resolve_scope_candidates(rows)
    assert len(candidates) == 2
    label_sets = {
        tuple(sorted(c.scope.labels_dict.items())) for c in candidates
    }
    assert (("run", "v1"),) in label_sets
    assert (("run", "v1"), ("slice", "3")) in label_sets

  def test_candidate_ordering_deterministic(self):
    rows_forward = [
        _candidate_row(user_id="alice"),
        _candidate_row(user_id="bob"),
    ]
    rows_reverse = list(reversed(rows_forward))
    forward = _resolve_scope_candidates(rows_forward)
    reverse = _resolve_scope_candidates(rows_reverse)
    assert forward == reverse

  def test_null_experiment_rows_shared_into_experiments(self):
    # Mirrors the SQL row-scope semantics: NULL-experiment rows are
    # shared, not a separate candidate, when experiments exist.
    rows = [
        _candidate_row(experiment_id="e1", tag_payload=None),
        _candidate_row(experiment_id=None, tag_payload=None),
    ]
    candidates = _resolve_scope_candidates(rows)
    assert len(candidates) == 1
    assert candidates[0].scope.experiment_id == "e1"

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

  def test_exact_payloads_split_into_traces(self):
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
    assert len(traces) == 2
    payloads = {
        tuple(sorted(t.scope.labels_dict.items())): {s.span_id for s in t.spans}
        for t in traces
    }
    assert payloads[(("assistant", "cc"),)] == {"s1"}
    assert payloads[(("assistant", "cc"), ("subagent_id", "x"))] == {"s2"}

  def test_shared_spans_are_copied_per_trace(self):
    # Trace._build_tree mutates Span.children; sibling traces must
    # not share the same span object (review P1-6).
    rows = [
        _mock_row(
            dict(
                _event_row(custom_tags={"run": "v0"}, span_id="c0"),
                parent_span_id="shared",
            )
        ),
        _mock_row(
            dict(
                _event_row(custom_tags={"run": "v1"}, span_id="c1"),
                parent_span_id="shared",
            )
        ),
        _mock_row(_event_row(span_id="shared")),
    ]
    traces = _build_traces_from_rows(rows)
    by_run = {t.scope.labels_dict["run"]: t for t in traces}
    by_run["v0"]._build_tree()
    shared_v0 = next(s for s in by_run["v0"].spans if s.span_id == "shared")
    assert [c.span_id for c in shared_v0.children] == ["c0"]
    by_run["v1"]._build_tree()
    # v1's tree build must not have rewritten v0's shared parent.
    assert [c.span_id for c in shared_v0.children] == ["c0"]

  def test_trace_id_derived_per_scope(self):
    rows = [
        _mock_row(
            _event_row(
                custom_tags={"run": "v0"}, span_id="p0", trace_id="tr-v0"
            )
        ),
        _mock_row(
            _event_row(
                custom_tags={"run": "v1"}, span_id="p1", trace_id="tr-v1"
            )
        ),
    ]
    traces = _build_traces_from_rows(rows)
    ids = {t.scope.labels_dict["run"]: t.trace_id for t in traces}
    assert ids == {"v0": "tr-v0", "v1": "tr-v1"}

  def test_null_experiment_rows_shared_in_grouping(self):
    rows = [
        _mock_row(_event_row(experiment_id="e1", span_id="e1row")),
        _mock_row(_event_row(span_id="nullrow")),
    ]
    traces = _build_traces_from_rows(rows)
    assert len(traces) == 1
    assert traces[0].scope.experiment_id == "e1"
    assert {s.span_id for s in traces[0].spans} == {"e1row", "nullrow"}

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

  def test_selector_event_types_push_down_after_exact_resolution(self):
    scope = TraceScope(custom_labels={"run": "v1"})
    candidate_batch = [_mock_row(_candidate_row(tag_payload='{"run": "v1"}'))]
    selected = _event_row(
        custom_tags=None,
        span_id="selected-shared",
    )
    selected["event_type"] = "TOOL_COMPLETED"
    client, mock_bq = self._client([candidate_batch, [_mock_row(selected)]])

    trace = client.get_trace_by_selector(
        TraceSelector(
            session_id="sess-1",
            scope_signature=scope.scope_signature,
        ),
        event_types=["TOOL_COMPLETED"],
    )

    fetch_query = mock_bq.query.call_args.args[0]
    fetch_params = {
        param.name: param
        for param in mock_bq.query.call_args.kwargs[
            "job_config"
        ].query_parameters
    }
    assert "e.event_type IN UNNEST(@selected_event_types)" in fetch_query
    assert fetch_params["selected_event_types"].values == ["TOOL_COMPLETED"]
    assert trace.scope == scope
    assert [span.span_id for span in trace.spans] == ["selected-shared"]

  def test_selector_event_types_can_return_empty_attributed_trace(self):
    scope = TraceScope(custom_labels={"run": "v1"})
    candidate_batch = [_mock_row(_candidate_row(tag_payload='{"run": "v1"}'))]
    client, _ = self._client([candidate_batch, []])

    trace = client.get_trace_by_selector(
        TraceSelector(
            session_id="sess-1",
            scope_signature=scope.scope_signature,
        ),
        event_types=["TOOL_COMPLETED"],
    )

    assert trace.identity == TraceIdentity(session_id="sess-1", user_id="alice")
    assert trace.scope == scope
    assert trace.spans == []

  def test_selector_event_types_preserve_resolved_scope_trace_id(self):
    scope = TraceScope(custom_labels={"run": "v1"})
    candidate_batch = [
        _mock_row(
            _candidate_row(
                tag_payload='{"run": "v1"}',
                scope_trace_id="scope-trace-1",
            )
        )
    ]
    selected_shared = _event_row(
        custom_tags=None,
        span_id="selected-shared",
        trace_id="shared-trace",
    )
    selected_shared["event_type"] = "USER_MESSAGE_RECEIVED"
    client, mock_bq = self._client(
        [candidate_batch, [_mock_row(selected_shared)]]
    )

    trace = client.get_trace_by_selector(
        TraceSelector(
            session_id="sess-1",
            scope_signature=scope.scope_signature,
        ),
        event_types=["USER_MESSAGE_RECEIVED"],
    )

    assert trace.trace_id == "scope-trace-1"
    assert "AS scope_trace_id" in mock_bq.query.call_args_list[0].args[0]

  def test_event_filtered_zero_span_semantics_are_documented(self):
    for method in (Client.get_session_trace, Client.get_trace_by_selector):
      doc = method.__doc__ or ""
      assert "zero-span" in doc
      assert "does not raise" in doc

  def test_mixed_selector_event_types_separates_scope_metadata_from_spans(self):
    scope_v0 = TraceScope(custom_labels={"run": "v0"})
    scope_v1 = TraceScope(custom_labels={"run": "v1"})
    candidate_batch = [
        _mock_row(_candidate_row(tag_payload='{"run": "v0"}')),
        _mock_row(_candidate_row(tag_payload='{"run": "v1"}')),
    ]
    metadata_batch = [
        _mock_row(_event_row(custom_tags={"run": "v0"}, span_id="meta-v0")),
        _mock_row(_event_row(custom_tags={"run": "v1"}, span_id="meta-v1")),
    ]
    selected = _event_row(custom_tags=None, span_id="selected")
    selected["event_type"] = "TOOL_COMPLETED"
    client, mock_bq = self._client(
        [candidate_batch, metadata_batch, [_mock_row(selected)]]
    )

    trace = client.get_trace_by_selector(
        TraceSelector(session_id="sess-1"),
        allow_mixed_scope=True,
        event_types=["TOOL_COMPLETED"],
    )

    metadata_query = mock_bq.query.call_args_list[1].args[0]
    fetch_query = mock_bq.query.call_args_list[2].args[0]
    assert "e.content" not in metadata_query
    assert "e.event_type IN UNNEST(@selected_event_types)" in fetch_query
    assert trace.scope is None
    assert trace.scope_coverage == tuple(
        sorted((scope_v0.scope_signature, scope_v1.scope_signature))
    )
    assert [span.span_id for span in trace.spans] == ["selected"]

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


class TestRowScopeSharedRowRule:
  """Only fully untagged rows are shared (review P1-1)."""

  def test_missing_pinned_key_on_tagged_row_excluded(self):
    where = TraceFilter(custom_labels={"run": "v1"}).row_scope_where()
    # The shared-row branch checks the WHOLE payload, not the key:
    # {"slice": "secret"} rows (tagged, missing "run") are excluded,
    # while missing/JSON-null/empty payloads all count as untagged.
    assert (
        "COALESCE(TO_JSON_STRING(JSON_QUERY(e.attributes,"
        " '$.custom_tags')), 'null') IN ('null', '{}')" in where
    )
    assert (
        "JSON_VALUE(e.attributes, CONCAT('$.custom_tags.', @label_key_0))"
        " IS NULL" not in where
    )


class TestResolvedSelectorFilterConversion:
  """The anchored fetch uses U1's retry conversion (review P1-3)."""

  def _client_with_batches(self, batches):
    from unittest.mock import MagicMock

    mock_bq = MagicMock()
    jobs = []
    for batch in batches:
      job = MagicMock()
      job.result.return_value = batch
      jobs.append(job)
    mock_bq.query.side_effect = jobs
    return (
        Client(
            project_id="proj",
            dataset_id="ds",
            verify_schema=False,
            bq_client=mock_bq,
        ),
        mock_bq,
    )

  def test_null_experiment_fetch_preserves_context(self):
    # Round 6 P1-4 (supersedes round 3): a resolved NULL experiment
    # fetches WITHOUT an experiment row predicate — restricting to
    # NULL rows would erase the context needed to classify shared
    # rows — and the consistency selection picks the resolved scope.
    candidate_batch = [_mock_row(_candidate_row(experiment_id=None))]
    fetch_batch = [_mock_row(_event_row())]
    client, mock_bq = self._client_with_batches([candidate_batch, fetch_batch])
    trace = client.get_session_trace("sess-1")
    assert trace.scope.experiment_id is None
    fetch_query = mock_bq.query.call_args[0][0]
    assert "JSON_VALUE(e.attributes, '$.experiment_id') IS NULL" not in (
        fetch_query
    )

  def test_unaddressable_label_retry_supported(self):
    # BigQuery JSON can store this member; the retry must drop it
    # from SQL under signature attestation instead of raising.
    candidate_batch = [
        _mock_row(_candidate_row(tag_payload='{"a\\\\": "x"}')),
    ]
    fetch_batch = [_mock_row(_event_row(custom_tags={"a\\": "x"}))]
    client, mock_bq = self._client_with_batches([candidate_batch, fetch_batch])
    trace = client.get_session_trace("sess-1")
    assert trace.scope.labels_dict == {"a\\": "x"}
    fetch_params = {
        p.name
        for p in mock_bq.query.call_args[1]["job_config"].query_parameters
    }
    assert "label_key_0" not in fetch_params  # dropped, attested


class TestSingularConsistencyFailClosed:
  """Resolution/fetch disagreement raises (review P1-4)."""

  def test_stale_scope_raises_instead_of_substituting(self):
    from unittest.mock import MagicMock

    candidate_batch = [
        _mock_row(_candidate_row(tag_payload='{"run": "v0"}')),
    ]
    # Fetch returns only untagged rows: the resolved v0 scope is gone.
    fetch_batch = [_mock_row(_event_row(span_id="shared"))]
    mock_bq = MagicMock()
    jobs = []
    for batch in [candidate_batch, fetch_batch]:
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
    with pytest.raises(ValueError, match="consistency failure"):
      client.get_session_trace("sess-1")


class TestLimitAfterScopeExpansion:
  """TraceFilter.limit bounds the returned traces (review P1-8)."""

  def test_limit_applies_after_expansion(self):
    from unittest.mock import MagicMock

    rows = [
        _mock_row(_event_row(custom_tags={"run": "v0"}, span_id="p0")),
        _mock_row(_event_row(custom_tags={"run": "v1"}, span_id="p1")),
    ]
    mock_bq = MagicMock()
    job = MagicMock()
    job.result.return_value = rows
    mock_bq.query.return_value = job
    client = Client(
        project_id="proj",
        dataset_id="ds",
        verify_schema=False,
        bq_client=mock_bq,
    )
    traces = client.list_traces(TraceFilter(limit=1))
    assert len(traces) == 1

  def test_ordering_is_deterministic(self):
    from bigquery_agent_analytics.client import _ordered_limited_traces

    rows_a = [
        _mock_row(_event_row(user_id="alice", span_id="a")),
        _mock_row(_event_row(user_id="bob", span_id="b")),
    ]
    rows_b = list(reversed(rows_a))
    order_a = [
        t.identity.user_id
        for t in _ordered_limited_traces(_build_traces_from_rows(rows_a), None)
    ]
    order_b = [
        t.identity.user_id
        for t in _ordered_limited_traces(_build_traces_from_rows(rows_b), None)
    ]
    assert order_a == order_b


class TestSelectorPushdownAndCap:
  """Identity pins reach SQL; candidates are capped (review P2-11)."""

  def test_identity_pins_pushed_into_resolution_query(self):
    from unittest.mock import MagicMock

    candidate_batch = [_mock_row(_candidate_row(user_id="alice"))]
    fetch_batch = [_mock_row(_event_row(user_id="alice"))]
    mock_bq = MagicMock()
    jobs = []
    for batch in [candidate_batch, fetch_batch]:
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
    client.get_session_trace("sess-1", user_id="alice")
    resolve_query = mock_bq.query.call_args_list[0][0][0]
    assert "AND user_id = @pin_user_id" in resolve_query

  def test_null_pin_pushdown(self):
    from unittest.mock import MagicMock

    candidate_batch = [_mock_row(_candidate_row(user_id=None))]
    fetch_batch = [_mock_row(_event_row(user_id=None))]
    mock_bq = MagicMock()
    jobs = []
    for batch in [candidate_batch, fetch_batch]:
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
    client.get_session_trace("sess-1", user_id=None)
    resolve_query = mock_bq.query.call_args_list[0][0][0]
    assert "AND user_id IS NULL" in resolve_query

  def test_truncated_page_with_matches_raises_typed_ambiguity(self):
    from unittest.mock import MagicMock

    from bigquery_agent_analytics.client import _MAX_SCOPE_CANDIDATES

    # A truncated page whose NON-boundary identities still hold
    # multiple matches raises the TYPED ambiguity surface, marked as
    # a lower bound. The boundary identity (bob, sorted last) is
    # excluded from classification (round 6, P1-5) but the ambiguity
    # among alice's fully-enumerated scopes is provable.
    candidate_batch = [
        _mock_row(
            _candidate_row(user_id="alice", tag_payload=f'{{"k": "v{i}"}}')
        )
        for i in range(_MAX_SCOPE_CANDIDATES - 2)
    ] + [
        _mock_row(_candidate_row(user_id="bob", tag_payload=f'{{"b": "v{i}"}}'))
        for i in range(3)
    ]
    mock_bq = MagicMock()
    job = MagicMock()
    job.result.return_value = candidate_batch
    mock_bq.query.return_value = job
    client = Client(
        project_id="proj",
        dataset_id="ds",
        verify_schema=False,
        bq_client=mock_bq,
    )
    with pytest.raises(AmbiguousSessionError) as exc_info:
      client.get_session_trace("sess-1")
    assert len(exc_info.value.candidates) <= _MAX_SCOPE_CANDIDATES + 1
    # Round 5 P2-10: the capped set is marked as a lower bound.
    assert exc_info.value.population_truncated is True
    assert "At least" in str(exc_info.value)
    assert exc_info.value.to_dict()["population_truncated"] is True
    query = mock_bq.query.call_args[0][0]
    assert "LIMIT @candidate_limit" in query

  def test_truncated_single_match_cannot_prove_uniqueness(self):
    from unittest.mock import MagicMock

    from bigquery_agent_analytics.client import _MAX_SCOPE_CANDIDATES

    # Round 5 P1-1 (supersedes round 4): a truncated page with one
    # match cannot prove uniqueness — another matching candidate may
    # sort beyond the page — so the read fails with the bound error.
    target_signature = TraceScope(custom_labels={"k": "v0"}).scope_signature
    candidate_batch = [
        _mock_row(_candidate_row(tag_payload=f'{{"k": "v{i}"}}'))
        for i in range(_MAX_SCOPE_CANDIDATES + 1)
    ]
    mock_bq = MagicMock()
    job = MagicMock()
    job.result.return_value = candidate_batch
    mock_bq.query.return_value = job
    client = Client(
        project_id="proj",
        dataset_id="ds",
        verify_schema=False,
        bq_client=mock_bq,
    )
    with pytest.raises(ValueError, match="neither uniqueness nor absence"):
      client.get_session_trace("sess-1", scope_signature=target_signature)

  def test_exact_selector_resolves_on_complete_page(self):
    from unittest.mock import MagicMock

    # A complete (non-truncated) page with one match resolves.
    target_signature = TraceScope(custom_labels={"k": "v0"}).scope_signature
    candidate_batch = [
        _mock_row(_candidate_row(tag_payload='{"k": "v0"}')),
        _mock_row(_candidate_row(tag_payload='{"k": "v1"}')),
    ]
    fetch_batch = [
        _mock_row(_event_row(custom_tags={"k": "v0"}, span_id="p0")),
    ]
    mock_bq = MagicMock()
    jobs = []
    for batch in [candidate_batch, fetch_batch]:
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
    trace = client.get_session_trace("sess-1", scope_signature=target_signature)
    assert trace.scope.labels_dict == {"k": "v0"}

  def test_truncated_page_without_match_redacted_value_error(self):
    from unittest.mock import MagicMock

    from bigquery_agent_analytics.client import _MAX_SCOPE_CANDIDATES

    missing_signature = TraceScope(
        custom_labels={"k": "not-present"}
    ).scope_signature
    candidate_batch = [
        _mock_row(_candidate_row(tag_payload=f'{{"k": "v{i}"}}'))
        for i in range(_MAX_SCOPE_CANDIDATES + 1)
    ]
    mock_bq = MagicMock()
    job = MagicMock()
    job.result.return_value = candidate_batch
    mock_bq.query.return_value = job
    client = Client(
        project_id="proj",
        dataset_id="ds",
        verify_schema=False,
        bq_client=mock_bq,
    )
    with pytest.raises(ValueError) as exc_info:
      client.get_session_trace("sess-1", scope_signature=missing_signature)
    message = str(exc_info.value)
    assert "enumeration bound" in message
    assert "v0" not in message and "not-present" not in message


class TestAllowMixedScope:
  """The KTD4 escape hatch returns conversation-complete reads."""

  def test_mixed_scope_read_merges_one_identity(self):
    from unittest.mock import MagicMock

    candidate_batch = [
        _mock_row(_candidate_row(tag_payload='{"run": "v0"}')),
        _mock_row(_candidate_row(tag_payload='{"run": "v1"}')),
    ]
    fetch_batch = [
        _mock_row(_event_row(custom_tags={"run": "v0"}, span_id="p0")),
        _mock_row(_event_row(custom_tags={"run": "v1"}, span_id="p1")),
        _mock_row(_event_row(span_id="shared")),
    ]
    mock_bq = MagicMock()
    jobs = []
    for batch in [candidate_batch, fetch_batch]:
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
    trace = client.get_session_trace("sess-1", allow_mixed_scope=True)
    assert [s.span_id for s in trace.spans] == ["p0", "p1", "shared"]
    assert trace.identity.user_id == "alice"
    assert trace.scope is None  # no single resolved scope describes it
    # KTD4 coverage metadata names the covered scope signatures.
    assert trace.scope_coverage is not None
    assert len(trace.scope_coverage) == 2
    assert mock_bq.query.call_count == 2

  def test_mixed_scope_still_ambiguous_across_identities(self):
    from unittest.mock import MagicMock

    candidate_batch = [
        _mock_row(_candidate_row(user_id="alice")),
        _mock_row(_candidate_row(user_id="bob")),
    ]
    mock_bq = MagicMock()
    job = MagicMock()
    job.result.return_value = candidate_batch
    mock_bq.query.return_value = job
    client = Client(
        project_id="proj",
        dataset_id="ds",
        verify_schema=False,
        bq_client=mock_bq,
    )
    with pytest.raises(AmbiguousSessionError):
      client.get_session_trace("sess-1", allow_mixed_scope=True)


class TestRound3Regressions:
  """PR #371 review round 3 reproduced findings."""

  def _client(self, batches):
    from unittest.mock import MagicMock

    mock_bq = MagicMock()
    jobs = []
    for batch in batches:
      job = MagicMock()
      job.result.return_value = batch
      jobs.append(job)
    mock_bq.query.side_effect = jobs
    return (
        Client(
            project_id="proj",
            dataset_id="ds",
            verify_schema=False,
            bq_client=mock_bq,
        ),
        mock_bq,
    )

  def test_mixed_read_preserves_duplicate_span_ids_and_order(self):
    # P1-1: no span_id dedup, no chronology loss — one span per row
    # in producer order, even when span ids repeat.
    candidate_batch = [
        _mock_row(_candidate_row(tag_payload='{"run": "v0"}')),
        _mock_row(_candidate_row(tag_payload='{"run": "v1"}')),
    ]
    fetch_batch = [
        _mock_row(_event_row(custom_tags={"run": "v0"}, span_id="dup")),
        _mock_row(_event_row(custom_tags={"run": "v1"}, span_id="dup")),
        _mock_row(_event_row(span_id="tail")),
    ]
    client, mock_bq = self._client([candidate_batch, fetch_batch])
    trace = client.get_session_trace("sess-1", allow_mixed_scope=True)
    assert [s.span_id for s in trace.spans] == ["dup", "dup", "tail"]
    assert mock_bq.query.call_count == 2

  def test_exact_selector_wins_over_mixed_scope_flag(self):
    # P1-3: a signature-pinned selector returns its exact candidate
    # even with allow_mixed_scope=True.
    v0_signature = TraceScope(custom_labels={"run": "v0"}).scope_signature
    candidate_batch = [
        _mock_row(_candidate_row(tag_payload='{"run": "v0"}')),
        _mock_row(_candidate_row(tag_payload='{"run": "v1"}')),
    ]
    fetch_batch = [
        _mock_row(_event_row(custom_tags={"run": "v0"}, span_id="p0")),
    ]
    client, _ = self._client([candidate_batch, fetch_batch])
    trace = client.get_session_trace(
        "sess-1", scope_signature=v0_signature, allow_mixed_scope=True
    )
    assert trace.scope is not None
    assert trace.scope.labels_dict == {"run": "v0"}
    assert trace.scope_coverage is None

  def test_deep_copy_isolates_span_contents(self):
    # P1-2: shared rows must not alias content/attributes dicts.
    rows = [
        _mock_row(_event_row(custom_tags={"run": "v0"}, span_id="p0")),
        _mock_row(_event_row(custom_tags={"run": "v1"}, span_id="p1")),
        _mock_row(_event_row(span_id="shared")),
    ]
    traces = _build_traces_from_rows(rows)
    shared_copies = [
        s for t in traces for s in t.spans if s.span_id == "shared"
    ]
    assert len(shared_copies) == 2
    assert shared_copies[0] is not shared_copies[1]
    shared_copies[0].content["poison"] = True
    assert "poison" not in shared_copies[1].content

  def test_null_experiment_shared_rows_isolated_across_subgroups(self):
    # P1-2 second shape: a NULL-experiment row shared into e1/e2 is
    # not the same object in both traces.
    rows = [
        _mock_row(_event_row(experiment_id="e1", span_id="a")),
        _mock_row(_event_row(experiment_id="e2", span_id="b")),
        _mock_row(_event_row(span_id="nullrow")),
    ]
    traces = _build_traces_from_rows(rows)
    assert len(traces) == 2
    null_spans = [s for t in traces for s in t.spans if s.span_id == "nullrow"]
    assert len(null_spans) == 2
    assert null_spans[0] is not null_spans[1]

  def test_genuine_null_experiment_scope_selectable(self):
    # P1-5: a tagged NULL-experiment pass keeps its own candidate.
    rows = [
        _candidate_row(experiment_id="e1", tag_payload=None),
        _candidate_row(experiment_id=None, tag_payload='{"run": "v9"}'),
    ]
    candidates = _resolve_scope_candidates(rows)
    experiments = {c.scope.experiment_id for c in candidates}
    assert experiments == {"e1", None}
    null_candidate = next(
        c for c in candidates if c.scope.experiment_id is None
    )
    assert null_candidate.scope.labels_dict == {"run": "v9"}
    matching = _candidates_matching_selector(
        candidates, TraceSelector(session_id="sess-1", experiment_id=None)
    )
    assert matching == [null_candidate]

  def test_max_traces_bounds_expansion(self):
    # P2-11 / P1-7: construction stops at the bound.
    rows = [
        _mock_row(_event_row(custom_tags={"k": f"v{i}"}, span_id=f"s{i}"))
        for i in range(6)
    ]
    bounded = _build_traces_from_rows(rows, max_traces=2)
    assert len(bounded) == 2
    unbounded = _build_traces_from_rows(rows)
    assert len(unbounded) == 6

  def test_trace_id_ignores_earlier_shared_row(self):
    # P2-10: an early untagged row's trace id must not shadow the
    # scoped producer ids.
    rows = [
        _mock_row(_event_row(span_id="shared", trace_id="trace-shared")),
        _mock_row(
            _event_row(
                custom_tags={"run": "v0"}, span_id="p0", trace_id="tr-v0"
            )
        ),
        _mock_row(
            _event_row(
                custom_tags={"run": "v1"}, span_id="p1", trace_id="tr-v1"
            )
        ),
    ]
    traces = _build_traces_from_rows(rows)
    ids = {t.scope.labels_dict["run"]: t.trace_id for t in traces}
    assert ids == {"v0": "tr-v0", "v1": "tr-v1"}

  def test_null_and_empty_string_identities_order_distinctly(self):
    # P2-12: NULL and '' must not tie in deterministic ordering.
    from bigquery_agent_analytics.client import _ordered_limited_traces

    rows_forward = [
        _mock_row(_event_row(user_id=None, span_id="n")),
        _mock_row(_event_row(user_id="", span_id="e")),
    ]
    rows_reverse = list(reversed(rows_forward))
    kept_forward = _ordered_limited_traces(
        _build_traces_from_rows(rows_forward), 1
    )[0].identity.user_id
    kept_reverse = _ordered_limited_traces(
        _build_traces_from_rows(rows_reverse), 1
    )[0].identity.user_id
    assert kept_forward == kept_reverse

  def test_schema_type_mismatch_raises(self):
    # P1-8: a STRING-backed attributes column is a hard error.
    from unittest.mock import MagicMock

    mock_bq = MagicMock()
    job = MagicMock()
    job.result.return_value = [
        _mock_row({"column_name": name, "data_type": dtype})
        for name, dtype in [
            ("attributes", "STRING"),
            ("content", "JSON"),
            ("session_id", "STRING"),
        ]
    ]
    mock_bq.query.return_value = job
    with pytest.raises(ValueError, match="incompatible column types"):
      Client(
          project_id="proj",
          dataset_id="ds",
          verify_schema=True,
          bq_client=mock_bq,
      )

  def test_error_messages_redact_scope_values(self):
    # P1-9: malformed-payload and consistency errors carry no values.
    with pytest.raises(ValueError) as exc_info:
      _parse_tag_payload('{"secret-key": 42}')
    message = str(exc_info.value)
    assert "secret-key" not in message
    assert "42" not in message

    candidate_batch = [
        _mock_row(_candidate_row(tag_payload='{"secret": "value"}')),
    ]
    fetch_batch = [_mock_row(_event_row(span_id="shared"))]
    client, _ = self._client([candidate_batch, fetch_batch])
    with pytest.raises(ValueError) as exc_info:
      client.get_session_trace("sess-1")
    message = str(exc_info.value)
    assert "consistency failure" in message
    assert "secret" not in message and "value" not in message

  def test_query_fragments_snapshot_consistent(self):
    # P2-13: fragments and params come from one snapshot.
    filt = TraceFilter(custom_labels={"run": "v0"})
    where, row_where, params = filt.to_query_fragments()
    assert "@label_key_0" in where
    assert "@label_key_0" in row_where
    names = {p.name for p in params}
    assert {"label_key_0", "label_val_0"} <= names


class TestRound4Regressions:
  """PR #371 review round 4 reproduced findings."""

  def _client(self, batches):
    from unittest.mock import MagicMock

    mock_bq = MagicMock()
    jobs = []
    for batch in batches:
      job = MagicMock()
      job.result.return_value = batch
      jobs.append(job)
    mock_bq.query.side_effect = jobs
    return (
        Client(
            project_id="proj",
            dataset_id="ds",
            verify_schema=False,
            bq_client=mock_bq,
        ),
        mock_bq,
    )

  def test_mixed_scope_nonexistent_selector_not_found(self):
    # P1-1: run=missing must not return the whole identity — the
    # scope-pinned identity query comes back empty.
    candidate_batch: list = []  # pushdown pins exclude everything
    client, mock_bq = self._client([candidate_batch])
    # Round 8, P3-12: a pin-excluded page names the pins, not a
    # false session absence.
    with pytest.raises(ValueError, match="under the selector's pins"):
      client.get_session_trace(
          "sess-1",
          custom_labels={"run": "missing"},
          allow_mixed_scope=True,
      )
    candidate_query = mock_bq.query.call_args[0][0]
    # Candidate discovery carries the scope pushdown.
    assert "@pin_label_key_0" in candidate_query
    assert mock_bq.query.call_count == 1

  def test_mixed_scope_pins_prevent_false_identity_ambiguity(self):
    # P1-1: scope pins that select only Alice's scopes must not
    # produce an Alice/Bob ambiguity — the identity query honors the
    # pins and returns one identity.
    candidate_batch = [
        _mock_row(
            _candidate_row(
                user_id="alice",
                tag_payload='{"slice": "1", "team": "a"}',
            )
        ),
        _mock_row(
            _candidate_row(
                user_id="alice",
                tag_payload='{"slice": "2", "team": "a"}',
            )
        ),
        _mock_row(_candidate_row(user_id="bob")),
    ]
    fetch_batch = [
        _mock_row(_event_row(user_id="alice", custom_tags={"team": "a"})),
    ]
    client, mock_bq = self._client([candidate_batch, fetch_batch])
    trace = client.get_session_trace(
        "sess-1", custom_labels={"team": "a"}, allow_mixed_scope=True
    )
    assert trace.identity.user_id == "alice"
    assert mock_bq.query.call_count == 2

  def test_mixed_coverage_reflects_fetched_scopes(self):
    # P1-3: coverage names every scope actually fetched, not just the
    # selector-matching ones.
    candidate_batch = [
        _mock_row(_candidate_row(tag_payload='{"run": "v0"}')),
        _mock_row(_candidate_row(tag_payload='{"run": "v1"}')),
    ]
    fetch_batch = [
        _mock_row(_event_row(custom_tags={"run": "v0"}, span_id="p0")),
        _mock_row(_event_row(custom_tags={"run": "v1"}, span_id="p1")),
    ]
    client, _ = self._client([candidate_batch, fetch_batch])
    trace = client.get_session_trace("sess-1", allow_mixed_scope=True)
    assert trace.scope_coverage is not None
    v0_sig = TraceScope(custom_labels={"run": "v0"}).scope_signature
    v1_sig = TraceScope(custom_labels={"run": "v1"}).scope_signature
    assert set(trace.scope_coverage) == {v0_sig, v1_sig}

  def test_max_traces_retains_most_recent_scope(self):
    # P1-4: rank-before-retain — bounded construction must keep the
    # trace that unbounded construction + ordering would keep.
    from datetime import timedelta

    early = dict(
        _event_row(custom_tags={"run": "early"}, span_id="e"),
        timestamp=_TS,
    )
    late = dict(
        _event_row(custom_tags={"run": "late"}, span_id="l"),
        timestamp=_TS + timedelta(hours=1),
    )
    rows = [_mock_row(early), _mock_row(late)]
    bounded = _build_traces_from_rows(rows, max_traces=1)
    assert len(bounded) == 1
    assert bounded[0].scope.labels_dict == {"run": "late"}

  def test_discarded_slots_do_not_trigger_copies(self):
    # P1-5: with one retained slot, its spans are the original
    # objects — no deep copies were made for discarded slots.
    from datetime import timedelta

    rows = [
        _mock_row(
            dict(
                _event_row(custom_tags={"run": f"v{i}"}, span_id=f"s{i}"),
                timestamp=_TS + timedelta(minutes=i),
            )
        )
        for i in range(5)
    ] + [_mock_row(_event_row(span_id="shared"))]
    bounded = _build_traces_from_rows(rows, max_traces=1)
    assert len(bounded) == 1
    shared = [s for s in bounded[0].spans if s.span_id == "shared"]
    assert len(shared) == 1
    # Single retained destination: the span was not copied.
    assert not shared[0].children  # sanity: still a clean span

  def test_mixed_identity_ambiguity_uses_real_candidates(self):
    # P1-7: cross-identity mixed ambiguity carries REAL scope
    # candidates (executable retries), not synthetic empty scopes.
    candidate_batch = [
        _mock_row(_candidate_row(user_id="alice", tag_payload='{"run": "v0"}')),
        _mock_row(_candidate_row(user_id="bob", tag_payload='{"run": "v1"}')),
    ]
    client, mock_bq = self._client([candidate_batch])
    with pytest.raises(AmbiguousSessionError) as exc_info:
      client.get_session_trace("sess-1", allow_mixed_scope=True)
    payloads = [
        c["selector"]["custom_labels"]
        for c in exc_info.value.to_dict()["candidates"]
    ]
    assert {"run": "v0"} in payloads
    assert {"run": "v1"} in payloads
    # A complete candidate page proves the cross-identity ambiguity
    # without any duplicate identity or scope rediscovery.
    candidate_query = mock_bq.query.call_args[0][0]
    assert "QUALIFY ROW_NUMBER() OVER" not in candidate_query
    assert mock_bq.query.call_count == 1

  def test_mixed_fetch_revalidates_row_identity(self):
    # P2-8: a fetched row not matching the resolved identity fails
    # closed instead of being returned under the wrong identity.
    candidate_batch = [
        _mock_row(_candidate_row(user_id="alice")),
        _mock_row(_candidate_row(user_id="alice", tag_payload='{"a": "b"}')),
    ]
    identity_batch = [
        _mock_row(
            {
                "session_id": "sess-1",
                "user_id": "alice",
                "root_agent_name": None,
            }
        )
    ]
    fetch_batch = [
        _mock_row(_event_row(user_id="alice", span_id="ok")),
        _mock_row(_event_row(user_id="mallory", span_id="bad")),
    ]
    client, _ = self._client([candidate_batch, identity_batch, fetch_batch])
    with pytest.raises(ValueError, match="consistency failure"):
      client.get_session_trace("sess-1", allow_mixed_scope=True)

  def test_shared_row_id_cannot_contaminate_sibling_scope(self):
    # P2-9: a shared row carrying a sibling scope's trace id must not
    # leak into the other scope's trace id.
    rows = [
        _mock_row(
            _event_row(
                custom_tags={"run": "v0"}, span_id="p0", trace_id="tr-v0"
            )
        ),
        _mock_row(
            _event_row(
                custom_tags={"run": "v1"}, span_id="p1", trace_id="tr-v1"
            )
        ),
        # Shared row that happens to carry v0's trace id.
        _mock_row(_event_row(span_id="shared", trace_id="tr-v0")),
    ]
    traces = _build_traces_from_rows(rows)
    ids = {t.scope.labels_dict["run"]: t.trace_id for t in traces}
    assert ids == {"v0": "tr-v0", "v1": "tr-v1"}

  def test_snapshot_detaches_event_types(self):
    # P1-6: mutating the source filter's event_types after snapshot
    # must not affect the snapshot.
    filt = TraceFilter(event_types=["A"])
    snap = filt.snapshot()
    filt.event_types.append("B")
    assert snap.event_types == ["A"]


class TestRound5Regressions:
  """PR #371 review round 5 reproduced findings."""

  def _client(self, batches):
    from unittest.mock import MagicMock

    mock_bq = MagicMock()
    jobs = []
    for batch in batches:
      job = MagicMock()
      job.result.return_value = batch
      jobs.append(job)
    mock_bq.query.side_effect = jobs
    return (
        Client(
            project_id="proj",
            dataset_id="ds",
            verify_schema=False,
            bq_client=mock_bq,
        ),
        mock_bq,
    )

  def test_mixed_read_validates_python_only_signature_pin(self):
    # P1-2: a nonexistent scope_signature with allow_mixed_scope must
    # fail not-found, not return the whole identity.
    missing = TraceScope(custom_labels={"k": "missing"}).scope_signature
    candidate_batch = [
        _mock_row(_candidate_row(tag_payload='{"run": "v0"}')),
        _mock_row(_candidate_row(tag_payload='{"run": "v1"}')),
    ]
    identity_batch = [
        _mock_row(
            {
                "session_id": "sess-1",
                "user_id": "alice",
                "root_agent_name": None,
            }
        )
    ]
    fetch_batch = [
        _mock_row(_event_row(custom_tags={"run": "v0"}, span_id="p0")),
        _mock_row(_event_row(custom_tags={"run": "v1"}, span_id="p1")),
    ]
    client, _ = self._client([candidate_batch, identity_batch, fetch_batch])
    with pytest.raises(ValueError, match="No candidates match"):
      client.get_session_trace(
          "sess-1", scope_signature=missing, allow_mixed_scope=True
      )

  def test_mixed_read_validates_unaddressable_label_pin(self):
    # P1-2: an unaddressable label pin (not SQL-pushable) must be
    # verified against the fetched population.
    candidate_batch = [
        _mock_row(_candidate_row(tag_payload='{"run": "v0"}')),
        _mock_row(_candidate_row(tag_payload='{"run": "v1"}')),
    ]
    identity_batch = [
        _mock_row(
            {
                "session_id": "sess-1",
                "user_id": "alice",
                "root_agent_name": None,
            }
        )
    ]
    fetch_batch = [
        _mock_row(_event_row(custom_tags={"run": "v0"}, span_id="p0")),
        _mock_row(_event_row(custom_tags={"run": "v1"}, span_id="p1")),
    ]
    client, _ = self._client([candidate_batch, identity_batch, fetch_batch])
    with pytest.raises(ValueError, match="No candidates match"):
      client.get_session_trace(
          "sess-1",
          custom_labels={"a\\": "absent"},
          allow_mixed_scope=True,
      )

  def test_experiment_none_pin_not_pushed_down(self):
    # P1-3: experiment_id=None stays Python-side so shared NULL rows
    # cannot masquerade as a genuine NULL-experiment scope.
    candidate_batch = [
        _mock_row(_candidate_row(experiment_id="e1", tag_payload=None)),
        _mock_row(_candidate_row(experiment_id=None, tag_payload=None)),
    ]
    client, mock_bq = self._client([candidate_batch])
    with pytest.raises(ValueError, match="No candidates match"):
      client.get_session_trace("sess-1", experiment_id=None)
    resolve_query = mock_bq.query.call_args[0][0]
    assert "'$.experiment_id') IS NULL" not in resolve_query

  def test_filtered_listing_drops_unmatched_sibling_scopes(self):
    # P1-4: a run=v1 filter must not return an unrelated unlabeled
    # experiment scope reconstructed from admitted shared rows.
    from unittest.mock import MagicMock

    rows = [
        _mock_row(_event_row(custom_tags={"run": "v1"}, span_id="v1row")),
        _mock_row(_event_row(experiment_id="e9", span_id="e9row")),
    ]
    mock_bq = MagicMock()
    job = MagicMock()
    job.result.return_value = rows
    mock_bq.query.return_value = job
    client = Client(
        project_id="proj",
        dataset_id="ds",
        verify_schema=False,
        bq_client=mock_bq,
    )
    traces = client.list_traces(TraceFilter(custom_labels={"run": "v1"}))
    assert len(traces) == 1
    assert traces[0].scope.labels_dict == {"run": "v1"}

  def test_snapshot_defeats_lying_event_types(self):
    # P1-5: a lying list subclass injected past __setattr__ cannot
    # erase filters from the snapshot.
    class Liar(list):

      def __iter__(self):
        return iter([])

      def __len__(self):
        return 0

    filt = TraceFilter()
    object.__setattr__(filt, "event_types", Liar(["A"]))
    snap = filt.snapshot()
    assert snap.event_types == ["A"]

  def test_identity_ambiguity_candidates_selector_constrained(self):
    # P1-6: an excluded pass must not be advertised as a retry; every
    # ambiguous identity is represented via per-identity discovery.
    candidate_batch = [
        _mock_row(
            _candidate_row(
                user_id="alice", tag_payload='{"team": "x", "s": "1"}'
            )
        ),
        _mock_row(
            _candidate_row(
                user_id="alice", tag_payload='{"team": "x", "s": "2"}'
            )
        ),
        _mock_row(
            _candidate_row(user_id="bob", tag_payload='{"team": "x", "s": "3"}')
        ),
    ]
    identity_batch = [
        _mock_row(
            {
                "session_id": "sess-1",
                "user_id": "alice",
                "root_agent_name": None,
            }
        ),
        _mock_row(
            {"session_id": "sess-1", "user_id": "bob", "root_agent_name": None}
        ),
    ]
    batch = [
        _mock_row(
            _candidate_row(
                user_id="alice", tag_payload='{"team": "x", "s": "1"}'
            )
        ),
        _mock_row(
            _candidate_row(user_id="alice", tag_payload='{"excluded": "y"}')
        ),
        _mock_row(
            _candidate_row(user_id="bob", tag_payload='{"team": "x", "s": "3"}')
        ),
    ]
    client, _ = self._client([candidate_batch, identity_batch, batch])
    with pytest.raises(AmbiguousSessionError) as exc_info:
      client.get_session_trace(
          "sess-1", custom_labels={"team": "x"}, allow_mixed_scope=True
      )
    payload = exc_info.value.to_dict()
    users = {c["selector"]["user_id"] for c in payload["candidates"]}
    assert users == {"alice", "bob"}  # identity-complete
    for candidate in payload["candidates"]:
      labels = candidate["selector"]["custom_labels"] or {}
      assert labels.get("team") == "x"  # selector-constrained

  def test_factored_shared_spans_not_materialized_for_discarded(self):
    # P1-7: shared spans are attached only to retained slots.
    from datetime import timedelta

    rows = [
        _mock_row(
            dict(
                _event_row(custom_tags={"run": f"v{i}"}, span_id=f"s{i}"),
                timestamp=_TS + timedelta(minutes=i),
            )
        )
        for i in range(4)
    ] + [_mock_row(_event_row(span_id="shared"))]
    bounded = _build_traces_from_rows(rows, max_traces=1)
    assert len(bounded) == 1
    # The retained (most recent) slot carries the shared row once.
    assert [s.span_id for s in bounded[0].spans] == ["s3", "shared"]

  def test_scope_with_no_own_id_does_not_inherit_sibling_id(self):
    # P2-9: shared-row ids are trusted only in single-scope subgroups.
    rows = [
        _mock_row(
            _event_row(
                custom_tags={"run": "v0"}, span_id="p0", trace_id="tr-v0"
            )
        ),
        _mock_row(
            dict(
                _event_row(custom_tags={"run": "v1"}, span_id="p1"),
                trace_id=None,
            )
        ),
        _mock_row(_event_row(span_id="shared", trace_id="tr-v0")),
    ]
    traces = _build_traces_from_rows(rows)
    ids = {t.scope.labels_dict["run"]: t.trace_id for t in traces}
    assert ids["v0"] == "tr-v0"
    # v1 has no own id and siblings exist: falls back to session id.
    assert ids["v1"] == "sess-1"

  def test_single_scope_subgroup_still_uses_shared_id(self):
    rows = [
        _mock_row(
            dict(
                _event_row(custom_tags={"run": "v0"}, span_id="p0"),
                trace_id=None,
            )
        ),
        _mock_row(_event_row(span_id="shared", trace_id="tr-shared")),
    ]
    traces = _build_traces_from_rows(rows)
    assert len(traces) == 1
    assert traces[0].trace_id == "tr-shared"


class TestRound6Regressions:
  """PR #371 review round 6 reproduced findings."""

  def _client(self, batches):
    from unittest.mock import MagicMock

    mock_bq = MagicMock()
    jobs = []
    for batch in batches:
      job = MagicMock()
      job.result.return_value = batch
      jobs.append(job)
    mock_bq.query.side_effect = jobs
    return (
        Client(
            project_id="proj",
            dataset_id="ds",
            verify_schema=False,
            bq_client=mock_bq,
        ),
        mock_bq,
    )

  def test_experiment_pin_materializes_without_name_error(self):
    # P1-1: non-empty materialization under string and SQL_NULL
    # experiment pins executes the slot predicate.
    from unittest.mock import MagicMock

    from bigquery_agent_analytics.trace import SQL_NULL

    rows = [_mock_row(_event_row(experiment_id="e1", span_id="a"))]
    mock_bq = MagicMock()
    job = MagicMock()
    job.result.return_value = rows
    mock_bq.query.return_value = job
    client = Client(
        project_id="proj",
        dataset_id="ds",
        verify_schema=False,
        bq_client=mock_bq,
    )
    traces = client.list_traces(TraceFilter(experiment_id="e1"))
    assert len(traces) == 1
    assert traces[0].scope.experiment_id == "e1"

    mock_bq.query.return_value = job
    null_rows = [_mock_row(_event_row(custom_tags={"run": "v9"}, span_id="n"))]
    job2 = MagicMock()
    job2.result.return_value = null_rows
    mock_bq.query.return_value = job2
    traces = client.list_traces(TraceFilter(experiment_id=SQL_NULL))
    assert len(traces) == 1
    assert traces[0].scope.experiment_id is None

  def test_mixed_pins_require_one_joint_scope(self):
    # P1-2: a signature matching scope A plus a label matching scope
    # B is no match — the conjunction must hold on ONE scope.
    sig_a = TraceScope(custom_labels={"a\\": "x"}).scope_signature
    candidate_batch = [
        _mock_row(_candidate_row(tag_payload='{"a\\\\": "x"}')),
        _mock_row(_candidate_row(tag_payload='{"b\\\\": "y"}')),
    ]
    identity_batch = [
        _mock_row(
            {
                "session_id": "sess-1",
                "user_id": "alice",
                "root_agent_name": None,
            }
        )
    ]
    fetch_batch = [
        _mock_row(_event_row(custom_tags={"a\\": "x"}, span_id="p0")),
        _mock_row(_event_row(custom_tags={"b\\": "y"}, span_id="p1")),
    ]
    client, _ = self._client([candidate_batch, identity_batch, fetch_batch])
    with pytest.raises(ValueError, match="No candidates match"):
      client.get_session_trace(
          "sess-1",
          scope_signature=sig_a,
          custom_labels={"b\\": "y"},
          allow_mixed_scope=True,
      )

  def test_sql_null_experiment_listing_preserves_context(self):
    # P1-4: an identity with a real e1 pass plus an untagged shared
    # NULL row must NOT yield a manufactured empty NULL scope under
    # an SQL_NULL experiment filter.
    from unittest.mock import MagicMock

    from bigquery_agent_analytics.trace import SQL_NULL

    rows = [
        _mock_row(_event_row(experiment_id="e1", span_id="e1row")),
        _mock_row(_event_row(span_id="sharednull")),
    ]
    mock_bq = MagicMock()
    job = MagicMock()
    job.result.return_value = rows
    mock_bq.query.return_value = job
    client = Client(
        project_id="proj",
        dataset_id="ds",
        verify_schema=False,
        bq_client=mock_bq,
    )
    traces = client.list_traces(TraceFilter(experiment_id=SQL_NULL))
    assert traces == []
    # And the row scope no longer erases the e1 context.
    query = mock_bq.query.call_args[0][0]
    assert "'$.experiment_id') IS NULL\\nORDER" not in query

  def test_boundary_identity_excluded_from_truncated_page(self):
    # P1-5: the boundary identity's partial rows must not classify
    # into retry candidates; here bob's page-straddling identity is
    # excluded while alice's complete candidates raise ambiguity.
    from bigquery_agent_analytics.client import _MAX_SCOPE_CANDIDATES

    candidate_batch = [
        _mock_row(
            _candidate_row(user_id="alice", tag_payload=f'{{"k": "v{i}"}}')
        )
        for i in range(_MAX_SCOPE_CANDIDATES - 1)
    ] + [
        # Boundary identity: only its shared NULL row made the page.
        _mock_row(_candidate_row(user_id="bob", tag_payload=None)),
        _mock_row(_candidate_row(user_id="bob", tag_payload=None)),
    ]
    client, _ = self._client([candidate_batch])
    with pytest.raises(AmbiguousSessionError) as exc_info:
      client.get_session_trace("sess-1")
    users = {
        c["selector"]["user_id"] for c in exc_info.value.to_dict()["candidates"]
    }
    assert users == {"alice"}  # bob's partial identity excluded
    assert exc_info.value.population_truncated is True

  def test_identity_shared_pool_not_expanded_across_experiments(self):
    # P1-6: untagged NULL-experiment rows are attached only to
    # retained slots; with max_traces=1 over many experiments the
    # shared row appears once, in the single retained trace.
    from datetime import timedelta

    rows = [
        _mock_row(
            dict(
                _event_row(experiment_id=f"e{i}", span_id=f"s{i}"),
                timestamp=_TS + timedelta(minutes=i),
            )
        )
        for i in range(5)
    ] + [_mock_row(_event_row(span_id="shared"))]
    bounded = _build_traces_from_rows(rows, max_traces=1)
    assert len(bounded) == 1
    assert bounded[0].scope.experiment_id == "e4"
    assert [s.span_id for s in bounded[0].spans] == ["s4", "shared"]

  def test_shared_id_not_reused_across_experiments(self):
    # P2-8: e1 and e2 must not both inherit a shared row's id.
    rows = [
        _mock_row(
            dict(_event_row(experiment_id="e1", span_id="a"), trace_id=None)
        ),
        _mock_row(
            dict(_event_row(experiment_id="e2", span_id="b"), trace_id=None)
        ),
        _mock_row(_event_row(span_id="shared", trace_id="tr-e1")),
    ]
    traces = _build_traces_from_rows(rows)
    ids = {t.scope.experiment_id: t.trace_id for t in traces}
    assert ids == {"e1": "sess-1", "e2": "sess-1"}

  def test_event_types_boundary_validation(self):
    # P2-9: hostile iterables are rejected closed at the boundary.
    import collections

    with pytest.raises(TypeError, match="list of strings"):
      TraceFilter(event_types=collections.UserList(["A"]))
    with pytest.raises(TypeError, match="entries must be strings"):
      TraceFilter(event_types=["A", 1])
    filt = TraceFilter(event_types=("A",))
    assert filt.event_types == ["A"]

  def test_signature_pin_verified_beyond_coverage_bound(self):
    # P2-10 (round 6) + P1-1 flag exactness (round 7): when the
    # candidate page misses the target scope entirely, the mixed
    # read's conjunctive verification over the MATERIALIZED fetched
    # scopes still finds it, even though coverage metadata is omitted
    # for size.
    from bigquery_agent_analytics.client import _MAX_SCOPE_CANDIDATES

    n = _MAX_SCOPE_CANDIDATES + 1
    target_sig = TraceScope(custom_labels={"zz": "target"}).scope_signature
    # Page contains only k-scopes; the zz-scope sorts beyond it.
    candidate_batch = [
        _mock_row(_candidate_row(tag_payload=f'{{"k": "v{i}"}}'))
        for i in range(n)
    ]
    identity_batch = [
        _mock_row(
            {
                "session_id": "sess-1",
                "user_id": "alice",
                "root_agent_name": None,
            }
        )
    ]
    fetch_batch = [
        _mock_row(_event_row(custom_tags={"k": f"v{i}"}, span_id=f"s{i}"))
        for i in range(n)
    ] + [_mock_row(_event_row(custom_tags={"zz": "target"}, span_id="tz"))]
    client, _ = self._client([candidate_batch, identity_batch, fetch_batch])
    trace = client.get_session_trace(
        "sess-1", scope_signature=target_sig, allow_mixed_scope=True
    )
    assert trace.scope_coverage is None  # metadata omitted for size
    assert trace.identity.user_id == "alice"  # but the read verified

  def test_flag_with_unique_truncated_match_resolves_exactly(self):
    # Round 7 P1-1: exact signature + allow_mixed_scope returns THE
    # selected scope, never a mixed trace of sibling scopes.
    from bigquery_agent_analytics.client import _MAX_SCOPE_CANDIDATES

    n = _MAX_SCOPE_CANDIDATES + 1
    target_sig = TraceScope(custom_labels={"k": "v0"}).scope_signature
    candidate_batch = [
        _mock_row(_candidate_row(tag_payload=f'{{"k": "v{i}"}}'))
        for i in range(n)
    ]
    # Round 9 P1-1: the truncated singleton is returned only after
    # the bounded identity page proves intrinsic-identity uniqueness.
    identity_batch = [
        _mock_row(
            {
                "session_id": "sess-1",
                "user_id": "alice",
                "root_agent_name": None,
            }
        )
    ]
    fetch_batch = [
        _mock_row(_event_row(custom_tags={"k": "v0"}, span_id="p0")),
    ]
    client, _ = self._client([candidate_batch, identity_batch, fetch_batch])
    trace = client.get_session_trace(
        "sess-1", scope_signature=target_sig, allow_mixed_scope=True
    )
    assert trace.scope is not None
    assert trace.scope.labels_dict == {"k": "v0"}
    assert [s.span_id for s in trace.spans] == ["p0"]


class TestRound7Regressions:
  """PR #371 review round 7 reproduced findings."""

  def _client(self, batches):
    from unittest.mock import MagicMock

    mock_bq = MagicMock()
    jobs = []
    for batch in batches:
      job = MagicMock()
      job.result.return_value = batch
      jobs.append(job)
    mock_bq.query.side_effect = jobs
    return (
        Client(
            project_id="proj",
            dataset_id="ds",
            verify_schema=False,
            bq_client=mock_bq,
        ),
        mock_bq,
    )

  def test_single_identity_truncated_page_proves_ambiguity(self):
    # P1-1: 65 tagged scopes under ONE identity — the tagged rows
    # independently prove their scopes, so the typed ambiguity fires
    # (marked truncated) instead of a plain ValueError.
    from bigquery_agent_analytics.client import _MAX_SCOPE_CANDIDATES

    candidate_batch = [
        _mock_row(_candidate_row(tag_payload=f'{{"k": "v{i}"}}'))
        for i in range(_MAX_SCOPE_CANDIDATES + 1)
    ]
    client, _ = self._client([candidate_batch])
    with pytest.raises(AmbiguousSessionError) as exc_info:
      client.get_session_trace("sess-1")
    assert exc_info.value.population_truncated is True
    assert len(exc_info.value.candidates) >= 2

  def test_boundary_key_canonicalized(self):
    # P1-2: raw missing vs explicit-JSON-null root encodings are ONE
    # semantic identity; the boundary exclusion must not split them
    # and fabricate an empty-scope retry for the partial half.
    from bigquery_agent_analytics.client import _MAX_SCOPE_CANDIDATES

    candidate_batch = [
        _mock_row(
            _candidate_row(user_id="alice", tag_payload=f'{{"k": "v{i}"}}')
        )
        for i in range(_MAX_SCOPE_CANDIDATES - 1)
    ] + [
        # Bob's identity split across encodings: raw missing root on
        # one row, explicit JSON null on the boundary row. Both are
        # untagged — context-dependent — and must BOTH be excluded.
        _mock_row(
            {
                "session_id": "sess-1",
                "user_id": "bob",
                "root_agent_name": None,
                "experiment_id": '"e1"',
                "tag_payload": None,
                "row_count": 1,
            }
        ),
        _mock_row(
            {
                "session_id": "sess-1",
                "user_id": "bob",
                "root_agent_name": "null",
                "experiment_id": None,
                "tag_payload": None,
                "row_count": 1,
            }
        ),
    ]
    client, _ = self._client([candidate_batch])
    with pytest.raises(AmbiguousSessionError) as exc_info:
      client.get_session_trace("sess-1")
    payload = exc_info.value.to_dict()
    bob_candidates = [
        c for c in payload["candidates"] if c["selector"]["user_id"] == "bob"
    ]
    # Round 9 P1-2 refines round 7: bob's COMPLETE e1 experiment
    # group is proven and stays a real retry, while the split NULL
    # context still must not fabricate an empty-scope candidate.
    assert [c["selector"]["experiment_id"] for c in bob_candidates] == ["e1"]

  def test_flag_with_unique_match_returns_selected_scope(self):
    # P1-1 second shape: exact signature + allow_mixed_scope under
    # truncation returns THE selected scope (covered above in
    # test_flag_with_unique_truncated_match_resolves_exactly); here
    # the non-truncated variant sanity-checks precedence.
    target_sig = TraceScope(custom_labels={"k": "v0"}).scope_signature
    candidate_batch = [
        _mock_row(_candidate_row(tag_payload='{"k": "v0"}')),
        _mock_row(_candidate_row(tag_payload='{"k": "v1"}')),
    ]
    fetch_batch = [
        _mock_row(_event_row(custom_tags={"k": "v0"}, span_id="p0")),
    ]
    client, _ = self._client([candidate_batch, fetch_batch])
    trace = client.get_session_trace(
        "sess-1", scope_signature=target_sig, allow_mixed_scope=True
    )
    assert trace.scope.labels_dict == {"k": "v0"}

  def test_singular_fetch_materializes_only_resolved_scope(self):
    # P1-4: sibling scopes admitted by the row fetch are not built —
    # shared rows in the returned trace are original objects (single
    # retained destination means no deep copies were made).
    candidate_batch = [
        _mock_row(_candidate_row(tag_payload='{"run": "v0"}')),
    ]
    fetch_batch = [
        _mock_row(_event_row(custom_tags={"run": "v0"}, span_id="p0")),
        _mock_row(_event_row(span_id="shared")),
    ]
    client, _ = self._client([candidate_batch, fetch_batch])
    trace = client.get_session_trace("sess-1")
    assert {s.span_id for s in trace.spans} == {"p0", "shared"}
    shared = next(s for s in trace.spans if s.span_id == "shared")
    assert not shared.children  # untouched original

  def test_prefiltered_read_does_not_trust_shared_id(self):
    # P1-5: a scope-prefiltered singular read must not inherit a
    # shared row's trace id — the invisible sibling may own it.
    candidate_batch = [
        _mock_row(_candidate_row(tag_payload='{"run": "v0"}')),
        _mock_row(_candidate_row(tag_payload='{"run": "v1"}')),
    ]
    fetch_batch = [
        _mock_row(
            dict(
                _event_row(custom_tags={"run": "v0"}, span_id="p0"),
                trace_id=None,
            )
        ),
        _mock_row(_event_row(span_id="shared", trace_id="tr-sibling")),
    ]
    client, _ = self._client([candidate_batch, fetch_batch])
    trace = client.get_session_trace("sess-1", custom_labels={"run": "v0"})
    assert trace.trace_id == "sess-1"  # session fallback, not tr-sibling

  def test_non_object_attributes_fail_closed(self):
    # P2: a falsey non-object attributes value ([]) must not become
    # a valid empty scope.
    row = _event_row(span_id="bad")
    row["attributes"] = "[]"
    with pytest.raises(ValueError, match="JSON object"):
      _build_traces_from_rows([_mock_row(row)])

  def test_list_query_has_full_tie_breakers(self):
    from bigquery_agent_analytics.client import _LIST_TRACES_QUERY

    assert "e.span_id, e.invocation_id," in _LIST_TRACES_QUERY
    assert "e.event_type" in _LIST_TRACES_QUERY

  def test_scope_filtered_listing_escalates_anchor_limit(self):
    # P1-3: when the first page's identities all classify to invalid
    # scopes, the anchor limit escalates instead of starving results.
    from unittest.mock import MagicMock

    from bigquery_agent_analytics.trace import SQL_NULL

    # First attempt: only an e1 identity with a shared NULL row
    # (manufactured NULL scope rejected). Second attempt: includes
    # the older genuine NULL-scope identity.
    first_rows = [
        _mock_row(_event_row(experiment_id="e1", span_id="a")),
        _mock_row(_event_row(span_id="sharednull")),
    ]
    second_rows = first_rows + [
        _mock_row(
            _event_row(
                session_id="older",
                custom_tags={"run": "v9"},
                span_id="genuine",
            )
        ),
    ]
    mock_bq = MagicMock()
    jobs = []
    for batch in [first_rows, second_rows]:
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
    traces = client.list_traces(TraceFilter(experiment_id=SQL_NULL, limit=1))
    assert len(traces) == 1
    assert traces[0].session_id == "older"
    assert mock_bq.query.call_count == 2
    second_params = {
        p.name: p.value
        for p in mock_bq.query.call_args[1]["job_config"].query_parameters
    }
    assert second_params["trace_limit"] == 8  # escalated from 1


class TestRound8Regressions:
  """PR #371 review round 8 reproduced findings."""

  def _client(self, batches):
    from unittest.mock import MagicMock

    mock_bq = MagicMock()
    jobs = []
    for batch in batches:
      job = MagicMock()
      job.result.return_value = batch
      jobs.append(job)
    mock_bq.query.side_effect = jobs
    return (
        Client(
            project_id="proj",
            dataset_id="ds",
            verify_schema=False,
            bq_client=mock_bq,
        ),
        mock_bq,
    )

  def test_discovery_queries_fold_explicit_null_encoding(self):
    # P1-1: every discovery query folds the explicit-JSON-null
    # encoding into raw-missing AT THE SOURCE, so one semantic
    # identity is one contiguous run of the ordered page.
    from bigquery_agent_analytics.client import _RESOLVE_CANDIDATES_BATCH_QUERY
    from bigquery_agent_analytics.client import _RESOLVE_SESSION_CANDIDATES_QUERY
    from bigquery_agent_analytics.client import _RESOLVE_SESSION_IDENTITIES_QUERY

    for query in (
        _RESOLVE_SESSION_CANDIDATES_QUERY,
        _RESOLVE_CANDIDATES_BATCH_QUERY,
    ):
      normalized_query = " ".join(query.split())
      for attr in ("root_agent_name", "experiment_id", "custom_tags"):
        assert f"JSON_QUERY(attributes, '$.{attr}')" in query
        assert (
            f"TO_JSON_STRING(JSON_QUERY(attributes, '$.{attr}'))"
            in normalized_query
        )
      assert query.count("NULLIF(") == 3
    assert "NULLIF(" in _RESOLVE_SESSION_IDENTITIES_QUERY
    # The batch window runs over a plain subquery (never SELECT-list
    # aliases of the grouped query) and QUALIFY has its required
    # sibling clause.
    assert "SELECT * FROM (" in _RESOLVE_CANDIDATES_BATCH_QUERY
    assert "WHERE TRUE" in _RESOLVE_CANDIDATES_BATCH_QUERY
    assert (
        "PARTITION BY user_id, root_agent_name"
        in _RESOLVE_CANDIDATES_BATCH_QUERY
    )

  def test_truncated_page_cannot_manufacture_phantom_scope(self):
    # P1-1: with canonical encodings a non-boundary identity's rows
    # are contiguous and fully classified — its untagged shared row
    # must NOT surface as a phantom sole-empty-scope candidate.
    identity_a_rows = [
        _mock_row(_candidate_row(user_id="u1")),  # untagged NULL-exp
        _mock_row(_candidate_row(user_id="u1", experiment_id="E1")),
    ]
    boundary_rows = [
        _mock_row(
            _candidate_row(
                user_id="u1",
                root_agent_name="m",
                tag_payload='{"run": "v%d"}' % i,
            )
        )
        for i in range(63)
    ]
    client, _ = self._client([identity_a_rows + boundary_rows])
    with pytest.raises(AmbiguousSessionError) as exc_info:
      client.get_session_trace("sess-1")
    a_candidates = [
        c
        for c in exc_info.value.candidates
        if c.identity.user_id == "u1" and c.identity.root_agent_name is None
    ]
    # Identity A classifies completely: its untagged row is shared
    # infrastructure for the E1 pass, never an empty-scope phantom.
    assert [c.scope.experiment_id for c in a_candidates] == ["E1"]

  def test_api_judge_escalates_anchor_limit(self):
    # P1-2: the LLM-judge API fallback shares the escalating fetch,
    # so a scope-filtered evaluation set cannot silently starve.
    import dataclasses as dc

    from bigquery_agent_analytics.evaluators import SessionScore
    from bigquery_agent_analytics.trace import SQL_NULL

    first_rows = [
        _mock_row(_event_row(experiment_id="e1", span_id="a")),
        _mock_row(_event_row(span_id="sharednull")),
    ]
    second_rows = first_rows + [
        _mock_row(
            _event_row(
                session_id="older",
                custom_tags={"run": "v9"},
                span_id="genuine",
            )
        ),
    ]
    client, mock_bq = self._client([first_rows, second_rows])

    class _StubJudge:
      name = "stub"

      async def evaluate_session(self, trace_text, final):
        return SessionScore(
            session_id="pending", scores={"q": 1.0}, passed=True
        )

    from google.cloud import bigquery as bq

    filt = TraceFilter(experiment_id=SQL_NULL, limit=1)
    report = client._api_judge(
        _StubJudge(),
        "tbl",
        "TRUE",
        [bq.ScalarQueryParameter("trace_limit", "INT64", 1)],
        row_where=filt.row_scope_where(),
        limit=1,
        trace_filter=filt,
    )
    assert mock_bq.query.call_count == 2
    assert report.total_sessions == 1
    assert report.session_scores[0].session_id == "older"

  def test_experiment_only_subgroups_keep_local_trace_ids(self):
    # P2-3: a session tagged only via experiment_id keeps each
    # pass's genuine trace id — the local pool is judged at subgroup
    # granularity, not the identity-level slot census.
    rows = [
        _mock_row(_event_row(experiment_id="A", trace_id="TA", span_id="a1")),
        _mock_row(_event_row(experiment_id="A", trace_id="TA", span_id="a2")),
        _mock_row(_event_row(experiment_id="B", trace_id="TB", span_id="b1")),
    ]
    traces = _build_traces_from_rows([dict(r.items()) for r in rows])
    by_exp = {t.scope.experiment_id: t.trace_id for t in traces}
    assert by_exp == {"A": "TA", "B": "TB"}

  def test_local_shared_id_untrusted_with_sibling_payload_scopes(self):
    # P2-3 soundness bound: a subgroup-local untagged row with two
    # sibling payload scopes could belong to either — fall back.
    rows = [
        dict(
            _event_row(
                experiment_id="A",
                custom_tags={"r": "1"},
                trace_id=None,
                span_id="p1",
            )
        ),
        dict(
            _event_row(
                experiment_id="A",
                custom_tags={"r": "2"},
                trace_id=None,
                span_id="p2",
            )
        ),
        dict(_event_row(experiment_id="A", trace_id="TL", span_id="sh")),
    ]
    traces = _build_traces_from_rows(rows)
    assert sorted(t.trace_id for t in traces) == ["sess-1", "sess-1"]

  def test_mixed_read_rejects_non_object_attributes(self):
    # P2-2: the mixed-scope read applies the same fail-closed guard
    # as every sibling path — no silent [] scope, no AttributeError.
    import json as json_mod

    for bad_attributes in ("[]", '["x"]'):
      identity_batch = [
          _mock_row(
              {
                  "session_id": "sess-1",
                  "user_id": "alice",
                  "root_agent_name": None,
              }
          )
      ]
      good = _event_row(span_id="ok")
      bad = _event_row(span_id="bad")
      bad["attributes"] = bad_attributes
      client, _ = self._client(
          [identity_batch, [_mock_row(good), _mock_row(bad)]]
      )
      with pytest.raises(ValueError, match="JSON object"):
        client._fetch_mixed_scope_trace(TraceSelector(session_id="sess-1"))

  def test_event_types_renormalized_at_sql_boundary(self):
    # P2-5: a vars()-injected lying container can neither erase the
    # event_type predicate nor rewrite the emitted parameter.
    class _FalseyList(list):

      def __bool__(self):
        return False

    class _RewritingList(list):

      def __iter__(self):
        return iter(["INNOCUOUS"])

    filt = TraceFilter()
    vars(filt)["event_types"] = _FalseyList(["TOOL_ERROR"])
    where, params = filt.to_sql_conditions()
    assert "event_type IN UNNEST(@event_types)" in where
    by_name = {p.name: p for p in params}
    assert list(by_name["event_types"].values) == ["TOOL_ERROR"]

    filt2 = TraceFilter()
    vars(filt2)["event_types"] = _RewritingList(["TOOL_ERROR"])
    where2, params2 = filt2.to_sql_conditions()
    assert "event_type IN UNNEST(@event_types)" in where2
    by_name2 = {p.name: p for p in params2}
    assert list(by_name2["event_types"].values) == ["TOOL_ERROR"]

  def test_poison_row_quarantines_only_its_identity(self, caplog):
    # P2-6: one malformed row excludes only its own identity from
    # bulk listings; other sessions are unaffected and the exclusion
    # is logged. Singular construction stays fail-closed.
    import logging

    good = _event_row(session_id="s-good", trace_id="TG", span_id="g1")
    bad_valid = _event_row(session_id="s-bad", span_id="b0")
    bad = _event_row(session_id="s-bad", span_id="b1")
    bad["attributes"] = '{"experiment_id": 7}'
    client, _ = self._client(
        [[_mock_row(good), _mock_row(bad_valid), _mock_row(bad)]]
    )
    with caplog.at_level(logging.WARNING):
      traces = client.list_traces(TraceFilter(limit=10))
    assert [t.session_id for t in traces] == ["s-good"]
    assert any("Quarantined" in r.message for r in caplog.records)
    with pytest.raises(ValueError, match="experiment_id"):
      _build_traces_from_rows([dict(bad.items()) for bad in [_mock_row(bad)]])

  def test_session_scores_attributed_and_merged_without_overwrite(self):
    # P2-7: per-scope expanded score rows carry identity/scope
    # attribution, and cross-criterion merging keys on the attributed
    # unit instead of overwriting one pass with another.
    from bigquery_agent_analytics.client import _build_report
    from bigquery_agent_analytics.client import _merge_criterion_reports
    from bigquery_agent_analytics.evaluators import SessionScore

    class _Criterion:
      name = "quality"
      threshold = 0.5

    def _score(sig, value):
      return SessionScore(
          session_id="sess-1",
          scores={"quality": value},
          passed=True,
          details={"scope_signature": sig},
      )

    report = _merge_criterion_reports(
        "judge",
        "ds",
        [_Criterion()],
        [
            (
                _Criterion(),
                _build_report(
                    evaluator_name="judge",
                    dataset="ds",
                    session_scores=[
                        _score("v1:a", 0.9),
                        _score("v1:b", 0.2),
                    ],
                ),
            )
        ],
    )
    assert report.total_sessions == 2
    by_sig = {
        ss.details["scope_signature"]: ss.scores["quality"]
        for ss in report.session_scores
    }
    assert by_sig == {"v1:a": 0.9, "v1:b": 0.2}

  def test_filtered_listing_stops_when_anchors_exhausted(self):
    # P2-8: matches under-filling the limit do not trigger blind
    # re-scans once the page proves the anchors are exhausted.
    rows = [
        _mock_row(_event_row(experiment_id="E", trace_id="T1", span_id="a"))
    ]
    client, mock_bq = self._client([rows, rows, rows])
    traces = client.list_traces(TraceFilter(experiment_id="E", limit=100))
    assert len(traces) == 1
    assert mock_bq.query.call_count == 1

  def test_sql_null_filtered_listing_keeps_sound_shared_trace_id(self):
    # P3-10: a Python-side scope predicate does not blind the slot
    # census — the same trace reports the same id filtered or not.
    from bigquery_agent_analytics.trace import SQL_NULL

    rows = [_mock_row(_event_row(trace_id="TS", span_id="x1"))]
    client, _ = self._client([rows])
    filtered = client.list_traces(TraceFilter(experiment_id=SQL_NULL))
    client2, _ = self._client([rows])
    unfiltered = client2.list_traces(TraceFilter())
    assert [t.trace_id for t in filtered] == ["TS"]
    assert [t.trace_id for t in unfiltered] == ["TS"]

  def test_attributes_path_rejects_double_encoded_custom_tags(self):
    # P3-11: a double-encoded custom_tags string must not advertise
    # a scope that SQL matching and candidate resolution reject.
    with pytest.raises(ValueError, match="double-encoded"):
      _parse_tag_payload('{"run": "v1"}', source="attributes")
    # The resolution encoding still accepts its TO_JSON_STRING form.
    assert _parse_tag_payload('{"run": "v1"}') == {"run": "v1"}

  def test_pinned_no_match_error_names_pins(self):
    # P3-12: pins excluding every row must not report the session as
    # absent.
    client, _ = self._client([[]])
    with pytest.raises(ValueError, match="under the selector's pins"):
      client.get_session_trace("sess-1", user_id="nobody")
    client2, _ = self._client([[]])
    with pytest.raises(ValueError, match="No events found"):
      client2.get_session_trace("sess-1")


class TestRound9Regressions:
  """PR #371 review round 9 reproduced findings."""

  def _client(self, batches):
    from unittest.mock import MagicMock

    mock_bq = MagicMock()
    jobs = []
    for batch in batches:
      job = MagicMock()
      job.result.return_value = batch
      jobs.append(job)
    mock_bq.query.side_effect = jobs
    return (
        Client(
            project_id="proj",
            dataset_id="ds",
            verify_schema=False,
            bq_client=mock_bq,
        ),
        mock_bq,
    )

  def _anchored(self, row):
    row["anchor_user_id"] = row.get("user_id")
    row["anchor_root_agent_name"] = None
    return row

  def test_truncated_singleton_requires_identity_uniqueness(self):
    # P1-1: a truncated page with one matching signature must not
    # bypass cross-identity ambiguity — an unseen identity may carry
    # the same signature beyond the cut.
    from bigquery_agent_analytics.client import _MAX_SCOPE_CANDIDATES

    n = _MAX_SCOPE_CANDIDATES + 1
    target_sig = TraceScope(custom_labels={"k": "v0"}).scope_signature
    candidate_batch = [
        _mock_row(
            _candidate_row(user_id="alice", tag_payload=f'{{"k": "v{i}"}}')
        )
        for i in range(n)
    ]
    two_identities = [
        _mock_row(
            {"session_id": "sess-1", "user_id": u, "root_agent_name": None}
        )
        for u in ("alice", "bob")
    ]
    # The mixed-path fallback re-discovers identities, then resolves
    # per-identity candidates; bob carries the SAME signature.
    batch_candidates = [
        _mock_row(_candidate_row(user_id="alice", tag_payload='{"k": "v0"}')),
        _mock_row(_candidate_row(user_id="bob", tag_payload='{"k": "v0"}')),
    ]
    client, mock_bq = self._client(
        [candidate_batch, two_identities, batch_candidates]
    )
    with pytest.raises(AmbiguousSessionError):
      client.get_session_trace(
          "sess-1", scope_signature=target_sig, allow_mixed_scope=True
      )
    # Round 10, P3-1: the mixed-path fallback reuses the discovery
    # page instead of rerunning the identical query.
    assert mock_bq.query.call_count == 3

  def test_truncated_singleton_with_full_identity_pins_skips_discovery(self):
    # P1-1 companion: a fully pinned intrinsic identity is unique by
    # construction — no identity-discovery query runs.
    from bigquery_agent_analytics.client import _MAX_SCOPE_CANDIDATES

    n = _MAX_SCOPE_CANDIDATES + 1
    target_sig = TraceScope(custom_labels={"k": "v0"}).scope_signature
    candidate_batch = [
        _mock_row(
            _candidate_row(user_id="alice", tag_payload=f'{{"k": "v{i}"}}')
        )
        for i in range(n)
    ]
    fetch_batch = [
        _mock_row(_event_row(custom_tags={"k": "v0"}, span_id="p0")),
    ]
    client, mock_bq = self._client([candidate_batch, fetch_batch])
    trace = client.get_session_trace(
        "sess-1",
        user_id="alice",
        root_agent_name=None,
        scope_signature=target_sig,
        allow_mixed_scope=True,
    )
    assert trace.scope is not None
    assert trace.scope.labels_dict == {"k": "v0"}
    assert mock_bq.query.call_count == 2  # candidates + fetch only

  def test_truncated_experiment_groups_prove_typed_ambiguity(self):
    # P1-2: 65 untagged distinct experiments must surface the 64
    # complete groups as typed retry candidates, not collapse into a
    # generic enumeration-bound ValueError.
    from bigquery_agent_analytics.client import _MAX_SCOPE_CANDIDATES

    n = _MAX_SCOPE_CANDIDATES + 1
    candidate_batch = [
        _mock_row(_candidate_row(experiment_id=f"E{i:03d}")) for i in range(n)
    ]
    client, _ = self._client([candidate_batch])
    with pytest.raises(AmbiguousSessionError) as exc_info:
      client.get_session_trace("sess-1")
    assert exc_info.value.population_truncated
    experiments = [c.scope.experiment_id for c in exc_info.value.candidates]
    assert len(experiments) == _MAX_SCOPE_CANDIDATES
    assert f"E{_MAX_SCOPE_CANDIDATES:03d}" not in experiments  # cut group

  def test_batched_truncated_partition_keeps_complete_groups(self):
    # P1-2 batched path: a truncated per-identity partition retains
    # its proven candidates instead of discarding the identity.
    alice = TraceIdentity(session_id="sess-1", user_id="alice")
    bob = TraceIdentity(session_id="sess-1", user_id="bob")
    # per_identity_limit = max(2, 64 // 2) = 32; 33 rows => truncated.
    rows = [
        _mock_row(_candidate_row(user_id="alice", experiment_id=f"E{i:03d}"))
        for i in range(33)
    ] + [_mock_row(_candidate_row(user_id="bob", experiment_id="B0"))]
    client, _ = self._client([rows])
    candidates, truncated = client._real_candidates_for_identities(
        TraceSelector(session_id="sess-1"), [alice, bob]
    )
    assert truncated
    alice_exps = [
        c.scope.experiment_id
        for c in candidates
        if c.identity.user_id == "alice"
    ]
    assert len(alice_exps) == 32  # complete groups retained
    assert "E032" not in alice_exps  # the cut final group is not
    assert [
        c.scope.experiment_id for c in candidates if c.identity.user_id == "bob"
    ] == ["B0"]

  def test_escalation_reaches_the_documented_ceiling(self):
    # P1-3: with limit=1 the anchor page must keep escalating past 64
    # while pages stay saturated — a match at anchor 65+ is found.
    def page(count, with_match):
      rows = [
          _mock_row(
              self._anchored(
                  _event_row(
                      session_id=f"s{i}",
                      custom_tags={"k": "x"},
                      span_id=f"sp{i}",
                  )
              )
          )
          for i in range(count)
      ]
      if with_match:
        rows.append(
            _mock_row(
                self._anchored(
                    _event_row(
                        session_id="s-match",
                        custom_tags={"k": "v"},
                        span_id="hit",
                    )
                )
            )
        )
      return rows

    client, mock_bq = self._client(
        [page(1, False), page(8, False), page(64, False), page(512, True)]
    )
    traces = client.list_traces(TraceFilter(custom_labels={"k": "v"}, limit=1))
    assert [t.session_id for t in traces] == ["s-match"]
    limits = [
        {p.name: p.value for p in call.kwargs["job_config"].query_parameters}[
            "trace_limit"
        ]
        for call in mock_bq.query.call_args_list
    ]
    assert limits == [1, 8, 64, 512]

  def test_quarantined_anchors_do_not_fake_exhaustion(self):
    # P1-4: two SQL-distinct malformed anchors collapsing into one
    # quarantine key must not end escalation above valid results.
    bad_rows = []
    for i, root in enumerate((7, 8)):
      row = _event_row(session_id="s-bad", span_id=f"b{i}")
      row["attributes"] = f'{{"root_agent_name": {root}, "experiment_id": "E"}}'
      row["anchor_user_id"] = "alice"
      row["anchor_root_agent_name"] = str(root)
      bad_rows.append(_mock_row(row))
    good = _mock_row(
        self._anchored(
            _event_row(session_id="s-good", experiment_id="E", span_id="g1")
        )
    )
    client, mock_bq = self._client([bad_rows, bad_rows + [good]])
    traces = client.list_traces(TraceFilter(experiment_id="E", limit=2))
    assert [t.session_id for t in traces] == ["s-good"]
    assert mock_bq.query.call_count == 2

  def test_json_string_scalar_attributes_never_form_a_scope(self):
    # P1-5: a schema-valid JSON string scalar (decoded to str by the
    # BigQuery client) and a double-encoded object must both fail
    # validation — Span's parse-failure fallback must not launder
    # them into a legitimate empty scope.
    for raw in ("opaque", '"opaque"', '"{\\"a\\": \\"b\\"}"', "[1]"):
      row = _event_row(span_id="sp1")
      row["attributes"] = raw
      with pytest.raises(ValueError, match="JSON object"):
        _build_traces_from_rows([dict(row.items())])
      quarantined = _build_traces_from_rows(
          [dict(row.items())], on_malformed="quarantine"
      )
      assert quarantined == []

  def test_evaluator_details_cannot_spoof_attribution(self):
    # P2-1: reserved attribution keys are assigned from the trace,
    # never defaulted from evaluator-supplied details.
    from google.cloud import bigquery as bq

    from bigquery_agent_analytics.evaluators import SessionScore

    rows = [
        _mock_row(
            self._anchored(
                _event_row(experiment_id="E", trace_id="T1", span_id="a")
            )
        )
    ]
    client, _ = self._client([rows])

    class _SpoofingJudge:
      name = "spoof"

      async def evaluate_session(self, trace_text, final):
        return SessionScore(
            session_id="pending",
            scores={"q": 1.0},
            passed=True,
            details={
                "user_id": "spoof",
                "root_agent_name": "spoof",
                "scope_signature": "spoof",
            },
        )

    report = client._api_judge(
        _SpoofingJudge(),
        "tbl",
        "TRUE",
        [bq.ScalarQueryParameter("trace_limit", "INT64", 1)],
        row_where="TRUE",
        limit=1,
        trace_filter=None,
    )
    details = report.session_scores[0].details
    assert details["user_id"] == "alice"
    assert details["root_agent_name"] is None
    assert details["scope_signature"] == (
        TraceScope(experiment_id="E").scope_signature
    )


class TestRound10Regressions:
  """PR #371 review round 10 reproduced findings."""

  def _client(self, batches):
    from unittest.mock import MagicMock

    mock_bq = MagicMock()
    jobs = []
    for batch in batches:
      job = MagicMock()
      job.result.return_value = batch
      jobs.append(job)
    mock_bq.query.side_effect = jobs
    return (
        Client(
            project_id="proj",
            dataset_id="ds",
            verify_schema=False,
            bq_client=mock_bq,
        ),
        mock_bq,
    )

  def _anchored(self, row):
    row["anchor_user_id"] = row.get("user_id")
    row["anchor_root_agent_name"] = None
    return row

  def test_attested_string_scalar_cannot_fabricate_identity(self):
    # P1-1: the BigQuery decoder hands a JSON STRING scalar over as
    # the decoded inner str; when its text is serialized object
    # syntax a bare reparse accepts it while every SQL path sees a
    # top-level string. The JSON_TYPE attestation is authoritative.
    decoded = '{"root_agent_name": "fabricated", "custom_tags": {"run": "v1"}}'
    row = _event_row(span_id="x1")
    row["attributes"] = decoded
    row["attributes_type"] = "string"
    with pytest.raises(ValueError, match="JSON object"):
      _build_traces_from_rows([dict(row.items())])
    assert (
        _build_traces_from_rows([dict(row.items())], on_malformed="quarantine")
        == []
    )
    # The attestation accepts real objects and JSON null.
    good = _event_row(span_id="g1", custom_tags={"run": "v1"})
    good["attributes_type"] = "object"
    built = _build_traces_from_rows([dict(good.items())])
    assert built[0].scope.labels_dict == {"run": "v1"}
    null_attrs = _event_row(span_id="n1")
    null_attrs["attributes"] = None
    null_attrs["attributes_type"] = None
    assert len(_build_traces_from_rows([dict(null_attrs.items())])) == 1
    # Rows without the attestation keep the round-9 fallback: object
    # text parses, non-object text fails.
    legacy = _event_row(span_id="l1", custom_tags={"run": "v1"})
    assert len(_build_traces_from_rows([dict(legacy.items())])) == 1

  def test_list_query_projects_attributes_type(self):
    from bigquery_agent_analytics.client import _GET_SESSION_TRACE_QUERY
    from bigquery_agent_analytics.client import _LIST_TRACES_QUERY

    for query in (_LIST_TRACES_QUERY, _GET_SESSION_TRACE_QUERY):
      assert "JSON_TYPE(e.attributes) AS attributes_type" in query

  def test_truncated_singleton_without_signature_uses_mixed_read(self):
    # P1-2: full identity pins prove identity uniqueness, not scope
    # uniqueness — a Python-only subset pin (unaddressable label key)
    # with one VISIBLE match may match another scope beyond the cut,
    # so without a scope_signature the flag's promise is the
    # conversation-complete mixed read.
    from bigquery_agent_analytics.client import _MAX_SCOPE_CANDIDATES

    n = _MAX_SCOPE_CANDIDATES + 1
    unaddressable = "k\\"  # odd trailing backslash: Python-side pin
    candidate_batch = [
        _mock_row(
            _candidate_row(
                user_id="alice",
                tag_payload=(
                    '{"k\\\\": "v"}' if i == 0 else f'{{"j": "v{i}"}}'
                ),
            )
        )
        for i in range(n)
    ]
    identity_batch = [
        _mock_row(
            {
                "session_id": "sess-1",
                "user_id": "alice",
                "root_agent_name": None,
            }
        )
    ]
    fetch_batch = [
        _mock_row(_event_row(custom_tags={unaddressable: "v"}, span_id="m0")),
        _mock_row(_event_row(custom_tags={"j": "v1"}, span_id="m1")),
    ]
    client, mock_bq = self._client(
        [candidate_batch, identity_batch, fetch_batch]
    )
    trace = client.get_session_trace(
        "sess-1",
        user_id="alice",
        root_agent_name=None,
        custom_labels={unaddressable: "v"},
        allow_mixed_scope=True,
    )
    # Conversation-complete mixed read, not the first visible match.
    assert trace.scope is None
    assert [s.span_id for s in trace.spans] == ["m0", "m1"]

  def test_quarantined_newest_anchor_does_not_starve_listing(self):
    # P1-3: an unfiltered listing must refill past a quarantined
    # newest anchor instead of returning [] after one query.
    bad = _event_row(session_id="s-bad", span_id="b1")
    bad["attributes"] = '{"experiment_id": 7}'
    bad = self._anchored(bad)
    good = self._anchored(
        _event_row(session_id="s-old", trace_id="TG", span_id="g1")
    )
    client, mock_bq = self._client(
        [[_mock_row(bad)], [_mock_row(bad), _mock_row(good)]]
    )
    traces = client.list_traces(TraceFilter(limit=1))
    assert [t.session_id for t in traces] == ["s-old"]
    assert mock_bq.query.call_count == 2


class TestRound11Regressions:
  """PR #371 review round 11 reproduced findings."""

  def _client(self, batches):
    mock_bq = MagicMock()
    jobs = []
    for batch in batches:
      job = MagicMock()
      job.result.return_value = batch
      jobs.append(job)
    mock_bq.query.side_effect = jobs
    return (
        Client(
            project_id="proj",
            dataset_id="ds",
            verify_schema=False,
            bq_client=mock_bq,
        ),
        mock_bq,
    )

  @staticmethod
  def _discovery_row(*, valid, **kwargs):
    row = _candidate_row(**kwargs)
    row["attributes_valid"] = valid
    return _mock_row(row)

  def test_discovery_attestation_precedes_every_enumeration_limit(self):
    for query, limit_marker in (
        (_RESOLVE_SESSION_CANDIDATES_QUERY, "LIMIT @candidate_limit"),
        (_RESOLVE_SESSION_IDENTITIES_QUERY, "LIMIT @identity_limit"),
    ):
      assert "JSON_TYPE(attributes)" in query
      assert "attributes_valid" in query
      assert query.index("ORDER BY attributes_valid DESC") < query.index(
          limit_marker
      )

    batch_window_start = _RESOLVE_CANDIDATES_BATCH_QUERY.index(
        "QUALIFY ROW_NUMBER() OVER"
    )
    batch_window_end = _RESOLVE_CANDIDATES_BATCH_QUERY.index(
        ") <= @per_identity_capped", batch_window_start
    )
    assert (
        "ORDER BY attributes_valid DESC"
        in _RESOLVE_CANDIDATES_BATCH_QUERY[batch_window_start:batch_window_end]
    )

  def test_malformed_discovery_row_cannot_create_public_candidate(self):
    candidates = [
        self._discovery_row(
            valid=True, user_id="alice", tag_payload='{"run": "v1"}'
        ),
        self._discovery_row(valid=False, user_id="mallory"),
    ]
    fetched = [
        _mock_row(
            _event_row(user_id="alice", custom_tags={"run": "v1"}, span_id="ok")
        )
    ]
    client, mock_bq = self._client([candidates, fetched])

    trace = client.get_session_trace("sess-1")

    assert trace.identity == TraceIdentity(session_id="sess-1", user_id="alice")
    assert [span.span_id for span in trace.spans] == ["ok"]
    assert mock_bq.query.call_count == 2

  def test_malformed_only_discovery_fails_before_fetch(self):
    candidates = [self._discovery_row(valid=False, user_id="mallory")]
    raw = _event_row(user_id="mallory", span_id="bad")
    raw["attributes"] = '{"root_agent_name": "fabricated"}'
    raw["attributes_type"] = "string"
    client, mock_bq = self._client([candidates, [_mock_row(raw)]])

    with pytest.raises(ValueError, match="JSON object"):
      client.get_session_trace("sess-1")

    assert mock_bq.query.call_count == 1

  def test_malformed_identity_does_not_join_mixed_read_population(self):
    identity_rows = [
        self._discovery_row(valid=True, user_id="alice"),
        self._discovery_row(valid=False, user_id="mallory"),
    ]
    client, _ = self._client([identity_rows])

    identities, truncated = client._discover_session_identities(
        TraceSelector(session_id="sess-1")
    )

    assert identities == [TraceIdentity(session_id="sess-1", user_id="alice")]
    assert not truncated

  def test_malformed_batched_row_cannot_create_retry_candidate(self):
    alice = TraceIdentity(session_id="sess-1", user_id="alice")
    bob = TraceIdentity(session_id="sess-1", user_id="bob")
    rows = [
        self._discovery_row(
            valid=True, user_id="alice", tag_payload='{"run": "v1"}'
        ),
        self._discovery_row(valid=False, user_id="bob"),
    ]
    client, _ = self._client([rows])

    candidates, truncated = client._real_candidates_for_identities(
        TraceSelector(session_id="sess-1"), [alice, bob]
    )

    assert [candidate.identity for candidate in candidates] == [alice]
    assert not truncated

  def test_attested_malformed_row_quarantines_only_its_sql_anchor(self):
    malformed = _event_row(session_id="shared", user_id="alice", span_id="bad")
    malformed["attributes"] = "opaque"
    malformed["attributes_type"] = "string"
    malformed["anchor_user_id"] = "alice"
    malformed["anchor_root_agent_name"] = None
    valid = _event_row(
        session_id="shared",
        user_id="alice",
        root_agent_name="named-root",
        span_id="good",
    )
    valid["attributes_type"] = "object"
    valid["anchor_user_id"] = "alice"
    valid["anchor_root_agent_name"] = "named-root"

    traces = _build_traces_from_rows(
        [_mock_row(malformed), _mock_row(valid)], on_malformed="quarantine"
    )

    assert [trace.identity for trace in traces] == [
        TraceIdentity(
            session_id="shared",
            user_id="alice",
            root_agent_name="named-root",
        )
    ]
    assert [span.span_id for span in traces[0].spans] == ["good"]

  def test_malformed_row_without_sql_anchor_uses_coarse_quarantine(self):
    malformed = _event_row(session_id="shared", user_id="alice", span_id="bad")
    malformed["attributes"] = "opaque"
    malformed["attributes_type"] = "string"
    valid = _event_row(
        session_id="shared",
        user_id="alice",
        root_agent_name="named-root",
        span_id="good",
    )

    traces = _build_traces_from_rows(
        [_mock_row(malformed), _mock_row(valid)], on_malformed="quarantine"
    )

    assert traces == []

  def test_quarantine_uses_projected_anchor_user_independently(self):
    malformed = _event_row(session_id="shared", user_id="alice", span_id="bad")
    malformed["attributes"] = "opaque"
    malformed["attributes_type"] = "string"
    malformed["anchor_user_id"] = "projected-owner"
    malformed["anchor_root_agent_name"] = None
    valid = _event_row(session_id="shared", user_id="alice", span_id="good")
    valid["attributes_type"] = "object"
    valid["anchor_user_id"] = "alice"
    valid["anchor_root_agent_name"] = None

    traces = _build_traces_from_rows(
        [_mock_row(malformed), _mock_row(valid)], on_malformed="quarantine"
    )

    assert [trace.identity for trace in traces] == [
        TraceIdentity(session_id="shared", user_id="alice")
    ]
    assert [span.span_id for span in traces[0].spans] == ["good"]

  def test_invalid_projected_anchor_uses_coarse_quarantine(self):
    malformed = _event_row(session_id="shared", user_id="alice", span_id="bad")
    malformed["attributes"] = "opaque"
    malformed["attributes_type"] = "string"
    malformed["anchor_user_id"] = 7
    malformed["anchor_root_agent_name"] = None
    valid = _event_row(
        session_id="shared",
        user_id="alice",
        root_agent_name="named-root",
        span_id="good",
    )

    traces = _build_traces_from_rows(
        [_mock_row(malformed), _mock_row(valid)], on_malformed="quarantine"
    )

    assert traces == []

  def test_builder_validates_attestation_before_span_json_parsing(self):
    deeply_nested = "[" * 10000 + "0" + "]" * 10000
    malformed = _event_row(session_id="deep", span_id="bad")
    malformed["attributes"] = deeply_nested
    malformed["attributes_type"] = "string"
    malformed["anchor_user_id"] = "alice"
    malformed["anchor_root_agent_name"] = None

    with pytest.raises(ValueError, match="JSON object"):
      _build_traces_from_rows([_mock_row(malformed)])

    assert (
        _build_traces_from_rows(
            [_mock_row(malformed)], on_malformed="quarantine"
        )
        == []
    )

  def test_mixed_read_validates_attestation_before_span_json_parsing(self):
    identity_rows = [
        self._discovery_row(valid=True, user_id="alice"),
    ]
    deeply_nested = "[" * 10000 + "0" + "]" * 10000
    malformed = _event_row(session_id="deep", span_id="bad")
    malformed["attributes"] = deeply_nested
    malformed["attributes_type"] = "string"
    client, _ = self._client([identity_rows, [_mock_row(malformed)]])

    with pytest.raises(ValueError, match="JSON object"):
      client._fetch_mixed_scope_trace(TraceSelector(session_id="sess-1"))

  def test_complete_same_identity_page_reuses_candidates_for_mixed_read(self):
    candidates = [
        self._discovery_row(
            valid=True, user_id="alice", tag_payload='{"run": "v0"}'
        ),
        self._discovery_row(
            valid=True, user_id="alice", tag_payload='{"run": "v1"}'
        ),
    ]
    fetched = [
        _mock_row(_event_row(custom_tags={"run": "v0"}, span_id="v0")),
        _mock_row(_event_row(custom_tags={"run": "v1"}, span_id="v1")),
    ]
    client, mock_bq = self._client([candidates, fetched])

    trace = client.get_session_trace("sess-1", allow_mixed_scope=True)

    assert trace.identity == TraceIdentity(session_id="sess-1", user_id="alice")
    assert trace.scope is None
    assert [span.span_id for span in trace.spans] == ["v0", "v1"]
    assert mock_bq.query.call_count == 2

  def test_complete_cross_identity_page_raises_without_rediscovery(self):
    candidates = [
        self._discovery_row(
            valid=True, user_id="alice", tag_payload='{"run": "v0"}'
        ),
        self._discovery_row(
            valid=True, user_id="bob", tag_payload='{"run": "v0"}'
        ),
    ]
    client, mock_bq = self._client([candidates])

    with pytest.raises(AmbiguousSessionError) as exc_info:
      client.get_session_trace("sess-1", allow_mixed_scope=True)

    assert {
        candidate.identity.user_id for candidate in exc_info.value.candidates
    } == {"alice", "bob"}
    assert not exc_info.value.population_truncated
    assert mock_bq.query.call_count == 1


class TestRound12Regressions:
  """PR #371 review round 12 reproduced findings."""

  def _client(self, batches):
    mock_bq = MagicMock()
    jobs = []
    for batch in batches:
      job = MagicMock()
      job.result.return_value = batch
      jobs.append(job)
    mock_bq.query.side_effect = jobs
    return (
        Client(
            project_id="proj",
            dataset_id="ds",
            verify_schema=False,
            bq_client=mock_bq,
        ),
        mock_bq,
    )

  @staticmethod
  def _attested_candidate(**kwargs):
    row = _candidate_row(**kwargs)
    row["attributes_valid"] = True
    return _mock_row(row)

  @staticmethod
  def _anchored(row):
    row["anchor_user_id"] = row.get("user_id")
    row["anchor_root_agent_name"] = None
    return row

  def test_truncated_ambiguity_payload_retries_exact_scope_by_default(self):
    import json

    from bigquery_agent_analytics.client import _MAX_SCOPE_CANDIDATES

    unaddressable_key = "k\\"
    candidate_page = [
        self._attested_candidate(
            tag_payload=json.dumps({unaddressable_key: f"v{i}"})
        )
        for i in range(_MAX_SCOPE_CANDIDATES + 1)
    ]
    client, _ = self._client([candidate_page])

    with pytest.raises(AmbiguousSessionError) as exc_info:
      client.get_session_trace("sess-1")

    selector_payload = json.loads(
        json.dumps(exc_info.value.to_dict()["candidates"][0]["selector"])
    )
    selected_value = selector_payload["custom_labels"][unaddressable_key]
    fetched = [
        _mock_row(
            _event_row(
                custom_tags={unaddressable_key: selected_value},
                span_id="selected",
            )
        )
    ]
    retry_client, mock_bq = self._client([candidate_page, fetched])

    trace = retry_client.get_trace_by_selector(
        TraceSelector(**selector_payload)
    )

    assert trace.scope is not None
    assert trace.scope.labels_dict == {unaddressable_key: selected_value}
    assert [span.span_id for span in trace.spans] == ["selected"]
    assert mock_bq.query.call_count == 2

  def test_truncated_exact_scope_with_partial_identity_uses_bounded_proof(self):
    import json

    from bigquery_agent_analytics.client import _MAX_SCOPE_CANDIDATES

    unaddressable_key = "k\\"
    candidate_page = [
        self._attested_candidate(
            tag_payload=json.dumps({unaddressable_key: f"v{i}"})
        )
        for i in range(_MAX_SCOPE_CANDIDATES + 1)
    ]
    target_scope = TraceScope(
        custom_labels={unaddressable_key: "v0"}
    ).scope_signature
    identity_page = [
        self._attested_candidate(user_id="alice", tag_payload=None)
    ]
    fetched = [
        _mock_row(
            _event_row(
                custom_tags={unaddressable_key: "v0"},
                span_id="selected",
            )
        )
    ]
    client, mock_bq = self._client([candidate_page, identity_page, fetched])

    trace = client.get_session_trace(
        "sess-1",
        user_id="alice",
        custom_labels={unaddressable_key: "v0"},
        scope_signature=target_scope,
    )

    assert trace.scope is not None
    assert trace.scope.scope_signature == target_scope
    assert [span.span_id for span in trace.spans] == ["selected"]
    assert mock_bq.query.call_count == 3

  def test_singular_fetch_excludes_attested_invalid_attributes(self):
    normalized_query = " ".join(_GET_SESSION_TRACE_QUERY.split())

    assert (
        "COALESCE(JSON_TYPE(e.attributes), 'null') "
        "IN ('object', 'null')" in normalized_query
    )
    assert normalized_query.index(
        "COALESCE(JSON_TYPE(e.attributes), 'null')"
    ) < normalized_query.index("JSON_VALUE(e.attributes, '$.root_agent_name')")

  def test_quarantine_logs_only_identity_groups_actually_removed(self, caplog):
    malformed = self._anchored(
        _event_row(session_id="shared", user_id="alice", span_id="bad")
    )
    malformed["attributes"] = "opaque"
    malformed["attributes_type"] = "string"
    valid_same_group = self._anchored(
        _event_row(session_id="shared", user_id="alice", span_id="valid")
    )
    orphan_malformed = self._anchored(
        _event_row(session_id="orphan", user_id="mallory", span_id="orphan")
    )
    orphan_malformed["attributes"] = "opaque"
    orphan_malformed["attributes_type"] = "string"

    with caplog.at_level("WARNING"):
      traces = _build_traces_from_rows(
          [
              _mock_row(malformed),
              _mock_row(valid_same_group),
              _mock_row(orphan_malformed),
          ],
          on_malformed="quarantine",
      )

    warnings = [
        record.message
        for record in caplog.records
        if record.message.startswith("Quarantined")
    ]
    assert traces == []
    assert len(warnings) == 1
    assert warnings[0].startswith("Quarantined 1 identity group")

  def test_quarantine_does_not_log_when_no_group_was_removed(self, caplog):
    orphan_malformed = self._anchored(
        _event_row(session_id="orphan", user_id="mallory", span_id="orphan")
    )
    orphan_malformed["attributes"] = "opaque"
    orphan_malformed["attributes_type"] = "string"

    with caplog.at_level("WARNING"):
      traces = _build_traces_from_rows(
          [_mock_row(orphan_malformed)],
          on_malformed="quarantine",
      )

    assert traces == []
    assert not [
        record
        for record in caplog.records
        if record.message.startswith("Quarantined")
    ]

  def test_listing_escalation_logs_each_quarantined_group_once(self, caplog):
    bad_valid = self._anchored(
        _event_row(session_id="newest", user_id="alice", span_id="valid")
    )
    bad_malformed = self._anchored(
        _event_row(session_id="newest", user_id="alice", span_id="bad")
    )
    bad_malformed["attributes"] = "opaque"
    bad_malformed["attributes_type"] = "string"
    older = self._anchored(
        _event_row(session_id="older", user_id="bob", span_id="older")
    )
    client, mock_bq = self._client(
        [
            [_mock_row(bad_valid), _mock_row(bad_malformed)],
            [
                _mock_row(bad_valid),
                _mock_row(bad_malformed),
                _mock_row(older),
            ],
        ]
    )

    with caplog.at_level("WARNING"):
      traces = client.list_traces(TraceFilter(limit=1))

    warnings = [
        record.message
        for record in caplog.records
        if record.message.startswith("Quarantined")
    ]
    assert [trace.session_id for trace in traces] == ["older"]
    assert mock_bq.query.call_count == 2
    assert len(warnings) == 1
    assert warnings[0].startswith("Quarantined 1 identity group")

  def test_direct_builder_reports_each_independent_quarantine_call(
      self, caplog
  ):
    valid = self._anchored(
        _event_row(session_id="shared", user_id="alice", span_id="valid")
    )
    malformed = self._anchored(
        _event_row(session_id="shared", user_id="alice", span_id="bad")
    )
    malformed["attributes"] = "opaque"
    malformed["attributes_type"] = "string"
    with caplog.at_level("WARNING"):
      _build_traces_from_rows(
          [_mock_row(valid), _mock_row(malformed)],
          on_malformed="quarantine",
      )
      _build_traces_from_rows(
          [_mock_row(valid), _mock_row(malformed)],
          on_malformed="quarantine",
      )

    assert (
        len(
            [
                record
                for record in caplog.records
                if record.message.startswith("Quarantined 1 identity group")
            ]
        )
        == 2
    )

  def test_invalid_tail_does_not_truncate_candidate_population(self):
    import json

    from bigquery_agent_analytics.client import _MAX_SCOPE_CANDIDATES

    valid = [
        self._attested_candidate(tag_payload=json.dumps({"run": f"v{i}"}))
        for i in range(_MAX_SCOPE_CANDIDATES)
    ]
    invalid_row = _candidate_row(user_id="mallory")
    invalid_row["attributes_valid"] = False
    client, _ = self._client([valid + [_mock_row(invalid_row)]])

    with pytest.raises(AmbiguousSessionError) as exc_info:
      client.get_session_trace("sess-1")

    assert len(exc_info.value.candidates) == _MAX_SCOPE_CANDIDATES
    assert not exc_info.value.population_truncated

  def test_invalid_tail_does_not_truncate_identity_population(self):
    from bigquery_agent_analytics.client import _MAX_IDENTITIES

    valid = [
        self._attested_candidate(user_id=f"user-{i}")
        for i in range(_MAX_IDENTITIES)
    ]
    invalid_row = _candidate_row(user_id="mallory")
    invalid_row["attributes_valid"] = False
    client, _ = self._client([valid + [_mock_row(invalid_row)]])

    identities, truncated = client._discover_session_identities(
        TraceSelector(session_id="sess-1")
    )

    assert len(identities) == _MAX_IDENTITIES
    assert not truncated

  def test_invalid_tail_does_not_truncate_batched_identity_partition(self):
    import json

    from bigquery_agent_analytics.client import _MAX_SCOPE_CANDIDATES

    identity = TraceIdentity(session_id="sess-1", user_id="alice")
    valid = [
        self._attested_candidate(
            user_id="alice", tag_payload=json.dumps({"run": f"v{i}"})
        )
        for i in range(_MAX_SCOPE_CANDIDATES)
    ]
    invalid_row = _candidate_row(user_id="alice")
    invalid_row["attributes_valid"] = False
    client, _ = self._client([valid + [_mock_row(invalid_row)]])

    candidates, truncated = client._real_candidates_for_identities(
        TraceSelector(session_id="sess-1"), [identity]
    )

    assert len(candidates) == _MAX_SCOPE_CANDIDATES
    assert not truncated


class TestRound13Regressions:
  """PR #371 review round 13 reproduced findings."""

  @staticmethod
  def _job(rows):
    job = MagicMock()
    job.result.return_value = rows
    return job

  @staticmethod
  def _anchored(row):
    row["anchor_user_id"] = row.get("user_id")
    row["anchor_root_agent_name"] = None
    return row

  @staticmethod
  def _has_outer_attestation(query):
    normalized = " ".join(query.split())
    return (
        "COALESCE(JSON_TYPE(e.attributes), 'null') "
        "IN ('object', 'null')" in normalized
    )

  @staticmethod
  def _has_cte_attestation(query):
    normalized = " ".join(query.split())
    return (
        "COALESCE(JSON_TYPE(attributes), 'null') "
        "IN ('object', 'null')" in normalized
    )

  def test_public_singular_retry_and_listing_share_attestation_boundary(
      self, caplog
  ):
    import json

    unaddressable_key = "k\\"

    def candidate_rows():
      rows = []
      for value in ("v0", "v1"):
        row = _candidate_row(
            user_id="alice",
            tag_payload=json.dumps({unaddressable_key: value}),
        )
        row["attributes_valid"] = True
        rows.append(_mock_row(row))
      return rows

    def valid_row(value, span_id):
      row = _event_row(
          user_id="alice",
          custom_tags=(
              {unaddressable_key: value} if value is not None else None
          ),
          span_id=span_id,
      )
      row["attributes_type"] = "object"
      return row

    def poison_row():
      row = _event_row(user_id="alice", span_id="x1")
      row["attributes"] = "opaque"
      row["attributes_type"] = "string"
      return row

    valid_fetch = valid_row("v0", "selected")
    valid_listing = [
        self._anchored(valid_row(None, "shared")),
        self._anchored(valid_row("v0", "v0")),
        self._anchored(valid_row("v1", "v1")),
    ]
    poison = self._anchored(poison_row())
    queries = []

    def query_side_effect(query, **_kwargs):
      queries.append(query)
      if "COUNT(*) AS row_count" in query:
        return self._job(candidate_rows())
      if "WITH trace_sessions AS" in query:
        # This fake models BigQuery's expanded-row WHERE: when the
        # outer attestation is absent, the same-anchor scalar is
        # re-admitted and Python must quarantine the whole identity.
        rows = (
            valid_listing
            if self._has_outer_attestation(query)
            else [*valid_listing, poison]
        )
        return self._job([_mock_row(dict(row)) for row in rows])
      # Singular fetch parity: round 12 already added this predicate;
      # keep the public retry behavioral instead of substring-only.
      rows = (
          [valid_fetch]
          if self._has_outer_attestation(query)
          else [valid_fetch, poison]
      )
      return self._job([_mock_row(dict(row)) for row in rows])

    mock_bq = MagicMock()
    mock_bq.query.side_effect = query_side_effect
    client = Client(
        project_id="proj",
        dataset_id="ds",
        verify_schema=False,
        bq_client=mock_bq,
    )
    target_scope = TraceScope(
        custom_labels={unaddressable_key: "v0"}
    ).scope_signature
    selector = TraceSelector(
        session_id="sess-1",
        user_id="alice",
        root_agent_name=None,
        custom_labels={unaddressable_key: "v0"},
        scope_signature=target_scope,
    )

    with caplog.at_level("WARNING"):
      retry = client.get_trace_by_selector(selector)
      listed = client.list_traces(TraceFilter(limit=10))

    assert [span.span_id for span in retry.spans] == ["selected"]
    fetch_query = queries[1]
    assert self._has_outer_attestation(fetch_query)
    assert {trace.scope.labels_dict["k\\"] for trace in listed} == {
        "v0",
        "v1",
    }
    assert all(
        "x1" not in {span.span_id for span in trace.spans} for trace in listed
    )
    assert not [
        record
        for record in caplog.records
        if record.message.startswith("Quarantined")
    ]

  def test_listing_attestations_precede_both_enumeration_boundaries(self):
    normalized = " ".join(_LIST_TRACES_QUERY.split())
    cte_attestation = normalized.index(
        "COALESCE(JSON_TYPE(attributes), 'null') IN ('object', 'null')"
    )
    cte_where = normalized.index("{where}")
    group_by = normalized.index("GROUP BY session_id, user_id, root_agent_name")
    anchor_limit = normalized.index("LIMIT @trace_limit")
    outer_attestation = normalized.index(
        "COALESCE(JSON_TYPE(e.attributes), 'null') IN ('object', 'null')"
    )
    row_where = normalized.index("{row_where}")

    assert cte_attestation < cte_where < group_by < anchor_limit
    assert outer_attestation < row_where

  def test_poison_only_newest_anchor_does_not_starve_listing(self):
    poison = self._anchored(
        _event_row(session_id="newest-poison", user_id="mallory", span_id="x1")
    )
    poison["attributes"] = "opaque"
    poison["attributes_type"] = "string"
    older = self._anchored(
        _event_row(session_id="older-valid", user_id="alice", span_id="good")
    )
    older["attributes_type"] = "object"

    def query_side_effect(query, **_kwargs):
      # This models the anchor CTE's WHERE-before-LIMIT behavior.
      # Mocks cannot execute BigQuery SQL, so the selected page
      # depends explicitly on whether the production query carries
      # the CTE attestation.
      selected = older if self._has_cte_attestation(query) else poison
      return self._job([_mock_row(dict(selected))])

    mock_bq = MagicMock()
    mock_bq.query.side_effect = query_side_effect
    client = Client(
        project_id="proj",
        dataset_id="ds",
        verify_schema=False,
        bq_client=mock_bq,
    )

    traces = client.list_traces(TraceFilter(limit=1))

    assert [trace.session_id for trace in traces] == ["older-valid"]
    assert mock_bq.query.call_count == 1

  def test_poison_only_anchor_does_not_starve_api_evaluation(self):
    from google.cloud import bigquery as bq

    from bigquery_agent_analytics.evaluators import SessionScore

    poison = self._anchored(
        _event_row(session_id="newest-poison", user_id="mallory", span_id="x1")
    )
    poison["attributes"] = "opaque"
    poison["attributes_type"] = "string"
    older = self._anchored(
        _event_row(session_id="older-valid", user_id="alice", span_id="good")
    )
    older["attributes_type"] = "object"

    def query_side_effect(query, **_kwargs):
      selected = older if self._has_cte_attestation(query) else poison
      return self._job([_mock_row(dict(selected))])

    class _Judge:
      name = "judge"

      async def evaluate_session(self, trace_text, final):
        return SessionScore(
            session_id="pending", scores={"quality": 1.0}, passed=True
        )

    mock_bq = MagicMock()
    mock_bq.query.side_effect = query_side_effect
    client = Client(
        project_id="proj",
        dataset_id="ds",
        verify_schema=False,
        bq_client=mock_bq,
    )

    report = client._api_judge(
        _Judge(),
        "tbl",
        "TRUE",
        [bq.ScalarQueryParameter("trace_limit", "INT64", 1)],
        row_where="TRUE",
        limit=1,
        trace_filter=None,
    )

    assert report.total_sessions == 1
    assert report.session_scores[0].session_id == "older-valid"
    assert mock_bq.query.call_count == 1
