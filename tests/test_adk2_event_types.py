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

"""ADK 2.0 event-type registration across every SDK type surface (#211).

The producer (BQ AA Plugin, #293) emits four new event types plus two
workflow-boundary types. These tests assert each type is recognized by every
consumer surface — the ``EventType`` enum, ``event_semantics`` families, the
trace-evaluator allowlist, the UDF label map, and ``_EVENT_VIEW_DEFS`` — and
that the typed-view column SQL matches the keys the producer actually writes.
"""

from __future__ import annotations

from unittest import mock

import pytest

from bigquery_agent_analytics.event_semantics import ALL_KNOWN_EVENT_TYPES
from bigquery_agent_analytics.event_semantics import is_tool_event
from bigquery_agent_analytics.trace import EventType
from bigquery_agent_analytics.trace_evaluator import BigQueryTraceEvaluator
from bigquery_agent_analytics.udf_kernels import normalize_event_label
from bigquery_agent_analytics.views import _EVENT_VIEW_DEFS
from bigquery_agent_analytics.views import ViewManager

# The four types the #293 producer cut ships with full typed-view columns.
SHIPPED_TYPES = (
    "AGENT_TRANSFER",
    "EVENT_COMPACTION",
    "AGENT_STATE_CHECKPOINT",
    "TOOL_PAUSED",
)
# Workflow boundaries — registered now, typed columns deferred to #207.
WORKFLOW_TYPES = ("WORKFLOW_NODE_STARTING", "WORKFLOW_NODE_COMPLETED")
NEW_TYPES = SHIPPED_TYPES + WORKFLOW_TYPES


@pytest.fixture
def vm():
  return ViewManager(
      project_id="test-project",
      dataset_id="analytics",
      table_id="agent_events",
      bq_client=mock.MagicMock(),
  )


# ------------------------------------------------------------------ #
# A. Cross-surface registration                                       #
# ------------------------------------------------------------------ #


@pytest.mark.parametrize("event_type", NEW_TYPES)
def test_event_type_enum_has_new_type(event_type):
  assert EventType(event_type).value == event_type


@pytest.mark.parametrize("event_type", NEW_TYPES)
def test_event_semantics_knows_new_type(event_type):
  assert event_type in ALL_KNOWN_EVENT_TYPES


@pytest.mark.parametrize("event_type", NEW_TYPES)
def test_trace_evaluator_default_allowlist_has_new_type(event_type):
  assert event_type in BigQueryTraceEvaluator._DEFAULT_EVENT_TYPES


@pytest.mark.parametrize("event_type", NEW_TYPES)
def test_udf_label_map_recognizes_new_type(event_type):
  # Every new type maps to a real category, never the "other" fallback.
  assert normalize_event_label(event_type) != "other"


@pytest.mark.parametrize("event_type", NEW_TYPES)
def test_view_def_registered_for_new_type(vm, event_type):
  assert event_type in _EVENT_VIEW_DEFS
  assert event_type in vm.available_event_types
  # SQL builds and carries the event filter for every registered type.
  sql = vm.get_view_sql(event_type)
  assert f"WHERE event_type = '{event_type}'" in sql


def test_no_new_type_falls_through_any_surface():
  # One assertion that the whole set is wired everywhere — guards against
  # a future type being added to one surface but missed on another.
  for event_type in NEW_TYPES:
    assert EventType(event_type)
    assert event_type in ALL_KNOWN_EVENT_TYPES
    assert event_type in BigQueryTraceEvaluator._DEFAULT_EVENT_TYPES
    assert normalize_event_label(event_type) != "other"
    assert event_type in _EVENT_VIEW_DEFS


# ------------------------------------------------------------------ #
# B. Semantics                                                        #
# ------------------------------------------------------------------ #


def test_tool_paused_is_a_tool_event():
  assert is_tool_event("TOOL_PAUSED") is True


def test_label_categories():
  assert normalize_event_label("AGENT_TRANSFER") == "agent"
  assert normalize_event_label("AGENT_STATE_CHECKPOINT") == "agent"
  assert normalize_event_label("TOOL_PAUSED") == "tool"
  assert normalize_event_label("EVENT_COMPACTION") == "compaction"
  assert normalize_event_label("WORKFLOW_NODE_STARTING") == "workflow"
  assert normalize_event_label("WORKFLOW_NODE_COMPLETED") == "workflow"


# ------------------------------------------------------------------ #
# C. Typed-view columns match the producer-emitted keys               #
# ------------------------------------------------------------------ #


def test_agent_transfer_columns(vm):
  sql = vm.get_view_sql("AGENT_TRANSFER")
  assert "JSON_VALUE(content, '$.from_agent') AS from_agent" in sql
  assert "JSON_VALUE(content, '$.to_agent') AS to_agent" in sql


def test_event_compaction_columns_preserve_fractional_epoch(vm):
  sql = vm.get_view_sql("EVENT_COMPACTION")
  assert "AS start_timestamp" in sql
  assert "AS end_timestamp" in sql
  assert "compacted_content" in sql
  # Fractional float-epoch precision preserved via micros, not a bare cast.
  assert "TIMESTAMP_MICROS" in sql
  assert "$.start_timestamp" in sql


def test_agent_state_checkpoint_columns_inline(vm):
  sql = vm.get_view_sql("AGENT_STATE_CHECKPOINT")
  assert (
      "CAST(JSON_VALUE(content, '$.end_of_agent') AS BOOL) AS end_of_agent"
      in sql
  )
  assert "JSON_QUERY(content, '$.agent_state') AS agent_state" in sql
  # Presence discriminator preserves missing-vs-explicit-JSON-null
  # (mirrors the producer's own view).
  assert (
      "JSON_TYPE(JSON_QUERY(content, '$.agent_state')) AS agent_state_type"
      in sql
  )
  # Offload columns are a #208 follow-up — must not be present yet.
  assert "agent_state_uri" not in sql
  assert "agent_state_sha256" not in sql


def test_tool_paused_columns(vm):
  sql = vm.get_view_sql("TOOL_PAUSED")
  assert (
      "JSON_VALUE(attributes, '$.adk.function_call_id') AS function_call_id"
      in sql
  )
  assert "JSON_VALUE(attributes, '$.adk.pause_kind') AS pause_kind" in sql
  # pause_orphan is a TOOL_COMPLETED field, not a TOOL_PAUSED field.
  assert "pause_orphan" not in sql


@pytest.mark.parametrize("event_type", WORKFLOW_TYPES)
def test_workflow_nodes_are_base_header_only(vm, event_type):
  # Typed columns are blocked on #207; the view is header-only for now.
  suffix, extra_columns = _EVENT_VIEW_DEFS[event_type]
  assert extra_columns == ""
  sql = vm.get_view_sql(event_type)
  assert "event_type" in sql  # standard headers still present


# ------------------------------------------------------------------ #
# D. TOOL_COMPLETED extended with long-running pair keys (#199/#293)   #
# ------------------------------------------------------------------ #


def test_tool_completed_exposes_long_running_columns(vm):
  sql = vm.get_view_sql("TOOL_COMPLETED")
  # Pre-existing columns still present.
  assert "AS tool_name" in sql
  assert "AS total_ms" in sql
  # New ADK long-running pair keys, typed.
  assert (
      "JSON_VALUE(attributes, '$.adk.function_call_id') AS function_call_id"
      in sql
  )
  assert "JSON_VALUE(attributes, '$.adk.pause_kind') AS pause_kind" in sql
  assert (
      "CAST(JSON_VALUE(attributes, '$.adk.pause_orphan') AS BOOL) AS pause_orphan"
      in sql
  )
