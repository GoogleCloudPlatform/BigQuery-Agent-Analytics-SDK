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

"""Tests for cross-event views (#210) and their shared workflow fixture (#218).

``ViewManager`` carries two registries: ``_EVENT_VIEW_DEFS`` (one view per
event type) and ``_CROSS_EVENT_VIEW_DEFS`` (analytical views spanning event
types). The plumbing tests register a fake cross-event definition to prove
the deployment path — Python and CLI — and pin the per-event SQL so the
plumbing cannot change it.

The fixture tests check that ``tests/fixtures/adk2_workflow_events.json``
covers every #218 shape the way the plugin writes it, and the view tests run
each registered view's SQL on it in SQLite.
"""

import collections
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
from tests.cross_event_view_harness import execute_cross_event_view
from tests.cross_event_view_harness import fixture_events
from tests.cross_event_view_harness import load_adk2_workflow_fixture

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


# ------------------------------------------------------------------ #
# Shared ADK 2.0 workflow fixture (#218)                               #
# ------------------------------------------------------------------ #

# Event types the plugin writes from an ADK Event (on_event_callback), so
# their rows carry the Event envelope keys. USER_MESSAGE_RECEIVED and
# TOOL_COMPLETED rows here come from callbacks with no source Event.
_EVENT_ROW_TYPES = frozenset(
    {
        "AGENT_RESPONSE",
        "AGENT_STATE_CHECKPOINT",
        "AGENT_TRANSFER",
        "EVENT_COMPACTION",
        "STATE_DELTA",
        "TOOL_PAUSED",
    }
)
_CALLBACK_ROW_TYPES = frozenset({"USER_MESSAGE_RECEIVED", "TOOL_COMPLETED"})
_EVENT_ENVELOPE_KEYS = ["source_event_id", "node", "branch", "scope"]
_IDENTITY = ("app_name", "user_id", "session_id", "invocation_id")


def _adk(event):
  return (event["attributes"] or {}).get("adk")


def _event_rows(events):
  """Rows written from an ADK Event: they carry a source_event_id."""
  return [e for e in events if "source_event_id" in (_adk(e) or {})]


def _node_run_ids(path):
  """Returns (run_id, parent_run_id) as google-adk's NodeInfo derives them."""
  segments = path.split("/") if path else []

  def leaf_run_id(segs):
    leaf = segs[-1].rsplit("@", 1) if segs else []
    return leaf[1] if len(leaf) == 2 else None

  parent_run_id = leaf_run_id(segments[:-1]) if len(segments) > 1 else None
  return leaf_run_id(segments) or "", parent_run_id


class TestWorkflowFixtureContract:
  """The fixture covers every #218 shape, laid out as the plugin writes it."""

  @pytest.fixture
  def events(self):
    return fixture_events()

  def test_row_ids_are_sequential(self, events):
    ids = [event["_fixture_id"][: len("row-NN")] for event in events]
    assert ids == [f"row-{n:02d}" for n in range(1, len(events) + 1)]

  def test_branch_fanout(self, events):
    branches = {
        _adk(e)["branch"]
        for e in _event_rows(events)
        if e["invocation_id"] == "inv-1"
    }
    assert branches - {None} == {
        "planner.researcher_a",
        "planner.researcher_b",
        "planner.critic",
    }

  def test_chain_of_three_agent_transfers(self, events):
    transfers = sorted(
        (e for e in events if e["event_type"] == "AGENT_TRANSFER"),
        key=lambda e: e["timestamp"],
    )
    assert [
        (e["content"]["from_agent"], e["content"]["to_agent"])
        for e in transfers
    ] == [
        ("coordinator", "planner"),
        ("planner", "executor"),
        ("executor", "verifier"),
    ]
    for transfer in transfers:
      # The plugin writes only these two keys; from_agent is the author.
      assert set(transfer["content"]) == {"from_agent", "to_agent"}
      assert transfer["content"]["from_agent"] == transfer["agent"]

  def test_compaction_has_fractional_second_boundaries(self, events):
    (compaction,) = [e for e in events if e["event_type"] == "EVENT_COMPACTION"]
    start = compaction["content"]["start_timestamp"]
    end = compaction["content"]["end_timestamp"]
    assert not start.is_integer() and not end.is_integer()
    assert 0 < end - start < 0.01

  def test_checkpoint_has_both_inline_shapes(self, events):
    payloads = [
        e["content"]
        for e in events
        if e["event_type"] == "AGENT_STATE_CHECKPOINT"
    ]
    assert payloads == [
        {"agent_state": {"cursor": "page_2", "step": 1}, "end_of_agent": False},
        {"agent_state": None, "end_of_agent": True},
    ]

  def test_long_running_tool_resumes_in_a_later_invocation(self, events):
    (paused,) = [e for e in events if e["event_type"] == "TOOL_PAUSED"]
    (resumed,) = [
        e
        for e in events
        if e["event_type"] == "TOOL_COMPLETED"
        and _adk(e)["function_call_id"] == _adk(paused)["function_call_id"]
    ]
    assert _adk(paused)["pause_kind"] == _adk(resumed)["pause_kind"] == "tool"
    assert resumed["session_id"] == paused["session_id"]
    assert (paused["invocation_id"], resumed["invocation_id"]) == (
        "inv-1",
        "inv-2",
    )
    assert paused["content"] == {
        "tool": "async_bigquery_export",
        "args": {"table": "events"},
    }
    assert set(resumed["content"]) == {"tool", "result"}
    # The plugin does not write pause_orphan yet.
    assert "pause_orphan" not in _adk(resumed)

  def test_synthetic_hitl_orphan_row_is_documented(self):
    fixture = load_adk2_workflow_fixture()
    (orphan,) = [
        e for e in fixture["events"] if (_adk(e) or {}).get("pause_orphan")
    ]
    assert orphan["event_type"] == "TOOL_COMPLETED"
    assert _adk(orphan)["pause_kind"] == "hitl_confirmation"
    assert _adk(orphan)["pause_orphan"] is True
    note = fixture["_synthetic_hitl_orphan_provenance"]
    assert orphan["_fixture_id"] in note
    for text in (note, orphan["_synthetic_provenance"]):
      assert "SYNTHETIC" in text
      assert "#199" in text
      assert "HITL_*_COMPLETED" in text

  def test_every_scope_shape(self, events):
    scopes = [_adk(e)["scope"] for e in _event_rows(events)]
    shapes = {(s["kind"], s["id"]) for s in scopes if s is not None}
    assert shapes == {
        ("node_run", "root@1/child@2"),
        ("node_run", "planner@1"),
        ("function_call", "adk-fc-001"),
        ("function_call", "call_abc123"),
        ("function_call", "toolu_xyz"),
    }
    assert None in scopes
    # The plugin's rule: node_run if the last path segment has "@".
    for kind, scope_id in shapes:
      leaf = scope_id.rsplit("/", 1)[-1]
      assert kind == ("node_run" if "@" in leaf else "function_call")

  def test_one_source_event_writes_two_rows(self, events):
    rows = _event_rows(events)
    counts = collections.Counter(_adk(e)["source_event_id"] for e in rows)
    assert [key for key, n in counts.items() if n > 1] == ["evt-fanout-shared"]
    fanned_out = [
        e["event_type"]
        for e in rows
        if _adk(e)["source_event_id"] == "evt-fanout-shared"
    ]
    assert fanned_out == ["STATE_DELTA", "AGENT_RESPONSE"]

  def test_workflow_node_paths(self, events):
    assert {_adk(e)["node"]["path"] for e in _event_rows(events)} == {
        "",
        "root@1",
        "planner@1",
        "root@1/child@2",
        "root@1/child@2/grandchild@3",
    }

  def test_pre20_and_callback_only_rows(self, events):
    (legacy,) = [e for e in events if not _adk(e)]
    assert legacy["attributes"] == {}
    callback_rows = [
        e["_fixture_id"][: len("row-NN")]
        for e in events
        if _adk(e) and "source_event_id" not in _adk(e)
    ]
    assert callback_rows == ["row-02", "row-15", "row-16"]

  def test_only_event_rows_carry_the_event_envelope(self, events):
    for event in events:
      adk = _adk(event)
      if adk is None:
        continue
      assert adk["schema_version"] == "1"
      assert adk["app_name"] == "team_app"
      present = [key for key in _EVENT_ENVELOPE_KEYS if key in adk]
      if event["event_type"] in _EVENT_ROW_TYPES:
        assert present == _EVENT_ENVELOPE_KEYS, event["_fixture_id"]
      else:
        assert event["event_type"] in _CALLBACK_ROW_TYPES
        assert present == [], event["_fixture_id"]

  def test_node_run_ids_derive_from_path(self, events):
    for event in _event_rows(events):
      node = _adk(event)["node"]
      assert (node["run_id"], node["parent_run_id"]) == _node_run_ids(
          node["path"]
      ), event["_fixture_id"]

  def test_state_delta_payload_is_in_attributes(self, events):
    (state_delta,) = [e for e in events if e["event_type"] == "STATE_DELTA"]
    assert state_delta["content"] is None
    assert state_delta["attributes"]["state_delta"] == {"score": 0.98}


@pytest.mark.parametrize("view_key", sorted(_CROSS_EVENT_VIEW_DEFS))
class TestCrossEventViewsOnWorkflowFixture:
  """Every registered cross-event view runs on the fixture (#218, #219)."""

  def test_rows_have_full_telemetry_identity(self, vm, view_key):
    rows = execute_cross_event_view(vm, view_key, fixture_events())
    assert rows
    for row in rows:
      assert None not in [row[field] for field in _IDENTITY]
      assert row["user_id"] != "user-legacy"

  def test_pre20_and_callback_only_rows_produce_no_rows(self, vm, view_key):
    events = fixture_events("row-01", "row-02", "row-15", "row-16")
    assert execute_cross_event_view(vm, view_key, events) == []
