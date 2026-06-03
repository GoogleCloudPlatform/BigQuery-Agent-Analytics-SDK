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

"""Tests for ontology+binding synthesis from a property graph (issue #277, PR 3).

Pure, offline: the parsed graph and column types are built in-process. The
final test feeds the synthesised pair through the SDK's real ``resolve()`` to
prove the output is materialization-ready (the whole point of #277).
"""

from __future__ import annotations

from typing import Mapping

import pytest

from bigquery_ontology.graph_ddl_parser import parse_property_graph_ddl
from bigquery_ontology.graph_schema_join import resolve_graph_column_types
from bigquery_ontology.graph_to_spec import derive_ontology_binding
from bigquery_ontology.graph_to_spec import GraphSpecSynthesisError
from bigquery_ontology.ontology_models import PropertyType


class _FakeProvider:

  def __init__(self, schemas: Mapping[str, Mapping[str, str]]) -> None:
    self._schemas = schemas

  def column_types(self, table_ref: str) -> Mapping[str, str]:
    return self._schemas[table_ref]


def _derive(ddl: str, schemas, *, project="p", dataset="d"):
  graph = parse_property_graph_ddl(ddl)
  types = resolve_graph_column_types(graph, _FakeProvider(schemas))
  return derive_ontology_binding(graph, types, project=project, dataset=dataset)


# A concrete (no ${...}) codelab-shaped graph in the SDK transpiler style:
# entity name == LABEL, snake-case table aliases, session_id in KEY/PROPERTIES.
_DDL = """
CREATE OR REPLACE PROPERTY GRAPH p.d.agent_decisions_graph
  NODE TABLES (
    p.d.decision_request AS decision_request
      KEY (request_id, session_id)
      LABEL DecisionRequest
      PROPERTIES (request_id, request_text, requested_at, session_id, extracted_at),
    p.d.decision_option AS decision_option
      KEY (option_id, session_id)
      LABEL DecisionOption
      PROPERTIES (option_id, option_label, confidence, session_id, extracted_at)
  )
  EDGE TABLES (
    p.d.evaluates_option AS evaluates_option
      KEY (request_id, option_id, session_id)
      SOURCE KEY (request_id, session_id) REFERENCES decision_request (request_id, session_id)
      DESTINATION KEY (option_id, session_id) REFERENCES decision_option (option_id, session_id)
      LABEL evaluatesOption
      PROPERTIES (extracted_at)
  )
"""

_SCHEMAS = {
    "p.d.decision_request": {
        "request_id": "STRING",
        "request_text": "STRING",
        "requested_at": "TIMESTAMP",
        "session_id": "STRING",
        "extracted_at": "TIMESTAMP",
    },
    "p.d.decision_option": {
        "option_id": "STRING",
        "option_label": "STRING",
        "confidence": "FLOAT64",
        "session_id": "STRING",
        "extracted_at": "TIMESTAMP",
    },
    "p.d.evaluates_option": {
        "request_id": "STRING",
        "option_id": "STRING",
        "session_id": "STRING",
        "extracted_at": "TIMESTAMP",
    },
}


def test_entity_name_is_label_and_types_come_from_schema() -> None:
  ontology, _ = _derive(_DDL, _SCHEMAS)
  assert ontology.ontology == "agent_decisions_graph"
  names = {e.name for e in ontology.entities}
  assert names == {"DecisionRequest", "DecisionOption"}  # LABELs, not aliases

  request = next(e for e in ontology.entities if e.name == "DecisionRequest")
  prop_types = {p.name: p.type for p in request.properties}
  # session_id / extracted_at metadata stripped; types recovered from schema.
  assert prop_types == {
      "request_id": PropertyType.STRING,
      "request_text": PropertyType.STRING,
      "requested_at": PropertyType.TIMESTAMP,
  }
  option = next(e for e in ontology.entities if e.name == "DecisionOption")
  assert {p.name: p.type for p in option.properties}["confidence"] == (
      PropertyType.DOUBLE
  )


def test_primary_key_strips_metadata_and_maps_to_property_names() -> None:
  ontology, _ = _derive(_DDL, _SCHEMAS)
  request = next(e for e in ontology.entities if e.name == "DecisionRequest")
  # KEY was (request_id, session_id); session_id stripped.
  assert request.keys.primary == ["request_id"]


def test_relationship_endpoints_resolve_aliases_to_entity_names() -> None:
  ontology, binding = _derive(_DDL, _SCHEMAS)
  assert [r.name for r in ontology.relationships] == ["evaluatesOption"]
  rel = ontology.relationships[0]
  assert rel.from_ == "DecisionRequest"  # alias decision_request -> LABEL
  assert rel.to == "DecisionOption"

  rel_binding = binding.relationships[0]
  # FK columns stripped of session_id.
  assert rel_binding.from_columns == ["request_id"]
  assert rel_binding.to_columns == ["option_id"]


def test_binding_target_and_sources() -> None:
  _, binding = _derive(_DDL, _SCHEMAS, project="proj", dataset="ds")
  assert binding.ontology == "agent_decisions_graph"
  assert binding.binding == "agent_decisions_graph_binding"
  assert binding.target.project == "proj"
  assert binding.target.dataset == "ds"
  account = next(e for e in binding.entities if e.name == "DecisionRequest")
  assert account.source == "p.d.decision_request"
  cols = {pb.name: pb.column for pb in account.properties}
  assert cols == {
      "request_id": "request_id",
      "request_text": "request_text",
      "requested_at": "requested_at",
  }


def test_renamed_property_and_passthrough_key() -> None:
  # A renamed property (acct_id AS account_id) and a KEY column that is not a
  # stored property (it gets a passthrough property so the PK is bindable).
  ddl = """
  CREATE PROPERTY GRAPH g
    NODE TABLES (
      raw.accounts AS Account
        KEY (acct_id)
        LABEL Account PROPERTIES (acct_id AS account_id, balance)
    )
  """
  schemas = {"raw.accounts": {"acct_id": "STRING", "balance": "NUMERIC"}}
  ontology, binding = _derive(ddl, schemas)
  account = ontology.entities[0]
  prop_types = {p.name: p.type for p in account.properties}
  assert prop_types == {
      "account_id": PropertyType.STRING,
      "balance": PropertyType.NUMERIC,
  }
  # PK column acct_id maps to property name account_id.
  assert account.keys.primary == ["account_id"]
  cols = {pb.name: pb.column for pb in binding.entities[0].properties}
  assert cols == {"account_id": "acct_id", "balance": "balance"}


def test_derived_property_is_skipped() -> None:
  ddl = """
  CREATE PROPERTY GRAPH g
    NODE TABLES (
      raw.persons AS Person
        KEY (person_id)
        LABEL Person PROPERTIES (
          person_id,
          (first || ' ' || last) AS full_name
        )
    )
  """
  schemas = {
      "raw.persons": {
          "person_id": "STRING",
          "first": "STRING",
          "last": "STRING",
      }
  }
  ontology, binding = _derive(ddl, schemas)
  names = {p.name for p in ontology.entities[0].properties}
  assert "full_name" not in names  # derived -> skipped
  assert names == {"person_id"}
  assert all(p.expr is None for p in ontology.entities[0].properties)


def test_multi_label_node_raises() -> None:
  ddl = (
      "CREATE PROPERTY GRAPH g NODE TABLES ("
      " t AS N KEY (id) LABEL Child LABEL Parent PROPERTIES (id))"
  )
  schemas = {"t": {"id": "STRING"}}
  with pytest.raises(GraphSpecSynthesisError, match="labels"):
    _derive(ddl, schemas)


def test_synthesised_pair_resolves_for_materialization() -> None:
  # The end-to-end goal: the synthesised ontology+binding must be consumable by
  # the SDK's real resolver, exactly like a hand-written pair would be.
  resolve = pytest.importorskip(
      "bigquery_agent_analytics.resolved_spec"
  ).resolve
  ontology, binding = _derive(_DDL, _SCHEMAS)
  resolved = resolve(ontology, binding)

  assert resolved.name == "agent_decisions_graph"
  entity = next(e for e in resolved.entities if e.name == "DecisionRequest")
  assert entity.key_columns == ("request_id",)
  # The resolver re-injects the SDK metadata columns we stripped.
  assert entity.metadata_columns == ("session_id", "extracted_at")
  rel = next(r for r in resolved.relationships if r.name == "evaluatesOption")
  assert rel.from_entity == "DecisionRequest"
  assert rel.to_entity == "DecisionOption"
