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
"""Tests for ``examples/evalbench_week0_freeze_e2e.sh`` (#435).

The post-freeze Week 0 e2e is a presenter-facing team demo, ``--fixture``
only: nothing here reaches BigQuery. It tells the widget-stock failed
session as a six-act story -- the customer asked, the agent went silent,
evalbench-import + failed_sessions find it, the frozen G1 taxonomy names
it, and a punchline states the next debugging action. Same real session
as the merged MVP e2e demo; distinct from the EXAMPLE Acme pack
(``examples/evalbench_week0_full_idea.sh``), which stays illustrative.
The six-week clock has NOT started.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from bigquery_agent_analytics import failure_taxonomy

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / "examples" / "evalbench_week0_freeze_e2e.sh"
_EXAMPLE_PACK = _REPO_ROOT / "examples" / "evalbench_week0_full_idea.sh"
_FIXTURE_DIR = _REPO_ROOT / "examples" / "fixtures"

_SESSION_ID = "7e352c34-4c1c-4395-acd5-fb3c8f215346"
_EVAL_ID = "7e352c34"

# The six-act story a presenter reads aloud, in order: customer, trace,
# import, failed_sessions, judge, punchline.
_BANNERS = (
    "=== The customer asked. The agent went silent. ===",
    "=== What the trace shows ===",
    "=== Import the real job so we can query that failure ===",
    "=== Step 1: evalbench-import ===",
    "=== failed_sessions finds the one that never answered ===",
    "=== Step 2: evalbench-failed-sessions ===",
    "=== A live judge would miss this ===",
    "=== Step 3: evalbench-score ===",
    "=== Punchline ===",
)

_PUNCHLINE = (
    "This widget-stock session failed because the agent never answered "
    "(goal_completion=0.0). G1 names it task/planning, tool blockers, and "
    "finalization — it never planned the lookup, never called "
    "check_inventory, never finished. Next debugging action: inspect why "
    "the trace died after AGENT_STARTING before the inventory tool."
)

_FROZEN_CATEGORIES_LINE = (
    '"taxonomy_categories": ["task/planning", "finalization", "tool blockers"]'
)

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


def _assert_freeze_walkthrough(stdout: str) -> None:
  for banner in _BANNERS:
    assert banner in stdout
  # The six acts appear in story order.
  positions = [stdout.index(b) for b in _BANNERS]
  assert positions == sorted(positions)
  customer = stdout[positions[0] : positions[1]]
  trace = stdout[positions[1] : positions[2]]
  imported = stdout[positions[2] : positions[4]]
  failed = stdout[positions[4] : positions[6]]
  judged = stdout[positions[6] : positions[8]]
  punchline = stdout[positions[8] :]
  # Act 1: the customer and the session, no jargon.
  assert "support_agent" in customer
  assert "terse support agent" in customer
  assert "inventory or tickets" in customer
  assert "real-user-0" in customer
  assert "How many widgets are in stock?" in customer
  assert _SESSION_ID in customer
  assert f"eval_id:       {_EVAL_ID}" in customer
  assert "test-project-0728-467323.bqaa_e2e_real.agent_events" in customer
  # Act 2: the trace stops after AGENT_STARTING; the silence is obvious.
  assert "USER_MESSAGE_RECEIVED" in trace
  assert "INVOCATION_STARTING" in trace
  assert "AGENT_STARTING" in trace
  assert "silence" in trace
  assert "no check_inventory" in trace
  assert "no LLM_RESPONSE" in trace
  assert "no AGENT_COMPLETED" in trace
  assert "ab7535a5" in trace
  assert "There are 0 widgets in stock." in trace
  assert "no answer" in trace
  # Act 3: import result, shaped like the 455 fixture.
  assert '"status": "imported"' in imported
  assert '"event_row_count": 27' in imported
  assert '"score_row_count": 7' in imported
  assert (
      '"failed_sessions_view": "analytics-project.bqaa.evalbench_failed_sessions"'
      in imported
  )
  # Act 4: failed_sessions as JSON -- mechanical flags, the FROZEN
  # taxonomy_categories line, plain-English glosses, one trust note.
  assert "--format json" in failed
  assert "evalbench-import:mvp-e2e-real-traces:v1:7e352c34" in failed
  assert '"process_failed": true' in failed
  assert '"missing_completion": true' in failed
  assert '"score_failed": true' in failed
  assert '"failing_scores": {"goal_completion": 0.0}' in failed
  assert _FROZEN_CATEGORIES_LINE in failed
  assert '"session_count": 7' in failed
  assert '"failed_count": 1' in failed
  assert "1 of 7" in failed
  assert "never decided to look up stock" in failed
  assert "never called check_inventory" in failed
  assert "never produced an answer" in failed
  assert "not SANA/Strands" in failed
  assert "Fail-closed D4" in failed
  assert "Hai-Yuan Cao" in failed
  assert "funding rec" in failed
  assert "Clock not started" in failed
  # The demo is a story, not a G1 mapping lecture: no arrow table.
  assert "missing_completion ->" not in stdout
  assert "process_failed     ->" not in stdout
  assert "score_failed       ->" not in stdout
  # Act 5: the judge misses it; failed_sessions is the denominator.
  assert '"pass_rate": 1.0' in judged
  assert '"llm_feedback": null' in judged
  assert '"pinned_sessions": 7' in judged
  assert "failed_sessions, not the judge," in judged
  assert "denominator" in judged
  # Act 6: punchline is exactly one paragraph, then nothing else.
  assert punchline.strip().splitlines()[1:] == [_PUNCHLINE]
  # Nowhere does the demo invent people, the example partner, or a
  # started clock.
  assert "Acme" not in stdout
  assert "Alex Rivera" not in stdout
  assert "Jordan Lee" not in stdout
  assert "clock has started" not in stdout
  assert "clock has not started" in stdout or "Clock not started" in stdout


def test_fixture_flag_tells_the_story_and_exits_zero() -> None:
  result = _run("--fixture")
  assert result.returncode == 0, result.stderr
  assert result.stderr == ""
  assert "fixture mode" in result.stdout
  assert _PUNCHLINE in result.stdout
  _assert_freeze_walkthrough(result.stdout)


def test_fixture_env_var_alone_is_rejected() -> None:
  # Regression: the demo is --fixture argv only. An inherited
  # EVALBENCH_FIXTURE=1 with no arguments must not run the transcript.
  result = _run(env={"EVALBENCH_FIXTURE": "1"})
  assert result.returncode == 2
  assert "--fixture only" in result.stderr
  assert "=== The customer asked" not in result.stdout
  assert result.stdout == ""


def test_without_fixture_flag_exits_two_and_runs_nothing() -> None:
  result = _run()
  assert result.returncode == 2
  assert "--fixture only" in result.stderr
  assert "=== The customer asked" not in result.stdout
  assert result.stdout == ""


def test_synth_flag_is_rejected_without_running() -> None:
  # The freeze demo has no --synth mode; it is an unknown argument.
  result = _run("--synth")
  assert result.returncode == 2
  assert "--fixture only" in result.stderr
  assert "=== The customer asked" not in result.stdout
  assert result.stdout == ""


def test_unknown_argument_is_rejected() -> None:
  result = _run("--bogus")
  assert result.returncode == 2
  assert "unknown argument '--bogus'" in result.stderr
  assert result.stdout == ""


def test_help_prints_usage_without_running() -> None:
  result = _run("--help")
  assert result.returncode == 0
  assert "--fixture" in result.stdout
  assert "=== The customer asked" not in result.stdout


def test_example_acme_pack_is_not_the_freeze() -> None:
  # The EXAMPLE pack still exists and stays illustrative; the freeze demo
  # neither sources it nor mentions its fictional partner.
  assert _EXAMPLE_PACK.exists()
  partner = json.loads(
      (_FIXTURE_DIR / "week0_example_partner.json").read_text()
  )
  assert partner["example"] is True
  assert partner["g1_frozen"] is False
  # Only header comments may point readers at the pack; no code line runs
  # it, sources it, or prints its fictional partner.
  code_lines = [
      line
      for line in _SCRIPT.read_text().splitlines()
      if not line.lstrip().startswith("#")
  ]
  assert not any("week0_full_idea" in line for line in code_lines)
  assert not any("week0_example" in line for line in code_lines)
  assert not any("Acme" in line for line in code_lines)


def test_production_taxonomy_is_frozen_at_v010() -> None:
  # The freeze the demo narrates is real, in production code.
  assert failure_taxonomy.TAXONOMY_VERSION == "0.1.0"
  config = failure_taxonomy.taxonomy_config()
  assert config["g1_frozen"] is True
  assert config["taxonomy_version"] == "0.1.0"
  # The frozen mechanical mapping the demo's labels come from.
  assert dict(failure_taxonomy.FLAG_TO_CATEGORY) == {
      "missing_completion": "finalization",
      "process_failed": "tool blockers",
      "score_failed": "task/planning",
  }
