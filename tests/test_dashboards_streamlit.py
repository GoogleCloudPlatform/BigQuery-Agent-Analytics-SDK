"""Consolidated unit, chart, and AppTest suite for the Streamlit dashboard."""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path
import sys
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
  queries._NEXT_RUN_ID = 0
  yield
  queries._run_query_cached.clear()
  queries._SEEN_RUN_IDS.clear()
  queries._NEXT_RUN_ID = 0


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
  assert models.Window(start, start + dt.timedelta(days=3)).bucket == "HOUR"
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

  assert models.snap(moment, seconds=0) == moment.replace(microsecond=0)
  assert models.snap(moment, seconds=-10) == moment.replace(microsecond=0)

  naive = dt.datetime(2026, 9, 1, 12, 34, 56, 789000)
  snapped_naive = models.snap(naive, seconds=300)
  assert snapped_naive == expected_snapped
  assert snapped_naive.tzinfo == dt.timezone.utc

  offset_tz = dt.timezone(dt.timedelta(hours=2))
  aware_offset = dt.datetime(2026, 9, 1, 14, 34, 56, 789000, tzinfo=offset_tz)
  snapped_offset = models.snap(aware_offset, seconds=300)
  assert snapped_offset == expected_snapped
  assert snapped_offset.tzinfo == dt.timezone.utc

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
  assert res.stats_known is True
  assert (
      models.QueryResult(pd.DataFrame(), bytes_billed_known=False).stats_known
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
      "('___ALL___' IN UNNEST(@event_types) OR e.event_type IN"
      " UNNEST(@event_types))"
      in scope_aliased
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
  ev_time = queries.build_events_over_time_sql(sample_refs, sample_window)
  assert "@event_types" in ev_time and "GROUP BY bucket, event_type" in ev_time

  err_time = queries.build_errors_over_time_sql(sample_refs, sample_window)
  assert "@event_types" not in err_time
  assert "ENDS_WITH(event_type, '_ERROR')" in err_time

  by_agent = queries.build_events_by_agent_sql(sample_refs, sample_window)
  assert "IFNULL(agent, 'unknown') AS agent_name" in by_agent
  assert "@event_types" in by_agent

  top_err = queries.build_top_errors_sql(sample_refs, sample_window)
  assert "AND error_message IS NOT NULL" in top_err
  assert f"LIMIT {models.TOP_ERRORS_LIMIT}" in top_err

  tok_time = queries.build_llm_tokens_over_time_sql(sample_refs, sample_window)
  assert f"FROM {sample_refs.view('llm_responses')}" in tok_time

  lat_pct = queries.build_llm_latency_percentiles_sql(
      sample_refs, sample_window
  )
  assert "OFFSET(95)] AS p95_total_ms" in lat_pct

  tok_mod = queries.build_tokens_by_model_sql(sample_refs, sample_window)
  assert "IFNULL(model_version, 'unknown') AS model" in tok_mod

  t_usage = queries.build_tool_usage_sql(sample_refs, sample_window)
  assert f"FROM {sample_refs.view('tool_starts')}" in t_usage

  t_lat = queries.build_tool_latency_sql(sample_refs, sample_window)
  assert f"FROM {sample_refs.view('tool_completions')}" in t_lat


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
  assert queries.query_parameters("SELECT 1", filters) == []

  live_cfg = queries.job_config("SELECT 1", filters, 5_000_000, dry_run=False)
  assert live_cfg.maximum_bytes_billed == 5_000_000
  assert live_cfg.dry_run is False

  dry_cfg = queries.job_config("SELECT 1", filters, 5_000_000, dry_run=True)
  assert dry_cfg.maximum_bytes_billed is None
  assert dry_cfg.dry_run is True


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
    assert res_auth.stats_known is True

  # 2. Dry-run estimate > max_bytes trips guardrail with known 0 bytes
  mock_client = mock.MagicMock()
  mock_probe = mock.MagicMock(total_bytes_processed=5000)
  mock_client.query.return_value = mock_probe
  with mock.patch.object(queries, "get_client", return_value=mock_client):
    res_guard = queries.run_query("SELECT * FROM big", filters, "proj", 1000)
    assert res_guard.df.empty and "Guardrail:" in (res_guard.error or "")
    assert (res_guard.bytes_processed, res_guard.bytes_billed) == (0, 0)
    assert res_guard.stats_known is True

    # 3. Dry-run API failure -> known 0 bytes
    mock_client.query.side_effect = gexc.GoogleAPICallError("Dry run denied")
    res_api = queries.run_query("SELECT * FROM big", filters, "proj", 1000)
    assert res_api.df.empty and "Dry run denied" in (res_api.error or "")
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

  with mock.patch.object(queries, "get_client", return_value=mock_client):
    f1 = models.Filters(agents=("agent-1",))

    # First run: live execution (dry-run probe + live job = 2 query calls)
    r1 = queries.run_query("SELECT 1", f1, "proj-1", 1_000_000)
    assert mock_client.query.call_count == 2
    assert (r1.bytes_processed, r1.bytes_billed, r1.cache_hit) == (
        100,
        200,
        False,
    )

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

  # Unknown billed bytes on first run -> stats_known=False; cache hit -> True
  queries._SEEN_RUN_IDS.clear()
  with mock.patch.object(
      queries,
      "_run_query_cached",
      return_value=(pd.DataFrame(), 100, 0, False, True, False, 99),
  ):
    r_unk = queries.run_query("SELECT unk", f1, "proj", 1000)
    assert r_unk.bytes_billed_known is False and r_unk.stats_known is False
    r_dedup = queries.run_query("SELECT unk", f1, "proj", 1000)
    assert r_dedup.cache_hit is True and r_dedup.bytes_billed == 0
    assert r_dedup.bytes_billed_known is True and r_dedup.stats_known is True

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

  # 3. If total_bytes_billed stays None after reload fails, billed_known=False
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

  # 4. Degraded job (neither stat known, ended=None, reload fails) -> None
  job_dead = mock.MagicMock(
      total_bytes_processed=None,
      total_bytes_billed=None,
      ended=None,
  )
  job_dead.reload.side_effect = RuntimeError("gone")
  assert queries._extract_job_stats(job_dead) is None
  plain_exc = RuntimeError("download boom")
  assert queries._query_failure(job_dead, plain_exc) is plain_exc

  # 5. Result download failure retains job stats when known, or marks unknown
  mock_client = mock.MagicMock()
  mock_probe = mock.MagicMock(total_bytes_processed=1000)
  mock_failed_dl = mock.MagicMock(
      total_bytes_processed=20480,
      total_bytes_billed=10_485_760,
      cache_hit=False,
      ended=dt.datetime.now(dt.timezone.utc),
  )
  mock_failed_dl.to_dataframe.side_effect = gexc.ServiceUnavailable("503")
  job_dead.to_dataframe.side_effect = gexc.ServiceUnavailable("503 dead")
  mock_client.query.side_effect = [
      mock_probe,
      mock_failed_dl,
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
    assert (res.bytes_processed, res.bytes_billed, res.stats_known) == (
        20480,
        10_485_760,
        True,
    )

    res_dead = queries.run_query(
        "SELECT * FROM dead", models.Filters(), "p", 10**8
    )
    assert res_dead.df.empty and res_dead.error is not None
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
      mock.patch.object(queries, "run_query", return_value=ok_res),
      mock.patch.object(queries.st, "spinner"),
  ):
    options, res = queries.load_filter_options(sample_context)
    assert options == {
        "agent": ["agent-a", "agent-b"],
        "event_type": ["start"],
        "session_id": ["sess-1"],
        "user_id": ["user-1"],
    }
    assert res is ok_res
    assert sample_context.scan_log == [
        models.ScanEntry(
            label="Filter options",
            bytes_billed=100,
            bytes_processed=50,
            cache_hit=False,
            bytes_billed_known=False,
            bytes_processed_known=True,
        )
    ]

  # Error path returns empty dict and preserves error result
  err_res = models.QueryResult(df=pd.DataFrame(), error="Access Denied")
  with mock.patch.object(queries, "fetch", return_value=err_res):
    options, res = queries.load_filter_options(sample_context)
    assert options == {} and res.error == "Access Denied"


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
    cm_over = charts.color_map(
        "overflow", [f"n_{i}" for i in range(12)], theme
    )
    assert len(cm_over) == 13
    assert cm_over["n_8"] == theme.categorical[8 % len(theme.categorical)]


def test_chart_builders_data_shaping(sample_context):
  """Verifies data shaping in stacked/ranked/grouped bars and lines."""
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
    assert models.OTHER_LABEL in {t.name for t in fig_stack.data}
    assert len({t.marker.color for t in fig_stack.data}) == 8

    # 2. ranked_bars keeps top N and sorts ascending for horizontal rendering
    df_rank = pd.DataFrame(
        {"agent": [f"agent_{i}" for i in range(15)], "count": range(0, 150, 10)}
    )
    fig_rank = charts.ranked_bars(
        df_rank, "agent", "count", sample_context, top=5
    )
    assert list(fig_rank.data[0].x) == [100, 110, 120, 130, 140]
    assert list(fig_rank.data[0].y) == [f"agent_{i}" for i in range(10, 15)]

    # 3. grouped_bars creates one horizontal trace per series with distinct hues
    df_grp = pd.DataFrame(
        {"model": ["m1", "m2"], "prompt": [100, 300], "comp": [50, 150]}
    )
    fig_grp = charts.grouped_bars(
        df_grp,
        "model",
        [("prompt", "Prompt"), ("comp", "Completion")],
        sample_context,
        "tok",
    )
    assert [t.name for t in fig_grp.data] == ["Prompt", "Completion"]
    assert fig_grp.data[0].marker.color != fig_grp.data[1].marker.color
    assert fig_grp.layout.barmode == "group"

    # 4. lines() adds direct end annotations for <= 4 series, omits for > 4
    df_lines = pd.DataFrame(
        {
            "bucket": ["2026-01-01", "2026-01-02"],
            "s1": [10, 20],
            "s2": [15, 25],
            "s3": [20, 30],
            "s4": [25, 35],
            "s5": [30, 40],
        }
    )
    fig_two = charts.lines(
        df_lines,
        "bucket",
        [("s1", "S1"), ("s2", "S2")],
        sample_context,
        "dom",
    )
    assert [a.text for a in fig_two.layout.annotations] == ["S1", "S2"]

    fig_five = charts.lines(
        df_lines,
        "bucket",
        [(f"s{i}", f"S{i}") for i in range(1, 6)],
        sample_context,
        "dom",
    )
    assert len(fig_five.layout.annotations) == 0


def test_panel_branches():
  """Verifies panel() empty caption, chart + expander, and table-only view."""
  with (
      mock.patch.object(charts.st, "markdown"),
      mock.patch.object(charts.st, "caption") as mock_caption,
      mock.patch.object(charts.st, "plotly_chart") as mock_plotly,
      mock.patch.object(charts.st, "expander") as mock_expander,
      mock.patch.object(charts.st, "dataframe") as mock_df,
  ):
    charts.panel("Empty", None, pd.DataFrame(), empty="Nothing.")
    mock_caption.assert_called_once_with("Nothing.")
    mock_plotly.assert_not_called()
    mock_expander.assert_not_called()
    mock_df.assert_not_called()

    mock_caption.reset_mock()
    df = pd.DataFrame([{"a": 1}])
    charts.panel("With Chart", go.Figure(), df, key="k")
    mock_plotly.assert_called_once()
    mock_expander.assert_called_once_with("Table view", expanded=False)

    mock_plotly.reset_mock()
    mock_expander.reset_mock()
    charts.panel("Table Only", None, df)
    mock_plotly.assert_not_called()
    mock_expander.assert_called_once_with("Table view", expanded=True)


# ------------------------------------------------------------------ #
# 5. app.py: Business Logic & End-to-End AppTest Flows               #
# ------------------------------------------------------------------ #


def test_app_helpers_and_sidebar_connection(monkeypatch):
  """Verifies _seed_options, _default_for, and env vs form project handling."""
  all_sentinel = (models.ALL_SENTINEL,)
  state = {"flt_agent": ["agent-pending"]}
  with mock.patch.object(app.st, "session_state", state):
    assert app._seed_options("flt_agent", ["agent-1"], ("agent-applied",)) == [
        "agent-1",
        "agent-applied",
        "agent-pending",
    ]
  assert app._default_for(all_sentinel) == []
  assert app._default_for(("agent-x",)) == ["agent-x"]

  # 1. When BQ_PROJECT_ID is set in env, user input cannot override it
  monkeypatch.setenv("BQ_PROJECT_ID", "env-locked-project")
  monkeypatch.setenv("BQ_DATASET_ID", "env_dataset")
  with (
      mock.patch.object(
          app.st,
          "text_input",
          side_effect=lambda label, value="", **kw: (
              "injected-proj" if label == "Project ID" else value
          ),
      ),
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

  # 3. Blank dataset before Connect click does not render sidebar error
  monkeypatch.delenv("BQ_DATASET_ID", raising=False)
  with (
      mock.patch.object(
          app.st, "text_input", side_effect=lambda label, value="", **kw: value
      ),
      mock.patch.object(app.st, "selectbox", return_value="1 GB"),
      mock.patch.object(app.st, "form_submit_button", return_value=False),
      mock.patch.object(app.st, "session_state", {}),
      mock.patch.object(app.st.sidebar, "error") as mock_error,
  ):
    refs, _ = app.sidebar_connection()
    assert refs is None
    mock_error.assert_not_called()


def test_row_overview_and_llm_metrics(sample_context):
  """Verifies KPI formatting in row_overview and token cost math in row_llm."""
  metric_calls: list[tuple[str, str]] = []
  fake_cols = lambda n: [
      mock.MagicMock() for _ in range(n if isinstance(n, int) else len(n))
  ]

  # 1. row_overview normal vs None/NaN values
  for row_data, expected in [
      (
          {
              "sessions": 12,
              "events": 340,
              "error_rate": 0.0731,
              "avg_llm_latency_ms": 1234.5,
          },
          [
              ("Sessions", "12"),
              ("Events", "340"),
              ("Error rate", "7.31%"),
              ("Avg LLM latency", "1,235 ms"),
          ],
      ),
      (
          {
              "sessions": 12,
              "events": 340,
              "error_rate": float("nan"),
              "avg_llm_latency_ms": None,
          },
          [
              ("Sessions", "12"),
              ("Events", "340"),
              ("Error rate", "—"),
              ("Avg LLM latency", "—"),
          ],
      ),
  ]:
    metric_calls.clear()
    df_ov = pd.DataFrame([row_data])
    with (
        mock.patch.object(
            app,
            "fetch",
            side_effect=lambda sql, ctx, label, df=df_ov: models.QueryResult(
                df if label == "Overview stats" else pd.DataFrame()
            ),
        ),
        mock.patch.object(app.st, "columns", side_effect=fake_cols),
        mock.patch.object(
            app,
            "_metric",
            side_effect=lambda col, label, val, *a, **kw: metric_calls.append(
                (label, val)
            ),
        ),
        mock.patch.object(app, "panel"),
    ):
      app.row_overview(sample_context)
      assert metric_calls == expected

  # 2. row_llm estimated cost: (1M / 1e6 * $1.25) + (0.5M / 1e6 * $5.00) = $3.75
  metric_calls.clear()
  df_llm = pd.DataFrame(
      [
          {
              "llm_calls": 10,
              "prompt_tokens": 1_000_000,
              "completion_tokens": 500_000,
              "total_tokens": 1_500_000,
          }
      ]
  )
  with (
      mock.patch.object(
          app,
          "fetch",
          side_effect=lambda sql, ctx, label: models.QueryResult(
              df_llm if label == "LLM totals" else pd.DataFrame()
          ),
      ),
      mock.patch.object(app.st, "columns", side_effect=fake_cols),
      mock.patch.object(
          app,
          "_metric",
          side_effect=lambda col, label, val, *a, **kw: metric_calls.append(
              (label, val)
          ),
      ),
      mock.patch.object(app, "panel"),
  ):
    app.row_llm(sample_context)
    assert ("Estimated cost", "$3.75") in metric_calls


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

    # 2. Uncached-only run omits cache clause and includes cap + TTL
    sample_context.scan_log = [
        models.ScanEntry("q1", 10_485_760, 1024, False, True, True),
    ]
    app.footer(sample_context)
    assert mock_caption.call_count == 3
    text_uncached = mock_caption.call_args_list[0][0][0]
    assert "served from cache" not in text_uncached
    assert "per-query cap 1.0 GB · results cached for 5 min" in text_uncached

    # 3. Mixed live, cached, and unknown billing/scan entries
    mock_caption.reset_mock()
    sample_context.scan_log = [
        models.ScanEntry("q1", 10_485_760, 1024, False, True, True),
        models.ScanEntry("q2", 0, 2048, True, True, True),
        models.ScanEntry("q3", 0, 512, False, False, True),
        models.ScanEntry("q4", 10_485_760, 0, False, True, False),
    ]
    app.footer(sample_context)
    assert mock_caption.call_count == 3
    text = mock_caption.call_args_list[0][0][0]
    assert "4 queries this run" in text
    assert (
        f"{models.humanize_bytes(20_971_520)} billed"
        f" ({models.humanize_bytes(1536)} processed)" in text
    )
    assert "1 served from cache (2.0 KB scan avoided via cache)" in text
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
    assert (
        "2 served from cache (≥ 1.0 MB scan avoided via cache; 1 query scan"
        " size unrecorded)" in text_unrec
    )
    assert "with unavailable scan data" not in text_unrec


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

  def fake_fetch(sql, ctx, label):
    del sql
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

    # 1. Verify accept_new_options flags on multiselect widgets
    for key in ("flt_agent", "flt_user_id", "flt_session_id"):
      assert at.sidebar.multiselect(key=key).proto.accept_new_options is True
    assert (
        at.sidebar.multiselect(key="flt_event_type").proto.accept_new_options
        is False
    )

    # 2. Unsubmitted widget edits do not mutate applied_filters until Apply
    at.sidebar.multiselect(key="flt_agent").select("agent-a")
    at.sidebar.multiselect(key="flt_user_id").select("user-1")
    assert at.session_state["applied_filters"] == models.Filters()

    apply_btn = [b for b in at.sidebar.button if b.label == "Apply filters"][0]
    apply_btn.click().run()
    assert not at.exception
    assert at.session_state["applied_filters"].agents == ("agent-a",)
    assert at.session_state["applied_filters"].user_ids == ("user-1",)

    # 3. Mutate BigQuery options to omit "agent-a" -> re-seeded and preserved
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

    # 4. Transient load_filter_options error reuses cached _filter_options
    opts_state["opts"] = {}
    opts_state["res"] = models.QueryResult(
        df=pd.DataFrame(), error="Transient BQ failure"
    )
    at.run()
    assert not at.exception
    assert at.session_state["_filter_options"]["agent"] == [
        "agent-c",
        "agent-d",
    ]
    opts_state["opts"] = {
        "agent": ["agent-c", "agent-d"],
        "user_id": ["user-1", "user-2"],
        "event_type": ["start", "complete"],
        "session_id": ["sess-1", "sess-2"],
    }
    opts_state["res"] = empty_res

    # 5. Apply a custom ID, then clear selection back to ALL_SENTINEL
    at.sidebar.multiselect(key="flt_agent").set_value(["custom-agent-xyz"])
    apply_btn = [b for b in at.sidebar.button if b.label == "Apply filters"][0]
    apply_btn.click().run()
    assert at.session_state["applied_filters"].agents == ("custom-agent-xyz",)

    at.sidebar.multiselect(key="flt_agent").unselect("custom-agent-xyz")
    apply_btn = [b for b in at.sidebar.button if b.label == "Apply filters"][0]
    apply_btn.click().run()
    assert at.session_state["applied_filters"].agents == (models.ALL_SENTINEL,)

    # 6. Re-apply a filter, then change Dataset ID -> resets all filters
    at.sidebar.multiselect(key="flt_agent").set_value(["agent-c"])
    apply_btn = [b for b in at.sidebar.button if b.label == "Apply filters"][0]
    apply_btn.click().run()
    assert at.session_state["applied_filters"].agents == ("agent-c",)
    assert at.session_state["_selected_session_id"] == "sess-init-1"

    [ti for ti in at.sidebar.text_input if ti.label == "Dataset ID"][0].input(
        "test_dataset_2"
    )
    [b for b in at.sidebar.button if b.label == "Connect"][0].click().run()
    assert not at.exception
    assert at.session_state["applied_filters"] == models.Filters()
    assert at.sidebar.multiselect(key="flt_agent").value == []
    assert "_selected_session_id" not in at.session_state


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
  current_sess = sess_res1
  fetched_labels: list[str] = []

  def fake_fetch(sql, ctx, label):
    del sql, ctx
    fetched_labels.append(label)
    if label == "Recent sessions":
      return current_sess
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
        for lbl in ("LLM totals", "Tool usage", "Recent sessions")
    )

    # 2. Switch across LLM & FinOps and Tools & Execution tabs
    fetched_labels.clear()
    controls[0].set_value("LLM & FinOps").run()
    assert "LLM totals" in fetched_labels and "Tool usage" not in fetched_labels

    fetched_labels.clear()
    controls = [c for c in at.segmented_control if c.label == "Dashboard"]
    controls[0].set_value("Tools & Execution").run()
    assert "Tool usage" in fetched_labels and "LLM totals" not in fetched_labels

    # 3. Switch to Sessions & Traces tab -> Session selectbox renders
    controls = [c for c in at.segmented_control if c.label == "Dashboard"]
    controls[0].set_value("Sessions & Traces").run()
    assert not at.exception
    sess_box = [s for s in at.selectbox if s.label == "Session"][0]
    assert sess_box.value == "sess-1"

    # 4. Select sess-2, then refresh session list where sess-2 shifts to index 1
    sess_box.select("sess-2").run()
    assert at.session_state["_selected_session_id"] == "sess-2"

    current_sess = sess_res2
    at.run()
    assert not at.exception
    sess_box_refreshed = [s for s in at.selectbox if s.label == "Session"][0]
    assert sess_box_refreshed.value == "sess-2"
    assert at.session_state["_selected_session_id"] == "sess-2"

    # 5. If selected session ages out of results, falls back to index 0
    current_sess = sess_res3
    at.run()
    assert not at.exception
    sess_box_fallback = [s for s in at.selectbox if s.label == "Session"][0]
    assert sess_box_fallback.value == "sess-brand-new"
    assert at.session_state["_selected_session_id"] == "sess-brand-new"
