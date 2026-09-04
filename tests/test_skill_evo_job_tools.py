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

"""Tests for skill_evolution_job.tools (no GCP, no ADK, no network)."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

_REPO_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
_JOB_DIR = os.path.join(_REPO_ROOT, "deploy", "skill_evolution_job")
if _JOB_DIR not in sys.path:
  sys.path.insert(0, _JOB_DIR)

from skill_evolution_job import config
from skill_evolution_job import engine
from skill_evolution_job import hooks
from skill_evolution_job import registry
from skill_evolution_job import tools

_ENV_VARS = (
    "AGENT_REGISTRY",
    "GITHUB_REPO",
    "GITHUB_BASE_BRANCH",
    "GATE_POLICY",
    "EVOLUTION_HOOKS",
    "TRAFFIC_CMD",
    "SCORE_CMD",
    "GATE_CMD",
    "EVOLUTION_WORKDIR",
    "EVOLUTION_PUBLISH",
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "QUALITY_APP_NAME",
    "PROJECT_ID",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
  for var in _ENV_VARS:
    monkeypatch.delenv(var, raising=False)
  config.reset_workdir_cache()
  registry.reset_cache()
  hooks.reset_cache()
  engine.reset_cache()
  yield
  config.reset_workdir_cache()
  registry.reset_cache()
  hooks.reset_cache()
  engine.reset_cache()


def _write_registry(tmp_path, payload, name="agent_registry.json"):
  path = tmp_path / name
  path.write_text(json.dumps(payload))
  return str(path)


def _write_report(
    path,
    meaningful_rate,
    total_sessions=50,
    meaningful=None,
    excluded_error_shaped=None,
    unhelpful_rate=None,
):
  summary = {
      "meaningful_rate": meaningful_rate,
      "total_sessions": total_sessions,
      "meaningful": (
          meaningful
          if meaningful is not None
          else round(total_sessions * meaningful_rate / 100)
      ),
  }
  if excluded_error_shaped is not None:
    summary["excluded_error_shaped"] = {"count": excluded_error_shaped}
  if unhelpful_rate is not None:
    summary["unhelpful_rate"] = unhelpful_rate
  with open(path, "w") as f:
    json.dump({"summary": summary, "sessions": []}, f)


# ---------------------------------------------------------------------------
# snapshot_skills / restore_skills round-trip
# ---------------------------------------------------------------------------


def test_snapshot_and_restore_round_trip(tmp_path, monkeypatch):
  skill_dir = tmp_path / "agents" / "a" / "skill"
  skill_dir.mkdir(parents=True)
  skill_md = skill_dir / "SKILL.md"
  original_content = "# Original Skill\n\nv0 content.\n"
  skill_md.write_text(original_content)

  registry_path = _write_registry(
      tmp_path,
      {"repo_root": ".", "agents": {"a": {"skill_dir": str(skill_dir)}}},
  )
  monkeypatch.setenv("AGENT_REGISTRY", registry_path)

  run_dir = tmp_path / "run"
  snap_result = tools.snapshot_skills("v0", str(run_dir))
  assert snap_result["status"] == "success"
  assert snap_result["label"] == "v0"
  saved_path = snap_result["saved"]["a"]
  assert saved_path == str(run_dir / "v0_a_skill.md")
  assert os.path.isfile(saved_path)

  # Mutate the live SKILL.md.
  skill_md.write_text("# Mutated\n\nsomething else.\n")
  assert skill_md.read_text() != original_content

  restore_result = tools.restore_skills("v0", str(run_dir))
  assert restore_result["status"] == "success"
  assert restore_result["restored"]["a"] == str(skill_md)
  assert skill_md.read_text() == original_content


def test_restore_skills_no_snapshot_found(tmp_path, monkeypatch):
  skill_dir = tmp_path / "agents" / "a" / "skill"
  skill_dir.mkdir(parents=True)
  (skill_dir / "SKILL.md").write_text("content")
  registry_path = _write_registry(
      tmp_path,
      {"repo_root": ".", "agents": {"a": {"skill_dir": str(skill_dir)}}},
  )
  monkeypatch.setenv("AGENT_REGISTRY", registry_path)

  run_dir = tmp_path / "run"
  os.makedirs(run_dir)
  result = tools.restore_skills("v1", str(run_dir))
  assert result["status"] == "error"
  assert "v1" in result["error"]


# ---------------------------------------------------------------------------
# count_failures
# ---------------------------------------------------------------------------
#
# NOTE: contrary to the task description ("classifies sessions per the
# engine's convention"), count_failures never inspects the report's
# "sessions" list at all. It reads summary.total_sessions and
# summary.meaningful and computes failures = total - meaningful. Tests
# below reflect that actual behavior.


def test_count_failures_below_threshold(tmp_path):
  report_path = tmp_path / "quality_report.json"
  _write_report(
      report_path, meaningful_rate=90.0, total_sessions=50, meaningful=45
  )
  result = tools.count_failures(str(report_path))
  assert result["total_sessions"] == 50
  assert result["meaningful"] == 45
  assert result["failures"] == 5
  assert result["min_failures_threshold"] == 30
  assert result["should_evolve"] is False
  assert result["meaningful_rate"] == 90.0


def test_count_failures_meets_threshold_with_min_failures_env(
    tmp_path, monkeypatch
):
  monkeypatch.setenv("MIN_FAILURES", "10")
  report_path = tmp_path / "quality_report.json"
  _write_report(
      report_path, meaningful_rate=50.0, total_sessions=50, meaningful=25
  )
  result = tools.count_failures(str(report_path))
  assert result["failures"] == 25
  assert result["min_failures_threshold"] == 10
  assert result["should_evolve"] is True


def test_count_failures_missing_report(tmp_path):
  result = tools.count_failures(str(tmp_path / "missing.json"))
  assert "error" in result
  assert "not found" in result["error"]


# ---------------------------------------------------------------------------
# compare_versions
# ---------------------------------------------------------------------------


def test_compare_versions_picks_evolved_and_reports_delta(tmp_path):
  _write_report(
      tmp_path / "v0_quality_report.json",
      meaningful_rate=60.0,
      total_sessions=50,
  )
  _write_report(
      tmp_path / "v1_quality_report.json",
      meaningful_rate=80.0,
      total_sessions=50,
  )
  result = tools.compare_versions(str(tmp_path))
  assert result["status"] == "success"
  assert result["best_version"] == "v1"
  assert result["best_meaningful_rate"] == 80.0

  by_version = {v["version"]: v for v in result["versions"]}
  assert by_version["v0"]["delta"] is None
  assert by_version["v1"]["delta"] == 20.0
  assert "+20.0pp" in result["table"]


def test_compare_versions_excluded_error_shaped_suppresses_delta(tmp_path):
  _write_report(
      tmp_path / "v0_quality_report.json",
      meaningful_rate=60.0,
      total_sessions=50,
  )
  _write_report(
      tmp_path / "v1_quality_report.json",
      meaningful_rate=80.0,
      total_sessions=50,
      excluded_error_shaped=3,
  )
  result = tools.compare_versions(str(tmp_path))
  by_version = {v["version"]: v for v in result["versions"]}
  assert by_version["v1"]["excluded"] == 3
  # A shrunken-denominator report cannot feed a delta.
  assert by_version["v1"]["delta"] is None
  assert "excluded" in result["table"]


def test_compare_versions_no_reports(tmp_path):
  result = tools.compare_versions(str(tmp_path))
  assert "error" in result
  assert "No quality reports" in result["error"]


def test_compare_versions_missing_dir(tmp_path):
  result = tools.compare_versions(str(tmp_path / "nope"))
  assert "error" in result
  assert "not found" in result["error"]


# ---------------------------------------------------------------------------
# _mask_tokens
# ---------------------------------------------------------------------------


def test_mask_tokens_replaces_both_env_tokens(monkeypatch):
  monkeypatch.setenv("GH_TOKEN", "secret123")
  monkeypatch.setenv("GITHUB_TOKEN", "other456")
  text = "clone failed: token secret123 and also other456 leaked"
  masked = tools._mask_tokens(text)
  assert "secret123" not in masked
  assert "other456" not in masked
  assert masked.count("***") == 2


def test_mask_tokens_noop_without_env(monkeypatch):
  text = "nothing to mask here"
  assert tools._mask_tokens(text) == text


# ---------------------------------------------------------------------------
# _gh_repo_args
# ---------------------------------------------------------------------------


def test_gh_repo_args_set(monkeypatch):
  monkeypatch.setenv("GITHUB_REPO", "owner/repo")
  assert tools._gh_repo_args() == ["--repo", "owner/repo"]


def test_gh_repo_args_unset():
  assert tools._gh_repo_args() == []


# ---------------------------------------------------------------------------
# _default_pr_body
# ---------------------------------------------------------------------------


def test_default_pr_body_mentions_version_and_rates(tmp_path, monkeypatch):
  def _fail_if_called(*args, **kwargs):
    raise AssertionError("_default_pr_body must not shell out")

  monkeypatch.setattr(subprocess, "run", _fail_if_called)

  metrics = {
      "baseline_meaningful": 60.0,
      "baseline_unhelpful": 10.0,
      "baseline_label": "v0",
      "evolved_meaningful": 80.0,
      "evolved_unhelpful": 5.0,
      "baseline_excl": 0,
      "evolved_excl": 0,
  }
  run_dir = tmp_path / "run"
  os.makedirs(run_dir)
  body = tools._default_pr_body(
      "supervisor", "v1", metrics, 12345, str(run_dir)
  )
  assert "v1" in body
  assert "60.0%" in body
  assert "80.0%" in body
  assert "12345" in body
  assert "DENOMINATORS DIFFER" not in body


def test_default_pr_body_flags_differing_denominators(tmp_path):
  metrics = {
      "baseline_meaningful": 60.0,
      "baseline_unhelpful": 10.0,
      "baseline_label": "v0",
      "evolved_meaningful": 80.0,
      "evolved_unhelpful": 5.0,
      "baseline_excl": 2,
      "evolved_excl": 0,
  }
  run_dir = tmp_path / "run"
  os.makedirs(run_dir)
  body = tools._default_pr_body("supervisor", "v1", metrics, 100, str(run_dir))
  assert "DENOMINATORS DIFFER" in body


# ---------------------------------------------------------------------------
# create_evolution_pr: GATE_POLICY=require with no gate hook configured
# ---------------------------------------------------------------------------


def test_create_evolution_pr_gate_required_with_no_hook_errors(
    tmp_path, monkeypatch
):
  registry_path = _write_registry(
      tmp_path,
      {"repo_root": ".", "agents": {"a": {"skill_dir": "a/skill"}}},
  )
  monkeypatch.setenv("AGENT_REGISTRY", registry_path)
  monkeypatch.setenv("GATE_POLICY", "require")
  # EVOLUTION_PUBLISH must be true, otherwise create_evolution_pr forces
  # dry_run=True and returns before ever reaching the gate check.
  monkeypatch.setenv("EVOLUTION_PUBLISH", "true")

  run_dir = tmp_path / "run"
  os.makedirs(run_dir)
  result = tools.create_evolution_pr(str(run_dir), version="v1")
  assert result == {
      "status": "error",
      "error": (
          "ERROR: GATE_POLICY=require but no gate hook is configured"
          " (set EVOLUTION_HOOKS or GATE_CMD)"
      ),
  }


# ---------------------------------------------------------------------------
# score_candidate: no hook configured
# ---------------------------------------------------------------------------


def test_score_candidate_skipped_when_no_hook_configured(tmp_path):
  candidate_path = tmp_path / "candidate.md"
  candidate_path.write_text("# Candidate\n")
  skill_dir = tmp_path / "skill"
  skill_dir.mkdir()
  run_dir = tmp_path / "run"

  result = tools.score_candidate(
      str(candidate_path), str(skill_dir), str(run_dir)
  )
  assert "skipped" in result
  assert "SCORE_CMD" in result["skipped"]


# ---------------------------------------------------------------------------
# _round_guard (EVOLUTION_MAX_ROUNDS)
# ---------------------------------------------------------------------------


def test_round_guard_refuses_second_round_per_key(monkeypatch):
  monkeypatch.setenv("EVOLUTION_MAX_ROUNDS", "1")
  monkeypatch.setattr(tools, "_rounds_run", {})

  assert tools._round_guard("run_evolution[/a]") is None
  refused = tools._round_guard("run_evolution[/a]")
  assert refused["status"] == "refused"
  assert "EVOLUTION_MAX_ROUNDS=1" in refused["reason"]
  # Counted per agent: another skill dir still gets its own round.
  assert tools._round_guard("run_evolution[/b]") is None


def test_round_guard_never_refuses_without_env(monkeypatch):
  monkeypatch.delenv("EVOLUTION_MAX_ROUNDS", raising=False)
  monkeypatch.setattr(tools, "_rounds_run", {})

  for _ in range(5):
    assert tools._round_guard("run_evolution[/a]") is None


def test_run_evolution_refuses_past_max_rounds(tmp_path, monkeypatch):
  skill_dir = tmp_path / "skill"
  skill_dir.mkdir()
  (skill_dir / "SKILL.md").write_text("# Skill\n")
  report_path = tmp_path / "quality_report.json"
  _write_report(report_path, meaningful_rate=50.0)

  monkeypatch.setenv("EVOLUTION_MAX_ROUNDS", "1")
  # No AGENT_REGISTRY (the autouse fixture clears it), so
  # _resolve_skill_dir passes this absolute path straight through and the
  # guard key is the path as given.
  monkeypatch.setattr(tools, "_rounds_run", {f"run_evolution[{skill_dir}]": 1})

  result = tools.run_evolution(
      str(report_path), str(skill_dir), run_dir=str(tmp_path)
  )
  assert result["status"] == "refused"
  assert "EVOLUTION_MAX_ROUNDS=1" in result["reason"]


# ---------------------------------------------------------------------------
# _collect_quality_metrics
# ---------------------------------------------------------------------------


def _metrics_run_dir(tmp_path, candidate=True, evolved_score=None):
  """Run dir with initial/v1 reports and (optionally) a candidate report."""
  run_dir = tmp_path / "run"
  (run_dir / "candidates").mkdir(parents=True)
  _write_report(
      run_dir / "initial_quality_report.json",
      meaningful_rate=23.1,
      total_sessions=26,
      meaningful=6,
      unhelpful_rate=50.0,
  )
  _write_report(
      run_dir / "v1_quality_report.json",
      meaningful_rate=26.9,
      total_sessions=26,
      meaningful=7,
      unhelpful_rate=40.0,
  )
  if candidate:
    _write_report(
        run_dir / "candidates" / "candidate_1_report.json",
        meaningful_rate=100.0,
        total_sessions=26,
        meaningful=26,
        unhelpful_rate=0.0,
    )
  if evolved_score is not None:
    (run_dir / "evolved_score.json").write_text(json.dumps(evolved_score))
  return run_dir


def test_collect_quality_metrics_prefers_matching_candidate_report(tmp_path):
  run_dir = _metrics_run_dir(
      tmp_path,
      evolved_score={"version": "evolved", "meaningful_rate": 100.0},
  )
  metrics = tools._collect_quality_metrics(str(run_dir), "v1")
  assert metrics["evolved_meaningful"] == "100.0"
  assert metrics["evolved_unhelpful"] == "0.0"
  assert metrics["evolved_source"] == "evolved_score.json"
  assert metrics["baseline_meaningful"] == "23.1"
  assert metrics["baseline_label"] == "initial"


def test_collect_quality_metrics_evolved_score_without_report(tmp_path):
  run_dir = _metrics_run_dir(
      tmp_path,
      candidate=False,
      evolved_score={"version": "evolved", "meaningful_rate": 100.0},
  )
  metrics = tools._collect_quality_metrics(str(run_dir), "v1")
  assert metrics["evolved_meaningful"] == "100.0"
  # No candidate report to read the split from.
  assert metrics["evolved_unhelpful"] == "?"
  assert metrics["evolved_source"] == "evolved_score.json"


def test_collect_quality_metrics_falls_back_to_version_report(tmp_path):
  run_dir = _metrics_run_dir(tmp_path)
  metrics = tools._collect_quality_metrics(str(run_dir), "v1")
  assert metrics["evolved_meaningful"] == "26.9"
  assert metrics["evolved_unhelpful"] == "40.0"
  assert metrics["evolved_source"] == "report"


def test_collect_quality_metrics_ignores_unmeasurable_evolved_score(tmp_path):
  run_dir = _metrics_run_dir(
      tmp_path,
      evolved_score={
          "version": "evolved",
          "meaningful_rate": 100.0,
          "unmeasurable": True,
      },
  )
  metrics = tools._collect_quality_metrics(str(run_dir), "v1")
  assert metrics["evolved_meaningful"] == "26.9"
  assert metrics["evolved_source"] == "report"


# ---------------------------------------------------------------------------
# _identical_prior_report (run_quality_report stale detection)
# ---------------------------------------------------------------------------


def _write_summary_report(path, summary, mtime):
  path.write_text(json.dumps({"summary": dict(summary)}))
  os.utime(path, (mtime, mtime))


def test_identical_prior_report_finds_older_twin(tmp_path):
  summary = {"total_sessions": 26, "meaningful": 7}
  older = tmp_path / "a_quality_report.json"
  output = tmp_path / "b_quality_report.json"
  _write_summary_report(older, summary, 1000)
  _write_summary_report(output, summary, 2000)

  assert tools._identical_prior_report(str(output), summary) == str(older)
  # A different meaningful count is a genuinely new measurement.
  assert (
      tools._identical_prior_report(
          str(output), {"total_sessions": 26, "meaningful": 8}
      )
      is None
  )


def test_identical_prior_report_ignores_newer_files(tmp_path):
  summary = {"total_sessions": 26, "meaningful": 7}
  output = tmp_path / "b_quality_report.json"
  newer = tmp_path / "c_quality_report.json"
  _write_summary_report(output, summary, 1000)
  _write_summary_report(newer, summary, 2000)

  assert tools._identical_prior_report(str(output), summary) is None


# ---------------------------------------------------------------------------
# create_evolution_pr: git commit failure
# ---------------------------------------------------------------------------


def test_create_evolution_pr_reports_commit_failure(tmp_path, monkeypatch):
  workdir = tmp_path / "clone"
  (workdir / ".git").mkdir(parents=True)
  registry_path = _write_registry(
      tmp_path,
      {
          "repo_root": str(workdir),
          "agents": {"a": {"skill_dir": "agents/a/skill"}},
      },
  )
  monkeypatch.setenv("AGENT_REGISTRY", registry_path)
  monkeypatch.setenv("EVOLUTION_WORKDIR", str(workdir))
  monkeypatch.setenv("EVOLUTION_PUBLISH", "true")
  monkeypatch.setenv("GATE_POLICY", "skip")

  run_dir = tmp_path / "run"
  run_dir.mkdir()
  (run_dir / "v1_a_skill.md").write_text("# Evolved\n")

  def _fake_run(cmd, *args, **kwargs):
    failed = list(cmd[:2]) == ["git", "commit"]
    return subprocess.CompletedProcess(
        cmd,
        1 if failed else 0,
        stdout="nothing to commit, working tree clean" if failed else "",
        stderr="",
    )

  monkeypatch.setattr(subprocess, "run", _fake_run)

  result = tools.create_evolution_pr(str(run_dir), version="v1")
  assert result["status"] == "error"
  assert "git commit failed" in result["error"]
  assert "nothing to commit" in result["error"]
