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

"""Tests-First unit tests for Slice 2: query router, SQL templates, search, and pattern-to-eval."""

from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from datetime import timezone
import inspect
import json
from pathlib import Path
import re
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

_DEMO_DIR = Path(__file__).resolve().parents[1]
if str(_DEMO_DIR) not in sys.path:
  sys.path.insert(0, str(_DEMO_DIR))

from analytics.index_and_search import _fuse_rrf_rows
from analytics.index_and_search import assert_pure_ascii_sql  # noqa: E402
from analytics.index_and_search import build_candidate_keys_param
from analytics.index_and_search import build_materialize_eval_turns_sql
from analytics.index_and_search import load_named_sql_queries
from analytics.index_and_search import render_sql_tokens
from analytics.index_and_search import run_index_and_search
from analytics.index_and_search import sanitize_search_query_for_bq
from analytics.index_and_search import setup_dual_indexes
from analytics.index_and_search import split_sql_statements
from analytics.pattern_to_eval import build_eval_suite_from_patterns  # noqa: E402
from analytics.pattern_to_eval import compute_eval_cost_cascade
from analytics.pattern_to_eval import discover_patterns_bigquery
from analytics.pattern_to_eval import discover_patterns_offline
from analytics.pattern_to_eval import run_pattern_to_eval
from analytics.query_router import classify_layer0  # noqa: E402
from analytics.query_router import QueryRouter
from analytics.query_router import ROUTE_HYBRID
from analytics.query_router import ROUTE_KEYWORD
from analytics.seed_scale_benchmark import generate_synthetic_eval_turns  # noqa: E402
from analytics.seed_scale_benchmark import seed_scale_rows_to_bigquery

from bigquery_agent_analytics import EvalSuite  # noqa: E402

_SQL_DIR = _DEMO_DIR / "sql"
_SQL_FILES = [
    "01_materialize_eval_turns.sql",
    "02_setup_dual_indexes.sql",
    "03_search_keyword_and_semantic.sql",
    "04_verify_contradictions_ai_generate.sql",
    "05_pattern_discovery.sql",
]


def _sample_turn_rows() -> list[dict[str, object]]:
  """Builds realistic deterministic turn fixtures mirroring demo scenarios."""
  return [
      {
          "session_id": "sess_01_healthy",
          "invocation_id": "inv_001",
          "turn_index": 0,
          "user_id": "demo_user",
          "root_agent": "support_agent",
          "turn_start_timestamp": "2026-11-24T00:00:01+00:00",
          "turn_end_timestamp": "2026-11-24T00:00:03+00:00",
          "tool_call_count": 2,
          "tool_error_count": 0,
          "llm_error_count": 0,
          "agent_error_count": 0,
          "has_error": False,
          "has_truncated_content": False,
          "has_tool_loop": False,
          "max_consecutive_identical_tool_calls": 1,
          "distinct_tools_used": ["lookup_order", "issue_refund"],
          "total_prompt_tokens": 1100,
          "total_completion_tokens": 90,
          "total_tokens": 1190,
          "turn_latency_ms": 1400,
          "eval_ready": True,
          "error_summary": None,
          "raw_trace_bytes": 11200,
          "_ai_index_trajectory": (
              "[OUTCOME: root_agent=support_agent has_error=false "
              "tool_calls=2 tool_errors=0 loop=false latency_ms=1400]\n"
              "USER: Please issue a refund for order ORD-9012.\n"
              "TOOL: lookup_order(order_id='ORD-9012') -> ok=true\n"
              "TOOL: issue_refund(order_id='ORD-9012') -> ok=true\n"
              "AGENT: Your refund for order ORD-9012 has been completed."
          ),
          "trace": {
              "agents": {"support_agent": {"instruction": "x" * 9000}},
              "turns": [{"turn_index": 0, "turn_id": "inv_001", "events": []}],
          },
      },
      {
          "session_id": "sess_02_exact_err",
          "invocation_id": "inv_002",
          "turn_index": 0,
          "user_id": "demo_user",
          "root_agent": "support_agent",
          "turn_start_timestamp": "2026-11-24T00:01:01+00:00",
          "turn_end_timestamp": "2026-11-24T00:01:03+00:00",
          "tool_call_count": 1,
          "tool_error_count": 1,
          "llm_error_count": 0,
          "agent_error_count": 0,
          "has_error": True,
          "has_truncated_content": False,
          "has_tool_loop": False,
          "max_consecutive_identical_tool_calls": 1,
          "distinct_tools_used": ["issue_refund"],
          "total_prompt_tokens": 1250,
          "total_completion_tokens": 95,
          "total_tokens": 1345,
          "turn_latency_ms": 1650,
          "eval_ready": True,
          "error_summary": "CARD_EXPIRED",
          "raw_trace_bytes": 11500,
          "_ai_index_trajectory": (
              "[OUTCOME: root_agent=support_agent has_error=true "
              "tool_calls=1 tool_errors=1 loop=false latency_ms=1650]\n"
              "USER: Refund order ORD-8841.\n"
              "TOOL: issue_refund(order_id='ORD-8841') -> "
              "ERROR: CARD_EXPIRED\n"
              "AGENT: Sorry, your refund failed because the card expired."
          ),
          "trace": {
              "agents": {"support_agent": {"instruction": "x" * 9000}},
              "turns": [{"turn_index": 0, "turn_id": "inv_002", "events": []}],
          },
      },
      {
          "session_id": "sess_03_contradiction",
          "invocation_id": "inv_003",
          "turn_index": 0,
          "user_id": "demo_user",
          "root_agent": "support_agent",
          "turn_start_timestamp": "2026-11-24T00:02:01+00:00",
          "turn_end_timestamp": "2026-11-24T00:02:03+00:00",
          "tool_call_count": 1,
          "tool_error_count": 1,
          "llm_error_count": 0,
          "agent_error_count": 0,
          "has_error": True,
          "has_truncated_content": False,
          "has_tool_loop": False,
          "max_consecutive_identical_tool_calls": 1,
          "distinct_tools_used": ["issue_refund"],
          "total_prompt_tokens": 1300,
          "total_completion_tokens": 80,
          "total_tokens": 1380,
          "turn_latency_ms": 1710,
          "eval_ready": True,
          "error_summary": "CARD_EXPIRED",
          "raw_trace_bytes": 11800,
          "_ai_index_trajectory": (
              "[OUTCOME: root_agent=support_agent has_error=true "
              "tool_calls=1 tool_errors=1 loop=false latency_ms=1710]\n"
              "USER: Please refund order ORD-8841.\n"
              "TOOL: issue_refund(order_id='ORD-8841') -> "
              "ERROR: CARD_EXPIRED\n"
              "AGENT: Your refund for order ORD-8841 has been processed!"
          ),
          "trace": {
              "agents": {"support_agent": {"instruction": "x" * 9000}},
              "turns": [{"turn_index": 0, "turn_id": "inv_003", "events": []}],
          },
      },
      {
          "session_id": "sess_04_retry_loop",
          "invocation_id": "inv_004",
          "turn_index": 0,
          "user_id": "demo_user",
          "root_agent": "support_agent",
          "turn_start_timestamp": "2026-11-24T00:03:01+00:00",
          "turn_end_timestamp": "2026-11-24T00:03:08+00:00",
          "tool_call_count": 4,
          "tool_error_count": 4,
          "llm_error_count": 0,
          "agent_error_count": 0,
          "has_error": True,
          "has_truncated_content": False,
          "has_tool_loop": True,
          "max_consecutive_identical_tool_calls": 4,
          "distinct_tools_used": ["search_knowledge_base"],
          "total_prompt_tokens": 4200,
          "total_completion_tokens": 220,
          "total_tokens": 4420,
          "turn_latency_ms": 6800,
          "eval_ready": True,
          "error_summary": "DEADLINE_EXCEEDED",
          "raw_trace_bytes": 16200,
          "_ai_index_trajectory": (
              "[OUTCOME: root_agent=support_agent has_error=true "
              "tool_calls=4 tool_errors=4 loop=true latency_ms=6800]\n"
              "USER: Check warehouse delay policy for ORD-7710.\n"
              "TOOL: search_knowledge_base(query='ORD-7710') x4 -> "
              "ERROR: DEADLINE_EXCEEDED\n"
              "AGENT: Sorry, the knowledge base lookup failed after retries."
          ),
          "trace": {
              "agents": {"support_agent": {"instruction": "x" * 9000}},
              "turns": [{"turn_index": 0, "turn_id": "inv_004", "events": []}],
          },
      },
      {
          "session_id": "sess_05_bloat",
          "invocation_id": "inv_005",
          "turn_index": 0,
          "user_id": "demo_user",
          "root_agent": "support_agent",
          "turn_start_timestamp": "2026-11-24T00:04:01+00:00",
          "turn_end_timestamp": "2026-11-24T00:04:04+00:00",
          "tool_call_count": 1,
          "tool_error_count": 0,
          "llm_error_count": 0,
          "agent_error_count": 0,
          "has_error": False,
          "has_truncated_content": False,
          "has_tool_loop": False,
          "max_consecutive_identical_tool_calls": 1,
          "distinct_tools_used": ["export_audit_blob"],
          "total_prompt_tokens": 5800,
          "total_completion_tokens": 150,
          "total_tokens": 5950,
          "turn_latency_ms": 2100,
          "eval_ready": True,
          "error_summary": None,
          "raw_trace_bytes": 24500,
          "_ai_index_trajectory": (
              "[OUTCOME: root_agent=support_agent has_error=false "
              "tool_calls=1 tool_errors=0 loop=false latency_ms=2100]\n"
              "USER: Export audit summary for account ACCT-10.\n"
              "TOOL: export_audit_blob(account_id='ACCT-10') -> ok=true\n"
              "AGENT: Audit summary exported successfully."
          ),
          "trace": {
              "agents": {"support_agent": {"instruction": "x" * 9000}},
              "turns": [{"turn_index": 0, "turn_id": "inv_005", "events": []}],
          },
      },
      {
          "session_id": "sess_06_healthy_policy",
          "invocation_id": "inv_006",
          "turn_index": 0,
          "user_id": "demo_user",
          "root_agent": "support_agent",
          "turn_start_timestamp": "2026-11-24T00:05:01+00:00",
          "turn_end_timestamp": "2026-11-24T00:05:02+00:00",
          "tool_call_count": 1,
          "tool_error_count": 0,
          "llm_error_count": 0,
          "agent_error_count": 0,
          "has_error": False,
          "has_truncated_content": False,
          "has_tool_loop": False,
          "max_consecutive_identical_tool_calls": 1,
          "distinct_tools_used": ["check_policy_eligibility"],
          "total_prompt_tokens": 950,
          "total_completion_tokens": 75,
          "total_tokens": 1025,
          "turn_latency_ms": 1100,
          "eval_ready": True,
          "error_summary": None,
          "raw_trace_bytes": 10400,
          "_ai_index_trajectory": (
              "[OUTCOME: root_agent=support_agent has_error=false "
              "tool_calls=1 tool_errors=0 loop=false latency_ms=1100]\n"
              "USER: Am I eligible for warranty coverage on gold tier?\n"
              "TOOL: check_policy_eligibility(customer_tier='gold') -> "
              "eligible=true\n"
              "AGENT: Yes, your gold tier order is eligible for warranty."
          ),
          "trace": {
              "agents": {"support_agent": {"instruction": "x" * 9000}},
              "turns": [{"turn_index": 0, "turn_id": "inv_006", "events": []}],
          },
      },
  ]


# ---------------------------------------------------------------------------
# 1. Pure-ASCII & GoogleSQL Contract Tests
# ---------------------------------------------------------------------------


def test_all_sql_files_exist_and_are_pure_ascii():
  placeholder_in_comment_re = re.compile(
      r"\{(?:PROJECT_ID|DATASET_ID|EVENTS_TABLE|EVAL_TURNS_TABLE|"
      r"ENDPOINT|CONNECTION_ARG)\}"
  )
  for name in _SQL_FILES:
    path = _SQL_DIR / name
    assert path.is_file(), f"Missing SQL file: {path}"
    text = path.read_text(encoding="utf-8")
    assert_pure_ascii_sql(text, name=name)
    for line in text.splitlines():
      if line.lstrip().startswith("--"):
        assert not placeholder_in_comment_re.search(
            line
        ), f"Comment in {name} must not contain brace placeholder: {line}"


def test_assert_pure_ascii_sql_rejects_non_ascii():
  with pytest.raises(ValueError, match="non-ASCII"):
    assert_pure_ascii_sql("-- em-dash \u2014 fails", name="bad.sql")


def test_01_materialize_eval_turns_matches_head_schema():
  sql_text = (_SQL_DIR / "01_materialize_eval_turns.sql").read_text(
      encoding="utf-8"
  )
  rendered = sql_text.format(
      PROJECT_ID="demo-proj",
      DATASET_ID="agent_ops",
      EVENTS_TABLE="agent_events",
      EVAL_TURNS_TABLE="eval_turns",
  )
  # Check final output projection: must expose has_error and root_agent,
  # and must NOT expose status or root_agent_name columns on eval_turns.
  final_select = rendered.split("FROM\n  EvalTurns", maxsplit=1)[0]
  final_tail = final_select.rsplit("SELECT", maxsplit=1)[-1]
  for required_col in (
      "session_id",
      "invocation_id",
      "turn_index",
      "user_id",
      "root_agent",
      "has_error",
      "has_tool_loop",
      "eval_ready",
      "trace",
      "error_summary",
      "raw_trace_bytes",
      "_ai_index_trajectory",
  ):
    assert re.search(rf"\b{required_col}\b", final_tail), required_col

  assert not re.search(r"\bstatus\b", final_tail)
  assert not re.search(r"\broot_agent_name\b", final_tail)
  assert not re.search(r"\berror_message\b", final_tail)


def test_02_setup_dual_indexes_contract():
  sql_text = (_SQL_DIR / "02_setup_dual_indexes.sql").read_text(
      encoding="utf-8"
  )
  stmts = split_sql_statements(sql_text)
  assert len(stmts) == 3

  embed_ddl, search_ddl, vec_ddl = stmts
  assert "_ai_index_trajectory_embedding" in embed_ddl
  assert "AI.EMBED" in embed_ddl
  assert "embeddinggemma-300m" in embed_ddl
  assert "asynchronous = TRUE" in embed_ddl

  assert "CREATE SEARCH INDEX IF NOT EXISTS idx_turns_search" in search_ddl
  assert "LOG_ANALYZER" in search_ddl
  assert "default_index_column_granularity = 'COLUMN'" in search_ddl
  assert "root_agent" in search_ddl
  assert "error_summary" in search_ddl
  assert not re.search(r"\bstatus\b", search_ddl)
  assert not re.search(r"\berror_message\b", search_ddl)

  assert "CREATE VECTOR INDEX IF NOT EXISTS idx_turns_vec" in vec_ddl
  assert re.search(r"\(\s*_ai_index_trajectory_embedding\s*\)", vec_ddl)
  assert "_ai_index_embedding" not in vec_ddl
  assert "PARTITION BY" not in vec_ddl.upper()
  assert "STORING (" in vec_ddl
  assert "_ai_index_trajectory" in vec_ddl.split("STORING (", maxsplit=1)[1]
  assert "index_type = 'IVF'" in vec_ddl
  assert "distance_type = 'COSINE'" in vec_ddl


def test_03_search_queries_fix_all_head_contracts():
  queries = load_named_sql_queries(
      _SQL_DIR / "03_search_keyword_and_semantic.sql"
  )
  expected_keys = {
      "PATH_A_KEYWORD",
      "PATH_A_METADATA_FILTER",
      "PATH_B_AI_SEARCH_RERANK",
      "PATH_B_AI_SEARCH_NO_RERANK",
      "PATH_B_SYNC_EMBED_FALLBACK",
      "PATH_C_HYBRID_RRF",
      "PATH_C_HYBRID_RRF_NO_RERANK",
      "STEP2_LAZY_TRACE_HYDRATION",
  }
  assert expected_keys.issubset(queries.keys())

  for qname, sql_block in queries.items():
    if qname == "STEP2_LAZY_TRACE_HYDRATION":
      assert "trace" in sql_block
      continue
    # Step-1 search templates must never project trace JSON or use status.
    assert not re.search(r"\bstatus\b", sql_block), qname
    assert not re.search(r"\broot_agent_name\b", sql_block), qname
    assert "has_error" in sql_block, qname
    assert "root_agent" in sql_block, qname

  path_b_rerank = queries["PATH_B_AI_SEARCH_RERANK"]
  assert "AI.SEARCH(" in path_b_rerank
  assert "'_ai_index_trajectory'" in path_b_rerank
  assert "@search_query" in path_b_rerank
  assert "'q_vec'" not in path_b_rerank
  assert "_ai_index_embedding" not in path_b_rerank
  assert "_ai_index_trajectory_embedding" not in path_b_rerank
  assert "rerank => TRUE" in path_b_rerank
  assert "rerank_score" in path_b_rerank
  assert "ORDER BY rerank_score DESC" in path_b_rerank
  assert "ORDER BY distance ASC" not in path_b_rerank

  path_b_no_rerank = queries["PATH_B_AI_SEARCH_NO_RERANK"]
  assert "AI.SEARCH(" in path_b_no_rerank
  assert "rerank => TRUE" not in path_b_no_rerank
  assert "ORDER BY distance ASC" in path_b_no_rerank

  path_b_fallback = queries["PATH_B_SYNC_EMBED_FALLBACK"]
  assert "AI.EMBED(" in path_b_fallback
  assert "ML.DISTANCE(" in path_b_fallback

  path_c = queries["PATH_C_HYBRID_RRF"]
  assert "1.0 / (60 +" in path_c
  assert "SEARCH(" in path_c
  assert "@keyword_query" in path_c
  assert "@search_query" in path_c
  assert "AI.SEARCH(" in path_c
  assert "rerank => TRUE" in path_c
  assert (
      "ORDER BY rrf_score DESC, COALESCE(sem.distance, 1.0) ASC, "
      "session_id ASC, invocation_id ASC" in path_c
  )

  path_c_no_rerank = queries["PATH_C_HYBRID_RRF_NO_RERANK"]
  assert "1.0 / (60 +" in path_c_no_rerank
  assert "AI.SEARCH(" in path_c_no_rerank
  assert "rerank => FALSE" in path_c_no_rerank
  assert "CAST(NULL AS FLOAT64) AS rerank_score" in path_c_no_rerank
  assert (
      "ORDER BY distance ASC, base.session_id ASC, base.invocation_id ASC"
      in path_c_no_rerank
  )
  assert (
      "ORDER BY rrf_score DESC, COALESCE(sem.distance, 1.0) ASC, "
      "session_id ASC, invocation_id ASC" in path_c_no_rerank
  )


def test_04_verify_contradictions_ai_generate_contract():
  sql_text = (_SQL_DIR / "04_verify_contradictions_ai_generate.sql").read_text(
      encoding="utf-8"
  )
  assert "UNNEST(@candidate_keys) AS k WITH OFFSET AS ord" in sql_text
  assert "ORDER BY ord ASC" in sql_text
  assert "LIMIT @top_k" in sql_text
  assert "AI.GENERATE(" in sql_text
  assert "_ai_index_trajectory" in sql_text
  assert "thinking_level" in sql_text
  assert "generation_config" in sql_text
  for field in (
      "is_silent_contradiction BOOL",
      "pattern_label STRING",
      "confidence FLOAT64",
      "evidence_snippet STRING",
  ):
    assert field in sql_text

  # Rendering with a single-line CONNECTION_ARG must leave comments untouched
  # and emit valid single-line connection_id syntax before model_params.
  rendered = render_sql_tokens(
      sql_text,
      {
          "PROJECT_ID": "demo-proj",
          "DATASET_ID": "agent_ops",
          "EVAL_TURNS_TABLE": "eval_turns",
          "ENDPOINT": "gemini-4-hydrogen",
          "CONNECTION_ARG": "connection_id => 'demo-proj.us.vertex_conn', ",
      },
  )
  assert (
      "connection_id => 'demo-proj.us.vertex_conn', model_params =>" in rendered
  )
  for line in rendered.splitlines():
    if "Substitution contract:" in line:
      assert "connection_id =>" not in line
  assert (
      inspect.signature(setup_dual_indexes)
      .parameters["embed_wait_seconds"]
      .default
      == 65
  )
  assert (
      inspect.signature(run_index_and_search)
      .parameters["embed_wait_seconds"]
      .default
      == 65
  )


def test_05_pattern_discovery_uses_agent_line_and_no_not_search():
  sql_text = (_SQL_DIR / "05_pattern_discovery.sql").read_text(encoding="utf-8")
  assert "NOT SEARCH" not in sql_text.upper()
  assert "REGEXP_EXTRACT" in sql_text
  assert "AGENT:" in sql_text
  assert "SILENT_CONTRADICTION_CANDIDATE" in sql_text
  assert "TOOL_LOOP" in sql_text
  assert "EXACT_ERROR_SPIKE" in sql_text
  assert "TOKEN_BLOAT" in sql_text
  assert "denied|restricted|unauthorized" in sql_text
  for extra_marker in (
      "unfortunately",
      "dispatched",
      "rejected",
      r"could\s+not",
      "couldn't",
      "can't",
  ):
    assert extra_marker in sql_text
  assert not re.search(r"\bstatus\b", sql_text)
  assert not re.search(r"\broot_agent_name\b", sql_text)

  # Offline parity check proving contradiction heuristic catches silent
  # contradictions even though [OUTCOME: has_error=true] and TOOL ERROR exist,
  # and does NOT falsely flag explicit refusal/apology turns.
  patterns = discover_patterns_offline(_sample_turn_rows())
  by_class = {p["pattern_class"]: p for p in patterns}
  assert "SILENT_CONTRADICTION_CANDIDATE" in by_class
  sc = by_class["SILENT_CONTRADICTION_CANDIDATE"]
  sc_sessions = {t["session_id"] for t in sc["sample_turns"]}
  assert "sess_03_contradiction" in sc_sessions
  assert "sess_02_exact_err" not in sc_sessions
  assert by_class["TOOL_LOOP"]["affected_turns"] == 1
  assert by_class["EXACT_ERROR_SPIKE"]["affected_turns"] == 1
  assert by_class["TOKEN_BLOAT"]["affected_turns"] == 1

  # 19 KB turn (> 18,000 byte threshold) must be classified as TOKEN_BLOAT.
  bloat_19kb = dict(
      _sample_turn_rows()[0], session_id="sess_19kb", raw_trace_bytes=19000
  )
  bloat_patterns = discover_patterns_offline([bloat_19kb])
  assert len(bloat_patterns) == 1
  assert bloat_patterns[0]["pattern_class"] == "TOKEN_BLOAT"

  # Healthy multi-tool turns up to 9,300 tokens (< 12,000 threshold) are NOT
  # falsely classified as TOKEN_BLOAT, while >= 12,000 tokens still triggers.
  normal_9300_tok = dict(
      _sample_turn_rows()[0],
      session_id="sess_9300tok",
      raw_trace_bytes=12216,
      total_tokens=9300,
  )
  assert discover_patterns_offline([normal_9300_tok]) == []

  bloat_12239_tok = dict(
      _sample_turn_rows()[0],
      session_id="sess_12239tok",
      raw_trace_bytes=12500,
      total_tokens=12239,
  )
  bloat_tok_patterns = discover_patterns_offline([bloat_12239_tok])
  assert len(bloat_tok_patterns) == 1
  assert bloat_tok_patterns[0]["pattern_class"] == "TOKEN_BLOAT"

  # Explicit denial / restriction response classifies as EXACT_ERROR_SPIKE,
  # never a false SILENT_CONTRADICTION_CANDIDATE.
  denied_turn = {
      "session_id": "sess_07_denied",
      "invocation_id": "inv_007",
      "turn_index": 0,
      "root_agent": "support_agent",
      "has_error": True,
      "has_tool_loop": False,
      "error_summary": "PERMISSION_DENIED",
      "total_tokens": 1400,
      "raw_trace_bytes": 11000,
      "_ai_index_trajectory": (
          "[OUTCOME: root_agent=support_agent has_error=true]\n"
          "USER: Look up order VIP-0001.\n"
          "TOOL_RES: lookup_order -> PERMISSION_DENIED\n"
          "AGENT: Access to order VIP-0001 is denied due to restricted policy."
      ),
  }
  denied_patterns = discover_patterns_offline([denied_turn])
  assert len(denied_patterns) == 1
  assert denied_patterns[0]["pattern_class"] == "EXACT_ERROR_SPIKE"


# ---------------------------------------------------------------------------
# 2. Layer 0 & Layer 1 Query Router Tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("query", "expected_mode"),
    [
        ("", "KEYWORD"),
        ("   ", "KEYWORD"),
        ('"refund has been processed"', "KEYWORD"),
        ("sess_9f8a7b6c", "KEYWORD"),
        ("inv_0042", "KEYWORD"),
        ("CARD_EXPIRED", "KEYWORD"),
        ("DEADLINE_EXCEEDED", "KEYWORD"),
        ("why did issue_refund succeed after CARD_EXPIRED?", "HYBRID"),
    ],
)
def test_layer0_deterministic_routing_zero_api_calls(
    query: str, expected_mode: str
):
  decision = classify_layer0(query)
  assert decision is not None
  assert decision.mode == expected_mode
  assert decision.layer == "L0_RULE"
  assert decision.cost_usd == 0.0
  assert decision.latency_ms == 0.0


def test_layer1_decisions_api_routing_and_v1beta(
    monkeypatch: pytest.MonkeyPatch,
):
  created_clients = []

  class FakeDecisionsClient:

    def __init__(self, *, api_key=None, http_options=None):
      created_clients.append({"api_key": api_key, "http_options": http_options})
      self.decisions = self
      self._confidence = 0.92
      self._choice = "NATURAL_LANGUAGE"

    def create(self, *, model, input, questions):
      assert model == "gemini-decision-1"
      assert "route" in questions
      return SimpleNamespace(
          answers={
              "route": SimpleNamespace(
                  choice=self._choice,
                  confidence=self._confidence,
                  probabilities={
                      "KEYWORD": 1.0 - self._confidence,
                      "NATURAL_LANGUAGE": self._confidence,
                  },
              )
          }
      )

  fake = FakeDecisionsClient(
      api_key="test-key", http_options={"api_version": "v1beta"}
  )
  monkeypatch.delenv("GEMINI_DECISIONS_API_BASE", raising=False)
  monkeypatch.setenv("GEMINI_API_KEY", "test-key")
  monkeypatch.setattr(
      "analytics.query_router._build_decisions_client",
      lambda api_key: FakeDecisionsClient(
          api_key=api_key, http_options={"api_version": "v1beta"}
      ),
  )

  router = QueryRouter(backend="decisions")
  high_conf = router.classify("agent falsely told the user a refund succeeded")
  assert high_conf.mode == "NATURAL_LANGUAGE"
  assert high_conf.layer == "L1_DECISIONS_API"
  assert high_conf.output_tokens == 0
  assert created_clients[-1]["http_options"] == {"api_version": "v1beta"}

  # Low confidence (< 0.70) must escalate to HYBRID.
  fake._confidence = 0.61
  router_injected = QueryRouter(backend="decisions", decisions_client=fake)
  low_conf = router_injected.classify("refund issue on user account")
  assert low_conf.mode == "HYBRID"
  assert low_conf.layer == "L1_DECISIONS_API"
  assert low_conf.confidence == pytest.approx(0.61)


def test_layer1_vertex_hydrogen_and_flash_fallback():
  calls = []

  class FakeModels:

    def generate_content(self, *, model, contents, config):
      calls.append({"model": model, "contents": contents, "config": config})
      payload = json.dumps(
          {
              "route": "NATURAL_LANGUAGE",
              "confidence": 0.89,
              "reason": "behavioral description",
          }
      )
      return SimpleNamespace(
          text=payload,
          usage_metadata=SimpleNamespace(
              prompt_token_count=42,
              candidates_token_count=18,
              thoughts_token_count=7,
          ),
      )

  fake_vertex = SimpleNamespace(models=FakeModels())
  router = QueryRouter(backend="vertex", vertex_client=fake_vertex)
  assert router.location == "global"
  decision = router.classify(
      "agent got stuck calling the same lookup tool repeatedly"
  )
  assert decision.mode == "NATURAL_LANGUAGE"
  assert decision.layer == "L1_VERTEX_HYDROGEN"
  assert decision.input_tokens == 42
  assert decision.output_tokens == 25
  assert calls[0]["model"] == "gemini-4-hydrogen"
  assert calls[0]["config"].thinking_config.thinking_level.value == "LOW"

  legacy_router = QueryRouter(
      backend="vertex",
      vertex_model="gemini-2.5-flash",
      location="us-central1",
      vertex_client=fake_vertex,
  )
  legacy_decision = legacy_router.classify(
      "agent got stuck calling the same lookup tool repeatedly"
  )
  assert legacy_decision.layer == "L1_VERTEX_FLASH"
  assert calls[1]["model"] == "gemini-2.5-flash"
  assert calls[1]["config"].thinking_config.thinking_budget == 0


def test_live_router_fails_closed_when_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
):
  monkeypatch.delenv("GEMINI_API_KEY", raising=False)
  monkeypatch.delenv("GEMINI_DECISIONS_API_BASE", raising=False)
  monkeypatch.setattr(
      "analytics.query_router._build_vertex_client",
      lambda **kwargs: (_ for _ in ()).throw(RuntimeError("no adc")),
  )
  router = QueryRouter(backend="vertex", offline=False)
  with pytest.raises(RuntimeError, match="no adc"):
    router.classify("agent got stuck calling lookup repeatedly")

  decisions_router = QueryRouter(backend="decisions", offline=False)
  with pytest.raises(RuntimeError, match="Decisions API"):
    decisions_router.classify("agent got stuck calling lookup repeatedly")


def test_parse_decisions_api_base_validates_schemes_and_loopback():
  from analytics.query_router import _parse_decisions_api_base  # noqa: PLC0415

  assert (
      _parse_decisions_api_base(
          "https://generativelanguage.googleapis.com/v1beta/"
      )
      == "https://generativelanguage.googleapis.com/v1beta"
  )
  assert (
      _parse_decisions_api_base("http://127.0.0.1:8080/v1beta/")
      == "http://127.0.0.1:8080/v1beta"
  )
  assert (
      _parse_decisions_api_base("http://localhost:9001")
      == "http://localhost:9001"
  )

  for bad_url in (
      "",
      "   ",
      "http://example.com/v1beta",
      "http://127.0.0.1/v1beta",
      "https://user:pass@generativelanguage.googleapis.com/v1beta",
      "https://generativelanguage.googleapis.com/v1beta?key=123",
      "https://generativelanguage.googleapis.com/v1beta#frag",
      "ftp://generativelanguage.googleapis.com/v1beta",
      "https://bad host/v1beta",
      "https://generativelanguage.googleapis.com/v1beta\x00",
  ):
    with pytest.raises(ValueError):
      _parse_decisions_api_base(bad_url)


def test_routedecision_model_populated_and_positional_compat():
  from analytics.query_router import RouteDecision  # noqa: PLC0415

  positional = RouteDecision("KEYWORD", "L0_RULE", 1.0, "rule", 0.0)
  assert positional.model == ""

  l0 = classify_layer0("CARD_EXPIRED")
  assert l0 is not None
  assert l0.model == "deterministic_rules"

  offline_router = QueryRouter(backend="offline", offline=True)
  l1_off = offline_router.classify(
      "why did the agent loop silently without reporting failure"
  )
  assert l1_off.layer == "L1_OFFLINE_HEURISTIC"
  assert l1_off.model == "offline_heuristic"


def test_layer1_decisions_rest_fallback_when_sdk_lacks_decisions(
    monkeypatch: pytest.MonkeyPatch,
):
  from http.server import BaseHTTPRequestHandler  # noqa: PLC0415
  from http.server import ThreadingHTTPServer  # noqa: PLC0415
  import threading  # noqa: PLC0415

  captured: list[dict[str, object]] = []
  response_queue: list[dict[str, object]] = [
      {
          "answers": {
              "route": {
                  "choice": "NATURAL_LANGUAGE",
                  "confidence": 0.96,
                  "probabilities": {
                      "KEYWORD": 0.04,
                      "NATURAL_LANGUAGE": 0.96,
                  },
              }
          },
          "usage": {"total_input_tokens": 125},
      },
      {
          "answers": {
              "route": {
                  "choice": "NATURAL_LANGUAGE",
                  "probabilities": {
                      "KEYWORD": 0.39,
                      "NATURAL_LANGUAGE": 0.61,
                  },
              }
          },
          "usage": {"total_input_tokens": 98},
      },
      {
          "answers": {
              "route": {
                  "choice": "NATURAL_LANGUAGE",
                  "confidence": 0.91,
                  "probabilities": {
                      "KEYWORD": 0.09,
                      "NATURAL_LANGUAGE": 0.91,
                  },
              }
          },
          "usage": {"total_input_tokens": 110},
      },
  ]

  class _Handler(BaseHTTPRequestHandler):

    def do_POST(self) -> None:
      length = int(self.headers.get("Content-Length", "0"))
      body = json.loads(self.rfile.read(length).decode("utf-8"))
      captured.append(
          {
              "path": self.path,
              "api_key": self.headers.get("x-goog-api-key"),
              "content_type": self.headers.get("Content-Type"),
              "body": body,
          }
      )
      payload = json.dumps(response_queue.pop(0)).encode("utf-8")
      self.send_response(200)
      self.send_header("Content-Type", "application/json")
      self.send_header("Content-Length", str(len(payload)))
      self.end_headers()
      self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:
      del format, args

  server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
  thread = threading.Thread(target=server.serve_forever, daemon=True)
  thread.start()
  base_url = f"http://127.0.0.1:{server.server_address[1]}/v1beta"
  try:
    # Simulate google-genai 2.34.0 where client has no `.decisions` attribute.
    monkeypatch.setenv("GEMINI_API_KEY", "test-rest-key")
    monkeypatch.setenv("GEMINI_DECISIONS_API_BASE", base_url)
    monkeypatch.setattr(
        "analytics.query_router._build_decisions_client",
        lambda api_key: SimpleNamespace(models=SimpleNamespace()),
    )

    router_auto = QueryRouter(backend="auto")
    d1 = router_auto.classify(
        "agent claimed refund succeeded even though tool failed"
    )
    assert d1.mode == "NATURAL_LANGUAGE"
    assert d1.layer == "L1_DECISIONS_API"
    assert d1.model == "gemini-decision-1"
    assert d1.input_tokens == 125
    assert d1.output_tokens == 0
    assert d1.confidence == pytest.approx(0.96)
    assert d1.probabilities == pytest.approx(
        {"KEYWORD": 0.04, "NATURAL_LANGUAGE": 0.96}
    )
    assert captured[0]["path"] == "/v1beta/decisions"
    assert captured[0]["api_key"] == "test-rest-key"
    assert captured[0]["content_type"] == "application/json"
    req_body = captured[0]["body"]
    assert isinstance(req_body, dict)
    assert req_body["model"] == "gemini-decision-1"
    assert req_body["input"] == [
        {
            "type": "text",
            "text": "agent claimed refund succeeded even though tool failed",
        }
    ]
    assert req_body["questions"]["route"]["type"] == "choice"

    # Missing confidence falls back to max(probabilities) and <0.70 -> HYBRID.
    router_dec = QueryRouter(backend="decisions")
    d2 = router_dec.classify("refund issue on user account")
    assert d2.mode == "HYBRID"
    assert d2.raw_choice == "NATURAL_LANGUAGE"
    assert d2.confidence == pytest.approx(0.61)
    assert d2.model == "gemini-decision-1"

    # Custom base without api_key still runs REST without x-goog-api-key header.
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    router_nokey = QueryRouter(backend="decisions", decisions_api_base=base_url)
    d3 = router_nokey.classify("agent repeated lookup in a loop")
    assert d3.mode == "NATURAL_LANGUAGE"
    assert captured[2]["api_key"] is None
  finally:
    server.shutdown()
    server.server_close()


def test_layer1_decisions_rest_fails_closed_on_block_redirect_and_errors():
  from http.server import BaseHTTPRequestHandler  # noqa: PLC0415
  from http.server import ThreadingHTTPServer  # noqa: PLC0415
  import threading  # noqa: PLC0415

  secret_key = "test-secret-rest-key-xyz"
  redirect_hits: list[str] = []

  class _EvilTarget(BaseHTTPRequestHandler):

    def do_POST(self) -> None:
      redirect_hits.append(self.headers.get("x-goog-api-key") or "")
      self.send_response(200)
      self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
      del format, args

  evil_server = ThreadingHTTPServer(("127.0.0.1", 0), _EvilTarget)
  evil_thread = threading.Thread(target=evil_server.serve_forever, daemon=True)
  evil_thread.start()
  evil_url = (
      f"http://127.0.0.1:{evil_server.server_address[1]}/v1beta/decisions"
  )

  scenarios: list[tuple[int, dict[str, str], str]] = [
      (302, {"Location": evil_url}, "{}"),
      (200, {}, json.dumps({"promptFeedback": {"blockReason": "SAFETY"}})),
      (200, {}, json.dumps({"prompt_feedback": {"block_reason": "OTHER"}})),
      (500, {}, f"upstream error echoing {secret_key}"),
      (200, {}, json.dumps({"answers": {"route": {"choice": "BOGUS"}}})),
  ]

  class _FailHandler(BaseHTTPRequestHandler):

    def do_POST(self) -> None:
      length = int(self.headers.get("Content-Length", "0"))
      _ = self.rfile.read(length)
      status, extra_headers, body_str = scenarios.pop(0)
      body_bytes = body_str.encode("utf-8")
      self.send_response(status)
      for hk, hv in extra_headers.items():
        self.send_header(hk, hv)
      self.send_header("Content-Type", "application/json")
      self.send_header("Content-Length", str(len(body_bytes)))
      self.end_headers()
      self.wfile.write(body_bytes)

    def log_message(self, format: str, *args: object) -> None:
      del format, args

  server = ThreadingHTTPServer(("127.0.0.1", 0), _FailHandler)
  thread = threading.Thread(target=server.serve_forever, daemon=True)
  thread.start()
  base_url = f"http://127.0.0.1:{server.server_address[1]}/v1beta"
  try:
    router = QueryRouter(
        backend="decisions",
        api_key=secret_key,
        decisions_api_base=base_url,
        decisions_client=SimpleNamespace(),
    )
    # 1. Redirect blocked, no key leaked to evil target.
    with pytest.raises(RuntimeError, match="redirect|HTTP 302"):
      router.classify("agent got stuck calling lookup repeatedly")
    assert redirect_hits == []

    # 2. promptFeedback.blockReason fails closed.
    with pytest.raises(RuntimeError, match="blocked"):
      router.classify("agent got stuck calling lookup repeatedly")

    # 3. prompt_feedback.block_reason fails closed.
    with pytest.raises(RuntimeError, match="blocked"):
      router.classify("agent got stuck calling lookup repeatedly")

    # 4. HTTP 500 scrubs api_key from raised RuntimeError.
    with pytest.raises(RuntimeError) as exc_info:
      router.classify("agent got stuck calling lookup repeatedly")
    assert secret_key not in str(exc_info.value)

    # 5. Malformed answers fails closed.
    with pytest.raises(RuntimeError, match="malformed"):
      router.classify("agent got stuck calling lookup repeatedly")
  finally:
    server.shutdown()
    server.server_close()
    evil_server.shutdown()
    evil_server.server_close()


def test_index_and_search_cli_threads_router_backend_and_decisions_args(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
  from analytics import index_and_search  # noqa: PLC0415

  captured: dict[str, object] = {}

  def _fake_run(**kwargs):
    captured.update(kwargs)
    return {
        "index_status": {"ai_embed_column": "MOCK"},
        "queries": [],
    }

  monkeypatch.setattr(index_and_search, "run_index_and_search", _fake_run)
  rc = index_and_search.main(
      [
          "--offline",
          "--router-backend",
          "decisions",
          "--decisions-model",
          "gemini-decision-1",
          "--decisions-api-base",
          "http://127.0.0.1:8080/v1beta",
          "--output-json",
          str(tmp_path / "out.json"),
      ]
  )
  assert rc == 0
  assert captured["router_backend"] == "decisions"
  assert captured["decisions_model"] == "gemini-decision-1"
  assert captured["decisions_api_base"] == "http://127.0.0.1:8080/v1beta"


def test_layer1_decisions_sdk_parity_usage_blocking_and_malformed():
  responses: list[object] = [
      SimpleNamespace(
          answers={
              "route": SimpleNamespace(
                  choice="NATURAL_LANGUAGE",
                  confidence=0.94,
                  probabilities={"KEYWORD": 0.06, "NATURAL_LANGUAGE": 0.94},
              )
          },
          usage=SimpleNamespace(total_input_tokens=77),
      ),
      SimpleNamespace(
          answers={
              "route": SimpleNamespace(
                  choice="NATURAL_LANGUAGE",
                  confidence=0.88,
                  probabilities={"KEYWORD": 0.12, "NATURAL_LANGUAGE": 0.88},
              )
          },
          usage=SimpleNamespace(total_input_tokens=0, prompt_token_count=99),
      ),
      SimpleNamespace(
          prompt_feedback=SimpleNamespace(block_reason="SAFETY"),
          answers=None,
      ),
      SimpleNamespace(
          promptFeedback={"blockReason": "OTHER"},
          answers=None,
      ),
      SimpleNamespace(answers=None),
      SimpleNamespace(answers={}),
      SimpleNamespace(answers={"route": SimpleNamespace(choice="BOGUS")}),
  ]

  class _FakeDecisions:

    def create(self, *, model, input, questions):
      del model, input, questions
      return responses.pop(0)

  router = QueryRouter(
      backend="decisions",
      decisions_client=SimpleNamespace(decisions=_FakeDecisions()),
  )
  query = "agent falsely told the user a refund succeeded"

  d_usage = router.classify(query)
  assert d_usage.mode == "NATURAL_LANGUAGE"
  assert d_usage.input_tokens == 77
  assert d_usage.model == "gemini-decision-1"

  d_zero = router.classify(query)
  assert d_zero.mode == "NATURAL_LANGUAGE"
  assert d_zero.input_tokens == 0

  with pytest.raises(RuntimeError, match="blocked.*SAFETY"):
    router.classify(query)

  with pytest.raises(RuntimeError, match="blocked.*OTHER"):
    router.classify(query)

  with pytest.raises(RuntimeError, match="malformed"):
    router.classify(query)

  with pytest.raises(RuntimeError, match="malformed"):
    router.classify(query)

  with pytest.raises(RuntimeError, match="malformed"):
    router.classify(query)


def test_finalize_decisions_route_guards_nan_inf_and_zero_tokens(
    monkeypatch: pytest.MonkeyPatch,
):
  from http.server import BaseHTTPRequestHandler  # noqa: PLC0415
  from http.server import ThreadingHTTPServer  # noqa: PLC0415
  import math  # noqa: PLC0415
  import threading  # noqa: PLC0415

  sdk_payloads = [
      SimpleNamespace(
          answers={
              "route": SimpleNamespace(
                  choice="NATURAL_LANGUAGE",
                  confidence="NaN",
                  probabilities={
                      "KEYWORD": float("nan"),
                      "NATURAL_LANGUAGE": float("inf"),
                  },
              )
          },
          usage=SimpleNamespace(total_input_tokens=64),
      ),
      SimpleNamespace(
          answers={
              "route": SimpleNamespace(
                  choice="NATURAL_LANGUAGE",
                  confidence="inf",
                  probabilities={"KEYWORD": 0.1, "NATURAL_LANGUAGE": 0.9},
              )
          },
          usage=SimpleNamespace(total_input_tokens=64),
      ),
      SimpleNamespace(
          answers={
              "route": SimpleNamespace(
                  choice="NATURAL_LANGUAGE",
                  confidence=None,
                  probabilities={
                      "KEYWORD": 0.25,
                      "NATURAL_LANGUAGE": float("nan"),
                  },
              )
          },
          usage=SimpleNamespace(total_input_tokens=64),
      ),
  ]

  class _FakeDecisions:

    def create(self, *, model, input, questions):
      del model, input, questions
      return sdk_payloads.pop(0)

  sdk_router = QueryRouter(
      backend="decisions",
      decisions_client=SimpleNamespace(decisions=_FakeDecisions()),
  )
  query = "agent claimed refund succeeded even though tool failed"

  d_nan = sdk_router.classify(query)
  assert math.isfinite(d_nan.confidence)
  assert d_nan.confidence == pytest.approx(0.5)
  assert d_nan.probabilities == {}
  assert d_nan.mode == "HYBRID"

  d_inf = sdk_router.classify(query)
  assert math.isfinite(d_inf.confidence)
  assert d_inf.confidence == pytest.approx(0.5)
  assert d_inf.mode == "HYBRID"

  d_partial = sdk_router.classify(query)
  assert d_partial.probabilities == pytest.approx({"KEYWORD": 0.25})
  assert d_partial.confidence == pytest.approx(0.25)
  assert d_partial.mode == "HYBRID"

  rest_responses = [
      {
          "answers": {
              "route": {
                  "choice": "NATURAL_LANGUAGE",
                  "confidence": "NaN",
                  "probabilities": {
                      "KEYWORD": "inf",
                      "NATURAL_LANGUAGE": "-inf",
                  },
              }
          },
          "usage": {"total_input_tokens": 0, "prompt_token_count": 99},
      }
  ]

  class _RestHandler(BaseHTTPRequestHandler):

    def do_POST(self) -> None:
      length = int(self.headers.get("Content-Length", "0"))
      _ = self.rfile.read(length)
      payload = json.dumps(rest_responses.pop(0)).encode("utf-8")
      self.send_response(200)
      self.send_header("Content-Type", "application/json")
      self.send_header("Content-Length", str(len(payload)))
      self.end_headers()
      self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:
      del format, args

  server = ThreadingHTTPServer(("127.0.0.1", 0), _RestHandler)
  thread = threading.Thread(target=server.serve_forever, daemon=True)
  thread.start()
  base_url = f"http://127.0.0.1:{server.server_address[1]}/v1beta"
  try:
    # Even if SDK exposes `.decisions`, a custom decisions_api_base must use REST.
    monkeypatch.setattr(
        "analytics.query_router._build_decisions_client",
        lambda api_key: SimpleNamespace(
            decisions=SimpleNamespace(
                create=lambda **kwargs: (_ for _ in ()).throw(
                    AssertionError("custom base must not bypass REST")
                )
            )
        ),
    )
    rest_router = QueryRouter(
        backend="decisions",
        api_key="test-rest-key",
        decisions_api_base=base_url,
    )
    d_rest = rest_router.classify(query)
    assert d_rest.input_tokens == 0
    assert math.isfinite(d_rest.confidence)
    assert d_rest.confidence == pytest.approx(0.5)
    assert d_rest.probabilities == {}
    assert d_rest.mode == "HYBRID"
  finally:
    server.shutdown()
    server.server_close()


def test_run_e2e_demo_load_safe_env_preserves_caller_overrides(tmp_path: Path):
  import os  # noqa: PLC0415
  import subprocess  # noqa: PLC0415

  script_text = (_DEMO_DIR / "run_e2e_demo.sh").read_text(encoding="utf-8")
  match = re.search(r"(load_safe_env\(\)\s*\{.*?\n\})", script_text, re.DOTALL)
  assert match is not None
  env_file = tmp_path / ".env"
  env_file.write_text(
      "\n".join(
          [
              "PROJECT_ID=dotenv-project",
              "GEMINI_API_KEY=",
              "ROUTER_BACKEND=vertex",
              "GEMINI_DECISIONS_API_BASE=https://dotenv.example.com/v1beta",
              "DECISIONS_MODEL=gemini-decision-1",
              "UNSAFE_VAR=ignored",
          ]
      )
      + "\n",
      encoding="utf-8",
  )
  bash_cmd = (
      match.group(1)
      + '\nload_safe_env "$1"\n'
      + 'printf "%s\\n" '
      + '"${PROJECT_ID:-}" '
      + '"${GEMINI_API_KEY:-}" '
      + '"${ROUTER_BACKEND:-}" '
      + '"${GEMINI_DECISIONS_API_BASE:-}" '
      + '"${DECISIONS_MODEL:-}" '
      + '"${UNSAFE_VAR:-}"\n'
  )
  env = {
      "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
      "GEMINI_API_KEY": "caller-api-key",
      "ROUTER_BACKEND": "decisions",
      "GEMINI_DECISIONS_API_BASE": "http://127.0.0.1:8080/v1beta",
  }
  proc = subprocess.run(
      ["bash", "-euo", "pipefail", "-c", bash_cmd, "_", str(env_file)],
      env=env,
      capture_output=True,
      text=True,
      check=False,
  )
  assert proc.returncode == 0, proc.stderr
  assert proc.stdout.splitlines() == [
      "dotenv-project",
      "caller-api-key",
      "decisions",
      "http://127.0.0.1:8080/v1beta",
      "gemini-decision-1",
      "",
  ]


# ---------------------------------------------------------------------------
# 3. Materialization, Dual-Index Gate & Scale Seeder Tests
# ---------------------------------------------------------------------------


def test_build_materialize_eval_turns_sql_uses_adk_and_fallback(
    monkeypatch: pytest.MonkeyPatch,
):
  monkeypatch.setattr(
      "analytics.index_and_search._resolve_adk_eval_turn_builder",
      lambda: lambda events_fqn, raw_where_clause="": (
          f"SELECT session_id, invocation_id, trace FROM `{events_fqn}`"
          + (f" WHERE {raw_where_clause}" if raw_where_clause else "")
      ),
  )
  sql_adk, source_adk = build_materialize_eval_turns_sql(
      project_id="demo-proj",
      dataset_id="agent_ops",
      events_table="agent_events",
      eval_turns_table="eval_turns",
  )
  assert source_adk == "adk_build_eval_turn_query_sql"
  assert "CREATE OR REPLACE TABLE `demo-proj.agent_ops.eval_turns`" in sql_adk
  assert (
      "OPTIONS(labels=[('bqaa_demo', 'combined_cuj_trace_search')])" in sql_adk
  )
  assert "CAST(NULL AS STRING) AS _ai_index_trajectory" in sql_adk
  assert "BYTE_LENGTH(TO_JSON_STRING(b.trace)) AS raw_trace_bytes" in sql_adk
  assert "error_summary" in sql_adk

  # Real resolver imports package export `google.adk.plugins.build_eval_turn_query_sql`
  # without requiring bigquery_agent_analytics_plugin / pyarrow.
  fake_plugins_pkg = SimpleNamespace(
      build_eval_turn_query_sql=lambda events_fqn, raw_where_clause="": (
          f"SELECT session_id, invocation_id, trace FROM `{events_fqn}`"
      )
  )
  monkeypatch.undo()
  monkeypatch.setitem(
      sys.modules,
      "google.adk.plugins.bigquery_agent_analytics_plugin",
      None,
  )
  monkeypatch.setitem(sys.modules, "google.adk.plugins", fake_plugins_pkg)
  from analytics.index_and_search import _resolve_adk_eval_turn_builder  # noqa: PLC0415

  assert (
      _resolve_adk_eval_turn_builder()
      is fake_plugins_pkg.build_eval_turn_query_sql
  )

  monkeypatch.setattr(
      "analytics.index_and_search._resolve_adk_eval_turn_builder",
      lambda: None,
  )
  sql_fb, source_fb = build_materialize_eval_turns_sql(
      project_id="demo-proj",
      dataset_id="agent_ops",
      events_table="agent_events",
      eval_turns_table="eval_turns",
  )
  assert source_fb == "sql_01_materialize_eval_turns_fallback"
  assert "CREATE OR REPLACE TABLE `demo-proj.agent_ops.eval_turns`" in sql_fb
  assert (
      "OPTIONS(labels=[('bqaa_demo', 'combined_cuj_trace_search')])" in sql_fb
  )
  assert "CAST(NULL AS STRING) AS _ai_index_trajectory" in sql_fb


def _mock_bq_client_for_counts(row_count: int, ready_count: int):
  executed_sql = []

  def fake_query(sql_text, job_config=None):
    executed_sql.append(sql_text)
    job = MagicMock()
    job.total_bytes_processed = 1024
    job.slot_millis = 12
    job._properties = {"statistics": {"query": {}}}
    if "COUNT(*)" in sql_text:
      job.result.return_value = [{"row_count": row_count, "ready": ready_count}]
    else:
      job.result.return_value = []
    return job

  client = MagicMock()
  client.query.side_effect = fake_query
  return client, executed_sql


def test_setup_dual_indexes_gates_ivf_at_5000_rows():
  bq_small, sql_small = _mock_bq_client_for_counts(row_count=8, ready_count=0)
  status_small = setup_dual_indexes(
      bq_small,
      project_id="demo-proj",
      dataset_id="agent_ops",
      eval_turns_table="eval_turns",
      embed_wait_seconds=0,
  )
  assert status_small["vector_index"] == "SKIPPED_BELOW_5000_ROWS"
  assert status_small["row_count"] == 8
  assert not any("CREATE VECTOR INDEX" in s for s in sql_small)

  bq_large, sql_large = _mock_bq_client_for_counts(
      row_count=5008, ready_count=5008
  )
  status_large = setup_dual_indexes(
      bq_large,
      project_id="demo-proj",
      dataset_id="agent_ops",
      eval_turns_table="eval_turns",
      embed_wait_seconds=0,
  )
  assert status_large["vector_index"] == "CREATED"
  assert status_large["embeddings_ready"] == 5008
  assert any("CREATE VECTOR INDEX" in s for s in sql_large)


def test_seed_scale_benchmark_generates_deterministic_25_col_rows():
  rows = generate_synthetic_eval_turns(count=25)
  assert len(rows) == 25
  required_cols = {
      "session_id",
      "invocation_id",
      "turn_index",
      "user_id",
      "root_agent",
      "turn_start_timestamp",
      "turn_end_timestamp",
      "tool_call_count",
      "tool_error_count",
      "llm_error_count",
      "agent_error_count",
      "has_error",
      "has_truncated_content",
      "has_tool_loop",
      "max_consecutive_identical_tool_calls",
      "distinct_tools_used",
      "total_prompt_tokens",
      "total_completion_tokens",
      "total_tokens",
      "turn_latency_ms",
      "eval_ready",
      "trace",
      "error_summary",
      "raw_trace_bytes",
      "_ai_index_trajectory",
  }
  assert required_cols.issubset(rows[0].keys())
  assert rows[0]["raw_trace_bytes"] > len(rows[0]["_ai_index_trajectory"])
  assert rows == generate_synthetic_eval_turns(count=25)


def test_run_index_and_search_offline_end_to_end(tmp_path: Path):
  input_json = tmp_path / "live_demo_results.json"
  input_json.write_text(
      json.dumps({"eval_turns": _sample_turn_rows()}), encoding="utf-8"
  )
  output_json = tmp_path / "search_results.json"
  # Check `build_candidate_keys_param` serializes cleanly as ARRAY<STRUCT>.
  param = build_candidate_keys_param(_sample_turn_rows()[:2])
  api_repr = param.to_api_repr()
  serialized = json.loads(json.dumps(api_repr))
  assert serialized["parameterType"]["type"] == "ARRAY"
  assert serialized["parameterType"]["arrayType"]["type"] == "STRUCT"
  assert len(serialized["parameterValue"]["arrayValues"]) == 2

  # Check `split_sql_statements` ignores semicolons inside `--` comments.
  stmts = split_sql_statements("-- comment; ignored\nSELECT 1;\nSELECT 2;")
  assert stmts == ["SELECT 1", "SELECT 2"]

  # Offline end-to-end check.
  report = run_index_and_search(
      input_json=input_json,
      output_json=output_json,
      offline=True,
  )
  assert output_json.is_file()
  assert report["index_status"]["vector_index"] == "SKIPPED_BELOW_5000_ROWS"
  assert len(report["queries"]) == 5
  modes = {
      q["query_id"]: q["router_decision"]["mode"] for q in report["queries"]
  }
  assert modes["q1_exact_error"] == "KEYWORD"
  assert modes["q2_quoted_phrase"] == "KEYWORD"
  assert modes["q3_nl_contradiction"] == "NATURAL_LANGUAGE"
  assert modes["q4_nl_retry_loop"] == "NATURAL_LANGUAGE"
  assert modes["q5_hybrid_mixed"] == "HYBRID"

  # Layer 3 contradiction verification flags silent contradiction cleanly.
  q3 = next(
      q for q in report["queries"] if q["query_id"] == "q3_nl_contradiction"
  )
  verifications = {v["session_id"]: v for v in q3["layer3_verifications"]}
  assert (
      verifications["sess_03_contradiction"]["is_silent_contradiction"] is True
  )
  if "sess_01_healthy" in verifications:
    assert verifications["sess_01_healthy"]["is_silent_contradiction"] is False


def test_sanitize_search_query_for_bq_and_live_session_filter(tmp_path: Path):
  q5_sanitized = sanitize_search_query_for_bq(
      "why did issue_refund succeed after CARD_EXPIRED?", mode=ROUTE_HYBRID
  )
  assert "?" not in q5_sanitized
  assert "issue_refund" in q5_sanitized
  assert "CARD_EXPIRED" in q5_sanitized
  assert " OR " in q5_sanitized
  assert "why" not in q5_sanitized.lower().split()
  assert "did" not in q5_sanitized.lower().split()

  assert (
      sanitize_search_query_for_bq(
          '"refund has been processed"', mode=ROUTE_KEYWORD
      )
      == '"refund has been processed"'
  )
  assert (
      sanitize_search_query_for_bq("ORD-8841?", mode=ROUTE_KEYWORD)
      == '"ORD-8841"'
  )

  # Mocked live run verifies session_id filtering and @keyword_query binding.
  recorded_calls = []

  def fake_query(sql_text, job_config=None):
    recorded_calls.append((sql_text, job_config))
    job = MagicMock()
    job.total_bytes_processed = 512
    job.slot_millis = 4
    job._properties = {"statistics": {"query": {}}}
    if "COUNT(*)" in sql_text:
      job.result.return_value = [{"row_count": 2, "ready": 2}]
    elif "WHERE _ai_index_trajectory IS NULL" in sql_text:
      job.result.return_value = []
    elif "AI.GENERATE(" in sql_text:
      job.result.return_value = []
    else:
      job.result.return_value = []
    return job

  bq_client = MagicMock()
  bq_client.query.side_effect = fake_query
  bq_client.get_table.return_value = SimpleNamespace(
      num_bytes=2048, labels={"bqaa_demo": "combined_cuj_trace_search"}
  )

  live_input = tmp_path / "live_demo_results.json"
  live_input.write_text(
      json.dumps({"session_ids": ["sess_run1_a", "sess_run1_b"]}),
      encoding="utf-8",
  )
  out_json = tmp_path / "live_search.json"
  router = QueryRouter(backend="offline", offline=True)
  run_index_and_search(
      project_id="demo-proj",
      dataset_id="agent_ops",
      input_json=live_input,
      output_json=out_json,
      embed_wait_seconds=0,
      offline=False,
      bq_client=bq_client,
      router=router,
  )

  materialize_sql = recorded_calls[0][0]
  assert "CREATE OR REPLACE TABLE" in materialize_sql
  assert "session_id IN ('sess_run1_a', 'sess_run1_b')" in materialize_sql

  hybrid_call = next(
      (sql_s, cfg) for sql_s, cfg in recorded_calls if "KeywordRanked" in sql_s
  )
  param_map = {p.name: p.value for p in hybrid_call[1].query_parameters}
  assert "?" not in param_map["keyword_query"]
  assert "issue_refund" in param_map["keyword_query"]
  assert "CARD_EXPIRED" in param_map["keyword_query"]
  assert "?" in param_map["search_query"]

  # Negative check (#check-can-fail): unsafe session_id raises ValueError.
  bad_input = tmp_path / "bad_live.json"
  bad_input.write_text(
      json.dumps({"session_ids": ["bad'; DROP TABLE x;--"]}), encoding="utf-8"
  )
  with pytest.raises(ValueError, match="Unsafe session_id"):
    run_index_and_search(
        project_id="demo-proj",
        dataset_id="agent_ops",
        input_json=bad_input,
        output_json=out_json,
        embed_wait_seconds=0,
        offline=False,
        bq_client=bq_client,
        router=router,
    )


# ---------------------------------------------------------------------------
# 4. Pattern-to-Eval & 8-Lever Cost Cascade Tests
# ---------------------------------------------------------------------------


def test_eval_suite_health_fails_when_unbalanced_and_passes_when_balanced():
  # Negative check (#check-can-fail): negative-only suite emits health warning.
  unbalanced = EvalSuite(name="unbalanced_only_negatives")
  patterns = discover_patterns_offline(_sample_turn_rows())
  for idx, p in enumerate(patterns):
    sample = p["sample_turns"][0]
    from bigquery_agent_analytics import EvalCategory
    from bigquery_agent_analytics import EvalTaskDef

    unbalanced.add_task(
        EvalTaskDef(
            task_id=f"neg_{idx}",
            session_id=sample["session_id"],
            description=p["pattern_class"],
            category=EvalCategory.REGRESSION,
            expected_trajectory=[{"tool_name": "issue_refund", "args": {}}],
            is_positive_case=False,
        )
    )
  assert unbalanced.check_health().warnings != []

  # Balanced generated suite passes check_health() with zero warnings and uses
  # only genuine session IDs from input rows (never fabricated IDs).
  sample_rows = _sample_turn_rows()
  known_sessions = {str(r["session_id"]) for r in sample_rows}
  suite, health = build_eval_suite_from_patterns(
      patterns=patterns,
      turn_rows=sample_rows,
  )
  assert health.warnings == []
  assert 0.3 <= health.balance_ratio <= 0.7
  suite_tasks = suite.get_tasks()
  assert {t.session_id for t in suite_tasks}.issubset(known_sessions)
  assert not any(
      t.session_id.startswith("cap_baseline_healthy_")
      or t.session_id.startswith("baseline_guard_")
      for t in suite_tasks
  )
  covered_patterns = {
      tag.split(":", 1)[1]
      for t in suite_tasks
      for tag in t.tags
      if tag.startswith("pattern:") and not tag.endswith("HEALTHY_BASELINE")
  }
  assert covered_patterns == {
      "EXACT_ERROR_SPIKE",
      "SILENT_CONTRADICTION_CANDIDATE",
      "TOOL_LOOP",
      "TOKEN_BLOAT",
  }
  roundtrip = EvalSuite.from_json(suite.to_json())
  assert roundtrip.check_health().warnings == []


def test_build_eval_suite_never_fabricates_sessions_when_no_healthy_rows():
  # Negative check (#check-can-fail): all-failure inputs produce honest imbalance
  # warnings rather than inventing synthetic cap_baseline_healthy_* session IDs.
  failure_only = [r for r in _sample_turn_rows() if r["has_error"]]
  patterns = discover_patterns_offline(failure_only)
  suite, health = build_eval_suite_from_patterns(
      patterns=patterns,
      turn_rows=failure_only,
  )
  suite_tasks = suite.get_tasks()
  assert len(suite_tasks) > 0
  assert all(not t.is_positive_case for t in suite_tasks)
  assert not any(
      t.session_id.startswith("cap_baseline_healthy_") for t in suite_tasks
  )
  assert health.warnings != []


def test_eval_cost_cascade_monotonic_reductions():
  fake_search_report = {
      "queries": [
          {"router_decision": {"latency_ms": 0.0}},
          {"router_decision": {"latency_ms": 0.0}},
          {"router_decision": {"latency_ms": 2500.0}},
          {"router_decision": {"latency_ms": 2500.0}},
          {"router_decision": {"latency_ms": 0.0}},
      ]
  }
  cascade = compute_eval_cost_cascade(
      turn_rows=_sample_turn_rows(),
      search_report=fake_search_report,
      top_k=5,
  )
  assert len(cascade) == 4
  arm1, arm2, arm3, arm4 = cascade
  for arm in cascade:
    assert arm["estimation_mode"] == "MODELED_PROJECTION"
    assert arm["pricing_model"]["compact_judge_in_per_1m_usd"] == 0.30
    assert arm["pricing_model"]["compact_judge_out_per_1m_usd"] == 2.50
  assert arm4["tokens_per_1k_evals"] < arm3["tokens_per_1k_evals"]
  assert arm3["tokens_per_1k_evals"] < arm2["tokens_per_1k_evals"]
  assert arm2["tokens_per_1k_evals"] < arm1["tokens_per_1k_evals"]

  assert arm4["cost_per_1k_usd"] < arm3["cost_per_1k_usd"]
  assert arm3["cost_per_1k_usd"] < arm2["cost_per_1k_usd"]
  assert arm2["cost_per_1k_usd"] < arm1["cost_per_1k_usd"]
  assert arm3["latency_ms_p50"] <= arm2["latency_ms_p50"]
  assert arm4["savings_vs_naive_pct"] > 90.0

  # Regression: no artificial avg_s3_tokens * 4.0 S1 inflation when S1 is small.
  tiny_cascade = compute_eval_cost_cascade(
      turn_rows=[
          {
              "raw_trace_bytes": 400,
              "_ai_index_trajectory": "x" * 800,
              "has_error": True,
          }
      ]
  )
  assert tiny_cascade[0]["measured_inputs"]["avg_s1_tokens"] == 100.0
  assert tiny_cascade[0]["measured_inputs"]["avg_s3_tokens"] == 200.0


def test_run_pattern_to_eval_offline_writes_suite_and_report(tmp_path: Path):
  input_json = tmp_path / "live_demo_results.json"
  input_json.write_text(
      json.dumps({"eval_turns": _sample_turn_rows()}), encoding="utf-8"
  )
  report_json = tmp_path / "pattern_eval_report.json"
  suite_json = tmp_path / "eval_dataset_generated.json"

  report = run_pattern_to_eval(
      input_json=input_json,
      output_json=report_json,
      eval_suite_output=suite_json,
      offline=True,
  )
  assert report_json.is_file()
  assert suite_json.is_file()
  assert report["suite_health"]["warnings"] == []
  assert len(report["discovered_patterns"]) >= 4
  loaded = EvalSuite.from_json(suite_json.read_text(encoding="utf-8"))
  assert loaded.check_health().warnings == []


# ---------------------------------------------------------------------------
# 5. Blind-Review Hardening & Edge-Case Regression Tests
# ---------------------------------------------------------------------------


def test_sanitize_search_query_strips_reserved_ops_and_backticks():
  cleaned_kw = sanitize_search_query_for_bq(
      "refund AND card OR `ORD-8841` NOT failed +=<>|~^", mode=ROUTE_KEYWORD
  )
  tokens_kw = cleaned_kw.split()
  for op in ("AND", "OR", "NOT"):
    assert op not in tokens_kw
  for bad_ch in ("`", "+", "=", "<", ">", "|", "~", "^"):
    assert bad_ch not in cleaned_kw
  assert "refund" in cleaned_kw
  assert '"ORD-8841"' in cleaned_kw

  # Only-operators input resolves to empty string so caller uses metadata filter.
  assert sanitize_search_query_for_bq("AND OR NOT", mode=ROUTE_KEYWORD) == ""
  assert sanitize_search_query_for_bq("AND OR NOT", mode=ROUTE_HYBRID) == ""

  # BigQuery SEARCH reserved keywords IN and NEAR are quoted cleanly so
  # queries like 'error IN billing NEAR timeout' never trigger parser errors.
  kw_in_near = sanitize_search_query_for_bq(
      "error IN billing NEAR timeout", mode=ROUTE_KEYWORD
  )
  assert '"IN"' in kw_in_near.split()
  assert '"NEAR"' in kw_in_near.split()

  hyb_in_near = sanitize_search_query_for_bq(
      "CARD_EXPIRED IN billing NEAR timeout", mode=ROUTE_HYBRID
  )
  assert '"IN"' in hyb_in_near
  assert '"NEAR"' in hyb_in_near


def test_query_router_handles_null_confidence():
  class FakeNullConfModels:

    def generate_content(self, *, model, contents, config):
      del model, contents, config
      return SimpleNamespace(
          text='{"route": "KEYWORD", "confidence": null, "reason": "ok"}',
          usage_metadata=SimpleNamespace(
              prompt_token_count=20, candidates_token_count=10
          ),
      )

  router = QueryRouter(
      backend="vertex",
      vertex_client=SimpleNamespace(models=FakeNullConfModels()),
  )
  decision = router.classify(
      "agent failed silently without acknowledging tool error"
  )
  assert decision.confidence == pytest.approx(0.5)
  assert decision.mode == ROUTE_HYBRID

  class FakeNullPayloadModels:

    def generate_content(self, *, model, contents, config):
      del model, contents, config
      return SimpleNamespace(text="null", usage_metadata=None)

  router_null = QueryRouter(
      backend="vertex",
      vertex_client=SimpleNamespace(models=FakeNullPayloadModels()),
  )
  dec_null = router_null.classify(
      "agent got stuck repeating search calls endlessly"
  )
  assert dec_null.confidence == pytest.approx(0.5)
  assert dec_null.mode == ROUTE_HYBRID


def test_hybrid_no_rerank_and_embeddings_zero_fallback(tmp_path: Path):
  # Offline --no-rerank emits NO_RERANK SQL template names.
  offline_out = tmp_path / "offline_no_rerank.json"
  offline_rep = run_index_and_search(
      output_json=offline_out, rerank=False, offline=True
  )
  templates = {q["query_id"]: q["sql_template"] for q in offline_rep["queries"]}
  assert templates["q3_nl_contradiction"] == "PATH_B_AI_SEARCH_NO_RERANK"
  assert templates["q5_hybrid_mixed"] == "PATH_C_HYBRID_RRF_NO_RERANK"

  # Live mock with ready=2 and rerank=False executes PATH_C_HYBRID_RRF_NO_RERANK.
  executed_sql_no_rerank = []

  def fake_no_rerank_query(sql_text, job_config=None):
    del job_config
    executed_sql_no_rerank.append(sql_text)
    job = MagicMock()
    job.total_bytes_processed = 100
    job.slot_millis = 5
    job._properties = {"statistics": {"query": {}}}
    if "COUNT(*)" in sql_text:
      job.result.return_value = [{"row_count": 2, "ready": 2}]
    else:
      job.result.return_value = []
    return job

  bq_no_rerank = MagicMock()
  bq_no_rerank.query.side_effect = fake_no_rerank_query
  bq_no_rerank.get_table.return_value = SimpleNamespace(
      num_bytes=512, labels={"bqaa_demo": "combined_cuj_trace_search"}
  )
  router = QueryRouter(backend="offline", offline=True)
  rep_no_rerank = run_index_and_search(
      project_id="demo-proj",
      dataset_id="agent_ops",
      output_json=tmp_path / "live_no_rerank.json",
      rerank=False,
      embed_wait_seconds=0,
      offline=False,
      bq_client=bq_no_rerank,
      router=router,
  )
  q5_nr = next(
      q for q in rep_no_rerank["queries"] if q["query_id"] == "q5_hybrid_mixed"
  )
  assert q5_nr["sql_template"] == "PATH_C_HYBRID_RRF_NO_RERANK"
  assert any("rerank => FALSE" in s for s in executed_sql_no_rerank)

  # Live mock with embeddings_ready=0 routes HYBRID through Python RRF
  # fallback and sums both kw_job + sem_job telemetry.
  def fake_zero_ready_query(sql_text, job_config=None):
    del job_config
    job = MagicMock()
    job._properties = {"statistics": {"query": {}}}
    if "COUNT(*)" in sql_text:
      job.total_bytes_processed = 64
      job.slot_millis = 2
      job.result.return_value = [{"row_count": 2, "ready": 0}]
    elif "AND SEARCH(" in sql_text:
      job.total_bytes_processed = 1000
      job.slot_millis = 10
      job._properties = {
          "statistics": {
              "query": {
                  "searchStatistics": {
                      "indexUnusedReasons": [{"code": "KW_SMALL"}]
                  }
              }
          }
      }
      job.result.return_value = [
          {
              "session_id": "s_kw",
              "invocation_id": "inv_1",
              "turn_index": 0,
              "root_agent": "support_agent",
              "has_error": True,
              "turn_latency_ms": 100,
              "_ai_index_trajectory": "kw",
          }
      ]
    elif "ML.DISTANCE(" in sql_text:
      job.total_bytes_processed = 2500
      job.slot_millis = 25
      job._properties = {
          "statistics": {
              "query": {
                  "vectorSearchStatistics": {
                      "indexUnusedReasons": [{"code": "VEC_SMALL"}]
                  }
              }
          }
      }
      job.result.return_value = [
          {
              "session_id": "s_sem",
              "invocation_id": "inv_2",
              "turn_index": 0,
              "root_agent": "support_agent",
              "has_error": True,
              "turn_latency_ms": 200,
              "_ai_index_trajectory": "sem",
              "distance": 0.12,
          }
      ]
    else:
      job.total_bytes_processed = 50
      job.slot_millis = 1
      job.result.return_value = []
    return job

  bq_zero = MagicMock()
  bq_zero.query.side_effect = fake_zero_ready_query
  bq_zero.get_table.return_value = SimpleNamespace(
      num_bytes=512, labels={"bqaa_demo": "combined_cuj_trace_search"}
  )
  rep_zero = run_index_and_search(
      project_id="demo-proj",
      dataset_id="agent_ops",
      output_json=tmp_path / "live_zero_ready.json",
      embed_wait_seconds=0,
      offline=False,
      bq_client=bq_zero,
      router=router,
  )
  q5_zero = next(
      q for q in rep_zero["queries"] if q["query_id"] == "q5_hybrid_mixed"
  )
  assert q5_zero["sql_template"] == "PATH_C_HYBRID_PYTHON_FALLBACK"
  assert q5_zero["step1_bytes_processed"] == 3500
  assert q5_zero["slot_ms"] == 35
  assert q5_zero["search_unused_reasons"] == ["KW_SMALL"]
  assert q5_zero["vector_unused_reasons"] == ["VEC_SMALL"]

  # Runtime exception during PATH_C_HYBRID_RRF (when ready=2) also falls back
  # to PATH_C_HYBRID_PYTHON_FALLBACK with summed kw+sem bytes and slots.
  def fake_raise_hybrid_query(sql_text, job_config=None):
    if "KeywordRanked" in sql_text:
      raise RuntimeError("AI.SEARCH transient error")
    if "COUNT(*)" in sql_text:
      job = MagicMock()
      job.total_bytes_processed = 64
      job.slot_millis = 2
      job._properties = {"statistics": {"query": {}}}
      job.result.return_value = [{"row_count": 2, "ready": 2}]
      return job
    return fake_zero_ready_query(sql_text, job_config)

  bq_exc = MagicMock()
  bq_exc.query.side_effect = fake_raise_hybrid_query
  bq_exc.get_table.return_value = SimpleNamespace(
      num_bytes=512, labels={"bqaa_demo": "combined_cuj_trace_search"}
  )
  rep_exc = run_index_and_search(
      project_id="demo-proj",
      dataset_id="agent_ops",
      output_json=tmp_path / "live_exc_fallback.json",
      embed_wait_seconds=0,
      offline=False,
      bq_client=bq_exc,
      router=router,
  )
  q5_exc = next(
      q for q in rep_exc["queries"] if q["query_id"] == "q5_hybrid_mixed"
  )
  assert q5_exc["sql_template"] == "PATH_C_HYBRID_PYTHON_FALLBACK"
  assert q5_exc["step1_bytes_processed"] == 3500
  assert q5_exc["slot_ms"] == 35

  # Deterministic tie-breaking in _fuse_rrf_rows: lower distance wins first,
  # then session_id lexicographically when RRF score ties.
  fused = _fuse_rrf_rows(
      kw_rows=[
          {"session_id": "b_sess", "invocation_id": "inv_1"},
          {"session_id": "a_sess", "invocation_id": "inv_1"},
      ],
      sem_rows=[
          {"session_id": "c_sess", "invocation_id": "inv_1", "distance": 0.25},
          {"session_id": "b_sess", "invocation_id": "inv_1", "distance": 0.50},
      ],
      top_k=5,
  )
  assert fused[0]["session_id"] == "b_sess"
  assert fused[0]["distance"] == 0.50
  assert [r["session_id"] for r in fused[1:]] == ["c_sess", "a_sess"]


def test_bq_identifier_validation_and_location_threading(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
  bad_proj = "bad; DROP TABLE eval_turns;--"
  with pytest.raises(ValueError, match="Invalid BigQuery project_id"):
    discover_patterns_bigquery(
        MagicMock(), project_id=bad_proj, dataset_id="agent_ops"
    )
  with pytest.raises(ValueError, match="Invalid BigQuery dataset_id"):
    run_pattern_to_eval(
        project_id="demo-proj", dataset_id="bad-dataset", offline=False
    )
  with pytest.raises(ValueError, match="Invalid BigQuery table_id"):
    seed_scale_rows_to_bigquery(
        MagicMock(),
        project_id="demo-proj",
        dataset_id="agent_ops",
        eval_turns_table="bad-table",
        count=1,
    )

  from agent import run_live_demo  # noqa: PLC0415

  with pytest.raises(ValueError, match="Invalid BigQuery project_id"):
    run_live_demo.run_demo(
        project_id=bad_proj, dataset_id="agent_ops", mock_live=False
    )
  with pytest.raises(ValueError, match="Invalid BigQuery table_id"):
    run_live_demo._materialize_and_fetch_live_eval_turns(
        project_id="demo-proj",
        dataset_id="agent_ops",
        table_id="agent_events",
        eval_turns_table="bad;table",
        location="US",
        session_ids=["s1"],
    )

  # Thread --location cleanly into bigquery.Client in run_pattern_to_eval.
  captured_clients = []

  class FakeBQClient:

    def __init__(self, *, project, location="US"):
      captured_clients.append((project, location))

    def query(self, sql_text, job_config=None):
      del sql_text, job_config
      job = MagicMock()
      job.result.return_value = []
      return job

  import google.cloud.bigquery as bq_mod  # noqa: PLC0415

  monkeypatch.setattr(bq_mod, "Client", FakeBQClient)
  run_pattern_to_eval(
      project_id="demo-proj",
      dataset_id="agent_ops",
      location="EU",
      output_json=tmp_path / "p_out.json",
      eval_suite_output=tmp_path / "s_out.json",
      offline=False,
  )
  assert captured_clients == [("demo-proj", "EU")]


def test_seed_scale_rows_uses_recent_default_timestamp():
  from datetime import datetime  # noqa: PLC0415
  from datetime import timedelta  # noqa: PLC0415
  from datetime import timezone  # noqa: PLC0415

  captured_chunks = []
  client = MagicMock()
  client.get_table.return_value = SimpleNamespace(
      schema=[], labels={"bqaa_demo": "combined_cuj_trace_search"}
  )
  client.load_table_from_json.side_effect = (
      lambda chunk, table_fqn, job_config=None: (
          captured_chunks.extend(chunk),
          MagicMock(),
      )[1]
  )
  inserted = seed_scale_rows_to_bigquery(
      client,
      project_id="demo-proj",
      dataset_id="agent_ops",
      count=3,
      location="US",
  )
  assert inserted == 3
  assert len(captured_chunks) == 3
  now_utc = datetime.now(timezone.utc)
  seeded_ts = datetime.fromisoformat(
      str(captured_chunks[0]["turn_start_timestamp"])
  )
  assert (
      (now_utc - timedelta(hours=3))
      <= seeded_ts
      <= (now_utc - timedelta(hours=1))
  )
  assert seeded_ts.isoformat() != "2026-11-24T00:00:00+00:00"


def test_healthy_multitool_live_turns_stay_balanced_above_9k_tokens():
  # Regression test matching observed live ADK notebook metrics:
  # healthy multi-tool turns (~9k-9.3k tokens, ~11.5k-12.2k bytes) remain in
  # healthy_rows rather than being misclassified as TOKEN_BLOAT.
  base_rows = {r["session_id"]: dict(r) for r in _sample_turn_rows()}
  live_parity_rows = [
      dict(
          base_rows["sess_01_healthy"],
          session_id="sess_01_healthy_refund",
          raw_trace_bytes=11540,
          total_tokens=9054,
      ),
      dict(base_rows["sess_02_exact_err"]),
      dict(base_rows["sess_03_contradiction"]),
      dict(
          base_rows["sess_04_retry_loop"],
          session_id="sess_04_deadline_retry_loop",
          raw_trace_bytes=12324,
          total_tokens=11428,
      ),
      dict(
          base_rows["sess_05_bloat"],
          session_id="sess_05_bloated_audit_blob",
          raw_trace_bytes=24777,
          total_tokens=12239,
      ),
      dict(
          base_rows["sess_06_healthy_policy"],
          session_id="sess_06_multi_tool_policy_check",
          raw_trace_bytes=12216,
          total_tokens=9300,
      ),
  ]
  patterns = discover_patterns_offline(live_parity_rows)
  by_class = {p["pattern_class"]: p for p in patterns}
  bloat_sessions = {
      t["session_id"] for t in by_class["TOKEN_BLOAT"]["sample_turns"]
  }
  assert bloat_sessions == {"sess_05_bloated_audit_blob"}

  suite, health = build_eval_suite_from_patterns(
      patterns=patterns, turn_rows=live_parity_rows
  )
  suite_tasks = suite.get_tasks()
  pos_sessions = {t.session_id for t in suite_tasks if t.is_positive_case}
  assert pos_sessions == {
      "sess_01_healthy_refund",
      "sess_06_multi_tool_policy_check",
  }
  assert len(pos_sessions) >= 2
  assert health.balance_ratio >= 0.3
  assert health.warnings == []


def test_shell_scripts_offline_permissions_and_scale_cleanup():
  setup_text = (_DEMO_DIR / "setup.sh").read_text(encoding="utf-8")
  assert setup_text.count('chmod 600 "$ENV_FILE"') == 2
  assert "google-adk[bigquery-analytics]" in setup_text
  assert "bigquery_agent_analytics_plugin" in setup_text
  assert "${EXISTING_LOC,,}" not in setup_text
  assert "tr '[:upper:]' '[:lower:]'" in setup_text
  assert "bqaa_demo=combined_cuj_trace_search" in setup_text

  e2e_text = (_DEMO_DIR / "run_e2e_demo.sh").read_text(encoding="utf-8")
  assert 'if [[ "$OFFLINE_MODE" != "true" ]]; then' in e2e_text
  assert '--location "$BQ_LOCATION"' in e2e_text
  assert "load_safe_env" in e2e_text
  assert "umask 077" in e2e_text

  reset_text = (_DEMO_DIR / "reset.sh").read_text(encoding="utf-8")
  assert "STARTS_WITH(session_id, 'scale_synth_')" in reset_text
  assert "--delete-scale-rows" in reset_text
  assert "${EVAL_TURNS_TABLE}_scale" not in reset_text
  assert "load_safe_env" in reset_text
  assert "check_table_owned_by_demo" in reset_text
  assert "FAILURES" in reset_text


def test_staging_ownership_verifier_error_and_partial_embeddings(
    tmp_path: Path,
):
  from analytics.index_and_search import assert_demo_table_ownership_or_absent  # noqa: PLC0415
  from analytics.index_and_search import populate_trajectories_in_bigquery  # noqa: PLC0415

  # Ownership check rejects unrelated pre-existing tables without demo label.
  unowned_client = MagicMock()
  unowned_client.get_table.return_value = SimpleNamespace(
      labels={"env": "prod"}
  )
  with pytest.raises(RuntimeError, match="missing demo ownership label"):
    assert_demo_table_ownership_or_absent(
        unowned_client, "demo-proj", "agent_ops", "eval_turns"
    )

  owned_client = MagicMock()
  owned_client.get_table.return_value = SimpleNamespace(
      labels={"bqaa_demo": "combined_cuj_trace_search"}
  )
  assert_demo_table_ownership_or_absent(
      owned_client, "demo-proj", "agent_ops", "eval_turns"
  )

  # populate_trajectories_in_bigquery creates unique per-run __stage_<hex> tables.
  stage_names = []
  pop_client = MagicMock()

  def _fake_pop_query(sql_text, job_config=None):
    del job_config
    job = MagicMock()
    if "WHERE _ai_index_trajectory IS NULL" in sql_text:
      job.result.return_value = [
          {
              "session_id": "s1",
              "invocation_id": "i1",
              "root_agent": "a",
              "has_error": False,
              "tool_call_count": 0,
              "tool_error_count": 0,
              "has_tool_loop": False,
              "turn_latency_ms": 10,
              "error_summary": "",
              "trace": {},
          }
      ]
    else:
      job.result.return_value = []
    return job

  def _fake_load(staged, stage_fqn, job_config=None):
    del staged, job_config
    stage_names.append(stage_fqn)
    return MagicMock()

  pop_client.query.side_effect = _fake_pop_query
  pop_client.load_table_from_json.side_effect = _fake_load
  populate_trajectories_in_bigquery(
      pop_client, project_id="demo-proj", dataset_id="agent_ops"
  )
  populate_trajectories_in_bigquery(
      pop_client, project_id="demo-proj", dataset_id="agent_ops"
  )
  assert len(stage_names) == 2
  assert stage_names[0] != stage_names[1]
  assert "__stage_" in stage_names[0]

  # Partial embeddings emit explicit warning + L3 error emits VERIFIER_ERROR.
  def fake_partial_and_verifier_err(sql_text, job_config=None):
    del job_config
    job = MagicMock()
    job.total_bytes_processed = 128
    job.slot_millis = 4
    job._properties = {"statistics": {"query": {}}}
    if "COUNT(*)" in sql_text:
      job.result.return_value = [{"row_count": 8, "ready": 3}]
    elif "AI.GENERATE(" in sql_text:
      job.result.return_value = [
          {
              "session_id": "s_err",
              "invocation_id": "i_err",
              "is_silent_contradiction": False,
              "pattern_label": None,
              "confidence": None,
              "evidence_snippet": None,
              "ai_generate_status": "QUOTA_EXCEEDED",
          }
      ]
    elif "KeywordRanked" in sql_text:
      job.result.return_value = [
          {
              "session_id": "s_err",
              "invocation_id": "i_err",
              "turn_index": 0,
              "root_agent": "a",
              "has_error": True,
              "turn_latency_ms": 100,
              "_ai_index_trajectory": "err",
              "rrf_score": 0.03,
          }
      ]
    else:
      job.result.return_value = []
    return job

  bq_err = MagicMock()
  bq_err.query.side_effect = fake_partial_and_verifier_err
  bq_err.get_table.return_value = SimpleNamespace(
      num_bytes=512, labels={"bqaa_demo": "combined_cuj_trace_search"}
  )
  with pytest.warns(RuntimeWarning, match="Partial embeddings ready"):
    rep = run_index_and_search(
        project_id="demo-proj",
        dataset_id="agent_ops",
        output_json=tmp_path / "partial_err.json",
        embed_wait_seconds=0,
        offline=False,
        bq_client=bq_err,
        router=QueryRouter(backend="offline", offline=True),
    )
  assert any(
      "Partial embeddings ready: 3/8" in w
      for w in rep["index_status"]["warnings"]
  )
  q5_res = next(q for q in rep["queries"] if q["query_id"] == "q5_hybrid_mixed")
  assert q5_res["layer3_verifications"][0]["pattern_label"] == "VERIFIER_ERROR"
  assert (
      q5_res["layer3_verifications"][0]["ai_generate_status"]
      == "QUOTA_EXCEEDED"
  )


# ---------------------------------------------------------------------------
# 6. PR Review Hardening Tests (Redaction, Ownership, Router, Scoring, Scale)
# ---------------------------------------------------------------------------


def test_fallback_extractor_and_populate_scrub_nested_secrets_and_error_summary():
  from analytics.index_and_search import _extract_s3_text  # noqa: PLC0415
  from analytics.index_and_search import populate_trajectories_in_bigquery  # noqa: PLC0415

  trace = {
      "turns": [
          {
              "events": [
                  {
                      "content": {
                          "role": "model",
                          "parts": [
                              {
                                  "function_call": {
                                      "name": "vault_login",
                                      "args": {
                                          "credentials": {
                                              "value": "fb_nested_cred"
                                          },
                                          "password": {
                                              "nested": "fb_nested_pw"
                                          },
                                          "auth": ["fb_auth_item"],
                                          "author": "customer_ops_agent",
                                      },
                                  }
                              },
                              {
                                  "function_response": {
                                      "name": "vault_login",
                                      "response": {
                                          "signing_key": "fb_sign_key",
                                          "detail": (
                                              "the password is fb_free_pw"
                                              " CARD_EXPIRED"
                                          ),
                                      },
                                  }
                              },
                              {
                                  "text": (
                                      "connection string is"
                                      " postgres://u:fb_db_pw@h/db"
                                  )
                              },
                          ],
                      }
                  }
              ]
          }
      ]
  }
  s3_fb = _extract_s3_text(
      trace, row={"root_agent": "customer_ops_agent", "has_error": True}
  )
  for secret in (
      "fb_nested_cred",
      "fb_nested_pw",
      "fb_auth_item",
      "fb_sign_key",
      "fb_free_pw",
      "fb_db_pw",
  ):
    assert secret not in s3_fb
  assert "CARD_EXPIRED" in s3_fb
  assert "customer_ops_agent" in s3_fb

  staged_rows: list[dict] = []
  executed_sql: list[str] = []
  pop_client = MagicMock()

  def _fake_query(sql_text, job_config=None):
    del job_config
    executed_sql.append(sql_text)
    job = MagicMock()
    if "WHERE _ai_index_trajectory IS NULL" in sql_text:
      job.result.return_value = [
          {
              "session_id": "s1",
              "invocation_id": "i1",
              "root_agent": "a",
              "has_error": True,
              "tool_call_count": 1,
              "tool_error_count": 1,
              "has_tool_loop": False,
              "turn_latency_ms": 10,
              "error_summary": "the password is bq_err_secret CARD_EXPIRED",
              "trace": {},
          }
      ]
    else:
      job.result.return_value = []
    return job

  def _fake_load(staged, stage_fqn, job_config=None):
    del stage_fqn, job_config
    staged_rows.extend(staged)
    return MagicMock()

  pop_client.query.side_effect = _fake_query
  pop_client.load_table_from_json.side_effect = _fake_load
  populate_trajectories_in_bigquery(
      pop_client, project_id="demo-proj", dataset_id="agent_ops"
  )
  assert len(staged_rows) == 1
  assert "bq_err_secret" not in str(staged_rows[0]["error_summary"])
  assert "CARD_EXPIRED" in str(staged_rows[0]["error_summary"])
  assert any("t.error_summary = s.error_summary" in s for s in executed_sql)


def test_ownership_guard_fail_closed_and_scale_seeder_validation():
  from analytics.index_and_search import assert_demo_table_ownership_or_absent  # noqa: PLC0415

  class _Forbidden(Exception):
    code = 403

  class _NotFound(Exception):
    code = 404

  # Forbidden/non-404 errors must re-raise, never pass as absent.
  err_client = MagicMock()
  err_client.get_table.side_effect = _Forbidden("access denied")
  with pytest.raises(_Forbidden):
    assert_demo_table_ownership_or_absent(
        err_client, "demo-proj", "agent_ops", "eval_turns"
    )

  # labels=None must fail closed.
  none_labels_client = MagicMock()
  none_labels_client.get_table.return_value = SimpleNamespace(labels=None)
  with pytest.raises(RuntimeError, match="missing demo ownership label"):
    assert_demo_table_ownership_or_absent(
        none_labels_client, "demo-proj", "agent_ops", "eval_turns"
    )

  # Confirmed 404 NotFound passes as absent.
  nf_client = MagicMock()
  nf_client.get_table.side_effect = _NotFound("missing")
  assert_demo_table_ownership_or_absent(
      nf_client, "demo-proj", "agent_ops", "eval_turns"
  )

  # seed_scale_rows_to_bigquery rejects count < 0 and unowned tables BEFORE DELETE.
  bad_count_client = MagicMock()
  with pytest.raises(ValueError, match="non-negative"):
    seed_scale_rows_to_bigquery(
        bad_count_client,
        project_id="demo-proj",
        dataset_id="agent_ops",
        count=-1,
    )
  bad_count_client.get_table.assert_not_called()
  bad_count_client.query.assert_not_called()

  unowned_seed_client = MagicMock()
  unowned_seed_client.get_table.return_value = SimpleNamespace(
      schema=[], labels={"owner": "other_team"}
  )
  with pytest.raises(RuntimeError, match="missing demo ownership label"):
    seed_scale_rows_to_bigquery(
        unowned_seed_client,
        project_id="demo-proj",
        dataset_id="agent_ops",
        count=5,
    )
  unowned_seed_client.query.assert_not_called()


def test_router_hardening_timeouts_host_allowlist_offline_auto_fallback_and_cost(
    monkeypatch: pytest.MonkeyPatch,
):
  from http.server import BaseHTTPRequestHandler  # noqa: PLC0415
  from http.server import ThreadingHTTPServer  # noqa: PLC0415
  import threading  # noqa: PLC0415

  from analytics.query_router import _build_decisions_client  # noqa: PLC0415
  from analytics.query_router import _build_vertex_client  # noqa: PLC0415
  from analytics.query_router import _parse_decisions_api_base  # noqa: PLC0415

  # 1. Client builders set vertexai=False (decisions) and 60_000 ms timeouts.
  client_kwargs: list[dict] = []

  class _FakeGenai:

    @staticmethod
    def Client(**kwargs):
      client_kwargs.append(kwargs)
      return SimpleNamespace(**kwargs)

  monkeypatch.setitem(sys.modules, "google.genai", _FakeGenai)
  monkeypatch.setattr("google.genai", _FakeGenai, raising=False)
  _build_decisions_client("k-test")
  _build_vertex_client(project_id="demo-proj", location="global")
  assert client_kwargs[0]["vertexai"] is False
  assert client_kwargs[0]["http_options"] == {
      "api_version": "v1beta",
      "timeout": 60_000,
  }
  assert client_kwargs[1]["vertexai"] is True
  assert client_kwargs[1]["http_options"] == {"timeout": 60_000}

  # 2. HTTPS base allowlist rejects non-googleapis hosts.
  with pytest.raises(ValueError, match="googleapis.com"):
    _parse_decisions_api_base("https://evil.example.com/v1beta")

  # 3. Offline mode ignores stale/invalid GEMINI_DECISIONS_API_BASE.
  monkeypatch.setenv("GEMINI_DECISIONS_API_BASE", "not a valid url")
  offline_router = QueryRouter(backend="offline", offline=True)
  assert offline_router.classify("agent repeated tool calls").layer == (
      "L1_OFFLINE_HEURISTIC"
  )
  monkeypatch.delenv("GEMINI_DECISIONS_API_BASE", raising=False)

  # 4. Straddling API key at byte 295-315 of HTTP 400 body leaks ZERO prefix.
  straddle_key = "STRADDLE_KEY_ABCDEF1234567890"
  body_400 = ("X" * 295) + straddle_key + " tail"

  class _Err400Handler(BaseHTTPRequestHandler):

    def do_POST(self) -> None:
      length = int(self.headers.get("Content-Length", "0"))
      _ = self.rfile.read(length)
      payload = body_400.encode("utf-8")
      self.send_response(400)
      self.send_header("Content-Type", "text/plain")
      self.send_header("Content-Length", str(len(payload)))
      self.end_headers()
      self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:
      del format, args

  server = ThreadingHTTPServer(("127.0.0.1", 0), _Err400Handler)
  thread = threading.Thread(target=server.serve_forever, daemon=True)
  thread.start()
  base_url = f"http://127.0.0.1:{server.server_address[1]}/v1beta"
  try:
    strict_router = QueryRouter(
        backend="decisions",
        api_key=straddle_key,
        decisions_api_base=base_url,
    )
    with pytest.raises(RuntimeError) as exc_info:
      strict_router.classify("agent got stuck calling lookup repeatedly")
    err_msg = str(exc_info.value)
    assert "STRAD" not in err_msg
    assert "STRADDLE_KEY" not in err_msg
    assert "[REDA" in err_msg

    # 5. backend="auto" degrades cleanly from Decisions error -> Vertex -> HYBRID.
    monkeypatch.setattr(
        "analytics.query_router._build_vertex_client",
        lambda **kwargs: (_ for _ in ()).throw(RuntimeError("vertex down")),
    )
    auto_router = QueryRouter(
        backend="auto",
        api_key=straddle_key,
        decisions_api_base=base_url,
    )
    with pytest.warns(RuntimeWarning):
      degraded = auto_router.classify(
          "agent got stuck calling lookup repeatedly"
      )
    assert degraded.mode == ROUTE_HYBRID
    assert degraded.layer == "L1_FALLBACK_HYBRID"
    assert degraded.confidence == pytest.approx(0.5)
    assert degraded.model == "fallback_hybrid"
  finally:
    server.shutdown()
    server.server_close()

  # 6. Dynamic cost_usd scales with reported input_tokens.
  token_counts = [0, 128, 1000]

  class _TokenDecisions:

    def create(self, *, model, input, questions):
      del model, input, questions
      return SimpleNamespace(
          answers={
              "route": SimpleNamespace(
                  choice="NATURAL_LANGUAGE",
                  confidence=0.95,
                  probabilities={"KEYWORD": 0.05, "NATURAL_LANGUAGE": 0.95},
              )
          },
          usage=SimpleNamespace(total_input_tokens=token_counts.pop(0)),
      )

  cost_router = QueryRouter(
      backend="decisions",
      decisions_client=SimpleNamespace(decisions=_TokenDecisions()),
  )
  d0 = cost_router.classify("agent repeated tool calls after timeout")
  d128 = cost_router.classify("agent repeated tool calls after timeout")
  d1000 = cost_router.classify("agent repeated tool calls after timeout")
  assert d0.cost_usd == 0.0
  assert d128.cost_usd == pytest.approx(0.000046)
  assert d1000.cost_usd == pytest.approx(0.00036)


def test_scale_session_filter_synthetic_exclusion_hybrid_order_and_suite_eval(
    tmp_path: Path,
):
  from analytics.pattern_to_eval import evaluate_suite_against_turns  # noqa: PLC0415

  # 1. Hybrid SQL CTEs enforce ORDER BY kw_rank ASC before LIMIT @candidate_k.
  search_queries = load_named_sql_queries(
      _SQL_DIR / "03_search_keyword_and_semantic.sql"
  )
  for qname in ("PATH_C_HYBRID_RRF", "PATH_C_HYBRID_RRF_NO_RERANK"):
    assert "ORDER BY kw_rank ASC" in search_queries[qname]

  # 2. sql/05_pattern_discovery.sql excludes scale_synth_* rows.
  pat_sql = (_SQL_DIR / "05_pattern_discovery.sql").read_text(encoding="utf-8")
  assert "NOT STARTS_WITH(session_id, 'scale_synth_')" in pat_sql

  # 3. run_index_and_search applies session_ids filter even with scale seeding.
  recorded_sql: list[str] = []

  def _fake_scale_query(sql_text, job_config=None):
    del job_config
    recorded_sql.append(sql_text)
    job = MagicMock()
    job.total_bytes_processed = 128
    job.slot_millis = 2
    job._properties = {"statistics": {"query": {}}}
    if "COUNT(*)" in sql_text:
      job.result.return_value = [{"row_count": 5002, "ready": 5002}]
    else:
      job.result.return_value = []
    return job

  bq_scale = MagicMock()
  bq_scale.query.side_effect = _fake_scale_query
  bq_scale.get_table.return_value = SimpleNamespace(
      num_bytes=512,
      schema=[],
      labels={"bqaa_demo": "combined_cuj_trace_search"},
  )
  live_in = tmp_path / "live_scale.json"
  live_in.write_text(
      json.dumps({"session_ids": ["sess_real_1", "sess_real_2"]}),
      encoding="utf-8",
  )
  run_index_and_search(
      project_id="demo-proj",
      dataset_id="agent_ops",
      input_json=live_in,
      output_json=tmp_path / "scale_search.json",
      with_vector_index_scale=True,
      embed_wait_seconds=0,
      offline=False,
      bq_client=bq_scale,
      router=QueryRouter(backend="offline", offline=True),
  )
  assert "session_id IN ('sess_real_1', 'sess_real_2')" in recorded_sql[0]

  # 4. Synthetic scale_synth_* rows are excluded from pattern discovery & EvalSuite.
  mixed_rows = _sample_turn_rows() + generate_synthetic_eval_turns(
      count=15, prefix="scale_synth"
  )
  patterns = discover_patterns_offline(mixed_rows)
  suite, _ = build_eval_suite_from_patterns(
      patterns=patterns, turn_rows=mixed_rows
  )
  assert not any(
      t.session_id.startswith("scale_synth_") for t in suite.get_tasks()
  )

  # 5. Real EvalSuite threshold scoring catches silent contradictions and passes
  #    explicit error acknowledgments.
  from bigquery_agent_analytics import EvalCategory  # noqa: PLC0415
  from bigquery_agent_analytics import EvalTaskDef  # noqa: PLC0415

  scoring_suite = EvalSuite(name="scoring_check")
  scoring_suite.add_task(
      EvalTaskDef(
          task_id="t_contradiction",
          session_id="sess_bad_claim",
          description="Must acknowledge error",
          category=EvalCategory.REGRESSION,
          thresholds={
              "error_acknowledgment_rate": 1.0,
              "contradiction_rate": 0.0,
          },
          is_positive_case=False,
      )
  )
  scoring_suite.add_task(
      EvalTaskDef(
          task_id="t_honest_err",
          session_id="sess_good_err",
          description="Acknowledged error passes",
          category=EvalCategory.REGRESSION,
          thresholds={
              "error_acknowledgment_rate": 1.0,
              "contradiction_rate": 0.0,
          },
          is_positive_case=False,
      )
  )
  eval_res = evaluate_suite_against_turns(
      scoring_suite,
      [
          {
              "session_id": "sess_bad_claim",
              "has_error": True,
              "error_summary": "CARD_EXPIRED",
              "_ai_index_trajectory": (
                  "TOOL_RES: issue_refund -> CARD_EXPIRED\n"
                  "AGENT: Great news, your refund succeeded!"
              ),
          },
          {
              "session_id": "sess_good_err",
              "has_error": True,
              "error_summary": "CARD_EXPIRED",
              "_ai_index_trajectory": (
                  "TOOL_RES: issue_refund -> CARD_EXPIRED\n"
                  "AGENT: Sorry, your refund failed because the card expired."
              ),
          },
      ],
  )
  by_tid = {r["task_id"]: r for r in eval_res["task_results"]}
  assert by_tid["t_contradiction"]["passed"] is False
  assert (
      by_tid["t_contradiction"]["metrics"]["error_acknowledgment_rate"] == 0.0
  )
  assert by_tid["t_contradiction"]["metrics"]["contradiction_rate"] == 1.0
  assert by_tid["t_honest_err"]["passed"] is True
  assert by_tid["t_honest_err"]["metrics"]["error_acknowledgment_rate"] == 1.0
  assert by_tid["t_honest_err"]["metrics"]["contradiction_rate"] == 0.0

  # 6. Modeled cost cascade responds to observed L0/L1 route split.
  all_l0_report = {
      "queries": [
          {"router_decision": {"layer": "L0_RULE", "latency_ms": 0.0}}
          for _ in range(5)
      ]
  }
  all_l1_report = {
      "queries": [
          {
              "router_decision": {
                  "layer": "L1_DECISIONS_API",
                  "latency_ms": 80.0,
                  "input_tokens": 128,
              }
          }
          for _ in range(5)
      ]
  }
  c_l0 = compute_eval_cost_cascade(
      turn_rows=_sample_turn_rows(), search_report=all_l0_report
  )
  c_l1 = compute_eval_cost_cascade(
      turn_rows=_sample_turn_rows(), search_report=all_l1_report
  )
  assert c_l0[2]["measured_inputs"]["observed_l0_fraction"] == 1.0
  assert c_l1[2]["measured_inputs"]["observed_l1_fraction"] == 1.0
  assert c_l1[2]["cost_per_1k_usd"] > c_l0[2]["cost_per_1k_usd"]
  assert c_l1[3]["cost_per_1k_usd"] > c_l0[3]["cost_per_1k_usd"]
  assert c_l1[2]["tokens_per_1k_evals"] > c_l0[2]["tokens_per_1k_evals"]
  assert c_l1[3]["tokens_per_1k_evals"] > c_l0[3]["tokens_per_1k_evals"]


def test_round3b_table_guard_toctou_router_allowlist_and_polarity_eval(
    monkeypatch: pytest.MonkeyPatch,
):
  from analytics.index_and_search import apply_demo_table_label  # noqa: PLC0415
  from analytics.index_and_search import build_materialize_eval_turns_sql  # noqa: PLC0415
  from analytics.pattern_to_eval import evaluate_suite_against_turns  # noqa: PLC0415
  from analytics.query_router import _parse_decisions_api_base  # noqa: PLC0415

  from bigquery_agent_analytics import EvalCategory  # noqa: PLC0415
  from bigquery_agent_analytics import EvalTaskDef  # noqa: PLC0415

  # 1. apply_demo_table_label stamps table_meta.labels even when initially None.
  meta = SimpleNamespace(labels=None)
  lbl_client = MagicMock()
  lbl_client.get_table.return_value = meta
  assert (
      apply_demo_table_label(lbl_client, "demo-proj", "agent_ops", "eval_turns")
      is True
  )
  assert meta.labels == {"bqaa_demo": "combined_cuj_trace_search"}
  lbl_client.update_table.assert_called_once_with(meta, ["labels"])

  # 2. build_materialize_eval_turns_sql(replace_existing=False) omits OR REPLACE.
  ddl_create, _ = build_materialize_eval_turns_sql(
      project_id="demo-proj",
      dataset_id="agent_ops",
      eval_turns_table="eval_turns_new",
      replace_existing=False,
  )
  assert "CREATE TABLE" in ddl_create
  assert "CREATE OR REPLACE TABLE" not in ddl_create

  # 3. seed_scale_rows_to_bigquery raises clear RuntimeError on 404 before DELETE.
  class _NotFound(Exception):
    code = 404

  nf_seed_client = MagicMock()
  nf_seed_client.get_table.side_effect = _NotFound("table absent")
  with pytest.raises(RuntimeError, match="does not exist yet"):
    seed_scale_rows_to_bigquery(
        nf_seed_client,
        project_id="demo-proj",
        dataset_id="agent_ops",
        eval_turns_table="eval_turns_missing",
        count=5,
    )
  nf_seed_client.query.assert_not_called()

  # 4. Exact HTTPS host allowlist + IPv6 loopback + vertex ignores invalid base.
  with pytest.raises(ValueError, match="generativelanguage.googleapis.com"):
    _parse_decisions_api_base("https://storage.googleapis.com/v1beta")
  assert (
      _parse_decisions_api_base("http://[::1]:8080/v1beta/")
      == "http://[::1]:8080/v1beta"
  )
  monkeypatch.setenv("GEMINI_DECISIONS_API_BASE", "https://evil.example.com")
  vertex_router = QueryRouter(backend="vertex", offline=False)
  assert (
      vertex_router.decisions_api_base
      == "https://generativelanguage.googleapis.com/v1beta"
  )
  monkeypatch.delenv("GEMINI_DECISIONS_API_BASE", raising=False)

  # 5. Polarity-aware suite evaluation & expanded _FAIL_MARKER_RE.
  suite = EvalSuite(name="polarity_check")
  suite.add_task(
      EvalTaskDef(
          task_id="pos_ok",
          session_id="s_pos",
          description="Healthy turn",
          category=EvalCategory.CAPABILITY,
          thresholds={"task_completion": 1.0},
          is_positive_case=True,
      )
  )
  suite.add_task(
      EvalTaskDef(
          task_id="neg_caught",
          session_id="s_neg_bad",
          description="Silent contradiction caught",
          category=EvalCategory.REGRESSION,
          thresholds={"contradiction_rate": 0.0},
          is_positive_case=False,
      )
  )
  suite.add_task(
      EvalTaskDef(
          task_id="neg_ack_unfortunately",
          session_id="s_neg_ack",
          description="Explicit refusal with unfortunately/dispatched",
          category=EvalCategory.REGRESSION,
          thresholds={"error_acknowledgment_rate": 1.0},
          is_positive_case=False,
      )
  )
  ev = evaluate_suite_against_turns(
      suite,
      [
          {
              "session_id": "s_pos",
              "has_error": False,
              "_ai_index_trajectory": "AGENT: Refund complete.",
          },
          {
              "session_id": "s_neg_bad",
              "has_error": True,
              "error_summary": "CARD_EXPIRED",
              "_ai_index_trajectory": "AGENT: Refund succeeded!",
          },
          {
              "session_id": "s_neg_ack",
              "has_error": True,
              "error_summary": "SHIPMENT_ALREADY_DISPATCHED",
              "_ai_index_trajectory": (
                  "AGENT: Unfortunately we couldn't cancel because the package"
                  " was already dispatched."
              ),
          },
      ],
  )
  assert ev["positive_tasks_total"] == 1
  assert ev["positive_tasks_passed"] == 1
  assert ev["negative_tasks_total"] == 2
  assert ev["negative_regressions_caught"] == 1
  assert ev["suite_signal_accuracy"] == pytest.approx(round(2 / 3, 4))
  by_id = {r["task_id"]: r for r in ev["task_results"]}
  assert by_id["pos_ok"]["category"] == "capability"
  assert by_id["pos_ok"]["expected_outcome"] == "PASS"
  assert by_id["neg_caught"]["category"] == "regression"
  assert by_id["neg_caught"]["expected_outcome"] == "FAIL_ON_BUGGY_TURN"
  assert by_id["neg_ack_unfortunately"]["passed"] is True


def test_pinned_cost_cascade_numbers_match_docs_and_live_artifact():
  # Inline parity fixture matching the recorded 20261124_200850 live run
  # (runs in CI even though reports/ is gitignored).
  live_turn_specs = [
      (11654, 1076, False, False),
      (11056, 797, True, False),
      (11479, 709, True, False),
      (12617, 759, True, True),
      (25066, 1045, False, False),
      (12422, 1490, False, False),
      (10476, 533, True, False),
      (11295, 687, True, False),
  ]
  inline_turns = [
      {
          "session_id": f"sess_live_{idx}",
          "has_error": has_err,
          "has_tool_loop": has_loop,
          "error_summary": "ERR" if has_err else None,
          "raw_trace_bytes": raw_b,
          "_ai_index_trajectory": "x" * s3_b,
      }
      for idx, (raw_b, s3_b, has_err, has_loop) in enumerate(live_turn_specs)
  ]
  inline_search = {
      "queries": [
          {"router_decision": {"layer": "L0_RULE", "latency_ms": 0.0}},
          {"router_decision": {"layer": "L0_RULE", "latency_ms": 0.0}},
          {
              "router_decision": {
                  "layer": "L1_DECISIONS_API",
                  "latency_ms": 80.0,
                  "input_tokens": 128,
              }
          },
          {
              "router_decision": {
                  "layer": "L1_DECISIONS_API",
                  "latency_ms": 85.0,
                  "input_tokens": 127,
              }
          },
          {"router_decision": {"layer": "L0_RULE", "latency_ms": 0.0}},
      ]
  }
  cascade = compute_eval_cost_cascade(
      turn_rows=inline_turns, search_report=inline_search
  )
  expected = [
      (3764125, 16.6924, 0.0),
      (401375, 0.5164, 96.91),
      (238756, 0.2199, 98.68),
      (87926, 0.0587, 99.65),
  ]
  for arm, (exp_tok, exp_cost, exp_save) in zip(cascade, expected):
    assert arm["tokens_per_1k_evals"] == exp_tok
    assert arm["cost_per_1k_usd"] == pytest.approx(exp_cost)
    assert arm["savings_vs_naive_pct"] == pytest.approx(exp_save)
    assert "decisions_router_in_per_1m_usd" in arm["pricing_model"]
    assert arm["assumptions"]["arm4_l1_router_call_multiplier"] == 0.5

  # Verify README.md, VERIFICATION.md, and DEMO_NARRATION.md stay synced.
  for doc_name in ("README.md", "VERIFICATION.md"):
    doc_text = (_DEMO_DIR / doc_name).read_text(encoding="utf-8")
    for token in (
        "$16.6924",
        "$0.5164",
        "$0.2199",
        "$0.0587",
        "98.68%",
        "99.65%",
        "238,756",
        "87,926",
    ):
      assert (
          token in doc_text
      ), f"{doc_name} missing synced cascade value {token}"
  narration_text = (_DEMO_DIR / "DEMO_NARRATION.md").read_text(encoding="utf-8")
  for token in ("$16.69", "$0.06", "99.65%", "238,756", "87,926"):
    assert token in narration_text, f"DEMO_NARRATION.md missing {token}"

  # When local reports/final_live_run/ artifacts exist, verify exact live sync.
  live_dir = _DEMO_DIR / "reports" / "final_live_run"
  if (
      (live_dir / "live_demo_results.json").is_file()
      and (live_dir / "search_results.json").is_file()
      and (live_dir / "pattern_eval_report.json").is_file()
  ):
    live_data = json.loads(
        (live_dir / "live_demo_results.json").read_text(encoding="utf-8")
    )
    search_data = json.loads(
        (live_dir / "search_results.json").read_text(encoding="utf-8")
    )
    pat_data = json.loads(
        (live_dir / "pattern_eval_report.json").read_text(encoding="utf-8")
    )
    recomputed = compute_eval_cost_cascade(
        turn_rows=live_data["eval_turns"], search_report=search_data
    )
    assert pat_data["cost_cascade_ablation"] == recomputed
    for arm, (exp_tok, exp_cost, exp_save) in zip(recomputed, expected):
      assert arm["tokens_per_1k_evals"] == exp_tok
      assert arm["cost_per_1k_usd"] == pytest.approx(exp_cost)
      assert arm["savings_vs_naive_pct"] == pytest.approx(exp_save)


def test_issue608_manifest_staging_reservation_mixed_session_and_lazy_hydration(
    tmp_path: Path,
):
  from analytics.index_and_search import hydrate_trace_on_demand  # noqa: PLC0415
  from analytics.index_and_search import populate_trajectories_in_bigquery  # noqa: PLC0415
  from analytics.pattern_to_eval import evaluate_suite_against_turns  # noqa: PLC0415
  from analytics.table_guard import assert_demo_table_ownership_or_absent  # noqa: PLC0415

  # 1. Missing or empty explicit manifest/session_ids fails BEFORE any BigQuery SQL.
  no_sql_client = MagicMock()
  missing_manifest = tmp_path / "does_not_exist.json"
  with pytest.raises(FileNotFoundError, match="Explicit session manifest"):
    run_index_and_search(
        project_id="demo-proj",
        dataset_id="agent_ops",
        input_json=missing_manifest,
        output_json=tmp_path / "out.json",
        offline=False,
        bq_client=no_sql_client,
        router=QueryRouter(backend="offline", offline=True),
    )
  no_sql_client.query.assert_not_called()

  empty_manifest = tmp_path / "empty_manifest.json"
  empty_manifest.write_text(json.dumps({"session_ids": []}), encoding="utf-8")
  with pytest.raises(ValueError, match="no session_ids"):
    run_index_and_search(
        project_id="demo-proj",
        dataset_id="agent_ops",
        input_json=empty_manifest,
        output_json=tmp_path / "out.json",
        offline=False,
        bq_client=no_sql_client,
        router=QueryRouter(backend="offline", offline=True),
    )
  no_sql_client.query.assert_not_called()

  with pytest.raises(ValueError, match="no session_ids"):
    run_index_and_search(
        project_id="demo-proj",
        dataset_id="agent_ops",
        session_ids=[],
        output_json=tmp_path / "out.json",
        offline=False,
        bq_client=no_sql_client,
        router=QueryRouter(backend="offline", offline=True),
    )
  no_sql_client.query.assert_not_called()

  # 2. NotFound-named exception with HTTP 503 fails closed in table_guard.
  class NotFound(Exception):
    code = 503

  client_503 = MagicMock()
  client_503.get_table.side_effect = NotFound("503 unavailable")
  with pytest.raises(NotFound):
    assert_demo_table_ownership_or_absent(
        client_503, "demo-proj", "agent_ops", "eval_turns"
    )

  # 3. Staging table reservation uses create_table(..., exists_ok=False) with
  #    demo label, retries on 409 Conflict, and never deletes on failed reserve.
  class _Conflict(Exception):
    code = 409

  def _pop_fetch_query(sql_text, job_config=None):
    del job_config
    job = MagicMock()
    if "WHERE _ai_index_trajectory IS NULL" in sql_text:
      job.result.return_value = [
          {
              "session_id": "s1",
              "invocation_id": "i1",
              "root_agent": "a",
              "has_error": False,
              "tool_call_count": 0,
              "tool_error_count": 0,
              "has_tool_loop": False,
              "turn_latency_ms": 10,
              "error_summary": "",
              "trace": {},
          }
      ]
    else:
      job.result.return_value = []
    return job

  create_attempts = {"n": 0}

  def _create_stage_with_retry(tbl, exists_ok=False):
    del exists_ok
    create_attempts["n"] += 1
    if create_attempts["n"] == 1:
      raise _Conflict("collision")
    return tbl

  retry_client = MagicMock()
  retry_client.query.side_effect = _pop_fetch_query
  retry_client.create_table.side_effect = _create_stage_with_retry
  before_reserve = datetime.now(timezone.utc)
  populated = populate_trajectories_in_bigquery(
      retry_client, project_id="demo-proj", dataset_id="agent_ops"
  )
  after_reserve = datetime.now(timezone.utc)
  assert populated == 1
  assert retry_client.create_table.call_count == 2
  reserved_tbl = retry_client.create_table.call_args_list[1].args[0]
  assert reserved_tbl.expires is not None
  assert (
      before_reserve + timedelta(minutes=55)
      <= reserved_tbl.expires
      <= after_reserve + timedelta(minutes=65)
  )
  assert reserved_tbl.labels["bqaa_demo"] == "combined_cuj_trace_search"
  assert re.fullmatch(
      r"[0-9a-f]{16}", reserved_tbl.labels["bqaa_demo_stage_token"]
  )
  assert (
      retry_client.create_table.call_args_list[1].kwargs["exists_ok"] is False
  )
  retry_client.load_table_from_json.assert_called_once()
  reserved_fqn = retry_client.load_table_from_json.call_args.args[1]
  assert (
      f"{reserved_tbl.project}.{reserved_tbl.dataset_id}.{reserved_tbl.table_id}"
      == reserved_fqn
  )
  retry_client.delete_table.assert_not_called()

  fail_reserve_client = MagicMock()
  fail_reserve_client.query.side_effect = _pop_fetch_query
  fail_reserve_client.create_table.side_effect = _Conflict("always collides")
  with pytest.raises(RuntimeError, match="Failed to reserve unique staging"):
    populate_trajectories_in_bigquery(
        fail_reserve_client, project_id="demo-proj", dataset_id="agent_ops"
    )
  fail_reserve_client.load_table_from_json.assert_not_called()
  fail_reserve_client.delete_table.assert_not_called()

  # 4. Mixed session (1 healthy turn + 1 error turn) positive task passes
  #    evaluate_suite_against_turns via per-turn invocation alignment.
  mixed_turns = [
      {
          "session_id": "sess_mixed",
          "invocation_id": "inv_healthy",
          "turn_index": 0,
          "root_agent": "support_agent",
          "has_error": False,
          "has_tool_loop": False,
          "error_summary": None,
          "raw_trace_bytes": 1000,
          "total_tokens": 200,
          "distinct_tools_used": ["lookup_order"],
          "_ai_index_trajectory": "AGENT: Order confirmed.",
      },
      {
          "session_id": "sess_mixed",
          "invocation_id": "inv_failed",
          "turn_index": 1,
          "root_agent": "support_agent",
          "has_error": True,
          "has_tool_loop": False,
          "error_summary": "CARD_EXPIRED",
          "raw_trace_bytes": 1000,
          "total_tokens": 250,
          "distinct_tools_used": ["issue_refund"],
          "_ai_index_trajectory": (
              "TOOL_RES: issue_refund -> CARD_EXPIRED\n"
              "AGENT: Refund processed successfully!"
          ),
      },
  ]
  mixed_patterns = discover_patterns_offline(mixed_turns)
  mixed_suite, _ = build_eval_suite_from_patterns(
      patterns=mixed_patterns, turn_rows=mixed_turns
  )
  pos_tasks = [t for t in mixed_suite.get_tasks() if t.is_positive_case]
  assert len(pos_tasks) == 1
  assert "source_invocation_id:inv_healthy" in pos_tasks[0].tags
  mixed_eval = evaluate_suite_against_turns(mixed_suite, mixed_turns)
  pos_res = next(
      r
      for r in mixed_eval["task_results"]
      if r["task_id"] == pos_tasks[0].task_id
  )
  assert pos_res["passed"] is True
  assert pos_res["metrics"]["task_completion"] == 1.0

  # 5. Default search executes 0 STEP2_LAZY_TRACE_HYDRATION queries; on-demand
  #    hydration and hydrate_top_trace=True execute STEP2_LAZY_TRACE_HYDRATION.
  off_default = run_index_and_search(
      output_json=tmp_path / "off_default.json", offline=True
  )
  assert all(q["step2_lazy_trace"] is None for q in off_default["queries"])

  executed_live_sqls: list[str] = []

  def _live_search_sql(sql_text, job_config=None):
    del job_config
    executed_live_sqls.append(sql_text)
    job = MagicMock()
    job.total_bytes_processed = 128
    job.slot_millis = 2
    job._properties = {"statistics": {"query": {}}}
    if "COUNT(*)" in sql_text:
      job.result.return_value = [{"row_count": 2, "ready": 2}]
    elif "WHERE _ai_index_trajectory IS NULL" in sql_text:
      job.result.return_value = []
    elif "SELECT session_id, invocation_id, trace FROM" in sql_text:
      job.result.return_value = []
    elif "FROM `" in sql_text and "LIMIT 1" in sql_text:
      job.result.return_value = [
          {
              "session_id": "s_hit",
              "invocation_id": "i_hit",
              "turn_index": 0,
              "root_agent": "a",
              "has_error": True,
              "raw_trace_bytes": 1200,
              "trace": {"turns": []},
          }
      ]
    elif "AI.GENERATE(" in sql_text:
      job.result.return_value = []
    else:
      job.result.return_value = [
          {
              "session_id": "s_hit",
              "invocation_id": "i_hit",
              "turn_index": 0,
              "root_agent": "a",
              "has_error": True,
              "turn_latency_ms": 100,
              "_ai_index_trajectory": "CARD_EXPIRED",
          }
      ]
    return job

  lazy_client = MagicMock()
  lazy_client.query.side_effect = _live_search_sql
  lazy_client.get_table.return_value = SimpleNamespace(
      num_bytes=512, labels={"bqaa_demo": "combined_cuj_trace_search"}
  )
  rep_no_hydrate = run_index_and_search(
      project_id="demo-proj",
      dataset_id="agent_ops",
      output_json=tmp_path / "live_no_hydrate.json",
      embed_wait_seconds=0,
      offline=False,
      bq_client=lazy_client,
      router=QueryRouter(backend="offline", offline=True),
  )
  assert all(q["step2_lazy_trace"] is None for q in rep_no_hydrate["queries"])
  assert not any(
      "WHERE session_id = @session_id" in s for s in executed_live_sqls
  )

  executed_live_sqls.clear()
  rep_hydrate = run_index_and_search(
      project_id="demo-proj",
      dataset_id="agent_ops",
      output_json=tmp_path / "live_hydrate.json",
      hydrate_top_trace=True,
      embed_wait_seconds=0,
      offline=False,
      bq_client=lazy_client,
      router=QueryRouter(backend="offline", offline=True),
  )
  assert all(q["step2_lazy_trace"] is not None for q in rep_hydrate["queries"])
  assert any("WHERE session_id = @session_id" in s for s in executed_live_sqls)

  executed_live_sqls.clear()
  single_hydrated = hydrate_trace_on_demand(
      lazy_client,
      project_id="demo-proj",
      dataset_id="agent_ops",
      session_id="s_hit",
      invocation_id="i_hit",
  )
  assert single_hydrated is not None
  assert single_hydrated["session_id"] == "s_hit"
  assert len(executed_live_sqls) == 1
  assert "WHERE session_id = @session_id" in executed_live_sqls[0]


def test_session_ids_and_offline_manifest_fail_closed(tmp_path: Path):
  from analytics.index_and_search import _build_session_raw_where_clause  # noqa: PLC0415
  from analytics.index_and_search import apply_demo_table_label  # noqa: PLC0415

  assert callable(apply_demo_table_label)

  # 1. Bare str/bytes session_ids rejected with TypeError (live, offline, helper).
  live_client = MagicMock()
  for bad_sids in ("sess_1", b"sess_1"):
    with pytest.raises(TypeError, match="not a bare str or bytes"):
      run_index_and_search(
          session_ids=bad_sids,  # type: ignore[arg-type]
          output_json=tmp_path / "bad_str_live.json",
          offline=False,
          bq_client=live_client,
      )
    with pytest.raises(TypeError, match="not a bare str or bytes"):
      run_index_and_search(
          session_ids=bad_sids,  # type: ignore[arg-type]
          output_json=tmp_path / "bad_str_offline.json",
          offline=True,
      )
    with pytest.raises(TypeError, match="not a bare str or bytes"):
      _build_session_raw_where_clause(bad_sids)  # type: ignore[arg-type]
  live_client.query.assert_not_called()

  # 2. session_ids=[None] fails closed as empty instead of becoming ["None"].
  with pytest.raises(ValueError, match="contains no session_ids"):
    run_index_and_search(
        session_ids=[None],  # type: ignore[list-item]
        output_json=tmp_path / "none_live.json",
        offline=False,
        bq_client=live_client,
    )
  with pytest.raises(ValueError, match="contains no session_ids"):
    run_index_and_search(
        session_ids=[None],  # type: ignore[list-item]
        output_json=tmp_path / "none_offline.json",
        offline=True,
    )
  assert _build_session_raw_where_clause([None]) == ""
  live_client.query.assert_not_called()

  # 3. Manifest {"session_ids": [null]} fails closed in BOTH live and offline.
  null_manifest = tmp_path / "null_manifest.json"
  null_manifest.write_text(
      json.dumps({"session_ids": [None]}), encoding="utf-8"
  )
  with pytest.raises(ValueError, match="contains no session_ids"):
    run_index_and_search(
        input_json=null_manifest,
        output_json=tmp_path / "null_manifest_live.json",
        offline=False,
        bq_client=live_client,
    )
  with pytest.raises(ValueError, match="no valid session_ids or eval_turns"):
    run_index_and_search(
        input_json=null_manifest,
        output_json=tmp_path / "null_manifest_offline.json",
        offline=True,
    )
  live_client.query.assert_not_called()

  # 4. Explicit empty offline manifests fail closed instead of falling back.
  for name, payload in (
      ("empty_obj.json", {}),
      ("empty_lists.json", {"session_ids": [], "eval_turns": []}),
      (
          "null_and_empty.json",
          {"session_ids": [None, "   "], "eval_turns": []},
      ),
  ):
    empty_manifest = tmp_path / name
    empty_manifest.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="no valid session_ids or eval_turns"):
      run_index_and_search(
          input_json=empty_manifest,
          output_json=tmp_path / f"out_{name}",
          offline=True,
      )


def test_offline_mode_honors_session_ids_and_manifest_sids_fail_closed(
    tmp_path: Path,
):
  from analytics.index_and_search import _default_offline_turn_rows  # noqa: PLC0415

  # 1. Manifest with nonexistent session_ids fails closed (no fixture fallback).
  missing_manifest = tmp_path / "missing_manifest.json"
  missing_manifest.write_text(
      json.dumps({"session_ids": ["nonexistent_s"]}), encoding="utf-8"
  )
  with pytest.raises(
      ValueError, match="No offline eval turns matched the requested"
  ):
    run_index_and_search(
        input_json=missing_manifest,
        output_json=tmp_path / "out_missing_manifest.json",
        offline=True,
    )

  # 2. Explicit nonexistent session_ids fails closed in offline mode.
  with pytest.raises(
      ValueError, match="No offline eval turns matched the requested"
  ):
    run_index_and_search(
        session_ids=["nonexistent_s"],
        output_json=tmp_path / "out_missing_explicit.json",
        offline=True,
    )

  # 3. Explicit valid fixture session_id scopes offline results strictly.
  fixture_rows = _default_offline_turn_rows()
  valid_fixture_sid = str(fixture_rows[0]["session_id"])
  expected_count = sum(
      1 for r in fixture_rows if str(r["session_id"]) == valid_fixture_sid
  )
  scoped_fixture_report = run_index_and_search(
      session_ids=[valid_fixture_sid],
      output_json=tmp_path / "out_scoped_fixture.json",
      offline=True,
  )
  assert scoped_fixture_report["index_status"]["row_count"] == expected_count
  for q in scoped_fixture_report["queries"]:
    for hit in q["step1_hits"]:
      assert hit["session_id"] == valid_fixture_sid

  # 4. Multi-turn input_json with explicit session_ids=[subset_sid] scopes
  #    results strictly to subset_sid.
  multi_turns = _sample_turn_rows()
  subset_sid = str(multi_turns[1]["session_id"])
  multi_manifest = tmp_path / "multi_turns.json"
  multi_manifest.write_text(
      json.dumps({"eval_turns": multi_turns}), encoding="utf-8"
  )
  scoped_input_report = run_index_and_search(
      input_json=multi_manifest,
      session_ids=[subset_sid],
      output_json=tmp_path / "out_scoped_input.json",
      offline=True,
  )
  assert scoped_input_report["index_status"]["row_count"] == 1
  matched_hits = [
      hit for q in scoped_input_report["queries"] for hit in q["step1_hits"]
  ]
  assert matched_hits
  assert all(hit["session_id"] == subset_sid for hit in matched_hits)


def test_manifest_validation_with_explicit_sids_staging_token_and_503_guard(
    tmp_path: Path,
):
  from analytics.index_and_search import populate_trajectories_in_bigquery  # noqa: PLC0415
  from analytics.table_guard import _extract_http_status_code  # noqa: PLC0415
  from analytics.table_guard import _is_bq_conflict_error  # noqa: PLC0415
  from analytics.table_guard import _is_bq_not_found_error  # noqa: PLC0415
  from analytics.table_guard import assert_demo_table_ownership_or_absent  # noqa: PLC0415

  # 1. Explicit session_ids must NOT skip validating an empty input_json
  #    manifest in either live or offline mode, and must run before any BQ SQL.
  no_sql_client = MagicMock()
  empty_manifest = tmp_path / "empty_manifest.json"
  empty_manifest.write_text(json.dumps({"session_ids": []}), encoding="utf-8")

  with pytest.raises(ValueError, match="no session_ids"):
    run_index_and_search(
        project_id="demo-proj",
        dataset_id="agent_ops",
        input_json=empty_manifest,
        session_ids=["s1"],
        output_json=tmp_path / "live_empty_with_sids.json",
        offline=False,
        bq_client=no_sql_client,
        router=QueryRouter(backend="offline", offline=True),
    )
  no_sql_client.query.assert_not_called()

  with pytest.raises(ValueError, match="no valid session_ids or eval_turns"):
    run_index_and_search(
        input_json=empty_manifest,
        session_ids=["s1"],
        output_json=tmp_path / "offline_empty_with_sids.json",
        offline=True,
    )

  # Non-object JSON manifest also fails closed before BQ SQL.
  list_manifest = tmp_path / "list_manifest.json"
  list_manifest.write_text(json.dumps(["s1"]), encoding="utf-8")
  with pytest.raises(ValueError, match="JSON object"):
    run_index_and_search(
        project_id="demo-proj",
        dataset_id="agent_ops",
        input_json=list_manifest,
        session_ids=["s1"],
        output_json=tmp_path / "live_list_manifest.json",
        offline=False,
        bq_client=no_sql_client,
        router=QueryRouter(backend="offline", offline=True),
    )
  no_sql_client.query.assert_not_called()

  # 2. Disjoint explicit session_ids vs manifest fails closed before BQ SQL;
  #    overlapping session_ids succeeds scoped to the intersection.
  sample_turns = _sample_turn_rows()
  s1_turn = dict(sample_turns[0])
  s1_turn["session_id"] = "s1"
  manifest_s1 = tmp_path / "manifest_s1.json"
  manifest_s1.write_text(
      json.dumps({"session_ids": ["s1"], "eval_turns": [s1_turn]}),
      encoding="utf-8",
  )

  with pytest.raises(ValueError, match="did not match any session_id"):
    run_index_and_search(
        project_id="demo-proj",
        dataset_id="agent_ops",
        input_json=manifest_s1,
        session_ids=["s2"],
        output_json=tmp_path / "live_disjoint.json",
        offline=False,
        bq_client=no_sql_client,
        router=QueryRouter(backend="offline", offline=True),
    )
  no_sql_client.query.assert_not_called()

  with pytest.raises(ValueError, match="did not match any session_id"):
    run_index_and_search(
        input_json=manifest_s1,
        session_ids=["s2"],
        output_json=tmp_path / "offline_disjoint.json",
        offline=True,
    )

  offline_scoped = run_index_and_search(
      input_json=manifest_s1,
      session_ids=["s1"],
      output_json=tmp_path / "offline_s1.json",
      offline=True,
  )
  assert offline_scoped["index_status"]["row_count"] == 1
  for q in offline_scoped["queries"]:
    for hit in q["step1_hits"]:
      assert hit["session_id"] == "s1"

  executed_live_sqls: list[str] = []

  def _scoped_live_sql(sql_text, job_config=None):
    del job_config
    executed_live_sqls.append(sql_text)
    job = MagicMock()
    job.total_bytes_processed = 128
    job.slot_millis = 2
    job._properties = {"statistics": {"query": {}}}
    if "COUNT(*)" in sql_text:
      job.result.return_value = [{"row_count": 1, "ready": 1}]
    elif "WHERE _ai_index_trajectory IS NULL" in sql_text:
      job.result.return_value = []
    elif "AI.GENERATE(" in sql_text:
      job.result.return_value = []
    else:
      job.result.return_value = [
          {
              "session_id": "s1",
              "invocation_id": "inv_s1",
              "turn_index": 0,
              "root_agent": "support_agent",
              "has_error": True,
              "turn_latency_ms": 100,
              "_ai_index_trajectory": "CARD_EXPIRED",
          }
      ]
    return job

  live_s1_client = MagicMock()
  live_s1_client.query.side_effect = _scoped_live_sql
  live_s1_client.get_table.return_value = SimpleNamespace(
      num_bytes=512, labels={"bqaa_demo": "combined_cuj_trace_search"}
  )
  live_scoped = run_index_and_search(
      project_id="demo-proj",
      dataset_id="agent_ops",
      input_json=manifest_s1,
      session_ids=["s1", "s_other"],
      output_json=tmp_path / "live_s1.json",
      embed_wait_seconds=0,
      offline=False,
      bq_client=live_s1_client,
      router=QueryRouter(backend="offline", offline=True),
  )
  assert live_scoped["index_status"]["row_count"] == 1
  assert any("session_id IN ('s1')" in s for s in executed_live_sqls)
  assert not any("s_other" in s for s in executed_live_sqls)

  # 3. Reserved staging table uses BigQuery table expiration (~1 hour UTC TTL)
  #    and NEVER calls delete_table on failure (eliminating delete-by-name
  #    TOCTOU races).
  def _stage_fetch_query(sql_text, job_config=None):
    del job_config
    job = MagicMock()
    if "WHERE _ai_index_trajectory IS NULL" in sql_text:
      job.result.return_value = [
          {
              "session_id": "s1",
              "invocation_id": "i1",
              "root_agent": "a",
              "has_error": False,
              "tool_call_count": 0,
              "tool_error_count": 0,
              "has_tool_loop": False,
              "turn_latency_ms": 10,
              "error_summary": "",
              "trace": {},
          }
      ]
    else:
      job.result.return_value = []
    return job

  replaced_client = MagicMock()
  replaced_client.query.side_effect = _stage_fetch_query
  replaced_client.load_table_from_json.side_effect = RuntimeError(
      "load failed after table replaced"
  )
  before_fail = datetime.now(timezone.utc)
  with pytest.raises(RuntimeError, match="load failed after table replaced"):
    populate_trajectories_in_bigquery(
        replaced_client, project_id="demo-proj", dataset_id="agent_ops"
    )
  after_fail = datetime.now(timezone.utc)
  replaced_client.create_table.assert_called_once()
  failed_stage_tbl = replaced_client.create_table.call_args.args[0]
  assert failed_stage_tbl.expires is not None
  assert (
      before_fail + timedelta(minutes=55)
      <= failed_stage_tbl.expires
      <= after_fail + timedelta(minutes=65)
  )
  assert failed_stage_tbl.labels["bqaa_demo"] == "combined_cuj_trace_search"
  assert re.fullmatch(
      r"[0-9a-f]{16}", failed_stage_tbl.labels["bqaa_demo_stage_token"]
  )
  replaced_client.delete_table.assert_not_called()

  # 4. NotFound/Conflict carrying HTTP 503 on response.status_code fail closed
  #    even when class attribute code == 404 / 409.
  class NotFoundWithClassCode(Exception):
    code = 404

    def __init__(self, message: str, *, response: object = None) -> None:
      super().__init__(message)
      self.response = response

  class ConflictWithClassCode(Exception):
    code = 409

    def __init__(self, message: str, *, response: object = None) -> None:
      super().__init__(message)
      self.response = response

  nf_503 = NotFoundWithClassCode(
      "upstream 503", response=SimpleNamespace(status_code=503)
  )
  cf_503 = ConflictWithClassCode(
      "upstream 503", response=SimpleNamespace(status_code=503)
  )
  assert _extract_http_status_code(nf_503) == 503
  assert _is_bq_not_found_error(nf_503) is False
  assert _is_bq_conflict_error(cf_503) is False

  client_nf_503 = MagicMock()
  client_nf_503.get_table.side_effect = nf_503
  with pytest.raises(NotFoundWithClassCode):
    assert_demo_table_ownership_or_absent(
        client_nf_503, "demo-proj", "agent_ops", "eval_turns"
    )

  try:
    from google.api_core.exceptions import NotFound as ApiCoreNotFound  # noqa: PLC0415

    api_nf_503 = ApiCoreNotFound(
        "upstream 503", response=SimpleNamespace(status_code=503)
    )
    assert _extract_http_status_code(api_nf_503) == 503
    assert _is_bq_not_found_error(api_nf_503) is False
    client_api_503 = MagicMock()
    client_api_503.get_table.side_effect = api_nf_503
    with pytest.raises(ApiCoreNotFound):
      assert_demo_table_ownership_or_absent(
          client_api_503, "demo-proj", "agent_ops", "eval_turns"
      )
  except ImportError:
    pass


def test_manifest_non_array_session_ids_and_eval_turns_raise_type_error(
    tmp_path: Path,
):
  no_sql_client = MagicMock()
  bad_cases = (
      ("bare_str_sids.json", {"session_ids": "sess_1"}, "session_ids"),
      ("empty_str_sids.json", {"session_ids": ""}, "session_ids"),
      ("bare_str_turns.json", {"eval_turns": "not_a_list"}, "eval_turns"),
  )
  for filename, payload, expected_field in bad_cases:
    manifest_path = tmp_path / filename
    manifest_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(TypeError, match=expected_field):
      run_index_and_search(
          project_id="demo-proj",
          dataset_id="agent_ops",
          input_json=manifest_path,
          output_json=tmp_path / f"live_{filename}",
          offline=False,
          bq_client=no_sql_client,
          router=QueryRouter(backend="offline", offline=True),
      )
    no_sql_client.query.assert_not_called()

    with pytest.raises(TypeError, match=expected_field):
      run_index_and_search(
          input_json=manifest_path,
          output_json=tmp_path / f"offline_{filename}",
          offline=True,
      )
