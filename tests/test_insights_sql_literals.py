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

"""Insights keeps its endpoint and model inside their quoted SQL tokens.

``Client.insights()`` and ``insights_async()`` extract session facets
with AI.GENERATE, which puts the client's ``endpoint`` between single
quotes, or with the legacy ML.GENERATE_TEXT, which puts ``text_model``
(by default the client's ``endpoint``) between backticks as
``MODEL `{model}```. An endpoint with two or more dots skips AI.GENERATE,
and any endpoint reaches ML.GENERATE_TEXT when AI.GENERATE fails. The
endpoint is client configuration, which the BigQuery Remote Function
takes from the request's ``userDefinedContext`` before its ``insights``
operation runs this pipeline. A quote, backtick or backslash in either
value used to end its token early, so the rest of the value became SQL.
"""

import asyncio
from unittest.mock import MagicMock

import pytest

from bigquery_agent_analytics import Client
from bigquery_agent_analytics import client as client_module
from bigquery_agent_analytics.insights import AnalysisSection
from tests.test_client_project_dataset_ids import _load
from tests.test_client_project_dataset_ids import _REMOTE_FUNCTION_DIR
from tests.test_judge_sql_literals import _ENDPOINT_PAYLOADS
from tests.test_judge_sql_literals import _LEGACY_MODEL_PAYLOADS
from tests.test_judge_sql_literals import _token_after

# What follows the endpoint literal in the AI.GENERATE facet query.
_THEN_MODEL_PARAMS = ",\n  model_params => JSON '"
# What follows the model reference in the ML.GENERATE_TEXT facet query.
_THEN_PROMPT = ",\n    STRUCT(\n      CONCAT(@facet_prompt, "

# One dot keeps it on the AI.GENERATE path, where its quotes ended the
# literal. When AI.GENERATE fails, the same endpoint becomes the MODEL
# reference, where its backtick ended the identifier.
_FALLBACK_ENDPOINT = "gemini-2.5-flash`, (SELECT 'forged' AS prompt)) --"

# The judge's endpoint payloads, plus the one above.
_AI_GENERATE_ENDPOINTS = {**_ENDPOINT_PAYLOADS, "backtick": _FALLBACK_ENDPOINT}


def _insights(client, **kwargs):
  return client.insights(**kwargs)


def _insights_async(client, **kwargs):
  return asyncio.run(client.insights_async(**kwargs))


_ENTRY_POINTS = {"insights": _insights, "insights_async": _insights_async}


@pytest.fixture(autouse=True)
def _no_analysis_calls(monkeypatch):
  """Stubs the analysis and summary steps, which send no BigQuery query."""

  async def section(prompt_name, context, model="gemini-2.5-flash"):
    return AnalysisSection(title=prompt_name)

  async def summary(report, model="gemini-2.5-flash"):
    return ""

  monkeypatch.setattr(client_module, "run_analysis_prompt", section)
  monkeypatch.setattr(client_module, "generate_executive_summary", summary)


def _bq_client(ai_generate_fails=False):
  """Fake BigQuery client for one insights run.

  The session-metadata query finds one session, so the pipeline goes on
  to extract facets; every other query returns no rows. With
  *ai_generate_fails*, the AI.GENERATE facet query raises, so the
  pipeline falls back to ML.GENERATE_TEXT.
  """

  def query(sql, job_config=None):
    if ai_generate_fails and "AI.GENERATE(" in sql:
      raise RuntimeError("AI.GENERATE is unavailable")
    job = MagicMock()
    job.result.return_value = (
        [{"session_id": "s1"}] if "LIMIT @max_sessions" in sql else []
    )
    return job

  bq = MagicMock()
  bq.query.side_effect = query
  return bq


def _facet_query(bq, function):
  """The one query sent to *bq* that calls *function*."""
  [sql] = [
      call.args[0]
      for call in bq.query.call_args_list
      if function in call.args[0]
  ]
  return sql


def _facet_sql(run, function, endpoint=None, ai_generate_fails=False, **kwargs):
  """The facet query that calls *function* in one insights run."""
  bq = _bq_client(ai_generate_fails)
  client = Client(
      project_id="p",
      dataset_id="d",
      verify_schema=False,
      bq_client=bq,
      endpoint=endpoint,
  )
  run(client, **kwargs)
  return _facet_query(bq, function)


@pytest.mark.parametrize(
    "run", list(_ENTRY_POINTS.values()), ids=list(_ENTRY_POINTS)
)
class TestAIGenerateFacets:
  """``endpoint => '...'`` stays one string literal."""

  @pytest.mark.parametrize(
      "endpoint",
      list(_AI_GENERATE_ENDPOINTS.values()),
      ids=list(_AI_GENERATE_ENDPOINTS),
  )
  def test_endpoint_stays_one_string_literal(self, run, endpoint):
    sql = _facet_sql(run, "AI.GENERATE(", endpoint=endpoint)
    value, end = _token_after(sql, "endpoint => ")
    assert value == endpoint
    assert sql[end:].startswith(_THEN_MODEL_PARAMS)

  def test_audit_payload_renders_escaped(self, run):
    sql = _facet_sql(
        run, "AI.GENERATE(", endpoint=_ENDPOINT_PAYLOADS["quote_adds_argument"]
    )
    assert (
        "\n  endpoint => 'gemini-2.5-flash\\', request_type => \\'SHARED',"
        "\n  model_params => JSON '"
    ) in sql

  @pytest.mark.parametrize(
      "endpoint",
      ["gemini-2.5-flash", "gemini-2.5-pro", "gemini-3-flash-preview"],
  )
  def test_valid_endpoint_renders_unchanged(self, run, endpoint):
    sql = _facet_sql(run, "AI.GENERATE(", endpoint=endpoint)
    assert f"\n  endpoint => '{endpoint}'{_THEN_MODEL_PARAMS}" in sql


@pytest.mark.parametrize(
    "run", list(_ENTRY_POINTS.values()), ids=list(_ENTRY_POINTS)
)
class TestLegacyFacets:
  """``MODEL `...``` stays one quoted identifier on every way in."""

  @pytest.mark.parametrize(
      "endpoint",
      list(_LEGACY_MODEL_PAYLOADS.values()),
      ids=list(_LEGACY_MODEL_PAYLOADS),
  )
  def test_legacy_endpoint_stays_one_quoted_identifier(self, run, endpoint):
    # Two dots skip AI.GENERATE, and the endpoint is the model.
    sql = _facet_sql(run, "ML.GENERATE_TEXT(", endpoint=endpoint)
    value, end = _token_after(sql, "MODEL ")
    assert value == endpoint
    assert sql[end:].startswith(_THEN_PROMPT)

  @pytest.mark.parametrize(
      "text_model",
      list(_LEGACY_MODEL_PAYLOADS.values()),
      ids=list(_LEGACY_MODEL_PAYLOADS),
  )
  def test_text_model_stays_one_quoted_identifier(self, run, text_model):
    # The default endpoint tries AI.GENERATE first; when that fails,
    # text_model is the model.
    sql = _facet_sql(
        run, "ML.GENERATE_TEXT(", ai_generate_fails=True, text_model=text_model
    )
    value, end = _token_after(sql, "MODEL ")
    assert value == text_model
    assert sql[end:].startswith(_THEN_PROMPT)

  def test_endpoint_after_ai_generate_fails_stays_one_quoted_identifier(
      self, run
  ):
    sql = _facet_sql(
        run,
        "ML.GENERATE_TEXT(",
        endpoint=_FALLBACK_ENDPOINT,
        ai_generate_fails=True,
    )
    value, end = _token_after(sql, "MODEL ")
    assert value == _FALLBACK_ENDPOINT
    assert sql[end:].startswith(_THEN_PROMPT)

  def test_audit_payload_renders_escaped(self, run):
    sql = _facet_sql(
        run,
        "ML.GENERATE_TEXT(",
        endpoint=_LEGACY_MODEL_PAYLOADS["backtick_adds_sql"],
    )
    assert (
        "\n    MODEL `proj.ds.judge_model\\`, (SELECT 'forged' AS prompt)) --`,"
        "\n    STRUCT(\n"
    ) in sql

  @pytest.mark.parametrize(
      "endpoint",
      [
          "proj.ds.my_model",
          "my-project.my_dataset.my_model",
          "example.com:my-project.my_dataset.my_model",
      ],
  )
  def test_valid_model_reference_renders_unchanged(self, run, endpoint):
    sql = _facet_sql(run, "ML.GENERATE_TEXT(", endpoint=endpoint)
    assert f"\n    MODEL `{endpoint}`{_THEN_PROMPT}" in sql


class TestRemoteFunction:
  """The ``insights`` operation keeps a request's endpoint in its token."""

  @pytest.mark.parametrize(
      "endpoint,function,marker,then",
      [
          (
              _ENDPOINT_PAYLOADS["quote_adds_argument"],
              "AI.GENERATE(",
              "endpoint => ",
              _THEN_MODEL_PARAMS,
          ),
          (
              _LEGACY_MODEL_PAYLOADS["backtick_adds_sql"],
              "ML.GENERATE_TEXT(",
              "MODEL ",
              _THEN_PROMPT,
          ),
      ],
      ids=["ai_generate", "ml_generate_text"],
  )
  def test_insights_operation_keeps_endpoint_in_its_token(
      self, monkeypatch, endpoint, function, marker, then
  ):
    bq = _bq_client()
    monkeypatch.setattr(
        client_module, "make_bq_client", lambda *args, **kwargs: bq
    )
    dispatch = _load("rf_dispatch", _REMOTE_FUNCTION_DIR / "dispatch.py")
    client = dispatch.build_client_from_context(
        {"project_id": "p", "dataset_id": "d", "endpoint": endpoint}
    )
    dispatch.dispatch(client, "insights", {})
    sql = _facet_query(bq, function)
    value, end = _token_after(sql, marker)
    assert value == endpoint
    assert sql[end:].startswith(then)
