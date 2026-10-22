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

"""Client refuses events tables that would leave their quoted table path.

``Client`` formats ``table_id`` into ``FROM `{project}.{dataset}.{table}```
in its queries, and ``evaluate``, ``evaluate_categorical``, ``insights``
and ``insights_async`` format their ``dataset`` argument, an events table
override, the same way. The BigQuery Remote Function takes ``table_id``
from the request's ``userDefinedContext``, else ``BQ_AGENT_TABLE``
(``_deploy_runtime.resolve_client_options``). A backtick in either value
closed the quoted path, so the rest of the value became SQL. The
constructor, every later assignment to ``client.table_id`` and those
overrides now run drift's table-name check, so a bad value raises
``ValueError`` before any query. These tables are only read, so a
wildcard read (one table name and a single trailing ``*``, such as
``agent_events_*`` over sharded tables) keeps working, and so do table
names with dashes, spaces or non-ASCII letters. Drift and deep analysis
keep #539's stricter rule, which refuses a wildcard.
"""

import asyncio
from unittest.mock import MagicMock

import pytest

from bigquery_agent_analytics import _deploy_runtime
from bigquery_agent_analytics import Client
from bigquery_agent_analytics import SystemEvaluator
from bigquery_agent_analytics import TraceFilter
from bigquery_agent_analytics.categorical_evaluator import CategoricalEvaluationConfig
from bigquery_agent_analytics.categorical_evaluator import CategoricalMetricCategory
from bigquery_agent_analytics.categorical_evaluator import CategoricalMetricDefinition
from tests.test_client_project_dataset_ids import _load
from tests.test_client_project_dataset_ids import _METHODS
from tests.test_client_project_dataset_ids import _post
from tests.test_client_project_dataset_ids import _REMOTE_FUNCTION_DIR
from tests.test_client_project_dataset_ids import handle_request
from tests.test_client_project_dataset_ids import remote_bq
from tests.test_drift_production_table import _NOT_A_TABLE_NAME as _DRIFT_PAYLOADS
from tests.test_drift_production_table import _TABLE_NAMES

# The review's payload: it closes the events table path and joins a table
# from another dataset.
_CROSS_JOIN_PAYLOAD = (
    "agent_events` CROSS JOIN `p.private.secrets` AS injected --"
)

# The CROSS JOIN payload behind a wildcard's '*'.
_WILDCARD_CROSS_JOIN_PAYLOAD = (
    "agent_events_*` CROSS JOIN `p.private.secrets` AS injected --"
)

# Values that are neither one BigQuery table name nor one wildcard read:
# drift's two payloads, one value per kind of character the check refuses,
# and each malformed wildcard.
_NOT_A_TABLE_SOURCE = {
    **_DRIFT_PAYLOADS,
    "cross_join": _CROSS_JOIN_PAYLOAD,
    "dotted": "private.secrets",
    "backslash": "agent_events\\",
    "trailing_newline": "agent_events\n",
    "tab": "agent" + chr(9) + "events",
    "single_quote": "agent'events",
    "double_quote": 'agent"events',
    "semicolon": "agent_events;",
    "hash_comment": "agent_events#",
    "block_comment": "agent_events/*",
    # GoogleSQL refuses a partition decorator in FROM ("cannot include
    # decorator"), so no read ever worked with one.
    "partition_decorator": "agent_events$20260101",
    "empty": "",
    "none": None,
    "bytes": b"agent_events",
    # A wildcard is one table name followed by a single trailing '*'.
    "bare_wildcard": "*",
    "leading_wildcard": "*_events",
    "inner_wildcard": "agent*events",
    "double_wildcard": "agent_events_**",
    "wildcard_then_backtick": "agent_events_*`",
    "wildcard_cross_join": _WILDCARD_CROSS_JOIN_PAYLOAD,
    "dotted_wildcard": "private.secrets_*",
    "escaped_wildcard": "agent_events_\\*",
    "wildcard_then_space": "agent_events_* ",
    "wildcard_then_newline": "agent_events_*\n",
}

# Write destinations, and drift's events table (#539), take one table name
# only: the values above and a wildcard.
_NOT_A_TABLE_NAME = {**_NOT_A_TABLE_SOURCE, "wildcard": "agent_events_*"}

# Wildcard reads over sharded tables, which the read-only sources accept.
_WILDCARDS = ["agent_events_*", "agent-events_v*", "ग्राहक_*"]

# An empty or None override falls back to the client's table_id, so only
# the other values can reach a method as an override.
_NOT_A_TABLE_OVERRIDE = {
    name: value for name, value in _NOT_A_TABLE_SOURCE.items() if value
}

# The remote-function cases: drift's payloads and the review's, plain and
# behind a wildcard.
_REMOTE_PAYLOADS = {
    **_DRIFT_PAYLOADS,
    "cross_join": _CROSS_JOIN_PAYLOAD,
    "wildcard_cross_join": _WILDCARD_CROSS_JOIN_PAYLOAD,
}

_TABLE_ID_ERROR = "^table_id must be one table name"
_OVERRIDE_ERROR = (
    r"^events table \(dataset or table_id\) must be one table name"
)

# The constructor's own ways to run: no query, or schema verification,
# which runs one query. (table_id="auto" replaces the value it is given.)
_CONSTRUCTOR_MODES = {
    "no_query": {"verify_schema": False},
    "verify_schema": {"verify_schema": True},
}

_CATEGORICAL_CONFIG = CategoricalEvaluationConfig(
    metrics=[
        CategoricalMetricDefinition(
            name="tone",
            definition="Overall tone of the conversation.",
            categories=[
                CategoricalMetricCategory(
                    name="positive", definition="User is satisfied."
                ),
                CategoricalMetricCategory(
                    name="negative", definition="User is frustrated."
                ),
            ],
        )
    ]
)


def _bq_client():
  """Fake BigQuery client whose every query returns no rows."""
  bq = MagicMock()
  bq.query.return_value.result.return_value = []
  return bq


def _sent_sql(bq):
  return [call.args[0] for call in bq.query.call_args_list]


def _built_client(bq):
  return Client(
      project_id="p", dataset_id="d", verify_schema=False, bq_client=bq
  )


def _evaluate(client, **kwargs):
  return client.evaluate(
      evaluator=SystemEvaluator.latency(),
      filters=TraceFilter(limit=1),
      **kwargs,
  )


def _evaluate_categorical(client, **kwargs):
  return client.evaluate_categorical(config=_CATEGORICAL_CONFIG, **kwargs)


def _insights(client, **kwargs):
  return client.insights(**kwargs)


def _insights_async(client, **kwargs):
  return asyncio.run(client.insights_async(**kwargs))


def _drift_detection(client, **kwargs):
  return client.drift_detection(golden_dataset="golden_questions", **kwargs)


def _drift_detection_async(client, **kwargs):
  return asyncio.run(
      client.drift_detection_async(golden_dataset="golden_questions", **kwargs)
  )


def _deep_analysis(client, **kwargs):
  return client.deep_analysis(**kwargs)


def _deep_analysis_async(client, **kwargs):
  return asyncio.run(client.deep_analysis_async(**kwargs))


# The methods whose events table override was formatted unchecked. Drift
# and deep analysis check theirs in feedback (#539).
_OVERRIDE_METHODS = {
    "evaluate": _evaluate,
    "evaluate_categorical": _evaluate_categorical,
    "insights": _insights,
    "insights_async": _insights_async,
}

# The ID tests' methods that read the client's table with Client's own
# check; drift and deep analysis keep #539's rule (see _STRICT_METHODS).
_READ_METHODS = {
    name: method
    for name, method in _METHODS.items()
    if name not in ("drift_detection_async", "deep_analysis_async")
}

# Drift and deep analysis keep #539's rule for their events table: one
# table name, no wildcard.
_STRICT_METHODS = {
    "drift_detection": _drift_detection,
    "drift_detection_async": _drift_detection_async,
    "deep_analysis": _deep_analysis,
    "deep_analysis_async": _deep_analysis_async,
}


class TestConstructor:
  """``Client()`` refuses a bad ``table_id`` before any query."""

  @pytest.mark.parametrize(
      "mode", list(_CONSTRUCTOR_MODES.values()), ids=list(_CONSTRUCTOR_MODES)
  )
  @pytest.mark.parametrize(
      "table_id",
      list(_NOT_A_TABLE_SOURCE.values()),
      ids=list(_NOT_A_TABLE_SOURCE),
  )
  def test_rejects_value_that_is_not_one_table_name(self, table_id, mode):
    bq = _bq_client()
    with pytest.raises(ValueError, match=_TABLE_ID_ERROR):
      Client(
          project_id="p",
          dataset_id="d",
          table_id=table_id,
          bq_client=bq,
          **mode,
      )
    assert _sent_sql(bq) == []

  @pytest.mark.parametrize("table_id", _TABLE_NAMES)
  def test_accepts_table_name(self, table_id):
    bq = _bq_client()
    client = Client(
        project_id="p",
        dataset_id="d",
        table_id=table_id,
        verify_schema=False,
        bq_client=bq,
    )
    assert client.table_id == table_id
    _evaluate(client)
    [sql] = _sent_sql(bq)
    assert f"`p.d.{table_id}`" in sql

  @pytest.mark.parametrize("table_id", _WILDCARDS)
  def test_accepts_wildcard_read(self, table_id):
    bq = _bq_client()
    client = Client(
        project_id="p",
        dataset_id="d",
        table_id=table_id,
        verify_schema=False,
        bq_client=bq,
    )
    assert client.table_id == table_id
    _evaluate(client)
    [sql] = _sent_sql(bq)
    assert f"\nFROM `p.d.{table_id}`\n" in sql


class TestAssignmentAfterConstruction:
  """Setting ``client.table_id`` later runs the same check."""

  @pytest.mark.parametrize(
      "table_id",
      list(_NOT_A_TABLE_SOURCE.values()),
      ids=list(_NOT_A_TABLE_SOURCE),
  )
  def test_rejects_value_that_is_not_one_table_name(self, table_id):
    client = _built_client(_bq_client())
    with pytest.raises(ValueError, match=_TABLE_ID_ERROR):
      client.table_id = table_id
    assert client.table_id == "agent_events"

  @pytest.mark.parametrize(
      "method", list(_METHODS.values()), ids=list(_METHODS)
  )
  def test_refused_assignment_never_reaches_a_query(self, method):
    bq = _bq_client()
    client = _built_client(bq)
    with pytest.raises(ValueError, match=_TABLE_ID_ERROR):
      client.table_id = _CROSS_JOIN_PAYLOAD
    method(client)
    sent = _sent_sql(bq)
    assert not any("CROSS JOIN" in sql for sql in sent)
    assert any("`p.d.agent_events`" in sql for sql in sent)

  @pytest.mark.parametrize("table_id", _TABLE_NAMES)
  def test_valid_assignment_reaches_the_query(self, table_id):
    bq = _bq_client()
    client = _built_client(bq)
    client.table_id = table_id
    assert client.table_id == table_id
    _evaluate(client)
    [sql] = _sent_sql(bq)
    assert f"`p.d.{table_id}`" in sql

  @pytest.mark.parametrize(
      "method", list(_READ_METHODS.values()), ids=list(_READ_METHODS)
  )
  @pytest.mark.parametrize("table_id", _WILDCARDS)
  def test_wildcard_assignment_reaches_every_read(self, table_id, method):
    bq = _bq_client()
    client = _built_client(bq)
    client.table_id = table_id
    method(client)
    sent = _sent_sql(bq)
    assert sent
    assert all(f"`p.d.{table_id}`" in sql for sql in sent)


@pytest.mark.parametrize(
    "method", list(_OVERRIDE_METHODS.values()), ids=list(_OVERRIDE_METHODS)
)
class TestEventsTableOverride:
  """A method's ``dataset`` override is checked like drift's."""

  @pytest.mark.parametrize(
      "dataset",
      list(_NOT_A_TABLE_OVERRIDE.values()),
      ids=list(_NOT_A_TABLE_OVERRIDE),
  )
  def test_rejects_override_that_is_not_one_table_name(self, method, dataset):
    bq = _bq_client()
    with pytest.raises(ValueError, match=_OVERRIDE_ERROR):
      method(_built_client(bq), dataset=dataset)
    assert _sent_sql(bq) == []

  @pytest.mark.parametrize("dataset", _TABLE_NAMES + _WILDCARDS)
  def test_table_name_override_reaches_the_query(self, method, dataset):
    bq = _bq_client()
    method(_built_client(bq), dataset=dataset)
    sent = _sent_sql(bq)
    assert sent
    assert all(f"`p.d.{dataset}`" in sql for sql in sent)


@pytest.mark.parametrize(
    "method", list(_STRICT_METHODS.values()), ids=list(_STRICT_METHODS)
)
class TestStrictEventsTable:
  """Drift and deep analysis still refuse a wildcard events table (#539)."""

  @pytest.mark.parametrize("table", _WILDCARDS)
  def test_refuses_wildcard_client_table(self, method, table):
    bq = _bq_client()
    client = _built_client(bq)
    client.table_id = table
    with pytest.raises(ValueError, match=_OVERRIDE_ERROR):
      method(client)
    assert _sent_sql(bq) == []

  @pytest.mark.parametrize("dataset", _WILDCARDS)
  def test_refuses_wildcard_override(self, method, dataset):
    bq = _bq_client()
    with pytest.raises(ValueError, match=_OVERRIDE_ERROR):
      method(_built_client(bq), dataset=dataset)
    assert _sent_sql(bq) == []


class TestRemoteFunction:
  """A request's ``table_id`` is refused before the client can query."""

  @pytest.mark.parametrize(
      "table_id",
      list(_REMOTE_PAYLOADS.values()),
      ids=list(_REMOTE_PAYLOADS),
  )
  def test_dispatch_build_client_refuses_table_id(self, remote_bq, table_id):
    dispatch = _load("rf_dispatch", _REMOTE_FUNCTION_DIR / "dispatch.py")
    with pytest.raises(ValueError, match=_TABLE_ID_ERROR):
      dispatch.build_client_from_context(
          {"project_id": "p", "dataset_id": "d", "table_id": table_id}
      )
    assert _sent_sql(remote_bq) == []

  @pytest.mark.parametrize(
      "table_id",
      list(_REMOTE_PAYLOADS.values()),
      ids=list(_REMOTE_PAYLOADS),
  )
  def test_shared_runtime_refuses_table_id(self, remote_bq, table_id):
    # The streaming-evaluation worker builds its client the same way.
    with pytest.raises(ValueError, match=_TABLE_ID_ERROR):
      _deploy_runtime.build_client_from_context(
          {"project_id": "p", "dataset_id": "d", "table_id": table_id}
      )
    assert _sent_sql(remote_bq) == []

  @pytest.mark.parametrize(
      "table_id",
      list(_REMOTE_PAYLOADS.values()),
      ids=list(_REMOTE_PAYLOADS),
  )
  def test_request_with_injected_table_id_is_a_400(
      self, handle_request, remote_bq, table_id
  ):
    payload, status = _post(
        handle_request,
        {"project_id": "p", "dataset_id": "d", "table_id": table_id},
    )
    assert status == 400, payload
    assert payload["errorMessage"].startswith(
        "table_id must be one table name"
    ), payload
    assert _sent_sql(remote_bq) == []

  def test_request_with_injected_table_env_var_is_a_400(
      self, monkeypatch, handle_request, remote_bq
  ):
    monkeypatch.setenv("BQ_AGENT_TABLE", _CROSS_JOIN_PAYLOAD)
    payload, status = _post(
        handle_request, {"project_id": "p", "dataset_id": "d"}
    )
    assert status == 400, payload
    assert payload["errorMessage"].startswith(
        "table_id must be one table name"
    ), payload
    assert _sent_sql(remote_bq) == []

  @pytest.mark.parametrize("table_id", _TABLE_NAMES)
  def test_request_with_table_name_still_runs(
      self, handle_request, remote_bq, table_id
  ):
    payload, status = _post(
        handle_request,
        {"project_id": "my-proj", "dataset_id": "my-ds", "table_id": table_id},
    )
    assert status == 200, payload
    assert len(payload["replies"]) == 1
    assert f"`my-proj.my-ds.{table_id}`" in _sent_sql(remote_bq)[0]

  @pytest.mark.parametrize("table_id", _WILDCARDS)
  def test_request_with_wildcard_table_still_runs(
      self, handle_request, remote_bq, table_id
  ):
    payload, status = _post(
        handle_request,
        {"project_id": "my-proj", "dataset_id": "my-ds", "table_id": table_id},
    )
    assert status == 200, payload
    assert len(payload["replies"]) == 1
    assert f"`my-proj.my-ds.{table_id}`" in _sent_sql(remote_bq)[0]

  def test_request_with_wildcard_table_env_var_still_runs(
      self, monkeypatch, handle_request, remote_bq
  ):
    monkeypatch.setenv("BQ_AGENT_TABLE", "agent_events_*")
    payload, status = _post(
        handle_request, {"project_id": "my-proj", "dataset_id": "my-ds"}
    )
    assert status == 200, payload
    assert "`my-proj.my-ds.agent_events_*`" in _sent_sql(remote_bq)[0]
