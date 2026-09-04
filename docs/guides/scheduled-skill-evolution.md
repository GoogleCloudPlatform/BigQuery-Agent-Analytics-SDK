# Evolve agent skills on a schedule — quality reports in, pull requests out

This runbook takes the skill-evolution engine
([`scripts/skill_evolution.py`](../../scripts/README.md#skill-evolution)) from
a manual CLI run to a **hands-off weekly production loop**: a Cloud Run Job
reads the sessions your agent already writes to BigQuery (via the SDK's
analytics plugin), scores them with `scripts/quality_report.py`, and — only
when quality is below your threshold — evolves the agent's `SKILL.md` and
opens a pull request against your agent repo with the evidence attached.

The deep reference (flags, env contract, hook signatures, IAM table,
troubleshooting) is the
[skill-evolution job README](../../deploy/skill_evolution_job/README.md).
This guide is the narrative: what the loop does, what you must provide, and
what arrives in the PR.

## The shape

```text
agent_events (BigQuery)          Cloud Run Job (weekly):
  your agent's sessions,   ───►    1. quality_report.py --time-period 7d
  written by the SDK's              2. gate: meaningful_rate >= 95%? exit 0
  analytics plugin                  3. evolve_skill(): parallel analysts read
                                       failure trajectories on a frozen skill,
                                       inductive consolidation keeps recurring
                                       rules (Trace2Skill)
                                    4. optional host hooks: score candidates
                                       with YOUR eval, gate with YOUR tests
                                    5. branch + commit + PR on your agent repo
```

Three properties make this safe to leave running:

- **Quiet when healthy.** At or above the quality threshold the run exits
  without touching your repo. No weekly PR noise.
- **Evidence-bound.** Every rule the evolved skill gains traces back to
  failure trajectories in the quality report; the PR body carries the
  before/after metrics and failure counts.
- **You keep the merge button.** The job's output is a PR, not a deploy.
  Nothing changes in production until a human merges (or until you wire the
  optional `publish` hook to your own promotion pipeline).

## What you provide

1. **Sessions in BigQuery.** Your agent runs with the SDK's analytics plugin
   writing `agent_events`. That's the only data source the default loop
   needs.
2. **An agent registry** in your repo — a small JSON file mapping agent
   names to skill directories (multi-agent repos list several; `order`
   controls co-evolution sequence).
3. **A GitHub token** in Secret Manager, scoped to the agent repo
   (contents + pull requests, read/write).
4. Optionally, **host hooks** — your own scoring and gating. Without them
   the loop still works: the engine picks candidates with its built-in
   heuristic and the PR notes that candidates were unscored. With a `score`
   hook (run your agent over your eval set against a candidate skill) the
   winner is chosen by measured quality, and with `GATE_POLICY=require` +
   a `gate` hook your test suite becomes a hard pre-PR gate.

## Deploy

```bash
cd deploy/skill_evolution_job
./deploy.sh \
  --project my-project \
  --region us-central1 \
  --dataset agent_analytics \
  --github-repo my-org/my-agent-repo \
  --agent-registry agent_registry.json \
  --gh-secret skill-evolution-gh \
  --gcs-bucket my-project-skill-evolution \
  --smoke
```

One command: service accounts (split runtime/scheduler by default), IAM
grants (BigQuery read-only on the events dataset), image build, Cloud Run
Job, weekly Cloud Scheduler trigger, and a smoke execution that must print
`SELF-TEST PASS`.

Cadence defaults to Mondays 09:00 (`--schedule "0 9 * * 1"`). The first
scheduled fire needs `MIN_SESSIONS` (default 20) sessions in the window —
below that the job logs `NOTHING TO DO` and exits 0 (or calls your `traffic`
hook to generate evaluation sessions, if you configured one).

## What arrives in the PR

- The evolved `SKILL.md` (and only that file), on a
  `skill-evolution/<agent>-<version>-<timestamp>` branch.
- A body with: the quality report the run started from, meaningful-rate
  before → after (when a `score` hook measured candidates), failure counts
  by category, and the run's artifact location in GCS.
- Full run artifacts in `gs://<bucket>/skill_evolution_runs/<timestamp>/`:
  the report, every candidate, per-candidate scores, and the consolidation
  trace — enough to audit why each rule made it in.

Review it like any teammate's PR. If the diff looks wrong, close it — the
next run starts from the unmerged incumbent, so rejecting a PR costs
nothing.

## Variations

- **No GitHub credentials** — omit `--github-repo`/`--gh-secret`: artifacts
  go to GCS only, you apply skills manually.
- **Multi-agent repos** — register several agents; `--mode coevolve`
  (or `EVOLUTION_MODE=coevolve`) evolves them in registry order,
  re-measuring between agents; `--mode auto` (the default) first
  classifies which agent owns the failures and evolves only that one.
- **Issue-driven** — `--batch` consumes open `[quality]` GitHub issues
  (e.g. filed by your monitoring) instead of running its own report;
  `--from-issue N` targets one.
- **Local first** — the whole loop runs on a laptop against a local
  checkout: see “Local dry run” in the
  [job README](../../deploy/skill_evolution_job/README.md#local-dry-run-no-cloud).
