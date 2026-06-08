# sample_run — a committed end-to-end run

This folder is a complete, recorded run of `./run_e2e_demo.sh` on
`gemini-3-flash-preview`, committed so you can inspect the exact inputs and
outputs of the skill-evolution loop without running anything. (Live runs go to
`runs/<timestamp>/`, which is git-ignored; this is a curated copy of one.)

The headline result for this run (see `RESULT.md`): held-out correctness
**V0 23.8% → V1 100%**, grounding (tool-call share) **29% → 86%**, evolved skill
**2.5 KB**.

## The workflow, and what each file is

The loop runs in five steps. The model, tool, and questions are identical for V0
and V1 — only the `SKILL.md` changes — so any delta is attributable to the skill.

1. **V0 traffic (evolve set).** The flawed V0 skill answers the evolve questions.
   → `v0_evolve_traffic.json` — raw conversations, one per session:
   `{session_id, question, conversation[], final_response, tool_calls, ...}`,
   the schema `quality_report.py --conversations-file` consumes.

2. **Score V0 (evolve set).** `quality_report.py --eval-spec eval_spec.json
   --tag-turns` grades each conversation against the golden Q&A and tags
   corrections.
   → `v0_evolve_report.json` — **the engine's input.** Each session has
   `metrics.response_usefulness.category` (meaningful / unhelpful / partial /
   declined), `golden_eval` (`matched`, `expected_answer`, `similarity`), and
   `sub_trajectories` (correction outcomes: recovered / parroted / not_recovered).
   `summary.golden_eval_summary.matched_meaningful_rate` is the headline metric.

3. **V0 baseline (held-out).** Same two steps on the *disjoint* held-out test set.
   → `v0_test_traffic.json`, `v0_test_report.json` — the honest baseline, on
   questions the engine never trains on.

4. **Evolve.** `evolve_skill()` partitions the V0 evolve report into successes /
   failures, runs the analyst fleet, consolidates (best-of-N), and writes a new
   skill.
   → `v1_skill.md` — the evolved skill (`version: "1"`, `evolved_from: "0"`),
   tool-first, with a "Premature HR Deflection" anti-pattern.

5. **V1 result + compare (held-out).** Deploy V1, re-run the held-out set, score,
   and compare.
   → `v1_test_traffic.json`, `v1_test_report.json` — V1 scored identically.
   → `RESULT.md` / `RESULT.json` — V0 vs V1: overall, single-turn, anti-parroting,
   and parroted-sub-trajectory counts.

## Before / after, from these files

The same held-out question, V0 vs V1 (from `v0_test_report.json` and
`v1_test_report.json`):

```text
Q: "How much does the company put into an HSA for family coverage?"

V0:  category=unhelpful   tool_calls=1   golden_matched=true
  "I do not have that information. Please contact HR ..."

V1:  category=meaningful  tool_calls=1   golden_matched=true
  "The company contributes $1,500 per year into an HSA for family coverage
   ($750 individual)."
```

## Reproduce

```bash
cd ..        # examples/skill_evolution_lab
./setup.sh YOUR_PROJECT_ID us-central1
./run_e2e_demo.sh
```

Numbers vary run-to-run (LLM nondeterminism, golden-match set), but the direction
is stable: V0 defers/declines on topics it has a tool for; V1 uses the tool.
