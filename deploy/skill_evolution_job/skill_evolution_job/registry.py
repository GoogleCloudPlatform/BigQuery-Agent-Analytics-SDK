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

"""Agent registry: which agents the job may evolve, and where.

The registry is a JSON file (``AGENT_REGISTRY`` env var) describing the
host repo's evolvable agents:

.. code-block:: json

    {
      "repo_root": ".",
      "default_app_name": "my_app",
      "agents": {
        "supervisor": {
          "skill_dir": "agents/supervisor/skill",
          "label": "Supervisor",
          "order": 0
        },
        "policy_agent": {
          "skill_dir": "agents/policy_agent/skill",
          "label": "Policy Agent",
          "order": 1,
          "app_name": "policy_app",
          "skill_id": "policy-qa"
        }
      }
    }

Path resolution (all relative paths anchor inside the single host-repo
workdir from :mod:`skill_evolution_job.config`, falling back to the
registry file's own directory in dry-run mode without a workdir):

* ``AGENT_REGISTRY`` itself: absolute, or relative to the workdir.
* ``repo_root``: absolute, or relative to the workdir; defaults to the
  workdir (the cloned host repo).
* ``agents.<name>.skill_dir``: absolute, or relative to ``repo_root``.
  Must contain ``SKILL.md``.

Loading is lazy — nothing is read at import time — so the container can
start (and ``--test`` can report a useful error) even when the registry
is missing or malformed.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os

from . import config

logger = logging.getLogger(__name__)


class RegistryError(ValueError):
  """Raised when agent_registry.json is missing or malformed."""


@dataclasses.dataclass(frozen=True)
class AgentSpec:
  """One evolvable agent from agent_registry.json."""

  name: str
  skill_dir: str  # absolute, resolved
  label: str
  order: int
  app_name: str | None = None
  skill_id: str | None = None


@dataclasses.dataclass(frozen=True)
class Registry:
  """Parsed, path-resolved agent registry."""

  path: str
  repo_root: str
  default_app_name: str | None
  agents: dict[str, AgentSpec]

  @property
  def default_agent(self) -> str:
    return self.ordered_names()[0]

  def ordered_names(self) -> list[str]:
    """Agent names sorted by ``order`` (ties broken by name).

    ``EVOLUTION_ORDER`` (comma-separated names) overrides the registry's
    ``order`` fields entirely when set.
    """
    override = config.get_config().evolution_order
    if override:
      names = [n.strip() for n in override.split(",") if n.strip()]
      unknown = [n for n in names if n not in self.agents]
      if unknown:
        raise RegistryError(
            f"EVOLUTION_ORDER names unknown agents {unknown}; registry has"
            f" {sorted(self.agents)}"
        )
      return names
    return sorted(self.agents, key=lambda n: (self.agents[n].order, n))

  def agent(self, name: str) -> AgentSpec:
    if name not in self.agents:
      raise RegistryError(
          f"Unknown agent {name!r}; registry {self.path} has"
          f" {sorted(self.agents)}"
      )
    return self.agents[name]

  def resolve_skill_dir(self, name_or_path: str) -> str:
    """Resolve an agent-name shortcut to its skill dir; pass paths through."""
    if name_or_path in self.agents:
      return self.agents[name_or_path].skill_dir
    return name_or_path

  def app_name_for(self, agent_name: str | None = None) -> str | None:
    """App name for quality reports: agent override → registry default."""
    if agent_name and agent_name in self.agents:
      spec = self.agents[agent_name]
      if spec.app_name:
        return spec.app_name
    return self.default_app_name


_registry: Registry | None = None


def reset_cache() -> None:
  """Forget the cached registry (tests, and after AGENT_REGISTRY changes)."""
  global _registry
  _registry = None


def registry_path() -> str:
  """Resolve the AGENT_REGISTRY path (absolute)."""
  cfg = config.get_config()
  path = cfg.agent_registry
  if not path:
    raise RegistryError(
        "AGENT_REGISTRY is not set. Point it at the host repo's"
        " agent_registry.json (see agent_registry.example.json)."
    )
  if not os.path.isabs(path):
    base = config.workdir_or_none() or os.getcwd()
    path = os.path.join(base, path)
  return os.path.abspath(path)


def load_registry(path: str | None = None) -> Registry:
  """Parse and validate agent_registry.json (no caching)."""
  path = os.path.abspath(path) if path else registry_path()
  if not os.path.isfile(path):
    raise RegistryError(
        f"Agent registry not found at {path}. Set AGENT_REGISTRY to a valid"
        " registry file inside the host repo."
    )
  try:
    with open(path) as f:
      raw = json.load(f)
  except json.JSONDecodeError as exc:
    raise RegistryError(f"Agent registry {path} is not valid JSON: {exc}")
  if not isinstance(raw, dict) or not isinstance(raw.get("agents"), dict):
    raise RegistryError(
        f"Agent registry {path} must be a JSON object with an 'agents'"
        " object."
    )
  if not raw["agents"]:
    raise RegistryError(f"Agent registry {path} lists no agents.")

  anchor = config.workdir_or_none() or os.path.dirname(path)
  repo_root = raw.get("repo_root") or anchor
  if not os.path.isabs(repo_root):
    repo_root = os.path.join(anchor, repo_root)
  repo_root = os.path.abspath(repo_root)

  agents: dict[str, AgentSpec] = {}
  for index, (name, spec) in enumerate(raw["agents"].items()):
    if not isinstance(spec, dict) or not spec.get("skill_dir"):
      raise RegistryError(
          f"Agent {name!r} in {path} must be an object with a 'skill_dir'."
      )
    skill_dir = spec["skill_dir"]
    if not os.path.isabs(skill_dir):
      skill_dir = os.path.join(repo_root, skill_dir)
    skill_dir = os.path.abspath(skill_dir)
    try:
      order = int(spec.get("order", index))
    except (TypeError, ValueError):
      raise RegistryError(
          f"Agent {name!r} in {path} has a non-integer 'order':"
          f" {spec.get('order')!r}"
      )
    agents[name] = AgentSpec(
        name=name,
        skill_dir=skill_dir,
        label=spec.get("label", name),
        order=order,
        app_name=spec.get("app_name"),
        skill_id=spec.get("skill_id"),
    )

  registry = Registry(
      path=path,
      repo_root=repo_root,
      default_app_name=raw.get("default_app_name"),
      agents=agents,
  )
  logger.info("Loaded agent registry from %s (%d agents)", path, len(agents))
  return registry


def get_registry(force_reload: bool = False) -> Registry:
  """Lazy, cached registry access — call at point of use, never at import."""
  global _registry
  if _registry is None or force_reload:
    _registry = load_registry()
  return _registry
