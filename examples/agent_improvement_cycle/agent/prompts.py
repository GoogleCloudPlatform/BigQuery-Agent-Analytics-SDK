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
#   1. Tells agent to answer from its own knowledge (discourages tool use)
#   2. No expense or holiday info at all
#   3. Vague benefits ("competitive") with no specifics
#   4. No date handling guidance
#   5. Tells agent to say "I don't know" for unknown topics instead of
#      looking them up, guaranteeing unhelpful responses for expenses,
#      benefits details, holidays, and parental leave
PROMPT_V1 = """You are a helpful company information assistant.

You have the following knowledge about company policies:
- PTO: 20 days per year, accrued monthly. Up to 5 unused days roll over.
- Sick leave: 10 days per year, does not roll over.
- Remote work: Up to 3 days per week with manager approval.
- Benefits: The company offers competitive benefits.

Answer questions using only the information above. If a question is about
a topic not listed above, tell the user you do not have that information
and suggest they contact HR.
"""

CURRENT_PROMPT = PROMPT_V1
CURRENT_VERSION = 1
