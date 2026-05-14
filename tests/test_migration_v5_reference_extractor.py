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

"""Tests for ``examples/migration_v5/reference_extractor.py``.

Covers:

* Per-tool extractor behavior (one event → expected nodes + edges).
* Module-level surface contract (``EXTRACTORS``, ``RESOLVED_GRAPH``,
  ``SPEC``) the revalidation CLI expects.
* End-to-end: a full 5-event decision flow merges into a graph
  that passes ``validate_extracted_graph`` against the MAKO
  ``RESOLVED_GRAPH``.
* AgentSession synthesis from the plugin envelope ``session_id``
  (Beat 4.4's ``partOfSession`` hub edge depends on this).
* Empty-result fast paths (non-tool events, missing keys,
  non-dict content).
"""

from __future__ import annotations

import importlib
import json
import pathlib
import sys

import pytest

# The reference extractor lives under ``examples/``, which isn't
# on ``sys.path`` by default. Add the repo root so
# ``import examples.migration_v5.reference_extractor`` works.
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
  sys.path.insert(0, str(_REPO_ROOT))

reference_extractor = importlib.import_module(
    "examples.migration_v5.reference_extractor"
)

from bigquery_agent_analytics.extracted_models import ExtractedGraph
from bigquery_agent_analytics.graph_validation import FallbackScope
from bigquery_agent_analytics.graph_validation import validate_extracted_graph
from bigquery_agent_analytics.structured_extraction import merge_extraction_results

# ------------------------------------------------------------------ #
# Helpers                                                              #
# ------------------------------------------------------------------ #


def _tool_event(
    tool: str,
    result: dict,
    *,
    session_id: str = "sess-A",
    span_id: str = "span-1",
) -> dict:
  """Build a synthetic ``TOOL_COMPLETED`` event with the
  same shape the BQ AA plugin produces. The fields outside
  ``content`` are deliberately sparse — the extractor only
  reads ``content`` + envelope-side ``session_id`` /
  ``span_id``."""
  return {
      "event_type": "TOOL_COMPLETED",
      "session_id": session_id,
      "span_id": span_id,
      "content": {"tool": tool, "result": result},
  }


def _decision_flow(session_id: str = "sess-A") -> list[dict]:
  """The canonical five-event flow the agent produces per
  decision."""
  return [
      _tool_event(
          "capture_context",
          {
              "context_id": "ctx-1",
              "snapshot_payload": {
                  "audience_size": 1000,
                  "budget_remaining_usd": 500,
              },
          },
          session_id=session_id,
          span_id="s1",
      ),
      _tool_event(
          "propose_decision_point",
          {"decision_point_id": "dp-1", "reversibility": "compensable"},
          session_id=session_id,
          span_id="s2",
      ),
      _tool_event(
          "evaluate_candidate",
          {"candidate_id": "cand-1", "decision_point_id": "dp-1"},
          session_id=session_id,
          span_id="s3",
      ),
      _tool_event(
          "commit_outcome",
          {
              "outcome_id": "out-1",
              "decision_point_id": "dp-1",
              "selected_candidate_id": "cand-1",
              "rationale": "best fit",
          },
          session_id=session_id,
          span_id="s4",
      ),
      _tool_event(
          "complete_execution",
          {
              "execution_id": "exec-1",
              "decision_point_id": "dp-1",
              "context_id": "ctx-1",
              "outcome_id": "out-1",
              "business_entity_id": "campaign-x",
              "latency_ms": 42,
          },
          session_id=session_id,
          span_id="s5",
      ),
  ]


# ------------------------------------------------------------------ #
# Module surface contract                                              #
# ------------------------------------------------------------------ #


def test_module_surface_exports():
  """Revalidation CLI requires
  ``EXTRACTORS`` (non-empty dict[str, callable]) +
  ``RESOLVED_GRAPH``. ``SPEC`` is optional."""
  assert isinstance(reference_extractor.EXTRACTORS, dict)
  assert reference_extractor.EXTRACTORS
  for event_type, fn in reference_extractor.EXTRACTORS.items():
    assert isinstance(event_type, str) and event_type
    assert callable(fn)
  assert reference_extractor.RESOLVED_GRAPH is not None


def test_extractors_keyed_on_tool_completed():
  """Only ``TOOL_COMPLETED`` events carry MAKO tool
  outputs; the dict must be keyed on that event_type so the
  revalidation CLI dispatches correctly."""
  assert "TOOL_COMPLETED" in reference_extractor.EXTRACTORS


# ------------------------------------------------------------------ #
# Per-tool extractors                                                  #
# ------------------------------------------------------------------ #


def test_capture_context_emits_context_snapshot():
  event = _tool_event(
      "capture_context",
      {"context_id": "ctx-42", "snapshot_payload": {"audience_size": 99}},
  )
  result = reference_extractor.extract_mako_decision_event(event, None)
  assert len(result.nodes) == 1
  assert len(result.edges) == 0
  node = result.nodes[0]
  assert node.entity_name == "ContextSnapshot"
  assert (
      node.node_id == "sess-A:ContextSnapshot:context_snapshot_id=sess-A:ctx-42"
  )
  prop_by_name = {p.name: p.value for p in node.properties}
  assert prop_by_name["context_snapshot_id"] == "sess-A:ctx-42"
  # ``snapshot_payload`` must be a JSON-serialized STRING
  # (ontology declares xsd:string; validator rejects raw
  # dict values as ``unsupported_type``).
  assert isinstance(prop_by_name["snapshot_payload"], str)
  assert json.loads(prop_by_name["snapshot_payload"]) == {"audience_size": 99}


def test_propose_decision_point_emits_decision_point():
  event = _tool_event(
      "propose_decision_point",
      {"decision_point_id": "dp-42", "reversibility": "compensable"},
  )
  result = reference_extractor.extract_mako_decision_event(event, None)
  assert len(result.nodes) == 1
  assert result.nodes[0].entity_name == "DecisionPoint"
  assert (
      result.nodes[0].node_id
      == "sess-A:DecisionPoint:decision_point_id=sess-A:dp-42"
  )


def test_evaluate_candidate_emits_node_and_edge():
  """``evaluate_candidate`` produces both a ``Candidate``
  node and the ``evaluatesCandidate`` edge that points
  back at the originating ``DecisionPoint``."""
  event = _tool_event(
      "evaluate_candidate",
      {"candidate_id": "cand-7", "decision_point_id": "dp-7"},
  )
  result = reference_extractor.extract_mako_decision_event(event, None)
  assert len(result.nodes) == 1
  assert result.nodes[0].entity_name == "Candidate"
  assert len(result.edges) == 1
  edge = result.edges[0]
  assert edge.relationship_name == "evaluatesCandidate"
  assert (
      edge.from_node_id == "sess-A:DecisionPoint:decision_point_id=sess-A:dp-7"
  )
  assert edge.to_node_id == "sess-A:Candidate:candidate_id=sess-A:cand-7"


def test_commit_outcome_marks_span_partial_when_rationale_present():
  """The ``rationale`` field is trace-only — MAKO doesn't
  declare it on ``SelectionOutcome``, so the span stays in
  the AI transcript even after structured extraction."""
  with_rationale = _tool_event(
      "commit_outcome",
      {
          "outcome_id": "out-1",
          "decision_point_id": "dp-1",
          "selected_candidate_id": "cand-1",
          "rationale": "best fit",
      },
      span_id="s-with",
  )
  result = reference_extractor.extract_mako_decision_event(with_rationale, None)
  assert "s-with" in result.partially_handled_span_ids
  assert "s-with" not in result.fully_handled_span_ids

  without = _tool_event(
      "commit_outcome",
      {
          "outcome_id": "out-1",
          "decision_point_id": "dp-1",
          "selected_candidate_id": "cand-1",
      },
      span_id="s-without",
  )
  result_no = reference_extractor.extract_mako_decision_event(without, None)
  assert "s-without" in result_no.fully_handled_span_ids
  assert "s-without" not in result_no.partially_handled_span_ids


def test_complete_execution_synthesizes_agent_session_and_hub_edges():
  """The central hub: one node + four edges + the
  envelope-side ``AgentSession`` synthesis that Beat 4.4's
  hub-shape traversal needs."""
  event = _tool_event(
      "complete_execution",
      {
          "execution_id": "exec-1",
          "decision_point_id": "dp-1",
          "context_id": "ctx-1",
          "outcome_id": "out-1",
          "business_entity_id": "campaign-x",
          "latency_ms": 42,
      },
      session_id="my-session",
  )
  result = reference_extractor.extract_mako_decision_event(event, None)
  nodes_by_entity = {n.entity_name: n for n in result.nodes}
  assert set(nodes_by_entity) == {"DecisionExecution", "AgentSession"}
  assert nodes_by_entity["AgentSession"].node_id == (
      "my-session:AgentSession:agent_session_id=my-session"
  )

  edges_by_rel = {e.relationship_name for e in result.edges}
  assert edges_by_rel == {
      "executedAtDecisionPoint",
      "atContextSnapshot",
      "hasSelectionOutcome",
      "partOfSession",
  }
  part_of_session = next(
      e for e in result.edges if e.relationship_name == "partOfSession"
  )
  assert (
      part_of_session.from_node_id
      == "my-session:DecisionExecution:decision_execution_id=my-session:exec-1"
  )
  assert part_of_session.to_node_id == (
      "my-session:AgentSession:agent_session_id=my-session"
  )


# ------------------------------------------------------------------ #
# Empty / non-matching fast paths                                       #
# ------------------------------------------------------------------ #


def test_non_tool_event_returns_empty():
  """Reasoning / message events fall through to the AI
  fallback — the extractor must not produce noise."""
  event = {
      "event_type": "LLM_RESPONSE",
      "session_id": "sess-A",
      "span_id": "span-x",
      "content": {"text": "I will pick candidate A."},
  }
  result = reference_extractor.extract_mako_decision_event(event, None)
  assert not result.nodes and not result.edges


def test_unknown_tool_returns_empty():
  event = _tool_event("not_a_mako_tool", {"some": "data"})
  result = reference_extractor.extract_mako_decision_event(event, None)
  assert not result.nodes and not result.edges


def test_missing_required_field_returns_empty():
  """Each per-tool extractor needs specific keys in
  ``result``. Without them, no partial graph is emitted."""
  event = _tool_event(
      "complete_execution",
      {"execution_id": "exec-1"},  # missing dp/ctx/outcome
  )
  result = reference_extractor.extract_mako_decision_event(event, None)
  assert not result.nodes and not result.edges


def test_non_dict_content_returns_empty():
  event = {
      "event_type": "TOOL_COMPLETED",
      "session_id": "sess-A",
      "span_id": "span-1",
      "content": "not-a-dict",
  }
  result = reference_extractor.extract_mako_decision_event(event, None)
  assert not result.nodes and not result.edges


# ------------------------------------------------------------------ #
# End-to-end: full decision flow validates against the ontology        #
# ------------------------------------------------------------------ #


def test_full_decision_flow_validator_clean():
  """A merged 5-tool flow produces 6 nodes + 6 edges that
  pass the MAKO validator. This is the contract the
  notebook's Beat 3 cells (3.3 / 3.5 / 3.7) gate on."""
  results = [
      reference_extractor.extract_mako_decision_event(ev, None)
      for ev in _decision_flow()
  ]
  merged = merge_extraction_results(results)
  assert len(merged.nodes) == 6
  assert len(merged.edges) == 6

  graph = ExtractedGraph(name="mako", nodes=merged.nodes, edges=merged.edges)
  report = validate_extracted_graph(reference_extractor.RESOLVED_GRAPH, graph)
  assert report.ok, [(f.scope.value, f.code, f.path) for f in report.failures]


def test_full_decision_flow_emits_part_of_session_edge():
  """Beat 4.4's hub-shape traversal expects at least one
  ``partOfSession`` row. The reference extractor must
  synthesize it from the envelope side."""
  results = [
      reference_extractor.extract_mako_decision_event(ev, None)
      for ev in _decision_flow()
  ]
  merged = merge_extraction_results(results)
  rels = {e.relationship_name for e in merged.edges}
  assert "partOfSession" in rels


def test_two_sessions_dedupe_agent_session_nodes():
  """If two distinct sessions both complete a decision,
  the merged graph carries two distinct ``AgentSession``
  nodes (one per session) — not one collapsed node nor
  duplicate rows."""
  events = _decision_flow("sess-A") + _decision_flow("sess-B")
  results = [
      reference_extractor.extract_mako_decision_event(ev, None) for ev in events
  ]
  merged = merge_extraction_results(results)
  agent_sessions = [n for n in merged.nodes if n.entity_name == "AgentSession"]
  assert {n.node_id for n in agent_sessions} == {
      "sess-A:AgentSession:agent_session_id=sess-A",
      "sess-B:AgentSession:agent_session_id=sess-B",
  }


def test_two_sessions_have_unique_node_pk_values_and_edge_ids():
  """Cross-session collision regression: two sessions whose
  agent tools happen to emit identical raw IDs (same args
  → same content-hashed IDs) must produce **distinct** PK
  column values on the materialized rows AND distinct
  ``edge_id`` values. Without session-scoping the
  extracted graph would silently merge two sessions'
  decision flows."""
  events = _decision_flow("sess-A") + _decision_flow("sess-B")
  results = [
      reference_extractor.extract_mako_decision_event(ev, None) for ev in events
  ]
  merged = merge_extraction_results(results)

  # Every node's PK property value must be unique across
  # the whole graph (excluding AgentSession, whose PK is
  # the session_id itself — already unique per session).
  pk_property_names = {
      "ContextSnapshot": "context_snapshot_id",
      "DecisionPoint": "decision_point_id",
      "Candidate": "candidate_id",
      "SelectionOutcome": "selection_outcome_id",
      "DecisionExecution": "decision_execution_id",
      "AgentSession": "agent_session_id",
  }
  pk_values: list[tuple[str, str]] = []
  for node in merged.nodes:
    pk_name = pk_property_names[node.entity_name]
    pk_value = next(p.value for p in node.properties if p.name == pk_name)
    pk_values.append((node.entity_name, pk_value))
  # Build set of all (entity, pk_value) pairs; duplicates
  # within the same entity name are the bug.
  seen: dict[str, set[str]] = {}
  for entity, value in pk_values:
    seen.setdefault(entity, set())
    assert (
        value not in seen[entity]
    ), f"duplicate PK value {value!r} for entity {entity!r}"
    seen[entity].add(value)

  # Every edge_id must be unique.
  edge_ids = [e.edge_id for e in merged.edges]
  assert len(edge_ids) == len(set(edge_ids)), (
      f"duplicate edge_id in merged graph: "
      f"{[eid for eid in edge_ids if edge_ids.count(eid) > 1]}"
  )


def test_complete_execution_carries_envelope_span_and_trace():
  """``DecisionExecution.spanId`` / ``traceId`` are MAKO-
  declared provenance properties pulled from the plugin
  envelope of the ``complete_execution`` event."""
  event = _tool_event(
      "complete_execution",
      {
          "execution_id": "exec-1",
          "decision_point_id": "dp-1",
          "context_id": "ctx-1",
          "outcome_id": "out-1",
      },
      session_id="sess-1",
      span_id="span-prov-1",
  )
  event["trace_id"] = "trace-prov-1"
  result = reference_extractor.extract_mako_decision_event(event, None)
  exec_node = next(
      n for n in result.nodes if n.entity_name == "DecisionExecution"
  )
  prop_by_name = {p.name: p.value for p in exec_node.properties}
  assert prop_by_name["span_id"] == "span-prov-1"
  assert prop_by_name["trace_id"] == "trace-prov-1"


def test_complete_execution_omits_span_trace_when_missing():
  """Provenance props are optional — sparse-event sources
  (offline replay, synthetic fixtures) may omit them and
  shouldn't trip the validator with empty-string
  ``span_id`` / ``trace_id`` properties."""
  event = _tool_event(
      "complete_execution",
      {
          "execution_id": "exec-1",
          "decision_point_id": "dp-1",
          "context_id": "ctx-1",
          "outcome_id": "out-1",
      },
      span_id="",
  )
  # No trace_id key at all
  result = reference_extractor.extract_mako_decision_event(event, None)
  exec_node = next(
      n for n in result.nodes if n.entity_name == "DecisionExecution"
  )
  prop_names = {p.name for p in exec_node.properties}
  assert "span_id" not in prop_names
  assert "trace_id" not in prop_names
