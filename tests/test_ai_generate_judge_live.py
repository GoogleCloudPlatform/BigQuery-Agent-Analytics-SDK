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

"""Live BigQuery integration tests for the LLM-judge AI.GENERATE path.

These tests submit the exact SQL produced by
``render_ai_generate_judge_query`` to a real BigQuery project so the
generated query is verified against current AI.GENERATE semantics —
not just against unit-test mocks. They exist because the SDK's
prior AI.GENERATE template used the table-valued
``FROM ..., AI.GENERATE(...)`` shape, which silently passed every
mocked test in the suite while the real AI.GENERATE function only
exposes the scalar form. Mocks alone won't catch that class of bug.

Skipped by default. To run them locally or in a release pipeline,
set:

    BQAA_RUN_LIVE_TESTS=1
    PROJECT_ID=...                    # GCP project with BQ + AI access
    DATASET_ID=...                    # dataset containing agent_events
    BQAA_JUDGE_ENDPOINT=...           # optional, defaults to gemini-2.5-flash
    BQAA_AI_GENERATE_CONNECTION_ID=...   # optional, e.g. 'us.bqaa_ai_gen'

Both ``connection_id`` paths are exercised when the connection env
var is supplied; otherwise only the end-user-credentials shape runs.
"""

from __future__ import annotations

import os

import pytest

from bigquery_agent_analytics.evaluators import render_ai_generate_judge_query
from bigquery_agent_analytics.evaluators import split_judge_prompt_template

_LIVE = os.environ.get("BQAA_RUN_LIVE_TESTS") == "1"

pytestmark = pytest.mark.skipif(
    not _LIVE,
    reason=(
        "Live BigQuery tests skipped. Set BQAA_RUN_LIVE_TESTS=1 plus"
        " PROJECT_ID + DATASET_ID to opt in."
    ),
)


@pytest.fixture(scope="module")
def live_config():
  """Resolves environment-supplied live-test configuration."""
  project = os.environ.get("PROJECT_ID")
  dataset = os.environ.get("DATASET_ID")
  if not project or not dataset:
    pytest.skip(
        "PROJECT_ID and DATASET_ID env vars are required for live tests."
    )
  endpoint = os.environ.get("BQAA_JUDGE_ENDPOINT", "gemini-2.5-flash")
  connection_id = os.environ.get("BQAA_AI_GENERATE_CONNECTION_ID") or None
  return {
      "project": project,
      "dataset": dataset,
      "table": "agent_events",
      "endpoint": endpoint,
      "connection_id": connection_id,
  }


@pytest.fixture(scope="module")
def bq_client(live_config):
  """Real BigQuery client; skips cleanly when google-cloud-bigquery missing."""
  pytest.importorskip("google.cloud.bigquery")
  from google.cloud import bigquery

  return bigquery.Client(project=live_config["project"], location="US")


def _build_judge_params():
  """Return ScalarQueryParameter objects mirroring _ai_generate_judge.

  Uses a minimal hand-written prompt so the live test doesn't depend
  on a specific prebuilt evaluator's wording. The shape of the
  parameters is what matters for the SQL parse check.
  """
  from google.cloud import bigquery as bq

  prefix, middle, suffix = split_judge_prompt_template(
      "Score this 1-10 for helpfulness.\n## Trace\n{trace_text}\n"
      "## Final Response\n{final_response}\n"
      'Return JSON only: {{"score": <int>, "justification": <str>}}'
  )
  return [
      bq.ScalarQueryParameter("trace_limit", "INT64", 1),
      bq.ScalarQueryParameter("judge_prompt_prefix", "STRING", prefix),
      bq.ScalarQueryParameter("judge_prompt_middle", "STRING", middle),
      bq.ScalarQueryParameter("judge_prompt_suffix", "STRING", suffix),
  ]


def _run_query(bq_client, sql: str, params: list):
  """Submit the SQL to BigQuery and return the result rows."""
  from google.cloud import bigquery as bq

  job = bq_client.query(
      sql,
      job_config=bq.QueryJobConfig(query_parameters=params),
  )
  return list(job.result())


class TestAIGenerateJudgeLiveBigQuery:
  """End-to-end shape + behavior checks for the rendered SQL."""

  def test_query_parses_against_live_bigquery(self, live_config, bq_client):
    """The generated SQL must parse — primary regression for the
    'Table-valued function not found' bug that hit previous SDK
    versions when AI.GENERATE migrated to scalar-only form."""
    sql = render_ai_generate_judge_query(
        project=live_config["project"],
        dataset=live_config["dataset"],
        table=live_config["table"],
        # WHERE that almost certainly matches no rows; we only care
        # about parse + AI.GENERATE acceptance, not actual scoring.
        where="FALSE",
        endpoint=live_config["endpoint"],
        connection_id=None,
    )
    rows = _run_query(bq_client, sql, _build_judge_params())
    # FALSE filter -> zero rows is fine; the contract is "the query
    # parsed and ran." Schema introspection still works on empty
    # results.
    assert isinstance(rows, list)

  def test_returned_columns_match_unit_test_assumption(
      self, live_config, bq_client
  ):
    """Run with a permissive WHERE to actually invoke AI.GENERATE; assert
    the result schema names ``score`` / ``justification`` / ``gen_status``,
    matching what _ai_generate_judge() reads from each row."""
    sql = render_ai_generate_judge_query(
        project=live_config["project"],
        dataset=live_config["dataset"],
        table=live_config["table"],
        # Limit to one recent session; this is the smallest possible
        # AI.GENERATE invocation that proves end-to-end shape.
        where=(
            "timestamp > TIMESTAMP_SUB("
            "CURRENT_TIMESTAMP(), INTERVAL 24 HOUR)"
        ),
        endpoint=live_config["endpoint"],
        connection_id=None,
    )
    job = bq_client.query(
        sql,
        job_config=_query_config(_build_judge_params()),
    )
    schema = {field.name for field in job.result().schema}
    # _ai_generate_judge() reads these column names from each row.
    assert "session_id" in schema
    assert "score" in schema
    assert "justification" in schema
    # gen_status is the AI.GENERATE struct's status field, surfaced
    # so callers (or follow-up SDK polish) can distinguish parse
    # errors from low-score outcomes.
    assert "gen_status" in schema

  def test_with_connection_id_when_provided(self, live_config, bq_client):
    """If BQAA_AI_GENERATE_CONNECTION_ID is set, the same query also
    runs cleanly with the connection_id argument inlined."""
    connection_id = live_config["connection_id"]
    if not connection_id:
      pytest.skip(
          "BQAA_AI_GENERATE_CONNECTION_ID not set; skipping the"
          " optional-escape-hatch verification."
      )
    sql = render_ai_generate_judge_query(
        project=live_config["project"],
        dataset=live_config["dataset"],
        table=live_config["table"],
        where="FALSE",
        endpoint=live_config["endpoint"],
        connection_id=connection_id,
    )
    # Sanity-check that the connection arg made it into the SQL.
    assert f"connection_id => '{connection_id}'" in sql
    rows = _run_query(bq_client, sql, _build_judge_params())
    assert isinstance(rows, list)


def _query_config(params):
  from google.cloud import bigquery as bq

  return bq.QueryJobConfig(query_parameters=params)
