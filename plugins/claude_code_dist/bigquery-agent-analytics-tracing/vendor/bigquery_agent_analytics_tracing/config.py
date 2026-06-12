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

"""Runtime configuration for the BQAA tracing logger and drainer."""

from __future__ import annotations

from dataclasses import dataclass
import os

from ._writer_identity import DEFAULT_WRITER_LABEL

BQAA_EVENT_TYPES = frozenset(
    {
        "LLM_REQUEST",
        "LLM_RESPONSE",
        "TOOL_STARTING",
        "TOOL_COMPLETED",
        "HITL_CREDENTIAL_REQUEST",
        "HITL_CREDENTIAL_REQUEST_COMPLETED",
        "HITL_CONFIRMATION_REQUEST",
        "HITL_CONFIRMATION_REQUEST_COMPLETED",
        "HITL_INPUT_REQUEST",
        "HITL_INPUT_REQUEST_COMPLETED",
        "STATE_DELTA",
    }
)

DEFAULT_SPOOL_DIR = "/tmp/bqaa-agent-tracing/spool"
DEFAULT_STATE_DIR = "/tmp/bqaa-agent-tracing"
DEFAULT_TRANSCRIPT_MAX_BYTES = 256 * 1024  # 256 KB streamed cap per stop event.
DEFAULT_STATE_TTL_HOURS = 24
DEFAULT_DRAIN_IDLE_SECONDS = 8.0
DEFAULT_DRAIN_BATCH_SIZE = 50
DEFAULT_DRAIN_POLL_SECONDS = 0.5
DEFAULT_LOG_FILE = "/tmp/bqaa-agent-tracing.log"


def _env_bool(name: str, default: bool) -> bool:
  raw = os.environ.get(name)
  if raw is None:
    return default
  return raw.lower() == "true"


@dataclass
class BQAAConfig:
  """All knobs the logger and drainer need.

  Construct directly for library use, or call ``BQAAConfig.from_env()`` to
  pick up the standard ``BQAA_*`` env vars. The env factory is what hook
  scripts and the drainer subprocess use so producers stay drop-in.
  """

  project_id: str
  dataset: str
  table: str = "agent_events"
  agent_name: str = "coding-agent"
  user_id: str = "local-user"
  enabled: bool = True
  dry_run: bool = False
  direct_write: bool = False
  auto_create_table: bool = True
  auto_create_dataset: bool = False
  location: str | None = None
  max_content_length: int = 5000
  log_file: str = DEFAULT_LOG_FILE
  spool_dir: str = DEFAULT_SPOOL_DIR
  state_dir: str = DEFAULT_STATE_DIR
  state_ttl_hours: float = DEFAULT_STATE_TTL_HOURS
  transcript_max_bytes: int = DEFAULT_TRANSCRIPT_MAX_BYTES
  drain_idle_seconds: float = DEFAULT_DRAIN_IDLE_SECONDS
  drain_batch_size: int = DEFAULT_DRAIN_BATCH_SIZE
  drain_poll_seconds: float = DEFAULT_DRAIN_POLL_SECONDS
  writer_label: str = DEFAULT_WRITER_LABEL

  @classmethod
  def from_env(cls) -> "BQAAConfig":
    return cls(
        project_id=(
            os.environ.get("BQAA_PROJECT_ID")
            or os.environ.get("GCP_PROJECT_ID")
            or os.environ.get("GOOGLE_CLOUD_PROJECT")
            or ""
        ),
        dataset=(
            os.environ.get("BQAA_DATASET") or os.environ.get("BQ_DATASET") or ""
        ),
        table=(
            os.environ.get("BQAA_TABLE")
            or os.environ.get("BQ_TABLE")
            or "agent_events"
        ),
        agent_name=os.environ.get("BQAA_AGENT_NAME") or "coding-agent",
        user_id=(
            os.environ.get("BQAA_USER_ID")
            or os.environ.get("USER")
            or "local-user"
        ),
        enabled=_env_bool("BQAA_TRACE_ENABLED", True),
        dry_run=_env_bool("BQAA_DRY_RUN", False),
        direct_write=_env_bool("BQAA_DIRECT_WRITE", False),
        auto_create_table=_env_bool("BQAA_AUTO_CREATE_TABLE", True),
        auto_create_dataset=_env_bool("BQAA_AUTO_CREATE_DATASET", False),
        location=os.environ.get("BQAA_LOCATION") or None,
        max_content_length=int(
            os.environ.get("BQAA_MAX_CONTENT_LENGTH", "5000")
        ),
        log_file=os.environ.get("BQAA_LOG_FILE", DEFAULT_LOG_FILE),
        spool_dir=os.environ.get("BQAA_SPOOL_DIR", DEFAULT_SPOOL_DIR),
        state_dir=os.environ.get("BQAA_STATE_DIR", DEFAULT_STATE_DIR),
        state_ttl_hours=float(
            os.environ.get("BQAA_STATE_TTL_HOURS", str(DEFAULT_STATE_TTL_HOURS))
        ),
        transcript_max_bytes=int(
            os.environ.get(
                "BQAA_TRANSCRIPT_MAX_BYTES", str(DEFAULT_TRANSCRIPT_MAX_BYTES)
            )
        ),
        drain_idle_seconds=float(
            os.environ.get(
                "BQAA_DRAIN_IDLE_SECONDS", str(DEFAULT_DRAIN_IDLE_SECONDS)
            )
        ),
        drain_batch_size=int(
            os.environ.get(
                "BQAA_DRAIN_BATCH_SIZE", str(DEFAULT_DRAIN_BATCH_SIZE)
            )
        ),
        drain_poll_seconds=float(
            os.environ.get(
                "BQAA_DRAIN_POLL_SECONDS", str(DEFAULT_DRAIN_POLL_SECONDS)
            )
        ),
        writer_label=(
            os.environ.get("BQAA_WRITER_LABEL") or DEFAULT_WRITER_LABEL
        ),
    )
