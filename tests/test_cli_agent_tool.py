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

"""Executable locks for the selector-aware CLI agent example."""

import json
from unittest.mock import MagicMock
from unittest.mock import patch

from examples import cli_agent_tool


def test_failed_cli_command_parses_structured_json():
  ambiguity = {
      "error": "ambiguous_session",
      "candidate_count": 2,
      "candidates": [{"selector": {"session_id": "s1", "user_id": None}}],
  }
  completed = MagicMock(
      returncode=2,
      stdout=json.dumps(ambiguity),
      stderr="",
  )

  with patch.object(cli_agent_tool.subprocess, "run", return_value=completed):
    result = cli_agent_tool.run_bq_agent_sdk("get-trace", {"session_id": "s1"})

  assert result["error"] == "ambiguous_session"
  assert result["candidates"][0]["selector"]["user_id"] is None
  assert result["exit_code"] == 2


def test_get_session_trace_accepts_retry_selector():
  selector = {
      "session_id": "s1",
      "user_id": None,
      "root_agent_name": "root",
      "experiment_id": None,
  }

  with patch.object(
      cli_agent_tool,
      "run_bq_agent_sdk",
      return_value={"trace_id": "t1"},
  ) as run:
    result = cli_agent_tool.get_session_trace(selector=selector)

  assert result == {"trace_id": "t1"}
  args = run.call_args.args[1]
  assert "session_id" not in args
  assert json.loads(args["selector_json"]) == selector
