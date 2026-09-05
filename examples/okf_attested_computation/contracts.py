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

"""Validated dictionaries, canonical encodings and verdict vocabulary.

Everything here is stdlib only and makes no network calls. The canonical
CBOR encoder is a restricted copy of the PR 474 adapter encoder
(``examples/okf_bqaa_adapter/adapter.py``); ``fixtures/cbor_vectors.json``
pins its output so the two cannot drift silently.
"""

from __future__ import annotations

import datetime
from decimal import Decimal
from decimal import InvalidOperation
import hashlib
import hmac
import re
from typing import Any
import unicodedata

RECEIPT_VERSION = "okf-receipt-spike/v1"
PROFILE_CONTRACT_VERSION = "okf-context/1"
CANONICALIZATION_VERSION = "receipt-cbor/v1"
COMPILER_VERSION = "okf-receipt-compiler/v1"
MAC_ALGORITHM = "HMAC-SHA256"
AUDIENCE = "okf-receipt-demo-cli/v1"
REQUEST_TTL_SECONDS = 300

# Verdicts (per spec section 5).
VERIFIED = "VERIFIED"
UNVERIFIABLE = "UNVERIFIABLE"
REJECTED = "REJECTED"
VERDICTS = (VERIFIED, UNVERIFIABLE, REJECTED)

# execution_match values.
MATCH = "MATCH"
MISMATCH = "MISMATCH"
UNKNOWN = "UNKNOWN"

# Hash / MAC domains. Every digest is domain || 0x00 || cbor(value).
DOMAIN_PUBLICATION = "okf-receipt:publication"
DOMAIN_COMPUTATION = "okf-receipt:computation-bytes"
DOMAIN_SQL = "okf-receipt:compiled-sql"
DOMAIN_OUTPUT = "okf-receipt:output-contract"
DOMAIN_REQUEST = "okf-receipt:request"
DOMAIN_DETAILS = "okf-receipt:details"
DOMAIN_ATTESTER = "okf-receipt:attester-artifact"
DOMAIN_COMMIT_REQUESTER = "okf-receipt:commit:requester"
DOMAIN_COMMIT_PARAMS = "okf-receipt:commit:parameters"
DOMAIN_COMMIT_RESULT = "okf-receipt:commit:result"
DOMAIN_INTEGRITY = "okf-receipt:integrity"

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

# Public receipt projection: field order is informational; CBOR sorts keys.
RECEIPT_FIELDS = (
    "receipt_version",
    "profile_contract_version",
    "canonicalization_version",
    "receipt_id",
    "request_id",
    "context_ref",
    "publication_id",
    "computation_digest",
    "job",
    "requester_commitment",
    "audience",
    "nonce",
    "issued_at",
    "expires_at",
    "executed_artifact_hash",
    "parameter_binding_commitment",
    "result_commitment",
    "output_contract_digest",
    "policy_version",
    "attester_artifact_hash",
    "execution_match",
    "verdict",
    "reason_codes",
    "details_digest",
    "integrity_proof",
)


class ContractError(ValueError):
  """A validated dictionary failed its contract."""


# --------------------------------------------------------------------------
# Canonical CBOR (restricted domain) and domain-separated digests
# --------------------------------------------------------------------------


def _head(major: int, arg: int) -> bytes:
  if arg < 24:
    return bytes([(major << 5) | arg])
  for ai, size in ((24, 1), (25, 2), (26, 4), (27, 8)):
    if arg < (1 << (8 * size)):
      return bytes([(major << 5) | ai]) + arg.to_bytes(size, "big")
  raise ContractError("length too large")


def cbor(obj: Any) -> bytes:
  """Canonical CBOR (RFC 8949 4.2.1) over the receipt value domain.

  Accepted: string-keyed maps, lists/tuples, bool, non-negative int, None,
  str. Floats, bytes, negative ints and non-string keys are rejected so a
  binary float can never leak into a commitment.
  """
  if obj is False:
    return b"\xf4"
  if obj is True:
    return b"\xf5"
  if obj is None:
    return b"\xf6"
  if isinstance(obj, bool):  # pragma: no cover - handled above
    raise ContractError("bool")
  if isinstance(obj, int):
    if obj < 0:
      raise ContractError("negative integers are outside the receipt domain")
    return _head(0, obj)
  if isinstance(obj, float):
    raise ContractError("floats are outside the receipt domain")
  if isinstance(obj, bytes):
    raise ContractError("bytes are outside the receipt domain")
  if isinstance(obj, str):
    b = unicodedata.normalize("NFC", obj).encode("utf-8")
    return _head(3, len(b)) + b
  if isinstance(obj, (list, tuple)):
    return _head(4, len(obj)) + b"".join(cbor(x) for x in obj)
  if isinstance(obj, dict):
    for k in obj:
      if not isinstance(k, str):
        raise ContractError("map keys must be strings")
    items = sorted((cbor(k), cbor(v)) for k, v in obj.items())
    return _head(5, len(items)) + b"".join(k + v for k, v in items)
  raise ContractError(f"unsupported type {type(obj).__name__}")


def digest(domain: str, obj: Any) -> str:
  """Domain-separated SHA-256 hex over canonical CBOR."""
  return hashlib.sha256(
      domain.encode("ascii") + b"\x00" + cbor(obj)
  ).hexdigest()


def digest_bytes(domain: str, raw: bytes) -> str:
  """Domain-separated SHA-256 hex over raw artifact bytes (no CBOR)."""
  return hashlib.sha256(domain.encode("ascii") + b"\x00" + raw).hexdigest()


def commit(key: bytes, domain: str, obj: Any) -> str:
  """Keyed commitment: HMAC(K, domain || 0x00 || cbor(value))."""
  if not isinstance(key, bytes) or len(key) < 32:
    raise ContractError("commitment key must be >= 32 bytes")
  msg = domain.encode("ascii") + b"\x00" + cbor(obj)
  return hmac.new(key, msg, hashlib.sha256).hexdigest()


def constant_time_equal(a: str, b: str) -> bool:
  return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


# --------------------------------------------------------------------------
# Scalar encodings
# --------------------------------------------------------------------------


def decimal_string(value: Any) -> str:
  """Exact normalized decimal string: ``400`` not ``400.00`` or ``4E+2``.

  Accepts str, int or Decimal. Floats are rejected outright.
  """
  if isinstance(value, bool) or isinstance(value, float):
    raise ContractError("NUMERIC values must not be floats or bools")
  if isinstance(value, int):
    value = Decimal(value)
  elif isinstance(value, str):
    if not value or value != value.strip():
      raise ContractError("NUMERIC string is empty or padded")
    try:
      value = Decimal(value)
    except InvalidOperation as exc:
      raise ContractError(f"not a decimal: {value!r}") from exc
  elif not isinstance(value, Decimal):
    raise ContractError(f"unsupported NUMERIC type {type(value).__name__}")
  if not value.is_finite():
    raise ContractError("NUMERIC must be finite")
  norm = value.normalize()
  sign, digits, exp = norm.as_tuple()
  if exp > 0:
    norm = norm.quantize(Decimal(1))
  text = format(norm, "f")
  if text in ("-0", "-0.0"):
    text = "0"
  return text


def date_string(value: Any) -> str:
  """ISO date string; rejects datetimes, non-ISO forms and impossible dates."""
  if isinstance(value, datetime.datetime):
    raise ContractError("DATE parameters must not carry a time component")
  if isinstance(value, datetime.date):
    return value.isoformat()
  if not isinstance(value, str) or not _DATE_RE.match(value):
    raise ContractError(f"not an ISO date: {value!r}")
  try:
    datetime.date.fromisoformat(value)
  except ValueError as exc:
    raise ContractError(f"impossible date: {value!r}") from exc
  return value


def money_display(value: str, unit: str, label: str) -> str:
  """Render ``Gross margin: $400.00 USD`` from a canonical decimal string."""
  amount = Decimal(value).quantize(Decimal("0.01"))
  sign = "-" if amount < 0 else ""
  return f"{label}: {sign}${abs(amount):,.2f} {unit}"


# --------------------------------------------------------------------------
# Validated dictionaries
# --------------------------------------------------------------------------


def require_keys(obj: Any, required: tuple[str, ...], what: str) -> dict:
  if not isinstance(obj, dict):
    raise ContractError(f"{what} must be a mapping")
  missing = [k for k in required if k not in obj]
  if missing:
    raise ContractError(f"{what} missing {missing}")
  return obj


def validate_claim(claim: Any) -> dict:
  """A display claim proposed by the agent: field, decimal value, unit."""
  claim = require_keys(claim, ("field", "value", "unit"), "claim")
  extra = sorted(set(claim) - {"field", "value", "unit"})
  if extra:
    raise ContractError(f"claim carries undeclared keys {extra}")
  field = claim["field"]
  unit = claim["unit"]
  if not isinstance(field, str) or not _NAME_RE.match(field):
    raise ContractError("claim field must be a lowercase identifier")
  if not isinstance(unit, str) or not unit.isupper() or len(unit) != 3:
    raise ContractError("claim unit must be a three-letter code")
  return {"field": field, "value": decimal_string(claim["value"]), "unit": unit}


def validate_job_tuple(job: Any) -> dict:
  job = require_keys(job, ("project", "location", "job_id"), "job")
  for k in ("project", "location", "job_id"):
    if not isinstance(job[k], str) or not job[k]:
      raise ContractError(f"job.{k} must be a non-empty string")
  return {k: job[k] for k in ("project", "location", "job_id")}


def validate_receipt_shape(receipt: Any) -> dict:
  """Structural check of a public receipt projection (no MAC check here)."""
  receipt = require_keys(receipt, RECEIPT_FIELDS, "receipt")
  extra = sorted(set(receipt) - set(RECEIPT_FIELDS))
  if extra:
    raise ContractError(f"receipt carries undeclared keys {extra}")
  if receipt["receipt_version"] != RECEIPT_VERSION:
    raise ContractError("unsupported receipt_version")
  if receipt["profile_contract_version"] != PROFILE_CONTRACT_VERSION:
    raise ContractError("unsupported profile_contract_version")
  if receipt["canonicalization_version"] != CANONICALIZATION_VERSION:
    raise ContractError("unsupported canonicalization_version")
  if receipt["verdict"] not in VERDICTS:
    raise ContractError("unknown verdict")
  if receipt["execution_match"] not in (MATCH, MISMATCH, UNKNOWN):
    raise ContractError("unknown execution_match")
  validate_job_tuple(receipt["job"])
  proof = require_keys(
      receipt["integrity_proof"], ("algorithm", "key_id", "mac"), "proof"
  )
  if proof["algorithm"] != MAC_ALGORITHM:
    raise ContractError("unsupported integrity algorithm")
  if not isinstance(receipt["reason_codes"], list):
    raise ContractError("reason_codes must be a list")
  for ts in ("issued_at", "expires_at"):
    if not isinstance(receipt[ts], int) or isinstance(receipt[ts], bool):
      raise ContractError(f"{ts} must be an integer timestamp")
  return receipt


def receipt_payload(receipt: dict) -> dict:
  """The receipt with ``integrity_proof`` removed, for MAC computation."""
  return {k: v for k, v in receipt.items() if k != "integrity_proof"}
