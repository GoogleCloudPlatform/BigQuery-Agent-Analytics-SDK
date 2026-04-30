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

"""Build the decision-lineage property graph from real plugin traces.

Discovers every ``session_id`` already in ``agent_events`` (written by
the BQ AA Plugin during ``run_agent.py``) and runs the SDK's end-to-end
extraction pipeline across all of them in one call:

  ``mgr.build_context_graph(
      session_ids=<all sessions>,
      use_ai_generate=True,
      include_decisions=True,
  )``

Which runs:
  1. ``extract_biz_nodes`` — AI.GENERATE pulls business entities from
     every span in every session.
  2. ``create_cross_links`` — Evaluated edges TechNode -> BizNode.
  3. ``extract_decision_points`` — AI.GENERATE pulls DecisionPoints
     and Candidates with rejection rationale.
  4. ``store_decision_points`` — writes the extracted rows.
  5. ``create_decision_edges`` — MadeDecision + CandidateEdge rows.
  6. ``create_property_graph(include_decisions=True)`` — emits
     ``CREATE OR REPLACE PROPERTY GRAPH`` with all four node labels.

Reports per-step row counts plus a per-session breakdown so you can
confirm AI.GENERATE actually extracted decisions for each session
before opening BigQuery Studio.

Note: AI.GENERATE results are non-deterministic. Re-running may yield
slightly different rejection-rationale wording or candidate counts.
The graph structure (TechNode, BizNode, DecisionPoint, CandidateNode
+ four edge labels) is stable.
"""

from __future__ import annotations

import os
import sys

from dotenv import load_dotenv
from google.cloud import bigquery

from bigquery_agent_analytics.context_graph import ContextGraphConfig
from bigquery_agent_analytics.context_graph import ContextGraphManager

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_env_path = os.path.join(_SCRIPT_DIR, ".env")
if os.path.exists(_env_path):
  load_dotenv(dotenv_path=_env_path)

PROJECT_ID = os.getenv("PROJECT_ID")
DATASET_ID = os.getenv("DATASET_ID", "decision_lineage_rich_demo")
DATASET_LOCATION = os.getenv("DATASET_LOCATION", "us-central1")
TABLE_ID = os.getenv("TABLE_ID", "agent_events")
ENDPOINT = os.getenv("DEMO_AI_ENDPOINT", "gemini-2.5-flash")

if not PROJECT_ID:
  print(
      "ERROR: PROJECT_ID not set. Run ./setup.sh first.",
      file=sys.stderr,
  )
  sys.exit(2)


_DISTINCT_SESSIONS_QUERY = """\
SELECT
  session_id,
  COUNT(*) AS event_count,
  MIN(timestamp) AS first_event_at
FROM `{project}.{dataset}.{table}`
WHERE session_id IS NOT NULL
GROUP BY session_id
ORDER BY first_event_at
"""


def _fetch_sessions(client: bigquery.Client) -> list[tuple[str, int]]:
  query = _DISTINCT_SESSIONS_QUERY.format(
      project=PROJECT_ID, dataset=DATASET_ID, table=TABLE_ID
  )
  rows = list(client.query(query).result())
  return [(r.session_id, int(r.event_count)) for r in rows]


def main() -> int:
  print(f"Project : {PROJECT_ID}")
  print(f"Dataset : {DATASET_ID}")
  print(f"Endpoint: {ENDPOINT}")
  print()

  client = bigquery.Client(project=PROJECT_ID, location=DATASET_LOCATION)

  try:
    sessions = _fetch_sessions(client)
  except Exception as exc:  # pylint: disable=broad-except
    print(
        f"ERROR: could not list sessions in "
        f"{PROJECT_ID}.{DATASET_ID}.{TABLE_ID}: {exc}",
        file=sys.stderr,
    )
    return 1

  if not sessions:
    print(
        "ERROR: no sessions found in agent_events. Did run_agent.py "
        "finish? Re-run ./setup.sh.",
        file=sys.stderr,
    )
    return 1

  print(f"Found {len(sessions)} session(s):")
  for sid, n in sessions:
    print(f"  - {sid}  ({n} events)")
  print()

  session_ids = [sid for sid, _ in sessions]
  print(
      "Running ContextGraphManager.build_context_graph "
      "(AI.GENERATE on, decisions on)..."
  )
  print(
      "AI.GENERATE runs twice across all sessions (biz nodes, then "
      "decisions). Expect ~30-90s."
  )
  print()

  config = ContextGraphConfig(endpoint=ENDPOINT)
  mgr = ContextGraphManager(
      project_id=PROJECT_ID,
      dataset_id=DATASET_ID,
      table_id=TABLE_ID,
      config=config,
      client=client,
      location=DATASET_LOCATION,
  )

  results = mgr.build_context_graph(
      session_ids=session_ids,
      use_ai_generate=True,
      include_decisions=True,
  )

  print()
  print("Pipeline results:")
  for k in (
      "biz_nodes_count",
      "cross_links_created",
      "decision_points_count",
      "decision_points_stored",
      "decision_edges_created",
      "property_graph_created",
  ):
    if k in results:
      print(f"  {k:<28} {results[k]}")

  if not results.get("property_graph_created"):
    print()
    print(
        "WARNING: property_graph_created is False. "
        "Check stderr for the BigQuery error.",
        file=sys.stderr,
    )
    return 1

  decisions = results.get("decision_points_count", 0)
  # Each session's prompt instructs the agent to make 5 decisions, so
  # the loose target is 5 per session. AI.GENERATE often surfaces the
  # same decision under more than one decision_type label (variance),
  # so the actual count tends to land at-or-above this target.
  expected_decisions = 5 * len(session_ids)
  min_acceptable = max(3, len(session_ids))
  if decisions == 0:
    print()
    print(
        "WARNING: AI.GENERATE returned zero decision points. The graph "
        "is built but the decision tables are empty. Re-run "
        "build_graph.py — this is a non-determinism artifact.",
        file=sys.stderr,
    )
  elif decisions < min_acceptable:
    print()
    print(
        f"WARNING: AI.GENERATE returned {decisions} decision points "
        f"across {len(session_ids)} sessions (target ~"
        f"{expected_decisions}). Graph will look thin; consider "
        f"re-running build_graph.py.",
        file=sys.stderr,
    )
  elif decisions < expected_decisions:
    print()
    print(
        f"NOTE: AI.GENERATE extracted {decisions} decision points "
        f"across {len(session_ids)} sessions (target ~"
        f"{expected_decisions}, 5 per agent run). This is normal "
        f"model variance; the demo narration is written to be "
        f"count-agnostic.",
        file=sys.stderr,
    )

  print()
  print(f"Graph    : {mgr.config.graph_name}")
  print(f"Browse it in BigQuery Studio under {PROJECT_ID}.{DATASET_ID}.")
  return 0


if __name__ == "__main__":
  sys.exit(main())
