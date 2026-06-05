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

"""Join a parsed property graph with table schemas to recover property types.

Building block 2 of deriving a materialization spec from the property graph
alone (GitHub issue #277). A ``CREATE PROPERTY GRAPH`` statement names columns
but declares **no types** -- BigQuery infers property types from the underlying
table columns. :mod:`bigquery_ontology.graph_ddl_parser` (PR 1) gives us a
types-free :class:`~bigquery_ontology.graph_ddl_parser.ParsedPropertyGraph`;
this module looks up each referenced column's type and maps it back to the
logical :class:`~bigquery_ontology.ontology_models.PropertyType`.

Scope (per #277): schema join for property **types** only. Synthesising the
in-memory ontology/binding is PR 3.

I/O is isolated behind the :class:`SchemaProvider` protocol so the join logic is
a pure, offline-testable transform. :class:`BigQuerySchemaProvider` is the
concrete provider that reads ``INFORMATION_SCHEMA.COLUMNS``.

Type recovery is the inverse of ``bigquery_ontology.scaffold._ONTOLOGY_TO_BQ_TYPE``.
BigQuery identifiers are case-insensitive, so column lookups are
case-insensitive; type names are normalised (uppercased, type parameters such
as ``NUMERIC(38, 9)`` stripped) and common GoogleSQL aliases (``INTEGER`` ==
``INT64``, ``FLOAT`` == ``FLOAT64``, ``BOOLEAN`` == ``BOOL``, ``DECIMAL`` ==
``NUMERIC``) are accepted. Types with no logical analogue (``GEOGRAPHY``,
``INTERVAL``, ``ARRAY<...>``, ``STRUCT<...>``, ...) raise
:class:`UnsupportedColumnTypeError`.
"""

from __future__ import annotations

import dataclasses
import re
from typing import Mapping, Optional, Protocol

from .graph_ddl_parser import ParsedEdgeTable
from .graph_ddl_parser import ParsedNodeTable
from .graph_ddl_parser import ParsedPropertyGraph
from .ontology_models import PropertyType

__all__ = [
    "GraphSchemaError",
    "MissingColumnError",
    "UnsupportedColumnTypeError",
    "SchemaProvider",
    "BigQuerySchemaProvider",
    "TableColumnTypes",
    "GraphColumnTypes",
    "googlesql_type_to_property_type",
    "resolve_graph_column_types",
]


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class GraphSchemaError(ValueError):
  """Base error for schema-join failures."""


class MissingColumnError(GraphSchemaError):
  """A column referenced by the property graph is absent from its table."""


class UnsupportedColumnTypeError(GraphSchemaError):
  """A column's GoogleSQL type has no logical ``PropertyType`` analogue."""


# --------------------------------------------------------------------------- #
# Type recovery: GoogleSQL type -> logical PropertyType
# --------------------------------------------------------------------------- #

# Inverse of ``scaffold._ONTOLOGY_TO_BQ_TYPE`` plus the GoogleSQL aliases and
# legacy names that ``INFORMATION_SCHEMA.COLUMNS.data_type`` can return.
# (test_graph_schema_join.py pins this against scaffold so the two cannot drift.)
_GOOGLESQL_TO_PROPERTY_TYPE: dict[str, PropertyType] = {
    "STRING": PropertyType.STRING,
    "BYTES": PropertyType.BYTES,
    "INT64": PropertyType.INTEGER,
    "INTEGER": PropertyType.INTEGER,
    "FLOAT64": PropertyType.DOUBLE,
    "FLOAT": PropertyType.DOUBLE,
    "NUMERIC": PropertyType.NUMERIC,
    "DECIMAL": PropertyType.NUMERIC,
    "BIGNUMERIC": PropertyType.NUMERIC,
    "BIGDECIMAL": PropertyType.NUMERIC,
    "BOOL": PropertyType.BOOLEAN,
    "BOOLEAN": PropertyType.BOOLEAN,
    "DATE": PropertyType.DATE,
    "TIME": PropertyType.TIME,
    "DATETIME": PropertyType.DATETIME,
    "TIMESTAMP": PropertyType.TIMESTAMP,
    "JSON": PropertyType.JSON,
}

# Strips a trailing type-parameter list, e.g. ``NUMERIC(38, 9)`` -> ``NUMERIC``,
# ``STRING(255)`` -> ``STRING``. Parametrised forms only appear on scalar types.
_TYPE_PARAM_RE = re.compile(r"\s*\(.*\)\s*$", re.DOTALL)


def googlesql_type_to_property_type(data_type: str) -> PropertyType:
  """Map a GoogleSQL column type to the logical ``PropertyType``.

  Args:
    data_type: A type as reported by ``INFORMATION_SCHEMA.COLUMNS.data_type``
      (e.g. ``"INT64"``, ``"NUMERIC(38, 9)"``, ``"timestamp"``).

  Returns:
    The corresponding :class:`PropertyType`.

  Raises:
    UnsupportedColumnTypeError: For types with no logical analogue
      (``GEOGRAPHY``, ``INTERVAL``, ``ARRAY<...>``, ``STRUCT<...>``, ``RANGE``,
      ...).
  """
  normalized = _TYPE_PARAM_RE.sub("", (data_type or "").strip()).upper()
  property_type = _GOOGLESQL_TO_PROPERTY_TYPE.get(normalized)
  if property_type is None:
    raise UnsupportedColumnTypeError(
        f"Column type {data_type!r} has no logical PropertyType analogue."
        " Supported scalar types: "
        + ", ".join(sorted(set(_GOOGLESQL_TO_PROPERTY_TYPE)))
        + "."
    )
  return property_type


# --------------------------------------------------------------------------- #
# Schema provider
# --------------------------------------------------------------------------- #


class SchemaProvider(Protocol):
  """Supplies physical column types for a table referenced by the graph."""

  def column_types(self, table_ref: str) -> Mapping[str, str]:
    """Return ``{column_name: googlesql_data_type}`` for ``table_ref``.

    ``table_ref`` is the reference exactly as written in the DDL (the form
    produced by the parser), e.g. ``raw.accounts`` or ``proj.ds.t``.
    """
    ...


class BigQuerySchemaProvider:
  """A :class:`SchemaProvider` backed by ``INFORMATION_SCHEMA.COLUMNS``.

  ``default_project`` / ``default_dataset`` fill in table references that are
  not fully qualified (``dataset.table`` or bare ``table``). Results are cached
  per resolved table for the lifetime of the provider.
  """

  def __init__(
      self,
      client,
      *,
      default_project: Optional[str] = None,
      default_dataset: Optional[str] = None,
  ) -> None:
    self._client = client
    self._default_project = default_project
    self._default_dataset = default_dataset
    self._cache: dict[str, Mapping[str, str]] = {}

  def _split(self, table_ref: str) -> tuple[str, str, str]:
    if "${" in table_ref or "}" in table_ref:
      raise GraphSchemaError(
          f"Table reference {table_ref!r} still contains an unresolved"
          " ${...} placeholder; render it (e.g. envsubst) before reading"
          " its schema."
      )
    parts = table_ref.split(".")
    if len(parts) == 3:
      project, dataset, table = parts
    elif len(parts) == 2:
      project, (dataset, table) = self._default_project, parts
    elif len(parts) == 1:
      project, dataset, table = (
          self._default_project,
          self._default_dataset,
          parts[0],
      )
    else:
      raise GraphSchemaError(
          f"Cannot parse table reference {table_ref!r}: expected 1-3"
          " dot-separated parts."
      )
    if not project or not dataset:
      raise GraphSchemaError(
          f"Table reference {table_ref!r} is not fully qualified and no"
          " default project/dataset was supplied to BigQuerySchemaProvider."
      )
    return project, dataset, table

  def column_types(self, table_ref: str) -> Mapping[str, str]:
    project, dataset, table = self._split(table_ref)
    cache_key = f"{project}.{dataset}.{table}"
    if cache_key in self._cache:
      return self._cache[cache_key]

    from google.cloud import bigquery

    sql = (
        "SELECT column_name, data_type\n"
        f"FROM `{project}.{dataset}`.INFORMATION_SCHEMA.COLUMNS\n"
        "WHERE table_name = @table_name"
    )
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("table_name", "STRING", table)
        ]
    )
    rows = self._client.query(sql, job_config=job_config).result()
    columns = {row["column_name"]: row["data_type"] for row in rows}
    if not columns:
      raise GraphSchemaError(
          f"No columns found for table `{cache_key}` in"
          " INFORMATION_SCHEMA.COLUMNS. Does the table exist and has the"
          " table DDL been applied?"
      )
    self._cache[cache_key] = columns
    return columns


# --------------------------------------------------------------------------- #
# Join result
# --------------------------------------------------------------------------- #


@dataclasses.dataclass(frozen=True)
class TableColumnTypes:
  """Resolved logical types for the columns one graph table references."""

  alias: str
  source: str
  column_types: Mapping[str, PropertyType]  # keyed by column name as written


@dataclasses.dataclass(frozen=True)
class GraphColumnTypes:
  """Resolved column types for every node and edge table in a graph."""

  nodes: tuple[TableColumnTypes, ...]
  edges: tuple[TableColumnTypes, ...]

  def node(self, alias: str) -> TableColumnTypes:
    for table in self.nodes:
      if table.alias == alias:
        return table
    raise KeyError(f"No node table {alias!r} in resolved types.")

  def edge(self, alias: str) -> TableColumnTypes:
    for table in self.edges:
      if table.alias == alias:
        return table
    raise KeyError(f"No edge table {alias!r} in resolved types.")


# --------------------------------------------------------------------------- #
# Join
# --------------------------------------------------------------------------- #


def _resolve_table(
    alias: str,
    source: str,
    referenced_columns: list[str],
    provider: SchemaProvider,
) -> TableColumnTypes:
  raw_types = provider.column_types(source)
  # BigQuery identifiers are case-insensitive; index by lowercased name but
  # report types under the column name exactly as the DDL wrote it.
  lowered = {name.lower(): (name, dtype) for name, dtype in raw_types.items()}
  resolved: dict[str, PropertyType] = {}
  for column in referenced_columns:
    if column in resolved:
      continue
    match = lowered.get(column.lower())
    if match is None:
      raise MissingColumnError(
          f"Table {alias!r} (`{source}`) has no column {column!r} referenced"
          f" by the property graph. Columns present: "
          f"{sorted(raw_types)}."
      )
    _, dtype = match
    try:
      resolved[column] = googlesql_type_to_property_type(dtype)
    except UnsupportedColumnTypeError as exc:
      raise UnsupportedColumnTypeError(
          f"Table {alias!r} (`{source}`), column {column!r}: {exc}"
      ) from exc
  return TableColumnTypes(alias=alias, source=source, column_types=resolved)


def _node_referenced_columns(node: ParsedNodeTable) -> list[str]:
  # Derived properties (column is None) have no physical column to type.
  columns = list(node.key_columns)
  columns.extend(p.column for p in node.properties if p.column is not None)
  return columns


def _edge_referenced_columns(edge: ParsedEdgeTable) -> list[str]:
  # The edge table physically holds its KEY and the SOURCE/DESTINATION foreign
  # keys; the REFERENCES columns belong to the node tables and are typed there.
  columns = list(edge.key_columns)
  columns.extend(edge.source_key_columns)
  columns.extend(edge.dest_key_columns)
  columns.extend(p.column for p in edge.properties if p.column is not None)
  return columns


def resolve_graph_column_types(
    graph: ParsedPropertyGraph, provider: SchemaProvider
) -> GraphColumnTypes:
  """Resolve logical types for every column the property graph references.

  For each node and edge table, the columns named in ``KEY``, the edge
  ``SOURCE``/``DESTINATION`` foreign keys, and the stored ``PROPERTIES`` are
  looked up in that table's schema (via ``provider``) and mapped to a
  :class:`PropertyType`. Derived properties (``(expr) AS name``) have no
  physical column and are skipped.

  Args:
    graph: A parsed property graph (see :mod:`graph_ddl_parser`).
    provider: Supplies ``{column: googlesql_type}`` per table.

  Returns:
    A :class:`GraphColumnTypes` with resolved types for nodes and edges.

  Raises:
    MissingColumnError: If a referenced column is absent from its table.
    UnsupportedColumnTypeError: If a column's type has no logical analogue.
    GraphSchemaError: For provider-level failures (e.g. unresolved placeholder
      references, missing tables).
  """
  nodes = tuple(
      _resolve_table(
          node.alias, node.source, _node_referenced_columns(node), provider
      )
      for node in graph.nodes
  )
  edges = tuple(
      _resolve_table(
          edge.alias, edge.source, _edge_referenced_columns(edge), provider
      )
      for edge in graph.edges
  )
  return GraphColumnTypes(nodes=nodes, edges=edges)
