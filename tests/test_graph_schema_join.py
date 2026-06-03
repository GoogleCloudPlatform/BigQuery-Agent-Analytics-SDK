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

"""Tests for the parsed-graph + INFORMATION_SCHEMA join (issue #277, PR 2).

The join logic is exercised offline through a dict-backed fake provider; the
BigQuery-backed provider is exercised through a fake client (no live BigQuery).
"""

from __future__ import annotations

from pathlib import Path
from typing import Mapping

import pytest

from bigquery_ontology.graph_ddl_parser import parse_property_graph_ddl
from bigquery_ontology.graph_schema_join import BigQuerySchemaProvider
from bigquery_ontology.graph_schema_join import googlesql_type_to_property_type
from bigquery_ontology.graph_schema_join import GraphSchemaError
from bigquery_ontology.graph_schema_join import MissingColumnError
from bigquery_ontology.graph_schema_join import resolve_graph_column_types
from bigquery_ontology.graph_schema_join import UnsupportedColumnTypeError
from bigquery_ontology.ontology_models import PropertyType
# Pin the inverse type map against the canonical forward map so they can't drift.
from bigquery_ontology.scaffold import _ONTOLOGY_TO_BQ_TYPE

_REPO = Path(__file__).resolve().parents[1]
_CODELAB_DDL = (
    _REPO
    / "examples"
    / "codelab"
    / "periodic_materialization"
    / "property_graph.sql"
)


class _FakeProvider:
  """A SchemaProvider backed by an in-memory ``{source: {col: type}}`` map."""

  def __init__(self, schemas: Mapping[str, Mapping[str, str]]) -> None:
    self._schemas = schemas

  def column_types(self, table_ref: str) -> Mapping[str, str]:
    return self._schemas[table_ref]


# --------------------------------------------------------------------------- #
# Type recovery
# --------------------------------------------------------------------------- #


def test_inverse_map_is_consistent_with_scaffold() -> None:
  # Every forward PropertyType -> BQ mapping must round-trip back.
  for property_type, bq_type in _ONTOLOGY_TO_BQ_TYPE.items():
    assert googlesql_type_to_property_type(bq_type) == property_type


@pytest.mark.parametrize(
    "data_type,expected",
    [
        ("INT64", PropertyType.INTEGER),
        ("INTEGER", PropertyType.INTEGER),
        ("FLOAT64", PropertyType.DOUBLE),
        ("FLOAT", PropertyType.DOUBLE),
        ("BOOL", PropertyType.BOOLEAN),
        ("BOOLEAN", PropertyType.BOOLEAN),
        ("DECIMAL", PropertyType.NUMERIC),
        ("BIGNUMERIC", PropertyType.NUMERIC),
        ("NUMERIC(38, 9)", PropertyType.NUMERIC),
        ("STRING(255)", PropertyType.STRING),
        ("timestamp", PropertyType.TIMESTAMP),
        ("  Json  ", PropertyType.JSON),
    ],
)
def test_googlesql_aliases_params_and_case(data_type, expected) -> None:
  assert googlesql_type_to_property_type(data_type) == expected


@pytest.mark.parametrize(
    "data_type",
    ["GEOGRAPHY", "INTERVAL", "ARRAY<INT64>", "STRUCT<a INT64>", "RANGE<DATE>"],
)
def test_unsupported_types_raise(data_type) -> None:
  with pytest.raises(UnsupportedColumnTypeError):
    googlesql_type_to_property_type(data_type)


# --------------------------------------------------------------------------- #
# Join over the parsed graph
# --------------------------------------------------------------------------- #

_CODELAB_SCHEMAS = {
    "${PROJECT_ID}.${DATASET}.decision_request": {
        "request_id": "STRING",
        "request_text": "STRING",
        "requested_at": "TIMESTAMP",
        "session_id": "STRING",
        "extracted_at": "TIMESTAMP",
    },
    "${PROJECT_ID}.${DATASET}.decision_option": {
        "option_id": "STRING",
        "option_label": "STRING",
        "confidence": "FLOAT64",
        "session_id": "STRING",
        "extracted_at": "TIMESTAMP",
    },
    "${PROJECT_ID}.${DATASET}.decision_outcome": {
        "outcome_id": "STRING",
        "status": "STRING",
        "rationale": "STRING",
        "decided_at": "TIMESTAMP",
        "session_id": "STRING",
        "extracted_at": "TIMESTAMP",
    },
    "${PROJECT_ID}.${DATASET}.evaluates_option": {
        "request_id": "STRING",
        "option_id": "STRING",
        "session_id": "STRING",
        "extracted_at": "TIMESTAMP",
    },
    "${PROJECT_ID}.${DATASET}.resulted_in": {
        "request_id": "STRING",
        "outcome_id": "STRING",
        "session_id": "STRING",
        "extracted_at": "TIMESTAMP",
    },
}


def test_resolves_codelab_graph_column_types() -> None:
  graph = parse_property_graph_ddl(_CODELAB_DDL.read_text(encoding="utf-8"))
  resolved = resolve_graph_column_types(graph, _FakeProvider(_CODELAB_SCHEMAS))

  option = resolved.node("decision_option")
  assert option.column_types == {
      "option_id": PropertyType.STRING,
      "option_label": PropertyType.STRING,
      "confidence": PropertyType.DOUBLE,  # FLOAT64 -> double
  }
  outcome = resolved.node("decision_outcome")
  assert outcome.column_types["decided_at"] == PropertyType.TIMESTAMP

  # Edge FK columns (in the edge table) are typed; only referenced columns
  # appear (no unreferenced session_id/extracted_at).
  evaluates = resolved.edge("evaluates_option")
  assert evaluates.column_types == {
      "request_id": PropertyType.STRING,
      "option_id": PropertyType.STRING,
  }


def test_derived_property_is_skipped_not_required() -> None:
  # The finance graph has a derived (given_name || ' ' || family_name) AS
  # full_name. There is no full_name column, and the join must not require one.
  ddl = """
  CREATE PROPERTY GRAPH finance
    NODE TABLES (
      raw.persons AS Person
        KEY (person_id)
        LABEL Person PROPERTIES (
          person_id,
          given_name AS first_name,
          (given_name || ' ' || family_name) AS full_name
        )
    )
  """
  graph = parse_property_graph_ddl(ddl)
  schemas = {
      "raw.persons": {
          "person_id": "STRING",
          "given_name": "STRING",
          "family_name": "STRING",
      }
  }
  resolved = resolve_graph_column_types(graph, _FakeProvider(schemas))
  person = resolved.node("Person")
  # full_name is derived -> not a column -> absent from resolved types.
  assert "full_name" not in person.column_types
  assert person.column_types == {
      "person_id": PropertyType.STRING,
      "given_name": PropertyType.STRING,
  }


def test_missing_column_raises_with_context() -> None:
  graph = parse_property_graph_ddl(
      "CREATE PROPERTY GRAPH g NODE TABLES ("
      " raw.t AS T KEY (id) LABEL T PROPERTIES (id, missing_col))"
  )
  provider = _FakeProvider({"raw.t": {"id": "STRING"}})
  with pytest.raises(MissingColumnError, match="missing_col"):
    resolve_graph_column_types(graph, provider)


def test_unsupported_column_type_raises_with_context() -> None:
  graph = parse_property_graph_ddl(
      "CREATE PROPERTY GRAPH g NODE TABLES ("
      " raw.t AS T KEY (id) LABEL T PROPERTIES (id, shape))"
  )
  provider = _FakeProvider({"raw.t": {"id": "STRING", "shape": "GEOGRAPHY"}})
  with pytest.raises(UnsupportedColumnTypeError, match="shape"):
    resolve_graph_column_types(graph, provider)


def test_column_lookup_is_case_insensitive() -> None:
  graph = parse_property_graph_ddl(
      "CREATE PROPERTY GRAPH g NODE TABLES ("
      " raw.t AS T KEY (Id) LABEL T PROPERTIES (Id, Amount))"
  )
  provider = _FakeProvider({"raw.t": {"id": "STRING", "amount": "INT64"}})
  resolved = resolve_graph_column_types(graph, provider)
  assert resolved.node("T").column_types == {
      "Id": PropertyType.STRING,
      "Amount": PropertyType.INTEGER,
  }


# --------------------------------------------------------------------------- #
# BigQuerySchemaProvider (fake client, no live BigQuery)
# --------------------------------------------------------------------------- #


class _FakeQueryJob:

  def __init__(self, rows):
    self._rows = rows

  def result(self):
    return self._rows


class _FakeClient:

  def __init__(self, rows):
    self._rows = rows
    self.queries: list[str] = []

  def query(self, sql, job_config=None):
    self.queries.append(sql)
    self.last_job_config = job_config
    return _FakeQueryJob(self._rows)


def test_bq_provider_queries_information_schema_and_parses() -> None:
  rows = [
      {"column_name": "request_id", "data_type": "STRING"},
      {"column_name": "confidence", "data_type": "FLOAT64"},
  ]
  client = _FakeClient(rows)
  provider = BigQuerySchemaProvider(client)
  result = provider.column_types("proj.ds.decision_request")
  assert result == {"request_id": "STRING", "confidence": "FLOAT64"}
  sql = client.queries[0]
  assert "`proj.ds`.INFORMATION_SCHEMA.COLUMNS" in sql
  assert "@table_name" in sql  # parameterized, not interpolated


def test_bq_provider_caches_per_table() -> None:
  client = _FakeClient([{"column_name": "id", "data_type": "STRING"}])
  provider = BigQuerySchemaProvider(client)
  provider.column_types("proj.ds.t")
  provider.column_types("proj.ds.t")
  assert len(client.queries) == 1  # second call served from cache


def test_bq_provider_resolves_default_project_and_dataset() -> None:
  client = _FakeClient([{"column_name": "id", "data_type": "STRING"}])
  provider = BigQuerySchemaProvider(
      client, default_project="p", default_dataset="d"
  )
  provider.column_types("just_table")
  assert "`p.d`.INFORMATION_SCHEMA.COLUMNS" in client.queries[0]
  provider.column_types("ds2.tbl")
  assert "`p.ds2`.INFORMATION_SCHEMA.COLUMNS" in client.queries[1]


def test_bq_provider_rejects_unresolved_placeholder() -> None:
  provider = BigQuerySchemaProvider(_FakeClient([]))
  with pytest.raises(GraphSchemaError, match="placeholder"):
    provider.column_types("${PROJECT_ID}.${DATASET}.t")


def test_bq_provider_requires_qualification_without_defaults() -> None:
  provider = BigQuerySchemaProvider(_FakeClient([]))
  with pytest.raises(GraphSchemaError, match="not fully qualified"):
    provider.column_types("bare_table")


def test_bq_provider_empty_table_raises() -> None:
  provider = BigQuerySchemaProvider(_FakeClient([]))
  with pytest.raises(GraphSchemaError, match="No columns found"):
    provider.column_types("proj.ds.missing")
