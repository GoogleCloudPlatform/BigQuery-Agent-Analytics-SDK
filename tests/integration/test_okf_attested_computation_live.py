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

"""Live acceptance for examples/okf_attested_computation (real BigQuery).

Opt in explicitly::

    OKF_SPIKE_LIVE=1 GOOGLE_CLOUD_PROJECT=test-project-0728-467323 \\
        python -m pytest tests/integration/test_okf_attested_computation_live.py -q -s

Skipped otherwise. A skipped run is not acceptance evidence. Requires ADC
for a real user with jobUser + dataViewer on the fixture dataset, and (for
R8/R9) permission to impersonate ``OKF_SPIKE_RESTRICTED_SA``. Every case
appends a sanitized record to ``evidence/receipt/live_cases.json``.
"""

from __future__ import annotations

import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

EXAMPLE_DIR = (
    Path(__file__).resolve().parents[2]
    / "examples"
    / "okf_attested_computation"
)
EVIDENCE = EXAMPLE_DIR / "evidence" / "receipt" / "live_cases.json"
LIVE_PROJECT = "test-project-0728-467323"
_LIVE = os.environ.get("OKF_SPIKE_LIVE", "") == "1"
_PROJECT_OK = os.environ.get("GOOGLE_CLOUD_PROJECT") == LIVE_PROJECT
RESTRICTED_SA = os.environ.get(
    "OKF_SPIKE_RESTRICTED_SA",
    "okf-receipt-restricted@test-project-0728-467323.iam.gserviceaccount.com",
)

pytestmark = pytest.mark.skipif(
    not (_LIVE and _PROJECT_OK),
    reason="live spike tests need OKF_SPIKE_LIVE=1 and GOOGLE_CLOUD_PROJECT="
    + LIVE_PROJECT,
)

if _LIVE and _PROJECT_OK:
  sys.path.append(str(EXAMPLE_DIR))
  import attacks  # noqa: E402
  import broker  # noqa: E402
  import consume as consume_mod  # noqa: E402
  import contracts  # noqa: E402
  import execute as execute_mod  # noqa: E402
  import hermetic  # noqa: E402
  import publication as publication_mod  # noqa: E402
  import receipt_store  # noqa: E402
  import verify as verify_mod  # noqa: E402

JAN = {"period_start": "2026-01-01", "period_end": "2026-01-31"}
JAN_FEB = {"period_start": "2026-01-01", "period_end": "2026-02-28"}
GOOD = {"field": "gross_margin_usd", "value": "400", "unit": "USD"}


def _now() -> int:
  return int(time.time())


ALIASES = {
    "raincoatrun@gmail.com": "user:owner",
    RESTRICTED_SA: "sa:okf-receipt-restricted",
}


def _redact(obj):
  """Replace real principals with aliases before anything is committed."""
  if isinstance(obj, dict):
    return {k: _redact(v) for k, v in obj.items()}
  if isinstance(obj, list):
    return [_redact(v) for v in obj]
  if isinstance(obj, str):
    for real, alias in ALIASES.items():
      obj = obj.replace(real, alias)
    return obj
  return obj


def _record(case: str, payload: dict) -> None:
  payload = _redact(payload)
  EVIDENCE.parent.mkdir(parents=True, exist_ok=True)
  rows = json.loads(EVIDENCE.read_text()) if EVIDENCE.exists() else []
  rows = [r for r in rows if r.get("case") != case]
  rows.append(
      dict(
          case=case,
          recorded_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
          **payload,
      )
  )
  EVIDENCE.write_text(json.dumps(rows, indent=2, sort_keys=True, default=str))


def _job_stats(client, job: dict) -> dict:
  j = client.get_job(
      job["job_id"], project=job["project"], location=job["location"]
  )
  return {
      "job_id": job["job_id"],
      "user_email": j.user_email,
      "state": j.state,
      "cache_hit": j.cache_hit,
      "total_bytes_processed": j.total_bytes_processed,
      "total_bytes_billed": j.total_bytes_billed,
      "slot_millis": j.slot_millis,
      "created": j.created.isoformat() if j.created else None,
  }


def _summary(out: dict) -> dict:
  return {
      k: out.get(k) for k in ("verdict", "execution_match", "reason_codes")
  } | {"released": "display" in out}


@pytest.fixture(scope="module")
def pub():
  return publication_mod.load_fixture_publication(EXAMPLE_DIR)


@pytest.fixture(scope="module")
def user_session(pub):
  return broker.open_live_session(pub["project"], pub["location"])


@pytest.fixture(scope="module")
def keys(tmp_path_factory):
  return receipt_store.KeyStore(tmp_path_factory.mktemp("keys"))


@pytest.fixture(scope="module")
def registry_path(tmp_path_factory):
  return tmp_path_factory.mktemp("reg") / "registry.sqlite"


@pytest.fixture(scope="module")
def registry(registry_path):
  return receipt_store.Registry(registry_path)


def _issue_then_consume(req, handle, claim, evidence, registry, keys):
  """Trusted two-step path: verifier seals, consumer gates; fresh clocks."""
  issued = verify_mod.verify(
      req["request_id"],
      handle["receipt_id"],
      claim,
      evidence,
      registry,
      registry,
      _now(),
      keys=keys,
  )
  out = consume_mod.consume(
      req["request_id"],
      handle["receipt_id"],
      claim,
      evidence,
      registry,
      registry,
      _now(),
      keys=keys,
  )
  return issued, out


def _user_world(user_session, pub, registry):
  caller = execute_mod.BigQueryCallerClient(user_session.delegated_client())
  evidence = verify_mod.BigQueryEvidenceClient(user_session.delegated_client())
  req = broker.approve_request(
      user_session, pub, dict(JAN), contracts.AUDIENCE, _now(), registry
  )
  return caller, evidence, req


# -- R1 + independence ---------------------------------------------------------


def test_r1_approved_run_verifies_and_releases(
    user_session, pub, registry, keys, registry_path
):
  assert user_session["principal_kind"] == "user", user_session
  caller, evidence, req = _user_world(user_session, pub, registry)
  handle = execute_mod.execute(req, pub, caller, registry)
  first = verify_mod.verify(
      req["request_id"],
      handle["receipt_id"],
      GOOD,
      evidence,
      registry,
      registry,
      _now(),
      keys=keys,
  )
  assert first["verdict"] == "VERIFIED" and first["execution_match"] == "MATCH"
  assert first["value"] == "400"
  # Independence: a fresh process with only registry path, key dir, ids and ADC.
  code = (
      "import sys, json; sys.path.append(%r); import receipt_store, verify, broker\n"
      "reg = receipt_store.Registry(%r); keys = receipt_store.KeyStore(%r)\n"
      "s = broker.open_live_session(%r, %r)\n"
      "ev = verify.BigQueryEvidenceClient(s.delegated_client())\n"
      "out = verify.verify(%r, %r, %r, ev, reg, reg, %d, keys=keys)\n"
      "print(json.dumps({k: out[k] for k in ('verdict','execution_match','reason_codes')} | {'rc': out['receipt']['result_commitment'], 'eah': out['receipt']['executed_artifact_hash']}))"
  ) % (
      str(EXAMPLE_DIR),
      str(registry_path),
      str(keys._dir),
      pub["project"],
      pub["location"],
      req["request_id"],
      handle["receipt_id"],
      GOOD,
      _now(),
  )
  proc = subprocess.run(
      [sys.executable, "-c", code],
      capture_output=True,
      text=True,
      timeout=180,
      check=False,
  )
  assert proc.returncode == 0, proc.stderr[-800:]
  second = json.loads(proc.stdout.strip().splitlines()[-1])
  assert second["verdict"] == "VERIFIED"
  assert second["rc"] == first["receipt"]["result_commitment"]
  assert second["eah"] == first["receipt"]["executed_artifact_hash"]
  issued_out, out = _issue_then_consume(
      req, handle, GOOD, evidence, registry, keys
  )
  assert out["verdict"] == "VERIFIED"
  assert out["display"] == "Gross margin: $400.00 USD · VERIFIED"
  stats = _job_stats(user_session.delegated_client(), handle["job"])
  assert stats["user_email"] == user_session["authenticated_requester"]
  assert stats["cache_hit"] is False
  _record(
      "R1_approved",
      {
          "job": stats,
          "first_verify": _summary(first),
          "fresh_process_verify": second,
          "consume": _summary(out),
          "display": out["display"],
      },
  )


# -- R2-R5 --------------------------------------------------------------------


def test_r2_sql_substitution_real_job_rejected(
    user_session, pub, registry, keys
):
  caller, evidence, req = _user_world(user_session, pub, registry)
  wrong = hermetic.product_cost_only_sql(req["compiled_sql"])
  handle = attacks.adversarial_execute(req, wrong, JAN, caller, registry)
  issued_out, out = _issue_then_consume(
      req, handle, dict(GOOD, value="600"), evidence, registry, keys
  )
  assert out["verdict"] == "REJECTED" and out["reason_codes"] == [
      "sql_mismatch"
  ]
  assert "display" not in out and "value" not in out
  # The wrong job really produced 600 (read directly, outside the trusted path).
  rows = list(
      user_session.delegated_client().get_job(handle["job"]["job_id"]).result()
  )
  assert str(rows[0][0]) == "600"
  _record(
      "R2_sql_substitution",
      {
          "job": _job_stats(user_session.delegated_client(), handle["job"]),
          "wrong_job_actual_value": "600",
          "consume": _summary(out),
      },
  )


def test_r3_parameter_substitution_real_job_rejected(
    user_session, pub, registry, keys
):
  caller, evidence, req = _user_world(user_session, pub, registry)
  handle = attacks.adversarial_execute(
      req, req["compiled_sql"], JAN_FEB, caller, registry
  )
  issued_out, out = _issue_then_consume(
      req, handle, dict(GOOD, value="515"), evidence, registry, keys
  )
  assert out["verdict"] == "REJECTED" and out["reason_codes"] == [
      "parameter_mismatch"
  ]
  assert "display" not in out
  rows = list(
      user_session.delegated_client().get_job(handle["job"]["job_id"]).result()
  )
  assert str(rows[0][0]) == "515"
  _record(
      "R3_parameter_substitution",
      {
          "job": _job_stats(user_session.delegated_client(), handle["job"]),
          "changed_period_actual_value": "515",
          "consume": _summary(out),
      },
  )


def test_r4_display_substitution_rejected(user_session, pub, registry, keys):
  caller, evidence, req = _user_world(user_session, pub, registry)
  handle = execute_mod.execute(req, pub, caller, registry)
  sealed = verify_mod.verify(
      req["request_id"],
      handle["receipt_id"],
      GOOD,
      evidence,
      registry,
      registry,
      _now(),
      keys=keys,
  )
  assert sealed["verdict"] == "VERIFIED", sealed
  proof = registry.get_receipt(handle["receipt_id"])
  outs = {}
  for name, claim in (
      ("value_600", dict(GOOD, value="600")),
      ("wrong_field", dict(GOOD, field="total_arr_usd")),
      ("wrong_unit", dict(GOOD, unit="EUR")),
  ):
    issued_out, out = _issue_then_consume(
        req, handle, claim, evidence, registry, keys
    )
    assert out["verdict"] == "REJECTED" and "display" not in out, name
    assert out["reason_codes"] == ["display_mismatch"], (name, out)
    assert registry.get_receipt(handle["receipt_id"]) == proof, name
    outs[name] = _summary(out)
  assert not registry.is_consumed(req["request_id"])
  # The honest claim still releases after the rejected attempts.
  good = consume_mod.consume(
      req["request_id"],
      handle["receipt_id"],
      GOOD,
      evidence,
      registry,
      registry,
      _now(),
      keys=keys,
  )
  assert good["verdict"] == "VERIFIED", good
  outs["honest_after_rejections"] = _summary(good)
  _record(
      "R4_display_substitution",
      {
          "job": _job_stats(user_session.delegated_client(), handle["job"]),
          "claims": outs,
      },
  )


def test_r5_missing_evidence_stays_unverifiable(
    user_session, pub, registry, keys
):
  caller, evidence, req = _user_world(user_session, pub, registry)
  invented = attacks.register_invented_job(req, registry)
  issued_out_missing, out_missing = _issue_then_consume(
      req, invented, GOOD, evidence, registry, keys
  )
  assert out_missing["verdict"] == "UNVERIFIABLE" and out_missing[
      "reason_codes"
  ] == ["job_missing"]
  req2 = broker.approve_request(
      user_session, pub, dict(JAN), contracts.AUDIENCE, _now(), registry
  )
  handle = execute_mod.execute(req2, pub, caller, registry)
  meta_only = verify_mod.MetadataOnlyEvidenceClient(evidence)
  issued_out_meta, out_meta = _issue_then_consume(
      req2, handle, GOOD, meta_only, registry, keys
  )
  assert (
      out_meta["verdict"] == "UNVERIFIABLE"
      and out_meta["execution_match"] == "MATCH"
  )
  assert out_meta["reason_codes"] == ["result_unavailable"]
  assert "display" not in out_missing and "display" not in out_meta
  _record(
      "R5_missing_evidence",
      {
          "invented_job": _summary(out_missing),
          "metadata_only": _summary(out_meta),
      },
  )


# -- R8 / R9 with a real restricted principal ------------------------------------


def _restricted_session(pub):
  try:
    return broker.open_impersonated_session(
        pub["project"], pub["location"], RESTRICTED_SA
    )
  except Exception as exc:  # pylint: disable=broad-except
    _record(
        "R8_R9_restricted_identity",
        {
            "status": "BLOCKED",
            "error": f"{type(exc).__name__}: {str(exc)[:200]}",
        },
    )
    pytest.skip(f"restricted identity unavailable: {exc}")


def _set_dataset_reader(owner_client, pub, principal: str, grant: bool) -> str:
  from google.cloud import bigquery

  ds = owner_client.get_dataset(f"{pub['project']}.{pub['dataset']}")
  entries = [
      e
      for e in ds.access_entries
      if not (e.entity_type == "userByEmail" and e.entity_id == principal)
  ]
  if grant:
    entries.append(bigquery.AccessEntry("READER", "userByEmail", principal))
  ds.access_entries = entries
  owner_client.update_dataset(ds, ["access_entries"])
  return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _wait_probe(evidence, req, expected: str, timeout_s: int = 240) -> dict:
  deadline = time.time() + timeout_s
  last = None
  while time.time() < deadline:
    last = broker.probe_access(evidence, dict(req, job=None))
    if last["sources"] == expected:
      return last
    time.sleep(10)
  return last or {}


def test_r8_service_account_job_with_user_label_rejected(
    user_session, pub, registry, keys
):
  sa_session = _restricted_session(pub)
  owner_client = user_session.delegated_client()
  granted_at = _set_dataset_reader(owner_client, pub, RESTRICTED_SA, True)
  try:
    sa_evidence = verify_mod.BigQueryEvidenceClient(
        sa_session.delegated_client()
    )
    _, user_evidence, req = _user_world(user_session, pub, registry)
    probe = _wait_probe(sa_evidence, req, broker.ALLOWED)
    assert probe.get("sources") == broker.ALLOWED, probe
    sa_caller = execute_mod.BigQueryCallerClient(sa_session.delegated_client())
    handle = attacks.adversarial_execute(
        req,
        req["compiled_sql"],
        JAN,
        sa_caller,
        registry,
        labels={
            "requester": user_session["authenticated_requester"]
            .replace("@", "_at_")
            .replace(".", "_")
        },
    )
    issued_out, out = _issue_then_consume(
        req, handle, GOOD, user_evidence, registry, keys
    )
    assert out["verdict"] == "REJECTED" and out["reason_codes"] == [
        "owner_mismatch"
    ]
    assert "display" not in out
    stats = _job_stats(owner_client, handle["job"])
    assert stats["user_email"] == RESTRICTED_SA
    _record(
        "R8_identity_spoof",
        {
            "job": stats,
            "label_claimed_requester": user_session["authenticated_requester"],
            "consume": _summary(out),
            "granted_at": granted_at,
        },
    )
  finally:
    _set_dataset_reader(owner_client, pub, RESTRICTED_SA, False)


def test_r9_revocation_and_output_denial(user_session, pub, registry, keys):
  sa_session = _restricted_session(pub)
  owner_client = user_session.delegated_client()
  timeline = {}
  timeline["granted_at"] = _set_dataset_reader(
      owner_client, pub, RESTRICTED_SA, True
  )
  try:
    sa_caller = execute_mod.BigQueryCallerClient(sa_session.delegated_client())
    sa_evidence = verify_mod.BigQueryEvidenceClient(
        sa_session.delegated_client()
    )
    req = broker.approve_request(
        sa_session, pub, dict(JAN), contracts.AUDIENCE, _now(), registry
    )
    assert req["requester"] == RESTRICTED_SA
    probe = _wait_probe(sa_evidence, req, broker.ALLOWED)
    timeline["allowed_probe_at"] = probe.get("probed_at")
    assert probe.get("sources") == broker.ALLOWED, probe
    handle = execute_mod.execute(req, pub, sa_caller, registry)
    first = verify_mod.verify(
        req["request_id"],
        handle["receipt_id"],
        GOOD,
        sa_evidence,
        registry,
        registry,
        _now(),
        keys=keys,
    )
    assert first["verdict"] == "VERIFIED", first
    # Denied output: the restricted principal cannot read the USER's job.
    _, _, user_req = _user_world(user_session, pub, registry)
    user_handle = execute_mod.execute(
        user_req, pub, execute_mod.BigQueryCallerClient(owner_client), registry
    )
    denied = verify_mod.verify(
        user_req["request_id"],
        user_handle["receipt_id"],
        GOOD,
        sa_evidence,
        registry,
        registry,
        _now(),
        keys=keys,
    )
    assert denied["verdict"] == "REJECTED" and denied["reason_codes"] == [
        "job_read_denied"
    ], denied
    # Revoke source access after issuance, then attempt consumption.
    timeline["revoked_at"] = _set_dataset_reader(
        owner_client, pub, RESTRICTED_SA, False
    )
    probe = _wait_probe(sa_evidence, req, broker.DENIED)
    timeline["denied_probe_at"] = probe.get("probed_at")
    assert probe.get("sources") == broker.DENIED, probe
    issued_out, out = _issue_then_consume(
        req, handle, GOOD, sa_evidence, registry, keys
    )
    assert out["verdict"] == "REJECTED" and "display" not in out
    assert out["reason_codes"][0] in (
        "access_denied",
        "job_read_denied",
        "result_read_denied",
    ), out
    assert not registry.is_consumed(req["request_id"])
    _record(
        "R9_revocation_output_denial",
        {
            "restricted_principal": RESTRICTED_SA,
            "job": _job_stats(owner_client, handle["job"]),
            "verify_before_revocation": _summary(first),
            "restricted_reads_user_job": _summary(denied),
            "consume_after_revocation": _summary(out),
            "timeline": timeline,
        },
    )
  finally:
    _set_dataset_reader(owner_client, pub, RESTRICTED_SA, False)
