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

"""Live ADK observe agent: the producer side of the OKF adapter demo.

``okf_rfc_observe_agent`` (``gemini-3.8-flash`` on Vertex, location
``global``) runs a multi-turn finance session (10–12 related questions, more
if needed) under one ``session_id`` with two observer-only tools whose
return values carry the OKF envelope (``kind``, ``context_ref``, ``okf``).
The committed export must contain >= 100 real ``agent_events`` rows.
``BigQueryAgentAnalyticsPlugin`` streams every event into
``<project>.<dataset>.agent_events``; after shutdown this module reads the
session back and writes the committed fixtures that ``adapter.py`` consumes.

Observer-only: the tools never read the authored ``cymbal-finance-core``
bundle, never execute SQL, and never return principal, paths, query text,
parameter values, ``concept_version_id`` or a destination table. Nothing is
attested; the receipt verdict is ``UNVERIFIABLE`` (no-execution).

Run once (needs ADC for the project)::

  export GOOGLE_CLOUD_PROJECT=test-project-0728-467323
  export GOOGLE_CLOUD_LOCATION=global
  export GOOGLE_GENAI_USE_VERTEXAI=True
  export DEMO_MODEL_ID=gemini-3.8-flash
  python examples/okf_bqaa_adapter/observe_agent.py
"""

from __future__ import annotations

import asyncio
import datetime
import hashlib
import json
import os
from pathlib import Path
import sys
import time

from google.adk.agents import Agent
from google.adk.models import Gemini
from google.adk.plugins.bigquery_agent_analytics_plugin import BigQueryAgentAnalyticsPlugin
from google.adk.plugins.bigquery_agent_analytics_plugin import BigQueryLoggerConfig
from google.adk.runners import InMemoryRunner
from google.cloud import bigquery
from google.genai import types

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import adapter  # noqa: E402  (same-directory module)

PROJECT = os.environ.get("GOOGLE_CLOUD_PROJECT", "test-project-0728-467323")
DATASET = os.environ.get("OKF_DEMO_DATASET", "okf_rfc_demo")
TABLE = os.environ.get("OKF_DEMO_TABLE", "agent_events")
BQ_LOCATION = os.environ.get("OKF_DEMO_BQ_LOCATION", "US")
MODEL = os.environ.get("DEMO_MODEL_ID", "gemini-3.8-flash")
# us-central1 returned 404 for gemini-3.8-flash; "global" is what works.
VERTEX_LOCATION = os.environ.get("GOOGLE_CLOUD_LOCATION", "global")
FORBIDDEN_MODEL_MARKERS = ("2.5", "3.5", "flash-latest")

APP_NAME = "okf_rfc_demo"
AGENT_NAME = "okf_rfc_observe_agent"
USER_ID = "okf-observe-demo"  # demo pseudo-user, not a real principal
MIN_EVENTS = 100
QUESTIONS = [
    (
        "What was active-customer revenue in Germany last quarter — and can I"
        " trust the number?"
    ),
    "Same question for France last quarter — what does the receipt say?",
    "Same for the United Kingdom last quarter.",
    "How did Germany compare to the prior quarter?",
    "What does the trust / receipt verdict actually mean for this number?",
    "Why is the legacy customer-revenue metric excluded from current reporting?",
    "What policy governs active-customer revenue recognition?",
    "Which BigQuery tables back this metric?",
    "What is the observed definition of an active customer?",
    ("Can I get a region roll-up for Germany, France, and the UK together?"),
    "If the number is unproven, what would make the receipt attested?",
    "Which excluded items must I not use for last-quarter reporting?",
]
EXTRA_QUESTIONS = [
    "How does the governed_by link constrain last-quarter Germany revenue?",
    "Walk the ranks of the observed catalog and say what each title is for.",
    "Is customer revenue (legacy) ever acceptable for this quarter?",
    "Restate the receipt runtime and parameter schema without inventing values.",
    "Compare the France receipt to the Germany receipt — same context_ref?",
    "What would a reviewer still not know after seeing this observer envelope?",
]
LABEL = "derived/demo, observer-only, nothing attested"
WRITER = {
    "plugin": "google.adk.plugins.bigquery_agent_analytics_plugin",
    "label": "bigquery-agent-analytics-plugin/live",
    "mode": "storage-write-api",
}

FIXTURES = HERE / "fixtures"
EVENTS_PATH = FIXTURES / "live_observe_agent_events.json"
LIVE_PATH = FIXTURES / "live.json"
IDENTITIES_PATH = FIXTURES / "live_identities.json"

os.environ["GOOGLE_CLOUD_PROJECT"] = PROJECT
os.environ["GOOGLE_CLOUD_LOCATION"] = VERTEX_LOCATION
os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "True"

# In-process observed catalog (derived/demo). Titles, types, ranks, one
# exclusion and one edge only: exactly what an observer may see. This is NOT
# the authored cymbal-finance-core bundle and is never read from disk.
OBSERVED_CATALOG = {
    "label": "derived/demo observed catalog (in-process); not authored",
    "items": [
        {"rank": 1, "type": "Metric", "title": "Active-customer revenue"},
        {
            "rank": 2,
            "type": "Attested Computation",
            "title": "Active-customer revenue by region and quarter",
        },
        {"rank": 3, "type": "Business Concept", "title": "Active customer"},
        {
            "rank": 4,
            "type": "Policy",
            "title": "Revenue recognition eligibility",
        },
        {"rank": 5, "type": "BigQuery Table", "title": "Billing invoice lines"},
        {"rank": 6, "type": "BigQuery Table", "title": "CRM customers"},
    ],
    "excluded": [
        {
            "type": "Metric",
            "title": "Customer revenue (legacy)",
            "reason": "superseded; out of force since 2026-06-20",
        }
    ],
    "links": [
        {
            "from": "Active-customer revenue",
            "to": "Revenue recognition eligibility",
            "rel": "governed_by",
        }
    ],
}
# Demo pin of the in-process catalog. Labelled as such: it is a hash of the
# observed catalog above, not an authored publication_id.
CATALOG_PIN = (
    "sha256:"
    + hashlib.sha256(
        b"okf-demo:observed-catalog:v0\x00"
        + json.dumps(
            OBSERVED_CATALOG, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()
)
ENVELOPE_REF = "okf:env-observe#" + CATALOG_PIN[7:19]


def check_model(model: str) -> str:
  """Fail closed on model ids this demo must never run on."""
  for marker in FORBIDDEN_MODEL_MARKERS:
    if marker in model:
      raise SystemExit(
          f"refusing model {model!r}: contains {marker!r}. This demo pins"
          " gemini-3.8-flash (DEMO_MODEL_ID) on Vertex location global."
      )
  return model


# ---- tools (observer-only; envelope on the return value) -----------------


def okf_retrieve_context(
    mode: str = "current", token_budget: int = 8000
) -> dict:
  """Retrieve the observed OKF context envelope for the finance question.

  Returns titles, types and ranks only (plus one exclusion and one edge).
  Never returns authored text, paths, SQL, principal or concept_version_id.

  Args:
    mode: retrieval mode; only "current" is observed in this demo.
    token_budget: advisory packing budget carried on the envelope.
  """
  del token_budget  # advisory only; the in-process catalog is tiny
  return {
      "kind": adapter.KIND_RETRIEVE,
      "context_ref": ENVELOPE_REF,
      "item_count": len(OBSERVED_CATALOG["items"]),
      "excluded_count": len(OBSERVED_CATALOG["excluded"]),
      "okf": {
          "publication_id": CATALOG_PIN,
          "publication_id_note": (
              "pin of the in-process demo catalog; not an authored publication"
          ),
          "profile_contract_version": adapter.PROFILE_CONTRACT_VERSION,
          "mode": mode,
          "items": OBSERVED_CATALOG["items"],
          "excluded": OBSERVED_CATALOG["excluded"],
          "links": OBSERVED_CATALOG["links"],
      },
      "label": "derived/demo, observer-only",
  }


def okf_run_attested_computation(context_ref: str) -> dict:
  """Return the receipt for the sanctioned computation bound to context_ref.

  Honest no-op: nothing is executed and nothing is attested. The verdict is
  UNVERIFIABLE (no-execution). Parameter names and types are declared;
  values are never observed. A context_ref that does not bind to the
  envelope issued by okf_retrieve_context is refused, not fabricated.

  Args:
    context_ref: the context_ref returned by okf_retrieve_context.
  """
  bound = isinstance(context_ref, str) and context_ref.startswith(ENVELOPE_REF)
  receipt = {
      "kind": adapter.KIND_RECEIPT,
      "context_ref": context_ref,
      "okf": {
          "verdict": "UNVERIFIABLE" if bound else "REFUSED",
          "verdict_reason": (
              "no-execution; observer-only demo, nothing attested"
              if bound
              else "context_ref does not bind to the retrieve envelope"
          ),
          "runtime": "bigquery-named-parameters",
          "parameter_schema": [
              {"name": "region", "type": "STRING", "required": True},
              {"name": "quarter_start", "type": "DATE", "required": True},
              {"name": "quarter_end", "type": "DATE", "required": True},
          ],
          "receipt_fields": ["verdict", "verdict_reason", "receipt_id"],
      },
      "label": "derived/demo, observer-only, not attested",
  }
  if bound:
    receipt["okf"]["receipt_id"] = "rcpt-observe-noexec"
  return receipt


INSTRUCTION = (
    "You are an observer-only finance analyst agent. For every question you"
    " MUST first call okf_retrieve_context (mode='current'), then call"
    " okf_run_attested_computation with the exact context_ref returned by"
    " okf_retrieve_context, and only then answer. Cite only the context_ref."
    " Report the receipt verdict and verdict_reason verbatim; if the verdict"
    " is not ATTESTED, say plainly that the number is unproven. Never print"
    " SQL, the principal, file paths, or concept_version_id."
)


def build_agent(model: str = MODEL) -> Agent:
  check_model(model)
  return Agent(
      name=AGENT_NAME,
      model=Gemini(
          model=model, retry_options=types.HttpRetryOptions(attempts=3)
      ),
      description=(
          "Observer-only demo: retrieve derived OKF context and an"
          " unattested receipt for Germany active-customer revenue."
      ),
      instruction=INSTRUCTION,
      tools=[okf_retrieve_context, okf_run_attested_computation],
  )


def build_plugin() -> BigQueryAgentAnalyticsPlugin:
  return BigQueryAgentAnalyticsPlugin(
      project_id=PROJECT,
      dataset_id=DATASET,
      table_id=TABLE,
      location=BQ_LOCATION,
      config=BigQueryLoggerConfig(
          enabled=True,
          max_content_length=64 * 1024,
          batch_size=1,
          shutdown_timeout=20.0,
      ),
  )


def ensure_dataset(client: bigquery.Client) -> None:
  ds = bigquery.Dataset(f"{PROJECT}.{DATASET}")
  ds.location = BQ_LOCATION
  client.create_dataset(ds, exists_ok=True)


async def _ask(runner, session_id: str, question: str) -> None:
  content = types.Content(role="user", parts=[types.Part(text=question)])
  async for event in runner.run_async(
      user_id=USER_ID, session_id=session_id, new_message=content
  ):
    if getattr(event, "content", None) and event.content.parts:
      for part in event.content.parts:
        if getattr(part, "function_call", None):
          print("TOOL_CALL", part.function_call.name)
        elif getattr(part, "text", None):
          print("EVENT_TEXT", part.text[:500])


async def _run(model: str) -> str:
  plugin = build_plugin()
  runner = InMemoryRunner(
      agent=build_agent(model), app_name=APP_NAME, plugins=[plugin]
  )
  session = await runner.session_service.create_session(
      app_name=runner.app_name, user_id=USER_ID
  )
  print("SESSION", session.id)
  queue = list(QUESTIONS)
  extra_i = 0
  turn = 0
  while queue:
    question = queue.pop(0)
    turn += 1
    print("TURN", turn, question[:120])
    await _ask(runner, session.id, question)
    if queue:
      continue
    # Peek BQ; keep going in THIS session_id until >= MIN_EVENTS.
    peek = fetch_session_rows(session.id, wait_s=25.0, require_stable=False)
    print("EVENT_COUNT_SO_FAR", len(peek))
    if len(peek) >= MIN_EVENTS:
      break
    if extra_i >= len(EXTRA_QUESTIONS):
      print(
          "WARNING still below MIN_EVENTS after extras;"
          " will export-gate after shutdown"
      )
      break
    queue.append(EXTRA_QUESTIONS[extra_i])
    extra_i += 1
  await runner.close()
  await plugin.shutdown()  # idempotent; drains the write queue (<= 20s)
  return session.id


def run_observe_agent(model: str = MODEL) -> str:
  """Run the live multi-turn observe agent; return the BQAA session_id."""
  check_model(model)
  print("PROJECT", PROJECT)
  print("DATASET", f"{PROJECT}.{DATASET}.{TABLE}")
  print("MODEL", model)
  print("VERTEX_LOCATION", VERTEX_LOCATION)
  ensure_dataset(bigquery.Client(project=PROJECT, location=BQ_LOCATION))
  return asyncio.run(_run(model))


# ---- export ----------------------------------------------------------------

_COLUMNS = (
    "timestamp",
    "event_type",
    "agent",
    "session_id",
    "invocation_id",
    "user_id",
    "trace_id",
    "span_id",
    "parent_span_id",
    "content",
    "attributes",
    "latency_ms",
    "status",
    "error_message",
    "is_truncated",
)
_JSON_COLUMNS = ("content", "attributes", "latency_ms")


def _row_to_dict(row) -> dict:
  out = {}
  for col in _COLUMNS:
    v = row.get(col)
    if col == "timestamp" and v is not None:
      v = v.astimezone(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
    elif col in _JSON_COLUMNS and isinstance(v, str):
      try:
        v = json.loads(v)
      except ValueError:
        pass
    out[col] = v
  return out


def fetch_session_rows(
    session_id: str,
    *,
    project: str = PROJECT,
    dataset: str = DATASET,
    table: str = TABLE,
    wait_s: float = 180.0,
    require_stable: bool = True,
) -> list[dict]:
  """Read one session back from agent_events (polls until it settles).

  Multi-turn sessions emit many INVOCATION_COMPLETED rows; do not treat the
  first one as done. Wait until the row count is stable (or wait_s elapses).
  """
  client = bigquery.Client(project=project, location=BQ_LOCATION)
  sql = (
      f"SELECT {', '.join(_COLUMNS)} FROM `{project}.{dataset}.{table}`"
      " WHERE session_id = @session_id ORDER BY timestamp, span_id"
  )
  cfg = bigquery.QueryJobConfig(
      query_parameters=[
          bigquery.ScalarQueryParameter("session_id", "STRING", session_id)
      ]
  )
  deadline = time.monotonic() + wait_s
  previous = -1
  stable = 0
  rows: list[dict] = []
  while True:
    rows = [_row_to_dict(r) for r in client.query(sql, job_config=cfg).result()]
    if rows and len(rows) == previous:
      stable += 1
      if (not require_stable) or stable >= 2:
        return rows
    else:
      stable = 0
    previous = len(rows)
    if time.monotonic() >= deadline:
      return rows
    time.sleep(5)


def export_session(
    session_id: str,
    *,
    model: str = MODEL,
    events_path: Path = EVENTS_PATH,
    live_path: Path = LIVE_PATH,
    identities_path: Path = IDENTITIES_PATH,
) -> dict:
  """Export a session to the committed fixtures; fail closed on bad shape."""
  rows = fetch_session_rows(session_id, wait_s=180.0, require_stable=True)
  if not rows:
    raise SystemExit(f"no agent_events rows for session {session_id}")
  if len(rows) < MIN_EVENTS:
    raise SystemExit(
        f"event_count {len(rows)} < {MIN_EVENTS} for session {session_id}."
        " 15-row smokes are not the demo; add turns and re-export."
    )
  table = f"{PROJECT}.{DATASET}.{TABLE}"
  trace = adapter.build_trace(
      rows,
      table=table,
      writer=WRITER,
      label=LABEL,
      agent={"name": AGENT_NAME, "framework": "google-adk", "model": model},
      extra={
          "project": PROJECT,
          "dataset": DATASET,
          "vertex_location": VERTEX_LOCATION,
      },
  )
  obs = adapter.require_retrieve_shaped(trace)  # raises if not retrieve-shaped
  receipt = obs["receipt"]
  if not str(obs["receipt_context_ref"]).startswith(obs["context_ref"]):
    raise SystemExit(
        "receipt context_ref does not bind to the retrieve envelope:"
        f" {obs['receipt_context_ref']!r} vs {obs['context_ref']!r}"
    )
  if receipt.get("verdict") != "UNVERIFIABLE" or not receipt.get("receipt_id"):
    raise SystemExit(f"receipt not usable: {receipt}")
  models = {
      adapter._attributes(r).get("model")
      for r in rows
      if r["event_type"] == "LLM_REQUEST"
  }
  if model not in models:
    raise SystemExit(f"LLM_REQUEST rows carry {models}, expected {model!r}")

  ran_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
  trace["exported_at"] = ran_at
  result = adapter.adapt(trace)
  identities = adapter.compute_identities(
      result["files"], result["constants"], adapter.load_manifests()
  )
  events_path.parent.mkdir(parents=True, exist_ok=True)
  _write_json(events_path, trace)
  _write_json(
      live_path,
      {
          "label": LABEL,
          "session_id": obs["session_id"],
          "trace_id": obs["trace_id"],
          "agent": AGENT_NAME,
          "model": model,
          "project": PROJECT,
          "dataset": DATASET,
          "table": table,
          "vertex_location": VERTEX_LOCATION,
          "ran_at": ran_at,
          "event_count": len(rows),
          "context_ref": obs["context_ref"],
          "receipt_context_ref": obs["receipt_context_ref"],
      },
  )
  _write_json(identities_path, adapter.identities_document(result, identities))
  print("EXPORTED", events_path)
  print("EXPORTED", live_path)
  print("EXPORTED", identities_path)
  return trace


def _write_json(path: Path, obj) -> None:
  path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", "utf-8")


def main() -> int:
  session_id = run_observe_agent(MODEL)
  trace = export_session(session_id, model=MODEL)
  print("PROJECT", PROJECT)
  print("DATASET", DATASET)
  print("MODEL", MODEL)
  print("SESSION", session_id)
  print("TRACE", trace["trace_id"])
  print("EVENT_COUNT", len(trace["events"]))
  return 0


if __name__ == "__main__":
  sys.exit(main())
