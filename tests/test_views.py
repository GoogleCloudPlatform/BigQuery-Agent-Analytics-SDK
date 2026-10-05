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

import logging
import re
from unittest import mock

import pytest

from bigquery_agent_analytics.views import _CROSS_EVENT_VIEW_DEFS
from bigquery_agent_analytics.views import _EVENT_VIEW_DEFS
from bigquery_agent_analytics.views import _OTEL_CORRELATION_COLUMNS
from bigquery_agent_analytics.views import _STANDARD_HEADERS
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
    expected = {**_EVENT_VIEW_DEFS, **_CROSS_EVENT_VIEW_DEFS}
    assert set(created) == set(expected)
    assert vm.bq_client.query.call_count == len(expected)

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


_OTEL_SPAN_COLUMN = "JSON_VALUE(attributes, '$.otel.span_id') AS otel_span_id"
_OTEL_TRACE_COLUMN = (
    "JSON_VALUE(attributes, '$.otel.trace_id') AS otel_trace_id"
)

# Per-event views whose own typed columns read ``attributes``.
_ATTRIBUTE_READERS = frozenset(
    {
        "LLM_REQUEST",
        "LLM_RESPONSE",
        "TOOL_COMPLETED",
        "STATE_DELTA",
        "A2A_INTERACTION",
        "TOOL_PAUSED",
    }
)
_ATTRIBUTE_FREE = sorted(set(_EVENT_VIEW_DEFS) - _ATTRIBUTE_READERS)
_HEADER_ONLY = sorted(
    et for et, (_, extra) in _EVENT_VIEW_DEFS.items() if not extra
)


def _reads(sql, column):
  return re.search(rf"\b{column}\b", sql) is not None


@pytest.fixture
def denied_vm():
  return ViewManager(
      project_id=PROJECT,
      dataset_id=DATASET,
      table_id=TABLE,
      bq_client=mock.MagicMock(),
      denied_columns=("attributes",),
  )


class TestOtelCorrelationColumns:
  """Per-event views project ``attributes.otel`` by default (#312)."""

  @pytest.mark.parametrize("event_type", sorted(_EVENT_VIEW_DEFS))
  def test_every_per_event_view_projects_otel_columns(self, vm, event_type):
    sql = vm.get_view_sql(event_type)
    assert sql.count(_OTEL_SPAN_COLUMN) == 1
    assert sql.count(_OTEL_TRACE_COLUMN) == 1

  @pytest.mark.parametrize("event_type", sorted(_EVENT_VIEW_DEFS))
  def test_otel_columns_sit_between_headers_and_typed_columns(
      self, vm, event_type
  ):
    sql = vm.get_view_sql(event_type)
    _, extra_columns = _EVENT_VIEW_DEFS[event_type]
    expected = f"{_STANDARD_HEADERS},\n{_OTEL_CORRELATION_COLUMNS}"
    if extra_columns:
      expected += f",\n{extra_columns}"
    assert f"SELECT\n{expected}\nFROM " in sql

  @pytest.mark.parametrize("event_type", _HEADER_ONLY)
  def test_header_only_views_have_no_trailing_comma(self, vm, event_type):
    lines = vm.get_view_sql(event_type).split("\n")
    from_idx = next(i for i, line in enumerate(lines) if "FROM" in line)
    assert lines[from_idx - 1] == f"  {_OTEL_TRACE_COLUMN}"

  def test_standard_headers_stay_denylist_safe(self):
    """The shared headers must not read any projectable payload column."""
    for column in ("attributes", "content", "content_parts", "latency_ms"):
      assert not _reads(_STANDARD_HEADERS, column)
    assert "otel" not in _STANDARD_HEADERS
    assert _STANDARD_HEADERS.endswith("is_truncated")

  def test_source_event_id_is_not_a_shared_column(self, vm):
    assert "source_event_id" not in _STANDARD_HEADERS
    assert "source_event_id" not in _OTEL_CORRELATION_COLUMNS
    for event_type in _EVENT_VIEW_DEFS:
      assert vm.get_view_sql(event_type).count("AS source_event_id") <= 1

  def test_cross_event_views_get_no_otel_columns(self, vm):
    for key in _CROSS_EVENT_VIEW_DEFS:
      assert "otel" not in vm.get_view_sql(key)


class TestDeniedColumns:
  """``denied_columns`` mirrors the producer's ``payload_column_denylist``."""

  def test_attribute_readers_match_registry(self):
    readers = {
        et
        for et, (_, extra) in _EVENT_VIEW_DEFS.items()
        if _reads(extra, "attributes")
    }
    assert readers == _ATTRIBUTE_READERS
    # 25 per-event views: 6 read attributes, 19 do not.
    assert len(_ATTRIBUTE_FREE) == 19

  def test_create_all_views_deploys_only_attribute_free_views(
      self, denied_vm, caplog
  ):
    with caplog.at_level(logging.WARNING):
      created = denied_vm.create_all_views()
    assert sorted(created) == _ATTRIBUTE_FREE
    issued = [c.args[0] for c in denied_vm.bq_client.query.call_args_list]
    assert len(issued) == len(_ATTRIBUTE_FREE)
    for sql in issued:
      assert not _reads(sql, "attributes")
      assert "otel_" not in sql
    skipped = {
        r.args[0]
        for r in caplog.records
        if r.levelno == logging.WARNING and r.msg.startswith("Skipping view")
    }
    # compaction_windows reads attributes.adk.app_name, so it is skipped too.
    assert skipped == _ATTRIBUTE_READERS | {"compaction_windows"}

  @pytest.mark.parametrize("event_type", _ATTRIBUTE_FREE)
  def test_denied_sql_only_drops_the_otel_columns(
      self, vm, denied_vm, event_type
  ):
    default_sql = vm.get_view_sql(event_type)
    denied_sql = denied_vm.get_view_sql(event_type)
    assert denied_sql == default_sql.replace(
        f",\n{_OTEL_CORRELATION_COLUMNS}", "", 1
    )
    assert not _reads(denied_sql, "attributes")

  @pytest.mark.parametrize("event_type", sorted(_ATTRIBUTE_READERS))
  def test_create_view_rejects_attribute_readers_before_querying(
      self, denied_vm, event_type
  ):
    with pytest.raises(ValueError, match="reads denied column"):
      denied_vm.create_view(event_type)
    denied_vm.bq_client.query.assert_not_called()

  @pytest.mark.parametrize("event_type", sorted(_ATTRIBUTE_READERS))
  def test_get_view_sql_still_renders_attribute_readers(
      self, denied_vm, event_type
  ):
    sql = denied_vm.get_view_sql(event_type)
    assert f"WHERE event_type = '{event_type}'" in sql
    assert "otel_" not in sql

  def test_create_view_attribute_free_view(self, denied_vm):
    denied_vm.create_view("TOOL_STARTING")
    denied_vm.bq_client.query.assert_called_once()
    sql = denied_vm.bq_client.query.call_args[0][0]
    assert "AS tool_name" in sql
    assert not _reads(sql, "attributes")

  def test_compaction_windows_sql_unchanged(self, vm, denied_vm):
    assert denied_vm.get_view_sql("compaction_windows") == vm.get_view_sql(
        "compaction_windows"
    )
    with pytest.raises(ValueError, match="reads denied column"):
      denied_vm.create_view("compaction_windows")

  @pytest.mark.parametrize("column", ["content", "latency_ms", "content_parts"])
  def test_other_denied_columns_skip_their_readers(self, column):
    vm = ViewManager(
        project_id=PROJECT,
        dataset_id=DATASET,
        bq_client=mock.MagicMock(),
        denied_columns=[column],
    )
    created = vm.create_all_views()
    expected = {
        key
        for key, (_, sql) in {
            **_EVENT_VIEW_DEFS,
            **_CROSS_EVENT_VIEW_DEFS,
        }.items()
        if not _reads(sql, column)
    }
    assert set(created) == expected
    # attributes is still present, so the OTel columns stay.
    for event_type in set(created) & set(_EVENT_VIEW_DEFS):
      assert _OTEL_SPAN_COLUMN in vm.get_view_sql(event_type)

  @pytest.mark.parametrize(
      "column, skipped, kept",
      [
          ("content", ["TOOL_STARTING", "AGENT_TRANSFER"], ["LLM_ERROR"]),
          ("latency_ms", ["LLM_ERROR", "AGENT_COMPLETED"], ["TOOL_STARTING"]),
          ("content_parts", [], sorted(_EVENT_VIEW_DEFS)),
      ],
  )
  def test_denial_examples(self, column, skipped, kept):
    vm = ViewManager(
        project_id=PROJECT,
        dataset_id=DATASET,
        bq_client=mock.MagicMock(),
        denied_columns={column},
    )
    created = vm.create_all_views()
    for event_type in skipped:
      assert event_type not in created
    for event_type in kept:
      assert event_type in created
    assert "USER_MESSAGE_RECEIVED" in created

  def test_default_denies_nothing(self, vm):
    assert vm.denied_columns == frozenset()
    assert set(vm.create_all_views()) == set(_EVENT_VIEW_DEFS) | set(
        _CROSS_EVENT_VIEW_DEFS
    )

  @pytest.mark.parametrize("value", [None, (), []])
  def test_empty_values_deny_nothing(self, value):
    vm = ViewManager(PROJECT, DATASET, denied_columns=value)
    assert vm.denied_columns == frozenset()
    assert _OTEL_SPAN_COLUMN in vm.get_view_sql("USER_MESSAGE_RECEIVED")

  @pytest.mark.parametrize(
      "value",
      [
          ["attributes"],
          ("attributes",),
          {"attributes"},
          frozenset({"attributes"}),
      ],
  )
  def test_accepts_any_collection(self, value):
    vm = ViewManager(PROJECT, DATASET, denied_columns=value)
    assert vm.denied_columns == frozenset({"attributes"})

  def test_denied_columns_is_keyword_only(self):
    with pytest.raises(TypeError):
      ViewManager(PROJECT, DATASET, TABLE, "adk_", None, ("attributes",))

  def test_rejects_bare_string(self):
    with pytest.raises(TypeError, match="not a string"):
      ViewManager(PROJECT, DATASET, denied_columns="attributes")

  @pytest.mark.parametrize(
      "column", ["trace_id", "span_id", "timestamp", "event_id", "Attributes"]
  )
  def test_rejects_non_projectable_columns(self, column):
    with pytest.raises(ValueError, match="projectable payload columns"):
      ViewManager(PROJECT, DATASET, denied_columns=[column])
