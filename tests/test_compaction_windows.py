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

"""Behavior tests for #216, including an opt-in BigQuery dialect check."""

from datetime import datetime
from datetime import timedelta
from datetime import timezone
import json
import os
from unittest import mock

import pytest
from typer.testing import CliRunner

from bigquery_agent_analytics import views
from bigquery_agent_analytics.views import ViewManager
from tests.cross_event_view_harness import execute_cross_event_view
from tests.cross_event_view_harness import fixture_events

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_SOURCE = "`test-project.analytics.agent_events`"


def _event(start=1.234, end=1.235, **overrides):
  return {
      "event_type": "EVENT_COMPACTION",
      "attributes": {"adk": {"app_name": "app"}},
      "user_id": "user",
      "session_id": "session",
      "invocation_id": "invocation",
      "content": {"start_timestamp": start, "end_timestamp": end},
      **overrides,
  }


@pytest.fixture
def vm():
  return ViewManager(
      project_id="test-project",
      dataset_id="analytics",
      bq_client=mock.MagicMock(),
  )


@pytest.fixture
def run_view(vm):
  """Execute the generated SELECT with SQLite shims for BigQuery functions.

  Filtering, DISTINCT and projection run as SQL, not as a Python copy of the
  view. The opt-in test below checks the same contract in real BigQuery.
  """

  def run(events):
    return execute_cross_event_view(vm, "compaction_windows", events)

  return run


def test_fractional_window_survives_conversion(run_view):
  (row,) = run_view([_event()])
  assert row == {
      "app_name": "app",
      "user_id": "user",
      "session_id": "session",
      "invocation_id": "invocation",
      "start_ts": "1970-01-01T00:00:01.234000+00:00",
      "end_ts": "1970-01-01T00:00:01.235000+00:00",
      "start_seconds": 1.234,
      "end_seconds": 1.235,
  }
  assert (
      datetime.fromisoformat(row["end_ts"])
      - datetime.fromisoformat(row["start_ts"])
  ) == timedelta(milliseconds=1)


@pytest.mark.parametrize(
    "start,end", [(0, 0), (-1.235, -1.234), ("1.234", "1.235")]
)
def test_zero_negative_and_string_epoch_values(run_view, start, end):
  (row,) = run_view([_event(start, end)])
  assert (
      row["start_ts"] == (_EPOCH + timedelta(seconds=float(start))).isoformat()
  )
  assert row["end_ts"] == (_EPOCH + timedelta(seconds=float(end))).isoformat()


def test_distinct_windows_remain_separate(run_view):
  rows = run_view(
      [
          _event(),
          _event(),
          _event(1.2345, 1.236),
          _event(3, 4),
      ]
  )
  assert {(row["start_seconds"], row["end_seconds"]) for row in rows} == {
      (1.234, 1.235),
      (1.2345, 1.236),
      (3, 4),
  }
  assert len(rows) == 3


@pytest.mark.parametrize(
    "field", ["app_name", "user_id", "session_id", "invocation_id"]
)
def test_same_boundaries_do_not_merge_different_identities(run_view, field):
  changes = {field: "other"}
  if field == "app_name":
    changes = {"attributes": {"adk": {"app_name": "other"}}}
  rows = run_view([_event(), _event(**changes)])
  assert len(rows) == 2
  assert {row[field] for row in rows} == {
      {
          "app_name": "app",
          "user_id": "user",
          "session_id": "session",
          "invocation_id": "invocation",
      }[field],
      "other",
  }


@pytest.mark.parametrize(
    "field", ["app_name", "user_id", "session_id", "invocation_id"]
)
@pytest.mark.parametrize("value", [None, ""])
def test_incomplete_identities_are_excluded(run_view, field, value):
  changes = {field: value}
  if field == "app_name":
    changes = {"attributes": {"adk": {"app_name": value}}}
  assert run_view([_event(**changes)]) == []


@pytest.mark.parametrize("attributes", [None, {}, {"adk": None}, {"adk": {}}])
def test_legacy_rows_do_not_create_null_identity_windows(run_view, attributes):
  assert run_view([_event(attributes=attributes)]) == []


@pytest.mark.parametrize(
    "content", [None, {}, {"start_timestamp": 1.234}, {"end_timestamp": 1.235}]
)
def test_missing_boundaries_are_excluded(run_view, content):
  assert run_view([_event(content=content)]) == []


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "invalid",
        True,
        {},
        [],
        "NaN",
        "Infinity",
        "-Infinity",
        1e308,
        -1e308,
        253402300800,
        -62135596801,
    ],
)
@pytest.mark.parametrize("field", ["start_timestamp", "end_timestamp"])
def test_invalid_boundaries_do_not_abort_valid_windows(run_view, field, value):
  invalid = _event()
  invalid["content"][field] = value
  assert run_view([invalid, _event()]) == run_view([_event()])


def test_reversed_windows_are_excluded_before_rounding(run_view):
  assert run_view([_event(2, 1), _event(1.2340002, 1.2340001)]) == []


def test_non_compaction_events_are_excluded(run_view):
  assert run_view([_event(event_type="LLM_RESPONSE")]) == []


def test_representative_workflow_fixture(run_view):
  """The shared ADK 2.0 fixture (#218) has one fractional-second window."""
  assert run_view(fixture_events()) == [
      {
          "app_name": "team_app",
          "user_id": "user-1",
          "session_id": "sess-1",
          "invocation_id": "inv-1",
          "start_ts": "2026-06-01T10:00:01.234567+00:00",
          "end_ts": "2026-06-01T10:00:01.235891+00:00",
          "start_seconds": 1780308001.234567,
          "end_seconds": 1780308001.235891,
      }
  ]


def test_partition_limitation_is_documented(run_view):
  """The view has no event ``timestamp`` column, so it cannot prune
  partitions; its SDK.md section must say so, and point to the
  per-event view, until the view exposes one."""
  (row,) = run_view([_event()])
  assert "timestamp" not in row
  sdk_md = os.path.join(os.path.dirname(__file__), os.pardir, "SDK.md")
  with open(sdk_md, encoding="utf-8") as f:
    doc = f.read()
  # A renamed heading raises ValueError instead of widening the search.
  start = doc.index("#### Cross-event views")
  section = doc[start : doc.index("\n## ", start)]
  assert any(
      "require_partition_filter" in paragraph
      and "adk_event_compactions" in paragraph
      for paragraph in section.split("\n\n")
  )


def test_custom_source_prefix_and_cli_deployment():
  from bigquery_agent_analytics.cli import app

  client = mock.MagicMock()
  vm = ViewManager(
      project_id="proj",
      dataset_id="ds",
      table_id="custom_events",
      view_prefix="custom_",
      bq_client=client,
  )
  sql = vm.get_view_sql("compaction_windows")
  assert "`proj.ds.custom_compaction_windows`" in sql
  assert "FROM `proj.ds.custom_events`" in sql

  with mock.patch.object(views, "make_bq_client", return_value=client):
    result = CliRunner().invoke(
        app,
        [
            "views",
            "create",
            "compaction_windows",
            "--project-id=proj",
            "--dataset-id=ds",
        ],
    )
  assert result.exit_code == 0, result.output
  client.query.assert_called_once()
  issued = client.query.call_args
  assert "`proj.ds.adk_compaction_windows`" in issued.args[0]
  assert issued.kwargs["job_config"].labels["sdk_feature"] == "views"


@pytest.mark.skipif(
    os.environ.get("BQAA_LIVE_BQ") != "1"
    or not os.environ.get("BQAA_LIVE_BQ_PROJECT"),
    reason="Set BQAA_LIVE_BQ=1 and BQAA_LIVE_BQ_PROJECT for the BigQuery probe",
)
def test_compaction_windows_bigquery_contract(vm):
  """Run the generated query on inline fixtures; create no cloud resources."""
  from google.cloud import bigquery

  events = [
      _event(),
      _event(),
      _event(3, 4),
      _event(user_id="other"),
      _event(attributes={}),
      _event(content={}),
      _event(2, 1),
      _event(event_type="LLM_RESPONSE"),
  ]
  for invalid in ("NaN", "Infinity", "invalid", 1e308, 253402300800):
    events.extend([_event(start=invalid), _event(end=invalid)])
  query = vm.get_view_sql("compaction_windows").split(" AS\n", 1)[1]
  query = query.replace(_SOURCE, "fixture_events")
  fixture = """WITH fixture_events AS (
    SELECT
      JSON_VALUE(event, '$.event_type') AS event_type,
      JSON_QUERY(event, '$.attributes') AS attributes,
      JSON_VALUE(event, '$.user_id') AS user_id,
      JSON_VALUE(event, '$.session_id') AS session_id,
      JSON_VALUE(event, '$.invocation_id') AS invocation_id,
      JSON_QUERY(event, '$.content') AS content
    FROM UNNEST(JSON_QUERY_ARRAY(@events)) AS event
  ), """
  config = bigquery.QueryJobConfig(
      query_parameters=[
          bigquery.ScalarQueryParameter("events", "STRING", json.dumps(events))
      ],
      maximum_bytes_billed=10 * 1024 * 1024,
  )
  client = bigquery.Client(project=os.environ["BQAA_LIVE_BQ_PROJECT"])
  try:
    rows = list(
        client.query(
            fixture + query.removeprefix("WITH "),
            job_config=config,
            location=os.environ.get("BQAA_LIVE_BQ_LOCATION", "US"),
        ).result()
    )
  finally:
    client.close()
  assert len(rows) == 3
  assert {(r.user_id, r.start_ts, r.end_ts) for r in rows} == {
      (user, _EPOCH + timedelta(seconds=start), _EPOCH + timedelta(seconds=end))
      for user, start, end in [
          ("user", 1.234, 1.235),
          ("other", 1.234, 1.235),
          ("user", 3, 4),
      ]
  }
