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
from typing import Any, Optional

logger = logging.getLogger("bigquery_agent_analytics." + __name__)


_ANSI_RED = "\x1b[31m"
_ANSI_YELLOW = "\x1b[33m"
_ANSI_RESET = "\x1b[0m"


def _colorize(text: str, ansi_code: str, enabled: bool) -> str:
  """Wraps text in an ANSI color code when enabled, else returns unchanged."""
  if not enabled:
    return text
  return f"{ansi_code}{text}{_ANSI_RESET}"


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

    Populated for ``TOOL_STARTING``, ``TOOL_COMPLETED``,
    ``TOOL_ERROR``, and ``HITL_*`` events where the plugin writes
    the tool name into ``content.tool``. Returns ``None`` for
    spans that do not describe a tool invocation.
    """
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
  """

  start_time: Optional[datetime] = None
  end_time: Optional[datetime] = None
  agent_id: Optional[str] = None
  user_id: Optional[str] = None
  session_ids: Optional[list[str]] = None
  experiment_id: Optional[str] = None
  has_error: Optional[bool] = None
  error_type: Optional[str] = None
  custom_labels: Optional[dict[str, str]] = None
  min_latency_ms: Optional[float] = None
  max_latency_ms: Optional[float] = None
  event_types: Optional[list[str]] = None
  tool_origin: Optional[str] = None
  root_agent_name: Optional[str] = None
  limit: int = 100

  @classmethod
  def from_cli_args(
      cls,
      last: str | None = None,
      agent_id: str | None = None,
      session_id: str | None = None,
      user_id: str | None = None,
      has_error: bool | None = None,
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
    if self.user_id:
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
    if self.experiment_id:
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
            f" CONCAT('$.labels.', @{param_key}))"
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
    if self.root_agent_name:
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


@dataclass
class Trace:
  """A complete agent trace for a session.

  Contains all spans (events) for the session and provides
  visualization via the :meth:`render` method.
  """

  trace_id: str
  session_id: str
  spans: list[Span] = field(default_factory=list)
  user_id: Optional[str] = None
  start_time: Optional[datetime] = None
  end_time: Optional[datetime] = None
  total_latency_ms: Optional[float] = None

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
            return result
        elif c:
          return str(c)

    for span in reversed(self.spans):
      if span.event_type == "AGENT_COMPLETED":
        c = span.content
        if isinstance(c, dict):
          result = c.get("response") or c.get("text_summary")
          if result:
            return result
        elif c:
          return str(c)
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
