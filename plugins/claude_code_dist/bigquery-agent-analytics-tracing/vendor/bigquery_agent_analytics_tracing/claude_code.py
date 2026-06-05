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

"""Claude Code hook producer for BQAA.

Maps Claude Code's native hooks (``SessionStart``, ``UserPromptSubmit``,
``PreToolUse``, ``PostToolUse``, ``Stop``, ``SubagentStop``,
``Notification``, ``PermissionRequest``, ``SessionEnd``) onto BQAA
``agent_events`` rows. Each hook receives a JSON payload on stdin and
this module's ``main()`` is the entry that hook shell wrappers invoke.

State lives in two on-disk JSON stores guarded by ``fcntl.flock``:

  * ``state_<session>.json`` — session-scoped state (session id, current
    trace/span/invocation, transcript bookmark, agent identity).
  * ``tool_<session>_<tool_use_id>.json`` — per-tool state so concurrent
    ``PreToolUse`` fires for parallel tools cannot clobber each other's
    ``start_ms`` / ``span_id``.

Stale state files older than ``BQAA_STATE_TTL_HOURS`` are purged on the
next ``SessionStart`` so long-running shells do not leak.

Default ``attributes.source`` values mirror what the upstream tracing
skill emits today so adoption queries that key off
``attributes.session_metadata.source`` keep working:

  * Most events: ``"claude_code"``
  * SubagentStop:  ``"claude_code_subagent"``
  * Notification:  ``"claude_code_notification"``
  * PermissionRequest: ``"claude_code_permission_request"``
  * SessionEnd:    ``"claude_code_session_end"``
"""

from __future__ import annotations

import contextlib
from datetime import datetime
from datetime import timezone
import fcntl
import json
import os
from pathlib import Path
import sys
import time
import traceback
from typing import Any, Iterator

from ._utils import deterministic_span
from ._utils import hex_id
from ._utils import iso_timestamp
from ._utils import log_to_file
from ._utils import safe_json_loads
from ._utils import timestamp_ms
from ._utils import to_jsonable
from .config import BQAAConfig
from .config import DEFAULT_STATE_DIR
from .logger import BigQueryAgentAnalyticsLogger

CLAUDE_CODE_DEFAULT_AGENT = "claude-code"


# ----------------------------------------------------------------------------
# Transcript reader
# ----------------------------------------------------------------------------


def _read_transcript_since(
    path: str, start_line: int, max_bytes: int
) -> tuple[str, str, dict[str, int], bool]:
  """Stream-read the Claude Code transcript and stop after ``max_bytes``.

  Returns ``(output_text, model, usage, was_truncated)``. The transcript
  is a JSONL file of message objects; we read assistant lines after
  ``start_line`` (set on ``UserPromptSubmit``) until the byte cap is
  reached.
  """
  output_parts: list[str] = []
  bytes_collected = 0
  was_truncated = False
  model = ""
  usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
  if not path:
    return "", model, usage, False
  transcript = Path(path).expanduser()
  if not transcript.exists():
    return "", model, usage, False

  with transcript.open("r", encoding="utf-8", errors="replace") as handle:
    for line_no, line in enumerate(handle, start=1):
      if line_no <= start_line or not line.strip():
        continue
      item = safe_json_loads(line, {})
      if item.get("type") != "assistant":
        continue
      message = item.get("message") or {}
      model = message.get("model") or model
      text = _text_from_content(message.get("content"))
      if text:
        if max_bytes > 0:
          encoded = text.encode("utf-8", "replace")
          remaining = max_bytes - bytes_collected
          if remaining <= 0:
            was_truncated = True
          elif len(encoded) > remaining:
            output_parts.append(
                encoded[:remaining].decode("utf-8", "replace")
                + "...[TRUNCATED]"
            )
            bytes_collected = max_bytes
            was_truncated = True
          else:
            output_parts.append(text)
            bytes_collected += len(encoded)
        else:
          output_parts.append(text)
      raw_usage = message.get("usage") or {}
      usage["prompt_tokens"] += int(raw_usage.get("input_tokens") or 0)
      usage["prompt_tokens"] += int(
          raw_usage.get("cache_read_input_tokens") or 0
      )
      usage["prompt_tokens"] += int(
          raw_usage.get("cache_creation_input_tokens") or 0
      )
      usage["completion_tokens"] += int(raw_usage.get("output_tokens") or 0)
  usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
  return "\n".join(output_parts), model, usage, was_truncated


def _text_from_content(content: Any) -> str:
  if isinstance(content, str):
    return content
  if isinstance(content, list):
    pieces = []
    for item in content:
      if isinstance(item, dict) and item.get("type") == "text":
        pieces.append(str(item.get("text", "")))
      elif isinstance(item, str):
        pieces.append(item)
    return "\n".join(p for p in pieces if p)
  return ""


def _line_count(path: str) -> int:
  if not path:
    return 0
  transcript = Path(path).expanduser()
  if not transcript.exists():
    return 0
  with transcript.open("r", encoding="utf-8", errors="replace") as handle:
    return sum(1 for _ in handle)


# ----------------------------------------------------------------------------
# State store with file locking
# ----------------------------------------------------------------------------


class _LockedJSONStore:
  """Atomic read-modify-write JSON store guarded by ``fcntl.flock``."""

  def __init__(self, path: Path):
    self.path = path
    self.path.parent.mkdir(parents=True, exist_ok=True)

  @contextlib.contextmanager
  def transaction(self) -> Iterator[dict[str, Any]]:
    fd = os.open(str(self.path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
      fcntl.flock(fd, fcntl.LOCK_EX)
      try:
        with os.fdopen(fd, "r+", encoding="utf-8", closefd=False) as handle:
          handle.seek(0)
          raw = handle.read()
          state = safe_json_loads(raw, {}) if raw.strip() else {}
          yield state
          handle.seek(0)
          handle.truncate()
          handle.write(json.dumps(state, sort_keys=True))
          handle.flush()
          os.fsync(fd)
      finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
      try:
        os.close(fd)
      except OSError:
        pass

  def read(self) -> dict[str, Any]:
    if not self.path.exists():
      return {}
    with self.transaction() as state:
      return dict(state)

  def remove(self) -> None:
    try:
      self.path.unlink()
    except FileNotFoundError:
      pass


class StateStore:
  """Per-session state with locked RMW + per-tool sub-files.

  Session-scoped fields live in ``state_<session>.json``. Per-tool
  sub-state lives in ``tool_<session>_<tool_use_id>.json`` so concurrent
  ``PreToolUse`` fires for parallel tools do not clobber each other's
  ``start_ms`` / ``span_id``.
  """

  def __init__(self, key: str, root: str = DEFAULT_STATE_DIR):
    safe_key = (
        "".join(ch for ch in key if ch.isalnum() or ch in "._-") or "default"
    )
    self.key = safe_key
    self.root = Path(root).expanduser()
    self.root.mkdir(parents=True, exist_ok=True)
    self._session_store = _LockedJSONStore(self.root / f"state_{safe_key}.json")
    self._cache: dict[str, Any] = self._session_store.read()

  def get(self, key: str, default: Any = None) -> Any:
    return self._cache.get(key, default)

  def set(self, key: str, value: Any) -> None:
    self.update({key: value})

  def update(self, values: dict[str, Any]) -> None:
    with self._session_store.transaction() as state:
      state.update(values)
      self._cache = dict(state)

  def delete(self, *keys: str) -> None:
    with self._session_store.transaction() as state:
      for key in keys:
        state.pop(key, None)
      self._cache = dict(state)

  def remove(self) -> None:
    self._session_store.remove()
    for path in self.root.glob(f"tool_{self.key}_*.json"):
      try:
        path.unlink()
      except FileNotFoundError:
        pass

  # -- Per-tool sub-state ---------------------------------------------------

  def _tool_path(self, tool_use_id: str) -> Path:
    safe = (
        "".join(ch for ch in tool_use_id if ch.isalnum() or ch in "._-")
        or "unknown"
    )
    return self.root / f"tool_{self.key}_{safe}.json"

  def set_tool(self, tool_use_id: str, value: dict[str, Any]) -> None:
    store = _LockedJSONStore(self._tool_path(tool_use_id))
    with store.transaction() as state:
      state.clear()
      state.update(value)

  def pop_tool(self, tool_use_id: str) -> dict[str, Any]:
    path = self._tool_path(tool_use_id)
    store = _LockedJSONStore(path)
    with store.transaction() as state:
      value = dict(state)
    try:
      path.unlink()
    except FileNotFoundError:
      pass
    return value


def cleanup_stale_state(root: str | Path, ttl_hours: float) -> int:
  """Remove state and per-tool files older than ``ttl_hours``.

  Returns the number of files removed. No-op when ``ttl_hours <= 0``.
  """
  if ttl_hours <= 0:
    return 0
  base = Path(root).expanduser()
  if not base.exists():
    return 0
  cutoff = time.time() - ttl_hours * 3600
  removed = 0
  for path in base.iterdir():
    if not path.is_file():
      continue
    name = path.name
    if not (
        name.startswith("state_") or name.startswith("tool_")
    ) or not name.endswith(".json"):
      continue
    try:
      mtime = path.stat().st_mtime
    except FileNotFoundError:
      continue
    if mtime < cutoff:
      try:
        path.unlink()
        removed += 1
      except FileNotFoundError:
        pass
  return removed


# ----------------------------------------------------------------------------
# Tool helpers
# ----------------------------------------------------------------------------


def _tool_origin(tool_name: Any) -> str:
  name = str(tool_name or "")
  if name.lower().startswith("mcp__"):
    return "MCP"
  if name in {"Task", "Subagent"}:
    return "SUB_AGENT"
  return "LOCAL"


def _tool_status(result: Any) -> tuple[str, str | None]:
  if isinstance(result, dict):
    if result.get("is_error") or result.get("error"):
      return "ERROR", str(
          result.get("error") or result.get("message") or "tool error"
      )
  text = str(result or "")
  if text.lower().startswith("error:"):
    return "ERROR", text[:1000]
  return "OK", None


# ----------------------------------------------------------------------------
# Hook adapter
# ----------------------------------------------------------------------------


class ClaudeHookBQAAAdapter:
  """Dispatches Claude Code hook payloads to ``BigQueryAgentAnalyticsLogger``."""

  def __init__(self, logger: BigQueryAgentAnalyticsLogger | None = None):
    self.logger = logger or BigQueryAgentAnalyticsLogger()
    self.config = self.logger.config

  def process(self, hook_name: str, payload: dict[str, Any]) -> None:
    state = StateStore(self._state_key(payload), root=self.config.state_dir)
    if hook_name == "SessionStart":
      self._session_start(payload, state)
    elif hook_name == "UserPromptSubmit":
      self._user_prompt_submit(payload, state)
    elif hook_name == "PreToolUse":
      self._pre_tool_use(payload, state)
    elif hook_name == "PostToolUse":
      self._post_tool_use(payload, state)
    elif hook_name == "Stop":
      self._stop(payload, state)
    elif hook_name == "SubagentStop":
      self._subagent_stop(payload, state)
    elif hook_name == "Notification":
      self._notification(payload, state)
    elif hook_name == "PermissionRequest":
      self._permission_request(payload, state)
    elif hook_name == "SessionEnd":
      self._session_end(payload, state)

  def _state_key(self, payload: dict[str, Any]) -> str:
    return (
        str(payload.get("session_id") or "")
        or os.environ.get("CLAUDE_SESSION_KEY", "")
        or str(os.getppid())
    )

  def _ensure_session(self, payload: dict[str, Any], state: StateStore) -> None:
    if state.get("session_id"):
      return
    self._session_start(payload, state)

  def _session_start(self, payload: dict[str, Any], state: StateStore) -> None:
    cwd = payload.get("cwd") or os.getcwd()
    session_id = payload.get("session_id") or hex_id(32)
    state.update(
        {
            "session_id": session_id,
            "project_name": (
                os.environ.get("BQAA_PROJECT_NAME") or Path(cwd).name
            ),
            "agent": (
                os.environ.get("BQAA_AGENT_NAME") or CLAUDE_CODE_DEFAULT_AGENT
            ),
            "user_id": os.environ.get("BQAA_USER_ID") or os.environ.get("USER"),
            "trace_count": int(state.get("trace_count", 0)),
            "session_start_ms": timestamp_ms(),
        }
    )
    try:
      cleanup_stale_state(self.config.state_dir, self.config.state_ttl_hours)
    except OSError:
      pass

  def _user_prompt_submit(
      self, payload: dict[str, Any], state: StateStore
  ) -> None:
    self._ensure_session(payload, state)
    trace_count = int(state.get("trace_count", 0)) + 1
    trace_id = hex_id(32)
    span_id = hex_id(16)
    invocation_id = payload.get("invocation_id") or hex_id(32)
    prompt = str(payload.get("prompt") or "")
    transcript = str(payload.get("transcript_path") or "")
    state.update(
        {
            "trace_count": trace_count,
            "current_trace_id": trace_id,
            "current_span_id": span_id,
            "current_invocation_id": invocation_id,
            "current_prompt": prompt,
            "current_start_ms": timestamp_ms(),
            "transcript_path": transcript,
            "transcript_start_line": _line_count(transcript),
        }
    )
    self.logger.log_llm_request(
        prompt=prompt,
        session_id=state.get("session_id"),
        invocation_id=invocation_id,
        trace_id=trace_id,
        span_id=span_id,
        agent=state.get("agent"),
        user_id=state.get("user_id"),
        attributes={
            "session_metadata": {
                "project_name": state.get("project_name"),
                "cwd": payload.get("cwd"),
                "source": "claude_code",
                "trace_number": trace_count,
            },
            "custom_tags": {"assistant": "claude_code"},
        },
    )

  def _pre_tool_use(self, payload: dict[str, Any], state: StateStore) -> None:
    self._ensure_session(payload, state)
    tool_use_id = str(payload.get("tool_use_id") or hex_id(16))
    span_id = deterministic_span(tool_use_id)
    tool_name = str(payload.get("tool_name") or "unknown")
    state.set_tool(
        tool_use_id,
        {
            "start_ms": timestamp_ms(),
            "span_id": span_id,
            "tool_name": tool_name,
        },
    )
    self.logger.log_tool_starting(
        tool=tool_name,
        args=to_jsonable(payload.get("tool_input") or {}),
        session_id=state.get("session_id"),
        invocation_id=state.get("current_invocation_id") or hex_id(32),
        trace_id=state.get("current_trace_id") or hex_id(32),
        span_id=span_id,
        parent_span_id=state.get("current_span_id"),
        agent=state.get("agent"),
        tool_origin=_tool_origin(tool_name),
        attributes={"source": "claude_code"},
    )

  def _post_tool_use(self, payload: dict[str, Any], state: StateStore) -> None:
    self._ensure_session(payload, state)
    tool_use_id = str(payload.get("tool_use_id") or "")
    tool_state = state.pop_tool(tool_use_id) if tool_use_id else {}
    start_ms = int(tool_state.get("start_ms") or timestamp_ms())
    tool_name = str(
        payload.get("tool_name") or tool_state.get("tool_name") or "unknown"
    )
    # If Pre never fired we still want correlation — derive deterministically.
    span_id = tool_state.get("span_id") or deterministic_span(tool_use_id)
    result = payload.get("tool_response")
    status, error_message = _tool_status(result)
    self.logger.log_tool_completed(
        tool=tool_name,
        result=to_jsonable(result),
        session_id=state.get("session_id"),
        invocation_id=state.get("current_invocation_id") or hex_id(32),
        trace_id=state.get("current_trace_id") or hex_id(32),
        span_id=span_id,
        parent_span_id=state.get("current_span_id"),
        agent=state.get("agent"),
        tool_origin=_tool_origin(tool_name),
        total_ms=max(0, timestamp_ms() - start_ms),
        status=status,
        error_message=error_message,
        attributes={"source": "claude_code"},
    )

  def _stop(self, payload: dict[str, Any], state: StateStore) -> None:
    if not state.get("current_trace_id"):
      return
    transcript = str(
        payload.get("transcript_path") or state.get("transcript_path") or ""
    )
    output, model, usage, was_truncated = _read_transcript_since(
        transcript,
        int(state.get("transcript_start_line") or 0),
        self.config.transcript_max_bytes,
    )
    output = output or str(payload.get("response") or "(No response captured)")
    start_ms = int(state.get("current_start_ms") or timestamp_ms())
    self.logger.log_llm_response(
        response=output,
        session_id=state.get("session_id"),
        invocation_id=state.get("current_invocation_id"),
        trace_id=state.get("current_trace_id"),
        span_id=state.get("current_span_id"),
        agent=state.get("agent"),
        user_id=state.get("user_id"),
        model=model or str(payload.get("model") or ""),
        usage_metadata=usage,
        total_ms=max(0, timestamp_ms() - start_ms),
        attributes={
            "session_metadata": {
                "project_name": state.get("project_name"),
                "source": "claude_code",
                "trace_number": state.get("trace_count"),
            },
            "custom_tags": {"assistant": "claude_code"},
        },
        is_truncated=was_truncated,
    )
    state.delete(
        "current_trace_id",
        "current_span_id",
        "current_invocation_id",
        "current_prompt",
        "current_start_ms",
        "transcript_path",
        "transcript_start_line",
    )

  def _subagent_stop(self, payload: dict[str, Any], state: StateStore) -> None:
    if not state.get("current_trace_id"):
      return
    transcript = str(payload.get("agent_transcript_path") or "")
    output, model, usage, was_truncated = _read_transcript_since(
        transcript, 0, self.config.transcript_max_bytes
    )
    agent_name = str(
        payload.get("agent_type") or payload.get("agent_id") or "subagent"
    )
    self.logger.log_llm_response(
        response=output or str(payload.get("output") or ""),
        session_id=state.get("session_id"),
        invocation_id=state.get("current_invocation_id"),
        trace_id=state.get("current_trace_id"),
        span_id=hex_id(16),
        parent_span_id=state.get("current_span_id"),
        agent=agent_name,
        user_id=state.get("user_id"),
        model=model,
        usage_metadata=usage,
        attributes={
            "session_metadata": {
                "project_name": state.get("project_name"),
                "source": "claude_code_subagent",
            },
            "custom_tags": {
                "assistant": "claude_code",
                "subagent_id": payload.get("agent_id"),
            },
        },
        is_truncated=was_truncated,
    )

  def _notification(self, payload: dict[str, Any], state: StateStore) -> None:
    self._ensure_session(payload, state)
    self.logger.log_event(
        event_type="STATE_DELTA",
        agent=state.get("agent"),
        session_id=state.get("session_id"),
        invocation_id=state.get("current_invocation_id"),
        trace_id=state.get("current_trace_id"),
        span_id=hex_id(16),
        parent_span_id=state.get("current_span_id"),
        content={
            "notification": {
                "title": payload.get("title"),
                "message": payload.get("message"),
                "type": payload.get("notification_type") or "info",
            }
        },
        attributes={
            "state_delta": {
                "source": "claude_code_notification",
                "notification_type": (
                    payload.get("notification_type") or "info"
                ),
            }
        },
    )

  def _permission_request(
      self, payload: dict[str, Any], state: StateStore
  ) -> None:
    self._ensure_session(payload, state)
    self.logger.log_event(
        event_type="HITL_CONFIRMATION_REQUEST",
        agent=state.get("agent"),
        session_id=state.get("session_id"),
        invocation_id=state.get("current_invocation_id"),
        trace_id=state.get("current_trace_id"),
        span_id=hex_id(16),
        parent_span_id=state.get("current_span_id"),
        content={
            "permission": payload.get("permission"),
            "tool": payload.get("tool_name"),
            "args": to_jsonable(payload.get("tool_input") or {}),
        },
        attributes={
            "session_metadata": {"source": "claude_code_permission_request"},
            "custom_tags": {"assistant": "claude_code"},
        },
    )

  def _session_end(self, payload: dict[str, Any], state: StateStore) -> None:
    self._ensure_session(payload, state)
    self.logger.log_event(
        event_type="STATE_DELTA",
        agent=state.get("agent"),
        session_id=state.get("session_id"),
        invocation_id=state.get("current_invocation_id"),
        trace_id=state.get("current_trace_id"),
        span_id=hex_id(16),
        parent_span_id=state.get("current_span_id"),
        content={"session_end": True},
        attributes={
            "state_delta": {
                "source": "claude_code_session_end",
                "trace_count": state.get("trace_count", 0),
                "duration_ms": max(
                    0,
                    timestamp_ms()
                    - int(state.get("session_start_ms") or timestamp_ms()),
                ),
            }
        },
    )
    state.remove()


# ----------------------------------------------------------------------------
# Entry points
# ----------------------------------------------------------------------------


def run_claude_hook(
    hook_name: str, payload: dict[str, Any] | None = None
) -> None:
  """Convenience: dispatch a single hook payload through a fresh adapter."""
  adapter = ClaudeHookBQAAAdapter()
  adapter.process(hook_name, payload or {})


def main(argv: list[str] | None = None) -> int:
  """Hook entry: read JSON payload from stdin, dispatch by hook name.

  Invoked by hook shell wrappers as
  ``python -m bigquery_agent_analytics_tracing.claude_code <HookName>``
  or via the ``bqaa-claude-hook`` console script. Errors are logged but
  never propagated so the host agent's hot path stays clean.
  """
  argv = argv if argv is not None else sys.argv[1:]
  if not argv:
    print("usage: bqaa-claude-hook <ClaudeHookName>", file=sys.stderr)
    return 2
  hook_name = argv[0]
  raw = sys.stdin.read()
  payload = safe_json_loads(raw, {}) if raw.strip() else {}
  config = BQAAConfig.from_env()
  if not config.enabled:
    return 0
  try:
    ClaudeHookBQAAAdapter(BigQueryAgentAnalyticsLogger(config)).process(
        hook_name, payload
    )
  except Exception as exc:  # Hooks must never break the calling agent.
    log_to_file(
        config, f"ERROR hook={hook_name}: {exc}\n{traceback.format_exc()}"
    )
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
