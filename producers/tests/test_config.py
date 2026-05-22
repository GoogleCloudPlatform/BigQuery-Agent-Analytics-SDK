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

"""Tests for ``BQAAConfig`` env-driven construction and defaults."""

from __future__ import annotations

import pytest

from bigquery_agent_analytics_tracing._writer_identity import DEFAULT_WRITER_LABEL
from bigquery_agent_analytics_tracing._writer_identity import WRITER_PLUGIN_NAME
from bigquery_agent_analytics_tracing.config import BQAAConfig
from bigquery_agent_analytics_tracing.config import DEFAULT_DRAIN_BATCH_SIZE
from bigquery_agent_analytics_tracing.config import DEFAULT_DRAIN_IDLE_SECONDS
from bigquery_agent_analytics_tracing.config import DEFAULT_LOG_FILE
from bigquery_agent_analytics_tracing.config import DEFAULT_SPOOL_DIR
from bigquery_agent_analytics_tracing.config import DEFAULT_STATE_DIR

_BQAA_VARS = [
    "BQAA_PROJECT_ID",
    "BQAA_DATASET",
    "BQAA_TABLE",
    "BQAA_AGENT_NAME",
    "BQAA_USER_ID",
    "BQAA_TRACE_ENABLED",
    "BQAA_DRY_RUN",
    "BQAA_DIRECT_WRITE",
    "BQAA_AUTO_CREATE_TABLE",
    "BQAA_AUTO_CREATE_DATASET",
    "BQAA_LOCATION",
    "BQAA_MAX_CONTENT_LENGTH",
    "BQAA_LOG_FILE",
    "BQAA_SPOOL_DIR",
    "BQAA_STATE_DIR",
    "BQAA_STATE_TTL_HOURS",
    "BQAA_TRANSCRIPT_MAX_BYTES",
    "BQAA_DRAIN_IDLE_SECONDS",
    "BQAA_DRAIN_BATCH_SIZE",
    "BQAA_DRAIN_POLL_SECONDS",
    "BQAA_WRITER_LABEL",
    "GCP_PROJECT_ID",
    "GOOGLE_CLOUD_PROJECT",
    "BQ_DATASET",
    "BQ_TABLE",
    "USER",
]


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
  for var in _BQAA_VARS:
    monkeypatch.delenv(var, raising=False)
  yield


def test_from_env_defaults_with_empty_env():
  config = BQAAConfig.from_env()

  assert config.project_id == ""
  assert config.dataset == ""
  assert config.table == "agent_events"
  assert config.agent_name == "coding-agent"
  assert config.user_id == "local-user"
  assert config.enabled is True
  assert config.dry_run is False
  assert config.direct_write is False
  assert config.auto_create_table is True
  assert config.auto_create_dataset is False
  assert config.location is None
  assert config.max_content_length == 5000
  assert config.log_file == DEFAULT_LOG_FILE
  assert config.spool_dir == DEFAULT_SPOOL_DIR
  assert config.state_dir == DEFAULT_STATE_DIR
  assert config.drain_idle_seconds == DEFAULT_DRAIN_IDLE_SECONDS
  assert config.drain_batch_size == DEFAULT_DRAIN_BATCH_SIZE
  assert config.writer_label == DEFAULT_WRITER_LABEL


def test_from_env_reads_bqaa_specific_vars(monkeypatch):
  monkeypatch.setenv("BQAA_PROJECT_ID", "proj-bqaa")
  monkeypatch.setenv("GCP_PROJECT_ID", "proj-gcp")
  monkeypatch.setenv("BQAA_DATASET", "agents")
  monkeypatch.setenv("BQAA_TABLE", "events_v2")
  monkeypatch.setenv("BQAA_AGENT_NAME", "claude-code")
  monkeypatch.setenv("BQAA_USER_ID", "alice")
  monkeypatch.setenv("BQAA_TRACE_ENABLED", "false")
  monkeypatch.setenv("BQAA_DRY_RUN", "true")
  monkeypatch.setenv("BQAA_DIRECT_WRITE", "true")
  monkeypatch.setenv("BQAA_AUTO_CREATE_TABLE", "false")
  monkeypatch.setenv("BQAA_AUTO_CREATE_DATASET", "true")
  monkeypatch.setenv("BQAA_LOCATION", "US")
  monkeypatch.setenv("BQAA_MAX_CONTENT_LENGTH", "10000")
  monkeypatch.setenv("BQAA_WRITER_LABEL", "my-deployment/1.2.3")

  config = BQAAConfig.from_env()

  assert (
      config.project_id == "proj-bqaa"
  ), "BQAA_PROJECT_ID wins over GCP_PROJECT_ID"
  assert config.dataset == "agents"
  assert config.table == "events_v2"
  assert config.agent_name == "claude-code"
  assert config.user_id == "alice"
  assert config.enabled is False
  assert config.dry_run is True
  assert config.direct_write is True
  assert config.auto_create_table is False
  assert config.auto_create_dataset is True
  assert config.location == "US"
  assert config.max_content_length == 10000
  assert config.writer_label == "my-deployment/1.2.3"


def test_from_env_falls_back_to_gcp_and_bq_legacy_vars(monkeypatch):
  monkeypatch.setenv("GCP_PROJECT_ID", "proj-gcp")
  monkeypatch.setenv("BQ_DATASET", "legacy_dataset")
  monkeypatch.setenv("BQ_TABLE", "legacy_table")
  monkeypatch.setenv("USER", "legacy-user")

  config = BQAAConfig.from_env()

  assert config.project_id == "proj-gcp"
  assert config.dataset == "legacy_dataset"
  assert config.table == "legacy_table"
  assert config.user_id == "legacy-user"


def test_trace_enabled_kill_switch(monkeypatch):
  monkeypatch.setenv("BQAA_TRACE_ENABLED", "false")
  config = BQAAConfig.from_env()
  assert config.enabled is False

  monkeypatch.setenv("BQAA_TRACE_ENABLED", "true")
  config = BQAAConfig.from_env()
  assert config.enabled is True


def test_writer_label_defaults_to_package_identity():
  config = BQAAConfig.from_env()
  assert config.writer_label.startswith(WRITER_PLUGIN_NAME + "/")
  assert config.writer_label == DEFAULT_WRITER_LABEL


def test_writer_label_env_override_wins(monkeypatch):
  monkeypatch.setenv("BQAA_WRITER_LABEL", "custom/9.9.9")
  config = BQAAConfig.from_env()
  assert config.writer_label == "custom/9.9.9"
