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
"""Tests for ``examples/evalbench_mvp_e2e.sh`` (#435 slice 5, #97).

Only the offline ``--fixture`` path is exercised; nothing here reaches
BigQuery. Live mode is checked only as far as "refuses to start without
its environment", which also never reaches BigQuery.
"""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess

import pytest

_SCRIPT = (
    Path(__file__).resolve().parents[1] / "examples" / "evalbench_mvp_e2e.sh"
)

# The fixture tells one session's story in six beats; each CLI step banner
# serves the beat it follows.
_BANNERS = (
    "=== This agent was asked to check widget stock. Here is the session. ===",
    "=== What happened ===",
    "=== Import those traces into EvalBench so we can query this failure ===",
    "=== Step 1: evalbench-import ===",
    "=== This session in failed_sessions ===",
    "=== Step 2: evalbench-failed-sessions ===",
    "=== Score this session ===",
    "=== Step 3: evalbench-score ===",
    "=== Punchline ===",
)

_PUNCHLINE = (
    "This widget-stock session failed because the agent never answered;"
    " goal_completion=0.0."
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


def _assert_fixture_walkthrough(stdout: str) -> None:
  for banner in _BANNERS:
    assert banner in stdout
  # Story beats and step banners appear in narrative order.
  positions = [stdout.index(b) for b in _BANNERS]
  assert positions == sorted(positions)
  setup = stdout[positions[0] : positions[1]]
  happened = stdout[positions[1] : positions[2]]
  imported = stdout[positions[2] : positions[4]]
  failed = stdout[positions[4] : positions[6]]
  scored = stdout[positions[6] : positions[8]]
  punchline = stdout[positions[8] :]
  # 1. Setup: the protagonist session.
  assert "support_agent" in setup
  assert "real-user-0" in setup
  assert "7e352c34-4c1c-4395-acd5-fb3c8f215346" in setup
  assert "scenario_id:   7e352c34" in setup
  # 2. What happened: the verbatim prompt and no answer.
  assert "How many widgets are in stock?" in happened
  assert "(no response)" in happened
  assert "AGENT_STARTING" in happened
  assert "no AGENT_COMPLETED" in happened
  # 3. Import result with manifest + failed_sessions_view.
  assert '"status": "imported"' in imported
  assert '"manifest": {' in imported
  assert '"generation_id"' in imported
  assert (
      '"failed_sessions_view": "analytics-project.bqaa.evalbench_failed_sessions"'
      in imported
  )
  # 4. This session's one failed_sessions row, with the versioned id.
  assert "session_id" in failed
  assert "evalbench-import:mvp-e2e-real-traces:v1:7e352c34" in failed
  assert "process_failed" in failed
  assert "missing_completion" in failed
  assert '[{"comparator": "goal_completion", "score": 0.0}]' in failed
  assert "1 of 7 sessions failed" in failed
  assert "session_count=7 failed_count=1" in failed
  # 5. Score report with details.evalbench; the judge is not the denominator.
  assert '"details": {' in scored
  assert '"evalbench": {' in scored
  assert '"pinned_sessions": 7' in scored
  assert '"pass_rate": 1.0' in scored
  assert '"llm_feedback": null' in scored
  assert "goal_completion is 0.0" in scored
  # 6. Punchline: exactly one sentence, then nothing else.
  assert punchline.strip().splitlines()[1:] == [_PUNCHLINE]


def test_fixture_flag_tells_the_session_story_and_exits_zero() -> None:
  result = _run("--fixture")
  assert result.returncode == 0, result.stderr
  assert result.stderr == ""
  assert "fixture mode" in result.stdout
  assert _PUNCHLINE in result.stdout
  _assert_fixture_walkthrough(result.stdout)


def test_fixture_env_var_is_honored() -> None:
  result = _run(env={"EVALBENCH_FIXTURE": "1"})
  assert result.returncode == 0, result.stderr
  _assert_fixture_walkthrough(result.stdout)


def test_fixture_uses_supplied_names_without_bigquery() -> None:
  result = _run(
      "--fixture",
      env={
          "BQ_AGENT_PROJECT": "my-analytics",
          "BQ_AGENT_DATASET": "mirror",
          "EVALBENCH_JOB_ID": "gemini-cli-tools-42",
          "EVALBENCH_IMPORT_VERSION": "v3",
      },
  )
  assert result.returncode == 0, result.stderr
  # The names change; the protagonist session does not.
  assert "evalbench-import:gemini-cli-tools-42:v3:7e352c34" in result.stdout
  assert "How many widgets are in stock?" in result.stdout
  assert _PUNCHLINE in result.stdout
  assert (
      '"failed_sessions_view": "my-analytics.mirror.evalbench_failed_sessions"'
      in result.stdout
  )
  assert '"import_version": "v3"' in result.stdout


def test_live_mode_requires_environment_before_any_command() -> None:
  result = _run()
  assert result.returncode == 1
  assert "BQ_AGENT_PROJECT" in result.stderr
  assert "=== Step 1" not in result.stdout


def test_unknown_argument_is_rejected() -> None:
  result = _run("--bogus")
  assert result.returncode == 2
  assert "unknown argument '--bogus'" in result.stderr


def test_synth_mode_rejects_split_projects_before_any_command() -> None:
  # --synth builds the EvalBench-shaped dataset and the mirror dataset in one
  # project, so split projects stop the script before step 0 runs anything.
  result = _run(
      "--synth",
      env={
          "BQ_AGENT_PROJECT": "analytics-project",
          "EVALBENCH_PROJECT": "benchmark-project",
          "EVALBENCH_PYTHON": "/nonexistent/python",
      },
  )
  assert result.returncode == 1
  assert "EVALBENCH_PROJECT=benchmark-project differs" in result.stderr
  assert "=== Step 0" not in result.stdout


def test_synth_mode_defaults_names_and_runs_step_zero_first() -> None:
  # With a project set, --synth fills in the demo's dataset/job defaults and
  # step 0 is the first thing it runs. An interpreter that cannot start makes
  # step 0 fail (exit 2) before any BigQuery call.
  result = _run(
      "--synth",
      env={
          "BQ_AGENT_PROJECT": "analytics-project",
          "EVALBENCH_PYTHON": "/nonexistent/python",
      },
  )
  assert result.returncode == 2
  assert "=== Step 0: synthesize EvalBench tables from real traces ===" in (
      result.stdout
  )
  assert "=== Step 1" not in result.stdout
  assert (
      "job mvp-e2e-real-traces (analytics-project.bqaa_evalbench_mvp_demo ->"
      " analytics-project.bqaa_evalbench_mvp_mirror)"
  ) in result.stdout
  assert "bqaa_e2e_real.agent_events ->" in result.stdout
  assert "step failed: /nonexistent/python" in result.stderr
