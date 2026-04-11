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

"""Example: ADK agent that uses bq-agent-sdk CLI for self-diagnostics.

This shows how an agent can call the SDK CLI as a tool to inspect its
own traces, run evaluations, and generate insights — enabling
self-monitoring and adaptive behavior.

Prerequisites:
    pip install bigquery-agent-analytics google-adk

Usage:
    export BQ_AGENT_PROJECT=my-project
    export BQ_AGENT_DATASET=agent_analytics
    python examples/cli_agent_tool.py
"""

from __future__ import annotations

import json
import subprocess

from google.adk import Agent


def run_bq_agent_sdk(command: str, args: dict) -> dict:
  """Run a bq-agent-sdk CLI command and return parsed JSON output.

  Args:
      command: CLI command name (e.g. "doctor", "evaluate", "insights").
      args: Dict of CLI arguments (keys without leading dashes).

  Returns:
      Parsed JSON output from the command.
  """
  cmd = ["bq-agent-sdk", command, "--format=json"]
  for key, value in args.items():
    flag = key.replace("_", "-")
    if isinstance(value, bool):
      if value:
        cmd.append(f"--{flag}")
    else:
      cmd.append(f"--{flag}={value}")

  result = subprocess.run(
      cmd,
      capture_output=True,
      text=True,
      check=False,
  )
  if result.returncode == 0:
    return json.loads(result.stdout)
  return {"error": result.stderr.strip(), "exit_code": result.returncode}


# -- Agent tool definitions --


def check_health() -> dict:
  """Check SDK connectivity and data health."""
  return run_bq_agent_sdk("doctor", {})


def evaluate_recent_sessions(
    evaluator: str = "latency",
    last: str = "1h",
    limit: int = 50,
) -> dict:
  """Evaluate recent sessions with a given evaluator."""
  return run_bq_agent_sdk(
      "evaluate",
      {
          "evaluator": evaluator,
          "last": last,
          "limit": limit,
      },
  )


def get_insights(last: str = "24h") -> dict:
  """Generate an insights report over recent traces."""
  return run_bq_agent_sdk("insights", {"last": last})


def get_session_trace(session_id: str) -> dict:
  """Retrieve the full trace for a specific session."""
  return run_bq_agent_sdk(
      "get-trace",
      {"session_id": session_id},
  )


# -- Example agent setup --


def create_self_monitoring_agent():
  """Create an ADK agent with self-monitoring tools."""
  agent = Agent(
      model="gemini-2.0-flash",
      name="self_monitoring_agent",
      instruction=(
          "You are an agent that can monitor your own performance. "
          "Use the available tools to check health, evaluate recent "
          "sessions, get insights, and inspect specific traces. "
          "When asked about your performance, use these tools to "
          "provide data-driven answers."
      ),
      tools=[
          check_health,
          evaluate_recent_sessions,
          get_insights,
          get_session_trace,
      ],
  )
  return agent


if __name__ == "__main__":
  agent = create_self_monitoring_agent()
  print("Self-monitoring agent created with tools:")
  for tool in [
      check_health,
      evaluate_recent_sessions,
      get_insights,
      get_session_trace,
  ]:
    print(f"  - {tool.__name__}: {tool.__doc__}")
  print()
  print("Example usage:")
  print("  result = check_health()")
  print(
      '  result = evaluate_recent_sessions(evaluator="error_rate",'
      ' last="24h")'
  )
