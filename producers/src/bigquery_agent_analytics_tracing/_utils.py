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

"""Shared helpers for serialization, hashing, timestamps, private
directories, file locking, and log emission. No dependencies on other package
modules so this can be safely imported from anywhere in the package."""

from __future__ import annotations

import contextlib
from datetime import datetime
from datetime import timezone
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import time
from typing import Any, Iterator, TYPE_CHECKING
import uuid
import weakref

if TYPE_CHECKING:
  from .config import BQAAConfig

SENSITIVE_KEYS = frozenset(
    {
        "api_key",
        "authorization",
        "client_secret",
        "id_token",
        "password",
        "refresh_token",
        "secret",
        "token",
    }
)


def utc_now() -> datetime:
  return datetime.now(timezone.utc)


def timestamp_ms() -> int:
  return int(time.time() * 1000)


def iso_timestamp(value: datetime | None = None) -> str:
  ts = value or utc_now()
  return ts.isoformat(timespec="microseconds").replace("+00:00", "Z")


def hex_id(chars: int) -> str:
  return uuid.uuid4().hex[:chars]


def deterministic_span(seed: str, chars: int = 16) -> str:
  """Stable hex span id derived from a seed (e.g. tool_use_id).

  Used so that a PostToolUse without a matching PreToolUse can still emit a
  span_id that correlates with whatever the (missing) start would have
  produced. Better than ``hex_id`` which yields a fresh random id and breaks
  span correlation.
  """
  if not seed:
    return hex_id(chars)
  digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()
  return digest[:chars]


def to_jsonable(value: Any) -> Any:
  if value is None or isinstance(value, (str, int, float, bool)):
    return value
  if isinstance(value, dict):
    return {str(k): to_jsonable(v) for k, v in value.items()}
  if isinstance(value, (list, tuple, set)):
    return [to_jsonable(v) for v in value]
  if hasattr(value, "model_dump"):
    return to_jsonable(value.model_dump())
  if hasattr(value, "to_dict"):
    return to_jsonable(value.to_dict())
  return str(value)


def truncate(value: Any, max_len: int) -> tuple[Any, bool]:
  """Recursively truncate strings and redact sensitive dict keys.

  Returns ``(clipped_value, was_truncated)``. ``max_len == -1`` disables
  truncation. Keys whose lowercase name is in ``SENSITIVE_KEYS`` or starts
  with ``"temp:"`` are replaced with ``"[REDACTED]"``.
  """
  value = to_jsonable(value)
  if max_len == -1:
    return value, False
  if isinstance(value, str):
    if len(value) > max_len:
      return value[:max_len] + "...[TRUNCATED]", True
    return value, False
  if isinstance(value, list):
    out = []
    truncated = False
    for item in value:
      next_item, did_truncate = truncate(item, max_len)
      out.append(next_item)
      truncated = truncated or did_truncate
    return out, truncated
  if isinstance(value, dict):
    out: dict[str, Any] = {}
    truncated = False
    for key, item in value.items():
      key_lower = str(key).lower()
      if key_lower in SENSITIVE_KEYS or key_lower.startswith("temp:"):
        out[key] = "[REDACTED]"
        continue
      next_item, did_truncate = truncate(item, max_len)
      out[key] = next_item
      truncated = truncated or did_truncate
    return out, truncated
  return value, False


def safe_json_loads(value: str | None, default: Any) -> Any:
  if not value:
    return default
  try:
    return json.loads(value)
  except json.JSONDecodeError:
    return default


# POSIX-only pieces (the effective uid, O_NOFOLLOW and other open flags,
# the *at() calls behind ``dir_fd``) are looked up when used. Where one is
# missing, protection degrades to best effort instead of breaking import
# or the first write.


def _flag(name: str) -> int:
  """``os.<name>`` open flag, or 0 where this platform lacks it."""
  return getattr(os, name, 0)


def _euid() -> int | None:
  """Effective uid, or None without POSIX ownership (e.g. Windows)."""
  geteuid = getattr(os, "geteuid", None)
  return geteuid() if geteuid is not None else None


def _dir_fd_supported() -> bool:
  """Whether files can be used relative to a directory descriptor."""
  # By name, so that a wrapped os.stat (say) does not look unsupported.
  # os.replace uses the same renameat() as os.rename, and os.lstat the
  # same fstatat() as os.stat (not every Python version lists it).
  needed = {"open", "stat", "mkdir", "rename", "unlink"}
  return (
      hasattr(os, "O_DIRECTORY")
      and hasattr(os, "O_NOFOLLOW")
      and needed <= {f.__name__ for f in os.supports_dir_fd}
      and "listdir" in {f.__name__ for f in os.supports_fd}
  )


def _check_dir(info: os.stat_result, path: Path) -> None:
  """Raise unless ``info`` is a directory that only we can modify."""
  euid = _euid()
  if not stat.S_ISDIR(info.st_mode):
    problem = "is a symlink or not a directory"
  elif euid is None:
    # No POSIX ids (Windows reports every dir as 0o777): the per-user
    # location is all the protection there is.
    return
  elif info.st_uid != euid:
    problem = f"is owned by uid {info.st_uid}, not {euid}"
  elif info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
    problem = "is writable by its group or other users"
  else:
    return
  raise PermissionError(f"Refusing BQAA tracing dir {path}: it {problem}.")


def _check_own_file(info: os.stat_result, path: Path) -> None:
  """Raise unless ``info`` is a regular file that we own."""
  euid = _euid()
  if not stat.S_ISREG(info.st_mode):
    problem = "is not a regular file"
  elif euid is not None and info.st_uid != euid:
    problem = f"is owned by uid {info.st_uid}, not {euid}"
  else:
    return
  raise PermissionError(f"Refusing BQAA tracing file {path}: it {problem}.")


def _open_nofollow(target: str | Path, flags: int, mode: int, **at: int) -> int:
  """``os.open`` that never follows a symlink as the last component.

  O_NONBLOCK keeps a FIFO planted under the name from hanging the open.
  """
  if not _flag("O_NOFOLLOW") and os.path.islink(target):
    # Best effort; wherever ``dir_fd`` is used, O_NOFOLLOW exists.
    raise PermissionError(
        f"Refusing BQAA tracing file {target}: it is a symlink."
    )
  flags |= _flag("O_NOFOLLOW") | _flag("O_CLOEXEC") | _flag("O_NONBLOCK")
  return os.open(target, flags, mode, **at)


def _require_trusted_chain(directory: Path) -> None:
  """Raise unless ``directory`` and all its parents belong to root or us.

  A directory owned by another user lets that user rename our entries
  away and put their own in place.
  """
  euid = _euid()
  if euid is None:
    return
  real = Path(os.path.realpath(directory))
  for current in (real, *real.parents):
    owner = os.stat(current).st_uid
    if owner not in (0, euid):
      raise PermissionError(
          f"Refusing BQAA tracing path under {current}: it is owned by uid"
          f" {owner}."
      )


def _require_trusted_parents(fd: int, path: Path) -> None:
  """``_require_trusted_chain`` for the parents of the directory ``fd``.

  Walks ``..`` from the descriptor, so these are the parents of the
  directory actually opened, not of a path that may have been swapped.
  """
  euid = _euid()
  if euid is None:
    return
  current, up = os.fstat(fd), ".."
  for _ in range(256):
    parent = os.stat(up, dir_fd=fd)
    if (parent.st_dev, parent.st_ino) == (current.st_dev, current.st_ino):
      return  # "/" is its own parent.
    if parent.st_uid not in (0, euid):
      raise PermissionError(
          f"Refusing BQAA tracing path under {path / up}: it is owned by"
          f" uid {parent.st_uid}."
      )
    current, up = parent, f"{up}/.."
  raise PermissionError(f"Refusing BQAA tracing dir {path}: it is too deep.")


class PrivateDir:
  """A directory only we can modify, pinned by the descriptor checked.

  Returned by ``ensure_private_dir``. Files are created, listed, opened,
  renamed and removed relative to that descriptor (``openat()`` and
  friends), so renaming or replacing a path component after the check,
  as another user may in a non-sticky shared parent such as a Kubernetes
  emptyDir ``/tmp``, cannot redirect them. Without ``dir_fd`` support
  ``fd`` is None and the methods fall back to paths.
  """

  def __init__(self, path: Path, fd: int | None):
    self.path = path
    self.fd = fd
    self._close = None if fd is None else weakref.finalize(self, os.close, fd)

  def __enter__(self) -> PrivateDir:
    return self

  def __exit__(self, *exc_info: object) -> None:
    self.close()

  def close(self) -> None:
    if self._close is not None:
      self._close()

  def _at(self, name: str) -> tuple[str, dict[str, int]]:
    if self.fd is None:
      return os.path.join(self.path, name), {}
    return name, {"dir_fd": self.fd}

  def names(self) -> list[str]:
    return os.listdir(self.path if self.fd is None else self.fd)

  def lstat(self, name: str) -> os.stat_result:
    target, at = self._at(name)
    return os.lstat(target, **at)

  def exists(self, name: str) -> bool:
    try:
      self.lstat(name)
    except FileNotFoundError:
      return False
    return True

  def open(self, name: str, flags: int, mode: int = 0o600) -> int:
    """Open a regular file of ours in this dir, never through a symlink."""
    target, at = self._at(name)
    fd = _open_nofollow(target, flags, mode, **at)
    try:
      _check_own_file(os.fstat(fd), self.path / name)
    except BaseException:
      os.close(fd)
      raise
    return fd

  def unlink(self, name: str) -> None:
    target, at = self._at(name)
    os.unlink(target, **at)

  def replace(
      self, name: str, new_name: str, new_dir: PrivateDir | None = None
  ) -> None:
    """Rename ``name`` to ``new_name`` in ``new_dir`` (default: here)."""
    new_dir = new_dir or self
    if self.fd is None or new_dir.fd is None:
      os.replace(
          os.path.join(self.path, name), os.path.join(new_dir.path, new_name)
      )
    else:
      os.replace(name, new_name, src_dir_fd=self.fd, dst_dir_fd=new_dir.fd)

  def subdir(self, name: str) -> PrivateDir:
    """``ensure_private_dir`` for ``name`` inside this dir."""
    if self.fd is None:
      return ensure_private_dir(self.path / name)
    try:
      os.mkdir(name, 0o700, dir_fd=self.fd)
    except FileExistsError:
      pass
    return _open_private_dir(self.path / name, name, self.fd)


def ensure_private_dir(path: str | os.PathLike[str]) -> PrivateDir:
  """Create ``path`` owner-only (0o700), or verify an existing one.

  Spool and state files carry prompts, responses and tool I/O, and the
  drainer uploads every envelope it finds with this user's credentials.
  A directory that another local user pre-created or can write to (e.g.
  a fixed name under a shared /tmp, or a group-writable one) is refused
  instead of silently reused. The check is made on an open descriptor,
  which the returned ``PrivateDir`` keeps using; close it when done.
  """
  directory = Path(path).expanduser()
  missing = []
  current = directory
  while not os.path.lexists(current):
    missing.append(current)
    current = current.parent
  for component in reversed(missing):
    try:
      os.mkdir(component, 0o700)
    except FileExistsError:
      pass
  if not _dir_fd_supported():
    _check_dir(os.lstat(directory), directory)
    _require_trusted_chain(directory.parent)
    return PrivateDir(directory, None)
  return _open_private_dir(directory, directory, None)


def _open_private_dir(
    path: Path, target: str | Path, dir_fd: int | None
) -> PrivateDir:
  flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | _flag("O_CLOEXEC")
  try:
    fd = os.open(target, flags, dir_fd=dir_fd)
  except OSError as exc:
    # O_NOFOLLOW fails with ELOOP (EMLINK on FreeBSD), O_DIRECTORY with
    # ENOTDIR.
    if exc.errno in (errno.ELOOP, errno.EMLINK, errno.ENOTDIR):
      raise PermissionError(
          f"Refusing BQAA tracing dir {path}: it is a symlink or not a"
          " directory."
      ) from exc
    raise
  try:
    _check_dir(os.fstat(fd), path)
    _require_trusted_parents(fd, path)
  except BaseException:
    os.close(fd)
    raise
  return PrivateDir(path, fd)


@contextlib.contextmanager
def file_lock(path: Path, mode: int = fcntl.LOCK_EX) -> Iterator[int]:
  """Open a lockfile exclusively. Blocks until the lock is acquired."""
  path.parent.mkdir(parents=True, exist_ok=True)
  fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o600)
  try:
    fcntl.flock(fd, mode)
    yield fd
  finally:
    try:
      fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
      os.close(fd)


# The process's own standard streams: written to as they are.
_STREAM_ALIASES = frozenset({"/dev/stdout", "/dev/stderr"})


def _open_log(config: "BQAAConfig") -> int:
  """Open ``config.log_file`` for appending; raise if it is unsafe.

  The default log lives in the state dir and gets the same private-dir
  check (so a refused dir never receives the refusal message), as does a
  log whose dir does not exist yet. An existing dir must not belong to
  another user and, if it is ours, must be private; otherwise it is a
  shared dir such as /tmp. The log itself must be a character device
  (e.g. /dev/null or a terminal) or a regular file of ours, never reached
  through a symlink and, in a shared dir, with no other hard link. One
  that others can read is made owner-only.
  """
  path = Path(config.log_file).expanduser()
  append = os.O_WRONLY | os.O_APPEND | _flag("O_CLOEXEC") | _flag("O_NOCTTY")
  if str(path) in _STREAM_ALIASES:
    return os.open(path, append)  # Symlinks into /dev/fd or /proc.
  in_state_dir = path.parent == Path(config.state_dir).expanduser()
  if in_state_dir or not path.parent.is_dir():
    shared = False
    with ensure_private_dir(path.parent) as directory:
      fd = directory.open(path.name, append | os.O_CREAT)
  else:
    _require_trusted_chain(path.parent)
    parent = os.stat(path.parent)
    shared = parent.st_uid != _euid()
    if not shared:
      _check_dir(parent, path.parent)
    fd = _open_nofollow(path, append | os.O_CREAT, 0o600)
  try:
    info = os.fstat(fd)
    if stat.S_ISCHR(info.st_mode):
      if hasattr(os, "set_blocking"):
        os.set_blocking(fd, True)  # Undo O_NONBLOCK for terminal writes.
      return fd
    _check_own_file(info, path)
    if shared and info.st_nlink != 1:
      # Another user may have linked one of our files in under this name.
      raise PermissionError(
          f"Refusing BQAA tracing file {path}: it has other hard links."
      )
    if info.st_mode & 0o077 and hasattr(os, "fchmod"):
      os.fchmod(fd, 0o600)
  except BaseException:
    os.close(fd)
    raise
  return fd


def log_to_file(config: "BQAAConfig", message: str) -> None:
  """Append a single log line to ``config.log_file``. Silent on failure.

  The drainer and the spool writer share this — debugging signal lives in
  one place, and a broken or unsafe log path (see ``_open_log``) never
  crashes the agent's hot path.
  """
  if not config.log_file:
    return
  try:
    fd = _open_log(config)
    with os.fdopen(fd, "a", encoding="utf-8") as handle:
      handle.write(f"[{iso_timestamp()}] {message}\n")
  except (OSError, RuntimeError):  # RuntimeError: "~" with no home dir.
    pass
