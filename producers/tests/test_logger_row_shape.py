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

"""Tests for ``BigQueryAgentAnalyticsLogger`` row shape and routing."""

from __future__ import annotations

import json

import pytest

from bigquery_agent_analytics_tracing._writer_identity import WRITER_PLUGIN_NAME
from bigquery_agent_analytics_tracing.config import BQAAConfig
from bigquery_agent_analytics_tracing.logger import BigQueryAgentAnalyticsLogger


@pytest.fixture
def dry_run_config(tmp_path):
  return BQAAConfig(
      project_id="test-project",
      dataset="test_dataset",
      agent_name="claude-code",
      dry_run=True,
      log_file=str(tmp_path / "bqaa.log"),
      spool_dir=str(tmp_path / "spool"),
      state_dir=str(tmp_path / "state"),
      writer_label="bigquery-agent-analytics-tracing/test",
  )


def test_log_event_returns_row_with_canonical_top_level_fields(dry_run_config):
  logger = BigQueryAgentAnalyticsLogger(dry_run_config)
  row = logger.log_event(
      event_type="STATE_DELTA",
      content={"hello": "world"},
      session_id="sess",
      invocation_id="inv",
      trace_id="trace",
      span_id="span",
  )

  expected_fields = {
      "timestamp",
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
  assert set(row.keys()) == expected_fields
  assert row["event_type"] == "STATE_DELTA"
  assert row["agent"] == "claude-code"
  assert row["status"] == "OK"
  assert row["is_truncated"] is False


def test_writer_attribution_stamped_on_every_row(dry_run_config):
  logger = BigQueryAgentAnalyticsLogger(dry_run_config)
  row = logger.log_event(event_type="STATE_DELTA", content={"x": 1})

  writer = row["attributes"]["writer"]
  assert writer["plugin"] == WRITER_PLUGIN_NAME
  assert writer["label"] == "bigquery-agent-analytics-tracing/test"
  assert writer["agent"] == "claude-code"
  assert writer["mode"] == "dry_run"
  assert "version" in writer


def test_writer_mode_reflects_routing(tmp_path, monkeypatch):
  # writer.mode is derived inside log_event before _emit_row runs, so
  # stubbing _emit_row to a no-op lets us assert all three modes without
  # hitting BigQuery for the direct/spool cases.
  monkeypatch.setattr(
      BigQueryAgentAnalyticsLogger,
      "_emit_row",
      lambda self, row: None,
  )
  for kwargs, expected_mode in [
      ({"dry_run": True, "direct_write": False}, "dry_run"),
      ({"dry_run": False, "direct_write": True}, "direct"),
      ({"dry_run": False, "direct_write": False}, "spool"),
  ]:
    config = BQAAConfig(
        project_id="p",
        dataset="d",
        log_file=str(tmp_path / f"bqaa-{expected_mode}.log"),
        spool_dir=str(tmp_path / f"spool-{expected_mode}"),
        state_dir=str(tmp_path / f"state-{expected_mode}"),
        **kwargs,
    )
    logger = BigQueryAgentAnalyticsLogger(config)
    row = logger.log_event(event_type="STATE_DELTA")
    assert row["attributes"]["writer"]["mode"] == expected_mode, (
        f"expected mode={expected_mode!r} for kwargs={kwargs!r}, got"
        f" {row['attributes']['writer']['mode']!r}"
    )


def test_explicit_agent_override_propagates_to_row_and_writer(dry_run_config):
  logger = BigQueryAgentAnalyticsLogger(dry_run_config)
  row = logger.log_event(
      event_type="STATE_DELTA",
      agent="codex-cli",
  )
  assert row["agent"] == "codex-cli"
  assert row["attributes"]["writer"]["agent"] == "codex-cli"


def test_disabled_logger_short_circuits(dry_run_config):
  dry_run_config.enabled = False
  logger = BigQueryAgentAnalyticsLogger(dry_run_config)
  row = logger.log_event(event_type="STATE_DELTA")
  assert row == {}


def test_unknown_event_type_raises(dry_run_config):
  logger = BigQueryAgentAnalyticsLogger(dry_run_config)
  with pytest.raises(ValueError, match="Unsupported BQAA event_type"):
    logger.log_event(event_type="NOT_A_REAL_EVENT")


def test_content_truncation_marks_is_truncated(dry_run_config):
  dry_run_config.max_content_length = 10
  logger = BigQueryAgentAnalyticsLogger(dry_run_config)
  row = logger.log_event(
      event_type="STATE_DELTA",
      content={"text": "x" * 100},
  )
  assert row["is_truncated"] is True
  assert "[TRUNCATED]" in row["content"]["text"]


def test_sensitive_keys_are_redacted(dry_run_config):
  logger = BigQueryAgentAnalyticsLogger(dry_run_config)
  row = logger.log_event(
      event_type="STATE_DELTA",
      content={
          "api_key": "very-secret",
          "nested": {"password": "p4ssw0rd", "ok_field": "fine"},
      },
  )
  assert row["content"]["api_key"] == "[REDACTED]"
  assert row["content"]["nested"]["password"] == "[REDACTED]"
  assert row["content"]["nested"]["ok_field"] == "fine"


def test_caller_writer_attribute_does_not_suppress_package_attribution(
    dry_run_config,
):
  """attributes.writer is reserved — a caller-supplied writer must not
  win, or adoption queries on attributes.writer.plugin/version/label/mode
  silently break the moment a producer passes its own writer-like data.
  """
  logger = BigQueryAgentAnalyticsLogger(dry_run_config)
  caller_writer = {"plugin": "spoofed", "version": "99.99", "extra": "x"}
  row = logger.log_event(
      event_type="STATE_DELTA",
      attributes={"writer": caller_writer, "session_metadata": {"k": "v"}},
  )

  writer = row["attributes"]["writer"]
  assert writer["plugin"] == WRITER_PLUGIN_NAME
  assert writer["plugin"] != "spoofed"
  assert writer["label"] == "bigquery-agent-analytics-tracing/test"

  # Caller's value preserved under a separate key, not silently dropped.
  assert row["attributes"]["writer_caller"] == caller_writer
  # Unrelated attributes pass through untouched.
  assert row["attributes"]["session_metadata"] == {"k": "v"}


def test_part_attributes_dict_is_json_serialized_for_bigquery(dry_run_config):
  """part_attributes is declared as STRING in the schema; structured values
  must be JSON-serialized so the insert_rows_json path doesn't reject the
  row and the Storage Write API path doesn't see a non-string."""
  logger = BigQueryAgentAnalyticsLogger(dry_run_config)
  logger.log_event(
      event_type="STATE_DELTA",
      content_parts=[
          {
              "mime_type": "text/plain",
              "uri": None,
              "object_ref": None,
              "text": "hi",
              "part_index": 0,
              "part_attributes": {"lang": "en", "confidence": 0.9},
              "storage_mode": "INLINE",
          },
          {
              "mime_type": "text/plain",
              "uri": None,
              "object_ref": None,
              "text": "already-string",
              "part_index": 1,
              "part_attributes": "raw-string",
              "storage_mode": "INLINE",
          },
      ],
  )

  # The dry-run log line carries the BigQuery-shape row (post-serialize).
  log_text = __import__("pathlib").Path(dry_run_config.log_file).read_text()
  payload = log_text.split("DRY_RUN ", 1)[1].splitlines()[0]
  emitted = json.loads(payload)
  parts = emitted["content_parts"]

  # Dict → JSON string with sorted keys (matches object_ref.details).
  assert isinstance(parts[0]["part_attributes"], str)
  assert json.loads(parts[0]["part_attributes"]) == {
      "confidence": 0.9,
      "lang": "en",
  }
  # Already-string values pass through unchanged.
  assert parts[1]["part_attributes"] == "raw-string"


def test_dry_run_writes_to_log_file_and_not_bigquery(tmp_path, dry_run_config):
  logger = BigQueryAgentAnalyticsLogger(dry_run_config)
  logger.log_event(event_type="STATE_DELTA", content={"smoke": True})

  log_text = (tmp_path / "bqaa.log").read_text()
  assert "DRY_RUN" in log_text
  # The dry-run log line is a single JSON envelope after "DRY_RUN ".
  dry_payload = log_text.split("DRY_RUN ", 1)[1].splitlines()[0]
  parsed = json.loads(dry_payload)
  assert parsed["event_type"] == "STATE_DELTA"


def test_spool_mode_writes_envelope_file(tmp_path, monkeypatch):
  config = BQAAConfig(
      project_id="test-project",
      dataset="test_dataset",
      agent_name="claude-code",
      spool_dir=str(tmp_path / "spool"),
      log_file=str(tmp_path / "bqaa.log"),
      state_dir=str(tmp_path / "state"),
  )

  # Stub the drainer spawn so the test doesn't fork a real process.
  spawn_called = []

  def _fake_ensure(_config):
    spawn_called.append(True)

  monkeypatch.setattr(
      "bigquery_agent_analytics_tracing.logger._ensure_drainer", _fake_ensure
  )

  logger = BigQueryAgentAnalyticsLogger(config)
  logger.log_event(event_type="STATE_DELTA", content={"x": 1})

  spool_files = list((tmp_path / "spool").glob("event-*.json"))
  assert len(spool_files) == 1
  envelope = json.loads(spool_files[0].read_text())
  assert envelope["config"]["project_id"] == "test-project"
  assert envelope["config"]["dataset"] == "test_dataset"
  assert envelope["row"]["event_type"] == "STATE_DELTA"
  # JSON-typed fields are pre-serialized so the drainer's insert_rows_json
  # fallback can use them as-is.
  assert isinstance(envelope["row"]["content"], str)
  assert isinstance(envelope["row"]["attributes"], str)
  assert spawn_called == [True]


def test_direct_write_requires_project_and_dataset():
  config = BQAAConfig(project_id="", dataset="", direct_write=True)
  logger = BigQueryAgentAnalyticsLogger(config)
  with pytest.raises(ValueError, match="BQAA_PROJECT_ID"):
    logger.log_event(event_type="STATE_DELTA")
