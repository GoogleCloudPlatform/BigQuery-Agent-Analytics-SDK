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

"""Tool boundary regressions for scope, limits, and failed candidate writes."""

import json
import os
from pathlib import Path
import sys
from unittest.mock import Mock

import pytest

_JOB_DIR = str(
    Path(__file__).resolve().parents[1] / "deploy" / "skill_evolution_job"
)
if _JOB_DIR not in sys.path:
  sys.path.insert(0, _JOB_DIR)

from skill_evolution_job import config
from skill_evolution_job import evolve
from skill_evolution_job import hooks
from skill_evolution_job import registry
from skill_evolution_job import tools


@pytest.fixture
def job(tmp_path, monkeypatch):
  for name in (
      "EVOLUTION_MAX_ROUNDS",
      "EVOLUTION_CANDIDATES",
      "EVOLUTION_WORKDIR",
      "GITHUB_REPO",
  ):
    monkeypatch.delenv(name, raising=False)
  config.reset_workdir_cache()
  registry.reset_cache()
  monkeypatch.setattr(tools, "_rounds_run", {})
  monkeypatch.setattr(hooks, "get_hook", lambda name: (None, "test"))
  skills = {}
  for name in ("allowed", "other"):
    directory = tmp_path / name
    directory.mkdir()
    (directory / "SKILL.md").write_bytes(b"original\r\n")
    skills[name] = directory
  reg = tmp_path / "registry.json"
  reg.write_text(
      json.dumps(
          {
              "repo_root": str(tmp_path),
              "agents": {
                  name: {"skill_dir": str(directory)}
                  for name, directory in skills.items()
              },
          }
      )
  )
  monkeypatch.setenv("AGENT_REGISTRY", str(reg))
  monkeypatch.setenv("EVOLUTION_TARGET_AGENTS", "allowed")
  report = tmp_path / "report.json"
  report.write_text(json.dumps({"summary": {"meaningful_rate": 20}}))
  yield skills, report
  config.reset_workdir_cache()
  registry.reset_cache()


@pytest.mark.parametrize("form", ["name", "path", "symlink"])
def test_bound_agent_rejects_other_skill_before_side_effects(
    job, tmp_path, monkeypatch, form
):
  skills, report = job
  forbidden = Mock(side_effect=AssertionError("evolution must not run"))
  monkeypatch.setattr(evolve, "evolve", forbidden)
  target = "other" if form == "name" else str(skills["other"])
  if form == "symlink":
    alias = tmp_path / "alias"
    alias.symlink_to(skills["other"], target_is_directory=True)
    target = str(alias)
  run_dir = tmp_path / "run"
  result = tools.run_evolution(
      str(report), target, run_dir=str(run_dir), candidates=1
  )
  assert result["status"] == "refused"
  assert not tools._rounds_run
  assert not run_dir.exists()
  assert (skills["other"] / "SKILL.md").read_bytes() == b"original\r\n"
  forbidden.assert_not_called()


@pytest.mark.parametrize("form", ["name", "path", "symlink"])
def test_bound_agent_accepts_its_name_and_resolved_paths(
    job, tmp_path, monkeypatch, form
):
  skills, report = job
  generation = Mock(return_value="accepted")
  monkeypatch.setattr(evolve, "evolve", generation)
  target = "allowed" if form == "name" else str(skills["allowed"])
  if form == "symlink":
    alias = tmp_path / "alias"
    alias.symlink_to(skills["allowed"], target_is_directory=True)
    target = str(alias)
  result = tools.run_evolution(str(report), target, candidates=1)
  assert result["status"] == "success"
  assert (skills["allowed"] / "SKILL.md").read_text() == "accepted"
  assert (skills["other"] / "SKILL.md").read_bytes() == b"original\r\n"


def test_failed_evolution_restores_exact_original_and_round(job, monkeypatch):
  skills, report = job
  skill = skills["allowed"] / "SKILL.md"

  def fails(**kwargs):
    skill.write_text("unaccepted candidate")
    raise RuntimeError("scorer failed after installing candidate")

  monkeypatch.setattr(evolve, "evolve", fails)
  result = tools.run_evolution(str(report), "allowed", candidates=1)
  assert result["status"] == "error"
  assert skill.read_bytes() == b"original\r\n"
  assert (
      tools._rounds_run[f"run_evolution[{os.path.realpath(skills['allowed'])}]"]
      == 0
  )


@pytest.mark.parametrize("value", ["bad", "-1", "3", "1.5", ""])
def test_invalid_round_limit_returns_error_before_evolution(
    job, monkeypatch, value
):
  _, report = job
  monkeypatch.setenv("EVOLUTION_MAX_ROUNDS", value)
  generation = Mock()
  monkeypatch.setattr(evolve, "evolve", generation)
  result = tools.run_evolution(str(report), "allowed", candidates=1)
  assert result["status"] == "error"
  assert "0 to 2" in result["error"]
  assert not tools._rounds_run
  generation.assert_not_called()


def test_single_candidate_provides_score_artifact_directory(
    job, tmp_path, monkeypatch
):
  _, report = job
  generation = Mock(return_value="selected")
  monkeypatch.setattr(evolve, "evolve", generation)
  run_dir = tmp_path / "run"
  result = tools.run_evolution(
      str(report), "allowed", run_dir=str(run_dir), candidates=1
  )
  assert result["status"] == "success"
  assert generation.call_args.kwargs["candidates_dir"] == str(
      run_dir / "candidates"
  )
  assert generation.call_args.kwargs["incumbent_score"] is None
