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


if __name__ == "__main__":
  unittest.main()
