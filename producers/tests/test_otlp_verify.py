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

  def __init__(
      self,
      unauthed_status=401,
      authed_status=200,
      authed_body='{"published": 1, "dead_lettered": 0}',
  ):
    self.unauthed_status = unauthed_status
    self.authed_status = authed_status
    self.authed_body = authed_body
    self.posts = []  # (url, body, headers)

  def __call__(self, url, body, headers):
    self.posts.append((url, body, headers))
    if "Authorization" in headers:
      return self.authed_status, self.authed_body
    return self.unauthed_status, "unauthorized"


class FakeBQ:
  """Answers queries by substring; records them."""

  def __init__(self, tables=None, counts=None, raising=()):
    self.tables = _ALL_TABLES if tables is None else tables
    self.counts = counts or {}
    self.raising = tuple(raising)  # substrings whose queries raise
    self.queries = []

  def __call__(self, query):
    self.queries.append(query)
    for needle in self.raising:
      if needle in query:
        raise RuntimeError(f"boom: {needle}")
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
    if query.lstrip().startswith("SELECT") and "agent_events_otlp" in query:
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


# --------------------------------------------------------------------------
# #332 full-review hardening
# --------------------------------------------------------------------------


def test_verify_freshness_uses_each_tables_real_timestamp_column():
  # bqaa_metrics is a VIEW without ingest_time (it projects time_timestamp),
  # and otlp_dead_letter keys on received_at — querying ingest_time on either
  # crashes verify against every healthy deployment.
  bq = FakeBQ(counts={"otel_logs": 1, "bqaa_metrics": 1})
  verify.run_verify(
      verify.VerifySettings(**_SETTINGS), http_post=FakeHttp(), query_rows=bq
  )
  logs_q = [q for q in bq.queries if ".otel_logs`" in q][0]
  assert "ingest_time" in logs_q
  metrics_q = [q for q in bq.queries if ".bqaa_metrics`" in q][0]
  assert "time_timestamp" in metrics_q
  assert "ingest_time" not in metrics_q
  dlq_q = [q for q in bq.queries if ".otlp_dead_letter`" in q][0]
  assert "received_at" in dlq_q
  assert "ingest_time" not in dlq_q


def test_verify_flags_unreachable_endpoint_as_failure():
  # status 0 is the "transport failed" convention from default_http_post.
  results = verify.run_verify(
      verify.VerifySettings(**_SETTINGS),
      http_post=lambda url, body, headers: (0, "connection refused"),
      query_rows=FakeBQ(),
  )
  assert any(
      "reachable" in r.name or "auth" in r.name for r in _failures(results)
  )


def test_verify_flags_authed_non_200_as_failure():
  results = verify.run_verify(
      verify.VerifySettings(**_SETTINGS),
      http_post=FakeHttp(authed_status=503),
      query_rows=FakeBQ(),
  )
  assert any("reachable" in r.name for r in _failures(results))


def test_verify_survives_missing_dataset_instead_of_crashing():
  # A missing dataset (NotFound on INFORMATION_SCHEMA) is exactly what the
  # existence check should REPORT, not crash on.
  results = verify.run_verify(
      verify.VerifySettings(**_SETTINGS),
      http_post=FakeHttp(),
      query_rows=FakeBQ(raising=("INFORMATION_SCHEMA",)),
  )
  bad = [r for r in results if "exist" in r.name]
  assert bad and not bad[0].ok and "boom" in bad[0].detail


def test_verify_survives_failing_count_query():
  results = verify.run_verify(
      verify.VerifySettings(**_SETTINGS),
      http_post=FakeHttp(),
      query_rows=FakeBQ(raising=(".otel_logs`",)),
  )
  recent = [r for r in results if "otel_logs" in r.name]
  assert recent and not recent[0].ok and "boom" in recent[0].detail


def test_verify_skips_counts_for_missing_tables():
  bq = FakeBQ(tables=[])
  results = verify.run_verify(
      verify.VerifySettings(**_SETTINGS), http_post=FakeHttp(), query_rows=bq
  )
  assert not any("recent" in r.name or "dead" in r.name for r in results)
  assert not any("COUNT(*)" in q for q in bq.queries)


def test_verify_settings_reject_invalid_signal_tier():
  import pytest

  with pytest.raises(ValueError, match="signal"):
    verify.VerifySettings(**_SETTINGS, signals=("logs", "metrics", "trace"))
  with pytest.raises(ValueError, match="signal"):
    verify.VerifySettings(**_SETTINGS, signals=("logs",))


def test_smoke_send_failure_short_circuits_bigquery():
  bq = FakeBQ()
  results = verify.run_smoke(
      verify.VerifySettings(**_SETTINGS),
      http_post=FakeHttp(authed_status=500),
      query_rows=bq,
      sleep=lambda _: None,
  )
  assert _failures(results)
  assert bq.queries == []


def test_smoke_detects_its_own_record_being_dead_lettered():
  # The receiver returns 200 with dead_lettered>0 for per-record decode
  # failures; accepting that would burn the whole polling budget before
  # failing with no diagnostic.
  results = verify.run_smoke(
      verify.VerifySettings(**_SETTINGS),
      http_post=FakeHttp(authed_body='{"published": 0, "dead_lettered": 1}'),
      query_rows=FakeBQ(),
      sleep=lambda _: None,
      timeout_s=0,
  )
  send = [r for r in results if "send" in r.name]
  assert send and all(not r.ok for r in send)
  assert any("dead" in r.detail for r in send)


def test_smoke_projection_mismatch_is_a_failure():
  counts = {"otel_logs": 1, "otel_metric_gauge": 1, "bqaa_metrics": 1}

  def wrong_event(query):
    if query.lstrip().startswith("SELECT") and "agent_events_otlp" in query:
      return [("other.event",)]
    return FakeBQ(counts=counts)(query)

  def no_row(query):
    if query.lstrip().startswith("SELECT") and "agent_events_otlp" in query:
      return []
    return FakeBQ(counts=counts)(query)

  for query_rows in (wrong_event, no_row):
    results = verify.run_smoke(
        verify.VerifySettings(**_SETTINGS),
        http_post=FakeHttp(),
        query_rows=query_rows,
        sleep=lambda _: None,
    )
    projected = [r for r in results if "projected" in r.name]
    assert projected and not projected[0].ok and not projected[0].warning


def test_smoke_retries_until_rows_land():
  calls = {"n": 0}
  sleeps = []

  def eventually(query):
    if "COUNT(*)" in query and ".otel_logs`" in query:
      calls["n"] += 1
      return [(1 if calls["n"] >= 2 else 0,)]
    return FakeBQ(counts={"otel_metric_gauge": 1, "bqaa_metrics": 1})(query)

  def fake_projection(query):
    if query.lstrip().startswith("SELECT") and "agent_events_otlp" in query:
      return [("claude_code.user_prompt",)]
    return eventually(query)

  results = verify.run_smoke(
      verify.VerifySettings(**_SETTINGS),
      http_post=FakeHttp(),
      query_rows=fake_projection,
      sleep=sleeps.append,
      timeout_s=60,
  )
  assert not _failures(results)
  assert calls["n"] == 2
  assert sleeps and sleeps[0] == 5


def test_smoke_survives_failing_merge_or_projection_query():
  counts = {"otel_logs": 1, "otel_metric_gauge": 1, "bqaa_metrics": 1}

  def merge_boom(query):
    if query.lstrip().startswith("MERGE"):
      raise RuntimeError("concurrent update")
    return FakeBQ(counts=counts)(query)

  results = verify.run_smoke(
      verify.VerifySettings(**_SETTINGS),
      http_post=FakeHttp(),
      query_rows=merge_boom,
      sleep=lambda _: None,
  )
  projected = [r for r in results if "projected" in r.name]
  assert projected and not projected[0].ok
  assert "concurrent update" in projected[0].detail


def test_default_http_post_reports_unreachable_as_status_zero():
  status, detail = verify.default_http_post(
      "http://127.0.0.1:1/v1/logs", b"{}", {}
  )
  assert status == 0
  assert detail


def test_default_http_post_does_not_follow_redirects():
  # A redirect would resend the Authorization header to another host and
  # convert the POST to a GET that dead-letters on the receiver; redirects
  # must surface as a status, never be followed.
  assert hasattr(verify, "_NO_REDIRECT_OPENER")
  handler = verify._NoRedirect()
  assert (
      handler.redirect_request(None, None, 303, "See Other", {}, "https://x")
      is None
  )


def test_make_query_rows_converts_rows_to_tuples(monkeypatch):
  class FakeJob:

    def result(self):
      return [[1, "a"], [2, "b"]]

  class FakeClient:

    def __init__(self, project):
      assert project == "my-proj"

    def query(self, query):
      return FakeJob()

  import google.cloud.bigquery as bigquery

  monkeypatch.setattr(bigquery, "Client", FakeClient)
  rows = verify.make_query_rows("my-proj")("SELECT 1")
  assert rows == [(1, "a"), (2, "b")]


def test_smoke_survives_failing_landing_query():
  # A missing table / permission error while polling for landed rows must
  # become a failed check, not a traceback that also hides earlier results.
  def landing_boom(query):
    if "COUNT(*)" in query and ".otel_logs`" in query:
      raise RuntimeError("permission denied")
    return FakeBQ(counts={"otel_metric_gauge": 1, "bqaa_metrics": 1})(query)

  results = verify.run_smoke(
      verify.VerifySettings(**_SETTINGS),
      http_post=FakeHttp(),
      query_rows=landing_boom,
      sleep=lambda _: None,
      timeout_s=0,
  )
  logs_check = [r for r in results if "otel_logs" in r.name]
  assert logs_check and not logs_check[0].ok
  assert "permission denied" in logs_check[0].detail
  # The projection step must not run when a landing check failed.
  assert not any("projected" in r.name for r in results)


def test_make_query_rows_is_lazy_so_credential_errors_become_check_failures(
    monkeypatch,
):
  # Missing ADC / client construction failure must surface through the
  # guarded per-check calls ("query failed: ..."), not crash the CLI
  # before the first check runs.
  import google.cloud.bigquery as bigquery

  def boom(project):
    raise RuntimeError("Could not automatically determine credentials")

  monkeypatch.setattr(bigquery, "Client", boom)
  query_rows = verify.make_query_rows("p")  # must NOT raise here
  results = verify.run_verify(
      verify.VerifySettings(**_SETTINGS),
      http_post=FakeHttp(),
      query_rows=query_rows,
  )
  exist = [r for r in results if "exist" in r.name]
  assert exist and not exist[0].ok
  assert "credentials" in exist[0].detail


def test_default_http_post_never_raises_on_malformed_url():
  # Docstring contract: (status, body), never raises — including a
  # schemeless endpoint that makes urllib's Request constructor throw.
  status, detail = verify.default_http_post(
      "receiver.example.com/v1/logs", b"{}", {}
  )
  assert status == 0
  assert detail


def test_verify_settings_reject_non_identifier_project_and_dataset():
  # project/dataset are interpolated into backtick-quoted SQL identifiers;
  # a backtick-bearing value breaks out of the quoting and appends SQL.
  import pytest

  bad = dict(_SETTINGS)
  bad["dataset"] = "ds` WHERE 1=1; DROP TABLE x;--"
  with pytest.raises(ValueError, match="dataset"):
    verify.VerifySettings(**bad)
  bad = dict(_SETTINGS)
  bad["project"] = "p`.hax"
  with pytest.raises(ValueError, match="project"):
    verify.VerifySettings(**bad)
  # Legitimate ids still pass: underscores, digits, hyphens/dots/colons
  # (legacy domain-scoped projects).
  ok = dict(_SETTINGS)
  ok["project"] = "domain.com:my-proj-1"
  ok["dataset"] = "agent_analytics_2"
  assert verify.VerifySettings(**ok).qualified
