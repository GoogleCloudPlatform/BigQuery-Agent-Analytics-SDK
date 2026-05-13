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

"""Runnable MAKO demo agent + BQ AA plugin wiring.

This module exports:

* ``root_agent`` — an ADK ``Agent`` configured with five
  MAKO decision-flow tools and a system prompt that walks
  the agent through ``capture_context →
  propose_decision_point → evaluate_candidate (×3-5) →
  commit_outcome → complete_execution`` for each decision.
* ``bq_logging_plugin`` — a
  ``BigQueryAgentAnalyticsPlugin`` instance bound to the
  demo's ``(project, dataset, table)``. ``run_agent.py``
  attaches this plugin to ``InMemoryRunner``; the plugin
  writes plugin-shape rows into ``agent_events`` for every
  invocation, agent, LLM, tool, and HITL event.
* ``APP_NAME`` — the ADK app name used when constructing
  the runner.

**This is the event source of truth** — the BQ AA plugin's
``agent_events`` table, populated by running this agent.
``mako_artifacts.py`` generates TTL-derived snapshots
(ontology / binding / DDL / property graph); this module
generates the event stream by actually running an agent.
The trace rows the plugin emits are exactly what the
notebook's Beat 3 extractors consume.

Tools are intentionally lightweight: each one acknowledges
the agent's commitment with a synthetic MAKO entity ID and
echoes the relevant MAKO-declared data properties. The
demo's value is in the agent's reasoning trace (the
``LLM_RESPONSE`` rows that name alternatives + rationale)
plus the structured ``TOOL_*`` rows the plugin captures —
not in the tool internals.

Run via ``run_agent.py`` (the driver). Direct invocation of
``root_agent`` is fine too; the plugin is preconfigured for
the dataset named in ``DATASET_ID``.
"""

from __future__ import annotations

import hashlib
import os
from typing import Any

from dotenv import load_dotenv
from google.adk.agents import Agent
from google.adk.models import Gemini
from google.adk.plugins.bigquery_agent_analytics_plugin import BigQueryAgentAnalyticsPlugin
from google.adk.plugins.bigquery_agent_analytics_plugin import BigQueryLoggerConfig
import google.auth
from google.genai import types

# Load .env adjacent to this file if present (for local
# development); env vars set at the OS level take
# precedence.
_HERE = os.path.dirname(os.path.abspath(__file__))
_env_path = os.path.join(_HERE, ".env")
if os.path.exists(_env_path):
  load_dotenv(dotenv_path=_env_path)

_, _auth_project = google.auth.default()
PROJECT_ID = os.getenv("PROJECT_ID") or _auth_project
DATASET_ID = os.getenv("DATASET_ID", "migration_v5_demo")
DATASET_LOCATION = os.getenv("DATASET_LOCATION", "US")
TABLE_ID = os.getenv("TABLE_ID", "agent_events")
MODEL_ID = os.getenv("DEMO_AGENT_MODEL", "gemini-2.5-flash")
AGENT_LOCATION = os.getenv("DEMO_AGENT_LOCATION", "us-central1")

# google-adk + google-genai pick these env vars up at
# construction time. Set them so the runner uses Vertex AI
# with the right project + region.
os.environ["GOOGLE_CLOUD_PROJECT"] = PROJECT_ID or ""
os.environ["GOOGLE_CLOUD_LOCATION"] = AGENT_LOCATION
os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "True"

APP_NAME = "migration_v5_demo"


SYSTEM_PROMPT = """You are a MAKO decision agent for an
ads-monetization platform. For each user message, walk
through the MAKO decision flow ONCE end-to-end using the
five tools provided.

The flow, in order, per decision:

1. Call ``capture_context`` with the current audience size
   and remaining budget. This records a ``ContextSnapshot``.
2. Call ``propose_decision_point`` with a decision type
   (``AUDIENCE_SEGMENT`` | ``BID_VALUE`` | ``CREATIVE_VARIANT``
   | ``FREQUENCY_CAP``) and a reversibility value
   (``reversible`` | ``irreversible`` | ``compensable``).
   This records a ``DecisionPoint``.
3. Call ``evaluate_candidate`` THREE TO FIVE times with
   distinct candidate labels. Each call records a
   ``Candidate`` and an ``evaluatesCandidate`` edge.
4. Call ``commit_outcome`` with the ID of the winning
   candidate and a one-sentence rationale. This records a
   ``SelectionOutcome`` and the ``selectedCandidate`` edge.
5. Call ``complete_execution`` with the decision point's
   ID, the context snapshot ID returned by
   ``capture_context``, the outcome ID returned by
   ``commit_outcome``, and a business entity ID. All four
   arguments are required — the tool will fail without
   them. This records the ``DecisionExecution`` (the MAKO
   central hub) and wires the edges to ``AgentSession``,
   ``DecisionPoint``, ``ContextSnapshot``, and
   ``SelectionOutcome``.

Always enumerate the candidates and reasoning in your text
before calling each tool. The reasoning trace is what
downstream analytics consumes."""


# ------------------------------------------------------------------ #
# Tools — each commits one step of the MAKO decision flow            #
# ------------------------------------------------------------------ #


def _short_hash(*parts: Any) -> str:
  raw = "::".join(str(p) for p in parts).encode("utf-8")
  return hashlib.sha1(raw).hexdigest()[:10]


def capture_context(
    audience_size: int, budget_remaining_usd: float
) -> dict[str, Any]:
  """Record a ``ContextSnapshot`` for the upcoming decision.

  Args:
    audience_size: Estimated reachable audience.
    budget_remaining_usd: Budget remaining in USD.

  Returns:
    Dict with ``context_id`` and the captured payload.
    The agent uses ``context_id`` to thread the snapshot
    through ``complete_execution``.
  """
  context_id = "ctx-" + _short_hash(audience_size, budget_remaining_usd)
  return {
      "status": "ok",
      "context_id": context_id,
      # ContextSnapshot.snapshotPayload (MAKO-declared).
      "snapshot_payload": {
          "audience_size": audience_size,
          "budget_remaining_usd": budget_remaining_usd,
      },
  }


def propose_decision_point(
    decision_type: str, reversibility: str
) -> dict[str, Any]:
  """Record a ``DecisionPoint`` for the current step.

  Args:
    decision_type: One of ``AUDIENCE_SEGMENT`` |
      ``BID_VALUE`` | ``CREATIVE_VARIANT`` |
      ``FREQUENCY_CAP``.
    reversibility: ``reversible`` | ``irreversible`` |
      ``compensable`` (MAKO ``DecisionPoint.reversibility``).
  """
  decision_point_id = "dp-" + _short_hash(decision_type, reversibility)
  return {
      "status": "ok",
      "decision_point_id": decision_point_id,
      "decision_type": decision_type,
      # DecisionPoint.reversibility (MAKO-declared).
      "reversibility": reversibility,
  }


def evaluate_candidate(
    decision_point_id: str, candidate_label: str
) -> dict[str, Any]:
  """Record one ``Candidate`` evaluation for the current
  decision point.

  Args:
    decision_point_id: ID returned by
      ``propose_decision_point``.
    candidate_label: Human-readable candidate label
      (e.g. ``"Premium Subscribers"``,
      ``"$1.20 CPM"``).
  """
  candidate_id = "cand-" + _short_hash(decision_point_id, candidate_label)
  return {
      "status": "ok",
      "candidate_id": candidate_id,
      "decision_point_id": decision_point_id,
      "candidate_label": candidate_label,
  }


def commit_outcome(
    decision_point_id: str,
    selected_candidate_id: str,
    rationale: str,
) -> dict[str, Any]:
  """Record the ``SelectionOutcome`` for a decision point.

  Args:
    decision_point_id: ID returned by
      ``propose_decision_point``.
    selected_candidate_id: ID returned by the winning
      ``evaluate_candidate`` call.
    rationale: One-sentence justification.
  """
  outcome_id = "out-" + _short_hash(decision_point_id, selected_candidate_id)
  return {
      "status": "ok",
      "outcome_id": outcome_id,
      "decision_point_id": decision_point_id,
      "selected_candidate_id": selected_candidate_id,
      "rationale": rationale,
  }


def complete_execution(
    decision_point_id: str,
    context_id: str,
    outcome_id: str,
    business_entity_id: str,
) -> dict[str, Any]:
  """Record the ``DecisionExecution`` — MAKO's central hub
  that ties session + decision point + context + outcome
  together.

  Args:
    decision_point_id: ID from ``propose_decision_point``.
    context_id: ID from ``capture_context``.
    outcome_id: ID from ``commit_outcome``.
    business_entity_id: External reference, e.g. the
      campaign or audience the decision applies to (MAKO
      ``DecisionExecution.businessEntityId``).
  """
  execution_id = "exec-" + _short_hash(
      decision_point_id, context_id, outcome_id
  )
  return {
      "status": "ok",
      "execution_id": execution_id,
      "decision_point_id": decision_point_id,
      "context_id": context_id,
      "outcome_id": outcome_id,
      # DecisionExecution.businessEntityId (MAKO-declared).
      "business_entity_id": business_entity_id,
      # ``latency_ms`` would be measured by the agent's
      # runtime; the tool returns a synthetic value so the
      # plugin trace carries a realistic
      # ``DecisionExecution.latencyMs``.
      "latency_ms": 42,
  }


MAKO_TOOLS = (
    capture_context,
    propose_decision_point,
    evaluate_candidate,
    commit_outcome,
    complete_execution,
)


# ------------------------------------------------------------------ #
# Agent + plugin wiring                                               #
# ------------------------------------------------------------------ #


root_agent = Agent(
    name="mako_decision_agent",
    model=Gemini(
        model=MODEL_ID,
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    description=(
        "MAKO decision agent. Walks through the MAKO "
        "decision flow (context → decision point → "
        "candidate evaluation → outcome → execution) for "
        "each user-supplied decision request, enumerating "
        "alternatives and rationale at every step."
    ),
    instruction=SYSTEM_PROMPT,
    tools=list(MAKO_TOOLS),
)


_bq_config = BigQueryLoggerConfig(
    enabled=True,
    max_content_length=500 * 1024,
    batch_size=1,
    shutdown_timeout=15.0,
)
bq_logging_plugin = BigQueryAgentAnalyticsPlugin(
    project_id=PROJECT_ID,
    dataset_id=DATASET_ID,
    table_id=TABLE_ID,
    location=DATASET_LOCATION,
    config=_bq_config,
)
