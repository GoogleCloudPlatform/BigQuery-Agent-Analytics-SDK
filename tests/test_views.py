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

"""Tests for the ViewManager and event-specific view generation."""

from unittest import mock

import pytest

from bigquery_agent_analytics.views import _EVENT_VIEW_DEFS
from bigquery_agent_analytics.views import ViewManager

PROJECT = "test-project"
DATASET = "analytics"
TABLE = "agent_events"


@pytest.fixture
def vm():
  return ViewManager(
      project_id=PROJECT,
      dataset_id=DATASET,
      table_id=TABLE,
      bq_client=mock.MagicMock(),
  )


class TestViewManager:

  def test_available_event_types(self, vm):
    types = vm.available_event_types
    assert "LLM_REQUEST" in types
    assert "LLM_RESPONSE" in types
    assert "LLM_ERROR" in types
    assert "TOOL_STARTING" in types
    assert "TOOL_COMPLETED" in types
    assert "TOOL_ERROR" in types
    assert "USER_MESSAGE_RECEIVED" in types
    assert "AGENT_STARTING" in types
    assert "AGENT_COMPLETED" in types
    assert "INVOCATION_STARTING" in types
    assert "INVOCATION_COMPLETED" in types
    assert "STATE_DELTA" in types
    assert "HITL_CREDENTIAL_REQUEST" in types
    assert "HITL_CONFIRMATION_REQUEST" in types
    assert "HITL_INPUT_REQUEST" in types
    assert "HITL_CREDENTIAL_REQUEST_COMPLETED" in types
    assert "HITL_CONFIRMATION_REQUEST_COMPLETED" in types
    assert "HITL_INPUT_REQUEST_COMPLETED" in types
    assert "A2A_INTERACTION" in types
    assert len(types) == len(_EVENT_VIEW_DEFS)

  def test_get_view_name(self, vm):
    assert vm.get_view_name("LLM_REQUEST") == "adk_llm_requests"
    assert vm.get_view_name("TOOL_STARTING") == "adk_tool_starts"

  def test_get_view_sql_contains_event_filter(self, vm):
    sql = vm.get_view_sql("LLM_REQUEST")
    assert "WHERE event_type = 'LLM_REQUEST'" in sql
    assert "CREATE OR REPLACE VIEW" in sql
    assert f"`{PROJECT}.{DATASET}." in sql

  def test_get_view_sql_has_standard_headers(self, vm):
    sql = vm.get_view_sql("TOOL_STARTING")
    for header in [
        "timestamp",
        "event_type",
        "agent",
        "session_id",
        "invocation_id",
        "span_id",
        "is_truncated",
    ]:
      assert header in sql

  def test_get_view_sql_llm_request_columns(self, vm):
    sql = vm.get_view_sql("LLM_REQUEST")
    assert "model" in sql
    assert "request_content" in sql
    assert "llm_config" in sql

  def test_get_view_sql_tool_starting_columns(self, vm):
    sql = vm.get_view_sql("TOOL_STARTING")
    assert "tool_name" in sql
    assert "tool_origin" in sql
    assert "tool_args" in sql

  def test_get_view_sql_tool_completed_columns(self, vm):
    sql = vm.get_view_sql("TOOL_COMPLETED")
    assert "tool_name" in sql
    assert "tool_result" in sql
    assert "total_ms" in sql

  def test_get_view_sql_llm_response_tokens(self, vm):
    sql = vm.get_view_sql("LLM_RESPONSE")
    assert "usage_prompt_tokens" in sql
    assert "usage_completion_tokens" in sql
    assert "usage_total_tokens" in sql
    assert "ttft_ms" in sql
    assert "model_version" in sql
    assert "usage_metadata" in sql

  def test_get_view_sql_event_type_in_headers(self, vm):
    """Every view includes event_type in the standard headers."""
    sql = vm.get_view_sql("TOOL_STARTING")
    assert "event_type" in sql

  def test_get_view_sql_empty_extra_columns(self, vm):
    """Views with no extra columns produce valid SQL."""
    sql = vm.get_view_sql("USER_MESSAGE_RECEIVED")
    assert "CREATE OR REPLACE VIEW" in sql
    assert "event_type = 'USER_MESSAGE_RECEIVED'" in sql
    # Should NOT have a trailing comma before FROM
    lines = sql.split("\n")
    from_idx = next(i for i, line in enumerate(lines) if "FROM" in line)
    pre_from = lines[from_idx - 1].strip()
    assert not pre_from.endswith(","), f"Trailing comma before FROM: {pre_from}"

  def test_get_view_sql_unknown_event_raises(self, vm):
    with pytest.raises(KeyError, match="Unknown event_type"):
      vm.get_view_sql("NONEXISTENT_TYPE")

  def test_create_view_executes_sql(self, vm):
    vm.create_view("LLM_REQUEST")
    vm.bq_client.query.assert_called_once()
    sql = vm.bq_client.query.call_args[0][0]
    assert "LLM_REQUEST" in sql
    vm.bq_client.query.return_value.result.assert_called_once()

  def test_create_view_labels_job_config_with_views_feature(self, vm):
    vm.create_view("LLM_REQUEST")
    job_config = vm.bq_client.query.call_args.kwargs.get("job_config")
    assert job_config is not None
    assert dict(job_config.labels or {}).get("sdk_feature") == "views"

  def test_vanilla_client_emits_warn_once(self, caplog):
    # PR #25 review: a caller who injects a vanilla bigquery.Client into
    # ViewManager silently loses sdk / sdk_version / sdk_surface
    # defaults, so those jobs disappear from INFORMATION_SCHEMA tracking
    # queries. Mirror Phase 1's warn-once behavior from Client.bq_client.
    import logging

    from google.auth.credentials import AnonymousCredentials
    from google.cloud import bigquery

    vanilla = bigquery.Client(
        project=PROJECT, credentials=AnonymousCredentials()
    )
    vm = ViewManager(project_id=PROJECT, dataset_id=DATASET, bq_client=vanilla)
    with caplog.at_level(logging.WARNING):
      _ = vm.bq_client
      _ = vm.bq_client
      _ = vm.bq_client
    warnings = [
        r
        for r in caplog.records
        if "SDK telemetry labels will not be applied" in r.message
    ]
    assert len(warnings) == 1

  def test_create_all_views(self, vm):
    created = vm.create_all_views()
    assert len(created) == len(_EVENT_VIEW_DEFS)
    assert vm.bq_client.query.call_count == len(_EVENT_VIEW_DEFS)

  def test_create_all_views_handles_errors(self, vm):
    vm.bq_client.query.side_effect = Exception("BQ error")
    created = vm.create_all_views()
    assert len(created) == 0

  def test_custom_prefix(self):
    vm = ViewManager(
        project_id=PROJECT,
        dataset_id=DATASET,
        view_prefix="custom_",
        bq_client=mock.MagicMock(),
    )
    assert vm.get_view_name("LLM_REQUEST") == "custom_llm_requests"
    sql = vm.get_view_sql("LLM_REQUEST")
    assert "custom_llm_requests" in sql

  def test_all_event_defs_produce_valid_sql(self, vm):
    """Every defined event type produces SQL without errors."""
    for event_type in _EVENT_VIEW_DEFS:
      sql = vm.get_view_sql(event_type)
      assert "CREATE OR REPLACE VIEW" in sql
      assert f"event_type = '{event_type}'" in sql


class TestA2AInteractionView:
  """Tests for the A2A_INTERACTION view shape.

  The A2A_INTERACTION view exposes lineage IDs (task / context),
  the request / response payloads, and a typed
  ``receiver_session_id`` column derived via COALESCE so both A2A
  response shapes (task-shaped and ``A2AMessage``-shaped) are
  covered.
  """

  def test_view_name(self, vm):
    assert vm.get_view_name("A2A_INTERACTION") == "adk_a2a_interactions"

  def test_view_sql_columns_present(self, vm):
    """All five typed columns appear in the rendered SQL."""
    sql = vm.get_view_sql("A2A_INTERACTION")
    assert "AS a2a_task_id" in sql
    assert "AS a2a_context_id" in sql
    assert "AS a2a_request" in sql
    assert "AS a2a_response" in sql
    assert "AS receiver_session_id" in sql

  def test_view_sql_event_filter(self, vm):
    sql = vm.get_view_sql("A2A_INTERACTION")
    assert "WHERE event_type = 'A2A_INTERACTION'" in sql

  def test_view_sql_extracts_a2a_metadata_from_attributes(self, vm):
    """task_id / context_id / request / response come from
    ``attributes.a2a_metadata.*`` — the JSON column the BQ AA
    Plugin writes them to.
    """
    sql = vm.get_view_sql("A2A_INTERACTION")
    assert (
        """JSON_VALUE(
    attributes, '$.a2a_metadata."a2a:task_id"'
  )"""
        in sql
    )
    assert (
        """JSON_VALUE(
    attributes, '$.a2a_metadata."a2a:context_id"'
  )"""
        in sql
    )
    assert (
        """JSON_QUERY(
    attributes, '$.a2a_metadata."a2a:request"'
  )"""
        in sql
    )
    assert (
        """JSON_QUERY(
    attributes, '$.a2a_metadata."a2a:response"'
  )"""
        in sql
    )

  def test_view_sql_receiver_session_id_covers_task_response_shape(self, vm):
    """Task-shaped responses carry ``adk_session_id`` at
    ``content.metadata.adk_session_id`` (the BQ AA Plugin uses the
    response's task object as the row content). The first COALESCE
    branch addresses this path.
    """
    sql = vm.get_view_sql("A2A_INTERACTION")
    assert "JSON_VALUE(content, '$.metadata.adk_session_id')" in sql

  def test_view_sql_receiver_session_id_covers_message_response_shape(self, vm):
    """``A2AMessage``-shaped responses (no task wrapper) keep the
    response object under
    ``attributes.a2a_metadata."a2a:response"`` rather than as the
    row content. The second COALESCE branch addresses this path.
    """
    sql = vm.get_view_sql("A2A_INTERACTION")
    assert (
        """JSON_VALUE(
      attributes,
      '$.a2a_metadata."a2a:response".metadata.adk_session_id'
    )"""
        in sql
    )

  def test_view_sql_receiver_session_id_uses_coalesce(self, vm):
    """The two response-shape paths must be combined under one
    ``COALESCE`` so a single typed column always carries the
    receiver session id when it's present in either location.
    """
    sql = vm.get_view_sql("A2A_INTERACTION")
    receiver_block_start = sql.find("COALESCE")
    receiver_block_end = sql.find(") AS receiver_session_id")
    assert (
        receiver_block_start != -1
    ), "COALESCE must wrap the receiver_session_id paths"
    assert (
        receiver_block_end != -1
    ), "receiver_session_id alias must close a COALESCE"
    assert receiver_block_start < receiver_block_end

  def test_view_sql_keeps_standard_headers(self, vm):
    """A2A_INTERACTION view still surfaces the demo-wide identity
    headers — ``session_id`` is what downstream auditor projections
    use as the caller-side join key.
    """
    sql = vm.get_view_sql("A2A_INTERACTION")
    for header in [
        "timestamp",
        "event_type",
        "session_id",
        "invocation_id",
        "span_id",
    ]:
      assert header in sql
