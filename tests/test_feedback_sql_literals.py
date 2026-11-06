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

"""Drift and question distribution keep their model inside its SQL token.

``compute_drift`` puts its ``embedding_model`` between single quotes as
AI.EMBED's ``endpoint``, or, when it has two or more dots, between
backticks as ML.GENERATE_EMBEDDING's ``MODEL``; both appear twice, once
for the golden questions and once for production.
``compute_question_distribution`` puts its ``text_model`` between single
quotes as AI.GENERATE's ``endpoint``, or, with two or more dots, between
backticks as ML.GENERATE_TEXT's ``MODEL``. ``Client.drift_detection`` and
``deep_analysis`` pass the caller's model, and deep analysis defaults to
the client's endpoint. A quote, backtick or backslash in the model ended
its token early, so the rest of the value became SQL.
"""

import asyncio
from unittest.mock import MagicMock

import pytest

from bigquery_agent_analytics import Client
from bigquery_agent_analytics.feedback import AnalysisConfig
from bigquery_agent_analytics.feedback import compute_drift
from bigquery_agent_analytics.feedback import compute_question_distribution
from tests.test_judge_sql_literals import _ENDPOINT_PAYLOADS
from tests.test_judge_sql_literals import _LEGACY_MODEL_PAYLOADS
from tests.test_judge_sql_literals import _read_quoted

# What follows each endpoint literal in drift's AI.EMBED query.
_THEN_EMBEDDING = "\n    ).result AS embedding\n"
# What follows the endpoint literal in the AI.GENERATE grouping query.
_THEN_MODEL_PARAMS = ",\n  model_params => JSON '"
# What follows the golden and the production MODEL in drift's legacy query.
_THEN_GOLDEN_STRUCT = ",\n      STRUCT(question AS content)\n"
_THEN_PRODUCTION_STRUCT = ",\n      STRUCT(\n        JSON_EXTRACT_SCALAR("
# What follows the MODEL in the legacy grouping query.
_THEN_PROMPT_STRUCT = ",\n    STRUCT(\n      CONCAT(\n"


def _bq_client(fail_on=None):
  """Fake BigQuery client whose every query returns no rows.

  A query that contains *fail_on* raises instead.
  """

  def query(sql, job_config=None):
    if fail_on and fail_on in sql:
      raise RuntimeError(f"{fail_on} is unavailable")
    job = MagicMock()
    job.result.return_value = []
    return job

  bq = MagicMock()
  bq.query.side_effect = query
  return bq


def _client(bq, **options):
  return Client(
      project_id="p",
      dataset_id="d",
      verify_schema=False,
      bq_client=bq,
      **options,
  )


def _compute_drift(bq, model):
  asyncio.run(
      compute_drift(
          bq_client=bq,
          project_id="p",
          dataset_id="d",
          table_id="agent_events",
          golden_table="golden_questions",
          where_clause="1=1",
          query_params=[],
          embedding_model=model,
      )
  )


def _drift_detection(bq, model):
  _client(bq).drift_detection(
      golden_dataset="golden_questions", embedding_model=model
  )


def _drift_detection_async(bq, model):
  asyncio.run(
      _client(bq).drift_detection_async(
          golden_dataset="golden_questions", embedding_model=model
      )
  )


def _compute_question_distribution(bq, model):
  asyncio.run(
      compute_question_distribution(
          bq_client=bq,
          project_id="p",
          dataset_id="d",
          table_id="agent_events",
          where_clause="1=1",
          query_params=[],
          config=AnalysisConfig(),
          text_model=model,
      )
  )


def _deep_analysis(bq, model):
  _client(bq).deep_analysis(text_model=model)


def _deep_analysis_async(bq, model):
  asyncio.run(_client(bq).deep_analysis_async(text_model=model))


def _deep_analysis_with_client_endpoint(bq, model):
  _client(bq, endpoint=model).deep_analysis()


_DRIFT_CALLS = {
    "compute_drift": _compute_drift,
    "drift_detection": _drift_detection,
    "drift_detection_async": _drift_detection_async,
}

_DISTRIBUTION_CALLS = {
    "compute_question_distribution": _compute_question_distribution,
    "deep_analysis": _deep_analysis,
    "deep_analysis_async": _deep_analysis_async,
    "deep_analysis-client_endpoint": _deep_analysis_with_client_endpoint,
}

# Each way a model reaches a single-quoted endpoint literal: the call, the
# function whose query holds the literals, and what follows each literal.
_ENDPOINT_SINKS = {
    **{
        f"{name}-AI.EMBED": (call, "AI.EMBED(", [_THEN_EMBEDDING] * 2)
        for name, call in _DRIFT_CALLS.items()
    },
    **{
        f"{name}-AI.GENERATE": (call, "AI.GENERATE(", [_THEN_MODEL_PARAMS])
        for name, call in _DISTRIBUTION_CALLS.items()
    },
}

# Each way a two-dot model reaches a backtick-quoted MODEL reference.
_MODEL_SINKS = {
    **{
        f"{name}-ML.GENERATE_EMBEDDING": (
            call,
            "ML.GENERATE_EMBEDDING(",
            [_THEN_GOLDEN_STRUCT, _THEN_PRODUCTION_STRUCT],
        )
        for name, call in _DRIFT_CALLS.items()
    },
    **{
        f"{name}-ML.GENERATE_TEXT": (
            call,
            "ML.GENERATE_TEXT(",
            [_THEN_PROMPT_STRUCT],
        )
        for name, call in _DISTRIBUTION_CALLS.items()
    },
}


def _query_calling(bq, function):
  """The one query sent to *bq* that calls *function*."""
  [sql] = [
      call.args[0]
      for call in bq.query.call_args_list
      if function in call.args[0]
  ]
  return sql


def _sent_sql(send, function, model):
  bq = _bq_client()
  send(bq, model)
  return _query_calling(bq, function)


def _tokens_after(sql, marker):
  """Decodes the quoted token after each *marker*, in order.

  Returns each token's value and what follows it, up to the next marker.
  """
  tokens = []
  index = sql.find(marker)
  while index != -1:
    value, end = _read_quoted(sql, index + len(marker))
    index = sql.find(marker, end)
    tokens.append((value, sql[end : None if index == -1 else index]))
  return tokens


def _assert_tokens(sql, marker, value, thens):
  tokens = _tokens_after(sql, marker)
  assert [token for token, _ in tokens] == [value] * len(thens)
  for (_, rest), then in zip(tokens, thens):
    assert rest.startswith(then)


@pytest.mark.parametrize(
    "send,function,thens",
    list(_ENDPOINT_SINKS.values()),
    ids=list(_ENDPOINT_SINKS),
)
class TestEndpointLiterals:
  """``endpoint => '...'`` stays one string literal."""

  @pytest.mark.parametrize(
      "model",
      list(_ENDPOINT_PAYLOADS.values()),
      ids=list(_ENDPOINT_PAYLOADS),
  )
  def test_endpoint_stays_one_string_literal(
      self, send, function, thens, model
  ):
    sql = _sent_sql(send, function, model)
    _assert_tokens(sql, "endpoint => ", model, thens)

  def test_valid_endpoint_renders_unchanged(self, send, function, thens):
    model = (
        "text-embedding-005" if function == "AI.EMBED(" else "gemini-2.5-pro"
    )
    sql = _sent_sql(send, function, model)
    for then in thens:
      assert f"endpoint => '{model}'{then}" in sql
    assert sql.count("endpoint => ") == len(thens)


@pytest.mark.parametrize(
    "send,function,thens", list(_MODEL_SINKS.values()), ids=list(_MODEL_SINKS)
)
class TestModelReferences:
  """``MODEL `...``` stays one quoted identifier."""

  @pytest.mark.parametrize(
      "model",
      list(_LEGACY_MODEL_PAYLOADS.values()),
      ids=list(_LEGACY_MODEL_PAYLOADS),
  )
  def test_model_reference_stays_one_quoted_identifier(
      self, send, function, thens, model
  ):
    sql = _sent_sql(send, function, model)
    _assert_tokens(sql, "MODEL ", model, thens)

  @pytest.mark.parametrize(
      "model",
      [
          "my-project.my_dataset.my_model",
          "example.com:my-project.my_dataset.my_model",
      ],
  )
  def test_valid_model_reference_renders_unchanged(
      self, send, function, thens, model
  ):
    sql = _sent_sql(send, function, model)
    for then in thens:
      assert f"MODEL `{model}`{then}" in sql
    assert sql.count("MODEL ") == len(thens)


def test_drift_audit_payload_renders_escaped():
  sql = _sent_sql(
      _compute_drift, "AI.EMBED(", _ENDPOINT_PAYLOADS["quote_adds_argument"]
  )
  escaped = "'gemini-2.5-flash\\', request_type => \\'SHARED'"
  assert sql.count(f"endpoint => {escaped}{_THEN_EMBEDDING}") == 2


def test_grouping_fallback_model_renders_unchanged():
  # When AI.GENERATE fails, grouping falls back to the dataset's default
  # model, built from the checked project and dataset IDs.
  bq = _bq_client(fail_on="AI.GENERATE(")
  _compute_question_distribution(bq, "gemini-2.5-flash")
  sql = _query_calling(bq, "ML.GENERATE_TEXT(")
  assert f"MODEL `p.d.gemini_text_model`{_THEN_PROMPT_STRUCT}" in sql


# Custom categories that left the prompt's categories literal, or changed
# it, when their quotes were doubled.
_CUSTOM_CATEGORIES = {
    "apostrophe": ["User's satisfied", "Billing"],
    "quote_adds_sql": ["Other', (SELECT secret FROM `p.private.secrets`), '"],
    "trailing_backslash": ["Billing\\"],
    "line_break": ["Billing',\n'Refunds"],
    "carriage_return": ["Billing" + chr(13)],
    "nul": ["Billing" + chr(0)],
}

_DEFAULT_CATEGORIES = (
    "General Inquiry, Technical Support, Account Management, Feature"
    " Request, Bug Report, Other"
)


def _grouping_sql(categories, function, model, via_client=False):
  """The grouping query that calls *function*, with these categories."""
  bq = _bq_client()
  config = AnalysisConfig(mode="custom", custom_categories=categories)
  if via_client:
    _client(bq).deep_analysis(configuration=config, text_model=model)
  else:
    asyncio.run(
        compute_question_distribution(
            bq_client=bq,
            project_id="p",
            dataset_id="d",
            table_id="agent_events",
            where_clause="1=1",
            query_params=[],
            config=config,
            text_model=model,
        )
    )
  return _query_calling(bq, function)


def _categories_literal(sql):
  """The prompt's categories literal, decoded, and what follows it."""
  value, end = _read_quoted(sql, sql.index("'Categories: "))
  return value, sql[end:]


# Both grouping queries: the function each calls and a model that picks it.
_GROUPING_PATHS = {
    "AI.GENERATE": ("AI.GENERATE(", "gemini-2.5-flash"),
    "ML.GENERATE_TEXT": ("ML.GENERATE_TEXT(", "proj.ds.my_model"),
}


@pytest.mark.parametrize(
    "function,model", list(_GROUPING_PATHS.values()), ids=list(_GROUPING_PATHS)
)
class TestCustomCategories:
  """The categories literal of both grouping prompts stays one literal."""

  @pytest.mark.parametrize(
      "via_client", [False, True], ids=["direct", "client"]
  )
  @pytest.mark.parametrize(
      "categories",
      list(_CUSTOM_CATEGORIES.values()),
      ids=list(_CUSTOM_CATEGORIES),
  )
  def test_categories_stay_one_string_literal(
      self, function, model, categories, via_client
  ):
    sql = _grouping_sql(categories, function, model, via_client)
    value, rest = _categories_literal(sql)
    assert value == f"Categories: {', '.join(categories)}\n"
    assert rest.lstrip(", \n").startswith("'Question: '")

  def test_default_categories_render_unchanged(self, function, model):
    bq = _bq_client()
    asyncio.run(
        compute_question_distribution(
            bq_client=bq,
            project_id="p",
            dataset_id="d",
            table_id="agent_events",
            where_clause="1=1",
            query_params=[],
            config=AnalysisConfig(),
            text_model=model,
        )
    )
    sql = _query_calling(bq, function)
    assert f"'Categories: {_DEFAULT_CATEGORIES}\\n'" in sql
