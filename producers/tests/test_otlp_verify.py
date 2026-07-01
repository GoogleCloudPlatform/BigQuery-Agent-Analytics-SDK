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

"""Tests for ``bqaa-otel verify`` / smoke (#324 PR3).

``run_verify`` is read-only (reachability, auth enforcement, table/view
existence, recent rows, DLQ health). ``run_smoke`` additionally injects
synthetic OTLP logs+metrics and follows them through the native tables and
the ``agent_events_otlp`` projection — the same path the live e2e test
drives, via shared payload builders.
"""

import json

from bigquery_agent_analytics_tracing.otlp import verify

_SETTINGS = dict(
    endpoint="https://recv.example.run.app",
    token="tok",
    project="my-proj",
    dataset="ds",
)

_ALL_TABLES = [
    ("otel_logs",),
    ("otel_metric_sum",),
    ("otel_metric_gauge",),
    ("otel_metric_histogram",),
    ("otel_metric_exponential_histogram",),
    ("otel_metric_summary",),
    ("otlp_dead_letter",),
    ("agent_events_otlp",),
    ("otel_logs_dedup",),
    ("otel_metric_sum_dedup",),
    ("otel_metric_gauge_dedup",),
    ("otel_metric_histogram_dedup",),
    ("otel_metric_exponential_histogram_dedup",),
    ("otel_metric_summary_dedup",),
    ("bqaa_metrics",),
]


class FakeHttp:
  """(status, body) per (path, authed?); records posts."""

  def __init__(self, unauthed_status=401, authed_status=200):
    self.unauthed_status = unauthed_status
    self.authed_status = authed_status
    self.posts = []  # (url, body, headers)

  def __call__(self, url, body, headers):
    self.posts.append((url, body, headers))
    if "Authorization" in headers:
      return self.authed_status, "{}"
    return self.unauthed_status, "unauthorized"


class FakeBQ:
  """Answers queries by substring; records them."""

  def __init__(self, tables=None, counts=None):
    self.tables = _ALL_TABLES if tables is None else tables
    self.counts = counts or {}
    self.queries = []

  def __call__(self, query):
    self.queries.append(query)
    if "INFORMATION_SCHEMA.TABLES" in query:
      return list(self.tables)
    for needle, value in self.counts.items():
      if needle in query:
        return [(value,)]
    return [(0,)]


def _failures(results):
  return [r for r in results if not r.ok and not r.warning]


# --------------------------------------------------------------------------
# verify (read-only)
# --------------------------------------------------------------------------


def test_verify_all_green():
  results = verify.run_verify(
      verify.VerifySettings(**_SETTINGS),
      http_post=FakeHttp(),
      query_rows=FakeBQ(counts={"otel_logs": 5}),
  )
  assert results and not _failures(results)


def test_verify_flags_unreachable_or_open_endpoint():
  # 200 without auth means the receiver is not enforcing the bearer token.
  results = verify.run_verify(
      verify.VerifySettings(**_SETTINGS),
      http_post=FakeHttp(unauthed_status=200),
      query_rows=FakeBQ(),
  )
  assert any("auth" in r.name for r in _failures(results))


def test_verify_flags_missing_tables():
  tables = [t for t in _ALL_TABLES if t != ("agent_events_otlp",)]
  results = verify.run_verify(
      verify.VerifySettings(**_SETTINGS),
      http_post=FakeHttp(),
      query_rows=FakeBQ(tables=tables),
  )
  bad = _failures(results)
  assert any("agent_events_otlp" in r.detail for r in bad)


def test_verify_does_not_require_otel_spans_unless_traces():
  results = verify.run_verify(
      verify.VerifySettings(**_SETTINGS),
      http_post=FakeHttp(),
      query_rows=FakeBQ(),  # no otel_spans in _ALL_TABLES
  )
  assert not any("otel_spans" in r.detail for r in _failures(results))
  traces = verify.run_verify(
      verify.VerifySettings(**_SETTINGS, signals=("logs", "metrics", "traces")),
      http_post=FakeHttp(),
      query_rows=FakeBQ(),
  )
  assert any("otel_spans" in r.detail for r in _failures(traces))


def test_verify_zero_recent_rows_is_warning_not_failure():
  results = verify.run_verify(
      verify.VerifySettings(**_SETTINGS),
      http_post=FakeHttp(),
      query_rows=FakeBQ(counts={}),  # all counts 0
  )
  recent = [r for r in results if "recent" in r.name]
  assert recent and all(r.warning and not r.ok for r in recent)
  assert not _failures(results)


def test_verify_recent_dead_letters_is_warning():
  results = verify.run_verify(
      verify.VerifySettings(**_SETTINGS),
      http_post=FakeHttp(),
      query_rows=FakeBQ(counts={"otlp_dead_letter": 3, "otel_logs": 5}),
  )
  dlq = [r for r in results if "dead" in r.name]
  assert dlq and dlq[0].warning and "3" in dlq[0].detail
  assert not _failures(results)


def test_verify_is_read_only():
  bq = FakeBQ(counts={"otel_logs": 1})
  http = FakeHttp()
  verify.run_verify(
      verify.VerifySettings(**_SETTINGS), http_post=http, query_rows=bq
  )
  assert not any("MERGE" in q or "INSERT" in q for q in bq.queries)
  # Probe posts carry an empty logs request, never synthetic events.
  assert not any(b"user_prompt" in body for _, body, _ in http.posts)


# --------------------------------------------------------------------------
# smoke (write path)
# --------------------------------------------------------------------------


def test_smoke_sends_logs_and_metrics_and_verifies_projection():
  bq = FakeBQ(
      counts={
          "otel_logs": 1,
          "otel_metric_gauge": 1,
          "bqaa_metrics": 1,
      }
  )
  # Distinguish the projection query: it selects event_type.
  original = bq.__call__

  def with_projection(query):
    if "agent_events_otlp" in query and "event_type" in query:
      bq.queries.append(query)
      return [("claude_code.user_prompt",)]
    return original(query)

  http = FakeHttp()
  results = verify.run_smoke(
      verify.VerifySettings(**_SETTINGS),
      http_post=http,
      query_rows=with_projection,
      sleep=lambda _: None,
  )
  assert not _failures(results)
  paths = [url for url, _, _ in http.posts]
  assert any(url.endswith("/v1/logs") for url in paths)
  assert any(url.endswith("/v1/metrics") for url in paths)
  # The scheduled MERGE is exercised so the projection is verified now.
  assert any("MERGE" in q for q in bq.queries)


def test_smoke_fails_when_rows_never_land():
  results = verify.run_smoke(
      verify.VerifySettings(**_SETTINGS),
      http_post=FakeHttp(),
      query_rows=FakeBQ(counts={}),
      sleep=lambda _: None,
      timeout_s=0,
  )
  assert _failures(results)


def test_smoke_traces_tier_reports_pending_span_landing():
  results = verify.run_smoke(
      verify.VerifySettings(**_SETTINGS, signals=("logs", "metrics", "traces")),
      http_post=FakeHttp(),
      query_rows=FakeBQ(
          counts={"otel_logs": 1, "otel_metric_gauge": 1, "bqaa_metrics": 1}
      ),
      sleep=lambda _: None,
  )
  traces = [r for r in results if "traces" in r.name]
  assert traces and traces[0].warning
  assert "span" in traces[0].detail.lower()


# --------------------------------------------------------------------------
# shared payload builders (also used by the live e2e test)
# --------------------------------------------------------------------------


def test_synthetic_payload_builders_shape():
  logs = verify.synthetic_logs_payload("runid123", now_nanos=42)
  record = logs["resourceLogs"][0]["scopeLogs"][0]["logRecords"][0]
  assert record["eventName"] == "claude_code.user_prompt"
  attrs = {a["key"]: a["value"]["stringValue"] for a in record["attributes"]}
  assert attrs["bqaa.run_id"] == "runid123"
  metrics = verify.synthetic_gauge_payload("runid123", now_nanos=42)
  metric = metrics["resourceMetrics"][0]["scopeMetrics"][0]["metrics"][0]
  assert metric["name"] == "bqaa_e2e_runid123"
  assert json.dumps(logs) and json.dumps(metrics)  # JSON-serializable
