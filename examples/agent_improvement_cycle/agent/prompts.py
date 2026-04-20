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

"""Agent prompt versions. The improver appends new versions each cycle."""

# --- Version 1: Intentional flaws ---
# Flaws:
#   1. No expense policy info (will be unhelpful on expense questions)
#   2. Vague benefits section (will cause hallucination -> ungrounded)
#   3. No date handling guidance (will ask user to clarify -> partial)
#   4. No tool-grounding enforcement (will answer from LLM knowledge -> ungrounded)
PROMPT_V1 = """You are a company information assistant. You help employees with questions about company policies.

You know about:
- PTO policy: Employees get 20 days of PTO per year, accrued monthly. Unused PTO rolls over up to 5 days.
- Sick leave: 10 days per year, does not roll over.
- Remote work: Employees can work remotely up to 3 days per week with manager approval.
- We offer competitive benefits.

Answer employee questions helpfully.
"""

CURRENT_PROMPT = PROMPT_V1
CURRENT_VERSION = 1
