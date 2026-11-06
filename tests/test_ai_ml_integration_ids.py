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

"""The AI/ML integration classes check project and dataset IDs like Client.

``EmbeddingSearchClient``, ``AnomalyDetector`` and ``BatchEvaluator``
format ``project_id`` and ``dataset_id`` into every query they send, as
backtick-quoted ``{project}.{dataset}`` table and model paths, so a
backtick in either value closed the path and the rest of the value
became SQL. Their constructors, and every later assignment to either
attribute, now run the checks ``Client`` runs, so a bad value raises
``ValueError`` before any query, and a hyphenated dataset such as
``my-ds`` keeps working.
"""

import asyncio
from unittest.mock import MagicMock

import pytest

from bigquery_agent_analytics.ai_ml_integration import AnomalyDetector
from bigquery_agent_analytics.ai_ml_integration import BatchEvaluator
from bigquery_agent_analytics.ai_ml_integration import BigQueryAIClient
from bigquery_agent_analytics.ai_ml_integration import EmbeddingSearchClient
from tests.test_client_project_dataset_ids import _ASSIGNED_DATASET_PAYLOAD
from tests.test_client_project_dataset_ids import _ASSIGNED_PROJECT_PAYLOAD
from tests.test_client_project_dataset_ids import _DATASET_IDS
from tests.test_client_project_dataset_ids import _NOT_A_DATASET_ID
from tests.test_client_project_dataset_ids import _NOT_A_PROJECT_ID
from tests.test_client_project_dataset_ids import _PROJECT_IDS

# The classes that format both IDs into their SQL.
_CLASSES = {
    "EmbeddingSearchClient": EmbeddingSearchClient,
    "AnomalyDetector": AnomalyDetector,
    "BatchEvaluator": BatchEvaluator,
}

# Every query those classes send: the class, the constructor options that
# pick the query, and the call that sends it.
_METHODS = {
    "EmbeddingSearchClient.search": (
        EmbeddingSearchClient,
        {},
        lambda search: search.search(query_embedding=[0.1, 0.2]),
    ),
    "EmbeddingSearchClient.build_embeddings_index": (
        EmbeddingSearchClient,
        {},
        lambda search: search.build_embeddings_index(),
    ),
    "EmbeddingSearchClient.build_embeddings_index-legacy": (
        EmbeddingSearchClient,
        {"embedding_model": "models.embedding.text_embedding"},
        lambda search: search.build_embeddings_index(),
    ),
    "AnomalyDetector.train_latency_model": (
        AnomalyDetector,
        {"use_legacy_anomaly_model": True},
        lambda detector: detector.train_latency_model(),
    ),
    "AnomalyDetector.detect_latency_anomalies": (
        AnomalyDetector,
        {},
        lambda detector: detector.detect_latency_anomalies(),
    ),
    "AnomalyDetector.detect_latency_anomalies-legacy": (
        AnomalyDetector,
        {"use_legacy_anomaly_model": True},
        lambda detector: detector.detect_latency_anomalies(),
    ),
    "AnomalyDetector.forecast_latency": (
        AnomalyDetector,
        {},
        lambda detector: detector.forecast_latency(),
    ),
    "AnomalyDetector.forecast_latency-legacy": (
        AnomalyDetector,
        {"use_legacy_anomaly_model": True},
        lambda detector: detector.forecast_latency(),
    ),
    "AnomalyDetector.train_behavior_model": (
        AnomalyDetector,
        {},
        lambda detector: detector.train_behavior_model(),
    ),
    "AnomalyDetector.detect_behavior_anomalies": (
        AnomalyDetector,
        {},
        lambda detector: detector.detect_behavior_anomalies(),
    ),
    "BatchEvaluator.evaluate_recent_sessions": (
        BatchEvaluator,
        {},
        lambda evaluator: evaluator.evaluate_recent_sessions(),
    ),
}


def _bq_client():
  """Fake BigQuery client whose every query returns no rows."""
  bq = MagicMock()
  bq.query.return_value.result.return_value = []
  return bq


def _sent_sql(bq):
  return [call.args[0] for call in bq.query.call_args_list]


def _built(cls, bq=None, **options):
  return cls(
      project_id="p", dataset_id="d", client=bq or _bq_client(), **options
  )


def _send(method, instance):
  asyncio.run(method(instance))


@pytest.mark.parametrize("cls", list(_CLASSES.values()), ids=list(_CLASSES))
class TestConstructor:
  """Each class refuses a bad ID when it is built, as ``Client()`` does."""

  @pytest.mark.parametrize(
      "dataset_id",
      list(_NOT_A_DATASET_ID.values()),
      ids=list(_NOT_A_DATASET_ID),
  )
  def test_rejects_value_that_is_not_one_dataset_id(self, cls, dataset_id):
    with pytest.raises(
        ValueError, match="^dataset_id must be one BigQuery dataset ID"
    ):
      cls(project_id="p", dataset_id=dataset_id, client=_bq_client())

  @pytest.mark.parametrize(
      "project_id",
      list(_NOT_A_PROJECT_ID.values()),
      ids=list(_NOT_A_PROJECT_ID),
  )
  def test_rejects_value_that_is_not_one_project_id(self, cls, project_id):
    with pytest.raises(
        ValueError, match="^project_id must be one Google Cloud project ID"
    ):
      cls(project_id=project_id, dataset_id="d", client=_bq_client())

  @pytest.mark.parametrize("dataset_id", _DATASET_IDS)
  def test_accepts_dataset_id(self, cls, dataset_id):
    instance = cls(
        project_id="my-project", dataset_id=dataset_id, client=_bq_client()
    )
    assert instance.dataset_id == dataset_id

  @pytest.mark.parametrize("project_id", _PROJECT_IDS)
  def test_accepts_project_id(self, cls, project_id):
    instance = cls(
        project_id=project_id, dataset_id="analytics", client=_bq_client()
    )
    assert instance.project_id == project_id


@pytest.mark.parametrize("cls", list(_CLASSES.values()), ids=list(_CLASSES))
class TestAssignmentAfterConstruction:
  """Setting ``project_id`` or ``dataset_id`` later runs the same check."""

  @pytest.mark.parametrize(
      "dataset_id",
      list(_NOT_A_DATASET_ID.values()),
      ids=list(_NOT_A_DATASET_ID),
  )
  def test_rejects_value_that_is_not_one_dataset_id(self, cls, dataset_id):
    instance = _built(cls)
    with pytest.raises(
        ValueError, match="^dataset_id must be one BigQuery dataset ID"
    ):
      instance.dataset_id = dataset_id
    assert instance.dataset_id == "d"

  @pytest.mark.parametrize(
      "project_id",
      list(_NOT_A_PROJECT_ID.values()),
      ids=list(_NOT_A_PROJECT_ID),
  )
  def test_rejects_value_that_is_not_one_project_id(self, cls, project_id):
    instance = _built(cls)
    with pytest.raises(
        ValueError, match="^project_id must be one Google Cloud project ID"
    ):
      instance.project_id = project_id
    assert instance.project_id == "p"


@pytest.mark.parametrize(
    "cls,options,method", list(_METHODS.values()), ids=list(_METHODS)
)
class TestQueries:
  """No query carries a bad ID, and valid IDs reach every query."""

  @pytest.mark.parametrize(
      "field,value",
      [
          ("dataset_id", _ASSIGNED_DATASET_PAYLOAD),
          ("project_id", _ASSIGNED_PROJECT_PAYLOAD),
      ],
      ids=["dataset", "project"],
  )
  def test_injected_assignment_never_reaches_a_query(
      self, cls, options, method, field, value
  ):
    bq = _bq_client()
    instance = _built(cls, bq, **options)
    with pytest.raises(ValueError, match=f"^{field} must be one"):
      setattr(instance, field, value)
      _send(method, instance)
    assert _sent_sql(bq) == []

  @pytest.mark.parametrize(
      "field,value,path",
      [
          ("dataset_id", "my-ds", "`p.my-ds."),
          (
              "project_id",
              "example.com:my-project",
              "`example.com:my-project.d.",
          ),
      ],
      ids=["dataset", "project"],
  )
  def test_valid_assignment_reaches_every_query(
      self, cls, options, method, field, value, path
  ):
    bq = _bq_client()
    instance = _built(cls, bq, **options)
    setattr(instance, field, value)
    _send(method, instance)
    sent = _sent_sql(bq)
    assert sent
    for sql in sent:
      assert path in sql


def test_bigquery_ai_client_formats_neither_id_into_sql():
  # BigQueryAIClient keeps both IDs but its queries read neither, so it
  # needs no check. If one starts to, give it the check the others have.
  bq = _bq_client()
  ai = BigQueryAIClient(
      project_id="unused-project",
      dataset_id="unused_dataset",
      client=bq,
      embedding_model="models.embedding.text_embedding",
  )
  asyncio.run(ai.generate_text("hello"))
  asyncio.run(ai.generate_embeddings(["hello"]))
  ai.embedding_model = None
  asyncio.run(ai.generate_embeddings(["hello"]))
  sent = _sent_sql(bq)
  assert len(sent) == 3
  for sql in sent:
    assert "unused-project" not in sql
    assert "unused_dataset" not in sql
