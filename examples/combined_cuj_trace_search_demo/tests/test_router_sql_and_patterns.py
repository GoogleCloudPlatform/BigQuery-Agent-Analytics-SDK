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
  assert "thinking_budget" in sql_text
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
          "ENDPOINT": "gemini-2.5-flash",
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


def test_layer1_vertex_flash_fallback_thinking_budget_zero():
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
              prompt_token_count=42, candidates_token_count=18
          ),
      )

  fake_vertex = SimpleNamespace(models=FakeModels())
  router = QueryRouter(backend="vertex", vertex_client=fake_vertex)
  decision = router.classify(
      "agent got stuck calling the same lookup tool repeatedly"
  )
  assert decision.mode == "NATURAL_LANGUAGE"
  assert decision.layer == "L1_VERTEX_FLASH"
  assert decision.input_tokens == 42
  assert decision.output_tokens == 18
  assert calls[0]["model"] == "gemini-2.5-flash"
  assert calls[0]["config"].thinking_config.thinking_budget == 0


def test_live_router_fails_closed_when_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
):
  monkeypatch.delenv("GEMINI_API_KEY", raising=False)
  monkeypatch.setattr(
      "analytics.query_router._build_vertex_client",
      lambda **kwargs: (_ for _ in ()).throw(RuntimeError("no adc")),
  )
  router = QueryRouter(backend="vertex", offline=False)
  with pytest.raises(RuntimeError, match="no adc"):
    router.classify("agent got stuck calling lookup repeatedly")


# ---------------------------------------------------------------------------
# 3. Materialization, Dual-Index Gate & Scale Seeder Tests
# ---------------------------------------------------------------------------


def test_build_materialize_eval_turns_sql_uses_adk_and_fallback(
    monkeypatch: pytest.MonkeyPatch,
):
  sql_adk, source_adk = build_materialize_eval_turns_sql(
      project_id="demo-proj",
      dataset_id="agent_ops",
      events_table="agent_events",
      eval_turns_table="eval_turns",
  )
  assert source_adk == "adk_build_eval_turn_query_sql"
  assert "CREATE OR REPLACE TABLE `demo-proj.agent_ops.eval_turns`" in sql_adk
  assert "CAST(NULL AS STRING) AS _ai_index_trajectory" in sql_adk
  assert "BYTE_LENGTH(TO_JSON_STRING(b.trace)) AS raw_trace_bytes" in sql_adk
  assert "error_summary" in sql_adk

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
  bq_client.get_table.return_value = SimpleNamespace(num_bytes=2048)

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
  assert arm4["tokens_per_1k_evals"] < arm3["tokens_per_1k_evals"]
  assert arm3["tokens_per_1k_evals"] < arm2["tokens_per_1k_evals"]
  assert arm2["tokens_per_1k_evals"] < arm1["tokens_per_1k_evals"]

  assert arm4["cost_per_1k_usd"] < arm3["cost_per_1k_usd"]
  assert arm3["cost_per_1k_usd"] < arm2["cost_per_1k_usd"]
  assert arm2["cost_per_1k_usd"] < arm1["cost_per_1k_usd"]
  assert arm3["latency_ms_p50"] <= arm2["latency_ms_p50"]
  assert arm4["savings_vs_naive_pct"] > 90.0


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
  bq_no_rerank.get_table.return_value = SimpleNamespace(num_bytes=512)
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
  bq_zero.get_table.return_value = SimpleNamespace(num_bytes=512)
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
  bq_exc.get_table.return_value = SimpleNamespace(num_bytes=512)
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
  client.get_table.return_value = SimpleNamespace(schema=[])
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

  e2e_text = (_DEMO_DIR / "run_e2e_demo.sh").read_text(encoding="utf-8")
  assert 'if [[ "$OFFLINE_MODE" != "true" ]]; then' in e2e_text
  assert '--location "$BQ_LOCATION"' in e2e_text

  reset_text = (_DEMO_DIR / "reset.sh").read_text(encoding="utf-8")
  assert "STARTS_WITH(session_id, 'scale_synth_')" in reset_text
  assert "--delete-scale-rows" in reset_text
  assert "${EVAL_TURNS_TABLE}_scale" in reset_text
