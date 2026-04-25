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

"""Tests for the SDK Client class."""

from datetime import datetime
from datetime import timezone
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from bigquery_agent_analytics.categorical_evaluator import CategoricalEvaluationConfig
from bigquery_agent_analytics.categorical_evaluator import CategoricalMetricCategory
from bigquery_agent_analytics.categorical_evaluator import CategoricalMetricDefinition
from bigquery_agent_analytics.client import Client
from bigquery_agent_analytics.evaluators import CodeEvaluator
from bigquery_agent_analytics.evaluators import EvaluationReport
from bigquery_agent_analytics.trace import TraceFilter


def _mock_bq_client():
  """Creates a mock BigQuery client."""
  return MagicMock()


def _make_event_rows(n=3, session_id="sess-1"):
  """Creates mock BigQuery result rows for events."""
  ts = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
  events = [
      {
          "event_type": "USER_MESSAGE_RECEIVED",
          "agent": None,
          "timestamp": ts,
          "session_id": session_id,
          "invocation_id": "inv-1",
          "user_id": "user-1",
          "trace_id": "trace-1",
          "span_id": "s1",
          "parent_span_id": None,
          "content": '{"text_summary": "Hello"}',
          "content_parts": [],
          "attributes": "{}",
          "latency_ms": None,
          "status": "OK",
          "error_message": None,
          "is_truncated": False,
      },
      {
          "event_type": "TOOL_STARTING",
          "agent": "my_agent",
          "timestamp": ts,
          "session_id": session_id,
          "invocation_id": "inv-1",
          "user_id": "user-1",
          "trace_id": "trace-1",
          "span_id": "s2",
          "parent_span_id": "s1",
          "content": '{"tool": "search", "args": {"q": "hi"}}',
          "content_parts": [],
          "attributes": "{}",
          "latency_ms": None,
          "status": "OK",
          "error_message": None,
          "is_truncated": False,
      },
      {
          "event_type": "AGENT_COMPLETED",
          "agent": "my_agent",
          "timestamp": ts,
          "session_id": session_id,
          "invocation_id": "inv-1",
          "user_id": "user-1",
          "trace_id": "trace-1",
          "span_id": "s3",
          "parent_span_id": "s1",
          "content": '{"response": "Hello!"}',
          "content_parts": [],
          "attributes": "{}",
          "latency_ms": '{"total_ms": 250}',
          "status": "OK",
          "error_message": None,
          "is_truncated": False,
      },
  ]
  return events[:n]


def _make_mock_row(data):
  """Creates a mock BQ row that supports dict(row) and row.get()."""
  mock = MagicMock()
  mock.__iter__ = MagicMock(return_value=iter(data.items()))
  mock.get = data.get
  mock.keys = data.keys
  mock.values = data.values
  mock.items = data.items
  # Support dict() conversion
  mock.__getitem__ = lambda self, k: data[k]
  return mock


class TestClientInit:
  """Tests for Client initialization."""

  def test_init_skip_verify(self):
    mock_bq = _mock_bq_client()
    client = Client(
        project_id="proj",
        dataset_id="ds",
        table_id="events",
        verify_schema=False,
        bq_client=mock_bq,
    )
    assert client.project_id == "proj"
    assert client.dataset_id == "ds"
    assert client.table_id == "events"
    mock_bq.query.assert_not_called()

  def test_init_with_verify(self):
    mock_bq = _mock_bq_client()
    # Mock schema query result
    mock_rows = [
        _make_mock_row(
            {
                "column_name": "timestamp",
                "data_type": "TIMESTAMP",
            }
        ),
        _make_mock_row(
            {
                "column_name": "event_type",
                "data_type": "STRING",
            }
        ),
        _make_mock_row(
            {
                "column_name": "session_id",
                "data_type": "STRING",
            }
        ),
        _make_mock_row(
            {
                "column_name": "content",
                "data_type": "JSON",
            }
        ),
    ]
    mock_job = MagicMock()
    mock_job.result.return_value = mock_rows
    mock_bq.query.return_value = mock_job

    client = Client(
        project_id="proj",
        dataset_id="ds",
        verify_schema=True,
        bq_client=mock_bq,
    )
    mock_bq.query.assert_called_once()


class TestClientGetTrace:
  """Tests for Client.get_trace()."""

  def test_get_trace_success(self):
    mock_bq = _mock_bq_client()
    rows = [_make_mock_row(r) for r in _make_event_rows()]
    mock_job = MagicMock()
    mock_job.result.return_value = rows
    mock_bq.query.return_value = mock_job

    client = Client(
        project_id="proj",
        dataset_id="ds",
        verify_schema=False,
        bq_client=mock_bq,
    )
    trace = client.get_trace("trace-1")

    assert trace.trace_id == "trace-1"
    assert trace.session_id == "sess-1"
    assert len(trace.spans) == 3
    assert trace.user_id == "user-1"

  def test_get_trace_not_found(self):
    mock_bq = _mock_bq_client()
    mock_job = MagicMock()
    mock_job.result.return_value = []
    mock_bq.query.return_value = mock_job

    client = Client(
        project_id="proj",
        dataset_id="ds",
        verify_schema=False,
        bq_client=mock_bq,
    )

    with pytest.raises(ValueError, match="No events found"):
      client.get_trace("nonexistent")


class TestClientListTraces:
  """Tests for Client.list_traces()."""

  def test_list_traces_groups_by_session(self):
    mock_bq = _mock_bq_client()
    rows_s1 = [_make_mock_row(r) for r in _make_event_rows(2, "s1")]
    rows_s2 = [_make_mock_row(r) for r in _make_event_rows(2, "s2")]
    mock_job = MagicMock()
    mock_job.result.return_value = rows_s1 + rows_s2
    mock_bq.query.return_value = mock_job

    client = Client(
        project_id="proj",
        dataset_id="ds",
        verify_schema=False,
        bq_client=mock_bq,
    )
    traces = client.list_traces()

    assert len(traces) == 2
    session_ids = {t.session_id for t in traces}
    assert "s1" in session_ids
    assert "s2" in session_ids

  def test_list_traces_with_filter(self):
    mock_bq = _mock_bq_client()
    mock_job = MagicMock()
    mock_job.result.return_value = []
    mock_bq.query.return_value = mock_job

    client = Client(
        project_id="proj",
        dataset_id="ds",
        verify_schema=False,
        bq_client=mock_bq,
    )
    filt = TraceFilter(agent_id="my_agent", has_error=True)
    traces = client.list_traces(filter_criteria=filt)

    assert traces == []
    # Verify query was called
    mock_bq.query.assert_called_once()


class TestClientEvaluate:
  """Tests for Client.evaluate()."""

  def test_evaluate_code_evaluator(self):
    mock_bq = _mock_bq_client()
    # Mock session summary results
    summary_rows = [
        _make_mock_row(
            {
                "session_id": "s1",
                "total_events": 10,
                "tool_calls": 3,
                "tool_errors": 0,
                "llm_calls": 2,
                "avg_latency_ms": 1500.0,
                "max_latency_ms": 3000.0,
                "total_latency_ms": 5000.0,
                "turn_count": 2,
                "has_error": False,
            }
        ),
        _make_mock_row(
            {
                "session_id": "s2",
                "total_events": 20,
                "tool_calls": 5,
                "tool_errors": 3,
                "llm_calls": 4,
                "avg_latency_ms": 8000.0,
                "max_latency_ms": 15000.0,
                "total_latency_ms": 40000.0,
                "turn_count": 6,
                "has_error": True,
            }
        ),
    ]
    mock_job = MagicMock()
    mock_job.result.return_value = summary_rows
    mock_bq.query.return_value = mock_job

    client = Client(
        project_id="proj",
        dataset_id="ds",
        verify_schema=False,
        bq_client=mock_bq,
    )

    evaluator = CodeEvaluator.latency(threshold_ms=5000)
    report = client.evaluate(evaluator=evaluator)

    assert isinstance(report, EvaluationReport)
    assert report.total_sessions == 2
    assert report.evaluator_name == "latency_evaluator"
    # s1 should pass (1500ms < 5000ms), s2 should fail
    assert report.passed_sessions >= 1

  def test_evaluate_invalid_evaluator(self):
    mock_bq = _mock_bq_client()
    client = Client(
        project_id="proj",
        dataset_id="ds",
        verify_schema=False,
        bq_client=mock_bq,
    )

    with pytest.raises(TypeError, match="Unsupported"):
      client.evaluate(evaluator="not_an_evaluator")


class TestClientEndpointInit:
  """Tests for Client endpoint and connection_id params."""

  def test_default_endpoint(self):
    mock_bq = _mock_bq_client()
    client = Client(
        project_id="proj",
        dataset_id="ds",
        verify_schema=False,
        bq_client=mock_bq,
    )
    assert client.endpoint == "gemini-2.5-flash"
    assert client.connection_id is None

  def test_custom_endpoint(self):
    mock_bq = _mock_bq_client()
    client = Client(
        project_id="proj",
        dataset_id="ds",
        verify_schema=False,
        bq_client=mock_bq,
        endpoint="gemini-2.5-pro",
    )
    assert client.endpoint == "gemini-2.5-pro"

  def test_custom_connection_id(self):
    mock_bq = _mock_bq_client()
    client = Client(
        project_id="proj",
        dataset_id="ds",
        verify_schema=False,
        bq_client=mock_bq,
        connection_id="us.my-connection",
    )
    assert client.connection_id == "us.my-connection"

  def test_legacy_model_ref(self):
    mock_bq = _mock_bq_client()
    client = Client(
        project_id="proj",
        dataset_id="ds",
        verify_schema=False,
        bq_client=mock_bq,
        endpoint="proj.ds.my_model",
    )
    assert client.endpoint == "proj.ds.my_model"
    assert client._is_legacy_model_ref(client.endpoint)


class TestIsLegacyModelRef:
  """Tests for Client._is_legacy_model_ref()."""

  def test_endpoint_name(self):
    assert not Client._is_legacy_model_ref("gemini-2.5-flash")

  def test_endpoint_with_one_dot(self):
    assert not Client._is_legacy_model_ref("gemini-2.5-pro")

  def test_legacy_model_two_dots(self):
    assert Client._is_legacy_model_ref("p.d.model_name")

  def test_legacy_model_three_dots(self):
    assert Client._is_legacy_model_ref("a.b.c.d")


class TestAIGenerateJudge:
  """Tests for Client._ai_generate_judge()."""

  def test_ai_generate_judge_typed_columns(self):
    mock_bq = _mock_bq_client()
    mock_rows = [
        _make_mock_row(
            {
                "session_id": "s1",
                "trace_text": "USER: hi",
                "final_response": "hello",
                "score": 8,
                "justification": "Good response",
            }
        ),
        _make_mock_row(
            {
                "session_id": "s2",
                "trace_text": "USER: bye",
                "final_response": "goodbye",
                "score": 3,
                "justification": "Incomplete",
            }
        ),
    ]
    mock_job = MagicMock()
    mock_job.result.return_value = mock_rows
    mock_bq.query.return_value = mock_job

    client = Client(
        project_id="proj",
        dataset_id="ds",
        verify_schema=False,
        bq_client=mock_bq,
    )

    from bigquery_agent_analytics.evaluators import _JudgeCriterion
    from bigquery_agent_analytics.evaluators import LLMAsJudge

    evaluator = LLMAsJudge.correctness(threshold=0.5)
    criterion = evaluator._criteria[0]

    report = client._ai_generate_judge(
        evaluator,
        criterion,
        "agent_events_v2",
        "TRUE",
        [],
    )
    assert report.total_sessions == 2
    assert report.session_scores[0].scores["correctness"] == 0.8
    assert report.session_scores[1].scores["correctness"] == 0.3
    assert report.session_scores[0].passed is True
    assert report.session_scores[1].passed is False

  def test_fallback_chain_tries_ai_generate_first(self):
    """Verify _evaluate_llm_judge tries AI.GENERATE first."""
    mock_bq = _mock_bq_client()
    mock_rows = [
        _make_mock_row(
            {
                "session_id": "s1",
                "trace_text": "USER: hi",
                "final_response": "hello",
                "score": 7,
                "justification": "OK",
            }
        ),
    ]
    mock_job = MagicMock()
    mock_job.result.return_value = mock_rows
    mock_bq.query.return_value = mock_job

    client = Client(
        project_id="proj",
        dataset_id="ds",
        verify_schema=False,
        bq_client=mock_bq,
    )
    from bigquery_agent_analytics.evaluators import LLMAsJudge

    evaluator = LLMAsJudge.correctness()
    report = client._evaluate_llm_judge(
        evaluator,
        "agent_events",
        "TRUE",
        [],
    )
    # Should have gotten a result from AI.GENERATE path
    assert report.total_sessions == 1
    # Verify AI.GENERATE query was used (contains endpoint)
    call_args = mock_bq.query.call_args
    query_str = call_args[0][0]
    assert "AI.GENERATE" in query_str

  def test_ai_generate_passes_full_prompt_template(self):
    """AI.GENERATE judge passes the full Python template, not a truncated split.

    Regression guard for the prompt-parity bug — earlier versions
    sent only ``prompt_template.split('{trace_text}')[0]`` to AI.GENERATE,
    silently dropping the per-criterion output-format spec that
    follows the placeholders. This test asserts the BQ query receives
    three parameters (prefix/middle/suffix) and that, concatenated
    with the SQL trace_text/final_response columns, they reproduce
    the exact Python template.
    """
    mock_bq = _mock_bq_client()
    mock_rows = [
        _make_mock_row(
            {
                "session_id": "s1",
                "trace_text": "USER: hi",
                "final_response": "hello",
                "score": 8,
                "justification": "ok",
            }
        ),
    ]
    mock_job = MagicMock()
    mock_job.result.return_value = mock_rows
    mock_bq.query.return_value = mock_job

    client = Client(
        project_id="proj",
        dataset_id="ds",
        verify_schema=False,
        bq_client=mock_bq,
    )
    from bigquery_agent_analytics.evaluators import LLMAsJudge

    evaluator = LLMAsJudge.correctness(threshold=0.5)
    criterion = evaluator._criteria[0]
    client._ai_generate_judge(evaluator, criterion, "agent_events", "TRUE", [])

    # Inspect the QueryJobConfig.query_parameters that landed on the
    # BigQuery client.
    call_kwargs = mock_bq.query.call_args.kwargs
    job_config = call_kwargs["job_config"]
    by_name = {p.name: p.value for p in job_config.query_parameters}
    assert "judge_prompt_prefix" in by_name
    assert "judge_prompt_middle" in by_name
    assert "judge_prompt_suffix" in by_name
    # ``judge_prompt`` (the old single-segment param) must no longer
    # appear — its presence would mean a caller is still on the
    # truncated-split path.
    assert "judge_prompt" not in by_name
    # Concatenation reproduces the full template when the
    # placeholders are filled in.
    reconstructed = (
        by_name["judge_prompt_prefix"]
        + "TRACE_HERE"
        + by_name["judge_prompt_middle"]
        + "RESPONSE_HERE"
        + by_name["judge_prompt_suffix"]
    )
    expected = criterion.prompt_template.format(
        trace_text="TRACE_HERE", final_response="RESPONSE_HERE"
    )
    assert reconstructed == expected
    # The Python template's per-criterion output-format spec must
    # survive the round trip — that's the whole point of the fix.
    assert "JSON object" in by_name["judge_prompt_suffix"]

  def test_bqml_judge_passes_full_prompt_template(self):
    """ML.GENERATE_TEXT path uses the same prefix/middle/suffix params."""
    mock_bq = _mock_bq_client()
    mock_rows = [
        _make_mock_row(
            {
                "session_id": "s1",
                "trace_text": "USER: hi",
                "final_response": "hello",
                "evaluation": '{"correctness": 8, "justification": "ok"}',
            }
        ),
    ]
    mock_job = MagicMock()
    mock_job.result.return_value = mock_rows
    mock_bq.query.return_value = mock_job

    client = Client(
        project_id="proj",
        dataset_id="ds",
        verify_schema=False,
        bq_client=mock_bq,
    )
    from bigquery_agent_analytics.evaluators import LLMAsJudge

    evaluator = LLMAsJudge.correctness(threshold=0.5)
    criterion = evaluator._criteria[0]
    client._bqml_judge(
        evaluator,
        criterion,
        "agent_events",
        "TRUE",
        [],
        text_model="proj.ds.gemini_text_model",
    )

    job_config = mock_bq.query.call_args.kwargs["job_config"]
    names = {p.name for p in job_config.query_parameters}
    assert {
        "judge_prompt_prefix",
        "judge_prompt_middle",
        "judge_prompt_suffix",
    }.issubset(names)
    assert "judge_prompt" not in names

  def test_ai_generate_success_sets_execution_mode(self):
    """When AI.GENERATE succeeds, report.details says so explicitly."""
    mock_bq = _mock_bq_client()
    mock_rows = [
        _make_mock_row(
            {
                "session_id": "s1",
                "trace_text": "USER: hi",
                "final_response": "hello",
                "score": 8,
                "justification": "ok",
            }
        ),
    ]
    mock_job = MagicMock()
    mock_job.result.return_value = mock_rows
    mock_bq.query.return_value = mock_job

    client = Client(
        project_id="proj",
        dataset_id="ds",
        verify_schema=False,
        bq_client=mock_bq,
    )
    from bigquery_agent_analytics.evaluators import LLMAsJudge

    report = client._evaluate_llm_judge(
        LLMAsJudge.correctness(), "agent_events", "TRUE", []
    )
    assert report.details["execution_mode"] == "ai_generate"
    # No fallback fired -> no fallback_reason on the report.
    assert "fallback_reason" not in report.details

  def test_ai_generate_failure_falls_back_to_ml_generate_text(self):
    """AI.GENERATE failure -> BQML path takes over, mode reflects it."""
    mock_bq = _mock_bq_client()

    # First query call raises (AI.GENERATE), subsequent calls return
    # a BQML-shaped row.
    bqml_rows = [
        _make_mock_row(
            {
                "session_id": "s1",
                "trace_text": "USER: hi",
                "final_response": "hello",
                "evaluation": '{"correctness": 8, "justification": "ok"}',
            }
        ),
    ]
    bqml_job = MagicMock()
    bqml_job.result.return_value = bqml_rows
    mock_bq.query.side_effect = [
        Exception("AI.GENERATE not available in this region"),
        bqml_job,
    ]

    client = Client(
        project_id="proj",
        dataset_id="ds",
        verify_schema=False,
        bq_client=mock_bq,
    )
    from bigquery_agent_analytics.evaluators import LLMAsJudge

    report = client._evaluate_llm_judge(
        LLMAsJudge.correctness(), "agent_events", "TRUE", []
    )
    assert report.details["execution_mode"] == "ml_generate_text"
    assert "ai_generate" in report.details["fallback_reason"]
    assert "AI.GENERATE not available" in report.details["fallback_reason"]

  def test_both_bq_paths_fail_falls_back_to_api(self):
    """AI.GENERATE + ML.GENERATE_TEXT both fail -> Gemini API fallback."""
    mock_bq = _mock_bq_client()
    mock_bq.query.side_effect = Exception("connection missing")

    client = Client(
        project_id="proj",
        dataset_id="ds",
        verify_schema=False,
        bq_client=mock_bq,
    )
    # Stub _api_judge to avoid an actual google-genai call. The
    # method exists on the client; we just want execution_mode set.
    from bigquery_agent_analytics.evaluators import EvaluationReport
    from bigquery_agent_analytics.evaluators import LLMAsJudge

    stub_report = EvaluationReport(
        dataset="proj.ds.agent_events WHERE TRUE",
        evaluator_name="correctness_judge",
        total_sessions=0,
    )
    with patch.object(client, "_api_judge", return_value=stub_report):
      report = client._evaluate_llm_judge(
          LLMAsJudge.correctness(), "agent_events", "TRUE", []
      )
    assert report.details["execution_mode"] == "api_fallback"
    # Both upstream tiers should be named in the fallback chain.
    assert "ai_generate" in report.details["fallback_reason"]
    assert "ml_generate_text" in report.details["fallback_reason"]


class TestSplitJudgePromptTemplate:
  """Tests for evaluators.split_judge_prompt_template."""

  def test_full_template_round_trips(self):
    from bigquery_agent_analytics.evaluators import split_judge_prompt_template

    tmpl = (
        "You are a judge.\n## Trace\n{trace_text}\n## Response\n"
        "{final_response}\n## Score\nReturn JSON."
    )
    prefix, middle, suffix = split_judge_prompt_template(tmpl)
    rebuilt = prefix + "TT" + middle + "FR" + suffix
    assert rebuilt == tmpl.format(trace_text="TT", final_response="FR")

  def test_missing_final_response_keeps_label_next_to_response(self):
    """Custom template with {trace_text} only — Response: label must
    precede the appended response value.

    The SQL CONCAT runs prefix ++ trace_text ++ middle ++
    final_response ++ suffix, so a synthesized label for the
    missing placeholder belongs *immediately before* the value it
    labels — not on the far side of it.
    """
    from bigquery_agent_analytics.evaluators import split_judge_prompt_template

    tmpl = "Prefix\n{trace_text}\nThen something."
    prefix, middle, suffix = split_judge_prompt_template(tmpl)
    rebuilt = prefix + "TRACE" + middle + "RESPONSE" + suffix
    assert "Response:\nRESPONSE" in rebuilt
    # Whatever followed {trace_text} in the original template
    # appears before the synthesized response label.
    assert "Then something." in rebuilt
    assert rebuilt.index("Then something.") < rebuilt.index(
        "Response:\nRESPONSE"
    )

  def test_missing_trace_text_keeps_label_next_to_trace(self):
    """Custom template with {final_response} only — Trace: label must
    precede the appended trace value.

    Regression guard for the reviewer-flagged bug: earlier versions
    returned ``("", "\\nTrace:\\n" + before_response, suffix)`` which
    injected ``<TRACE>\\nTrace:\\n<original prompt>...`` — trace
    landed on the wrong side of the label, and the user's prompt
    text appeared after the trace instead of before it.
    """
    from bigquery_agent_analytics.evaluators import split_judge_prompt_template

    tmpl = "Custom rules.\n{final_response}\nDone."
    prefix, middle, suffix = split_judge_prompt_template(tmpl)
    rebuilt = prefix + "TRACE" + middle + "RESPONSE" + suffix
    # User's "Custom rules." prose appears before the synthesized
    # Trace: label, and the trace value sits right after the label.
    assert "Custom rules.\n" in rebuilt
    assert "Trace:\nTRACE" in rebuilt
    assert rebuilt.index("Custom rules.") < rebuilt.index("Trace:\nTRACE")
    # Response follows the trace, and the user's "Done." tail
    # appears after the response.
    assert rebuilt.index("Trace:\nTRACE") < rebuilt.index("RESPONSE")
    assert rebuilt.index("RESPONSE") < rebuilt.index("Done.")

  def test_no_placeholders_appends_labeled_trace_then_response(self):
    """Template with neither placeholder — labels precede their values.

    Original instructions stay first; trace block comes next with
    its label; response block comes last with its label.
    """
    from bigquery_agent_analytics.evaluators import split_judge_prompt_template

    tmpl = "Just instructions, no placeholders."
    prefix, middle, suffix = split_judge_prompt_template(tmpl)
    rebuilt = prefix + "TRACE" + middle + "RESPONSE" + suffix
    assert rebuilt.startswith(tmpl)
    assert "Trace:\nTRACE" in rebuilt
    assert "Response:\nRESPONSE" in rebuilt
    assert rebuilt.index("Trace:\nTRACE") < rebuilt.index("Response:\nRESPONSE")


class TestMultiCriterionJudge:
  """Tests for multi-criterion LLM judge (Fix #1)."""

  def test_all_criteria_evaluated(self):
    """Verify all criteria are evaluated, not just the first."""
    mock_bq = _mock_bq_client()
    mock_rows = [
        _make_mock_row(
            {
                "session_id": "s1",
                "trace_text": "USER: hi",
                "final_response": "hello",
                "score": 8,
                "justification": "Good",
            }
        ),
    ]
    mock_job = MagicMock()
    mock_job.result.return_value = mock_rows
    mock_bq.query.return_value = mock_job

    client = Client(
        project_id="proj",
        dataset_id="ds",
        verify_schema=False,
        bq_client=mock_bq,
    )

    from bigquery_agent_analytics.evaluators import LLMAsJudge

    # Build evaluator with TWO criteria
    judge = LLMAsJudge(name="multi_judge")
    judge.add_criterion(
        name="correctness",
        prompt_template="Score correctness.\n{trace_text}\n{final_response}",
        score_key="correctness",
        threshold=0.5,
    )
    judge.add_criterion(
        name="helpfulness",
        prompt_template="Score helpfulness.\n{trace_text}\n{final_response}",
        score_key="helpfulness",
        threshold=0.5,
    )

    report = client._evaluate_llm_judge(
        judge,
        "agent_events",
        "TRUE",
        [],
    )

    # AI.GENERATE should be called twice (once per criterion)
    assert mock_bq.query.call_count == 2
    # Session should have scores from both criteria
    assert report.total_sessions == 1
    ss = report.session_scores[0]
    assert "correctness" in ss.scores or "helpfulness" in ss.scores

  def test_empty_criteria_returns_empty_report(self):
    mock_bq = _mock_bq_client()
    client = Client(
        project_id="proj",
        dataset_id="ds",
        verify_schema=False,
        bq_client=mock_bq,
    )
    from bigquery_agent_analytics.evaluators import LLMAsJudge

    judge = LLMAsJudge(name="empty")
    report = client._evaluate_llm_judge(
        judge,
        "agent_events",
        "TRUE",
        [],
    )
    assert report.total_sessions == 0


class TestFalsePassFix:
  """Tests for empty scores false pass fix (Fix #2)."""

  def test_empty_score_fails(self):
    """Session with no parseable score should NOT pass."""
    mock_bq = _mock_bq_client()
    # Return row with score=None (unparseable)
    mock_rows = [
        _make_mock_row(
            {
                "session_id": "s1",
                "trace_text": "USER: hi",
                "final_response": "hello",
                "score": None,
                "justification": "",
            }
        ),
    ]
    mock_job = MagicMock()
    mock_job.result.return_value = mock_rows
    mock_bq.query.return_value = mock_job

    client = Client(
        project_id="proj",
        dataset_id="ds",
        verify_schema=False,
        bq_client=mock_bq,
    )

    from bigquery_agent_analytics.evaluators import _JudgeCriterion
    from bigquery_agent_analytics.evaluators import LLMAsJudge

    evaluator = LLMAsJudge.correctness(threshold=0.5)
    criterion = evaluator._criteria[0]

    report = client._ai_generate_judge(
        evaluator,
        criterion,
        "agent_events",
        "TRUE",
        [],
    )
    # Empty scores should mean FAILED, not passed
    assert report.session_scores[0].passed is False
    assert report.session_scores[0].scores == {}

  def test_valid_score_passes(self):
    """Session with valid score above threshold should pass."""
    mock_bq = _mock_bq_client()
    mock_rows = [
        _make_mock_row(
            {
                "session_id": "s1",
                "trace_text": "USER: hi",
                "final_response": "hello",
                "score": 8,
                "justification": "Good",
            }
        ),
    ]
    mock_job = MagicMock()
    mock_job.result.return_value = mock_rows
    mock_bq.query.return_value = mock_job

    client = Client(
        project_id="proj",
        dataset_id="ds",
        verify_schema=False,
        bq_client=mock_bq,
    )

    from bigquery_agent_analytics.evaluators import LLMAsJudge

    evaluator = LLMAsJudge.correctness(threshold=0.5)
    criterion = evaluator._criteria[0]

    report = client._ai_generate_judge(
        evaluator,
        criterion,
        "agent_events",
        "TRUE",
        [],
    )
    assert report.session_scores[0].passed is True
    assert report.session_scores[0].scores["correctness"] == 0.8


class TestApiJudgeUsesTableParams:
  """Tests for API judge using correct table/filter (Fix #3)."""

  def test_api_judge_uses_table_and_where(self):
    """_api_judge should query the specified table with WHERE."""
    mock_bq = _mock_bq_client()
    # Return empty results (no traces)
    mock_job = MagicMock()
    mock_job.result.return_value = []
    mock_bq.query.return_value = mock_job

    client = Client(
        project_id="proj",
        dataset_id="ds",
        verify_schema=False,
        bq_client=mock_bq,
    )

    from bigquery_agent_analytics.evaluators import LLMAsJudge

    evaluator = LLMAsJudge.correctness()
    report = client._api_judge(
        evaluator,
        "custom_table",
        "agent = 'my_agent'",
        [],
    )

    # Verify the query used the custom table
    call_args = mock_bq.query.call_args
    query_str = call_args[0][0]
    assert "custom_table" in query_str
    assert "my_agent" in query_str
    assert report.total_sessions == 0


class TestStrictMode:
  """Tests for strict evaluation mode (Feature #3)."""

  def test_strict_mode_marks_empty_as_failed(self):
    mock_bq = _mock_bq_client()
    # One good score, one empty score
    mock_rows = [
        _make_mock_row(
            {
                "session_id": "s1",
                "trace_text": "USER: hi",
                "final_response": "hello",
                "score": 8,
                "justification": "Good",
            }
        ),
        _make_mock_row(
            {
                "session_id": "s2",
                "trace_text": "USER: bye",
                "final_response": "goodbye",
                "score": None,
                "justification": "",
            }
        ),
    ]
    mock_job = MagicMock()
    mock_job.result.return_value = mock_rows
    mock_bq.query.return_value = mock_job

    client = Client(
        project_id="proj",
        dataset_id="ds",
        verify_schema=False,
        bq_client=mock_bq,
    )

    from bigquery_agent_analytics.evaluators import LLMAsJudge

    evaluator = LLMAsJudge.correctness(threshold=0.5)
    report = client.evaluate(
        evaluator=evaluator,
        strict=True,
    )
    # s1 should pass, s2 should fail (empty scores)
    assert report.passed_sessions == 1
    assert report.failed_sessions == 1
    # s2 should have parse_error detail
    s2 = [s for s in report.session_scores if s.session_id == "s2"]
    assert s2[0].passed is False
    assert s2[0].details.get("parse_error") is True


class TestAutoDetectTable:
  """Tests for table auto-detection (Feature #1)."""

  def test_auto_detects_agent_events(self):
    mock_bq = _mock_bq_client()
    # Mock table exists query
    mock_rows = [
        _make_mock_row({"table_name": "agent_events"}),
    ]
    mock_job = MagicMock()
    mock_job.result.return_value = mock_rows
    mock_bq.query.return_value = mock_job

    client = Client(
        project_id="proj",
        dataset_id="ds",
        table_id="auto",
        verify_schema=False,
        bq_client=mock_bq,
    )
    assert client.table_id == "agent_events"

  def test_auto_falls_back_to_v2(self):
    mock_bq = _mock_bq_client()
    mock_rows = [
        _make_mock_row({"table_name": "agent_events_v2"}),
    ]
    mock_job = MagicMock()
    mock_job.result.return_value = mock_rows
    mock_bq.query.return_value = mock_job

    client = Client(
        project_id="proj",
        dataset_id="ds",
        table_id="auto",
        verify_schema=False,
        bq_client=mock_bq,
    )
    assert client.table_id == "agent_events_v2"

  def test_auto_raises_when_no_table(self):
    mock_bq = _mock_bq_client()
    mock_job = MagicMock()
    mock_job.result.return_value = []
    mock_bq.query.return_value = mock_job

    with pytest.raises(ValueError, match="No events table"):
      Client(
          project_id="proj",
          dataset_id="ds",
          table_id="auto",
          verify_schema=False,
          bq_client=mock_bq,
      )


class TestDoctor:
  """Tests for Client.doctor() (Feature #5)."""

  def test_doctor_returns_report(self):
    mock_bq = _mock_bq_client()
    # Schema query returns all columns
    schema_rows = [
        _make_mock_row({"column_name": col, "data_type": "STRING"})
        for col in [
            "timestamp",
            "event_type",
            "session_id",
            "content",
            "agent",
            "invocation_id",
            "user_id",
            "trace_id",
            "span_id",
            "parent_span_id",
            "attributes",
            "latency_ms",
            "status",
            "error_message",
            "content_parts",
            "is_truncated",
        ]
    ]
    # Event coverage query
    event_rows = [
        _make_mock_row(
            {"event_type": "USER_MESSAGE_RECEIVED", "event_count": 10}
        ),
        _make_mock_row({"event_type": "LLM_RESPONSE", "event_count": 5}),
    ]

    call_count = [0]

    def mock_query(*args, **kwargs):
      call_count[0] += 1
      job = MagicMock()
      if call_count[0] == 1:
        job.result.return_value = schema_rows
      else:
        job.result.return_value = event_rows
      return job

    mock_bq.query = mock_query

    client = Client(
        project_id="proj",
        dataset_id="ds",
        verify_schema=False,
        bq_client=mock_bq,
    )
    report = client.doctor()

    assert report["table"] == "proj.ds.agent_events"
    assert report["schema"]["status"] == "ok"
    assert "event_coverage" in report
    assert isinstance(report["warnings"], list)
    assert "ai_generate" in report


class TestHitlMetrics:
  """Tests for Client.hitl_metrics() (Feature #4)."""

  def test_hitl_metrics_empty(self):
    mock_bq = _mock_bq_client()
    mock_job = MagicMock()
    mock_job.result.return_value = []
    mock_bq.query.return_value = mock_job

    client = Client(
        project_id="proj",
        dataset_id="ds",
        verify_schema=False,
        bq_client=mock_bq,
    )
    metrics = client.hitl_metrics()

    assert metrics["total_hitl_events"] == 0
    assert metrics["events"] == []
    assert metrics["completion_rates"] == {
        "confirmation": 0.0,
        "credential": 0.0,
        "input": 0.0,
    }

  def test_hitl_metrics_with_data(self):
    mock_bq = _mock_bq_client()
    mock_rows = [
        _make_mock_row(
            {
                "event_type": "HITL_CONFIRMATION_REQUEST",
                "event_count": 10,
                "session_count": 5,
                "completed_count": 0,
                "avg_latency_ms": 200.0,
            }
        ),
        _make_mock_row(
            {
                "event_type": "HITL_CONFIRMATION_REQUEST_COMPLETED",
                "event_count": 8,
                "session_count": 5,
                "completed_count": 8,
                "avg_latency_ms": 100.0,
            }
        ),
    ]
    mock_job = MagicMock()
    mock_job.result.return_value = mock_rows
    mock_bq.query.return_value = mock_job

    client = Client(
        project_id="proj",
        dataset_id="ds",
        verify_schema=False,
        bq_client=mock_bq,
    )
    metrics = client.hitl_metrics()

    assert metrics["total_hitl_events"] == 18
    assert len(metrics["events"]) == 2
    assert metrics["completion_rates"]["confirmation"] == 0.8


class TestFetchSessionMetadata:
  """Tests that _fetch_session_metadata correctly maps BQ rows."""

  @pytest.mark.asyncio
  async def test_maps_hitl_and_state_changes(self):
    from bigquery_agent_analytics.insights import InsightsConfig

    mock_bq = _mock_bq_client()
    mock_rows = [
        _make_mock_row(
            {
                "session_id": "sess-1",
                "event_count": 30,
                "tool_calls": 5,
                "tool_errors": 1,
                "llm_calls": 8,
                "turn_count": 4,
                "total_latency_ms": 6000.0,
                "avg_latency_ms": 200.0,
                "agents_used": ["agent_a", "agent_b"],
                "tools_used": ["search", "calc"],
                "has_error": True,
                "hitl_events": 3,
                "state_changes": 7,
                "start_time": datetime(2024, 6, 1, tzinfo=timezone.utc),
                "end_time": datetime(2024, 6, 1, 0, 10, tzinfo=timezone.utc),
            }
        ),
    ]
    mock_job = MagicMock()
    mock_job.result.return_value = mock_rows
    mock_bq.query.return_value = mock_job

    client = Client(
        project_id="proj",
        dataset_id="ds",
        verify_schema=False,
        bq_client=mock_bq,
    )
    result = await client._fetch_session_metadata(
        table="agent_events",
        where="TRUE",
        params=[],
        config=InsightsConfig(),
    )

    assert len(result) == 1
    meta = result[0]
    assert meta.session_id == "sess-1"
    assert meta.hitl_events == 3
    assert meta.state_changes == 7
    assert meta.event_count == 30
    assert meta.has_error is True

  @pytest.mark.asyncio
  async def test_missing_hitl_fields_default_zero(self):
    """Rows from older schemas omit hitl_events/state_changes."""
    from bigquery_agent_analytics.insights import InsightsConfig

    mock_bq = _mock_bq_client()
    mock_rows = [
        _make_mock_row(
            {
                "session_id": "sess-2",
                "event_count": 10,
                "tool_calls": 2,
                "tool_errors": 0,
                "llm_calls": 3,
                "turn_count": 1,
                "total_latency_ms": 1000.0,
                "avg_latency_ms": 100.0,
                "agents_used": [],
                "tools_used": [],
                "has_error": False,
                "start_time": None,
                "end_time": None,
            }
        ),
    ]
    mock_job = MagicMock()
    mock_job.result.return_value = mock_rows
    mock_bq.query.return_value = mock_job

    client = Client(
        project_id="proj",
        dataset_id="ds",
        verify_schema=False,
        bq_client=mock_bq,
    )
    result = await client._fetch_session_metadata(
        table="agent_events",
        where="TRUE",
        params=[],
        config=InsightsConfig(),
    )

    assert len(result) == 1
    meta = result[0]
    assert meta.hitl_events == 0
    assert meta.state_changes == 0


# ------------------------------------------------------------------ #
# Categorical Evaluation                                               #
# ------------------------------------------------------------------ #


def _make_categorical_config(**overrides):
  """Builds a minimal CategoricalEvaluationConfig for testing."""
  defaults = dict(
      metrics=[
          CategoricalMetricDefinition(
              name="tone",
              definition="Tone.",
              categories=[
                  CategoricalMetricCategory(
                      name="positive", definition="Good."
                  ),
                  CategoricalMetricCategory(name="negative", definition="Bad."),
              ],
          ),
      ],
  )
  defaults.update(overrides)
  return CategoricalEvaluationConfig(**defaults)


class TestEvaluateCategoricalEndpoint:
  """Tests for endpoint resolution in evaluate_categorical()."""

  def test_legacy_model_ref_on_client_falls_back_to_default(self):
    """Client with a legacy BQML endpoint should NOT pass it to
    AI.GENERATE — it should fall back to the default endpoint."""
    mock_bq = _mock_bq_client()
    mock_bq.query.return_value.result.return_value = iter([])
    client = Client(
        project_id="proj",
        dataset_id="ds",
        verify_schema=False,
        bq_client=mock_bq,
        endpoint="proj.ds.my_model",
    )
    config = _make_categorical_config()
    report = client.evaluate_categorical(config=config)

    # The SQL sent to BigQuery should contain the default Gemini
    # endpoint, not the legacy model ref.
    sql = mock_bq.query.call_args[0][0]
    assert "gemini-2.5-flash" in sql
    assert "proj.ds.my_model" not in sql

  def test_config_endpoint_overrides_client(self):
    """Explicit config.endpoint should take precedence over client."""
    mock_bq = _mock_bq_client()
    mock_bq.query.return_value.result.return_value = iter([])
    client = Client(
        project_id="proj",
        dataset_id="ds",
        verify_schema=False,
        bq_client=mock_bq,
        endpoint="gemini-2.5-pro",
    )
    config = _make_categorical_config(endpoint="gemini-2.0-flash")
    report = client.evaluate_categorical(config=config)

    sql = mock_bq.query.call_args[0][0]
    assert "gemini-2.0-flash" in sql

  def test_config_default_uses_client_endpoint(self):
    """When config.endpoint is the default, client.endpoint should be
    used (if it is not a legacy ref)."""
    mock_bq = _mock_bq_client()
    mock_bq.query.return_value.result.return_value = iter([])
    client = Client(
        project_id="proj",
        dataset_id="ds",
        verify_schema=False,
        bq_client=mock_bq,
        endpoint="gemini-2.5-pro",
    )
    config = _make_categorical_config()  # default endpoint
    report = client.evaluate_categorical(config=config)

    sql = mock_bq.query.call_args[0][0]
    assert "gemini-2.5-pro" in sql

  def test_explicit_default_overrides_legacy_client(self):
    """Explicitly setting config.endpoint='gemini-2.5-flash' should
    override even a legacy client endpoint."""
    mock_bq = _mock_bq_client()
    mock_bq.query.return_value.result.return_value = iter([])
    client = Client(
        project_id="proj",
        dataset_id="ds",
        verify_schema=False,
        bq_client=mock_bq,
        endpoint="proj.ds.my_model",
    )
    # Explicitly set — but same as default value.
    # The precedence logic treats this as "not explicitly set" so
    # it falls back to the legacy guard, which returns the default.
    config = _make_categorical_config(endpoint="gemini-2.5-flash")
    report = client.evaluate_categorical(config=config)

    sql = mock_bq.query.call_args[0][0]
    assert "gemini-2.5-flash" in sql
    assert "proj.ds.my_model" not in sql


class TestEvaluateCategoricalDataset:
  """Tests for dataset metadata in evaluate_categorical()."""

  def test_report_reflects_table_override(self):
    """When dataset= is passed, the report should reference it."""
    mock_bq = _mock_bq_client()
    mock_bq.query.return_value.result.return_value = iter([])
    client = Client(
        project_id="proj",
        dataset_id="ds",
        verify_schema=False,
        bq_client=mock_bq,
    )
    config = _make_categorical_config()
    report = client.evaluate_categorical(
        config=config,
        dataset="custom_events",
    )
    assert "custom_events" in report.dataset

  def test_report_uses_default_table(self):
    """Without dataset= override, report should use default table."""
    mock_bq = _mock_bq_client()
    mock_bq.query.return_value.result.return_value = iter([])
    client = Client(
        project_id="proj",
        dataset_id="ds",
        verify_schema=False,
        bq_client=mock_bq,
    )
    config = _make_categorical_config()
    report = client.evaluate_categorical(config=config)
    assert "agent_events" in report.dataset


class TestEvaluateCategoricalFallback:
  """Tests for AI.GENERATE → Gemini API fallback in evaluate_categorical."""

  def test_ai_generate_success_sets_execution_mode(self):
    """When AI.GENERATE succeeds, execution_mode should be 'ai_generate'."""
    mock_bq = _mock_bq_client()
    mock_bq.query.return_value.result.return_value = iter([])
    client = Client(
        project_id="proj",
        dataset_id="ds",
        verify_schema=False,
        bq_client=mock_bq,
    )
    config = _make_categorical_config()
    report = client.evaluate_categorical(config=config)
    assert report.details["execution_mode"] == "ai_generate"

  def test_ai_generate_failure_triggers_api_fallback(self):
    """When AI.GENERATE raises, should fall back to Gemini API."""
    mock_bq = _mock_bq_client()

    # First call (AI.GENERATE) raises; second call (transcript fetch)
    # returns one session.
    transcript_row = MagicMock()
    transcript_row.__iter__ = lambda self: iter(
        [("session_id", "s1"), ("transcript", "USER: Hello")]
    )
    transcript_row.keys = lambda: ["session_id", "transcript"]

    call_count = [0]

    def query_side_effect(*args, **kwargs):
      call_count[0] += 1
      result = MagicMock()
      if call_count[0] == 1:
        # AI.GENERATE query fails.
        result.result.side_effect = Exception("AI.GENERATE not available")
      else:
        # Transcript query succeeds.
        result.result.return_value = iter(
            [
                {"session_id": "s1", "transcript": "USER: Hello"},
            ]
        )
      return result

    mock_bq.query.side_effect = query_side_effect

    client = Client(
        project_id="proj",
        dataset_id="ds",
        verify_schema=False,
        bq_client=mock_bq,
    )
    config = _make_categorical_config()

    with patch(
        "bigquery_agent_analytics.client.classify_sessions_via_api",
    ) as mock_api:
      import asyncio

      from bigquery_agent_analytics.categorical_evaluator import CategoricalMetricResult
      from bigquery_agent_analytics.categorical_evaluator import CategoricalSessionResult

      async def fake_api(transcripts, config, endpoint):
        return [
            CategoricalSessionResult(
                session_id="s1",
                metrics=[
                    CategoricalMetricResult(
                        metric_name=m.name,
                        category=m.categories[0].name,
                    )
                    for m in config.metrics
                ],
            )
        ]

      mock_api.side_effect = fake_api
      report = client.evaluate_categorical(config=config)

    assert report.details["execution_mode"] == "api_fallback"
    assert "AI.GENERATE not available" in report.details["fallback_reason"]
    assert report.total_sessions == 1

  def test_fallback_reason_absent_on_success(self):
    """On AI.GENERATE success, there should be no fallback_reason."""
    mock_bq = _mock_bq_client()
    mock_bq.query.return_value.result.return_value = iter([])
    client = Client(
        project_id="proj",
        dataset_id="ds",
        verify_schema=False,
        bq_client=mock_bq,
    )
    config = _make_categorical_config()
    report = client.evaluate_categorical(config=config)
    assert "fallback_reason" not in report.details

  def test_api_unavailable_when_genai_not_installed(self):
    """When AI.GENERATE fails and google-genai is missing, report
    should have execution_mode='api_unavailable'."""
    mock_bq = _mock_bq_client()

    call_count = [0]

    def query_side_effect(*args, **kwargs):
      call_count[0] += 1
      result = MagicMock()
      if call_count[0] == 1:
        result.result.side_effect = Exception("AI.GENERATE not available")
      else:
        result.result.return_value = iter(
            [
                {"session_id": "s1", "transcript": "USER: Hello"},
            ]
        )
      return result

    mock_bq.query.side_effect = query_side_effect

    client = Client(
        project_id="proj",
        dataset_id="ds",
        verify_schema=False,
        bq_client=mock_bq,
    )
    config = _make_categorical_config()

    with patch(
        "bigquery_agent_analytics.client.classify_sessions_via_api",
        side_effect=ImportError("No module named 'google.genai'"),
    ):
      report = client.evaluate_categorical(config=config)

    assert report.details["execution_mode"] == "api_unavailable"
    assert report.details["api_error"] == "google-genai not installed"
    assert "AI.GENERATE not available" in report.details["fallback_reason"]
    assert report.total_sessions == 0


class TestEvaluateCategoricalPersistence:
  """Tests for persist_results flow in evaluate_categorical."""

  def _make_client_with_results(self):
    """Returns a (client, mock_bq) pair where AI.GENERATE returns
    one session with valid classifications."""
    import json

    mock_bq = _mock_bq_client()
    row = {
        "session_id": "s1",
        "transcript": "USER: Hello",
        "classifications": json.dumps(
            [
                {"metric_name": "tone", "category": "positive"},
            ]
        ),
    }
    mock_bq.query.return_value.result.return_value = iter([row])
    mock_bq.insert_rows_json.return_value = []  # no errors

    client = Client(
        project_id="proj",
        dataset_id="ds",
        verify_schema=False,
        bq_client=mock_bq,
    )
    return client, mock_bq

  def test_persist_disabled_by_default(self):
    """When persist_results=False (default), no DDL or insert calls."""
    client, mock_bq = self._make_client_with_results()
    config = _make_categorical_config()
    report = client.evaluate_categorical(config=config)

    # Only one query call: the AI.GENERATE query.
    assert mock_bq.query.call_count == 1
    mock_bq.insert_rows_json.assert_not_called()
    assert "persisted" not in report.details

  def test_persist_creates_table_and_inserts(self):
    """When persist_results=True, DDL and insert_rows_json are called."""
    client, mock_bq = self._make_client_with_results()
    # DDL query returns immediately.
    ddl_result = MagicMock()
    ai_result = MagicMock()
    ai_result.result.return_value = (
        mock_bq.query.return_value.result.return_value
    )

    import json

    row = {
        "session_id": "s1",
        "transcript": "USER: Hello",
        "classifications": json.dumps(
            [
                {"metric_name": "tone", "category": "positive"},
            ]
        ),
    }

    call_count = [0]

    def query_side_effect(*args, **kwargs):
      call_count[0] += 1
      result = MagicMock()
      if call_count[0] == 1:
        # AI.GENERATE query.
        result.result.return_value = iter([row])
      else:
        # DDL query.
        result.result.return_value = None
      return result

    mock_bq.query.side_effect = query_side_effect

    config = _make_categorical_config(
        persist_results=True,
        results_table="my_results",
    )
    report = client.evaluate_categorical(config=config)

    # Two queries: AI.GENERATE + DDL.
    assert mock_bq.query.call_count == 2
    ddl_sql = mock_bq.query.call_args_list[1][0][0]
    assert "CREATE TABLE IF NOT EXISTS" in ddl_sql
    assert "my_results" in ddl_sql

    # insert_rows_json called with flattened rows.
    mock_bq.insert_rows_json.assert_called_once()
    table_ref = mock_bq.insert_rows_json.call_args[0][0]
    assert "my_results" in table_ref
    rows = mock_bq.insert_rows_json.call_args[0][1]
    assert len(rows) == 1
    assert rows[0]["session_id"] == "s1"
    assert rows[0]["metric_name"] == "tone"
    assert rows[0]["category"] == "positive"

    assert report.details["persisted"] is True
    assert report.details["persisted_rows"] == 1

  def test_persist_uses_default_table_name(self):
    """When results_table is None, uses the default table name."""
    client, mock_bq = self._make_client_with_results()

    import json

    row = {
        "session_id": "s1",
        "transcript": "USER: Hello",
        "classifications": json.dumps(
            [
                {"metric_name": "tone", "category": "positive"},
            ]
        ),
    }

    call_count = [0]

    def query_side_effect(*args, **kwargs):
      call_count[0] += 1
      result = MagicMock()
      if call_count[0] == 1:
        result.result.return_value = iter([row])
      else:
        result.result.return_value = None
      return result

    mock_bq.query.side_effect = query_side_effect

    config = _make_categorical_config(persist_results=True)
    report = client.evaluate_categorical(config=config)

    table_ref = mock_bq.insert_rows_json.call_args[0][0]
    assert "categorical_results" in table_ref

  def test_persist_failure_sets_error_details(self):
    """When insert fails, report should have persisted=False."""
    client, mock_bq = self._make_client_with_results()

    import json

    row = {
        "session_id": "s1",
        "transcript": "USER: Hello",
        "classifications": json.dumps(
            [
                {"metric_name": "tone", "category": "positive"},
            ]
        ),
    }

    call_count = [0]

    def query_side_effect(*args, **kwargs):
      call_count[0] += 1
      result = MagicMock()
      if call_count[0] == 1:
        result.result.return_value = iter([row])
      else:
        result.result.return_value = None
      return result

    mock_bq.query.side_effect = query_side_effect
    mock_bq.insert_rows_json.return_value = [{"errors": "some error"}]

    config = _make_categorical_config(
        persist_results=True,
        results_table="my_results",
    )
    report = client.evaluate_categorical(config=config)

    assert report.details["persisted"] is False
    assert "persist_error" in report.details

  def test_persist_ddl_failure_sets_error_details(self):
    """When DDL fails, report should have persisted=False."""
    client, mock_bq = self._make_client_with_results()

    import json

    row = {
        "session_id": "s1",
        "transcript": "USER: Hello",
        "classifications": json.dumps(
            [
                {"metric_name": "tone", "category": "positive"},
            ]
        ),
    }

    call_count = [0]

    def query_side_effect(*args, **kwargs):
      call_count[0] += 1
      result = MagicMock()
      if call_count[0] == 1:
        result.result.return_value = iter([row])
      else:
        result.result.side_effect = Exception("Permission denied")
      return result

    mock_bq.query.side_effect = query_side_effect

    config = _make_categorical_config(
        persist_results=True,
        results_table="my_results",
    )
    report = client.evaluate_categorical(config=config)

    assert report.details["persisted"] is False
    assert "Permission denied" in report.details["persist_error"]

  def test_persist_skipped_on_empty_results(self):
    """When no sessions to persist, skip with a note."""
    mock_bq = _mock_bq_client()
    mock_bq.query.return_value.result.return_value = iter([])

    client = Client(
        project_id="proj",
        dataset_id="ds",
        verify_schema=False,
        bq_client=mock_bq,
    )
    config = _make_categorical_config(
        persist_results=True,
        results_table="my_results",
    )
    report = client.evaluate_categorical(config=config)

    assert report.details["persisted"] is False
    assert report.details["persist_note"] == "no sessions to persist"
    mock_bq.insert_rows_json.assert_not_called()


# ------------------------------------------------------------------ #
# AI.CLASSIFY in evaluate_categorical                                  #
# ------------------------------------------------------------------ #


class TestEvaluateCategoricalAiClassify:
  """Tests for AI.CLASSIFY integration in evaluate_categorical."""

  def test_ai_classify_used_when_no_justification(self):
    """When include_justification=False, SQL should use AI.CLASSIFY."""
    mock_bq = _mock_bq_client()
    mock_bq.query.return_value.result.return_value = iter([])
    client = Client(
        project_id="proj",
        dataset_id="ds",
        verify_schema=False,
        bq_client=mock_bq,
    )
    config = _make_categorical_config(include_justification=False)
    report = client.evaluate_categorical(config=config)

    sql = mock_bq.query.call_args[0][0]
    assert "AI.CLASSIFY" in sql
    assert report.details["execution_mode"] == "ai_classify"

  def test_ai_classify_skipped_when_justification_true(self):
    """When include_justification=True, SQL should use AI.GENERATE."""
    mock_bq = _mock_bq_client()
    mock_bq.query.return_value.result.return_value = iter([])
    client = Client(
        project_id="proj",
        dataset_id="ds",
        verify_schema=False,
        bq_client=mock_bq,
    )
    config = _make_categorical_config(include_justification=True)
    report = client.evaluate_categorical(config=config)

    sql = mock_bq.query.call_args[0][0]
    assert "AI.GENERATE" in sql
    assert "AI.CLASSIFY" not in sql
    assert report.details["execution_mode"] == "ai_generate"

  def test_ai_classify_failure_falls_back_to_ai_generate(self):
    """When AI.CLASSIFY fails, should fall back to AI.GENERATE."""
    mock_bq = _mock_bq_client()

    call_count = [0]

    def query_side_effect(*args, **kwargs):
      call_count[0] += 1
      result = MagicMock()
      if call_count[0] == 1:
        # AI.CLASSIFY fails.
        result.result.side_effect = Exception("AI.CLASSIFY not available")
      else:
        # AI.GENERATE succeeds.
        result.result.return_value = iter([])
      return result

    mock_bq.query.side_effect = query_side_effect

    client = Client(
        project_id="proj",
        dataset_id="ds",
        verify_schema=False,
        bq_client=mock_bq,
    )
    config = _make_categorical_config(include_justification=False)
    report = client.evaluate_categorical(config=config)

    assert report.details["execution_mode"] == "ai_generate"
    assert (
        "AI.CLASSIFY not available"
        in report.details["classify_fallback_reason"]
    )

  def test_ai_classify_and_generate_both_fail_falls_back_to_api(self):
    """When both AI.CLASSIFY and AI.GENERATE fail, falls back to API."""
    mock_bq = _mock_bq_client()

    call_count = [0]

    def query_side_effect(*args, **kwargs):
      call_count[0] += 1
      result = MagicMock()
      if call_count[0] <= 2:
        # AI.CLASSIFY and AI.GENERATE both fail.
        result.result.side_effect = Exception(f"step {call_count[0]} failed")
      else:
        # Transcript fetch for API fallback succeeds.
        result.result.return_value = iter(
            [{"session_id": "s1", "transcript": "USER: Hello"}]
        )
      return result

    mock_bq.query.side_effect = query_side_effect

    client = Client(
        project_id="proj",
        dataset_id="ds",
        verify_schema=False,
        bq_client=mock_bq,
    )
    config = _make_categorical_config(include_justification=False)

    with patch(
        "bigquery_agent_analytics.client.classify_sessions_via_api",
    ) as mock_api:
      from bigquery_agent_analytics.categorical_evaluator import CategoricalMetricResult
      from bigquery_agent_analytics.categorical_evaluator import CategoricalSessionResult

      async def fake_api(transcripts, config, endpoint):
        return [
            CategoricalSessionResult(
                session_id="s1",
                metrics=[
                    CategoricalMetricResult(
                        metric_name=m.name,
                        category=m.categories[0].name,
                    )
                    for m in config.metrics
                ],
            )
        ]

      mock_api.side_effect = fake_api
      report = client.evaluate_categorical(config=config)

    assert report.details["execution_mode"] == "api_fallback"

  def test_ai_classify_persists_correct_execution_mode(self):
    """execution_mode should be 'ai_classify' when AI.CLASSIFY succeeds."""
    mock_bq = _mock_bq_client()
    row = {
        "session_id": "s1",
        "transcript": "USER: Hello",
        "classify_0": "positive",
    }
    mock_bq.query.return_value.result.return_value = iter([row])
    client = Client(
        project_id="proj",
        dataset_id="ds",
        verify_schema=False,
        bq_client=mock_bq,
    )
    config = _make_categorical_config(include_justification=False)
    report = client.evaluate_categorical(config=config)

    assert report.details["execution_mode"] == "ai_classify"
    assert report.total_sessions == 1

  def test_ai_classify_null_tracked_separately_from_parse_errors(self):
    """NULL results from AI.CLASSIFY should be tracked as
    classify_null_count, not as parse_errors."""
    mock_bq = _mock_bq_client()
    row = {
        "session_id": "s1",
        "transcript": "USER: Hello",
        "classify_0": None,
    }
    mock_bq.query.return_value.result.return_value = iter([row])
    client = Client(
        project_id="proj",
        dataset_id="ds",
        verify_schema=False,
        bq_client=mock_bq,
    )
    config = _make_categorical_config(include_justification=False)
    report = client.evaluate_categorical(config=config)

    assert report.details["classify_null_count"] == 1
    # NULL is not a parse error — it's an execution failure.
    assert report.details["parse_errors"] == 0

  def test_ai_generate_fallback_uses_connection_id(self):
    """connection_id should appear in AI.GENERATE SQL when set."""
    mock_bq = _mock_bq_client()
    mock_bq.query.return_value.result.return_value = iter([])
    client = Client(
        project_id="proj",
        dataset_id="ds",
        verify_schema=False,
        bq_client=mock_bq,
        connection_id="proj.us.conn",
    )
    config = _make_categorical_config(include_justification=True)
    report = client.evaluate_categorical(config=config)

    sql = mock_bq.query.call_args[0][0]
    assert "connection_id => 'proj.us.conn'" in sql

  def test_config_connection_id_overrides_client(self):
    """config.connection_id should take precedence over client."""
    mock_bq = _mock_bq_client()
    mock_bq.query.return_value.result.return_value = iter([])
    client = Client(
        project_id="proj",
        dataset_id="ds",
        verify_schema=False,
        bq_client=mock_bq,
        connection_id="proj.us.client_conn",
    )
    config = _make_categorical_config(
        include_justification=False,
        connection_id="proj.us.config_conn",
    )
    report = client.evaluate_categorical(config=config)

    sql = mock_bq.query.call_args[0][0]
    assert "proj.us.config_conn" in sql
    assert "proj.us.client_conn" not in sql


# ------------------------------------------------------------------ #
# create_categorical_views                                             #
# ------------------------------------------------------------------ #


class TestCreateCategoricalViews:

  def test_delegates_to_view_manager(self):
    mock_bq = _mock_bq_client()
    client = Client(
        project_id="proj",
        dataset_id="ds",
        verify_schema=False,
        bq_client=mock_bq,
    )
    with patch(
        "bigquery_agent_analytics.categorical_views.CategoricalViewManager"
    ) as mock_cls:
      vm = MagicMock()
      vm.create_all_views.return_value = {
          "categorical_results_latest": "categorical_results_latest",
      }
      mock_cls.return_value = vm

      result = client.create_categorical_views()

      mock_cls.assert_called_once_with(
          project_id="proj",
          dataset_id="ds",
          results_table="categorical_results",
          view_prefix="",
          location=None,
          bq_client=mock_bq,
      )
      assert "categorical_results_latest" in result

  def test_custom_table_and_prefix(self):
    mock_bq = _mock_bq_client()
    client = Client(
        project_id="proj",
        dataset_id="ds",
        verify_schema=False,
        bq_client=mock_bq,
    )
    with patch(
        "bigquery_agent_analytics.categorical_views.CategoricalViewManager"
    ) as mock_cls:
      vm = MagicMock()
      vm.create_all_views.return_value = {}
      mock_cls.return_value = vm

      client.create_categorical_views(
          results_table="my_results",
          view_prefix="adk_",
      )

      call_kwargs = mock_cls.call_args[1]
      assert call_kwargs["results_table"] == "my_results"
      assert call_kwargs["view_prefix"] == "adk_"
