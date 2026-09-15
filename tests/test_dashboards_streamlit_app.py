"""Unit tests for the Streamlit dashboard."""

from __future__ import annotations

import contextlib
import datetime as dt
import importlib.util
import os
from pathlib import Path
import sys
from unittest import mock

import pytest

ROOT = Path(__file__).resolve().parents[1]
APP_PATH = ROOT / "dashboards" / "streamlit" / "app.py"
CHARTS_PATH = ROOT / "dashboards" / "streamlit" / "charts.py"
MODELS_PATH = ROOT / "dashboards" / "streamlit" / "models.py"
QUERIES_PATH = ROOT / "dashboards" / "streamlit" / "queries.py"

# Modules that dashboard components import. Modules in _UNCONDITIONAL_MOCKS
# are always stubbed with MagicMocks to isolate tests from third-party UI
# and cloud libraries; any remaining modules are mocked only if missing.
_OPTIONAL_MODULES = (
    "streamlit",
    "pandas",
    "plotly",
    "plotly.graph_objects",
    "google",
    "google.api_core",
    "google.api_core.exceptions",
    "google.auth",
    "google.auth.exceptions",
    "google.cloud",
    "google.cloud.bigquery",
)

_UNCONDITIONAL_MOCKS = (
    "dotenv",
    "streamlit",
    "plotly",
    "plotly.graph_objects",
    "google.cloud.bigquery",
)

# `from X import Y` reads Y off the parent package, and a MagicMock parent
# auto-creates an attribute unrelated to the sys.modules entry configured
# below. Point these at the real entries so app.py binds what we set up.
_PARENT_ATTRS = (
    ("google.cloud", "bigquery"),
    ("google.api_core", "exceptions"),
    ("google.auth", "exceptions"),
    ("plotly", "graph_objects"),
)


class _MockArrayQueryParameter:
  """Stand-in that keeps what ``query_parameters`` passes positionally.

  Attribute names mirror ``bigquery.ArrayQueryParameter`` so assertions
  hold whether the real client library is installed or not.
  """

  def __init__(self, name, array_type, values):
    self.name = name
    self.array_type = array_type
    self.values = values


class _MockQueryJobConfig:
  """Stand-in for ``bigquery.QueryJobConfig``."""

  def __init__(self, **kwargs):
    self.maximum_bytes_billed = None
    for k, v in kwargs.items():
      setattr(self, k, v)


class _MockDataFrame:
  """Mock mimicking ``pandas.DataFrame`` that exposes ``.empty``."""

  def __init__(self, data=None, *args, **kwargs):
    self.data = data
    self.empty = not bool(data)


_MISSING = object()


@contextlib.contextmanager
def _mocked_optional_imports():
  """Installs mocks for absent optional deps, then restores ``sys.modules``.

  The mocks must not outlive the load. They are process-global, so a test
  module imported later in the same session would otherwise resolve
  ``google.cloud`` or ``pandas`` to a MagicMock and run against it
  silently instead of failing — or skipping — honestly.
  """
  saved_modules: dict[str, object] = {}
  saved_parent_attrs: list[tuple[object, str, object, bool]] = []
  try:
    for name in _UNCONDITIONAL_MOCKS:
      saved_modules[name] = sys.modules.get(name, _MISSING)
      sys.modules[name] = mock.MagicMock()

    for name in _OPTIONAL_MODULES:
      if name in _UNCONDITIONAL_MOCKS:
        continue
      if name not in sys.modules:
        try:
          __import__(name)
        except ImportError:
          saved_modules[name] = _MISSING
          sys.modules[name] = mock.MagicMock()

    for parent, attr in _PARENT_ATTRS:
      child_key = f"{parent}.{attr}"
      if parent in sys.modules and child_key in sys.modules:
        parent_mod = sys.modules[parent]
        child_mod = sys.modules[child_key]
        if isinstance(parent_mod, mock.MagicMock):
          had_attr = attr in parent_mod.__dict__
          orig_attr = parent_mod.__dict__.get(attr)
        else:
          had_attr = hasattr(parent_mod, attr)
          orig_attr = getattr(parent_mod, attr, None)
        saved_parent_attrs.append((parent_mod, attr, orig_attr, had_attr))
        setattr(parent_mod, attr, child_mod)

    # Only ever patch a mock, never a real module: mutating an installed
    # package would be the same leak in a different disguise.
    bq_mod = sys.modules["google.cloud.bigquery"]
    bq_mod.ArrayQueryParameter = _MockArrayQueryParameter
    bq_mod.QueryJobConfig = _MockQueryJobConfig

    pd_mod = sys.modules.get("pandas")
    if isinstance(pd_mod, mock.MagicMock):
      pd_mod.DataFrame = _MockDataFrame

    gexc_mod = sys.modules.get("google.api_core.exceptions")
    if isinstance(gexc_mod, mock.MagicMock):
      for exc_name in (
          "GoogleAPICallError",
          "NotFound",
          "Forbidden",
          "RetryError",
          "ServiceUnavailable",
      ):
        if not isinstance(getattr(gexc_mod, exc_name, None), type):
          setattr(gexc_mod, exc_name, type(exc_name, (Exception,), {}))

    gauth_mod = sys.modules.get("google.auth.exceptions")
    if isinstance(gauth_mod, mock.MagicMock):
      if not isinstance(
          getattr(gauth_mod, "DefaultCredentialsError", None), type
      ):
        setattr(
            gauth_mod,
            "DefaultCredentialsError",
            type("DefaultCredentialsError", (Exception,), {}),
        )

    st_mod = sys.modules["streamlit"]

    def _passthrough(*args, **kwargs):
      def _decorator(func):
        return func

      return _decorator

    st_mod.cache_data = _passthrough
    st_mod.cache_resource = _passthrough

    yield
  finally:
    for parent_mod, attr, orig_attr, had_attr in reversed(saved_parent_attrs):
      if had_attr:
        setattr(parent_mod, attr, orig_attr)
      else:
        try:
          delattr(parent_mod, attr)
        except AttributeError:
          pass

    for name, orig_val in reversed(list(saved_modules.items())):
      if orig_val is _MISSING:
        sys.modules.pop(name, None)
      else:
        sys.modules[name] = orig_val


def _load_module(name: str, path: Path):
  """Loads a Python module dynamically with mocked optional deps."""
  if name in sys.modules:
    return sys.modules[name]
  spec = importlib.util.spec_from_file_location(name, path)
  assert spec is not None and spec.loader is not None
  mod = importlib.util.module_from_spec(spec)
  sys.modules[name] = mod
  dir_path = str(path.parent)
  added_path = False
  if dir_path not in sys.path:
    sys.path.insert(0, dir_path)
    added_path = True
  saved_submods = {}
  try:
    for submod in ("charts", "models", "queries"):
      saved_submods[submod] = sys.modules.get(submod, _MISSING)
      stored = sys.modules.get(f"dashboards_streamlit_{submod}")
      if stored is not None:
        sys.modules[submod] = stored
    with _mocked_optional_imports():
      spec.loader.exec_module(mod)
  except Exception:
    sys.modules.pop(name, None)
    raise
  finally:
    if added_path and dir_path in sys.path:
      sys.path.remove(dir_path)
    for submod, orig_val in saved_submods.items():
      if orig_val is _MISSING:
        sys.modules.pop(submod, None)
      else:
        sys.modules[submod] = orig_val
  return mod


def _load_streamlit_models():
  """Loads dashboards/streamlit/models.py with mocked optional deps."""
  return _load_module("dashboards_streamlit_models", MODELS_PATH)


def _load_streamlit_charts():
  """Loads dashboards/streamlit/charts.py with mocked optional deps."""
  return _load_module("dashboards_streamlit_charts", CHARTS_PATH)


def _load_streamlit_queries():
  """Loads dashboards/streamlit/queries.py with mocked optional deps."""
  return _load_module("dashboards_streamlit_queries", QUERIES_PATH)


def _load_streamlit_app():
  """Loads dashboards/streamlit/app.py dynamically with mocked optional deps."""
  with mock.patch.dict(os.environ, {"BQAA_DASHBOARD_SKIP_DOTENV": "1"}):
    return _load_module("dashboards_streamlit_app", APP_PATH)


models = _load_streamlit_models()
charts = _load_streamlit_charts()
queries = _load_streamlit_queries()
app = _load_streamlit_app()


@pytest.fixture(autouse=True)
def _isolate_test_env(monkeypatch: pytest.MonkeyPatch):
  monkeypatch.setenv("BQAA_DASHBOARD_SKIP_DOTENV", "1")


@pytest.fixture(autouse=True)
def _reset_queries_state():
  try:
    yield
  finally:
    queries._SEEN_RUN_IDS.clear()
    queries._NEXT_RUN_ID = 0


@pytest.fixture(autouse=True)
def _pin_eager_tabs(monkeypatch: pytest.MonkeyPatch):
  monkeypatch.setenv("STREAMLIT_LAZY_TABS", "false")
  orig = app._LAZY_TABS
  app._LAZY_TABS = False
  try:
    yield
  finally:
    app._LAZY_TABS = orig


@pytest.fixture
def sample_refs():
  return models.TableRefs(
      project="test-project",
      dataset="test_dataset",
      table="agent_events",
      view_prefix="adk_",
  )


@pytest.fixture
def sample_window():
  return models.Window(
      start=dt.datetime(2026, 9, 1, 0, 0, 0, tzinfo=dt.timezone.utc),
      end=dt.datetime(2026, 9, 2, 0, 0, 0, tzinfo=dt.timezone.utc),
  )


def test_table_refs_formatting_and_defaults():
  refs = models.TableRefs(
      project="my-proj",
      dataset="my_ds",
      table="my_table",
      view_prefix="custom_",
  )
  assert refs.events == "`my-proj.my_ds.my_table`"
  assert refs.view("llm_responses") == "`my-proj.my_ds.custom_llm_responses`"
  assert refs.view("tool_errors") == "`my-proj.my_ds.custom_tool_errors`"

  default_refs = models.TableRefs(
      project="my-proj",
      dataset="my_ds",
      table="my_table",
  )
  assert default_refs.view_prefix == models.DEFAULT_VIEW_PREFIX
  assert (
      default_refs.view("llm_responses") == "`my-proj.my_ds.adk_llm_responses`"
  )


def test_validate_refs_valid():
  refs, errors = models.validate_refs(
      "valid-project-123", "valid_dataset", "valid_table_id", "adk_"
  )
  assert errors == []
  assert refs is not None
  assert refs.project == "valid-project-123"
  assert refs.dataset == "valid_dataset"
  assert refs.table == "valid_table_id"
  assert refs.view_prefix == "adk_"
  assert refs.events == "`valid-project-123.valid_dataset.valid_table_id`"

  # Empty view prefix is valid (e.g. view named simply 'llm_responses')
  refs_empty_prefix, errors = models.validate_refs(
      "valid-project", "valid_dataset", "events", ""
  )
  assert errors == []
  assert refs_empty_prefix is not None
  assert refs_empty_prefix.view_prefix == ""
  assert refs_empty_prefix.view("llm") == "`valid-project.valid_dataset.llm`"


def test_validate_refs_invalid_identifiers():
  # Invalid project
  refs, errors = models.validate_refs(
      "-invalid-start", "dataset", "table", "prefix_"
  )
  assert refs is None
  assert any("Invalid project ID" in e for e in errors)

  refs, errors = models.validate_refs(
      "proj with space", "dataset", "table", "prefix_"
  )
  assert refs is None
  assert any("Invalid project ID" in e for e in errors)

  refs, errors = models.validate_refs(
      "proj`inject", "dataset", "table", "prefix_"
  )
  assert refs is None
  assert any("Invalid project ID" in e for e in errors)

  # Invalid dataset
  refs, errors = models.validate_refs(
      "proj", "dataset-with-dash", "table", "prefix_"
  )
  assert refs is None
  assert any("Invalid dataset ID" in e for e in errors)

  refs, errors = models.validate_refs(
      "proj", "ds with space", "table", "prefix_"
  )
  assert refs is None
  assert any("Invalid dataset ID" in e for e in errors)

  # Invalid table
  refs, errors = models.validate_refs(
      "proj", "dataset", "table-with-dash", "prefix_"
  )
  assert refs is None
  assert any("Invalid table ID" in e for e in errors)

  refs, errors = models.validate_refs(
      "proj", "dataset", "tbl`inject", "prefix_"
  )
  assert refs is None
  assert any("Invalid table ID" in e for e in errors)

  refs, errors = models.validate_refs(
      "proj", "dataset`inject", "table", "prefix_"
  )
  assert refs is None
  assert any("Invalid dataset ID" in e for e in errors)

  refs, errors = models.validate_refs(
      "proj", "dataset", "table", "invalid-prefix"
  )
  assert refs is None
  assert any("Invalid view prefix" in e for e in errors)

  refs, errors = models.validate_refs(
      "proj", "dataset", "table", "prefix`inject"
  )
  assert refs is None
  assert any("Invalid view prefix" in e for e in errors)

  # Multiple errors combined
  refs, errors = models.validate_refs("", "", "", "bad-prefix")
  assert refs is None
  assert len(errors) == 4


def test_validate_refs_trailing_newlines_and_sanitization():
  """Assert regexes reject trailing newlines and validate_refs sanitizes or rejects them."""
  # Direct regex matching rejects trailing newlines due to \Z anchoring
  assert not models._PROJECT_RE.match("proj\n")
  assert not models._NAME_RE.match("ds\n")
  assert not models._NAME_RE.match("tbl\n")
  assert not models._PREFIX_RE.match("adk_\n")

  # validate_refs strips inputs at boundary, sanitizing them into valid TableRefs
  refs, errors = models.validate_refs("proj\n", "ds\n", "tbl\n", "adk_\n")
  assert errors == []
  assert refs is not None
  assert refs.project == "proj"
  assert refs.dataset == "ds"
  assert refs.table == "tbl"
  assert refs.view_prefix == "adk_"
  assert "\n" not in refs.events
  assert "\n" not in refs.view("llm")

  # Internal unstrippable newlines cannot be sanitized by strip and must be rejected
  refs_bad, errors_bad = models.validate_refs(
      "proj\nextra", "ds\nextra", "tbl\nextra", "adk_\nextra"
  )
  assert refs_bad is None
  assert len(errors_bad) == 4


def test_time_bounds_and_window_intervals():
  start = dt.datetime(2026, 9, 1, 12, 0, 0, tzinfo=dt.timezone.utc)
  end = dt.datetime(2026, 9, 1, 14, 30, 0, tzinfo=dt.timezone.utc)
  w = models.Window(start=start, end=end)

  # time_bounds default column
  tb = queries.time_bounds(w)
  expected_tb = (
      'timestamp >= TIMESTAMP "2026-09-01 12:00:00+00:00"\n'
      '  AND timestamp < TIMESTAMP "2026-09-01 14:30:00+00:00"'
  )
  assert tb == expected_tb

  # time_bounds custom column
  tb_custom = queries.time_bounds(w, column="e.timestamp")
  expected_custom = (
      'e.timestamp >= TIMESTAMP "2026-09-01 12:00:00+00:00"\n'
      '  AND e.timestamp < TIMESTAMP "2026-09-01 14:30:00+00:00"'
  )
  assert tb_custom == expected_custom

  # Span property
  assert w.span == dt.timedelta(hours=2, minutes=30)

  # Bucket granularity logic:
  # <= 6 hours -> MINUTE
  w_minute = models.Window(start=start, end=start + dt.timedelta(hours=6))
  assert w_minute.bucket == "MINUTE"

  # <= 3 days -> HOUR
  w_hour = models.Window(start=start, end=start + dt.timedelta(days=3))
  assert w_hour.bucket == "HOUR"

  # > 3 days -> DAY
  w_day = models.Window(start=start, end=start + dt.timedelta(days=7))
  assert w_day.bucket == "DAY"


def test_snap_and_make_window():
  moment = dt.datetime(2026, 9, 1, 12, 34, 56, 789000, tzinfo=dt.timezone.utc)
  snapped = models.snap(moment, seconds=300)
  assert snapped == dt.datetime(2026, 9, 1, 12, 30, 0, tzinfo=dt.timezone.utc)
  assert models.snap(moment) == dt.datetime(
      2026, 9, 1, 12, 30, 0, tzinfo=dt.timezone.utc
  )

  window = models.make_window(dt.timedelta(hours=1), now=moment)
  assert window.end == dt.datetime(
      2026, 9, 1, 12, 30, 0, tzinfo=dt.timezone.utc
  )
  assert window.start == dt.datetime(
      2026, 9, 1, 11, 30, 0, tzinfo=dt.timezone.utc
  )

  # Non-positive seconds does not raise ZeroDivisionError
  assert models.snap(moment, seconds=0) == moment.replace(microsecond=0)
  assert models.snap(moment, seconds=-10) == moment.replace(microsecond=0)

  # Naive input is normalized to UTC and returns aware UTC
  naive_moment = dt.datetime(2026, 9, 1, 12, 34, 56, 789000)
  naive_snapped = models.snap(naive_moment, seconds=300)
  assert naive_snapped == dt.datetime(
      2026, 9, 1, 12, 30, 0, tzinfo=dt.timezone.utc
  )
  assert naive_snapped.tzinfo == dt.timezone.utc
  assert models.snap(naive_moment, seconds=0) == dt.datetime(
      2026, 9, 1, 12, 34, 56, tzinfo=dt.timezone.utc
  )

  # Aware input with timezone offset is converted to UTC
  # UTC+2 at 14:34:56 is 12:34:56 UTC
  offset_tz = dt.timezone(dt.timedelta(hours=2))
  aware_offset = dt.datetime(2026, 9, 1, 14, 34, 56, 789000, tzinfo=offset_tz)
  aware_snapped = models.snap(aware_offset, seconds=300)
  assert aware_snapped == dt.datetime(
      2026, 9, 1, 12, 30, 0, tzinfo=dt.timezone.utc
  )
  assert aware_snapped.tzinfo == dt.timezone.utc
  assert models.snap(aware_offset, seconds=0) == dt.datetime(
      2026, 9, 1, 12, 34, 56, tzinfo=dt.timezone.utc
  )

  # Snapping with interval > 3600 (e.g. 2 hours) aligns across hour boundaries
  m1 = dt.datetime(2026, 9, 1, 12, 59, 0, tzinfo=dt.timezone.utc)
  m2 = dt.datetime(2026, 9, 1, 13, 1, 0, tzinfo=dt.timezone.utc)
  assert models.snap(m1, seconds=7200) == models.snap(m2, seconds=7200)
  assert models.snap(m1, seconds=7200) == dt.datetime(
      2026, 9, 1, 12, 0, 0, tzinfo=dt.timezone.utc
  )


def test_build_overview_totals_sql(sample_refs, sample_window):
  sql = queries.build_overview_totals_sql(sample_refs, sample_window)

  assert "COUNT(DISTINCT e.session_id) AS sessions" in sql
  assert "COUNT(*) AS events" in sql
  assert "SAFE_DIVIDE(COUNTIF(" in sql
  assert "ENDS_WITH(e.event_type, '_ERROR')" in sql
  assert "e.error_message IS NOT NULL" in sql
  assert "UPPER(e.status) = 'ERROR'" in sql
  assert "SELECT AVG(r.total_ms)" in sql
  assert f"FROM {sample_refs.view('llm_responses')} AS r" in sql
  assert f"FROM {sample_refs.events} AS e" in sql
  assert queries.time_bounds(sample_window, "e.timestamp") in sql
  assert queries.time_bounds(sample_window, "r.timestamp") in sql
  assert "HAVING COUNT(*) > 0" in sql

  # Overview scoping: agents, user_ids, session_ids (event_type is omitted by
  # design)
  assert "@agents" in sql
  assert "@user_ids" in sql
  assert "@session_ids" in sql
  assert "@event_types" not in sql


def test_build_trace_detail_sql(sample_refs, sample_window):
  limit = 500
  sql = queries.build_trace_detail_sql(sample_refs, sample_window, limit=limit)

  assert f"FROM {sample_refs.events}" in sql
  assert queries.time_bounds(sample_window) in sql
  assert "ORDER BY timestamp DESC" in sql
  assert f"LIMIT {limit}" in sql
  assert "JSON_VALUE(attributes, '$.model')" in sql
  assert "JSON_VALUE(attributes, '$.model_version')" in sql
  assert "JSON_VALUE(content, '$.tool') AS tool_name" in sql
  assert (
      "SAFE_CAST(JSON_VALUE(latency_ms, '$.total_ms') AS INT64) AS total_ms"
      in sql
  )
  assert "error_message" in sql
  assert "COALESCE(" in sql

  # Scoping includes event_types
  assert "@event_types" in sql
  assert "@agents" in sql
  assert "@user_ids" in sql
  assert "@session_ids" in sql


def test_build_llm_calls_total_sql(sample_refs, sample_window):
  sql = queries.build_llm_calls_total_sql(sample_refs, sample_window)

  assert f"FROM {sample_refs.view('llm_responses')}" in sql
  assert (
      "COUNT(DISTINCT CONCAT(trace_id, '|', span_id))\n"
      "    + COUNTIF(trace_id IS NULL OR span_id IS NULL) AS llm_calls" in sql
  )
  assert "IFNULL(SUM(usage_prompt_tokens), 0) AS prompt_tokens" in sql
  assert "IFNULL(SUM(usage_completion_tokens), 0) AS completion_tokens" in sql
  assert "IFNULL(SUM(usage_total_tokens), 0) AS total_tokens" in sql
  assert "@price_in" not in sql
  assert "@price_out" not in sql
  assert "estimated_cost_usd" not in sql
  assert queries.time_bounds(sample_window) in sql
  assert "HAVING COUNT(*) > 0" in sql


def test_build_recent_sessions_sql(sample_refs, sample_window):
  limit = 250
  sql = queries.build_recent_sessions_sql(
      sample_refs, sample_window, limit=limit
  )

  assert f"FROM {sample_refs.events}" in sql
  assert queries.time_bounds(sample_window) in sql
  assert "session_id IS NOT NULL" in sql
  assert (
      "STRING_AGG(DISTINCT user_id, ', ' ORDER BY user_id)\n"
      "    AS session_users_in_window" in sql
  )
  assert "MIN(timestamp) AS started_in_window_at" in sql
  assert "MAX(timestamp) AS last_event_in_window_at" in sql
  assert (
      "TIMESTAMP_DIFF(MAX(timestamp), MIN(timestamp), SECOND)\n"
      "    AS duration_in_window_s" in sql
  )
  assert "COUNT(DISTINCT agent) AS session_agents_in_window" in sql
  assert "COUNT(*) AS session_events_in_window" in sql
  assert (
      "COUNTIF(ENDS_WITH(event_type, '_ERROR') OR error_message IS NOT NULL OR"
      " UPPER(status) = 'ERROR') AS session_errors_in_window" in sql
  )
  assert (
      "IFNULL(SUM(IF(event_type = 'LLM_RESPONSE',\n"
      "    SAFE_CAST(JSON_VALUE(content, '$.usage.prompt') AS INT64), NULL)),"
      " 0)\n"
      "    AS session_input_tokens_in_window" in sql
  )
  assert (
      "IFNULL(SUM(IF(event_type = 'LLM_RESPONSE',\n"
      "    SAFE_CAST(JSON_VALUE(content, '$.usage.completion') AS INT64),"
      " NULL)), 0)\n"
      "    AS session_output_tokens_in_window" in sql
  )
  assert "GROUP BY session_id" in sql
  assert (
      "HAVING LOGICAL_OR('___ALL___' IN UNNEST(@agents)\n"
      "    OR agent IN UNNEST(@agents))" in sql
  )
  assert (
      "AND LOGICAL_OR('___ALL___' IN UNNEST(@user_ids)\n"
      "    OR user_id IN UNNEST(@user_ids))" in sql
  )
  assert (
      "AND LOGICAL_OR('___ALL___' IN UNNEST(@event_types)\n"
      "    OR event_type IN UNNEST(@event_types))" in sql
  )
  assert "ORDER BY last_event_in_window_at DESC" in sql
  assert f"LIMIT {limit}" in sql


def test_build_tool_errors_sql(sample_refs, sample_window):
  limit = 100
  sql = queries.build_tool_errors_sql(sample_refs, sample_window, limit=limit)

  assert f"FROM {sample_refs.view('tool_errors')}" in sql
  assert "UNION ALL" in sql
  assert f"FROM {sample_refs.view('tool_completions')}" in sql
  assert "AND (error_message IS NOT NULL OR UPPER(status) = 'ERROR')" in sql
  assert "ORDER BY timestamp DESC" in sql
  assert f"LIMIT {limit}" in sql


def test_build_filter_options_sql(sample_refs, sample_window):
  sql = queries.build_filter_options_sql(sample_refs, sample_window)

  assert f"FROM {sample_refs.events} AS e," in sql
  assert "UNNEST([" in sql
  assert "STRUCT('agent' AS kind, e.agent AS value)" in sql
  assert "STRUCT('user_id', e.user_id)" in sql
  assert "STRUCT('event_type', e.event_type)" in sql
  assert "STRUCT('session_id', e.session_id)" in sql
  assert "WHERE " + queries.time_bounds(sample_window, "e.timestamp") in sql
  assert "AND f.value IS NOT NULL" in sql
  assert "GROUP BY f.kind, f.value" in sql
  assert "QUALIFY ROW_NUMBER() OVER (" in sql
  assert f"<= {models.FILTER_OPTIONS_LIMIT}" in sql
  assert "ORDER BY f.kind, f.value" in sql


def test_build_events_over_time_sql(sample_refs, sample_window):
  sql = queries.build_events_over_time_sql(sample_refs, sample_window)

  assert f"TIMESTAMP_TRUNC(timestamp, {sample_window.bucket}) AS bucket" in sql
  assert "event_type," in sql
  assert "COUNT(*) AS events" in sql
  assert f"FROM {sample_refs.events}" in sql
  assert queries.time_bounds(sample_window) in sql
  assert "GROUP BY bucket, event_type" in sql
  assert "ORDER BY bucket" in sql
  assert "@event_types" in sql


def test_build_errors_over_time_sql(sample_refs, sample_window):
  sql = queries.build_errors_over_time_sql(sample_refs, sample_window)

  assert f"TIMESTAMP_TRUNC(timestamp, {sample_window.bucket}) AS bucket" in sql
  assert "event_type," in sql
  assert "COUNT(*) AS errors" in sql
  assert f"FROM {sample_refs.events}" in sql
  assert queries.time_bounds(sample_window) in sql
  assert "ENDS_WITH(event_type, '_ERROR')" in sql
  assert "error_message IS NOT NULL" in sql
  assert "UPPER(status) = 'ERROR'" in sql
  assert "GROUP BY bucket, event_type" in sql
  assert "ORDER BY bucket" in sql
  # Event types filter should NOT be in errors_over_time
  assert "@event_types" not in sql


def test_build_events_by_agent_sql(sample_refs, sample_window):
  sql = queries.build_events_by_agent_sql(sample_refs, sample_window)

  assert "IFNULL(agent, 'unknown') AS agent_name" in sql
  assert "COUNT(*) AS events" in sql
  assert f"FROM {sample_refs.events}" in sql
  assert queries.time_bounds(sample_window) in sql
  assert "GROUP BY agent_name" in sql
  assert "HAVING COUNT(*) > 0" in sql
  assert "ORDER BY events DESC" in sql
  assert "@event_types" in sql


def test_build_top_errors_sql(sample_refs, sample_window):
  limit = 50
  sql = queries.build_top_errors_sql(sample_refs, sample_window, limit=limit)

  assert "error_message," in sql
  assert "COUNT(*) AS errors," in sql
  assert "COUNT(DISTINCT session_id) AS sessions," in sql
  assert "COUNT(DISTINCT agent) AS agents," in sql
  assert "MAX(timestamp) AS last_seen" in sql
  assert f"FROM {sample_refs.events}" in sql
  assert queries.time_bounds(sample_window) in sql
  assert "AND error_message IS NOT NULL" in sql
  assert "GROUP BY error_message" in sql
  assert "HAVING COUNT(*) > 0" in sql
  assert "ORDER BY errors DESC" in sql
  assert f"LIMIT {limit}" in sql

  # Default limit is TOP_ERRORS_LIMIT (50)
  sql_default = queries.build_top_errors_sql(sample_refs, sample_window)
  assert f"LIMIT {models.TOP_ERRORS_LIMIT}" in sql_default
  assert "LIMIT 50" in sql_default


def test_build_llm_tokens_over_time_sql(sample_refs, sample_window):
  sql = queries.build_llm_tokens_over_time_sql(sample_refs, sample_window)

  assert f"TIMESTAMP_TRUNC(timestamp, {sample_window.bucket}) AS bucket" in sql
  assert "IFNULL(SUM(usage_prompt_tokens), 0) AS prompt_tokens" in sql
  assert "IFNULL(SUM(usage_completion_tokens), 0) AS completion_tokens" in sql
  assert "IFNULL(SUM(usage_total_tokens), 0) AS total_tokens" in sql
  assert f"FROM {sample_refs.view('llm_responses')}" in sql
  assert queries.time_bounds(sample_window) in sql
  assert "GROUP BY bucket" in sql
  assert "ORDER BY bucket" in sql


def test_build_llm_latency_percentiles_sql(sample_refs, sample_window):
  sql = queries.build_llm_latency_percentiles_sql(sample_refs, sample_window)

  assert f"TIMESTAMP_TRUNC(timestamp, {sample_window.bucket}) AS bucket" in sql
  assert "APPROX_QUANTILES(total_ms, 100)[OFFSET(50)] AS p50_total_ms" in sql
  assert "APPROX_QUANTILES(total_ms, 100)[OFFSET(95)] AS p95_total_ms" in sql
  assert "APPROX_QUANTILES(ttft_ms, 100)[OFFSET(50)] AS p50_ttft_ms" in sql
  assert f"FROM {sample_refs.view('llm_responses')}" in sql
  assert queries.time_bounds(sample_window) in sql
  assert "GROUP BY bucket" in sql
  assert "ORDER BY bucket" in sql


def test_build_tokens_by_model_sql(sample_refs, sample_window):
  sql = queries.build_tokens_by_model_sql(sample_refs, sample_window)

  assert "IFNULL(model_version, 'unknown') AS model" in sql
  assert "IFNULL(SUM(usage_prompt_tokens), 0) AS prompt_tokens" in sql
  assert "IFNULL(SUM(usage_completion_tokens), 0) AS completion_tokens" in sql
  assert "IFNULL(SUM(usage_total_tokens), 0) AS total_tokens" in sql
  assert "COUNT(*) AS responses" in sql
  assert f"FROM {sample_refs.view('llm_responses')}" in sql
  assert queries.time_bounds(sample_window) in sql
  assert "GROUP BY model" in sql
  assert "ORDER BY total_tokens DESC" in sql


def test_build_tool_usage_sql(sample_refs, sample_window):
  sql = queries.build_tool_usage_sql(sample_refs, sample_window)

  assert "IFNULL(tool_name, 'unknown') AS tool_name" in sql
  assert "COUNT(*) AS invocations" in sql
  assert f"FROM {sample_refs.view('tool_starts')}" in sql
  assert queries.time_bounds(sample_window) in sql
  assert "GROUP BY tool_name" in sql
  assert "ORDER BY invocations DESC" in sql


def test_build_tool_latency_sql(sample_refs, sample_window):
  sql = queries.build_tool_latency_sql(sample_refs, sample_window)

  assert "IFNULL(tool_name, 'unknown') AS tool_name" in sql
  assert "COUNT(*) AS completions" in sql
  assert "AVG(total_ms) AS avg_ms" in sql
  assert "APPROX_QUANTILES(total_ms, 100)[OFFSET(50)] AS p50_ms" in sql
  assert "APPROX_QUANTILES(total_ms, 100)[OFFSET(95)] AS p95_ms" in sql
  assert f"FROM {sample_refs.view('tool_completions')}" in sql
  assert queries.time_bounds(sample_window) in sql
  assert "GROUP BY tool_name" in sql
  assert "ORDER BY p95_ms DESC" in sql


def test_filters_and_as_filter_values():
  f_default = models.Filters()
  assert f_default.agents == (models.ALL_SENTINEL,)
  assert f_default.user_ids == (models.ALL_SENTINEL,)
  assert f_default.event_types == (models.ALL_SENTINEL,)
  assert f_default.session_ids == (models.ALL_SENTINEL,)

  assert models.as_filter_values([]) == (models.ALL_SENTINEL,)
  assert models.as_filter_values(["a", "b"]) == ("a", "b")


def test_query_parameters():
  filters = models.Filters(
      agents=("agent_1",),
      user_ids=("user_1",),
      event_types=("LLM_RESPONSE",),
      session_ids=("sess_1",),
  )

  # Query that uses @agents and @session_ids only
  sql = (
      "SELECT * FROM t WHERE agent IN UNNEST(@agents) AND session_id IN"
      " UNNEST(@session_ids)"
  )
  params = queries.query_parameters(sql, filters)
  by_name = {p.name: p for p in params}
  assert sorted(by_name) == ["agents", "session_ids"]

  # Each bound parameter carries its filter's values as ARRAY<STRING>.
  assert by_name["agents"].array_type == "STRING"
  assert list(by_name["agents"].values) == ["agent_1"]
  assert list(by_name["session_ids"].values) == ["sess_1"]

  # Query that uses all 4
  sql_all = "SELECT 1 WHERE @agents @user_ids @event_types @session_ids"
  params_all = queries.query_parameters(sql_all, filters)
  assert len(params_all) == 4

  # A query with no filter references binds nothing (build_filter_options_sql
  # is the real case), so BigQuery never receives an unreferenced parameter.
  assert queries.query_parameters("SELECT 1", filters) == []


def test_humanize_bytes():
  assert models.humanize_bytes(500) == "500 B"
  assert models.humanize_bytes(1024) == "1.0 KB"
  assert models.humanize_bytes(10 * 1024**2) == "10.0 MB"
  assert models.humanize_bytes(2.5 * 1024**3) == "2.5 GB"
  assert models.humanize_bytes(3 * 1024**4) == "3.0 TB"


def test_scope_helper():
  # Without alias and without event_type
  scope_bare = queries._scope()
  assert (
      "('___ALL___' IN UNNEST(@agents) OR agent IN UNNEST(@agents))"
      in scope_bare
  )
  assert (
      "('___ALL___' IN UNNEST(@user_ids) OR user_id IN UNNEST(@user_ids))"
      in scope_bare
  )
  assert (
      "('___ALL___' IN UNNEST(@session_ids) OR session_id IN"
      " UNNEST(@session_ids))" in scope_bare
  )
  assert "@event_types" not in scope_bare

  # With alias and with event_type
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


def test_themes():
  assert models.LIGHT_THEME.surface == "#fcfcfb"
  assert models.DARK_THEME.surface == "#1a1a19"
  assert len(models.LIGHT_THEME.categorical) == 8
  assert len(models.DARK_THEME.categorical) == 8
  assert charts.active_theme() in (models.LIGHT_THEME, models.DARK_THEME)


def test_context_and_query_result(sample_refs, sample_window):
  filters = models.Filters()
  ctx = models.Context(
      refs=sample_refs,
      window=sample_window,
      filters=filters,
      max_bytes=1024**3,
      theme=models.LIGHT_THEME,
      price_in=1.25,
      price_out=5.00,
  )
  assert ctx.refs == sample_refs
  assert ctx.window == sample_window
  assert ctx.filters == filters
  assert ctx.max_bytes == 1024**3
  assert ctx.theme == models.LIGHT_THEME
  assert (ctx.price_in, ctx.price_out) == (1.25, 5.00)
  assert ctx.scan_log == []

  # Two Contexts must not share one scan log; a mutable default would make
  # every panel's cost accumulate across reruns.
  other = models.Context(
      refs=sample_refs,
      window=sample_window,
      filters=filters,
      max_bytes=1024**3,
      theme=models.LIGHT_THEME,
      price_in=1.25,
      price_out=5.00,
  )
  ctx.scan_log.append(
      models.ScanEntry(
          label="overview",
          bytes_billed=1024,
          bytes_processed=1024,
          cache_hit=False,
          bytes_billed_known=True,
          bytes_processed_known=True,
      )
  )
  assert other.scan_log == []

  # A QueryResult defaults to "succeeded, nothing scanned, not cached".
  sentinel = object()
  result = models.QueryResult(sentinel)
  assert result.df is sentinel
  assert result.error is None
  assert result.bytes_processed == 0
  assert result.bytes_billed == 0
  assert result.cache_hit is False
  assert result.bytes_processed_known is True
  assert result.bytes_billed_known is True
  assert result.stats_known is True


def test_sidebar_connection_locks_env_project(monkeypatch):
  monkeypatch.setenv("BQ_PROJECT_ID", "env-locked-project")
  monkeypatch.setenv("BQ_DATASET_ID", "env_dataset")
  monkeypatch.setenv("BQ_TABLE_ID", "agent_events")
  monkeypatch.setenv("BQ_VIEW_PREFIX", "adk_")

  def fake_text_input(label, value="", **kwargs):
    if label == "Project ID":
      return "user-injected-project"
    return value

  with (
      mock.patch.object(app.st, "text_input", side_effect=fake_text_input),
      mock.patch.object(app.st, "selectbox", return_value="1 GB"),
  ):
    refs, cap = app.sidebar_connection()
    assert refs is not None
    assert refs.project == "env-locked-project"
    assert refs.dataset == "env_dataset"
    assert refs.table == "agent_events"
    assert cap == models.BYTES_CAPS["1 GB"]


def test_sidebar_connection_fallback_when_env_unset(monkeypatch):
  monkeypatch.delenv("BQ_PROJECT_ID", raising=False)
  monkeypatch.setenv("BQ_DATASET_ID", "env_dataset")
  monkeypatch.setenv("BQ_TABLE_ID", "agent_events")
  monkeypatch.setenv("BQ_VIEW_PREFIX", "adk_")

  def fake_text_input(label, value="", **kwargs):
    if label == "Project ID":
      return "form-project"
    return value

  with (
      mock.patch.object(app.st, "text_input", side_effect=fake_text_input),
      mock.patch.object(app.st, "selectbox", return_value="1 GB"),
  ):
    refs, cap = app.sidebar_connection()
    assert refs is not None
    assert refs.project == "form-project"
    assert refs.dataset == "env_dataset"
    assert cap == models.BYTES_CAPS["1 GB"]


def test_sidebar_connection_no_error_when_dataset_blank_without_connect(
    monkeypatch,
):
  """BQ_PROJECT_ID set in env, BQ_DATASET_ID blank, no Connect click -> no st.sidebar.error rendered."""
  monkeypatch.setenv("BQ_PROJECT_ID", "test-project")
  monkeypatch.delenv("BQ_DATASET_ID", raising=False)
  monkeypatch.setenv("BQ_TABLE_ID", "agent_events")

  def fake_text_input(label, value="", **kwargs):
    return value

  state = {}
  with (
      mock.patch.object(app.st, "text_input", side_effect=fake_text_input),
      mock.patch.object(app.st, "selectbox", return_value="1 GB"),
      mock.patch.object(app.st, "form_submit_button", return_value=False),
      mock.patch.object(app.st, "session_state", state),
      mock.patch.object(app.st.sidebar, "error") as mock_error,
  ):
    refs, cap = app.sidebar_connection()
    assert refs is None
    mock_error.assert_not_called()


def test_row_llm_cost_calculation(sample_refs, sample_window):
  ctx = models.Context(
      refs=sample_refs,
      window=sample_window,
      filters=models.Filters(),
      max_bytes=1024**3,
      theme=models.LIGHT_THEME,
      price_in=1.25,
      price_out=5.00,
  )
  mock_df = mock.MagicMock()
  mock_df.empty = False
  mock_row = {
      "llm_calls": 10,
      "prompt_tokens": 1_000_000,
      "completion_tokens": 500_000,
      "total_tokens": 1_500_000,
  }
  mock_df.iloc.__getitem__.return_value = mock_row

  def fake_columns(n):
    return [
        mock.MagicMock() for _ in range(n if isinstance(n, int) else len(n))
    ]

  with (
      mock.patch.object(app, "fetch") as mock_fetch,
      mock.patch.object(app.st, "columns", side_effect=fake_columns),
      mock.patch.object(app, "_metric") as mock_metric,
  ):
    mock_fetch.return_value = models.QueryResult(mock_df)
    app.row_llm(ctx)
    # Check that _metric was called for Estimated cost with $3.75
    # (1_000_000 / 1e6 * 1.25) + (500_000 / 1e6 * 5.00) = 1.25 + 2.50 = 3.75
    calls = mock_metric.call_args_list
    cost_call = next(c for c in calls if c.args[1] == "Estimated cost")
    assert cost_call.args[2] == "$3.75"


def test_row_overview_data_path(sample_refs, sample_window):
  """Verify row_overview formatting for numeric, None, and NaN metric values."""
  pd = pytest.importorskip("pandas")
  ctx = models.Context(
      refs=sample_refs,
      window=sample_window,
      filters=models.Filters(),
      max_bytes=1000,
      theme=models.LIGHT_THEME,
      price_in=0.0,
      price_out=0.0,
  )

  def fake_columns(n):
    return [
        mock.MagicMock() for _ in range(n if isinstance(n, int) else len(n))
    ]

  # 1. Normal values: sessions=12, events=340, error_rate=0.0731, avg_llm_latency_ms=1234.5
  df_valid = pd.DataFrame(
      [
          {
              "sessions": 12,
              "events": 340,
              "error_rate": 0.0731,
              "avg_llm_latency_ms": 1234.5,
          }
      ]
  )
  metric_calls = []

  def fake_metric(col, label, val, *args, **kwargs):
    metric_calls.append((label, val))

  def fake_fetch(sql, ctx, label):
    if label == "Overview stats":
      return models.QueryResult(df=current_df, error=None)
    return models.QueryResult(df=pd.DataFrame(), error=None)

  current_df = df_valid
  with (
      mock.patch.object(app, "fetch", side_effect=fake_fetch),
      mock.patch.object(app.st, "columns", side_effect=fake_columns),
      mock.patch.object(app, "_metric", side_effect=fake_metric),
      mock.patch.object(app, "panel"),
      mock.patch.object(app, "stacked_bars"),
      mock.patch.object(app, "ranked_bars"),
  ):
    app.row_overview(ctx)

    assert metric_calls == [
        ("Sessions", "12"),
        ("Events", "340"),
        ("Error rate", "7.31%"),
        ("Avg LLM latency", "1,235 ms"),
    ]

  # 2. None values: error_rate=None, avg_llm_latency_ms=None -> "—"
  df_none = pd.DataFrame(
      [
          {
              "sessions": 12,
              "events": 340,
              "error_rate": None,
              "avg_llm_latency_ms": None,
          }
      ]
  )
  current_df = df_none
  metric_calls.clear()
  with (
      mock.patch.object(app, "fetch", side_effect=fake_fetch),
      mock.patch.object(app.st, "columns", side_effect=fake_columns),
      mock.patch.object(app, "_metric", side_effect=fake_metric),
      mock.patch.object(app, "panel"),
      mock.patch.object(app, "stacked_bars"),
      mock.patch.object(app, "ranked_bars"),
  ):
    app.row_overview(ctx)

    assert metric_calls == [
        ("Sessions", "12"),
        ("Events", "340"),
        ("Error rate", "—"),
        ("Avg LLM latency", "—"),
    ]

  # 3. NaN values: error_rate=NaN, avg_llm_latency_ms=NaN -> "—"
  df_nan = pd.DataFrame(
      [
          {
              "sessions": 12,
              "events": 340,
              "error_rate": float("nan"),
              "avg_llm_latency_ms": float("nan"),
          }
      ]
  )
  current_df = df_nan
  metric_calls.clear()
  with (
      mock.patch.object(app, "fetch", side_effect=fake_fetch),
      mock.patch.object(app.st, "columns", side_effect=fake_columns),
      mock.patch.object(app, "_metric", side_effect=fake_metric),
      mock.patch.object(app, "panel"),
      mock.patch.object(app, "stacked_bars"),
      mock.patch.object(app, "ranked_bars"),
  ):
    app.row_overview(ctx)

    assert metric_calls == [
        ("Sessions", "12"),
        ("Events", "340"),
        ("Error rate", "—"),
        ("Avg LLM latency", "—"),
    ]


def test_explain_actionable_advice():
  # DefaultCredentialsError
  cred_err = queries.gauth_exc.DefaultCredentialsError(
      "Could not automatically determine credentials."
  )
  explained_creds = queries._explain(cred_err)
  assert "Could not automatically determine credentials." in explained_creds
  assert (
      "Run `gcloud auth application-default login` or set"
      " `GOOGLE_APPLICATION_CREDENTIALS`." in explained_creds
  )

  # NotFound
  not_found_err = queries.gexc.NotFound("Dataset not found")
  explained_nf = queries._explain(not_found_err)
  assert "Dataset not found" in explained_nf
  assert "bq-agent-sdk views create-all" in explained_nf

  # Forbidden
  forbidden_err = queries.gexc.Forbidden("Access denied")
  explained_forb = queries._explain(forbidden_err)
  assert "Access denied" in explained_forb
  assert "roles/bigquery.jobUser" in explained_forb

  # bytesBilledLimitExceeded
  limit_err = Exception("Query aborted: bytesBilledLimitExceeded error")
  explained_limit = queries._explain(limit_err)
  assert "bytesBilledLimitExceeded" in explained_limit
  assert "Raise the per-query scan cap in the sidebar" in explained_limit

  # Generic fallback
  generic_err = ValueError("Something unexpected")
  assert queries._explain(generic_err) == "Something unexpected"


def test_run_query_default_credentials_error():
  with mock.patch.object(
      queries,
      "get_client",
      side_effect=queries.gauth_exc.DefaultCredentialsError("No auth creds"),
  ):
    filters = models.Filters()
    result = queries.run_query("SELECT 1", filters, "test-proj", 1024**3)
    assert result.df.empty is True
    assert result.error is not None
    assert "No auth creds" in result.error
    assert "gcloud auth application-default login" in result.error
    assert result.bytes_processed == 0
    assert result.bytes_billed == 0
    assert result.bytes_processed_known is True
    assert result.bytes_billed_known is True
    assert result.cache_hit is False


def test_run_query_cache_hit_deduplication():
  mock_df = mock.MagicMock()
  mock_df.empty = False
  mock_df_2 = mock.MagicMock()
  mock_df_2.empty = False

  filters = models.Filters()

  # Simulate first execution returning run_id=101 and 5000 bytes
  with mock.patch.object(
      queries,
      "_run_query_cached",
      return_value=(mock_df, 5000, 5000, False, True, True, 101),
  ):
    # First run: new run_id
    result1 = queries.run_query("SELECT 1", filters, "test-proj", 1024**3)
    assert result1.df is mock_df
    assert result1.error is None
    assert result1.bytes_processed == 5000
    assert result1.bytes_billed == 5000
    assert result1.cache_hit is False
    assert result1.bytes_processed_known is True
    assert result1.bytes_billed_known is True
    assert result1.stats_known is True
    assert 101 in queries._SEEN_RUN_IDS

    # Second run: cached result from @st.cache_data returns the same
    # run_id 101
    result2 = queries.run_query("SELECT 1", filters, "test-proj", 1024**3)
    assert result2.df is mock_df
    assert result2.error is None
    assert result2.bytes_processed == 5000
    assert result2.bytes_billed == 0
    assert result2.cache_hit is True
    assert result2.bytes_processed_known is True
    assert result2.bytes_billed_known is True
    assert result2.stats_known is True

  # Third run: a different query invocation produces a new run_id=102
  with mock.patch.object(
      queries,
      "_run_query_cached",
      return_value=(mock_df_2, 8000, 8000, False, True, True, 102),
  ):
    result3 = queries.run_query("SELECT 2", filters, "test-proj", 1024**3)
    assert result3.df is mock_df_2
    assert result3.error is None
    assert result3.bytes_processed == 8000
    assert result3.cache_hit is False
    assert result3.bytes_processed_known is True
    assert result3.bytes_billed_known is True
    assert result3.stats_known is True
    assert 102 in queries._SEEN_RUN_IDS


def test_run_query_cached_guardrail_and_execution():
  mock_client = mock.MagicMock()
  mock_probe = mock.MagicMock()
  mock_probe.total_bytes_processed = 2000
  mock_client.query.return_value = mock_probe

  filters = models.Filters()

  with mock.patch.object(queries, "get_client", return_value=mock_client):
    # Probe exceeds max_bytes (1000): _run_query_cached raises RuntimeError
    with pytest.raises(RuntimeError) as exc_info:
      queries._run_query_cached(
          "SELECT * FROM big_table", filters, "proj", 1000
      )
    assert "Guardrail: this query would scan" in str(exc_info.value)

    # Test run_query catches the error and returns guardrail in QueryResult
    res = queries.run_query("SELECT * FROM big_table", filters, "proj", 1000)
    assert res.df.empty is True
    assert res.error is not None
    assert "Guardrail: this query would scan" in res.error
    assert res.bytes_processed == 0
    assert res.bytes_billed == 0
    assert res.bytes_processed_known is True
    assert res.bytes_billed_known is True
    assert res.cache_hit is False

    # Normal query within max_bytes: returns 7-tuple on success
    mock_probe.total_bytes_processed = 500
    mock_job = mock.MagicMock()
    mock_job.total_bytes_processed = 500
    mock_job.total_bytes_billed = 10_485_760
    mock_job.cache_hit = False
    expected_df = mock.MagicMock()
    mock_job.to_dataframe.return_value = expected_df
    mock_client.query.side_effect = [mock_probe, mock_job]

    df, bytes_proc, bytes_billed, hit, proc_known, bill_known, run_id = (
        queries._run_query_cached("SELECT 1", filters, "proj", 1000)
    )
    assert df is expected_df
    assert bytes_proc == 500
    assert bytes_billed == 10_485_760
    assert hit is False
    assert proc_known is True
    assert bill_known is True
    assert isinstance(run_id, int)


def test_run_query_guardrail_and_dry_run_known_zero():
  """Verifies that when guardrail trips or dry run fails, run_query returns QueryResult with known zero bytes."""
  mock_client = mock.MagicMock()
  mock_probe = mock.MagicMock()
  filters = models.Filters()

  # 1. Guardrail trip (estimate > max_bytes)
  mock_probe.total_bytes_processed = 5000
  mock_client.query.return_value = mock_probe

  with mock.patch.object(queries, "get_client", return_value=mock_client):
    with pytest.raises(queries.QueryExecutionError) as exc_info:
      queries._run_query_cached(
          "SELECT * FROM big_table", filters, "proj", 1000
      )
    assert "Guardrail: this query would scan" in str(exc_info.value)
    assert exc_info.value.bytes_processed == 0
    assert exc_info.value.bytes_billed == 0
    assert exc_info.value.bytes_processed_known is True
    assert exc_info.value.bytes_billed_known is True

    result_guardrail = queries.run_query(
        "SELECT * FROM big_table", filters, "proj", 1000
    )
    assert result_guardrail.df.empty is True
    assert result_guardrail.error is not None
    assert "Guardrail: this query would scan" in result_guardrail.error
    assert result_guardrail.bytes_processed == 0
    assert result_guardrail.bytes_billed == 0
    assert result_guardrail.bytes_processed_known is True
    assert result_guardrail.bytes_billed_known is True
    assert result_guardrail.cache_hit is False

  # 2. Dry run / API failure
  api_err = queries.gexc.GoogleAPICallError("Dry run failed: access denied")
  mock_client.query.side_effect = api_err

  with mock.patch.object(queries, "get_client", return_value=mock_client):
    with pytest.raises(queries.QueryExecutionError) as exc_info:
      queries._run_query_cached(
          "SELECT * FROM big_table", filters, "proj", 1000
      )
    assert exc_info.value.bytes_processed == 0
    assert exc_info.value.bytes_billed == 0
    assert exc_info.value.bytes_processed_known is True
    assert exc_info.value.bytes_billed_known is True

    result_dry_run = queries.run_query(
        "SELECT * FROM big_table", filters, "proj", 1000
    )
    assert result_dry_run.df.empty is True
    assert result_dry_run.error is not None
    assert result_dry_run.bytes_processed == 0
    assert result_dry_run.bytes_billed == 0
    assert result_dry_run.bytes_processed_known is True
    assert result_dry_run.bytes_billed_known is True
    assert result_dry_run.cache_hit is False


def test_run_query_success_with_unavailable_billing_stats():
  """Verifies successful query with total_bytes_billed None preserves proc_known=True, bill_known=False."""
  mock_client = mock.MagicMock()
  mock_probe = mock.MagicMock()
  mock_probe.total_bytes_processed = 500
  mock_job = mock.MagicMock()
  mock_job.total_bytes_processed = 500
  mock_job.total_bytes_billed = None
  mock_job.cache_hit = False
  mock_job.reload.side_effect = RuntimeError("Reload failed")
  expected_df = mock.MagicMock()
  expected_df.empty = False
  mock_job.to_dataframe.return_value = expected_df
  mock_client.query.side_effect = [mock_probe, mock_job]

  filters = models.Filters()
  with mock.patch.object(queries, "get_client", return_value=mock_client):
    result = queries.run_query("SELECT 1", filters, "proj", 1000)
    assert result.df is expected_df
    assert result.error is None
    assert result.bytes_processed == 500
    assert result.bytes_billed == 0
    assert result.cache_hit is False
    assert result.bytes_processed_known is True
    assert result.bytes_billed_known is False
    assert result.stats_known is False

  # Deduplicated rerun marks bytes_billed_known=True ($0 billed is known locally)
  with mock.patch.object(
      queries,
      "_run_query_cached",
      return_value=(expected_df, 500, 0, False, True, False, 101),
  ):
    queries._SEEN_RUN_IDS.add(101)
    res_dedup = queries.run_query("SELECT 1", filters, "proj", 1000)
    assert res_dedup.df is expected_df
    assert res_dedup.error is None
    assert res_dedup.bytes_processed == 500
    assert res_dedup.bytes_billed == 0
    assert res_dedup.cache_hit is True
    assert res_dedup.bytes_processed_known is True
    assert res_dedup.bytes_billed_known is True
    assert res_dedup.stats_known is True


def test_run_query_clears_seen_run_ids_when_exceeding_max(monkeypatch):
  queries._SEEN_RUN_IDS.clear()
  monkeypatch.setattr(queries, "_MAX_SEEN_RUN_IDS", 3)
  filters = models.Filters()
  mock_df = mock.MagicMock()

  with mock.patch.object(
      queries,
      "_run_query_cached",
      side_effect=[
          (mock_df, 100, 100, False, True, True, 1),
          (mock_df, 100, 100, False, True, True, 2),
          (mock_df, 100, 100, False, True, True, 3),
          (mock_df, 100, 100, False, True, True, 4),
      ],
  ):
    queries.run_query("Q1", filters, "proj", 1000)
    queries.run_query("Q2", filters, "proj", 1000)
    queries.run_query("Q3", filters, "proj", 1000)
    assert queries._SEEN_RUN_IDS == {1, 2, 3}

    res = queries.run_query("Q4", filters, "proj", 1000)
    assert queries._SEEN_RUN_IDS == {2, 3, 4}
    assert res.bytes_processed == 100
    assert res.cache_hit is False


def test_job_config_caps_bytes_on_live_runs_only():
  """Tests that maximum_bytes_billed is only set on live runs, not dry runs."""
  filters = models.Filters()
  live_cfg = queries.job_config("SELECT 1", filters, 5_000_000, dry_run=False)
  assert live_cfg.maximum_bytes_billed == 5_000_000
  assert live_cfg.dry_run is False

  dry_cfg = queries.job_config("SELECT 1", filters, 5_000_000, dry_run=True)
  assert dry_cfg.maximum_bytes_billed is None
  assert dry_cfg.dry_run is True


def test_run_query_seen_run_ids_clearing_at_default_max():
  queries._SEEN_RUN_IDS.clear()
  queries._SEEN_RUN_IDS.update(range(queries._MAX_SEEN_RUN_IDS))
  assert len(queries._SEEN_RUN_IDS) == queries._MAX_SEEN_RUN_IDS
  mock_df = mock.MagicMock()
  with mock.patch.object(
      queries,
      "_run_query_cached",
      return_value=(mock_df, 100, 100, False, True, True, 99999),
  ):
    queries.run_query("Q", models.Filters(), "proj", 1000)
    expected_len = (
        queries._MAX_SEEN_RUN_IDS - (queries._MAX_SEEN_RUN_IDS // 4) + 1
    )
    assert len(queries._SEEN_RUN_IDS) == expected_len
    assert 99999 in queries._SEEN_RUN_IDS


def test_load_filter_options_returns_tuple_and_handles_error(
    sample_refs, sample_window
):
  ctx = models.Context(
      refs=sample_refs,
      window=sample_window,
      filters=models.Filters(),
      max_bytes=1000,
      theme=models.LIGHT_THEME,
      price_in=0.0,
      price_out=0.0,
  )
  mock_df = mock.MagicMock()
  mock_df.empty = True
  error_result = models.QueryResult(
      df=mock_df, error="Access Denied: Dataset not found"
  )
  with mock.patch.object(queries, "fetch", return_value=error_result):
    options, result = queries.load_filter_options(ctx)
    assert options == {}
    assert result is error_result
    assert result.error == "Access Denied: Dataset not found"


def test_seed_options_preserves_selection():
  all_sentinel = (models.ALL_SENTINEL,)
  state = {"flt_agent": ["agent-1", "agent-2"], "flt_user_id": ["user-1"]}
  with mock.patch.object(app.st, "session_state", state):
    merged = app._seed_options("flt_agent", ["agent-1"], all_sentinel)
    # agent-2 was absent from fetched options, but is preserved
    assert merged == ["agent-1", "agent-2"]

    merged_empty = app._seed_options("flt_user_id", [], all_sentinel)
    assert merged_empty == ["user-1"]

    merged_none = app._seed_options("flt_session_id", ["sess-1"], all_sentinel)
    assert merged_none == ["sess-1"]


def test_seed_options_and_default_for_survive_widget_identity_change():
  all_sentinel = (models.ALL_SENTINEL,)
  applied_agents = ("agent-x", "agent-y")
  state = {}
  with mock.patch.object(app.st, "session_state", state):
    seeded = app._seed_options("flt_agent", ["agent-1"], applied_agents)
    assert seeded == ["agent-1", "agent-x", "agent-y"]

  assert app._default_for(applied_agents) == ["agent-x", "agent-y"]
  assert app._default_for(all_sentinel) == []


def test_sidebar_filters_preserves_selection_and_accepts_custom_options():
  state = {
      "flt_agent": ["agent-active"],
      "flt_user_id": ["user-123"],
      "flt_event_type": ["agent_start"],
      "flt_session_id": ["sess-456"],
  }
  options: dict[str, list[str]] = {}

  passed_options: dict[str, list[str]] = {}
  multiselect_kwargs: dict[str, dict] = {}

  def fake_multiselect(label, options, key, **kwargs):
    passed_options[key] = options
    multiselect_kwargs[key] = kwargs
    return state.get(key, [])

  def fake_submit(*args, **kwargs):
    if "on_click" in kwargs and callable(kwargs["on_click"]):
      kwargs["on_click"]()
    return True

  with (
      mock.patch.object(app.st, "session_state", state),
      mock.patch.object(app.st, "sidebar", mock.MagicMock()),
      mock.patch.object(app.st, "form_submit_button", side_effect=fake_submit),
      mock.patch.object(
          app.st,
          "number_input",
          side_effect=lambda *args, **kwargs: kwargs.get("value", 0.0),
      ),
      mock.patch.object(app.st, "multiselect", side_effect=fake_multiselect),
  ):
    filters, price_in, price_out = app.sidebar_filters(options)
    assert (price_in, price_out) == (1.25, 5.00)

    # Active selections are preserved even when options query returned empty
    assert state["flt_agent"] == ["agent-active"]
    assert state["flt_user_id"] == ["user-123"]
    assert state["flt_event_type"] == ["agent_start"]
    assert state["flt_session_id"] == ["sess-456"]
    assert filters.agents == ("agent-active",)
    assert filters.user_ids == ("user-123",)
    assert filters.event_types == ("agent_start",)
    assert filters.session_ids == ("sess-456",)

    # Passed options to widgets contain the active selections
    assert "agent-active" in passed_options["flt_agent"]
    assert "user-123" in passed_options["flt_user_id"]
    assert "sess-456" in passed_options["flt_session_id"]

    # Custom options enabled on agent, user, session
    assert multiselect_kwargs["flt_agent"].get("accept_new_options") is True
    assert multiselect_kwargs["flt_user_id"].get("accept_new_options") is True
    assert (
        multiselect_kwargs["flt_session_id"].get("accept_new_options") is True
    )
    assert not multiselect_kwargs["flt_event_type"].get("accept_new_options")


def test_sidebar_filters_returns_applied_filters_when_not_submitted():
  prior_filters = models.Filters(agents=("agent-saved",))
  state = {
      "applied_filters": prior_filters,
      "flt_agent": ["agent-unsubmitted"],
  }
  options = {"agent": ["agent-saved", "agent-unsubmitted"]}

  with (
      mock.patch.object(app.st, "session_state", state),
      mock.patch.object(app.st, "sidebar", mock.MagicMock()),
      mock.patch.object(app.st, "form_submit_button", return_value=False),
      mock.patch.object(
          app.st,
          "number_input",
          side_effect=lambda *args, **kwargs: kwargs.get("value", 0.0),
      ),
      mock.patch.object(
          app.st, "multiselect", return_value=["agent-unsubmitted"]
      ),
  ):
    filters, price_in, price_out = app.sidebar_filters(options)
    assert filters == prior_filters
    assert state["applied_filters"] == prior_filters


def test_custom_values_outside_1000_bounded_options_accepted_and_preserved():
  """Verifies custom values beyond the 1,000 option limit are accepted and bound."""
  # Simulate exactly 1,000 bounded options returned by BigQuery
  bounded_agents = [f"agent-{i:04d}" for i in range(1000)]
  bounded_users = [f"user-{i:04d}" for i in range(1000)]
  bounded_sessions = [f"sess-{i:04d}" for i in range(1000)]
  options = {
      "agent": bounded_agents,
      "user_id": bounded_users,
      "event_type": ["agent_start", "agent_end"],
      "session_id": bounded_sessions,
  }

  custom_agent = "agent-custom-outside-1000"
  custom_user = "user-custom-outside-1000"
  custom_session = "sess-custom-outside-1000"
  assert custom_agent not in bounded_agents
  assert custom_user not in bounded_users
  assert custom_session not in bounded_sessions

  state = {
      "flt_agent": [custom_agent],
      "flt_user_id": [custom_user],
      "flt_event_type": [],
      "flt_session_id": [custom_session],
  }

  all_sentinel = (models.ALL_SENTINEL,)

  # Test _seed_options directly: 1000 options + 1 custom value = 1001 options
  with mock.patch.object(app.st, "session_state", state):
    merged_agents = app._seed_options("flt_agent", bounded_agents, all_sentinel)
    assert len(merged_agents) == 1001
    assert merged_agents[-1] == custom_agent

    merged_users = app._seed_options("flt_user_id", bounded_users, all_sentinel)
    assert len(merged_users) == 1001
    assert merged_users[-1] == custom_user

    merged_sessions = app._seed_options(
        "flt_session_id", bounded_sessions, all_sentinel
    )
    assert len(merged_sessions) == 1001
    assert merged_sessions[-1] == custom_session

  # Test sidebar_filters with mock st widgets
  passed_options = {}
  multiselect_kwargs = {}

  def fake_multiselect(label, options, key, **kwargs):
    passed_options[key] = options
    multiselect_kwargs[key] = kwargs
    return state.get(key, [])

  def fake_submit(*args, **kwargs):
    if "on_click" in kwargs and callable(kwargs["on_click"]):
      kwargs["on_click"]()
    return True

  with (
      mock.patch.object(app.st, "session_state", state),
      mock.patch.object(app.st, "sidebar", mock.MagicMock()),
      mock.patch.object(app.st, "form_submit_button", side_effect=fake_submit),
      mock.patch.object(
          app.st,
          "number_input",
          side_effect=lambda *args, **kwargs: kwargs.get("value", 0.0),
      ),
      mock.patch.object(app.st, "multiselect", side_effect=fake_multiselect),
  ):
    filters, price_in, price_out = app.sidebar_filters(options)
    assert (price_in, price_out) == (1.25, 5.00)

    # 1. Custom values outside the 1000 options are preserved and accepted
    assert filters.agents == (custom_agent,)
    assert filters.user_ids == (custom_user,)
    assert filters.session_ids == (custom_session,)

    # 2. Multiselect options contain 1001 options including the custom value
    assert len(passed_options["flt_agent"]) == 1001
    assert custom_agent in passed_options["flt_agent"]
    assert len(passed_options["flt_user_id"]) == 1001
    assert custom_user in passed_options["flt_user_id"]
    assert len(passed_options["flt_session_id"]) == 1001
    assert custom_session in passed_options["flt_session_id"]

    # 3. Downstream query_parameters correctly binds the custom values outside 1000
    sql = "SELECT 1 WHERE agent IN UNNEST(@agents) AND user_id IN UNNEST(@user_ids) AND session_id IN UNNEST(@session_ids)"
    params = queries.query_parameters(sql, filters)
    param_map = {p.name: p.values for p in params}
    assert param_map["agents"] == [custom_agent]
    assert param_map["user_ids"] == [custom_user]
    assert param_map["session_ids"] == [custom_session]


def test_load_module_resets_sys_path(tmp_path):
  test_file = tmp_path / "dynamic_test_module.py"
  test_file.write_text(
      "import sys\n"
      "from pathlib import Path\n"
      "dir_was_in_path = str(Path(__file__).parent) in sys.path\n"
  )
  dir_str = str(tmp_path)
  assert dir_str not in sys.path

  mod_name = "test_dynamic_sys_path_cleanup"
  try:
    mod = _load_module(mod_name, test_file)
    assert mod.dir_was_in_path is True
    assert dir_str not in sys.path
  finally:
    sys.modules.pop(mod_name, None)
    if dir_str in sys.path:
      sys.path.remove(dir_str)


def test_load_module_resets_sys_path_on_error(tmp_path):
  test_file = tmp_path / "failing_dynamic_module.py"
  test_file.write_text("raise RuntimeError('intentional load failure')\n")
  dir_str = str(tmp_path)
  assert dir_str not in sys.path

  mod_name = "test_failing_dynamic_module"
  try:
    with pytest.raises(RuntimeError, match="intentional load failure"):
      _load_module(mod_name, test_file)
    assert dir_str not in sys.path
  finally:
    sys.modules.pop(mod_name, None)
    if dir_str in sys.path:
      sys.path.remove(dir_str)


def test_charts_color_map():
  state = {}
  with mock.patch.object(charts.st, "session_state", state):
    theme = models.LIGHT_THEME
    # First call assigns slots stably
    cm1 = charts.color_map("agent", ["agent_a", "agent_b"], theme)
    assert cm1["agent_a"] == theme.categorical[0]
    assert cm1["agent_b"] == theme.categorical[1]
    assert cm1[models.OTHER_LABEL] == theme.muted
    assert state["_slots::agent"] == {"agent_a": 0, "agent_b": 1}
    assert models.OTHER_LABEL not in state["_slots::agent"]

    # Second call retains previously assigned slots
    cm2 = charts.color_map("agent", ["agent_b", "agent_c"], theme)
    assert cm2["agent_b"] == theme.categorical[1]
    assert cm2["agent_c"] == theme.categorical[0]
    assert cm2[models.OTHER_LABEL] == theme.muted
    assert state["_slots::agent"] == {
        "agent_a": 0,
        "agent_b": 1,
        "agent_c": 0,
    }
    assert models.OTHER_LABEL not in state["_slots::agent"]

    # Deduplication and OTHER_LABEL input handling in category list
    cm_dedup = charts.color_map(
        "dedup", ["x", "x", models.OTHER_LABEL, "y"], theme
    )
    assert set(cm_dedup.keys()) == {"x", "y", models.OTHER_LABEL}
    assert state["_slots::dedup"] == {"x": 0, "y": 1}
    assert models.OTHER_LABEL not in state["_slots::dedup"]

    # Intra-chart collision resolution:
    # pre-seed state["_slots::collision"] = {"k1": 0, "k2": 0}
    state["_slots::collision"] = {"k1": 0, "k2": 0}
    cm_coll = charts.color_map("collision", ["k1", "k2"], theme)
    assert state["_slots::collision"]["k1"] == 0
    assert state["_slots::collision"]["k2"] == 1
    assert cm_coll["k1"] == theme.categorical[0]
    assert cm_coll["k2"] == theme.categorical[1]

    # Slot reuse beyond categorical palette length with modulo wrapping
    names = [f"name_{i}" for i in range(12)]
    cm3 = charts.color_map("overflow", names, theme)
    assert len(cm3) == 13
    for n in names:
      assert cm3[n] in theme.categorical
    assert state["_slots::overflow"]["name_0"] == 0
    assert state["_slots::overflow"]["name_8"] == 0
    assert state["_slots::overflow"]["name_11"] == 3
    assert models.OTHER_LABEL not in state["_slots::overflow"]


def test_charts_fold_others():
  # 1. Empty dataframe returned unmodified
  df_empty = mock.MagicMock()
  df_empty.empty = True
  assert charts.fold_others(df_empty, "cat", "val") is df_empty

  # 2. Within limit: nunique <= limit returns unmodified
  df_small = mock.MagicMock()
  df_small.empty = False
  df_small.__getitem__.return_value.nunique.return_value = 3
  assert charts.fold_others(df_small, "cat", "val", limit=5) is df_small

  # Exact boundary limit check (nunique == limit returns unmodified)
  df_boundary = mock.MagicMock()
  df_boundary.empty = False
  df_boundary.__getitem__.return_value.nunique.return_value = 5
  assert charts.fold_others(df_boundary, "cat", "val", limit=5) is df_boundary

  # 3. Exceeding limit: nunique > limit executes folding logic
  df_large = mock.MagicMock()
  df_large.empty = False
  df_large.__getitem__.return_value.nunique.return_value = 10
  res = charts.fold_others(df_large, "cat", "val", group_cols=["grp"], limit=5)
  assert res is not df_large


def test_app_main_filter_preservation_on_error(sample_refs, sample_window):
  state = {}
  initial_options = {"agent": ["agent-1", "agent-2"]}
  success_res = models.QueryResult(df=mock.MagicMock(), error=None)
  error_res = models.QueryResult(df=mock.MagicMock(), error="BigQuery timeout")

  with (
      mock.patch.object(app.st, "session_state", state),
      mock.patch.object(app.st, "set_page_config"),
      mock.patch.object(app.st, "title"),
      mock.patch.object(app.st, "tabs", return_value=[mock.MagicMock()] * 4),
      mock.patch.object(
          app, "sidebar_connection", return_value=(sample_refs, 1000)
      ),
      mock.patch.object(app, "sidebar_window", return_value=sample_window),
      mock.patch.object(app, "active_theme", return_value=models.LIGHT_THEME),
      mock.patch.object(app, "row_overview") as mock_row_overview,
      mock.patch.object(app, "row_llm"),
      mock.patch.object(app, "row_tools"),
      mock.patch.object(app, "row_sessions"),
      mock.patch.object(app, "footer"),
      mock.patch.object(app, "sidebar_filters") as mock_sidebar_filters,
  ):
    mock_sidebar_filters.return_value = (models.Filters(), 0.0, 0.0)

    # First run succeeds: options stashed in session_state['_filter_options']
    with mock.patch.object(
        app, "load_filter_options", return_value=(initial_options, success_res)
    ):
      app.main()
      assert state["_filter_options"] == initial_options
      mock_sidebar_filters.assert_called_with(initial_options)
      assert mock_row_overview.call_count == 1

    # Second run encounters error: stashed options retrieved
    mock_sidebar_filters.reset_mock()
    with mock.patch.object(
        app, "load_filter_options", return_value=({}, error_res)
    ):
      app.main()
      assert state["_filter_options"] == initial_options
      mock_sidebar_filters.assert_called_with(initial_options)
      assert mock_row_overview.call_count == 2

    # Third run: query succeeds with empty options (e.g. empty time range)
    # -> stashed options preserved
    mock_sidebar_filters.reset_mock()
    with mock.patch.object(
        app, "load_filter_options", return_value=({}, success_res)
    ):
      app.main()
      assert state["_filter_options"] == initial_options
      mock_sidebar_filters.assert_called_with(initial_options)
      assert mock_row_overview.call_count == 3


def test_footer(sample_refs, sample_window):
  ctx = models.Context(
      refs=sample_refs,
      window=sample_window,
      filters=models.Filters(),
      max_bytes=1024**3,
      theme=models.LIGHT_THEME,
      price_in=3.0,
      price_out=15.0,
      scan_log=[],
  )
  with (
      mock.patch.object(app.st, "caption") as mock_caption,
      mock.patch.object(app.st, "divider") as mock_divider,
  ):
    # Early return on empty scan_log
    app.footer(ctx)
    mock_caption.assert_not_called()
    mock_divider.assert_not_called()

    # Populated scan_log renders caption with query count, billed total,
    # processed total, cache count, per-query cap, and TTL text.
    # Cached queries must be excluded from billed and processed totals.
    ctx.scan_log = [
        models.ScanEntry("q1", 10_485_760, 1024, False, True, True),
        models.ScanEntry("q2", 0, 2048, True, True, True),
        models.ScanEntry("q3", 10_485_760, 4096, False, True, True),
    ]
    app.footer(ctx)
    mock_divider.assert_called_once()
    assert mock_caption.call_count == 3
    caption_text = mock_caption.call_args_list[0][0][0]
    caption_note = mock_caption.call_args_list[1][0][0]
    caption_latency_note = mock_caption.call_args_list[2][0][0]
    # Assert billed and processed totals mathematically exclude cached query bytes
    billed_bytes = 10_485_760 + 10_485_760
    processed_bytes = 1024 + 4096
    assert f"{models.humanize_bytes(billed_bytes)} billed" in caption_text
    assert (
        f"({models.humanize_bytes(processed_bytes)} processed)" in caption_text
    )
    assert "10 MB minimum" in caption_note
    assert "compute/capacity reservations" in caption_note
    assert "metadata delivery latency" in caption_latency_note
    assert "billing telemetry" in caption_latency_note
    assert (
        "unrecorded queries are excluded from the totals"
        in caption_latency_note
    )
    # Assert query counts and cache hit text
    assert "3 queries this run" in caption_text
    assert (
        f"1 served from cache ({models.humanize_bytes(2048)} scan avoided via cache)"
        in caption_text
    )
    # Assert per-query cap and TTL text
    assert (
        f"per-query cap {models.humanize_bytes(ctx.max_bytes)}" in caption_text
    )
    assert (
        f"results cached for {models.CACHE_TTL_SECONDS // 60} min"
        in caption_text
    )

    # Assert 100% cache hit run reports factual cached count without "savings" promises
    ctx.scan_log = [
        models.ScanEntry("q1", 0, 1024, True, True, True),
        models.ScanEntry("q2", 0, 2048, True, True, True),
    ]
    app.footer(ctx)
    caption_cached = mock_caption.call_args_list[-3][0][0]
    assert "0 B billed (0 B processed)" in caption_cached
    assert (
        f"2 served from cache ({models.humanize_bytes(1024 + 2048)} scan avoided via cache)"
        in caption_cached
    )
    assert "billing cost saved via cache" not in caption_cached
    assert "$0 billed" not in caption_cached

    # Assert queries with unknown billing data are counted separately
    ctx.scan_log = [
        models.ScanEntry("q1", 10_485_760, 1024, False, True, True),
        models.ScanEntry("q2", 0, 0, False, False, False),
        models.ScanEntry("q3", 0, 2048, True, True, True),
    ]
    app.footer(ctx)
    caption_unknown = mock_caption.call_args_list[-3][0][0]
    assert "3 queries this run" in caption_unknown
    assert (
        f"{models.humanize_bytes(10_485_760)} billed ({models.humanize_bytes(1024)} processed)"
        in caption_unknown
    )
    assert (
        f"1 served from cache ({models.humanize_bytes(2048)} scan avoided via cache)"
        in caption_unknown
    )
    assert "1 with unavailable billing data" in caption_unknown

    # Assert entry with independent availability accumulates processed bytes when known
    ctx.scan_log = [
        models.ScanEntry("q1", 10_485_760, 1024, False, True, True),
        models.ScanEntry(
            "q2", 0, 512, False, False, True
        ),  # billed unknown, processed known
        models.ScanEntry("q3", 0, 2048, True, True, True),
    ]
    app.footer(ctx)
    caption_independent = mock_caption.call_args_list[-3][0][0]
    assert "3 queries this run" in caption_independent
    assert (
        f"{models.humanize_bytes(10_485_760)} billed ({models.humanize_bytes(1024 + 512)} processed)"
        in caption_independent
    )
    assert "1 with unavailable billing data" in caption_independent

    # Assert entry with unavailable scan data reports correctly
    ctx.scan_log = [
        models.ScanEntry(
            "q1",
            10_485_760,
            0,
            False,
            True,
            False,
        ),  # billed known, processed unknown
    ]
    app.footer(ctx)
    caption_proc_unknown = mock_caption.call_args_list[-3][0][0]
    assert "1 with unavailable scan data" in caption_proc_unknown

    # Assert run with no cached queries omits cache clause
    ctx.scan_log = [
        models.ScanEntry("q1", 10_485_760, 1024, False, True, True),
    ]
    app.footer(ctx)
    caption_no_cache = mock_caption.call_args_list[-3][0][0]
    assert "served from cache" not in caption_no_cache

    # Assert cached query with unrecorded scan size renders with ≥ and note
    ctx.scan_log = [
        models.ScanEntry("q1", 0, 1024 * 1024, True, True, True),
        models.ScanEntry("q2", 0, 0, True, True, False),
    ]
    app.footer(ctx)
    caption_unrec = mock_caption.call_args_list[-3][0][0]
    assert (
        "2 served from cache (≥ 1.0 MB scan avoided via cache; 1 query scan size unrecorded)"
        in caption_unrec
    )
    assert "with unavailable scan data" not in caption_unrec


def test_footer_guardrail_queries(sample_refs, sample_window):
  """Verifies footer displays 0 B billed (0 B processed) on guardrail queries without unavailable telemetry notes."""
  ctx = models.Context(
      refs=sample_refs,
      window=sample_window,
      filters=models.Filters(),
      max_bytes=1024**3,
      theme=models.LIGHT_THEME,
      price_in=3.0,
      price_out=15.0,
      scan_log=[
          models.ScanEntry(
              label="guardrail_query_1",
              bytes_billed=0,
              bytes_processed=0,
              cache_hit=False,
              bytes_billed_known=True,
              bytes_processed_known=True,
          ),
          models.ScanEntry(
              label="guardrail_query_2",
              bytes_billed=0,
              bytes_processed=0,
              cache_hit=False,
              bytes_billed_known=True,
              bytes_processed_known=True,
          ),
      ],
  )
  with (
      mock.patch.object(app.st, "caption") as mock_caption,
      mock.patch.object(app.st, "divider") as mock_divider,
  ):
    app.footer(ctx)
    mock_divider.assert_called_once()
    assert mock_caption.call_count == 3
    caption_text = mock_caption.call_args_list[0][0][0]
    assert "2 queries this run" in caption_text
    assert "0 B billed (0 B processed)" in caption_text
    assert "unavailable billing data" not in caption_text
    assert "unavailable scan data" not in caption_text

  # Also test integrated flow where load_query triggers guardrail trip and writes to ctx.scan_log
  mock_client = mock.MagicMock()
  mock_probe = mock.MagicMock()
  mock_probe.total_bytes_processed = 5000
  mock_client.query.return_value = mock_probe

  ctx_live = models.Context(
      refs=sample_refs,
      window=sample_window,
      filters=models.Filters(),
      max_bytes=1000,
      theme=models.LIGHT_THEME,
      price_in=3.0,
      price_out=15.0,
      scan_log=[],
  )
  with (
      mock.patch.object(queries, "get_client", return_value=mock_client),
      mock.patch.object(queries.st, "spinner"),
      mock.patch.object(queries.st, "error"),
  ):
    res = queries.fetch("SELECT 1", ctx_live, "guardrail_label")
    assert res.error is not None
    assert "Guardrail:" in res.error
    assert len(ctx_live.scan_log) == 1
    assert ctx_live.scan_log[0].bytes_billed == 0
    assert ctx_live.scan_log[0].bytes_processed == 0
    assert ctx_live.scan_log[0].bytes_billed_known is True
    assert ctx_live.scan_log[0].bytes_processed_known is True

  with (
      mock.patch.object(app.st, "caption") as mock_caption,
      mock.patch.object(app.st, "divider"),
  ):
    app.footer(ctx_live)
    caption_text = mock_caption.call_args_list[0][0][0]
    assert "1 queries this run" in caption_text
    assert "0 B billed (0 B processed)" in caption_text
    assert "unavailable billing data" not in caption_text
    assert "unavailable scan data" not in caption_text


def test_fetch_records_scan_log_entry(sample_refs, sample_window):
  fake_df = _MockDataFrame([{"count": 42}])
  query_result = models.QueryResult(
      df=fake_df,
      error=None,
      bytes_processed=1024,
      bytes_billed=10 * 1024 * 1024,
      cache_hit=False,
      bytes_processed_known=True,
      bytes_billed_known=True,
  )
  with mock.patch.object(queries, "run_query", return_value=query_result):
    ctx = models.Context(
        refs=sample_refs,
        window=sample_window,
        filters=models.Filters(),
        max_bytes=1024**3,
        theme=models.LIGHT_THEME,
        price_in=3.0,
        price_out=15.0,
    )
    res = queries.fetch("SELECT 1", ctx, "Test Panel")
    assert res.df is fake_df
    assert len(ctx.scan_log) == 1
    entry = ctx.scan_log[0]
    assert isinstance(entry, models.ScanEntry)
    assert entry.label == "Test Panel"
    assert entry.bytes_billed == 10 * 1024 * 1024
    assert entry.bytes_processed == 1024
    assert entry.cache_hit is False
    assert entry.bytes_billed_known is True
    assert entry.bytes_processed_known is True


def test_sidebar_filters_case_a_custom_values_submission():
  """Case A: From ALL, enter custom-agent, custom-user, custom-session and click Apply once:

  all submitted arrays must be the custom values (not ['___ALL___']).
  """
  state = {}
  options = {
      "agent": [],
      "user_id": [],
      "event_type": ["agent_start"],
      "session_id": [],
  }

  widget_inputs = {
      "flt_agent": ["custom-agent"],
      "flt_user_id": ["custom-user"],
      "flt_event_type": [],
      "flt_session_id": ["custom-session"],
  }

  def fake_multiselect(label, options, key, **kwargs):
    val = widget_inputs.get(key, [])
    state[key] = val
    return val

  def fake_submit(*args, **kwargs):
    if "on_click" in kwargs and callable(kwargs["on_click"]):
      kwargs["on_click"]()
    return True

  with (
      mock.patch.object(app.st, "session_state", state),
      mock.patch.object(app.st, "sidebar", mock.MagicMock()),
      mock.patch.object(app.st, "form_submit_button", side_effect=fake_submit),
      mock.patch.object(
          app.st,
          "number_input",
          side_effect=lambda *args, **kwargs: kwargs.get("value", 0.0),
      ),
      mock.patch.object(app.st, "multiselect", side_effect=fake_multiselect),
  ):
    filters, price_in, price_out = app.sidebar_filters(options)
    assert (price_in, price_out) == (1.25, 5.00)

    assert filters.agents == ("custom-agent",)
    assert filters.user_ids == ("custom-user",)
    assert filters.event_types == (models.ALL_SENTINEL,)
    assert filters.session_ids == ("custom-session",)

    assert state["applied_filters"] == filters
    assert state["flt_agent"] == ["custom-agent"]
    assert state["flt_user_id"] == ["custom-user"]
    assert state["flt_event_type"] == []
    assert state["flt_session_id"] == ["custom-session"]


def test_sidebar_filters_case_b_change_applied_to_new_values():
  """Case B: Apply alpha/u1/LLM_RESPONSE/s1, then change to existing beta/u2/TOOL_END/s2

  and click Apply again: all arrays must commit the new values
  (beta/u2/TOOL_END/s2).
  """
  prior_filters = models.Filters(
      agents=("alpha",),
      user_ids=("u1",),
      event_types=("LLM_RESPONSE",),
      session_ids=("s1",),
  )
  state = {
      "applied_filters": prior_filters,
      "flt_agent": ["alpha"],
      "flt_user_id": ["u1"],
      "flt_event_type": ["LLM_RESPONSE"],
      "flt_session_id": ["s1"],
  }
  options = {
      "agent": ["alpha", "beta"],
      "user_id": ["u1", "u2"],
      "event_type": ["LLM_RESPONSE", "TOOL_END"],
      "session_id": ["s1", "s2"],
  }

  widget_inputs = {
      "flt_agent": ["beta"],
      "flt_user_id": ["u2"],
      "flt_event_type": ["TOOL_END"],
      "flt_session_id": ["s2"],
  }

  def fake_multiselect(label, options, key, **kwargs):
    val = widget_inputs.get(key, [])
    state[key] = val
    return val

  def fake_submit(*args, **kwargs):
    if "on_click" in kwargs and callable(kwargs["on_click"]):
      kwargs["on_click"]()
    return True

  with (
      mock.patch.object(app.st, "session_state", state),
      mock.patch.object(app.st, "sidebar", mock.MagicMock()),
      mock.patch.object(app.st, "form_submit_button", side_effect=fake_submit),
      mock.patch.object(
          app.st,
          "number_input",
          side_effect=lambda *args, **kwargs: kwargs.get("value", 0.0),
      ),
      mock.patch.object(app.st, "multiselect", side_effect=fake_multiselect),
  ):
    filters, price_in, price_out = app.sidebar_filters(options)
    assert (price_in, price_out) == (1.25, 5.00)

    assert filters.agents == ("beta",)
    assert filters.user_ids == ("u2",)
    assert filters.event_types == ("TOOL_END",)
    assert filters.session_ids == ("s2",)

    assert state["applied_filters"] == filters
    assert state["flt_agent"] == ["beta"]
    assert state["flt_user_id"] == ["u2"]
    assert state["flt_event_type"] == ["TOOL_END"]
    assert state["flt_session_id"] == ["s2"]


def test_sidebar_filters_case_c_clearing_selection_commits_all_sentinel():
  """Case C: Clearing a selection and clicking Apply must commit ALL_SENTINEL."""
  prior_filters = models.Filters(
      agents=("beta",),
      user_ids=("u2",),
      event_types=("TOOL_END",),
      session_ids=("s2",),
  )
  state = {
      "applied_filters": prior_filters,
      "flt_agent": ["beta"],
      "flt_user_id": ["u2"],
      "flt_event_type": ["TOOL_END"],
      "flt_session_id": ["s2"],
  }
  options = {
      "agent": ["beta"],
      "user_id": ["u2"],
      "event_type": ["TOOL_END"],
      "session_id": ["s2"],
  }

  def fake_multiselect(label, options, key, **kwargs):
    state[key] = []
    return []

  def fake_submit(*args, **kwargs):
    if "on_click" in kwargs and callable(kwargs["on_click"]):
      kwargs["on_click"]()
    return True

  with (
      mock.patch.object(app.st, "session_state", state),
      mock.patch.object(app.st, "sidebar", mock.MagicMock()),
      mock.patch.object(app.st, "form_submit_button", side_effect=fake_submit),
      mock.patch.object(
          app.st,
          "number_input",
          side_effect=lambda *args, **kwargs: kwargs.get("value", 0.0),
      ),
      mock.patch.object(app.st, "multiselect", side_effect=fake_multiselect),
  ):
    filters, price_in, price_out = app.sidebar_filters(options)
    assert (price_in, price_out) == (1.25, 5.00)

    assert filters.agents == (models.ALL_SENTINEL,)
    assert filters.user_ids == (models.ALL_SENTINEL,)
    assert filters.event_types == (models.ALL_SENTINEL,)
    assert filters.session_ids == (models.ALL_SENTINEL,)

    assert state["applied_filters"] == filters
    assert state["flt_agent"] == []
    assert state["flt_user_id"] == []
    assert state["flt_event_type"] == []
    assert state["flt_session_id"] == []


def test_reset_filters_on_tablerefs_change(sample_refs, sample_window):
  """Verify filter states are reset when TableRefs changes (connection change)."""
  old_refs = sample_refs
  new_refs = models.TableRefs(
      project="other-proj",
      dataset="other_ds",
      table="other_events",
      view_prefix="other_",
  )
  state = {
      "_last_refs": old_refs,
      "applied_filters": models.Filters(agents=("agent-x",)),
      "_filter_options": {"agent": ["agent-x"]},
      "flt_agent": ["agent-x"],
      "flt_user_id": ["user-1"],
      "flt_event_type": ["start"],
      "flt_session_id": ["sess-1"],
      "_selected_session_id": "sess-1",
  }

  success_res = models.QueryResult(df=mock.MagicMock(), error=None)

  with (
      mock.patch.object(app.st, "session_state", state),
      mock.patch.object(app.st, "set_page_config"),
      mock.patch.object(app.st, "title"),
      mock.patch.object(app.st, "tabs", return_value=[mock.MagicMock()] * 4),
      mock.patch.object(
          app, "sidebar_connection", return_value=(new_refs, 1000)
      ),
      mock.patch.object(app, "sidebar_window", return_value=sample_window),
      mock.patch.object(app, "active_theme", return_value=models.LIGHT_THEME),
      mock.patch.object(
          app, "load_filter_options", return_value=({}, success_res)
      ),
      mock.patch.object(app, "row_overview") as mock_row_overview,
      mock.patch.object(app, "row_llm"),
      mock.patch.object(app, "row_tools"),
      mock.patch.object(app, "row_sessions"),
      mock.patch.object(app, "footer"),
      mock.patch.object(app, "sidebar_filters") as mock_sidebar_filters,
  ):
    mock_sidebar_filters.return_value = (models.Filters(), 0.0, 0.0)
    app.main()

    mock_row_overview.assert_called_once()
    assert state["_last_refs"] == new_refs
    assert state["applied_filters"] == models.Filters()
    assert "_filter_options" not in state
    assert "flt_agent" not in state
    assert "flt_user_id" not in state
    assert "flt_event_type" not in state
    assert "flt_session_id" not in state
    assert "_selected_session_id" not in state


def test_app_main_lazy_tabs_dispatch(sample_refs, sample_window):
  """Verify lazy tabs mode uses st.segmented_control and dispatches to each row function."""
  state = {}
  success_res = models.QueryResult(df=mock.MagicMock(), error=None)
  for tab_choice, expected_fn in [
      ("Overview", "row_overview"),
      ("LLM & FinOps", "row_llm"),
      ("Tools & Execution", "row_tools"),
      ("Sessions & Traces", "row_sessions"),
      (None, "row_overview"),
  ]:
    with (
        mock.patch.object(app, "_LAZY_TABS", True),
        mock.patch.object(app.st, "session_state", state),
        mock.patch.object(app.st, "set_page_config"),
        mock.patch.object(app.st, "title"),
        mock.patch.object(
            app.st, "segmented_control", return_value=tab_choice
        ) as mock_seg,
        mock.patch.object(
            app, "sidebar_connection", return_value=(sample_refs, 1000)
        ),
        mock.patch.object(app, "sidebar_window", return_value=sample_window),
        mock.patch.object(app, "active_theme", return_value=models.LIGHT_THEME),
        mock.patch.object(
            app, "load_filter_options", return_value=({}, success_res)
        ),
        mock.patch.object(app, "row_overview") as m_overview,
        mock.patch.object(app, "row_llm") as m_llm,
        mock.patch.object(app, "row_tools") as m_tools,
        mock.patch.object(app, "row_sessions") as m_sessions,
        mock.patch.object(app, "footer"),
        mock.patch.object(
            app, "sidebar_filters", return_value=(models.Filters(), 0.0, 0.0)
        ),
    ):
      app.main()
      mock_seg.assert_called_once_with(
          "Dashboard",
          [
              "Overview",
              "LLM & FinOps",
              "Tools & Execution",
              "Sessions & Traces",
          ],
          default="Overview",
          label_visibility="collapsed",
          required=True,
          key="_active_tab",
      )
      fn_map = {
          "row_overview": m_overview,
          "row_llm": m_llm,
          "row_tools": m_tools,
          "row_sessions": m_sessions,
      }
      for name, mock_fn in fn_map.items():
        if name == expected_fn:
          mock_fn.assert_called_once()
        else:
          mock_fn.assert_not_called()


def test_row_sessions_maintains_selected_trace_on_refresh(
    sample_refs, sample_window
):
  """R4: Keep inspected trace selected when recent sessions refresh."""
  state = {"_selected_session_id": "sess-preserved"}
  fake_df = mock.MagicMock()
  fake_df.empty = False
  fake_df.__getitem__.return_value.tolist.return_value = [
      "sess-newest",
      "sess-preserved",
      "sess-older",
  ]

  sessions_result = models.QueryResult(
      df=fake_df,
      error=None,
      bytes_processed=100,
      bytes_billed=100,
      cache_hit=False,
  )

  ctx = models.Context(
      refs=sample_refs,
      window=sample_window,
      filters=models.Filters(),
      max_bytes=1000,
      theme=models.LIGHT_THEME,
      price_in=0.0,
      price_out=0.0,
  )

  selectbox_calls = []

  def fake_selectbox(label, options, index=0, **kwargs):
    selectbox_calls.append({"label": label, "options": options, "index": index})
    return options[index]

  with (
      mock.patch.object(app.st, "session_state", state),
      mock.patch.object(app.st, "divider"),
      mock.patch.object(app.st, "markdown"),
      mock.patch.object(app.st, "caption"),
      mock.patch.object(app.st, "dataframe"),
      mock.patch.object(
          app,
          "fetch",
          side_effect=[
              sessions_result,
              models.QueryResult(df=_MockDataFrame(), error=None),
          ],
      ),
      mock.patch.object(app.st, "selectbox", side_effect=fake_selectbox),
  ):
    app.row_sessions(ctx)

    # The selectbox should maintain selection of "sess-preserved" at index 1 instead of jumping to 0
    assert len(selectbox_calls) == 1
    call = selectbox_calls[0]
    assert call["options"] == ["sess-newest", "sess-preserved", "sess-older"]
    assert call["index"] == 1
    assert state["_selected_session_id"] == "sess-preserved"

  # If prev_chosen is not in ids, fall back to index 0
  state["_selected_session_id"] = "sess-not-present"
  selectbox_calls.clear()
  with (
      mock.patch.object(app.st, "session_state", state),
      mock.patch.object(app.st, "divider"),
      mock.patch.object(app.st, "markdown"),
      mock.patch.object(app.st, "caption"),
      mock.patch.object(app.st, "dataframe"),
      mock.patch.object(
          app,
          "fetch",
          side_effect=[
              sessions_result,
              models.QueryResult(df=_MockDataFrame(), error=None),
          ],
      ),
      mock.patch.object(app.st, "selectbox", side_effect=fake_selectbox),
  ):
    app.row_sessions(ctx)
    assert len(selectbox_calls) == 1
    assert selectbox_calls[0]["index"] == 0
    assert state["_selected_session_id"] == "sess-newest"


def test_row_sessions_non_empty_trace_table(sample_refs, sample_window):
  """Verify st.dataframe is called with width='stretch', hide_index=True for non-empty trace table."""
  pd = pytest.importorskip("pandas")
  state = {}
  sessions_df = pd.DataFrame({"session_id": ["sess-1", "sess-2"]})
  trace_df = pd.DataFrame(
      [
          {
              "event_id": "evt-1",
              "event_type": "tool_start",
              "timestamp": "2026-01-01 00:00:00+00:00",
          },
          {
              "event_id": "evt-2",
              "event_type": "tool_complete",
              "timestamp": "2026-01-01 00:01:00+00:00",
          },
      ]
  )

  sessions_result = models.QueryResult(
      df=sessions_df,
      error=None,
      bytes_processed=100,
      bytes_billed=100,
      cache_hit=False,
  )
  trace_result = models.QueryResult(
      df=trace_df,
      error=None,
      bytes_processed=200,
      bytes_billed=200,
      cache_hit=False,
  )

  ctx = models.Context(
      refs=sample_refs,
      window=sample_window,
      filters=models.Filters(),
      max_bytes=1000,
      theme=models.LIGHT_THEME,
      price_in=0.0,
      price_out=0.0,
  )

  with (
      mock.patch.object(app.st, "session_state", state),
      mock.patch.object(app.st, "divider"),
      mock.patch.object(app.st, "markdown"),
      mock.patch.object(app.st, "caption") as mock_caption,
      mock.patch.object(app.st, "dataframe") as mock_dataframe,
      mock.patch.object(
          app,
          "fetch",
          side_effect=[sessions_result, trace_result],
      ),
      mock.patch.object(app.st, "selectbox", return_value="sess-1"),
  ):
    app.row_sessions(ctx)

    mock_dataframe.assert_called_once_with(
        trace_df, width="stretch", hide_index=True
    )
    caption_calls = [c.args[0] for c in mock_caption.call_args_list]
    assert any("read the session chronologically" in c for c in caption_calls)
    assert not any("No events for this session" in c for c in caption_calls)


def test_billing_statistics_retained_on_dataframe_download_failure():
  """R5: Retain billing statistics after result-download failure."""
  mock_client = mock.MagicMock()
  mock_probe = mock.MagicMock()
  mock_probe.total_bytes_processed = 1000
  mock_client.query.return_value = mock_probe

  mock_job = mock.MagicMock()
  mock_job.total_bytes_processed = 20480
  mock_job.total_bytes_billed = 10485760
  mock_job.cache_hit = False
  mock_job.ended = dt.datetime.now(dt.timezone.utc)
  # Simulate ServiceUnavailable or network drop during to_dataframe()
  from google.api_core import exceptions as gexc

  mock_job.to_dataframe.side_effect = gexc.ServiceUnavailable(
      "503 Service Unavailable"
  )

  mock_client.query.side_effect = [mock_probe, mock_job]
  filters = models.Filters()

  with mock.patch.object(queries, "get_client", return_value=mock_client):
    result = queries.run_query(
        "SELECT * FROM events", filters, "proj", 100000000
    )

    assert result.df.empty is True
    assert result.error is not None
    assert result.bytes_processed == 20480
    assert result.bytes_billed == 10485760
    assert result.cache_hit is False
    assert result.bytes_processed_known is True
    assert result.bytes_billed_known is True
    assert result.stats_known is True

  # Also test when cache_hit is True
  mock_client2 = mock.MagicMock()
  mock_probe2 = mock.MagicMock()
  mock_probe2.total_bytes_processed = 1000
  mock_job2 = mock.MagicMock()
  mock_job2.total_bytes_processed = 5000
  mock_job2.total_bytes_billed = 10485760
  mock_job2.cache_hit = True
  mock_job2.ended = dt.datetime.now(dt.timezone.utc)
  mock_job2.to_dataframe.side_effect = RuntimeError("Stream closed")
  mock_client2.query.side_effect = [mock_probe2, mock_job2]

  with mock.patch.object(queries, "get_client", return_value=mock_client2):
    result2 = queries.run_query(
        "SELECT * FROM events", filters, "proj", 100000000
    )

    assert result2.df.empty is True
    assert result2.error is not None
    assert result2.bytes_processed == 5000
    assert result2.bytes_billed == 0  # $0 billed if cache_hit
    assert result2.cache_hit is True
    assert result2.bytes_processed_known is True
    assert result2.bytes_billed_known is True
    assert result2.stats_known is True


def test_extract_job_stats_degraded_path_yields_runtime_error_and_zero_bytes():
  """Job with total_bytes_processed=None and ended=None yields RuntimeError and 0 bytes."""
  mock_client = mock.MagicMock()
  mock_probe = mock.MagicMock()
  mock_probe.total_bytes_processed = 1000

  mock_job = mock.MagicMock()
  mock_job.total_bytes_processed = None
  mock_job.total_bytes_billed = None
  mock_job.ended = None
  mock_job.reload.side_effect = RuntimeError("job gone")
  mock_job.to_dataframe.side_effect = RuntimeError(
      "Query failed without job stats"
  )

  # Directly verify _extract_job_stats and _query_failure degraded behavior
  assert queries._extract_job_stats(mock_job) is None
  failure_err = queries._query_failure(
      "Query failed without job stats", mock_job
  )
  assert type(failure_err) is RuntimeError
  assert not isinstance(failure_err, queries.QueryExecutionError)

  mock_client.query.side_effect = [mock_probe, mock_job]
  filters = models.Filters()

  with mock.patch.object(queries, "get_client", return_value=mock_client):
    result = queries.run_query(
        "SELECT * FROM events", filters, "proj", 100000000
    )

    assert result.df.empty is True
    assert "Query failed without job stats" in result.error
    assert result.bytes_processed == 0
    assert result.bytes_billed == 0
    assert result.cache_hit is False
    assert result.bytes_processed_known is False
    assert result.bytes_billed_known is False
    assert result.stats_known is False


def test_extract_job_stats_reloads_when_initially_none():
  """Job with total_bytes_processed initially None reloads to populate stats."""
  mock_job = mock.MagicMock()
  mock_job.total_bytes_processed = None
  mock_job.ended = None
  mock_job.cache_hit = False

  def fake_reload(*args, **kwargs):
    mock_job.total_bytes_processed = 2048
    mock_job.total_bytes_billed = 10485760

  mock_job.reload.side_effect = fake_reload

  stats = queries._extract_job_stats(mock_job)
  mock_job.reload.assert_called_once_with(
      timeout=queries._JOB_RELOAD_TIMEOUT_SECONDS
  )
  assert stats == (2048, 10485760, False, True, True)


def test_extract_job_stats_reloads_when_total_bytes_billed_is_none():
  """Job with total_bytes_processed populated but total_bytes_billed None reloads."""
  mock_job = mock.MagicMock()
  mock_job.total_bytes_processed = 2048
  mock_job.total_bytes_billed = None
  mock_job.ended = dt.datetime.now(dt.timezone.utc)
  mock_job.cache_hit = False

  def fake_reload(*args, **kwargs):
    mock_job.total_bytes_billed = 10485760

  mock_job.reload.side_effect = fake_reload

  stats = queries._extract_job_stats(mock_job)
  mock_job.reload.assert_called_once_with(
      timeout=queries._JOB_RELOAD_TIMEOUT_SECONDS
  )
  assert stats == (2048, 10485760, False, True, True)


def test_extract_job_stats_billed_remains_none_marks_stats_unknown():
  """If total_bytes_billed remains None after reload, stats_known is False."""
  mock_job = mock.MagicMock()
  mock_job.total_bytes_processed = 2048
  mock_job.total_bytes_billed = None
  mock_job.ended = dt.datetime.now(dt.timezone.utc)
  mock_job.cache_hit = False
  mock_job.reload.side_effect = RuntimeError("reload failed")

  stats = queries._extract_job_stats(mock_job)
  assert stats == (2048, 0, False, True, False)

  err = queries._query_failure("Failed to fetch", mock_job)
  assert isinstance(err, queries.QueryExecutionError)
  assert err.bytes_processed_known is True
  assert err.bytes_billed_known is False
  assert err.stats_known is False
  assert err.bytes_billed == 0
  assert err.bytes_processed == 2048


def test_fetch_shows_loading_spinner(sample_refs, sample_window):
  """R5: Wrap panel queries in fetch with st.spinner."""
  ctx = models.Context(
      refs=sample_refs,
      window=sample_window,
      filters=models.Filters(),
      max_bytes=1024**3,
      theme=models.LIGHT_THEME,
      price_in=3.0,
      price_out=15.0,
  )
  query_res = models.QueryResult(
      df=_MockDataFrame([{"a": 1}]),
      error=None,
      bytes_processed=100,
      bytes_billed=100,
      cache_hit=False,
  )

  with (
      mock.patch.object(queries.st, "spinner") as mock_spinner,
      mock.patch.object(queries, "run_query", return_value=query_res),
  ):
    queries.fetch("SELECT 1", ctx, "Overview totals")
    mock_spinner.assert_called_once_with("Loading Overview totals...")
