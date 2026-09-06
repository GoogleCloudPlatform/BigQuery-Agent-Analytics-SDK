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

"""Trusted consumer: authenticate, re-verify, probe access, consume, render.

``consume`` is the only code path that may release a governed value. It
checks the stored receipt's MAC, key status and binding to the request and
audience; re-runs the independent verifier against fresh API evidence;
compares the fresh result commitment with the receipt; probes current
source/output access under the requester; atomically consumes the request
nonce; and only then formats the authoritative scalar. Every failure
returns a verdict and reason codes with no value or display at all.
"""

from __future__ import annotations

from typing import Any

import broker
import contracts
import receipt_store
import verify as verify_mod


def _fail(
    verdict: str,
    match: str,
    reasons: list[str],
    request_id: str,
    receipt_id: str,
) -> dict:
  return {
      "request_id": request_id,
      "receipt_id": receipt_id,
      "execution_match": match,
      "verdict": verdict,
      "reason_codes": list(reasons),
  }


def consume(
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
  """Gate and render one governed value; see module docstring.

  ``now`` is the entry timestamp. Deadlines are re-sampled from the trusted
  ``clock`` after every remote read and again inside the consumption
  transaction, so a request that expires mid-consume is rejected.
  """
  clock = contracts.trusted_clock(now, clock)
  if keys is None:
    return _fail(
        contracts.UNVERIFIABLE,
        contracts.UNKNOWN,
        ["signing_key_unavailable"],
        request_id,
        receipt_id,
    )
  request = registry.load_request(request_id)
  if request is None:
    return _fail(
        contracts.REJECTED,
        contracts.UNKNOWN,
        ["unknown_request"],
        request_id,
        receipt_id,
    )
  stored = store.get_receipt(receipt_id)
  if stored is None:
    return _fail(
        contracts.REJECTED,
        contracts.UNKNOWN,
        ["unknown_receipt"],
        request_id,
        receipt_id,
    )
  if stored.get("request_id") != request_id:
    return _fail(
        contracts.REJECTED,
        contracts.UNKNOWN,
        ["receipt_request_mismatch"],
        request_id,
        receipt_id,
    )

  # 1. Authenticity. Only a sealed receipt is consumable; the executor's
  # pending handle must first pass the independent verifier, and any other
  # shape (e.g. a sealed receipt with fields removed) is an integrity failure.
  if receipt_store.is_pending_handle(stored):
    return _fail(
        contracts.REJECTED,
        contracts.UNKNOWN,
        ["receipt_not_verified"],
        request_id,
        receipt_id,
    )
  sealed = True
  if sealed:
    problems = receipt_store.check_receipt_integrity(stored, keys)
    if problems:
      return _fail(
          contracts.REJECTED,
          contracts.UNKNOWN,
          problems,
          request_id,
          receipt_id,
      )
    if (
        stored["audience"] != contracts.AUDIENCE
        or stored["audience"] != request["audience"]
    ):
      return _fail(
          contracts.REJECTED,
          contracts.UNKNOWN,
          ["audience_mismatch"],
          request_id,
          receipt_id,
      )
    if stored["nonce"] != request["nonce"]:
      return _fail(
          contracts.REJECTED,
          contracts.UNKNOWN,
          ["nonce_mismatch"],
          request_id,
          receipt_id,
      )
    if now >= stored["expires_at"]:
      return _fail(
          contracts.REJECTED,
          contracts.UNKNOWN,
          ["request_expired"],
          request_id,
          receipt_id,
      )
    for key in (
        "publication_id",
        "context_ref",
        "computation_digest",
        "policy_version",
    ):
      if stored[key] != request[key]:
        return _fail(
            contracts.REJECTED,
            contracts.UNKNOWN,
            ["receipt_binding_mismatch"],
            request_id,
            receipt_id,
        )
    if stored["executed_artifact_hash"] != request["compiled_sql_digest"]:
      return _fail(
          contracts.REJECTED,
          contracts.MISMATCH,
          ["receipt_binding_mismatch"],
          request_id,
          receipt_id,
      )
    if stored["output_contract_digest"] != request["output_contract_digest"]:
      return _fail(
          contracts.REJECTED,
          contracts.MISMATCH,
          ["receipt_binding_mismatch"],
          request_id,
          receipt_id,
      )
    if stored["verdict"] != contracts.VERIFIED:
      return _fail(
          stored["verdict"],
          stored["execution_match"],
          list(stored["reason_codes"]) or ["receipt_not_verified"],
          request_id,
          receipt_id,
      )
    prior = stored
  else:
    prior = None

  # 2. Fresh, independent verification (never trusts the stored verdict).
  fresh = verify_mod.verify(
      request_id,
      receipt_id,
      claim,
      evidence_client,
      registry,
      store,
      now,
      keys=keys,
      trusted_bundle_dir=trusted_bundle_dir,
      clock=clock,
  )
  if fresh["verdict"] != contracts.VERIFIED or "value" not in fresh:
    return _fail(
        fresh["verdict"],
        fresh["execution_match"],
        fresh["reason_codes"],
        request_id,
        receipt_id,
    )
  receipt = fresh["receipt"]
  commit = receipt_store.commitments(keys, receipt["integrity_proof"]["key_id"])
  authoritative = {
      "field": fresh["field"],
      "value": fresh["value"],
      "unit": fresh["unit"],
  }

  # 3. Receipt binding: the fresh evidence must agree with the prior receipt.
  if prior is not None:
    for key in (
        "result_commitment",
        "executed_artifact_hash",
        "parameter_binding_commitment",
        "requester_commitment",
        "job",
    ):
      if prior[key] != receipt[key]:
        return _fail(
            contracts.REJECTED,
            contracts.MISMATCH,
            ["receipt_binding_mismatch"],
            request_id,
            receipt_id,
        )

  # 4. Claim binding against the keyed result commitment.
  try:
    claim_v = contracts.validate_claim(claim)
  except contracts.ContractError:
    return _fail(
        contracts.REJECTED,
        contracts.MATCH,
        ["claim_invalid"],
        request_id,
        receipt_id,
    )
  expected_commitment = commit(contracts.DOMAIN_COMMIT_RESULT, claim_v)
  if not contracts.constant_time_equal(
      expected_commitment, receipt["result_commitment"]
  ):
    return _fail(
        contracts.REJECTED,
        contracts.MATCH,
        ["display_mismatch"],
        request_id,
        receipt_id,
    )
  if claim_v != authoritative:
    return _fail(
        contracts.REJECTED,
        contracts.MATCH,
        ["display_mismatch"],
        request_id,
        receipt_id,
    )

  # 5. Current access under the requester (separate from proof checking).
  job = registry.load_job(request_id)
  probe = broker.probe_access(evidence_client, dict(request, job=job))
  if probe["outcome"] == broker.DENIED:
    return _fail(
        contracts.REJECTED,
        contracts.MATCH,
        ["access_denied"],
        request_id,
        receipt_id,
    )
  if probe["outcome"] != broker.ALLOWED:
    return _fail(
        contracts.UNVERIFIABLE,
        contracts.MATCH,
        ["access_unavailable"],
        request_id,
        receipt_id,
    )

  # 6. Render first so a formatting failure can never spend the nonce.
  try:
    display = contracts.money_display(
        authoritative["value"],
        authoritative["unit"],
        request["output"]["label"],
    )
  except contracts.ContractError:
    return _fail(
        contracts.UNVERIFIABLE,
        contracts.MATCH,
        ["render_failed"],
        request_id,
        receipt_id,
    )

  # 7. Deadline re-check after reads, probes and rendering, then atomic
  #    one-time consumption with the deadline enforced inside the transaction.
  deadline = request["expires_at"]
  if prior is not None:
    deadline = min(deadline, prior["expires_at"])
  if clock() >= deadline or broker.request_is_expired(request, clock()):
    return _fail(
        contracts.REJECTED,
        contracts.MATCH,
        ["request_expired"],
        request_id,
        receipt_id,
    )
  gate = registry.try_consume(
      request_id,
      request["nonce"],
      request["audience"],
      deadline=deadline,
      clock=clock,
  )
  if gate == "expired":
    return _fail(
        contracts.REJECTED,
        contracts.MATCH,
        ["request_expired"],
        request_id,
        receipt_id,
    )
  if gate != "consumed":
    return _fail(
        contracts.REJECTED,
        contracts.MATCH,
        ["request_consumed"],
        request_id,
        receipt_id,
    )

  # 8. Release only the authoritative value with the pinned field/unit.
  return {
      "request_id": request_id,
      "receipt_id": receipt_id,
      "execution_match": contracts.MATCH,
      "verdict": contracts.VERIFIED,
      "reason_codes": [],
      "field": authoritative["field"],
      "unit": authoritative["unit"],
      "value": authoritative["value"],
      "display": f"{display} · {contracts.VERIFIED}",
      "job": job,
      "access_probe": probe,
  }
