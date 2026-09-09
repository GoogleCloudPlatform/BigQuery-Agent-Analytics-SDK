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

"""Locator for the SDK evolution engine.

The engine is ``scripts/skill_evolution.py``, baked into the image from
the SDK checkout ``deploy.sh`` runs in. It is looked up in order:

1. ``SDK_SCRIPTS_DIR`` (set to ``/app/scripts`` in the container image),
2. ``/app/scripts`` (container default),
3. ``<repo>/scripts`` relative to this file (development checkout).

Callers use the resolved module's ``evolve_skill`` directly: image and
engine ship together, so the host-hook keyword arguments
(``error_analyst_fn``, ``incumbent_score``, ``analyst_timeout_s``) are
always present.
"""

from __future__ import annotations

import importlib.util
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
