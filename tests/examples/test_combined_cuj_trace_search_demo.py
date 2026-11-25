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

"""Root CI pytest entry point for examples/combined_cuj_trace_search_demo."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DEMO_DIR = REPO_ROOT / "examples" / "combined_cuj_trace_search_demo"
DEMO_TESTS_DIR = DEMO_DIR / "tests"


def _run_demo_pytest_module(test_filename: str) -> None:
  """Runs a demo test file in an isolated subprocess with clean PYTHONPATH."""
  test_path = DEMO_TESTS_DIR / test_filename
  if not test_path.exists():
    pytest.skip(f"{test_filename} not present yet")
  env = os.environ.copy()
  env["PYTHONDONTWRITEBYTECODE"] = "1"
  env["PYTHONSAFEPATH"] = "1"
  src_dir = REPO_ROOT / "src"
  existing_pp = env.get("PYTHONPATH", "")
  pp_parts = [str(DEMO_DIR), str(src_dir)]
  if existing_pp:
    pp_parts.append(existing_pp)
  env["PYTHONPATH"] = os.pathsep.join(pp_parts)

  proc = subprocess.run(
      [sys.executable, "-m", "pytest", "--tb=short", "-q", str(test_path)],
      cwd=str(REPO_ROOT),
      env=env,
      capture_output=True,
      text=True,
      check=False,
  )
  assert (
      proc.returncode == 0
  ), f"{test_filename} failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"


def test_slice1_trajectory_extractor_and_agent() -> None:
  _run_demo_pytest_module("test_trajectory_extractor_and_agent.py")


def test_slice2_router_sql_and_patterns() -> None:
  _run_demo_pytest_module("test_router_sql_and_patterns.py")


def test_slice3_showcase_html_and_hygiene() -> None:
  assert (DEMO_TESTS_DIR / "test_showcase_html_and_hygiene.py").exists()
  _run_demo_pytest_module("test_showcase_html_and_hygiene.py")
