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

Sends one minimal audience-risk-review request to ``RECEIVER_A2A_URL``
via the A2A client, waits for the response, then queries
``<RECEIVER_DATASET_ID>.<RECEIVER_TABLE_ID>`` and asserts at least
one row exists.

If this fails before caller campaigns run, the most likely cause is
that ``run_receiver_server.py`` is using ``to_a2a()``'s default
plugin-free runner instead of the explicit-runner path; the
receiver agent processes the request but the plugin is silent.
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid

from dotenv import load_dotenv
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


_SMOKE_PROMPT = (
    "Smoke test for audience-risk review. Evaluate three candidate "
    "audiences for an athletic-footwear campaign: "
    "(1) Active runners 18-35 in major metros; "
    "(2) Recovery-from-injury fitness segment; "
    "(3) Adults browsing fertility-clinic search content. "
    "Return the structured SELECTED/DROPPED breakdown."
)


async def _send_request() -> int:
  """Posts one A2A message/send request and returns the HTTP status."""
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
    return resp.status_code


def _count_receiver_rows() -> int:
  client = bigquery.Client(project=PROJECT_ID, location=DATASET_LOCATION)
  query = (
      f"SELECT COUNT(*) AS receiver_rows FROM "
      f"`{PROJECT_ID}.{RECEIVER_DATASET_ID}.{RECEIVER_TABLE_ID}`"
  )
  rows = list(client.query(query).result())
  return int(rows[0]["receiver_rows"]) if rows else 0


def main() -> int:
  print(f"Smoking receiver at {RECEIVER_A2A_URL} ...")
  status = asyncio.run(_send_request())
  if status >= 400:
    print(
        f"ERROR: receiver returned HTTP {status}. The server is not "
        "responding successfully — check `run_receiver_server.py` "
        "logs.",
        file=sys.stderr,
    )
    return 1

  receiver_rows = _count_receiver_rows()
  print(
      f"  Receiver agent_events rows: {receiver_rows} "
      f"(table=`{PROJECT_ID}.{RECEIVER_DATASET_ID}.{RECEIVER_TABLE_ID}`)"
  )
  if receiver_rows == 0:
    print(
        "ERROR: receiver agent_events table is empty after the smoke "
        "request. The receiver server is most likely running with "
        "`to_a2a()`'s default plugin-free runner. Verify "
        "`run_receiver_server.py` constructs `Runner(..., "
        "plugins=[receiver_plugin])` and passes it via `runner=`.",
        file=sys.stderr,
    )
    return 1
  print("OK — receiver row gate passes.")
  return 0


if __name__ == "__main__":
  sys.exit(main())
