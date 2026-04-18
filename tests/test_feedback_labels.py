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

"""Tests that feedback.py wires every query site through SDK labels."""

import asyncio
from unittest.mock import MagicMock

import pytest

from bigquery_agent_analytics.feedback import _frequently_asked
from bigquery_agent_analytics.feedback import _frequently_unanswered
from bigquery_agent_analytics.feedback import _semantic_drift
from bigquery_agent_analytics.feedback import _semantic_grouping
from bigquery_agent_analytics.feedback import AnalysisConfig
from bigquery_agent_analytics.feedback import compute_drift


def _mock_bq_client():
  """Mock BigQuery client whose query() returns an empty result job."""
  client = MagicMock()
  query_job = MagicMock()
  query_job.result.return_value = []
  client.query.return_value = query_job
  return client


def _labels_per_call(mock_bq):
  """Return a list of (sql, labels) tuples, one per mock_bq.query(...) call."""
  out = []
  for call in mock_bq.query.call_args_list:
    args, kwargs = call
    sql = args[0] if args else kwargs.get("query", "")
    cfg = kwargs.get("job_config")
    labels = dict(cfg.labels) if cfg and cfg.labels else {}
    out.append((sql, labels))
  return out


class TestComputeDriftLabels:

  def test_golden_and_production_queries_both_labeled_drift(self):
    mock_bq = _mock_bq_client()
    asyncio.run(
        compute_drift(
            bq_client=mock_bq,
            project_id="p",
            dataset_id="d",
            table_id="agent_events",
            golden_table="golden",
            where_clause="1=1",
            query_params=[],
        )
    )
    calls = _labels_per_call(mock_bq)
    # Both fetches (golden, production) should carry sdk_feature=drift.
    assert len(calls) >= 2
    for _sql, labels in calls:
      assert labels.get("sdk_feature") == "drift"


class TestSemanticDriftLabels:

  def test_ai_embed_path_labels_with_ai_embed(self):
    mock_bq = _mock_bq_client()
    asyncio.run(
        _semantic_drift(
            bq_client=mock_bq,
            project_id="p",
            dataset_id="d",
            table_id="agent_events",
            golden_table="golden",
            where_clause="1=1",
            query_params=[],
            embedding_model="gemini-embedding-001",
            golden_questions=["g1", "g2"],
            prod_questions=["p1", "p2"],
            similarity_threshold=0.3,
        )
    )
    _sql, labels = _labels_per_call(mock_bq)[0]
    assert labels.get("sdk_feature") == "drift"
    assert labels.get("sdk_ai_function") == "ai-embed"

  def test_legacy_ml_path_labels_with_ml_generate_embedding(self):
    mock_bq = _mock_bq_client()
    asyncio.run(
        _semantic_drift(
            bq_client=mock_bq,
            project_id="p",
            dataset_id="d",
            table_id="agent_events",
            golden_table="golden",
            where_clause="1=1",
            query_params=[],
            embedding_model="p.d.embedding_model",  # legacy ref form
            golden_questions=["g1"],
            prod_questions=["p1"],
            similarity_threshold=0.3,
        )
    )
    _sql, labels = _labels_per_call(mock_bq)[0]
    assert labels.get("sdk_feature") == "drift"
    assert labels.get("sdk_ai_function") == "ml-generate-embedding"


class TestQuestionDistributionLabels:

  def test_frequently_asked_labels_feedback(self):
    mock_bq = _mock_bq_client()
    loop = asyncio.new_event_loop()
    try:
      loop.run_until_complete(
          _frequently_asked(
              mock_bq, "p", "d", "agent_events", "1=1", [], top_k=5, loop=loop
          )
      )
    finally:
      loop.close()
    _sql, labels = _labels_per_call(mock_bq)[0]
    assert labels.get("sdk_feature") == "feedback"

  def test_frequently_unanswered_labels_feedback(self):
    mock_bq = _mock_bq_client()
    loop = asyncio.new_event_loop()
    try:
      loop.run_until_complete(
          _frequently_unanswered(
              mock_bq, "p", "d", "agent_events", "1=1", [], top_k=5, loop=loop
          )
      )
    finally:
      loop.close()
    _sql, labels = _labels_per_call(mock_bq)[0]
    assert labels.get("sdk_feature") == "feedback"


class TestSemanticGroupingLabels:

  def test_ai_generate_grouping_labels_ai_generate(self):
    mock_bq = _mock_bq_client()
    config = AnalysisConfig(
        mode="auto_group_using_semantics",
        top_k=5,
    )
    loop = asyncio.new_event_loop()
    try:
      loop.run_until_complete(
          _semantic_grouping(
              mock_bq,
              "p",
              "d",
              "agent_events",
              "1=1",
              [],
              config,
              text_model="gemini-2.5-flash",
              loop=loop,
          )
      )
    finally:
      loop.close()
    calls = _labels_per_call(mock_bq)
    assert calls, "expected at least one query"
    # First call is the AI.GENERATE grouping path.
    _sql, labels = calls[0]
    assert labels.get("sdk_feature") == "feedback"
    assert labels.get("sdk_ai_function") == "ai-generate"

  def test_legacy_ml_generate_text_grouping_labels_ml_generate_text(self):
    # A legacy model ref (project.dataset.model) skips the AI.GENERATE
    # try block entirely and dispatches through the ML.GENERATE_TEXT
    # branch, which must carry ai_function=ml-generate-text.
    mock_bq = _mock_bq_client()
    config = AnalysisConfig(
        mode="auto_group_using_semantics",
        top_k=5,
    )
    loop = asyncio.new_event_loop()
    try:
      loop.run_until_complete(
          _semantic_grouping(
              mock_bq,
              "p",
              "d",
              "agent_events",
              "1=1",
              [],
              config,
              text_model="p.d.gemini_text_model",
              loop=loop,
          )
      )
    finally:
      loop.close()
    calls = _labels_per_call(mock_bq)
    assert calls, "expected the legacy branch to have dispatched a query"
    _sql, labels = calls[0]
    assert labels.get("sdk_feature") == "feedback"
    assert labels.get("sdk_ai_function") == "ml-generate-text"
