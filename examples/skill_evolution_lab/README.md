# Skill Evolution Lab

An agent that **rewrites its own skill** from its conversation traces — no
teacher model, no managed optimizer. One company-policy Q&A agent starts with a
deliberately flawed `SKILL.md`, generates traffic, and the SDK's evolution
engine reads the failing trajectories and produces a small, tool-first V1 skill.
The skill is versioned in the **Gemini Enterprise Agent Platform Skill
Registry** (V0 = revision 1, V1 = revision 2).

The point of the example is the **closed loop** — trace → golden-grounded score →
evolve → re-score, all attributable because only the skill file changes. V0 is
deliberately crippled (told to ignore a tool that already has the answers), so
the large V0→V1 delta *illustrates* the loop finding and fixing a real defect; a
plausibly-written baseline would gain less. See [`VERIFICATION.md`](VERIFICATION.md#what-this-proves--and-what-it-doesnt).

This is the runnable companion to the blog post *"Your Agent Can Write Its Own
Skill"* (BigQuery Agent Analytics Series). See
[`VERIFICATION.md`](VERIFICATION.md) for a recorded end-to-end run.

## What it shows

- **Self-improvement from traces.** The engine
  (`scripts/skill_evolution.py`, imported here — not copied) partitions scored
  conversations into successes/failures, runs a fleet of parallel analysts, and
  consolidates recurring rules into a versioned `SKILL.md`.
- **Ground-truth scoring.** Quality is graded against a golden Q&A answer key
  (`eval/eval_spec.json`) via the SDK's `quality_report.py`, not a
  no-ground-truth "usefulness" guess.
- **The anti-parroting rule.** Multi-turn cases where the user asserts a *wrong*
  correction. A good agent re-verifies with its tool and holds the right figure
  instead of caving. The engine detects parroting and learns a "re-verify, don't
  just agree" rule.
- **Skill Registry versioning.** The evolved skill is mirrored to the registry
  as a new immutable revision; `reset.sh` reverts both the local copy and the
  registry to V0.

## Layout

```text
skill_evolution_lab/
  agent/
    agent.py           # genai agent factory: SKILL.md (instruction) + tools
    tools.py           # lookup_company_policy + get_current_date (the data)
    skill_registry.py  # REST client for the Skill Registry (create/update/...)
  skills/
    SKILL.md           # working copy (starts as the flawed V0)
    SKILL.v0.md        # immutable flawed V0 baseline (used by reset)
  eval/
    eval_spec.json                  # scope + golden Q&A answer key (ground truth)
    questions_evolve.json           # questions the skill evolves from
    questions_test.json             # held-out questions for the V0->V1 number
    questions_corrections.json      # anti-parroting cases (teach)
    questions_corrections_heldout.json  # anti-parroting cases (held-out)
  run_agent.py          # runs questions through the agent -> conversations JSON
  analyze_and_evolve.py # scored report -> evolve_skill() -> V1 (+ registry)
  compare_runs.py       # V0 vs V1 golden-grounded correctness + parroting
  registry_cli.py       # create/update/delete/inspect registry revisions
  print_rate.py         # one-line golden-rate printer (used by run_e2e_demo.sh)
  run_e2e_demo.sh       # the whole cycle, one command (one model)
  run_sweep.sh          # run_e2e across models x seeds -> mean[range] table
  aggregate_sweep.py    # aggregate a sweep into the VERIFICATION table
  setup.sh / reset.sh   # write .env / revert to V0 (local + registry)
  sample_run/           # a committed end-to-end run (scored reports, evolved
                        #   skill, RESULT) + README explaining each artifact
```

A complete recorded run lives in [`sample_run/`](sample_run/) — the scored V0/V1
reports, the evolved skill, and `RESULT.md` — so you can read the exact inputs and
outputs (and what each file means) without running anything. Live runs write to
`runs/<timestamp>/` (git-ignored).

## Prerequisites

- A GCP project with Vertex AI enabled; `roles/aiplatform.user`.
- `gcloud auth application-default login`.
- [`uv`](https://github.com/astral-sh/uv) — the scripts run via `uv run`, which
  installs the repo's dependencies from the root `pyproject.toml` automatically
  on first use, so there's no separate install step.
- Gemini 3.x models are served from the Vertex `global` endpoint (handled
  automatically); the Skill Registry is regional (`us-central1` by default).

## Run it

```bash
cd examples/skill_evolution_lab
./setup.sh YOUR_PROJECT_ID us-central1      # writes .env, resets to V0
./run_e2e_demo.sh                           # V0 -> evolve -> V1 -> compare
```

The run deploys the flawed V0, generates and scores traffic on the evolve and
held-out test sets, evolves a tool-first V1 skill, re-scores the held-out set,
prints the V0→V1 comparison, and restores V0. Artifacts land in
`runs/<timestamp>_<model>/` (git-ignored), with `RESULT.md` as the summary.

**Runtime:** about **15–18 minutes** end-to-end at the default size (55 evolve +
55 held-out questions, almost entirely Gemini API calls); `setup.sh` takes a few
seconds. The first run also does a one-time `uv` dependency sync.

**What you'll see** — a per-step progress trace ending in the comparison table:

```text
[V0] traffic + score ...
     V0 test:   18.2% (10/55 golden-matched)
[evolve] analyst=gemini-3.1-pro-preview (this is the slow step) ...
[V1] traffic + score ...
     V1 test:   100.0% (55/55 golden-matched)

| Metric                    | V0 (flawed)   | V1 (evolved)   | Delta   |
| Overall                   | 18.2% (10/55) | 100.0% (55/55) | +81.8pp |
```

Numbers vary slightly run-to-run (LLM nondeterminism), but the direction is
stable. See [`VERIFICATION.md`](VERIFICATION.md) for a recorded + reproduced run.

### With the Skill Registry

```bash
WITH_REGISTRY=1 SKILL_ID=skill-lab-policy ./setup.sh YOUR_PROJECT_ID us-central1
WITH_REGISTRY=1 SKILL_ID=skill-lab-policy ./run_e2e_demo.sh
WITH_REGISTRY=1 SKILL_ID=skill-lab-policy ./reset.sh   # revert local + registry
```

Inspect revisions any time: `uv run python registry_cli.py revisions
--skill-id skill-lab-policy`.

### Model overrides

```bash
# Default agent is gemini-3.5-flash; try other GA models:
AGENT_MODEL=gemini-3.1-flash-lite ./run_e2e_demo.sh
AGENT_MODEL=gemini-2.5-pro ./run_e2e_demo.sh
```

`AGENT_MODEL` is the agent under test; `ANALYST_MODEL` runs the evolution
analysts/consolidator; `JUDGE_MODEL` (default `gemini-2.5-flash`, regional)
scores. The model, tools, and questions are fixed across V0 and V1 — only the
skill changes — so any delta is attributable to the skill.

## How it relates to the research

The engine follows [Trace2Skill](https://arxiv.org/abs/2603.25158) (parallel
analysts + inductive consolidation, held-out validation) and
[AutoSkill](https://arxiv.org/abs/2603.01145) (versioned skill evolution as a
semantic merge). It is the same `evolve_skill()` the knowledge-supervisor
quality lab imports from this SDK.
