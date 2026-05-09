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

"""Smoke-test the receiver A2A server end-to-end.

Captures the receiver row count before the request, sends one
minimal audience-risk-review request to ``RECEIVER_A2A_URL`` via the
A2A client, waits for the response, then polls
``<RECEIVER_DATASET_ID>.<RECEIVER_TABLE_ID>`` until the count
strictly exceeds the before-count.

The strict-greater check matters: a previous demo run may have left
rows in the receiver table, and a plain "count > 0" check would
silently pass even if the *current* receiver server is running
without the plugin. The smoke gate's purpose is to confirm the
*current* request landed, not that the table was ever written to.

The poll itself matters because
``BigQueryAgentAnalyticsPlugin._log_event`` queues spans into an
async writer; ``batch_size=1`` triggers a flush per event but the
BigQuery write is still asynchronous w.r.t. the HTTP response. We
retry with backoff so the gate has a real chance to observe the
new row.

If the gate fails after the polling window, the most likely cause
is that ``run_receiver_server.py`` is using ``to_a2a()``'s default
plugin-free runner instead of the explicit-runner path; the
receiver agent processes the request but the plugin is silent.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import uuid

from dotenv import load_dotenv
from google.api_core import exceptions as gax_exceptions
import google.auth
from google.cloud import bigquery
import httpx

_HERE = os.path.dirname(os.path.abspath(__file__))
_ENV_PATH = os.path.join(_HERE, ".env")
if os.path.exists(_ENV_PATH):
  load_dotenv(dotenv_path=_ENV_PATH)

_, _auth_project = google.auth.default()
PROJECT_ID = os.getenv("PROJECT_ID") or _auth_project
DATASET_LOCATION = os.getenv("DATASET_LOCATION", "us-central1")
RECEIVER_DATASET_ID = os.getenv("RECEIVER_DATASET_ID", "a2a_receiver_demo")
RECEIVER_TABLE_ID = os.getenv("RECEIVER_TABLE_ID", "agent_events")
RECEIVER_A2A_URL = os.getenv("RECEIVER_A2A_URL", "http://127.0.0.1:8000")
SMOKE_POLL_TIMEOUT_S = int(os.getenv("DEMO_SMOKE_POLL_TIMEOUT_S", "60"))
SMOKE_POLL_INTERVAL_S = float(os.getenv("DEMO_SMOKE_POLL_INTERVAL_S", "2.0"))


_SMOKE_PROMPT = (
    "Smoke test for audience-risk review. Evaluate three candidate "
    "audiences for an athletic-footwear campaign: "
    "(1) Active runners 18-35 in major metros; "
    "(2) Recovery-from-injury fitness segment; "
    "(3) Adults browsing fertility-clinic search content. "
    "Return the structured SELECTED/DROPPED breakdown."
)


async def _send_request() -> tuple[int, dict | None]:
  """Posts one A2A message/send request.

  Returns ``(http_status, parsed_body)``. ``parsed_body`` is ``None``
  if the response wasn't JSON.
  """
  url = RECEIVER_A2A_URL.rstrip("/")
  payload = {
      "jsonrpc": "2.0",
      "id": str(uuid.uuid4()),
      "method": "message/send",
      "params": {
          "message": {
              "role": "user",
              "messageId": str(uuid.uuid4()),
              "parts": [{"kind": "text", "text": _SMOKE_PROMPT}],
          },
      },
  }
  async with httpx.AsyncClient(timeout=120.0) as client:
    resp = await client.post(url, json=payload)
    print(f"  Receiver responded: HTTP {resp.status_code}")
    if resp.status_code >= 400:
      print(f"  Body: {resp.text[:600]}", file=sys.stderr)
      return resp.status_code, None
    try:
      body = resp.json()
    except json.JSONDecodeError:
      print(
          f"  WARNING: response was HTTP 200 but not JSON: "
          f"{resp.text[:300]}",
          file=sys.stderr,
      )
      return resp.status_code, None
    return resp.status_code, body


def _count_receiver_rows(bq_client: bigquery.Client) -> int:
  """Return current receiver row count.

  Treats a NotFound (table doesn't exist yet) as zero rows so the
  poll loop can still time out cleanly with the intended diagnostic
  instead of stack-tracing on a clean dataset.
  """
  query = (
      f"SELECT COUNT(*) AS receiver_rows FROM "
      f"`{PROJECT_ID}.{RECEIVER_DATASET_ID}.{RECEIVER_TABLE_ID}`"
  )
  try:
    rows = list(bq_client.query(query).result())
  except gax_exceptions.NotFound:
    return 0
  return int(rows[0]["receiver_rows"]) if rows else 0


def _poll_for_new_receiver_rows(
    bq_client: bigquery.Client,
    before_count: int,
    timeout_s: float,
    interval_s: float,
) -> int:
  """Poll until receiver row count strictly exceeds ``before_count``.

  Returns the final observed count. The strict-greater check protects
  against a stale-rows pass: a previous demo run may have left rows
  in the receiver table, and a plain ``count > 0`` check would
  silently pass even if the *current* receiver server is running
  without the BQ AA Plugin attached. The smoke gate's purpose is to
  confirm the *current* request landed, not that the table was ever
  written to.
  """
  deadline = time.monotonic() + timeout_s
  count = before_count
  attempt = 0
  while time.monotonic() < deadline:
    attempt += 1
    count = _count_receiver_rows(bq_client)
    if count > before_count:
      print(
          f"  Receiver agent_events rows: {count} "
          f"(was {before_count} before the smoke request; observed "
          f"after {attempt} poll(s))"
      )
      return count
    time.sleep(interval_s)
  print(
      f"  Receiver agent_events rows: {count} (was {before_count} "
      f"before; no new rows after {timeout_s:.0f}s poll, "
      f"{attempt} attempt(s))"
  )
  return count


def main() -> int:
  print(f"Smoking receiver at {RECEIVER_A2A_URL} ...")
  bq_client = bigquery.Client(project=PROJECT_ID, location=DATASET_LOCATION)

  # Capture the row count before the smoke request so we can require
  # a strict increase. Without this, a re-run on a dataset that
  # already has rows from a prior demo would pass even if the
  # current receiver server is running without the plugin.
  before_count = _count_receiver_rows(bq_client)
  print(f"  Receiver agent_events rows before request: {before_count}")

  status, body = asyncio.run(_send_request())
  if status >= 400:
    print(
        f"ERROR: receiver returned HTTP {status}. The server is not "
        "responding successfully — check `run_receiver_server.py` "
        "logs.",
        file=sys.stderr,
    )
    return 1
  if body is not None and isinstance(body, dict) and body.get("error"):
    err = body["error"]
    print(
        "ERROR: receiver returned a JSON-RPC error in a 200 "
        f"response: code={err.get('code')!r}, "
        f"message={err.get('message')!r}",
        file=sys.stderr,
    )
    return 1

  receiver_rows = _poll_for_new_receiver_rows(
      bq_client,
      before_count=before_count,
      timeout_s=SMOKE_POLL_TIMEOUT_S,
      interval_s=SMOKE_POLL_INTERVAL_S,
  )
  print(f"  Table: `{PROJECT_ID}.{RECEIVER_DATASET_ID}.{RECEIVER_TABLE_ID}`")
  if receiver_rows <= before_count:
    print(
        "ERROR: receiver agent_events row count did not increase "
        f"after the smoke request ({before_count} before -> "
        f"{receiver_rows} after, polled {SMOKE_POLL_TIMEOUT_S}s). "
        "The receiver server is most likely running with `to_a2a()`'s "
        "default plugin-free runner, or the plugin is failing to "
        "write. Verify `run_receiver_server.py` constructs "
        "`Runner(..., plugins=[receiver_plugin])` and passes it via "
        "`runner=`.",
        file=sys.stderr,
    )
    return 1
  print("OK — receiver row gate passes.")
  return 0


if __name__ == "__main__":
  sys.exit(main())
