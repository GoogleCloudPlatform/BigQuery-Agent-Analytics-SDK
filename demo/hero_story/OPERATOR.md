# Operator track: fresh-project install

The presenter track ([README.md](README.md)) assumes this has already been
done once, rehearsed, and left standing. This file is the full path from an
empty GCP project to demo-ready.

## The clock, precisely

**≤30 minutes total, of which Cloud Build is ~10,** measured from
`preflight.sh` green to `verify --smoke` green. The clock **excludes** the
prerequisites below — install/auth failures are preflight findings, not
runbook time.

## Prerequisites (outside the clock)

| Requirement | Check | Notes |
|---|---|---|
| gcloud CLI, authenticated | `gcloud auth list` | Owner/Editor-equivalent on the target project |
| bq CLI | `bq version` | Ships with gcloud SDK |
| Billing enabled on project | `gcloud billing projects describe $PROJECT` | Cloud Build/Run refuse without it |
| Org policy allows `allUsers` invoker on Cloud Run | org-policy check in preflight | The receiver is bearer-token-authenticated at the app layer but publicly routable |
| BQAA producers package | `pip install "<repo>/producers[receiver]"` or `PYTHONPATH=producers/src` from a checkout | Provides `bqaa-otel` |
| Claude Code CLI, authenticated | `claude --version` + an interactive login done once | Session hangs without auth |
| Codex CLI ≥ 0.142.5, authenticated | `codex --version`; `~/.codex/auth.json` exists | Config shapes are version-pinned; see #317 |
| Region choice | default `us-central1` | Cloud Run + Artifact Registry region; BigQuery location default `US` |

Run `scripts/preflight.sh` — it verifies all of the above in under two
minutes with actionable messages. **Do not start the clock until it is
green.**

## Install (inside the clock)

```bash
export PROJECT=<your-project> DATASET=bqaa_hero_demo_$(date +%Y%m%d)
scripts/preflight.sh "$PROJECT" "$DATASET"

# Plan first (prints every command, mutates nothing), then execute:
bqaa-otel bootstrap --project "$PROJECT" --dataset "$DATASET" --build-from-source \
  --signals logs,metrics,traces --source claude-code,codex \
  --out demo/hero_story/evidence/artifacts
bqaa-otel bootstrap --project "$PROJECT" --dataset "$DATASET" --build-from-source \
  --signals logs,metrics,traces --source claude-code,codex \
  --out demo/hero_story/evidence/artifacts --execute
```

Every step is idempotent/convergent: if a step fails, the error output shows
the exact command and its stderr — fix the cause and re-run the same
command; it resumes.

Then install the generated artifacts:

- **Claude Code**: paste `claude-code.managed-settings.json` into the Claude
  admin console (Owner/Primary Owner; there is no admin API), or distribute
  via MDM (`claude-code.endpoint-managed.md`). For a laptop-only demo, the
  session script applies the same env vars directly.
- **Codex**: fill the literal bearer token into `codex.config.toml`
  (`gcloud secrets versions access latest --secret=bqaa-otlp-token`) and
  install as user-level `~/.codex/config.toml` (the demo scripts use an
  isolated `CODEX_HOME` instead of touching your real config). Codex does
  **not** expand `${ENV}` references in config headers — the file holds the
  literal secret; never commit it.

## Timing expectations

| Step | Time |
|---|---|
| preflight | < 2 min |
| bootstrap: APIs, dataset+DDL, secret, SAs, topics, IAM | ~3 min |
| bootstrap: Cloud Build image | ~8–10 min |
| bootstrap: Cloud Run deploys, subscription, DTS, artifacts | ~2 min |
| sessions + flush (`run_sessions.sh`) | ~4 min |
| `verify --smoke` | ~1–2 min |

## Troubleshooting (each of these happened in real runs)

| Symptom | Cause / fix |
|---|---|
| A `bq`/`gcloud` step fails with its stderr shown | Fix the stated cause; re-run bootstrap (convergent) |
| `verify --smoke` metric checks pass but product metric queries are empty | Metric counters flush on a cadence; the session script pins `OTEL_METRIC_EXPORT_INTERVAL` and waits — do not shorten the waits |
| Codex session hangs | stdin must be closed (`< /dev/null`), use `--skip-git-repo-check` outside a repo, and ensure `auth.json` exists in `CODEX_HOME` |
| Codex 401s at the receiver | The literal token was not filled into `config.toml` (env refs are not expanded) |
| Rows stamped with the wrong product | The `x-bqaa-source-product` header is missing from that exporter's config |
| Dead letters > 0 | Inspect `otlp_dead_letter.raw_b64` (replayable); the DLQ retention subscription keeps transport-level failures |

## Teardown

`scripts/teardown.sh` — dry-run by default, `--confirm` to delete; consumes
the resource inventory written at bootstrap time
(`evidence/demo_resources.json`) and removes only allowlisted demo
resources, then verifies the DTS scheduled MERGE is gone and the dataset no
longer exists. The dataset contains real telemetry: tear down promptly for
throwaway projects.

## Enterprise hardening before broad rollout (>200 users)

The pilot posture above is deliberately simple. Before company-wide
rollout, treat these as first-class admin operations (from an exec/security
review of this demo):

| Area | Pilot posture | Broad-rollout hardening |
|---|---|---|
| Token handling | one shared bearer token; Codex config holds it literally | documented rotation runbook (add secret version → redeploy → redistribute), consider per-team tokens; lost-laptop/leak response |
| Ingress | public Cloud Run + app-layer bearer auth (401-enforced) | private ingress / IAP / enterprise gateway; threat model, rate limits, audit logging, alerting |
| Config distribution | paste managed settings; Codex user-level file | MDM/managed dotfiles as the only channel; no hand-distributed secrets |
| Attribution taxonomy | `department`/`cost_center`/`env` recommended | REQUIRED and validated, or cost queries degrade to `unattributed` |
| Cost figures | estimate CTE with visible rates/as-of date | finance-grade rate table + model mapping before any billing use |
| Telemetry drift | verified against pinned versions (codex 0.142.5) | monthly compatibility smoke (`verify --smoke` + Q2 non-empty per product); shapes and encodings changed during this project's own verification |
| Retention | demo dataset torn down promptly | partition expiration + retention policy set at dataset creation |

## Version capture

`run_sessions.sh` records into `evidence/`: BQAA commit, `gcloud`/`bq`
versions, `claude --version`, `codex --version`, project/dataset/region,
signal + privacy tiers, and the `DEMO_RUN_ID`. Codex OTel behavior is
version-sensitive (#317) — the evidence must say what it was recorded
against.
