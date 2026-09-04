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

"""Agent Insights for BigQuery Agent Analytics SDK.

Generates comprehensive insights reports from agent conversation traces
stored in BigQuery. Inspired by the multi-stage insights pipeline
architecture, this module provides:

1. **Session filtering** - Exclude sub-sessions, short sessions.
2. **Metadata extraction** - Tokens, tool counts, error rates.
3. **Facet extraction** - LLM-based structured analysis per session.
4. **Aggregation** - Distributions and top-N across sessions.
5. **Multi-prompt analysis** - 7 specialized analysis prompts.
6. **Report generation** - Structured insights report.

Example usage::

    from bigquery_agent_analytics import Client

    client = Client(project_id="my-project", dataset_id="analytics")
    report = client.insights(max_sessions=50)
    print(report.summary())
    print(report.executive_summary)
"""

from __future__ import annotations

import asyncio
from collections import Counter
from dataclasses import dataclass
from dataclasses import field as dc_field
from datetime import datetime
from datetime import timezone
import json
import logging
from typing import Any, Optional

from pydantic import BaseModel
from pydantic import Field

from .evaluators import _parse_json_from_text

logger = logging.getLogger("bigquery_agent_analytics." + __name__)


# ------------------------------------------------------------------ #
# Facet Schema                                                         #
# ------------------------------------------------------------------ #

# Goal categories for agent interactions
GOAL_CATEGORIES = [
    "question_answering",
    "data_retrieval",
    "task_automation",
    "code_generation",
    "content_creation",
    "troubleshooting",
    "analysis",
    "planning",
    "summarization",
    "translation",
    "search",
    "monitoring",
    "configuration",
    "other",
]

# Outcome levels
OUTCOMES = [
    "success",
    "partial_success",
    "failure",
    "abandoned",
    "unclear",
]

# User satisfaction levels
SATISFACTION_LEVELS = [
    "very_satisfied",
    "satisfied",
    "neutral",
    "dissatisfied",
    "very_dissatisfied",
    "unknown",
]

# Friction types that can occur during agent interactions
FRICTION_TYPES = [
    "tool_error",
    "slow_response",
    "wrong_answer",
    "repeated_questions",
    "context_lost",
    "hallucination",
    "unclear_instructions",
    "missing_capability",
    "auth_failure",
    "timeout",
    "format_error",
    "other",
]

# Session types
SESSION_TYPES = [
    "question_answer",
    "task_execution",
    "troubleshooting",
    "exploration",
    "multi_turn_dialog",
]


# ------------------------------------------------------------------ #
# Data Models                                                          #
# ------------------------------------------------------------------ #


class SessionFacet(BaseModel):
  """Structured facets extracted from a single session.

  Each session is analyzed by an LLM to produce structured
  metadata about goals, outcomes, satisfaction, and friction.
  """

  session_id: str = Field(description="Session identifier.")
  goal_categories: list[str] = Field(
      default_factory=list,
      description="Categorized goals for this session.",
  )
  outcome: str = Field(
      default="unclear",
      description="Session outcome level.",
  )
  satisfaction: str = Field(
      default="unknown",
      description="Inferred user satisfaction.",
  )
  friction_types: list[str] = Field(
      default_factory=list,
      description="Types of friction encountered.",
  )
  session_type: str = Field(
      default="question_answer",
      description="Type of interaction.",
  )
  agent_effectiveness: float = Field(
      default=5.0,
      description="Agent effectiveness score (1-10).",
  )
  primary_success: bool = Field(
      default=False,
      description="Whether the primary goal was achieved.",
  )
  key_topics: list[str] = Field(
      default_factory=list,
      description="Key topics discussed in this session.",
  )
  summary: str = Field(
      default="",
      description="Brief session summary.",
  )


class SessionMetadata(BaseModel):
  """Quantitative metadata extracted from a session."""

  session_id: str = Field(description="Session identifier.")
  event_count: int = Field(default=0)
  tool_calls: int = Field(default=0)
  tool_errors: int = Field(default=0)
  llm_calls: int = Field(default=0)
  turn_count: int = Field(default=0)
  total_latency_ms: float = Field(default=0.0)
  avg_latency_ms: float = Field(default=0.0)
  agents_used: list[str] = Field(default_factory=list)
  tools_used: list[str] = Field(default_factory=list)
  has_error: bool = Field(default=False)
  hitl_events: int = Field(default=0)
  state_changes: int = Field(default=0)
  start_time: Optional[datetime] = None
  end_time: Optional[datetime] = None


class AggregatedInsights(BaseModel):
  """Aggregated facet distributions across all sessions."""

  total_sessions: int = Field(default=0)
  goal_distribution: dict[str, int] = Field(
      default_factory=dict,
      description="Count per goal category.",
  )
  outcome_distribution: dict[str, int] = Field(
      default_factory=dict,
      description="Count per outcome level.",
  )
  satisfaction_distribution: dict[str, int] = Field(
      default_factory=dict,
      description="Count per satisfaction level.",
  )
  friction_distribution: dict[str, int] = Field(
      default_factory=dict,
      description="Count per friction type.",
  )
  session_type_distribution: dict[str, int] = Field(
      default_factory=dict,
      description="Count per session type.",
  )
  avg_effectiveness: float = Field(default=0.0)
  success_rate: float = Field(default=0.0)
  top_topics: list[tuple[str, int]] = Field(
      default_factory=list,
      description="Most common topics with counts.",
  )
  top_tools: list[tuple[str, int]] = Field(
      default_factory=list,
      description="Most used tools with counts.",
  )
  top_agents: list[tuple[str, int]] = Field(
      default_factory=list,
      description="Most active agents with counts.",
  )
  avg_latency_ms: float = Field(default=0.0)
  avg_turns: float = Field(default=0.0)
  error_rate: float = Field(default=0.0)


class AnalysisSection(BaseModel):
  """A single analysis section from the multi-prompt pipeline."""

  title: str = Field(description="Section title.")
  content: str = Field(
      default="",
      description="Analysis content (markdown).",
  )


class InsightsConfig(BaseModel):
  """Configuration for the insights pipeline."""

  max_sessions: int = Field(
      default=50,
      description="Maximum number of sessions to analyze.",
  )
  min_events_per_session: int = Field(
      default=3,
      description="Skip sessions with fewer events.",
  )
  min_turns_per_session: int = Field(
      default=1,
      description="Skip sessions with fewer user turns.",
  )
  include_sub_sessions: bool = Field(
      default=False,
      description="Whether to include sub-sessions.",
  )
  analysis_prompts: Optional[list[str]] = Field(
      default=None,
      description="Custom analysis prompt names to run.",
  )


class InsightsReport(BaseModel):
  """Complete insights report for agent interactions.

  Contains per-session facets, aggregated distributions,
  specialized analysis sections, and an executive summary.
  """

  created_at: datetime = Field(
      default_factory=lambda: datetime.now(timezone.utc),
  )
  config: InsightsConfig = Field(
      default_factory=InsightsConfig,
  )
  session_facets: list[SessionFacet] = Field(
      default_factory=list,
  )
  session_metadata: list[SessionMetadata] = Field(
      default_factory=list,
  )
  aggregated: AggregatedInsights = Field(
      default_factory=AggregatedInsights,
  )
  analysis_sections: list[AnalysisSection] = Field(
      default_factory=list,
  )
  executive_summary: str = Field(
      default="",
      description="At-a-glance executive summary.",
  )

  @property
  def total_sessions(self) -> int:
    return self.aggregated.total_sessions

  @property
  def success_rate(self) -> float:
    return self.aggregated.success_rate

  def summary(self) -> str:
    """Returns a human-readable summary of insights."""
    agg = self.aggregated
    lines = [
        "Agent Insights Report",
        f"  Generated: {self.created_at:%Y-%m-%d %H:%M UTC}",
        f"  Sessions analyzed: {agg.total_sessions}",
        f"  Success rate: {agg.success_rate:.0%}",
        f"  Avg effectiveness: {agg.avg_effectiveness:.1f}/10",
        f"  Avg latency: {agg.avg_latency_ms:.0f}ms",
        f"  Avg turns: {agg.avg_turns:.1f}",
        f"  Error rate: {agg.error_rate:.1%}",
        "",
    ]

    if agg.goal_distribution:
      lines.append("  Top Goals:")
      sorted_goals = sorted(
          agg.goal_distribution.items(),
          key=lambda x: x[1],
          reverse=True,
      )
      for goal, count in sorted_goals[:5]:
        lines.append(f"    {goal}: {count}")

    if agg.outcome_distribution:
      lines.append("  Outcomes:")
      for outcome, count in sorted(
          agg.outcome_distribution.items(),
          key=lambda x: x[1],
          reverse=True,
      ):
        lines.append(f"    {outcome}: {count}")

    if agg.friction_distribution:
      lines.append("  Top Friction Points:")
      sorted_f = sorted(
          agg.friction_distribution.items(),
          key=lambda x: x[1],
          reverse=True,
      )
      for friction, count in sorted_f[:5]:
        lines.append(f"    {friction}: {count}")

    if self.analysis_sections:
      lines.append("")
      lines.append("  Analysis Sections:")
      for section in self.analysis_sections:
        lines.append(f"    - {section.title}")

    return "\n".join(lines)

  def get_section(self, title: str) -> Optional[AnalysisSection]:
    """Returns an analysis section by title."""
    for s in self.analysis_sections:
      if s.title.lower() == title.lower():
        return s
    return None


# ------------------------------------------------------------------ #
# SQL Templates                                                        #
# ------------------------------------------------------------------ #

_SESSION_METADATA_QUERY = """\
SELECT
  session_id,
  COUNT(*) AS event_count,
  COUNTIF(event_type = 'TOOL_STARTING') AS tool_calls,
  COUNTIF(event_type = 'TOOL_ERROR') AS tool_errors,
  COUNTIF(event_type = 'LLM_REQUEST') AS llm_calls,
  COUNTIF(
    event_type = 'USER_MESSAGE_RECEIVED'
  ) AS turn_count,
  TIMESTAMP_DIFF(
    MAX(timestamp), MIN(timestamp), MILLISECOND
  ) AS total_latency_ms,
  AVG(
    CAST(
      JSON_VALUE(latency_ms, '$.total_ms')
      AS FLOAT64
    )
  ) AS avg_latency_ms,
  ARRAY_AGG(
    DISTINCT agent IGNORE NULLS
  ) AS agents_used,
  ARRAY_AGG(
    DISTINCT JSON_VALUE(content, '$.tool')
    IGNORE NULLS
  ) AS tools_used,
  COUNTIF(
    ENDS_WITH(event_type, '_ERROR')
    OR error_message IS NOT NULL
    OR status = 'ERROR'
  ) > 0 AS has_error,
  COUNTIF(event_type LIKE 'HITL_%') AS hitl_events,
  COUNTIF(event_type = 'STATE_DELTA') AS state_changes,
  MIN(timestamp) AS start_time,
  MAX(timestamp) AS end_time
FROM `{project}.{dataset}.{table}`
WHERE {where}
GROUP BY session_id
HAVING event_count >= @min_events
  AND turn_count >= @min_turns
ORDER BY start_time DESC
LIMIT @max_sessions
"""

_SESSION_TRANSCRIPT_QUERY = """\
SELECT
  session_id,
  STRING_AGG(
    CONCAT(
      event_type,
      COALESCE(CONCAT(' [', agent, ']'), ''),
      ': ',
      COALESCE(
        JSON_VALUE(content, '$.text_summary'),
        JSON_VALUE(content, '$.response'),
        JSON_VALUE(content, '$.tool'),
        CAST(status AS STRING),
        ''
      )
    ),
    '\\n' ORDER BY timestamp
  ) AS transcript,
  ARRAY_AGG(
    JSON_VALUE(content, '$.response')
    IGNORE NULLS
    ORDER BY timestamp DESC
    LIMIT 1
  )[SAFE_OFFSET(0)] AS final_response
FROM `{project}.{dataset}.{table}`
WHERE session_id IN UNNEST(@session_ids)
GROUP BY session_id
"""

_AI_GENERATE_FACET_EXTRACTION_QUERY = """\
WITH transcripts AS (
  SELECT
    session_id,
    STRING_AGG(
      CONCAT(
        event_type,
        COALESCE(CONCAT(' [', agent, ']'), ''),
        ': ',
        COALESCE(
          JSON_VALUE(content, '$.text_summary'),
          JSON_VALUE(content, '$.response'),
          JSON_VALUE(content, '$.tool'),
          ''
        )
      ),
      '\\n' ORDER BY timestamp
    ) AS transcript
  FROM `{project}.{dataset}.{table}`
  WHERE session_id IN UNNEST(@session_ids)
  GROUP BY session_id
)
SELECT
  session_id,
  transcript,
  result.*
FROM transcripts,
AI.GENERATE(
  prompt => CONCAT(@facet_prompt, '\\n\\nTranscript:\\n', transcript),
  endpoint => '{endpoint}',
  model_params => JSON '{{"temperature": 0.1, "max_output_tokens": 1024}}',
  output_schema => CONCAT(
    'goal_categories ARRAY<STRING>, ',
    'outcome STRING, ',
    'satisfaction STRING, ',
    'friction_types ARRAY<STRING>, ',
    'session_type STRING, ',
    'agent_effectiveness INT64, ',
    'primary_success BOOL, ',
    'key_topics ARRAY<STRING>, ',
    'summary STRING'
  )
) AS result
"""

_AI_GENERATE_ANALYSIS_QUERY = """\
SELECT
  result.*
FROM AI.GENERATE(
  prompt => @analysis_prompt,
  endpoint => '{endpoint}',
  model_params => JSON '{{"temperature": 0.3, "max_output_tokens": 2048}}'
) AS result
"""

# Legacy templates kept for backward compatibility with pre-created
# BQ ML models.
_LEGACY_FACET_EXTRACTION_QUERY = """\
WITH transcripts AS (
  SELECT
    session_id,
    STRING_AGG(
      CONCAT(
        event_type,
        COALESCE(CONCAT(' [', agent, ']'), ''),
        ': ',
        COALESCE(
          JSON_VALUE(content, '$.text_summary'),
          JSON_VALUE(content, '$.response'),
          JSON_VALUE(content, '$.tool'),
          ''
        )
      ),
      '\\n' ORDER BY timestamp
    ) AS transcript
  FROM `{project}.{dataset}.{table}`
  WHERE session_id IN UNNEST(@session_ids)
  GROUP BY session_id
)
SELECT
  session_id,
  transcript,
  ML.GENERATE_TEXT(
    MODEL `{model}`,
    STRUCT(
      CONCAT(@facet_prompt, '\\n\\nTranscript:\\n', transcript)
      AS prompt
    ),
    STRUCT(0.1 AS temperature, 1024 AS max_output_tokens)
  ).ml_generate_text_result AS facets_json
FROM transcripts
"""

_LEGACY_ANALYSIS_QUERY = """\
SELECT
  ML.GENERATE_TEXT(
    MODEL `{model}`,
    STRUCT(@analysis_prompt AS prompt),
    STRUCT(0.3 AS temperature, 2048 AS max_output_tokens)
  ).ml_generate_text_result AS analysis
"""

# Keep backward-compatible aliases.
_FACET_EXTRACTION_QUERY = _LEGACY_FACET_EXTRACTION_QUERY
_ANALYSIS_QUERY = _LEGACY_ANALYSIS_QUERY


# ------------------------------------------------------------------ #
# Prompts                                                              #
# ------------------------------------------------------------------ #

_FACET_EXTRACTION_PROMPT = """\
Analyze this agent conversation transcript and extract structured \
facets. Respond with ONLY a valid JSON object.

Required fields:
- "goal_categories": list of categories from [{goal_cats}]
- "outcome": one of [{outcomes}]
- "satisfaction": one of [{satisfaction}]
- "friction_types": list from [{friction_types}] (empty if none)
- "session_type": one of [{session_types}]
- "agent_effectiveness": integer 1-10
- "primary_success": boolean
- "key_topics": list of 1-3 short topic strings
- "summary": one sentence summary"""

ANALYSIS_PROMPTS = {
    "task_areas": {
        "title": "Task Areas",
        "prompt": """\
You are analyzing aggregated data from {total} agent conversation \
sessions. Based on the following data, write a concise analysis of \
what tasks users are bringing to the agent.

Goal distribution: {goal_dist}
Top topics: {top_topics}
Session types: {session_type_dist}

Write 3-5 bullet points about the key task areas and patterns. \
Be specific about what users need help with.""",
    },
    "interaction_patterns": {
        "title": "Interaction Patterns",
        "prompt": """\
Analyze how users interact with the agent based on these metrics:

Sessions: {total}
Avg turns per session: {avg_turns:.1f}
Avg latency: {avg_latency:.0f}ms
Session type distribution: {session_type_dist}
Top tools used: {top_tools}
Top agents: {top_agents}

Write 3-5 bullet points about interaction patterns, including \
session length, tool usage, and multi-agent patterns.""",
    },
    "what_works_well": {
        "title": "What Works Well",
        "prompt": """\
Analyze what the agent does well based on these metrics:

Success rate: {success_rate:.0%}
Avg effectiveness: {avg_effectiveness:.1f}/10
Outcome distribution: {outcome_dist}
Satisfaction distribution: {satisfaction_dist}
Top goals with high success: {successful_goals}

Write 3-5 bullet points highlighting agent strengths.""",
    },
    "friction_analysis": {
        "title": "Friction Analysis",
        "prompt": """\
Analyze friction points users encounter with the agent:

Error rate: {error_rate:.1%}
Friction distribution: {friction_dist}
Outcome distribution: {outcome_dist}
Failed session topics: {failed_topics}
Avg latency: {avg_latency:.0f}ms

Write 3-5 bullet points about key friction areas and their \
likely root causes.""",
    },
    "tool_usage": {
        "title": "Tool Usage Patterns",
        "prompt": """\
Analyze tool usage patterns across agent sessions:

Total sessions: {total}
Avg tool calls per session: {avg_tool_calls:.1f}
Tool error rate: {tool_error_rate:.1%}
Top tools: {top_tools}
Tools in failed sessions: {error_tools}

Write 3-5 bullet points about tool utilization, reliability, \
and optimization opportunities.""",
    },
    "suggestions": {
        "title": "Improvement Suggestions",
        "prompt": """\
Based on the full agent analytics data, suggest improvements:

Success rate: {success_rate:.0%}
Top friction points: {friction_dist}
Failed session patterns: {failed_topics}
Underused tools: {underused_tools}
Avg effectiveness: {avg_effectiveness:.1f}/10
Goal categories with low success: {low_success_goals}

Write 3-5 specific, actionable improvement suggestions \
prioritized by potential impact.""",
    },
    "trends": {
        "title": "Trends & Anomalies",
        "prompt": """\
Identify trends and anomalies in the agent analytics data:

Total sessions: {total}
Time range: {time_range}
Goal distribution: {goal_dist}
Outcome over time: {outcome_dist}
Error rate: {error_rate:.1%}
Top topics: {top_topics}

Write 3-5 bullet points about emerging trends, seasonal \
patterns, or anomalies that warrant attention.""",
    },
}

_EXECUTIVE_SUMMARY_PROMPT = """\
You are writing an executive summary for an agent analytics \
insights report. Based on the following analysis sections, \
write a concise "At a Glance" summary in 4-6 sentences.

{sections_text}

Metrics:
- Sessions analyzed: {total}
- Success rate: {success_rate:.0%}
- Avg effectiveness: {avg_effectiveness:.1f}/10
- Error rate: {error_rate:.1%}
- Avg latency: {avg_latency:.0f}ms

Write a brief, executive-level summary highlighting the \
most important findings and recommended actions."""


# ------------------------------------------------------------------ #
# Core Functions                                                       #
# ------------------------------------------------------------------ #


def aggregate_facets(
    facets: list[SessionFacet],
    metadata: list[SessionMetadata],
) -> AggregatedInsights:
  """Aggregates per-session facets into distributions.

  Args:
      facets: Per-session facet data.
      metadata: Per-session quantitative metadata.

  Returns:
      AggregatedInsights with distributions and averages.
  """
  total = len(facets)
  if total == 0:
    return AggregatedInsights()

  # Goal distribution
  goal_counter: Counter = Counter()
  for f in facets:
    for g in f.goal_categories:
      goal_counter[g] += 1

  # Outcome distribution
  outcome_counter = Counter(f.outcome for f in facets)

  # Satisfaction distribution
  satisfaction_counter = Counter(f.satisfaction for f in facets)

  # Friction distribution
  friction_counter: Counter = Counter()
  for f in facets:
    for ft in f.friction_types:
      friction_counter[ft] += 1

  # Session type distribution
  session_type_counter = Counter(f.session_type for f in facets)

  # Effectiveness and success
  effectiveness_scores = [f.agent_effectiveness for f in facets]
  avg_eff = (
      sum(effectiveness_scores) / len(effectiveness_scores)
      if effectiveness_scores
      else 0.0
  )
  success_count = sum(1 for f in facets if f.primary_success)
  success_rate = success_count / total if total else 0.0

  # Topics
  topic_counter: Counter = Counter()
  for f in facets:
    for t in f.key_topics:
      topic_counter[t] += 1

  # Tools and agents from metadata
  tool_counter: Counter = Counter()
  agent_counter: Counter = Counter()
  total_latency = 0.0
  total_turns = 0
  error_count = 0

  for m in metadata:
    for t in m.tools_used:
      tool_counter[t] += 1
    for a in m.agents_used:
      agent_counter[a] += 1
    total_latency += m.avg_latency_ms or 0.0
    total_turns += m.turn_count
    if m.has_error:
      error_count += 1

  meta_count = len(metadata) or 1
  avg_latency = total_latency / meta_count
  avg_turns = total_turns / meta_count
  error_rate = error_count / meta_count

  return AggregatedInsights(
      total_sessions=total,
      goal_distribution=dict(goal_counter),
      outcome_distribution=dict(outcome_counter),
      satisfaction_distribution=dict(satisfaction_counter),
      friction_distribution=dict(friction_counter),
      session_type_distribution=dict(session_type_counter),
      avg_effectiveness=avg_eff,
      success_rate=success_rate,
      top_topics=topic_counter.most_common(20),
      top_tools=tool_counter.most_common(20),
      top_agents=agent_counter.most_common(10),
      avg_latency_ms=avg_latency,
      avg_turns=avg_turns,
      error_rate=error_rate,
  )


def parse_facet_response(
    session_id: str,
    text: str,
) -> SessionFacet:
  """Parses an LLM facet extraction response.

  Args:
      session_id: The session ID.
      text: Raw LLM response text.

  Returns:
      SessionFacet with parsed fields, falling back to
      defaults for any missing fields.
  """
  parsed = _parse_json_from_text(text)
  if not parsed:
    return SessionFacet(session_id=session_id)

  return SessionFacet(
      session_id=session_id,
      goal_categories=[
          g for g in parsed.get("goal_categories", []) if g in GOAL_CATEGORIES
      ]
      or ["other"],
      outcome=(
          parsed.get("outcome", "unclear")
          if parsed.get("outcome") in OUTCOMES
          else "unclear"
      ),
      satisfaction=(
          parsed.get("satisfaction", "unknown")
          if parsed.get("satisfaction") in SATISFACTION_LEVELS
          else "unknown"
      ),
      friction_types=[
          f for f in parsed.get("friction_types", []) if f in FRICTION_TYPES
      ],
      session_type=(
          parsed.get("session_type", "question_answer")
          if parsed.get("session_type") in SESSION_TYPES
          else "question_answer"
      ),
      agent_effectiveness=max(
          1.0,
          min(10.0, float(parsed.get("agent_effectiveness", 5))),
      ),
      primary_success=bool(parsed.get("primary_success", False)),
      key_topics=parsed.get("key_topics", [])[:5],
      summary=str(parsed.get("summary", ""))[:200],
  )


def parse_facet_from_ai_generate_row(
    session_id: str,
    row: dict[str, Any],
) -> SessionFacet:
  """Parses a typed AI.GENERATE result row into a SessionFacet.

  Unlike ``parse_facet_response`` which parses free-form JSON text,
  this reads typed columns produced by ``output_schema`` directly.

  Args:
      session_id: The session ID.
      row: Dict with typed column values from AI.GENERATE.

  Returns:
      SessionFacet with validated fields, falling back to
      defaults for any missing or invalid values.
  """
  raw_goals = row.get("goal_categories") or []
  goals = [g for g in raw_goals if g in GOAL_CATEGORIES] or ["other"]

  raw_outcome = row.get("outcome")
  outcome = raw_outcome if raw_outcome in OUTCOMES else "unclear"

  raw_sat = row.get("satisfaction")
  satisfaction = raw_sat if raw_sat in SATISFACTION_LEVELS else "unknown"

  raw_friction = row.get("friction_types") or []
  friction = [f for f in raw_friction if f in FRICTION_TYPES]

  raw_stype = row.get("session_type")
  session_type = raw_stype if raw_stype in SESSION_TYPES else "question_answer"

  raw_eff = row.get("agent_effectiveness")
  try:
    effectiveness = max(1.0, min(10.0, float(raw_eff)))
  except (TypeError, ValueError):
    effectiveness = 5.0

  primary_success = bool(row.get("primary_success", False))

  raw_topics = row.get("key_topics") or []
  key_topics = list(raw_topics)[:5]

  raw_summary = row.get("summary") or ""
  summary = str(raw_summary)[:200]

  return SessionFacet(
      session_id=session_id,
      goal_categories=goals,
      outcome=outcome,
      satisfaction=satisfaction,
      friction_types=friction,
      session_type=session_type,
      agent_effectiveness=effectiveness,
      primary_success=primary_success,
      key_topics=key_topics,
      summary=summary,
  )


def build_facet_prompt() -> str:
  """Builds the facet extraction prompt with schema."""
  return _FACET_EXTRACTION_PROMPT.format(
      goal_cats=", ".join(GOAL_CATEGORIES),
      outcomes=", ".join(OUTCOMES),
      satisfaction=", ".join(SATISFACTION_LEVELS),
      friction_types=", ".join(FRICTION_TYPES),
      session_types=", ".join(SESSION_TYPES),
  )


def build_analysis_context(
    agg: AggregatedInsights,
    facets: list[SessionFacet],
    metadata: list[SessionMetadata],
) -> dict[str, Any]:
  """Builds the context dict for analysis prompts.

  Args:
      agg: Aggregated insights data.
      facets: Per-session facets.
      metadata: Per-session metadata.

  Returns:
      Dict with all template variables for analysis prompts.
  """
  # Successful goals
  success_goals: Counter = Counter()
  fail_goals: Counter = Counter()
  failed_topics: Counter = Counter()
  for f in facets:
    if f.primary_success:
      for g in f.goal_categories:
        success_goals[g] += 1
    else:
      for g in f.goal_categories:
        fail_goals[g] += 1
      for t in f.key_topics:
        failed_topics[t] += 1

  # Tool stats
  total_tool_calls = sum(m.tool_calls for m in metadata)
  total_tool_errors = sum(m.tool_errors for m in metadata)
  avg_tool_calls = total_tool_calls / len(metadata) if metadata else 0
  tool_error_rate = (
      total_tool_errors / total_tool_calls if total_tool_calls > 0 else 0.0
  )

  # Error tools
  error_tools: Counter = Counter()
  for m in metadata:
    if m.has_error:
      for t in m.tools_used:
        error_tools[t] += 1

  # Underused tools
  all_tools = Counter()
  for m in metadata:
    for t in m.tools_used:
      all_tools[t] += 1
  underused = [
      t for t, c in all_tools.most_common() if c <= max(1, len(metadata) * 0.05)
  ]

  # Low success goals
  low_success = []
  for g in set(list(success_goals) + list(fail_goals)):
    s = success_goals.get(g, 0)
    f = fail_goals.get(g, 0)
    total = s + f
    if total > 0 and s / total < 0.5:
      low_success.append(f"{g} ({s}/{total})")

  # Time range
  times = [m.start_time for m in metadata if m.start_time]
  if times:
    time_range = f"{min(times):%Y-%m-%d} to {max(times):%Y-%m-%d}"
  else:
    time_range = "unknown"

  return {
      "total": agg.total_sessions,
      "goal_dist": dict(
          sorted(
              agg.goal_distribution.items(),
              key=lambda x: x[1],
              reverse=True,
          )
      ),
      "outcome_dist": dict(agg.outcome_distribution),
      "satisfaction_dist": dict(agg.satisfaction_distribution),
      "friction_dist": dict(
          sorted(
              agg.friction_distribution.items(),
              key=lambda x: x[1],
              reverse=True,
          )
      ),
      "session_type_dist": dict(agg.session_type_distribution),
      "top_topics": agg.top_topics[:10],
      "top_tools": agg.top_tools[:10],
      "top_agents": agg.top_agents[:5],
      "success_rate": agg.success_rate,
      "avg_effectiveness": agg.avg_effectiveness,
      "avg_latency": agg.avg_latency_ms,
      "avg_turns": agg.avg_turns,
      "error_rate": agg.error_rate,
      "successful_goals": success_goals.most_common(5),
      "failed_topics": failed_topics.most_common(10),
      "avg_tool_calls": avg_tool_calls,
      "tool_error_rate": tool_error_rate,
      "error_tools": error_tools.most_common(5),
      "underused_tools": underused[:5],
      "low_success_goals": low_success[:5],
      "time_range": time_range,
  }


async def extract_facets_via_api(
    transcripts: dict[str, str],
    model: str = "gemini-2.5-flash",
) -> list[SessionFacet]:
  """Extracts facets using the Gemini API (fallback).

  Args:
      transcripts: Mapping of session_id to transcript text.
      model: Model to use for extraction.

  Returns:
      List of SessionFacet objects.
  """
  prompt_template = build_facet_prompt()
  facets = []

  try:
    from google import genai
    from google.genai import types

    client = genai.Client()

    for sid, transcript in transcripts.items():
      text = transcript
      if len(text) > 25000:
        text = text[:25000] + "\n... [truncated]"

      full_prompt = prompt_template + "\n\nTranscript:\n" + text

      try:
        response = await client.aio.models.generate_content(
            model=model,
            contents=full_prompt,
            config=types.GenerateContentConfig(
                temperature=0.1,
                max_output_tokens=1024,
            ),
        )
        facet = parse_facet_response(sid, response.text.strip())
        facets.append(facet)
      except Exception as e:
        logger.warning("Facet extraction failed for %s: %s", sid, e)
        facets.append(SessionFacet(session_id=sid))

  except ImportError:
    logger.warning("google-genai not installed, returning empty facets.")
    for sid in transcripts:
      facets.append(SessionFacet(session_id=sid))

  return facets


async def run_analysis_prompt(
    prompt_name: str,
    context: dict[str, Any],
    model: str = "gemini-2.5-flash",
) -> AnalysisSection:
  """Runs a single analysis prompt.

  Args:
      prompt_name: Key from ANALYSIS_PROMPTS.
      context: Template variables dict.
      model: Model to use.

  Returns:
      AnalysisSection with the analysis result.
  """
  if prompt_name not in ANALYSIS_PROMPTS:
    return AnalysisSection(
        title=prompt_name,
        content=f"Unknown analysis prompt: {prompt_name}",
    )

  prompt_config = ANALYSIS_PROMPTS[prompt_name]
  title = prompt_config["title"]

  try:
    prompt = prompt_config["prompt"].format(**context)
  except KeyError as e:
    logger.warning(
        "Missing context key for %s: %s",
        prompt_name,
        e,
    )
    return AnalysisSection(title=title, content="")

  try:
    from google import genai
    from google.genai import types

    client = genai.Client()
    response = await client.aio.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(
            temperature=0.3,
            max_output_tokens=2048,
        ),
    )
    return AnalysisSection(
        title=title,
        content=response.text.strip(),
    )

  except ImportError:
    logger.warning("google-genai not installed.")
    return AnalysisSection(
        title=title,
        content="Analysis unavailable (genai not installed).",
    )
  except Exception as e:
    logger.warning("Analysis prompt %s failed: %s", title, e)
    return AnalysisSection(
        title=title,
        content=f"Analysis failed: {e}",
    )


async def generate_executive_summary(
    report: InsightsReport,
    model: str = "gemini-2.5-flash",
) -> str:
  """Generates an executive summary from analysis sections.

  Args:
      report: The insights report with analysis sections.
      model: Model to use.

  Returns:
      Executive summary string.
  """
  sections_text = "\n\n".join(
      f"## {s.title}\n{s.content}" for s in report.analysis_sections
  )
  agg = report.aggregated

  prompt = _EXECUTIVE_SUMMARY_PROMPT.format(
      sections_text=sections_text,
      total=agg.total_sessions,
      success_rate=agg.success_rate,
      avg_effectiveness=agg.avg_effectiveness,
      error_rate=agg.error_rate,
      avg_latency=agg.avg_latency_ms,
  )

  try:
    from google import genai
    from google.genai import types

    client = genai.Client()
    response = await client.aio.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(
            temperature=0.3,
            max_output_tokens=1024,
        ),
    )
    return response.text.strip()

  except ImportError:
    return "Executive summary unavailable (genai not installed)."
  except Exception as e:
    logger.warning("Executive summary failed: %s", e)
    return f"Executive summary generation failed: {e}"
