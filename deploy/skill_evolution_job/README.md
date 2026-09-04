# Scheduled Skill Evolution — Cloud Run Job

A self-sustained deployment surface that runs the SDK's skill-evolution
engine ([`scripts/skill_evolution.py`](../../scripts/README.md#skill-evolution))
unattended: a weekly Cloud Run Job reads your agent's **BigQuery quality
report**, decides whether the agent's `SKILL.md` needs improving, evolves it
with the Trace2Skill engine, and **opens a pull request** against your agent
repo with the evolved skill, before/after metrics, and the failure evidence
that drove every change.

```text
Cloud Scheduler (weekly)
    └─> Cloud Run Job
          1. quality_report.py over your agent_events dataset   (BigQuery)
          2. quality gate: meaningful_rate ≥ threshold? → done  (no PR noise)
          3. evolve_skill(): parallel analysts + consolidation  (Vertex AI)
          4. optional host hooks: traffic / score / gate        (your code)
          5. PR against your agent repo with the evolved SKILL.md (GitHub)
```

The evolution loop itself is driven by an ADK agent with 16 tools
(`run_quality_report`, `run_evolution`, `compare_versions`,
`create_evolution_pr`, …), so a single scheduled execution can reason
end-to-end: *report → diagnose → evolve → verify → publish*.

**Adopt, don't fork:** the container carries no copy of the engine logic.
It imports the same `scripts/skill_evolution.py` and `scripts/quality_report.py`
that ship in this repo (staged into the image by `deploy.sh`), so engine
improvements land in the job by rebuilding the image.

The job feature-detects the engine it was given: `error_analyst` /
`toolbox` hooks (agentic analysts) and the incumbent-guarded candidate
selection need an engine whose `evolve_skill` accepts `error_analyst_fn`,
`tools` and `incumbent_score`. On an engine without them the job logs
which keyword it dropped and falls back to single-pass analysts and
engine-side (size-based) selection. To bake a different engine than this
checkout's, point `deploy.sh --scripts-dir` at another SDK checkout's
`scripts/` directory; the image is then reproducible from the flag rather
than from files copied over by hand.

## Quick start (PR mode — recommended)

Prerequisites: `gcloud` + `python3` locally; a BigQuery dataset where the
SDK's analytics plugin writes `agent_events`; a GitHub repo containing your
agent's `SKILL.md`.

1. **Add an agent registry to your agent repo** (e.g. `agent_registry.json`
   at the repo root — see [Agent registry](#agent-registry)):

   ```json
   {
     "repo_root": ".",
     "default_app_name": "my_agent_app",
     "agents": {
       "my_agent": {
         "skill_dir": "agents/my_agent/skill",
         "label": "My production agent",
         "order": 0
       }
     }
   }
   ```

2. **Store a GitHub token** (fine-grained PAT with *contents: read/write*
   and *pull requests: read/write* on the agent repo) in Secret Manager:

   ```bash
   printf '%s' "$GITHUB_TOKEN" | \
     gcloud secrets create skill-evolution-gh --data-file=- --project my-project
   ```

3. **Deploy** — one command creates the service accounts, IAM grants,
   image, Cloud Run Job, and the weekly Cloud Scheduler trigger:

   ```bash
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

   `--smoke` executes the job once with `--test` and requires the
   `SELF-TEST PASS` sentinel in its logs (engine located, registry parsed,
   tools registered, hooks resolved).

4. Every Monday 09:00 (override with `--schedule`), the job runs the full
   loop. If the last 7 days of sessions score **at or above the quality
   threshold** (default 95% meaningful rate), it exits quietly. Below it,
   you get a PR titled `Evolve <agent> skill to v<N> (<before>% -> <after>%)`
   with the evolved `SKILL.md` and a metrics table.

Tear down with `./deploy.sh --project my-project --region us-central1 --down`.

## GCS-only fallback (no GitHub credentials)

Omit `--github-repo` / `--gh-secret` and pass only `--gcs-bucket`. The job
still builds the quality report and runs evolution, but publishes nothing:
each run's artifacts (report, evolved skill, per-candidate scores,
consolidation trace) are uploaded to
`gs://<bucket>/skill_evolution_runs/<timestamp>/` for you to review and
apply manually. In this mode the registry must be reachable some other way —
either bake it into a custom image or point `AGENT_REGISTRY` at an absolute
path mounted into the container.

## Agent registry

`agent_registry.json` tells the job where skills live inside your repo:

| Field | Required | Meaning |
|-------|----------|---------|
| `repo_root` | no | Anchor for relative `skill_dir`s (default: the cloned repo root; falls back to the registry file's directory in local dry-runs) |
| `default_app_name` | no | Default `app_name` filter for the quality report |
| `agents.<name>.skill_dir` | yes | Directory containing the agent's `SKILL.md` (plus optional `references/*.md`) |
| `agents.<name>.label` | no | Human-readable description (used in prompts and PRs) |
| `agents.<name>.order` | no | Evolution order for multi-agent (co-evolution) runs; defaults to declaration order. Override at runtime with `EVOLUTION_ORDER=a,b,c` |
| `agents.<name>.app_name` | no | Per-agent report filter (falls back to `default_app_name`) |
| `agents.<name>.skill_id` | no | Stable identifier for host-side publishing hooks |

See [`agent_registry.example.json`](agent_registry.example.json).

## Host hooks (optional, but they close the loop)

The job is generic; four capabilities are inherently **host-specific** and
are delegated through hooks. Every hook is optional — an unconfigured hook
is skipped with a logged reason, and the loop degrades gracefully (e.g.
candidate selection falls back to the engine's heuristic when no `score`
hook exists).

Set `EVOLUTION_HOOKS=my_hooks_module`. The module is imported from the
job's own path first; if that fails it is retried with the host-repo
clone (the job's workdir) on `sys.path` — so the adapter can simply live
in your agent repo (e.g. `EVOLUTION_HOOKS=eval.my_hooks`). Packages the
adapter needs beyond the job's own go into the image via
`deploy.sh --extra-requirements your_requirements.txt`. The module may
define any subset of:

| Hook | Signature | Purpose |
|------|-----------|---------|
| `traffic` | `traffic(run_dir) -> dict` | Generate fresh eval sessions when BigQuery has fewer than `MIN_SESSIONS` |
| `score` | `score(candidate_path, skill_dir, run_dir) -> dict` | Score one candidate `SKILL.md` with your own agent + eval set; must return `{"meaningful_rate": <0-100>, ...}` |
| `gate` | `gate(run_dir, version, agent) -> (bool \| None, str)` | Pre-publish acceptance check (e.g. run your test suite against the evolved skill); only an explicit `False` blocks the PR — `None` means inconclusive and proceeds |
| `toolbox` | `toolbox(agent) -> str` | Text description of the agent's tools, injected into analyst prompts |
| `error_analyst` | `error_analyst(client, model, session, skill, tools)` | Custom per-failure analyst (only used when the engine supports `error_analyst_fn`; see `--scripts-dir` above) |
| `publish` | `publish(skill_dir, run_dir)` | Push the accepted skill to a registry/deployment target after the PR |

`traffic`, `score` and `gate` also accept a **shell-command fallback** via
`TRAFFIC_CMD` / `SCORE_CMD` / `GATE_CMD` for hosts whose tooling isn't
importable Python. Placeholders `{run_dir}`, `{report}`, `{candidate}`,
`{skill_dir}`, `{agent}` are substituted before execution
(`HOOK_CMD_TIMEOUT_S` bounds each call, default 3600s):

```bash
SCORE_CMD='python eval/score.py --skill {candidate} --out {run_dir}/score.json'
GATE_CMD='pytest tests/skill_contract -q'
```

- `SCORE_CMD` contract: exit 0 and print, as the **last stdout line**, either
  a bare number or a JSON object containing `meaningful_rate`.
- `GATE_CMD` contract: exit code decides pass/fail; the output tail becomes
  the gate reason. `GATE_POLICY=require` makes a missing/failing gate block
  the PR (default `skip`: missing gate logs and proceeds).

A module hook always wins over its `*_CMD` fallback; a broken
`EVOLUTION_HOOKS` import fails loudly rather than silently skipping.

## Execution modes

The container entrypoint is `python main.py`; scheduled fires use env-driven
defaults, and `gcloud run jobs execute --args` reaches every CLI mode:

| Invocation | What it does |
|------------|--------------|
| *(no args, `FULL_LOOP=true`)* | Full loop: report → sufficiency check → quality gate → evolve → PR |
| `--test` | Self-test; prints `SELF-TEST PASS` (used by `deploy.sh --smoke`) |
| `--report path.json --mode <agent>` | Evolve one agent from an existing report (no BigQuery) |
| `--mode coevolve` | Multi-agent co-evolution in registry order |
| `--mode bottleneck` | Classify which agent is responsible for current failures |
| `--batch` | Process open `[quality]` GitHub issues (requires `GITHUB_REPO`; `EVOLUTION_MIN_OPEN_ISSUES` gates the run) |
| `--from-issue N` | Evolve from one specific quality issue |

Useful knobs: `--rounds`, `--candidates`, `--min-failures`, `--run-dir`,
`--trace-labels K=V`, `--quality-source bigquery|synthetic`,
`--agent-registry PATH`.

## Environment reference

Set by `deploy.sh` (override with `gcloud run jobs update --update-env-vars`):

| Variable | Default | Meaning |
|----------|---------|---------|
| `PROJECT_ID` | — | GCP project (also accepts `GOOGLE_CLOUD_PROJECT`) |
| `DATASET_ID` / `DATASET_LOCATION` | — / `US` | BigQuery events dataset |
| `TABLE_ID` | `agent_events` | Events table read by `scripts/quality_report.py` |
| `AGENT_REGISTRY` | — | Registry path; relative → inside the repo clone |
| `GITHUB_REPO` / `GITHUB_BASE_BRANCH` | — / `main` | Agent repo for clone + PRs; unset = dry-run (no PRs) |
| `GH_TOKEN` | — | GitHub token (wired from Secret Manager by `deploy.sh`) |
| `FULL_LOOP` | unset | `true` = scheduled full-loop behavior |
| `EVOLUTION_GCS_BUCKET` | — | Run-artifact bucket (also accepts `GCS_BUCKET`) |

Tuning (all optional):

| Variable | Default | Meaning |
|----------|---------|---------|
| `EVAL_TIME_PERIOD` | `7d` | Report window |
| `MIN_SESSIONS` | `20` | Minimum sessions before evolving (below: `traffic` hook or a clean `NOTHING TO DO` exit) |
| `QUALITY_THRESHOLD` | `0.95` | Meaningful-rate gate; at/above = no evolution |
| `QUALITY_APP_NAME` | registry default | Report `app_name` filter |
| `EVOLUTION_TRACE_LABELS` | — | `K=V,K2=V2` report label filters |
| `SKILL_EVOLUTION_MODEL_ID` | `gemini-2.5-pro` | Analyst/consolidation model |
| `EVOLUTION_MODEL_ID` / `EVAL_MODEL_ID` | `gemini-2.5-pro` / — | Orchestrating agent / judge models |
| `EVOLUTION_MODE` | `evolve` | Default mode for scheduled fires |
| `EVOLUTION_TARGET_AGENTS` / `EVOLUTION_ORDER` | — | Restrict / reorder co-evolution |
| `EVOLUTION_CANDIDATES` / `EVOLUTION_MAX_ROUNDS` | auto | Candidate count / round cap. Both are binding: `run_evolution` and `run_coevolution` refuse rounds past the cap (per agent) and use the bound candidate count over whatever the orchestrating agent asks for |
| `EVOLUTION_TOOLBOX` | — | Toolbox text (literal or `@/path/to/file`) |
| `GATE_POLICY` | `skip` | `require` = missing/failing gate blocks the PR |
| `EVOLUTION_PUBLISH` | `false` | Gates **real** PR/issue creation. `false` = local previews only (`pr_preview.md` / issue file in the run dir), even with `GITHUB_REPO` set. `deploy.sh` sets it to `true` when both `--github-repo` and `--gh-secret` are wired |
| `GIT_USER_NAME` / `GIT_USER_EMAIL` | `skill-evolution-job` | Commit identity on evolution branches |
| `EVOLUTION_WORKDIR` | — | Use an existing checkout instead of cloning (local runs) |
| `HOOK_CMD_TIMEOUT_S` | `3600` | Per-`*_CMD` timeout |

## Local dry run (no cloud)

```bash
pip install -e ".[llm]" google-adk

# Toy host repo: a git checkout with SKILL.md + agent_registry.json.
EVOLUTION_WORKDIR=/path/to/agent-repo \
AGENT_REGISTRY=agent_registry.json \
python deploy/skill_evolution_job/main.py \
  --report quality_report.json --mode my_agent --run-dir /tmp/evo_run
```

Evolution model calls go to Vertex AI via your ADC. With `EVOLUTION_WORKDIR`
unset and no `GITHUB_REPO`, the job runs in dry-run mode: evolution happens
against registry paths resolved relative to the registry file, artifacts stay
in `--run-dir`, and PR/publish steps are skipped with logged reasons.
`python deploy/skill_evolution_job/main.py --test` validates an environment
without touching BigQuery or the model.

## IAM summary

| Identity | Role | Scope | Why |
|----------|------|-------|-----|
| runtime SA | `roles/bigquery.jobUser` | project | quality-report queries |
| runtime SA | `roles/bigquery.dataViewer` | events dataset | read-only event access |
| runtime SA | `roles/aiplatform.user` | project | Gemini calls (evolution + judge) |
| runtime SA | `roles/secretmanager.secretAccessor` | the GH secret | GitHub token |
| runtime SA | `roles/storage.objectAdmin` | the runs bucket | artifact upload/download |
| scheduler SA | `roles/run.invoker` | the job | fire scheduled executions |

`--single-sa` collapses the two identities for non-production setups.

## Operations notes

- **Task timeout**: default 14400s (4h). A full loop is LLM-bound; for
  many-agent registries or large evolve sets raise it:
  `--task-timeout 28800`.
- **Retries are deliberately 0**: a retried half-finished run could open
  duplicate PRs. The next scheduled fire is the retry.
- **Idempotence / noise control**: the quality gate means a healthy agent
  produces no PRs, and `MIN_SESSIONS` prevents evolving on thin evidence
  (`NOTHING TO DO` exit, code 0).
- **Cost**: one full run ≈ one quality report (BigQuery + judge calls over
  the window) + `candidates × analysts` Gemini calls. Bound it with
  `EVOLUTION_CANDIDATES`, `EVAL_TIME_PERIOD`, and the weekly cadence.
- **Troubleshooting**: run `--test` via
  `gcloud run jobs execute bqaa-skill-evolution --args=--test --wait`, then
  read the execution logs; every skipped capability logs exactly which
  variable would enable it.
