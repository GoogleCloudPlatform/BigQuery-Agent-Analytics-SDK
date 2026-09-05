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

"""SYNTHETIC BigQuery API emulation for hermetic runs and tests.

``FakeBigQuery`` stores REST-shaped Job and GetQueryResults resources. The
caller client emulates ``jobs.insert`` for the fixture publication only: it
recognises the approved compiled SQL and the product-cost-only mutation and
returns the planned fixture values (400 / 515 / 600). The evidence client
returns only API-shaped resources; nothing here is a measured result.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import re
from typing import Any

import contracts
import execute as execute_mod
import publication as publication_mod
import verify as verify_mod

HERE = Path(__file__).resolve().parent
_FULL_COGS = (
    "    COALESCE(c.product_cost, 0)\n"
    "    + COALESCE(c.fulfillment_cost, 0)\n"
    "    + COALESCE(c.shipping_cost, 0)\n"
    "    + COALESCE(c.payment_fee, 0)"
)
_PRODUCT_COST_ONLY = "    COALESCE(c.product_cost, 0)"
_TABLE_RE = re.compile(r"`([A-Za-z0-9_\-]+)\.([A-Za-z0-9_]+)\.([A-Za-z0-9_]+)`")


def product_cost_only_sql(compiled_sql: str) -> str:
  """The retired product-cost-only formula (the 'total-ARR' style attack)."""
  if _FULL_COGS not in compiled_sql:
    raise contracts.ContractError("compiled SQL does not carry full COGS")
  return compiled_sql.replace(_FULL_COGS, _PRODUCT_COST_ONLY)


def _expected() -> dict:
  return json.loads((HERE / "fixtures" / "expected.json").read_text())


class FakeBigQuery:
  """In-memory job/result store keyed by (project, location, job_id)."""

  def __init__(self):
    self.jobs: dict[tuple[str, str, str], dict] = {}
    self.results: dict[tuple[str, str, str], dict] = {}
    # Per-principal permissions: table -> set(principal), job read allowed.
    self.table_readers: dict[str, set[str]] = {}
    self.job_readers: set[str] = set()

  def grant_tables(self, principal: str, tables: list[str]) -> None:
    for t in tables:
      self.table_readers.setdefault(t, set()).add(principal)

  def revoke_tables(self, principal: str, tables: list[str]) -> None:
    for t in tables:
      self.table_readers.get(t, set()).discard(principal)

  def can_read(self, principal: str, tables: list[str]) -> bool:
    return all(principal in self.table_readers.get(t, set()) for t in tables)


class HermeticCallerClient:
  """Executor-side emulation bound to one principal."""

  def __init__(self, fake: FakeBigQuery, principal: str, publication: dict):
    self._fake = fake
    self._principal = principal
    self._pub = publication

  def _evaluate(self, sql: str, parameters: dict) -> str:
    exp = _expected()
    approved = self._pub["compiled_sql"]
    key = (parameters.get("period_start"), parameters.get("period_end"))
    if sql == approved:
      for name in ("approved_january", "approved_january_february"):
        row = exp[name]
        if key == (row["period_start"], row["period_end"]):
          return row["gross_margin_usd"]
      raise verify_mod.EvidenceError("fixture has no result for these dates")
    if sql == product_cost_only_sql(approved):
      row = exp["product_cost_only_january"]
      if key == (row["period_start"], row["period_end"]):
        return row["gross_margin_usd"]
    raise verify_mod.EvidenceError("fixture cannot evaluate this SQL", 400)

  def dry_run(self, sql: str, parameters: dict, types: dict) -> int:
    tables = [".".join(m) for m in _TABLE_RE.findall(sql)]
    if not self._fake.can_read(self._principal, tables):
      raise verify_mod.EvidenceError("Access Denied", 403)
    return 591

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
    project, location = self._pub["project"], self._pub["location"]
    key = (project, location, job_id)
    if key in self._fake.jobs:
      raise execute_mod.JobAlreadyExists(job_id)
    tables = [".".join(m) for m in _TABLE_RE.findall(sql)]
    if not self._fake.can_read(self._principal, tables):
      raise verify_mod.EvidenceError("Access Denied", 403)
    value = self._evaluate(sql, parameters)
    self._fake.jobs[key] = {
        "jobReference": {
            "projectId": project,
            "location": location,
            "jobId": job_id,
        },
        "user_email": self._principal,
        "status": {"state": "DONE"},
        "configuration": {
            "jobType": "QUERY",
            "labels": dict(labels),
            "query": {
                "query": sql,
                "useLegacySql": False,
                "useQueryCache": False,
                "queryParameters": [
                    {
                        "name": name,
                        "parameterType": {"type": parameter_types[name]},
                        "parameterValue": {"value": val},
                    }
                    for name, val in parameters.items()
                ],
            },
        },
        "statistics": {
            "creationTime": "1767225600000",
            "endTime": "1767225601000",
            "totalSlotMs": "100",
            "query": {
                "cacheHit": False,
                "statementType": "SELECT",
                "totalBytesProcessed": "591",
                "totalBytesBilled": str(10 * 1024 * 1024 * len(set(tables))),
                "referencedTables": [
                    {"projectId": p, "datasetId": d, "tableId": t}
                    for p, d, t in sorted({tuple(x.split(".")) for x in tables})
                ],
            },
        },
    }
    self._fake.results[key] = {
        "kind": "bigquery#getQueryResultsResponse",
        "jobComplete": True,
        "schema": {
            "fields": [
                {
                    "name": "gross_margin_usd",
                    "type": "NUMERIC",
                    "mode": "NULLABLE",
                }
            ]
        },
        "totalRows": "1",
        "rows": [{"f": [{"v": value}]}],
        "cacheHit": False,
    }
    return {"job_id": job_id, "state": "DONE", "error": None}

  def get_job(self, job_id: str) -> dict:
    return {"job_id": job_id, "state": "DONE", "error": None}

  def probe_sources(self, sql: str, parameters: dict) -> None:
    self.dry_run(sql, parameters, {k: "DATE" for k in parameters})

  def probe_output(self, job: dict | None) -> None:
    if not job:
      raise contracts.ContractError("no registered job")
    key = (job["project"], job["location"], job["job_id"])
    res = self._fake.jobs.get(key)
    if res is None:
      raise verify_mod.EvidenceError("Not found", 404)
    if (
        res["user_email"] != self._principal
        and self._principal not in self._fake.job_readers
    ):
      raise verify_mod.EvidenceError("Access Denied", 403)


class HermeticEvidenceClient:
  """Verifier-side emulation. ``mode`` selects an evidence failure model."""

  MODES = (
      "full",
      "metadata_only",
      "job_denied",
      "result_denied",
      "transient",
      "missing",
  )

  def __init__(self, fake: FakeBigQuery, principal: str, mode: str = "full"):
    if mode not in self.MODES:
      raise ValueError(mode)
    self._fake = fake
    self._principal = principal
    self.mode = mode
    self.calls: list[tuple[str, str]] = []

  def _lookup(
      self, store: dict, project: str, location: str, job_id: str, what: str
  ) -> dict:
    self.calls.append((what, job_id))
    if self.mode == "transient":
      raise verify_mod.EvidenceUnavailable("503 backend unavailable", 503)
    if self.mode == "missing":
      raise verify_mod.EvidenceError("Not found", 404)
    if self.mode == "job_denied":
      raise verify_mod.EvidenceError("Access Denied", 403)
    if what == "result" and self.mode == "result_denied":
      raise verify_mod.EvidenceError("Access Denied", 403)
    if what == "result" and self.mode == "metadata_only":
      raise verify_mod.EvidenceUnavailable("result read not available")
    key = (project, location, job_id)
    if key not in store:
      raise verify_mod.EvidenceError("Not found", 404)
    res = store[key]
    owner = (
        self._fake.jobs[key]["user_email"] if key in self._fake.jobs else None
    )
    if (
        owner != self._principal
        and self._principal not in self._fake.job_readers
    ):
      raise verify_mod.EvidenceError("Access Denied", 403)
    return copy.deepcopy(res)

  def get_job(self, project: str, location: str, job_id: str) -> dict:
    return self._lookup(self._fake.jobs, project, location, job_id, "job")

  def get_query_results(
      self, project: str, location: str, job_id: str, page_token: str | None
  ) -> dict:
    return self._lookup(self._fake.results, project, location, job_id, "result")

  def probe_sources(self, sql: str, parameters: dict) -> None:
    if self.mode == "transient":
      raise verify_mod.EvidenceUnavailable("503 backend unavailable", 503)
    tables = [".".join(m) for m in _TABLE_RE.findall(sql)]
    if not self._fake.can_read(self._principal, tables):
      raise verify_mod.EvidenceError("Access Denied", 403)

  def probe_output(self, job: dict | None) -> None:
    if not job:
      raise contracts.ContractError("no registered job")
    self.get_job(job["project"], job["location"], job["job_id"])


def fixture_world(
    principal: str = "requester@example.test",
) -> tuple[FakeBigQuery, dict]:
  """A FakeBigQuery with the fixture publication granted to ``principal``."""
  pub = publication_mod.load_fixture_publication(HERE)
  fake = FakeBigQuery()
  fake.grant_tables(principal, pub["dependencies"])
  return fake, pub


def fingerprint(obj: Any) -> str:
  return hashlib.sha256(json.dumps(obj, sort_keys=True).encode()).hexdigest()[
      :16
  ]
