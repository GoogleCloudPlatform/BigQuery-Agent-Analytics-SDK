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

"""Tests for legacy LLMAsJudge adapter and backward-compatibility re-exports, shims, and deprecation warnings in evaluators.py."""

from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from bigquery_agent_analytics.evaluators import LLMAsJudge
from bigquery_agent_analytics.evaluators import render_ai_generate_judge_query
from bigquery_agent_analytics.evaluators import split_judge_prompt_template
from bigquery_agent_analytics.system_evaluator import SessionScore


@pytest.mark.asyncio
async def test_llm_as_judge_correctness():
  """Test LLMAsJudge.correctness factory."""
  judge = LLMAsJudge.correctness(threshold=0.7)
  assert judge.name == "correctness_judge"
  assert len(judge._criteria) == 1
  assert judge._criteria[0].name == "correctness"
  assert judge._criteria[0].threshold == 0.7


@pytest.mark.asyncio
async def test_llm_as_judge_add_criterion():
  """Test adding custom criterion."""
  judge = LLMAsJudge(name="custom_judge")
  judge.add_criterion(
      name="my_metric",
      prompt_template="Rate {trace_text}",
      score_key="score",
      threshold=0.6,
  )

  assert len(judge._criteria) == 1
  assert judge._criteria[0].name == "my_metric"


@pytest.mark.asyncio
async def test_llm_as_judge_evaluate_session():
  """Test evaluate_session with mocked genai."""
  judge = LLMAsJudge.correctness(threshold=0.5)

  mock_response = MagicMock()
  mock_response.text = '{"correctness": 8, "justification": "Good job"}'

  mock_client_instance = MagicMock()
  mock_client_instance.aio.models.generate_content = AsyncMock(
      return_value=mock_response
  )

  with patch("google.genai.Client", return_value=mock_client_instance):
    score = await judge.evaluate_session(
        trace_text="User: hi\nAgent: hello",
        final_response="hello",
    )

    assert isinstance(score, SessionScore)
    assert score.scores["correctness"] == 0.8
    assert score.passed is True
    assert "correctness: Good job" in score.llm_feedback


@pytest.mark.asyncio
async def test_llm_as_judge_evaluate_session_fail():
  """Test evaluate_session when score is below threshold."""
  judge = LLMAsJudge.correctness(threshold=0.9)

  mock_response = MagicMock()
  mock_response.text = '{"correctness": 5, "justification": "Okay job"}'

  mock_client_instance = MagicMock()
  mock_client_instance.aio.models.generate_content = AsyncMock(
      return_value=mock_response
  )

  with patch("google.genai.Client", return_value=mock_client_instance):
    score = await judge.evaluate_session(
        trace_text="User: hi\nAgent: hello",
        final_response="hello",
    )

    assert score.scores["correctness"] == 0.5
    assert score.passed is False


@pytest.mark.asyncio
async def test_llm_as_judge_correctness_model_override():
  """Test LLMAsJudge.correctness with model override."""
  judge = LLMAsJudge.correctness(model="gemini-custom")
  assert judge.llm_judge_model == "gemini-custom"


def test_split_judge_prompt_template():
  """Test split_judge_prompt_template helper."""
  prompt = "Score this.\nTrace:\n{trace_text}\nResponse:\n{final_response}\nJSON only."

  with pytest.deprecated_call():
    prefix, middle, suffix = split_judge_prompt_template(prompt)

  assert prefix == "Score this.\nTrace:\n"
  assert middle == "\nResponse:\n"
  assert suffix == "\nJSON only."


def test_render_ai_generate_judge_query():
  """Test render_ai_generate_judge_query helper."""
  with pytest.deprecated_call():
    query = render_ai_generate_judge_query(
        project="my-project",
        dataset="my_dataset",
        table="my_table",
        where="session_id = '123'",
        endpoint="gemini-1.5-flash",
    )

  assert "FROM `my-project.my_dataset.my_table`" in query
  assert "WHERE session_id = '123'" in query
  assert "endpoint => 'gemini-1.5-flash'" in query
  assert "connection_id" not in query


def test_render_ai_generate_judge_query_with_connection():
  """Test render_ai_generate_judge_query helper with connection_id."""
  with pytest.deprecated_call():
    query = render_ai_generate_judge_query(
        project="my-project",
        dataset="my_dataset",
        table="my_table",
        where="session_id = '123'",
        endpoint="gemini-1.5-flash",
        connection_id="us.my-connection",
    )

  assert "connection_id => 'us.my-connection'" in query


def test_deprecated_constants():
  """Assert that importing/accessing legacy constants raises a DeprecationWarning."""
  import bigquery_agent_analytics.evaluators as ev

  with pytest.deprecated_call():
    q1 = ev.AI_GENERATE_JUDGE_BATCH_QUERY
  assert "AI.GENERATE(" in q1

  with pytest.deprecated_call():
    q2 = ev.LLM_JUDGE_BATCH_QUERY
  assert "ML.GENERATE_TEXT(" in q2


def test_evaluators_module_getattr_fallback():
  """Assert that module __getattr__ correctly raises AttributeError for non-existent attributes."""
  import bigquery_agent_analytics.evaluators as ev

  with pytest.raises(AttributeError, match="has no attribute 'NON_EXISTENT'"):
    _ = ev.NON_EXISTENT
