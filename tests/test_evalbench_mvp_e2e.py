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

_BANNERS = (
    "=== Step 1: evalbench-import ===",
    "=== Step 2: evalbench-failed-sessions ===",
    "=== Step 3: evalbench-score ===",
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
  # Banners appear in pipeline order.
  positions = [stdout.index(b) for b in _BANNERS]
  assert positions == sorted(positions)
  # Step 1: import result with manifest + failed_sessions_view.
  assert '"status": "imported"' in stdout
  assert '"manifest": {' in stdout
  assert '"generation_id"' in stdout
  assert (
      '"failed_sessions_view": "analytics-project.bqaa.evalbench_failed_sessions"'
      in stdout
  )
  # Step 2: a failed-sessions table with versioned session ids.
  assert "session_id" in stdout
  assert "evalbench-import:gemini-cli-tools-2026-08-30:v1:read-file" in stdout
  assert "session_count=4 failed_count=2" in stdout
  # Step 3: a score report with details.evalbench.
  assert '"details": {' in stdout
  assert '"evalbench": {' in stdout
  assert '"pinned_sessions": 4' in stdout
  assert '"pass_rate": 0.75' in stdout


def test_fixture_flag_walks_three_steps_and_exits_zero() -> None:
  result = _run("--fixture")
  assert result.returncode == 0, result.stderr
  assert result.stderr == ""
  assert "fixture mode" in result.stdout
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
  assert "evalbench-import:gemini-cli-tools-42:v3:read-file" in result.stdout
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
