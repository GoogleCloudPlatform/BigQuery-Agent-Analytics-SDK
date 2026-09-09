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

"""Tests for skill_evolution_job.engine (locator) and the engine call."""

import inspect
import json
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

# Stand-in engine for the locator tests: only the name matters.
_STUB_ENGINE = textwrap.dedent(
    """
    def evolve_skill(report, current_skill, **kwargs):
      return current_skill
    """
)

# Stand-in engine that records the keyword arguments it was called with.
_RECORDING_ENGINE = textwrap.dedent(
    """
    CALLS = []


    def evolve_skill(report, current_skill, **kwargs):
      CALLS.append(kwargs)
      return current_skill
    """
)


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
  monkeypatch.delenv("SDK_SCRIPTS_DIR", raising=False)
  monkeypatch.delenv("ANALYST_TIMEOUT_S", raising=False)
  engine.reset_cache()
  yield
  engine.reset_cache()


def _install_fake_engine(tmp_path, source, monkeypatch):
  (tmp_path / "skill_evolution.py").write_text(source)
  monkeypatch.setenv("SDK_SCRIPTS_DIR", str(tmp_path))
  engine.reset_cache()


def test_locator_prefers_sdk_scripts_dir(tmp_path, monkeypatch):
  _install_fake_engine(tmp_path, _STUB_ENGINE, monkeypatch)
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


def test_load_engine_caches(tmp_path, monkeypatch):
  _install_fake_engine(tmp_path, _STUB_ENGINE, monkeypatch)
  first = engine.load_engine()
  assert engine.load_engine() is first
  assert engine.load_engine(force_reload=True) is not first


def test_real_engine_keyword_contract():
  """The engine baked into the image must accept every kwarg we pass.

  There is no feature detection any more: the image stages this
  checkout's scripts/, so a kwarg going missing here is a build break,
  not something to degrade around at runtime.
  """
  module = engine.load_engine()
  assert callable(module.evolve_skill)
  supported = set(inspect.signature(module.evolve_skill).parameters)
  for kwarg in (
      "score_fn",
      "min_improvement",
      "candidates",
      "analyst_mode",
      "tools",
      "artifacts_dir",
      "version_label",
      "client",
      "error_analyst_fn",
      "incumbent_score",
      "analyst_timeout_s",
  ):
    assert kwarg in supported, f"engine lost kwarg {kwarg}"


@pytest.mark.parametrize(
    ("env_value", "expected"),
    [(None, 600.0), ("30", 30.0), ("0", None)],
)
def test_evolve_passes_analyst_timeout_to_engine(
    tmp_path, monkeypatch, env_value, expected
):
  _install_fake_engine(tmp_path, _RECORDING_ENGINE, monkeypatch)
  if env_value is not None:
    monkeypatch.setenv("ANALYST_TIMEOUT_S", env_value)
  monkeypatch.delenv("EVOLUTION_CANDIDATES", raising=False)
  monkeypatch.delenv("EVOLUTION_MAX_ANALYSTS", raising=False)
  monkeypatch.setattr(evolve, "_vertex_client", lambda: object())
  monkeypatch.setattr(evolve, "_derive_toolbox", lambda *_: None)
  monkeypatch.setattr(evolve, "_agent_for_skill_dir", lambda *_: None)
  monkeypatch.setattr(evolve, "_resolve_error_analyst", lambda *_: None)

  skill_dir = tmp_path / "skill"
  skill_dir.mkdir()
  (skill_dir / "SKILL.md").write_text("---\nname: example\n---\nAnswer.\n")
  report = tmp_path / "report.json"
  report.write_text(
      json.dumps({"summary": {"meaningful_rate": 95}, "sessions": []})
  )

  evolve.evolve(str(report), str(skill_dir))

  assert engine.load_engine().CALLS[-1]["analyst_timeout_s"] == expected


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
