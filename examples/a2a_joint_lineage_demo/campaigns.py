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

"""Campaign briefs for the joint A2A lineage demo.

Each entry becomes one caller-side ADK session. The supervisor makes
four local decisions (goal, budget, channel mix, creative theme) and
then delegates audience-risk review to the remote governance agent
over A2A. The remote delegation is what produces the
``A2A_INTERACTION`` row the auditor projection joins on.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CampaignBrief:
  campaign: str
  brand: str
  brief: str


CAMPAIGN_BRIEFS: list[CampaignBrief] = [
    CampaignBrief(
        campaign="Nike Summer Run 2026",
        brand="Nike",
        brief=(
            "Plan our Nike Summer Run 2026 campaign. Total media "
            "budget is $360K. Lead category is performance running "
            "shoes. Memorial Day to early July footprint preferred. "
            "Pick the goal, the primary $120K placement, the "
            "channel-mix strategy, and the creative theme. Then "
            "delegate audience-risk review to the governance agent "
            "with three candidate audiences for adults 18-35 who "
            "regularly run."
        ),
    ),
    CampaignBrief(
        campaign="Adidas Track Season 2026",
        brand="Adidas",
        brief=(
            "Plan our Adidas Track Season 2026 push. Budget $420K. "
            "Lead category is sprint spikes and track apparel. "
            "Outdoor track season runs March-May. Pick goal, primary "
            "$150K placement, channel-mix strategy, and creative "
            "theme. Then delegate audience-risk review with three "
            "candidate audiences for high-school and NCAA sprinters."
        ),
    ),
    CampaignBrief(
        campaign="Lululemon Yoga Flow 2026",
        brand="Lululemon",
        brief=(
            "Plan our Lululemon Yoga Flow 2026 push. Budget $250K. "
            "Lead category is athleisure and yoga apparel. Spring "
            "wellness window is April-May. Pick goal, primary $90K "
            "placement, channel-mix strategy, and creative theme. "
            "Then delegate audience-risk review with three candidate "
            "audiences for urban yoga and pilates practitioners."
        ),
    ),
]
