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

"""Default prompt templates for the improvement cycle."""

from __future__ import annotations

import os
import sys

_SRC_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "src")
)
if _SRC_DIR not in sys.path and os.path.isdir(_SRC_DIR):
  sys.path.insert(0, _SRC_DIR)

from bigquery_agent_analytics.evaluation_rubrics import build_metrics  # noqa: E402

_JUDGE_PROMPT_TEMPLATE = """You are evaluating an AI agent's response to a user question.

Question: {question}
Response: {response}
{tool_check}

Evaluation Rubrics:
__RUBRICS_BLOCK__

Return JSON with these fields:
{{
  "pass": true or false,
  "response_usefulness": __USEFULNESS_CATEGORIES__,
  "tool_usage": __TOOL_USAGE_CATEGORIES__,
  "reason": "one-sentence explanation"
}}

A response PASSES ("pass": true) when response_usefulness is __PASS_USEFULNESS__.
A response FAILS ("pass": false) when response_usefulness is __FAIL_USEFULNESS__.{tool_fail_rule}
Return ONLY the JSON, no other text.
"""


def _format_metric_rubric(metric) -> str:
  lines = [f"- {metric.name}: {metric.definition}"]
  for cat in metric.categories:
    lines.append(f"  * {cat.name}: {cat.definition}")
  return "\n".join(lines)


def _format_category_choices(metric) -> str:
  return " | ".join(f'"{cat.name}"' for cat in metric.categories)


def get_rubric_categories() -> tuple[tuple[str, ...], tuple[str, ...]]:
  """Return canonical (response_usefulness, tool_usage) category names."""
  metrics_by_name = {m.name: m for m in build_metrics()}
  usefulness_cats = tuple(
      c.name for c in metrics_by_name["response_usefulness"].categories
  )
  tool_usage_cats = tuple(
      c.name for c in metrics_by_name["tool_usage"].categories
  )
  return usefulness_cats, tool_usage_cats


USEFULNESS_CATEGORIES, TOOL_USAGE_CATEGORIES = get_rubric_categories()


def build_judge_prompt() -> str:
  """Build the local golden-eval judge prompt from canonical SDK rubrics."""
  metrics_by_name = {m.name: m for m in build_metrics()}
  usefulness = metrics_by_name["response_usefulness"]
  tool_usage = metrics_by_name["tool_usage"]
  rubrics_block = (
      f"{_format_metric_rubric(usefulness)}\n"
      f"{_format_metric_rubric(tool_usage)}"
  )
  pass_cat = f'"{usefulness.categories[0].name}"'
  fail_cats = " or ".join(f'"{c.name}"' for c in usefulness.categories[1:])
  return (
      _JUDGE_PROMPT_TEMPLATE.replace("__RUBRICS_BLOCK__", rubrics_block)
      .replace(
          "__USEFULNESS_CATEGORIES__", _format_category_choices(usefulness)
      )
      .replace(
          "__TOOL_USAGE_CATEGORIES__", _format_category_choices(tool_usage)
      )
      .replace("__PASS_USEFULNESS__", pass_cat)
      .replace("__FAIL_USEFULNESS__", fail_cats)
  )


JUDGE_PROMPT = build_judge_prompt()


IMPROVER_PROMPT = """You are an agent prompt engineer. Your job is to improve an AI agent's system prompt based on quality evaluation results.

## Current Agent Prompt (version {current_version})
```
{current_prompt}
```

## Quality Report Summary
- Total sessions: {total_sessions}
- Meaningful (helpful): {meaningful} ({meaningful_rate}%)
- Partial: {partial}
- Unhelpful: {unhelpful} ({unhelpful_rate}%)

## Unhelpful and Partial Sessions (these need fixing)
{problem_sessions}

## Available Tools
The agent has these tools available:
{tool_signatures}

## Your Task
Analyze the unhelpful/partial sessions and improve the agent prompt to fix these issues. The agent has tools that can answer these questions, but the prompt doesn't guide the agent to use them properly.

Rules:
1. Keep the prompt concise (under 500 words)
2. Add specific guidance for topics where the agent failed
3. Add instructions to ALWAYS use the available tools before answering
4. Keep all existing correct behavior
5. Do NOT remove information that was working correctly

Return your response as JSON with exactly these fields:
{{
  "improved_prompt": "the full improved prompt text",
  "changes_summary": "brief description of what changed and why"
}}

Return ONLY the JSON, no other text.
"""
