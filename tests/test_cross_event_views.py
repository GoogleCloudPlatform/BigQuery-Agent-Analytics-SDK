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

"""Tests for cross-event view deployment plumbing (#210).

``ViewManager`` carries two registries: ``_EVENT_VIEW_DEFS`` (one view per
event type) and ``_CROSS_EVENT_VIEW_DEFS`` (analytical views spanning event
types). These tests register a fake cross-event definition to prove the
deployment path — Python and CLI — and pin the per-event SQL so the plumbing
cannot change it.
"""

import json
import os
import pathlib
from unittest import mock

import pytest
from typer.testing import CliRunner

from bigquery_agent_analytics import views
from bigquery_agent_analytics.cli import app
from bigquery_agent_analytics.views import _CROSS_EVENT_VIEW_DEFS
from bigquery_agent_analytics.views import _EVENT_VIEW_DEFS
from bigquery_agent_analytics.views import ViewManager

PROJECT = "test-project"
DATASET = "analytics"
TABLE = "agent_events"

_GOLDEN = (
    pathlib.Path(__file__).parent
    / "fixtures"
    / "views"
    / "per_event_views.golden.sql"
)

_FAKE_NAME = "fake_session_rollup"
_FAKE_DEF = (
    "fake_session_rollups",
    """\
SELECT
  s.session_id,
  COUNT(*) AS tool_calls
FROM `{project}.{dataset}.{table}` AS s
JOIN `{project}.{dataset}.{view_prefix}tool_starts` AS t
  USING (session_id)
GROUP BY s.session_id""",
)

runner = CliRunner()


@pytest.fixture
def vm():
  return ViewManager(
      project_id=PROJECT,
      dataset_id=DATASET,
      table_id=TABLE,
      bq_client=mock.MagicMock(),
  )


@pytest.fixture
def fake_cross_event_def():
  with mock.patch.dict(_CROSS_EVENT_VIEW_DEFS, {_FAKE_NAME: _FAKE_DEF}):
    yield _FAKE_NAME


def _render_per_event_snapshot(vm):
  return "".join(f"-- {et}\n{vm.get_view_sql(et)}\n" for et in _EVENT_VIEW_DEFS)


class TestPerEventViewsUnchanged:
  """The second registry must not alter any per-event view."""

  def test_per_event_sql_matches_golden(self, vm):
    """Byte-for-byte snapshot of every per-event CREATE VIEW statement.

    Regenerate after an intentional per-event view change with::

        UPDATE_VIEW_GOLDEN=1 pytest tests/test_cross_event_views.py
    """
    rendered = _render_per_event_snapshot(vm)
    if os.environ.get("UPDATE_VIEW_GOLDEN"):
      _GOLDEN.write_text(rendered)
    assert rendered == _GOLDEN.read_text()

  def test_per_event_sql_unaffected_by_registered_cross_event_view(
      self, vm, fake_cross_event_def
  ):
    assert _render_per_event_snapshot(vm) == _GOLDEN.read_text()

  def test_available_event_types_excludes_cross_event_views(
      self, vm, fake_cross_event_def
  ):
    assert vm.available_event_types == sorted(_EVENT_VIEW_DEFS)


class TestCrossEventRegistry:

  def test_shipped_registry_has_no_key_or_name_collisions(self):
    """Both registries share one result dict and one BigQuery namespace."""
    assert not set(_CROSS_EVENT_VIEW_DEFS) & set(_EVENT_VIEW_DEFS)
    event_suffixes = {suffix for suffix, _ in _EVENT_VIEW_DEFS.values()}
    cross_suffixes = [suffix for suffix, _ in _CROSS_EVENT_VIEW_DEFS.values()]
    assert len(cross_suffixes) == len(set(cross_suffixes))
    assert not set(cross_suffixes) & event_suffixes

  def test_available_cross_event_views(self, vm, fake_cross_event_def):
    assert vm.available_cross_event_views == sorted(_CROSS_EVENT_VIEW_DEFS)
    assert _FAKE_NAME in vm.available_cross_event_views

  def test_view_name_uses_prefix(self, vm, fake_cross_event_def):
    assert vm.get_view_name(_FAKE_NAME) == "adk_fake_session_rollups"

  def test_view_sql(self, vm, fake_cross_event_def):
    assert vm.get_view_sql(_FAKE_NAME) == (
        "CREATE OR REPLACE VIEW"
        " `test-project.analytics.adk_fake_session_rollups` AS\n"
        "SELECT\n"
        "  s.session_id,\n"
        "  COUNT(*) AS tool_calls\n"
        "FROM `test-project.analytics.agent_events` AS s\n"
        "JOIN `test-project.analytics.adk_tool_starts` AS t\n"
        "  USING (session_id)\n"
        "GROUP BY s.session_id\n"
    )

  def test_view_sql_honors_custom_prefix_and_table(self, fake_cross_event_def):
    vm = ViewManager(
        project_id=PROJECT,
        dataset_id=DATASET,
        table_id="my_events",
        view_prefix="custom_",
        bq_client=mock.MagicMock(),
    )
    sql = vm.get_view_sql(_FAKE_NAME)
    assert "`test-project.analytics.custom_fake_session_rollups`" in sql
    assert "`test-project.analytics.my_events`" in sql
    assert "`test-project.analytics.custom_tool_starts`" in sql

  def test_unknown_name_lists_both_registries(self, vm, fake_cross_event_def):
    with pytest.raises(KeyError, match="Unknown event_type") as exc_info:
      vm.get_view_sql("NOT_A_VIEW")
    assert _FAKE_NAME in str(exc_info.value)
    assert "LLM_REQUEST" in str(exc_info.value)

  def test_create_view_accepts_cross_event_name(self, vm, fake_cross_event_def):
    vm.create_view(_FAKE_NAME)
    vm.bq_client.query.assert_called_once()
    sql = vm.bq_client.query.call_args[0][0]
    assert sql == vm.get_view_sql(_FAKE_NAME)

  def test_key_collision_with_event_type_is_rejected(self, vm):
    with mock.patch.dict(_CROSS_EVENT_VIEW_DEFS, {"LLM_REQUEST": _FAKE_DEF}):
      with pytest.raises(ValueError, match="LLM_REQUEST"):
        vm.create_all_views()
    vm.bq_client.query.assert_not_called()

  def test_view_name_collision_with_event_view_is_rejected(self, vm):
    clash = ("llm_requests", _FAKE_DEF[1])
    with mock.patch.dict(_CROSS_EVENT_VIEW_DEFS, {_FAKE_NAME: clash}):
      with pytest.raises(ValueError, match="llm_requests"):
        vm.create_all_views()
    vm.bq_client.query.assert_not_called()


class TestCreateAllViews:

  def test_creates_both_registries_in_one_call(self, vm, fake_cross_event_def):
    created = vm.create_all_views()

    assert created[_FAKE_NAME] == "adk_fake_session_rollups"
    for event_type in _EVENT_VIEW_DEFS:
      assert created[event_type] == vm.get_view_name(event_type)
    expected = {**_EVENT_VIEW_DEFS, **_CROSS_EVENT_VIEW_DEFS}
    assert set(created) == set(expected)
    assert vm.bq_client.query.call_count == len(expected)

  def test_cross_event_views_are_created_after_per_event_views(
      self, vm, fake_cross_event_def
  ):
    """Cross-event SQL may read per-event views, so those deploy first."""
    created = vm.create_all_views()

    expected = [*_EVENT_VIEW_DEFS, *_CROSS_EVENT_VIEW_DEFS]
    assert list(created) == expected
    issued = [call[0][0] for call in vm.bq_client.query.call_args_list]
    assert issued == [vm.get_view_sql(key) for key in expected]

  def test_cross_event_failure_does_not_drop_per_event_views(
      self, vm, fake_cross_event_def
  ):
    def _query(sql, **kwargs):
      if "fake_session_rollups" in sql:
        raise RuntimeError("BQ error")
      return mock.MagicMock()

    vm.bq_client.query.side_effect = _query
    created = vm.create_all_views()

    assert _FAKE_NAME not in created
    expected = set(_EVENT_VIEW_DEFS) | set(_CROSS_EVENT_VIEW_DEFS)
    assert set(created) == expected - {_FAKE_NAME}

  def test_empty_cross_event_registry_matches_per_event_only(self, vm):
    with mock.patch.dict(_CROSS_EVENT_VIEW_DEFS, clear=True):
      created = vm.create_all_views()
    assert list(created) == list(_EVENT_VIEW_DEFS)
    assert vm.bq_client.query.call_count == len(_EVENT_VIEW_DEFS)


class TestCli:
  """``views`` CLI deploys the combined set through the real ViewManager."""

  @pytest.fixture
  def bq_client(self):
    client = mock.MagicMock()
    with mock.patch.object(views, "make_bq_client", return_value=client):
      yield client

  def test_views_create_all_deploys_cross_event_view(
      self, bq_client, fake_cross_event_def
  ):
    result = runner.invoke(
        app,
        ["views", "create-all", "--project-id=proj", "--dataset-id=ds"],
    )

    assert result.exit_code == 0, result.output
    parsed = json.loads(result.output)
    assert parsed[_FAKE_NAME] == "adk_fake_session_rollups"
    assert parsed["LLM_REQUEST"] == "adk_llm_requests"
    expected = {**_EVENT_VIEW_DEFS, **_CROSS_EVENT_VIEW_DEFS}
    assert set(parsed) == set(expected)
    issued = [call[0][0] for call in bq_client.query.call_args_list]
    assert len(issued) == len(expected)
    assert "`proj.ds.adk_fake_session_rollups`" in issued[-1]

  def test_views_create_single_cross_event_view(
      self, bq_client, fake_cross_event_def
  ):
    result = runner.invoke(
        app,
        ["views", "create", _FAKE_NAME, "--project-id=proj", "--dataset-id=ds"],
    )

    assert result.exit_code == 0, result.output
    bq_client.query.assert_called_once()
    assert (
        "`proj.ds.adk_fake_session_rollups`" in bq_client.query.call_args[0][0]
    )
