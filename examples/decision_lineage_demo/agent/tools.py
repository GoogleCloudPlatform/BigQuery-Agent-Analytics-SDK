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

"""Decision-commit tools for the media-planner agent.

Each tool corresponds to one of the five decision types tracked by
the demo:

  - select_audience       (audience_selection)
  - allocate_budget       (budget_allocation)
  - select_creative       (creative_selection)
  - define_channel_strategy (channel_strategy)
  - schedule_launch       (scheduling)

Tools just acknowledge the choice and return synthetic IDs/metrics —
the demo's value is in the agent's reasoning trace (the LLM_RESPONSE
that names alternatives and rationale before calling the tool), not
in the tools themselves.
"""

from __future__ import annotations

import hashlib
from typing import Any


def _short_hash(*parts: str) -> str:
  raw = "::".join(parts).encode("utf-8")
  return hashlib.sha1(raw).hexdigest()[:10]


def select_audience(
    audience: str, campaign: str, rationale: str
) -> dict[str, Any]:
  """Commit the selected audience for a campaign.

  Args:
      audience: Selected audience name (e.g. "Athletes 18-35").
      campaign: Campaign name (e.g. "Nike Summer Run 2026").
      rationale: One-sentence justification for the SELECTED audience.

  Returns:
      Dict with status, audience_id, and an estimated reach figure.
  """
  audience_id = "aud-" + _short_hash(campaign, audience)
  estimated_reach = 1_500_000 + (hash(audience) % 5_000_000)
  return {
      "status": "ok",
      "audience_id": audience_id,
      "audience": audience,
      "campaign": campaign,
      "estimated_reach": estimated_reach,
      "rationale": rationale,
  }


def allocate_budget(
    placement: str, amount_usd: float, campaign: str, rationale: str
) -> dict[str, Any]:
  """Commit a primary budget allocation for a campaign.

  Args:
      placement: Selected placement (e.g. "Instagram Reels").
      amount_usd: Allocated budget in USD for this placement.
      campaign: Campaign name.
      rationale: One-sentence justification for the SELECTED placement.

  Returns:
      Dict with status, placement_id, and committed dollars.
  """
  placement_id = "pl-" + _short_hash(campaign, placement)
  return {
      "status": "ok",
      "placement_id": placement_id,
      "placement": placement,
      "campaign": campaign,
      "committed_usd": float(amount_usd),
      "rationale": rationale,
  }


def select_creative(
    theme: str, campaign: str, rationale: str
) -> dict[str, Any]:
  """Commit the selected creative theme for a campaign.

  Args:
      theme: Selected creative theme (e.g. "Just Do It - Summer").
      campaign: Campaign name.
      rationale: One-sentence justification for the SELECTED theme.

  Returns:
      Dict with status, creative_id, and a synthetic asset_count.
  """
  creative_id = "cr-" + _short_hash(campaign, theme)
  asset_count = 6 + (abs(hash(theme)) % 6)
  return {
      "status": "ok",
      "creative_id": creative_id,
      "theme": theme,
      "campaign": campaign,
      "asset_count": asset_count,
      "rationale": rationale,
  }


def define_channel_strategy(
    strategy: str, campaign: str, rationale: str
) -> dict[str, Any]:
  """Commit the channel strategy (primary / reinforcement mix).

  Args:
      strategy: Selected strategy (e.g. "Instagram-led, TV reinforcement").
      campaign: Campaign name.
      rationale: One-sentence justification for the SELECTED strategy.
  """
  strategy_id = "ch-" + _short_hash(campaign, strategy)
  return {
      "status": "ok",
      "strategy_id": strategy_id,
      "strategy": strategy,
      "campaign": campaign,
      "rationale": rationale,
  }


def schedule_launch(
    launch_date: str,
    duration_weeks: int,
    campaign: str,
    rationale: str,
) -> dict[str, Any]:
  """Commit the launch window for a campaign.

  Args:
      launch_date: ISO-like launch date string (e.g. "2026-05-27").
      duration_weeks: Number of weeks the campaign will run.
      campaign: Campaign name.
      rationale: One-sentence justification for the SELECTED window.
  """
  schedule_id = "sch-" + _short_hash(campaign, launch_date)
  return {
      "status": "ok",
      "schedule_id": schedule_id,
      "launch_date": launch_date,
      "duration_weeks": int(duration_weeks),
      "campaign": campaign,
      "rationale": rationale,
  }


AGENT_TOOLS = [
    select_audience,
    allocate_budget,
    select_creative,
    define_channel_strategy,
    schedule_launch,
]
