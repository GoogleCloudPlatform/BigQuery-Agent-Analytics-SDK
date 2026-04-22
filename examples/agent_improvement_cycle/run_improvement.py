#!/usr/bin/env python3
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

"""Run the improvement cycle for any ADK agent.

Loads ``config.json`` (or a path given via ``--agent-config``)
to discover the agent module, prompts file, and eval cases.

Usage:
    python run_improvement.py <report.json>
    python run_improvement.py --agent-config /path/to/config.json <report.json>
    python run_improvement.py --from-eval-results <eval_results.json>
"""

import argparse
import asyncio
import os
import sys

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _SCRIPT_DIR)

from agent_improvement import load_config
from agent_improvement import run_improvement

_DEFAULT_CONFIG = os.path.join(_SCRIPT_DIR, "config.json")


def main() -> None:
  parser = argparse.ArgumentParser(
      description="Improve agent prompt based on quality report"
  )
  parser.add_argument(
      "report_json",
      help="Path to the quality report JSON file",
  )
  parser.add_argument(
      "--agent-config",
      type=str,
      default=_DEFAULT_CONFIG,
      help="Path to the agent's config.json (default: config.json)",
  )
  parser.add_argument(
      "--from-eval-results",
      action="store_true",
      help=(
          "Treat report_json as golden eval results (from run_eval.py"
          " --golden) instead of a BigQuery quality report."
      ),
  )
  args = parser.parse_args()

  config = load_config(args.agent_config)
  result = asyncio.run(
      run_improvement(
          config,
          report_path=args.report_json,
          from_eval_results=args.from_eval_results,
      )
  )

  if result["new_version"] == result["old_version"]:
    sys.exit(1)


if __name__ == "__main__":
  main()
