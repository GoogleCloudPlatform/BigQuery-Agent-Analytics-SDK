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
import re
import time
from typing import Any, Callable
import urllib.error
import urllib.request
import uuid

from . import config_artifacts
from . import sql as otel_sql

# (status, body) for a POST; body is bytes, headers a plain dict. Status 0
# means the transport itself failed (unreachable/DNS/TLS/timeout).
HttpPost = Callable[[str, bytes, dict], tuple[int, str]]
# Rows for a query, as sequences indexable like bigquery Row tuples.
QueryRows = Callable[[str], list]

_POLL_INTERVAL_S = 5
_HTTP_TIMEOUT_S = 30
# Each table's real timestamp column: bqaa_metrics is a VIEW that projects
# time_timestamp (no ingest_time), and otlp_dead_letter keys on received_at.
# Querying the wrong column crashes against every healthy deployment.
_TIMESTAMP_COLUMNS = {
    "otel_logs": "ingest_time",
    "bqaa_metrics": "time_timestamp",
    "otlp_dead_letter": "received_at",
}

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

  def __post_init__(self):
    # Same tier gate as config/bootstrap: a typo like 'traces'->'trace'
    # would silently drop the otel_spans existence expectations.
    if frozenset(self.signals) not in config_artifacts.SIGNAL_TIERS:
      raise ValueError(
          f"unsupported signal tier {','.join(self.signals)!r}; expected"
          " 'logs,metrics' or 'logs,metrics,traces'"
      )
    # Both feed backtick-quoted SQL identifiers (settings.qualified): a
    # backtick-bearing value would break out of the quoting and append
    # arbitrary SQL. Allow dots/colons/hyphens in project ids (legacy
    # domain-scoped projects); dataset ids are word characters only.
    if not re.fullmatch(r"[a-z0-9.:-]+", self.project, re.IGNORECASE):
      raise ValueError(f"invalid GCP project id {self.project!r}")
    if not re.fullmatch(r"\w+", self.dataset, re.ASCII):
      raise ValueError(f"invalid BigQuery dataset id {self.dataset!r}")

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


def _recent_count_check(
    settings: VerifySettings,
    query_rows: QueryRows,
    table: str,
    *,
    name: str,
    ok_fn: Callable[[int], bool],
    detail_fn: Callable[[int], str],
) -> CheckResult:
  """COUNT rows in the freshness window; a query error is a failed check.

  A count the deployment considers unhealthy is reported as a warning
  (advisory), never a hard failure — a fresh deployment has no rows and a
  busy one may have dead letters worth inspecting.
  """
  column = _TIMESTAMP_COLUMNS[table]
  try:
    count = query_rows(
        f"SELECT COUNT(*) FROM `{settings.qualified}.{table}` WHERE"
        f" {column} > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL"
        f" {settings.recent_hours} HOUR)"
    )[0][0]
  except Exception as exc:  # noqa: BLE001 - report, never crash the report
    return CheckResult(name=name, ok=False, detail=f"query failed: {exc}")
  ok = ok_fn(count)
  return CheckResult(name=name, ok=ok, detail=detail_fn(count), warning=not ok)


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
  # rejected, an authenticated empty request must be accepted. Status 0
  # means the transport itself failed (unreachable/DNS/TLS/timeout).
  status, detail_body = http_post(logs_url, _empty_logs_body(), {})
  results.append(
      CheckResult(
          name="endpoint auth enforced",
          ok=status == 401,
          detail=(
              f"unreachable: {detail_body}"
              if status == 0
              else f"unauthenticated POST {logs_url} -> {status} (want 401)"
          ),
      )
  )
  status, detail_body = http_post(
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
          detail=(
              f"unreachable: {detail_body}"
              if status == 0
              # A decode-only probe: 200 proves auth + decode, not the
              # Pub/Sub publish path (use --smoke for end-to-end proof).
              else f"authenticated POST {logs_url} -> {status} (want 200;"
              " decode-only probe — use --smoke for the full path)"
          ),
      )
  )

  # 2. Table/view existence. A failing query here (dataset missing,
  # permission denied) is exactly what this check must REPORT, not crash on.
  try:
    existing = {
        row[0]
        for row in query_rows(
            "SELECT table_name FROM"
            f" `{settings.qualified}.INFORMATION_SCHEMA.TABLES`"
        )
    }
  except Exception as exc:  # noqa: BLE001 - report, never crash the report
    results.append(
        CheckResult(
            name="tables and views exist",
            ok=False,
            detail=f"query failed: {exc}",
        )
    )
    return results  # every later check needs the listing
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
    results.append(
        _recent_count_check(
            settings,
            query_rows,
            table,
            name=f"recent rows in {table}",
            ok_fn=lambda count: count > 0,
            detail_fn=lambda count: (
                f"{count} rows in the last {settings.recent_hours}h"
            ),
        )
    )

  # 4. Dead-letter health: rows here mean malformed/failed deliveries.
  if "otlp_dead_letter" not in missing:
    results.append(
        _recent_count_check(
            settings,
            query_rows,
            "otlp_dead_letter",
            name="dead-letter health",
            ok_fn=lambda count: count == 0,
            detail_fn=lambda count: (
                f"{count} dead-lettered records in the last"
                f" {settings.recent_hours}h"
                + ("" if count == 0 else " — inspect otlp_dead_letter.raw_b64")
            ),
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


def _send_counts(body: str) -> tuple[int, int]:
  """(published, dead_lettered) from a receiver response body.

  Unparseable/absent counts read as healthy (-1, 0): only an explicit
  dead-letter or zero-published report fails the send check.
  """
  try:
    parsed = json.loads(body)
    return int(parsed.get("published", -1)), int(parsed.get("dead_lettered", 0))
  except (ValueError, TypeError, AttributeError):
    return -1, 0


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
    # The receiver returns 200 with dead_lettered>0 for per-record decode
    # failures; accepting that would burn the whole polling budget before
    # failing with no diagnostic.
    published, dead = _send_counts(body)
    ok = status == 200 and dead == 0 and published != 0
    results.append(
        CheckResult(
            name=f"smoke send {path}",
            ok=ok,
            detail=(
                f"POST {path} -> {status}"
                + ("" if ok else f" (published={published}, dead={dead})")
            ),
        )
    )
  if any(not r.ok for r in results):
    return results

  # One shared deadline for every landing check: --timeout bounds the whole
  # wait phase, not each check separately.
  deadline = time.monotonic() + timeout_s

  def _wait_count(query: str) -> int:
    while True:
      count = query_rows(query)[0][0]
      if count or time.monotonic() >= deadline:
        return count
      sleep(_POLL_INTERVAL_S)

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
    # A missing table / permission error while polling must become a failed
    # check, not a traceback that also hides the earlier results.
    try:
      count = _wait_count(query)
    except Exception as exc:  # noqa: BLE001 - report, never crash the report
      results.append(
          CheckResult(name=name, ok=False, detail=f"query failed: {exc}")
      )
      landed = False
      continue
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
    # verify the smoke event projected with the product event name. The
    # MERGE upserts on idempotency_key, so racing the scheduled run is
    # data-safe; a serialization error surfaces as a failed check.
    try:
      query_rows(otel_sql.agent_events_otlp_merge_sql(settings.qualified))
      rows = query_rows(
          f"SELECT event_type FROM `{settings.qualified}.agent_events_otlp`"
          f" WHERE JSON_VALUE(attributes, '$.\"bqaa.run_id\"') = '{run_id}'"
      )
    except Exception as exc:  # noqa: BLE001 - report, never crash the report
      results.append(
          CheckResult(
              name="smoke event projected into agent_events_otlp",
              ok=False,
              detail=f"query failed: {exc}",
          )
      )
      return results
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


class _NoRedirect(urllib.request.HTTPRedirectHandler):
  """Never follow redirects.

  urllib would otherwise resend the Authorization header to the redirect
  target (token exfiltration on a compromised/typo'd endpoint) and convert
  the POST to a GET that dead-letters on the receiver. A 3xx surfaces as
  its status code instead.
  """

  def redirect_request(self, *args, **kwargs):
    return None


_NO_REDIRECT_OPENER = urllib.request.build_opener(_NoRedirect)


def default_http_post(url: str, body: bytes, headers: dict) -> tuple[int, str]:
  """POST via urllib; (status, body), never raises.

  4xx/5xx/3xx return their status code; transport failures (DNS,
  connection refused, TLS, timeout) return status 0 with the error text —
  the one condition a reachability check must report gracefully.
  """
  try:
    # Request() itself raises ValueError on malformed/schemeless URLs, so
    # it lives inside the guard to keep the never-raises contract.
    request = urllib.request.Request(url, data=body, headers=headers)
    with _NO_REDIRECT_OPENER.open(request, timeout=_HTTP_TIMEOUT_S) as resp:
      return resp.status, resp.read().decode("utf-8", "replace")
  except urllib.error.HTTPError as exc:
    return exc.code, exc.read().decode("utf-8", "replace")
  except (urllib.error.URLError, OSError, ValueError) as exc:
    return 0, str(exc)


def make_query_rows(project: str) -> QueryRows:
  """A QueryRows backed by google-cloud-bigquery.

  The client is constructed lazily on first use so factory-time failures
  (missing ADC, import problems) surface through the guarded per-check
  calls as ``query failed: ...`` rows instead of crashing the CLI before
  the first check runs.
  """
  client = None

  def query_rows(query: str) -> list:
    nonlocal client
    if client is None:
      from google.cloud import bigquery

      client = bigquery.Client(project=project)
    return [tuple(row) for row in client.query(query).result()]

  return query_rows
