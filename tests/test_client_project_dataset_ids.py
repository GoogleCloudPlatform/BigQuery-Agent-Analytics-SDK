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

"""Client refuses project and dataset IDs that would leave their quoted path.

``Client`` formats ``project_id`` and ``dataset_id`` into roughly 50 SQL
templates as ``FROM `{project}.{dataset}.{table}```, and the constructor
runs two of them itself: ``table_id="auto"`` reads
``INFORMATION_SCHEMA.TABLES`` even with ``verify_schema=False``, and
``verify_schema=True`` reads ``INFORMATION_SCHEMA.COLUMNS``. The BigQuery
Remote Function builds its client from the request's
``userDefinedContext`` (``_deploy_runtime.resolve_client_options``), so a
backtick in either value closed the quoted path and the rest of the value
became SQL. ``Client.__init__`` now checks both values first, so no sync
or async method and no remote-function operation can send them.
"""

import asyncio
import importlib.util
import json
import pathlib
import sys
import types
from unittest.mock import MagicMock

import pytest

from bigquery_agent_analytics import _deploy_runtime
from bigquery_agent_analytics import Client
from bigquery_agent_analytics import client as client_module
from bigquery_agent_analytics import SystemEvaluator
from bigquery_agent_analytics import TraceFilter

_REMOTE_FUNCTION_DIR = (
    pathlib.Path(__file__).resolve().parents[1] / "deploy" / "remote_function"
)

# Closes the INFORMATION_SCHEMA.TABLES path that table_id="auto" reads and
# lists the tables of another dataset instead.
_DATASET_PAYLOAD = (
    "d.INFORMATION_SCHEMA.TABLES` WHERE FALSE UNION ALL SELECT table_name"
    " FROM `p.private.INFORMATION_SCHEMA.TABLES` --"
)
# The same attack through the project.
_PROJECT_PAYLOAD = "p." + _DATASET_PAYLOAD

# Values that are not one dataset ID. A single '-' is accepted (see
# _DATASET_IDS), but '--' is a comment marker.
_NOT_A_DATASET_ID = {
    "union_payload": _DATASET_PAYLOAD,
    # Only the backtick is outside the dataset-ID set here.
    "backtick_only": "golden`questions",
    "dotted": "hr.salaries",
    "space": "my dataset",
    "tab": "my" + chr(9) + "dataset",
    "trailing_newline": "analytics\n",
    "single_quote": "my'dataset",
    "double_quote": 'my"dataset',
    "backslash": "analytics\\",
    "semicolon": "analytics;",
    "line_comment": "analytics--",
    "inner_line_comment": "my--dataset",
    "hash_comment": "analytics#",
    "block_comment": "analytics/*",
    "non_ascii_letter": "donn" + chr(0xE9) + "es",
    "empty": "",
    "none": None,
    "bytes": b"analytics",
}

# Values that are not one Google Cloud project ID.
_NOT_A_PROJECT_ID = {
    "union_payload": _PROJECT_PAYLOAD,
    # Only the backtick is outside the project-ID set here.
    "backtick_only": "my`project",
    "dotted": "p.d",
    "space": "my project",
    "trailing_newline": "my-project\n",
    "single_quote": "my'project",
    "double_quote": 'my"project',
    "backslash": "my-project\\",
    "semicolon": "my-project;",
    "trailing_line_comment": "my-project--",
    "hash_comment": "my-project#",
    "block_comment": "my-project/*",
    "uppercase": "My-Project",
    "underscore": "my_project",
    "leading_digit": "1project",
    "leading_dash": "-my-project",
    "empty_domain": ":my-project",
    "domain_without_project": "example.com:",
    "empty": "",
    "none": None,
}

# Project IDs the client must keep accepting, including the one-letter ID
# the rest of the suite uses. Inside the backtick-quoted path a doubled
# dash is part of the ID, not a comment.
_PROJECT_IDS = [
    "p",
    "my-project",
    "audit-project-123",
    "tokyo--rain-123",
    "example.com:my-project",
    "my-company.co.uk:analytics-1",
]

# Dataset IDs the client must keep accepting. The hyphenated ones are what
# the remote-function and evaluator tests and the design doc pass today.
_DATASET_IDS = [
    "d",
    "analytics",
    "agent_analytics_v2",
    "Analytics_2026",
    "_staging",
    "my-ds",
    "test-dataset",
]

# The constructor's own ways to reach SQL: none, table auto-detection
# (which runs even with verify_schema=False), and schema verification.
_CONSTRUCTOR_MODES = {
    "no_query": {"verify_schema": False},
    "auto_detect": {"table_id": "auto", "verify_schema": False},
    "verify_schema": {"verify_schema": True},
}


def _bq_client(rows=()):
  """Fake BigQuery client whose every query returns *rows*."""
  bq = MagicMock()
  bq.query.return_value.result.return_value = list(rows)
  return bq


def _detecting_bq_client():
  """Fake BigQuery client whose every query finds ``agent_events``."""
  return _bq_client([{"table_name": "agent_events"}])


def _sent_sql(bq):
  return [call.args[0] for call in bq.query.call_args_list]


def _load(name, path):
  spec = importlib.util.spec_from_file_location(name, path)
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  return module


class TestConstructor:
  """``Client()`` refuses a bad ID before any query it runs itself."""

  @pytest.mark.parametrize(
      "mode", list(_CONSTRUCTOR_MODES.values()), ids=list(_CONSTRUCTOR_MODES)
  )
  @pytest.mark.parametrize(
      "dataset_id",
      list(_NOT_A_DATASET_ID.values()),
      ids=list(_NOT_A_DATASET_ID),
  )
  def test_rejects_value_that_is_not_one_dataset_id(self, dataset_id, mode):
    bq = _detecting_bq_client()
    with pytest.raises(
        ValueError, match="^dataset_id must be one BigQuery dataset ID"
    ):
      Client(project_id="p", dataset_id=dataset_id, bq_client=bq, **mode)
    assert _sent_sql(bq) == []

  @pytest.mark.parametrize(
      "mode", list(_CONSTRUCTOR_MODES.values()), ids=list(_CONSTRUCTOR_MODES)
  )
  @pytest.mark.parametrize(
      "project_id",
      list(_NOT_A_PROJECT_ID.values()),
      ids=list(_NOT_A_PROJECT_ID),
  )
  def test_rejects_value_that_is_not_one_project_id(self, project_id, mode):
    bq = _detecting_bq_client()
    with pytest.raises(
        ValueError, match="^project_id must be one Google Cloud project ID"
    ):
      Client(project_id=project_id, dataset_id="d", bq_client=bq, **mode)
    assert _sent_sql(bq) == []

  @pytest.mark.parametrize("dataset_id", _DATASET_IDS)
  def test_accepts_dataset_id(self, dataset_id):
    bq = _bq_client()
    client = Client(
        project_id="my-project",
        dataset_id=dataset_id,
        verify_schema=True,
        bq_client=bq,
    )
    assert client.dataset_id == dataset_id
    [sql] = _sent_sql(bq)
    assert (
        f"\nFROM `my-project.{dataset_id}.INFORMATION_SCHEMA.COLUMNS`\n" in sql
    )

  @pytest.mark.parametrize("project_id", _PROJECT_IDS)
  def test_accepts_project_id(self, project_id):
    bq = _detecting_bq_client()
    client = Client(
        project_id=project_id,
        dataset_id="analytics",
        table_id="auto",
        verify_schema=False,
        bq_client=bq,
    )
    assert client.table_id == "agent_events"
    [sql] = _sent_sql(bq)
    assert f"\nFROM `{project_id}.analytics.INFORMATION_SCHEMA.TABLES`\n" in sql


def _evaluate(client):
  return client.evaluate(
      evaluator=SystemEvaluator.latency(), filters=TraceFilter(limit=1)
  )


def _insights(client):
  return client.insights()


def _insights_async(client):
  return asyncio.run(client.insights_async())


def _drift_detection_async(client):
  return asyncio.run(
      client.drift_detection_async(golden_dataset="golden_questions")
  )


def _deep_analysis_async(client):
  return asyncio.run(client.deep_analysis_async())


_METHODS = {
    "evaluate": _evaluate,
    "insights": _insights,
    "insights_async": _insights_async,
    "drift_detection_async": _drift_detection_async,
    "deep_analysis_async": _deep_analysis_async,
}


class TestSyncAndAsyncMethods:
  """No method reaches a query with a bad ID: the client is never built."""

  @pytest.mark.parametrize(
      "method", list(_METHODS.values()), ids=list(_METHODS)
  )
  @pytest.mark.parametrize(
      "field,ids",
      [
          ("dataset_id", {"project_id": "p", "dataset_id": _DATASET_PAYLOAD}),
          ("project_id", {"project_id": _PROJECT_PAYLOAD, "dataset_id": "d"}),
      ],
      ids=["dataset", "project"],
  )
  def test_injected_id_never_reaches_a_query(self, field, ids, method):
    bq = _bq_client()
    with pytest.raises(ValueError, match=f"^{field} must be one"):
      method(Client(verify_schema=False, bq_client=bq, **ids))
    assert _sent_sql(bq) == []


# A userDefinedContext per bad field. table_id="auto" makes the constructor
# itself query, so these also prove the check runs before auto-detection.
_INJECTED_CONTEXTS = {
    "dataset": (
        "dataset_id",
        {"project_id": "p", "dataset_id": _DATASET_PAYLOAD, "table_id": "auto"},
    ),
    "project": (
        "project_id",
        {"project_id": _PROJECT_PAYLOAD, "dataset_id": "d", "table_id": "auto"},
    ),
}


@pytest.fixture
def remote_bq(monkeypatch):
  """The BigQuery client a remote-function ``Client`` creates lazily."""
  bq = _detecting_bq_client()
  monkeypatch.setattr(
      client_module, "make_bq_client", lambda *args, **kwargs: bq
  )
  return bq


@pytest.fixture
def handle_request(monkeypatch):
  """``main.handle_request``, with flask and functions_framework stubbed."""
  flask = types.ModuleType("flask")
  flask.jsonify = lambda payload: payload
  functions_framework = types.ModuleType("functions_framework")
  functions_framework.http = lambda function: function
  monkeypatch.setitem(sys.modules, "flask", flask)
  monkeypatch.setitem(sys.modules, "functions_framework", functions_framework)
  monkeypatch.setitem(
      sys.modules,
      "dispatch",
      _load("dispatch", _REMOTE_FUNCTION_DIR / "dispatch.py"),
  )
  return _load("rf_main", _REMOTE_FUNCTION_DIR / "main.py").handle_request


def _post(handle_request, user_defined_context):
  """One ``analyze`` call, sent the way BigQuery calls the function."""
  request = MagicMock()
  request.get_json.return_value = {
      "calls": [["analyze", json.dumps({"session_id": "s1"})]],
      "userDefinedContext": user_defined_context,
  }
  response = handle_request(request)
  return response if isinstance(response, tuple) else (response, 200)


class TestRemoteFunction:
  """``userDefinedContext`` IDs are refused before the client can query."""

  @pytest.mark.parametrize(
      "field,udc",
      list(_INJECTED_CONTEXTS.values()),
      ids=list(_INJECTED_CONTEXTS),
  )
  def test_dispatch_build_client_refuses_injected_id(
      self, remote_bq, field, udc
  ):
    dispatch = _load("rf_dispatch", _REMOTE_FUNCTION_DIR / "dispatch.py")
    with pytest.raises(ValueError, match=f"^{field} must be one"):
      dispatch.build_client_from_context(udc)
    assert _sent_sql(remote_bq) == []

  @pytest.mark.parametrize(
      "field,udc",
      list(_INJECTED_CONTEXTS.values()),
      ids=list(_INJECTED_CONTEXTS),
  )
  def test_shared_runtime_refuses_injected_id(self, remote_bq, field, udc):
    # The streaming-evaluation worker builds its client the same way.
    with pytest.raises(ValueError, match=f"^{field} must be one"):
      _deploy_runtime.build_client_from_context(udc)
    assert _sent_sql(remote_bq) == []

  @pytest.mark.parametrize(
      "field,udc",
      list(_INJECTED_CONTEXTS.values()),
      ids=list(_INJECTED_CONTEXTS),
  )
  def test_request_with_injected_id_is_a_400(
      self, handle_request, remote_bq, field, udc
  ):
    payload, status = _post(handle_request, udc)
    assert status == 400, payload
    assert payload["errorMessage"].startswith(f"{field} must be one"), payload
    assert _sent_sql(remote_bq) == []

  def test_request_with_hyphenated_dataset_still_runs(
      self, handle_request, remote_bq
  ):
    payload, status = _post(
        handle_request, {"project_id": "my-proj", "dataset_id": "my-ds"}
    )
    assert status == 200, payload
    assert len(payload["replies"]) == 1
    assert "`my-proj.my-ds.agent_events`" in _sent_sql(remote_bq)[0]

  def test_shared_runtime_keeps_hyphenated_dataset(self, remote_bq):
    client = _deploy_runtime.build_client_from_context(
        {"project_id": "my-proj", "dataset_id": "my-ds"}
    )
    assert (client.project_id, client.dataset_id) == ("my-proj", "my-ds")
    assert _sent_sql(remote_bq) == []
