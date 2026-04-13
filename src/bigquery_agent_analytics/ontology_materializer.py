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

"""Physical table materialization + routing for ontology-driven graphs.

Provides ``OntologyMaterializer`` — takes an ``ExtractedGraph`` produced
by ``OntologyGraphManager`` and persists nodes and edges into BigQuery
tables according to the ontology binding configuration.

Each entity maps to one physical table (``binding.source``), and each
relationship maps to one physical table.  Persistence follows the
same delete-then-insert idempotency pattern as V3's
``ContextGraphManager``.

Example usage::

    from bigquery_agent_analytics.ontology_models import load_graph_spec
    from bigquery_agent_analytics.ontology_graph import OntologyGraphManager
    from bigquery_agent_analytics.ontology_materializer import (
        OntologyMaterializer,
    )

    spec = load_graph_spec("examples/ymgo_graph_spec.yaml", env="p.d")
    mgr = OntologyGraphManager(
        project_id="my-project", dataset_id="analytics", spec=spec,
    )
    graph = mgr.extract_graph(session_ids=["sess-1"])

    mat = OntologyMaterializer(
        project_id="my-project", dataset_id="analytics", spec=spec,
    )
    mat.create_tables()
    result = mat.materialize(graph, session_ids=["sess-1"])
    print(result)  # {"mako_DecisionPoint": 3, "CandidateEdge": 5, ...}
"""

from __future__ import annotations

import datetime
import logging
from typing import Optional

from google.cloud import bigquery

from .ontology_models import EntitySpec
from .ontology_models import ExtractedEdge
from .ontology_models import ExtractedGraph
from .ontology_models import ExtractedNode
from .ontology_models import GraphSpec
from .ontology_models import PropertySpec
from .ontology_models import RelationshipSpec

logger = logging.getLogger("bigquery_agent_analytics." + __name__)

# ------------------------------------------------------------------ #
# Type mapping: YAML property types -> BQ DDL types                    #
# ------------------------------------------------------------------ #

_DDL_TYPE_MAP: dict[str, str] = {
    "string": "STRING",
    "int64": "INT64",
    "double": "FLOAT64",
    "float64": "FLOAT64",
    "bool": "BOOL",
    "boolean": "BOOL",
    "timestamp": "TIMESTAMP",
    "date": "DATE",
    "bytes": "BYTES",
}


def _ddl_type(yaml_type: str) -> str:
  """Map a YAML property type to a BQ DDL column type."""
  normalized = yaml_type.strip().lower()
  if normalized not in _DDL_TYPE_MAP:
    raise ValueError(
        f"Unsupported property type {yaml_type!r}. "
        f"Supported: {sorted(_DDL_TYPE_MAP.keys())}."
    )
  return _DDL_TYPE_MAP[normalized]


# ------------------------------------------------------------------ #
# DDL Generation                                                       #
# ------------------------------------------------------------------ #


def _entity_columns(entity: EntitySpec) -> dict[str, str]:
  """Return ordered ``{col_name: DDL_TYPE}`` for an entity spec."""
  cols: dict[str, str] = {}
  for prop in entity.properties:
    cols[prop.name] = _ddl_type(prop.type)
  cols.setdefault("session_id", "STRING")
  cols.setdefault("extracted_at", "TIMESTAMP")
  return cols


def _relationship_columns(
    rel: RelationshipSpec,
    spec: GraphSpec,
) -> dict[str, str]:
  """Return ordered ``{col_name: DDL_TYPE}`` for a relationship spec."""
  entity_map = {e.name: e for e in spec.entities}
  src = entity_map[rel.from_entity]
  tgt = entity_map[rel.to_entity]
  src_prop_map = {p.name: p for p in src.properties}
  tgt_prop_map = {p.name: p for p in tgt.properties}

  cols: dict[str, str] = {}
  from_cols = rel.binding.from_columns or src.keys.primary
  for col in from_cols:
    cols[col] = _ddl_type(src_prop_map[col].type)
  to_cols = rel.binding.to_columns or tgt.keys.primary
  for col in to_cols:
    if col not in cols:
      cols[col] = _ddl_type(tgt_prop_map[col].type)
  for prop in rel.properties:
    if prop.name not in cols:
      cols[prop.name] = _ddl_type(prop.type)
  cols.setdefault("session_id", "STRING")
  cols.setdefault("extracted_at", "TIMESTAMP")
  return cols


def _merge_columns(
    existing: dict[str, str],
    incoming: dict[str, str],
    table_ref: str,
) -> None:
  """Merge *incoming* columns into *existing*, raising on type conflicts."""
  for name, dtype in incoming.items():
    if name in existing and existing[name] != dtype:
      raise ValueError(
          f"Column type conflict for {name!r} in shared table "
          f"{table_ref!r}: existing {existing[name]} vs incoming {dtype}."
      )
    existing[name] = dtype


def _columns_to_ddl(table_ref: str, columns: dict[str, str]) -> str:
  """Build ``CREATE TABLE IF NOT EXISTS`` DDL from a column dict."""
  col_defs = [f"  {name} {dtype}" for name, dtype in columns.items()]
  return (
      f"CREATE TABLE IF NOT EXISTS `{table_ref}` (\n"
      + ",\n".join(col_defs)
      + "\n)"
  )


def compile_entity_ddl(
    entity: EntitySpec,
    project_id: str,
    dataset_id: str,
) -> str:
  """Generate ``CREATE TABLE IF NOT EXISTS`` DDL for an entity.

  Columns: all spec properties + metadata columns
  (``session_id``, ``extracted_at``).
  """
  table_ref = entity.binding.source
  # If binding.source is already fully qualified (3-part), use as-is.
  # Otherwise, prefix with project.dataset.
  if table_ref.count(".") < 2:
    table_ref = f"{project_id}.{dataset_id}.{table_ref}"

  return _columns_to_ddl(table_ref, _entity_columns(entity))


def compile_relationship_ddl(
    rel: RelationshipSpec,
    spec: GraphSpec,
    project_id: str,
    dataset_id: str,
) -> str:
  """Generate ``CREATE TABLE IF NOT EXISTS`` DDL for a relationship.

  Columns: from-entity key columns + to-entity key columns +
  relationship properties + metadata.
  """
  table_ref = rel.binding.source
  if table_ref.count(".") < 2:
    table_ref = f"{project_id}.{dataset_id}.{table_ref}"

  return _columns_to_ddl(table_ref, _relationship_columns(rel, spec))


# ------------------------------------------------------------------ #
# Routing: ExtractedGraph -> row dicts                                 #
# ------------------------------------------------------------------ #


def _route_node(
    node: ExtractedNode,
    entity_spec: EntitySpec,
    session_id: str,
) -> dict:
  """Convert an ``ExtractedNode`` to a row dict for ``insert_rows_json``.

  Only properties declared in the entity spec are included.  Extra
  properties emitted by AI.GENERATE (e.g. hallucinated fields or
  fields belonging to a different entity type) are silently dropped
  to prevent ``insert_rows_json`` failures on non-schema columns.
  """
  schema_props = {p.name for p in entity_spec.properties}
  row: dict = {}
  for prop in node.properties:
    if prop.name in schema_props:
      row[prop.name] = prop.value
  row["session_id"] = session_id
  row["extracted_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
  return row


def _parse_key_segment(node_id: str) -> dict[str, str]:
  """Parse the key segment from a node ID.

  Node IDs look like ``{session_id}:{entity_name}:{k1=v1,k2=v2}``.
  Returns a dict of key-value pairs from the last segment, or empty
  dict if the format is unexpected (e.g. index-based fallback IDs).
  """
  parts = node_id.split(":")
  if len(parts) < 3:
    return {}
  key_segment = parts[-1]
  if "=" not in key_segment:
    return {}
  result = {}
  for pair in key_segment.split(","):
    if "=" in pair:
      k, v = pair.split("=", 1)
      result[k] = v
  return result


def _route_edge(
    edge: ExtractedEdge,
    rel: RelationshipSpec,
    spec: GraphSpec,
    session_id: str,
) -> dict:
  """Convert an ``ExtractedEdge`` to a row dict for ``insert_rows_json``.

  Foreign key columns are populated from the edge's ``from_node_id``
  and ``to_node_id`` key segments, mapped through the relationship's
  ``from_columns``/``to_columns`` binding.
  """
  row: dict = {}

  # Map from-entity keys.  from_columns are a subset of the source
  # entity's primary keys, so the column names match the key names
  # in the parsed node ID segment.
  from_keys = _parse_key_segment(edge.from_node_id)
  if rel.binding.from_columns:
    for col in rel.binding.from_columns:
      row[col] = from_keys.get(col, "")
  else:
    row.update(from_keys)

  # Map to-entity keys (same logic).
  to_keys = _parse_key_segment(edge.to_node_id)
  if rel.binding.to_columns:
    for col in rel.binding.to_columns:
      row[col] = to_keys.get(col, "")
  else:
    row.update(to_keys)

  # Edge properties — only include properties declared in the spec.
  # Extra AI-emitted fields are dropped to prevent insert failures.
  schema_props = {p.name for p in rel.properties}
  for prop in edge.properties:
    if prop.name in schema_props:
      row[prop.name] = prop.value

  # Determine session_id for delete-scoped ownership.
  # For lineage edges with to_session_column, session_id = to_session value
  # so that the edge is owned by the destination session.
  # For normal edges, session_id = the session being processed (V4 behavior).
  if rel.binding.to_session_column and rel.binding.to_session_column in row:
    row["session_id"] = row[rel.binding.to_session_column]
  else:
    row["session_id"] = session_id
  row["extracted_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
  return row


# ------------------------------------------------------------------ #
# Delete queries (session-scoped cleanup for idempotency)              #
# ------------------------------------------------------------------ #

_DELETE_FOR_SESSIONS = """\
DELETE FROM `{table_ref}`
WHERE session_id IN UNNEST(@session_ids)
"""


# ------------------------------------------------------------------ #
# OntologyMaterializer                                                 #
# ------------------------------------------------------------------ #


class OntologyMaterializer:
  """Persists extracted ontology graphs into BigQuery tables.

  Each entity and relationship in the spec maps to a physical table
  via ``binding.source``.  Persistence uses delete-then-insert
  (same pattern as V3) for session-scoped idempotency.

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

  def _table_ref(self, binding_source: str) -> str:
    """Resolve a binding source to a fully qualified table reference."""
    if binding_source.count(".") >= 2:
      return binding_source
    return f"{self.project_id}.{self.dataset_id}.{binding_source}"

  # ---- DDL --------------------------------------------------------

  def _merged_table_ddl(
      self,
  ) -> tuple[dict[str, str], dict[str, list[str]]]:
    """Merge columns per physical table across all spec entries.

    Returns:
        A tuple of ``(table_ddl, table_names)`` where
        ``table_ddl`` maps ``table_ref → CREATE TABLE DDL`` and
        ``table_names`` maps ``table_ref → [spec entry names]``.

    Raises:
        ValueError: If two entries define the same column name with
            different types on a shared table.
    """
    table_columns: dict[str, dict[str, str]] = {}
    table_names: dict[str, list[str]] = {}

    for entity in self.spec.entities:
      table_ref = self._table_ref(entity.binding.source)
      merged = table_columns.setdefault(table_ref, {})
      _merge_columns(merged, _entity_columns(entity), table_ref)
      table_names.setdefault(table_ref, []).append(entity.name)

    for rel in self.spec.relationships:
      table_ref = self._table_ref(rel.binding.source)
      merged = table_columns.setdefault(table_ref, {})
      _merge_columns(merged, _relationship_columns(rel, self.spec), table_ref)
      table_names.setdefault(table_ref, []).append(rel.name)

    table_ddl = {
        ref: _columns_to_ddl(ref, cols) for ref, cols in table_columns.items()
    }
    return table_ddl, table_names

  # ---- DDL (public) ------------------------------------------------

  def get_entity_ddl(self, entity_name: str) -> str:
    """Return the merged CREATE TABLE DDL for the entity's physical table."""
    entity_map = {e.name: e for e in self.spec.entities}
    if entity_name not in entity_map:
      raise ValueError(
          f"Entity {entity_name!r} not found in spec. "
          f"Available: {sorted(entity_map.keys())}."
      )
    table_ref = self._table_ref(entity_map[entity_name].binding.source)
    table_ddl, _ = self._merged_table_ddl()
    return table_ddl[table_ref]

  def get_relationship_ddl(self, rel_name: str) -> str:
    """Return the merged CREATE TABLE DDL for the relationship's physical table."""
    rel_map = {r.name: r for r in self.spec.relationships}
    if rel_name not in rel_map:
      raise ValueError(
          f"Relationship {rel_name!r} not found in spec. "
          f"Available: {sorted(rel_map.keys())}."
      )
    table_ref = self._table_ref(rel_map[rel_name].binding.source)
    table_ddl, _ = self._merged_table_ddl()
    return table_ddl[table_ref]

  def get_all_ddl(self) -> dict[str, str]:
    """Return merged DDL for all physical tables.

    When multiple spec entries share the same ``binding.source``,
    the DDL contains the union of all columns.  Each entry name
    maps to the merged DDL for its physical table.

    Returns:
        Dict mapping ``{entity_or_rel_name}`` → DDL string.
    """
    table_ddl, table_names = self._merged_table_ddl()
    result = {}
    for table_ref, names in table_names.items():
      for name in names:
        result[name] = table_ddl[table_ref]
    return result

  def create_tables(self) -> dict[str, str]:
    """Execute DDL to create all entity and relationship tables.

    When multiple spec entries share the same ``binding.source``,
    their columns are merged into a single ``CREATE TABLE`` DDL so
    that the physical table contains the union of all required
    columns.  A ``ValueError`` is raised if two entries define the
    same column name with different types.

    Returns:
        Dict mapping ``{name}`` → table reference for created tables.
    """
    table_ddl, table_names = self._merged_table_ddl()

    created = {}
    for table_ref, ddl in table_ddl.items():
      try:
        job = self.bq_client.query(ddl)
        job.result()
        for name in table_names[table_ref]:
          created[name] = table_ref
      except Exception as e:
        logger.warning(
            "Failed to create table %s for %s: %s",
            table_ref,
            table_names[table_ref],
            e,
        )

    return created

  # ---- Materialization --------------------------------------------

  def _delete_for_sessions(
      self, table_ref: str, session_ids: list[str]
  ) -> None:
    """Delete rows for given sessions from a table."""
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ArrayQueryParameter("session_ids", "STRING", session_ids),
        ]
    )
    try:
      job = self.bq_client.query(
          _DELETE_FOR_SESSIONS.format(table_ref=table_ref),
          job_config=job_config,
      )
      job.result()
    except Exception as e:
      err_msg = str(e).lower()
      if "not found" in err_msg or "does not exist" in err_msg:
        logger.debug("Table %s does not exist yet: %s", table_ref, e)
      else:
        logger.warning("Delete for sessions failed on %s: %s", table_ref, e)

  def materialize(
      self,
      graph: ExtractedGraph,
      session_ids: list[str],
  ) -> dict[str, int]:
    """Materialize an ``ExtractedGraph`` into BigQuery tables.

    Uses delete-then-insert for session-scoped idempotency:
    existing rows for the given sessions are deleted before
    inserting the new graph data.

    Args:
        graph: The extracted graph to persist.
        session_ids: Sessions being materialized (scopes the
            delete for idempotency).

    Returns:
        Dict mapping entity/relationship name to row count inserted.
    """
    entity_map = {e.name: e for e in self.spec.entities}
    rel_map = {r.name: r for r in self.spec.relationships}
    result: dict[str, int] = {}

    # Derive session_id for rows from the session_ids parameter.
    # For single-session extractions this is straightforward; for
    # multi-session, nodes already carry session_id in their node_id.
    default_session_id = session_ids[0] if len(session_ids) == 1 else ""

    # Collect all rows per physical table.  Multiple spec entries
    # may share the same binding.source, so we group by resolved
    # table ref to avoid delete-then-insert races on shared tables.
    table_rows: dict[str, list[dict]] = {}
    # Track per-name counts for the return value.
    name_counts: dict[str, int] = {}

    # Route nodes.
    for node in graph.nodes:
      entity = entity_map.get(node.entity_name)
      if entity is None:
        logger.debug("Skipping node with unknown entity %r", node.entity_name)
        continue
      table_ref = self._table_ref(entity.binding.source)
      parts = node.node_id.split(":")
      sid = parts[0] if parts else default_session_id
      row = _route_node(node, entity, sid)
      table_rows.setdefault(table_ref, []).append(row)
      name_counts[node.entity_name] = name_counts.get(node.entity_name, 0) + 1

    # Route edges.
    for edge in graph.edges:
      rel = rel_map.get(edge.relationship_name)
      if rel is None:
        logger.debug(
            "Skipping edge with unknown relationship %r",
            edge.relationship_name,
        )
        continue
      table_ref = self._table_ref(rel.binding.source)
      parts = edge.from_node_id.split(":")
      sid = parts[0] if parts else default_session_id
      row = _route_edge(edge, rel, self.spec, sid)
      table_rows.setdefault(table_ref, []).append(row)
      name_counts[edge.relationship_name] = (
          name_counts.get(edge.relationship_name, 0) + 1
      )

    # One delete + one insert per physical table.
    inserted_tables: set[str] = set()
    for table_ref, rows in table_rows.items():
      self._delete_for_sessions(table_ref, session_ids)
      try:
        errors = self.bq_client.insert_rows_json(table_ref, rows)
        if errors:
          logger.error("Insert errors for %s: %s", table_ref, errors)
        else:
          inserted_tables.add(table_ref)
      except Exception as e:
        logger.warning("Failed to insert rows for %s: %s", table_ref, e)

    # Build result: only include names whose table insert succeeded.
    for name, count in name_counts.items():
      entity = entity_map.get(name)
      rel = rel_map.get(name)
      if entity:
        ref = self._table_ref(entity.binding.source)
      elif rel:
        ref = self._table_ref(rel.binding.source)
      else:
        continue
      if ref in inserted_tables:
        result[name] = count

    return result
