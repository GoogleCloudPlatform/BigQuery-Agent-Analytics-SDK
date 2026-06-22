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

"""Shared helpers for serialization, hashing, timestamps, file locking, and
log emission. No dependencies on other package modules so this can be safely
imported from anywhere in the package."""

from __future__ import annotations

import contextlib
from datetime import datetime
from datetime import timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import time
from typing import Any, Iterator, TYPE_CHECKING
import uuid

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


def log_to_file(config: "BQAAConfig", message: str) -> None:
  """Append a single log line to ``config.log_file``. Silent on failure.

  The drainer and the spool writer share this — debugging signal lives in
  one place, and a broken log path never crashes the agent's hot path.
  """
  if not config.log_file:
    return
  try:
    with open(config.log_file, "a", encoding="utf-8") as handle:
      handle.write(f"[{iso_timestamp()}] {message}\n")
  except OSError:
    pass
