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

"""Short-term, long-term and reasoning memory read from BQAA traces.

``neo4j-agent-memory`` splits agent memory into three layers that the
application writes explicitly (``add_message``, ``add_preference``,
``start_trace`` / ``record_tool_call`` / ``complete_trace``). Here the ADK
``BigQueryAgentAnalyticsPlugin`` is the only writer: it already logs each
user message, model response, tool call and state change to
``agent_events``. This module reads those rows back with one
``Client.list_traces`` call and derives each layer:

* short-term: the conversation of each session, from
  ``USER_MESSAGE_RECEIVED`` rows and the text parts of ``LLM_RESPONSE`` rows;
* long-term: ADK ``user:``-scoped session state, read from ``STATE_DELTA``
  rows and kept as a version history, plus the entities the agent passed to
  its tools;
* reasoning: one trace per ADK invocation, with a step per model turn, the
  tool calls that turn made, and a derived outcome.

Every item keeps the ``session_id`` and ``span_id`` of the row it came from.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime
from datetime import timezone
import json
import re
from typing import Any, Mapping, Optional

from bigquery_agent_analytics import Client
from bigquery_agent_analytics import Span
from bigquery_agent_analytics import Trace
from bigquery_agent_analytics import TraceFilter

ANSWERED = "answered"
ANSWERED_WITH_ERRORS = "answered_with_errors"
UNANSWERED = "unanswered"

# ADK ``State.USER_PREFIX``: keys shared by every session of one user.
USER_STATE_PREFIX = "user:"

# The plugin writes an LLM_RESPONSE as parts joined by " | ": "text: '...'",
# "call: <tool>", "resp: <tool>" or "other".
_RESPONSE_PART = re.compile(r" \| (?=text: |call: |resp: |other(?: \| |$))")

_STOP_WORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "can",
        "do",
        "for",
        "from",
        "i",
        "in",
        "is",
        "it",
        "me",
        "my",
        "now",
        "of",
        "on",
        "or",
        "so",
        "that",
        "the",
        "this",
        "to",
        "with",
        "you",
        "your",
    }
)


# ------------------------------------------------------------------ #
# Records                                                              #
# ------------------------------------------------------------------ #


@dataclasses.dataclass(frozen=True)
class Message:
  """One user or assistant turn of a conversation."""

  role: str
  content: str
  timestamp: datetime
  session_id: str
  span_id: Optional[str]


@dataclasses.dataclass(frozen=True)
class SessionInfo:
  """A session with its time range and message count."""

  session_id: str
  created_at: datetime
  updated_at: datetime
  message_count: int
  first_message_preview: Optional[str]


@dataclasses.dataclass(frozen=True)
class Preference:
  """One version of a ``user:`` state key.

  ``category`` is the state key without the ``user:`` prefix and
  ``preference`` its value. A version is valid from the ``STATE_DELTA``
  row that wrote it until the next write of the same key.
  """

  category: str
  preference: Any
  valid_from: datetime
  valid_until: Optional[datetime]
  session_id: str
  span_id: Optional[str]


@dataclasses.dataclass(frozen=True)
class EntityMention:
  """A tool call that received an entity as an argument."""

  tool_name: str
  argument: str
  session_id: str
  span_id: Optional[str]
  timestamp: datetime


@dataclasses.dataclass(frozen=True)
class Entity:
  """An entity the agent acted on, with every tool call that named it."""

  name: str
  entity_type: str
  mentions: tuple[EntityMention, ...]

  @property
  def sessions(self) -> tuple[str, ...]:
    return tuple(sorted({m.session_id for m in self.mentions}))

  @property
  def last_seen(self) -> datetime:
    return max(m.timestamp for m in self.mentions)


@dataclasses.dataclass(frozen=True)
class ToolCall:
  """A tool call: ``TOOL_STARTING`` paired with its completion or error."""

  tool_name: str
  arguments: dict[str, Any]
  result: Any
  status: str  # "success", "error", or "pending" (no completion row yet)
  duration_ms: Optional[float]
  error: Optional[str]
  session_id: str
  span_id: Optional[str]
  started_at: datetime


@dataclasses.dataclass(frozen=True)
class ReasoningStep:
  """One model turn and the tool calls it requested."""

  step_number: int
  thought: Optional[str]
  action: str
  observation: Optional[str]
  tool_calls: tuple[ToolCall, ...]


@dataclasses.dataclass(frozen=True)
class ReasoningTrace:
  """What the agent did for one task (one ADK invocation).

  The outcome is the text of the invocation's last model response. The
  status is derived from the logged rows, not declared by the application:
  ``answered`` means a final text answer and no error rows,
  ``answered_with_errors`` a final answer after at least one error row, and
  ``unanswered`` no final text answer (still running, or it failed).
  """

  trace_id: str
  session_id: str
  user_id: Optional[str]
  task: Optional[str]
  steps: tuple[ReasoningStep, ...]
  outcome: Optional[str]
  outcome_status: str
  started_at: datetime
  completed_at: Optional[datetime]
  latency_ms: float
  llm_calls: int
  total_tokens: int
  errors: tuple[str, ...]

  @property
  def success(self) -> bool:
    return self.outcome_status == ANSWERED

  @property
  def tool_calls(self) -> tuple[ToolCall, ...]:
    return tuple(call for step in self.steps for call in step.tool_calls)

  @property
  def metrics(self) -> dict[str, Any]:
    return {
        "latency_ms": self.latency_ms,
        "llm_calls": self.llm_calls,
        "tool_calls": len(self.tool_calls),
        "tool_errors": sum(c.status == "error" for c in self.tool_calls),
        "total_tokens": self.total_tokens,
    }


@dataclasses.dataclass(frozen=True)
class SimilarTrace:
  trace: ReasoningTrace
  similarity: float


@dataclasses.dataclass(frozen=True)
class ToolStats:
  """Per-tool usage aggregated over the loaded traces."""

  name: str
  total_calls: int
  successful_calls: int
  failed_calls: int
  success_rate: float
  avg_duration_ms: Optional[float]
  last_used_at: datetime
  last_failure: Optional[ToolCall]


# ------------------------------------------------------------------ #
# Row parsing                                                          #
# ------------------------------------------------------------------ #


def _ordered_spans(trace: Trace) -> list[Span]:
  return sorted(trace.spans, key=lambda span: span.timestamp)


def _user_text(span: Span) -> Optional[str]:
  text = span.content.get("text_summary") or span.content.get("text")
  return text if isinstance(text, str) and text else None


def _unquote(value: str) -> str:
  if len(value) >= 2 and value[0] in "'\"" and value[-1] == value[0]:
    return value[1:-1]
  if value[:1] in ("'", '"'):
    return value[1:]
  return value


def _response_parts(response: Any) -> tuple[list[str], list[str]]:
  """Splits an ``LLM_RESPONSE`` ``response`` into text parts and tool calls."""
  if not isinstance(response, str) or not response:
    return [], []
  texts, calls = [], []
  for part in _RESPONSE_PART.split(response):
    if part.startswith("call: "):
      calls.append(part[len("call: ") :].strip())
    elif part.startswith("text: "):
      texts.append(_unquote(part[len("text: ") :]))
    elif not (part.startswith("resp: ") or part in ("other", "None")):
      # "None" is the plugin's placeholder for a response without parts.
      texts.append(part)
  return [text for text in texts if text], calls


def _usage_total(span: Span) -> int:
  usage = span.content.get("usage")
  if isinstance(usage, dict) and isinstance(usage.get("total"), int):
    return usage["total"]
  return 0


def _compact(value: Any, limit: int = 120) -> str:
  text = (
      value
      if isinstance(value, str)
      else json.dumps(value, ensure_ascii=False, default=str)
  )
  return text if len(text) <= limit else text[: limit - 3] + "..."


def _conversation(trace: Trace) -> list[Message]:
  messages = []
  for span in _ordered_spans(trace):
    if span.event_type == "USER_MESSAGE_RECEIVED":
      text = _user_text(span)
      if text:
        messages.append(
            Message(
                "user", text, span.timestamp, trace.session_id, span.span_id
            )
        )
    elif span.event_type == "LLM_RESPONSE":
      texts, _ = _response_parts(span.content.get("response"))
      if texts:
        messages.append(
            Message(
                "assistant",
                " ".join(texts),
                span.timestamp,
                trace.session_id,
                span.span_id,
            )
        )
  return messages


def _session_trace(traces: list[Trace], session_id: str) -> Trace:
  matches = [t for t in traces if t.session_id == session_id]
  if not matches:
    raise KeyError(f"unknown session {session_id}")
  if len(matches) > 1:
    raise ValueError(
        f"session {session_id} maps to {len(matches)} traces (different root"
        " agents or evaluation scopes); load memory with a narrower filter."
    )
  return matches[0]


def _tool_call(session_id: str, start: Optional[Span], end: Span) -> ToolCall:
  start_content = start.content if start is not None else {}
  tool = end.content.get("tool") or start_content.get("tool") or "unknown"
  arguments = start_content.get("args") or end.content.get("args") or {}
  failed = end.event_type == "TOOL_ERROR" or end.is_error
  return ToolCall(
      tool_name=tool,
      arguments=dict(arguments),
      result=None if failed else end.content.get("result"),
      status="error" if failed else "success",
      duration_ms=end.latency_ms,
      error=(end.error_message or "tool error") if failed else None,
      session_id=session_id,
      span_id=end.span_id or (start.span_id if start is not None else None),
      started_at=start.timestamp if start is not None else end.timestamp,
  )


def _pending_call(session_id: str, start: Span) -> ToolCall:
  return ToolCall(
      tool_name=start.content.get("tool") or "unknown",
      arguments=dict(start.content.get("args") or {}),
      result=None,
      status="pending",
      duration_ms=None,
      error=None,
      session_id=session_id,
      span_id=start.span_id,
      started_at=start.timestamp,
  )


def _observation(calls: list[ToolCall]) -> Optional[str]:
  parts = []
  for call in calls:
    if call.status == "error":
      parts.append(f"{call.tool_name} -> error: {call.error}")
    elif call.status == "pending":
      parts.append(f"{call.tool_name} -> pending")
    else:
      parts.append(f"{call.tool_name} -> {_compact(call.result)}")
  return "; ".join(parts) or None


@dataclasses.dataclass
class _StepDraft:
  thought: Optional[str]
  action: str
  calls: list[ToolCall] = dataclasses.field(default_factory=list)


def _reasoning_trace(
    trace: Trace, trace_id: str, spans: list[Span]
) -> ReasoningTrace:
  responses = [s for s in spans if s.event_type == "LLM_RESPONSE"]
  final = responses[-1] if responses else None
  outcome = None
  if final is not None:
    texts, calls = _response_parts(final.content.get("response"))
    if texts and not calls:
      outcome = " ".join(texts)

  task = None
  drafts: list[_StepDraft] = []
  # Tool span id -> (step index, call index, TOOL_STARTING span).
  open_calls: dict[Any, tuple[int, int, Span]] = {}
  for span in spans:
    if span.event_type == "USER_MESSAGE_RECEIVED" and task is None:
      task = _user_text(span)
    elif span.event_type == "LLM_RESPONSE":
      if span is final and outcome is not None:
        continue
      texts, calls = _response_parts(span.content.get("response"))
      action = "call: " + ", ".join(calls) if calls else "respond"
      drafts.append(_StepDraft(" ".join(texts) or None, action))
    elif span.event_type == "TOOL_STARTING":
      if not drafts:
        tool = span.content.get("tool") or "unknown"
        drafts.append(_StepDraft(None, f"call: {tool}"))
      key = span.span_id or span.content.get("tool")
      open_calls[key] = (len(drafts) - 1, len(drafts[-1].calls), span)
      drafts[-1].calls.append(_pending_call(trace.session_id, span))
    elif span.event_type in ("TOOL_COMPLETED", "TOOL_ERROR"):
      key = span.span_id or span.content.get("tool")
      if key in open_calls:
        step_index, call_index, start = open_calls.pop(key)
        drafts[step_index].calls[call_index] = _tool_call(
            trace.session_id, start, span
        )
      else:
        call = _tool_call(trace.session_id, None, span)
        if not drafts:
          drafts.append(_StepDraft(None, f"call: {call.tool_name}"))
        drafts[-1].calls.append(call)

  steps = tuple(
      ReasoningStep(
          step_number=i,
          thought=draft.thought,
          action=draft.action,
          observation=_observation(draft.calls),
          tool_calls=tuple(draft.calls),
      )
      for i, draft in enumerate(drafts, start=1)
  )
  errors = tuple(s.error_message or s.event_type for s in spans if s.is_error)
  if outcome is None:
    status = UNANSWERED
  elif errors:
    status = ANSWERED_WITH_ERRORS
  else:
    status = ANSWERED
  completed = [s for s in spans if s.event_type == "INVOCATION_COMPLETED"]
  return ReasoningTrace(
      trace_id=trace_id,
      session_id=trace.session_id,
      user_id=trace.user_id,
      task=task,
      steps=steps,
      outcome=outcome,
      outcome_status=status,
      started_at=spans[0].timestamp,
      completed_at=completed[-1].timestamp if completed else None,
      latency_ms=(spans[-1].timestamp - spans[0].timestamp).total_seconds()
      * 1000,
      llm_calls=len(responses),
      total_tokens=sum(_usage_total(s) for s in responses),
      errors=errors,
  )


def _invocation_traces(trace: Trace) -> list[ReasoningTrace]:
  groups: dict[str, list[Span]] = {}
  for span in _ordered_spans(trace):
    groups.setdefault(span.invocation_id or trace.session_id, []).append(span)
  return [
      _reasoning_trace(trace, trace_id, spans)
      for trace_id, spans in groups.items()
  ]


def _tokens(text: Optional[str]) -> frozenset[str]:
  words = set()
  for word in re.findall(r"[a-z0-9]+", (text or "").lower()):
    if len(word) < 2 or word in _STOP_WORDS:
      continue
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
      word = word[:-1]
    words.add(word)
  return frozenset(words)


def lexical_similarity(a: Optional[str], b: Optional[str]) -> float:
  """Jaccard overlap of the two texts' content words (no embeddings)."""
  left, right = _tokens(a), _tokens(b)
  if not left or not right:
    return 0.0
  return len(left & right) / len(left | right)


def _utc(value: datetime) -> str:
  return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _preview(text: Optional[str], limit: int = 80) -> Optional[str]:
  if text is None or len(text) <= limit:
    return text
  return text[: limit - 3] + "..."


# ------------------------------------------------------------------ #
# Memory layers                                                        #
# ------------------------------------------------------------------ #


class ShortTermMemory:
  """Conversations, one per session."""

  def __init__(self, traces: list[Trace]) -> None:
    self._traces = traces

  def get_conversation(self, session_id: str) -> list[Message]:
    return _conversation(_session_trace(self._traces, session_id))

  def list_sessions(self) -> list[SessionInfo]:
    """Sessions, most recently active first."""
    sessions = []
    for trace in self._traces:
      spans = _ordered_spans(trace)
      if not spans:
        continue
      messages = _conversation(trace)
      first = next((m.content for m in messages if m.role == "user"), None)
      sessions.append(
          SessionInfo(
              session_id=trace.session_id,
              created_at=spans[0].timestamp,
              updated_at=spans[-1].timestamp,
              message_count=len(messages),
              first_message_preview=_preview(first),
          )
      )
    sessions.sort(key=lambda s: s.updated_at, reverse=True)
    return sessions


class LongTermMemory:
  """User-scoped state with history, and the entities tools acted on."""

  def __init__(
      self, traces: list[Trace], entity_args: Mapping[str, str]
  ) -> None:
    self._traces = traces
    self._entity_args = dict(entity_args)

  def get_preference_history(self) -> list[Preference]:
    """Every version of every ``user:`` key, oldest first."""
    writes = []
    for trace in self._traces:
      for span in trace.spans:
        if span.event_type != "STATE_DELTA":
          continue
        delta = span.attributes.get("state_delta")
        if not isinstance(delta, dict):
          continue
        for key, value in delta.items():
          if isinstance(key, str) and key.startswith(USER_STATE_PREFIX):
            category = key[len(USER_STATE_PREFIX) :]
            writes.append(
                (
                    span.timestamp,
                    category,
                    value,
                    trace.session_id,
                    span.span_id,
                )
            )
    writes.sort(key=lambda write: (write[0], write[1]))
    history: list[Preference] = []
    latest: dict[str, int] = {}
    for timestamp, category, value, session_id, span_id in writes:
      if category in latest:
        index = latest[category]
        history[index] = dataclasses.replace(
            history[index], valid_until=timestamp
        )
      latest[category] = len(history)
      history.append(
          Preference(category, value, timestamp, None, session_id, span_id)
      )
    return history

  def get_preferences(
      self, as_of: Optional[datetime] = None
  ) -> dict[str, Preference]:
    """The version of each key valid at ``as_of`` (default: the latest)."""
    current = {}
    for pref in self.get_preference_history():
      if as_of is None:
        valid = pref.valid_until is None
      else:
        valid = pref.valid_from <= as_of and (
            pref.valid_until is None or as_of < pref.valid_until
        )
      if valid and pref.preference is not None:
        current[pref.category] = pref
    return current

  def get_entities(self) -> list[Entity]:
    """Entities named by the configured tool arguments, most used first.

    The application declares which tool arguments hold entities and their
    type, e.g. ``{"city": "LOCATION"}``. Only string values are used.
    """
    mentions: dict[tuple[str, str], list[EntityMention]] = {}
    for trace in self._traces:
      for span in _ordered_spans(trace):
        if span.event_type != "TOOL_STARTING":
          continue
        args = span.content.get("args")
        if not isinstance(args, dict):
          continue
        for argument, entity_type in self._entity_args.items():
          value = args.get(argument)
          if isinstance(value, str) and value.strip():
            mentions.setdefault((value.strip(), entity_type), []).append(
                EntityMention(
                    tool_name=span.content.get("tool") or "unknown",
                    argument=argument,
                    session_id=trace.session_id,
                    span_id=span.span_id,
                    timestamp=span.timestamp,
                )
            )
    entities = [
        Entity(
            name, entity_type, tuple(sorted(found, key=lambda m: m.timestamp))
        )
        for (name, entity_type), found in mentions.items()
    ]
    entities.sort(key=lambda e: e.name)
    entities.sort(key=lambda e: e.last_seen, reverse=True)
    entities.sort(key=lambda e: len(e.mentions), reverse=True)
    return entities


class ReasoningMemory:
  """One reasoning trace per ADK invocation."""

  def __init__(self, traces: list[Trace]) -> None:
    self._traces = traces
    self._all = [rt for trace in traces for rt in _invocation_traces(trace)]

  def get_session_traces(self, session_id: str) -> list[ReasoningTrace]:
    """The traces of one session, oldest first."""
    return _invocation_traces(_session_trace(self._traces, session_id))

  def get_trace_with_steps(self, trace_id: str) -> ReasoningTrace:
    matches = [rt for rt in self._all if rt.trace_id == trace_id]
    if not matches:
      raise KeyError(f"unknown trace {trace_id}")
    if len(matches) > 1:
      raise ValueError(f"trace {trace_id} appears in {len(matches)} sessions")
    return matches[0]

  def list_traces(
      self,
      *,
      success_only: Optional[bool] = None,
      since: Optional[datetime] = None,
      until: Optional[datetime] = None,
  ) -> list[ReasoningTrace]:
    """Traces started in ``[since, until]``, newest first.

    ``success_only=True`` keeps ``answered`` traces, ``False`` keeps the
    rest, and ``None`` keeps all. The outcome is derived after the fetch:
    ``TraceFilter(has_error=False)`` does not exclude sessions with error
    rows (it only requires one non-error row).
    """
    traces = [
        rt
        for rt in self._all
        if (success_only is None or rt.success == success_only)
        and (since is None or rt.started_at >= since)
        and (until is None or rt.started_at <= until)
    ]
    traces.sort(key=lambda rt: rt.started_at, reverse=True)
    return traces

  def get_similar_traces(
      self,
      task: str,
      *,
      limit: int = 3,
      success_only: bool = True,
      threshold: float = 0.2,
      exclude_session_id: Optional[str] = None,
  ) -> list[SimilarTrace]:
    """Past traces whose task shares words with ``task``, best first."""
    scored = []
    for rt in self.list_traces(success_only=True if success_only else None):
      if exclude_session_id is not None and rt.session_id == exclude_session_id:
        continue
      score = lexical_similarity(task, rt.task)
      if score >= threshold:
        scored.append(SimilarTrace(rt, score))
    scored.sort(key=lambda s: s.similarity, reverse=True)
    return scored[:limit]

  def get_tool_stats(self, tool_name: Optional[str] = None) -> list[ToolStats]:
    """Per-tool call counts, success rate and latency, most used first."""
    calls: dict[str, list[ToolCall]] = {}
    for rt in self._all:
      for call in rt.tool_calls:
        if tool_name is None or call.tool_name == tool_name:
          calls.setdefault(call.tool_name, []).append(call)
    stats = []
    for name, found in calls.items():
      durations = [c.duration_ms for c in found if c.duration_ms is not None]
      failures = [c for c in found if c.status == "error"]
      successes = sum(c.status == "success" for c in found)
      stats.append(
          ToolStats(
              name=name,
              total_calls=len(found),
              successful_calls=successes,
              failed_calls=len(failures),
              success_rate=successes / len(found),
              avg_duration_ms=(
                  sum(durations) / len(durations) if durations else None
              ),
              last_used_at=max(c.started_at for c in found),
              last_failure=(
                  max(failures, key=lambda c: c.started_at)
                  if failures
                  else None
              ),
          )
      )
    stats.sort(key=lambda s: s.name)
    stats.sort(key=lambda s: s.total_calls, reverse=True)
    return stats


class UserMemory:
  """All three memory layers for one user."""

  def __init__(
      self,
      user_id: str,
      traces: list[Trace],
      *,
      entity_args: Optional[Mapping[str, str]] = None,
  ) -> None:
    self.user_id = user_id
    self.traces = list(traces)
    self.short_term = ShortTermMemory(self.traces)
    self.long_term = LongTermMemory(self.traces, entity_args or {})
    self.reasoning = ReasoningMemory(self.traces)

  def get_context(
      self, query: str, *, session_id: str, max_items: int = 5
  ) -> str:
    """A prompt-ready block combining the three layers.

    Each line ends with ``[session/span]`` (or ``[session/trace]``) so the
    model's context can be traced back to the rows it came from.
    """
    lines = [f"# Memory for user {self.user_id}", ""]

    lines.append(f"## Short-term: current conversation (session {session_id})")
    try:
      conversation = self.short_term.get_conversation(session_id)
    except KeyError:
      conversation = []
    lines += [
        f"- {m.role}: {m.content} [{m.session_id}/{m.span_id}]"
        for m in conversation[-max_items:]
    ] or ["- (none)"]

    lines += ["", "## Long-term: user preferences (ADK user: state)"]
    history = self.long_term.get_preference_history()
    current = self.long_term.get_preferences()
    entries = []
    for category in sorted(current)[:max_items]:
      pref = current[category]
      replaced = [
          h.preference
          for h in history
          if h.category == category and h.valid_until == pref.valid_from
      ]
      note = f"; replaced {replaced[-1]}" if replaced else ""
      entries.append(
          f"- {category} = {pref.preference} (since"
          f" {_utc(pref.valid_from)}{note}) [{pref.session_id}/{pref.span_id}]"
      )
    lines += entries or ["- (none)"]

    lines += ["", "## Long-term: entities the agent acted on"]
    lines += [
        f"- {e.name} ({e.entity_type}): {len(e.mentions)} tool"
        f" call{'s' if len(e.mentions) != 1 else ''} in {', '.join(e.sessions)}"
        for e in self.long_term.get_entities()[:max_items]
    ] or ["- (none)"]

    lines += ["", "## Reasoning: similar past tasks that succeeded"]
    similar = self.reasoning.get_similar_traces(
        query, limit=max_items, exclude_session_id=session_id
    )
    entries = []
    for match in similar:
      rt = match.trace
      tools = ", ".join(dict.fromkeys(c.tool_name for c in rt.tool_calls))
      entries.append(
          f'- {match.similarity:.2f} "{rt.task}" -> {tools or "no tools"} ->'
          f' "{rt.outcome}" [{rt.session_id}/{rt.trace_id}]'
      )
    lines += entries or ["- (none)"]

    lines += ["", "## Reasoning: tools that failed before"]
    failing = [
        s for s in self.reasoning.get_tool_stats() if s.last_failure is not None
    ]
    lines += [
        f"- {s.name} failed {s.failed_calls} of {s.total_calls} calls; last"
        f" error: {s.last_failure.error}"
        f" [{s.last_failure.session_id}/{s.last_failure.span_id}]"
        for s in failing[:max_items]
    ] or ["- (none)"]
    return "\n".join(lines)


def load_user_memory(
    client: Client,
    user_id: str,
    *,
    since: Optional[datetime] = None,
    limit: int = 100,
    entity_args: Optional[Mapping[str, str]] = None,
) -> UserMemory:
  """Reads one user's traces with a single ``Client.list_traces`` call.

  The ``user_id`` pin is applied in SQL, so other users' rows are never
  fetched. It is a filter, not an access control: restrict who can read
  the table with BigQuery IAM or row-level security.
  """
  traces = client.list_traces(
      TraceFilter(user_id=user_id, start_time=since, limit=limit)
  )
  return UserMemory(user_id, traces, entity_args=entity_args)
