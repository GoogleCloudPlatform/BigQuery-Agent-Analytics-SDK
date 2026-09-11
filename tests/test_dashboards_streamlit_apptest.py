"""AppTest suite for the Streamlit dashboard.

Uses streamlit.testing.v1.AppTest to execute and assert against the live app
flow without unconditional mocks of streamlit.
"""

from __future__ import annotations

from pathlib import Path
import sys
from unittest import mock

import pandas as pd
import pytest

pytest.importorskip("streamlit")

from streamlit.testing.v1 import AppTest

DASHBOARDS_DIR = (
    Path(__file__).resolve().parents[1] / "dashboards" / "streamlit"
)
if str(DASHBOARDS_DIR) not in sys.path:
  sys.path.insert(0, str(DASHBOARDS_DIR))

import app
import models
import queries

APP_PATH = DASHBOARDS_DIR / "app.py"


def _app_test() -> AppTest:
  return AppTest.from_file(str(APP_PATH), default_timeout=30)


@pytest.fixture(autouse=True)
def mock_env(monkeypatch: pytest.MonkeyPatch):
  """Seeds standard BigQuery connection environment variables."""
  monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)
  monkeypatch.setenv("BQ_PROJECT_ID", "test-project")
  monkeypatch.setenv("BQ_DATASET_ID", "test_dataset")
  monkeypatch.setenv("BQ_TABLE_ID", "events")
  monkeypatch.setenv("BQ_VIEW_PREFIX", "adk_")


@pytest.fixture
def mock_queries():
  """Stubs out BigQuery queries for fast and hermetic app testing."""
  filter_options = {
      "agent": ["agent-a", "agent-b"],
      "user_id": ["user-1", "user-2"],
      "event_type": ["start", "complete"],
      "session_id": ["sess-1", "sess-2", "sess-3"],
  }
  query_res = models.QueryResult(
      df=pd.DataFrame(),
      error=None,
      bytes_processed=0,
      bytes_billed=0,
      cache_hit=True,
  )
  with (
      mock.patch.object(
          queries,
          "load_filter_options",
          return_value=(filter_options, query_res),
      ),
      mock.patch.object(
          app,
          "load_filter_options",
          return_value=(filter_options, query_res),
          create=True,
      ),
      mock.patch.object(queries, "fetch", return_value=query_res),
      mock.patch.object(app, "fetch", return_value=query_res, create=True),
  ):
    yield


def test_sidebar_filters_apply(mock_queries):
  """Test entering filters, clicking Apply filters, and verifying applied_filters."""
  at = _app_test()
  at.run()
  assert not at.exception

  # Select values in filter multiselects
  at.sidebar.multiselect(key="flt_agent").select("agent-a")
  at.sidebar.multiselect(key="flt_user_id").select("user-1")

  apply_btn = [b for b in at.sidebar.button if b.label == "Apply filters"][0]
  apply_btn.click().run()
  assert not at.exception

  applied: models.Filters = at.session_state["applied_filters"]
  assert applied.agents == ("agent-a",)
  assert applied.user_ids == ("user-1",)
  assert applied.event_types == (models.ALL_SENTINEL,)
  assert applied.session_ids == (models.ALL_SENTINEL,)


def test_sidebar_filters_successive_apply(mock_queries):
  """Apply A, then Apply B, asserting B is applied."""
  at = _app_test()
  at.run()
  assert not at.exception

  # Apply A
  at.sidebar.multiselect(key="flt_agent").select("agent-a")
  apply_btn = [b for b in at.sidebar.button if b.label == "Apply filters"][0]
  apply_btn.click().run()
  assert at.session_state["applied_filters"].agents == ("agent-a",)

  # Apply B
  at.sidebar.multiselect(key="flt_agent").set_value(["agent-b"])
  apply_btn = [b for b in at.sidebar.button if b.label == "Apply filters"][0]
  apply_btn.click().run()
  assert at.session_state["applied_filters"].agents == ("agent-b",)


def test_sidebar_filters_custom_id(mock_queries):
  """Entering a custom ID via multiselect and applying it."""
  at = _app_test()
  at.run()
  assert not at.exception

  # Provide a custom agent ID not previously in options
  at.sidebar.multiselect(key="flt_agent").set_value(["custom-agent-xyz"])
  apply_btn = [b for b in at.sidebar.button if b.label == "Apply filters"][0]
  apply_btn.click().run()
  assert not at.exception

  assert at.session_state["applied_filters"].agents == ("custom-agent-xyz",)


def test_filter_widgets_accept_new_options_flags(mock_queries):
  """Verify accept_new_options is True for custom-input widgets and False for fixed options."""
  at = _app_test()
  at.run()
  assert not at.exception

  for key in ("flt_agent", "flt_user_id", "flt_session_id"):
    assert at.sidebar.multiselect(key=key).proto.accept_new_options is True
  assert (
      at.sidebar.multiselect(key="flt_event_type").proto.accept_new_options
      is False
  )


def test_sidebar_filters_clear_selection(mock_queries):
  """Clearing an existing selection and applying verifies ALL_SENTINEL is applied."""
  at = _app_test()
  at.run()
  assert not at.exception

  # Select an agent and apply
  at.sidebar.multiselect(key="flt_agent").select("agent-a")
  apply_btn = [b for b in at.sidebar.button if b.label == "Apply filters"][0]
  apply_btn.click().run()
  assert not at.exception
  assert at.session_state["applied_filters"].agents == ("agent-a",)

  # Clear selection and apply
  at.sidebar.multiselect(key="flt_agent").unselect("agent-a")
  apply_btn = [b for b in at.sidebar.button if b.label == "Apply filters"][0]
  apply_btn.click().run()
  assert not at.exception
  applied_filters: models.Filters = at.session_state["applied_filters"]
  assert applied_filters.agents == (models.ALL_SENTINEL,)


def test_session_selectbox_preservation():
  """Verify session selectbox maintains selected trace when session options refresh."""
  sess_res1 = models.QueryResult(
      df=pd.DataFrame({"session_id": ["sess-1", "sess-2", "sess-3"]}),
      error=None,
      bytes_processed=0,
      bytes_billed=0,
      cache_hit=True,
  )
  sess_res2 = models.QueryResult(
      df=pd.DataFrame({"session_id": ["sess-newest", "sess-2", "sess-older"]}),
      error=None,
      bytes_processed=0,
      bytes_billed=0,
      cache_hit=True,
  )
  current_res = sess_res1

  def fake_fetch(sql, ctx, label):
    if label == "Recent sessions":
      return current_res
    return models.QueryResult(
        df=pd.DataFrame(),
        error=None,
        bytes_processed=0,
        bytes_billed=0,
        cache_hit=True,
    )

  with (
      mock.patch.object(
          queries, "load_filter_options", return_value=({}, sess_res1)
      ),
      mock.patch.object(
          app, "load_filter_options", return_value=({}, sess_res1), create=True
      ),
      mock.patch.object(queries, "fetch", side_effect=fake_fetch),
      mock.patch.object(app, "fetch", side_effect=fake_fetch, create=True),
  ):
    at = _app_test()
    at.run()
    assert not at.exception

    sess_box = [s for s in at.selectbox if s.label == "Session"][0]
    assert sess_box.value == "sess-1"

    # Select sess-2
    sess_box.select("sess-2").run()
    assert at.session_state["_selected_session_id"] == "sess-2"

    # Refresh sessions list: sess-2 is still present but now at index 1 instead of 0
    current_res = sess_res2
    at.run()
    assert not at.exception

    sess_box_refreshed = [s for s in at.selectbox if s.label == "Session"][0]
    assert sess_box_refreshed.value == "sess-2"
    assert at.session_state["_selected_session_id"] == "sess-2"


def test_connection_change_resets_filters():
  """Changing TableRefs resets filter session state."""
  filter_options = {"agent": ["agent-a"]}
  query_res = models.QueryResult(
      df=pd.DataFrame(),
      error=None,
      bytes_processed=0,
      bytes_billed=0,
      cache_hit=True,
  )
  recent_sessions_res = models.QueryResult(
      df=pd.DataFrame({"session_id": ["sess-init-1"]}),
      error=None,
      bytes_processed=0,
      bytes_billed=0,
      cache_hit=True,
  )

  def fake_fetch(sql, ctx, label):
    if label == "Recent sessions" and ctx.refs.dataset == "test_dataset":
      return recent_sessions_res
    return query_res

  with (
      mock.patch.object(
          queries,
          "load_filter_options",
          return_value=(filter_options, query_res),
      ),
      mock.patch.object(
          app,
          "load_filter_options",
          return_value=(filter_options, query_res),
          create=True,
      ),
      mock.patch.object(queries, "fetch", side_effect=fake_fetch),
      mock.patch.object(app, "fetch", side_effect=fake_fetch, create=True),
  ):
    at = _app_test()
    at.run()
    assert not at.exception
    assert at.session_state["_selected_session_id"] == "sess-init-1"

    # Apply an agent filter
    ms = at.sidebar.multiselect(key="flt_agent")
    ms.select("agent-a")
    apply_btn = [b for b in at.sidebar.button if b.label == "Apply filters"][0]
    apply_btn.click().run()
    assert at.session_state["applied_filters"].agents == ("agent-a",)
    assert at.session_state["_selected_session_id"] == "sess-init-1"

    # Change dataset in connection form
    ti_ds = [ti for ti in at.sidebar.text_input if ti.label == "Dataset ID"][0]
    ti_ds.input("test_dataset_2")
    connect_btn = [b for b in at.sidebar.button if b.label == "Connect"][0]
    connect_btn.click().run()
    assert not at.exception

    # Verify filter state has been reset to defaults
    assert at.session_state["applied_filters"] == models.Filters()
    assert at.session_state["flt_agent"] == []
    assert at.sidebar.multiselect(key="flt_agent").value == []
    assert "_selected_session_id" not in at.session_state


def test_run_query_cached_keys_on_every_argument():
  """Verify that changing any of the 4 arguments causes a cache miss, identical arguments hit cache."""
  queries._run_query_cached.clear()
  try:
    mock_client = mock.MagicMock()
    mock_job = mock.MagicMock()
    mock_job.total_bytes_processed = 100
    mock_job.total_bytes_billed = 200
    mock_job.cache_hit = False
    mock_job.to_dataframe.return_value = pd.DataFrame({"col": [1]})
    mock_client.query.return_value = mock_job

    with mock.patch.object(queries, "get_client", return_value=mock_client):
      sql_1 = "SELECT 1"
      filters_1 = models.Filters(agents=("agent-1",))
      project_1 = "proj-1"
      max_bytes_1 = 1_000_000

      # Initial run: live execution (dry run probe + execution query)
      res1 = queries._run_query_cached(sql_1, filters_1, project_1, max_bytes_1)
      assert mock_client.query.call_count == 2
      run_id1 = res1[4]

      # Identical arguments: served from cache (no extra query calls, same run_id)
      res2 = queries._run_query_cached(sql_1, filters_1, project_1, max_bytes_1)
      assert mock_client.query.call_count == 2
      assert res2[4] == run_id1

      # 1. Change sql: results in cache miss / live execution
      sql_2 = "SELECT 2"
      res_sql = queries._run_query_cached(
          sql_2, filters_1, project_1, max_bytes_1
      )
      assert mock_client.query.call_count == 4
      assert res_sql[4] != run_id1

      # 2. Change filters: results in cache miss / live execution
      filters_2 = models.Filters(agents=("agent-2",))
      res_flt = queries._run_query_cached(
          sql_1, filters_2, project_1, max_bytes_1
      )
      assert mock_client.query.call_count == 6
      assert res_flt[4] != run_id1

      # 3. Change project_id: results in cache miss / live execution
      project_2 = "proj-2"
      res_prj = queries._run_query_cached(
          sql_1, filters_1, project_2, max_bytes_1
      )
      assert mock_client.query.call_count == 8
      assert res_prj[4] != run_id1

      # 4. Change maximum_bytes_billed: results in cache miss / live execution
      max_bytes_2 = 2_000_000
      res_bytes = queries._run_query_cached(
          sql_1, filters_1, project_1, max_bytes_2
      )
      assert mock_client.query.call_count == 10
      assert res_bytes[4] != run_id1

      # Calling with identical arguments again is served from cache
      res_cached = queries._run_query_cached(
          sql_1, filters_1, project_1, max_bytes_2
      )
      assert mock_client.query.call_count == 10
      assert res_cached[4] == res_bytes[4]
  finally:
    queries._run_query_cached.clear()


def test_load_filter_options_groupby_fanout():
  """Builds a (kind, value) DataFrame and verifies exact fanout mapping."""
  df = pd.DataFrame(
      [
          {"kind": "agent", "value": "agent-a"},
          {"kind": "agent", "value": "agent-b"},
          {"kind": "event_type", "value": "start"},
          {"kind": "event_type", "value": "complete"},
          {"kind": "session_id", "value": "sess-1"},
          {"kind": "session_id", "value": "sess-2"},
          {"kind": "user_id", "value": "user-1"},
          {"kind": "user_id", "value": "user-2"},
      ]
  )
  query_res = models.QueryResult(
      df=df,
      error=None,
      bytes_processed=50,
      bytes_billed=100,
      cache_hit=False,
  )

  refs = models.TableRefs(
      project="test-project",
      dataset="test_dataset",
      table="events",
      view_prefix="adk_",
  )
  window = models.make_window(models.TIME_RANGES["Last 24 hours"])
  ctx = models.Context(
      refs=refs,
      window=window,
      filters=models.Filters(),
      max_bytes=1_000_000,
      theme=models.LIGHT_THEME,
      price_in=0.0,
      price_out=0.0,
  )

  with mock.patch.object(queries, "fetch", return_value=query_res):
    options, res = queries.load_filter_options(ctx)
    assert options == {
        "agent": ["agent-a", "agent-b"],
        "event_type": ["start", "complete"],
        "session_id": ["sess-1", "sess-2"],
        "user_id": ["user-1", "user-2"],
    }
    assert res is query_res


def test_filter_widget_options_change_preserves_applied_filters():
  """Simulates changed options dictionary between AppTest runs, verifying retention and re-seeding."""
  opts_data = {
      "opts": {
          "agent": ["agent-a", "agent-b"],
          "user_id": ["user-1", "user-2"],
          "event_type": ["start", "complete"],
          "session_id": ["sess-1", "sess-2"],
      }
  }
  query_res = models.QueryResult(
      df=pd.DataFrame(),
      error=None,
      bytes_processed=0,
      bytes_billed=0,
      cache_hit=True,
  )

  def fake_load(ctx):
    return opts_data["opts"], query_res

  with (
      mock.patch.object(queries, "load_filter_options", side_effect=fake_load),
      mock.patch.object(
          app, "load_filter_options", side_effect=fake_load, create=True
      ),
      mock.patch.object(queries, "fetch", return_value=query_res),
      mock.patch.object(app, "fetch", return_value=query_res, create=True),
  ):
    at = _app_test()
    at.run()
    assert not at.exception

    # Select and apply an initial agent filter
    at.sidebar.multiselect(key="flt_agent").select("agent-a")
    apply_btn = [b for b in at.sidebar.button if b.label == "Apply filters"][0]
    apply_btn.click().run()
    assert not at.exception
    assert at.session_state["applied_filters"].agents == ("agent-a",)

    # 1. Simulate BigQuery options omitting the applied value "agent-a"
    opts_data["opts"] = {
        "agent": ["agent-c", "agent-d"],
        "user_id": ["user-1", "user-2"],
        "event_type": ["start", "complete"],
        "session_id": ["sess-1", "sess-2"],
    }
    at.run()
    assert not at.exception

    # applied_filters remains unchanged
    assert at.session_state["applied_filters"].agents == ("agent-a",)
    # The applied value "agent-a" is re-seeded into widget options
    ms_agent = at.sidebar.multiselect(key="flt_agent")
    assert "agent-a" in ms_agent.options
    assert ms_agent.options == ["agent-c", "agent-d", "agent-a"]
    assert ms_agent.value == ["agent-a"]

    # 2. Simulate BigQuery options growing
    opts_data["opts"] = {
        "agent": ["agent-c", "agent-d", "agent-e"],
        "user_id": ["user-1", "user-2"],
        "event_type": ["start", "complete"],
        "session_id": ["sess-1", "sess-2"],
    }
    at.run()
    assert not at.exception

    # applied_filters remains unchanged and new options are available alongside re-seeded applied value
    assert at.session_state["applied_filters"].agents == ("agent-a",)
    ms_agent_grown = at.sidebar.multiselect(key="flt_agent")
    assert "agent-e" in ms_agent_grown.options
    assert "agent-a" in ms_agent_grown.options
    assert ms_agent_grown.value == ["agent-a"]
