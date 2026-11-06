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

"""The context and ontology graphs keep their endpoint in its SQL literal.

``ContextGraphManager`` (business-entity and decision-point extraction)
and ``OntologyGraphManager`` (graph extraction, and the SQL that
``get_extraction_sql`` returns for inspection) put the resolved
AI.GENERATE endpoint between single quotes: an ``https://`` URL as given,
or a model name inside the Vertex AI URL built from the project.
``Client.context_graph()`` passes the client's endpoint. A quote or
backslash in the endpoint ended the literal early, so the rest of the
value became SQL.
"""

from unittest.mock import MagicMock

import pytest

from bigquery_agent_analytics import Client
from bigquery_agent_analytics.context_graph import ContextGraphConfig
from bigquery_agent_analytics.context_graph import ContextGraphManager
from bigquery_agent_analytics.ontology_graph import OntologyGraphManager
from tests.test_judge_sql_literals import _ENDPOINT_PAYLOADS
from tests.test_judge_sql_literals import _token_after
from tests.test_ontology_graph import _simple_spec

# The Vertex AI URL that a model name resolves to in project "p".
_MODEL_URL = (
    "https://aiplatform.googleapis.com/v1/projects/p/locations/global/"
    "publishers/google/models/"
)

# What follows the endpoint literal in each AI.GENERATE query.
_THEN_RESULT = "\n        ).result,\n"
_THEN_MODEL_PARAMS = ",\n      model_params => JSON '"
_THEN_OUTPUT_SCHEMA = ",\n        output_schema => 'graph_json STRING'"


def _bq_client():
  """Fake BigQuery client whose every query returns no rows."""
  bq = MagicMock()
  bq.query.return_value.result.return_value = []
  return bq


def _ai_generate_query(bq):
  """The one query sent to *bq* that calls AI.GENERATE."""
  [sql] = [
      call.args[0]
      for call in bq.query.call_args_list
      if "AI.GENERATE(" in call.args[0]
  ]
  return sql


def _context_graph(bq, endpoint):
  return ContextGraphManager(
      project_id="p",
      dataset_id="d",
      config=ContextGraphConfig(endpoint=endpoint),
      client=bq,
  )


def _ontology_graph(bq, endpoint):
  return OntologyGraphManager(
      project_id="p",
      dataset_id="d",
      spec=_simple_spec(),
      endpoint=endpoint,
      bq_client=bq,
  )


def _biz_nodes_sql(endpoint):
  bq = _bq_client()
  _context_graph(bq, endpoint).extract_biz_nodes(session_ids=["s1"])
  return _ai_generate_query(bq)


def _decision_points_sql(endpoint):
  bq = _bq_client()
  _context_graph(bq, endpoint).extract_decision_points(session_ids=["s1"])
  return _ai_generate_query(bq)


def _client_biz_nodes_sql(endpoint):
  bq = _bq_client()
  client = Client(
      project_id="p",
      dataset_id="d",
      verify_schema=False,
      bq_client=bq,
      endpoint=endpoint,
  )
  client.context_graph().extract_biz_nodes(session_ids=["s1"])
  return _ai_generate_query(bq)


def _ontology_sql(endpoint):
  bq = _bq_client()
  _ontology_graph(bq, endpoint).extract_graph(session_ids=["s1"])
  return _ai_generate_query(bq)


def _ontology_inspection_sql(endpoint):
  return _ontology_graph(_bq_client(), endpoint).get_extraction_sql()


# Each AI.GENERATE query that holds the endpoint, and what follows it.
_SINKS = {
    "ContextGraphManager.extract_biz_nodes": (_biz_nodes_sql, _THEN_RESULT),
    "ContextGraphManager.extract_decision_points": (
        _decision_points_sql,
        _THEN_MODEL_PARAMS,
    ),
    "Client.context_graph-extract_biz_nodes": (
        _client_biz_nodes_sql,
        _THEN_RESULT,
    ),
    "OntologyGraphManager.extract_graph": (_ontology_sql, _THEN_OUTPUT_SCHEMA),
    "OntologyGraphManager.get_extraction_sql": (
        _ontology_inspection_sql,
        _THEN_OUTPUT_SCHEMA,
    ),
}


@pytest.mark.parametrize(
    "sql_for,then", list(_SINKS.values()), ids=list(_SINKS)
)
class TestEndpointLiteral:
  """``endpoint => '...'`` stays one string literal."""

  @pytest.mark.parametrize("form", ["model_name", "url"])
  @pytest.mark.parametrize(
      "payload",
      list(_ENDPOINT_PAYLOADS.values()),
      ids=list(_ENDPOINT_PAYLOADS),
  )
  def test_endpoint_stays_one_string_literal(
      self, sql_for, then, payload, form
  ):
    # A model name ends up inside the Vertex AI URL; a URL is used as is.
    endpoint = payload if form == "model_name" else _MODEL_URL + payload
    sql = sql_for(endpoint)
    value, end = _token_after(sql, "endpoint => ")
    assert value == _MODEL_URL + payload
    assert sql[end:].startswith(then)

  @pytest.mark.parametrize(
      "endpoint",
      [
          "gemini-2.5-flash",
          "gemini-3-pro-preview",
          _MODEL_URL + "gemini-3-pro-preview",
      ],
  )
  def test_valid_endpoint_renders_unchanged(self, sql_for, then, endpoint):
    url = endpoint if endpoint.startswith("https://") else _MODEL_URL + endpoint
    assert f"endpoint => '{url}'{then}" in sql_for(endpoint)


def test_audit_payload_renders_escaped():
  sql = _biz_nodes_sql(_ENDPOINT_PAYLOADS["quote_adds_argument"])
  assert (
      f"endpoint => '{_MODEL_URL}gemini-2.5-flash\\', request_type =>"
      f" \\'SHARED'{_THEN_RESULT}"
  ) in sql
