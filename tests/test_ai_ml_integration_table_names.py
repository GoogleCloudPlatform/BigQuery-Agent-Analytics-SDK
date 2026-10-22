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

"""The AI/ML integration classes check their table names like drift does.

``AnomalyDetector`` and ``BatchEvaluator`` format ``table_id``, and
``EmbeddingSearchClient`` formats ``embeddings_table`` and
``source_table``, into backtick-quoted ``{project}.{dataset}.{table}``
paths in the queries they send. A backtick in any of them closed the path,
so the rest of the value became SQL, such as a ``CROSS JOIN`` on a table
in another dataset. The constructors, and every later assignment to those
attributes, now run drift's table-name check
(``feedback._validate_table_name``), so a bad value raises ``ValueError``
before any query, and table names with dashes, spaces or non-ASCII letters
keep working.
"""

import pytest

from bigquery_agent_analytics.ai_ml_integration import AnomalyDetector
from bigquery_agent_analytics.ai_ml_integration import BatchEvaluator
from bigquery_agent_analytics.ai_ml_integration import EmbeddingSearchClient
from tests.test_ai_ml_integration_ids import _bq_client
from tests.test_ai_ml_integration_ids import _built
from tests.test_ai_ml_integration_ids import _METHODS
from tests.test_ai_ml_integration_ids import _send
from tests.test_ai_ml_integration_ids import _sent_sql
from tests.test_client_table_id import _CROSS_JOIN_PAYLOAD
from tests.test_client_table_id import _NOT_A_TABLE_NAME
from tests.test_drift_production_table import _TABLE_NAMES

# Each table attribute: its class, its name and its default.
_TABLE_ATTRIBUTES = {
    "AnomalyDetector.table_id": (AnomalyDetector, "table_id", "agent_events"),
    "BatchEvaluator.table_id": (BatchEvaluator, "table_id", "agent_events"),
    "EmbeddingSearchClient.embeddings_table": (
        EmbeddingSearchClient,
        "embeddings_table",
        "trace_embeddings",
    ),
    "EmbeddingSearchClient.source_table": (
        EmbeddingSearchClient,
        "source_table",
        "agent_events",
    ),
}

# The legacy forecast reads only the trained model, not the table.
_ANOMALY_METHODS = [
    name
    for name in _METHODS
    if name.startswith("Anomaly")
    and name != "AnomalyDetector.forecast_latency-legacy"
]
_INDEX_METHODS = [
    name for name in _METHODS if ".build_embeddings_index" in name
]

# Every query that formats a table attribute: the method that sends it
# (one of the ID tests' methods), the attribute, and the path it renders,
# with {} for the table.
_SINKS = {
    **{
        f"{method}-table_id": (method, "table_id", "`p.d.{}`")
        for method in _ANOMALY_METHODS
    },
    "BatchEvaluator.evaluate_recent_sessions-table_id": (
        "BatchEvaluator.evaluate_recent_sessions",
        "table_id",
        "`p.d.{}`",
    ),
    "EmbeddingSearchClient.search-embeddings_table": (
        "EmbeddingSearchClient.search",
        "embeddings_table",
        "`p.d.{}`",
    ),
    **{
        f"{method}-embeddings_table": (
            method,
            "embeddings_table",
            "`p.d.{}_indexed`",
        )
        for method in _INDEX_METHODS
    },
    **{
        f"{method}-source_table": (method, "source_table", "`p.d.{}`")
        for method in _INDEX_METHODS
    },
}


@pytest.mark.parametrize(
    "cls,attribute,default",
    list(_TABLE_ATTRIBUTES.values()),
    ids=list(_TABLE_ATTRIBUTES),
)
class TestTableAttributes:
  """Construction and later assignment both run the table-name check."""

  @pytest.mark.parametrize(
      "table",
      list(_NOT_A_TABLE_NAME.values()),
      ids=list(_NOT_A_TABLE_NAME),
  )
  def test_constructor_rejects_value_that_is_not_one_table_name(
      self, cls, attribute, default, table
  ):
    bq = _bq_client()
    with pytest.raises(ValueError, match=f"^{attribute} must be one table"):
      _built(cls, bq, **{attribute: table})
    assert _sent_sql(bq) == []

  @pytest.mark.parametrize(
      "table",
      list(_NOT_A_TABLE_NAME.values()),
      ids=list(_NOT_A_TABLE_NAME),
  )
  def test_assignment_rejects_value_that_is_not_one_table_name(
      self, cls, attribute, default, table
  ):
    instance = _built(cls)
    with pytest.raises(ValueError, match=f"^{attribute} must be one table"):
      setattr(instance, attribute, table)
    assert getattr(instance, attribute) == default

  @pytest.mark.parametrize("table", _TABLE_NAMES)
  def test_accepts_table_name(self, cls, attribute, default, table):
    instance = _built(cls, **{attribute: table})
    assert getattr(instance, attribute) == table
    setattr(instance, attribute, default)
    assert getattr(instance, attribute) == default


@pytest.mark.parametrize(
    "method,attribute,path", list(_SINKS.values()), ids=list(_SINKS)
)
class TestEveryQuery:
  """No query sends a refused table, and table names still reach them."""

  def test_refused_assignment_never_reaches_the_query(
      self, method, attribute, path
  ):
    cls, options, send = _METHODS[method]
    bq = _bq_client()
    instance = _built(cls, bq, **options)
    default = getattr(instance, attribute)
    with pytest.raises(ValueError, match=f"^{attribute} must be one table"):
      setattr(instance, attribute, _CROSS_JOIN_PAYLOAD)
    _send(send, instance)
    sent = _sent_sql(bq)
    assert not any("CROSS JOIN" in sql for sql in sent)
    assert any(path.format(default) in sql for sql in sent)

  @pytest.mark.parametrize("table", _TABLE_NAMES)
  def test_table_name_reaches_the_query(self, method, attribute, path, table):
    cls, options, send = _METHODS[method]
    bq = _bq_client()
    _send(send, _built(cls, bq, **{**options, attribute: table}))
    assert any(path.format(table) in sql for sql in _sent_sql(bq))
