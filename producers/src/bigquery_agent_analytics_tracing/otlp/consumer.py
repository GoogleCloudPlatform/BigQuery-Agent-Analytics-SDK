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

"""Pub/Sub -> BigQuery consumer for envelope v1 (issue #316, PR 4).

Deployable glue: a Pub/Sub subscriber whose callback runs each message through
``writer.handle_message`` into BigQuery. The append-only BigQuery client and the
Pub/Sub client are lazy-imported (``receiver`` / core extras); the per-message
callback is pure and unit-tested. ``ack`` on success, ``nack`` on failure so the
message redelivers (and ultimately hits the subscription's dead-letter policy).
"""

from __future__ import annotations

from typing import Any

from bigquery_agent_analytics_tracing.otlp import writer


class BigQueryAppendWriter:
  """``writer.BigQueryWriter`` backed by ``insert_rows_json`` (append-only)."""

  def __init__(self, project: str, dataset: str) -> None:
    from google.cloud import bigquery  # noqa: PLC0415 - optional dependency

    self._client = bigquery.Client(project=project)
    self._project = project
    self._dataset = dataset

  def append(self, table: str, row: dict[str, Any]) -> None:
    table_id = f"{self._project}.{self._dataset}.{table}"
    errors = self._client.insert_rows_json(table_id, [row])
    if errors:
      raise writer.TableWriteError(f"insert into {table} failed: {errors}")


def make_callback(
    bq_writer: writer.BigQueryWriter, *, enable_spans: bool = False
):
  """Build a Pub/Sub message callback. Pure — testable with a fake message."""

  def callback(message: Any) -> None:
    try:
      writer.handle_message(message.data, bq_writer, enable_spans=enable_spans)
      message.ack()
    except Exception:  # noqa: BLE001 - nack -> redelivery / subscription DLQ
      message.nack()

  return callback


def run_subscriber(
    subscription: str,
    bq_writer: writer.BigQueryWriter,
    *,
    enable_spans: bool = False,
) -> None:  # pragma: no cover - deployment glue
  """Block on a Pub/Sub streaming pull, landing messages into BigQuery."""
  from google.cloud import pubsub_v1  # noqa: PLC0415 - optional dependency

  client = pubsub_v1.SubscriberClient()
  future = client.subscribe(
      subscription, callback=make_callback(bq_writer, enable_spans=enable_spans)
  )
  future.result()
