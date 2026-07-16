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

import collections.abc
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from enum import Enum
import functools
import json
import logging
import re
from types import MappingProxyType
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


def _validated_filter_pin(name: str, value: Any) -> Any:
  """Validate one TraceFilter scalar pin (tri-state, exact string).

  Applied on every attribute write AND again at the SQL boundary,
  because ``vars(filt)`` writes bypass ``__setattr__`` and would
  otherwise bind a non-string (e.g. the UNSET sentinel) as a STRING
  query parameter that fails at submission.
  """
  if not (value is None or value is SQL_NULL or isinstance(value, str)):
    raise TypeError(
        f"TraceFilter.{name} must be a string, None (unfiltered),"
        " or SQL_NULL."
    )
  if isinstance(value, str):
    value = _exact_str(value)
  return value


def _is_unaddressable_label_key(key: str) -> bool:
  """True if BigQuery JSONPath cannot encode this member key.

  Live-verified grammar: a run of consecutive backslashes immediately
  before a double quote or at the end of the key is invalid only when
  the run length is odd — even runs parse and match (verified through
  length 4 in both contexts). Interior backslash runs not adjacent to
  a quote are always literal.
  """
  i, n = 0, len(key)
  while i < n:
    if key[i] == "\\":
      start = i
      while i < n and key[i] == "\\":
        i += 1
      if (i - start) % 2 == 1 and (i == n or key[i] == '"'):
        return True
    else:
      i += 1
  return False


def _jsonpath_member_segment(key: str) -> str:
  """Encode a label key as a quoted JSONPath member segment.

  Label keys are user data: dots, brackets, quotes, or an empty key
  must select the literal member instead of changing the path
  structure, so every key is wrapped in double quotes and appended to
  ``$.custom_tags.`` as one segment.

  BigQuery's JSONPath grammar is NOT JSON-string escaping (all
  verified against live BigQuery): inside a quoted member only the
  double quote is escaped (``\\"``); backslashes — single, doubled,
  or leading — are matched literally, so doubling them silently
  matches nothing.

  That grammar leaves some key shapes unrepresentable: an
  odd-length run of backslashes immediately before a double quote or
  at the end of the key merges with the following quote and BigQuery
  aborts with ``Invalid token in JSONPath`` (even-length runs parse
  and match). Those keys are rejected here — and earlier, at
  filter-label validation — instead of submitting a query that
  errors.

  Raises:
      ValueError: If the key contains an odd-length backslash run
          immediately before a double quote or at the end of the key.
  """
  if _is_unaddressable_label_key(key):
    raise ValueError(
        f"Custom label key {key!r} cannot be addressed by BigQuery"
        " JSONPath: an odd-length backslash run immediately before a"
        " double quote or at the end of the key has no valid"
        " quoted-member encoding."
    )
  escaped = key.replace('"', '\\"')
  return f'"{escaped}"'


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
  user_id: _FilterPin = None
  session_ids: Optional[list[str]] = None
  experiment_id: _FilterPin = None
  has_error: Optional[bool] = None
  error_type: Optional[str] = None
  custom_labels: Optional[dict[str, str]] = None
  min_latency_ms: Optional[float] = None
  max_latency_ms: Optional[float] = None
  event_types: Optional[list[str]] = None
  tool_origin: Optional[str] = None
  root_agent_name: _FilterPin = None
  limit: int = 100

  def __setattr__(self, name: str, value: Any) -> None:
    # Validated on every write, not just construction: the filter is
    # mutable, and a post-construction `f.user_id = UNSET` would
    # otherwise bind the sentinel object as an equality query
    # parameter. UNSET (and any foreign value) belongs to the
    # selector surface.
    if name in ("user_id", "root_agent_name", "experiment_id"):
      value = _validated_filter_pin(name, value)
    if name == "custom_labels" and value is not None:
      # Direct filter labels feed JSONPath construction; a str
      # subclass overriding replace() could redirect the selected
      # member, so keys and values are copied to exact built-in
      # strings (with post-normalization duplicate detection) and
      # stored in a validating mutable dict so ordinary in-place
      # writes stay checked while dict-style mutation keeps working.
      # Skip rewrapping when the assigned value IS this field's
      # current validating container: augmented assignment (|=)
      # stores the returned self back through here, and copying it
      # would detach existing aliases and make repeated updates
      # quadratic. Externally assigned containers are still copied.
      if value is not getattr(self, "custom_labels", None):
        value = _ValidatedLabels(_normalized_filter_labels(value))
    if name == "session_ids" and value is not None:
      # Same identity-skip as custom_labels, for += on the list.
      if value is not getattr(self, "session_ids", None):
        value = _normalized_session_ids(value)
    object.__setattr__(self, name, value)

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
    # Boundary revalidation: __dict__-level writes bypass __setattr__,
    # and a foreign value must not reach parameter construction.
    user_id = _validated_filter_pin("user_id", self.user_id)
    root_agent_name = _validated_filter_pin(
        "root_agent_name", self.root_agent_name
    )
    experiment_id = _validated_filter_pin("experiment_id", self.experiment_id)
    if user_id is SQL_NULL:
      conditions.append("user_id IS NULL")
    elif user_id is not None:
      conditions.append("user_id = @user_id")
      params.append(
          bigquery.ScalarQueryParameter(
              "user_id",
              "STRING",
              user_id,
          )
      )
    # Normalize BEFORE testing truthiness: a falsey container
    # subclass injected through vars(filt) could hide real contents
    # from an `if self.session_ids` check and silently emit an
    # unfiltered predicate. The normalizers read real storage through
    # trusted descriptors, so the snapshot's truthiness is honest.
    session_ids = (
        _normalized_session_ids(self.session_ids)
        if self.session_ids is not None
        else None
    )
    if session_ids:
      conditions.append("session_id IN UNNEST(@session_ids)")
      params.append(
          bigquery.ArrayQueryParameter(
              "session_ids",
              "STRING",
              session_ids,
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
    if experiment_id is SQL_NULL:
      conditions.append("JSON_VALUE(attributes, '$.experiment_id') IS NULL")
    elif experiment_id is not None:
      conditions.append(
          "JSON_VALUE(attributes, '$.experiment_id')" " = @experiment_id"
      )
      params.append(
          bigquery.ScalarQueryParameter(
              "experiment_id",
              "STRING",
              experiment_id,
          )
      )
    # Same normalize-before-truthiness rule as session_ids: the
    # trusted snapshot also revalidates low-level mutation (e.g.
    # dict.__setitem__ bypassing the validating subclass).
    labels = (
        _normalized_filter_labels(self.custom_labels)
        if self.custom_labels is not None
        else None
    )
    if labels:
      for i, (key, value) in enumerate(labels.items()):
        param_key = f"label_key_{i}"
        param_val = f"label_val_{i}"
        conditions.append(
            f"JSON_VALUE(attributes,"
            f" CONCAT('$.custom_tags.', @{param_key}))"
            f" = @{param_val}"
        )
        params.append(
            bigquery.ScalarQueryParameter(
                param_key, "STRING", _jsonpath_member_segment(key)
            )
        )
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
    if root_agent_name is SQL_NULL:
      conditions.append("JSON_VALUE(attributes, '$.root_agent_name') IS NULL")
    elif root_agent_name is not None:
      conditions.append(
          "JSON_VALUE(attributes, '$.root_agent_name')" " = @root_agent_name"
      )
      params.append(
          bigquery.ScalarQueryParameter(
              "root_agent_name",
              "STRING",
              root_agent_name,
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

  def row_scope_where(self, alias: str = "e") -> str:
    """Alias-qualified row-scope predicates for the outer row fetch.

    Issue #359: ``to_sql_conditions()`` selects candidate SESSIONS;
    the composite anchor join then fetches every anchored row, so
    caller-selected scope (custom labels and experiment) must be
    re-applied to the fetched rows or a reused session id merges
    foreign evaluation passes into one trace.

    Semantics are conflict-excluding, per the live-data
    characterization recorded on issue #361: real sessions carry
    untagged rows and per-row enrichment keys alongside a consistent
    base payload, so a pinned label ``k=v`` excludes rows whose ``k``
    carries a DIFFERENT non-NULL value (foreign-pass rows) while
    keeping rows that lack ``k`` (shared conversation rows) —
    preserving complete-trace semantics (R6) within the selected
    scope. The emitted fragment reuses the query parameters that
    ``to_sql_conditions()`` already declares (``@experiment_id``,
    ``@label_key_N``/``@label_val_N``), so both must be rendered into
    the same query.

    Args:
        alias: Table alias of the outer event-row fetch.

    Returns:
        A SQL boolean expression (``TRUE`` when no row-scope
        dimension is pinned).
    """
    conditions = []
    experiment_id = _validated_filter_pin("experiment_id", self.experiment_id)
    if experiment_id is SQL_NULL:
      conditions.append(
          f"JSON_VALUE({alias}.attributes, '$.experiment_id') IS NULL"
      )
    elif experiment_id is not None:
      conditions.append(
          f"(JSON_VALUE({alias}.attributes, '$.experiment_id')"
          " = @experiment_id"
          f" OR JSON_VALUE({alias}.attributes, '$.experiment_id')"
          " IS NULL)"
      )
    labels = (
        _normalized_filter_labels(self.custom_labels)
        if self.custom_labels is not None
        else None
    )
    if labels:
      for i in range(len(labels)):
        conditions.append(
            f"(JSON_VALUE({alias}.attributes,"
            f" CONCAT('$.custom_tags.', @label_key_{i}))"
            f" = @label_val_{i}"
            f" OR JSON_VALUE({alias}.attributes,"
            f" CONCAT('$.custom_tags.', @label_key_{i})) IS NULL)"
        )
    return " AND ".join(conditions) if conditions else "TRUE"


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
    # One-shot: __setattr__ raises, but a direct second __init__ call
    # would rewrite process-global display state through
    # object.__setattr__; reject it, and validate the canonical name
    # so a first call can never install a foreign value either.
    try:
      object.__getattribute__(self, "_name")
    except AttributeError:
      pass
    else:
      raise TypeError("Pin sentinels are immutable.")
    if type(name) is not str or name not in ("UNSET", "SQL_NULL"):
      raise ValueError("Unknown pin sentinel name.")
    object.__setattr__(self, "_name", name)

  def __setattr__(self, name: str, value: Any) -> None:
    # Pickle restoration and wire encoding are derived from singleton
    # identity, but a writable display name would still let one write
    # confuse process-global diagnostics.
    raise AttributeError("Pin sentinels are immutable.")

  def __delattr__(self, name: str) -> None:
    raise AttributeError("Pin sentinels are immutable.")

  def __repr__(self) -> str:
    return self._name

  def __copy__(self) -> "_PinSentinel":
    return self

  def __deepcopy__(self, memo: dict) -> "_PinSentinel":
    return self

  def __reduce__(self) -> tuple[Any, tuple[str]]:
    return (_resolve_pin_sentinel, (_pin_sentinel_name(self),))


class _UnsetType(_PinSentinel):
  """Type of :data:`UNSET` — valid only on the selector surface."""


class _SqlNullType(_PinSentinel):
  """Type of :data:`SQL_NULL` — valid only on the filter surface."""


UNSET = _UnsetType("UNSET")
"""Selector pin state: this dimension is not pinned at all."""

SQL_NULL = _SqlNullType("SQL_NULL")
"""Filter pin state: match only rows where this dimension is SQL NULL."""


def _resolve_pin_sentinel(name: str) -> _PinSentinel:
  """Resolve a pickled sentinel back to its module singleton."""
  return {"UNSET": UNSET, "SQL_NULL": SQL_NULL}[name]


def _pin_sentinel_name(sentinel: _PinSentinel) -> str:
  """Canonical sentinel name derived from singleton identity.

  Pickle and wire encodings must not trust mutable display state;
  only the two module singletons have names.
  """
  if sentinel is UNSET:
    return "UNSET"
  if sentinel is SQL_NULL:
    return "SQL_NULL"
  raise ValueError("Unknown pin sentinel.")


def _exact_str(value: str) -> str:
  """Copy a ``str`` (or subclass) into an exact built-in ``str``.

  ``str`` subclasses can override ``__eq__``/``__hash__``/``__ne__``,
  and identity comparison, deduplication, and mirror validation must
  never run on caller-controlled semantics. ``str.join`` copies the
  character data through internal APIs without invoking any subclass
  hook.

  Surrogate code units are rejected: Python distinguishes an astral
  scalar from the equivalent explicit high+low surrogate string, but
  JSON escaping maps both to the same wire bytes, so two distinct
  accepted scopes would collapse into one signature and retry
  selector after a round trip. Such strings are also not
  representable in BigQuery, so no real identity can contain them.

  Raises:
      ValueError: If the string contains surrogate code units.
  """
  copied = value if type(value) is str else "".join((value,))
  try:
    copied.encode("utf-8")
  except UnicodeEncodeError as exc:
    raise ValueError(
        "Identity-contract strings must not contain surrogate code"
        " units; they cannot round-trip through JSON or BigQuery"
        " without collapsing distinct values."
    ) from exc
  return copied


def _validated_label_item(key: Any, value: Any) -> tuple[str, str]:
  """Validate and normalize one filter-label key/value pair.

  Keys that BigQuery JSONPath cannot address (trailing backslash or
  backslash-before-quote) are rejected here so the failure happens at
  assignment time instead of aborting the query at submission.
  """
  if not isinstance(key, str) or not isinstance(value, str):
    raise TypeError(
        "TraceFilter.custom_labels keys and values must be strings."
    )
  key = _exact_str(key)
  if _is_unaddressable_label_key(key):
    raise ValueError(
        f"Custom label key {key!r} cannot be addressed by BigQuery"
        " JSONPath: an odd-length backslash run immediately before a"
        " double quote or at the end of the key has no valid"
        " quoted-member encoding."
    )
  return key, _exact_str(value)


class _NonStringOperand:
  """Marker: a read/removal operand that can never match stored data.

  Hash and equality are identity-based, so delegating it to the
  native dict/list method produces the method's own miss behavior
  (False/0/KeyError/ValueError) and native arity validation without
  invoking any caller-controlled hook — including ``__repr__``, which
  this class overrides so even error formatting stays safe.
  """

  __slots__ = ()

  def __repr__(self) -> str:
    return "<non-string operand>"


_NON_STRING_OPERAND = _NonStringOperand()

# Trusted type descriptors: attribute access on a class consults its
# metaclass, so hostile metaclass properties could hijack __name__,
# __mro__, or hash-slot reads. These getset descriptors bypass the
# metaclass entirely.
_TYPE_NAME = type.__dict__["__name__"]
_TYPE_MRO = type.__dict__["__mro__"]
_TYPE_DICT = type.__dict__["__dict__"]


def _safe_type_name(obj: Any) -> str:
  """Real class name of ``obj`` without metaclass attribute hooks."""
  return _TYPE_NAME.__get__(type(obj))


def _class_hash_slot(tp: type) -> Any:
  """Resolve ``__hash__`` along the real MRO without metaclass hooks."""
  for klass in _TYPE_MRO.__get__(tp):
    class_dict = _TYPE_DICT.__get__(klass)
    if "__hash__" in class_dict:
      return class_dict["__hash__"]
  return None


# Hash slots that can never fail or run caller code when invoked by
# the native dict. A class hash slot being non-None does NOT prove an
# instance is hashable (a tuple holding a list and a writable
# memoryview both fail when actually hashed), and custom __hash__
# implementations are caller code; only these unconditional builtin
# slots may reach native hashing.
# Tuple + identity comparison: frozenset membership would hash (and
# possibly compare) the foreign slot object itself, executing caller
# code during classification.
_UNCONDITIONAL_HASH_SLOTS = tuple(
    slot
    for slot in (
        object.__dict__.get("__hash__"),
        int.__dict__.get("__hash__"),
        float.__dict__.get("__hash__"),
        complex.__dict__.get("__hash__"),
        bytes.__dict__.get("__hash__"),
        frozenset.__dict__.get("__hash__"),
        type(None).__dict__.get("__hash__"),
    )
    if slot is not None
)


def _trusted_sequence_snapshot(source: Any) -> Any:
  """Copy a real list/tuple (subclass) through trusted descriptors.

  A list or tuple subclass can override ``__iter__``/``__len__``/
  ``__getitem__`` to present contents that differ from its real
  storage, silently erasing identity or scope pins during
  normalization. ``list.copy`` and ``tuple.__iter__`` read the actual
  C-level storage without invoking any subclass hook. Non-list/tuple
  values pass through for the caller's own validation.
  """
  if isinstance(source, list):
    return list.copy(source)
  if isinstance(source, tuple):
    return list(tuple.__iter__(source))
  return source


def _exact_lookup_str(value: Any, require_hashable: bool = False) -> Any:
  """Copy a str (or subclass) read-operand into an exact ``str``.

  Removal and membership operands must not drive comparisons with
  caller-controlled ``__eq__``/``__hash__`` — an equality-overriding
  operand could delete or match a different, valid entry, which
  boundary revalidation cannot detect afterwards. Detection uses the
  real ``type()``, never ``isinstance()``, because a spoofed
  ``__class__`` property would otherwise smuggle a non-string into
  the str-subclass copy path.

  Non-string operands map to :data:`_NON_STRING_OPERAND` and are
  delegated to the native method, which yields its own miss behavior.
  With ``require_hashable`` (the mapping surfaces), unhashable
  operands raise ``TypeError`` exactly like a plain dict — detected
  through the class's real hash slot so no caller hook runs; plain
  lists compare rather than hash, so list surfaces keep miss
  semantics. Surrogates are not rejected here because a read of a
  never-storable key should simply miss.
  """
  tp = type(value)
  if tp is str:
    return value
  if issubclass(tp, str):
    return "".join((value,))
  if require_hashable:
    slot = _class_hash_slot(tp)
    if slot is None:
      raise TypeError(f"unhashable type: {_safe_type_name(value)!r}")
    if not any(slot is trusted for trusted in _UNCONDITIONAL_HASH_SLOTS):
      # Conditional (tuple/memoryview) or custom hash implementations
      # would have to actually execute to know whether they hash;
      # reject them without running caller code. This deliberately
      # narrows the native contract for exotic key types.
      raise TypeError(
          "TraceFilter.custom_labels keys are strings; non-string"
          " operands with conditional or custom __hash__"
          f" implementations are rejected: {_safe_type_name(value)!r}."
      )
  return _NON_STRING_OPERAND


class _SafeSetComparisonsMixin:
  """Trusted rich comparisons for the set-like label views.

  The inherited ``collections.abc.Set`` comparisons test membership
  inside the caller's counterpart set, whose hostile elements would
  drive hashing/equality with their own hooks and could report false
  equality. Each comparison instead normalizes the counterpart once
  (exact strings via each view's element normalizer; anything else is
  a foreign element that can never match this view's contents) and
  compares plain trusted sets. Cardinality is preserved: distinct
  counterpart elements that collapse to one normalized element make
  the counterpart strictly larger, never equal.
  """

  __slots__ = ()

  def _normalized_counterpart(self, other: Any) -> Any:
    if not isinstance(other, collections.abc.Set):
      return None
    # Trusted reads for real set/frozenset (sub)classes: overridden
    # __iter__/__len__ could present contents differing from the real
    # storage and fake equality with this view.
    if isinstance(other, frozenset):
      elements = frozenset.__iter__(other)
      size = frozenset.__len__(other)
    elif isinstance(other, set):
      elements = set.__iter__(other)
      size = set.__len__(other)
    else:
      elements = iter(other)
      size = len(other)
    exact: set = set()
    foreign = 0
    for element in elements:
      normalized = self._normalize_element(element)
      if normalized is _NON_STRING_OPERAND:
        foreign += 1
      else:
        exact.add(normalized)
    return exact, foreign, size

  def _own_elements(self) -> set:
    return set(iter(self))

  def __eq__(self, other: Any) -> Any:
    counterpart = self._normalized_counterpart(other)
    if counterpart is None:
      return NotImplemented
    exact, foreign, size = counterpart
    return foreign == 0 and len(exact) == size and self._own_elements() == exact

  def __ne__(self, other: Any) -> Any:
    result = self.__eq__(other)
    return result if result is NotImplemented else not result

  def __le__(self, other: Any) -> Any:
    counterpart = self._normalized_counterpart(other)
    if counterpart is None:
      return NotImplemented
    exact, _, _ = counterpart
    return self._own_elements() <= exact

  def __lt__(self, other: Any) -> Any:
    counterpart = self._normalized_counterpart(other)
    if counterpart is None:
      return NotImplemented
    exact, foreign, _ = counterpart
    own = self._own_elements()
    return own <= exact and (foreign > 0 or own != exact)

  def __ge__(self, other: Any) -> Any:
    counterpart = self._normalized_counterpart(other)
    if counterpart is None:
      return NotImplemented
    exact, foreign, size = counterpart
    return foreign == 0 and len(exact) == size and exact <= self._own_elements()

  def __gt__(self, other: Any) -> Any:
    counterpart = self._normalized_counterpart(other)
    if counterpart is None:
      return NotImplemented
    exact, foreign, size = counterpart
    own = self._own_elements()
    return foreign == 0 and len(exact) == size and exact <= own and exact != own

  def isdisjoint(self, other: Any) -> bool:
    counterpart = self._normalized_counterpart(other)
    if counterpart is None:
      return not any(element in self for element in iter(other))
    exact, _, _ = counterpart
    return self._own_elements().isdisjoint(exact)

  def _algebra_counterpart(self, other: Any, keep_foreign: bool) -> Any:
    """Normalized elements for set algebra, failing closed as needed.

    Native dict views accept ANY iterable operand in set algebra
    (lists, tuples, dicts, generators), not just sets, so this
    accepts the same — reading real container (sub)classes through
    trusted descriptors. Results of ``&``/``-`` are subsets of this
    view's own trusted elements, so foreign counterpart elements
    simply cannot match and are ignored. Results of ``|``/``^`` and
    the reflected ``-`` would have to CONTAIN the foreign elements;
    emitting hostile objects in a trusted result is refused instead.
    """
    if isinstance(other, frozenset):
      elements: Any = frozenset.__iter__(other)
    elif isinstance(other, set):
      elements = set.__iter__(other)
    elif isinstance(other, dict):
      # Native view algebra iterates a dict operand's keys.
      elements = dict.keys(other)
    elif isinstance(other, (list, tuple)):
      elements = _trusted_sequence_snapshot(other)
    else:
      try:
        elements = iter(other)
      except TypeError:
        return None
    exact: set = set()
    foreign = 0
    for element in elements:
      normalized = self._normalize_element(element)
      if normalized is _NON_STRING_OPERAND:
        foreign += 1
      else:
        exact.add(normalized)
    if foreign and not keep_foreign:
      raise TypeError(
          "set operation would include non-conforming elements from"
          " the counterpart; the trusted view refuses to emit them."
      )
    return exact

  def __and__(self, other: Any) -> Any:
    exact = self._algebra_counterpart(other, keep_foreign=True)
    if exact is None:
      return NotImplemented
    return self._own_elements() & exact

  __rand__ = __and__

  def __sub__(self, other: Any) -> Any:
    exact = self._algebra_counterpart(other, keep_foreign=True)
    if exact is None:
      return NotImplemented
    return self._own_elements() - exact

  def __rsub__(self, other: Any) -> Any:
    exact = self._algebra_counterpart(other, keep_foreign=False)
    if exact is None:
      return NotImplemented
    return exact - self._own_elements()

  def __or__(self, other: Any) -> Any:
    exact = self._algebra_counterpart(other, keep_foreign=False)
    if exact is None:
      return NotImplemented
    return self._own_elements() | exact

  __ror__ = __or__

  def __xor__(self, other: Any) -> Any:
    exact = self._algebra_counterpart(other, keep_foreign=False)
    if exact is None:
      return NotImplemented
    return self._own_elements() ^ exact

  __rxor__ = __xor__

  __hash__ = None  # type: ignore[assignment]


class _SafeKeysView(_SafeSetComparisonsMixin, collections.abc.KeysView):
  """Live keys view with operand-normalized membership.

  Backed directly by the validating container — live, allocation-free
  iteration, with ``reversed()`` and ``.mapping`` supported like
  native dict views — while membership and set comparisons never run
  caller comparison hooks.
  """

  @staticmethod
  def _normalize_element(element: Any) -> Any:
    return _exact_lookup_str(element)

  def __contains__(self, key: Any) -> bool:
    lookup = _exact_lookup_str(key, require_hashable=True)
    return lookup is not _NON_STRING_OPERAND and dict.__contains__(
        self._mapping, lookup
    )

  def __reversed__(self) -> Any:
    return reversed(dict.keys(self._mapping))

  @property
  def mapping(self) -> Any:
    return MappingProxyType(self._mapping)


class _SafeItemsView(_SafeSetComparisonsMixin, collections.abc.ItemsView):
  """Live items view with operand-normalized membership."""

  @staticmethod
  def _normalize_element(element: Any) -> Any:
    if type(element) is not tuple or len(element) != 2:
      return _NON_STRING_OPERAND
    key, value = element
    lookup_key = _exact_lookup_str(key)
    lookup_value = _exact_lookup_str(value)
    if lookup_key is _NON_STRING_OPERAND or lookup_value is _NON_STRING_OPERAND:
      return _NON_STRING_OPERAND
    return (lookup_key, lookup_value)

  def __contains__(self, item: Any) -> bool:
    if type(item) is not tuple or len(item) != 2:
      return False
    key, value = item
    lookup_key = _exact_lookup_str(key, require_hashable=True)
    lookup_value = _exact_lookup_str(value)
    if lookup_key is _NON_STRING_OPERAND or lookup_value is _NON_STRING_OPERAND:
      return False
    sentinel = _NON_STRING_OPERAND
    stored = dict.get(self._mapping, lookup_key, sentinel)
    return stored is not sentinel and stored == lookup_value

  def __reversed__(self) -> Any:
    return reversed(dict.items(self._mapping))

  @property
  def mapping(self) -> Any:
    return MappingProxyType(self._mapping)


class _SafeValuesView(collections.abc.ValuesView):
  """Live values view with operand-normalized membership.

  Plain-dict values views compare rather than hash (and support
  neither set operations nor value-based equality), so non-string
  operands miss instead of raising and no comparison mixin applies.
  """

  def __contains__(self, value: Any) -> bool:
    lookup = _exact_lookup_str(value)
    if lookup is _NON_STRING_OPERAND:
      return False
    return any(stored == lookup for stored in dict.values(self._mapping))

  def __reversed__(self) -> Any:
    return reversed(dict.values(self._mapping))

  @property
  def mapping(self) -> Any:
    return MappingProxyType(self._mapping)


class _ValidatedLabels(dict):
  """Mutable ``dict[str, str]`` that validates every write.

  Ordinary dict-style mutation (``f.custom_labels["slice"] = "3"``)
  is part of the public ``TraceFilter`` contract and keeps working;
  each write normalizes hostile ``str`` subclasses to exact built-in
  strings and rejects non-strings and surrogates, so mutation cannot
  smuggle values past the assignment-time validation. Batch updates
  are normalized as a whole (with post-normalization duplicate
  detection) and applied atomically. Read/removal operands are
  normalized too, so equality-overriding subclasses cannot look up or
  delete a different entry. Low-level ``dict.__setitem__`` bypasses
  are caught by the SQL-boundary revalidation.
  """

  def __init__(self, *args: Any, **kwargs: Any) -> None:
    # Standard dict(*args, **kwargs) construction (including the
    # iterable-of-pairs form dataclasses.asdict() uses to rebuild
    # mapping subclasses), routed through the validated batch path.
    super().__init__()
    if args or kwargs:
      self.update(*args, **kwargs)

  def __setitem__(self, key: Any, value: Any) -> None:
    key, value = _validated_label_item(key, value)
    super().__setitem__(key, value)

  def __getitem__(self, key: Any) -> str:
    return super().__getitem__(_exact_lookup_str(key, require_hashable=True))

  def __contains__(self, key: Any) -> bool:
    return super().__contains__(_exact_lookup_str(key, require_hashable=True))

  def __delitem__(self, key: Any) -> None:
    super().__delitem__(_exact_lookup_str(key, require_hashable=True))

  def get(self, key: Any, default: Any = None) -> Any:
    return super().get(_exact_lookup_str(key, require_hashable=True), default)

  def pop(self, key: Any, *args: Any) -> Any:
    # Native precedence: dict.pop reports excess defaults BEFORE
    # touching the key, so arity is validated ahead of hashability.
    # Delegation then preserves native miss errors carrying only the
    # safe-repr sentinel, never the caller object.
    if len(args) > 1:
      raise TypeError(f"pop expected at most 2 arguments, got {1 + len(args)}")
    return super().pop(_exact_lookup_str(key, require_hashable=True), *args)

  def __ior__(self, other: Any) -> "_ValidatedLabels":
    self.update(other)
    return self

  def setdefault(self, key: Any, default: Any = None) -> Any:
    # Lookup first: an existing key returns its value without
    # validating the (unused) default, matching plain-dict semantics.
    lookup = _exact_lookup_str(key, require_hashable=True)
    if super().__contains__(lookup):
      return super().__getitem__(lookup)
    key, default = _validated_label_item(key, default)
    super().__setitem__(key, default)
    return default

  def update(self, *args: Any, **kwargs: Any) -> None:
    # Normalize the whole batch first so (a) two distinct subclass
    # keys with identical character data fail closed instead of one
    # predicate silently overwriting the other, and (b) a validation
    # failure commits nothing rather than a valid prefix. The raw
    # input is parsed WITHOUT building an intermediate dict: a
    # pre-normalization dict(*args) would let a caller-controlled
    # colliding __hash__/always-true __eq__ merge distinct keys
    # before exact-string normalization ever runs.
    if len(args) > 1:
      raise TypeError(f"update expected at most 1 argument, got {len(args)}")
    raw_items: list[Any] = []
    if args:
      source = args[0]
      if isinstance(source, dict):
        # Trusted read for real dict (sub)classes; a hidden entry
        # would silently erase a pin.
        raw_items.extend(dict.items(source))
      elif hasattr(source, "keys"):
        raw_items.extend((key, source[key]) for key in source.keys())
      else:
        raw_items.extend(_trusted_sequence_snapshot(source))
    raw_items.extend(kwargs.items())
    batch: dict[str, str] = {}
    exact_raw: dict[str, bool] = {}
    for item in raw_items:
      if isinstance(item, (list, tuple)):
        item = _trusted_sequence_snapshot(item)
      raw_key, raw_value = item
      key, value = _validated_label_item(raw_key, raw_value)
      is_exact = type(raw_key) is str
      if key in batch and not (is_exact and exact_raw[key]):
        # Two DISTINCT subclass keys normalizing to one string is a
        # silent predicate drop and fails closed; ordinary exact-str
        # duplicates keep standard dict last-write-wins semantics.
        raise ValueError(f"Duplicate custom label key: {key!r}.")
      batch[key] = value
      exact_raw[key] = is_exact
    for key, value in batch.items():
      super().__setitem__(key, value)

  def keys(self) -> "_SafeKeysView":
    # Hardened LIVE views over self: they reflect later mutation and
    # support reversed()/.mapping like native dict views, while
    # membership and set comparisons normalize operands instead of
    # running caller hooks.
    return _SafeKeysView(self)

  def items(self) -> "_SafeItemsView":
    return _SafeItemsView(self)

  def values(self) -> "_SafeValuesView":
    return _SafeValuesView(self)

  def __eq__(self, other: Any) -> Any:
    # Equality is part of the trusted read surface: comparing against
    # a dict containing hostile operands must not run their hooks. A
    # counterpart containing any non-string entry can never equal
    # this container; str subclasses are copied and compared by
    # character data, matching well-behaved native semantics.
    if not issubclass(type(other), dict):
      return NotImplemented
    normalized: dict[str, str] = {}
    for key, value in dict.items(other):
      lookup_key = _exact_lookup_str(key)
      lookup_value = _exact_lookup_str(value)
      if (
          lookup_key is _NON_STRING_OPERAND
          or lookup_value is _NON_STRING_OPERAND
      ):
        return False
      normalized[lookup_key] = lookup_value
    if len(normalized) != dict.__len__(other):
      # Distinct counterpart keys that collapse to one normalized key
      # must stay unequal, matching native cardinality semantics.
      return False
    return dict.__eq__(dict(self), normalized)

  def __ne__(self, other: Any) -> Any:
    # dict.__ne__ is a C slot that would bypass the trusted __eq__.
    result = self.__eq__(other)
    return result if result is NotImplemented else not result

  __hash__ = None  # type: ignore[assignment]

  def __reduce__(self) -> tuple[Any, tuple[dict]]:
    return (self.__class__, (dict(self),))


class _ValidatedSessionIds(list):
  """Mutable ``list[str]`` that validates every write.

  ``session_ids`` is a legacy identity surface: ordinary in-place
  mutation (``append``, ``extend``, slice assignment, ``+=``) keeps
  working but every added entry is checked against the exact-string/
  surrogate contract, so a mutated list cannot collapse identities on
  the JSON wire or reach the BigQuery array parameter unchecked.
  Batch inputs are fully materialized and validated before any entry
  is committed — self-extension therefore terminates, and a failing
  batch commits nothing. Membership/removal operands are normalized
  so equality-overriding subclasses cannot remove or match a
  different, valid session ID.
  """

  def __init__(self, iterable: Any = ()) -> None:
    iterable = _trusted_sequence_snapshot(iterable)
    super().__init__([_validated_session_id_entry(v) for v in iterable])

  def append(self, value: Any) -> None:
    super().append(_validated_session_id_entry(value))

  def extend(self, iterable: Any) -> None:
    # Materialize BEFORE extending: a lazy generator over `self`
    # (f.session_ids.extend(f.session_ids)) would observe every newly
    # appended entry and never terminate; materializing also makes a
    # failed batch atomic instead of committing a valid prefix. Real
    # list/tuple inputs are snapshotted through trusted descriptors
    # so a lying __iter__ cannot hide entries.
    iterable = _trusted_sequence_snapshot(iterable)
    values = [_validated_session_id_entry(v) for v in iterable]
    super().extend(values)

  def insert(self, index: int, value: Any) -> None:
    super().insert(index, _validated_session_id_entry(value))

  def __setitem__(self, index: Any, value: Any) -> None:
    if isinstance(index, slice):
      value = [_validated_session_id_entry(v) for v in value]
    else:
      value = _validated_session_id_entry(value)
    super().__setitem__(index, value)

  def __iadd__(self, iterable: Any) -> "_ValidatedSessionIds":
    self.extend(iterable)
    return self

  def __contains__(self, value: Any) -> bool:
    return super().__contains__(_exact_lookup_str(value))

  def remove(self, value: Any) -> None:
    super().remove(_exact_lookup_str(value))

  def index(self, value: Any, *args: Any) -> int:
    # Delegation keeps native start/stop arity validation and native
    # miss errors carrying only the safe-repr sentinel.
    return super().index(_exact_lookup_str(value), *args)

  def count(self, value: Any) -> int:
    return super().count(_exact_lookup_str(value))

  def __eq__(self, other: Any) -> Any:
    # Trusted equality: hostile elements in the counterpart must not
    # drive comparisons (see _ValidatedLabels.__eq__).
    if not issubclass(type(other), list):
      return NotImplemented
    normalized = []
    for value in list.__iter__(other):
      lookup = _exact_lookup_str(value)
      if lookup is _NON_STRING_OPERAND:
        return False
      normalized.append(lookup)
    return list.__eq__(list(self), normalized)

  def __ne__(self, other: Any) -> Any:
    # list.__ne__ is a C slot that would bypass the trusted __eq__.
    result = self.__eq__(other)
    return result if result is NotImplemented else not result

  __hash__ = None  # type: ignore[assignment]

  def __reduce__(self) -> tuple[Any, tuple[list]]:
    return (self.__class__, (list(self),))


def _validated_session_id_entry(value: Any) -> str:
  """Validate and normalize one session-id entry."""
  if not isinstance(value, str):
    raise TypeError("TraceFilter.session_ids entries must be strings.")
  return _exact_str(value)


def _normalized_filter_labels(labels: Any) -> dict[str, str]:
  """Validate and copy filter labels into exact built-in strings.

  Called on every assignment and again on every consuming boundary,
  so neither post-construction rebinding nor low-level in-place
  mutation can smuggle a hostile key past JSONPath construction.

  Raises:
      TypeError: If ``labels`` is not a dict or a key/value is not a
          string.
      ValueError: If two source keys normalize to the same built-in
          string (distinct ``str``-subclass keys with identical
          character data would otherwise silently drop a predicate
          instead of failing closed), or a string contains
          surrogates.
  """
  if not isinstance(labels, dict):
    raise TypeError("TraceFilter.custom_labels must be a dict or None.")
  normalized: dict[str, str] = {}
  # Trusted read: a subclass items() override must not hide entries.
  for key, value in dict.items(labels):
    key, value = _validated_label_item(key, value)
    if key in normalized:
      raise ValueError(f"Duplicate custom label key: {key!r}.")
    normalized[key] = value
  return normalized


def _normalized_session_ids(session_ids: Any) -> list[str]:
  """Validate and copy session IDs into exact built-in strings.

  ``session_ids`` is a legacy identity surface feeding a BigQuery
  array parameter and JSON serialization, so it follows the same
  exact-string/surrogate rules as the other identity dimensions.
  Called on assignment and re-applied at the SQL boundary; the stored
  value is a validating list subclass so ordinary in-place mutation
  is checked at write time as well.
  """
  if isinstance(session_ids, str) or not isinstance(session_ids, (list, tuple)):
    raise TypeError(
        "TraceFilter.session_ids must be a list of strings or None."
    )
  return _ValidatedSessionIds(session_ids)


def _parse_scope_signature_labels(
    signature: str,
) -> Optional[dict[str, str]]:
  """Parse a canonical ``v1:`` scope signature into its label payload.

  Returns the ``custom_labels`` mapping the signature attests, or
  ``None`` when the signature is not one a real :class:`TraceScope`
  can generate. Used to prove that a label pin being excluded from
  SQL is actually part of the exact scope the signature will select,
  so validation is strictly canonical: exact schema (exactly the two
  known fields), exact built-in types, no duplicate label keys, and
  — the decisive check — byte-for-byte re-encoding equality against
  the signature rebuilt from the parsed payload. Parser failures on
  hostile input (including ``RecursionError`` from deep nesting) are
  normalized to a rejection.
  """
  if type(signature) is not str or not signature.startswith("v1:"):
    return None
  try:
    payload = json.loads(signature[3:])
  except (ValueError, RecursionError):
    # ValueError covers JSONDecodeError; RecursionError is raised by
    # deeply nested input and must reject, not propagate.
    return None
  if type(payload) is not dict or set(payload) != {
      "custom_labels",
      "experiment_id",
  }:
    return None
  experiment_id = payload["experiment_id"]
  if experiment_id is not None and type(experiment_id) is not str:
    return None
  raw_labels = payload["custom_labels"]
  if type(raw_labels) is not list:
    return None
  labels: dict[str, str] = {}
  for entry in raw_labels:
    if (
        type(entry) is not list
        or len(entry) != 2
        or type(entry[0]) is not str
        or type(entry[1]) is not str
    ):
      return None
    if entry[0] in labels:
      # Canonical signatures cannot contain duplicate label keys.
      return None
    labels[entry[0]] = entry[1]
  try:
    rebuilt = TraceScope(
        experiment_id=experiment_id, custom_labels=labels or None
    )
  except (TypeError, ValueError):
    return None
  if rebuilt.scope_signature != signature:
    # Non-canonical encodings (unsorted labels, whitespace, escape
    # variants) are signatures no TraceScope generates; attesting
    # them would let impossible signatures strip SQL predicates.
    return None
  return labels


_PIN_WIRE_KEY = "$pin"


def decode_pin(value: Any) -> Any:
  """Decode the tagged JSON wire encoding of a pin sentinel.

  ``serialize()`` encodes :data:`SQL_NULL` (and a bare :data:`UNSET`
  outside dataclass fields, which are omitted instead) as the tagged
  object ``{"$pin": "<name>"}``, because plain JSON ``null`` already
  means "unfiltered" on :class:`TraceFilter` and "pin to SQL NULL" on
  :class:`TraceSelector`. This helper restores the module singleton
  from that tag and returns every other value unchanged, so
  ``TraceFilter(user_id=decode_pin(payload["user_id"]))`` round-trips
  a NULL-safe filter.

  Only the exact wire shape resolves: an exact built-in ``dict`` with
  the single exact-``str`` key ``"$pin"`` and an exact-``str`` tag of
  ``"UNSET"`` or ``"SQL_NULL"`` — precisely what ``json.loads``
  produces. Near misses, including strings whose subclass equality
  claims to match, pass through unchanged; hostile comparison
  semantics must never mint a real sentinel.
  """
  if type(value) is not dict or len(value) != 1:
    return value
  key, tag = next(iter(value.items()))
  if type(key) is not str or key != _PIN_WIRE_KEY:
    return value
  if type(tag) is not str or tag not in ("UNSET", "SQL_NULL"):
    return value
  return _resolve_pin_sentinel(tag)


# Separate pin aliases per surface so the annotations match the
# runtime contract: filters reject UNSET and selectors reject
# SQL_NULL (issue #362 round 7).
_FilterPin = Union[str, "_SqlNullType", None]
_SelectorPin = Union[str, "_UnsetType", None]

# Accepted label shapes include the JSON wire form (lists of two-item
# lists) that serialization emits and the constructors normalize.
_LabelsInput = Union[
    dict[str, str],
    Sequence[tuple[str, str]],
    Sequence[list[str]],
    None,
]


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
  # Trusted reads: a dict subclass overriding .items(), or a list/
  # tuple subclass overriding __iter__, could hide entries and
  # silently erase label pins.
  if isinstance(labels, dict):
    items = list(dict.items(labels))
  else:
    items = list(_trusted_sequence_snapshot(labels))
  if not items:
    return None
  seen: set[str] = set()
  normalized: list[tuple[str, str]] = []
  for item in items:
    if not isinstance(item, (tuple, list)):
      raise TypeError(
          "Each custom label entry must be a two-item (key, value)" " pair."
      )
    # Snapshot the pair too: unpacking would call a subclass
    # __iter__, which could yield different values than it stores.
    pair = _trusted_sequence_snapshot(item)
    if len(pair) != 2:
      raise TypeError(
          "Each custom label entry must be a two-item (key, value)" " pair."
      )
    key, value = pair
    if not isinstance(key, str) or not isinstance(value, str):
      raise TypeError("Custom label keys and values must be strings.")
    key = _exact_str(key)
    value = _exact_str(value)
    if key in seen:
      raise ValueError(f"Duplicate custom label key: {key!r}.")
    seen.add(key)
    normalized.append((key, value))
  return tuple(sorted(normalized))


def _sealed_value_type(cls: type) -> type:
  """Make a frozen slotted dataclass one-shot initializable.

  ``frozen=True`` blocks ``setattr`` but the generated ``__init__``
  and ``__setstate__`` write through ``object.__setattr__``, so
  either could rewrite a published instance (changing the hash of a
  dict key, desynchronizing attached Trace mirrors, or partially
  committing before validation raises). Both are guarded:

  * Initialized-state detection reads the first field's slot through
    its trusted member descriptor — never ``hasattr``, whose lookup a
    subclass ``__getattribute__`` could spoof.
  * The guarded ``__init__`` does not use ``functools.wraps``, so the
    generated mutating initializer is not republished as
    ``__wrapped__``.
  * ``__setstate__`` still populates a blank object (pickle/deepcopy)
    but rejects initialized instances and re-runs ``__post_init__``
    validation on the restored state, so hostile state cannot install
    invalid component types.
  """
  generated_init = cls.__init__
  generated_setstate = cls.__dict__.get("__setstate__")
  first_field = next(iter(cls.__dataclass_fields__))
  first_member = cls.__dict__[first_field]
  post_init = getattr(cls, "__post_init__", None)

  def _is_initialized(self: Any) -> bool:
    try:
      first_member.__get__(self, cls)
    except AttributeError:
      return False
    return True

  def __init__(self: Any, *args: Any, **kwargs: Any) -> None:
    if _is_initialized(self):
      raise TypeError(
          f"{cls.__name__} is immutable; __init__ cannot be called"
          " on an initialized instance."
      )
    generated_init(self, *args, **kwargs)

  __init__.__name__ = "__init__"
  __init__.__qualname__ = f"{cls.__qualname__}.__init__"
  __init__.__doc__ = generated_init.__doc__
  __init__.__module__ = cls.__module__
  cls.__init__ = __init__

  field_names = tuple(cls.__dataclass_fields__)
  members = tuple(cls.__dict__[name] for name in field_names)

  if generated_setstate is not None:

    def __setstate__(self: Any, state: Any) -> None:
      if _is_initialized(self):
        raise TypeError(
            f"{cls.__name__} is immutable; __setstate__ cannot be"
            " called on an initialized instance."
        )
      if type(state) not in (list, tuple) or len(state) != len(field_names):
        raise TypeError(
            f"{cls.__name__}.__setstate__ expected"
            f" {len(field_names)} field values."
        )
      # Atomic restore: validate/normalize on a blank probe first so
      # a failing state leaves `self` blank and retryable instead of
      # permanently initialized with invalid contents.
      probe = cls.__new__(cls)
      generated_setstate(probe, state)
      if post_init is not None:
        # Restored state is untrusted input: re-run the same
        # validation/normalization construction applies.
        post_init(probe)
      for member in members:
        # member.__set__ writes the slot directly: object.__setattr__
        # would consult the MRO and could invoke a subclass property
        # setter, breaking restoration atomicity.
        member.__set__(self, member.__get__(probe, cls))

    __setstate__.__name__ = "__setstate__"
    __setstate__.__qualname__ = f"{cls.__qualname__}.__setstate__"
    __setstate__.__module__ = cls.__module__
    cls.__setstate__ = __setstate__

  return cls


@_sealed_value_type
@dataclass(frozen=True, slots=True)
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
    object.__setattr__(self, "session_id", _exact_str(self.session_id))
    for dim in ("user_id", "root_agent_name"):
      value = getattr(self, dim)
      if value is None:
        continue
      if not isinstance(value, str):
        # Non-string values (e.g. 1 vs True) can compare equal while
        # producing different serialized forms, silently merging or
        # splitting identities downstream.
        raise TypeError(f"TraceIdentity.{dim} must be a string or None.")
      # str subclasses may override __eq__/__hash__, which would let
      # distinct identities compare equal during fail-closed dedup.
      object.__setattr__(self, dim, _exact_str(value))


@_sealed_value_type
@dataclass(frozen=True, slots=True)
class TraceScope:
  """Caller-selected scope pinning one recorded pass of a session.

  ``custom_labels`` is canonicalized to a sorted key/value tuple so
  equality, hashing, and :attr:`scope_signature` are independent of
  the order labels were supplied in.
  """

  experiment_id: Optional[str] = None
  custom_labels: _LabelsInput = None

  def __post_init__(self) -> None:
    if self.experiment_id is not None:
      if not isinstance(self.experiment_id, str):
        # 1 and True compare and hash equal but sign differently, so a
        # non-string experiment_id could dedupe two distinct scopes.
        raise TypeError("TraceScope.experiment_id must be a string or None.")
      object.__setattr__(self, "experiment_id", _exact_str(self.experiment_id))
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


@_sealed_value_type
@dataclass(frozen=True, slots=True)
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
  user_id: _SelectorPin = UNSET
  root_agent_name: _SelectorPin = UNSET
  experiment_id: _SelectorPin = UNSET
  custom_labels: _LabelsInput = None
  scope_signature: Optional[str] = None

  def __post_init__(self) -> None:
    if not isinstance(self.session_id, str):
      raise TypeError("TraceSelector.session_id must be a string.")
    object.__setattr__(self, "session_id", _exact_str(self.session_id))
    for dim in ("user_id", "root_agent_name", "experiment_id"):
      value = getattr(self, dim)
      if value is UNSET or value is None:
        continue
      if not isinstance(value, str):
        raise TypeError(
            f"TraceSelector.{dim} must be a string, None (pin to SQL"
            " NULL), or left UNSET."
        )
      object.__setattr__(self, dim, _exact_str(value))
    if self.scope_signature is not None:
      if not isinstance(self.scope_signature, str):
        raise TypeError(
            "TraceSelector.scope_signature must be a string or None."
        )
      object.__setattr__(
          self, "scope_signature", _exact_str(self.scope_signature)
      )
    object.__setattr__(
        self, "custom_labels", _canonicalize_labels(self.custom_labels)
    )

  @staticmethod
  def _pin_to_filter_value(value: _SelectorPin) -> _FilterPin:
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

    BigQuery JSON can store label keys whose quoted JSONPath form
    does not exist (odd backslash runs before a quote or at the end
    of the key), so a resolved candidate can legitimately carry such
    a label. Because label predicates are only a subset pre-filter,
    those keys are excluded from the SQL conditions when the exact
    ``scope_signature`` pin is present — the signature still selects
    exactly this candidate during resolution, keeping the one-step
    retry executable. Without a signature the exclusion would
    silently broaden the query, so the conversion fails closed
    instead.

    Raises:
        ValueError: If a label key is unaddressable in JSONPath and
            no ``scope_signature`` pin is present to preserve
            exactness.
    """
    labels = dict(self.custom_labels) if self.custom_labels else None
    if labels:
      addressable = {
          key: value
          for key, value in labels.items()
          if not _is_unaddressable_label_key(key)
      }
      if len(addressable) != len(labels):
        dropped = {key: labels[key] for key in set(labels) - set(addressable)}
        attested = (
            _parse_scope_signature_labels(self.scope_signature)
            if self.scope_signature is not None
            else None
        )
        # A signature only justifies dropping a label pin if the
        # exact scope it selects actually CONTAINS that pin — a
        # mismatched signature (which resolution will honor) would
        # otherwise silently erase the pin. Generated retry selectors
        # carry the candidate's own signature and always pass.
        if attested is None or any(
            attested.get(key) != value for key, value in dropped.items()
        ):
          raise ValueError(
              "Custom label keys"
              f" {sorted(dropped)!r} cannot be addressed by BigQuery"
              " JSONPath, and the scope_signature pin is absent or"
              " does not attest those exact label pairs; dropping"
              " them would silently broaden or misdirect the query."
              " Retry with the candidate's full selector (whose"
              " signature contains its labels) or without the"
              " unaddressable label pins."
          )
        labels = addressable or None
    return TraceFilter(
        session_ids=[self.session_id],
        user_id=self._pin_to_filter_value(self.user_id),
        root_agent_name=self._pin_to_filter_value(self.root_agent_name),
        experiment_id=self._pin_to_filter_value(self.experiment_id),
        custom_labels=labels,
        limit=limit,
    )


@_sealed_value_type
@dataclass(frozen=True, slots=True)
class ResolvedTraceSelector:
  """A fully resolved candidate: intrinsic identity plus scope."""

  identity: TraceIdentity
  scope: TraceScope = field(default_factory=TraceScope)

  def __post_init__(self) -> None:
    # Foreign component values (e.g. identity=1 vs identity=True) or
    # subclasses overriding __eq__/__hash__ can compare equal across
    # distinct candidates, silently collapsing a real ambiguity
    # during deduplication — exact trusted types only.
    if type(self.identity) is not TraceIdentity:
      raise TypeError("ResolvedTraceSelector.identity must be a TraceIdentity.")
    if type(self.scope) is not TraceScope:
      raise TypeError("ResolvedTraceSelector.scope must be a TraceScope.")

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
    deduped = tuple(dict.fromkeys(_validated_candidates(candidates)))
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
    self._candidates: tuple[ResolvedTraceSelector, ...] = deduped
    self._retry_dimensions: tuple[str, ...] = self._differing_dimensions(
        deduped
    )
    dims = ", ".join(self._retry_dimensions) or "scope"
    super().__init__(
        f"Ambiguous singular session lookup: {len(deduped)}"
        f" candidates match. Retry with an explicit selector for: {dims}."
    )

  @property
  def candidates(self) -> tuple[ResolvedTraceSelector, ...]:
    """The distinct colliding candidates (read-only).

    Exposed as a property so the constructor-validated ambiguity
    state cannot be reassigned into a shape that contradicts the
    message, payload, or pickle behavior.
    """
    return self._candidates

  @property
  def retry_dimensions(self) -> tuple[str, ...]:
    """Dimension names that disambiguate a retry (read-only)."""
    return self._retry_dimensions

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


def _validated_candidates(
    candidates: Sequence[ResolvedTraceSelector],
) -> list[ResolvedTraceSelector]:
  """Require real resolved selectors before any dedup/hash decision.

  Foreign values that happen to compare equal (or hash together)
  would otherwise collapse silently inside ``dict.fromkeys()``
  instead of failing clearly. Exact type is required — a
  ``ResolvedTraceSelector`` subclass can override ``__post_init__``,
  ``__eq__``, and ``__hash__``, so ``isinstance`` would still let
  caller-controlled semantics drive the fail-closed ambiguity
  decision.
  """
  validated = list(candidates)
  for candidate in validated:
    if type(candidate) is not ResolvedTraceSelector:
      raise TypeError(
          "Candidates must be ResolvedTraceSelector instances (exact"
          f" type); got {_safe_type_name(candidate)!r}."
      )
  return validated


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
  resolved = list(dict.fromkeys(_validated_candidates(candidates)))
  if not resolved:
    raise ValueError("No candidates match the requested session.")
  if len(resolved) > 1:
    raise AmbiguousSessionError(candidates=resolved)
  return resolved[0]


class _WeakrefableSlotted:
  """Base adding weak-reference support to slotted dataclasses.

  ``dataclass(slots=True)`` alone would drop ``__weakref__``;
  ``weakref_slot=True`` requires Python 3.11, so a slotted base
  carries the slot for the 3.10 floor.
  """

  __slots__ = ("__weakref__",)


@dataclass(slots=True)
class Trace(_WeakrefableSlotted):
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
  trace. To change identity, build a new ``Trace``. The class is
  slot-backed, so there is no writable instance ``__dict__`` through
  which ``vars(trace)`` could retag the identity behind the guards
  (ad-hoc attributes are consequently not supported).
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
    if name == "scope":
      if value is not None and type(value) is not TraceScope:
        raise TypeError("Trace.scope must be a TraceScope or None.")
      # Scope carries the run's provenance: once attached it can only
      # be re-assigned an equal value, never replaced or cleared, or
      # serialized output would report a different pass (or none) for
      # the same spans.
      current = getattr(self, "scope", None)
      if current is not None and value != current:
        raise ValueError(
            "Trace.scope cannot be replaced or detached once attached;"
            " build a new Trace for a different scope."
        )
    if name == "session_id":
      # Enforce types before any equality check: a foreign object's
      # comparison methods must never drive the mirror validation.
      if not isinstance(value, str):
        raise TypeError("Trace.session_id must be a string.")
      value = _exact_str(value)
    elif name == "user_id" and value is not None:
      if not isinstance(value, str):
        raise TypeError("Trace.user_id must be a string or None.")
      value = _exact_str(value)
    if name in ("session_id", "user_id", "identity"):
      self._check_identity_write(name, value)
    object.__setattr__(self, name, value)
    if name == "identity" and value is not None and self.user_id is None:
      # Keep the legacy mirror in sync when an identity is attached
      # before user_id is known (dataclass __init__ assigns user_id
      # first, and client code may attach identity after construction).
      object.__setattr__(self, "user_id", value.user_id)

  def __delattr__(self, name: str) -> None:
    if name in ("session_id", "user_id", "identity", "scope"):
      # Deleting an identity- or provenance-carrying field would fall
      # back to the dataclass class-level default (None), silently
      # detaching it and reopening the retag path the write guards
      # close.
      raise AttributeError(
          f"Trace.{name} carries the identity contract and cannot be"
          " deleted."
      )
    object.__delattr__(self, name)

  def _check_identity_write(self, name: str, value: Any) -> None:
    """Reject writes that would desynchronize identity and mirrors."""
    if name == "identity":
      if value is not None and type(value) is not TraceIdentity:
        # A mutable duck-typed stand-in (e.g. SimpleNamespace) or a
        # subclass with overridden equality could bypass these guards
        # through its own alias or comparison semantics; only the
        # exact frozen, validated value object is an acceptable
        # authority.
        raise TypeError("Trace.identity must be a TraceIdentity or None.")
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
