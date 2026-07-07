# Skill Evolution Lab

An agent that **rewrites its own skill** from its conversation traces — no
managed optimizer, no hand-written patches (an analyst LLM only *diagnoses* the
traces; it never supplies the answer — the default analyst is a stronger model
than the agent, and the patches it produces carry behavioral rules, zero facts). One company-policy Q&A agent starts with a
deliberately flawed `SKILL.md`, generates traffic, and the SDK's evolution
engine reads the failing trajectories and produces a small, tool-first V1 skill.
The skill is versioned in the **Gemini Enterprise Agent Platform Skill
Registry** (V0 = revision 1, V1 = revision 2).

The point of the example is the **closed loop** — trace → golden-graded score →
evolve → re-score, all attributable because only the skill file changes. V0 is
deliberately crippled with **two** realistic defects — told to ignore a tool
that already has the answers, and told to accept user "corrections" at face
value (so it genuinely parrots wrong figures back) — so the large V0→V1 delta
*illustrates* the loop finding and fixing real defects; a plausibly-written
baseline would gain less. See [`VERIFICATION.md`](VERIFICATION.md#what-this-proves--and-what-it-doesnt).

Why BQAA makes this reusable: the Agent Analytics plugin captures the whole
improvement substrate — the conversation, the **tool calls (name + args)**, the
user corrections, and the outcome labels — in one analyzable place. The evolution
engine turns those traces into behavioral skill rules and validates them on
held-out traffic before creating a new skill revision. (This lab writes that same
schema to local JSON so it runs without BigQuery; in production the traces come
straight from the BQAA tables.)

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
  correction. V0's second defect ("accept their correction") makes the agent
  genuinely cave and repeat the user's wrong number; the scorer tags those
  sub-trajectories `parroted` from the trace, the engine reclassifies the fake
  wins to failures, and the evolved skill learns a "re-verify with the tool,
  don't just agree" rule.
- **Tool selection.** The agent has two meaningful tools —
  `lookup_company_policy` (facts) and `calculate_disability_pay` (a computed
  payout). The evolved skill learns *which* tool to reach for, not just "use a tool."
- **Out-of-scope handling.** The held-out set includes out-of-scope questions
  (IT, personal finance). The evolved skill learns to decline them deliberately
  (route IT to IT) instead of reflexively deflecting everything to HR.
- **Skill Registry versioning.** The evolved skill is mirrored to the registry
  as a new immutable revision — only after it beats V0 on the held-out
  comparison (a losing V1 is never pushed); `reset.sh` reverts both the local
  copy and the registry to V0.

## Layout

```text
skill_evolution_lab/
  agent/
    agent.py           # genai agent factory: SKILL.md (instruction) + tools
    tools.py           # lookup_company_policy (facts) + calculate_disability_pay (math) + get_current_date
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
    questions_oos.json                  # out-of-scope cases (teach)
    questions_oos_heldout.json          # out-of-scope cases (held-out)
  run_agent.py          # runs questions through the agent -> conversations JSON
  analyze_and_evolve.py # scored report -> evolve_skill() -> V1 (+ registry)
  compare_runs.py       # before/after golden-graded correctness + parroting (any two versions)
  registry_cli.py       # create/update/delete/inspect registry revisions
  print_rate.py         # one-line golden-rate printer (used by run_e2e_demo.sh)
  run_e2e_demo.sh       # the whole cycle, one command (one model)
  run_sweep.sh          # run_e2e across models x seeds -> mean[range] table
  aggregate_sweep.py    # aggregate a sweep into the VERIFICATION table
  setup.sh / reset.sh   # write .env / revert to V0 (local + registry)
  sample_run/           # a committed end-to-end run (scored reports, V0 + evolved
                        #   skill, RESULT) + round2/ companion (V1->V2 gate) + README
```

A complete recorded run lives in [`sample_run/`](sample_run/) — the scored V0/V1
reports, the V0 and evolved skills, and `RESULT.md` — so you can read the exact inputs and
outputs (and what each file means) without running anything. Live runs write to
`runs/<timestamp>/` (git-ignored).

## Prerequisites

- A GCP project; `roles/aiplatform.user` (plus rights to enable services on the
  first run — `setup.sh` enables the **Vertex AI API** for you).
- `gcloud auth application-default login`.
- [`uv`](https://github.com/astral-sh/uv) — the scripts run via `uv run`, which
  installs the repo's dependencies from the root `pyproject.toml` automatically
  on first use, so there's no separate install step.
- `jq` (optional) — only used for the golden-count banner in `run_e2e_demo.sh`;
  without it the banner shows `?` and everything else works.
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

**Runtime:** about **10 minutes** end-to-end at the default size (68 evolve +
80 held-out questions, almost entirely Gemini API calls) on the default
`gemini-3.1-flash-lite`; `--rounds 2` roughly doubles it. `setup.sh` takes a
few seconds. The first run also does a one-time `uv` dependency sync.

**What you'll see** — a per-step progress trace ending in the comparison table.
The per-step rate is over the *in-scope* questions matched to the answer key (the
out-of-scope ones have no golden entry and are scored separately as declines):

```text
  ▶ STEP 1/4: V0 BASELINE (flawed skill)
     V0 test:   28.6% (20/70 matched to the answer key, of 80 total; 10 out-of-scope)
  ▶ STEP 2/4: EVOLVE THE SKILL   (analyst=gemini-3.1-pro-preview -- the slow step)
  ▶ STEP 3/4: MEASURE V1 (held-out)
     V1 test:   100.0% (70/70 matched to the answer key, of 80 total; 10 out-of-scope)
  ▶ STEP 4/4: COMPARE V0 vs V1

| Metric                    | V0 (flawed)   | V1 (evolved)   | Delta    |
| Overall                   | 37.5% (30/80) | 97.5% (78/80)  | +60.0pp  |
| Corrections (anti-parrot) | 0.0% (0/15)   | 100.0% (15/15) | +100.0pp |
```

Numbers vary slightly run-to-run (LLM nondeterminism), but the direction is
stable. See [`VERIFICATION.md`](VERIFICATION.md) for a recorded + reproduced run.

### A second round (V0 -> V1 -> V2)

```bash
./run_e2e_demo.sh --rounds 2
```

Round 2 runs only when V1 beat V0. It replays the evolve set on V1 (fresh
learning signal: what does V1 *still* get wrong?), evolves V1 -> V2 with
`--version-label v2` (so `v2_patches.json`, `v2_candidates/`, and
`v2_selection.txt` land beside the `v1_*` artifacts instead of overwriting
them), measures V2 on the same held-out set, and keeps V2 **only when it beats
V1** — otherwise the incumbent V1 stays and `RESULT_ROUND2.md` +
`v2_selection.txt` record why. Both outcomes are the demo working as designed:
a gain proves the loop compounds; a kept incumbent proves the guard holds. A
recorded round-2 run is committed at
[`sample_run/round2/`](sample_run/round2/) — its V2 *tied* V1 and the gate
kept the incumbent.

### Score from BigQuery (the production wiring)

```bash
./run_e2e_demo.sh --from-bigquery
```

By default the demo writes traces to local JSON in the exact schema the Agent
Analytics plugin logs, so it runs without BigQuery. `--from-bigquery` exercises
the production wiring instead: `run_agent.py --log-bigquery` inserts every
session into a real `agent_events` table (created on first use;
`DATASET_ID`/`TABLE_ID` from `.env`, defaults `agent_analytics.agent_events`)
in the plugin's row shape — `USER_MESSAGE_RECEIVED` / `TOOL_STARTING` /
`TOOL_COMPLETED` / `LLM_RESPONSE` spans in true chronological order, with
`root_agent_name` and per-run `custom_tags` — and scoring reads the sessions
back through the SDK's BigQuery trace path (`quality_report.py --label
run=<id> --label slice=<set>`). The scorecards come back identical to the
local path: verdicts, parroting sub-trajectories, and structured tool calls
(the BigQuery path now populates `tool_calls_detail` from the `TOOL_*` spans).
Requires the BigQuery API enabled and table read/write + job permissions on
the project.

### With the Skill Registry

```bash
WITH_REGISTRY=1 SKILL_ID=skill-lab-policy ./setup.sh YOUR_PROJECT_ID us-central1
WITH_REGISTRY=1 SKILL_ID=skill-lab-policy ./run_e2e_demo.sh
WITH_REGISTRY=1 SKILL_ID=skill-lab-policy ./reset.sh   # revert local + registry
```

The registry push happens *after* the held-out comparison (Step 4) and only
when the new version beats the incumbent — the loop's "keep the new skill only
when it wins" property is enforced, not just described. With `--rounds 2` the
pushed revision is whichever version won the final gate (V2 if it beat V1,
else V1).

Inspect revisions any time: `uv run python registry_cli.py revisions
--skill-id skill-lab-policy`.

### Model overrides

```bash
# Default agent is gemini-3.1-flash-lite (it obeys the flawed V0 most
# literally, so the default run reproduces the headline); try other GA models:
AGENT_MODEL=gemini-3.5-flash ./run_e2e_demo.sh
AGENT_MODEL=gemini-2.5-pro ./run_e2e_demo.sh
```

`AGENT_MODEL` is the agent under test; `ANALYST_MODEL` runs the evolution
analysts/consolidator; `JUDGE_MODEL` (default `gemini-2.5-flash`, regional)
scores. The model, tools, and questions are fixed across V0 and V1 — only the
skill changes — so any delta is attributable to the skill.

### Multi-model sweep (long-running job)

`run_sweep.sh` runs the whole e2e for every model × seed and aggregates the
result into the **mean [min–max] table in [`VERIFICATION.md`](VERIFICATION.md)**
— so one unlucky run can't masquerade as the headline. At the default size (4
models × 3 seeds) it takes **~3–4 hours**, so it self-logs to
`runs/SWEEP_<ts>.log` and is safe to detach:

```bash
# Foreground (prints live progress, then the final table):
./run_sweep.sh

# Background (survives logout); tail progress, read the table when done:
nohup ./run_sweep.sh >/dev/null 2>&1 &
tail -f runs/SWEEP_*.log     # live progress
cat  runs/SWEEP_*.md         # final mean [range] table per model

# Subset / fewer seeds:
MODELS="gemini-3.5-flash gemini-2.5-pro" SEEDS=2 ./run_sweep.sh
```

A single failed run (API blip, quota) is logged and skipped — the sweep keeps
going and still aggregates whatever completed. To re-read a finished sweep
without re-running, point the aggregator at its manifest:
`uv run python aggregate_sweep.py --manifest runs/sweep_<ts>.tsv`.

### Reusing the engine on your own agent

`analyze_and_evolve.py` is a thin wrapper; the engine
([`scripts/skill_evolution.py`](../../scripts/skill_evolution.py)) is reusable
directly from the CLI, no example code required:

```bash
uv run python ../../scripts/skill_evolution.py \
    --report your_quality_report.json --skill your_SKILL.md -o evolved.md \
    --eval-spec your_eval_spec.json \   # its `tools` field makes analysts tool-aware
    --artifacts-dir out/               # patches, best-of-N candidates, selection.txt
```

`--tools "<freeform description>"` is an alternative to `--eval-spec` when you
don't have a spec file. The agent runner ([`run_agent.py`](run_agent.py)) records
each session's structured tool calls (`tool_calls_detail: [{name, args}]`), which
the scorer carries through so the analysts — and `compare_runs.py`'s tool-selection
table — can reason about *which* tool was chosen, not just how many calls happened.

## How it relates to the research

The engine follows [Trace2Skill](https://arxiv.org/abs/2603.25158) (parallel
analysts + inductive consolidation, held-out validation) and
[AutoSkill](https://arxiv.org/abs/2603.01145) (versioned skill evolution as a
semantic merge). It is the same `evolve_skill()` the knowledge-supervisor
quality lab imports from this SDK.

Here's how each paper's design maps to what this lab actually builds:

| Dimension | Trace2Skill | AutoSkill | This lab |
| --- | --- | --- | --- |
| Learning signal | Execution trajectories | User-dialogue turns | Scored conversation traces -- wins and misses, logged to BigQuery by the Agent Analytics plugin |
| When it learns | Batch (consolidate a pool) | Online (after each interaction) | Batch, once per round (`V0 -> V1 -> V2`) |
| Analysts | Multi-turn agentic (ReAct) loops with file access | -- (background skill extraction, no analyst fleet) | One single-pass LLM call per trajectory, on a frozen copy of the skill |
| Consolidation / merge | Hierarchical tree merge | `P_merge` semantic merge, per observation | Flat, prevalence-weighted, best-of-N; an accumulative semantic union that never drops a section |
| Validation | Held-out; evolve/test disjoint (§2.1) | Online evaluation | Golden-Q&A-grounded score on a disjoint held-back set |
| Scale & models | 200+ trajectories, cross-model transfer, multi-domain; Qwen, self-hosted | 22K+ real conversations (WildChat-1M) | 68 study + 80 held-out, one domain; Gemini 3.x, Gemini 2.5 Pro |

Full accumulative `P_merge` across rounds and online skill retrieval are future work.

### What we add on top of both

- **Golden-Q&A grading.** Every answer is graded against a verified golden answer, so the
  loop's fitness function is real correctness -- not an LLM judge's "sounds helpful," which
  happily rewards confident, wrong answers.
- **Anti-parroting, detect-then-learn.** The scorer works from the run's trace: a recovery
  counts as genuine only when the agent actually called a tool to re-check the fact. Echo the
  user's correction back with no tool call and it is flagged as parroting and reclassified
  from success to failure, so the engine never reinforces a fake win.
- **Best-of-N with an incumbent guard.** Consolidation is stochastic, so each round generates
  several candidate skills. With a `score_fn`, the engine keeps the best-scoring one only if
  it beats the current skill, so an unlucky round falls back instead of shipping a regression.
  (The default run skips scoring and takes the median-size candidate.)
- **Compaction.** Evolved skills are distilled back down when they grow too large -- the skill
  is loaded into the agent's context whenever it is relevant, so an ever-growing manual costs
  tokens on every call and buries the rules that matter under ones that don't.

The anti-bloat rules are enforced by the consolidator prompt in `scripts/skill_evolution.py`,
before compaction ever runs -- which is why an evolved skill stays behavioral (~2.4KB) rather
than a pile of baked facts:

```text
8.  A skill is BEHAVIORAL, not a knowledge base. Do NOT add facts of any kind --
    the skill says HOW to behave; the TOOL supplies WHAT. If the agent needs a
    fact, the rule is "look it up".
10. Do NOT add a Keyword / Terminology Mapping section -- the lookup tool resolves
    the user's own wording itself.
12. Generalize, do not enumerate: prefer ONE rule over a long list of specific topics.
```
