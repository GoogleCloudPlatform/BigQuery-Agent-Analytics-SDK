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

"""Tests for the OTel-native OTLP receiver schema package (issue #316, PR 1).

Uses fakes matching the ``google.cloud.bigquery`` SchemaField shape so the
suite never needs ``google-cloud-bigquery`` installed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from bigquery_agent_analytics_tracing.otlp import projection
from bigquery_agent_analytics_tracing.otlp import schema


@dataclass
class _FakeSchemaField:
  name: str
  field_type: str
  mode: str = "NULLABLE"
  fields: tuple[Any, ...] = ()


class _FakeBigQueryModule:

  def SchemaField(  # noqa: N802 — mirrors google.cloud.bigquery API.
      self,
      name: str,
      field_type: str,
      mode: str = "NULLABLE",
      fields=(),
  ) -> _FakeSchemaField:
    return _FakeSchemaField(
        name=name, field_type=field_type, mode=mode, fields=tuple(fields)
    )


BQ = _FakeBigQueryModule()


def _by_name(fields: list[Any]) -> dict[str, Any]:
  return {f.name: f for f in fields}


# --------------------------------------------------------------------------
# Table registry
# --------------------------------------------------------------------------


def test_native_tables_are_the_expected_eight():
  assert set(schema.NATIVE_TABLES) == {
      "otel_logs",
      "otel_metric_sum",
      "otel_metric_gauge",
      "otel_metric_histogram",
      "otel_metric_exponential_histogram",
      "otel_metric_summary",
      "otel_spans",
      "otlp_dead_letter",
  }


def test_metrics_are_split_into_five_per_type_tables():
  assert schema.METRIC_TABLES == (
      "otel_metric_sum",
      "otel_metric_gauge",
      "otel_metric_histogram",
      "otel_metric_exponential_histogram",
      "otel_metric_summary",
  )


# --------------------------------------------------------------------------
# Receiver metadata + source_position (idempotency)
# --------------------------------------------------------------------------


def test_every_native_table_carries_source_position_and_idempotency_key():
  for name, (schema_fn, _, _) in schema.NATIVE_TABLES.items():
    fields = _by_name(schema_fn(BQ))
    assert "idempotency_key" in fields, name
    assert "source_position" in fields, name
    sp = fields["source_position"]
    assert sp.field_type == "RECORD", name
    sp_fields = {f.name for f in sp.fields}
    assert "raw_otlp_request_hash" in sp_fields, name
    assert {"resource_index", "scope_index", "record_index"} <= sp_fields, name


def test_native_analytics_tables_require_dedup_identity_fields():
  # idempotency_key/source_position/ingest_time/otel_schema_version are REQUIRED
  # on analytics tables: the *_dedup views partition on idempotency_key, so a
  # null key would collapse unrelated rows.
  for name in schema.NATIVE_TABLES:
    if name == "otlp_dead_letter":
      continue
    fields = _by_name(schema.NATIVE_TABLES[name][0](BQ))
    for col in (
        "idempotency_key",
        "source_position",
        "ingest_time",
        "otel_schema_version",
        "source_product",
        "source_signal",
    ):
      assert fields[col].mode == "REQUIRED", (name, col)


def test_dead_letter_keeps_identity_nullable_for_partial_failures():
  # A whole-request decode failure may lack idempotency_key / source_position.
  fields = _by_name(schema.otlp_dead_letter_schema(BQ))
  assert fields["idempotency_key"].mode == "NULLABLE"
  assert fields["source_position"].mode == "NULLABLE"


def test_otel_schema_version_is_stamped_on_rows_and_labels():
  logs = _by_name(schema.otel_logs_schema(BQ))
  assert "otel_schema_version" in logs
  labels = schema.table_labels("otel_logs")
  assert labels["otel_schema_version"] == schema.OTEL_SCHEMA_VERSION
  assert labels["bqaa_table"] == "otel_logs"


# --------------------------------------------------------------------------
# Logs
# --------------------------------------------------------------------------


def test_otel_logs_promotes_identity_and_keeps_attribute_bags_as_json():
  fields = _by_name(schema.otel_logs_schema(BQ))
  # promoted scalar identity (clusterable)
  assert fields["service_name"].field_type == "STRING"
  assert fields["severity_number"].field_type == "INTEGER"
  assert fields["trace_id"].field_type == "STRING"
  assert fields["timestamp"].mode == "REQUIRED"
  # open attribute bags stay JSON
  for bag in (
      "body",
      "resource_attributes",
      "scope_attributes",
      "log_attributes",
  ):
    assert fields[bag].field_type == "JSON", bag


# --------------------------------------------------------------------------
# Metrics — per-type tails
# --------------------------------------------------------------------------


def test_sum_has_temporality_and_monotonicity_gauge_does_not():
  sum_f = _by_name(schema.otel_metric_sum_schema(BQ))
  gauge_f = _by_name(schema.otel_metric_gauge_schema(BQ))
  assert sum_f["value"].field_type == "FLOAT"
  assert sum_f["aggregation_temporality"].field_type == "INTEGER"
  assert sum_f["is_monotonic"].field_type == "BOOLEAN"
  assert "aggregation_temporality" not in gauge_f
  assert "is_monotonic" not in gauge_f


def test_histogram_buckets_are_struct_of_arrays():
  h = _by_name(schema.otel_metric_histogram_schema(BQ))
  assert h["bucket_counts"].field_type == "INTEGER"
  assert h["bucket_counts"].mode == "REPEATED"
  assert h["explicit_bounds"].field_type == "FLOAT"
  assert h["explicit_bounds"].mode == "REPEATED"


def test_exponential_histogram_has_scale_zero_threshold_and_pos_neg_buckets():
  e = _by_name(schema.otel_metric_exponential_histogram_schema(BQ))
  assert e["scale"].field_type == "INTEGER"
  assert e["zero_count"].field_type == "INTEGER"
  assert e["zero_threshold"].field_type == "FLOAT"
  for side in ("positive", "negative"):
    rec = e[side]
    assert rec.field_type == "RECORD"
    sub = {f.name: f for f in rec.fields}
    assert sub["offset"].field_type == "INTEGER"
    assert sub["bucket_counts"].mode == "REPEATED"


def test_summary_has_quantiles_and_no_exemplars_or_temporality():
  s = _by_name(schema.otel_metric_summary_schema(BQ))
  q = s["quantile_values"]
  assert q.field_type == "RECORD"
  assert q.mode == "REPEATED"
  assert {f.name for f in q.fields} == {"quantile", "value"}
  # Per the OTLP proto, Summary has neither exemplars nor temporality.
  assert "exemplars" not in s
  assert "aggregation_temporality" not in s


def test_exemplars_carry_trace_and_span_ids_on_applicable_metric_tables():
  for table_fn in (
      schema.otel_metric_sum_schema,
      schema.otel_metric_gauge_schema,
      schema.otel_metric_histogram_schema,
      schema.otel_metric_exponential_histogram_schema,
  ):
    ex = _by_name(table_fn(BQ))["exemplars"]
    assert ex.mode == "REPEATED"
    sub = {f.name for f in ex.fields}
    assert {"span_id", "trace_id", "value"} <= sub


# --------------------------------------------------------------------------
# Spans
# --------------------------------------------------------------------------


def test_spans_store_events_and_links_as_repeated_records():
  fields = _by_name(schema.otel_spans_schema(BQ))
  for nested in ("events", "links"):
    rec = fields[nested]
    assert rec.field_type == "RECORD"
    assert rec.mode == "REPEATED"


# --------------------------------------------------------------------------
# Partitioning / clustering keys
# --------------------------------------------------------------------------


def test_clustering_keys_are_real_scalar_top_level_columns():
  # BigQuery cannot cluster on JSON or repeated columns; every clustering key
  # must be a top-level scalar column that exists in the table.
  allowed = {"STRING", "INTEGER", "BOOLEAN", "TIMESTAMP", "NUMERIC", "DATE"}
  for name, (
      schema_fn,
      partition_field,
      cluster_fields,
  ) in schema.NATIVE_TABLES.items():
    fields = _by_name(schema_fn(BQ))
    assert partition_field in fields, (name, partition_field)
    assert fields[partition_field].field_type == "TIMESTAMP", name
    for col in cluster_fields:
      assert col in fields, (name, col)
      assert fields[col].field_type in allowed, (name, col)
      assert fields[col].mode != "REPEATED", (name, col)


# --------------------------------------------------------------------------
# Dedup views
# --------------------------------------------------------------------------


def test_dedup_tables_exclude_dead_letter():
  assert "otlp_dead_letter" not in projection.DEDUP_TABLES
  assert "otel_logs" in projection.DEDUP_TABLES


def test_dedup_view_sql_dedupes_on_idempotency_key():
  sql = projection.dedup_view_sql("otel_logs", dataset="ds")
  assert "CREATE OR REPLACE VIEW `ds.otel_logs_dedup`" in sql
  assert "FROM `ds.otel_logs`" in sql
  assert "ROW_NUMBER() OVER (" in sql
  # Newest-write-wins, matching docs/otlp_receiver_design.md (so a replayed or
  # repaired row supersedes the older one).
  assert "PARTITION BY idempotency_key ORDER BY ingest_time DESC" in sql


def test_dedup_view_sql_rejects_dead_letter():
  with pytest.raises(ValueError):
    projection.dedup_view_sql("otlp_dead_letter")


# --------------------------------------------------------------------------
# Schema parity — agent_events_otlp projection ⊇ agent_events
# --------------------------------------------------------------------------


def test_projection_is_a_faithful_superset_of_agent_events():
  # Every canonical agent_events column (name/type/mode) is reproduced by the
  # agent_events_otlp projection. If agent_events grows a column, this fails
  # until the projection adds it.
  assert projection.missing_agent_events_columns(BQ) == []


def test_projection_adds_provenance_beyond_agent_events():
  proj = {f.name for f in projection.agent_events_otlp_columns(BQ)}
  assert {
      "source_product",
      "source_signal",
      "source_event_name",
      "crosswalk_version",
  } <= proj


def test_parity_check_is_recursive_into_nested_records():
  # The parity signature includes nested record paths, so drift *inside*
  # content_parts / object_ref is caught — not only top-level columns.
  flat = set(projection._flatten(projection.agent_events_otlp_columns(BQ)))
  assert "content_parts:RECORD:REPEATED" in flat
  assert "content_parts.object_ref:RECORD:NULLABLE" in flat
  assert "content_parts.object_ref.details:JSON:NULLABLE" in flat


def test_parity_fails_when_a_nested_agent_events_field_is_unmatched(
    monkeypatch,
):
  # Simulate agent_events growing a nested field the projection lacks: the
  # parity check must report it (proves the recursion actually guards drift).
  real = projection.agent_events_otlp_columns

  def _drop_object_ref(bigquery):
    fields = real(bigquery)
    return [f for f in fields if f.name != "content_parts"] + [
        bigquery.SchemaField("content_parts", "RECORD", mode="REPEATED")
    ]

  monkeypatch.setattr(projection, "agent_events_otlp_columns", _drop_object_ref)
  missing = projection.missing_agent_events_columns(BQ)
  assert any("content_parts.object_ref" in sig for sig in missing)
