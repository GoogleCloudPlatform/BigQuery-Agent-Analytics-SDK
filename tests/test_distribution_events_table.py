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

"""Question distribution's events table must stay one table name in its path.

``compute_question_distribution`` formats ``table_id`` into
``FROM `{project}.{dataset}.{table}``` on every path: frequently asked,
frequently unanswered, and semantic grouping with its fallback.
``Client.deep_analysis`` and ``deep_analysis_async`` pass their ``dataset``
override, or else the client's ``table_id``, through unchanged. A backtick
in that value used to close the quoted path, so an application that
forwards a caller-chosen events table let the caller append SQL that ran
with the application's BigQuery credentials.
"""

import asyncio
from unittest.mock import MagicMock

import pytest

from bigquery_agent_analytics import Client
from bigquery_agent_analytics.feedback import AnalysisConfig
from bigquery_agent_analytics.feedback import compute_question_distribution

# Values that are not one BigQuery table name.
_NOT_A_TABLE_NAME = {
    # The review's payload: the first GROUP BY completes the template's
    # aggregate, and the UNION's relation has the columns the rest of the
    # template's WHERE clause reads, so the line comment only swallows the
    # closing backtick.
    "review_union": (
        "agent_events` WHERE FALSE GROUP BY question UNION ALL SELECT"
        " 'distribution_probe' AS question, 1 AS frequency FROM (SELECT"
        " 'USER_MESSAGE_RECEIVED' AS event_type, JSON"
        ' \'{"text_summary":"x"}\' AS content) --'
    ),
    # Only the backtick is outside the table-name set here.
    "backtick_only": "agent_events` CROSS JOIN `salaries",
    # A dot names a table in another dataset.
    "dotted": "hr.salaries",
}

# BigQuery's table-name rules allow dashes, spaces and non-ASCII letters
# and marks, so these must keep working.
_TABLE_NAMES = {
    "underscore": "agent_events",
    "dash": "agent-events_v2",
    "space": "agent events 01",
    "accented_letter": "\u00e9tudiant-01",
    "devanagari_marks": "\u0917\u094d\u0930\u093e\u0939\u0915",
}


def _bq_client():
  """Fake BigQuery client whose every query returns no rows."""
  client = MagicMock()
  job = MagicMock()
  job.result.return_value = []
  client.query.return_value = job
  return client


def _sent_sql(bq_client):
  return [call.args[0] for call in bq_client.query.call_args_list]


def _events_from(table):
  """The frequently-asked query's FROM line for *table* in ``p.d``."""
  return f"\nFROM `p.d.{table}`\nWHERE "


def _distribution(
    bq_client, table_id, mode="frequently_asked", text_model=None
):
  return asyncio.run(
      compute_question_distribution(
          bq_client=bq_client,
          project_id="p",
          dataset_id="d",
          table_id=table_id,
          where_clause="1=1",
          query_params=[],
          config=AnalysisConfig(mode=mode),
          text_model=text_model,
      )
  )


class TestComputeQuestionDistributionTable:

  @pytest.mark.parametrize(
      "table_id",
      list(_NOT_A_TABLE_NAME.values()),
      ids=list(_NOT_A_TABLE_NAME),
  )
  def test_rejects_value_that_is_not_one_table_name(self, table_id):
    bq = _bq_client()
    with pytest.raises(ValueError, match="events table"):
      _distribution(bq, table_id)
    assert _sent_sql(bq) == []

  @pytest.mark.parametrize(
      ("mode", "text_model"),
      [
          ("frequently_asked", None),
          ("frequently_unanswered", None),
          # An endpoint name selects AI.GENERATE; a dotted model
          # reference selects ML.GENERATE_TEXT.
          ("auto_group_using_semantics", "text-endpoint"),
          ("auto_group_using_semantics", "p.d.text_model"),
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
  def test_every_query_path_checks_before_any_sql(self, mode, text_model):
    bq = _bq_client()
    with pytest.raises(ValueError, match="events table"):
      _distribution(
          bq,
          _NOT_A_TABLE_NAME["review_union"],
          mode=mode,
          text_model=text_model,
      )
    assert _sent_sql(bq) == []

  @pytest.mark.parametrize(
      "table_id", list(_TABLE_NAMES.values()), ids=list(_TABLE_NAMES)
  )
  def test_accepts_bigquery_table_names(self, table_id):
    bq = _bq_client()
    _distribution(bq, table_id)
    assert _events_from(table_id) in _sent_sql(bq)[-1]


@pytest.fixture(params=["sync", "async"])
def run_deep_analysis(request):
  """Runs ``deep_analysis`` or ``deep_analysis_async`` on a client."""
  config = AnalysisConfig(mode="frequently_asked")

  def run(client, **kwargs):
    if request.param == "sync":
      return client.deep_analysis(configuration=config, **kwargs)
    return asyncio.run(
        client.deep_analysis_async(configuration=config, **kwargs)
    )

  return run


class TestClientDistributionTable:
  """The public sync and async entry points and both table sources."""

  @pytest.mark.parametrize(
      "dataset",
      list(_NOT_A_TABLE_NAME.values()),
      ids=list(_NOT_A_TABLE_NAME),
  )
  def test_rejects_dataset_override(self, run_deep_analysis, dataset):
    bq = _bq_client()
    client = Client(
        project_id="p", dataset_id="d", verify_schema=False, bq_client=bq
    )
    with pytest.raises(ValueError, match="events table"):
      run_deep_analysis(client, dataset=dataset)
    assert _sent_sql(bq) == []

  @pytest.mark.parametrize(
      "table_id",
      list(_NOT_A_TABLE_NAME.values()),
      ids=list(_NOT_A_TABLE_NAME),
  )
  def test_rejects_constructor_table_id(self, run_deep_analysis, table_id):
    bq = _bq_client()
    client = Client(
        project_id="p",
        dataset_id="d",
        table_id=table_id,
        verify_schema=False,
        bq_client=bq,
    )
    with pytest.raises(ValueError, match="events table"):
      run_deep_analysis(client)
    assert _sent_sql(bq) == []

  @pytest.mark.parametrize(
      "dataset", list(_TABLE_NAMES.values()), ids=list(_TABLE_NAMES)
  )
  def test_dataset_override_table_names_still_reach_bigquery(
      self, run_deep_analysis, dataset
  ):
    bq = _bq_client()
    client = Client(
        project_id="p", dataset_id="d", verify_schema=False, bq_client=bq
    )
    run_deep_analysis(client, dataset=dataset)
    assert _events_from(dataset) in _sent_sql(bq)[-1]

  @pytest.mark.parametrize(
      "table_id", list(_TABLE_NAMES.values()), ids=list(_TABLE_NAMES)
  )
  def test_constructor_table_names_still_reach_bigquery(
      self, run_deep_analysis, table_id
  ):
    bq = _bq_client()
    client = Client(
        project_id="p",
        dataset_id="d",
        table_id=table_id,
        verify_schema=False,
        bq_client=bq,
    )
    run_deep_analysis(client)
    assert _events_from(table_id) in _sent_sql(bq)[-1]
