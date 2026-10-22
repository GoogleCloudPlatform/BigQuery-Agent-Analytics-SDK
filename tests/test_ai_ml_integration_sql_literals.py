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

"""The AI/ML integration classes keep endpoints and models in their tokens.

``BatchEvaluator`` puts its ``endpoint`` (or the deprecated
``eval_model``) between single quotes in its AI.GENERATE query, and so
does ``BigQueryAIClient.generate_text`` with the client's ``endpoint``
(or the deprecated ``text_model``) or a per-call ``endpoint``.
``BigQueryAIClient`` and ``EmbeddingSearchClient`` put
``embedding_endpoint`` between single quotes for AI.EMBED, and an
``embedding_model`` with two or more dots between backticks as
``MODEL `{model}``` for ML.GENERATE_EMBEDDING. A quote, backtick or
backslash in any of them ended its token early, so the rest of the value
became SQL.
"""

import asyncio
from unittest.mock import MagicMock

import pytest

from bigquery_agent_analytics.ai_ml_integration import BatchEvaluator
from bigquery_agent_analytics.ai_ml_integration import BigQueryAIClient
from bigquery_agent_analytics.ai_ml_integration import EmbeddingSearchClient
from tests.test_judge_sql_literals import _ENDPOINT_PAYLOADS
from tests.test_judge_sql_literals import _LEGACY_MODEL_PAYLOADS
from tests.test_judge_sql_literals import _token_after

# What follows the endpoint literal in both AI.GENERATE queries.
_THEN_MODEL_PARAMS = ",\n    model_params => JSON '"


def _batch_evaluator(bq, **options):
  return BatchEvaluator(project_id="p", dataset_id="d", client=bq, **options)


def _ai_client(bq, **options):
  return BigQueryAIClient(project_id="p", dataset_id="d", client=bq, **options)


def _search_client(bq, **options):
  return EmbeddingSearchClient(
      project_id="p", dataset_id="d", client=bq, **options
  )


def _evaluate_with_endpoint(bq, value):
  asyncio.run(_batch_evaluator(bq, endpoint=value).evaluate_recent_sessions())


def _evaluate_with_eval_model(bq, value):
  asyncio.run(_batch_evaluator(bq, eval_model=value).evaluate_recent_sessions())


def _generate_with_client_endpoint(bq, value):
  asyncio.run(_ai_client(bq, endpoint=value).generate_text("hello"))


def _generate_with_text_model(bq, value):
  asyncio.run(_ai_client(bq, text_model=value).generate_text("hello"))


def _generate_with_call_endpoint(bq, value):
  asyncio.run(_ai_client(bq).generate_text("hello", endpoint=value))


def _embed_with_endpoint(bq, value):
  ai = _ai_client(bq, embedding_endpoint=value)
  asyncio.run(ai.generate_embeddings(["hello"]))


def _embed_with_model(bq, value):
  ai = _ai_client(bq, embedding_model=value)
  asyncio.run(ai.generate_embeddings(["hello"]))


def _index_with_endpoint(bq, value):
  search = _search_client(bq, embedding_endpoint=value)
  asyncio.run(search.build_embeddings_index())


def _index_with_model(bq, value):
  search = _search_client(bq, embedding_model=value)
  asyncio.run(search.build_embeddings_index())


# Each way an endpoint reaches a single-quoted literal: the call that
# sends it, what follows the literal, and a valid value.
_ENDPOINT_SINKS = {
    "BatchEvaluator-endpoint": (
        _evaluate_with_endpoint,
        _THEN_MODEL_PARAMS,
        "gemini-2.5-flash",
    ),
    "BatchEvaluator-eval_model": (
        _evaluate_with_eval_model,
        _THEN_MODEL_PARAMS,
        "gemini-2.5-pro",
    ),
    "generate_text-client_endpoint": (
        _generate_with_client_endpoint,
        _THEN_MODEL_PARAMS,
        "gemini-2.5-flash",
    ),
    "generate_text-text_model": (
        _generate_with_text_model,
        _THEN_MODEL_PARAMS,
        "gemini-2.5-pro",
    ),
    "generate_text-call_endpoint": (
        _generate_with_call_endpoint,
        _THEN_MODEL_PARAMS,
        "gemini-3-flash-preview",
    ),
    "generate_embeddings-embedding_endpoint": (
        _embed_with_endpoint,
        "\n    ).result AS embedding\n",
        "text-embedding-005",
    ),
    "build_embeddings_index-embedding_endpoint": (
        _index_with_endpoint,
        "\n    ).result as embedding\n",
        "text-embedding-005",
    ),
}

# Each way a two-dot embedding_model reaches a backtick-quoted MODEL
# reference: the call that sends it, what follows it, and a valid value.
_MODEL_SINKS = {
    "generate_embeddings-embedding_model": (
        _embed_with_model,
        ",\n      STRUCT(content AS content)\n",
        "my-project.my_dataset.text_embedding",
    ),
    "build_embeddings_index-embedding_model": (
        _index_with_model,
        ",\n      STRUCT(JSON_EXTRACT_SCALAR(e.content, ",
        "example.com:my-project.my_dataset.text_embedding",
    ),
}


def _sent_sql(send, value):
  """The one query *send* sends with *value* in place."""
  bq = MagicMock()
  bq.query.return_value.result.return_value = []
  send(bq, value)
  [call] = bq.query.call_args_list
  return call.args[0]


@pytest.mark.parametrize(
    "send,then,valid", list(_ENDPOINT_SINKS.values()), ids=list(_ENDPOINT_SINKS)
)
class TestEndpointLiterals:
  """``endpoint => '...'`` stays one string literal."""

  @pytest.mark.parametrize(
      "endpoint",
      list(_ENDPOINT_PAYLOADS.values()),
      ids=list(_ENDPOINT_PAYLOADS),
  )
  def test_endpoint_stays_one_string_literal(self, send, then, valid, endpoint):
    sql = _sent_sql(send, endpoint)
    value, end = _token_after(sql, "endpoint => ")
    assert value == endpoint
    assert sql[end:].startswith(then)

  def test_valid_endpoint_renders_unchanged(self, send, then, valid):
    sql = _sent_sql(send, valid)
    assert f"endpoint => '{valid}'{then}" in sql


@pytest.mark.parametrize(
    "send,then,valid", list(_MODEL_SINKS.values()), ids=list(_MODEL_SINKS)
)
class TestModelReferences:
  """``MODEL `...``` stays one quoted identifier."""

  @pytest.mark.parametrize(
      "model",
      list(_LEGACY_MODEL_PAYLOADS.values()),
      ids=list(_LEGACY_MODEL_PAYLOADS),
  )
  def test_model_reference_stays_one_quoted_identifier(
      self, send, then, valid, model
  ):
    sql = _sent_sql(send, model)
    value, end = _token_after(sql, "MODEL ")
    assert value == model
    assert sql[end:].startswith(then)

  def test_valid_model_reference_renders_unchanged(self, send, then, valid):
    sql = _sent_sql(send, valid)
    assert f"MODEL `{valid}`{then}" in sql


def test_batch_evaluator_audit_payload_renders_escaped():
  sql = _sent_sql(
      _evaluate_with_endpoint, _ENDPOINT_PAYLOADS["quote_adds_argument"]
  )
  assert (
      "\n    endpoint => 'gemini-2.5-flash\\', request_type => \\'SHARED',"
      "\n    model_params => JSON '"
  ) in sql
