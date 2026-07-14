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

"""Trace reconstruction and visualization for BigQuery Agent Analytics SDK.

This module provides the Trace and Span objects that allow developers
to reconstruct and visualize agent conversation traces stored in
BigQuery. The key feature is ``trace.render()`` which generates a
hierarchical DAG view of the agent's reasoning steps.

Example usage::

    client = Client(project_id="my-project", dataset_id="analytics")
    trace = client.get_trace("trace-123")
    trace.render()  # Prints hierarchical DAG in notebook/terminal
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from enum import Enum
import json
import logging
import re
from typing import Any, Optional, Sequence, Union

logger = logging.getLogger("bigquery_agent_analytics." + __name__)


_ANSI_RED = "\x1b[31m"
_ANSI_YELLOW = "\x1b[33m"
_ANSI_RESET = "\x1b[0m"


def _colorize(text: str, ansi_code: str, enabled: bool) -> str:
  """Wraps text in an ANSI color code when enabled, else returns unchanged."""
  if not enabled:
    return text
  return f"{ansi_code}{text}{_ANSI_RESET}"


_TEXT_WRAPPER_PREFIX = "text: "


def _unwrap_text_field(value: str) -> str:
  """Strip a leading ``text: '...'`` wrapper if present.

  Some ADK plugin versions serialize an LLM response with a literal
  ``text: '...'`` Python-repr-style prefix in ``content.response``.
  Strip it so ``Span.summary`` and ``Trace.render`` surface a clean
  human-readable string.
  """
  if not value.startswith(_TEXT_WRAPPER_PREFIX):
    return value
  inner = value[len(_TEXT_WRAPPER_PREFIX) :]
  if not inner or inner[0] not in ("'", '"'):
    return inner
  quote = inner[0]
  # Match trailing quote only if the string is not truncated.
  if len(inner) >= 2 and inner.endswith(quote):
    return inner[1:-1]
  # Truncated — drop the opening quote and leave the rest.
  return inner[1:]


class EventType(Enum):
  """Standard event types logged by the analytics plugin."""

  USER_MESSAGE_RECEIVED = "USER_MESSAGE_RECEIVED"
  INVOCATION_STARTING = "INVOCATION_STARTING"
  INVOCATION_COMPLETED = "INVOCATION_COMPLETED"
  AGENT_STARTING = "AGENT_STARTING"
  AGENT_COMPLETED = "AGENT_COMPLETED"
  LLM_REQUEST = "LLM_REQUEST"
  LLM_RESPONSE = "LLM_RESPONSE"
  LLM_ERROR = "LLM_ERROR"
  TOOL_STARTING = "TOOL_STARTING"
  TOOL_COMPLETED = "TOOL_COMPLETED"
  TOOL_ERROR = "TOOL_ERROR"
  STATE_DELTA = "STATE_DELTA"
  HITL_CONFIRMATION_REQUEST = "HITL_CONFIRMATION_REQUEST"
  HITL_CREDENTIAL_REQUEST = "HITL_CREDENTIAL_REQUEST"
  HITL_INPUT_REQUEST = "HITL_INPUT_REQUEST"
  HITL_CONFIRMATION_REQUEST_COMPLETED = "HITL_CONFIRMATION_REQUEST_COMPLETED"
  HITL_CREDENTIAL_REQUEST_COMPLETED = "HITL_CREDENTIAL_REQUEST_COMPLETED"
  HITL_INPUT_REQUEST_COMPLETED = "HITL_INPUT_REQUEST_COMPLETED"
  # ADK 2.0 event types (producer: BQ AA Plugin minimum cut, #293).
  AGENT_TRANSFER = "AGENT_TRANSFER"
  EVENT_COMPACTION = "EVENT_COMPACTION"
  AGENT_STATE_CHECKPOINT = "AGENT_STATE_CHECKPOINT"
  TOOL_PAUSED = "TOOL_PAUSED"
  # Workflow-node boundaries — registered now; the producer derives
  # these per #207 (typed-view columns are a #207 follow-up).
  WORKFLOW_NODE_STARTING = "WORKFLOW_NODE_STARTING"
  WORKFLOW_NODE_COMPLETED = "WORKFLOW_NODE_COMPLETED"


@dataclass
class ObjectRef:
  """Reference to an externally stored object."""

  uri: Optional[str] = None
  version: Optional[str] = None
  authorizer: Optional[str] = None
  details: Optional[dict[str, Any]] = None


@dataclass
class ContentPart:
  """A single part of multimodal content."""

  mime_type: Optional[str] = None
  text: Optional[str] = None
  uri: Optional[str] = None
  storage_mode: Optional[str] = None
  object_ref: Optional[ObjectRef] = None
  part_index: Optional[int] = None
  part_attributes: Optional[str] = None


@dataclass
class Span:
  """Represents a single span (event) in a trace.

  Spans form a tree structure via ``parent_span_id`` references.
  """

  event_type: str
  agent: Optional[str]
  timestamp: datetime
  content: dict[str, Any] = field(default_factory=dict)
  attributes: dict[str, Any] = field(default_factory=dict)
  span_id: Optional[str] = None
  parent_span_id: Optional[str] = None
  latency_ms: Optional[float] = None
  status: str = "OK"
  error_message: Optional[str] = None
  content_parts: list[ContentPart] = field(default_factory=list)
  children: list[Span] = field(default_factory=list)
  session_id: Optional[str] = None
  invocation_id: Optional[str] = None
  user_id: Optional[str] = None
  trace_id: Optional[str] = None
  time_to_first_token_ms: Optional[float] = None

  @classmethod
  def from_bigquery_row(cls, row: dict[str, Any]) -> Span:
    """Creates a Span from a BigQuery row dictionary."""
    content = row.get("content")
    if isinstance(content, str):
      try:
        content = json.loads(content)
      except (json.JSONDecodeError, TypeError):
        content = {"raw": content}
    elif content is None:
      content = {}

    attributes = row.get("attributes")
    if isinstance(attributes, str):
      try:
        attributes = json.loads(attributes)
      except (json.JSONDecodeError, TypeError):
        attributes = {}
    elif attributes is None:
      attributes = {}

    latency_ms = row.get("latency_ms")
    time_to_first_token_ms = None
    if isinstance(latency_ms, str):
      try:
        latency_data = json.loads(latency_ms)
        time_to_first_token_ms = latency_data.get("time_to_first_token_ms")
        latency_ms = latency_data.get("total_ms")
      except (json.JSONDecodeError, TypeError):
        latency_ms = None
    elif isinstance(latency_ms, dict):
      time_to_first_token_ms = latency_ms.get("time_to_first_token_ms")
      latency_ms = latency_ms.get("total_ms")

    parts_raw = row.get("content_parts", [])
    content_parts = []
    if parts_raw:
      for p in parts_raw:
        obj_ref = None
        obj_ref_raw = p.get("object_ref")
        if obj_ref_raw and isinstance(obj_ref_raw, dict):
          obj_ref = ObjectRef(
              uri=obj_ref_raw.get("uri"),
              version=obj_ref_raw.get("version"),
              authorizer=obj_ref_raw.get("authorizer"),
              details=obj_ref_raw.get("details"),
          )
        content_parts.append(
            ContentPart(
                mime_type=p.get("mime_type"),
                text=p.get("text"),
                uri=p.get("uri"),
                storage_mode=p.get("storage_mode"),
                object_ref=obj_ref,
                part_index=p.get("part_index"),
                part_attributes=p.get("part_attributes"),
            )
        )

    return cls(
        event_type=row.get("event_type", "UNKNOWN"),
        agent=row.get("agent"),
        timestamp=row.get("timestamp", datetime.now(timezone.utc)),
        content=content,
        attributes=attributes,
        span_id=row.get("span_id"),
        parent_span_id=row.get("parent_span_id"),
        latency_ms=latency_ms,
        status=row.get("status", "OK"),
        error_message=row.get("error_message"),
        content_parts=content_parts,
        session_id=row.get("session_id"),
        invocation_id=row.get("invocation_id"),
        user_id=row.get("user_id"),
        trace_id=row.get("trace_id"),
        time_to_first_token_ms=time_to_first_token_ms,
    )

  @property
  def has_error(self) -> bool:
    """Returns True if this span indicates an error.

    Uses the canonical error detection predicate: the event type
    ends with ``_ERROR``, the ``error_message`` field is populated,
    or the ``status`` column is ``'ERROR'``.
    """
    return (
        self.event_type.endswith("_ERROR")
        or self.error_message is not None
        or self.status == "ERROR"
    )

  @property
  def is_error(self) -> bool:
    """Returns True if this span represents an error.

    Uses the canonical predicate: event type ends with
    ``_ERROR``, ``error_message`` is set, or ``status`` is
    ``'ERROR'``.
    """
    return self.has_error

  @property
  def subtree_has_error(self) -> bool:
    """Returns True if this span or any descendant has an error."""
    if self.has_error:
      return True
    return any(c.subtree_has_error for c in self.children)

  @property
  def failure_context(self) -> Optional[str]:
    """Returns a concise failure description if this span errored.

    Combines the event_type, tool name (if applicable), and the
    error_message into a single string for quick debugging.
    """
    if not self.is_error:
      return None
    parts = [self.event_type]
    if self.tool_name:
      parts.append(f"tool={self.tool_name}")
    if self.error_message:
      parts.append(self.error_message[:200])
    return " | ".join(parts)

  @property
  def tool_name(self) -> Optional[str]:
    """Returns the tool name for tool-related events.

    Populated only for ``TOOL_STARTING``, ``TOOL_COMPLETED``,
    ``TOOL_ERROR``, and ``HITL_*`` event types where the plugin
    writes the tool name into ``content.tool``. Returns ``None``
    for any other event type, even if ``content`` happens to
    carry a ``"tool"`` key — callers rely on this attribute
    meaning "this span invoked a tool."
    """
    if self.event_type not in (
        "TOOL_STARTING",
        "TOOL_COMPLETED",
        "TOOL_ERROR",
    ) and not self.event_type.startswith("HITL_"):
      return None
    tool = self.content.get("tool")
    return tool if tool else None

  @property
  def label(self) -> str:
    """Returns a human-readable label for this span."""
    parts = [self.event_type]
    if self.agent:
      parts.append(f"[{self.agent}]")

    # Add contextual detail
    if self.event_type in ("TOOL_STARTING", "TOOL_COMPLETED", "TOOL_ERROR"):
      tool = self.content.get("tool", "")
      if tool:
        parts.append(f"({tool})")
    elif self.event_type == "LLM_REQUEST":
      model = self.attributes.get("model", "")
      if model:
        parts.append(f"({model})")
    elif self.event_type.startswith("HITL_"):
      tool = self.content.get("tool", "")
      if tool:
        parts.append(f"({tool})")
    elif self.event_type == "STATE_DELTA":
      pass  # No extra detail needed in label

    if self.is_error:
      parts.append("ERROR")

    return " ".join(parts)

  @property
  def summary(self) -> str:
    """Returns a brief content summary for display."""
    if self.error_message:
      return self.error_message[:120]

    # HITL events: show tool name and args/result
    if self.event_type.startswith("HITL_"):
      tool = self.content.get("tool", "")
      if self.event_type.endswith("_COMPLETED"):
        result = self.content.get("result", "")
        text = f"{tool}: {result}" if tool else str(result)
      else:
        args = self.content.get("args", "")
        text = f"{tool}: {args}" if tool else str(args)
      if len(text) > 120:
        return text[:117] + "..."
      return text

    # STATE_DELTA: show keys changed
    # Plugin stores state delta in attributes.state_delta; fall back to
    # content.delta and then content itself for older formats.
    if self.event_type == "STATE_DELTA":
      delta = self.attributes.get("state_delta")
      if not delta:
        delta = self.content.get("delta")
      if not delta:
        delta = self.content
      if isinstance(delta, dict):
        keys = list(delta.keys())
        if keys:
          text = f"keys: {', '.join(keys)}"
          if len(text) > 120:
            return text[:117] + "..."
          return text
      return ""

    text = self.content.get("text_summary") or ""
    if not text:
      text = self.content.get("response") or ""
    if not text:
      text = self.content.get("text") or ""
    if not text:
      text = self.content.get("raw") or ""
    text = _unwrap_text_field(text) if isinstance(text, str) else text
    if not text and self.content_parts:
      for p in self.content_parts:
        if p.text:
          text = p.text
          break
        p_uri = p.uri
        if not p_uri and p.object_ref:
          p_uri = p.object_ref.uri
        if p_uri:
          text = f"[{p.mime_type or 'file'}] {p_uri}"
          break

    if len(text) > 120:
      return text[:117] + "..."
    return text


_TIME_WINDOW_RE = re.compile(r"^(\d+)([mhd])$")


def _parse_time_window(window: str) -> datetime:
  """Parse a relative time window into an absolute start time.

  Args:
      window: String like ``'30m'``, ``'1h'``, ``'7d'``.

  Returns:
      ``datetime`` representing *now - window*.

  Raises:
      ValueError: If the format is unrecognised.
  """
  match = _TIME_WINDOW_RE.match(window.strip().lower())
  if not match:
    raise ValueError(
        f"Invalid time window: {window!r}. "
        f"Expected format: Xm, Xh, or Xd "
        f"(e.g. '30m', '1h', '7d')."
    )
  value = int(match.group(1))
  unit = match.group(2)
  if unit == "m":
    delta = timedelta(minutes=value)
  elif unit == "h":
    delta = timedelta(hours=value)
  else:  # "d"
    delta = timedelta(days=value)
  return datetime.now(timezone.utc) - delta


@dataclass
class TraceFilter:
  """Filtering criteria for listing traces.

  All fields are optional. When multiple fields are set they
  are combined with AND logic.

  The identity/scope dimensions ``user_id``, ``root_agent_name``, and
  ``experiment_id`` are three-state (issue #359): ``None`` leaves the
  dimension unfiltered (legacy behavior), the :data:`SQL_NULL`
  sentinel matches only rows where the dimension is SQL ``NULL``, and
  a string — including the empty string — matches by equality.
  """

  start_time: Optional[datetime] = None
  end_time: Optional[datetime] = None
  agent_id: Optional[str] = None
  user_id: _ScalarPin = None
  session_ids: Optional[list[str]] = None
  experiment_id: _ScalarPin = None
  has_error: Optional[bool] = None
  error_type: Optional[str] = None
  custom_labels: Optional[dict[str, str]] = None
  min_latency_ms: Optional[float] = None
  max_latency_ms: Optional[float] = None
  event_types: Optional[list[str]] = None
  tool_origin: Optional[str] = None
  root_agent_name: _ScalarPin = None
  limit: int = 100

  def __post_init__(self) -> None:
    for dim in ("user_id", "root_agent_name", "experiment_id"):
      value = getattr(self, dim)
      if value is None or value is SQL_NULL or isinstance(value, str):
        continue
      # UNSET (and any foreign sentinel) belongs to the selector
      # surface; accepting it here would bind the sentinel object as
      # an equality query parameter.
      raise TypeError(
          f"TraceFilter.{dim} must be a string, None (unfiltered), or"
          " SQL_NULL."
      )

  @classmethod
  def from_cli_args(
      cls,
      last: str | None = None,
      agent_id: str | None = None,
      session_id: str | None = None,
      user_id: str | None = None,
      has_error: bool | None = None,
      custom_labels: dict[str, str] | None = None,
      limit: int = 100,
  ) -> "TraceFilter":
    """Build a ``TraceFilter`` from CLI-style arguments.

    Parses ``--last`` time windows (e.g. ``'1h'`` means
    *start_time = now - 1 hour*).  Also used by the Remote
    Function dispatch layer to convert params JSON into a
    filter.

    Supported ``last`` formats: ``Xm`` (minutes), ``Xh``
    (hours), ``Xd`` (days).

    Args:
        last: Relative time window string.
        agent_id: Filter to a specific agent.
        session_id: Filter to a single session.
        user_id: Filter to a specific user.
        has_error: If set, filter by error presence.
        custom_labels: Filter by custom_tags key-value pairs
            written via ``BigQueryLoggerConfig.custom_tags``.
        limit: Maximum number of traces to return.

    Returns:
        A configured ``TraceFilter``.

    Raises:
        ValueError: If *last* has an unrecognised format.
    """
    start_time = None
    if last is not None:
      start_time = _parse_time_window(last)
    session_ids = [session_id] if session_id else None
    return cls(
        start_time=start_time,
        agent_id=agent_id,
        user_id=user_id,
        session_ids=session_ids,
        has_error=has_error,
        custom_labels=custom_labels,
        limit=limit,
    )

  def to_sql_conditions(self) -> tuple[str, list]:
    """Converts filter to SQL WHERE clauses and query parameters.

    Returns:
        Tuple of (SQL conditions string, list of BQ query params).
    """
    from google.cloud import bigquery

    conditions = []
    params = []

    if self.start_time:
      conditions.append("timestamp >= @start_time")
      params.append(
          bigquery.ScalarQueryParameter(
              "start_time",
              "TIMESTAMP",
              self.start_time,
          )
      )
    if self.end_time:
      conditions.append("timestamp <= @end_time")
      params.append(
          bigquery.ScalarQueryParameter(
              "end_time",
              "TIMESTAMP",
              self.end_time,
          )
      )
    if self.agent_id:
      conditions.append("agent = @agent_id")
      params.append(
          bigquery.ScalarQueryParameter(
              "agent_id",
              "STRING",
              self.agent_id,
          )
      )
    if self.user_id is SQL_NULL:
      conditions.append("user_id IS NULL")
    elif self.user_id is not None:
      conditions.append("user_id = @user_id")
      params.append(
          bigquery.ScalarQueryParameter(
              "user_id",
              "STRING",
              self.user_id,
          )
      )
    if self.session_ids:
      conditions.append("session_id IN UNNEST(@session_ids)")
      params.append(
          bigquery.ArrayQueryParameter(
              "session_ids",
              "STRING",
              self.session_ids,
          )
      )
    if self.has_error is True:
      conditions.append(
          "(ENDS_WITH(event_type, '_ERROR')"
          " OR error_message IS NOT NULL"
          " OR status = 'ERROR')"
      )
    elif self.has_error is False:
      conditions.append(
          "NOT ENDS_WITH(event_type, '_ERROR')"
          " AND error_message IS NULL"
          " AND status != 'ERROR'"
      )
    if self.error_type:
      conditions.append("error_message LIKE @error_type")
      params.append(
          bigquery.ScalarQueryParameter(
              "error_type",
              "STRING",
              f"%{self.error_type}%",
          )
      )
    if self.min_latency_ms is not None:
      conditions.append(
          "CAST(JSON_VALUE(latency_ms, '$.total_ms')"
          " AS FLOAT64) >= @min_latency_ms"
      )
      params.append(
          bigquery.ScalarQueryParameter(
              "min_latency_ms",
              "FLOAT64",
              self.min_latency_ms,
          )
      )
    if self.max_latency_ms is not None:
      conditions.append(
          "CAST(JSON_VALUE(latency_ms, '$.total_ms')"
          " AS FLOAT64) <= @max_latency_ms"
      )
      params.append(
          bigquery.ScalarQueryParameter(
              "max_latency_ms",
              "FLOAT64",
              self.max_latency_ms,
          )
      )
    if self.experiment_id is SQL_NULL:
      conditions.append("JSON_VALUE(attributes, '$.experiment_id') IS NULL")
    elif self.experiment_id is not None:
      conditions.append(
          "JSON_VALUE(attributes, '$.experiment_id')" " = @experiment_id"
      )
      params.append(
          bigquery.ScalarQueryParameter(
              "experiment_id",
              "STRING",
              self.experiment_id,
          )
      )
    if self.custom_labels:
      for i, (key, value) in enumerate(self.custom_labels.items()):
        param_key = f"label_key_{i}"
        param_val = f"label_val_{i}"
        conditions.append(
            f"JSON_VALUE(attributes,"
            f" CONCAT('$.custom_tags.', @{param_key}))"
            f" = @{param_val}"
        )
        params.append(bigquery.ScalarQueryParameter(param_key, "STRING", key))
        params.append(bigquery.ScalarQueryParameter(param_val, "STRING", value))
    if self.event_types:
      conditions.append("event_type IN UNNEST(@event_types)")
      params.append(
          bigquery.ArrayQueryParameter(
              "event_types",
              "STRING",
              self.event_types,
          )
      )
    if self.tool_origin:
      conditions.append("JSON_VALUE(content, '$.tool_origin') = @tool_origin")
      params.append(
          bigquery.ScalarQueryParameter(
              "tool_origin",
              "STRING",
              self.tool_origin,
          )
      )
    if self.root_agent_name is SQL_NULL:
      conditions.append("JSON_VALUE(attributes, '$.root_agent_name') IS NULL")
    elif self.root_agent_name is not None:
      conditions.append(
          "JSON_VALUE(attributes, '$.root_agent_name')" " = @root_agent_name"
      )
      params.append(
          bigquery.ScalarQueryParameter(
              "root_agent_name",
              "STRING",
              self.root_agent_name,
          )
      )

    params.append(
        bigquery.ScalarQueryParameter(
            "trace_limit",
            "INT64",
            self.limit,
        )
    )

    where = " AND ".join(conditions) if conditions else "TRUE"
    return where, params


class _PinSentinel:
  """Singleton marker for selector/filter pin states (issue #359).

  ``None`` is ambiguous for identity dimensions: a resolved candidate
  can legitimately carry SQL ``NULL`` in ``user_id``,
  ``root_agent_name``, or ``experiment_id``, so "not pinned" and
  "pinned to NULL" need distinct representations. These sentinels are
  identity-compared (``is``) and survive ``copy``, ``deepcopy``, and
  ``pickle`` — a clone would silently change pin semantics (an
  ``UNSET`` clone reads as a concrete equality pin, an ``SQL_NULL``
  clone stops emitting ``IS NULL``).
  """

  __slots__ = ("_name",)

  def __init__(self, name: str) -> None:
    self._name = name

  def __repr__(self) -> str:
    return self._name

  def __copy__(self) -> "_PinSentinel":
    return self

  def __deepcopy__(self, memo: dict) -> "_PinSentinel":
    return self

  def __reduce__(self) -> tuple[Any, tuple[str]]:
    return (_resolve_pin_sentinel, (self._name,))


UNSET = _PinSentinel("UNSET")
"""Selector pin state: this dimension is not pinned at all."""

SQL_NULL = _PinSentinel("SQL_NULL")
"""Filter pin state: match only rows where this dimension is SQL NULL."""


def _resolve_pin_sentinel(name: str) -> _PinSentinel:
  """Resolve a pickled sentinel back to its module singleton."""
  return {"UNSET": UNSET, "SQL_NULL": SQL_NULL}[name]


_ScalarPin = Union[str, "_PinSentinel", None]

_LabelsInput = Union[dict[str, str], Sequence[tuple[str, str]], None]


def _canonicalize_labels(
    labels: _LabelsInput,
) -> Optional[tuple[tuple[str, str], ...]]:
  """Normalize custom labels into a sorted key/value tuple.

  JSON objects do not preserve key order, so scope equality and
  signatures must never depend on the order labels were supplied in.

  Empty label payloads normalize to ``None`` so ``{}``, ``()``, and
  ``None`` all mean "no labels" and compare (and sign) identically.

  Every entry is rebuilt as a fresh ``(key, value)`` tuple rather
  than kept as the caller's container element, so JSON round-tripped
  payloads (lists of two-item lists) normalize to the same hashable
  canonical form as native tuple input.

  Raises:
      TypeError: If an entry is not a two-item (key, value) pair, or
          a key or value is not a string. Values are never coerced,
          because silent coercion would let two distinct inputs
          collide in filters and scope signatures.
      ValueError: If the same key appears more than once. A duplicate
          key cannot round-trip through the dict-based
          ``TraceFilter.custom_labels`` surface without silently
          dropping one value.
  """
  if labels is None:
    return None
  items = list(labels.items() if isinstance(labels, dict) else labels)
  if not items:
    return None
  seen: set[str] = set()
  normalized: list[tuple[str, str]] = []
  for item in items:
    if not isinstance(item, (tuple, list)) or len(item) != 2:
      raise TypeError(
          "Each custom label entry must be a two-item (key, value)" " pair."
      )
    key, value = item
    if not isinstance(key, str) or not isinstance(value, str):
      raise TypeError("Custom label keys and values must be strings.")
    if key in seen:
      raise ValueError(f"Duplicate custom label key: {key!r}.")
    seen.add(key)
    normalized.append((key, value))
  return tuple(sorted(normalized))


@dataclass(frozen=True)
class TraceIdentity:
  """Intrinsic identity of a scoped multi-turn session (issue #359).

  ``session_id`` is the persistent conversation-thread identifier and
  may be reused across users, root agents, and evaluation passes; the
  intrinsic dimensions here keep colliding sessions distinguishable.
  ``trace_id`` is deliberately absent — it is the producer's
  OpenTelemetry execution identifier, and one multi-turn session may
  contain more than one trace ID.
  """

  session_id: str
  user_id: Optional[str] = None
  root_agent_name: Optional[str] = None

  def __post_init__(self) -> None:
    if not isinstance(self.session_id, str):
      raise TypeError("TraceIdentity.session_id must be a string.")
    for dim in ("user_id", "root_agent_name"):
      value = getattr(self, dim)
      if value is not None and not isinstance(value, str):
        # Non-string values (e.g. 1 vs True) can compare equal while
        # producing different serialized forms, silently merging or
        # splitting identities downstream.
        raise TypeError(f"TraceIdentity.{dim} must be a string or None.")


@dataclass(frozen=True)
class TraceScope:
  """Caller-selected scope pinning one recorded pass of a session.

  ``custom_labels`` is canonicalized to a sorted key/value tuple so
  equality, hashing, and :attr:`scope_signature` are independent of
  the order labels were supplied in.
  """

  experiment_id: Optional[str] = None
  custom_labels: _LabelsInput = None

  def __post_init__(self) -> None:
    if self.experiment_id is not None and not isinstance(
        self.experiment_id, str
    ):
      # 1 and True compare and hash equal but sign differently, so a
      # non-string experiment_id could dedupe two distinct scopes.
      raise TypeError("TraceScope.experiment_id must be a string or None.")
    object.__setattr__(
        self, "custom_labels", _canonicalize_labels(self.custom_labels)
    )

  @property
  def labels_dict(self) -> dict[str, str]:
    """Returns the canonical labels as a plain dict."""
    return dict(self.custom_labels) if self.custom_labels else {}

  @property
  def scope_signature(self) -> str:
    """Versioned canonical string signature for this scope.

    Two scopes with the same experiment and label payload always
    produce the same signature, so V0/V1 evaluation passes remain
    distinguishable even when the caller did not know their labels
    in advance.

    The encoding is a ``v1:`` prefix followed by compact JSON with
    sorted keys. JSON escaping makes the signature injective:
    delimiter characters or Unicode inside experiment IDs or label
    keys/values cannot make two distinct scopes collide, unlike a
    naive ``k=v;k=v`` concatenation.
    """
    payload = {
        "experiment_id": self.experiment_id,
        "custom_labels": (
            [[k, v] for k, v in self.custom_labels]
            if self.custom_labels
            else []
        ),
    }
    return "v1:" + json.dumps(payload, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class TraceSelector:
  """Optional caller pins for resolving a scoped session.

  The scalar identity/scope dimensions are three-state: the
  :data:`UNSET` default means "not pinned" (candidate resolution
  decides whether the unpinned population is unambiguous), an
  explicit ``None`` pins the dimension to SQL ``NULL``, and a string
  — including the empty string — pins it by equality. A resolved
  candidate whose ``user_id`` is NULL therefore retries as a
  different selector than a bare session-only request.

  ``custom_labels`` is a subset pin: selected traces must carry at
  least these labels. ``scope_signature`` is the exact-scope pin: it
  matches only candidates whose full resolved
  :attr:`TraceScope.scope_signature` equals it, so the selector for a
  ``{'run': 'v1'}`` pass cannot also match a
  ``{'run': 'v1', 'slice': '3'}`` pass, and an unlabeled candidate is
  distinguishable from an unpinned scope. Maps onto
  :class:`TraceFilter` via :meth:`to_trace_filter` so both surfaces
  share one predicate implementation; ``scope_signature`` has no SQL
  equivalent and is enforced during candidate resolution.
  """

  session_id: str
  user_id: _ScalarPin = UNSET
  root_agent_name: _ScalarPin = UNSET
  experiment_id: _ScalarPin = UNSET
  custom_labels: _LabelsInput = None
  scope_signature: Optional[str] = None

  def __post_init__(self) -> None:
    if not isinstance(self.session_id, str):
      raise TypeError("TraceSelector.session_id must be a string.")
    for dim in ("user_id", "root_agent_name", "experiment_id"):
      value = getattr(self, dim)
      if value is UNSET or value is None or isinstance(value, str):
        continue
      raise TypeError(
          f"TraceSelector.{dim} must be a string, None (pin to SQL"
          " NULL), or left UNSET."
      )
    if self.scope_signature is not None and not isinstance(
        self.scope_signature, str
    ):
      raise TypeError("TraceSelector.scope_signature must be a string or None.")
    object.__setattr__(
        self, "custom_labels", _canonicalize_labels(self.custom_labels)
    )

  @staticmethod
  def _pin_to_filter_value(value: _ScalarPin) -> _ScalarPin:
    """Map a selector pin onto the TraceFilter three-state encoding."""
    if value is UNSET:
      return None
    if value is None:
      return SQL_NULL
    return value

  def to_trace_filter(self, limit: int = 100) -> TraceFilter:
    """Build the equivalent list-level ``TraceFilter`` for these pins.

    Explicit ``None`` pins become NULL-safe :data:`SQL_NULL`
    predicates; :data:`UNSET` dimensions stay unfiltered. The
    ``scope_signature`` pin is not representable as a SQL predicate —
    label pins pre-filter rows to a superset and resolution applies
    the exact signature match.
    """
    return TraceFilter(
        session_ids=[self.session_id],
        user_id=self._pin_to_filter_value(self.user_id),
        root_agent_name=self._pin_to_filter_value(self.root_agent_name),
        experiment_id=self._pin_to_filter_value(self.experiment_id),
        custom_labels=dict(self.custom_labels) if self.custom_labels else None,
        limit=limit,
    )


@dataclass(frozen=True)
class ResolvedTraceSelector:
  """A fully resolved candidate: intrinsic identity plus scope."""

  identity: TraceIdentity
  scope: TraceScope = field(default_factory=TraceScope)

  @property
  def scope_signature(self) -> str:
    """Canonical scope signature of this candidate."""
    return self.scope.scope_signature

  def to_selector(self) -> TraceSelector:
    """Build the retry-ready :class:`TraceSelector` pinning this candidate.

    Every dimension is pinned — a resolved NULL ``user_id``,
    ``root_agent_name``, or ``experiment_id`` becomes an explicit
    ``None`` pin (matched NULL-safely), never an unpinned dimension,
    and ``scope_signature`` pins the exact resolved scope. The retry
    therefore selects only this candidate, not the original ambiguous
    population.
    """
    return TraceSelector(
        session_id=self.identity.session_id,
        user_id=self.identity.user_id,
        root_agent_name=self.identity.root_agent_name,
        experiment_id=self.scope.experiment_id,
        custom_labels=self.scope.custom_labels,
        scope_signature=self.scope_signature,
    )

  def to_retry_payload(self) -> dict[str, Any]:
    """JSON-safe candidate payload for structured error surfaces.

    ``selector`` holds exactly the :class:`TraceSelector` keyword
    arguments that pin this candidate (``TraceSelector(**selector)``
    reconstructs it); a JSON ``null`` there is an explicit
    pin-to-SQL-NULL, since resolved candidates leave no dimension
    unpinned. ``scope_signature`` is mirrored at the top level as the
    candidate's canonical scope key for correlation. Event content
    and judge context never appear here.
    """
    return {
        "selector": {
            "session_id": self.identity.session_id,
            "user_id": self.identity.user_id,
            "root_agent_name": self.identity.root_agent_name,
            "experiment_id": self.scope.experiment_id,
            "custom_labels": self.scope.labels_dict or None,
            "scope_signature": self.scope_signature,
        },
        "scope_signature": self.scope_signature,
    }


_RETRY_DIMENSIONS: tuple[str, ...] = (
    "user_id",
    "root_agent_name",
    "experiment_id",
    "custom_labels",
    # Label pins are subset matches, so custom_labels alone cannot
    # unambiguously retry an unlabeled or subset-labeled candidate;
    # scope_signature is the exact discriminator and is hinted
    # whenever candidate signatures differ.
    "scope_signature",
)


class AmbiguousSessionError(ValueError):
  """A singular session lookup matched more than one candidate.

  Subclasses ``ValueError`` so existing callers that catch the
  not-found ``ValueError`` from singular lookups degrade gracefully.

  The printable form is redacted: it carries only the candidate count
  and the names of the dimensions that would disambiguate a retry —
  never user IDs, label values, event content, or judge context. The
  full candidate selectors remain programmatically accessible via
  :attr:`candidates` and :meth:`to_dict` for structured surfaces that
  are allowed to return identity dimensions and scope signatures.
  """

  def __init__(self, candidates: Sequence[ResolvedTraceSelector]):
    deduped = tuple(dict.fromkeys(candidates))
    if len(deduped) < 2:
      raise ValueError(
          "AmbiguousSessionError requires at least two distinct"
          " candidates; a zero- or one-candidate population is not"
          " ambiguous."
      )
    if len({c.identity.session_id for c in deduped}) > 1:
      raise ValueError(
          "AmbiguousSessionError candidates must share one session_id;"
          " candidates from different sessions are distinct lookups,"
          " not an ambiguity."
      )
    self.candidates: tuple[ResolvedTraceSelector, ...] = deduped
    self.retry_dimensions: tuple[str, ...] = self._differing_dimensions(
        self.candidates
    )
    dims = ", ".join(self.retry_dimensions) or "scope"
    super().__init__(
        f"Ambiguous singular session lookup: {len(self.candidates)}"
        f" candidates match. Retry with an explicit selector for: {dims}."
    )

  def __reduce__(self) -> tuple[Any, tuple[Any]]:
    # Exception.args holds the redacted message, so default
    # copy/deepcopy/pickle reconstruction would call __init__ with a
    # string instead of the candidate sequence. Rebuild from the
    # stored candidates instead.
    return (self.__class__, (self.candidates,))

  @staticmethod
  def _differing_dimensions(
      candidates: Sequence[ResolvedTraceSelector],
  ) -> tuple[str, ...]:
    """Names of the dimensions whose values differ across candidates."""
    extractors = {
        "user_id": lambda c: c.identity.user_id,
        "root_agent_name": lambda c: c.identity.root_agent_name,
        "experiment_id": lambda c: c.scope.experiment_id,
        "custom_labels": lambda c: c.scope.custom_labels,
        "scope_signature": lambda c: c.scope_signature,
    }
    differing = []
    for dim in _RETRY_DIMENSIONS:
      values = {extractors[dim](c) for c in candidates}
      if len(values) > 1:
        differing.append(dim)
    return tuple(differing)

  def to_dict(self) -> dict[str, Any]:
    """Structured, JSON-safe payload for agent-facing surfaces.

    Each candidate entry is the exact shape produced by
    :meth:`ResolvedTraceSelector.to_retry_payload`: a ``selector``
    dict of :class:`TraceSelector` keyword arguments that retries the
    lookup in one step, plus the candidate's ``scope_signature``.
    Carries candidate identity dimensions and scope signatures only;
    event content and judge context never appear here.
    """
    return {
        "error": "ambiguous_session",
        "candidate_count": len(self.candidates),
        "retry_dimensions": list(self.retry_dimensions),
        "candidates": [c.to_retry_payload() for c in self.candidates],
    }


def resolve_singular_candidate(
    candidates: Sequence[ResolvedTraceSelector],
) -> ResolvedTraceSelector:
  """Validate that a singular (legacy session-only) lookup is unambiguous.

  Args:
      candidates: The resolved candidates matching the caller's key.

  Returns:
      The single matching candidate. Duplicate occurrences of one
      resolved selector count as a single candidate, so a repeated
      row from candidate discovery never turns an unambiguous lookup
      into an ambiguity error.

  Raises:
      ValueError: If no candidate matches.
      AmbiguousSessionError: If more than one distinct candidate
          matches. No implicit fallback (such as newest-wins) is ever
          applied.
  """
  resolved = list(dict.fromkeys(candidates))
  if not resolved:
    raise ValueError("No candidates match the requested session.")
  if len(resolved) > 1:
    raise AmbiguousSessionError(candidates=resolved)
  return resolved[0]


@dataclass
class Trace:
  """A complete agent trace for a session.

  Contains all spans (events) for the session and provides
  visualization via the :meth:`render` method.

  ``identity`` and ``scope`` are additive (issue #359): they carry the
  resolved intrinsic identity and selected scope so two traces sharing
  a ``session_id`` stay distinguishable after serialization. The
  legacy scalar fields keep their names and meanings.

  When ``identity`` is present it is the single identity authority:
  the legacy ``session_id``/``user_id`` scalars are mirrors of it.
  The invariant holds for the object's whole lifetime, not just
  construction — assigning a contradictory ``session_id`` or
  ``user_id`` raises, and attaching an identity to a trace with an
  unset ``user_id`` backfills the mirror. An identity may be attached
  late only while none exists; once attached it cannot be replaced or
  detached (only re-assigned an equal value), so a trace can never be
  retagged to a different identity. Downstream code keyed on either
  surface therefore never sees two different identities for one
  trace. To change identity, build a new ``Trace``.
  """

  trace_id: str
  session_id: str
  spans: list[Span] = field(default_factory=list)
  user_id: Optional[str] = None
  start_time: Optional[datetime] = None
  end_time: Optional[datetime] = None
  total_latency_ms: Optional[float] = None
  identity: Optional[TraceIdentity] = None
  scope: Optional[TraceScope] = None

  def __setattr__(self, name: str, value: Any) -> None:
    if name in ("session_id", "user_id", "identity"):
      self._check_identity_write(name, value)
    object.__setattr__(self, name, value)
    if name == "identity" and value is not None and self.user_id is None:
      # Keep the legacy mirror in sync when an identity is attached
      # before user_id is known (dataclass __init__ assigns user_id
      # first, and client code may attach identity after construction).
      object.__setattr__(self, "user_id", value.user_id)

  def _check_identity_write(self, name: str, value: Any) -> None:
    """Reject writes that would desynchronize identity and mirrors."""
    if name == "identity":
      current = getattr(self, "identity", None)
      if current is not None and value != current:
        # Replacement (including a NULL-user -> named-user retag or a
        # root-agent-only swap) and detachment would both let the
        # trace change identity after the fact; only an equal
        # idempotent re-assignment is allowed.
        raise ValueError(
            "Trace.identity cannot be replaced or detached once"
            " attached; build a new Trace for a different identity."
        )
    identity = value if name == "identity" else getattr(self, "identity", None)
    if identity is None:
      return
    session_id = (
        value if name == "session_id" else getattr(self, "session_id", None)
    )
    if session_id != identity.session_id:
      raise ValueError(
          "Trace.identity.session_id contradicts Trace.session_id;"
          " the identity is authoritative and mirrored fields must"
          " match it."
      )
    user_id = value if name == "user_id" else getattr(self, "user_id", None)
    if user_id is not None and user_id != identity.user_id:
      raise ValueError(
          "Trace.identity.user_id contradicts Trace.user_id; the"
          " identity is authoritative and mirrored fields must"
          " match it."
      )
    if name == "user_id" and value is None and identity.user_id is not None:
      raise ValueError(
          "Trace.user_id mirrors Trace.identity.user_id and cannot be"
          " cleared while an identity with a non-NULL user is"
          " attached."
      )

  def _build_tree(self) -> list[Span]:
    """Builds a tree of spans using parent_span_id relationships."""
    by_id: dict[str, Span] = {}
    for span in self.spans:
      if span.span_id:
        by_id[span.span_id] = span
      span.children = []

    roots: list[Span] = []
    for span in self.spans:
      parent = span.parent_span_id
      if parent and parent in by_id:
        by_id[parent].children.append(span)
      else:
        roots.append(span)

    return roots

  def render(self, format: str = "tree", color: bool = False) -> str:
    """Renders the trace as a hierarchical DAG view.

    This generates a tree representation of the agent's
    reasoning steps:
    ``User Input -> Agent Thought -> Tool Call -> Response``

    Multimodal content parts show their MIME type and URI.

    Args:
        format: Render format. Currently supports "tree".
        color: When ``True``, wrap error markers and warning
            markers in ANSI color codes (red and yellow
            respectively). Default ``False`` emits plain text
            suitable for any output target. Enable this in TTY
            contexts (terminal sessions) for faster visual
            scanning of failures in large traces.

    Returns:
        A string containing the rendered trace. Also printed
        to stdout for notebook/terminal use.
    """
    roots = self._build_tree()
    lines: list[str] = []

    header = f"Trace: {self.trace_id}"
    if self.session_id:
      header += f" | Session: {self.session_id}"
    if self.total_latency_ms is not None:
      header += f" | {self.total_latency_ms:.0f}ms"
    lines.append(header)
    lines.append("=" * len(header))

    if not roots:
      # Flat rendering when no span IDs exist
      for span in self.spans:
        self._render_flat_span(span, lines, color=color)
    else:
      for root in roots:
        self._render_span(root, lines, prefix="", is_last=True, color=color)

    output = "\n".join(lines)
    print(output)
    return output

  def _render_span(
      self,
      span: Span,
      lines: list[str],
      prefix: str,
      is_last: bool,
      color: bool = False,
  ) -> None:
    """Recursively renders a span and its children as a tree."""
    connector = "\u2514\u2500 " if is_last else "\u251c\u2500 "

    if span.is_error:
      status_icon = _colorize("\u2717", _ANSI_RED, color)
    elif span.subtree_has_error:
      # Propagate error visibility: mark parents whose subtree
      # contains an error so the failure is visible at every level.
      status_icon = _colorize("\u26a0", _ANSI_YELLOW, color)
    else:
      status_icon = "\u2713"

    latency_str = ""
    if span.latency_ms is not None:
      latency_str = f" ({span.latency_ms:.0f}ms)"

    line = f"{prefix}{connector}[{status_icon}] {span.label}"
    line += latency_str

    summary = span.summary
    if summary:
      line += f" - {summary}"

    lines.append(line)

    # Multimodal content parts
    child_prefix = prefix + ("   " if is_last else "\u2502  ")
    for part in span.content_parts:
      part_uri = part.uri
      if not part_uri and part.object_ref:
        part_uri = part.object_ref.uri
      if part_uri:
        lines.append(
            f"{child_prefix}   [{part.mime_type or 'file'}] {part_uri}"
        )

    for i, child in enumerate(span.children):
      self._render_span(
          child,
          lines,
          child_prefix,
          is_last=(i == len(span.children) - 1),
          color=color,
      )

  def _render_flat_span(
      self,
      span: Span,
      lines: list[str],
      color: bool = False,
  ) -> None:
    """Renders a single span without tree structure."""
    if span.is_error:
      status_icon = _colorize("\u2717", _ANSI_RED, color)
    else:
      status_icon = "\u2713"
    latency = ""
    if span.latency_ms is not None:
      latency = f" ({span.latency_ms:.0f}ms)"

    summary = span.summary
    detail = f" - {summary}" if summary else ""
    lines.append(f"  [{status_icon}] {span.label}{latency}{detail}")

  @property
  def tool_calls(self) -> list[dict[str, Any]]:
    """Extracts tool calls from the trace."""
    calls = []
    starts: dict[str, Span] = {}

    for span in self.spans:
      if span.event_type == "TOOL_STARTING":
        key = span.span_id or span.content.get("tool", "")
        starts[key] = span
      elif span.event_type in ("TOOL_COMPLETED", "TOOL_ERROR"):
        key = span.span_id or span.content.get("tool", "")
        start = starts.pop(key, None)
        origin = span.content.get("tool_origin") or (
            start.content.get("tool_origin") if start else None
        )
        entry = {
            "tool_name": span.content.get("tool", "unknown"),
            "args": start.content.get("args", {}) if start else {},
            "result": span.content.get("result"),
            "status": span.status,
            "error": span.error_message,
            "latency_ms": span.latency_ms,
        }
        if origin:
          entry["tool_origin"] = origin
        calls.append(entry)

    return calls

  @property
  def final_response(self) -> Optional[str]:
    """Extracts the final agent response text.

    Checks LLM_RESPONSE first (the ADK plugin always populates
    ``content.response`` there), then falls back to
    AGENT_COMPLETED for backward compatibility.
    """
    for span in reversed(self.spans):
      if span.event_type == "LLM_RESPONSE":
        c = span.content
        if isinstance(c, dict):
          result = c.get("response")
          if result:
            return (
                _unwrap_text_field(result)
                if isinstance(result, str)
                else result
            )
        elif c:
          return _unwrap_text_field(str(c))

    for span in reversed(self.spans):
      if span.event_type == "AGENT_COMPLETED":
        c = span.content
        if isinstance(c, dict):
          result = c.get("response") or c.get("text_summary")
          if result:
            return (
                _unwrap_text_field(result)
                if isinstance(result, str)
                else result
            )
        elif c:
          return _unwrap_text_field(str(c))
    return None

  @property
  def error_spans(self) -> list[Span]:
    """Returns all spans that indicate an error."""
    return [s for s in self.spans if s.is_error]

  def errors(self) -> list[dict[str, Any]]:
    """Returns error spans with full failure context.

    Each entry contains the span's event_type, agent, tool name,
    error_message, latency, and span_id for easy debugging.

    Returns:
        List of dicts describing each error.
    """
    results = []
    for span in self.spans:
      if span.is_error:
        entry: dict[str, Any] = {
            "event_type": span.event_type,
            "agent": span.agent,
            "span_id": span.span_id,
            "error_message": span.error_message,
            "failure_context": span.failure_context,
            "latency_ms": span.latency_ms,
            "timestamp": span.timestamp,
        }
        tool = span.content.get("tool")
        if tool:
          entry["tool"] = tool
        origin = span.content.get("tool_origin")
        if origin:
          entry["tool_origin"] = origin
        results.append(entry)
    return results
