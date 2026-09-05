# Receipt spike evidence report

Date: 2026-09-05 · Branch `spike/okf-result-bound-receipt-20260905` (off
upstream `main` 22d44db, PR 474 merge 4f54b5c is an ancestor) · Author:
Haiyuan's agent (Fable 5.1) · Reviewer / merge gate: Haiyuan.

## Verdict

**Receipt slice: credible isolated pass (Y).** One caller-delegated
Attested Computation (Acme gross margin, January 2026) executed as a real
user-owned BigQuery job, was independently verified from `jobs.get` +
`jobs.getQueryResults` (`MATCH` / `VERIFIED`), and the trusted consumer
released `Gross margin: $400.00 USD · VERIFIED` exactly once. The
product-cost-only substitution (real job value 600), the period
substitution (real job value 515), display substitution, a
service-account job with a user label, revocation after issuance, denied
output, receipt tampering and replay were all blocked with no value
released. Missing evidence stayed `UNVERIFIABLE`.

This earns **MODERATE delivery for receipts only** per the intent. It is
not `ATTESTED`, not a combined-runtime pass, and does not depend on or
compensate for the graph spike.

## Authority chain (measured)

| Step | Principal / artifact | Evidence |
|---|---|---|
| Source pin | `knowledge-catalog` 31da799, `gross-margin-period.md` sha256 `5e96ae11…f0e7` | `fixtures/acme_retail/SOURCE.md`, digest re-checked on every load |
| Derived publication | `okf-receipt-spike/acme-retail-derived/gross-margin-period`, compiler `okf-receipt-compiler/v1` | `fixtures/publication.json`; compiled SQL digest `c8026298…636c` (same in every live receipt) |
| Session broker | requester from ADC tokeninfo: `raincoatrun@gmail.com` (kind `user`) | `broker.open_live_session` |
| Executor | user-delegated `google.cloud.bigquery.Client`, cache off, GoogleSQL, DATE bindings, `maximum_bytes_billed` = max(100 MiB, 4×dry-run) | job resources below |
| Verifier | same requester's delegation, confined REST reads; a **second process** re-read the same job and produced identical `result_commitment` and `executed_artifact_hash` | `live_cases.json` → `R1_approved.fresh_process_verify` |
| Receipt / keys | HMAC-SHA256, separate commit + integrity keys, mode 0600, `keys.json` metadata | `receipt_store.KeyStore` |
| Consumer | MAC + key status → re-verify → commitment binding → access probe → `consume_once` → render | `consume.py` |

## Live evidence (2026-09-05, project `test-project-0728-467323`, location US)

Fixture dataset `okf_receipt_spike_20260905`: 7 SYNTHETIC tables created
23:38Z with 30-day default table expiration; provisioning digest of
`fixtures/fixture.sql` = `940aacdc…f124`. Dataset ACL after the run is
back to the defaults plus the owner (verified 23:58Z).

| Row | Case | Job id (all `okf_rcpt_…`, owner) | Verifier / consumer outcome | Released |
|---|---|---|---|---|
| R1 | approved January | `9e98122903b09728ef305564_9c851d3d5c44e0c8` (user) | MATCH / VERIFIED, fresh-process VERIFIED, consumer `$400.00 USD` | **yes, once** |
| R2 | product-cost-only SQL | `236396cc55221c6df44201d4_6bc5866c7d85eccb` (user), real result 600 | MISMATCH / REJECTED `sql_mismatch` | no |
| R3 | period_end 2026-02-28 | `a1ca0ac0d57e3bf5e873f299_210b78e7849efbb3` (user), real result 515 | MISMATCH / REJECTED `parameter_mismatch` | no |
| R4 | claim 600 / wrong field / wrong unit | `0d5dee6d11a98c7551a44b04_c890dce166d831b4` (user) | MATCH / REJECTED `display_mismatch` ×3; request not consumed | no |
| R5 | invented job id; metadata-only verifier | none / real job | UNKNOWN / UNVERIFIABLE `job_missing`; MATCH / UNVERIFIABLE `result_unavailable` | no |
| R6 | stored receipt job_id mutated after sealing (CLI `tamper`) | real job | REJECTED `receipt_integrity_failed` | no |
| R7 | same receipt twice (CLI `replay`) | real job | first VERIFIED, second REJECTED `request_consumed` | once |
| R8 | job submitted by `okf-receipt-restricted@…` SA with label `requester=<user>` against the user's request | `2af7ac1fcedc0d6dcc17985b_be9060b46c4e965f` (SA) | MISMATCH / REJECTED `owner_mismatch` | no |
| R9a | restricted SA as requester, dataset READER granted, approved run | `e351983f98432d5ac5119a18_7e0d4556bbda7dd2` (SA) | MATCH / VERIFIED before revocation | not consumed |
| R9b | restricted SA verifying the **user's** job | user job | UNKNOWN / REJECTED `job_read_denied` | no |
| R9c | dataset grant revoked, then consume | same SA job | MATCH / REJECTED `access_denied`, cached result not released | no |

Per-job measurements (from `jobs.get`): every job `cache_hit=False`,
`total_bytes_processed` 495–591 B, `total_bytes_billed` 73,400,320 B
(7 tables × 10 MiB minimum), `slot_millis` 34–59. Approximately 18 billed
jobs across preflight, CLI and pytest, about 1.3 GB billed in total; dry
runs are free. Policy propagation for the dataset ACL revocation: revoked
23:56:38.8Z, first denied probe 23:57:00Z (≈22 s).

Machine-readable: `preflight.json` (Day-1 probe), `cases.json` (7 CLI
cases hermetic + live), `live_cases.json` (pytest live rows).

## Identity topology (recorded, no tokens exported)

| Principal | Grants | Role in spike |
|---|---|---|
| `raincoatrun@gmail.com` (ADC user) | project `roles/owner`, `bigquery.jobUser`, `bigquery.dataViewer`; dataset OWNER | requester for R1–R5, evidence reader, provisioning |
| `okf-receipt-restricted@test-project-0728-467323.iam.gserviceaccount.com` | created 23:40Z; project `roles/bigquery.jobUser` only; `raincoatrun@gmail.com` holds `roles/iam.serviceAccountTokenCreator` on it | real restricted principal for R8/R9 via impersonation; dataset READER granted and revoked inside the tests (timestamps in `live_cases.json`) |
| `bqaa-ci-sandbox@…`, `grafana-bq@…`, `haiyuan@google.com` | pre-existing | **not used** |

Impersonation of a pre-existing sandbox SA was denied
(`iam.serviceAccounts.getAccessToken`), so a dedicated temporary SA was
created rather than modifying another workflow's identity.

## Test commands and results

```text
python -m pytest tests/examples/test_okf_attested_computation.py tests/examples/test_okf_bqaa_adapter.py -q
  112 passed (94 receipt + 18 observer; order-independent)
OKF_SPIKE_LIVE=1 GOOGLE_CLOUD_PROJECT=test-project-0728-467323 \
  python -m pytest tests/integration/test_okf_attested_computation_live.py -q
  7 passed in 53.57s (R1 + fresh-process rerun, R2, R3, R4, R5, R8, R9)
python -m pytest tests/integration/test_okf_attested_computation_live.py -q   (no env)
  7 skipped   <- a skipped run is NOT acceptance evidence
python examples/okf_attested_computation/run.py --case <case> [--live]
  approved -> exit 0 and the display line; all six attack cases -> exit 2, no number
bash autoformat.sh ; isort --check-only ; pyink --check   -> clean
python -m pytest -q (full repo)  -> see "Blocked / limitations"
```

## Blocked / limitations (honest list)

- **Full-repo pytest**: `tests/test_cli_agent_tool.py` fails at
  collection in this local environment (`ImportError:
  GEN_AI_TOOL_DEFINITIONS` from `opentelemetry.semconv`, a google.adk vs
  opentelemetry-semconv version mismatch). It reproduces on the base
  commit with the spike files absent and is unrelated to this change;
  CI on the PR is the authoritative full-suite run.
- **Verifier principal**: process/code independence only. The verifier
  reads with the same requester's delegation, as the spec allows; it is
  not a separate constrained service identity.
- **Key custody**: local HMAC keys in an operator-private directory
  (pytest tmp dirs, erased with the session; CLI live keys under
  `/tmp/okf-spikes/receipt-impl/keys`, retention 24 h then erase). No
  rotation policy, no portable signature.
- **R6/R7 breadth live**: only the `tamper` and `replay` CLI cases ran
  live; the full field-mutation, key-revocation, key-erasure, concurrent
  consumer and expiry rows are hermetic.
- **R10** (publication / table map / output contract mutation) and
  transient-API rows are hermetic by nature.
- **Data quality** is unproven: the sample SQL is not a validated
  accounting implementation; `CURRENT_DATE()` makes results
  date-dependent (fixture orders are >30 days old).
- **Tooling note**: one ad-hoc in-process impersonation probe was denied
  by the local agent permission classifier; the same path inside the
  pytest live suite ran and produced the R8/R9 evidence above.

## Cleanup and retention

- Fixture tables expire automatically 30 days after creation
  (≈2026-10-05). Dataset-level SA grant already removed.
- Keep `okf-receipt-restricted` SA and the token-creator binding until the
  2026-09-19 evidence checkpoint so Haiyuan can rerun the live suite; then:
  ```bash
  gcloud projects remove-iam-policy-binding test-project-0728-467323 \
    --member=serviceAccount:okf-receipt-restricted@test-project-0728-467323.iam.gserviceaccount.com \
    --role=roles/bigquery.jobUser
  gcloud iam service-accounts delete okf-receipt-restricted@test-project-0728-467323.iam.gserviceaccount.com
  bq rm -r -f -d test-project-0728-467323:okf_receipt_spike_20260905
  ```
- After key erasure the retained CLI receipts become `receipt_key_unknown`
  → REJECTED at the consumer and UNVERIFIABLE as evidence, by design.
- Nothing belonging to another session was modified or deleted.
