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

"""Run the policy agent over a set of questions and emit conversations JSON.

Each question may be single-turn (``{"id", "question"}``) or multi-turn
(``{"id", "turns": [...]}``). Multi-turn cases drive the anti-parroting
scenario: the user asks a question, then pushes a *wrong* "correction"; a good
agent re-verifies with its tool and holds the right figure instead of parroting
the user's number.

Output is ``{"conversations": [...]}`` in the schema consumed by the SDK's
``quality_report.py --conversations-file`` (session_id, question,
final_response, conversation[], tool_calls), so scoring is identical whether
the traces come from here or from BigQuery.

Usage:
  python run_agent.py --skill skills/SKILL.md \
      --questions eval/questions_test.json --questions eval/questions_corrections.json \
      --model gemini-3.1-flash-lite -o run/v0_test.json
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import logging
import os
import sys
import time

import _quiet  # noqa: F401  -- mute noisy warnings/loggers before google imports

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
  sys.path.insert(0, _SCRIPT_DIR)

from agent.agent import build_config  # noqa: E402
from agent.agent import make_client  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("run_agent")


def _load_questions(paths):
  """Load and concatenate question files into a flat list of dicts."""
  items = []
  for path in paths:
    with open(path) as f:
      data = json.load(f)
    qs = data.get("questions", data) if isinstance(data, dict) else data
    for q in qs:
      items.append(q)
  return items


def _turns_of(question: dict):
  """Normalize a question dict to a list of user turn strings."""
  if question.get("turns"):
    return list(question["turns"])
  return [question.get("question", "")]


def _response_text(resp) -> str:
  """Concatenate text parts of a response (resp.text can be None)."""
  try:
    if resp.text:
      return resp.text
  except (ValueError, AttributeError):
    pass
  parts = []
  for cand in getattr(resp, "candidates", None) or []:
    content = getattr(cand, "content", None)
    for part in getattr(content, "parts", None) or []:
      if getattr(part, "text", None):
        parts.append(part.text)
  return "\n".join(parts)


def _tool_calls_detail(chat) -> list[dict]:
  """Structured function calls across the full (uncurated) chat history.

  Returns ``[{"name": str, "args": dict}, ...]`` in call order, so downstream
  scoring/analysis can show *which* tool was selected (e.g. lookup vs. the
  calculator), not just how many calls happened. ``tool_calls`` (the int count)
  is derived from ``len()`` of this.
  """
  calls = []
  try:
    history = chat.get_history(curated=False)
  except TypeError:
    history = chat.get_history()
  for content in history or []:
    for part in getattr(content, "parts", None) or []:
      fc = getattr(part, "function_call", None)
      if fc:
        args = getattr(fc, "args", None)
        calls.append(
            {
                "name": getattr(fc, "name", "") or "",
                "args": dict(args) if args else {},
            }
        )
  return calls


def _session_events(chat) -> list[tuple[str, dict]]:
  """Ordered ``(event_type, content)`` pairs in the BQAA plugin's schema.

  Walks the full chat history in chronological order and emits the same
  event types (and ``content`` shapes) the BigQuery Agent Analytics plugin
  logs -- USER_MESSAGE_RECEIVED / LLM_RESPONSE / TOOL_STARTING /
  TOOL_COMPLETED -- so a session written to the events table reads back
  through the SDK exactly like plugin-logged traffic (span order included,
  which is what the parroting sub-trajectory check leans on).
  """
  try:
    history = chat.get_history(curated=False)
  except TypeError:
    history = chat.get_history()
  events = []
  for content in history or []:
    role = getattr(content, "role", "") or ""
    for part in getattr(content, "parts", None) or []:
      text = getattr(part, "text", None)
      if text:
        if role == "user":
          events.append(("USER_MESSAGE_RECEIVED", {"text": text}))
        else:
          events.append(("LLM_RESPONSE", {"response": text}))
      fc = getattr(part, "function_call", None)
      if fc:
        args = getattr(fc, "args", None)
        events.append(
            (
                "TOOL_STARTING",
                {
                    "tool": getattr(fc, "name", "") or "",
                    "args": dict(args) if args else {},
                },
            )
        )
      fr = getattr(part, "function_response", None)
      if fr:
        resp = getattr(fr, "response", None)
        try:
          json.dumps(resp)
        except (TypeError, ValueError):
          resp = str(resp)
        events.append(
            (
                "TOOL_COMPLETED",
                {"tool": getattr(fr, "name", "") or "", "result": resp},
            )
        )
  return events


def _run_one(client, model, skill_text, question, collect_events=False):
  """Run a single (possibly multi-turn) question through the agent."""
  turns = _turns_of(question)
  config = build_config(skill_text)
  chat = client.chats.create(model=model, config=config)
  conversation = []
  final = ""
  t0 = time.monotonic()
  for turn in turns:
    resp = chat.send_message(turn)
    text = _response_text(resp)
    conversation.append({"role": "user", "text": turn})
    conversation.append({"role": "agent", "text": text})
    final = text
  latency = round(time.monotonic() - t0, 2)
  tool_detail = _tool_calls_detail(chat)
  record = {
      "session_id": question.get("id", turns[0][:40]),
      "question": turns[0],
      "final_response": final,
      "conversation": conversation,
      "tool_calls": len(tool_detail),
      "tool_calls_detail": tool_detail,
      "answered_by": "policy_agent",
      "latency_s": latency,
      "category": question.get("category", ""),
  }
  if collect_events:
    record["_events"] = _session_events(chat)
  return record


def _write_bigquery(results, app_name, labels):
  """Insert the sessions into a BQAA-schema ``agent_events`` table.

  Writes the exact row shape the BigQuery Agent Analytics plugin produces
  (see the SDK's ``seed_events._event_schema``): one row per event, JSON
  string ``content``/``attributes``, ``root_agent_name`` and ``custom_tags``
  in ``attributes`` so ``quality_report.py --app-name/--label`` can filter.
  Table config comes from the environment: PROJECT_ID / GOOGLE_CLOUD_PROJECT,
  DATASET_ID (default ``agent_analytics``), TABLE_ID (default
  ``agent_events``), DATASET_LOCATION (default REGION or us-central1).
  Creates the dataset/table on first use.
  """
  from datetime import datetime
  from datetime import timedelta
  from datetime import timezone
  import uuid

  from google.cloud import bigquery

  project = os.getenv("PROJECT_ID") or os.getenv("GOOGLE_CLOUD_PROJECT")
  dataset = os.getenv("DATASET_ID", "agent_analytics")
  table_id = os.getenv("TABLE_ID", "agent_events")
  location = (
      os.getenv("DATASET_LOCATION") or os.getenv("REGION") or "us-central1"
  )
  table_ref = f"{project}.{dataset}.{table_id}"

  # The plugin's agent_events contract (producers/..._tracing/schema.py),
  # including content_parts -- the SDK's trace reader selects every one of
  # these columns by name, so a lab-created table must carry the full shape.
  schema = [
      bigquery.SchemaField("timestamp", "TIMESTAMP", mode="REQUIRED"),
      bigquery.SchemaField("event_type", "STRING"),
      bigquery.SchemaField("agent", "STRING"),
      bigquery.SchemaField("session_id", "STRING"),
      bigquery.SchemaField("invocation_id", "STRING"),
      bigquery.SchemaField("user_id", "STRING"),
      bigquery.SchemaField("trace_id", "STRING"),
      bigquery.SchemaField("span_id", "STRING"),
      bigquery.SchemaField("parent_span_id", "STRING"),
      bigquery.SchemaField("content", "JSON"),
      bigquery.SchemaField(
          "content_parts",
          "RECORD",
          mode="REPEATED",
          fields=[
              bigquery.SchemaField("mime_type", "STRING"),
              bigquery.SchemaField("uri", "STRING"),
              bigquery.SchemaField(
                  "object_ref",
                  "RECORD",
                  fields=[
                      bigquery.SchemaField("uri", "STRING"),
                      bigquery.SchemaField("version", "STRING"),
                      bigquery.SchemaField("authorizer", "STRING"),
                      bigquery.SchemaField("details", "JSON"),
                  ],
              ),
              bigquery.SchemaField("text", "STRING"),
              bigquery.SchemaField("part_index", "INTEGER"),
              bigquery.SchemaField("part_attributes", "STRING"),
              bigquery.SchemaField("storage_mode", "STRING"),
          ],
      ),
      bigquery.SchemaField("attributes", "JSON"),
      bigquery.SchemaField("latency_ms", "JSON"),
      bigquery.SchemaField("status", "STRING"),
      bigquery.SchemaField("error_message", "STRING"),
      bigquery.SchemaField("is_truncated", "BOOLEAN"),
  ]

  bq = bigquery.Client(project=project)
  ds = bigquery.Dataset(f"{project}.{dataset}")
  ds.location = location
  bq.create_dataset(ds, exists_ok=True)
  table = bigquery.Table(table_ref, schema=schema)
  table.time_partitioning = bigquery.TimePartitioning(field="timestamp")
  bq.create_table(table, exists_ok=True)

  attributes = json.dumps({"root_agent_name": app_name, "custom_tags": labels})
  now = datetime.now(timezone.utc)
  rows = []
  for r in results:
    if r.get("error"):
      continue
    ts = now
    for event_type, content in r.get("_events", []):
      # 1ms increments keep span order stable under BigQuery's timestamp sort.
      ts = ts + timedelta(milliseconds=1)
      rows.append(
          {
              "timestamp": ts.isoformat(),
              "event_type": event_type,
              "agent": app_name,
              "session_id": r["session_id"],
              "invocation_id": uuid.uuid4().hex,
              "user_id": "lab-user",
              "trace_id": r["session_id"],
              "span_id": uuid.uuid4().hex[:16],
              "parent_span_id": None,
              "status": "ok",
              "error_message": None,
              "is_truncated": False,
              "content": json.dumps(content),
              "attributes": attributes,
              "latency_ms": "{}",
          }
      )
  errors = bq.insert_rows_json(table_ref, rows)
  if errors:
    raise RuntimeError(f"BigQuery insert failed: {errors[:3]}")
  logger.info(
      "Logged %d event row(s) for %d session(s) -> %s (labels: %s)",
      len(rows),
      sum(1 for r in results if not r.get("error")),
      table_ref,
      labels,
  )


def run(
    skill_path,
    question_paths,
    model,
    out_path,
    concurrency=8,
    log_bigquery=False,
    app_name="skill-evolution-lab",
    labels=None,
):
  """Run all questions concurrently and write a conversations file."""
  with open(skill_path) as f:
    skill_text = f.read()
  questions = _load_questions(question_paths)
  client = make_client(model)
  logger.info(
      "Running %d question(s) on %s (skill=%s)...",
      len(questions),
      model,
      os.path.basename(skill_path),
  )

  results = [None] * len(questions)
  with cf.ThreadPoolExecutor(max_workers=concurrency) as pool:
    future_to_idx = {
        pool.submit(_run_one, client, model, skill_text, q, log_bigquery): i
        for i, q in enumerate(questions)
    }
    for future in cf.as_completed(future_to_idx):
      idx = future_to_idx[future]
      try:
        results[idx] = future.result()
      except Exception as exc:  # pylint: disable=broad-except
        q = questions[idx]
        logger.warning("Question %s failed: %s", q.get("id", idx), exc)
        results[idx] = {
            "session_id": q.get("id", str(idx)),
            "question": _turns_of(q)[0],
            "final_response": f"[ERROR: {exc}]",
            "conversation": [],
            "tool_calls": 0,
            "tool_calls_detail": [],
            "answered_by": "policy_agent",
            "latency_s": None,
            "category": q.get("category", ""),
            "error": True,
        }

  if log_bigquery:
    _write_bigquery(results, app_name, labels or {})

  # The events list is a write-path detail; keep the conversations file in
  # the exact schema quality_report.py --conversations-file consumes.
  for r in results:
    r.pop("_events", None)
  with open(out_path, "w") as f:
    json.dump({"conversations": results}, f, indent=2)
  ok = sum(1 for r in results if not r.get("error"))
  logger.info(
      "Wrote %d conversation(s) (%d ok) -> %s", len(results), ok, out_path
  )
  return out_path


def _main():
  parser = argparse.ArgumentParser(description=__doc__)
  parser.add_argument("--skill", required=True, help="Path to SKILL.md")
  parser.add_argument(
      "--questions",
      action="append",
      required=True,
      help="Question JSON file (repeatable; files are concatenated)",
  )
  parser.add_argument(
      "--model", default="gemini-3.1-flash-lite", help="Gemini model"
  )
  parser.add_argument("-o", "--out", required=True, help="Output JSON path")
  parser.add_argument("--concurrency", type=int, default=8)
  parser.add_argument(
      "--log-bigquery",
      action="store_true",
      help=(
          "Also write each session to a BQAA-schema agent_events table"
          " (PROJECT_ID/DATASET_ID/TABLE_ID env), so scoring can read it back"
          " through the SDK's BigQuery path -- the production wiring"
      ),
  )
  parser.add_argument(
      "--app-name",
      default="skill-evolution-lab",
      help="root_agent_name attribute for --log-bigquery rows",
  )
  parser.add_argument(
      "--bq-label",
      action="append",
      default=None,
      metavar="KEY=VALUE",
      help=(
          "custom_tags entry for --log-bigquery rows (repeatable); use a"
          " unique run label so quality_report --label can select this run"
      ),
  )
  args = parser.parse_args()
  labels = {}
  for item in args.bq_label or []:
    key, sep, value = item.partition("=")
    if not sep:
      parser.error(f"--bq-label expects KEY=VALUE, got: {item}")
    labels[key] = value
  run(
      args.skill,
      args.questions,
      args.model,
      args.out,
      args.concurrency,
      log_bigquery=args.log_bigquery,
      app_name=args.app_name,
      labels=labels,
  )


if __name__ == "__main__":
  _main()
