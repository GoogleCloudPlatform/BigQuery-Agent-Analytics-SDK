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

"""Materialize each org's context graph independently.

Runs ``ContextGraphManager.build_context_graph(... include_decisions=
True)`` twice — once over the caller dataset, once over the receiver
dataset. Each org gets its own SDK-owned backing tables
(``extracted_biz_nodes``, ``decision_points``, ``candidates``,
``context_cross_links``, ``made_decision_edges``, ``candidate_edges``)
and its own canonical SDK property graph.

The auditor projection layer (PR 2) is built on top of these
SDK-owned tables; it is intentionally not part of this script.

Acceptance gate: receiver-side decision extraction must produce
``decision_points >= 3`` and ``candidates >= 9`` for the default
demo run. If this fails, tighten ``receiver_agent/prompts.py``
before debugging the graph DDL.
"""

from __future__ import annotations

import os
import sys

from dotenv import load_dotenv
import google.auth
from google.cloud import bigquery

from bigquery_agent_analytics import ContextGraphConfig
from bigquery_agent_analytics import ContextGraphManager
from bigquery_agent_analytics import make_bq_client

_HERE = os.path.dirname(os.path.abspath(__file__))
_ENV_PATH = os.path.join(_HERE, ".env")
if os.path.exists(_ENV_PATH):
  load_dotenv(dotenv_path=_ENV_PATH)

_, _auth_project = google.auth.default()
PROJECT_ID = os.getenv("PROJECT_ID") or _auth_project
DATASET_LOCATION = os.getenv("DATASET_LOCATION", "us-central1")
CALLER_DATASET_ID = os.getenv("CALLER_DATASET_ID", "a2a_caller_demo")
CALLER_TABLE_ID = os.getenv("CALLER_TABLE_ID", "agent_events")
RECEIVER_DATASET_ID = os.getenv("RECEIVER_DATASET_ID", "a2a_receiver_demo")
RECEIVER_TABLE_ID = os.getenv("RECEIVER_TABLE_ID", "agent_events")
# BigQuery AI.GENERATE endpoint default — verified live in May 2026:
# the Gemini 3 preview is reachable ONLY via the full HTTPS endpoint
# URL at locations/global. The simple-name resolver in BQML does not
# recognize "gemini-3-flash" or "gemini-3-flash-preview" today; both
# return 404. The model ID is gemini-3-flash-preview (NOT
# gemini-3-flash). The endpoint URL is per-project, so the default is
# computed from PROJECT_ID at module load. setup.sh writes the same
# resolved URL into .env so the explicit env var also works in the
# normal demo flow. To fall back to a stable model that does work as
# a simple name, override DEMO_AI_ENDPOINT=gemini-2.5-flash.
DEMO_AI_ENDPOINT = os.getenv(
    "DEMO_AI_ENDPOINT",
    f"https://aiplatform.googleapis.com/v1/projects/{PROJECT_ID}"
    f"/locations/global/publishers/google/models/gemini-3-flash-preview",
)

# Receiver-extraction acceptance gate. The receiver prompt forces
# three options per call; for the default 3-campaign demo the
# receiver should produce at least 3 decisions and 9 candidates.
MIN_RECEIVER_DECISIONS = int(os.getenv("DEMO_MIN_RECEIVER_DECISIONS", "3"))
MIN_RECEIVER_CANDIDATES = int(os.getenv("DEMO_MIN_RECEIVER_CANDIDATES", "9"))


def _discover_session_ids(
    bq_client: bigquery.Client, dataset_id: str, table_id: str
) -> list[str]:
  """Return distinct session ids in <dataset>.<table>."""
  q = (
      f"SELECT DISTINCT session_id FROM "
      f"`{PROJECT_ID}.{dataset_id}.{table_id}` "
      f"WHERE session_id IS NOT NULL"
  )
  rows = list(bq_client.query(q).result())
  return sorted(str(r["session_id"]) for r in rows)


def _build_one(
    label: str, dataset_id: str, table_id: str, bq_client: bigquery.Client
) -> int:
  """Materialize the SDK context graph for one dataset."""
  session_ids = _discover_session_ids(bq_client, dataset_id, table_id)
  if not session_ids:
    print(
        f"ERROR: {label} dataset {dataset_id}.{table_id} has zero "
        "sessions; cannot build a graph.",
        file=sys.stderr,
    )
    return 1
  print(
      f"[{label}] discovered {len(session_ids)} session(s) in "
      f"{dataset_id}.{table_id}"
  )

  # endpoint is configured on ContextGraphConfig; build_context_graph
  # itself does not accept an ai_endpoint kwarg.
  manager = ContextGraphManager(
      project_id=PROJECT_ID,
      dataset_id=dataset_id,
      table_id=table_id,
      client=bq_client,
      location=DATASET_LOCATION,
      config=ContextGraphConfig(endpoint=DEMO_AI_ENDPOINT),
  )
  print(
      f"[{label}] running ContextGraphManager.build_context_graph "
      "(use_ai_generate=True, include_decisions=True)..."
  )
  results = manager.build_context_graph(
      session_ids=session_ids,
      use_ai_generate=True,
      include_decisions=True,
  )

  # build_context_graph swallows per-step exceptions and reports
  # success as bool flags in the results dict; treat a False on any
  # of the gating steps as a hard failure so we don't print "graph
  # built" and then have an empty / broken graph downstream.
  required_flags = [
      "cross_links_created",
      "decision_points_stored",
      "decision_edges_created",
      "property_graph_created",
  ]
  failed = [
      flag for flag in required_flags if flag in results and not results[flag]
  ]
  if failed:
    print(
        f"ERROR: [{label}] graph build reported failures on: "
        f"{failed}. Full results: {results}",
        file=sys.stderr,
    )
    return 1
  print(
      f"[{label}] graph built. biz_nodes={results.get('biz_nodes_count')} "
      f"decision_points={results.get('decision_points_count')}"
  )
  return 0


def _check_receiver_extraction(bq_client: bigquery.Client) -> int:
  """Receiver-side decision/candidate extraction acceptance gate."""
  q = f"""
    SELECT
      (SELECT COUNT(*) FROM
        `{PROJECT_ID}.{RECEIVER_DATASET_ID}.decision_points`)
        AS receiver_decisions,
      (SELECT COUNT(*) FROM
        `{PROJECT_ID}.{RECEIVER_DATASET_ID}.candidates`)
        AS receiver_candidates
  """
  row = list(bq_client.query(q).result())[0]
  decisions = int(row["receiver_decisions"])
  candidates = int(row["receiver_candidates"])
  print(
      f"Receiver extraction: decision_points={decisions} "
      f"(min {MIN_RECEIVER_DECISIONS}), candidates={candidates} "
      f"(min {MIN_RECEIVER_CANDIDATES})"
  )
  if decisions < MIN_RECEIVER_DECISIONS or candidates < MIN_RECEIVER_CANDIDATES:
    print(
        "ERROR: receiver extraction below threshold. The receiver "
        "system prompt likely isn't enforcing the three-option "
        "format. Tighten receiver_agent/prompts.py before debugging "
        "the graph DDL.",
        file=sys.stderr,
    )
    return 1
  print("Receiver extraction gate OK.")
  return 0


def main() -> int:
  bq_client = make_bq_client(PROJECT_ID, location=DATASET_LOCATION)

  rc = _build_one("caller", CALLER_DATASET_ID, CALLER_TABLE_ID, bq_client)
  if rc != 0:
    return rc

  rc = _build_one("receiver", RECEIVER_DATASET_ID, RECEIVER_TABLE_ID, bq_client)
  if rc != 0:
    return rc

  return _check_receiver_extraction(bq_client)


if __name__ == "__main__":
  sys.exit(main())
