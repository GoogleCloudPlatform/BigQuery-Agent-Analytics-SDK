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
import re
import sys

from dotenv import load_dotenv
import google.auth
from google.cloud import bigquery

from bigquery_agent_analytics import Candidate
from bigquery_agent_analytics import ContextGraphConfig
from bigquery_agent_analytics import ContextGraphManager
from bigquery_agent_analytics import DecisionPoint
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

_OPTION_RE = re.compile(
    r"^\s*[-*]\s*(?P<name>.*?)\s+[-—–]\s+"
    r"(?P<status>SELECTED|DROPPED)\s+[-—–]\s+"
    r"score\s+(?P<score>0(?:\.\d+)?|1(?:\.0+)?)\s+[-—–]\s+"
    r"rationale:\s*(?P<rationale>.+?)\s*$",
    re.IGNORECASE,
)


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


def _receiver_extraction_counts(bq_client: bigquery.Client) -> tuple[int, int]:
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
  return int(row["receiver_decisions"]), int(row["receiver_candidates"])


def _clean_receiver_response(raw: str) -> str:
  """Normalizes the receiver's prompt-shaped response text."""
  text = raw.strip()
  if text.startswith("text: "):
    text = text[len("text: ") :].strip()
  if len(text) >= 2 and text[0] == "'" and text[-1] == "'":
    text = text[1:-1]
  return text


def _parse_receiver_decision_response(
    session_id: str,
    span_id: str,
    response_text: str,
) -> tuple[DecisionPoint, list[Candidate]] | None:
  """Parse the receiver demo's strict three-option response contract.

  This is a demo-specific safety net for live presentations. The SDK's
  general path still uses AI.GENERATE; this parser only understands the
  receiver_agent/prompts.py shape:

    Decision type: ...
    Options considered:
    - name — SELECTED|DROPPED — score N.NN — rationale: ...

  Returning ``None`` means the response did not match the receiver
  contract and should remain an extraction failure.
  """
  text = _clean_receiver_response(response_text)
  decision_match = re.search(r"^Decision type:\s*(.+)$", text, re.MULTILINE)
  if not decision_match:
    return None

  candidates: list[Candidate] = []
  decision_id = f"{session_id}:demo_receiver_dp:{span_id}:0"
  for idx, line in enumerate(text.splitlines()):
    option_match = _OPTION_RE.match(line)
    if not option_match:
      continue
    status = option_match.group("status").upper()
    rationale = option_match.group("rationale").strip()
    candidates.append(
        Candidate(
            candidate_id=f"{decision_id}:c:{idx}",
            decision_id=decision_id,
            session_id=session_id,
            name=option_match.group("name").strip(),
            score=float(option_match.group("score")),
            status=status,
            rejection_rationale=None if status == "SELECTED" else rationale,
        )
    )

  if len(candidates) < 3:
    return None

  dp = DecisionPoint(
      decision_id=decision_id,
      session_id=session_id,
      span_id=span_id,
      decision_type=decision_match.group(1).strip(),
      description="Receiver audience-risk review extracted from prompt-shaped response.",
  )
  return dp, candidates


def _repair_receiver_extraction_from_prompt_contract(
    bq_client: bigquery.Client,
) -> int:
  """Fallback parse for the receiver demo's strict LLM_RESPONSE shape."""
  q = f"""
    SELECT
      session_id,
      span_id,
      JSON_EXTRACT_SCALAR(content, '$.response') AS response_text
    FROM `{PROJECT_ID}.{RECEIVER_DATASET_ID}.{RECEIVER_TABLE_ID}`
    WHERE event_type = 'LLM_RESPONSE'
      AND agent = 'audience_risk_reviewer'
      AND content IS NOT NULL
    ORDER BY timestamp ASC
  """
  rows = list(bq_client.query(q).result())
  decision_points: list[DecisionPoint] = []
  candidates: list[Candidate] = []
  for row in rows:
    response_text = row["response_text"]
    if not response_text:
      continue
    parsed = _parse_receiver_decision_response(
        session_id=str(row["session_id"]),
        span_id=str(row["span_id"]),
        response_text=str(response_text),
    )
    if parsed is None:
      continue
    dp, cands = parsed
    decision_points.append(dp)
    candidates.extend(cands)

  if not decision_points or not candidates:
    print(
        "ERROR: receiver fallback parser found zero prompt-shaped "
        "decisions. Tighten receiver_agent/prompts.py.",
        file=sys.stderr,
    )
    return 1

  manager = ContextGraphManager(
      project_id=PROJECT_ID,
      dataset_id=RECEIVER_DATASET_ID,
      table_id=RECEIVER_TABLE_ID,
      client=bq_client,
      location=DATASET_LOCATION,
      config=ContextGraphConfig(endpoint=DEMO_AI_ENDPOINT),
  )
  session_ids = sorted({dp.session_id for dp in decision_points})
  print(
      "Receiver AI.GENERATE extraction was below threshold; applying "
      "demo fallback parser for receiver_agent/prompts.py contract..."
  )
  if not manager.store_decision_points(decision_points, candidates):
    print("ERROR: fallback decision storage failed.", file=sys.stderr)
    return 1
  if not manager.create_decision_edges(session_ids):
    print("ERROR: fallback decision edge creation failed.", file=sys.stderr)
    return 1
  print(
      f"Fallback stored {len(decision_points)} receiver decision(s) and "
      f"{len(candidates)} candidate(s)."
  )
  return 0


def _check_receiver_extraction(bq_client: bigquery.Client) -> int:
  """Receiver-side decision/candidate extraction acceptance gate."""
  decisions, candidates = _receiver_extraction_counts(bq_client)
  print(
      f"Receiver extraction: decision_points={decisions} "
      f"(min {MIN_RECEIVER_DECISIONS}), candidates={candidates} "
      f"(min {MIN_RECEIVER_CANDIDATES})"
  )
  if (
      decisions >= MIN_RECEIVER_DECISIONS
      and candidates >= MIN_RECEIVER_CANDIDATES
  ):
    print("Receiver extraction gate OK.")
    return 0

  repair_rc = _repair_receiver_extraction_from_prompt_contract(bq_client)
  if repair_rc != 0:
    return repair_rc

  decisions, candidates = _receiver_extraction_counts(bq_client)
  print(
      f"Receiver extraction after fallback: decision_points={decisions} "
      f"(min {MIN_RECEIVER_DECISIONS}), candidates={candidates} "
      f"(min {MIN_RECEIVER_CANDIDATES})"
  )
  if decisions < MIN_RECEIVER_DECISIONS or candidates < MIN_RECEIVER_CANDIDATES:
    print(
        "ERROR: receiver extraction below threshold. The receiver "
        "system prompt likely isn't enforcing the three-option format "
        "and the fallback parser could not recover enough rows. "
        "Tighten receiver_agent/prompts.py before debugging the graph "
        "DDL.",
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
