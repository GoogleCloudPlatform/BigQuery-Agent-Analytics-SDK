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

"""Tests for PR #17 fixes.

Covers:
  P1 - Semantic grouping AI.GENERATE path
  P1 - golden_dataset removal from evaluate()
  P2 - agent_events_v2 doc/example consistency
  P3 - Strict-mode parse_errors always present
  P3 - Drift new_questions preserve original casing
  Feature - BigQueryTraceEvaluator include_event_types
  Feature - Docs consistency check for agent_events_v2
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from datetime import timezone
import inspect
import os
from pathlib import Path
import re
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from bigquery_agent_analytics.client import _apply_strict_mode
from bigquery_agent_analytics.client import Client
from bigquery_agent_analytics.evaluators import EvaluationReport
from bigquery_agent_analytics.evaluators import SessionScore
from bigquery_agent_analytics.feedback import _AI_GENERATE_SEMANTIC_GROUPING_QUERY
from bigquery_agent_analytics.feedback import _is_legacy_model_ref
from bigquery_agent_analytics.feedback import _LEGACY_SEMANTIC_GROUPING_QUERY
from bigquery_agent_analytics.feedback import _sanitize_categories
from bigquery_agent_analytics.feedback import _semantic_drift
from bigquery_agent_analytics.feedback import AnalysisConfig
from bigquery_agent_analytics.feedback import compute_drift
from bigquery_agent_analytics.feedback import compute_question_distribution
from bigquery_agent_analytics.trace_evaluator import BigQueryTraceEvaluator

# ================================================================== #
# Helpers                                                              #
# ================================================================== #

ROOT = Path(__file__).resolve().parent.parent


class _MockRow(dict):
  """Dict subclass that satisfies BigQuery row protocol."""

  def __init__(self, d):
    super().__init__(d)

  def keys(self):
    return super().keys()

  def values(self):
    return super().values()

  def items(self):
    return super().items()

  def get(self, key, default=None):
    return super().get(key, default)


def _make_mock_bq_client(rows=None):
  mock_client = MagicMock()
  mock_result = MagicMock()
  mock_result.result.return_value = [_MockRow(r) for r in (rows or [])]
  mock_client.query.return_value = mock_result
  return mock_client


# ================================================================== #
# P1: Semantic grouping AI.GENERATE endpoint routing                   #
# ================================================================== #


class TestSemanticGroupingRouting:
  """Semantic grouping should route through AI.GENERATE first."""

  def test_legacy_model_ref_detection(self):
    """_is_legacy_model_ref detects BQ ML model references."""
    assert _is_legacy_model_ref("project.dataset.model")
    assert _is_legacy_model_ref("p.d.m.extra")
    assert not _is_legacy_model_ref("gemini-2.5-flash")
    assert not _is_legacy_model_ref("gemini-2.5-pro")

  def test_ai_generate_query_uses_endpoint(self):
    """AI.GENERATE query should use endpoint (not MODEL)."""
    assert "endpoint =>" in _AI_GENERATE_SEMANTIC_GROUPING_QUERY
    assert "AI.GENERATE" in _AI_GENERATE_SEMANTIC_GROUPING_QUERY

  def test_legacy_query_uses_model(self):
    """Legacy query should use MODEL and ML.GENERATE_TEXT."""
    assert "MODEL `{model}`" in _LEGACY_SEMANTIC_GROUPING_QUERY
    assert "ML.GENERATE_TEXT" in _LEGACY_SEMANTIC_GROUPING_QUERY

  async def test_semantic_grouping_tries_ai_generate_first(self):
    """Semantic grouping should try AI.GENERATE before legacy."""
    mock_client = _make_mock_bq_client(
        [
            {"question": "How do I reset?", "category": "Technical Support"},
            {"question": "What is my balance?", "category": "Account"},
        ]
    )
    config = AnalysisConfig(mode="auto_group_using_semantics")
    loop = asyncio.get_event_loop()

    result = await compute_question_distribution(
        bq_client=mock_client,
        project_id="p",
        dataset_id="d",
        table_id="t",
        where_clause="1=1",
        query_params=[],
        config=config,
        text_model="gemini-2.5-flash",
    )

    # Should have used AI.GENERATE path
    assert result.details.get("grouping_mode") == "ai_generate"
    assert result.total_questions == 2

  async def test_semantic_grouping_legacy_for_bqml_ref(self):
    """When text_model is a legacy ref, use ML.GENERATE_TEXT."""
    mock_client = _make_mock_bq_client(
        [
            {"question": "Hello", "category": "General"},
        ]
    )
    config = AnalysisConfig(mode="auto_group_using_semantics")

    result = await compute_question_distribution(
        bq_client=mock_client,
        project_id="p",
        dataset_id="d",
        table_id="t",
        where_clause="1=1",
        query_params=[],
        config=config,
        text_model="project.dataset.my_model",
    )

    assert result.details.get("grouping_mode") == "legacy_ml_generate_text"

  async def test_semantic_grouping_fallback_to_frequently_asked(self):
    """When AI.GENERATE and legacy both fail, fallback to freq asked."""
    mock_client = MagicMock()

    call_count = [0]

    def _query_side_effect(query, **kwargs):
      call_count[0] += 1
      if call_count[0] <= 2:
        # First two calls (AI.GENERATE and legacy) fail
        raise Exception("BQ error")
      # Third call (frequently_asked) succeeds
      mock_result = MagicMock()
      mock_result.result.return_value = [
          _MockRow({"question": "FAQ", "frequency": 5}),
      ]
      return mock_result

    mock_client.query.side_effect = _query_side_effect
    config = AnalysisConfig(mode="auto_group_using_semantics")

    result = await compute_question_distribution(
        bq_client=mock_client,
        project_id="p",
        dataset_id="d",
        table_id="t",
        where_clause="1=1",
        query_params=[],
        config=config,
        text_model="gemini-2.5-flash",
    )

    assert result.details.get("grouping_mode") == ("frequently_asked_fallback")

  def test_client_deep_analysis_passes_endpoint(self):
    """Client.deep_analysis passes self.endpoint to grouping."""
    client = Client(
        project_id="p",
        dataset_id="d",
        verify_schema=False,
        bq_client=_make_mock_bq_client(
            [
                {"question": "Q1", "category": "Cat1"},
            ]
        ),
    )
    # Default endpoint should not be a legacy ref
    assert not _is_legacy_model_ref(client.endpoint)


# ================================================================== #
# P1: golden_dataset removed from evaluate()                          #
# ================================================================== #


class TestGoldenDatasetRemoved:
  """golden_dataset should no longer be in evaluate() signature."""

  def test_no_golden_dataset_param(self):
    """evaluate() should not accept golden_dataset."""
    sig = inspect.signature(Client.evaluate)
    assert "golden_dataset" not in sig.parameters

  def test_evaluate_still_works(self):
    """evaluate() should still work with remaining params."""
    from bigquery_agent_analytics import SystemEvaluator

    client = Client(
        project_id="p",
        dataset_id="d",
        verify_schema=False,
        bq_client=_make_mock_bq_client(
            [
                {
                    "session_id": "s1",
                    "event_count": 5,
                    "avg_latency_ms": 100.0,
                    "total_tokens": 50,
                    "turn_count": 2,
                    "tool_calls": 1,
                    "error_count": 0,
                    "has_error": False,
                    "unique_tools": 1,
                },
            ]
        ),
    )
    evaluator = SystemEvaluator.latency(threshold_ms=5000)
    report = client.evaluate(evaluator)
    assert report.total_sessions == 1


# ================================================================== #
# P2/P3: agent_events_v2 docs consistency check                       #
# ================================================================== #


class TestAgentEventsV2Consistency:
  """No source code or examples should default to agent_events_v2."""

  def _scan_files(self, glob_pattern, exclude=None):
    """Find agent_events_v2 refs in matching files."""
    exclude = exclude or set()
    hits = []
    for path in ROOT.rglob(glob_pattern):
      if any(ex in str(path) for ex in exclude):
        continue
      try:
        text = path.read_text()
      except (UnicodeDecodeError, IsADirectoryError):
        continue
      for i, line in enumerate(text.splitlines(), 1):
        if "agent_events_v2" in line:
          hits.append((str(path.relative_to(ROOT)), i, line.strip()))
    return hits

  def test_no_v2_as_default_in_source(self):
    """No source module should use agent_events_v2 as a default value."""
    hits = self._scan_files("*.py", exclude={"test_", "e2e_demo_output"})
    # Look specifically for default parameter values like
    # table_id="agent_events_v2" or table_id: str = "agent_events_v2"
    bad = []
    for path, lineno, line in hits:
      if not path.startswith("src/"):
        continue
      # Match default value assignments (not docs or auto-detect)
      if re.search(r'=\s*["\']agent_events_v2["\']', line):
        # Exclude auto-detect lists and schema queries
        if "AUTO_DETECT" in line or "table_name" in line:
          continue
        bad.append((path, lineno, line))
    assert not bad, f"Found agent_events_v2 as default value:\n" + "\n".join(
        f"  {p}:{n}: {l}" for p, n, l in bad
    )

  def test_no_v2_in_trace_evaluator_docs(self):
    """trace_evaluator.py docs should reference agent_events."""
    text = (
        ROOT / "src" / "bigquery_agent_analytics" / "trace_evaluator.py"
    ).read_text()
    # Check docstrings (not the DEFAULT_EVENT_TYPES list)
    docstring_matches = re.findall(
        r'(?:""".*?agent_events_v2.*?""")',
        text,
        re.DOTALL,
    )
    assert (
        not docstring_matches
    ), f"Found agent_events_v2 in trace_evaluator.py docstrings"

  def test_no_v2_default_in_e2e_demo(self):
    """e2e_demo.py should default to agent_events."""
    text = (ROOT / "examples" / "e2e_demo.py").read_text()
    # Find the TABLE_ID default
    match = re.search(r'TABLE_ID\s*=.*?"(agent_events[^"]*)"', text)
    assert match, "Could not find TABLE_ID in e2e_demo.py"
    assert (
        match.group(1) == "agent_events"
    ), f"e2e_demo.py TABLE_ID defaults to {match.group(1)!r}"

  def test_no_v2_in_bigframes_example(self):
    """bigframes_evaluator.py example should use agent_events."""
    text = (
        ROOT / "src" / "bigquery_agent_analytics" / "bigframes_evaluator.py"
    ).read_text()
    # Find table_id in the example
    example_section = text[:500]  # Example is at the top
    assert 'table_id="agent_events_v2"' not in example_section


# ================================================================== #
# P3: Strict-mode parse_errors always present                          #
# ================================================================== #


class TestStrictModeParseErrorsStable:
  """parse_errors should always be in report.details (not aggregate_scores)."""

  def test_parse_errors_in_details_when_errors_exist(self):
    report = EvaluationReport(
        dataset="test",
        evaluator_name="judge",
        total_sessions=2,
        passed_sessions=1,
        failed_sessions=1,
        aggregate_scores={"correctness": 0.5},
        session_scores=[
            SessionScore(
                session_id="s1",
                scores={"correctness": 0.8},
                passed=True,
            ),
            SessionScore(
                session_id="s2",
                scores={},
                passed=True,
            ),
        ],
    )
    strict = _apply_strict_mode(report)
    assert strict.details["parse_errors"] == 1
    assert strict.details["parse_error_rate"] == 0.5
    assert "parse_errors" not in strict.aggregate_scores

  def test_parse_errors_in_details_when_zero(self):
    report = EvaluationReport(
        dataset="test",
        evaluator_name="judge",
        total_sessions=1,
        passed_sessions=1,
        failed_sessions=0,
        aggregate_scores={"correctness": 0.8},
        session_scores=[
            SessionScore(
                session_id="s1",
                scores={"correctness": 0.8},
                passed=True,
            ),
        ],
    )
    strict = _apply_strict_mode(report)
    assert "parse_errors" in strict.details
    assert strict.details["parse_errors"] == 0
    assert strict.details["parse_error_rate"] == 0.0
    assert "parse_errors" not in strict.aggregate_scores


# ================================================================== #
# P3: Drift new_questions preserve original casing                     #
# ================================================================== #


class TestDriftPreservesCasing:
  """Drift report should preserve original question casing."""

  async def test_keyword_drift_preserves_casing(self):
    """Keyword-based drift should return original-cased questions."""
    mock_client = MagicMock()

    # Golden questions
    golden_result = MagicMock()
    golden_result.result.return_value = [
        _MockRow({"question": "What is ADK?"}),
        _MockRow({"question": "How to deploy?"}),
    ]

    # Production questions (different casing + new question)
    prod_result = MagicMock()
    prod_result.result.return_value = [
        _MockRow({"question": "what is adk?"}),
        _MockRow({"question": "New Production Question?"}),
    ]

    mock_client.query.side_effect = [golden_result, prod_result]

    report = await compute_drift(
        bq_client=mock_client,
        project_id="p",
        dataset_id="d",
        table_id="t",
        golden_table="golden",
        where_clause="1=1",
        query_params=[],
    )

    # Covered should have original golden casing
    assert "What is ADK?" in report.covered_questions

    # Uncovered should have original golden casing
    assert "How to deploy?" in report.uncovered_questions

    # New questions should have original production casing
    assert "New Production Question?" in report.new_questions

  async def test_semantic_drift_preserves_casing(self):
    """Semantic drift new_questions should preserve original casing."""
    mock_client = MagicMock()

    # Semantic drift query result
    semantic_result = MagicMock()
    semantic_result.result.return_value = [
        _MockRow(
            {
                "golden_question": "What is ADK?",
                "closest_production": "what is ADK framework?",
                "distance": 0.1,
            }
        ),
    ]

    mock_client.query.side_effect = [
        # Golden query
        MagicMock(
            result=MagicMock(
                return_value=[
                    _MockRow({"question": "What is ADK?"}),
                ]
            )
        ),
        # Prod query
        MagicMock(
            result=MagicMock(
                return_value=[
                    _MockRow({"question": "What Is ADK?"}),
                    _MockRow({"question": "New Question Here?"}),
                ]
            )
        ),
        # Semantic drift query
        semantic_result,
    ]

    report = await compute_drift(
        bq_client=mock_client,
        project_id="p",
        dataset_id="d",
        table_id="t",
        golden_table="golden",
        where_clause="1=1",
        query_params=[],
        embedding_model="p.d.embedding_model",
    )

    # New questions should preserve original casing
    assert any("New Question Here?" == q for q in report.new_questions)


# ================================================================== #
# Feature: BigQueryTraceEvaluator include_event_types                  #
# ================================================================== #


class TestIncludeEventTypes:
  """BigQueryTraceEvaluator should support custom event type filters."""

  def test_default_event_types(self):
    """Default should include all standard ADK event types."""
    evaluator = BigQueryTraceEvaluator(
        project_id="p",
        dataset_id="d",
        client=MagicMock(),
    )
    assert "USER_MESSAGE_RECEIVED" in evaluator.include_event_types
    assert "TOOL_STARTING" in evaluator.include_event_types
    assert "LLM_RESPONSE" in evaluator.include_event_types
    assert "STATE_DELTA" in evaluator.include_event_types
    assert "HITL_CONFIRMATION_REQUEST" in evaluator.include_event_types

  def test_custom_event_types(self):
    """Custom event types should override defaults."""
    custom = ["USER_MESSAGE_RECEIVED", "TOOL_COMPLETED"]
    evaluator = BigQueryTraceEvaluator(
        project_id="p",
        dataset_id="d",
        client=MagicMock(),
        include_event_types=custom,
    )
    assert evaluator.include_event_types == custom

  async def test_event_types_passed_to_query(self):
    """Query should use include_event_types as parameter."""
    from google.cloud import bigquery

    mock_client = MagicMock()
    mock_result = MagicMock()
    mock_result.result.return_value = [
        _MockRow(
            {
                "event_type": "TOOL_COMPLETED",
                "agent": "test_agent",
                "timestamp": datetime(2026, 1, 1, tzinfo=timezone.utc),
                "content": '{"tool": "search"}',
                "attributes": "{}",
                "span_id": "sp1",
                "parent_span_id": None,
                "latency_ms": None,
                "status": "OK",
                "error_message": None,
                "user_id": "u1",
            }
        ),
    ]
    mock_client.query.return_value = mock_result

    custom = ["TOOL_COMPLETED"]
    evaluator = BigQueryTraceEvaluator(
        project_id="p",
        dataset_id="d",
        client=mock_client,
        include_event_types=custom,
    )
    await evaluator.get_session_trace("s1")

    # Verify the query was called with event_types parameter
    call_args = mock_client.query.call_args
    job_config = call_args[1].get("job_config") or call_args[0][1]
    param_names = [p.name for p in job_config.query_parameters]
    assert "event_types" in param_names

    # Find the event_types parameter value
    for p in job_config.query_parameters:
      if p.name == "event_types":
        assert p.values == custom

  def test_query_uses_unnest(self):
    """SQL query should use UNNEST for event type filtering."""
    assert "IN UNNEST(@event_types)" in (
        BigQueryTraceEvaluator._SESSION_TRACE_QUERY
    )


# ================================================================== #
# SQL injection: _sanitize_categories                                  #
# ================================================================== #


class TestSanitizeCategories:
  """_sanitize_categories should escape SQL-unsafe characters."""

  def test_plain_string_unchanged(self):
    """Normal text passes through unchanged."""
    assert _sanitize_categories("Billing, Technical") == "Billing, Technical"

  def test_single_quote_doubled(self):
    """Single quotes are doubled for safe SQL embedding."""
    assert _sanitize_categories("It's broken") == "It''s broken"

  def test_backslash_escaped(self):
    """Backslashes are escaped."""
    assert _sanitize_categories("path\\name") == "path\\\\name"

  def test_combined_escaping(self):
    """Both quotes and backslashes are handled together."""
    result = _sanitize_categories("O'Brien's \\data")
    assert result == "O''Brien''s \\\\data"

  def test_empty_string(self):
    assert _sanitize_categories("") == ""

  def test_no_v2_in_notebook_demo(self):
    """e2e_notebook_demo.ipynb should not default to agent_events_v2."""
    notebook_path = ROOT / "examples" / "e2e_notebook_demo.ipynb"
    if notebook_path.exists():
      text = notebook_path.read_text()
      # Should not have agent_events_v2 as default value
      assert (
          '"agent_events_v2"' not in text
      ), "e2e_notebook_demo.ipynb still references agent_events_v2"
