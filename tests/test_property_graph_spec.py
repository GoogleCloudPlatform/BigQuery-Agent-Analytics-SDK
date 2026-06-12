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

  with pytest.raises(ValueError, match="exactly one"):
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

  with pytest.raises(ValueError, match="Provide --graph"):
    _resolve_ontology_binding(
        ontology_path=None,
        binding_path=None,
        property_graph_path=None,
        project_id="p",
        dataset_id="d",
        bq_client=_FakeClient({}),
    )


def test_orchestrator_separate_graph_dataset(tmp_path) -> None:
  # Two-dataset deploy: events live in one dataset, the graph in another.
  # The derived binding must target the GRAPH dataset (graph_*), while the
  # events dataset_id is left for the orchestrator's event read.
  from bigquery_agent_analytics.materialize_window import _resolve_ontology_binding

  ddl_file = tmp_path / "property_graph.sql"
  ddl_file.write_text(_DDL, encoding="utf-8")  # uses ${PROJECT_ID}.${DATASET}

  _, binding = _resolve_ontology_binding(
      ontology_path=None,
      binding_path=None,
      property_graph_path=str(ddl_file),
      project_id="events-proj",
      dataset_id="events_ds",
      graph_project_id="graph-proj",
      graph_dataset_id="graph_ds",
      bq_client=_FakeClient(_SCHEMAS),
  )
  # Binding target + sources resolve to the GRAPH dataset, not events.
  assert binding.target.project == "graph-proj"
  assert binding.target.dataset == "graph_ds"
  request = next(e for e in binding.entities if e.name == "DecisionRequest")
  assert request.source == "graph-proj.graph_ds.decision_request"


def test_orchestrator_graph_dataset_defaults_to_events_dataset(
    tmp_path,
) -> None:
  # Single-dataset shape (codelab/CLI): omitting graph_* targets dataset_id.
  from bigquery_agent_analytics.materialize_window import _resolve_ontology_binding

  ddl_file = tmp_path / "property_graph.sql"
  ddl_file.write_text(_DDL, encoding="utf-8")

  _, binding = _resolve_ontology_binding(
      ontology_path=None,
      binding_path=None,
      property_graph_path=str(ddl_file),
      project_id="p",
      dataset_id="d",
      bq_client=_FakeClient(_SCHEMAS),
  )
  assert binding.target.project == "p"
  assert binding.target.dataset == "d"


def test_state_table_defaults_to_graph_dataset_in_split_mode() -> None:
  # The per-run checkpoint must land in the WRITABLE graph dataset, never the
  # read-only events dataset, when split-dataset mode is in use and no explicit
  # state_table is given.
  from bigquery_agent_analytics.materialize_window import _state_table_defaults
  from bigquery_agent_analytics.materialize_window import DEFAULT_STATE_TABLE_NAME
  from bigquery_agent_analytics.materialize_window import parse_state_table_ref

  proj, ds = _state_table_defaults(
      "events-proj", "events_ds", "graph-proj", "graph_ds"
  )
  assert (proj, ds) == ("graph-proj", "graph_ds")

  # End-to-end: with no explicit state_table, the ref resolves to the graph
  # dataset (not the events dataset).
  state_proj, state_ds, state_name = parse_state_table_ref(
      DEFAULT_STATE_TABLE_NAME, default_project=proj, default_dataset=ds
  )
  assert (state_proj, state_ds) == ("graph-proj", "graph_ds")
  assert state_name == DEFAULT_STATE_TABLE_NAME


def test_state_table_defaults_to_events_in_single_dataset() -> None:
  from bigquery_agent_analytics.materialize_window import _state_table_defaults

  assert _state_table_defaults("p", "d", None, None) == ("p", "d")


# --------------------------------------------------------------------------- #
# Deployed-graph mode: fetch the DDL from INFORMATION_SCHEMA.PROPERTY_GRAPHS
# --------------------------------------------------------------------------- #

# Captured live from BigQuery (test-project-0728-467323, 2026-06): the
# normalized ``ddl`` column INFORMATION_SCHEMA.PROPERTY_GRAPHS returns for the
# codelab graph, genericized to project ``p`` / dataset ``d``. Shape pins the
# parser against the normalized form: plain ``CREATE PROPERTY GRAPH`` (no
# ``OR REPLACE``), newline-heavy layout, ``KEY (a,b)`` without spaces, edge
# PROPERTIES that include the SDK metadata columns, trailing semicolon.
_INFOSCHEMA_DDL = """CREATE PROPERTY GRAPH `p.d.agent_decisions_graph`
NODE TABLES (
`p.d.decision_request` AS decision_request
KEY (request_id)
LABEL DecisionRequest PROPERTIES (request_id, request_text, requested_at),

`p.d.decision_option` AS decision_option
KEY (option_id)
LABEL DecisionOption PROPERTIES (option_id, option_label, confidence))

EDGE TABLES (
`p.d.evaluates_option` AS evaluates_option
KEY (request_id,option_id)
SOURCE KEY (request_id)
  REFERENCES decision_request (request_id)
DESTINATION KEY (option_id)
  REFERENCES decision_option (option_id)
LABEL evaluatesOption PROPERTIES (request_id, option_id, session_id, extracted_at));
"""

_SCHEMAS_WITH_METADATA = {
    "decision_request": _SCHEMAS["decision_request"]
    + [
        {"column_name": "session_id", "data_type": "STRING"},
        {"column_name": "extracted_at", "data_type": "TIMESTAMP"},
    ],
    "decision_option": _SCHEMAS["decision_option"]
    + [
        {"column_name": "session_id", "data_type": "STRING"},
        {"column_name": "extracted_at", "data_type": "TIMESTAMP"},
    ],
    "evaluates_option": _SCHEMAS["evaluates_option"]
    + [
        {"column_name": "session_id", "data_type": "STRING"},
        {"column_name": "extracted_at", "data_type": "TIMESTAMP"},
    ],
}


class _FakeGraphClient:
  """Dispatches on the query's parameter name.

  ``graph_name`` -> INFORMATION_SCHEMA.PROPERTY_GRAPHS lookup;
  ``table_name`` -> INFORMATION_SCHEMA.COLUMNS lookup; a parameter-less query
  is the available-graphs listing used for not-found error messages. Records
  every SQL string for assertions.
  """

  def __init__(self, graphs, schemas):
    self._graphs = graphs  # {graph_name: ddl}
    self._schemas = schemas
    self.queries = []

  def query(self, sql, job_config=None):
    self.queries.append(sql)
    if job_config is None:
      return _FakeJob(
          [{"property_graph_name": name} for name in sorted(self._graphs)]
      )
    param = job_config.query_parameters[0]
    if param.name == "graph_name":
      ddl = self._graphs.get(param.value)
      return _FakeJob([{"ddl": ddl}] if ddl is not None else [])
    return _FakeJob(self._schemas.get(param.value, []))


def test_split_graph_ref_qualification() -> None:
  from bigquery_agent_analytics.property_graph_spec import split_graph_ref

  assert split_graph_ref("g", default_project="p", default_dataset="d") == (
      "p",
      "d",
      "g",
  )
  assert split_graph_ref("ds2.g", default_project="p", default_dataset="d") == (
      "p",
      "ds2",
      "g",
  )
  assert split_graph_ref(
      "p2.ds2.g", default_project="p", default_dataset="d"
  ) == ("p2", "ds2", "g")


def test_split_graph_ref_rejects_too_many_parts() -> None:
  from bigquery_agent_analytics.property_graph_spec import PropertyGraphLookupError
  from bigquery_agent_analytics.property_graph_spec import split_graph_ref

  with pytest.raises(PropertyGraphLookupError, match="1-3"):
    split_graph_ref("a.b.c.d", default_project="p", default_dataset="d")


def test_fetch_property_graph_ddl_happy_path() -> None:
  from bigquery_agent_analytics.property_graph_spec import fetch_property_graph_ddl

  client = _FakeGraphClient(
      {"agent_decisions_graph": _INFOSCHEMA_DDL}, _SCHEMAS_WITH_METADATA
  )
  ddl = fetch_property_graph_ddl(
      client,
      project_id="p",
      dataset_id="d",
      graph_name="agent_decisions_graph",
  )
  assert ddl == _INFOSCHEMA_DDL
  # The lookup is dataset-qualified (no region qualifier needed).
  assert "`p.d`.INFORMATION_SCHEMA.PROPERTY_GRAPHS" in client.queries[0]


def test_fetch_property_graph_ddl_qualified_name_overrides_defaults() -> None:
  from bigquery_agent_analytics.property_graph_spec import fetch_property_graph_ddl

  client = _FakeGraphClient(
      {"agent_decisions_graph": _INFOSCHEMA_DDL}, _SCHEMAS_WITH_METADATA
  )
  fetch_property_graph_ddl(
      client,
      project_id="p",
      dataset_id="d",
      graph_name="p2.graph_ds.agent_decisions_graph",
  )
  assert "`p2.graph_ds`.INFORMATION_SCHEMA.PROPERTY_GRAPHS" in (
      client.queries[0]
  )


def test_fetch_property_graph_ddl_not_found_lists_available() -> None:
  from bigquery_agent_analytics.property_graph_spec import fetch_property_graph_ddl
  from bigquery_agent_analytics.property_graph_spec import PropertyGraphLookupError

  client = _FakeGraphClient(
      {"other_graph": "CREATE PROPERTY GRAPH ..."}, _SCHEMAS_WITH_METADATA
  )
  with pytest.raises(PropertyGraphLookupError) as exc_info:
    fetch_property_graph_ddl(
        client, project_id="p", dataset_id="d", graph_name="missing_graph"
    )
  assert "missing_graph" in str(exc_info.value)
  assert "other_graph" in str(exc_info.value)


def test_fetch_property_graph_ddl_not_found_empty_dataset() -> None:
  from bigquery_agent_analytics.property_graph_spec import fetch_property_graph_ddl
  from bigquery_agent_analytics.property_graph_spec import PropertyGraphLookupError

  client = _FakeGraphClient({}, _SCHEMAS_WITH_METADATA)
  with pytest.raises(PropertyGraphLookupError, match="no property graphs"):
    fetch_property_graph_ddl(
        client, project_id="p", dataset_id="d", graph_name="missing_graph"
    )


def test_orchestrator_resolves_via_deployed_graph() -> None:
  # End-to-end: --graph mode fetches the normalized INFORMATION_SCHEMA DDL
  # and derives the same ontology + binding the file path would. The fixture
  # is the live-captured normalized form, so this also pins parser tolerance
  # for that shape (KEY without spaces, metadata columns in edge PROPERTIES,
  # no OR REPLACE).
  from bigquery_agent_analytics.materialize_window import _resolve_ontology_binding

  client = _FakeGraphClient(
      {"agent_decisions_graph": _INFOSCHEMA_DDL}, _SCHEMAS_WITH_METADATA
  )
  ontology, binding = _resolve_ontology_binding(
      ontology_path=None,
      binding_path=None,
      property_graph_path=None,
      graph="agent_decisions_graph",
      project_id="p",
      dataset_id="d",
      bq_client=client,
  )
  assert ontology.ontology == "agent_decisions_graph"
  assert {e.name for e in ontology.entities} == {
      "DecisionRequest",
      "DecisionOption",
  }
  assert [r.name for r in ontology.relationships] == ["evaluatesOption"]
  request_binding = next(
      e for e in binding.entities if e.name == "DecisionRequest"
  )
  assert request_binding.source == "p.d.decision_request"


def test_orchestrator_graph_mode_uses_graph_dataset_for_lookup() -> None:
  # Split-dataset deploy: the deployed graph lives in the graph dataset, so
  # the INFORMATION_SCHEMA lookup must target graph_dataset_id, not the
  # events dataset.
  from bigquery_agent_analytics.materialize_window import _resolve_ontology_binding

  graph_ddl = _INFOSCHEMA_DDL.replace("`p.d.", "`p.graph_ds.")
  client = _FakeGraphClient(
      {"agent_decisions_graph": graph_ddl}, _SCHEMAS_WITH_METADATA
  )
  _, binding = _resolve_ontology_binding(
      ontology_path=None,
      binding_path=None,
      property_graph_path=None,
      graph="agent_decisions_graph",
      project_id="p",
      dataset_id="events_ds",
      graph_dataset_id="graph_ds",
      bq_client=client,
  )
  assert "`p.graph_ds`.INFORMATION_SCHEMA.PROPERTY_GRAPHS" in (
      client.queries[0]
  )
  assert binding.target.dataset == "graph_ds"


def test_orchestrator_rejects_graph_with_other_modes() -> None:
  from bigquery_agent_analytics.materialize_window import _resolve_ontology_binding

  with pytest.raises(ValueError, match="exactly one"):
    _resolve_ontology_binding(
        ontology_path=None,
        binding_path=None,
        property_graph_path="g.sql",
        graph="agent_decisions_graph",
        project_id="p",
        dataset_id="d",
        bq_client=_FakeClient({}),
    )


def test_normalize_graph_target_qualified_ref_sets_graph_target() -> None:
  # P2 from PR review: a qualified --graph ref must drive the WHOLE graph
  # target (state table, binding target), not just the INFORMATION_SCHEMA
  # lookup — otherwise a CLI user with a read-only events dataset writes
  # state into it.
  from bigquery_agent_analytics.materialize_window import _normalize_graph_target
  from bigquery_agent_analytics.materialize_window import _state_table_defaults

  gp, gd, name = _normalize_graph_target(
      "p2.graph_ds.my_graph", "events-proj", "events_ds", None, None
  )
  assert (gp, gd, name) == ("p2", "graph_ds", "my_graph")
  # The state table then follows the graph's own dataset.
  assert _state_table_defaults("events-proj", "events_ds", gp, gd) == (
      "p2",
      "graph_ds",
  )

  # dataset.graph: project falls back to the events project.
  gp, gd, name = _normalize_graph_target(
      "graph_ds.my_graph", "events-proj", "events_ds", None, None
  )
  assert (gp, gd, name) == ("events-proj", "graph_ds", "my_graph")


def test_normalize_graph_target_bare_name_keeps_explicit_args() -> None:
  from bigquery_agent_analytics.materialize_window import _normalize_graph_target

  # Bare name + explicit graph_* args (the run_job split-dataset shape).
  assert _normalize_graph_target(
      "my_graph", "events-proj", "events_ds", "gproj", "gds"
  ) == ("gproj", "gds", "my_graph")
  # Bare name, single-dataset shape: falls back to the events target.
  assert _normalize_graph_target("my_graph", "p", "d", None, None) == (
      "p",
      "d",
      "my_graph",
  )
