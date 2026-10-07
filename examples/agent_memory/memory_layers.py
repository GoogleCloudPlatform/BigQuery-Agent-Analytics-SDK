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
  its tools. Facts and entities extracted from the conversations by a
  separate job (``memory_consolidation.py`` runs ``AI.GENERATE`` over the
  same rows) can be passed in as well;
* reasoning: one trace per ADK invocation, with a step per model turn, the
  tool calls that turn made, and a derived outcome.

Every item keeps the ``session_id`` and ``span_id`` of the row it came from.
"""

from __future__ import annotations

import bisect
import dataclasses
from datetime import datetime
from datetime import timezone
import json
import re
from typing import Any, Iterable, Mapping, Optional

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
# "call: <tool>", "resp: <tool>" or "other". Text is not escaped, so a text
# part can itself contain " | call: ..."; see response_parts().
_PART_SEPARATOR = " | "
_TEXT_PREFIX = "text: '"
# A quote can close a text part only where a separator or the end follows.
_TEXT_END = re.compile(r"'(?= \| |\Z)")
# The plugin cuts an over-long payload and appends this marker, so the last
# text part of a truncated response has no closing quote.
_TRUNCATED = "...[TRUNCATED]"
_TOOL_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.\-]*")
# Bounds on the search for readings, so crafted text cannot stall it.
_MAX_READINGS = 256
_MAX_STEPS = 8192

# google-adk 2.11 writes these attributes only on a terminal (non-partial)
# LLM_RESPONSE; streaming fragments carry neither.
_TERMINAL_MARKERS = ("cache_type", "finish_reason")

# Long outcomes and tool arguments are shortened in get_context().
_OUTCOME_LIMIT = 400
_REUSE_LIMIT = 1500

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
  """One user or assistant turn of a conversation.

  An assistant message is one model call. ``complete`` is False when only
  streaming fragments of it were recorded (the stream never finished).
  """

  role: str
  content: str
  timestamp: datetime
  session_id: str
  span_id: Optional[str]
  complete: bool = True


@dataclasses.dataclass(frozen=True)
class SessionInfo:
  """A session with its time range and message count.

  ``state`` is the session-scoped state the plugin logged with the
  session's first row (``attributes.session_metadata.state``), without the
  ``user:``, ``app:`` and ``temp:`` keys.
  """

  session_id: str
  created_at: datetime
  updated_at: datetime
  message_count: int
  first_message_preview: Optional[str]
  state: Mapping[str, Any] = dataclasses.field(default_factory=dict, hash=False)


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
  """Where an entity appeared.

  ``source`` is ``"tool"`` for a tool call that received the entity as an
  argument (``tool_name`` and ``argument`` say which), or ``"extracted"``
  for a message it was extracted from.
  """

  tool_name: Optional[str]
  argument: Optional[str]
  session_id: str
  span_id: Optional[str]
  timestamp: datetime
  source: str = "tool"


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
class Fact:
  """A fact extracted from a message, with the row it came from.

  ``subject``, ``predicate`` and ``object`` hold the relation (for example
  "Maya Chen", "owns_category", "Jeans"), and ``statement`` the same fact
  as a sentence.
  """

  subject: str
  subject_type: str
  predicate: str
  object: str
  object_type: str
  statement: str
  session_id: str
  span_id: Optional[str]
  observed_at: datetime


@dataclasses.dataclass(frozen=True)
class ExtractedEntity:
  """An entity named in a message, as extracted from it."""

  name: str
  entity_type: str
  session_id: str
  span_id: Optional[str]
  observed_at: datetime


@dataclasses.dataclass(frozen=True)
class ToolCall:
  """A tool call: ``TOOL_STARTING`` paired with its completion or error.

  A completion whose result is a dict with ``"status": "error"`` (a tool
  that reports a failure instead of raising) counts as an error too.
  """

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
class ModelCall:
  """One model call: its ``LLM_REQUEST`` and every response row of its span.

  A streamed call writes several ``LLM_RESPONSE`` rows on one span. The
  call is ``complete`` when a terminal response row was recorded, or (for
  producers that write no terminal marker) when a non-error row follows its
  last response row. Only a complete call's text counts as an answer.
  """

  span_id: Optional[str]
  started_at: datetime
  ended_at: datetime
  texts: tuple[str, ...]
  calls: tuple[str, ...]
  complete: bool
  failed: bool
  error: Optional[str]
  total_tokens: int
  last_index: int  # position of its last row in the invocation's rows


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

  The outcome is the text of the invocation's last model call, and only
  when that call completed (a terminal response row was recorded) with text
  and no tool calls. The status is derived from the logged rows, not
  declared by the application: ``answered`` means such a final answer and
  no error rows, ``answered_with_errors`` a final answer after at least one
  error row, and ``unanswered`` no recorded final answer (still running, an
  interrupted stream, or a failure). ``outcome_span_id`` is the span of the
  model call that produced the answer.
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
  outcome_span_id: Optional[str] = None

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


def _parts_at(
    response: str,
    pos: int,
    text_ends: list[int],
    truncated: bool,
    longest_first: bool,
):
  """Yields ``(end, (kind, value))`` for each part that can start at pos.

  ``text_ends`` are the quotes that can close a text part. Quotes inside
  the text are not escaped, so each one after ``pos`` is a candidate end.
  """
  if response.startswith(_TEXT_PREFIX, pos):
    start = pos + len(_TEXT_PREFIX)
    closes = range(bisect.bisect_left(text_ends, start), len(text_ends))
    if longest_first:
      if truncated:
        yield len(response), ("text", response[start:])
      closes = reversed(closes)
    for i in closes:
      yield text_ends[i] + 1, ("text", response[start : text_ends[i]])
    if truncated and not longest_first:
      yield len(response), ("text", response[start:])
  for prefix, kind in (("call: ", "call"), ("resp: ", "resp")):
    if response.startswith(prefix, pos):
      end = response.find(_PART_SEPARATOR, pos)
      end = len(response) if end == -1 else end
      name = response[pos + len(prefix) : end]
      if _TOOL_NAME.fullmatch(name):
        yield end, (kind, name)
  if response.startswith("other", pos):
    end = pos + len("other")
    if end == len(response) or response.startswith(_PART_SEPARATOR, end):
      yield end, ("other", "")


def _readings(
    response: str, longest_first: bool = True
) -> list[list[tuple[str, str]]]:
  """Ways to read ``response`` as plugin parts joined by `` | ``.

  A depth-first search. Trying longer text parts first finds the readings
  that keep text whole before the bounds can cut the search short; trying
  shorter ones first finds the most finely split readings. A position that
  cannot reach the end is not searched twice.
  """
  text_ends = [match.start() for match in _TEXT_END.finditer(response)]
  truncated = response.endswith(_TRUNCATED)

  def parts_at(pos: int):
    return _parts_at(response, pos, text_ends, truncated, longest_first)

  readings: list[list[tuple[str, str]]] = []
  dead: set[int] = set()
  # Frames: (start, parts before it, its candidate parts, readings on entry).
  stack = [(0, [], parts_at(0), 0)]
  steps = 0
  while stack and len(readings) < _MAX_READINGS and steps < _MAX_STEPS:
    pos, parts, candidates, found = stack[-1]
    candidate = next(candidates, None)
    if candidate is None:
      stack.pop()
      if len(readings) == found:
        dead.add(pos)
      continue
    steps += 1
    end, part = candidate
    if end == len(response):
      readings.append(parts + [part])
    elif end + len(_PART_SEPARATOR) not in dead:
      start = end + len(_PART_SEPARATOR)
      stack.append((start, parts + [part], parts_at(start), len(readings)))
  return readings


def _evidence_score(
    reading: list[tuple[str, str]], executed_tools: list[str]
) -> int:
  """Calls confirmed by recorded tool calls, minus calls with no record."""
  available = list(executed_tools)
  score = 0
  for kind, value in reading:
    if kind != "call":
      continue
    if value in available:
      available.remove(value)
      score += 1
    else:
      score -= 1
  return score


def response_parts(
    response: Any, executed_tools: Optional[list[str]] = None
) -> tuple[list[str], list[str]]:
  """Reads an ``LLM_RESPONSE`` ``response`` into text parts and tool calls.

  The plugin joins ``text: '...'``, ``call: <tool>``, ``resp: <tool>`` and
  ``other`` parts with `` | `` and does not escape the text, so text that
  itself contains `` | call: x`` can be read more than one way. The
  structurally valid readings are compared. When several fit, the tools the
  model call actually ran (``executed_tools``, from the ``TOOL_STARTING``
  rows that follow it) pick the reading; without that evidence the reading
  with the fewest parts wins, keeping the text whole and inventing no call.
  A truncated response keeps the plugin's marker on its last text part. A
  response that is not in the part format at all is returned as text.
  """
  if not isinstance(response, str) or not response or response == "None":
    # "None" is the plugin's placeholder for a response without parts.
    return [], []
  readings = _readings(response)
  if not readings:
    text = response
    if text.startswith(_TEXT_PREFIX):
      text = text[len(_TEXT_PREFIX) :]
      text = text[:-1] if text.endswith("'") else text
    return ([text] if text else []), []
  if len(readings) > 1 and executed_tools is not None:
    # Evidence can favour a finely split reading that the search for
    # whole-text readings did not reach in a long response.
    readings += _readings(response, longest_first=False)
    best = max(
        readings, key=lambda r: (_evidence_score(r, executed_tools), -len(r))
    )
  else:
    best = min(readings, key=len)
  texts = [value for kind, value in best if kind == "text" and value]
  calls = [value for kind, value in best if kind == "call"]
  return texts, calls


def _usage_total(span: Span) -> int:
  usage = span.content.get("usage")
  if isinstance(usage, dict) and isinstance(usage.get("total"), int):
    return usage["total"]
  return 0


def _is_terminal_response(span: Span) -> bool:
  return span.event_type == "LLM_RESPONSE" and any(
      key in span.attributes for key in _TERMINAL_MARKERS
  )


def model_calls(spans: list[Span]) -> list[ModelCall]:
  """The model calls of one invocation, in the order they finished.

  ``spans`` are one invocation's rows in time order. Response rows are
  grouped by span id, so the fragments of a streamed call form one call.
  """
  groups: dict[str, list[tuple[int, Span]]] = {}
  requests: dict[str, Span] = {}
  for index, span in enumerate(spans):
    if span.event_type == "LLM_REQUEST" and span.span_id:
      requests.setdefault(span.span_id, span)
    elif span.event_type in ("LLM_RESPONSE", "LLM_ERROR"):
      groups.setdefault(span.span_id or f"row-{index}", []).append(
          (index, span)
      )
  firsts = sorted(rows[0][0] for rows in groups.values())
  calls = []
  for rows in sorted(groups.values(), key=lambda rows: rows[-1][0]):
    last = rows[-1][0]
    responses = [span for _, span in rows if span.event_type == "LLM_RESPONSE"]
    failed = any(
        span.event_type == "LLM_ERROR" or span.is_error for _, span in rows
    )
    terminal = [span for span in responses if _is_terminal_response(span)]
    following = spans[last + 1] if last + 1 < len(spans) else None
    if failed:
      complete = False
    elif terminal:
      complete = True
    else:
      complete = following is not None and not following.is_error
    # Tools it asked for: TOOL_STARTING rows before the next model call.
    upto = next((i for i in firsts if i > last), len(spans))
    executed = [
        span.content.get("tool") or "unknown"
        for span in spans[last + 1 : upto]
        if span.event_type == "TOOL_STARTING"
    ]
    texts: list[str] = []
    called: list[str] = []
    if complete:
      sources = terminal or responses[-1:]
      for span in sources:
        found, asked = response_parts(
            span.content.get("response"),
            executed if len(sources) == 1 else None,
        )
        texts += [text for text in found if text not in texts]
        called += asked
    else:
      # Fragments are deltas: join them, but do not call them an answer.
      fragments = []
      for span in responses:
        found, asked = response_parts(span.content.get("response"))
        fragments += found
        called += asked
      texts = ["".join(fragments)] if fragments else []
    first = rows[0][1]
    request = requests.get(first.span_id) if first.span_id else None
    started = (
        request.timestamp
        if request is not None and request.timestamp <= first.timestamp
        else first.timestamp
    )
    calls.append(
        ModelCall(
            span_id=rows[-1][1].span_id,
            started_at=started,
            ended_at=rows[-1][1].timestamp,
            texts=tuple(texts),
            calls=tuple(called),
            complete=complete,
            failed=failed,
            error=next(
                (span.error_message for _, span in rows if span.error_message),
                None,
            ),
            # Streamed usage is cumulative, so take the largest, not the sum.
            total_tokens=max((_usage_total(s) for s in responses), default=0),
            last_index=last,
        )
    )
  return calls


def _compact(value: Any, limit: int = 120) -> str:
  text = (
      value
      if isinstance(value, str)
      else json.dumps(value, ensure_ascii=False, default=str)
  )
  return text if len(text) <= limit else text[: limit - 3] + "..."


def _conversation(trace: Trace) -> list[Message]:
  messages = []
  for spans in spans_by_invocation(trace).values():
    for span in spans:
      if span.event_type == "USER_MESSAGE_RECEIVED":
        text = _user_text(span)
        if text:
          messages.append(
              Message(
                  "user", text, span.timestamp, trace.session_id, span.span_id
              )
          )
    for call in model_calls(spans):
      if call.texts:
        messages.append(
            Message(
                "assistant",
                " ".join(call.texts),
                call.ended_at,
                trace.session_id,
                call.span_id,
                complete=call.complete,
            )
        )
  messages.sort(key=lambda message: message.timestamp)
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
  result = None if failed else end.content.get("result")
  reported = isinstance(result, dict) and result.get("status") == "error"
  if failed:
    error = end.error_message or "tool error"
  elif reported:
    error = str(result.get("message") or "the tool reported an error")
  else:
    error = None
  return ToolCall(
      tool_name=tool,
      arguments=dict(arguments),
      result=result,
      status="error" if failed or reported else "success",
      duration_ms=end.latency_ms,
      error=error,
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
  model = model_calls(spans)
  by_last_row = {call.last_index: call for call in model}
  final = model[-1] if model else None
  outcome = None
  if final is not None and final.complete and final.texts and not final.calls:
    outcome = " ".join(final.texts)

  task = None
  drafts: list[_StepDraft] = []
  # Tool span id -> (step index, call index, TOOL_STARTING span).
  open_calls: dict[Any, tuple[int, int, Span]] = {}
  for index, span in enumerate(spans):
    if span.event_type == "USER_MESSAGE_RECEIVED" and task is None:
      task = _user_text(span)
    elif index in by_last_row:
      call = by_last_row[index]
      if call is final and outcome is not None:
        continue
      if call.failed:
        action = "model call failed"
      elif call.calls:
        action = "call: " + ", ".join(call.calls)
      else:
        action = "respond" if call.complete else "respond (incomplete)"
      drafts.append(_StepDraft(" ".join(call.texts) or None, action))
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
      llm_calls=len(model),
      total_tokens=sum(call.total_tokens for call in model),
      errors=errors,
      outcome_span_id=final.span_id if outcome is not None else None,
  )


def spans_by_invocation(trace: Trace) -> dict[str, list[Span]]:
  """A session's spans grouped by ADK invocation, in time order."""
  groups: dict[str, list[Span]] = {}
  for span in _ordered_spans(trace):
    groups.setdefault(span.invocation_id or trace.session_id, []).append(span)
  return groups


def _invocation_traces(trace: Trace) -> list[ReasoningTrace]:
  return [
      _reasoning_trace(trace, trace_id, spans)
      for trace_id, spans in spans_by_invocation(trace).items()
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


def _session_state(spans: list[Span]) -> dict[str, Any]:
  """Session-scoped state from the first row that logged session metadata."""
  for span in spans:
    metadata = span.attributes.get("session_metadata")
    state = metadata.get("state") if isinstance(metadata, dict) else None
    if isinstance(state, dict):
      return {
          key: value
          for key, value in state.items()
          if not key.startswith((USER_STATE_PREFIX, "app:", "temp:"))
      }
  return {}


def _entity_key(name: str, entity_type: str) -> tuple[str, str]:
  return (" ".join(name.split()).casefold(), entity_type.strip().upper())


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
              state=_session_state(spans),
          )
      )
    sessions.sort(key=lambda s: s.updated_at, reverse=True)
    return sessions


class LongTermMemory:
  """User-scoped state with history, entities, and extracted facts."""

  def __init__(
      self,
      traces: list[Trace],
      entity_args: Mapping[str, str],
      facts: Iterable[Fact] = (),
      extracted_entities: Iterable[ExtractedEntity] = (),
  ) -> None:
    self._traces = traces
    self._entity_args = dict(entity_args)
    self._facts = sorted(facts, key=lambda f: f.observed_at)
    self._extracted = list(extracted_entities)

  def get_facts(self) -> list[Fact]:
    """Extracted facts, oldest first."""
    return list(self._facts)

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
    """Entities from tool arguments and extraction, most mentioned first.

    The application declares which tool arguments hold entities and their
    type, e.g. ``{"city": "LOCATION"}``; only string values are used.
    Extracted entities are added with ``source="extracted"``. Names that
    differ only in case or spacing are one entity, shown as first seen.
    """
    mentions: dict[tuple[str, str], list[EntityMention]] = {}
    names: dict[tuple[str, str], tuple[str, str]] = {}
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
            key = _entity_key(value, entity_type)
            names.setdefault(key, (value.strip(), entity_type))
            mentions.setdefault(key, []).append(
                EntityMention(
                    tool_name=span.content.get("tool") or "unknown",
                    argument=argument,
                    session_id=trace.session_id,
                    span_id=span.span_id,
                    timestamp=span.timestamp,
                )
            )
    for item in sorted(self._extracted, key=lambda e: e.observed_at):
      if not item.name.strip():
        continue
      key = _entity_key(item.name, item.entity_type)
      names.setdefault(key, (item.name.strip(), item.entity_type.strip()))
      mentions.setdefault(key, []).append(
          EntityMention(
              tool_name=None,
              argument=None,
              session_id=item.session_id,
              span_id=item.span_id,
              timestamp=item.observed_at,
              source="extracted",
          )
      )
    entities = [
        Entity(*names[key], tuple(sorted(found, key=lambda m: m.timestamp)))
        for key, found in mentions.items()
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
      scores: Optional[Mapping[str, float]] = None,
  ) -> list[SimilarTrace]:
    """Past traces similar to ``task``, best first.

    By default similarity is the word overlap of the two tasks. Pass
    ``scores`` (trace id to similarity, for example from an embedding
    search) to rank by those instead; traces without a score are skipped.
    """
    scored = []
    for rt in self.list_traces(success_only=True if success_only else None):
      if exclude_session_id is not None and rt.session_id == exclude_session_id:
        continue
      if scores is None:
        score = lexical_similarity(task, rt.task)
      elif rt.trace_id in scores:
        score = scores[rt.trace_id]
      else:
        continue
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
      facts: Iterable[Fact] = (),
      extracted_entities: Iterable[ExtractedEntity] = (),
  ) -> None:
    self.user_id = user_id
    self.traces = list(traces)
    self.short_term = ShortTermMemory(self.traces)
    self.long_term = LongTermMemory(
        self.traces, entity_args or {}, facts, extracted_entities
    )
    self.reasoning = ReasoningMemory(self.traces)

  def get_context(
      self,
      query: str,
      *,
      session_id: str,
      max_items: int = 5,
      scores: Optional[Mapping[str, float]] = None,
      threshold: Optional[float] = None,
      reuse_tools: Iterable[str] = (),
  ) -> str:
    """A prompt-ready block combining the three layers.

    Every line ends with ``[session/span]`` source references (several for
    an entity, the newest ``max_items`` of them) so each fact in the model's
    context can be traced back to the rows it came from. ``scores`` and
    ``threshold`` go to ``get_similar_traces``. For tools named in
    ``reuse_tools``, a similar past task also shows its last successful
    call of such a tool, with the arguments (the SQL that answered, say),
    so the model can adapt it instead of starting over.
    """
    lines = [f"# Memory for user {self.user_id}", ""]

    lines.append(f"## Short-term: current conversation (session {session_id})")
    try:
      conversation = self.short_term.get_conversation(session_id)
    except KeyError:
      conversation = []
    lines += [
        f"- {m.role}{'' if m.complete else ' (incomplete)'}: {m.content}"
        f" [{m.session_id}/{m.span_id}]"
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

    facts = self.long_term.get_facts()
    if facts:
      # One line per distinct relation, from its newest source.
      newest: dict[tuple[str, str, str], Fact] = {}
      for fact in facts:
        relation = (fact.subject, fact.predicate, fact.object)
        newest[tuple(part.casefold() for part in relation)] = fact
      shown_facts = sorted(newest.values(), key=lambda f: f.observed_at)
      lines += ["", "## Long-term: facts from earlier conversations"]
      lines += [
          f"- {f.statement or f'{f.subject} {f.predicate} {f.object}'}"
          f" [{f.session_id}/{f.span_id}]"
          for f in shown_facts[-2 * max_items :]
      ]

    entities = self.long_term.get_entities()
    from_tools = all(m.source == "tool" for e in entities for m in e.mentions)
    lines.append("")
    lines.append(
        "## Long-term: entities the agent acted on"
        if from_tools
        else "## Long-term: entities from conversations and tool calls"
    )
    entries = []
    for entity in entities[:max_items]:
      shown = entity.mentions[-max_items:]
      older = len(entity.mentions) - len(shown)
      sources = [f"{m.session_id}/{m.span_id}" for m in shown]
      if older:
        sources.insert(0, f"+{older} earlier")
      count = len(entity.mentions)
      if all(m.source == "tool" for m in entity.mentions):
        noun = f"tool call{'s' if count != 1 else ''}"
      else:
        noun = f"mention{'s' if count != 1 else ''}"
      entries.append(
          f"- {entity.name} ({entity.entity_type}): {count} {noun}"
          f" [{', '.join(sources)}]"
      )
    lines += entries or ["- (none)"]

    lines += ["", "## Reasoning: similar past tasks that succeeded"]
    similar = self.reasoning.get_similar_traces(
        query,
        limit=max_items,
        exclude_session_id=session_id,
        scores=scores,
        **({} if threshold is None else {"threshold": threshold}),
    )
    reuse = frozenset(reuse_tools)
    entries = []
    for match in similar:
      rt = match.trace
      tools = ", ".join(dict.fromkeys(c.tool_name for c in rt.tool_calls))
      entries.append(
          f'- {match.similarity:.2f} trace {rt.trace_id}: "{rt.task}" ->'
          f' {tools or "no tools"} -> "{_preview(rt.outcome, _OUTCOME_LIMIT)}"'
          f" [{rt.session_id}/{rt.outcome_span_id}]"
      )
      call = next(
          (
              c
              for c in reversed(rt.tool_calls)
              if c.tool_name in reuse and c.status == "success"
          ),
          None,
      )
      if call is not None:
        args = json.dumps(call.arguments, ensure_ascii=False, sort_keys=True)
        entries.append(
            f"  reuse: {call.tool_name}({_preview(args, _REUSE_LIMIT)})"
            f" [{call.session_id}/{call.span_id}]"
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
    facts: Iterable[Fact] = (),
    extracted_entities: Iterable[ExtractedEntity] = (),
) -> UserMemory:
  """Reads one user's traces with a single ``Client.list_traces`` call.

  The ``user_id`` pin is applied in SQL, so other users' rows are never
  fetched. It is a filter, not an access control: restrict who can read
  the table with BigQuery IAM or row-level security. Extracted ``facts``
  and ``extracted_entities`` are not read here; pass this user's, as loaded
  by ``memory_consolidation.load_memory_items``.
  """
  traces = client.list_traces(
      TraceFilter(user_id=user_id, start_time=since, limit=limit)
  )
  return UserMemory(
      user_id,
      traces,
      entity_args=entity_args,
      facts=facts,
      extracted_entities=extracted_entities,
  )
