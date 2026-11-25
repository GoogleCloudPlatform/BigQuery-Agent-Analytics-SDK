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
        ("cookies", "3", "cookies=3"),
    )
    for key_name, val, text_form in benign_kv_parity:
      with self.subTest(benign_parity=key_name):
        self.assertFalse(is_sensitive_key(key_name, is_container=False))
        d_out = scrub_sensitive_data({key_name: val})
        self.assertEqual(d_out[key_name], val)
        self.assertEqual(scrub_sensitive_text(text_form), text_form)
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
        ("cookies", "3", "cookies=3"),
    )
    for key_name, val, text_form in benign_cases:
      with self.subTest(benign_case=f"{key_name}:{val}"):
        self.assertFalse(is_sensitive_key(key_name, is_container=False))
        self.assertEqual(scrub_sensitive_data({key_name: val})[key_name], val)
        self.assertEqual(scrub_sensitive_text(text_form), text_form)
    self.assertEqual(
        scrub_sensitive_text("status is delivered"), "status is delivered"
    )


if __name__ == "__main__":
  unittest.main()
