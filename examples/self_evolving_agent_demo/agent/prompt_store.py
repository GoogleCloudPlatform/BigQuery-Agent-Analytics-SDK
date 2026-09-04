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

"""Tiny local prompt registry for the demo.

The tracked source stays immutable during a run. The active prompt
version is stored in ``prompt_state.json``, which is ignored by Git and
created by setup/reset/evolution scripts.
"""

from __future__ import annotations

import argparse
from datetime import datetime
from datetime import timezone
import json
import os
from typing import Any

from .prompts import V1_PROMPT

_DEMO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE_PATH = os.path.join(_DEMO_DIR, "prompt_state.json")


def _state(version: str, prompt: str, rationale: str) -> dict[str, Any]:
  return {
      "version": version,
      "prompt": prompt,
      "rationale": rationale,
      "updated_at": datetime.now(timezone.utc).isoformat(),
  }


def read_state() -> dict[str, Any]:
  """Read the current prompt state, falling back to V1."""
  if not os.path.exists(STATE_PATH):
    return _state("v1", V1_PROMPT, "Default V1 prompt.")
  with open(STATE_PATH) as f:
    data = json.load(f)
  version = str(data.get("version", "v1")).lower()
  prompt = str(data.get("prompt") or V1_PROMPT)
  return {
      "version": version,
      "prompt": prompt,
      "rationale": str(data.get("rationale", "")),
      "updated_at": str(data.get("updated_at", "")),
  }


def read_prompt() -> tuple[str, str]:
  """Return ``(prompt, version)`` for agent construction."""
  state = read_state()
  return state["prompt"], state["version"]


def write_prompt(version: str, prompt: str, rationale: str) -> dict[str, Any]:
  """Persist prompt text as the active demo prompt version."""
  normalized = version.strip().lower()
  if normalized not in {"v1", "v2", "candidate"}:
    raise ValueError(f"Unsupported prompt version: {version!r}")
  if not prompt.strip():
    raise ValueError("Prompt text must not be empty.")
  state = _state(normalized, prompt.strip(), rationale)
  with open(STATE_PATH, "w") as f:
    json.dump(state, f, indent=2)
    f.write("\n")
  return state


def reset_state() -> dict[str, Any]:
  """Reset the demo to the intentionally inefficient V1 prompt."""
  return write_prompt("v1", V1_PROMPT, "Reset to baseline V1 prompt.")


def main() -> None:
  parser = argparse.ArgumentParser(description="Manage demo prompt state.")
  parser.add_argument("action", choices=["show", "reset"])
  args = parser.parse_args()

  if args.action == "reset":
    state = reset_state()
  else:
    state = read_state()

  print(json.dumps(state, indent=2))


if __name__ == "__main__":
  main()
