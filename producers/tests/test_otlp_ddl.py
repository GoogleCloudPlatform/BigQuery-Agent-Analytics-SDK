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

"""Tests for the OTel-native BigQuery DDL generator (issue #316, PR 5)."""

from __future__ import annotations

from bigquery_agent_analytics_tracing.otlp import ddl


def test_otel_logs_ddl_has_partition_cluster_labels_and_types():
  sql = ddl.create_table_sql("otel_logs", dataset="ds")
  assert "CREATE TABLE IF NOT EXISTS `ds.otel_logs` (" in sql
  assert "PARTITION BY DATE(timestamp)" in sql
  assert "CLUSTER BY service_name, severity_number, trace_id" in sql
  assert '("bqaa_table", "otel_logs")' in sql
  assert '("otel_schema_version", "1")' in sql
  # type translation + required + JSON
  assert "timestamp TIMESTAMP NOT NULL" in sql
  assert "severity_number INT64" in sql
  assert "body JSON" in sql


def test_required_source_position_struct_is_not_null():
  sql = ddl.create_table_sql("otel_logs", dataset="ds")
  assert "source_position STRUCT<" in sql
  assert "raw_otlp_request_hash STRING" in sql
  # PR-1 fix: dedup identity is REQUIRED on analytics tables.
  assert "NOT NULL" in sql.split("source_position STRUCT<", 1)[1][:200]
  assert "idempotency_key STRING NOT NULL" in sql


def test_metric_sum_ddl_translates_bool_and_array_struct_exemplars():
  sql = ddl.create_table_sql("otel_metric_sum", dataset="ds")
  assert "value FLOAT64" in sql
  assert "is_monotonic BOOL" in sql
  assert "aggregation_temporality INT64" in sql
  # exemplars: ARRAY<STRUCT<...>> with trace/span join keys
  assert "exemplars ARRAY<STRUCT<" in sql
  assert "span_id STRING" in sql
  assert "trace_id STRING" in sql


def test_histogram_ddl_uses_repeated_scalar_arrays():
  sql = ddl.create_table_sql("otel_metric_histogram", dataset="ds")
  assert "bucket_counts ARRAY<INT64>" in sql
  assert "explicit_bounds ARRAY<FLOAT64>" in sql


def test_exponential_histogram_ddl_nests_bucket_struct():
  sql = ddl.create_table_sql("otel_metric_exponential_histogram", dataset="ds")
  assert "positive STRUCT<offset INT64, bucket_counts ARRAY<INT64>>" in sql


def test_dead_letter_keeps_identity_nullable():
  sql = ddl.create_table_sql("otlp_dead_letter", dataset="ds")
  assert "idempotency_key STRING" in sql
  assert "idempotency_key STRING NOT NULL" not in sql


def test_agent_events_otlp_curated_table_ddl():
  sql = ddl.agent_events_otlp_table_sql(dataset="ds")
  assert "CREATE TABLE IF NOT EXISTS `ds.agent_events_otlp` (" in sql
  assert "content JSON" in sql
  assert "content_parts ARRAY<STRUCT<" in sql
  assert "PARTITION BY DATE(timestamp)" in sql
  assert "CLUSTER BY source_product, event_type" in sql


def test_create_all_bundles_tables_views_and_gates_spans():
  sql = ddl.create_all_sql(dataset="ds")
  # native analytics tables + curated + views, spans excluded by default
  for table in (
      "otel_logs",
      "otel_metric_sum",
      "otel_metric_summary",
      "otlp_dead_letter",
      "agent_events_otlp",
  ):
    assert f"`ds.{table}`" in sql
  assert "otel_spans" not in sql
  assert "`ds.otel_logs_dedup`" in sql
  assert "`ds.bqaa_metrics`" in sql


def test_create_all_includes_spans_when_enabled():
  sql = ddl.create_all_sql(dataset="ds", enable_spans=True)
  assert "CREATE TABLE IF NOT EXISTS `ds.otel_spans` (" in sql
  assert "`ds.otel_spans_dedup`" in sql
