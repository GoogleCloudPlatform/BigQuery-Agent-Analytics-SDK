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

"""Tests for the CategoricalViewManager and dashboard view generation."""

from unittest import mock

import pytest

from bigquery_agent_analytics.categorical_views import _CATEGORICAL_VIEW_DEFS
from bigquery_agent_analytics.categorical_views import _VIEW_CREATION_ORDER
from bigquery_agent_analytics.categorical_views import CategoricalViewManager

PROJECT = "test-project"
DATASET = "analytics"


@pytest.fixture
def vm():
  return CategoricalViewManager(
      project_id=PROJECT,
      dataset_id=DATASET,
      bq_client=mock.MagicMock(),
  )


class TestCategoricalViewManager:

  def test_available_views(self, vm):
    views = vm.available_views()
    assert "categorical_results_latest" in views
    assert "categorical_daily_counts" in views
    assert "categorical_hourly_counts" in views
    assert "categorical_operational_metrics" in views
    assert len(views) == len(_CATEGORICAL_VIEW_DEFS)

  def test_available_views_order_matches_creation_order(self, vm):
    assert vm.available_views() == _VIEW_CREATION_ORDER

  def test_get_view_sql_base_dedup(self, vm):
    sql = vm.get_view_sql("categorical_results_latest")
    assert "CREATE OR REPLACE VIEW" in sql
    assert f"`{PROJECT}.{DATASET}." in sql
    assert "ROW_NUMBER()" in sql
    assert "PARTITION BY session_id, metric_name" in sql
    assert "COALESCE(prompt_version, '')" in sql
    assert "ORDER BY created_at DESC, raw_response DESC" in sql
    assert "categorical_results`" in sql

  def test_get_view_sql_daily_counts(self, vm):
    sql = vm.get_view_sql("categorical_daily_counts")
    assert "CREATE OR REPLACE VIEW" in sql
    assert "DATE(created_at) AS eval_date" in sql
    assert "metric_name" in sql
    assert "category" in sql
    assert "execution_mode" in sql
    assert "COUNT(*) AS session_count" in sql
    # References the base dedup view, not the raw table
    assert "categorical_results_latest" in sql

  def test_get_view_sql_hourly_counts(self, vm):
    sql = vm.get_view_sql("categorical_hourly_counts")
    assert "CREATE OR REPLACE VIEW" in sql
    assert "TIMESTAMP_TRUNC(created_at, HOUR) AS eval_hour" in sql
    assert "metric_name" in sql
    assert "category" in sql
    assert "COUNT(*) AS session_count" in sql
    assert "categorical_results_latest" in sql

  def test_get_view_sql_operational_metrics(self, vm):
    sql = vm.get_view_sql("categorical_operational_metrics")
    assert "CREATE OR REPLACE VIEW" in sql
    assert "parse_error" in sql
    assert "passed_validation" in sql
    assert "SAFE_DIVIDE" in sql
    assert "parse_error_rate" in sql
    assert "validation_failures" in sql
    assert "fallback_count" in sql
    assert "fallback_rate" in sql
    assert "categorical_results_latest" in sql

  def test_get_view_sql_unknown_raises(self, vm):
    with pytest.raises(KeyError, match="Unknown view"):
      vm.get_view_sql("nonexistent_view")

  def test_create_view_executes_sql(self, vm):
    vm.create_view("categorical_results_latest")
    vm.bq_client.query.assert_called_once()
    sql = vm.bq_client.query.call_args[0][0]
    assert "categorical_results_latest" in sql
    vm.bq_client.query.return_value.result.assert_called_once()

  def test_create_view_labels_with_eval_categorical_feature(self, vm):
    vm.create_view("categorical_daily_counts")
    job_config = vm.bq_client.query.call_args.kwargs.get("job_config")
    assert job_config is not None
    assert (
        dict(job_config.labels or {}).get("sdk_feature") == "eval-categorical"
    )

  def test_vanilla_client_emits_warn_once(self, caplog):
    # PR #25 review: mirror Phase 1 warn-once behavior.
    import logging

    from google.auth.credentials import AnonymousCredentials
    from google.cloud import bigquery

    vanilla = bigquery.Client(
        project=PROJECT, credentials=AnonymousCredentials()
    )
    vm = CategoricalViewManager(
        project_id=PROJECT, dataset_id=DATASET, bq_client=vanilla
    )
    with caplog.at_level(logging.WARNING):
      _ = vm.bq_client
      _ = vm.bq_client
    warnings = [
        r
        for r in caplog.records
        if "SDK telemetry labels will not be applied" in r.message
    ]
    assert len(warnings) == 1

  def test_create_all_views(self, vm):
    created = vm.create_all_views()
    assert len(created) == len(_CATEGORICAL_VIEW_DEFS)
    assert vm.bq_client.query.call_count == len(_CATEGORICAL_VIEW_DEFS)

  def test_create_all_views_returns_prefixed_names(self, vm):
    created = vm.create_all_views()
    for view_name, prefixed in created.items():
      assert prefixed == view_name  # no prefix by default

  def test_create_all_views_handles_errors(self, vm):
    vm.bq_client.query.side_effect = Exception("BQ error")
    created = vm.create_all_views()
    assert len(created) == 0

  def test_custom_prefix(self):
    vm = CategoricalViewManager(
        project_id=PROJECT,
        dataset_id=DATASET,
        view_prefix="adk_",
        bq_client=mock.MagicMock(),
    )
    sql = vm.get_view_sql("categorical_results_latest")
    assert "adk_categorical_results_latest" in sql

    sql_daily = vm.get_view_sql("categorical_daily_counts")
    assert "adk_categorical_daily_counts" in sql_daily
    # Downstream views reference the prefixed base view
    assert "adk_categorical_results_latest" in sql_daily

  def test_custom_prefix_in_create_all(self):
    vm = CategoricalViewManager(
        project_id=PROJECT,
        dataset_id=DATASET,
        view_prefix="adk_",
        bq_client=mock.MagicMock(),
    )
    created = vm.create_all_views()
    for view_name, prefixed in created.items():
      assert prefixed == f"adk_{view_name}"

  def test_custom_results_table(self):
    vm = CategoricalViewManager(
        project_id=PROJECT,
        dataset_id=DATASET,
        results_table="my_custom_results",
        bq_client=mock.MagicMock(),
    )
    sql = vm.get_view_sql("categorical_results_latest")
    assert "my_custom_results" in sql
    # Should NOT reference the default table
    assert "categorical_results`" not in sql

  def test_all_views_produce_valid_sql(self, vm):
    """Every defined view produces SQL without errors."""
    for view_name in _CATEGORICAL_VIEW_DEFS:
      sql = vm.get_view_sql(view_name)
      assert "CREATE OR REPLACE VIEW" in sql
      assert f"`{PROJECT}.{DATASET}." in sql

  def test_downstream_views_read_from_base(self, vm):
    """All non-base views query the dedup base, not the raw table."""
    for view_name in _VIEW_CREATION_ORDER[1:]:
      sql = vm.get_view_sql(view_name)
      assert "categorical_results_latest" in sql

  def test_base_view_dedup_excludes_rn(self, vm):
    """The base view uses SELECT * EXCEPT(_rn) to hide the helper column."""
    sql = vm.get_view_sql("categorical_results_latest")
    assert "EXCEPT(_rn)" in sql
    assert "_rn = 1" in sql

  def test_operational_metrics_excludes_parse_errors_from_validation(self, vm):
    """validation_failures should exclude parse_error rows."""
    sql = vm.get_view_sql("categorical_operational_metrics")
    assert "NOT passed_validation AND NOT parse_error" in sql

  def test_location_passed_to_lazy_client(self):
    """When no bq_client is given, the lazy client uses the location."""
    vm = CategoricalViewManager(
        project_id=PROJECT,
        dataset_id=DATASET,
        location="EU",
    )
    assert vm.location == "EU"

    with mock.patch(
        "bigquery_agent_analytics.categorical_views.make_bq_client"
    ) as mock_factory:
      mock_factory.return_value = mock.MagicMock()
      _ = vm.bq_client
      mock_factory.assert_called_once_with(PROJECT, location="EU")

  def test_no_location_omits_kwarg(self):
    """When location is None, the lazy client passes location=None."""
    vm = CategoricalViewManager(
        project_id=PROJECT,
        dataset_id=DATASET,
    )

    with mock.patch(
        "bigquery_agent_analytics.categorical_views.make_bq_client"
    ) as mock_factory:
      mock_factory.return_value = mock.MagicMock()
      _ = vm.bq_client
      # make_bq_client treats location=None as "no location"; passes it
      # explicitly so the factory owns the decision.
      mock_factory.assert_called_once_with(PROJECT, location=None)
