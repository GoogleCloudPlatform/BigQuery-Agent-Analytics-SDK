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

"""Tests that ai_ml_integration.py wires every query site through SDK labels.

One focused test per function family (primary/legacy branches as separate
tests). One async-path regression test proving labels attach on the
QueryJobConfig BEFORE loop.run_in_executor dispatch. Warn-once tests for
every class that accepts an injected client.
"""

import asyncio
from datetime import datetime
from datetime import timezone
import logging
from unittest.mock import MagicMock

from google.auth.credentials import AnonymousCredentials
from google.cloud import bigquery
import pytest

from bigquery_agent_analytics.ai_ml_integration import AnomalyDetector
from bigquery_agent_analytics.ai_ml_integration import BatchEvaluator
from bigquery_agent_analytics.ai_ml_integration import BigQueryAIClient
from bigquery_agent_analytics.ai_ml_integration import EmbeddingSearchClient


def _mock_bq_client():
  """Mock client whose .query().result() returns an empty iterable."""
  client = MagicMock()
  job = MagicMock()
  job.result.return_value = []
  client.query.return_value = job
  return client


def _last_job_config(mock_bq):
  return mock_bq.query.call_args.kwargs.get("job_config")


def _last_labels(mock_bq):
  cfg = _last_job_config(mock_bq)
  return dict(cfg.labels) if cfg and cfg.labels else {}


def _all_labels(mock_bq):
  return [
      dict(call.kwargs["job_config"].labels or {})
      for call in mock_bq.query.call_args_list
      if call.kwargs.get("job_config") is not None
  ]


def _run(coro):
  loop = asyncio.new_event_loop()
  try:
    return loop.run_until_complete(coro)
  finally:
    loop.close()


# ------------------------------------------------------------------ #
# BigQueryAIClient                                                     #
# ------------------------------------------------------------------ #


class TestGenerateTextLabels:

  def test_generate_text_labels_ai_generate(self):
    mock_bq = _mock_bq_client()
    client = BigQueryAIClient(project_id="p", dataset_id="d", client=mock_bq)
    _run(client.generate_text("hello"))
    labels = _last_labels(mock_bq)
    assert labels.get("sdk_feature") == "ai-ml"
    assert labels.get("sdk_ai_function") == "ai-generate"


class TestGenerateEmbeddingsLabels:

  def test_ai_embed_path_labels_ai_embed(self):
    mock_bq = _mock_bq_client()
    client = BigQueryAIClient(project_id="p", dataset_id="d", client=mock_bq)
    _run(client.generate_embeddings(["text1", "text2"]))
    labels = _last_labels(mock_bq)
    assert labels.get("sdk_feature") == "ai-ml"
    assert labels.get("sdk_ai_function") == "ai-embed"

  def test_legacy_ml_generate_embedding_path_labels_ml_generate_embedding(
      self,
  ):
    mock_bq = _mock_bq_client()
    client = BigQueryAIClient(
        project_id="p",
        dataset_id="d",
        client=mock_bq,
        # Fully-qualified BQ ML model reference triggers the legacy path.
        embedding_model="p.d.text_embedding_model",
    )
    _run(client.generate_embeddings(["text1"]))
    labels = _last_labels(mock_bq)
    assert labels.get("sdk_feature") == "ai-ml"
    assert labels.get("sdk_ai_function") == "ml-generate-embedding"


# ------------------------------------------------------------------ #
# EmbeddingSearchClient                                                #
# ------------------------------------------------------------------ #


class TestVectorSearchLabels:

  def test_search_labels_ai_ml_without_ai_function(self):
    # ML.DISTANCE is vector math, not an LLM invocation. The SDK only
    # sets feature="ai-ml"; no sdk_ai_function dimension.
    mock_bq = _mock_bq_client()
    client = EmbeddingSearchClient(
        project_id="p", dataset_id="d", client=mock_bq
    )
    _run(client.search(query_embedding=[0.1, 0.2], top_k=5))
    labels = _last_labels(mock_bq)
    assert labels.get("sdk_feature") == "ai-ml"
    assert "sdk_ai_function" not in labels


class TestBuildEmbeddingsIndexLabels:

  def test_ai_embed_path_labels_ai_embed(self):
    mock_bq = _mock_bq_client()
    client = EmbeddingSearchClient(
        project_id="p", dataset_id="d", client=mock_bq
    )
    _run(client.build_embeddings_index(since_days=30))
    labels = _last_labels(mock_bq)
    assert labels.get("sdk_feature") == "ai-ml"
    assert labels.get("sdk_ai_function") == "ai-embed"

  def test_legacy_index_labels_ml_generate_embedding(self):
    mock_bq = _mock_bq_client()
    client = EmbeddingSearchClient(
        project_id="p",
        dataset_id="d",
        client=mock_bq,
        embedding_model="p.d.text_embedding_model",
    )
    _run(client.build_embeddings_index(since_days=30))
    labels = _last_labels(mock_bq)
    assert labels.get("sdk_feature") == "ai-ml"
    assert labels.get("sdk_ai_function") == "ml-generate-embedding"


# ------------------------------------------------------------------ #
# AnomalyDetector                                                      #
# ------------------------------------------------------------------ #


class TestTrainLatencyModelLabels:

  def test_legacy_train_labels_ai_ml_without_ai_function(self):
    # Only exercised when use_legacy_anomaly_model=True; CREATE MODEL
    # DDL does not invoke an AI function at query time.
    mock_bq = _mock_bq_client()
    detector = AnomalyDetector(
        project_id="p",
        dataset_id="d",
        client=mock_bq,
        use_legacy_anomaly_model=True,
    )
    _run(detector.train_latency_model(training_days=7))
    labels = _last_labels(mock_bq)
    assert labels.get("sdk_feature") == "ai-ml"
    assert "sdk_ai_function" not in labels


class TestDetectLatencyAnomaliesLabels:

  def test_ai_path_labels_ai_detect_anomalies(self):
    mock_bq = _mock_bq_client()
    detector = AnomalyDetector(project_id="p", dataset_id="d", client=mock_bq)
    _run(detector.detect_latency_anomalies(since_hours=24))
    labels = _last_labels(mock_bq)
    assert labels.get("sdk_feature") == "ai-ml"
    assert labels.get("sdk_ai_function") == "ai-detect-anomalies"

  def test_legacy_path_labels_ml_detect_anomalies(self):
    mock_bq = _mock_bq_client()
    detector = AnomalyDetector(
        project_id="p",
        dataset_id="d",
        client=mock_bq,
        use_legacy_anomaly_model=True,
    )
    _run(detector.detect_latency_anomalies(since_hours=24))
    labels = _last_labels(mock_bq)
    assert labels.get("sdk_feature") == "ai-ml"
    assert labels.get("sdk_ai_function") == "ml-detect-anomalies"


class TestForecastLatencyLabels:

  def test_ai_path_labels_ai_forecast(self):
    mock_bq = _mock_bq_client()
    detector = AnomalyDetector(project_id="p", dataset_id="d", client=mock_bq)
    _run(detector.forecast_latency(horizon_hours=24))
    labels = _last_labels(mock_bq)
    assert labels.get("sdk_feature") == "ai-ml"
    assert labels.get("sdk_ai_function") == "ai-forecast"

  def test_legacy_path_labels_ml_forecast(self):
    mock_bq = _mock_bq_client()
    detector = AnomalyDetector(
        project_id="p",
        dataset_id="d",
        client=mock_bq,
        use_legacy_anomaly_model=True,
    )
    _run(detector.forecast_latency(horizon_hours=24))
    labels = _last_labels(mock_bq)
    assert labels.get("sdk_feature") == "ai-ml"
    assert labels.get("sdk_ai_function") == "ml-forecast"


class TestTrainBehaviorModelLabels:

  def test_both_queries_labeled_ai_ml(self):
    # Dispatches TWO queries (features-table build + autoencoder
    # CREATE MODEL). Neither invokes an AI function at query time;
    # both should carry sdk_feature=ai-ml without sdk_ai_function.
    mock_bq = _mock_bq_client()
    detector = AnomalyDetector(project_id="p", dataset_id="d", client=mock_bq)
    _run(detector.train_behavior_model())
    all_labels = _all_labels(mock_bq)
    assert len(all_labels) == 2
    for labels in all_labels:
      assert labels.get("sdk_feature") == "ai-ml"
      assert "sdk_ai_function" not in labels


class TestDetectBehaviorAnomaliesLabels:

  def test_labels_ml_detect_anomalies(self):
    mock_bq = _mock_bq_client()
    detector = AnomalyDetector(project_id="p", dataset_id="d", client=mock_bq)
    _run(detector.detect_behavior_anomalies(since_hours=24))
    labels = _last_labels(mock_bq)
    assert labels.get("sdk_feature") == "ai-ml"
    assert labels.get("sdk_ai_function") == "ml-detect-anomalies"


# ------------------------------------------------------------------ #
# BatchEvaluator                                                       #
# ------------------------------------------------------------------ #


class TestEvaluateRecentSessionsLabels:

  def test_labels_ai_generate(self):
    mock_bq = _mock_bq_client()
    evaluator = BatchEvaluator(project_id="p", dataset_id="d", client=mock_bq)
    _run(evaluator.evaluate_recent_sessions(days=1, limit=10))
    labels = _last_labels(mock_bq)
    assert labels.get("sdk_feature") == "ai-ml"
    assert labels.get("sdk_ai_function") == "ai-generate"


# ------------------------------------------------------------------ #
# Async dispatch ordering — labels must land BEFORE run_in_executor     #
# ------------------------------------------------------------------ #


class TestAsyncDispatchOrdering:
  """Proves that labels are attached to the QueryJobConfig in the
  caller's thread, before the lambda crosses the executor boundary.

  The mock's `query` lambda is invoked inside the worker thread, but
  its captured `job_config` reference is the same object the caller's
  thread built — so asserting labels are present at call_args is
  equivalent to asserting they were set *before* dispatch. To make
  this explicit, the test runs through a real asyncio loop with the
  real default ThreadPoolExecutor, catching any regression where a
  future design re-introduces per-thread state for label resolution.
  """

  def test_labels_present_on_config_handed_to_executor(self):
    mock_bq = MagicMock()

    def capture(query, job_config=None, **_):
      # Inspect from the executor worker thread.
      captured = dict(job_config.labels or {}) if job_config else {}
      captured["_query_preview"] = (query or "")[:48]
      mock_bq._captured = captured
      result_job = MagicMock()
      result_job.result.return_value = []
      return result_job

    mock_bq.query.side_effect = capture

    client = BigQueryAIClient(project_id="p", dataset_id="d", client=mock_bq)
    asyncio.run(client.generate_text("hello"))

    captured = mock_bq._captured
    assert captured.get("sdk_feature") == "ai-ml"
    assert captured.get("sdk_ai_function") == "ai-generate"


# ------------------------------------------------------------------ #
# Warn-once pattern for each class that accepts an injected client      #
# ------------------------------------------------------------------ #


class TestVanillaClientWarnOnce:

  @pytest.mark.parametrize(
      "factory",
      [
          lambda vanilla: BigQueryAIClient(
              project_id="p", dataset_id="d", client=vanilla
          ),
          lambda vanilla: EmbeddingSearchClient(
              project_id="p", dataset_id="d", client=vanilla
          ),
          lambda vanilla: AnomalyDetector(
              project_id="p", dataset_id="d", client=vanilla
          ),
          lambda vanilla: BatchEvaluator(
              project_id="p", dataset_id="d", client=vanilla
          ),
      ],
      ids=[
          "BigQueryAIClient",
          "EmbeddingSearchClient",
          "AnomalyDetector",
          "BatchEvaluator",
      ],
  )
  def test_vanilla_client_emits_one_warning(self, caplog, factory):
    vanilla = bigquery.Client(project="p", credentials=AnonymousCredentials())
    obj = factory(vanilla)

    with caplog.at_level(logging.WARNING):
      _ = obj.client
      _ = obj.client
      _ = obj.client

    warnings = [
        r
        for r in caplog.records
        if "SDK telemetry labels will not be applied" in r.message
    ]
    assert len(warnings) == 1
