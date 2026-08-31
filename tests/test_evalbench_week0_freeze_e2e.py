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

The post-freeze Week 0 e2e is ``--fixture`` only: nothing here reaches
BigQuery. It replays the REAL Week 0 freeze (partner + D4 + G1, landed by
the Week 0 freeze PR) on the same widget-stock failed session as the
merged MVP e2e demo. It is distinct from the EXAMPLE Acme pack
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

# The frozen record first (partner, D4, G1), then the widget-session
# through-line from the merged MVP e2e demo, in order.
_BANNERS = (
    "=== Partner: Google Cloud BQAA (this SDK) ===",
    "=== D4: fail-closed memo for this pilot ===",
    "=== G1 freeze: taxonomy v0.1.0 ===",
    "=== This agent was asked to check widget stock. Here is the session. ===",
    "=== What happened ===",
    "=== Import those traces into EvalBench so we can query this failure ===",
    "=== Step 1: evalbench-import ===",
    "=== This session in failed_sessions with frozen G1 labels ===",
    "=== Step 2: evalbench-failed-sessions ===",
    "=== Score this session ===",
    "=== Step 3: evalbench-score ===",
    "=== Punchline ===",
)

_PUNCHLINE = (
    "This widget-stock session failed because the agent never answered;"
    " goal_completion=0.0. G1 frozen labels are task/planning, finalization,"
    " tool blockers."
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
  # The frozen record and the through-line appear in narrative order.
  positions = [stdout.index(b) for b in _BANNERS]
  assert positions == sorted(positions)
  partner = stdout[positions[0] : positions[1]]
  d4 = stdout[positions[1] : positions[2]]
  g1 = stdout[positions[2] : positions[3]]
  session = stdout[positions[3] : positions[4]]
  happened = stdout[positions[4] : positions[5]]
  imported = stdout[positions[5] : positions[7]]
  failed = stdout[positions[7] : positions[9]]
  scored = stdout[positions[9] : positions[11]]
  punchline = stdout[positions[11] :]
  # 1. Partner: the REAL frozen partner, not the Acme example.
  assert "Google Cloud BigQuery Agent Analytics" in partner
  assert "mvp-e2e-real-traces" in partner
  assert "SANA-adjacent" in partner
  assert "not a SANA fork" in partner
  assert "not duplicating" in partner
  assert "docs/week0_partner.md" in partner
  assert "Acme" not in stdout
  # 2. D4: fail-closed, one named real consumer, no example people.
  assert "fail-closed" in d4
  assert "Hai-Yuan Cao" in d4
  assert "test-project-0728-467323" in d4
  assert "Part II funding recommendation" in d4
  assert "docs/week0_d4_memo.md" in d4
  assert "Alex Rivera" not in stdout
  assert "Jordan Lee" not in stdout
  # 3. G1: the frozen version, the mechanical mapping, no clock start.
  assert "taxonomy_version: 0.1.0" in g1
  assert "g1_frozen: true" in g1
  assert "clock_started: false" in g1
  assert "missing_completion -> finalization" in g1
  assert "process_failed     -> tool blockers" in g1
  assert "score_failed       -> task/planning" in g1
  assert "frozen order" in g1
  assert "docs/week0_g1_taxonomy.md" in g1
  # 4. The session: the same protagonist as the merged MVP e2e demo.
  assert "support_agent" in session
  assert "real-user-0" in session
  assert _SESSION_ID in session
  assert f"scenario_id:   {_EVAL_ID}" in session
  # 5. What happened: the verbatim prompt and no answer.
  assert "How many widgets are in stock?" in happened
  assert "(no response)" in happened
  assert "AGENT_STARTING" in happened
  assert "no AGENT_COMPLETED" in happened
  assert "ab7535a5" in happened
  # 6. Import result, shaped like the 455 fixture.
  assert '"status": "imported"' in imported
  assert '"event_row_count": 27' in imported
  assert '"score_row_count": 7' in imported
  assert (
      '"failed_sessions_view": "analytics-project.bqaa.evalbench_failed_sessions"'
      in imported
  )
  # 7. failed_sessions as JSON: mechanical flags still true, plus the
  # FROZEN taxonomy_categories in frozen order.
  assert "--format json" in failed
  assert "evalbench-import:mvp-e2e-real-traces:v1:7e352c34" in failed
  assert '"process_failed": true' in failed
  assert '"missing_completion": true' in failed
  assert '"score_failed": true' in failed
  assert '"failing_scores": {"goal_completion": 0.0}' in failed
  assert _FROZEN_CATEGORIES_LINE in failed
  assert '"session_count": 7' in failed
  assert '"failed_count": 1' in failed
  assert "1 of 7 sessions failed" in failed
  assert "W0.4 denominator" in failed
  # 8. Score: the judge is not the denominator.
  assert '"pass_rate": 1.0' in scored
  assert '"llm_feedback": null' in scored
  assert '"pinned_sessions": 7' in scored
  assert "goal_completion is 0.0" in scored
  assert "W0.4 denominator" in scored
  # 9. Punchline: exactly one sentence, then nothing else.
  assert punchline.strip().splitlines()[1:] == [_PUNCHLINE]
  # Nowhere does the demo start the clock.
  assert "clock has started" not in stdout
  assert "clock has not started" in stdout or "Clock has not started" in stdout


def test_fixture_flag_replays_the_freeze_and_exits_zero() -> None:
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
  assert "=== Partner" not in result.stdout
  assert result.stdout == ""


def test_without_fixture_flag_exits_two_and_runs_nothing() -> None:
  result = _run()
  assert result.returncode == 2
  assert "--fixture only" in result.stderr
  assert "=== Partner" not in result.stdout
  assert result.stdout == ""


def test_synth_flag_is_rejected_without_running() -> None:
  # The freeze demo has no --synth mode; it is an unknown argument.
  result = _run("--synth")
  assert result.returncode == 2
  assert "--fixture only" in result.stderr
  assert "=== Partner" not in result.stdout
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
  assert "=== Partner" not in result.stdout


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
  # The demo's mapping lines match the frozen FLAG_TO_CATEGORY.
  assert dict(failure_taxonomy.FLAG_TO_CATEGORY) == {
      "missing_completion": "finalization",
      "process_failed": "tool blockers",
      "score_failed": "task/planning",
  }
