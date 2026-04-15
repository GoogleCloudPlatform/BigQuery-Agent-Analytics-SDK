# src/bigquery_agent_analytics/extracted_models.py
"""Runtime containers for AI-extracted graph instances.

These models represent the output of the extraction pipeline — nodes,
edges, and property values extracted from agent telemetry by AI or
structured extractors. They are SDK-specific and have no upstream
equivalent in the ``bigquery_ontology`` package.
"""

from __future__ import annotations

from typing import Any

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


class ExtractedGraph(BaseModel):
  """A complete graph instance extracted from agent telemetry."""

  name: str = Field(description="Graph name from the spec.")
  nodes: list[ExtractedNode] = Field(
      default_factory=list, description="Extracted nodes."
  )
  edges: list[ExtractedEdge] = Field(
      default_factory=list, description="Extracted edges."
  )
