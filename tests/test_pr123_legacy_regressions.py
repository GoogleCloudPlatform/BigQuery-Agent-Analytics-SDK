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

"""Legacy judge scores retain their fixed ten-point scale after the refactor."""

import json
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from bigquery_agent_analytics.evaluators import _JudgeCriterion
from bigquery_agent_analytics.evaluators import LLMAsJudge


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "raw,normalized,passed",
    [(0, 0.0, False), (1, 0.1, False), (5, 0.5, True), (10, 1.0, True)],
)
async def test_legacy_score_scale(raw, normalized, passed):
  client = MagicMock()
  client.aio.models.generate_content = AsyncMock(
      return_value=MagicMock(text=json.dumps({"correctness": raw}))
  )
  with patch("google.genai.Client", return_value=client):
    result = await LLMAsJudge.correctness().evaluate_session("trace", "answer")
  assert result.scores == {"correctness": normalized}
  assert result.passed is passed


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", [True, -1, 11, "NaN", "Infinity", None])
async def test_invalid_legacy_scores_cannot_pass(raw):
  client = MagicMock()
  client.aio.models.generate_content = AsyncMock(
      return_value=MagicMock(text=json.dumps({"correctness": raw}))
  )
  with patch("google.genai.Client", return_value=client):
    result = await LLMAsJudge.correctness().evaluate_session("trace", "answer")
  assert result.scores == {"correctness": 0.0}
  assert not result.passed


@pytest.mark.asyncio
async def test_empty_legacy_judge_does_not_pass():
  result = await LLMAsJudge().evaluate_session("trace", "answer")
  assert not result.passed


def test_existing_constructor_and_model_alias_remain_compatible():
  criterion = _JudgeCriterion("custom", "{trace_text}", "score", 0.7)
  judge = LLMAsJudge("custom", [criterion], "custom-model")
  assert judge._criteria == [criterion]
  assert judge.model == judge.llm_judge_model == "custom-model"
  judge.llm_judge_model = "updated"
  assert judge.model == "updated"


def test_legacy_wildcard_exports_keep_shared_report_and_sql_symbols():
  import bigquery_agent_analytics.evaluators as legacy
  import bigquery_agent_analytics.system_evaluator as system

  for name in ("SessionScore", "EvaluationReport", "SESSION_SUMMARY_QUERY"):
    assert name in legacy.__all__
    assert getattr(legacy, name) is getattr(system, name)
  assert "DEFAULT_ENDPOINT" in legacy.__all__
  assert "strip_markdown_fences" in legacy.__all__
