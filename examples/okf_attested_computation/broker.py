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

"""Trusted session broker: authenticated requester, approved requests, probes.

A ``session`` is created here from the actual credential and is never
accepted from the model. ``approve_request`` validates the agent's typed
parameter choice against the pinned publication, mints the nonce, audience
and expiry itself, and stores the request before anything executes.
Credential bytes stay inside the session object's private delegation
callable; they are never serialized.
"""

from __future__ import annotations

import secrets
import time
from typing import Any, Callable

import contracts
import publication as publication_mod

RESERVED_PARAMETER_NAMES = frozenset(
    {
        "sql",
        "query",
        "requester",
        "user",
        "user_email",
        "identity",
        "job_id",
        "job",
        "nonce",
        "audience",
        "expires_at",
        "publication",
        "verdict",
        "receipt",
        "key",
    }
)

# Access probe outcomes (spec section 4).
ALLOWED = "ALLOWED"
DENIED = "DENIED"
UNAVAILABLE = "UNAVAILABLE"


class Session(dict):
  """Broker-created session. The delegation callable is private state."""

  def __init__(
      self,
      authenticated_requester: str,
      principal_kind: str,
      delegation: Callable[[], Any],
      session_id: str | None = None,
  ):
    super().__init__(
        authenticated_requester=authenticated_requester,
        principal_kind=principal_kind,
        session_id=session_id or "sess-" + secrets.token_hex(8),
        created_at=int(time.time()),
    )
    self._delegation = delegation

  def delegated_client(self) -> Any:
    """Return a caller-delegated client; never exposes credential bytes."""
    return self._delegation()

  def __reduce__(self):  # pragma: no cover - defensive
    raise TypeError("sessions are not serializable")


def open_hermetic_session(
    authenticated_requester: str, client_factory: Callable[[], Any]
) -> Session:
  """Session for hermetic runs; the factory returns an emulated client."""
  return Session(authenticated_requester, "hermetic", client_factory)


def open_live_session(project: str, location: str) -> Session:
  """Session from Application Default Credentials (network call).

  The requester email is established from the credential's tokeninfo, not
  from any agent argument. Import is local so the module stays importable
  without google-cloud-bigquery.
  """
  import json
  import urllib.request

  import google.auth
  import google.auth.transport.requests
  from google.cloud import bigquery

  scopes = ["https://www.googleapis.com/auth/cloud-platform"]
  creds, _ = google.auth.default(scopes=scopes)
  creds.refresh(google.auth.transport.requests.Request())
  info = json.load(
      urllib.request.urlopen(
          "https://oauth2.googleapis.com/tokeninfo?access_token=" + creds.token
      )
  )
  email = info.get("email")
  if not email:
    raise contracts.ContractError("credential has no verified email")
  kind = "service_account" if email.endswith("gserviceaccount.com") else "user"

  def _factory() -> Any:
    return bigquery.Client(
        project=project, location=location, credentials=creds
    )

  return Session(email, kind, _factory)


def open_impersonated_session(
    project: str, location: str, target_principal: str
) -> Session:
  """Session for a real restricted principal via IAM impersonation.

  Used only by negative tests (R8/R9). The identity is taken from the
  target principal string that IAM actually honours, never from a label.
  """
  import google.auth
  from google.auth import impersonated_credentials
  import google.auth.transport.requests
  from google.cloud import bigquery

  scopes = ["https://www.googleapis.com/auth/cloud-platform"]
  source, _ = google.auth.default(scopes=scopes)
  creds = impersonated_credentials.Credentials(
      source_credentials=source,
      target_principal=target_principal,
      target_scopes=scopes,
      lifetime=600,
  )
  creds.refresh(google.auth.transport.requests.Request())

  def _factory() -> Any:
    return bigquery.Client(
        project=project, location=location, credentials=creds
    )

  return Session(target_principal, "service_account", _factory)


# --------------------------------------------------------------------------
# Request approval
# --------------------------------------------------------------------------


def _validate_parameters(publication: dict, parameters: Any) -> dict:
  if not isinstance(parameters, dict):
    raise contracts.ContractError("parameters must be a mapping")
  declared = publication_mod.parameter_types(publication)
  supplied = list(parameters)
  if len(set(k.lower() for k in supplied)) != len(supplied):
    raise contracts.ContractError("duplicate parameter names")
  for name in supplied:
    if name in RESERVED_PARAMETER_NAMES or name not in declared:
      raise contracts.ContractError(f"undeclared parameter {name!r}")
  missing = sorted(set(declared) - set(supplied))
  if missing:
    raise contracts.ContractError(f"missing parameters {missing}")
  typed: dict[str, str] = {}
  for name, ptype in declared.items():
    if ptype != "DATE":
      raise contracts.ContractError(f"unsupported parameter type {ptype}")
    typed[name] = contracts.date_string(parameters[name])
  if "period_start" in typed and "period_end" in typed:
    if typed["period_start"] > typed["period_end"]:
      raise contracts.ContractError("period_start is after period_end")
  return typed


def approve_request(
    session: Any,
    publication: dict,
    parameters: dict,
    audience: str,
    now: int,
    registry: Any,
) -> dict:
  """Approve a typed request against a pinned publication and register it."""
  if not isinstance(session, Session):
    raise contracts.ContractError("session must be broker-created")
  if audience != contracts.AUDIENCE:
    raise contracts.ContractError("unrecognized audience")
  if not isinstance(now, int) or isinstance(now, bool) or now <= 0:
    raise contracts.ContractError("now must be a positive integer")
  for key in ("publication_digest", "compiled_sql_digest", "compiled_sql"):
    if key not in publication:
      raise contracts.ContractError("publication is not pinned")
  # Re-derive the compiled SQL from the pinned bytes; a tampered publication
  # dict cannot bind a request.
  if publication_mod.compile_sql(publication) != publication["compiled_sql"]:
    raise contracts.ContractError("publication compiled_sql is inconsistent")
  typed = _validate_parameters(publication, parameters)
  nonce = secrets.token_hex(32)  # 256-bit
  request_id = "req-" + secrets.token_hex(12)
  request = {
      "request_id": request_id,
      "nonce": nonce,
      "requester": session["authenticated_requester"],
      "principal_kind": session["principal_kind"],
      "session_id": session["session_id"],
      "audience": audience,
      "context_ref": publication["context_ref"],
      "publication_id": publication["publication_id"],
      "publication_digest": publication["publication_digest"],
      "computation_path": publication["computation_path"],
      "computation_digest": publication["computation_digest"],
      "bundle_dir": publication["bundle_dir"],
      "compiled_sql": publication["compiled_sql"],
      "compiled_sql_digest": publication["compiled_sql_digest"],
      "parameters": typed,
      "parameter_types": publication_mod.parameter_types(publication),
      "output": dict(publication["output"]),
      "output_contract_digest": publication["output_contract_digest"],
      "dependencies": list(publication["dependencies"]),
      "project": publication["project"],
      "location": publication["location"],
      "policy_version": publication["policy_version"],
      "compiler_version": publication["compiler_version"],
      "issued_at": now,
      "expires_at": now + contracts.REQUEST_TTL_SECONDS,
  }
  request["request_digest"] = contracts.digest(
      contracts.DOMAIN_REQUEST, request
  )
  registry.save_request(request)
  return request


def request_is_expired(request: dict, now: int) -> bool:
  return now >= request["expires_at"] or now < request["issued_at"]


# --------------------------------------------------------------------------
# Current-access probes (separate from proof checking)
# --------------------------------------------------------------------------


def probe_access(client: Any, request: dict) -> dict:
  """Uncached, bounded probe of current source/output access.

  ``client`` must expose ``probe_sources(sql, params) -> None`` (dry-run
  under the current requester; raises on denial) and
  ``probe_output(job) -> None`` (``jobs.get`` on the registered job). Any
  exception carrying ``code == 403`` is a conclusive denial; other errors
  are UNAVAILABLE. Nothing here reads cached results.
  """
  outcome: dict[str, Any] = {"sources": None, "output": None}
  for name, call in (
      (
          "sources",
          lambda: client.probe_sources(
              request["compiled_sql"], request["parameters"]
          ),
      ),
      ("output", lambda: client.probe_output(request.get("job"))),
  ):
    try:
      call()
      outcome[name] = ALLOWED
    except Exception as exc:  # pylint: disable=broad-except
      code = getattr(exc, "code", None)
      outcome[name] = DENIED if code == 403 else UNAVAILABLE
      outcome[f"{name}_error"] = f"{type(exc).__name__}"
  if DENIED in (outcome["sources"], outcome["output"]):
    outcome["outcome"] = DENIED
  elif UNAVAILABLE in (outcome["sources"], outcome["output"]):
    outcome["outcome"] = UNAVAILABLE
  else:
    outcome["outcome"] = ALLOWED
  outcome["probed_at"] = int(time.time())
  return outcome
