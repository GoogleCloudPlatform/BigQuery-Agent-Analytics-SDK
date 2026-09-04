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
import warnings

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


def _client(bq_client=None, **kwargs):
  return Client(
      project_id="caller-project",
      dataset_id="caller_dataset",
      table_id="default_events",
      verify_schema=False,
      bq_client=bq_client if bq_client is not None else MagicMock(),
      **kwargs,
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


def _force_legacy_api(client, monkeypatch):
  monkeypatch.setattr(
      client,
      "_ai_generate_judge",
      MagicMock(side_effect=RuntimeError("AI unavailable")),
  )
  monkeypatch.setattr(
      client,
      "_bqml_judge",
      MagicMock(side_effect=RuntimeError("BQML unavailable")),
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
    _force_legacy_api(client, monkeypatch)
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
  if kind == "legacy":
    _force_legacy_api(client, monkeypatch)
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
  if kind == "legacy":
    assert report.details["execution_mode"] == "api_fallback"
    assert "AI unavailable" in report.details["fallback_reason"]
    assert "BQML unavailable" in report.details["fallback_reason"]


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


def test_legacy_public_dispatch_uses_configured_bigquery_endpoint(monkeypatch):
  bq = MagicMock()
  bq.query.return_value.result.return_value = [
      {"session_id": "session-a", "score": 8, "justification": "measured"},
      {"session_id": "session-b", "score": None, "justification": "empty"},
  ]
  client = _client(
      bq,
      endpoint="gemini-2.5-pro",
      connection_id="caller-project.us.judge_connection",
  )
  judge = LLMAsJudge.correctness(threshold=0.7)
  api = AsyncMock(
      side_effect=AssertionError("BigQuery success must not use API")
  )
  monkeypatch.setattr(judge, "evaluate_session", api)

  with warnings.catch_warnings():
    warnings.simplefilter("error", DeprecationWarning)
    report = client.evaluate(
        judge,
        dataset="selected_events",
        filters=TraceFilter(user_id="selected-user", limit=2),
        strict=True,
    )

  assert bq.query.call_count == 1
  sql = bq.query.call_args.args[0]
  assert "AI.GENERATE(" in sql
  assert "endpoint => 'gemini-2.5-pro'" in sql
  assert "connection_id => 'caller-project.us.judge_connection'" in sql
  assert "`caller-project.caller_dataset.selected_events`" in sql
  params = bq.query.call_args.kwargs["job_config"].query_parameters
  assert any(
      param.name == "user_id" and param.value == "selected-user"
      for param in params
  )
  assert report.dataset.startswith(
      "caller-project.caller_dataset.selected_events"
  )
  assert report.details["execution_mode"] == "ai_generate"
  assert "fallback_reason" not in report.details
  assert report.details["parse_errors"] == 1
  assert report.passed_sessions == 1
  assert report.failed_sessions == 1
  assert report.aggregate_scores == {"correctness": 0.8}
  api.assert_not_awaited()


@pytest.mark.parametrize("legacy_model", [False, True])
def test_legacy_public_dispatch_keeps_bqml_success(monkeypatch, legacy_model):
  bq = MagicMock()
  job = MagicMock()
  job.result.return_value = [
      {
          "session_id": "session-a",
          "evaluation": '{"correctness": 9, "justification": "BQML"}',
      }
  ]
  if legacy_model:
    endpoint = "models-project.models.explicit_model"
    bq.query.return_value = job
  else:
    endpoint = "gemini-2.5-flash"
    bq.query.side_effect = [RuntimeError("AI tier unavailable"), job]
  client = _client(bq, endpoint=endpoint)
  judge = LLMAsJudge.correctness()
  api = AsyncMock(side_effect=AssertionError("BQML success must not use API"))
  monkeypatch.setattr(judge, "evaluate_session", api)

  report = client.evaluate(judge, dataset="selected_events")

  assert bq.query.call_count == (1 if legacy_model else 2)
  sql = bq.query.call_args.args[0]
  assert "ML.GENERATE_TEXT" in sql
  model = (
      endpoint
      if legacy_model
      else "caller-project.caller_dataset.gemini_text_model"
  )
  assert f"MODEL `{model}`" in sql
  assert "`caller-project.caller_dataset.selected_events`" in sql
  labels = bq.query.call_args.kwargs["job_config"].labels
  assert labels["sdk_ai_function"] == "ml-generate-text"
  assert report.details["execution_mode"] == "ml_generate_text"
  assert report.dataset.startswith(
      "caller-project.caller_dataset.selected_events"
  )
  assert report.aggregate_scores == {"correctness": 0.9}
  if legacy_model:
    assert "fallback_reason" not in report.details
  else:
    assert (
        "ai_generate: AI tier unavailable" == report.details["fallback_reason"]
    )
  api.assert_not_awaited()


def test_legacy_empty_criteria_keeps_selected_table_without_queries():
  bq = MagicMock()
  client = _client(bq)

  report = client.evaluate(LLMAsJudge(name="empty"), dataset="selected_events")

  assert (
      report.dataset
      == "caller-project.caller_dataset.selected_events WHERE TRUE"
  )
  assert report.details["execution_mode"] == "no_op"
  assert report.total_sessions == 0
  bq.query.assert_not_called()


def test_legacy_api_fallback_keeps_scope_and_concurrency_override(monkeypatch):
  bq = MagicMock()
  bq.query.side_effect = [
      RuntimeError("AI disabled"),
      RuntimeError("BQML disabled"),
  ]
  client = _client(bq)
  filters = TraceFilter(
      experiment_id="selected-run", custom_labels={"cohort": "chosen"}, limit=3
  )
  scope = TraceScope("selected-run", {"cohort": "chosen"})
  traces = [
      Trace(
          trace_id=f"trace-{user}",
          session_id="shared-id",
          identity=TraceIdentity("shared-id", user, "support"),
          scope=scope,
      )
      for user in ("user-a", "user-b", "user-c")
  ]
  fetched = {}

  def fetch(**kwargs):
    filters.limit = 999
    filters.experiment_id = "mutated"
    fetched.update(kwargs)
    return traces

  monkeypatch.setattr(client, "_fetch_filtered_traces", fetch)
  evaluator = LLMAsJudge.correctness()
  active = 0
  peak = 0

  async def evaluate(*args):
    nonlocal active, peak
    active += 1
    peak = max(peak, active)
    try:
      await asyncio.sleep(0.005)
      return SessionScore(session_id="ignored", scores={"correctness": 1.0})
    finally:
      active -= 1

  monkeypatch.setattr(evaluator, "evaluate_session", evaluate)
  report = client.evaluate(
      evaluator, filters=filters, dataset="selected_events", max_concurrency=1
  )

  assert peak == 1
  assert fetched["table"] == "selected_events"
  assert fetched["limit"] == 3
  assert "$.custom_tags." in fetched["row_where"]
  values = [param.value for param in fetched["params"]]
  assert "selected-run" in values
  assert "mutated" not in values
  assert all(fetched["scope_predicate"](trace.scope) for trace in traces)
  assert {score.details["user_id"] for score in report.session_scores} == {
      "user-a",
      "user-b",
      "user-c",
  }
  assert all(
      score.details["scope_signature"] == scope.scope_signature
      for score in report.session_scores
  )
  assert report.details["execution_mode"] == "api_fallback"
  assert (
      report.details["fallback_reason"]
      == "ai_generate: AI disabled; ml_generate_text: BQML disabled"
  )
  assert report.aggregate_scores == {"correctness": 1.0}


def test_performance_does_not_enter_legacy_bigquery_tiers(monkeypatch):
  client = _client(endpoint="models-project.models.explicit_model")
  monkeypatch.setattr(
      client, "_fetch_filtered_traces", lambda **kwargs: [_trace()]
  )
  ai = MagicMock(side_effect=AssertionError("legacy AI tier must not run"))
  bqml = MagicMock(side_effect=AssertionError("legacy BQML tier must not run"))
  monkeypatch.setattr(client, "_ai_generate_judge", ai)
  monkeypatch.setattr(client, "_bqml_judge", bqml)
  evaluator = _performance()
  monkeypatch.setattr(
      evaluator,
      "evaluate_session",
      AsyncMock(
          return_value=EvaluationResult(
              session_id="shared-session",
              eval_status=EvalStatus.PASSED,
              scores={"quality": 1.0},
          )
      ),
  )

  report = client.evaluate(evaluator)

  assert report.details["execution_mode"] == "performance_evaluator"
  assert report.passed_sessions == 1
  ai.assert_not_called()
  bqml.assert_not_called()
