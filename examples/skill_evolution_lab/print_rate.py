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

"""Print the golden-matched meaningful rate from a quality report (one line)."""

import json
import sys

with open(sys.argv[1]) as f:
  g = json.load(f)["summary"].get("golden_eval_summary", {})
print(
    f"{g.get('matched_meaningful_rate')}% "
    f"({g.get('matched_meaningful')}/{g.get('matched')} golden-matched)"
)
