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
"""Tests for rule-based span attribution (parent #435).

Every session is a small synthetic ``agent_events`` fixture built in this
file, shaped like the rows the ADK BigQuery plugin writes. Nothing reaches
BigQuery.
"""

from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from datetime import timezone
import json
import time

import pytest

from bigquery_agent_analytics import failure_taxonomy
from bigquery_agent_analytics import rule_taxonomy
from bigquery_agent_analytics.rule_taxonomy import _answer_contains_reference
from bigquery_agent_analytics.rule_taxonomy import _parse_reference
from bigquery_agent_analytics.rule_taxonomy import _reference_seen_in
from bigquery_agent_analytics.rule_taxonomy import _reports_value
from bigquery_agent_analytics.rule_taxonomy import collect_session_facts
from bigquery_agent_analytics.rule_taxonomy import label_failed_session_rules
from bigquery_agent_analytics.rule_taxonomy import primary_label
from bigquery_agent_analytics.rule_taxonomy import RuleFailureLabel
from bigquery_agent_analytics.rule_taxonomy import ToolProfile

_T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)

# A data-analysis agent: one tool runs code, one previews a file.
_PROFILE = ToolProfile(
    compute_tools={"run_code"},
    preview_tools={"preview"},
    purpose_arg="purpose",
    tool_error_pattern=r"\AERROR:",
    failed_call_pattern=r"\Aexit code [1-9]",
    output_filter=lambda text: text.split("\n", 1)[-1],
)


class _Session:
  """Builds the rows of one session in timestamp order."""

  def __init__(self, session_id="sess-1"):
    self.session_id = session_id
    self.invocation_id = None
    self.rows = []

  def add(self, event_type, content=None, *, span_id=None, **fields):
    n = len(self.rows) + 1
    row = {
        "session_id": self.session_id,
        "invocation_id": self.invocation_id,
        "trace_id": "trace-1",
        "span_id": span_id or f"span-{n}",
        "event_id": f"event-{n}",
        "event_type": event_type,
        "agent": "analyst",
        "timestamp": _T0 + timedelta(seconds=n),
        "content": json.dumps(content) if content is not None else None,
        "status": "OK",
        "error_message": None,
    }
    row.update(fields)
    self.rows.append(row)
    return self

  def start(self, prompt="What is the mean rainfall?"):
    self.add("INVOCATION_STARTING")
    self.add("USER_MESSAGE_RECEIVED", {"text": prompt})
    return self.add("AGENT_STARTING")

  def llm(self, response):
    return self.add("LLM_RESPONSE", {"response": response})

  def tool(
      self, name, args, result, *, error=False, done=True, truncated=False
  ):
    span_id = f"tool-{len(self.rows) + 1}"
    self.llm(f"call: {name}")
    self.add("TOOL_STARTING", {"tool": name, "args": args}, span_id=span_id)
    if error:
      self.add(
          "TOOL_ERROR",
          {"tool": name},
          span_id=span_id,
          status="ERROR",
          error_message=result,
      )
    elif done:
      self.add(
          "TOOL_COMPLETED",
          {"tool": name, "result": result},
          span_id=span_id,
          is_truncated=truncated,
      )
    return self

  def answer(self, text):
    self.llm(f"text: '{text}'")
    self.add("AGENT_COMPLETED")
    return self.add("INVOCATION_COMPLETED")

  def call_limit(self, limit=30):
    message = f"Max number of llm calls limit of `{limit}` exceeded"
    for event_type in ("AGENT_ERROR", "INVOCATION_ERROR"):
      self.add(event_type, status="ERROR", error_message=message)
    return self

  def last_span(self):
    return self.rows[-1]["span_id"]

  def end_first(self):
    """Ties the last call's end row with its start and sorts it first.

    The session query in ``client.py`` breaks timestamp ties by
    ``event_type``, so TOOL_COMPLETED and TOOL_ERROR come before
    TOOL_STARTING.
    """
    start, end = self.rows[-2], self.rows[-1]
    assert start["event_type"] == "TOOL_STARTING"
    end["timestamp"] = start["timestamp"]
    self.rows[-2:] = [end, start]
    return self


def _rules(session, **kwargs):
  kwargs.setdefault("profile", _PROFILE)
  return label_failed_session_rules(session.rows, **kwargs)


def _ids(labels):
  return [label.rule_id for label in labels]


def _by_rule(labels):
  return {label.rule_id: label for label in labels}


def _steps(session):
  return collect_session_facts(session.rows, profile=_PROFILE).steps


def _label(**overrides):
  fields = dict(
      session_id="sess-1",
      trace_id="trace-1",
      span_id="span-1",
      failure_category="turn-waste",
      evidence="x",
      confidence=0.7,
      target_kind="span",
      rule_id="TEST",
  )
  fields.update(overrides)
  return RuleFailureLabel(**fields)


def _pasted_session(pasted_lines, *, after=None):
  """A preview, then code that pastes ``pasted_lines`` of it, then ``after``."""
  preview = [f"station_{i},{100 + i}.5" for i in range(5)]
  code = "data = '''\n" + "\n".join(preview[:pasted_lines]) + "\n'''"
  session = _Session().start()
  session.tool("preview", {"path": "/data/rain.csv"}, "\n".join(preview))
  session.tool("run_code", {"code": code}, "exit code 0\n102.5")
  if after:
    after(session)
  return session.answer("102.5")


class TestRuleFailureLabel:

  def test_as_tuple_is_the_rfc_shape(self):
    label = _label()
    assert label.as_tuple() == ("trace-1", "span-1", "turn-waste", "x", 0.7)

  def test_categories_are_the_frozen_names_except_unknown(self):
    assert rule_taxonomy.RULE_CATEGORY_NAMES == tuple(
        n for n in failure_taxonomy.FROZEN_CATEGORY_NAMES if n != "unknown"
    )

  @pytest.mark.parametrize("category", ["unknown", "timeout", ""])
  def test_rejects_other_categories(self, category):
    with pytest.raises(ValueError, match="G1-frozen"):
      _label(failure_category=category)

  @pytest.mark.parametrize("confidence", [0, 0.0, 1.5, -0.1, True, "0.5"])
  def test_rejects_confidence_outside_unit_interval(self, confidence):
    with pytest.raises(ValueError, match="confidence"):
      _label(confidence=confidence)

  @pytest.mark.parametrize("confidence", [1.0, 1, 0.01])
  def test_accepts_confidence_up_to_one(self, confidence):
    assert _label(confidence=confidence).confidence == confidence

  def test_rejects_missing_span_id(self):
    with pytest.raises(ValueError, match="never invent"):
      _label(span_id="")

  def test_rejects_unknown_target_kind(self):
    with pytest.raises(ValueError, match="target_kind"):
      _label(target_kind="session")

  def test_rejects_empty_rule_id(self):
    with pytest.raises(ValueError, match="rule_id"):
      _label(rule_id="")


class TestToolProfile:

  def test_tool_names_become_frozensets(self):
    profile = ToolProfile(compute_tools=["a", "a"], preview_tools=("b",))
    assert profile.compute_tools == frozenset({"a"})
    assert profile.preview_tools == frozenset({"b"})

  def test_rejects_a_bare_string(self):
    with pytest.raises(TypeError, match="compute_tools"):
      ToolProfile(compute_tools="run_code")

  def test_rejects_an_invalid_pattern(self):
    with pytest.raises(Exception):
      ToolProfile(tool_error_pattern="(")

  def test_rejects_an_output_filter_that_is_not_callable(self):
    with pytest.raises(TypeError, match="output_filter"):
      ToolProfile(output_filter="strip")


class TestSessionFacts:

  def test_pairs_tool_calls_and_strips_purpose(self):
    session = (
        _Session()
        .start()
        .tool("preview", {"path": "a.csv", "purpose": "look"}, "x,y")
        .tool("run_code", {"code": "print(1)"}, "exit code 1\nBoom")
        .tool("run_code", {"code": "print(2)"}, "", done=False)
    )
    facts = collect_session_facts(session.rows, profile=_PROFILE)
    first, second, third = facts.steps
    assert (first.tool, first.args, first.purpose) == (
        "preview",
        {"path": "a.csv"},
        "look",
    )
    assert first.status == "OK" and first.result == "x,y"
    assert second.status == "OK" and facts.failed_steps == (2,)
    assert third.status == "MISSING"

  def test_exposes_is_truncated_on_steps(self):
    session = _Session().start()
    session.tool("preview", {"path": "a.csv"}, "x,y ...", truncated=True)
    session.tool("preview", {"path": "b.csv"}, "x,y")
    assert [s.is_truncated for s in _steps(session)] == [True, False]

  def test_drops_rows_that_repeat_an_event_id(self):
    # The plugin writes at least once; a retried write keeps its event_id.
    session = _Session().start().tool("preview", {"path": "a.csv"}, "x")
    start = next(r for r in session.rows if r["event_type"] == "TOOL_STARTING")
    session.rows.insert(session.rows.index(start) + 1, dict(start))
    facts = collect_session_facts(session.rows, profile=_PROFILE)
    assert len(facts.steps) == 1
    assert facts.steps[0].status == "OK"
    assert facts.repeats == ()

  def test_drops_a_repeated_start_after_its_call_ended(self):
    # A fast call can log its start and end with the same timestamp; the
    # retried copy of the start then sorts after the end row.
    session = _Session().start().tool("preview", {"path": "a.csv"}, "x")
    start, end = session.rows[-2], session.rows[-1]
    end["timestamp"] = start["timestamp"]
    session.rows.append(dict(start))
    facts = collect_session_facts(session.rows, profile=_PROFILE)
    assert [s.status for s in facts.steps] == ["OK"]
    assert len(facts.rows) == len(session.rows) - 1

  def test_ignores_a_second_start_of_an_open_call(self):
    session = _Session().start().tool("preview", {"path": "a.csv"}, "x")
    start = next(r for r in session.rows if r["event_type"] == "TOOL_STARTING")
    copy = dict(start, event_id="another-id")
    session.rows.insert(session.rows.index(start) + 1, copy)
    assert len(_steps(session)) == 1

  def test_ignores_a_second_end_row_of_a_call(self):
    session = _Session().start().tool("preview", {"path": "a.csv"}, "x")
    session.rows.append(
        dict(
            session.rows[-1],
            event_id="another-id",
            content=json.dumps({"tool": "preview", "result": "y"}),
        )
    )
    assert [(s.status, s.result) for s in _steps(session)] == [("OK", "x")]

  def test_pairs_consecutive_calls_on_one_span_in_order(self):
    # A writer may stamp every row with the same ambient span id.
    session = _Session().start()
    session.tool("run_code", {"code": "a"}, "boom", error=True)
    session.tool("run_code", {"code": "b"}, "boom", error=True)
    session.tool("run_code", {"code": "c"}, "exit code 0\n42")
    session.answer("42")
    for row in session.rows:
      row["span_id"] = "ambient"
    steps = _steps(session)
    assert [(s.args["code"], s.status) for s in steps] == [
        ("a", "ERROR"),
        ("b", "ERROR"),
        ("c", "OK"),
    ]
    assert "TB-UNRECOVERED" not in _by_rule(_rules(session))

  @pytest.mark.parametrize("end_type", ["TOOL_COMPLETED", "TOOL_ERROR"])
  def test_pairs_an_end_row_sorted_before_its_start(self, end_type):
    error = end_type == "TOOL_ERROR"
    session = _Session().start()
    session.tool("run_code", {"code": "x"}, "boom", error=error).end_first()
    assert session.rows[-2]["event_type"] == end_type
    (step,) = _steps(session)
    assert step.args == {"code": "x"}
    assert step.status == ("ERROR" if error else "OK")
    assert step.row == len(session.rows) - 1  # the start row

  def test_pairs_rows_without_a_span_id_by_tool_name(self):
    session = _Session().start()
    session.tool("run_code", {"code": "a"}, "exit code 0\n1")
    session.tool("run_code", {"code": "b"}, "exit code 0\n2")
    session.answer("2")
    for row in session.rows:
      if row["event_type"].startswith("TOOL_"):
        row["span_id"] = None
    steps = _steps(session)
    assert [(s.args, s.status, s.result) for s in steps] == [
        ({"code": "a"}, "OK", "exit code 0\n1"),
        ({"code": "b"}, "OK", "exit code 0\n2"),
    ]
    # Unpaired, the last call looked unfinished, and TB-UNRECOVERED could
    # not anchor to it.
    assert _rules(session) == ()

  def test_pairs_an_end_row_without_a_span_id_with_its_start(self):
    session = _Session().start().tool("run_code", {"code": "x"}, "1")
    session.rows[-1]["span_id"] = None
    (step,) = _steps(session)
    assert (step.args, step.status, step.span_id) == (
        {"code": "x"},
        "OK",
        session.rows[-2]["span_id"],
    )

  def test_pairs_rows_with_a_new_span_id_on_every_row(self):
    # Some writers (e.g. the SDK's seed_events) give every row its own id.
    session = _Session().start()
    session.tool("preview", {"path": "a.csv"}, "x")
    session.tool("preview", {"path": "b.csv"}, "y")
    for n, row in enumerate(session.rows):
      row["span_id"] = f"row-{n}"
    steps = _steps(session)
    assert [(s.args["path"], s.result) for s in steps] == [
        ("a.csv", "x"),
        ("b.csv", "y"),
    ]

  def test_drops_a_start_stored_twice_when_its_end_sorts_first(self):
    # The bundled writer has no event_id and writes at least once.
    session = _Session().start()
    session.tool("run_code", {"code": "x"}, "exit code 0\n42").end_first()
    session.rows.append(dict(session.rows[-1]))
    for row in session.rows:
      row.pop("event_id")
    session.answer("42")
    assert [s.status for s in _steps(session)] == ["OK"]
    assert _rules(session) == ()

  def test_an_identical_retry_on_one_span_is_a_second_call(self):
    session = _Session().start()
    for _ in range(2):
      session.tool("run_code", {"code": "a"}, "exit code 1\nKeyError")
    session.answer("1")
    for row in session.rows:
      row["span_id"] = "ambient"
    facts = collect_session_facts(session.rows, profile=_PROFILE)
    assert (len(facts.steps), facts.repeats) == (2, (2,))

  def test_parallel_calls_on_one_span_pair_by_tool_name(self):
    session = _Session().start()
    session.add("TOOL_STARTING", {"tool": "preview", "args": {"path": "a"}})
    session.add("TOOL_STARTING", {"tool": "run_code", "args": {"code": "x"}})
    session.add("TOOL_COMPLETED", {"tool": "run_code", "result": "42"})
    session.add(
        "TOOL_ERROR",
        {"tool": "preview"},
        status="ERROR",
        error_message="file not found",
    )
    for row in session.rows:
      row["span_id"] = "ambient"
    assert [(s.tool, s.status) for s in _steps(session)] == [
        ("preview", "ERROR"),
        ("run_code", "OK"),
    ]

  def test_a_span_end_skips_a_later_start(self):
    # A start-less end on the invocation span must not take the next call.
    session = _Session().start()
    session.add("TOOL_COMPLETED", {"tool": "approve", "result": "approved"})
    session.add("TOOL_STARTING", {"tool": "run_code", "args": {"code": "x"}})
    session.add(
        "TOOL_ERROR",
        {"tool": "run_code"},
        status="ERROR",
        error_message="ERROR: down",
    )
    for row in session.rows[-3:]:
      row["span_id"] = "invocation"
    steps = _steps(session)
    assert [(s.tool, s.args, s.status) for s in steps] == [
        ("approve", {}, "OK"),
        ("run_code", {"code": "x"}, "ERROR"),
    ]

  def test_pairs_a_tied_end_without_a_span_id(self):
    session = _Session().start()
    session.tool("run_code", {"code": "x"}, "exit code 0\n1").end_first()
    for row in session.rows[-2:]:
      row["span_id"] = None
    (step,) = _steps(session)
    assert (step.args, step.status) == ({"code": "x"}, "OK")

  def test_a_start_less_end_belongs_to_its_invocation(self):
    session = _Session()
    session.invocation_id = "inv-1"
    session.start().tool("preview", {"path": "a"}, "x").answer("1")
    session.invocation_id = "inv-2"
    session.start()
    session.add(
        "TOOL_COMPLETED", {"tool": "preview", "result": "y"}, span_id="inv"
    )
    session.answer("2")
    facts = collect_session_facts(session.rows, profile=_PROFILE)
    assert [s.result for s in facts.run_steps] == ["y"]

  def test_resumed_long_running_results_are_not_repeats(self):
    # The ADK plugin logs a long-running tool's pending result on the call
    # span, then each result the user sends back as TOOL_COMPLETED on the
    # invocation span, which no TOOL_STARTING has.
    session = _Session().start()
    for order in ("o1", "o2"):
      session.tool("approve", {"order": order}, "pending")
    for _ in range(2):
      session.add(
          "TOOL_COMPLETED",
          {"tool": "approve", "result": "approved"},
          span_id="invocation",
      )
    facts = collect_session_facts(session.rows, profile=_PROFILE)
    assert [(s.args, s.result) for s in facts.steps] == [
        ({"order": "o1"}, "pending"),
        ({"order": "o2"}, "pending"),
        ({}, "approved"),
        ({}, "approved"),
    ]
    assert facts.repeats == ()

  def test_tool_name_fallback_skips_a_later_start(self):
    session = _Session().start()
    session.add("TOOL_COMPLETED", {"tool": "preview", "result": "early"})
    session.add("TOOL_STARTING", {"tool": "preview", "args": {"path": "a"}})
    for row in session.rows[-2:]:
      row["span_id"] = None
    assert [(s.result, s.status) for s in _steps(session)] == [
        ("early", "OK"),
        ("", "MISSING"),
    ]

  def test_tool_name_fallback_skips_a_start_with_its_own_end(self):
    # The NULL-span end row sorts between the call's start and its own end.
    session = _Session().start().tool("preview", {"path": "a.csv"}, "x")
    other = dict(
        session.rows[-1],
        span_id=None,
        event_id="other",
        content=json.dumps({"tool": "preview", "result": "other"}),
    )
    session.rows.insert(len(session.rows) - 1, other)
    assert [(s.args, s.result) for s in _steps(session)] == [
        ({"path": "a.csv"}, "x"),
        ({}, "other"),
    ]

  def test_end_rows_on_a_span_no_start_has_are_separate_calls(self):
    # The ADK plugin logs a long-running tool's resumed results on the
    # invocation span, with no TOOL_STARTING of their own.
    session = _Session().start()
    for result in ("approved", "shipped"):
      session.add(
          "TOOL_COMPLETED",
          {"tool": "ask_human", "result": result},
          span_id="invocation",
      )
    assert [s.result for s in _steps(session)] == ["approved", "shipped"]

  def test_pairs_string_timestamps_of_different_precision(self):
    session = _Session().start().tool("preview", {"path": "a.csv"}, "x")
    session.rows[-1]["span_id"] = None  # pairs by tool name and time
    session.rows[-2]["timestamp"] = "2026-01-01T00:00:04Z"
    session.rows[-1]["timestamp"] = "2026-01-01T00:00:04.000500Z"
    for row in session.rows[:-2]:
      row["timestamp"] = row["timestamp"].isoformat()
    assert [s.status for s in _steps(session)] == ["OK"]

  def test_counts_an_end_row_without_a_start(self):
    session = _Session().start().tool("preview", {"path": "a.csv"}, "x")
    session.answer("1")
    session.rows = [
        r for r in session.rows if r["event_type"] != "TOOL_STARTING"
    ]
    (step,) = _steps(session)
    assert (step.tool, step.args, step.status, step.result) == (
        "preview",
        {},
        "OK",
        "x",
    )
    assert "TB-NO-TOOLS" not in _by_rule(_rules(session))

  @pytest.mark.parametrize("flagged", ["TOOL_STARTING", "TOOL_COMPLETED"])
  def test_is_truncated_when_either_row_is(self, flagged):
    session = _Session().start().tool("run_code", {"code": "x"}, "1")
    next(r for r in session.rows if r["event_type"] == flagged)[
        "is_truncated"
    ] = True
    assert _steps(session)[0].is_truncated

  def test_repeats_ignore_the_purpose_argument(self):
    session = _Session().start()
    for purpose in ("first look", "second look"):
      session.tool("preview", {"path": "a.csv", "purpose": purpose}, "x")
    facts = collect_session_facts(session.rows, profile=_PROFILE)
    assert facts.repeats == (2,)

  @pytest.mark.parametrize(
      "second, duplicate",
      [
          ("Load the rain data file!", True),
          ("load the rain data file again", True),  # Jaccard 5/6
          ("load rain data file now", False),  # Jaccard 4/6
          ("load data", False),  # too few words to compare
      ],
  )
  def test_near_duplicate_purposes(self, second, duplicate):
    session = _Session().start()
    first = "load the rain data file"
    session.tool("preview", {"path": "a.csv", "purpose": first}, "x")
    session.tool("preview", {"path": "b.csv", "purpose": second}, "y")
    facts = collect_session_facts(session.rows, profile=_PROFILE)
    assert facts.near_duplicates == ((2,) if duplicate else ())

  def test_top_duplicate_counts_identical_purposes(self):
    session = _Session().start()
    for path in ("a.csv", "b.csv", "c.csv"):
      session.tool("preview", {"path": path, "purpose": "Load it"}, "x")
    facts = collect_session_facts(session.rows, profile=_PROFILE)
    assert facts.top_duplicate == ("Load it", 3)

  def test_rows_are_ordered_by_timestamp(self):
    session = _Session().start().answer("42")
    facts = collect_session_facts(list(reversed(session.rows)))
    assert facts.rows[0]["event_type"] == "INVOCATION_STARTING"
    assert facts.final_response == "42"

  def test_session_id_skips_unusable_values(self):
    session = _Session().start().answer("42")
    session.rows[0]["session_id"] = "null"
    assert collect_session_facts(session.rows).session_id == "sess-1"

  def test_tolerates_rows_without_an_event_type(self):
    session = _Session().start()
    session.add(None)
    session.answer("42")
    assert collect_session_facts(session.rows).completed

  def test_rejects_several_sessions(self):
    rows = _Session("a").start().rows + _Session("b").start().rows
    with pytest.raises(ValueError, match="per-session"):
      label_failed_session_rules(rows)

  def test_rejects_no_rows(self):
    with pytest.raises(ValueError, match="no agent_events rows"):
      label_failed_session_rules([])


class TestToolBlockers:

  def test_abort_anchors_to_the_first_error_outside_tool_rows(self):
    session = _Session().start()
    session.tool("run_code", {"code": "x"}, "sandbox busy", error=True)
    session.tool("preview", {"path": "a.csv"}, "x")
    session.add(
        "LLM_ERROR",
        status="ERROR",
        error_message="429 RESOURCE_EXHAUSTED https://example.com",
        span_id="llm-err",
    )
    session.add("AGENT_ERROR", status="ERROR", error_message="429")
    labels = _rules(session)
    assert _ids(labels) == ["TB-ABORT"]
    label = labels[0]
    assert label.failure_category == "tool blockers"
    assert (label.span_id, label.target_kind) == ("llm-err", "span")
    assert "after 2 tool call(s): 429 RESOURCE_EXHAUSTED" in label.evidence
    assert label.confidence == 0.95

  def test_abort_strips_urls_from_other_errors(self):
    session = _Session().start()
    session.add(
        "AGENT_ERROR",
        status="ERROR",
        error_message="503 from https://api.example.com/v1/x\ntraceback",
    )
    label = _by_rule(_rules(session))["TB-ABORT"]
    assert "503 from <url>" in label.evidence
    assert (
        "https://" not in label.evidence and "traceback" not in label.evidence
    )

  def test_abort_on_tool_errors_only_anchors_to_the_last_one(self):
    session = _Session().start()
    session.tool("run_code", {"code": "a"}, "sandbox busy", error=True)
    session.tool("run_code", {"code": "b"}, "sandbox gone", error=True)
    label = _by_rule(_rules(session))["TB-ABORT"]
    assert label.span_id == _steps(session)[1].span_id
    assert "sandbox gone" in label.evidence

  def test_abort_skips_an_error_the_run_got_past(self):
    session = _Session().start()
    session.add(
        "LLM_ERROR",
        status="ERROR",
        error_message="429 RESOURCE_EXHAUSTED",
        span_id="early",
    )
    session.tool("preview", {"path": "a.csv"}, "x")
    session.tool("preview", {"path": "b.csv"}, "y")
    session.add(
        "AGENT_ERROR",
        status="ERROR",
        error_message="sandbox crashed",
        span_id="late",
    )
    label = _by_rule(_rules(session))["TB-ABORT"]
    assert label.span_id == "late"
    assert "after 2 tool call(s): sandbox crashed" in label.evidence

  def test_a_tool_call_that_worked_is_progress(self):
    session = _Session().start()
    session.llm("call: preview")
    session.add(
        "TOOL_STARTING", {"tool": "preview", "args": {}}, span_id="call"
    )
    session.add(
        "LLM_ERROR", status="ERROR", error_message="429", span_id="early"
    )
    session.add(
        "TOOL_COMPLETED", {"tool": "preview", "result": "x"}, span_id="call"
    )
    session.add(
        "AGENT_ERROR", status="ERROR", error_message="boom", span_id="late"
    )
    assert _by_rule(_rules(session))["TB-ABORT"].span_id == "late"

  def test_a_model_response_is_progress(self):
    session = _Session().start()
    session.add(
        "LLM_ERROR", status="ERROR", error_message="429", span_id="early"
    )
    session.llm("thinking")
    session.add(
        "AGENT_ERROR", status="ERROR", error_message="boom", span_id="late"
    )
    assert _by_rule(_rules(session))["TB-ABORT"].span_id == "late"

  def test_an_errored_model_response_is_not_progress(self):
    session = _Session().start()
    session.add(
        "LLM_ERROR", status="ERROR", error_message="429", span_id="early"
    )
    session.add("LLM_RESPONSE", {"response": "x"}, status="ERROR")
    session.add("AGENT_ERROR", status="ERROR", error_message="boom")
    assert _by_rule(_rules(session))["TB-ABORT"].span_id == "early"

  def test_abort_prefers_the_error_after_a_tool_error(self):
    session = _Session().start()
    session.tool("run_code", {"code": "x"}, "busy", error=True)
    session.add(
        "LLM_ERROR", status="ERROR", error_message="429", span_id="llm-err"
    )
    assert _by_rule(_rules(session))["TB-ABORT"].span_id == "llm-err"

  def test_abort_on_parallel_tool_errors_anchors_to_the_last(self):
    session = _Session().start()
    session.llm("call: run_code | call: run_code")
    for span in ("t1", "t2"):
      session.add(
          "TOOL_STARTING",
          {"tool": "run_code", "args": {"code": span}},
          span_id=span,
      )
    for span in ("t1", "t2"):
      session.add(
          "TOOL_ERROR",
          {"tool": "run_code"},
          span_id=span,
          status="ERROR",
          error_message=span,
      )
    assert _by_rule(_rules(session))["TB-ABORT"].span_id == "t2"

  def test_a_completed_run_is_not_aborted(self):
    session = _Session().start()
    session.llm("text: '1'")
    session.add("AGENT_ERROR", status="ERROR", error_message="callback")
    session.add("INVOCATION_COMPLETED")
    assert "TB-ABORT" not in _by_rule(_rules(session))

  def test_abort_names_the_error_of_a_resumed_invocation(self):
    session = _Session().start()
    session.add("LLM_ERROR", status="ERROR", error_message="429")
    session.add("INVOCATION_ERROR", status="ERROR", error_message="429")
    session.add("INVOCATION_STARTING")
    session.add(
        "LLM_ERROR", status="ERROR", error_message="400 bad", span_id="late"
    )
    session.add("INVOCATION_ERROR", status="ERROR", error_message="400 bad")
    label = _by_rule(_rules(session))["TB-ABORT"]
    assert label.span_id == "late" and "400 bad" in label.evidence

  def test_no_abort_without_an_error_after_the_last_progress(self):
    session = _Session().start()
    session.add("LLM_ERROR", status="ERROR", error_message="429")
    session.tool("preview", {"path": "a.csv"}, "x")
    assert "TB-ABORT" not in _by_rule(_rules(session))

  def test_abort_counts_only_the_calls_before_it(self):
    session = _Session().start().tool("preview", {"path": "a.csv"}, "x")
    session.add(
        "LLM_ERROR", status="ERROR", error_message="429", span_id="llm-err"
    )
    session.add("TOOL_STARTING", {"tool": "preview", "args": {"path": "b"}})
    session.add("AGENT_ERROR", status="ERROR", error_message="429")
    label = _by_rule(_rules(session))["TB-ABORT"]
    assert label.span_id == "llm-err"
    assert "after 1 tool call(s)" in label.evidence

  def test_abort_on_a_tool_error_sorted_first_counts_its_call(self):
    session = _Session().start()
    session.tool("run_code", {"code": "a"}, "sandbox gone", error=True)
    label = _by_rule(_rules(session.end_first()))["TB-ABORT"]
    assert label.span_id == _steps(session)[0].span_id
    assert "after 1 tool call(s): sandbox gone" in label.evidence

  def test_a_sub_agent_completing_is_not_the_run_completing(self):
    session = _Session().start()
    session.add("AGENT_STARTING", agent="helper")
    session.tool("preview", {"path": "a.csv"}, "x")
    session.add("AGENT_COMPLETED", agent="helper")
    session.add(
        "LLM_ERROR", status="ERROR", error_message="429", span_id="llm-err"
    )
    session.add("AGENT_ERROR", status="ERROR", error_message="429")
    assert not collect_session_facts(session.rows).completed
    assert _by_rule(_rules(session))["TB-ABORT"].span_id == "llm-err"

  def test_an_earlier_invocation_completing_is_not_the_run_completing(self):
    session = _Session()
    session.invocation_id = "inv-1"
    session.start().tool("preview", {"path": "a.csv"}, "x").answer("1")
    session.invocation_id = "inv-2"
    session.start()
    session.add(
        "LLM_ERROR", status="ERROR", error_message="429", span_id="llm-err"
    )
    session.add("INVOCATION_ERROR", status="ERROR", error_message="429")
    facts = collect_session_facts(session.rows)
    assert (facts.invocation_id, facts.completed) == ("inv-2", False)
    label = _by_rule(_rules(session))["TB-ABORT"]
    assert label.span_id == "llm-err"
    assert "after 0 tool call(s)" in label.evidence

  def test_an_earlier_invocation_hitting_the_call_limit(self):
    session = _Session()
    session.invocation_id = "inv-1"
    session.start().tool("preview", {"path": "a.csv"}, "x").call_limit()
    session.invocation_id = "inv-2"
    session.start().tool("run_code", {"code": "x"}, "exit code 0\n1")
    session.answer("1")
    facts = collect_session_facts(session.rows)
    assert facts.completed and not facts.call_limit_hit
    assert _rules(session) == ()

  def test_ids_that_never_repeat_do_not_split_the_run(self):
    session = _Session().start().tool("preview", {"path": "a.csv"}, "x")
    session.answer("1")
    for n, row in enumerate(session.rows):
      row["invocation_id"] = f"inv-{n}"
    facts = collect_session_facts(session.rows)
    assert (facts.invocation_id, facts.completed) == (None, True)
    assert len(facts.run_steps) == 1

  def test_the_run_is_the_latest_invocation_in_any_row_order(self):
    session = _Session()
    session.invocation_id = "inv-1"
    session.start().add("AGENT_ERROR", status="ERROR", error_message="429")
    session.invocation_id = "inv-2"
    session.start().answer("1")
    session.rows[0]["timestamp"] = None
    for rows in (session.rows, list(reversed(session.rows))):
      facts = collect_session_facts(rows)
      assert (facts.invocation_id, facts.completed) == ("inv-2", True)

  def test_rows_without_a_timestamp_key_do_not_pick_the_run(self):
    session = _Session()
    session.invocation_id = "inv-1"
    session.start().add("AGENT_ERROR", status="ERROR", error_message="429")
    session.invocation_id = "inv-2"
    session.start().answer("1")
    del session.rows[0]["timestamp"]
    facts = collect_session_facts(session.rows)
    assert (facts.invocation_id, facts.completed) == ("inv-2", True)

  def test_a_transfer_completes_with_the_agent_that_took_over(self):
    # After transfer_to_agent, only the specialist logs AGENT_COMPLETED.
    session = _Session().start()
    session.tool("transfer_to_agent", {"agent_name": "specialist"}, "")
    session.add("AGENT_STARTING", agent="specialist")
    session.tool("run_code", {"code": "x"}, "ERROR: disk full")
    session.llm("text: 'Sorry, I could not finish.'")
    session.add("AGENT_COMPLETED", agent="specialist")
    session.add("INVOCATION_COMPLETED")
    assert collect_session_facts(session.rows).completed
    assert "TB-UNRECOVERED" in _by_rule(_rules(session))

  def test_a_resumed_invocation_that_completes(self):
    session = _Session().start()
    session.add("INVOCATION_ERROR", status="ERROR", error_message="429")
    session.add("INVOCATION_STARTING")
    session.answer("1")
    assert collect_session_facts(session.rows).completed

  def test_without_invocation_rows_an_agent_completion_counts(self):
    session = _Session().start().answer("1")
    session.rows = [
        r for r in session.rows if not r["event_type"].startswith("INVOC")
    ]
    assert collect_session_facts(session.rows).completed

  def test_the_answer_of_an_earlier_invocation_is_not_the_runs(self):
    session = _Session()
    session.invocation_id = "inv-1"
    session.start().answer("The mean was 812.45 mm.")
    session.invocation_id = "inv-2"
    session.start()
    session.add("LLM_ERROR", status="ERROR", error_message="429")
    session.add("INVOCATION_ERROR", status="ERROR", error_message="429")
    facts = collect_session_facts(session.rows)
    assert facts.final_response is None
    labels = _rules(session, reference_answer="812.45")
    assert _ids(labels) == ["TB-ABORT"]

  def test_untimed_rows_pick_the_last_invocation(self):
    session = _Session()
    session.invocation_id = "inv-1"
    session.start().add("AGENT_ERROR", status="ERROR", error_message="429")
    session.invocation_id = "inv-2"
    session.start().answer("1")
    for row in session.rows:
      row["timestamp"] = None
    assert collect_session_facts(session.rows).invocation_id == "inv-2"

  def test_a_follow_up_turn_without_tools(self):
    # The last turn only restates a value computed in an earlier turn.
    session = _Session()
    session.invocation_id = "inv-1"
    session.start()
    session.tool("run_code", {"code": "x"}, "exit code 0\nmean = 812.45")
    session.answer("812.45")
    session.invocation_id = "inv-2"
    session.start().answer("About 800 mm.")
    labels = _rules(session, reference_answer="812.45")
    assert _ids(labels) == ["FIN-SEEN-NOT-REPORTED"]

  def test_a_gap_label_anchors_on_the_runs_last_row(self):
    session = _Session()
    session.invocation_id = "inv-1"
    session.start().tool("preview", {"path": "a"}, "x").answer("1")
    session.invocation_id = "inv-2"
    session.start().llm("None").add("AGENT_COMPLETED")
    run_last = session.last_span()
    session.invocation_id = None
    session.add("STATE_DELTA")  # e.g. a writer's session-end row
    label = _by_rule(_rules(session))["FIN-NO-ANSWER"]
    assert (label.span_id, label.target_kind) == (run_last, "gap_after_span")

  def test_an_invocation_error_means_the_run_did_not_complete(self):
    session = _Session().start().answer("1")
    session.add("INVOCATION_ERROR", status="ERROR", error_message="boom")
    assert not collect_session_facts(session.rows).completed

  def test_unrecovered_looks_at_the_last_call_of_the_run(self):
    session = _Session()
    session.invocation_id = "inv-1"
    session.start()
    session.tool("run_code", {"code": "x"}, "backend down", error=True)
    session.answer("unknown")
    session.invocation_id = "inv-2"
    session.start().answer("1")
    assert "TB-UNRECOVERED" not in _by_rule(_rules(session))

  def test_call_limit_is_not_an_abort(self):
    session = _Session().start().tool("preview", {"path": "a.csv"}, "x")
    assert _ids(_rules(session.call_limit())) == ["TW-CALL-CAP"]

  def test_call_limit_after_a_tool_error_is_turn_waste(self):
    session = _Session().start()
    session.tool("preview", {"path": "a.csv"}, "x")
    session.tool("run_code", {"code": "x"}, "sandbox busy", error=True)
    labels = _rules(session.call_limit())
    assert _ids(labels) == ["TW-CALL-CAP"]
    assert primary_label(labels).span_id == _steps(session)[1].span_id

  def test_no_tools_needs_a_profile_with_tools(self):
    session = _Session().start().answer("42")
    label = _by_rule(_rules(session))["TB-NO-TOOLS"]
    assert (label.span_id, label.target_kind) == (
        session.last_span(),
        "gap_after_span",
    )
    assert _rules(session, profile=ToolProfile()) == ()

  def test_unrecovered_final_tool_error(self):
    session = _Session().start()
    session.tool("run_code", {"code": "x"}, "backend unavailable", error=True)
    session.answer("unknown")
    label = _by_rule(_rules(session))["TB-UNRECOVERED"]
    assert (label.span_id, label.target_kind) == (
        _steps(session)[0].span_id,
        "span",
    )
    assert "backend unavailable" in label.evidence

  def test_unrecovered_final_call_without_an_end_row(self):
    session = _Session().start()
    session.tool("run_code", {"code": "x"}, None, done=False)
    session.answer("unknown")
    label = _by_rule(_rules(session))["TB-UNRECOVERED"]
    assert "no TOOL_COMPLETED was logged" in label.evidence

  def test_tool_error_pattern_counts_as_tool_failure(self):
    session = _Session().start()
    session.tool("run_code", {"code": "x"}, "ERROR: timed out after 120s")
    session.answer("unknown")
    assert "TB-UNRECOVERED" in _by_rule(_rules(session))

  def test_a_failed_call_is_not_a_tool_failure(self):
    session = _Session().start()
    session.tool("run_code", {"code": "x"}, "exit code 1\nKeyError")
    session.answer("unknown")
    assert "TB-UNRECOVERED" not in _by_rule(_rules(session))


class TestFinalization:

  def test_seen_not_reported(self):
    session = _Session().start()
    session.tool("run_code", {"code": "x"}, "exit code 0\nmean = 812.45")
    session.tool("run_code", {"code": "y"}, "exit code 0\nmean = 790.1")
    session.answer("790.1")
    labels = _rules(session, reference_answer=812.45)
    assert _ids(labels) == ["FIN-SEEN-NOT-REPORTED"]
    label = labels[0]
    assert label.failure_category == "finalization"
    assert (label.span_id, label.target_kind) == (
        _steps(session)[0].span_id,
        "gap_after_span",
    )
    assert "step 1" in label.evidence and "'790.1'" in label.evidence

  def test_seen_when_the_end_row_sorts_first(self):
    session = _Session().start()
    session.tool("run_code", {"code": "x"}, "exit code 0\nmean = 812.45")
    session.end_first().answer("790.1")
    labels = _rules(session, reference_answer=812.45)
    assert _ids(labels) == ["FIN-SEEN-NOT-REPORTED"]

  def test_seen_needs_a_reference_answer(self):
    session = _Session().start()
    session.tool("run_code", {"code": "x"}, "exit code 0\nmean = 812.45")
    session.answer("790.1")
    assert "FIN-SEEN-NOT-REPORTED" not in _by_rule(_rules(session))

  def test_seen_ignores_a_short_number(self):
    session = _Session().start()
    session.tool("run_code", {"code": "x"}, "exit code 0\nrows: 12")
    session.answer("13")
    assert _rules(session, reference_answer=12) == ()

  def test_seen_reads_the_filtered_output(self):
    # The output filter drops the first line, so the header does not count.
    session = _Session().start()
    session.tool("run_code", {"code": "x"}, "812.45\nnothing printed")
    session.answer("790.1")
    assert _rules(session, reference_answer="812.45") == ()

  def test_in_answer_anchors_after_the_last_call(self):
    session = _Session().start()
    session.tool("run_code", {"code": "x"}, "exit code 0\ndone")
    session.answer("The mean is 812.45 mm")
    label = _by_rule(_rules(session, reference_answer="812.45"))[
        "FIN-IN-ANSWER"
    ]
    assert "inside prose" in label.evidence
    assert (label.span_id, label.target_kind) == (
        _steps(session)[-1].span_id,
        "gap_after_span",
    )

  def test_no_answer_after_a_final_tool_call(self):
    # The run completed, but its last model turn was a tool call.
    session = _Session().start()
    session.tool("run_code", {"code": "x"}, "exit code 0\n1")
    session.llm("call: run_code")
    session.add("AGENT_COMPLETED")
    labels = _rules(session)
    assert _ids(labels) == ["FIN-NO-ANSWER"]
    assert (labels[0].span_id, labels[0].target_kind) == (
        _steps(session)[-1].span_id,
        "gap_after_span",
    )

  def test_no_answer_after_an_empty_model_turn(self):
    # The plugin logs a turn with no content parts as "None".
    session = _Session().start()
    session.tool("run_code", {"code": "x"}, "exit code 0\n1")
    session.llm("None")
    session.add("AGENT_COMPLETED")
    assert _ids(_rules(session)) == ["FIN-NO-ANSWER"]


class TestReferenceMatching:

  @pytest.mark.parametrize(
      "answer, reference, how",
      [
          ("812.45", "812.45", "2-decimal precision"),
          ("812.449", "812.45", "2-decimal precision"),
          ("812.6", "812.45", None),
          ("The mean is 812.45 mm", "812.45", "inside prose"),
          ("812.45 mm at 3 stations", "812.45", None),
          ("['B', 'A']", ["A", "B"], "not in the expected format"),
          ("The stations are ['B', 'A']", ["A", "B"], "embedded in prose"),
          ("['A']", ["A", "B"], None),
          ("['A', 'B', 'C']", ["A", "B"], None),
          ("boston.", "Boston", "only in case"),
          ("City:\nBoston", "Boston", "one line"),
          ("The city is Boston", "Boston", "ends with"),
          ("Bostonian", "Boston", None),
          ("True", True, "only in case"),
          # Large integers are compared exactly, not as floats.
          ("12345678901234567", 12345678901234567, "0-decimal precision"),
          ("12345678901234568", 12345678901234567, None),
          ("12345678901234568", "12345678901234567", None),
          # A comma is a thousands separator, never a join.
          ("1,234", 1234, "0-decimal precision"),
          ("3,5", 35, None),
          ("Total: 1,234,567 rows", 1234567, "inside prose"),
          # A short value counts only where a line reports it.
          ("812.6, computed over 3 files", 3, None),
          ("The answer is 3.", 3, "inside prose"),
          ("Answer: 3", 3, "inside prose"),
          ("The answer is 3 days.", 3, "inside prose"),
          ("**Answer:** 42", 42, "inside prose"),
          ("The mean rainfall is 4.5 mm", 4.5, "inside prose"),
          ("I checked 3 tables.\n\n**Answer:** 42", 42, "inside prose"),
          ("Based on 3 tables, the answer is **42**.", 42, "inside prose"),
          ("Across 12 samples, the mean is 4.6 ms.", "4.6", "inside prose"),
          ("Of 120 users surveyed, retention was 42%.", "42%", "inside prose"),
          ("In Q3 2023 the store had 15 orders.", 15, "inside prose"),
          ("812.6, based on 3 files", 3, None),
          # Other references
          ('{"a": 1.5}', {"a": 1.5}, "only in case"),
          ("3,5", 5, None),  # a decimal comma
          ("[1.0]", "[1.00000000000000001]", None),
      ],
  )
  def test_answer_contains_reference(self, answer, reference, how):
    found = _answer_contains_reference(answer, _parse_reference(reference))
    if how is None:
      assert found is None
    else:
      assert how in found

  @pytest.mark.parametrize(
      "text, reference, seen",
      [
          ("mean = 812.45", 812.45, True),
          ("mean = 812.46", 812.45, False),
          ("rows: 12", 12, False),  # too few significant digits
          ("2001 5\n2002 7\n2003 9", 2002, False),  # a year as a row label
          ("2001 5\nanswer: 2002", 2002, True),
          ("[2, 1]", [1, 2], True),
          ("[1, 2, 3]", [1, 2], False),
          ("city: Boston", "Boston", True),
          ("Bostonian", "Boston", False),
          ("yes", "yes", False),  # too common to count
          ("id = 12345678901234568", 12345678901234567, False),
          ("total = 1,234,567", 1234567, True),
          # In a CSV row, commas separate fields.
          ("station,812,450", 812450, False),
          ("station,812,450", 450, True),
          ("812,450,2001", 812, True),
          ("[812,450]", 450, True),
          ("[812,450]", 812450, False),
      ],
  )
  def test_reference_seen_in(self, text, reference, seen):
    assert _reference_seen_in(text, _parse_reference(reference)) is seen

  def test_dense_series_needs_a_reported_line(self):
    series = " ".join(f"{100 + i * 0.5:.1f}" for i in range(30))
    reference = _parse_reference(105.5)
    assert "105.5" in series
    assert not _reference_seen_in(series, reference)
    assert _reference_seen_in(series + "\nresult = 105.5", reference)

  @pytest.mark.parametrize(
      "line, reports",
      [
          ("2002", True),
          ("x: 2002", True),
          ("x:2002", False),
          ("x :2002", True),
          ("the year is 2002.", True),
          ("this 2002", False),
          ("2002 7", False),
      ],
  )
  def test_reports_value(self, line, reports):
    assert _reports_value(line, _parse_reference(2002)) is reports

  @pytest.mark.parametrize(
      "answer, reference, how",
      [
          ("812.6, computed over 3 files", 3, None),
          ("812.6, computed across 3 files", 3, None),
          ("812.6, computed from 3 files", 3, None),
          ("812.6, computed using 3 files", 3, None),
          ("812.6, computed among 3 files", 3, None),
          ("812.6, computed over 3.", 3, "inside prose"),
          ("812.6, computed over 312 files", 312, "inside prose"),
          ("812.6, computed over 3.5 files", 3.5, "inside prose"),
      ],
  )
  def test_count_in_prose(self, answer, reference, how):
    found = _answer_contains_reference(answer, _parse_reference(reference))
    assert found is None if how is None else how in found

  def test_a_huge_reference_does_not_raise(self):
    reference = _parse_reference("1e1000000")
    assert _answer_contains_reference("the total is 1e1000000 units", reference)

  @pytest.mark.parametrize(
      "reference", ["1e-999999999", "1e-9999999999999999999"]
  )
  def test_an_extreme_exponent_does_not_raise(self, reference):
    session = _Session().start()
    session.tool("run_code", {"code": "x"}, "exit code 0\n5")
    session.answer("0.5")
    assert _rules(session, reference_answer=reference) == ()

  def test_reports_value_is_fast_on_long_lines(self):
    line = "x" * 100_000 + ":" * 20_000
    started = time.perf_counter()
    assert not _reports_value(line, _parse_reference(2002))
    assert time.perf_counter() - started < 1.0


class TestTurnWaste:

  def test_call_cap_anchors_to_the_first_wasted_call(self):
    session = _Session().start()
    session.tool("preview", {"path": "a.csv"}, "x")
    for i in range(3):
      session.tool("run_code", {"code": f"load({i})"}, "exit code 1\nKeyError")
    labels = _rules(session.call_limit())
    assert _ids(labels) == ["TW-CALL-CAP"]
    label = primary_label(labels)
    assert (label.span_id, label.target_kind) == (
        _steps(session)[1].span_id,
        "span",
    )
    assert label.confidence == 0.85
    assert "30-call limit" in label.evidence
    assert "3 of 4 calls were wasted (3 failed" in label.evidence

  def test_call_cap_with_little_waste_is_less_confident(self):
    session = _Session().start()
    for path in ("a.csv", "b.csv", "c.csv", "d.csv"):
      session.tool("preview", {"path": path}, "x")
    session.tool("run_code", {"code": "x"}, "exit code 1\nKeyError")
    label = _by_rule(_rules(session.call_limit()))["TW-CALL-CAP"]
    assert label.confidence == 0.65  # 1 of 5 calls wasted

  def test_call_cap_without_waste_marks_the_gap(self):
    session = _Session().start()
    session.tool("preview", {"path": "a.csv"}, "x")
    label = _by_rule(_rules(session.call_limit(5)))["TW-CALL-CAP"]
    assert (label.span_id, label.target_kind) == (
        _steps(session)[-1].span_id,
        "gap_after_span",
    )
    assert label.confidence == 0.65
    assert "5-call limit" in label.evidence

  def test_waste_counts_only_repeats_within_the_run(self):
    session = _Session()
    session.invocation_id = "inv-1"
    session.start()
    for i in range(6):
      session.tool("run_code", {"code": f"q{i}", "purpose": f"step {i}"}, "1")
    session.answer("1")
    session.invocation_id = "inv-2"
    session.start()
    for i in range(6):
      session.tool("run_code", {"code": f"q{i}", "purpose": f"step {i}"}, "1")
    session.answer("1")
    facts = collect_session_facts(session.rows, profile=_PROFILE)
    assert (facts.repeats, facts.near_duplicates, facts.waste_ratio) == (
        (),
        (),
        0.0,
    )
    assert "TW-THRASH" not in _by_rule(_rules(session))

  def test_waste_ratio_reads_the_run(self):
    session = _Session()
    session.invocation_id = "inv-1"
    session.start()
    for i in range(10):
      session.tool("preview", {"path": f"{i}"}, "x")
    session.answer("1")
    session.invocation_id = "inv-2"
    session.start()
    for i in range(6):
      session.tool("run_code", {"code": f"t{i}"}, "exit code 1\nKeyError")
    session.answer("2")
    assert (
        collect_session_facts(session.rows, profile=_PROFILE).waste_ratio == 1
    )

  def test_a_run_resumed_after_the_call_limit_that_completes(self):
    session = _Session().start().tool("preview", {"path": "a"}, "x")
    session.call_limit()
    session.add("INVOCATION_STARTING").answer("40 mm")
    facts = collect_session_facts(session.rows, profile=_PROFILE)
    assert (facts.completed, facts.call_limit_hit) == (True, False)
    assert "TW-CALL-CAP" not in _by_rule(_rules(session))

  def test_call_cap_message_comes_from_the_run(self):
    session = _Session()
    session.invocation_id = "inv-1"
    session.start().call_limit(5)
    session.invocation_id = "inv-2"
    session.start().tool("preview", {"path": "a.csv"}, "x").call_limit(30)
    label = _by_rule(_rules(session))["TW-CALL-CAP"]
    assert "30-call limit" in label.evidence

  def test_call_cap_reads_only_the_run(self):
    session = _Session()
    session.invocation_id = "inv-1"
    session.start()
    for _ in range(4):
      session.tool("preview", {"path": "a.csv"}, "x")
    session.answer("1")
    session.invocation_id = "inv-2"
    session.start().tool("run_code", {"code": "x"}, "exit code 0\n1")
    label = _by_rule(_rules(session.call_limit()))["TW-CALL-CAP"]
    assert (label.span_id, label.target_kind) == (
        _steps(session)[-1].span_id,
        "gap_after_span",
    )
    assert label.confidence == 0.65
    assert "after 1 tool calls" in label.evidence
    assert "0 of 1 calls were wasted" in label.evidence

  @pytest.mark.parametrize(
      "failed, succeeded, fires",
      [
          (5, 3, False),  # 5 wasted: fewer than 6
          (6, 9, True),  # 6 of 15: exactly 40%
          (6, 10, False),  # 6 of 16: under 40%
      ],
  )
  def test_thrash_thresholds(self, failed, succeeded, fires):
    session = _Session().start()
    for i in range(failed):
      session.tool("run_code", {"code": f"try({i})"}, "exit code 1\nKeyError")
    for i in range(succeeded):
      session.tool("run_code", {"code": f"ok({i})"}, f"exit code 0\n{i}")
    session.answer("3")
    labels = _rules(session)
    assert _ids(labels) == (["TW-THRASH"] if fires else [])
    if fires:
      assert labels[0].span_id == _steps(session)[0].span_id
      assert not labels[0].primary
      assert primary_label(labels) is None


class TestIncompleteEvidence:

  def test_preview_only_reads_the_whole_session(self):
    # A follow-up turn may only reformat an answer computed earlier.
    session = _Session()
    session.invocation_id = "inv-1"
    session.start().tool("run_code", {"code": "x"}, "exit code 0\n1")
    session.answer("1")
    session.invocation_id = "inv-2"
    session.start().answer("1.0")
    assert _rules(session) == ()

  def test_preview_only(self):
    session = _Session().start()
    session.tool("preview", {"path": "a.csv"}, "x,y\n1,2")
    session.answer("2")
    labels = _rules(session)
    assert _ids(labels) == ["IE-PREVIEW-ONLY"]
    assert labels[0].failure_category == "incomplete evidence"
    assert (labels[0].span_id, labels[0].target_kind) == (
        _steps(session)[0].span_id,
        "gap_after_span",
    )

  def test_preview_only_needs_compute_tools(self):
    session = _Session().start()
    session.tool("preview", {"path": "a.csv"}, "x,y\n1,2")
    session.answer("2")
    assert _rules(session, profile=ToolProfile(preview_tools={"preview"})) == ()

  @pytest.mark.parametrize("pasted, fires", [(2, False), (3, True), (5, True)])
  def test_pasted_preview_needs_three_lines(self, pasted, fires):
    session = _pasted_session(pasted)
    labels = _by_rule(_rules(session))
    assert ("IE-PASTED-PREVIEW" in labels) is fires
    if fires:
      label = labels["IE-PASTED-PREVIEW"]
      assert label.span_id == _steps(session)[1].span_id
      assert f"{pasted} lines pasted" in label.evidence
      assert "rain.csv" in label.evidence

  @pytest.mark.parametrize(
      "after",
      [
          # A later call that failed, or that never logged an end row, is
          # not the computation the answer came from.
          lambda s: s.tool("run_code", {"code": "y"}, "exit code 1\nKeyError"),
          lambda s: s.tool("run_code", {"code": "y"}, None, done=False),
      ],
  )
  def test_pasted_preview_targets_the_last_completed_computation(self, after):
    session = _pasted_session(3, after=after)
    label = _by_rule(_rules(session))["IE-PASTED-PREVIEW"]
    assert label.span_id == _steps(session)[1].span_id

  def test_reading_the_file_is_not_pasting(self):
    preview = "\n".join(f"station_{i},{100 + i}.5" for i in range(5))
    code = "df = read('/data/rain.csv')\n" + preview
    session = _Session().start()
    session.tool("preview", {"path": "/data/rain.csv"}, preview)
    session.tool("run_code", {"code": code}, "exit code 0\n102.5")
    session.answer("102.5")
    assert "IE-PASTED-PREVIEW" not in _by_rule(_rules(session))


class TestChecksAndPrecedence:

  def test_builtin_order_then_checks(self):
    session = _Session().start()
    session.tool("run_code", {"code": "x"}, "exit code 0\n812.45")
    session.call_limit()

    def wrong_file(facts):
      return facts.label(
          "wrong source",
          "BENCH-WRONG-FILE",
          "read b.csv, the task needs a.csv",
          0.7,
          step=facts.steps[0],
      )

    labels = _rules(session, reference_answer=812.45, checks=[wrong_file])
    assert _ids(labels) == [
        "FIN-SEEN-NOT-REPORTED",
        "TW-CALL-CAP",
        "BENCH-WRONG-FILE",
    ]
    assert primary_label(labels).rule_id == "FIN-SEEN-NOT-REPORTED"
    assert labels[-1].span_id == _steps(session)[0].span_id

  def test_checks_may_return_none_or_several_labels(self):
    session = _Session().start().tool("preview", {"path": "a"}, "x").answer("1")

    def two(facts):
      return [
          facts.label("wrong source", "A", "a", 0.5),
          facts.label("task/planning", "B", "b", 0.5),
      ]

    labels = _rules(session, checks=[lambda facts: None, two])
    assert _ids(labels)[-2:] == ["A", "B"]
    assert labels[-1].target_kind == "gap_after_span"
    assert labels[-1].span_id == session.last_span()

  def test_check_label_on_a_row_of_the_session(self):
    session = _Session().start().answer("1")
    labels = _rules(session, checks=[lambda facts: _label()])
    assert labels[-1] == _label()

  def test_check_label_without_a_trace_id(self):
    session = _Session().start().answer("1")
    label = _label(trace_id=None)
    assert _rules(session, checks=[lambda facts: label])[-1] == label

  @pytest.mark.parametrize(
      "overrides, match",
      [
          (dict(session_id="sess-2"), "for session 'sess-2'"),
          (dict(span_id="span-999"), "no row of session"),
          (dict(trace_id="trace-2"), "no row of session"),
      ],
  )
  def test_check_label_must_anchor_in_the_session(self, overrides, match):
    session = _Session().start().answer("1")
    with pytest.raises(ValueError, match=match):
      _rules(session, checks=[lambda facts: _label(**overrides)])

  def test_check_returning_another_type_fails(self):
    session = _Session().start().answer("1")
    with pytest.raises(TypeError, match="RuleFailureLabel"):
      _rules(session, checks=[lambda facts: ["wrong source"]])

  def test_anchor_without_span_id_fails_closed(self):
    session = _Session().start().answer("1")
    session.rows[-1]["span_id"] = None
    with pytest.raises(ValueError, match="synthetic"):
      _rules(session)
