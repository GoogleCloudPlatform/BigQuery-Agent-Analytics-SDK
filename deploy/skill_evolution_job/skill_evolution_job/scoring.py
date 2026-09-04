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

"""Shared score-hook validation and temporary candidate installation."""

from __future__ import annotations

import json
import logging
import math
from pathlib import Path
import tempfile

logger = logging.getLogger(__name__)


def normalize_score_result(result) -> float | None:
  """Return a measured finite rate, or None for an unusable evaluation."""
  if not isinstance(result, dict):
    return None
  if "returncode" in result and result["returncode"] != 0:
    return None
  if (
      str(result.get("status", "")).lower()
      in {"error", "failed", "skipped", "unmeasurable"}
      or result.get("error")
      or result.get("skipped")
      or result.get("unmeasurable")
  ):
    return None
  raw = result.get("meaningful_rate")
  if raw is None or isinstance(raw, bool):
    return None
  try:
    rate = float(raw)
  except (TypeError, ValueError, OverflowError):
    return None
  if not math.isfinite(rate):
    return None

  report_path = result.get("report_path")
  if report_path:
    try:
      with open(report_path) as stream:
        report = json.load(stream)
      summary = report.get("summary") or {}
      excluded = summary.get("excluded_error_shaped") or {}
      if int(excluded.get("count", 0)) != 0:
        return None
    except (OSError, TypeError, ValueError, AttributeError, OverflowError):
      logger.warning("Cannot validate scored report %r", report_path)
      return None
  return rate


def make_score_fn(score_hook, skill_dir: str, run_dir: str):
  """Adapt a host hook while restoring the incumbent after every score.

  Hooks may install the candidate into SKILL.md to evaluate it. Each call
  restores the exact incumbent bytes even if the hook raises, so no later
  candidate or retry inherits an unaccepted installation. Distinct files
  keep every evaluation's associated reports available for inspection.
  """
  skill_path = Path(skill_dir) / "SKILL.md"
  incumbent = skill_path.read_bytes()
  Path(run_dir).mkdir(parents=True, exist_ok=True)

  def score(skill_content: str) -> float | None:
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        prefix="_score_candidate_",
        suffix=".md",
        dir=run_dir,
        delete=False,
    ) as candidate:
      candidate.write(skill_content)
      candidate_path = candidate.name
    try:
      result = score_hook(candidate_path, skill_dir, run_dir)
      return normalize_score_result(result)
    finally:
      skill_path.write_bytes(incumbent)

  return score
