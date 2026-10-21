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

"""Drift's production table must stay one table name inside its quoted path.

``compute_drift`` formats ``table_id`` into
``FROM `{project}.{dataset}.{table}```. ``Client.drift_detection`` and
``drift_detection_async`` pass their ``dataset`` override, or else the
client's ``table_id``, through unchanged. A backtick in that value used to
close the quoted path, so an application that forwards a caller-chosen
events table let the caller append SQL that ran with the application's
BigQuery credentials.
"""

import asyncio
from unittest.mock import MagicMock

import pytest

from bigquery_agent_analytics import Client
from bigquery_agent_analytics.feedback import compute_drift

# Values that are not one BigQuery table name. Both close the
# backtick-quoted path and append SQL of the caller's choosing.
_NOT_A_TABLE_NAME = {
    # The security audit's payload: the line comment swallows the
    # template's closing backtick, and the UNION forges a production
    # question that drift reports as new.
    "audit_union": (
        "agent_events` WHERE FALSE UNION ALL SELECT 'audit-session',"
        " 'injected question' FROM (SELECT 'USER_MESSAGE_RECEIVED' AS"
        ' event_type, JSON \'{"text_summary":"x"}\' AS content) --'
    ),
    # Only the backtick is outside the table-name set here.
    "backtick_only": "agent_events` CROSS JOIN `salaries",
}

# BigQuery's table-name rules allow dashes, spaces and non-ASCII letters
# and marks, so these must keep working.
_TABLE_NAMES = [
    "agent_events",
    "agent-events_v2",
    "agent events 01",
    "étudiant-01",
    "ग्राहक",
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


def _production_sql(bq_client):
  """The production query: drift sends it after the golden query."""
  [_, production] = _sent_sql(bq_client)
  return production


def _compute_drift(bq_client, table_id):
  return asyncio.run(
      compute_drift(
          bq_client=bq_client,
          project_id="p",
          dataset_id="d",
          table_id=table_id,
          golden_table="golden_questions",
          where_clause="1=1",
          query_params=[],
      )
  )


class TestComputeDriftProductionTable:

  @pytest.mark.parametrize(
      "table_id",
      list(_NOT_A_TABLE_NAME.values()),
      ids=list(_NOT_A_TABLE_NAME),
  )
  def test_rejects_value_that_is_not_one_table_name(self, table_id):
    bq = _bq_client()
    with pytest.raises(ValueError, match="events table"):
      _compute_drift(bq, table_id)
    assert _sent_sql(bq) == []

  @pytest.mark.parametrize("table_id", _TABLE_NAMES)
  def test_accepts_bigquery_table_names(self, table_id):
    bq = _bq_client()
    _compute_drift(bq, table_id)
    assert f"\nFROM `p.d.{table_id}`\nWHERE " in _production_sql(bq)


@pytest.fixture(params=["sync", "async"])
def run_drift(request):
  """Runs ``drift_detection`` or ``drift_detection_async`` on a client."""

  def run(client, **kwargs):
    if request.param == "sync":
      return client.drift_detection(golden_dataset="golden_questions", **kwargs)
    return asyncio.run(
        client.drift_detection_async(
            golden_dataset="golden_questions", **kwargs
        )
    )

  return run


class TestClientDriftProductionTable:
  """The public sync and async entry points and both table sources."""

  @pytest.mark.parametrize(
      "dataset",
      list(_NOT_A_TABLE_NAME.values()),
      ids=list(_NOT_A_TABLE_NAME),
  )
  def test_rejects_dataset_override(self, run_drift, dataset):
    bq = _bq_client()
    client = Client(
        project_id="p", dataset_id="d", verify_schema=False, bq_client=bq
    )
    with pytest.raises(ValueError, match="events table"):
      run_drift(client, dataset=dataset)
    assert _sent_sql(bq) == []

  @pytest.mark.parametrize(
      "table_id",
      list(_NOT_A_TABLE_NAME.values()),
      ids=list(_NOT_A_TABLE_NAME),
  )
  def test_rejects_constructor_table_id(self, run_drift, table_id):
    bq = _bq_client()
    client = Client(
        project_id="p",
        dataset_id="d",
        table_id=table_id,
        verify_schema=False,
        bq_client=bq,
    )
    with pytest.raises(ValueError, match="events table"):
      run_drift(client)
    assert _sent_sql(bq) == []

  def test_default_table_still_reaches_bigquery(self, run_drift):
    bq = _bq_client()
    client = Client(
        project_id="p", dataset_id="d", verify_schema=False, bq_client=bq
    )
    run_drift(client)
    assert "\nFROM `p.d.agent_events`\nWHERE " in _production_sql(bq)

  @pytest.mark.parametrize(
      "dataset", ["agent-events_v2", "agent events 01", "étudiant-01"]
  )
  def test_dataset_override_table_names_still_reach_bigquery(
      self, run_drift, dataset
  ):
    bq = _bq_client()
    client = Client(
        project_id="p", dataset_id="d", verify_schema=False, bq_client=bq
    )
    run_drift(client, dataset=dataset)
    assert f"\nFROM `p.d.{dataset}`\nWHERE " in _production_sql(bq)
