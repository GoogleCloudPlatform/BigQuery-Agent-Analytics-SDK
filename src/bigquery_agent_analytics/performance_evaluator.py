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

from bigquery_agent_analytics.utils import _extract_json_from_text
from bigquery_agent_analytics.utils import _parse_json_from_text
from bigquery_agent_analytics.utils import strip_markdown_fences

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


class PerformanceEvaluator:
  """Evaluates agent traces stored in BigQuery to assess performance.

  This evaluator retrieves trace data from BigQuery and computes various
  metrics including trajectory matching, response quality, and custom metrics.

  Example:
      evaluator = PerformanceEvaluator(
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
      # ADK 2.0 event types (producer #293).
      "AGENT_TRANSFER",
      "EVENT_COMPACTION",
      "AGENT_STATE_CHECKPOINT",
      "TOOL_PAUSED",
      "WORKFLOW_NODE_STARTING",
      "WORKFLOW_NODE_COMPLETED",
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

  # One-Sided LLM Judge Prompt (No golden response required)
  _ONE_SIDED_JUDGE_PROMPT = """You are evaluating an AI agent's task execution trajectory and final response for sentiment and hallucination (faithfulness).

## Task Description
{task_description}

## Agent Trajectory (Actual)
{trajectory_json}

## Final Response (Actual)
{final_response}

## Instructions
Score the following criteria from 0 to 10:
1. sentiment: (0 to 10 scale) Was the tone positive, professional, helpful, and safe?
2. hallucination: (0 to 10 scale) Does the final response contain claims that are NOT supported by the captured tool call trajectory (i.e., hallucinating facts not retrieved by tools)? Score 10 for perfect grounding (no hallucinations), 0 for severe hallucination.

IMPORTANT: You MUST respond with ONLY a valid JSON object matching the format below. No explanation before or after.
Keep justification brief (under 100 characters).

Required JSON format:
{{
  "sentiment": 8,
  "hallucination": 10,
  "justification": "Brief reason explaining the scores"
}}
"""

  # Side-by-Side LLM Judge Prompt (Golden response required)
  _SIDE_BY_SIDE_JUDGE_PROMPT = """You are evaluating an AI agent's task execution trajectory and final response for correctness and efficiency against a golden reference response.

## Task Description
{task_description}

## Agent Trajectory (Actual)
{trajectory_json}

## Expected Trajectory (Golden, if provided)
{expected_trajectory}

## Golden Response (Ground Truth)
{golden_response}

## Final Response (Actual)
{final_response}

## Instructions
Evaluate the actual trajectory and response against the golden reference. You must score the following criteria:
1. final_answer_correct: (Binary: 1 for yes/pass, or 0 for no/fail) Does the agent's final response accurately address the user's request and contain the key facts matching the golden response?
2. tool_usage_correct: (Binary: 1 for yes/pass, or 0 for no/fail) Did the agent use the correct tools with correct arguments as recorded in the trajectory?
3. sound_reasoning: (Binary: 1 for yes/pass, or 0 for no/fail) Was the agent's reasoning sound and logical throughout the conversation?
4. efficiency: (Binary: 1 for yes/pass, or 0 for no/fail) Were all tool calls necessary and minimal? Fails (0) if there are redundant or excessive tool calls.

IMPORTANT: You MUST respond with ONLY a valid JSON object matching the format below. No explanation before or after.
Keep justification brief (under 100 characters).

Required JSON format:
{{
  "final_answer_correct": 1,
  "tool_usage_correct": 1,
  "sound_reasoning": 1,
  "efficiency": 1,
  "justification": "Brief reason explaining the scores"
}}
"""

  def __init__(
      self,
      project_id: str = "proj",
      dataset_id: str = "ds",
      table_id: str = "agent_events",
      client: Optional[bigquery.Client] = None,
      llm_judge_model: Optional[str] = None,
      include_event_types: Optional[list[str]] = None,
      name: Optional[str] = None,
  ) -> None:
    """Initializes the PerformanceEvaluator."""
    self.project_id = project_id
    self.dataset_id = dataset_id
    self.table_id = table_id
    self.table_ref = f"{project_id}.{dataset_id}.{table_id}"
    self._client = client
    self._warned_unlabeled_client = False
    self.llm_judge_model = llm_judge_model or "gemini-2.5-flash"
    self.include_event_types = include_event_types or self._DEFAULT_EVENT_TYPES
    self._custom_rubrics: list[dict[str, Any]] = []

  @property
  def name(self) -> str:
    return "performance_evaluator"

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

  def add_rubric(
      self,
      name: str,
      prompt_template: str,
      score_key: str,
      threshold: float = 0.5,
  ) -> PerformanceEvaluator:
    """Adds a custom LLM rubric to the PerformanceEvaluator.

    Args:
        name: Rubric metric name.
        prompt_template: Prompt with {trace_text} and {final_response}
          placeholders.
        score_key: JSON key in LLM response containing score.
        threshold: Pass/fail threshold (0-1 scale).

    Returns:
        Self for chaining.
    """
    self._custom_rubrics.append(
        {
            "name": name,
            "prompt_template": prompt_template,
            "score_key": score_key,
            "threshold": threshold,
        }
    )
    return self

  async def get_session_trace(self, session_id: str) -> SessionTrace:
    """Retrieves the complete trace for a session."""
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
    job_config = with_sdk_labels(job_config, feature="trace-read")

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

    trace.extract_tool_trajectory()
    trace.final_response = trace.extract_final_response()

    if events:
      start = min(e.timestamp for e in events)
      end = max(e.timestamp for e in events)
      trace.total_latency_ms = int((end - start).total_seconds() * 1000)

    return trace

  def evaluate_deterministic_trajectory(
      self,
      trace: SessionTrace,
      golden_trajectory: list[dict[str, Any]],
      match_type: MatchType = MatchType.EXACT,
  ) -> dict[str, float]:
    """Computes deterministic trajectory matching and step efficiency scores.

    Args:
        trace: The SessionTrace object containing actual tool calls.
        golden_trajectory: Optimal tool calls expected.
        match_type: Matching criteria strategy.

    Returns:
        A dict of computed deterministic scores.
    """
    scores: dict[str, float] = {}
    if match_type == MatchType.EXACT:
      scores["trajectory_exact_match"] = TrajectoryMetrics.compute_exact_match(
          trace.tool_calls, golden_trajectory
      )
    elif match_type == MatchType.IN_ORDER:
      scores["trajectory_in_order"] = TrajectoryMetrics.compute_in_order_match(
          trace.tool_calls, golden_trajectory
      )
    elif match_type == MatchType.ANY_ORDER:
      scores["trajectory_any_order"] = (
          TrajectoryMetrics.compute_any_order_match(
              trace.tool_calls, golden_trajectory
          )
      )

    scores["step_efficiency"] = TrajectoryMetrics.compute_step_efficiency(
        len(trace.tool_calls),
        len(golden_trajectory),
    )
    return scores

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
    """Evaluates a single session against golden data."""
    trace = await self.get_session_trace(session_id)

    scores: dict[str, float] = {}
    details: dict[str, Any] = {
        "actual_tool_calls": len(trace.tool_calls),
        "expected_tool_calls": (
            len(golden_trajectory) if golden_trajectory else 0
        ),
    }

    if golden_trajectory is not None:
      scores.update(
          self.evaluate_deterministic_trajectory(
              trace, golden_trajectory, match_type
          )
      )

    llm_feedback = None
    if use_llm_judge:
      llm_scores, llm_feedback = await self.llm_judge_evaluate(
          trace=trace,
          task_description=task_description or "Complete the user's request.",
          expected_trajectory=golden_trajectory,
          golden_response=golden_response,
      )
      scores.update(llm_scores)

      # Custom LLM rubrics
      if self._custom_rubrics:
        trace_text = "\n".join(
            f"{e.event_type}: {json.dumps(e.content)}" for e in trace.events
        )
        feedback_parts = []
        if llm_feedback:
          feedback_parts.append(llm_feedback)
        for rubric in self._custom_rubrics:
          score, feedback = await self._evaluate_custom_rubric(
              rubric,
              trace_text,
              trace.final_response,
              golden_response,
          )
          scores[rubric["name"]] = score
          if feedback:
            feedback_parts.append(f"{rubric['name']}: {feedback}")
        llm_feedback = "\n".join(feedback_parts)

    if custom_metrics:
      for metric_name, metric_fn in custom_metrics.items():
        try:
          score = metric_fn(trace, golden_trajectory, golden_response)
          scores[metric_name] = float(score)
        except Exception as e:
          logger.warning("Custom metric %s failed: %s", metric_name, e)
          scores[metric_name] = 0.0

    thresholds = thresholds or {}
    passed = True
    for metric_name, score in scores.items():
      threshold = thresholds.get(metric_name, 0.5)
      if score < threshold:
        passed = False
        details[f"{metric_name}_threshold"] = threshold

    return EvaluationResult(
        session_id=session_id,
        eval_status=EvalStatus.PASSED if passed else EvalStatus.FAILED,
        scores=scores,
        overall_score=sum(scores.values()) / len(scores) if scores else None,
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
    """Evaluates multiple sessions from an eval dataset."""
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

  async def llm_judge_evaluate(
      self,
      trace: SessionTrace,
      task_description: str,
      expected_trajectory: Optional[list[dict[str, Any]]],
      golden_response: Optional[str] = None,
  ) -> tuple[dict[str, float], str]:
    """Uses LLM as judge to evaluate the trace."""
    try:
      from google import genai
      from google.genai import types
    except ImportError:
      logger.warning("google-genai not installed, skipping LLM judge.")
      return {}, "LLM judge unavailable - google-genai not installed"

    trajectory_data = [
        {
            "tool": tc.tool_name,
            "args": tc.args,
            "status": tc.status,
        }
        for tc in trace.tool_calls
    ]

    # Generate prompts
    one_sided_prompt = self._ONE_SIDED_JUDGE_PROMPT.format(
        task_description=task_description,
        trajectory_json=json.dumps(trajectory_data, indent=2),
        final_response=trace.final_response or "No response captured",
    )

    side_by_side_prompt = None
    if golden_response:
      side_by_side_prompt = self._SIDE_BY_SIDE_JUDGE_PROMPT.format(
          task_description=task_description,
          trajectory_json=json.dumps(trajectory_data, indent=2),
          expected_trajectory=json.dumps(expected_trajectory, indent=2)
          if expected_trajectory
          else "Not provided",
          golden_response=golden_response,
          final_response=trace.final_response or "No response captured",
      )

    scores = {}
    feedback_parts = []

    # 1. Run One-Sided Evaluation
    try:
      client = genai.Client()
      response = await client.aio.models.generate_content(
          model=self.llm_judge_model,
          contents=one_sided_prompt,
          config=types.GenerateContentConfig(
              temperature=0.1,
              max_output_tokens=1024,
          ),
      )
      response_text = (response.text or "").strip()
      json_str = _extract_json_from_text(response_text)
      if json_str:
        result = json.loads(json_str)
        sentiment = float(result.get("sentiment", 10)) / 10.0
        hallucination = float(result.get("hallucination", 10)) / 10.0
        scores["llm_judge_sentiment"] = sentiment
        scores["llm_judge_hallucination"] = hallucination
        feedback_parts.append(result.get("justification", response_text))
    except Exception as e:
      logger.warning("One-sided LLM evaluation failed: %s", e)

    # 2. Run Side-by-Side Evaluation
    if side_by_side_prompt:
      try:
        client = genai.Client()
        response = await client.aio.models.generate_content(
            model=self.llm_judge_model,
            contents=side_by_side_prompt,
            config=types.GenerateContentConfig(
                temperature=0.1,
                max_output_tokens=1024,
            ),
        )
        response_text = (response.text or "").strip()
        json_str = _extract_json_from_text(response_text)
        if json_str:
          result = json.loads(json_str)
          final_answer_correct = float(result.get("final_answer_correct", 0))
          tool_usage_correct = float(result.get("tool_usage_correct", 0))
          sound_reasoning = float(result.get("sound_reasoning", 0))
          efficiency = float(result.get("efficiency", 0))

          scores["llm_judge_final_answer_correct"] = final_answer_correct
          scores["llm_judge_tool_usage_correct"] = tool_usage_correct
          scores["llm_judge_sound_reasoning"] = sound_reasoning
          scores["llm_judge_efficiency"] = efficiency

          scores["llm_judge_correctness"] = (
              1.0
              if (
                  final_answer_correct == 1.0
                  and tool_usage_correct == 1.0
                  and sound_reasoning == 1.0
              )
              else 0.0
          )
          feedback_parts.append(result.get("justification", response_text))
      except Exception as e:
        logger.warning("Side-by-side LLM evaluation failed: %s", e)

    feedback = "\n".join(feedback_parts)
    return scores, feedback

  async def _evaluate_custom_rubric(
      self,
      rubric: dict[str, Any],
      trace_text: str,
      final_response: str,
      golden_response: Optional[str] = None,
  ) -> tuple[float, str]:
    """Evaluates a custom LLM rubric."""
    prompt = rubric["prompt_template"].format(
        trace_text=trace_text,
        final_response=final_response or "No response.",
        golden_response=golden_response or "No golden response.",
    )
    try:
      from google import genai
      from google.genai import types

      client = genai.Client()
      response = await client.aio.models.generate_content(
          model=self.llm_judge_model,
          contents=prompt,
          config=types.GenerateContentConfig(
              temperature=0.1,
              max_output_tokens=2048,
          ),
      )
      text = response.text.strip()
      result = _parse_json_from_text(text)
      if result and rubric["score_key"] in result:
        raw = float(result[rubric["score_key"]])
        score = raw / 10.0 if raw > 1.0 else raw
        justification = result.get("justification", "")
        return score, justification
      return 0.0, text
    except Exception as e:
      logger.warning("Custom rubric %s failed: %s", rubric["name"], e)
      return 0.0, str(e)


# Keep aliases for backward compatibility
BigQueryTraceEvaluator = PerformanceEvaluator


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

    differences: dict[str, Any] = {
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
    r1 = trace1.final_response
    r2 = trace2.final_response
    if r1 and r2:
      differences["response_match"] = r1.strip() == r2.strip()

    return differences
