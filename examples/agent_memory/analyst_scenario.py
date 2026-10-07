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

"""The scripted week behind the recorded run.

Six analysts at TheLook, an online clothing retailer whose data is the
public ``bigquery-public-data.thelook_ecommerce`` dataset, use one
data-analyst agent over five business days. They state their scope and
definitions early in the week and come back to them later: daily check-ins,
follow-ups that only make sense with memory ("my categories", "the board
meeting", "the same lead-time check"), a changed exchange rate, a widened
category scope. The agent runs for real; only the questions are scripted.

Days are simulated: each session carries its date in ADK session state
(``sim_date``), which the agent treats as today and the plugin logs with
every row. For the sessions marked ``compare``, the same first question is
also put to an agent without memory, on the same day, as a control.
"""

from __future__ import annotations

import dataclasses


@dataclasses.dataclass(frozen=True)
class Analyst:
  user_id: str
  name: str
  role: str


@dataclasses.dataclass(frozen=True)
class Day:
  number: int
  date: str  # YYYY-MM-DD, the agent's "today"
  weekday: str


@dataclasses.dataclass(frozen=True)
class ScriptedSession:
  day: int
  user_id: str
  turns: tuple[str, ...]
  compare: bool = False  # also ask the first turn without memory

  def session_id(self, run_tag: str, index: int) -> str:
    user = self.user_id.split(".")[0]
    return f"an-{run_tag}-d{self.day}-{user}-{index}"


ANALYSTS = (
    Analyst(
        "maya.chen",
        "Maya Chen",
        "merchandising lead for women's Jeans and Dresses",
    ),
    Analyst("raj.patel", "Raj Patel", "FP&A analyst"),
    Analyst(
        "lena.okafor",
        "Lena Okafor",
        "operations manager for two distribution centers",
    ),
    Analyst("diego.alvarez", "Diego Alvarez", "growth marketing lead"),
    Analyst("priya.nair", "Priya Nair", "customer insights analyst"),
    Analyst(
        "tom.becker",
        "Tom Becker",
        "category manager for men's Outerwear & Coats and Active",
    ),
)

DAYS = (
    Day(1, "2026-10-01", "Thursday"),
    Day(2, "2026-10-02", "Friday"),
    Day(3, "2026-10-05", "Monday"),
    Day(4, "2026-10-06", "Tuesday"),
    Day(5, "2026-10-07", "Wednesday"),
)

SESSIONS = (
    # ---- Day 1, Thursday ----
    ScriptedSession(
        1,
        "maya.chen",
        (
            "Hi, I'm Maya Chen. I lead merchandising for women's Jeans and"
            " Dresses. How much revenue did my categories bring in last month?",
            "Please always use net revenue: leave out cancelled and returned"
            " items. And compare it with the month before.",
        ),
    ),
    ScriptedSession(
        1,
        "raj.patel",
        (
            "I'm Raj Patel from FP&A. Our fiscal year starts on February 1,"
            " and by net revenue we mean shipped and complete items only. What"
            " was net revenue in fiscal Q2?",
            "Report amounts in euros from now on, at 0.92 EUR per USD.",
        ),
    ),
    ScriptedSession(
        1,
        "lena.okafor",
        (
            "I'm Lena Okafor in operations. I manage the Memphis and Chicago"
            " distribution centers. What was the average time from order to"
            " shipment last month for items from my DCs?",
        ),
    ),
    ScriptedSession(
        1,
        "priya.nair",
        (
            "I'm Priya Nair from customer insights. I track the repeat"
            " purchase rate: the share of first-time buyers who order again"
            " within 90 days. What was it for customers whose first order was"
            " in June?",
        ),
    ),
    ScriptedSession(
        1,
        "tom.becker",
        (
            "Tom Becker here, category manager for men's Outerwear & Coats and"
            " Active. Show units sold last month by category. I care about"
            " units, not revenue.",
        ),
    ),
    ScriptedSession(
        1,
        "maya.chen",
        ("Which five brands drove most of my Jeans net revenue last month?",),
    ),
    ScriptedSession(
        1,
        "lena.okafor",
        (
            "Which product categories shipped slowest from my DCs last"
            " month?",
        ),
    ),
    # ---- Day 2, Friday ----
    ScriptedSession(
        2, "maya.chen", ("Morning! How did my categories do yesterday?",)
    ),
    ScriptedSession(
        2,
        "raj.patel",
        (
            "The board meeting is on October 20. I'll need fiscal Q3-to-date"
            " net revenue and order count for it. What are they so far?",
        ),
    ),
    ScriptedSession(
        2, "lena.okafor", ("How many items did my DCs ship yesterday?",)
    ),
    ScriptedSession(
        2,
        "diego.alvarez",
        (
            "Hi, Diego from growth here. Our focus markets are Brazil and"
            " Spain. Which traffic sources brought the most new users there"
            " last month?",
            "I always want weekly numbers, with weeks starting on Monday. Show"
            " new users per week in my markets for the last six weeks.",
        ),
    ),
    ScriptedSession(
        2,
        "priya.nair",
        (
            "Focus on customers aged 18 to 34 from now on. How does the repeat"
            " rate look for July first-time buyers?",
        ),
    ),
    ScriptedSession(
        2,
        "tom.becker",
        (
            "Keep an eye on Columbia and The North Face for me. How many units"
            " of each sold last month in my categories?",
        ),
    ),
    ScriptedSession(
        2,
        "maya.chen",
        (
            "We review the holiday assortment on November 12. For Dresses,"
            " which price bands sell best?",
            "Use these price bands from now on: under $50, $50 to $100, and"
            " over $100.",
        ),
    ),
    # ---- Day 3, Monday ----
    ScriptedSession(
        3, "maya.chen", ("Morning. How did my categories do over the weekend?",)
    ),
    ScriptedSession(
        3,
        "raj.patel",
        (
            "Treasury updated our rate: use 0.86 EUR per USD from today."
            " Recompute fiscal Q3 to date.",
        ),
    ),
    ScriptedSession(
        3,
        "lena.okafor",
        ("How many items did my DCs ship over the weekend?",),
    ),
    ScriptedSession(
        3,
        "diego.alvarez",
        (
            "We launch the Spain loyalty campaign on October 19. Which age"
            " groups in Spain have the highest share of users who have"
            " ordered?",
        ),
    ),
    ScriptedSession(
        3,
        "priya.nair",
        (
            "Our quarterly business review is on October 23. Which traffic"
            " source brings the most repeat buyers among my customers?",
        ),
    ),
    ScriptedSession(
        3, "tom.becker", ("Weekend units for my categories, please.",)
    ),
    ScriptedSession(
        3,
        "maya.chen",
        ("How did my categories do last month?",),
        compare=True,
    ),
    # ---- Day 4, Tuesday ----
    ScriptedSession(
        4,
        "maya.chen",
        (
            "Add Swim to my categories from now on. What was the return rate"
            " for my categories last month?",
        ),
    ),
    ScriptedSession(4, "raj.patel", ("What was net revenue yesterday?",)),
    ScriptedSession(
        4,
        "lena.okafor",
        ("Run the same lead-time check for this month so far.",),
        compare=True,
    ),
    ScriptedSession(4, "diego.alvarez", ("How did my markets do last week?",)),
    ScriptedSession(
        4,
        "priya.nair",
        (
            "Any change in the repeat rate for August first-time buyers in my"
            " segment?",
        ),
    ),
    ScriptedSession(
        4,
        "tom.becker",
        (
            "I meet Columbia's account team on October 14. What's their"
            " monthly units trend in my categories over the last three"
            " months?",
        ),
    ),
    ScriptedSession(
        4,
        "lena.okafor",
        (
            "Show lead times in hours with one decimal from now on. Which of"
            " my DCs had the higher return rate last month?",
        ),
    ),
    ScriptedSession(
        4,
        "raj.patel",
        ("Give me the monthly net revenue trend for this fiscal year.",),
    ),
    # ---- Day 5, Wednesday ----
    ScriptedSession(5, "maya.chen", ("How did my categories do yesterday?",)),
    ScriptedSession(
        5,
        "raj.patel",
        ("What numbers do I need for the board meeting?",),
        compare=True,
    ),
    ScriptedSession(
        5,
        "lena.okafor",
        (
            "How many items did my DCs ship yesterday, and what was the"
            " average lead time?",
        ),
    ),
    ScriptedSession(
        5,
        "diego.alvarez",
        ("How are my markets trending?",),
        compare=True,
    ),
    ScriptedSession(
        5,
        "priya.nair",
        ("What should I bring to the business review?",),
        compare=True,
    ),
    ScriptedSession(
        5,
        "tom.becker",
        ("Units yesterday for my categories and my watch brands?",),
        compare=True,
    ),
    ScriptedSession(
        5,
        "maya.chen",
        (
            "Prep me for the assortment review: the top three brands per"
            " category by net revenue last month, split by my price bands.",
        ),
    ),
)


def analyst(user_id: str) -> Analyst:
  return next(a for a in ANALYSTS if a.user_id == user_id)


def day(number: int) -> Day:
  return next(d for d in DAYS if d.number == number)


def numbered_sessions(run_tag: str) -> list[tuple[str, ScriptedSession]]:
  """Each scripted session with its session id, in run order."""
  counts: dict[tuple[int, str], int] = {}
  out = []
  for scripted in sorted(SESSIONS, key=lambda s: s.day):
    key = (scripted.day, scripted.user_id)
    counts[key] = counts.get(key, 0) + 1
    out.append((scripted.session_id(run_tag, counts[key]), scripted))
  return out
