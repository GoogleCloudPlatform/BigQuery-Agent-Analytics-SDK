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

"""Tests for skill_evolution_job hooks + orchestrator (no GCP, no ADK)."""

import asyncio
import json
import os
import sys
import textwrap

import pytest

_REPO_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
_JOB_DIR = os.path.join(_REPO_ROOT, "deploy", "skill_evolution_job")
if _JOB_DIR not in sys.path:
  sys.path.insert(0, _JOB_DIR)

from skill_evolution_job import config
from skill_evolution_job import hooks
from skill_evolution_job import registry

_ENV_VARS = (
    "EVOLUTION_HOOKS",
    "TRAFFIC_CMD",
    "SCORE_CMD",
    "GATE_CMD",
    "GATE_POLICY",
    "QUALITY_SOURCE",
    "QUALITY_THRESHOLD",
    "MIN_SESSIONS",
    "GITHUB_REPO",
    "EVOLUTION_WORKDIR",
    "AGENT_REGISTRY",
    "FULL_LOOP",
    "MIN_FAILURES",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
  for var in _ENV_VARS:
    monkeypatch.delenv(var, raising=False)
  config.reset_workdir_cache()
  hooks.reset_cache()
  yield
  config.reset_workdir_cache()
  hooks.reset_cache()


@pytest.fixture
def hooks_module(tmp_path, monkeypatch):
  """Install a temp EVOLUTION_HOOKS module exposing traffic + score."""
  mod = tmp_path / "my_host_hooks.py"
  mod.write_text(
      textwrap.dedent(
          """
          CALLS = []


          def traffic(run_dir):
            CALLS.append(("traffic", run_dir))
            return {"status": "success"}


          def score(candidate, skill_dir, run_dir):
            CALLS.append(("score", candidate))
            return {"meaningful_rate": 88.0}
          """
      )
  )
  monkeypatch.syspath_prepend(str(tmp_path))
  monkeypatch.setenv("EVOLUTION_HOOKS", "my_host_hooks")
  sys.modules.pop("my_host_hooks", None)
  yield "my_host_hooks"
  sys.modules.pop("my_host_hooks", None)


# ---------------------------------------------------------------------------
# Hook resolution precedence: module > *_CMD > skip
# ---------------------------------------------------------------------------


def test_module_hook_beats_cmd(hooks_module, monkeypatch):
  monkeypatch.setenv("TRAFFIC_CMD", "echo never-run")
  hook, source = hooks.get_hook("traffic")
  assert hook is not None
  assert source == f"module {hooks_module}.traffic"
  assert hook("/tmp/r")["status"] == "success"


def test_cmd_hook_when_module_lacks_callable(hooks_module, monkeypatch):
  monkeypatch.setenv("GATE_CMD", "true")
  hook, source = hooks.get_hook("gate")
  assert hook is not None
  assert source == "command GATE_CMD"


def test_skip_with_reason_when_unconfigured():
  hook, reason = hooks.get_hook("score")
  assert hook is None
  assert "not configured" in reason
  assert "SCORE_CMD" in reason


def test_module_only_hooks_have_no_cmd_fallback(monkeypatch):
  monkeypatch.setenv("TOOLBOX_CMD", "echo nope")
  hook, reason = hooks.get_hook("toolbox")
  assert hook is None
  assert "EVOLUTION_HOOKS" in reason


def test_unknown_hook_name_raises():
  with pytest.raises(ValueError, match="Unknown hook"):
    hooks.get_hook("nonsense")


def test_broken_hooks_module_fails_loudly(monkeypatch):
  monkeypatch.setenv("EVOLUTION_HOOKS", "definitely_not_importable_xyz")
  with pytest.raises(RuntimeError, match="EVOLUTION_HOOKS"):
    hooks.get_hook("traffic")


# ---------------------------------------------------------------------------
# Command hooks: placeholders + result parsing
# ---------------------------------------------------------------------------


def test_substitute_placeholders():
  cmd = "run.sh --dir {run_dir} --agent {agent} --keep {unknown}"
  out = hooks.substitute(cmd, {"run_dir": "/r", "agent": "sup"})
  assert out == "run.sh --dir /r --agent sup --keep {unknown}"


def test_gate_cmd_pass_and_fail(monkeypatch, tmp_path):
  monkeypatch.setenv("GATE_CMD", "test -f {run_dir}/marker")
  hook, _ = hooks.get_hook("gate")
  passed, _ = hook(str(tmp_path), "v1", "sup")
  assert passed is False
  (tmp_path / "marker").touch()
  passed, _ = hook(str(tmp_path), "v1", "sup")
  assert passed is True


def test_score_cmd_json_line(monkeypatch, tmp_path):
  monkeypatch.setenv(
      "SCORE_CMD",
      'echo \'{"meaningful_rate": 72.5, "sessions": 10}\'',
  )
  hook, _ = hooks.get_hook("score")
  result = hook(str(tmp_path / "c.md"), str(tmp_path), str(tmp_path))
  assert result["meaningful_rate"] == 72.5
  assert result["sessions"] == 10


def test_score_cmd_bare_number(monkeypatch, tmp_path):
  monkeypatch.setenv("SCORE_CMD", "echo 61.5")
  hook, _ = hooks.get_hook("score")
  result = hook("c.md", "s", str(tmp_path))
  assert result["meaningful_rate"] == 61.5


def test_score_cmd_failure_raises(monkeypatch, tmp_path):
  monkeypatch.setenv("SCORE_CMD", "sh -c 'echo boom >&2; exit 3'")
  hook, _ = hooks.get_hook("score")
  with pytest.raises(RuntimeError, match="SCORE_CMD failed"):
    hook("c.md", "s", str(tmp_path))


def test_score_cmd_no_rate_raises(monkeypatch, tmp_path):
  monkeypatch.setenv("SCORE_CMD", "echo done")
  hook, _ = hooks.get_hook("score")
  with pytest.raises(RuntimeError, match="meaningful_rate"):
    hook("c.md", "s", str(tmp_path))


def test_cmd_output_masks_tokens(monkeypatch, tmp_path):
  monkeypatch.setenv("GH_TOKEN", "sekret-token-123")
  monkeypatch.setenv("GATE_CMD", "sh -c 'echo pushed to sekret-token-123'")
  hook, _ = hooks.get_hook("gate")
  _, detail = hook(str(tmp_path), "v1", "sup")
  assert "sekret-token-123" not in detail
  assert "***" in detail


# ---------------------------------------------------------------------------
# Orchestrator short-circuits (delta 6) — no ADK import needed
# ---------------------------------------------------------------------------

from skill_evolution_job import main as job_main


def _write_report(path, meaningful_rate, total_sessions=50):
  with open(path, "w") as f:
    json.dump(
        {
            "summary": {
                "meaningful_rate": meaningful_rate,
                "total_sessions": total_sessions,
            },
            "sessions": [],
        },
        f,
    )


def test_full_loop_quality_gate_short_circuit(tmp_path, monkeypatch):
  report = tmp_path / "v0_quality_report.json"
  _write_report(report, 97.0)
  monkeypatch.setattr(
      job_main,
      "_bigquery_quality_report",
      lambda run_dir: (str(report), 50),
  )
  result = asyncio.run(
      job_main.run_evolution_agent(report_path=None, run_dir=str(tmp_path))
  )
  assert "QUALITY GATE" in result
  assert "97.0%" in result


def test_full_loop_insufficient_sessions_no_traffic_hook(tmp_path, monkeypatch):
  monkeypatch.setenv("MIN_SESSIONS", "20")
  monkeypatch.setattr(
      job_main, "_bigquery_quality_report", lambda run_dir: (None, 3)
  )
  result = asyncio.run(
      job_main.run_evolution_agent(report_path=None, run_dir=str(tmp_path))
  )
  assert result.startswith("NOTHING TO DO")
  assert "3" in result and "20" in result
  assert not result.startswith("ERROR:")  # main() exits 0 on this


def test_full_loop_traffic_hook_retries_report(
    tmp_path, hooks_module, monkeypatch
):
  report = tmp_path / "v0_quality_report.json"
  _write_report(report, 97.5)  # gate stops the run after the retry
  calls = {"n": 0}

  def fake_report(run_dir):
    calls["n"] += 1
    return (None, 5) if calls["n"] == 1 else (str(report), 30)

  monkeypatch.setattr(job_main, "_bigquery_quality_report", fake_report)
  result = asyncio.run(
      job_main.run_evolution_agent(report_path=None, run_dir=str(tmp_path))
  )
  assert calls["n"] == 2
  module = sys.modules[hooks_module]
  assert ("traffic", str(tmp_path)) in module.CALLS
  assert "QUALITY GATE" in result


def test_full_loop_traffic_hook_still_insufficient(
    tmp_path, hooks_module, monkeypatch
):
  monkeypatch.setattr(
      job_main, "_bigquery_quality_report", lambda run_dir: (None, 5)
  )
  result = asyncio.run(
      job_main.run_evolution_agent(report_path=None, run_dir=str(tmp_path))
  )
  assert result.startswith("ERROR:")
  assert "MIN_SESSIONS" in result


def test_quality_threshold_env_respected(tmp_path, monkeypatch):
  report = tmp_path / "v0_quality_report.json"
  _write_report(report, 82.0)
  monkeypatch.setenv("QUALITY_THRESHOLD", "0.8")
  monkeypatch.setattr(
      job_main,
      "_bigquery_quality_report",
      lambda run_dir: (str(report), 50),
  )
  result = asyncio.run(
      job_main.run_evolution_agent(report_path=None, run_dir=str(tmp_path))
  )
  assert "QUALITY GATE" in result


# ---------------------------------------------------------------------------
# Env contract sanity (mode/env matrix)
# ---------------------------------------------------------------------------


def test_config_defaults(monkeypatch):
  for var in (
      "EVAL_TIME_PERIOD",
      "GATE_POLICY",
      "EVOLUTION_MODE",
      "GITHUB_BASE_BRANCH",
      "EVOLUTION_PUBLISH",
  ):
    monkeypatch.delenv(var, raising=False)
  cfg = config.get_config()
  assert cfg.eval_time_period == "7d"
  assert cfg.min_sessions == 20
  assert cfg.gate_policy == "skip"
  assert cfg.evolution_mode == "evolve"
  assert cfg.quality_source == "bigquery"
  assert cfg.github_base_branch == "main"
  assert cfg.evolution_publish is False


def test_config_env_overrides(monkeypatch):
  monkeypatch.setenv("QUALITY_SOURCE", "synthetic")
  monkeypatch.setenv("GATE_POLICY", "require")
  monkeypatch.setenv("EVOLUTION_PUBLISH", "true")
  monkeypatch.setenv("MIN_SESSIONS", "5")
  monkeypatch.setenv("QUALITY_THRESHOLD", "0.9")
  cfg = config.get_config()
  assert cfg.quality_source == "synthetic"
  assert cfg.gate_policy == "require"
  assert cfg.evolution_publish is True
  assert cfg.min_sessions == 5
  assert cfg.quality_threshold == 0.9


def test_mask_tokens(monkeypatch):
  monkeypatch.setenv("GH_TOKEN", "abc123")
  monkeypatch.setenv("GITHUB_TOKEN", "xyz789")
  masked = config.mask_tokens("push https://x:abc123@gh xyz789 done")
  assert "abc123" not in masked and "xyz789" not in masked


def test_workdir_dry_run_returns_none():
  assert config.workdir_or_none() is None
  with pytest.raises(RuntimeError, match="dry-run"):
    config.workdir()


def test_workdir_rejects_non_git_dir(tmp_path, monkeypatch):
  monkeypatch.setenv("EVOLUTION_WORKDIR", str(tmp_path))
  with pytest.raises(RuntimeError, match="not a git"):
    config.workdir()


def test_workdir_uses_existing_checkout(tmp_path, monkeypatch):
  (tmp_path / ".git").mkdir()
  monkeypatch.setenv("EVOLUTION_WORKDIR", str(tmp_path))
  assert config.workdir() == str(tmp_path)
  # Cached for the process.
  monkeypatch.delenv("EVOLUTION_WORKDIR")
  assert config.workdir() == str(tmp_path)


def test_workdir_accepts_git_worktree_file(tmp_path, monkeypatch):
  # In a git worktree ``.git`` is a FILE pointing at the real gitdir,
  # not a directory; it is still a checkout the job can commit in.
  (tmp_path / ".git").write_text("gitdir: /repo/.git/worktrees/wt\n")
  assert os.path.isfile(tmp_path / ".git")
  monkeypatch.setenv("EVOLUTION_WORKDIR", str(tmp_path))
  assert config.workdir() == str(tmp_path)


# ---------------------------------------------------------------------------
# main() argument parser
# ---------------------------------------------------------------------------


def test_mode_help_explains_unloadable_registry(monkeypatch, capsys):
  monkeypatch.delenv("AGENT_REGISTRY", raising=False)
  registry.reset_cache()
  monkeypatch.setattr(sys, "argv", ["skill-evolution-job", "--help"])

  with pytest.raises(SystemExit) as excinfo:
    job_main.main()
  assert excinfo.value.code == 0

  # Collapse argparse's wrapping so the assertion is width-independent.
  help_text = " ".join(capsys.readouterr().out.split())
  assert "--mode" in help_text
  assert "registry not loaded" in help_text
