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

"""Feedback queries must keep the project and dataset inside their quoted path.

``compute_drift`` and ``compute_question_distribution`` format the
project and dataset into ``FROM `{project}.{dataset}.{table}```.
``Client`` passes its ``project_id`` and ``dataset_id`` through unchanged,
and the BigQuery Remote Function builds that client from the request's
``userDefinedContext`` (``_deploy_runtime.resolve_client_options``). A
backtick in either value used to close the quoted path, so the rest of the
value became SQL that ran with the client's BigQuery credentials.
"""

import asyncio
import functools
import importlib.util
import json
import pathlib
from unittest.mock import MagicMock

import pytest

from bigquery_agent_analytics import Client
from bigquery_agent_analytics.feedback import AnalysisConfig
from bigquery_agent_analytics.feedback import compute_drift
from bigquery_agent_analytics.feedback import compute_question_distribution

_DISPATCH_PATH = (
    pathlib.Path(__file__).resolve().parents[1]
    / "deploy"
    / "remote_function"
    / "dispatch.py"
)

# The review's payload. As a dataset it closes the quoted path after a real
# table, forges a golden question with a UNION, and comments out the rest of
# the template.
_DATASET_PAYLOAD = (
    "d.golden_questions` WHERE FALSE UNION ALL SELECT 'review_probe' AS"
    " question --"
)
# The same attack through the project.
_PROJECT_PAYLOAD = "p." + _DATASET_PAYLOAD

# Values that are not one BigQuery dataset ID.
_NOT_A_DATASET_ID = {
    "review_payload": _DATASET_PAYLOAD,
    # Only the backtick is outside the dataset-ID set here.
    "backtick_only": "golden`questions",
    "dotted": "hr.salaries",
    "space": "my dataset",
    "trailing_newline": "analytics\n",
    "single_quote": "my'dataset",
    "double_quote": 'my"dataset',
    "semicolon": "analytics;",
    "line_comment": "analytics--",
    "hash_comment": "analytics#",
    "block_comment": "analytics/*",
    "backslash": "analytics\\",
    # BigQuery dataset IDs never contain a dash or a non-ASCII letter.
    "dash": "my-dataset",
    "non_ascii_letter": "donn\u00e9es",
    "empty": "",
    "not_a_string": None,
}

# Values that are not one Google Cloud project ID.
_NOT_A_PROJECT_ID = {
    "review_payload": _PROJECT_PAYLOAD,
    # Only the backtick is outside the project-ID set here.
    "backtick_only": "my`project",
    "dotted": "p.d",
    "space": "my project",
    "trailing_newline": "my-project\n",
    "single_quote": "my'project",
    "double_quote": 'my"project',
    "semicolon": "my-project;",
    "trailing_line_comment": "my-project--",
    "hash_comment": "my-project#",
    "block_comment": "my-project/*",
    "backslash": "my-project\\",
    # BigQuery refuses these project IDs itself.
    "uppercase": "My-Project",
    "underscore": "my_project",
    "leading_dash": "-my-project",
    "empty_domain": ":my-project",
    "domain_without_project": "example.com:",
    "empty": "",
    "not_a_string": None,
}

# Project IDs BigQuery accepts, including the one-letter ID the rest of the
# suite uses. Inside the backtick-quoted path a doubled dash is part of the
# ID, not a comment.
_PROJECT_IDS = [
    "p",
    "my-project",
    "audit-project-123",
    "tokyo--rain-123",
    "example.com:my-project",
    "my-company.co.uk:analytics-1",
]

_DATASET_IDS = [
    "d",
    "analytics",
    "agent_analytics_v2",
    "Analytics_2026",
    # BigQuery hides datasets whose names begin with an underscore.
    "_staging",
]


def _bq_client():
  """Fake BigQuery client whose every query returns no rows."""
  client = MagicMock()
  job = MagicMock()
  job.result.return_value = []
  client.query.return_value = job
  return client


def _sent_sql(bq_client):
  return [call.args[0] for call in bq_client.query.call_args_list]


def _events_from(project_id, dataset_id):
  """The events query's FROM line for *project_id* and *dataset_id*."""
  return f"\nFROM `{project_id}.{dataset_id}.agent_events`\nWHERE "


def _drift(bq_client, project_id, dataset_id, embedding_model=None):
  return asyncio.run(
      compute_drift(
          bq_client=bq_client,
          project_id=project_id,
          dataset_id=dataset_id,
          table_id="agent_events",
          golden_table="golden_questions",
          where_clause="1=1",
          query_params=[],
          embedding_model=embedding_model,
      )
  )


def _distribution(
    bq_client,
    project_id,
    dataset_id,
    mode="frequently_asked",
    text_model=None,
):
  return asyncio.run(
      compute_question_distribution(
          bq_client=bq_client,
          project_id=project_id,
          dataset_id=dataset_id,
          table_id="agent_events",
          where_clause="1=1",
          query_params=[],
          config=AnalysisConfig(mode=mode),
          text_model=text_model,
      )
  )


@pytest.fixture(params=[_drift, _distribution], ids=["drift", "distribution"])
def build_queries(request):
  """``compute_drift`` or ``compute_question_distribution``."""
  return request.param


class TestQueryBuilders:
  """Both feedback query builders check the path before any SQL is sent."""

  @pytest.mark.parametrize(
      "dataset_id",
      list(_NOT_A_DATASET_ID.values()),
      ids=list(_NOT_A_DATASET_ID),
  )
  def test_rejects_value_that_is_not_one_dataset_id(
      self, build_queries, dataset_id
  ):
    bq = _bq_client()
    with pytest.raises(
        ValueError, match="dataset_id must be one BigQuery dataset ID"
    ):
      build_queries(bq, "p", dataset_id)
    assert _sent_sql(bq) == []

  @pytest.mark.parametrize(
      "project_id",
      list(_NOT_A_PROJECT_ID.values()),
      ids=list(_NOT_A_PROJECT_ID),
  )
  def test_rejects_value_that_is_not_one_project_id(
      self, build_queries, project_id
  ):
    bq = _bq_client()
    with pytest.raises(
        ValueError, match="project_id must be one Google Cloud project ID"
    ):
      build_queries(bq, project_id, "d")
    assert _sent_sql(bq) == []

  @pytest.mark.parametrize("project_id", _PROJECT_IDS)
  def test_accepts_project_ids(self, build_queries, project_id):
    bq = _bq_client()
    build_queries(bq, project_id, "analytics")
    assert _events_from(project_id, "analytics") in _sent_sql(bq)[-1]

  @pytest.mark.parametrize("dataset_id", _DATASET_IDS)
  def test_accepts_dataset_ids(self, build_queries, dataset_id):
    bq = _bq_client()
    build_queries(bq, "my-project", dataset_id)
    assert _events_from("my-project", dataset_id) in _sent_sql(bq)[-1]

  def test_golden_query_keeps_a_domain_scoped_project(self):
    bq = _bq_client()
    _drift(bq, "example.com:my-project", "analytics")
    assert _sent_sql(bq)[0] == (
        "SELECT question\n"
        "FROM `example.com:my-project.analytics.golden_questions`\n"
    )


class TestEveryQueryPath:
  """Each drift and distribution path refuses the payload before any SQL."""

  @pytest.mark.parametrize(
      "embedding_model",
      [None, "text-embedding-005", "p.d.embedding_model"],
      ids=["keyword_overlap", "ai_embed", "ml_generate_embedding"],
  )
  def test_drift(self, embedding_model):
    bq = _bq_client()
    with pytest.raises(ValueError, match="dataset_id"):
      _drift(bq, "p", _DATASET_PAYLOAD, embedding_model=embedding_model)
    assert _sent_sql(bq) == []

  @pytest.mark.parametrize(
      ("mode", "text_model"),
      [
          ("frequently_asked", None),
          ("frequently_unanswered", None),
          ("auto_group_using_semantics", "gemini-2.5-flash"),
          ("auto_group_using_semantics", "p.d.gemini_text_model"),
          ("auto_group_using_semantics", None),
      ],
      ids=[
          "frequently_asked",
          "frequently_unanswered",
          "ai_generate",
          "ml_generate_text",
          "frequently_asked_fallback",
      ],
  )
  def test_distribution(self, mode, text_model):
    bq = _bq_client()
    with pytest.raises(ValueError, match="dataset_id"):
      _distribution(bq, "p", _DATASET_PAYLOAD, mode=mode, text_model=text_model)
    assert _sent_sql(bq) == []


_CLIENT_CALLS = {
    "drift_detection": {"golden_dataset": "golden_questions"},
    "deep_analysis": {"configuration": AnalysisConfig(mode="frequently_asked")},
}


@pytest.fixture(params=["sync", "async"])
def call_client(request):
  """Runs a ``Client`` feedback method, or its ``_async`` twin."""

  def call(client, method):
    kwargs = _CLIENT_CALLS[method]
    if request.param == "sync":
      return getattr(client, method)(**kwargs)
    return asyncio.run(getattr(client, f"{method}_async")(**kwargs))

  return call


class TestClient:
  """The public sync and async entry points, with the constructor's values."""

  @pytest.mark.parametrize("method", list(_CLIENT_CALLS))
  @pytest.mark.parametrize(
      ("project_id", "dataset_id", "name"),
      [
          ("p", _DATASET_PAYLOAD, "dataset_id"),
          (_PROJECT_PAYLOAD, "d", "project_id"),
      ],
      ids=["dataset_id", "project_id"],
  )
  def test_rejects_injected_value(
      self, call_client, method, project_id, dataset_id, name
  ):
    bq = _bq_client()
    client = Client(
        project_id=project_id,
        dataset_id=dataset_id,
        verify_schema=False,
        bq_client=bq,
    )
    with pytest.raises(ValueError, match=f"{name} must be one"):
      call_client(client, method)
    assert _sent_sql(bq) == []

  @pytest.mark.parametrize("method", list(_CLIENT_CALLS))
  def test_hyphenated_project_still_reaches_bigquery(self, call_client, method):
    bq = _bq_client()
    client = Client(
        project_id="my-project",
        dataset_id="analytics",
        verify_schema=False,
        bq_client=bq,
    )
    call_client(client, method)
    assert _events_from("my-project", "analytics") in _sent_sql(bq)[-1]


class TestRemoteFunctionContext:
  """``userDefinedContext`` through ``resolve_client_options`` to drift.

  The client is built and the calls processed the way
  ``main.handle_request`` does it.
  """

  @staticmethod
  def _drift_reply(monkeypatch, bq, user_defined_context):
    spec = importlib.util.spec_from_file_location("rf_dispatch", _DISPATCH_PATH)
    dispatch = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dispatch)
    # The client built from the context sends its queries to the fake.
    monkeypatch.setattr(
        dispatch, "Client", functools.partial(Client, bq_client=bq)
    )
    monkeypatch.delenv("BQ_AGENT_TABLE", raising=False)
    client = dispatch.build_client_from_context(user_defined_context)
    [reply] = dispatch.process_calls(
        client, [["drift", json.dumps({"golden_dataset": "golden_questions"})]]
    )
    return reply

  @pytest.mark.parametrize(
      ("user_defined_context", "name"),
      [
          ({"project_id": "p", "dataset_id": _DATASET_PAYLOAD}, "dataset_id"),
          ({"project_id": _PROJECT_PAYLOAD, "dataset_id": "d"}, "project_id"),
      ],
      ids=["dataset_id", "project_id"],
  )
  def test_injected_context_is_a_row_error_and_sends_no_sql(
      self, monkeypatch, user_defined_context, name
  ):
    bq = _bq_client()
    reply = self._drift_reply(monkeypatch, bq, user_defined_context)
    assert reply.get("_error", {}).get("code") == "ValueError", reply
    assert reply["_error"]["message"].startswith(f"{name} must be one"), reply
    assert _sent_sql(bq) == []

  def test_hyphenated_project_context_still_reaches_bigquery(self, monkeypatch):
    bq = _bq_client()
    reply = self._drift_reply(
        monkeypatch, bq, {"project_id": "my-project", "dataset_id": "analytics"}
    )
    assert "_error" not in reply, reply
    assert _sent_sql(bq)[0] == (
        "SELECT question\nFROM `my-project.analytics.golden_questions`\n"
    )
