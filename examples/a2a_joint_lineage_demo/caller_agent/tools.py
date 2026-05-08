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

"""Local decision-commit tools for the caller-side supervisor.

The caller has four local tools — campaign goal, budget, channel
mix, and creative theme — and delegates audience-risk review to the
receiver via ``RemoteA2aAgent`` (wired in ``agent.py``, not here).

These tools just acknowledge the LLM's structured choice and return
synthetic IDs. The demo's value is in the agent's reasoning trace
(LLM_RESPONSE text that names alternatives and rationale before each
tool call), not in the tool side effects.
"""

from __future__ import annotations

import hashlib
from typing import Any


def _short_hash(*parts: str) -> str:
  raw = "::".join(parts).encode("utf-8")
  return hashlib.sha1(raw).hexdigest()[:10]


def select_campaign_goal(
    goal: str, campaign: str, rationale: str
) -> dict[str, Any]:
  """Commit the selected campaign goal.

  Args:
      goal: Selected campaign goal (e.g. "Drive trial purchase").
      campaign: Campaign name.
      rationale: One-sentence justification for the SELECTED goal.

  Returns:
      Dict with status, goal_id, and recorded rationale.
  """
  return {
      "status": "ok",
      "goal_id": "goal-" + _short_hash(campaign, goal),
      "campaign": campaign,
      "goal": goal,
      "rationale": rationale,
  }


def allocate_budget(
    placement: str, amount_usd: int, campaign: str, rationale: str
) -> dict[str, Any]:
  """Commit a primary placement and budget allocation.

  Args:
      placement: Selected primary placement (e.g. "Connected TV").
      amount_usd: Allocated USD amount.
      campaign: Campaign name.
      rationale: Justification for the SELECTED placement.

  Returns:
      Dict with status, allocation_id, placement, amount, rationale.
  """
  return {
      "status": "ok",
      "allocation_id": "alloc-" + _short_hash(campaign, placement),
      "placement": placement,
      "amount_usd": amount_usd,
      "campaign": campaign,
      "rationale": rationale,
  }


def choose_channel_mix(
    strategy: str, campaign: str, rationale: str
) -> dict[str, Any]:
  """Commit the channel-mix strategy.

  Args:
      strategy: Selected channel-mix strategy (e.g. "CTV + paid
          social retarget").
      campaign: Campaign name.
      rationale: Justification for the SELECTED strategy.

  Returns:
      Dict with status, strategy_id, strategy, rationale.
  """
  return {
      "status": "ok",
      "strategy_id": "chan-" + _short_hash(campaign, strategy),
      "strategy": strategy,
      "campaign": campaign,
      "rationale": rationale,
  }


def choose_creative_theme(
    theme: str, campaign: str, rationale: str
) -> dict[str, Any]:
  """Commit the creative theme.

  Args:
      theme: Selected creative theme (e.g. "Personal best — every
          mile counts").
      campaign: Campaign name.
      rationale: Justification for the SELECTED theme.

  Returns:
      Dict with status, theme_id, theme, rationale.
  """
  return {
      "status": "ok",
      "theme_id": "creative-" + _short_hash(campaign, theme),
      "theme": theme,
      "campaign": campaign,
      "rationale": rationale,
  }


CALLER_LOCAL_TOOLS = [
    select_campaign_goal,
    allocate_budget,
    choose_channel_mix,
    choose_creative_theme,
]
