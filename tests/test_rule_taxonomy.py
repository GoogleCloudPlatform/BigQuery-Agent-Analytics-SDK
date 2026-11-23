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
    self.rows = []

  def add(self, event_type, content=None, *, span_id=None, **fields):
    n = len(self.rows) + 1
    row = {
        "session_id": self.session_id,
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

  def test_check_returning_another_type_fails(self):
    session = _Session().start().answer("1")
    with pytest.raises(TypeError, match="RuleFailureLabel"):
      _rules(session, checks=[lambda facts: ["wrong source"]])

  def test_anchor_without_span_id_fails_closed(self):
    session = _Session().start().answer("1")
    session.rows[-1]["span_id"] = None
    with pytest.raises(ValueError, match="synthetic"):
      _rules(session)
