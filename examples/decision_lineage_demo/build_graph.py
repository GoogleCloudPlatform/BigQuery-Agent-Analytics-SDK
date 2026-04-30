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

"""Build the decision-lineage property graph from seeded traces.

Calls the SDK's end-to-end pipeline:

  ``mgr.build_context_graph(
      session_ids,
      use_ai_generate=True,
      include_decisions=True,
  )``

Which runs:
  1. ``extract_biz_nodes`` — AI.GENERATE pulls business entities.
  2. ``create_cross_links`` — Evaluated edges TechNode -> BizNode.
  3. ``extract_decision_points`` — AI.GENERATE pulls DecisionPoints
     and Candidates with rejection rationale.
  4. ``store_decision_points`` — writes the extracted rows.
  5. ``create_decision_edges`` — MadeDecision + CandidateEdge rows.
  6. ``create_property_graph(include_decisions=True)`` — the
     ``CREATE OR REPLACE PROPERTY GRAPH`` DDL with all four labels.

Reports per-step row counts so you can confirm AI.GENERATE actually
extracted decisions before opening BigQuery Studio.

Note: AI.GENERATE results are non-deterministic. Re-running may yield
slightly different rejection-rationale wording or candidate counts.
The graph structure (TechNode, BizNode, DecisionPoint, CandidateNode
+ four edge labels) is stable.
"""

from __future__ import annotations

import os
import sys

from dotenv import load_dotenv

from bigquery_agent_analytics.context_graph import ContextGraphConfig
from bigquery_agent_analytics.context_graph import ContextGraphManager

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_env_path = os.path.join(_SCRIPT_DIR, ".env")
if os.path.exists(_env_path):
  load_dotenv(dotenv_path=_env_path)

PROJECT_ID = os.getenv("PROJECT_ID")
DATASET_ID = os.getenv("DATASET_ID", "decision_lineage_demo")
TABLE_ID = os.getenv("TABLE_ID", "agent_events")
SESSION_ID = os.getenv("DEMO_SESSION_ID", "sess-nike-summer-2026")
ENDPOINT = os.getenv("DEMO_AI_ENDPOINT", "gemini-2.5-flash")

if not PROJECT_ID:
  print(
      "ERROR: PROJECT_ID not set. Run ./setup.sh first.",
      file=sys.stderr,
  )
  sys.exit(2)


def main() -> int:
  print(f"Project : {PROJECT_ID}")
  print(f"Dataset : {DATASET_ID}")
  print(f"Session : {SESSION_ID}")
  print(f"Endpoint: {ENDPOINT}")
  print()
  print("Running ContextGraphManager.build_context_graph "
        "(AI.GENERATE on, decisions on)...")
  print("This calls AI.GENERATE twice — once for biz entities, once "
        "for decision points. Expect ~30-60s.")
  print()

  config = ContextGraphConfig(endpoint=ENDPOINT)
  mgr = ContextGraphManager(
      project_id=PROJECT_ID,
      dataset_id=DATASET_ID,
      table_id=TABLE_ID,
      config=config,
  )

  results = mgr.build_context_graph(
      session_ids=[SESSION_ID],
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

  if results.get("decision_points_count", 0) == 0:
    print()
    print(
        "WARNING: AI.GENERATE returned zero decision points. The graph "
        "is built but the decision tables are empty. Re-run "
        "build_graph.py — this is a non-determinism artifact.",
        file=sys.stderr,
    )

  print()
  print(f"Graph    : {mgr.config.graph_name}")
  print("Browse it in BigQuery Studio under "
        f"{PROJECT_ID}.{DATASET_ID}.")
  return 0


if __name__ == "__main__":
  sys.exit(main())
