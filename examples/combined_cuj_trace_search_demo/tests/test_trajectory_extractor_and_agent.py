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

    # 2. Budget exhaustion in _scrub_embedded_json_containers never leaks tail.
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
    import time  # noqa: PLC0415

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
        t0 = time.monotonic()
        scrubbed_bomb = scrub_sensitive_text(bomb_text)
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 1.0)
        self.assertNotIn(tail_secret, scrubbed_bomb)
        self.assertIn("[REDACTED]", scrubbed_bomb)

  def test_preceding_word_perf_and_repr_escaped_dotted_numbered_keys(
      self,
  ) -> None:
    import time  # noqa: PLC0415

    from analytics.trajectory_extractor import is_sensitive_key  # noqa: PLC0415
    from analytics.trajectory_extractor import scrub_sensitive_data  # noqa: PLC0415
    from analytics.trajectory_extractor import scrub_sensitive_text  # noqa: PLC0415

    # 1. O(N^2) regression guard: repeated multiword tail keys on one line.
    long_line = ("x code=1 " * 25_000) + "password=tail7"
    t0 = time.monotonic()
    scrubbed_long = scrub_sensitive_text(long_line)
    elapsed = time.monotonic() - t0
    self.assertLess(elapsed, 1.0)
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
    import time  # noqa: PLC0415

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
        t0 = time.monotonic()
        out = scrub_sensitive_text(payload)
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 1.0)
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
    import time  # noqa: PLC0415

    from analytics.trajectory_extractor import scrub_sensitive_text  # noqa: PLC0415

    pad_17k = "a" * 17_000

    # 1. Dotted-path O(N) perf (< 1.0s) with trailing secret redaction.
    t0 = time.monotonic()
    dotted_out = scrub_sensitive_text(("a." * 25_000) + " password=END_S")
    self.assertLess(time.monotonic() - t0, 1.0)
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
    t1 = time.monotonic()
    scrub_sensitive_text('Cookie: a=\\"' + ("\\" * 20_000))
    self.assertLess(time.monotonic() - t1, 1.0)

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
    import time  # noqa: PLC0415

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
        t0 = time.monotonic()
        out = scrub_sensitive_text(payload)
        self.assertLess(time.monotonic() - t0, 1.0)
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
    import time  # noqa: PLC0415

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

    t0 = time.monotonic()
    semi_out = scrub_sensitive_text("Cookie: " + (";" * 50_000) + " b=END_CK4")
    self.assertLess(time.monotonic() - t0, 1.0)
    self.assertNotIn("END_CK4", semi_out)
    self.assertIn("[REDACTED]", semi_out)

    t1 = time.monotonic()
    comma_out = scrub_sensitive_text("Cookie: a=1" + (",x" * 20_000))
    self.assertLess(time.monotonic() - t1, 1.0)
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
    import time  # noqa: PLC0415

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
    t0 = time.monotonic()
    perf_out = scrub_sensitive_text(("x code=1 " * 25_000) + "password=tail7")
    self.assertLess(time.monotonic() - t0, 0.5)
    self.assertNotIn("tail7", perf_out)
    self.assertIn("[REDACTED]", perf_out)

  def test_stray_quote_kv_values_depth2_escapes_cookie_kv_boundaries_and_3bs_routing(
      self,
  ) -> None:
    import time  # noqa: PLC0415

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

    t0 = time.monotonic()
    _ = scrub_sensitive_text('"a="' * 20_000)
    self.assertLess(time.monotonic() - t0, 1.0)

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

    from analytics.trajectory_extractor import _unescape_safe_ascii_unicode_escapes  # noqa: PLC0415
    from analytics.trajectory_extractor import extract_s3_outcome_first
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
        t0 = time.perf_counter()
        unescaped = _unescape_safe_ascii_unicode_escapes(payload)
        dt_unescape = time.perf_counter() - t0
        self.assertTrue(unescaped.endswith(expected_suffix))
        self.assertLess(dt_unescape, 2.0)

        t1 = time.perf_counter()
        scrubbed = scrub_sensitive_text(payload)
        dt_scrub = time.perf_counter() - t1
        self.assertEqual(scrubbed, payload)
        self.assertLess(dt_scrub, 2.0)

    colons_payload = "a:" * 20000
    t2 = time.perf_counter()
    self.assertEqual(scrub_sensitive_text(colons_payload), colons_payload)
    self.assertLess(time.perf_counter() - t2, 2.0)

    for label, payload in (
        ("url_auth_colon_comma_50k", "https://u" + ":1," * 50000),
        ("url_auth_paren_50k", "https://u:" + ")" * 50000),
        ("url_auth_alnum_comma_50k", "https://u:1" + "a," * 50000),
        ("leading_path_punct_50k", "./-" * 50000),
    ):
      with self.subTest(linear_url_or_punct_perf=label):
        t3 = time.perf_counter()
        self.assertEqual(scrub_sensitive_text(payload), payload)
        self.assertLess(time.perf_counter() - t3, 2.0)


if __name__ == "__main__":
  unittest.main()
