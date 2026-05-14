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

"""Hand-authored reference extractor for the MAKO decision flow.

Consumed by:

* The notebook's Beat 3 cells (3.3 / 3.4 / 3.5 / 3.7) via
  ``measure_compile(..., reference_extractor=...)``.
* The revalidation CLI
  (``bqaa-revalidate-extractors``) via
  ``--reference-extractors-module
  examples.migration_v5.reference_extractor``.

Both consumers expect the same module-level surface:

* ``EXTRACTORS`` — ``dict[str, Callable]`` mapping
  ``event_type`` to an extractor with signature
  ``(event, spec) -> StructuredExtractionResult``.
* ``RESOLVED_GRAPH`` — a ``ResolvedGraph`` produced by
  ``resolve(ontology, binding)``. The harness uses it to
  validate extractor output before fingerprinting.
* ``SPEC`` (optional) — forwarded as the second argument
  of every extractor call. We default to ``None`` since
  the MAKO extractors don't consume the spec.

Coverage:

The MAKO agent emits ``TOOL_COMPLETED`` events for five
decision-flow tools. The extractor switches on the tool
name and produces the per-tool slice of the MAKO graph:

| Tool                       | Node                   | Edges                                                                                                            |
|----------------------------|------------------------|------------------------------------------------------------------------------------------------------------------|
| ``capture_context``        | ``ContextSnapshot``    | —                                                                                                                |
| ``propose_decision_point`` | ``DecisionPoint``      | —                                                                                                                |
| ``evaluate_candidate``     | ``Candidate``          | ``evaluatesCandidate`` (DecisionPoint → Candidate)                                                              |
| ``commit_outcome``         | ``SelectionOutcome``   | ``selectedCandidate`` (SelectionOutcome → Candidate)                                                            |
| ``complete_execution``     | ``DecisionExecution``  | ``executedAtDecisionPoint``, ``atContextSnapshot``, ``hasSelectionOutcome``, plus ``AgentSession`` + ``partOfSession`` |

``AgentSession`` is synthesized from the plugin
envelope's ``session_id`` because the agent's tools don't
return a session-shaped payload. The synthesis happens
inside ``_extract_complete_execution`` so it only fires
once per session (when the agent finishes a decision
flow), not on every event.

Node-ID encoding follows the binding's per-entity PK
columns (see PR #155's mako_artifacts.py): each node_id
is ``{session_id}:{Entity}:{pk_col}={value}``. Edge FK
column values fall out of ``parse_key_segment`` against
those node IDs, which is how
``ontology_materializer._route_edge`` reads them.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any, Optional

from bigquery_agent_analytics.extracted_models import ExtractedEdge
from bigquery_agent_analytics.extracted_models import ExtractedNode
from bigquery_agent_analytics.extracted_models import ExtractedProperty
from bigquery_agent_analytics.resolved_spec import resolve as _resolve_spec
from bigquery_agent_analytics.structured_extraction import StructuredExtractionResult
from bigquery_ontology import load_binding
from bigquery_ontology import load_ontology

# Resolve paths relative to this file so the module works
# regardless of CWD (the notebook + the revalidation CLI
# both import this from different directories).
_HERE = pathlib.Path(__file__).parent
_ONTOLOGY_PATH = _HERE / "ontology.yaml"
_BINDING_PATH = _HERE / "binding.yaml"


# ------------------------------------------------------------------ #
# Per-tool extractors                                                 #
# ------------------------------------------------------------------ #


def _extract_capture_context(
    session_id: str, span_id: str, result: dict
) -> StructuredExtractionResult:
  """``capture_context`` → ``ContextSnapshot`` node."""
  context_id = result.get("context_id")
  if not context_id:
    return StructuredExtractionResult()

  node_id = f"{session_id}:ContextSnapshot:context_snapshot_id={context_id}"
  properties = [ExtractedProperty(name="context_snapshot_id", value=context_id)]
  if "snapshot_payload" in result:
    # ``ContextSnapshot.snapshotPayload`` is declared
    # ``xsd:string`` in MAKO; the validator rejects a dict
    # value as ``unsupported_type``. JSON-serialize so the
    # payload survives as a queryable string column. (The
    # binding's column is plain ``STRING``, not ``JSON`` —
    # downstream consumers ``JSON_VALUE`` it.)
    raw_payload = result["snapshot_payload"]
    if isinstance(raw_payload, (dict, list)):
      payload_value = json.dumps(raw_payload, sort_keys=True)
    else:
      payload_value = str(raw_payload)
    properties.append(
        ExtractedProperty(name="snapshot_payload", value=payload_value)
    )

  node = ExtractedNode(
      node_id=node_id,
      entity_name="ContextSnapshot",
      labels=["ContextSnapshot"],
      properties=properties,
  )
  return StructuredExtractionResult(
      nodes=[node],
      fully_handled_span_ids={span_id} if span_id else set(),
  )


def _extract_propose_decision_point(
    session_id: str, span_id: str, result: dict
) -> StructuredExtractionResult:
  """``propose_decision_point`` → ``DecisionPoint`` node."""
  decision_point_id = result.get("decision_point_id")
  if not decision_point_id:
    return StructuredExtractionResult()

  node_id = f"{session_id}:DecisionPoint:decision_point_id={decision_point_id}"
  properties = [
      ExtractedProperty(name="decision_point_id", value=decision_point_id),
  ]
  if "reversibility" in result:
    properties.append(
        ExtractedProperty(name="reversibility", value=result["reversibility"])
    )

  node = ExtractedNode(
      node_id=node_id,
      entity_name="DecisionPoint",
      labels=["DecisionPoint"],
      properties=properties,
  )
  return StructuredExtractionResult(
      nodes=[node],
      fully_handled_span_ids={span_id} if span_id else set(),
  )


def _extract_evaluate_candidate(
    session_id: str, span_id: str, result: dict
) -> StructuredExtractionResult:
  """``evaluate_candidate`` → ``Candidate`` node +
  ``evaluatesCandidate`` edge (DecisionPoint → Candidate)."""
  candidate_id = result.get("candidate_id")
  decision_point_id = result.get("decision_point_id")
  if not candidate_id or not decision_point_id:
    return StructuredExtractionResult()

  candidate_node_id = f"{session_id}:Candidate:candidate_id={candidate_id}"
  decision_point_node_id = (
      f"{session_id}:DecisionPoint:decision_point_id={decision_point_id}"
  )

  node = ExtractedNode(
      node_id=candidate_node_id,
      entity_name="Candidate",
      labels=["Candidate"],
      properties=[ExtractedProperty(name="candidate_id", value=candidate_id)],
  )
  edge = ExtractedEdge(
      edge_id=f"evaluatesCandidate:{decision_point_id}:{candidate_id}",
      relationship_name="evaluatesCandidate",
      from_node_id=decision_point_node_id,
      to_node_id=candidate_node_id,
  )
  return StructuredExtractionResult(
      nodes=[node],
      edges=[edge],
      fully_handled_span_ids={span_id} if span_id else set(),
  )


def _extract_commit_outcome(
    session_id: str, span_id: str, result: dict
) -> StructuredExtractionResult:
  """``commit_outcome`` → ``SelectionOutcome`` node +
  ``selectedCandidate`` edge (SelectionOutcome → Candidate).

  Rationale field on the tool result is **trace-only** —
  MAKO doesn't declare ``rationale`` on
  ``SelectionOutcome``, so the span is marked
  ``partially_handled`` (the free-text rationale stays in
  the AI transcript)."""
  outcome_id = result.get("outcome_id")
  selected_candidate_id = result.get("selected_candidate_id")
  if not outcome_id or not selected_candidate_id:
    return StructuredExtractionResult()

  outcome_node_id = (
      f"{session_id}:SelectionOutcome:selection_outcome_id={outcome_id}"
  )
  candidate_node_id = (
      f"{session_id}:Candidate:candidate_id={selected_candidate_id}"
  )

  node = ExtractedNode(
      node_id=outcome_node_id,
      entity_name="SelectionOutcome",
      labels=["SelectionOutcome"],
      properties=[
          ExtractedProperty(name="selection_outcome_id", value=outcome_id)
      ],
  )
  edge = ExtractedEdge(
      edge_id=f"selectedCandidate:{outcome_id}:{selected_candidate_id}",
      relationship_name="selectedCandidate",
      from_node_id=outcome_node_id,
      to_node_id=candidate_node_id,
  )

  partial = {span_id} if span_id and "rationale" in result else set()
  full = {span_id} if span_id and "rationale" not in result else set()
  return StructuredExtractionResult(
      nodes=[node],
      edges=[edge],
      fully_handled_span_ids=full,
      partially_handled_span_ids=partial,
  )


def _extract_complete_execution(
    session_id: str, span_id: str, result: dict
) -> StructuredExtractionResult:
  """``complete_execution`` → ``DecisionExecution`` node +
  every edge that hangs off the central hub.

  This is also where the envelope-side ``AgentSession`` is
  synthesized. The agent's tools never return a session
  payload, but the plugin envelope carries ``session_id``
  on every event. Emitting ``AgentSession`` + the
  ``partOfSession`` edge from this extractor keeps the
  whole hub-shape graph in one place — Beat 4.4's hub-
  shape traversal `(DecisionExecution)-[partOfSession]->
  (AgentSession)` is what consumes them.
  """
  execution_id = result.get("execution_id")
  decision_point_id = result.get("decision_point_id")
  context_id = result.get("context_id")
  outcome_id = result.get("outcome_id")
  if not (execution_id and decision_point_id and context_id and outcome_id):
    return StructuredExtractionResult()

  execution_node_id = (
      f"{session_id}:DecisionExecution:decision_execution_id={execution_id}"
  )
  decision_point_node_id = (
      f"{session_id}:DecisionPoint:decision_point_id={decision_point_id}"
  )
  context_node_id = (
      f"{session_id}:ContextSnapshot:context_snapshot_id={context_id}"
  )
  outcome_node_id = (
      f"{session_id}:SelectionOutcome:selection_outcome_id={outcome_id}"
  )
  agent_session_node_id = (
      f"{session_id}:AgentSession:agent_session_id={session_id}"
  )

  execution_properties = [
      ExtractedProperty(name="decision_execution_id", value=execution_id),
  ]
  if "business_entity_id" in result:
    execution_properties.append(
        ExtractedProperty(
            name="business_entity_id", value=result["business_entity_id"]
        )
    )
  if "latency_ms" in result:
    execution_properties.append(
        ExtractedProperty(name="latency_ms", value=result["latency_ms"])
    )

  execution_node = ExtractedNode(
      node_id=execution_node_id,
      entity_name="DecisionExecution",
      labels=["DecisionExecution"],
      properties=execution_properties,
  )

  # AgentSession synthesis: one node per session,
  # primary-key column ``agent_session_id`` (per binding).
  # ``AgentSession.sessionId`` is the MAKO-declared data
  # property — value is the same envelope session_id.
  agent_session_node = ExtractedNode(
      node_id=agent_session_node_id,
      entity_name="AgentSession",
      labels=["AgentSession"],
      properties=[
          ExtractedProperty(name="agent_session_id", value=session_id),
          ExtractedProperty(name="session_id", value=session_id),
      ],
  )

  edges = [
      ExtractedEdge(
          edge_id=f"executedAtDecisionPoint:{execution_id}:{decision_point_id}",
          relationship_name="executedAtDecisionPoint",
          from_node_id=execution_node_id,
          to_node_id=decision_point_node_id,
      ),
      ExtractedEdge(
          edge_id=f"atContextSnapshot:{execution_id}:{context_id}",
          relationship_name="atContextSnapshot",
          from_node_id=execution_node_id,
          to_node_id=context_node_id,
      ),
      ExtractedEdge(
          edge_id=f"hasSelectionOutcome:{execution_id}:{outcome_id}",
          relationship_name="hasSelectionOutcome",
          from_node_id=execution_node_id,
          to_node_id=outcome_node_id,
      ),
      ExtractedEdge(
          edge_id=f"partOfSession:{execution_id}:{session_id}",
          relationship_name="partOfSession",
          from_node_id=execution_node_id,
          to_node_id=agent_session_node_id,
      ),
  ]

  return StructuredExtractionResult(
      nodes=[execution_node, agent_session_node],
      edges=edges,
      fully_handled_span_ids={span_id} if span_id else set(),
  )


# ------------------------------------------------------------------ #
# Top-level extractor (event_type-keyed dispatch)                    #
# ------------------------------------------------------------------ #


_TOOL_HANDLERS = {
    "capture_context": _extract_capture_context,
    "propose_decision_point": _extract_propose_decision_point,
    "evaluate_candidate": _extract_evaluate_candidate,
    "commit_outcome": _extract_commit_outcome,
    "complete_execution": _extract_complete_execution,
}


def extract_mako_decision_event(
    event: dict, spec: Any
) -> StructuredExtractionResult:
  """Reference extractor for MAKO ``TOOL_COMPLETED`` events.

  The MAKO agent emits five tool-call types; this function
  dispatches on ``content.tool`` and delegates to the
  per-tool helper. Non-tool events (LLM_REQUEST,
  USER_MESSAGE_RECEIVED, etc.) return an empty result —
  the AI fallback handles them.

  Args:
    event: Plugin event row (dict-shaped, matches
      ``_get_events_schema`` from
      ``bigquery_agent_analytics_plugin``). Required keys:
      ``content`` (dict), ``session_id`` (str),
      ``span_id`` (str).
    spec: Unused. Forwarded by the
      ``StructuredExtractor`` contract.

  Returns:
    A ``StructuredExtractionResult`` — empty when the
    event isn't a MAKO tool-call or required fields are
    missing.
  """
  del spec  # Reference extractors take spec but MAKO doesn't use it.

  content = event.get("content")
  if not isinstance(content, dict):
    return StructuredExtractionResult()
  tool_name = content.get("tool")
  if tool_name not in _TOOL_HANDLERS:
    return StructuredExtractionResult()
  result = content.get("result")
  if not isinstance(result, dict):
    return StructuredExtractionResult()

  session_id = event.get("session_id") or ""
  span_id = event.get("span_id") or ""
  return _TOOL_HANDLERS[tool_name](session_id, span_id, result)


# ------------------------------------------------------------------ #
# Module-level surface for the revalidation CLI + harness            #
# ------------------------------------------------------------------ #


def _load_resolved_graph():
  """Lazy load to keep import-time work minimal — the
  revalidation CLI imports this module from arbitrary CWDs
  and only some callers actually use the ``RESOLVED_GRAPH``
  attribute."""
  ontology = load_ontology(str(_ONTOLOGY_PATH))
  binding = load_binding(str(_BINDING_PATH), ontology=ontology)
  return _resolve_spec(ontology, binding)


# The revalidation CLI keys this dict on the
# ``event_type`` column. MAKO's structured payloads all
# land in ``TOOL_COMPLETED`` events (one per tool call;
# the agent emits five per decision flow). Other event
# types (``LLM_RESPONSE`` reasoning text,
# ``USER_MESSAGE_RECEIVED`` raw prompt, etc.) are left to
# the AI fallback.
EXTRACTORS = {
    "TOOL_COMPLETED": extract_mako_decision_event,
}

RESOLVED_GRAPH = _load_resolved_graph()

# ``SPEC`` is the second arg the harness/CLI passes to
# every extractor call. The MAKO extractor doesn't use it
# (the graph shape is locked in by ``RESOLVED_GRAPH``);
# ``None`` matches the harness's keyword default.
SPEC: Optional[Any] = None
