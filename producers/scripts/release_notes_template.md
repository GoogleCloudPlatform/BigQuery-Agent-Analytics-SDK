<!-- Customer-first release notes (issue #349): rendered by the release
     workflow with version/digest; auto-generated commit notes are
     appended below by the GitHub release. Order is deliberate:
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

# 3. Distribute the generated config artifacts (Claude Code managed
#    settings via admin console/MDM; Codex config.toml via managed
#    dotfiles) — written to --out with a do-not-commit token warning.

# 4. Prove the pipeline end to end:
BQAA_OTLP_TOKEN=... bqaa-otel verify --smoke \
  --signals logs,metrics,traces --endpoint $URL \
  --project $PROJECT --dataset $DATASET

# 5. Clean removal when done (dry-run by default, existence-verified):
bqaa-otel teardown --project $PROJECT --dataset $DATASET
```

## Receiver image (pinned by digest)

```
{public_image}:{version}@{digest}
```

Tags are immutable; `latest` is never published. `SHA256SUMS` for all
artifacts is attached to this release.

## Verified product versions

Claude Code {claude_version} · Codex {codex_min_version} (verified
minimum — config shapes are version-pinned). Product telemetry drifts:
re-run the compatibility smoke monthly.

## Evaluation path

The rehearsed end-to-end demo (both products, real telemetry, privacy
proofs) lives in [`demo/hero_story/`](../../demo/hero_story/).

---
