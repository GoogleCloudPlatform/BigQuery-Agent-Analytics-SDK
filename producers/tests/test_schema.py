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

"""Tests for the canonical BQAA ``agent_events`` schema.

Uses fakes that match the ``google.cloud.bigquery`` SchemaField shape so we
do not require ``google-cloud-bigquery`` to be importable for unit testing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from bigquery_agent_analytics_tracing.schema import bq_schema


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


EXPECTED_TOP_LEVEL = [
    ("timestamp", "TIMESTAMP", "REQUIRED"),
    ("event_type", "STRING", "NULLABLE"),
    ("agent", "STRING", "NULLABLE"),
    ("session_id", "STRING", "NULLABLE"),
    ("invocation_id", "STRING", "NULLABLE"),
    ("user_id", "STRING", "NULLABLE"),
    ("trace_id", "STRING", "NULLABLE"),
    ("span_id", "STRING", "NULLABLE"),
    ("parent_span_id", "STRING", "NULLABLE"),
    ("content", "JSON", "NULLABLE"),
    ("content_parts", "RECORD", "REPEATED"),
    ("attributes", "JSON", "NULLABLE"),
    ("latency_ms", "JSON", "NULLABLE"),
    ("status", "STRING", "NULLABLE"),
    ("error_message", "STRING", "NULLABLE"),
    ("is_truncated", "BOOLEAN", "NULLABLE"),
]


def test_top_level_columns_match_adk_bqaa_contract():
  schema = bq_schema(_FakeBigQueryModule())

  actual = [(f.name, f.field_type, f.mode) for f in schema]
  assert actual == EXPECTED_TOP_LEVEL


def test_content_parts_record_shape():
  schema = bq_schema(_FakeBigQueryModule())
  parts = next(f for f in schema if f.name == "content_parts")
  assert parts.mode == "REPEATED"

  part_fields = {f.name: f for f in parts.fields}
  assert part_fields["mime_type"].field_type == "STRING"
  assert part_fields["uri"].field_type == "STRING"
  assert part_fields["text"].field_type == "STRING"
  assert part_fields["part_index"].field_type == "INTEGER"
  assert part_fields["part_attributes"].field_type == "STRING"
  assert part_fields["storage_mode"].field_type == "STRING"

  object_ref = part_fields["object_ref"]
  assert object_ref.field_type == "RECORD"
  ref_fields = {f.name: f.field_type for f in object_ref.fields}
  assert ref_fields == {
      "uri": "STRING",
      "version": "STRING",
      "authorizer": "STRING",
      "details": "JSON",
  }


def test_timestamp_is_required():
  schema = bq_schema(_FakeBigQueryModule())
  ts = next(f for f in schema if f.name == "timestamp")
  assert ts.field_type == "TIMESTAMP"
  assert ts.mode == "REQUIRED"
