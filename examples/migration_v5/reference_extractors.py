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

"""Reference extractors module for the migration v5 demo.

Exposes the contract ``bqaa-revalidate-extractors`` requires:

* ``EXTRACTORS`` — dict[str, Callable] keyed by event_type.
* ``RESOLVED_GRAPH`` — output of ``resolve(ontology, binding)``
  with the demo's MAKO subset.
* ``SPEC`` — optional; defaults to None.

The handwritten ``extract_mako_decision_event`` extractor
mirrors the BKA-decision pattern from
``bigquery_agent_analytics.structured_extraction``: each
``mako_decision`` event produces one ``DecisionPoint`` node
plus its outgoing edges to ``Candidate`` / ``SelectionOutcome``
/ ``ContextSnapshot``. The compiled extractor (built in
notebook Beat 3.3 via ``measure_compile``) is checked
against this reference for behavioral parity.
"""

from __future__ import annotations

import pathlib
from typing import Any

from bigquery_agent_analytics.extracted_models import ExtractedEdge
from bigquery_agent_analytics.extracted_models import ExtractedNode
from bigquery_agent_analytics.extracted_models import ExtractedProperty
from bigquery_agent_analytics.resolved_spec import resolve
from bigquery_agent_analytics.structured_extraction import StructuredExtractionResult
from bigquery_ontology import load_binding
from bigquery_ontology import load_ontology

# Compute the resolved graph at import time so the CLI's
# reference module contract (``RESOLVED_GRAPH`` at module
# scope) is satisfied.
_FIXTURE_DIR = pathlib.Path(__file__).parent
_ONTOLOGY = load_ontology(str(_FIXTURE_DIR / "ontology_demo.yaml"))
_BINDING = load_binding(str(_FIXTURE_DIR / "binding.yaml"), ontology=_ONTOLOGY)
RESOLVED_GRAPH = resolve(_ONTOLOGY, _BINDING)
SPEC: Any = None


def extract_mako_decision_event(
    event: dict, spec: Any
) -> StructuredExtractionResult:
  """Extract a MAKO ``DecisionPoint`` (plus its edges) from
  one ``mako_decision`` event.

  Output shape per event:

  * 1 ``DecisionPoint`` node (always present if the content
    dict contains ``decision_id``).
  * 1 ``SelectionOutcome`` node (always present — every
    decision picks exactly one outcome).
  * 1 ``ContextSnapshot`` reference node (the decision points
    at a previously-captured snapshot — we emit a thin node
    so the edge has a target, even though the rich payload
    landed via the earlier ``context_captured`` event).
  * N ``Candidate`` nodes (one per evaluated candidate).
  * 1 ``hasOutcome`` edge.
  * 1 ``hasContext`` edge.
  * N ``hasCandidate`` edges.

  Returns ``StructuredExtractionResult()`` (empty) when the
  event isn't a well-shaped ``mako_decision`` — same
  conservative shape ``extract_bka_decision_event`` uses."""
  content = event.get("content")
  if not isinstance(content, dict):
    return StructuredExtractionResult()
  decision_id = content.get("decision_id")
  if not isinstance(decision_id, str) or not decision_id:
    return StructuredExtractionResult()

  session_id = event.get("session_id", "")
  span_id = event.get("span_id", "")

  decision_node_id = f"{session_id}:DecisionPoint:id={decision_id}"
  decision_node = ExtractedNode(
      node_id=decision_node_id,
      entity_name="DecisionPoint",
      labels=["DecisionPoint"],
      properties=[
          ExtractedProperty(name="id", value=decision_id),
          ExtractedProperty(
              name="decisionType",
              value=content.get("decision_type", ""),
          ),
          ExtractedProperty(
              name="decidedAt",
              value=event.get("event_timestamp", ""),
          ),
      ],
  )

  nodes: list[ExtractedNode] = [decision_node]
  edges: list[ExtractedEdge] = []

  # Outcome.
  outcome_id = content.get("outcome_id")
  selected_id = content.get("selected_candidate_id")
  rationale = content.get("rationale", "")
  if isinstance(outcome_id, str) and outcome_id:
    outcome_node_id = f"{session_id}:SelectionOutcome:id={outcome_id}"
    nodes.append(
        ExtractedNode(
            node_id=outcome_node_id,
            entity_name="SelectionOutcome",
            labels=["SelectionOutcome"],
            properties=[
                ExtractedProperty(name="id", value=outcome_id),
                ExtractedProperty(
                    name="selectedCandidateId",
                    value=(selected_id if isinstance(selected_id, str) else ""),
                ),
                ExtractedProperty(name="rationale", value=rationale),
            ],
        )
    )
    edges.append(
        ExtractedEdge(
            edge_id=f"{decision_node_id}->hasOutcome->{outcome_node_id}",
            relationship_name="hasOutcome",
            from_node_id=decision_node_id,
            to_node_id=outcome_node_id,
            properties=[],
        )
    )

  # Context reference. Thin node — the rich snapshot landed
  # via the earlier ``context_captured`` event.
  context_id = content.get("context_id")
  if isinstance(context_id, str) and context_id:
    context_node_id = f"{session_id}:ContextSnapshot:id={context_id}"
    nodes.append(
        ExtractedNode(
            node_id=context_node_id,
            entity_name="ContextSnapshot",
            labels=["ContextSnapshot"],
            properties=[
                ExtractedProperty(name="id", value=context_id),
            ],
        )
    )
    edges.append(
        ExtractedEdge(
            edge_id=f"{decision_node_id}->hasContext->{context_node_id}",
            relationship_name="hasContext",
            from_node_id=decision_node_id,
            to_node_id=context_node_id,
            properties=[],
        )
    )

  # Candidates.
  for cand in content.get("candidates", []) or ():
    if not isinstance(cand, dict):
      continue
    cand_id = cand.get("candidate_id")
    if not isinstance(cand_id, str) or not cand_id:
      continue
    cand_node_id = f"{session_id}:Candidate:id={cand_id}"
    nodes.append(
        ExtractedNode(
            node_id=cand_node_id,
            entity_name="Candidate",
            labels=["Candidate"],
            properties=[
                ExtractedProperty(name="id", value=cand_id),
                ExtractedProperty(
                    name="candidateLabel",
                    value=cand.get("candidate_label", ""),
                ),
                ExtractedProperty(name="score", value=cand.get("score", 0.0)),
            ],
        )
    )
    edges.append(
        ExtractedEdge(
            edge_id=f"{decision_node_id}->hasCandidate->{cand_node_id}",
            relationship_name="hasCandidate",
            from_node_id=decision_node_id,
            to_node_id=cand_node_id,
            properties=[],
        )
    )

  fully_handled: set[str] = {span_id} if span_id else set()

  return StructuredExtractionResult(
      nodes=nodes,
      edges=edges,
      fully_handled_span_ids=fully_handled,
      partially_handled_span_ids=set(),
  )


# Public contract for ``bqaa-revalidate-extractors``.
EXTRACTORS: dict = {
    "mako_decision": extract_mako_decision_event,
}
