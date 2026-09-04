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

"""Host hooks: the seam for everything the SDK cannot ship generically.

Traffic generation, candidate scoring, publish gates, toolbox
definitions, and registry pushes are host-specific. The job resolves
each hook in this order:

1. **Module** — ``EVOLUTION_HOOKS`` names an importable module; a
   callable attribute with the hook's name wins.
2. **Command** — for ``traffic``/``score``/``gate`` only, a
   ``TRAFFIC_CMD``/``SCORE_CMD``/``GATE_CMD`` shell command with
   ``{run_dir}``/``{report}``/``{candidate}``/``{skill_dir}``/``{agent}``
   placeholders.
3. **Skip** — hook unconfigured; callers log the returned reason and
   continue with the generic behavior (e.g. the engine's size-based
   candidate selection when ``score`` is missing).

Hook callables and their contracts:

- ``traffic(run_dir) -> dict``: generate evaluation traffic.
- ``score(candidate, skill_dir, run_dir) -> dict``: score a candidate
  SKILL.md; must include ``meaningful_rate`` (0-100).
- ``gate(run_dir, version, agent) -> (bool, str)``: publish gate verdict
  and reason. ``GATE_POLICY=require`` makes a missing gate fatal.
- ``toolbox(agent) -> str``: tool descriptions given to error analysts.
- ``error_analyst(client, model, session, skill, tools) -> str``:
  agentic per-failure analyst; wired only when the resolved engine
  supports ``error_analyst_fn``.
- ``publish(skill_dir, run_dir) -> dict``: push the winning skill to a
  host registry after the PR.
"""

from __future__ import annotations

import importlib
import json
import logging
import os
import subprocess
import sys
from typing import Any, Callable

from . import config

logger = logging.getLogger(__name__)

HOOK_NAMES = (
    "traffic",
    "score",
    "gate",
    "toolbox",
    "error_analyst",
    "publish",
)

# Hooks that may also be configured as shell commands.
_CMD_ENV = {
    "traffic": "TRAFFIC_CMD",
    "score": "SCORE_CMD",
    "gate": "GATE_CMD",
}

_CMD_TIMEOUT_S = config._env_int("HOOK_CMD_TIMEOUT_S", 3600)

_module_cache: dict[str, Any] = {}


def reset_cache() -> None:
  """Forget imported hook modules (tests only)."""
  _module_cache.clear()


def _hooks_module():
  """Import the EVOLUTION_HOOKS module, or None when unset/broken.

  A module that is not importable from the job's own path is retried
  with the host-repo workdir on ``sys.path``: hook adapters naturally
  live in the host agent repo, which the job clones at runtime.
  """
  module_path = config.get_config().evolution_hooks
  if not module_path:
    return None
  if module_path in _module_cache:
    return _module_cache[module_path]
  try:
    module = importlib.import_module(module_path)
  except ModuleNotFoundError as exc:
    workdir = config.workdir_or_none()
    if not workdir or workdir in sys.path:
      raise RuntimeError(
          f"EVOLUTION_HOOKS={module_path!r} failed to import: {exc}"
      ) from exc
    logger.info(
        "EVOLUTION_HOOKS %r not importable directly — retrying with the"
        " host-repo workdir %s on sys.path.",
        module_path,
        workdir,
    )
    sys.path.insert(0, workdir)
    try:
      module = importlib.import_module(module_path)
    except Exception as retry_exc:  # noqa: BLE001 - import failure is terminal
      raise RuntimeError(
          f"EVOLUTION_HOOKS={module_path!r} failed to import (also tried"
          f" with workdir {workdir} on sys.path): {retry_exc}"
      ) from retry_exc
  except Exception as exc:  # noqa: BLE001 - any import failure is terminal
    raise RuntimeError(
        f"EVOLUTION_HOOKS={module_path!r} failed to import: {exc}"
    ) from exc
  _module_cache[module_path] = module
  return module


def substitute(command: str, values: dict[str, str]) -> str:
  """Replace ``{placeholder}`` tokens; unknown tokens are left alone."""
  for key, value in values.items():
    command = command.replace("{" + key + "}", str(value))
  return command


def _run_cmd(name: str, command: str, values: dict[str, str]) -> dict:
  """Run a *_CMD hook; returns exit code, output tail, parsed result."""
  rendered = substitute(command, values)
  logger.info("Running %s hook command: %s", name.upper(), rendered)
  proc = subprocess.run(
      rendered,
      shell=True,
      capture_output=True,
      text=True,
      timeout=_CMD_TIMEOUT_S,
  )
  output = config.mask_tokens(
      (proc.stdout or "") + ("\n" + proc.stderr if proc.stderr else "")
  ).strip()
  result: dict[str, Any] = {
      "returncode": proc.returncode,
      "output_tail": output[-2000:],
  }
  # The last non-empty stdout line may carry structured output.
  for line in reversed((proc.stdout or "").strip().splitlines()):
    line = line.strip()
    if not line:
      continue
    try:
      parsed = json.loads(line)
      if isinstance(parsed, dict):
        result.update(parsed)
      else:
        result["value"] = parsed
    except (ValueError, TypeError):
      pass
    break
  return result


def _cmd_hook(name: str, command: str) -> Callable:
  """Wrap a shell command as the hook callable for ``name``."""

  if name == "traffic":

    def traffic(run_dir: str) -> dict:
      return _run_cmd(name, command, {"run_dir": run_dir})

    return traffic

  if name == "score":

    def score(candidate: str, skill_dir: str, run_dir: str) -> dict:
      result = _run_cmd(
          name,
          command,
          {
              "candidate": candidate,
              "skill_dir": skill_dir,
              "run_dir": run_dir,
          },
      )
      if result["returncode"] != 0:
        raise RuntimeError(
            f"SCORE_CMD failed (exit {result['returncode']}):"
            f" {result['output_tail'][-500:]}"
        )
      if "meaningful_rate" not in result and "value" in result:
        try:
          result["meaningful_rate"] = float(result["value"])
        except (TypeError, ValueError):
          pass
      if "meaningful_rate" not in result:
        raise RuntimeError(
            "SCORE_CMD produced no meaningful_rate: last stdout line must"
            " be a number or a JSON object with 'meaningful_rate'."
        )
      return result

    return score

  if name == "gate":

    def gate(run_dir: str, version: str, agent: str) -> tuple[bool, str]:
      result = _run_cmd(
          name,
          command,
          {"run_dir": run_dir, "version": version, "agent": agent},
      )
      passed = result["returncode"] == 0
      return passed, result["output_tail"][-1000:]

    return gate

  raise ValueError(f"Hook {name!r} has no command form")


def get_hook(name: str) -> tuple[Callable | None, str]:
  """Resolve a hook: (callable, source) or (None, skip reason)."""
  if name not in HOOK_NAMES:
    raise ValueError(f"Unknown hook {name!r}; expected one of {HOOK_NAMES}")

  module = _hooks_module()
  if module is not None:
    candidate = getattr(module, name, None)
    if callable(candidate):
      return candidate, f"module {module.__name__}.{name}"

  cmd_env = _CMD_ENV.get(name)
  if cmd_env:
    command = os.environ.get(cmd_env, "").strip()
    if command:
      return _cmd_hook(name, command), f"command {cmd_env}"

  if module is not None and cmd_env:
    reason = (
        f"hook '{name}' not configured (EVOLUTION_HOOKS module has no"
        f" callable '{name}' and {cmd_env} is unset)"
    )
  elif module is not None:
    reason = (
        f"hook '{name}' not configured (EVOLUTION_HOOKS module has no"
        f" callable '{name}')"
    )
  elif cmd_env:
    reason = f"hook '{name}' not configured (set EVOLUTION_HOOKS or {cmd_env})"
  else:
    reason = f"hook '{name}' not configured (set EVOLUTION_HOOKS)"
  return None, reason
