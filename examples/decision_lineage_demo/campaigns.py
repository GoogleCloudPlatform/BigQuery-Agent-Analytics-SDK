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

"""Campaign briefs for the multi-session demo run.

Each entry becomes one isolated agent invocation (one session) where
the agent makes five decisions: audience, budget, creative, channel,
schedule. Different brands, audiences, budgets, and seasons keep the
extraction surface diverse.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CampaignBrief:
  campaign: str
  brief: str


CAMPAIGN_BRIEFS: list[CampaignBrief] = [
    CampaignBrief(
        campaign="Nike Summer Run 2026",
        brief=(
            "Plan our Nike Summer Run 2026 push. Total media budget is "
            "$360K. Primary audience: serious runners 18-35. Lead "
            "category is performance running shoes. Memorial Day to "
            "early July footprint preferred. Pick the audience, the "
            "primary $120K placement, the creative theme, the channel "
            "strategy, and the launch window."
        ),
    ),
    CampaignBrief(
        campaign="Nike Winter Trail 2026",
        brief=(
            "Plan our Nike Winter Trail 2026 launch. Budget $500K. "
            "Audience: trail-runners and hikers 25-45 in cold-weather "
            "metros. Category: insulated outdoor footwear. We need "
            "lift through the December retail window. Plan audience, "
            "primary $180K placement, creative theme, channel "
            "strategy, and launch window."
        ),
    ),
    CampaignBrief(
        campaign="Adidas Track Season 2026",
        brief=(
            "Plan our Adidas Track Season 2026 push targeting NCAA "
            "and high-school sprinters 16-22. Budget $420K. Category: "
            "sprint spikes and track apparel. Outdoor track season is "
            "March-May. Pick audience, primary $150K placement, "
            "creative theme, channel strategy, and launch window."
        ),
    ),
    CampaignBrief(
        campaign="Puma Soccer Cup 2026",
        brief=(
            "Plan our Puma Soccer Cup 2026 push. Budget $280K. "
            "Audience: soccer fans 18-30, weighted to club-team "
            "supporters. Category: cleats and replica jerseys. Tournament "
            "window is June-July. Pick audience, primary $100K "
            "placement, creative theme, channel strategy, and launch "
            "window."
        ),
    ),
    CampaignBrief(
        campaign="Reebok CrossFit Open 2026",
        brief=(
            "Plan our Reebok CrossFit Open 2026 push. Budget $340K. "
            "Audience: fitness pros and amateur athletes 25-40 active "
            "in CrossFit boxes. Category: training shoes and "
            "competition apparel. Open window is February-March. Pick "
            "audience, primary $120K placement, creative theme, "
            "channel strategy, and launch window."
        ),
    ),
    CampaignBrief(
        campaign="Lululemon Yoga Flow 2026",
        brief=(
            "Plan our Lululemon Yoga Flow 2026 push. Budget $250K. "
            "Audience: yoga and pilates practitioners 22-45, urban "
            "metros. Category: athleisure and yoga apparel. Spring "
            "wellness window is April-May. Pick audience, primary "
            "$90K placement, creative theme, channel strategy, and "
            "launch window."
        ),
    ),
]
