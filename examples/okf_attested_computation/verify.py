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

"""Independent verifier: bind execution evidence and the authoritative result.

``verify`` never receives the executor's SQL, result object or verdict. It
loads the approved request and registered job tuple from the private
registry, re-loads and re-compiles the trusted publication from pinned
bytes, then reads ``jobs.get`` and ``jobs.getQueryResults`` through an
evidence client confined to that one job. It compares the literal SQL,
typed bindings, actual job owner, dialect, cache flags, statement type and
referenced tables, validates the one-cell NUMERIC result shape with
``Decimal``, and only then issues a sealed receipt.

Verdicts: proven contradiction or conclusive denial -> REJECTED; missing,
incomplete or unreachable evidence -> UNVERIFIABLE; everything else must
match for VERIFIED. Failed outputs never carry a value.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import broker
import contracts
import publication as publication_mod
import receipt_store

MAX_RESULT_PAGES = 4


class EvidenceError(Exception):
  """Raised by evidence clients. ``code`` mirrors the HTTP status if any."""

  def __init__(self, message: str, code: int | None = None):
    super().__init__(message)
    self.code = code


class EvidenceUnavailable(EvidenceError):
  """Transport failure or method not available (never a denial)."""


class _Outcome(Exception):

  def __init__(self, verdict: str, match: str, reasons: list[str]):
    super().__init__(verdict)
    self.verdict, self.match, self.reasons = verdict, match, reasons


def _rejected(match: str, *reasons: str) -> _Outcome:
  return _Outcome(contracts.REJECTED, match, list(reasons))


def _unverifiable(match: str, *reasons: str) -> _Outcome:
  return _Outcome(contracts.UNVERIFIABLE, match, list(reasons))


def _read(fn, *args, what: str, match: str = contracts.UNKNOWN) -> Any:
  """Map evidence-client exceptions to verdicts (403 is a known denial)."""
  try:
    return fn(*args)
  except EvidenceUnavailable as exc:
    raise _unverifiable(match, f"{what}_unavailable") from exc
  except EvidenceError as exc:
    if exc.code == 404:
      raise _unverifiable(match, f"{what}_missing") from exc
    if exc.code == 403:
      raise _rejected(match, f"{what}_read_denied") from exc
    raise _unverifiable(match, f"{what}_unavailable") from exc


def _reload_publication(request: dict, trusted_bundle_dir: str | None) -> dict:
  bundle_dir = trusted_bundle_dir or request["bundle_dir"]
  try:
    manifest = publication_mod.load_manifest(
        Path(bundle_dir) / "publication.json"
    )
    pub = publication_mod.load_publication(bundle_dir, manifest)
  except (OSError, ValueError) as exc:
    raise _unverifiable(contracts.UNKNOWN, "publication_unavailable") from exc
  if pub["publication_digest"] != request["publication_digest"]:
    raise _rejected(contracts.MISMATCH, "publication_mutated")
  if pub["compiled_sql"] != request["compiled_sql"]:
    raise _rejected(contracts.MISMATCH, "publication_mutated")
  if pub["output_contract_digest"] != request["output_contract_digest"]:
    raise _rejected(contracts.MISMATCH, "publication_mutated")
  if sorted(pub["dependencies"]) != sorted(request["dependencies"]):
    raise _rejected(contracts.MISMATCH, "publication_mutated")
  return pub


def _check_job_resource(
    job: Any, tuple_: dict, request: dict, pub: dict
) -> dict:
  """Compare the authoritative Job resource with the approved request."""
  if not isinstance(job, dict):
    raise _unverifiable(contracts.UNKNOWN, "job_malformed")
  ref = job.get("jobReference") or {}
  if (
      ref.get("projectId") != tuple_["project"]
      or ref.get("location") != tuple_["location"]
      or ref.get("jobId") != tuple_["job_id"]
  ):
    raise _rejected(contracts.MISMATCH, "job_reference_mismatch")
  status = job.get("status") or {}
  if status.get("state") != "DONE":
    raise _unverifiable(contracts.UNKNOWN, "job_incomplete")
  if status.get("errorResult"):
    raise _unverifiable(contracts.UNKNOWN, "job_failed")
  owner = job.get("user_email")
  if not owner:
    raise _unverifiable(contracts.UNKNOWN, "owner_missing")
  if owner != request["requester"]:
    raise _rejected(contracts.MISMATCH, "owner_mismatch")
  cfg = (job.get("configuration") or {}).get("query")
  if not cfg or not isinstance(cfg, dict):
    raise _unverifiable(contracts.UNKNOWN, "sql_missing")
  if (job.get("configuration") or {}).get("dryRun"):
    raise _rejected(contracts.MISMATCH, "dry_run_job")
  literal = cfg.get("query")
  if not isinstance(literal, str) or not literal:
    raise _unverifiable(contracts.UNKNOWN, "sql_missing")
  if literal != pub["compiled_sql"]:
    raise _rejected(contracts.MISMATCH, "sql_mismatch")
  if "useLegacySql" not in cfg:
    raise _unverifiable(contracts.UNKNOWN, "dialect_missing")
  if cfg["useLegacySql"] is not False:
    raise _rejected(contracts.MISMATCH, "legacy_sql")
  if cfg.get("useQueryCache", True) is not False:
    raise _rejected(contracts.MISMATCH, "cache_not_disabled")
  bindings = cfg.get("queryParameters")
  if bindings is None:
    raise _unverifiable(contracts.UNKNOWN, "bindings_missing")
  actual: dict[str, tuple[str, Any]] = {}
  for p in bindings:
    name = p.get("name")
    ptype = (p.get("parameterType") or {}).get("type")
    value = (p.get("parameterValue") or {}).get("value")
    if not name or name in actual:
      raise _rejected(contracts.MISMATCH, "parameter_mismatch")
    actual[name] = (ptype, value)
  expected = {
      name: ("DATE", value) for name, value in request["parameters"].items()
  }
  if set(actual) != set(expected):
    raise _rejected(contracts.MISMATCH, "parameter_mismatch")
  for name, (ptype, value) in actual.items():
    if ptype != expected[name][0]:
      raise _rejected(contracts.MISMATCH, "parameter_mismatch")
    try:
      if contracts.date_string(value) != expected[name][1]:
        raise _rejected(contracts.MISMATCH, "parameter_mismatch")
    except contracts.ContractError as exc:
      raise _rejected(contracts.MISMATCH, "parameter_mismatch") from exc
  stats = (job.get("statistics") or {}).get("query") or {}
  if stats.get("cacheHit") is True:
    raise _rejected(contracts.MISMATCH, "cache_hit")
  stype = stats.get("statementType")
  if stype is None:
    raise _unverifiable(contracts.UNKNOWN, "statement_type_missing")
  if stype != "SELECT":
    raise _rejected(contracts.MISMATCH, "statement_type")
  refs = stats.get("referencedTables")
  if refs is None:
    raise _unverifiable(contracts.UNKNOWN, "dependencies_missing")
  referenced = sorted(
      f"{t.get('projectId')}.{t.get('datasetId')}.{t.get('tableId')}"
      for t in refs
  )
  if referenced != sorted(pub["dependencies"]):
    raise _rejected(contracts.MISMATCH, "dependency_mismatch")
  return {
      "owner": owner,
      "state": status["state"],
      "statement_type": stype,
      "cache_hit": stats.get("cacheHit"),
      "total_bytes_processed": stats.get("totalBytesProcessed"),
      "total_bytes_billed": stats.get("totalBytesBilled"),
      "total_slot_ms": (job.get("statistics") or {}).get("totalSlotMs"),
      "creation_time": (job.get("statistics") or {}).get("creationTime"),
      "end_time": (job.get("statistics") or {}).get("endTime"),
      "referenced_tables": referenced,
  }


def _read_result(evidence_client: Any, tuple_: dict, pub: dict) -> dict:
  """Fetch all pages of the original result and validate the one-cell shape."""
  page_token = None
  rows: list = []
  schema = None
  total_rows = None
  for _ in range(MAX_RESULT_PAGES):
    page = _read(
        evidence_client.get_query_results,
        tuple_["project"],
        tuple_["location"],
        tuple_["job_id"],
        page_token,
        what="result",
        match=contracts.MATCH,
    )
    if not isinstance(page, dict):
      raise _unverifiable(contracts.MATCH, "result_malformed")
    if page.get("jobComplete") is not True:
      raise _unverifiable(contracts.MATCH, "result_incomplete")
    if schema is None:
      schema = page.get("schema")
      total_rows = page.get("totalRows")
    rows.extend(page.get("rows") or [])
    page_token = page.get("pageToken")
    if not page_token:
      break
  else:
    raise _rejected(contracts.MATCH, "result_shape")
  if schema is None or total_rows is None:
    raise _unverifiable(contracts.MATCH, "result_evidence_missing")
  fields = schema.get("fields") or []
  out = pub["output"]
  if (
      len(fields) != 1
      or fields[0].get("name") != out["field"]
      or fields[0].get("type") != out["type"]
      or fields[0].get("mode", "NULLABLE") == "REPEATED"
  ):
    raise _rejected(contracts.MATCH, "schema_mismatch")
  try:
    if int(total_rows) != 1 or len(rows) != 1:
      raise _rejected(contracts.MATCH, "result_shape")
  except (TypeError, ValueError) as exc:
    raise _unverifiable(contracts.MATCH, "result_malformed") from exc
  cells = rows[0].get("f") if isinstance(rows[0], dict) else None
  if not isinstance(cells, list) or len(cells) != 1:
    raise _rejected(contracts.MATCH, "result_shape")
  cell = cells[0].get("v") if isinstance(cells[0], dict) else None
  if cell is None:
    raise _rejected(contracts.MATCH, "result_null")
  if not isinstance(cell, str):
    raise _rejected(contracts.MATCH, "result_shape")
  try:
    value = contracts.decimal_string(cell)
  except contracts.ContractError as exc:
    raise _rejected(contracts.MATCH, "result_shape") from exc
  return {"field": out["field"], "value": value, "unit": out["unit"]}


def _build_receipt(
    receipt_id: str,
    request: dict,
    tuple_: dict,
    match: str,
    verdict: str,
    reasons: list[str],
    result: dict | None,
    details: dict,
    keys: receipt_store.KeyStore,
    now: int,
) -> dict:
  commit = receipt_store.commitments(keys)
  receipt = {
      "receipt_version": contracts.RECEIPT_VERSION,
      "profile_contract_version": contracts.PROFILE_CONTRACT_VERSION,
      "canonicalization_version": contracts.CANONICALIZATION_VERSION,
      "receipt_id": receipt_id,
      "request_id": request["request_id"],
      "context_ref": request["context_ref"],
      "publication_id": request["publication_id"],
      "computation_digest": request["computation_digest"],
      "job": dict(tuple_),
      "requester_commitment": commit(
          contracts.DOMAIN_COMMIT_REQUESTER, request["requester"]
      ),
      "audience": request["audience"],
      "nonce": request["nonce"],
      "issued_at": now,
      "expires_at": request["expires_at"],
      "executed_artifact_hash": request["compiled_sql_digest"],
      "parameter_binding_commitment": commit(
          contracts.DOMAIN_COMMIT_PARAMS, request["parameters"]
      ),
      "result_commitment": (
          commit(contracts.DOMAIN_COMMIT_RESULT, result) if result else ""
      ),
      "output_contract_digest": request["output_contract_digest"],
      "policy_version": request["policy_version"],
      "attester_artifact_hash": publication_mod.attester_artifact_hash(),
      "execution_match": match,
      "verdict": verdict,
      "reason_codes": list(reasons),
      "details_digest": contracts.digest(contracts.DOMAIN_DETAILS, details),
  }
  return receipt_store.seal_receipt(receipt, keys)


def verify(
    request_id: str,
    receipt_id: str,
    claim: dict,
    evidence_client: Any,
    registry: Any,
    store: Any,
    now: int,
    *,
    keys: receipt_store.KeyStore | None = None,
    trusted_bundle_dir: str | None = None,
    clock: Any = None,
) -> dict:
  """Independently verify a registered execution and bind the claim.

  ``store`` is the receipt store (may be the same object as ``registry``).
  ``keys`` is the verifier-owned :class:`KeyStore`; when omitted no receipt
  can be sealed and the verdict is UNVERIFIABLE. ``now`` is the entry
  timestamp; the request deadline is re-checked against the trusted
  ``clock`` after the remote reads so a slow read cannot carry an expired
  request into VERIFIED.
  """
  clock = contracts.trusted_clock(now, clock)
  match = contracts.UNKNOWN
  reasons: list[str] = []
  verdict = contracts.UNVERIFIABLE
  result: dict | None = None
  details: dict[str, Any] = {"request_id": request_id, "receipt_id": receipt_id}
  request = registry.load_request(request_id)
  tuple_ = registry.load_job(request_id) if request else None
  try:
    if request is None:
      raise _rejected(contracts.UNKNOWN, "unknown_request")
    handle = store.get_receipt(receipt_id)
    if handle is None:
      raise _rejected(contracts.UNKNOWN, "unknown_receipt")
    if handle.get("request_id") != request_id:
      raise _rejected(contracts.UNKNOWN, "receipt_request_mismatch")
    try:
      claim = contracts.validate_claim(claim)
    except contracts.ContractError as exc:
      raise _rejected(contracts.UNKNOWN, "claim_invalid") from exc
    if keys is None or keys.current_key_id is None:
      raise _unverifiable(contracts.UNKNOWN, "signing_key_unavailable")
    if broker.request_is_expired(request, now):
      raise _rejected(contracts.UNKNOWN, "request_expired")
    if registry.is_consumed(request_id):
      raise _rejected(contracts.UNKNOWN, "request_consumed")
    pub = _reload_publication(request, trusted_bundle_dir)
    if tuple_ is None:
      raise _unverifiable(contracts.UNKNOWN, "job_not_registered")
    job = _read(
        evidence_client.get_job,
        tuple_["project"],
        tuple_["location"],
        tuple_["job_id"],
        what="job",
    )
    details["job"] = _check_job_resource(job, tuple_, request, pub)
    match = contracts.MATCH
    authoritative = _read_result(evidence_client, tuple_, pub)
    details["result_schema"] = [pub["output"]["field"], pub["output"]["type"]]
    if broker.request_is_expired(request, clock()):
      raise _rejected(contracts.MATCH, "request_expired")
    result = authoritative
    verdict = contracts.VERIFIED
  except _Outcome as outcome:
    verdict, match, reasons = outcome.verdict, outcome.match, outcome.reasons

  # The sealed receipt records the evidence verdict only. The display claim
  # is bound below and reported to the caller, but a wrong claim never
  # overwrites or downgrades an authentic VERIFIED proof.
  out: dict[str, Any] = {
      "request_id": request_id,
      "receipt_id": receipt_id,
      "execution_match": match,
      "verdict": verdict,
      "reason_codes": reasons,
  }
  out = _finish(
      out,
      request,
      tuple_,
      keys,
      match,
      verdict,
      reasons,
      result,
      details,
      store,
      receipt_id,
      now,
  )
  if out["verdict"] == contracts.VERIFIED and result is not None:
    claim_ok = (
        isinstance(claim, dict)
        and claim.get("field") == result["field"]
        and claim.get("unit") == result["unit"]
        and contracts.constant_time_equal(
            str(claim.get("value")), result["value"]
        )
    )
    if not claim_ok:
      out["verdict"] = contracts.REJECTED
      out["reason_codes"] = ["display_mismatch"]
      for key in ("value", "field", "unit", "label"):
        out.pop(key, None)
  return out


def _may_store(stored: Any, keys: receipt_store.KeyStore) -> bool:
  """Only a pending handle or an authentic non-VERIFIED receipt is replaced.

  An authentic VERIFIED receipt is never downgraded, and a record that
  fails integrity is kept as tamper evidence rather than silently reissued.
  """
  if stored is None or receipt_store.is_pending_handle(stored):
    return True
  if receipt_store.check_receipt_integrity(stored, keys):
    return False
  return stored["verdict"] != contracts.VERIFIED


def _finish(
    out: dict,
    request: dict | None,
    tuple_: dict | None,
    keys: receipt_store.KeyStore | None,
    match: str,
    verdict: str,
    reasons: list[str],
    result: dict | None,
    details: dict,
    store: Any,
    receipt_id: str,
    now: int,
) -> dict:
  """Seal and store a receipt when the request/job are known; return output."""
  if (
      request is not None
      and tuple_ is not None
      and keys is not None
      and keys.current_key_id is not None
  ):
    try:
      receipt = _build_receipt(
          receipt_id,
          request,
          tuple_,
          match,
          verdict,
          reasons,
          result,
          details,
          keys,
          now,
      )
      if _may_store(store.get_receipt(receipt_id), keys):
        store.put_receipt(receipt_id, receipt)
      out["receipt"] = receipt
    except contracts.ContractError as exc:
      out["verdict"] = contracts.UNVERIFIABLE
      out["reason_codes"] = list(reasons) + [f"receipt_seal_failed:{exc}"]
      return out
  if verdict == contracts.VERIFIED and result is not None:
    out["value"] = result["value"]
    out["field"] = result["field"]
    out["unit"] = result["unit"]
    out["label"] = request["output"]["label"] if request else ""
  return out


class BigQueryEvidenceClient:
  """Raw ``jobs.get`` / ``jobs.getQueryResults`` reads via the REST API.

  Uses the delegated client's connection so the principal is the broker's
  requester. Evidence reads are confined to the (project, location, job_id)
  passed in by the verifier; the only other method is the dry-run access
  probe used by the consumer.
  """

  def __init__(self, client: Any):
    self._client = client

  def _call(self, path: str, params: dict) -> dict:
    from google.api_core import exceptions

    try:
      return self._client._connection.api_request(  # pylint: disable=protected-access
          method="GET", path=path, query_params=params
      )
    except exceptions.NotFound as exc:
      raise EvidenceError(str(exc), 404) from exc
    except exceptions.Forbidden as exc:
      raise EvidenceError(str(exc), 403) from exc
    except exceptions.GoogleAPICallError as exc:
      raise EvidenceUnavailable(str(exc), exc.code) from exc
    except Exception as exc:  # pylint: disable=broad-except
      raise EvidenceUnavailable(str(exc)) from exc

  def get_job(self, project: str, location: str, job_id: str) -> dict:
    return self._call(
        f"/projects/{project}/jobs/{job_id}", {"location": location}
    )

  def get_query_results(
      self, project: str, location: str, job_id: str, page_token: str | None
  ) -> dict:
    params: dict[str, Any] = {"location": location, "maxResults": 1000}
    if page_token:
      params["pageToken"] = page_token
    return self._call(f"/projects/{project}/queries/{job_id}", params)

  # -- current-access probes (broker.probe_access) -------------------------

  def probe_sources(self, sql: str, parameters: dict) -> None:
    from google.cloud import bigquery

    cfg = bigquery.QueryJobConfig(
        dry_run=True,
        use_query_cache=False,
        use_legacy_sql=False,
        query_parameters=[
            bigquery.ScalarQueryParameter(k, "DATE", v)
            for k, v in parameters.items()
        ],
    )
    self._client.query(sql, job_config=cfg)

  def probe_output(self, job: dict | None) -> None:
    if not job:
      raise contracts.ContractError("no registered job")
    self.get_job(job["project"], job["location"], job["job_id"])


class MetadataOnlyEvidenceClient:
  """Wrapper that can read job metadata but has no result-read method.

  Models the historical metadata-only attester; fidelity cannot pass.
  """

  def __init__(self, inner: Any):
    self._inner = inner

  def get_job(self, project: str, location: str, job_id: str) -> dict:
    return self._inner.get_job(project, location, job_id)

  def get_query_results(self, *args: Any) -> dict:
    raise EvidenceUnavailable("result read not available to this verifier")

  def probe_sources(self, sql: str, parameters: dict) -> None:
    self._inner.probe_sources(sql, parameters)

  def probe_output(self, job: dict | None) -> None:
    self._inner.probe_output(job)
