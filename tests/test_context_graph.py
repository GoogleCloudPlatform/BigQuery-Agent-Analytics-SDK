# Copyright 2025 Google LLC
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

"""Tests for the context_graph module."""

from datetime import datetime
from datetime import timezone
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest

from bigquery_agent_analytics.context_graph import BizNode
from bigquery_agent_analytics.context_graph import Candidate
from bigquery_agent_analytics.context_graph import ContextGraphConfig
from bigquery_agent_analytics.context_graph import ContextGraphManager
from bigquery_agent_analytics.context_graph import DecisionPoint
from bigquery_agent_analytics.context_graph import WorldChangeAlert
from bigquery_agent_analytics.context_graph import WorldChangeReport

# ------------------------------------------------------------------ #
# Data Model Tests                                                     #
# ------------------------------------------------------------------ #


class TestBizNode:
  """Tests for BizNode dataclass."""

  def test_creation(self):
    node = BizNode(
        span_id="span-1",
        session_id="sess-1",
        node_type="Product",
        node_value="Yahoo Homepage",
    )
    assert node.node_type == "Product"
    assert node.node_value == "Yahoo Homepage"
    assert node.confidence == 1.0
    assert node.metadata == {}

  def test_with_confidence(self):
    node = BizNode(
        span_id="span-1",
        session_id="sess-1",
        node_type="Targeting",
        node_value="Millennials",
        confidence=0.92,
        metadata={"source": "brief"},
    )
    assert node.confidence == 0.92
    assert node.metadata["source"] == "brief"


class TestWorldChangeAlert:
  """Tests for WorldChangeAlert model."""

  def test_creation(self):
    alert = WorldChangeAlert(
        biz_node="Yahoo Homepage",
        original_state="Product: Yahoo Homepage",
        current_state="unavailable",
        drift_type="inventory_depleted",
        severity=0.9,
    )
    assert alert.biz_node == "Yahoo Homepage"
    assert alert.drift_type == "inventory_depleted"
    assert alert.severity == 0.9
    assert alert.recommendation == "Review before approving."


class TestWorldChangeReport:
  """Tests for WorldChangeReport model."""

  def test_safe_report(self):
    report = WorldChangeReport(
        session_id="sess-1",
        total_entities_checked=5,
        stale_entities=0,
        is_safe_to_approve=True,
    )
    assert report.is_safe_to_approve
    assert report.stale_entities == 0
    assert "Safe to approve  : True" in report.summary()

  def test_unsafe_report(self):
    alert = WorldChangeAlert(
        biz_node="Yahoo Homepage",
        original_state="Product: Yahoo Homepage",
        current_state="sold_out",
        drift_type="inventory_depleted",
        severity=0.95,
    )
    report = WorldChangeReport(
        session_id="sess-1",
        alerts=[alert],
        total_entities_checked=3,
        stale_entities=1,
        is_safe_to_approve=False,
    )
    assert not report.is_safe_to_approve
    assert report.stale_entities == 1
    summary = report.summary()
    assert "inventory_depleted" in summary
    assert "Yahoo Homepage" in summary

  def test_summary_format(self):
    report = WorldChangeReport(
        session_id="sess-42",
        total_entities_checked=10,
        stale_entities=2,
        is_safe_to_approve=False,
        alerts=[
            WorldChangeAlert(
                biz_node="Product A",
                original_state="available",
                current_state="depleted",
                drift_type="unavailable",
                severity=0.8,
            ),
            WorldChangeAlert(
                biz_node="Product B",
                original_state="$50",
                current_state="$75",
                drift_type="price_changed",
                severity=0.6,
            ),
        ],
    )
    summary = report.summary()
    assert "sess-42" in summary
    assert "Entities checked : 10" in summary
    assert "Stale entities   : 2" in summary
    assert "Product A" in summary
    assert "Product B" in summary


class TestContextGraphConfig:
  """Tests for ContextGraphConfig model."""

  def test_defaults(self):
    config = ContextGraphConfig()
    assert config.biz_nodes_table == "extracted_biz_nodes"
    assert config.cross_links_table == "context_cross_links"
    assert config.graph_name == "agent_context_graph"
    assert config.max_hops == 20
    assert "Product" in config.entity_types

  def test_custom_config(self):
    config = ContextGraphConfig(
        graph_name="adcp_graph",
        entity_types=["Ad", "Inventory"],
        max_hops=10,
    )
    assert config.graph_name == "adcp_graph"
    assert config.entity_types == ["Ad", "Inventory"]
    assert config.max_hops == 10


# ------------------------------------------------------------------ #
# ContextGraphManager Tests                                            #
# ------------------------------------------------------------------ #


class TestContextGraphManager:
  """Tests for ContextGraphManager."""

  def _make_manager(self, mock_client=None):
    return ContextGraphManager(
        project_id="test-project",
        dataset_id="test_dataset",
        table_id="agent_events",
        client=mock_client or MagicMock(),
    )

  def test_resolve_endpoint_short_name(self):
    mgr = self._make_manager()
    ep = mgr._resolve_endpoint()
    assert ep == (
        "https://aiplatform.googleapis.com/v1/projects/"
        "test-project/locations/global/publishers/google/"
        "models/gemini-2.5-flash"
    )

  def test_resolve_endpoint_full_url(self):
    mgr = self._make_manager()
    mgr.config = ContextGraphConfig(
        endpoint="https://aiplatform.googleapis.com/v1/projects/p/locations/global/publishers/google/models/gemini-3-flash-preview"
    )
    ep = mgr._resolve_endpoint()
    assert ep.startswith("https://")
    assert "gemini-3-flash-preview" in ep

  def test_resolve_endpoint_rejects_legacy_ref(self):
    mgr = self._make_manager()
    mgr.config = ContextGraphConfig(endpoint="my-project.my_dataset.my_model")
    with pytest.raises(ValueError, match="Legacy BQ ML"):
      mgr._resolve_endpoint()

  def test_get_property_graph_ddl(self):
    mgr = self._make_manager()
    ddl = mgr.get_property_graph_ddl()
    assert "CREATE OR REPLACE PROPERTY GRAPH" in ddl
    assert "test-project" in ddl
    assert "test_dataset" in ddl
    assert "agent_events" in ddl
    assert "TechNode" in ddl
    assert "BizNode" in ddl
    assert "Caused" in ddl
    assert "Evaluated" in ddl
    # P1: composite keys
    assert "KEY (biz_node_id)" in ddl
    assert "KEY (link_id)" in ddl

  def test_get_property_graph_ddl_custom_name(self):
    mgr = self._make_manager()
    ddl = mgr.get_property_graph_ddl(graph_name="my_graph")
    assert "my_graph" in ddl

  def test_get_reasoning_chain_gql(self):
    mgr = self._make_manager()
    gql = mgr.get_reasoning_chain_gql(
        decision_event_type="HITL_CONFIRMATION_REQUEST_COMPLETED",
        biz_entity="Yahoo Homepage",
    )
    assert "GRAPH" in gql
    assert "MATCH" in gql
    assert "Caused" in gql
    assert "Evaluated" in gql
    # P2: biz_entity is parameterized, not interpolated
    assert "@biz_entity" in gql
    assert "Yahoo Homepage" not in gql

  def test_get_reasoning_chain_gql_no_entity(self):
    mgr = self._make_manager()
    gql = mgr.get_reasoning_chain_gql(
        decision_event_type="AGENT_COMPLETED",
    )
    assert "GRAPH" in gql
    assert "@biz_entity" not in gql

  def test_get_causal_chain_gql(self):
    mgr = self._make_manager()
    gql = mgr.get_causal_chain_gql(session_id="sess-1")
    assert "GRAPH" in gql
    assert "MATCH" in gql
    assert "USER_MESSAGE_RECEIVED" in gql

  def test_create_property_graph_success(self):
    mock_client = MagicMock()
    mock_job = MagicMock()
    mock_client.query.return_value = mock_job
    mgr = self._make_manager(mock_client)

    result = mgr.create_property_graph()
    assert result is True
    mock_client.query.assert_called_once()
    mock_job.result.assert_called_once()

  def test_create_property_graph_failure(self):
    mock_client = MagicMock()
    mock_client.query.side_effect = Exception("BigQuery error")
    mgr = self._make_manager(mock_client)

    result = mgr.create_property_graph()
    assert result is False

  def test_store_biz_nodes_empty(self):
    mgr = self._make_manager()
    assert mgr.store_biz_nodes([]) is True

  def test_store_biz_nodes_success(self):
    mock_client = MagicMock()
    mock_query_job = MagicMock()
    mock_client.query.return_value = mock_query_job
    mock_load_job = MagicMock()
    mock_client.load_table_from_json.return_value = mock_load_job
    mgr = self._make_manager(mock_client)

    nodes = [
        BizNode(
            span_id="s1",
            session_id="sess-1",
            node_type="Product",
            node_value="Homepage",
        ),
    ]
    result = mgr.store_biz_nodes(nodes)
    assert result is True
    mock_client.load_table_from_json.assert_called_once()
    # Streaming insert path must NOT be used — that's the buffered
    # write path that breaks rerun idempotency.
    mock_client.insert_rows_json.assert_not_called()

  def test_store_biz_nodes_insert_error(self):
    mock_client = MagicMock()
    mock_query_job = MagicMock()
    mock_client.query.return_value = mock_query_job
    # Load job raises — surfaces as False from store_biz_nodes.
    mock_client.load_table_from_json.side_effect = Exception("load failed")
    mgr = self._make_manager(mock_client)

    nodes = [
        BizNode(
            span_id="s1",
            session_id="sess-1",
            node_type="Product",
            node_value="Homepage",
        ),
    ]
    result = mgr.store_biz_nodes(nodes)
    assert result is False

  def test_store_biz_nodes_dedupes_by_biz_node_id_and_uses_load_job(self):
    """The BizNode KEY in the property graph is biz_node_id, so the
    backing table must have at most one row per id. ``store_biz_nodes``
    dedupes (last-wins) before insert AND writes via a load job, not
    via the streaming-insert API.

    The streaming insert path was the source of the duplicate-key
    rerun bug (PR #99 follow-up): its buffer is invisible to DML
    DELETE for ~30 minutes, so a re-run within that window would
    add duplicates against rows the DELETE could not evict. Load
    jobs write to managed storage, so the DELETE in the same Python
    session sees the rows.
    """
    mock_client = MagicMock()
    mock_query_job = MagicMock()
    mock_client.query.return_value = mock_query_job
    mock_load_job = MagicMock()
    mock_client.load_table_from_json.return_value = mock_load_job
    mgr = self._make_manager(mock_client)

    nodes = [
        BizNode(
            span_id="s1",
            session_id="sess-1",
            node_type="Product",
            node_value="Homepage",
            confidence=0.5,
        ),
        BizNode(
            span_id="s1",
            session_id="sess-1",
            node_type="Product",
            node_value="Homepage",
            confidence=0.9,  # later — should win
        ),
        BizNode(
            span_id="s2",
            session_id="sess-1",
            node_type="Product",
            node_value="Other",
        ),
    ]
    assert mgr.store_biz_nodes(nodes) is True
    mock_client.insert_rows_json.assert_not_called()
    args, _ = mock_client.load_table_from_json.call_args
    loaded_rows = args[0]
    biz_ids = [r["biz_node_id"] for r in loaded_rows]
    assert len(biz_ids) == len(
        set(biz_ids)
    ), f"load_table_from_json received duplicate biz_node_id keys: {biz_ids}"
    dup_row = next(
        r for r in loaded_rows if r["biz_node_id"] == "s1:Product:Homepage"
    )
    assert dup_row["confidence"] == 0.9

  def test_store_biz_nodes_is_rerun_idempotent_via_session_delete(self):
    """Calling store_biz_nodes twice with the same nodes must not
    duplicate biz_node_id rows.

    The append-only load-job path is not idempotent on its own —
    the BizNode KEY (biz_node_id) graph contract would be violated.
    store_biz_nodes() therefore issues a DELETE FROM ... WHERE
    session_id IN UNNEST(@session_ids) before the load, so the
    second call evicts the first call's rows before appending.
    """
    mock_client = MagicMock()
    mock_query_job = MagicMock()
    mock_client.query.return_value = mock_query_job
    mock_load_job = MagicMock()
    mock_client.load_table_from_json.return_value = mock_load_job
    mgr = self._make_manager(mock_client)

    nodes = [
        BizNode(
            span_id="s1",
            session_id="sess-1",
            node_type="Product",
            node_value="Homepage",
        ),
    ]
    assert mgr.store_biz_nodes(nodes) is True
    # Capture all query() calls — the per-session DELETE is one of
    # them (the other being the table-create DDL).
    delete_calls_run_1 = [
        c
        for c in mock_client.query.call_args_list
        if "DELETE FROM" in c[0][0]
        and self._make_manager().config.biz_nodes_table in c[0][0]
    ]
    assert len(delete_calls_run_1) == 1, (
        "store_biz_nodes should issue exactly one biz_nodes DELETE"
        " before its load on each call; got"
        f" {len(delete_calls_run_1)}"
    )

    # Re-invoke. The second call must also DELETE before load.
    assert mgr.store_biz_nodes(nodes) is True
    delete_calls_total = [
        c
        for c in mock_client.query.call_args_list
        if "DELETE FROM" in c[0][0]
        and self._make_manager().config.biz_nodes_table in c[0][0]
    ]
    assert len(delete_calls_total) == 2, (
        "second store_biz_nodes call must also DELETE for idempotency;"
        f" total observed: {len(delete_calls_total)}"
    )
    # Both load-job calls must have happened — but both with the
    # same single deduped row (the second one repeats the first).
    assert mock_client.load_table_from_json.call_count == 2

  def test_detect_world_changes_no_drift(self):
    mock_client = MagicMock()
    mock_job = MagicMock()
    mock_job.result.return_value = []
    mock_client.query.return_value = mock_job
    mgr = self._make_manager(mock_client)

    report = mgr.detect_world_changes(session_id="sess-1")
    assert report.is_safe_to_approve
    assert report.stale_entities == 0
    assert len(report.alerts) == 0

  def test_detect_world_changes_with_drift(self):
    mock_client = MagicMock()
    mock_job = MagicMock()
    # Simulate returned biz nodes with evaluated_at timestamps
    mock_job.result.return_value = [
        {
            "span_id": "s1",
            "node_type": "Product",
            "node_value": "Yahoo Homepage",
            "confidence": 0.95,
            "evaluated_at": datetime(2025, 6, 1, 12, 0, tzinfo=timezone.utc),
        },
        {
            "span_id": "s2",
            "node_type": "Targeting",
            "node_value": "Millennials",
            "confidence": 0.90,
            "evaluated_at": datetime(2025, 6, 1, 12, 1, tzinfo=timezone.utc),
        },
    ]
    mock_client.query.return_value = mock_job
    mgr = self._make_manager(mock_client)

    def check_state(node):
      # Verify evaluated_at timestamp is passed through
      assert node.evaluated_at is not None
      if node.node_value == "Yahoo Homepage":
        return {
            "available": False,
            "current_value": "sold_out",
            "drift_type": "inventory_depleted",
            "severity": 0.95,
        }
      return {"available": True, "current_value": node.node_value}

    report = mgr.detect_world_changes(
        session_id="sess-1",
        current_state_fn=check_state,
    )
    assert not report.is_safe_to_approve
    assert report.stale_entities == 1
    assert len(report.alerts) == 1
    assert report.alerts[0].biz_node == "Yahoo Homepage"
    assert report.alerts[0].drift_type == "inventory_depleted"

  def test_detect_world_changes_fn_exception(self):
    mock_client = MagicMock()
    mock_job = MagicMock()
    mock_job.result.return_value = [
        {
            "span_id": "s1",
            "node_type": "Product",
            "node_value": "Test",
            "confidence": 1.0,
            "evaluated_at": datetime(2025, 6, 1, 12, 0, tzinfo=timezone.utc),
        },
    ]
    mock_client.query.return_value = mock_job
    mgr = self._make_manager(mock_client)

    def bad_fn(node):
      raise RuntimeError("API failure")

    report = mgr.detect_world_changes(
        session_id="sess-1",
        current_state_fn=bad_fn,
    )
    # Fail-closed: callback failure → not safe to approve
    assert not report.is_safe_to_approve
    assert report.check_failed is True

  def test_detect_world_changes_query_failure_is_fail_closed(self):
    mock_client = MagicMock()
    mock_client.query.side_effect = Exception("BigQuery unavailable")
    mgr = self._make_manager(mock_client)

    report = mgr.detect_world_changes(session_id="sess-1")
    assert not report.is_safe_to_approve
    assert report.check_failed is True
    assert "CHECK FAILED" in report.summary()

  def test_create_cross_links_success(self):
    mock_client = MagicMock()
    mock_job = MagicMock()
    mock_client.query.return_value = mock_job
    mgr = self._make_manager(mock_client)

    result = mgr.create_cross_links(["sess-1"])
    assert result is True
    # create table + delete old links + insert new links
    assert mock_client.query.call_count == 3

  def test_create_cross_links_failure(self):
    mock_client = MagicMock()
    mock_client.query.side_effect = Exception("fail")
    mgr = self._make_manager(mock_client)

    result = mgr.create_cross_links(["sess-1"])
    assert result is False

  def test_build_context_graph(self):
    mock_client = MagicMock()
    mock_job = MagicMock()
    mock_job.result.return_value = []
    mock_client.query.return_value = mock_job
    mgr = self._make_manager(mock_client)

    results = mgr.build_context_graph(
        session_ids=["sess-1"],
        use_ai_generate=False,
    )
    assert "biz_nodes_count" in results
    assert "cross_links_created" in results
    assert "property_graph_created" in results

  def test_explain_decision_failure(self):
    mock_client = MagicMock()
    mock_client.query.side_effect = Exception("GQL error")
    mgr = self._make_manager(mock_client)

    result = mgr.explain_decision(
        biz_entity="Yahoo Homepage",
    )
    assert result == []

  def test_traverse_causal_chain_failure(self):
    mock_client = MagicMock()
    mock_client.query.side_effect = Exception("GQL error")
    mgr = self._make_manager(mock_client)

    result = mgr.traverse_causal_chain(session_id="sess-1")
    assert result == []

  def test_extract_query_uses_prompt_only_extraction(self):
    """Biz-node extraction relies on prompt-shaped JSON output, not on
    AI.GENERATE's ``output_schema`` parameter.

    Background: the current BigQuery AI.GENERATE parser rejects the
    JSON-Schema strings the SDK previously passed via ``output_schema``
    (it now expects a SQL-style column list like ``'foo STRING'``).
    The SDK instead asks the model in-prompt to return a JSON array
    and parses the response with markdown-fence stripping +
    ``JSON_EXTRACT_ARRAY``.
    """
    self._make_manager()
    from bigquery_agent_analytics.context_graph import _EXTRACT_BIZ_NODES_QUERY

    # No output_schema kwarg in the AI.GENERATE call.
    assert "output_schema =>" not in _EXTRACT_BIZ_NODES_QUERY
    # Prompt enumerates the field contract.
    assert "entity_type" in _EXTRACT_BIZ_NODES_QUERY
    assert "entity_value" in _EXTRACT_BIZ_NODES_QUERY
    assert "confidence" in _EXTRACT_BIZ_NODES_QUERY
    # Markdown-fence stripping + JSON_EXTRACT_ARRAY parse pipeline.
    assert "JSON_EXTRACT_ARRAY" in _EXTRACT_BIZ_NODES_QUERY
    assert "REGEXP_REPLACE" in _EXTRACT_BIZ_NODES_QUERY

  def test_property_graph_ddl_has_artifact_uri(self):
    mgr = self._make_manager()
    ddl = mgr.get_property_graph_ddl()
    assert "artifact_uri" in ddl

  def test_property_graph_ddl_evaluated_has_properties(self):
    mgr = self._make_manager()
    ddl = mgr.get_property_graph_ddl()
    assert "link_type" in ddl
    assert "created_at" in ddl

  def test_reconstruct_trace_gql_success(self):
    mock_client = MagicMock()
    mock_job = MagicMock()
    mock_job.result.return_value = [
        {
            "parent_span_id": "s1",
            "parent_event_type": "USER_MESSAGE_RECEIVED",
            "parent_agent": "root",
            "parent_timestamp": datetime(
                2025, 6, 1, 12, 0, tzinfo=timezone.utc
            ),
            "session_id": "sess-1",
            "parent_invocation_id": "inv-1",
            "parent_content": {},
            "parent_latency_ms": None,
            "parent_status": "OK",
            "parent_error_message": None,
            "child_span_id": "s2",
            "child_event_type": "LLM_REQUEST",
            "child_agent": "root",
            "child_timestamp": datetime(2025, 6, 1, 12, 1, tzinfo=timezone.utc),
            "child_invocation_id": "inv-1",
            "child_content": {},
            "child_latency_ms": 500,
            "child_status": "OK",
            "child_error_message": None,
        },
    ]
    mock_client.query.return_value = mock_job
    mgr = self._make_manager(mock_client)

    rows = mgr.reconstruct_trace_gql(session_id="sess-1")
    assert len(rows) == 1
    assert rows[0]["parent_span_id"] == "s1"
    assert rows[0]["child_span_id"] == "s2"

  def test_reconstruct_trace_gql_failure(self):
    mock_client = MagicMock()
    mock_client.query.side_effect = Exception("GQL error")
    mgr = self._make_manager(mock_client)

    result = mgr.reconstruct_trace_gql(session_id="sess-1")
    assert result == []

  def test_biz_node_has_evaluated_at_and_artifact_uri(self):
    node = BizNode(
        span_id="s1",
        session_id="sess-1",
        node_type="Product",
        node_value="Yahoo Homepage",
        evaluated_at=datetime(2025, 6, 1, 12, 0, tzinfo=timezone.utc),
        artifact_uri="gs://bucket/path/file.json",
    )
    assert node.evaluated_at is not None
    assert node.artifact_uri == "gs://bucket/path/file.json"

  def test_detect_world_changes_passes_evaluated_at(self):
    mock_client = MagicMock()
    mock_job = MagicMock()
    eval_time = datetime(2025, 6, 1, 12, 0, tzinfo=timezone.utc)
    mock_job.result.return_value = [
        {
            "span_id": "s1",
            "node_type": "Product",
            "node_value": "Test",
            "confidence": 1.0,
            "evaluated_at": eval_time,
        },
    ]
    mock_client.query.return_value = mock_job
    mgr = self._make_manager(mock_client)

    received_timestamps = []

    def check_fn(node):
      received_timestamps.append(node.evaluated_at)
      return {"available": True, "current_value": node.node_value}

    mgr.detect_world_changes(
        session_id="sess-1",
        current_state_fn=check_fn,
    )
    assert len(received_timestamps) == 1
    assert received_timestamps[0] == eval_time

  def test_get_biz_nodes_returns_artifact_uri(self):
    mock_client = MagicMock()
    mock_job = MagicMock()
    mock_job.result.return_value = [
        {
            "biz_node_id": "s1:Product:Yahoo",
            "span_id": "s1",
            "session_id": "sess-1",
            "node_type": "Product",
            "node_value": "Yahoo",
            "confidence": 0.95,
            "artifact_uri": "gs://bucket/output.json",
        },
    ]
    mock_client.query.return_value = mock_job
    mgr = self._make_manager(mock_client)

    nodes = mgr.get_biz_nodes_for_session("sess-1")
    assert len(nodes) == 1
    assert nodes[0].artifact_uri == "gs://bucket/output.json"

  def test_read_biz_nodes_returns_artifact_uri(self):
    mock_client = MagicMock()
    mock_job = MagicMock()
    mock_job.result.return_value = [
        {
            "span_id": "s1",
            "session_id": "sess-1",
            "node_type": "Product",
            "node_value": "Yahoo",
            "confidence": 0.95,
            "artifact_uri": "gs://bucket/file.pdf",
        },
    ]
    mock_client.query.return_value = mock_job
    mgr = self._make_manager(mock_client)

    nodes = mgr._read_biz_nodes(["sess-1"])
    assert len(nodes) == 1
    assert nodes[0].artifact_uri == "gs://bucket/file.pdf"

  def test_cross_link_id_uses_biz_node_id(self):
    from bigquery_agent_analytics.context_graph import _INSERT_CROSS_LINKS_QUERY

    assert "b.biz_node_id AS link_id" in _INSERT_CROSS_LINKS_QUERY

  def test_merge_deletes_stale_biz_nodes(self):
    from bigquery_agent_analytics.context_graph import _EXTRACT_BIZ_NODES_QUERY

    assert "WHEN NOT MATCHED BY SOURCE" in _EXTRACT_BIZ_NODES_QUERY
    assert "DELETE" in _EXTRACT_BIZ_NODES_QUERY

  def test_store_biz_nodes_persists_artifact_uri(self):
    mock_client = MagicMock()
    mock_query_job = MagicMock()
    mock_client.query.return_value = mock_query_job
    mock_load_job = MagicMock()
    mock_client.load_table_from_json.return_value = mock_load_job
    mgr = self._make_manager(mock_client)

    nodes = [
        BizNode(
            span_id="s1",
            session_id="sess-1",
            node_type="Product",
            node_value="Homepage",
            artifact_uri="gs://bucket/artifact.json",
        ),
    ]
    result = mgr.store_biz_nodes(nodes)
    assert result is True
    call_args = mock_client.load_table_from_json.call_args
    loaded_rows = call_args[0][0]
    assert loaded_rows[0]["artifact_uri"] == "gs://bucket/artifact.json"

  def test_create_cross_links_fails_on_real_delete_error(self):
    mock_client = MagicMock()
    # First call (create table) succeeds, second (delete) fails
    mock_job_ok = MagicMock()
    mock_job_ok.result.return_value = None
    call_count = {"n": 0}

    def side_effect(*args, **kwargs):
      call_count["n"] += 1
      if call_count["n"] == 2:
        raise Exception("Permission denied")
      return mock_job_ok

    mock_client.query.side_effect = side_effect
    mgr = self._make_manager(mock_client)

    result = mgr.create_cross_links(["sess-1"])
    assert result is False

  def test_create_cross_links_ignores_not_found_delete(self):
    mock_client = MagicMock()
    mock_job_ok = MagicMock()
    mock_job_ok.result.return_value = None
    call_count = {"n": 0}

    def side_effect(*args, **kwargs):
      call_count["n"] += 1
      if call_count["n"] == 2:
        raise Exception("Table not found: cross_links")
      return mock_job_ok

    mock_client.query.side_effect = side_effect
    mgr = self._make_manager(mock_client)

    result = mgr.create_cross_links(["sess-1"])
    assert result is True


# ------------------------------------------------------------------ #
# Decision Semantics Data Model Tests                                  #
# ------------------------------------------------------------------ #


class TestDecisionPoint:
  """Tests for DecisionPoint dataclass."""

  def test_creation(self):
    dp = DecisionPoint(
        decision_id="dp-1",
        session_id="sess-1",
        span_id="span-5",
        decision_type="audience_selection",
        description="Select target audience for Nike campaign",
    )
    assert dp.decision_id == "dp-1"
    assert dp.decision_type == "audience_selection"
    assert dp.description == "Select target audience for Nike campaign"
    assert dp.metadata == {}

  def test_defaults(self):
    dp = DecisionPoint(
        decision_id="dp-1",
        session_id="sess-1",
        span_id="span-1",
        decision_type="placement",
    )
    assert dp.description == ""
    assert dp.timestamp is None
    assert dp.metadata == {}


class TestCandidate:
  """Tests for Candidate dataclass."""

  def test_selected_candidate(self):
    c = Candidate(
        candidate_id="c-1",
        decision_id="dp-1",
        session_id="sess-1",
        name="Athletes 18-35",
        score=0.91,
        status="SELECTED",
    )
    assert c.name == "Athletes 18-35"
    assert c.score == 0.91
    assert c.status == "SELECTED"
    assert c.rejection_rationale is None

  def test_dropped_candidate(self):
    c = Candidate(
        candidate_id="c-2",
        decision_id="dp-1",
        session_id="sess-1",
        name="Fitness Enthusiasts",
        score=0.78,
        status="DROPPED",
        rejection_rationale="Budget constraint: $50K insufficient for reach",
    )
    assert c.status == "DROPPED"
    assert "Budget constraint" in c.rejection_rationale

  def test_defaults(self):
    c = Candidate(
        candidate_id="c-1",
        decision_id="dp-1",
        session_id="sess-1",
        name="Test",
    )
    assert c.score == 0.0
    assert c.status == "SELECTED"
    assert c.rejection_rationale is None
    assert c.properties == {}


# ------------------------------------------------------------------ #
# Decision Semantics Manager Tests                                     #
# ------------------------------------------------------------------ #


class TestDecisionSemantics:
  """Tests for Decision Semantics extension methods."""

  def _make_manager(self, mock_client=None):
    return ContextGraphManager(
        project_id="test-project",
        dataset_id="test_dataset",
        table_id="agent_events",
        client=mock_client or MagicMock(),
    )

  def test_config_has_decision_tables(self):
    config = ContextGraphConfig()
    assert config.decision_points_table == "decision_points"
    assert config.candidates_table == "candidates"
    assert config.made_decision_edges_table == "made_decision_edges"
    assert config.candidate_edges_table == "candidate_edges"

  def test_config_custom_decision_tables(self):
    config = ContextGraphConfig(
        decision_points_table="my_decisions",
        candidates_table="my_candidates",
        made_decision_edges_table="my_md_edges",
        candidate_edges_table="my_cand_edges",
    )
    assert config.decision_points_table == "my_decisions"
    assert config.candidates_table == "my_candidates"
    assert config.made_decision_edges_table == "my_md_edges"
    assert config.candidate_edges_table == "my_cand_edges"

  def test_store_decision_points_empty(self):
    mgr = self._make_manager()
    assert mgr.store_decision_points([], []) is True

  def test_store_decision_points_success(self):
    mock_client = MagicMock()
    mock_query_job = MagicMock()
    mock_client.query.return_value = mock_query_job
    mock_load_job = MagicMock()
    mock_client.load_table_from_json.return_value = mock_load_job
    mgr = self._make_manager(mock_client)

    dps = [
        DecisionPoint(
            decision_id="dp-1",
            session_id="sess-1",
            span_id="s5",
            decision_type="audience_selection",
            description="Select audience",
        ),
    ]
    candidates = [
        Candidate(
            candidate_id="c-1",
            decision_id="dp-1",
            session_id="sess-1",
            name="Athletes 18-35",
            score=0.91,
            status="SELECTED",
        ),
        Candidate(
            candidate_id="c-2",
            decision_id="dp-1",
            session_id="sess-1",
            name="Fitness Enthusiasts",
            score=0.78,
            status="DROPPED",
            rejection_rationale="Budget constraint",
        ),
    ]
    result = mgr.store_decision_points(dps, candidates)
    assert result is True
    # Two load jobs (one for decision_points, one for candidates).
    assert mock_client.load_table_from_json.call_count == 2
    # Streaming insert path must not be used.
    mock_client.insert_rows_json.assert_not_called()

  def test_store_decision_points_dedupes_by_id_and_uses_load_job(self):
    """The DecisionPoint and CandidateNode KEYs in the property
    graph are decision_id and candidate_id; the backing tables must
    have at most one row per id. ``store_decision_points`` dedupes
    (last-wins) before insert AND writes via load jobs, not via the
    streaming-insert API.

    Both halves are needed: the in-Python dedupe handles the
    in-batch duplicate case (AI.GENERATE returning overlapping
    items in a single extraction), and the load-job path handles
    the cross-batch / rerun case (a prior ``DELETE`` is invisible
    to the next streaming insert because of the ~30 minute legacy
    streaming buffer; load jobs write to managed storage so the
    DELETE in the same Python session sees the rows).
    """
    mock_client = MagicMock()
    mock_query_job = MagicMock()
    mock_client.query.return_value = mock_query_job
    mock_load_job = MagicMock()
    mock_client.load_table_from_json.return_value = mock_load_job
    mgr = self._make_manager(mock_client)

    dps = [
        DecisionPoint(
            decision_id="dp-1",
            session_id="sess-1",
            span_id="s5",
            decision_type="audience_selection",
            description="first",
        ),
        DecisionPoint(
            decision_id="dp-1",  # duplicate id — last wins
            session_id="sess-1",
            span_id="s5",
            decision_type="audience_selection",
            description="second",
        ),
        DecisionPoint(
            decision_id="dp-2",
            session_id="sess-1",
            span_id="s6",
            decision_type="budget_allocation",
        ),
    ]
    candidates = [
        Candidate(
            candidate_id="c-1",
            decision_id="dp-1",
            session_id="sess-1",
            name="A",
            score=0.5,
            status="SELECTED",
        ),
        Candidate(
            candidate_id="c-1",  # duplicate id — last wins
            decision_id="dp-1",
            session_id="sess-1",
            name="A",
            score=0.95,
            status="SELECTED",
        ),
    ]
    assert mgr.store_decision_points(dps, candidates) is True
    # No streaming insert path.
    mock_client.insert_rows_json.assert_not_called()
    # Two load-job calls: first DPs, second candidates.
    dp_args, _ = mock_client.load_table_from_json.call_args_list[0]
    cand_args, _ = mock_client.load_table_from_json.call_args_list[1]
    dp_rows = dp_args[0]
    cand_rows = cand_args[0]
    dp_ids = [r["decision_id"] for r in dp_rows]
    cand_ids = [r["candidate_id"] for r in cand_rows]
    assert len(dp_ids) == len(
        set(dp_ids)
    ), f"load_table_from_json received duplicate decision_id keys: {dp_ids}"
    assert len(cand_ids) == len(
        set(cand_ids)
    ), f"load_table_from_json received duplicate candidate_id keys: {cand_ids}"
    dp_dup = next(r for r in dp_rows if r["decision_id"] == "dp-1")
    cand_dup = next(r for r in cand_rows if r["candidate_id"] == "c-1")
    assert dp_dup["description"] == "second"
    assert cand_dup["score"] == 0.95

  def test_store_decision_points_dp_insert_error(self):
    mock_client = MagicMock()
    mock_query_job = MagicMock()
    mock_client.query.return_value = mock_query_job
    mock_client.load_table_from_json.side_effect = Exception("load failed")
    mgr = self._make_manager(mock_client)

    dps = [
        DecisionPoint(
            decision_id="dp-1",
            session_id="sess-1",
            span_id="s5",
            decision_type="test",
        ),
    ]
    result = mgr.store_decision_points(dps, [])
    assert result is False

  def test_store_decision_points_table_create_failure(self):
    mock_client = MagicMock()
    mock_client.query.side_effect = Exception("Permission denied")
    mgr = self._make_manager(mock_client)

    dps = [
        DecisionPoint(
            decision_id="dp-1",
            session_id="sess-1",
            span_id="s5",
            decision_type="test",
        ),
    ]
    result = mgr.store_decision_points(dps, [])
    assert result is False

  def test_create_decision_edges_success(self):
    mock_client = MagicMock()
    mock_job = MagicMock()
    mock_client.query.return_value = mock_job
    mgr = self._make_manager(mock_client)

    result = mgr.create_decision_edges(["sess-1"])
    assert result is True
    # 4 table creates + 2 deletes + 2 inserts = 8
    assert mock_client.query.call_count == 8

  def test_create_decision_edges_failure(self):
    mock_client = MagicMock()
    mock_client.query.side_effect = Exception("BQ error")
    mgr = self._make_manager(mock_client)

    result = mgr.create_decision_edges(["sess-1"])
    assert result is False

  def test_get_decision_property_graph_ddl(self):
    mgr = self._make_manager()
    ddl = mgr.get_decision_property_graph_ddl()
    assert "CREATE OR REPLACE PROPERTY GRAPH" in ddl
    assert "DecisionPoint" in ddl
    assert "CandidateNode" in ddl
    assert "MadeDecision" in ddl
    assert "CandidateEdge" in ddl
    assert "decision_type" in ddl
    assert "rejection_rationale" in ddl
    assert "test-project" in ddl
    assert "test_dataset" in ddl

  def test_get_decision_property_graph_ddl_custom_name(self):
    mgr = self._make_manager()
    ddl = mgr.get_decision_property_graph_ddl(graph_name="my_graph")
    assert "my_graph" in ddl

  def test_get_eu_audit_gql(self):
    mgr = self._make_manager()
    gql = mgr.get_eu_audit_gql()
    assert "GRAPH" in gql
    assert "MATCH" in gql
    assert "DecisionPoint" in gql
    assert "CandidateNode" in gql
    assert "MadeDecision" in gql
    assert "CandidateEdge" in gql
    assert "candidate_score" in gql
    assert "rejection_rationale" in gql

  def test_get_eu_audit_gql_with_decision_type(self):
    mgr = self._make_manager()
    gql = mgr.get_eu_audit_gql(decision_type="audience_selection")
    assert "@decision_type" in gql

  def test_get_dropped_candidates_gql(self):
    mgr = self._make_manager()
    gql = mgr.get_dropped_candidates_gql()
    assert "GRAPH" in gql
    assert "DROPPED_CANDIDATE" in gql
    assert "CandidateEdge" in gql
    assert "rejection_rationale" in gql
    assert "DecisionPoint" in gql

  def test_get_decision_points_for_session(self):
    mock_client = MagicMock()
    mock_job = MagicMock()
    mock_job.result.return_value = [
        {
            "decision_id": "dp-1",
            "session_id": "sess-1",
            "span_id": "s5",
            "decision_type": "audience_selection",
            "description": "Select audience",
        },
    ]
    mock_client.query.return_value = mock_job
    mgr = self._make_manager(mock_client)

    dps = mgr.get_decision_points_for_session("sess-1")
    assert len(dps) == 1
    assert dps[0].decision_id == "dp-1"
    assert dps[0].decision_type == "audience_selection"

  def test_get_decision_points_for_session_failure(self):
    mock_client = MagicMock()
    mock_client.query.side_effect = Exception("BQ error")
    mgr = self._make_manager(mock_client)

    dps = mgr.get_decision_points_for_session("sess-1")
    assert dps == []

  def test_get_candidates_for_decision(self):
    mock_client = MagicMock()
    mock_job = MagicMock()
    mock_job.result.return_value = [
        {
            "candidate_id": "c-1",
            "decision_id": "dp-1",
            "session_id": "sess-1",
            "name": "Athletes 18-35",
            "score": 0.91,
            "status": "SELECTED",
            "rejection_rationale": None,
        },
        {
            "candidate_id": "c-2",
            "decision_id": "dp-1",
            "session_id": "sess-1",
            "name": "Fitness Enthusiasts",
            "score": 0.78,
            "status": "DROPPED",
            "rejection_rationale": "Budget constraint",
        },
    ]
    mock_client.query.return_value = mock_job
    mgr = self._make_manager(mock_client)

    candidates = mgr.get_candidates_for_decision("dp-1")
    assert len(candidates) == 2
    assert candidates[0].name == "Athletes 18-35"
    assert candidates[0].status == "SELECTED"
    assert candidates[1].status == "DROPPED"
    assert candidates[1].rejection_rationale == "Budget constraint"

  def test_get_candidates_for_decision_failure(self):
    mock_client = MagicMock()
    mock_client.query.side_effect = Exception("BQ error")
    mgr = self._make_manager(mock_client)

    candidates = mgr.get_candidates_for_decision("dp-1")
    assert candidates == []

  def test_export_audit_trail(self):
    mock_client = MagicMock()
    mock_job_dp = MagicMock()
    mock_job_dp.result.return_value = [
        {
            "decision_id": "dp-1",
            "session_id": "sess-1",
            "span_id": "s5",
            "decision_type": "audience_selection",
            "description": "Select audience",
        },
    ]
    mock_job_cand = MagicMock()
    mock_job_cand.result.return_value = [
        {
            "candidate_id": "c-1",
            "decision_id": "dp-1",
            "session_id": "sess-1",
            "name": "Athletes 18-35",
            "score": 0.91,
            "status": "SELECTED",
            "rejection_rationale": None,
        },
        {
            "candidate_id": "c-2",
            "decision_id": "dp-1",
            "session_id": "sess-1",
            "name": "Fitness Enthusiasts",
            "score": 0.78,
            "status": "DROPPED",
            "rejection_rationale": "Budget constraint",
        },
    ]
    # First query returns DPs, second returns candidates
    mock_client.query.side_effect = [mock_job_dp, mock_job_cand]
    mgr = self._make_manager(mock_client)

    trail = mgr.export_audit_trail("sess-1")
    assert len(trail) == 1
    assert trail[0]["decision_type"] == "audience_selection"
    assert len(trail[0]["candidates"]) == 2
    assert trail[0]["candidates"][0]["name"] == "Athletes 18-35"
    assert trail[0]["candidates"][1]["rejection_rationale"] == (
        "Budget constraint"
    )

  def test_export_audit_trail_exclude_dropped(self):
    mock_client = MagicMock()
    mock_job_dp = MagicMock()
    mock_job_dp.result.return_value = [
        {
            "decision_id": "dp-1",
            "session_id": "sess-1",
            "span_id": "s5",
            "decision_type": "audience_selection",
            "description": "Select audience",
        },
    ]
    mock_job_cand = MagicMock()
    mock_job_cand.result.return_value = [
        {
            "candidate_id": "c-1",
            "decision_id": "dp-1",
            "session_id": "sess-1",
            "name": "Athletes 18-35",
            "score": 0.91,
            "status": "SELECTED",
            "rejection_rationale": None,
        },
        {
            "candidate_id": "c-2",
            "decision_id": "dp-1",
            "session_id": "sess-1",
            "name": "Fitness Enthusiasts",
            "score": 0.78,
            "status": "DROPPED",
            "rejection_rationale": "Budget constraint",
        },
    ]
    mock_client.query.side_effect = [mock_job_dp, mock_job_cand]
    mgr = self._make_manager(mock_client)

    trail = mgr.export_audit_trail("sess-1", include_dropped=False)
    assert len(trail) == 1
    assert len(trail[0]["candidates"]) == 1
    assert trail[0]["candidates"][0]["status"] == "SELECTED"

  def test_decision_extraction_uses_typed_ai_generate_output(self):
    """Decision extraction uses SQL-style AI.GENERATE output_schema.

    BigQuery rejects the old JSON-Schema string shape, but current
    AI.GENERATE accepts SQL-style output_schema declarations. The
    decision extractor uses that typed output for the high-variance
    nested decision/candidate shape, then converts ``.decisions`` to
    JSON so the Python parser keeps the same row contract.
    """
    from bigquery_agent_analytics.context_graph import _EXTRACT_DECISION_POINTS_AI_QUERY

    assert "output_schema =>" in _EXTRACT_DECISION_POINTS_AI_QUERY
    assert "decisions ARRAY<STRUCT<" in _EXTRACT_DECISION_POINTS_AI_QUERY
    assert "candidates ARRAY<STRUCT<" in _EXTRACT_DECISION_POINTS_AI_QUERY
    assert "TO_JSON_STRING(" in _EXTRACT_DECISION_POINTS_AI_QUERY
    assert ").decisions" in _EXTRACT_DECISION_POINTS_AI_QUERY
    assert "model_params => JSON" in _EXTRACT_DECISION_POINTS_AI_QUERY
    assert '"temperature": 0.0' in _EXTRACT_DECISION_POINTS_AI_QUERY
    for field in (
        "decision_type",
        "description",
        "candidates",
        "name",
        "score",
        "status",
        "rejection_rationale",
    ):
      assert (
          field in _EXTRACT_DECISION_POINTS_AI_QUERY
      ), f"prompt must enumerate {field!r}"

  def test_decision_property_graph_ddl_includes_base_pillars(self):
    """Decision DDL still includes TechNode, BizNode, Caused, Evaluated."""
    mgr = self._make_manager()
    ddl = mgr.get_decision_property_graph_ddl()
    assert "TechNode" in ddl
    assert "BizNode" in ddl
    assert "Caused" in ddl
    assert "Evaluated" in ddl

  def test_extract_decision_points_empty_rows(self):
    mock_client = MagicMock()
    mock_job = MagicMock()
    mock_job.result.return_value = []
    mock_client.query.return_value = mock_job
    mgr = self._make_manager(mock_client)

    dps, cands = mgr.extract_decision_points(["sess-1"])
    assert dps == []
    assert cands == []

  def test_extract_decision_points_client_path_returns_stubs(self):
    """use_ai_generate=False returns raw_payload stubs, no candidates."""
    mock_client = MagicMock()
    mock_job = MagicMock()
    mock_job.result.return_value = [
        {
            "span_id": "s5",
            "session_id": "sess-1",
            "event_type": "LLM_RESPONSE",
            "payload_text": "Selected Athletes 18-35 audience",
        },
        {
            "span_id": "s8",
            "session_id": "sess-1",
            "event_type": "TOOL_COMPLETED",
            "payload_text": "Placement decision made",
        },
    ]
    mock_client.query.return_value = mock_job
    mgr = self._make_manager(mock_client)

    dps, cands = mgr.extract_decision_points(
        ["sess-1"],
        use_ai_generate=False,
    )
    assert len(dps) == 2
    assert dps[0].span_id == "s5"
    assert dps[0].session_id == "sess-1"
    assert dps[0].decision_type == "raw_payload"
    assert dps[1].span_id == "s8"
    assert cands == []

  def test_extract_decision_points_ai_generate_parses_json(self):
    """AI.GENERATE path parses JSON into DecisionPoints + Candidates."""
    import json

    mock_client = MagicMock()
    mock_job = MagicMock()
    mock_job.result.return_value = [
        {
            "span_id": "s5",
            "session_id": "sess-1",
            "decisions_json": json.dumps(
                [
                    {
                        "decision_type": "audience_selection",
                        "description": "Select target audience",
                        "candidates": [
                            {
                                "name": "Athletes 18-35",
                                "score": 0.91,
                                "status": "SELECTED",
                                "rejection_rationale": None,
                            },
                            {
                                "name": "Fitness Enthusiasts",
                                "score": 0.78,
                                "status": "DROPPED",
                                "rejection_rationale": "Lower engagement",
                            },
                        ],
                    },
                ]
            ),
        },
    ]
    mock_client.query.return_value = mock_job
    mgr = self._make_manager(mock_client)

    dps, cands = mgr.extract_decision_points(
        ["sess-1"],
        use_ai_generate=True,
    )
    assert len(dps) == 1
    assert dps[0].decision_type == "audience_selection"
    assert dps[0].description == "Select target audience"
    assert dps[0].span_id == "s5"
    assert dps[0].session_id == "sess-1"

    assert len(cands) == 2
    assert cands[0].name == "Athletes 18-35"
    assert cands[0].score == 0.91
    assert cands[0].status == "SELECTED"
    assert cands[0].rejection_rationale is None
    assert cands[1].name == "Fitness Enthusiasts"
    assert cands[1].status == "DROPPED"
    assert cands[1].rejection_rationale == "Lower engagement"

  def test_extract_decision_points_ai_generate_bad_json(self):
    """AI.GENERATE path skips rows with unparseable JSON."""
    mock_client = MagicMock()
    mock_job = MagicMock()
    mock_job.result.return_value = [
        {
            "span_id": "s5",
            "session_id": "sess-1",
            "decisions_json": "not valid json {{{",
        },
    ]
    mock_client.query.return_value = mock_job
    mgr = self._make_manager(mock_client)

    dps, cands = mgr.extract_decision_points(
        ["sess-1"],
        use_ai_generate=True,
    )
    assert dps == []
    assert cands == []

  def test_extract_decision_points_ai_generate_empty_json(self):
    """AI.GENERATE path skips rows with empty decisions_json."""
    mock_client = MagicMock()
    mock_job = MagicMock()
    mock_job.result.return_value = [
        {
            "span_id": "s5",
            "session_id": "sess-1",
            "decisions_json": "",
        },
    ]
    mock_client.query.return_value = mock_job
    mgr = self._make_manager(mock_client)

    dps, cands = mgr.extract_decision_points(
        ["sess-1"],
        use_ai_generate=True,
    )
    assert dps == []
    assert cands == []

  def test_extract_decision_points_failure(self):
    mock_client = MagicMock()
    mock_client.query.side_effect = Exception("BQ error")
    mgr = self._make_manager(mock_client)

    dps, cands = mgr.extract_decision_points(["sess-1"])
    assert dps == []
    assert cands == []

  def test_store_candidates_insert_error(self):
    mock_client = MagicMock()
    mock_query_job = MagicMock()
    mock_client.query.return_value = mock_query_job
    # First load (decision_points) succeeds; second (candidates) raises.
    call_count = {"n": 0}

    def load_side_effect(rows, table_ref, job_config=None):
      call_count["n"] += 1
      if call_count["n"] == 2:
        raise Exception("candidates load failed")
      job = MagicMock()
      job.result.return_value = None
      return job

    mock_client.load_table_from_json.side_effect = load_side_effect
    mgr = self._make_manager(mock_client)

    dps = [
        DecisionPoint(
            decision_id="dp-1",
            session_id="sess-1",
            span_id="s5",
            decision_type="test",
        ),
    ]
    candidates = [
        Candidate(
            candidate_id="c-1",
            decision_id="dp-1",
            session_id="sess-1",
            name="Test",
            score=0.5,
        ),
    ]
    result = mgr.store_decision_points(dps, candidates)
    assert result is False

  def test_create_property_graph_with_decisions(self):
    mock_client = MagicMock()
    mock_job = MagicMock()
    mock_client.query.return_value = mock_job
    mgr = self._make_manager(mock_client)

    result = mgr.create_property_graph(include_decisions=True)
    assert result is True
    sql = mock_client.query.call_args[0][0]
    assert "DecisionPoint" in sql
    assert "CandidateNode" in sql
    assert "MadeDecision" in sql

  def test_create_property_graph_without_decisions(self):
    mock_client = MagicMock()
    mock_job = MagicMock()
    mock_client.query.return_value = mock_job
    mgr = self._make_manager(mock_client)

    result = mgr.create_property_graph(include_decisions=False)
    assert result is True
    sql = mock_client.query.call_args[0][0]
    assert "DecisionPoint" not in sql

  def test_explain_decision_audit_path_includes_all(self):
    """session_id triggers EU audit GQL path, include_dropped=True."""
    mock_client = MagicMock()
    mock_job = MagicMock()
    mock_job.result.return_value = [
        {
            "decision_id": "dp-1",
            "decision_type": "audience",
            "decision_description": "test",
            "candidate_name": "Athletes",
            "candidate_score": 0.91,
            "candidate_status": "SELECTED",
            "rejection_rationale": None,
            "edge_type": "SELECTED_CANDIDATE",
            "span_id": "s5",
            "event_type": "LLM_RESPONSE",
            "agent": "root",
        },
        {
            "decision_id": "dp-1",
            "decision_type": "audience",
            "decision_description": "test",
            "candidate_name": "Fitness",
            "candidate_score": 0.78,
            "candidate_status": "DROPPED",
            "rejection_rationale": "Budget",
            "edge_type": "DROPPED_CANDIDATE",
            "span_id": "s5",
            "event_type": "LLM_RESPONSE",
            "agent": "root",
        },
    ]
    mock_client.query.return_value = mock_job
    mgr = self._make_manager(mock_client)

    results = mgr.explain_decision(
        session_id="sess-1",
        include_dropped=True,
    )
    assert len(results) == 2
    assert results[0]["candidate_name"] == "Athletes"
    assert results[1]["candidate_name"] == "Fitness"
    assert results[1]["candidate_status"] == "DROPPED"

  def test_explain_decision_audit_path_filters_dropped(self):
    """include_dropped=False filters out DROPPED candidates."""
    mock_client = MagicMock()
    mock_job = MagicMock()
    mock_job.result.return_value = [
        {
            "decision_id": "dp-1",
            "decision_type": "audience",
            "candidate_name": "Athletes",
            "candidate_score": 0.91,
            "candidate_status": "SELECTED",
            "rejection_rationale": None,
        },
        {
            "decision_id": "dp-1",
            "decision_type": "audience",
            "candidate_name": "Fitness",
            "candidate_score": 0.78,
            "candidate_status": "DROPPED",
            "rejection_rationale": "Budget",
        },
    ]
    mock_client.query.return_value = mock_job
    mgr = self._make_manager(mock_client)

    results = mgr.explain_decision(
        session_id="sess-1",
        include_dropped=False,
    )
    assert len(results) == 1
    assert results[0]["candidate_name"] == "Athletes"

  def test_explain_decision_audit_with_decision_type(self):
    """decision_type filter is passed to EU audit GQL."""
    mock_client = MagicMock()
    mock_job = MagicMock()
    mock_job.result.return_value = [
        {
            "decision_id": "dp-1",
            "decision_type": "audience_selection",
            "candidate_name": "Athletes",
            "candidate_score": 0.91,
            "candidate_status": "SELECTED",
        },
    ]
    mock_client.query.return_value = mock_job
    mgr = self._make_manager(mock_client)

    results = mgr.explain_decision(
        session_id="sess-1",
        decision_type="audience_selection",
    )
    assert len(results) == 1
    # Verify the query includes the decision_type parameter
    call_args = mock_client.query.call_args
    job_config = call_args[1].get("job_config") or call_args[0][1]
    param_names = [p.name for p in job_config.query_parameters]
    assert "decision_type" in param_names

  def test_explain_decision_audit_fallback_on_gql_error(self):
    """EU audit GQL failure falls back to export_audit_trail."""
    mock_client = MagicMock()
    # First call (EU audit GQL) fails
    mock_job_fail = MagicMock()
    mock_job_fail.result.side_effect = Exception("GQL not available")
    # Fallback: export_audit_trail calls get_decision_points + get_candidates
    mock_job_dp = MagicMock()
    mock_job_dp.result.return_value = [
        {
            "decision_id": "dp-1",
            "session_id": "sess-1",
            "span_id": "s5",
            "decision_type": "audience",
            "description": "test",
        },
    ]
    mock_job_cand = MagicMock()
    mock_job_cand.result.return_value = [
        {
            "candidate_id": "c-1",
            "decision_id": "dp-1",
            "session_id": "sess-1",
            "name": "Athletes",
            "score": 0.91,
            "status": "SELECTED",
            "rejection_rationale": None,
        },
    ]
    mock_client.query.side_effect = [
        mock_job_fail,
        mock_job_dp,
        mock_job_cand,
    ]
    mgr = self._make_manager(mock_client)

    results = mgr.explain_decision(
        session_id="sess-1",
        include_dropped=True,
    )
    # Falls back to export_audit_trail format
    assert len(results) == 1
    assert results[0]["decision_type"] == "audience"

  def test_explain_decision_reasoning_chain_fallback(self):
    """No session_id triggers old BizNode reasoning chain path."""
    mock_client = MagicMock()
    mock_job = MagicMock()
    mock_job.result.return_value = [
        {
            "decision_span_id": "s1",
            "entity_value": "Nike",
            "entity_type": "Product",
        },
    ]
    mock_client.query.return_value = mock_job
    mgr = self._make_manager(mock_client)

    results = mgr.explain_decision(
        biz_entity="Nike",
    )
    assert len(results) == 1
    assert results[0]["entity_value"] == "Nike"

  def test_build_context_graph_with_decisions(self):
    mock_client = MagicMock()
    mock_query_job = MagicMock()
    mock_query_job.result.return_value = []
    mock_client.query.return_value = mock_query_job
    mock_load_job = MagicMock()
    mock_client.load_table_from_json.return_value = mock_load_job
    mgr = self._make_manager(mock_client)

    results = mgr.build_context_graph(
        session_ids=["sess-1"],
        use_ai_generate=False,
        include_decisions=True,
    )
    assert "decision_points_count" in results
    assert "decision_edges_created" in results
    assert results["property_graph_created"] is True

  def test_export_audit_trail_json_format(self):
    mock_client = MagicMock()
    mock_job_dp = MagicMock()
    mock_job_dp.result.return_value = [
        {
            "decision_id": "dp-1",
            "session_id": "sess-1",
            "span_id": "s5",
            "decision_type": "audience",
            "description": "test",
        },
    ]
    mock_job_cand = MagicMock()
    mock_job_cand.result.return_value = [
        {
            "candidate_id": "c-1",
            "decision_id": "dp-1",
            "session_id": "sess-1",
            "name": "Athletes",
            "score": 0.91,
            "status": "SELECTED",
            "rejection_rationale": None,
        },
    ]
    mock_client.query.side_effect = [mock_job_dp, mock_job_cand]
    mgr = self._make_manager(mock_client)

    result = mgr.export_audit_trail("sess-1", format="json")
    assert isinstance(result, str)
    import json

    parsed = json.loads(result)
    assert len(parsed) == 1
    assert parsed[0]["decision_type"] == "audience"

  def test_store_decision_points_deletes_before_insert(self):
    """Verifies idempotency: delete queries run before the load job.

    Writes go through a load job (managed storage), not the
    streaming-insert API — so the DELETE issued just above is
    visible to the load that follows in the same Python session.
    """
    mock_client = MagicMock()
    mock_query_job = MagicMock()
    mock_client.query.return_value = mock_query_job
    mock_load_job = MagicMock()
    mock_client.load_table_from_json.return_value = mock_load_job
    mgr = self._make_manager(mock_client)

    dps = [
        DecisionPoint(
            decision_id="dp-1",
            session_id="sess-1",
            span_id="s5",
            decision_type="test",
        ),
    ]
    mgr.store_decision_points(dps, [])
    # Queries: 4 table creates + 4 deletes; at minimum the
    # DELETEs ran. The load-job for DPs ran via
    # load_table_from_json.
    assert mock_client.query.call_count >= 4
    mock_client.load_table_from_json.assert_called_once()
    mock_client.insert_rows_json.assert_not_called()

  def test_decision_ddl_edge_source_dest_types(self):
    """MadeDecision: TechNode->DecisionPoint,
    CandidateEdge: DecisionPoint->CandidateNode."""
    mgr = self._make_manager()
    ddl = mgr.get_decision_property_graph_ddl()
    # MadeDecision edge: source=TechNode, dest=DecisionPoint
    assert "SOURCE KEY (span_id) REFERENCES TechNode" in ddl
    assert "DESTINATION KEY (decision_id) REFERENCES DecisionPoint" in ddl
    # CandidateEdge: source=DecisionPoint, dest=CandidateNode
    assert "SOURCE KEY (decision_id) REFERENCES DecisionPoint" in ddl
    assert "DESTINATION KEY (candidate_id) REFERENCES CandidateNode" in ddl

  def test_eu_audit_gql_edge_direction(self):
    """EU audit GQL traverses forward: TechNode->DP->Candidate."""
    mgr = self._make_manager()
    gql = mgr.get_eu_audit_gql()
    # Should go forward, not backward
    assert "-[md:MadeDecision]->" in gql
    assert "-[ce:CandidateEdge]->" in gql
    assert "<-" not in gql


# ------------------------------------------------------------------ #
# Client integration test                                              #
# ------------------------------------------------------------------ #


class TestClientContextGraph:
  """Tests for Client.context_graph() factory method."""

  def test_context_graph_returns_manager(self):
    with patch("bigquery_agent_analytics.client.make_bq_client"):
      from bigquery_agent_analytics.client import Client

      # Patch schema verification
      with patch.object(Client, "_verify_schema"):
        client = Client(
            project_id="p",
            dataset_id="d",
        )
        mgr = client.context_graph()
        assert isinstance(mgr, ContextGraphManager)
        assert mgr.project_id == "p"
        assert mgr.dataset_id == "d"

  def test_context_graph_with_config(self):
    with patch("bigquery_agent_analytics.client.make_bq_client"):
      from bigquery_agent_analytics.client import Client

      with patch.object(Client, "_verify_schema"):
        client = Client(
            project_id="p",
            dataset_id="d",
        )
        cfg = ContextGraphConfig(graph_name="custom_graph")
        mgr = client.context_graph(config=cfg)
        assert mgr.config.graph_name == "custom_graph"

  def test_get_session_trace_gql_fallback_on_empty(self):
    """GQL with no edges falls back to flat get_session_trace."""
    with patch("bigquery_agent_analytics.client.make_bq_client"):
      from bigquery_agent_analytics.client import Client
      from bigquery_agent_analytics.trace import Trace

      with patch.object(Client, "_verify_schema"):
        client = Client(
            project_id="p",
            dataset_id="d",
        )
        # GQL returns empty
        with patch.object(
            ContextGraphManager,
            "reconstruct_trace_gql",
            return_value=[],
        ):
          mock_trace = Trace(trace_id="t1", session_id="sess-1", spans=[])
          with patch.object(
              Client,
              "get_session_trace",
              return_value=mock_trace,
          ) as mock_flat:
            result = client.get_session_trace_gql(session_id="sess-1")
            mock_flat.assert_called_once_with("sess-1")
            assert result.session_id == "sess-1"

  def test_get_session_trace_gql_merges_isolated_events(self):
    """GQL edges + flat SQL merge captures isolated events."""
    with patch("bigquery_agent_analytics.client.make_bq_client"):
      from bigquery_agent_analytics.client import Client
      from bigquery_agent_analytics.trace import Span
      from bigquery_agent_analytics.trace import Trace

      with patch.object(Client, "_verify_schema"):
        client = Client(
            project_id="p",
            dataset_id="d",
        )
        ts = datetime(2025, 6, 1, 12, 0, tzinfo=timezone.utc)
        # GQL returns one edge pair (s1 -> s2)
        gql_rows = [
            {
                "parent_span_id": "s1",
                "parent_event_type": "USER_MESSAGE_RECEIVED",
                "parent_agent": "root",
                "parent_timestamp": ts,
                "session_id": "sess-1",
                "parent_invocation_id": "inv-1",
                "parent_content": {},
                "parent_latency_ms": None,
                "parent_status": "OK",
                "parent_error_message": None,
                "child_span_id": "s2",
                "child_event_type": "LLM_REQUEST",
                "child_agent": "root",
                "child_timestamp": ts,
                "child_invocation_id": "inv-1",
                "child_content": {},
                "child_latency_ms": 500,
                "child_status": "OK",
                "child_error_message": None,
            },
        ]
        # Flat trace has s1, s2, and an isolated s3
        flat_spans = [
            Span(
                event_type="USER_MESSAGE_RECEIVED",
                agent="root",
                timestamp=ts,
                span_id="s1",
            ),
            Span(
                event_type="LLM_REQUEST",
                agent="root",
                timestamp=ts,
                span_id="s2",
            ),
            Span(
                event_type="STATE_DELTA",
                agent="root",
                timestamp=ts,
                span_id="s3",
            ),
        ]
        flat_trace = Trace(
            trace_id="t1",
            session_id="sess-1",
            spans=flat_spans,
        )
        with patch.object(
            ContextGraphManager,
            "reconstruct_trace_gql",
            return_value=gql_rows,
        ):
          with patch.object(
              Client,
              "get_session_trace",
              return_value=flat_trace,
          ):
            result = client.get_session_trace_gql(session_id="sess-1")
            span_ids = {s.span_id for s in result.spans}
            # All three spans present: s1, s2 from GQL + s3 from flat
            assert "s1" in span_ids
            assert "s2" in span_ids
            assert "s3" in span_ids
            assert len(result.spans) == 3

  def test_get_session_trace_gql_backfills_parent_link(self):
    """Span first seen as parent_ gets parent_span_id backfilled."""
    with patch("bigquery_agent_analytics.client.make_bq_client"):
      from bigquery_agent_analytics.client import Client
      from bigquery_agent_analytics.trace import Span
      from bigquery_agent_analytics.trace import Trace

      with patch.object(Client, "_verify_schema"):
        client = Client(
            project_id="p",
            dataset_id="d",
        )
        ts1 = datetime(2025, 6, 1, 12, 0, tzinfo=timezone.utc)
        ts2 = datetime(2025, 6, 1, 12, 1, tzinfo=timezone.utc)
        ts3 = datetime(2025, 6, 1, 12, 2, tzinfo=timezone.utc)
        # Row 1: s2 is parent of s3
        # Row 2: s1 is parent of s2
        # So s2 is seen first as parent_ (no parent link),
        # then as child_ of s1 — must backfill.
        gql_rows = [
            {
                "parent_span_id": "s2",
                "parent_event_type": "LLM_REQUEST",
                "parent_agent": "root",
                "parent_timestamp": ts2,
                "session_id": "sess-1",
                "parent_invocation_id": "inv-1",
                "parent_content": {},
                "parent_latency_ms": None,
                "parent_status": "OK",
                "parent_error_message": None,
                "child_span_id": "s3",
                "child_event_type": "TOOL_COMPLETED",
                "child_agent": "root",
                "child_timestamp": ts3,
                "child_invocation_id": "inv-1",
                "child_content": {},
                "child_latency_ms": 100,
                "child_status": "OK",
                "child_error_message": None,
            },
            {
                "parent_span_id": "s1",
                "parent_event_type": "USER_MESSAGE_RECEIVED",
                "parent_agent": "root",
                "parent_timestamp": ts1,
                "session_id": "sess-1",
                "parent_invocation_id": "inv-1",
                "parent_content": {},
                "parent_latency_ms": None,
                "parent_status": "OK",
                "parent_error_message": None,
                "child_span_id": "s2",
                "child_event_type": "LLM_REQUEST",
                "child_agent": "root",
                "child_timestamp": ts2,
                "child_invocation_id": "inv-1",
                "child_content": {},
                "child_latency_ms": 200,
                "child_status": "OK",
                "child_error_message": None,
            },
        ]
        flat_trace = Trace(
            trace_id="t1",
            session_id="sess-1",
            spans=[
                Span(
                    event_type="USER_MESSAGE_RECEIVED",
                    agent="root",
                    timestamp=ts1,
                    span_id="s1",
                ),
                Span(
                    event_type="LLM_REQUEST",
                    agent="root",
                    timestamp=ts2,
                    span_id="s2",
                ),
                Span(
                    event_type="TOOL_COMPLETED",
                    agent="root",
                    timestamp=ts3,
                    span_id="s3",
                ),
            ],
        )
        with patch.object(
            ContextGraphManager,
            "reconstruct_trace_gql",
            return_value=gql_rows,
        ):
          with patch.object(
              Client,
              "get_session_trace",
              return_value=flat_trace,
          ):
            result = client.get_session_trace_gql(session_id="sess-1")
            by_id = {s.span_id: s for s in result.spans}
            # s2 should have s1 as parent (backfilled)
            assert by_id["s2"].parent_span_id == "s1"
            # s3 should have s2 as parent
            assert by_id["s3"].parent_span_id == "s2"
            # s1 has no parent
            assert by_id["s1"].parent_span_id is None

  def test_get_session_trace_gql_chronological_order(self):
    """Spans are returned in chronological order."""
    with patch("bigquery_agent_analytics.client.make_bq_client"):
      from bigquery_agent_analytics.client import Client
      from bigquery_agent_analytics.trace import Trace

      with patch.object(Client, "_verify_schema"):
        client = Client(
            project_id="p",
            dataset_id="d",
        )
        ts1 = datetime(2025, 6, 1, 12, 0, tzinfo=timezone.utc)
        ts2 = datetime(2025, 6, 1, 12, 1, tzinfo=timezone.utc)
        ts3 = datetime(2025, 6, 1, 12, 2, tzinfo=timezone.utc)
        # GQL rows in reverse order
        gql_rows = [
            {
                "parent_span_id": "s2",
                "parent_event_type": "LLM_REQUEST",
                "parent_agent": "root",
                "parent_timestamp": ts2,
                "session_id": "sess-1",
                "parent_invocation_id": "inv-1",
                "parent_content": {},
                "parent_latency_ms": None,
                "parent_status": "OK",
                "parent_error_message": None,
                "child_span_id": "s3",
                "child_event_type": "TOOL_COMPLETED",
                "child_agent": "root",
                "child_timestamp": ts3,
                "child_invocation_id": "inv-1",
                "child_content": {},
                "child_latency_ms": 100,
                "child_status": "OK",
                "child_error_message": None,
            },
            {
                "parent_span_id": "s1",
                "parent_event_type": "USER_MESSAGE_RECEIVED",
                "parent_agent": "root",
                "parent_timestamp": ts1,
                "session_id": "sess-1",
                "parent_invocation_id": "inv-1",
                "parent_content": {},
                "parent_latency_ms": None,
                "parent_status": "OK",
                "parent_error_message": None,
                "child_span_id": "s2",
                "child_event_type": "LLM_REQUEST",
                "child_agent": "root",
                "child_timestamp": ts2,
                "child_invocation_id": "inv-1",
                "child_content": {},
                "child_latency_ms": 200,
                "child_status": "OK",
                "child_error_message": None,
            },
        ]
        flat_trace = Trace(
            trace_id="t1",
            session_id="sess-1",
            spans=[],
        )
        with patch.object(
            ContextGraphManager,
            "reconstruct_trace_gql",
            return_value=gql_rows,
        ):
          with patch.object(
              Client,
              "get_session_trace",
              return_value=flat_trace,
          ):
            result = client.get_session_trace_gql(session_id="sess-1")
            ids = [s.span_id for s in result.spans]
            assert ids == ["s1", "s2", "s3"]
