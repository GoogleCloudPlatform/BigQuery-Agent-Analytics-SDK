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

"""Controlled adversarial executor. NOT part of the trusted path.

Used only by tests and the demo attack cases to submit a real job whose SQL
or bindings differ from the approved request, then register it as if a
compromised executor had done so. The normal ``execute.execute`` API has
no free-form SQL input; this module exists so the negative cases produce
authentic job evidence rather than fabricated fixtures.
"""

from __future__ import annotations

import secrets
from typing import Any

import execute as execute_mod


def adversarial_execute(
    request: dict,
    sql: str,
    parameters: dict,
    caller_client: Any,
    registry: Any,
    *,
    job_id: str | None = None,
    labels: dict | None = None,
) -> dict:
  """Submit ``sql``/``parameters`` and bind the job to ``request``."""
  job_id = job_id or execute_mod.job_id_for(request)
  types = {k: "DATE" for k in parameters}
  try:
    job = caller_client.submit(
        job_id=job_id,
        sql=sql,
        parameters=parameters,
        parameter_types=types,
        maximum_bytes_billed=execute_mod.MIN_BYTES_BILLED,
        timeout_ms=execute_mod.JOB_TIMEOUT_MS,
        labels=labels or {"okf_request": request["request_id"][4:20]},
    )
  except execute_mod.JobAlreadyExists:
    job = caller_client.get_job(job_id)
  tuple_ = {
      "project": request["project"],
      "location": request["location"],
      "job_id": job_id,
  }
  registry.register_job(request["request_id"], tuple_)
  receipt_id = "rcpt-" + secrets.token_hex(12)
  registry.put_receipt(
      receipt_id,
      {"receipt_id": receipt_id, "request_id": request["request_id"], "status": "pending"},
  )
  return {
      "request_id": request["request_id"],
      "receipt_id": receipt_id,
      "job": tuple_,
      "job_state": job.get("state"),
  }


def register_invented_job(request: dict, registry: Any) -> dict:
  """Bind a job ID that was never submitted (missing-evidence case)."""
  tuple_ = {
      "project": request["project"],
      "location": request["location"],
      "job_id": "okf_rcpt_invented_" + secrets.token_hex(6),
  }
  registry.register_job(request["request_id"], tuple_)
  receipt_id = "rcpt-" + secrets.token_hex(12)
  registry.put_receipt(
      receipt_id,
      {"receipt_id": receipt_id, "request_id": request["request_id"], "status": "pending"},
  )
  return {"request_id": request["request_id"], "receipt_id": receipt_id, "job": tuple_}
