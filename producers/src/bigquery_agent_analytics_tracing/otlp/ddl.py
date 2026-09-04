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

"""Generate BigQuery DDL from the OTel-native schema package (issue #316, PR 5).

PR 1 defines the tables as ``SchemaField`` lists (for the BigQuery client); the
deployment path needs ``CREATE TABLE`` / view DDL. This module bridges the two
so ``deploy/otlp_receiver/setup.sh`` (and the bootstrap of #324) create exactly
the schema the writer expects — single source of truth, no hand-kept DDL.

Pure string generation with a local field factory, so it needs neither
``google-cloud-bigquery`` nor a live project and is fully unit-testable.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dc_field
from typing import Any

from bigquery_agent_analytics_tracing.otlp import projection
from bigquery_agent_analytics_tracing.otlp import schema as otel_schema
from bigquery_agent_analytics_tracing.otlp import sql as otel_sql

# Legacy SchemaField type -> BigQuery DDL (GoogleSQL) type.
_DDL_TYPE = {
    "INTEGER": "INT64",
    "FLOAT": "FLOAT64",
    "BOOLEAN": "BOOL",
    "STRING": "STRING",
    "TIMESTAMP": "TIMESTAMP",
    "JSON": "JSON",
    "NUMERIC": "NUMERIC",
    "DATE": "DATE",
    "BYTES": "BYTES",
}


@dataclass
class _Field:
  name: str
  field_type: str
  mode: str = "NULLABLE"
  fields: tuple[Any, ...] = dc_field(default_factory=tuple)


class _BQ:
  """Minimal ``google.cloud.bigquery`` SchemaField factory."""

  def SchemaField(  # noqa: N802 - mirrors the BigQuery API.
      self, name, field_type, mode="NULLABLE", fields=()
  ) -> _Field:
    return _Field(name, field_type, mode, tuple(fields))


def _base_type(field: _Field) -> str:
  if field.field_type == "RECORD":
    inner = ", ".join(f"{f.name} {_type_ddl(f)}" for f in field.fields)
    return f"STRUCT<{inner}>"
  return _DDL_TYPE.get(field.field_type, field.field_type)


def _type_ddl(field: _Field) -> str:
  base = _base_type(field)
  return f"ARRAY<{base}>" if field.mode == "REPEATED" else base


def _column_ddl(field: _Field) -> str:
  ddl = f"{field.name} {_type_ddl(field)}"
  if field.mode == "REQUIRED":
    ddl += " NOT NULL"
  return ddl


def _labels_ddl(labels: dict[str, str]) -> str:
  pairs = ", ".join(f'("{k}", "{v}")' for k, v in sorted(labels.items()))
  return f"labels=[{pairs}]"


def _table_ddl(
    table: str,
    fields: list[_Field],
    *,
    dataset: str,
    partition_field: str,
    cluster_fields: tuple[str, ...],
    labels: dict[str, str],
) -> str:
  cols = ",\n  ".join(_column_ddl(f) for f in fields)
  ddl = f"CREATE TABLE IF NOT EXISTS `{dataset}.{table}` (\n  {cols}\n)\n"
  ddl += f"PARTITION BY DATE({partition_field})\n"
  if cluster_fields:
    ddl += f"CLUSTER BY {', '.join(cluster_fields)}\n"
  ddl += f"OPTIONS({_labels_ddl(labels)});"
  return ddl


def create_table_sql(table_name: str, dataset: str = "${dataset}") -> str:
  """``CREATE TABLE`` DDL for one native table (partition + cluster + labels)."""
  schema_fn, partition_field, cluster_fields = otel_schema.NATIVE_TABLES[
      table_name
  ]
  return _table_ddl(
      table_name,
      schema_fn(_BQ()),
      dataset=dataset,
      partition_field=partition_field,
      cluster_fields=cluster_fields,
      labels=otel_schema.table_labels(table_name),
  )


def agent_events_otlp_table_sql(dataset: str = "${dataset}") -> str:
  """``CREATE TABLE`` DDL for the curated ``agent_events_otlp`` projection."""
  return _table_ddl(
      "agent_events_otlp",
      projection.agent_events_otlp_columns(_BQ()),
      dataset=dataset,
      partition_field="timestamp",
      cluster_fields=("source_product", "event_type"),
      labels={
          "bqaa_table": "agent_events_otlp",
          "otel_schema_version": otel_schema.OTEL_SCHEMA_VERSION,
      },
  )


def create_all_sql(
    dataset: str = "${dataset}", *, enable_spans: bool = False
) -> str:
  """Full DDL bundle: native tables + curated table + dedup/projection views.

  The scheduled ``MERGE`` that populates ``agent_events_otlp`` is *not* part of
  the create bundle (it is registered as a scheduled query); see
  ``sql.agent_events_otlp_merge_sql``.
  """
  statements: list[str] = []
  for table in otel_schema.NATIVE_TABLES:
    if table == "otel_spans" and not enable_spans:
      continue
    statements.append(create_table_sql(table, dataset))
  statements.append(agent_events_otlp_table_sql(dataset))
  for table in projection.DEDUP_TABLES:
    if table == "otel_spans" and not enable_spans:
      continue
    statements.append(projection.dedup_view_sql(table, dataset))
  statements.append(otel_sql.bqaa_metrics_view_sql(dataset))
  return "\n\n".join(statements) + "\n"
