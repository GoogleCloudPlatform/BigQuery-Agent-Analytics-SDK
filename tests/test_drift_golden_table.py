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

"""Drift's golden table must stay one table name inside its quoted path.

``compute_drift`` formats ``golden_table`` into
``FROM `{project}.{dataset}.{golden_table}```. The BigQuery Remote
Function's ``drift`` operation passes its ``golden_dataset`` param
through unchanged, and whoever can call the routine writes those params.
A backtick in the value used to close the quoted path and append SQL
that ran as the function's service account.
"""

import asyncio
import importlib.util
import json
import pathlib
from unittest.mock import MagicMock

import pytest

from bigquery_agent_analytics import Client
from bigquery_agent_analytics.feedback import compute_drift

_DISPATCH_PATH = (
    pathlib.Path(__file__).resolve().parents[1]
    / "deploy"
    / "remote_function"
    / "dispatch.py"
)

# Values that are not one BigQuery table name. The first three close the
# backtick-quoted path and append SQL of the caller's choosing.
_NOT_A_TABLE_NAME = {
    "union_read": (
        "golden` WHERE FALSE UNION ALL SELECT salary AS question"
        " FROM `p.hr.salaries"
    ),
    # Only the backtick is outside the table-name set here.
    "backtick_only": (
        "golden` UNION ALL SELECT salary AS question FROM `salaries"
    ),
    "second_statement": (
        "golden`; DROP TABLE `p.d.agent_events`; SELECT 'x' AS question"
        " FROM `p.d.golden"
    ),
    "dotted_path": "hr.salaries",
    "backslash": "golden\\",
    "newline": "golden\nquestions",
    "empty": "",
    "not_a_string": 7,
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


def _compute_drift(bq_client, golden_table):
  return asyncio.run(
      compute_drift(
          bq_client=bq_client,
          project_id="p",
          dataset_id="d",
          table_id="agent_events",
          golden_table=golden_table,
          where_clause="1=1",
          query_params=[],
      )
  )


class TestComputeDriftGoldenTable:

  @pytest.mark.parametrize(
      "golden_table",
      list(_NOT_A_TABLE_NAME.values()),
      ids=list(_NOT_A_TABLE_NAME),
  )
  def test_rejects_value_that_is_not_one_table_name(self, golden_table):
    bq = _bq_client()
    with pytest.raises(ValueError, match="golden"):
      _compute_drift(bq, golden_table)
    assert _sent_sql(bq) == []

  @pytest.mark.parametrize(
      "golden_table",
      # BigQuery's own examples of valid table names include a space and
      # non-ASCII letters, so those must keep working.
      ["golden_questions", "golden-qs_v2", "table 01", "étudiant-01", "ग्राहक"],
  )
  def test_accepts_bigquery_table_names(self, golden_table):
    bq = _bq_client()
    _compute_drift(bq, golden_table)
    assert _sent_sql(bq)[0] == f"SELECT question\nFROM `p.d.{golden_table}`\n"


class TestRemoteFunctionDrift:
  """The deployed entry point, driven the way main.handle_request does."""

  @staticmethod
  def _dispatch():
    spec = importlib.util.spec_from_file_location("rf_dispatch", _DISPATCH_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

  def test_injected_golden_dataset_is_a_row_error_and_sends_no_sql(self):
    bq = _bq_client()
    client = Client(
        project_id="p", dataset_id="d", verify_schema=False, bq_client=bq
    )
    params = {"golden_dataset": _NOT_A_TABLE_NAME["union_read"]}

    [reply] = self._dispatch().process_calls(
        client, [["drift", json.dumps(params)]]
    )

    assert reply.get("_error", {}).get("code") == "ValueError", reply
    assert _sent_sql(bq) == []

  def test_plain_golden_dataset_still_reaches_bigquery(self):
    bq = _bq_client()
    client = Client(
        project_id="p", dataset_id="d", verify_schema=False, bq_client=bq
    )
    params = {"golden_dataset": "golden_questions"}

    [reply] = self._dispatch().process_calls(
        client, [["drift", json.dumps(params)]]
    )

    assert "_error" not in reply, reply
    assert _sent_sql(bq)[0] == "SELECT question\nFROM `p.d.golden_questions`\n"
