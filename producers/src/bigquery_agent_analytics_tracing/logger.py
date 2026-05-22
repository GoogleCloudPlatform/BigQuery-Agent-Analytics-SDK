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

"""Builds rows in the canonical BQAA ``agent_events`` shape and routes them
to one of three sinks: dry-run log, synchronous direct insert, or async
spool + drainer.

Hook subprocess (hot path):
  1. Build the BQAA row.
  2. Append a JSONL line to ``config.spool_dir`` (sync, ~1 ms).
  3. Spawn a detached drainer subprocess (idempotent via flock).
  4. Exit. The host agent never blocks on BigQuery.

The drainer subprocess (``drain.py``) holds an exclusive flock, batches
spooled rows, writes through the BigQuery Storage Write API when
available, and falls back to ``insert_rows_json`` otherwise.
"""

from __future__ import annotations

from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any
import uuid

from ._utils import hex_id
from ._utils import iso_timestamp
from ._utils import log_to_file
from ._utils import truncate
from ._writer_identity import __version__ as WRITER_VERSION
from ._writer_identity import WRITER_PLUGIN_NAME
from .config import BQAA_EVENT_TYPES
from .config import BQAAConfig
from .schema import bq_schema


def content_part(text: str, index: int) -> dict[str, Any]:
  """Inline-text content part. Multimodal/GCS offload is a follow-up."""
  return {
      "mime_type": "text/plain",
      "uri": None,
      "object_ref": None,
      "text": text,
      "part_index": index,
      "part_attributes": None,
      "storage_mode": "INLINE",
  }


def _serialize_bq_json_fields(row: dict[str, Any]) -> dict[str, Any]:
  """Serialize JSON-typed fields so ``insert_rows_json`` sees strings."""
  bq_row = dict(row)
  for field in ("content", "attributes", "latency_ms"):
    value = bq_row.get(field)
    if value is not None and not isinstance(value, str):
      bq_row[field] = json.dumps(value, sort_keys=True)
  parts = []
  for part in bq_row.get("content_parts") or []:
    next_part = dict(part)
    object_ref = next_part.get("object_ref")
    if isinstance(object_ref, dict):
      details = object_ref.get("details")
      if details is not None and not isinstance(details, str):
        next_object_ref = dict(object_ref)
        next_object_ref["details"] = json.dumps(details, sort_keys=True)
        next_part["object_ref"] = next_object_ref
    # part_attributes is STRING in the schema; serialize structured values
    # so callers passing dicts/lists don't break the insert_rows_json path.
    part_attrs = next_part.get("part_attributes")
    if part_attrs is not None and not isinstance(part_attrs, str):
      next_part["part_attributes"] = json.dumps(part_attrs, sort_keys=True)
    parts.append(next_part)
  bq_row["content_parts"] = parts
  return bq_row


def _ensure_drainer(config: BQAAConfig) -> None:
  """Spawn the drainer in a detached subprocess if none is running.

  Uses a non-blocking flock on a pidfile to dedupe; a running drainer holds
  the lock for its entire lifetime. If we cannot take the lock, a drainer is
  already running and will pick up our spool file.

  Spawned via ``python -m bigquery_agent_analytics_tracing.drain``. For
  vendored plugin installs (no wheel on ``BQAA_PYTHON``), set
  ``PYTHONPATH`` to the vendored package root before the hook fires, or use
  a plugin-side wrapper script that performs the ``sys.path`` insert.
  """
  spool = Path(config.spool_dir).expanduser()
  spool.mkdir(parents=True, exist_ok=True)
  pidfile = spool / ".drainer.pid"
  fd = os.open(str(pidfile), os.O_CREAT | os.O_RDWR, 0o600)
  try:
    try:
      fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
      return  # Another drainer is running.
    fcntl.flock(fd, fcntl.LOCK_UN)
  finally:
    os.close(fd)

  python_bin = os.environ.get("BQAA_PYTHON") or sys.executable or "python3"
  try:
    subprocess.Popen(
        [python_bin, "-m", "bigquery_agent_analytics_tracing.drain"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        close_fds=True,
        env=os.environ.copy(),
    )
  except OSError as exc:
    log_to_file(config, f"DRAINER_SPAWN_FAIL {exc}")


class BigQueryAgentAnalyticsLogger:
  """Builds BQAA rows and routes them to spool / dry-run / sync write.

  Default emit mode is ``spool`` (writes a JSONL line and triggers the
  async drainer subprocess). ``config.dry_run`` writes to
  ``config.log_file`` only. ``config.direct_write`` falls back to a
  synchronous ``insert_rows_json`` call, kept for debugging and parity
  tests.
  """

  def __init__(self, config: BQAAConfig | None = None):
    self.config = config or BQAAConfig.from_env()
    self._client = None
    self._table_ready = False

  def log_event(
      self,
      *,
      event_type: str,
      content: dict[str, Any] | None = None,
      attributes: dict[str, Any] | None = None,
      latency_ms: dict[str, Any] | None = None,
      agent: str | None = None,
      session_id: str | None = None,
      invocation_id: str | None = None,
      user_id: str | None = None,
      trace_id: str | None = None,
      span_id: str | None = None,
      parent_span_id: str | None = None,
      status: str = "OK",
      error_message: str | None = None,
      timestamp: datetime | None = None,
      content_parts: list[dict[str, Any]] | None = None,
      is_truncated: bool = False,
  ) -> dict[str, Any]:
    if not self.config.enabled:
      return {}
    if event_type not in BQAA_EVENT_TYPES:
      raise ValueError(f"Unsupported BQAA event_type: {event_type}")

    clipped_content, content_truncated = truncate(
        content or {}, self.config.max_content_length
    )
    # attributes.writer is reserved for package attribution so adoption
    # queries on attributes.writer.{plugin,version,label,agent,mode} have a
    # stable contract. A caller-supplied "writer" key is moved to
    # attributes.writer_caller rather than silently dropping their data.
    merged_attributes = dict(attributes or {})
    caller_writer = merged_attributes.pop("writer", None)
    merged_attributes["writer"] = {
        "plugin": WRITER_PLUGIN_NAME,
        "version": WRITER_VERSION,
        "label": self.config.writer_label,
        "agent": agent or self.config.agent_name,
        "mode": (
            "dry_run"
            if self.config.dry_run
            else ("direct" if self.config.direct_write else "spool")
        ),
    }
    if caller_writer is not None:
      merged_attributes["writer_caller"] = caller_writer
    clipped_attributes, attr_truncated = truncate(
        merged_attributes, self.config.max_content_length
    )
    clipped_latency, latency_truncated = truncate(latency_ms or {}, 1000)
    clipped_parts, parts_truncated = truncate(
        content_parts or [], self.config.max_content_length
    )

    row = {
        "timestamp": iso_timestamp(timestamp),
        "event_type": event_type,
        "agent": agent or self.config.agent_name,
        "session_id": session_id,
        "invocation_id": invocation_id,
        "user_id": user_id or self.config.user_id,
        "trace_id": trace_id,
        "span_id": span_id or hex_id(16),
        "parent_span_id": parent_span_id,
        "content": clipped_content,
        "content_parts": clipped_parts,
        "attributes": clipped_attributes,
        "latency_ms": clipped_latency,
        "status": status,
        "error_message": error_message,
        "is_truncated": bool(
            is_truncated
            or content_truncated
            or attr_truncated
            or latency_truncated
            or parts_truncated
        ),
    }
    self._emit_row(row)
    return row

  def log_llm_request(
      self,
      *,
      prompt: str,
      session_id: str,
      invocation_id: str,
      trace_id: str,
      span_id: str,
      parent_span_id: str | None = None,
      agent: str | None = None,
      user_id: str | None = None,
      system_prompt: str | None = None,
      attributes: dict[str, Any] | None = None,
  ) -> dict[str, Any]:
    return self.log_event(
        event_type="LLM_REQUEST",
        agent=agent,
        user_id=user_id,
        session_id=session_id,
        invocation_id=invocation_id,
        trace_id=trace_id,
        span_id=span_id,
        parent_span_id=parent_span_id,
        content={
            "system_prompt": system_prompt or "",
            "prompt": [{"role": "user", "content": prompt}],
        },
        content_parts=[content_part(prompt, 0)] if prompt else [],
        attributes=attributes,
    )

  def log_llm_response(
      self,
      *,
      response: str,
      session_id: str,
      invocation_id: str,
      trace_id: str,
      span_id: str,
      parent_span_id: str | None = None,
      agent: str | None = None,
      user_id: str | None = None,
      model: str | None = None,
      usage_metadata: dict[str, int] | None = None,
      total_ms: int | None = None,
      status: str = "OK",
      error_message: str | None = None,
      attributes: dict[str, Any] | None = None,
      is_truncated: bool = False,
  ) -> dict[str, Any]:
    usage = usage_metadata or {}
    attrs = {
        "model": model or "",
        "usage_metadata": {
            "prompt_tokens": int(usage.get("prompt_tokens") or 0),
            "completion_tokens": int(usage.get("completion_tokens") or 0),
            "total_tokens": int(usage.get("total_tokens") or 0),
        },
    }
    if attributes:
      attrs.update(attributes)
    return self.log_event(
        event_type="LLM_RESPONSE",
        agent=agent,
        user_id=user_id,
        session_id=session_id,
        invocation_id=invocation_id,
        trace_id=trace_id,
        span_id=span_id,
        parent_span_id=parent_span_id,
        content={"response": response},
        content_parts=[content_part(response, 0)] if response else [],
        attributes=attrs,
        latency_ms={"total_ms": total_ms} if total_ms is not None else {},
        status=status,
        error_message=error_message,
        is_truncated=is_truncated,
    )

  def log_tool_starting(
      self,
      *,
      tool: str,
      args: dict[str, Any],
      session_id: str,
      invocation_id: str,
      trace_id: str,
      span_id: str,
      parent_span_id: str | None,
      agent: str | None = None,
      tool_origin: str = "LOCAL",
      attributes: dict[str, Any] | None = None,
  ) -> dict[str, Any]:
    return self.log_event(
        event_type="TOOL_STARTING",
        agent=agent,
        session_id=session_id,
        invocation_id=invocation_id,
        trace_id=trace_id,
        span_id=span_id,
        parent_span_id=parent_span_id,
        content={"tool": tool, "args": args, "tool_origin": tool_origin},
        attributes=attributes,
    )

  def log_tool_completed(
      self,
      *,
      tool: str,
      result: Any,
      session_id: str,
      invocation_id: str,
      trace_id: str,
      span_id: str,
      parent_span_id: str | None,
      agent: str | None = None,
      tool_origin: str = "LOCAL",
      total_ms: int | None = None,
      status: str = "OK",
      error_message: str | None = None,
      attributes: dict[str, Any] | None = None,
  ) -> dict[str, Any]:
    return self.log_event(
        event_type="TOOL_COMPLETED",
        agent=agent,
        session_id=session_id,
        invocation_id=invocation_id,
        trace_id=trace_id,
        span_id=span_id,
        parent_span_id=parent_span_id,
        content={
            "tool": tool,
            "result": result,
            "tool_origin": tool_origin,
        },
        attributes=attributes,
        latency_ms={"total_ms": total_ms} if total_ms is not None else {},
        status=status,
        error_message=error_message,
    )

  def _emit_row(self, row: dict[str, Any]) -> None:
    bq_row = _serialize_bq_json_fields(row)
    if self.config.dry_run:
      log_to_file(
          self.config,
          "DRY_RUN " + json.dumps(bq_row, sort_keys=True, default=str),
      )
      return
    if not self.config.project_id or not self.config.dataset:
      raise ValueError(
          "BQAA_PROJECT_ID/GCP_PROJECT_ID and BQAA_DATASET are required"
      )
    if self.config.direct_write:
      self._direct_insert(bq_row)
      return
    self._spool(bq_row)

  def _spool(self, bq_row: dict[str, Any]) -> None:
    spool = Path(self.config.spool_dir).expanduser()
    spool.mkdir(parents=True, exist_ok=True)
    envelope = {
        "config": {
            "project_id": self.config.project_id,
            "dataset": self.config.dataset,
            "table": self.config.table,
            "location": self.config.location,
            "auto_create_table": self.config.auto_create_table,
            "auto_create_dataset": self.config.auto_create_dataset,
            "writer_label": self.config.writer_label,
        },
        "row": bq_row,
    }
    name = f"event-{time.time_ns()}-{os.getpid()}-{uuid.uuid4().hex[:8]}.json"
    path = spool / name
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(envelope, sort_keys=True, default=str), encoding="utf-8"
    )
    os.replace(tmp, path)
    _ensure_drainer(self.config)

  def _direct_insert(self, bq_row: dict[str, Any]) -> None:
    self._ensure_table()
    errors = self._client.insert_rows_json(self._table_id, [bq_row])
    if errors:
      raise RuntimeError(f"BigQuery insert failed: {errors}")

  def _ensure_table(self) -> None:
    if self._table_ready:
      return
    from google.cloud import bigquery
    from google.cloud.exceptions import NotFound

    self._client = self._client or bigquery.Client(
        project=self.config.project_id, location=self.config.location
    )
    dataset_id = f"{self.config.project_id}.{self.config.dataset}"
    if self.config.auto_create_dataset:
      try:
        self._client.get_dataset(dataset_id)
      except NotFound:
        dataset = bigquery.Dataset(dataset_id)
        if self.config.location:
          dataset.location = self.config.location
        self._client.create_dataset(dataset)

    try:
      self._client.get_table(self._table_id)
    except NotFound:
      if not self.config.auto_create_table:
        raise
      table = bigquery.Table(self._table_id, schema=bq_schema(bigquery))
      table.time_partitioning = bigquery.TimePartitioning(
          type_=bigquery.TimePartitioningType.DAY,
          field="timestamp",
      )
      table.clustering_fields = ["event_type", "agent", "user_id"]
      table.labels = {"adk_schema_version": "1"}
      self._client.create_table(table)
    self._table_ready = True

  @property
  def _table_id(self) -> str:
    return (
        f"{self.config.project_id}."
        f"{self.config.dataset}."
        f"{self.config.table}"
    )
