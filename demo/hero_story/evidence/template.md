# Demo run evidence — `DEMO_RUN_ID: <run_id>`

Copy this file per run; `scripts/run_sessions.sh` and `run_queries.sh`
fill most fields. This is the raw record behind `one_pager.md`.

## Redaction rules (enforced before anything leaves the team)

Screenshots and pasted outputs must contain **none** of the following:

- bearer tokens (any `Authorization` header content)
- personal emails or raw `user_id` values — use the SHA256 hashes the SQL
  pack already emits
- raw prompt or response text
- the full receiver URL when shared outside the org (show
  `https://bqaa-otlp-receiver-…run.app`)

## Versions (Codex OTel behavior is version-sensitive — #317)

| Component | Value |
|---|---|
| BQAA commit | `<git rev-parse --short HEAD>` |
| `bqaa-otel` invocation mode | installed package / `PYTHONPATH=producers/src` |
| gcloud | `<gcloud version | head -1>` |
| bq | `<bq version>` |
| Claude Code | `<claude --version>` |
| Codex | `<codex --version>` (minimum verified: 0.142.5) |

## Run parameters

| Parameter | Value |
|---|---|
| project / dataset / region | |
| signals tier | logs,metrics,traces |
| privacy tier | baseline |
| `DEMO_RUN_ID` (= `env` resource attribute) | |
| session start / end (UTC) | |

## Captured outputs

- [ ] `preflight.txt` — all checks green
- [ ] `bootstrap_plan.txt` — the plan-mode command listing (no secrets)
- [ ] `verify_smoke.txt` — all checks green (pipeline proof)
- [ ] `sql/NN_*.csv` — one result per pack query, this run id only
- [ ] `replay_refusal.txt` — the exit-2 privacy gate capture
- [ ] `demo_resources.json` — resource inventory (feeds teardown)

## Notes / anomalies

<!-- anything surprising, with links to issues filed -->
