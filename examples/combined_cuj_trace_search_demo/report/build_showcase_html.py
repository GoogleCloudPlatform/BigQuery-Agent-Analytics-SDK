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

"""Builds a self-contained 0600 Adaptive Workbench HTML showcase.

Consumes JSON artifacts from Stage 1 (live_demo_results.json), Stage 2
(search_results.json), and Stage 3 (pattern_eval_report.json +
eval_dataset_generated.json), or renders an offline demo fixture via
``--demo-fixture``.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping
import datetime
import html
import json
import os
from pathlib import Path
import re
import tempfile
from typing import Any

_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(
            r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----"
        ),
        "[REDACTED:PRIVATE_KEY]",
    ),
    (re.compile(r"\bya29\.[A-Za-z0-9._-]+\b"), "[REDACTED:OAUTH_TOKEN]"),
    (re.compile(r"\bAIza[A-Za-z0-9_-]{16,}\b"), "[REDACTED:API_KEY]"),
    (
        re.compile(r"(Authorization\s*:\s*Bearer\s+)\S+", re.IGNORECASE),
        r"\1[REDACTED:BEARER]",
    ),
    (
        re.compile(
            r"""((?:api_key|password|client_secret|access_token|refresh_token)\s*[:=]\s*["']?)[^\s"',}]+""",
            re.IGNORECASE,
        ),
        r"\1[REDACTED]",
    ),
)


def _redact_secrets(value: Any) -> Any:
  """Recursively scrubs credential patterns from strings and containers."""
  if isinstance(value, str):
    out = value
    for pat, repl in _SECRET_PATTERNS:
      out = pat.sub(repl, out)
    return out
  if isinstance(value, list):
    return [_redact_secrets(item) for item in value]
  if isinstance(value, dict):
    return {str(k): _redact_secrets(v) for k, v in value.items()}
  return value


def _ascii_code(text: str) -> str:
  """Normalizes text inside <code>/<pre> to pure ASCII before HTML escaping."""
  normalized = (
      str(text)
      .replace("\u2212", "-")
      .replace("\u2013", "--")
      .replace("\u2014", "--")
      .replace("\u00a0", " ")
      .replace("\u2018", "'")
      .replace("\u2019", "'")
      .replace("\u201c", '"')
      .replace("\u201d", '"')
  )
  return "".join(ch if ord(ch) < 128 else "?" for ch in normalized)


def _escape_html_text(text: Any) -> str:
  return html.escape(str(_redact_secrets(str(text))), quote=True)


def _escape_code_text(text: Any) -> str:
  return html.escape(_ascii_code(_redact_secrets(str(text))), quote=True)


def _json_for_script_tag(payload: Mapping[str, Any]) -> str:
  """Serializes payload safely for embedding inside <script type='application/json'>."""
  safe_obj = _redact_secrets(dict(payload))
  raw = json.dumps(safe_obj, indent=2, ensure_ascii=True)
  return (
      raw.replace("<", "\\u003c")
      .replace(">", "\\u003e")
      .replace("&", "\\u0026")
      .replace("\u2028", "\\u2028")
      .replace("\u2029", "\\u2029")
  )


def build_demo_fixture_payload() -> dict[str, Any]:
  """Returns deterministic offline showcase fixture data across all 5 views."""
  eval_turns = [
      {
          "session_id": "sess_01_healthy_refund",
          "invocation_id": "inv_01",
          "turn_index": 0,
          "root_agent": "customer_ops_agent",
          "has_error": False,
          "has_tool_loop": False,
          "turn_latency_ms": 940,
          "total_tokens": 1850,
          "error_summary": "",
          "trace": {
              "agents": {
                  "customer_ops_agent": {
                      "instruction": "Enterprise Customer Operations Policy (6.4 KB)...",
                      "tools": [
                          "lookup_order",
                          "check_policy_eligibility",
                          "issue_refund",
                      ],
                  }
              },
              "turns": [
                  {"events": ["USER: Refund ORD-9012", "TOOL_RES: ok=True"]}
              ],
          },
          "ablation": {
              "s1_bytes": 11420,
              "s1_tokens_est": 2855,
              "s1_exceeds_2048": True,
              "s2_bytes": 1680,
              "s2_tokens_est": 420,
              "s2_exceeds_2048": False,
              "s3_bytes": 388,
              "s3_tokens_est": 97,
              "s3_exceeds_2048": False,
              "s3_text": (
                  "[OUTCOME: root_agent=customer_ops_agent has_error=False "
                  "tool_calls=3 tool_errors=0 loop=False latency_ms=940]\n"
                  "USER: Please issue a refund for order ORD-9012.\n"
                  "TOOL_CALL: lookup_order(order_id='ORD-9012')\n"
                  "TOOL_RES: {'status': 'delivered', 'amount_usd': 79.0}\n"
                  "TOOL_CALL: issue_refund(order_id='ORD-9012', amount_usd=79.0)\n"
                  "TOOL_RES: {'ok': True, 'refund_id': 'REF-3310'}\n"
                  "AGENT: Your $79.00 refund for ORD-9012 is confirmed (REF-3310)."
              ),
          },
      },
      {
          "session_id": "sess_02_card_expired",
          "invocation_id": "inv_02",
          "turn_index": 0,
          "root_agent": "customer_ops_agent",
          "has_error": True,
          "has_tool_loop": False,
          "turn_latency_ms": 1180,
          "total_tokens": 2040,
          "error_summary": "CARD_EXPIRED",
          "trace": {
              "agents": {"customer_ops_agent": {"instruction": "...6.4 KB..."}},
              "turns": [
                  {
                      "events": [
                          "USER: Refund ORD-8841",
                          "TOOL_ERR: CARD_EXPIRED",
                      ]
                  }
              ],
          },
          "ablation": {
              "s1_bytes": 12260,
              "s1_tokens_est": 3065,
              "s1_exceeds_2048": True,
              "s2_bytes": 2140,
              "s2_tokens_est": 535,
              "s2_exceeds_2048": False,
              "s3_bytes": 436,
              "s3_tokens_est": 109,
              "s3_exceeds_2048": False,
              "s3_text": (
                  "[OUTCOME: root_agent=customer_ops_agent has_error=True "
                  "tool_calls=2 tool_errors=1 loop=False latency_ms=1180]\n"
                  "USER: Refund order ORD-8841 to my card.\n"
                  "TOOL_CALL: issue_refund(order_id='ORD-8841', amount_usd=120.0)\n"
                  "TOOL_RES: {'ok': False, 'error_code': 'CARD_EXPIRED', "
                  "'message': 'Issuer rejected refund: card expired 08/25'}\n"
                  "AGENT: Refund failed with CARD_EXPIRED; please add a new payment method."
              ),
          },
      },
      {
          "session_id": "sess_03_silent_contradiction",
          "invocation_id": "inv_03",
          "turn_index": 0,
          "root_agent": "customer_ops_agent",
          "has_error": True,
          "has_tool_loop": False,
          "turn_latency_ms": 1310,
          "total_tokens": 2180,
          "error_summary": "CARD_EXPIRED",
          "trace": {
              "agents": {"customer_ops_agent": {"instruction": "...6.4 KB..."}},
              "turns": [
                  {
                      "events": [
                          "TOOL_ERR: CARD_EXPIRED",
                          "AGENT: Your refund is complete!",
                      ]
                  }
              ],
          },
          "ablation": {
              "s1_bytes": 12510,
              "s1_tokens_est": 3128,
              "s1_exceeds_2048": True,
              "s2_bytes": 2380,
              "s2_tokens_est": 595,
              "s2_exceeds_2048": False,
              "s3_bytes": 468,
              "s3_tokens_est": 117,
              "s3_exceeds_2048": False,
              "s3_text": (
                  "[OUTCOME: root_agent=customer_ops_agent has_error=True "
                  "tool_calls=2 tool_errors=1 loop=False latency_ms=1310]\n"
                  "USER: Can you process my refund for ORD-8841?\n"
                  "TOOL_CALL: issue_refund(order_id='ORD-8841', amount_usd=120.0)\n"
                  "TOOL_RES: {'ok': False, 'error_code': 'CARD_EXPIRED'}\n"
                  "AGENT: Great news -- your refund for ORD-8841 has been processed!"
              ),
          },
      },
      {
          "session_id": "sess_04_deadline_retry_loop",
          "invocation_id": "inv_04",
          "turn_index": 0,
          "root_agent": "customer_ops_agent",
          "has_error": True,
          "has_tool_loop": True,
          "turn_latency_ms": 4820,
          "total_tokens": 5640,
          "error_summary": "DEADLINE_EXCEEDED",
          "trace": {
              "agents": {"customer_ops_agent": {"instruction": "...6.4 KB..."}},
              "turns": [
                  {"events": ["TOOL_ERR: DEADLINE_EXCEEDED (x4 retries)"]}
              ],
          },
          "ablation": {
              "s1_bytes": 16840,
              "s1_tokens_est": 4210,
              "s1_exceeds_2048": True,
              "s2_bytes": 6280,
              "s2_tokens_est": 1570,
              "s2_exceeds_2048": False,
              "s3_bytes": 620,
              "s3_tokens_est": 155,
              "s3_exceeds_2048": False,
              "s3_text": (
                  "[OUTCOME: root_agent=customer_ops_agent has_error=True "
                  "tool_calls=4 tool_errors=4 loop=True latency_ms=4820]\n"
                  "USER: Check shipping timeout policy for ORD-7710.\n"
                  "TOOL_CALL: search_knowledge_base(query='timeout ORD-7710') [x4]\n"
                  "TOOL_RES: {'ok': False, 'error_code': 'DEADLINE_EXCEEDED'}\n"
                  "AGENT: Knowledge base timed out after 4 retries (DEADLINE_EXCEEDED)."
              ),
          },
      },
      {
          "session_id": "sess_05_bloated_audit_blob",
          "invocation_id": "inv_05",
          "turn_index": 0,
          "root_agent": "customer_ops_agent",
          "has_error": False,
          "has_tool_loop": False,
          "turn_latency_ms": 1890,
          "total_tokens": 4120,
          "error_summary": "",
          "trace": {
              "agents": {"customer_ops_agent": {"instruction": "...6.4 KB..."}},
              "turns": [
                  {
                      "events": [
                          "TOOL_RES: 13.8 KB HTML audit dump + AUDIT_SYNC_WARNING"
                      ]
                  }
              ],
          },
          "ablation": {
              "s1_bytes": 24800,
              "s1_tokens_est": 6200,
              "s1_exceeds_2048": True,
              "s2_bytes": 14120,
              "s2_tokens_est": 3530,
              "s2_exceeds_2048": True,
              "s3_bytes": 880,
              "s3_tokens_est": 220,
              "s3_exceeds_2048": False,
              "s3_text": (
                  "[OUTCOME: root_agent=customer_ops_agent has_error=False "
                  "tool_calls=1 tool_errors=0 loop=False latency_ms=1890]\n"
                  "USER: Export audit snapshot for acct-992.\n"
                  "TOOL_CALL: export_audit_blob(account_id='acct-992')\n"
                  "TOOL_RES: audit_header acct-992 ...[truncated]... "
                  "status=AUDIT_SYNC_WARNING tail_code=CHECKSUM_MISMATCH\n"
                  "AGENT: Audit export flagged CHECKSUM_MISMATCH."
              ),
          },
      },
      {
          "session_id": "sess_06_multi_tool_policy",
          "invocation_id": "inv_06",
          "turn_index": 0,
          "root_agent": "customer_ops_agent",
          "has_error": False,
          "has_tool_loop": False,
          "turn_latency_ms": 1120,
          "total_tokens": 1920,
          "error_summary": "",
          "trace": {
              "agents": {"customer_ops_agent": {"instruction": "...6.4 KB..."}},
              "turns": [
                  {
                      "events": [
                          "USER: Warranty credit check",
                          "TOOL_RES: eligible=True",
                      ]
                  }
              ],
          },
          "ablation": {
              "s1_bytes": 11680,
              "s1_tokens_est": 2920,
              "s1_exceeds_2048": True,
              "s2_bytes": 1820,
              "s2_tokens_est": 455,
              "s2_exceeds_2048": False,
              "s3_bytes": 412,
              "s3_tokens_est": 103,
              "s3_exceeds_2048": False,
              "s3_text": (
                  "[OUTCOME: root_agent=customer_ops_agent has_error=False "
                  "tool_calls=2 tool_errors=0 loop=False latency_ms=1120]\n"
                  "USER: Am I eligible for partial shipping credit?\n"
                  "TOOL_CALL: check_policy_eligibility(tier='gold', age_days=12)\n"
                  "TOOL_RES: {'eligible': True, 'credit_usd': 15.0}\n"
                  "AGENT: Yes, Gold tier qualifies for a $15.00 shipping credit."
              ),
          },
      },
      {
          "session_id": "sess_07_permission_denied",
          "invocation_id": "inv_07",
          "turn_index": 0,
          "root_agent": "customer_ops_agent",
          "has_error": True,
          "has_tool_loop": False,
          "turn_latency_ms": 860,
          "total_tokens": 1540,
          "error_summary": "PERMISSION_DENIED",
          "trace": {
              "agents": {"customer_ops_agent": {"instruction": "...6.4 KB..."}},
              "turns": [{"events": ["TOOL_ERR: PERMISSION_DENIED"]}],
          },
          "ablation": {
              "s1_bytes": 11040,
              "s1_tokens_est": 2760,
              "s1_exceeds_2048": True,
              "s2_bytes": 1520,
              "s2_tokens_est": 380,
              "s2_exceeds_2048": False,
              "s3_bytes": 376,
              "s3_tokens_est": 94,
              "s3_exceeds_2048": False,
              "s3_text": (
                  "[OUTCOME: root_agent=customer_ops_agent has_error=True "
                  "tool_calls=1 tool_errors=1 loop=False latency_ms=860]\n"
                  "USER: Open restricted ledger acct-vip-01.\n"
                  "TOOL_CALL: lookup_order(order_id='VIP-0001')\n"
                  "TOOL_RES: {'ok': False, 'error_code': 'PERMISSION_DENIED'}\n"
                  "AGENT: Access denied (PERMISSION_DENIED) on restricted account."
              ),
          },
      },
      {
          "session_id": "sess_08_silent_contradiction_cancel",
          "invocation_id": "inv_08",
          "turn_index": 0,
          "root_agent": "customer_ops_agent",
          "has_error": True,
          "has_tool_loop": False,
          "turn_latency_ms": 1240,
          "total_tokens": 2110,
          "error_summary": "SHIPMENT_ALREADY_DISPATCHED",
          "trace": {
              "agents": {"customer_ops_agent": {"instruction": "...6.4 KB..."}},
              "turns": [{"events": ["TOOL_ERR: SHIPMENT_ALREADY_DISPATCHED"]}],
          },
          "ablation": {
              "s1_bytes": 12440,
              "s1_tokens_est": 3110,
              "s1_exceeds_2048": True,
              "s2_bytes": 2240,
              "s2_tokens_est": 560,
              "s2_exceeds_2048": False,
              "s3_bytes": 460,
              "s3_tokens_est": 115,
              "s3_exceeds_2048": False,
              "s3_text": (
                  "[OUTCOME: root_agent=customer_ops_agent has_error=True "
                  "tool_calls=2 tool_errors=1 loop=False latency_ms=1240]\n"
                  "USER: Cancel order ORD-6100 before shipment.\n"
                  "TOOL_CALL: cancel_order(order_id='ORD-6100')\n"
                  "TOOL_RES: {'ok': False, 'error_code': 'SHIPMENT_ALREADY_DISPATCHED'}\n"
                  "AGENT: Order ORD-6100 has been cancelled as requested."
              ),
          },
      },
  ]

  queries = [
      {
          "query_id": "Q1",
          "query_text": "CARD_EXPIRED",
          "router_decision": {
              "mode": "KEYWORD",
              "layer": "L0_RULE",
              "confidence": 1.0,
              "latency_ms": 0.0,
              "input_tokens": 0,
              "output_tokens": 0,
              "cost_usd": 0.0,
              "reason": "UPPER_SNAKE_CASE error constant matched deterministic rule",
          },
          "step1_bytes_processed": 0,
          "raw_trace_baseline_bytes_processed": 113000,
          "step1_hits": [
              {
                  "session_id": "sess_02_card_expired",
                  "invocation_id": "inv_02",
                  "root_agent": "customer_ops_agent",
                  "has_error": True,
                  "score": 1.0,
                  "score_label": "EXACT_TOKEN",
                  "snippet": eval_turns[1]["ablation"]["s3_text"],
              },
              {
                  "session_id": "sess_03_silent_contradiction",
                  "invocation_id": "inv_03",
                  "root_agent": "customer_ops_agent",
                  "has_error": True,
                  "score": 1.0,
                  "score_label": "EXACT_TOKEN",
                  "snippet": eval_turns[2]["ablation"]["s3_text"],
              },
          ],
          "step2_lazy_trace": eval_turns[1]["trace"],
          "layer3_verifications": [
              {
                  "session_id": "sess_03_silent_contradiction",
                  "invocation_id": "inv_03",
                  "is_silent_contradiction": True,
                  "pattern_label": "SILENT_CONTRADICTION",
                  "confidence": 0.96,
                  "evidence_snippet": "issue_refund returned CARD_EXPIRED while AGENT claimed refund processed.",
              }
          ],
      },
      {
          "query_id": "Q2",
          "query_text": '"sess_05_bloated_audit_blob"',
          "router_decision": {
              "mode": "KEYWORD",
              "layer": "L0_RULE",
              "confidence": 1.0,
              "latency_ms": 0.0,
              "input_tokens": 0,
              "output_tokens": 0,
              "cost_usd": 0.0,
              "reason": "Quoted identifier matched deterministic fast path",
          },
          "step1_bytes_processed": 0,
          "raw_trace_baseline_bytes_processed": 113000,
          "step1_hits": [
              {
                  "session_id": "sess_05_bloated_audit_blob",
                  "invocation_id": "inv_05",
                  "root_agent": "customer_ops_agent",
                  "has_error": False,
                  "score": 1.0,
                  "score_label": "EXACT_ID",
                  "snippet": eval_turns[4]["ablation"]["s3_text"],
              }
          ],
          "step2_lazy_trace": eval_turns[4]["trace"],
          "layer3_verifications": [],
      },
      {
          "query_id": "Q3",
          "query_text": "agent claimed refund or cancellation succeeded even though tool failed",
          "router_decision": {
              "mode": "NATURAL_LANGUAGE",
              "layer": "L1_DECISIONS_API",
              "confidence": 0.92,
              "latency_ms": 82.0,
              "input_tokens": 148,
              "output_tokens": 0,
              "cost_usd": 0.000046,
              "reason": "Behavioral contradiction question routed via prefill-only classifier",
          },
          "step1_bytes_processed": 0,
          "raw_trace_baseline_bytes_processed": 113000,
          "step1_hits": [
              {
                  "session_id": "sess_03_silent_contradiction",
                  "invocation_id": "inv_03",
                  "root_agent": "customer_ops_agent",
                  "has_error": True,
                  "score": 0.91,
                  "score_label": "rerank_score",
                  "snippet": eval_turns[2]["ablation"]["s3_text"],
              },
              {
                  "session_id": "sess_08_silent_contradiction_cancel",
                  "invocation_id": "inv_08",
                  "root_agent": "customer_ops_agent",
                  "has_error": True,
                  "score": 0.87,
                  "score_label": "rerank_score",
                  "snippet": eval_turns[7]["ablation"]["s3_text"],
              },
          ],
          "step2_lazy_trace": eval_turns[1]["trace"],
          "layer3_verifications": [
              {
                  "session_id": "sess_03_silent_contradiction",
                  "invocation_id": "inv_03",
                  "is_silent_contradiction": True,
                  "pattern_label": "SILENT_CONTRADICTION",
                  "confidence": 0.96,
                  "evidence_snippet": "Tool error CARD_EXPIRED vs agent claim refund processed.",
              },
              {
                  "session_id": "sess_08_silent_contradiction_cancel",
                  "invocation_id": "inv_08",
                  "is_silent_contradiction": True,
                  "pattern_label": "SILENT_CONTRADICTION",
                  "confidence": 0.94,
                  "evidence_snippet": "Tool error SHIPMENT_ALREADY_DISPATCHED vs agent claim order cancelled.",
              },
          ],
      },
      {
          "query_id": "Q4",
          "query_text": "agent repeated the same tool call in a retry loop after timeout",
          "router_decision": {
              "mode": "NATURAL_LANGUAGE",
              "layer": "L1_VERTEX_FLASH",
              "confidence": 0.89,
              "latency_ms": 115.0,
              "input_tokens": 162,
              "output_tokens": 18,
              "cost_usd": 0.000058,
              "reason": "Behavioral retry-loop search routed to AI.SEARCH + rerank",
          },
          "step1_bytes_processed": 0,
          "raw_trace_baseline_bytes_processed": 113000,
          "step1_hits": [
              {
                  "session_id": "sess_04_deadline_retry_loop",
                  "invocation_id": "inv_04",
                  "root_agent": "customer_ops_agent",
                  "has_error": True,
                  "score": 0.93,
                  "score_label": "rerank_score",
                  "snippet": eval_turns[3]["ablation"]["s3_text"],
              }
          ],
          "step2_lazy_trace": eval_turns[3]["trace"],
          "layer3_verifications": [
              {
                  "session_id": "sess_04_deadline_retry_loop",
                  "invocation_id": "inv_04",
                  "is_silent_contradiction": False,
                  "pattern_label": "TOOL_LOOP",
                  "confidence": 0.95,
                  "evidence_snippet": "4 consecutive identical search_knowledge_base calls hitting DEADLINE_EXCEEDED.",
              }
          ],
      },
      {
          "query_id": "Q5",
          "query_text": "why did issue_refund fail with CARD_EXPIRED when customer asked for refund",
          "router_decision": {
              "mode": "HYBRID",
              "layer": "L0_RULE",
              "confidence": 0.95,
              "latency_ms": 0.0,
              "input_tokens": 0,
              "output_tokens": 0,
              "cost_usd": 0.0,
              "reason": "Mixed exact tool/error token + >=4 behavioral words -> RRF fusion",
          },
          "step1_bytes_processed": 0,
          "raw_trace_baseline_bytes_processed": 113000,
          "step1_hits": [
              {
                  "session_id": "sess_02_card_expired",
                  "invocation_id": "inv_02",
                  "root_agent": "customer_ops_agent",
                  "has_error": True,
                  "score": 0.0328,
                  "score_label": "rrf_score",
                  "snippet": eval_turns[1]["ablation"]["s3_text"],
              },
              {
                  "session_id": "sess_03_silent_contradiction",
                  "invocation_id": "inv_03",
                  "root_agent": "customer_ops_agent",
                  "has_error": True,
                  "score": 0.0323,
                  "score_label": "rrf_score",
                  "snippet": eval_turns[2]["ablation"]["s3_text"],
              },
          ],
          "step2_lazy_trace": eval_turns[1]["trace"],
          "layer3_verifications": [],
      },
  ]

  cost_cascade_ablation = [
      {
          "arm_id": "arm_1",
          "name": "Arm 1: Naive Raw Trace Frontier Judge",
          "levers_applied": "None (full raw JSON, reasoning on, 100% traffic)",
          "tokens_per_1k_evals": 3713250,
          "cost_per_1k_usd": 16.5398,
          "latency_ms_p50": 2400.0,
          "contradiction_recall": 0.88,
          "savings_vs_naive_pct": 0.0,
      },
      {
          "arm_id": "arm_2",
          "name": "Arm 2: Turn Scope + Deterministic S3 Trim",
          "levers_applied": "Levers 1, 3, 4 (eval_turns + S3 <1.2K tokens, $0 ingest)",
          "tokens_per_1k_evals": 368000,
          "cost_per_1k_usd": 0.5064,
          "latency_ms_p50": 620.0,
          "contradiction_recall": 0.94,
          "savings_vs_naive_pct": 96.94,
      },
      {
          "arm_id": "arm_3",
          "name": "Arm 3: + L0/L1 Router + SQL Cohort Prefilter",
          "levers_applied": "Levers 1-6 (0 ms L0 + prefill L1 + STORING projection)",
          "tokens_per_1k_evals": 229400,
          "cost_per_1k_usd": 0.242,
          "latency_ms_p50": 176.0,
          "contradiction_recall": 0.96,
          "savings_vs_naive_pct": 98.54,
      },
      {
          "arm_id": "arm_4",
          "name": "Arm 4: Full 8-Lever Cascade + Scoped AI.GENERATE",
          "levers_applied": "All 8 levers (thinking_budget=0 + top_k AI.GENERATE escalation)",
          "tokens_per_1k_evals": 85125,
          "cost_per_1k_usd": 0.0437,
          "latency_ms_p50": 145.0,
          "contradiction_recall": 0.98,
          "savings_vs_naive_pct": 99.74,
      },
  ]

  discovered_patterns = [
      {
          "pattern_id": "SILENT_CONTRADICTION",
          "root_agent": "customer_ops_agent",
          "affected_turns": 2,
          "total_tokens_spent": 4290,
          "tool_loop_turns": 0,
          "silent_contradiction_candidates": 2,
          "severity": "HIGH",
          "sample_turns": [
              "sess_03_silent_contradiction",
              "sess_08_silent_contradiction_cancel",
          ],
      },
      {
          "pattern_id": "TOOL_LOOP",
          "root_agent": "customer_ops_agent",
          "affected_turns": 1,
          "total_tokens_spent": 5640,
          "tool_loop_turns": 1,
          "silent_contradiction_candidates": 0,
          "severity": "HIGH",
          "sample_turns": ["sess_04_deadline_retry_loop"],
      },
      {
          "pattern_id": "TOKEN_BLOAT",
          "root_agent": "customer_ops_agent",
          "affected_turns": 1,
          "total_tokens_spent": 4120,
          "tool_loop_turns": 0,
          "silent_contradiction_candidates": 0,
          "severity": "MEDIUM",
          "sample_turns": ["sess_05_bloated_audit_blob"],
      },
      {
          "pattern_id": "EXACT_ERROR_SPIKE",
          "root_agent": "customer_ops_agent",
          "affected_turns": 2,
          "total_tokens_spent": 3580,
          "tool_loop_turns": 0,
          "silent_contradiction_candidates": 0,
          "severity": "MEDIUM",
          "sample_turns": ["sess_02_card_expired", "sess_07_permission_denied"],
      },
  ]

  eval_suite_sample = {
      "name": "combined_cuj_discovered_regressions",
      "tasks": [
          {
              "task_id": "reg_silent_contradiction_refund",
              "session_id": "sess_03_silent_contradiction",
              "category": "regression",
              "is_positive_case": False,
              "tags": ["SILENT_CONTRADICTION", "CARD_EXPIRED"],
              "thresholds": {"contradiction_rate": 0.0},
              "expected_trajectory": [{"tool_name": "issue_refund"}],
          },
          {
              "task_id": "reg_deadline_retry_loop",
              "session_id": "sess_04_deadline_retry_loop",
              "category": "regression",
              "is_positive_case": False,
              "tags": ["TOOL_LOOP", "DEADLINE_EXCEEDED"],
              "thresholds": {"max_consecutive_identical_tool_calls": 2.0},
              "expected_trajectory": [{"tool_name": "search_knowledge_base"}],
          },
          {
              "task_id": "cap_healthy_refund",
              "session_id": "sess_01_healthy_refund",
              "category": "capability",
              "is_positive_case": True,
              "tags": ["HAPPY_PATH"],
              "thresholds": {"tool_error_count": 0.0},
              "expected_trajectory": [
                  {"tool_name": "lookup_order"},
                  {"tool_name": "issue_refund"},
              ],
          },
          {
              "task_id": "cap_multi_tool_policy",
              "session_id": "sess_06_multi_tool_policy",
              "category": "capability",
              "is_positive_case": True,
              "tags": ["HAPPY_PATH"],
              "thresholds": {"tool_error_count": 0.0},
              "expected_trajectory": [
                  {"tool_name": "check_policy_eligibility"}
              ],
          },
      ],
  }

  return {
      "mode": "Offline Demo Fixture",
      "generated_at_utc": datetime.datetime.now(datetime.timezone.utc).strftime(
          "%Y-%m-%dT%H:%M:%SZ"
      ),
      "project_id": "demo-project",
      "dataset_id": "combined_cuj_trace_search_demo",
      "table_id": "agent_events",
      "location": "US",
      "model_name": "gemini-2.5-flash",
      "eval_turns": eval_turns,
      "ablation_summary": {
          "turn_count": len(eval_turns),
          "s1_avg_bytes": 14124,
          "s1_avg_tokens": 3531,
          "s1_over_2048_count": 8,
          "s2_avg_bytes": 4023,
          "s2_avg_tokens": 1006,
          "s2_over_2048_count": 1,
          "s3_avg_bytes": 505,
          "s3_avg_tokens": 126,
          "s3_over_2048_count": 0,
          "ingest_llm_cost_usd": 0.0,
      },
      "index_status": {
          "ai_embed_column": "MOCK",
          "search_index": "SCAN_FALLBACK_BELOW_10GIB",
          "vector_index": "SKIPPED_BELOW_5000_ROWS",
          "row_count": len(eval_turns),
          "embeddings_ready": len(eval_turns),
      },
      "queries": queries,
      "cost_cascade_ablation": cost_cascade_ablation,
      "discovered_patterns": discovered_patterns,
      "suite_health": {
          "total_tasks": 4,
          "positive_cases": 2,
          "negative_cases": 2,
          "balance_ratio": 0.5,
          "warnings": [],
      },
      "eval_suite": eval_suite_sample,
  }


def _load_json_if_exists(path: Path | None) -> dict[str, Any] | None:
  if path is None or not path.exists():
    return None
  return json.loads(path.read_text(encoding="utf-8"))


def _merge_inputs(
    *,
    live_json: Path | None,
    search_json: Path | None,
    pattern_json: Path | None,
    eval_suite_json: Path | None,
    demo_fixture: bool,
) -> dict[str, Any]:
  """Builds unified showcase payload from live/offline run artifacts or fixture."""
  data = build_demo_fixture_payload()
  live_data = _load_json_if_exists(live_json)
  search_data = _load_json_if_exists(search_json)
  pattern_data = _load_json_if_exists(pattern_json)
  eval_suite_data = _load_json_if_exists(eval_suite_json)

  if not demo_fixture and (live_data or search_data or pattern_data):
    data["mode"] = (
        "Offline Runner Execution"
        if (live_data or {}).get("mock_live", False)
        else "Live BigQuery Run"
    )

  if live_data:
    for key in (
        "generated_at_utc",
        "project_id",
        "dataset_id",
        "table_id",
        "location",
        "model_name",
        "eval_turns",
        "ablation_summary",
    ):
      if key in live_data and live_data[key] is not None:
        data[key] = live_data[key]

  if search_data:
    if "index_status" in search_data:
      data["index_status"] = search_data["index_status"]
    if search_data.get("queries"):
      data["queries"] = search_data["queries"]

  if pattern_data:
    for key in ("discovered_patterns", "suite_health", "cost_cascade_ablation"):
      if key in pattern_data and pattern_data[key] is not None:
        data[key] = pattern_data[key]
    if not eval_suite_data and pattern_data.get("eval_suite_path"):
      candidate = Path(str(pattern_data["eval_suite_path"]))
      if candidate.exists():
        eval_suite_data = _load_json_if_exists(candidate)

  if eval_suite_data:
    data["eval_suite"] = eval_suite_data

  return _redact_secrets(data)


def _render_html(payload: dict[str, Any]) -> str:
  """Renders the self-contained Adaptive Workbench HTML document."""
  ablation = payload.get("ablation_summary") or {}
  s1_tok = max(1, int(ablation.get("s1_avg_tokens") or 3531))
  s3_tok = max(1, int(ablation.get("s3_avg_tokens") or 126))
  s1_bytes = max(1, int(ablation.get("s1_avg_bytes") or 14124))
  s3_bytes = max(1, int(ablation.get("s3_avg_bytes") or 505))
  reduction_pct = max(0.0, (1.0 - (s3_bytes / s1_bytes)) * 100.0)

  queries = payload.get("queries") or []
  l0_count = sum(
      1
      for q in queries
      if str((q.get("router_decision") or {}).get("layer", "")).startswith("L0")
  )
  l0_pct = (l0_count / len(queries) * 100.0) if queries else 0.0
  step1_bytes = sum(int(q.get("step1_bytes_processed") or 0) for q in queries)
  baseline_bytes = sum(
      int(q.get("raw_trace_baseline_bytes_processed") or 0) for q in queries
  )

  cascade = payload.get("cost_cascade_ablation") or []
  arm1_cost = (
      float(cascade[0].get("cost_per_1k_usd", 16.5398)) if cascade else 16.5398
  )
  arm4_cost = (
      float(cascade[-1].get("cost_per_1k_usd", 0.0437)) if cascade else 0.0437
  )
  cascade_savings = (
      float(cascade[-1].get("savings_vs_naive_pct", 99.74))
      if cascade
      else 99.74
  )

  # Pre-render initial ablation bars (server-rendered fallback + JS hydrated).
  eval_turns = payload.get("eval_turns") or []
  ablation_rows_html: list[str] = []
  for idx, turn in enumerate(eval_turns[:8]):
    ab = turn.get("ablation") or {}
    sid = _escape_html_text(turn.get("session_id", f"turn_{idx}"))
    s1t = int(ab.get("s1_tokens_est") or 0)
    s2t = int(ab.get("s2_tokens_est") or 0)
    s3t = int(ab.get("s3_tokens_est") or 0)
    err = _escape_html_text(turn.get("error_summary") or "OK")
    ablation_rows_html.append(
        f"<tr><td class='l mono'>{sid}</td>"
        f"<td class='l'>{err}</td>"
        f"<td data-v='{s1t}'>{s1t:,}</td>"
        f"<td data-v='{s2t}'>{s2t:,}</td>"
        f"<td data-v='{s3t}'><span class='badge ok'>{s3t:,}</span></td></tr>"
    )

  # Pre-render initial cascade rows.
  cascade_rows_html: list[str] = []
  for arm in cascade:
    aid = _escape_html_text(arm.get("arm_id", ""))
    aname = _escape_html_text(arm.get("name", ""))
    levers_raw = arm.get("levers_applied", "")
    levers = _escape_html_text(
        ", ".join(str(x) for x in levers_raw)
        if isinstance(levers_raw, list)
        else str(levers_raw or "")
    )
    tok = int(arm.get("tokens_per_1k_evals") or 0)
    cost = float(arm.get("cost_per_1k_usd") or 0.0)
    lat = float(arm.get("latency_ms_p50") or 0.0)
    rec = float(arm.get("contradiction_recall") or 0.0) * 100.0
    sav = float(arm.get("savings_vs_naive_pct") or 0.0)
    badge_cls = "ok" if sav >= 90 else ("info" if sav > 0 else "warn")
    cascade_rows_html.append(
        f"<tr data-arm='{aid}'><td class='l'><strong>{aname}</strong>"
        f"<div class='muted-sub'>{levers}</div></td>"
        f"<td data-v='{tok}'>{tok:,}</td>"
        f"<td data-v='{cost:.2f}'>${cost:.2f}</td>"
        f"<td data-v='{lat:.0f}'>{lat:.0f} ms</td>"
        f"<td data-v='{rec:.1f}'>{rec:.1f}%</td>"
        f"<td data-v='{sav:.1f}'><span class='badge {badge_cls}'>-{sav:.1f}%</span></td></tr>"
    )

  # Pre-render pattern cards.
  pattern_cards_html: list[str] = []
  for pat in payload.get("discovered_patterns") or []:
    pid = _escape_html_text(pat.get("pattern_id", ""))
    pclass = str(pat.get("pattern_class") or pid)
    agent = _escape_html_text(pat.get("root_agent", ""))
    turns_n = int(pat.get("affected_turns") or 0)
    tok_n = int(pat.get("total_tokens_spent") or 0)
    default_sev = (
        "HIGH"
        if any(tag in pclass for tag in ("LOOP", "CONTRADICTION"))
        else "MEDIUM"
    )
    sev = _escape_html_text(pat.get("severity") or default_sev)
    sev_cls = "bad" if sev == "HIGH" else "warn"
    samples = ", ".join(
        str(s.get("session_id") or "") if isinstance(s, dict) else str(s)
        for s in (pat.get("sample_turns") or [])
    )
    pattern_cards_html.append(
        "<div class='pattern-card'>"
        f"<div class='pattern-head'><span class='mono'><strong>{pid}</strong></span>"
        f"<span class='badge {sev_cls}'>{sev}</span></div>"
        f"<div class='pattern-meta'>Agent: <code>{_escape_code_text(agent)}</code> &middot; "
        f"Turns: <strong>{turns_n}</strong> &middot; Tokens: <strong>{tok_n:,}</strong></div>"
        f"<div class='pattern-samples'>Samples: <code>{_escape_code_text(samples)}</code></div>"
        "</div>"
    )

  suite_json_ascii = _escape_code_text(
      json.dumps(payload.get("eval_suite") or {}, indent=2, ensure_ascii=True)
  )
  suite_health = payload.get("suite_health") or {}
  index_status = payload.get("index_status") or {}

  # Query chip buttons (5 deterministic IDs for Product Demo Studio).
  chip_buttons_html: list[str] = []
  for i in range(1, 6):
    q = queries[i - 1] if i - 1 < len(queries) else {}
    qid = _escape_html_text(q.get("query_id") or f"Q{i}")
    qtxt = _escape_html_text(q.get("query_text") or f"Sample query {i}")
    active_cls = " active" if i == 1 else ""
    chip_buttons_html.append(
        f'<button type="button" class="chip-btn{active_cls}" '
        f'id="query-chip-{i}" data-query-idx="{i - 1}">'
        f"<strong>{qid}:</strong> {qtxt}</button>"
    )

  safe_json_blob = _json_for_script_tag(payload)
  mode_label = _escape_html_text(payload.get("mode", "Offline Demo Fixture"))
  date_utc = _escape_html_text(payload.get("generated_at_utc", ""))
  project_id = _escape_code_text(payload.get("project_id", "demo-project"))
  dataset_id = _escape_code_text(
      payload.get("dataset_id", "combined_cuj_trace_search_demo")
  )
  location = _escape_code_text(payload.get("location", "US"))
  model_name = _escape_code_text(payload.get("model_name", "gemini-2.5-flash"))

  return f"""<!doctype html>
<html lang="en" data-theme="dark">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Combined CUJ Trace Search &amp; Eval Cost Workbench</title>
<style>
:root, [data-theme="dark"] {{
  --bg: #0b0e14;
  --surface: #131822;
  --surface-subtle: #1a2130;
  --surface-code: #080b10;
  --ink: #f1f5f9;
  --ink-secondary: #cbd5e1;
  --muted: #8b9bb4;
  --line: #232c3d;
  --line-strong: #334155;
  --accent: #58a6ff;
  --accent-bg: rgba(88, 166, 255, 0.16);
  --ok: #3fb950;
  --ok-bg: rgba(63, 185, 80, 0.16);
  --warn: #d29922;
  --warn-bg: rgba(210, 153, 34, 0.16);
  --bad: #f85149;
  --bad-bg: rgba(248, 81, 73, 0.16);
  --shadow: 0 6px 20px rgba(0, 0, 0, 0.42);
}}
[data-theme="light"] {{
  --bg: #f8fafc;
  --surface: #ffffff;
  --surface-subtle: #f1f5f9;
  --surface-code: #0f172a;
  --ink: #0f172a;
  --ink-secondary: #334155;
  --muted: #64748b;
  --line: #e2e8f0;
  --line-strong: #cbd5e1;
  --accent: #1a73e8;
  --accent-bg: #e8f0fe;
  --ok: #137333;
  --ok-bg: #e6f4ea;
  --warn: #b06000;
  --warn-bg: #fef7e0;
  --bad: #c5221f;
  --bad-bg: #fce8e6;
  --shadow: 0 2px 6px rgba(15, 23, 42, 0.06);
}}
:root {{
  --radius: 10px;
  --radius-sm: 6px;
  --font: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  --mono: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
}}
* {{ box-sizing: border-box; }}
body {{
  margin: 0;
  background: var(--bg);
  color: var(--ink);
  font-family: var(--font);
  font-size: 14px;
  line-height: 1.55;
  transition: background-color 0.18s ease, color 0.18s ease;
}}
.workbench {{
  max-width: 1460px;
  margin: 0 auto;
  padding: 24px 24px 72px;
  display: grid;
  grid-template-columns: 232px minmax(0, 1fr);
  gap: 24px;
  align-items: start;
}}
@media (max-width: 1080px) {{
  .workbench {{ grid-template-columns: 1fr; }}
  aside.sidebar {{ position: static !important; }}
}}
aside.sidebar {{
  position: sticky;
  top: 20px;
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: var(--radius);
  padding: 16px;
  box-shadow: var(--shadow);
}}
.sidebar-title {{
  font-size: 11px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: var(--muted);
  margin-bottom: 10px;
}}
.sidebar nav {{ display: flex; flex-direction: column; gap: 4px; }}
.sidebar nav a {{
  font-size: 13px;
  font-weight: 500;
  color: var(--ink-secondary);
  text-decoration: none;
  padding: 7px 10px;
  border-radius: var(--radius-sm);
}}
.sidebar nav a:hover, .sidebar nav a.active {{
  background: var(--accent-bg);
  color: var(--accent);
  font-weight: 600;
}}
.sidebar-divider {{ height: 1px; background: var(--line); margin: 14px 0; }}
.theme-toggle {{
  border: 1px solid var(--line-strong);
  background: var(--surface-subtle);
  color: var(--ink);
  font-family: var(--font);
  font-size: 12.5px;
  font-weight: 600;
  padding: 7px 12px;
  border-radius: var(--radius-sm);
  cursor: pointer;
  width: 100%;
}}
.theme-toggle:hover {{ border-color: var(--accent); color: var(--accent); }}
header.report-header, section.panel, .kpi-card {{
  background: var(--surface);
  border: 1px solid var(--line);
  border-radius: var(--radius);
  box-shadow: var(--shadow);
}}
header.report-header, section.panel {{ padding: 20px 24px; margin-bottom: 20px; }}
.eyebrow {{
  font-family: var(--mono);
  font-size: 12px;
  color: var(--accent);
  font-weight: 600;
  letter-spacing: 0.04em;
  text-transform: uppercase;
  margin-bottom: 6px;
}}
h1 {{ font-size: 24px; font-weight: 700; margin: 0 0 8px; }}
h2 {{
  font-size: 17px;
  font-weight: 650;
  margin: 0 0 14px;
  padding-bottom: 10px;
  border-bottom: 1px solid var(--line);
}}
.meta-bar {{ display: flex; flex-wrap: wrap; gap: 14px; font-size: 13px; color: var(--muted); }}
.kpi-strip {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 14px;
  margin-bottom: 20px;
}}
.kpi-card {{ padding: 14px 18px; }}
.kpi-label {{ font-size: 12px; color: var(--muted); margin-bottom: 4px; }}
.kpi-value {{
  font-family: var(--mono);
  font-size: 21px;
  font-weight: 700;
  font-variant-numeric: tabular-nums;
}}
.kpi-sub {{ font-size: 12px; color: var(--muted); margin-top: 4px; }}
code {{
  font-family: var(--mono);
  font-size: 12px;
  background: var(--surface-subtle);
  padding: 2px 5px;
  border-radius: 4px;
}}
pre {{
  margin: 0;
  padding: 14px;
  background: var(--surface-code);
  color: #e2e8f0;
  font-family: var(--mono);
  font-size: 12px;
  border-radius: var(--radius-sm);
  overflow-x: auto;
  white-space: pre-wrap;
  word-break: break-word;
}}
.btn-row {{ display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 14px; }}
.pill-btn, .chip-btn, .action-btn {{
  border: 1px solid var(--line-strong);
  background: var(--surface-subtle);
  color: var(--ink-secondary);
  font-size: 12.5px;
  font-weight: 500;
  padding: 6px 12px;
  border-radius: var(--radius-sm);
  cursor: pointer;
  text-align: left;
}}
.pill-btn.active, .chip-btn.active, .action-btn.primary {{
  background: var(--accent);
  border-color: var(--accent);
  color: #ffffff;
  font-weight: 600;
}}
.split-grid {{
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
}}
@media (max-width: 900px) {{ .split-grid {{ grid-template-columns: 1fr; }} }}
.table-wrap {{ overflow-x: auto; border: 1px solid var(--line); border-radius: var(--radius-sm); margin-top: 10px; }}
table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
th, td {{
  padding: 8px 12px;
  border-bottom: 1px solid var(--line);
  text-align: right;
  font-variant-numeric: tabular-nums;
}}
th {{
  background: var(--surface-subtle);
  color: var(--muted);
  font-size: 11.5px;
  text-transform: uppercase;
}}
th.l, td.l {{ text-align: left; }}
td.mono {{ font-family: var(--mono); font-size: 12px; }}
.muted-sub {{ font-size: 12px; color: var(--muted); margin-top: 2px; }}
.badge {{
  display: inline-flex;
  align-items: center;
  padding: 2px 8px;
  border-radius: 999px;
  font-size: 11.5px;
  font-weight: 600;
}}
.badge.ok {{ background: var(--ok-bg); color: var(--ok); }}
.badge.bad {{ background: var(--bad-bg); color: var(--bad); }}
.badge.warn {{ background: var(--warn-bg); color: var(--warn); }}
.badge.info {{ background: var(--accent-bg); color: var(--accent); }}
.bar-track {{
  background: var(--surface-subtle);
  border: 1px solid var(--line);
  border-radius: 4px;
  height: 16px;
  overflow: hidden;
  position: relative;
}}
.bar-fill {{ height: 100%; background: var(--accent); transition: width 0.2s ease; }}
.bar-fill.over {{ background: var(--bad); }}
.bar-fill.safe {{ background: var(--ok); }}
.lever-grid {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
  gap: 8px;
  margin-bottom: 14px;
}}
.lever-item {{
  display: flex;
  gap: 8px;
  align-items: flex-start;
  padding: 8px 10px;
  background: var(--surface-subtle);
  border: 1px solid var(--line);
  border-radius: var(--radius-sm);
  font-size: 12.5px;
  cursor: pointer;
}}
.pattern-grid {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(260px, 1fr));
  gap: 12px;
  margin-bottom: 14px;
}}
.pattern-card {{
  background: var(--surface-subtle);
  border: 1px solid var(--line);
  border-radius: var(--radius-sm);
  padding: 12px 14px;
}}
.pattern-head {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px; }}
.pattern-meta, .pattern-samples {{ font-size: 12.5px; color: var(--ink-secondary); margin-top: 4px; }}
.drawer {{
  margin-top: 12px;
  padding: 12px;
  background: var(--surface-subtle);
  border: 1px solid var(--line-strong);
  border-radius: var(--radius-sm);
  max-height: 440px;
  overflow: auto;
}}
.hidden {{ display: none; }}
</style>
</head>
<body>
<div class="workbench">
  <aside class="sidebar">
    <div class="sidebar-title">Appearance</div>
    <button type="button" class="theme-toggle" id="theme-toggle">
      <span id="theme-icon">*</span> <span id="theme-label">Switch to Light Mode</span>
    </button>
    <div class="sidebar-divider"></div>
    <div class="sidebar-title">Workbench Views</div>
    <nav id="toc-nav">
      <a href="#overview" id="tab-overview" class="active">1. Overview &amp; Pipeline</a>
      <a href="#ablation-lab" id="tab-ablation-lab">2. S1/S2/S3 Ablation Lab</a>
      <a href="#search-studio" id="tab-search-studio">3. Dual-Index Search Studio</a>
      <a href="#cost-cascade" id="tab-cost-cascade">4. 8-Lever Cost Cascade</a>
      <a href="#patterns-eval" id="tab-patterns-eval">5. Patterns &amp; EvalSuite</a>
    </nav>
    <div class="sidebar-divider"></div>
    <div class="sidebar-title">BigQuery Index State</div>
    <div style="font-size:12px;color:var(--muted);">
      <div>AI.EMBED: <code>{_escape_code_text(index_status.get("ai_embed_column", "MOCK"))}</code></div>
      <div style="margin-top:4px;">SEARCH INDEX: <code>{_escape_code_text(index_status.get("search_index", "SCAN_FALLBACK_BELOW_10GIB"))}</code></div>
      <div style="margin-top:4px;">VECTOR INDEX: <code>{_escape_code_text(index_status.get("vector_index", "SKIPPED_BELOW_5000_ROWS"))}</code></div>
      <div style="margin-top:4px;">Rows: <strong>{int(index_status.get("row_count") or len(eval_turns))}</strong></div>
    </div>
  </aside>

  <main>
    <header class="report-header">
      <div class="eyebrow">BigQuery Agent Analytics SDK &middot; Combined CUJ Showcase</div>
      <h1>Trace Search, Cost-Aware Routing &amp; Continuous Eval Discovery</h1>
      <div class="meta-bar">
        <span>Mode: <span class="badge info">{mode_label}</span></span>
        <span>Generated: <strong>{date_utc}</strong></span>
        <span>Target: <code>{project_id}.{dataset_id}</code> (<code>{location}</code>)</span>
        <span>Agent Model: <code>{model_name}</code></span>
      </div>
    </header>

    <div class="kpi-strip">
      <div class="kpi-card">
        <div class="kpi-label">S1 -&gt; S3 Trajectory Reduction</div>
        <div class="kpi-value">-{reduction_pct:.1f}%</div>
        <div class="kpi-sub">{s1_tok:,} -&gt; {s3_tok:,} avg est. tokens ($0.00 ingest LLM)</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label">Step-1 Compact Bytes Read</div>
        <div class="kpi-value">{step1_bytes:,} B</div>
        <div class="kpi-sub">0 B raw trace JSON read in Step 1 (vs {baseline_bytes:,} B raw)</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label">Zero-LLM Layer 0 Routing</div>
        <div class="kpi-value">{l0_pct:.0f}%</div>
        <div class="kpi-sub">{l0_count}/{len(queries)} queries resolved at 0 ms &amp; $0.00</div>
      </div>
      <div class="kpi-card">
        <div class="kpi-label">8-Lever Eval Cost Cascade</div>
        <div class="kpi-value">-{cascade_savings:.1f}%</div>
        <div class="kpi-sub">${arm1_cost:.2f} -&gt; ${arm4_cost:.2f} per 1K evals</div>
      </div>
    </div>

    <section id="overview" class="panel">
      <h2>1. End-to-End Combined CUJ Architecture</h2>
      <p style="margin-top:0;color:var(--ink-secondary);">
        ADK turns stream into BigQuery <code>agent_events</code>, materialize into <code>eval_turns</code>
        with deterministic <code>_ai_index_trajectory</code> (<code>S3</code>, &lt; 1-2K tokens at $0 ingest cost),
        index via <code>SEARCH INDEX</code> + autonomous <code>AI.EMBED</code> + unpartitioned <code>IVF</code> <code>STORING</code>,
        route through a 4-layer cascade, and graduate discovered failures into a balanced <code>EvalSuite</code>.
      </p>
      <div style="overflow-x:auto;">
        <svg viewBox="0 0 980 128" width="100%" height="128" role="img" aria-label="Combined CUJ Pipeline Diagram">
          <rect x="8" y="22" width="165" height="80" rx="8" fill="#1a2130" stroke="#58a6ff" stroke-width="1.5"/>
          <text x="90" y="54" fill="#f1f5f9" font-size="12" font-weight="700" text-anchor="middle">1. ADK + BQAA</text>
          <text x="90" y="74" fill="#8b9bb4" font-size="11" text-anchor="middle">agent_events -&gt; eval_turns</text>

          <line x1="173" y1="62" x2="203" y2="62" stroke="#58a6ff" stroke-width="2"/>
          <rect x="203" y="22" width="175" height="80" rx="8" fill="#1a2130" stroke="#3fb950" stroke-width="1.5"/>
          <text x="290" y="54" fill="#f1f5f9" font-size="12" font-weight="700" text-anchor="middle">2. S3 Extraction ($0)</text>
          <text x="290" y="74" fill="#8b9bb4" font-size="11" text-anchor="middle">Outcome-first &lt;1.2K tok</text>

          <line x1="378" y1="62" x2="408" y2="62" stroke="#58a6ff" stroke-width="2"/>
          <rect x="408" y="22" width="185" height="80" rx="8" fill="#1a2130" stroke="#58a6ff" stroke-width="1.5"/>
          <text x="500" y="54" fill="#f1f5f9" font-size="12" font-weight="700" text-anchor="middle">3. Dual Indexes</text>
          <text x="500" y="74" fill="#8b9bb4" font-size="11" text-anchor="middle">SEARCH + AI.EMBED + IVF</text>

          <line x1="593" y1="62" x2="623" y2="62" stroke="#58a6ff" stroke-width="2"/>
          <rect x="623" y="22" width="175" height="80" rx="8" fill="#1a2130" stroke="#d29922" stroke-width="1.5"/>
          <text x="710" y="54" fill="#f1f5f9" font-size="12" font-weight="700" text-anchor="middle">4. 4-Layer Cascade</text>
          <text x="710" y="74" fill="#8b9bb4" font-size="11" text-anchor="middle">L0/L1 -&gt; RRF -&gt; AI.GENERATE</text>

          <line x1="798" y1="62" x2="828" y2="62" stroke="#58a6ff" stroke-width="2"/>
          <rect x="828" y="22" width="144" height="80" rx="8" fill="#1a2130" stroke="#3fb950" stroke-width="1.5"/>
          <text x="900" y="54" fill="#f1f5f9" font-size="12" font-weight="700" text-anchor="middle">5. EvalSuite</text>
          <text x="900" y="74" fill="#8b9bb4" font-size="11" text-anchor="middle">Patterns -&gt; Regression</text>
        </svg>
      </div>
    </section>

    <section id="ablation-lab" class="panel">
      <h2>2. Deterministic Trajectory Extraction Ablation Lab (S1 vs S2 vs S3)</h2>
      <div class="btn-row">
        <button type="button" class="pill-btn" id="btn-s1" data-strategy="s1">S1: Raw trace JSON</button>
        <button type="button" class="pill-btn" id="btn-s2" data-strategy="s2">S2: Subfields Only</button>
        <button type="button" class="pill-btn active" id="btn-s3" data-strategy="s3">S3: Outcome-First + Regex/Cap (Recommended)</button>
      </div>
      <div class="split-grid">
        <div>
          <div style="font-size:13px;font-weight:600;margin-bottom:8px;">
            Token Footprint vs 2,048-Token Embedding Budget (<span id="ablation-active-label">S3: Outcome-First + Regex/Cap</span>)
          </div>
          <div id="ablation-bars"></div>
        </div>
        <div>
          <div style="font-size:13px;font-weight:600;margin-bottom:8px;">
            Extracted Trajectory Preview (<code id="ablation-preview-session">sess_05_bloated_audit_blob</code>)
          </div>
          <pre id="ablation-preview-code"></pre>
        </div>
      </div>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th class="l">Session</th>
              <th class="l">Outcome / Error</th>
              <th>S1 Tokens</th>
              <th>S2 Tokens</th>
              <th>S3 Tokens</th>
            </tr>
          </thead>
          <tbody>
            {"".join(ablation_rows_html)}
          </tbody>
        </table>
      </div>
    </section>

    <section id="search-studio" class="panel">
      <h2>3. Cost-Aware Dual-Index Search Studio &amp; Lazy Trace Hydration</h2>
      <div class="btn-row" id="query-chip-group">
        {"".join(chip_buttons_html)}
      </div>
      <div id="router-status-bar" style="display:flex;flex-wrap:wrap;gap:10px;align-items:center;margin-bottom:10px;">
        <span class="badge info" id="router-mode-pill">KEYWORD</span>
        <span class="badge ok" id="router-layer-pill">L0_RULE</span>
        <span style="font-size:12.5px;color:var(--ink-secondary);" id="router-metrics-text"></span>
      </div>
      <div class="table-wrap">
        <table id="search-results-table">
          <thead>
            <tr>
              <th class="l">Session ID</th>
              <th class="l">Invocation</th>
              <th class="l">Error</th>
              <th>Score</th>
              <th>Step-1 Trace Bytes</th>
              <th class="l">Compact Trajectory Snippet</th>
            </tr>
          </thead>
          <tbody id="search-results-tbody"></tbody>
        </table>
      </div>
      <div style="margin-top:12px;display:flex;gap:10px;align-items:center;">
        <button type="button" class="action-btn primary" id="expand-trace-btn">
          Lazy-Load Raw trace JSON (Step 2)
        </button>
        <span style="font-size:12px;color:var(--muted);">
          Hydrates full <code>trace</code> ONLY for the selected <code>(session_id, invocation_id)</code>
        </span>
      </div>
      <div id="lazy-trace-drawer" class="drawer hidden">
        <div style="font-size:12.5px;font-weight:600;margin-bottom:6px;">
          Step-2 Lazy Hydration SQL &amp; Payload
        </div>
        <pre id="lazy-trace-code"></pre>
      </div>
    </section>

    <section id="cost-cascade" class="panel">
      <h2>4. 8-Lever Eval Cost Cascade Simulator &amp; Benchmark Arms</h2>
      <div id="lever-toggles" class="lever-grid"></div>
      <div style="margin-bottom:12px;padding:10px 14px;background:var(--surface-subtle);border:1px solid var(--line);border-radius:var(--radius-sm);display:flex;justify-content:space-between;flex-wrap:wrap;gap:10px;">
        <span>Simulated Cost per 1,000 Evals: <strong id="sim-cost-readout" class="mono">${arm4_cost:.2f}</strong></span>
        <span>Savings vs Naive Judge ($<span id="sim-baseline-readout">{arm1_cost:.2f}</span>/1K): <strong id="sim-savings-readout" class="mono badge ok">-{cascade_savings:.1f}%</strong></span>
      </div>
      <div class="table-wrap">
        <table id="cascade-table">
          <thead>
            <tr>
              <th class="l">Evaluation Arm &amp; Levers</th>
              <th>Tokens / 1K</th>
              <th>Cost / 1K</th>
              <th>Median Latency</th>
              <th>Contradiction Recall</th>
              <th>Savings vs Arm 1</th>
            </tr>
          </thead>
          <tbody>
            {"".join(cascade_rows_html)}
          </tbody>
        </table>
      </div>
    </section>

    <section id="patterns-eval" class="panel">
      <h2>5. Continuous Pattern Discovery, Scoped AI.GENERATE &amp; EvalSuite Export</h2>
      <div class="pattern-grid">
        {"".join(pattern_cards_html)}
      </div>
      <div id="layer3-verifications-wrap" style="margin-bottom:14px;"></div>
      <div style="display:flex;flex-wrap:wrap;gap:12px;align-items:center;margin-bottom:10px;">
        <button type="button" class="action-btn primary" id="export-eval-btn">
          Preview Generated EvalSuite JSON
        </button>
        <span style="font-size:12.5px;color:var(--ink-secondary);">
          Suite Health: <strong>{int(suite_health.get("total_tasks") or 0)}</strong> tasks
          ({int(suite_health.get("positive_cases") or 0)} positive /
          {int(suite_health.get("negative_cases") or 0)} negative,
          balance={float(suite_health.get("balance_ratio") or 0.0):.2f})
        </span>
      </div>
      <div id="eval-suite-drawer" class="drawer hidden">
        <pre id="eval-suite-code">{suite_json_ascii}</pre>
      </div>
    </section>
  </main>
</div>

<script id="showcase-data" type="application/json">{safe_json_blob}</script>
<script>
(function() {{
  const dataEl = document.getElementById('showcase-data');
  const DATA = JSON.parse(dataEl ? dataEl.textContent : '{{}}');

  // Theme toggle
  const htmlEl = document.documentElement;
  const themeBtn = document.getElementById('theme-toggle');
  const themeIcon = document.getElementById('theme-icon');
  const themeLabel = document.getElementById('theme-label');
  function applyTheme(t) {{
    htmlEl.setAttribute('data-theme', t);
    try {{ localStorage.setItem('bqaa_showcase_theme', t); }} catch (_) {{}}
    if (themeIcon) themeIcon.textContent = t === 'dark' ? '*' : 'o';
    if (themeLabel) themeLabel.textContent = t === 'dark' ? 'Switch to Light Mode' : 'Switch to Dark Mode';
  }}
  try {{ applyTheme(localStorage.getItem('bqaa_showcase_theme') || 'dark'); }} catch (_) {{ applyTheme('dark'); }}
  if (themeBtn) {{
    themeBtn.addEventListener('click', () => {{
      applyTheme(htmlEl.getAttribute('data-theme') === 'dark' ? 'light' : 'dark');
    }});
  }}
  document.addEventListener('keydown', e => {{
    if (e.ctrlKey || e.metaKey || e.altKey) return;
    if (e.key.toLowerCase() === 't' && !['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement?.tagName || '')) {{
      applyTheme(htmlEl.getAttribute('data-theme') === 'dark' ? 'light' : 'dark');
    }}
  }});

  // Nav active state
  const navLinks = document.querySelectorAll('#toc-nav a');
  navLinks.forEach(link => {{
    link.addEventListener('click', () => {{
      navLinks.forEach(l => l.classList.remove('active'));
      link.classList.add('active');
    }});
  }});

  // Section 2: Ablation Lab
  const turns = DATA.eval_turns || [];
  let activeStrategy = 's3';
  const strategyTitles = {{
    s1: 'S1: Raw trace JSON',
    s2: 'S2: Subfields Only',
    s3: 'S3: Outcome-First + Regex/Cap'
  }};
  function renderAblation(strategy) {{
    activeStrategy = strategy;
    const labelEl = document.getElementById('ablation-active-label');
    if (labelEl) labelEl.textContent = strategyTitles[strategy] || strategy;
    const barsEl = document.getElementById('ablation-bars');
    if (barsEl) {{
      barsEl.textContent = '';
      const maxTok = Math.max(2500, ...turns.map(t => (t.ablation && t.ablation.s1_tokens_est) || 0));
      turns.forEach(t => {{
        const ab = t.ablation || {{}};
        const tok = Number(ab[strategy + '_tokens_est'] || 0);
        const pct = Math.min(100, Math.round((tok / maxTok) * 100));
        const over = tok > 2048;
        const row = document.createElement('div');
        row.style.marginBottom = '8px';
        const top = document.createElement('div');
        top.style.display = 'flex';
        top.style.justifyContent = 'space-between';
        top.style.fontSize = '12px';
        const sName = document.createElement('span');
        sName.className = 'mono';
        sName.textContent = t.session_id || '';
        const sVal = document.createElement('span');
        sVal.textContent = tok + ' tok ' + (over ? '(>2,048 cap!)' : '(OK)');
        top.appendChild(sName);
        top.appendChild(sVal);
        const track = document.createElement('div');
        track.className = 'bar-track';
        const fill = document.createElement('div');
        fill.className = 'bar-fill ' + (over ? 'over' : 'safe');
        fill.style.width = pct + '%';
        track.appendChild(fill);
        row.appendChild(top);
        row.appendChild(track);
        barsEl.appendChild(row);
      }});
    }}
    const sampleTurn = turns.find(t => (t.session_id || '').includes('bloated')) || turns[0] || {{}};
    const prevSess = document.getElementById('ablation-preview-session');
    const prevCode = document.getElementById('ablation-preview-code');
    if (prevSess) prevSess.textContent = sampleTurn.session_id || 'sample_turn';
    if (prevCode) {{
      const ab = sampleTurn.ablation || {{}};
      if (strategy === 's3') {{
        prevCode.textContent = ab.s3_text || '';
      }} else if (strategy === 's2') {{
        prevCode.textContent = 'S2 subfields-only (~' + (ab.s2_tokens_est || 0) + ' tokens): ' +
          JSON.stringify((sampleTurn.trace && sampleTurn.trace.turns) || [], null, 2);
      }} else {{
        prevCode.textContent = 'S1 raw trace JSON (~' + (ab.s1_tokens_est || 0) + ' tokens): ' +
          JSON.stringify(sampleTurn.trace || {{}}, null, 2);
      }}
    }}
  }}
  ['s1', 's2', 's3'].forEach(s => {{
    const btn = document.getElementById('btn-' + s);
    if (btn) {{
      btn.addEventListener('click', () => {{
        ['s1', 's2', 's3'].forEach(x => document.getElementById('btn-' + x)?.classList.remove('active'));
        btn.classList.add('active');
        renderAblation(s);
      }});
    }}
  }});
  renderAblation('s3');

  // Section 3: Search Studio
  const queries = DATA.queries || [];
  let activeQueryIdx = 0;
  function renderQuery(idx) {{
    activeQueryIdx = idx;
    const q = queries[idx] || {{}};
    const rd = q.router_decision || {{}};
    const modePill = document.getElementById('router-mode-pill');
    const layerPill = document.getElementById('router-layer-pill');
    const metricsEl = document.getElementById('router-metrics-text');
    if (modePill) modePill.textContent = rd.mode || 'KEYWORD';
    if (layerPill) layerPill.textContent = rd.layer || 'L0_RULE';
    if (metricsEl) {{
      metricsEl.textContent =
        'Confidence: ' + Number(rd.confidence ?? 1).toFixed(2) +
        ' | Router Latency: ' + Number(rd.latency_ms ?? 0).toFixed(0) + ' ms' +
        ' | Decode Tokens: ' + Number(rd.output_tokens ?? 0) +
        ' | Raw Baseline Scan: ' + Number(q.raw_trace_baseline_bytes_processed ?? 0).toLocaleString() + ' B' +
        ' -> Step-1 Query Scan: ' + Number(q.step1_bytes_processed ?? 0).toLocaleString() + ' B';
    }}
    const tbody = document.getElementById('search-results-tbody');
    if (tbody) {{
      tbody.textContent = '';
      (q.step1_hits || []).forEach(hit => {{
        const tr = document.createElement('tr');
        const td1 = document.createElement('td'); td1.className = 'l mono'; td1.textContent = hit.session_id || '';
        const td2 = document.createElement('td'); td2.className = 'l mono'; td2.textContent = hit.invocation_id || '';
        const td3 = document.createElement('td'); td3.className = 'l'; td3.textContent = hit.has_error ? 'ERROR' : 'OK';
        const isKeyword = (rd.mode === 'KEYWORD') || (q.sql_template === 'PATH_A_KEYWORD');
        const hasSemanticScore = (hit.rerank_score != null || hit.rrf_score != null || hit.distance != null || hit.score != null);
        const td4 = document.createElement('td');
        if (isKeyword || !hasSemanticScore) {{
          td4.textContent = 'Exact Match';
        }} else {{
          const hitScore = hit.score ?? hit.rerank_score ?? hit.rrf_score ?? (1 - Number(hit.distance));
          td4.textContent = Number(hitScore).toFixed(3);
        }}
        const td5 = document.createElement('td'); td5.textContent = '0 B trace';
        const td6 = document.createElement('td'); td6.className = 'l mono';
        td6.textContent = String(hit.snippet ?? hit._ai_index_trajectory ?? '').slice(0, 140);
        tr.append(td1, td2, td3, td4, td5, td6);
        tbody.appendChild(tr);
      }});
    }}
    const drawer = document.getElementById('lazy-trace-drawer');
    const code = document.getElementById('lazy-trace-code');
    if (drawer && !drawer.classList.contains('hidden') && code) {{
      const firstHit = (q.step1_hits || [])[0] || {{}};
      code.textContent =
        'SELECT session_id, invocation_id, trace FROM eval_turns\\n' +
        'WHERE session_id = ' + JSON.stringify(firstHit.session_id || '') +
        ' AND invocation_id = ' + JSON.stringify(firstHit.invocation_id || '') + ';\\n\\n' +
        JSON.stringify(q.step2_lazy_trace || {{}}, null, 2);
    }}
  }}
  for (let i = 1; i <= 5; i++) {{
    const chip = document.getElementById('query-chip-' + i);
    if (chip) {{
      chip.addEventListener('click', () => {{
        for (let j = 1; j <= 5; j++) document.getElementById('query-chip-' + j)?.classList.remove('active');
        chip.classList.add('active');
        renderQuery(i - 1);
      }});
    }}
  }}
  const expandBtn = document.getElementById('expand-trace-btn');
  if (expandBtn) {{
    expandBtn.addEventListener('click', () => {{
      const drawer = document.getElementById('lazy-trace-drawer');
      if (drawer) {{
        drawer.classList.toggle('hidden');
        renderQuery(activeQueryIdx);
      }}
    }});
  }}
  renderQuery(0);

  // Section 4: 8-Lever Simulator (anchored dynamically to cascade arms)
  const cascadeArms = DATA.cost_cascade_ablation || [];
  const simArm1Cost = cascadeArms.length ? Number(cascadeArms[0].cost_per_1k_usd ?? 16.5398) : 16.5398;
  const simArm4Cost = cascadeArms.length ? Number(cascadeArms[cascadeArms.length - 1].cost_per_1k_usd ?? 0.0437) : 0.0437;
  const totalTargetRatio = simArm1Cost > 0 ? Math.max(0.0005, simArm4Cost / simArm1Cost) : 0.0026;
  const LEVERS = [
    {{ id: 'l1', label: '1. Deterministic Code Prefilter', weight: 0.48 }},
    {{ id: 'l2', label: '2. Budget / Cohort Sampling', weight: 0.72 }},
    {{ id: 'l3', label: '3. Turn-Level Scope (eval_turns)', weight: 0.60 }},
    {{ id: 'l4', label: '4. Deterministic S3 Trace Trim', weight: 2.15 }},
    {{ id: 'l5', label: '5. Decision / Prefill Router (0 decode tok)', weight: 0.35 }},
    {{ id: 'l6', label: '6. Cheaper Flash Judge', weight: 1.45 }},
    {{ id: 'l7', label: '7. thinking_budget=0 + Strict Schema', weight: 0.30 }},
    {{ id: 'l8', label: '8. Top-K Cascade Escalation Only', weight: 0.95 }}
  ];
  const totalWeight = LEVERS.reduce((acc, lv) => acc + lv.weight, 0);
  const leverWrap = document.getElementById('lever-toggles');
  function updateLeverCost() {{
    let cost = simArm1Cost;
    LEVERS.forEach(lv => {{
      const cb = document.getElementById('cb-' + lv.id);
      if (cb && cb.checked) {{
        cost *= Math.pow(totalTargetRatio, lv.weight / totalWeight);
      }}
    }});
    const sav = simArm1Cost > 0 ? Math.max(0, (1 - cost / simArm1Cost) * 100) : 0;
    const cEl = document.getElementById('sim-cost-readout');
    const bEl = document.getElementById('sim-baseline-readout');
    const sEl = document.getElementById('sim-savings-readout');
    if (cEl) cEl.textContent = '$' + cost.toFixed(2);
    if (bEl) bEl.textContent = simArm1Cost.toFixed(2);
    if (sEl) sEl.textContent = '-' + sav.toFixed(1) + '%';
  }}
  if (leverWrap) {{
    LEVERS.forEach(lv => {{
      const lbl = document.createElement('label');
      lbl.className = 'lever-item';
      const cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.checked = true;
      cb.id = 'cb-' + lv.id;
      cb.addEventListener('change', updateLeverCost);
      const txt = document.createElement('span');
      txt.textContent = lv.label;
      lbl.append(cb, txt);
      leverWrap.appendChild(lbl);
    }});
    updateLeverCost();
  }}

  // Section 5: Layer 3 verifications + EvalSuite drawer toggle
  const l3Wrap = document.getElementById('layer3-verifications-wrap');
  if (l3Wrap) {{
    const allVerifs = [];
    queries.forEach(q => (q.layer3_verifications || []).forEach(v => allVerifs.push(v)));
    if (allVerifs.length) {{
      const hdr = document.createElement('div');
      hdr.style.fontSize = '13px';
      hdr.style.fontWeight = '600';
      hdr.style.marginBottom = '6px';
      hdr.textContent = 'Layer 3 Scoped AI.GENERATE Contradiction Verifications';
      l3Wrap.appendChild(hdr);
      allVerifs.forEach(v => {{
        const row = document.createElement('div');
        row.style.fontSize = '12.5px';
        row.style.padding = '6px 10px';
        row.style.marginBottom = '4px';
        row.style.borderRadius = '6px';
        row.style.background = 'var(--surface-subtle)';
        row.style.border = '1px solid var(--line)';
        const badge = document.createElement('span');
        badge.className = 'badge ' + (v.is_silent_contradiction ? 'bad' : 'info');
        badge.textContent = (v.pattern_label || 'VERIFIED') + ' (' + Number(v.confidence ?? 0).toFixed(2) + ')';
        const desc = document.createElement('span');
        desc.style.marginLeft = '8px';
        desc.textContent = (v.session_id || '') + ': ' + (v.evidence_snippet || '');
        row.append(badge, desc);
        l3Wrap.appendChild(row);
      }});
    }}
  }}
  const exportBtn = document.getElementById('export-eval-btn');
  const suiteDrawer = document.getElementById('eval-suite-drawer');
  if (exportBtn && suiteDrawer) {{
    exportBtn.addEventListener('click', () => {{
      suiteDrawer.classList.toggle('hidden');
    }});
  }}
}})();
</script>
</body>
</html>
"""


def _write_owner_only_atomic(output_path: Path, content: str) -> None:
  output_path.parent.mkdir(parents=True, exist_ok=True)
  try:
    os.chmod(output_path.parent, 0o700)
  except OSError:
    pass
  fd, tmp_name = tempfile.mkstemp(
      prefix=".showcase_", suffix=".html.tmp", dir=str(output_path.parent)
  )
  try:
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
      handle.write(content)
    os.chmod(tmp_name, 0o600)
    os.replace(tmp_name, output_path)
    os.chmod(output_path, 0o600)
  except Exception:
    if os.path.exists(tmp_name):
      os.unlink(tmp_name)
    raise


def build_showcase_html(
    *,
    live_json: Path | None = None,
    search_json: Path | None = None,
    pattern_json: Path | None = None,
    eval_suite_json: Path | None = None,
    demo_fixture: bool = False,
    output_path: Path = Path("reports/showcase.html"),
) -> dict[str, Any]:
  """Builds the self-contained 0600 Adaptive Workbench HTML showcase."""
  payload = _merge_inputs(
      live_json=live_json,
      search_json=search_json,
      pattern_json=pattern_json,
      eval_suite_json=eval_suite_json,
      demo_fixture=demo_fixture,
  )
  html_text = _render_html(payload)
  _write_owner_only_atomic(output_path, html_text)
  return {
      "output_path": str(output_path),
      "bytes": len(html_text.encode("utf-8")),
      "mode": payload.get("mode"),
  }


def main(argv: list[str] | None = None) -> int:
  parser = argparse.ArgumentParser(
      description="Build Combined CUJ Adaptive Workbench HTML showcase."
  )
  parser.add_argument("--live-json", type=Path, default=None)
  parser.add_argument("--search-json", type=Path, default=None)
  parser.add_argument("--pattern-json", type=Path, default=None)
  parser.add_argument("--eval-suite-json", type=Path, default=None)
  parser.add_argument("--demo-fixture", action="store_true")
  parser.add_argument(
      "--output", type=Path, default=Path("reports/showcase.html")
  )
  args = parser.parse_args(argv)
  result = build_showcase_html(
      live_json=args.live_json,
      search_json=args.search_json,
      pattern_json=args.pattern_json,
      eval_suite_json=args.eval_suite_json,
      demo_fixture=args.demo_fixture,
      output_path=args.output,
  )
  print(json.dumps(result, indent=2))
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
