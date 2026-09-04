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

"""PR123 metric evidence and main trace-identity compatibility regressions."""

from datetime import datetime
from datetime import timezone
import importlib
import json
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from bigquery_agent_analytics.performance_evaluator import EvalStatus
from bigquery_agent_analytics.performance_evaluator import PerformanceEvaluator
from bigquery_agent_analytics.performance_evaluator import SessionTrace
from bigquery_agent_analytics.trace import Span
from bigquery_agent_analytics.trace import Trace
from bigquery_agent_analytics.trace import TraceIdentity
from bigquery_agent_analytics.trace import TraceScope
from bigquery_agent_analytics.trace import TraceSelector


@pytest.fixture
def evaluator():
  return PerformanceEvaluator(
      project_id="p", dataset_id="d", client=MagicMock()
  )


def _session(response="wrong"):
  return SessionTrace(
      session_id="s1", user_id="u1", events=[], final_response=response
  )


def _judge(responses):
  client = MagicMock()
  client.aio.models.generate_content = AsyncMock(
      side_effect=[
          MagicMock(text=json.dumps(value))
          if not isinstance(value, Exception)
          else value
          for value in responses
      ]
  )
  return patch("google.genai.Client", return_value=client)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "actual, expected, score",
    [
        ("wrong", "right", 0.0),
        (" right ", "RIGHT", 1.0),
        (None, "right", 0.0),
        (None, "", 0.0),
        ("", "", 1.0),
    ],
)
async def test_golden_response_always_produces_deterministic_evidence(
    evaluator, actual, expected, score
):
  evaluator.get_session_trace = AsyncMock(return_value=_session(actual))
  result = await evaluator.evaluate_session("s1", golden_response=expected)
  assert result.scores == {"response_match": score}
  assert result.overall_score == score
  assert result.eval_status is (
      EvalStatus.PASSED if score else EvalStatus.FAILED
  )


@pytest.mark.asyncio
async def test_no_requested_metrics_never_pass(evaluator):
  result = await evaluator.evaluate_session("s1", trace=_session())
  assert result.eval_status is EvalStatus.FAILED
  assert result.scores == {}
  assert result.overall_score is None
  assert "No evaluation metrics" in result.details["errors"][0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"justification": "no scores"},
        {"sentiment": 10},
        {"sentiment": None, "hallucination": 10},
        {"sentiment": "10", "hallucination": 10},
        {"sentiment": True, "hallucination": 10},
        {"sentiment": float("nan"), "hallucination": 10},
        {"sentiment": 10, "hallucination": float("inf")},
        {"sentiment": -1, "hallucination": 10},
        {"sentiment": 11, "hallucination": 10},
        [10, 10],
        None,
    ],
)
async def test_invalid_one_sided_judges_fail_even_with_passing_trajectory(
    evaluator, payload
):
  with _judge([payload]):
    result = await evaluator.evaluate_session(
        "s1",
        trace=_session(),
        golden_trajectory=[],
        use_llm_judge=True,
        thresholds={"llm_judge_sentiment": 0.0, "llm_judge_hallucination": 0.0},
    )
  assert result.scores["trajectory_exact_match"] == 1.0
  assert "llm_judge_sentiment" not in result.scores
  assert "llm_judge_hallucination" not in result.scores
  assert result.eval_status is EvalStatus.FAILED
  assert "missing required metrics" in result.details["errors"][0]
  assert "failed" in result.llm_judge_feedback


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text", ["", "not json", '{"sentiment": 10,', "```json\n{wrong}\n```"]
)
async def test_malformed_judge_response_has_explicit_failure(evaluator, text):
  client = MagicMock()
  client.aio.models.generate_content = AsyncMock(
      return_value=MagicMock(text=text)
  )
  with patch("google.genai.Client", return_value=client):
    result = await evaluator.evaluate_session(
        "s1", trace=_session(), use_llm_judge=True
    )
  assert result.eval_status is EvalStatus.FAILED
  assert result.scores == {}
  assert result.details["errors"]
  assert "failed" in result.llm_judge_feedback


@pytest.mark.asyncio
async def test_model_failure_does_not_become_empty_success(evaluator):
  with _judge([RuntimeError("fixture model unavailable")]):
    result = await evaluator.evaluate_session(
        "s1", trace=_session(), use_llm_judge=True
    )
  assert result.eval_status is EvalStatus.FAILED
  assert result.scores == {}
  assert result.details["errors"]
  assert "fixture model unavailable" in result.llm_judge_feedback


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "key, bad",
    [
        ("final_answer_correct", None),
        ("tool_usage_correct", float("nan")),
        ("sound_reasoning", 2),
        ("efficiency", 0.5),
        ("efficiency", "1"),
    ],
)
async def test_invalid_side_by_side_result_cannot_hide_behind_valid_metrics(
    evaluator, key, bad
):
  payload = dict(
      final_answer_correct=1,
      tool_usage_correct=1,
      sound_reasoning=1,
      efficiency=1,
  )
  if bad is None:
    del payload[key]
  else:
    payload[key] = bad
  with _judge([dict(sentiment=10, hallucination=10), payload]):
    result = await evaluator.evaluate_session(
        "s1",
        trace=_session("right"),
        golden_response="right",
        use_llm_judge=True,
    )
  assert result.scores["response_match"] == 1.0
  assert result.scores["llm_judge_sentiment"] == 1.0
  assert "llm_judge_correctness" not in result.scores
  assert result.eval_status is EvalStatus.FAILED
  assert result.details["errors"]
  assert "Side-by-side LLM evaluation failed" in result.llm_judge_feedback


@pytest.mark.asyncio
async def test_both_authored_rubrics_keep_their_scales(evaluator):
  with _judge(
      [
          dict(sentiment=1, hallucination=9, justification="one sided"),
          dict(
              final_answer_correct=1,
              tool_usage_correct=1,
              sound_reasoning=1,
              efficiency=0,
              justification="side by side",
          ),
      ]
  ):
    result = await evaluator.evaluate_session(
        "s1",
        trace=_session("right"),
        golden_response="right",
        use_llm_judge=True,
    )
  assert result.scores["llm_judge_sentiment"] == 0.1
  assert result.scores["llm_judge_hallucination"] == 0.9
  assert result.scores["llm_judge_correctness"] == 1.0
  assert result.scores["llm_judge_efficiency"] == 0.0
  assert result.eval_status is EvalStatus.FAILED
  assert "errors" not in result.details
  assert result.llm_judge_feedback == "one sided\nside by side"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload", [{}, {"quality": float("nan")}, {"quality": 11}]
)
async def test_invalid_custom_rubric_fails_even_with_zero_threshold(
    evaluator, payload
):
  evaluator.add_rubric(
      "quality", "Score {final_response}", "quality", threshold=0.0
  )
  with _judge([dict(sentiment=10, hallucination=10), payload]):
    result = await evaluator.evaluate_session(
        "s1", trace=_session(), use_llm_judge=True
    )
  assert result.eval_status is EvalStatus.FAILED
  assert "quality" not in result.scores
  assert any(
      "Custom rubric quality failed" in error
      for error in result.details["errors"]
  )


@pytest.mark.asyncio
async def test_rubric_threshold_and_valid_score_are_applied(evaluator):
  evaluator.add_rubric(
      "quality", "Score {final_response}", "quality", threshold=0.9
  )
  with _judge(
      [
          dict(sentiment=10, hallucination=10),
          dict(quality=8, justification="partial"),
      ]
  ):
    result = await evaluator.evaluate_session(
        "s1", trace=_session(), use_llm_judge=True
    )
  assert result.scores["quality"] == 0.8
  assert result.eval_status is EvalStatus.FAILED
  assert result.details["quality_threshold"] == 0.9


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [float("nan"), float("inf")])
async def test_nonfinite_custom_metric_cannot_pass(evaluator, value):
  result = await evaluator.evaluate_session(
      "s1", trace=_session(), custom_metrics={"custom": lambda *args: value}
  )
  assert result.eval_status is EvalStatus.FAILED
  assert result.scores == {}
  assert "finite" in result.details["errors"][0]


@pytest.mark.asyncio
async def test_provided_scoped_trace_is_evaluated_without_another_read(
    evaluator,
):
  identity = TraceIdentity(
      session_id="s1", user_id="alice", root_agent_name="root"
  )
  scope = TraceScope(experiment_id="exp", custom_labels={"run": "v1"})
  trace = Trace(
      trace_id="trace1",
      session_id="s1",
      identity=identity,
      scope=scope,
      scope_coverage=(scope.scope_signature,),
      spans=[
          Span(
              event_type="LLM_RESPONSE",
              agent="root",
              timestamp=datetime.now(timezone.utc),
              content={"response": "right"},
              attributes={},
              span_id="span1",
          )
      ],
  )
  evaluator.get_session_trace = AsyncMock(
      side_effect=AssertionError("must not requery")
  )
  evaluator.get_trace_by_selector = AsyncMock(
      side_effect=AssertionError("must not requery")
  )
  observed = []

  def metric(converted, *args):
    observed.append(converted)
    converted.events[0].content["response"] = "modified copy"
    return 1.0

  result = await evaluator.evaluate_session(
      "s1",
      trace=trace,
      golden_response="right",
      custom_metrics={"copy_check": metric},
  )
  assert result.eval_status is EvalStatus.PASSED
  assert result.details["user_id"] == "alice"
  assert result.details["root_agent_name"] == "root"
  assert result.details["experiment_id"] == "exp"
  assert result.details["scope_signature"] == scope.scope_signature
  assert observed[0].scope_coverage == (scope.scope_signature,)
  assert trace.spans[0].content["response"] == "right"
  evaluator.get_session_trace.assert_not_awaited()
  evaluator.get_trace_by_selector.assert_not_awaited()


@pytest.mark.asyncio
async def test_materialized_trace_rejects_ambiguous_or_mismatched_parameters(
    evaluator,
):
  with pytest.raises(ValueError, match="trace.session_id"):
    await evaluator.evaluate_session("other", trace=_session())
  with pytest.raises(ValueError, match="either trace or selector"):
    await evaluator.evaluate_session(
        "s1", trace=_session(), selector=TraceSelector(session_id="s1")
    )


def test_trace_shim_preserves_public_and_private_exports():
  current = importlib.import_module(
      "bigquery_agent_analytics.performance_evaluator"
  )
  legacy = importlib.import_module("bigquery_agent_analytics.trace_evaluator")
  assert legacy is current
  for name in (
      "BigQueryTraceEvaluator",
      "SessionTrace",
      "TraceEvent",
      "ToolCall",
      "EvalStatus",
      "MatchType",
      "EvaluationResult",
      "TraceReplayRunner",
      "ReplayContext",
      "TraceIdentity",
      "TraceScope",
      "TraceSelector",
      "_extract_json_from_text",
      "_parse_json_from_text",
      "strip_markdown_fences",
  ):
    assert getattr(legacy, name) is getattr(current, name)
  assert current.BigQueryTraceEvaluator is PerformanceEvaluator


@pytest.mark.asyncio
async def test_custom_rubric_bottom_score_is_not_perfect(evaluator):
  evaluator.add_rubric(
      "quality", "Score {final_response}", "quality", threshold=0.8
  )
  with _judge([dict(sentiment=10, hallucination=10), dict(quality=1)]):
    result = await evaluator.evaluate_session(
        "s1", trace=_session(), use_llm_judge=True
    )
  assert result.scores["quality"] == 0.1
  assert result.eval_status is EvalStatus.FAILED
  assert result.details["quality_threshold"] == 0.8
