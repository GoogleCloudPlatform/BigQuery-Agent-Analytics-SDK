# OKF result-bound receipt spike (Attested Computation, caller-delegated)

**spike · example-only · SYNTHETIC fixture · not a production receipt
service · Haiyuan is the merge gate**

One OKF v0.2 §10 Attested Computation (Acme Retail *gross margin for a
period*) is approved by a trusted broker, executed under the caller's own
BigQuery delegation, then **independently verified** by a separate code
path that re-reads `jobs.get` and `jobs.getQueryResults`. A deterministic
consumer releases the number only after the sealed receipt, the fresh
evidence, the claim and current access all agree, and the request nonce is
consumed exactly once. A pending handle is never consumable: the verifier
must seal a receipt first, and a wrong display claim never overwrites an
authentic VERIFIED receipt.

This is a follow-on to the PR 474 observer (`examples/okf_bqaa_adapter`),
which stays untouched and still emits an honest no-execution
`UNVERIFIABLE` receipt.

## What is demonstrated

| Case | Expected | Hermetic | Live (2026-09-05) |
|---|---|---|---|
| R1 approved January | MATCH / VERIFIED, releases `400.00 USD` | pass | pass, user-owned job |
| R2 product-cost-only formula (real job yields 600) | REJECTED `sql_mismatch` | pass | pass |
| R3 period_end moved to 2026-02-28 (real job yields 515) | REJECTED `parameter_mismatch` | pass | pass |
| R4 claim 600 / wrong field / wrong unit on a valid run | REJECTED `display_mismatch` | pass | pass |
| R5 invented job, metadata-only verifier | UNVERIFIABLE | pass | pass |
| R5 transient API failure | UNVERIFIABLE | pass | hermetic only |
| R6 mutated receipt fields / MAC / key revoked or erased | REJECTED | pass | tamper case only; field matrix and key lifecycle hermetic |
| R7 replay, concurrent consumers, wrong request, expired | one release only | pass | replay case only; concurrency and expiry hermetic |
| R8 service-account job with a user label | REJECTED `owner_mismatch` | pass | see `evidence/receipt/report.md` |
| R9 revocation after issuance, denied output | REJECTED, no cached release | pass | see `evidence/receipt/report.md` |
| R10 publication / table map / output contract mutated | REJECTED `publication_mutated` | pass | n/a (local) |
| R11 exact NUMERIC shape; observer stays UNVERIFIABLE | pass | pass | n/a |

Expected values (400 / 600 / 515) are **planned fixture expectations** in
`fixtures/expected.json`. Measured values live only under `evidence/`.

## Run

```bash
# hermetic (default): SYNTHETIC API emulation, no GCP
python examples/okf_attested_computation/run.py --case approved
python examples/okf_attested_computation/run.py --case sql-substitution
#   cases: approved | sql-substitution | parameter-substitution |
#          display-substitution | missing-evidence | tamper | replay

# live: real BigQuery jobs under your ADC user
export GOOGLE_CLOUD_PROJECT=test-project-0728-467323
python examples/okf_attested_computation/run.py --case approved --live \
    --key-dir ~/.okf-receipt-keys --registry ~/.okf-receipt/registry.sqlite

# tests
python -m pytest tests/examples/test_okf_attested_computation.py -q
OKF_SPIKE_LIVE=1 GOOGLE_CLOUD_PROJECT=test-project-0728-467323 \
    python -m pytest tests/integration/test_okf_attested_computation_live.py -q
```

Exit status is `0` only when the consumer prints
`Gross margin: $400.00 USD · VERIFIED`. Every blocked case exits `2` and
prints a verdict plus reason codes and **no number**. No flag skips
verification.

## Trust boundary

| Component | File | Trusted? | Notes |
|---|---|---|---|
| Session broker | `broker.py` | yes | requester from the real credential (tokeninfo), 256-bit nonce, audience, 5-minute expiry; typed DATE parameters only; reserved names rejected |
| Publication pin / compiler | `publication.py` | yes | source bytes digest-pinned; exactly one `sql` fence; allowlisted backticked table substitution only; single SELECT |
| Executor | `execute.py` | yes (caller-delegated) | dry-run-derived `maximum_bytes_billed`, cache off, GoogleSQL, broker job id; returns only `(project, location, job_id)` + opaque receipt id |
| Independent verifier | `verify.py` | yes | confined `jobs.get` + `getQueryResults`; compares owner, literal SQL, typed bindings, dialect, cache, statement type, referenced tables, one-cell NUMERIC via `Decimal` |
| Receipt store / keys | `receipt_store.py` | yes | private SQLite; separate commit/integrity keys, mode 0600; `consume_once` is atomic |
| Consumer | `consume.py` | yes | MAC + key status, re-verify, commitment binding, current access probe, consume, render |
| Adversarial executor | `attacks.py` | **no** | test-only free-form SQL; never on the trusted path |
| API emulation | `hermetic.py` | **no** | SYNTHETIC resources for hermetic runs |

The agent can only choose the declared computation, supply typed parameter
values, and present an opaque receipt id plus a display claim. It cannot
supply SQL, identity, job evidence, keys, publication content or a verdict.

## Verdict contract

`execution_match ∈ {MATCH, MISMATCH, UNKNOWN}`; `verdict ∈ {VERIFIED,
UNVERIFIABLE, REJECTED}`. Missing or unreachable evidence is
`UNVERIFIABLE`; proven contradiction or a conclusive 403 is `REJECTED`.
Both block release. `VERIFIED` is a per-call outcome and is not OKF
`verified:` metadata; nothing here is labelled `ATTESTED`.

## Receipt projection

`receipt_version okf-receipt-spike/v1`, `profile_contract_version
okf-context/1`, `canonicalization_version receipt-cbor/v1` (the PR 474
adapter's canonical CBOR over a restricted domain; vectors pinned in
`fixtures/cbor_vectors.json`). Keyed commitments protect low-entropy
requester / parameter / result values; the integrity proof is
`HMAC-SHA256` over the payload minus `integrity_proof` with a separate key.
This is a local MAC profile, not a portable third-party signature.

## Fixture

`fixtures/fixture.sql` provisions seven SYNTHETIC tables in
`test-project-0728-467323.okf_receipt_spike_20260905` (US, 30-day table
expiration). `fixtures/acme_retail/` is a verbatim copy of the Acme source
with attribution (`SOURCE.md`); `fixtures/publication.json` is the derived
manifest that remaps the fictional `acme.*` identifiers.

## Limitations

- Local HMAC custody, no key rotation policy, no third-party signature.
- Process/code independence of the verifier, not a different BigQuery
  principal.
- The sample SQL is not a validated accounting implementation.
- `CURRENT_DATE()` in the source makes results date-dependent; the fixture
  orders are old enough for the 30-day window during the spike.
- Not an SDK API; example scope only.
