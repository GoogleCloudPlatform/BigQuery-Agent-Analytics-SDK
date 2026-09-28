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

"""Spool, state and log files must stay private to the current user.

Spooled rows carry prompts, responses and tool I/O, and the drainer
uploads every envelope it finds with the current user's credentials. The
old defaults were fixed names in the shared ``/tmp``, reused without an
ownership check, so another local user could read rows, plant envelopes,
or plant symlinks the producer followed. Ownership by another user is
simulated by faking ``os.geteuid`` / ``os.stat``: tests cannot chown.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import stat
import tempfile

import pytest

from bigquery_agent_analytics_tracing import drain
from bigquery_agent_analytics_tracing import logger as logger_module
from bigquery_agent_analytics_tracing._utils import log_to_file
from bigquery_agent_analytics_tracing.claude_code import StateStore
from bigquery_agent_analytics_tracing.config import BQAAConfig
from bigquery_agent_analytics_tracing.config import DEFAULT_LOG_FILE
from bigquery_agent_analytics_tracing.config import DEFAULT_SPOOL_DIR
from bigquery_agent_analytics_tracing.config import DEFAULT_STATE_DIR
from bigquery_agent_analytics_tracing.logger import BigQueryAgentAnalyticsLogger


@pytest.fixture(autouse=True)
def _common_umask():
  previous = os.umask(0o022)
  yield
  os.umask(previous)


@pytest.fixture
def no_drainer(monkeypatch):
  monkeypatch.setattr(logger_module, "_ensure_drainer", lambda _config: None)


@pytest.fixture
def uploads(monkeypatch):
  """Projects the drainer would write to, with BigQuery stubbed out."""
  projects = []
  monkeypatch.setattr(drain, "_storage_write_available", lambda: False)
  monkeypatch.setattr(
      drain,
      "_write_batch_insert_rows_json",
      lambda **kwargs: projects.append(kwargs["project"]) or True,
  )
  return projects


def _plant_envelope(directory: Path) -> Path:
  envelope = directory / "event-0-planted.json"
  envelope.write_text(
      json.dumps(
          {
              "config": {
                  "project_id": "attacker-project",
                  "dataset": "planted",
                  "table": "t",
              },
              "row": {"event_type": "STATE_DELTA"},
          }
      )
  )
  return envelope


def _mode(path: Path) -> int:
  return stat.S_IMODE(os.lstat(path).st_mode)


def _config(state_dir: Path, **kwargs) -> BQAAConfig:
  return BQAAConfig(
      project_id="victim-project",
      dataset="victim_dataset",
      state_dir=str(state_dir),
      spool_dir=str(state_dir / "spool"),
      log_file=str(state_dir / "bqaa.log"),
      **kwargs,
  )


def _log_one_row(config: BQAAConfig) -> None:
  BigQueryAgentAnalyticsLogger(config).log_event(
      event_type="STATE_DELTA", content={"prompt": "secret"}
  )


def _victim_file(tmp_path: Path) -> Path:
  victim = tmp_path / "victim-home" / ".bashrc"
  victim.parent.mkdir()
  victim.write_text("export KEEP=1\n")
  return victim


# ----------------------------------------------------------------------------
# Defaults
# ----------------------------------------------------------------------------


def test_default_paths_live_in_a_per_user_dir_under_tempdir():
  base = Path(DEFAULT_STATE_DIR)
  assert base.parent == Path(tempfile.gettempdir())
  assert base.name == f"bqaa-agent-tracing-{os.getuid()}"
  assert Path(DEFAULT_SPOOL_DIR).parent == base
  assert Path(DEFAULT_LOG_FILE).parent == base


# ----------------------------------------------------------------------------
# Confidentiality: what the producer creates is owner-only
# ----------------------------------------------------------------------------


def test_spooled_rows_and_their_dirs_are_owner_only(tmp_path, no_drainer):
  config = _config(tmp_path / "tracing")

  _log_one_row(config)

  spool = Path(config.spool_dir)
  (envelope,) = spool.glob("event-*.json")
  assert _mode(Path(config.state_dir)) == 0o700
  assert _mode(spool) == 0o700
  assert _mode(envelope) == 0o600


def test_dry_run_log_is_owner_only(tmp_path):
  config = _config(tmp_path / "tracing", dry_run=True)
  config.log_file = str(tmp_path / "bqaa.log")

  _log_one_row(config)

  log = Path(config.log_file)
  assert "secret" in log.read_text()
  assert _mode(log) == 0o600


# ----------------------------------------------------------------------------
# Integrity: never reuse a directory another local user can control
# ----------------------------------------------------------------------------


def test_state_store_refuses_precreated_world_writable_dir(tmp_path):
  shared = tmp_path / "bqaa-agent-tracing"
  shared.mkdir()
  shared.chmod(0o777)

  with pytest.raises(PermissionError):
    StateStore("s1", root=str(shared))
  assert list(shared.iterdir()) == []


def test_spool_refuses_dir_owned_by_another_user(
    tmp_path, monkeypatch, no_drainer
):
  config = _config(tmp_path / "tracing")
  spool = Path(config.spool_dir)
  spool.mkdir(parents=True)
  monkeypatch.setattr(os, "geteuid", lambda: os.getuid() + 1)

  with pytest.raises(PermissionError):
    _log_one_row(config)
  assert list(spool.iterdir()) == []


def test_spool_refuses_symlinked_dir(tmp_path, no_drainer):
  elsewhere = tmp_path / "elsewhere"
  elsewhere.mkdir()
  config = _config(tmp_path / "tracing")
  Path(config.state_dir).mkdir()
  Path(config.spool_dir).symlink_to(elsewhere)

  with pytest.raises(PermissionError):
    _log_one_row(config)
  assert list(elsewhere.iterdir()) == []


def test_spool_refuses_dir_under_parent_owned_by_another_user(
    tmp_path, monkeypatch, no_drainer
):
  shared = tmp_path / "shared-tmp"
  shared.mkdir()
  shared.chmod(0o1777)  # sticky, yet its owner may still swap entries
  config = _config(shared / "tracing")
  foreign = os.path.realpath(shared)
  real_stat = os.stat

  def _stat_with_foreign_owner(path, *args, **kwargs):
    result = real_stat(path, *args, **kwargs)
    if isinstance(path, int) or os.path.realpath(path) != foreign:
      return result
    fields = list(result[:10])
    fields[stat.ST_UID] = os.getuid() + 1
    return os.stat_result(fields)

  monkeypatch.setattr(os, "stat", _stat_with_foreign_owner)

  with pytest.raises(PermissionError):
    _log_one_row(config)


def test_sticky_shared_parent_like_tmp_is_allowed(tmp_path, no_drainer):
  shared = tmp_path / "shared-tmp"
  shared.mkdir()
  shared.chmod(0o1777)
  config = _config(shared / "bqaa-agent-tracing-test")

  _log_one_row(config)

  assert len(list(Path(config.spool_dir).glob("event-*.json"))) == 1
  assert _mode(Path(config.state_dir)) == 0o700


def test_world_writable_tmp_without_sticky_bit_still_works(
    tmp_path, no_drainer
):
  # Kubernetes emptyDir volumes mounted at /tmp are 0777 without the
  # sticky bit; the private dir inside is still verified on every use.
  emptydir = tmp_path / "emptydir-tmp"
  emptydir.mkdir()
  emptydir.chmod(0o777)
  config = _config(emptydir / "bqaa-agent-tracing-test")

  _log_one_row(config)

  assert len(list(Path(config.spool_dir).glob("event-*.json"))) == 1
  assert _mode(Path(config.state_dir)) == 0o700


def test_drainer_does_not_upload_envelopes_from_unsafe_spool(
    tmp_path, monkeypatch, uploads
):
  spool = tmp_path / "shared" / "spool"
  spool.mkdir(parents=True)
  spool.chmod(0o777)
  planted = _plant_envelope(spool)
  for name in ("BQAA_DRY_RUN", "BQAA_TRACE_ENABLED"):
    monkeypatch.delenv(name, raising=False)
  monkeypatch.setenv("BQAA_SPOOL_DIR", str(spool))
  monkeypatch.setenv("BQAA_LOG_FILE", str(tmp_path / "bqaa.log"))
  monkeypatch.setenv("BQAA_DRAIN_IDLE_SECONDS", "0.05")
  monkeypatch.setenv("BQAA_DRAIN_POLL_SECONDS", "0.01")

  assert drain.main() == 1
  assert uploads == []
  assert planted.exists()


def test_drainer_rechecks_spool_before_every_pass(tmp_path, uploads):
  swapped_in = tmp_path / "attacker-dir"
  swapped_in.mkdir()
  _plant_envelope(swapped_in)
  config = _config(tmp_path / "tracing")
  Path(config.state_dir).mkdir(mode=0o700)
  # A long-running drainer passed its startup check before the swap.
  Path(config.spool_dir).symlink_to(swapped_in)

  with pytest.raises(PermissionError):
    asyncio.run(drain._drain_once(config, use_storage_api=False))
  assert uploads == []


# ----------------------------------------------------------------------------
# Fixed-name files are never opened through a planted symlink
# ----------------------------------------------------------------------------


def test_drainer_pidfile_symlink_is_not_followed(tmp_path):
  spool = tmp_path / "spool"
  spool.mkdir(mode=0o700)
  victim = _victim_file(tmp_path)
  (spool / ".drainer.pid").symlink_to(victim)

  with pytest.raises(OSError):
    with drain._try_acquire_pidfile(spool / ".drainer.pid"):
      pass
  assert victim.read_text() == "export KEEP=1\n"


def test_state_file_symlink_is_not_followed(tmp_path):
  root = tmp_path / "state"
  root.mkdir(mode=0o700)
  victim = _victim_file(tmp_path)
  (root / "state_s1.json").symlink_to(victim)

  with pytest.raises(OSError):
    StateStore("s1", root=str(root))
  assert victim.read_text() == "export KEEP=1\n"


def test_log_is_not_written_into_unsafe_state_dir(tmp_path):
  shared = tmp_path / "bqaa-agent-tracing"
  shared.mkdir()
  shared.chmod(0o777)
  victim = _victim_file(tmp_path)
  config = _config(shared)
  Path(config.log_file).symlink_to(victim)

  log_to_file(config, "ERROR hook=SessionStart: refused")

  assert victim.read_text() == "export KEEP=1\n"
