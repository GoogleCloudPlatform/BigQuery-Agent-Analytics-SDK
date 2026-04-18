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

"""Tests that context_graph.py wires every query site through SDK labels.

Grouped by workflow family rather than one test per site:
- Extraction (AI.GENERATE + fallback)
- Table & graph DDL (no ai_function)
- GQL queries (reasoning, causal, trace, audit)
- Decision-semantics (extract + store + edge creation)
- World-change detection
- Loop-labeling regression guards: every loop pattern that either
  rebuilds a fresh labeled config per iteration (DDL loop) OR shares
  one labeled config across all iterations (delete/insert loops).
- Warn-once pattern for the single class that accepts an injected
  client.

No async guard here — context_graph.py is fully synchronous. The
Phase 0 `test_survives_run_in_executor_dispatch` test already covers
the async-executor-vs-label-ordering regression for the SDK as a
whole.
"""

import logging
from unittest.mock import MagicMock

from google.auth.credentials import AnonymousCredentials
from google.cloud import bigquery
import pytest

from bigquery_agent_analytics.context_graph import ContextGraphConfig
from bigquery_agent_analytics.context_graph import ContextGraphManager


def _mock_bq_client():
  client = MagicMock()
  job = MagicMock()
  job.result.return_value = []
  client.query.return_value = job
  client.insert_rows_json.return_value = []
  return client


def _make_manager(mock_client):
  return ContextGraphManager(
      project_id="p",
      dataset_id="d",
      table_id="agent_events",
      client=mock_client,
  )


def _labels_per_call(mock_bq):
  """Return every (sql, labels) seen on mock_bq.query across all calls."""
  out = []
  for call in mock_bq.query.call_args_list:
    args, kwargs = call
    sql = args[0] if args else kwargs.get("query", "")
    cfg = kwargs.get("job_config")
    labels = dict(cfg.labels) if cfg and cfg.labels else {}
    out.append((sql, labels))
  return out


def _all_feature_labels(mock_bq):
  return [
      labels.get("sdk_feature") for _sql, labels in _labels_per_call(mock_bq)
  ]


# ------------------------------------------------------------------ #
# Business-entity extraction                                           #
# ------------------------------------------------------------------ #


class TestExtractBizNodesLabels:
  """Extraction runs multiple queries — the CREATE TABLE DDL and the
  extract step itself. AI.GENERATE path carries sdk_ai_function; the
  client-side fallback path does not."""

  def test_ai_generate_extract_labels_ai_generate(self):
    mock_bq = _mock_bq_client()
    mgr = _make_manager(mock_bq)
    mgr._extract_via_ai_generate(["sess-1"])
    calls = _labels_per_call(mock_bq)
    # Expect: CREATE TABLE DDL (no ai_function) + AI.GENERATE extract.
    assert any(
        l.get("sdk_feature") == "context-graph"
        and l.get("sdk_ai_function") == "ai-generate"
        for _sql, l in calls
    ), f"no AI.GENERATE-labeled call found; got {calls}"

  def test_client_side_payload_extract_labels_context_graph_no_ai(self):
    mock_bq = _mock_bq_client()
    mgr = _make_manager(mock_bq)
    mgr._extract_payloads_for_client(["sess-1"])
    _sql, labels = _labels_per_call(mock_bq)[0]
    assert labels.get("sdk_feature") == "context-graph"
    assert "sdk_ai_function" not in labels


# ------------------------------------------------------------------ #
# Table & graph DDL (no ai_function)                                   #
# ------------------------------------------------------------------ #


class TestDdlLabels:

  def test_ensure_biz_nodes_table_labels_context_graph(self):
    mock_bq = _mock_bq_client()
    mgr = _make_manager(mock_bq)
    mgr._ensure_biz_nodes_table()
    _sql, labels = _labels_per_call(mock_bq)[0]
    assert labels.get("sdk_feature") == "context-graph"
    assert "sdk_ai_function" not in labels

  def test_create_property_graph_labels_context_graph(self):
    mock_bq = _mock_bq_client()
    mgr = _make_manager(mock_bq)
    mgr.create_property_graph()
    features = _all_feature_labels(mock_bq)
    assert features == ["context-graph"]

  def test_ensure_decision_tables_labels_each_ddl(self):
    mock_bq = _mock_bq_client()
    mgr = _make_manager(mock_bq)
    mgr._ensure_decision_tables()
    features = _all_feature_labels(mock_bq)
    # Four distinct CREATE-TABLE DDLs run in a loop; each must be
    # separately labeled with a fresh job_config.
    assert len(features) == 4
    for f in features:
      assert f == "context-graph"


# ------------------------------------------------------------------ #
# GQL queries                                                          #
# ------------------------------------------------------------------ #


class TestGqlLabels:

  def test_reconstruct_trace_gql_labels_context_graph(self):
    mock_bq = _mock_bq_client()
    mgr = _make_manager(mock_bq)
    mgr.reconstruct_trace_gql(session_id="sess-1")
    _sql, labels = _labels_per_call(mock_bq)[0]
    assert labels.get("sdk_feature") == "context-graph"
    assert "sdk_ai_function" not in labels

  def test_traverse_causal_chain_labels_context_graph(self):
    mock_bq = _mock_bq_client()
    mgr = _make_manager(mock_bq)
    mgr.traverse_causal_chain(session_id="sess-1")
    _sql, labels = _labels_per_call(mock_bq)[0]
    assert labels.get("sdk_feature") == "context-graph"

  def test_explain_decision_gql_paths_labeled(self):
    mock_bq = _mock_bq_client()
    mgr = _make_manager(mock_bq)
    mgr.explain_decision(
        decision_event_type="TOOL_STARTING",
        session_id="sess-1",
    )
    features = _all_feature_labels(mock_bq)
    assert features, "expected at least one GQL query to dispatch"
    for f in features:
      assert f == "context-graph"


# ------------------------------------------------------------------ #
# Decision semantics                                                   #
# ------------------------------------------------------------------ #


class TestDecisionSemanticsLabels:

  def test_extract_decisions_ai_path_labels_ai_generate(self):
    mock_bq = _mock_bq_client()
    mgr = _make_manager(mock_bq)
    mgr._extract_decisions_via_ai_generate(["sess-1"])
    _sql, labels = _labels_per_call(mock_bq)[0]
    assert labels.get("sdk_feature") == "context-graph"
    assert labels.get("sdk_ai_function") == "ai-generate"

  def test_extract_decisions_client_path_no_ai(self):
    mock_bq = _mock_bq_client()
    mgr = _make_manager(mock_bq)
    mgr._extract_decisions_for_client(["sess-1"])
    _sql, labels = _labels_per_call(mock_bq)[0]
    assert labels.get("sdk_feature") == "context-graph"
    assert "sdk_ai_function" not in labels

  def test_get_decision_points_for_session_labeled(self):
    mock_bq = _mock_bq_client()
    mgr = _make_manager(mock_bq)
    mgr.get_decision_points_for_session("sess-1")
    _sql, labels = _labels_per_call(mock_bq)[0]
    assert labels.get("sdk_feature") == "context-graph"

  def test_get_candidates_for_decision_labeled(self):
    mock_bq = _mock_bq_client()
    mgr = _make_manager(mock_bq)
    mgr.get_candidates_for_decision("dp-1")
    _sql, labels = _labels_per_call(mock_bq)[0]
    assert labels.get("sdk_feature") == "context-graph"


# ------------------------------------------------------------------ #
# World-change detection                                               #
# ------------------------------------------------------------------ #


class TestWorldChangeLabels:

  def test_get_biz_nodes_for_session_labeled(self):
    mock_bq = _mock_bq_client()
    mgr = _make_manager(mock_bq)
    mgr.get_biz_nodes_for_session("sess-1")
    _sql, labels = _labels_per_call(mock_bq)[0]
    assert labels.get("sdk_feature") == "context-graph"
    assert "sdk_ai_function" not in labels


# ------------------------------------------------------------------ #
# Loop-labeling regression guards                                      #
# ------------------------------------------------------------------ #


class TestSharedConfigLoopsKeepLabels:
  """Three methods dispatch multiple queries that share a single
  `job_config` across a loop or sequence:

  - `create_cross_links`: DELETE then INSERT, both reusing one config.
  - `_delete_decision_data_for_sessions`: 4 DELETEs in a loop.
  - `create_decision_edges`: 2 DELETE-loop iterations + 2 explicit
    INSERTs, all sharing one config.

  These are the complement to `test_ensure_decision_tables_labels_each_ddl`
  (which proves the fresh-config-per-iteration pattern works).
  Regression guard: if someone later splits a shared config out of the
  loop without also labeling the new one — OR strips labels after
  construction in some future refactor — at least one iteration's
  query will lose its label. Assert every query carries
  sdk_feature=context-graph."""

  def test_create_cross_links_delete_insert_keep_labels(self):
    mock_bq = _mock_bq_client()
    mgr = _make_manager(mock_bq)
    mgr.create_cross_links(["sess-1", "sess-2"])
    # Expect 3 queries: CREATE TABLE DDL (fresh config), DELETE
    # (shared config), INSERT (same shared config).
    features = _all_feature_labels(mock_bq)
    assert (
        len(features) >= 2
    ), f"expected ≥2 labeled queries (delete+insert share a config); got {features}"
    for f in features:
      assert f == "context-graph"

  def test_delete_decision_data_labels_every_iteration(self):
    # 4 DELETEs run in a Python loop, each using the single shared
    # labeled `job_config` built before the loop.
    mock_bq = _mock_bq_client()
    mgr = _make_manager(mock_bq)
    mgr._delete_decision_data_for_sessions(["sess-1"])
    features = _all_feature_labels(mock_bq)
    assert (
        len(features) == 4
    ), f"expected exactly 4 DELETEs in the loop; got {len(features)}"
    for f in features:
      assert f == "context-graph"

  def test_create_decision_edges_all_queries_labeled(self):
    # `create_decision_edges` runs: _ensure_decision_tables (4 DDLs,
    # fresh configs) + 2 DELETEs (shared config) + 2 INSERTs (same
    # shared config) = 8 total queries. Every one should be labeled.
    mock_bq = _mock_bq_client()
    mgr = _make_manager(mock_bq)
    mgr.create_decision_edges(["sess-1"])
    features = _all_feature_labels(mock_bq)
    assert (
        len(features) >= 8
    ), f"expected ≥8 queries (4 DDLs + 2 DELETEs + 2 INSERTs); got {len(features)}"
    for f in features:
      assert f == "context-graph"


# ------------------------------------------------------------------ #
# Warn-once for ContextGraphManager                                    #
# ------------------------------------------------------------------ #


class TestVanillaClientWarnOnce:

  def test_vanilla_client_emits_one_warning(self, caplog):
    vanilla = bigquery.Client(project="p", credentials=AnonymousCredentials())
    mgr = ContextGraphManager(project_id="p", dataset_id="d", client=vanilla)
    with caplog.at_level(logging.WARNING):
      _ = mgr.client
      _ = mgr.client
      _ = mgr.client
    warnings = [
        r
        for r in caplog.records
        if "SDK telemetry labels will not be applied" in r.message
    ]
    assert len(warnings) == 1
