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

## Review fix pass (2026-09-06)

Astra requested changes on `67889b0` (one P1, six P2); Opus filed three
overlapping findings. Six were closed in `05fc445`; Astra's re-review
found P2 #5 only partially fixed (the consumer still judged expiry against
its entry timestamp after remote reads); `6407dcf` closed that, and a
second re-review found the default clock still truncated fractional wall
time (P2 #5b), closed in the commit after `0a13be0`. Every row is covered
by hermetic tests. `cases.json` and `live_cases.json` are regenerated at
the current head (attester artifact hash recorded per record); earlier
heads' CLI evidence was not retained separately.

| # | Finding | Fix | Test |
|---|---|---|---|
| P1 | sealed receipt with `receipt_version` deleted was treated as pending and reissued | only the exact executor handle `{receipt_id, request_id, status: pending}` is pending, and a pending handle is **not consumable** (`receipt_not_verified`); every other shape goes through the integrity check and fails as `receipt_integrity_failed`; a tampered record is never overwritten | `test_p1_deleted_receipt_version_is_rejected_not_reissued`, `test_pending_handle_is_not_consumable` |
| P2 | `Decimal.normalize()` under default precision collapsed distinct 38-digit NUMERIC values; `InvalidOperation` escaped | fixed 120-digit local context, 76-digit cap, every `ArithmeticError` mapped to `ContractError` at both the verifier and renderer | `test_decimal_string_is_exact_for_38_digit_numeric` |
| P2 | no hard cost ceiling | `HARD_MAX_BYTES_BILLED = 1 GiB`; a dry run above it refuses submission; the cap is `min(1 GiB, max(100 MiB, 4×dry-run))` | `test_executor_enforces_hard_cost_ceiling` |
| P2 | README marked transient-API and full R6/R7 rows as live | table now labels those rows hermetic only | n/a |
| P2 #5 | CLI captured the clock before approval and reused it; consumer then judged expiry only against its entry `now`, so a request expiring during `getQueryResults` / access probe still released | `run.py` reads the clock fresh per call; `contracts.trusted_clock` (injectable, entry-relative, never backwards) is re-sampled by the verifier after the result read and by the consumer after reads, probes and rendering; `Registry.try_consume` re-checks the deadline **inside** the `BEGIN IMMEDIATE` transaction and returns `expired` without inserting | `test_p2_5_expiry_crossed_during_consumer_reads_blocks_release` (result read / source probe / output probe), `test_p2_5_verifier_rechecks_expiry_after_result_read`, `test_p2_5_consumption_transaction_enforces_deadline`, `test_p2_5_release_still_works_just_inside_deadline`; Astra's `probes.py` `prior_5` now records REJECTED, no value, nonce unconsumed |
| P2 #5b | default `trusted_clock` computed `int(now) + int(elapsed)`, so fractional wall time lost alignment with the absolute deadline: entry wall 1299.75 (`now` 1299), read ends 1300.25, clock still 1299 → released after deadline 1300 | default clock is fractional and wall-aligned: anchor `now` to the whole second of entry, add the exact wall time elapsed since that second boundary, never run backwards; `Registry.try_consume` compares the fractional sample with the deadline (no integer cast); injected clocks unchanged | `test_p2_5b_fractional_deadline_crossing_blocks_on_default_clock` (result read / source probe / output probe / render, production clock with `time.time` patched), `test_p2_5b_fractional_deadline_crossing_blocks_through_cli_helper` (Astra's actual `run._issue_then_consume` boundary), `test_p2_5b_fractional_just_inside_deadline_still_releases` (liveness), `test_p2_5b_default_clock_is_wall_aligned_and_fractional`; Astra's `rereview-0a13be0/probes.py` fractional stages now record REJECTED / no value / nonce unconsumed |
| P2 | a failed display claim overwrote the sealed VERIFIED receipt | the sealed receipt records the evidence verdict only; the claim is bound at return/consume time; an authentic VERIFIED receipt is never downgraded, an UNVERIFIABLE one may be upgraded, a record failing integrity is kept as tamper evidence | `test_wrong_claim_does_not_overwrite_verified_receipt`, `test_verify_can_upgrade_unverifiable_but_never_downgrade_verified`; live R4 now runs all three wrong claims and then releases the honest one |
| P2 | rendering ran after `consume_once` | render first; a render failure returns `UNVERIFIABLE render_failed` with the nonce intact | `test_render_failure_does_not_spend_nonce` |

Also: committed evidence now uses `user:owner` / `sa:okf-receipt-restricted`
aliases (exact topology retained privately); the live suite redacts on
write. The verifier docstring no longer claims "no query methods".

Post-fix results: hermetic 101 receipt tests + 18 observer tests pass;
live suite 7/7 (64.8 s); CLI live cases approved → exit 0, six attack
cases → exit 2 with no number. One earlier live rerun hit a transient
"project does not have the reservation in the data region" error on three
jobs while no reservation existed in the project; the immediate rerun
passed and the error is recorded here rather than hidden.

## Authority chain (measured)

| Step | Principal / artifact | Evidence |
|---|---|---|
| Source pin | `knowledge-catalog` 31da799, `gross-margin-period.md` sha256 `5e96ae11…f0e7` | `fixtures/acme_retail/SOURCE.md`, digest re-checked on every load |
| Derived publication | `okf-receipt-spike/acme-retail-derived/gross-margin-period`, compiler `okf-receipt-compiler/v1` | `fixtures/publication.json`; compiled SQL digest `c8026298…636c` (same in every live receipt) |
| Session broker | requester from ADC tokeninfo: `user:owner` (kind `user`) | `broker.open_live_session` |
| Executor | user-delegated `google.cloud.bigquery.Client`, cache off, GoogleSQL, DATE bindings, `maximum_bytes_billed` = min(1 GiB, max(100 MiB, 4×dry-run)) | job resources below |
| Verifier | same requester's delegation, confined REST reads; a **second process** re-read the same job and produced identical `result_commitment` and `executed_artifact_hash` | `live_cases.json` → `R1_approved.fresh_process_verify` |
| Receipt / keys | HMAC-SHA256, separate commit + integrity keys, mode 0600, `keys.json` metadata | `receipt_store.KeyStore` |
| Consumer | sealed receipt required → MAC + key status → re-verify → commitment binding → access probe → render → `consume_once` | `consume.py` |

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
| `user:owner` (ADC user; exact identity retained privately) | project `roles/owner`, `bigquery.jobUser`, `bigquery.dataViewer`; dataset OWNER | requester for R1–R5, evidence reader, provisioning |
| `okf-receipt-restricted@test-project-0728-467323.iam.gserviceaccount.com` | created 23:40Z; project `roles/bigquery.jobUser` only; `user:owner` holds `roles/iam.serviceAccountTokenCreator` on it | real restricted principal for R8/R9 via impersonation; dataset READER granted and revoked inside the tests (timestamps in `live_cases.json`) |
| `bqaa-ci-sandbox@…`, `grafana-bq@…`, a second pre-existing owner user | pre-existing | **not used** |

Impersonation of a pre-existing sandbox SA was denied
(`iam.serviceAccounts.getAccessToken`), so a dedicated temporary SA was
created rather than modifying another workflow's identity.

## Test commands and results

```text
python -m pytest tests/examples/test_okf_attested_computation.py tests/examples/test_okf_bqaa_adapter.py -q
  119 passed (101 receipt + 18 observer; order-independent) after the fix pass
OKF_SPIKE_LIVE=1 GOOGLE_CLOUD_PROJECT=test-project-0728-467323 \
  python -m pytest tests/integration/test_okf_attested_computation_live.py -q
  7 passed in 53.57s (2026-09-05) and 7 passed in 64.78s (2026-09-06 fix pass)
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
