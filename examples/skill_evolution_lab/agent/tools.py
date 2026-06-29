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

A single ``lookup_company_policy`` tool returns the authoritative figures for
every HR topic (PTO, sick leave, remote work, expenses, benefits, holidays,
bereavement, jury duty, EAP, flex time, tuition reimbursement, short-term
disability). ``get_current_date`` returns today's date.

The point of the demo is that the *tool already has the right answers* -- the
deliberately flawed V0 skill just tells the model not to call it. Evolution
rewrites the skill to be tool-first, and the same tool then produces correct,
grounded answers.
"""

import datetime
from zoneinfo import ZoneInfo

# Synonyms callers use, mapped to canonical COMPANY_POLICIES keys.
_TOPIC_ALIASES = {
    "vacation": "pto",
    "annual_leave": "pto",
    "telecommuting": "remote_work",
    "work_from_home": "remote_work",
    "wfh": "remote_work",
    "reimbursement": "expenses",
    "meals": "expenses",
    "per_diem": "expenses",
    "health_insurance": "benefits",
    "insurance": "benefits",
    "medical": "benefits",
    "dental": "benefits",
    "vision": "benefits",
    "hsa": "benefits",
    "orthodontia": "benefits",
    "401k": "benefits",
    "retirement": "benefits",
    "pension": "benefits",
    "parental_leave": "benefits",
    "maternity_leave": "benefits",
    "paternity_leave": "benefits",
    "adoption_leave": "benefits",
    "enrollment": "benefits",
    "open_enrollment": "benefits",
    "employee_assistance_program": "eap",
    "counseling": "eap",
    "therapy": "eap",
    "jury": "jury_duty",
    "flextime": "flex_time",
    "flexible_schedule": "flex_time",
    "compressed_week": "flex_time",
    "tuition": "tuition_reimbursement",
    "education": "tuition_reimbursement",
    "disability": "short_term_disability",
    "std": "short_term_disability",
}

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
            " A physician's certification is required."
        ),
    },
}


def _resolve_topic(topic: str):
  """Resolve a free-text topic to a canonical (key, value) pair.

  Applies the synonym alias map, then an exact match, then a fuzzy substring
  match. Returns ``(None, None)`` if nothing matches.
  """
  topic_key = topic.lower().strip().replace(" ", "_").replace("-", "_")
  topic_key = _TOPIC_ALIASES.get(topic_key, topic_key)
  if topic_key in COMPANY_POLICIES:
    return topic_key, COMPANY_POLICIES[topic_key]
  for key, value in COMPANY_POLICIES.items():
    if topic_key in key or key in topic_key:
      return key, value
  return None, None


def lookup_company_policy(topic: str) -> dict:
  """Look up the authoritative figures for a company HR policy or benefit.

  Args:
    topic: The HR topic to look up. One of: pto, sick_leave, remote_work,
      expenses, benefits, holidays, bereavement, jury_duty, eap, flex_time,
      tuition_reimbursement, short_term_disability. Common synonyms (vacation,
      wfh, 401k, insurance, parental_leave, tuition, jury, ...) are accepted.

  Returns:
    A dict with the policy details, or an ``error`` field listing the valid
    topics if nothing matched.
  """
  topic_key, result = _resolve_topic(topic)
  if result is None:
    return {
        "error": (
            f"'{topic}' did not match a known policy. Valid topics: "
            + ", ".join(sorted(COMPANY_POLICIES))
        )
    }
  return result


def get_current_date() -> str:
  """Get the current date and day of the week (US Pacific time).

  Returns:
    A string with today's date and day name.
  """
  now = datetime.datetime.now(tz=ZoneInfo("America/Los_Angeles"))
  return f"Today is {now.strftime('%A, %B %d, %Y')} (Pacific Time)"


# Single tool list, reused by the agent factory and any callers.
AGENT_TOOLS = [lookup_company_policy, get_current_date]
