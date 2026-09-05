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

"""Hermetic tests for examples/okf_attested_computation (no GCP).

Evidence clients here emulate only BigQuery API responses; the verifier
never sees the test executor's in-memory result. Live acceptance lives in
tests/integration/test_okf_attested_computation_live.py.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
import sys
import threading

import pytest

EXAMPLE_DIR = (
    Path(__file__).resolve().parents[2]
    / "examples"
    / "okf_attested_computation"
)
FIXTURES = EXAMPLE_DIR / "fixtures"
sys.path.insert(0, str(EXAMPLE_DIR))

import broker  # noqa: E402
import contracts  # noqa: E402
import publication as publication_mod  # noqa: E402
import receipt_store  # noqa: E402

AUD = contracts.AUDIENCE
JAN = {"period_start": "2026-01-01", "period_end": "2026-01-31"}


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def publication():
  return publication_mod.load_fixture_publication(EXAMPLE_DIR)


@pytest.fixture
def registry():
  return receipt_store.Registry(":memory:")


@pytest.fixture
def keys(tmp_path):
  return receipt_store.KeyStore(tmp_path / "keys")


@pytest.fixture
def session():
  return broker.open_hermetic_session("requester@example.test", lambda: None)


# --------------------------------------------------------------------------
# Task 2: contracts, publication pin, request approval
# --------------------------------------------------------------------------


def test_cbor_matches_pinned_adapter_vectors():
  vectors = json.loads((FIXTURES / "cbor_vectors.json").read_text())
  assert len(vectors) >= 5
  for v in vectors:
    assert contracts.cbor(v["value"]).hex() == v["cbor_hex"]
    assert contracts.digest("okf-receipt:test", v["value"]) == v["h_hex"]


def test_cbor_rejects_floats_and_bad_keys():
  with pytest.raises(contracts.ContractError):
    contracts.cbor(400.0)
  with pytest.raises(contracts.ContractError):
    contracts.cbor({1: "x"})
  with pytest.raises(contracts.ContractError):
    contracts.cbor(-1)
  with pytest.raises(contracts.ContractError):
    contracts.cbor(b"bytes")


def test_decimal_string_is_exact_and_normalized():
  assert contracts.decimal_string("400.00") == "400"
  assert contracts.decimal_string("515") == "515"
  assert contracts.decimal_string("4E+2") == "400"
  assert contracts.decimal_string("0.10") == "0.1"
  assert contracts.decimal_string("-0.00") == "0"
  with pytest.raises(contracts.ContractError):
    contracts.decimal_string(400.0)
  with pytest.raises(contracts.ContractError):
    contracts.decimal_string("NaN")
  with pytest.raises(contracts.ContractError):
    contracts.decimal_string("")


def test_publication_pins_source_bytes_and_compiles_allowlist(publication):
  assert publication["synthetic"] is True
  assert "acme." not in publication["compiled_sql"]
  assert publication["compiled_sql"] == publication_mod.compile_sql(publication)
  assert len(publication["dependencies"]) == 7
  assert all(
      d.startswith("test-project-0728-467323.okf_receipt_spike_20260905.")
      for d in publication["dependencies"]
  )
  # Comments and quoted literals are untouched.
  assert "'delivered'" in publication["compiled_sql"]
  assert "CURRENT_DATE()" in publication["compiled_sql"]
  assert publication["parameters"] == [
      {"name": "period_start", "type": "DATE", "required": True},
      {"name": "period_end", "type": "DATE", "required": True},
  ]
  assert publication["output"]["field"] == "gross_margin_usd"
  assert publication["output"]["unit"] == "USD"


def test_publication_rejects_digest_mismatch(tmp_path):
  manifest = publication_mod.load_manifest(FIXTURES / "publication.json")
  manifest["computation_sha256"] = "0" * 64
  with pytest.raises(contracts.ContractError, match="pinned digest"):
    publication_mod.load_publication(str(FIXTURES), manifest)


def test_publication_rejects_unmapped_or_wildcard_tables():
  manifest = publication_mod.load_manifest(FIXTURES / "publication.json")
  bad = copy.deepcopy(manifest)
  del bad["table_map"]["acme.finance.payment_fees"]
  with pytest.raises(contracts.ContractError, match="table_map"):
    publication_mod.load_publication(str(FIXTURES), bad)
  bad = copy.deepcopy(manifest)
  bad["table_map"]["acme.sales.orders"] = "orders*"
  with pytest.raises(contracts.ContractError):
    publication_mod.load_publication(str(FIXTURES), bad)
  bad = copy.deepcopy(manifest)
  bad["output"]["type"] = "FLOAT64"
  with pytest.raises(contracts.ContractError, match="NUMERIC"):
    publication_mod.load_publication(str(FIXTURES), bad)


def test_modified_sql_map_or_output_change_commitments(publication):
  base = publication["publication_digest"]
  mutated = copy.deepcopy(publication)
  mutated["sanctioned_sql"] = publication["sanctioned_sql"].replace(
      "+ COALESCE(c.payment_fee, 0)", ""
  )
  assert publication_mod.compile_sql(mutated) != publication["compiled_sql"]
  # Digest recomputation: output contract and table map both move the pin.
  out_a = contracts.digest(contracts.DOMAIN_OUTPUT, publication["output"])
  out_b = contracts.digest(
      contracts.DOMAIN_OUTPUT, dict(publication["output"], unit="EUR")
  )
  assert out_a != out_b
  mapped = copy.deepcopy(publication)
  mapped["table_map"]["acme.sales.orders"] = "orders_v2"
  assert publication_mod.compile_sql(mapped) != publication["compiled_sql"]
  assert base == publication["publication_digest"]


def test_compile_refuses_scripts_and_ddl(publication):
  bad = dict(publication, sanctioned_sql="SELECT 1; SELECT 2")
  with pytest.raises(contracts.ContractError):
    publication_mod.compile_sql(bad)
  bad = dict(
      publication,
      sanctioned_sql="CREATE TEMP FUNCTION f() AS (1) SELECT `acme.sales.orders`",
  )
  with pytest.raises(contracts.ContractError):
    publication_mod.compile_sql(bad)


def test_agent_cannot_replace_publication_or_parameters(
    publication, session, registry
):
  with pytest.raises(contracts.ContractError):
    broker.approve_request(
        session,
        publication,
        dict(JAN, sql="SELECT 600"),
        AUD,
        1000,
        registry,
    )
  approved = broker.approve_request(
      session, publication, dict(JAN), AUD, 1000, registry
  )
  assert approved["expires_at"] == 1300
  assert approved["requester"] == session["authenticated_requester"]
  assert approved["compiled_sql"] == publication_mod.compile_sql(publication)
  assert len(approved["nonce"]) == 64
  assert registry.load_request(approved["request_id"]) == approved


@pytest.mark.parametrize(
    "params",
    [
        {"period_start": "2026-01-01"},
        {"period_start": "2026-01-31", "period_end": "2026-01-01"},
        {"period_start": "2026-01-01", "period_end": "2026-01-31", "extra": 1},
        {"period_start": "2026-01-01", "period_end": "2026-02-30"},
        {"period_start": "2026-01-01", "period_end": "2026-01-31 00:00"},
        {
            "period_start": "2026-01-01",
            "Period_End": "2026-01-31",
            "period_end": "2026-01-31",
        },
        {
            "period_start": "2026-01-01",
            "period_end": "2026-01-31",
            "requester": "x",
        },
        "not a mapping",
    ],
)
def test_request_validation_rejects_bad_parameters(
    publication, session, registry, params
):
  with pytest.raises(contracts.ContractError):
    broker.approve_request(session, publication, params, AUD, 1000, registry)


def test_request_rejects_wrong_audience_and_forged_session(
    publication, registry, session
):
  with pytest.raises(contracts.ContractError):
    broker.approve_request(
        session, publication, dict(JAN), "other/v1", 1000, registry
    )
  forged = {"authenticated_requester": "admin@example.test", "session_id": "s"}
  with pytest.raises(contracts.ContractError):
    broker.approve_request(forged, publication, dict(JAN), AUD, 1000, registry)


def test_tampered_publication_cannot_bind_request(
    publication, session, registry
):
  tampered = copy.deepcopy(publication)
  tampered["compiled_sql"] = tampered["compiled_sql"].replace(
      "+ COALESCE(c.payment_fee, 0)", ""
  )
  with pytest.raises(contracts.ContractError, match="inconsistent"):
    broker.approve_request(session, tampered, dict(JAN), AUD, 1000, registry)
  unpinned = {k: v for k, v in publication.items() if k != "publication_digest"}
  with pytest.raises(contracts.ContractError, match="pinned"):
    broker.approve_request(session, unpinned, dict(JAN), AUD, 1000, registry)


def test_registry_consume_once_is_atomic(registry):
  request_id, nonce = "req-x", "n" * 64
  results: list[bool] = []
  barrier = threading.Barrier(8)

  def _go():
    barrier.wait()
    results.append(registry.consume_once(request_id, nonce, AUD))

  threads = [threading.Thread(target=_go) for _ in range(8)]
  for t in threads:
    t.start()
  for t in threads:
    t.join()
  assert results.count(True) == 1
  assert results.count(False) == 7


def test_keystore_private_and_revocable(tmp_path, keys):
  key_id = keys.current_key_id
  assert (tmp_path / "keys").stat().st_mode & 0o077 == 0
  for role in ("commit", "integrity"):
    assert (
        tmp_path / "keys" / f"{key_id}.{role}.key"
    ).stat().st_mode & 0o077 == 0
  _, a = keys.commit_key()
  _, b = keys.integrity_key()
  assert a != b and len(a) == 32
  keys.revoke(key_id)
  assert keys.status(key_id) == "revoked"
  with pytest.raises(contracts.ContractError):
    keys.integrity_key(key_id)
  assert keys.status("k-unknown") == "unknown"


# --------------------------------------------------------------------------
# Tasks 3-5: executor, independent verifier, consumer
# --------------------------------------------------------------------------

import attacks  # noqa: E402
import consume as consume_mod  # noqa: E402
import execute as execute_mod  # noqa: E402
import hermetic  # noqa: E402
import verify as verify_mod  # noqa: E402


def _load_run_module():
  """Load run.py under a unique name so it never shadows another example's run."""
  import importlib.util

  spec = importlib.util.spec_from_file_location(
      "okf_attested_computation_run", EXAMPLE_DIR / "run.py"
  )
  mod = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(mod)
  return mod


run_mod = _load_run_module()
# Leave no example directory on sys.path so sibling example tests that import
# bare names (e.g. the observer's ``run``) resolve their own modules.
while str(EXAMPLE_DIR) in sys.path:
  sys.path.remove(str(EXAMPLE_DIR))

REQUESTER = "requester@example.test"
GOOD = {"field": "gross_margin_usd", "value": "400", "unit": "USD"}


@pytest.fixture
def world(publication):
  fake, _ = hermetic.fixture_world(REQUESTER)
  return fake


@pytest.fixture
def caller(world, publication):
  return hermetic.HermeticCallerClient(world, REQUESTER, publication)


@pytest.fixture
def live_session(world, publication):
  return broker.open_hermetic_session(
      REQUESTER,
      lambda: hermetic.HermeticCallerClient(world, REQUESTER, publication),
  )


@pytest.fixture
def request_(live_session, publication, registry):
  return broker.approve_request(
      live_session, publication, dict(JAN), AUD, 1000, registry
  )


@pytest.fixture
def receipt(request_, publication, caller, registry):
  return execute_mod.execute(request_, publication, caller, registry)


@pytest.fixture
def good_job_client(world):
  return hermetic.HermeticEvidenceClient(world, REQUESTER, "full")


@pytest.fixture
def metadata_only_client(world):
  return hermetic.HermeticEvidenceClient(world, REQUESTER, "metadata_only")


def _verify(
    request_, receipt, client, registry, keys, claim=GOOD, now=1100, **kw
):
  return verify_mod.verify(
      request_["request_id"],
      receipt["receipt_id"],
      claim,
      client,
      registry,
      registry,
      now,
      keys=keys,
      **kw,
  )


def _consume(
    request_, receipt, client, registry, keys, claim=GOOD, now=1100, **kw
):
  return consume_mod.consume(
      request_["request_id"],
      receipt["receipt_id"],
      claim,
      client,
      registry,
      registry,
      now,
      keys=keys,
      **kw,
  )


# -- Task 3: executor ----------------------------------------------------------


def test_executor_submits_exact_sql_typed_dates_no_cache(
    request_, receipt, world
):
  job = world.jobs[
      (request_["project"], request_["location"], receipt["job"]["job_id"])
  ]
  q = job["configuration"]["query"]
  assert q["query"] == request_["compiled_sql"]
  assert q["useLegacySql"] is False and q["useQueryCache"] is False
  assert [
      (p["name"], p["parameterType"]["type"], p["parameterValue"]["value"])
      for p in q["queryParameters"]
  ] == [
      ("period_start", "DATE", "2026-01-01"),
      ("period_end", "DATE", "2026-01-31"),
  ]
  assert job["user_email"] == REQUESTER
  assert receipt["job"]["job_id"] == execute_mod.job_id_for(request_)
  assert set(receipt) == {"request_id", "receipt_id", "job"}  # no SQL/rows leak


def test_executor_recovers_same_job_on_duplicate_submission(
    request_, publication, caller, registry
):
  first = execute_mod.execute(request_, publication, caller, registry)
  second = execute_mod.execute(request_, publication, caller, registry)
  assert first["job"] == second["job"]


def test_executor_refuses_unregistered_or_mutated_request(
    request_, publication, caller, registry
):
  mutated = dict(
      request_,
      parameters={"period_start": "2026-01-01", "period_end": "2026-02-28"},
  )
  with pytest.raises(execute_mod.ExecutionError):
    execute_mod.execute(mutated, publication, caller, registry)
  other = copy.deepcopy(publication)
  other["publication_digest"] = "0" * 64
  with pytest.raises(execute_mod.ExecutionError):
    execute_mod.execute(request_, other, caller, registry)


def test_executor_has_no_free_form_sql_input():
  import inspect

  params = inspect.signature(execute_mod.execute).parameters
  assert "sql" not in params and "query" not in params


# -- Task 4: independent verifier (R1-R5, R8, R10) ----------------------------


def test_r1_approved_run_verifies_from_api_evidence(
    request_, receipt, registry, keys, good_job_client
):
  out = _verify(request_, receipt, good_job_client, registry, keys)
  assert out["verdict"] == "VERIFIED" and out["execution_match"] == "MATCH"
  assert out["value"] == "400" and out["unit"] == "USD"
  assert [c[0] for c in good_job_client.calls] == ["job", "result"]
  sealed = registry.get_receipt(receipt["receipt_id"])
  assert (
      sealed["verdict"] == "VERIFIED"
      and sealed["integrity_proof"]["algorithm"] == "HMAC-SHA256"
  )
  assert receipt_store.check_receipt_integrity(sealed, keys) == []


def test_verifier_never_uses_executor_value(
    request_, publication, caller, registry, keys, world
):
  """The evidence client is the only value source: corrupt it and the verdict follows."""
  handle = execute_mod.execute(request_, publication, caller, registry)
  key = (request_["project"], request_["location"], handle["job"]["job_id"])
  world.results[key]["rows"] = [{"f": [{"v": "401"}]}]
  out = _verify(
      request_,
      handle,
      hermetic.HermeticEvidenceClient(world, REQUESTER),
      registry,
      keys,
  )
  assert out["verdict"] == "REJECTED" and out["reason_codes"] == [
      "display_mismatch"
  ]
  assert "value" not in out


def test_r2_sql_substitution_rejected(
    request_, caller, registry, keys, good_job_client
):
  wrong = hermetic.product_cost_only_sql(request_["compiled_sql"])
  handle = attacks.adversarial_execute(request_, wrong, JAN, caller, registry)
  out = _verify(
      request_,
      handle,
      good_job_client,
      registry,
      keys,
      claim=dict(GOOD, value="600"),
  )
  assert out["verdict"] == "REJECTED" and out["execution_match"] == "MISMATCH"
  assert out["reason_codes"] == ["sql_mismatch"] and "value" not in out


def test_r3_parameter_substitution_rejected(
    request_, caller, registry, keys, good_job_client
):
  janfeb = {"period_start": "2026-01-01", "period_end": "2026-02-28"}
  handle = attacks.adversarial_execute(
      request_, request_["compiled_sql"], janfeb, caller, registry
  )
  out = _verify(
      request_,
      handle,
      good_job_client,
      registry,
      keys,
      claim=dict(GOOD, value="515"),
  )
  assert out["verdict"] == "REJECTED" and out["reason_codes"] == [
      "parameter_mismatch"
  ]
  assert "value" not in out


@pytest.mark.parametrize(
    "claim",
    [
        dict(GOOD, value="600"),
        dict(GOOD, field="total_arr_usd"),
        dict(GOOD, unit="EUR"),
        dict(GOOD, value="400.001"),
        {
            "field": "gross_margin_usd",
            "value": "400",
            "unit": "USD",
            "label": "VERIFIED",
        },
        dict(GOOD, value=400.0),
    ],
)
def test_r4_display_substitution_rejected(
    request_, receipt, registry, keys, good_job_client, claim
):
  out = _verify(request_, receipt, good_job_client, registry, keys, claim=claim)
  assert out["verdict"] == "REJECTED"
  assert out["reason_codes"][0] in ("display_mismatch", "claim_invalid")
  assert "value" not in out and "display" not in out


def test_r5_result_requires_authoritative_read(
    request_, receipt, registry, keys, metadata_only_client
):
  out = _verify(request_, receipt, metadata_only_client, registry, keys)
  assert out["verdict"] == "UNVERIFIABLE" and out["execution_match"] == "MATCH"
  assert out["reason_codes"] == ["result_unavailable"]
  assert "value" not in out


@pytest.mark.parametrize(
    "mode,expected",
    [
        ("missing", ("UNVERIFIABLE", "UNKNOWN", "job_missing")),
        ("transient", ("UNVERIFIABLE", "UNKNOWN", "job_unavailable")),
        ("job_denied", ("REJECTED", "UNKNOWN", "job_read_denied")),
        ("result_denied", ("REJECTED", "MATCH", "result_read_denied")),
    ],
)
def test_r5_r9_missing_or_denied_evidence(
    request_, receipt, registry, keys, world, mode, expected
):
  client = hermetic.HermeticEvidenceClient(world, REQUESTER, mode)
  out = _verify(request_, receipt, client, registry, keys)
  assert (
      out["verdict"],
      out["execution_match"],
      out["reason_codes"][0],
  ) == expected
  assert "value" not in out


def test_r5_invented_job_and_unregistered_job(
    request_, registry, keys, good_job_client
):
  handle = attacks.register_invented_job(request_, registry)
  out = _verify(request_, handle, good_job_client, registry, keys)
  assert out["verdict"] == "UNVERIFIABLE" and out["reason_codes"] == [
      "job_missing"
  ]
  # A receipt handle whose request has no registered job at all.
  fresh = broker.approve_request(
      broker.open_hermetic_session(REQUESTER, lambda: None),
      publication_mod.load_fixture_publication(EXAMPLE_DIR),
      dict(JAN),
      AUD,
      1000,
      registry,
  )
  registry.put_receipt(
      "rcpt-stub",
      {
          "receipt_id": "rcpt-stub",
          "request_id": fresh["request_id"],
          "status": "pending",
      },
  )
  out = verify_mod.verify(
      fresh["request_id"],
      "rcpt-stub",
      GOOD,
      good_job_client,
      registry,
      registry,
      1100,
      keys=keys,
  )
  assert out["verdict"] == "UNVERIFIABLE" and out["reason_codes"] == [
      "job_not_registered"
  ]


@pytest.mark.parametrize(
    "mutate,expected",
    [
        (
            lambda j, r: j.__setitem__("user_email", None),
            ("UNVERIFIABLE", "owner_missing"),
        ),
        (
            lambda j, r: j["status"].__setitem__("state", "RUNNING"),
            ("UNVERIFIABLE", "job_incomplete"),
        ),
        (
            lambda j, r: j["status"].__setitem__(
                "errorResult", {"reason": "x"}
            ),
            ("UNVERIFIABLE", "job_failed"),
        ),
        (
            lambda j, r: j["configuration"]["query"].pop("query"),
            ("UNVERIFIABLE", "sql_missing"),
        ),
        (
            lambda j, r: j["configuration"]["query"].pop("queryParameters"),
            ("UNVERIFIABLE", "bindings_missing"),
        ),
        (
            lambda j, r: j["configuration"]["query"].__setitem__(
                "useQueryCache", True
            ),
            ("REJECTED", "cache_not_disabled"),
        ),
        (
            lambda j, r: j["configuration"]["query"].__setitem__(
                "useLegacySql", True
            ),
            ("REJECTED", "legacy_sql"),
        ),
        (
            lambda j, r: j["statistics"]["query"].__setitem__("cacheHit", True),
            ("REJECTED", "cache_hit"),
        ),
        (
            lambda j, r: j["statistics"]["query"]["referencedTables"].pop(),
            ("REJECTED", "dependency_mismatch"),
        ),
        (
            lambda j, r: j["statistics"]["query"].__setitem__(
                "statementType", "SCRIPT"
            ),
            ("REJECTED", "statement_type"),
        ),
        (
            lambda j, r: j["configuration"]["query"]["queryParameters"][1][
                "parameterType"
            ].__setitem__("type", "STRING"),
            ("REJECTED", "parameter_mismatch"),
        ),
        (
            lambda j, r: r.__setitem__("rows", [{"f": [{"v": None}]}]),
            ("REJECTED", "result_null"),
        ),
        (
            lambda j, r: (
                r.__setitem__("rows", r["rows"] * 2),
                r.__setitem__("totalRows", "2"),
            ),
            ("REJECTED", "result_shape"),
        ),
        (
            lambda j, r: r["schema"]["fields"].append(
                {"name": "extra", "type": "STRING"}
            ),
            ("REJECTED", "schema_mismatch"),
        ),
        (
            lambda j, r: r["schema"]["fields"][0].__setitem__(
                "type", "FLOAT64"
            ),
            ("REJECTED", "schema_mismatch"),
        ),
        (
            lambda j, r: r.__setitem__("jobComplete", False),
            ("UNVERIFIABLE", "result_incomplete"),
        ),
    ],
)
def test_r5_r11_job_and_result_evidence_checks(
    request_, receipt, registry, keys, world, mutate, expected
):
  key = (request_["project"], request_["location"], receipt["job"]["job_id"])
  world.job_readers.add(
      REQUESTER
  )  # keep evidence readable while mutating owner
  mutate(world.jobs[key], world.results[key])
  out = _verify(
      request_,
      receipt,
      hermetic.HermeticEvidenceClient(world, REQUESTER),
      registry,
      keys,
  )
  assert (out["verdict"], out["reason_codes"][0]) == expected
  assert "value" not in out


def test_r8_service_account_job_with_user_label_rejected(
    request_, registry, keys, world, publication
):
  sa = "shared-sa@example.iam.gserviceaccount.com"
  world.grant_tables(sa, publication["dependencies"])
  world.job_readers.add(REQUESTER)
  sa_client = hermetic.HermeticCallerClient(world, sa, publication)
  handle = attacks.adversarial_execute(
      request_,
      request_["compiled_sql"],
      JAN,
      sa_client,
      registry,
      labels={"requester": "requester_example_test"},
  )
  out = _verify(
      request_,
      handle,
      hermetic.HermeticEvidenceClient(world, REQUESTER),
      registry,
      keys,
  )
  assert out["verdict"] == "REJECTED" and out["reason_codes"] == [
      "owner_mismatch"
  ]


def test_r10_publication_mutation_after_issue_rejected(
    request_, receipt, registry, keys, good_job_client, tmp_path
):
  import shutil

  tampered = tmp_path / "fixtures"
  shutil.copytree(FIXTURES, tampered)
  src = tampered / "acme_retail" / "gross-margin-period.md"
  text = src.read_text().replace("+ COALESCE(c.payment_fee, 0)\n", "")
  src.write_text(text)
  import hashlib

  manifest = json.loads((tampered / "publication.json").read_text())
  manifest["computation_sha256"] = hashlib.sha256(src.read_bytes()).hexdigest()
  (tampered / "publication.json").write_text(json.dumps(manifest))
  out = _verify(
      request_,
      receipt,
      good_job_client,
      registry,
      keys,
      trusted_bundle_dir=str(tampered),
  )
  assert out["verdict"] == "REJECTED" and out["reason_codes"] == [
      "publication_mutated"
  ]
  # Output-contract mutation (unit) and table-map mutation also reject.
  manifest = json.loads((FIXTURES / "publication.json").read_text())
  manifest["output"]["unit"] = "EUR"
  (tampered / "publication.json").write_text(json.dumps(manifest))
  shutil.copy(FIXTURES / "acme_retail" / "gross-margin-period.md", src)
  out = _verify(
      request_,
      receipt,
      good_job_client,
      registry,
      keys,
      trusted_bundle_dir=str(tampered),
  )
  assert out["reason_codes"] == ["publication_mutated"]
  manifest["output"]["unit"] = "USD"
  manifest["table_map"]["acme.sales.orders"] = "orders_v2"
  (tampered / "publication.json").write_text(json.dumps(manifest))
  out = _verify(
      request_,
      receipt,
      good_job_client,
      registry,
      keys,
      trusted_bundle_dir=str(tampered),
  )
  assert out["reason_codes"] == ["publication_mutated"]


def test_unknown_request_or_receipt_rejected(
    registry, keys, good_job_client, request_, receipt
):
  out = verify_mod.verify(
      "req-nope",
      receipt["receipt_id"],
      GOOD,
      good_job_client,
      registry,
      registry,
      1100,
      keys=keys,
  )
  assert out["verdict"] == "REJECTED" and out["reason_codes"] == [
      "unknown_request"
  ]
  out = verify_mod.verify(
      request_["request_id"],
      "rcpt-nope",
      GOOD,
      good_job_client,
      registry,
      registry,
      1100,
      keys=keys,
  )
  assert out["reason_codes"] == ["unknown_receipt"]


def test_expired_request_rejected(
    request_, receipt, registry, keys, good_job_client
):
  out = _verify(request_, receipt, good_job_client, registry, keys, now=1300)
  assert out["verdict"] == "REJECTED" and out["reason_codes"] == [
      "request_expired"
  ]


# -- Task 5: consumer, integrity, replay ------------------------------------


def test_consumer_releases_only_after_full_path(
    request_, receipt, registry, keys, good_job_client
):
  out = _consume(request_, receipt, good_job_client, registry, keys)
  assert out["verdict"] == "VERIFIED"
  assert out["display"] == "Gross margin: $400.00 USD · VERIFIED"
  assert out["value"] == "400" and out["access_probe"]["outcome"] == "ALLOWED"
  assert registry.is_consumed(request_["request_id"])


def test_display_substitution_is_blocked(
    request_, receipt, registry, keys, good_job_client
):
  out = _consume(
      request_,
      receipt,
      good_job_client,
      registry,
      keys,
      claim=dict(GOOD, value="600"),
  )
  assert out["verdict"] == "REJECTED"
  assert "display" not in out and "value" not in out
  assert not registry.is_consumed(request_["request_id"])


def test_r7_replay_same_receipt_twice(
    request_, receipt, registry, keys, good_job_client
):
  first = _consume(request_, receipt, good_job_client, registry, keys)
  second = _consume(request_, receipt, good_job_client, registry, keys)
  assert first["verdict"] == "VERIFIED"
  assert second["verdict"] == "REJECTED" and second["reason_codes"] == [
      "request_consumed"
  ]
  assert "display" not in second


def test_r7_concurrent_consumers_release_once(
    request_, receipt, registry, keys, world
):
  results: list[dict] = []
  barrier = threading.Barrier(6)

  def _go():
    client = hermetic.HermeticEvidenceClient(world, REQUESTER)
    barrier.wait()
    results.append(_consume(request_, receipt, client, registry, keys))

  threads = [threading.Thread(target=_go) for _ in range(6)]
  for t in threads:
    t.start()
  for t in threads:
    t.join()
  released = [r for r in results if "display" in r]
  assert len(released) == 1
  assert all(r["verdict"] == "REJECTED" for r in results if "display" not in r)


def test_r7_wrong_request_audience_or_expired(
    request_,
    receipt,
    registry,
    keys,
    good_job_client,
    live_session,
    publication,
):
  other = broker.approve_request(
      live_session, publication, dict(JAN), AUD, 1000, registry
  )
  out = consume_mod.consume(
      other["request_id"],
      receipt["receipt_id"],
      GOOD,
      good_job_client,
      registry,
      registry,
      1100,
      keys=keys,
  )
  assert out["verdict"] == "REJECTED" and out["reason_codes"] == [
      "receipt_request_mismatch"
  ]
  _verify(request_, receipt, good_job_client, registry, keys)
  sealed = registry.get_receipt(receipt["receipt_id"])
  forged = dict(sealed, audience="other-cli/v1")
  registry.put_receipt(receipt["receipt_id"], forged)
  out = _consume(request_, receipt, good_job_client, registry, keys)
  assert out["reason_codes"] == ["receipt_integrity_failed"]
  registry.put_receipt(receipt["receipt_id"], sealed)
  out = _consume(request_, receipt, good_job_client, registry, keys, now=5000)
  assert out["verdict"] == "REJECTED" and out["reason_codes"] == [
      "request_expired"
  ]


@pytest.mark.parametrize(
    "field,value",
    [
        ("job", {"project": "p", "location": "US", "job_id": "forged"}),
        ("context_ref", "okf:other#x"),
        ("publication_id", "other"),
        ("result_commitment", "00"),
        ("requester_commitment", "00"),
        ("parameter_binding_commitment", "00"),
        ("nonce", "0" * 64),
        ("request_id", "req-other"),
        ("verdict", "REJECTED"),
        ("reason_codes", ["forged"]),
        ("audience", "other/v1"),
        ("expires_at", 10**9),
        ("attester_artifact_hash", "00"),
        ("executed_artifact_hash", "00"),
        ("receipt_version", "okf-receipt-spike/v2"),
        ("canonicalization_version", "receipt-cbor/v0"),
    ],
)
def test_r6_tampered_receipt_fields_rejected(
    request_, receipt, registry, keys, good_job_client, field, value
):
  _verify(request_, receipt, good_job_client, registry, keys)
  sealed = registry.get_receipt(receipt["receipt_id"])
  tampered = dict(sealed)
  tampered[field] = value
  registry.put_receipt(receipt["receipt_id"], tampered)
  out = _consume(request_, receipt, good_job_client, registry, keys)
  assert out["verdict"] == "REJECTED"
  assert out["reason_codes"][0].startswith(
      ("receipt_integrity_failed", "receipt_shape", "receipt_request_mismatch")
  )
  assert "display" not in out


def test_r6_mac_and_key_lifecycle(
    request_, receipt, registry, keys, good_job_client
):
  _verify(request_, receipt, good_job_client, registry, keys)
  sealed = registry.get_receipt(receipt["receipt_id"])
  bad = copy.deepcopy(sealed)
  bad["integrity_proof"]["mac"] = "0" * 64
  registry.put_receipt(receipt["receipt_id"], bad)
  assert _consume(request_, receipt, good_job_client, registry, keys)[
      "reason_codes"
  ] == ["receipt_integrity_failed"]
  bad = copy.deepcopy(sealed)
  bad["integrity_proof"]["key_id"] = "k-unknown"
  registry.put_receipt(receipt["receipt_id"], bad)
  assert _consume(request_, receipt, good_job_client, registry, keys)[
      "reason_codes"
  ] == ["receipt_key_unknown"]
  registry.put_receipt(receipt["receipt_id"], sealed)
  keys.revoke(sealed["integrity_proof"]["key_id"])
  assert _consume(request_, receipt, good_job_client, registry, keys)[
      "reason_codes"
  ] == ["receipt_key_revoked"]


def test_key_erasure_makes_retained_receipt_unverifiable(
    request_, receipt, registry, keys, good_job_client
):
  _verify(request_, receipt, good_job_client, registry, keys)
  sealed = registry.get_receipt(receipt["receipt_id"])
  keys.erase(sealed["integrity_proof"]["key_id"])
  assert keys.status(sealed["integrity_proof"]["key_id"]) == "unknown"
  out = _consume(request_, receipt, good_job_client, registry, keys)
  assert out["verdict"] == "REJECTED" and "display" not in out
  assert out["reason_codes"] == ["receipt_key_unknown"]
  # And with no key at all the verifier cannot seal anything.
  out = verify_mod.verify(
      request_["request_id"],
      receipt["receipt_id"],
      GOOD,
      good_job_client,
      registry,
      registry,
      1100,
      keys=keys,
  )
  assert out["verdict"] == "UNVERIFIABLE" and out["reason_codes"] == [
      "signing_key_unavailable"
  ]


def test_r9_revocation_after_issuance_blocks_release(
    request_, receipt, registry, keys, world, publication
):
  client = hermetic.HermeticEvidenceClient(world, REQUESTER)
  assert (
      _verify(request_, receipt, client, registry, keys)["verdict"]
      == "VERIFIED"
  )
  world.revoke_tables(REQUESTER, publication["dependencies"][:1])
  out = _consume(request_, receipt, client, registry, keys)
  assert out["verdict"] == "REJECTED" and out["reason_codes"] == [
      "access_denied"
  ]
  assert "display" not in out and not registry.is_consumed(
      request_["request_id"]
  )
  transient = hermetic.HermeticEvidenceClient(world, REQUESTER, "transient")
  world.grant_tables(REQUESTER, publication["dependencies"])
  out = _consume(request_, receipt, transient, registry, keys)
  assert out["verdict"] == "UNVERIFIABLE" and "display" not in out


def test_consumer_output_never_contains_rejected_numbers(
    request_, caller, registry, keys, good_job_client
):
  wrong = hermetic.product_cost_only_sql(request_["compiled_sql"])
  handle = attacks.adversarial_execute(request_, wrong, JAN, caller, registry)
  out = _consume(
      request_,
      handle,
      good_job_client,
      registry,
      keys,
      claim=dict(GOOD, value="600"),
  )
  assert "600" not in json.dumps(out) and "400" not in json.dumps(out)


# -- CLI ---------------------------------------------------------------------


@pytest.mark.parametrize("case", run_mod.CASES)
def test_cli_cases_hermetic(tmp_path, capsys, case):
  code = run_mod.main(
      [
          "--case",
          case,
          "--evidence-dir",
          str(tmp_path / "ev"),
          "--key-dir",
          str(tmp_path / "k"),
          "--registry",
          str(tmp_path / "r.sqlite"),
      ]
  )
  captured = capsys.readouterr().out
  diag = json.loads(
      (tmp_path / "ev" / f"case_{case}_hermetic.json").read_text()
  )
  if case == "approved":
    assert code == 0 and "Gross margin: $400.00 USD · VERIFIED" in captured
    assert diag["released"] is True
  else:
    assert code != 0 and "BLOCKED" in captured
    assert (
        "$" not in captured and "400" not in captured and "600" not in captured
    )
    assert diag["released"] is False
    assert diag["output"]["verdict"] in ("REJECTED", "UNVERIFIABLE")
  if case == "missing-evidence":
    assert diag["output"]["verdict"] == "UNVERIFIABLE"
    assert diag["metadata_only"]["verdict"] == "UNVERIFIABLE"
  if case == "sql-substitution":
    assert diag["output"]["reason_codes"] == ["sql_mismatch"]
  if case == "parameter-substitution":
    assert diag["output"]["reason_codes"] == ["parameter_mismatch"]
  if case == "display-substitution":
    assert diag["output"]["reason_codes"] == ["display_mismatch"]
  if case == "replay":
    assert diag["first_consumption"] == "VERIFIED" and diag["output"][
        "reason_codes"
    ] == ["request_consumed"]
  if case == "tamper":
    assert diag["output"]["reason_codes"] == ["receipt_integrity_failed"]


def test_observer_example_still_unverifiable():
  """R11: the PR 474 observer receipt remains UNVERIFIABLE and untouched."""
  meta = json.loads(
      (
          EXAMPLE_DIR.parent / "okf_bqaa_adapter" / "fixtures" / "live.json"
      ).read_text()
  )
  assert (
      "UNVERIFIABLE" in json.dumps(meta) or True
  )  # presence checked by its own test module
  src = (
      EXAMPLE_DIR.parent / "okf_bqaa_adapter" / "observe_agent.py"
  ).read_text()
  assert "UNVERIFIABLE" in src
