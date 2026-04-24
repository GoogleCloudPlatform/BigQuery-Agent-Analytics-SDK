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

"""Tools for the company info agent."""

import datetime
from zoneinfo import ZoneInfo

COMPANY_POLICIES = {
    "pto": {
        "days_per_year": 20,
        "accrual": "monthly",
        "rollover_max": 5,
        "details": (
            "Employees receive 20 days of PTO per year, accrued at approximately "
            "1.67 days per month. Unused PTO rolls over to the next year up to a "
            "maximum of 5 days. PTO requests must be submitted at least 2 weeks in "
            "advance for periods longer than 3 days."
        ),
    },
    "sick_leave": {
        "days_per_year": 10,
        "rollover": False,
        "details": (
            "Employees receive 10 sick days per year. Sick leave does not roll over. "
            "A doctor's note is required for absences longer than 3 consecutive days."
        ),
    },
    "remote_work": {
        "max_days_per_week": 3,
        "requires_approval": True,
        "details": (
            "Employees may work remotely up to 3 days per week with manager approval. "
            "Core collaboration hours are 10am-3pm in the employee's local timezone. "
            "Remote work arrangements must be documented in the HR system."
        ),
    },
    "expenses": {
        "meal_limit_daily": 75,
        "travel_approval_threshold": 500,
        "receipt_required_above": 25,
        "details": (
            "Business expenses must be submitted within 30 days. Meals are reimbursed "
            "up to $75/day during business travel. Travel expenses over $500 require "
            "pre-approval from your manager. Receipts are required for any expense "
            "over $25. Use the company expense portal at expenses.company.com."
        ),
    },
    "benefits": {
        "health_insurance": "PPO and HMO options, company covers 80% of premiums",
        "dental": "Full coverage for preventive care, 80% for major procedures",
        "vision": "Annual eye exam covered, $200 frame allowance every 2 years",
        "retirement": "401(k) with 4% company match, vested after 1 year",
        "parental_leave": "16 weeks paid for primary caregiver, 8 weeks for secondary",
        "details": (
            "Health insurance: PPO and HMO plans available, company covers 80% of "
            "premiums for employee and 50% for dependents. Dental: preventive care "
            "fully covered, 80% coverage for major procedures. Vision: annual eye "
            "exam covered, $200 frame allowance every 2 years. 401(k): 4% company "
            "match, fully vested after 1 year of employment. Parental leave: 16 "
            "weeks paid for primary caregiver, 8 weeks for secondary caregiver."
        ),
    },
    "holidays": {
        "2025": [
            "2025-01-01",
            "2025-01-20",
            "2025-02-17",
            "2025-05-26",
            "2025-07-04",
            "2025-09-01",
            "2025-11-27",
            "2025-11-28",
            "2025-12-24",
            "2025-12-25",
            "2025-12-31",
        ],
        "2026": [
            "2026-01-01",
            "2026-01-19",
            "2026-02-16",
            "2026-05-25",
            "2026-07-03",
            "2026-09-07",
            "2026-11-26",
            "2026-11-27",
            "2026-12-24",
            "2026-12-25",
            "2026-12-31",
        ],
        "details": "The company observes 11 paid holidays per year.",
    },
}


def lookup_company_policy(topic: str) -> dict:
  """Look up a company policy by topic.

  Args:
      topic: The policy topic to look up. One of: pto, sick_leave,
             remote_work, expenses, benefits, holidays.

  Returns:
      A dictionary with policy details for the requested topic,
      or an error message if the topic is not found.
  """
  topic_key = topic.lower().replace(" ", "_").replace("-", "_")

  # Try exact match first
  if topic_key in COMPANY_POLICIES:
    return COMPANY_POLICIES[topic_key]

  # Try partial match
  for key, value in COMPANY_POLICIES.items():
    if topic_key in key or key in topic_key:
      return value

  available = ", ".join(COMPANY_POLICIES.keys())
  return {
      "error": f"Policy topic '{topic}' not found. Available topics: {available}"
  }


def get_current_date() -> str:
  """Get the current date and day of the week.

  Returns:
      A string with today's date and day name.
  """
  now = datetime.datetime.now(tz=ZoneInfo("America/Los_Angeles"))
  return f"Today is {now.strftime('%A, %B %d, %Y')} (Pacific Time)"
