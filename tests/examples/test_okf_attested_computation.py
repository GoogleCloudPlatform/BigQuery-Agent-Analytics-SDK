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
  return broker.open_hermetic_session(
      "requester@example.test", lambda: None
  )


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
        {"period_start": "2026-01-01", "Period_End": "2026-01-31", "period_end": "2026-01-31"},
        {"period_start": "2026-01-01", "period_end": "2026-01-31", "requester": "x"},
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
    broker.approve_request(session, publication, dict(JAN), "other/v1", 1000, registry)
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
    assert (tmp_path / "keys" / f"{key_id}.{role}.key").stat().st_mode & 0o077 == 0
  _, a = keys.commit_key()
  _, b = keys.integrity_key()
  assert a != b and len(a) == 32
  keys.revoke(key_id)
  assert keys.status(key_id) == "revoked"
  with pytest.raises(contracts.ContractError):
    keys.integrity_key(key_id)
  assert keys.status("k-unknown") == "unknown"
