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

"""Print the golden-matched meaningful rate from a quality report (one line).

The denominator is the *in-scope* subset that matched the answer key -- NOT the
full question set. Out-of-scope questions have no golden entry, so they are not
counted here (they are scored separately as declines). We spell that out so
"14/70" is never mistaken for a 70-question test.

Out-of-scope is counted by the ``oos_`` session-id prefix (the same convention
``compare_runs.py`` uses), NOT by assuming every unmatched session is
out-of-scope: an in-scope question that fails the golden match is printed as
"unmatched" so a matching failure is visible instead of silently dropped.

``--require-execution-mode MODE`` additionally exits 1 unless the report's
top-level ``details.execution_mode`` equals MODE. The demo passes
``ai_generate`` right after every score: a whole-pass fall-back to the Gemini
API judge would otherwise exit 0 and let the run present itself as
server-side through the promotion gate (per-session ``api_retry`` inside an
``ai_generate`` pass is still allowed and disclosed in the session records).
"""

import argparse
import json
import sys

_parser = argparse.ArgumentParser()
_parser.add_argument("report")
_parser.add_argument("--require-execution-mode", default=None)
_args = _parser.parse_args()

with open(_args.report) as f:
  report = json.load(f)

if _args.require_execution_mode:
  observed = (report.get("details") or {}).get("execution_mode")
  if observed != _args.require_execution_mode:
    print(
        f"FATAL: execution_mode={observed!r} (required:"
        f" {_args.require_execution_mode!r}) -- the pass was not judged"
        " server-side; refusing to let it through the gate.",
        file=sys.stderr,
    )
    sys.exit(1)

g = report["summary"].get("golden_eval_summary", {})

rate = g.get("matched_meaningful_rate")
matched_meaningful = g.get("matched_meaningful")
matched = g.get("matched")
total = g.get("total_sessions")

# Split the unmatched sessions: oos_ ids are by-design golden-less; anything
# else is an in-scope question the matcher failed to pair with the answer key.
unmatched_ids = [
    s.get("session_id", "")
    for s in report.get("sessions", [])
    if not (s.get("golden_eval") or {}).get("matched")
]
out_of_scope = sum(1 for sid in unmatched_ids if sid.startswith("oos_"))
unmatched_other = len(unmatched_ids) - out_of_scope

line = f"{rate}% ({matched_meaningful}/{matched} matched to the answer key"
if total is not None:
  line += f", of {total} total"
if out_of_scope:
  line += f"; {out_of_scope} out-of-scope"
if unmatched_other:
  line += f"; {unmatched_other} unmatched"
line += ")"
print(line)
