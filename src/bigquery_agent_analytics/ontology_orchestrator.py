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

"""High-level orchestrator for ontology-driven graph pipelines (V4).

Ties together all ontology phases into a single ``build_ontology_graph``
call:

1. Load the YAML spec (Phase 1–2)
2. Extract an ``ExtractedGraph`` via ``AI.GENERATE`` (Phase 3)
3. Create physical tables and materialize rows (Phase 4)
4. Create the BigQuery Property Graph (Phase 5)

Also provides ``compile_showcase_gql`` for generating GQL traversal
queries from the ontology spec.

Example usage::

    from bigquery_agent_analytics.ontology_orchestrator import (
        build_ontology_graph,
        compile_showcase_gql,
    )

    result = build_ontology_graph(
        session_ids=["sess-1", "sess-2"],
        spec_path="examples/ymgo_graph_spec.yaml",
        project_id="my-project",
        dataset_id="analytics",
        env="my-project.analytics",
    )
    print(result)

    gql = compile_showcase_gql(result["spec"], "my-project", "analytics")
    print(gql)
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from .ontology_models import GraphSpec
from .ontology_models import load_graph_spec

logger = logging.getLogger("bigquery_agent_analytics." + __name__)


# ------------------------------------------------------------------ #
# GQL Showcase Query                                                   #
# ------------------------------------------------------------------ #

_LINEAGE_GQL_TEMPLATE = """\
GRAPH `{graph_ref}`
MATCH
  ({prior_alias}:{entity_label})-[{edge_alias}:{edge_label}]->({current_alias}:{entity_label})
{where_clause}\
RETURN
  {return_columns}
ORDER BY {edge_alias}.event_time DESC
LIMIT @result_limit
"""

_SHOWCASE_GQL_TEMPLATE = """\
GRAPH `{graph_ref}`
MATCH
  ({src_alias}:{src_label})-[{edge_alias}:{edge_label}]->({dst_alias}:{dst_label})
{where_clause}\
RETURN
  {return_columns}
ORDER BY {order_column}
LIMIT @result_limit
"""


def compile_showcase_gql(
    spec: GraphSpec,
    project_id: str,
    dataset_id: str,
    graph_name: Optional[str] = None,
    relationship_name: Optional[str] = None,
    session_filter: bool = True,
) -> str:
  """Generate a GQL traversal query from the ontology spec.

  Produces a ``MATCH`` query for a single relationship, returning
  source node properties, edge properties, and destination node
  properties.  The ``LIMIT`` clause uses the ``@result_limit``
  query parameter (set at query execution time).

  Args:
      spec: The validated graph spec.
      project_id: GCP project ID.
      dataset_id: BigQuery dataset ID.
      graph_name: Override graph name (defaults to ``spec.name``).
      relationship_name: Which relationship to traverse.  Defaults
          to the first relationship in the spec.
      session_filter: If True, adds a ``WHERE`` clause filtering
          by ``@session_id`` parameter.

  Returns:
      A GQL query string.

  Raises:
      ValueError: If the spec has no relationships, or the named
          relationship is not found.
  """
  if not spec.relationships:
    raise ValueError("Cannot generate showcase GQL: spec has no relationships.")

  rel_map = {r.name: r for r in spec.relationships}
  if relationship_name:
    if relationship_name not in rel_map:
      raise ValueError(
          f"Relationship {relationship_name!r} not found in spec. "
          f"Available: {sorted(rel_map.keys())}."
      )
    rel = rel_map[relationship_name]
  else:
    rel = spec.relationships[0]

  entity_map = {e.name: e for e in spec.entities}
  src_entity = entity_map[rel.from_entity]
  dst_entity = entity_map[rel.to_entity]

  name = graph_name or spec.name
  graph_ref = f"{project_id}.{dataset_id}.{name}"

  # Build short aliases from entity names and deduplicate all three.
  src_alias = _short_alias(src_entity.name)
  dst_alias = _short_alias(dst_entity.name)
  edge_alias = _short_alias(rel.name, prefix="e")

  # Ensure no two aliases collide (src vs dst, edge vs src, edge vs dst).
  if dst_alias == src_alias:
    dst_alias = dst_alias + "2"
  if edge_alias == src_alias:
    edge_alias = edge_alias + "2"
  if edge_alias == dst_alias:
    edge_alias = edge_alias + "3"

  # WHERE clause.
  where_clause = ""
  if session_filter:
    where_clause = f"WHERE {src_alias}.session_id = @session_id\n"

  # RETURN columns: source PKs + properties, edge properties,
  # destination PKs + properties.
  return_cols = []
  for prop in src_entity.properties:
    return_cols.append(f"{src_alias}.{prop.name} AS src_{prop.name}")
  for prop in rel.properties:
    return_cols.append(f"{edge_alias}.{prop.name}")
  for prop in dst_entity.properties:
    return_cols.append(f"{dst_alias}.{prop.name} AS dst_{prop.name}")

  # Order by the source entity's first primary key.
  order_column = f"{src_alias}.{src_entity.keys.primary[0]}"

  return _SHOWCASE_GQL_TEMPLATE.format(
      graph_ref=graph_ref,
      src_alias=src_alias,
      src_label=src_entity.name,
      edge_alias=edge_alias,
      edge_label=rel.name,
      dst_alias=dst_alias,
      dst_label=dst_entity.name,
      where_clause=where_clause,
      return_columns=",\n  ".join(return_cols),
      order_column=order_column,
  )


def compile_lineage_gql(
    spec: GraphSpec,
    project_id: str,
    dataset_id: str,
    relationship_name: str,
    graph_name: Optional[str] = None,
    session_filter: bool = True,
) -> str:
  """Generate a GQL lineage traversal query for cross-session edges.

  Produces a ``MATCH`` query for a self-edge lineage relationship,
  returning prior-session properties, edge metadata (changed_properties,
  event_time), and current-session properties.

  Args:
      spec: The validated graph spec.
      project_id: GCP project ID.
      dataset_id: BigQuery dataset ID.
      relationship_name: The lineage relationship to traverse.
      graph_name: Override graph name (defaults to ``spec.name``).
      session_filter: If True, adds a ``WHERE`` clause filtering
          the current (destination) node by ``@session_id``.

  Returns:
      A GQL query string.

  Raises:
      ValueError: If the relationship is not found or is not a
          self-edge (from_entity != to_entity).
  """
  rel_map = {r.name: r for r in spec.relationships}
  if relationship_name not in rel_map:
    raise ValueError(
        f"Relationship {relationship_name!r} not found in spec. "
        f"Available: {sorted(rel_map.keys())}."
    )
  rel = rel_map[relationship_name]
  if rel.from_entity != rel.to_entity:
    raise ValueError(
        f"Relationship {relationship_name!r} is not a self-edge "
        f"(from={rel.from_entity!r}, to={rel.to_entity!r}). "
        f"Lineage GQL requires from_entity == to_entity."
    )

  entity_map = {e.name: e for e in spec.entities}
  entity = entity_map[rel.from_entity]

  name = graph_name or spec.name
  graph_ref = f"{project_id}.{dataset_id}.{name}"

  prior_alias = "prior"
  current_alias = "current"
  edge_alias = _short_alias(rel.name, prefix="e")

  where_clause = ""
  if session_filter:
    where_clause = f"WHERE {current_alias}.session_id = @session_id\n"

  return_cols = []
  for prop in entity.properties:
    return_cols.append(f"{prior_alias}.{prop.name} AS prior_{prop.name}")
  for prop in rel.properties:
    return_cols.append(f"{edge_alias}.{prop.name}")
  for prop in entity.properties:
    return_cols.append(f"{current_alias}.{prop.name} AS current_{prop.name}")

  return _LINEAGE_GQL_TEMPLATE.format(
      graph_ref=graph_ref,
      prior_alias=prior_alias,
      entity_label=entity.name,
      edge_alias=edge_alias,
      edge_label=rel.name,
      current_alias=current_alias,
      where_clause=where_clause,
      return_columns=",\n  ".join(return_cols),
  )


def _short_alias(name: str, prefix: str = "") -> str:
  """Derive a short alias from an entity/relationship name.

  Examples:
      ``mako_DecisionPoint`` → ``dp``
      ``sup_YahooAdUnit`` → ``yau``
      ``CandidateEdge`` → ``ece`` (with prefix ``e``)
  """
  # Split on underscores; skip the namespace prefix (e.g., "mako", "sup").
  parts = name.split("_")
  if len(parts) > 1:
    # Join the non-prefix segments back, then extract CamelCase initials.
    remainder = "".join(parts[1:])
  else:
    remainder = name

  # CamelCase: take lowercase first letters of uppercase segments.
  alias = "".join(c.lower() for c in remainder if c.isupper())
  if not alias:
    alias = remainder[:2].lower()
  return prefix + alias


# ------------------------------------------------------------------ #
# Orchestrator                                                         #
# ------------------------------------------------------------------ #


def build_ontology_graph(
    session_ids: list[str],
    spec_path: str,
    project_id: str,
    dataset_id: str,
    env: Optional[str] = None,
    graph_name: Optional[str] = None,
    table_id: str = "agent_events",
    endpoint: str = "gemini-2.5-flash",
    use_ai_generate: bool = True,
    location: Optional[str] = None,
) -> dict[str, Any]:
  """Run the full ontology graph pipeline end-to-end.

  1. Load the YAML spec.
  2. Extract an ``ExtractedGraph`` from agent telemetry.
  3. Create physical tables (if not exists).
  4. Materialize extracted nodes/edges into tables.
  5. Create the BigQuery Property Graph.

  Args:
      session_ids: Sessions to extract from.
      spec_path: Path to the YAML graph spec.
      project_id: GCP project ID.
      dataset_id: BigQuery dataset ID.
      env: Value for ``{{ env }}`` placeholder substitution.
      graph_name: Override the property graph name.
      table_id: Source telemetry table name.
      endpoint: AI.GENERATE model endpoint.
      use_ai_generate: If True, uses server-side AI extraction.
      location: BigQuery location.

  Returns:
      A dict with keys: ``spec``, ``graph``, ``tables_created``,
      ``rows_materialized``, ``property_graph_created``,
      ``graph_name``, ``graph_ref``.
  """
  from .ontology_graph import OntologyGraphManager
  from .ontology_materializer import OntologyMaterializer
  from .ontology_property_graph import OntologyPropertyGraphCompiler

  # 1. Load spec.
  spec = load_graph_spec(spec_path, env=env)
  name = graph_name or spec.name
  logger.info(
      "Loaded spec %r with %d entities, %d relationships.",
      spec.name,
      len(spec.entities),
      len(spec.relationships),
  )

  # 2. Extract graph.
  extractor = OntologyGraphManager(
      project_id=project_id,
      dataset_id=dataset_id,
      spec=spec,
      table_id=table_id,
      endpoint=endpoint,
      location=location,
  )
  graph = extractor.extract_graph(
      session_ids=session_ids,
      use_ai_generate=use_ai_generate,
  )
  logger.info(
      "Extracted %d nodes, %d edges.", len(graph.nodes), len(graph.edges)
  )

  # 3. Create tables.
  materializer = OntologyMaterializer(
      project_id=project_id,
      dataset_id=dataset_id,
      spec=spec,
      location=location,
  )
  tables_created = materializer.create_tables()
  logger.info("Tables created: %s", list(tables_created.keys()))

  # Validate that all required tables were created.
  expected_names = {e.name for e in spec.entities} | {
      r.name for r in spec.relationships
  }
  missing = expected_names - set(tables_created.keys())
  if missing:
    raise RuntimeError(
        f"Table creation incomplete — missing: {sorted(missing)}. "
        f"Created: {sorted(tables_created.keys())}. "
        "Cannot proceed with materialization."
    )

  # 4. Materialize.
  rows_materialized = materializer.materialize(graph, session_ids)
  logger.info("Rows materialized: %s", rows_materialized)

  # 5. Create property graph.
  compiler = OntologyPropertyGraphCompiler(
      project_id=project_id,
      dataset_id=dataset_id,
      spec=spec,
      location=location,
  )
  pg_created = compiler.create_property_graph(graph_name=name)

  graph_ref = f"{project_id}.{dataset_id}.{name}"
  logger.info("Property Graph %r created=%s.", graph_ref, pg_created)

  return {
      "spec": spec,
      "graph": graph,
      "tables_created": tables_created,
      "rows_materialized": rows_materialized,
      "property_graph_created": pg_created,
      "graph_name": name,
      "graph_ref": graph_ref,
  }
