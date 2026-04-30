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

"""Seed plugin-fidelity traces for one media-planner agent session.

In production these rows are written by the BigQuery Agent Analytics
Plugin running inside the agent. For the demo we INSERT realistic
plugin-shaped rows so the SDK extraction pipeline
(``mgr.build_context_graph(use_ai_generate=True, ...)``) has data to
work on.

The session captures a full Nike Summer Run 2026 media plan:

  * 1 invocation, 5 decision points
      - audience_selection
      - budget_allocation
      - creative_selection
      - channel_strategy
      - scheduling
  * 22 spans, including INVOCATION_STARTING/COMPLETED bookends,
    LLM_REQUEST/LLM_RESPONSE pairs (with token counts and
    time_to_first_token_ms), TOOL_STARTING/TOOL_COMPLETED pairs
    linked by tool_call_id, and HITL_CONFIRMATION_REQUEST +
    _COMPLETED final approval.
  * Each LLM_RESPONSE that carries a decision enumerates 3
    candidates (one SELECTED, two DROPPED with rationale). 15
    candidates total so AI.GENERATE has a rich extraction surface.

Plugin-shape conventions matched here (cross-checked against
``trace.Span.from_bigquery_row`` and the EventType enum):

  * ``content`` is JSON-encoded; per-event-type keys follow plugin
    convention (``tool``/``args``/``result``/``tool_call_id`` for
    tool events; ``messages`` and ``text_summary`` for LLM events).
  * ``attributes`` carries model name, temperature, and token counts
    on LLM events.
  * ``latency_ms`` is ``{total_ms, time_to_first_token_ms}``.
  * Tool calls are linked through ``content.tool_call_id`` between
    TOOL_STARTING and TOOL_COMPLETED.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from typing import Any

from dotenv import load_dotenv
from google.cloud import bigquery

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_env_path = os.path.join(_SCRIPT_DIR, ".env")
if os.path.exists(_env_path):
  load_dotenv(dotenv_path=_env_path)

PROJECT_ID = os.getenv("PROJECT_ID")
DATASET_ID = os.getenv("DATASET_ID", "decision_lineage_demo")
TABLE_ID = os.getenv("TABLE_ID", "agent_events")
SESSION_ID = os.getenv("DEMO_SESSION_ID", "sess-nike-summer-2026")
INVOCATION_ID = "inv-001"
USER_ID = "u-acme-mediabuyer-018"
TRACE_ID = "trace-" + SESSION_ID
PLANNER_AGENT = "media_planner"
ROOT_AGENT = "root_agent"
MODEL = "gemini-2.5-pro"
SYSTEM_PROMPT = (
    "You are an expert media-planner agent for athletic-footwear "
    "campaigns. Plan strategically, evaluate alternatives, and "
    "explicitly state which candidates you considered, the score "
    "you assigned each, and why dropped candidates were rejected. "
    "Then call the appropriate tool to commit each decision."
)

if not PROJECT_ID:
  print(
      "ERROR: PROJECT_ID not set. Run ./setup.sh first or export "
      "PROJECT_ID.",
      file=sys.stderr,
  )
  sys.exit(2)

# ---------------------------------------------------------------------- #
# Decision narrative.                                                      #
#                                                                          #
# Five LLM_RESPONSE bodies, each enumerating three candidates. Each is     #
# verbose enough that AI.GENERATE reliably extracts (decision_type,        #
# description, candidates[name, score, status, rejection_rationale]).      #
# ---------------------------------------------------------------------- #

_AUDIENCE_NARRATIVE = (
    "For the Nike Summer Run 2026 audience-selection decision I "
    "evaluated three candidate audiences:\n"
    "  1. 'Athletes 18-35' (SELECTED, score 0.92) — strongest fit on "
    "the Q1 conversion benchmark for performance footwear; matches "
    "category buyer profile from prior summer campaigns.\n"
    "  2. 'Casual fitness enthusiasts' (DROPPED, score 0.71) — below "
    "the 0.85 conversion threshold from the Q1 cohort study; would "
    "underperform on summer footwear LTV given lower category intent.\n"
    "  3. 'General sports fans' (DROPPED, score 0.58) — failed the "
    "brand-alignment audit; too broad for a running-shoe push, "
    "dilutes the performance positioning.\n"
    "Decision: target Athletes 18-35. Calling select_audience tool."
)

_BUDGET_NARRATIVE = (
    "For the Nike Summer Run 2026 budget-allocation decision (channel "
    "split for the primary $120K of the $360K total) I evaluated "
    "three candidate placements:\n"
    "  1. '$120K Instagram Reels' (SELECTED, score 0.88) — best CPM "
    "and retention for Athletes 18-35 based on Q1 history; reach "
    "extends to ~3.4M unique impressions over the launch window.\n"
    "  2. '$120K TikTok Spark Ads' (DROPPED, score 0.74) — channel "
    "overlap with Instagram Reels for the same demographic; Q1 "
    "retention 18% lower at matched spend.\n"
    "  3. '$120K YouTube Shorts' (DROPPED, score 0.62) — CPM 32% above "
    "the campaign ceiling; would breach the $360K total budget if "
    "scaled to required reach.\n"
    "Decision: allocate $120K to Instagram Reels. "
    "Calling allocate_budget tool."
)

_CREATIVE_NARRATIVE = (
    "For the Nike Summer Run 2026 creative-selection decision I "
    "evaluated three candidate creative themes:\n"
    "  1. 'Just Do It - Summer Edition' (SELECTED, score 0.91) — "
    "strongest brand-recall lift in the Q1 A/B against Athletes 18-35; "
    "extends an established platform with seasonal framing.\n"
    "  2. 'Run Past Your Limits' (DROPPED, score 0.78) — performed "
    "below the 0.85 brand-recall floor for the segment in pretest "
    "creatives; new creative platform without prior equity.\n"
    "  3. 'The Daily Mile' (DROPPED, score 0.65) — failed creative "
    "review on tone alignment; wellness narrative undercuts the "
    "performance positioning the campaign needs.\n"
    "Decision: ship 'Just Do It - Summer Edition'. "
    "Calling select_creative tool."
)

_CHANNEL_NARRATIVE = (
    "For the Nike Summer Run 2026 channel-strategy decision (primary "
    "platform versus reinforcement mix) I evaluated three approaches:\n"
    "  1. 'Instagram-led, TV reinforcement' (SELECTED, score 0.86) — "
    "best lift modeling for Athletes 18-35 within the existing $360K "
    "ceiling; full reach achievable without incremental spend.\n"
    "  2. 'TV-led, social reinforcement' (DROPPED, score 0.69) — would "
    "require a $200K incremental TV buy beyond the campaign ceiling; "
    "reach gain is diminishing at this footprint size.\n"
    "  3. 'Out-of-home led, digital reinforcement' (DROPPED, score "
    "0.54) — measurement gaps on conversion attribution; insufficient "
    "audience-match data for OOH inventory in target metros.\n"
    "Decision: Instagram-led with TV reinforcement."
)

_SCHEDULING_NARRATIVE = (
    "For the Nike Summer Run 2026 scheduling decision I evaluated "
    "three launch-window options:\n"
    "  1. 'May 27 launch, 8-week run' (SELECTED, score 0.93) — "
    "captures Memorial Day weekend kickoff and the full pre-July-4 "
    "footprint window; effective frequency holds above 5x for the "
    "audience.\n"
    "  2. 'June 17 launch, 6-week run' (DROPPED, score 0.71) — misses "
    "Memorial Day; shorter window cuts effective frequency below 4x "
    "for the audience and leaves CPM efficiency on the table.\n"
    "  3. 'July 4 launch, 4-week run' (DROPPED, score 0.55) — "
    "competitive density at peak holiday; CPMs forecast 25% above "
    "the campaign ceiling and creative wear-out risk is elevated.\n"
    "Decision: launch May 27 for an 8-week run."
)

# ---------------------------------------------------------------------- #
# Span builder helpers.                                                    #
# ---------------------------------------------------------------------- #

_BASE_TS = datetime(2026, 4, 23, 14, 0, 0, tzinfo=timezone.utc)


def _ts(seconds_offset: int) -> datetime:
  return _BASE_TS + timedelta(seconds=seconds_offset)


def _llm_attrs(
    input_tokens: int,
    output_tokens: int | None = None,
    temperature: float = 0.2,
) -> dict[str, Any]:
  attrs: dict[str, Any] = {
      "model": MODEL,
      "temperature": temperature,
      "input_tokens": input_tokens,
  }
  if output_tokens is not None:
    attrs["output_tokens"] = output_tokens
    attrs["finish_reason"] = "STOP"
  return attrs


def _llm_request_messages(user_turn: str) -> list[dict[str, str]]:
  return [
      {"role": "system", "content": SYSTEM_PROMPT},
      {"role": "user", "content": user_turn},
  ]


# ---------------------------------------------------------------------- #
# Trace.                                                                   #
#                                                                          #
# The chain: invocation bookends wrap a USER message → 5 LLM_REQ/RESP     #
# pairs (one per decision) → 3 TOOL_STARTING/COMPLETED pairs (the         #
# decisions that commit through tools) → HITL approval → AGENT_COMPLETED. #
# ---------------------------------------------------------------------- #

# Tool-call ids — LLM_RESPONSE function_calls reference these and link
# to the matching TOOL_STARTING/COMPLETED pair.
_TC_AUDIENCE = "tc-01-audience"
_TC_BUDGET = "tc-02-budget"
_TC_CREATIVE = "tc-03-creative"

SPANS: list[dict] = [
    # ------------------------------------------------------------------ #
    # Invocation bookend                                                   #
    # ------------------------------------------------------------------ #
    {
        "span_id": "span-001",
        "parent_span_id": None,
        "event_type": "INVOCATION_STARTING",
        "agent": ROOT_AGENT,
        "timestamp": _ts(0),
        "content": {
            "user_message": (
                "Plan our Nike Summer Run 2026 push: pick the audience, "
                "split a $360K media budget across three channels, "
                "choose the creative theme, and tell me when to launch."
            ),
        },
        "attributes": {"app_name": "media_planner_app"},
    },
    {
        "span_id": "span-002",
        "parent_span_id": "span-001",
        "event_type": "AGENT_STARTING",
        "agent": PLANNER_AGENT,
        "timestamp": _ts(0),
        "content": {},
        "attributes": {"agent_name": PLANNER_AGENT, "model": MODEL},
    },
    {
        "span_id": "span-003",
        "parent_span_id": "span-002",
        "event_type": "USER_MESSAGE_RECEIVED",
        "agent": PLANNER_AGENT,
        "timestamp": _ts(0),
        "content": {
            "text": (
                "Plan our Nike Summer Run 2026 push: pick the audience, "
                "split a $360K media budget across three channels, "
                "choose the creative theme, and tell me when to launch."
            ),
        },
        "attributes": {},
    },
    # ------------------------------------------------------------------ #
    # Decision 1: audience_selection                                       #
    # ------------------------------------------------------------------ #
    {
        "span_id": "span-004",
        "parent_span_id": "span-003",
        "event_type": "LLM_REQUEST",
        "agent": PLANNER_AGENT,
        "timestamp": _ts(1),
        "content": {
            "messages": _llm_request_messages(
                "First, pick the primary audience for the Nike Summer "
                "Run 2026 campaign. Evaluate at least three candidate "
                "audiences and explain why dropped candidates were "
                "rejected, then call select_audience to commit."
            ),
            "tools": ["select_audience", "allocate_budget", "select_creative"],
        },
        "attributes": _llm_attrs(input_tokens=482, temperature=0.2),
    },
    {
        "span_id": "span-005",
        "parent_span_id": "span-004",
        "event_type": "LLM_RESPONSE",
        "agent": PLANNER_AGENT,
        "timestamp": _ts(3),
        "content": {
            "text_summary": _AUDIENCE_NARRATIVE,
            "campaign": "Nike Summer Run 2026",
            "decision_type": "audience_selection",
            "function_calls": [
                {
                    "name": "select_audience",
                    "tool_call_id": _TC_AUDIENCE,
                    "args": {
                        "audience": "Athletes 18-35",
                        "campaign": "Nike Summer Run 2026",
                    },
                }
            ],
        },
        "attributes": _llm_attrs(
            input_tokens=482, output_tokens=420, temperature=0.2
        ),
        "latency_ms": {"total_ms": 1840, "time_to_first_token_ms": 410},
    },
    {
        "span_id": "span-006",
        "parent_span_id": "span-005",
        "event_type": "TOOL_STARTING",
        "agent": "audience_tool",
        "timestamp": _ts(3),
        "content": {
            "tool": "select_audience",
            "tool_call_id": _TC_AUDIENCE,
            "args": {
                "audience": "Athletes 18-35",
                "campaign": "Nike Summer Run 2026",
            },
        },
        "attributes": {},
    },
    {
        "span_id": "span-007",
        "parent_span_id": "span-006",
        "event_type": "TOOL_COMPLETED",
        "agent": "audience_tool",
        "timestamp": _ts(4),
        "content": {
            "tool": "select_audience",
            "tool_call_id": _TC_AUDIENCE,
            "result": {
                "status": "ok",
                "audience_id": "aud-athletes-18-35",
                "estimated_reach": 3_400_000,
            },
            "text_summary": (
                "Confirmed audience 'Athletes 18-35' for campaign "
                "'Nike Summer Run 2026'. Estimated reach 3.4M."
            ),
        },
        "attributes": {},
        "latency_ms": {"total_ms": 620},
    },
    # ------------------------------------------------------------------ #
    # Decision 2: budget_allocation                                        #
    # ------------------------------------------------------------------ #
    {
        "span_id": "span-008",
        "parent_span_id": "span-007",
        "event_type": "LLM_REQUEST",
        "agent": PLANNER_AGENT,
        "timestamp": _ts(5),
        "content": {
            "messages": _llm_request_messages(
                "Next, split the $360K media budget. Pick the primary "
                "$120K placement and explain alternatives that were "
                "considered and dropped. Call allocate_budget to "
                "commit the primary."
            ),
        },
        "attributes": _llm_attrs(input_tokens=611, temperature=0.2),
    },
    {
        "span_id": "span-009",
        "parent_span_id": "span-008",
        "event_type": "LLM_RESPONSE",
        "agent": PLANNER_AGENT,
        "timestamp": _ts(7),
        "content": {
            "text_summary": _BUDGET_NARRATIVE,
            "campaign": "Nike Summer Run 2026",
            "decision_type": "budget_allocation",
            "function_calls": [
                {
                    "name": "allocate_budget",
                    "tool_call_id": _TC_BUDGET,
                    "args": {
                        "placement": "Instagram Reels",
                        "amount_usd": 120_000,
                        "campaign": "Nike Summer Run 2026",
                    },
                }
            ],
        },
        "attributes": _llm_attrs(
            input_tokens=611, output_tokens=448, temperature=0.2
        ),
        "latency_ms": {"total_ms": 1910, "time_to_first_token_ms": 380},
    },
    {
        "span_id": "span-010",
        "parent_span_id": "span-009",
        "event_type": "TOOL_STARTING",
        "agent": "budget_tool",
        "timestamp": _ts(7),
        "content": {
            "tool": "allocate_budget",
            "tool_call_id": _TC_BUDGET,
            "args": {
                "placement": "Instagram Reels",
                "amount_usd": 120_000,
                "campaign": "Nike Summer Run 2026",
            },
        },
        "attributes": {},
    },
    {
        "span_id": "span-011",
        "parent_span_id": "span-010",
        "event_type": "TOOL_COMPLETED",
        "agent": "budget_tool",
        "timestamp": _ts(8),
        "content": {
            "tool": "allocate_budget",
            "tool_call_id": _TC_BUDGET,
            "result": {
                "status": "ok",
                "placement_id": "pl-instagram-reels-001",
                "committed_usd": 120_000,
                "remaining_budget_usd": 240_000,
            },
            "text_summary": (
                "Confirmed placement '$120K Instagram Reels' under "
                "campaign 'Nike Summer Run 2026'. $240,000 remaining."
            ),
        },
        "attributes": {},
        "latency_ms": {"total_ms": 540},
    },
    # ------------------------------------------------------------------ #
    # Decision 3: creative_selection                                       #
    # ------------------------------------------------------------------ #
    {
        "span_id": "span-012",
        "parent_span_id": "span-011",
        "event_type": "LLM_REQUEST",
        "agent": PLANNER_AGENT,
        "timestamp": _ts(9),
        "content": {
            "messages": _llm_request_messages(
                "Choose the creative theme. Evaluate at least three "
                "candidate themes against Athletes 18-35 and call "
                "select_creative to commit."
            ),
        },
        "attributes": _llm_attrs(input_tokens=702, temperature=0.2),
    },
    {
        "span_id": "span-013",
        "parent_span_id": "span-012",
        "event_type": "LLM_RESPONSE",
        "agent": PLANNER_AGENT,
        "timestamp": _ts(11),
        "content": {
            "text_summary": _CREATIVE_NARRATIVE,
            "campaign": "Nike Summer Run 2026",
            "decision_type": "creative_selection",
            "function_calls": [
                {
                    "name": "select_creative",
                    "tool_call_id": _TC_CREATIVE,
                    "args": {
                        "theme": "Just Do It - Summer Edition",
                        "campaign": "Nike Summer Run 2026",
                    },
                }
            ],
        },
        "attributes": _llm_attrs(
            input_tokens=702, output_tokens=412, temperature=0.2
        ),
        "latency_ms": {"total_ms": 1750, "time_to_first_token_ms": 395},
    },
    {
        "span_id": "span-014",
        "parent_span_id": "span-013",
        "event_type": "TOOL_STARTING",
        "agent": "creative_tool",
        "timestamp": _ts(11),
        "content": {
            "tool": "select_creative",
            "tool_call_id": _TC_CREATIVE,
            "args": {
                "theme": "Just Do It - Summer Edition",
                "campaign": "Nike Summer Run 2026",
            },
        },
        "attributes": {},
    },
    {
        "span_id": "span-015",
        "parent_span_id": "span-014",
        "event_type": "TOOL_COMPLETED",
        "agent": "creative_tool",
        "timestamp": _ts(12),
        "content": {
            "tool": "select_creative",
            "tool_call_id": _TC_CREATIVE,
            "result": {
                "status": "ok",
                "creative_id": "cr-jdi-summer-2026",
                "asset_count": 8,
            },
            "text_summary": (
                "Confirmed creative theme 'Just Do It - Summer Edition' "
                "(8 assets) for 'Nike Summer Run 2026'."
            ),
        },
        "attributes": {},
        "latency_ms": {"total_ms": 480},
    },
    # ------------------------------------------------------------------ #
    # Decision 4: channel_strategy (LLM-only, no tool commit)              #
    # ------------------------------------------------------------------ #
    {
        "span_id": "span-016",
        "parent_span_id": "span-015",
        "event_type": "LLM_REQUEST",
        "agent": PLANNER_AGENT,
        "timestamp": _ts(13),
        "content": {
            "messages": _llm_request_messages(
                "Recommend the channel strategy: which platform leads "
                "and which reinforce. Evaluate three approaches and "
                "explain why dropped options were rejected."
            ),
        },
        "attributes": _llm_attrs(input_tokens=798, temperature=0.2),
    },
    {
        "span_id": "span-017",
        "parent_span_id": "span-016",
        "event_type": "LLM_RESPONSE",
        "agent": PLANNER_AGENT,
        "timestamp": _ts(15),
        "content": {
            "text_summary": _CHANNEL_NARRATIVE,
            "campaign": "Nike Summer Run 2026",
            "decision_type": "channel_strategy",
        },
        "attributes": _llm_attrs(
            input_tokens=798, output_tokens=388, temperature=0.2
        ),
        "latency_ms": {"total_ms": 1680, "time_to_first_token_ms": 365},
    },
    # ------------------------------------------------------------------ #
    # Decision 5: scheduling (LLM-only)                                    #
    # ------------------------------------------------------------------ #
    {
        "span_id": "span-018",
        "parent_span_id": "span-017",
        "event_type": "LLM_REQUEST",
        "agent": PLANNER_AGENT,
        "timestamp": _ts(16),
        "content": {
            "messages": _llm_request_messages(
                "Final step: pick the launch window. Evaluate three "
                "candidate windows and explain why dropped options were "
                "rejected. Then summarize the full plan."
            ),
        },
        "attributes": _llm_attrs(input_tokens=864, temperature=0.2),
    },
    {
        "span_id": "span-019",
        "parent_span_id": "span-018",
        "event_type": "LLM_RESPONSE",
        "agent": PLANNER_AGENT,
        "timestamp": _ts(18),
        "content": {
            "text_summary": _SCHEDULING_NARRATIVE,
            "campaign": "Nike Summer Run 2026",
            "decision_type": "scheduling",
        },
        "attributes": _llm_attrs(
            input_tokens=864, output_tokens=402, temperature=0.2
        ),
        "latency_ms": {"total_ms": 1720, "time_to_first_token_ms": 372},
    },
    # ------------------------------------------------------------------ #
    # HITL final approval                                                  #
    # ------------------------------------------------------------------ #
    {
        "span_id": "span-020",
        "parent_span_id": "span-019",
        "event_type": "HITL_CONFIRMATION_REQUEST",
        "agent": PLANNER_AGENT,
        "timestamp": _ts(19),
        "content": {
            "tool": "confirm_plan",
            "args": {
                "plan_summary": (
                    "Nike Summer Run 2026: audience 'Athletes 18-35', "
                    "primary placement '$120K Instagram Reels' "
                    "($240K remaining), creative 'Just Do It - Summer "
                    "Edition' (8 assets), Instagram-led with TV "
                    "reinforcement, launch May 27 for 8 weeks."
                ),
            },
        },
        "attributes": {},
    },
    {
        "span_id": "span-021",
        "parent_span_id": "span-020",
        "event_type": "HITL_CONFIRMATION_REQUEST_COMPLETED",
        "agent": PLANNER_AGENT,
        "timestamp": _ts(35),
        "content": {
            "tool": "confirm_plan",
            "result": "approved",
            "approver": USER_ID,
            "text_summary": "User approved the full media plan.",
        },
        "attributes": {},
        "latency_ms": {"total_ms": 16_000},
    },
    # ------------------------------------------------------------------ #
    # AGENT_COMPLETED + INVOCATION_COMPLETED                               #
    # ------------------------------------------------------------------ #
    {
        "span_id": "span-022",
        "parent_span_id": "span-021",
        "event_type": "AGENT_COMPLETED",
        "agent": PLANNER_AGENT,
        "timestamp": _ts(35),
        "content": {
            "text_summary": (
                "Plan ready and approved for 'Nike Summer Run 2026': "
                "audience 'Athletes 18-35', primary placement '$120K "
                "Instagram Reels', creative 'Just Do It - Summer "
                "Edition', Instagram-led with TV reinforcement, "
                "launch May 27 for an 8-week run."
            ),
        },
        "attributes": {},
        "latency_ms": {"total_ms": 35_500},
    },
    {
        "span_id": "span-023",
        "parent_span_id": "span-001",
        "event_type": "INVOCATION_COMPLETED",
        "agent": ROOT_AGENT,
        "timestamp": _ts(35),
        "content": {},
        "attributes": {"app_name": "media_planner_app"},
        "latency_ms": {"total_ms": 35_700},
    },
]

# ---------------------------------------------------------------------- #
# agent_events table bootstrap.                                            #
# ---------------------------------------------------------------------- #

_AGENT_EVENTS_DDL = """\
CREATE TABLE IF NOT EXISTS `{project}.{dataset}.{table}` (
  timestamp TIMESTAMP NOT NULL,
  event_type STRING NOT NULL,
  agent STRING,
  session_id STRING,
  invocation_id STRING,
  user_id STRING,
  trace_id STRING,
  span_id STRING,
  parent_span_id STRING,
  content JSON,
  content_parts ARRAY<STRUCT<
    text STRING,
    object_ref STRUCT<uri STRING, mime_type STRING>
  >>,
  attributes JSON,
  latency_ms JSON,
  status STRING,
  error_message STRING,
  is_truncated BOOL
)
PARTITION BY DATE(timestamp)
CLUSTER BY event_type, agent
"""

_DELETE_DEMO_EVENTS = """\
DELETE FROM `{project}.{dataset}.{table}`
WHERE session_id = @session_id
"""


def _ensure_agent_events_table(client: bigquery.Client) -> None:
  ddl = _AGENT_EVENTS_DDL.format(
      project=PROJECT_ID, dataset=DATASET_ID, table=TABLE_ID
  )
  client.query(ddl).result()


def _delete_existing_demo_rows(client: bigquery.Client) -> None:
  query = _DELETE_DEMO_EVENTS.format(
      project=PROJECT_ID, dataset=DATASET_ID, table=TABLE_ID
  )
  job_config = bigquery.QueryJobConfig(
      query_parameters=[
          bigquery.ScalarQueryParameter(
              "session_id", "STRING", SESSION_ID
          ),
      ]
  )
  try:
    client.query(query, job_config=job_config).result()
  except Exception as exc:  # pylint: disable=broad-except
    msg = str(exc).lower()
    if "not found" not in msg and "does not exist" not in msg:
      raise


def _build_event_row(span: dict) -> dict:
  # JSON-typed columns (content, attributes, latency_ms) are sent as
  # JSON strings via insert_rows_json — the streaming insert REST
  # endpoint does not auto-encode nested Python dicts into JSON-typed
  # destinations.
  latency = span.get("latency_ms")
  return {
      "timestamp": span["timestamp"].isoformat(),
      "event_type": span["event_type"],
      "agent": span.get("agent"),
      "session_id": SESSION_ID,
      "invocation_id": INVOCATION_ID,
      "user_id": USER_ID,
      "trace_id": TRACE_ID,
      "span_id": span["span_id"],
      "parent_span_id": span.get("parent_span_id"),
      "content": json.dumps(span.get("content") or {}),
      "content_parts": [],
      "attributes": json.dumps(span.get("attributes") or {}),
      "latency_ms": json.dumps(latency) if latency else None,
      "status": span.get("status", "OK"),
      "error_message": span.get("error_message"),
      "is_truncated": False,
  }


def _insert_events(client: bigquery.Client) -> None:
  table_ref = f"{PROJECT_ID}.{DATASET_ID}.{TABLE_ID}"
  rows = [_build_event_row(s) for s in SPANS]
  errors = client.insert_rows_json(table_ref, rows)
  if errors:
    print(f"ERROR inserting agent_events rows: {errors}", file=sys.stderr)
    sys.exit(1)


def main() -> int:
  print(f"Project : {PROJECT_ID}")
  print(f"Dataset : {DATASET_ID}")
  print(f"Table   : {TABLE_ID}")
  print(f"Session : {SESSION_ID}")
  print(f"Spans   : {len(SPANS)}")
  print()

  client = bigquery.Client(project=PROJECT_ID)
  print("[1/3] Ensuring agent_events table exists...")
  _ensure_agent_events_table(client)
  print("[2/3] Clearing prior demo rows for this session (idempotent)...")
  _delete_existing_demo_rows(client)
  print(f"[3/3] Inserting {len(SPANS)} plugin-shape trace rows "
        f"covering 5 decisions, 15 candidates...")
  _insert_events(client)
  print()
  print("Trace seed complete.")
  return 0


if __name__ == "__main__":
  sys.exit(main())
