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

"""Tests for _telemetry module (SDK label emission for BigQuery jobs)."""

import asyncio
import logging
from unittest import mock

from google.auth.credentials import AnonymousCredentials
from google.cloud import bigquery
import pytest

from bigquery_agent_analytics._telemetry import LabeledBigQueryClient
from bigquery_agent_analytics._telemetry import make_bq_client
from bigquery_agent_analytics._telemetry import with_sdk_labels


def _make_test_client(surface="python"):
  return LabeledBigQueryClient(
      project="test-project",
      credentials=AnonymousCredentials(),
      sdk_surface=surface,
  )


class TestWithSdkLabels:
  """Tests for the public `with_sdk_labels` helper (per-call enrichment)."""

  def test_sets_feature_label(self):
    cfg = with_sdk_labels(bigquery.QueryJobConfig(), feature="trace-read")
    assert cfg.labels["sdk_feature"] == "trace-read"

  def test_sets_ai_function_label(self):
    cfg = with_sdk_labels(
        bigquery.QueryJobConfig(),
        feature="eval-llm-judge",
        ai_function="ai-generate",
    )
    assert cfg.labels["sdk_ai_function"] == "ai-generate"

  def test_omits_ai_function_when_not_provided(self):
    cfg = with_sdk_labels(bigquery.QueryJobConfig(), feature="trace-read")
    assert "sdk_ai_function" not in cfg.labels

  def test_requires_explicit_cfg(self):
    # PR #23 review: passing None silently returned a QueryJobConfig even
    # for load-job call sites, which would then get passed into
    # load_table_from_json() with the wrong type. Require the caller to
    # construct the appropriate config class.
    with pytest.raises(TypeError, match="cfg"):
      with_sdk_labels(None, feature="trace-read")

  def test_accepts_load_job_config(self):
    cfg = bigquery.LoadJobConfig()
    result = with_sdk_labels(cfg, feature="ontology-build")
    assert result is cfg
    assert isinstance(result, bigquery.LoadJobConfig)
    assert result.labels["sdk_feature"] == "ontology-build"

  def test_preserves_existing_user_labels(self):
    cfg = bigquery.QueryJobConfig()
    cfg.labels = {"team": "search"}
    with_sdk_labels(cfg, feature="trace-read")
    assert cfg.labels["team"] == "search"
    assert cfg.labels["sdk_feature"] == "trace-read"

  def test_preserves_query_parameters(self):
    cfg = bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter("x", "INT64", 1)]
    )
    with_sdk_labels(cfg, feature="trace-read")
    assert len(cfg.query_parameters) == 1

  def test_rejects_uppercase_feature(self):
    with pytest.raises(ValueError, match="feature"):
      with_sdk_labels(bigquery.QueryJobConfig(), feature="TraceRead")

  def test_rejects_feature_with_invalid_chars(self):
    with pytest.raises(ValueError, match="feature"):
      with_sdk_labels(bigquery.QueryJobConfig(), feature="user@example.com")

  def test_rejects_feature_over_63_chars(self):
    with pytest.raises(ValueError, match="feature"):
      with_sdk_labels(bigquery.QueryJobConfig(), feature="a" * 64)

  def test_rejects_ai_function_with_invalid_chars(self):
    with pytest.raises(ValueError, match="ai_function"):
      with_sdk_labels(
          bigquery.QueryJobConfig(),
          feature="eval-llm-judge",
          ai_function="AI.GENERATE",
      )

  def test_accepts_valid_identifiers(self):
    cfg = with_sdk_labels(
        bigquery.QueryJobConfig(),
        feature="context-graph",
        ai_function="ai-generate",
    )
    assert cfg.labels["sdk_feature"] == "context-graph"
    assert cfg.labels["sdk_ai_function"] == "ai-generate"


class TestLabeledBigQueryClient:
  """Tests for the client subclass that applies default labels to every job."""

  def test_rejects_invalid_surface_at_construction(self):
    with pytest.raises(ValueError, match="surface"):
      LabeledBigQueryClient(
          project="p",
          credentials=AnonymousCredentials(),
          sdk_surface="PYTHON",
      )

  def test_query_applies_default_labels_when_no_config(self):
    client = _make_test_client(surface="cli")
    with mock.patch.object(bigquery.Client, "query") as parent_query:
      client.query("SELECT 1")
    _, kwargs = parent_query.call_args
    cfg = kwargs["job_config"]
    assert cfg.labels["sdk"] == "bigquery-agent-analytics"
    assert cfg.labels["sdk_surface"] == "cli"
    assert "sdk_version" in cfg.labels

  def test_query_merges_labels_into_existing_config(self):
    client = _make_test_client(surface="python")
    user_cfg = bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter("x", "INT64", 1)]
    )
    with mock.patch.object(bigquery.Client, "query") as parent_query:
      client.query("SELECT @x", job_config=user_cfg)
    _, kwargs = parent_query.call_args
    assert kwargs["job_config"] is user_cfg  # same object, mutated
    assert user_cfg.labels["sdk"] == "bigquery-agent-analytics"
    assert len(user_cfg.query_parameters) == 1  # untouched

  def test_preserves_non_reserved_user_labels(self):
    client = _make_test_client()
    cfg = bigquery.QueryJobConfig()
    cfg.labels = {"team": "search", "env": "prod"}
    with mock.patch.object(bigquery.Client, "query"):
      client.query("SELECT 1", job_config=cfg)
    assert cfg.labels["team"] == "search"
    assert cfg.labels["env"] == "prod"

  def test_overrides_user_set_default_keys_with_warning(self, caplog):
    client = _make_test_client(surface="python")
    cfg = bigquery.QueryJobConfig()
    cfg.labels = {"sdk": "my-fork", "sdk_version": "0-0-1"}
    with (
        caplog.at_level(logging.WARNING),
        mock.patch.object(bigquery.Client, "query"),
    ):
      client.query("SELECT 1", job_config=cfg)
    assert cfg.labels["sdk"] == "bigquery-agent-analytics"
    assert any("reserved SDK label keys" in r.message for r in caplog.records)

  def test_no_warning_when_user_labels_are_not_reserved(self, caplog):
    client = _make_test_client()
    cfg = bigquery.QueryJobConfig()
    cfg.labels = {"team": "search"}
    with (
        caplog.at_level(logging.WARNING),
        mock.patch.object(bigquery.Client, "query"),
    ):
      client.query("SELECT 1", job_config=cfg)
    assert not caplog.records

  def test_surface_is_instance_attribute_not_contextvar(self):
    # Regression guard: labels must come from self._sdk_surface, not any
    # contextvar-like machinery. Two clients with different surfaces must
    # produce different labels even when called from the same thread.
    cli_client = _make_test_client(surface="cli")
    rf_client = _make_test_client(surface="remote-function")

    seen = []

    def capture(self, query, job_config=None, **_):
      seen.append(job_config.labels["sdk_surface"])
      return mock.MagicMock()

    with mock.patch.object(bigquery.Client, "query", capture):
      cli_client.query("SELECT 1")
      rf_client.query("SELECT 1")

    assert seen == ["cli", "remote-function"]

  def test_query_and_wait_also_labels(self):
    client = _make_test_client()
    with mock.patch.object(bigquery.Client, "query_and_wait") as parent:
      client.query_and_wait("SELECT 1")
    _, kwargs = parent.call_args
    assert kwargs["job_config"].labels["sdk"] == "bigquery-agent-analytics"

  def test_load_table_from_json_labels_load_job(self):
    client = _make_test_client()
    with mock.patch.object(bigquery.Client, "load_table_from_json") as parent:
      client.load_table_from_json([{"x": 1}], "d.t")
    _, kwargs = parent.call_args
    cfg = kwargs["job_config"]
    assert isinstance(cfg, bigquery.LoadJobConfig)
    assert cfg.labels["sdk"] == "bigquery-agent-analytics"

  def test_survives_run_in_executor_dispatch(self):
    # Core correctness guard for issue #52: instance attributes are
    # thread-safe for reads, so labels set at construction time must be
    # visible inside a ThreadPoolExecutor worker.
    client = _make_test_client(surface="cli")

    async def run():
      loop = asyncio.get_running_loop()
      with mock.patch.object(bigquery.Client, "query") as parent_query:
        await loop.run_in_executor(None, lambda: client.query("SELECT 1"))
        _, kwargs = parent_query.call_args
      return kwargs["job_config"].labels["sdk_surface"]

    surface = asyncio.run(run())
    assert surface == "cli"


class TestEndToEndFlow:
  """Integration: with_sdk_labels + LabeledBigQueryClient compose correctly."""

  def test_full_label_set_after_dispatch(self):
    client = _make_test_client(surface="cli")
    cfg = with_sdk_labels(bigquery.QueryJobConfig(), feature="trace-read")

    with mock.patch.object(bigquery.Client, "query") as parent_query:
      client.query("SELECT 1", job_config=cfg)

    final = parent_query.call_args.kwargs["job_config"].labels
    assert final["sdk"] == "bigquery-agent-analytics"
    assert final["sdk_surface"] == "cli"
    assert final["sdk_feature"] == "trace-read"
    assert "sdk_version" in final

  def test_ai_function_label_flows_through_dispatch(self):
    client = _make_test_client()
    cfg = with_sdk_labels(
        bigquery.QueryJobConfig(),
        feature="eval-llm-judge",
        ai_function="ai-generate",
    )

    with mock.patch.object(bigquery.Client, "query") as parent_query:
      client.query("SELECT 1", job_config=cfg)

    final = parent_query.call_args.kwargs["job_config"].labels
    assert final["sdk_feature"] == "eval-llm-judge"
    assert final["sdk_ai_function"] == "ai-generate"

  def test_client_does_not_warn_on_sdk_authored_feature_label(self, caplog):
    # When with_sdk_labels sets sdk_feature, the client dispatch must NOT
    # treat sdk_feature as a user-set reserved-key conflict. The client
    # only warns on sdk/sdk_version/sdk_surface — the default keys.
    client = _make_test_client()
    cfg = with_sdk_labels(bigquery.QueryJobConfig(), feature="trace-read")

    with (
        caplog.at_level(logging.WARNING),
        mock.patch.object(bigquery.Client, "query"),
    ):
      client.query("SELECT 1", job_config=cfg)

    assert not any(
        "reserved SDK label keys" in r.message for r in caplog.records
    )


class TestVersionLabel:
  """PR #23 review: sdk_version must always satisfy BigQuery label rules.

  Replacing `.` with `-` is insufficient for PEP 440 versions that include
  local metadata (e.g. `1.2.3+gabc`) or epoch markers — the `+` or `!`
  violates `[a-z0-9_-]`. `_read_version` must normalize-then-validate and
  fall back to a safe constant.
  """

  def test_module_version_matches_bq_label_format(self):
    from bigquery_agent_analytics import _telemetry

    assert _telemetry._LABEL_VALUE_RE.fullmatch(
        _telemetry._VERSION
    ), f"_VERSION={_telemetry._VERSION!r} would be rejected by BigQuery"

  def test_read_version_sanitizes_local_metadata(self):
    from bigquery_agent_analytics import _telemetry

    with mock.patch.object(
        _telemetry.metadata, "version", return_value="1.2.3+gabc.def"
    ):
      got = _telemetry._read_version()
    assert _telemetry._LABEL_VALUE_RE.fullmatch(got)
    assert "+" not in got
    assert "." not in got

  def test_read_version_falls_back_when_package_missing(self):
    from bigquery_agent_analytics import _telemetry

    with mock.patch.object(
        _telemetry.metadata,
        "version",
        side_effect=_telemetry.metadata.PackageNotFoundError,
    ):
      got = _telemetry._read_version()
    assert got == "unknown"

  def test_read_version_falls_back_when_sanitization_fails(self):
    # Pathological input that normalizes to empty — falls back safely.
    from bigquery_agent_analytics import _telemetry

    with mock.patch.object(_telemetry.metadata, "version", return_value=""):
      got = _telemetry._read_version()
    assert _telemetry._LABEL_VALUE_RE.fullmatch(got)


class TestMakeBqClient:
  """Tests for the make_bq_client factory."""

  def test_returns_labeled_client(self):
    client = make_bq_client("test-project", credentials=AnonymousCredentials())
    assert isinstance(client, LabeledBigQueryClient)

  def test_passes_surface(self):
    client = make_bq_client(
        "test-project",
        credentials=AnonymousCredentials(),
        sdk_surface="remote-function",
    )
    assert client._sdk_surface == "remote-function"

  def test_default_surface_is_python(self):
    client = make_bq_client("test-project", credentials=AnonymousCredentials())
    assert client._sdk_surface == "python"

  def test_passes_location(self):
    client = make_bq_client(
        "test-project",
        location="US",
        credentials=AnonymousCredentials(),
    )
    assert client.location == "US"
