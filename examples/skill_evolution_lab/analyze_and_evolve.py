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

"""Evolve the SKILL.md from a scored quality report.

This is a thin wrapper around the SDK's reusable evolution engine
(``scripts/skill_evolution.py``) -- the *same* code the knowledge-supervisor
quality lab imports, not a copy. It reads a quality report (produced by
``quality_report.py`` over a V0 traffic run), asks the engine for an improved
skill, writes the result to the local working copy, and -- optionally --
mirrors it to the Skill Registry as a new immutable revision (V1).

Usage:
  python analyze_and_evolve.py \
      --report run/v0_evolve_report.json \
      --skill skills/SKILL.md \
      --model gemini-3.1-pro-preview \
      -o run/v1_skill.md \
      [--registry-update --skill-id skill-lab-policy --location us-central1]
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys

import _quiet  # noqa: F401  -- mute noisy warnings/loggers before google imports

# Import the reusable engine from the SDK's scripts/ (no copy).
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_SDK_SCRIPTS = os.path.abspath(os.path.join(_SCRIPT_DIR, "..", "..", "scripts"))
if _SDK_SCRIPTS not in sys.path:
  sys.path.insert(0, _SDK_SCRIPTS)

import skill_evolution  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("analyze_and_evolve")


def _location_for(model: str) -> str:
  """Vertex location for the analyst model.

  Gemini 3.x and gemini-2.5-pro run on 'global' (most capacity, avoids regional
  429s under the parallel analyst fleet); others fall back to the region.
  """
  if model.startswith("gemini-3") or model == "gemini-2.5-pro":
    return "global"
  return os.getenv("GOOGLE_CLOUD_LOCATION", "us-central1")


def _mirror_to_registry(project: str, skill_path: str, skill_id: str, location):
  """Push the skill directory to the Skill Registry as a new revision."""
  # Lazy import so the registry client (and gcloud) is only needed on demand.
  sys.path.insert(0, os.path.join(_SCRIPT_DIR, "agent"))
  from skill_registry import SkillRegistry  # noqa: E402

  skill_dir = os.path.dirname(os.path.abspath(skill_path))
  reg = SkillRegistry(project, location=location or "us-central1")
  logger.info("Mirroring evolved skill to registry %s ...", skill_id)
  reg.update(
      skill_id,
      skill_dir,
      display_name="Skill Lab Policy Agent",
      description="Evolved tool-first policy skill",
  )
  revs = reg.list_revisions(skill_id)
  logger.info("Registry now has %d revision(s).", len(revs))


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--report", help="Scored V0 report JSON")
  parser.add_argument("--skill", required=True, help="Current SKILL.md path")
  parser.add_argument("-o", "--out", help="Evolved skill output")
  parser.add_argument(
      "--model",
      default="gemini-3.1-pro-preview",
      help=(
          "Analyst/consolidator model. Defaults to gemini-3.1-pro-preview"
          " rather than the engine's generic gemini-2.5-pro default because, on"
          " this task, gemini-2.5-pro baked specific (and wrong) figures into"
          " the skill; 3.1-pro-preview produces clean, tool-first skills."
      ),
  )
  parser.add_argument("--candidates", type=int, default=3)
  parser.add_argument("--max-chars", type=int, default=3500)
  parser.add_argument("--max-workers", type=int, default=10)
  parser.add_argument(
      "--version-label",
      default="v1",
      help=(
          "Version being PRODUCED (artifact filename prefix): 'v1' for the"
          " V0->V1 round, 'v2' for a V1->V2 round, so successive rounds don't"
          " overwrite each other's patches/candidates/selection artifacts"
      ),
  )
  parser.add_argument(
      "--write-working-copy",
      action="store_true",
      help="Also overwrite --skill with the evolved skill (the working copy)",
  )
  parser.add_argument(
      "--registry-update",
      action="store_true",
      help="Mirror the evolved skill to the Skill Registry as a new revision",
  )
  parser.add_argument(
      "--registry-push-only",
      action="store_true",
      help=(
          "Skip evolution entirely; just mirror --skill to the Skill Registry."
          " Used by run_e2e_demo.sh to push V1 only AFTER it wins the held-out"
          " comparison."
      ),
  )
  parser.add_argument("--skill-id", default=None)
  parser.add_argument("--location", default=None, help="Skill Registry region")
  parser.add_argument("--project", default=None)
  parser.add_argument(
      "--eval-spec",
      default=None,
      help=(
          "eval_spec.json; its `tools` field is shown to the analysts so they"
          " can propose 'use the tool' rules instead of NO_PATCH on deflections"
      ),
  )
  args = parser.parse_args()

  project = (
      args.project
      or os.getenv("GOOGLE_CLOUD_PROJECT")
      or os.getenv("PROJECT_ID")
  )

  if args.registry_push_only:
    if not args.skill_id:
      parser.error("--registry-push-only requires --skill-id")
    _mirror_to_registry(project, args.skill, args.skill_id, args.location)
    return
  if not args.report or not args.out:
    parser.error(
        "--report and -o/--out are required (unless --registry-push-only)"
    )

  with open(args.skill) as f:
    current_skill = f.read()

  # Tool-aware analysts are part of the core claim, so a provided --eval-spec
  # must exist and carry a non-empty `tools` field. Fail fast instead of
  # silently degrading to a non-tool-aware run.
  tools = None
  if args.eval_spec:
    if not os.path.exists(args.eval_spec):
      parser.error(f"--eval-spec not found: {args.eval_spec}")
    with open(args.eval_spec) as f:
      tools = (json.load(f) or {}).get("tools") or None
    if not tools:
      parser.error(
          f"--eval-spec {args.eval_spec} has no non-empty `tools` field; "
          "tool-aware analysts need it (remove --eval-spec to run without tools)."
      )

  logger.info(
      "Evolving %s from %s (analyst=%s)...",
      os.path.basename(args.skill),
      os.path.basename(args.report),
      args.model,
  )
  evolved = skill_evolution.evolve_skill(
      args.report,
      current_skill,
      model=args.model,
      project=project,
      location=_location_for(args.model),
      candidates=args.candidates,
      max_chars=args.max_chars,
      max_workers=args.max_workers,
      tools=tools,
      artifacts_dir=os.path.dirname(os.path.abspath(args.out)),
      version_label=args.version_label,
  )

  # Normalize trailing whitespace / EOF so the committed artifact stays clean
  # (model output can carry stray trailing spaces).
  evolved = "\n".join(line.rstrip() for line in evolved.splitlines()) + "\n"
  with open(args.out, "w") as f:
    f.write(evolved)
  changed = evolved.strip() != current_skill.strip()
  logger.info(
      "Wrote evolved skill -> %s (%dB, %s)",
      args.out,
      len(evolved),
      "changed" if changed else "UNCHANGED",
  )

  if args.write_working_copy:
    shutil.copy(args.out, args.skill)
    logger.info("Updated working copy: %s", args.skill)

  if args.registry_update:
    if not args.skill_id:
      parser.error("--registry-update requires --skill-id")
    _mirror_to_registry(project, args.skill, args.skill_id, args.location)


if __name__ == "__main__":
  main()
