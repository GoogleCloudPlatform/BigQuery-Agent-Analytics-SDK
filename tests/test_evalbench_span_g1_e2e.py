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
"""Tests for ``examples/evalbench_span_g1_e2e.sh`` (#466, parent #435).

The span-G1 e2e is a presenter-facing team demo, ``--fixture`` only:
nothing here reaches BigQuery. It tells the widget-stock failed session
as a six-act story whose load-bearing new act consumes the PR #467
``span_taxonomy`` library — the session-level ``failed_sessions`` + G1
denominator stays unchanged, and every tripped frozen category is
localized onto the real native ``AGENT_STARTING`` span
``b7ad6b7169203331`` with ``target_kind="gap_after_span"``. Same real
session as the merged MVP e2e demo and the native freeze demo (PR #465).
No new CLI exists or is invoked; the six-week clock has NOT started.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest

from bigquery_agent_analytics import failure_taxonomy
from bigquery_agent_analytics import span_taxonomy
from bigquery_agent_analytics.evalbench import EvalScorePolicy
from bigquery_agent_analytics.span_taxonomy import label_native_run
from tests.test_native_events_writer import _POLICY
from tests.test_native_events_writer import _SESSION_STUCK
from tests.test_span_taxonomy import _acceptance_run
from tests.test_span_taxonomy import _AGENT_STARTING_SPAN
from tests.test_span_taxonomy import _TRACE_STUCK

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / "examples" / "evalbench_span_g1_e2e.sh"
_NOTES = _REPO_ROOT / "examples" / "evalbench_span_g1_e2e.md"

_SOURCE_TABLE = "test-project-0728-467323.bqaa_e2e_real.agent_events"
_SESSION_ID = "7e352c34-4c1c-4395-acd5-fb3c8f215346"
_EVAL_ID = "7e352c34"
_IMPORT_IDENTITY = f"evalbench-native-import:mvp-e2e-real-traces:v1:{_EVAL_ID}"

# The six-act story a presenter reads aloud, in order: customer, trace,
# session-level denominator, span localization, judge, punchline.
_BANNERS = (
    "=== The customer asked. The agent went silent. ===",
    "=== What the trace shows ===",
    "=== Session-level G1 names the failure -- the denominator, unchanged ===",
    "=== Span-level G1 localizes it -- which span died (PR #467) ===",
    "=== A live judge would still miss this ===",
    "=== Punchline ===",
)

_PUNCHLINE = (
    "This widget-stock session failed because the agent never answered "
    "(goal_completion=0.0). Session-level G1 still names it task/planning, "
    "tool blockers, and finalization. Span-level G1 localizes all three to "
    "AGENT_STARTING span b7ad6b7169203331 (gap_after_span) — it died before "
    "check_inventory was ever called. Next debugging action: inspect that "
    "span."
)

_NATIVE_CLOSER = (
    "We did not need EvalBench tables. Native agent_events + span_taxonomy"
    " was enough."
)

_FROZEN_CATEGORIES_LINE = (
    '"taxonomy_categories": ["task/planning", "finalization", "tool blockers"]'
)

_RFC_TUPLE_LINE = "(trace_id, span_id, failure_category, evidence, confidence)"

# The exact runnable call act 4 teaches. It must carry the frozen
# goal_completion >= 1.0 score gate: the default EvalScorePolicy() has an
# empty min_scores, so a call without policy= would leave
# score_failed false and emit only two of the three printed rows. The
# fidelity test below re-runs this same policy-bearing invocation.
_TAUGHT_POLICY_LINE = '>>> policy = EvalScorePolicy({"goal_completion": 1.0})'
_TAUGHT_CALL_LINE = ">>> labels = label_native_run(run, policy=policy)"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None, reason="bash not available"
)


def _run(*args: str, env: dict[str, str] | None = None):
  # Start from a clean environment so a developer's exported
  # EVALBENCH_* / BQ_AGENT_* values cannot change what is asserted.
  clean = {
      k: v
      for k, v in os.environ.items()
      if not k.startswith(("EVALBENCH_", "BQ_AGENT_"))
  }
  return subprocess.run(
      ["bash", str(_SCRIPT), *args],
      capture_output=True,
      text=True,
      timeout=60,
      env={**clean, **(env or {})},
  )


def _assert_span_walkthrough(stdout: str) -> None:
  for banner in _BANNERS:
    assert banner in stdout
  # The six acts appear in story order.
  positions = [stdout.index(b) for b in _BANNERS]
  assert positions == sorted(positions)
  customer = stdout[positions[0] : positions[1]]
  trace = stdout[positions[1] : positions[2]]
  session_level = stdout[positions[2] : positions[3]]
  span_level = stdout[positions[3] : positions[4]]
  judged = stdout[positions[4] : positions[5]]
  punchline = stdout[positions[5] :]
  # Act 1: the customer and the session, no jargon.
  assert "support_agent" in customer
  assert "real-user-0" in customer
  assert "How many widgets are in stock?" in customer
  assert _SESSION_ID in customer
  assert f"eval_id:       {_EVAL_ID}" in customer
  assert "EvalBench" not in customer
  # Act 2: the trace names the REAL last span, then silence.
  assert _SOURCE_TABLE in trace
  assert "source of truth" in trace
  assert "USER_MESSAGE_RECEIVED" in trace
  assert "INVOCATION_STARTING" in trace
  assert "AGENT_STARTING" in trace
  assert f"span_id {_AGENT_STARTING_SPAN}" in trace
  assert _TRACE_STUCK in trace
  assert "silence" in trace
  assert "check_inventory" in trace
  assert "no LLM_RESPONSE" in trace
  assert "no AGENT_COMPLETED" in trace
  assert "ab7535a5" in trace
  assert "There are 0 widgets in stock." in trace
  # Act 3: session-level failed_sessions + G1, unchanged as denominator.
  assert '"process_failed": true' in session_level
  assert '"missing_completion": true' in session_level
  assert '"score_failed": true' in session_level
  assert '"failing_scores": {"goal_completion": 0.0}' in session_level
  assert _FROZEN_CATEGORIES_LINE in session_level
  assert "1 of 7" in session_level
  assert _IMPORT_IDENTITY in session_level
  assert "STILL the denominator" in session_level
  assert "never replace" in session_level
  assert "v0.1.0" in session_level
  # Act 4: the PR #467 library, its three localized rows, the RFC tuple.
  assert "span_taxonomy" in span_level
  assert "label_native_run" in span_level
  assert "label_failed_session_spans" in span_level
  # The taught call carries the frozen score policy; a no-policy call
  # (which could not produce the three printed rows) is never
  # demonstrated: every call site printed to stdout carries policy=.
  assert "EvalScorePolicy" in span_level
  assert _TAUGHT_POLICY_LINE in span_level
  assert _TAUGHT_CALL_LINE in span_level
  for line in stdout.splitlines():
    if "label_native_run(" in line:
      assert "policy=" in line, line
  assert "SpanFailureLabel" in span_level
  assert '"failure_category": "task/planning"' in span_level
  assert '"failure_category": "finalization"' in span_level
  assert '"failure_category": "tool blockers"' in span_level
  assert span_level.count(f'"span_id": "{_AGENT_STARTING_SPAN}"') == 3
  assert span_level.count('"target_kind": "gap_after_span"') == 3
  assert '"confidence": 1.0' in span_level
  assert "as_tuple()" in span_level
  assert _RFC_TUPLE_LINE in span_level
  assert "No synthetic span identifiers" in span_level
  assert "inspectable localized row on a real native span_id" in span_level
  # Act 5: the judge trap stays short and hands back to the span row.
  assert '"correctness": 1.0' in judged
  assert '"llm_feedback": null' in judged
  assert '"pass_rate": 1.0' in judged
  assert "denominator" in judged
  assert "span row" in judged
  # Act 6: punchline, then the native closer, then only the pointers.
  assert _PUNCHLINE in punchline
  assert _NATIVE_CLOSER in punchline
  assert punchline.index(_PUNCHLINE) < punchline.index(_NATIVE_CLOSER)
  assert "#466" in punchline
  assert "PR #467" in punchline
  assert "Hai-Yuan Cao" in punchline
  assert "funding rec" in punchline
  # No non-native evalbench-import command is run or displayed as run:
  # the only occurrences of that string are inside the native command
  # name / import identity prefix "evalbench-native-import".
  assert "evalbench-native-import" in stdout
  assert "evalbench-import" not in stdout.replace("evalbench-native-import", "")
  assert "--evalbench-dataset" not in stdout
  assert ".evalbench." not in stdout
  # Nowhere does the demo invent people, the example partner, a new CLI
  # for span labels, or a started clock.
  assert "Acme" not in stdout
  assert "bq-agent-sdk span" not in stdout
  assert "evalbench-span" not in stdout
  assert "clock has started" not in stdout
  assert "Clock not started" in stdout


def test_fixture_flag_tells_the_story_and_exits_zero() -> None:
  result = _run("--fixture")
  assert result.returncode == 0, result.stderr
  assert result.stderr == ""
  assert "fixture mode" in result.stdout
  assert _PUNCHLINE in result.stdout
  _assert_span_walkthrough(result.stdout)


def test_printed_labels_match_the_library_localization() -> None:
  # The act-4 JSON is sample output, but it must be TRUE sample output:
  # running the PR #467 library over the pinned widget-stock fixture
  # (offline, no BigQuery) yields exactly the printed span ids, trace id,
  # categories, target kinds, and evidence strings.
  result = _run("--fixture")
  assert result.returncode == 0, result.stderr
  # Run the invocation exactly as act 4 teaches it: build the taught
  # policy line's policy, confirm it IS the frozen writer-test gate, and
  # pass it the way the printed call does.
  assert _TAUGHT_POLICY_LINE in result.stdout
  assert _TAUGHT_CALL_LINE in result.stdout
  policy = EvalScorePolicy({"goal_completion": 1.0})
  assert policy == _POLICY
  labels = label_native_run(_acceptance_run(), policy=policy)
  assert len(labels) == 3
  assert [label.failure_category for label in labels] == [
      "task/planning",
      "finalization",
      "tool blockers",
  ]
  for label in labels:
    assert label.session_id == _SESSION_STUCK == _SESSION_ID
    assert label.eval_id == _EVAL_ID
    assert label.span_id == _AGENT_STARTING_SPAN
    assert label.trace_id == _TRACE_STUCK
    assert label.target_kind == span_taxonomy.TARGET_GAP_AFTER_SPAN
    assert label.confidence == span_taxonomy.MECHANICAL_CONFIDENCE
    assert label.evidence in result.stdout
    # The RFC tuple shape the demo narrates.
    assert label.as_tuple() == (
        label.trace_id,
        label.span_id,
        label.failure_category,
        label.evidence,
        label.confidence,
    )


def test_fixture_env_var_alone_is_rejected() -> None:
  # The demo is --fixture argv only. An inherited EVALBENCH_FIXTURE=1
  # with no arguments must not run the transcript.
  result = _run(env={"EVALBENCH_FIXTURE": "1"})
  assert result.returncode == 2
  assert "--fixture only" in result.stderr
  assert result.stdout == ""


def test_without_fixture_flag_exits_two_and_runs_nothing() -> None:
  result = _run()
  assert result.returncode == 2
  assert "--fixture only" in result.stderr
  assert result.stdout == ""


def test_synth_flag_is_rejected_without_running() -> None:
  # The span-G1 demo has no --synth mode; it is an unknown argument.
  result = _run("--synth")
  assert result.returncode == 2
  assert "--fixture only" in result.stderr
  assert result.stdout == ""


def test_unknown_argument_is_rejected() -> None:
  result = _run("--bogus")
  assert result.returncode == 2
  assert "unknown argument '--bogus'" in result.stderr
  assert result.stdout == ""


def test_help_flags_are_rejected_without_running() -> None:
  # The frozen argv contract: the only successful invocation is exactly
  # one argument equal to --fixture.
  for argv in (("--help",), ("-h",)):
    result = _run(*argv)
    assert result.returncode == 2, argv
    assert result.stdout == "", argv
    assert "--fixture only" in result.stderr, argv


def test_repeated_or_combined_flags_are_rejected() -> None:
  for argv in (
      ("--fixture", "--fixture"),
      ("--fixture", "--help"),
      ("--help", "--fixture"),
  ):
    result = _run(*argv)
    assert result.returncode == 2, argv
    assert result.stdout == "", argv
    assert "--fixture only" in result.stderr, argv


def test_script_never_invokes_live_tools() -> None:
  # Every transcript line is echoed sample output; no code line shells out
  # to bq, gcloud, python, or any CLI.
  code_lines = [
      line
      for line in _SCRIPT.read_text().splitlines()
      if not line.lstrip().startswith("#")
  ]
  for forbidden in ("bq ", "gcloud", "python", "curl ", "wget "):
    assert not any(
        line.lstrip().startswith(forbidden) for line in code_lines
    ), forbidden
  # The CLI name only ever appears inside comments or note/echo strings;
  # the span library has no CLI and none is invented here.
  for line in code_lines:
    if "bq-agent-sdk" in line:
      assert line.lstrip().startswith(("echo", "note")), line


def test_speaker_notes_cover_the_span_path() -> None:
  assert _NOTES.exists()
  notes = _NOTES.read_text()
  assert "pull/467" in notes
  assert "span_taxonomy" in notes
  assert "label_failed_session_spans" in notes
  assert "label_native_run" in notes
  # The notes mirror the transcript's policy-bearing call verbatim.
  assert 'policy = EvalScorePolicy({"goal_completion": 1.0})' in notes
  assert "labels = label_native_run(run, policy=policy)" in notes
  assert "span_id" in notes
  assert _AGENT_STARTING_SPAN in notes
  assert "gap_after_span" in notes
  assert _SESSION_ID in notes
  assert _IMPORT_IDENTITY in notes
  assert "clock has NOT started" in notes
  assert "clock has started" not in notes
  # The notes point back at the runnable script.
  assert "examples/evalbench_span_g1_e2e.sh --fixture" in notes


def test_session_level_taxonomy_stays_the_frozen_denominator() -> None:
  # The freeze the demo narrates is real, in production code, and the
  # span layer can only emit the three mechanically emittable names.
  assert failure_taxonomy.TAXONOMY_VERSION == "0.1.0"
  config = failure_taxonomy.taxonomy_config()
  assert config["g1_frozen"] is True
  assert dict(failure_taxonomy.FLAG_TO_CATEGORY) == {
      "missing_completion": "finalization",
      "process_failed": "tool blockers",
      "score_failed": "task/planning",
  }
  assert span_taxonomy.SPAN_CATEGORY_NAMES == (
      "task/planning",
      "finalization",
      "tool blockers",
  )
