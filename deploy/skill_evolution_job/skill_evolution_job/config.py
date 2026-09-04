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

"""Environment contract for the skill-evolution job.

Single source of truth for the environment variables the component
reads, plus the process-wide git workdir: exactly ONE clone of the host
agent repository (``GITHUB_REPO``) per process, created on first use and
cached. Evolution edits skills inside that clone and
``create_evolution_pr`` branches/commits against the same clone, so the
evolved SKILL.md is always in the directory that gets committed.

Without ``GITHUB_REPO`` (and without an explicit ``EVOLUTION_WORKDIR``)
the job runs in dry-run mode: quality report → evolution → artifacts to
GCS, no PR.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import subprocess
import tempfile
import typing

logger = logging.getLogger(__name__)


@typing.overload
def _env(name: str) -> str | None:
  ...


@typing.overload
def _env(name: str, default: str) -> str:
  ...


def _env(name: str, default: str | None = None) -> str | None:
  value = os.environ.get(name)
  if value is None or not value.strip():
    return default
  return value.strip()


def _env_bool(name: str, default: bool = False) -> bool:
  value = _env(name)
  if value is None:
    return default
  return value.lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
  value = _env(name)
  if value is None:
    return default
  try:
    return int(value)
  except ValueError:
    logger.warning("Ignoring non-integer %s=%r; using %d", name, value, default)
    return default


def _env_float(name: str) -> float | None:
  value = _env(name)
  if value is None:
    return None
  try:
    return float(value)
  except ValueError:
    logger.warning("Ignoring non-numeric %s=%r", name, value)
    return None


@dataclasses.dataclass(frozen=True)
class JobConfig:
  """Snapshot of the job's environment contract.

  Every field maps 1:1 to an environment variable (documented in
  deploy/skill_evolution_job/README.md). Built fresh on each
  :func:`get_config` call so orchestrator-exported binding variables
  (set in ``main.py`` after argument parsing) are picked up.
  """

  # --- BigQuery / quality report -------------------------------------
  project_id: str | None
  region: str | None
  dataset_id: str | None
  dataset_location: str | None
  quality_app_name: str | None  # falls back to registry default_app_name
  agent_version: str | None
  evolution_trace_labels: str | None
  eval_time_period: str
  min_sessions: int
  quality_threshold: float | None

  # --- Models ----------------------------------------------------------
  skill_evolution_model_id: str  # the orchestrator agent's own model
  evolution_model_id: str  # engine consolidation model
  eval_model_id: str | None
  model_location: str | None

  # --- Component -------------------------------------------------------
  agent_registry: str | None
  sdk_scripts_dir: str | None
  evolution_mode: str  # evolve | coevolve
  evolution_target_agents: str | None
  evolution_order: str | None
  evolution_toolbox: str | None  # inline text, or @/path/to/file

  # --- Host hooks --------------------------------------------------------
  evolution_hooks: str | None  # import path of a hooks module
  traffic_cmd: str | None
  score_cmd: str | None
  gate_cmd: str | None
  gate_policy: str  # skip | require

  # --- GitHub / publish --------------------------------------------------
  github_repo: str | None
  github_base_branch: str
  evolution_publish: bool
  git_user_name: str
  git_user_email: str
  evolution_workdir: str | None
  evolution_gcs_bucket: str | None

  # --- Orchestrator inputs -----------------------------------------------
  quality_source: str  # bigquery | synthetic (synthetic needs traffic hook)
  full_loop: bool
  quality_report_path: str | None
  issue_number: str | None


def get_config() -> JobConfig:
  """Read the environment contract. Cheap; call at point of use."""
  return JobConfig(
      project_id=_env("PROJECT_ID") or _env("GOOGLE_CLOUD_PROJECT"),
      region=_env("REGION") or _env("GOOGLE_CLOUD_LOCATION"),
      dataset_id=_env("DATASET_ID"),
      dataset_location=_env("DATASET_LOCATION"),
      quality_app_name=_env("QUALITY_APP_NAME"),
      agent_version=_env("AGENT_VERSION"),
      evolution_trace_labels=_env("EVOLUTION_TRACE_LABELS"),
      eval_time_period=_env("EVAL_TIME_PERIOD", "7d"),
      min_sessions=_env_int("MIN_SESSIONS", 20),
      quality_threshold=_env_float("QUALITY_THRESHOLD"),
      skill_evolution_model_id=_env(
          "SKILL_EVOLUTION_MODEL_ID", "gemini-2.5-pro"
      ),
      evolution_model_id=_env("EVOLUTION_MODEL_ID", "gemini-2.5-pro"),
      eval_model_id=_env("EVAL_MODEL_ID"),
      model_location=_env("MODEL_LOCATION"),
      agent_registry=_env("AGENT_REGISTRY"),
      sdk_scripts_dir=_env("SDK_SCRIPTS_DIR"),
      evolution_mode=_env("EVOLUTION_MODE", "evolve"),
      evolution_target_agents=_env("EVOLUTION_TARGET_AGENTS"),
      evolution_order=_env("EVOLUTION_ORDER"),
      evolution_toolbox=_env("EVOLUTION_TOOLBOX"),
      evolution_hooks=_env("EVOLUTION_HOOKS"),
      traffic_cmd=_env("TRAFFIC_CMD"),
      score_cmd=_env("SCORE_CMD"),
      gate_cmd=_env("GATE_CMD"),
      gate_policy=_env("GATE_POLICY", "skip"),
      github_repo=_env("GITHUB_REPO"),
      github_base_branch=_env("GITHUB_BASE_BRANCH", "main"),
      evolution_publish=_env_bool("EVOLUTION_PUBLISH", False),
      git_user_name=_env("GIT_USER_NAME", "skill-evolution-job"),
      git_user_email=_env(
          "GIT_USER_EMAIL", "skill-evolution-job@users.noreply.github.com"
      ),
      evolution_workdir=_env("EVOLUTION_WORKDIR"),
      evolution_gcs_bucket=_env("EVOLUTION_GCS_BUCKET") or _env("GCS_BUCKET"),
      quality_source=_env("QUALITY_SOURCE", "bigquery"),
      full_loop=_env_bool("FULL_LOOP", False),
      quality_report_path=_env("QUALITY_REPORT_PATH"),
      issue_number=_env("ISSUE_NUMBER"),
  )


def mask_tokens(text: str) -> str:
  """Mask any GitHub credential that git/gh may echo into stderr.

  git prints the full remote URL — embedded token included — in
  clone/push/fetch failure messages. Every stderr string that becomes a
  tool result (and thus reaches the agent context, Cloud Logging, and
  GCS run archives) must pass through here first.
  """
  if not text:
    return text
  for var in ("GH_TOKEN", "GITHUB_TOKEN"):
    token = os.environ.get(var)
    if token:
      text = text.replace(token, "***")
  return text


# Process-wide cached workdir (see module docstring).
_workdir: str | None = None


def reset_workdir_cache() -> None:
  """Forget the cached workdir (tests only)."""
  global _workdir
  _workdir = None


def workdir_or_none() -> str | None:
  """Return the host-repo workdir, or None in dry-run mode.

  Resolution order:
    1. ``EVOLUTION_WORKDIR`` pointing at an existing git checkout
       (local development / lab adapter).
    2. One ``--depth 1`` clone of ``GITHUB_REPO`` at
       ``GITHUB_BASE_BRANCH`` into a tempdir, cached for the process.
    3. Neither configured → None (dry-run mode: no PR path).
  """
  global _workdir
  if _workdir:
    return _workdir

  cfg = get_config()
  if cfg.evolution_workdir:
    path = os.path.abspath(cfg.evolution_workdir)
    # ``.git`` is a directory in a clone and a FILE in a git worktree;
    # both are checkouts the job can commit in.
    if not os.path.exists(os.path.join(path, ".git")):
      raise RuntimeError(
          f"EVOLUTION_WORKDIR={cfg.evolution_workdir!r} is not a git"
          " checkout. Point it at a clone of the host agent repo, or unset"
          " it to let the job clone GITHUB_REPO itself."
      )
    _workdir = path
    logger.info("Workdir: existing checkout %s", path)
    return _workdir

  if not cfg.github_repo:
    return None

  _workdir = _clone_host_repo(cfg)
  return _workdir


def workdir() -> str:
  """Return the host-repo workdir; raise when unconfigured."""
  path = workdir_or_none()
  if path is None:
    raise RuntimeError(
        "No git workdir: set GITHUB_REPO (plus GH_TOKEN for private repos"
        " and pushes) or EVOLUTION_WORKDIR. Without either, the job runs"
        " in dry-run mode and cannot touch a host repo."
    )
  return path


def _clone_host_repo(cfg: JobConfig) -> str:
  """Clone GITHUB_REPO once; the single site where GH_TOKEN enters a URL."""
  token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
  if token:
    clone_url = (
        f"https://x-access-token:{token}@github.com/{cfg.github_repo}.git"
    )
  else:
    clone_url = f"https://github.com/{cfg.github_repo}.git"
  dest = tempfile.mkdtemp(prefix="skill_evolution_workdir_")
  result = subprocess.run(
      [
          "git",
          "clone",
          "--depth",
          "1",
          "--branch",
          cfg.github_base_branch,
          clone_url,
          dest,
      ],
      capture_output=True,
      text=True,
  )
  if result.returncode != 0:
    # The clone URL embeds the token — never surface stderr unmasked.
    raise RuntimeError(
        f"Clone of {cfg.github_repo}@{cfg.github_base_branch} failed: "
        f"{mask_tokens(result.stderr)[-500:]}"
    )
  for key, value in (
      ("user.name", cfg.git_user_name),
      ("user.email", cfg.git_user_email),
  ):
    subprocess.run(["git", "config", key, value], cwd=dest, capture_output=True)
  logger.info(
      "Workdir: fresh clone of %s@%s at %s",
      cfg.github_repo,
      cfg.github_base_branch,
      dest,
  )
  return dest
