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

"""Tests for deriving ontology+binding from property-graph DDL (issue #277, PR 4).

Offline: a fake BigQuery client returns per-table column schemas keyed by the
``table_name`` query parameter, so the whole parse -> schema -> synthesize
chain runs without live BigQuery.
"""

from __future__ import annotations

import pytest

from bigquery_agent_analytics.property_graph_spec import derive_ontology_binding_from_ddl
from bigquery_agent_analytics.property_graph_spec import resolve_placeholders
from bigquery_ontology.ontology_models import PropertyType


class _FakeJob:

  def __init__(self, rows):
    self._rows = rows

  def result(self):
    return self._rows


class _FakeClient:
  """Returns rows for the table named by the query's ``table_name`` param."""

  def __init__(self, schemas):
    self._schemas = schemas  # {table_name: [{column_name, data_type}, ...]}

  def query(self, sql, job_config=None):
    table = job_config.query_parameters[0].value
    return _FakeJob(self._schemas.get(table, []))


_DDL = """
CREATE OR REPLACE PROPERTY GRAPH `${PROJECT_ID}.${DATASET}.agent_decisions_graph`
  NODE TABLES (
    `${PROJECT_ID}.${DATASET}.decision_request` AS decision_request
      KEY (request_id)
      LABEL DecisionRequest PROPERTIES (request_id, request_text, requested_at),
    `${PROJECT_ID}.${DATASET}.decision_option` AS decision_option
      KEY (option_id)
      LABEL DecisionOption PROPERTIES (option_id, option_label, confidence)
  )
  EDGE TABLES (
    `${PROJECT_ID}.${DATASET}.evaluates_option` AS evaluates_option
      KEY (request_id, option_id)
      SOURCE KEY (request_id) REFERENCES decision_request (request_id)
      DESTINATION KEY (option_id) REFERENCES decision_option (option_id)
      LABEL evaluatesOption
  );
"""

_SCHEMAS = {
    "decision_request": [
        {"column_name": "request_id", "data_type": "STRING"},
        {"column_name": "request_text", "data_type": "STRING"},
        {"column_name": "requested_at", "data_type": "TIMESTAMP"},
    ],
    "decision_option": [
        {"column_name": "option_id", "data_type": "STRING"},
        {"column_name": "option_label", "data_type": "STRING"},
        {"column_name": "confidence", "data_type": "FLOAT64"},
    ],
    "evaluates_option": [
        {"column_name": "request_id", "data_type": "STRING"},
        {"column_name": "option_id", "data_type": "STRING"},
    ],
}


# --------------------------------------------------------------------------- #
# Placeholder resolution
# --------------------------------------------------------------------------- #


def test_resolve_placeholders_substitutes_known_leaves_unknown() -> None:
  out = resolve_placeholders(
      "${PROJECT_ID}.${DATASET}.t and ${UNKNOWN}",
      {"PROJECT_ID": "p", "DATASET": "d"},
  )
  assert out == "p.d.t and ${UNKNOWN}"


# --------------------------------------------------------------------------- #
# End-to-end derive (parse -> fake schema -> synthesize)
# --------------------------------------------------------------------------- #


def test_derive_from_ddl_resolves_placeholders_and_types() -> None:
  ontology, binding = derive_ontology_binding_from_ddl(
      _DDL,
      project_id="p",
      dataset_id="d",
      bq_client=_FakeClient(_SCHEMAS),
  )

  assert ontology.ontology == "agent_decisions_graph"
  assert {e.name for e in ontology.entities} == {
      "DecisionRequest",
      "DecisionOption",
  }
  option = next(e for e in ontology.entities if e.name == "DecisionOption")
  assert {p.name: p.type for p in option.properties}["confidence"] == (
      PropertyType.DOUBLE
  )

  # ${...} placeholders were resolved before schema lookup; the binding source
  # is the concrete table reference.
  request_binding = next(
      e for e in binding.entities if e.name == "DecisionRequest"
  )
  assert request_binding.source == "p.d.decision_request"
  assert binding.target.project == "p"
  assert binding.target.dataset == "d"

  rel = ontology.relationships[0]
  assert rel.name == "evaluatesOption"
  assert rel.from_ == "DecisionRequest"
  assert rel.to == "DecisionOption"


def test_derive_surfaces_unresolved_placeholder() -> None:
  # An unsubstituted placeholder (no PROJECT_ID/DATASET match for this var)
  # must reach the schema provider, which rejects it clearly.
  ddl = "CREATE PROPERTY GRAPH `${OTHER}.d.t` NODE TABLES (`${OTHER}.d.t` AS N KEY (id) LABEL N PROPERTIES (id))"
  from bigquery_ontology.graph_schema_join import GraphSchemaError

  with pytest.raises(GraphSchemaError, match="placeholder"):
    derive_ontology_binding_from_ddl(
        ddl, project_id="p", dataset_id="d", bq_client=_FakeClient({})
    )


# --------------------------------------------------------------------------- #
# Orchestrator load seam (the branch run_materialize_window actually uses)
# --------------------------------------------------------------------------- #


def test_orchestrator_resolves_via_property_graph(tmp_path) -> None:
  # Proves run_materialize_window's --property-graph branch reads the DDL file,
  # derives the pair, and converges on the same (Ontology, Binding) the YAML
  # path would feed downstream -- exercised through the exact helper the
  # orchestrator calls.
  from bigquery_agent_analytics.materialize_window import _resolve_ontology_binding

  ddl_file = tmp_path / "property_graph.sql"
  ddl_file.write_text(_DDL, encoding="utf-8")

  ontology, binding = _resolve_ontology_binding(
      ontology_path=None,
      binding_path=None,
      property_graph_path=str(ddl_file),
      project_id="p",
      dataset_id="d",
      bq_client=_FakeClient(_SCHEMAS),
  )
  assert ontology.ontology == "agent_decisions_graph"
  assert {e.name for e in ontology.entities} == {
      "DecisionRequest",
      "DecisionOption",
  }
  assert [r.name for r in ontology.relationships] == ["evaluatesOption"]
  assert binding.target.project == "p"


def test_orchestrator_rejects_both_modes() -> None:
  from bigquery_agent_analytics.materialize_window import _resolve_ontology_binding

  with pytest.raises(ValueError, match="not both"):
    _resolve_ontology_binding(
        ontology_path="o.yaml",
        binding_path="b.yaml",
        property_graph_path="g.sql",
        project_id="p",
        dataset_id="d",
        bq_client=_FakeClient({}),
    )


def test_orchestrator_rejects_neither_mode() -> None:
  from bigquery_agent_analytics.materialize_window import _resolve_ontology_binding

  with pytest.raises(ValueError, match="Provide --property-graph"):
    _resolve_ontology_binding(
        ontology_path=None,
        binding_path=None,
        property_graph_path=None,
        project_id="p",
        dataset_id="d",
        bq_client=_FakeClient({}),
    )
