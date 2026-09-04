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
    evaluator = PerformanceEvaluator(
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
from collections import deque
import copy
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from enum import Enum
import json
import logging
import math
from typing import Any, Callable, Optional

from google.cloud import bigquery
from pydantic import BaseModel
from pydantic import Field

from bigquery_agent_analytics.utils import _extract_json_from_text
from bigquery_agent_analytics.utils import _parse_json_from_text
from bigquery_agent_analytics.utils import strip_markdown_fences

from ._telemetry import LabeledBigQueryClient
from ._telemetry import make_bq_client
from .trace import Trace
from .trace import TraceIdentity
from .trace import TraceScope
from .trace import TraceSelector
from .trace import UNSET

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
  latency_ms: Optional[float] = None
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
  latency_ms: Optional[float] = None


@dataclass
class SessionTrace:
  """Complete trace for a session."""

  session_id: str
  user_id: Optional[str]
  events: list[TraceEvent]
  tool_calls: list[ToolCall] = field(default_factory=list)
  final_response: Optional[str] = None
  total_latency_ms: Optional[int] = None
  identity: Optional[TraceIdentity] = None
  scope: Optional[TraceScope] = None
  scope_coverage: Optional[tuple[str, ...]] = None

  def extract_tool_trajectory(self) -> list[ToolCall]:
    """Extracts tool calls even when stable SQL ties put terminals first."""
    tool_calls = []
    tool_starts: dict[tuple[str, str], deque[TraceEvent]] = {}
    for event in self.events:
      if event.event_type == "TOOL_STARTING":
        tool_name = event.content.get("tool", "unknown")
        key = ("span", event.span_id) if event.span_id else ("tool", tool_name)
        tool_starts.setdefault(key, deque()).append(event)

    for event in self.events:
      if event.event_type == "TOOL_COMPLETED":
        tool_name = event.content.get("tool", "unknown")
        key = ("span", event.span_id) if event.span_id else ("tool", tool_name)
        starts = tool_starts.get(key)
        start_event = None
        if starts and starts[0].timestamp <= event.timestamp:
          start_event = starts.popleft()

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
        key = ("span", event.span_id) if event.span_id else ("tool", tool_name)
        starts = tool_starts.get(key)
        start_event = None
        if starts and starts[0].timestamp <= event.timestamp:
          start_event = starts.popleft()

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
  """Evaluates agent traces stored in BigQuery.

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
    self._name = name if name is not None else "performance_evaluator"
    self.project_id = project_id
    self.dataset_id = dataset_id
    self.table_id = table_id
    self.table_ref = f"{project_id}.{dataset_id}.{table_id}"
    self._client = client
    self._trace_client = None
    self._warned_unlabeled_client = False
    self.llm_judge_model = llm_judge_model or "gemini-2.5-flash"
    self.include_event_types = include_event_types or self._DEFAULT_EVENT_TYPES
    self._custom_rubrics: list[dict[str, Any]] = []

  @property
  def name(self) -> str:
    return self._name

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
        prompt_template: Prompt with {trace_text}, {final_response}, and
          {golden_response} placeholders. The judge is instructed to return
          a JSON score on a 0-10 scale.
        score_key: Required numeric JSON key in the judge response (0-10),
          normalized to 0-1 by dividing by 10.
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

  @property
  def trace_client(self):
    """Returns the shared identity-safe trace resolver."""
    if self._trace_client is None:
      # Import lazily so importing the standalone evaluator does not
      # eagerly load the full public client surface.
      from .client import Client

      self._trace_client = Client(
          project_id=self.project_id,
          dataset_id=self.dataset_id,
          table_id=self.table_id,
          verify_schema=False,
          bq_client=self.client,
      )
    return self._trace_client

  @staticmethod
  def _to_session_trace(
      trace: Trace,
      include_event_types: list[str],
  ) -> SessionTrace:
    """Converts the U2 trace model without weakening its attribution."""
    included = set(include_event_types)
    events = [
        TraceEvent(
            event_type=span.event_type,
            agent=span.agent,
            timestamp=span.timestamp,
            content=copy.deepcopy(span.content),
            attributes=copy.deepcopy(span.attributes),
            span_id=span.span_id,
            parent_span_id=span.parent_span_id,
            latency_ms=span.latency_ms,
            status=span.status,
            error_message=span.error_message,
        )
        for span in trace.spans
        if span.event_type in included
    ]
    result = SessionTrace(
        session_id=trace.session_id,
        user_id=trace.user_id,
        events=events,
        identity=trace.identity,
        scope=trace.scope,
        scope_coverage=trace.scope_coverage,
    )
    result.extract_tool_trajectory()
    result.final_response = result.extract_final_response()
    if events:
      start = min(event.timestamp for event in events)
      end = max(event.timestamp for event in events)
      result.total_latency_ms = int((end - start).total_seconds() * 1000)
    return result

  async def get_session_trace(
      self,
      session_id: str,
      *,
      user_id: Any = UNSET,
      root_agent_name: Any = UNSET,
      experiment_id: Any = UNSET,
      custom_labels: Optional[dict[str, str]] = None,
      scope_signature: Optional[str] = None,
      allow_mixed_scope: bool = False,
  ) -> SessionTrace:
    """Retrieves one identity-safe trace for a session.

    The scalar pins use the same three-state contract as
    :meth:`Client.get_session_trace`: omitted means unpinned, explicit
    ``None`` pins SQL NULL, and strings pin equality.
    """
    return await self.get_trace_by_selector(
        TraceSelector(
            session_id=session_id,
            user_id=user_id,
            root_agent_name=root_agent_name,
            experiment_id=experiment_id,
            custom_labels=custom_labels,
            scope_signature=scope_signature,
        ),
        allow_mixed_scope=allow_mixed_scope,
    )

  async def get_trace_by_selector(
      self,
      selector: TraceSelector,
      *,
      allow_mixed_scope: bool = False,
  ) -> SessionTrace:
    """Retrieves a trace through the shared U2 candidate resolver."""
    if type(selector) is not TraceSelector:
      raise TypeError("selector must be a TraceSelector.")
    loop = asyncio.get_running_loop()
    trace = await loop.run_in_executor(
        None,
        lambda: self.trace_client.get_trace_by_selector(
            selector,
            allow_mixed_scope=allow_mixed_scope,
            event_types=self.include_event_types,
        ),
    )
    return self._to_session_trace(trace, self.include_event_types)

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
      selector: Optional[TraceSelector] = None,
      *,
      trace: Trace | SessionTrace | None = None,
  ) -> EvaluationResult:
    """Evaluates a session against golden data and configured metrics.

    ``selector`` retains the exact identity/scope lookup contract. A
    materialized ``trace`` instead evaluates the caller's already selected
    data without another query. Its session ID must match ``session_id``;
    ``trace`` and ``selector`` cannot be supplied together. Missing or invalid
    requested metrics fail evaluation, including an entirely empty score set.
    """
    if trace is not None:
      if selector is not None:
        raise ValueError("Pass either trace or selector, not both.")
      if not isinstance(trace, (Trace, SessionTrace)):
        raise TypeError("trace must be a Trace or SessionTrace.")
      if trace.session_id != session_id:
        raise ValueError("trace.session_id must match session_id.")
      trace = (
          self._to_session_trace(trace, self.include_event_types)
          if isinstance(trace, Trace)
          else copy.deepcopy(trace)
      )
    elif selector is not None:
      if type(selector) is not TraceSelector:
        raise TypeError("selector must be a TraceSelector.")
      if selector.session_id != session_id:
        raise ValueError("selector.session_id must match session_id.")
      trace = await self.get_trace_by_selector(selector)
    else:
      trace = await self.get_session_trace(session_id)

    scores: dict[str, float] = {}
    errors: list[str] = []
    details: dict[str, Any] = {
        "actual_tool_calls": len(trace.tool_calls),
        "expected_tool_calls": (
            len(golden_trajectory) if golden_trajectory else 0
        ),
        "user_id": (
            trace.identity.user_id
            if trace.identity is not None
            else trace.user_id
        ),
        "root_agent_name": (
            trace.identity.root_agent_name
            if trace.identity is not None
            else None
        ),
        "experiment_id": (
            trace.scope.experiment_id if trace.scope is not None else None
        ),
        "scope_signature": (
            trace.scope.scope_signature if trace.scope is not None else None
        ),
    }
    if golden_trajectory is not None:
      scores.update(
          self.evaluate_deterministic_trajectory(
              trace, golden_trajectory, match_type
          )
      )

    if golden_response is not None:
      scores["response_match"] = (
          self._compute_response_match(trace.final_response, golden_response)
          if trace.final_response is not None
          else 0.0
      )

    llm_feedback = None
    if use_llm_judge:
      try:
        llm_scores, llm_feedback = await self.llm_judge_evaluate(
            trace=trace,
            task_description=task_description or "Complete the user's request.",
            expected_trajectory=golden_trajectory,
            golden_response=golden_response,
        )
        required = {"llm_judge_sentiment", "llm_judge_hallucination"}
        if golden_response is not None:
          required.update(
              {
                  "llm_judge_final_answer_correct",
                  "llm_judge_tool_usage_correct",
                  "llm_judge_sound_reasoning",
                  "llm_judge_efficiency",
                  "llm_judge_correctness",
              }
          )
        if not isinstance(llm_scores, dict):
          raise ValueError("LLM judge must return a score mapping.")
        missing = required - llm_scores.keys()
        if missing:
          errors.append(
              "LLM judge missing required metrics: "
              + ", ".join(sorted(missing))
          )
        for name, value in llm_scores.items():
          scores[name] = self._validated_score(value, name, 1.0)
      except Exception as exc:
        errors.append(f"LLM judge failed: {exc}")
        llm_feedback = str(exc)

      feedback_parts = [llm_feedback] if llm_feedback else []
      if self._custom_rubrics:
        trace_text = "\n".join(
            f"{e.event_type}: {json.dumps(e.content)}" for e in trace.events
        )
        for rubric in self._custom_rubrics:
          try:
            score, feedback = await self._evaluate_custom_rubric(
                rubric, trace_text, trace.final_response, golden_response
            )
            if score is None:
              errors.append(
                  f"Custom rubric {rubric['name']} failed: {feedback}"
              )
            else:
              scores[rubric["name"]] = self._validated_score(
                  score, rubric["name"], 1.0
              )
            if feedback:
              feedback_parts.append(f"{rubric['name']}: {feedback}")
          except Exception as exc:
            errors.append(f"Custom rubric {rubric['name']} failed: {exc}")
      llm_feedback = "\n".join(feedback_parts)

    if custom_metrics:
      for metric_name, metric_fn in custom_metrics.items():
        try:
          score = float(metric_fn(trace, golden_trajectory, golden_response))
          if not math.isfinite(score):
            raise ValueError("score must be finite")
          scores[metric_name] = score
        except Exception as exc:
          logger.warning("Custom metric %s failed: %s", metric_name, exc)
          errors.append(f"Custom metric {metric_name} failed: {exc}")

    if not scores:
      errors.append("No evaluation metrics were produced.")
    if errors:
      details["errors"] = errors
    rubric_thresholds = {
        r["name"]: r["threshold"] for r in self._custom_rubrics
    }
    rubric_thresholds.update(thresholds or {})
    passed = bool(scores) and not errors
    for metric_name, score in scores.items():
      threshold = rubric_thresholds.get(metric_name, 0.5)
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
        raw_selector = item.get("selector")
        selector = None
        if raw_selector is not None:
          if type(raw_selector) is TraceSelector:
            selector = raw_selector
          elif type(raw_selector) is dict:
            selector = TraceSelector(**raw_selector)
          else:
            raise TypeError(
                "eval dataset selector must be a TraceSelector or mapping."
            )
        session_id = item.get("session_id")
        if session_id is None and selector is not None:
          session_id = selector.session_id
        if session_id is None:
          raise ValueError("eval dataset item requires session_id or selector.")
        return await self.evaluate_session(
            session_id=session_id,
            golden_trajectory=item.get("expected_trajectory"),
            golden_response=item.get("expected_response"),
            match_type=match_type,
            task_description=item.get("task_description"),
            use_llm_judge=use_llm_judge,
            thresholds=item.get("thresholds"),
            selector=selector,
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

  @staticmethod
  def _validated_score(value: Any, key: str, maximum: float) -> float:
    """Require an actual finite JSON number within the rubric's scale."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
      raise ValueError(f"{key} must be a numeric score")
    value = float(value)
    if not math.isfinite(value) or not 0 <= value <= maximum:
      raise ValueError(f"{key} must be finite and between 0 and {maximum:g}")
    return value

  @classmethod
  def _judge_scores(
      cls, text: str, keys: tuple[str, ...], maximum: float
  ) -> dict[str, float]:
    result = _parse_json_from_text(text)
    if not isinstance(result, dict):
      raise ValueError("Judge response must contain a JSON score object")
    missing = set(keys) - result.keys()
    if missing:
      raise ValueError(
          "Missing required judge keys: " + ", ".join(sorted(missing))
      )
    scores = {}
    for key in keys:
      value = cls._validated_score(result[key], key, maximum)
      if maximum == 1.0 and value not in (0.0, 1.0):
        raise ValueError(f"{key} must be a binary score (0 or 1)")
      scores[f"llm_judge_{key}"] = value / maximum
    return scores

  async def llm_judge_evaluate(
      self,
      trace: SessionTrace,
      task_description: str,
      expected_trajectory: Optional[list[dict[str, Any]]],
      golden_response: Optional[str] = None,
  ) -> tuple[dict[str, float], str]:
    """Evaluate the authored one-sided and optional side-by-side rubrics.

    Each requested rubric must return all required finite numeric keys on
    its documented scale. Invalid rubrics produce no scores and explicit
    failure feedback; evaluate_session treats missing metrics as failure.
    """
    try:
      from google import genai
      from google.genai import types
    except ImportError:
      return {}, "LLM judge unavailable: google-genai is not installed"

    trajectory_data = [
        {"tool": tc.tool_name, "args": tc.args, "status": tc.status}
        for tc in trace.tool_calls
    ]
    prompts = [
        (
            "One-sided",
            self._ONE_SIDED_JUDGE_PROMPT.format(
                task_description=task_description,
                trajectory_json=json.dumps(trajectory_data, indent=2),
                final_response=trace.final_response or "No response captured",
            ),
            ("sentiment", "hallucination"),
            10.0,
        )
    ]
    if golden_response is not None:
      prompts.append(
          (
              "Side-by-side",
              self._SIDE_BY_SIDE_JUDGE_PROMPT.format(
                  task_description=task_description,
                  trajectory_json=json.dumps(trajectory_data, indent=2),
                  expected_trajectory=(
                      json.dumps(expected_trajectory, indent=2)
                      if expected_trajectory is not None
                      else "Not provided"
                  ),
                  golden_response=golden_response,
                  final_response=trace.final_response or "No response captured",
              ),
              (
                  "final_answer_correct",
                  "tool_usage_correct",
                  "sound_reasoning",
                  "efficiency",
              ),
              1.0,
          )
      )
    scores: dict[str, float] = {}
    feedback_parts: list[str] = []
    for label, prompt, keys, maximum in prompts:
      try:
        client = genai.Client()
        response = await client.aio.models.generate_content(
            model=self.llm_judge_model,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.1, max_output_tokens=1024
            ),
        )
        text = (response.text or "").strip()
        parsed_scores = self._judge_scores(text, keys, maximum)
        if label == "Side-by-side":
          parsed_scores["llm_judge_correctness"] = float(
              all(
                  parsed_scores["llm_judge_" + key] == 1.0
                  for key in (
                      "final_answer_correct",
                      "tool_usage_correct",
                      "sound_reasoning",
                  )
              )
          )
        scores.update(parsed_scores)
        result = _parse_json_from_text(text)
        feedback_parts.append(str(result.get("justification", text)))
      except Exception as exc:
        message = f"{label} LLM evaluation failed: {exc}"
        logger.warning("%s", message)
        feedback_parts.append(message)
    return scores, "\n".join(feedback_parts)

  async def _llm_judge_evaluate(
      self,
      trace: SessionTrace,
      task_description: str,
      expected_trajectory: Optional[list[dict[str, Any]]],
  ) -> tuple[dict[str, float], str]:
    """Compatibility entry point for the former private judge method."""
    return await self.llm_judge_evaluate(
        trace, task_description, expected_trajectory
    )

  async def _evaluate_custom_rubric(
      self,
      rubric: dict[str, Any],
      trace_text: str,
      final_response: Optional[str],
      golden_response: Optional[str] = None,
  ) -> tuple[Optional[float], str]:
    """Evaluate a custom rubric, distinguishing invalid output from zero."""
    try:
      from google import genai
      from google.genai import types

      prompt = rubric["prompt_template"].format(
          trace_text=trace_text,
          final_response=final_response or "No response.",
          golden_response=golden_response or "No golden response.",
      )
      prompt += (
          "\nReturn only a JSON object with a numeric "
          f"{rubric['score_key']!r} score from 0 to 10 and a justification."
      )
      client = genai.Client()
      response = await client.aio.models.generate_content(
          model=self.llm_judge_model,
          contents=prompt,
          config=types.GenerateContentConfig(
              temperature=0.1, max_output_tokens=2048
          ),
      )
      text = (response.text or "").strip()
      result = _parse_json_from_text(text)
      if not isinstance(result, dict) or rubric["score_key"] not in result:
        raise ValueError(f"Missing required rubric key: {rubric['score_key']}")
      raw = self._validated_score(
          result[rubric["score_key"]], rubric["score_key"], 10.0
      )
      score = raw / 10.0
      return score, str(result.get("justification", ""))
    except Exception as exc:
      logger.warning("Custom rubric %s failed: %s", rubric["name"], exc)
      return None, str(exc)


# Both import paths share the same classes, enums, and model instances.
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

  def __init__(self, evaluator: PerformanceEvaluator) -> None:
    """Initializes the replay runner.

    Args:
        evaluator: PerformanceEvaluator for trace retrieval.
    """
    self.evaluator = evaluator

  async def _get_trace(
      self,
      session_id: str,
      selector: Optional[TraceSelector],
  ) -> SessionTrace:
    """Resolve one replay input through the shared selector surface."""
    if selector is None:
      return await self.evaluator.get_session_trace(session_id)
    if type(selector) is not TraceSelector:
      raise TypeError("selector must be a TraceSelector.")
    if selector.session_id != session_id:
      raise ValueError("selector.session_id must match session_id.")
    return await self.evaluator.get_trace_by_selector(selector)

  async def replay_session(
      self,
      session_id: str,
      replay_mode: str = "full",
      step_callback: Optional[
          Callable[[TraceEvent, ReplayContext], None]
      ] = None,
      *,
      selector: Optional[TraceSelector] = None,
  ) -> ReplayContext:
    """Replays a recorded session step by step.

    Args:
        session_id: The session ID to replay.
        replay_mode: "full" for all events, "step" for pause at each step,
                     "tool_only" for only tool calls.
        step_callback: Optional callback invoked at each step.
        selector: Optional exact identity/scope selector for a reused session.

    Returns:
        ReplayContext with all injected responses.
    """
    trace = await self._get_trace(session_id, selector)

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
      *,
      selector_1: Optional[TraceSelector] = None,
      selector_2: Optional[TraceSelector] = None,
  ) -> dict[str, Any]:
    """Compares two session replays to identify differences.

    Args:
        session_id_1: First session ID.
        session_id_2: Second session ID.
        selector_1: Optional exact selector for the first reused session.
        selector_2: Optional exact selector for the second reused session.

    Returns:
        Dict with comparison results.
    """
    trace1 = await self._get_trace(session_id_1, selector_1)
    trace2 = await self._get_trace(session_id_2, selector_2)

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
