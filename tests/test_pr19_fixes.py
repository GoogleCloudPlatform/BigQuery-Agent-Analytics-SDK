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

"""Tests for PR #19 fixes.

Covers:
  P1 - Drift metrics use deduped keys for totals and coverage
  P3 - parse_errors moved from aggregate_scores to report.details
  Feature - EvaluationReport.details field for operational metadata
  Feature - Drift exposes raw_count and unique_count in details
  Feature - Docs consistency check for agent_events_v2
"""

from __future__ import annotations

from pathlib import Path
import re
from unittest.mock import MagicMock

import pytest

from bigquery_agent_analytics.client import _apply_strict_mode
from bigquery_agent_analytics.evaluators import EvaluationReport
from bigquery_agent_analytics.evaluators import SessionScore
from bigquery_agent_analytics.feedback import compute_drift

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


# ================================================================== #
# P1: Drift metrics consistency with duplicates                        #
# ================================================================== #


class TestDriftDedupedMetrics:
  """Drift totals and coverage must use deduped (unique) counts."""

  async def test_keyword_drift_dedupes_golden(self):
    """Duplicate golden questions should not inflate total_golden."""
    mock_client = MagicMock()

    golden_result = MagicMock()
    golden_result.result.return_value = [
        _MockRow({"question": "What is ADK?"}),
        _MockRow({"question": "What is ADK?"}),  # duplicate
        _MockRow({"question": "How to deploy?"}),
    ]

    prod_result = MagicMock()
    prod_result.result.return_value = [
        _MockRow({"question": "what is adk?"}),
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

    # 2 unique golden questions, not 3 raw rows
    assert report.total_golden == 2
    assert report.details["raw_golden_count"] == 3
    assert report.details["unique_golden_count"] == 2

  async def test_keyword_drift_dedupes_production(self):
    """Duplicate production questions should not inflate total_production."""
    mock_client = MagicMock()

    golden_result = MagicMock()
    golden_result.result.return_value = [
        _MockRow({"question": "Q1"}),
    ]

    prod_result = MagicMock()
    prod_result.result.return_value = [
        _MockRow({"question": "Q1"}),
        _MockRow({"question": "Q1"}),  # duplicate
        _MockRow({"question": "Q2"}),
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

    assert report.total_production == 2
    assert report.details["raw_production_count"] == 3
    assert report.details["unique_production_count"] == 2

  async def test_coverage_percentage_uses_deduped_golden(self):
    """Coverage % denominator should be unique golden count."""
    mock_client = MagicMock()

    # 3 raw rows but only 2 unique golden questions
    golden_result = MagicMock()
    golden_result.result.return_value = [
        _MockRow({"question": "Alpha"}),
        _MockRow({"question": "Alpha"}),  # duplicate
        _MockRow({"question": "Beta"}),
    ]

    # Production covers "alpha" only
    prod_result = MagicMock()
    prod_result.result.return_value = [
        _MockRow({"question": "alpha"}),
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

    # 1 covered out of 2 unique = 50%, not 1/3 = 33%
    assert report.coverage_percentage == pytest.approx(50.0)
    assert report.total_golden == 2
    assert len(report.covered_questions) == 1
    assert len(report.uncovered_questions) == 1

  async def test_keyword_drift_details_has_method(self):
    """Keyword drift details should include method."""
    mock_client = MagicMock()
    golden_result = MagicMock()
    golden_result.result.return_value = [_MockRow({"question": "Q"})]
    prod_result = MagicMock()
    prod_result.result.return_value = []
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
    assert report.details["method"] == "keyword_overlap"

  async def test_semantic_drift_dedupes_totals(self):
    """Semantic drift should also use deduped counts."""
    mock_client = MagicMock()

    # Golden: 3 rows, 2 unique
    golden_result = MagicMock()
    golden_result.result.return_value = [
        _MockRow({"question": "What is ADK?"}),
        _MockRow({"question": "What is ADK?"}),
        _MockRow({"question": "How to deploy?"}),
    ]

    # Prod: 2 rows, 2 unique
    prod_result = MagicMock()
    prod_result.result.return_value = [
        _MockRow({"question": "What Is ADK?"}),
        _MockRow({"question": "New Question?"}),
    ]

    # Semantic query result
    semantic_result = MagicMock()
    semantic_result.result.return_value = [
        _MockRow(
            {
                "golden_question": "What is ADK?",
                "closest_production": "What Is ADK?",
                "distance": 0.1,
            }
        ),
        _MockRow(
            {
                "golden_question": "How to deploy?",
                "closest_production": "What Is ADK?",
                "distance": 0.8,
            }
        ),
    ]

    mock_client.query.side_effect = [
        golden_result,
        prod_result,
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

    assert report.total_golden == 2  # unique, not 3
    assert report.total_production == 2  # unique
    assert report.details["raw_golden_count"] == 3
    assert report.details["unique_golden_count"] == 2


# ================================================================== #
# P3/Feature: parse_errors in report.details, not aggregate_scores     #
# ================================================================== #


class TestParseErrorsInDetails:
  """parse_errors and parse_error_rate belong in report.details."""

  def test_parse_errors_not_in_aggregate_scores(self):
    """aggregate_scores should not contain parse_errors."""
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
            SessionScore(session_id="s2", scores={}, passed=True),
        ],
    )
    strict = _apply_strict_mode(report)
    assert "parse_errors" not in strict.aggregate_scores
    assert "correctness" in strict.aggregate_scores

  def test_parse_errors_in_details(self):
    """details should contain parse_errors count and rate."""
    report = EvaluationReport(
        dataset="test",
        evaluator_name="judge",
        total_sessions=4,
        passed_sessions=3,
        failed_sessions=1,
        aggregate_scores={"correctness": 0.7},
        session_scores=[
            SessionScore(
                session_id="s1",
                scores={"correctness": 0.8},
                passed=True,
            ),
            SessionScore(session_id="s2", scores={}, passed=True),
            SessionScore(session_id="s3", scores={}, passed=True),
            SessionScore(
                session_id="s4",
                scores={"correctness": 0.6},
                passed=True,
            ),
        ],
    )
    strict = _apply_strict_mode(report)
    assert strict.details["parse_errors"] == 2
    assert strict.details["parse_error_rate"] == pytest.approx(0.5)

  def test_zero_parse_errors_in_details(self):
    """details should still contain parse_errors=0 when no errors."""
    report = EvaluationReport(
        dataset="test",
        evaluator_name="judge",
        total_sessions=1,
        passed_sessions=1,
        failed_sessions=0,
        aggregate_scores={"correctness": 0.9},
        session_scores=[
            SessionScore(
                session_id="s1",
                scores={"correctness": 0.9},
                passed=True,
            ),
        ],
    )
    strict = _apply_strict_mode(report)
    assert strict.details["parse_errors"] == 0
    assert strict.details["parse_error_rate"] == 0.0

  def test_aggregate_scores_preserved(self):
    """Original aggregate_scores should pass through unchanged."""
    report = EvaluationReport(
        dataset="test",
        evaluator_name="judge",
        total_sessions=1,
        passed_sessions=1,
        failed_sessions=0,
        aggregate_scores={"correctness": 0.9, "sentiment": 0.8},
        session_scores=[
            SessionScore(
                session_id="s1",
                scores={"correctness": 0.9},
                passed=True,
            ),
        ],
    )
    strict = _apply_strict_mode(report)
    assert strict.aggregate_scores == {"correctness": 0.9, "sentiment": 0.8}

  def test_strict_parse_error_preserves_attribution_and_existing_details(self):
    original_details = {
        "user_id": "alice",
        "root_agent_name": "root",
        "scope_signature": "scope-v1",
        "judge_metadata": {"attempt": 2},
    }
    report = EvaluationReport(
        dataset="test",
        evaluator_name="judge",
        total_sessions=1,
        passed_sessions=1,
        failed_sessions=0,
        session_scores=[
            SessionScore(
                session_id="s1",
                scores={},
                passed=True,
                details=original_details,
            ),
        ],
    )

    strict = _apply_strict_mode(report)

    assert strict.session_scores[0].details == {
        **original_details,
        "parse_error": True,
    }


# ================================================================== #
# Feature: EvaluationReport.details field exists                       #
# ================================================================== #


class TestEvaluationReportDetails:
  """EvaluationReport should have a details dict for operational metadata."""

  def test_details_field_exists(self):
    report = EvaluationReport(
        dataset="test",
        evaluator_name="eval",
        total_sessions=0,
    )
    assert hasattr(report, "details")
    assert isinstance(report.details, dict)

  def test_details_defaults_to_empty(self):
    report = EvaluationReport(
        dataset="test",
        evaluator_name="eval",
    )
    assert report.details == {}

  def test_details_accepts_values(self):
    report = EvaluationReport(
        dataset="test",
        evaluator_name="eval",
        details={"fallback_mode": "gemini_api", "query_path": "ai_generate"},
    )
    assert report.details["fallback_mode"] == "gemini_api"


# ================================================================== #
# Feature: Docs consistency check for agent_events_v2                  #
# ================================================================== #


class TestDocsConsistency:
  """No source default values should reference agent_events_v2."""

  def test_no_v2_default_in_source_modules(self):
    """Source modules should not use agent_events_v2 as a default."""
    src_dir = ROOT / "src" / "bigquery_agent_analytics"
    bad = []
    for path in src_dir.rglob("*.py"):
      try:
        text = path.read_text()
      except (UnicodeDecodeError, IsADirectoryError):
        continue
      for i, line in enumerate(text.splitlines(), 1):
        if "agent_events_v2" not in line:
          continue
        # Match default value assignments
        if re.search(r'=\s*["\']agent_events_v2["\']', line):
          # Exclude auto-detect lists and schema queries
          if "AUTO_DETECT" in line or "table_name" in line:
            continue
          bad.append(
              (
                  str(path.relative_to(ROOT)),
                  i,
                  line.strip(),
              )
          )
    assert not bad, "Found agent_events_v2 as default value:\n" + "\n".join(
        f"  {p}:{n}: {l}" for p, n, l in bad
    )

  def test_no_v2_default_in_examples(self):
    """Example files should not use agent_events_v2 as default."""
    examples_dir = ROOT / "examples"
    if not examples_dir.exists():
      return
    bad = []
    for path in examples_dir.rglob("*"):
      if path.suffix not in (".py", ".ipynb"):
        continue
      try:
        text = path.read_text()
      except (UnicodeDecodeError, IsADirectoryError):
        continue
      if '"agent_events_v2"' in text:
        bad.append(str(path.relative_to(ROOT)))
    assert not bad, "Found agent_events_v2 in examples:\n" + "\n".join(
        f"  {p}" for p in bad
    )

  def test_no_v2_in_trace_evaluator_docstrings(self):
    """trace_evaluator.py docstrings should not reference v2."""
    text = (
        ROOT / "src" / "bigquery_agent_analytics" / "trace_evaluator.py"
    ).read_text()
    docstring_matches = re.findall(
        r'(?:""".*?agent_events_v2.*?""")',
        text,
        re.DOTALL,
    )
    assert (
        not docstring_matches
    ), "Found agent_events_v2 in trace_evaluator.py docstrings"
