# src/bigquery_agent_analytics/extracted_models.py
"""Runtime containers for AI-extracted graph instances.

These models represent the output of the extraction pipeline — nodes,
edges, and property values extracted from agent telemetry by AI or
structured extractors. They are SDK-specific and have no upstream
equivalent in the ``bigquery_ontology`` package.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel
from pydantic import Field


class ExtractedProperty(BaseModel):
  """A single property value on an extracted node or edge."""

  name: str = Field(description="Property name.")
  value: Any = Field(description="Property value.")


class ExtractedNode(BaseModel):
  """A node instance extracted from agent telemetry."""

  node_id: str = Field(description="Unique node identifier.")
  entity_name: str = Field(description="Entity type from the spec.")
  labels: list[str] = Field(default_factory=list, description="Node labels.")
  properties: list[ExtractedProperty] = Field(
      default_factory=list, description="Property values."
  )


class ExtractedEdge(BaseModel):
  """An edge instance extracted from agent telemetry."""

  edge_id: str = Field(description="Unique edge identifier.")
  relationship_name: str = Field(description="Relationship type from the spec.")
  from_node_id: str = Field(description="Source node ID.")
  to_node_id: str = Field(description="Target node ID.")
  properties: list[ExtractedProperty] = Field(
      default_factory=list, description="Edge property values."
  )


DiagnosticCode = Literal[
    # Per-span — attributable from the structured-extraction pipeline.
    #
    # ``structured_unhandled`` carries a precise meaning: no
    # registered extractor was *invoked* for the span (its
    # ``event_type`` didn't match any key in the extractor
    # registry). An extractor that matched and returned an empty
    # ``StructuredExtractionResult()`` — e.g. a recognized event
    # whose content is missing a required field — is NOT
    # unhandled; that's a legitimate silent outcome and downstream
    # compiled-only failure semantics should not flip ``ok=false``
    # on it.
    "structured_fully_handled",
    "structured_partially_handled",
    "structured_unhandled",
    "extractor_exception",
    # Session-level — what is honestly knowable about the AI fallback
    # without span-attributed AI output. A future PR can add an
    # ``ai_handled`` per-span code if span provenance is added to the
    # ``_extract_via_ai_generate`` return path.
    "session_ai_fallback_attempted",
]


class ExtractionDiagnostic(BaseModel):
  """Per-span (or session-level) diagnostic emitted by the extraction
  pipeline when the caller opts into the diagnostics-emitting path
  (``extract_graph(..., run_structured=..., on_unhandled_span=...)``).

  Legacy callers using the bool-only surface
  (``extract_graph(session_ids, use_ai_generate=True/False)``) see an
  empty ``ExtractedGraph.diagnostics`` list — diagnostics are not
  emitted on the back-compat path, so the extraction semantics are
  unchanged. (``model_dump()`` does pick up the ``diagnostics``
  field as an additive key; see
  ``ExtractedGraph.diagnostics`` for the compatibility contract.)

  The diagnostic codes are deliberately narrow to what the
  ``run_structured_extractors`` framework can honestly attribute.
  ``ai_handled`` per span is intentionally NOT in the list because
  ``AI.GENERATE`` returns a graph, not a span-attributed result;
  ``session_ai_fallback_attempted`` is the session-level signal the
  call site can record from the call site itself.
  """

  diagnostic_code: DiagnosticCode = Field(
      description=(
          "Which diagnostic this is. Per-span codes attribute to a "
          "specific event; session_ai_fallback_attempted is the "
          "session-level signal."
      )
  )
  span_id: Optional[str] = Field(
      default=None,
      description=("Span ID for per-span codes. None for session-level codes."),
  )
  session_id: Optional[str] = Field(
      default=None,
      description=(
          "Session ID for session-level codes (currently just "
          "session_ai_fallback_attempted)."
      ),
  )
  event_type: Optional[str] = Field(
      default=None,
      description=(
          "Telemetry event_type for the span, when known. "
          "Populated for per-span codes; None for session-level "
          "codes."
      ),
  )
  detail: Optional[str] = Field(
      default=None,
      description=(
          "Free-form payload for the diagnostic. For "
          "extractor_exception, the captured exception text "
          "(``f'{type(exc).__name__}: {exc}'``). For other codes, "
          "typically None."
      ),
  )


class ExtractedGraph(BaseModel):
  """A complete graph instance extracted from agent telemetry."""

  name: str = Field(description="Graph name from the spec.")
  nodes: list[ExtractedNode] = Field(
      default_factory=list, description="Extracted nodes."
  )
  edges: list[ExtractedEdge] = Field(
      default_factory=list, description="Extracted edges."
  )
  diagnostics: list[ExtractionDiagnostic] = Field(
      default_factory=list,
      description=(
          "Per-span / session diagnostics emitted by the extraction "
          "pipeline when the caller opts into the diagnostics-"
          "emitting path. Empty list on the legacy bool surface so "
          "the extraction semantics are unchanged. Note: this is an "
          "additive Pydantic field, so ``model_dump()`` now includes "
          "``'diagnostics': []`` even for legacy callers — strict-"
          "shape JSON consumers should add a passthrough for the "
          "new key."
      ),
  )
