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

"""Create or reset the Vertex AI prompt for the demo agent.

Creates a new Vertex AI Prompt Registry resource with the V1 prompt
text and writes the prompt ID to ``.env`` and ``config.json``.

If a prompt already exists (``VERTEX_PROMPT_ID`` in .env), it is
deleted first so the new prompt starts at version 1.

Usage:
    python setup_vertex.py
"""

import argparse
import importlib.util
import json
import os

from google.genai.types import Content
from google.genai.types import Part
from vertexai import Client
from vertexai._genai.types.common import Prompt
from vertexai._genai.types.common import PromptData

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_ENV_PATH = os.path.join(_SCRIPT_DIR, ".env")
_CONFIG_PATH = os.path.join(_SCRIPT_DIR, "config.json")


def _load_config() -> dict:
  """Load config.json."""
  with open(_CONFIG_PATH) as f:
    return json.load(f)


def _read_v1_prompt(config: dict) -> str:
  """Read the V1 prompt text from the prompts file in config."""
  prompts_path = os.path.join(_SCRIPT_DIR, config["prompts_path"])
  spec = importlib.util.spec_from_file_location("prompts", prompts_path)
  mod = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(mod)
  return mod.PROMPT_V1.strip()


def _get_existing_prompt_id() -> str:
  """Read VERTEX_PROMPT_ID from .env if it exists."""
  if not os.path.exists(_ENV_PATH):
    return ""
  with open(_ENV_PATH) as f:
    for line in f:
      line = line.strip()
      if line.startswith("VERTEX_PROMPT_ID="):
        return line.split("=", 1)[1].strip()
  return ""


def _update_env(prompt_id: str) -> None:
  """Write VERTEX_PROMPT_ID to .env."""
  if os.path.exists(_ENV_PATH):
    with open(_ENV_PATH) as f:
      lines = f.readlines()
  else:
    lines = []

  new_line = f"VERTEX_PROMPT_ID={prompt_id}\n"
  found = False
  for i, line in enumerate(lines):
    if line.startswith("VERTEX_PROMPT_ID="):
      lines[i] = new_line
      found = True
      break

  if not found:
    if lines and not lines[-1].endswith("\n"):
      lines[-1] += "\n"
    lines.append(new_line)

  with open(_ENV_PATH, "w") as f:
    f.writelines(lines)


def _update_config_json(config: dict, prompt_id: str) -> None:
  """Write vertex_prompt_id to config.json."""
  config["vertex_prompt_id"] = prompt_id
  with open(_CONFIG_PATH, "w") as f:
    json.dump(config, f, indent=2)
    f.write("\n")


def main() -> None:
  parser = argparse.ArgumentParser(
      description="Create or reset the Vertex AI prompt"
  )
  parser.parse_args()

  config = _load_config()
  v1_text = _read_v1_prompt(config)
  model_id = config.get("model_id", "gemini-2.5-flash")
  app_name = config.get("app_name", "agent")
  location = config.get("vertex_location", "us-central1")
  print(f"  Initializing Vertex AI client (location={location})...")
  client = Client(location=location)

  existing_id = _get_existing_prompt_id()

  # Delete old prompt so we get a clean v1 (no leftover versions)
  if existing_id:
    print(f"  Deleting old prompt {existing_id}...")
    try:
      client.prompts.delete(prompt_id=existing_id, config={"timeout": 300})
      print("  Deleted.")
    except Exception as e:
      print(f"  Warning: could not delete old prompt: {e}")

  # Create fresh prompt with V1 content
  print("  Creating Vertex AI prompt with V1 content...")
  prompt_data = PromptData(
      system_instruction=Content(parts=[Part(text=v1_text)]),
      contents=[Content(role="user", parts=[Part(text="{{user_input}}")])],
      model=model_id,
  )
  prompt = client.prompts.create(
      prompt=Prompt(prompt_data=prompt_data),
      config={"prompt_display_name": app_name, "timeout": 300},
  )
  prompt_id = prompt.prompt_id
  print(f"  Created prompt: {prompt_id}")

  _update_env(prompt_id)
  _update_config_json(config, prompt_id)

  print(f"\nVertex AI Prompt ID: {prompt_id}")
  print("  Updated: .env (VERTEX_PROMPT_ID)")
  print("  Updated: config.json (vertex_prompt_id)")


if __name__ == "__main__":
  main()
