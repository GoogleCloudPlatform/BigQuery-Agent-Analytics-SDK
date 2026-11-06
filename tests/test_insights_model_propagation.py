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

"""Insights calls the model that the client or the run names (issue #577).

``Client.insights()`` and ``insights_async()`` call the Gemini API in
three steps: the facet-extraction fallback, which runs when AI.GENERATE
and ML.GENERATE_TEXT both fail; one call per analysis prompt; and the
executive summary. All three used to call ``gemini-2.5-flash`` whatever
``Client(endpoint=...)`` or ``insights(text_model=...)`` named, so they
failed with 404 NOT_FOUND in a location that does not serve that model.

The run's ``text_model`` wins over the client's ``endpoint`` in every
step, AI.GENERATE included. A full Vertex AI endpoint URL gives the
Gemini API the model name at the end of its path; its query and fragment
name nothing. A BigQuery ML model reference (``project.dataset.model``)
names no Gemini API model, so the Gemini API steps skip it, and
``DEFAULT_ENDPOINT`` is used only when neither value names one. A
``text_model`` that is neither a string nor None, or a client endpoint
that is not a string, raises TypeError before any query.
"""

import asyncio
import inspect
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from bigquery_agent_analytics import Client
from bigquery_agent_analytics import client as client_module
from bigquery_agent_analytics.evaluators import DEFAULT_ENDPOINT
from bigquery_agent_analytics.insights import _EXECUTIVE_SUMMARY_PROMPT
from bigquery_agent_analytics.insights import ANALYSIS_PROMPTS
from bigquery_agent_analytics.insights import build_facet_prompt
from bigquery_agent_analytics.insights import extract_facets_via_api
from bigquery_agent_analytics.insights import generate_executive_summary
from bigquery_agent_analytics.insights import run_analysis_prompt
from tests.test_client_project_dataset_ids import _load
from tests.test_client_project_dataset_ids import _REMOTE_FUNCTION_DIR

# The issue's endpoint, and a second model for text_model to override it.
_ENDPOINT = "gemini-3.8-flash"
_TEXT_MODEL = "gemini-3.8-pro"
# A BigQuery ML remote model: two dots, so not a Gemini API model.
_BQML_MODEL = "proj.ds.text_model"
# The full-URL form of _ENDPOINT that AI.GENERATE also takes.
_ENDPOINT_URL = (
    "https://aiplatform.googleapis.com/v1/projects/proj/locations/global/"
    "publishers/google/models/gemini-3.8-flash"
)
# The full-URL form of _TEXT_MODEL.
_TEXT_MODEL_URL = (
    "https://aiplatform.googleapis.com/v1/projects/proj/locations/global/"
    "publishers/google/models/gemini-3.8-pro"
)


def _insights(client, **kwargs):
  return client.insights(**kwargs)


def _insights_async(client, **kwargs):
  return asyncio.run(client.insights_async(**kwargs))


_ENTRY_POINTS = {"insights": _insights, "insights_async": _insights_async}


def _bq_client(ai_generate_fails=True, ml_generate_text_fails=True):
  """Fake BigQuery client for one insights run over one session.

  The session-metadata query finds session ``s1`` and the transcript
  query returns its transcript. By default the AI.GENERATE and
  ML.GENERATE_TEXT facet queries raise, so facet extraction falls back
  to the Gemini API, which is the path in the issue's log.
  """

  def query(sql, job_config=None):
    if "AI.GENERATE(" in sql and ai_generate_fails:
      raise RuntimeError("AI.GENERATE is unavailable")
    if "ML.GENERATE_TEXT(" in sql and ml_generate_text_fails:
      raise RuntimeError("ML.GENERATE_TEXT is unavailable")
    if "LIMIT @max_sessions" in sql:
      rows = [{"session_id": "s1"}]
    elif "AS final_response" in sql:
      rows = [{"session_id": "s1", "transcript": "USER_MESSAGE_RECEIVED: hi"}]
    elif "AI.GENERATE(" in sql:
      rows = [{"session_id": "s1"}]
    else:
      rows = []
    job = MagicMock()
    job.result.return_value = rows
    return job

  bq = MagicMock()
  bq.query.side_effect = query
  return bq


def _genai_client():
  """Fake Gemini API client; every call answers ``{}``."""
  response = MagicMock()
  response.text = "{}"
  genai_client = MagicMock()
  genai_client.aio.models.generate_content = AsyncMock(return_value=response)
  return genai_client


def _static_start(template):
  """The text of a prompt template before its first placeholder."""
  return template.split("{", 1)[0]


def _models_by_step(genai_client):
  """The model of every Gemini API call, grouped by insights step."""
  facet_start = build_facet_prompt()
  summary_start = _static_start(_EXECUTIVE_SUMMARY_PROMPT)
  analysis_starts = [
      _static_start(p["prompt"]) for p in ANALYSIS_PROMPTS.values()
  ]
  steps = {"facets": [], "analysis": [], "summary": [], "other": []}
  for call in genai_client.aio.models.generate_content.call_args_list:
    contents = call.kwargs["contents"]
    if contents.startswith(facet_start):
      step = "facets"
    elif contents.startswith(summary_start):
      step = "summary"
    elif any(contents.startswith(start) for start in analysis_starts):
      step = "analysis"
    else:
      step = "other"
    steps[step].append(call.kwargs["model"])
  return steps


def _every_step(model, facets=True):
  """Gemini API models by step when every call uses *model*."""
  return {
      "facets": [model] if facets else [],
      "analysis": [model] * len(ANALYSIS_PROMPTS),
      "summary": [model],
      "other": [],
  }


def _run(run, endpoint=None, bq=None, **kwargs):
  """Runs insights once; returns the Gemini API models by step."""
  client = Client(
      project_id="p",
      dataset_id="d",
      verify_schema=False,
      bq_client=bq or _bq_client(),
      endpoint=endpoint,
  )
  genai_client = _genai_client()
  with patch("google.genai.Client", return_value=genai_client):
    run(client, **kwargs)
  return _models_by_step(genai_client)


def _facet_queries(bq):
  """The facet queries sent to *bq*, in order, as (function, SQL)."""
  functions = []
  for call in bq.query.call_args_list:
    sql = call.args[0]
    for function in ("AI.GENERATE(", "ML.GENERATE_TEXT("):
      if function in sql:
        functions.append((function, sql))
  return functions


@pytest.mark.parametrize(
    "run", list(_ENTRY_POINTS.values()), ids=list(_ENTRY_POINTS)
)
class TestGeminiApiSteps:
  """Facet fallback, analysis prompts and summary call the run's model."""

  def test_default_client_calls_default_model(self, run):
    assert _run(run) == _every_step(DEFAULT_ENDPOINT)

  def test_client_endpoint_reaches_every_step(self, run):
    assert _run(run, endpoint=_ENDPOINT) == _every_step(_ENDPOINT)

  def test_text_model_reaches_every_step(self, run):
    assert _run(run, text_model=_TEXT_MODEL) == _every_step(_TEXT_MODEL)

  def test_text_model_overrides_client_endpoint(self, run):
    steps = _run(run, endpoint=_ENDPOINT, text_model=_TEXT_MODEL)
    assert steps == _every_step(_TEXT_MODEL)

  def test_endpoint_url_gives_its_model_name(self, run):
    assert _run(run, endpoint=_ENDPOINT_URL) == _every_step(_ENDPOINT)

  @pytest.mark.parametrize(
      "text_model,expected",
      [
          # Method, query, fragment and trailing-slash suffixes end the
          # model name.
          (_TEXT_MODEL_URL + ":generateContent", _TEXT_MODEL),
          (_TEXT_MODEL_URL + "?alt=sse", _TEXT_MODEL),
          (_TEXT_MODEL_URL + "#section", _TEXT_MODEL),
          (_TEXT_MODEL_URL + "/", _TEXT_MODEL),
          # A "/models/" in the query or the fragment does not replace
          # the model that the path names.
          (_TEXT_MODEL_URL + "?next=/models/query-model", _TEXT_MODEL),
          (_TEXT_MODEL_URL + "#/models/fragment-model", _TEXT_MODEL),
          # With no model in its path, the URL names nothing, so the
          # client's endpoint wins.
          (
              "https://example.test/v1/predict?next=/models/query-model",
              _ENDPOINT,
          ),
          ("https://example.test/v1/predict#/models/fragment-model", _ENDPOINT),
      ],
      ids=[
          "method_suffix",
          "query_suffix",
          "fragment_suffix",
          "trailing_slash",
          "models_in_query",
          "models_in_fragment",
          "models_only_in_query",
          "models_only_in_fragment",
      ],
  )
  def test_text_model_url_names_the_model_in_its_path(
      self, run, text_model, expected
  ):
    steps = _run(run, endpoint=_ENDPOINT, text_model=text_model)
    assert steps == _every_step(expected)

  def test_bqml_text_model_leaves_client_endpoint(self, run):
    steps = _run(run, endpoint=_ENDPOINT, text_model=_BQML_MODEL)
    assert steps == _every_step(_ENDPOINT)

  def test_bqml_endpoint_leaves_default_model(self, run):
    assert _run(run, endpoint=_BQML_MODEL) == _every_step(DEFAULT_ENDPOINT)

  def test_analysis_and_summary_after_ai_generate_facets(self, run):
    steps = _run(
        run, endpoint=_ENDPOINT, bq=_bq_client(ai_generate_fails=False)
    )
    assert steps == _every_step(_ENDPOINT, facets=False)


@pytest.mark.parametrize(
    "run", list(_ENTRY_POINTS.values()), ids=list(_ENTRY_POINTS)
)
class TestBigQueryFacetModel:
  """AI.GENERATE and ML.GENERATE_TEXT name the run's model too."""

  def test_ai_generate_names_client_endpoint(self, run):
    bq = _bq_client(ai_generate_fails=False)
    _run(run, endpoint=_ENDPOINT, bq=bq)
    [(function, sql)] = _facet_queries(bq)
    assert function == "AI.GENERATE("
    assert f"\n  endpoint => '{_ENDPOINT}',\n" in sql

  def test_ai_generate_names_text_model_over_endpoint(self, run):
    bq = _bq_client(ai_generate_fails=False)
    _run(run, endpoint=_ENDPOINT, text_model=_TEXT_MODEL, bq=bq)
    [(function, sql)] = _facet_queries(bq)
    assert function == "AI.GENERATE("
    assert f"\n  endpoint => '{_TEXT_MODEL}',\n" in sql

  def test_bqml_text_model_goes_to_ml_generate_text(self, run):
    # AI.GENERATE cannot call a BigQuery ML model, so it is not tried.
    bq = _bq_client(ai_generate_fails=False, ml_generate_text_fails=False)
    _run(run, endpoint=_ENDPOINT, text_model=_BQML_MODEL, bq=bq)
    [(function, sql)] = _facet_queries(bq)
    assert function == "ML.GENERATE_TEXT("
    assert f"MODEL `{_BQML_MODEL}`" in sql


def _run_refused(run, endpoint=None, **kwargs):
  """Runs insights expecting TypeError; returns (error, BigQuery, API)."""
  bq = _bq_client()
  client = Client(
      project_id="p",
      dataset_id="d",
      verify_schema=False,
      bq_client=bq,
      endpoint=endpoint,
  )
  genai_client = _genai_client()
  with patch("google.genai.Client", return_value=genai_client):
    with pytest.raises(TypeError) as error:
      run(client, **kwargs)
  return error.value, bq, genai_client


@pytest.mark.parametrize(
    "run", list(_ENTRY_POINTS.values()), ids=list(_ENTRY_POINTS)
)
class TestNonStringModels:
  """A model that is not a string fails before any query or API call."""

  @pytest.mark.parametrize(
      "text_model",
      [123, True, b"gemini-3.8-pro", ["gemini-3.8-pro"], ("x",), {}, 0, []],
      ids=["int", "bool", "bytes", "list", "tuple", "dict", "zero", "empty"],
  )
  def test_text_model_that_is_not_a_string_raises(self, run, text_model):
    error, bq, genai_client = _run_refused(
        run, endpoint=_ENDPOINT, text_model=text_model
    )
    assert "text_model must be a string or None" in str(error)
    assert bq.query.call_count == 0
    assert genai_client.aio.models.generate_content.call_count == 0

  @pytest.mark.parametrize(
      "text_model", [None, _TEXT_MODEL], ids=["no_text_model", "text_model"]
  )
  @pytest.mark.parametrize(
      "endpoint", [123, ["gemini-3.8-flash"]], ids=["int", "list"]
  )
  def test_endpoint_that_is_not_a_string_raises(
      self, run, endpoint, text_model
  ):
    error, bq, genai_client = _run_refused(
        run, endpoint=endpoint, text_model=text_model
    )
    assert "endpoint must be a string" in str(error)
    assert bq.query.call_count == 0
    assert genai_client.aio.models.generate_content.call_count == 0


class TestRemoteFunction:
  """The deployed ``insights`` operation calls the request's endpoint."""

  def test_insights_operation_calls_context_endpoint(self, monkeypatch):
    bq = _bq_client()
    monkeypatch.setattr(
        client_module, "make_bq_client", lambda *args, **kwargs: bq
    )
    dispatch = _load("rf_dispatch", _REMOTE_FUNCTION_DIR / "dispatch.py")
    client = dispatch.build_client_from_context(
        {"project_id": "p", "dataset_id": "d", "endpoint": _ENDPOINT}
    )
    genai_client = _genai_client()
    with patch("google.genai.Client", return_value=genai_client):
      dispatch.dispatch(client, "insights", {})
    assert _models_by_step(genai_client) == _every_step(_ENDPOINT)


class TestHelperDefaults:
  """Called without a model, the helpers use the client's default."""

  @pytest.mark.parametrize(
      "helper",
      [extract_facets_via_api, run_analysis_prompt, generate_executive_summary],
      ids=lambda helper: helper.__name__,
  )
  def test_default_model_is_client_default(self, helper):
    default = inspect.signature(helper).parameters["model"].default
    assert default == DEFAULT_ENDPOINT
