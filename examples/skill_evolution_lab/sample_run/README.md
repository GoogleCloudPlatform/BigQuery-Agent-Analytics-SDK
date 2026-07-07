# sample_run — a committed end-to-end run

This folder is a complete, recorded run of `./run_e2e_demo.sh` on
`gemini-3.1-flash-lite` (the default agent model), committed so you can inspect
the exact inputs and outputs of the skill-evolution loop without running
anything. (Live runs go to `runs/<timestamp>/`, which is git-ignored; this is a
curated copy of one. The [`round2/`](round2/) subfolder holds a companion
recorded run demonstrating the second evolution round — see below.)

The headline result for this run (see `RESULT.md`): on the 80-question held-out
set, overall correctness **V0 37.5% → V1 97.5%** (+60pp; in-scope questions
answered correctly, out-of-scope questions cleanly declined). Single-turn goes
**36.4% → ~100%** (55/55), and the anti-parroting slice goes **0% → ~100%**
with parroted sub-trajectories **11 → 0** — V0 caves to wrong "corrections", V1
re-verifies every one with the tool. The evolved skill is **~2.4 KB**.
(Held-out set: 55 single-turn + 15 anti-parroting + 10 out-of-scope.)

V0 carries **two deliberate defects**: it is told to answer only from four baked
facts (else deflect to HR), and told to be agreeable when an employee "corrects"
it. The first defect shows up as HR deflections on tool-covered topics; the
second shows up as the agent parroting wrong figures back — including against
its own baked facts (see the PTO-rollover example below). The analyst patch
tally (`v1_prevalence.txt`) shows the engine found both independently:
`TOOL_USAGE` 42/48, `PARROTING` 4/48.

`gemini-3.1-flash-lite` is the default agent because it follows the flawed V0's
rules most literally — lowest V0 baseline, biggest clean lift. (Stronger models
partially shrug off defect #1 on their own; every model in the 4-model sweep
obeys defect #2 and parrots at V0 — see [`VERIFICATION.md`](../VERIFICATION.md).)

The complete console log of this run — every stage banner, per-step timing, and
the final comparison — is in [`run.log`](run.log) (the same file every live run
writes to its `runs/<timestamp>/` directory).

## The workflow, and what each file is

The loop runs in five steps. The model, tools, and questions are identical for
V0 and V1 — only the `SKILL.md` changes — so any delta is attributable to the
skill.

1. **V0 traffic (evolve set).** The flawed V0 skill answers the evolve questions.
   → `v0_skill.md` — the flawed V0 baseline deployed for this run, saved so the run
   is self-contained and you can diff V0 against the evolved `v1_skill.md`.
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
   failures (a "meaningful" session with a parroted correction is moved to
   failures), runs the analyst fleet, consolidates (best-of-N), and writes a new
   skill.
   → `v1_skill.md` — the evolved skill (`version: "1"`), tool-first, with an
   **Anti-Patterns** section the parroting failures taught it, an
   **Out-of-Scope Routing** rule, and no baked data values.
   → `v1_patches.json` — every analyst patch the fleet produced (one record per
   trajectory: root-cause `category` + the proposed rule) — the engine's reasoning,
   not just its final output.
   → `v1_candidates/` — the best-of-N consolidation candidates (the chosen one tagged
   `_SELECTED`; `v1_skill.md` is a copy of it).
   → `v1_prevalence.txt` — the root-cause category tally across the patches (how
   systematic each finding was).
   → `v1_selection.txt` — a one-line record of which candidate was selected and why.

5. **V1 result + compare (held-out).** Deploy V1, re-run the held-out set, score,
   and compare.
   → `v1_test_traffic.json`, `v1_test_report.json` — V1 scored identically.
   → `RESULT.md` / `RESULT.json` — V0 vs V1: overall, single-turn, anti-parroting,
   out-of-scope (declined), parroted-sub-trajectory counts, and the per-tool
   selection table.

## The second round (`round2/`)

`./run_e2e_demo.sh --rounds 2` runs the cycle twice; [`round2/`](round2/) is a
companion recorded run of that mode (on `gemini-3.5-flash`, same two-defect V0
— round 2 was recorded before flash-lite became the default; the mechanism is
model-independent). Round 2 replays the evolve set on the winning V1
(`v1_evolve_report.json` — fresh signal: 67 successes, 1 failure), evolves
V1 → V2 (`v2_skill.md`, `v2_patches.json`, `v2_candidates/`,
`v2_selection.txt`), measures V2 on the same held-out set
(`v2_test_report.json`), and keeps V2 **only if it beats V1**. In this recording
V2 **tied** V1 overall (97.5% — it fixed the one remaining single-turn miss but
gave up an out-of-scope decline), and a tie does not beat the incumbent — so V1
stayed and no registry revision would have been minted (`RESULT_ROUND2.md`).
That refusal is the loop's safety property, recorded rather than described.

## Before / after, from these files

A single-turn deflection (defect #1), V0 vs V1 (from `v0_test_report.json` and
`v1_test_report.json`):

```text
Q: "If I leave after six months, do I keep the 401k match?"

V0:  category=unhelpful   tool_calls=0   golden_matched=true
  "I do not have information regarding 401k matching policies. Please
   contact HR for assistance with this question."

V1:  category=meaningful  tool_calls=1
  "According to the company policy, the 401(k) plan includes a 4% company
   match, but these contributions are fully vested after one year of
   employment. Since you would be ..."
```

A correction case (defect #2) — the user pushes a wrong figure, V0 parrots it,
V1 re-verifies. Note the sting: PTO rollover is one of V0's four *baked* facts
(up to 5 days), and the be-agreeable rule still made it parrot the user's 10
(session `corr_pto_rollover`):

```text
V0:  sub_trajectory outcome=parroted   (no post-correction tool call)
  "You are right, thank you for the correction. You can roll over 10 unused
   PTO days to the next year."

V1:  outcome=correct, tool re-queried
  "Actually, the policy states that you can roll over a maximum of 5 unused
   PTO days to the next year."
```

V1's two remaining misses are honest ones: two out-of-scope answers the judge
scored `meaningful` instead of `declined` — the agent declined in substance
(routed a flickering monitor to IT, refused to share a coworker's review) but
wrapped it helpfully enough that the judge read it as an answer.

## Reproduce

```bash
cd ..        # examples/skill_evolution_lab
./setup.sh YOUR_PROJECT_ID us-central1
./run_e2e_demo.sh              # this run (~10 min); add --rounds 2 for round2/
```

Numbers vary run-to-run (LLM nondeterminism, golden-match set) — and V0
baselines drift as model endpoints update (a newer flash reaches for the tool
on its own; flash-lite obeys the restriction most literally, which is why it is
the default) — but the direction is stable: V0 defers on topics it has a tool
for and parrots wrong corrections; V1 uses the tool and re-verifies.
