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

"""Export arbitrary BigQuery trace rows to LangSmith.

The connector deliberately knows nothing about event payload semantics. A
``FieldMapping`` declares the small set of values needed to construct a
LangSmith run. Every other source column is copied to run metadata, and mapped
payloads are passed through without event-type-specific interpretation.
"""

from __future__ import annotations

import base64
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import field
from datetime import date
from datetime import datetime
from datetime import time as datetime_time
from datetime import timedelta
from datetime import timezone
from decimal import Decimal
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import tempfile
import time
from typing import Any
import uuid

from google.cloud import bigquery
import yaml

from .._telemetry import make_bq_client
from .._telemetry import with_sdk_labels
from ..trace import _build_span_tree
from ..trace import Span

_LOGGER = logging.getLogger(__name__)
_TABLE_REFERENCE_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_-]*\.[A-Za-z0-9_][A-Za-z0-9_]*\."
    r"[A-Za-z0-9_][A-Za-z0-9_$-]*$"
)
_PATH_SEGMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_LANGSMITH_NAMESPACE = uuid.UUID("d9f0dc62-d16c-4fc1-9b72-08dc8cf77a84")
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_MISSING = object()
_SOURCE_STATUS_ERROR = "[SOURCE_STATUS_ERROR]"
_WATERMARK_VERSION = 1


@dataclass(frozen=True)
class FieldMapping:
  """Source paths used to populate LangSmith run fields.

  Paths are tuples of mapping keys. Dotted strings in configuration files are
  converted to tuples; they are traversed only when the source value is already
  a mapping. JSON strings are never decoded or inspected.
  """

  run_id: tuple[str, ...]
  trace_id: tuple[str, ...]
  parent_run_id: tuple[str, ...] | None = None
  name: tuple[str, ...] | None = None
  run_type: tuple[str, ...] | None = None
  start_time: tuple[str, ...] | None = None
  end_time: tuple[str, ...] | None = None
  latency_ms: tuple[str, ...] | None = None
  error: tuple[str, ...] | None = None
  status: tuple[str, ...] | None = None
  inputs: tuple[str, ...] | None = None
  outputs: tuple[str, ...] | None = None

  @classmethod
  def standard_adk(cls) -> FieldMapping:
    """Return the mapping for the ADK ``agent_events`` schema."""
    return cls(
        run_id=("span_id",),
        trace_id=("trace_id",),
        parent_run_id=("parent_span_id",),
        name=("event_type",),
        start_time=("timestamp",),
        latency_ms=("latency_ms", "total_ms"),
        error=("error_message",),
        status=("status",),
        inputs=("content",),
    )

  @classmethod
  def from_dict(cls, values: Mapping[str, Any]) -> FieldMapping:
    """Build a mapping from LangSmith-field-to-source-path values."""
    if "fields" in values:
      nested = values["fields"]
      if not isinstance(nested, Mapping):
        raise ValueError("mapping.fields must be an object")
      values = nested

    defaults = asdict(cls.standard_adk())
    unknown = set(values) - set(defaults)
    if unknown:
      raise ValueError(
          "unknown mapping field(s): " + ", ".join(sorted(unknown))
      )
    parsed: dict[str, tuple[str, ...] | None] = {}
    for name, default in defaults.items():
      raw = values.get(name, default)
      parsed[name] = _parse_path(raw, field_name=name)
    if not parsed["run_id"]:
      raise ValueError("mapping run_id is required")
    if not parsed["trace_id"]:
      raise ValueError("mapping trace_id is required")
    return cls(**parsed)  # type: ignore[arg-type]

  @classmethod
  def from_file(cls, path: str | Path) -> FieldMapping:
    """Load a mapping from a YAML or JSON file."""
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, Mapping):
      raise ValueError("mapping file must contain an object")
    return cls.from_dict(raw)

  def fingerprint(self) -> str:
    """Return a stable fingerprint used to bind incremental state."""
    payload = json.dumps(asdict(self), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()

  def mapped_whole_columns(self) -> frozenset[str]:
    """Return columns wholly represented by mapped LangSmith fields.

    A nested mapping such as ``payload.trace.id`` does not consume the whole
    ``payload`` column. It remains in metadata so sibling fields are not lost.
    """
    return frozenset(
        path[0]
        for name, path in asdict(self).items()
        if name != "status" and path is not None and len(path) == 1
    )


@dataclass(frozen=True)
class ExportConfig:
  """Operational settings for :func:`export`."""

  mapping: FieldMapping = field(default_factory=FieldMapping.standard_adk)
  project_id: str | None = None
  location: str | None = None
  langsmith_project: str | None = None
  langsmith_api_key: str | None = None
  langsmith_endpoint: str | None = None
  langsmith_workspace_id: str | None = None
  source_id: str | None = None
  since: datetime | None = None
  until: datetime | None = None
  where: str | None = None
  incremental: bool = False
  watermark_path: Path = Path(".bqaa-langsmith-watermark.json")
  batch_size: int = 100
  requests_per_second: float | None = 5.0
  max_retries: int = 3
  retry_backoff_seconds: float = 1.0
  max_dropped_rows: int = 1000

  def __post_init__(self) -> None:
    if self.batch_size < 1:
      raise ValueError("batch_size must be at least 1")
    if self.requests_per_second is not None and self.requests_per_second <= 0:
      raise ValueError("requests_per_second must be positive or None")
    if self.max_retries < 0:
      raise ValueError("max_retries cannot be negative")
    if self.retry_backoff_seconds < 0:
      raise ValueError("retry_backoff_seconds cannot be negative")
    if self.max_dropped_rows < 0:
      raise ValueError("max_dropped_rows cannot be negative")
    if self.since and self.until and self.since >= self.until:
      raise ValueError("since must be before until")


@dataclass(frozen=True)
class DroppedRow:
  """A source row that could not be exported."""

  source_run_id: str
  reason: str


@dataclass(frozen=True)
class ExportStats:
  """Summary returned by an export job."""

  rows_read: int
  exported: int
  skipped: int
  failed: int
  traces_exported: int
  batches: int
  dropped_rows: tuple[DroppedRow, ...]
  dropped_rows_truncated: int = 0
  watermark: datetime | None = None

  def to_dict(self) -> dict[str, Any]:
    """Return a JSON-friendly summary."""
    return {
        "rows_read": self.rows_read,
        "exported": self.exported,
        "skipped": self.skipped,
        "failed": self.failed,
        "traces_exported": self.traces_exported,
        "batches": self.batches,
        "dropped_rows": [asdict(row) for row in self.dropped_rows],
        "dropped_rows_truncated": self.dropped_rows_truncated,
        "watermark": self.watermark.isoformat() if self.watermark else None,
    }


@dataclass(frozen=True)
class _Watermark:
  timestamp: datetime
  trace_id: str
  run_id: str


def _max_watermark(
    first: _Watermark | None, second: _Watermark | None
) -> _Watermark | None:
  candidates = [item for item in (first, second) if item is not None]
  if not candidates:
    return None
  return max(
      candidates,
      key=lambda item: (item.timestamp, item.trace_id, item.run_id),
  )


@dataclass(frozen=True)
class _MappedRow:
  source_run_id: str
  source_trace_id: str
  source_parent_id: str | None
  name: str
  run_type: str
  start_time: datetime
  end_time: datetime | None
  error: str | None
  status: str | None
  inputs: dict[str, Any]
  outputs: dict[str, Any] | None
  metadata: dict[str, Any]


@dataclass(frozen=True)
class _PreparedTrace:
  runs: list[dict[str, Any]]
  source_run_ids: tuple[str, ...]
  skipped: tuple[DroppedRow, ...] = ()
  skipped_count: int = 0
  watermark_candidate: _Watermark | None = None


@dataclass
class _PendingBatch:
  runs: list[dict[str, Any]] = field(default_factory=list)
  source_run_ids: list[str] = field(default_factory=list)
  trace_tokens: list[int] = field(default_factory=list)

  def add(
      self,
      root: dict[str, Any],
      source_runs: list[dict[str, Any]],
      source_run_ids: list[str],
      trace_token: int,
  ) -> None:
    self.runs.append(root)
    self.runs.extend(source_runs)
    self.source_run_ids.extend(source_run_ids)
    self.trace_tokens.append(trace_token)

  def clear(self) -> None:
    self.runs.clear()
    self.source_run_ids.clear()
    self.trace_tokens.clear()


@dataclass
class _TraceProgress:
  remaining_chunks: int
  successful: bool
  watermark: _Watermark | None


@dataclass
class _DroppedRowCollector:
  maximum: int
  rows: list[DroppedRow] = field(default_factory=list)
  total: int = 0

  def add(self, row: DroppedRow) -> None:
    self.total += 1
    if len(self.rows) < self.maximum:
      self.rows.append(row)

  def extend(self, rows: Iterable[DroppedRow]) -> None:
    for row in rows:
      self.add(row)

  def record_omitted(self, count: int) -> None:
    self.total += count

  @property
  def truncated(self) -> int:
    return self.total - len(self.rows)


def _parse_path(value: Any, *, field_name: str) -> tuple[str, ...] | None:
  if value is None:
    return None
  if isinstance(value, str):
    path = tuple(value.split("."))
  elif isinstance(value, (list, tuple)) and all(
      isinstance(item, str) for item in value
  ):
    path = tuple(value)
  else:
    raise ValueError(f"mapping {field_name} must be a dotted path or null")
  if not path or any(not segment for segment in path):
    raise ValueError(f"mapping {field_name} contains an empty path segment")
  return path


def _path_value(row: Mapping[str, Any], path: tuple[str, ...] | None) -> Any:
  if path is None:
    return _MISSING
  value: Any = row
  for segment in path:
    if not isinstance(value, Mapping) or segment not in value:
      return _MISSING
    value = value[segment]
  return value


def _required_string(
    row: Mapping[str, Any], path: tuple[str, ...], name: str
) -> str:
  value = _path_value(row, path)
  if value is _MISSING or value is None or value == "":
    raise ValueError(f"missing mapped {name}")
  if not isinstance(value, (str, int, float, Decimal)):
    raise ValueError(f"mapped {name} must be a scalar")
  return str(value)


def _optional_string(
    row: Mapping[str, Any], path: tuple[str, ...] | None
) -> str | None:
  value = _path_value(row, path)
  if value is _MISSING or value is None:
    return None
  if not isinstance(value, (str, int, float, Decimal)):
    return None
  return str(value)


def _as_datetime(value: Any) -> datetime | None:
  if isinstance(value, datetime):
    parsed = value
  elif isinstance(value, str):
    try:
      parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
      return None
  else:
    return None
  if parsed.tzinfo is None:
    return parsed.replace(tzinfo=timezone.utc)
  return parsed.astimezone(timezone.utc)


def _as_io(value: Any) -> dict[str, Any] | None:
  if value is _MISSING or value is None:
    return None
  if callable(getattr(value, "items", None)):
    return _json_compatible(value)
  return {"value": _json_compatible(value)}


def _json_compatible(value: Any) -> Any:
  """Encode BigQuery scalar/container values without inspecting semantics."""
  if value is None or isinstance(value, (str, int, float, bool)):
    return value
  if isinstance(value, bytes):
    return {
        "encoding": "base64",
        "data": base64.b64encode(value).decode("ascii"),
    }
  if isinstance(value, Decimal):
    return str(value)
  if isinstance(value, (datetime, date, datetime_time)):
    return value.isoformat()
  if isinstance(value, uuid.UUID):
    return str(value)
  items = getattr(value, "items", None)
  if callable(items):
    return {str(key): _json_compatible(item) for key, item in items()}
  if isinstance(value, (list, tuple)):
    return [_json_compatible(item) for item in value]
  return str(value)


def _map_row(
    row: Mapping[str, Any],
    mapping: FieldMapping,
    consumed_columns: frozenset[str],
) -> _MappedRow:
  source_run_id = _required_string(row, mapping.run_id, "run_id")
  source_trace_id = _required_string(row, mapping.trace_id, "trace_id")
  source_parent_id = _optional_string(row, mapping.parent_run_id)
  name = _optional_string(row, mapping.name) or "BQAA event"
  run_type = _optional_string(row, mapping.run_type) or "chain"
  start_time = _as_datetime(_path_value(row, mapping.start_time)) or _EPOCH
  end_time = _as_datetime(_path_value(row, mapping.end_time))
  latency = _path_value(row, mapping.latency_ms)
  if end_time is None and isinstance(latency, (int, float)):
    end_time = start_time + timedelta(milliseconds=float(latency))
  error = _optional_string(row, mapping.error)
  status = _optional_string(row, mapping.status)
  if not error and status is not None and status.upper() == "ERROR":
    error = _SOURCE_STATUS_ERROR
  inputs = _as_io(_path_value(row, mapping.inputs)) or {}
  outputs = _as_io(_path_value(row, mapping.outputs))
  metadata = {
      key: _json_compatible(value)
      for key, value in row.items()
      if key not in consumed_columns
  }
  return _MappedRow(
      source_run_id=source_run_id,
      source_trace_id=source_trace_id,
      source_parent_id=source_parent_id,
      name=name,
      run_type=run_type,
      start_time=start_time,
      end_time=end_time,
      error=error,
      status=status,
      inputs=inputs,
      outputs=outputs,
      metadata=metadata,
  )


def _stable_uuid(source: str, *parts: str) -> uuid.UUID:
  return uuid.uuid5(_LANGSMITH_NAMESPACE, "\x1f".join((source, *parts)))


def _dotted_segment(timestamp: datetime, run_id: uuid.UUID) -> str:
  normalized = timestamp.astimezone(timezone.utc)
  return f"{normalized.strftime('%Y%m%dT%H%M%S%fZ')}{run_id}"


def _prepare_trace_runs(
    rows: Iterable[Mapping[str, Any]],
    *,
    mapping: FieldMapping,
    source_fingerprint: str,
    project_name: str,
    max_dropped_rows: int | None = None,
) -> _PreparedTrace:
  """Map one source trace to a LangSmith run tree.

  ``Trace._build_tree`` is the hierarchy authority used by the rest of the SDK.
  An exporter-owned synthetic root gives every LangSmith trace exactly one
  root, including source traces with multiple roots.
  """
  mapped: list[_MappedRow] = []
  skipped: list[DroppedRow] = []
  skipped_count = 0
  seen: set[tuple[str, str]] = set()
  consumed_columns = mapping.mapped_whole_columns()

  def record_skipped(row: DroppedRow) -> None:
    nonlocal skipped_count
    skipped_count += 1
    if max_dropped_rows is None or len(skipped) < max_dropped_rows:
      skipped.append(row)

  for index, row in enumerate(rows):
    try:
      item = _map_row(row, mapping, consumed_columns)
    except ValueError as exc:
      fallback = _optional_string(row, mapping.run_id) or f"row-{index}"
      record_skipped(DroppedRow(fallback, str(exc)))
      continue
    identity = (item.source_trace_id, item.source_run_id)
    if identity in seen:
      record_skipped(DroppedRow(item.source_run_id, "duplicate source run_id"))
      continue
    seen.add(identity)
    mapped.append(item)

  if not mapped:
    return _PreparedTrace(
        runs=[],
        source_run_ids=(),
        skipped=tuple(skipped),
        skipped_count=skipped_count,
    )

  trace_ids = {item.source_trace_id for item in mapped}
  if len(trace_ids) != 1:
    raise ValueError("_prepare_trace_runs requires rows from exactly one trace")
  source_trace_id = mapped[0].source_trace_id
  mapped.sort(key=lambda item: (item.start_time, item.source_run_id))

  span_to_row: dict[int, _MappedRow] = {}
  spans: list[Span] = []
  for item in mapped:
    span = Span(
        event_type=item.name,
        agent=None,
        timestamp=item.start_time,
        span_id=item.source_run_id,
        parent_span_id=item.source_parent_id,
    )
    spans.append(span)
    span_to_row[id(span)] = item
  roots = _build_span_tree(spans)

  trace_uuid = _stable_uuid(source_fingerprint, "trace", source_trace_id)
  # The synthetic root may be emitted by multiple overlapping or incremental
  # windows. A fixed structural timestamp keeps its wire representation and
  # every descendant's dotted-order prefix stable when a window does not
  # contain the source trace's first event. Source event timestamps remain
  # unchanged on their individual runs.
  root_dotted = _dotted_segment(_EPOCH, trace_uuid)
  runs: list[dict[str, Any]] = [
      {
          "id": trace_uuid,
          "trace_id": trace_uuid,
          "parent_run_id": None,
          "dotted_order": root_dotted,
          "session_name": project_name,
          "name": "BQAA trace",
          "run_type": "chain",
          "inputs": {},
          "start_time": _EPOCH,
          "end_time": _EPOCH,
          "extra": {
              "metadata": {},
              "bqaa": {"source_trace_id": source_trace_id},
          },
      }
  ]
  visited: set[str] = set()

  def append_component(
      component_root: Span, parent_uuid: uuid.UUID, parent_dotted: str
  ) -> None:
    # Iterative preorder traversal avoids failing on traces whose nesting depth
    # exceeds Python's recursion limit.
    stack = [(component_root, parent_uuid, parent_dotted)]
    while stack:
      span, span_parent_uuid, span_parent_dotted = stack.pop()
      item = span_to_row[id(span)]
      if item.source_run_id in visited:
        continue
      visited.add(item.source_run_id)
      run_uuid = _stable_uuid(
          source_fingerprint,
          "run",
          item.source_trace_id,
          item.source_run_id,
      )
      dotted = (
          span_parent_dotted + "." + _dotted_segment(item.start_time, run_uuid)
      )
      run: dict[str, Any] = {
          "id": run_uuid,
          "trace_id": trace_uuid,
          "parent_run_id": span_parent_uuid,
          "dotted_order": dotted,
          "session_name": project_name,
          "name": item.name,
          "run_type": item.run_type,
          "inputs": item.inputs,
          "start_time": item.start_time,
          "extra": {
              "metadata": item.metadata,
              "bqaa": {
                  "source_trace_id": item.source_trace_id,
                  "source_run_id": item.source_run_id,
                  **(
                      {"source_parent_run_id": item.source_parent_id}
                      if item.source_parent_id is not None
                      else {}
                  ),
              },
          },
      }
      if item.end_time is not None:
        run["end_time"] = item.end_time
      if item.outputs is not None:
        run["outputs"] = item.outputs
      if item.error is not None:
        run["error"] = item.error
      runs.append(run)
      children = sorted(
          span.children,
          key=lambda value: (
              span_to_row[id(value)].start_time,
              span_to_row[id(value)].source_run_id,
          ),
          reverse=True,
      )
      stack.extend((child, run_uuid, dotted) for child in children)

  for root in sorted(
      roots,
      key=lambda value: (
          span_to_row[id(value)].start_time,
          span_to_row[id(value)].source_run_id,
      ),
  ):
    # A query window may omit an ancestor. Pointing at that absent run while
    # using the synthetic root's dotted prefix creates a contradictory tree.
    # Flatten roots under the synthetic run; the original source parent stays
    # available in ``extra.bqaa.source_parent_run_id``.
    append_component(root, trace_uuid, root_dotted)

  # Existing trace reconstruction intentionally yields no roots for a cycle.
  # Keep every source row exportable by breaking each remaining component at a
  # deterministic node and placing that node below the synthetic root.
  for span in spans:
    item = span_to_row[id(span)]
    if item.source_run_id not in visited:
      append_component(span, trace_uuid, root_dotted)

  watermark_candidate = max(
      (
          _Watermark(
              item.start_time,
              item.source_trace_id,
              item.source_run_id,
          )
          for item in mapped
      ),
      key=lambda item: (item.timestamp, item.trace_id, item.run_id),
  )
  return _PreparedTrace(
      runs=runs,
      source_run_ids=tuple(item.source_run_id for item in mapped),
      skipped=tuple(skipped),
      skipped_count=skipped_count,
      watermark_candidate=watermark_candidate,
  )


def _sql_path(path: tuple[str, ...] | None, *, field_name: str) -> str:
  if path is None:
    raise ValueError(f"mapping {field_name} is required for this filter")
  if any(not _PATH_SEGMENT_RE.fullmatch(segment) for segment in path):
    raise ValueError(
        f"mapping {field_name} must contain BigQuery identifier segments"
    )
  return "bqaa_source." + ".".join(f"`{segment}`" for segment in path)


def _normalize_sql_source(source: str) -> str:
  return source.strip().removesuffix(";").rstrip()


def _build_source_query(
    source: str,
    *,
    mapping: FieldMapping,
    since: datetime | None,
    until: datetime | None,
    where: str | None,
    watermark: _Watermark | None,
) -> tuple[str, list[bigquery.ScalarQueryParameter]]:
  """Build the outer query and bound time/watermark parameters."""
  source = source.strip()
  if not source:
    raise ValueError("source cannot be empty")
  if _TABLE_REFERENCE_RE.fullmatch(source):
    sql = f"SELECT * FROM `{source}` AS bqaa_source"
  else:
    query = _normalize_sql_source(source)
    sql = f"SELECT * FROM (\n{query}\n) AS bqaa_source"

  conditions: list[str] = []
  parameters: list[bigquery.ScalarQueryParameter] = []
  if since is not None:
    timestamp = _sql_path(mapping.start_time, field_name="start_time")
    conditions.append(f"{timestamp} >= @bqaa_since")
    parameters.append(
        bigquery.ScalarQueryParameter("bqaa_since", "TIMESTAMP", since)
    )
  if until is not None:
    timestamp = _sql_path(mapping.start_time, field_name="start_time")
    conditions.append(f"{timestamp} < @bqaa_until")
    parameters.append(
        bigquery.ScalarQueryParameter("bqaa_until", "TIMESTAMP", until)
    )
  if watermark is not None:
    timestamp = _sql_path(mapping.start_time, field_name="start_time")
    trace_id = _sql_path(mapping.trace_id, field_name="trace_id")
    run_id = _sql_path(mapping.run_id, field_name="run_id")
    conditions.append(
        f"({timestamp} > @bqaa_watermark_timestamp OR "
        f"({timestamp} = @bqaa_watermark_timestamp AND "
        f"(CAST({trace_id} AS STRING) > @bqaa_watermark_trace_id OR "
        f"(CAST({trace_id} AS STRING) = @bqaa_watermark_trace_id AND "
        f"CAST({run_id} AS STRING) > @bqaa_watermark_run_id))))"
    )
    parameters.extend(
        [
            bigquery.ScalarQueryParameter(
                "bqaa_watermark_timestamp", "TIMESTAMP", watermark.timestamp
            ),
            bigquery.ScalarQueryParameter(
                "bqaa_watermark_trace_id", "STRING", watermark.trace_id
            ),
            bigquery.ScalarQueryParameter(
                "bqaa_watermark_run_id", "STRING", watermark.run_id
            ),
        ]
    )
  if where:
    conditions.append(f"({where})")
  if conditions:
    sql += "\nWHERE " + " AND ".join(conditions)
  trace_path = _sql_path(mapping.trace_id, field_name="trace_id")
  run_path = _sql_path(mapping.run_id, field_name="run_id")
  ordering = [f"CAST({trace_path} AS STRING)"]
  if mapping.start_time is not None:
    ordering.append(_sql_path(mapping.start_time, field_name="start_time"))
  ordering.append(f"CAST({run_path} AS STRING)")
  sql += "\nORDER BY " + ", ".join(ordering)
  return sql, parameters


def _canonical_source(source: str, source_id: str | None) -> str:
  if source_id:
    return source_id
  stripped = source.strip()
  if _TABLE_REFERENCE_RE.fullmatch(stripped):
    return "table:" + stripped.lower()
  normalized = _normalize_sql_source(stripped)
  return "sql:" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _load_watermark(
    path: Path,
    *,
    source: str,
    mapping: FieldMapping,
    destination: Mapping[str, str | None],
) -> _Watermark | None:
  if not path.exists():
    return None
  raw = json.loads(path.read_text(encoding="utf-8"))
  if not isinstance(raw, dict):
    raise ValueError("watermark file must contain an object")
  if raw.get("version") != _WATERMARK_VERSION:
    raise ValueError("unsupported watermark version")
  if (
      raw.get("source") != source
      or raw.get("mapping") != mapping.fingerprint()
      or raw.get("destination") != dict(destination)
  ):
    raise ValueError(
        "watermark belongs to a different source, field mapping, or destination"
    )
  timestamp = _as_datetime(raw.get("timestamp"))
  trace_id = raw.get("trace_id")
  run_id = raw.get("run_id")
  if (
      timestamp is None
      or not isinstance(trace_id, str)
      or not isinstance(run_id, str)
  ):
    raise ValueError("watermark timestamp/trace_id/run_id is invalid")
  return _Watermark(timestamp, trace_id, run_id)


def _save_watermark(
    path: Path,
    watermark: _Watermark,
    *,
    source: str,
    mapping: FieldMapping,
    destination: Mapping[str, str | None],
) -> None:
  path.parent.mkdir(parents=True, exist_ok=True)
  payload = json.dumps(
      {
          "version": _WATERMARK_VERSION,
          "source": source,
          "mapping": mapping.fingerprint(),
          "destination": dict(destination),
          "timestamp": watermark.timestamp.isoformat(),
          "trace_id": watermark.trace_id,
          "run_id": watermark.run_id,
      },
      sort_keys=True,
      indent=2,
  )
  fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
  try:
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
      stream.write(payload)
      stream.write("\n")
      stream.flush()
      os.fsync(stream.fileno())
    os.replace(temporary, path)
  except BaseException:
    Path(temporary).unlink(missing_ok=True)
    raise


def _is_retryable(exc: Exception) -> bool:
  nested = getattr(exc, "exceptions", None)
  if isinstance(nested, (list, tuple)):
    return bool(nested) and all(
        isinstance(item, Exception) and _is_retryable(item) for item in nested
    )

  try:
    from langsmith import utils as langsmith_utils
  except ImportError:
    langsmith_utils = None
  if langsmith_utils is not None:
    retryable_types = tuple(
        exception_type
        for name in (
            "LangSmithAPIError",
            "LangSmithConflictError",
            "LangSmithConnectionError",
            "LangSmithRateLimitError",
            "LangSmithRequestTimeout",
        )
        if isinstance(
            exception_type := getattr(langsmith_utils, name, None), type
        )
    )
    if retryable_types and isinstance(exc, retryable_types):
      return True

  status = getattr(exc, "status_code", None)
  if status is None:
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
  if status in (408, 409, 425, 429):
    return True
  if isinstance(status, int) and status >= 500:
    return True
  return isinstance(exc, (ConnectionError, TimeoutError))


class _RateLimiter:

  def __init__(
      self,
      requests_per_second: float | None,
      *,
      sleep: Callable[[float], None],
      monotonic: Callable[[], float],
  ) -> None:
    self._interval = (
        None if requests_per_second is None else 1.0 / requests_per_second
    )
    self._sleep = sleep
    self._monotonic = monotonic
    self._last_request: float | None = None

  def wait(self) -> None:
    if self._interval is None:
      return
    now = self._monotonic()
    if self._last_request is not None:
      remaining = self._interval - (now - self._last_request)
      if remaining > 0:
        self._sleep(remaining)
        now = self._monotonic()
    self._last_request = now


def _call_with_retry(
    operation: Callable[[], None],
    *,
    config: ExportConfig,
    limiter: _RateLimiter,
    sleep: Callable[[float], None],
) -> None:
  for attempt in range(config.max_retries + 1):
    limiter.wait()
    try:
      operation()
      return
    except Exception as exc:
      if attempt >= config.max_retries or not _is_retryable(exc):
        raise
      sleep(config.retry_backoff_seconds * (2**attempt))


class _ErrorReportingLangSmithClient:
  """Make LangSmith's tracing-error callback observable to the exporter.

  ``Client.batch_ingest_runs`` reports terminal HTTP failures through its
  callback and then returns. Without this adapter the exporter would count a
  swallowed failure as success and advance its watermark past lost rows.
  """

  def __init__(self, client: Any, errors: list[Exception]) -> None:
    self._client = client
    self._errors = errors

  def batch_ingest_runs(self, *, create=None, update=None) -> None:
    self._errors.clear()
    self._client.batch_ingest_runs(create=create, update=update)
    if self._errors:
      raise self._errors[0]


def _create_langsmith_client(config: ExportConfig) -> Any:
  try:
    from langsmith import Client
  except ImportError as exc:
    raise ImportError(
        "LangSmith export requires the optional dependency; install "
        "bigquery-agent-analytics[langsmith]"
    ) from exc
  errors: list[Exception] = []
  client = Client(
      api_url=config.langsmith_endpoint,
      api_key=config.langsmith_api_key,
      workspace_id=config.langsmith_workspace_id,
      auto_batch_tracing=False,
      tracing_error_callback=errors.append,
  )
  return _ErrorReportingLangSmithClient(client, errors)


def _resolved_destination(
    config: ExportConfig, project_name: str, client: Any
) -> dict[str, str | None]:
  raw_client = getattr(client, "_client", client)
  endpoint = (
      config.langsmith_endpoint
      or getattr(raw_client, "api_url", None)
      or os.environ.get("LANGSMITH_ENDPOINT")
      or os.environ.get("LANGCHAIN_ENDPOINT")
      or "https://api.smith.langchain.com"
  )
  workspace = (
      config.langsmith_workspace_id
      or getattr(raw_client, "workspace_id", None)
      or os.environ.get("LANGSMITH_WORKSPACE_ID")
      or os.environ.get("LANGCHAIN_WORKSPACE_ID")
  )
  return {
      "project": project_name,
      "endpoint": str(endpoint).rstrip("/"),
      "workspace_id": str(workspace) if workspace is not None else None,
  }


def _row_dict(row: Any) -> dict[str, Any]:
  items = getattr(row, "items", None)
  if callable(items):
    return dict(items())
  raise TypeError("BigQuery query returned a row without mapping semantics")


def export(
    source: str,
    *,
    config: ExportConfig | None = None,
    mapping: FieldMapping | Mapping[str, Any] | None = None,
    bq_client: Any = None,
    langsmith_client: Any = None,
) -> ExportStats:
  """Export a BigQuery table or SQL result to LangSmith.

  Injected clients are supported for controlled runtimes and tests. Otherwise
  BigQuery uses Application Default Credentials, and the optional LangSmith SDK
  reads its standard environment variables unless explicit config values are
  supplied.
  """
  config = config or ExportConfig()
  if mapping is None:
    resolved_mapping = config.mapping
  elif isinstance(mapping, FieldMapping):
    resolved_mapping = mapping
  else:
    resolved_mapping = FieldMapping.from_dict(mapping)

  canonical_source = _canonical_source(source, config.source_id)
  project_name = (
      config.langsmith_project
      or os.environ.get("LANGSMITH_PROJECT")
      or os.environ.get("LANGCHAIN_PROJECT")
      or "default"
  )
  if langsmith_client is None:
    langsmith_client = _create_langsmith_client(config)
  destination = _resolved_destination(config, project_name, langsmith_client)
  watermark = None
  if config.incremental:
    watermark = _load_watermark(
        config.watermark_path,
        source=canonical_source,
        mapping=resolved_mapping,
        destination=destination,
    )
  sql, parameters = _build_source_query(
      source,
      mapping=resolved_mapping,
      since=config.since,
      until=config.until,
      where=config.where,
      watermark=watermark,
  )
  if bq_client is None:
    project = config.project_id
    if project is None and _TABLE_REFERENCE_RE.fullmatch(source.strip()):
      project = source.strip().split(".", 1)[0]
    bq_client = make_bq_client(
        project,
        location=config.location,
        sdk_surface="langsmith_export",
    )
  job_config = bigquery.QueryJobConfig(query_parameters=parameters)
  job_config = with_sdk_labels(job_config, feature="langsmith-export")
  dropped_rows = _DroppedRowCollector(config.max_dropped_rows)
  limiter = _RateLimiter(
      config.requests_per_second,
      sleep=time.sleep,
      monotonic=time.monotonic,
  )

  rows_read = 0
  exported = 0
  skipped = 0
  failed = 0
  batches = 0
  traces_exported = 0
  exported_watermark: _Watermark | None = None
  pending = _PendingBatch()
  trace_progress: dict[int, _TraceProgress] = {}
  next_trace_token = 0

  def flush() -> None:
    nonlocal exported, failed, batches, traces_exported
    nonlocal exported_watermark
    if not pending.runs:
      return
    batches += 1
    batch_runs = list(pending.runs)
    try:
      _call_with_retry(
          lambda: langsmith_client.batch_ingest_runs(create=batch_runs),
          config=config,
          limiter=limiter,
          sleep=time.sleep,
      )
    except Exception as exc:
      failed += len(pending.source_run_ids)
      for source_run_id in pending.source_run_ids:
        dropped_rows.add(DroppedRow(source_run_id, type(exc).__name__))
      for token in pending.trace_tokens:
        trace_progress[token].successful = False
      _LOGGER.warning(
          "LangSmith export batch failed for %d row(s): %s",
          len(pending.source_run_ids),
          type(exc).__name__,
      )
    else:
      exported += len(pending.source_run_ids)
    for token in pending.trace_tokens:
      progress = trace_progress[token]
      progress.remaining_chunks -= 1
      if progress.remaining_chunks == 0:
        if progress.successful:
          traces_exported += 1
          exported_watermark = _max_watermark(
              exported_watermark, progress.watermark
          )
        del trace_progress[token]
    _LOGGER.info(
        "LangSmith export progress: exported=%d skipped=%d failed=%d",
        exported,
        skipped,
        failed,
    )
    pending.clear()

  def enqueue(trace: _PreparedTrace) -> None:
    nonlocal skipped, next_trace_token
    skipped += trace.skipped_count
    dropped_rows.extend(trace.skipped)
    dropped_rows.record_omitted(trace.skipped_count - len(trace.skipped))
    if not trace.runs:
      return
    root = trace.runs[0]
    source_runs = trace.runs[1:]
    source_run_ids = [
        str(run["extra"]["bqaa"]["source_run_id"]) for run in source_runs
    ]
    if len(source_runs) != len(trace.source_run_ids):
      raise ValueError("prepared trace run/source accounting is inconsistent")
    source_count = len(source_runs)
    chunk_count = (source_count + config.batch_size - 1) // config.batch_size
    trace_token = next_trace_token
    next_trace_token += 1
    trace_progress[trace_token] = _TraceProgress(
        remaining_chunks=chunk_count,
        successful=trace.skipped_count == 0,
        watermark=trace.watermark_candidate,
    )

    if pending.source_run_ids and (
        source_count > config.batch_size
        or len(pending.source_run_ids) + source_count > config.batch_size
    ):
      flush()
    for offset in range(0, source_count, config.batch_size):
      chunk_runs = source_runs[offset : offset + config.batch_size]
      chunk_ids = source_run_ids[offset : offset + config.batch_size]
      pending.add(root, chunk_runs, chunk_ids, trace_token)
      if len(pending.source_run_ids) == config.batch_size:
        flush()

  query_rows = bq_client.query(sql, job_config=job_config).result(
      page_size=config.batch_size
  )
  current_trace_id: str | None = None
  current_rows: list[dict[str, Any]] = []
  for raw_row in query_rows:
    row_index = rows_read
    rows_read += 1
    row = _row_dict(raw_row)
    try:
      trace_id = _required_string(row, resolved_mapping.trace_id, "trace_id")
    except ValueError as exc:
      run_id = (
          _optional_string(row, resolved_mapping.run_id) or f"row-{row_index}"
      )
      skipped += 1
      dropped_rows.add(DroppedRow(run_id, str(exc)))
      continue
    if current_trace_id is not None and trace_id != current_trace_id:
      enqueue(
          _prepare_trace_runs(
              current_rows,
              mapping=resolved_mapping,
              source_fingerprint=canonical_source,
              project_name=project_name,
              max_dropped_rows=max(
                  config.max_dropped_rows - len(dropped_rows.rows), 0
              ),
          )
      )
      current_rows = []
    current_trace_id = trace_id
    current_rows.append(row)
  if current_rows:
    enqueue(
        _prepare_trace_runs(
            current_rows,
            mapping=resolved_mapping,
            source_fingerprint=canonical_source,
            project_name=project_name,
            max_dropped_rows=max(
                config.max_dropped_rows - len(dropped_rows.rows), 0
            ),
        )
    )
  flush()

  final_watermark = watermark.timestamp if watermark else None
  # Never advance past a failed API batch. Replaying successful rows is safe
  # because IDs are stable, while advancing could permanently skip failures.
  if (
      config.incremental
      and skipped == 0
      and failed == 0
      and exported_watermark is not None
  ):
    next_watermark = exported_watermark
    _save_watermark(
        config.watermark_path,
        next_watermark,
        source=canonical_source,
        mapping=resolved_mapping,
        destination=destination,
    )
    final_watermark = next_watermark.timestamp

  return ExportStats(
      rows_read=rows_read,
      exported=exported,
      skipped=skipped,
      failed=failed,
      traces_exported=traces_exported,
      batches=batches,
      dropped_rows=tuple(dropped_rows.rows),
      dropped_rows_truncated=dropped_rows.truncated,
      watermark=final_watermark,
  )


__all__ = [
    "DroppedRow",
    "ExportConfig",
    "ExportStats",
    "FieldMapping",
    "export",
]
