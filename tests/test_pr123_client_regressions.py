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

"""Client dispatch regressions with offline, identity-bound evaluation data."""

import asyncio
from datetime import datetime
from datetime import timezone
import json
from unittest.mock import AsyncMock
from unittest.mock import MagicMock

import pytest

from bigquery_agent_analytics.client import Client
from bigquery_agent_analytics.evaluators import LLMAsJudge
from bigquery_agent_analytics.performance_evaluator import EvalStatus
from bigquery_agent_analytics.performance_evaluator import EvaluationResult
from bigquery_agent_analytics.performance_evaluator import PerformanceEvaluator
from bigquery_agent_analytics.system_evaluator import SessionScore
from bigquery_agent_analytics.trace import Trace
from bigquery_agent_analytics.trace import TraceFilter
from bigquery_agent_analytics.trace import TraceIdentity
from bigquery_agent_analytics.trace import TraceScope


def _client(bq_client=None):
  return Client(
      project_id="caller-project",
      dataset_id="caller_dataset",
      table_id="default_events",
      verify_schema=False,
      bq_client=bq_client if bq_client is not None else MagicMock(),
  )


def _trace(sid="shared-session", user="user-a", experiment="run-a"):
  return Trace(
      trace_id=f"trace-{user}-{experiment}",
      session_id=sid,
      identity=TraceIdentity(sid, user, "support"),
      scope=TraceScope(experiment_id=experiment),
  )


def _performance():
  return PerformanceEvaluator(
      project_id="evaluator-project",
      dataset_id="evaluator_dataset",
      table_id="other_events",
  )


@pytest.mark.parametrize("kind", ["performance", "legacy"])
def test_strict_mode_is_applied_through_public_dispatch(monkeypatch, kind):
  client = _client()
  trace = _trace()
  monkeypatch.setattr(
      client, "_fetch_filtered_traces", lambda **kwargs: [trace]
  )
  if kind == "performance":
    evaluator = _performance()
    result = EvaluationResult(
        session_id="incorrect-evaluator-id",
        eval_status=EvalStatus.PASSED,
        scores={},
        details={"user_id": "spoofed"},
    )
  else:
    evaluator = LLMAsJudge.correctness()
    result = SessionScore(
        session_id="incorrect-evaluator-id",
        scores={},
        passed=True,
        details={"user_id": "spoofed"},
    )
  monkeypatch.setattr(
      evaluator, "evaluate_session", AsyncMock(return_value=result)
  )

  report = client.evaluate(evaluator=evaluator, strict=True)

  assert report.total_sessions == 1
  assert report.passed_sessions == 0
  assert report.failed_sessions == 1
  score = report.session_scores[0]
  assert score.passed is False
  assert score.details["parse_error"] is True
  assert score.session_id == trace.session_id
  assert score.details["user_id"] == trace.identity.user_id
  assert score.details["root_agent_name"] == "support"
  assert score.details["scope_signature"] == trace.scope.scope_signature
  assert report.details["parse_errors"] == 1
  assert report.details["parse_error_rate"] == 1.0
  assert report.aggregate_scores == {}
  assert result.details == {"user_id": "spoofed"}


@pytest.mark.parametrize("kind", ["performance", "legacy"])
def test_per_session_errors_do_not_abort_bounded_evaluation(monkeypatch, kind):
  client = _client()
  traces = [_trace(sid=f"session-{index}") for index in range(13)]
  monkeypatch.setattr(client, "_fetch_filtered_traces", lambda **kwargs: traces)
  evaluator = (
      _performance() if kind == "performance" else LLMAsJudge.correctness()
  )
  active = 0
  peak = 0
  completed = 0

  async def evaluate_session(*args, **kwargs):
    nonlocal active, peak, completed
    active += 1
    peak = max(peak, active)
    call_number = completed
    completed += 1
    try:
      await asyncio.sleep(0.005)
      if call_number == 0:
        raise RuntimeError("one unavailable judge response")
      if kind == "performance":
        return EvaluationResult(
            session_id=kwargs["session_id"],
            eval_status=EvalStatus.PASSED,
            scores={"correctness": 0.75},
        )
      return SessionScore(
          session_id="assigned-by-client", scores={"correctness": 0.75}
      )
    finally:
      active -= 1

  monkeypatch.setattr(evaluator, "evaluate_session", evaluate_session)
  report = client.evaluate(evaluator=evaluator, strict=True)

  assert peak == 5
  assert completed == 13
  assert report.total_sessions == 13
  assert report.passed_sessions == 12
  assert report.failed_sessions == 1
  assert report.aggregate_scores == {"correctness": 0.75}
  assert [score.session_id for score in report.session_scores] == [
      trace.session_id for trace in traces
  ]
  failed = report.session_scores[0]
  assert "unavailable" in failed.details["evaluation_error"]
  assert failed.details["parse_error"] is True
  assert failed.details["user_id"] == "user-a"


def test_performance_aggregates_keep_colliding_identities_and_scopes(
    monkeypatch,
):
  client = _client()
  traces = [_trace(user="user-a"), _trace(user="user-b", experiment="run-b")]
  monkeypatch.setattr(client, "_fetch_filtered_traces", lambda **kwargs: traces)
  evaluator = _performance()

  async def evaluate_session(*, session_id, trace, **kwargs):
    return EvaluationResult(
        session_id=session_id,
        eval_status=EvalStatus.PASSED,
        scores={"quality": 0.5 if trace.identity.user_id == "user-a" else 1.0},
        details={"root_agent_name": "wrong", "scope_signature": "wrong"},
    )

  monkeypatch.setattr(evaluator, "evaluate_session", evaluate_session)
  report = client.evaluate(evaluator=evaluator)

  assert report.total_sessions == 2
  assert report.aggregate_scores == {"quality": 0.75}
  assert {score.details["user_id"] for score in report.session_scores} == {
      "user-a",
      "user-b",
  }
  assert {
      score.details["scope_signature"] for score in report.session_scores
  } == {trace.scope.scope_signature for trace in traces}


def test_performance_uses_caller_table_client_and_materialized_scope(
    monkeypatch,
):
  caller_bq = MagicMock()
  row = {
      "event_type": "AGENT_COMPLETED",
      "session_id": "shared-session",
      "user_id": "user-a",
      "trace_id": "trace-a",
      "span_id": "span-a",
      "invocation_id": "invocation-a",
      "parent_span_id": None,
      "timestamp": datetime(2026, 1, 1, tzinfo=timezone.utc),
      "agent": "support",
      "content": json.dumps({"response": "selected answer"}),
      "content_parts": [],
      "attributes": json.dumps(
          {
              "root_agent_name": "support",
              "experiment_id": "run-a",
              "custom_tags": {"cohort": "selected"},
          }
      ),
      "status": "OK",
      "error_message": None,
      "latency_ms": None,
      "is_truncated": False,
  }
  caller_bq.query.return_value.result.return_value = [row]
  client = _client(caller_bq)
  evaluator = _performance()
  read = AsyncMock(
      side_effect=AssertionError("must not re-query by session id")
  )
  monkeypatch.setattr(evaluator, "get_session_trace", read)
  judge = AsyncMock(
      return_value=(
          {"llm_judge_sentiment": 1.0, "llm_judge_hallucination": 1.0},
          "selected trace judged",
      )
  )
  monkeypatch.setattr(evaluator, "llm_judge_evaluate", judge)

  report = client.evaluate(
      evaluator=evaluator,
      dataset="custom_events",
      filters=TraceFilter(
          user_id="user-a",
          experiment_id="run-a",
          custom_labels={"cohort": "selected"},
          limit=1,
      ),
  )

  assert report.total_sessions == 1
  assert report.passed_sessions == 1
  assert report.dataset.startswith(
      "caller-project.caller_dataset.custom_events"
  )
  assert (
      "`caller-project.caller_dataset.custom_events`"
      in caller_bq.query.call_args.args[0]
  )
  assert "IS NOT DISTINCT FROM" in caller_bq.query.call_args.args[0]
  assert "$.custom_tags." in caller_bq.query.call_args.args[0]
  read.assert_not_awaited()
  judge.assert_awaited_once()
  judged = judge.await_args.kwargs["trace"]
  assert judged.session_id == "shared-session"
  assert judged.final_response == "selected answer"
  assert report.session_scores[0].details["user_id"] == "user-a"
  assert evaluator.project_id == "evaluator-project"
  assert evaluator.dataset_id == "evaluator_dataset"
  assert evaluator.table_id == "other_events"


@pytest.mark.parametrize("value", [0, -1, True, 1.5])
def test_invalid_concurrency_fails_before_query(value):
  bq = MagicMock()
  with pytest.raises(ValueError, match="max_concurrency"):
    _client(bq).evaluate(LLMAsJudge.correctness(), max_concurrency=value)
  bq.query.assert_not_called()


def test_filter_snapshot_survives_mutation_during_fetch(monkeypatch):
  client = _client()
  filters = TraceFilter(experiment_id="selected", limit=1)
  observed = {}

  def fetch(**kwargs):
    filters.experiment_id = "mutated"
    filters.limit = 100
    observed.update(kwargs)
    return []

  monkeypatch.setattr(client, "_fetch_filtered_traces", fetch)
  client.evaluate(evaluator=_performance(), filters=filters)

  assert observed["limit"] == 1
  assert "selected" in [param.value for param in observed["params"]]
  assert "mutated" not in [param.value for param in observed["params"]]
