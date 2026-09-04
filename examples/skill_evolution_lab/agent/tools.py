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

"""Tools for the skill-evolution lab's company-policy agent.

Two meaningful tools the agent must CHOOSE between:
- ``lookup_company_policy(topic)`` returns the authoritative figures for every HR
  topic (PTO, sick leave, remote work, expenses, benefits, holidays, bereavement,
  jury duty, EAP, flex time, tuition reimbursement, short-term disability).
- ``calculate_disability_pay(annual_salary, weeks_out)`` *computes* a personalized
  short-term-disability payout the lookup can't produce.
Plus ``get_current_date``.

The point of the demo is that the *tools already have the right answers* -- the
deliberately flawed V0 skill just tells the model not to call them. Evolution
rewrites the skill to be tool-first and to route to the right tool, and the same
tools then produce correct, grounded answers.
"""

import datetime
from zoneinfo import ZoneInfo

COMPANY_POLICIES = {
    "pto": {
        "days_per_year": 20,
        "accrual": "monthly",
        "rollover_max": 5,
        "details": (
            "Employees receive 20 days of PTO per year, accrued at about 1.67"
            " days per month. Unused PTO rolls over to the next year up to a"
            " maximum of 5 days. If you leave the company mid-year, unused"
            " accrued PTO is paid out in your final paycheck."
        ),
    },
    "sick_leave": {
        "days_per_year": 10,
        "rollover": False,
        "details": (
            "Employees receive 10 sick days per year. Sick leave does not roll"
            " over. A doctor's note is required for absences longer than 3"
            " consecutive days."
        ),
    },
    "remote_work": {
        "max_days_per_week": 3,
        "requires_approval": True,
        "details": (
            "Employees may work remotely up to 3 days per week with manager"
            " approval. Core collaboration hours are 10am-3pm in the employee's"
            " local timezone."
        ),
    },
    "expenses": {
        "meal_limit_daily": 75,
        "travel_approval_threshold": 500,
        "receipt_required_above": 25,
        "details": (
            "Business expenses must be submitted within 30 days. Meals are"
            " reimbursed up to $75/day during business travel. Travel expenses"
            " over $500 require pre-approval. Receipts are required for any"
            " expense over $25."
        ),
    },
    "benefits": {
        "health_insurance": "PPO and HMO; company covers 80% of premiums",
        "max_out_of_pocket": "$4,000 individual / $8,000 family (PPO in-network)",
        "hsa": "Company contributes $750/year individual, $1,500/year family",
        "dental": "Preventive fully covered, 80% for major procedures",
        "orthodontia": "50% coverage up to a $2,000 lifetime maximum",
        "vision": "Annual eye exam covered, $200 frame allowance every 2 years",
        "retirement": "401(k) with 4% company match, vested after 1 year",
        "parental_leave": "16 weeks primary caregiver, 8 weeks secondary",
        "details": (
            "Health insurance: PPO and HMO plans; the company covers 80% of"
            " premiums for the employee and 50% for dependents. Maximum"
            " out-of-pocket is $4,000 individual and $8,000 family (in-network,"
            " PPO). HSA: the company contributes $750/year for individual and"
            " $1,500/year for family coverage. Dental: preventive fully"
            " covered, 80% for major procedures; orthodontia at 50% up to a"
            " $2,000 lifetime maximum. Vision: annual eye exam covered, $200"
            " frame allowance every 2 years. 401(k): 4% company match, fully"
            " vested after 1 year. Parental leave: 16 weeks paid for primary"
            " caregiver, 8 weeks for secondary. Open enrollment runs every"
            " November; new hires enroll within 30 days."
        ),
    },
    "holidays": {
        "count": 11,
        "details": (
            "The company observes 11 paid holidays per year: New Year's Day,"
            " Martin Luther King Jr. Day, Presidents' Day, Memorial Day,"
            " Independence Day, Labor Day, the Wednesday and Thursday of"
            " Thanksgiving week, Christmas Eve, Christmas Day, and New Year's"
            " Eve. Juneteenth, Veterans Day, and Columbus Day are NOT company"
            " holidays."
        ),
    },
    "bereavement": {
        "immediate_family_days": 5,
        "extended_family_days": 3,
        "details": (
            "Bereavement leave is 5 paid days for an immediate family member"
            " (spouse or domestic partner, child, parent, or sibling) and 3"
            " paid days for an extended family member (grandparent,"
            " grandchild, or in-law)."
        ),
    },
    "jury_duty": {
        "paid": True,
        "details": (
            "Jury duty is fully paid for the entire duration of service with no"
            " day cap. Forward your jury summons to HR; any stipend you receive"
            " may be kept."
        ),
    },
    "eap": {
        "sessions_per_year": 8,
        "details": (
            "The Employee Assistance Program (EAP) offers free, confidential"
            " counseling with up to 8 sessions per issue per year, plus a 24/7"
            " hotline covering mental health, stress, legal, and financial"
            " matters."
        ),
    },
    "flex_time": {
        "requires_approval": True,
        "details": (
            "Flexible scheduling is available with manager approval: start any"
            " time between 7am and 10am as long as you cover the 10am-3pm core"
            " hours and work a full 8-hour day. Compressed weeks (e.g. four"
            " 10-hour days) are also allowed with manager approval."
        ),
    },
    "tuition_reimbursement": {
        "annual_max": 5250,
        "details": (
            "Tuition reimbursement covers up to $5,250 per calendar year for"
            " job-related courses or degree programs. Courses require manager"
            " pre-approval and a grade of B or better (or pass for pass/fail)."
        ),
    },
    "short_term_disability": {
        "income_replacement": "60% of salary",
        "max_weeks": 12,
        "details": (
            "Short-term disability covers 60% of salary for up to 12 weeks"
            " after a 7-day waiting period, for a qualifying medical condition."
            " The waiting period is unpaid time before benefits begin; it does"
            " NOT reduce the 12 payable weeks. A physician's certification is"
            " required."
        ),
    },
}


def lookup_company_policy(topic: str) -> dict:
  """Look up one company HR policy or benefit by topic.

  There is no hand-maintained synonym table: you choose the closest topic from
  the list below and map the user's everyday wording to it yourself (the model
  is good at this). The tool matches the topic key exactly (after normalizing
  case/spaces) or by substring, and otherwise returns the valid topics so you
  can retry.

  Args:
    topic: The policy topic to retrieve, one of: pto, sick_leave, remote_work,
      expenses, benefits, holidays, bereavement, jury_duty, eap, flex_time,
      tuition_reimbursement, short_term_disability.

  Returns:
    The policy details for that topic, or an ``error`` field listing the valid
    topics when nothing matches.
  """
  key = topic.lower().strip().replace(" ", "_").replace("-", "_")
  if key in COMPANY_POLICIES:
    return COMPANY_POLICIES[key]
  for candidate_key, value in COMPANY_POLICIES.items():
    if key in candidate_key or candidate_key in key:
      return value
  return {
      "error": (
          f"'{topic}' is not a known topic. Choose one of: "
          + ", ".join(sorted(COMPANY_POLICIES))
      )
  }


# Short-term-disability policy parameters (used by the calculator below).
_STD_INCOME_REPLACEMENT = 0.60  # STD replaces 60% of salary
_STD_MAX_WEEKS = 12
_STD_WAITING_DAYS = 7


def calculate_disability_pay(annual_salary: float, weeks_out: int) -> dict:
  """Compute short-term-disability (STD) pay for a given salary and leave length.

  This is a CALCULATION, not a lookup: STD replaces 60% of salary for up to 12
  weeks after a 7-day waiting period, so the dollar payout depends on the
  employee's salary and how many weeks they are out. The 7-day waiting period
  is unpaid time before benefits begin -- it does NOT reduce the number of
  payable weeks, so ``weeks_out`` counts benefit weeks and every covered week
  is paid. Use this whenever the user asks "how much would short-term
  disability pay me" with a specific salary and/or duration --
  ``lookup_company_policy`` only returns the 60%/12-week policy, never the
  dollar amount.

  Args:
    annual_salary: The employee's gross annual salary in dollars.
    weeks_out: Number of benefit weeks the employee expects to be on
      disability (after the unpaid 7-day waiting period).

  Returns:
    A dict with the weekly and total STD benefit, the covered weeks (capped at
    the 12-week maximum), and the policy parameters used.
  """
  covered_weeks = min(weeks_out, _STD_MAX_WEEKS)
  weekly_benefit = (annual_salary / 52.0) * _STD_INCOME_REPLACEMENT
  return {
      "annual_salary": annual_salary,
      "weeks_requested": weeks_out,
      "covered_weeks": covered_weeks,
      "capped_at_max": weeks_out > _STD_MAX_WEEKS,
      "weekly_benefit": round(weekly_benefit, 2),
      "total_benefit": round(weekly_benefit * covered_weeks, 2),
      "income_replacement": _STD_INCOME_REPLACEMENT,
      "waiting_period_days": _STD_WAITING_DAYS,
      "waiting_period_note": (
          f"The {_STD_WAITING_DAYS}-day waiting period is unpaid time before"
          " benefits begin; it does not reduce the payable weeks above."
      ),
  }


def get_current_date() -> str:
  """Get the current date and day of the week (US Pacific time).

  Returns:
    A string with today's date and day name.
  """
  now = datetime.datetime.now(tz=ZoneInfo("America/Los_Angeles"))
  return f"Today is {now.strftime('%A, %B %d, %Y')} (Pacific Time)"


# Single tool list, reused by the agent factory and any callers. Two tools the
# agent must CHOOSE between: lookup_company_policy (facts) vs
# calculate_disability_pay (a computed, personalized figure).
AGENT_TOOLS = [
    lookup_company_policy,
    calculate_disability_pay,
    get_current_date,
]
