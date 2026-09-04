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

"""Structured extraction registry for dedupe-safe context graph population.

Step 2 of the V5 Context Graph design: typed extractors that convert raw
agent telemetry events into ``ExtractedNode`` / ``ExtractedEdge`` instances
with explicit span-handling metadata.  Each extractor declares which spans
it fully or partially handles so downstream AI transcript construction can
skip already-covered data.

Typical usage::

    from bigquery_agent_analytics.structured_extraction import (
        run_structured_extractors,
        extract_bka_decision_event,
    )

    extractors = {'bka_decision': extract_bka_decision_event}
    result = run_structured_extractors(events, extractors, spec)
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from typing import Any, Callable, Optional

from bigquery_agent_analytics.extracted_models import ExtractedEdge
from bigquery_agent_analytics.extracted_models import ExtractedNode
from bigquery_agent_analytics.extracted_models import ExtractedProperty

# ------------------------------------------------------------------ #
# Data contracts                                                       #
# ------------------------------------------------------------------ #


@dataclass(frozen=True)
class ExtractorException:
  """Captured extractor exception.

  Emitted into :class:`StructuredExtractionResult` when
  ``run_structured_extractors`` is called with
  ``capture_extractor_exceptions=True``. The default path
  propagates extractor exceptions unchanged (see the contract
  notes on ``runtime_fallback.run_with_fallback`` and
  ``runtime_registry.WrappedRegistry`` — the diagnostics path
  is opt-in to preserve those documented contracts).
  """

  span_id: Optional[str]
  event_type: str
  detail: str  # f"{type(exc).__name__}: {exc}"


@dataclass
class StructuredExtractionResult:
  """Result of a single structured extractor run.

  Attributes:
    nodes: Extracted node instances.
    edges: Extracted edge instances.
    fully_handled_span_ids: Span IDs whose content is completely captured
        by the extracted nodes/edges and should be excluded from the AI
        transcript.
    partially_handled_span_ids: Span IDs whose content is only partially
        captured (e.g. free-text fields remain) and should be included in
        the AI transcript with an extraction hint.
    exceptions: Captured exceptions from individual extractor calls
        when ``run_structured_extractors`` ran with
        ``capture_extractor_exceptions=True``. Empty list on the
        default (propagating) path.
    invoked_span_ids: Span IDs for which a registered extractor was
        invoked (regardless of whether it produced output, returned
        empty, or raised). Populated by
        :func:`run_structured_extractors` for diagnostic emission;
        individual extractor implementations should not set this
        directly — the orchestrator owns it. The
        ``structured_unhandled`` diagnostic in ``extract_graph``
        uses this set to distinguish "no matching extractor" (a real
        coverage gap) from "extractor matched but emitted nothing"
        (a legitimate silent outcome, e.g. when a required field is
        missing from the event content).
  """

  nodes: list[ExtractedNode] = field(default_factory=list)
  edges: list[ExtractedEdge] = field(default_factory=list)
  fully_handled_span_ids: set[str] = field(default_factory=set)
  partially_handled_span_ids: set[str] = field(default_factory=set)
  exceptions: list[ExtractorException] = field(default_factory=list)
  invoked_span_ids: set[str] = field(default_factory=set)


# Type alias for extractor callables.
StructuredExtractor = Callable[[dict, Any], StructuredExtractionResult]


# ------------------------------------------------------------------ #
# Merge helper                                                         #
# ------------------------------------------------------------------ #


def merge_extraction_results(
    results: list[StructuredExtractionResult],
) -> StructuredExtractionResult:
  """Merge multiple extraction results into a single result.

  Nodes are deduplicated by ``node_id`` — when multiple results produce a
  node with the same ID the *last* occurrence wins.  Edges are simply
  concatenated (edge dedup is left to the downstream materialiser).
  Span-ID sets are unioned. Exception lists are concatenated.

  Args:
    results: Individual extraction results to merge.

  Returns:
    A single ``StructuredExtractionResult`` combining all inputs.
  """
  node_map: dict[str, ExtractedNode] = {}
  all_edges: list[ExtractedEdge] = []
  fully_handled: set[str] = set()
  partially_handled: set[str] = set()
  all_exceptions: list[ExtractorException] = []
  invoked: set[str] = set()

  for result in results:
    for node in result.nodes:
      node_map[node.node_id] = node
    all_edges.extend(result.edges)
    fully_handled |= result.fully_handled_span_ids
    partially_handled |= result.partially_handled_span_ids
    all_exceptions.extend(result.exceptions)
    invoked |= result.invoked_span_ids

  return StructuredExtractionResult(
      nodes=list(node_map.values()),
      edges=all_edges,
      fully_handled_span_ids=fully_handled,
      partially_handled_span_ids=partially_handled,
      exceptions=all_exceptions,
      invoked_span_ids=invoked,
  )


# ------------------------------------------------------------------ #
# Example extractor: BKA decision events                               #
# ------------------------------------------------------------------ #


def extract_bka_decision_event(
    event: dict,
    spec: Any,
) -> StructuredExtractionResult:
  """Extract a ``mako_DecisionPoint`` node from a BKA decision event.

  Looks for ``decision_id`` in the event's ``content`` dict.  If found,
  produces a node whose ``node_id`` is deterministic:

      ``{session_id}:mako_DecisionPoint:decision_id={value}``

  If the event also contains ``reasoning_text`` (unstructured free-text),
  the span is marked as *partially handled* so the AI transcript still
  includes it with an extraction hint.  Otherwise the span is *fully
  handled*.

  Args:
    event: Raw telemetry event dict.  Expected keys: ``span_id``,
        ``session_id``, ``content`` (a nested dict with at least
        ``decision_id``).
    spec: The active graph spec (unused by this extractor but
        required by the ``StructuredExtractor`` signature).

  Returns:
    A ``StructuredExtractionResult`` — empty if the event does not
    contain a ``decision_id``.
  """
  content = event.get('content')
  if not isinstance(content, dict):
    return StructuredExtractionResult()

  decision_id = content.get('decision_id')
  if decision_id is None:
    return StructuredExtractionResult()

  session_id = event.get('session_id', '')
  span_id = event.get('span_id', '')

  node_id = f'{session_id}:mako_DecisionPoint:decision_id={decision_id}'

  properties: list[ExtractedProperty] = [
      ExtractedProperty(name='decision_id', value=decision_id),
  ]

  # Carry over any additional structured fields from content.
  for key in ('outcome', 'confidence', 'alternatives_considered'):
    if key in content:
      properties.append(ExtractedProperty(name=key, value=content[key]))

  node = ExtractedNode(
      node_id=node_id,
      entity_name='mako_DecisionPoint',
      labels=['mako_DecisionPoint'],
      properties=properties,
  )

  has_reasoning_text = bool(content.get('reasoning_text'))

  if has_reasoning_text:
    fully_handled: set[str] = set()
    partially_handled: set[str] = {span_id} if span_id else set()
  else:
    fully_handled = {span_id} if span_id else set()
    partially_handled = set()

  return StructuredExtractionResult(
      nodes=[node],
      edges=[],
      fully_handled_span_ids=fully_handled,
      partially_handled_span_ids=partially_handled,
  )


# ------------------------------------------------------------------ #
# Runner                                                               #
# ------------------------------------------------------------------ #


def run_structured_extractors(
    events: list[dict],
    extractors: dict[str, StructuredExtractor],
    spec: Any,
    *,
    capture_extractor_exceptions: bool = False,
) -> StructuredExtractionResult:
  """Run registered extractors against a list of telemetry events.

  For each event whose ``event_type`` matches a key in *extractors*,
  the corresponding extractor is invoked.  All individual results are
  merged via :func:`merge_extraction_results`.

  Args:
    events: Raw telemetry event dicts.  Each must have an
        ``event_type`` key to match against the extractor registry.
    extractors: Mapping of ``event_type`` string to extractor callable.
    spec: The active graph spec forwarded to each extractor.
    capture_extractor_exceptions: When ``False`` (the default and the
        only behavior prior to issue #178), extractor exceptions
        propagate to the caller — this is the contract
        ``runtime_fallback.run_with_fallback`` and
        ``runtime_registry.WrappedRegistry`` rely on, so changing the
        default would silently break those propagate-exception
        contracts. When ``True``, extractor exceptions are caught,
        recorded in the returned result's ``exceptions`` list, and
        the loop continues so partial results still surface. Only the
        new diagnostics-emitting path in
        :func:`OntologyGraphManager.extract_graph` (with
        ``run_structured`` and ``on_unhandled_span`` set) opts into
        the True behavior.

  Returns:
    A single merged ``StructuredExtractionResult``. When
    ``capture_extractor_exceptions=True``, the result's
    ``exceptions`` list carries one entry per failed extractor call.
  """
  results: list[StructuredExtractionResult] = []
  exceptions: list[ExtractorException] = []
  invoked: set[str] = set()

  for event in events:
    event_type = event.get('event_type')
    if event_type is None:
      continue
    extractor = extractors.get(event_type)
    if extractor is None:
      continue
    # The orchestrator owns ``invoked_span_ids``. Record before the
    # call so a raising extractor still contributes its span to the
    # invoked set (the call WAS attempted; the diagnostic stream
    # surfaces it as ``extractor_exception``, not
    # ``structured_unhandled``).
    span_id = event.get('span_id')
    if span_id:
      invoked.add(span_id)
    if capture_extractor_exceptions:
      try:
        results.append(extractor(event, spec))
      except Exception as exc:  # noqa: BLE001 — by design when capturing
        exceptions.append(
            ExtractorException(
                span_id=span_id,
                event_type=event_type,
                detail=f'{type(exc).__name__}: {exc}',
            )
        )
    else:
      # Default path: exceptions propagate — the contract
      # ``runtime_fallback`` and ``runtime_registry`` rely on.
      results.append(extractor(event, spec))

  merged = (
      merge_extraction_results(results)
      if results
      else StructuredExtractionResult()
  )
  # Always attach the orchestrator's view of ``invoked_span_ids`` +
  # propagate any locally-caught exceptions (preserving any
  # exceptions an extractor result already carried — symmetric to
  # how nodes / edges / span_ids merge).
  return StructuredExtractionResult(
      nodes=merged.nodes,
      edges=merged.edges,
      fully_handled_span_ids=merged.fully_handled_span_ids,
      partially_handled_span_ids=merged.partially_handled_span_ids,
      exceptions=merged.exceptions + exceptions,
      invoked_span_ids=merged.invoked_span_ids | invoked,
  )
