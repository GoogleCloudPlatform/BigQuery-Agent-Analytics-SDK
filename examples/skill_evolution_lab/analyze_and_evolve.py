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
import logging
import os
import shutil
import sys

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


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--report", required=True, help="Scored V0 report JSON")
  parser.add_argument("--skill", required=True, help="Current SKILL.md path")
  parser.add_argument("-o", "--out", required=True, help="Evolved skill output")
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
      "--write-working-copy",
      action="store_true",
      help="Also overwrite --skill with the evolved skill (the working copy)",
  )
  parser.add_argument(
      "--registry-update",
      action="store_true",
      help="Mirror the evolved skill to the Skill Registry as a new revision",
  )
  parser.add_argument("--skill-id", default=None)
  parser.add_argument("--location", default=None, help="Skill Registry region")
  parser.add_argument("--project", default=None)
  args = parser.parse_args()

  project = (
      args.project
      or os.getenv("GOOGLE_CLOUD_PROJECT")
      or os.getenv("PROJECT_ID")
  )
  with open(args.skill) as f:
    current_skill = f.read()

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
    # Lazy import so the registry client (and gcloud) is only needed on demand.
    sys.path.insert(0, os.path.join(_SCRIPT_DIR, "agent"))
    from skill_registry import SkillRegistry  # noqa: E402

    skill_dir = os.path.dirname(os.path.abspath(args.skill))
    reg = SkillRegistry(project, location=args.location or "us-central1")
    logger.info("Mirroring evolved skill to registry %s ...", args.skill_id)
    reg.update(
        args.skill_id,
        skill_dir,
        display_name="Skill Lab Policy Agent",
        description="Evolved (V1) tool-first policy skill",
    )
    revs = reg.list_revisions(args.skill_id)
    logger.info("Registry now has %d revision(s).", len(revs))


if __name__ == "__main__":
  main()
