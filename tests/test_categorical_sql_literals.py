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

"""Categorical evaluation keeps labels, endpoint and connection in literals.

``build_classify_categories_literal`` puts each category's name and
definition between single quotes in AI.CLASSIFY's ``categories`` array,
and ``build_ai_classify_query`` and ``build_ai_generate_query`` put the
endpoint and ``connection_id`` between single quotes.
``Client.evaluate_categorical`` passes the config's or the client's
endpoint. The shared escape helper only doubled single quotes. GoogleSQL
literals use backslash escapes, so a backslash before the closing quote
escaped it and the rest of the value became SQL, and line breaks and
other control characters were left raw.
"""

from unittest.mock import MagicMock

import pytest

from bigquery_agent_analytics import Client
from bigquery_agent_analytics.categorical_evaluator import build_ai_classify_query
from bigquery_agent_analytics.categorical_evaluator import build_ai_generate_query
from bigquery_agent_analytics.categorical_evaluator import build_classify_categories_literal
from bigquery_agent_analytics.categorical_evaluator import CategoricalEvaluationConfig
from bigquery_agent_analytics.categorical_evaluator import CategoricalMetricCategory
from bigquery_agent_analytics.categorical_evaluator import CategoricalMetricDefinition
from tests.test_judge_sql_literals import _CONNECTION_PAYLOADS
from tests.test_judge_sql_literals import _ENDPOINT_PAYLOADS
from tests.test_judge_sql_literals import _read_quoted
from tests.test_judge_sql_literals import _token_after

# Category names or definitions that end their literal early, or change
# its value, when the helper only doubles single quotes.
_LABEL_PAYLOADS = {
    "apostrophe": "User's satisfied.",
    "backslash_quote": "positive\\', 'forged",
    "trailing_backslash": "positive\\",
    "line_break": "positive'),\n('forged",
    "carriage_return": "positive" + chr(13),
    "tab": "very" + chr(9) + "positive",
    "nul": "positive" + chr(0),
    "delete": "positive" + chr(0x7F),
}

# What follows each literal in the AI.CLASSIFY column and in both
# AI.GENERATE queries. The connection always comes before the endpoint.
_THEN_ENDPOINT = ",\n    endpoint => '"
_THEN_CLASSIFY_COLUMN = "\n  ) AS classify_0"
_THEN_MODEL_PARAMS = ",\n    model_params => JSON '"


def _metric(name="positive", definition="User is satisfied."):
  return CategoricalMetricDefinition(
      name="tone",
      definition="Overall tone.",
      categories=[
          CategoricalMetricCategory(name=name, definition="Happy."),
          CategoricalMetricCategory(name="negative", definition=definition),
      ],
  )


def _config(**overrides):
  return CategoricalEvaluationConfig(metrics=[_metric()], **overrides)


def _categories(literal):
  """Decodes AI.CLASSIFY's ``[('name', 'definition'), ...]`` literal.

  Fails unless the literal is exactly that shape, so a value that ends
  its literal early can't pass.
  """
  pairs = []
  assert literal[0] == "[", literal
  index = 1
  while literal[index] != "]":
    if pairs:
      assert literal[index : index + 2] == ", ", literal
      index += 2
    assert literal[index] == "(", literal
    name, index = _read_quoted(literal, index + 1)
    assert literal[index : index + 2] == ", ", literal
    definition, index = _read_quoted(literal, index + 2)
    assert literal[index] == ")", literal
    index += 1
    pairs.append((name, definition))
  assert index == len(literal) - 1, literal
  return pairs


def _classify_sql(endpoint="gemini-2.5-flash", connection_id=None):
  return build_ai_classify_query(
      config=_config(),
      project="p",
      dataset="d",
      table="t",
      where="1=1",
      endpoint=endpoint,
      connection_id=connection_id,
  )


def _generate_sql(endpoint="gemini-2.5-flash", connection_id=None):
  return build_ai_generate_query(
      "p", "d", "t", "1=1", endpoint, 0.0, connection_id=connection_id
  )


def _identity_bound_generate_sql(
    endpoint="gemini-2.5-flash", connection_id=None
):
  return build_ai_generate_query(
      "p",
      "d",
      "t",
      "1=1",
      endpoint,
      0.0,
      connection_id=connection_id,
      identity_bound=True,
  )


def _evaluate_categorical_sql(endpoint="gemini-2.5-flash", connection_id=None):
  bq = MagicMock()
  bq.query.return_value.result.return_value = []
  client = Client(
      project_id="p", dataset_id="d", verify_schema=False, bq_client=bq
  )
  client.evaluate_categorical(
      config=_config(endpoint=endpoint, connection_id=connection_id)
  )
  [call] = bq.query.call_args_list
  return call.args[0]


# Each query that holds an endpoint literal, and what follows it.
_ENDPOINT_SINKS = {
    "build_ai_classify_query": (_classify_sql, _THEN_CLASSIFY_COLUMN),
    "build_ai_generate_query": (_generate_sql, _THEN_MODEL_PARAMS),
    "build_ai_generate_query-identity_bound": (
        _identity_bound_generate_sql,
        _THEN_MODEL_PARAMS,
    ),
    "Client.evaluate_categorical": (
        _evaluate_categorical_sql,
        _THEN_MODEL_PARAMS,
    ),
}


class TestCategoryLabels:
  """Each name and definition stays one literal in the categories array."""

  @pytest.mark.parametrize(
      "label", list(_LABEL_PAYLOADS.values()), ids=list(_LABEL_PAYLOADS)
  )
  def test_name_stays_one_string_literal(self, label):
    literal = build_classify_categories_literal(_metric(name=label))
    assert _categories(literal) == [
        (label, "Happy."),
        ("negative", "User is satisfied."),
    ]

  @pytest.mark.parametrize(
      "label", list(_LABEL_PAYLOADS.values()), ids=list(_LABEL_PAYLOADS)
  )
  def test_definition_stays_one_string_literal(self, label):
    literal = build_classify_categories_literal(_metric(definition=label))
    assert _categories(literal) == [("positive", "Happy."), ("negative", label)]

  def test_classify_query_carries_the_decoded_labels(self):
    label = _LABEL_PAYLOADS["backslash_quote"]
    sql = build_ai_classify_query(
        config=CategoricalEvaluationConfig(metrics=[_metric(name=label)]),
        project="p",
        dataset="d",
        table="t",
        where="1=1",
    )
    marker = "categories => "
    start = sql.index(marker) + len(marker)
    end = sql.index("]\n", start) + 1
    assert _categories(sql[start:end]) == [
        (label, "Happy."),
        ("negative", "User is satisfied."),
    ]

  def test_plain_labels_render_unchanged(self):
    metric = _metric(
        name='très "bien"', definition="50% of users said: no change."
    )
    assert build_classify_categories_literal(metric) == (
        "[('très \"bien\"', 'Happy.'),"
        " ('negative', '50% of users said: no change.')]"
    )


@pytest.mark.parametrize(
    "sql_for,then",
    list(_ENDPOINT_SINKS.values()),
    ids=list(_ENDPOINT_SINKS),
)
class TestEndpointAndConnection:
  """``endpoint => '...'`` and ``connection_id => '...'`` stay literals."""

  @pytest.mark.parametrize(
      "endpoint",
      list(_ENDPOINT_PAYLOADS.values()),
      ids=list(_ENDPOINT_PAYLOADS),
  )
  def test_endpoint_stays_one_string_literal(self, sql_for, then, endpoint):
    sql = sql_for(endpoint=endpoint)
    value, end = _token_after(sql, "endpoint => ")
    assert value == endpoint
    assert sql[end:].startswith(then)

  @pytest.mark.parametrize(
      "connection_id",
      list(_CONNECTION_PAYLOADS.values()),
      ids=list(_CONNECTION_PAYLOADS),
  )
  def test_connection_stays_one_string_literal(
      self, sql_for, then, connection_id
  ):
    sql = sql_for(connection_id=connection_id)
    value, end = _token_after(sql, "connection_id => ")
    assert value == connection_id
    assert sql[end:].startswith(_THEN_ENDPOINT)

  def test_valid_values_render_unchanged(self, sql_for, then):
    sql = sql_for(endpoint="gemini-2.5-pro", connection_id="my-proj.us.conn")
    assert (
        "connection_id => 'my-proj.us.conn'"
        f"{_THEN_ENDPOINT}gemini-2.5-pro'{then}"
    ) in sql


def test_audit_payload_renders_escaped():
  sql = _generate_sql(endpoint=_ENDPOINT_PAYLOADS["quote_adds_argument"])
  assert (
      "\n    endpoint => 'gemini-2.5-flash\\', request_type => \\'SHARED',"
      "\n    model_params => JSON '"
  ) in sql
