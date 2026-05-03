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

"""Trace-Based Evaluation Harness for ADK Agents.

This module provides capabilities to evaluate agent behavior using stored
traces in BigQuery. It supports:

- Trajectory matching (exact, in-order, any-order)
- LLM-as-judge evaluation
- Custom metric scoring
- Deterministic replay for debugging

Example usage:
    evaluator = BigQueryTraceEvaluator(
        project_id="my-project",
        dataset_id="agent_analytics",
    )

    results = await evaluator.evaluate_session(
        session_id="session-123",
        golden_trajectory=[
            {"tool_name": "search", "args": {"query": "weather"}},
            {"tool_name": "format_response", "args": {}},
        ],
        golden_response="The weather is sunny.",
    )
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from enum import Enum
import json
import logging
from typing import Any, Callable, Optional

from google.cloud import bigquery
from pydantic import BaseModel
from pydantic import Field

from bigquery_agent_analytics.evaluators import strip_markdown_fences

from ._telemetry import LabeledBigQueryClient
from ._telemetry import make_bq_client
from ._telemetry import with_sdk_labels

logger = logging.getLogger("bigquery_agent_analytics." + __name__)


class MatchType(Enum):
  """The type of trajectory matching to use."""

  EXACT = "exact"
  """Requires perfect match between actual and expected tool calls."""

  IN_ORDER = "in_order"
  """Requires tools in same order, allows extra tools between."""

  ANY_ORDER = "any_order"
  """Requires all expected tools present, any order allowed."""


class EvalStatus(Enum):
  """Status of an evaluation."""

  PASSED = "passed"
  FAILED = "failed"
  NOT_EVALUATED = "not_evaluated"


@dataclass
class TraceEvent:
  """Represents a single event from a trace."""

  event_type: str
  agent: Optional[str]
  timestamp: datetime
  content: dict[str, Any]
  attributes: dict[str, Any]
  span_id: Optional[str] = None
  parent_span_id: Optional[str] = None
  latency_ms: Optional[int] = None
  status: str = "OK"
  error_message: Optional[str] = None

  @classmethod
  def from_bigquery_row(cls, row: dict[str, Any]) -> "TraceEvent":
    """Creates a TraceEvent from a BigQuery row."""
    content = row.get("content")
    if isinstance(content, str):
      try:
        content = json.loads(content)
      except (json.JSONDecodeError, TypeError):
        content = {"raw": content}
    elif content is None:
      content = {}

    attributes = row.get("attributes")
    if isinstance(attributes, str):
      try:
        attributes = json.loads(attributes)
      except (json.JSONDecodeError, TypeError):
        attributes = {}
    elif attributes is None:
      attributes = {}

    latency_ms = row.get("latency_ms")
    if isinstance(latency_ms, str):
      try:
        latency_data = json.loads(latency_ms)
        latency_ms = latency_data.get("total_ms")
      except (json.JSONDecodeError, TypeError):
        latency_ms = None
    elif isinstance(latency_ms, dict):
      latency_ms = latency_ms.get("total_ms")

    return cls(
        event_type=row.get("event_type", "UNKNOWN"),
        agent=row.get("agent"),
        timestamp=row.get("timestamp", datetime.now()),
        content=content,
        attributes=attributes,
        span_id=row.get("span_id"),
        parent_span_id=row.get("parent_span_id"),
        latency_ms=latency_ms,
        status=row.get("status", "OK"),
        error_message=row.get("error_message"),
    )


@dataclass
class ToolCall:
  """Represents a tool call extracted from a trace."""

  tool_name: str
  args: dict[str, Any]
  result: Optional[dict[str, Any]] = None
  status: str = "OK"
  error_message: Optional[str] = None
  latency_ms: Optional[int] = None


@dataclass
class SessionTrace:
  """Complete trace for a session."""

  session_id: str
  user_id: Optional[str]
  events: list[TraceEvent]
  tool_calls: list[ToolCall] = field(default_factory=list)
  final_response: Optional[str] = None
  total_latency_ms: Optional[int] = None

  def extract_tool_trajectory(self) -> list[ToolCall]:
    """Extracts the tool call trajectory from events."""
    tool_calls = []
    tool_starts: dict[str, TraceEvent] = {}

    for event in self.events:
      if event.event_type == "TOOL_STARTING":
        tool_name = event.content.get("tool", "unknown")
        tool_starts[event.span_id or tool_name] = event

      elif event.event_type == "TOOL_COMPLETED":
        tool_name = event.content.get("tool", "unknown")
        start_event = tool_starts.pop(event.span_id or tool_name, None)

        args = {}
        if start_event:
          args = start_event.content.get("args", {})

        tool_calls.append(
            ToolCall(
                tool_name=tool_name,
                args=args,
                result=event.content.get("result"),
                status="OK",
                latency_ms=event.latency_ms,
            )
        )

      elif event.event_type == "TOOL_ERROR":
        tool_name = event.content.get("tool", "unknown")
        start_event = tool_starts.pop(event.span_id or tool_name, None)

        args = {}
        if start_event:
          args = start_event.content.get("args", {})

        tool_calls.append(
            ToolCall(
                tool_name=tool_name,
                args=args,
                status="ERROR",
                error_message=event.error_message,
                latency_ms=event.latency_ms,
            )
        )

    self.tool_calls = tool_calls
    return tool_calls

  def extract_final_response(self) -> Optional[str]:
    """Extracts the final agent response from events.

    Checks LLM_RESPONSE first (most reliable response source),
    then falls back to AGENT_COMPLETED.
    """
    # Prefer the last LLM_RESPONSE (most reliable response source)
    for event in reversed(self.events):
      if event.event_type == "LLM_RESPONSE":
        content = event.content
        if isinstance(content, dict):
          return content.get("response") or content.get("text_summary")
        return str(content) if content else None

    # Fallback to AGENT_COMPLETED
    for event in reversed(self.events):
      if event.event_type == "AGENT_COMPLETED":
        content = event.content
        if isinstance(content, dict):
          return content.get("response") or content.get("text_summary")
        return str(content) if content else None

    return None


class TrajectoryMetrics:
  """Computes trajectory-based evaluation metrics."""

  @staticmethod
  def compute_exact_match(
      actual: list[ToolCall],
      expected: list[dict[str, Any]],
  ) -> float:
    """Computes exact match score between trajectories.

    Args:
        actual: List of actual tool calls from trace.
        expected: List of expected tool calls with tool_name and args.

    Returns:
        Score between 0.0 and 1.0.
    """
    if not expected:
      return 1.0 if not actual else 0.0

    if len(actual) != len(expected):
      return 0.0

    matches = 0
    for act, exp in zip(actual, expected):
      if act.tool_name == exp.get("tool_name"):
        # Check args if specified
        exp_args = exp.get("args", {})
        if not exp_args or TrajectoryMetrics._args_match(act.args, exp_args):
          matches += 1

    return matches / len(expected)

  @staticmethod
  def compute_in_order_match(
      actual: list[ToolCall],
      expected: list[dict[str, Any]],
  ) -> float:
    """Computes in-order match score.

    Checks if expected tools appear in order within actual calls.

    Args:
        actual: List of actual tool calls.
        expected: List of expected tool calls.

    Returns:
        Score between 0.0 and 1.0.
    """
    if not expected:
      return 1.0

    expected_idx = 0
    for act in actual:
      if expected_idx >= len(expected):
        break

      exp = expected[expected_idx]
      if act.tool_name == exp.get("tool_name"):
        exp_args = exp.get("args", {})
        if not exp_args or TrajectoryMetrics._args_match(act.args, exp_args):
          expected_idx += 1

    return expected_idx / len(expected)

  @staticmethod
  def compute_any_order_match(
      actual: list[ToolCall],
      expected: list[dict[str, Any]],
  ) -> float:
    """Computes any-order match score.

    Checks if all expected tools appear in actual calls (any order).

    Args:
        actual: List of actual tool calls.
        expected: List of expected tool calls.

    Returns:
        Score between 0.0 and 1.0.
    """
    if not expected:
      return 1.0

    remaining = list(expected)
    for act in actual:
      for i, exp in enumerate(remaining):
        if act.tool_name == exp.get("tool_name"):
          exp_args = exp.get("args", {})
          if not exp_args or TrajectoryMetrics._args_match(act.args, exp_args):
            remaining.pop(i)
            break

    matched = len(expected) - len(remaining)
    return matched / len(expected)

  @staticmethod
  def _args_match(actual: dict[str, Any], expected: dict[str, Any]) -> bool:
    """Checks if actual args contain expected args."""
    for key, value in expected.items():
      if key not in actual:
        return False
      if value is not None and actual[key] != value:
        return False
    return True

  @staticmethod
  def compute_step_efficiency(
      actual_steps: int,
      optimal_steps: int,
  ) -> float:
    """Computes step efficiency score.

    Args:
        actual_steps: Number of steps taken by agent.
        optimal_steps: Optimal number of steps.

    Returns:
        Score between 0.0 and 1.0 (1.0 = optimal or better).
    """
    if optimal_steps <= 0:
      return 1.0 if actual_steps == 0 else 0.0

    if actual_steps <= optimal_steps:
      return 1.0

    # Penalize extra steps with diminishing returns
    efficiency = optimal_steps / actual_steps
    return max(0.0, efficiency)


class EvaluationResult(BaseModel):
  """Result of evaluating a session trace."""

  session_id: str = Field(description="The session ID that was evaluated.")
  eval_status: EvalStatus = Field(description="Overall evaluation status.")
  scores: dict[str, float] = Field(
      default_factory=dict,
      description="Individual metric scores.",
  )
  overall_score: Optional[float] = Field(
      default=None,
      description="Overall weighted score if computed.",
  )
  details: dict[str, Any] = Field(
      default_factory=dict,
      description="Additional evaluation details.",
  )
  llm_judge_feedback: Optional[str] = Field(
      default=None,
      description="Feedback from LLM judge if used.",
  )


class BigQueryTraceEvaluator:
  """Evaluates agent traces stored in BigQuery.

  This evaluator retrieves trace data from BigQuery and computes various
  metrics including trajectory matching, response quality, and custom metrics.

  Example:
      evaluator = BigQueryTraceEvaluator(
          project_id="my-project",
          dataset_id="agent_analytics",
      )

      result = await evaluator.evaluate_session(
          session_id="sess-123",
          golden_trajectory=[{"tool_name": "search", "args": {"q": "test"}}],
      )
  """

  # SQL query to retrieve complete session trace
  _DEFAULT_EVENT_TYPES = [
      "USER_MESSAGE_RECEIVED",
      "AGENT_STARTING",
      "AGENT_COMPLETED",
      "TOOL_STARTING",
      "TOOL_COMPLETED",
      "TOOL_ERROR",
      "LLM_REQUEST",
      "LLM_RESPONSE",
      "LLM_ERROR",
      "INVOCATION_STARTING",
      "INVOCATION_COMPLETED",
      "STATE_DELTA",
      "HITL_CONFIRMATION_REQUEST",
      "HITL_CONFIRMATION_REQUEST_COMPLETED",
      "HITL_CREDENTIAL_REQUEST",
      "HITL_CREDENTIAL_REQUEST_COMPLETED",
      "HITL_INPUT_REQUEST",
      "HITL_INPUT_REQUEST_COMPLETED",
  ]

  _SESSION_TRACE_QUERY = """
  SELECT
    event_type,
    agent,
    timestamp,
    content,
    attributes,
    span_id,
    parent_span_id,
    latency_ms,
    status,
    error_message,
    user_id
  FROM `{project}.{dataset}.{table}`
  WHERE session_id = @session_id
    AND event_type IN UNNEST(@event_types)
  ORDER BY timestamp ASC
  """

  # Default LLM judge prompt for trajectory evaluation
  _LLM_JUDGE_PROMPT = """You are evaluating an AI agent's task execution trajectory.

## Task Description
{task_description}

## Agent Trajectory
{trajectory_json}

## Expected Trajectory (if provided)
{expected_trajectory}

## Final Response
{final_response}

## Evaluation Criteria
Score each criterion from 0 to 10:
1. task_completion: Did the agent successfully complete the task?
2. efficiency: Were the steps taken necessary and minimal?
3. tool_usage: Were the right tools used with correct arguments?
4. reasoning: Was the agent's reasoning sound?
5. overall: Overall score averaging the above.

IMPORTANT: You MUST respond with ONLY a valid JSON object. No explanation before or after.
Keep justification brief (under 100 characters).

Required JSON format:
{{"task_completion": 7, "efficiency": 8, "tool_usage": 9, "reasoning": 7, "overall": 8, "justification": "Brief reason"}}
"""

  def __init__(
      self,
      project_id: str,
      dataset_id: str,
      table_id: str = "agent_events",
      client: Optional[bigquery.Client] = None,
      llm_judge_model: Optional[str] = None,
      include_event_types: Optional[list[str]] = None,
  ) -> None:
    """Initializes the BigQueryTraceEvaluator.

    Args:
        project_id: Google Cloud project ID.
        dataset_id: BigQuery dataset ID containing trace data.
        table_id: BigQuery table ID. Defaults to "agent_events".
        client: Optional BigQuery client. Created if not provided.
        llm_judge_model: Optional model name for LLM-as-judge evaluation.
        include_event_types: Optional list of event types to include
            when fetching session traces.  Defaults to all standard
            ADK event types including HITL and STATE_DELTA.  Pass a
            custom list to restrict or extend the event types
            evaluated without patching SQL templates.
    """
    self.project_id = project_id
    self.dataset_id = dataset_id
    self.table_id = table_id
    self.table_ref = f"{project_id}.{dataset_id}.{table_id}"
    self._client = client
    self._warned_unlabeled_client = False
    self.llm_judge_model = llm_judge_model or "gemini-2.5-flash"
    self.include_event_types = include_event_types or self._DEFAULT_EVENT_TYPES

  @property
  def client(self) -> bigquery.Client:
    """Lazily initializes and returns the BigQuery client."""
    if self._client is None:
      self._client = make_bq_client(self.project_id)
    elif isinstance(self._client, bigquery.Client) and not isinstance(
        self._client, LabeledBigQueryClient
    ):
      if not self._warned_unlabeled_client:
        logger.warning(
            "User-provided bigquery.Client is not a "
            "LabeledBigQueryClient; SDK telemetry labels will not be "
            "applied to jobs from this client. To opt in, construct "
            "the client via bigquery_agent_analytics.make_bq_client() "
            "or pass a LabeledBigQueryClient directly."
        )
        self._warned_unlabeled_client = True
    return self._client

  async def get_session_trace(self, session_id: str) -> SessionTrace:
    """Retrieves the complete trace for a session.

    Args:
        session_id: The session ID to retrieve.

    Returns:
        SessionTrace containing all events for the session.
    """
    query = self._SESSION_TRACE_QUERY.format(
        project=self.project_id,
        dataset=self.dataset_id,
        table=self.table_id,
    )

    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter(
                "session_id",
                "STRING",
                session_id,
            ),
            bigquery.ArrayQueryParameter(
                "event_types",
                "STRING",
                self.include_event_types,
            ),
        ]
    )
    # Apply labels BEFORE executor dispatch so they materialize on the
    # QueryJobConfig in the caller's thread.
    job_config = with_sdk_labels(job_config, feature="trace-read")

    # Run query in executor to avoid blocking
    loop = asyncio.get_event_loop()
    query_job = await loop.run_in_executor(
        None,
        lambda: self.client.query(query, job_config=job_config),
    )

    results = await loop.run_in_executor(None, lambda: list(query_job.result()))

    events = [TraceEvent.from_bigquery_row(dict(row)) for row in results]

    user_id = None
    if results:
      user_id = results[0].get("user_id")

    trace = SessionTrace(
        session_id=session_id,
        user_id=user_id,
        events=events,
    )

    # Extract tool trajectory and final response
    trace.extract_tool_trajectory()
    trace.final_response = trace.extract_final_response()

    # Compute total latency
    if events:
      start = min(e.timestamp for e in events)
      end = max(e.timestamp for e in events)
      trace.total_latency_ms = int((end - start).total_seconds() * 1000)

    return trace

  async def evaluate_session(
      self,
      session_id: str,
      golden_trajectory: Optional[list[dict[str, Any]]] = None,
      golden_response: Optional[str] = None,
      match_type: MatchType = MatchType.EXACT,
      task_description: Optional[str] = None,
      use_llm_judge: bool = False,
      custom_metrics: Optional[dict[str, Callable]] = None,
      thresholds: Optional[dict[str, float]] = None,
  ) -> EvaluationResult:
    """Evaluates a single session against golden data.

    Args:
        session_id: The session ID to evaluate.
        golden_trajectory: Expected tool call sequence.
        golden_response: Expected final response.
        match_type: Type of trajectory matching to use.
        task_description: Description of the task for LLM judge.
        use_llm_judge: Whether to use LLM-as-judge evaluation.
        custom_metrics: Dict of custom metric functions.
        thresholds: Dict of metric name to threshold for pass/fail.

    Returns:
        EvaluationResult with scores and status.
    """
    # Retrieve trace
    trace = await self.get_session_trace(session_id)

    scores: dict[str, float] = {}
    details: dict[str, Any] = {
        "actual_tool_calls": len(trace.tool_calls),
        "expected_tool_calls": (
            len(golden_trajectory) if golden_trajectory else 0
        ),
    }

    # Compute trajectory score
    if golden_trajectory is not None:
      if match_type == MatchType.EXACT:
        scores["trajectory_exact_match"] = (
            TrajectoryMetrics.compute_exact_match(
                trace.tool_calls, golden_trajectory
            )
        )
      elif match_type == MatchType.IN_ORDER:
        scores["trajectory_in_order"] = (
            TrajectoryMetrics.compute_in_order_match(
                trace.tool_calls, golden_trajectory
            )
        )
      elif match_type == MatchType.ANY_ORDER:
        scores["trajectory_any_order"] = (
            TrajectoryMetrics.compute_any_order_match(
                trace.tool_calls, golden_trajectory
            )
        )

      # Step efficiency
      if golden_trajectory:
        scores["step_efficiency"] = TrajectoryMetrics.compute_step_efficiency(
            len(trace.tool_calls),
            len(golden_trajectory),
        )

    # Response matching (simple text comparison)
    if golden_response is not None and trace.final_response is not None:
      scores["response_match"] = self._compute_response_match(
          trace.final_response, golden_response
      )

    # LLM-as-judge evaluation
    llm_feedback = None
    if use_llm_judge:
      llm_scores, llm_feedback = await self._llm_judge_evaluate(
          trace=trace,
          task_description=task_description or "Complete the user's request.",
          expected_trajectory=golden_trajectory,
      )
      scores.update(llm_scores)

    # Custom metrics
    if custom_metrics:
      for metric_name, metric_fn in custom_metrics.items():
        try:
          score = metric_fn(trace, golden_trajectory, golden_response)
          scores[metric_name] = float(score)
        except Exception as e:
          logger.warning("Custom metric %s failed: %s", metric_name, e)
          scores[metric_name] = 0.0

    # Determine overall status
    thresholds = thresholds or {}
    passed = True
    for metric_name, score in scores.items():
      threshold = thresholds.get(metric_name, 0.5)
      if score < threshold:
        passed = False
        details[f"{metric_name}_threshold"] = threshold

    # Compute overall score as mean
    overall_score = None
    if scores:
      overall_score = sum(scores.values()) / len(scores)

    return EvaluationResult(
        session_id=session_id,
        eval_status=EvalStatus.PASSED if passed else EvalStatus.FAILED,
        scores=scores,
        overall_score=overall_score,
        details=details,
        llm_judge_feedback=llm_feedback,
    )

  async def evaluate_batch(
      self,
      eval_dataset: list[dict[str, Any]],
      match_type: MatchType = MatchType.EXACT,
      use_llm_judge: bool = False,
      concurrency: int = 5,
  ) -> list[EvaluationResult]:
    """Evaluates multiple sessions from an eval dataset.

    Args:
        eval_dataset: List of dicts with session_id, expected_trajectory, etc.
        match_type: Type of trajectory matching.
        use_llm_judge: Whether to use LLM judge.
        concurrency: Max concurrent evaluations.

    Returns:
        List of EvaluationResult for each session.
    """
    semaphore = asyncio.Semaphore(concurrency)

    async def evaluate_one(item: dict[str, Any]) -> EvaluationResult:
      async with semaphore:
        return await self.evaluate_session(
            session_id=item["session_id"],
            golden_trajectory=item.get("expected_trajectory"),
            golden_response=item.get("expected_response"),
            match_type=match_type,
            task_description=item.get("task_description"),
            use_llm_judge=use_llm_judge,
            thresholds=item.get("thresholds"),
        )

    tasks = [evaluate_one(item) for item in eval_dataset]
    return await asyncio.gather(*tasks)

  def _compute_response_match(
      self,
      actual: str,
      expected: str,
  ) -> float:
    """Computes simple response match score.

    Args:
        actual: Actual response text.
        expected: Expected response text.

    Returns:
        Score between 0.0 and 1.0.
    """
    if not actual or not expected:
      return 0.0 if actual != expected else 1.0

    # Normalize strings
    actual_norm = actual.lower().strip()
    expected_norm = expected.lower().strip()

    if actual_norm == expected_norm:
      return 1.0

    # Simple word overlap score
    actual_words = set(actual_norm.split())
    expected_words = set(expected_norm.split())

    if not expected_words:
      return 1.0 if not actual_words else 0.0

    intersection = actual_words & expected_words
    return len(intersection) / len(expected_words)

  async def _llm_judge_evaluate(
      self,
      trace: SessionTrace,
      task_description: str,
      expected_trajectory: Optional[list[dict[str, Any]]],
  ) -> tuple[dict[str, float], str]:
    """Uses LLM as judge to evaluate the trace.

    Args:
        trace: The session trace to evaluate.
        task_description: Description of the task.
        expected_trajectory: Expected tool calls if available.

    Returns:
        Tuple of (scores dict, feedback string).
    """
    try:
      from google import genai
      from google.genai import types
    except ImportError:
      logger.warning("google-genai not installed, skipping LLM judge.")
      return {}, "LLM judge unavailable - google-genai not installed"

    # Format trajectory for prompt
    trajectory_data = [
        {
            "tool": tc.tool_name,
            "args": tc.args,
            "status": tc.status,
        }
        for tc in trace.tool_calls
    ]

    prompt = self._LLM_JUDGE_PROMPT.format(
        task_description=task_description,
        trajectory_json=json.dumps(trajectory_data, indent=2),
        expected_trajectory=json.dumps(expected_trajectory, indent=2)
        if expected_trajectory
        else "Not provided",
        final_response=trace.final_response or "No response captured",
    )

    try:
      client = genai.Client()
      response = await client.aio.models.generate_content(
          model=self.llm_judge_model,
          contents=prompt,
          config=types.GenerateContentConfig(
              temperature=0.1,
              max_output_tokens=1024,
          ),
      )

      response_text = response.text.strip()

      # Strip markdown fences and extract JSON
      if response_text.startswith("```"):
        json_str = strip_markdown_fences(response_text)
      else:
        json_str = None
      if not json_str:
        # No fences found — try to extract JSON object directly
        if "{" in response_text:
          try:
            start = response_text.index("{")
            brace_count = 0
            end = start
            for i, char in enumerate(response_text[start:], start):
              if char == "{":
                brace_count += 1
              elif char == "}":
                brace_count -= 1
                if brace_count == 0:
                  end = i + 1
                  break
            json_str = response_text[start:end]
          except (ValueError, IndexError):
            pass

      if not json_str:
        return {}, response_text

      # Clean up the JSON string - handle common issues
      json_str = json_str.strip()
      # Remove control characters that break JSON parsing
      json_str = "".join(
          char for char in json_str if char >= " " or char in "\n\r\t"
      )

      try:
        result = json.loads(json_str)
      except json.JSONDecodeError:
        # Try to fix common JSON issues
        import re

        # Replace unescaped newlines in strings
        fixed_json = re.sub(r"(?<!\\)\\n", "\\\\n", json_str)
        # Try again with fixed JSON
        try:
          result = json.loads(fixed_json)
        except json.JSONDecodeError:
          # Last resort: extract scores using regex
          result = {}
          for key in [
              "task_completion",
              "efficiency",
              "tool_usage",
              "reasoning",
              "overall",
          ]:
            match = re.search(rf'"{key}"\s*:\s*(\d+(?:\.\d+)?)', json_str)
            if match:
              result[key] = float(match.group(1))
          if not result:
            return {}, f"Failed to parse LLM response: {response_text[:200]}"

      scores = {}
      for key in ["task_completion", "efficiency", "tool_usage", "reasoning"]:
        if key in result:
          # Normalize 0-10 scale to 0-1
          scores[f"llm_judge_{key}"] = float(result[key]) / 10.0

      if "overall" in result:
        scores["llm_judge_overall"] = float(result["overall"]) / 10.0

      feedback = result.get("justification", response_text)
      return scores, feedback

    except Exception as e:
      logger.warning("LLM judge evaluation failed: %s", e)
      return {}, f"LLM judge failed: {str(e)}"


@dataclass
class ReplayContext:
  """Context for deterministic trace replay."""

  llm_responses: dict[int, str] = field(default_factory=dict)
  tool_responses: dict[str, Any] = field(default_factory=dict)
  current_step: int = 0

  def inject_llm_response(self, response: str) -> None:
    """Injects a recorded LLM response for replay."""
    self.llm_responses[self.current_step] = response
    self.current_step += 1

  def inject_tool_response(self, tool_name: str, response: Any) -> None:
    """Injects a recorded tool response for replay."""
    self.tool_responses[tool_name] = response

  def get_llm_response(self, step: int) -> Optional[str]:
    """Gets injected LLM response for a step."""
    return self.llm_responses.get(step)

  def get_tool_response(self, tool_name: str) -> Optional[Any]:
    """Gets injected tool response."""
    return self.tool_responses.get(tool_name)


class TraceReplayRunner:
  """Replays agent sessions deterministically for debugging.

  This runner uses recorded traces to replay agent execution with
  deterministic outcomes, useful for debugging and root cause analysis.

  Example:
      replay_runner = TraceReplayRunner(evaluator)
      result = await replay_runner.replay_session(
          session_id="sess-123",
          replay_mode="step",
      )
  """

  def __init__(self, evaluator: BigQueryTraceEvaluator) -> None:
    """Initializes the replay runner.

    Args:
        evaluator: BigQueryTraceEvaluator for trace retrieval.
    """
    self.evaluator = evaluator

  async def replay_session(
      self,
      session_id: str,
      replay_mode: str = "full",
      step_callback: Optional[
          Callable[[TraceEvent, ReplayContext], None]
      ] = None,
  ) -> ReplayContext:
    """Replays a recorded session step by step.

    Args:
        session_id: The session ID to replay.
        replay_mode: "full" for all events, "step" for pause at each step,
                     "tool_only" for only tool calls.
        step_callback: Optional callback invoked at each step.

    Returns:
        ReplayContext with all injected responses.
    """
    trace = await self.evaluator.get_session_trace(session_id)

    replay_context = ReplayContext()

    for event in trace.events:
      # Filter by mode
      if replay_mode == "tool_only" and event.event_type not in [
          "TOOL_STARTING",
          "TOOL_COMPLETED",
          "TOOL_ERROR",
      ]:
        continue

      # Inject responses for replay
      if event.event_type == "LLM_RESPONSE":
        content = event.content
        response_text = ""
        if isinstance(content, dict):
          response_text = content.get("response", "")
        elif content:
          response_text = str(content)
        replay_context.inject_llm_response(response_text)

      elif event.event_type == "TOOL_COMPLETED":
        tool_name = event.content.get("tool", "unknown")
        result = event.content.get("result")
        replay_context.inject_tool_response(tool_name, result)

      # Invoke callback if provided
      if step_callback:
        step_callback(event, replay_context)

    return replay_context

  async def compare_replays(
      self,
      session_id_1: str,
      session_id_2: str,
  ) -> dict[str, Any]:
    """Compares two session replays to identify differences.

    Args:
        session_id_1: First session ID.
        session_id_2: Second session ID.

    Returns:
        Dict with comparison results.
    """
    trace1 = await self.evaluator.get_session_trace(session_id_1)
    trace2 = await self.evaluator.get_session_trace(session_id_2)

    differences = {
        "event_count_diff": len(trace1.events) - len(trace2.events),
        "tool_count_diff": len(trace1.tool_calls) - len(trace2.tool_calls),
        "tool_differences": [],
        "response_match": False,
    }

    # Compare tool calls
    max_tools = max(len(trace1.tool_calls), len(trace2.tool_calls))
    for i in range(max_tools):
      tc1 = trace1.tool_calls[i] if i < len(trace1.tool_calls) else None
      tc2 = trace2.tool_calls[i] if i < len(trace2.tool_calls) else None

      if tc1 is None or tc2 is None:
        differences["tool_differences"].append(
            {
                "index": i,
                "trace1": tc1.tool_name if tc1 else None,
                "trace2": tc2.tool_name if tc2 else None,
            }
        )
      elif tc1.tool_name != tc2.tool_name or tc1.args != tc2.args:
        differences["tool_differences"].append(
            {
                "index": i,
                "trace1": {"name": tc1.tool_name, "args": tc1.args},
                "trace2": {"name": tc2.tool_name, "args": tc2.args},
            }
        )

    # Compare responses
    if trace1.final_response and trace2.final_response:
      differences["response_match"] = (
          trace1.final_response.strip() == trace2.final_response.strip()
      )

    return differences
