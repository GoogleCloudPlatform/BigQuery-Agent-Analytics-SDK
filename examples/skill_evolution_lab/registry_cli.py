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

"""CLI over the Skill Registry client (create/update/delete/revisions).

Used by setup.sh (create V0) and reset.sh (revert to V0), and handy for
inspecting revisions. All operations are long-running and wait for completion.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_SCRIPT_DIR, "agent"))

from skill_registry import SkillRegistry  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("registry_cli")

_DISPLAY = "Skill Lab Policy Agent"


def main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument(
      "command", choices=["create", "update", "delete", "revisions", "exists"]
  )
  parser.add_argument("--skill-id", required=True)
  parser.add_argument("--skill-dir", default="skills")
  parser.add_argument("--location", default="us-central1")
  parser.add_argument("--project", default=None)
  parser.add_argument("--description", default="Policy agent skill (skill lab)")
  args = parser.parse_args()

  project = (
      args.project
      or os.getenv("GOOGLE_CLOUD_PROJECT")
      or os.getenv("PROJECT_ID")
  )
  reg = SkillRegistry(project, location=args.location)

  if args.command == "create":
    if reg.exists(args.skill_id):
      logger.info("%s already exists; updating instead.", args.skill_id)
      reg.update(
          args.skill_id,
          args.skill_dir,
          display_name=_DISPLAY,
          description=args.description,
      )
    else:
      reg.create(
          args.skill_id,
          args.skill_dir,
          display_name=_DISPLAY,
          description=args.description,
      )
    logger.info("revisions: %d", len(reg.list_revisions(args.skill_id)))
  elif args.command == "update":
    reg.update(
        args.skill_id,
        args.skill_dir,
        display_name=_DISPLAY,
        description=args.description,
    )
    logger.info("revisions: %d", len(reg.list_revisions(args.skill_id)))
  elif args.command == "delete":
    if reg.exists(args.skill_id):
      reg.delete(args.skill_id)
      logger.info("deleted %s", args.skill_id)
    else:
      logger.info("%s does not exist; nothing to delete.", args.skill_id)
  elif args.command == "revisions":
    revs = reg.list_revisions(args.skill_id)
    logger.info("%s has %d revision(s):", args.skill_id, len(revs))
    for r in revs:
      logger.info(
          "  %s  %s", r.get("name", "").split("/")[-1], r.get("createTime")
      )
  elif args.command == "exists":
    logger.info("%s", reg.exists(args.skill_id))


if __name__ == "__main__":
  main()
