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

"""Pure analytical kernels for BigQuery Python UDFs.

This module contains **pure functions** that implement the SDK's
deterministic scoring and event-classification logic.  Every function
here:

* accepts only typed scalar inputs (no dicts, no BigQuery client)
* returns a typed scalar output
* has no side effects, no I/O, no environment-dependent behavior

These kernels serve two purposes:

1. They are the single source of truth shared by both the Python SDK
   (``SystemEvaluator`` factories in ``evaluators.py``) and the BigQuery
   Python UDF registration SQL.
2. They can be tested in isolation with simple scalar assertions.

See ``docs/python_udf_support_design.md`` for the design rationale.
"""

from __future__ import annotations

import json
from typing import Any, Optional

# ------------------------------------------------------------------ #
# Event Semantics Kernels                                              #
# ------------------------------------------------------------------ #


def is_error_event(
    event_type: str,
    error_message: Optional[str] = None,
    status: str = "OK",
) -> bool:
  """Returns True when the event represents an error.

  Matches the canonical predicate: event type ends with ``_ERROR``,
  ``error_message`` is populated, or ``status`` is ``'ERROR'``.

  Args:
      event_type: The event_type column value.
      error_message: The error_message column value.
      status: The status column value.
  """
  return (
      event_type.endswith("_ERROR")
      or error_message is not None
      or status == "ERROR"
  )


def tool_outcome(event_type: str, status: str = "OK") -> str:
  """Returns a canonical tool outcome string.

  Args:
      event_type: The event type.
      status: The status column.

  Returns:
      One of ``"success"``, ``"error"``, or ``"in_progress"``.
  """
  if event_type == "TOOL_ERROR" or status == "ERROR":
    return "error"
  if event_type == "TOOL_COMPLETED":
    return "success"
  return "in_progress"


def extract_response_text_from_dict(
    content: Any,
) -> Optional[str]:
  """Extracts user-visible response text from a parsed content dict.

  This is the shared core used by both the Python SDK
  (``event_semantics.extract_response_text``) and the string-wrapper
  UDF kernel (``extract_response_text``).

  Checks keys in priority order: ``response``, ``text_summary``,
  ``text``, ``raw``.

  Args:
      content: The parsed ``content`` JSON column (dict expected).

  Returns:
      The response text or ``None``.
  """
  if not isinstance(content, dict):
    return str(content) if content else None
  return (
      content.get("response")
      or content.get("text_summary")
      or content.get("text")
      or content.get("raw")
      or None
  )


def extract_response_text(content_json: Optional[str]) -> Optional[str]:
  """Extracts user-visible response text from a JSON content string.

  This is a parse-wrapper for BigQuery Python UDFs (which do not
  support the ``JSON`` type): it accepts a JSON ``STRING``, parses
  it, then delegates to :func:`extract_response_text_from_dict`.

  Args:
      content_json: The ``content`` column as a JSON-formatted string.

  Returns:
      The response text or ``None``.
  """
  if not content_json:
    return None
  try:
    content = json.loads(content_json)
  except (json.JSONDecodeError, TypeError):
    return str(content_json) if content_json else None
  return extract_response_text_from_dict(content)


# ------------------------------------------------------------------ #
# Score Kernels                                                        #
# ------------------------------------------------------------------ #
#
# Each kernel returns a float in [0.0, 1.0].  The scoring formula
# is:  1.0 - (value / threshold), clamped to [0.0, 1.0].
#
# Edge-case contract (must be preserved for parity):
#   - Non-positive measured value  →  1.0  (no penalty)
#   - Measured value >= threshold  →  0.0  (full penalty)


def score_latency(avg_latency_ms: float, threshold_ms: float) -> float:
  """Score average latency against a threshold.

  Args:
      avg_latency_ms: Measured average latency in milliseconds.
      threshold_ms: Maximum acceptable latency.

  Returns:
      Score between 0.0 (worst) and 1.0 (best).
  """
  if avg_latency_ms <= 0:
    return 1.0
  if avg_latency_ms >= threshold_ms:
    return 0.0
  return 1.0 - (avg_latency_ms / threshold_ms)


def score_error_rate(
    tool_calls: int,
    tool_errors: int,
    max_error_rate: float,
) -> float:
  """Score tool error rate against a threshold.

  Args:
      tool_calls: Total number of tool calls.
      tool_errors: Number of tool errors.
      max_error_rate: Maximum acceptable error fraction.

  Returns:
      Score between 0.0 (worst) and 1.0 (best).
  """
  if tool_calls <= 0:
    return 1.0
  rate = tool_errors / tool_calls
  if rate >= max_error_rate:
    return 0.0
  return 1.0 - (rate / max_error_rate)


def score_turn_count(turn_count: int, max_turns: int) -> float:
  """Score turn count against a maximum.

  Args:
      turn_count: Measured number of conversation turns.
      max_turns: Maximum acceptable turn count.

  Returns:
      Score between 0.0 (worst) and 1.0 (best).
  """
  if turn_count <= 0:
    return 1.0
  if turn_count >= max_turns:
    return 0.0
  return 1.0 - (turn_count / max_turns)


def score_token_efficiency(total_tokens: int, max_tokens: int) -> float:
  """Score total token usage against a maximum.

  Args:
      total_tokens: Measured total token count.
      max_tokens: Maximum acceptable token count.

  Returns:
      Score between 0.0 (worst) and 1.0 (best).
  """
  if total_tokens <= 0:
    return 1.0
  if total_tokens >= max_tokens:
    return 0.0
  return 1.0 - (total_tokens / max_tokens)


def score_ttft(avg_ttft_ms: float, threshold_ms: float) -> float:
  """Score average time-to-first-token against a threshold.

  Args:
      avg_ttft_ms: Measured average TTFT in milliseconds.
      threshold_ms: Maximum acceptable TTFT.

  Returns:
      Score between 0.0 (worst) and 1.0 (best).
  """
  if avg_ttft_ms <= 0:
    return 1.0
  if avg_ttft_ms >= threshold_ms:
    return 0.0
  return 1.0 - (avg_ttft_ms / threshold_ms)


def score_cost(
    input_tokens: int,
    output_tokens: int,
    max_cost_usd: float,
    input_cost_per_1k: float = 0.00025,
    output_cost_per_1k: float = 0.00125,
) -> float:
  """Score estimated session cost against a maximum.

  Args:
      input_tokens: Number of input tokens.
      output_tokens: Number of output tokens.
      max_cost_usd: Maximum acceptable cost in USD.
      input_cost_per_1k: Cost per 1K input tokens.
      output_cost_per_1k: Cost per 1K output tokens.

  Returns:
      Score between 0.0 (worst) and 1.0 (best).
  """
  cost = (input_tokens / 1000) * input_cost_per_1k + (
      output_tokens / 1000
  ) * output_cost_per_1k
  if cost <= 0:
    return 1.0
  if cost >= max_cost_usd:
    return 0.0
  return 1.0 - (cost / max_cost_usd)


# ------------------------------------------------------------------ #
# Event Label Normalization                                            #
# ------------------------------------------------------------------ #

_EVENT_LABEL_MAP: dict[str, str] = {
    "LLM_REQUEST": "llm",
    "LLM_RESPONSE": "llm",
    "TOOL_STARTING": "tool",
    "TOOL_COMPLETED": "tool",
    "TOOL_ERROR": "tool_error",
    "TOOL_PAUSED": "tool",
    "USER_MESSAGE_RECEIVED": "user",
    "AGENT_COMPLETED": "agent",
    # ADK 2.0 event types (producer #293).
    "AGENT_TRANSFER": "agent",
    "AGENT_STATE_CHECKPOINT": "agent",
    "EVENT_COMPACTION": "compaction",
    "WORKFLOW_NODE_STARTING": "workflow",
    "WORKFLOW_NODE_COMPLETED": "workflow",
}


def normalize_event_label(event_type: str) -> str:
  """Normalize an event_type string to a high-level category.

  Useful for grouping events in aggregate queries.

  Args:
      event_type: The event_type column value.

  Returns:
      One of ``"llm"``, ``"tool"``, ``"tool_error"``, ``"user"``,
      ``"agent"``, ``"compaction"``, ``"workflow"``, or ``"other"``.
  """
  return _EVENT_LABEL_MAP.get(event_type, "other")


# ------------------------------------------------------------------ #
# STRING Envelope Kernels                                              #
# ------------------------------------------------------------------ #


def eval_summary_json(
    avg_latency_ms: float,
    tool_calls: int,
    tool_errors: int,
    turn_count: int,
    total_tokens: int,
    avg_ttft_ms: float,
    input_tokens: int,
    output_tokens: int,
    threshold_ms: float,
    max_error_rate: float,
    max_turns: int,
    max_tokens: int,
    ttft_threshold_ms: float,
    max_cost_usd: float,
    input_cost_per_1k: float = 0.00025,
    output_cost_per_1k: float = 0.00125,
) -> str:
  """Compute all six scores and return a JSON STRING summary.

  This is a convenience kernel that calls all six score kernels and
  assembles the results into a single JSON object.  Useful when you
  want a complete evaluation summary in one column without calling
  six separate UDFs.

  Args:
      avg_latency_ms: Measured average latency in milliseconds.
      tool_calls: Total number of tool calls.
      tool_errors: Number of tool errors.
      turn_count: Measured number of conversation turns.
      total_tokens: Measured total token count.
      avg_ttft_ms: Measured average TTFT in milliseconds.
      input_tokens: Number of input tokens.
      output_tokens: Number of output tokens.
      threshold_ms: Maximum acceptable latency.
      max_error_rate: Maximum acceptable error fraction.
      max_turns: Maximum acceptable turn count.
      max_tokens: Maximum acceptable token count.
      ttft_threshold_ms: Maximum acceptable TTFT.
      max_cost_usd: Maximum acceptable cost in USD.
      input_cost_per_1k: Cost per 1K input tokens.
      output_cost_per_1k: Cost per 1K output tokens.

  Returns:
      JSON string with ``latency``, ``error_rate``, ``turn_count``,
      ``token_efficiency``, ``ttft``, ``cost``, and ``passed`` keys.
  """
  scores = {
      "latency": score_latency(avg_latency_ms, threshold_ms),
      "error_rate": score_error_rate(tool_calls, tool_errors, max_error_rate),
      "turn_count": score_turn_count(turn_count, max_turns),
      "token_efficiency": score_token_efficiency(total_tokens, max_tokens),
      "ttft": score_ttft(avg_ttft_ms, ttft_threshold_ms),
      "cost": score_cost(
          input_tokens,
          output_tokens,
          max_cost_usd,
          input_cost_per_1k,
          output_cost_per_1k,
      ),
  }
  scores["passed"] = all(v >= 0.5 for v in scores.values())
  return json.dumps(scores)
