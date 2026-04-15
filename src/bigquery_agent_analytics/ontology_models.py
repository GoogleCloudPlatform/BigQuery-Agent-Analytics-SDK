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

"""Ontology graph models for configuration-driven context graphs.

Provides two model layers:

1. **Spec models** — Pydantic representations of the YAML ontology
   definition (``GraphSpec``, ``EntitySpec``, ``RelationshipSpec``, etc.).
2. **Extracted models** — Runtime containers for AI-extracted graph
   instances (``ExtractedGraph``, ``ExtractedNode``, ``ExtractedEdge``).

The ``load_graph_spec`` function reads a YAML file, resolves ``{{ env }}``
placeholders, flattens label-only inheritance, and validates referential
integrity before returning a ready-to-use ``GraphSpec``.

Example usage::

    from bigquery_agent_analytics.ontology_models import load_graph_spec

    spec = load_graph_spec("examples/ymgo_graph_spec.yaml", env="proj.ds")
    for entity in spec.entities:
        print(entity.name, entity.labels, entity.binding.source)
"""

from __future__ import annotations

import logging
from pathlib import Path
import re
from typing import Any, Optional

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
import yaml

logger = logging.getLogger("bigquery_agent_analytics." + __name__)

# ------------------------------------------------------------------ #
# Spec Models (YAML ontology definition)                               #
# ------------------------------------------------------------------ #


class PropertySpec(BaseModel):
  """Single property definition within an entity or relationship."""

  model_config = ConfigDict(extra="forbid")

  name: str = Field(description="Property name.")
  type: str = Field(description="Property type (string, int64, double, etc.).")
  description: str = Field(
      default="", description="Human-readable description."
  )


class KeySpec(BaseModel):
  """Primary key specification for an entity."""

  model_config = ConfigDict(extra="forbid")

  primary: list[str] = Field(description="Primary key column(s).", min_length=1)


class BindingSpec(BaseModel):
  """Physical BigQuery table binding."""

  model_config = ConfigDict(extra="forbid")

  source: str = Field(
      description="BigQuery table reference, may contain {{ env }}."
  )
  from_columns: Optional[list[str]] = Field(
      default=None,
      description="Join columns from the source entity.",
  )
  to_columns: Optional[list[str]] = Field(
      default=None,
      description="Join columns to the target entity.",
  )
  from_session_column: Optional[str] = Field(
      default=None,
      description=(
          "Edge column to use as session key for SOURCE endpoint. "
          "When set, this column maps to the source node's session_id "
          "key instead of the edge's own session_id. "
          "Used for cross-session lineage edges."
      ),
  )
  to_session_column: Optional[str] = Field(
      default=None,
      description=(
          "Edge column to use as session key for DESTINATION endpoint. "
          "When set, this column maps to the destination node's "
          "session_id key instead of the edge's own session_id. "
          "Used for cross-session lineage edges."
      ),
  )


class EntitySpec(BaseModel):
  """Node type definition in the ontology."""

  model_config = ConfigDict(extra="forbid")

  name: str = Field(description="Entity name (becomes node label).")
  description: str = Field(default="", description="Entity description.")
  extends: Optional[str] = Field(
      default=None,
      description="Parent label for label-only inheritance.",
  )
  binding: BindingSpec = Field(description="BigQuery table binding.")
  keys: KeySpec = Field(description="Key specification.")
  properties: list[PropertySpec] = Field(
      default_factory=list, description="Property definitions."
  )
  labels: list[str] = Field(
      default_factory=list,
      description="Resolved labels (populated during load).",
  )


class RelationshipSpec(BaseModel):
  """Edge type definition in the ontology."""

  model_config = ConfigDict(extra="forbid")

  name: str = Field(description="Relationship type name.")
  description: str = Field(default="", description="Relationship description.")
  from_entity: str = Field(description="Source entity name.")
  to_entity: str = Field(description="Target entity name.")
  binding: BindingSpec = Field(
      description="BigQuery table binding with join columns."
  )
  properties: list[PropertySpec] = Field(
      default_factory=list, description="Edge property definitions."
  )


class GraphSpec(BaseModel):
  """Top-level ontology graph specification parsed from YAML."""

  model_config = ConfigDict(extra="forbid")

  name: str = Field(description="Graph name.")
  entities: list[EntitySpec] = Field(
      default_factory=list, description="Entity definitions."
  )
  relationships: list[RelationshipSpec] = Field(
      default_factory=list, description="Relationship definitions."
  )


# ------------------------------------------------------------------ #
# Extracted Models (AI output / runtime instances)                     #
# ------------------------------------------------------------------ #


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


# ------------------------------------------------------------------ #
# Loading & Validation                                                 #
# ------------------------------------------------------------------ #


def _resolve_inheritance(spec: GraphSpec) -> GraphSpec:
  """Populate ``labels`` on each entity.

  For entities with ``extends``, labels are ``[name, extends]``.
  For entities without, labels are ``[name]``.

  This is label-only inheritance — no properties or bindings are
  copied from the parent.
  """
  for entity in spec.entities:
    if entity.extends:
      entity.labels = [entity.name, entity.extends]
    else:
      entity.labels = [entity.name]
  return spec


def _validate_graph_spec(spec: GraphSpec) -> None:
  """Validate referential integrity of a ``GraphSpec``.

  Raises:
      ValueError: On any of the following:
          - Duplicate entity names
          - Duplicate relationship names
          - Key columns not found in entity properties
          - Relationship ``from_entity`` or ``to_entity`` not in entities
          - Empty primary key list
          - Relationship ``from_columns`` and ``to_columns`` not both
            present, or mismatched lengths
          - Relationship ``from_columns`` not a subset of source entity
            primary keys
          - Relationship ``to_columns`` not a subset of target entity
            primary keys
  """
  entity_names = [e.name for e in spec.entities]

  # Duplicate entity names.
  seen: set[str] = set()
  for name in entity_names:
    if name in seen:
      raise ValueError(f"Duplicate entity name: {name!r}")
    seen.add(name)

  # Duplicate relationship names.
  seen = set()
  for rel in spec.relationships:
    if rel.name in seen:
      raise ValueError(f"Duplicate relationship name: {rel.name!r}")
    seen.add(rel.name)

  entity_map = {e.name: e for e in spec.entities}

  # Key columns must exist in properties.
  for entity in spec.entities:
    prop_names = {p.name for p in entity.properties}
    for key_col in entity.keys.primary:
      if key_col not in prop_names:
        raise ValueError(
            f"Entity {entity.name!r}: key column {key_col!r} "
            f"not found in properties."
        )

  # Relationship endpoint validation.
  for rel in spec.relationships:
    if rel.from_entity not in entity_map:
      raise ValueError(
          f"Relationship {rel.name!r}: from_entity "
          f"{rel.from_entity!r} is not a defined entity."
      )
    if rel.to_entity not in entity_map:
      raise ValueError(
          f"Relationship {rel.name!r}: to_entity "
          f"{rel.to_entity!r} is not a defined entity."
      )

    # Join column validation: both sides required together.
    has_from = rel.binding.from_columns is not None
    has_to = rel.binding.to_columns is not None
    if has_from != has_to:
      raise ValueError(
          f"Relationship {rel.name!r}: from_columns and to_columns "
          f"must both be present or both be absent."
      )

    if has_from and has_to:
      if len(rel.binding.from_columns) != len(rel.binding.to_columns):
        raise ValueError(
            f"Relationship {rel.name!r}: from_columns length "
            f"({len(rel.binding.from_columns)}) != to_columns length "
            f"({len(rel.binding.to_columns)})."
        )

      source_keys = set(entity_map[rel.from_entity].keys.primary)
      for col in rel.binding.from_columns:
        if col not in source_keys:
          raise ValueError(
              f"Relationship {rel.name!r}: from_column {col!r} "
              f"not in source entity {rel.from_entity!r} "
              f"primary keys {sorted(source_keys)}."
          )

      target_keys = set(entity_map[rel.to_entity].keys.primary)
      for col in rel.binding.to_columns:
        if col not in target_keys:
          raise ValueError(
              f"Relationship {rel.name!r}: to_column {col!r} "
              f"not in target entity {rel.to_entity!r} "
              f"primary keys {sorted(target_keys)}."
          )

    # Session column override validation: both required together.
    has_from_session = rel.binding.from_session_column is not None
    has_to_session = rel.binding.to_session_column is not None
    if has_from_session != has_to_session:
      raise ValueError(
          f"Relationship {rel.name!r}: from_session_column and "
          f"to_session_column must both be present or both be absent."
      )

    if has_from_session:
      rel_prop_names = {p.name for p in rel.properties}
      if rel.binding.from_session_column not in rel_prop_names:
        raise ValueError(
            f"Relationship {rel.name!r}: from_session_column "
            f"{rel.binding.from_session_column!r} not found in "
            f"relationship properties."
        )
      if rel.binding.to_session_column not in rel_prop_names:
        raise ValueError(
            f"Relationship {rel.name!r}: to_session_column "
            f"{rel.binding.to_session_column!r} not found in "
            f"relationship properties."
        )


def load_graph_spec_from_string(
    yaml_string: str,
    env: Optional[str] = None,
) -> GraphSpec:
  """Parse a YAML string into a validated ``GraphSpec``.

  Args:
      yaml_string: Raw YAML content.
      env: If provided, replaces ``{{ env }}`` placeholders in all
          binding source references. Simple regex substitution — no
          Jinja2 dependency.

  Returns:
      A validated ``GraphSpec`` with inheritance resolved.

  Raises:
      ValueError: On validation failures.
      yaml.YAMLError: On malformed YAML.
  """
  if env is not None:
    yaml_string = re.sub(r"\{\{\s*env\s*\}\}", env, yaml_string)

  data = yaml.safe_load(yaml_string)
  graph_data = data["graph"]
  spec = GraphSpec(**graph_data)
  _resolve_inheritance(spec)
  _validate_graph_spec(spec)
  return spec


def load_graph_spec(
    path: str,
    env: Optional[str] = None,
) -> GraphSpec:
  """Load and validate a ``GraphSpec`` from a YAML file.

  Args:
      path: Path to the YAML file.
      env: If provided, replaces ``{{ env }}`` placeholders.

  Returns:
      A validated ``GraphSpec`` with inheritance resolved.
  """
  yaml_string = Path(path).read_text(encoding="utf-8")
  return load_graph_spec_from_string(yaml_string, env=env)


def load_from_ontology_binding(
    ontology_path: str,
    binding_path: str,
    lineage_config: Optional[dict] = None,
) -> GraphSpec:
  """Load a ``GraphSpec`` from separated ontology + binding YAML files.

  Uses the upstream ``bigquery_ontology`` loaders to parse and validate
  the ontology and binding, then converts them to the SDK's ``GraphSpec``
  via the runtime spec adapter. This is the dual-loader entry point for
  the migration to the upstream ontology contract.

  ``load_graph_spec()`` continues to work for combined GraphSpec YAML.
  This function adds a second loading path for the separated format
  without breaking the existing one.

  Args:
      ontology_path: Path to a ``*.ontology.yaml`` file.
      binding_path: Path to a ``*.binding.yaml`` file.
      lineage_config: Optional dict mapping relationship names to
          ``LineageEdgeConfig`` for SDK-specific cross-session lineage.

  Returns:
      A validated ``GraphSpec`` with inheritance resolved.

  Raises:
      ImportError: If the ``bigquery_ontology`` package is not available.
      ValueError: On validation failures or conversion errors.
  """
  from bigquery_ontology import load_binding
  from bigquery_ontology import load_ontology

  from .runtime_spec import graph_spec_from_ontology_binding

  ontology = load_ontology(ontology_path)
  binding = load_binding(binding_path, ontology=ontology)
  spec = graph_spec_from_ontology_binding(
      ontology, binding, lineage_config=lineage_config
  )
  _resolve_inheritance(spec)
  _validate_graph_spec(spec)
  return spec
