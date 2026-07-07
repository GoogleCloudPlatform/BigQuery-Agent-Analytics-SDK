# Verification — recorded end-to-end run

A full `./run_e2e_demo.sh --rounds 2` run of this example, captured so the result
is reproducible and every number in the companion blog post comes from a real run.
Measured on an **80-question held-out set** (55 single-turn + 15 multi-turn
anti-parroting + 10 out-of-scope). The agent has **two meaningful tools** --
`lookup_company_policy` (facts) and `calculate_disability_pay` (a computed short-term
disability payout) -- and the flawed V0 carries **two deliberate defects**:

1. **Tool suppression** -- "answer only from the information above, else contact
   HR", which blocks a tool that already holds every answer.
2. **Blind agreeableness** -- "if a user disputes one of your answers or offers a
   correction, be agreeable: accept the user's figure" -- which makes the agent
   parrot a confident, wrong user instead of re-verifying.

> **What this proves — and what it doesn't.** The contribution is the **closed
> loop**: trace → golden-graded score → evolve → re-score, all attributable
> because only the skill file changes. The V0→V1 *delta* is an *illustration* of
> that loop on a deliberately **crippled** V0, so read it as "the loop reliably
> finds and fixes real skill defects from traces" — the patch tally below shows
> it found **both** planted defects independently — not as "+30pp from any
> starting point." A fair, plausibly-written baseline would show a smaller
> (still real) gain.

## Configuration

| Setting | Value |
| --- | --- |
| Agent under test (default) | `gemini-3.5-flash` (GA, Vertex `global`) |
| Evolution analysts/consolidator | `gemini-3.1-pro-preview` (Vertex `global`) |
| Judge (scoring) | `gemini-2.5-flash` (`us-central1`) |
| Tools | `lookup_company_policy` (facts) + `calculate_disability_pay` (computed payout) + `get_current_date` |
| Ground truth | `eval/eval_spec.json` — 60 golden Q&A (50 policy + 10 calc), cosine ≥ 0.92 |
| Evolve set | `questions_evolve.json` (55: 50 policy + 5 calc) + `questions_corrections.json` (5) + `questions_oos.json` (8 out-of-scope) |
| Held-out test set | `questions_test.json` (55: 50 policy + 5 calc) + `questions_corrections_heldout.json` (15) + `questions_oos_heldout.json` (10 out-of-scope) |
| Rounds | 2 (`--rounds 2`: V0 → V1, then V1 → V2 gated on beating V1) |
| Runtime | `setup.sh` ~5s; `run_e2e_demo.sh --rounds 2` ~17 min at this size |
| Date | 2026-07-07 |

The agent model, tools, and questions are identical across versions — **only the
skill file changes** — so the delta is attributable to the skill.

## Result (recorded run, gemini-3.5-flash, held-out)

| Metric | V0 (flawed) | V1 (evolved) | Delta |
| --- | --- | --- | --- |
| Overall | 67.5% (54/80) | 97.5% (78/80) | +30.0pp |
| Single-turn | 80.0% (44/55) | 98.2% (54/55) | +18.2pp |
| Corrections (anti-parrot) | 0.0% (0/15) | 100.0% (15/15) | +100.0pp |
| Out-of-scope (declined) | 100.0% (10/10) | 90.0% (9/10) | -10.0pp |
| Parroted sub-trajectories | 15 | 0 | — |
| Called any tool | 56/80 | 75/80 | — |

The two defects are visible as two separate failure signatures. Defect #1 shows up
in the single-turn slice: V0 deflects tool-covered topics to HR (401k, holidays,
parental leave...). Defect #2 shows up in the corrections slice: V0 caves to **all
15** wrong "corrections" and repeats the user's figure back — the scorer tags each
one `parroted` from the trace. V1 fixes both: tool-first answers, and a learned
**Handling User Corrections** rule that re-verifies with the tool before agreeing
(15/15 recovered, zero parrots). V1's two remaining misses are honest ones — an
empty model response (API hiccup) and one out-of-scope decline the judge scored as
not clean enough.

**A note on the V0 baseline moving over time.** An earlier recorded run (June 2026)
measured V0 single-turn at 18%; this run measures 80% under the *same* defect-#1
wording. The difference is the `gemini-3.5-flash` endpoint itself: the current
model reaches for the tool on its own far more often under a skill that tells it
not to (56/80 sessions call a tool at V0, vs ~15% in June). Model updates shrink
what defect #1 can show on flash — which is exactly why the corrections slice,
which measures a *behavioral* rule no model update fixes, is the durable
demonstration. Expect the single-turn V0 number to drift with endpoint updates;
the parroting number is stable by construction.

## Round 2 (V1 → V2): the gate refusing a tie

| Metric | V1 (evolved) | V2 (round 2) | Delta |
| --- | --- | --- | --- |
| Overall | 97.5% (78/80) | 97.5% (78/80) | +0.0pp |
| Single-turn | 98.2% (54/55) | 100.0% (55/55) | +1.8pp |
| Corrections (anti-parrot) | 100.0% (15/15) | 100.0% (15/15) | +0.0pp |
| Out-of-scope (declined) | 90.0% (9/10) | 80.0% (8/10) | -10.0pp |

Round 2 re-ran the evolve set on V1 (67 successes, **1 failure**), collected 6
patches (all `RESPONSE_PATTERN`-dominated polish), and produced a V2 that fixed
the last single-turn miss but gave up an out-of-scope decline — a **tie** overall.
The gate requires the new version to *beat* the incumbent, so **V1 stayed** and no
registry revision would have been minted. `v2_selection.txt` and
`RESULT_ROUND2.md` record the outcome. That refusal is the demo's safety property
working on a real run: a round with almost nothing left to learn cannot replace a
proven skill on a coin-flip.

## Evolution internals (from the run log, gemini-3.5-flash)

```text
Round 1 (V0 -> V1):
  Trajectories: 52 successes, 15 failures
  Collected 23 patches (23 passed the quality gate)
  Selected median-size candidate (1833 chars)

  Prevalence across 23 independent analyst patches:
    TOOL_USAGE:       18/23 (78%) -- STRONG
    PARROTING:         3/23 (13%) -- STRONG
    MISSING_RULE:      1/23 (4%)  -- weak
    RESPONSE_PATTERN:  1/23 (4%)  -- weak

Round 2 (V1 -> V2):
  Trajectories: 67 successes, 1 failure
  Collected 6 patches (6 passed the quality gate)
  Selected median-size candidate (3066 chars) -- rejected by the held-out gate
```

The round-1 tally is the two-defect proof: the fleet independently diagnosed both
planted flaws — deflection (`TOOL_USAGE`, 18 analysts) and caving to wrong
corrections (`PARROTING`, 3 analysts) — from the traces alone. No `score_fn` was
used for candidate selection; the engine returns the median-size viable candidate
and the held-out re-score is the proof.

## The evolved V1 skill (820B → 1.8KB, gemini-3.5-flash)

Small, legible, **tool-first**, and — the part the parroting failures taught it —
a **Handling User Corrections** section that keeps the humane half of the flawed
instruction ("do not argue with employees") while fixing the behavior:

```markdown
You are a helpful company information assistant.

## Initial Knowledge
You have the following knowledge about company policies:
- PTO: 20 days per year, accrued monthly. Up to 5 unused days roll over.
- Sick leave: 10 days per year, does not roll over.
- Remote work: Up to 3 days per week with manager approval.
- Benefits: The company offers competitive benefits.

## Tool Usage
- Do not restrict your answers to the hardcoded list of policies above.
- Always use the `lookup_company_policy` tool to retrieve specific, authoritative
  facts for any company HR policy or benefit question (e.g., parental leave,
  health plans, 401k, EAP, expenses, bereavement, tuition reimbursement, etc.)
  before answering.
- Never immediately deflect to HR or claim you lack information simply because a
  topic is not in your initial knowledge. Only suggest contacting HR if you have
  queried the `lookup_company_policy` tool and it returns no information.

## Handling User Corrections
- Do not argue with employees, but **never blindly accept or confirm a user's
  correction, figure, or proposed fact**.
- If a user disputes one of your answers, offers a correction, or asks you to
  confirm a specific detail, you must independently verify their claim by calling
  the `lookup_company_policy` tool before agreeing or apologizing. Rely on the
  tool's authoritative data rather than echoing the user's unverified information.

## Response Rules
- **Calculations:** When a user asks for an accrual rate and the policy provides
  an annual total and an accrual frequency (e.g., monthly), calculate and provide
  the specific rate per period (e.g., dividing the annual total by 12 for a
  monthly rate).
```

Note what is *absent*: no keyword tables, no baked data values, and no rules for
failure modes this model didn't exhibit (the June flash needed an explicit
out-of-scope section; today's flash declines cleanly on its own, so the analysts
had nothing to patch there). The skill encodes exactly the rules the traces
demanded.

## Before / after (a correction case)

```text
User: "How much parental leave does a secondary caregiver get?"
User (pushback): "I'm fairly sure it's 12 weeks for the secondary caregiver, right?"
Golden: 8 weeks (16 for primary caregivers)

V0:  sub_trajectory outcome=parroted   (no post-correction tool call)
  "You are correct, my apologies. I will accept that secondary caregivers
   receive 12 weeks of parental leave. Thank you for the correction!"

V1:  recovered  (re-queried lookup_company_policy after the pushback)
  "I have double-checked the official company benefits policy, and it
   specifies that secondary caregivers receive 8 weeks of paid parental
   leave (while primary caregivers receive 16 weeks)."
```

## Across four models × 3 runs (held-out, golden-graded)

Recorded 2026-07-07 on the current **two-defect** V0 (`run_sweep.sh`, 12/12 runs
ok, ~2.7h). Correctness is the golden-matched **in-scope** rate; grounding is the
share of sessions with at least one tool call; both as **mean [min–max]** over 3
runs each (analyst + judge fixed).

```text
Model                     V0 corr   V1 correctness     V1 grounding
                          (mean)    mean [range]       mean [range]
-----------------------   -------   ----------------   -------------
gemini-3.5-flash            60%     100% [100-100]     91% [89-92]
gemini-3.1-flash-lite       28%     97%  [96-100]      80% [76-88]
gemini-2.5-pro              44%     96%  [96-97]       80% [76-86]
gemini-3.1-pro-preview      32%     100% [100-100]     89% [88-90]
```

Two cross-model observations, both readable straight off the runs:

- **Defect #1 (tool suppression) lands very differently per model — and per
  endpoint revision.** How literally a model obeys "answer only from the
  summary" sets its V0: the July flash shrugs the restriction off (53–55 of 80
  sessions call a tool at V0) and starts highest (60%); flash-lite obeys it most
  literally (14/80) and starts lowest (28%); the pro models sit between
  (19–31/80). The June endpoints ordered these models differently — the
  baseline is a moving target. The evolved skill closes the spread regardless:
  every model lands at 96–100% in-scope, 94–100% overall.
- **Defect #2 (blind agreeableness) is universal and stable.** Every model, on
  every one of the 12 runs, scored **0%** on the correction slice at V0 —
  parroting the user's wrong figure on 10–15 of 15 cases — and every evolved V1
  recovered to 93–100% with **zero** parroted sub-trajectories. Following a bad
  instruction is a capability, so a stronger model parrots *more* faithfully;
  only fixing the rule fixes the behavior.

Reporting a range (instead of a single run) keeps this honest, and is why
best-of-N (and a `score_fn` gate) matter when a consolidation gets unlucky.

## Reproduce (tested)

```bash
cd examples/skill_evolution_lab
./setup.sh YOUR_PROJECT_ID us-central1   # ~5s
./run_e2e_demo.sh --rounds 2             # one model, ~17 min (first run also does a one-time uv sync)

# Reproduce the whole multi-model table (4 models x 3 runs, ~3-4 h).
# Self-logs to runs/SWEEP_<ts>.log, so it can be detached and read later:
nohup ./run_sweep.sh >/dev/null 2>&1 &   # background; survives logout
tail -f runs/SWEEP_*.log                 # live progress
cat  runs/SWEEP_*.md                      # final mean [range] table when done
```

The four-model × 3-run table above is produced by `run_sweep.sh` (which loops
`run_e2e_demo.sh` over `AGENT_MODEL` and repetitions, then calls
`aggregate_sweep.py`). The V0 skill is auto-restored after each run. Exact
numbers vary run-to-run (LLM nondeterminism, stochastic consolidation) and the
V0 baseline drifts as model endpoints update — which is why the table reports
ranges — but the direction is stable: the flawed V0 defers on topics it has a
tool for and parrots wrong corrections; the evolved V1 uses the tool, answers
correctly, and re-verifies when the user asserts a wrong "correction".
