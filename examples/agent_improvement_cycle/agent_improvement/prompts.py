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

JUDGE_PROMPT = """You are evaluating an AI agent's response to a user question.

Question: {question}
Response: {response}
{tool_check}
Return JSON with exactly these fields:
{{
  "pass": true or false,
  "reason": "one-sentence explanation"
}}

A response PASSES if it provides a specific, substantive answer to the question.
A response FAILS if it says "I don't know", defers elsewhere without answering, or gives vague/generic information without specifics.{tool_fail_rule}
Return ONLY the JSON, no other text.
"""


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
