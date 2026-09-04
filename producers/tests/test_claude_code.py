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

"""Tests for the Claude Code hook producer.

State store + per-hook dispatch are exercised via dry-run mode so no
BigQuery client is required.
"""

from __future__ import annotations

import io
import json
from pathlib import Path
import subprocess
import sys

import pytest

from bigquery_agent_analytics_tracing.claude_code import _read_transcript_since
from bigquery_agent_analytics_tracing.claude_code import _tool_origin
from bigquery_agent_analytics_tracing.claude_code import _tool_status
from bigquery_agent_analytics_tracing.claude_code import CLAUDE_CODE_DEFAULT_AGENT
from bigquery_agent_analytics_tracing.claude_code import ClaudeHookBQAAAdapter
from bigquery_agent_analytics_tracing.claude_code import cleanup_stale_state
from bigquery_agent_analytics_tracing.claude_code import main
from bigquery_agent_analytics_tracing.claude_code import run_claude_hook
from bigquery_agent_analytics_tracing.claude_code import StateStore
from bigquery_agent_analytics_tracing.config import BQAAConfig
from bigquery_agent_analytics_tracing.logger import BigQueryAgentAnalyticsLogger

# ----------------------------------------------------------------------------
# Fixtures + helpers
# ----------------------------------------------------------------------------


@pytest.fixture
def dry_run_config(tmp_path):
  return BQAAConfig(
      project_id="test-project",
      dataset="test_dataset",
      agent_name="coding-agent",
      dry_run=True,
      log_file=str(tmp_path / "bqaa.log"),
      spool_dir=str(tmp_path / "spool"),
      state_dir=str(tmp_path / "state"),
      writer_label="bigquery-agent-analytics-tracing/test",
      transcript_max_bytes=64 * 1024,
  )


@pytest.fixture
def adapter(dry_run_config):
  return ClaudeHookBQAAAdapter(BigQueryAgentAnalyticsLogger(dry_run_config))


def _emitted_rows(log_file: str) -> list[dict]:
  path = Path(log_file)
  if not path.exists():
    return []
  rows: list[dict] = []
  for line in path.read_text().splitlines():
    if "DRY_RUN " not in line:
      continue
    rows.append(json.loads(line.split("DRY_RUN ", 1)[1]))
  return rows


# ----------------------------------------------------------------------------
# Tool helpers
# ----------------------------------------------------------------------------


def test_tool_origin_classifies_mcp_subagent_and_local():
  assert _tool_origin("mcp__github__create_issue") == "MCP"
  assert _tool_origin("Task") == "SUB_AGENT"
  assert _tool_origin("Subagent") == "SUB_AGENT"
  assert _tool_origin("Bash") == "LOCAL"
  assert _tool_origin(None) == "LOCAL"


def test_tool_status_detects_error_dict_and_error_prefix_string():
  assert _tool_status({"is_error": True, "error": "boom"}) == ("ERROR", "boom")
  assert _tool_status({"error": "x", "message": "y"}) == ("ERROR", "x")
  assert _tool_status("Error: timed out")[0] == "ERROR"
  assert _tool_status("ok output") == ("OK", None)
  assert _tool_status(None) == ("OK", None)


# ----------------------------------------------------------------------------
# State store
# ----------------------------------------------------------------------------


def test_state_store_round_trips_session_and_tool_state(tmp_path):
  store = StateStore("session-abc", root=str(tmp_path))
  store.update({"session_id": "s1", "agent": "claude-code"})

  reread = StateStore("session-abc", root=str(tmp_path))
  assert reread.get("session_id") == "s1"
  assert reread.get("agent") == "claude-code"

  store.set_tool("tool-1", {"start_ms": 123, "span_id": "abc"})
  store.set_tool("tool-2", {"start_ms": 456, "span_id": "def"})

  popped = store.pop_tool("tool-1")
  assert popped == {"start_ms": 123, "span_id": "abc"}
  assert not (tmp_path / "tool_session-abc_tool-1.json").exists()
  assert (tmp_path / "tool_session-abc_tool-2.json").exists()


def test_state_store_remove_clears_session_and_per_tool_files(tmp_path):
  store = StateStore("s", root=str(tmp_path))
  store.update({"session_id": "s"})
  store.set_tool("t1", {"x": 1})
  store.set_tool("t2", {"x": 2})

  store.remove()

  assert not (tmp_path / "state_s.json").exists()
  assert list(tmp_path.glob("tool_s_*.json")) == []


def test_state_store_sanitizes_unsafe_keys(tmp_path):
  store = StateStore("../weird/key!", root=str(tmp_path))
  store.update({"x": 1})
  # No path traversal — file lands inside tmp_path with a sanitized name.
  files = list(tmp_path.glob("state_*.json"))
  assert len(files) == 1
  assert files[0].parent == tmp_path


def test_cleanup_stale_state_removes_old_files(tmp_path):
  recent = tmp_path / "state_recent.json"
  stale = tmp_path / "state_stale.json"
  unrelated = tmp_path / "other.json"
  for path in (recent, stale, unrelated):
    path.write_text("{}")
  import os

  # Make `stale` 25 hours old; ttl is 24h.
  past = (time_now := __import__("time").time()) - 25 * 3600
  os.utime(stale, (past, past))

  removed = cleanup_stale_state(tmp_path, ttl_hours=24)

  assert removed == 1
  assert recent.exists()
  assert not stale.exists()
  assert unrelated.exists()
  del time_now  # quiet linter


def test_cleanup_stale_state_noop_when_ttl_disabled(tmp_path):
  assert cleanup_stale_state(tmp_path, ttl_hours=0) == 0
  assert cleanup_stale_state(tmp_path, ttl_hours=-1) == 0


# ----------------------------------------------------------------------------
# Transcript reader
# ----------------------------------------------------------------------------


def test_read_transcript_since_collects_assistant_text_and_usage(tmp_path):
  transcript = tmp_path / "transcript.jsonl"
  transcript.write_text(
      "\n".join(
          [
              json.dumps({"type": "user", "message": {"content": "hi"}}),
              json.dumps(
                  {
                      "type": "assistant",
                      "message": {
                          "model": "claude-opus-4-7",
                          "content": [{"type": "text", "text": "hello"}],
                          "usage": {
                              "input_tokens": 10,
                              "cache_read_input_tokens": 2,
                              "output_tokens": 5,
                          },
                      },
                  }
              ),
              json.dumps(
                  {
                      "type": "assistant",
                      "message": {
                          "content": [{"type": "text", "text": "world"}]
                      },
                  }
              ),
          ]
      )
      + "\n"
  )

  text, model, usage, truncated = _read_transcript_since(
      str(transcript), start_line=0, max_bytes=1024
  )

  assert text == "hello\nworld"
  assert model == "claude-opus-4-7"
  assert usage["prompt_tokens"] == 12
  assert usage["completion_tokens"] == 5
  assert usage["total_tokens"] == 17
  assert truncated is False


def test_read_transcript_since_truncates_at_max_bytes(tmp_path):
  transcript = tmp_path / "t.jsonl"
  transcript.write_text(
      json.dumps(
          {
              "type": "assistant",
              "message": {"content": [{"type": "text", "text": "x" * 5000}]},
          }
      )
      + "\n"
  )

  text, _, _, truncated = _read_transcript_since(
      str(transcript), start_line=0, max_bytes=100
  )

  assert truncated is True
  assert "[TRUNCATED]" in text
  assert len(text) <= 120  # 100 bytes + suffix


def test_read_transcript_since_skips_lines_before_start(tmp_path):
  transcript = tmp_path / "t.jsonl"
  transcript.write_text(
      "\n".join(
          [
              json.dumps(
                  {
                      "type": "assistant",
                      "message": {"content": [{"type": "text", "text": "OLD"}]},
                  }
              ),
              json.dumps(
                  {
                      "type": "assistant",
                      "message": {"content": [{"type": "text", "text": "NEW"}]},
                  }
              ),
          ]
      )
      + "\n"
  )

  text, _, _, _ = _read_transcript_since(
      str(transcript), start_line=1, max_bytes=1024
  )

  assert "OLD" not in text
  assert "NEW" in text


def test_read_transcript_since_missing_path_is_safe():
  text, model, usage, truncated = _read_transcript_since(
      "/does/not/exist", 0, 1024
  )
  assert text == ""
  assert model == ""
  assert usage["total_tokens"] == 0
  assert truncated is False


# ----------------------------------------------------------------------------
# Hook dispatch
# ----------------------------------------------------------------------------


def test_session_start_initializes_session_state(adapter, monkeypatch):
  monkeypatch.setenv("BQAA_AGENT_NAME", "claude-code")
  adapter.process("SessionStart", {"session_id": "abc123", "cwd": "/tmp/proj"})

  state = StateStore("abc123", root=adapter.config.state_dir)
  assert state.get("session_id") == "abc123"
  assert state.get("agent") == "claude-code"
  assert state.get("project_name") == "proj"
  assert state.get("trace_count") == 0


def test_user_prompt_submit_emits_llm_request_with_session_metadata(
    adapter, dry_run_config
):
  adapter.process("SessionStart", {"session_id": "s1", "cwd": "/tmp/x"})
  adapter.process(
      "UserPromptSubmit",
      {"session_id": "s1", "prompt": "do thing", "transcript_path": ""},
  )

  rows = _emitted_rows(dry_run_config.log_file)
  llm_request = [r for r in rows if r["event_type"] == "LLM_REQUEST"]
  assert len(llm_request) == 1
  row = llm_request[0]
  attrs = json.loads(row["attributes"])
  assert attrs["session_metadata"]["source"] == "claude_code"
  assert attrs["session_metadata"]["trace_number"] == 1
  assert attrs["custom_tags"]["assistant"] == "claude_code"
  assert attrs["writer"]["plugin"] == "bigquery-agent-analytics-tracing"


def test_pre_then_post_tool_use_share_span_id(adapter, dry_run_config):
  adapter.process("SessionStart", {"session_id": "s1"})
  adapter.process("UserPromptSubmit", {"session_id": "s1", "prompt": "p"})

  payload = {
      "session_id": "s1",
      "tool_use_id": "tool-xyz",
      "tool_name": "Bash",
      "tool_input": {"command": "ls"},
  }
  adapter.process("PreToolUse", payload)
  adapter.process(
      "PostToolUse",
      {**payload, "tool_response": "ok"},
  )

  rows = _emitted_rows(dry_run_config.log_file)
  pre = next(r for r in rows if r["event_type"] == "TOOL_STARTING")
  post = next(r for r in rows if r["event_type"] == "TOOL_COMPLETED")
  assert pre["span_id"] == post["span_id"], (
      "PostToolUse must reuse the span_id PreToolUse stamped, so the"
      " trace shows one span per tool call"
  )
  assert post["status"] == "OK"


def test_post_tool_use_without_pre_derives_deterministic_span(
    adapter, dry_run_config
):
  adapter.process("SessionStart", {"session_id": "s1"})
  adapter.process("UserPromptSubmit", {"session_id": "s1", "prompt": "p"})

  adapter.process(
      "PostToolUse",
      {
          "session_id": "s1",
          "tool_use_id": "orphan-tool",
          "tool_name": "Bash",
          "tool_response": "ok",
      },
  )

  rows = _emitted_rows(dry_run_config.log_file)
  post = next(r for r in rows if r["event_type"] == "TOOL_COMPLETED")
  # Deterministic span from the seed "orphan-tool".
  from bigquery_agent_analytics_tracing._utils import deterministic_span

  assert post["span_id"] == deterministic_span("orphan-tool")


def test_post_tool_use_error_dict_marks_row_as_error(adapter, dry_run_config):
  adapter.process("SessionStart", {"session_id": "s1"})
  adapter.process("UserPromptSubmit", {"session_id": "s1", "prompt": "p"})
  adapter.process(
      "PostToolUse",
      {
          "session_id": "s1",
          "tool_use_id": "t1",
          "tool_name": "Bash",
          "tool_response": {"is_error": True, "error": "boom"},
      },
  )

  rows = _emitted_rows(dry_run_config.log_file)
  post = next(r for r in rows if r["event_type"] == "TOOL_COMPLETED")
  assert post["status"] == "ERROR"
  assert post["error_message"] == "boom"


def test_stop_emits_llm_response_with_transcript_content(
    adapter, dry_run_config, tmp_path
):
  # UserPromptSubmit bookmarks the current transcript line count; Stop
  # reads everything appended after that point. Mirror the real Claude
  # Code flow: prompt fires first, assistant response is written to the
  # transcript next, then Stop fires.
  transcript = tmp_path / "transcript.jsonl"
  transcript.write_text(
      json.dumps({"type": "user", "message": {"content": "question?"}}) + "\n"
  )

  adapter.process("SessionStart", {"session_id": "s1"})
  adapter.process(
      "UserPromptSubmit",
      {
          "session_id": "s1",
          "prompt": "question?",
          "transcript_path": str(transcript),
      },
  )

  with transcript.open("a") as fh:
    fh.write(
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "model": "claude-opus-4-7",
                    "content": [{"type": "text", "text": "the answer is 42"}],
                    "usage": {"input_tokens": 5, "output_tokens": 3},
                },
            }
        )
        + "\n"
    )

  adapter.process(
      "Stop", {"session_id": "s1", "transcript_path": str(transcript)}
  )

  rows = _emitted_rows(dry_run_config.log_file)
  resp = next(r for r in rows if r["event_type"] == "LLM_RESPONSE")
  content = json.loads(resp["content"])
  assert "the answer is 42" in content["response"]
  attrs = json.loads(resp["attributes"])
  assert attrs["model"] == "claude-opus-4-7"
  assert attrs["usage_metadata"]["completion_tokens"] == 3


def test_stop_without_active_trace_is_noop(adapter, dry_run_config):
  adapter.process("SessionStart", {"session_id": "s1"})
  # No UserPromptSubmit -> no current_trace_id -> Stop drops on the floor.
  adapter.process("Stop", {"session_id": "s1"})
  rows = _emitted_rows(dry_run_config.log_file)
  assert not any(r["event_type"] == "LLM_RESPONSE" for r in rows)


def test_notification_emits_state_delta(adapter, dry_run_config):
  adapter.process(
      "Notification",
      {
          "session_id": "s1",
          "title": "Permission needed",
          "message": "Allow?",
          "notification_type": "permission",
      },
  )
  rows = _emitted_rows(dry_run_config.log_file)
  note = next(r for r in rows if r["event_type"] == "STATE_DELTA")
  attrs = json.loads(note["attributes"])
  assert attrs["state_delta"]["source"] == "claude_code_notification"
  assert attrs["state_delta"]["notification_type"] == "permission"


def test_permission_request_emits_hitl_event(adapter, dry_run_config):
  adapter.process(
      "PermissionRequest",
      {
          "session_id": "s1",
          "permission": "Bash",
          "tool_name": "Bash",
          "tool_input": {"command": "rm"},
      },
  )
  rows = _emitted_rows(dry_run_config.log_file)
  hitl = next(r for r in rows if r["event_type"] == "HITL_CONFIRMATION_REQUEST")
  attrs = json.loads(hitl["attributes"])
  assert attrs["session_metadata"]["source"] == "claude_code_permission_request"


def test_session_end_emits_state_delta_and_clears_state(
    adapter, dry_run_config
):
  adapter.process("SessionStart", {"session_id": "s1"})
  adapter.process("SessionEnd", {"session_id": "s1"})

  rows = _emitted_rows(dry_run_config.log_file)
  end = [
      r
      for r in rows
      if r["event_type"] == "STATE_DELTA"
      and json.loads(r["attributes"])["state_delta"]["source"]
      == "claude_code_session_end"
  ]
  assert len(end) == 1

  # State files for the session are gone.
  state_dir = Path(dry_run_config.state_dir)
  assert not (state_dir / "state_s1.json").exists()


def test_unknown_hook_name_is_silent(adapter, dry_run_config):
  adapter.process("NotARealHook", {"session_id": "s1"})
  rows = _emitted_rows(dry_run_config.log_file)
  assert rows == []


def test_run_claude_hook_emits_through_env_config(monkeypatch, tmp_path):
  monkeypatch.setenv("BQAA_DRY_RUN", "true")
  monkeypatch.setenv("BQAA_LOG_FILE", str(tmp_path / "envrun.log"))
  monkeypatch.setenv("BQAA_SPOOL_DIR", str(tmp_path / "spool"))
  monkeypatch.setenv("BQAA_STATE_DIR", str(tmp_path / "state"))

  run_claude_hook("SessionStart", {"session_id": "envtest", "cwd": "/tmp/p"})
  run_claude_hook(
      "UserPromptSubmit",
      {"session_id": "envtest", "prompt": "x", "transcript_path": ""},
  )

  rows = _emitted_rows(str(tmp_path / "envrun.log"))
  assert any(r["event_type"] == "LLM_REQUEST" for r in rows)


# ----------------------------------------------------------------------------
# main() entry
# ----------------------------------------------------------------------------


def test_main_returns_2_when_no_hook_name(monkeypatch, capsys):
  monkeypatch.setattr(sys, "stdin", io.StringIO(""))
  rc = main(argv=[])
  err = capsys.readouterr().err
  assert rc == 2
  assert "usage:" in err


def test_main_short_circuits_when_trace_disabled(monkeypatch, tmp_path):
  monkeypatch.setenv("BQAA_TRACE_ENABLED", "false")
  monkeypatch.setenv("BQAA_DRY_RUN", "true")
  monkeypatch.setenv("BQAA_LOG_FILE", str(tmp_path / "off.log"))
  monkeypatch.setenv("BQAA_SPOOL_DIR", str(tmp_path / "spool"))
  monkeypatch.setenv("BQAA_STATE_DIR", str(tmp_path / "state"))
  monkeypatch.setattr(
      sys, "stdin", io.StringIO(json.dumps({"session_id": "s1"}))
  )

  rc = main(argv=["SessionStart"])

  assert rc == 0
  assert not (tmp_path / "off.log").exists()


def test_main_dispatches_via_stdin_payload(monkeypatch, tmp_path):
  monkeypatch.setenv("BQAA_DRY_RUN", "true")
  monkeypatch.setenv("BQAA_LOG_FILE", str(tmp_path / "stdin.log"))
  monkeypatch.setenv("BQAA_SPOOL_DIR", str(tmp_path / "spool"))
  monkeypatch.setenv("BQAA_STATE_DIR", str(tmp_path / "state"))
  payload = json.dumps(
      {"session_id": "s1", "prompt": "hi", "transcript_path": ""}
  )

  # SessionStart first to seed state, then UserPromptSubmit emits the row.
  monkeypatch.setattr(
      sys, "stdin", io.StringIO(json.dumps({"session_id": "s1"}))
  )
  assert main(argv=["SessionStart"]) == 0
  monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
  assert main(argv=["UserPromptSubmit"]) == 0

  log_text = (tmp_path / "stdin.log").read_text()
  assert "LLM_REQUEST" in log_text


def test_main_never_raises_on_hook_error(monkeypatch, tmp_path):
  """An adapter exception must be logged, not propagated, so the host
  agent's hot path stays clean."""
  monkeypatch.setenv("BQAA_DRY_RUN", "true")
  monkeypatch.setenv("BQAA_LOG_FILE", str(tmp_path / "err.log"))
  monkeypatch.setenv("BQAA_SPOOL_DIR", str(tmp_path / "spool"))
  monkeypatch.setenv("BQAA_STATE_DIR", str(tmp_path / "state"))

  def _boom(self, hook_name, payload):
    raise RuntimeError("synthetic")

  monkeypatch.setattr(ClaudeHookBQAAAdapter, "process", _boom)
  monkeypatch.setattr(sys, "stdin", io.StringIO("{}"))

  rc = main(argv=["SessionStart"])
  assert rc == 0
  assert "synthetic" in (tmp_path / "err.log").read_text()


# ----------------------------------------------------------------------------
# Module is invocable as `python -m`
# ----------------------------------------------------------------------------


def test_claude_code_module_is_invocable_as_python_m(tmp_path):
  src_root = Path(__file__).resolve().parents[1] / "src"
  env = {
      "PYTHONPATH": str(src_root),
      "BQAA_DRY_RUN": "true",
      "BQAA_LOG_FILE": str(tmp_path / "m.log"),
      "BQAA_SPOOL_DIR": str(tmp_path / "spool"),
      "BQAA_STATE_DIR": str(tmp_path / "state"),
      "PATH": "/usr/bin:/bin",
  }
  result = subprocess.run(
      [
          sys.executable,
          "-m",
          "bigquery_agent_analytics_tracing.claude_code",
          "SessionStart",
      ],
      env=env,
      input=json.dumps({"session_id": "s1"}),
      capture_output=True,
      text=True,
      timeout=15,
  )
  assert (
      result.returncode == 0
  ), f"stdout={result.stdout!r} stderr={result.stderr!r}"


def test_claude_code_default_agent_constant_is_stable():
  assert CLAUDE_CODE_DEFAULT_AGENT == "claude-code"
