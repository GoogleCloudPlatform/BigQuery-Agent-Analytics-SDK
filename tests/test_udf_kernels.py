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

"""Tests for UDF kernel functions.

Tests are organized in two sections:

1. **Direct kernel tests** — verify each pure function in isolation
   with typed scalar inputs, including edge cases. The ``udf_kernels``
   module powers the SQL-native UDF path in ``udf_sql_templates.py``
   and intentionally keeps the normalized ``1.0 - (observed / budget)``
   score for BigQuery SQL compatibility.
2. **Prebuilt divergence tests** — document that the Python
   ``CodeEvaluator.{latency, error_rate, ...}`` prebuilts *no longer*
   mirror the SQL kernel scores. They return a binary 1.0/0.0 gate
   against the raw observed value instead, so the same input yields
   different numeric scores via the two paths while the pass/fail
   outcome matches the user's budget intent.
"""

import pytest

from bigquery_agent_analytics.evaluators import CodeEvaluator
from bigquery_agent_analytics.udf_kernels import extract_response_text
from bigquery_agent_analytics.udf_kernels import is_error_event
from bigquery_agent_analytics.udf_kernels import score_cost
from bigquery_agent_analytics.udf_kernels import score_error_rate
from bigquery_agent_analytics.udf_kernels import score_latency
from bigquery_agent_analytics.udf_kernels import score_token_efficiency
from bigquery_agent_analytics.udf_kernels import score_ttft
from bigquery_agent_analytics.udf_kernels import score_turn_count
from bigquery_agent_analytics.udf_kernels import tool_outcome

# ------------------------------------------------------------------ #
# Event Semantics: is_error_event                                      #
# ------------------------------------------------------------------ #


class TestIsErrorEvent:

  def test_tool_error_type(self):
    assert is_error_event("TOOL_ERROR") is True

  def test_llm_error_type(self):
    assert is_error_event("LLM_ERROR") is True

  def test_error_status(self):
    assert is_error_event("LLM_REQUEST", status="ERROR") is True

  def test_error_message_present(self):
    assert is_error_event("LLM_REQUEST", error_message="oops") is True

  def test_normal_event(self):
    assert is_error_event("LLM_REQUEST") is False

  def test_ok_status_no_error(self):
    assert is_error_event("TOOL_COMPLETED", status="OK") is False

  def test_combined_signals(self):
    assert (
        is_error_event("TOOL_ERROR", error_message="x", status="ERROR") is True
    )


# ------------------------------------------------------------------ #
# Event Semantics: tool_outcome                                        #
# ------------------------------------------------------------------ #


class TestToolOutcome:

  def test_tool_completed(self):
    assert tool_outcome("TOOL_COMPLETED") == "success"

  def test_tool_error(self):
    assert tool_outcome("TOOL_ERROR") == "error"

  def test_error_status(self):
    assert tool_outcome("TOOL_COMPLETED", status="ERROR") == "error"

  def test_tool_starting(self):
    assert tool_outcome("TOOL_STARTING") == "in_progress"

  def test_unknown_type(self):
    assert tool_outcome("OTHER") == "in_progress"


# ------------------------------------------------------------------ #
# Event Semantics: extract_response_text                               #
# ------------------------------------------------------------------ #


class TestExtractResponseText:

  def test_response_key(self):
    assert extract_response_text('{"response": "hello"}') == "hello"

  def test_text_summary_key(self):
    assert extract_response_text('{"text_summary": "summary"}') == "summary"

  def test_text_key(self):
    assert extract_response_text('{"text": "body"}') == "body"

  def test_raw_key(self):
    assert extract_response_text('{"raw": "data"}') == "data"

  def test_priority_order(self):
    j = '{"text": "low", "response": "high"}'
    assert extract_response_text(j) == "high"

  def test_empty_dict(self):
    assert extract_response_text("{}") is None

  def test_none_input(self):
    assert extract_response_text(None) is None

  def test_empty_string(self):
    assert extract_response_text("") is None

  def test_invalid_json(self):
    assert extract_response_text("not json") == "not json"

  def test_non_dict_json(self):
    assert extract_response_text('"just a string"') == "just a string"

  def test_array_json(self):
    assert extract_response_text("[1,2,3]") == "[1, 2, 3]"


# ------------------------------------------------------------------ #
# Score Kernels: direct tests                                          #
# ------------------------------------------------------------------ #


class TestScoreLatency:

  def test_zero_latency(self):
    assert score_latency(0, 5000) == 1.0

  def test_negative_latency(self):
    assert score_latency(-100, 5000) == 1.0

  def test_at_threshold(self):
    assert score_latency(5000, 5000) == 0.0

  def test_over_threshold(self):
    assert score_latency(10000, 5000) == 0.0

  def test_half_threshold(self):
    assert score_latency(2500, 5000) == pytest.approx(0.5)

  def test_quarter_threshold(self):
    assert score_latency(1250, 5000) == pytest.approx(0.75)


class TestScoreErrorRate:

  def test_no_calls(self):
    assert score_error_rate(0, 0, 0.1) == 1.0

  def test_no_errors(self):
    assert score_error_rate(10, 0, 0.1) == 1.0

  def test_at_threshold(self):
    assert score_error_rate(10, 1, 0.1) == 0.0

  def test_over_threshold(self):
    assert score_error_rate(10, 5, 0.1) == 0.0

  def test_half_threshold(self):
    assert score_error_rate(100, 5, 0.1) == pytest.approx(0.5)


class TestScoreTurnCount:

  def test_zero_turns(self):
    assert score_turn_count(0, 10) == 1.0

  def test_negative_turns(self):
    assert score_turn_count(-1, 10) == 1.0

  def test_at_max(self):
    assert score_turn_count(10, 10) == 0.0

  def test_over_max(self):
    assert score_turn_count(20, 10) == 0.0

  def test_half_max(self):
    assert score_turn_count(5, 10) == pytest.approx(0.5)


class TestScoreTokenEfficiency:

  def test_zero_tokens(self):
    assert score_token_efficiency(0, 50000) == 1.0

  def test_negative_tokens(self):
    assert score_token_efficiency(-100, 50000) == 1.0

  def test_at_max(self):
    assert score_token_efficiency(50000, 50000) == 0.0

  def test_over_max(self):
    assert score_token_efficiency(100000, 50000) == 0.0

  def test_half_max(self):
    assert score_token_efficiency(25000, 50000) == pytest.approx(0.5)


class TestScoreTtft:

  def test_zero_ttft(self):
    assert score_ttft(0, 1000) == 1.0

  def test_negative_ttft(self):
    assert score_ttft(-50, 1000) == 1.0

  def test_at_threshold(self):
    assert score_ttft(1000, 1000) == 0.0

  def test_over_threshold(self):
    assert score_ttft(2000, 1000) == 0.0

  def test_half_threshold(self):
    assert score_ttft(500, 1000) == pytest.approx(0.5)


class TestScoreCost:

  def test_zero_tokens(self):
    assert score_cost(0, 0, 1.0) == 1.0

  def test_at_max_cost(self):
    # 1M input tokens * 0.00025/1k = 0.25
    # 200k output tokens * 0.00125/1k = 0.25
    # total = 0.50 → 0.50 < 0.50 is False → 0.0
    assert score_cost(1000000, 200000, 0.5) == 0.0

  def test_over_max_cost(self):
    assert score_cost(10000000, 10000000, 0.01) == 0.0

  def test_custom_pricing(self):
    # 1000 input * 0.001/1k = 0.001
    # 1000 output * 0.002/1k = 0.002
    # total = 0.003, max = 0.01 → 1 - 0.3 = 0.7
    assert score_cost(1000, 1000, 0.01, 0.001, 0.002) == pytest.approx(0.7)

  def test_default_pricing(self):
    # 10000 input * 0.00025/1k = 0.0025
    # 10000 output * 0.00125/1k = 0.0125
    # total = 0.015, max = 1.0 → 1 - 0.015 = 0.985
    assert score_cost(10000, 10000, 1.0) == pytest.approx(0.985)


# ------------------------------------------------------------------ #
# Prebuilt divergence: CodeEvaluator prebuilts are binary gates,
# ``udf_kernels`` stays on the normalized ``1.0 - observed/budget``.
# These two paths now intentionally disagree on the numeric score;
# they still agree on the user-intent boundary (observed <= budget).
# ------------------------------------------------------------------ #


class TestPrebuiltBinaryLatency:
  """CodeEvaluator.latency returns 1.0/0.0 against the raw budget."""

  @pytest.mark.parametrize(
      "avg,threshold,expected_score,expected_pass",
      [
          (0, 5000, 1.0, True),
          (2500, 5000, 1.0, True),  # old normalized impl: 0.5 -> fail
          (5000, 5000, 1.0, True),  # boundary inclusive
          (5001, 5000, 0.0, False),
          (10000, 5000, 0.0, False),
      ],
  )
  def test_binary(self, avg, threshold, expected_score, expected_pass):
    ev = CodeEvaluator.latency(threshold_ms=threshold)
    result = ev.evaluate_session({"session_id": "s1", "avg_latency_ms": avg})
    assert result.scores["latency"] == pytest.approx(expected_score)
    assert result.passed is expected_pass

  def test_sql_kernel_unchanged(self):
    """score_latency stays on the normalized score used by SQL UDFs."""
    assert score_latency(2500, 5000) == pytest.approx(0.5)
    assert score_latency(5000, 5000) == pytest.approx(0.0)


class TestPrebuiltBinaryErrorRate:

  @pytest.mark.parametrize(
      "calls,errors,max_rate,expected_score,expected_pass",
      [
          (0, 0, 0.1, 1.0, True),
          (10, 0, 0.1, 1.0, True),
          (10, 1, 0.1, 1.0, True),  # exactly at budget passes
          (10, 2, 0.1, 0.0, False),
          (100, 5, 0.1, 1.0, True),
          (50, 3, 0.2, 1.0, True),
      ],
  )
  def test_binary(self, calls, errors, max_rate, expected_score, expected_pass):
    ev = CodeEvaluator.error_rate(max_error_rate=max_rate)
    result = ev.evaluate_session(
        {"session_id": "s1", "tool_calls": calls, "tool_errors": errors}
    )
    assert result.scores["error_rate"] == pytest.approx(expected_score)
    assert result.passed is expected_pass


class TestPrebuiltBinaryTurnCount:

  @pytest.mark.parametrize(
      "turns,max_t,expected_score,expected_pass",
      [
          (0, 10, 1.0, True),
          (5, 10, 1.0, True),
          (10, 10, 1.0, True),
          (11, 10, 0.0, False),
          (20, 10, 0.0, False),
      ],
  )
  def test_binary(self, turns, max_t, expected_score, expected_pass):
    ev = CodeEvaluator.turn_count(max_turns=max_t)
    result = ev.evaluate_session({"session_id": "s1", "turn_count": turns})
    assert result.scores["turn_count"] == pytest.approx(expected_score)
    assert result.passed is expected_pass


class TestPrebuiltBinaryTokenEfficiency:

  @pytest.mark.parametrize(
      "tokens,max_t,expected_score,expected_pass",
      [
          (0, 50000, 1.0, True),
          (25000, 50000, 1.0, True),  # old normalized impl: 0.5 -> fail
          (50000, 50000, 1.0, True),
          (50001, 50000, 0.0, False),
          (100000, 50000, 0.0, False),
      ],
  )
  def test_binary(self, tokens, max_t, expected_score, expected_pass):
    ev = CodeEvaluator.token_efficiency(max_tokens=max_t)
    result = ev.evaluate_session({"session_id": "s1", "total_tokens": tokens})
    assert result.scores["token_efficiency"] == pytest.approx(expected_score)
    assert result.passed is expected_pass


class TestPrebuiltBinaryTtft:

  @pytest.mark.parametrize(
      "avg,threshold,expected_score,expected_pass",
      [
          (0, 1000, 1.0, True),
          (500, 1000, 1.0, True),  # old normalized impl: 0.5 -> fail
          (1000, 1000, 1.0, True),
          (1001, 1000, 0.0, False),
          (2000, 1000, 0.0, False),
      ],
  )
  def test_binary(self, avg, threshold, expected_score, expected_pass):
    ev = CodeEvaluator.ttft(threshold_ms=threshold)
    result = ev.evaluate_session({"session_id": "s1", "avg_ttft_ms": avg})
    assert result.scores["ttft"] == pytest.approx(expected_score)
    assert result.passed is expected_pass


class TestPrebuiltBinaryCost:

  @pytest.mark.parametrize(
      "inp,out,max_c,inp_rate,out_rate,expected_score,expected_pass",
      [
          # cost = 0.0, budget = 1.0 -> pass
          (0, 0, 1.0, 0.00025, 0.00125, 1.0, True),
          # cost ≈ 0.015, budget = 1.0 -> pass
          (10000, 10000, 1.0, 0.00025, 0.00125, 1.0, True),
          # cost = 0.5 exactly at budget -> pass (inclusive boundary)
          (1000000, 200000, 0.5, 0.00025, 0.00125, 1.0, True),
          # cost = 0.003, budget = 0.01 -> pass
          (1000, 1000, 0.01, 0.001, 0.002, 1.0, True),
          # cost = 2.0, budget = 0.01 -> fail
          (1000, 1000, 0.01, 1.0, 1.0, 0.0, False),
      ],
  )
  def test_binary(
      self,
      inp,
      out,
      max_c,
      inp_rate,
      out_rate,
      expected_score,
      expected_pass,
  ):
    ev = CodeEvaluator.cost_per_session(
        max_cost_usd=max_c,
        input_cost_per_1k=inp_rate,
        output_cost_per_1k=out_rate,
    )
    result = ev.evaluate_session(
        {"session_id": "s1", "input_tokens": inp, "output_tokens": out}
    )
    assert result.scores["cost"] == pytest.approx(expected_score)
    assert result.passed is expected_pass


# ------------------------------------------------------------------ #
# Parity: event semantics kernels vs event_semantics.py                #
# ------------------------------------------------------------------ #


class TestParityEventSemantics:
  """Prove UDF kernel event functions match event_semantics.py."""

  def test_is_error_matches(self):
    from bigquery_agent_analytics.event_semantics import is_error_event as orig

    cases = [
        ("TOOL_ERROR", None, "OK"),
        ("LLM_REQUEST", None, "OK"),
        ("LLM_REQUEST", "oops", "OK"),
        ("LLM_REQUEST", None, "ERROR"),
        ("TOOL_COMPLETED", None, "OK"),
    ]
    for et, em, st in cases:
      assert is_error_event(et, em, st) == orig(
          et, em, st
      ), f"Mismatch for ({et}, {em}, {st})"

  def test_tool_outcome_matches(self):
    from bigquery_agent_analytics.event_semantics import tool_outcome as orig

    cases = [
        ("TOOL_COMPLETED", "OK"),
        ("TOOL_ERROR", "OK"),
        ("TOOL_STARTING", "OK"),
        ("TOOL_COMPLETED", "ERROR"),
        ("OTHER", "OK"),
    ]
    for et, st in cases:
      assert tool_outcome(et, st) == orig(et, st), f"Mismatch for ({et}, {st})"

  def test_extract_response_text_matches(self):
    from bigquery_agent_analytics.event_semantics import extract_response_text as orig

    dicts = [
        {"response": "hello"},
        {"text_summary": "summary"},
        {"text": "body"},
        {"raw": "data"},
        {"text": "low", "response": "high"},
        {},
        {"unrelated": "value"},
    ]
    for d in dicts:
      import json

      assert extract_response_text(json.dumps(d)) == orig(
          d
      ), f"Mismatch for {d}"


# ------------------------------------------------------------------ #
# normalize_event_label                                                #
# ------------------------------------------------------------------ #


class TestNormalizeEventLabel:

  @pytest.mark.parametrize(
      "event_type,expected",
      [
          ("LLM_REQUEST", "llm"),
          ("LLM_RESPONSE", "llm"),
          ("TOOL_STARTING", "tool"),
          ("TOOL_COMPLETED", "tool"),
          ("TOOL_ERROR", "tool_error"),
          ("USER_MESSAGE_RECEIVED", "user"),
          ("AGENT_COMPLETED", "agent"),
          ("UNKNOWN", "other"),
          ("", "other"),
      ],
  )
  def test_label_mapping(self, event_type, expected):
    from bigquery_agent_analytics.udf_kernels import normalize_event_label

    assert normalize_event_label(event_type) == expected


# ------------------------------------------------------------------ #
# eval_summary_json                                                    #
# ------------------------------------------------------------------ #


class TestEvalSummaryJson:

  def test_returns_valid_json(self):
    import json

    from bigquery_agent_analytics.udf_kernels import eval_summary_json

    result = json.loads(
        eval_summary_json(
            2500.0,
            10,
            1,
            5,
            25000,
            500.0,
            10000,
            10000,
            5000.0,
            0.1,
            10,
            50000,
            1000.0,
            2.0,
        )
    )
    assert isinstance(result, dict)
    expected_keys = {
        "latency",
        "error_rate",
        "turn_count",
        "token_efficiency",
        "ttft",
        "cost",
        "passed",
    }
    assert set(result.keys()) == expected_keys

  def test_scores_match_individual_kernels(self):
    import json

    from bigquery_agent_analytics.udf_kernels import eval_summary_json

    result = json.loads(
        eval_summary_json(
            2500.0,
            10,
            1,
            5,
            25000,
            500.0,
            10000,
            10000,
            5000.0,
            0.1,
            10,
            50000,
            1000.0,
            2.0,
        )
    )
    assert result["latency"] == pytest.approx(score_latency(2500.0, 5000.0))
    assert result["error_rate"] == pytest.approx(score_error_rate(10, 1, 0.1))
    assert result["turn_count"] == pytest.approx(score_turn_count(5, 10))
    assert result["token_efficiency"] == pytest.approx(
        score_token_efficiency(25000, 50000)
    )
    assert result["ttft"] == pytest.approx(score_ttft(500.0, 1000.0))
    assert result["cost"] == pytest.approx(score_cost(10000, 10000, 2.0))

  def test_all_perfect_passes(self):
    import json

    from bigquery_agent_analytics.udf_kernels import eval_summary_json

    result = json.loads(
        eval_summary_json(
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            5000.0,
            0.1,
            10,
            50000,
            1000.0,
            2.0,
        )
    )
    assert result["passed"] is True
    for key in [
        "latency",
        "error_rate",
        "turn_count",
        "token_efficiency",
        "ttft",
        "cost",
    ]:
      assert result[key] == 1.0

  def test_all_worst_fails(self):
    import json

    from bigquery_agent_analytics.udf_kernels import eval_summary_json

    result = json.loads(
        eval_summary_json(
            99999.0,
            10,
            10,
            999,
            999999,
            99999.0,
            999999,
            999999,
            5000.0,
            0.1,
            10,
            50000,
            1000.0,
            0.01,
            0.001,
            0.002,
        )
    )
    assert result["passed"] is False
    for key in [
        "latency",
        "error_rate",
        "turn_count",
        "token_efficiency",
        "ttft",
        "cost",
    ]:
      assert result[key] == 0.0

  def test_partial_fail(self):
    import json

    from bigquery_agent_analytics.udf_kernels import eval_summary_json

    # latency at threshold → 0.0, everything else perfect
    result = json.loads(
        eval_summary_json(
            5000.0,
            0,
            0,
            0,
            0,
            0,
            0,
            0,
            5000.0,
            0.1,
            10,
            50000,
            1000.0,
            2.0,
        )
    )
    assert result["latency"] == 0.0
    assert result["passed"] is False
