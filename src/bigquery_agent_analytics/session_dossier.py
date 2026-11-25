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

"""A plain-text dossier of one failed session, for a blind judge (#435).

A dossier is what a human labeler or an LLM judge reads to decide why a
session failed: the task, the expected answer when there is one, how the
run ended, every tool call in order with its arguments and result, and
the final response. It holds no rule output and no existing label, so a
judge that reads it stays blind to both; ``build_session_dossier`` takes
no label argument.

Steps are numbered exactly like ``rule_taxonomy`` numbers them
(``ToolStep.index``), so "step 4" means the same call in a rule's
evidence, in a dossier and in a judge's answer. ``SessionDossier.steps``
maps each step a judge may cite to its real native ``span_id``. A step
whose rows carry no ``span_id`` is shown but cannot be cited.

Everything taken from the session is untrusted: the agent wrote the code
and the task text, and tools return arbitrary data. The dossier therefore

* takes the citable step numbers from ``SessionFacts``, never from the
  text, so a tool output that prints ``### Step 99:`` adds no step 99;
* fences code and results with more backticks than they contain;
* breaks up the ``trace_data`` tag name wherever untrusted text holds it,
  so the text cannot close the tag a judge prompt wraps the dossier in;
* puts the agent's ``purpose`` and the tool name on one line;
* cuts long values, marks every cut, and breaks up anything in untrusted
  text that looks like a cut marker;
* over a total budget, drops the bodies of middle steps first. The task,
  expected answer, context, outcome and final response are cut only by
  ``max_result_chars``, so they can keep a dossier over budget.

Offline and deterministic: the same session gives the same text.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import dataclasses
import hashlib
import json
import re
from typing import Any, Optional

from .evalbench import _usable_text
from .rule_taxonomy import _CALL_LIMIT_RE
from .rule_taxonomy import collect_session_facts
from .rule_taxonomy import SessionFacts
from .rule_taxonomy import ToolProfile
from .rule_taxonomy import ToolStep

DOSSIER_FORMAT_VERSION = "1"
DOSSIER_TAG = "trace_data"

# Steps kept in full, from the start and from the end, when the dossier is
# over budget. Bodies of the steps in between are dropped first; each
# fallback keeps fewer.
_KEEP_STEPS = ((10, 20), (3, 10), (1, 3), (0, 1), (0, 0))

_TAG_RE = re.compile(re.escape(DOSSIER_TAG), re.IGNORECASE)
_BACKTICKS_RE = re.compile(r"`+")
_ZERO_WIDTH_SPACE = "​"


@dataclasses.dataclass(frozen=True)
class DossierStep:
  """A step a judge may cite.

  ``index`` is ``ToolStep.index``. ``start`` and ``end`` are the step's
  character range in ``SessionDossier.text``. ``in_run`` is False for a
  step of an earlier invocation (see ``SessionFacts``).
  """

  index: int
  span_id: str
  trace_id: Optional[str]
  start: int
  end: int
  in_run: bool


@dataclasses.dataclass(frozen=True)
class SessionDossier:
  """The dossier text and how its parts map back to the session.

  Attributes:
    session_id: the session the dossier describes.
    text: the dossier.
    steps: the citable steps by number. The only valid step numbers.
    gap_anchor: ``(span_id, trace_id)`` of the run's last row, the span a
      ``gap_after_span`` label anchors to (as ``SessionFacts.label`` does
      without a step); None when that row has no ``span_id``.
    sections: character ranges of the parts that are not steps: ``task``,
      ``reference``, ``context``, ``outcome`` and ``final_response``, when
      present.
    cut_markers: every cut marker the text holds, in order.
    digest: a short hash of ``text``, usable as a cache key.
  """

  session_id: str
  text: str
  steps: Mapping[int, DossierStep]
  gap_anchor: Optional[tuple[str, Optional[str]]]
  sections: Mapping[str, tuple[int, int]]
  cut_markers: tuple[str, ...]
  digest: str

  def locate(self, start: int) -> Optional[str]:
    """Where a character offset of ``text`` falls: ``"step:N"`` or a section."""
    for index, step in self.steps.items():
      if step.start <= start < step.end:
        return f"step:{index}"
    for name, (begin, end) in self.sections.items():
      if begin <= start < end:
        return name
    return None


def dossier_from_events(
    session_events: Sequence[Mapping[str, Any]],
    *,
    profile: Optional[ToolProfile] = None,
    reference_answer: Any = None,
    task: Optional[str] = None,
    context: Optional[Mapping[str, str]] = None,
    max_result_chars: int = 8000,
    max_arg_chars: int = 4000,
    max_total_chars: int = 200_000,
) -> SessionDossier:
  """The dossier of one session's raw ``agent_events`` rows.

  Takes the rows ``label_failed_session_rules`` takes; see
  ``build_session_dossier`` for the other arguments.

  Raises:
    ValueError: no rows, or rows of several sessions.
  """
  facts = collect_session_facts(
      session_events, profile=profile, reference_answer=reference_answer
  )
  return build_session_dossier(
      facts,
      task=task,
      context=context,
      max_result_chars=max_result_chars,
      max_arg_chars=max_arg_chars,
      max_total_chars=max_total_chars,
  )


def build_session_dossier(
    facts: SessionFacts,
    *,
    task: Optional[str] = None,
    context: Optional[Mapping[str, str]] = None,
    max_result_chars: int = 8000,
    max_arg_chars: int = 4000,
    max_total_chars: int = 200_000,
) -> SessionDossier:
  """Render one session's facts as a dossier.

  Args:
    facts: from ``rule_taxonomy.collect_session_facts``. Its
      ``reference_answer``, when set, is shown as the expected answer, and
      its ``profile`` names the code and purpose arguments.
    task: the task text. Defaults to the first user message of the run.
    context: extra sections, title to text, for what only the caller
      knows (data sources, sub-answers, notes on a known-bad reference).
      Shown in the order given.
    max_result_chars: longest tool result or error message shown in full.
    max_arg_chars: longest code or argument block shown in full.
    max_total_chars: budget for the whole dossier. Over it, the bodies of
      middle steps are dropped, then more, until it fits or only headers
      remain.

  Raises:
    ValueError: a limit is not a positive integer.
  """
  for name, value in (
      ("max_result_chars", max_result_chars),
      ("max_arg_chars", max_arg_chars),
      ("max_total_chars", max_total_chars),
  ):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
      raise ValueError(f"{name} must be a positive integer, got {value!r}")
  head = _head(facts, task, context, max_result_chars)
  final = _final_section(facts, max_result_chars)
  bodies = [
      _step_body(s, facts, max_result_chars, max_arg_chars) for s in facts.steps
  ]
  headers = [_step_header(s, facts) for s in facts.steps]
  fixed = sum(len(part) for _, part in head) + len(final[1])
  fixed += sum(len(h) for h in headers) + len("\n## Steps (in order)\n")
  if not facts.steps:
    fixed += len(_NO_STEPS)

  keep = None  # None: every body is kept.
  if fixed + sum(len(b) for b in bodies) > max_total_chars:
    for first, last in _KEEP_STEPS:
      kept = _kept(len(bodies), first, last)
      size = sum(
          len(b if i in kept else _shrunk(b)) for i, b in enumerate(bodies)
      )
      keep = kept
      if fixed + size <= max_total_chars:
        break

  builder = _Builder()
  for name, part in head:
    builder.add(part, section=name)
  builder.add("\n## Steps (in order)\n")
  steps: dict[int, DossierStep] = {}
  for i, step in enumerate(facts.steps):
    start = builder.size
    builder.add(headers[i])
    builder.add(bodies[i] if keep is None or i in keep else _shrunk(bodies[i]))
    if step.span_id:
      steps[step.index] = DossierStep(
          index=step.index,
          span_id=step.span_id,
          trace_id=step.trace_id,
          start=start,
          end=builder.size,
          in_run=facts.in_run(step.invocation_id),
      )
  if not facts.steps:
    builder.add(_NO_STEPS)
  builder.add(final[1], section=final[0])
  text = builder.text()
  return SessionDossier(
      session_id=facts.session_id,
      text=text,
      steps=steps,
      gap_anchor=_gap_anchor(facts),
      sections=builder.sections,
      cut_markers=tuple(_CUT_RE.findall(text)),
      digest=hashlib.sha256(text.encode("utf-8")).hexdigest()[:16],
  )


# ---------------------------------------------------------------- pieces

_OMITTED = "[step body omitted: dossier budget]\n"
_NO_STEPS = "\n(no tool calls)\n"
_CUT_RE = re.compile(
    r"\[(?:cut: \d+ of \d+ chars shown|step body omitted: dossier budget)\]"
)
# The start of a cut marker. Untrusted text has its brackets broken up
# here, so only the dossier's own markers match.
MARKER_START_RE = re.compile(r"\[(?=cut:|step body omitted)")


# The dossier's own headings and labels. A quote made only of these holds
# no text from the session.
GENERATED_RE = re.compile(
    "|".join(
        [
            r"#+ ?(?:Session|Task|Expected answer|Context|Outcome"
            r"|Steps \(in order\)|Final response)\b",
            r"### Step \d+: \S*",
            r"\[status (?:OK|ERROR|MISSING)\]",
            r"\((?:earlier invocation|no span id; cannot be cited"
            r"|no task text recorded|no tool calls|no final response"
            r"|no message|empty|unknown tool"
            r"|no end row was logged for this call)\)",
            r"\b(?:purpose|args|code|result|tool error message):",
            r"The run (?:completed|did not complete"
            r"|stopped at the LLM call limit|stopped on an error \(\w+\))[.:]?",
            r"Tool calls: \d+ in the session, \d+ in the last invocation\.",
            r"Steps marked \(earlier invocation\) belong to an earlier turn of"
            r" the session\.",
            r"\[producer marked this row as clipped\]",
            r"`{3,}",
        ]
    )
)


def _shrunk(body: str) -> str:
  return _OMITTED if len(body) > len(_OMITTED) else body


class _Builder:
  """Concatenates parts and records the range of each named section."""

  def __init__(self) -> None:
    self._parts: list[str] = []
    self.size = 0
    self.sections: dict[str, tuple[int, int]] = {}

  def add(self, part: str, *, section: Optional[str] = None) -> None:
    if section is not None:
      self.sections[section] = (self.size, self.size + len(part))
    self._parts.append(part)
    self.size += len(part)

  def text(self) -> str:
    return "".join(self._parts)


def _kept(count: int, first: int, last: int) -> set[int]:
  return set(range(min(first, count))) | set(range(max(count - last, 0), count))


def neutralize(text: str) -> str:
  """Untrusted text with the dossier tag name and cut markers broken up."""
  text = _TAG_RE.sub(
      lambda m: m.group(0)[:5] + _ZERO_WIDTH_SPACE + m.group(0)[5:], text
  )
  return MARKER_START_RE.sub("[" + _ZERO_WIDTH_SPACE, text)


def _one_line(text: str) -> str:
  """Untrusted text that must stay on its line (a heading or a label)."""
  return " ".join(neutralize(text).split())


def _cut(text: str, limit: int) -> str:
  if len(text) <= limit:
    return text
  return text[:limit] + f" ...[cut: {limit} of {len(text)} chars shown]"


def _fenced(text: str) -> str:
  longest = max((len(m) for m in _BACKTICKS_RE.findall(text)), default=0)
  fence = "`" * max(3, longest + 1)
  return f"{fence}\n{text.rstrip()}\n{fence}\n"


def _value(value: Any) -> str:
  if value is None:
    return ""
  if isinstance(value, str):
    return value
  return json.dumps(value, ensure_ascii=False, default=str, sort_keys=True)


def _untrusted(value: Any, limit: int) -> str:
  return _cut(neutralize(_value(value)), limit)


def _task_text(facts: SessionFacts) -> Optional[str]:
  """The run's first user message, else the session's."""
  messages = [
      span for span in facts.spans if span.event_type == "USER_MESSAGE_RECEIVED"
  ]
  in_run = [s for s in messages if facts.in_run(_usable_text(s.invocation_id))]
  for span in in_run or messages:
    if isinstance(span.content, str) and span.content.strip():
      return span.content
    content = span.content if isinstance(span.content, Mapping) else {}
    for key in ("text", "text_summary", "raw"):
      value = content.get(key)
      if isinstance(value, str) and value.strip():
        return value
    if content:
      return _value(content)
  return None


def _head(
    facts: SessionFacts,
    task: Optional[str],
    context: Optional[Mapping[str, str]],
    limit: int,
) -> list[tuple[Optional[str], str]]:
  parts: list[tuple[Optional[str], str]] = [
      (None, f"# Session {_one_line(facts.session_id)}\n")
  ]
  task_text = task if task is not None else _task_text(facts)
  parts.append(
      (
          "task",
          "\n## Task\n"
          + (
              _fenced(_untrusted(task_text, limit))
              if task_text
              else "(no task text recorded)\n"
          ),
      )
  )
  if facts.reference_answer is not None:
    parts.append(
        (
            "reference",
            "\n## Expected answer\n"
            + _fenced(_untrusted(facts.reference_answer, limit)),
        )
    )
  if context:
    body = "\n## Context\n"
    for title, value in context.items():
      body += f"\n### {_one_line(_value(title))}\n" + _fenced(
          _untrusted(value, limit)
      )
    parts.append(("context", body))
  parts.append(("outcome", _outcome(facts, limit)))
  return parts


def _outcome(facts: SessionFacts, limit: int) -> str:
  run_steps = facts.run_steps
  lines = ["\n## Outcome\n"]
  if facts.completed:
    lines.append("The run completed.\n")
  elif facts.call_limit_hit:
    lines.append("The run stopped at the LLM call limit:\n")
    message = next(
        (
            span.error_message
            for span in facts.spans
            if facts.in_run(_usable_text(span.invocation_id))
            and _CALL_LIMIT_RE.search(str(span.error_message or ""))
        ),
        None,
    )
    lines.append(_fenced(_untrusted(message or "(no message)", limit)))
  elif facts.abort_row is not None:
    span = facts.spans[facts.abort_row]
    lines.append(f"The run stopped on an error ({span.event_type}):\n")
    lines.append(
        _fenced(_untrusted(span.error_message or "(no message)", limit))
    )
  else:
    lines.append("The run did not complete.\n")
  lines.append(
      f"Tool calls: {len(facts.steps)} in the session, {len(run_steps)} in"
      " the last invocation.\n"
  )
  if len(run_steps) != len(facts.steps):
    lines.append(
        "Steps marked (earlier invocation) belong to an earlier turn of the"
        " session.\n"
    )
  return "".join(lines)


def _step_header(step: ToolStep, facts: SessionFacts) -> str:
  tool = _one_line(step.tool or "") or "(unknown tool)"
  notes = []
  if not facts.in_run(step.invocation_id):
    notes.append("(earlier invocation)")
  if not step.span_id:
    notes.append("(no span id; cannot be cited)")
  suffix = (" " + " ".join(notes)) if notes else ""
  return f"\n### Step {step.index}: {tool}  [status {step.status}]{suffix}\n"


def _step_body(
    step: ToolStep, facts: SessionFacts, max_result: int, max_arg: int
) -> str:
  profile = facts.profile
  out = []
  if step.purpose:
    out.append(f"purpose: {_cut(_one_line(step.purpose), max_arg)}\n")
  args = dict(step.args or {})
  code = (
      args.pop(profile.code_arg, None)
      if step.tool in profile.compute_tools
      else None
  )
  if args:
    out.append("args:\n" + _fenced(_untrusted(args, max_arg)))
  if code is not None:
    out.append("code:\n" + _fenced(_untrusted(code, max_arg)))
  if step.status == "MISSING":
    out.append("result: (no end row was logged for this call)\n")
  else:
    out.append(
        "result:\n" + _fenced(_untrusted(step.result, max_result) or "(empty)")
    )
  if step.is_truncated:
    out.append("[producer marked this row as clipped]\n")
  if step.error_message:
    out.append(
        "tool error message:\n"
        + _fenced(_untrusted(step.error_message, max_result))
    )
  return "".join(out)


def _final_section(facts: SessionFacts, limit: int) -> tuple[str, str]:
  if facts.final_response is None:
    body = "(no final response)\n"
  else:
    body = _fenced(_untrusted(facts.final_response, limit))
  return ("final_response", "\n## Final response\n" + body)


def _gap_anchor(facts: SessionFacts) -> Optional[tuple[str, Optional[str]]]:
  last = next(
      (
          row
          for row in reversed(facts.rows)
          if facts.in_run(_usable_text(row.get("invocation_id")))
      ),
      facts.rows[-1],
  )
  span_id = _usable_text(last.get("span_id"))
  if not span_id:
    return None
  return (span_id, _usable_text(last.get("trace_id")))
