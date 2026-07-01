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

"""``bqaa-otel verify`` / smoke — deployment health checks (#324 PR3).

``run_verify`` is strictly read-only: endpoint reachability + bearer-token
enforcement, table/view existence, recent-row freshness, and dead-letter
health. ``run_smoke`` additionally exercises the write path with synthetic
OTLP logs + metrics and follows them through the native tables, the dedup
views, and the ``agent_events_otlp`` projection (running the scheduled
MERGE once, exactly as the live e2e test does — the payload builders here
are shared with it).

I/O is injected (``http_post``, ``query_rows``) so every check is
unit-testable; the CLI wires urllib + google-cloud-bigquery in
:func:`default_http_post` / :func:`default_query_rows`.
"""

from __future__ import annotations

import dataclasses
import json
import time
from typing import Any, Callable
import uuid

from . import sql as otel_sql

# (status, body) for a POST; body is bytes, headers a plain dict.
HttpPost = Callable[[str, bytes, dict], tuple[int, str]]
# Rows for a query, as sequences indexable like bigquery Row tuples.
QueryRows = Callable[[str], list]

_NATIVE_TABLES = (
    "otel_logs",
    "otel_metric_sum",
    "otel_metric_gauge",
    "otel_metric_histogram",
    "otel_metric_exponential_histogram",
    "otel_metric_summary",
    "otlp_dead_letter",
)
_VIEWS = (
    "otel_logs_dedup",
    "otel_metric_sum_dedup",
    "otel_metric_gauge_dedup",
    "otel_metric_histogram_dedup",
    "otel_metric_exponential_histogram_dedup",
    "otel_metric_summary_dedup",
    "bqaa_metrics",
)


@dataclasses.dataclass(frozen=True)
class VerifySettings:
  endpoint: str
  token: str
  project: str
  dataset: str
  signals: tuple[str, ...] = ("logs", "metrics")
  recent_hours: int = 24

  @property
  def qualified(self) -> str:
    return f"{self.project}.{self.dataset}"


@dataclasses.dataclass(frozen=True)
class CheckResult:
  name: str
  ok: bool
  detail: str
  warning: bool = False  # advisory: never fails the run


def _expected_tables(settings: VerifySettings) -> tuple[str, ...]:
  expected = _NATIVE_TABLES + ("agent_events_otlp",) + _VIEWS
  if "traces" in settings.signals:
    expected += ("otel_spans", "otel_spans_dedup")
  return expected


def _empty_logs_body() -> bytes:
  return json.dumps({"resourceLogs": []}).encode("utf-8")


def run_verify(
    settings: VerifySettings,
    *,
    http_post: HttpPost,
    query_rows: QueryRows,
) -> list[CheckResult]:
  """Read-only deployment checks; warnings never fail the run."""
  results: list[CheckResult] = []
  logs_url = settings.endpoint.rstrip("/") + "/v1/logs"

  # 1. Reachability + auth enforcement: an unauthenticated request must be
  # rejected, an authenticated empty request must be accepted.
  status, _ = http_post(logs_url, _empty_logs_body(), {})
  results.append(
      CheckResult(
          name="endpoint auth enforced",
          ok=status == 401,
          detail=f"unauthenticated POST {logs_url} -> {status} (want 401)",
      )
  )
  status, body = http_post(
      logs_url,
      _empty_logs_body(),
      {
          "Authorization": f"Bearer {settings.token}",
          "Content-Type": "application/json",
      },
  )
  results.append(
      CheckResult(
          name="endpoint reachable",
          ok=status == 200,
          detail=f"authenticated POST {logs_url} -> {status} (want 200)",
      )
  )

  # 2. Table/view existence.
  existing = {
      row[0]
      for row in query_rows(
          "SELECT table_name FROM"
          f" `{settings.qualified}.INFORMATION_SCHEMA.TABLES`"
      )
  }
  missing = [t for t in _expected_tables(settings) if t not in existing]
  results.append(
      CheckResult(
          name="tables and views exist",
          ok=not missing,
          detail=(
              "all present" if not missing else f"missing: {', '.join(missing)}"
          ),
      )
  )

  # 3. Recent rows (freshness): informational — a fresh deployment has none.
  for table in ("otel_logs", "bqaa_metrics"):
    if table in missing:
      continue
    count = query_rows(
        f"SELECT COUNT(*) FROM `{settings.qualified}.{table}` WHERE"
        " ingest_time > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL"
        f" {settings.recent_hours} HOUR)"
    )[0][0]
    results.append(
        CheckResult(
            name=f"recent rows in {table}",
            ok=count > 0,
            detail=f"{count} rows in the last {settings.recent_hours}h",
            warning=count == 0,
        )
    )

  # 4. Dead-letter health: rows here mean malformed/failed deliveries.
  if "otlp_dead_letter" not in missing:
    dead = query_rows(
        f"SELECT COUNT(*) FROM `{settings.qualified}.otlp_dead_letter`"
        " WHERE ingest_time > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL"
        f" {settings.recent_hours} HOUR)"
    )[0][0]
    results.append(
        CheckResult(
            name="dead-letter health",
            ok=dead == 0,
            detail=(
                f"{dead} dead-lettered records in the last"
                f" {settings.recent_hours}h"
                + ("" if dead == 0 else " — inspect otlp_dead_letter.raw_b64")
            ),
            warning=dead > 0,
        )
    )
  return results


# --------------------------------------------------------------------------
# Synthetic payloads (shared with producers/tests/test_otlp_e2e.py)
# --------------------------------------------------------------------------


def synthetic_logs_payload(run_id: str, now_nanos: int) -> dict:
  """One OTLP/JSON log record tagged with ``bqaa.run_id`` for tracking."""
  return {
      "resourceLogs": [
          {
              "resource": {
                  "attributes": [
                      {
                          "key": "service.name",
                          "value": {"stringValue": "claude-code"},
                      },
                  ]
              },
              "scopeLogs": [
                  {
                      "scope": {"name": "bqaa-smoke"},
                      "logRecords": [
                          {
                              "timeUnixNano": str(now_nanos),
                              "body": {"stringValue": "bqaa smoke"},
                              "eventName": "claude_code.user_prompt",
                              "attributes": [
                                  {
                                      "key": "bqaa.run_id",
                                      "value": {"stringValue": run_id},
                                  },
                                  {
                                      "key": "session.id",
                                      "value": {"stringValue": run_id},
                                  },
                              ],
                          }
                      ],
                  }
              ],
          }
      ]
  }


def synthetic_gauge_payload(run_id: str, now_nanos: int) -> dict:
  """One OTLP/JSON gauge point named after the run id."""
  return {
      "resourceMetrics": [
          {
              "resource": {"attributes": []},
              "scopeMetrics": [
                  {
                      "scope": {"name": "bqaa-smoke"},
                      "metrics": [
                          {
                              "name": f"bqaa_e2e_{run_id}",
                              "unit": "1",
                              "gauge": {
                                  "dataPoints": [
                                      {
                                          "asDouble": 1.0,
                                          "timeUnixNano": str(now_nanos),
                                      }
                                  ]
                              },
                          }
                      ],
                  }
              ],
          }
      ]
  }


def run_smoke(
    settings: VerifySettings,
    *,
    http_post: HttpPost,
    query_rows: QueryRows,
    sleep: Callable[[float], None] = time.sleep,
    timeout_s: float = 150,
) -> list[CheckResult]:
  """Send synthetic logs+metrics and follow them into BigQuery."""
  results: list[CheckResult] = []
  run_id = uuid.uuid4().hex
  now_nanos = int(time.time() * 1e9)
  headers = {
      "Authorization": f"Bearer {settings.token}",
      "Content-Type": "application/json",
  }
  base = settings.endpoint.rstrip("/")

  for path, payload in (
      ("/v1/logs", synthetic_logs_payload(run_id, now_nanos)),
      ("/v1/metrics", synthetic_gauge_payload(run_id, now_nanos)),
  ):
    status, body = http_post(
        base + path, json.dumps(payload).encode("utf-8"), headers
    )
    results.append(
        CheckResult(
            name=f"smoke send {path}",
            ok=status == 200,
            detail=f"POST {path} -> {status}",
        )
    )
  if any(not r.ok for r in results):
    return results

  def _wait_count(query: str) -> int:
    deadline = time.monotonic() + timeout_s
    while True:
      count = query_rows(query)[0][0]
      if count or time.monotonic() >= deadline:
        return count
      sleep(5)

  run_filter = f"JSON_VALUE(log_attributes, '$.\"bqaa.run_id\"') = '{run_id}'"
  checks = (
      (
          "smoke row in otel_logs",
          f"SELECT COUNT(*) FROM `{settings.qualified}.otel_logs`"
          f" WHERE {run_filter}",
      ),
      (
          "smoke point in otel_metric_gauge",
          f"SELECT COUNT(*) FROM `{settings.qualified}.otel_metric_gauge`"
          f" WHERE metric_name = 'bqaa_e2e_{run_id}'",
      ),
      (
          "smoke point in bqaa_metrics view",
          f"SELECT COUNT(*) FROM `{settings.qualified}.bqaa_metrics`"
          f" WHERE metric_name = 'bqaa_e2e_{run_id}'",
      ),
  )
  landed = True
  for name, query in checks:
    count = _wait_count(query)
    results.append(
        CheckResult(
            name=name,
            ok=count >= 1,
            detail=f"{count} rows (waited up to {timeout_s:.0f}s)",
        )
    )
    landed = landed and count >= 1

  if landed:
    # Run the projection MERGE now (scheduled every 15 min in prod) and
    # verify the smoke event projected with the product event name.
    query_rows(otel_sql.agent_events_otlp_merge_sql(settings.qualified))
    rows = query_rows(
        f"SELECT event_type FROM `{settings.qualified}.agent_events_otlp`"
        f" WHERE JSON_VALUE(attributes, '$.\"bqaa.run_id\"') = '{run_id}'"
    )
    ok = bool(rows) and rows[0][0] == "claude_code.user_prompt"
    results.append(
        CheckResult(
            name="smoke event projected into agent_events_otlp",
            ok=ok,
            detail=(
                f"event_type={rows[0][0]!r}" if rows else "no projected row"
            ),
        )
    )

  if "traces" in settings.signals:
    results.append(
        CheckResult(
            name="traces smoke",
            ok=False,
            warning=True,
            detail=(
                "span landing is not implemented yet (#324 traces tier,"
                " final PR of the stack) — otel_spans receives no rows"
            ),
        )
    )
  return results


# --------------------------------------------------------------------------
# Default I/O implementations (used by the CLI)
# --------------------------------------------------------------------------


def default_http_post(url: str, body: bytes, headers: dict) -> tuple[int, str]:
  """POST via urllib; returns (status, body) without raising on 4xx/5xx."""
  import urllib.error
  import urllib.request

  request = urllib.request.Request(url, data=body, headers=headers)
  try:
    with urllib.request.urlopen(request, timeout=30) as response:
      return response.status, response.read().decode("utf-8", "replace")
  except urllib.error.HTTPError as exc:
    return exc.code, exc.read().decode("utf-8", "replace")


def make_query_rows(project: str) -> QueryRows:
  """A QueryRows backed by google-cloud-bigquery."""
  from google.cloud import bigquery

  client = bigquery.Client(project=project)

  def query_rows(query: str) -> list:
    return [tuple(row) for row in client.query(query).result()]

  return query_rows
