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

"""Runtime fallback wiring for compiled structured extractors
(issue #75 Milestone C2.b).

Wraps a compiled extractor with the validator from #76 and a
*fallback* extractor (the existing handwritten or
``AI.GENERATE`` path). When the compiled extractor produces
output that crashes, doesn't fit the contract, or violates the
ontology in ways that can't be salvaged, the wrapper substitutes
the fallback's output. When the violations are pinpointable to
specific nodes / edges, the wrapper drops just those elements
and downgrades the event's span-handling so the AI transcript
still sees the source span and can recover the missing pieces.

The decision tree in :func:`run_with_fallback`:

1. Compiled extractor raises, or returns a non-
   :class:`StructuredExtractionResult` value
   → ``fallback_for_event`` (call fallback, return its output;
   the exception type+message is captured in the audit record).
2. Validate the compiled output via
   :func:`validate_extracted_graph`. No failures
   → ``compiled_unchanged`` (pass compiled output through).
3. Any ``EVENT``-scope failure, or any failure missing both a
   ``node_id`` and an ``edge_id`` we can pinpoint
   → ``fallback_for_event``.
4. Otherwise, every failure is pinpointable
   → ``compiled_filtered``: drop the offending nodes / edges,
   orphan-clean any edge that pointed at a dropped node, **and
   downgrade the event's span_id from
   ``fully_handled_span_ids`` to ``partially_handled_span_ids``**.
   The span-handling downgrade is what makes per-element
   fallback real in the existing runtime: the compiled output
   contributes the valid structured pieces, and AI still sees
   the source span for the missing ones.

Out of scope (deferred to other C2 sub-PRs):

* Validating the *fallback* output. The fallback path is the
  existing baseline (handwritten extractor or ``AI.GENERATE``);
  if it ever produces bad output, the runtime has bigger
  problems than this wrapper can solve.
* Catching exceptions from the *fallback* extractor. Same
  reasoning — the fallback is presumed correct. Exceptions from
  the fallback propagate to the caller, matching existing
  runtime behavior.
* Per-property salvage (drop one property, keep the node).
  ``FIELD``-scope failures drop the whole containing node /
  edge — conservative, simpler to reason about, and matches
  what the validator can attribute via ``node_id`` / ``edge_id``.
* The actual orchestrator call-site swap (where in the
  ``ontology_graph.py`` pipeline this wrapper gets called). C2.c.

#76's validator currently emits only ``NODE`` / ``FIELD`` /
``EDGE`` scopes. ``FallbackScope.EVENT`` is reserved for this
runtime layer; the wrapper handles it defensively but doesn't
imply #76 will ever produce it on its own.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Callable, Optional

from ..extracted_models import ExtractedEdge
from ..extracted_models import ExtractedGraph
from ..extracted_models import ExtractedNode
from ..graph_validation import FallbackScope
from ..graph_validation import validate_extracted_graph
from ..graph_validation import ValidationFailure
from ..graph_validation import ValidationReport
from ..structured_extraction import StructuredExtractionResult

_DECISION_COMPILED_UNCHANGED = "compiled_unchanged"
_DECISION_COMPILED_FILTERED = "compiled_filtered"
_DECISION_FALLBACK_FOR_EVENT = "fallback_for_event"


@dataclasses.dataclass(frozen=True)
class FallbackOutcome:
  """Outcome of one :func:`run_with_fallback` call.

  Fields:

  * ``result`` — the :class:`StructuredExtractionResult` the
    runtime should use. Always populated, regardless of decision.
  * ``decision`` — one of ``"compiled_unchanged"`` /
    ``"compiled_filtered"`` / ``"fallback_for_event"``. Stable
    string so telemetry can group on it.
  * ``compiled_exception`` — ``"<ExceptionType>: <message>"``
    when the compiled extractor raised, otherwise ``None``.
    Wrong-return-type cases (which don't raise) are captured as
    ``"WrongReturnType: <type-name>"`` here too.
  * ``dropped_node_ids`` — node IDs removed from the compiled
    output by per-element filtering; populated only on
    ``compiled_filtered``.
  * ``dropped_edge_ids`` — edge IDs removed from the compiled
    output, including both direct-failure drops and orphan-
    cleanup drops (edges that referenced a dropped node).
  * ``validation_failures`` — every failure
    :func:`validate_extracted_graph` produced on the compiled
    output. Always populated when validation ran (decisions
    ``compiled_filtered`` and ``fallback_for_event`` triggered
    by validation); empty for ``compiled_unchanged`` and for
    ``fallback_for_event`` triggered by exception / wrong type.
  """

  result: StructuredExtractionResult
  decision: str
  compiled_exception: Optional[str] = None
  dropped_node_ids: tuple[str, ...] = ()
  dropped_edge_ids: tuple[str, ...] = ()
  validation_failures: tuple[ValidationFailure, ...] = ()


def run_with_fallback(
    *,
    event: dict,
    spec: Any,
    resolved_graph: Any,
    compiled_extractor: Callable[[dict, Any], StructuredExtractionResult],
    fallback_extractor: Callable[[dict, Any], StructuredExtractionResult],
) -> FallbackOutcome:
  """Run *compiled_extractor* on *event* with the validator from
  #76 as the safety net; substitute *fallback_extractor* per the
  decision tree at module scope.

  Args:
    event: One telemetry event dict (the same shape that
      ``run_structured_extractors`` hands an extractor).
    spec: Forwarded to both extractors as their ``spec``
      argument. Treated as opaque here.
    resolved_graph: The :class:`ResolvedGraph` the validator
      should compare against. Passed to
      :func:`validate_extracted_graph`.
    compiled_extractor: The compiled extractor whose output the
      wrapper validates. Required.
    fallback_extractor: The fallback extractor the wrapper calls
      when the compiled extractor's output isn't safe to keep.
      Typically the handwritten extractor (e.g.,
      :func:`extract_bka_decision_event`) or an
      ``AI.GENERATE``-backed adapter. Required. Exceptions from
      this extractor propagate; the wrapper does not catch them.

  Returns:
    A populated :class:`FallbackOutcome`. The wrapper itself
    never raises on compiled-extractor or validator failure;
    it only propagates ``fallback_extractor`` exceptions
    upward.
  """
  # Stage 1: run compiled, catch shape failures.
  compiled_exception: Optional[str] = None
  compiled_result: Optional[StructuredExtractionResult] = None
  try:
    raw = compiled_extractor(event, spec)
  # ``Exception`` covers ordinary errors. ``SystemExit`` is a
  # ``BaseException`` subclass but it's exactly the kind of
  # thing a malicious or buggy bundle might raise to tear down
  # the runtime — capture it as a fallback signal instead, the
  # same way C2.a's loader catches ``BaseException`` at import
  # time. ``KeyboardInterrupt`` is intentionally *not* caught
  # so operator cancellation still works.
  except (Exception, SystemExit) as exc:  # noqa: BLE001 — record + fall back
    compiled_exception = f"{type(exc).__name__}: {exc}"
  else:
    if not isinstance(raw, StructuredExtractionResult):
      compiled_exception = f"WrongReturnType: {type(raw).__name__}"
    else:
      # Validate span-handling internals before treating the
      # result as well-formed. ``StructuredExtractionResult`` is
      # a ``@dataclass`` with no runtime type validation, so
      # ``fully_handled_span_ids=None`` / =``"span1"`` /
      # =``[1, 2]`` all pass through field assignment. Without
      # this check, those values either leak downstream (the
      # ``compiled_unchanged`` path) or break the filtered path's
      # ``set(...)`` coercion at span-handling-downgrade time.
      span_problem = _check_span_set_shape(
          raw.fully_handled_span_ids, "fully_handled_span_ids"
      ) or _check_span_set_shape(
          raw.partially_handled_span_ids, "partially_handled_span_ids"
      )
      if span_problem is not None:
        compiled_exception = f"MalformedResultInternals: {span_problem}"
      else:
        compiled_result = raw

  if compiled_result is None:
    # Compiled crashed or returned wrong shape — fall back for
    # the whole event. ``validation_failures`` stays empty
    # (we never got far enough to validate anything).
    return FallbackOutcome(
        result=fallback_extractor(event, spec),
        decision=_DECISION_FALLBACK_FOR_EVENT,
        compiled_exception=compiled_exception,
    )

  # Stage 2: validate compiled output via #76. Building
  # ``ExtractedGraph`` runs Pydantic validation on every node /
  # edge entry — if the compiled extractor returned a
  # ``StructuredExtractionResult`` whose internals contain dicts
  # or other non-``ExtractedNode`` items, that construction (or
  # the validator itself) raises before producing a report.
  # Catch that and treat it as a fallback signal — otherwise the
  # wrapper's "never raises on compiled / validator failure"
  # contract leaks.
  try:
    graph = ExtractedGraph(
        name="runtime_fallback",
        nodes=list(compiled_result.nodes),
        edges=list(compiled_result.edges),
    )
    report: ValidationReport = validate_extracted_graph(resolved_graph, graph)
  except Exception as exc:  # noqa: BLE001 — record + fall back
    return FallbackOutcome(
        result=fallback_extractor(event, spec),
        decision=_DECISION_FALLBACK_FOR_EVENT,
        compiled_exception=(
            f"MalformedResultInternals: {type(exc).__name__}: {exc}"
        ),
    )
  failures = report.failures

  if not failures:
    return FallbackOutcome(
        result=compiled_result,
        decision=_DECISION_COMPILED_UNCHANGED,
    )

  # Stage 3: any EVENT-scope failure or unpinpointable failure
  # → fall back for the whole event. Pinpointability is
  # *scope-specific*: NODE pinpointable iff ``node_id`` is set;
  # EDGE iff ``edge_id`` is set; FIELD iff either is set
  # (the validator attaches FIELD failures to whichever
  # container holds the offending property).
  has_event_scope = any(f.scope is FallbackScope.EVENT for f in failures)
  has_unpinpointable = any(
      f.scope is not FallbackScope.EVENT and not _is_failure_pinpointable(f)
      for f in failures
  )
  if has_event_scope or has_unpinpointable:
    return FallbackOutcome(
        result=fallback_extractor(event, spec),
        decision=_DECISION_FALLBACK_FOR_EVENT,
        validation_failures=failures,
    )

  # Stage 4: every failure is pinpointable → filter the
  # compiled result and downgrade span-handling.
  return _build_filtered_outcome(
      event=event,
      compiled_result=compiled_result,
      failures=failures,
  )


# ------------------------------------------------------------------ #
# Helpers                                                              #
# ------------------------------------------------------------------ #


def _is_failure_pinpointable(failure: ValidationFailure) -> bool:
  """Whether *failure* identifies a specific element the wrapper
  can drop. Per scope:

  * ``NODE`` — pinpointable iff ``node_id`` is set.
  * ``EDGE`` — pinpointable iff ``edge_id`` is set. (#76's
    ``missing_endpoint_key`` populates *both* ``node_id`` (the
    referenced endpoint) and ``edge_id`` (the offending edge);
    the right thing to drop is the edge, not the endpoint.)
  * ``FIELD`` — pinpointable iff *either* is set (the validator
    attaches FIELD failures to whichever container — node or
    edge — holds the offending property).
  * ``EVENT`` — handled at the higher gate before this is
    called, so always returns ``False`` here.
  """
  if failure.scope is FallbackScope.NODE:
    return bool(failure.node_id)
  if failure.scope is FallbackScope.EDGE:
    return bool(failure.edge_id)
  if failure.scope is FallbackScope.FIELD:
    return bool(failure.node_id) or bool(failure.edge_id)
  return False


def _build_filtered_outcome(
    *,
    event: dict,
    compiled_result: StructuredExtractionResult,
    failures: tuple[ValidationFailure, ...],
) -> FallbackOutcome:
  """Drop offending nodes/edges, orphan-clean dependent edges,
  downgrade span-handling. Caller guarantees every failure is
  pinpointable (has a usable ``node_id`` or ``edge_id`` for its
  scope per :func:`_is_failure_pinpointable`)."""
  # Switch on scope (not on which ID happens to be set).
  # ``NODE`` always drops by ``node_id``; ``EDGE`` always drops by
  # ``edge_id`` — even when ``node_id`` is also populated, as in
  # #76's ``missing_endpoint_key`` failure where ``node_id``
  # holds the *referenced* endpoint id and the actual fix is the
  # edge. ``FIELD`` drops the containing element, preferring the
  # edge when both IDs are set (the property literally lives on
  # the edge in that case).
  drop_node_ids: set[str] = set()
  drop_edge_ids: set[str] = set()
  for failure in failures:
    if failure.scope is FallbackScope.NODE and failure.node_id:
      drop_node_ids.add(failure.node_id)
    elif failure.scope is FallbackScope.EDGE and failure.edge_id:
      drop_edge_ids.add(failure.edge_id)
    elif failure.scope is FallbackScope.FIELD:
      if failure.edge_id:
        drop_edge_ids.add(failure.edge_id)
      elif failure.node_id:
        drop_node_ids.add(failure.node_id)
    # Other shapes were filtered out by the has_unpinpointable
    # gate at the caller.

  surviving_nodes: list[ExtractedNode] = [
      n for n in compiled_result.nodes if n.node_id not in drop_node_ids
  ]

  # Orphan-cleanup: edges that referenced a dropped node also
  # come out, even if those edges weren't directly named in any
  # failure. Without this the filtered output would have edges
  # pointing at nodes that don't exist — exactly the
  # ``unresolved_endpoint`` shape the validator already rejects,
  # which would re-fail on any future revalidation.
  surviving_edges: list[ExtractedEdge] = []
  orphan_edge_ids: set[str] = set()
  for edge in compiled_result.edges:
    if edge.edge_id in drop_edge_ids:
      continue
    if edge.from_node_id in drop_node_ids or edge.to_node_id in drop_node_ids:
      orphan_edge_ids.add(edge.edge_id)
      continue
    surviving_edges.append(edge)

  all_dropped_edge_ids = drop_edge_ids | orphan_edge_ids

  # Span-handling downgrade — load-bearing for C2.b. If we drop
  # any compiled-output element from this event, the AI
  # transcript needs to keep seeing the source span so it can
  # recover the missing pieces. ``fully_handled_span_ids`` means
  # "exclude this span from AI input"; leaving the event's
  # span fully-handled would make the dropped facts
  # unrecoverable.
  fully = set(compiled_result.fully_handled_span_ids)
  partial = set(compiled_result.partially_handled_span_ids)
  span_id = event.get("span_id") if isinstance(event, dict) else None
  if isinstance(span_id, str) and span_id:
    fully.discard(span_id)
    partial.add(span_id)

  filtered = StructuredExtractionResult(
      nodes=surviving_nodes,
      edges=surviving_edges,
      fully_handled_span_ids=fully,
      partially_handled_span_ids=partial,
  )

  return FallbackOutcome(
      result=filtered,
      decision=_DECISION_COMPILED_FILTERED,
      dropped_node_ids=tuple(sorted(drop_node_ids)),
      dropped_edge_ids=tuple(sorted(all_dropped_edge_ids)),
      validation_failures=failures,
  )


def _check_span_set_shape(value: Any, field_name: str) -> Optional[str]:
  """Reject malformed span-handling containers.

  ``StructuredExtractionResult.fully_handled_span_ids`` and
  ``partially_handled_span_ids`` are declared as ``set[str]``,
  but the dataclass doesn't enforce the type at runtime — a
  compiled extractor can return ``None``, a raw string, a list,
  or a set with non-string entries, and the dataclass happily
  stores it.

  Required shape: a ``set`` or ``frozenset`` whose elements are
  all non-empty strings. Strings themselves are rejected even
  though they're iterable — ``set("span1") == {"s", "p", "a",
  "n", "1"}`` is the corrupt-coercion shape this check exists
  to prevent. Lists / tuples are rejected for the same reason
  the field declaration says ``set``: callers downstream merge
  these via union, and accidentally storing a list would
  silently change merge semantics.

  Returns ``None`` if the shape is valid, or a short detail
  string naming the offending field and observed type.
  """
  if not isinstance(value, (set, frozenset)):
    return (
        f"{field_name} must be a set/frozenset of strings; got "
        f"{type(value).__name__}={value!r}"
    )
  for item in value:
    if not isinstance(item, str):
      return (
          f"{field_name} contains a non-string entry "
          f"{type(item).__name__}={item!r}"
      )
    if not item:
      return f"{field_name} contains an empty-string entry"
  return None
