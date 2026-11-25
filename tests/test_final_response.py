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
"""Tests for the final answer of a trace (#595).

Every case runs through both ``Trace.final_response`` and
``SessionTrace.extract_final_response``, which share one implementation.
Rows are shaped like the ones the ADK BigQuery plugin and the bundled
tracing writer store.
"""

from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from datetime import timezone
import json
import time

import pytest

from bigquery_agent_analytics.trace import _flag
from bigquery_agent_analytics.trace import Span
from bigquery_agent_analytics.trace import Trace
from bigquery_agent_analytics.trace_evaluator import SessionTrace
from bigquery_agent_analytics.trace_evaluator import TraceEvent

_T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
_ADK = {"adk": {"schema_version": "1", "app_name": "app"}}
_WRITER = {"writer": {"plugin": "bigquery-agent-analytics-tracing"}}


def _row(event_type, content=None, *, invocation="inv-1", attributes=None):
  return {
      "event_type": event_type,
      "content": content,
      "invocation_id": invocation,
      "attributes": _ADK if attributes is None else attributes,
  }


def _llm(response, **kwargs):
  return _row("LLM_RESPONSE", {"response": response}, **kwargs)


def _user(text="question", **kwargs):
  return _row("USER_MESSAGE_RECEIVED", {"text_summary": text}, **kwargs)


def _completed(content=None, **kwargs):
  return _row("AGENT_COMPLETED", content, **kwargs)


def _answers(rows):
  """The final answer from both accessors, which must agree."""
  times = [_T0 + timedelta(seconds=i) for i in range(len(rows))]
  spans = [
      Span(
          event_type=row["event_type"],
          agent="agent",
          timestamp=ts,
          content=row["content"],
          attributes=row["attributes"],
          invocation_id=row["invocation_id"],
      )
      for row, ts in zip(rows, times)
  ]
  events = [
      TraceEvent(
          event_type=row["event_type"],
          agent="agent",
          timestamp=ts,
          content=row["content"],
          attributes=row["attributes"],
          invocation_id=row["invocation_id"],
      )
      for row, ts in zip(rows, times)
  ]
  from_trace = Trace(trace_id="t", session_id="s", spans=spans).final_response
  from_session = SessionTrace(
      session_id="s", user_id="u", events=events
  ).extract_final_response()
  assert from_trace == from_session
  return from_trace


class TestCurrentInvocation:
  """#595 item 1: the answer comes from the invocation being answered."""

  @pytest.mark.parametrize("last_turn", ["None", "call: lookup"])
  def test_an_earlier_invocations_answer_is_not_returned(self, last_turn):
    rows = [
        _user(invocation="inv-1"),
        _llm("text: 'Answer one'", invocation="inv-1"),
        _completed({"response": "Answer one"}, invocation="inv-1"),
        _user(invocation="inv-2"),
        _llm(last_turn, invocation="inv-2"),
    ]
    assert _answers(rows) is None

  def test_a_completion_in_the_same_invocation_is_read(self):
    rows = [
        _llm("text: 'Answer one'", invocation="inv-1"),
        _user(invocation="inv-2"),
        _llm("call: exit_loop", invocation="inv-2"),
        _completed({"response": "Answer two"}, invocation="inv-2"),
    ]
    assert _answers(rows) == "Answer two"

  def test_a_nested_invocation_belongs_to_its_parent(self):
    child = dict(_ADK, adk={"parent_invocation_id": "inv-2"})
    rows = [
        _llm("text: 'Answer one'", invocation="inv-1"),
        _user(invocation="inv-2"),
        _llm("text: 'Answer two'", invocation="inv-2"),
        _llm("call: finish", invocation="inv-2-child", attributes=child),
        _llm("None", invocation="inv-2-child", attributes=child),
    ]
    assert _answers(rows) == "Answer two"

  def test_a_nested_invocations_text_is_not_the_answer(self):
    # The parent called an agent as a tool, which answered it; the parent
    # itself never answered.
    child = dict(_ADK, adk={"parent_invocation_id": "inv-2"})
    rows = [
        _user(invocation="inv-2"),
        _llm("call: research_agent", invocation="inv-2"),
        _llm("text: 'internal notes'", invocation="inv-c", attributes=child),
        _completed(invocation="inv-c", attributes=child),
        _llm("None", invocation="inv-2"),
    ]
    assert _answers(rows) is None

  def test_rows_without_an_id_inside_the_invocation_count(self):
    rows = [
        _user(invocation="inv-1"),
        _llm("text: 'Answer one'", invocation="inv-1"),
        _user(invocation="inv-2"),
        _llm("text: 'Answer two'", invocation=None),
    ]
    assert _answers(rows) == "Answer two"

  def test_an_agent_tool_session_read_alone(self):
    # AgentTool runs the agent in a session of its own; every row names
    # the caller's invocation as its parent, which is not in the input.
    child = dict(_ADK, adk={"parent_invocation_id": "e-parent"})
    rows = [
        _user(invocation="e-child", attributes=child),
        _llm("text: 'X is 42.'", invocation="e-child", attributes=child),
        _completed(invocation="e-child", attributes=child),
    ]
    assert _answers(rows) == "X is 42."

  def test_without_invocation_ids_the_last_user_message_bounds_it(self):
    rows = [
        _user(invocation=None),
        _llm("text: 'Answer one'", invocation=None),
        _user(invocation=None),
        _llm("None", invocation=None),
    ]
    assert _answers(rows) is None

  def test_ids_that_never_repeat_do_not_split_the_session(self):
    # Some generators (seed_events) give every row its own invocation_id
    # and write no ``adk`` envelope.
    rows = [
        _user(invocation="a", attributes={}),
        _llm("text: 'Answer'", invocation="b", attributes={}),
        _llm("None", invocation="c", attributes={}),
    ]
    assert _answers(rows) == "Answer"

  def test_one_row_per_invocation_after_an_event_type_filter(self):
    # get_session_trace(include_event_types=["LLM_RESPONSE"]) on ADK rows.
    rows = [
        _llm("text: 'Answer to Q1'", invocation="inv-1"),
        _row("LLM_RESPONSE", {"usage": {"total": 1}}, invocation="inv-2"),
    ]
    assert _answers(rows) is None

  def test_a_trailing_one_off_id_is_not_the_invocation(self):
    # A writer that stamps a fresh id on rows logged after the turn ended.
    rows = [
        _user(invocation="turn-1", attributes=_WRITER),
        _llm("Fixed the bug.", invocation="turn-1", attributes=_WRITER),
        _row(
            "TOOL_STARTING", {"tool": "x"}, invocation="x1", attributes=_WRITER
        ),
    ]
    assert _answers(rows) == "Fixed the bug."

  def test_an_id_less_row_before_the_invocation_is_not_read(self):
    rows = [
        _llm("text: 'Old answer'", invocation=None),
        _user(invocation="inv-2"),
        _llm("None", invocation="inv-2"),
        _llm("None", invocation="inv-2"),
    ]
    assert _answers(rows) is None


class TestAgentResponse:
  """AGENT_RESPONSE rows hold the answer for some agents and views."""

  def test_a_final_response_tool(self):
    rows = [
        _llm("call: set_model_response"),
        _row(
            "AGENT_RESPONSE",
            {"response": {"answer": 42}, "text_summary": "The answer is 42."},
        ),
    ]
    assert _answers(rows) == "The answer is 42."

  def test_a_compatibility_view_with_null_model_turns(self):
    # BQCA compatibility views leave LLM_RESPONSE content NULL.
    rows = [
        _user(),
        _row("LLM_RESPONSE", None),
        _row("AGENT_RESPONSE", {"response": "There were 42 orders."}),
        _row("INVOCATION_COMPLETED"),
    ]
    assert _answers(rows) == "There were 42 orders."

  def test_a_text_summary_is_preferred(self):
    # The plugin's response_text_key fills text_summary with the answer.
    rows = [
        _row(
            "AGENT_RESPONSE",
            {
                "text_summary": "Paris.",
                "response": 'text: \'{"response": "Paris."}\'',
            },
        )
    ]
    assert _answers(rows) == "Paris."

  def test_the_plugins_text_without_thoughts(self):
    rows = [
        _llm("text: 'thinking...' | text: 'Answer'"),
        _row("AGENT_RESPONSE", {"response": "text: 'Answer'"}),
    ]
    assert _answers(rows) == "Answer"


class TestCompletions:
  """Several AGENT_COMPLETED rows in one invocation (sub-agents)."""

  def test_the_latest_completion_with_an_answer(self):
    rows = [
        _llm("call: transfer_to_agent"),
        _completed({"response": "Sub-agent done."}),
        _completed({"response": "Root answer."}),
    ]
    assert _answers(rows) == "Root answer."

  @pytest.mark.parametrize(
      "last", [None, {"response": "call: exit_loop"}, {"response": "None"}]
  )
  def test_a_later_completion_without_an_answer_is_skipped(self, last):
    rows = [
        _llm("call: transfer_to_agent"),
        _completed({"response": "Root answer."}),
        _completed(last),
    ]
    assert _answers(rows) == "Root answer."


class TestProducers:
  """#595 item 2: only the plugin's rows use its part encoding."""

  @pytest.mark.parametrize(
      "answer", ["None", "call: lookup", "Status | call: support"]
  )
  def test_the_bundled_writer_stores_answers_verbatim(self, answer):
    rows = [_llm(answer, attributes=_WRITER)]
    assert _answers(rows) == answer

  @pytest.mark.parametrize(
      "answer", ["call: 555-0100", "call: (none)", "call: 12-34", "call: a b"]
  )
  def test_a_call_needs_a_function_name(self, answer):
    assert _answers([_llm(answer)]) == answer

  @pytest.mark.parametrize(
      "name",
      [
          "3d_tool",
          "1password_lookup",
          "ns:tool",
          "lookup(query='x')",
          'lookup({"q": "x"})',
      ],
  )
  def test_function_names_may_start_with_a_digit(self, name):
    assert _answers([_llm("text: 'x'"), _llm(f"call: {name}")]) is None

  def test_text_summary_is_not_decoded(self):
    rows = [_row("LLM_RESPONSE", {"text_summary": "call: lookup"})]
    assert _answers(rows) == "call: lookup"

  def test_rows_without_identity_still_use_the_encoding(self):
    # Plugin versions before the ``adk`` envelope.
    rows = [_llm("text: 'Done.'", attributes={}), _llm("None", attributes={})]
    assert _answers(rows) == "Done."


class TestTruncation:
  """#595 item 3: a clipped payload is not read as a tool call."""

  def test_clipped_text_keeps_its_visible_text(self):
    clipped = "text: 'Contact the desk | call: support...[TRUNCATED]"
    assert _answers([_llm(clipped)]) == (
        "Contact the desk | call: support...[TRUNCATED]"
    )

  @pytest.mark.parametrize(
      "clipped",
      [
          "call: lookup | text: 'Che...[TRUNCATED]",
          "call: lookup_cus...[TRUNCATED]",
          "text: 'Checking.' | other | cal...[TRUNCATED]",
      ],
  )
  def test_a_clipped_turn_that_calls_a_tool(self, clipped):
    assert _answers([_llm("text: 'Earlier.'"), _llm(clipped)]) is None

  @pytest.mark.parametrize(
      "clipped",
      [
          "text: 'Run this: echo 'ERROR' | c...[TRUNCATED]",
          "text: 'Press 'Help' | call: supp...[TRUNCATED]",
          "text: 'Filter it with jq '.items' | t...[TRUNCATED]",
      ],
  )
  def test_a_cut_after_a_quote_stays_in_the_text(self, clipped):
    # The cut may as well have landed inside the text.
    assert _answers([_llm(clipped)]) == clipped[len("text: '") :]

  def test_a_clipped_answer_parses_in_linear_time(self):
    table = ("text: '" + "| a " * 200_000)[:500_000] + "...[TRUNCATED]"
    started = time.perf_counter()
    assert _answers([_llm(table)]) == table[len("text: '") :]
    assert time.perf_counter() - started < 1.5

  def test_a_clipped_text_part_after_another(self):
    clipped = "text: 'First.' | text: 'Sec...[TRUNCATED]"
    assert _answers([_llm(clipped)]) == "First.\nSec...[TRUNCATED]"


class TestVerbatimRows:
  """Rows from producers that do not use the plugin's encoding."""

  @pytest.mark.parametrize(
      "answer", ["other", "call: support", "Executable code (sql): SELECT 1"]
  )
  def test_an_evalbench_completion(self, answer):
    evalbench = {"experiment_id": "x", "evalbench_scenario_id": "e1"}
    rows = [
        _user(attributes=evalbench),
        _completed(
            {"response": answer, "text_summary": answer}, attributes=evalbench
        ),
    ]
    assert _answers(rows) == answer


class TestStringContract:
  """#595 item 4: the answer is always a string or None."""

  def test_a_structured_completion_prefers_its_summary(self):
    rows = [
        _llm("call: finish"),
        _completed({"response": {"answer": "Done."}, "text_summary": "Done."}),
    ]
    assert _answers(rows) == "Done."

  def test_a_structured_completion_without_a_summary_is_json(self):
    rows = [_llm("call: finish"), _completed({"response": {"answer": 1}})]
    assert _answers(rows) == '{"answer":1}'

  @pytest.mark.parametrize("completion", ["call: exit_loop", "None"])
  def test_a_completion_is_filtered_like_a_model_turn(self, completion):
    rows = [_llm("call: finish"), _completed({"response": completion})]
    assert _answers(rows) is None

  def test_a_structured_model_response_is_json(self):
    rows = [_llm({"intent": "return", "needs_photo": True})]
    assert _answers(rows) == '{"intent":"return","needs_photo":true}'


class TestPartShapes:
  """#595 item 7: turns are classified by their parts."""

  @pytest.mark.parametrize(
      "last_turn",
      [
          "call: run_python | other",
          "call: search | text: 'Here it is.'",
          "call: run_python  ",
          "call: lookup(id=3)",
          "resp: lookup | call: next_step",
          "other | call: run_python",
      ],
  )
  def test_a_turn_with_a_call_holds_no_answer(self, last_turn):
    assert _answers([_llm("text: 'Earlier.'"), _llm(last_turn)]) is None

  @pytest.mark.parametrize(
      "turn, expected",
      [
          ("text: 'a' | text: 'b'", "a\nb"),
          ("text: 'It's done | now'", "It's done | now"),
          ("text: 'Answer'  ", "Answer"),
      ],
  )
  def test_text_parts(self, turn, expected):
    assert _answers([_llm(turn)]) == expected

  @pytest.mark.parametrize(
      "turn", ["other", "resp: lookup", "text: ''", "Executable code (PY): x"]
  )
  def test_a_turn_without_text_is_skipped(self, turn):
    assert _answers([_llm("text: 'Earlier.'"), _llm(turn)]) == "Earlier."

  def test_model_text_may_contain_a_delimiter(self):
    # The model wrote "' | call: x | None"; the turn then calls run_python.
    turn = "text: 'Done. ' | call: x | None' | call: run_python"
    assert _answers([_llm("text: 'Earlier.'"), _llm(turn)]) is None

  @pytest.mark.parametrize(
      "turn, expected",
      [
          (
              "text: 'Computing.' | Executable code (PYTHON): print(a | b)"
              " | Code execution result (OUTCOME_OK): 3"
              " | text: 'The answer is 3.'",
              "Computing.\nThe answer is 3.",
          ),
          (
              "Executable code (PYTHON): print(df.to_markdown())"
              " | Code execution result (OUTCOME_OK): | a | b |\n|--:|--:|\n"
              "| 1 | 2 | | text: 'Here is the table.'",
              "Here is the table.",
          ),
          (
              "Executable code (PYTHON): x = 1"
              " | Code execution result (OUTCOME_OK):  | text: 'Done.'",
              "Done.",
          ),
          ("text: 'Pick 'A' | call: lookup | then answer.'", None),
          ("text: 'Use 'x' | otherwise stop'", "Use 'x' | otherwise stop"),
      ],
  )
  def test_separators_inside_parts(self, turn, expected):
    expected = expected or "Pick 'A' | call: lookup | then answer."
    assert _answers([_llm("text: 'Earlier.'"), _llm(turn)]) == expected

  def test_a_code_only_turn_with_a_pipe_is_skipped(self):
    turn = "Executable code (PYTHON): x = a | b"
    assert _answers([_llm("text: 'Earlier.'"), _llm(turn)]) == "Earlier."

  def test_clipped_text_with_a_quoted_word_before_call(self):
    clipped = (
        "text: 'If the kiosk shows 'Out of order' | call: the front desk"
        " on ext...[TRUNCATED]"
    )
    assert _answers([_llm(clipped)]) == clipped[len("text: '") :]

  def test_a_quoted_cell_table_parses_in_linear_time(self):
    table = "text: '" + "| 'alpha' | 'beta' |\n" * 20_000 + "'"
    started = time.perf_counter()
    assert _answers([_llm(table)]) == table[len("text: '") : -1]
    assert time.perf_counter() - started < 1.0

  def test_a_long_chain_parses_in_linear_time(self):
    chain = "text: '" + " | call: x" * 20_000
    started = time.perf_counter()
    assert _answers([_llm(chain)]) == chain[len("text: '") :]
    assert time.perf_counter() - started < 1.0


class TestSkippedRows:
  """#595 item 8: empty rows are skipped, and the loader's ``raw`` key."""

  @pytest.mark.parametrize(
      "content",
      [
          {"usage": {"prompt": 1, "completion": 0, "total": 1}},
          {"response": ""},
          None,
      ],
  )
  def test_an_empty_model_turn_is_skipped(self, content):
    rows = [_llm("text: 'Done.'"), _row("LLM_RESPONSE", content or {})]
    assert _answers(rows) == "Done."

  def test_a_turn_that_calls_a_tool_ignores_its_summary(self):
    content = {"response": "call: lookup", "text_summary": "Looking it up."}
    assert _answers([_row("LLM_RESPONSE", content)]) is None

  @pytest.mark.parametrize(
      "cell, expected",
      [("The answer is 42.", "The answer is 42."), ("call: lookup", None)],
  )
  def test_a_scalar_json_string_from_the_loader(self, cell, expected):
    # The BigQuery client decodes a JSON string cell to a plain str, which
    # the loader then wraps as {"raw": ...}.
    span = Span.from_bigquery_row(
        {
            "event_type": "LLM_RESPONSE",
            "timestamp": _T0,
            "content": cell,
            "attributes": json.dumps(_ADK),
            "invocation_id": "inv-1",
        }
    )
    assert span.content == {"raw": cell}
    trace = Trace(trace_id="t", session_id="s", spans=[span])
    assert trace.final_response == expected


class TestSessionTraceLoading:
  """PerformanceEvaluator builds SessionTrace with the fields the walk reads."""

  def _session_trace(self, rows):
    from bigquery_agent_analytics.performance_evaluator import PerformanceEvaluator

    spans = [
        Span(
            event_type=row["event_type"],
            agent="agent",
            timestamp=_T0 + timedelta(seconds=i),
            content=row["content"],
            attributes=row["attributes"],
            invocation_id=row["invocation_id"],
        )
        for i, row in enumerate(rows)
    ]
    trace = Trace(trace_id="t", session_id="s", spans=spans)
    return PerformanceEvaluator._to_session_trace(
        trace, PerformanceEvaluator._DEFAULT_EVENT_TYPES
    )

  def test_agent_response_rows_are_fetched_by_default(self):
    rows = [
        _user(),
        _row("LLM_RESPONSE", None),
        _row("AGENT_RESPONSE", {"response": "42 orders."}),
    ]
    assert self._session_trace(rows).final_response == "42 orders."

  def test_invocation_ids_are_kept(self):
    # A resumed invocation has no USER_MESSAGE_RECEIVED of its own.
    rows = [
        _user(invocation="inv-1"),
        _llm("text: 'Need approval.'", invocation="inv-1"),
        _llm("None", invocation="inv-2"),
    ]
    assert self._session_trace(rows).final_response is None

  def test_trace_event_from_a_row_keeps_the_invocation_id(self):
    event = TraceEvent.from_bigquery_row(
        {"event_type": "LLM_RESPONSE", "invocation_id": "inv-7"}
    )
    assert event.invocation_id == "inv-7"


class TestFlag:
  """#595 item 5: missing and byte values of a BOOL column."""

  @pytest.mark.parametrize(
      "value, expected",
      [
          (float("nan"), False),
          (b"false", False),
          (b"true", True),
          (b"TRUE ", True),
          (1, True),
          (0.0, False),
      ],
  )
  def test_flag(self, value, expected):
    assert _flag(value) is expected

  def test_numpy_and_pandas_missing_values(self):
    np = pytest.importorskip("numpy")
    assert _flag(np.nan) is False
    assert _flag(np.datetime64("NaT", "ns")) is False
    assert _flag(np.True_) is True
    pd = pytest.importorskip("pandas")
    assert _flag(pd.NA) is False
    assert _flag(pd.NaT) is False
