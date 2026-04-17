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

"""Integration tests: client.py wires query call sites through SDK labels."""

from unittest import mock
from unittest.mock import MagicMock

from google.auth.credentials import AnonymousCredentials
from google.cloud import bigquery
import pytest

from bigquery_agent_analytics._telemetry import LabeledBigQueryClient
from bigquery_agent_analytics.client import Client


def _make_client(**kwargs):
  """Construct an SDK Client with schema verification skipped."""
  kwargs.setdefault("verify_schema", False)
  return Client(
      project_id="test-project", dataset_id="agent_analytics", **kwargs
  )


class TestClientBqClientFactory:
  """Tests for how `Client.bq_client` constructs or adapts its underlying BQ client."""

  def test_default_bq_client_is_labeled(self):
    client = _make_client()
    # Avoid making a real BQ connection during the test.
    with mock.patch(
        "bigquery_agent_analytics.client.make_bq_client",
        wraps=lambda *a, **kw: LabeledBigQueryClient(
            project="test-project",
            credentials=AnonymousCredentials(),
            sdk_surface="python",
        ),
    ) as factory:
      bq = client.bq_client
    factory.assert_called_once()
    assert isinstance(bq, LabeledBigQueryClient)
    assert bq._sdk_surface == "python"

  def test_user_provided_labeled_client_is_preserved(self):
    labeled = LabeledBigQueryClient(
        project="test-project",
        credentials=AnonymousCredentials(),
        sdk_surface="python",
    )
    client = _make_client(bq_client=labeled)
    assert client.bq_client is labeled

  def test_user_provided_vanilla_client_is_honored_as_is(self, caplog):
    # PR #24 review: rebuilding the caller's client from project /
    # credentials / location alone silently drops
    # default_query_job_config, client_info, client_options, custom
    # transport, subclass overrides, etc. Honor it as-is and WARN once
    # that SDK labels will not apply.
    import logging

    vanilla = bigquery.Client(
        project="test-project", credentials=AnonymousCredentials()
    )
    client = _make_client(bq_client=vanilla)
    with caplog.at_level(logging.WARNING):
      returned = client.bq_client
    assert returned is vanilla
    assert not isinstance(returned, LabeledBigQueryClient)
    assert any(
        "SDK telemetry labels will not be applied" in r.message
        for r in caplog.records
    )

  def test_warning_emitted_at_most_once_per_client(self, caplog):
    import logging

    vanilla = bigquery.Client(
        project="test-project", credentials=AnonymousCredentials()
    )
    client = _make_client(bq_client=vanilla)
    with caplog.at_level(logging.WARNING):
      _ = client.bq_client
      _ = client.bq_client
      _ = client.bq_client
    warnings = [
        r
        for r in caplog.records
        if "SDK telemetry labels will not be applied" in r.message
    ]
    assert len(warnings) == 1

  def test_vanilla_client_default_query_job_config_preserved(self):
    # Regression guard for PR #24 review: caller-set defaults like
    # maximum_bytes_billed must survive Client construction.
    default_cfg = bigquery.QueryJobConfig(
        maximum_bytes_billed=1_000_000_000, use_legacy_sql=False
    )
    default_cfg.labels = {"team": "search"}

    vanilla = bigquery.Client(
        project="test-project", credentials=AnonymousCredentials()
    )
    vanilla.default_query_job_config = default_cfg

    client = _make_client(bq_client=vanilla)
    returned = client.bq_client
    assert returned is vanilla
    # `default_query_job_config` returns a copy, so check content not identity.
    assert returned.default_query_job_config.maximum_bytes_billed == (
        1_000_000_000
    )
    assert returned.default_query_job_config.use_legacy_sql is False
    assert returned.default_query_job_config.labels == {"team": "search"}

  def test_mock_client_is_not_wrapped(self):
    # Existing tests pass MagicMock() as bq_client. Those must keep working;
    # the SDK only wraps real bigquery.Client instances.
    mock_client = MagicMock()
    client = _make_client(bq_client=mock_client)
    assert client.bq_client is mock_client


def _captured_labels_for(mock_bq):
  """Return the labels dict from the most recent mock_bq.query() call."""
  job_config = mock_bq.query.call_args.kwargs.get("job_config")
  assert job_config is not None, "expected a job_config on query()"
  return dict(job_config.labels or {})


class TestQuerySiteLabels:
  """Assert that representative query sites wire through `with_sdk_labels`."""

  def test_get_trace_labels_with_trace_read(self):
    mock_bq = MagicMock()
    mock_job = MagicMock()
    mock_job.result.return_value = iter([])
    mock_bq.query.return_value = mock_job

    client = _make_client(bq_client=mock_bq)
    # Empty result raises, but we only care that the query was dispatched
    # with the right label on the job config.
    with pytest.raises(ValueError):
      client.get_trace("trace-123")

    labels = _captured_labels_for(mock_bq)
    assert labels.get("sdk_feature") == "trace-read"

  def test_list_traces_labels_with_trace_read(self):
    mock_bq = MagicMock()
    mock_job = MagicMock()
    mock_job.result.return_value = iter([])
    mock_bq.query.return_value = mock_job

    client = _make_client(bq_client=mock_bq)
    from bigquery_agent_analytics.trace import TraceFilter

    client.list_traces(TraceFilter(agent_id="test-agent"))

    labels = _captured_labels_for(mock_bq)
    assert labels.get("sdk_feature") == "trace-read"

  def test_ai_generate_judge_labels_with_ai_generate(self):
    from bigquery_agent_analytics.evaluators import LLMAsJudge

    mock_bq = MagicMock()
    mock_job = MagicMock()
    # Empty result is fine — we only care that the query ran with the
    # expected labels.
    mock_job.result.return_value = iter([])
    mock_bq.query.return_value = mock_job

    client = _make_client(bq_client=mock_bq, connection_id="proj.us.conn")
    judge = LLMAsJudge.correctness(threshold=0.7)
    client.evaluate(evaluator=judge)

    # Multiple queries may fire (session summary + judge). At least one
    # should be the judge query with eval-llm-judge + ai-generate.
    judge_calls = [
        c
        for c in mock_bq.query.call_args_list
        if c.kwargs.get("job_config")
        and dict(c.kwargs["job_config"].labels or {}).get("sdk_feature")
        == "eval-llm-judge"
    ]
    assert judge_calls, "no query labeled with sdk_feature=eval-llm-judge"
    judge_labels = dict(judge_calls[0].kwargs["job_config"].labels or {})
    assert judge_labels.get("sdk_ai_function") == "ai-generate"
