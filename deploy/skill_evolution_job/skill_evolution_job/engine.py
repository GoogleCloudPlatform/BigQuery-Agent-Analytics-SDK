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

"""Locator and compatibility adapter for the SDK evolution engine.

The engine is ``scripts/skill_evolution.py``. It is looked up in order:

1. ``SDK_SCRIPTS_DIR`` (set to ``/app/scripts`` in the container image),
2. ``/app/scripts`` (container default),
3. ``<repo>/scripts`` relative to this file (development checkout).

``evolve_skill_compat`` feature-detects the engine's ``evolve_skill``
keyword arguments via ``inspect.signature`` and silently-but-loudly
(INFO log) drops the ones the resolved engine does not support. This
lets the same component run against today's engine and automatically
pick up newer keyword arguments (e.g. ``error_analyst_fn`` /
``incumbent_score`` from the agentic-analyst engine work) once they
land — no component change needed.

Semantic consequence worth knowing: on an engine WITHOUT
``incumbent_score``, the engine re-scores the incumbent skill through
``score_fn`` itself, roughly doubling scoring cost per evolution run.
Acceptable, but budget for it.
"""

from __future__ import annotations

import importlib.util
import inspect
import logging
import os
import sys
import types

logger = logging.getLogger(__name__)

_ENGINE_FILE = "skill_evolution.py"
_MODULE_NAME = "sdk_skill_evolution"

_engine: types.ModuleType | None = None


def reset_cache() -> None:
  """Forget the cached engine module (tests only)."""
  global _engine
  _engine = None
  sys.modules.pop(_MODULE_NAME, None)


def _candidate_dirs() -> list[str]:
  dirs = []
  env_dir = os.environ.get("SDK_SCRIPTS_DIR", "").strip()
  if env_dir:
    dirs.append(env_dir)
  dirs.append("/app/scripts")
  # Development checkout: <repo>/deploy/skill_evolution_job/skill_evolution_job
  repo_root = os.path.normpath(
      os.path.join(os.path.dirname(os.path.abspath(__file__)), "../../..")
  )
  dirs.append(os.path.join(repo_root, "scripts"))
  return dirs


def engine_path() -> str:
  """Locate scripts/skill_evolution.py; raise with the searched paths."""
  searched = []
  for directory in _candidate_dirs():
    path = os.path.join(directory, _ENGINE_FILE)
    if os.path.isfile(path):
      return path
    searched.append(path)
  raise FileNotFoundError(
      "Cannot locate the evolution engine (scripts/skill_evolution.py)."
      f" Searched: {searched}. Set SDK_SCRIPTS_DIR to the directory"
      " containing the SDK's scripts/."
  )


def load_engine(force_reload: bool = False) -> types.ModuleType:
  """Import the engine module from file (lazy, cached)."""
  global _engine
  if _engine is not None and not force_reload:
    return _engine
  path = engine_path()
  spec = importlib.util.spec_from_file_location(_MODULE_NAME, path)
  if spec is None or spec.loader is None:
    raise ImportError(f"Cannot build an import spec for {path}")
  module = importlib.util.module_from_spec(spec)
  sys.modules[_MODULE_NAME] = module
  spec.loader.exec_module(module)
  logger.info("Loaded evolution engine from %s", path)
  _engine = module
  return module


def supported_kwargs() -> set[str]:
  """Keyword parameters accepted by the resolved engine's evolve_skill."""
  engine = load_engine()
  signature = inspect.signature(engine.evolve_skill)
  if any(
      p.kind is inspect.Parameter.VAR_KEYWORD
      for p in signature.parameters.values()
  ):
    return set()  # empty sentinel: engine takes **kwargs, pass everything
  return set(signature.parameters)


def evolve_skill_compat(*args, **kwargs):
  """Call the engine's evolve_skill, dropping unsupported kwargs.

  Positional arguments pass through untouched. Keyword arguments not in
  the resolved engine's signature are dropped with an INFO log naming
  each one — the single compatibility choke point for every evolve/
  coevolve/bottleneck path in this package.
  """
  engine = load_engine()
  supported = supported_kwargs()
  if supported:
    dropped = sorted(k for k in kwargs if k not in supported)
    if dropped:
      logger.info(
          "Engine at %s does not support kwargs %s — dropping them"
          " (upgrade scripts/skill_evolution.py to use them).",
          engine.__file__,
          dropped,
      )
    kwargs = {k: v for k, v in kwargs.items() if k in supported}
  return engine.evolve_skill(*args, **kwargs)
