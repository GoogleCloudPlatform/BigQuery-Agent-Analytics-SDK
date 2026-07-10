<!-- Customer-first release notes (issue #349): rendered by
     scripts/render_release_notes.py (PR-tested — see
     tests/test_render_release_notes.py). Order is deliberate:
     install -> preflight -> bootstrap -> config artifacts -> verify ->
     cleanup. -->

## Install (no repository checkout)

```bash
pipx install bigquery-agent-analytics-tracing=={version}
```

## Deploy your telemetry warehouse

```bash
# 1. Readiness checks (mutates nothing; needs no build permissions):
bqaa-otel bootstrap --project $PROJECT --dataset $DATASET --preflight

# 2. Deploy (prints the plan first; --execute applies). Deploys the
#    released receiver image, pinned by digest:
bqaa-otel bootstrap --project $PROJECT --dataset $DATASET \
  --signals logs,metrics,traces --source claude-code,codex --execute

# 3. Fill the bearer token into the generated artifacts BEFORE
#    distributing them (Codex does NOT expand env vars in headers —
#    a literal <token> placeholder means every export gets a 401):
TOKEN=$(gcloud secrets versions access latest \
  --secret=bqaa-otlp-token --project $PROJECT)
sed -i.bak "s/<token>/${{TOKEN}}/g" codex.config.toml  # never commit this file
# Then distribute: Claude Code managed settings via admin console/MDM;
# Codex config.toml via managed dotfiles.

# 4. Prove the pipeline end to end:
BQAA_OTLP_TOKEN=$TOKEN bqaa-otel verify --smoke \
  --signals logs,metrics,traces --endpoint $URL \
  --project $PROJECT --dataset $DATASET

# 5. Clean removal when done — preview first, then execute:
bqaa-otel teardown --project $PROJECT --dataset $DATASET            # dry run
bqaa-otel teardown --project $PROJECT --dataset $DATASET --confirm  # deletes + existence-verifies
```

## Receiver image (pinned by digest)

```
{public_image}:{version}@{digest}
```

Tags are immutable; `latest` is never published. `SHA256SUMS` for all
artifacts is attached to this release.

## What's in this release (tracing package only)

- OTel-native OTLP receiver → BigQuery: native log/metric/span tables,
  dedup views, `agent_events_otlp` projection, transport DLQ (#316)
- `bqaa-otel` enterprise admin CLI: `config`, `bootstrap` (plan/execute/
  `--preflight`), `verify --smoke`, `teardown` — signal tiers and
  privacy tiers with an explicit content-logging acknowledgement gate
  (#324)
- Verified Codex telemetry contracts and deterministic `source_product`
  provenance for Claude Code + Codex in one schema (#317)
- Hardening found by real deployments: GA DCL grants, Cloud Run
  entrypoint fixes, OTLP enum-encoding normalization, generated-config
  correctness (#340, #342, #343)

## Verified product versions

Claude Code {claude_version} · Codex {codex_min_version} (verified
minimum — config shapes are version-pinned). Product telemetry drifts:
re-run the compatibility smoke monthly.

## Evaluation path

The rehearsed end-to-end demo (both products, real telemetry, privacy
proofs):
https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/tree/tracing-v{version}/demo/hero_story

---
