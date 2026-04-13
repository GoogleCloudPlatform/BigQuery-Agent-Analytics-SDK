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

"""Dynamic Property Graph DDL transpiler for ontology-driven graphs (V4).

Transpiles a ``GraphSpec`` into BigQuery ``CREATE OR REPLACE PROPERTY
GRAPH`` DDL, mapping entities to ``NODE TABLES`` and relationships to
``EDGE TABLES``.

This module sits beside the V3 ``ContextGraphManager`` DDL generation
and reuses the same DDL style, but derives the statement entirely from
the YAML ontology rather than hard-coding table structures.

Example usage::

    from bigquery_agent_analytics.ontology_models import load_graph_spec
    from bigquery_agent_analytics.ontology_property_graph import (
        OntologyPropertyGraphCompiler,
    )

    spec = load_graph_spec("examples/ymgo_graph_spec.yaml", env="p.d")
    compiler = OntologyPropertyGraphCompiler(
        project_id="my-project",
        dataset_id="analytics",
        spec=spec,
    )
    print(compiler.get_ddl())
    compiler.create_property_graph()
"""

from __future__ import annotations

import logging
from typing import Optional

from google.cloud import bigquery

from .ontology_models import EntitySpec
from .ontology_models import GraphSpec
from .ontology_models import RelationshipSpec

logger = logging.getLogger("bigquery_agent_analytics." + __name__)


# ------------------------------------------------------------------ #
# Table reference helper                                               #
# ------------------------------------------------------------------ #


def _resolve_table_ref(
    binding_source: str,
    project_id: str,
    dataset_id: str,
) -> str:
  """Resolve a binding source to a fully qualified table reference."""
  if binding_source.count(".") >= 2:
    return binding_source
  return f"{project_id}.{dataset_id}.{binding_source}"


# ------------------------------------------------------------------ #
# NODE TABLE clause                                                    #
# ------------------------------------------------------------------ #


def compile_node_table_clause(
    entity: EntitySpec,
    project_id: str,
    dataset_id: str,
) -> str:
  """Generate a ``NODE TABLES`` entry for one entity.

  Args:
      entity: The entity spec.
      project_id: GCP project ID.
      dataset_id: BigQuery dataset ID.

  Returns:
      A SQL fragment like::

          `p.d.table` AS EntityName
            KEY (pk1, pk2)
            LABEL EntityName
            PROPERTIES (col1, col2, session_id, extracted_at)
  """
  table_ref = _resolve_table_ref(entity.binding.source, project_id, dataset_id)

  # Node KEY includes session_id so that the same business entity in
  # different sessions produces distinct graph nodes.
  key_cols = ", ".join([*entity.keys.primary, "session_id"])

  # Labels: entity may have multiple from extends (label inheritance).
  label_lines = "\n      ".join(f"LABEL {lbl}" for lbl in entity.labels)

  # Properties: all entity property columns plus session_id and
  # extracted_at metadata.  BigQuery Property Graph only exposes
  # columns listed in PROPERTIES to GQL queries — KEY columns are
  # NOT automatically queryable, so we include everything here.
  prop_names = [p.name for p in entity.properties]
  prop_names.extend(["session_id", "extracted_at"])
  props_str = ",\n        ".join(prop_names)

  return (
      f"    `{table_ref}` AS {entity.name}\n"
      f"      KEY ({key_cols})\n"
      f"      {label_lines}\n"
      f"      PROPERTIES (\n"
      f"        {props_str}\n"
      f"      )"
  )


# ------------------------------------------------------------------ #
# EDGE TABLE clause                                                    #
# ------------------------------------------------------------------ #


def compile_edge_table_clause(
    rel: RelationshipSpec,
    spec: GraphSpec,
    project_id: str,
    dataset_id: str,
) -> str:
  """Generate an ``EDGE TABLES`` entry for one relationship.

  The ``SOURCE KEY`` uses the relationship's ``from_columns``
  (defaulting to the source entity's full primary key) and
  references the source node table's key.  Likewise for
  ``DESTINATION KEY``.

  BigQuery Property Graph requires that ``SOURCE KEY`` columns
  match the referenced ``NODE TABLE KEY`` exactly.  Subset
  bindings (``from_columns`` narrower than the entity's primary
  key) are valid for table materialization but produce invalid
  property-graph DDL.  This function raises ``ValueError`` if
  it detects a mismatch.

  Args:
      rel: The relationship spec.
      spec: The parent graph spec (for entity lookups).
      project_id: GCP project ID.
      dataset_id: BigQuery dataset ID.

  Returns:
      A SQL fragment for the EDGE TABLES block.

  Raises:
      ValueError: If ``from_columns`` or ``to_columns`` do not
          match the referenced entity's full primary key.
  """
  entity_map = {e.name: e for e in spec.entities}
  src = entity_map[rel.from_entity]
  tgt = entity_map[rel.to_entity]

  table_ref = _resolve_table_ref(rel.binding.source, project_id, dataset_id)

  from_cols = rel.binding.from_columns or list(src.keys.primary)
  to_cols = rel.binding.to_columns or list(tgt.keys.primary)

  # Validate that binding columns match the entity's full PK.
  # Property Graph requires SOURCE/DESTINATION KEY to exactly
  # match the referenced NODE TABLE KEY.
  if list(from_cols) != list(src.keys.primary):
    raise ValueError(
        f"Relationship {rel.name!r}: from_columns {list(from_cols)} "
        f"do not match {rel.from_entity} primary key "
        f"{list(src.keys.primary)}. Property Graph DDL requires "
        f"exact key matching. Subset bindings are supported for "
        f"materialization but not for Property Graph compilation."
    )
  if list(to_cols) != list(tgt.keys.primary):
    raise ValueError(
        f"Relationship {rel.name!r}: to_columns {list(to_cols)} "
        f"do not match {rel.to_entity} primary key "
        f"{list(tgt.keys.primary)}. Property Graph DDL requires "
        f"exact key matching. Subset bindings are supported for "
        f"materialization but not for Property Graph compilation."
    )

  # Session key columns for SOURCE and DESTINATION endpoints.
  # Default: edge's own session_id (V4 behavior).
  # Override: binding.from_session_column / to_session_column (V5 lineage).
  src_session_col = rel.binding.from_session_column or "session_id"
  dst_session_col = rel.binding.to_session_column or "session_id"

  # Edge KEY = from_columns + to_columns + session columns (deduplicated).
  edge_key_cols = list(from_cols)
  for col in to_cols:
    if col not in edge_key_cols:
      edge_key_cols.append(col)
  for col in [src_session_col, dst_session_col, "session_id"]:
    if col not in edge_key_cols:
      edge_key_cols.append(col)
  edge_key_str = ", ".join(edge_key_cols)

  # SOURCE KEY uses src_session_col mapped to node's session_id key.
  src_key_str = ", ".join([*from_cols, src_session_col])
  src_ref_str = ", ".join([*src.keys.primary, "session_id"])

  # DESTINATION KEY uses dst_session_col mapped to node's session_id key.
  dst_key_str = ", ".join([*to_cols, dst_session_col])
  dst_ref_str = ", ".join([*tgt.keys.primary, "session_id"])

  # Properties: relationship-specific properties + metadata.
  # Exclude columns already in the edge KEY — except session column
  # overrides, which must stay in PROPERTIES so they are queryable in
  # GQL (BigQuery Property Graph does not auto-expose KEY columns).
  key_set = set(edge_key_cols)
  session_override_cols = set()
  if rel.binding.from_session_column:
    session_override_cols.add(rel.binding.from_session_column)
  if rel.binding.to_session_column:
    session_override_cols.add(rel.binding.to_session_column)
  prop_names = [
      p.name
      for p in rel.properties
      if p.name not in key_set or p.name in session_override_cols
  ]
  prop_names.append("extracted_at")
  props_str = ",\n        ".join(prop_names)

  return (
      f"    `{table_ref}` AS {rel.name}\n"
      f"      KEY ({edge_key_str})\n"
      f"      SOURCE KEY ({src_key_str}) "
      f"REFERENCES {rel.from_entity} ({src_ref_str})\n"
      f"      DESTINATION KEY ({dst_key_str}) "
      f"REFERENCES {rel.to_entity} ({dst_ref_str})\n"
      f"      LABEL {rel.name}\n"
      f"      PROPERTIES (\n"
      f"        {props_str}\n"
      f"      )"
  )


# ------------------------------------------------------------------ #
# Full Property Graph DDL                                              #
# ------------------------------------------------------------------ #


def compile_property_graph_ddl(
    spec: GraphSpec,
    project_id: str,
    dataset_id: str,
    graph_name: Optional[str] = None,
) -> str:
  """Generate a complete ``CREATE OR REPLACE PROPERTY GRAPH`` DDL.

  Args:
      spec: The validated graph spec.
      project_id: GCP project ID.
      dataset_id: BigQuery dataset ID.
      graph_name: Override the graph name (defaults to ``spec.name``).

  Returns:
      The full DDL string.

  Raises:
      ValueError: If the spec has no entities.
  """
  if not spec.entities:
    raise ValueError(
        "Cannot generate Property Graph DDL: spec has no entities."
    )

  name = graph_name or spec.name
  graph_ref = f"{project_id}.{dataset_id}.{name}"

  node_clauses = []
  for entity in spec.entities:
    node_clauses.append(
        compile_node_table_clause(entity, project_id, dataset_id)
    )
  nodes_block = ",\n".join(node_clauses)

  edge_clauses = []
  for rel in spec.relationships:
    edge_clauses.append(
        compile_edge_table_clause(rel, spec, project_id, dataset_id)
    )

  parts = [
      f"CREATE OR REPLACE PROPERTY GRAPH `{graph_ref}`\n"
      f"  NODE TABLES (\n{nodes_block}\n  )",
  ]
  if edge_clauses:
    edges_block = ",\n".join(edge_clauses)
    parts.append(f"  EDGE TABLES (\n{edges_block}\n  )")

  return "\n".join(parts)


# ------------------------------------------------------------------ #
# OntologyPropertyGraphCompiler                                        #
# ------------------------------------------------------------------ #


class OntologyPropertyGraphCompiler:
  """Compiles ontology spec into BigQuery Property Graph DDL.

  Transpiles entities to ``NODE TABLES`` and relationships to
  ``EDGE TABLES``, referencing the physical tables from
  ``binding.source``.

  Args:
      project_id: GCP project ID.
      dataset_id: BigQuery dataset ID.
      spec: A validated ``GraphSpec``.
      bq_client: Optional pre-configured BigQuery client.
      location: BigQuery location.
  """

  def __init__(
      self,
      project_id: str,
      dataset_id: str,
      spec: GraphSpec,
      bq_client: Optional[bigquery.Client] = None,
      location: Optional[str] = None,
  ) -> None:
    self.project_id = project_id
    self.dataset_id = dataset_id
    self.spec = spec
    self.location = location
    self._bq_client = bq_client

  @property
  def bq_client(self) -> bigquery.Client:
    """Lazily initializes the BigQuery client."""
    if self._bq_client is None:
      kwargs: dict = {"project": self.project_id}
      if self.location:
        kwargs["location"] = self.location
      self._bq_client = bigquery.Client(**kwargs)
    return self._bq_client

  def get_ddl(self, graph_name: Optional[str] = None) -> str:
    """Return the ``CREATE OR REPLACE PROPERTY GRAPH`` DDL.

    Args:
        graph_name: Override the graph name (defaults to ``spec.name``).

    Returns:
        The full DDL string.
    """
    return compile_property_graph_ddl(
        self.spec, self.project_id, self.dataset_id, graph_name
    )

  def get_node_table_clause(self, entity_name: str) -> str:
    """Return the NODE TABLE clause for one entity.

    Args:
        entity_name: Name of the entity in the spec.

    Raises:
        ValueError: If the entity is not found.
    """
    entity_map = {e.name: e for e in self.spec.entities}
    if entity_name not in entity_map:
      raise ValueError(
          f"Entity {entity_name!r} not found in spec. "
          f"Available: {sorted(entity_map.keys())}."
      )
    return compile_node_table_clause(
        entity_map[entity_name], self.project_id, self.dataset_id
    )

  def get_edge_table_clause(self, rel_name: str) -> str:
    """Return the EDGE TABLE clause for one relationship.

    Args:
        rel_name: Name of the relationship in the spec.

    Raises:
        ValueError: If the relationship is not found.
    """
    rel_map = {r.name: r for r in self.spec.relationships}
    if rel_name not in rel_map:
      raise ValueError(
          f"Relationship {rel_name!r} not found in spec. "
          f"Available: {sorted(rel_map.keys())}."
      )
    return compile_edge_table_clause(
        rel_map[rel_name], self.spec, self.project_id, self.dataset_id
    )

  def create_property_graph(
      self,
      graph_name: Optional[str] = None,
  ) -> bool:
    """Execute the DDL to create the Property Graph in BigQuery.

    Args:
        graph_name: Override the graph name.

    Returns:
        True if successful, False otherwise.
    """
    ddl = self.get_ddl(graph_name)
    try:
      job = self.bq_client.query(ddl)
      job.result()
      name = graph_name or self.spec.name
      logger.info("Property Graph '%s' created successfully.", name)
      return True
    except Exception as e:
      logger.warning("Failed to create Property Graph: %s", e)
      return False
