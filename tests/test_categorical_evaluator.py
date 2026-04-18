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

"""Tests for the categorical evaluator module."""

import asyncio
import json
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from bigquery_agent_analytics.categorical_evaluator import build_ai_classify_query
from bigquery_agent_analytics.categorical_evaluator import build_ai_generate_query
from bigquery_agent_analytics.categorical_evaluator import build_categorical_prompt
from bigquery_agent_analytics.categorical_evaluator import build_categorical_report
from bigquery_agent_analytics.categorical_evaluator import build_classify_categories_literal
from bigquery_agent_analytics.categorical_evaluator import CATEGORICAL_AI_GENERATE_QUERY
from bigquery_agent_analytics.categorical_evaluator import CATEGORICAL_RESULTS_DDL
from bigquery_agent_analytics.categorical_evaluator import CATEGORICAL_TRANSCRIPT_QUERY
from bigquery_agent_analytics.categorical_evaluator import CategoricalEvaluationConfig
from bigquery_agent_analytics.categorical_evaluator import CategoricalEvaluationReport
from bigquery_agent_analytics.categorical_evaluator import CategoricalMetricCategory
from bigquery_agent_analytics.categorical_evaluator import CategoricalMetricDefinition
from bigquery_agent_analytics.categorical_evaluator import CategoricalMetricResult
from bigquery_agent_analytics.categorical_evaluator import CategoricalSessionResult
from bigquery_agent_analytics.categorical_evaluator import classify_sessions_via_api
from bigquery_agent_analytics.categorical_evaluator import flatten_results_to_rows
from bigquery_agent_analytics.categorical_evaluator import parse_categorical_row
from bigquery_agent_analytics.categorical_evaluator import parse_classifications
from bigquery_agent_analytics.categorical_evaluator import parse_classify_row

# ------------------------------------------------------------------ #
# Helpers                                                              #
# ------------------------------------------------------------------ #


def _make_config(include_justification=True):
  """Builds a two-metric config for testing."""
  return CategoricalEvaluationConfig(
      metrics=[
          CategoricalMetricDefinition(
              name="tone",
              definition="Overall tone of the conversation.",
              categories=[
                  CategoricalMetricCategory(
                      name="positive",
                      definition="User is satisfied.",
                  ),
                  CategoricalMetricCategory(
                      name="negative",
                      definition="User is frustrated.",
                  ),
                  CategoricalMetricCategory(
                      name="neutral",
                      definition="No strong sentiment.",
                  ),
              ],
          ),
          CategoricalMetricDefinition(
              name="safety",
              definition="Whether the response is safe.",
              categories=[
                  CategoricalMetricCategory(
                      name="safe",
                      definition="Response is safe.",
                  ),
                  CategoricalMetricCategory(
                      name="unsafe",
                      definition="Response contains unsafe content.",
                  ),
              ],
          ),
      ],
      include_justification=include_justification,
  )


# ------------------------------------------------------------------ #
# Model Tests                                                          #
# ------------------------------------------------------------------ #


class TestCategoricalModels:
  """Tests for Pydantic config and result models."""

  def test_metric_category_fields(self):
    cat = CategoricalMetricCategory(name="good", definition="It is good.")
    assert cat.name == "good"
    assert cat.definition == "It is good."

  def test_metric_definition_defaults(self):
    defn = CategoricalMetricDefinition(
        name="tone",
        definition="Tone.",
        categories=[
            CategoricalMetricCategory(name="a", definition="A."),
        ],
    )
    assert defn.required is True

  def test_config_defaults(self):
    config = _make_config()
    assert config.endpoint == "gemini-2.5-flash"
    assert config.temperature == 0.0
    assert config.persist_results is False
    assert config.include_justification is True
    assert config.prompt_version is None
    assert config.results_table is None

  def test_metric_result_defaults(self):
    result = CategoricalMetricResult(metric_name="tone")
    assert result.category is None
    assert result.passed_validation is True
    assert result.parse_error is False
    assert result.justification is None
    assert result.raw_response is None

  def test_session_result_defaults(self):
    sr = CategoricalSessionResult(session_id="s1")
    assert sr.metrics == []
    assert sr.details == {}

  def test_report_defaults(self):
    report = CategoricalEvaluationReport(dataset="test")
    assert report.total_sessions == 0
    assert report.evaluator_name == "categorical_evaluator"
    assert report.category_distributions == {}
    assert report.details == {}
    assert report.session_results == []
    assert report.created_at is not None


# ------------------------------------------------------------------ #
# Prompt Builder Tests                                                 #
# ------------------------------------------------------------------ #


class TestBuildCategoricalPrompt:
  """Tests for build_categorical_prompt."""

  def test_includes_metric_names(self):
    prompt = build_categorical_prompt(_make_config())
    assert "tone" in prompt
    assert "safety" in prompt

  def test_includes_category_names(self):
    prompt = build_categorical_prompt(_make_config())
    assert "positive" in prompt
    assert "negative" in prompt
    assert "neutral" in prompt
    assert "safe" in prompt
    assert "unsafe" in prompt

  def test_includes_definitions(self):
    prompt = build_categorical_prompt(_make_config())
    assert "User is satisfied" in prompt
    assert "Whether the response is safe" in prompt

  def test_includes_json_format_instruction(self):
    prompt = build_categorical_prompt(_make_config())
    assert "JSON array" in prompt
    assert "metric_name" in prompt
    assert "category" in prompt

  def test_includes_example(self):
    prompt = build_categorical_prompt(_make_config())
    # The example should be valid JSON.
    example_start = prompt.rfind("[")
    example_end = prompt.rfind("]") + 1
    example = json.loads(prompt[example_start:example_end])
    assert len(example) == 2
    assert example[0]["metric_name"] == "tone"

  def test_no_justification(self):
    prompt = build_categorical_prompt(_make_config(include_justification=False))
    assert "Do not include" in prompt
    # The output spec after the instruction lines should not list
    # justification as a required field.
    after_spec = prompt.split("Each element must have:")[1]
    spec_lines = after_spec.split("Example")[0]
    assert '"justification"' not in spec_lines


# ------------------------------------------------------------------ #
# Parse Classifications Tests                                          #
# ------------------------------------------------------------------ #


class TestParseClassifications:
  """Tests for parse_classifications."""

  def test_valid_json(self):
    config = _make_config()
    raw = json.dumps(
        [
            {
                "metric_name": "tone",
                "category": "positive",
                "justification": "kind",
            },
            {
                "metric_name": "safety",
                "category": "safe",
                "justification": "ok",
            },
        ]
    )
    results = parse_classifications(raw, config)
    assert len(results) == 2
    assert results[0].metric_name == "tone"
    assert results[0].category == "positive"
    assert results[0].passed_validation is True
    assert results[0].parse_error is False
    assert results[0].justification == "kind"
    assert results[1].metric_name == "safety"
    assert results[1].category == "safe"

  def test_invalid_category(self):
    config = _make_config()
    raw = json.dumps(
        [
            {"metric_name": "tone", "category": "unknown_val"},
            {"metric_name": "safety", "category": "safe"},
        ]
    )
    results = parse_classifications(raw, config)
    tone = results[0]
    assert tone.parse_error is True
    assert tone.passed_validation is False
    safety = results[1]
    assert safety.parse_error is False
    assert safety.passed_validation is True

  def test_missing_metric(self):
    config = _make_config()
    raw = json.dumps(
        [
            {"metric_name": "tone", "category": "positive"},
        ]
    )
    results = parse_classifications(raw, config)
    assert len(results) == 2
    safety = results[1]
    assert safety.metric_name == "safety"
    assert safety.parse_error is True
    assert safety.passed_validation is False

  def test_malformed_json(self):
    config = _make_config()
    results = parse_classifications("not json at all", config)
    assert len(results) == 2
    assert all(r.parse_error is True for r in results)
    assert all(r.passed_validation is False for r in results)

  def test_empty_input(self):
    config = _make_config()
    results = parse_classifications("", config)
    assert len(results) == 2
    assert all(r.parse_error is True for r in results)

  def test_none_input(self):
    config = _make_config()
    results = parse_classifications(None, config)
    assert len(results) == 2
    assert all(r.parse_error is True for r in results)

  def test_case_insensitive(self):
    config = _make_config()
    raw = json.dumps(
        [
            {"metric_name": "tone", "category": "POSITIVE"},
            {"metric_name": "safety", "category": "Safe"},
        ]
    )
    results = parse_classifications(raw, config)
    assert results[0].category == "positive"
    assert results[0].passed_validation is True
    assert results[1].category == "safe"
    assert results[1].passed_validation is True

  def test_extra_whitespace(self):
    config = _make_config()
    raw = json.dumps(
        [
            {"metric_name": "tone", "category": "  positive  "},
            {"metric_name": "safety", "category": "safe"},
        ]
    )
    results = parse_classifications(raw, config)
    assert results[0].category == "positive"
    assert results[0].passed_validation is True

  def test_unknown_metric_ignored(self):
    config = _make_config()
    raw = json.dumps(
        [
            {"metric_name": "tone", "category": "positive"},
            {"metric_name": "safety", "category": "safe"},
            {"metric_name": "bogus", "category": "whatever"},
        ]
    )
    results = parse_classifications(raw, config)
    assert len(results) == 2

  def test_duplicate_metric_flagged_as_error(self):
    config = _make_config()
    raw = json.dumps(
        [
            {"metric_name": "tone", "category": "positive"},
            {"metric_name": "tone", "category": "negative"},
            {"metric_name": "safety", "category": "safe"},
        ]
    )
    results = parse_classifications(raw, config)
    tone = results[0]
    assert tone.parse_error is True
    assert tone.passed_validation is False
    # The duplicate should wipe the category — it's ambiguous.
    assert tone.category is None
    # safety should be unaffected.
    safety = results[1]
    assert safety.category == "safe"
    assert safety.passed_validation is True

  def test_single_object_not_array(self):
    config = CategoricalEvaluationConfig(
        metrics=[
            CategoricalMetricDefinition(
                name="tone",
                definition="Tone.",
                categories=[
                    CategoricalMetricCategory(
                        name="positive",
                        definition="Good.",
                    ),
                ],
            ),
        ],
    )
    raw = json.dumps({"metric_name": "tone", "category": "positive"})
    results = parse_classifications(raw, config)
    assert len(results) == 1
    assert results[0].category == "positive"

  def test_markdown_json_fence(self):
    """parse_classifications should handle ```json fenced responses."""
    config = _make_config()
    inner = json.dumps(
        [
            {
                "metric_name": "tone",
                "category": "positive",
                "justification": "ok",
            },
            {
                "metric_name": "safety",
                "category": "safe",
                "justification": "fine",
            },
        ]
    )
    raw = f"```json\n{inner}\n```"
    results = parse_classifications(raw, config)
    assert len(results) == 2
    assert results[0].category == "positive"
    assert results[0].parse_error is False
    assert results[1].category == "safe"
    assert results[1].parse_error is False

  def test_markdown_plain_fence(self):
    """parse_classifications should handle plain ``` fenced responses."""
    config = _make_config()
    inner = json.dumps(
        [
            {"metric_name": "tone", "category": "positive"},
            {"metric_name": "safety", "category": "safe"},
        ]
    )
    raw = f"```\n{inner}\n```"
    results = parse_classifications(raw, config)
    assert len(results) == 2
    assert results[0].category == "positive"
    assert results[0].parse_error is False

  def test_markdown_fence_no_newline(self):
    """Handle ```json without newline after opening fence."""
    config = _make_config()
    inner = json.dumps(
        [
            {"metric_name": "tone", "category": "positive"},
            {"metric_name": "safety", "category": "safe"},
        ]
    )
    raw = f"```json{inner}```"
    results = parse_classifications(raw, config)
    assert len(results) == 2
    assert results[0].category == "positive"
    assert results[0].parse_error is False


# ------------------------------------------------------------------ #
# strip_markdown_fences Tests                                          #
# ------------------------------------------------------------------ #


class TestStripMarkdownFences:
  """Tests for the shared strip_markdown_fences helper."""

  def test_json_fence(self):
    from bigquery_agent_analytics.evaluators import strip_markdown_fences

    assert strip_markdown_fences('```json\n{"a": 1}\n```') == '{"a": 1}'

  def test_plain_fence(self):
    from bigquery_agent_analytics.evaluators import strip_markdown_fences

    assert strip_markdown_fences("```\n[1, 2]\n```") == "[1, 2]"

  def test_no_fence(self):
    from bigquery_agent_analytics.evaluators import strip_markdown_fences

    assert strip_markdown_fences('{"a": 1}') == '{"a": 1}'

  def test_empty(self):
    from bigquery_agent_analytics.evaluators import strip_markdown_fences

    assert strip_markdown_fences("") == ""

  def test_none(self):
    from bigquery_agent_analytics.evaluators import strip_markdown_fences

    assert strip_markdown_fences(None) is None

  def test_no_newline_after_fence(self):
    from bigquery_agent_analytics.evaluators import strip_markdown_fences

    assert strip_markdown_fences('```json{"a": 1}```') == '{"a": 1}'

  def test_whitespace_around(self):
    from bigquery_agent_analytics.evaluators import strip_markdown_fences

    result = strip_markdown_fences('  ```json\n  {"a": 1}  \n```  ')
    assert '"a": 1' in result

  def test_sql_fence(self):
    from bigquery_agent_analytics.evaluators import strip_markdown_fences

    assert strip_markdown_fences("```sql\nSELECT 1\n```") == "SELECT 1"

  def test_uppercase_language_tag(self):
    from bigquery_agent_analytics.evaluators import strip_markdown_fences

    assert strip_markdown_fences('```JSON\n{"a": 1}\n```') == '{"a": 1}'

  def test_unknown_language_tag(self):
    from bigquery_agent_analytics.evaluators import strip_markdown_fences

    assert strip_markdown_fences("```python\nprint('hi')\n```") == "print('hi')"

  def test_truncated_fence_no_closing(self):
    from bigquery_agent_analytics.evaluators import strip_markdown_fences

    assert strip_markdown_fences('```json\n{"a": 1}') == '{"a": 1}'

  def test_trailing_content_after_fence(self):
    from bigquery_agent_analytics.evaluators import strip_markdown_fences

    result = strip_markdown_fences(
        '```json\n{"score": 1}\n```\nHere\'s my analysis...'
    )
    assert result == '{"score": 1}'

  def test_language_tag_with_digits(self):
    from bigquery_agent_analytics.evaluators import strip_markdown_fences

    assert strip_markdown_fences("```json5\n{}\n```") == "{}"


# ------------------------------------------------------------------ #
# Parse Row Tests                                                      #
# ------------------------------------------------------------------ #


class TestParseCategoricalRow:
  """Tests for parse_categorical_row."""

  def test_valid_row(self):
    config = _make_config()
    raw = json.dumps(
        [
            {"metric_name": "tone", "category": "positive"},
            {"metric_name": "safety", "category": "safe"},
        ]
    )
    row = {
        "session_id": "s1",
        "transcript": "some text",
        "classifications": raw,
    }
    result = parse_categorical_row("s1", row, config)
    assert result.session_id == "s1"
    assert len(result.metrics) == 2
    assert result.metrics[0].category == "positive"
    assert result.metrics[1].category == "safe"

  def test_missing_classifications_column(self):
    config = _make_config()
    row = {"session_id": "s1", "transcript": "text"}
    result = parse_categorical_row("s1", row, config)
    assert len(result.metrics) == 2
    assert all(m.parse_error is True for m in result.metrics)


# ------------------------------------------------------------------ #
# Report Builder Tests                                                 #
# ------------------------------------------------------------------ #


class TestBuildCategoricalReport:
  """Tests for build_categorical_report."""

  def test_aggregation(self):
    config = _make_config()
    sessions = [
        CategoricalSessionResult(
            session_id="s1",
            metrics=[
                CategoricalMetricResult(
                    metric_name="tone", category="positive"
                ),
                CategoricalMetricResult(metric_name="safety", category="safe"),
            ],
        ),
        CategoricalSessionResult(
            session_id="s2",
            metrics=[
                CategoricalMetricResult(
                    metric_name="tone", category="positive"
                ),
                CategoricalMetricResult(
                    metric_name="safety", category="unsafe"
                ),
            ],
        ),
        CategoricalSessionResult(
            session_id="s3",
            metrics=[
                CategoricalMetricResult(
                    metric_name="tone", category="negative"
                ),
                CategoricalMetricResult(metric_name="safety", category="safe"),
            ],
        ),
    ]

    report = build_categorical_report("test_ds", sessions, config)
    assert report.total_sessions == 3
    assert report.category_distributions["tone"]["positive"] == 2
    assert report.category_distributions["tone"]["negative"] == 1
    assert report.category_distributions["safety"]["safe"] == 2
    assert report.category_distributions["safety"]["unsafe"] == 1
    assert report.details["parse_errors"] == 0
    assert report.details["parse_error_rate"] == 0.0

  def test_parse_error_counting(self):
    config = _make_config()
    sessions = [
        CategoricalSessionResult(
            session_id="s1",
            metrics=[
                CategoricalMetricResult(
                    metric_name="tone",
                    category="positive",
                ),
                CategoricalMetricResult(
                    metric_name="safety",
                    parse_error=True,
                    passed_validation=False,
                ),
            ],
        ),
    ]
    report = build_categorical_report("test_ds", sessions, config)
    assert report.details["parse_errors"] == 1
    # 1 error out of 2 total classifications.
    assert report.details["parse_error_rate"] == 0.5

  def test_empty_sessions(self):
    config = _make_config()
    report = build_categorical_report("test_ds", [], config)
    assert report.total_sessions == 0
    assert report.details["parse_errors"] == 0
    assert report.details["parse_error_rate"] == 0.0

  def test_summary(self):
    config = _make_config()
    sessions = [
        CategoricalSessionResult(
            session_id="s1",
            metrics=[
                CategoricalMetricResult(
                    metric_name="tone", category="positive"
                ),
                CategoricalMetricResult(metric_name="safety", category="safe"),
            ],
        ),
    ]
    report = build_categorical_report("test_ds", sessions, config)
    text = report.summary()
    assert "categorical_evaluator" in text
    assert "tone" in text
    assert "positive" in text


# ------------------------------------------------------------------ #
# SQL Template Tests                                                   #
# ------------------------------------------------------------------ #


class TestCategoricalAIGenerateQuery:
  """Tests for the SQL template constant."""

  def test_contains_ai_generate(self):
    assert "AI.GENERATE" in CATEGORICAL_AI_GENERATE_QUERY

  def test_contains_output_schema(self):
    assert "output_schema" in CATEGORICAL_AI_GENERATE_QUERY

  def test_contains_classifications_string(self):
    assert "classifications STRING" in CATEGORICAL_AI_GENERATE_QUERY

  def test_contains_endpoint_placeholder(self):
    assert "{endpoint}" in CATEGORICAL_AI_GENERATE_QUERY

  def test_does_not_use_legacy_ml_generate(self):
    assert "ML.GENERATE_TEXT" not in CATEGORICAL_AI_GENERATE_QUERY

  def test_scalar_function_shape(self):
    """AI.GENERATE is a scalar function — prompt is a positional arg,
    result is accessed via .classifications on the returned STRUCT."""
    assert ")).classifications" in CATEGORICAL_AI_GENERATE_QUERY

  def test_generation_config_format(self):
    """model_params must use GenerateContent API format."""
    assert "generationConfig" in CATEGORICAL_AI_GENERATE_QUERY
    assert "maxOutputTokens" in CATEGORICAL_AI_GENERATE_QUERY

  def test_not_table_valued(self):
    """Must NOT use the table-valued FROM ... AI.GENERATE(...) AS result
    syntax — that form does not exist in BigQuery."""
    assert "FROM session_transcripts," not in CATEGORICAL_AI_GENERATE_QUERY
    assert ") AS result" not in CATEGORICAL_AI_GENERATE_QUERY

  def test_format_succeeds(self):
    formatted = CATEGORICAL_AI_GENERATE_QUERY.format(
        project="p",
        dataset="d",
        table="t",
        where="1=1",
        endpoint="gemini-2.5-flash",
        temperature=0.0,
        max_output_tokens=8192,
    )
    assert "p.d.t" in formatted
    assert "gemini-2.5-flash" in formatted


# ------------------------------------------------------------------ #
# Transcript Query Tests                                               #
# ------------------------------------------------------------------ #


class TestCategoricalTranscriptQuery:
  """Tests for the transcript-only SQL template."""

  def test_does_not_contain_ai_generate(self):
    assert "AI.GENERATE" not in CATEGORICAL_TRANSCRIPT_QUERY

  def test_selects_session_id_and_transcript(self):
    assert "session_id" in CATEGORICAL_TRANSCRIPT_QUERY
    assert "transcript" in CATEGORICAL_TRANSCRIPT_QUERY

  def test_uses_same_transcript_building_as_ai_generate(self):
    """The transcript CTE should match the AI.GENERATE query."""
    assert "STRING_AGG" in CATEGORICAL_TRANSCRIPT_QUERY
    assert (
        "JSON_VALUE(content, '$.text_summary')" in CATEGORICAL_TRANSCRIPT_QUERY
    )

  def test_format_succeeds(self):
    formatted = CATEGORICAL_TRANSCRIPT_QUERY.format(
        project="p",
        dataset="d",
        table="t",
        where="1=1",
    )
    assert "p.d.t" in formatted


# ------------------------------------------------------------------ #
# API Fallback Tests                                                   #
# ------------------------------------------------------------------ #


def _run(coro):
  """Helper to run async tests."""
  return asyncio.run(coro)


def _mock_genai_modules(mock_client):
  """Sets up sys.modules mocks for google.genai imports."""
  import sys

  mock_genai = MagicMock()
  mock_genai.Client.return_value = mock_client
  mock_types = MagicMock()
  mock_google = MagicMock()
  mock_google.genai = mock_genai

  return patch.dict(
      sys.modules,
      {
          "google": mock_google,
          "google.genai": mock_genai,
          "google.genai.types": mock_types,
      },
  )


def _make_genai_client(generate_side_effect):
  """Builds a mock genai client with the given generate_content behavior."""
  mock_aio_models = MagicMock()
  mock_aio_models.generate_content = AsyncMock(
      side_effect=generate_side_effect
      if isinstance(generate_side_effect, (list, Exception))
      else None,
      return_value=generate_side_effect
      if not isinstance(generate_side_effect, (list, Exception))
      else None,
  )
  if isinstance(generate_side_effect, list):
    mock_aio_models.generate_content = AsyncMock(
        side_effect=generate_side_effect
    )
  mock_aio = MagicMock()
  mock_aio.models = mock_aio_models
  mock_client = MagicMock()
  mock_client.aio = mock_aio
  return mock_client, mock_aio_models


class TestClassifySessionsViaApi:
  """Tests for classify_sessions_via_api."""

  def test_valid_api_response(self):
    """Successful Gemini API response should be parsed and validated."""
    config = _make_config()
    transcripts = {"s1": "USER: Hello\nAGENT: Hi!"}

    raw_response = json.dumps(
        [
            {
                "metric_name": "tone",
                "category": "positive",
                "justification": "kind",
            },
            {
                "metric_name": "safety",
                "category": "safe",
                "justification": "ok",
            },
        ]
    )

    mock_response = MagicMock()
    mock_response.text = raw_response
    mock_client, _ = _make_genai_client(mock_response)

    with _mock_genai_modules(mock_client):
      results = _run(classify_sessions_via_api(transcripts, config))

    assert len(results) == 1
    assert results[0].session_id == "s1"
    assert results[0].metrics[0].category == "positive"
    assert results[0].metrics[1].category == "safe"

  def test_api_exception_per_session(self):
    """API failure for one session should produce parse errors for that
    session but not crash the whole run."""
    config = _make_config()
    transcripts = {"s1": "transcript1", "s2": "transcript2"}

    good_response = MagicMock()
    good_response.text = json.dumps(
        [
            {"metric_name": "tone", "category": "positive"},
            {"metric_name": "safety", "category": "safe"},
        ]
    )

    mock_client, _ = _make_genai_client(
        [good_response, Exception("API quota exceeded")]
    )

    with _mock_genai_modules(mock_client):
      results = _run(classify_sessions_via_api(transcripts, config))

    assert len(results) == 2
    # First session should succeed.
    assert results[0].metrics[0].category == "positive"
    # Second session should have parse errors.
    assert all(m.parse_error for m in results[1].metrics)

  def test_import_error_propagates(self):
    """When google-genai is not installed, ImportError should propagate
    so the caller can set the correct execution mode."""
    config = _make_config()
    transcripts = {"s1": "transcript1"}

    import builtins
    import sys

    saved = {}
    for key in list(sys.modules):
      if key.startswith("google.genai") or key == "google.genai":
        saved[key] = sys.modules.pop(key)

    original_import = builtins.__import__

    def mock_import(name, *args, **kwargs):
      if name == "google" or name.startswith("google.genai"):
        raise ImportError("No module named 'google.genai'")
      return original_import(name, *args, **kwargs)

    with pytest.raises(ImportError):
      with patch.object(builtins, "__import__", side_effect=mock_import):
        _run(classify_sessions_via_api(transcripts, config))

    sys.modules.update(saved)

  def test_case_insensitive_api_response(self):
    """API response with mixed-case categories should normalize."""
    config = _make_config()
    transcripts = {"s1": "USER: Hello"}

    mock_response = MagicMock()
    mock_response.text = json.dumps(
        [
            {"metric_name": "tone", "category": "POSITIVE"},
            {"metric_name": "safety", "category": "Safe"},
        ]
    )
    mock_client, _ = _make_genai_client(mock_response)

    with _mock_genai_modules(mock_client):
      results = _run(classify_sessions_via_api(transcripts, config))

    assert results[0].metrics[0].category == "positive"
    assert results[0].metrics[1].category == "safe"

  def test_long_transcript_truncated(self):
    """Transcripts longer than 25000 chars should be truncated."""
    config = _make_config()
    long_text = "x" * 30000
    transcripts = {"s1": long_text}

    mock_response = MagicMock()
    mock_response.text = json.dumps(
        [
            {"metric_name": "tone", "category": "neutral"},
            {"metric_name": "safety", "category": "safe"},
        ]
    )
    mock_client, mock_aio_models = _make_genai_client(mock_response)

    with _mock_genai_modules(mock_client):
      results = _run(classify_sessions_via_api(transcripts, config))

    # Verify the prompt was truncated by checking what was passed.
    call_args = mock_aio_models.generate_content.call_args
    prompt_sent = call_args[1]["contents"]
    assert "[truncated]" in prompt_sent


# ------------------------------------------------------------------ #
# Persistence Tests                                                    #
# ------------------------------------------------------------------ #


class TestCategoricalResultsDDL:
  """Tests for the results table DDL template."""

  def test_creates_table_if_not_exists(self):
    assert "CREATE TABLE IF NOT EXISTS" in CATEGORICAL_RESULTS_DDL

  def test_contains_all_schema_columns(self):
    for col in [
        "session_id STRING",
        "metric_name STRING",
        "category STRING",
        "justification STRING",
        "passed_validation BOOL",
        "parse_error BOOL",
        "raw_response STRING",
        "endpoint STRING",
        "execution_mode STRING",
        "prompt_version STRING",
        "created_at TIMESTAMP",
    ]:
      assert col in CATEGORICAL_RESULTS_DDL

  def test_format_succeeds(self):
    formatted = CATEGORICAL_RESULTS_DDL.format(
        project="p",
        dataset="d",
        results_table="my_results",
    )
    assert "p.d.my_results" in formatted


class TestFlattenResultsToRows:
  """Tests for flatten_results_to_rows."""

  def test_basic_flattening(self):
    config = _make_config()
    sessions = [
        CategoricalSessionResult(
            session_id="s1",
            metrics=[
                CategoricalMetricResult(
                    metric_name="tone",
                    category="positive",
                    justification="kind",
                ),
                CategoricalMetricResult(
                    metric_name="safety",
                    category="safe",
                ),
            ],
        ),
    ]
    report = build_categorical_report("test_ds", sessions, config)
    report.details["execution_mode"] = "ai_generate"

    rows = flatten_results_to_rows(report, config, "gemini-2.5-flash")

    assert len(rows) == 2
    assert rows[0]["session_id"] == "s1"
    assert rows[0]["metric_name"] == "tone"
    assert rows[0]["category"] == "positive"
    assert rows[0]["justification"] == "kind"
    assert rows[0]["passed_validation"] is True
    assert rows[0]["parse_error"] is False
    assert rows[0]["endpoint"] == "gemini-2.5-flash"
    assert rows[0]["execution_mode"] == "ai_generate"
    assert rows[1]["metric_name"] == "safety"
    assert rows[1]["category"] == "safe"

  def test_includes_prompt_version(self):
    config = _make_config()
    config = config.model_copy(update={"prompt_version": "v2.1"})
    sessions = [
        CategoricalSessionResult(
            session_id="s1",
            metrics=[
                CategoricalMetricResult(
                    metric_name="tone", category="positive"
                ),
                CategoricalMetricResult(metric_name="safety", category="safe"),
            ],
        ),
    ]
    report = build_categorical_report("test_ds", sessions, config)
    report.details["execution_mode"] = "ai_generate"

    rows = flatten_results_to_rows(report, config, "gemini-2.5-flash")

    assert all(r["prompt_version"] == "v2.1" for r in rows)

  def test_parse_error_rows(self):
    config = _make_config()
    sessions = [
        CategoricalSessionResult(
            session_id="s1",
            metrics=[
                CategoricalMetricResult(
                    metric_name="tone",
                    parse_error=True,
                    passed_validation=False,
                    raw_response="bad json",
                ),
                CategoricalMetricResult(
                    metric_name="safety",
                    parse_error=True,
                    passed_validation=False,
                ),
            ],
        ),
    ]
    report = build_categorical_report("test_ds", sessions, config)
    report.details["execution_mode"] = "api_fallback"

    rows = flatten_results_to_rows(report, config, "gemini-2.5-flash")

    assert len(rows) == 2
    assert rows[0]["parse_error"] is True
    assert rows[0]["passed_validation"] is False
    assert rows[0]["raw_response"] == "bad json"
    assert rows[0]["category"] is None
    assert rows[0]["execution_mode"] == "api_fallback"

  def test_empty_report(self):
    config = _make_config()
    report = build_categorical_report("test_ds", [], config)
    rows = flatten_results_to_rows(report, config, "gemini-2.5-flash")
    assert rows == []

  def test_multiple_sessions(self):
    config = _make_config()
    sessions = [
        CategoricalSessionResult(
            session_id="s1",
            metrics=[
                CategoricalMetricResult(
                    metric_name="tone", category="positive"
                ),
                CategoricalMetricResult(metric_name="safety", category="safe"),
            ],
        ),
        CategoricalSessionResult(
            session_id="s2",
            metrics=[
                CategoricalMetricResult(
                    metric_name="tone", category="negative"
                ),
                CategoricalMetricResult(
                    metric_name="safety", category="unsafe"
                ),
            ],
        ),
    ]
    report = build_categorical_report("test_ds", sessions, config)
    report.details["execution_mode"] = "ai_generate"

    rows = flatten_results_to_rows(report, config, "gemini-2.5-flash")

    assert len(rows) == 4
    session_ids = [r["session_id"] for r in rows]
    assert session_ids == ["s1", "s1", "s2", "s2"]


# ------------------------------------------------------------------ #
# build_classify_categories_literal Tests                              #
# ------------------------------------------------------------------ #


class TestBuildClassifyCategoriesLiteral:
  """Tests for build_classify_categories_literal."""

  def test_basic_format(self):
    metric = CategoricalMetricDefinition(
        name="tone",
        definition="Tone.",
        categories=[
            CategoricalMetricCategory(
                name="positive", definition="User is satisfied."
            ),
            CategoricalMetricCategory(
                name="negative", definition="User is frustrated."
            ),
        ],
    )
    result = build_classify_categories_literal(metric)
    assert result == (
        "[('positive', 'User is satisfied.'), "
        "('negative', 'User is frustrated.')]"
    )

  def test_single_category(self):
    metric = CategoricalMetricDefinition(
        name="safety",
        definition="Safety.",
        categories=[
            CategoricalMetricCategory(name="safe", definition="OK."),
        ],
    )
    result = build_classify_categories_literal(metric)
    assert result == "[('safe', 'OK.')]"

  def test_sql_quote_escaping(self):
    metric = CategoricalMetricDefinition(
        name="tone",
        definition="Tone.",
        categories=[
            CategoricalMetricCategory(
                name="it's good",
                definition="User's satisfied.",
            ),
        ],
    )
    result = build_classify_categories_literal(metric)
    assert "it''s good" in result
    assert "User''s satisfied." in result

  def test_empty_categories(self):
    metric = CategoricalMetricDefinition(
        name="tone",
        definition="Tone.",
        categories=[],
    )
    result = build_classify_categories_literal(metric)
    assert result == "[]"


# ------------------------------------------------------------------ #
# build_ai_classify_query Tests                                        #
# ------------------------------------------------------------------ #


class TestBuildAiClassifyQuery:
  """Tests for build_ai_classify_query."""

  def test_contains_ai_classify(self):
    config = _make_config(include_justification=False)
    sql = build_ai_classify_query(
        config, "p", "d", "t", "1=1", endpoint="gemini-2.5-flash"
    )
    assert "AI.CLASSIFY" in sql

  def test_one_column_per_metric(self):
    config = _make_config(include_justification=False)
    sql = build_ai_classify_query(
        config, "p", "d", "t", "1=1", endpoint="gemini-2.5-flash"
    )
    assert "classify_0" in sql
    assert "classify_1" in sql

  def test_categories_in_sql(self):
    config = _make_config(include_justification=False)
    sql = build_ai_classify_query(
        config, "p", "d", "t", "1=1", endpoint="gemini-2.5-flash"
    )
    assert "('positive', 'User is satisfied.')" in sql
    assert "('safe', 'Response is safe.')" in sql

  def test_connection_id_in_sql(self):
    config = _make_config(include_justification=False)
    sql = build_ai_classify_query(
        config,
        "p",
        "d",
        "t",
        "1=1",
        endpoint="gemini-2.5-flash",
        connection_id="proj.us.conn",
    )
    assert "connection_id => 'proj.us.conn'" in sql

  def test_endpoint_in_sql(self):
    config = _make_config(include_justification=False)
    sql = build_ai_classify_query(
        config, "p", "d", "t", "1=1", endpoint="gemini-2.5-flash"
    )
    assert "endpoint => 'gemini-2.5-flash'" in sql

  def test_both_connection_and_endpoint(self):
    config = _make_config(include_justification=False)
    sql = build_ai_classify_query(
        config,
        "p",
        "d",
        "t",
        "1=1",
        endpoint="gemini-2.5-flash",
        connection_id="proj.us.conn",
    )
    assert "connection_id => 'proj.us.conn'" in sql
    assert "endpoint => 'gemini-2.5-flash'" in sql

  def test_neither_connection_nor_endpoint(self):
    config = _make_config(include_justification=False)
    sql = build_ai_classify_query(config, "p", "d", "t", "1=1")
    assert "connection_id =>" not in sql
    assert "endpoint =>" not in sql

  def test_uses_transcript_cte(self):
    config = _make_config(include_justification=False)
    sql = build_ai_classify_query(
        config, "p", "d", "t", "1=1", endpoint="gemini-2.5-flash"
    )
    assert "session_transcripts" in sql
    assert "STRING_AGG" in sql

  def test_contains_trace_limit(self):
    config = _make_config(include_justification=False)
    sql = build_ai_classify_query(
        config, "p", "d", "t", "1=1", endpoint="gemini-2.5-flash"
    )
    assert "@trace_limit" in sql


# ------------------------------------------------------------------ #
# build_ai_generate_query Tests                                        #
# ------------------------------------------------------------------ #


class TestBuildAiGenerateQuery:
  """Tests for build_ai_generate_query."""

  def test_with_connection_id(self):
    sql = build_ai_generate_query(
        "p",
        "d",
        "t",
        "1=1",
        "gemini-2.5-flash",
        0.0,
        connection_id="proj.us.conn",
    )
    assert "connection_id => 'proj.us.conn'" in sql
    assert "AI.GENERATE" in sql

  def test_without_connection_id_matches_original(self):
    sql = build_ai_generate_query(
        "p",
        "d",
        "t",
        "1=1",
        "gemini-2.5-flash",
        0.0,
    )
    assert "connection_id =>" not in sql
    assert "AI.GENERATE" in sql
    assert "endpoint => 'gemini-2.5-flash'" in sql
    assert "classifications STRING" in sql

  def test_endpoint_is_escaped(self):
    sql = build_ai_generate_query(
        "p",
        "d",
        "t",
        "1=1",
        "it's-a-model",
        0.0,
    )
    assert "it''s-a-model" in sql


# ------------------------------------------------------------------ #
# parse_classify_row Tests                                             #
# ------------------------------------------------------------------ #


class TestParseClassifyRow:
  """Tests for parse_classify_row."""

  def test_valid_categories(self):
    config = _make_config(include_justification=False)
    row = {
        "session_id": "s1",
        "transcript": "text",
        "classify_0": "positive",
        "classify_1": "safe",
    }
    sr, null_count = parse_classify_row("s1", row, config)
    assert sr.session_id == "s1"
    assert len(sr.metrics) == 2
    assert sr.metrics[0].category == "positive"
    assert sr.metrics[0].passed_validation is True
    assert sr.metrics[0].parse_error is False
    assert sr.metrics[1].category == "safe"
    assert sr.metrics[1].passed_validation is True
    assert null_count == 0

  def test_null_category(self):
    config = _make_config(include_justification=False)
    row = {
        "session_id": "s1",
        "transcript": "text",
        "classify_0": None,
        "classify_1": "safe",
    }
    sr, null_count = parse_classify_row("s1", row, config)
    assert sr.metrics[0].category is None
    assert sr.metrics[0].passed_validation is False
    assert sr.metrics[0].parse_error is False
    assert sr.metrics[1].category == "safe"
    assert null_count == 1

  def test_mixed_results(self):
    config = _make_config(include_justification=False)
    row = {
        "session_id": "s1",
        "transcript": "text",
        "classify_0": "negative",
        "classify_1": None,
    }
    sr, null_count = parse_classify_row("s1", row, config)
    assert sr.metrics[0].category == "negative"
    assert sr.metrics[0].passed_validation is True
    assert sr.metrics[1].category is None
    assert sr.metrics[1].passed_validation is False
    assert null_count == 1

  def test_missing_column(self):
    config = _make_config(include_justification=False)
    row = {
        "session_id": "s1",
        "transcript": "text",
        "classify_0": "positive",
        # classify_1 missing — row.get returns None
    }
    sr, null_count = parse_classify_row("s1", row, config)
    assert sr.metrics[1].category is None
    assert sr.metrics[1].passed_validation is False
    assert sr.metrics[1].parse_error is False
    assert null_count == 1

  def test_empty_string_treated_as_value(self):
    config = _make_config(include_justification=False)
    row = {
        "session_id": "s1",
        "transcript": "text",
        "classify_0": "",
        "classify_1": "safe",
    }
    sr, null_count = parse_classify_row("s1", row, config)
    # Empty string is not None — it's a value from AI.CLASSIFY.
    assert sr.metrics[0].category == ""
    assert sr.metrics[0].passed_validation is True
    assert null_count == 0

  def test_justification_always_none(self):
    config = _make_config(include_justification=False)
    row = {
        "session_id": "s1",
        "transcript": "text",
        "classify_0": "positive",
        "classify_1": "safe",
    }
    sr, _ = parse_classify_row("s1", row, config)
    assert all(m.justification is None for m in sr.metrics)

  def test_raw_response_stores_value(self):
    config = _make_config(include_justification=False)
    row = {
        "session_id": "s1",
        "transcript": "text",
        "classify_0": "positive",
        "classify_1": "safe",
    }
    sr, _ = parse_classify_row("s1", row, config)
    assert sr.metrics[0].raw_response == "positive"
    assert sr.metrics[1].raw_response == "safe"

  def test_null_count_returned_correctly(self):
    config = _make_config(include_justification=False)
    row = {
        "session_id": "s1",
        "transcript": "text",
        "classify_0": None,
        "classify_1": None,
    }
    sr, null_count = parse_classify_row("s1", row, config)
    assert null_count == 2


# ------------------------------------------------------------------ #
# max_output_tokens Tests                                              #
# ------------------------------------------------------------------ #


class TestMaxOutputTokens:
  """Tests for max_output_tokens config and propagation."""

  def test_config_default(self):
    config = _make_config()
    assert config.max_output_tokens == 8192

  def test_config_custom_value(self):
    config = CategoricalEvaluationConfig(
        metrics=[
            CategoricalMetricDefinition(
                name="tone",
                definition="Tone.",
                categories=[
                    CategoricalMetricCategory(
                        name="positive",
                        definition="Good.",
                    ),
                ],
            ),
        ],
        max_output_tokens=4096,
    )
    assert config.max_output_tokens == 4096

  def test_build_ai_generate_query_default(self):
    sql = build_ai_generate_query(
        "p",
        "d",
        "t",
        "1=1",
        "gemini-2.5-flash",
        0.0,
    )
    assert 'maxOutputTokens": 8192' in sql

  def test_build_ai_generate_query_custom(self):
    sql = build_ai_generate_query(
        "p",
        "d",
        "t",
        "1=1",
        "gemini-2.5-flash",
        0.0,
        max_output_tokens=2048,
    )
    assert 'maxOutputTokens": 2048' in sql

  def test_template_uses_placeholder(self):
    formatted = CATEGORICAL_AI_GENERATE_QUERY.format(
        project="p",
        dataset="d",
        table="t",
        where="1=1",
        endpoint="ep",
        temperature=0.0,
        max_output_tokens=4096,
    )
    assert 'maxOutputTokens": 4096' in formatted

  def test_api_uses_config_value(self):
    """classify_sessions_via_api should pass config.max_output_tokens
    to the Gemini API GenerateContentConfig."""
    import sys

    config = _make_config()
    config = config.model_copy(update={"max_output_tokens": 2048})
    transcripts = {"s1": "USER: Hello"}

    mock_response = MagicMock()
    mock_response.text = json.dumps(
        [
            {"metric_name": "tone", "category": "positive"},
            {"metric_name": "safety", "category": "safe"},
        ]
    )
    mock_client, _ = _make_genai_client(mock_response)

    # Build mocks with access to the types module to verify
    # GenerateContentConfig call args.
    mock_genai = MagicMock()
    mock_genai.Client.return_value = mock_client

    with patch.dict(
        sys.modules,
        {
            "google": MagicMock(genai=mock_genai),
            "google.genai": mock_genai,
            "google.genai.types": mock_genai.types,
        },
    ):
      _run(classify_sessions_via_api(transcripts, config))

    # types.GenerateContentConfig is called inside classify_sessions_via_api
    mock_genai.types.GenerateContentConfig.assert_called_once_with(
        temperature=0.0,
        max_output_tokens=2048,
    )


# ------------------------------------------------------------------ #
# finish_reason logging Tests                                          #
# ------------------------------------------------------------------ #


class TestFinishReasonLogging:
  """Tests for finish_reason logging on parse errors."""

  def test_parse_error_logs_finish_reason(self):
    """When the API returns unparseable text, finish_reason should
    be logged as a warning."""
    config = _make_config()
    transcripts = {"s1": "USER: Hello"}

    mock_response = MagicMock()
    mock_response.text = "not valid json at all"
    mock_candidate = MagicMock()
    mock_candidate.finish_reason = "MAX_TOKENS"
    mock_response.candidates = [mock_candidate]

    mock_client, _ = _make_genai_client(mock_response)

    with _mock_genai_modules(mock_client):
      with patch(
          "bigquery_agent_analytics.categorical_evaluator.logger"
      ) as mock_logger:
        results = _run(classify_sessions_via_api(transcripts, config))

    assert all(m.parse_error for m in results[0].metrics)
    mock_logger.warning.assert_called()
    warning_args = mock_logger.warning.call_args[0]
    assert "finish_reason" in warning_args[0]

  def test_null_response_text_handled(self):
    """When response.text is None, should not crash."""
    config = _make_config()
    transcripts = {"s1": "USER: Hello"}

    mock_response = MagicMock()
    mock_response.text = None
    mock_response.candidates = []

    mock_client, _ = _make_genai_client(mock_response)

    with _mock_genai_modules(mock_client):
      results = _run(classify_sessions_via_api(transcripts, config))

    assert len(results) == 1
    assert all(m.parse_error for m in results[0].metrics)


# ------------------------------------------------------------------ #
# NULL retry logic Tests                                               #
# ------------------------------------------------------------------ #


class TestRetryFailedSessions:
  """Tests for _retry_failed_sessions on the Client."""

  def _make_client(self):
    """Build a Client with a mocked BQ client."""
    from bigquery_agent_analytics.client import Client

    mock_bq = MagicMock()
    mock_bq.query.return_value.result.return_value = []
    return Client(
        project_id="p",
        dataset_id="d",
        table_id="t",
        verify_schema=False,
        bq_client=mock_bq,
    )

  def test_retry_replaces_null_sessions(self):
    """NULL sessions should be replaced by successful API retries."""
    client = self._make_client()
    config = _make_config()
    transcripts = {"s1": "USER: Hello"}

    good_result = CategoricalSessionResult(
        session_id="s1",
        metrics=[
            CategoricalMetricResult(
                metric_name="tone",
                category="positive",
                passed_validation=True,
            ),
            CategoricalMetricResult(
                metric_name="safety",
                category="safe",
                passed_validation=True,
            ),
        ],
    )

    with patch(
        "bigquery_agent_analytics.client._run_sync",
        return_value=[good_result],
    ):
      results = client._retry_failed_sessions(
          transcripts,
          config,
          "gemini-2.5-flash",
          max_retries=1,
      )

    assert len(results) == 1
    assert results[0].session_id == "s1"
    assert results[0].metrics[0].category == "positive"

  def test_retry_exhausts_attempts(self):
    """Sessions that keep failing should exhaust all retry attempts."""
    client = self._make_client()
    config = _make_config()
    transcripts = {"s1": "USER: Hello"}

    bad_result = CategoricalSessionResult(
        session_id="s1",
        metrics=[
            CategoricalMetricResult(
                metric_name="tone",
                parse_error=True,
                passed_validation=False,
                raw_response="bad",
            ),
            CategoricalMetricResult(
                metric_name="safety",
                parse_error=True,
                passed_validation=False,
            ),
        ],
    )

    with patch(
        "bigquery_agent_analytics.client._run_sync",
        return_value=[bad_result],
    ) as mock_run:
      results = client._retry_failed_sessions(
          transcripts,
          config,
          "gemini-2.5-flash",
          max_retries=3,
      )

    assert len(results) == 0
    assert mock_run.call_count == 3

  def test_retry_handles_api_exception(self):
    """API exceptions during retry should not crash."""
    client = self._make_client()
    config = _make_config()
    transcripts = {"s1": "USER: Hello"}

    with patch(
        "bigquery_agent_analytics.client._run_sync",
        side_effect=RuntimeError("API down"),
    ):
      results = client._retry_failed_sessions(
          transcripts,
          config,
          "gemini-2.5-flash",
          max_retries=2,
      )

    assert len(results) == 0

  def test_retry_partial_success(self):
    """When some sessions succeed and some fail, only the successful
    ones should be returned."""
    client = self._make_client()
    config = _make_config()
    transcripts = {"s1": "text1", "s2": "text2"}

    good_result = CategoricalSessionResult(
        session_id="s1",
        metrics=[
            CategoricalMetricResult(
                metric_name="tone",
                category="positive",
                passed_validation=True,
            ),
            CategoricalMetricResult(
                metric_name="safety",
                category="safe",
                passed_validation=True,
            ),
        ],
    )
    bad_result = CategoricalSessionResult(
        session_id="s2",
        metrics=[
            CategoricalMetricResult(
                metric_name="tone",
                parse_error=True,
                passed_validation=False,
                raw_response="bad",
            ),
            CategoricalMetricResult(
                metric_name="safety",
                parse_error=True,
                passed_validation=False,
            ),
        ],
    )

    with patch(
        "bigquery_agent_analytics.client._run_sync",
        return_value=[good_result, bad_result],
    ):
      results = client._retry_failed_sessions(
          transcripts,
          config,
          "gemini-2.5-flash",
          max_retries=1,
      )

    assert len(results) == 1
    assert results[0].session_id == "s1"

  def test_ai_generate_detects_null_classifications(self):
    """_categorical_ai_generate should detect NULL classifications
    and trigger retry."""
    client = self._make_client()
    config = _make_config()

    valid_json = json.dumps(
        [
            {"metric_name": "tone", "category": "positive"},
            {"metric_name": "safety", "category": "safe"},
        ]
    )

    # Use plain dicts: dict(plain_dict) returns a copy, which
    # matches the behavior of dict(bigquery.Row).
    mock_rows = [
        {
            "session_id": "s1",
            "transcript": "text1",
            "classifications": valid_json,
        },
        {
            "session_id": "s2",
            "transcript": "text2",
            "classifications": None,
        },
    ]

    client.bq_client.query.return_value.result.return_value = mock_rows

    retry_result = CategoricalSessionResult(
        session_id="s2",
        metrics=[
            CategoricalMetricResult(
                metric_name="tone",
                category="negative",
                passed_validation=True,
            ),
            CategoricalMetricResult(
                metric_name="safety",
                category="safe",
                passed_validation=True,
            ),
        ],
    )

    with patch.object(
        client,
        "_retry_failed_sessions",
        return_value=[retry_result],
    ) as mock_retry:
      results, retry_meta = client._categorical_ai_generate(
          config,
          "t",
          "1=1",
          [],
          "gemini-2.5-flash",
      )

    mock_retry.assert_called_once()
    call_args = mock_retry.call_args
    assert "s2" in call_args[0][0]
    assert "s1" not in call_args[0][0]
    assert len(results) == 2
    assert retry_meta["failed_count"] == 1
    assert retry_meta["retry_attempted"] is True

  def test_ai_generate_no_retry_when_all_succeed(self):
    """When all rows have classifications, no retry should happen."""
    client = self._make_client()
    config = _make_config()

    valid_json = json.dumps(
        [
            {"metric_name": "tone", "category": "positive"},
            {"metric_name": "safety", "category": "safe"},
        ]
    )

    mock_rows = [
        {
            "session_id": "s1",
            "transcript": "text1",
            "classifications": valid_json,
        },
    ]

    client.bq_client.query.return_value.result.return_value = mock_rows

    with patch.object(
        client,
        "_retry_failed_sessions",
    ) as mock_retry:
      results, retry_meta = client._categorical_ai_generate(
          config,
          "t",
          "1=1",
          [],
          "gemini-2.5-flash",
      )

    mock_retry.assert_not_called()
    assert len(results) == 1
    assert results[0].metrics[0].category == "positive"
    assert retry_meta == {}

  def test_ai_generate_null_without_transcript_skips_retry(self):
    """NULL classifications with no transcript should NOT be retried."""
    client = self._make_client()
    config = _make_config()

    mock_rows = [
        {
            "session_id": "s1",
            "transcript": None,
            "classifications": None,
        },
    ]

    client.bq_client.query.return_value.result.return_value = mock_rows

    with patch.object(
        client,
        "_retry_failed_sessions",
    ) as mock_retry:
      results, retry_meta = client._categorical_ai_generate(
          config,
          "t",
          "1=1",
          [],
          "gemini-2.5-flash",
      )

    mock_retry.assert_not_called()
    assert len(results) == 1
    assert retry_meta == {}

  def test_ai_generate_detects_parse_error_classifications(self):
    """Non-NULL but unparseable classifications should also trigger retry."""
    client = self._make_client()
    config = _make_config()

    valid_json = json.dumps(
        [
            {"metric_name": "tone", "category": "positive"},
            {"metric_name": "safety", "category": "safe"},
        ]
    )

    mock_rows = [
        {
            "session_id": "s1",
            "transcript": "text1",
            "classifications": valid_json,
        },
        {
            "session_id": "s2",
            "transcript": "text2",
            "classifications": "not valid json",
        },
    ]

    client.bq_client.query.return_value.result.return_value = mock_rows

    retry_result = CategoricalSessionResult(
        session_id="s2",
        metrics=[
            CategoricalMetricResult(
                metric_name="tone",
                category="negative",
                passed_validation=True,
            ),
            CategoricalMetricResult(
                metric_name="safety",
                category="safe",
                passed_validation=True,
            ),
        ],
    )

    with patch.object(
        client,
        "_retry_failed_sessions",
        return_value=[retry_result],
    ) as mock_retry:
      results, retry_meta = client._categorical_ai_generate(
          config,
          "t",
          "1=1",
          [],
          "gemini-2.5-flash",
      )

    mock_retry.assert_called_once()
    call_args = mock_retry.call_args
    assert "s2" in call_args[0][0]
    assert "s1" not in call_args[0][0]
    assert len(results) == 2
    assert retry_meta["failed_count"] == 1
    assert retry_meta["retry_attempted"] is True


# ------------------------------------------------------------------ #
# ORDER BY before LIMIT — regression tests (#55)                       #
# ------------------------------------------------------------------ #


class TestQueryOrderByBeforeLimit:
  """Ensure all session-fetching query templates have ORDER BY before LIMIT."""

  def test_transcript_query_orders_before_limit(self):
    sql = CATEGORICAL_TRANSCRIPT_QUERY
    order_pos = sql.index("ORDER BY MAX(timestamp)")
    limit_pos = sql.index("LIMIT @trace_limit")
    assert order_pos < limit_pos

  def test_ai_generate_query_orders_before_limit(self):
    sql = CATEGORICAL_AI_GENERATE_QUERY
    order_pos = sql.index("ORDER BY MAX(timestamp)")
    limit_pos = sql.index("LIMIT @trace_limit")
    assert order_pos < limit_pos

  def test_ai_classify_query_orders_before_limit(self):
    config = _make_config()
    sql = build_ai_classify_query(config, "proj", "ds", "tbl", "1=1")
    order_pos = sql.index("ORDER BY MAX(timestamp)")
    limit_pos = sql.index("LIMIT @trace_limit")
    assert order_pos < limit_pos

  def test_ai_generate_dynamic_query_orders_before_limit(self):
    sql = build_ai_generate_query(
        "proj", "ds", "tbl", "1=1", "gemini-2.5-flash", 0.0
    )
    order_pos = sql.index("ORDER BY MAX(timestamp)")
    limit_pos = sql.index("LIMIT @trace_limit")
    assert order_pos < limit_pos

  def test_all_queries_have_session_id_tiebreaker(self):
    """ORDER BY should include session_id for full determinism."""
    config = _make_config()
    queries = [
        ("CATEGORICAL_TRANSCRIPT_QUERY", CATEGORICAL_TRANSCRIPT_QUERY),
        ("CATEGORICAL_AI_GENERATE_QUERY", CATEGORICAL_AI_GENERATE_QUERY),
        (
            "build_ai_classify_query",
            build_ai_classify_query(config, "p", "d", "t", "1=1"),
        ),
        (
            "build_ai_generate_query",
            build_ai_generate_query(
                "p", "d", "t", "1=1", "gemini-2.5-flash", 0.0
            ),
        ),
    ]
    for name, sql in queries:
      assert (
          "ORDER BY MAX(timestamp) DESC, session_id" in sql
      ), f"{name} missing session_id tiebreaker in ORDER BY"
