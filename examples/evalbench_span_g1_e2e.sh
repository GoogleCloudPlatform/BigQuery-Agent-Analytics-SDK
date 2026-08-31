#!/usr/bin/env bash
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

# Team demo: span-level G1 on the NATIVE path (#466, parent #435).
#
# This is a presenter script. Run it in front of the team and read the
# banners aloud: a real customer asked a support agent how many widgets
# are in stock, the agent went silent, session-level failed_sessions + G1
# names the failure (the denominator, unchanged), and the PR #467
# span_taxonomy library localizes every frozen category onto the real
# native span where the trace died -- span_id b7ad6b7169203331,
# target_kind gap_after_span. A teammate should understand the failure in
# 60-90 seconds of reading the output, then see WHICH SPAN to inspect.
#
# Same real session as the merged MVP e2e demo
# (examples/evalbench_mvp_e2e.sh) and the native freeze demo of PR #465:
# support_agent, session 7e352c34, "How many widgets are in stock?",
# never answered. What is new here is act 4: the localized span rows of
# `bigquery_agent_analytics.span_taxonomy` (PR #467) -- a pure library
# (`label_failed_session_spans` / `label_native_run`), deliberately no
# new CLI. Span labels localize; they never classify or replace the
# session-level denominator. No EvalBench configs/results/scores tables
# are read anywhere in the path; the evalbench-import adapter (#97) stays
# as an optional on-ramp and is never called here.
#
# The six-week clock has NOT started; it starts only when the first
# Week 1 snapshot job is kicked.
#
# This demo is --fixture only: no BigQuery, no --synth, no live CLI, no
# judge calls, no new CLI wrapped around span_taxonomy. Sample identities
# match the PR #467 span taxonomy tests (source
# test-project-0728-467323.bqaa_e2e_real.agent_events, native job
# mvp-e2e-real-traces, import_version v1); trace/span ids are the fixture
# stand-ins those tests pin, drawn FROM agent_events rows -- the
# attribution layer never invents a span identifier.
#
# Usage:
#   bash examples/evalbench_span_g1_e2e.sh --fixture
#
# Speaker notes: examples/evalbench_span_g1_e2e.md

set -euo pipefail

# The frozen argv contract: exactly one argument, equal to --fixture.
# Everything else -- no arguments, -h/--help, --synth, repeated flags --
# is rejected with one line on stderr and exit 2; the EVALBENCH_FIXTURE
# environment variable is deliberately not read.
if [[ $# -eq 1 && "$1" == "--fixture" ]]; then
  : # the only successful invocation
elif [[ $# -eq 1 ]]; then
  echo "evalbench_span_g1_e2e.sh: unknown argument '$1'." \
    "This demo is --fixture only -- no BigQuery, no --synth, no live" \
    "mode; the six-week clock has not started. Run:" \
    "bash examples/evalbench_span_g1_e2e.sh --fixture" >&2
  exit 2
else
  echo "evalbench_span_g1_e2e.sh: this demo is --fixture only, taking" \
    "exactly that one argument (no BigQuery, no live mode; the clock has" \
    "not started). Run: bash examples/evalbench_span_g1_e2e.sh --fixture" >&2
  exit 2
fi

banner() {
  echo
  echo "=== $* ==="
}

note() {
  echo "  # $*"
}

# Sample identities only; nothing below is read from or written to BigQuery.
# These match the PR #467 span taxonomy tests (which extend the PR #464
# native writer fixture with the native trace/span columns); the session
# facts are real and are not renamed by environment variables.
source_table="test-project-0728-467323.bqaa_e2e_real.agent_events"
job_id="mvp-e2e-real-traces"
version="v1"
# The protagonist: the same real trace as the merged MVP e2e demo.
scenario_id="7e352c34"
session_id="7e352c34-4c1c-4395-acd5-fb3c8f215346"
import_identity="evalbench-native-import:${job_id}:${version}:${scenario_id}"
# The localized anchor: the REAL last span of the failed trace, in the
# OTel hex shape the ADK plugin writes (fixture stand-in values pinned by
# tests/test_span_taxonomy.py -- always drawn from the rows, never made up).
trace_id="6ad3f30c47a2bfd1f1f6f2c1c19f6d2e"
span_id="b7ad6b7169203331"
span_ts="2026-07-27T20:30:41+00:00"

echo "EvalBench span-G1 e2e (fixture mode): job ${job_id}"
note "Team walkthrough: session-level G1 names one real failed session;"
note "span-level G1 (PR #467) says WHICH SPAN died. Offline; no BigQuery"
note "call is made in fixture mode. Clock not started."

# ---- Act 1: the customer -------------------------------------------------- #
banner "The customer asked. The agent went silent."
echo "  agent:         support_agent"
echo "  system prompt: \"You are a terse support agent. Use tools when asked"
echo "                 about inventory or tickets. Keep answers to one sentence.\""
echo "  user:          real-user-0"
echo "  asked:         How many widgets are in stock?"
echo "  session_id:    ${session_id}"
echo "  eval_id:       ${scenario_id}   (first 8 chars of session_id)"
note "This is a support ticket that went unanswered. No jargon yet."

# ---- Act 2: the trace ------------------------------------------------------ #
banner "What the trace shows"
echo "  The events live in production agent_events -- the source of truth:"
echo "  ${source_table}"
echo
echo "  USER_MESSAGE_RECEIVED -> INVOCATION_STARTING -> AGENT_STARTING"
echo "  ... then silence. Nothing else. No response ever reached the user."
echo
echo "  The last span that EXISTS is the AGENT_STARTING span:"
echo "    span_id ${span_id}  (trace ${trace_id})"
echo "  After it: nothing. What never happened: no check_inventory tool"
echo "  call, no LLM_RESPONSE, no AGENT_COMPLETED."
echo
echo "  Sibling session ab7535a5 asked the same question and answered"
echo "  \"There are 0 widgets in stock.\" -- so the agent CAN do this; this"
echo "  session just never did."
note "This is the human problem: a stock question with no answer. Remember"
note "that span id -- the whole demo lands on it."

# ---- Act 3: session-level denominator -------------------------------------- #
banner "Session-level G1 names the failure -- the denominator, unchanged"
note "Already-landed context, not the new act: the PR #464 native writer"
note "(bq-agent-sdk evalbench-native-import) snapshotted this trace from"
note "agent_events as job ${job_id}, import_version ${version} --"
note "no EvalBench configs/results/scores tables anywhere in the path."
note "failed_sessions found 1 of 7 sessions failed -- ours:"
cat <<JSON
{
  "session_id": "${session_id}",
  "scenario_id": "${scenario_id}",
  "process_failed": true,
  "missing_completion": true,
  "score_failed": true,
  "failed": true,
  "failing_scores": {"goal_completion": 0.0},
  "taxonomy_categories": ["task/planning", "finalization", "tool blockers"]
}
JSON
note "import identity: ${import_identity}"
note "(feature:job_id:import_version:eval_id -- one string a teammate can"
note "paste into a ticket; the rows keep the REAL ADK session_id.)"
note "SAY THIS LOUD: this session row is STILL the denominator. Span"
note "labels localize; they never classify a session and never replace"
note "failed_sessions + G1. Taxonomy frozen at v0.1.0; frozen order:"
note "task/planning, finalization, tool blockers."

# ---- Act 4: span localization ---------------------------------------------- #
banner "Span-level G1 localizes it -- which span died (PR #467)"
note "New in PR #467: bigquery_agent_analytics.span_taxonomy. A pure"
note "library, deliberately NO new CLI -- you call it from Python:"
echo
echo "  >>> from bigquery_agent_analytics.span_taxonomy import label_native_run"
echo "  >>> labels = label_native_run(run)   # run: the PR #464 NativeAgentEventsRun"
echo "  >>> # or per session: label_failed_session_spans(events, verdict, ...)"
echo
note "Sample output for this session (three SpanFailureLabel rows, one per"
note "tripped frozen category, in frozen order):"
cat <<JSON
[
  {
    "session_id": "${session_id}",
    "eval_id": "${scenario_id}",
    "trace_id": "${trace_id}",
    "span_id": "${span_id}",
    "failure_category": "task/planning",
    "evidence": "score_failed: the session's score gate failed (goal_completion=0.0); no further plan step follows AGENT_STARTING span ${span_id} at ${span_ts}",
    "confidence": 1.0,
    "target_kind": "gap_after_span"
  },
  {
    "session_id": "${session_id}",
    "eval_id": "${scenario_id}",
    "trace_id": "${trace_id}",
    "span_id": "${span_id}",
    "failure_category": "finalization",
    "evidence": "missing_completion: no AGENT_COMPLETED event follows AGENT_STARTING span ${span_id} at ${span_ts}; the trace goes silent after AGENT_STARTING span ${span_id} at ${span_ts}: no subsequent agent_events row exists",
    "confidence": 1.0,
    "target_kind": "gap_after_span"
  },
  {
    "session_id": "${session_id}",
    "eval_id": "${scenario_id}",
    "trace_id": "${trace_id}",
    "span_id": "${span_id}",
    "failure_category": "tool blockers",
    "evidence": "process_failed: no TOOL_STARTING event follows AGENT_STARTING span ${span_id} at ${span_ts} and check_inventory was never called (the completed sibling called it); the trace goes silent after AGENT_STARTING span ${span_id} at ${span_ts}: no subsequent agent_events row exists",
    "confidence": 1.0,
    "target_kind": "gap_after_span"
  }
]
JSON
note "All three frozen categories localize to the SAME real native span:"
note "AGENT_STARTING ${span_id}, target_kind gap_after_span -- a gap"
note "marker anchored to a real span. No synthetic span identifiers: every"
note "span_id is drawn from an agent_events row; a row without one fails"
note "closed rather than inventing an id."
note "RFC #435 Phase 2 tuple via SpanFailureLabel.as_tuple():"
note "(trace_id, span_id, failure_category, evidence, confidence)."
note "confidence 1.0 is MECHANICAL_CONFIDENCE: each evidence string is a"
note "checkable fact of the event stream, not a judged probability."
note "SAY THIS LOUD: the AGENT_STARTING -> silence punchline is now an"
note "inspectable localized row on a real native span_id."

# ---- Act 5: the judge ------------------------------------------------------ #
banner "A live judge would still miss this"
note "The same trap as the freeze demos, kept short: the correctness judge"
note "scored this unanswered session..."
cat <<JSON
{
  "session_id": "${session_id}",
  "scores": {"correctness": 1.0},
  "passed": true,
  "llm_feedback": null,
  "pass_rate": 1.0
}
JSON
note "correctness 1.0, llm_feedback null, pass_rate 1.0 over the 7 pinned"
note "sessions -- because there was nothing to judge. failed_sessions, not"
note "the judge, is the denominator. And now come BACK to the span row"
note "above: THAT is the thing a teammate can paste into a ticket --"
note "category, evidence, and the exact span to open."

# ---- Act 6: punchline ------------------------------------------------------ #
banner "Punchline"
echo "This widget-stock session failed because the agent never answered (goal_completion=0.0). Session-level G1 still names it task/planning, tool blockers, and finalization. Span-level G1 localizes all three to AGENT_STARTING span ${span_id} (gap_after_span) — it died before check_inventory was ever called. Next debugging action: inspect that span."
echo "We did not need EvalBench tables. Native agent_events + span_taxonomy was enough."
note "Span taxonomy library: issue #466, PR #467. Native writer: PR #464."
note "D4 fail-closed: Hai-Yuan Cao is the only named report consumer; a"
note "fixture cannot produce a funding rec. Clock not started."
exit 0
