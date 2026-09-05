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

"""Private registry (SQLite) and local MAC key custody.

The registry stores approved requests, registered job tuples, receipts and
atomic nonce-consumption state. ``KeyStore`` keeps two independent 256-bit
keys (commitment and integrity) in an operator-private directory with mode
0600; the agent and executor never receive key bytes. This is a local MAC
profile, not a portable third-party signature.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import sqlite3
import threading
import time
from typing import Any

import contracts

_SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
  request_id TEXT PRIMARY KEY,
  payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs (
  request_id TEXT PRIMARY KEY,
  payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS receipts (
  receipt_id TEXT PRIMARY KEY,
  request_id TEXT NOT NULL,
  payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS consumption (
  request_id TEXT NOT NULL,
  nonce TEXT NOT NULL,
  audience TEXT NOT NULL,
  consumed_at INTEGER NOT NULL,
  PRIMARY KEY (request_id)
);
"""


class Registry:
  """SQLite-backed private request/job/receipt registry."""

  def __init__(self, path: str | Path = ":memory:"):
    self._path = str(path)
    if self._path != ":memory:":
      parent = Path(self._path).parent
      parent.mkdir(parents=True, exist_ok=True)
    self._conn = sqlite3.connect(
        self._path, check_same_thread=False, isolation_level=None
    )
    self._lock = threading.Lock()
    with self._lock:
      self._conn.executescript(_SCHEMA)
    if self._path != ":memory:":
      os.chmod(self._path, 0o600)

  # -- requests ------------------------------------------------------------

  def save_request(self, request: dict) -> None:
    with self._lock:
      self._conn.execute(
          "INSERT INTO requests(request_id, payload) VALUES (?, ?)",
          (request["request_id"], json.dumps(request, sort_keys=True)),
      )

  def load_request(self, request_id: str) -> dict | None:
    with self._lock:
      row = self._conn.execute(
          "SELECT payload FROM requests WHERE request_id = ?", (request_id,)
      ).fetchone()
    return json.loads(row[0]) if row else None

  # -- jobs ----------------------------------------------------------------

  def register_job(self, request_id: str, job: dict) -> None:
    job = contracts.validate_job_tuple(job)
    with self._lock:
      existing = self._conn.execute(
          "SELECT payload FROM jobs WHERE request_id = ?", (request_id,)
      ).fetchone()
      if existing and json.loads(existing[0]) != job:
        raise contracts.ContractError(
            "a different job is already registered for this request"
        )
      self._conn.execute(
          "INSERT OR REPLACE INTO jobs(request_id, payload) VALUES (?, ?)",
          (request_id, json.dumps(job, sort_keys=True)),
      )

  def load_job(self, request_id: str) -> dict | None:
    with self._lock:
      row = self._conn.execute(
          "SELECT payload FROM jobs WHERE request_id = ?", (request_id,)
      ).fetchone()
    return json.loads(row[0]) if row else None

  # -- receipts ------------------------------------------------------------

  def put_receipt(self, receipt_id: str, payload: dict) -> None:
    with self._lock:
      self._conn.execute(
          "INSERT OR REPLACE INTO receipts(receipt_id, request_id, payload)"
          " VALUES (?, ?, ?)",
          (
              receipt_id,
              payload["request_id"],
              json.dumps(payload, sort_keys=True),
          ),
      )

  def get_receipt(self, receipt_id: str) -> dict | None:
    with self._lock:
      row = self._conn.execute(
          "SELECT payload FROM receipts WHERE receipt_id = ?", (receipt_id,)
      ).fetchone()
    return json.loads(row[0]) if row else None

  # -- consumption ---------------------------------------------------------

  def consume_once(self, request_id: str, nonce: str, audience: str) -> bool:
    """Atomically mark a request consumed. True exactly once per request."""
    with self._lock:
      try:
        self._conn.execute("BEGIN IMMEDIATE")
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO consumption"
            "(request_id, nonce, audience, consumed_at) VALUES (?, ?, ?, ?)",
            (request_id, nonce, audience, int(time.time())),
        )
        self._conn.execute("COMMIT")
      except Exception:
        self._conn.execute("ROLLBACK")
        raise
    return cur.rowcount == 1

  def is_consumed(self, request_id: str) -> bool:
    with self._lock:
      row = self._conn.execute(
          "SELECT 1 FROM consumption WHERE request_id = ?", (request_id,)
      ).fetchone()
    return row is not None

  def close(self) -> None:
    with self._lock:
      self._conn.close()


class KeyStore:
  """Two independent 256-bit MAC keys in an operator-private directory."""

  def __init__(self, directory: str | Path):
    self._dir = Path(directory)
    self._dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(self._dir, 0o700)
    self._meta_path = self._dir / "keys.json"
    if self._meta_path.exists():
      self._meta = json.loads(self._meta_path.read_text(encoding="utf-8"))
    else:
      self._meta = {"keys": {}, "revoked": []}
      self.rotate()

  # -- lifecycle -----------------------------------------------------------

  def rotate(self) -> str:
    key_id = "k-" + secrets.token_hex(8)
    for role in ("commit", "integrity"):
      path = self._dir / f"{key_id}.{role}.key"
      fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
      with os.fdopen(fd, "wb") as fh:
        fh.write(secrets.token_bytes(32))
    self._meta["keys"][key_id] = {"created_at": int(time.time())}
    self._meta["current"] = key_id
    self._save()
    return key_id

  def revoke(self, key_id: str) -> None:
    if key_id in self._meta["keys"] and key_id not in self._meta["revoked"]:
      self._meta["revoked"].append(key_id)
      self._save()

  def erase(self, key_id: str) -> None:
    """Erase key bytes; retained receipts become unverifiable evidence."""
    for role in ("commit", "integrity"):
      path = self._dir / f"{key_id}.{role}.key"
      if path.exists():
        path.write_bytes(b"\x00" * 32)
        path.unlink()
    self._meta["keys"].pop(key_id, None)
    if self._meta.get("current") == key_id:
      self._meta["current"] = None
    self._save()

  def _save(self) -> None:
    self._meta_path.write_text(
        json.dumps(self._meta, sort_keys=True), encoding="utf-8"
    )
    os.chmod(self._meta_path, 0o600)

  # -- access --------------------------------------------------------------

  @property
  def current_key_id(self) -> str | None:
    return self._meta.get("current")

  def status(self, key_id: str) -> str:
    if key_id in self._meta["revoked"]:
      return "revoked"
    if key_id not in self._meta["keys"]:
      return "unknown"
    for role in ("commit", "integrity"):
      if not (self._dir / f"{key_id}.{role}.key").exists():
        return "erased"
    return "active"

  def _read(self, key_id: str, role: str) -> bytes:
    if self.status(key_id) != "active":
      raise contracts.ContractError(f"key {key_id} is {self.status(key_id)}")
    raw = (self._dir / f"{key_id}.{role}.key").read_bytes()
    if len(raw) != 32:
      raise contracts.ContractError("corrupt key material")
    return raw

  def commit_key(self, key_id: str | None = None) -> tuple[str, bytes]:
    key_id = key_id or self.current_key_id
    if key_id is None:
      raise contracts.ContractError("no current key")
    return key_id, self._read(key_id, "commit")

  def integrity_key(self, key_id: str | None = None) -> tuple[str, bytes]:
    key_id = key_id or self.current_key_id
    if key_id is None:
      raise contracts.ContractError("no current key")
    return key_id, self._read(key_id, "integrity")


# --------------------------------------------------------------------------
# Receipt MAC helpers
# --------------------------------------------------------------------------


def seal_receipt(receipt: dict, keys: KeyStore) -> dict:
  """Attach ``integrity_proof`` computed with the current integrity key."""
  key_id, key = keys.integrity_key()
  payload = contracts.receipt_payload(receipt)
  mac = contracts.commit(key, contracts.DOMAIN_INTEGRITY, payload)
  sealed = dict(payload)
  sealed["integrity_proof"] = {
      "algorithm": contracts.MAC_ALGORITHM,
      "key_id": key_id,
      "mac": mac,
  }
  contracts.validate_receipt_shape(sealed)
  return sealed


def check_receipt_integrity(receipt: Any, keys: KeyStore) -> list[str]:
  """Return reason codes; empty means the receipt is structurally authentic."""
  try:
    contracts.validate_receipt_shape(receipt)
  except contracts.ContractError as exc:
    return [f"receipt_shape:{exc}"]
  proof = receipt["integrity_proof"]
  status = keys.status(proof["key_id"])
  if status != "active":
    return [f"receipt_key_{status}"]
  _, key = keys.integrity_key(proof["key_id"])
  expected = contracts.commit(
      key, contracts.DOMAIN_INTEGRITY, contracts.receipt_payload(receipt)
  )
  if not contracts.constant_time_equal(expected, str(proof["mac"])):
    return ["receipt_integrity_failed"]
  return []


def commitments(keys: KeyStore, key_id: str | None = None) -> Any:
  """Return a callable ``(domain, value) -> hex`` bound to the commit key."""
  _, key = keys.commit_key(key_id)
  return lambda domain, value: contracts.commit(key, domain, value)
