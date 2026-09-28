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
or plant symlinks the producer followed. A checked directory is then used
only through the descriptor that was checked: in a non-sticky shared
parent (a Kubernetes emptyDir ``/tmp``) another user can swap a path
component at any time. Ownership by another user is simulated by faking
``os.geteuid`` or the stat calls: tests cannot chown.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
from pathlib import Path
import select
import stat
import subprocess
import sys
import tempfile

import pytest

from bigquery_agent_analytics_tracing import _utils
from bigquery_agent_analytics_tracing import claude_code
from bigquery_agent_analytics_tracing import drain
from bigquery_agent_analytics_tracing import logger as logger_module
from bigquery_agent_analytics_tracing._utils import log_to_file
from bigquery_agent_analytics_tracing.claude_code import StateStore
from bigquery_agent_analytics_tracing.config import BQAAConfig
from bigquery_agent_analytics_tracing.config import DEFAULT_LOG_FILE
from bigquery_agent_analytics_tracing.config import DEFAULT_SPOOL_DIR
from bigquery_agent_analytics_tracing.config import DEFAULT_STATE_DIR
from bigquery_agent_analytics_tracing.logger import BigQueryAgentAnalyticsLogger

SRC = Path(__file__).resolve().parents[1] / "src"

GROUP_WRITABLE = pytest.mark.parametrize("mode", [0o770, 0o775], ids=oct)

# os.stat_result fields set by position; the rest go in by name.
_STAT_SEQUENCE_FIELDS = frozenset(
    {"st_mode", "st_ino", "st_dev", "st_nlink", "st_uid", "st_gid", "st_size"}
)


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


def _plant_envelope(
    directory: Path,
    name: str = "event-0-planted.json",
    project: str = "attacker-project",
) -> Path:
  envelope = directory / name
  envelope.write_text(
      json.dumps(
          {
              "config": {
                  "project_id": project,
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


def _private_dirs(*directories: Path) -> None:
  for directory in directories:
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)


def _clear_bqaa_env(monkeypatch) -> None:
  for name in list(os.environ):
    if name.startswith("BQAA_"):
      monkeypatch.delenv(name)


def _pretend_owned_by(monkeypatch, path: Path, uid: int) -> None:
  """Make every stat call report ``path`` (the inode) as owned by ``uid``."""
  target = os.lstat(path)
  identity = (target.st_dev, target.st_ino)

  def owned_by_uid(real_stat):
    def fake_stat(*args, **kwargs):
      result = real_stat(*args, **kwargs)
      if (result.st_dev, result.st_ino) != identity:
        return result
      fields = list(result)
      fields[stat.ST_UID] = uid
      named = {
          name: getattr(result, name)
          for name in dir(result)
          if name.startswith("st_") and name not in _STAT_SEQUENCE_FIELDS
      }
      return os.stat_result(fields, named)

    return fake_stat

  for name in ("stat", "lstat", "fstat"):
    monkeypatch.setattr(os, name, owned_by_uid(getattr(os, name)))


def _emptydir(tmp_path: Path) -> Path:
  """A shared parent like a Kubernetes emptyDir ``/tmp``: 0777, not sticky."""
  shared = tmp_path / "emptydir"
  shared.mkdir()
  shared.chmod(0o777)
  return shared


def _shared_tmp(tmp_path: Path, monkeypatch) -> Path:
  """A sticky world-writable dir owned by root, like ``/tmp``."""
  shared = tmp_path / "shared-tmp"
  shared.mkdir()
  shared.chmod(0o1777)
  _pretend_owned_by(monkeypatch, shared, 0)
  return shared


def _swap_after_first_check(
    monkeypatch, module, base: Path, replacement: Path
) -> list[Path]:
  """Swap ``base`` for a symlink right after ``module``'s first dir check.

  In a non-sticky shared parent another user may rename our entries, so
  this is the race a check not bound to what is used next would lose,
  scheduled deterministically.
  """
  real_check = module.ensure_private_dir
  swapped = []

  def check_then_swap(path):
    checked = real_check(path)
    if not swapped:
      base.rename(base.with_name(base.name + ".saved"))
      base.symlink_to(replacement, target_is_directory=True)
      swapped.append(base)
    return checked

  monkeypatch.setattr(module, "ensure_private_dir", check_then_swap)
  return swapped


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
  _pretend_owned_by(monkeypatch, shared, os.getuid() + 1)

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
# A swap right after the check does not redirect what is used next
# ----------------------------------------------------------------------------


def test_drainer_keeps_draining_the_spool_it_checked_after_a_swap(
    tmp_path, monkeypatch, uploads
):
  shared = _emptydir(tmp_path)
  config = _config(shared / "private-user")
  _private_dirs(Path(config.state_dir), Path(config.spool_dir))
  _plant_envelope(Path(config.spool_dir), "event-1-mine.json", "victim-project")
  attacker_tree = tmp_path / "attacker-tree"
  (attacker_tree / "spool").mkdir(parents=True)
  planted = _plant_envelope(attacker_tree / "spool")
  swapped = _swap_after_first_check(
      monkeypatch, drain, Path(config.state_dir), attacker_tree
  )

  drained = asyncio.run(drain._drain_once(config, use_storage_api=False))

  assert swapped
  assert (drained, uploads) == (1, ["victim-project"])
  assert planted.exists()
  checked_spool = shared / "private-user.saved" / "spool"
  assert list(checked_spool.glob("event-*.json")) == []


def test_spooled_row_lands_in_the_dir_checked_before_a_swap(
    tmp_path, monkeypatch, no_drainer
):
  shared = _emptydir(tmp_path)
  config = _config(shared / "private-user")
  attacker_tree = tmp_path / "attacker-tree"
  (attacker_tree / "spool").mkdir(parents=True)
  swapped = _swap_after_first_check(
      monkeypatch, logger_module, Path(config.state_dir), attacker_tree
  )

  _log_one_row(config)

  assert swapped
  assert list((attacker_tree / "spool").iterdir()) == []
  checked_spool = shared / "private-user.saved" / "spool"
  assert len(list(checked_spool.glob("event-*.json"))) == 1


def test_state_store_keeps_writing_the_dir_it_checked_after_a_swap(tmp_path):
  shared = _emptydir(tmp_path)
  base = shared / "private-user"
  store = StateStore("session", root=str(base))
  store.set("before", True)
  attacker_tree = tmp_path / "attacker-tree"
  attacker_tree.mkdir()
  planted = attacker_tree / "state_session.json"
  planted.write_text("{}")
  planted.chmod(0o666)
  base.rename(shared / "private-user.saved")
  base.symlink_to(attacker_tree, target_is_directory=True)

  store.set("current_prompt", "secret")

  assert json.loads(planted.read_text()) == {}
  checked = shared / "private-user.saved" / "state_session.json"
  assert json.loads(checked.read_text()) == {
      "before": True,
      "current_prompt": "secret",
  }


def test_drainer_pidfile_stays_in_the_dir_checked_before_a_swap(
    tmp_path, monkeypatch
):
  shared = _emptydir(tmp_path)
  base = shared / "private-user"
  _private_dirs(base, base / "spool")
  attacker_tree = tmp_path / "attacker-tree"
  (attacker_tree / "spool").mkdir(parents=True)
  planted = attacker_tree / "spool" / ".drainer.pid"
  planted.write_text("attacker")
  planted.chmod(0o666)
  swapped = _swap_after_first_check(monkeypatch, drain, base, attacker_tree)

  with drain._try_acquire_pidfile(base / "spool" / ".drainer.pid") as fd:
    assert fd is not None

  assert swapped
  assert planted.read_text() == "attacker"
  checked = shared / "private-user.saved" / "spool" / ".drainer.pid"
  assert checked.read_text() == str(os.getpid())


# ----------------------------------------------------------------------------
# Group-writable is as unsafe as world-writable
# ----------------------------------------------------------------------------


@GROUP_WRITABLE
def test_state_store_refuses_group_writable_dir(tmp_path, mode):
  root = tmp_path / "state"
  root.mkdir()
  root.chmod(mode)

  with pytest.raises(PermissionError):
    StateStore("s1", root=str(root))
  assert list(root.iterdir()) == []


@GROUP_WRITABLE
def test_hook_writes_no_state_into_group_writable_dir(
    tmp_path, monkeypatch, mode
):
  state_dir = tmp_path / "state"
  state_dir.mkdir()
  state_dir.chmod(mode)
  hook_log = tmp_path / "hook.log"
  _clear_bqaa_env(monkeypatch)
  monkeypatch.setenv("BQAA_STATE_DIR", str(state_dir))
  monkeypatch.setenv("BQAA_SPOOL_DIR", str(state_dir / "spool"))
  monkeypatch.setenv("BQAA_LOG_FILE", str(hook_log))
  monkeypatch.setattr(sys, "stdin", io.StringIO('{"session_id": "s1"}'))

  assert claude_code.main(["SessionStart"]) == 0
  assert list(state_dir.iterdir()) == []
  assert "Refusing BQAA tracing dir" in hook_log.read_text()


@GROUP_WRITABLE
def test_spool_refuses_group_writable_dir(tmp_path, no_drainer, mode):
  config = _config(tmp_path / "tracing")
  spool = Path(config.spool_dir)
  spool.mkdir(parents=True)
  spool.chmod(mode)

  with pytest.raises(PermissionError):
    _log_one_row(config)
  assert list(spool.iterdir()) == []


@GROUP_WRITABLE
def test_drainer_does_not_upload_from_group_writable_spool(
    tmp_path, monkeypatch, uploads, mode
):
  spool = tmp_path / "shared" / "spool"
  spool.mkdir(parents=True)
  planted = _plant_envelope(spool)
  spool.chmod(mode)
  _clear_bqaa_env(monkeypatch)
  monkeypatch.setenv("BQAA_SPOOL_DIR", str(spool))
  monkeypatch.setenv("BQAA_LOG_FILE", str(tmp_path / "bqaa.log"))
  monkeypatch.setenv("BQAA_DRAIN_IDLE_SECONDS", "0.05")
  monkeypatch.setenv("BQAA_DRAIN_POLL_SECONDS", "0.01")

  assert drain.main() == 1
  assert uploads == []
  assert planted.exists()


@GROUP_WRITABLE
def test_dead_letter_refuses_group_writable_dir(tmp_path, uploads, mode):
  config = _config(tmp_path / "tracing")
  spool = Path(config.spool_dir)
  _private_dirs(Path(config.state_dir), spool)
  corrupt = spool / "event-1-corrupt.json"
  corrupt.write_text("not json {{{")
  dead_letter = spool / "dead-letter"
  dead_letter.mkdir()
  dead_letter.chmod(mode)

  with pytest.raises(PermissionError):
    asyncio.run(drain._drain_once(config, use_storage_api=False))
  assert list(dead_letter.iterdir()) == []
  assert corrupt.exists()


@GROUP_WRITABLE
def test_log_is_not_written_into_group_writable_state_dir(tmp_path, mode):
  state_dir = tmp_path / "state"
  state_dir.mkdir()
  state_dir.chmod(mode)

  log_to_file(_config(state_dir), "ERROR hook=SessionStart: secret")

  assert list(state_dir.iterdir()) == []


@GROUP_WRITABLE
def test_explicit_log_refuses_group_writable_dir_we_own(tmp_path, mode):
  logs = tmp_path / "logs"
  logs.mkdir()
  logs.chmod(mode)
  config = _config(tmp_path / "tracing", dry_run=True)
  config.log_file = str(logs / "bqaa.log")

  _log_one_row(config)

  assert list(logs.iterdir()) == []


# ----------------------------------------------------------------------------
# The drainer trusts only regular envelope files that we own
# ----------------------------------------------------------------------------


def test_drainer_skips_envelope_owned_by_another_user(
    tmp_path, monkeypatch, uploads
):
  config = _config(tmp_path / "tracing")
  spool = Path(config.spool_dir)
  _private_dirs(Path(config.state_dir), spool)
  foreign = _plant_envelope(spool, "event-1-foreign.json")
  _plant_envelope(spool, "event-2-mine.json", "victim-project")
  _pretend_owned_by(monkeypatch, foreign, os.getuid() + 1)

  drained = asyncio.run(drain._drain_once(config, use_storage_api=False))

  assert (drained, uploads) == (1, ["victim-project"])
  assert foreign.exists()
  assert "event-1-foreign.json" in Path(config.log_file).read_text()


def test_drainer_skips_symlinked_envelope(tmp_path, uploads):
  config = _config(tmp_path / "tracing")
  spool = Path(config.spool_dir)
  _private_dirs(Path(config.state_dir), spool)
  outside = tmp_path / "outside"
  outside.mkdir()
  target = _plant_envelope(outside)
  (spool / "event-1-link.json").symlink_to(target)

  drained = asyncio.run(drain._drain_once(config, use_storage_api=False))

  assert (drained, uploads) == (0, [])
  assert target.exists()
  assert "event-1-link.json" in Path(config.log_file).read_text()


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


# ----------------------------------------------------------------------------
# An explicit log path, e.g. in a shared /tmp, is appended to only if safe
# ----------------------------------------------------------------------------


def _explicit_log_config(tmp_path: Path, log_file: Path) -> BQAAConfig:
  config = _config(tmp_path / "tracing", dry_run=True)
  config.log_file = str(log_file)
  return config


def test_explicit_log_does_not_follow_a_planted_symlink(tmp_path, monkeypatch):
  shared = _shared_tmp(tmp_path, monkeypatch)
  attacker_log = tmp_path / "attacker.log"
  attacker_log.write_text("")
  attacker_log.chmod(0o666)
  log = shared / "bqaa-agent-tracing.log"
  log.symlink_to(attacker_log)

  _log_one_row(_explicit_log_config(tmp_path, log))

  assert attacker_log.read_text() == ""


def test_explicit_log_refuses_a_file_owned_by_another_user(
    tmp_path, monkeypatch
):
  shared = _shared_tmp(tmp_path, monkeypatch)
  log = shared / "bqaa-agent-tracing.log"
  log.write_text("")
  log.chmod(0o666)
  _pretend_owned_by(monkeypatch, log, os.getuid() + 1)

  _log_one_row(_explicit_log_config(tmp_path, log))

  assert log.read_text() == ""


def test_explicit_log_refuses_a_hard_link_to_another_file(
    tmp_path, monkeypatch
):
  shared = _shared_tmp(tmp_path, monkeypatch)
  victim = _victim_file(tmp_path)
  log = shared / "bqaa-agent-tracing.log"
  os.link(victim, log)

  _log_one_row(_explicit_log_config(tmp_path, log))

  assert victim.read_text() == "export KEEP=1\n"


def test_hard_linked_log_in_a_dir_of_ours_still_works(tmp_path):
  # Only a dir others can write to lets them plant a link; ours may hold
  # linked files legitimately (e.g. rsync --link-dest backups).
  logs = tmp_path / "logs"
  logs.mkdir(mode=0o700)
  log = logs / "bqaa.log"
  log.write_text("")
  os.link(log, tmp_path / "backup-of-bqaa.log")

  _log_one_row(_explicit_log_config(tmp_path, log))

  assert "secret" in log.read_text()


def test_explicit_log_stays_in_the_dir_checked_before_a_swap(
    tmp_path, monkeypatch
):
  # Right after our log dir passes its check, another user renames it
  # away and puts in its place a symlink to a dir of theirs that holds a
  # hard link to one of our files.
  shared = _emptydir(tmp_path)
  logs = shared / "logs"
  logs.mkdir(mode=0o700)
  victim = _victim_file(tmp_path)
  attacker_tree = tmp_path / "attacker-tree"
  attacker_tree.mkdir()
  os.link(victim, attacker_tree / "bqaa.log")
  real_check = _utils._check_dir
  swapped = []

  def check_then_swap(info, path):
    real_check(info, path)
    if path == logs and not swapped:
      logs.rename(shared / "logs.saved")
      logs.symlink_to(attacker_tree, target_is_directory=True)
      swapped.append(logs)

  monkeypatch.setattr(_utils, "_check_dir", check_then_swap)

  log_to_file(_explicit_log_config(tmp_path, logs / "bqaa.log"), "secret")

  assert swapped
  assert victim.read_text() == "export KEEP=1\n"
  assert "secret" in (shared / "logs.saved" / "bqaa.log").read_text()


@pytest.mark.parametrize("foreign", ["dir", "parent"])
def test_explicit_log_refuses_a_dir_or_parent_owned_by_another_user(
    tmp_path, monkeypatch, foreign
):
  logs = tmp_path / "parent" / "logs"
  logs.mkdir(parents=True)
  owned = logs if foreign == "dir" else logs.parent
  _pretend_owned_by(monkeypatch, owned, os.getuid() + 1)

  log_to_file(_explicit_log_config(tmp_path, logs / "bqaa.log"), "secret")

  assert list(logs.iterdir()) == []


def test_explicit_log_in_a_symlinked_dir_still_works(tmp_path):
  # Like /tmp on macOS, a symlink to /private/tmp.
  logs = tmp_path / "logs"
  logs.mkdir(mode=0o700)
  (tmp_path / "link").symlink_to(logs, target_is_directory=True)

  log_to_file(
      _explicit_log_config(tmp_path, tmp_path / "link" / "bqaa.log"),
      "to-symlinked-dir",
  )

  assert "to-symlinked-dir" in (logs / "bqaa.log").read_text()


@pytest.mark.parametrize("mode", [0o300, 0o100], ids=oct)
def test_explicit_log_in_a_dir_we_can_search_but_not_list_still_works(
    tmp_path, mode
):
  # Appending to a known name needs only search permission on its dir,
  # so a private dir of ours without read permission must still work.
  logs = tmp_path / "logs"
  logs.mkdir(mode=0o700)
  log = logs / "bqaa.log"
  log.write_text("earlier line\n")
  log.chmod(0o600)
  logs.chmod(mode)
  try:
    with log.open("a") as handle:  # An ordinary append works here.
      handle.write("ordinary append\n")
    log_to_file(_explicit_log_config(tmp_path, log), "producer append")
  finally:
    logs.chmod(0o700)

  text = log.read_text()
  assert text.startswith("earlier line\nordinary append\n")
  assert "producer append" in text
  assert _mode(log) == 0o600


def test_explicit_log_without_search_only_open_flags_still_works(
    tmp_path, monkeypatch
):
  # Without O_SEARCH or O_PATH, the dir is opened for reading.
  for name in ("O_SEARCH", "O_PATH"):
    monkeypatch.delattr(os, name, raising=False)
  logs = tmp_path / "logs"
  logs.mkdir(mode=0o700)

  log_to_file(_explicit_log_config(tmp_path, logs / "bqaa.log"), "no-search")

  assert "no-search" in (logs / "bqaa.log").read_text()


def test_explicit_log_refuses_a_fifo(tmp_path, monkeypatch):
  shared = _shared_tmp(tmp_path, monkeypatch)
  log = shared / "bqaa-agent-tracing.log"
  os.mkfifo(log)
  reader = os.open(log, os.O_RDONLY | os.O_NONBLOCK)
  try:
    _log_one_row(_explicit_log_config(tmp_path, log))
    try:
      received = os.read(reader, 4096)
    except BlockingIOError:
      received = b""
  finally:
    os.close(reader)

  assert received == b""


def test_existing_readable_log_is_made_owner_only(tmp_path):
  legacy = tmp_path / "legacy"
  legacy.mkdir()
  legacy.chmod(0o755)
  log = legacy / "bqaa-agent-tracing.log"
  log.write_text("earlier line\n")
  log.chmod(0o644)

  _log_one_row(_explicit_log_config(tmp_path, log))

  assert _mode(log) == 0o600
  text = log.read_text()
  assert text.startswith("earlier line\n")
  assert "secret" in text


def test_log_to_dev_stderr_still_works(tmp_path):
  code = (
      "from bigquery_agent_analytics_tracing._utils import log_to_file\n"
      "from bigquery_agent_analytics_tracing.config import BQAAConfig\n"
      "log_to_file(BQAAConfig('p', 'd', log_file='/dev/stderr'), 'to-stderr')\n"
  )
  env = dict(os.environ, PYTHONPATH=str(SRC), TMPDIR=str(tmp_path))

  result = subprocess.run(
      [sys.executable, "-c", code],
      env=env,
      capture_output=True,
      text=True,
      timeout=60,
  )

  assert result.returncode == 0, result.stderr
  assert "to-stderr" in result.stderr


def test_log_to_a_terminal_device_still_works(tmp_path):
  controller, terminal = os.openpty()
  try:
    config = _config(tmp_path / "tracing")
    config.log_file = os.ttyname(terminal)

    log_to_file(config, "to-terminal")

    readable, _, _ = select.select([controller], [], [], 5)
    received = os.read(controller, 4096) if readable else b""
  finally:
    os.close(controller)
    os.close(terminal)

  assert b"to-terminal" in received


# ----------------------------------------------------------------------------
# POSIX-only APIs are optional: import and first writes work without them
# ----------------------------------------------------------------------------

_FIRST_WRITES = r"""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys

missing, root = json.loads(sys.argv[1]), Path(sys.argv[2])
for name in missing:
  if name == "dir_fd":
    os.supports_dir_fd = set()
    os.supports_fd = set()
  else:
    delattr(os, name)

from bigquery_agent_analytics_tracing import drain
from bigquery_agent_analytics_tracing import logger
from bigquery_agent_analytics_tracing._utils import log_to_file
from bigquery_agent_analytics_tracing.claude_code import StateStore
from bigquery_agent_analytics_tracing.config import BQAAConfig
from bigquery_agent_analytics_tracing.config import DEFAULT_STATE_DIR

subprocess.Popen = lambda *args, **kwargs: None  # never spawn a drainer
uploads = []
drain._write_batch_insert_rows_json = (
    lambda **kwargs: uploads.append(kwargs["project"]) or True
)
state_dir = root / "state"
StateStore("s", root=str(state_dir)).set("x", 1)
config = BQAAConfig(
    project_id="p",
    dataset="d",
    state_dir=str(state_dir),
    spool_dir=str(state_dir / "spool"),
    log_file=str(state_dir / "bqaa.log"),
)
logger.BigQueryAgentAnalyticsLogger(config).log_event(event_type="STATE_DELTA")
drained = asyncio.run(drain._drain_once(config, use_storage_api=False))
log_to_file(config, "logged")
print(json.dumps({"default_state_dir": DEFAULT_STATE_DIR, "drained": drained, "uploads": uploads}))
"""

_POSIX_ONLY = [
    "getuid",
    "geteuid",
    "O_NOFOLLOW",
    "O_DIRECTORY",
    "O_CLOEXEC",
    "O_NONBLOCK",
    "O_NOCTTY",
    "fchmod",
    "dir_fd",
]


@pytest.mark.parametrize(
    "missing",
    [["getuid"], ["geteuid"], ["O_NOFOLLOW"], ["dir_fd"], _POSIX_ONLY],
    ids=["getuid", "geteuid", "O_NOFOLLOW", "dir_fd", "all"],
)
def test_import_and_first_writes_work_without_posix_only_apis(
    tmp_path, missing
):
  env = {k: v for k, v in os.environ.items() if not k.startswith("BQAA_")}
  env.update(
      PYTHONPATH=str(SRC), LOGNAME="bqaa-test-user", TMPDIR=str(tmp_path)
  )
  root = tmp_path / "root"

  result = subprocess.run(
      [sys.executable, "-c", _FIRST_WRITES, json.dumps(missing), str(root)],
      env=env,
      capture_output=True,
      text=True,
      timeout=60,
  )

  assert result.returncode == 0, result.stderr
  out = json.loads(result.stdout)
  assert (out["drained"], out["uploads"]) == (1, ["p"])
  state_dir = root / "state"
  assert json.loads((state_dir / "state_s.json").read_text()) == {"x": 1}
  assert "logged" in (state_dir / "bqaa.log").read_text()
  assert _mode(state_dir) == 0o700
  assert _mode(state_dir / "state_s.json") == 0o600
  # Without getuid the default dir is per login name, never a constant.
  owner = "bqaa-test-user" if "getuid" in missing else str(os.getuid())
  assert Path(out["default_state_dir"]) == tmp_path / (
      f"bqaa-agent-tracing-{owner}"
  )


def test_without_dir_fd_support_unsafe_dirs_are_still_refused(
    tmp_path, monkeypatch, no_drainer
):
  monkeypatch.setattr(os, "supports_dir_fd", set())
  group_writable = tmp_path / "state"
  group_writable.mkdir()
  group_writable.chmod(0o775)
  elsewhere = tmp_path / "elsewhere"
  elsewhere.mkdir()
  config = _config(tmp_path / "tracing")
  Path(config.state_dir).mkdir()
  Path(config.spool_dir).symlink_to(elsewhere)

  with pytest.raises(PermissionError):
    StateStore("s1", root=str(group_writable))
  with pytest.raises(PermissionError):
    _log_one_row(config)
  assert list(group_writable.iterdir()) == []
  assert list(elsewhere.iterdir()) == []
