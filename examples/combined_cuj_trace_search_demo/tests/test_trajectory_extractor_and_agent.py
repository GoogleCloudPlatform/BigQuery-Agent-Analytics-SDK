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

"""Hermetic unit tests for Slice 1: trajectory extractor, tools, scenarios, and runner."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
import importlib
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import tempfile
import threading
import types
from types import SimpleNamespace
import unittest
from unittest import mock

DEMO_DIR = Path(__file__).resolve().parents[1]
if str(DEMO_DIR) not in sys.path:
  sys.path.insert(0, str(DEMO_DIR))


def _timed_scrub_median_sec(
    fn: Callable[[str], str],
    payload: str,
    *,
    trials: int = 3,
) -> tuple[str, float]:
  """Warms up and measures median elapsed seconds across ``trials`` calls."""
  import time  # noqa: PLC0415

  out = fn(payload)
  times: list[float] = []
  for _ in range(trials):
    t0 = time.perf_counter()
    out = fn(payload)
    times.append(time.perf_counter() - t0)
  return out, sorted(times)[len(times) // 2]


def _make_bloated_trace(*, tail_error: str = "CARD_EXPIRED") -> dict:
  """Builds a realistic AgentData trace with ~7 KB system prompt + >25 KB payload."""
  long_instruction = ("Enterprise Customer Operations Policy. " * 220).strip()
  html_rows = "".join(
      f"<div class='audit-row' data-idx='{i}'><span>2026-11-24T00:00:{i%60:02d}Z</span>"
      f"<span>0x{i:016x}{'deadbeef'*4}</span></div>"
      for i in range(180)
  )
  bloated_payload = {
      "audit_html": f"<html><body>{html_rows}</body></html>",
      "hex_trace": "0x" + ("abcd1234" * 400),
      "auth_debug": "Authorization: Bearer tok_secret_value_12345",
      "api_key": "AIza" + "SyTestSampleKey000111222333",
      "status": "FAILED",
      "error_code": tail_error,
      "message": f"Terminal downstream rejection: {tail_error}",
  }
  return {
      "agents": {
          "customer_ops_agent": {
              "agent_id": "customer_ops_agent",
              "instruction": long_instruction,
              "description": "Enterprise billing, refund, and order assistant.",
              "sub_agents": [],
              "tools": [
                  {
                      "function_declarations": [
                          {
                              "name": "lookup_order",
                              "description": "Fetch order metadata " * 15,
                              "parameters_json_schema": {
                                  "type": "object",
                                  "properties": {
                                      "order_id": {"type": "string"}
                                  },
                              },
                          },
                          {
                              "name": "issue_refund",
                              "description": "Issue customer refund " * 15,
                              "parameters_json_schema": {
                                  "type": "object",
                                  "properties": {
                                      "order_id": {"type": "string"},
                                      "reason": {"type": "string"},
                                      "amount_usd": {"type": "number"},
                                  },
                              },
                          },
                      ]
                  }
              ],
          }
      },
      "turns": [
          {
              "turn_index": 0,
              "turn_id": "inv_bloated_01",
              "events": [
                  {
                      "author": "user",
                      "content": {
                          "role": "user",
                          "parts": [
                              {
                                  "text": (
                                      "Please export the audit log and issue refund"
                                      " for ORD-8841."
                                  )
                              }
                          ],
                      },
                      "event_time": "2026-11-24T00:10:01.000000Z",
                  },
                  {
                      "author": "customer_ops_agent",
                      "content": {
                          "role": "model",
                          "parts": [
                              {
                                  "function_call": {
                                      "name": "export_audit_blob",
                                      "args": {"account_id": "acct-8841"},
                                  }
                              }
                          ],
                      },
                      "event_time": "2026-11-24T00:10:02.000000Z",
                  },
                  {
                      "author": "customer_ops_agent",
                      "content": {
                          "role": "user",
                          "parts": [
                              {
                                  "function_response": {
                                      "name": "export_audit_blob",
                                      "response": bloated_payload,
                                  }
                              }
                          ],
                      },
                      "event_time": "2026-11-24T00:10:03.000000Z",
                  },
                  {
                      "author": "customer_ops_agent",
                      "content": {
                          "role": "model",
                          "parts": [
                              {
                                  "text": (
                                      f"Refund check finished with terminal code"
                                      f" {tail_error}."
                                  )
                              }
                          ],
                      },
                      "event_time": "2026-11-24T00:10:04.000000Z",
                  },
              ],
          }
      ],
  }


class TrajectoryExtractorTest(unittest.TestCase):
  """Tests deterministic S1/S2/S3 trajectory extraction with zero LLM calls."""

  def test_imports_without_bigquery_or_adk(self) -> None:
    with mock.patch.dict(
        sys.modules,
        {
            "google.cloud.bigquery": None,
            "google.adk": None,
            "google.genai": None,
        },
    ):
      if "analytics.trajectory_extractor" in sys.modules:
        del sys.modules["analytics.trajectory_extractor"]
      mod = importlib.import_module("analytics.trajectory_extractor")
      self.assertTrue(hasattr(mod, "extract_s3_outcome_first"))

  def test_s1_gt_s2_gt_s3_and_preserves_trailing_error_under_2048_tokens(
      self,
  ) -> None:
    from analytics.trajectory_extractor import compute_ablation_metrics  # noqa: PLC0415
    from analytics.trajectory_extractor import estimate_tokens
    from analytics.trajectory_extractor import extract_s1_raw_trace
    from analytics.trajectory_extractor import extract_s2_subfields
    from analytics.trajectory_extractor import extract_s3_outcome_first

    trace = _make_bloated_trace(tail_error="CARD_EXPIRED")
    metrics = {
        "root_agent": "customer_ops_agent",
        "has_error": True,
        "tool_call_count": 2,
        "tool_error_count": 1,
        "has_tool_loop": False,
        "turn_latency_ms": 1850,
    }

    with mock.patch.object(
        socket.socket,
        "connect",
        side_effect=AssertionError("Network call forbidden in extractor"),
    ):
      s1 = extract_s1_raw_trace(trace)
      s2 = extract_s2_subfields(trace)
      s3 = extract_s3_outcome_first(trace, metrics=metrics, max_chars=4800)
      ablation = compute_ablation_metrics(trace, metrics=metrics)

    self.assertGreater(len(s1.encode("utf-8")), 25000)
    self.assertGreater(len(s1), len(s2))
    self.assertGreater(len(s2), len(s3))
    self.assertGreater(estimate_tokens(s1), 2048)
    self.assertGreater(estimate_tokens(s2), 2048)
    self.assertLess(estimate_tokens(s3), 1500)
    self.assertLessEqual(len(s3), 4800)

    self.assertTrue(s3.startswith("[OUTCOME:"))
    self.assertIn("root_agent=customer_ops_agent", s3)
    self.assertIn("has_error=True", s3)
    self.assertIn("CARD_EXPIRED", s3)
    self.assertNotIn("<div", s3)
    self.assertNotIn("tok_secret_value_12345", s3)
    self.assertNotIn("SyTestSampleKey", s3)
    self.assertIn("[REDACTED]", s3)

    self.assertTrue(ablation["s1_exceeds_2048"])
    self.assertTrue(ablation["s2_exceeds_2048"])
    self.assertFalse(ablation["s3_exceeds_2048"])
    self.assertEqual(ablation["s3_text"], s3)

  def test_retry_loop_and_strict_max_chars_preserves_outcome_and_tail(
      self,
  ) -> None:
    from analytics.trajectory_extractor import estimate_tokens  # noqa: PLC0415
    from analytics.trajectory_extractor import extract_s3_outcome_first

    events = [
        {
            "author": "user",
            "content": {
                "role": "user",
                "parts": [{"text": "Look up timeout policy for ORD-7710"}],
            },
        }
    ]
    for idx in range(5):
      events.append(
          {
              "author": "customer_ops_agent",
              "content": {
                  "role": "model",
                  "parts": [
                      {
                          "function_call": {
                              "name": "search_knowledge_base",
                              "args": {
                                  "query": "timeout ORD-7710 " + ("x" * 180)
                              },
                          }
                      }
                  ],
              },
          }
      )
      events.append(
          {
              "author": "customer_ops_agent",
              "content": {
                  "role": "user",
                  "parts": [
                      {
                          "function_response": {
                              "name": "search_knowledge_base",
                              "response": {
                                  "ok": False,
                                  "detail": f"retry_{idx} " + ("padding " * 80),
                                  "error_code": "DEADLINE_EXCEEDED",
                              },
                          }
                      }
                  ],
              },
          }
      )
    events.append(
        {
            "author": "customer_ops_agent",
            "content": {
                "role": "model",
                "parts": [
                    {
                        "text": (
                            "Knowledge base failed with DEADLINE_EXCEEDED after"
                            " retries."
                        )
                    }
                ],
            },
        }
    )
    trace = {
        "agents": {"customer_ops_agent": {"instruction": "x" * 6000}},
        "turns": [{"events": events}],
    }
    s3 = extract_s3_outcome_first(
        trace,
        metrics={
            "root_agent": "customer_ops_agent",
            "has_error": True,
            "tool_call_count": 5,
            "tool_error_count": 5,
            "has_tool_loop": True,
            "turn_latency_ms": 4820,
        },
        max_chars=1200,
    )
    self.assertLessEqual(len(s3), 1200)
    self.assertLess(estimate_tokens(s3), 2048)
    self.assertTrue(s3.startswith("[OUTCOME:"))
    self.assertIn("loop=True", s3)
    self.assertIn("DEADLINE_EXCEEDED", s3)

  def test_empty_trace_and_summarize_ablation(self) -> None:
    from analytics.trajectory_extractor import compute_ablation_metrics  # noqa: PLC0415
    from analytics.trajectory_extractor import estimate_tokens
    from analytics.trajectory_extractor import extract_s2_subfields
    from analytics.trajectory_extractor import extract_s3_outcome_first
    from analytics.trajectory_extractor import summarize_ablation

    self.assertEqual(estimate_tokens(""), 0)
    self.assertEqual(estimate_tokens("abcd"), 1)
    self.assertEqual(estimate_tokens("abcde"), 2)

    self.assertEqual(extract_s2_subfields({}), "")
    s3_empty = extract_s3_outcome_first({})
    self.assertTrue(s3_empty.startswith("[OUTCOME:"))

    row_a = {"ablation": compute_ablation_metrics(_make_bloated_trace())}
    summary = summarize_ablation([row_a])
    self.assertEqual(summary["turn_count"], 1)
    self.assertEqual(summary["ingest_llm_cost_usd"], 0.0)
    self.assertEqual(summary["s1_over_2048_count"], 1)
    self.assertEqual(summary["s3_over_2048_count"], 0)


class ToolsAndClassifierTest(unittest.TestCase):
  """Tests deterministic tools and sanitized_tool_classifier."""

  def test_deterministic_tools_behavior(self) -> None:
    from agent.tools import cancel_order  # noqa: PLC0415
    from agent.tools import check_policy_eligibility
    from agent.tools import export_audit_blob
    from agent.tools import issue_refund
    from agent.tools import lookup_order
    from agent.tools import search_knowledge_base

    healthy_order = lookup_order("ORD-9012")
    self.assertTrue(healthy_order["ok"])
    self.assertEqual(healthy_order["status"], "delivered")

    denied = lookup_order("VIP-0001")
    self.assertFalse(denied["ok"])
    self.assertEqual(denied["error_code"], "PERMISSION_DENIED")

    refund_ok = issue_refund("ORD-9012", "damaged item", 79.0)
    self.assertTrue(refund_ok["ok"])

    refund_err = issue_refund("ORD-8841", "customer request", 120.0)
    self.assertFalse(refund_err["ok"])
    self.assertEqual(refund_err["error_code"], "CARD_EXPIRED")

    kb_timeout = search_knowledge_base("timeout ORD-7710")
    self.assertFalse(kb_timeout["ok"])
    self.assertEqual(kb_timeout["error_code"], "DEADLINE_EXCEEDED")

    kb_ok = search_knowledge_base("warranty return window")
    self.assertTrue(kb_ok["ok"])
    self.assertTrue(kb_ok["articles"])

    policy = check_policy_eligibility("gold", 12)
    self.assertTrue(policy["ok"])
    self.assertTrue(policy["eligible"])

    cancel_err = cancel_order("ORD-6100", "changed mind")
    self.assertFalse(cancel_err["ok"])
    self.assertEqual(cancel_err["error_code"], "SHIPMENT_ALREADY_DISPATCHED")

    blob = export_audit_blob("acct-992", include_html_snapshot=True)
    self.assertTrue(blob["ok"])
    self.assertEqual(blob["warning_code"], "AUDIT_SYNC_WARNING")
    self.assertEqual(blob["tail_code"], "CHECKSUM_MISMATCH")
    self.assertNotIn("error_code", blob)
    self.assertGreater(len(json.dumps(blob)), 10240)
    self.assertIn("<div", blob["html_snapshot"])

  def test_sanitized_tool_classifier_scrubs_secrets_preserves_error_codes(
      self,
  ) -> None:
    from agent.tools import sanitized_tool_classifier  # noqa: PLC0415

    fake_tool = SimpleNamespace(name="issue_refund")
    fake_ctx = SimpleNamespace()
    ok_verdict = sanitized_tool_classifier(
        tool=fake_tool,
        tool_args={"order_id": "ORD-9012"},
        tool_context=fake_ctx,
        result={"ok": True, "refund_id": "REF-3310"},
    )
    self.assertIsNone(ok_verdict)

    oauth = "ya29." + "a0DummyToken12345"
    api_key = "AIza" + "SyDummyKey67890123456"
    verdict = sanitized_tool_classifier(
        tool=fake_tool,
        tool_args={"order_id": "ORD-8841"},
        tool_context=fake_ctx,
        result={
            "ok": False,
            "error_code": "CARD_EXPIRED",
            "message": (
                f"Bearer supersecret99 {oauth} {api_key} "
                "api_key=abc12345 password=hunter2 CARD_EXPIRED"
            ),
        },
    )
    self.assertIsNotNone(verdict)
    self.assertEqual(verdict.status, "ERROR")
    self.assertIn("CARD_EXPIRED", verdict.error_message)
    self.assertNotIn("supersecret99", verdict.error_message)
    self.assertNotIn("a0DummyToken12345", verdict.error_message)
    self.assertNotIn("SyDummyKey67890123456", verdict.error_message)
    self.assertNotIn("abc12345", verdict.error_message)
    self.assertNotIn("hunter2", verdict.error_message)
    self.assertIn("[REDACTED]", verdict.error_message)

  def test_key_aware_redaction_and_basic_auth(self) -> None:
    from agent.tools import sanitized_tool_classifier  # noqa: PLC0415
    from analytics.trajectory_extractor import extract_s3_outcome_first  # noqa: PLC0415

    trace = {
        "turns": [
            {
                "turn_index": 0,
                "events": [
                    {
                        "author": "customer_ops_agent",
                        "content": {
                            "role": "model",
                            "parts": [
                                {
                                    "function_call": {
                                        "name": "auth_probe",
                                        "args": {
                                            "password": "plain_password_val",
                                            "nested": {
                                                "client_secret": (
                                                    "plain_client_secret"
                                                ),
                                                "auth_token": "plain_auth_tok",
                                                "secret_key": "plain_sec_key",
                                                "apikey": "plain_api_key_val",
                                                "header": (
                                                    "Authorization: Basic"
                                                    " dXNlcjpwYXNz"
                                                ),
                                            },
                                            "total_tokens": 1234,
                                        },
                                    }
                                }
                            ],
                        },
                    }
                ],
            }
        ]
    }
    s3 = extract_s3_outcome_first(trace)
    for leaked in (
        "plain_password_val",
        "plain_client_secret",
        "plain_auth_tok",
        "plain_sec_key",
        "plain_api_key_val",
        "dXNlcjpwYXNz",
    ):
      self.assertNotIn(leaked, s3)
    self.assertIn("total_tokens=1234", s3)

    verdict = sanitized_tool_classifier(
        tool=SimpleNamespace(name="auth_probe"),
        tool_args={},
        tool_context=SimpleNamespace(),
        result={
            "ok": False,
            "error_code": "AUTH_FAILED",
            "message": (
                "Authorization: Basic QWxhZGRpbjpvcGVu apikey=raw_key_99 "
                "auth_token=tok_xyz secret_key=sec_123"
            ),
        },
    )
    self.assertIsNotNone(verdict)
    self.assertIn("AUTH_FAILED", verdict.error_message)
    for leaked in ("QWxhZGRpbjpvcGVu", "raw_key_99", "tok_xyz", "sec_123"):
      self.assertNotIn(leaked, verdict.error_message)

  def test_nested_container_secrets_expanded_keys_and_freetext_redaction(
      self,
  ) -> None:
    from analytics.trajectory_extractor import extract_s3_outcome_first  # noqa: PLC0415
    from analytics.trajectory_extractor import is_sensitive_key  # noqa: PLC0415
    from analytics.trajectory_extractor import scrub_sensitive_data  # noqa: PLC0415
    from analytics.trajectory_extractor import scrub_sensitive_text  # noqa: PLC0415

    # Expanded sensitive keys must match without false positives on author/authority.
    for sens in (
        "passphrase",
        "cookie",
        "set_cookie",
        "set-cookie",
        "signing_key",
        "encryption_key",
        "master_key",
        "connection_string",
        "auth",
        "user_auth",
        "auth_data",
        "credentials",
    ):
      self.assertTrue(is_sensitive_key(sens), f"Expected sensitive key: {sens}")
    for benign in ("author", "authority", "authorized_count", "total_tokens"):
      self.assertFalse(
          is_sensitive_key(benign), f"False positive sensitive key: {benign}"
      )

    # Nested dict/list under sensitive keys must be collapsed to [REDACTED].
    nested = scrub_sensitive_data(
        {
            "credentials": {"value": "nested_cred_secret_1"},
            "password": {"nested": "nested_pw_secret_2"},
            "auth": ["nested_auth_tok_3"],
            "passphrase": "plain_passphrase_4",
            "signing_key": "plain_signing_5",
            "encryption_key": "plain_enc_6",
            "master_key": "plain_master_7",
            "connection_string": "postgres://u:p@db/prod",
            "set_cookie": "sid=cookie_secret_8",
            "author": "customer_ops_agent",
            "authority": "payments.example.com",
            "empty_creds": {},
        }
    )
    self.assertEqual(nested["credentials"], "[REDACTED]")
    self.assertEqual(nested["password"], "[REDACTED]")
    self.assertEqual(nested["auth"], "[REDACTED]")
    self.assertEqual(nested["passphrase"], "[REDACTED]")
    self.assertEqual(nested["signing_key"], "[REDACTED]")
    self.assertEqual(nested["encryption_key"], "[REDACTED]")
    self.assertEqual(nested["master_key"], "[REDACTED]")
    self.assertEqual(nested["connection_string"], "[REDACTED]")
    self.assertEqual(nested["set_cookie"], "[REDACTED]")
    self.assertEqual(nested["author"], "customer_ops_agent")
    self.assertEqual(nested["authority"], "payments.example.com")

    # Free-text secret patterns must redact without touching benign phrases.
    scrubbed_text = scrub_sensitive_text(
        "the password is super_pw_11; passphrase is my_pp_22; "
        "api key is my_ak_33; credentials are user:pass_44; "
        "cookie is sess_ck_55; connection string is postgres://u:p@h/db; "
        "error is CARD_EXPIRED; status is delivered"
    )
    for secret in (
        "super_pw_11",
        "my_pp_22",
        "my_ak_33",
        "user:pass_44",
        "sess_ck_55",
        "postgres://u:p@h/db",
    ):
      self.assertNotIn(secret, scrubbed_text)
    self.assertIn("error is CARD_EXPIRED", scrubbed_text)
    self.assertIn("status is delivered", scrubbed_text)

    # End-to-end check inside extract_s3_outcome_first.
    s3 = extract_s3_outcome_first(
        {
            "turns": [
                {
                    "events": [
                        {
                            "author": "customer_ops_agent",
                            "content": {
                                "role": "model",
                                "parts": [
                                    {
                                        "function_call": {
                                            "name": "vault_call",
                                            "args": {
                                                "credentials": {
                                                    "value": "s3_nested_secret"
                                                },
                                                "author": "customer_ops_agent",
                                            },
                                        }
                                    },
                                    {
                                        "text": (
                                            "Note: the password is"
                                            " s3_freetext_pw and error is"
                                            " CARD_EXPIRED."
                                        )
                                    },
                                ],
                            },
                        }
                    ]
                }
            ]
        }
    )
    self.assertNotIn("s3_nested_secret", s3)
    self.assertNotIn("s3_freetext_pw", s3)
    self.assertIn("CARD_EXPIRED", s3)
    self.assertIn("customer_ops_agent", s3)


class PromptsScenariosAndRunnerTest(unittest.TestCase):
  """Tests scenarios, mock 17-col/22-col schemas, and run_live_demo CLI."""

  def test_system_instruction_and_eight_scenarios(self) -> None:
    from agent.prompts_and_scenarios import build_mock_live_artifacts  # noqa: PLC0415
    from agent.prompts_and_scenarios import DEMO_SCENARIOS
    from agent.prompts_and_scenarios import SYSTEM_INSTRUCTION

    inst_bytes = len(SYSTEM_INSTRUCTION.encode("utf-8"))
    self.assertGreaterEqual(inst_bytes, 4800)
    self.assertLessEqual(inst_bytes, 9000)

    self.assertEqual(len(DEMO_SCENARIOS), 8)
    scenario_ids = [s.scenario_id for s in DEMO_SCENARIOS]
    self.assertEqual(
        scenario_ids,
        [
            "scn_01_healthy_refund",
            "scn_02_exact_card_expired",
            "scn_03_silent_contradiction_refund",
            "scn_04_deadline_retry_loop",
            "scn_05_bloated_audit_blob",
            "scn_06_multi_tool_policy_check",
            "scn_07_permission_denied_lookup",
            "scn_08_silent_contradiction_cancellation",
        ],
    )

    events, eval_turns = build_mock_live_artifacts(
        user_id="demo_user",
        model_name="gemini-4-hydrogen",
    )
    self.assertEqual(len(eval_turns), 8)
    self.assertGreaterEqual(len(events), 32)

    expected_event_cols = {
        "timestamp",
        "event_id",
        "event_type",
        "agent",
        "session_id",
        "invocation_id",
        "user_id",
        "trace_id",
        "span_id",
        "parent_span_id",
        "content",
        "content_parts",
        "attributes",
        "latency_ms",
        "status",
        "error_message",
        "is_truncated",
    }
    self.assertEqual(set(events[0].keys()), expected_event_cols)

    for turn in eval_turns:
      # Align strictly with HEAD v_eval_turn (no status or root_agent_name).
      self.assertNotIn("status", turn)
      self.assertNotIn("root_agent_name", turn)
      self.assertIn("has_error", turn)
      self.assertIn("root_agent", turn)
      self.assertIn("has_tool_loop", turn)
      self.assertIn("max_consecutive_identical_tool_calls", turn)
      self.assertIn("distinct_tools_used", turn)
      self.assertIn("error_summary", turn)
      self.assertIn("raw_trace_bytes", turn)
      self.assertIn("trace", turn)
      self.assertIn("agents", turn["trace"])
      self.assertIn("turns", turn["trace"])

    by_id = {t["session_id"]: t for t in eval_turns}
    self.assertTrue(by_id["sess_04_deadline_retry_loop"]["has_tool_loop"])
    self.assertGreaterEqual(
        by_id["sess_04_deadline_retry_loop"][
            "max_consecutive_identical_tool_calls"
        ],
        3,
    )
    self.assertEqual(
        by_id["sess_02_exact_card_expired"]["error_summary"], "CARD_EXPIRED"
    )
    self.assertEqual(
        by_id["sess_08_silent_contradiction_cancellation"]["error_summary"],
        "SHIPMENT_ALREADY_DISPATCHED",
    )
    scn5_turn = by_id["sess_05_bloated_audit_blob"]
    self.assertFalse(scn5_turn["has_error"])
    self.assertEqual(scn5_turn["tool_error_count"], 0)
    self.assertIsNone(scn5_turn["error_summary"])
    self.assertGreater(scn5_turn["ablation"]["s2_tokens_est"], 2048)
    self.assertLess(scn5_turn["ablation"]["s3_tokens_est"], 1000)
    self.assertIn("AUDIT_SYNC_WARNING", scn5_turn["ablation"]["s3_text"])
    self.assertIn("CHECKSUM_MISMATCH", scn5_turn["ablation"]["s3_text"])
    scn5_events = [
        ev
        for ev in events
        if ev["session_id"] == "sess_05_bloated_audit_blob"
        and ev["event_type"] in ("TOOL_COMPLETED", "TOOL_ERROR")
    ]
    self.assertEqual(len(scn5_events), 1)
    self.assertEqual(scn5_events[0]["event_type"], "TOOL_COMPLETED")

  def test_run_live_demo_mock_live_cli_emits_contract_json(self) -> None:
    with tempfile.TemporaryDirectory() as tmp:
      out_json = Path(tmp) / "live_demo_results.json"
      env = os.environ.copy()
      env["PYTHONDONTWRITEBYTECODE"] = "1"
      env["PYTHONSAFEPATH"] = "1"
      existing_pp = env.get("PYTHONPATH", "")
      env["PYTHONPATH"] = (
          f"{DEMO_DIR}{os.pathsep}{existing_pp}"
          if existing_pp
          else str(DEMO_DIR)
      )
      proc = subprocess.run(
          [
              sys.executable,
              "-m",
              "agent.run_live_demo",
              "--project-id",
              "demo-project",
              "--dataset-id",
              "combined_cuj_trace_search_demo",
              "--table-id",
              "agent_events",
              "--location",
              "US",
              "--agent-location",
              "global",
              "--model",
              "gemini-4-hydrogen",
              "--mock-live",
              "--output-json",
              str(out_json),
          ],
          cwd=str(DEMO_DIR),
          env=env,
          capture_output=True,
          text=True,
          check=False,
      )
      self.assertEqual(
          proc.returncode,
          0,
          f"CLI failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}",
      )
      self.assertTrue(out_json.exists())
      payload = json.loads(out_json.read_text(encoding="utf-8"))
      self.assertTrue(payload["mock_live"])
      self.assertEqual(payload["project_id"], "demo-project")
      self.assertEqual(payload["dataset_id"], "combined_cuj_trace_search_demo")
      self.assertEqual(payload["table_id"], "agent_events")
      self.assertEqual(len(payload["session_ids"]), 8)
      self.assertEqual(len(payload["eval_turns"]), 8)
      summary = payload["ablation_summary"]
      self.assertEqual(summary["turn_count"], 8)
      self.assertEqual(summary["ingest_llm_cost_usd"], 0.0)
      self.assertGreater(summary["s1_avg_tokens"], summary["s2_avg_tokens"])
      self.assertGreater(summary["s2_avg_tokens"], summary["s3_avg_tokens"])
      self.assertEqual(summary["s3_over_2048_count"], 0)
      for turn in payload["eval_turns"]:
        abl = turn["ablation"]
        self.assertGreater(abl["s1_bytes"], abl["s2_bytes"])
        self.assertGreater(abl["s2_bytes"], abl["s3_bytes"])
        self.assertLess(abl["s3_tokens_est"], 1500)
        self.assertFalse(abl["s3_exceeds_2048"])
        agent_lines = re.findall(r"(?m)^AGENT:\s*(.+)$", abl["s3_text"])
        self.assertEqual(len(agent_lines), 1)
        if "silent_contradiction" in turn["session_id"]:
          self.assertTrue(turn["has_error"])
          self.assertNotRegex(
              agent_lines[0].lower(),
              r"\b(error|sorry|failed|unable|expired|cannot|declined)\b",
          )
      scn3 = next(
          t
          for t in payload["eval_turns"]
          if t["session_id"] == "sess_03_silent_contradiction_refund"
      )
      self.assertIn("refund has been processed", scn3["ablation"]["s3_text"])
      scn5 = next(
          t
          for t in payload["eval_turns"]
          if t["session_id"] == "sess_05_bloated_audit_blob"
      )
      self.assertFalse(scn5["has_error"])
      self.assertEqual(scn5["tool_error_count"], 0)
      self.assertIsNone(scn5["error_summary"])
      self.assertGreater(scn5["ablation"]["s2_tokens_est"], 2048)
      self.assertLess(scn5["ablation"]["s3_tokens_est"], 1000)
      self.assertIn("AUDIT_SYNC_WARNING", scn5["ablation"]["s3_text"])
      self.assertIn("CHECKSUM_MISMATCH", scn5["ablation"]["s3_text"])

  def test_live_plugin_lifecycle_uses_flush_and_shutdown(self) -> None:
    from agent import run_live_demo  # noqa: PLC0415

    fake_plugin = mock.MagicMock()
    fake_plugin.flush = mock.AsyncMock()
    fake_plugin.shutdown = mock.AsyncMock()

    async def _run_test() -> None:
      await run_live_demo._shutdown_bqaa_plugin(fake_plugin)

    asyncio.run(_run_test())
    fake_plugin.flush.assert_awaited_once()
    fake_plugin.shutdown.assert_awaited_once_with(
        close_background_transport=True
    )

  def test_run_demo_succeeds_inside_running_event_loop_and_async_entry(
      self,
  ) -> None:
    from agent import run_live_demo  # noqa: PLC0415
    from agent.prompts_and_scenarios import build_mock_live_artifacts  # noqa: PLC0415

    _, sample_turns = build_mock_live_artifacts(
        user_id="demo_user", model_name="gemini-4-hydrogen"
    )
    sample_turn = sample_turns[:1]
    seen_threads: list[int] = []

    async def _fake_live(
        **kwargs: object,
    ) -> tuple[list[str], list[dict], list[dict]]:
      del kwargs
      seen_threads.append(threading.get_ident())
      await asyncio.sleep(0)
      return (
          [sample_turn[0]["session_id"]],
          sample_turn,
          [
              {
                  "scenario_id": "scn_01_healthy_refund",
                  "session_id": sample_turn[0]["session_id"],
                  "tools_called": ["lookup_order"],
                  "response": "Refund completed.",
              }
          ],
      )

    caller_thread = threading.get_ident()
    with mock.patch.object(
        run_live_demo, "_run_live_scenarios", side_effect=_fake_live
    ):

      async def _inside_jupyter_loop() -> tuple[dict, dict]:
        sync_payload = run_live_demo.run_demo(
            project_id="demo-project",
            dataset_id="combined_cuj_trace_search_demo",
            mock_live=False,
        )
        async_payload = await run_live_demo.run_demo_async(
            project_id="demo-project",
            dataset_id="combined_cuj_trace_search_demo",
            mock_live=False,
        )
        return sync_payload, async_payload

      sync_res, async_res = asyncio.run(_inside_jupyter_loop())

      self.assertFalse(sync_res["mock_live"])
      self.assertEqual(len(sync_res["session_ids"]), 1)
      self.assertFalse(async_res["mock_live"])
      self.assertEqual(len(async_res["session_ids"]), 1)
      # Sync run_demo inside an active event loop delegates to a worker thread;
      # run_demo_async awaits directly on the caller event loop thread.
      self.assertEqual(len(seen_threads), 2)
      self.assertNotEqual(seen_threads[0], caller_thread)
      self.assertEqual(seen_threads[1], caller_thread)

      # Also verify normal CLI path (no active running event loop).
      cli_res = run_live_demo.run_demo(
          project_id="demo-project",
          dataset_id="combined_cuj_trace_search_demo",
          mock_live=False,
      )
      self.assertEqual(seen_threads[2], caller_thread)
      self.assertEqual(cli_res["ablation_summary"]["turn_count"], 1)

  def test_live_demo_disables_create_views_enforces_ownership_and_scrubs_errors(
      self,
  ) -> None:
    from agent import run_live_demo  # noqa: PLC0415
    import google.cloud.bigquery as bq_mod  # noqa: PLC0415

    # 1. BigQueryLoggerConfig in _run_live_scenarios must set create_views=False.
    captured_configs: list[dict] = []

    class _FakeLoggerConfig:

      def __init__(self, **kwargs):
        captured_configs.append(kwargs)

    class _FakePlugin:

      def __init__(self, **kwargs):
        del kwargs

      async def flush(self):
        return None

      async def shutdown(self, **kwargs):
        del kwargs
        return None

    fake_bq_client = mock.MagicMock()
    fake_bq_client.get_table.return_value = SimpleNamespace(
        labels={"bqaa_demo": "combined_cuj_trace_search"}
    )
    fake_adk_pkg = types.ModuleType("google.adk")
    fake_adk_pkg.__path__ = []  # type: ignore[attr-defined]
    fake_plugins_pkg = types.ModuleType("google.adk.plugins")
    fake_plugins_pkg.__path__ = []  # type: ignore[attr-defined]
    fake_plugin_mod = SimpleNamespace(
        BigQueryAgentAnalyticsPlugin=_FakePlugin,
        BigQueryLoggerConfig=_FakeLoggerConfig,
    )
    fake_adk_modules = {
        "google.adk": fake_adk_pkg,
        "google.adk.plugins": fake_plugins_pkg,
        "google.adk.plugins.bigquery_agent_analytics_plugin": fake_plugin_mod,
    }

    async def _fake_single(**kwargs):
      return {
          "scenario_id": kwargs["scenario"].scenario_id,
          "session_id": kwargs["session_id"],
          "tools_called": [],
          "response": "ok",
      }

    with (
        mock.patch.dict(sys.modules, fake_adk_modules),
        mock.patch.object(bq_mod, "Client", return_value=fake_bq_client),
        mock.patch.object(
            run_live_demo, "_run_single_live_scenario", side_effect=_fake_single
        ),
        mock.patch.object(
            run_live_demo,
            "_materialize_and_fetch_live_eval_turns",
            return_value=[],
        ),
    ):
      asyncio.run(
          run_live_demo._run_live_scenarios(
              project_id="demo-project",
              dataset_id="demo_ds",
              table_id="agent_events",
              eval_turns_table="eval_turns",
              location="US",
              agent_location="global",
              model_name="gemini-4-hydrogen",
              user_id="demo_user",
              timeout_sec=10,
          )
      )
    self.assertEqual(len(captured_configs), 1)
    self.assertFalse(captured_configs[0]["create_views"])

    # 2. _materialize_and_fetch_live_eval_turns must STILL enforce ownership
    #    under ImportError of analytics.index_and_search (never skip guard!).
    unowned_client = mock.MagicMock()
    unowned_client.get_table.return_value = SimpleNamespace(labels=None)
    with (
        mock.patch.dict(sys.modules, {"analytics.index_and_search": None}),
        mock.patch.object(bq_mod, "Client", return_value=unowned_client),
    ):
      with self.assertRaisesRegex(RuntimeError, "missing demo ownership label"):
        run_live_demo._materialize_and_fetch_live_eval_turns(
            project_id="demo-project",
            dataset_id="demo_ds",
            table_id="agent_events",
            eval_turns_table="eval_turns",
            location="US",
            session_ids=["s1"],
        )
    unowned_client.query.assert_not_called()

    # 3. Non-404 get_table errors must re-raise rather than look absent.
    forbidden_client = mock.MagicMock()
    forbidden_client.get_table.side_effect = PermissionError("403 forbidden")
    with (
        mock.patch.dict(sys.modules, {"analytics.index_and_search": None}),
        mock.patch.object(bq_mod, "Client", return_value=forbidden_client),
    ):
      with self.assertRaises(PermissionError):
        run_live_demo._materialize_and_fetch_live_eval_turns(
            project_id="demo-project",
            dataset_id="demo_ds",
            table_id="agent_events",
            eval_turns_table="eval_turns",
            location="US",
            session_ids=["s1"],
        )
    forbidden_client.query.assert_not_called()

    # 4. Confirmed 404 NotFound passes guard and scrubs error_summary on fetch.
    class _NotFound(Exception):
      code = 404

    ok_client = mock.MagicMock()
    ok_client.get_table.side_effect = _NotFound("table not found")

    def _fake_fetch_query(sql_text, job_config=None):
      del job_config
      job = mock.MagicMock()
      if "SELECT * FROM" in sql_text:
        job.result.return_value = [
            {
                "session_id": "s1",
                "invocation_id": "inv_1",
                "root_agent": "customer_ops_agent",
                "has_error": True,
                "error_summary": "the password is leaked_live_pw CARD_EXPIRED",
                "trace": {},
            }
        ]
      else:
        job.result.return_value = []
      return job

    ok_client.query.side_effect = _fake_fetch_query
    with (
        mock.patch.dict(sys.modules, {"analytics.index_and_search": None}),
        mock.patch.object(bq_mod, "Client", return_value=ok_client),
    ):
      rows = run_live_demo._materialize_and_fetch_live_eval_turns(
          project_id="demo-project",
          dataset_id="demo_ds",
          table_id="agent_events",
          eval_turns_table="eval_turns",
          location="US",
          session_ids=["s1"],
      )
    self.assertEqual(len(rows), 1)
    self.assertNotIn("leaked_live_pw", rows[0]["error_summary"])
    self.assertIn("CARD_EXPIRED", rows[0]["error_summary"])
    # Confirmed 404 must emit CREATE TABLE without OR REPLACE (closes TOCTOU).
    ddl_sql = ok_client.query.call_args_list[0].args[0]
    self.assertIn("CREATE TABLE", ddl_sql)
    self.assertNotIn("CREATE OR REPLACE TABLE", ddl_sql)

  def test_expanded_redaction_embedded_json_and_diagnostic_preservation(
      self,
  ) -> None:
    from analytics.trajectory_extractor import scrub_sensitive_data  # noqa: PLC0415
    from analytics.trajectory_extractor import scrub_sensitive_text  # noqa: PLC0415

    # 1. Expanded sensitive key spellings (camelCase, plurals, DSN/JWT/OAuth).
    payload = {
        "secrets": {"a": "sec_plural_val"},
        "secretAccessKey": "camel_sak_val",
        "github_token": "gh_tok_val",
        "jwt": "jwt_raw_val",
        "oauth": "oauth_raw_val",
        "dsn": "postgres://u:dsn_pw@host/db",
        "database_url": "mysql://u:dburl_pw@host/db",
        "aws_secret_access_key": "aws_sak_val",
        "csrf_token": "csrf_raw_val",
        "pwd": "pwd_raw_val",
        # Safe diagnostic metadata keys must NOT be over-redacted.
        "auth_error": "PERMISSION_DENIED",
        "auth_status": "forbidden",
        "credential_type": "oauth_service_account",
        "author": "alice",
    }
    scrubbed = scrub_sensitive_data(payload)
    for leak in (
        "sec_plural_val",
        "camel_sak_val",
        "gh_tok_val",
        "jwt_raw_val",
        "oauth_raw_val",
        "dsn_pw",
        "dburl_pw",
        "aws_sak_val",
        "csrf_raw_val",
        "pwd_raw_val",
    ):
      self.assertNotIn(leak, json.dumps(scrubbed))
    self.assertEqual(scrubbed["auth_error"], "PERMISSION_DENIED")
    self.assertEqual(scrubbed["auth_status"], "forbidden")
    self.assertEqual(scrubbed["credential_type"], "oauth_service_account")
    self.assertEqual(scrubbed["author"], "alice")

    # 2. Unified free-text forms + embedded JSON containers inside strings.
    raw_text = (
        "the api_key is free_api_1; client_secret is free_cs_2; "
        "the password is: free_pw_3; pwd is free_pwd_4; "
        "passwords are free_pws_5; aws_secret_access_key=free_aws_6; "
        "csrf_token=free_csrf_7; "
        'payload={"password": {"nested": "emb_json_pw"}, '
        '"credentials": ["emb_json_c1", "emb_json_c2"], '
        '"auth_error": "CARD_EXPIRED"}'
    )
    cleaned = scrub_sensitive_text(raw_text)
    for leak in (
        "free_api_1",
        "free_cs_2",
        "free_pw_3",
        "free_pwd_4",
        "free_pws_5",
        "free_aws_6",
        "free_csrf_7",
        "emb_json_pw",
        "emb_json_c1",
        "emb_json_c2",
    ):
      self.assertNotIn(leak, cleaned)
    self.assertIn("CARD_EXPIRED", cleaned)

    # 3. N1: Env-style / prefixed free-text keys must be redacted without
    #    over-matching benign identifiers.
    env_text = (
        "DB_PASSWORD=hunter2 db_password: db_pw_2 {'db_password': 'dict_pw_3'} "
        "x_api_key=x_key_4 stripe_api_key=sk_live_5 "
        "GOOGLE_CLIENT_SECRET=cs_99 REDIS_AUTH_TOKEN=rt_88 "
        "ANTHROPIC_API_KEY=ak_77 passwordless=true DB_PASSWORD_FILE=/etc/pw "
        "total_tokens=42 prompt_token=18 status is delivered CARD_EXPIRED"
    )
    cleaned_env = scrub_sensitive_text(env_text)
    for leak in (
        "hunter2",
        "db_pw_2",
        "dict_pw_3",
        "x_key_4",
        "sk_live_5",
        "cs_99",
        "rt_88",
        "ak_77",
    ):
      self.assertNotIn(leak, cleaned_env)
    for keep in (
        "passwordless=true",
        "DB_PASSWORD_FILE=/etc/pw",
        "total_tokens=42",
        "prompt_token=18",
        "status is delivered",
        "CARD_EXPIRED",
    ):
      self.assertIn(keep, cleaned_env)

    # 4. N2: Sensitive *_code / *_auth / auth_response_uri keys redacted while
    #    benign diagnostic keys stay preserved.
    n2_payload = {
        "authorization_code": "auth_code_val_1",
        "secret_code": "sec_code_val_2",
        "auth_code": "ac_val_3",
        "auth_response_uri": "https://app.example/cb?code=uri_secret_4",
        "basic_auth": "basic_secret_5",
        "proxy_auth": "proxy_secret_6",
        "http_auth": "http_secret_7",
        "auth_error": "PERMISSION_DENIED",
        "auth_status": "forbidden",
        "credential_type": "oauth_service_account",
        "author": "alice",
        "authority": "idp.example.com",
    }
    n2_scrubbed = scrub_sensitive_data(n2_payload)
    for leak in (
        "auth_code_val_1",
        "sec_code_val_2",
        "ac_val_3",
        "uri_secret_4",
        "basic_secret_5",
        "proxy_secret_6",
        "http_secret_7",
    ):
      self.assertNotIn(leak, json.dumps(n2_scrubbed))
    self.assertEqual(n2_scrubbed["auth_error"], "PERMISSION_DENIED")
    self.assertEqual(n2_scrubbed["auth_status"], "forbidden")
    self.assertEqual(n2_scrubbed["credential_type"], "oauth_service_account")
    self.assertEqual(n2_scrubbed["author"], "alice")
    self.assertEqual(n2_scrubbed["authority"], "idp.example.com")

    # 5. N3: Deep/pathological unclosed bracket runs complete quickly without
    #    RecursionError and still redact trailing JSON secrets.
    bracket_bomb = ("[" * 20_000) + ' {"password": {"nested": "tail_pw_ok"}}'
    scrubbed_bomb = scrub_sensitive_text(bracket_bomb)
    self.assertNotIn("tail_pw_ok", scrubbed_bomb)
    deep_obj = ('{"a": ' * 500) + ' {"credentials": ["deep_tail_secret"]}'
    scrubbed_deep = scrub_sensitive_text(deep_obj)
    self.assertNotIn("deep_tail_secret", scrubbed_deep)

    # 6. N1b: Base-parity camelCase / concatenated free-text secret forms +
    #    non-overmatch keep-list.
    base_parity_cases = (
        ("PGPASSWORD=pg1", ("pg1",)),
        ("newPassword=np1", ("np1",)),
        ("?oldPassword=a1&newPassword=b1", ("a1", "b1")),
        ("{userPassword: 'up1'}", ("up1",)),
        ("googleApiKey=gk1", ("gk1",)),
        ("stripeApiKey=sk1", ("sk1",)),
        ("myClientSecret=mc1", ("mc1",)),
        ("fbAccessToken=fb1", ("fb1",)),
        ("dbpassword: dp1", ("dp1",)),
    )
    for sample, secrets_to_hide in base_parity_cases:
      with self.subTest(sample=sample):
        out = scrub_sensitive_text(sample)
        for secret in secrets_to_hide:
          self.assertNotIn(secret, out)
        self.assertIn("[REDACTED]", out)

    keep_cases = (
        "passwordless=true",
        "DB_PASSWORD_FILE=/etc/pw",
        "total_tokens=42",
        "prompt_token=18",
        "status is delivered",
        "secretary is Bob",
        "cookies=3",
        "credential_type=oauth",
        "NOPASSWD_FILE=/etc/sudoers",
        "CARD_EXPIRED",
    )
    for keep_sample in keep_cases:
      with self.subTest(keep_sample=keep_sample):
        self.assertEqual(scrub_sensitive_text(keep_sample), keep_sample)

  def test_run_live_scenarios_upfront_guard_503_fail_closed_and_none_labels(
      self,
  ) -> None:
    from agent import run_live_demo  # noqa: PLC0415
    import google.cloud.bigquery as bq_mod  # noqa: PLC0415

    class _FakeLoggerConfig:

      def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.clustering_fields = ["event_type", "agent", "user_id"]

    class _FakePlugin:

      def __init__(self, **kwargs):
        del kwargs

      async def flush(self):
        return None

      async def shutdown(self, **kwargs):
        del kwargs
        return None

    fake_adk_pkg = types.ModuleType("google.adk")
    fake_adk_pkg.__path__ = []  # type: ignore[attr-defined]
    fake_plugins_pkg = types.ModuleType("google.adk.plugins")
    fake_plugins_pkg.__path__ = []  # type: ignore[attr-defined]
    fake_plugin_mod = SimpleNamespace(
        BigQueryAgentAnalyticsPlugin=_FakePlugin,
        BigQueryLoggerConfig=_FakeLoggerConfig,
        _get_events_schema=lambda: [
            bq_mod.SchemaField("timestamp", "TIMESTAMP"),
            bq_mod.SchemaField("event_type", "STRING"),
        ],
    )
    fake_adk_modules = {
        "google.adk": fake_adk_pkg,
        "google.adk.plugins": fake_plugins_pkg,
        "google.adk.plugins.bigquery_agent_analytics_plugin": fake_plugin_mod,
    }

    # 1. Unowned eval_turns_table fails upfront BEFORE loading ADK or running LLMs.
    unowned_eval_client = mock.MagicMock()
    unowned_eval_client.get_table.return_value = SimpleNamespace(
        labels={"owner": "other_team"}
    )
    single_mock = mock.AsyncMock()
    with (
        mock.patch.dict(sys.modules, fake_adk_modules),
        mock.patch.object(bq_mod, "Client", return_value=unowned_eval_client),
        mock.patch.object(
            run_live_demo, "_run_single_live_scenario", single_mock
        ),
    ):
      with self.assertRaisesRegex(RuntimeError, "missing demo ownership label"):
        asyncio.run(
            run_live_demo._run_live_scenarios(
                project_id="demo-project",
                dataset_id="demo_ds",
                table_id="agent_events",
                eval_turns_table="eval_turns",
                location="US",
                agent_location="global",
                model_name="gemini-4-hydrogen",
                user_id="demo_user",
                timeout_sec=10,
            )
        )
    single_mock.assert_not_awaited()

    # 2. events_fqn probe returning 503 fails closed without calling update_table.
    class _NotFound(Exception):
      code = 404

    class _ServiceUnavailable(Exception):
      code = 503

    err503_client = mock.MagicMock()

    def _get_table_503(fqn: str):
      if fqn.endswith(".eval_turns"):
        raise _NotFound("absent")
      raise _ServiceUnavailable("backend 503")

    err503_client.get_table.side_effect = _get_table_503
    with (
        mock.patch.dict(sys.modules, fake_adk_modules),
        mock.patch.object(bq_mod, "Client", return_value=err503_client),
        mock.patch.object(
            run_live_demo, "_run_single_live_scenario", single_mock
        ),
    ):
      with self.assertRaises(_ServiceUnavailable):
        asyncio.run(
            run_live_demo._run_live_scenarios(
                project_id="demo-project",
                dataset_id="demo_ds",
                table_id="agent_events",
                eval_turns_table="eval_turns",
                location="US",
                agent_location="global",
                model_name="gemini-4-hydrogen",
                user_id="demo_user",
                timeout_sec=10,
            )
        )
    err503_client.update_table.assert_not_called()
    single_mock.assert_not_awaited()

    # 3. Confirmed 404 on events_fqn creates table atomically with label
    #    BEFORE running scenarios, and never calls update_table post-run.
    new_tbl_client = mock.MagicMock()
    new_tbl_client.get_table.side_effect = _NotFound("absent")

    async def _fake_single(**kwargs):
      return {
          "scenario_id": kwargs["scenario"].scenario_id,
          "session_id": kwargs["session_id"],
          "tools_called": [],
          "response": "ok",
      }

    with (
        mock.patch.dict(sys.modules, fake_adk_modules),
        mock.patch.object(bq_mod, "Client", return_value=new_tbl_client),
        mock.patch.object(
            run_live_demo, "_run_single_live_scenario", side_effect=_fake_single
        ),
        mock.patch.object(
            run_live_demo,
            "_materialize_and_fetch_live_eval_turns",
            return_value=[],
        ),
    ):
      asyncio.run(
          run_live_demo._run_live_scenarios(
              project_id="demo-project",
              dataset_id="demo_ds",
              table_id="agent_events",
              eval_turns_table="eval_turns",
              location="US",
              agent_location="global",
              model_name="gemini-4-hydrogen",
              user_id="demo_user",
              timeout_sec=10,
          )
      )
    new_tbl_client.update_table.assert_not_called()
    new_tbl_client.create_table.assert_called_once()
    created_arg = new_tbl_client.create_table.call_args.args[0]
    self.assertTrue(created_arg.schema)
    self.assertTrue(any(f.name == "timestamp" for f in created_arg.schema))
    self.assertIsNotNone(created_arg.time_partitioning)
    self.assertEqual(
        created_arg.time_partitioning.type_, bq_mod.TimePartitioningType.DAY
    )
    self.assertEqual(created_arg.time_partitioning.field, "timestamp")
    self.assertEqual(
        created_arg.clustering_fields, ["event_type", "agent", "user_id"]
    )
    self.assertEqual(
        created_arg.labels, {"bqaa_demo": "combined_cuj_trace_search"}
    )
    self.assertFalse(new_tbl_client.create_table.call_args.kwargs["exists_ok"])

    # 4. Public get_events_schema is preferred over private _get_events_schema.
    pub_tbl_client = mock.MagicMock()
    pub_tbl_client.get_table.side_effect = _NotFound("absent")
    public_plugin_mod = SimpleNamespace(
        BigQueryAgentAnalyticsPlugin=_FakePlugin,
        BigQueryLoggerConfig=_FakeLoggerConfig,
        get_events_schema=lambda: [
            bq_mod.SchemaField("timestamp", "TIMESTAMP"),
            bq_mod.SchemaField("public_marker", "STRING"),
        ],
        _get_events_schema=lambda: [
            bq_mod.SchemaField("timestamp", "TIMESTAMP"),
            bq_mod.SchemaField("private_marker", "STRING"),
        ],
    )
    with (
        mock.patch.dict(
            sys.modules,
            {
                **fake_adk_modules,
                "google.adk.plugins.bigquery_agent_analytics_plugin": (
                    public_plugin_mod
                ),
            },
        ),
        mock.patch.object(bq_mod, "Client", return_value=pub_tbl_client),
        mock.patch.object(
            run_live_demo, "_run_single_live_scenario", side_effect=_fake_single
        ),
        mock.patch.object(
            run_live_demo,
            "_materialize_and_fetch_live_eval_turns",
            return_value=[],
        ),
    ):
      asyncio.run(
          run_live_demo._run_live_scenarios(
              project_id="demo-project",
              dataset_id="demo_ds",
              table_id="agent_events",
              eval_turns_table="eval_turns",
              location="US",
              agent_location="global",
              model_name="gemini-4-hydrogen",
              user_id="demo_user",
              timeout_sec=10,
          )
      )
    pub_created = pub_tbl_client.create_table.call_args.args[0]
    self.assertIn("public_marker", [f.name for f in pub_created.schema])

    # 5. Missing schema resolver, empty schema, or schema lacking timestamp
    #    must fail closed with RuntimeError without calling create_table.
    for bad_mod_name, bad_plugin_mod in (
        (
            "missing_resolver",
            SimpleNamespace(
                BigQueryAgentAnalyticsPlugin=_FakePlugin,
                BigQueryLoggerConfig=_FakeLoggerConfig,
            ),
        ),
        (
            "empty_schema",
            SimpleNamespace(
                BigQueryAgentAnalyticsPlugin=_FakePlugin,
                BigQueryLoggerConfig=_FakeLoggerConfig,
                _get_events_schema=lambda: [],
            ),
        ),
        (
            "missing_timestamp",
            SimpleNamespace(
                BigQueryAgentAnalyticsPlugin=_FakePlugin,
                BigQueryLoggerConfig=_FakeLoggerConfig,
                _get_events_schema=lambda: [
                    bq_mod.SchemaField("event_type", "STRING")
                ],
            ),
        ),
    ):
      with self.subTest(bad_schema_case=bad_mod_name):
        fail_client = mock.MagicMock()
        fail_client.get_table.side_effect = _NotFound("absent")
        with (
            mock.patch.dict(
                sys.modules,
                {
                    **fake_adk_modules,
                    "google.adk.plugins.bigquery_agent_analytics_plugin": (
                        bad_plugin_mod
                    ),
                },
            ),
            mock.patch.object(bq_mod, "Client", return_value=fail_client),
            mock.patch.object(
                run_live_demo, "_run_single_live_scenario", single_mock
            ),
        ):
          with self.assertRaisesRegex(
              RuntimeError,
              "Unable to resolve BigQueryAgentAnalyticsPlugin events schema"
              " with timestamp field",
          ):
            asyncio.run(
                run_live_demo._run_live_scenarios(
                    project_id="demo-project",
                    dataset_id="demo_ds",
                    table_id="agent_events",
                    eval_turns_table="eval_turns",
                    location="US",
                    agent_location="global",
                    model_name="gemini-4-hydrogen",
                    user_id="demo_user",
                    timeout_sec=10,
                )
            )
        fail_client.create_table.assert_not_called()

  def test_events_table_ownership_conflict_and_http_503_notfound(self) -> None:
    from agent import run_live_demo  # noqa: PLC0415
    from analytics.table_guard import _is_bq_not_found_error  # noqa: PLC0415
    import google.cloud.bigquery as bq_mod  # noqa: PLC0415

    class _NotFound(Exception):
      code = 404

    class _Conflict(Exception):
      code = 409

    # Class named NotFound carrying HTTP 503 must NOT be treated as 404.
    class NotFound(Exception):
      code = 503

    self.assertFalse(_is_bq_not_found_error(NotFound("transient 503")))

    plugin_inits: list[dict] = []

    class _FakeLoggerConfig:

      def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.clustering_fields = ["event_type", "agent", "user_id"]

    class _FakePlugin:

      def __init__(self, **kwargs):
        plugin_inits.append(kwargs)

      async def flush(self):
        return None

      async def shutdown(self, **kwargs):
        del kwargs
        return None

    fake_adk_pkg = types.ModuleType("google.adk")
    fake_adk_pkg.__path__ = []  # type: ignore[attr-defined]
    fake_plugins_pkg = types.ModuleType("google.adk.plugins")
    fake_plugins_pkg.__path__ = []  # type: ignore[attr-defined]
    fake_plugin_mod = SimpleNamespace(
        BigQueryAgentAnalyticsPlugin=_FakePlugin,
        BigQueryLoggerConfig=_FakeLoggerConfig,
        _get_events_schema=lambda: [
            bq_mod.SchemaField("timestamp", "TIMESTAMP"),
            bq_mod.SchemaField("event_type", "STRING"),
        ],
    )
    fake_adk_modules = {
        "google.adk": fake_adk_pkg,
        "google.adk.plugins": fake_plugins_pkg,
        "google.adk.plugins.bigquery_agent_analytics_plugin": fake_plugin_mod,
    }
    single_mock = mock.AsyncMock()

    # 1. Pre-existing unowned agent_events rejected BEFORE logger starts.
    unowned_events_client = mock.MagicMock()

    def _get_unowned_events(fqn: str):
      if fqn.endswith(".eval_turns"):
        raise _NotFound("absent")
      return SimpleNamespace(labels={"owner": "other_team"})

    unowned_events_client.get_table.side_effect = _get_unowned_events
    with (
        mock.patch.dict(sys.modules, fake_adk_modules),
        mock.patch.object(bq_mod, "Client", return_value=unowned_events_client),
        mock.patch.object(
            run_live_demo, "_run_single_live_scenario", single_mock
        ),
    ):
      with self.assertRaisesRegex(RuntimeError, "missing demo ownership label"):
        asyncio.run(
            run_live_demo._run_live_scenarios(
                project_id="demo-project",
                dataset_id="demo_ds",
                table_id="agent_events",
                eval_turns_table="eval_turns",
                location="US",
                agent_location="global",
                model_name="gemini-4-hydrogen",
                user_id="demo_user",
                timeout_sec=10,
            )
        )
    self.assertEqual(plugin_inits, [])
    unowned_events_client.create_table.assert_not_called()
    unowned_events_client.update_table.assert_not_called()
    single_mock.assert_not_awaited()

    # 2. NotFound(code=503) re-raises instead of creating or starting logger.
    err503_client = mock.MagicMock()

    def _get_503(fqn: str):
      if fqn.endswith(".eval_turns"):
        raise _NotFound("absent")
      raise NotFound("backend unavailable")

    err503_client.get_table.side_effect = _get_503
    with (
        mock.patch.dict(sys.modules, fake_adk_modules),
        mock.patch.object(bq_mod, "Client", return_value=err503_client),
        mock.patch.object(
            run_live_demo, "_run_single_live_scenario", single_mock
        ),
    ):
      with self.assertRaises(NotFound):
        asyncio.run(
            run_live_demo._run_live_scenarios(
                project_id="demo-project",
                dataset_id="demo_ds",
                table_id="agent_events",
                eval_turns_table="eval_turns",
                location="US",
                agent_location="global",
                model_name="gemini-4-hydrogen",
                user_id="demo_user",
                timeout_sec=10,
            )
        )
    self.assertEqual(plugin_inits, [])
    err503_client.create_table.assert_not_called()

    # 3. 409 Conflict during create_table against unowned table fails closed.
    conflict_unowned_client = mock.MagicMock()
    ev_calls = {"n": 0}

    def _get_conflict_unowned(fqn: str):
      if fqn.endswith(".eval_turns"):
        raise _NotFound("absent")
      ev_calls["n"] += 1
      if ev_calls["n"] == 1:
        raise _NotFound("absent initially")
      return SimpleNamespace(labels={"owner": "concurrent_other"})

    conflict_unowned_client.get_table.side_effect = _get_conflict_unowned
    conflict_unowned_client.create_table.side_effect = _Conflict(
        "already exists"
    )
    with (
        mock.patch.dict(sys.modules, fake_adk_modules),
        mock.patch.object(
            bq_mod, "Client", return_value=conflict_unowned_client
        ),
        mock.patch.object(
            run_live_demo, "_run_single_live_scenario", single_mock
        ),
    ):
      with self.assertRaisesRegex(RuntimeError, "missing demo ownership label"):
        asyncio.run(
            run_live_demo._run_live_scenarios(
                project_id="demo-project",
                dataset_id="demo_ds",
                table_id="agent_events",
                eval_turns_table="eval_turns",
                location="US",
                agent_location="global",
                model_name="gemini-4-hydrogen",
                user_id="demo_user",
                timeout_sec=10,
            )
        )
    self.assertEqual(plugin_inits, [])
    single_mock.assert_not_awaited()

    # 4. 409 Conflict during create_table against demo-owned table succeeds.
    conflict_owned_client = mock.MagicMock()
    ev_calls_ok = {"n": 0}

    def _get_conflict_owned(fqn: str):
      if fqn.endswith(".eval_turns"):
        raise _NotFound("absent")
      ev_calls_ok["n"] += 1
      if ev_calls_ok["n"] == 1:
        raise _NotFound("absent initially")
      return SimpleNamespace(labels={"bqaa_demo": "combined_cuj_trace_search"})

    conflict_owned_client.get_table.side_effect = _get_conflict_owned
    conflict_owned_client.create_table.side_effect = _Conflict("already exists")

    async def _fake_single(**kwargs):
      return {
          "scenario_id": kwargs["scenario"].scenario_id,
          "session_id": kwargs["session_id"],
          "tools_called": [],
          "response": "ok",
      }

    with (
        mock.patch.dict(sys.modules, fake_adk_modules),
        mock.patch.object(bq_mod, "Client", return_value=conflict_owned_client),
        mock.patch.object(
            run_live_demo, "_run_single_live_scenario", side_effect=_fake_single
        ),
        mock.patch.object(
            run_live_demo,
            "_materialize_and_fetch_live_eval_turns",
            return_value=[],
        ),
    ):
      asyncio.run(
          run_live_demo._run_live_scenarios(
              project_id="demo-project",
              dataset_id="demo_ds",
              table_id="agent_events",
              eval_turns_table="eval_turns",
              location="US",
              agent_location="global",
              model_name="gemini-4-hydrogen",
              user_id="demo_user",
              timeout_sec=10,
          )
      )
    self.assertEqual(len(plugin_inits), 1)
    conflict_owned_client.update_table.assert_not_called()

  def test_quoted_container_freetext_budget_tail_and_parent_bypass_redaction(
      self,
  ) -> None:
    from agent.tools import sanitized_tool_classifier  # noqa: PLC0415
    from analytics.trajectory_extractor import extract_s3_outcome_first  # noqa: PLC0415
    from analytics.trajectory_extractor import is_sensitive_key  # noqa: PLC0415
    from analytics.trajectory_extractor import scrub_sensitive_data  # noqa: PLC0415
    from analytics.trajectory_extractor import scrub_sensitive_text  # noqa: PLC0415

    # 1. Free-text sensitive key + balanced/quoted/unterminated values.
    err_cred = scrub_sensitive_text(
        'CARD_EXPIRED credentials={"value":"x_sec"}'
    )
    self.assertIn("CARD_EXPIRED", err_cred)
    self.assertNotIn("x_sec", err_cred)
    self.assertNotIn('"value"', err_cred)
    self.assertEqual(err_cred, "CARD_EXPIRED credentials=[REDACTED]")

    py_auth = scrub_sensitive_text(
        "CARD_EXPIRED auth={'token': 'py_tok_1', 'nested': {'k': 'py_val_2'}}"
        " tail=ok"
    )
    self.assertNotIn("py_tok_1", py_auth)
    self.assertNotIn("py_val_2", py_auth)
    self.assertIn("CARD_EXPIRED", py_auth)
    self.assertIn("tail=ok", py_auth)

    list_sec = scrub_sensitive_text(
        'secrets=[{"a": "list_s1"}, "list_s2"] status=done'
    )
    self.assertNotIn("list_s1", list_sec)
    self.assertNotIn("list_s2", list_sec)
    self.assertIn("status=done", list_sec)

    for quoted_case, secret_piece, keep_piece in (
        ('password="my secret with spaces" ok=1', "with spaces", "ok=1"),
        ("password: 'my secret with spaces', ok=2", "with spaces", "ok=2"),
        (
            'the password is "secret with spaces" and status is delivered',
            "with spaces",
            "status is delivered",
        ),
        (
            r'password="escaped \"quote\" secret" ok=3',
            "secret",
            "ok=3",
        ),
        ('password="unterminated secret tail', "secret tail", "password="),
        (
            'credentials={"unterminated": "container_tail',
            "container_tail",
            "credentials=",
        ),
    ):
      out = scrub_sensitive_text(quoted_case)
      self.assertNotIn(secret_piece, out)
      self.assertIn(keep_piece, out)
      self.assertIn("[REDACTED]", out)

    # 2. Budget exhaustion in _scrub_embedded_json_segments never leaks tail.
    many_objs = ("{} " * 40) + '{"password": {"nested": "tail_leak_40"}}'
    scrubbed_many = scrub_sensitive_text(many_objs)
    self.assertNotIn("tail_leak_40", scrubbed_many)
    self.assertIn("[REDACTED]", scrubbed_many)

    large_brackets = ("[" * 20_000) + ' {"password": {"nested": "tail_leak"}}'
    scrubbed_large = scrub_sensitive_text(large_brackets)
    self.assertNotIn("tail_leak", scrubbed_large)
    self.assertIn("[REDACTED]", scrubbed_large)

    oversized_json = (
        '{"padding": "'
        + ("a" * 17_000)
        + '", "password": {"nested": "oversized_leak"}}'
    )
    scrubbed_oversized = scrub_sensitive_text(oversized_json)
    self.assertNotIn("oversized_leak", scrubbed_oversized)

    # 3. Structured parent keys (auth_result, credentials_result, authentication,
    #    and container-valued auth_error) must redact while scalar diagnostics stay.
    for key_name in ("auth_result", "credentials_result", "authentication"):
      self.assertTrue(is_sensitive_key(key_name))
    self.assertFalse(is_sensitive_key("auth_error", is_container=False))
    self.assertTrue(is_sensitive_key("auth_error", is_container=True))

    structured = scrub_sensitive_data(
        {
            "auth_result": {"value": "s1_leak"},
            "credentials_result": {"value": "s2_leak"},
            "authentication": {"value": "s3_leak"},
            "auth_error": {"secret": "s4_leak"},
            "auth_status": "forbidden",
            "credential_type": "oauth_service_account",
            "oauth_state": "state_nonce_ok",
            "author": "customer_ops_agent",
            "authority": "payments.example.com",
        }
    )
    self.assertEqual(structured["auth_result"], "[REDACTED]")
    self.assertEqual(structured["credentials_result"], "[REDACTED]")
    self.assertEqual(structured["authentication"], "[REDACTED]")
    self.assertEqual(structured["auth_error"], "[REDACTED]")
    self.assertEqual(structured["auth_status"], "forbidden")
    self.assertEqual(structured["credential_type"], "oauth_service_account")
    self.assertEqual(structured["oauth_state"], "state_nonce_ok")
    self.assertEqual(structured["author"], "customer_ops_agent")
    self.assertEqual(structured["authority"], "payments.example.com")

    # Also verify end-to-end inside classifier and extract_s3_outcome_first.
    verdict = sanitized_tool_classifier(
        tool=SimpleNamespace(name="auth_probe"),
        tool_args={},
        tool_context=SimpleNamespace(),
        result={
            "ok": False,
            "error_code": "CARD_EXPIRED",
            "message": 'CARD_EXPIRED credentials={"value":"clf_leak"}',
        },
    )
    self.assertIsNotNone(verdict)
    self.assertIn("CARD_EXPIRED", verdict.error_message)
    self.assertNotIn("clf_leak", verdict.error_message)

    s3 = extract_s3_outcome_first(
        {
            "turns": [
                {
                    "events": [
                        {
                            "author": "customer_ops_agent",
                            "content": {
                                "role": "model",
                                "parts": [
                                    {
                                        "function_call": {
                                            "name": "auth_check",
                                            "args": {
                                                "auth_result": {
                                                    "value": "s3_ar_leak"
                                                },
                                                "credentials_result": {
                                                    "value": "s3_cr_leak"
                                                },
                                                "authentication": {
                                                    "value": "s3_an_leak"
                                                },
                                            },
                                        }
                                    },
                                    {
                                        "text": (
                                            "CARD_EXPIRED"
                                            ' credentials={"value":"s3_str_leak"}'
                                        )
                                    },
                                ],
                            },
                        }
                    ]
                }
            ]
        },
        metrics={
            "has_error": True,
            "error_summary": 'CARD_EXPIRED credentials={"value":"s3_sum_leak"}',
        },
    )
    for leaked in (
        "s3_ar_leak",
        "s3_cr_leak",
        "s3_an_leak",
        "s3_str_leak",
        "s3_sum_leak",
    ):
      self.assertNotIn(leaked, s3)
    self.assertIn("CARD_EXPIRED", s3)

  def test_multiline_unterminated_quotes_unified_kv_parity_and_bomb_perf(
      self,
  ) -> None:
    from analytics.index_and_search import _scrub_error_summary  # noqa: PLC0415
    from analytics.trajectory_extractor import extract_s3_outcome_first  # noqa: PLC0415
    from analytics.trajectory_extractor import is_sensitive_key  # noqa: PLC0415
    from analytics.trajectory_extractor import scrub_sensitive_data  # noqa: PLC0415
    from analytics.trajectory_extractor import scrub_sensitive_text  # noqa: PLC0415

    # 1. P1-1: Unterminated / oversized quoted values fail closed to EOS.
    p1_1_cases = (
        ('password="line1\nSECRET_LINE_2', "SECRET_LINE_2"),
        (
            'credentials=\'{\n  "value": "my_secret",\n  "pad": "truncated',
            "my_secret",
        ),
        (
            'private_key="-----BEGIN PRIVATE'
            " KEY-----\nMIIEvQIBADANBgkqhkiG9w0_SECRET",
            "MIIEvQIBADANBgkqhkiG9w0_SECRET",
        ),
        (
            'credentials="hdr\n' + ("B" * 16500) + 'TAIL_SECRET"',
            "TAIL_SECRET",
        ),
    )
    for sample, secret_val in p1_1_cases:
      with self.subTest(p1_1=secret_val):
        scrubbed = scrub_sensitive_text(sample)
        self.assertNotIn(secret_val, scrubbed)
        self.assertIn("[REDACTED]", scrubbed)

    # 2. P1-2: Unified dynamic KV scrubbing across scrub_sensitive_text,
    #    error_summary, and S3 trajectory.
    p1_2_cases = (
        ('CARD_EXPIRED auth_error={"value":"LEAK_AE_VAL"}', "LEAK_AE_VAL"),
        (
            "CARD_EXPIRED auth_error={'value':'LEAK_AE_SQ_VAL'}",
            "LEAK_AE_SQ_VAL",
        ),
        (
            'CARD_EXPIRED authentication_result={"value":"LEAK_AUTH_RES"}',
            "LEAK_AUTH_RES",
        ),
        (
            'CARD_EXPIRED credentials_payload={"value":"LEAK_CRED_PAY"}',
            "LEAK_CRED_PAY",
        ),
        (
            'CARD_EXPIRED credential_data={"value":"LEAK_CRED_DATA"}',
            "LEAK_CRED_DATA",
        ),
        ('credentials=\n{"value":"LEAK_ML_EQ"}', "LEAK_ML_EQ"),
        ("myPwd=secret_mypwd", "secret_mypwd"),
    )
    for sample, secret_val in p1_2_cases:
      with self.subTest(p1_2=secret_val):
        out_txt = scrub_sensitive_text(sample)
        out_err = _scrub_error_summary(sample)
        out_s3 = extract_s3_outcome_first(
            {
                "turns": [
                    {
                        "events": [
                            {
                                "author": "support_agent",
                                "content": {
                                    "role": "model",
                                    "parts": [{"text": sample}],
                                },
                            }
                        ]
                    }
                ]
            },
            metrics={"has_error": True, "error_summary": sample},
        )
        for surface_name, rendered in (
            ("text", out_txt),
            ("error_summary", out_err),
            ("s3", out_s3),
        ):
          self.assertNotIn(secret_val, rendered, msg=surface_name)
          self.assertIn("[REDACTED]", rendered, msg=surface_name)
        if "CARD_EXPIRED" in sample:
          self.assertIn("CARD_EXPIRED", out_txt)
          self.assertIn("CARD_EXPIRED", out_err)
          self.assertIn("CARD_EXPIRED", out_s3)

    # 3. P2-2: Dict/text parity for all secret keys + all benign keep forms.
    secret_parity_pairs = (
        ("DB_PASSWORD", "sec_db_pw"),
        ("PGPASSWORD", "sec_pg_pw"),
        ("myPwd", "sec_my_pwd"),
        ("auth_result", "sec_auth_res"),
        ("credentials_result", "sec_cred_res"),
        ("authentication", "sec_authn"),
        ("db_password", "sec_db_pw2"),
        ("x_api_key", "sec_x_api"),
        ("stripe_api_key", "sec_stripe_api"),
        ("GOOGLE_CLIENT_SECRET", "sec_gcs"),
        ("REDIS_AUTH_TOKEN", "sec_redis_tok"),
        ("ANTHROPIC_API_KEY", "sec_anth_api"),
        ("newPassword", "sec_new_pw"),
        ("oldPassword", "sec_old_pw"),
        ("userPassword", "sec_user_pw"),
        ("googleApiKey", "sec_g_api"),
        ("stripeApiKey", "sec_s_api"),
        ("myClientSecret", "sec_mcs"),
        ("fbAccessToken", "sec_fb_tok"),
        ("dbpassword", "sec_dbpw3"),
    )
    for key_name, val in secret_parity_pairs:
      with self.subTest(secret_parity=key_name):
        self.assertTrue(is_sensitive_key(key_name, is_container=False))
        d_out = scrub_sensitive_data({key_name: val})
        self.assertEqual(d_out[key_name], "[REDACTED]")
        t_out = scrub_sensitive_text(f"{key_name}={val}")
        self.assertNotIn(val, t_out)
        self.assertIn("[REDACTED]", t_out)

    # Container auth_error redacts in both dict and text.
    self.assertTrue(is_sensitive_key("auth_error", is_container=True))
    self.assertEqual(
        scrub_sensitive_data({"auth_error": {"value": "cnt_ae"}})["auth_error"],
        "[REDACTED]",
    )

    benign_kv_parity = (
        ("passwordless", "true", "passwordless=true"),
        (
            "DB_PASSWORD_FILE",
            "/var/run/secrets/db_pw",
            "DB_PASSWORD_FILE=/var/run/secrets/db_pw",
        ),
        ("NOPASSWD_FILE", "/etc/sudoers", "NOPASSWD_FILE=/etc/sudoers"),
        ("total_tokens", "1542", "total_tokens=1542"),
        ("prompt_token", "412", "prompt_token=412"),
        ("auth_error", "PERMISSION_DENIED", "auth_error: PERMISSION_DENIED"),
        ("auth_status", "forbidden", "auth_status: forbidden"),
        (
            "credential_type",
            "oauth_service_account",
            "credential_type: oauth_service_account",
        ),
        ("author", "alice", "author: alice"),
        ("authority", "ca", "authority: ca"),
    )
    for key_name, val, text_form in benign_kv_parity:
      with self.subTest(benign_parity=key_name):
        self.assertFalse(is_sensitive_key(key_name, is_container=False))
        d_out = scrub_sensitive_data({key_name: val})
        self.assertEqual(d_out[key_name], val)
        self.assertEqual(scrub_sensitive_text(text_form), text_form)
    # Numeric cookies counts are preserved while cookie jars/strings redact.
    self.assertTrue(is_sensitive_key("cookies", is_container=False))
    self.assertEqual(scrub_sensitive_data({"cookies": 3})["cookies"], 3)
    self.assertEqual(scrub_sensitive_data({"cookies": "3"})["cookies"], "3")
    self.assertEqual(scrub_sensitive_text("cookies=3"), "cookies=3")
    self.assertEqual(
        scrub_sensitive_data({"cookies": {"sessionid": "s_val"}})["cookies"],
        "[REDACTED]",
    )
    self.assertEqual(
        scrub_sensitive_data({"cookies": "sessionid=s_val"})["cookies"],
        "[REDACTED]",
    )
    self.assertEqual(
        scrub_sensitive_data({"request": {"cookies": ["sessionid=s_val"]}})[
            "request"
        ]["cookies"],
        "[REDACTED]",
    )
    for cookie_text in (
        'cookies={"sessionid": "s_val"}',
        'cookies="sessionid=s_val"',
        'request.cookies=["sessionid=s_val"]',
    ):
      out_c = scrub_sensitive_text(cookie_text)
      self.assertNotIn("s_val", out_c)
      self.assertIn("[REDACTED]", out_c)
    self.assertEqual(scrub_sensitive_text("CARD_EXPIRED"), "CARD_EXPIRED")
    self.assertEqual(
        scrub_sensitive_text("status is delivered"), "status is delivered"
    )

    # 4. Pathological bracket bombs remain sub-second with zero leaks.
    for bomb_name, bomb_text, tail_secret in (
        ("open_200k", ("[" * 200_000) + " password=bomb_s1", "bomb_s1"),
        ("mismatch_50k", ("[}" * 50_000) + " password=bomb_s2", "bomb_s2"),
    ):
      with self.subTest(bomb=bomb_name):
        scrubbed_bomb, elapsed = _timed_scrub_median_sec(
            scrub_sensitive_text, bomb_text
        )
        self.assertLess(elapsed, 5.0, f"{bomb_name} took {elapsed:.3f}s")
        self.assertNotIn(tail_secret, scrubbed_bomb)
        self.assertIn("[REDACTED]", scrubbed_bomb)

  def test_preceding_word_perf_and_repr_escaped_dotted_numbered_keys(
      self,
  ) -> None:
    from analytics.trajectory_extractor import is_sensitive_key  # noqa: PLC0415
    from analytics.trajectory_extractor import scrub_sensitive_data  # noqa: PLC0415
    from analytics.trajectory_extractor import scrub_sensitive_text  # noqa: PLC0415

    # 1. O(N^2) regression guard: repeated multiword tail keys on one line.
    long_line = ("x code=1 " * 25_000) + "password=tail7"
    scrubbed_long, elapsed = _timed_scrub_median_sec(
        scrub_sensitive_text, long_line
    )
    self.assertLess(elapsed, 5.0, f"long_line took {elapsed:.3f}s")
    self.assertNotIn("tail7", scrubbed_long)
    self.assertTrue(scrubbed_long.endswith("password=[REDACTED]"))

    # 2. Python bytes/string reprs, tuples, constructors, escaped quotes,
    #    dotted keys, and numbered/plural sensitive keys.
    secret_cases = (
        ("bytes_sq", "password=b'my_bytes_secret'", "my_bytes_secret"),
        ("bytes_dq", 'password=b"my_bytes_secret"', "my_bytes_secret"),
        ("raw_bytes", "password=rb'raw_bytes_sec'", "raw_bytes_sec"),
        (
            "tuple_pw",
            "password=('user_x', 'secret_pass_tuple')",
            "secret_pass_tuple",
        ),
        (
            "tuple_auth_err",
            "auth_error=('secret_ae_tuple',)",
            "secret_ae_tuple",
        ),
        (
            "ctor_creds",
            "credentials=Credentials(value='ctor_secret')",
            "ctor_secret",
        ),
        (
            "ctor_secret_str",
            "password=SecretStr('my_secret')",
            "my_secret",
        ),
        (
            "escaped_dq_spaces",
            r"password=\"my secret with spaces\" ok=1",
            "my secret with spaces",
        ),
        (
            "escaped_dq_inner",
            r"password=\"a \\\"inner\\\" b\" ok=1",
            "inner",
        ),
        (
            "escaped_dq_unterminated",
            'password=\\"unterminated\nsecret_tail',
            "secret_tail",
        ),
        (
            "dotted_sq",
            "{'db.password': 'dot_secret'}",
            "dot_secret",
        ),
        (
            "dotted_dq",
            '{"db.password": "dot_secret"}',
            "dot_secret",
        ),
        ("numbered_pw", "password1=s1", "s1"),
        ("numbered_apikey", "apiKey2=s2", "s2"),
        ("plural_apikeys", 'api_keys=["k1"]', "k1"),
    )
    for label, sample, secret_val in secret_cases:
      with self.subTest(secret_case=label):
        out = scrub_sensitive_text(sample)
        self.assertNotIn(secret_val, out)
        self.assertIn("[REDACTED]", out)
        if "ok=1" in sample:
          self.assertIn("ok=1", out)

    # Dict/text parity for dotted, numbered, and plural sensitive keys.
    self.assertEqual(
        scrub_sensitive_data({"db.password": "dot_secret"})["db.password"],
        "[REDACTED]",
    )
    self.assertEqual(
        scrub_sensitive_data({"password1": "s1"})["password1"], "[REDACTED]"
    )
    self.assertEqual(
        scrub_sensitive_data({"apiKey2": "s2"})["apiKey2"], "[REDACTED]"
    )
    self.assertEqual(
        scrub_sensitive_data({"api_keys": ["k1"]})["api_keys"], "[REDACTED]"
    )
    self.assertTrue(is_sensitive_key("db.password"))
    self.assertTrue(is_sensitive_key("password1"))
    self.assertTrue(is_sensitive_key("apiKey2"))
    self.assertTrue(is_sensitive_key("api_keys"))

    # 3. Benign keys and phrases remain preserved.
    benign_cases = (
        (
            "DB_PASSWORD_FILE",
            "/var/run/db_pw",
            "DB_PASSWORD_FILE=/var/run/db_pw",
        ),
        ("NOPASSWD_FILE", "/etc/sudoers", "NOPASSWD_FILE=/etc/sudoers"),
        ("passwordless", "true", "passwordless=true"),
        ("auth_error", "CARD_EXPIRED", 'auth_error="CARD_EXPIRED"'),
        ("auth_error", "invalid_grant", "auth_error=invalid_grant"),
        ("auth_status", "forbidden", "auth_status=forbidden"),
        (
            "credential_type",
            "oauth_service_account",
            "credential_type=oauth_service_account",
        ),
        ("total_tokens", "1542", "total_tokens=1542"),
        ("prompt_token", "412", "prompt_token=412"),
        ("author", "alice", "author=alice"),
        ("authority", "payments.example.com", "authority=payments.example.com"),
    )
    for key_name, val, text_form in benign_cases:
      with self.subTest(benign_case=f"{key_name}:{val}"):
        self.assertFalse(is_sensitive_key(key_name, is_container=False))
        self.assertEqual(scrub_sensitive_data({key_name: val})[key_name], val)
        self.assertEqual(scrub_sensitive_text(text_form), text_form)
    # Numeric cookies remain preserved while structured cookie values redact.
    self.assertEqual(scrub_sensitive_data({"cookies": 3})["cookies"], 3)
    self.assertEqual(scrub_sensitive_text("cookies=3"), "cookies=3")
    self.assertEqual(
        scrub_sensitive_data({"cookies": {"sessionid": "s_val"}})["cookies"],
        "[REDACTED]",
    )
    self.assertEqual(
        scrub_sensitive_text("status is delivered"), "status is delivered"
    )

  def test_cookie_jars_nextline_merge_base_diagnostics_large_json_and_503_guard(
      self,
  ) -> None:
    from unittest.mock import MagicMock  # noqa: PLC0415

    from analytics.table_guard import _is_bq_conflict_error  # noqa: PLC0415
    from analytics.table_guard import _is_bq_not_found_error  # noqa: PLC0415
    from analytics.table_guard import assert_demo_table_ownership_or_absent  # noqa: PLC0415
    from analytics.trajectory_extractor import is_sensitive_key  # noqa: PLC0415
    from analytics.trajectory_extractor import scrub_sensitive_data  # noqa: PLC0415
    from analytics.trajectory_extractor import scrub_sensitive_text  # noqa: PLC0415

    # 1. Cookie jars & cookie strings redact while numeric counts survive.
    self.assertEqual(
        scrub_sensitive_data({"cookies": {"sessionid": "abc"}}),
        {"cookies": "[REDACTED]"},
    )
    self.assertEqual(
        scrub_sensitive_data({"cookies": ["sessionid=abc"]}),
        {"cookies": "[REDACTED]"},
    )
    self.assertEqual(
        scrub_sensitive_data({"request": {"cookies": ["sessionid=abc"]}}),
        {"request": {"cookies": "[REDACTED]"}},
    )
    self.assertEqual(
        scrub_sensitive_data({"cookies": "sessionid=abc"}),
        {"cookies": "[REDACTED]"},
    )
    self.assertEqual(
        scrub_sensitive_data({"set_cookies": 3}),
        {"set_cookies": "[REDACTED]"},
    )
    self.assertEqual(scrub_sensitive_data({"cookies": 3}), {"cookies": 3})
    self.assertEqual(scrub_sensitive_data({"cookies": "3"}), {"cookies": "3"})
    self.assertEqual(
        scrub_sensitive_data({"credential_count": 3}),
        {"credential_count": 3},
    )
    self.assertEqual(
        scrub_sensitive_data({"credential_count": "3"}),
        {"credential_count": "3"},
    )
    self.assertEqual(
        scrub_sensitive_data({"credential_count": {"secret": "x"}}),
        {"credential_count": "[REDACTED]"},
    )
    self.assertEqual(scrub_sensitive_text("cookies=3"), "cookies=3")
    self.assertEqual(scrub_sensitive_text("cookies: 3"), "cookies: 3")
    self.assertEqual(
        scrub_sensitive_text("credential_count=3"), "credential_count=3"
    )
    for sample, secret_val in (
        ('cookies={"sessionid":"s_val"}', "s_val"),
        ('cookies=["sid=s_val"]', "s_val"),
        ('cookies="sessionid=s_val"', "s_val"),
        ("cookies=sessionid=s_val", "s_val"),
        ('request.cookies=["sid=s_val"]', "s_val"),
        ('credential_count={"value": "cc_sec"}', "cc_sec"),
    ):
      with self.subTest(cookie_or_count=sample):
        out = scrub_sensitive_text(sample)
        self.assertNotIn(secret_val, out)
        self.assertIn("[REDACTED]", out)

    # 2. Next-line bare values after sensitive keys redact cleanly.
    for sample, secret_val in (
        ("CARD_EXPIRED password:\n  nextline_sec", "nextline_sec"),
        ("password is:\nnextline_sec2", "nextline_sec2"),
        ("credentials:\n  nextline_sec3", "nextline_sec3"),
        ('credentials=\n{"value":"nl_json_sec"}', "nl_json_sec"),
    ):
      with self.subTest(nextline=sample):
        out = scrub_sensitive_text(sample)
        self.assertNotIn(secret_val, out)
        self.assertIn("[REDACTED]", out)
        if "CARD_EXPIRED" in sample:
          self.assertIn("CARD_EXPIRED", out)

    # 3. Merge-base key spellings redact while benign neighbors stay intact.
    for key_name, val in (
        ("passWord", "pw_sec"),
        ("mysecret", "ms_sec"),
        ("usercredentials", "uc_sec"),
        ("mypwd", "mp_sec"),
    ):
      with self.subTest(merge_base_key=key_name):
        self.assertTrue(is_sensitive_key(key_name, is_container=False))
        self.assertEqual(
            scrub_sensitive_data({key_name: val})[key_name], "[REDACTED]"
        )
        out = scrub_sensitive_text(f"{key_name}={val}")
        self.assertNotIn(val, out)
        self.assertIn("[REDACTED]", out)
    self.assertEqual(
        scrub_sensitive_text("secretary=alice author=bob authority=ca"),
        "secretary=alice author=bob authority=ca",
    )

    # 4. Preserved scalar diagnostics vs redacted containers/tokens.
    self.assertEqual(
        scrub_sensitive_data({"authentication_result": "FAILED"}),
        {"authentication_result": "FAILED"},
    )
    self.assertEqual(
        scrub_sensitive_text("authentication_result=failed"),
        "authentication_result=failed",
    )
    self.assertEqual(
        scrub_sensitive_data({"authentication_result": {"value": "ar_sec"}}),
        {"authentication_result": "[REDACTED]"},
    )
    self.assertNotIn(
        "ar_sec",
        scrub_sensitive_text('authentication_result={"value": "ar_sec"}'),
    )
    self.assertEqual(
        scrub_sensitive_data({"authentication": "failed"}),
        {"authentication": "failed"},
    )
    self.assertEqual(
        scrub_sensitive_text("Authentication: failed"),
        "Authentication: failed",
    )
    self.assertEqual(
        scrub_sensitive_text("Authentication:\n  failed"),
        "Authentication:\n  failed",
    )
    self.assertEqual(
        scrub_sensitive_data({"authentication": {"value": "auth_cnt_sec"}}),
        {"authentication": "[REDACTED]"},
    )
    for sample, secret_val in (
        ("Authentication: raw_secret_tok", "raw_secret_tok"),
        ('authentication={"token": "auth_tok_sec"}', "auth_tok_sec"),
    ):
      out = scrub_sensitive_text(sample)
      self.assertNotIn(secret_val, out)
      self.assertIn("[REDACTED]", out)

    # 5. Large ordinary JSON (>17 KiB) and 50 [] citations keep CARD_EXPIRED
    #    while tail / nested / Unicode-escaped secrets still redact.
    large_items = ",".join(f'{{"id":{i},"ok":true}}' for i in range(1100))
    large_ordinary = f'{{"items":[{large_items}]}} ERROR: CARD_EXPIRED'
    self.assertGreater(len(large_ordinary), 17_000)
    out_large_ord = scrub_sensitive_text(large_ordinary)
    self.assertIn("ERROR: CARD_EXPIRED", out_large_ord)
    self.assertNotIn("[REDACTED]", out_large_ord)

    cite_nums = ("[1] " * 50) + "ERROR: CARD_EXPIRED"
    self.assertEqual(scrub_sensitive_text(cite_nums), cite_nums)
    cite_info = ("[INFO] " * 50) + "ERROR: CARD_EXPIRED"
    self.assertEqual(scrub_sensitive_text(cite_info), cite_info)

    for label, sample, secret_val in (
        (
            "after_40_empty_objs",
            ("{} " * 40) + '{"password": "tail_sec_40"} ERROR: CARD_EXPIRED',
            "tail_sec_40",
        ),
        (
            "after_40_nonempty_objs",
            ('{"k":1} ' * 40)
            + '{"password": {"nested": "tail_sec_nonempty"}}'
            + " ERROR: CARD_EXPIRED",
            "tail_sec_nonempty",
        ),
        (
            "after_50_citations",
            ("[1] " * 50) + '{"password": "cite_sec"} ERROR: CARD_EXPIRED',
            "cite_sec",
        ),
        (
            "after_20k_brackets",
            ("[" * 20_000)
            + ' {"password": {"nested": "b20k_sec"}} ERROR: CARD_EXPIRED',
            "b20k_sec",
        ),
        (
            "inside_large_json",
            '{"padding": "'
            + ("a" * 17_000)
            + '", "password": {"nested": "big_sec"}} ERROR: CARD_EXPIRED',
            "big_sec",
        ),
        (
            "unicode_key_in_large_json",
            '{"padding": "'
            + ("a" * 17_000)
            + '", "\\u0070assword": "uni_sec"} ERROR: CARD_EXPIRED',
            "uni_sec",
        ),
        (
            "unicode_key_after_40_objs",
            ('{"k":1} ' * 40)
            + '{"\\u0070assword": "uni_sec_40"} ERROR: CARD_EXPIRED',
            "uni_sec_40",
        ),
    ):
      with self.subTest(budget_case=label):
        out = scrub_sensitive_text(sample)
        self.assertNotIn(secret_val, out)
        self.assertIn("[REDACTED]", out)
        self.assertIn("CARD_EXPIRED", out)

    # 6. NotFound/Conflict with HTTP 503 on response.status_code fail closed.
    class NotFoundWith404(Exception):
      code = 404

      def __init__(self, msg: str, *, response: object = None) -> None:
        super().__init__(msg)
        self.response = response

    class NotFoundNoCode(Exception):

      def __init__(self, msg: str, *, response: object = None) -> None:
        super().__init__(msg)
        self.response = response

    class ConflictWith409(Exception):
      code = 409

      def __init__(self, msg: str, *, response: object = None) -> None:
        super().__init__(msg)
        self.response = response

    exc_nf_404_503 = NotFoundWith404(
        "upstream 503", response=SimpleNamespace(status_code=503)
    )
    exc_nf_nocode_503 = NotFoundNoCode(
        "upstream 503", response=SimpleNamespace(status_code=503)
    )
    exc_cf_409_503 = ConflictWith409(
        "upstream 503", response=SimpleNamespace(status_code=503)
    )
    self.assertFalse(_is_bq_not_found_error(exc_nf_404_503))
    self.assertFalse(_is_bq_not_found_error(exc_nf_nocode_503))
    self.assertFalse(_is_bq_conflict_error(exc_cf_409_503))

    for exc_503 in (exc_nf_404_503, exc_nf_nocode_503):
      mock_bq = MagicMock()
      mock_bq.get_table.side_effect = exc_503
      with self.assertRaises(type(exc_503)):
        assert_demo_table_ownership_or_absent(
            mock_bq, "demo-proj", "agent_ops", "agent_events"
        )

  def test_post_budget_escaped_slash_long_keys_nextline_and_cookie_ids(
      self,
  ) -> None:
    from analytics.trajectory_extractor import is_sensitive_key  # noqa: PLC0415
    from analytics.trajectory_extractor import scrub_sensitive_data  # noqa: PLC0415
    from analytics.trajectory_extractor import scrub_sensitive_text  # noqa: PLC0415

    long_pw_key = ("very_long_prefix_" * 4) + "password"
    self.assertGreaterEqual(len(long_pw_key), 65)
    nested_esc = (
        r'"{\"password\": \"esc_sec\", '
        r'\"\\u0070assword\": \"esc_uni_sec\"}"'
    )
    tail_blob = (
        f'{{"nested": {nested_esc}, "db/password": "slash_sec", '
        f'"{long_pw_key}": "long_sec"}} ERROR: CARD_EXPIRED'
    )
    post_32_payload = ('{"k":1} ' * 35) + tail_blob
    large_20k_payload = (
        '{"padding": "'
        + ("a" * 20_500)
        + f'", "nested": {nested_esc}, "db/password": "slash_sec", '
        + f'"{long_pw_key}": "long_sec"}} ERROR: CARD_EXPIRED'
    )
    self.assertGreater(len(large_20k_payload), 20_480)

    for label, sample in (
        ("post_32_objects", post_32_payload),
        ("over_20kib_json", large_20k_payload),
    ):
      with self.subTest(case=label):
        out = scrub_sensitive_text(sample)
        for secret_val in ("esc_sec", "esc_uni_sec", "slash_sec", "long_sec"):
          self.assertNotIn(secret_val, out)
        self.assertIn("CARD_EXPIRED", out)

    # Dict and single-quoted parity for slash-separated sensitive keys.
    self.assertTrue(is_sensitive_key("db/password"))
    self.assertEqual(
        scrub_sensitive_data({"db/password": "slash_sec"})["db/password"],
        "[REDACTED]",
    )
    sq_slash = scrub_sensitive_text("{'db/password': 'sq_slash_sec'}")
    self.assertNotIn("sq_slash_sec", sq_slash)
    self.assertIn("[REDACTED]", sq_slash)

    # Next-line consecutive sensitive KV vs bare value vs blank-line boundary.
    self.assertEqual(
        scrub_sensitive_text("password:\ntoken: sec_colon_nextline"),
        "password:\ntoken: [REDACTED]",
    )
    db_url_out = scrub_sensitive_text("database_url:\n  postgres://u:p@host")
    self.assertNotIn("postgres://u:p@host", db_url_out)
    self.assertEqual(db_url_out, "database_url:\n  [REDACTED]")
    user_pw_out = scrub_sensitive_text("password:\n  user:pass123")
    self.assertNotIn("pass123", user_pw_out)
    self.assertEqual(user_pw_out, "password:\n  [REDACTED]")
    self.assertEqual(
        scrub_sensitive_text("password:\n\nERROR: CARD_EXPIRED"),
        "password:\n\nERROR: CARD_EXPIRED",
    )

    # Long / leading-zero numeric cookie IDs and non-finite floats redact
    # while short numeric counts stay preserved.
    cookie_id_out = scrub_sensitive_text('cookies="00481923819283"')
    self.assertNotIn("00481923819283", cookie_id_out)
    self.assertIn("[REDACTED]", cookie_id_out)
    long_cookie_out = scrub_sensitive_text('cookies="9876543210"')
    self.assertNotIn("9876543210", long_cookie_out)
    self.assertIn("[REDACTED]", long_cookie_out)
    self.assertEqual(
        scrub_sensitive_data({"cookies": "00481923819283"}),
        {"cookies": "[REDACTED]"},
    )
    self.assertEqual(
        scrub_sensitive_data({"cookies": "9876543210"}),
        {"cookies": "[REDACTED]"},
    )
    self.assertEqual(
        scrub_sensitive_data({"cookies": float("nan")}),
        {"cookies": "[REDACTED]"},
    )
    self.assertEqual(
        scrub_sensitive_data({"cookies": float("inf")}),
        {"cookies": "[REDACTED]"},
    )
    self.assertEqual(scrub_sensitive_text("cookies=3"), "cookies=3")
    self.assertEqual(scrub_sensitive_data({"cookies": 3}), {"cookies": 3})
    self.assertEqual(scrub_sensitive_data({"cookies": "3"}), {"cookies": "3"})
    self.assertEqual(scrub_sensitive_data({"cookies": "0"}), {"cookies": "0"})
    self.assertEqual(
        scrub_sensitive_data({"cookies": "120"}), {"cookies": "120"}
    )

  def test_json_keys_arrays_tight_counts_auth_result_s3_bytes_and_cookies(
      self,
  ) -> None:
    from analytics.trajectory_extractor import _flatten_structured_value  # noqa: PLC0415
    from analytics.trajectory_extractor import extract_s3_outcome_first  # noqa: PLC0415
    from analytics.trajectory_extractor import scrub_sensitive_data  # noqa: PLC0415
    from analytics.trajectory_extractor import scrub_sensitive_text  # noqa: PLC0415

    # 1. Full RFC 8259 JSON keys, string arrays, and double-encoded backslash
    #    escapes across budget/size tails.
    pad_17k = "a" * 17_000
    long_300_key = ("a" * 300) + "_password"
    tail_cases = (
        (
            "colon_key_over_16k",
            f'{{"body": "{pad_17k}", "request:password": "v_colon"}}'
            " ERROR: CARD_EXPIRED",
            ("v_colon",),
        ),
        (
            "after_33_objs",
            ('{"k":1} ' * 33)
            + '{"request:password": "v_after32"} '
            + '{"auth_result": {"value": "v_ar32"}} ERROR: CARD_EXPIRED',
            ("v_after32", "v_ar32"),
        ),
        (
            "escaped_slash_over_16k",
            f'{{"body": "{pad_17k}", "db\\/password": "v_escslash"}}'
            " ERROR: CARD_EXPIRED",
            ("v_escslash",),
        ),
        (
            "long_300_key_over_16k",
            f'{{"body": "{pad_17k}", "{long_300_key}": "v_long300"}}'
            " ERROR: CARD_EXPIRED",
            ("v_long300",),
        ),
        (
            "dollar_and_digit_keys_over_16k",
            f'{{"body": "{pad_17k}", "$secret": "v_dollar",'
            ' "0_password": "v_zero"} ERROR: CARD_EXPIRED',
            ("v_dollar", "v_zero"),
        ),
        (
            "string_array_tab",
            '["password:\\t v_arrtab"] ERROR: CARD_EXPIRED',
            ("v_arrtab",),
        ),
        (
            "string_array_tab_over_16k",
            f'["{pad_17k}", "password:\\t v_arrtab_big"] ERROR: CARD_EXPIRED',
            ("v_arrtab_big",),
        ),
        (
            "string_array_literal_tab_over_16k",
            f'["{pad_17k}", "password:\t v_arr_littab"] ERROR: CARD_EXPIRED',
            ("v_arr_littab",),
        ),
        (
            "double_encoded_trailing_backslash",
            r"\"password\": \"v_bs5\\\\\" ERROR: CARD_EXPIRED",
            ("v_bs5",),
        ),
    )
    for label, sample, secrets in tail_cases:
      with self.subTest(tail_case=label):
        out = scrub_sensitive_text(sample)
        for sec in secrets:
          self.assertNotIn(sec, out)
        self.assertIn("[REDACTED]", out)
        self.assertIn("CARD_EXPIRED", out)

    # 2. Tight small-count allowlist (0..999) and authentication_result status.
    for text_case, sec in (
        ("password_count=234567", "234567"),
        ("password_count=12", "12"),
        ("credential_count=123456", "123456"),
        ("cookies=456789", "456789"),
        ("cookies=1000", "1000"),
        ("cookies=003", "003"),
        ("authentication_result=opaque_tok_9f8a7b6c", "opaque_tok_9f8a7b6c"),
    ):
      with self.subTest(redacted_text=text_case):
        out = scrub_sensitive_text(text_case)
        self.assertNotIn(sec, out)
        self.assertIn("[REDACTED]", out)

    for key_name, bad_val in (
        ("password_count", 234567),
        ("password_count", 12),
        ("credential_count", 123456),
        ("cookies", 456789),
        ("cookies", 123456789012345),
        ("cookies", 1000),
        ("cookies", -1),
        ("cookies", 3.5),
        ("cookies", "003"),
        ("authentication_result", "opaque_tok_9f8a7b6c"),
        ("authentication_result", {"value": "x"}),
    ):
      with self.subTest(redacted_dict=(key_name, bad_val)):
        self.assertEqual(
            scrub_sensitive_data({key_name: bad_val}),
            {key_name: "[REDACTED]"},
        )

    for key_name, ok_val in (
        ("cookies", 0),
        ("cookies", 3),
        ("cookies", 999),
        ("credential_count", 3),
        ("authentication_result", "FAILED"),
    ):
      self.assertEqual(
          scrub_sensitive_data({key_name: ok_val}), {key_name: ok_val}
      )
    self.assertEqual(scrub_sensitive_text("cookies=3"), "cookies=3")
    self.assertEqual(
        scrub_sensitive_text("credential_count=3"), "credential_count=3"
    )
    self.assertEqual(
        scrub_sensitive_text("authentication_result=failed"),
        "authentication_result=failed",
    )

    # 3. Next-line dotted and quoted KV headers.
    self.assertEqual(
        scrub_sensitive_text("authentication:\n  request.password: v_dot_nl"),
        "authentication:\n  request.password: [REDACTED]",
    )
    self.assertEqual(
        scrub_sensitive_text('password:\n"token": v_q_nl'),
        'password:\n"token": [REDACTED]',
    )
    self.assertEqual(
        scrub_sensitive_text('password:\n"my quoted secret"'),
        'password:\n"[REDACTED]"',
    )

    # 4. S3 flattener preserves safe scalars while redacting sensitive values.
    flat_ok = _flatten_structured_value(
        {
            "cookies": 3,
            "credential_count": 3,
            "authentication": "failed",
            "authentication_result": "FAILED",
        }
    )
    self.assertIn("cookies=3", flat_ok)
    self.assertIn("credential_count=3", flat_ok)
    self.assertIn("authentication=failed", flat_ok)
    self.assertIn("authentication_result=FAILED", flat_ok)

    s3_out = extract_s3_outcome_first(
        {
            "turns": [
                {
                    "events": [
                        {
                            "author": "customer_ops_agent",
                            "content": {
                                "role": "model",
                                "parts": [
                                    {
                                        "function_response": {
                                            "name": "auth_check",
                                            "response": {
                                                "cookies": 3,
                                                "credential_count": 3,
                                                "authentication": "failed",
                                                "authentication_result": (
                                                    "FAILED"
                                                ),
                                            },
                                        }
                                    },
                                    {
                                        "function_response": {
                                            "name": "auth_leak_check",
                                            "response": {
                                                "cookies": {
                                                    "sessionid": "s3_ck_sec"
                                                },
                                                "authentication_result": (
                                                    "s3_opaque_tok"
                                                ),
                                            },
                                        }
                                    },
                                ],
                            },
                        }
                    ]
                }
            ]
        }
    )
    self.assertIn("cookies=3", s3_out)
    self.assertIn("credential_count=3", s3_out)
    self.assertIn("authentication=failed", s3_out)
    self.assertIn("authentication_result=FAILED", s3_out)
    self.assertNotIn("s3_ck_sec", s3_out)
    self.assertNotIn("s3_opaque_tok", s3_out)

    # 5. Bytes count & auth-status scalars decode to str for JSON serialization.
    scrubbed_bytes = scrub_sensitive_data(
        {
            "cookies": b"3",
            "credential_count": b"5",
            "authentication": b"failed",
            "authentication_result": b"FAILED",
        }
    )
    serialized = json.dumps(scrubbed_bytes)
    self.assertEqual(
        json.loads(serialized),
        {
            "cookies": "3",
            "credential_count": "5",
            "authentication": "failed",
            "authentication_result": "FAILED",
        },
    )

    # 6. Free-text Cookie / Set-Cookie header masking.
    self.assertEqual(
        scrub_sensitive_text("Cookie: a=1; b=2"), "Cookie: [REDACTED]"
    )
    self.assertEqual(
        scrub_sensitive_text("Cookie: a=1; b=2 CARD_EXPIRED"),
        "Cookie: [REDACTED] CARD_EXPIRED",
    )
    self.assertEqual(
        scrub_sensitive_text("Set-Cookie: sid=abc; Path=/; HttpOnly"),
        "Set-Cookie: [REDACTED]",
    )
    self.assertEqual(scrub_sensitive_text("cookies: 3"), "cookies: 3")

  def test_token_aware_kv_scanner_perf_int_guard_and_cookie_headers(
      self,
  ) -> None:
    from analytics.trajectory_extractor import scrub_sensitive_data  # noqa: PLC0415
    from analytics.trajectory_extractor import scrub_sensitive_text  # noqa: PLC0415

    # 1. Attribute access after brackets/parens or leading dot redacts cleanly.
    for sample, secret_val in (
        ("obj[0].password=supersecret", "supersecret"),
        ("get_auth().token=my_secret", "my_secret"),
        (".password=supersecret", "supersecret"),
    ):
      with self.subTest(dot_access=sample):
        out = scrub_sensitive_text(sample)
        self.assertNotIn(secret_val, out)
        self.assertIn("[REDACTED]", out)

    # 2. Quoted tokens with embedded KV or natural-language connectors.
    for sample, secret_val in (
        ('Field "password=my_secret_123" is invalid', "my_secret_123"),
        ('Check "password=my_secret_123": failed', "my_secret_123"),
        ("Error 'token=my_secret_123' is expired", "my_secret_123"),
        ('password:\n"secret_val" is bad', "secret_val"),
        (
            '"{\\"msg\\": \\"hello\\", \\"password\\": \\"my_real_pw_123\\"}": 1',
            "my_real_pw_123",
        ),
    ):
      with self.subTest(quoted_kv=sample):
        out = scrub_sensitive_text(sample)
        self.assertNotIn(secret_val, out)
        self.assertIn("[REDACTED]", out)

    # 3. Oversized digit strings on numeric-count keys never raise ValueError.
    digits_5000 = "1" * 5000
    self.assertEqual(
        scrub_sensitive_data({"cookies": digits_5000}),
        {"cookies": "[REDACTED]"},
    )
    out_digits = scrub_sensitive_text("cookies=" + digits_5000)
    self.assertNotIn(digits_5000, out_digits)
    self.assertEqual(out_digits, "cookies=[REDACTED]")

    # 4. Pathological quote/escape sequences run strictly in O(N) (< 1.0s).
    perf_cases = (
        ("edq_50k", r"\"" * 50_000, None),
        ("esq_50k", r"\'" * 50_000, None),
        ("unclosed_dq_5k_edq", '"hello ' + (r"\"a\" " * 5_000), None),
        ("edq_5k_with_tail", ('\\"a\\" ' * 5_000) + " password=END_S", "END_S"),
        ("edq_bs2_5k", '\\"a\\\\ ' * 5_000, None),
        ("edq_bs3_5k", r"\"" + (r"a\\\"" * 5_000), None),
    )
    for label, payload, secret_val in perf_cases:
      with self.subTest(perf_case=label):
        out, elapsed = _timed_scrub_median_sec(scrub_sensitive_text, payload)
        self.assertLess(elapsed, 5.0, f"{label} took {elapsed:.3f}s")
        if secret_val is not None:
          self.assertNotIn(secret_val, out)
          self.assertIn("[REDACTED]", out)

    # 5. Post-budget cross-field quotes, apostrophes, escaped inner quotes,
    #    and blank-line JSON values.
    pad_17k = "a" * 17_000
    post_budget_cases = (
        (
            "missing_pw_over_16k",
            f'{{"body": "{pad_17k}", "note": "Missing password:",'
            ' "api_key": "REAL_SECRET_LEAK"}',
            "REAL_SECRET_LEAK",
        ),
        (
            "missing_pw_after_33_objs",
            ('{"k":1} ' * 33)
            + '{"note": "Missing password:", "api_key": "REAL_SECRET_LEAK"}',
            "REAL_SECRET_LEAK",
        ),
        (
            "apostrophe_over_16k",
            f'{{"body": "{pad_17k}", "msg": "don\'t", "token": "abc\':def"}}',
            "abc':def",
        ),
        (
            "escaped_inner_quote_key",
            r"\"auth\\\"_result\": {\"v\": \"my_secret\"}",
            "my_secret",
        ),
        (
            "nl2_quoted_tail_over_16k",
            f'{{"body": "{pad_17k}", "password":\n\n"nl2_tail_secret"}}',
            "nl2_tail_secret",
        ),
    )
    for label, sample, secret_val in post_budget_cases:
      with self.subTest(post_budget=label):
        out = scrub_sensitive_text(sample)
        self.assertNotIn(secret_val, out)
        self.assertIn("[REDACTED]", out)

    # 6. Quoted and comma-containing Cookie headers.
    for sample, secrets in (
        ('Cookie: a="foo"; session=secret_val', ("foo", "secret_val")),
        ('Cookie: a="secret_1"; b=secret_2', ("secret_1", "secret_2")),
        ("Cookie: prefs=1,2; session=secret_val", ("1,2", "secret_val")),
        ('{"msg": "Cookie: a=1; b=2"}', ("a=1", "b=2")),
    ):
      with self.subTest(cookie_hdr=sample):
        out = scrub_sensitive_text(sample)
        for sec in secrets:
          self.assertNotIn(sec, out)
        self.assertIn("Cookie: [REDACTED]", out)
    self.assertEqual(
        scrub_sensitive_text("Cookie: a=1; b=2 CARD_EXPIRED"),
        "Cookie: [REDACTED] CARD_EXPIRED",
    )
    self.assertEqual(scrub_sensitive_text("cookies: 3"), "cookies: 3")

  def test_dotted_path_perf_json_escapes_quoted_inner_kv_cookies_and_multiline(
      self,
  ) -> None:
    from analytics.trajectory_extractor import scrub_sensitive_text  # noqa: PLC0415

    pad_17k = "a" * 17_000

    # 1. Dotted-path O(N) perf with trailing secret redaction.
    dotted_out, elapsed = _timed_scrub_median_sec(
        scrub_sensitive_text, ("a." * 25_000) + " password=END_S"
    )
    self.assertLess(elapsed, 5.0, f"dotted_path took {elapsed:.3f}s")
    self.assertNotIn("END_S", dotted_out)
    self.assertTrue(dotted_out.endswith(" password=[REDACTED]"))

    # 2. Post-budget JSON \t/\n escapes before anchor-bounded bare keys.
    for label, sample, secret_val in (
        (
            "json_tab_token",
            f'["{pad_17k}", "\\ttoken: secret_val_t"]',
            "secret_val_t",
        ),
        (
            "json_nl_token",
            f'["{pad_17k}", "Error:\\ntoken: secret_val_n"]',
            "secret_val_n",
        ),
        (
            "json_nl_auth",
            f'["{pad_17k}", "Error:\\nauth: secret_val_a"]',
            "secret_val_a",
        ),
        (
            "even_bs_token",
            r"foo\\token: secret_val_bs",
            "secret_val_bs",
        ),
    ):
      with self.subTest(json_escape_key=label):
        out = scrub_sensitive_text(sample)
        self.assertNotIn(secret_val, out)
        self.assertIn("[REDACTED]", out)
    # Even-backslash literal "\\ntoken" is not a \n escape before "token".
    self.assertEqual(
        scrub_sensitive_text(r"foo\\ntoken: benign_val"),
        r"foo\\ntoken: benign_val",
    )

    # 3. Quoted keys with inner KV and sensitive outer values.
    for label, sample, secret_val in (
        (
            "colon_space_key",
            f'{{"body":"{pad_17k}","request: password":"SECRET_1"}}',
            "SECRET_1",
        ),
        (
            "eq_key",
            f'{{"body":"{pad_17k}","user=password":"SECRET_2"}}',
            "SECRET_2",
        ),
        (
            "inner_kv_and_outer_val",
            f'{{"body":"{pad_17k}","password=1":"SECRET_3"}}',
            "SECRET_3",
        ),
        (
            "escaped_quote_colon_key",
            f'{{"body":"{pad_17k}","req\\"uest:password":"SECRET_4"}}',
            "SECRET_4",
        ),
        (
            "nextline_quoted_colon_key",
            'password:\n"request: token": SECRET_4B',
            "SECRET_4B",
        ),
    ):
      with self.subTest(quoted_outer=label):
        out = scrub_sensitive_text(sample)
        self.assertNotIn(secret_val, out)
        self.assertIn("[REDACTED]", out)

    for label, sample in (
        ("colon_nospace_plain", 'Check "password:my_secret_123": failed'),
        ("colon_nospace_esc", r"Check \"password:my_secret_123\": failed"),
        ("eq_plain", 'Check "password=my_secret_123": failed'),
    ):
      with self.subTest(quoted_inner_only=label):
        out = scrub_sensitive_text(sample)
        self.assertNotIn("my_secret_123", out)
        self.assertIn("[REDACTED]", out)
        self.assertIn(": failed", out)

    # 4. Hardened Cookie headers (empty value/name, single & escaped quotes).
    for label, sample, secrets in (
        ("empty_val", "Cookie: a=; b=SECRET_5", ("SECRET_5",)),
        (
            "empty_name",
            "Cookie: =SECRET_6; b=SECRET_7",
            ("SECRET_6", "SECRET_7"),
        ),
        (
            "single_quoted",
            "Cookie: a='foo'; session=sq_ck_sec",
            ("foo", "sq_ck_sec"),
        ),
        (
            "json_escaped_dq",
            json.dumps(
                {"body": pad_17k, "msg": 'Cookie: a="secret_1"; b=secret_2'}
            ),
            ("secret_1", "secret_2"),
        ),
        (
            "json_triple_escaped_dq",
            json.dumps(
                {"body": pad_17k, "msg": r"Cookie: a=\"foo\"; b=SECRET_8"}
            ),
            ("foo", "SECRET_8"),
        ),
    ):
      with self.subTest(cookie_hardened=label):
        out = scrub_sensitive_text(sample)
        for sec in secrets:
          self.assertNotIn(sec, out)
        self.assertIn("Cookie: [REDACTED]", out)

    # Pathological unclosed escaped-quote backslash run in Cookie stays O(N).
    _, elapsed = _timed_scrub_median_sec(
        scrub_sensitive_text, 'Cookie: a=\\"' + ("\\" * 20_000)
    )
    self.assertLess(elapsed, 5.0, f"cookie_backslash took {elapsed:.3f}s")

    # 5. RFC 8259 multiline / escaped-newline JSON connector before ":".
    for label, sample, secret_val in (
        (
            "literal_nl_conn",
            f'{{"body":"{pad_17k}","password"\n: "SECRET_9"}}',
            "SECRET_9",
        ),
        (
            "escaped_nl_conn",
            f'{{"body":"{pad_17k}","password"\\n: "SECRET_10"}}',
            "SECRET_10",
        ),
    ):
      with self.subTest(multiline_conn=label):
        out = scrub_sensitive_text(sample)
        self.assertNotIn(secret_val, out)
        self.assertIn("[REDACTED]", out)

  def test_dotted_keys_even_backslash_cookie_scanner_and_post_budget_json(
      self,
  ) -> None:
    from analytics.trajectory_extractor import scrub_sensitive_text  # noqa: PLC0415

    pad_17k = "a" * 17_000

    # 1. Dotted .token/.auth and digit-prefixed dotted keys (+ benign forms).
    for label, sample, secret_val in (
        ("user_token", "user.token=SECRET_UT", "SECRET_UT"),
        ("self_token", "self.token=SECRET", "SECRET"),
        ("client_auth", "client.auth=SECRET_CA", "SECRET_CA"),
        (
            "multiline_user_token",
            "password:\n  user.token: SECRET_NL_UT",
            "SECRET_NL_UT",
        ),
        ("digit_password", "1.password=SECRET_1PW", "SECRET_1PW"),
        ("zero_api_key", "0.api_key=SECRET", "SECRET"),
        ("ip_token", "127.0.0.1.token=SECRET_IP", "SECRET_IP"),
    ):
      with self.subTest(dotted_secret=label):
        out = scrub_sensitive_text(sample)
        self.assertNotIn(secret_val, out)
        self.assertIn("[REDACTED]", out)

    for benign in (
        "usage.prompt.token=412",
        "prompt.token=412",
        "resp.auth_status=failed",
    ):
      with self.subTest(dotted_benign=benign):
        self.assertEqual(scrub_sensitive_text(benign), benign)

    # 2. O(N) perf (< 1.0s) for digit-dotted runs and malformed Cookie escapes.
    for label, payload, secret_val in (
        ("digit_dotted_25k", ("1." * 25_000) + " password=END_S", "END_S"),
        (
            "cookie_edq_10k",
            'Cookie: a=\\"' + (r"\"" * 10_000) + "; b=END_CK",
            "END_CK",
        ),
        ("cookie_bs_20k", 'Cookie: a=\\"' + ("\\" * 20_000), None),
    ):
      with self.subTest(perf=label):
        out, elapsed = _timed_scrub_median_sec(scrub_sensitive_text, payload)
        self.assertLess(elapsed, 5.0, f"{label} took {elapsed:.3f}s")
        if secret_val is not None:
          self.assertNotIn(secret_val, out)
          self.assertIn("[REDACTED]", out)

    # 3. Even-backslash \\" before quoted keys and unclosed JSON string values.
    bs2_out = scrub_sensitive_text(r'log: \"x\\" \"password\": \"SECRET_BS2\"')
    self.assertNotIn("SECRET_BS2", bs2_out)
    self.assertIn("[REDACTED]", bs2_out)

    for label, sample in (
        (
            "short_json",
            '{"msg": "password: \\"unclosed\\\\", "error_code": "CARD_EXPIRED"}',
        ),
        (
            "post_budget_json",
            f'{{"body": "{pad_17k}", "msg": "password: \\"unclosed\\\\",'
            ' "error_code": "CARD_EXPIRED"}',
        ),
        (
            "multiline_post_budget_json",
            f'{{"body": "{pad_17k}", "msg": "err\npassword: \\"unclosed\\\\",'
            ' "error_code": "CARD_EXPIRED"}',
        ),
    ):
      with self.subTest(even_bs_unclosed=label):
        out = scrub_sensitive_text(sample)
        self.assertNotIn("unclosed", out)
        self.assertIn("[REDACTED]", out)
        self.assertIn("CARD_EXPIRED", out)

    # 4. Malformed and inner-escaped Cookie headers.
    for label, sample, secrets in (
        (
            "unclosed_single",
            'Cookie: session="unclosed_secret_1',
            ("unclosed_secret_1",),
        ),
        (
            "unclosed_second",
            'Cookie: a=1; b="unclosed_secret_2',
            ("unclosed_secret_2",),
        ),
        (
            "json_inner_escaped_dq",
            json.dumps(
                {"body": pad_17k, "msg": r'Cookie: a="x\"y"; b=SECRET_EQ'}
            ),
            ("SECRET_EQ", r"x\\\"y"),
        ),
    ):
      with self.subTest(cookie_edge=label):
        out = scrub_sensitive_text(sample)
        for sec in secrets:
          self.assertNotIn(sec, out)
        self.assertIn("Cookie: [REDACTED]", out)

    # 5. Post-budget JSON is/are escapes and JSON object-key numeric scalar.
    for label, sample, secret_val in (
        (
            "json_is_nl",
            f'["{pad_17k}", "password is\\nsecret"]',
            "secret",
        ),
        (
            "json_tab_is_colon",
            f'["{pad_17k}", "password\\tis: secret"]',
            "secret",
        ),
        (
            "json_obj_key_numeric",
            f'{{"body": "{pad_17k}", "password=1": 234567}}',
            "234567",
        ),
    ):
      with self.subTest(post_budget_r9=label):
        out = scrub_sensitive_text(sample)
        self.assertNotIn(secret_val, out)
        self.assertIn("[REDACTED]", out)

    for sample, secret_val, suffix in (
        ('Check "password=my_secret_123": failed', "my_secret_123", ": failed"),
        ('Check "auth=123": failed', "123", ": failed"),
        ('Error "user: token=abc": timed_out', "abc", ": timed_out"),
    ):
      with self.subTest(preserve_suffix=sample):
        out = scrub_sensitive_text(sample)
        self.assertNotIn(secret_val, out)
        self.assertIn("[REDACTED]", out)
        self.assertTrue(out.endswith(suffix))

  def test_json_escape_boundaries_cookie_routing_dotted_keys_and_stray_quotes(
      self,
  ) -> None:
    from analytics.trajectory_extractor import is_sensitive_key  # noqa: PLC0415
    from analytics.trajectory_extractor import scrub_sensitive_data  # noqa: PLC0415
    from analytics.trajectory_extractor import scrub_sensitive_text  # noqa: PLC0415

    pad_17k = "a" * 17_000

    # 1. Bare KV values and Cookie headers stop before JSON-escaped \t/\r/\n.
    for label, sample, secrets, must_keep in (
        (
            "bare_nl_next_kv",
            json.dumps(
                {"body": pad_17k, "msg": "password: SECRET_1\ntoken: SECRET_2"}
            ),
            ("SECRET_1", "SECRET_2"),
            (),
        ),
        (
            "bare_tab_next_kv",
            json.dumps(
                {
                    "body": pad_17k,
                    "msg": "password: SECRET_T1\ttoken: SECRET_T2",
                }
            ),
            ("SECRET_T1", "SECRET_T2"),
            (),
        ),
        (
            "cookie_nl_next_kv",
            json.dumps(
                {
                    "body": pad_17k,
                    "msg": "Cookie: a=1; b=2\ntoken: SECRET_CK",
                }
            ),
            ("a=1", "b=2", "SECRET_CK"),
            (),
        ),
        (
            "bare_nl_preserve_error",
            json.dumps(
                {"body": pad_17k, "msg": "password: SECRET_1\nCARD_EXPIRED"}
            ),
            ("SECRET_1",),
            ("CARD_EXPIRED",),
        ),
    ):
      with self.subTest(json_esc_stop=label):
        out = scrub_sensitive_text(sample)
        for sec in secrets:
          self.assertNotIn(sec, out)
        for keep in must_keep:
          self.assertIn(keep, out)
        self.assertIn("[REDACTED]", out)

    # 2. Cookie header routing, semicolons, escaped quotes, Expires, and commas.
    self.assertEqual(
        scrub_sensitive_text('{"cookies": 3, "status": "ok"}'),
        '{"cookies": 3, "status": "ok"}',
    )
    post_budget_cookies = json.dumps(
        {"body": pad_17k, "cookies": 3, "status": "ok"}
    )
    self.assertIn(
        '"cookies": 3, "status": "ok"',
        scrub_sensitive_text(post_budget_cookies),
    )
    self.assertEqual(
        scrub_sensitive_text("cookies: 3,token: SECRET_VAL"),
        "cookies: 3,token: [REDACTED]",
    )
    self.assertEqual(scrub_sensitive_text("Cookie: ;;;"), "Cookie: ;;;")

    for label, sample, secrets, must_keep in (
        (
            "cookie_comma_kv",
            "Cookie: sid=abc,token: SECRET_VAL",
            ("sid=abc", "SECRET_VAL"),
            (),
        ),
        (
            "cookie_leading_semi",
            "Cookie: ; b=SECRET_LSEMI",
            ("SECRET_LSEMI",),
            (),
        ),
        (
            "cookie_double_semi",
            "Cookie: a=1;; b=SECRET_DSEMI",
            ("SECRET_DSEMI",),
            (),
        ),
        (
            "cookie_escaped_sq_raw",
            r"Cookie: a=\'foo\'; b=SECRET_SQ\nstatus=200",
            ("foo", "SECRET_SQ"),
            ("status=200",),
        ),
        (
            "cookie_escaped_sq_nl",
            "Cookie: a=\\'foo\\'; b=SECRET_SQ\nstatus=200",
            ("foo", "SECRET_SQ"),
            ("status=200",),
        ),
        (
            "cookie_json_esc_sq_17k",
            json.dumps(
                {
                    "body": pad_17k,
                    "msg": r"Cookie: a=\'foo\'; b=SECRET_JSQ",
                    "error_code": "CARD_EXPIRED",
                }
            ),
            ("SECRET_JSQ",),
            ("CARD_EXPIRED",),
        ),
        (
            "cookie_json_esc_sq_unclosed_17k",
            json.dumps(
                {
                    "body": pad_17k,
                    "msg": r"Cookie: a=\'unclosed_sq",
                    "error_code": "CARD_EXPIRED",
                }
            ),
            ("unclosed_sq",),
            ("CARD_EXPIRED",),
        ),
        (
            "cookie_py_dict_trailing_even_bs",
            r"{'msg': 'Cookie: a=\\', 'error_code': 'CARD_EXPIRED'}",
            ("a=\\\\",),
            ("'error_code': 'CARD_EXPIRED'",),
        ),
        (
            "cookie_py_dict_trailing_single_bs",
            "{'msg': 'Cookie: a=\\', 'error_code': 'CARD_EXPIRED'}",
            (),
            ("'error_code': 'CARD_EXPIRED'",),
        ),
        (
            "cookie_url_colon",
            "Cookie: next=https://a.com/x; sid=SECRET_URL",
            ("https://a.com/x", "SECRET_URL"),
            (),
        ),
        (
            "set_cookie_expires_then_sid",
            (
                "Set-Cookie: a=1; Expires=Wed, 21 Oct 2015 07:28:00 GMT;"
                " sid=SECRET_AFTER_EXP"
            ),
            ("a=1", "SECRET_AFTER_EXP"),
            (),
        ),
        (
            "set_cookie_expires_preserve_err",
            (
                "Set-Cookie: a=1; Expires=Wed, 21 Oct 2015 07:28:00 GMT"
                " CARD_EXPIRED"
            ),
            ("a=1",),
            ("CARD_EXPIRED",),
        ),
        (
            "cookie_comma_value",
            "Cookie: prefs=1,2; session=secret_val",
            ("1,2", "secret_val"),
            (),
        ),
    ):
      with self.subTest(cookie_fix=label):
        out = scrub_sensitive_text(sample)
        for sec in secrets:
          self.assertNotIn(sec, out)
        for keep in must_keep:
          self.assertIn(keep, out)
        self.assertIn("[REDACTED]", out)

    semi_out, elapsed = _timed_scrub_median_sec(
        scrub_sensitive_text, "Cookie: " + (";" * 50_000) + " b=END_CK4"
    )
    self.assertLess(elapsed, 5.0, f"cookie_semicolon took {elapsed:.3f}s")
    self.assertNotIn("END_CK4", semi_out)
    self.assertIn("[REDACTED]", semi_out)

    comma_out, elapsed = _timed_scrub_median_sec(
        scrub_sensitive_text, "Cookie: a=1" + (",x" * 20_000)
    )
    self.assertLess(elapsed, 5.0, f"cookie_comma took {elapsed:.3f}s")
    self.assertEqual(comma_out, "Cookie: [REDACTED]")

    # 3. Shared dotted-segment matching in is_sensitive_key (dict & text).
    self.assertTrue(is_sensitive_key("user.token"))
    self.assertTrue(is_sensitive_key("127.0.0.1.token"))
    self.assertFalse(is_sensitive_key("usage.prompt.token"))
    self.assertFalse(is_sensitive_key("prompt.token"))
    self.assertFalse(is_sensitive_key("resp.auth_status"))

    self.assertEqual(
        scrub_sensitive_data({"user.token": "SECRET"}),
        {"user.token": "[REDACTED]"},
    )
    self.assertEqual(
        scrub_sensitive_data({"127.0.0.1.token": "SECRET"}),
        {"127.0.0.1.token": "[REDACTED]"},
    )
    self.assertEqual(
        scrub_sensitive_data(
            {
                "usage.prompt.token": 412,
                "prompt.token": 18,
                "resp.auth_status": "failed",
                "token_count": 1500,
                "token_type": "bearer",
            }
        ),
        {
            "usage.prompt.token": 412,
            "prompt.token": 18,
            "resp.auth_status": "failed",
            "token_count": 1500,
            "token_type": "bearer",
        },
    )
    for label, sample in (
        ("json_user_token", '{"user.token": "SECRET"}'),
        ("quoted_user_token", '"user.token": "SECRET"'),
        (
            "post_budget_user_token",
            json.dumps({"body": pad_17k, "user.token": "SECRET"}),
        ),
    ):
      with self.subTest(dotted_text=label):
        out = scrub_sensitive_text(sample)
        self.assertNotIn("SECRET", out)
        self.assertIn("[REDACTED]", out)

    # 4. Unmatched same-line quotes & \" do not hijack following quoted KV keys.
    for label, sample, secret_val in (
        (
            "stray_plain_dq",
            'log: "oops "password": "SECRET_STRAY_Q"',
            "SECRET_STRAY_Q",
        ),
        (
            "stray_json_3bs",
            json.dumps(
                {"body": pad_17k, "msg": 'x\\" "password": "SECRET_3BS"'}
            ),
            "SECRET_3BS",
        ),
        (
            "stray_json_4bs",
            json.dumps(
                {"body": pad_17k, "msg": 'x\\\\" "password": "SECRET_4BS"'}
            ),
            "SECRET_4BS",
        ),
        (
            "stray_raw_2bs",
            'x\\\\" "password": "SECRET_4BS_RAW"',
            "SECRET_4BS_RAW",
        ),
    ):
      with self.subTest(stray_quote=label):
        out = scrub_sensitive_text(sample)
        self.assertNotIn(secret_val, out)
        self.assertIn("[REDACTED]", out)

    # 5. Indented post-budget JSON & start-of-line / next-line quoted KV keys.
    for label, sample, secret_val in (
        (
            "indented_post_budget_json",
            json.dumps(
                {
                    "body": pad_17k,
                    "msg": json.dumps({"password=1": 234567}, indent=2),
                }
            ),
            "234567",
        ),
        (
            "yaml_newline_quoted_key",
            'a: 1\n"password=1": SECRET_YAML_BARE',
            "SECRET_YAML_BARE",
        ),
        (
            "start_of_text_quoted_key",
            '"password=1": SECRET_TOP_BARE',
            "SECRET_TOP_BARE",
        ),
        (
            "nextline_quoted_password_eq",
            'password:\n"password=1": SECRET_OUTER',
            "SECRET_OUTER",
        ),
        (
            "nextline_quoted_token_eq",
            'password:\n"token=1": SECRET_NL_TK',
            "SECRET_NL_TK",
        ),
    ):
      with self.subTest(quoted_boundary=label):
        out = scrub_sensitive_text(sample)
        self.assertNotIn(secret_val, out)
        self.assertIn("[REDACTED]", out)

    for sample, secret_val, suffix in (
        ('Check "password=my_secret_123": failed', "my_secret_123", ": failed"),
        ('Check "auth=123": failed', "123", ": failed"),
        ('Error "user: token=abc": timed_out', "abc", ": timed_out"),
    ):
      with self.subTest(control_suffix=sample):
        out = scrub_sensitive_text(sample)
        self.assertNotIn(secret_val, out)
        self.assertIn("[REDACTED]", out)
        self.assertTrue(out.endswith(suffix))

  def test_quote_hijack_guards_quoted_is_are_3bs_cookies_and_hot_path_perf(
      self,
  ) -> None:
    from analytics.trajectory_extractor import scrub_sensitive_data  # noqa: PLC0415
    from analytics.trajectory_extractor import scrub_sensitive_text  # noqa: PLC0415

    pad_17k = "a" * 17_000

    # 1. Empty/stray-quote hijack guards.
    for label, sample, secret_val in (
        (
            "json_pw_stray_dq",
            json.dumps(
                {"body": pad_17k, "msg": 'password=", "token": "SECRET_LEAK"'}
            ),
            "SECRET_LEAK",
        ),
        (
            "json_cookie_stray_dq",
            json.dumps(
                {"body": pad_17k, "msg": 'Cookie: a=", "token": "SECRET_LEAK"'}
            ),
            "SECRET_LEAK",
        ),
        (
            "multiline_pw_stray_dq",
            'note: "Error\nMissing password:", "api_key": "REAL_SECRET_LEAK"',
            "REAL_SECRET_LEAK",
        ),
        (
            "multiline_cookie_stray_dq",
            'note: "Error\nCookie: a=", "api_key": "REAL_SECRET_LEAK"',
            "REAL_SECRET_LEAK",
        ),
        (
            "multiline_cookie_py_dict_bs",
            "{'msg': 'Error\nCookie: a=\\\\' , 'api_key': 'REAL_SECRET_LEAK'}",
            "REAL_SECRET_LEAK",
        ),
    ):
      with self.subTest(quote_hijack=label):
        out = scrub_sensitive_text(sample)
        self.assertNotIn(secret_val, out)
        self.assertIn("[REDACTED]", out)

    # 2. Quoted is/are connectors + benign control.
    for label, sample, secret_val in (
        ("quoted_dq_is", '"password" is supersecret', "supersecret"),
        (
            "quoted_sq_is_colon",
            "'api_key' is: my_api_secret_val",
            "my_api_secret_val",
        ),
        (
            "quoted_dq_are",
            '"credentials" are my_secret_creds',
            "my_secret_creds",
        ),
        (
            "escaped_dq_is",
            r"\"password\" is \"supersecret\"",
            "supersecret",
        ),
    ):
      with self.subTest(quoted_is_are=label):
        out = scrub_sensitive_text(sample)
        self.assertNotIn(secret_val, out)
        self.assertIn("[REDACTED]", out)

    benign_ctrl = scrub_sensitive_text(
        'Field "password=my_secret_123" is invalid'
    )
    self.assertNotIn("my_secret_123", benign_ctrl)
    self.assertIn("[REDACTED]", benign_ctrl)
    self.assertIn(" is invalid", benign_ctrl)

    # 3. Double/triple-escaped \\" (bs=3) KV tokens.
    for label, sample, secret_val in (
        (
            "json_esc_in_msg",
            json.dumps(
                {
                    "body": pad_17k,
                    "msg": r"\"password\": \"SECRET_ESC_IN_MSG\"",
                }
            ),
            "SECRET_ESC_IN_MSG",
        ),
        (
            "raw_3bs",
            r"\\\"password\\\": \\\"SECRET_RAW_3BS\\\"",
            "SECRET_RAW_3BS",
        ),
        (
            "triple_nested_json",
            json.dumps(
                {
                    "body": pad_17k,
                    "msg": json.dumps(
                        {"nested": json.dumps({"password": "SECRET_3X_JSON"})}
                    ),
                }
            ),
            "SECRET_3X_JSON",
        ),
    ):
      with self.subTest(triple_escaped_kv=label):
        out = scrub_sensitive_text(sample)
        self.assertNotIn(secret_val, out)
        self.assertIn("[REDACTED]", out)

    # 4. Unclosed cookie quotes with spaces + comma-folded Set-Cookie.
    for label, sample, secret_val, must_keep in (
        (
            "unclosed_dq_cookie_space",
            'Cookie: a="foo bar; b=SECRET_SPACE_UNCLOSED\nstatus=ok',
            "SECRET_SPACE_UNCLOSED",
            "status=ok",
        ),
        (
            "unclosed_sq_cookie_space",
            "Cookie: a='foo bar; b=SECRET_SQ_SPACE_UNCLOSED\nstatus=ok",
            "SECRET_SQ_SPACE_UNCLOSED",
            "status=ok",
        ),
        (
            "comma_folded_set_cookie",
            "Set-Cookie: a=1; Path=/, b=SECRET_FOLDED",
            "SECRET_FOLDED",
            "",
        ),
    ):
      with self.subTest(cookie_edge=label):
        out = scrub_sensitive_text(sample)
        self.assertNotIn(secret_val, out)
        self.assertIn("[REDACTED]", out)
        if must_keep:
          self.assertIn(must_keep, out)

    # 5. is_sensitive_key non-dot separators, password.prompt.token, and
    #    next-line multi-word header preservation.
    self.assertEqual(
        scrub_sensitive_data({"password.prompt.token": "SECRET_PPT"}),
        {"password.prompt.token": "[REDACTED]"},
    )
    self.assertEqual(
        scrub_sensitive_data({"user/token": "SECRET_SLASH"}),
        {"user/token": "[REDACTED]"},
    )
    self.assertEqual(
        scrub_sensitive_data({"user: token": "SECRET_COLON"}),
        {"user: token": "[REDACTED]"},
    )
    for label, sample, secret_val in (
        ("quoted_colon_sep", '"user: token": "SECRET_QCOLON"', "SECRET_QCOLON"),
        ("quoted_slash_sep", '"user/token": "SECRET_JSLASH"', "SECRET_JSLASH"),
    ):
      with self.subTest(non_dot_sep=label):
        out = scrub_sensitive_text(sample)
        self.assertNotIn(secret_val, out)
        self.assertIn("[REDACTED]", out)
    self.assertEqual(
        scrub_sensitive_text("password:\n  api key: SECRET_1"),
        "password:\n  api key: [REDACTED]",
    )

    # 6. Hot-path performance.
    perf_out, elapsed = _timed_scrub_median_sec(
        scrub_sensitive_text, ("x code=1 " * 25_000) + "password=tail7"
    )
    self.assertLess(elapsed, 5.0, f"hot_path took {elapsed:.3f}s")
    self.assertNotIn("tail7", perf_out)
    self.assertIn("[REDACTED]", perf_out)

  def test_stray_quote_kv_values_depth2_escapes_cookie_kv_boundaries_and_3bs_routing(
      self,
  ) -> None:
    from analytics.trajectory_extractor import scrub_sensitive_text  # noqa: PLC0415

    pad_17k = "a" * 17_000

    # 1. Stray quotes before quoted KV values & Cookie values.
    for label, sample, secret_val in (
        (
            "stray_dq_before_pw",
            'x" password="MY_REAL_SECRET"',
            "MY_REAL_SECRET",
        ),
        (
            "stray_dq_sentence_pw",
            'note: "user said "hello" and password="MY_REAL_SECRET"',
            "MY_REAL_SECRET",
        ),
        (
            "stray_sq_before_token",
            "err='bad, token='MY_SECRET_TOKEN'",
            "MY_SECRET_TOKEN",
        ),
        (
            "json_stray_dq_pw",
            json.dumps({"msg": 'err="bad, password="MY_REAL_SECRET"'}),
            "MY_REAL_SECRET",
        ),
        (
            "json_17k_stray_dq_pw",
            json.dumps(
                {
                    "body": pad_17k,
                    "msg": 'err="bad, password="MY_REAL_SECRET"',
                }
            ),
            "MY_REAL_SECRET",
        ),
        (
            "escaped_stray_dq_pw",
            r"x\" password=\"MY_REAL_SECRET\"",
            "MY_REAL_SECRET",
        ),
        (
            "stray_dq_cookie_sid",
            'log: "oops, Cookie: a=1; sid="SECRET_COOKIE"',
            "SECRET_COOKIE",
        ),
    ):
      with self.subTest(stray_quote_kv=label):
        out = scrub_sensitive_text(sample)
        self.assertNotIn(secret_val, out)
        self.assertIn("[REDACTED]", out)

    benign_json_out = scrub_sensitive_text(
        '{"note": "Missing password:", "error_code": "CARD_EXPIRED"}'
    )
    self.assertIn("CARD_EXPIRED", benign_json_out)

    _, elapsed = _timed_scrub_median_sec(scrub_sensitive_text, '"a="' * 20_000)
    self.assertLess(elapsed, 5.0, f"quoted_eq took {elapsed:.3f}s")

    # 2. Depth-2 escaped-quote period (_escaped_quote_mod_and_period).
    for label, sample, secret_val in (
        (
            "depth2_value_embedded_quote",
            json.dumps(
                {
                    "body": pad_17k,
                    "msg": json.dumps(
                        {"nested": json.dumps({"password": 'ab"SECRET_TAIL'})}
                    ),
                }
            ),
            "SECRET_TAIL",
        ),
        (
            "depth2_key_embedded_quote",
            json.dumps(
                {
                    "body": pad_17k,
                    "msg": json.dumps(
                        {"nested": json.dumps({'password"1': "SECRET_VAL"})}
                    ),
                }
            ),
            "SECRET_VAL",
        ),
        (
            "depth1_5bs_prefix_pw",
            json.dumps(
                {"body": pad_17k, "msg": r'foo\\"password": "SECRET_L5"'}
            ),
            "SECRET_L5",
        ),
    ):
      with self.subTest(depth2_escape=label):
        out = scrub_sensitive_text(sample)
        self.assertNotIn(secret_val, out)
        self.assertIn("[REDACTED]", out)

    # 3. Cookie semicolon KV connector boundaries & opaque colon token.
    for label, sample in (
        ("cookie_semi_token_colon", "Cookie: a=1; token: SECRET_VAL"),
        ("cookie_semi_pw_colon", "Cookie: a=1; password: SECRET_VAL"),
        ("cookie_semi_pw_spaced_eq", "Cookie: a=1; password = SECRET_VAL"),
        ("cookie_semi_pw_is", "Cookie: a=1; password is SECRET_VAL"),
    ):
      with self.subTest(cookie_kv_boundary=label):
        out = scrub_sensitive_text(sample)
        self.assertNotIn("SECRET_VAL", out)
        self.assertIn("[REDACTED]", out)

    cookies_count_out = scrub_sensitive_text("cookies: 3; token: SECRET_VAL")
    self.assertIn("cookies: 3", cookies_count_out)
    self.assertNotIn("SECRET_VAL", cookies_count_out)
    self.assertIn("[REDACTED]", cookies_count_out)

    self.assertEqual(
        scrub_sensitive_text("Cookie: sess:opaque_secret_val"),
        "Cookie: [REDACTED]",
    )
    self.assertEqual(
        scrub_sensitive_text("Set-Cookie: sid=abc; Path=/; HttpOnly"),
        "Set-Cookie: [REDACTED]",
    )

    # 4. Multi-backslash escaped-quote routing in _try_redact_sensitive_kv.
    for label, sample, secret_val in (
        (
            "json_17k_cookie_1bs",
            json.dumps({"body": pad_17k, "msg": r'Cookie: "secret_val"'}),
            "secret_val",
        ),
        (
            "json_17k_cookie_3bs",
            json.dumps({"body": pad_17k, "msg": r"Cookie: \"secret_val\""}),
            "secret_val",
        ),
        ("raw_cookie_1bs", r"Cookie: \"secret_val\"", "secret_val"),
        ("raw_cookie_3bs", r"Cookie: \\\"secret_val\\\"", "secret_val"),
        ("pw_nl2_1bs", "password:\n\n" + r"\"nl2_secret\"", "nl2_secret"),
        ("pw_nl2_3bs", "password:\n\n" + r"\\\"nl2_secret\\\"", "nl2_secret"),
        (
            "json_17k_pw_nl2_1bs",
            json.dumps(
                {"body": pad_17k, "msg": "password:\n\n" + r'"nl2_secret"'}
            ),
            "nl2_secret",
        ),
        (
            "json_17k_pw_nl2_3bs",
            json.dumps(
                {"body": pad_17k, "msg": "password:\n\n" + r"\"nl2_secret\""}
            ),
            "nl2_secret",
        ),
    ):
      with self.subTest(multi_bs_routing=label):
        out = scrub_sensitive_text(sample)
        self.assertNotIn(secret_val, out)
        self.assertIn("[REDACTED]", out)

    # 5. Stray quote before quoted sensitive key with `is`.
    for label, sample, secret_val in (
        (
            "stray_dq_quoted_pw_is",
            'log: "oops "password" is supersecret',
            "supersecret",
        ),
        (
            "json_17k_stray_dq_quoted_token_is",
            json.dumps(
                {
                    "body": pad_17k,
                    "msg": 'password=", "token" is "SECRET_LEAK_IS"',
                }
            ),
            "SECRET_LEAK_IS",
        ),
    ):
      with self.subTest(stray_quote_key_is=label):
        out = scrub_sensitive_text(sample)
        self.assertNotIn(secret_val, out)
        self.assertIn("[REDACTED]", out)

    self.assertEqual(
        scrub_sensitive_text('He said "hello" is a greeting'),
        'He said "hello" is a greeting',
    )

  def test_quote_kv_false_positive_nested_json_escapes_unicode_and_nl_quoted_is(
      self,
  ) -> None:
    from analytics.trajectory_extractor import scrub_sensitive_text  # noqa: PLC0415

    pad_17k = "x" * 17000

    # 1. Balanced quotes before `:en` / `=en` / `: auth failed`.
    for label, sample, secret_val in (
        (
            "cookie_colon_en",
            'Cookie: a="foo"; prefs=":en"; sid=REAL_SESSION_SECRET',
            "REAL_SESSION_SECRET",
        ),
        (
            "cookie_eq_en",
            'Cookie: a="foo"; prefs="=en"; sid=REAL_SESSION_SECRET',
            "REAL_SESSION_SECRET",
        ),
        (
            "escaped_cookie_colon_en",
            r"Cookie: a=\"foo\"; prefs=\":en\"; sid=REAL_SESSION_SECRET",
            "REAL_SESSION_SECRET",
        ),
    ):
      with self.subTest(balanced_quote_before_connector=label):
        out = scrub_sensitive_text(sample)
        self.assertNotIn(secret_val, out)
        self.assertIn("[REDACTED]", out)

    self.assertEqual(
        scrub_sensitive_text('password="!@#$%^" msg=": auth failed"'),
        'password="[REDACTED]" msg=": auth failed"',
    )
    self.assertEqual(
        scrub_sensitive_text('password="my_secret_pw" msg=": auth failed"'),
        'password="[REDACTED]" msg=": auth failed"',
    )

    # 2. Post-budget nested JSON `\\t`/`\\n` and `\uXXXX` escapes.
    for label, sample, secrets in (
        (
            "nested_json_list_tab",
            json.dumps(
                {"body": pad_17k, "msg": json.dumps(["password:\t secret_tab"])}
            ),
            ("secret_tab",),
        ),
        (
            "nested_json_dict_nl_two_secrets",
            json.dumps(
                {
                    "body": pad_17k,
                    "msg": json.dumps({"err": "password: s1\ntoken: s2"}),
                }
            ),
            ("s1", "s2"),
        ),
        (
            "nested_json_dict_nl_second_line_token",
            json.dumps(
                {
                    "body": pad_17k,
                    "msg": json.dumps({"err": "login failed\ntoken: s2"}),
                }
            ),
            ("s2",),
        ),
        (
            "unicode_escaped_key",
            f'{{"body": "{pad_17k}", "err": "\\u0070assword: secret_val"}}',
            ("secret_val",),
        ),
        (
            "unicode_escaped_colon",
            f'{{"body": "{pad_17k}", "err": "password\\u003a secret_val"}}',
            ("secret_val",),
        ),
    ):
      with self.subTest(post_budget_nested_or_unicode=label):
        out = scrub_sensitive_text(sample)
        for secret_val in secrets:
          self.assertNotIn(secret_val, out)
        self.assertIn("[REDACTED]", out)

    # 3. Next-line quoted `is`/`are` headers.
    self.assertEqual(
        scrub_sensitive_text('password:\n"token" is secret_val'),
        'password:\n"token" is [REDACTED]',
    )
    self.assertEqual(
        scrub_sensitive_text('password:\n"cookies" are secret_val'),
        'password:\n"cookies" are [REDACTED]',
    )
    self.assertEqual(
        scrub_sensitive_text('password:\n"secret_val" is bad'),
        'password:\n"[REDACTED]" is bad',
    )

    # 4. Balanced `msg="password=" password="END_S"`.
    self.assertEqual(
        scrub_sensitive_text('msg="password=" password="END_S"'),
        'msg="password=" password="[REDACTED]"',
    )

    # 5. Oversized (>16 KiB) JSON string literals still unescape cleanly.
    for label, sample, secrets in (
        (
            "oversized_nested_list_tab",
            json.dumps(
                {"msg": json.dumps(["a" * 17_000, "password:\t secret_tab"])}
            ),
            ("secret_tab",),
        ),
        (
            "oversized_nested_dict_nl",
            json.dumps(
                {
                    "msg": json.dumps(
                        {"body": "a" * 17_000, "err": "password: s1\ntoken: s2"}
                    )
                }
            ),
            ("s1", "s2"),
        ),
        (
            "oversized_nested_cookie_nl",
            json.dumps(
                {
                    "msg": json.dumps(
                        {
                            "body": "a" * 17_000,
                            "err": "Cookie: a=1; b=2\ntoken: SECRET_CK",
                        }
                    )
                }
            ),
            ("SECRET_CK",),
        ),
        (
            "oversized_unicode_escaped_key",
            '{"msg": "' + ("a" * 17_000) + ' \\u0070assword: secret_val"}',
            ("secret_val",),
        ),
        (
            "oversized_triple_nested_indented",
            json.dumps(
                {
                    "outer": json.dumps(
                        {
                            "msg": json.dumps(
                                {"body": "a" * 17_000, "password=1": 234567},
                                indent=2,
                            )
                        }
                    )
                }
            ),
            ("234567",),
        ),
    ):
      with self.subTest(oversized_json_string=label):
        out = scrub_sensitive_text(sample)
        for secret_val in secrets:
          self.assertNotIn(secret_val, out)
        self.assertIn("[REDACTED]", out)

    # 6. Truncated/multiline Branch B JSON escapes, _inspect_quoted_kv_token,
    #    and unclosed ASCII unicode escapes.
    for label, sample, secret_val, must_keep in (
        (
            "truncated_nested_dict_tab",
            json.dumps({"msg": json.dumps({"err": "password:\t SECRET_TTAB"})})[
                :-3
            ],
            "SECRET_TTAB",
            "",
        ),
        (
            "truncated_nested_list_nl",
            json.dumps(
                {"msg": json.dumps(["a" * 100, "password:\n SECRET_ANL"])}
            )[:-3],
            "SECRET_ANL",
            "",
        ),
        (
            "truncated_nested_unicode",
            json.dumps({"msg": '{"err": "\\u0070assword: SECRET_DTU"}'})[:-3],
            "SECRET_DTU",
            "",
        ),
        (
            "branch_b_escaped_tab",
            r"[\"password:\\t SECRET_VALUE\"]",
            "SECRET_VALUE",
            "",
        ),
        (
            "branch_b_escaped_unicode",
            r"[\"\\u0070assword: SECRET_U\"]",
            "SECRET_U",
            "",
        ),
        (
            "multiline_unicode_escape",
            '{"msg": "line1\n\\u0070assword: SECRET_MLU"}',
            "SECRET_MLU",
            "",
        ),
        (
            "unclosed_unicode_escape",
            '{"msg": "err \\u0070assword: SECRET_TUU',
            "SECRET_TUU",
            "",
        ),
        (
            "quoted_is_nested_json_tab",
            "Payload "
            + json.dumps(json.dumps({"err": "password:\t SECRET_QTAB"}))
            + " is invalid",
            "SECRET_QTAB",
            " is invalid",
        ),
        (
            "quoted_is_unicode_kv",
            'Field "\\u0070assword=SECRET_QUIS" is invalid',
            "SECRET_QUIS",
            " is invalid",
        ),
        (
            "quoted_colon_nested_json_tab",
            '"{\\"err\\": \\"password:\\\\t SECRET_VALUE\\"}": ok',
            "SECRET_VALUE",
            "",
        ),
        (
            "prose_quoted_colon_nested_json_tab",
            'Log "{\\"err\\": \\"password:\\\\t SECRET_VALUE\\"}": ok',
            "SECRET_VALUE",
            ": ok",
        ),
    ):
      with self.subTest(unified_json_decode=label):
        out = scrub_sensitive_text(sample)
        self.assertNotIn(secret_val, out)
        self.assertIn("[REDACTED]", out)
        if must_keep:
          self.assertIn(must_keep, out)

    self.assertEqual(
        scrub_sensitive_text('{"msg": "benign \\u0041"}'),
        '{"msg": "benign \\u0041"}',
    )

    # 7. Opaque Cookie tokens before is/are/:/=, post-budget _SECRET_RULES
    #    escapes, and truncated/3bs whitespace.
    for sample, expected in (
        (
            "Cookie: sess_9f8a7b6c1234 is expired",
            "Cookie: [REDACTED] is expired",
        ),
        (
            "Set-Cookie: c9f8a7b6c5d4 is rejected",
            "Set-Cookie: [REDACTED] is rejected",
        ),
        (
            "Cookie: c9f8a7b6c5d4: rejected",
            "Cookie: [REDACTED]: rejected",
        ),
        (
            "Cookie: c9f8a7b6c5d4 : rejected",
            "Cookie: [REDACTED] : rejected",
        ),
        (
            "Cookie: c9f8a7b6c5d4 = rejected",
            "Cookie: [REDACTED] = rejected",
        ),
        (
            "Cookies: tok1_tok2 are invalid",
            "Cookies: [REDACTED] are invalid",
        ),
    ):
      with self.subTest(opaque_cookie_connector=sample):
        self.assertEqual(scrub_sensitive_text(sample), expected)

    cookie_nested_key_out = scrub_sensitive_text("Cookie: token: SECRET_VAL")
    self.assertNotIn("SECRET_VAL", cookie_nested_key_out)
    self.assertIn("Cookie: token:", cookie_nested_key_out)
    self.assertIn("[REDACTED]", cookie_nested_key_out)

    for label, sample, secret_val in (
        (
            "post_budget_bearer_tab",
            json.dumps({"body": pad_17k, "msg": "Bearer\tmy_secret_token_99"}),
            "my_secret_token_99",
        ),
        (
            "post_budget_bearer_nl",
            json.dumps({"body": pad_17k, "msg": "Bearer\nmy_secret_token_nl"}),
            "my_secret_token_nl",
        ),
        (
            "post_budget_auth_bearer_sk",
            json.dumps(
                {
                    "body": pad_17k,
                    "msg": "Authorization\nBearer sk_live_1234567890abcdef",
                }
            ),
            "sk_live_1234567890abcdef",
        ),
        (
            "post_budget_basic_tab",
            json.dumps({"body": pad_17k, "msg": "Basic\tdXNlcjpwYXNz"}),
            "dXNlcjpwYXNz",
        ),
        (
            "post_budget_aiza_tab",
            json.dumps(
                {
                    "body": pad_17k,
                    "msg": "x\tAIzaSyDUMMYKEY12345678901234567890",
                }
            ),
            "AIzaSyDUMMYKEY12345678901234567890",
        ),
    ):
      with self.subTest(post_budget_secret_rule=label):
        out = scrub_sensitive_text(sample)
        self.assertNotIn(secret_val, out)
        self.assertIn("[REDACTED]", out)

    for label, sample, secret_val, must_keep in (
        (
            "truncated_5_nested_tab",
            json.dumps(
                {"msg": json.dumps({"err": "password:\t SECRET_TTAB_5"})}
            )[:-5],
            "SECRET_TTAB_5",
            "",
        ),
        (
            "truncated_5_nested_nl",
            json.dumps(
                {"msg": json.dumps({"err": "password:\n SECRET_TNL_5"})}
            )[:-5],
            "SECRET_TNL_5",
            "",
        ),
        (
            "lead_bs_3_double_escaped_tab",
            r"[\\\"password:\\\\t SECRET_3BS\\\"]",
            "SECRET_3BS",
            "",
        ),
        (
            "truncated_5_triple_nested_tab",
            json.dumps(
                {
                    "outer": json.dumps(
                        {"inner": json.dumps({"err": "password:\t SECRET_T3X"})}
                    )
                }
            )[:-5],
            "SECRET_T3X",
            "",
        ),
        (
            "unclosed_escaped_dq_preserves_error_code",
            '{"body": "'
            + pad_17k
            + '", "msg": "err\npassword: \\"unclosed", "error_code": "CARD_EXPIRED"}',
            "unclosed",
            '"error_code": "CARD_EXPIRED"',
        ),
    ):
      with self.subTest(truncated_or_3bs_or_unclosed=label):
        out = scrub_sensitive_text(sample)
        self.assertNotIn(secret_val, out)
        self.assertIn("[REDACTED]", out)
        if must_keep:
          self.assertIn(must_keep, out)

  def test_post_budget_nl_json_scalars_mismatched_quotes_path_headers_and_linear_unescape(
      self,
  ) -> None:
    import time  # noqa: PLC0415

    from analytics.trajectory_extractor import _is_likely_url_hostname  # noqa: PLC0415
    from analytics.trajectory_extractor import _is_valid_dns_host_syntax
    from analytics.trajectory_extractor import _unescape_safe_ascii_unicode_escapes
    from analytics.trajectory_extractor import extract_s3_outcome_first
    from analytics.trajectory_extractor import scrub_sensitive_data
    from analytics.trajectory_extractor import scrub_sensitive_text

    budget_prefix = '{"ok":1} ' * 32

    # 1. Post-budget JSON tail unquoted scalars after blank lines.
    post_budget_payload = (
        budget_prefix + '{"password":\n\n 493826,"error_code":"CARD_EXPIRED"}'
    )
    for label, sample, secret_val, must_keep in (
        (
            "post_budget_json_nl_int",
            post_budget_payload,
            "493826",
            "CARD_EXPIRED",
        ),
        (
            "post_budget_json_nl_neg_sci",
            budget_prefix
            + '{"password":\n\n -493826.5e-2,"error_code":"CARD_EXPIRED"}',
            "493826.5e-2",
            "CARD_EXPIRED",
        ),
        (
            "post_budget_json_escaped_nl_int",
            budget_prefix
            + r'{"password":\n\n 493826,"error_code":"CARD_EXPIRED"}',
            "493826",
            "CARD_EXPIRED",
        ),
        (
            "escaped_dq_nl_int",
            r"\"password\":" + "\n\n 493826",
            "493826",
            "",
        ),
        (
            "standalone_dq_nl_int",
            '"password":\n\n 493826',
            "493826",
            "",
        ),
        (
            "post_budget_unsafe_cookies_count",
            budget_prefix
            + '{"cookies":\n\n 493826,"error_code":"CARD_EXPIRED"}',
            "493826",
            "CARD_EXPIRED",
        ),
    ):
      with self.subTest(post_budget_json_nl_scalar=label):
        out = scrub_sensitive_text(sample)
        self.assertNotIn(secret_val, out)
        self.assertIn("[REDACTED]", out)
        if must_keep:
          self.assertIn(must_keep, out)

    safe_cookies_out = scrub_sensitive_text(
        budget_prefix + '{"cookies":\n\n 3,"error_code":"CARD_EXPIRED"}'
    )
    self.assertIn('"cookies":\n\n 3', safe_cookies_out)
    self.assertIn("CARD_EXPIRED", safe_cookies_out)
    self.assertNotIn("[REDACTED]", safe_cookies_out)

    for prose_sample in (
        "password:\n\nERROR: CARD_EXPIRED",
        'Field "password":\n\nERROR: CARD_EXPIRED',
        'Field "password" is:\n\n 493826',
    ):
      with self.subTest(prose_nl_preserved=prose_sample):
        self.assertEqual(scrub_sensitive_text(prose_sample), prose_sample)

    s3_trace = {
        "turns": [
            {
                "turn_index": 0,
                "turn_id": "inv_post_budget_nl",
                "events": [
                    {
                        "author": "customer_ops_agent",
                        "content": {
                            "role": "model",
                            "parts": [{"text": post_budget_payload}],
                        },
                    }
                ],
            }
        ]
    }
    s3_text = extract_s3_outcome_first(
        s3_trace,
        metrics={"error_summary": post_budget_payload},
    )
    self.assertNotIn("493826", s3_text)
    self.assertIn("CARD_EXPIRED", s3_text)
    self.assertIn("[REDACTED]", s3_text)

    # 2. Escaped opening quote and mismatched quote-key recovery.
    for label, sample, secret_val, must_keep in (
        (
            "edq_plain_close",
            r'\"password": "S3CR3T" CARD_EXPIRED',
            "S3CR3T",
            "CARD_EXPIRED",
        ),
        (
            "edq_plain_close_post_budget",
            '{"ok":1} ' * 33 + r'\"password": "S3CR3T" CARD_EXPIRED',
            "S3CR3T",
            "CARD_EXPIRED",
        ),
        (
            "edq_3bs_1bs_close",
            r'\\\"password\": "S3CR3T" CARD_EXPIRED',
            "S3CR3T",
            "CARD_EXPIRED",
        ),
        (
            "edq_stray_prefix",
            r'\"stray \"password": "S3CR3T" CARD_EXPIRED',
            "S3CR3T",
            "CARD_EXPIRED",
        ),
        (
            "dq_escaped_close_str",
            r'"password\": "S3CR3T" CARD_EXPIRED',
            "S3CR3T",
            "CARD_EXPIRED",
        ),
        (
            "dq_escaped_close_num",
            r'"password\": 493826 CARD_EXPIRED',
            "493826",
            "CARD_EXPIRED",
        ),
        (
            "esq_plain_close",
            r"\'password': 'S3CR3T' CARD_EXPIRED",
            "S3CR3T",
            "CARD_EXPIRED",
        ),
        (
            "esq_escaped_close",
            r"\'password\': \'S3CR3T\' CARD_EXPIRED",
            "S3CR3T",
            "CARD_EXPIRED",
        ),
        (
            "edq_sq_close",
            r"\"password': 'S3CR3T' CARD_EXPIRED",
            "S3CR3T",
            "CARD_EXPIRED",
        ),
        (
            "dq_sq_close",
            '"password\': "S3CR3T" CARD_EXPIRED',
            "S3CR3T",
            "CARD_EXPIRED",
        ),
        (
            "sq_dq_close",
            "'password\": 'S3CR3T' CARD_EXPIRED",
            "S3CR3T",
            "CARD_EXPIRED",
        ),
        (
            "auth_next_line_edq_mismatched",
            'authentication:\n  \\"password": "S3CR3T"',
            "S3CR3T",
            '\\"password":',
        ),
        (
            "auth_next_line_dq_escaped_close",
            'authentication:\n  "password\\": "S3CR3T"',
            "S3CR3T",
            '"password\\":',
        ),
        (
            "auth_next_line_esq_plain_close",
            "authentication:\n  \\'password': 'S3CR3T'",
            "S3CR3T",
            "\\'password':",
        ),
        (
            "dq_escaped_close_json_object",
            r'{"password\": 493826, "error_code": "CARD_EXPIRED"}',
            "493826",
            '"error_code": "CARD_EXPIRED"',
        ),
        (
            "dq_escaped_close_json_object_post_budget",
            '{"ok":1} ' * 33
            + r'{"password\": 493826, "error_code": "CARD_EXPIRED"}',
            "493826",
            '"error_code": "CARD_EXPIRED"',
        ),
        (
            "dq_escaped_close_bare_then_quoted",
            r'"password\": S3CR3T, "error_code": "CARD_EXPIRED"',
            "S3CR3T",
            '"error_code": "CARD_EXPIRED"',
        ),
        (
            "dq_escaped_close_num_then_quoted_err",
            r'"password\": 493826 "CARD_EXPIRED"',
            "493826",
            '"CARD_EXPIRED"',
        ),
        (
            "dq_escaped_close_num_then_quoted_foo",
            r'"password\": 493826 "foo"',
            "493826",
            '"foo"',
        ),
        (
            "dq_sq_close_num_then_quoted_err",
            '"password\': 493826, "error_code": "CARD_EXPIRED"',
            "493826",
            '"error_code": "CARD_EXPIRED"',
        ),
        (
            "edq_esq_close_then_edq_err",
            r"\"password\': \'S3CR3T\', \"error_code\": \"CARD_EXPIRED\"",
            "S3CR3T",
            r"\"error_code\": \"CARD_EXPIRED\"",
        ),
        (
            "edq_3bs_close_str_then_err",
            r"\"password\\\": \"S3CR3T\" CARD_EXPIRED",
            "S3CR3T",
            "CARD_EXPIRED",
        ),
        (
            "edq_3bs_close_num_then_edq_err",
            r"\"password\\\": 493826, \"error_code\": \"CARD_EXPIRED\"",
            "493826",
            r"\"error_code\": \"CARD_EXPIRED\"",
        ),
    ):
      with self.subTest(mismatched_quote_key=label):
        out = scrub_sensitive_text(sample)
        self.assertNotIn(secret_val, out)
        self.assertIn("[REDACTED]", out)
        if must_keep:
          self.assertIn(must_keep, out)

    self.assertEqual(scrub_sensitive_text(r"It\'s ok"), r"It\'s ok")
    for valid_json_sample, expected_obj in (
        (
            '{"msg": "he said \\"password\\": \\"S3CR3T\\""}',
            {"msg": 'he said "password": "[REDACTED]"'},
        ),
        (
            '{"msg": "\\"password\\": \\"S3CR3T\\""}',
            {"msg": '"password": "[REDACTED]"'},
        ),
        (
            '{"msg": "password\'s value: ok"}',
            {"msg": "password's value: ok"},
        ),
    ):
      with self.subTest(valid_json_preserved=valid_json_sample):
        out = scrub_sensitive_text(valid_json_sample)
        self.assertNotIn("S3CR3T", out)
        self.assertEqual(json.loads(out), expected_obj)
        pb_out = scrub_sensitive_text(budget_prefix + valid_json_sample)
        self.assertNotIn("S3CR3T", pb_out)
        self.assertEqual(json.loads(pb_out[len(budget_prefix) :]), expected_obj)

    # 3. Slash and colon path headers, URL spans, cookies, and token prefixes.
    for sample, expected in (
        (
            "authentication:\n  request/password: samplevalue",
            "authentication:\n  request/password: [REDACTED]",
        ),
        (
            "authentication:\n  request:password: samplevalue",
            "authentication:\n  request:password: [REDACTED]",
        ),
        (
            "authentication:\n  auth:password: samplevalue",
            "authentication:\n  auth:password: [REDACTED]",
        ),
        (
            "authentication:\n  request:password=samplevalue",
            "authentication:\n  request:password=[REDACTED]",
        ),
        (
            "authentication:\n  projects/p/secrets/password: samplevalue",
            "authentication:\n  projects/p/secrets/password: [REDACTED]",
        ),
        (
            "authentication:\n  /request/password: samplevalue",
            "authentication:\n  /request/password: [REDACTED]",
        ),
        (
            "authentication:\n  request/api key: samplevalue",
            "authentication:\n  request/api key: [REDACTED]",
        ),
        (
            "authentication:\n  request:api key: samplevalue",
            "authentication:\n  request:api key: [REDACTED]",
        ),
        (
            "authentication:\n  bearer token: samplevalue",
            "authentication:\n  bearer token: [REDACTED]",
        ),
        (
            "authentication:\n  request/bearer token: samplevalue",
            "authentication:\n  request/bearer token: [REDACTED]",
        ),
        (
            "request/secret/val: samplevalue",
            "request/secret/val: [REDACTED]",
        ),
        (
            "request:auth:password: samplevalue",
            "request:auth:password: [REDACTED]",
        ),
        (
            "auth:credentials:token: samplevalue",
            "auth:credentials:token: [REDACTED]",
        ),
        (
            "password:password: login failed",
            "password:[REDACTED] login failed",
        ),
        (
            "password:secret: FAILED",
            "password:[REDACTED] FAILED",
        ),
        (
            "token:token: expired",
            "token:[REDACTED] expired",
        ),
        (
            "api_key:apikey: rejected",
            "api_key:[REDACTED] rejected",
        ),
        (
            "user:password:password: FAILED",
            "user:password:[REDACTED] FAILED",
        ),
        (
            "password:letmein:password: FAILED",
            "password:[REDACTED] FAILED",
        ),
        (
            "password:hunter:my_token: FAILED",
            "password:[REDACTED] FAILED",
        ),
        (
            "token:abc:def:api_key: x1",
            "token:[REDACTED] x1",
        ),
        (
            "secret:secret: FAILED",
            "secret:[REDACTED] FAILED",
        ),
        (
            "auth:password = hunter2",
            "auth:password = [REDACTED]",
        ),
        (
            "authentication:\n  auth:password = samplevalue",
            "authentication:\n  auth:password = [REDACTED]",
        ),
        (
            "oauth:accessToken: S3CR3T_TOKEN",
            "oauth:accessToken: [REDACTED]",
        ),
        (
            "authentication:\n  auth:apiKey: samplevalue",
            "authentication:\n  auth:apiKey: [REDACTED]",
        ),
        (
            "Auth:Key: samplevalue",
            "Auth:Key: [REDACTED]",
        ),
        (
            "Secret:Access_Key: samplevalue",
            "Secret:Access_Key: [REDACTED]",
        ),
        (
            "secrets:db_password: hunter2",
            "secrets:db_password: [REDACTED]",
        ),
        (
            "authentication:\n  auth:db_password: samplevalue",
            "authentication:\n  auth:db_password: [REDACTED]",
        ),
        (
            "auth:v1:password: S3CR3T",
            "auth:v1:password: [REDACTED]",
        ),
        (
            "auth:oauth2:access_token: S3CR3T",
            "auth:oauth2:access_token: [REDACTED]",
        ),
        (
            "secrets:v2:db_password: hunter2",
            "secrets:v2:db_password: [REDACTED]",
        ),
        (
            "SECRETS:PROD:DB_PASSWORD: hunter2",
            "SECRETS:PROD:DB_PASSWORD: [REDACTED]",
        ),
        (
            "https://oauth2:abc&def99@gitlab.com",
            "https://oauth2:[REDACTED]@gitlab.com",
        ),
        (
            "https://token:p4*ss99@api.example.com",
            "https://token:[REDACTED]@api.example.com",
        ),
        (
            "https://user:a,b@host.example.com",
            "https://user:[REDACTED]@host.example.com",
        ),
        (
            "https://user:a;b@host.example.com",
            "https://user:[REDACTED]@host.example.com",
        ),
        (
            "https://user:a)b@host.example.com",
            "https://user:[REDACTED]@host.example.com",
        ),
        (
            "https://user:a'b@host.example.com",
            "https://user:[REDACTED]@host.example.com",
        ),
        (
            "https://auth.example.com:8443,alice@example.com",
            "https://auth.example.com:8443,alice@example.com",
        ),
        (
            "url=https://example.com:8443,error_code=CARD_EXPIRED,email=alice@corp.com",
            "url=https://example.com:8443,error_code=CARD_EXPIRED,email=alice@corp.com",
        ),
        (
            "https://user:pass@host.example.com:8443,error_code=CARD_EXPIRED,email=alice@corp.com",
            "https://user:[REDACTED]@host.example.com:8443,error_code=CARD_EXPIRED,email=alice@corp.com",
        ),
        (
            "https://example.com/.password=SECRET",
            "https://example.com/.password=[REDACTED]",
        ),
        (
            "https://example.com/-token=SECRET",
            "https://example.com/-token=[REDACTED]",
        ),
        (
            "https://h/q=1/v1/auth/verify: CARD_EXPIRED",
            "https://h/q=1/v1/auth/verify: CARD_EXPIRED",
        ),
        (
            "./password: S3CR3T",
            "./password: [REDACTED]",
        ),
        (
            "../password: S3CR3T",
            "../password: [REDACTED]",
        ),
        (
            "/.password: S3CR3T",
            "/.password: [REDACTED]",
        ),
        (
            "/-password=S3CR3T",
            "/-password=[REDACTED]",
        ),
        (
            "authentication:\n  ./request/password: samplevalue",
            "authentication:\n  ./request/password: [REDACTED]",
        ),
        (
            "https://example.com/.-password=S3CR3T",
            "https://example.com/.-password=[REDACTED]",
        ),
        (
            ".api key: S3CR3T",
            ".api key: [REDACTED]",
        ),
        (
            "./request/api key: S3CR3T",
            "./request/api key: [REDACTED]",
        ),
        (
            "authentication:\n  ./request/api key: samplevalue",
            "authentication:\n  ./request/api key: [REDACTED]",
        ),
        (
            "auth/request:password: samplevalue",
            "auth/request:password: [REDACTED]",
        ),
        (
            "secrets/v1:password: samplevalue",
            "secrets/v1:password: [REDACTED]",
        ),
        (
            "authentication:\n  auth/v1:password: samplevalue",
            "authentication:\n  auth/v1:password: [REDACTED]",
        ),
        (
            "password/v1:secret: FAILED",
            "password/v1:[REDACTED] FAILED",
        ),
        (
            "auth:v1/password: samplevalue",
            "auth:v1/password: [REDACTED]",
        ),
        (
            "authentication:\n  auth:v1/password: samplevalue",
            "authentication:\n  auth:v1/password: [REDACTED]",
        ),
        (
            "auth:request/apikey: samplevalue",
            "auth:request/apikey: [REDACTED]",
        ),
        (
            "auth:oauth2_token: samplevalue",
            "auth:oauth2_token: [REDACTED]",
        ),
        (
            "credentials:ssh_passphrase: samplevalue",
            "credentials:ssh_passphrase: [REDACTED]",
        ),
        (
            "secrets:jwt_signing_key: samplevalue",
            "secrets:jwt_signing_key: [REDACTED]",
        ),
        (
            "auth:Password1: FAILED",
            "auth:[REDACTED] FAILED",
        ),
        (
            "auth:secret42: FAILED",
            "auth:[REDACTED] FAILED",
        ),
        (
            "request:password:samplevalue",
            "request:password:[REDACTED]",
        ),
        (
            "a:" * 20 + "password: samplevalue",
            "a:" * 20 + "password: [REDACTED]",
        ),
        (
            "password:hunter2: FAILED",
            "password:[REDACTED] FAILED",
        ),
        (
            "password:hunter2:",
            "password:[REDACTED]",
        ),
        (
            "password:cGFzc3dvcmQxMjM= CARD_EXPIRED",
            "password:[REDACTED] CARD_EXPIRED",
        ),
        (
            "client_secret:Zm9vYmFy=",
            "client_secret:[REDACTED]",
        ),
        (
            "user:password:hunter2: FAILED",
            "user:password:[REDACTED] FAILED",
        ),
        (
            "config:api_key:sk_live_x9: rejected",
            "config:api_key:[REDACTED] rejected",
        ),
        (
            '{"msg": "password:hunter2: retry"}',
            '{"msg":"password:[REDACTED] retry"}',
        ),
        (
            "authentication:\n  password:hunter2: FAILED",
            "authentication:\n  password:[REDACTED] FAILED",
        ),
        (
            "Cookie:_ga=GA1.2.3; JSESSIONID=9f8e7d6c5b4a",
            "Cookie:[REDACTED]",
        ),
        (
            "api_key:proj1:secret999",
            "api_key:[REDACTED]",
        ),
        (
            "auth:error_code: CARD_EXPIRED",
            "auth:error_code: CARD_EXPIRED",
        ),
        (
            "password:author:hunter2",
            "password:[REDACTED]",
        ),
        (
            "secret:status_code:abc123",
            "secret:[REDACTED]",
        ),
        (
            "token:key_file=abc123",
            "token:[REDACTED]",
        ),
        (
            "password:Password1: FAILED",
            "password:[REDACTED] FAILED",
        ),
        (
            "password:password123:",
            "password:[REDACTED]",
        ),
        (
            "password:Secret42: login failed",
            "password:[REDACTED] login failed",
        ),
        (
            "token:Auth2024: expired",
            "token:[REDACTED] expired",
        ),
        (
            "user:password:Password1: FAILED",
            "user:password:[REDACTED] FAILED",
        ),
        (
            "api_key:apikey7: rejected",
            "api_key:[REDACTED] rejected",
        ),
        (
            'password:my_secret:"x"',
            'password:[REDACTED]"x"',
        ),
        (
            "authentication:\n  auth:key: samplevalue",
            "authentication:\n  auth:key: [REDACTED]",
        ),
        (
            "authentication:\n  secret:access_key: samplevalue",
            "authentication:\n  secret:access_key: [REDACTED]",
        ),
        (
            "aws:secret:access_key: samplevalue",
            "aws:secret:access_key: [REDACTED]",
        ),
        (
            "auth:code: samplevalue",
            "auth:code: [REDACTED]",
        ),
        (
            "auth:header: samplevalue",
            "auth:header: [REDACTED]",
        ),
        (
            "auth:response_uri: https://cb?code=1",
            "auth:response_uri: [REDACTED]",
        ),
        (
            "auth:user:password: S3CR3T",
            "auth:user:password: [REDACTED]",
        ),
        (
            "credentials:aws:secret_access_key: wJalrXUtnFEMI",
            "credentials:aws:secret_access_key: [REDACTED]",
        ),
        (
            "secrets:prod:db_password: hunter2",
            "secrets:prod:db_password: [REDACTED]",
        ),
        (
            "authentication:\n  https://example.com/secret_token",
            "authentication:\n  [REDACTED]",
        ),
        (
            "authentication:\n  https://user:pass@example.com/api",
            "authentication:\n  [REDACTED]",
        ),
        (
            "database_url:\n  postgres://u:p@host",
            "database_url:\n  [REDACTED]",
        ),
        (
            "error at https://api.example.com/v1/auth/verify: CARD_EXPIRED",
            "error at https://api.example.com/v1/auth/verify: CARD_EXPIRED",
        ),
        (
            "https://auth.example.com:8443/healthz",
            "https://auth.example.com:8443/healthz",
        ),
        (
            "https://example.com/cb?access_token=SECRET&error_code=CARD_EXPIRED",
            "https://example.com/cb?access_token=[REDACTED]&error_code=CARD_EXPIRED",
        ),
        (
            "https://oauth2:glpat-AbCdEf123456@gitlab.com/g/r.git",
            "https://oauth2:[REDACTED]@gitlab.com/g/r.git",
        ),
        (
            "https://x-access-token:0123456789abcdef@github.com/o/r.git",
            "https://x-access-token:[REDACTED]@github.com/o/r.git",
        ),
        (
            "https://password:SECRET_PW@example.com:8443/healthz",
            "https://password:[REDACTED]@example.com:8443/healthz",
        ),
        (
            "https://alice:hunter2@example.com/repo",
            "https://alice:[REDACTED]@example.com/repo",
        ),
        (
            "wss://token:ws_secret_999@ws.example.com/stream",
            "wss://token:[REDACTED]@ws.example.com/stream",
        ),
        (
            "ftp://password:ftp_secret_999@ftp.example.com/pub",
            "ftp://password:[REDACTED]@ftp.example.com/pub",
        ),
        (
            r"https:\/\/oauth2:glpat-AbCdEf123456@gitlab.com\/g\/r.git",
            r"https:\/\/oauth2:[REDACTED]@gitlab.com\/g\/r.git",
        ),
        (
            "uri:/login/password=hunter2",
            "uri:/login/password=[REDACTED]",
        ),
        (
            "x:/a/b/c/api_key=SECRET999",
            "x:/a/b/c/api_key=[REDACTED]",
        ),
        (
            "path:/request/password: samplevalue",
            "path:/request/password: [REDACTED]",
        ),
        (
            "see https://example.com/v1/password: S3CR3T_VAL next",
            "see https://example.com/v1/password: [REDACTED] next",
        ),
        (
            "see https://example.com/v1/api_key: sk_test_mysecret_12345678",
            "see https://example.com/v1/api_key: [REDACTED]",
        ),
        (
            "password=p&ss=w0rd",
            "password=[REDACTED]",
        ),
        (
            "token=abc123&sig=def456",
            "token=[REDACTED]",
        ),
        (
            "X-Goog-Credential=abc&X-Goog-Signature=deadbeef",
            "X-Goog-Credential=[REDACTED]",
        ),
        (
            "access_token=TOK1&client_secret=SEC2",
            "access_token=[REDACTED]&client_secret=[REDACTED]",
        ),
        (
            "request/error_code: CARD_EXPIRED",
            "request/error_code: CARD_EXPIRED",
        ),
        (
            "usage.prompt token: 812",
            "usage.prompt token: 812",
        ),
        (
            "usage/prompt token: 812",
            "usage/prompt token: 812",
        ),
        (
            "request/prompt token: 42",
            "request/prompt token: 42",
        ),
        (
            "password/prompt token: SECRET",
            "password/prompt token: [REDACTED]",
        ),
    ):
      with self.subTest(path_header=sample):
        self.assertEqual(scrub_sensitive_text(sample), expected)

    for label, sample, secret_val, must_keep in (
        (
            "balanced_dq_esc_close_then_secrets",
            'note="password\\": 123456" secrets=["TOPSECRET"]',
            "TOPSECRET",
            'note="password\\": [REDACTED]"',
        ),
        (
            "balanced_dq_sq_close_then_secrets",
            'note="password\': 123456" secrets=["TOPSECRET"]',
            "TOPSECRET",
            'note="password\': [REDACTED]"',
        ),
        (
            "balanced_dq_esc_close_then_ctor",
            'note="password\\": 123456" secret=SecretStr("TOPSECRET")',
            "TOPSECRET",
            'note="password\\": [REDACTED]"',
        ),
        (
            "post_budget_array_mismatched_then_unicode_pw",
            budget_prefix
            + '["password\\": 123456", "authentication:\\u000apassword: TOPSECRET"]',
            "TOPSECRET",
            "",
        ),
        (
            "balanced_edq_3bs_close_then_secrets",
            r'note=\"password\\\": 123456\" secrets=["TOPSECRET"]',
            "TOPSECRET",
            r"note=\"password\\\": [REDACTED]\"",
        ),
        (
            "balanced_edq_sq_close_then_secrets",
            r"note=\"password" + "': 123456" + r'\" secrets=["TOPSECRET"]',
            "TOPSECRET",
            r"note=\"password" + "': [REDACTED]" + r"\"",
        ),
        (
            "balanced_dq_inner_tail_bar",
            'note="password\\": 123456 bar" secrets=["TOPSECRET"]',
            "TOPSECRET",
            'note="password\\": [REDACTED] bar"',
        ),
        (
            "balanced_dq_inner_tail_comma",
            'note="password\\": 123456, other=1" secrets=["TOPSECRET"]',
            "TOPSECRET",
            'note="password\\": [REDACTED], other=1"',
        ),
        (
            "balanced_dq_sq_inner_tail_ok",
            'note="password\': 123456 ok" secrets=["TOPSECRET"]',
            "TOPSECRET",
            'note="password\': [REDACTED] ok"',
        ),
        (
            "balanced_dq_escaped_val_tail_bar",
            'note="password\\": \\"123456\\" bar" secrets=["TOPSECRET"]',
            "TOPSECRET",
            'note="password\\": \\"[REDACTED]\\" bar"',
        ),
        (
            "balanced_dq_inner_tail_token_and_outer_secrets",
            'note="password\\": 123456; token=INNER_SEC" secrets=["TOPSECRET"]',
            "INNER_SEC",
            'note="password\\": [REDACTED]; token=[REDACTED]"',
        ),
        (
            "balanced_edq_3bs_inner_tail_bar",
            r"note=\"password\\\": 123456 bar\" secrets=[\"TOPSECRET\"]",
            "TOPSECRET",
            r"note=\"password\\\": [REDACTED] bar\"",
        ),
        (
            "balanced_dq_inner_tail_digits",
            'note="password\\": 123456 789" secrets=["TOPSECRET"]',
            "TOPSECRET",
            'note="password\\": [REDACTED] 789"',
        ),
        (
            "balanced_dq_unclosed_inner_sq_preserves_error_code",
            'note="password\': \'123456" error_code="CARD_EXPIRED"',
            "123456",
            'error_code="CARD_EXPIRED"',
        ),
        (
            "balanced_edq_unclosed_inner_sq_preserves_error_code",
            r"note=\"password': '123456\" error_code=\"CARD_EXPIRED\"",
            "123456",
            r"error_code=\"CARD_EXPIRED\"",
        ),
        (
            "url_oauth_query_client_secret",
            "GET https://auth.example.com/oauth/token?"
            "grant_type=authorization_code&redirect_uri="
            "https://app.example.com/cb&client_secret=abc999zz&code=xyz",
            "abc999zz",
            "https://auth.example.com/oauth/token?",
        ),
        (
            "url_post_body_client_secret",
            "POST body: grant_type=authorization_code&code=xyz&redirect_uri="
            "https://app.example.com/cb&client_id=app1&client_secret=abc999zz",
            "abc999zz",
            "redirect_uri=https://app.example.com/cb",
        ),
        (
            "url_json_amp_access_token",
            '{"url": "https://api.example.com/v1/data&access_token=abc999zz"}',
            "abc999zz",
            "https://api.example.com/v1/data",
        ),
        (
            "url_amp_password",
            "next=https://app.example.com/home&password=hunter2zz",
            "hunter2zz",
            "next=https://app.example.com/home",
        ),
        (
            "url_pipe_token",
            "GET|https://api.example.com/v1|token=abc123zz|200",
            "abc123zz",
            "GET|https://api.example.com/v1|",
        ),
        (
            "url_html_gt_token",
            "<a href=https://h.example.com/x>token=abc123zz</a>",
            "abc123zz",
            "<a href=https://h.example.com/x>",
        ),
        (
            "url_star_password",
            "https://h.example.com/x*password=abc999",
            "abc999",
            "https://h.example.com/x*",
        ),
        (
            "url_path_eq_token",
            "https://h.example.com/dl/token=abc123zz/file.zip",
            "abc123zz",
            "https://h.example.com/dl/",
        ),
        (
            "url_path_colon_password",
            "https://h.example.com/a/password:hunter2/b",
            "hunter2",
            "https://h.example.com/a/password:",
        ),
        (
            "url_path_auth_colon_pw_eq",
            "see https://example.com/foo/auth:password=SEC1 ok",
            "SEC1",
            " ok",
        ),
        (
            "balanced_edq_unclosed_inner_esq_preserves_error_code",
            r"note=\"password\': \'123456\" error_code=\"CARD_EXPIRED\"",
            "123456",
            r"error_code=\"CARD_EXPIRED\"",
        ),
        (
            "pathless_url_amp_client_secret_then_email",
            "redirect_uri=https://app.example.com&client_secret=abc999zz&login_hint=alice@example.com",
            "abc999zz",
            "redirect_uri=https://app.example.com",
        ),
        (
            "pathless_url_amp_password_then_email",
            "next=https://app.example.com&password=hunter2zz&email=a@b.co",
            "hunter2zz",
            "next=https://app.example.com",
        ),
        (
            "pathless_url_comma_password_then_email",
            "url=https://h.example.com,password=abc999,user=a@b.c",
            "abc999",
            "user=a@b.c",
        ),
        (
            "pathless_url_semi_token_then_email",
            "https://h.example.com;token=abc999;u=a@b",
            "abc999",
            "u=a@b",
        ),
        (
            "pathless_url_amp_secret_colon_time_then_email",
            "https://example.com&client_secret=abc999&t=12:30&login_hint=a@b.c",
            "abc999",
            "https://example.com",
        ),
        (
            "pathless_url_comma_password_corp_email",
            "url=https://example.com,password=S3CR3T,email=alice@corp.com",
            "S3CR3T",
            "email=alice@corp.com",
        ),
        (
            "pathless_url_semi_api_key_corp_email",
            "url=https://example.com;api_key=S3CR3T;actor=alice@corp.com",
            "S3CR3T",
            "actor=alice@corp.com",
        ),
        (
            "pathless_url_paren_comma_secret_corp_email",
            "(https://example.com),secret=S3CR3T,contact=alice@corp.com",
            "S3CR3T",
            "contact=alice@corp.com",
        ),
        (
            "pathless_url_json_amp_access_token_then_email",
            '{"url": "https://api.example.com&access_token=abc999zz&user=alice@corp.com"}',
            "abc999zz",
            "https://api.example.com",
        ),
        (
            "pathless_url_escaped_sq_comma_password_then_email",
            r"url=https://example.com\',password=S3CR3T,email=alice@corp.com",
            "S3CR3T",
            "email=alice@corp.com",
        ),
    ):
      with self.subTest(round19_guard=label):
        out = scrub_sensitive_text(sample)
        self.assertNotIn(secret_val, out)
        self.assertIn("[REDACTED]", out)
        if must_keep:
          self.assertIn(must_keep, out)

    both_inner_and_outer = scrub_sensitive_text(
        'note="password\\": 123456; token=INNER_SEC" secrets=["TOPSECRET"]'
    )
    self.assertNotIn("123456", both_inner_and_outer)
    self.assertNotIn("INNER_SEC", both_inner_and_outer)
    self.assertNotIn("TOPSECRET", both_inner_and_outer)

    pb_unclosed_sq_json = scrub_sensitive_text(
        budget_prefix
        + '{"note": "password\': \'123456", "error_code": "CARD_EXPIRED"}'
    )
    self.assertNotIn("123456", pb_unclosed_sq_json)
    self.assertEqual(
        json.loads(pb_unclosed_sq_json[len(budget_prefix) :]),
        {"note": "password': [REDACTED]", "error_code": "CARD_EXPIRED"},
    )

    # 4. Linear-time ASCII unicode unescaping & colon-chain perf + parity.
    for raw_seq, expected in (
        (r"\u0070", "p"),
        (r"\\u0070", r"\\u0070"),
        (r"\\\u0070", r"\\p"),
        (r"\u0020", " "),
        (r"\u007e", "~"),
        (r"\u0022", r"\u0022"),
        (r"\u005c", r"\u005c"),
        (r"\u005C", r"\u005C"),
        (r"\u007f", r"\u007f"),
        (r"\u007F", r"\u007F"),
        (r"\u001f", r"\u001f"),
    ):
      with self.subTest(ascii_unicode_parity=raw_seq):
        self.assertEqual(
            _unescape_safe_ascii_unicode_escapes(raw_seq), expected
        )

    for label, payload, expected_suffix in (
        ("bs40k_x_u0070", "\\" * 40000 + "x \\u0070", "x p"),
        ("bs40k_u0070", "\\" * 40000 + "\\u0070", "\\\\p"),
    ):
      with self.subTest(linear_backslash_perf=label):
        unescaped, dt_unescape = _timed_scrub_median_sec(
            _unescape_safe_ascii_unicode_escapes, payload
        )
        self.assertTrue(unescaped.endswith(expected_suffix))
        self.assertLess(
            dt_unescape, 5.0, f"{label} unescape took {dt_unescape:.3f}s"
        )

        scrubbed, dt_scrub = _timed_scrub_median_sec(
            scrub_sensitive_text, payload
        )
        self.assertEqual(scrubbed, payload)
        self.assertLess(dt_scrub, 5.0, f"{label} scrub took {dt_scrub:.3f}s")

    colons_payload = "a:" * 20000
    colons_out, dt_colons = _timed_scrub_median_sec(
        scrub_sensitive_text, colons_payload
    )
    self.assertEqual(colons_out, colons_payload)
    self.assertLess(dt_colons, 5.0, f"colons_payload took {dt_colons:.3f}s")

    for label, payload in (
        ("url_auth_colon_comma_50k", "https://u" + ":1," * 50000),
        ("url_auth_paren_50k", "https://u:" + ")" * 50000),
        ("url_auth_alnum_comma_50k", "https://u:1" + "a," * 50000),
        ("leading_path_punct_50k", "./-" * 50000),
        ("unclosed_dq_50k", '"' + "a" * 50000),
        ("many_dq_50k", '"' * 50000),
    ):
      with self.subTest(linear_url_or_punct_perf=label):
        out, elapsed = _timed_scrub_median_sec(scrub_sensitive_text, payload)
        self.assertEqual(out, payload)
        self.assertLess(elapsed, 5.0, f"{label} took {elapsed:.3f}s")

    for label, payload_fn, expected_fn in (
        (
            "repeated_auth_slash",
            lambda n: "auth/" * n + "tenant:field",
            lambda n: "auth/" * n + "tenant:[REDACTED]",
        ),
        (
            "repeated_auth_v1_colon",
            lambda n: "auth:v1:" * n + "field",
            lambda n: "auth:[REDACTED]",
        ),
        (
            "repeated_auth_plus_v1_colon",
            lambda n: "auth:" + "v1:" * n + "field",
            lambda n: "auth:[REDACTED]",
        ),
        (
            "repeated_auth_space_colon",
            lambda n: "auth: " * n + "field",
            lambda n: "auth: [REDACTED] " * (n // 2) + "field"
            if n % 2 == 0
            else "auth: [REDACTED]",
        ),
        (
            "repeated_auth_oauth2_space_colon",
            lambda n: "auth: oauth2: " * n + "field",
            lambda n: "auth: [REDACTED] " * (n - 1)
            + "auth: oauth2: [REDACTED]",
        ),
        (
            "repeated_auth_v1_v2_space_colon",
            lambda n: "auth: v1: v2: " * n + "field",
            lambda n: (
                "auth: [REDACTED] v2: " * (n - 4)
                + "auth: v1: v2: " * 4
                + "[REDACTED]"
            ),
        ),
        (
            "url_userinfo_many_amps",
            lambda n: "https://token:S" + "&k=v" * n + "@example.test/repo",
            lambda n: "https://token:[REDACTED]@example.test/repo",
        ),
        (
            "url_multi_at_many_amps",
            lambda n: "https://user:a@b" + "&k=v" * n + "@example.test/repo",
            lambda n: "https://user:[REDACTED]@example.test/repo",
        ),
        (
            "repeated_auth_tight_double_colon",
            lambda n: "auth:auth:" * n + "field",
            lambda n: "auth:[REDACTED]",
        ),
        (
            "repeated_auth_space_double_colon",
            lambda n: "auth: auth: " * n + "field",
            lambda n: "auth: [REDACTED] " * n + "field",
        ),
        (
            "repeated_auth_oauth2_creds_nl",
            lambda n: "auth: oauth2: credentials:\n" * n + "password: S3CR3T",
            lambda n: (
                "auth: oauth2: credentials:\n" * n + "password: [REDACTED]"
            ),
        ),
        (
            "repeated_auth_nl_env",
            lambda n: "auth:\nenv: " * n + "field",
            lambda n: (
                "auth:\n[REDACTED] " * (n - 1) + "auth:\nenv: [REDACTED]"
            ),
        ),
        (
            "repeated_auth_v1_nl",
            lambda n: "auth: v1:\n" * n + "field",
            lambda n: "auth: v1:\n" * n + "[REDACTED]",
        ),
        (
            "repeated_auth_auth_v1_sp",
            lambda n: "auth: auth/v1: " * n + "field",
            lambda n: "auth: [REDACTED] " * n + "field",
        ),
        (
            "repeated_auth_v1_auth_tight",
            lambda n: "auth/v1:auth:" * n + "field",
            lambda n: "auth/v1:[REDACTED]",
        ),
        (
            "repeated_auth_oauth2_auth_sp",
            lambda n: "auth: oauth2: auth: " * n + "field",
            lambda n: (
                "auth: [REDACTED] auth: [REDACTED] oauth2: [REDACTED] "
                * (n // 2 - 1)
                + "auth: [REDACTED] auth: [REDACTED] oauth2: auth: [REDACTED]"
            ),
        ),
        (
            "repeated_auth_nl_env_nl",
            lambda n: "auth:\nenv:\n" * n + "field",
            lambda n: "auth:\nenv:\n" * n + "[REDACTED]",
        ),
        (
            "repeated_user_auth_v1_pw",
            lambda n: 'user_auth/v1:password="S" ' * n,
            lambda n: 'user_auth/v1:password="[REDACTED]" ' * n,
        ),
        (
            "repeated_secret_store_v1_pw",
            lambda n: 'secret_store:v1:password="S" ' * n,
            lambda n: 'secret_store:v1:password="[REDACTED]" ' * n,
        ),
        (
            "repeated_auth_creds_mm_quote",
            lambda n: 'auth: credentials:\n  "password\': "S"\n' * (n // 4),
            lambda n: (
                'auth: credentials:\n  "password\': "[REDACTED]"\n' * (n // 4)
            ),
        ),
        (
            "repeated_auth_v1_nl_b64_eq",
            lambda n: "auth: v1:\nc2VjcmV0=\n" * (n // 4),
            lambda n: "auth: v1:\n[REDACTED]\n" * (n // 4),
        ),
        (
            "repeated_auth_oauth2_4l_key",
            lambda n: (
                'auth: oauth2:\n  v1:\n    v2:\n      key="S"\n' * (n // 4)
            ),
            lambda n: (
                'auth: oauth2:\n  v1:\n    v2:\n      key="[REDACTED]"\n'
                * (n // 4)
            ),
        ),
        (
            "mismatched_long_upper_run_key",
            lambda n: (
                'authentication:\n  "aa-'
                + ("A" * n)
                + 'apassword--1\': "S3CR3T"'
            ),
            lambda n: (
                'authentication:\n  "aa-'
                + ("A" * n)
                + 'apassword--1\': "[REDACTED]"'
            ),
        ),
    ):
      with self.subTest(linear_scaling_4k_vs_16k=label):
        small_payload = payload_fn(4000)
        large_payload = payload_fn(16000)
        scrub_sensitive_text(small_payload)
        small_times: list[float] = []
        large_times: list[float] = []
        out_small = ""
        out_large = ""
        for _ in range(5):
          t_small = time.perf_counter()
          out_small = scrub_sensitive_text(small_payload)
          small_times.append(time.perf_counter() - t_small)
          t_large = time.perf_counter()
          out_large = scrub_sensitive_text(large_payload)
          large_times.append(time.perf_counter() - t_large)
        dt_small = sorted(small_times)[2]
        dt_large = sorted(large_times)[2]
        self.assertEqual(out_small, expected_fn(4000))
        self.assertEqual(out_large, expected_fn(16000))
        self.assertLess(dt_large, 5.0)
        self.assertLess(dt_large / max(dt_small, 1e-4), 8.0)

    # 5. Review 4 regressions (even-backslash JSON keys, prose comma, long
    #    mismatched keys, JSON string header lookalikes, tight container =,
    #    userinfo &name=, linear container head, HTTP method + path).
    for label, sample, secret_val, must_keep in (
        (
            "even_2bs_open_quote_nl_nl",
            r'{\\"password":' + "\n\n" + 'S3CR3T,"error_code":"CARD_EXPIRED"}',
            "S3CR3T",
            '"error_code":"CARD_EXPIRED"',
        ),
        (
            "even_4bs_open_quote_nl_nl",
            r'{\\\\"password":'
            + "\n\n"
            + 'S3CR3T,"error_code":"CARD_EXPIRED"}',
            "S3CR3T",
            '"error_code":"CARD_EXPIRED"',
        ),
        (
            "even_2bs_open_quote_escaped_nl_nl",
            r'{\\"password":\n\nS3CR3T,"error_code":"CARD_EXPIRED"}',
            "S3CR3T",
            '"error_code":"CARD_EXPIRED"',
        ),
        (
            "even_2bs_open_quote_post_budget",
            budget_prefix
            + r'{\\"password":'
            + "\n\n"
            + 'S3CR3T,"error_code":"CARD_EXPIRED"}',
            "S3CR3T",
            '"error_code":"CARD_EXPIRED"',
        ),
        (
            "json_comma_after_digit_nl_nl",
            budget_prefix + '{"a": 1, "password":\n\n S3CR3T}',
            "S3CR3T",
            '"a": 1',
        ),
        (
            "json_comma_after_string_nl_nl",
            budget_prefix + '{"a": "x", "password":\n\n S3CR3T}',
            "S3CR3T",
            '"a": "x"',
        ),
        (
            "json_comma_after_bool_nl_nl",
            budget_prefix + '{"a": true, "password":\n\n S3CR3T}',
            "S3CR3T",
            '"a": true',
        ),
        (
            "long_1025_mismatched_edq_key",
            r"\"" + "a" * 1016 + '.password": "S3CR3T" CARD_EXPIRED',
            "S3CR3T",
            "CARD_EXPIRED",
        ),
        (
            "long_1025_mismatched_edq_key_post_budget",
            budget_prefix
            + r"\""
            + "a" * 1016
            + '.password": "S3CR3T" CARD_EXPIRED',
            "S3CR3T",
            "CARD_EXPIRED",
        ),
        (
            "long_1025_mismatched_dq_esc_close",
            '"' + "a" * 1016 + r'.password\": "S3CR3T" CARD_EXPIRED',
            "S3CR3T",
            "CARD_EXPIRED",
        ),
        (
            "leading_ws_mismatched_edq_sq_key",
            r"\" password\': " + '"S3CR3T" CARD_EXPIRED',
            "S3CR3T",
            "CARD_EXPIRED",
        ),
        (
            "post_budget_json_header_lookalike_str",
            budget_prefix + '{"password":\n"SampleMarker_password\': one",'
            '"error_code":"CARD_EXPIRED"}',
            "SampleMarker_password",
            '"error_code":"CARD_EXPIRED"',
        ),
        (
            "oversized_17k_json_header_lookalike_str",
            '{"pad":"'
            + "a" * 17000
            + '","password":\n"SampleMarker_password\': one",'
            '"error_code":"CARD_EXPIRED"}',
            "SampleMarker_password",
            '"error_code":"CARD_EXPIRED"',
        ),
        (
            "bare_key_nl_header_lookalike_str",
            'password:\n"SampleMarker_password\': one"',
            "SampleMarker_password",
            'password:\n"[REDACTED]"',
        ),
        (
            "bare_key_nl_escaped_dq_lookalike_str",
            r"password:" + "\n" + r"\"SampleMarker_password" + "': one\"",
            "SampleMarker_password",
            "[REDACTED]",
        ),
    ):
      with self.subTest(review4_guard=label):
        out = scrub_sensitive_text(sample)
        self.assertNotIn(secret_val, out)
        self.assertIn("[REDACTED]", out)
        if must_keep:
          self.assertIn(must_keep, out)

    for s3_label, s3_payload, s3_secret, s3_keep in (
        (
            "header_lookalike",
            budget_prefix + '{"password":\n"SampleMarker_password\': one",'
            '"error_code":"CARD_EXPIRED"}',
            "SampleMarker_password",
            "CARD_EXPIRED",
        ),
        (
            "json_nextline_bare_kv",
            budget_prefix + '{"password":\ntoken: S3CR3T}',
            "S3CR3T",
            "token",
        ),
    ):
      with self.subTest(review4_s3=s3_label):
        s3_out = extract_s3_outcome_first(
            {
                "turns": [
                    {
                        "turn_index": 0,
                        "turn_id": f"inv_{s3_label}",
                        "events": [
                            {
                                "author": "customer_ops_agent",
                                "content": {
                                    "role": "model",
                                    "parts": [{"text": s3_payload}],
                                },
                            }
                        ],
                    }
                ]
            },
            metrics={"error_summary": s3_payload},
        )
        self.assertNotIn(s3_secret, s3_out)
        self.assertIn(s3_keep, s3_out)
        self.assertIn("[REDACTED]", s3_out)

    for sample, expected in (
        (
            'Field, "password":\n\nERROR: CARD_EXPIRED',
            'Field, "password":\n\nERROR: CARD_EXPIRED',
        ),
        (
            'I typed, "password":\n\nERROR: CARD_EXPIRED',
            'I typed, "password":\n\nERROR: CARD_EXPIRED',
        ),
        (
            '{"msg": "Field, \\"password\\":\\n\\nERROR: CARD_EXPIRED"}',
            '{"msg": "Field, \\"password\\":\\n\\nERROR: CARD_EXPIRED"}',
        ),
        (
            'auth/v1:password="SampleMarker"',
            'auth/v1:password="[REDACTED]"',
        ),
        (
            'authentication:\n  auth/v1:password="SampleMarker"',
            'authentication:\n  auth/v1:password="[REDACTED]"',
        ),
        (
            'auth:password="SampleMarker"',
            'auth:password="[REDACTED]"',
        ),
        (
            'auth:v1:password="SampleMarker"',
            'auth:v1:password="[REDACTED]"',
        ),
        (
            "auth/v1:password=SampleMarker",
            "auth/v1:password=[REDACTED]",
        ),
        (
            'auth/v1:password={"k":"v"}',
            "auth/v1:password=[REDACTED]",
        ),
        (
            "secret:Zm9vYmFy= CARD_EXPIRED",
            "secret:[REDACTED] CARD_EXPIRED",
        ),
        (
            "auth:password=, CARD_EXPIRED",
            "auth:[REDACTED], CARD_EXPIRED",
        ),
        (
            "auth:password=\n\nCARD_EXPIRED",
            "auth:[REDACTED]\n\nCARD_EXPIRED",
        ),
        (
            '{"password":\ntoken: S3CR3T}',
            '{"password":\ntoken: [REDACTED]}',
        ),
        (
            budget_prefix + '{"password":\ntoken: S3CR3T}',
            budget_prefix + '{"password":\ntoken: [REDACTED]}',
        ),
        (
            '{"password":\n"token": "S3CR3T"}',
            '{"password":\n"token": "[REDACTED]"}',
        ),
        (
            'password:\n"SampleMarker_password\': one:"',
            'password:\n"[REDACTED]"',
        ),
        (
            'password:\n"SampleMarker_password\': one="',
            'password:\n"[REDACTED]"',
        ),
        (
            'password:\n"SampleMarker_password\': one is"',
            'password:\n"[REDACTED]"',
        ),
        (
            'password:\n"SampleMarker_password\': "',
            'password:\n"[REDACTED]"',
        ),
        (
            '"password":\n"SampleMarker_password\': "',
            '"password":\n"[REDACTED]"',
        ),
        (
            "password:\n\"SampleMarker_password': one",
            "password:\n\"SampleMarker_password': [REDACTED]",
        ),
        (
            'authentication:\n  "password": "S3CR3T"',
            'authentication:\n  "password": "[REDACTED]"',
        ),
        (
            r"authentication:" + '\n  "password\\": "S3CR3T"',
            r"authentication:" + '\n  "password\\": "[REDACTED]"',
        ),
        (
            "authentication:\n  \"password': S3CR3T",
            "authentication:\n  \"password': [REDACTED]",
        ),
        (
            "GET /v1/password/: S3CR3T",
            "GET /v1/password/: [REDACTED]",
        ),
        (
            "GET /oauth/token/: S3CR3T",
            "GET /oauth/token/: [REDACTED]",
        ),
        (
            "GET /v1/auth/verify/: CARD_EXPIRED",
            "GET /v1/auth/verify/: CARD_EXPIRED",
        ),
        (
            "https://token:SampleMarker&error_code=CARD_EXPIRED@example.test/repo",
            "https://token:[REDACTED]@example.test/repo",
        ),
        (
            "https://user:SampleMarker&author=me@example.test/repo",
            "https://user:[REDACTED]@example.test/repo",
        ),
        (
            "https://user:SampleMarker&token=v@example.test/repo",
            "https://user:[REDACTED]@example.test/repo",
        ),
        (
            "https://token:foo,secret=SampleMarker@example.test/repo",
            "https://token:[REDACTED]@example.test/repo",
        ),
        (
            "https://token:foo;secret=SampleMarker@example.test/repo",
            "https://token:[REDACTED]@example.test/repo",
        ),
        (
            "https://user:1234&part=v@example.test/repo",
            "https://user:[REDACTED]@example.test/repo",
        ),
        (
            "https://token:12345&part=v@example.test/repo",
            "https://token:[REDACTED]@example.test/repo",
        ),
        (
            "https://token:12345,part=v@example.test/repo",
            "https://token:[REDACTED]@example.test/repo",
        ),
        (
            "https://user:8443&part=v@example.test/repo",
            "https://user:[REDACTED]@example.test/repo",
        ),
        (
            "https://alice.smith:1234&part=v@example.test/repo",
            "https://alice.smith:[REDACTED]@example.test/repo",
        ),
        (
            "https://example.com:8443,email=alice@example.com",
            "https://example.com:8443,email=alice@example.com",
        ),
        (
            "https://example.com:8443&token=SampleSecret&next=alice@example.com",
            "https://example.com:8443&token=[REDACTED]",
        ),
        (
            "https://example.com:8443&email=user@corp.com/x",
            "https://example.com:8443&email=user@corp.com/x",
        ),
        (
            "https://example.com:8443&token=SampleSecret&next=alice@example.com/callback",
            "https://example.com:8443&token=[REDACTED]",
        ),
        (
            "https://alice.smith:SampleMarker&status=1@example.test",
            "https://alice.smith:[REDACTED]@example.test",
        ),
        (
            "https://alice.smith:SampleMarker&token=v@example.test",
            "https://alice.smith:[REDACTED]@example.test",
        ),
        (
            "https://alice.smith:SampleMarker&author=me@example.test",
            "https://alice.smith:[REDACTED]@example.test",
        ),
        (
            "https://localhost:SampleMarker&token=v@example.test",
            "https://localhost:[REDACTED]@example.test",
        ),
        (
            "https://alice.smith:1234&part=v@example.test",
            "https://alice.smith:[REDACTED]@example.test",
        ),
        (
            "https://alice.smith:9999&token=SECRET123@example.test",
            "https://alice.smith:[REDACTED]@example.test",
        ),
        (
            "https://user:sec@ret&part=v@example.test/repo",
            "https://user:[REDACTED]@example.test/repo",
        ),
        (
            "https://user:p@ss&part=v@example.test/repo",
            "https://user:[REDACTED]@example.test/repo",
        ),
        (
            "https://user:pass@host.example.com:8443,email=alice@corp.com",
            "https://user:[REDACTED]@host.example.com:8443,email=alice@corp.com",
        ),
        (
            "https://user:pass@host.example.com,email=alice@corp.com",
            "https://user:[REDACTED]@host.example.com,email=alice@corp.com",
        ),
        (
            "https://localhost:8080&token=SampleSecret&email=alice@example.com",
            "https://localhost:8080&token=[REDACTED]",
        ),
        (
            "https://host:nonport&token=SampleSecret",
            "https://host:nonport&token=[REDACTED]",
        ),
        (
            "https://example.com:8443) email=alice@example.com",
            "https://example.com:8443) email=alice@example.com",
        ),
        (
            'auth/v1:password= "SampleMarker"',
            'auth/v1:password= "[REDACTED]"',
        ),
        (
            "auth/v1:password= SampleMarker",
            "auth/v1:password= [REDACTED]",
        ),
        (
            'auth/v1:password= {"k": "SampleMarker"}',
            "auth/v1:password= [REDACTED]",
        ),
        (
            'auth/v1:password= ["SampleMarker"]',
            "auth/v1:password= [REDACTED]",
        ),
        (
            'auth/v1:password=\n"SampleMarker"',
            'auth/v1:password=\n"[REDACTED]"',
        ),
        (
            'authentication:\n  auth/v1:password= "SampleMarker"',
            'authentication:\n  auth/v1:password= "[REDACTED]"',
        ),
        (
            'auth:password= "SampleMarker"',
            'auth:password= "[REDACTED]"',
        ),
        (
            'SECRETS:PROD:DB_PASSWORD= "SampleMarker"',
            'SECRETS:PROD:DB_PASSWORD= "[REDACTED]"',
        ),
        (
            'auth:v1:v2:v3:password="SampleMarker"',
            'auth:v1:v2:v3:password="[REDACTED]"',
        ),
        (
            "auth:v1:v2:v3:password: SampleMarker",
            "auth:v1:v2:v3:password: [REDACTED]",
        ),
        (
            "auth/v1:error_code=CARD_EXPIRED",
            "auth/v1:error_code=CARD_EXPIRED",
        ),
        (
            "auth/v1: error_code=CARD_EXPIRED",
            "auth/v1: error_code=CARD_EXPIRED",
        ),
        (
            'auth/v1: password="SampleMarker"',
            'auth/v1: password="[REDACTED]"',
        ),
        (
            'auth/v1: password: "SampleMarker"',
            'auth/v1: password: "[REDACTED]"',
        ),
        (
            'auth/v1: password={"k": "SampleMarker"}',
            "auth/v1: password=[REDACTED]",
        ),
        (
            'auth: password="SampleMarker"',
            'auth: password="[REDACTED]"',
        ),
        (
            'authentication:\n  auth/v1: password="SampleMarker"',
            'authentication:\n  auth/v1: password="[REDACTED]"',
        ),
        (
            "auth: oauth2:access_token: S3CR3T",
            "auth: oauth2:access_token: [REDACTED]",
        ),
        (
            'auth: v1:password="SampleMarker"',
            'auth: v1:password="[REDACTED]"',
        ),
        (
            '{"credentials.status": "S3CR3T"}',
            '{"credentials.status":"[REDACTED]"}',
        ),
        (
            "auth:status=S3CR3T",
            "auth:status=[REDACTED]",
        ),
        (
            "auth/v1:status=S3CR3T",
            "auth/v1:status=[REDACTED]",
        ),
        (
            "credentials:status=S3CR3T_VAL",
            "credentials:status=[REDACTED]",
        ),
        (
            "credentials:status: S3CR3T_VAL",
            "credentials:status: [REDACTED]",
        ),
        (
            "credentials:status: ok",
            "credentials:status: ok",
        ),
        (
            r"password:" + "\n" + r"\"SampleMarker_password\': \"CARD_EXPIRED",
            r"password:" + "\n" + r"\"[REDACTED]\"CARD_EXPIRED",
        ),
        (
            'password:\n\\"SampleMarker_password\': \\"CARD_EXPIRED',
            'password:\n\\"[REDACTED]\\"CARD_EXPIRED',
        ),
        (
            r"{\"password\":"
            + "\n"
            + r"\"SampleMarker_password\': \"two\",\"error_code\":\"CARD_EXPIRED\"}",
            r"{\"password\":"
            + "\n"
            + r"\"SampleMarker_password\': \"[REDACTED]\",\"error_code\":\"CARD_EXPIRED\"}",
        ),
        (
            'password:\n"SampleMarker_password\': "CARD_EXPIRED',
            'password:\n"[REDACTED]"CARD_EXPIRED',
        ),
        (
            '"password":\n"SampleMarker_password\': "CARD_EXPIRED',
            '"password":\n"[REDACTED]"CARD_EXPIRED',
        ),
        (
            r"{\"password\":"
            + "\n"
            + r'\"SampleMarker_password": one\",\"error_code\":\"CARD_EXPIRED\"}',
            r"{\"password\":"
            + "\n"
            + r'[REDACTED]": one\",\"error_code\":\"CARD_EXPIRED\"}',
        ),
        (
            "auth:\n  \"SampleMarker_password': one\ntail_code: OK",
            "auth:\n  \"SampleMarker_password': [REDACTED]\ntail_code: OK",
        ),
        (
            "secret:\n  \"SampleMarker_password': one",
            "secret:\n  \"SampleMarker_password': [REDACTED]",
        ),
        (
            "authentication:\n  \"SampleMarker_password': one",
            "authentication:\n  \"SampleMarker_password': [REDACTED]",
        ),
        (
            'auth/v1:key="SampleMarker"',
            'auth/v1:key="[REDACTED]"',
        ),
        (
            'auth:v1:key="SampleMarker"',
            'auth:v1:key="[REDACTED]"',
        ),
        (
            'auth/v1: key="SampleMarker"',
            'auth/v1: key="[REDACTED]"',
        ),
        (
            'auth:v1: key="SampleMarker"',
            'auth:v1: key="[REDACTED]"',
        ),
        (
            'auth: v1:key="SampleMarker"',
            'auth: v1:key="[REDACTED]"',
        ),
        (
            'auth/v1:code="SampleMarker"',
            'auth/v1:code="[REDACTED]"',
        ),
        (
            'auth/v1:header="SampleMarker"',
            'auth/v1:header="[REDACTED]"',
        ),
        (
            'authentication:\n  auth/v1:key="SampleMarker"',
            'authentication:\n  auth/v1:key="[REDACTED]"',
        ),
        (
            'SECRETS:PROD: KEY="SampleMarker"',
            'SECRETS:PROD: KEY="[REDACTED]"',
        ),
        (
            'auth/v1:status="SampleMarker"',
            'auth/v1:status="[REDACTED]"',
        ),
        (
            'credentials:status="SampleMarker"',
            'credentials:status="[REDACTED]"',
        ),
        (
            "auth/v1:status=SampleMarker",
            "auth/v1:status=[REDACTED]",
        ),
        (
            'auth/v1:status={"k": "SampleMarker"}',
            "auth/v1:status=[REDACTED]",
        ),
        (
            'secrets:db="SampleMarker"',
            'secrets:db="[REDACTED]"',
        ),
        (
            'auth:status="SampleMarker"',
            'auth:status="[REDACTED]"',
        ),
        (
            r"auth:password=\nbenign_key=CARD_EXPIRED",
            r"auth:[REDACTED]\nbenign_key=CARD_EXPIRED",
        ),
        (
            r"auth:password=\nerror_code=CARD_EXPIRED",
            r"auth:[REDACTED]\nerror_code=CARD_EXPIRED",
        ),
        (
            "auth: v1:error_code=CARD_EXPIRED",
            "auth: v1:error_code=CARD_EXPIRED",
        ),
        (
            "auth/v1: error_code = CARD_EXPIRED",
            "auth/v1: error_code = CARD_EXPIRED",
        ),
        (
            "auth/v1:error_code = CARD_EXPIRED",
            "auth/v1:error_code = CARD_EXPIRED",
        ),
        (
            "https://user:p@ss.dev&part=v@example.test/repo",
            "https://user:[REDACTED]@example.test/repo",
        ),
        (
            "https://ops.internal:1234&part=v@corp.com/repo",
            "https://ops.internal:[REDACTED]@corp.com/repo",
        ),
        (
            "https://10.0.0.1:8080&part=S@corp.com/v1",
            "https://10.0.0.1:[REDACTED]@corp.com/v1",
        ),
        (
            "https://localhost:1234&part=v@example.test/repo",
            "https://localhost:[REDACTED]@example.test/repo",
        ),
        (
            "https://bot.dev:1234&part=v@example.test/repo",
            "https://bot.dev:[REDACTED]@example.test/repo",
        ),
        (
            "https://svc.io:9000&part=v@example.test/repo",
            "https://svc.io:[REDACTED]@example.test/repo",
        ),
        (
            "https://alice.dev:8080&token=SEC@prod.example.com:8443/api",
            "https://alice.dev:8080&token=[REDACTED]",
        ),
        (
            "https://alice.smith:8080&token=SEC@prod.example.com:8443/api",
            "https://alice.smith:[REDACTED]@prod.example.com:8443/api",
        ),
        (
            "http://localhost:1234&part=S3CR3T@example.test",
            "http://localhost:[REDACTED]@example.test",
        ),
        (
            "https://user:pass@site.co.uk,email=alice@corp.com",
            "https://user:[REDACTED]@site.co.uk,email=alice@corp.com",
        ),
        (
            (
                "https://api.example.co.uk:8443&token=SampleSecret"
                "&next=alice@example.com/callback"
            ),
            "https://api.example.co.uk:8443&token=[REDACTED]",
        ),
        (
            "https://example.com:8443&password=p@ssw0rd",
            "https://example.com:8443&password=[REDACTED]",
        ),
        (
            "url=https://example.com:8443,password=p@ssw0rd",
            "url=https://example.com:8443,password=[REDACTED]",
        ),
        (
            "https://token:SampleMarker&token=SEC@internal-host",
            "https://token:[REDACTED]@internal-host",
        ),
        (
            "https://example.com:8443&password=p@ss/word",
            "https://example.com:8443&password=[REDACTED]",
        ),
        (
            "https://example.com:8443&password=mySecret@corp.com",
            "https://example.com:8443&password=[REDACTED]",
        ),
        (
            "https://example.com:8443,password=p@ssw0rd,error_code=CARD_EXPIRED",
            "https://example.com:8443,password=[REDACTED],error_code=CARD_EXPIRED",
        ),
        (
            "https://example.com:8443;token=p@ssw0rd",
            "https://example.com:8443;token=[REDACTED]",
        ),
        (
            "https://user:p@ss.dev&status_code=1@example.test/repo",
            "https://user:[REDACTED]@example.test/repo",
        ),
        (
            "https://user:p@ss.dev&token=S3CR3T@example.test/repo",
            "https://user:[REDACTED]@ss.dev&token=[REDACTED]",
        ),
        (
            "https://user:p@ss.dev&email=x@example.test/repo",
            "https://user:[REDACTED]@ss.dev&email=x@example.test/repo",
        ),
        (
            "https://user:p@ss&token=S3CR3T@example.test/repo",
            "https://user:[REDACTED]@example.test/repo",
        ),
        (
            "https://user:p@ss&email=x@example.test/repo",
            "https://user:[REDACTED]@example.test/repo",
        ),
        (
            "https://user:p@ss.dev:8080&part=v@example.test/repo",
            "https://user:[REDACTED]@example.test/repo",
        ),
        (
            "https://user:p@10.0.0.1&status_code=1@example.test/repo",
            "https://user:[REDACTED]@example.test/repo",
        ),
        (
            "https://user:p@localhost&email=x@example.test/repo",
            "https://user:[REDACTED]@localhost&email=x@example.test/repo",
        ),
        (
            "https://user:p@site.co.uk&email=x@example.test/repo",
            "https://user:[REDACTED]@site.co.uk&email=x@example.test/repo",
        ),
        (
            "https://alice.dev:8080&error_code=123&part=v@prod.example.com/api",
            "https://alice.dev:[REDACTED]@prod.example.com/api",
        ),
        (
            "https://localhost:8080&error_code=123&part=v@prod.example.com/api",
            "https://localhost:[REDACTED]@prod.example.com/api",
        ),
        (
            "https://10.0.0.1:8080&error_code=123&part=v@prod.example.com/api",
            "https://10.0.0.1:[REDACTED]@prod.example.com/api",
        ),
        (
            "https://alice.dev:8080&token=SEC&part=1@prod.example.com/api",
            "https://alice.dev:[REDACTED]@prod.example.com/api",
        ),
        (
            "https://token:SampleMarker&error_code=1@internal-host/repo",
            "https://token:[REDACTED]@internal-host/repo",
        ),
        (
            'auth: v1: key="SampleMarker"',
            'auth: v1: key="[REDACTED]"',
        ),
        (
            "auth: v1: error_code = CARD_EXPIRED",
            "auth: v1: error_code = CARD_EXPIRED",
        ),
        (
            'auth:oauth2: password="SampleMarker"',
            'auth:oauth2: password="[REDACTED]"',
        ),
        (
            'v1:auth: password="SampleMarker"',
            'v1:auth: password="[REDACTED]"',
        ),
        (
            "auth:oauth2: error_code=CARD_EXPIRED",
            "auth:oauth2: error_code=CARD_EXPIRED",
        ),
        (
            "auth: oauth2: access_token: S3CR3T",
            "auth: oauth2: access_token: [REDACTED]",
        ),
        (
            'auth: credentials: password="SampleMarker"',
            'auth: credentials: password="[REDACTED]"',
        ),
        (
            "auth: oauth2: error_code=CARD_EXPIRED",
            "auth: oauth2: error_code=CARD_EXPIRED",
        ),
        (
            'auth:v1: v2: key="SampleMarker"',
            'auth:v1: v2: key="[REDACTED]"',
        ),
        (
            'auth/v1: v2: v3: key="SampleMarker"',
            'auth/v1: v2: v3: key="[REDACTED]"',
        ),
        (
            'auth: v1: v2: key="SampleMarker"',
            'auth: v1: v2: key="[REDACTED]"',
        ),
        (
            'auth/v1:Password="SampleMarker"',
            'auth/v1:Password="[REDACTED]"',
        ),
        (
            'auth:Password="SampleMarker"',
            'auth:Password="[REDACTED]"',
        ),
        (
            'auth/v1:AccessToken="SampleMarker"',
            'auth/v1:AccessToken="[REDACTED]"',
        ),
        (
            'auth/v1:mySecretVal="SampleMarker"',
            'auth/v1:mySecretVal="[REDACTED]"',
        ),
        (
            'auth/v1:custom_organization_secret_field="SampleMarker"',
            'auth/v1:custom_organization_secret_field="[REDACTED]"',
        ),
        (
            "https://example.com:8443&next=alice@example.com/cb",
            "https://example.com:8443&next=alice@example.com/cb",
        ),
        (
            "https://example.com:8443&redirect=alice@example.com/cb",
            "https://example.com:8443&redirect=alice@example.com/cb",
        ),
        (
            "https://example.com:8443&callback=alice@example.com/cb",
            "https://example.com:8443&callback=alice@example.com/cb",
        ),
        (
            "https://example.com:8443&next=alice@example.com",
            "https://example.com:8443&next=alice@example.com",
        ),
        (
            "https://example.com:8443&foo=bar&email=alice@example.com/cb",
            "https://example.com:8443&foo=bar&email=alice@example.com/cb",
        ),
        (
            "https://example.com:8443&foo=bar&next=alice@example.com/cb",
            "https://example.com:8443&foo=bar&next=alice@example.com/cb",
        ),
        (
            "https://example.com:8443&client_id=app1&password=myP@ss.dev",
            "https://example.com:8443&client_id=app1&password=[REDACTED]",
        ),
        (
            "https://example.com:8443,id=1,api_key=part1@part2.io",
            "https://example.com:8443,id=1,api_key=[REDACTED]",
        ),
        (
            "https://example.com:8443&password=mySecret@corp.com/callback",
            "https://example.com:8443&password=[REDACTED]",
        ),
        (
            "https://example.com:8443&password=p@ss.com/word",
            "https://example.com:8443&password=[REDACTED]",
        ),
        (
            "https://example.com:8443&password=p@corp.com/v1",
            "https://example.com:8443&password=[REDACTED]",
        ),
        (
            "https://example.com:8443&token=tok@example.com/v1",
            "https://example.com:8443&token=[REDACTED]",
        ),
        (
            "https://example.com:8443&api_key=k@corp.io/v2",
            "https://example.com:8443&api_key=[REDACTED]",
        ),
        (
            "https://alice.dev:8080&error_code=123@prod.example.com",
            "https://alice.dev:[REDACTED]@prod.example.com",
        ),
        (
            "https://token:SampleMarker&error_code=1@internal-host",
            "https://token:[REDACTED]@internal-host",
        ),
        (
            "https://user:p@ss.dev&part=1@example.test",
            "https://user:[REDACTED]@example.test",
        ),
        (
            "https://user:p@ss.dev&status_code=1@example.test",
            "https://user:[REDACTED]@example.test",
        ),
        (
            "https://user:p@ss.dev:8080&part=v@example.test",
            "https://user:[REDACTED]@example.test",
        ),
        (
            r"password:" + "\n" + r"\"SampleMarker_password\': \" CARD_EXPIRED",
            r"password:" + "\n" + r"\"[REDACTED]\" CARD_EXPIRED",
        ),
        (
            'password:\n"SampleMarker_password\': " CARD_EXPIRED',
            'password:\n"[REDACTED]" CARD_EXPIRED',
        ),
        (
            '"password":\n"SampleMarker_password\': " CARD_EXPIRED',
            '"password":\n"[REDACTED]" CARD_EXPIRED',
        ),
        (
            r'{"password":'
            + "\n"
            + r'"SampleMarker_password'
            + '\': ="\\n,"error_code":"CARD_EXPIRED"}',
            r'{"password":'
            + "\n"
            + '"[REDACTED]"\\n,"error_code":"CARD_EXPIRED"}',
        ),
        (
            "secret:\n\"SampleMarker_password': one",
            "secret:\n\"SampleMarker_password': [REDACTED]",
        ),
        (
            budget_prefix + '{"secret":\n"SampleMarker_password\': one\ntwo",'
            '"error_code":"CARD_EXPIRED"}',
            budget_prefix
            + '{"secret":\n"[REDACTED]","error_code":"CARD_EXPIRED"}',
        ),
        (
            budget_prefix + '{"auth":\n"SampleMarker_password\': one\ntwo",'
            '"error_code":"CARD_EXPIRED"}',
            budget_prefix
            + '{"auth":\n"[REDACTED]","error_code":"CARD_EXPIRED"}',
        ),
        (
            budget_prefix
            + '{"a":"x","password":\n\nS3CR3T,"error_code":"CARD_EXPIRED"}',
            budget_prefix
            + '{"a":"x","password":\n\n[REDACTED],"error_code":"CARD_EXPIRED"}',
        ),
        (
            budget_prefix + '{"a": "", "password":\n\nS3CR3T}',
            budget_prefix + '{"a": "", "password":\n\n[REDACTED]}',
        ),
        (
            '{"a":"","password":\n\nS3CR3T}',
            '{"a":"","password":\n\n[REDACTED]}',
        ),
        (
            '{"a": "x\\"y", "password":\n\nS3CR3T,"error_code":"CARD_EXPIRED"}',
            '{"a": "x\\"y", "password":\n\n[REDACTED],"error_code":"CARD_EXPIRED"}',
        ),
        (
            "He said 'hello', \"password\":\n\nERROR: CARD_EXPIRED",
            "He said 'hello', \"password\":\n\nERROR: CARD_EXPIRED",
        ),
        (
            r'{\\"a": \\"x", \\"password":'
            + '\n\nS3CR3T,"error_code":"CARD_EXPIRED"}',
            r'{\\"a": \\"x", \\"password":'
            + '\n\n[REDACTED],"error_code":"CARD_EXPIRED"}',
        ),
        (
            "get /v1/auth/verify: CARD_EXPIRED",
            "get /v1/auth/verify: CARD_EXPIRED",
        ),
        (
            "get /v1/password/: S3CR3T",
            "get /v1/password/: [REDACTED]",
        ),
        (
            'If true, "password":\n\nERROR: CARD_EXPIRED',
            'If true, "password":\n\nERROR: CARD_EXPIRED',
        ),
        (
            'Got HTTP 401, "password":\n\nERROR: CARD_EXPIRED',
            'Got HTTP 401, "password":\n\nERROR: CARD_EXPIRED',
        ),
        (
            'See item 1, "password":\n\nERROR: CARD_EXPIRED',
            'See item 1, "password":\n\nERROR: CARD_EXPIRED',
        ),
        (
            'Section "auth", "password":\n\nERROR: CARD_EXPIRED',
            'Section "auth", "password":\n\nERROR: CARD_EXPIRED',
        ),
        (
            'He said "hello", "password":\n\nERROR: CARD_EXPIRED',
            'He said "hello", "password":\n\nERROR: CARD_EXPIRED',
        ),
        (
            '{"a": true, "password":\n\nS3CR3T}',
            '{"a": true, "password":\n\n[REDACTED]}',
        ),
        (
            '{"a": 401, "password":\n\nS3CR3T}',
            '{"a": 401, "password":\n\n[REDACTED]}',
        ),
        (
            '{"a": "auth", "password":\n\nS3CR3T}',
            '{"a": "auth", "password":\n\n[REDACTED]}',
        ),
        (
            r"{\"a\": \"x\", \"password\":" + "\n\nS3CR3T}",
            r"{\"a\": \"x\", \"password\":" + "\n\n[REDACTED]}",
        ),
        (
            '{"pad": "' + "x" * 17000 + '", "password":\n\nS3CR3T}',
            '{"pad": "' + "x" * 17000 + '", "password":\n\n[REDACTED]}',
        ),
        (
            "https://token:SampleMarker&part=v@example.test/repo",
            "https://token:[REDACTED]@example.test/repo",
        ),
        (
            "https://oauth2:abc&x=1@gitlab.com/g",
            "https://oauth2:[REDACTED]@gitlab.com/g",
        ),
        (
            "https://token:ab,c=d@example.test/repo",
            "https://token:[REDACTED]@example.test/repo",
        ),
        (
            "GET /v1/auth/verify: CARD_EXPIRED",
            "GET /v1/auth/verify: CARD_EXPIRED",
        ),
        (
            "POST /oauth/token/status: 503",
            "POST /oauth/token/status: 503",
        ),
        (
            "GET /v1/password: S3CR3T",
            "GET /v1/password: [REDACTED]",
        ),
    ):
      with self.subTest(review4_exact=sample):
        self.assertEqual(scrub_sensitive_text(sample), expected)

    self.assertEqual(
        scrub_sensitive_data({"credentials.status": "S3CR3T"}),
        {"credentials.status": "[REDACTED]"},
    )
    self.assertFalse(_is_likely_url_hostname(".com"))
    self.assertTrue(_is_likely_url_hostname("good-1.com"))
    self.assertTrue(_is_likely_url_hostname("example.com"))
    self.assertTrue(_is_likely_url_hostname("site.co.uk"))
    self.assertTrue(_is_likely_url_hostname("10.0.0.1"))
    self.assertTrue(_is_likely_url_hostname("localhost"))
    self.assertTrue(_is_likely_url_hostname("api.mycompany.xyz"))
    self.assertTrue(_is_likely_url_hostname("svc.cluster.localdomain"))
    for host in (
        "alice.smith",
        "ret",
        "ss",
        "ssw0rd",
        "999.0.0.1",
        "-bad.com",
        "bad-.com",
        "foo=bar.com",
        "a..b.com",
        "my_host.com",
        "localhost.",
    ):
      with self.subTest(unlikely_url_hostname=host):
        self.assertFalse(_is_likely_url_hostname(host))

    self.assertTrue(_is_valid_dns_host_syntax("backend"))
    self.assertTrue(_is_valid_dns_host_syntax("alice.smith"))
    for host in (
        "999.0.0.1",
        "foo=bar.com",
        "a..b.com",
        "my_host.com",
        "localhost.",
    ):
      with self.subTest(invalid_dns_host_syntax=host):
        self.assertFalse(_is_valid_dns_host_syntax(host))

    for sample, expected in (
        (
            'auth: oauth2:\nkey="SampleMarker"',
            'auth: oauth2:\nkey="[REDACTED]"',
        ),
        (
            "auth: oauth2:\nerror_code=CARD_EXPIRED",
            "auth: oauth2:\nerror_code=CARD_EXPIRED",
        ),
        (
            "auth: oauth2:\n  password: S3CR3T",
            "auth: oauth2:\n  password: [REDACTED]",
        ),
        (
            'auth: oauth2:\n{"k": "S3CR3T"}',
            "auth: oauth2:\n[REDACTED]",
        ),
        (
            'auth: oauth2:\n{"password": "S3CR3T"}',
            "auth: oauth2:\n[REDACTED]",
        ),
        (
            "auth: oauth2: S3CR3T",
            "auth: oauth2: [REDACTED]",
        ),
        (
            'auth:oauth2:v1:password="SampleMarker"',
            'auth:oauth2:v1:password="[REDACTED]"',
        ),
        (
            'auth:oauth2:v1:key="SampleMarker"',
            'auth:oauth2:v1:key="[REDACTED]"',
        ),
        (
            'auth:oauth2:v1: key="SampleMarker"',
            'auth:oauth2:v1: key="[REDACTED]"',
        ),
        (
            'auth:oauth2:v1: password="SampleMarker"',
            'auth:oauth2:v1: password="[REDACTED]"',
        ),
        (
            "auth:oauth2:v1:error_code=CARD_EXPIRED",
            "auth:oauth2:v1:error_code=CARD_EXPIRED",
        ),
        (
            "auth:oauth2:error_code=CARD_EXPIRED",
            "auth:oauth2:error_code=CARD_EXPIRED",
        ),
        (
            "auth: v1: client_id=app1",
            "auth: v1: client_id=app1",
        ),
        (
            "auth: v1: http_method=POST",
            "auth: v1: http_method=POST",
        ),
        (
            "auth: v1: region=us-central1",
            "auth: v1: region=us-central1",
        ),
        (
            'auth:v1/v2:password="SampleMarker"',
            'auth:v1/v2:password="[REDACTED]"',
        ),
        (
            'auth: v1/v2:password="SampleMarker"',
            'auth: v1/v2:password="[REDACTED]"',
        ),
        (
            'auth:app.v1:password="SampleMarker"',
            'auth:app.v1:password="[REDACTED]"',
        ),
        (
            'auth:oauth2/v1:password="SampleMarker"',
            'auth:oauth2/v1:password="[REDACTED]"',
        ),
        (
            'authentication:auth/v1:password="SampleMarker"',
            'authentication:auth/v1:password="[REDACTED]"',
        ),
        (
            'auth:PROD:password="SampleMarker"',
            'auth:PROD:password="[REDACTED]"',
        ),
        (
            'auth:PROD:key="SampleMarker"',
            'auth:PROD:key="[REDACTED]"',
        ),
        (
            'auth: auth: key="SampleMarker"',
            'auth: auth: key="[REDACTED]"',
        ),
        (
            'auth: auth: custom_val="SampleMarker"',
            'auth: auth: custom_val="[REDACTED]"',
        ),
        (
            (
                "auth: oauth2: credentials: authorization: "
                'password="SampleMarker"'
            ),
            'auth: oauth2: credentials: authorization: password="[REDACTED]"',
        ),
        (
            "auth:oauth2:S3CR3T",
            "auth:[REDACTED]",
        ),
        (
            "auth:auth:S3CR3T",
            "auth:[REDACTED]",
        ),
        (
            "auth/v1:MyHeader = SampleMarker",
            "auth/v1:MyHeader = [REDACTED]",
        ),
        (
            "auth/v1:PrivateVal = SampleMarker",
            "auth/v1:PrivateVal = [REDACTED]",
        ),
        (
            "auth: PrivateVal = SampleMarker",
            "auth: PrivateVal = [REDACTED]",
        ),
        (
            "auth: v1: PrivateVal = SampleMarker",
            "auth: v1: PrivateVal = [REDACTED]",
        ),
        (
            "auth/v1:MyCustomKey=SampleMarker",
            "auth/v1:MyCustomKey=[REDACTED]",
        ),
        (
            "secret:YWJjZGU= CARD_EXPIRED",
            "secret:[REDACTED] CARD_EXPIRED",
        ),
        (
            "auth: v1: cookie_count=3",
            "auth: v1: cookie_count=3",
        ),
        (
            "auth: v1: token_count=5",
            "auth: v1: token_count=5",
        ),
        (
            "auth: v1: auth_count=2",
            "auth: v1: auth_count=2",
        ),
        (
            "auth: v1: cookie_count=S3CR3T",
            "auth: v1: cookie_count=[REDACTED]",
        ),
        (
            "auth: v1: status=OK",
            "auth: v1: status=OK",
        ),
        (
            "auth: v1: reason=invalid",
            "auth: v1: reason=invalid",
        ),
        (
            "https://example.com:8443&api_key=sec@ret:1234",
            "https://example.com:8443&api_key=[REDACTED]",
        ),
        (
            "https://backend:8443&password=myP@ss/w0rd",
            "https://backend:8443&password=[REDACTED]",
        ),
        (
            "https://api.mycompany.xyz:8443&password=myP@ss/w0rd",
            "https://api.mycompany.xyz:8443&password=[REDACTED]",
        ),
        (
            (
                "https://example.com:8443&password="
                "mySecret@corp.com:8443/callback"
            ),
            "https://example.com:8443&password=[REDACTED]",
        ),
        (
            "https://example.com:8443&token=mySecret@localhost:8080/cb",
            "https://example.com:8443&token=[REDACTED]",
        ),
        (
            (
                "https://example.com:8443&token=S3CR3T"
                "&email=alice@corp.com:8443/cb"
            ),
            "https://example.com:8443&token=[REDACTED]",
        ),
        (
            (
                "https://user:pass@example.com:8443"
                "&password=part1@part2.io/part3Secret"
            ),
            "https://user:[REDACTED]@example.com:8443&password=[REDACTED]",
        ),
        (
            (
                "https://alice@example.com:8443"
                "&password=part1@part2.io/part3Secret"
            ),
            "https://alice@example.com:8443&password=[REDACTED]",
        ),
        (
            "https://alice@example.com&password=part1@part2.io/part3Secret",
            "https://alice@example.com&password=[REDACTED]",
        ),
        (
            (
                "https://user:pass@example.com:8443,"
                "error_code=CARD_EXPIRED,next=alice@example.com/cb"
            ),
            (
                "https://user:[REDACTED]@example.com:8443,"
                "error_code=CARD_EXPIRED,next=alice@example.com/cb"
            ),
        ),
        (
            "https://user:pass@backend:8443&password=p@ss/w0rd",
            "https://user:[REDACTED]@backend:8443&password=[REDACTED]",
        ),
        (
            "https://backend:8443&flag&password=p@ss/w0rd",
            "https://backend:8443&flag&password=[REDACTED]",
        ),
        (
            "https://user:S3CR3T&token=VAL@internal-host",
            "https://user:[REDACTED]@internal-host",
        ),
        (
            "https://user:myPass&error_code=1&password=SEC@service-box",
            "https://user:[REDACTED]@service-box",
        ),
        (
            "https://localhost:SampleMarker&token=v@internal-host",
            "https://localhost:[REDACTED]@internal-host",
        ),
        (
            "https://alice.dev:8080&error_code=123@internal-host",
            "https://alice.dev:[REDACTED]@internal-host",
        ),
        (
            "https://user:p@ss.dev&part=1@mid.dev&next=alice@example.com",
            "https://user:[REDACTED]@mid.dev&next=alice@example.com",
        ),
        (
            "https://user:p@ss.dev&part=1@mid.dev&token=SEC@example.com",
            "https://user:[REDACTED]@mid.dev&token=[REDACTED]",
        ),
        (
            "https://user:p@ss.dev:8080&flag&part=1@example.test",
            "https://user:[REDACTED]@example.test",
        ),
        (
            "https://user:p@ss.dev&error_code=1@internal-host",
            "https://user:[REDACTED]@internal-host",
        ),
        (
            (
                "url=https://user:pass@host.example.com:8443,"
                "email=alice@corp.com,password=myP@ssword123"
            ),
            (
                "url=https://user:[REDACTED]@host.example.com:8443,"
                "email=alice@corp.com,password=[REDACTED]"
            ),
        ),
        (
            (
                "https://user:pass@example.com:8443"
                "&password=sec@ret@MyRealSecret123"
            ),
            "https://user:[REDACTED]@example.com:8443&password=[REDACTED]",
        ),
        (
            (
                "url=https://user:pass@host.example.com:8443,"
                "email=alice@corp.com,error_code=CARD_EXPIRED,"
                "recipient=bob@corp.com"
            ),
            (
                "url=https://user:[REDACTED]@host.example.com:8443,"
                "email=alice@corp.com,error_code=CARD_EXPIRED,"
                "recipient=bob@corp.com"
            ),
        ),
        (
            (
                "url=https://user:pass@host.example.com,"
                "email=alice@corp.com,password=myP@ssword123"
            ),
            (
                "url=https://user:[REDACTED]@host.example.com,"
                "email=alice@corp.com,password=[REDACTED]"
            ),
        ),
        (
            (
                "https://user:pass@example.com"
                "&password=part1@part2.io/part3Secret"
            ),
            "https://user:[REDACTED]@example.com&password=[REDACTED]",
        ),
        (
            (
                "https://user:pass@example.com,"
                "error_code=CARD_EXPIRED,next=alice@example.com/cb"
            ),
            (
                "https://user:[REDACTED]@example.com,"
                "error_code=CARD_EXPIRED,next=alice@example.com/cb"
            ),
        ),
        (
            "url=https://user:pass@host.example.com,next=alice@corp.com/cb",
            (
                "url=https://user:[REDACTED]@host.example.com,"
                "next=alice@corp.com/cb"
            ),
        ),
        (
            (
                "https://user:pass@example.com&flag"
                "&password=part1@part2.io/part3Secret"
            ),
            "https://user:[REDACTED]@example.com&flag&password=[REDACTED]",
        ),
        (
            (
                "https://user:p@ss@example.com:8443"
                "&password=part1@part2.io/part3Secret"
            ),
            "https://user:[REDACTED]@example.com:8443&password=[REDACTED]",
        ),
        (
            (
                "https://user:p@ss@example.com:8443,"
                "error_code=CARD_EXPIRED,next=alice@example.com/cb"
            ),
            (
                "https://user:[REDACTED]@example.com:8443,"
                "error_code=CARD_EXPIRED,next=alice@example.com/cb"
            ),
        ),
        (
            "https://alice@bad_token:S3CR3T@corp.com/api",
            "https://alice@bad_token:[REDACTED]@corp.com/api",
        ),
        (
            "https://alice@example.com:SecretPass123@prod.corp.com/api",
            "https://alice@example.com:[REDACTED]@prod.corp.com/api",
        ),
        (
            "https://alice@corp.com:S3CR3T@prod.example.com",
            "https://alice@corp.com:[REDACTED]@prod.example.com",
        ),
        (
            "https://alice@corp.com:8443&part=1@prod.example.com/api",
            "https://alice@corp.com:[REDACTED]@prod.example.com/api",
        ),
        (
            "https://user:1234&secret@internal-host",
            "https://user:[REDACTED]@internal-host",
        ),
        (
            "https://user:8080,pw@internal-host",
            "https://user:[REDACTED]@internal-host",
        ),
        (
            "https://user:1234&pw@internal-host",
            "https://user:[REDACTED]@internal-host",
        ),
        (
            "https://user:1234&abc@ssw0rd",
            "https://user:[REDACTED]@ssw0rd",
        ),
        (
            'auth: env: key="SampleMarker"',
            'auth: env: key="[REDACTED]"',
        ),
        (
            'auth: region: key="SampleMarker"',
            'auth: region: key="[REDACTED]"',
        ),
        (
            'auth: client_id: key="SampleMarker"',
            'auth: client_id: key="[REDACTED]"',
        ),
        (
            'auth: oauth2: env: key="SampleMarker"',
            'auth: oauth2: env: key="[REDACTED]"',
        ),
        (
            'auth: oauth2: region: key="SampleMarker"',
            'auth: oauth2: region: key="[REDACTED]"',
        ),
        (
            'auth: v1: env: key="SampleMarker"',
            'auth: v1: env: key="[REDACTED]"',
        ),
        (
            "auth: env: S3CR3T",
            "auth: env: [REDACTED]",
        ),
        (
            "auth: v1: HTTPMethod=POST",
            "auth: v1: HTTPMethod=POST",
        ),
        (
            'auth: oauth2: credentials:\nkey="SampleMarker"',
            'auth: oauth2: credentials:\nkey="[REDACTED]"',
        ),
        (
            'auth: v1: oauth2:\nkey="SampleMarker"',
            'auth: v1: oauth2:\nkey="[REDACTED]"',
        ),
        (
            'oauth2:\nkey="SampleMarker"',
            'oauth2:\nkey="[REDACTED]"',
        ),
        (
            'auth/v1:\nkey="SampleMarker"',
            'auth/v1:\nkey="[REDACTED]"',
        ),
        (
            "auth: oauth2: credentials:\nerror_code=CARD_EXPIRED",
            "auth: oauth2: credentials:\nerror_code=CARD_EXPIRED",
        ),
        (
            "auth: v1: oauth2:\nerror_code=CARD_EXPIRED",
            "auth: v1: oauth2:\nerror_code=CARD_EXPIRED",
        ),
        (
            "oauth2:\nerror_code=CARD_EXPIRED",
            "oauth2:\nerror_code=CARD_EXPIRED",
        ),
        (
            "auth/v1:\nerror_code=CARD_EXPIRED",
            "auth/v1:\nerror_code=CARD_EXPIRED",
        ),
        (
            "auth/v1:Key1=SampleMarker",
            "auth/v1:Key1=[REDACTED]",
        ),
        (
            "auth/v1:MyVal1=SampleMarker",
            "auth/v1:MyVal1=[REDACTED]",
        ),
        (
            "auth: MyVal1=SampleMarker",
            "auth: MyVal1=[REDACTED]",
        ),
        (
            "auth: MyVal1 = SampleMarker",
            "auth: MyVal1 = [REDACTED]",
        ),
        (
            'auth: MyVal1= "SampleMarker"',
            'auth: MyVal1= "[REDACTED]"',
        ),
        (
            "auth/v1.status=OK",
            "auth/v1.status=OK",
        ),
        (
            "auth/v1.status=S3CR3T",
            "auth/v1.status=[REDACTED]",
        ),
        (
            "auth/v1.cookie_count=3",
            "auth/v1.cookie_count=3",
        ),
        (
            "auth:\nstatus=OK",
            "auth:\nstatus=OK",
        ),
        (
            "auth:\nstatus=S3CR3T",
            "auth:\nstatus=[REDACTED]",
        ),
        (
            "auth: v1: session_id=S3CR3T",
            "auth: v1: session_id=[REDACTED]",
        ),
        (
            "https://user:p@ss.dev&flag@example.test",
            "https://user:[REDACTED]@example.test",
        ),
        (
            "https://user:pass@site.co.uk,alice@corp.com",
            "https://user:[REDACTED]@site.co.uk,alice@corp.com",
        ),
        (
            "https://user:pass@site.co.uk;alice@corp.com",
            "https://user:[REDACTED]@site.co.uk;alice@corp.com",
        ),
        (
            (
                "https://user:pass@backend:8443"
                "&password=part1@part2.io/part3Secret"
            ),
            "https://user:[REDACTED]@backend:8443&password=[REDACTED]",
        ),
        (
            "https://user:pass@backend:8443&password=mySecret@corp.com",
            "https://user:[REDACTED]@backend:8443&password=[REDACTED]",
        ),
        (
            (
                "https://user:pass@backend:8443,"
                "error_code=CARD_EXPIRED,next=alice@example.com/cb"
            ),
            (
                "https://user:[REDACTED]@backend:8443,"
                "error_code=CARD_EXPIRED,next=alice@example.com/cb"
            ),
        ),
        (
            (
                "https://user:pass@svc.cluster.localdomain:8443"
                "&password=part1@part2.io/part3Secret"
            ),
            (
                "https://user:[REDACTED]@svc.cluster.localdomain:8443"
                "&password=[REDACTED]"
            ),
        ),
        (
            (
                "https://user:pass@svc.cluster.localdomain"
                "&password=part1@part2.io/part3Secret"
            ),
            (
                "https://user:[REDACTED]@svc.cluster.localdomain"
                "&password=[REDACTED]"
            ),
        ),
        (
            (
                "https://user:pass@db.internal-cluster:8443"
                "&password=mySecret@corp.com"
            ),
            (
                "https://user:[REDACTED]@db.internal-cluster:8443"
                "&password=[REDACTED]"
            ),
        ),
        (
            (
                "https://user:pass@internal-box:8443,"
                "error_code=CARD_EXPIRED,next=alice@example.com/cb"
            ),
            (
                "https://user:[REDACTED]@internal-box:8443,"
                "error_code=CARD_EXPIRED,next=alice@example.com/cb"
            ),
        ),
        (
            'tenant:auth:\nkey="SampleMarker"',
            'tenant:auth:\nkey="[REDACTED]"',
        ),
        (
            "tenant:auth:\nerror_code=CARD_EXPIRED",
            "tenant:auth:\nerror_code=CARD_EXPIRED",
        ),
        (
            'auth: v1:\nkey="SampleMarker"',
            'auth: v1:\nkey="[REDACTED]"',
        ),
        (
            "auth: v1:\nerror_code=CARD_EXPIRED",
            "auth: v1:\nerror_code=CARD_EXPIRED",
        ),
        (
            'auth: env:\nkey="SampleMarker"',
            'auth: env:\nkey="[REDACTED]"',
        ),
        (
            'auth: oauth2:\n  v1: key="SampleMarker"',
            'auth: oauth2:\n  v1: key="[REDACTED]"',
        ),
        (
            'auth:\nenv: key="SampleMarker"',
            'auth:\nenv: key="[REDACTED]"',
        ),
        (
            'auth:\nv1: key="SampleMarker"',
            'auth:\nv1: key="[REDACTED]"',
        ),
        (
            "auth:\n  v1: v2: error_code=CARD_EXPIRED",
            "auth:\n  v1: v2: error_code=CARD_EXPIRED",
        ),
        (
            'auth: oauth2:\n  credentials: key="SampleMarker"',
            'auth: oauth2:\n  credentials: key="[REDACTED]"',
        ),
        (
            "auth: v1:\n  password: S3CR3T",
            "auth: v1:\n  password: [REDACTED]",
        ),
        (
            'auth: v1:\n{"password": "S3CR3T"}',
            "auth: v1:\n[REDACTED]",
        ),
        (
            "tenant:auth:\n  password: S3CR3T",
            "tenant:auth:\n  password: [REDACTED]",
        ),
        (
            "auth/v1:My1Key=SampleMarker",
            "auth/v1:My1Key=[REDACTED]",
        ),
        (
            "auth/v1:V1Key=SampleMarker",
            "auth/v1:V1Key=[REDACTED]",
        ),
        (
            "auth/v1:ApiV2=SampleMarker",
            "auth/v1:ApiV2=[REDACTED]",
        ),
        (
            "auth/v1:My_Key_1=SampleMarker",
            "auth/v1:My_Key_1=[REDACTED]",
        ),
        (
            "auth: v1: My1Key=SampleMarker",
            "auth: v1: My1Key=[REDACTED]",
        ),
        (
            "auth: v1: My_Key_1=SampleMarker",
            "auth: v1: My_Key_1=[REDACTED]",
        ),
        (
            "secret:c2VjcmV0= CARD_EXPIRED",
            "secret:[REDACTED] CARD_EXPIRED",
        ),
        (
            'authentication:\n  "myApp.password\': "SampleMarker"',
            'authentication:\n  "myApp.password\': "[REDACTED]"',
        ),
        (
            'authentication:\n  \\"myApp.password\': \\"SampleMarker\\"',
            'authentication:\n  \\"myApp.password\': \\"[REDACTED]\\"',
        ),
        (
            (
                'authentication:\n  \\"'
                + ("a" * 1020)
                + '.password\': \\"SampleMarker\\"'
            ),
            (
                'authentication:\n  \\"'
                + ("a" * 1020)
                + '.password\': \\"[REDACTED]\\"'
            ),
        ),
        (
            (
                'authentication:\n  "'
                + ("a" * 1020)
                + '.password\': "SampleMarker"'
            ),
            (
                'authentication:\n  "'
                + ("a" * 1020)
                + '.password\': "[REDACTED]"'
            ),
        ),
        (
            "secret:abcdefg= CARD_EXPIRED",
            "secret:[REDACTED] CARD_EXPIRED",
        ),
        (
            "secret:AbcdEfg= CARD_EXPIRED",
            "secret:[REDACTED] CARD_EXPIRED",
        ),
        (
            "secret:a1b2c3d= CARD_EXPIRED",
            "secret:[REDACTED] CARD_EXPIRED",
        ),
        (
            "secret:abc123d= CARD_EXPIRED",
            "secret:[REDACTED] CARD_EXPIRED",
        ),
        (
            "auth:abcdefg= CARD_EXPIRED",
            "auth:[REDACTED] CARD_EXPIRED",
        ),
        (
            "credentials:abcdefg= CARD_EXPIRED",
            "credentials:[REDACTED] CARD_EXPIRED",
        ),
        (
            "oauth2:abcdefg= CARD_EXPIRED",
            "oauth2:[REDACTED] CARD_EXPIRED",
        ),
        (
            "auth:password= SampleMarker",
            "auth:password= [REDACTED]",
        ),
        (
            "auth:key= SampleMarker",
            "auth:key= [REDACTED]",
        ),
        (
            'auth:custom_val="SampleMarker"',
            'auth:custom_val="[REDACTED]"',
        ),
        (
            "auth/v1:v1Key=SampleMarker",
            "auth/v1:v1Key=[REDACTED]",
        ),
        (
            "auth/v1:s3Key=SampleMarker",
            "auth/v1:s3Key=[REDACTED]",
        ),
        (
            "auth/v1:k8sKey=SampleMarker",
            "auth/v1:k8sKey=[REDACTED]",
        ),
        (
            "auth/v1:Key1a=SampleMarker",
            "auth/v1:Key1a=[REDACTED]",
        ),
        (
            "auth/v1:ApiV2b=SampleMarker",
            "auth/v1:ApiV2b=[REDACTED]",
        ),
        (
            "auth/v1:JWTKey=SampleMarker",
            "auth/v1:JWTKey=[REDACTED]",
        ),
        (
            'auth: auth/v1:password="SampleMarker"',
            'auth: auth/v1:password="[REDACTED]"',
        ),
        (
            'auth:auth/v1:password="SampleMarker"',
            'auth:auth/v1:password="[REDACTED]"',
        ),
        (
            'auth/v1:auth:password="SampleMarker"',
            'auth/v1:auth:password="[REDACTED]"',
        ),
        (
            'auth: auth/v1: key="SampleMarker"',
            'auth: auth/v1: key="[REDACTED]"',
        ),
        (
            'auth/v1:auth: key="SampleMarker"',
            'auth/v1:auth: key="[REDACTED]"',
        ),
        (
            "auth:\n  auth/v1:error_code=CARD_EXPIRED",
            "auth:\n  auth/v1:error_code=CARD_EXPIRED",
        ),
        (
            "auth:\n  auth/v1: error_code=CARD_EXPIRED",
            "auth:\n  auth/v1: error_code=CARD_EXPIRED",
        ),
        (
            "auth: auth/v1:error_code=CARD_EXPIRED",
            "auth: auth/v1:error_code=CARD_EXPIRED",
        ),
        (
            'user_auth/v1:password="SampleMarker"',
            'user_auth/v1:password="[REDACTED]"',
        ),
        (
            'db_credentials:password="SampleMarker"',
            'db_credentials:password="[REDACTED]"',
        ),
        (
            'app:user_auth/v1:password="SampleMarker"',
            'app:user_auth/v1:password="[REDACTED]"',
        ),
        (
            'api_key:token="SampleMarker"',
            'api_key:token="[REDACTED]"',
        ),
        (
            'secret_config:api_key="SampleMarker"',
            'secret_config:api_key="[REDACTED]"',
        ),
        (
            'user_auth/v1:key="SampleMarker"',
            'user_auth/v1:key="[REDACTED]"',
        ),
        (
            "user_auth/v1:error_code=CARD_EXPIRED",
            "user_auth/v1:error_code=CARD_EXPIRED",
        ),
        (
            "client_secret:Zm9vYmFy= CARD_EXPIRED",
            "client_secret:[REDACTED] CARD_EXPIRED",
        ),
        (
            "auth:\n  oauth2: error_code=CARD_EXPIRED",
            "auth:\n  oauth2: error_code=CARD_EXPIRED",
        ),
        (
            "auth:\n  oauth2: client_id=app1",
            "auth:\n  oauth2: client_id=app1",
        ),
        (
            "auth:\n  oauth2: env=prod",
            "auth:\n  oauth2: env=prod",
        ),
        (
            "auth:\n  credentials: error_code=CARD_EXPIRED",
            "auth:\n  credentials: error_code=CARD_EXPIRED",
        ),
        (
            "auth:\n  oauth2: v1: error_code=CARD_EXPIRED",
            "auth:\n  oauth2: v1: error_code=CARD_EXPIRED",
        ),
        (
            "auth:\n  oauth2: credentials: error_code=CARD_EXPIRED",
            "auth:\n  oauth2: credentials: error_code=CARD_EXPIRED",
        ),
        (
            "auth:\n  oauth2: region: error_code=CARD_EXPIRED",
            "auth:\n  oauth2: region: error_code=CARD_EXPIRED",
        ),
        (
            "oauth2:\n  credentials: error_code=CARD_EXPIRED",
            "oauth2:\n  credentials: error_code=CARD_EXPIRED",
        ),
        (
            "tenant:auth:\n  oauth2: error_code=CARD_EXPIRED",
            "tenant:auth:\n  oauth2: error_code=CARD_EXPIRED",
        ),
        (
            "auth: v1:\n  oauth2: error_code=CARD_EXPIRED",
            "auth: v1:\n  oauth2: error_code=CARD_EXPIRED",
        ),
        (
            "auth:\n  oauth2: S3CR3T",
            "auth:\n  oauth2: [REDACTED]",
        ),
        (
            'auth:\n  oauth2: {"token": "S3CR3T"}',
            "auth:\n  oauth2: [REDACTED]",
        ),
        (
            'auth: oauth2:\n  v1:\n  key="SampleMarker"',
            'auth: oauth2:\n  v1:\n  key="[REDACTED]"',
        ),
        (
            'auth:\nenv:\nkey="SampleMarker"',
            'auth:\nenv:\nkey="[REDACTED]"',
        ),
        (
            'auth:\nv1:\nkey="SampleMarker"',
            'auth:\nv1:\nkey="[REDACTED]"',
        ),
        (
            "auth:\n  v1:\n  error_code=CARD_EXPIRED",
            "auth:\n  v1:\n  error_code=CARD_EXPIRED",
        ),
        (
            "auth: env:\nS3CR3T",
            "auth: env:\n[REDACTED]",
        ),
        (
            "auth: v1:\nS3CR3T",
            "auth: v1:\n[REDACTED]",
        ),
        (
            "auth:\nenv: S3CR3T",
            "auth:\nenv: [REDACTED]",
        ),
        (
            "auth:\nv1: S3CR3T",
            "auth:\nv1: [REDACTED]",
        ),
        (
            "db_credentials:error_code=CARD_EXPIRED",
            "db_credentials:error_code=CARD_EXPIRED",
        ),
        (
            "secret:key_file=abc123",
            "secret:[REDACTED]",
        ),
        (
            "auth:key_file=abc123",
            "auth:[REDACTED]",
        ),
        (
            'v1/authentication:\n  "password\': "S3CR3T"',
            'v1/authentication:\n  "password\': "[REDACTED]"',
        ),
        (
            'credentials: oauth2:\n  "password\': "S3CR3T"',
            'credentials: oauth2:\n  "password\': "[REDACTED]"',
        ),
        (
            'authentication: v1:\n  "password\': "S3CR3T"',
            'authentication: v1:\n  "password\': "[REDACTED]"',
        ),
        (
            'auth:\n  "password\': "SampleMarker"',
            'auth:\n  "password\': "[REDACTED]"',
        ),
        (
            'auth:\n  "myApp.password\': "SampleMarker"',
            'auth:\n  "myApp.password\': "[REDACTED]"',
        ),
        (
            'authentication:\n  "ClientSecret\': "S3CR3T"',
            'authentication:\n  "ClientSecret\': "[REDACTED]"',
        ),
        (
            'authentication:\n  "oauth2Password\': "S3CR3T"',
            'authentication:\n  "oauth2Password\': "[REDACTED]"',
        ),
        (
            'authentication:\n  "Password\': "SampleMarker"',
            'authentication:\n  "Password\': "[REDACTED]"',
        ),
        (
            'authentication:\n  "serviceAccount.ApiKey\': "SampleMarker"',
            'authentication:\n  "serviceAccount.ApiKey\': "[REDACTED]"',
        ),
        (
            'authentication:\n  "auth.password1\': "SampleMarker"',
            'authentication:\n  "auth.password1\': "[REDACTED]"',
        ),
        (
            'authentication:\n  "password1\': "SampleMarker"',
            'authentication:\n  "password1\': "[REDACTED]"',
        ),
        (
            'authentication:\n  "auth_token_1\': "SampleMarker"',
            'authentication:\n  "auth_token_1\': "[REDACTED]"',
        ),
        (
            'authentication:\n  "v1.ApiKey\': "SampleMarker"',
            'authentication:\n  "v1.ApiKey\': "[REDACTED]"',
        ),
        (
            'credentials:\n  "Password\': "SampleMarker"',
            'credentials:\n  "Password\': "[REDACTED]"',
        ),
        (
            'oauth2:\n  "serviceAccount.ApiKey\': "SampleMarker"',
            'oauth2:\n  "serviceAccount.ApiKey\': "[REDACTED]"',
        ),
        (
            'auth: credentials:\n  "password\': "S3CR3T"',
            'auth: credentials:\n  "password\': "[REDACTED]"',
        ),
        (
            'tenant:authentication:\n  "password\': "S3CR3T"',
            'tenant:authentication:\n  "password\': "[REDACTED]"',
        ),
        (
            'auth: oauth2:\n  "password\': "S3CR3T"',
            'auth: oauth2:\n  "password\': "[REDACTED]"',
        ),
        (
            'auth: v1:\n  "password\': "S3CR3T"',
            'auth: v1:\n  "password\': "[REDACTED]"',
        ),
        (
            'secret:\n  "password\': "S3CR3T"',
            'secret:\n  "password\': "[REDACTED]"',
        ),
        (
            'authorization:\n  "password\': "S3CR3T"',
            'authorization:\n  "password\': "[REDACTED]"',
        ),
        (
            'authentication:\n  "SampleMarker_password\': "CARD_EXPIRED',
            'authentication:\n  "[REDACTED]"CARD_EXPIRED',
        ),
        (
            'secret:\n  "SampleMarker_password\': "CARD_EXPIRED',
            'secret:\n  "[REDACTED]"CARD_EXPIRED',
        ),
        (
            "secret: abcdefg= CARD_EXPIRED",
            "secret: [REDACTED] CARD_EXPIRED",
        ),
        (
            "secret: AbcdEfg= CARD_EXPIRED",
            "secret: [REDACTED] CARD_EXPIRED",
        ),
        (
            "auth: eyJhbGciOiJIUzI1NiJ9= CARD_EXPIRED",
            "auth: [REDACTED] CARD_EXPIRED",
        ),
        (
            "secret:\n  AbcdEfg= CARD_EXPIRED",
            "secret:\n  [REDACTED] CARD_EXPIRED",
        ),
        (
            "secret:\n  abcdefg= CARD_EXPIRED",
            "secret:\n  [REDACTED] CARD_EXPIRED",
        ),
        (
            "credentials: abcdefg= CARD_EXPIRED",
            "credentials: [REDACTED] CARD_EXPIRED",
        ),
        (
            "authorization: AbcdEfg= CARD_EXPIRED",
            "authorization: [REDACTED] CARD_EXPIRED",
        ),
        (
            "oauth2: abcdefg= CARD_EXPIRED",
            "oauth2: [REDACTED] CARD_EXPIRED",
        ),
        (
            "auth/v1:eyJhbGciOiJIUzI1NiJ9= CARD_EXPIRED",
            "auth/v1:[REDACTED] CARD_EXPIRED",
        ),
        (
            "auth/v1:s3BucketKey=SampleMarker",
            "auth/v1:s3BucketKey=[REDACTED]",
        ),
        (
            "auth/v1:v1Key1=SampleMarker",
            "auth/v1:v1Key1=[REDACTED]",
        ),
        (
            "auth/v1:v1_Key=SampleMarker",
            "auth/v1:v1_Key=[REDACTED]",
        ),
        (
            "auth/v1:s3_Key=SampleMarker",
            "auth/v1:s3_Key=[REDACTED]",
        ),
        (
            "auth/v1:Key1val=SampleMarker",
            "auth/v1:Key1val=[REDACTED]",
        ),
        (
            "auth/v1:ApiV2beta=SampleMarker",
            "auth/v1:ApiV2beta=[REDACTED]",
        ),
        (
            "auth/v1:V1beta=SampleMarker",
            "auth/v1:V1beta=[REDACTED]",
        ),
        (
            "auth/v1:SHA256Key=SampleMarker",
            "auth/v1:SHA256Key=[REDACTED]",
        ),
        (
            "auth/v1:HTTP2Key=SampleMarker",
            "auth/v1:HTTP2Key=[REDACTED]",
        ),
        (
            "auth: v1:\nc2VjcmV0=",
            "auth: v1:\n[REDACTED]",
        ),
        (
            "auth: v1:\n  c2VjcmV0=",
            "auth: v1:\n  [REDACTED]",
        ),
        (
            "auth: env:\nYWJjZGU=",
            "auth: env:\n[REDACTED]",
        ),
        (
            "auth:\n  v1:\n  c2VjcmV0=",
            "auth:\n  v1:\n  [REDACTED]",
        ),
        (
            "auth: v1:\nc2VjcmV0= CARD_EXPIRED",
            "auth: v1:\n[REDACTED] CARD_EXPIRED",
        ),
        (
            "auth: v1:\nerror_code= CARD_EXPIRED",
            "auth: v1:\nerror_code= CARD_EXPIRED",
        ),
        (
            'auth: oauth2:\n  v1:\n    v2:\n      key="SampleMarker"',
            'auth: oauth2:\n  v1:\n    v2:\n      key="[REDACTED]"',
        ),
        (
            'user_auth/v1: password="SampleMarker"',
            'user_auth/v1: password="[REDACTED]"',
        ),
        (
            'user_auth/v1: password: "SampleMarker"',
            'user_auth/v1: password: "[REDACTED]"',
        ),
        (
            'secret_store:v1:password="SampleMarker"',
            'secret_store:v1:password="[REDACTED]"',
        ),
        (
            'secret_config: password="SampleMarker"',
            'secret_config: password="[REDACTED]"',
        ),
        (
            'user_credentials/v1: password="SampleMarker"',
            'user_credentials/v1: password="[REDACTED]"',
        ),
        (
            'app_secret/v1: password="SampleMarker"',
            'app_secret/v1: password="[REDACTED]"',
        ),
        (
            'db_credentials: password="SampleMarker"',
            'db_credentials: password="[REDACTED]"',
        ),
        (
            "secret_store:v1:error_code=CARD_EXPIRED",
            "secret_store:v1:error_code=CARD_EXPIRED",
        ),
        (
            "user_auth/v1: error_code=CARD_EXPIRED",
            "user_auth/v1: error_code=CARD_EXPIRED",
        ),
        (
            "secret_store:v1: S3CR3T",
            "secret_store:v1: [REDACTED]",
        ),
        (
            "secret_store:v1:S3CR3T",
            "secret_store:[REDACTED]",
        ),
        (
            "api_key: abcdefg= CARD_EXPIRED",
            "api_key: [REDACTED] CARD_EXPIRED",
        ),
        (
            'authentication:\n  "Client_Secret\': "SampleMarker"',
            'authentication:\n  "Client_Secret\': "[REDACTED]"',
        ),
        (
            'auth: oauth2:\n  "Client_Secret\': "SampleMarker"',
            'auth: oauth2:\n  "Client_Secret\': "[REDACTED]"',
        ),
        (
            'authentication:\n  "Api_Key\': "SampleMarker"',
            'authentication:\n  "Api_Key\': "[REDACTED]"',
        ),
        (
            'authentication:\n  "Auth_Token_1\': "SampleMarker"',
            'authentication:\n  "Auth_Token_1\': "[REDACTED]"',
        ),
        (
            'authentication:\n  "myApp.Access_Token\': "SampleMarker"',
            'authentication:\n  "myApp.Access_Token\': "[REDACTED]"',
        ),
        (
            'auth:\n  "OAuthClientSecret\': "SampleMarker"',
            'auth:\n  "OAuthClientSecret\': "[REDACTED]"',
        ),
        (
            'auth:\n  "OAuthToken\': "SampleMarker"',
            'auth:\n  "OAuthToken\': "[REDACTED]"',
        ),
        (
            'auth:\n  "XApiKey\': "SampleMarker"',
            'auth:\n  "XApiKey\': "[REDACTED]"',
        ),
        (
            'auth:\n  "iOSPassword\': "SampleMarker"',
            'auth:\n  "iOSPassword\': "[REDACTED]"',
        ),
        (
            'auth:\n  "authTokenURL\': "SampleMarker"',
            'auth:\n  "authTokenURL\': "[REDACTED]"',
        ),
        (
            'authentication:\n  "X-Api-Key\': "SampleMarker"',
            'authentication:\n  "X-Api-Key\': "[REDACTED]"',
        ),
        (
            (
                "authentication:\n  \"PrimaryServiceAccountClientSecretKey':"
                ' "SampleMarker"'
            ),
            (
                "authentication:\n  \"PrimaryServiceAccountClientSecretKey':"
                ' "[REDACTED]"'
            ),
        ),
        (
            'authentication:\n  "myApp:password\': "SampleMarker"',
            'authentication:\n  "myApp:password\': "[REDACTED]"',
        ),
        (
            'auth:\n  "sub:token\': "SampleMarker"',
            'auth:\n  "sub:token\': "[REDACTED]"',
        ),
        (
            'auth:\n  "oauth2:password\': "SampleMarker"',
            'auth:\n  "oauth2:password\': "[REDACTED]"',
        ),
        (
            'db_credentials:\n  "password\': "SampleMarker"',
            'db_credentials:\n  "password\': "[REDACTED]"',
        ),
        (
            'user_auth:\n  "password\': "SampleMarker"',
            'user_auth:\n  "password\': "[REDACTED]"',
        ),
        (
            'client_secret:\n  "password\': "SampleMarker"',
            'client_secret:\n  "password\': "[REDACTED]"',
        ),
        (
            'secret_config:\n  "password\': "SampleMarker"',
            'secret_config:\n  "password\': "[REDACTED]"',
        ),
        (
            'auth_config:\n  "password\': "SampleMarker"',
            'auth_config:\n  "password\': "[REDACTED]"',
        ),
        (
            'auth:\n"password\': "SampleMarker"',
            'auth:\n"password\': "[REDACTED]"',
        ),
        (
            'authentication:\n"password\': "SampleMarker"',
            'authentication:\n"password\': "[REDACTED]"',
        ),
        (
            'credentials:\n"password\': "SampleMarker"',
            'credentials:\n"password\': "[REDACTED]"',
        ),
        (
            "auth/v1:QWRtaW4= CARD_EXPIRED",
            "auth/v1:[REDACTED] CARD_EXPIRED",
        ),
        (
            "auth/v1:SGVsbG8= CARD_EXPIRED",
            "auth/v1:[REDACTED] CARD_EXPIRED",
        ),
        (
            "auth/v1:YWJja2U0= CARD_EXPIRED",
            "auth/v1:[REDACTED] CARD_EXPIRED",
        ),
        (
            "auth: v1:\nQWRtaW4= CARD_EXPIRED",
            "auth: v1:\n[REDACTED] CARD_EXPIRED",
        ),
        (
            "auth: v1:\nSGVsbG8= CARD_EXPIRED",
            "auth: v1:\n[REDACTED] CARD_EXPIRED",
        ),
        (
            "auth: oauth2:\n  v1:\n    v2:\n      QWRtaW4= CARD_EXPIRED",
            "auth: oauth2:\n  v1:\n    v2:\n      [REDACTED] CARD_EXPIRED",
        ),
        (
            "auth/v1:Zm9vYmFy= CARD_EXPIRED",
            "auth/v1:[REDACTED] CARD_EXPIRED",
        ),
        (
            "auth/v1:YWJjZGU= CARD_EXPIRED",
            "auth/v1:[REDACTED] CARD_EXPIRED",
        ),
        (
            "auth/v1:c2VjcmV0= CARD_EXPIRED",
            "auth/v1:[REDACTED] CARD_EXPIRED",
        ),
        (
            "auth/v1:customValURL=SampleMarker",
            "auth/v1:customValURL=[REDACTED]",
        ),
        (
            "auth/v1:tokenURL=SampleMarker",
            "auth/v1:tokenURL=[REDACTED]",
        ),
        (
            "auth/v1:customValA=SampleMarker",
            "auth/v1:customValA=[REDACTED]",
        ),
        (
            "auth/v1:v1KeyA=SampleMarker",
            "auth/v1:v1KeyA=[REDACTED]",
        ),
        (
            "auth/v1:mySixWordCamelKeyVal=SampleMarker",
            "auth/v1:mySixWordCamelKeyVal=[REDACTED]",
        ),
        (
            "auth/v1:iOSPassword= SampleMarker",
            "auth/v1:iOSPassword= [REDACTED]",
        ),
        (
            "db_credentials: v1: env: S3CR3T",
            "db_credentials: v1: env: [REDACTED]",
        ),
        (
            "user_auth: v1: env: S3CR3T",
            "user_auth: v1: env: [REDACTED]",
        ),
        (
            "secret_store: v1: env: S3CR3T",
            "secret_store: v1: env: [REDACTED]",
        ),
        (
            "user_auth: oauth2: S3CR3T",
            "user_auth: oauth2: [REDACTED]",
        ),
        (
            "client_secret: env: S3CR3T",
            "client_secret: env: [REDACTED]",
        ),
        (
            'user_auth/v1: auth: key="SampleMarker"',
            'user_auth/v1: auth: key="[REDACTED]"',
        ),
        (
            'auth:\nenv: "password": "SampleMarker"',
            'auth:\nenv: "password": "[REDACTED]"',
        ),
        (
            'credentials:\nenv: "password": "SampleMarker"',
            'credentials:\nenv: "password": "[REDACTED]"',
        ),
        (
            'secret:\nenv: "password": "SampleMarker"',
            'secret:\nenv: "password": "[REDACTED]"',
        ),
        (
            'auth: env: "password": "SampleMarker"',
            'auth: env: "password": "[REDACTED]"',
        ),
        (
            'auth: v1: "password": "SampleMarker"',
            'auth: v1: "password": "[REDACTED]"',
        ),
        (
            'auth: oauth2: "password": "SampleMarker"',
            'auth: oauth2: "password": "[REDACTED]"',
        ),
        (
            'auth:\n  v1: "password": "SampleMarker"',
            'auth:\n  v1: "password": "[REDACTED]"',
        ),
        (
            'auth:\n  env: "password": "SampleMarker"',
            'auth:\n  env: "password": "[REDACTED]"',
        ),
        (
            'auth:\nuser/v1: "password": "SampleMarker"',
            'auth:\nuser/v1: "password": "[REDACTED]"',
        ),
        (
            'auth:\nmetadata: "password": "SampleMarker"',
            'auth:\nmetadata: "password": "[REDACTED]"',
        ),
        (
            "auth:\n  v1: 'password': 'SampleMarker'",
            "auth:\n  v1: 'password': '[REDACTED]'",
        ),
        (
            'auth:\n  v1: \\"password\\": \\"SampleMarker\\"',
            'auth:\n  v1: \\"password\\": \\"[REDACTED]\\"',
        ),
        (
            'auth: oauth2:\n  v1: "password": "SampleMarker"',
            'auth: oauth2:\n  v1: "password": "[REDACTED]"',
        ),
        (
            'auth: v1: "Client_Secret": "SampleMarker"',
            'auth: v1: "Client_Secret": "[REDACTED]"',
        ),
        (
            'user_auth: v1: "password": "SampleMarker"',
            'user_auth: v1: "password": "[REDACTED]"',
        ),
        (
            'user_auth: env: "password": "SampleMarker"',
            'user_auth: env: "password": "[REDACTED]"',
        ),
        (
            'api_key: v1: "password": "SampleMarker"',
            'api_key: v1: "password": "[REDACTED]"',
        ),
        (
            'auth:\n  v1: "password\': "SampleMarker"',
            'auth:\n  v1: "password\': "[REDACTED]"',
        ),
        (
            'auth:\nenv: "password\': "SampleMarker"',
            'auth:\nenv: "password\': "[REDACTED]"',
        ),
        (
            'auth:\n  env: "Client_Secret\': "SampleMarker"',
            'auth:\n  env: "Client_Secret\': "[REDACTED]"',
        ),
        (
            'auth: v1: "password\': "SampleMarker"',
            'auth: v1: "password\': "[REDACTED]"',
        ),
        (
            'auth: oauth2: "password\': "SampleMarker"',
            'auth: oauth2: "password\': "[REDACTED]"',
        ),
        (
            '"authentication":\n  "password\': "SampleMarker"',
            '"authentication":\n  "password\': "[REDACTED]"',
        ),
        (
            '"authentication":\n  "Api_Key\': "SampleMarker"',
            '"authentication":\n  "Api_Key\': "[REDACTED]"',
        ),
        (
            '"credentials":\n  "password\': "SampleMarker"',
            '"credentials":\n  "password\': "[REDACTED]"',
        ),
        (
            '"token":\n  "password\': "SampleMarker"',
            '"token":\n  "password\': "[REDACTED]"',
        ),
        (
            "'authentication':\n  \"password': \"SampleMarker\"",
            "'authentication':\n  \"password': \"[REDACTED]\"",
        ),
        (
            '{"authentication":\n  "password\': "SampleMarker"}',
            '{"authentication":\n  "password\': "[REDACTED]"}',
        ),
        (
            'authentication:\n  "Api_Key\': "SampleMarker',
            "authentication:\n  \"Api_Key': [REDACTED]",
        ),
        (
            'authentication:\n  "Client_Secret\': "SampleMarker\n',
            "authentication:\n  \"Client_Secret': [REDACTED]",
        ),
        (
            'authentication:\n  "api key\': "SampleMarker"',
            'authentication:\n  "api key\': "[REDACTED]"',
        ),
        (
            'authentication:\n  "aws secret key\': "SampleMarker"',
            'authentication:\n  "aws secret key\': "[REDACTED]"',
        ),
        (
            "authentication:\n  \"Api_Key': S3CR3T CARD_EXPIRED",
            "authentication:\n  \"Api_Key': [REDACTED] CARD_EXPIRED",
        ),
        (
            "authentication:\n  \"api key': S3CR3T CARD_EXPIRED",
            "authentication:\n  \"api key': [REDACTED] CARD_EXPIRED",
        ),
        (
            "auth/v1:QEdjZjA= CARD_EXPIRED",
            "auth/v1:[REDACTED] CARD_EXPIRED",
        ),
        (
            "auth/v1:QEdjZmE= CARD_EXPIRED",
            "auth/v1:[REDACTED] CARD_EXPIRED",
        ),
        (
            "auth/v1:ABcdefg= CARD_EXPIRED",
            "auth/v1:[REDACTED] CARD_EXPIRED",
        ),
        (
            "auth/v1:ABcdef1= CARD_EXPIRED",
            "auth/v1:[REDACTED] CARD_EXPIRED",
        ),
        (
            "auth/v1:JWTAbc1= CARD_EXPIRED",
            "auth/v1:[REDACTED] CARD_EXPIRED",
        ),
        (
            "auth/v1:APITok1= CARD_EXPIRED",
            "auth/v1:[REDACTED] CARD_EXPIRED",
        ),
        (
            "auth/v1:TLSKey1= CARD_EXPIRED",
            "auth/v1:[REDACTED] CARD_EXPIRED",
        ),
        (
            "auth/v1:V1Token= CARD_EXPIRED",
            "auth/v1:[REDACTED] CARD_EXPIRED",
        ),
        (
            "auth: v1:\nQEdjZjA= CARD_EXPIRED",
            "auth: v1:\n[REDACTED] CARD_EXPIRED",
        ),
        (
            "auth: oauth2:\n  v1:\n    v2:\n      QEdjZjA= CARD_EXPIRED",
            "auth: oauth2:\n  v1:\n    v2:\n      [REDACTED] CARD_EXPIRED",
        ),
        (
            "auth/v1:S3Key=SampleMarker",
            "auth/v1:S3Key=[REDACTED]",
        ),
        (
            "auth/v1:X509Cert=SampleMarker",
            "auth/v1:X509Cert=[REDACTED]",
        ),
        (
            "auth/v1:clientUUID=SampleMarker",
            "auth/v1:clientUUID=[REDACTED]",
        ),
        (
            "auth/v1:SAKey=SampleMarker",
            "auth/v1:SAKey=[REDACTED]",
        ),
        (
            "auth/v1:ADToken=SampleMarker",
            "auth/v1:ADToken=[REDACTED]",
        ),
        (
            "auth/v1:EC2Key=SampleMarker",
            "auth/v1:EC2Key=[REDACTED]",
        ),
        (
            "auth/v1:S3Key = SampleMarker",
            "auth/v1:S3Key = [REDACTED]",
        ),
        (
            'auth/v1:S3Key= "SampleMarker"',
            'auth/v1:S3Key= "[REDACTED]"',
        ),
        (
            "db_credentials: v1: PROD: S3CR3T",
            "db_credentials: v1: PROD: [REDACTED]",
        ),
        (
            "user_auth: v1: Env: S3CR3T",
            "user_auth: v1: Env: [REDACTED]",
        ),
        (
            "user_auth: STAGING: S3CR3T",
            "user_auth: STAGING: [REDACTED]",
        ),
        (
            "user_auth:oauth2: S3CR3T",
            "user_auth:oauth2: [REDACTED]",
        ),
        (
            "signing_key: oauth2: S3CR3T",
            "signing_key: oauth2: [REDACTED]",
        ),
        (
            '{"secret":\n"api_key\': one\ntwo","error_code":"CARD_EXPIRED"}',
            '{"secret":\n"[REDACTED]","error_code":"CARD_EXPIRED"}',
        ),
        (
            '{"ok":1} ' * 32
            + '{"secret":\n"api_key\': one\ntwo","error_code":"CARD_EXPIRED"}',
            '{"ok":1} ' * 32
            + '{"secret":\n"[REDACTED]","error_code":"CARD_EXPIRED"}',
        ),
        (
            '{"secret":\n"Secret_Key\': one\ntwo","error_code":"CARD_EXPIRED"}',
            '{"secret":\n"[REDACTED]","error_code":"CARD_EXPIRED"}',
        ),
        (
            (
                '{"secret":\n'
                '"aws secret key\': one\ntwo","error_code":"CARD_EXPIRED"}'
            ),
            '{"secret":\n"[REDACTED]","error_code":"CARD_EXPIRED"}',
        ),
        (
            '{"secret":\n"Token\': one\ntwo","error_code":"CARD_EXPIRED"}',
            '{"secret":\n"[REDACTED]","error_code":"CARD_EXPIRED"}',
        ),
        (
            '{"secret":\n"ApiKey\': one\ntwo","error_code":"CARD_EXPIRED"}',
            '{"secret":\n"[REDACTED]","error_code":"CARD_EXPIRED"}',
        ),
        (
            '{"password":\n"password\': one\ntwo","error_code":"CARD_EXPIRED"}',
            '{"password":\n"[REDACTED]","error_code":"CARD_EXPIRED"}',
        ),
        (
            (
                r"{\"secret\":\n"
                r"\"Secret_Key\': one\ntwo\",\"error_code\":\"CARD_EXPIRED\"}"
            ),
            r"{\"secret\":\n\"[REDACTED]\",\"error_code\":\"CARD_EXPIRED\"}",
        ),
        (
            '"authentication":\n  "OAuth_Token\': "SampleMarker"',
            '"authentication":\n  "OAuth_Token\': "[REDACTED]"',
        ),
        (
            'authentication:\n  "OAuth_Token\': "SampleMarker"',
            'authentication:\n  "OAuth_Token\': "[REDACTED]"',
        ),
        (
            'authentication:\n  "OAuth Token\': "SampleMarker"',
            'authentication:\n  "OAuth Token\': "[REDACTED]"',
        ),
        (
            'authentication:\n  "X-Api_Key\': "SampleMarker"',
            'authentication:\n  "X-Api_Key\': "[REDACTED]"',
        ),
        (
            'authentication:\n  "x_api key\': "SampleMarker"',
            'authentication:\n  "x_api key\': "[REDACTED]"',
        ),
        (
            'authentication:\n  "google cloud secret api key\': "SampleMarker"',
            'authentication:\n  "google cloud secret api key\': "[REDACTED]"',
        ),
        (
            'authentication:\n  "Api_Key_v2\': "SampleMarker"',
            'authentication:\n  "Api_Key_v2\': "[REDACTED]"',
        ),
        (
            'authentication:\n  "AUTHORIZATION_Key\': "SampleMarker"',
            'authentication:\n  "AUTHORIZATION_Key\': "[REDACTED]"',
        ),
        (
            'authentication:\n  "Client_Secret_v1\': "SampleMarker"',
            'authentication:\n  "Client_Secret_v1\': "[REDACTED]"',
        ),
        (
            'authentication:\n  "myApp_Secret\': "SampleMarker"',
            'authentication:\n  "myApp_Secret\': "[REDACTED]"',
        ),
        (
            'authentication:\n  "auth_ClientSecret\': "SampleMarker"',
            'authentication:\n  "auth_ClientSecret\': "[REDACTED]"',
        ),
        (
            'authentication:\n  "iOS_Password\': "SampleMarker"',
            'authentication:\n  "iOS_Password\': "[REDACTED]"',
        ),
        (
            'authentication:\n  "AWS OAuth Token\': "SampleMarker"',
            'authentication:\n  "AWS OAuth Token\': "[REDACTED]"',
        ),
        (
            'authentication:\n  "myClient Secret\': "SampleMarker"',
            'authentication:\n  "myClient Secret\': "[REDACTED]"',
        ),
        (
            'authentication:\n  "my aws prod api secret key\': "SampleMarker"',
            'authentication:\n  "my aws prod api secret key\': "[REDACTED]"',
        ),
        (
            'auth:\n  "Client_Secret_v1\': "SampleMarker"',
            'auth:\n  "Client_Secret_v1\': "[REDACTED]"',
        ),
        (
            'auth:\nenv: "OAuth_Token\': "SampleMarker"',
            'auth:\nenv: "OAuth_Token\': "[REDACTED]"',
        ),
        (
            "authentication:\n  \"OAuth Token': S3CR3T CARD_EXPIRED",
            "authentication:\n  \"OAuth Token': [REDACTED] CARD_EXPIRED",
        ),
        (
            "authentication:\n  \"X-Api_Key': S3CR3T CARD_EXPIRED",
            "authentication:\n  \"X-Api_Key': [REDACTED] CARD_EXPIRED",
        ),
        (
            "db_credentials: v1: password: S3CR3T",
            "db_credentials: v1: password: [REDACTED]",
        ),
        (
            "user_auth: env: api_key: S3CR3T",
            "user_auth: env: api_key: [REDACTED]",
        ),
        (
            "secret_store: PROD: db_password: S3CR3T",
            "secret_store: PROD: db_password: [REDACTED]",
        ),
        (
            "signing_key: Env: private_key: S3CR3T",
            "signing_key: Env: private_key: [REDACTED]",
        ),
        (
            "db_credentials: password: S3CR3T",
            "db_credentials: password: [REDACTED]",
        ),
        (
            "db_credentials:prod: S3CR3T",
            "db_credentials:prod: [REDACTED]",
        ),
        (
            "user_auth:v1:PROD: S3CR3T",
            "user_auth:v1:PROD: [REDACTED]",
        ),
        (
            "db_credentials: v1: Prod: S3CR3T",
            "db_credentials: v1: Prod: [REDACTED]",
        ),
        (
            "user_auth: Prod: S3CR3T",
            "user_auth: Prod: [REDACTED]",
        ),
        (
            "user_auth: Staging: S3CR3T",
            "user_auth: Staging: [REDACTED]",
        ),
        (
            r"user_auth/v1: password: \"SampleMarker\"",
            r"user_auth/v1: password: \"[REDACTED]\"",
        ),
        (
            "auth: secret:\n  S3CR3T",
            "auth: secret:\n  [REDACTED]",
        ),
        (
            "auth: credentials:\n  YWJjZA==",
            "auth: credentials:\n  [REDACTED]",
        ),
        (
            'auth: secret:\n  "S3CR3T"',
            'auth: secret:\n  "[REDACTED]"',
        ),
        (
            "auth: oauth2: credentials:\npassword: S3CR3T",
            "auth: oauth2: credentials:\npassword: [REDACTED]",
        ),
        (
            "auth: v1: c2VjcmV0= CARD_EXPIRED",
            "auth: v1: [REDACTED] CARD_EXPIRED",
        ),
        (
            "auth: v1: QEdjZjA= CARD_EXPIRED",
            "auth: v1: [REDACTED] CARD_EXPIRED",
        ),
        (
            "auth: v1: YWJjZA==",
            "auth: v1: [REDACTED]",
        ),
        (
            "auth: oauth2: v1: YWJjZA== CARD_EXPIRED",
            "auth: oauth2: v1: [REDACTED] CARD_EXPIRED",
        ),
        (
            r"auth/v1:custom_field= \"SampleMarker\"",
            r"auth/v1:custom_field= \"[REDACTED]\"",
        ),
        (
            r"auth/v1: S3Key= \"SampleMarker two\"",
            r"auth/v1: S3Key= \"[REDACTED]\"",
        ),
        # Spaced colon leaves, next-line leaves, and spaced hops for sensitive
        # heads
        ("db_password: secret123", "db_password: [REDACTED]"),
        ("ssh_passphrase: my_passphrase_123", "ssh_passphrase: [REDACTED]"),
        ("svc_jwt: eyJhbGciOi.payload.sig", "svc_jwt: [REDACTED]"),
        ("db_dsn: postgres://user:secret@host/db", "db_dsn: [REDACTED]"),
        (
            'db_password:\n  {\n    "val": "x"\n  }',
            "db_password:\n  [REDACTED]",
        ),
        ('db_password:\n  "secret123"', 'db_password:\n  "[REDACTED]"'),
        ("db_password:\n  token=x9", "db_password:\n  token=[REDACTED]"),
        ("db_password:\n  YWJjZGVm", "db_password:\n  [REDACTED]"),
        (
            'db_password: password:\n  {\n    "val": "x"\n  }',
            "db_password: password:\n  [REDACTED]",
        ),
        ("auth : db_password: x", "auth : db_password: [REDACTED]"),
        ("service : jwt: token123", "service : jwt: [REDACTED]"),
        ("db : dsn : postgres://u:p@h/db", "db : dsn : [REDACTED]"),
        ("db : db_dsn : S3CR3T", "db : db_dsn : [REDACTED]"),
        ("auth : password : secret", "auth : password : [REDACTED]"),
        ("auth: db_password: x", "auth: db_password: [REDACTED]"),
        ("service: svc_jwt: tok", "service: svc_jwt: [REDACTED]"),
        (
            "db_password: v1: prod: password: S3CR3T",
            "db_password: v1: prod: password: [REDACTED]",
        ),
        (
            "db_password: prod: password: S3CR3T",
            "db_password: prod: password: [REDACTED]",
        ),
        (
            'db_password: v1: password:\n  "secret123"',
            'db_password: v1: password:\n  "[REDACTED]"',
        ),
        (
            "db_password: v1: password:\n  YWJjZGVm",
            "db_password: v1: password:\n  [REDACTED]",
        ),
        (
            "db_password: v1: password:\n  token=x9",
            "db_password: v1: password:\n  token=[REDACTED]",
        ),
        # Underscore and PascalCase_lowercase mismatched-quote headers
        (
            '{"auth":\n  "_password\': S3CR3T\n}',
            '{"auth":\n  "_password\': [REDACTED]\n}',
        ),
        (
            '{"auth":\n  "password_\': S3CR3T\n}',
            '{"auth":\n  "password_\': [REDACTED]\n}',
        ),
        (
            '{"auth":\n  "__password\': S3CR3T\n}',
            '{"auth":\n  "__password\': [REDACTED]\n}',
        ),
        (
            '{"auth":\n  "Bearer_token\': S3CR3T\n}',
            '{"auth":\n  "Bearer_token\': [REDACTED]\n}',
        ),
        (
            '{"auth":\n  "Access_secret\': S3CR3T\n}',
            '{"auth":\n  "Access_secret\': [REDACTED]\n}',
        ),
        (
            r'{"auth":\n  \"_password\': S3CR3T\n}',
            r'{"auth":\n  \"_password\': [REDACTED]\n}',
        ),
        (
            r'{"auth":\n  \"Bearer_token\': S3CR3T\n}',
            r'{"auth":\n  \"Bearer_token\': [REDACTED]\n}',
        ),
        (
            'authentication:\n  "ApiKey_secret\': "SampleMarker"',
            'authentication:\n  "ApiKey_secret\': "[REDACTED]"',
        ),
        (
            'authentication:\n  "AccessKey_secret\': "SampleMarker"',
            'authentication:\n  "AccessKey_secret\': "[REDACTED]"',
        ),
        (
            'authentication:\n  "ClientSecret_token\': "SampleMarker"',
            'authentication:\n  "ClientSecret_token\': "[REDACTED]"',
        ),
        (
            'authentication:\n  "RefreshToken_secret\': "SampleMarker"',
            'authentication:\n  "RefreshToken_secret\': "[REDACTED]"',
        ),
        (
            'authentication:\n  "UserAuth_token\': "SampleMarker"',
            'authentication:\n  "UserAuth_token\': "[REDACTED]"',
        ),
        (
            'authentication:\n  "BigQuery_secret\': "SampleMarker"',
            'authentication:\n  "BigQuery_secret\': "[REDACTED]"',
        ),
        (
            'authentication:\n  "AccessToken_secret\': "SampleMarker"',
            'authentication:\n  "AccessToken_secret\': "[REDACTED]"',
        ),
        (
            'authentication:\n  "ServiceAccount_password\': "SampleMarker"',
            'authentication:\n  "ServiceAccount_password\': "[REDACTED]"',
        ),
        (
            'authentication:\n  "ClientSecret_hash\': "SampleMarker"',
            'authentication:\n  "ClientSecret_hash\': "[REDACTED]"',
        ),
        (
            'authentication:\n  "OAuthClient_secret\': "SampleMarker"',
            'authentication:\n  "OAuthClient_secret\': "[REDACTED]"',
        ),
        (
            'authentication:\n  "FirebaseAuth_token\': "SampleMarker"',
            'authentication:\n  "FirebaseAuth_token\': "[REDACTED]"',
        ),
        # Multiline JSON string lookalikes (pre- and post-budget) and child KV
        # members
        (
            '{"secret":\n"password\': \'one\'\ntwo","error_code":"CARD_EXPIRED"}',
            '{"secret":\n"[REDACTED]","error_code":"CARD_EXPIRED"}',
        ),
        (
            '{"secret":\n"password\': (one)\ntwo","error_code":"CARD_EXPIRED"}',
            '{"secret":\n"[REDACTED]","error_code":"CARD_EXPIRED"}',
        ),
        (
            '{"secret":\n"password\': {a:1}\ntwo","error_code":"CARD_EXPIRED"}',
            '{"secret":\n"[REDACTED]","error_code":"CARD_EXPIRED"}',
        ),
        (
            '{"secret":\n"password\': [1]\ntwo","error_code":"CARD_EXPIRED"}',
            '{"secret":\n"[REDACTED]","error_code":"CARD_EXPIRED"}',
        ),
        (
            '{"secret":\n"password\': \\"one\\"\ntwo","error_code":"CARD_EXPIRED"}',
            '{"secret":\n"[REDACTED]","error_code":"CARD_EXPIRED"}',
        ),
        (
            '{"ok":1} ' * 32
            + '{"secret":\n"password\': \'one\'\ntwo","error_code":"CARD_EXPIRED"}',
            '{"ok":1} ' * 32
            + '{"secret":\n"[REDACTED]","error_code":"CARD_EXPIRED"}',
        ),
        (
            '{"ok":1} ' * 32
            + '{"secret":\n"password\': (one)\ntwo","error_code":"CARD_EXPIRED"}',
            '{"ok":1} ' * 32
            + '{"secret":\n"[REDACTED]","error_code":"CARD_EXPIRED"}',
        ),
        (
            '{"ok":1} ' * 32
            + '{"secret":\n"password\': {a:1}\ntwo","error_code":"CARD_EXPIRED"}',
            '{"ok":1} ' * 32
            + '{"secret":\n"[REDACTED]","error_code":"CARD_EXPIRED"}',
        ),
        (
            '{"ok":1} ' * 32
            + '{"secret":\n"password\': [1]\ntwo","error_code":"CARD_EXPIRED"}',
            '{"ok":1} ' * 32
            + '{"secret":\n"[REDACTED]","error_code":"CARD_EXPIRED"}',
        ),
        (
            '{"ok":1} ' * 32
            + '{"secret":\n"password\': \\"one\\"\ntwo","error_code":"CARD_EXPIRED"}',
            '{"ok":1} ' * 32
            + '{"secret":\n"[REDACTED]","error_code":"CARD_EXPIRED"}',
        ),
        (
            '{"authentication":\n  "password\': S3CR3T\n}',
            '{"authentication":\n  "password\': [REDACTED]\n}',
        ),
        (
            '{"authentication":\n  "password\': S3CR3T, "error_code": "CARD_EXPIRED"}',
            '{"authentication":\n  "password\': [REDACTED], "error_code": "CARD_EXPIRED"}',
        ),
        (
            '{"authentication":\n  "password\': S3CR3T}, "error_code": "CARD_EXPIRED"}',
            '{"authentication":\n  "password\': [REDACTED]}, "error_code": "CARD_EXPIRED"}',
        ),
        (
            '{"authentication":\n  \\"password\': S3CR3T, \\"error_code\\": \\"CARD_EXPIRED\\"}',
            '{"authentication":\n  \\"password\': [REDACTED], \\"error_code\\": \\"CARD_EXPIRED\\"}',
        ),
        (
            '{"authentication":\n  \\"password\': S3CR3T, "error_code": "CARD_EXPIRED"}',
            '{"authentication":\n  \\"password\': [REDACTED], "error_code": "CARD_EXPIRED"}',
        ),
        (
            '{"secret":\n"password\': one, two",\n"error_code": "CARD_EXPIRED"}',
            '{"secret":"[REDACTED]","error_code":"CARD_EXPIRED"}',
        ),
        (
            '{"secret":\n"password\': one,",\n"error_code": "CARD_EXPIRED"}',
            '{"secret":"[REDACTED]","error_code":"CARD_EXPIRED"}',
        ),
        # Quoted mismatched-quote guards, JSON enclosures, and colon chains
        (
            '{\n  "authentication":\n  "password\': "two"\n}',
            '{\n  "authentication":\n  "password\': "[REDACTED]"\n}',
        ),
        (
            'authentication:\n  "password\': "one"',
            'authentication:\n  "password\': "[REDACTED]"',
        ),
        (
            'authentication:\n  "password\': "SK_LIVE_SECRET_999',
            "authentication:\n  \"password': [REDACTED]",
        ),
        (
            'authentication:\n  "password\': "MY_PROD_TOKEN',
            "authentication:\n  \"password': [REDACTED]",
        ),
        (
            'authentication:\n  "secret\': "AWS_SECRET_999\nCARD_EXPIRED',
            "authentication:\n  \"secret': [REDACTED]\nCARD_EXPIRED",
        ),
        (
            'authentication:\n  "password\': "CLIENT_SECRET_V2\nmore',
            "authentication:\n  \"password': [REDACTED]",
        ),
        (
            'authentication:\n  "password\': "ONE_APP_SECRET',
            "authentication:\n  \"password': [REDACTED]",
        ),
        (
            'authentication:\n  "password\': " S3CR3T"',
            'authentication:\n  "password\': "[REDACTED]"',
        ),
        (
            'password:\n"password\': " S3CR3T"',
            'password:\n"password\': "[REDACTED]"',
        ),
        (
            'authentication:\n  "password\': " S3CR3T',
            "authentication:\n  \"password': [REDACTED]",
        ),
        (
            'authentication:\n  "password\': S3CR3T CARD_EXPIRED"',
            'authentication:\n  "password\': [REDACTED] CARD_EXPIRED"',
        ),
        (
            r"authentication:\n  \"password\': S3CR3T CARD_EXPIRED\"",
            r"authentication:\n  \"password\': [REDACTED] CARD_EXPIRED\"",
        ),
        (
            '{"password":\n"SampleMarker_password\': one"}',
            '{"password":"[REDACTED]"}',
        ),
        (
            '{\n  "authentication":\n  "password\': "S3CR3T"\n  done"\n}',
            '{\n  "authentication":\n  "[REDACTED]"\n}',
        ),
        (
            r"{\n  \"authentication\":\n  \"password\': \"S3CR3T\"\n  done\"\n}",
            r"{\n  \"authentication\":\n  \"[REDACTED]\"\n}",
        ),
        (
            '{"secret":\n"password\': "alpha" beta\ngamma","error_code":"CARD_EXPIRED"}',
            '{"secret":\n"[REDACTED]","error_code":"CARD_EXPIRED"}',
        ),
        (
            '{"secret":\n"password\': first, second\nthird","error_code":"CARD_EXPIRED"}',
            '{"secret":\n"[REDACTED]","error_code":"CARD_EXPIRED"}',
        ),
        (
            '{"secret":\n"password\': (a), b\nc","error_code":"CARD_EXPIRED"}',
            '{"secret":\n"[REDACTED]","error_code":"CARD_EXPIRED"}',
        ),
        (
            "{}\n" * 33
            + '{"password":\n"SampleMarker_password\': one, two\nthree"}',
            "{}\n" * 33 + '{"password":\n"[REDACTED]"}',
        ),
        (
            "{}\n" * 33
            + r"{\"password\":\n\"SampleMarker_password\': \"alpha\", two\"}",
            "{}\n" * 33 + r"{\"password\":\n\"[REDACTED]\"}",
        ),
        (
            r"{\"password\":"
            + "\n"
            + r"\"SampleMarker_password\': one\ntwo\",\"error_code\":\"CARD_EXPIRED\"}",
            r"{\"password\":"
            + "\n"
            + r"\"[REDACTED]\",\"error_code\":\"CARD_EXPIRED\"}",
        ),
        (
            '{"secret":\n\'password": alpha\nbeta\', "error_code": "CARD_EXPIRED"}',
            '{"secret":\n\'[REDACTED]\', "error_code": "CARD_EXPIRED"}',
        ),
        (
            r'{\"secret\":\n\'password": alpha\\nbeta\',\"error_code\":\"CARD_EXPIRED\"}',
            r"{\"secret\":\n\'[REDACTED]\',\"error_code\":\"CARD_EXPIRED\"}",
        ),
        (
            r'{\"secret\":\n\\\'password": alpha\nbeta\\\',\"error_code\":\"CARD_EXPIRED\"}',
            r"{\"secret\":\n\\\'[REDACTED]\\\',\"error_code\":\"CARD_EXPIRED\"}",
        ),
        (
            '{"secret":\n"password\': '
            + "x" * 2100
            + '\ntail","error_code":"CARD_EXPIRED"}',
            '{"secret":\n"[REDACTED]","error_code":"CARD_EXPIRED"}',
        ),
        (
            '{"secret":\n"password\': '
            + "x" * 10000
            + '\ntail","error_code":"CARD_EXPIRED"}',
            '{"secret":\n"[REDACTED]","error_code":"CARD_EXPIRED"}',
        ),
        (
            "password: Hunter: FAILED",
            "password: [REDACTED] FAILED",
        ),
        (
            "password: Timeout: expired",
            "password: [REDACTED] expired",
        ),
        (
            "password: Admin: error occurred",
            "password: [REDACTED] error occurred",
        ),
        (
            "db_password: Hunter: FAILED",
            "db_password: [REDACTED] FAILED",
        ),
        (
            "signing_key: Env:\n  private_key: S3CR3T",
            "signing_key: Env:\n  private_key: [REDACTED]",
        ),
        (
            "secret_store: PROD:\n  db_password: S3CR3T",
            "secret_store: PROD:\n  db_password: [REDACTED]",
        ),
        (
            "user_auth: status: ok",
            "user_auth: status: ok",
        ),
        (
            "credentials: auth_error: CARD_EXPIRED",
            "credentials: auth_error: CARD_EXPIRED",
        ),
        (
            'db_password: v1:\n"S3CR3T"',
            'db_password: v1:\n"[REDACTED]"',
        ),
        (
            'db_credentials: prod:\n"S3CR3T"',
            'db_credentials: prod:\n"[REDACTED]"',
        ),
        (
            "db_password: v1:\nCARD_EXPIRED",
            "db_password: [REDACTED]\nCARD_EXPIRED",
        ),
        (
            "db_password:v1:password: S3CR3T",
            "db_password:v1:password: [REDACTED]",
        ),
        (
            "db_password:v1:password:\n  S3CR3T",
            "db_password:v1:password:\n  [REDACTED]",
        ),
        (
            "db_credentials:password:\n  S3CR3T",
            "db_credentials:password:\n  [REDACTED]",
        ),
        (
            'db_password:password:\n"S3CR3T"',
            'db_password:password:\n"[REDACTED]"',
        ),
        (
            'db_credentials:Tenant:password="S3CR3T, PART2"',
            'db_credentials:Tenant:password="[REDACTED]"',
        ),
        (
            'db_password:tenant:password="S3CR3T, PART2"',
            'db_password:tenant:password="[REDACTED]"',
        ),
    ):
      with self.subTest(container_colon_and_url_host=sample):
        self.assertEqual(scrub_sensitive_text(sample), expected)

  def test_pem_multiline_json_member_bounds_and_colon_status_guards(
      self,
  ) -> None:
    from analytics.trajectory_extractor import _extract_error_markers  # noqa: PLC0415
    from analytics.trajectory_extractor import extract_s3_outcome_first
    from analytics.trajectory_extractor import scrub_sensitive_text

    for sample, expected in (
        (
            'token: "-----BEGIN PRIVATE KEY-----\n'
            "MIIF_SECRET_PART2\n"
            "CARD_EXPIRED",
            "token: [REDACTED]",
        ),
        (
            'private_key="-----BEGIN RSA PRIVATE KEY-----\n'
            "MIIF_SECRET_2\n"
            "CARD_EXPIRED",
            "private_key=[REDACTED]",
        ),
        (
            "password: '-----BEGIN EC PRIVATE KEY-----\n"
            "SECRET_LINE\n"
            "CARD_EXPIRED",
            "password: [REDACTED]",
        ),
        (
            'password="S3CR3T\nCARD_EXPIRED\nMORE_SECRET',
            "password=[REDACTED]",
        ),
        (
            'api_key="S3CR3T\nCARD_EXPIRED\nMORE_KEY_BYTES',
            "api_key=[REDACTED]",
        ),
        (
            'secret="S3CR3T\nCARD_EXPIRED\nSECRET_LINE3_UNPROTECTED',
            "secret=[REDACTED]",
        ),
        (
            r"\"password\": \"S3CR3T\nCARD_EXPIRED",
            r"\"password\": [REDACTED]\nCARD_EXPIRED",
        ),
        (
            r"password: \'S3CR3T\nCARD_EXPIRED",
            r"password: [REDACTED]\nCARD_EXPIRED",
        ),
        (
            r"\"token\": \"S3CR3T\nDEADLINE_EXCEEDED",
            r"\"token\": [REDACTED]\nDEADLINE_EXCEEDED",
        ),
        (
            r'{"log": "\"password\": \"S3CR3T\\nCARD_EXPIRED"}',
            r'{"log":"\"password\": [REDACTED]\\nCARD_EXPIRED"}',
        ),
        (
            '{"password": "S3CR3T" oops} '
            '{"error_code": "CARD_EXPIRED", "token": "TOK"}',
            '{"password": "[REDACTED]" oops} '
            '{"error_code":"CARD_EXPIRED","token":"[REDACTED]"}',
        ),
        (
            '{"password": "S3CR3T" oops] '
            '{"error_code": "CARD_EXPIRED", "token": "TOK"}',
            '{"password": "[REDACTED]" oops] '
            '{"error_code":"CARD_EXPIRED","token":"[REDACTED]"}',
        ),
        (
            '{"password": "S3CR3T" CARD_EXPIRED} {"ok": "1"}',
            '{"password": "[REDACTED]" CARD_EXPIRED} {"ok": "1"}',
        ),
        (
            '{"password": "S3CR3T   "   , "error": "CARD_EXPIRED"}',
            '{"password":"[REDACTED]","error":"CARD_EXPIRED"}',
        ),
        (
            r"{\"password\": \"S3CR3T\" oops\n}\n"
            r"{\"error_code\": \"CARD_EXPIRED\", \"token\": \"TOK\"}",
            r"{\"password\": \"[REDACTED]\" oops\n}\n"
            r"{\"error_code\": \"CARD_EXPIRED\", \"token\": \"[REDACTED]\"}",
        ),
        (
            '{\n  "secret":\n'
            '  "SampleMarker_password\': one, "CARD_EXPIRED"\n}',
            '{\n  "secret":\n  "[REDACTED]"CARD_EXPIRED"\n}',
        ),
        (
            '{\n  "secret":\n'
            '  "SampleMarker_password\': one" "CARD_EXPIRED"\n}',
            '{\n  "secret":\n  "[REDACTED]" "CARD_EXPIRED"\n}',
        ),
        (
            '{"secret":\n"password\': "alpha"\n} beta",'
            '"error_code":"CARD_EXPIRED"}',
            '{"secret":\n"[REDACTED]","error_code":"CARD_EXPIRED"}',
        ),
        (
            '{"secret":\n\\\'password": alpha\nbeta\','
            '"error_code":"CARD_EXPIRED"}',
            '{"secret":\n[REDACTED],"error_code":"CARD_EXPIRED"}',
        ),
        (
            '{\n  "authentication":\n  "password\': "S3CR3T"\n  }\n',
            '{\n  "authentication":\n  "password\': "[REDACTED]"\n  }\n',
        ),
        (
            "{\n  'authentication':\n  'password\": 'S3CR3T'\n  }\n",
            "{\n  'authentication':\n" "  'password\": '[REDACTED]'\n  }\n",
        ),
        (
            '{\n  "authentication":\n  "password\': "S3CR3T"\n'
            '  "error_code": "CARD_EXPIRED"\n}',
            '{\n  "authentication":\n  "password\': "[REDACTED]"\n'
            '  "error_code": "CARD_EXPIRED"\n}',
        ),
        (
            r"{\n  \"authentication\":\n" r"  \"password\': \"S3CR3T\"\n  }\n",
            r"{\n  \"authentication\":\n"
            r"  \"password\': \"[REDACTED]\"\n  }\n",
        ),
        (
            r"{\n  \"authentication\":\n  \"password\': \"S3CR3T\"\n"
            r"  \"error_code\": \"CARD_EXPIRED\"\n}",
            r"{\n  \"authentication\":\n  \"password\': \"[REDACTED]\"\n"
            r"  \"error_code\": \"CARD_EXPIRED\"\n}",
        ),
        (
            '{\n  "authentication":\n  "password\': "S3CR3T",\n  } beta"\n}',
            '{\n  "authentication":\n  "[REDACTED]"\n}',
        ),
        (
            r"{\n  \"authentication\":\n"
            r"  \"password\': \"S3CR3T\",\n  } beta\"\n}",
            r"{\n  \"authentication\":\n  \"[REDACTED]\"\n}",
        ),
        (
            '{"a": "prefix \'password\': \\"S3CR3T\\"\\n'
            '  \\"next\\": \\"OK\\"", '
            '"secret": "password\': \\"TOK\\"\\n  hello"}',
            '{"a":"prefix \'password\': \\"[REDACTED]\\"\\n'
            '  \\"next\\": \\"OK\\"","secret":"[REDACTED]"}',
        ),
        (
            "db_password: Env: CARD_EXPIRED",
            "db_password: [REDACTED] CARD_EXPIRED",
        ),
        (
            "db_password: PROD: CARD_EXPIRED",
            "db_password: [REDACTED] CARD_EXPIRED",
        ),
        (
            "token: Env: CARD_EXPIRED",
            "token: [REDACTED] CARD_EXPIRED",
        ),
        (
            "token: PROD: CARD_EXPIRED",
            "token: [REDACTED] CARD_EXPIRED",
        ),
        (
            "api_key: v2: CARD_EXPIRED",
            "api_key: [REDACTED] CARD_EXPIRED",
        ),
        (
            "password: Status: CARD_EXPIRED",
            "password: [REDACTED] CARD_EXPIRED",
        ),
        (
            "password: reason: CARD_EXPIRED",
            "password: [REDACTED] CARD_EXPIRED",
        ),
        (
            "user_auth: status: CARD_EXPIRED",
            "user_auth: [REDACTED] CARD_EXPIRED",
        ),
        (
            "credentials: Auth: CARD_EXPIRED",
            "credentials: [REDACTED] CARD_EXPIRED",
        ),
        (
            "auth: v1: CARD_EXPIRED",
            "auth: [REDACTED] CARD_EXPIRED",
        ),
        (
            "secret_store: Env: CARD_EXPIRED",
            "secret_store: [REDACTED] CARD_EXPIRED",
        ),
        (
            "password: Secret: status: ok",
            "password: [REDACTED] status: ok",
        ),
        (
            "password: secret: status: ok",
            "password: [REDACTED] status: ok",
        ),
        (
            "password: my secret hunter2 CARD_EXPIRED",
            "password: [REDACTED] secret hunter2 CARD_EXPIRED",
        ),
        (
            "token: John: Doe: CARD_EXPIRED",
            "token: [REDACTED] Doe: CARD_EXPIRED",
        ),
        (
            "db_password: v1: password: S3CR3T",
            "db_password: v1: password: [REDACTED]",
        ),
        (
            "secret_store: password: S3CR3T",
            "secret_store: password: [REDACTED]",
        ),
        (
            "oauth: reason: expired",
            "oauth: reason: expired",
        ),
    ):
      with self.subTest(sample=sample):
        self.assertEqual(scrub_sensitive_text(sample), expected)

    self.assertEqual(
        _extract_error_markers(r"\"password\": \"S3CR3T\nCARD_EXPIRED"),
        ["CARD_EXPIRED"],
    )
    self.assertEqual(
        _extract_error_markers(r"password: \'S3CR3T\nCARD_EXPIRED"),
        ["CARD_EXPIRED"],
    )

    for s3_label, s3_payload in (
        (
            "brace_member_bound",
            '{"password": "S3CR3T" oops} '
            '{"error_code": "CARD_EXPIRED", "token": "TOK"}',
        ),
        (
            "bracket_member_bound",
            '{"password": "S3CR3T" oops] '
            '{"error_code": "CARD_EXPIRED", "token": "TOK"}',
        ),
        (
            "trailing_space_member_bound",
            '{"password": "S3CR3T   "   , "error": "CARD_EXPIRED"}',
        ),
        (
            "escaped_brace_member_bound",
            r"{\"password\": \"S3CR3T\" oops\n}\n"
            r"{\"error_code\": \"CARD_EXPIRED\", \"token\": \"TOK\"}",
        ),
    ):
      with self.subTest(s3_member_bound=s3_label):
        s3_out = extract_s3_outcome_first(
            {
                "turns": [
                    {
                        "turn_index": 0,
                        "turn_id": f"inv_{s3_label}",
                        "events": [
                            {
                                "author": "customer_ops_agent",
                                "content": {
                                    "role": "model",
                                    "parts": [{"text": s3_payload}],
                                },
                            }
                        ],
                    }
                ]
            },
            metrics={"error_summary": s3_payload},
        )
        self.assertIn("has_error=True", s3_out)
        self.assertIn("CARD_EXPIRED", s3_out)
        self.assertIn("[REDACTED]", s3_out)
        self.assertNotIn("S3CR3T", s3_out)
        self.assertNotIn("TOK", s3_out)

  def test_p1_json_bounds_error_parity_colon_eq_chains_and_url_amp(
      self,
  ) -> None:
    from analytics.trajectory_extractor import _extract_error_markers  # noqa: PLC0415
    from analytics.trajectory_extractor import extract_s3_outcome_first
    from analytics.trajectory_extractor import scrub_sensitive_text

    for sample, expected in (
        (
            '{"password": "S3CR3T" oops} token: TOK "tail"',
            '{"password": "[REDACTED]" oops} token: [REDACTED] "tail"',
        ),
        (
            'password: "SECRET_1\nCARD_EXPIRED secret_token_on_same_line',
            "password: [REDACTED]",
        ),
        (
            'password="line1\nCARD_EXPIRED S3CR3T_TAIL',
            "password=[REDACTED]",
        ),
        (
            r"password=\"line1\nCARD_EXPIRED S3CR3T_TAIL",
            "password=[REDACTED]",
        ),
        (
            'password: "line1\nERROR: CARD_EXPIRED S3CR3T_TAIL',
            "password: [REDACTED]",
        ),
        (
            'password="line1\nS3CR3T_HEAD CARD_EXPIRED',
            "password=[REDACTED]",
        ),
        (
            r"password=\"line1\nS3CR3T_HEAD CARD_EXPIRED",
            "password=[REDACTED]",
        ),
        (
            'password="line1\nTOKEN_LEAK ERROR: CARD_EXPIRED',
            "password=[REDACTED]",
        ),
        (
            'password="S3CR3T_LINE1\nS3CR3T_LINE2 CARD_EXPIRED',
            "password=[REDACTED]",
        ),
        (
            r"\"password\": \"S3CR3T_1\nS3CR3T_2 CARD_EXPIRED",
            r"\"password\": [REDACTED]",
        ),
        (
            r'password="line1\nS3CR3T_LINE2\\nCARD_EXPIRED',
            "password=[REDACTED]",
        ),
        (
            r"\"password\": \"S3CR3T_LINE1\nS3CR3T_LINE2\\nCARD_EXPIRED",
            r"\"password\": [REDACTED]",
        ),
        (
            'password="line1\nERROR: CARD_EXPIRED',
            "password=[REDACTED]\nERROR: CARD_EXPIRED",
        ),
        (
            '{"secret":\n"password\': alpha,\n} beta",'
            '"error_code":"CARD_EXPIRED"}',
            '{"secret":\n"[REDACTED]","error_code":"CARD_EXPIRED"}',
        ),
        (
            '{"password":\n"SampleMarker_password\': one,\n} beta"}',
            '{"password":\n"[REDACTED]"}',
        ),
        (
            '{"secret":\n"password\': alpha,\n} S3CR3T_BETA",'
            '"error_code":"CARD_EXPIRED"}',
            '{"secret":\n"[REDACTED]","error_code":"CARD_EXPIRED"}',
        ),
        (
            '{"secret":\n"password\': alpha,\n] S3CR3T_BETA",'
            '"error_code":"CARD_EXPIRED"}',
            '{"secret":\n"[REDACTED]","error_code":"CARD_EXPIRED"}',
        ),
        (
            '{"secret":\n"password\': alpha\n, S3CR3T_BETA",'
            '"error_code":"CARD_EXPIRED"}',
            '{"secret":\n"[REDACTED]","error_code":"CARD_EXPIRED"}',
        ),
        (
            '{"secret":\n"password\': alpha, } S3CR3T_BETA\n",'
            '"error_code":"CARD_EXPIRED"}',
            '{"secret":\n"[REDACTED]","error_code":"CARD_EXPIRED"}',
        ),
        (
            '{"secret":\n"password\': alpha ] S3CR3T_BETA\n",'
            '"error_code":"CARD_EXPIRED"}',
            '{"secret":\n"[REDACTED]","error_code":"CARD_EXPIRED"}',
        ),
        (
            '{"bad\n, "secret":\n\\"password\': alpha, } S3CR3T_BETA\n\\",'
            ' "error":"CARD_EXPIRED"}',
            '{"bad\n, "secret":\n\\"[REDACTED]\\", "error":"CARD_EXPIRED"}',
        ),
        (
            '{"bad\n, "secret":\n\\"password\': alpha,\n} S3CR3T_BETA\\",'
            ' "error":"CARD_EXPIRED"}',
            '{"bad\n, "secret":\n\\"[REDACTED]\\", "error":"CARD_EXPIRED"}',
        ),
        (
            '{"bad\n, "secret":\n\\"password\': alpha\n, S3CR3T_BETA\\",'
            ' "error":"CARD_EXPIRED"}',
            '{"bad\n, "secret":\n\\"[REDACTED]\\", "error":"CARD_EXPIRED"}',
        ),
        (
            "credentials: hunter2: status: ok",
            "credentials: [REDACTED] status: ok",
        ),
        (
            "secret: hunter2: status: ok",
            "secret: [REDACTED] status: ok",
        ),
        (
            "credentials: mypass: reason: failed",
            "credentials: [REDACTED] reason: failed",
        ),
        (
            "oauth: hunter2: result: ok",
            "oauth: [REDACTED] result: ok",
        ),
        (
            "credentials: hunter2: S3CR3T",
            "credentials: [REDACTED] S3CR3T",
        ),
        (
            "secret: hunter2: S3CR3T",
            "secret: [REDACTED] S3CR3T",
        ),
        (
            "credentials: hunter2: password: S3CR3T",
            "credentials: [REDACTED] password: [REDACTED]",
        ),
        (
            "secret: hunter2: Token: S3CR3T",
            "secret: [REDACTED] Token: [REDACTED]",
        ),
        (
            "credentials: v1/hunter2: status: ok",
            "credentials: [REDACTED] status: ok",
        ),
        (
            "auth: prod/v1: password: S3CR3T",
            "auth: prod/v1: password: [REDACTED]",
        ),
        (
            "auth: v1: password: S3CR3T",
            "auth: v1: password: [REDACTED]",
        ),
        (
            "auth: credentials:\n  c2VjcmV0= CARD_EXPIRED",
            "auth: credentials:\n  [REDACTED] CARD_EXPIRED",
        ),
        (
            "credentials: env: password: S3CR3T",
            "credentials: env: password: [REDACTED]",
        ),
        (
            'auth:Tenant:password="SampleMarker"',
            'auth:Tenant:password="[REDACTED]"',
        ),
        (
            'auth:myService:password="SampleMarker"',
            'auth:myService:password="[REDACTED]"',
        ),
        (
            'authentication:\n  auth:myService:password="SampleMarker"',
            'authentication:\n  auth:myService:password="[REDACTED]"',
        ),
        (
            'auth:customer_identity_service:password="SampleMarker"',
            'auth:customer_identity_service:password="[REDACTED]"',
        ),
        (
            'auth:myService:password={"v":"SampleMarker"}',
            "auth:myService:password=[REDACTED]",
        ),
        (
            'db_password:myService:password="SampleMarker"',
            'db_password:myService:password="[REDACTED]"',
        ),
        (
            'user_auth:MyService:password="SampleMarker"',
            'user_auth:MyService:password="[REDACTED]"',
        ),
        (
            'secret:secret="SampleMarker"',
            'secret:secret="[REDACTED]"',
        ),
        (
            'auth:auth="SampleMarker"',
            'auth:auth="[REDACTED]"',
        ),
        (
            'auth:v1:v1="SampleMarker"',
            'auth:v1:v1="[REDACTED]"',
        ),
        (
            "auth:oauth2:credentials:secret:"
            'authorization:password="SampleMarker"',
            "auth:oauth2:credentials:secret:"
            'authorization:password="[REDACTED]"',
        ),
        (
            "http://localhost:1234&SampleMarker@example.test",
            "http://localhost:[REDACTED]@example.test",
        ),
        (
            "https://10.0.0.1:443&SampleMarker@example.test",
            "https://10.0.0.1:[REDACTED]@example.test",
        ),
        (
            '{"password": "val" {"a": "b"} S3CR3T", "other": 1}',
            '{"password": "[REDACTED]", "other": 1}',
        ),
        (
            '{"password": "val" ["b"] S3CR3T", "other": 1}',
            '{"password": "[REDACTED]", "other": 1}',
        ),
        (
            r'{"password": \"val\" {\"a\": \"b\"} S3CR3T\", \"other\": 1}',
            r'{"password": \"[REDACTED]\", \"other\": 1}',
        ),
        (
            '{"password": "val" {"a": "b"} S3CR3T\n", "other": 1}',
            '{"password": "[REDACTED]", "other": 1}',
        ),
        (
            '{"secret":\n"SampleMarker_password\': "alpha" } S3CR3T_BETA",'
            '"error_code":"CARD_EXPIRED"}',
            '{"secret":\n"[REDACTED]","error_code":"CARD_EXPIRED"}',
        ),
        (
            '{"secret":\n"SampleMarker_password\': "alpha" ] S3CR3T_BETA",'
            '"error_code":"CARD_EXPIRED"}',
            '{"secret":\n"[REDACTED]","error_code":"CARD_EXPIRED"}',
        ),
        (
            r'{"bad\n, "secret":\n\"SampleMarker_password\': '
            r'\"alpha\" } S3CR3T_BETA\", "error":"CARD_EXPIRED"}',
            r'{"bad\n, "secret":\n\"[REDACTED]\", "error":"CARD_EXPIRED"}',
        ),
        (
            '{"secret":\n"SampleMarker_password\': "alpha" } S3CR3T_BETA",'
            '"next":"ok"}',
            '{"secret":\n"[REDACTED]","next":"ok"}',
        ),
        (
            r'{"bad\n, "secret":\n\"SampleMarker_password\': '
            r'\"alpha\" } S3CR3T_BETA\", "next":"ok"}',
            r'{"bad\n, "secret":\n\"[REDACTED]\", "next":"ok"}',
        ),
        (
            '{"secret": "SampleMarker_password\': "alpha" } S3CR3T_BETA"}',
            '{"secret": "[REDACTED]"}',
        ),
        (
            '{"password": "alpha"} log "hi", "u":"a"',
            '{"password":"[REDACTED]"} log "hi", "u":"a"',
        ),
        (
            '{"a": \'bad\', "password": "alpha"} log "hi", "u":"a"',
            '{"a": \'bad\', "password": "[REDACTED]"} log "hi", "u":"a"',
        ),
        (
            '{"password": "alpha"} S3CR3T_BETA',
            '{"password":"[REDACTED]"} S3CR3T_BETA',
        ),
        (
            "secrets:hunter2: extra",
            "secrets:[REDACTED] extra",
        ),
        (
            "credentials:hunter2:foo: bar",
            "credentials:[REDACTED] bar",
        ),
        (
            "auth:hunter2:error: fail",
            "auth:[REDACTED] fail",
        ),
        (
            "password:hunter2:region=us-east-1",
            "password:[REDACTED]",
        ),
        (
            "auth:hunter2:region=us-east-1",
            "auth:[REDACTED]",
        ),
        (
            "password:hunter2:auth_error: CARD_EXPIRED",
            "password:[REDACTED] CARD_EXPIRED",
        ),
        (
            "auth:hunter2:auth_error: CARD_EXPIRED",
            "auth:[REDACTED] CARD_EXPIRED",
        ),
        (
            "credentials:hunter2: foo=bar",
            "credentials:[REDACTED] foo=bar",
        ),
        (
            "password:hunter2: foo=bar",
            "password:[REDACTED] foo=bar",
        ),
        (
            "auth:hunter2:status: ok",
            "auth:[REDACTED] ok",
        ),
        (
            "auth:v1:hunter2:status: ok",
            "auth:[REDACTED] ok",
        ),
        (
            "auth:hunter2:token_count: 5",
            "auth:[REDACTED] 5",
        ),
        (
            "auth/hunter2: status: ok",
            "auth/hunter2: [REDACTED] ok",
        ),
        (
            "auth/hunter2:token_count: 5",
            "auth/hunter2:[REDACTED] 5",
        ),
        (
            "password:foo:bar=S3CR3T",
            "password:foo:bar=[REDACTED]",
        ),
        (
            "auth: v1/v2: status: ok",
            "auth: v1/v2: status: ok",
        ),
        (
            "auth:v1:token_count: 5",
            "auth:v1:token_count: 5",
        ),
        (
            'auth:myService/password="SampleMarker"',
            'auth:myService/password="[REDACTED]"',
        ),
        (
            'auth:myService.password="SampleMarker"',
            'auth:myService.password="[REDACTED]"',
        ),
        (
            'auth:_Password="SampleMarker"',
            'auth:_Password="[REDACTED]"',
        ),
        (
            'auth:v1/myService:password="SampleMarker"',
            'auth:v1/myService:password="[REDACTED]"',
        ),
        (
            'auth:Tenant/v1:password="SampleMarker"',
            'auth:Tenant/v1:password="[REDACTED]"',
        ),
        (
            'auth:myService.prod:password="SampleMarker"',
            'auth:myService.prod:password="[REDACTED]"',
        ),
        (
            'auth:v1.Tenant:password="SampleMarker"',
            'auth:v1.Tenant:password="[REDACTED]"',
        ),
        (
            'auth:v1/custom_field="SampleMarker"',
            'auth:v1/custom_field="[REDACTED]"',
        ),
        (
            'authentication:\n  v1/key="SampleMarker"',
            'authentication:\n  v1/key="[REDACTED]"',
        ),
        (
            'credentials:v1/key="SampleMarker"',
            'credentials:v1/key="[REDACTED]"',
        ),
        (
            'credentials:\n  v1/key="SampleMarker"',
            'credentials:\n  v1/key="[REDACTED]"',
        ),
        (
            'token:\n  v1/key="SampleMarker"',
            'token:\n  v1/key="[REDACTED]"',
        ),
        (
            "endpoint: https://example.com/a=1?b=2",
            "endpoint: https://example.com/a=1?b=2",
        ),
        (
            "path: /usr/local/bin=ignored",
            "path: /usr/local/bin=ignored",
        ),
        (
            "auth: status=ok",
            "auth: status=ok",
        ),
        (
            "auth:status=ok",
            "auth:status=ok",
        ),
        (
            "auth:https://example.com/callback?code=public",
            "auth:[REDACTED]",
        ),
    ):
      with self.subTest(sample=sample):
        self.assertEqual(scrub_sensitive_text(sample), expected)

    self.assertEqual(_extract_error_markers(r"foo\\nCARD_EXPIRED"), [])

    s3_colon_payload = "credentials: hunter2: status: ok"
    s3_out = extract_s3_outcome_first(
        {
            "turns": [
                {
                    "turn_index": 0,
                    "turn_id": "inv_colon_chain",
                    "events": [
                        {
                            "author": "customer_ops_agent",
                            "content": {
                                "role": "model",
                                "parts": [{"text": s3_colon_payload}],
                            },
                        }
                    ],
                }
            ]
        },
        metrics={"error_summary": s3_colon_payload},
    )
    self.assertIn("has_error=True", s3_out)
    self.assertIn("credentials=[REDACTED]", s3_out)
    self.assertIn("status=ok", s3_out)
    self.assertNotIn("hunter2", s3_out)

    for perf_label, perf_payload, expected_tail in (
        (
            "malformed_json_repeated",
            '{"password": "x" a '
            + " ".join('{"password": "x" a' for _ in range(1600))
            + ', "z": 1}',
            '"z"',
        ),
        (
            "malformed_json_braces_newlines",
            "{\n"
            + " ".join('{"password": "x" {} a' for _ in range(1600))
            + ',\n"z": 1}',
            '"z"',
        ),
        (
            "escaped_log_repeated",
            '{"log": "'
            + " ".join(r"{\"password\": \"x\" a" for _ in range(1600))
            + r", \"z\": 1}",
            r"\"z\"",
        ),
        (
            "escaped_member_repeated",
            r"{\"password\": \"x\" a "
            + " ".join(r"{\"password\": \"x\" a" for _ in range(1600))
            + r", \"z\": 1}",
            r"\"z\"",
        ),
        (
            "escaped_braces_repeated",
            r"{\"password\": \"x\" {} " * 1600 + r", \"z\": 1}",
            r"\"z\"",
        ),
    ):
      with self.subTest(perf_shape=perf_label):
        out, elapsed = _timed_scrub_median_sec(
            scrub_sensitive_text, perf_payload
        )
        self.assertLess(elapsed, 5.0, f"{perf_label} took {elapsed:.3f}s")
        self.assertIn("[REDACTED]", out)
        self.assertIn(expected_tail, out)

    escaped_secret_tail = (
        r"{ \"secret\":\n\"SampleMarker_password\': \"alpha\" "
        + ("b" * 6500)
        + r" S3CR3T_BETA\"}"
    )
    for boundary_label, payload, forbidden_tokens, required_tokens in (
        (
            "boundary_secret_after_prefix",
            '{"password": "x" oops '
            + ("a" * 9974)
            + ' { "secret":\n"SampleMarker_password\': "alpha" '
            + ("b" * 6500)
            + ' S3CR3T_BETA"}',
            ("alpha", "S3CR3T_BETA", "bbbbbb"),
            ("[REDACTED]",),
        ),
        (
            "boundary_password_after_prefix",
            '{"password": "x" oops '
            + ("a" * 9974)
            + ' {"password": "alpha" '
            + ("b" * 6500)
            + ' S3CR3T_BETA"}',
            ("alpha", "S3CR3T_BETA", "bbbbbb"),
            ("[REDACTED]",),
        ),
        (
            "boundary_escaped_with_leading_key",
            r"{\"k\":1, \"password\": \"x\" oops "
            + ("a" * 9974)
            + " "
            + escaped_secret_tail,
            ("alpha", "S3CR3T_BETA", "bbbbbb"),
            ("[REDACTED]",),
        ),
        (
            "boundary_escaped_password_prefix",
            r"{\"password\": \"x\" oops "
            + ("a" * 9974)
            + " "
            + escaped_secret_tail,
            ("alpha", "S3CR3T_BETA", "bbbbbb"),
            ("[REDACTED]",),
        ),
        (
            "boundary_16365_second_password",
            '{"password": "a" ' + ("x" * 16365) + ' {"password": "b" S3CR3T"}}',
            ("S3CR3T",),
            ("[REDACTED]",),
        ),
        (
            "boundary_16400_second_password",
            '{"password": "a" ' + ("x" * 16400) + ' {"password": "b" S3CR3T"}}',
            ("S3CR3T",),
            ("[REDACTED]",),
        ),
        (
            "boundary_16250_multiline_value",
            '{"secret": "first" '
            + ("a" * 16250)
            + '{"password": "x" "y"\n'
            + ("b" * 300)
            + '", "k": 1}',
            ("bbbbbb",),
            ("[REDACTED]", "k"),
        ),
        (
            "boundary_16250_escaped_multiline_value",
            '{"log": "{\\"secret\\": \\"first\\" '
            + ("a" * 16250)
            + '{\\"password\\": \\"x\\" \\"y\\"\\n'
            + ("b" * 300)
            + '\\", \\"k\\": 1}"}',
            ("bbbbbb",),
            ("[REDACTED]", "k"),
        ),
    ):
      with self.subTest(boundary_shape=boundary_label):
        out = scrub_sensitive_text(payload)
        for token in forbidden_tokens:
          self.assertNotIn(token, out)
        for token in required_tokens:
          self.assertIn(token, out)

    for budget_label, budget_payload, expected_tail in (
        (
            "repeated_malformed_with_long_tail",
            '{"password": "x" a ' * 2000 + "b" * 20000,
            "bbbbb",
        ),
        (
            "repeated_malformed_with_trailing_object",
            '{"password": "x" a ' * 2000 + ' {"k": "v"}',
            '{"k": "v"}',
        ),
        (
            "repeated_malformed_nested_kv",
            '{"password": "x" {"k": "v"} ' * 1600,
            '{"k": "v"}',
        ),
    ):
      with self.subTest(budget_shape=budget_label):
        out, elapsed = _timed_scrub_median_sec(
            scrub_sensitive_text, budget_payload
        )
        self.assertLess(elapsed, 5.0, f"{budget_label} took {elapsed:.3f}s")
        self.assertIn("[REDACTED]", out)
        self.assertIn(expected_tail, out)

    for sample, expected in (
        (
            '{"secret":\n'
            '"SampleMarker_password\': "alpha" {"a": "b", "c": "d"}'
            ' S3CR3T_BETA", "other": 1}',
            '{"secret":\n"[REDACTED]", "other": 1}',
        ),
        (
            '{"password": "alpha" {"a": "b", "c": "d"} S3CR3T_BETA",'
            ' "other": 1}',
            '{"password": "[REDACTED]", "other": 1}',
        ),
        (
            r'{"log": "{\"password\": \"alpha\" {\"a\": \"b\", \"c\": \"d\"} '
            r'S3CR3T_BETA\", \"other\": 1}"}',
            r'{"log":"{\"password\": \"[REDACTED]\", \"other\": 1}"}',
        ),
        (
            '{"password": "alpha" [{"a": "b", "c": "d"}] S3CR3T_BETA",'
            ' "other": 1}',
            '{"password": "[REDACTED]", "other": 1}',
        ),
        (
            '{"password": "val" "a"\n} S3CR3T", "other": 1}',
            '{"password": "[REDACTED]", "other": 1}',
        ),
        (
            '{"password": "val" "a"\n] S3CR3T", "other": 1}',
            '{"password": "[REDACTED]", "other": 1}',
        ),
        (
            r"{\"password\": \"val\" \"a\"\n} S3CR3T\", \"other\": 1}",
            r"{\"password\": \"[REDACTED]\", \"other\": 1}",
        ),
        (
            '{"a": \'bad\', "password": "alpha"} S3CR3T", "other": 1}',
            '{"a": \'bad\', "password": "[REDACTED]", "other": 1}',
        ),
        (
            '{"secret":\n"password\': "alpha"\n} S3CR3T_BETA",'
            '"error_code":"CARD_EXPIRED"}',
            '{"secret":\n"[REDACTED]","error_code":"CARD_EXPIRED"}',
        ),
        (
            'password: "SampleMarker_password\': one\ntwo"',
            'password: "[REDACTED]"',
        ),
        (
            '{"password":\n"SampleMarker_password\': 1) secret\ntwo"}',
            '{"password":\n"[REDACTED]"}',
        ),
        (
            '{"password":\n"SampleMarker_password\': 1) secret\ntwo",'
            ' "error": "ok"}',
            '{"password":\n"[REDACTED]", "error": "ok"}',
        ),
        (
            'auth:hunter2:{"secret": "S3CR3T"}',
            'auth:[REDACTED]"secret":"[REDACTED]"}',
        ),
        (
            'auth:hunter2: {"secret": "S3CR3T"}',
            'auth:[REDACTED] {"secret":"[REDACTED]"}',
        ),
        (
            "credentials:hunter2:[1, 2]",
            "credentials:[REDACTED], 2]",
        ),
        (
            'auth:hunter2: "password": "S3CR3T"',
            'auth:[REDACTED] "password": "[REDACTED]"',
        ),
        (
            'secrets:hunter2:{"k": "S3CR3T"}',
            'secrets:[REDACTED]"k": "S3CR3T"}',
        ),
        (
            'credentials:hunter2: {"k": "S3CR3T"}',
            'credentials:[REDACTED] {"k": "S3CR3T"}',
        ),
        (
            'auth:v1:hunter2:{"k": "S3CR3T"}',
            'auth:[REDACTED]"k": "S3CR3T"}',
        ),
        (
            'auth:v1:{"secret": "S3CR3T"}',
            "auth:v1:[REDACTED]",
        ),
        (
            'auth:my-service/v1.0:password="SampleMarker"',
            'auth:my-service/v1.0:password="[REDACTED]"',
        ),
        (
            'auth:v1.0:password="SampleMarker"',
            'auth:v1.0:password="[REDACTED]"',
        ),
        (
            'auth:tenant/2:password="SampleMarker"',
            'auth:tenant/2:password="[REDACTED]"',
        ),
        (
            'auth:api.v1.2:password="SampleMarker"',
            'auth:api.v1.2:password="[REDACTED]"',
        ),
        (
            'auth:v1/2026:password="SampleMarker"',
            'auth:v1/2026:password="[REDACTED]"',
        ),
        (
            'auth:127.0.0.1:password="SampleMarker"',
            'auth:127.0.0.1:password="[REDACTED]"',
        ),
        (
            "endpoint: 1.0/2.0=ok",
            "endpoint: 1.0/2.0=ok",
        ),
        (
            "path: 127.0.0.1:8080=ok",
            "path: 127.0.0.1:8080=ok",
        ),
        (
            "date: 2026.11/27=ok",
            "date: 2026.11/27=ok",
        ),
    ):
      with self.subTest(sample=sample):
        self.assertEqual(scrub_sensitive_text(sample), expected)

    for exhaustion_label, payload in (
        (
            "chunked_nested_kv_20x300",
            ('{"password": "a" {"k": "v"} ' + ("x" * 300) + " ") * 20
            + '{"password": "b" S3CR3T"}',
        ),
        (
            "chunked_nested_kv_9x2200",
            ('{"password": "a" {"k": "v"} ' + ("x" * 2200) + " ") * 9
            + '{"password": "b" S3CR3T"}',
        ),
        (
            "chunked_quoted_20x300",
            ('{"password": "a" "q" ' + ("x" * 300) + " ") * 20
            + '{"password": "b" S3CR3T"}',
        ),
        (
            "repeated_nested_kv_44",
            '{"password": "a" {"k": "v"} ' * 44 + '{"password": "b" S3CR3T"}',
        ),
        (
            "repeated_short_pwd_45",
            '{"pwd":"a"b{' * 45 + ' {"password": "b" S3CR3T"}',
        ),
    ):
      with self.subTest(exhaustion_shape=exhaustion_label):
        out = scrub_sensitive_text(payload)
        self.assertNotIn("S3CR3T", out)
        self.assertIn("[REDACTED]", out)

    for reps in (1, 4, 5, 20):
      with self.subTest(long_gap_secret_reps=reps):
        a20k = (
            '{"password": "x" {{ "q"}, oops ' * reps
            + ("a" * 20000)
            + ' { "secret":\n"SampleMarker_password\': "alpha" '
            + ("b" * 100)
            + ' S3CR3T_BETA"}'
        )
        out = scrub_sensitive_text(a20k)
        self.assertNotIn("alpha", out)
        self.assertNotIn("S3CR3T_BETA", out)
        self.assertNotIn("bbbbbb", out)

    for reps in (1, 5, 20):
      with self.subTest(long_gap_password_reps=reps):
        b20k = (
            '{"password": "x" { "q"}, ' * reps
            + ("a" * 20000)
            + ' {"password": "alpha" '
            + ("b" * 100)
            + ' S3CR3T_BETA"}'
        )
        out = scrub_sensitive_text(b20k)
        self.assertNotIn("alpha", out)
        self.assertNotIn("S3CR3T_BETA", out)
        self.assertNotIn("bbbbbb", out)

    for scaling_label, payload in (
        (
            "repeated_nested_kv_1600_tail",
            ('{"password": "x" {"k": "v"} ' * 1600)
            + '{"password": "b" S3CR3T"}',
        ),
        (
            "escaped_nested_kv_1600_tail",
            (r"{\"password\": \"x\" {\"k\": \"v\"} " * 1600)
            + r"{\"password\": \"b\" S3CR3T\"}",
        ),
        (
            "backslash_scaling_unescaped",
            ('{"password": "a" { "q"}, ' * 253)
            + ("\\" * 64000)
            + ' {"password": "b" S3CR3T"}',
        ),
        (
            "backslash_scaling_escaped",
            (r"{\"password\": \"a\" { \"q\"}, " * 253)
            + ("\\" * 64000)
            + r" {\"password\": \"b\" S3CR3T\"}",
        ),
    ):
      with self.subTest(scaling_shape=scaling_label):
        out, elapsed = _timed_scrub_median_sec(scrub_sensitive_text, payload)
        self.assertLess(elapsed, 5.0, f"{scaling_label} took {elapsed:.3f}s")
        self.assertNotIn("S3CR3T", out)

  def test_scrub_outer_prose_and_brace_continuation_regressions(self):
    from analytics.trajectory_extractor import scrub_sensitive_text  # noqa: PLC0415

    for sample, expected in (
        (
            '{"a": \'bad\', "password": "alpha"}\nS3CR3T", "other": 1}',
            '{"a": \'bad\', "password": "[REDACTED]", "other": 1}',
        ),
        (
            r"{\"password\": \"alpha\"}\nS3CR3T\", \"other\": 1}",
            r"{\"password\": \"[REDACTED]\", \"other\": 1}",
        ),
        (
            '{"a": \'bad\', "password": "alpha"} '
            'foo \\"bar" S3CR3T", "other": 1}',
            '{"a": \'bad\', "password": "[REDACTED]", "other": 1}',
        ),
        (
            r"{\"a\": \'bad\', \"password\": \"alpha\"} "
            r"foo \\\"bar\" S3CR3T\", \"other\": 1}",
            r"{\"a\": \'bad\', \"password\": \"[REDACTED]\", \"other\": 1}",
        ),
        (
            '{"a": \'bad\', "password": "alpha"} '
            + ("x" * 4200)
            + ' S3CR3T", "other": 1}',
            '{"a": \'bad\', "password": "[REDACTED]", "other": 1}',
        ),
        (
            '{"a": \'bad\', "password": "alpha"} '
            '"unclosed\nS3CR3T", "other": 1}',
            '{"a": \'bad\', "password": "[REDACTED]", "other": 1}',
        ),
        (
            '{"password": "alpha" {"re": "a{1,3"} S3CR3T", "other": 1}',
            '{"password": "[REDACTED]", "other": 1}',
        ),
        (
            '{"secret":\n"SampleMarker_password\': "alpha" '
            '{ S3CR3T_BETA","error_code":"CARD_EXPIRED"}',
            '{"secret":\n"[REDACTED]","error_code":"CARD_EXPIRED"}',
        ),
        (
            '{"password": "val" "{" S3CR3T_BETA", "other": 1}',
            '{"password": "[REDACTED]", "other": 1}',
        ),
        (
            '{"password": "val" "[" S3CR3T_BETA", "other": 1}',
            '{"password": "[REDACTED]", "other": 1}',
        ),
        (
            '{"password": "alpha" [ {"a": "b"} S3CR3T_BETA", "other": 1}',
            '{"password": "[REDACTED]", "other": 1}',
        ),
    ):
      with self.subTest(sample=sample[:80]):
        self.assertEqual(scrub_sensitive_text(sample), expected)

    for payload, forbidden in (
        (
            '{"password": "alpha" } outer\nS3CR3T_BETA", "other": 1}',
            ("S3CR3T_BETA",),
        ),
        (
            '{"password": "alpha" } '
            + ("b" * 4100)
            + ' S3CR3T_BETA", "other": 1}',
            ("S3CR3T_BETA", "bbbb"),
        ),
    ):
      with self.subTest(outer_prose=payload[:64]):
        out = scrub_sensitive_text(payload)
        self.assertIn("[REDACTED]", out)
        self.assertIn('"other": 1', out)
        for token in forbidden:
          self.assertNotIn(token, out)

    jsonl_tail = (
        '{"password": "SECRET1"\n{"msg": "keep_me"}\nnote: "benign_text"'
    )
    jsonl_expected = (
        '{"password": "[REDACTED]"\n{"msg": "keep_me"}\nnote: "benign_text"'
    )
    for prefix in ("", '{"ok":1}\n' * 32):
      with self.subTest(jsonl_prefix_len=len(prefix)):
        self.assertEqual(
            scrub_sensitive_text(prefix + jsonl_tail),
            prefix + jsonl_expected,
        )

    for sample in (
        '{"password": "a" "b { S3CR3T"}',
        '{"password": "a" "b { S3CR3T"',
        '{"password": "a" "b [ S3CR3T"}',
        r"{\"password\": \"a\" \"b { S3CR3T\"}",
        '{"password": "a" "b {{ S3CR3T"}',
        '{"password": "a" {"b": 1} S3CR3T"',
    ):
      with self.subTest(brace_sample=sample):
        out = scrub_sensitive_text(sample)
        self.assertNotIn("S3CR3T", out)
        self.assertIn("[REDACTED]", out)

    nested_escaped = (
        r'{"log": "{\"password\": \"alpha\" '
        r'{\"k\": \"{\"} S3CR3T\", \"other\": 1}"}'
    )
    out = scrub_sensitive_text(nested_escaped)
    self.assertNotIn("S3CR3T", out)
    self.assertIn("[REDACTED]", out)
    self.assertIn("other", out)

  def test_scrub_multiline_and_unclosed_key_regressions(self):
    from analytics.trajectory_extractor import scrub_sensitive_text  # noqa: PLC0415

    multiline_cases = (
        (
            '{"password":\n"SampleMarker_password\': line1\n'
            'he said "phrase" S3CR3T", "error_code": "CARD_EXPIRED"}'
        ),
        (
            '{"password":\n"SampleMarker_password\': one\n'
            '\'two\': S3CR3T", "error_code": "CARD_EXPIRED"}'
        ),
    )
    for prefix in ("", '{"ok":1} ' * 32):
      for idx, payload in enumerate(multiline_cases):
        with self.subTest(prefix_len=len(prefix), case_idx=idx):
          out = scrub_sensitive_text(prefix + payload)
          self.assertNotIn("SampleMarker", out)
          self.assertNotIn("S3CR3T", out)
          self.assertIn('"error_code": "CARD_EXPIRED"', out)

    for payload in (
        '{"msg": "unclosed_val, "' + ("a" * 1020) + '.password": "S3CR3T"}',
        r'{"log": "{\"msg\": \"unclosed_val, \"'
        + ("a" * 1020)
        + r'.password\": \"S3CR3T\"}"}',
        '{"msg": "unclosed_val, " password": "S3CR3T"}',
        r'{"log": "{\"msg\": \"unclosed_val, \" password\": \"S3CR3T\"}"}',
    ):
      with self.subTest(unclosed_key=payload[:64]):
        out = scrub_sensitive_text(payload)
        self.assertNotIn("S3CR3T", out)
        self.assertIn("[REDACTED]", out)

    benign = '{"msg": "hello " " world: ok"}'
    self.assertEqual(scrub_sensitive_text(benign), benign)

  def test_scrub_next_line_lookalikes_and_numeric_tail_parity(self):
    from analytics.trajectory_extractor import scrub_sensitive_text  # noqa: PLC0415

    for sample, expected in (
        (
            'password:\n"SampleMarker_password\': one\ntwo"',
            'password:\n"[REDACTED]"',
        ),
        (
            'password:\n  "SampleMarker_password\': one\ntwo"',
            'password:\n  "[REDACTED]"',
        ),
        (
            'password =\n"SampleMarker_password\': one\ntwo"',
            'password =\n"[REDACTED]"',
        ),
        (
            "password:\n'SampleMarker_password\": one\ntwo'",
            "password:\n'[REDACTED]'",
        ),
        (
            'auth: v1:\n"SampleMarker_password\': one\ntwo"',
            'auth: v1:\n"[REDACTED]"',
        ),
        (
            'auth:\nhunter2/v1: "password": "S3CR3T"',
            'auth:\n[REDACTED] "password": "[REDACTED]"',
        ),
        (
            'auth:\nhunter2/prod: "password": "S3CR3T"',
            'auth:\n[REDACTED] "password": "[REDACTED]"',
        ),
        (
            'credentials:\nhunter2/v1: "secret": "S3CR3T"',
            'credentials:\n[REDACTED] "secret": "[REDACTED]"',
        ),
        (
            "auth: v1: status.200=ok",
            "auth: v1: status.200=ok",
        ),
        (
            "auth: err/404=not_found",
            "auth: [REDACTED]",
        ),
        (
            "auth: v1.0=ok",
            "auth: [REDACTED]",
        ),
        (
            "auth:v1.0:status=ok",
            "auth:v1.0:status=ok",
        ),
        (
            "auth:127.0.0.1:status=ok",
            "auth:127.0.0.1:status=ok",
        ),
        (
            "auth:tenant/2:status=ok",
            "auth:tenant/2:status=ok",
        ),
        (
            "auth:v1/2026:status=ok",
            "auth:v1/2026:status=ok",
        ),
        (
            "auth:api.v1.2:status=ok",
            "auth:api.v1.2:status=ok",
        ),
        (
            "auth:v1.0:token_count=3",
            "auth:v1.0:token_count=3",
        ),
        (
            "auth:127.0.0.1:token_count=3",
            "auth:127.0.0.1:token_count=3",
        ),
        (
            "auth:v1.0:error_code=CARD_EXPIRED",
            "auth:v1.0:error_code=CARD_EXPIRED",
        ),
        (
            "auth:hunter2:status=ok",
            "auth:[REDACTED]",
        ),
        (
            "auth/hunter2:status=ok",
            "auth/hunter2:[REDACTED]",
        ),
    ):
      with self.subTest(sample=sample):
        self.assertEqual(scrub_sensitive_text(sample), expected)

  def test_scrub_linear_scaling_prose_and_unclosed_quotes(self):
    from analytics.trajectory_extractor import scrub_sensitive_text  # noqa: PLC0415

    for scaling_label, payload in (
        (
            "prose_same_line_no_sp_brace_6400",
            '{"password": "S3CR3T"} oops ' * 6400,
        ),
        (
            "prose_esc_same_line_no_sp_brace_6400",
            '{"log": "' + r"{\"password\": \"S3CR3T\"} oops " * 6400 + '"}',
        ),
        (
            "repeated_brace_long_tail_1600",
            '{"password": "S3CR3T"} oops ' * 1600 + ("a" * 10000),
        ),
        (
            "repeated_unclosed_quote_opens_kv",
            ('{"msg": "unclosed ' + ("a" * 40)) * 400
            + '", "password": "S3CR3T"}',
        ),
        (
            "repeated_unclosed_edq_quote_opens_kv",
            (r"\"x\" " + r"\\\"y") * 800 + " password=S3CR3T",
        ),
    ):
      with self.subTest(scaling_shape=scaling_label):
        out, elapsed = _timed_scrub_median_sec(scrub_sensitive_text, payload)
        self.assertLess(elapsed, 5.0, f"{scaling_label} took {elapsed:.3f}s")
        self.assertNotIn("S3CR3T", out)
        self.assertIn("[REDACTED]", out)

  def test_scrub_nested_delegation_brace_prose_and_version_word_redactions(
      self,
  ):
    from analytics.trajectory_extractor import scrub_sensitive_text  # noqa: PLC0415

    exact_cases = (
        (
            '{"secret":\n"note\': "S3CR3T_VAL_1", "other": 1}',
            '{"secret":\n"[REDACTED]", "other": 1}',
        ),
        ("auth:letmein/1:status=ok", "auth:[REDACTED]"),
        ("auth:letmein.2:status=ok", "auth:[REDACTED]"),
        ("auth:opensesame/2026:status=ok", "auth:[REDACTED]"),
        ("auth:letmein/v1.0:status=ok", "auth:[REDACTED]"),
        (
            'auth:letmein/1: "password": "S"',
            'auth:[REDACTED] "password": "[REDACTED]"',
        ),
        ("auth:letmein/1: status: ok", "auth:[REDACTED] status: ok"),
        (
            'credentials:opensesame/2026: "secret": "S"',
            'credentials:[REDACTED] "secret": "[REDACTED]"',
        ),
        (
            'token:letmein.1: "secret": "S"',
            'token:[REDACTED] "secret": "[REDACTED]"',
        ),
        ("password:tenant/2:status=ok", "password:tenant/2:status=ok"),
        ("password:v1/2026:status=ok", "password:v1/2026:status=ok"),
        ("auth:shard.3:status=ok", "auth:[REDACTED]"),
        ("auth:region/2:status=ok", "auth:region/2:status=ok"),
        ("auth:zone-1.2:status=ok", "auth:[REDACTED]"),
        ("auth:cluster/2:status=ok", "auth:cluster/2:status=ok"),
        ("auth:service.3:status=ok", "auth:service.3:status=ok"),
        (
            '{"a": \'bad\', "password": "alpha"} foo {"x": 1} S3CR3T", "other": 1}',
            '{"a": \'bad\', "password": "[REDACTED]", "other": 1}',
        ),
        (
            '{"password": "alpha" "x"} {"k": 1} S3CR3T", "other": 1}',
            '{"password": "[REDACTED]", "other": 1}',
        ),
        (
            '{"a": \'bad\', "password": "alpha"} foo \\" S3CR3T", "other": 1}',
            '{"a": \'bad\', "password": "[REDACTED]", "other": 1}',
        ),
        (
            '{"a": \'bad\', "password": "alpha"} foo \\"bar" S3CR3T", "other": 1}',
            '{"a": \'bad\', "password": "[REDACTED]", "other": 1}',
        ),
        (
            '{"password": "alpha"\n{"k": "v"} S3CR3T", "other": 1}',
            '{"password": "[REDACTED]", "other": 1}',
        ),
        (
            '{"password": "alpha" oops\n{"k": "v"} S3CR3T", "other": 1}',
            '{"password": "[REDACTED]", "other": 1}',
        ),
        (
            '{"password": "alpha"} log "hi", "u":"a"',
            '{"password":"[REDACTED]"} log "hi", "u":"a"',
        ),
        (
            '{"password": "alpha" {"re": "line1\n{line2"} S3CR3T", "other": 1}',
            '{"password": "[REDACTED]", "other": 1}',
        ),
        (
            '{"password": "alpha" {\'k\': \'{\'} S3CR3T", "other": 1}',
            '{"password": "[REDACTED]", "other": 1}',
        ),
        (
            'password: "SampleMarker_password\': "alpha", "other": 1\ntwo"',
            'password: "[REDACTED]"alpha", "other": 1\ntwo"',
        ),
        (
            '{"password":\n"SampleMarker_password\': one\nauth: hello S3CR3T", "error_code": "CARD_EXPIRED"}',
            '{"password":\n"[REDACTED]", "error_code": "CARD_EXPIRED"}',
        ),
        (
            '{"secret":\n"SampleMarker_password\': one", "error_code": "CARD_EXPIRED"}',
            '{"secret":"[REDACTED]","error_code":"CARD_EXPIRED"}',
        ),
        (
            'auth: credentials:\n  "password\': "S"\n' * 4,
            'auth: credentials:\n  "password\': "[REDACTED]"\n' * 4,
        ),
    )
    for sample, expected in exact_cases:
      with self.subTest(sample=sample[:80]):
        self.assertEqual(scrub_sensitive_text(sample), expected)

    multi_elem_cases = (
        (
            '{"secret":\n"password\': "S3CR3T_VAL_1", {"secret":\n"password\': "S3CR3T_VAL_2"',
            ("S3CR3T_VAL_1", "S3CR3T_VAL_2"),
            ("[REDACTED]",),
        ),
        (
            '{"secret":\n"password\': "S3CR3T_VAL_1", {"secret":\n"password\': "S3CR3T_VAL_2"}',
            ("S3CR3T_VAL_1", "S3CR3T_VAL_2"),
            ("[REDACTED]",),
        ),
        (
            '{"secret":\n"note\': "S3CR3T_VAL_1", {"secret":\n"note\': "S3CR3T_VAL_2"',
            ("S3CR3T_VAL_1", "S3CR3T_VAL_2"),
            ("[REDACTED]",),
        ),
        (
            r'{"log": "{\"a\": \'bad\', \"password\": \"alpha\"} foo {\"x\": 1} S3CR3T\", \"other\": 1}"',
            ("S3CR3T",),
            ("[REDACTED]", "other"),
        ),
        (
            r'{"log": "{\"a\": \'bad\', \"password\": \"alpha\"} foo \\\" S3CR3T\", \"other\": 1}"',
            ("S3CR3T",),
            ("[REDACTED]", "other"),
        ),
        (
            r'{"log": "{\"password\": \"alpha\" {\'k\': \'{\'} S3CR3T\", \"other\": 1}"}',
            ("S3CR3T",),
            ("[REDACTED]", "other"),
        ),
    )
    for payload, forbidden, required in multi_elem_cases:
      with self.subTest(multi_elem=payload[:80]):
        out = scrub_sensitive_text(payload)
        for token in forbidden:
          self.assertNotIn(token, out)
        for token in required:
          self.assertIn(token, out)

  def test_scrub_linear_scaling_nested_json_candidates(self):
    from analytics.trajectory_extractor import scrub_sensitive_text  # noqa: PLC0415

    for scaling_label, payload, required, forbidden in (
        (
            "enclosing_candidates_32k",
            ('{"secret":\n"password\': "S3CR3T", {"other": 1}\n' * 800)[:32000],
            (),
            (),
        ),
        (
            "plain_note_candidates_32k",
            ('{"secret":\n"note\': "S3CR3T", ' * 1200)[:32000],
            (),
            (),
        ),
        (
            "long_ml_quoted_brace_4k",
            '{"password": "alpha" {"re": "x'
            + ("\ny" * 4000)
            + '"} S3CR3T", "other": 1}',
            ('"other": 1',),
            ("y\ny",),
        ),
    ):
      with self.subTest(scaling_shape=scaling_label):
        out, elapsed = _timed_scrub_median_sec(scrub_sensitive_text, payload)
        self.assertLess(elapsed, 5.0, f"{scaling_label} took {elapsed:.3f}s")
        self.assertNotIn("S3CR3T", out)
        self.assertIn("[REDACTED]", out)
        for token in required:
          self.assertIn(token, out)
        for token in forbidden:
          self.assertNotIn(token, out)

  def test_scrub_multiline_continuation_delegation_and_uppercase_ns(self):
    from analytics.trajectory_extractor import scrub_sensitive_text  # noqa: PLC0415

    prefix_32 = '{"ok":1} ' * 32
    premature_payload = (
        '{"a": \'bad\', "password": "alpha"} foo '
        '{"re": "line1\n}line2"} S3CR3T", "other": 1}'
    )
    premature_expected = '{"a": \'bad\', "password": "[REDACTED]", "other": 1}'
    exact_cases = (
        (
            "line3_close_quote_auth_hdr",
            (
                '{"password":\n"SampleMarker_password\': one\n'
                'auth: hello\nS3CR3T", "error_code": "CARD_EXPIRED"}'
            ),
            '{"password":\n"[REDACTED]", "error_code": "CARD_EXPIRED"}',
        ),
        (
            "line3_close_quote_api_key_hdr",
            (
                '{"password":\n"SampleMarker_password\': one\n'
                'api_key: hello\nS3CR3T", "error_code": "CARD_EXPIRED"}'
            ),
            '{"password":\n"[REDACTED]", "error_code": "CARD_EXPIRED"}',
        ),
        (
            "delegation_guard_comma_obj",
            '{"secret":\n"password\': S3CR3T_1\nS3CR3T_2", {"other": 1}',
            '{"secret":\n"[REDACTED]", {"other": 1}',
        ),
        (
            "delegation_guard_space_comma_obj",
            '{"secret":\n"password\': S3CR3T_1\nS3CR3T_2" , {"other": 1}',
            '{"secret":\n"[REDACTED]" , {"other": 1}',
        ),
        (
            "delegation_guard_quoted_child",
            '{"secret":\n"password\': "S3CR3T_1" S3CR3T_2", {"other": 1}',
            '{"secret":\n"[REDACTED]", {"other": 1}',
        ),
        (
            "embedded_multiline_auth_open_brace",
            (
                '{"password": "alpha" {"re": "line1\n'
                'auth: { public"} , "other": "keep"}'
            ),
            (
                '{"password": "[REDACTED]" {"re": "line1\n'
                'auth: [REDACTED]"} , "other": "keep"}'
            ),
        ),
        (
            "embedded_multiline_auth_open_brace_escaped",
            (
                '{"log": "{\\"password\\": \\"alpha\\" '
                '{\\"re\\": \\"line1\nauth: { public\\"} , '
                '\\"other\\": \\"keep\\"}"}'
            ),
            (
                '{"log": "{\\"password\\": \\"[REDACTED]\\" '
                '{\\"re\\": \\"line1\nauth: [REDACTED]\\"} , '
                '\\"other\\": \\"keep\\"}"}'
            ),
        ),
        (
            "embedded_multiline_auth_ok_tail_secret",
            (
                '{"password": "alpha" {"re": "line1\n'
                'auth: ok"} S3CR3T", "other": 1}'
            ),
            '{"password": "[REDACTED]", "other": 1}',
        ),
        (
            "embedded_multiline_close_brace_tail_secret",
            (
                '{"password": "alpha" {"re": "line1\n}", "k": "v"} '
                'S3CR3T", "other": 1}'
            ),
            '{"password": "[REDACTED]", "other": 1}',
        ),
        (
            "embedded_multiline_close_brace_double_close",
            '{"password": "alpha" {"re": "line1\n}", "k": "v"}}',
            '{"password": "[REDACTED]" {"re": "line1\n}", "k": "v"}}',
        ),
        (
            "premature_close_brace_depth1_multiline",
            premature_payload,
            premature_expected,
        ),
        (
            "premature_close_brace_depth1_with_prefix",
            prefix_32 + premature_payload,
            prefix_32 + premature_expected,
        ),
        (
            "uppercase_ns_numeric_tail",
            "auth:OPENSESAME/2:status=ok",
            "auth:[REDACTED]",
        ),
        (
            "uppercase_ns_numeric_tail_before_password",
            'auth:OPENSESAME/2: "password": "S"',
            'auth:[REDACTED] "password": "[REDACTED]"',
        ),
        (
            "safe_env_ns_numeric_tail",
            "auth:prod/2:status=ok",
            "auth:prod/2:status=ok",
        ),
    )
    for label, payload, expected in exact_cases:
      with self.subTest(label=label):
        self.assertEqual(scrub_sensitive_text(payload), expected)

    marker_cases = (
        (
            "escaped_line3_close_quote_auth_hdr",
            (
                '{"log": "{\\"password\\":\\n\\"SampleMarker_password\\\': '
                'one\\nauth: hello\\nS3CR3T\\", '
                '\\"error_code\\": \\"CARD_EXPIRED\\"}"}'
            ),
            ("CARD_EXPIRED", "[REDACTED]"),
            ("S3CR3T", "SampleMarker"),
        ),
        (
            "two_block_continuation_preserves_error_code",
            (
                '{"secret":\n"password\': S3CR3T\n'
                '{"secret":\n"password\': S3CR3T\nCARD_EXPIRED\nend"'
            ),
            ("CARD_EXPIRED",),
            ("S3CR3T",),
        ),
        (
            "escaped_delegation_guard_quoted_child",
            (
                r'{"log": "{\"secret\":\n\"password\': '
                r'\"S3CR3T_1\" S3CR3T_2\", {\"other\": 1}"}'
            ),
            ("other", "[REDACTED]"),
            ("S3CR3T_1", "S3CR3T_2"),
        ),
        (
            "escaped_delegation_guard_multiline_child",
            (
                '{"log": "{\\"secret\\":\n\\"password\': '
                'S3CR3T_1\nS3CR3T_2\\", {\\"other\\": 1}"}'
            ),
            ("other", "[REDACTED]"),
            ("S3CR3T_1", "S3CR3T_2"),
        ),
        (
            "escaped_embedded_multiline_close_brace",
            (
                r'{"log": "{\"password\": \"alpha\" '
                r"{\"re\": \"line1\n}\", \"k\": \"v\"} "
                r'S3CR3T\", \"other\": 1}"}'
            ),
            ("other", "[REDACTED]"),
            ("S3CR3T",),
        ),
    )
    for label, payload, required, forbidden in marker_cases:
      with self.subTest(label=label):
        out = scrub_sensitive_text(payload)
        for token in required:
          self.assertIn(token, out)
        for token in forbidden:
          self.assertNotIn(token, out)

  def test_scrub_linear_scaling_escaped_quotes_and_continuations(self):
    from analytics.trajectory_extractor import scrub_sensitive_text  # noqa: PLC0415

    cases = (
        (
            "esq_in_brace_bomb_32k",
            (
                r'{"log": "{\"password\": \"alpha\" '
                + (r"{\'k\': \'x " * 1070)
                + r'\"}"}'
            ),
            ("[REDACTED]",),
            ("alpha",),
        ),
        (
            "brace_open_then_esc_q_32k",
            (
                '{"secret": "alpha" '
                + ('} {"k": 1} \\" ' * 2285)
                + ' "end", "other": 1}'
            ),
            ('"other": 1',),
            ("alpha",),
        ),
        (
            "brace_open_esc_q_pairs_32k",
            (
                '{"secret": "alpha" '
                + ('} {"k": 1} \\" } {"k": 1} ' * 1450)
                + ' "end", "other": 1}'
            ),
            ('"other": 1',),
            ("alpha",),
        ),
        (
            "brace_open_esc_q_escaped_mode_32k",
            (
                '{"log": "'
                + r"{\"secret\": \"alpha\" "
                + (r"} {\"k\": 1} \\\" " * 2000)
                + r' \"end\", \"other\": 1}"}'
            ),
            ("other",),
            ("alpha",),
        ),
        (
            "cont_plain_card_expired_end_32k",
            (
                ('{"secret":\n"password\': S3CR3T\n' * 1100)
                + 'CARD_EXPIRED\nend"'
            ),
            ("CARD_EXPIRED",),
            ("S3CR3T",),
        ),
        (
            "cont_plain_distant_hdr_32k",
            ('{"secret":\n"password\': S3CR3T\n' * 1100) + "x\npassword: z",
            (),
            ("S3CR3T",),
        ),
        (
            "repeated_auth_oauth2_creds_nl_4k",
            "auth: oauth2:\n  credentials: ok\n" * 4000,
            ("credentials: [REDACTED]",),
            (),
        ),
        (
            "repeated_auth_v1_v2_space_colon_4k",
            "auth: v1: v2: ok\n" * 4000,
            ("auth: v1: v2: [REDACTED]",),
            (),
        ),
    )
    for label, payload, required, forbidden in cases:
      with self.subTest(label=label):
        out, elapsed = _timed_scrub_median_sec(scrub_sensitive_text, payload)
        self.assertLess(elapsed, 5.0, f"{label} took {elapsed:.3f}s")
        for token in required:
          self.assertIn(token, out)
        for token in forbidden:
          self.assertNotIn(token, out)

  def test_scrub_deep_multiline_long_mismatched_and_pretty_continuations(self):
    from analytics.trajectory_extractor import _scrub_sensitive_kv_in_text  # noqa: PLC0415
    from analytics.trajectory_extractor import scrub_sensitive_text

    exact_cases = [
        (
            "delegated_child_kv_before_eof_nl",
            '"password":\n"password\': S3CR3T_1\n\nERROR: CARD_EXPIRED"\n',
            '"password":\n"password\': [REDACTED]\n\nERROR: CARD_EXPIRED"\n',
        ),
        (
            "delegated_child_kv_before_nl_brace",
            '{"password":\n"password\': S3CR3T_1\n\nERROR: CARD_EXPIRED"\n}',
            '{"password":\n"password\': [REDACTED]\n\nERROR: CARD_EXPIRED"\n}',
        ),
        (
            "delegated_child_kv_before_comma_obj",
            (
                '{"password":\n"password\': S3CR3T_1\n\n'
                'ERROR: CARD_EXPIRED", {"other": 1}'
            ),
            (
                '{"password":\n"password\': [REDACTED]\n\n'
                'ERROR: CARD_EXPIRED", {"other": 1}'
            ),
        ),
        (
            "delegated_prefixed_child_kv_before_comma_obj",
            (
                '{"password":\n"SampleMarker_password\': S3CR3T_1\n\n'
                'ERROR: CARD_EXPIRED", {"other": 1}'
            ),
            (
                '{"password":\n"SampleMarker_password\': [REDACTED]\n\n'
                'ERROR: CARD_EXPIRED", {"other": 1}'
            ),
        ),
        (
            "pretty_nl_before_brace_multiline_auth",
            (
                '{\n  "password":\n"SampleMarker_password\': one\n'
                'auth: hello\nS3CR3T"\n}'
            ),
            '{\n  "password":\n"[REDACTED]"\n}',
        ),
        (
            "pretty_nl_before_brace_inline_auth",
            (
                '{\n  "password":\n"SampleMarker_password\': one\n'
                'auth: hello S3CR3T"\n}'
            ),
            '{\n  "password":\n"[REDACTED]"\n}',
        ),
        (
            "pretty_nl_before_comma_multiline_auth",
            (
                '{\n  "password":\n"SampleMarker_password\': one\n'
                'auth: hello\nS3CR3T"\n  ,\n  "other": "keep"\n}'
            ),
            '{\n  "password":\n"[REDACTED]"\n  ,\n  "other": "keep"\n}',
        ),
        (
            "multiline_close_open_brace_boundary",
            (
                '{"password": "alpha" {"re": "line1\n}\n{ keep"} '
                ', "other": "keep"}'
            ),
            (
                '{"password": "[REDACTED]" {"re": "line1\n}\n{ keep"} '
                ', "other": "keep"}'
            ),
        ),
        (
            "long_container_token_non_sensitive",
            ("org_" * 36) + "auth: status=ok",
            ("org_" * 36) + "auth: status=ok",
        ),
    ]
    for label, payload, expected in exact_cases:
      with self.subTest(case=label):
        self.assertEqual(scrub_sensitive_text(payload), expected)

    self.assertEqual(
        _scrub_sensitive_kv_in_text(
            '{"note": "line1\npassword: S3CR3T_DIRECT\nline3"}',
            _depth=3,
        ),
        '{"note": "line1\npassword: [REDACTED]\nline3"}',
    )

    marker_cases = [
        (
            "long_container_token_sensitive_kv",
            ("org_" * 36) + "auth: password=S3CR3T",
            ("[REDACTED]", ("org_" * 36) + "auth:"),
            ("S3CR3T",),
        ),
        (
            "deep_3layer_json_password_colon",
            (
                r'{"a": "{\"b\": \"{\\\"c\\\": \\\"{\\\\\\\"note\\\\\\\": '
                r'\\\\\\\"line1\\\\\\\\npassword: S3CR3T\\\\\\\"}\\\"}\"}"}'
            ),
            ("[REDACTED]",),
            ("S3CR3T",),
        ),
        (
            "deep_3layer_json_token_eq",
            (
                r'{"a": "{\"b\": \"{\\\"c\\\": \\\"{\\\\\\\"note\\\\\\\": '
                r'\\\\\\\"line1\\\\\\\\ntoken=S3CR3T\\\\\\\"}\\\"}\"}"}'
            ),
            ("[REDACTED]",),
            ("S3CR3T",),
        ),
        (
            "deep_3layer_json_prefixed_password_eq",
            (
                r'{"a": "{\"b\": \"{\\\"c\\\": \\\"{\\\\\\\"note\\\\\\\": '
                r'\\\\\\\"line1\\\\\\\\nfoo password=S3CR3T\\\\\\\"}\\\"}\"}"}'
            ),
            ("[REDACTED]",),
            ("S3CR3T",),
        ),
        (
            "deep_3layer_json_outer_wrapper",
            (
                r'{"outer": {"a": "{\"b\": \"{\\\"c\\\": '
                r"\\\"{\\\\\\\"note\\\\\\\": "
                r'\\\\\\\"line1\\\\\\\\npassword: S3CR3T\\\\\\\"}\\\"}\"}"}}'
            ),
            ("[REDACTED]",),
            ("S3CR3T",),
        ),
        (
            "deep_3layer_escaped_single_quote",
            (
                r'step1="step2=\"step3=\\\\\"{'
                r"'note': 'line1\\npassword: S3CR3T_SQ3X\\nline3'"
                r'}\\\\\"\""'
            ),
            ("[REDACTED]", "line3"),
            ("S3CR3T_SQ3X",),
        ),
        (
            "deep_3layer_truncated_prefix",
            (
                r'step1="step2=\"step3=\\\\\"{\\\\\"trunc\\\\\": , '
                r"\\\\\"note\\\\\": \\\\\"line1\\n"
                r'password: S3CR3T_TRUNC3\\nline3\\\\\"}\\\\\"\""'
            ),
            ("[REDACTED]", "line3"),
            ("S3CR3T_TRUNC3",),
        ),
        (
            "deep_3layer_branch_b_escaped_json",
            (
                r'step1="step2=\"step3=\\\\\"prefix {\\\\\\"note\\\\\\": '
                r'\\\\\\"line1\\npassword: S3CR3T_BD3\\nline3\\\\\\"}\\\\\"\""'
            ),
            ("[REDACTED]", "line3"),
            ("S3CR3T_BD3",),
        ),
        (
            "deep_3layer_branch_b_inline_kv",
            (
                r'step1="step2=\"step3=\\\\\"prefix {\\\\\\"note\\\\\\": '
                r'\\\\\\"line1\\npassword=S3CR3T_BD3\\nline3\\\\\\"}\\\\\"\""'
            ),
            ("[REDACTED]", "line3"),
            ("S3CR3T_BD3",),
        ),
        (
            "long_mismatched_key_257_json_log",
            (
                r'{"log": "{\"note\": \"hello\"'
                + ("a" * 248)
                + r"password\': \"S3CR3T\"}"
                + r'"}'
            ),
            ("[REDACTED]",),
            ("S3CR3T",),
        ),
        (
            "long_mismatched_key_1025_json_log",
            (
                r'{"log": "{\"note\": \"hello\"'
                + ("a" * 1016)
                + r"password\': \"S3CR3T\"}"
                + r'"}'
            ),
            ("[REDACTED]",),
            ("S3CR3T",),
        ),
        (
            "long_mismatched_key_257_prefix_dot",
            r"\"prefix \"" + ("a" * 248) + ".password': S3CR3T_E",
            ("[REDACTED]",),
            ("S3CR3T_E",),
        ),
        (
            "long_mismatched_key_1025_prefix_dot",
            r"\"prefix \"" + ("a" * 1016) + ".password': S3CR3T_E",
            ("[REDACTED]",),
            ("S3CR3T_E",),
        ),
        (
            "mismatched_key_delim_eq_json_log",
            r'{"log": "{\"note\": \"hello\"a=b password\': \"S3CR3T\"}"}',
            ("[REDACTED]",),
            ("S3CR3T",),
        ),
        (
            "mismatched_key_delim_brace_json_log",
            r'{"log": "{\"note\": \"hello\"a{b password\': \"S3CR3T\"}"}',
            ("[REDACTED]",),
            ("S3CR3T",),
        ),
        (
            "mismatched_key_delim_bracket_comma_json_log",
            r'{"log": "{\"note\": \"hello\"a[b,c password\': \"S3CR3T\"}"}',
            ("[REDACTED]",),
            ("S3CR3T",),
        ),
        (
            "mismatched_key_delim_eq_prefix_dot",
            r"\"prefix \"" + "foo=bar.password': S3CR3T_E",
            ("[REDACTED]",),
            ("S3CR3T_E",),
        ),
        (
            "mismatched_key_delim_brace_prefix_dot",
            r"\"prefix \"" + "foo{bar}.password': S3CR3T_E",
            ("[REDACTED]",),
            ("S3CR3T_E",),
        ),
        (
            "mismatched_key_delim_bracket_comma_prefix_dot",
            r"\"prefix \"" + "foo[bar],baz.password': S3CR3T_E",
            ("[REDACTED]",),
            ("S3CR3T_E",),
        ),
        (
            "pretty_escaped_nl_before_brace",
            (
                '{"log": "{\n  \\"password\\":\n'
                "\\\"SampleMarker_password': one\nauth: hello\n"
                'S3CR3T\\"\n}"}'
            ),
            ("[REDACTED]",),
            ("S3CR3T", "SampleMarker"),
        ),
        (
            "pretty_double_escaped_nl_before_brace",
            (
                '{"log": "{\\n  \\"password\\":\\n'
                "\\\"SampleMarker_password': one\\nauth: hello\\n"
                'S3CR3T\\"\\n}"}'
            ),
            ("[REDACTED]",),
            ("S3CR3T", "SampleMarker"),
        ),
        (
            "pretty_escaped_nl_before_comma",
            (
                '{"log": "{\n  \\"password\\":\n'
                "\\\"SampleMarker_password': one\nauth: hello\n"
                'S3CR3T\\"\n  ,\n  \\"other\\": \\"keep\\"\n}"}'
            ),
            ("[REDACTED]", "keep"),
            ("S3CR3T", "SampleMarker"),
        ),
        (
            "pretty_double_escaped_nl_before_comma",
            (
                '{"log": "{\\n  \\"password\\":\\n'
                "\\\"SampleMarker_password': one\\nauth: hello\\n"
                'S3CR3T\\"\\n  ,\\n  \\"other\\": \\"keep\\"\\n}"}'
            ),
            ("[REDACTED]", "keep"),
            ("S3CR3T", "SampleMarker"),
        ),
    ]
    for label, payload, required, forbidden in marker_cases:
      with self.subTest(case=label):
        out = scrub_sensitive_text(payload)
        for token in required:
          self.assertIn(token, out)
        for token in forbidden:
          self.assertNotIn(token, out)

  def test_scrub_linear_scaling_long_mismatched_and_pretty_bombs(self):
    from analytics.trajectory_extractor import scrub_sensitive_text  # noqa: PLC0415

    bomb_cases = [
        (
            "esq_closed_brace_bomb_32k",
            (
                r'{"log": "{\"password\": \"alpha\" '
                + (r"{\'k\': \'x\'} " * 2000)
                + r'}"}'
            ),
            ("[REDACTED]",),
            ("alpha",),
        ),
        (
            "edq_mismatch_256_bomb_32k",
            (
                r'{"log": "'
                + (r"{\"note\": \"" + ("a" * 260) + r"\': \"x\"} ") * 110
                + r'"}'
            ),
            ("note",),
            (),
        ),
        (
            "edq_mismatch_long_delim_bomb_32k",
            (
                r'{"log": "'
                + (r"{\"a=b,c{d}e[f];" + ("z" * 260) + r"\'y\" ") * 110
                + r'"}'
            ),
            ("a=b,c{d}e[f];",),
            (),
        ),
        (
            "esq_trailing_apostrophe_bomb_32k",
            (
                r'{"log": "{\"password\": \"alpha\" '
                + (r"\"a\' " * 200)
                + (r"{\'k\': \'x " * 2000)
                + r'\"}"}'
            ),
            ("[REDACTED]",),
            ("alpha",),
        ),
        (
            "pretty_nl_seen_hdr_bomb_32k",
            ('{"secret":\n"password\': S3CR3T\nauth: hello\n' * 800) + '"\n}',
            ("[REDACTED]",),
            ("S3CR3T",),
        ),
        (
            "pretty_edq_seen_hdr_bomb_32k",
            (
                '{"log": "{\n  \\"password\\":\n'
                + ("\\\"password': S3CR3T\nauth: hello\n" * 900)
                + '\\"\n}"}'
            ),
            ("[REDACTED]",),
            ("S3CR3T",),
        ),
        (
            "comma_fallback_k4000_plain_a",
            '{"secret":\n"password\': "x", '
            + ('{"a": 1}, "y", ' * 4000)
            + 'end"}',
            ("[REDACTED]",),
            ("x", "end"),
        ),
        (
            "comma_fallback_k4000_plain_b",
            '{"secret":\n"password\': "x", {"a": 1}, '
            + ('"y", {"a": 1}, ' * 4000)
            + 'end"',
            ("[REDACTED]",),
            ("x",),
        ),
        (
            "comma_fallback_k4000_escaped",
            '{"secret":\n\\"password\': "x", '
            + ('{"a": 1}, \\"y\\", ' * 4000)
            + 'end\\"}',
            ("[REDACTED]", "end"),
            ("x",),
        ),
    ]
    for label, payload, required, forbidden in bomb_cases:
      with self.subTest(case=label):
        out, elapsed = _timed_scrub_median_sec(scrub_sensitive_text, payload)
        self.assertLess(
            elapsed,
            5.0,
            f"{label} took {elapsed:.3f}s (expected < 5.0s)",
        )
        for token in required:
          self.assertIn(token, out)
        for token in forbidden:
          self.assertNotIn(token, out)

  def test_scrub_comma_fallback_escaped_nl_lb3_and_bracket_boundary(self):
    from analytics.trajectory_extractor import _is_enclosing_json_string_literal  # noqa: PLC0415
    from analytics.trajectory_extractor import scrub_sensitive_text

    exact_cases = [
        (
            "comma_err_marker_preserved",
            '{"secret":\n"password\': "x", {"a": 1}, "y", {"a": 1}\nERROR: CARD_EXPIRED", "other": 1}',
            '{"secret":\n"[REDACTED]", {"a": 1}\nERROR: CARD_EXPIRED", "other": 1}',
        ),
        (
            "escaped_nl_sibling_preserved",
            'prefix {"outer": "{\\"password\\":\n\\"SampleMarker_password\': one\nauth: \\"ok\\"\\n\\"other\\": \\"val\\", \\"z\\": 1\\n}"',
            'prefix {"outer": "{\\"password\\":\n\\"SampleMarker_password\': [REDACTED]\nauth: \\"[REDACTED]\\"\\n\\"other\\": \\"val\\", \\"z\\": 1\\n}"',
        ),
        (
            "escaped_nl_card_expired_preserved",
            'password: \\"SampleMarker_password\': S3CR3T_1\nERROR: CARD_EXPIRED\nauth: \\"ok\\"\\n\\"other\\": \\"val\\"\\n}',
            'password: \\"SampleMarker_password\': [REDACTED]\nERROR: CARD_EXPIRED\nauth: \\"[REDACTED]\\"\\n\\"other\\": \\"val\\"\\n}',
        ),
        (
            "lb3_nl2_comma_member_sep",
            '{"outer": "{\\"log\\": \\"{\\\\\\"password\\\\\\":\n\\\\\\"SampleMarker_password\': one\nauth: hello\nS3CR3T\\\\\\"\\\\n  ,  \\\\n  \\\\\\"other\\\\\\": \\\\\\"keep\\\\\\"}\\"}"}',
            '{"outer": "{\\"log\\": \\"{\\\\\\"password\\\\\\":\n\\\\\\"[REDACTED]\\\\\\"\\\\n  ,  \\\\n  \\\\\\"other\\\\\\": \\\\\\"keep\\\\\\"}\\"}"}',
        ),
        (
            "lb3_nl2_rbrace_sep",
            '{\\\\\\"password\\\\\\":\n\\\\\\"SampleMarker_password\': one\nauth: hello\nS3CR3T\\\\\\"}\\\\n , \\\\\\"other\\\\\\": 1}',
            '{\\\\\\"password\\\\\\":\n\\\\\\"[REDACTED]\\\\\\"}\\\\n , \\\\\\"other\\\\\\": 1}',
        ),
        (
            "multiline_rbrace_bracket_boundary_preserved",
            '{"password": "alpha" {"re": "line1\n}\n[1]"} , "other": "keep"}',
            '{"password": "[REDACTED]" {"re": "line1\n}\n[1]"} , "other": "keep"}',
        ),
    ]
    for label, payload, expected in exact_cases:
      with self.subTest(case=label):
        self.assertEqual(scrub_sensitive_text(payload), expected)

    direct_escaped = r'{"a": 1, \"password\': S3CR3T_LEAK\nauth: \"ok\"\n\"other\": \"val\"\n}'
    self.assertFalse(
        _is_enclosing_json_string_literal(
            direct_escaped,
            direct_escaped.index("S3CR3T_LEAK"),
            is_escaped_dq=True,
            lead_bs=1,
            quote_char='"',
            allow_same_line=True,
        )
    )

  def test_scrubber_b64_chains_urls_semicolon_mime_cookies_json_depth_counts_and_tails(
      self,
  ) -> None:
    import concurrent.futures  # noqa: PLC0415

    from analytics.trajectory_extractor import _ALL_SAFE_COUNT_KEYS  # noqa: PLC0415
    from analytics.trajectory_extractor import _LLM_METRIC_COUNT_KEYS  # noqa: PLC0415
    from analytics.trajectory_extractor import _NESTED_JSON_BUDGET  # noqa: PLC0415
    from analytics.trajectory_extractor import _SAFE_SMALL_COUNT_KEYS  # noqa: PLC0415
    from analytics.trajectory_extractor import is_sensitive_key  # noqa: PLC0415
    from analytics.trajectory_extractor import scrub_sensitive_data  # noqa: PLC0415
    from analytics.trajectory_extractor import scrub_sensitive_text  # noqa: PLC0415

    # 1. Base64-padded multi-row scaling without RecursionError.
    b64_secret = "c2VjcmV0X3Rva2VuX3ZhbHVlX3BhZGRlZA=="
    for row_count in (16, 64, 128, 256, 512, 1000):
      with self.subTest(b64_rows=row_count):
        payload = "\n".join(
            f"merchant_{i:03d}_api_key: {b64_secret}" for i in range(row_count)
        )
        out = scrub_sensitive_text(payload)
        self.assertNotIn(b64_secret, out)
        self.assertEqual(out.count("[REDACTED]"), row_count)

    payload_160 = "\n".join(
        f"merchant_{i:03d}_api_key: {b64_secret}" for i in range(160)
    )
    payload_640 = "\n".join(
        f"merchant_{i:03d}_api_key: {b64_secret}" for i in range(640)
    )
    out_160, dt_160 = _timed_scrub_median_sec(scrub_sensitive_text, payload_160)
    out_640, dt_640 = _timed_scrub_median_sec(scrub_sensitive_text, payload_640)
    self.assertEqual(out_160.count("[REDACTED]"), 160)
    self.assertEqual(out_640.count("[REDACTED]"), 640)
    self.assertLess(dt_640, 5.0)
    self.assertLess(dt_640 / max(dt_160, 1e-4), 8.0)

    # 2. Scalar credential '=' values vs container chains.
    for raw, expected in (
        ("password: DemoCanary=17", "password: [REDACTED]"),
        ("X-Api-Key: DemoCanary=17", "X-Api-Key: [REDACTED]"),
        ("api_key: token_pref=17", "api_key: [REDACTED]"),
        ("password:foo:bar=S3CR3T", "password:foo:bar=[REDACTED]"),
        ("auth: MyVal1=SampleMarker", "auth: MyVal1=[REDACTED]"),
    ):
      with self.subTest(scalar_eq=raw):
        self.assertEqual(scrub_sensitive_text(raw), expected)

    # 3. Single-label URL hosts vs userinfo secrets.
    for raw, expected in (
        (
            "url=postgresql://svc:fixture@db,password=before@DemoCanary,"
            "status=CARD_EXPIRED",
            "url=postgresql://svc:[REDACTED]@db,password=[REDACTED],"
            "status=CARD_EXPIRED",
        ),
        (
            "url=redis://:tok@cache;secret=abc@xyz,status=CARD_EXPIRED",
            "url=redis://:[REDACTED]@cache;secret=[REDACTED],"
            "status=CARD_EXPIRED",
        ),
    ):
      with self.subTest(single_label_url=raw):
        self.assertEqual(scrub_sensitive_text(raw), expected)

    # 4. Compound container parent + tight colon leaf.
    for raw, expected in (
        (
            'secret_config:api_key:"SampleMarker"',
            'secret_config:api_key:"[REDACTED]"',
        ),
        (
            'secret_store:api_key:"SampleMarker"',
            'secret_store:api_key:"[REDACTED]"',
        ),
        (
            "app_credentials:client_secret:'SampleMarker'",
            "app_credentials:client_secret:'[REDACTED]'",
        ),
    ):
      with self.subTest(compound_container=raw):
        self.assertEqual(scrub_sensitive_text(raw), expected)

    # 5. JSON sibling keyed with ';' (e.g. MIME content types).
    mime_cases = (
        (
            "valid_json",
            '{"password": "S3CR3T", '
            '"application/json; charset=utf-8": "ok", '
            '"error": "CARD_EXPIRED"}',
            ("S3CR3T",),
        ),
        (
            "inner_unbalanced_quote",
            '{"password": "alpha"beta", '
            '"application/json; charset=utf-8": "ok", '
            '"error": "CARD_EXPIRED"}',
            ("alpha", "beta"),
        ),
        (
            "mismatched_newline",
            '{"password":\n"alpha\', '
            '"application/json; charset=utf-8": "ok", '
            '"error": "CARD_EXPIRED"}',
            ("alpha",),
        ),
        (
            "small_obj_inner_quote",
            '{"password": "al"pha", '
            '"application/json; charset=utf-8": "ok", '
            '"error": "CARD_EXPIRED"}',
            ("pha",),
        ),
        (
            "post_budget_json",
            ('{"k":1} ' * 33) + '{"password": "S3CR3T", '
            '"application/json; charset=utf-8": "ok", '
            '"error": "CARD_EXPIRED"}',
            ("S3CR3T",),
        ),
    )
    for label, payload, forbidden in mime_cases:
      with self.subTest(semicolon_mime=label):
        out = scrub_sensitive_text(payload)
        self.assertIn("[REDACTED]", out)
        self.assertIn("application/json; charset=utf-8", out)
        self.assertIn("CARD_EXPIRED", out)
        for token in forbidden:
          self.assertNotIn(token, out)

    # 6. HTTP method + /auth routes vs /v1/password/ and /oauth/token/.
    for unchanged in (
        "GET /v1/auth: 401",
        "GET /auth: 401",
        "POST /api/v1/auth: 403",
        "GET /v1/auth/verify: CARD_EXPIRED",
    ):
      with self.subTest(http_auth_route=unchanged):
        self.assertEqual(scrub_sensitive_text(unchanged), unchanged)

    for raw, expected in (
        ("GET /v1/password/: S3CR3T", "GET /v1/password/: [REDACTED]"),
        ("GET /oauth/token/: S3CR3T", "GET /oauth/token/: [REDACTED]"),
    ):
      with self.subTest(http_secret_route=raw):
        self.assertEqual(scrub_sensitive_text(raw), expected)

    # 7. Deep nested JSON, budget fail-closed, and thread-safe ContextVar state.
    def _wrap_json_layers(inner: dict, layers: int) -> str:
      val = json.dumps(inner)
      for idx in range(layers - 1):
        val = json.dumps({f"wrap_{idx}": val})
      return json.dumps({"payload": val})

    nested_5 = _wrap_json_layers({"password": "S3CR3T_5"}, 5)
    out_5 = scrub_sensitive_text(nested_5)
    self.assertNotIn("S3CR3T_5", out_5)
    self.assertIn("[REDACTED]", out_5)
    self.assertIsInstance(json.loads(out_5), dict)

    prefix_32 = (
        " ".join(json.dumps({f"k{i}": f"v{i}"}) for i in range(32)) + " "
    )
    nested_4 = _wrap_json_layers({"password": "S3CR3T_4"}, 4)
    post_budget_4 = prefix_32 + nested_4
    out_post_4 = scrub_sensitive_text(post_budget_4)
    self.assertNotIn("S3CR3T_4", out_post_4)
    self.assertIn("[REDACTED]", out_post_4)
    self.assertTrue(out_post_4.startswith(prefix_32))
    self.assertIsInstance(json.loads(out_post_4[len(prefix_32) :]), dict)

    nested_12 = _wrap_json_layers({"password": "S3CR3T_12"}, 12)
    out_12 = scrub_sensitive_text(nested_12)
    self.assertNotIn("S3CR3T_12", out_12)
    self.assertIn("[REDACTED]", out_12)
    self.assertIsInstance(json.loads(out_12), dict)

    mixed_payloads = [nested_5, post_budget_4, nested_12] * 8
    sequential_outputs = [scrub_sensitive_text(p) for p in mixed_payloads]

    def _scrub_and_read_budget(text: str) -> tuple[str, object]:
      result = scrub_sensitive_text(text)
      return result, _NESTED_JSON_BUDGET.get()

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
      concurrent_results = list(
          pool.map(_scrub_and_read_budget, mixed_payloads)
      )
    self.assertEqual([r[0] for r in concurrent_results], sequential_outputs)
    self.assertTrue(all(r[1] is None for r in concurrent_results))
    self.assertIsNone(_NESTED_JSON_BUDGET.get())

    # 8. Request Cookie vs Set-Cookie, cookie-octet ')'/'\'', and open quotes.
    cookie_cases = (
        ("Cookie: sid=a; CARD_EXPIRED", "Cookie: [REDACTED]; CARD_EXPIRED"),
        (
            'Cookie: sid=a; {"error":"CARD_EXPIRED"}',
            'Cookie: [REDACTED]; {"error":"CARD_EXPIRED"}',
        ),
        (
            "Cookie: sid=a, status=CARD_EXPIRED",
            "Cookie: [REDACTED], status=CARD_EXPIRED",
        ),
        (
            "Set-Cookie: a=1; Path=/; HttpOnly; Secure; Partitioned",
            "Set-Cookie: [REDACTED]",
        ),
        ("Cookie: sid=aa)samplevalue; name=nextvalue", "Cookie: [REDACTED]"),
        ("Cookie: sid=aa'samplevalue; name=nextvalue", "Cookie: [REDACTED]"),
        ("Cookie: sid=aa)", "Cookie: [REDACTED]"),
        (
            "(Cookie: sid=a) CARD_EXPIRED",
            "(Cookie: [REDACTED]) CARD_EXPIRED",
        ),
        ('Cookie: sid="first samplevalue', "Cookie: [REDACTED]"),
        (
            'Cookie: sid="first samplevalue\nstatus=ok',
            "Cookie: [REDACTED]\nstatus=ok",
        ),
        (
            'Cookie: sid="first samplevalue CARD_EXPIRED',
            "Cookie: [REDACTED]",
        ),
        (
            'Cookie: sid="first samplevalue\nERROR: CARD_EXPIRED',
            "Cookie: [REDACTED]\nERROR: CARD_EXPIRED",
        ),
    )
    for raw, expected in cookie_cases:
      with self.subTest(cookie_case=raw):
        out = scrub_sensitive_text(raw)
        self.assertEqual(out, expected)
        self.assertNotIn("samplevalue", out)
        self.assertNotIn("nextvalue", out)

    # 9. token_count policy consistency across structured data and text.
    self.assertTrue(is_sensitive_key("token_count"))
    self.assertIn("token_count", _LLM_METRIC_COUNT_KEYS)
    self.assertNotIn("token_count", _SAFE_SMALL_COUNT_KEYS)
    self.assertTrue(all(is_sensitive_key(k) for k in _ALL_SAFE_COUNT_KEYS))

    self.assertEqual(
        scrub_sensitive_data({"token_count": 1500}), {"token_count": 1500}
    )
    for preserved in (
        "token_count=1500",
        "auth:v1:token_count: 5",
        "auth:v1.0:token_count=3",
    ):
      with self.subTest(token_count_preserved=preserved):
        self.assertEqual(scrub_sensitive_text(preserved), preserved)

    self.assertEqual(
        scrub_sensitive_data({"token_count": "samplevalue"}),
        {"token_count": "[REDACTED]"},
    )
    for raw, forbidden in (
        ('token_count="samplevalue"', "samplevalue"),
        ('{"token_count": "samplevalue"}', "samplevalue"),
        ("token_count=-1", "-1"),
        ('token_count="003"', "003"),
        ("auth:v1:token_count: S3CR3T", "S3CR3T"),
    ):
      with self.subTest(token_count_redacted=raw):
        out = scrub_sensitive_text(raw)
        self.assertNotIn(forbidden, out)
        self.assertIn("[REDACTED]", out)

    # 10. Tail-scanner bool/null JSON quoting, blank-line literals, >16KiB tails.
    tail_prefix = '{"k0": 0} ' * 33
    tail_bool_out = scrub_sensitive_text(
        tail_prefix + '{"password": true, "secret": false, "api_key": null}'
    )
    self.assertTrue(tail_bool_out.startswith(tail_prefix))
    self.assertEqual(
        json.loads(tail_bool_out[len(tail_prefix) :]),
        {
            "password": "[REDACTED]",
            "secret": "[REDACTED]",
            "api_key": "[REDACTED]",
        },
    )

    self.assertEqual(
        scrub_sensitive_text(
            "password:\n\ntrue\nsecret:\n\nfalse\napi_key:\n\nnull"
        ),
        "password:\n\n[REDACTED]\nsecret:\n\n[REDACTED]\napi_key:\n\n[REDACTED]",
    )

    tail_blank_out = scrub_sensitive_text(
        tail_prefix
        + '{"password":\n\ntrue, "secret":\n\nfalse, "api_key":\n\nnull}'
    )
    self.assertTrue(tail_blank_out.startswith(tail_prefix))
    self.assertEqual(
        json.loads(tail_blank_out[len(tail_prefix) :]),
        {
            "password": "[REDACTED]",
            "secret": "[REDACTED]",
            "api_key": "[REDACTED]",
        },
    )

    huge_val = "a" * 17000
    huge_out = scrub_sensitive_text(
        f'secret={{"k": "{huge_val}"}}\nERROR: CARD_EXPIRED'
    )
    self.assertNotIn(huge_val, huge_out)
    self.assertIn("[REDACTED]", huge_out)
    self.assertIn("ERROR: CARD_EXPIRED", huge_out)

  def test_scrubber_llm_telemetry_path_eq_tight_chains_amp_url_depth_and_oversized_spans(
      self,
  ):
    import unittest.mock  # noqa: PLC0415

    from analytics.trajectory_extractor import scrub_sensitive_data  # noqa: PLC0415
    from analytics.trajectory_extractor import scrub_sensitive_text
    import analytics.trajectory_extractor as scrubber_mod  # noqa: PLC0415

    scrub_data = scrub_sensitive_data
    orig_scrub_at_depth = scrubber_mod._scrub_text_at_depth

    # 1. LLM token telemetry vs standalone token_count.
    telemetry_fields = (
        "prompt_token_count",
        "candidates_token_count",
        "thoughts_token_count",
        "total_token_count",
        "input_token_count",
        "output_token_count",
        "cached_content_token_count",
        "token_counts",
    )
    telemetry_obj = {f: i + 10 for i, f in enumerate(telemetry_fields)}
    telemetry_obj["token_count"] = 1500
    telemetry_obj["token_count_s"] = "ok"
    telemetry_json = json.dumps(telemetry_obj)
    telemetry_kv = (
        " ".join(f"{f}={i + 10}" for i, f in enumerate(telemetry_fields))
        + ' token_count=1500 token_count_s="ok"'
    )
    telemetry_cases = [
        ("json", telemetry_json, telemetry_json),
        ("bare_kv", telemetry_kv, telemetry_kv),
        ("tail_32", "{} " * 32 + telemetry_json, "{} " * 32 + telemetry_json),
        (
            "standalone_kv_str",
            'token_count="samplevalue"',
            'token_count="[REDACTED]"',
        ),
        ("path_leaf_num", "usage.token_count=42", "usage.token_count=42"),
        (
            "path_leaf_secret",
            "usage.token_count: S3CR3T",
            "usage.token_count: [REDACTED]",
        ),
    ]
    for label, raw, expected in telemetry_cases:
      with self.subTest(telemetry=label):
        self.assertEqual(scrub_sensitive_text(raw), expected)

    safe_telemetry_dict = {
        "prompt_token_count": 412,
        "candidates_token_count": 100,
        "token_counts": 2,
        "token_count": 1500,
        "token_count_bad": "x",
    }
    data_cases = [
        ("safe_dict", safe_telemetry_dict, safe_telemetry_dict),
        (
            "secret_str",
            {"token_count": "samplevalue"},
            {"token_count": "[REDACTED]"},
        ),
    ]
    for label, payload, expected in data_cases:
      with self.subTest(telemetry_data=label):
        self.assertEqual(scrub_data(payload), expected)

    # 2. Scalar '=' guard on tight ':', slashed/dotted paths, Bearer=...,
    #    and container/namespace '=' delegation.
    path_eq_cases = [
        ("password:DemoCanary=17", "password:[REDACTED]"),
        ("X-Api-Key:DemoCanary=17", "X-Api-Key:[REDACTED]"),
        ("v1/password: DemoCanary=17", "v1/password: [REDACTED]"),
        ("auth/v1/password: DemoCanary=17", "auth/v1/password: [REDACTED]"),
        ("auth/password: DemoCanary=17", "auth/password: [REDACTED]"),
        ("auth/api_key: DemoCanary=17", "auth/api_key: [REDACTED]"),
        ("auth.password: DemoCanary=17", "auth.password: [REDACTED]"),
        ("authorization: Bearer=DemoCanary", "authorization: [REDACTED]"),
        ("auth: password=S", "auth: password=[REDACTED]"),
        (
            'auth/v1:password="SampleMarker"',
            'auth/v1:password="[REDACTED]"',
        ),
        ("credentials: api_key=S", "credentials: api_key=[REDACTED]"),
        ("secret_config: password=S", "secret_config: password=[REDACTED]"),
        (
            "db_password:myService:password=S3CR3T",
            "db_password:myService:password=[REDACTED]",
        ),
    ]
    for raw, expected in path_eq_cases:
      with self.subTest(path_eq=raw):
        self.assertEqual(scrub_sensitive_text(raw), expected)

    # 3. Compound and multi-hop tight colon-leaf adoption parity.
    tight_chain_positives = [
        ("secret_config:api_key: SampleMarker", "secret_config:api_key:"),
        ('secret_store:api_key: r"SampleMarker"', "secret_store:api_key:"),
        ('secret_config:api_key:r"SampleMarker"', "secret_config:api_key:"),
        (
            'secret_config:sub:api_key:"SampleMarker"',
            "secret_config:sub:api_key:",
        ),
        ('user_secret:api_key:"SampleMarker"', "user_secret:api_key:"),
        ('api_token:password:"SampleMarker"', "api_token:password:"),
    ]
    for raw, prefix in tight_chain_positives:
      with self.subTest(tight_chain_pos=raw):
        out = scrub_sensitive_text(raw)
        self.assertTrue(out.startswith(prefix), out)
        self.assertNotIn("SampleMarker", out)
        self.assertIn("[REDACTED]", out)

    tight_chain_negatives = [
        ("password: hunter2: status: ok", "password: [REDACTED] status: ok"),
        ("auth:OPENSESAME/2:status=ok", "auth:[REDACTED]"),
        (
            'auth:OPENSESAME/2: "password": "S"',
            'auth:[REDACTED] "password": "[REDACTED]"',
        ),
        (
            "db_password: Env: CARD_EXPIRED",
            "db_password: [REDACTED] CARD_EXPIRED",
        ),
        ('password:my_secret:"x"', 'password:[REDACTED]"x"'),
    ]
    for raw, expected in tight_chain_negatives:
      with self.subTest(tight_chain_neg=raw):
        self.assertEqual(scrub_sensitive_text(raw), expected)

    # 4. Single-label URL host across '&' and existing '&' userinfo parity.
    amp_url_cases = [
        (
            "url=postgresql://svc:fixture@db"
            "&password=before@DemoCanary&status=CARD_EXPIRED",
            "url=postgresql://svc:[REDACTED]@db"
            "&password=[REDACTED]&status=CARD_EXPIRED",
        ),
        (
            "http://localhost:1234&SampleMarker@example.test",
            "http://localhost:[REDACTED]@example.test",
        ),
        (
            "https://token:SampleMarker&part=v@example.test/repo",
            "https://token:[REDACTED]@example.test/repo",
        ),
    ]
    for raw, expected in amp_url_cases:
      with self.subTest(amp_url=raw):
        self.assertEqual(scrub_sensitive_text(raw), expected)

    # 5. Linear O(d) nested JSON call count, flat \u/\" exemption,
    #    and depth-8/9 unescaped-inner probes.
    with unittest.mock.patch.object(
        scrubber_mod, "_scrub_text_at_depth", wraps=orig_scrub_at_depth
    ) as patched:
      for d in range(1, 11):
        payload_str = json.dumps(
            {"password": "S3CR3T_VAL", "status": "CARD_EXPIRED"}
        )
        plain_obj = {"password": "S3CR3T_VAL", "status": "CARD_EXPIRED"}
        for _ in range(d):
          payload_str = json.dumps({"k": payload_str})
          plain_obj = {"k": plain_obj}
        for kind, raw in (
            ("str", payload_str),
            ("dict", json.dumps(plain_obj)),
        ):
          with self.subTest(nested_json_depth=d, kind=kind):
            patched.reset_mock()
            out = scrub_sensitive_text(raw)
            self.assertLessEqual(patched.call_count, 3 * d)
            self.assertNotIn("S3CR3T_VAL", out)
            self.assertIn("[REDACTED]", out)
            self.assertIn("CARD_EXPIRED", out)

      branch_items = [
          json.dumps(
              {"password": f"S3CR3T_BR{i}"}
              if i % 2
              else {"api_key": f"S3CR3T_BR{i}"}
          )
          for i in range(1, 13)
      ]
      branch_raw = json.dumps(branch_items)
      self.assertLess(len(branch_raw), 16384)
      with self.subTest(branch_bomb="array_12"):
        patched.reset_mock()
        branch_out = scrub_sensitive_text(branch_raw)
        self.assertLessEqual(patched.call_count, 260)
        for i in range(1, 13):
          self.assertNotIn(f"S3CR3T_BR{i}", branch_out)
        self.assertIn("[REDACTED]", branch_out)

      wrapped = json.dumps(
          {
              "password": "S3CR3T_W1",
              "api_key": "S3CR3T_W2",
          }
      )
      for i in range(10):
        wrapped = json.dumps({f"w_{i}": wrapped})
      self.assertGreaterEqual(len(wrapped), 4075)
      self.assertLess(len(wrapped), 16384)
      with self.subTest(branch_bomb="wrapped_10"):
        patched.reset_mock()
        wrapped_out = scrub_sensitive_text(wrapped)
        self.assertLessEqual(patched.call_count, 260)
        self.assertNotIn("S3CR3T_W1", wrapped_out)
        self.assertNotIn("S3CR3T_W2", wrapped_out)
        self.assertIn("[REDACTED]", wrapped_out)

    benign_row = '{"ok": "a\\u0020b"}'
    benign_tail = (
        '{"ok": "a\\u0020b", "url": "a\\u0026b", '
        '"msg": "err: \\"CARD_EXPIRED\\""}'
    )
    rows_text = "\n".join([benign_row] * 260 + [benign_tail])
    rows_out = scrub_sensitive_text(rows_text)
    self.assertNotIn("[REDACTED]", rows_out)
    self.assertEqual(rows_out, rows_text)

    def _dumps_n(val, n):
      for _ in range(n):
        val = json.dumps(val)
      return val

    wrap_val = 'msg="password=DemoCanary123" ok="CARD_EXPIRED"'
    for _ in range(8):
      wrap_val = f"wrap={json.dumps(wrap_val)}"

    depth_probes = [
        ("n8", _dumps_n('{"note": "password=S3CR3T_N8"}', 8), "S3CR3T_N8", ""),
        ("dq8", _dumps_n('foo="password=S3CR3T_DQ8"', 8), "S3CR3T_DQ8", ""),
        ("sq8", _dumps_n("foo='password=S3CR3T_SQ8'", 8), "S3CR3T_SQ8", ""),
        ("b9", _dumps_n("password=S3CR3T_B9", 9), "S3CR3T_B9", ""),
        (
            "ps9",
            json.dumps({"payload": _dumps_n("foo='password=S3CR3T_PS9'", 9)}),
            "S3CR3T_PS9",
            "",
        ),
        ("wrap8", wrap_val, "DemoCanary123", "CARD_EXPIRED"),
    ]
    for label, raw, secret, keep in depth_probes:
      with self.subTest(depth_probe=label):
        out = scrub_sensitive_text(raw)
        self.assertNotIn(secret, out)
        self.assertIn("[REDACTED]", out)
        if keep:
          self.assertIn(keep, out)

    direct_d8 = orig_scrub_at_depth(
        '{"msg": "password=DemoCanary123"}', _depth=8
    )
    self.assertNotIn("DemoCanary123", direct_d8)
    self.assertIn("[REDACTED]", direct_d8)

    # 6. Parenthesized Cookie header and semicolon boundaries.
    cookie_cases = [
        (
            "(Cookie: sid=a); status=CARD_EXPIRED",
            "(Cookie: [REDACTED]); status=CARD_EXPIRED",
        ),
        (
            "(Cookie: sid=a) ; status=CARD_EXPIRED",
            "(Cookie: [REDACTED]) ; status=CARD_EXPIRED",
        ),
        (
            "Cookie: sid=aa)samplevalue; name=nextvalue",
            "Cookie: [REDACTED]",
        ),
    ]
    for raw, expected in cookie_cases:
      with self.subTest(cookie=raw):
        self.assertEqual(scrub_sensitive_text(raw), expected)

    # 7. Over-16-KiB r"..." and inline/JSON containers.
    huge = "x" * 17000
    oversized_cases = [
        (
            "raw_str",
            f'password=r"{huge}"\nERROR: CARD_EXPIRED',
            "password=[REDACTED]\nERROR: CARD_EXPIRED",
            False,
        ),
        (
            "inline_container",
            f'secret={{"a":"{huge}"}} CARD_EXPIRED',
            "secret=[REDACTED] CARD_EXPIRED",
            False,
        ),
        (
            "json_container",
            f'{{"secret": {{"a": "{huge}"}}, "status": "CARD_EXPIRED"}}',
            '{"secret": "[REDACTED]", "status": "CARD_EXPIRED"}',
            True,
        ),
    ]
    for label, raw, expected, parse_json in oversized_cases:
      with self.subTest(oversized=label):
        out = scrub_sensitive_text(raw)
        self.assertEqual(out, expected)
        if parse_json:
          self.assertEqual(
              json.loads(out),
              {"secret": "[REDACTED]", "status": "CARD_EXPIRED"},
          )

  def test_scrubber_json_keys_dups_deep_arrays_and_colon_leaves(self) -> None:
    import json  # noqa: PLC0415
    import time  # noqa: PLC0415

    from analytics.trajectory_extractor import _scrub_text_at_depth  # noqa: PLC0415
    from analytics.trajectory_extractor import scrub_sensitive_data
    from analytics.trajectory_extractor import scrub_sensitive_text

    # 1. Embedded JSON object keys and duplicate JSON object keys.
    ghp_tok = "ghp_" + "0123456789abcdef0123456789abcdef0123"
    oauth_tok = "ya29" + ".a0AfH6SMBx_123456"
    json_key_and_dup_cases = (
        ('{"url=https://user:S3CR3T@x.com": "ok"}', "S3CR3T", "ok"),
        (f'{{"{ghp_tok}": "ok"}}', "ghp_", "ok"),
        (f'{{"Bearer {oauth_tok}": "ok"}}', "ya29.", "ok"),
        ('[{"password=S3CR3T": "v"}]', "S3CR3T", "password=[REDACTED]"),
        ('{"nested": {"Bearer S3CR3T": "v"}}', "S3CR3T", "v"),
        ('{"a": "password=S3CR3T", "a": "ok"}', "S3CR3T", "ok"),
        (f'{{"a": "Bearer {oauth_tok}", "a": "ok"}}', "ya29.", "ok"),
        ('{"a": "Bearer S3CR3T_TOK", "a": "ok"}', "S3CR3T", "ok"),
        ('outer {"x": {"a": "password=S3CR3T", "a": "ok"}}', "S3CR3T", "ok"),
        (
            'log="{\\"a\\": \\"password=S3CR3T\\", \\"a\\": \\"ok\\"}"',
            "S3CR3T",
            "ok",
        ),
    )
    for raw, secret, keep in json_key_and_dup_cases:
      with self.subTest(json_case=raw):
        out = scrub_sensitive_text(raw)
        self.assertNotIn(secret, out)
        self.assertIn("[REDACTED]", out)
        self.assertIn(keep, out)

    with self.subTest(json_case="normal_headers_preserved"):
      self.assertEqual(
          scrub_sensitive_text(
              '{"content-type": "application/json; charset=utf-8",'
              ' "token": "SECRET"}'
          ),
          '{"content-type":"application/json; charset=utf-8",'
          '"token":"[REDACTED]"}',
      )

    # 2. Deep unescaped fallback across depths 8..40.
    for d in (8, 11, 12, 20, 32, 36, 40):
      with self.subTest(depth=d):
        self.assertEqual(
            _scrub_text_at_depth("foo='password=S3CR3T_DD'", _depth=d),
            "foo='password=[REDACTED]'",
        )
        deep_json = _scrub_text_at_depth(
            '{"m": "foo=\'password=S3CR3T_DD\'"}', _depth=d
        )
        self.assertNotIn("S3CR3T_DD", deep_json)
        self.assertIn("[REDACTED]", deep_json)
        self.assertEqual(
            _scrub_text_at_depth(
                'log="{\\"password\\": \\"S3CR3T_DD\\"}"', _depth=d
            ),
            'log="[REDACTED]"',
        )

    # 3. Large > 16 KiB 300-item JSON string arrays.
    large_arrays = (
        (
            "nested_json_strings",
            json.dumps(
                [
                    json.dumps(
                        {
                            "password": f"S3CR3T_{i}",
                            "status": "CARD_EXPIRED",
                            "pad": "x" * 20,
                        }
                    )
                    for i in range(300)
                ]
            ),
        ),
        (
            "escaped_log_strings",
            json.dumps(
                [
                    (
                        f'log="password=\\"S3CR3T_{i}\\"'
                        ' status=\\"CARD_EXPIRED\\" ' + ("x" * 15) + '"'
                    )
                    for i in range(300)
                ]
            ),
        ),
    )
    for label, payload in large_arrays:
      with self.subTest(large_array=label):
        self.assertGreater(len(payload), 16384)
        start = time.monotonic()
        out = scrub_sensitive_text(payload)
        elapsed = time.monotonic() - start
        self.assertEqual(out.count("CARD_EXPIRED"), 300)
        self.assertNotIn("S3CR3T_", out)
        self.assertLess(elapsed, 1.0)  # generous CI ceiling

    # 4. Scalar-credential colon shielding, multi-hop colon leaves,
    #    dotted trailing-host URL &, and non-sensitive token_count prefixes.
    exact_cases = (
        ("svc:password: DemoCanary=17", "svc:password: [REDACTED]"),
        ("prod:api_key: DemoCanary=17", "prod:api_key: [REDACTED]"),
        ("env:secret: DemoCanary=17", "env:secret: [REDACTED]"),
        ("app:secret_key: DemoCanary=17", "app:secret_key: [REDACTED]"),
        ("auth: MyVal1=SampleMarker", "auth: MyVal1=[REDACTED]"),
        ("credentials: api_key=S", "credentials: api_key=[REDACTED]"),
        (
            "secret_config: password=S",
            "secret_config: password=[REDACTED]",
        ),
        ("password:foo:bar=S3CR3T", "password:foo:bar=[REDACTED]"),
        (
            "db_password:myService:password=S3CR3T",
            "db_password:myService:password=[REDACTED]",
        ),
        ("secret:key_file=abc123", "secret:[REDACTED]"),
        ('secret:secret="SampleMarker"', 'secret:secret="[REDACTED]"'),
        (
            "secret_config:sub:api_key: SampleMarker",
            "secret_config:sub:api_key: [REDACTED]",
        ),
        (
            "auth_config:sub:api_key: SampleMarker",
            "auth_config:sub:api_key: [REDACTED]",
        ),
        (
            "user_secret:api_key: SampleMarker",
            "user_secret:api_key: [REDACTED]",
        ),
        (
            "api_token:password: SampleMarker",
            "api_token:password: [REDACTED]",
        ),
        (
            "password: hunter2: status: ok",
            "password: [REDACTED] status: ok",
        ),
        ('password:my_secret:"x"', 'password:[REDACTED]"x"'),
        ("auth:OPENSESAME/2:status=ok", "auth:[REDACTED]"),
        (
            'auth:OPENSESAME/2: "password": "S"',
            'auth:[REDACTED] "password": "[REDACTED]"',
        ),
        (
            "db_password: Env: CARD_EXPIRED",
            "db_password: [REDACTED] CARD_EXPIRED",
        ),
        (
            "url=postgresql://svc:fixture@db&password=before@DemoCanary.com",
            "url=postgresql://svc:[REDACTED]@db&password=[REDACTED]",
        ),
        (
            "url=postgresql://svc:fixture@db&password=before@DemoCanary.com"
            " trailing",
            "url=postgresql://svc:[REDACTED]@db&password=[REDACTED] trailing",
        ),
        (
            "https://user:p@ss&token=S3CR3T@example.test/repo",
            "https://user:[REDACTED]@example.test/repo",
        ),
        (
            "https://user:p@ss&email=x@example.test/repo",
            "https://user:[REDACTED]@example.test/repo",
        ),
        (
            "http://localhost:1234&SampleMarker@example.test",
            "http://localhost:[REDACTED]@example.test",
        ),
        ("step1.token_count=1500", "step1.token_count=1500"),
        ("agent_step.token_count=1500", "agent_step.token_count=1500"),
        ("trace.token_count=1500", "trace.token_count=1500"),
        ("stats.token_count=1500", "stats.token_count=1500"),
        ("foo.token_count=1500", "foo.token_count=1500"),
        ("usage.token_count=1500", "usage.token_count=1500"),
        ("auth:hunter2:token_count: 5", "auth:[REDACTED] 5"),
        ("usage.token_count: S3CR3T", "usage.token_count: [REDACTED]"),
    )
    for raw, expected in exact_cases:
      with self.subTest(exact=raw):
        self.assertEqual(scrub_sensitive_text(raw), expected)

    with self.subTest(structured="token_count"):
      self.assertEqual(
          scrub_sensitive_data(
              {"step1.token_count": 1500, "password.token_count": 1500}
          ),
          {"step1.token_count": 1500, "password.token_count": "[REDACTED]"},
      )

    # 5. Over-16-KiB plain and escaped quoted values.
    big = "a" * 17000
    for label, raw, expected in (
        (
            "json_plain",
            f'{{"secret": "{big}", "status": "CARD_EXPIRED"}}',
            '{"secret": "[REDACTED]", "status": "CARD_EXPIRED"}',
        ),
        (
            "plain_quoted",
            f'secret="{big}" CARD_EXPIRED',
            'secret="[REDACTED]" CARD_EXPIRED',
        ),
    ):
      with self.subTest(big_value=label):
        self.assertEqual(scrub_sensitive_text(raw), expected)

    with self.subTest(big_value="escaped_json"):
      escaped_big = scrub_sensitive_text(
          f'{{"log": "password=\\"{big}\\" status=CARD_EXPIRED"}}'
      )
      self.assertIn("CARD_EXPIRED", escaped_big)
      self.assertNotIn(big, escaped_big)
      self.assertIn("[REDACTED]", escaped_big)

  def test_scrubber_key_collisions_data_keys_unknown_colon_and_depth1_arrays(
      self,
  ) -> None:
    import time  # noqa: PLC0415

    from analytics.trajectory_extractor import scrub_sensitive_data  # noqa: PLC0415
    from analytics.trajectory_extractor import scrub_sensitive_text  # noqa: PLC0415

    # 1. Colliding redacted JSON object keys.
    for label, raw, keep, redact in (
        (
            "url_creds_dumps",
            json.dumps(
                {
                    "https://user:sec1@example.com": "CARD_EXPIRED",
                    "https://user:sec2@example.com": "ok",
                }
            ),
            ("CARD_EXPIRED", "ok"),
            ("sec1", "sec2"),
        ),
        (
            "url_creds_path",
            '{"https://u:sec1@example.com/a": "CARD_EXPIRED", '
            '"https://u:sec2@example.com/a": "ok"}',
            ("CARD_EXPIRED", "ok"),
            ("sec1", "sec2"),
        ),
        (
            "token_parens",
            '{"req (token=sec1) status": {"code": "CARD_EXPIRED"}, '
            '"req (token=sec2) status": {"code": "OK"}}',
            ("CARD_EXPIRED", "OK"),
            ("sec1", "sec2"),
        ),
        (
            "oauth_keys",
            json.dumps(
                {
                    "ya29" + ".S3CR3T_A": "DEADLINE_EXCEEDED",
                    "ya29" + ".S3CR3T_B": "CARD_EXPIRED",
                }
            ),
            ("DEADLINE_EXCEEDED", "CARD_EXPIRED"),
            ("S3CR3T_",),
        ),
        (
            "unicode_escape_key",
            '{"\\u0070assword=sec1": "CARD_EXPIRED", "password=sec2": "ok"}',
            (),
            ("sec1", "sec2"),
        ),
        (
            "nested_outer_key",
            '{"outer": {"https://u:sec1@x.com": "CARD_EXPIRED", '
            '"https://u:sec2@x.com": "ok"}, "password": "TopSecret99"}',
            ("CARD_EXPIRED",),
            ("sec1", "sec2", "TopSecret99"),
        ),
    ):
      with self.subTest(colliding_json_key=label):
        out = scrub_sensitive_text(raw)
        for token in keep:
          self.assertIn(token, out)
        for secret in redact:
          self.assertNotIn(secret, out)

    # 2. Secret-bearing Mapping keys and collision disambiguation.
    benign_mapping = {
        "content-type": "application/json; charset=utf-8",
        "Bearer": "ok",
    }
    for label, payload, expected in (
        (
            "url_key",
            {"https://u:Hunter22@db.internal": "ok"},
            {"https://u:[REDACTED]@db.internal": "ok"},
        ),
        (
            "ghp_key",
            {"ghp_" + "0123456789abcdef0123456789abcdef0123": "ok"},
            {"[REDACTED]": "ok"},
        ),
        ("benign_keys", benign_mapping, benign_mapping),
    ):
      with self.subTest(data_key=label):
        self.assertEqual(scrub_sensitive_data(payload), expected)

    with self.subTest(data_key="colliding_mapping_keys"):
      out_map = scrub_sensitive_data(
          {
              "https://u:sec1@x.com": "CARD_EXPIRED",
              "https://u:sec2@x.com": "ok",
          }
      )
      self.assertEqual(len(out_map), 2)
      self.assertEqual(set(out_map.values()), {"CARD_EXPIRED", "ok"})
      dumped = json.dumps(out_map)
      self.assertNotIn("sec1", dumped)
      self.assertNotIn("sec2", dumped)

    # 3. Unknown-namespace multi-hop unquoted colon chains.
    for raw, expected in (
        (
            "auth.password:custom_ns:api_key: SampleMarker",
            "auth.password:custom_ns:api_key: [REDACTED]",
        ),
        (
            "auth.password:custom_ns:token: SampleMarker",
            "auth.password:custom_ns:token: [REDACTED]",
        ),
        (
            "api_secret:custom_ns:token: SampleMarker",
            "api_secret:custom_ns:token: [REDACTED]",
        ),
        (
            "password:custom_ns:api_key: SampleMarker",
            "password:custom_ns:api_key: [REDACTED]",
        ),
        (
            "secret:custom_ns:password: SampleMarker status=CARD_EXPIRED",
            "secret:custom_ns:password: [REDACTED] status=CARD_EXPIRED",
        ),
        (
            "api_secret:v1:token: SampleMarker",
            "api_secret:v1:token: [REDACTED]",
        ),
        ("token:abc:def:api_key: x1", "token:[REDACTED] x1"),
    ):
      with self.subTest(colon_chain=raw):
        self.assertEqual(scrub_sensitive_text(raw), expected)

    for raw in (
        "auth_config:custom_ns:api_key: Env: CARD_EXPIRED",
        "auth.password:custom_ns:api_key: CARD_EXPIRED",
    ):
      with self.subTest(colon_chain_preserve=raw):
        out = scrub_sensitive_text(raw)
        self.assertIn("CARD_EXPIRED", out)
        self.assertIn("[REDACTED]", out)

    # 4. Wrapped large 300-item arrays, caps, and linear nesting cost.
    inner_300 = [
        json.dumps(
            {"status": "CARD_EXPIRED", "msg": f"item_{i}", "pad": "x" * 20}
        )
        for i in range(300)
    ]
    inner_300_sec = [
        json.dumps(
            {"status": "CARD_EXPIRED", "password": f"Sec_{i}", "pad": "x" * 20}
        )
        for i in range(300)
    ]
    for label, payload in (
        ("wrapper_dict", json.dumps({"wrapper": json.dumps(inner_300)})),
        ("log_str", "log=" + json.dumps(json.dumps(inner_300))),
        (
            "depth2_wrap",
            json.dumps({"a": json.dumps({"b": json.dumps(inner_300)})}),
        ),
        (
            "wrapper_with_secrets",
            json.dumps({"wrapper": json.dumps(inner_300_sec)}),
        ),
    ):
      with self.subTest(wrapped_array=label):
        start = time.monotonic()
        out = scrub_sensitive_text(payload)
        elapsed = time.monotonic() - start
        self.assertEqual(out.count("CARD_EXPIRED"), 300)
        self.assertNotIn("Sec_", out)
        self.assertLess(elapsed, 1.5)

    nested_300 = [
        json.dumps({"m": json.dumps(json.dumps({"password": f"Sec_{i}"}))})
        for i in range(300)
    ]
    flat_2500 = [json.dumps({"password": f"Sec_{i}"}) for i in range(2500)]
    for label, items, limit_s in (
        ("nested_300_cap", nested_300, 1.5),
        ("flat_2500_cap", flat_2500, 2.0),
    ):
      with self.subTest(adversarial_cap=label):
        payload = json.dumps({"wrapper": json.dumps(items)})
        start = time.monotonic()
        out = scrub_sensitive_text(payload)
        elapsed = time.monotonic() - start
        self.assertNotIn("Sec_", out)
        self.assertLess(elapsed, limit_s)

    scrubber_mod = sys.modules[scrub_sensitive_text.__module__]
    orig_scrub = scrubber_mod.__dict__["_scrub_text_at_depth"]
    nested = "ok"
    for depth in range(1, 13):
      nested = json.dumps([nested])
      with self.subTest(linear_nesting_depth=depth):
        with mock.patch.object(
            scrubber_mod, "_scrub_text_at_depth", side_effect=orig_scrub
        ) as spy:
          scrub_sensitive_text(nested)
        self.assertGreater(spy.call_count, 0)
        self.assertLessEqual(spy.call_count, 3 * depth)

  def test_scrubber_escaped_flat_budget_json_numbers_and_sens_hops(
      self,
  ) -> None:
    from analytics.trajectory_extractor import scrub_sensitive_text  # noqa: PLC0415

    escape_templates = (
        ("nl", "line1\nline2_{}"),
        ("tab", "col1\tcol2_{}"),
        ("dq", 'say "hi"_{}'),
        ("win", "C:\\app\\pay.py:{} C:\\Users\\app\\C:\\users\\app"),
        ("unicode", "café_☕_{}"),
    )
    for esc_name, tmpl in escape_templates:
      items = [
          json.dumps(
              {"status": "CARD_EXPIRED", "msg": tmpl.format(i)},
              ensure_ascii=True,
          )
          for i in range(300)
      ]
      wrappers = (
          ("wrapper_dict", json.dumps({"wrapper": json.dumps(items)})),
          (
              "depth2_wrap",
              json.dumps({"a": json.dumps({"b": json.dumps(items)})}),
          ),
          ("ndjson", json.dumps({"wrapper": "\n".join(items)})),
      )
      for wrap_name, payload in wrappers:
        with self.subTest(escape=esc_name, wrapper=wrap_name):
          self.assertEqual(
              scrub_sensitive_text(payload).count("CARD_EXPIRED"), 300
          )

    tail_err = "err=" + json.dumps(
        json.dumps({"status": "CARD_EXPIRED", "msg": "x\ny"})
    )
    sibling_cases = (
        (
            "wrapped",
            json.dumps(
                {
                    "wrapper": json.dumps(
                        {
                            "items": [
                                json.dumps({"msg": f"x\ny_{i}"})
                                for i in range(300)
                            ],
                            "last_error": tail_err,
                        }
                    )
                }
            ),
        ),
        (
            "unwrapped",
            json.dumps(
                {
                    "items": json.dumps(
                        [json.dumps({"msg": f"a\nb_{i}"}) for i in range(300)]
                    ),
                    "last_error": tail_err,
                }
            ),
        ),
    )
    for label, payload in sibling_cases:
      with self.subTest(sibling_starvation=label):
        self.assertIn("CARD_EXPIRED", scrub_sensitive_text(payload))

    d2_esc_sec = json.dumps(
        {
            "wrapper": json.dumps(
                [
                    json.dumps({"m": json.dumps({"password": f"Sec_{i}"})})
                    for i in range(300)
                ]
            )
        }
    )
    self.assertNotIn("Sec_", scrub_sensitive_text(d2_esc_sec))

    raw = (
        '{"a": {"https://u:sec1@x.com": "v1", "https://u:sec2@x.com": "v2"}, '
        '"token_count": 5, "password_count": 500000, '
        '"password": 493826, "api_key": -12.34e5}'
    )
    out = scrub_sensitive_text(raw)
    for kept in ("v1", "v2"):
      self.assertIn(kept, out)
    for leaked in ("sec1", "sec2", "500000", "493826", "-12.34e5"):
      self.assertNotIn(leaked, out)
    parsed = json.loads(out)
    self.assertEqual(parsed["token_count"], 5)
    for key in ("password_count", "password", "api_key"):
      self.assertEqual(parsed[key], "[REDACTED]")

    colon_cases = (
        (
            "auth.password:custom_ns:secret:api_key: SampleMarker",
            "auth.password:custom_ns:secret:api_key: [REDACTED]",
        ),
        (
            "auth.password:custom_ns:secret_config:api_key: SampleMarker",
            "auth.password:custom_ns:secret_config:api_key: [REDACTED]",
        ),
        (
            "api_secret:ns1:ns2:token: SampleMarker",
            "api_secret:[REDACTED] SampleMarker",
        ),
        (
            "auth.password:v1:secret:api_key: SampleMarker",
            "auth.password:v1:secret:api_key: [REDACTED]",
        ),
        (
            "auth.password:custom_ns:v1:api_key: SampleMarker",
            "auth.password:custom_ns:v1:api_key: [REDACTED]",
        ),
        ("token:abc:def:api_key: x1", "token:[REDACTED] x1"),
    )
    for src, expected in colon_cases:
      with self.subTest(colon_hop=src):
        self.assertEqual(scrub_sensitive_text(src), expected)


if __name__ == "__main__":
  unittest.main()
