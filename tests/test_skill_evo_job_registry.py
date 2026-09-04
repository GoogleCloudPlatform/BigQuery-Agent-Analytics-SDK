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

"""Tests for skill_evolution_job.registry (no GCP access needed)."""

import json
import os
import sys

import pytest

_REPO_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
_JOB_DIR = os.path.join(_REPO_ROOT, "deploy", "skill_evolution_job")
if _JOB_DIR not in sys.path:
  sys.path.insert(0, _JOB_DIR)

from skill_evolution_job import config
from skill_evolution_job import registry

_EXAMPLE = os.path.join(_JOB_DIR, "agent_registry.example.json")

_ENV_VARS = (
    "AGENT_REGISTRY",
    "EVOLUTION_ORDER",
    "EVOLUTION_WORKDIR",
    "GITHUB_REPO",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
  for var in _ENV_VARS:
    monkeypatch.delenv(var, raising=False)
  config.reset_workdir_cache()
  registry.reset_cache()
  yield
  config.reset_workdir_cache()
  registry.reset_cache()


def _write_registry(tmp_path, payload, name="agent_registry.json"):
  path = tmp_path / name
  path.write_text(json.dumps(payload))
  return str(path)


def test_example_registry_parses():
  reg = registry.load_registry(_EXAMPLE)
  assert set(reg.agents) == {"supervisor", "policy_agent"}
  assert reg.default_app_name == "my_agent_app"
  assert reg.agents["policy_agent"].skill_id == "policy-qa"
  # repo_root "." resolves next to the example file (no workdir configured).
  assert reg.repo_root == os.path.dirname(_EXAMPLE)
  assert reg.agents["supervisor"].skill_dir == os.path.join(
      reg.repo_root, "agents/supervisor/skill"
  )


def test_relative_paths_resolve_against_workdir(tmp_path, monkeypatch):
  workdir = tmp_path / "clone"
  (workdir / ".git").mkdir(parents=True)
  reg_path = _write_registry(
      workdir,
      {"agents": {"a": {"skill_dir": "agents/a/skill"}}},
  )
  monkeypatch.setenv("EVOLUTION_WORKDIR", str(workdir))
  monkeypatch.setenv("AGENT_REGISTRY", "agent_registry.json")
  assert registry.registry_path() == reg_path
  reg = registry.get_registry()
  # repo_root defaults to the workdir clone.
  assert reg.repo_root == str(workdir)
  assert reg.agents["a"].skill_dir == str(workdir / "agents/a/skill")


def test_absolute_skill_dir_passes_through(tmp_path):
  abs_dir = str(tmp_path / "elsewhere" / "skill")
  path = _write_registry(tmp_path, {"agents": {"a": {"skill_dir": abs_dir}}})
  reg = registry.load_registry(path)
  assert reg.agents["a"].skill_dir == abs_dir
  assert reg.resolve_skill_dir("a") == abs_dir
  assert reg.resolve_skill_dir("/some/path") == "/some/path"


def test_order_sorting_and_override(tmp_path, monkeypatch):
  path = _write_registry(
      tmp_path,
      {
          "agents": {
              "b": {"skill_dir": "b/skill", "order": 2},
              "a": {"skill_dir": "a/skill", "order": 1},
              "c": {"skill_dir": "c/skill", "order": 1},
          }
      },
  )
  reg = registry.load_registry(path)
  assert reg.ordered_names() == ["a", "c", "b"]
  assert reg.default_agent == "a"

  monkeypatch.setenv("EVOLUTION_ORDER", "c, b")
  assert reg.ordered_names() == ["c", "b"]

  monkeypatch.setenv("EVOLUTION_ORDER", "nope")
  with pytest.raises(registry.RegistryError, match="EVOLUTION_ORDER"):
    reg.ordered_names()


def test_order_defaults_to_declaration_index(tmp_path):
  path = _write_registry(
      tmp_path,
      {
          "agents": {
              "z": {"skill_dir": "z/skill"},
              "a": {"skill_dir": "a/skill"},
          }
      },
  )
  reg = registry.load_registry(path)
  assert reg.ordered_names() == ["z", "a"]


def test_app_name_resolution(tmp_path):
  path = _write_registry(
      tmp_path,
      {
          "default_app_name": "default_app",
          "agents": {
              "a": {"skill_dir": "a/skill", "app_name": "a_app"},
              "b": {"skill_dir": "b/skill"},
          },
      },
  )
  reg = registry.load_registry(path)
  assert reg.app_name_for("a") == "a_app"
  assert reg.app_name_for("b") == "default_app"
  assert reg.app_name_for() == "default_app"


def test_malformed_registry_errors(tmp_path):
  cases = [
      {"agents": {}},
      {"agents": []},
      {},
      {"agents": {"a": {}}},
      {"agents": {"a": {"skill_dir": "s", "order": "first"}}},
  ]
  for payload in cases:
    path = _write_registry(tmp_path, payload)
    with pytest.raises(registry.RegistryError):
      registry.load_registry(path)

  bad_json = tmp_path / "bad.json"
  bad_json.write_text("{not json")
  with pytest.raises(registry.RegistryError, match="not valid JSON"):
    registry.load_registry(str(bad_json))

  with pytest.raises(registry.RegistryError, match="not found"):
    registry.load_registry(str(tmp_path / "missing.json"))


def test_unknown_agent_errors():
  reg = registry.load_registry(_EXAMPLE)
  with pytest.raises(registry.RegistryError, match="Unknown agent"):
    reg.agent("nope")


def test_lazy_load_no_env(monkeypatch):
  # Import already happened at module top without AGENT_REGISTRY set;
  # only get_registry() should raise.
  with pytest.raises(registry.RegistryError, match="AGENT_REGISTRY"):
    registry.get_registry()


def test_agents_summary_keyed_by_name_in_registry_order(tmp_path, monkeypatch):
  path = _write_registry(
      tmp_path,
      {
          "repo_root": str(tmp_path),
          "agents": {
              "a": {"skill_dir": "a/skill", "label": "A agent", "order": 1},
              "b": {"skill_dir": "b/skill", "label": "B agent", "order": 0},
          },
      },
  )
  monkeypatch.setenv("AGENT_REGISTRY", path)
  summary = registry.agents_summary()
  # Ordered by the registry's evolution order, not declaration order.
  assert list(summary) == ["b", "a"]
  assert summary == {
      "b": {"label": "B agent", "skill_dir": str(tmp_path / "b" / "skill")},
      "a": {"label": "A agent", "skill_dir": str(tmp_path / "a" / "skill")},
  }


def test_get_registry_caches(tmp_path, monkeypatch):
  path = _write_registry(tmp_path, {"agents": {"a": {"skill_dir": "s"}}})
  monkeypatch.setenv("AGENT_REGISTRY", path)
  first = registry.get_registry()
  assert registry.get_registry() is first
  assert registry.get_registry(force_reload=True) is not first
