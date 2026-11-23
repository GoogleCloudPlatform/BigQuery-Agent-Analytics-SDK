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

"""Rule-based span attribution for failed sessions (#435).

``span_taxonomy`` localizes the three categories the mechanical
three-flag verdict can emit, at full confidence. This module adds
deterministic *rules* that infer four G1-frozen categories from the event
stream of one failed session: two the mechanical verdict never emits
(``turn-waste`` and ``incomplete evidence``), and finer diagnoses for two
it does (``tool blockers`` and ``finalization``):

* ``tool blockers``: the run was aborted by an error (``TB-ABORT``), never
  called a tool (``TB-NO-TOOLS``), or ended on a tool call that failed at
  the tool level (``TB-UNRECOVERED``).
* ``finalization``: the answer contains the reference value in the wrong
  form (``FIN-IN-ANSWER``), a tool output already showed the reference
  value but the agent never reported it (``FIN-SEEN-NOT-REPORTED``), or
  the run completed without a final response (``FIN-NO-ANSWER``).
* ``turn-waste``: the run hit the LLM call limit (``TW-CALL-CAP``), or
  spent much of a completed run on failed and repeated calls
  (``TW-THRASH``, supporting evidence only).
* ``incomplete evidence``: the answer rests on previews only
  (``IE-PREVIEW-ONLY``), or the final computation used lines pasted from a
  preview instead of reading the source (``IE-PASTED-PREVIEW``).

Contracts honored:

* **A separate label type.** Rules infer; they do not observe. Their
  output is ``RuleFailureLabel``, which carries the ``rule_id`` that fired
  and a confidence in (0, 1]. ``SpanFailureLabel`` and its mechanical
  contract are unchanged.
* **Frozen names only.** Every label uses a G1-frozen name
  (``failure_taxonomy``, taxonomy v0.1.0) other than ``unknown``.
* **No synthetic span identifiers.** Every ``span_id`` is the real native
  ``span_id`` of an input row. ``target_kind="gap_after_span"`` marks a
  failure that is the *absence* of an action after that span (for example
  a value that was printed but never reported); the evidence says which.
* **No agent-specific names.** Tool names and output formats differ per
  agent, so rules that need them read a ``ToolProfile``. Without one, only
  the rules that rely on the event stream alone run.
* **Benchmark checks are pluggable.** ``reference_answer`` enables the
  rules that need the expected answer; anything benchmark-specific (data
  sources, sub-answers, re-running the agent's code) is a ``checks``
  callable that reads the same ``SessionFacts`` the built-in rules read.
* **At-least-once writes.** Rows that repeat an ``event_id`` are dropped
  before anything is counted.
* **Offline.** Pure and deterministic: no BigQuery, no LLM, no network.
"""

from __future__ import annotations

import ast
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
import dataclasses
import json
import re
from typing import Any, Optional, Union

from .evalbench import _usable_text
from .failure_taxonomy import FROZEN_CATEGORY_NAMES
from .span_taxonomy import _ordered_single_session
from .span_taxonomy import TARGET_GAP_AFTER_SPAN
from .span_taxonomy import TARGET_SPAN
from .trace import Span
from .trace import Trace

TOOL_BLOCKERS = "tool blockers"
FINALIZATION = "finalization"
TURN_WASTE = "turn-waste"
INCOMPLETE_EVIDENCE = "incomplete evidence"

# Every frozen name except ``unknown``, which is the absence of a
# diagnosis rather than one.
RULE_CATEGORY_NAMES = tuple(
    name for name in FROZEN_CATEGORY_NAMES if name != "unknown"
)

# Thresholds and confidences come from the diagnostic engine described in
# #435, where they were set on KramaBench runs. They are module constants
# so they can be revisited in one place once more agents are labeled.
CALL_CAP_WASTE_RATIO = 0.25
THRASH_MIN_RATIO = 0.4
THRASH_MIN_WASTED = 6
NEAR_DUPLICATE_JACCARD = 0.75
NEAR_DUPLICATE_MIN_WORDS = 3
PASTED_MIN_LINES = 3
PASTED_MIN_LINE_CHARS = 8
SEEN_MIN_SIGNIFICANT_DIGITS = 3
DENSE_SERIES_MIN = 20
YEAR_RANGE = (1800, 2100)

_CALL_LIMIT_RE = re.compile(r"Max number of llm calls|LlmCallsLimitExceeded")
_CALL_LIMIT_SIZE_RE = re.compile(r"limit of `?(\d+)`? exceeded")
_NUMBER_RE = re.compile(
    r"(?<![\w.])[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?:[eE][-+]?\d+)?"
    r"(?![\w])"
)
_BARE_NUMBER_RE = re.compile(r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")
_LITERAL_RE = re.compile(r"[\[(][^\[\]()]{0,4000}[\])]")
_WORD_RE = re.compile(r"\w+")
_IS_TAIL_RE = re.compile(r"\bis\Z")
_MARK_RE = re.compile(r"[:=]|\bis")
_URL_RE = re.compile(r"https?://\S+")
_STOPWORD_ANSWERS = frozenset(
    {"yes", "no", "true", "false", "nan", "none", "null"}
)
_TOOL_END_EVENTS = ("TOOL_COMPLETED", "TOOL_ERROR")
_TOOL_EVENTS = ("TOOL_STARTING",) + _TOOL_END_EVENTS


@dataclasses.dataclass(frozen=True)
class RuleFailureLabel:
  """One G1-frozen category inferred by a rule, on one real native span.

  ``as_tuple()`` is the RFC #435 Phase 2 shape ``(trace_id, span_id,
  failure_category, evidence, confidence)``, the same as
  ``SpanFailureLabel.as_tuple()``. ``rule_id`` names the rule that fired.
  ``primary`` is False for supporting evidence that should not be a
  session's diagnosis on its own (see ``primary_label``).
  """

  session_id: str
  trace_id: Optional[str]
  span_id: str
  failure_category: str
  evidence: str
  confidence: float
  target_kind: str
  rule_id: str
  primary: bool = True

  def __post_init__(self) -> None:
    if self.failure_category not in RULE_CATEGORY_NAMES:
      raise ValueError(
          f"failure_category {self.failure_category!r} is not one of the"
          f" G1-frozen names {RULE_CATEGORY_NAMES!r} (taxonomy v0.1.0)"
      )
    if self.target_kind not in (TARGET_SPAN, TARGET_GAP_AFTER_SPAN):
      raise ValueError(
          f"target_kind must be {TARGET_SPAN!r} or"
          f" {TARGET_GAP_AFTER_SPAN!r}, got {self.target_kind!r}"
      )
    if not isinstance(self.span_id, str) or not self.span_id:
      raise ValueError(
          "span_id must be a non-empty real native span id; rule labels"
          " never invent span identifiers"
      )
    if (
        isinstance(self.confidence, bool)
        or not isinstance(self.confidence, (int, float))
        or not 0.0 < self.confidence <= 1.0
    ):
      raise ValueError(
          f"confidence must be a number in (0, 1], got {self.confidence!r}"
      )
    if not isinstance(self.rule_id, str) or not self.rule_id:
      raise ValueError("rule_id must be a non-empty string")

  def as_tuple(
      self,
  ) -> tuple[Optional[str], str, str, str, float]:
    """The RFC tuple: (trace_id, span_id, failure_category, evidence, confidence)."""
    return (
        self.trace_id,
        self.span_id,
        self.failure_category,
        self.evidence,
        self.confidence,
    )


@dataclasses.dataclass(frozen=True)
class ToolProfile:
  """How one agent's tools map onto the roles the rules reason about.

  Attributes:
    compute_tools: tools that run the agent's own code or queries and
      return their output (for example ``run_python`` or ``execute_sql``).
    preview_tools: tools that return an excerpt of a data source.
    code_arg: the argument of a compute tool that holds the code.
    path_arg: the argument of a preview tool that names the source.
    purpose_arg: an argument in which the agent states why it makes the
      call, if the agent's tools have one. Excluded when comparing calls
      for exact repeats, and used to find near-duplicate calls.
    tool_error_pattern: regex; a tool result it matches (``re.search``)
      means the tool itself failed, like a TOOL_ERROR row.
    failed_call_pattern: regex; a compute result it matches means the
      agent's code ran but failed (for example a nonzero exit code).
    output_filter: maps a compute result to the part the agent's code
      printed (for example dropping an exit-code header or stderr).
  """

  compute_tools: frozenset[str] = frozenset()
  preview_tools: frozenset[str] = frozenset()
  code_arg: str = "code"
  path_arg: str = "path"
  purpose_arg: Optional[str] = None
  tool_error_pattern: Optional[str] = None
  failed_call_pattern: Optional[str] = None
  output_filter: Optional[Callable[[str], str]] = None

  def __post_init__(self) -> None:
    for name in ("compute_tools", "preview_tools"):
      value = getattr(self, name)
      if isinstance(value, str) or not isinstance(value, Iterable):
        raise TypeError(f"{name} must be a collection of tool names")
      object.__setattr__(self, name, frozenset(value))
    for name in ("tool_error_pattern", "failed_call_pattern"):
      pattern = getattr(self, name)
      if pattern is not None:
        re.compile(pattern)
    if self.output_filter is not None and not callable(self.output_filter):
      raise TypeError("output_filter must be callable")


@dataclasses.dataclass(frozen=True)
class ToolStep:
  """One tool call: a TOOL_STARTING row and its TOOL_COMPLETED/TOOL_ERROR.

  ``status`` is ``"OK"``, ``"ERROR"`` (a TOOL_ERROR row or an ERROR
  status), or ``"MISSING"`` when no end row was logged. ``args`` excludes
  ``ToolProfile.purpose_arg``, which is in ``purpose``.
  """

  index: int
  tool: Optional[str]
  args: Mapping[str, Any]
  purpose: Optional[str]
  result: str
  status: str
  error_message: Optional[str]
  span_id: Optional[str]
  trace_id: Optional[str]
  is_truncated: bool


@dataclasses.dataclass(frozen=True)
class SessionFacts:
  """What the rules know about one failed session, computed once.

  Built-in rules and ``checks`` callables read the same object. ``steps``
  are 1-indexed in timestamp order. ``final_response`` is
  ``Trace.final_response``. ``failed_steps``, ``repeats`` and
  ``near_duplicates`` hold step indexes; ``wasted`` is their union.
  """

  session_id: str
  rows: tuple[Mapping[str, Any], ...]
  spans: tuple[Span, ...]
  steps: tuple[ToolStep, ...]
  final_response: Optional[str]
  completed: bool
  call_limit_hit: bool
  abort_row: Optional[int]
  reference_answer: Any
  profile: ToolProfile
  failed_steps: tuple[int, ...]
  repeats: tuple[int, ...]
  near_duplicates: tuple[int, ...]
  wasted: tuple[int, ...]
  top_duplicate: Optional[tuple[str, int]]

  @property
  def waste_ratio(self) -> float:
    return len(self.wasted) / len(self.steps) if self.steps else 0.0

  @property
  def compute_steps(self) -> tuple[ToolStep, ...]:
    return tuple(s for s in self.steps if s.tool in self.profile.compute_tools)

  def step(self, index: Optional[int]) -> Optional[ToolStep]:
    """The step with this 1-based index, or None."""
    if index is None or not 1 <= index <= len(self.steps):
      return None
    return self.steps[index - 1]

  def label(
      self,
      category: str,
      rule_id: str,
      evidence: str,
      confidence: float,
      *,
      step: Optional[ToolStep] = None,
      target_kind: str = TARGET_SPAN,
      primary: bool = True,
  ) -> RuleFailureLabel:
    """A label anchored to ``step``'s span.

    Without a step, the label marks the gap after the session's last row.

    Raises:
      ValueError: the anchor row has no ``span_id``.
    """
    if step is not None:
      span_id, trace_id = step.span_id, step.trace_id
    else:
      last = self.rows[-1]
      span_id = _usable_text(last.get("span_id"))
      trace_id = _usable_text(last.get("trace_id"))
      target_kind = TARGET_GAP_AFTER_SPAN
    if not span_id:
      raise ValueError(
          f"the anchor row of session {self.session_id!r} has no span_id;"
          " refusing to invent a synthetic span identifier"
      )
    return RuleFailureLabel(
        session_id=self.session_id,
        trace_id=trace_id,
        span_id=span_id,
        failure_category=category,
        evidence=evidence,
        confidence=confidence,
        target_kind=target_kind,
        rule_id=rule_id,
        primary=primary,
    )


RuleCheck = Callable[
    [SessionFacts], Union[None, RuleFailureLabel, Iterable[RuleFailureLabel]]
]


def label_failed_session_rules(
    session_events: Sequence[Mapping[str, Any]],
    *,
    profile: Optional[ToolProfile] = None,
    reference_answer: Any = None,
    checks: Sequence[RuleCheck] = (),
) -> tuple[RuleFailureLabel, ...]:
  """Run the failure rules over the rows of one failed session.

  ``session_events`` are the raw native ``agent_events``-shaped rows of
  exactly one session, the same rows ``label_failed_session_spans``
  reads. The caller decides that the session failed (for example with
  ``evalbench.classify_sessions``); the rules only say how.

  ``reference_answer`` is the expected answer, if the session is scored
  against one (a number, a string, a list, or their JSON text). Without
  it, ``FIN-IN-ANSWER`` and ``FIN-SEEN-NOT-REPORTED`` do not run. With
  it, the session is assumed to have been scored wrong.

  Each built-in category yields at most one label, in the precedence order
  tool blockers, finalization, turn-waste, incomplete evidence. Labels
  from ``checks`` follow, in the order given. Use ``primary_label`` to
  pick the session's diagnosis.

  Raises:
    ValueError: no rows, rows of several sessions, or a label's anchor row
      has no ``span_id``.
  """
  facts = collect_session_facts(
      session_events, profile=profile, reference_answer=reference_answer
  )
  labels: list[RuleFailureLabel] = []
  for rule in (
      _rule_tool_blockers,
      _rule_finalization,
      _rule_turn_waste,
      _rule_incomplete_evidence,
  ):
    label = rule(facts)
    if label is not None:
      labels.append(label)
  for check in checks:
    result = check(facts)
    if result is None:
      continue
    if isinstance(result, RuleFailureLabel):
      labels.append(result)
      continue
    for label in result:
      if not isinstance(label, RuleFailureLabel):
        raise TypeError(
            f"check {check!r} returned {type(label).__name__}, not"
            " RuleFailureLabel"
        )
      labels.append(label)
  return tuple(labels)


def primary_label(
    labels: Iterable[RuleFailureLabel],
) -> Optional[RuleFailureLabel]:
  """The first label that may stand as the diagnosis, or None."""
  return next((label for label in labels if label.primary), None)


def collect_session_facts(
    session_events: Sequence[Mapping[str, Any]],
    *,
    profile: Optional[ToolProfile] = None,
    reference_answer: Any = None,
) -> SessionFacts:
  """Pair tool calls and derive the facts the rules read.

  Raises:
    ValueError: no rows, or rows of several sessions.
  """
  profile = profile or ToolProfile()
  rows = _drop_repeated_events(_ordered_single_session(session_events))
  if not rows:
    raise ValueError("cannot diagnose a session with no agent_events rows")
  session_id = next(
      (sid for r in rows if (sid := _usable_text(r.get("session_id")))),
      None,
  )
  if not session_id:
    raise ValueError("agent_events rows carry no session_id")
  spans = tuple(Span.from_bigquery_row(dict(row)) for row in rows)
  trace = Trace(
      trace_id=_usable_text(rows[0].get("trace_id")) or session_id,
      session_id=session_id,
      spans=list(spans),
  )
  final = trace.final_response
  final_response = None if final is None else str(final)
  steps = _pair_tool_calls(spans, profile)

  completed = any(s.event_type == "AGENT_COMPLETED" for s in spans)
  call_limit_hit = any(_CALL_LIMIT_RE.search(_message(s)) for s in spans)
  abort_row = None
  if not completed and not call_limit_hit:
    errors = [
        i
        for i, s in enumerate(spans)
        if s.status == "ERROR" or str(s.event_type or "").endswith("_ERROR")
    ]
    # A tool error the agent may have recovered from is not what stopped
    # the run; prefer the first error outside the tool rows.
    non_tool = [i for i in errors if spans[i].event_type not in _TOOL_EVENTS]
    if non_tool:
      abort_row = non_tool[0]
    elif errors:
      abort_row = errors[-1]

  failed = sorted(
      s.index
      for s in steps
      if (s.tool in profile.compute_tools and _call_failed(s, profile))
      or _tool_failed(s, profile)
  )
  repeats = []
  seen_calls: set[tuple[Optional[str], str]] = set()
  for step in steps:
    key = (
        step.tool,
        json.dumps(step.args, sort_keys=True, ensure_ascii=False, default=str),
    )
    if key in seen_calls:
      repeats.append(step.index)
    seen_calls.add(key)

  near_duplicates = []
  earlier: list[tuple[str, set[str]]] = []
  counts: Counter[str] = Counter()
  for step in steps:
    norm = _norm_words(step.purpose)
    if not norm:
      continue
    words = set(norm.split())
    counts[norm] += 1
    for prev_norm, prev_words in earlier:
      if norm == prev_norm or (
          len(words) >= NEAR_DUPLICATE_MIN_WORDS
          and len(prev_words) >= NEAR_DUPLICATE_MIN_WORDS
          and _jaccard(words, prev_words) >= NEAR_DUPLICATE_JACCARD
      ):
        near_duplicates.append(step.index)
        break
    earlier.append((norm, words))
  top_duplicate = None
  if counts:
    norm, count = counts.most_common(1)[0]
    if count >= 2:
      original = next(
          s.purpose for s in steps if _norm_words(s.purpose) == norm
      )
      top_duplicate = (original, count)

  return SessionFacts(
      session_id=session_id,
      rows=rows,
      spans=spans,
      steps=steps,
      final_response=final_response,
      completed=completed,
      call_limit_hit=call_limit_hit,
      abort_row=abort_row,
      reference_answer=reference_answer,
      profile=profile,
      failed_steps=tuple(failed),
      repeats=tuple(repeats),
      near_duplicates=tuple(near_duplicates),
      wasted=tuple(sorted(set(failed) | set(repeats) | set(near_duplicates))),
      top_duplicate=top_duplicate,
  )


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------


def _rule_tool_blockers(facts: SessionFacts) -> Optional[RuleFailureLabel]:
  steps = facts.steps
  if facts.abort_row is not None:
    row = facts.spans[facts.abort_row]
    summary = _summarize_error(row.error_message) or "no error message logged"
    span_id = _usable_text(row.span_id)
    if not span_id:
      raise ValueError(
          f"the {row.event_type} row of session {facts.session_id!r} has no"
          " span_id; refusing to invent a synthetic span identifier"
      )
    return RuleFailureLabel(
        session_id=facts.session_id,
        trace_id=_usable_text(row.trace_id),
        span_id=span_id,
        failure_category=TOOL_BLOCKERS,
        evidence=(
            f"{row.event_type} aborted the run after {len(steps)} tool"
            f" call(s): {summary}; no AGENT_COMPLETED event follows"
        ),
        confidence=0.95,
        target_kind=TARGET_SPAN,
        rule_id="TB-ABORT",
    )
  profile = facts.profile
  if not steps and (profile.compute_tools or profile.preview_tools):
    return facts.label(
        TOOL_BLOCKERS,
        "TB-NO-TOOLS",
        "the session ended without a single tool call",
        0.9,
    )
  if steps and facts.completed:
    final = steps[-1]
    if final.status == "MISSING" or _tool_failed(final, profile):
      detail = (
          "no TOOL_COMPLETED was logged"
          if final.status == "MISSING"
          else _short(final.result or final.error_message or final.status, 70)
      )
      return facts.label(
          TOOL_BLOCKERS,
          "TB-UNRECOVERED",
          f"the final tool call (step {final.index}, {final.tool}) failed at"
          f" the tool level ({detail}) and the run ended without a"
          " successful retry",
          0.8,
          step=final,
      )
  return None


def _rule_finalization(facts: SessionFacts) -> Optional[RuleFailureLabel]:
  last = facts.steps[-1] if facts.steps else None
  answer = facts.final_response
  reference = _parse_reference(facts.reference_answer)
  if answer is not None and reference is not None:
    how = _answer_contains_reference(answer, reference)
    if how:
      return facts.label(
          FINALIZATION,
          "FIN-IN-ANSWER",
          f"the final response contains the reference value"
          f" {_short(reference.raw, 90)} but was scored wrong: {how}",
          0.9,
          step=last,
          target_kind=TARGET_GAP_AFTER_SPAN,
      )
  if reference is not None:
    for step in facts.compute_steps:
      if _reference_seen_in(_output(step, facts.profile), reference):
        outcome = (
            "the run ended without a final response"
            if answer is None
            else f"the final response was {_short(answer, 50)!r}"
        )
        return facts.label(
            FINALIZATION,
            "FIN-SEEN-NOT-REPORTED",
            f"{step.tool} output at step {step.index} already showed the"
            f" reference value {_short(reference.raw, 90)}, but {outcome}",
            0.75,
            step=step,
            target_kind=TARGET_GAP_AFTER_SPAN,
        )
  if answer is None and facts.completed:
    return facts.label(
        FINALIZATION,
        "FIN-NO-ANSWER",
        f"the run completed after {len(facts.steps)} tool call(s) without a"
        " final response",
        0.85,
        step=last,
        target_kind=TARGET_GAP_AFTER_SPAN,
    )
  return None


def _waste_summary(facts: SessionFacts) -> str:
  example = ""
  if facts.top_duplicate:
    purpose, count = facts.top_duplicate
    example = f", e.g. {_short(purpose, 60)!r} x{count}"
  return (
      f"{len(facts.wasted)} of {len(facts.steps)} calls were wasted"
      f" ({len(facts.failed_steps)} failed, {len(facts.repeats)} exact"
      f" repeats, {len(facts.near_duplicates)} near-duplicate"
      f" purposes{example})"
  )


def _rule_turn_waste(facts: SessionFacts) -> Optional[RuleFailureLabel]:
  if facts.call_limit_hit:
    message = next(
        (
            s.error_message
            for s in facts.spans
            if _CALL_LIMIT_RE.search(_message(s))
        ),
        "",
    )
    size = _CALL_LIMIT_SIZE_RE.search(message)
    cap = f"{size.group(1)}-call" if size else "LLM call"
    confidence = 0.85 if facts.waste_ratio >= CALL_CAP_WASTE_RATIO else 0.65
    if facts.wasted:
      step, target = facts.step(facts.wasted[0]), TARGET_SPAN
    else:
      step = facts.steps[-1] if facts.steps else None
      target = TARGET_GAP_AFTER_SPAN
    return facts.label(
        TURN_WASTE,
        "TW-CALL-CAP",
        f"hit the {cap} limit after {len(facts.steps)} tool calls without a"
        f" final response; {_waste_summary(facts)}",
        confidence,
        step=step,
        target_kind=target,
    )
  if (
      facts.completed
      and facts.waste_ratio >= THRASH_MIN_RATIO
      and len(facts.wasted) >= THRASH_MIN_WASTED
  ):
    return facts.label(
        TURN_WASTE,
        "TW-THRASH",
        _waste_summary(facts),
        0.6,
        step=facts.step(facts.wasted[0]),
        primary=False,
    )
  return None


def _rule_incomplete_evidence(
    facts: SessionFacts,
) -> Optional[RuleFailureLabel]:
  profile = facts.profile
  if facts.final_response is None or not profile.compute_tools:
    return None
  compute = facts.compute_steps
  if not compute:
    return facts.label(
        INCOMPLETE_EVIDENCE,
        "IE-PREVIEW-ONLY",
        f"answered after {len(facts.steps)} tool call(s) without any"
        f" computation ({', '.join(sorted(profile.compute_tools))}): the"
        " answer rests on listing and preview output only",
        0.7,
        step=facts.steps[-1] if facts.steps else None,
        target_kind=TARGET_GAP_AFTER_SPAN,
    )
  target = next(
      (
          s
          for s in reversed(compute)
          if s.status == "OK" and not _call_failed(s, profile)
      ),
      None,
  )
  if target is None or not profile.preview_tools:
    return None
  code = target.args.get(profile.code_arg)
  code = code if isinstance(code, str) else ""
  code_lines = {
      line.strip()
      for line in code.splitlines()
      if len(line.strip()) >= PASTED_MIN_LINE_CHARS
  }
  for step in facts.steps[: target.index - 1]:
    if step.tool not in profile.preview_tools or _tool_failed(step, profile):
      continue
    path = step.args.get(profile.path_arg)
    if not isinstance(path, str) or not path:
      continue
    preview_lines = [line for line in step.result.splitlines() if line.strip()]
    copied = code_lines & {
        line.strip()
        for line in preview_lines
        if len(line.strip()) >= PASTED_MIN_LINE_CHARS
    }
    name = re.split(r"[\\/]", path)[-1]
    if (
        len(copied) >= PASTED_MIN_LINES
        and name.casefold() not in code.casefold()
    ):
      return facts.label(
          INCOMPLETE_EVIDENCE,
          "IE-PASTED-PREVIEW",
          f"step {target.index} computed the answer from {len(copied)} lines"
          f" pasted from the step-{step.index} preview of {name} (a"
          f" {len(preview_lines)}-line excerpt) and never read the source"
          " itself",
          0.85,
          step=target,
      )
  return None


# ---------------------------------------------------------------------------
# Tool calls
# ---------------------------------------------------------------------------


def _pair_tool_calls(
    spans: Sequence[Span], profile: ToolProfile
) -> tuple[ToolStep, ...]:
  """Pair TOOL_STARTING rows with their end rows by ``span_id``."""
  pending: list[dict[str, Any]] = []
  by_span: dict[str, dict[str, Any]] = {}
  for span in spans:
    content = span.content if isinstance(span.content, Mapping) else {}
    if span.event_type == "TOOL_STARTING":
      args = content.get("args")
      args = dict(args) if isinstance(args, Mapping) else {}
      purpose = None
      if profile.purpose_arg is not None and profile.purpose_arg in args:
        value = args.pop(profile.purpose_arg)
        purpose = value if isinstance(value, str) else None
      step = {
          "tool": _usable_text(content.get("tool")),
          "args": args,
          "purpose": purpose,
          "result": "",
          "status": "MISSING",
          "error_message": None,
          "span_id": _usable_text(span.span_id),
          "trace_id": _usable_text(span.trace_id),
          "is_truncated": False,
      }
      if step["span_id"] in by_span:
        continue  # a second start for a call that is still open
      pending.append(step)
      if step["span_id"]:
        by_span[step["span_id"]] = step
    elif span.event_type in _TOOL_END_EVENTS:
      step = by_span.pop(_usable_text(span.span_id) or "", None)
      if step is None:
        continue
      result = content.get("result")
      if result is None and span.event_type == "TOOL_ERROR":
        result = span.error_message
      step["result"] = _text(result)
      step["status"] = (
          "ERROR"
          if span.event_type == "TOOL_ERROR" or span.status == "ERROR"
          else "OK"
      )
      step["error_message"] = span.error_message
      step["is_truncated"] = span.is_truncated
  return tuple(
      ToolStep(index=i, **step) for i, step in enumerate(pending, start=1)
  )


def _drop_repeated_events(
    rows: Sequence[Mapping[str, Any]],
) -> tuple[Mapping[str, Any], ...]:
  """Keeps the first row of each ``event_id``.

  The plugin writes at least once, and a retried write keeps its
  ``event_id``, so a repeated id is the same event stored twice. Rows
  without an ``event_id`` are all kept.
  """
  seen: set[str] = set()
  kept = []
  for row in rows:
    event_id = _usable_text(row.get("event_id"))
    if event_id is not None:
      if event_id in seen:
        continue
      seen.add(event_id)
    kept.append(row)
  return tuple(kept)


def _message(span: Span) -> str:
  return "" if span.error_message is None else str(span.error_message)


def _text(value: Any) -> str:
  if value is None:
    return ""
  if isinstance(value, str):
    return value
  return json.dumps(value, ensure_ascii=False, default=str)


def _tool_failed(step: ToolStep, profile: ToolProfile) -> bool:
  """The tool itself failed (a TOOL_ERROR row, or its error pattern)."""
  if step.status == "ERROR":
    return True
  pattern = profile.tool_error_pattern
  return pattern is not None and re.search(pattern, step.result) is not None


def _call_failed(step: ToolStep, profile: ToolProfile) -> bool:
  """A compute call that did not run to completion."""
  if _tool_failed(step, profile):
    return True
  pattern = profile.failed_call_pattern
  return pattern is not None and re.search(pattern, step.result) is not None


def _output(step: ToolStep, profile: ToolProfile) -> str:
  if profile.output_filter is None:
    return step.result
  return profile.output_filter(step.result)


def _summarize_error(text: Optional[str]) -> Optional[str]:
  """A one-line, URL-free summary of an error message."""
  if not text:
    return None
  if "RESOURCE_EXHAUSTED" in text or re.search(r"\b429\b", text):
    return "429 RESOURCE_EXHAUSTED (model API rate limit)"
  first = next((line for line in text.splitlines() if line.strip()), text)
  return _short(_URL_RE.sub("<url>", first), 160)


def _short(text: Any, limit: int = 80) -> str:
  value = " ".join(str(text).split())
  return value if len(value) <= limit else value[: limit - 3] + "..."


def _norm_words(text: Optional[str]) -> str:
  return " ".join(_WORD_RE.findall((text or "").casefold()))


def _jaccard(a: set[str], b: set[str]) -> float:
  return len(a & b) / len(a | b) if a and b else 0.0


# ---------------------------------------------------------------------------
# Reference answers
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class _Reference:
  kind: str  # "number" | "list" | "string"
  value: Any
  decimals: int = 0
  raw: str = ""


def _parse_reference(value: Any) -> Optional[_Reference]:
  if value is None:
    return None
  if isinstance(value, str):
    text = value.strip()
  else:
    text = json.dumps(value, ensure_ascii=False, default=str)
  if not text:
    return None
  try:
    decoded = json.loads(text)
  except ValueError:
    decoded = text
  if isinstance(decoded, bool):
    return _Reference("string", str(decoded).lower(), raw=text)
  if isinstance(decoded, (int, float)):
    return _Reference("number", float(decoded), _decimals(text), raw=text)
  if isinstance(decoded, list):
    return _Reference("list", tuple(decoded), raw=text)
  if isinstance(decoded, str):
    number = _as_number(decoded)
    if number is not None:
      return _Reference("number", number[0], number[1], raw=decoded.strip())
    return _Reference("string", decoded, raw=decoded)
  return _Reference("string", str(decoded), raw=text)


def _decimals(text: str) -> int:
  """Decimal places ``text`` is written to; ``4.6e-13`` has 14."""
  mantissa, _, exponent = text.lower().partition("e")
  places = len(mantissa.split(".", 1)[1]) if "." in mantissa else 0
  try:
    places -= int(exponent) if exponent else 0
  except ValueError:
    pass
  return max(places, 0)


def _as_number(text: Any) -> Optional[tuple[float, int]]:
  """A bare number (quotes, commas, %, spaces allowed) as (value, decimals)."""
  if isinstance(text, bool) or text is None:
    return None
  if isinstance(text, int):
    return float(text), 0
  if isinstance(text, float):
    return text, _decimals(repr(text))
  candidate = (
      str(text).strip().strip("'\"`").strip().rstrip("%").replace(",", "")
  ).strip()
  if not _BARE_NUMBER_RE.fullmatch(candidate):
    return None
  try:
    return float(candidate), _decimals(candidate)
  except ValueError:
    return None


def _numbers_in(text: str) -> list[tuple[float, int]]:
  out = []
  for match in _NUMBER_RE.finditer(text):
    token = match.group(0).replace(",", "")
    try:
      out.append((float(token), _decimals(token)))
    except ValueError:
      continue
  return out


def _close_at(value: float, target: float, decimals: int) -> bool:
  """True when ``value`` rounds to ``target`` at ``decimals`` places."""
  return abs(value - target) <= 0.5 * 10 ** (-decimals) * (1 + 1e-9)


def _significant_digits(raw: str) -> int:
  digits = re.sub(r"[^0-9]", "", re.split(r"[eE]", raw)[0])
  return len(digits.lstrip("0"))


def _tokens(text: Any) -> list[str]:
  return _WORD_RE.findall(str(text).casefold())


def _scalar_equal(candidate: Any, reference: Any) -> bool:
  reference_number = _as_number(reference)
  candidate_number = _as_number(candidate)
  if reference_number is not None and candidate_number is not None:
    return _close_at(
        candidate_number[0], reference_number[0], reference_number[1]
    )
  return _tokens(candidate) == _tokens(reference) and bool(_tokens(reference))


def _lists_match(candidate: Sequence[Any], reference: Sequence[Any]) -> bool:
  """Multiset equality, numbers compared at each reference item's precision."""
  if not reference or len(candidate) != len(reference):
    return False
  remaining = list(candidate)
  for item in reference:
    for position, other in enumerate(remaining):
      if _scalar_equal(other, item):
        del remaining[position]
        break
    else:
      return False
  return True


def _list_literals(text: str) -> Iterable[tuple[Any, ...]]:
  for match in _LITERAL_RE.finditer(text):
    try:
      value = ast.literal_eval(match.group(0))
    except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
      continue
    if (
        isinstance(value, (list, tuple))
        and value
        and all(
            isinstance(v, (str, int, float)) and not isinstance(v, bool)
            for v in value
        )
    ):
      yield tuple(value)


def _answer_contains_reference(
    answer: str, reference: _Reference
) -> Optional[str]:
  """How a scored-wrong answer still contains the reference value."""
  if reference.kind == "list":
    for literal in _list_literals(answer):
      if _lists_match(literal, reference.value):
        prose = answer.strip()
        if prose.startswith(("[", "(")):
          return (
              "the answer is the reference list but not in the expected format"
          )
        return (
            f"the reference list is embedded in prose ({_short(prose, 50)!r})"
        )
    return None
  if reference.kind == "number":
    bare = _as_number(answer)
    if bare is not None:
      if _close_at(bare[0], reference.value, reference.decimals):
        return (
            "the answer equals the reference at its"
            f" {reference.decimals}-decimal precision but was not rounded"
            " or formatted as requested"
        )
      return None
    numbers = _numbers_in(answer)
    if numbers and _close_at(
        numbers[-1][0], reference.value, reference.decimals
    ):
      return (
          "the answer states the reference value inside prose"
          f" ({_short(answer, 50)!r})"
      )
    return None
  reference_tokens = _tokens(reference.value)
  if not reference_tokens:
    return None
  answer_tokens = _tokens(answer)
  if answer_tokens == reference_tokens:
    return (
        "the answer differs from the reference only in case, quoting or"
        " punctuation"
    )
  if any(_tokens(line) == reference_tokens for line in answer.splitlines()):
    return (
        "one line of the answer is the reference value, surrounded by extra"
        " text"
    )
  if (
      len(answer_tokens) > len(reference_tokens)
      and answer_tokens[-len(reference_tokens) :] == reference_tokens
  ):
    return (
        "the answer ends with the reference value but wraps it in a sentence"
        f" ({_short(answer, 50)!r})"
    )
  return None


def _reported_token(line: str) -> Optional[str]:
  """The token ``line`` reports as its result, or None.

  That is the line's only token, or its last one when it directly follows
  ``:``, ``=`` or the word ``is`` (``x: 2002``, ``= 4.6``, ``mean is 3.``).
  Linear in the line length: printed lines can be very long.
  """
  text = line.strip()
  if text.endswith(".") and text[:-1].rstrip():
    text = text[:-1].rstrip()
  parts = text.rsplit(None, 1)
  if not parts:
    return None
  token = parts[-1]
  head = parts[0] if len(parts) == 2 else ""
  if not head or head.endswith((":", "=")) or _IS_TAIL_RE.search(head):
    return token
  mark = _MARK_RE.search(token)
  if mark and mark.end() < len(token):
    return token[mark.end() :]
  return None


def _reports_value(line: str, reference: _Reference) -> bool:
  """Does ``line`` state the value as a result (``x: 2002``, ``= 4.6``)?"""
  token = _reported_token(line)
  if token is None:
    return False
  number = _as_number(token)
  return number is not None and _close_at(
      number[0], reference.value, reference.decimals
  )


def _reference_seen_in(text: str, reference: _Reference) -> bool:
  """Is a *distinctive* rendering of the reference value in ``text``?"""
  if reference.kind == "list":
    return any(
        _lists_match(literal, reference.value)
        for literal in _list_literals(text)
    )
  if reference.kind == "number":
    if _significant_digits(reference.raw) < SEEN_MIN_SIGNIFICANT_DIGITS:
      return False
    numbers = _numbers_in(text)
    if not any(
        _close_at(value, reference.value, reference.decimals)
        for value, _ in numbers
    ):
      return False
    year_like = (
        reference.decimals == 0
        and YEAR_RANGE[0] <= reference.value <= YEAR_RANGE[1]
    )
    similar = sum(
        1
        for value, _ in numbers
        if reference.value and 0.5 <= value / reference.value <= 2
    )
    if year_like or similar >= DENSE_SERIES_MIN:
      # A year is also a row label in any table over the period, and a long
      # series of similar values hits the reference by chance: count those
      # only where a line reports the value as a result.
      return any(_reports_value(line, reference) for line in text.splitlines())
    return True
  reference_tokens = _tokens(reference.value)
  if not reference_tokens or " ".join(reference_tokens) in _STOPWORD_ANSWERS:
    return False
  pattern = re.escape(" ".join(reference_tokens))
  answer_line = re.compile(rf"(?:[:=]|\bis)\s*{pattern}\s*\Z")
  for line in text.splitlines():
    if _tokens(line) == reference_tokens:
      return True
    if answer_line.search(" ".join(re.findall(r"\w+|[:=]", line.casefold()))):
      return True
  return False
