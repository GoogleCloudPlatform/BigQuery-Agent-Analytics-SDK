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

"""Caller-delegated job submission for an approved request.

``execute`` takes an approved request and the pinned publication, compiles
the SQL again from the publication, and submits exactly one GoogleSQL
SELECT with typed DATE parameters, query cache disabled, a bounded
``maximum_bytes_billed`` derived from a dry run, and a broker-generated job
ID tied to the request nonce. It returns only the registered job tuple and
an opaque receipt handle; it never returns SQL or rows to the agent.

There is deliberately no free-form SQL input on this path.
"""

from __future__ import annotations

import secrets
from typing import Any

import contracts
import publication as publication_mod

MIN_BYTES_BILLED = 100 * 1024 * 1024  # BigQuery bills 10 MiB per table minimum.
HARD_MAX_BYTES_BILLED = 1024**3  # plan-stated 1 GiB ceiling; never exceeded.
JOB_TIMEOUT_MS = 60_000


class ExecutionError(RuntimeError):
  """Submission failed; no receipt can be issued."""


class JobAlreadyExists(ExecutionError):
  """The broker-generated job ID was already submitted."""


def job_id_for(request: dict) -> str:
  """Deterministic job ID for a request: recoverable, never duplicated."""
  return f"okf_rcpt_{request['request_id'][4:]}_{request['nonce'][:16]}"


def execute(
    request: dict, publication: dict, caller_client: Any, registry: Any
) -> dict:
  """Submit the approved computation under the caller's delegation.

  ``caller_client`` is broker-provided (``session.delegated_client()``
  wrapped by :class:`BigQueryCallerClient` or the hermetic emulation) and
  must expose ``dry_run``, ``submit`` and ``get_job``.
  """
  stored = registry.load_request(request["request_id"])
  if stored is None or stored != request:
    raise ExecutionError("request is not the registered request")
  if publication["publication_digest"] != request["publication_digest"]:
    raise ExecutionError("publication does not match the approved request")
  sql = publication_mod.compile_sql(publication)
  if sql != request["compiled_sql"]:
    raise ExecutionError("compiled SQL drifted from the approved request")
  params = request["parameters"]
  types = request["parameter_types"]

  job_id = job_id_for(request)
  tuple_ = {
      "project": request["project"],
      "location": request["location"],
      "job_id": job_id,
  }
  labels = {"okf_request": request["request_id"][4:20]}
  try:
    estimated = int(caller_client.dry_run(sql, params, types))
    if estimated > HARD_MAX_BYTES_BILLED:
      raise ExecutionError(
          f"dry run estimates {estimated} bytes, above the"
          f" {HARD_MAX_BYTES_BILLED} byte ceiling"
      )
    max_bytes = min(HARD_MAX_BYTES_BILLED, max(MIN_BYTES_BILLED, estimated * 4))
    job = caller_client.submit(
        job_id=job_id,
        sql=sql,
        parameters=params,
        parameter_types=types,
        maximum_bytes_billed=max_bytes,
        timeout_ms=JOB_TIMEOUT_MS,
        labels=labels,
    )
  except JobAlreadyExists:
    job = caller_client.get_job(job_id)
  state = job.get("state")
  if state != "DONE":
    raise ExecutionError(f"job {job_id} did not complete: {state}")
  if job.get("error"):
    registry.register_job(request["request_id"], tuple_)
    raise ExecutionError(f"job {job_id} failed: {job['error']}")
  registry.register_job(request["request_id"], tuple_)
  receipt_id = "rcpt-" + secrets.token_hex(12)
  registry.put_receipt(
      receipt_id,
      {
          "receipt_id": receipt_id,
          "request_id": request["request_id"],
          "status": "pending",
      },
  )
  return {
      "request_id": request["request_id"],
      "receipt_id": receipt_id,
      "job": tuple_,
  }


class BigQueryCallerClient:
  """Adapter from ``google.cloud.bigquery.Client`` to the executor protocol."""

  def __init__(self, client: Any):
    self._client = client

  @staticmethod
  def _params(parameters: dict, types: dict) -> list:
    from google.cloud import bigquery

    return [
        bigquery.ScalarQueryParameter(name, types[name], value)
        for name, value in parameters.items()
    ]

  def dry_run(self, sql: str, parameters: dict, types: dict) -> int:
    from google.cloud import bigquery

    cfg = bigquery.QueryJobConfig(
        dry_run=True,
        use_query_cache=False,
        use_legacy_sql=False,
        query_parameters=self._params(parameters, types),
    )
    job = self._client.query(sql, job_config=cfg)
    return int(job.total_bytes_processed or 0)

  def submit(
      self,
      job_id: str,
      sql: str,
      parameters: dict,
      parameter_types: dict,
      maximum_bytes_billed: int,
      timeout_ms: int,
      labels: dict,
  ) -> dict:
    from google.api_core import exceptions
    from google.cloud import bigquery

    cfg = bigquery.QueryJobConfig(
        use_query_cache=False,
        use_legacy_sql=False,
        query_parameters=self._params(parameters, parameter_types),
        maximum_bytes_billed=maximum_bytes_billed,
        job_timeout_ms=timeout_ms,
        labels=labels,
    )
    try:
      job = self._client.query(
          sql, job_config=cfg, job_id=job_id, job_retry=None
      )
    except exceptions.Conflict as exc:
      raise JobAlreadyExists(job_id) from exc
    try:
      job.result(timeout=timeout_ms / 1000)
    except Exception as exc:  # pylint: disable=broad-except
      return {"job_id": job_id, "state": job.state, "error": str(exc)[:200]}
    return {"job_id": job_id, "state": job.state, "error": job.error_result}

  def get_job(self, job_id: str) -> dict:
    job = self._client.get_job(job_id)
    job.result()
    return {"job_id": job_id, "state": job.state, "error": job.error_result}

  # -- current-access probes (broker.probe_access) -------------------------

  def probe_sources(self, sql: str, parameters: dict) -> None:
    types = {k: "DATE" for k in parameters}
    self.dry_run(sql, parameters, types)

  def probe_output(self, job: dict | None) -> None:
    if not job:
      raise contracts.ContractError("no registered job")
    self._client.get_job(
        job["job_id"], project=job["project"], location=job["location"]
    )
