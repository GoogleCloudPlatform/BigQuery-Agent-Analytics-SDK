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

"""Tests for skill_evolution_job.engine (locator + compat adapter)."""

import logging
import os
import sys
import textwrap

import pytest

_REPO_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
_JOB_DIR = os.path.join(_REPO_ROOT, "deploy", "skill_evolution_job")
if _JOB_DIR not in sys.path:
  sys.path.insert(0, _JOB_DIR)

from skill_evolution_job import engine
from skill_evolution_job import evolve

# evolve_skill as on upstream main today (no error_analyst_fn /
# incumbent_score / analyst_timeout_s).
_UPSTREAM_ENGINE = textwrap.dedent(
    """
    def evolve_skill(
        report_path,
        skill_path,
        output_path=None,
        *,
        model="gemini-2.5-pro",
        project=None,
        location=None,
        max_workers=8,
        max_success_samples=5,
        candidates=1,
        max_chars=0,
        analyst_mode="single",
        score_fn=None,
        min_improvement=0.0,
        tools=None,
        artifacts_dir=None,
        version_label=None,
        client=None,
    ):
      return {"received": sorted(k for k in locals() if k != "client")}
    """
)

# evolve_skill with the agentic-analyst extensions (#395).
_FORK_ENGINE = textwrap.dedent(
    """
    def evolve_skill(
        report_path,
        skill_path,
        output_path=None,
        *,
        model="gemini-2.5-pro",
        project=None,
        location=None,
        max_workers=8,
        max_success_samples=5,
        candidates=1,
        max_chars=0,
        analyst_mode="single",
        score_fn=None,
        min_improvement=0.0,
        tools=None,
        artifacts_dir=None,
        version_label=None,
        client=None,
        error_analyst_fn=None,
        incumbent_score=None,
        analyst_timeout_s=600,
    ):
      return {
          "received": sorted(k for k in locals() if k != "client"),
          "incumbent_score": incumbent_score,
      }
    """
)


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
  monkeypatch.delenv("SDK_SCRIPTS_DIR", raising=False)
  engine.reset_cache()
  yield
  engine.reset_cache()


def _install_fake_engine(tmp_path, source, monkeypatch):
  (tmp_path / "skill_evolution.py").write_text(source)
  monkeypatch.setenv("SDK_SCRIPTS_DIR", str(tmp_path))
  engine.reset_cache()


def test_locator_prefers_sdk_scripts_dir(tmp_path, monkeypatch):
  _install_fake_engine(tmp_path, _UPSTREAM_ENGINE, monkeypatch)
  assert engine.engine_path() == str(tmp_path / "skill_evolution.py")


def test_locator_falls_back_to_repo_scripts():
  assert engine.engine_path() == os.path.join(
      _REPO_ROOT, "scripts", "skill_evolution.py"
  )


def test_locator_error_lists_searched_paths(tmp_path, monkeypatch):
  monkeypatch.setenv("SDK_SCRIPTS_DIR", str(tmp_path / "nowhere"))
  monkeypatch.setattr(
      engine,
      "_candidate_dirs",
      lambda: [str(tmp_path / "nowhere"), "/app/scripts"],
  )
  with pytest.raises(FileNotFoundError, match="SDK_SCRIPTS_DIR"):
    engine.engine_path()


def test_real_engine_import_smoke():
  module = engine.load_engine()
  assert callable(module.evolve_skill)
  # Baseline kwargs the component relies on must exist upstream.
  supported = engine.supported_kwargs()
  for kwarg in (
      "score_fn",
      "min_improvement",
      "candidates",
      "analyst_mode",
      "tools",
      "artifacts_dir",
      "version_label",
      "client",
  ):
    assert kwarg in supported, f"engine lost kwarg {kwarg}"


def test_compat_drops_unsupported_kwargs_with_log(
    tmp_path, monkeypatch, caplog
):
  _install_fake_engine(tmp_path, _UPSTREAM_ENGINE, monkeypatch)
  with caplog.at_level(logging.INFO, logger="skill_evolution_job.engine"):
    result = engine.evolve_skill_compat(
        "report.json",
        "SKILL.md",
        candidates=3,
        error_analyst_fn=lambda: None,
        incumbent_score=42.0,
        analyst_timeout_s=60,
    )
  assert "error_analyst_fn" not in result["received"]
  assert "candidates" in result["received"]
  dropped_logs = [r for r in caplog.records if "dropping" in r.getMessage()]
  assert len(dropped_logs) == 1
  message = dropped_logs[0].getMessage()
  for name in ("analyst_timeout_s", "error_analyst_fn", "incumbent_score"):
    assert name in message


def test_compat_passes_all_kwargs_on_fork_engine(tmp_path, monkeypatch, caplog):
  _install_fake_engine(tmp_path, _FORK_ENGINE, monkeypatch)
  with caplog.at_level(logging.INFO, logger="skill_evolution_job.engine"):
    result = engine.evolve_skill_compat(
        "report.json",
        "SKILL.md",
        candidates=3,
        error_analyst_fn=lambda: None,
        incumbent_score=42.0,
    )
  assert "error_analyst_fn" in result["received"]
  assert result["incumbent_score"] == 42.0
  assert not [r for r in caplog.records if "dropping" in r.getMessage()]


def test_load_engine_caches(tmp_path, monkeypatch):
  _install_fake_engine(tmp_path, _UPSTREAM_ENGINE, monkeypatch)
  first = engine.load_engine()
  assert engine.load_engine() is first
  assert engine.load_engine(force_reload=True) is not first


# ---------------------------------------------------------------------------
# evolve.bound_candidates / evolve.resolve_candidates
# ---------------------------------------------------------------------------


def test_bound_candidates_reads_env(monkeypatch):
  monkeypatch.delenv("EVOLUTION_CANDIDATES", raising=False)
  assert evolve.bound_candidates() is None
  monkeypatch.setenv("EVOLUTION_CANDIDATES", "2")
  assert evolve.bound_candidates() == 2


def test_resolve_candidates_env_is_binding(monkeypatch):
  monkeypatch.setenv("EVOLUTION_CANDIDATES", "2")
  assert evolve.resolve_candidates(3, {"meaningful_rate": 50}) == 2


def test_resolve_candidates_uses_caller_value_without_env(monkeypatch):
  monkeypatch.delenv("EVOLUTION_CANDIDATES", raising=False)
  assert evolve.resolve_candidates(3, {"meaningful_rate": 50}) == 3


def test_resolve_candidates_auto_selects_one_at_high_rate(monkeypatch):
  monkeypatch.delenv("EVOLUTION_CANDIDATES", raising=False)
  assert evolve.resolve_candidates(None, {"meaningful_rate": 95}) == 1
