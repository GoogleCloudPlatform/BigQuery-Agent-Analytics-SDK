"""Consolidated unit, chart, and AppTest suite for the Streamlit dashboard."""

from __future__ import annotations

import dataclasses
import datetime as dt
import os
from pathlib import Path
import sys
import types
from unittest import mock

import pytest

pytest.importorskip("streamlit")
pytest.importorskip("plotly")

from google.api_core import exceptions as gexc
from google.auth import exceptions as gauth_exc
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from streamlit.testing.v1 import AppTest

_PREV_SKIP_DOTENV = os.environ.get("BQAA_DASHBOARD_SKIP_DOTENV")
os.environ["BQAA_DASHBOARD_SKIP_DOTENV"] = "1"

DASHBOARDS_DIR = (
    Path(__file__).resolve().parents[1] / "dashboards" / "streamlit"
)
if str(DASHBOARDS_DIR) not in sys.path:
  sys.path.insert(0, str(DASHBOARDS_DIR))

import app
import charts
import models
import queries

if _PREV_SKIP_DOTENV is None:
  os.environ.pop("BQAA_DASHBOARD_SKIP_DOTENV", None)
else:
  os.environ["BQAA_DASHBOARD_SKIP_DOTENV"] = _PREV_SKIP_DOTENV

APP_PATH = DASHBOARDS_DIR / "app.py"


def _app_test() -> AppTest:
  return AppTest.from_file(str(APP_PATH), default_timeout=30)


@pytest.fixture(autouse=True)
def _isolate_env_and_query_state(monkeypatch: pytest.MonkeyPatch):
  """Seeds clean environment variables and resets query cache/dedup state."""
  monkeypatch.setenv("BQAA_DASHBOARD_SKIP_DOTENV", "1")
  monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "")
  monkeypatch.setenv("BQ_PROJECT_ID", "test-project")
  monkeypatch.setenv("BQ_DATASET_ID", "test_dataset")
  monkeypatch.setenv("BQ_TABLE_ID", "events")
  monkeypatch.setenv("BQ_VIEW_PREFIX", "adk_")
  monkeypatch.setenv("STREAMLIT_LAZY_TABS", "false")
  queries._run_query_cached.clear()
  queries._SEEN_RUN_IDS.clear()
  monkeypatch.setattr(queries, "_NEXT_RUN_ID", 0)
  yield
  queries._run_query_cached.clear()
  queries._SEEN_RUN_IDS.clear()


@pytest.fixture
def sample_refs() -> models.TableRefs:
  return models.TableRefs(
      project="test-project",
      dataset="test_dataset",
      table="agent_events",
      view_prefix="adk_",
  )


@pytest.fixture
def sample_window() -> models.Window:
  return models.Window(
      start=dt.datetime(2026, 9, 1, 0, 0, 0, tzinfo=dt.timezone.utc),
      end=dt.datetime(2026, 9, 2, 0, 0, 0, tzinfo=dt.timezone.utc),
  )


@pytest.fixture
def sample_context(
    sample_refs: models.TableRefs, sample_window: models.Window
) -> models.Context:
  return models.Context(
      refs=sample_refs,
      window=sample_window,
      filters=models.Filters(),
      max_bytes=1024**3,
      theme=models.LIGHT_THEME,
      price_in=1.25,
      price_out=5.00,
  )


# ------------------------------------------------------------------ #
# 1. models.py: Identifiers, Windows, Filters & Helpers              #
# ------------------------------------------------------------------ #


def test_table_refs_and_validate_refs():
  """Verifies TableRefs formatting, validation, and injection rejection."""
  refs = models.TableRefs("my-proj", "my_ds", "my_table", "custom_")
  assert refs.events == "`my-proj.my_ds.my_table`"
  assert refs.view("llm_responses") == "`my-proj.my_ds.custom_llm_responses`"

  default_refs = models.TableRefs("my-proj", "my_ds", "my_table")
  assert default_refs.view_prefix == models.DEFAULT_VIEW_PREFIX
  assert (
      default_refs.view("llm_responses") == "`my-proj.my_ds.adk_llm_responses`"
  )

  # Valid identifiers and empty view prefix
  valid, errors = models.validate_refs(
      "valid-project-123", "valid_dataset", "valid_table_id", ""
  )
  assert errors == []
  assert valid is not None
  assert valid.view("llm") == "`valid-project-123.valid_dataset.llm`"

  # Boundary whitespace/newlines are stripped; raw regexes anchor with \Z
  assert not models._PROJECT_RE.match("proj\n")
  assert not models._NAME_RE.match("ds\n")
  assert not models._PREFIX_RE.match("adk_\n")
  stripped, errors = models.validate_refs("proj\n", "ds\n", "tbl\n", "adk_\n")
  assert errors == [] and stripped == models.TableRefs(
      "proj", "ds", "tbl", "adk_"
  )

  # Invalid / injection-attempt identifiers are rejected
  for bad_args, expected_err in [
      (("-invalid", "ds", "tbl", "p_"), "Invalid project ID"),
      (("proj`inject", "ds", "tbl", "p_"), "Invalid project ID"),
      (("proj", "ds-dash", "tbl", "p_"), "Invalid dataset ID"),
      (("proj", "ds`inject", "tbl", "p_"), "Invalid dataset ID"),
      (("proj", "ds", "tbl-dash", "p_"), "Invalid table ID"),
      (("proj", "ds", "tbl`inject", "p_"), "Invalid table ID"),
      (("proj", "ds", "tbl", "bad-prefix"), "Invalid view prefix"),
      (("proj", "ds", "tbl", "p`inject"), "Invalid view prefix"),
  ]:
    bad_refs, errs = models.validate_refs(*bad_args)
    assert bad_refs is None
    assert any(expected_err in e for e in errs)

  # Internal newlines or all-invalid inputs return all 4 errors
  for all_bad in [
      ("", "", "", "bad-prefix"),
      ("proj\nx", "ds\nx", "tbl\nx", "adk_\nx"),
  ]:
    bad_refs, errs = models.validate_refs(*all_bad)
    assert bad_refs is None
    assert len(errs) == 4


def test_window_snap_and_time_bounds():
  """Verifies Window buckets, timestamp snapping, and SQL time_bounds."""
  start = dt.datetime(2026, 9, 1, 12, 0, 0, tzinfo=dt.timezone.utc)
  end = dt.datetime(2026, 9, 1, 14, 30, 0, tzinfo=dt.timezone.utc)
  w = models.Window(start=start, end=end)

  assert w.span == dt.timedelta(hours=2, minutes=30)
  assert queries.time_bounds(w) == (
      'timestamp >= TIMESTAMP "2026-09-01 12:00:00+00:00"\n'
      '  AND timestamp < TIMESTAMP "2026-09-01 14:30:00+00:00"'
  )
  assert queries.time_bounds(w, column="e.timestamp") == (
      'e.timestamp >= TIMESTAMP "2026-09-01 12:00:00+00:00"\n'
      '  AND e.timestamp < TIMESTAMP "2026-09-01 14:30:00+00:00"'
  )

  # Bucket granularity: <= 6h -> MINUTE, <= 3d -> HOUR, > 3d -> DAY
  assert models.Window(start, start + dt.timedelta(hours=6)).bucket == "MINUTE"
  assert (
      models.Window(start, start + dt.timedelta(hours=6, seconds=1)).bucket
      == "HOUR"
  )
  assert models.Window(start, start + dt.timedelta(days=3)).bucket == "HOUR"
  assert (
      models.Window(start, start + dt.timedelta(days=3, seconds=1)).bucket
      == "DAY"
  )
  assert models.Window(start, start + dt.timedelta(days=7)).bucket == "DAY"

  # Snapping to 5-minute intervals, naive/offset UTC conversion, and <=0 guard
  moment = dt.datetime(2026, 9, 1, 12, 34, 56, 789000, tzinfo=dt.timezone.utc)
  expected_snapped = dt.datetime(2026, 9, 1, 12, 30, 0, tzinfo=dt.timezone.utc)
  assert models.snap(moment) == expected_snapped

  win = models.make_window(dt.timedelta(hours=1), now=moment)
  assert (win.start, win.end) == (
      dt.datetime(2026, 9, 1, 11, 30, 0, tzinfo=dt.timezone.utc),
      expected_snapped,
  )
  win_live = models.make_window(dt.timedelta(hours=1))
  assert win_live.end.tzinfo == dt.timezone.utc
  assert win_live.span == dt.timedelta(hours=1)

  assert models.snap(moment, seconds=1) == moment.replace(microsecond=0)
  assert models.snap(moment, seconds=0) == moment.replace(microsecond=0)
  assert models.snap(moment, seconds=-10) == moment.replace(microsecond=0)

  naive = dt.datetime(2026, 9, 1, 12, 34, 56, 789000)
  assert models.snap(naive, seconds=0) == moment.replace(microsecond=0)
  snapped_naive = models.snap(naive, seconds=300)
  assert snapped_naive == expected_snapped
  assert snapped_naive.tzinfo == dt.timezone.utc

  offset_tz = dt.timezone(dt.timedelta(hours=2))
  aware_offset = dt.datetime(2026, 9, 1, 14, 34, 56, 789000, tzinfo=offset_tz)
  snapped_offset = models.snap(aware_offset, seconds=300)
  assert snapped_offset == expected_snapped
  assert snapped_offset.tzinfo == dt.timezone.utc

  # Non-UTC offset timestamps in Window are converted to UTC in time_bounds
  w_offset = models.Window(
      start=dt.datetime(2026, 9, 1, 14, 0, 0, tzinfo=offset_tz),
      end=dt.datetime(2026, 9, 1, 16, 30, 0, tzinfo=offset_tz),
  )
  assert queries.time_bounds(w_offset) == (
      'timestamp >= TIMESTAMP "2026-09-01 12:00:00+00:00"\n'
      '  AND timestamp < TIMESTAMP "2026-09-01 14:30:00+00:00"'
  )

  # Multi-hour snapping aligns across hour boundaries
  m1 = dt.datetime(2026, 9, 1, 12, 59, 0, tzinfo=dt.timezone.utc)
  m2 = dt.datetime(2026, 9, 1, 13, 1, 0, tzinfo=dt.timezone.utc)
  assert models.snap(m1, seconds=7200) == models.snap(m2, seconds=7200)


def test_filters_humanize_bytes_and_context(sample_refs, sample_window):
  """Verifies Filters, humanize_bytes, Context isolation, and stats_known."""
  f_default = models.Filters()
  assert f_default == (
      (models.ALL_SENTINEL,),
      (models.ALL_SENTINEL,),
      (models.ALL_SENTINEL,),
      (models.ALL_SENTINEL,),
  )
  assert models.as_filter_values([]) == (models.ALL_SENTINEL,)
  assert models.as_filter_values(["a", "b"]) == ("a", "b")

  assert models.humanize_bytes(-2048) == "-2.0 KB"
  assert models.humanize_bytes(500) == "500 B"
  assert models.humanize_bytes(1024) == "1.0 KB"
  assert models.humanize_bytes(10 * 1024**2) == "10.0 MB"
  assert models.humanize_bytes(2.5 * 1024**3) == "2.5 GB"
  assert models.humanize_bytes(3 * 1024**4) == "3.0 TB"

  ctx1 = models.Context(
      sample_refs, sample_window, f_default, 1024, models.LIGHT_THEME, 1.0, 2.0
  )
  ctx2 = models.Context(
      sample_refs, sample_window, f_default, 1024, models.LIGHT_THEME, 1.0, 2.0
  )
  ctx1.scan_log.append(models.ScanEntry("q", 10, 10, False, True, True))
  assert ctx2.scan_log == []

  res = models.QueryResult(pd.DataFrame())
  assert (res.bytes_processed, res.bytes_billed, res.cache_hit) == (0, 0, False)
  assert res.stats_known is True
  assert (
      models.QueryResult(pd.DataFrame(), bytes_billed_known=False).stats_known
      is False
  )
  assert (
      models.QueryResult(
          pd.DataFrame(), bytes_processed_known=False
      ).stats_known
      is False
  )
  q_err = queries.QueryExecutionError("boom")
  assert q_err.stats_known is False
  assert (
      queries.QueryExecutionError(
          "boom", bytes_processed_known=True, bytes_billed_known=True
      ).stats_known
      is True
  )
  assert (
      queries.QueryExecutionError(
          "boom", bytes_processed_known=True, bytes_billed_known=False
      ).stats_known
      is False
  )
  assert (
      queries.QueryExecutionError(
          "boom", bytes_processed_known=False, bytes_billed_known=True
      ).stats_known
      is False
  )


# ------------------------------------------------------------------ #
# 2. queries.py: SQL Builders & Parameter Binding                    #
# ------------------------------------------------------------------ #


def test_sql_builders_and_scope(sample_refs, sample_window):
  """Verifies SQL structure and filter scoping across all 15 query builders."""
  # _scope helper
  scope_bare = queries._scope()
  assert (
      "('___ALL___' IN UNNEST(@agents) OR agent IN UNNEST(@agents))"
      in scope_bare
  )
  assert "@event_types" not in scope_bare
  scope_aliased = queries._scope("e", event_type=True)
  assert (
      "('___ALL___' IN UNNEST(@agents) OR e.agent IN UNNEST(@agents))"
      in scope_aliased
  )
  assert (
      "('___ALL___' IN UNNEST(@user_ids) OR e.user_id IN UNNEST(@user_ids))"
      in scope_aliased
  )
  assert (
      "('___ALL___' IN UNNEST(@event_types) OR e.event_type IN"
      " UNNEST(@event_types))" in scope_aliased
  )
  assert (
      "('___ALL___' IN UNNEST(@session_ids) OR e.session_id IN"
      " UNNEST(@session_ids))" in scope_aliased
  )

  # 1. overview_totals (omits @event_types, includes llm_responses subquery)
  ov = queries.build_overview_totals_sql(sample_refs, sample_window)
  assert f"FROM {sample_refs.view('llm_responses')} AS r" in ov
  assert "HAVING COUNT(*) > 0" in ov
  assert "@agents" in ov and "@event_types" not in ov

  # 2. trace_detail (includes @event_types, newest first, coalesced model)
  tr = queries.build_trace_detail_sql(sample_refs, sample_window, limit=500)
  assert "COALESCE(" in tr and "ORDER BY timestamp DESC" in tr
  assert "LIMIT 500" in tr and "@event_types" in tr

  # 3. llm_calls_total (span deduplication + token sums, no SQL cost params)
  llm_tot = queries.build_llm_calls_total_sql(sample_refs, sample_window)
  assert "COUNT(DISTINCT CONCAT(trace_id, '|', span_id))" in llm_tot
  assert "IFNULL(SUM(usage_prompt_tokens), 0) AS prompt_tokens" in llm_tot
  assert "@price_in" not in llm_tot and "estimated_cost_usd" not in llm_tot

  # 4. recent_sessions (WHERE scoped by @session_ids, HAVING LOGICAL_OR)
  sess = queries.build_recent_sessions_sql(
      sample_refs, sample_window, limit=250
  )
  assert "GROUP BY session_id" in sess
  assert "HAVING LOGICAL_OR('___ALL___' IN UNNEST(@agents)" in sess
  assert "AND LOGICAL_OR('___ALL___' IN UNNEST(@event_types)" in sess
  assert "LIMIT 250" in sess

  # 5. tool_errors (UNION ALL of tool_errors and error tool_completions)
  terr = queries.build_tool_errors_sql(sample_refs, sample_window, limit=100)
  assert f"FROM {sample_refs.view('tool_errors')}" in terr
  assert "UNION ALL" in terr
  assert f"FROM {sample_refs.view('tool_completions')}" in terr

  # 6. filter_options (single-pass UNNEST STRUCT pivot, exempt from Grafana)
  fopt = queries.build_filter_options_sql(sample_refs, sample_window)
  for expected_fragment in (
      f"FROM {sample_refs.events} AS e",
      "STRUCT('agent' AS kind, e.agent AS value)",
      "STRUCT('user_id', e.user_id)",
      "STRUCT('event_type', e.event_type)",
      "STRUCT('session_id', e.session_id)",
      queries.time_bounds(sample_window, "e.timestamp"),
      "AND f.value IS NOT NULL",
      "GROUP BY f.kind, f.value",
      "PARTITION BY f.kind ORDER BY MAX(e.timestamp) DESC",
      f"<= {models.FILTER_OPTIONS_LIMIT}",
      "ORDER BY f.kind, f.value",
  ):
    assert expected_fragment in fopt

  # 7-15. Remaining time-series and breakdown builders
  bucket_expr = f"TIMESTAMP_TRUNC(timestamp, {sample_window.bucket}) AS bucket"

  ev_time = queries.build_events_over_time_sql(sample_refs, sample_window)
  assert bucket_expr in ev_time
  assert "@event_types" in ev_time and "GROUP BY bucket, event_type" in ev_time

  err_time = queries.build_errors_over_time_sql(sample_refs, sample_window)
  assert bucket_expr in err_time
  assert "@event_types" not in err_time
  assert "ENDS_WITH(event_type, '_ERROR')" in err_time

  by_agent = queries.build_events_by_agent_sql(sample_refs, sample_window)
  assert "IFNULL(agent, 'unknown') AS agent_name" in by_agent
  assert "@event_types" in by_agent

  top_err = queries.build_top_errors_sql(sample_refs, sample_window)
  assert "AND error_message IS NOT NULL" in top_err
  assert f"LIMIT {models.TOP_ERRORS_LIMIT}" in top_err

  tok_time = queries.build_llm_tokens_over_time_sql(sample_refs, sample_window)
  assert bucket_expr in tok_time
  assert f"FROM {sample_refs.view('llm_responses')}" in tok_time

  lat_pct = queries.build_llm_latency_percentiles_sql(
      sample_refs, sample_window
  )
  assert bucket_expr in lat_pct
  assert "OFFSET(95)] AS p95_total_ms" in lat_pct

  tok_mod = queries.build_tokens_by_model_sql(sample_refs, sample_window)
  assert "IFNULL(model_version, 'unknown') AS model" in tok_mod

  t_usage = queries.build_tool_usage_sql(sample_refs, sample_window)
  assert f"FROM {sample_refs.view('tool_starts')}" in t_usage

  t_lat = queries.build_tool_latency_sql(sample_refs, sample_window)
  assert f"FROM {sample_refs.view('tool_completions')}" in t_lat

  minute_win = models.Window(
      sample_window.start, sample_window.start + dt.timedelta(hours=1)
  )
  for builder in (
      queries.build_events_over_time_sql,
      queries.build_errors_over_time_sql,
      queries.build_llm_tokens_over_time_sql,
      queries.build_llm_latency_percentiles_sql,
  ):
    assert "TIMESTAMP_TRUNC(timestamp, MINUTE) AS bucket" in builder(
        sample_refs, minute_win
    )


def test_query_parameters_and_job_config():
  """Verifies selective parameter binding and live-only maximum_bytes_billed."""
  filters = models.Filters(
      agents=("agent_1",),
      user_ids=("user_1",),
      event_types=("LLM_RESPONSE",),
      session_ids=("sess_1",),
  )
  sql = (
      "SELECT * FROM t WHERE agent IN UNNEST(@agents) AND session_id IN"
      " UNNEST(@session_ids)"
  )
  params = {p.name: p for p in queries.query_parameters(sql, filters)}
  assert sorted(params) == ["agents", "session_ids"]
  assert params["agents"].array_type == "STRING"
  assert list(params["agents"].values) == ["agent_1"]

  all_params = {
      p.name: list(p.values)
      for p in queries.query_parameters(
          queries._scope(event_type=True), filters
      )
  }
  assert all_params == {
      "agents": ["agent_1"],
      "user_ids": ["user_1"],
      "event_types": ["LLM_RESPONSE"],
      "session_ids": ["sess_1"],
  }
  assert queries.query_parameters("SELECT @agents_x", filters) == []

  live_cfg = queries.job_config(sql, filters, 5_000_000, dry_run=False)
  assert live_cfg.maximum_bytes_billed == 5_000_000
  assert live_cfg.dry_run is False
  assert live_cfg.use_legacy_sql is False
  assert live_cfg.use_query_cache is True
  assert live_cfg.labels == {"app": "bqaa_streamlit"}
  assert [p.name for p in live_cfg.query_parameters] == [
      "agents",
      "session_ids",
  ]

  dry_cfg = queries.job_config(sql, filters, 5_000_000, dry_run=True)
  assert dry_cfg.maximum_bytes_billed is None
  assert dry_cfg.dry_run is True

  queries.get_client.clear()
  with mock.patch.object(queries.bigquery, "Client") as mock_bq_client:
    c1 = queries.get_client("proj-cached")
    c2 = queries.get_client("proj-cached")
    assert c1 is c2
    mock_bq_client.assert_called_once_with(project="proj-cached")
  queries.get_client.clear()


# ------------------------------------------------------------------ #
# 3. queries.py: Execution, Guardrails, Caching & Telemetry          #
# ------------------------------------------------------------------ #


def test_explain_actionable_advice():
  """Verifies operator remediation messages retain original error details."""
  msg_auth = queries._explain(gauth_exc.DefaultCredentialsError("No creds"))
  assert "gcloud auth application-default login" in msg_auth
  assert "No creds" in msg_auth

  msg_404 = queries._explain(gexc.NotFound("Dataset not found"))
  assert "bq-agent-sdk views create-all" in msg_404
  assert "Dataset not found" in msg_404

  msg_403 = queries._explain(gexc.Forbidden("Access denied"))
  assert "roles/bigquery.jobUser" in msg_403
  assert "Access denied" in msg_403

  msg_cap = queries._explain(Exception("bytesBilledLimitExceeded"))
  assert "Raise the per-query scan cap" in msg_cap
  assert "bytesBilledLimitExceeded" in msg_cap

  assert queries._explain(ValueError("plain error")) == "plain error"


def test_run_query_guardrail_and_dry_run_errors():
  """Verifies dry-run byte cap guardrail and auth/API dry-run error handling."""
  filters = models.Filters()

  # 1. DefaultCredentialsError on client creation -> known 0 bytes + detail
  with mock.patch.object(
      queries,
      "get_client",
      side_effect=gauth_exc.DefaultCredentialsError("No auth creds"),
  ):
    res_auth = queries.run_query("SELECT 1", filters, "proj", 1000)
    assert res_auth.df.empty and "gcloud auth" in (res_auth.error or "")
    assert "No auth creds" in (res_auth.error or "")
    assert (res_auth.bytes_processed, res_auth.bytes_billed) == (0, 0)
    assert res_auth.cache_hit is False
    assert res_auth.stats_known is True

  # 2. Dry-run estimate > max_bytes trips guardrail with known 0 bytes
  mock_client = mock.MagicMock()
  mock_probe = mock.MagicMock(total_bytes_processed=5000)
  mock_client.query.return_value = mock_probe
  with mock.patch.object(queries, "get_client", return_value=mock_client):
    res_guard = queries.run_query("SELECT * FROM big", filters, "proj", 1000)
    assert res_guard.df.empty and "Guardrail:" in (res_guard.error or "")
    assert (res_guard.bytes_processed, res_guard.bytes_billed) == (0, 0)
    assert res_guard.cache_hit is False
    assert res_guard.stats_known is True

    # Exact cap (estimated == max_bytes) does NOT trip guardrail
    mock_exact = mock.MagicMock(
        total_bytes_processed=1000,
        total_bytes_billed=1000,
        cache_hit=False,
    )
    mock_exact.to_dataframe.return_value = pd.DataFrame({"x": [1]})
    mock_client.query.return_value = mock_exact
    res_exact = queries.run_query("SELECT * FROM exact", filters, "proj", 1000)
    assert res_exact.error is None and not res_exact.df.empty

    # 3. Dry-run API failure -> known 0 bytes
    mock_client.query.side_effect = gexc.GoogleAPICallError("Dry run denied")
    res_api = queries.run_query("SELECT * FROM big", filters, "proj", 1000)
    assert res_api.df.empty and "Dry run denied" in (res_api.error or "")
    assert (res_api.bytes_processed, res_api.bytes_billed) == (0, 0)
    assert res_api.cache_hit is False
    assert res_api.stats_known is True


def test_run_query_caching_and_deduplication(monkeypatch):
  """Verifies @st.cache_data keying and _SEEN_RUN_IDS deduplication/eviction."""
  mock_client = mock.MagicMock()
  mock_job = mock.MagicMock(
      total_bytes_processed=100,
      total_bytes_billed=200,
      cache_hit=False,
  )
  mock_job.to_dataframe.return_value = pd.DataFrame({"col": [1]})
  mock_client.query.return_value = mock_job

  with mock.patch.object(
      queries, "get_client", return_value=mock_client
  ) as mock_get_client:
    f1 = models.Filters(agents=("agent-1",))

    # First run: live execution (dry-run probe + live job = 2 query calls)
    r1 = queries.run_query("SELECT 1", f1, "proj-1", 1_000_000)
    mock_get_client.assert_called_once_with("proj-1")
    mock_job.to_dataframe.assert_called_once_with(create_bqstorage_client=False)
    assert mock_client.query.call_count == 2
    assert [c.args[0] for c in mock_client.query.call_args_list] == [
        "SELECT 1",
        "SELECT 1",
    ]
    cfgs = [c.kwargs["job_config"] for c in mock_client.query.call_args_list]
    assert cfgs[0].dry_run is True and cfgs[0].maximum_bytes_billed is None
    assert (
        cfgs[1].dry_run is False and cfgs[1].maximum_bytes_billed == 1_000_000
    )
    assert (r1.bytes_processed, r1.bytes_billed, r1.cache_hit) == (
        100,
        200,
        False,
    )
    assert r1.error is None and r1.stats_known is True

    # Identical args: served from @st.cache_data; run_query deduplicates billing
    r2 = queries.run_query("SELECT 1", f1, "proj-1", 1_000_000)
    assert mock_client.query.call_count == 2
    assert (r2.bytes_processed, r2.bytes_billed, r2.cache_hit) == (
        100,
        0,
        True,
    )
    assert r2.stats_known is True

    # Changing any of the 4 arguments (sql, filters, project, max_bytes) misses
    queries.run_query("SELECT 2", f1, "proj-1", 1_000_000)
    assert mock_client.query.call_count == 4
    queries.run_query(
        "SELECT 1", models.Filters(agents=("agent-2",)), "proj-1", 1_000_000
    )
    assert mock_client.query.call_count == 6
    queries.run_query("SELECT 1", f1, "proj-2", 1_000_000)
    assert mock_client.query.call_count == 8
    queries.run_query("SELECT 1", f1, "proj-1", 2_000_000)
    assert mock_client.query.call_count == 10

    # Live job with total_bytes_billed=None calls reload(); if reload raises ->
    # rows and known scan bytes are retained, stats_known=False on first run,
    # then True on @st.cache_data hit
    job_unk = mock.MagicMock(
        total_bytes_processed=100,
        total_bytes_billed=None,
        cache_hit=False,
    )
    job_unk.to_dataframe.return_value = pd.DataFrame({"col": [7]})
    job_unk.reload.side_effect = RuntimeError("metadata reload failed")
    mock_client.query.return_value = job_unk
    r_unk = queries.run_query("SELECT unk", f1, "proj-1", 1_000_000)
    job_unk.reload.assert_called_once_with(
        timeout=queries._JOB_RELOAD_TIMEOUT_SECONDS
    )
    assert r_unk.error is None and list(r_unk.df["col"]) == [7]
    assert r_unk.bytes_processed == 100 and r_unk.bytes_processed_known is True
    assert r_unk.bytes_billed_known is False and r_unk.stats_known is False
    r_dedup = queries.run_query("SELECT unk", f1, "proj-1", 1_000_000)
    assert r_dedup.cache_hit is True and r_dedup.bytes_billed == 0
    assert r_dedup.bytes_billed_known is True and r_dedup.stats_known is True

    # Live job with total_bytes_processed=None also triggers reload()
    job_unk.total_bytes_processed, job_unk.total_bytes_billed = None, 200
    r_proc = queries.run_query("SELECT unk_proc", f1, "proj-1", 1_000_000)
    assert job_unk.reload.call_count == 2
    assert (r_proc.bytes_processed, r_proc.bytes_processed_known) == (0, False)
    assert (r_proc.bytes_billed, r_proc.bytes_billed_known) == (200, True)

  # _SEEN_RUN_IDS quarter eviction (max=8 -> evicts 8 // 4 == 2 oldest IDs)
  queries._SEEN_RUN_IDS.clear()
  monkeypatch.setattr(queries, "_MAX_SEEN_RUN_IDS", 8)
  with mock.patch.object(
      queries,
      "_run_query_cached",
      side_effect=[
          (pd.DataFrame(), 100, 100, False, True, True, i) for i in range(1, 10)
      ],
  ):
    for i in range(1, 9):
      queries.run_query(f"Q{i}", f1, "proj", 1000)
    assert queries._SEEN_RUN_IDS == set(range(1, 9))
    queries.run_query("Q9", f1, "proj", 1000)
    assert queries._SEEN_RUN_IDS == {3, 4, 5, 6, 7, 8, 9}


def test_job_stats_extraction_and_download_failure_recovery():
  """Verifies _extract_job_stats, BQ cache hits, and download failure paths."""
  # 1. Reloads when total_bytes_processed or total_bytes_billed is None
  job = mock.MagicMock(
      total_bytes_processed=None,
      total_bytes_billed=None,
      ended=None,
      cache_hit=False,
  )

  def populate_on_reload(**kwargs):
    del kwargs
    job.total_bytes_processed = 2048
    job.total_bytes_billed = 10_485_760

  job.reload.side_effect = populate_on_reload
  assert queries._extract_job_stats(job) == (
      2048,
      10_485_760,
      False,
      True,
      True,
  )
  job.reload.assert_called_once_with(
      timeout=queries._JOB_RELOAD_TIMEOUT_SECONDS
  )

  # 2. BigQuery cache_hit=True forces bytes_billed=0 and billed_known=True
  job_bq_cache = mock.MagicMock(
      total_bytes_processed=4096,
      total_bytes_billed=None,
      ended=dt.datetime.now(dt.timezone.utc),
      cache_hit=True,
  )
  assert queries._extract_job_stats(job_bq_cache) == (
      4096,
      0,
      True,
      True,
      True,
  )

  # 3. Partial stats after reload failure preserve whichever stat is known
  job_partial = mock.MagicMock(
      total_bytes_processed=2048,
      total_bytes_billed=None,
      ended=dt.datetime.now(dt.timezone.utc),
      cache_hit=False,
  )
  job_partial.reload.side_effect = RuntimeError("reload failed")
  assert queries._extract_job_stats(job_partial) == (
      2048,
      0,
      False,
      True,
      False,
  )
  job_partial.reload.assert_called_once()
  job_partial.total_bytes_processed, job_partial.cache_hit = None, True
  assert queries._extract_job_stats(job_partial) == (0, 0, True, False, True)

  job_only_billed = mock.MagicMock(
      total_bytes_processed=None,
      total_bytes_billed=4096,
      ended=dt.datetime.now(dt.timezone.utc),
      cache_hit=False,
  )
  job_only_billed.reload.side_effect = RuntimeError("reload failed")
  assert queries._extract_job_stats(job_only_billed) == (
      0,
      4096,
      False,
      False,
      True,
  )
  job_only_billed.reload.assert_called_once()

  # 4. Degraded job (neither stat known, ended=None, reload fails) -> None
  assert queries._extract_job_stats(None) is None
  job_dead = mock.MagicMock(
      total_bytes_processed=None,
      total_bytes_billed=None,
      ended=None,
      cache_hit=False,
  )
  job_dead.reload.side_effect = RuntimeError("gone")
  assert queries._extract_job_stats(job_dead) is None
  err = queries._query_failure("download boom", job_dead)
  assert type(err) is RuntimeError and str(err) == "download boom"

  # 5. Result download failure retains job stats when known, or marks unknown
  mock_client = mock.MagicMock()
  mock_probe = mock.MagicMock(total_bytes_processed=1000)
  mock_failed_dl = mock.MagicMock(
      total_bytes_processed=20480,
      total_bytes_billed=10_485_760,
      cache_hit=False,
      ended=dt.datetime.now(dt.timezone.utc),
  )
  mock_failed_dl.to_dataframe.side_effect = RuntimeError("Arrow stream broke")
  mock_cached_dl = mock.MagicMock(
      total_bytes_processed=8192,
      total_bytes_billed=0,
      cache_hit=True,
      ended=dt.datetime.now(dt.timezone.utc),
  )
  mock_cached_dl.to_dataframe.side_effect = gexc.ServiceUnavailable("503 cache")
  job_dead.to_dataframe.side_effect = gexc.BadRequest(
      "bytesBilledLimitExceeded"
  )
  mock_client.query.side_effect = [
      mock_probe,
      mock_failed_dl,
      mock_probe,
      mock_cached_dl,
      mock_probe,
      job_dead,
      mock_probe,
      job_bq_cache,
  ]
  job_bq_cache.to_dataframe.return_value = pd.DataFrame({"x": [1]})

  with mock.patch.object(queries, "get_client", return_value=mock_client):
    res = queries.run_query(
        "SELECT * FROM events", models.Filters(), "p", 10**8
    )
    assert res.df.empty and res.error is not None
    assert (
        res.bytes_processed,
        res.bytes_billed,
        res.cache_hit,
        res.stats_known,
    ) == (
        20480,
        10_485_760,
        False,
        True,
    )

    res_cached_dl = queries.run_query(
        "SELECT * FROM cached_fail", models.Filters(), "p", 10**8
    )
    assert res_cached_dl.df.empty and res_cached_dl.error is not None
    assert (
        res_cached_dl.bytes_processed,
        res_cached_dl.bytes_billed,
        res_cached_dl.cache_hit,
        res_cached_dl.stats_known,
    ) == (8192, 0, True, True)

    res_dead = queries.run_query(
        "SELECT * FROM dead", models.Filters(), "p", 10**8
    )
    assert res_dead.df.empty and "Raise the per-query scan cap" in (
        res_dead.error or ""
    )
    assert (
        res_dead.bytes_processed,
        res_dead.bytes_billed,
        res_dead.cache_hit,
    ) == (0, 0, False)
    assert (
        res_dead.bytes_processed_known,
        res_dead.bytes_billed_known,
        res_dead.stats_known,
    ) == (False, False, False)

    res_bq_hit = queries.run_query(
        "SELECT * FROM hit", models.Filters(), "p", 10**8
    )
    assert (
        res_bq_hit.bytes_processed,
        res_bq_hit.bytes_billed,
        res_bq_hit.cache_hit,
        res_bq_hit.stats_known,
    ) == (4096, 0, True, True)


def test_fetch_and_load_filter_options(sample_context):
  """Verifies fetch scan_log recording and load_filter_options fanout/errors."""
  ctx = dataclasses.replace(
      sample_context, filters=models.Filters(agents=("agent-a",))
  )
  df = pd.DataFrame(
      [
          {"kind": "agent", "value": "agent-a"},
          {"kind": "agent", "value": "agent-b"},
          {"kind": "event_type", "value": "start"},
          {"kind": "session_id", "value": "sess-1"},
          {"kind": "user_id", "value": "user-1"},
      ]
  )
  ok_res = models.QueryResult(
      df=df,
      error=None,
      bytes_processed=50,
      bytes_billed=100,
      cache_hit=False,
      bytes_processed_known=True,
      bytes_billed_known=False,
  )
  with (
      mock.patch.object(queries, "run_query", return_value=ok_res) as mock_run,
      mock.patch.object(queries.st, "spinner") as mock_spinner,
      mock.patch.object(queries.st, "error") as mock_ok_err,
  ):
    options, res = queries.load_filter_options(ctx)
    mock_ok_err.assert_not_called()
    mock_spinner.assert_called_once_with("Loading Filter options...")
    mock_run.assert_called_once_with(
        queries.build_filter_options_sql(ctx.refs, ctx.window),
        ctx.filters,
        "test-project",
        1024**3,
    )
    assert options == {
        "agent": ["agent-a", "agent-b"],
        "event_type": ["start"],
        "session_id": ["sess-1"],
        "user_id": ["user-1"],
    }
    assert res is ok_res
    assert ctx.scan_log == [
        models.ScanEntry(
            label="Filter options",
            bytes_billed=100,
            bytes_processed=50,
            cache_hit=False,
            bytes_billed_known=False,
            bytes_processed_known=True,
        )
    ]

  # Zero-byte cached/empty result and error path in fetch()
  err_res = models.QueryResult(
      df=pd.DataFrame(),
      error="Access Denied",
      bytes_processed=0,
      bytes_billed=0,
      cache_hit=True,
  )
  with (
      mock.patch.object(queries, "run_query", return_value=err_res),
      mock.patch.object(queries.st, "spinner"),
      mock.patch.object(queries.st, "error") as mock_st_err,
  ):
    options, res = queries.load_filter_options(ctx)
    assert options == {} and res.error == "Access Denied"
    mock_st_err.assert_called_once_with("**Filter options** — Access Denied")
    assert ctx.scan_log[-1] == models.ScanEntry(
        label="Filter options",
        bytes_billed=0,
        bytes_processed=0,
        cache_hit=True,
        bytes_billed_known=True,
        bytes_processed_known=True,
    )


# ------------------------------------------------------------------ #
# 4. charts.py: Data Transformations & Panel Branching               #
# ------------------------------------------------------------------ #


def test_fold_others():
  """Verifies top-(limit-1) selection, Other tail sum, boundaries, and ties."""
  df = pd.DataFrame(
      {
          "cat": [f"cat_{i}" for i in range(10)],
          "val": [10 * (i + 1) for i in range(10)],
      }
  )
  folded = charts.fold_others(df, key="cat", value="val", limit=5)
  assert list(folded.columns) == ["cat", "val"]
  assert folded["cat"].nunique() == 5
  assert folded["val"].sum() == df["val"].sum()
  assert folded[folded["cat"] == models.OTHER_LABEL]["val"].iloc[0] == 210

  # Exact limit (nunique == limit), within limit, and empty return unchanged
  df_exact = pd.DataFrame(
      {"cat": [f"c{i}" for i in range(5)], "val": [1, 2, 3, 4, 5]}
  )
  assert charts.fold_others(df_exact, "cat", "val", limit=5).equals(df_exact)
  df_small = pd.DataFrame({"cat": ["a", "b"], "val": [1, 2]})
  assert charts.fold_others(df_small, "cat", "val", limit=5).equals(df_small)
  assert charts.fold_others(
      pd.DataFrame(columns=["cat", "val"]), "cat", "val"
  ).empty
  assert charts.fold_others(pd.DataFrame(), "cat", "val").empty

  # Ties at the limit boundary preserve total sum and cap at limit
  df_ties = pd.DataFrame(
      {"cat": ["a", "b", "c", "d", "e"], "val": [10, 10, 10, 10, 10]}
  )
  folded_ties = charts.fold_others(df_ties, "cat", "val", limit=3)
  assert folded_ties["cat"].nunique() == 3
  assert folded_ties["val"].sum() == 50

  # Per-group exact values with group_cols
  df_grouped = pd.DataFrame(
      {
          "bucket": ["b1", "b1", "b1", "b2", "b2", "b2"],
          "cat": ["A", "B", "C", "A", "B", "C"],
          "val": [10, 5, 2, 20, 10, 3],
      }
  )
  res = charts.fold_others(
      df_grouped, "cat", "val", group_cols=["bucket"], limit=2
  )
  assert list(res.columns) == ["bucket", "cat", "val"]
  by_group = {(r["bucket"], r["cat"]): r["val"] for _, r in res.iterrows()}
  assert by_group == {
      ("b1", "A"): 10,
      ("b1", models.OTHER_LABEL): 7,
      ("b2", "A"): 20,
      ("b2", models.OTHER_LABEL): 13,
  }


def test_color_map():
  """Verifies stable session slot allocation, collisions, and overflow."""
  state: dict[str, object] = {}
  with mock.patch.object(charts.st, "session_state", state):
    theme = models.LIGHT_THEME
    cm1 = charts.color_map(
        "agent",
        ["agent_a", "agent_a", models.OTHER_LABEL, "agent_b"],
        theme,
    )
    assert cm1["agent_a"] == theme.categorical[0]
    assert cm1["agent_b"] == theme.categorical[1]
    assert cm1[models.OTHER_LABEL] == theme.muted
    assert state["_slots::agent"] == {"agent_a": 0, "agent_b": 1}

    # Second call retains agent_b's slot (1) and gives free slot (0) to agent_c
    cm2 = charts.color_map("agent", ["agent_b", "agent_c"], theme)
    assert cm2["agent_b"] == theme.categorical[1]
    assert cm2["agent_c"] == theme.categorical[0]

    # Intra-chart collision resolution preserves k1 and reassigns k2 to slot 1
    state["_slots::collision"] = {"k1": 0, "k2": 0}
    cm_coll = charts.color_map("collision", ["k1", "k2"], theme)
    assert cm_coll["k1"] == theme.categorical[0]
    assert cm_coll["k2"] == theme.categorical[1]

    # Overflow past 8 slots wraps cleanly via modulo
    cm_over = charts.color_map("overflow", [f"n_{i}" for i in range(12)], theme)
    assert len(cm_over) == 13
    assert cm_over["n_8"] == theme.categorical[0]
    assert cm_over["n_9"] == theme.categorical[1]


def test_chart_builders_data_shaping(sample_context):
  """Verifies theme selection, layout invariants, and data shaping in charts."""
  # 0. active_theme() resolves st.context.theme.type and get_option fallback
  dark_ctx = types.SimpleNamespace(theme=types.SimpleNamespace(type="DARK"))
  none_ctx = types.SimpleNamespace(theme=types.SimpleNamespace(type=None))
  with mock.patch.object(charts.st, "context", dark_ctx):
    assert charts.active_theme() == models.DARK_THEME
  with (
      mock.patch.object(charts.st, "context", none_ctx),
      mock.patch.object(
          charts.st, "get_option", return_value="dark"
      ) as mock_opt,
  ):
    assert charts.active_theme() == models.DARK_THEME
    mock_opt.assert_called_once_with("theme.base")
  with (
      mock.patch.object(charts.st, "context", None),
      mock.patch.object(charts.st, "get_option", return_value=None),
  ):
    assert charts.active_theme() == models.LIGHT_THEME

  for theme in (models.LIGHT_THEME, models.DARK_THEME):
    base = charts.base_figure(theme, height=360)
    assert base.layout.height == 360
    assert base.layout.paper_bgcolor == theme.surface
    assert base.layout.plot_bgcolor == theme.surface
    assert base.layout.font.color == theme.text_secondary
    assert base.layout.hoverlabel.bgcolor == theme.surface
    assert base.layout.legend.orientation == "h"
    assert base.layout.xaxis.gridcolor == theme.grid
    assert base.layout.xaxis.linecolor == theme.axis
    assert base.layout.xaxis.tickfont.color == theme.muted
    assert base.layout.xaxis.showline is True
    assert base.layout.yaxis.showline is False
    assert base.layout.showlegend is False

  state: dict[str, object] = {}
  with mock.patch.object(charts.st, "session_state", state):
    # 1. stacked_bars folds >8 categories into Other with distinct bar traces
    df_stack = pd.DataFrame(
        {
            "bucket": ["2026-01-01"] * 10,
            "type": [f"t_{i}" for i in range(10)],
            "cnt": range(1, 11),
        }
    )
    fig_stack = charts.stacked_bars(
        df_stack, "bucket", "type", "cnt", sample_context, "type"
    )
    assert len(fig_stack.data) == 8
    trace_xy = {t.name: (list(t.x), list(t.y)) for t in fig_stack.data}
    assert trace_xy[models.OTHER_LABEL] == (["2026-01-01"], [6])
    assert trace_xy["t_9"] == (["2026-01-01"], [10])
    other_colors = {
        t.marker.color for t in fig_stack.data if t.name == models.OTHER_LABEL
    }
    assert other_colors == {sample_context.theme.muted}
    assert len({t.marker.color for t in fig_stack.data}) == 8
    assert fig_stack.layout.barmode == "stack"
    assert fig_stack.layout.bargap == 0.25
    assert fig_stack.layout.showlegend is True
    assert fig_stack.layout.hovermode == "x unified"

    # Single-category stacked_bars hides legend; 2 categories shows legend
    fig_single = charts.stacked_bars(
        pd.DataFrame({"bucket": ["2026-01-01"], "type": ["only"], "cnt": [3]}),
        "bucket",
        "type",
        "cnt",
        sample_context,
        "type",
    )
    assert fig_single.layout.showlegend is False
    assert (
        charts.stacked_bars(
            df_stack.iloc[:2], "bucket", "type", "cnt", sample_context, "type"
        ).layout.showlegend
        is True
    )

    # 2. ranked_bars keeps top N and sorts ascending for horizontal rendering
    df_rank = pd.DataFrame(
        {"agent": [f"agent_{i}" for i in range(15)], "count": range(0, 150, 10)}
    )
    fig_rank = charts.ranked_bars(
        df_rank, "agent", "count", sample_context, top=5
    )
    assert fig_rank.data[0].orientation == "h"
    assert list(fig_rank.data[0].x) == [100, 110, 120, 130, 140]
    assert list(fig_rank.data[0].y) == [f"agent_{i}" for i in range(10, 15)]
    assert fig_rank.data[0].marker.color == sample_context.theme.categorical[0]
    assert fig_rank.layout.bargap == 0.35
    assert fig_rank.layout.xaxis.tickformat == ","
    assert fig_rank.layout.showlegend is False

    # 3. grouped_bars ranks by first series and creates one horizontal trace/series
    df_grp = pd.DataFrame(
        {
            "model": ["m1", "m2", "m3"],
            "prompt": [50, 300, 200],
            "comp": [900, 100, 150],
        }
    )
    fig_grp = charts.grouped_bars(
        df_grp,
        "model",
        [("prompt", "Prompt"), ("comp", "Completion")],
        sample_context,
        "tok",
        top=2,
    )
    assert [t.name for t in fig_grp.data] == ["Prompt", "Completion"]
    assert all(t.orientation == "h" for t in fig_grp.data)
    assert list(fig_grp.data[0].y) == ["m3", "m2"]
    assert list(fig_grp.data[0].x) == [200, 300]
    assert list(fig_grp.data[1].x) == [150, 100]
    assert fig_grp.data[0].marker.color != fig_grp.data[1].marker.color
    assert fig_grp.layout.barmode == "group"
    assert fig_grp.layout.bargap == 0.3
    assert fig_grp.layout.bargroupgap == 0.08
    assert fig_grp.layout.xaxis.tickformat == ","
    assert fig_grp.layout.showlegend is True

    # 4. lines() sorts by x, disables connectgaps, and labels last real point <= 4
    df_lines = pd.DataFrame(
        {
            "bucket": ["2026-01-02", "2026-01-01"],
            "s1": [20.0, 10.0],
            "s2": [float("nan"), 15.0],
            "s_nan": [float("nan"), float("nan")],
            "s3": [30.0, 20.0],
            "s4": [35.0, 25.0],
            "s5": [40.0, 30.0],
        }
    )
    fig_four = charts.lines(
        df_lines,
        "bucket",
        [("s1", "S1"), ("s2", "S2"), ("s_nan", "SN"), ("s3", "S3")],
        sample_context,
        "dom",
        unit=" ms",
    )
    assert list(fig_four.data[0].x) == ["2026-01-01", "2026-01-02"]
    assert list(fig_four.data[0].y) == [10.0, 20.0]
    assert all(
        t.mode == "lines"
        and t.connectgaps is False
        and " ms" in t.hovertemplate
        for t in fig_four.data
    )
    assert [(a.text, a.x, a.y) for a in fig_four.layout.annotations] == [
        ("S1", "2026-01-02", 20.0),
        ("S2", "2026-01-01", 15.0),
        ("S3", "2026-01-02", 30.0),
    ]
    assert fig_four.layout.showlegend is True
    assert fig_four.layout.hovermode == "x unified"
    assert fig_four.layout.margin.r == 96

    fig_five = charts.lines(
        df_lines,
        "bucket",
        [(f"s{i}", f"S{i}") for i in range(1, 6)],
        sample_context,
        "dom",
    )
    assert len(fig_five.layout.annotations) == 0
    assert fig_five.layout.showlegend is True
    assert fig_five.layout.margin.r == 8


def test_panel_branches():
  """Verifies panel() empty caption, chart + expander, and table-only view."""
  with (
      mock.patch.object(charts.st, "markdown") as mock_md,
      mock.patch.object(charts.st, "caption") as mock_caption,
      mock.patch.object(charts.st, "plotly_chart") as mock_plotly,
      mock.patch.object(charts.st, "expander") as mock_expander,
      mock.patch.object(charts.st, "dataframe") as mock_df,
  ):
    charts.panel("Empty", None, pd.DataFrame(), empty="Nothing.")
    mock_md.assert_called_once_with("**Empty**")
    mock_caption.assert_called_once_with("Nothing.")
    mock_plotly.assert_not_called()
    mock_expander.assert_not_called()
    mock_df.assert_not_called()

    mock_caption.reset_mock()
    charts.panel("Default Empty", None, pd.DataFrame())
    mock_caption.assert_called_once_with("No matching data in this range.")

    mock_caption.reset_mock()
    fig = go.Figure()
    df = pd.DataFrame([{"a": 1}])
    charts.panel("With Chart", fig, df, key="k")
    mock_plotly.assert_called_once_with(fig, width="stretch", key="k")
    mock_expander.assert_called_once_with("Table view", expanded=False)
    mock_df.assert_called_once_with(df, width="stretch", hide_index=True)

    mock_plotly.reset_mock()
    mock_expander.reset_mock()
    mock_df.reset_mock()
    charts.panel("Chart No Key", fig, df)
    mock_plotly.assert_called_once_with(fig, width="stretch", key=None)

    mock_plotly.reset_mock()
    mock_expander.reset_mock()
    mock_df.reset_mock()
    charts.panel("Table Only", None, df)
    mock_plotly.assert_not_called()
    mock_expander.assert_called_once_with("Table view", expanded=True)
    mock_df.assert_called_once_with(df, width="stretch", hide_index=True)


# ------------------------------------------------------------------ #
# 5. app.py: Business Logic & End-to-End AppTest Flows               #
# ------------------------------------------------------------------ #


def test_app_helpers_and_sidebar_connection(monkeypatch):
  """Verifies _seed_options, _default_for, and env vs form project handling."""
  all_sentinel = (models.ALL_SENTINEL,)
  state = {
      "flt_agent": ["agent-pending", "", models.ALL_SENTINEL, "agent-1"],
      "flt_scalar": "scalar-val",
      "flt_empty_scalar": "",
  }
  with mock.patch.object(app.st, "session_state", state):
    assert app._seed_options(
        "flt_agent",
        ["agent-1"],
        ("agent-applied", "", models.ALL_SENTINEL, "agent-applied"),
    ) == [
        "agent-1",
        "agent-applied",
        "agent-pending",
    ]
    assert app._seed_options("flt_missing", ["opt-1"], all_sentinel) == [
        "opt-1"
    ]
    assert app._seed_options("flt_scalar", ["opt-1"], all_sentinel) == [
        "opt-1",
        "scalar-val",
    ]
    assert app._seed_options("flt_empty_scalar", ["opt-1"], all_sentinel) == [
        "opt-1"
    ]
    assert app._pending("flt_agent") == [
        "agent-pending",
        "",
        models.ALL_SENTINEL,
        "agent-1",
    ]
    assert app._pending("flt_scalar") == ["scalar-val"]
    assert app._pending("flt_empty_scalar") == []
    assert app._pending("flt_missing") == []

  assert app._default_for(all_sentinel) == []
  assert app._default_for(("agent-x",)) == ["agent-x"]

  # 1. When BQ_PROJECT_ID is set in env, user input cannot override it
  monkeypatch.setenv("BQ_PROJECT_ID", "env-locked-project")
  monkeypatch.setenv("BQ_DATASET_ID", "env_dataset")
  proj_disabled_flags: list[bool] = []

  def _fake_text_input(label, value="", **kw):
    if label == "Project ID":
      proj_disabled_flags.append(bool(kw.get("disabled")))
      return "injected-proj"
    return value

  with (
      mock.patch.object(app.st.sidebar, "form", return_value=mock.MagicMock()),
      mock.patch.object(app.st, "text_input", side_effect=_fake_text_input),
      mock.patch.object(app.st, "selectbox", return_value="1 GB"),
      mock.patch.object(app.st, "form_submit_button", return_value=False),
      mock.patch.object(app.st, "session_state", {}),
  ):
    refs, cap = app.sidebar_connection()
    assert refs is not None and refs.project == "env-locked-project"
    assert cap == models.BYTES_CAPS["1 GB"]

    # 2. When BQ_PROJECT_ID is unset, sidebar uses the form's Project ID
    monkeypatch.delenv("BQ_PROJECT_ID", raising=False)
    refs_form, _ = app.sidebar_connection()
    assert refs_form is not None and refs_form.project == "injected-proj"
    assert proj_disabled_flags == [True, False]

  # 3. Blank dataset before Connect click does not render sidebar error,
  #    whereas invalid dataset after Connect click surfaces st.sidebar.error
  monkeypatch.delenv("BQ_DATASET_ID", raising=False)
  conn_state: dict[str, object] = {}
  with (
      mock.patch.object(app.st.sidebar, "form", return_value=mock.MagicMock()),
      mock.patch.object(
          app.st, "text_input", side_effect=lambda label, value="", **kw: value
      ),
      mock.patch.object(app.st, "selectbox", return_value="1 GB"),
      mock.patch.object(
          app.st, "form_submit_button", return_value=False
      ) as mock_submit,
      mock.patch.object(app.st, "session_state", conn_state),
      mock.patch.object(app.st.sidebar, "error") as mock_error,
  ):
    refs, _ = app.sidebar_connection()
    assert refs is None
    mock_error.assert_not_called()

    # Clicking Connect with an empty dataset sets _connect_attempted and errors
    monkeypatch.setenv("BQ_PROJECT_ID", "test-project")
    mock_submit.return_value = True
    refs_empty_submit, _ = app.sidebar_connection()
    assert refs_empty_submit is None
    assert conn_state["_connect_attempted"] is True
    mock_error.assert_called_once_with("Invalid dataset ID: ''")
    mock_error.reset_mock()

    # Next rerun (submit=False) still shows error while _connect_attempted is latched
    mock_submit.return_value = False
    refs_latched, _ = app.sidebar_connection()
    assert refs_latched is None
    mock_error.assert_called_once_with("Invalid dataset ID: ''")
    mock_error.reset_mock()

    conn_state.clear()
    monkeypatch.setenv("BQ_DATASET_ID", "bad-ds")
    refs_bad, _ = app.sidebar_connection()
    assert refs_bad is None
    mock_error.assert_called_once_with("Invalid dataset ID: 'bad-ds'")


def test_row_overview_and_llm_metrics(sample_context):
  """Verifies KPI formatting, empty states, and chart wiring across tabs."""
  metric_calls: list[tuple[int, str, str, str | None]] = []
  panel_figs: dict[str, go.Figure | None] = {}
  panel_kwargs: dict[str, dict[str, object]] = {}

  def fake_cols(n):
    count = n if isinstance(n, int) else len(n)
    cols = []
    for idx in range(count):
      col = mock.MagicMock()
      col.metric.side_effect = (
          lambda label, val, help=None, i=idx: metric_calls.append(
              (i, label, val, help)
          )
      )
      cols.append(col)
    return cols

  def make_fetch(kpi_label: str, kpi_df: pd.DataFrame, charts_map: dict):
    def _fetch(sql, ctx, label):
      del ctx
      assert sql.strip()
      df = (
          kpi_df
          if label == kpi_label
          else charts_map.get(label, pd.DataFrame())
      )
      return models.QueryResult(df)

    return _fetch

  def record_panel(title, fig, df, **kw):
    del df
    panel_figs[title] = fig
    panel_kwargs[title] = kw

  # 1. row_overview empty, normal, and None/NaN KPI values + chart wiring
  err_help = (
      "An event counts as an error when its type ends in _ERROR, it carries an"
      " error message, or its status is ERROR."
  )
  ov_charts = {
      "Events over time": pd.DataFrame(
          [{"bucket": "2026-01-01", "event_type": "E1", "events": 5}]
      ),
      "Errors over time": pd.DataFrame(
          [{"bucket": "2026-01-01", "event_type": "ERR", "errors": 2}]
      ),
      "Events by agent": pd.DataFrame([{"agent_name": "ag-1", "events": 5}]),
  }
  for df_ov, expected in [
      (
          pd.DataFrame(),
          [
              (0, "Sessions", "—", None),
              (1, "Events", "—", None),
              (2, "Error rate", "—", None),
              (3, "Avg LLM latency", "—", None),
          ],
      ),
      (
          pd.DataFrame(
              [
                  {
                      "sessions": 1234,
                      "events": 56789,
                      "error_rate": 0.0731,
                      "avg_llm_latency_ms": 1234.5,
                  }
              ]
          ),
          [
              (0, "Sessions", "1,234", None),
              (1, "Events", "56,789", None),
              (2, "Error rate", "7.31%", err_help),
              (3, "Avg LLM latency", "1,235 ms", None),
          ],
      ),
      (
          pd.DataFrame(
              [
                  {
                      "sessions": 1234,
                      "events": 56789,
                      "error_rate": float("nan"),
                      "avg_llm_latency_ms": None,
                  }
              ]
          ),
          [
              (0, "Sessions", "1,234", None),
              (1, "Events", "56,789", None),
              (2, "Error rate", "—", err_help),
              (3, "Avg LLM latency", "—", None),
          ],
      ),
  ]:
    metric_calls.clear()
    with (
        mock.patch.object(app.st, "session_state", {}),
        mock.patch.object(app.st, "caption"),
        mock.patch.object(
            app,
            "fetch",
            side_effect=make_fetch("Overview stats", df_ov, ov_charts),
        ),
        mock.patch.object(app.st, "columns", side_effect=fake_cols),
        mock.patch.object(app, "panel", side_effect=record_panel),
    ):
      app.row_overview(sample_context)
      assert metric_calls == expected

  assert [t.name for t in panel_figs["Events over time"].data] == ["E1"]
  assert [t.name for t in panel_figs["Errors over time"].data] == ["ERR"]
  assert list(panel_figs["Events by agent"].data[0].y) == ["ag-1"]
  assert panel_kwargs["Errors over time"]["empty"] == (
      "No errors in this range."
  )
  assert panel_kwargs["Top error messages"]["empty"] == (
      "No error messages in this range."
  )

  # 2. row_llm empty vs populated KPIs + row_llm and row_tools chart wiring
  df_llm = pd.DataFrame(
      [
          {
              "llm_calls": 1500,
              "prompt_tokens": 1_000_000,
              "completion_tokens": 500_000,
              "total_tokens": 1_500_000,
          }
      ]
  )
  llm_and_tool_charts = {
      "Token usage over time": pd.DataFrame(
          [
              {
                  "bucket": "2026-01-01",
                  "prompt_tokens": 100,
                  "completion_tokens": 50,
                  "total_tokens": 150,
              }
          ]
      ),
      "LLM latency": pd.DataFrame(
          [
              {
                  "bucket": "2026-01-01",
                  "p50_total_ms": 100.0,
                  "p95_total_ms": 250.0,
                  "p50_ttft_ms": 40.0,
              }
          ]
      ),
      "Tokens by model": pd.DataFrame(
          [
              {
                  "model": "gemini-2.5-pro",
                  "prompt_tokens": 100,
                  "completion_tokens": 50,
              }
          ]
      ),
      "Tool invocations": pd.DataFrame(
          [{"tool_name": "search_tool", "invocations": 7}]
      ),
      "Tool latency": pd.DataFrame(
          [{"tool_name": "search_tool", "p95_ms": 80.0, "p50_ms": 30.0}]
      ),
  }
  for df_tot, expected_llm in [
      (
          pd.DataFrame(),
          [
              (0, "LLM calls", "—", None),
              (1, "Total tokens", "—", None),
              (2, "Output tokens", "—", None),
              (3, "Estimated cost", "—", None),
          ],
      ),
      (
          df_llm,
          [
              (
                  0,
                  "LLM calls",
                  "1,500",
                  (
                      "Counted per distinct span, so streaming chunks do not"
                      " inflate it."
                  ),
              ),
              (1, "Total tokens", "1,500,000", None),
              (2, "Output tokens", "500,000", None),
              (
                  3,
                  "Estimated cost",
                  "$3.75",
                  (
                      "Derived from token counts at the sidebar rates — not"
                      " telemetry."
                  ),
              ),
          ],
      ),
  ]:
    metric_calls.clear()
    with (
        mock.patch.object(app.st, "session_state", {}),
        mock.patch.object(app.st, "caption"),
        mock.patch.object(
            app,
            "fetch",
            side_effect=make_fetch("LLM totals", df_tot, llm_and_tool_charts),
        ),
        mock.patch.object(app.st, "columns", side_effect=fake_cols),
        mock.patch.object(app, "panel", side_effect=record_panel),
    ):
      app.row_llm(sample_context)
      assert metric_calls == expected_llm
      if not df_tot.empty:
        app.row_tools(sample_context)

  assert {t.name for t in panel_figs["Token usage over time"].data} == {
      "Input",
      "Output",
  }
  assert [t.name for t in panel_figs["LLM latency (ms)"].data] == [
      "p50",
      "p95",
      "TTFT p50",
  ]
  assert [t.name for t in panel_figs["Tokens by model"].data] == [
      "Input",
      "Output",
  ]
  assert list(panel_figs["Tool invocations"].data[0].y) == ["search_tool"]
  assert [t.name for t in panel_figs["Tool latency (ms)"].data] == [
      "p95",
      "p50",
  ]
  assert panel_kwargs["Tool errors"]["empty"] == (
      "No tool errors in this range."
  )

  # 3. Empty DataFrames in row_overview, row_llm, row_tools, and row_sessions
  panel_figs.clear()
  sess_state: dict[str, object] = {"_selected_session_id": "sess-2"}
  with (
      mock.patch.object(app.st, "session_state", sess_state),
      mock.patch.object(app.st, "caption") as mock_cap,
      mock.patch.object(app.st, "divider"),
      mock.patch.object(app.st, "markdown"),
      mock.patch.object(app.st, "dataframe") as mock_df,
      mock.patch.object(
          app.st,
          "selectbox",
          side_effect=lambda lbl, options, index: options[index],
      ) as mock_sb,
      mock.patch.object(
          app,
          "fetch",
          side_effect=lambda sql, ctx, label: models.QueryResult(
              pd.DataFrame()
          ),
      ),
      mock.patch.object(app.st, "columns", side_effect=fake_cols),
      mock.patch.object(
          app,
          "panel",
          side_effect=lambda title, fig, df, **kw: panel_figs.setdefault(
              title, fig
          ),
      ),
  ):
    app.row_overview(sample_context)
    app.row_llm(sample_context)
    app.row_tools(sample_context)
    app.row_sessions(sample_context)
    assert all(fig is None for fig in panel_figs.values())
    mock_sb.assert_not_called()
    mock_cap.assert_any_call("Pick a session once one is listed above.")

    # Populated sessions with trace selection and empty vs populated trace
    trace_calls: list[models.Context] = []

    def fetch_sessions(sql, ctx, label):
      del sql
      if label == "Recent sessions":
        return models.QueryResult(
            pd.DataFrame({"session_id": ["sess-1", "sess-2"]})
        )
      trace_calls.append(ctx)
      return models.QueryResult(pd.DataFrame([{"event_id": "e1"}]))

    with mock.patch.object(app, "fetch", side_effect=fetch_sessions):
      app.row_sessions(sample_context)
      mock_sb.assert_called_once_with(
          "Session", options=["sess-1", "sess-2"], index=1
      )
      assert trace_calls[-1].filters.session_ids == ("sess-2",)
      mock_df.assert_called_once_with(
          mock.ANY, width="stretch", hide_index=True
      )


def test_footer_byte_and_cache_accounting(sample_context):
  """Verifies footer scan/billing/cache/unrecorded summary math."""
  with (
      mock.patch.object(app.st, "caption") as mock_caption,
      mock.patch.object(app.st, "divider") as mock_divider,
  ):
    # 1. Empty scan_log renders nothing
    app.footer(sample_context)
    mock_caption.assert_not_called()
    mock_divider.assert_not_called()

    # 2. Uncached-only and known-zero guardrail runs omit cache/missing notes
    sample_context.scan_log = [
        models.ScanEntry("q1", 10_485_760, 1024, False, True, True),
    ]
    app.footer(sample_context)
    mock_divider.assert_called_once()
    assert mock_caption.call_count == 3
    text_uncached = mock_caption.call_args_list[0][0][0]
    assert "served from cache" not in text_uncached
    assert "per-query cap 1.0 GB · results cached for 5 min" in text_uncached

    mock_caption.reset_mock()
    sample_context.scan_log = [
        models.ScanEntry("q0", 0, 0, False, True, True),
    ]
    app.footer(sample_context)
    text_zero = mock_caption.call_args_list[0][0][0]
    assert "0 B billed (0 B processed)" in text_zero
    assert "unavailable" not in text_zero

    # 3. Mixed live, cached, and unknown billing/scan entries (2 cached hits)
    mock_caption.reset_mock()
    sample_context.scan_log = [
        models.ScanEntry("q1", 10_485_760, 1024, False, True, True),
        models.ScanEntry("q2", 0, 2048, True, True, True),
        models.ScanEntry("q2b", 0, 1024, True, True, True),
        models.ScanEntry("q3", 0, 512, False, False, True),
        models.ScanEntry("q4", 10_485_760, 0, False, True, False),
    ]
    app.footer(sample_context)
    assert mock_caption.call_count == 3
    text = mock_caption.call_args_list[0][0][0]
    assert (
        f"5 queries this run · {models.humanize_bytes(20_971_520)} billed"
        f" ({models.humanize_bytes(1536)} processed)" in text
    )
    assert "2 served from cache (3.0 KB scan avoided via cache)" in text
    assert "1 with unavailable billing data" in text
    assert "1 with unavailable scan data" in text

    # 4. Cached query with unrecorded scan size uses ≥ and does NOT count as
    #    "with unavailable scan data" (which is for fresh queries only)
    mock_caption.reset_mock()
    sample_context.scan_log = [
        models.ScanEntry("q1", 0, 1024 * 1024, True, True, True),
        models.ScanEntry("q2", 0, 0, True, True, False),
    ]
    app.footer(sample_context)
    assert mock_caption.call_count == 3
    text_unrec = mock_caption.call_args_list[0][0][0]
    assert "0 B billed (0 B processed)" in text_unrec
    assert (
        "2 served from cache (≥ 1.0 MB scan avoided via cache; 1 query scan"
        " size unrecorded)" in text_unrec
    )
    assert "with unavailable scan data" not in text_unrec

    # 5. Plural "queries" when >= 2 cached queries have unrecorded scan sizes
    mock_caption.reset_mock()
    sample_context.scan_log = [
        models.ScanEntry("q1", 0, 0, True, True, False),
        models.ScanEntry("q2", 0, 0, True, True, False),
    ]
    app.footer(sample_context)
    text_plural = mock_caption.call_args_list[0][0][0]
    assert (
        "2 served from cache (≥ 0 B scan avoided via cache; 2 queries scan"
        " size unrecorded)" in text_plural
    )


def test_apptest_sidebar_filters_and_connection_reset():
  """End-to-end AppTest for sidebar filters, option mutations, and reset."""
  empty_res = models.QueryResult(df=pd.DataFrame(), cache_hit=True)
  opts_state: dict[str, object] = {
      "opts": {
          "agent": ["agent-a", "agent-b"],
          "user_id": ["user-1", "user-2"],
          "event_type": ["start", "complete"],
          "session_id": ["sess-1", "sess-2"],
      },
      "res": empty_res,
  }
  sess_res = models.QueryResult(
      df=pd.DataFrame({"session_id": ["sess-init-1"]}), cache_hit=True
  )
  fetched_labels: list[str] = []

  def fake_fetch(sql, ctx, label):
    del sql
    fetched_labels.append(label)
    ctx.scan_log.append(models.ScanEntry(label, 0, 0, True, True, True))
    if label == "Recent sessions" and ctx.refs.dataset == "test_dataset":
      return sess_res
    return empty_res

  with (
      mock.patch.object(
          queries,
          "load_filter_options",
          side_effect=lambda ctx: (opts_state["opts"], opts_state["res"]),
      ),
      mock.patch.object(queries, "fetch", side_effect=fake_fetch),
  ):
    at = _app_test()
    at.run()
    assert not at.exception
    assert {
        "Overview stats",
        "LLM totals",
        "Tool invocations",
        "Recent sessions",
    } <= set(fetched_labels)

    # 1. Verify default time range, pricing, caption buckets, and accept_new_options flags
    assert at.sidebar.number_input(key="flt_price_in").value == 1.25
    assert at.sidebar.number_input(key="flt_price_out").value == 5.0
    assert any("queries this run" in c.value for c in at.caption)
    range_sb = [s for s in at.sidebar.selectbox if s.label == "Range"][0]
    assert range_sb.value == "Last 24 hours"
    assert any("hour buckets" in c.value for c in at.sidebar.caption)
    for key in ("flt_agent", "flt_user_id", "flt_session_id"):
      assert at.sidebar.multiselect(key=key).proto.accept_new_options is True
    assert (
        at.sidebar.multiselect(key="flt_event_type").proto.accept_new_options
        is False
    )

    # 2. Unsubmitted widget edits do not mutate applied_filters until Apply
    at.sidebar.multiselect(key="flt_agent").select("agent-a")
    at.sidebar.multiselect(key="flt_user_id").select("user-1")
    at.sidebar.multiselect(key="flt_event_type").select("start")
    at.sidebar.multiselect(key="flt_session_id").select("sess-1")
    assert at.session_state["applied_filters"] == models.Filters()

    apply_btn = [b for b in at.sidebar.button if b.label == "Apply filters"][0]
    apply_btn.click().run()
    assert not at.exception
    assert at.session_state["applied_filters"] == models.Filters(
        agents=("agent-a",),
        user_ids=("user-1",),
        event_types=("start",),
        session_ids=("sess-1",),
    )

    # 3. Clear agent selection while options are unchanged -> resets to ALL_SENTINEL
    at.sidebar.multiselect(key="flt_agent").unselect("agent-a")
    apply_btn = [b for b in at.sidebar.button if b.label == "Apply filters"][0]
    apply_btn.click().run()
    assert not at.exception
    assert at.session_state["applied_filters"].agents == (models.ALL_SENTINEL,)
    assert at.session_state["applied_filters"].user_ids == ("user-1",)

    # 4. Re-select "agent-a", then mutate BigQuery options to omit "agent-a"
    at.sidebar.multiselect(key="flt_agent").select("agent-a")
    apply_btn = [b for b in at.sidebar.button if b.label == "Apply filters"][0]
    apply_btn.click().run()
    assert at.session_state["applied_filters"].agents == ("agent-a",)

    opts_state["opts"] = {
        "agent": ["agent-c", "agent-d"],
        "user_id": ["user-1", "user-2"],
        "event_type": ["start", "complete"],
        "session_id": ["sess-1", "sess-2"],
    }
    at.run()
    assert not at.exception
    ms_agent = at.sidebar.multiselect(key="flt_agent")
    assert ms_agent.options == ["agent-c", "agent-d", "agent-a"]
    assert ms_agent.value == ["agent-a"]
    assert at.session_state["applied_filters"].agents == ("agent-a",)

    # 5. Transient error OR empty successful refresh reuses cached _filter_options
    for res_override in (
        models.QueryResult(df=pd.DataFrame(), error="Transient BQ failure"),
        empty_res,
    ):
      opts_state["opts"] = {}
      opts_state["res"] = res_override
      at.run()
      assert not at.exception
      assert at.session_state["_filter_options"]["agent"] == [
          "agent-c",
          "agent-d",
      ]
      assert at.session_state["applied_filters"].agents == ("agent-a",)
      assert at.session_state["_selected_session_id"] == "sess-init-1"
      assert at.sidebar.multiselect(key="flt_agent").options == [
          "agent-c",
          "agent-d",
          "agent-a",
      ]

    # 6. Change Dataset ID -> resets filters, cached options, and session selection
    at.session_state["flt_price_in"] = 2.5
    opts_state["opts"] = {}
    opts_state["res"] = empty_res
    [ti for ti in at.sidebar.text_input if ti.label == "Dataset ID"][0].input(
        "test_dataset_2"
    )
    [b for b in at.sidebar.button if b.label == "Connect"][0].click().run()
    assert not at.exception
    assert at.session_state["applied_filters"] == models.Filters()
    for key in ("flt_agent", "flt_user_id", "flt_event_type", "flt_session_id"):
      assert at.session_state[key] == []
      assert at.sidebar.multiselect(key=key).value == []
    assert "_filter_options" not in at.session_state
    assert "_selected_session_id" not in at.session_state
    assert at.session_state["flt_price_in"] == 2.5

    # 7. Apply a custom ID not in options on the freshly reset form
    at.sidebar.multiselect(key="flt_agent").set_value(["custom-agent-xyz"])
    apply_btn = [b for b in at.sidebar.button if b.label == "Apply filters"][0]
    apply_btn.click().run()
    assert not at.exception
    assert at.session_state["applied_filters"].agents == ("custom-agent-xyz",)

    # 8. Invalid dataset in sidebar stops main() with st.info prompt;
    #    reconnecting to test_dataset_2 re-seeds widget from applied_filters
    [ti for ti in at.sidebar.text_input if ti.label == "Dataset ID"][0].input(
        "bad-dataset!"
    )
    [b for b in at.sidebar.button if b.label == "Connect"][0].click().run()
    assert not at.exception
    assert any("Invalid dataset ID" in e.value for e in at.sidebar.error)
    assert any("Set `BQ_PROJECT_ID`" in i.value for i in at.info)

    [ti for ti in at.sidebar.text_input if ti.label == "Dataset ID"][0].input(
        "test_dataset_2"
    )
    [b for b in at.sidebar.button if b.label == "Connect"][0].click().run()
    assert not at.exception
    assert at.session_state["applied_filters"].agents == ("custom-agent-xyz",)
    assert at.sidebar.multiselect(key="flt_agent").value == ["custom-agent-xyz"]


def test_apptest_lazy_tabs_and_session_trace_preservation(
    monkeypatch: pytest.MonkeyPatch,
):
  """End-to-end AppTest for lazy tabs and session selectbox preservation."""
  monkeypatch.setenv("STREAMLIT_LAZY_TABS", "true")
  empty_res = models.QueryResult(df=pd.DataFrame(), cache_hit=True)
  sess_res1 = models.QueryResult(
      df=pd.DataFrame({"session_id": ["sess-1", "sess-2", "sess-3"]}),
      cache_hit=True,
  )
  sess_res2 = models.QueryResult(
      df=pd.DataFrame({"session_id": ["sess-newest", "sess-2", "sess-older"]}),
      cache_hit=True,
  )
  sess_res3 = models.QueryResult(
      df=pd.DataFrame({"session_id": ["sess-brand-new", "sess-older"]}),
      cache_hit=True,
  )
  trace_df = pd.DataFrame(
      [
          {
              "event_id": "evt-1",
              "event_type": "TOOL_STARTING",
              "timestamp": "2026-01-01 00:00:00+00:00",
          },
          {
              "event_id": "evt-2",
              "event_type": "TOOL_COMPLETED",
              "timestamp": "2026-01-01 00:01:00+00:00",
          },
      ]
  )
  trace_res = models.QueryResult(df=trace_df, cache_hit=True)
  current_sess = sess_res1
  fetched_labels: list[str] = []
  trace_filters: list[models.Filters] = []

  def fake_fetch(sql, ctx, label):
    del sql
    fetched_labels.append(label)
    if label == "Recent sessions":
      return current_sess
    if label == "Trace detail":
      trace_filters.append(ctx.filters)
      if ctx.filters.session_ids == ("sess-brand-new",):
        return empty_res
      return trace_res
    return empty_res

  with (
      mock.patch.object(
          queries, "load_filter_options", return_value=({}, empty_res)
      ),
      mock.patch.object(queries, "fetch", side_effect=fake_fetch),
  ):
    at = _app_test()
    at.run()
    assert not at.exception

    # 1. Lazy tabs defaults to Overview; other tabs do not run until selected
    controls = [c for c in at.segmented_control if c.label == "Dashboard"]
    assert len(controls) == 1 and controls[0].value == "Overview"
    assert "Overview stats" in fetched_labels
    assert not any(
        lbl in fetched_labels
        for lbl in ("LLM totals", "Tool invocations", "Recent sessions")
    )

    # 2. Switch across LLM & FinOps and Tools & Execution tabs
    fetched_labels.clear()
    controls[0].set_value("LLM & FinOps").run()
    assert (
        "LLM totals" in fetched_labels
        and "Tool invocations" not in fetched_labels
    )

    fetched_labels.clear()
    controls = [c for c in at.segmented_control if c.label == "Dashboard"]
    controls[0].set_value("Tools & Execution").run()
    assert (
        "Tool invocations" in fetched_labels
        and "LLM totals" not in fetched_labels
    )

    # 3. Switch to Sessions & Traces -> selectbox and populated trace table
    at.session_state["applied_filters"] = models.Filters(agents=("agent-x",))
    controls = [c for c in at.segmented_control if c.label == "Dashboard"]
    controls[0].set_value("Sessions & Traces").run()
    assert not at.exception
    sess_box = [s for s in at.selectbox if s.label == "Session"][0]
    assert sess_box.value == "sess-1"
    assert trace_filters[-1] == models.Filters(
        agents=("agent-x",), session_ids=("sess-1",)
    )
    assert len(at.dataframe) == 2
    assert list(at.dataframe[1].value["event_id"]) == ["evt-1", "evt-2"]
    assert any(
        "read the session chronologically" in c.value for c in at.caption
    )
    assert not any("No events for this session" in c.value for c in at.caption)

    # 4. Select sess-2, then refresh session list where sess-2 shifts to index 1
    sess_box.select("sess-2").run()
    assert at.session_state["_selected_session_id"] == "sess-2"
    assert trace_filters[-1].session_ids == ("sess-2",)

    current_sess = sess_res2
    at.run()
    assert not at.exception
    sess_box_refreshed = [s for s in at.selectbox if s.label == "Session"][0]
    assert sess_box_refreshed.value == "sess-2"
    assert at.session_state["_selected_session_id"] == "sess-2"
    assert len(at.dataframe) == 2

    # 5. If selected session ages out of results, falls back to index 0
    current_sess = sess_res3
    at.run()
    assert not at.exception
    sess_box_fallback = [s for s in at.selectbox if s.label == "Session"][0]
    assert sess_box_fallback.value == "sess-brand-new"
    assert at.session_state["_selected_session_id"] == "sess-brand-new"
    assert len(at.dataframe) == 1
    assert any("No events for this session" in c.value for c in at.caption)

    # 6. If segmented_control returns None, lazy tabs falls back to Overview
    fetched_labels.clear()
    with mock.patch.object(st, "segmented_control", return_value=None):
      at.run()
    assert not at.exception
    assert "Overview stats" in fetched_labels
