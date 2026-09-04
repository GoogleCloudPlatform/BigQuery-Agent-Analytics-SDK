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

"""Live integration tests for PerformanceEvaluator LLM judge path.

These tests run the PerformanceEvaluator against real BigQuery
infrastructure and Vertex AI (via google-genai) to verify that the
LLM-as-judge scoring path works E2E with real APIs.

Gating — skipped unless:
* ``BQAA_LIVE_BQ=1``
* ``BQAA_LIVE_BQ_PROJECT``
* ``BQAA_LIVE_BQ_SOURCE_DATASET``
"""

from __future__ import annotations

import asyncio
import os

import pytest

from bigquery_agent_analytics import Client
from bigquery_agent_analytics import PerformanceEvaluator
from bigquery_agent_analytics import TraceFilter
from bigquery_agent_analytics.performance_evaluator import EvalStatus

_LIVE = os.environ.get("BQAA_LIVE_BQ", "").lower() in ("1", "true", "yes")
_PROJECT = os.environ.get("BQAA_LIVE_BQ_PROJECT")
_SOURCE_DATASET = os.environ.get("BQAA_LIVE_BQ_SOURCE_DATASET")
_LOCATION = os.environ.get("BQAA_LIVE_BQ_LOCATION", "US")

pytestmark = pytest.mark.skipif(
    not _LIVE or not _PROJECT or not _SOURCE_DATASET,
    reason=(
        "Live BQ tests require BQAA_LIVE_BQ=1, BQAA_LIVE_BQ_PROJECT, "
        "and BQAA_LIVE_BQ_SOURCE_DATASET."
    ),
)


@pytest.fixture(scope="module")
def live_config():
  return {
      "project": _PROJECT,
      "dataset": _SOURCE_DATASET,
      "table": "agent_events",
      "location": _LOCATION,
  }


@pytest.fixture(scope="module")
def bq_client(live_config):
  pytest.importorskip("google.cloud.bigquery")
  from google.cloud import bigquery

  return bigquery.Client(
      project=live_config["project"], location=live_config["location"]
  )


@pytest.fixture(scope="module")
def sample_trace(bq_client, live_config):
  """Fetch one identity- and scope-resolved trace from the live source."""
  client = Client(
      project_id=live_config["project"],
      dataset_id=live_config["dataset"],
      table_id=live_config["table"],
      bq_client=bq_client,
  )
  traces = client.list_traces(TraceFilter(limit=1))
  if not traces:
    pytest.skip("No traces found in the configured live source")
  return traces[0]


def test_performance_evaluator_live_llm_judge(
    live_config, sample_trace, bq_client
):
  """E2E check that PerformanceEvaluator can evaluate a session using LLM judge."""
  evaluator = PerformanceEvaluator(
      project_id=live_config["project"],
      dataset_id=live_config["dataset"],
      table_id=live_config["table"],
      client=bq_client,
  )

  # Run evaluation synchronously
  async def run_eval():
    return await evaluator.evaluate_session(
        session_id=sample_trace.session_id,
        trace=sample_trace,
        use_llm_judge=True,
    )

  result = asyncio.run(run_eval())

  assert result.session_id == sample_trace.session_id
  assert not result.details.get("errors"), result.llm_judge_feedback
  assert result.eval_status in (EvalStatus.PASSED, EvalStatus.FAILED)

  # One-sided judge should populate these scores
  assert "llm_judge_sentiment" in result.scores
  assert "llm_judge_hallucination" in result.scores

  # Scores should be normalized between 0.0 and 1.0
  assert 0.0 <= result.scores["llm_judge_sentiment"] <= 1.0
  assert 0.0 <= result.scores["llm_judge_hallucination"] <= 1.0

  assert result.llm_judge_feedback is not None
  assert len(result.llm_judge_feedback) > 0
