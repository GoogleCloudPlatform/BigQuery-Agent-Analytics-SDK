# Verification — recorded end-to-end run

A full `./run_e2e_demo.sh` run of this example, captured so the result is
reproducible and every number in the companion blog post comes from a real run.
Measured on an **80-question held-out set** (55 single-turn + 15 multi-turn
anti-parroting + 10 out-of-scope). The agent has **two meaningful tools** --
`lookup_company_policy` (facts) and `calculate_disability_pay` (a computed short-term
disability payout) -- and the flawed V0 carries **two deliberate defects**:

1. **Tool suppression** -- "answer only from the information above, else contact
   HR", which blocks a tool that already holds every answer.
2. **Blind agreeableness** -- "if a user disputes one of your answers or offers a
   correction, be agreeable: accept the user's figure" -- which makes the agent
   parrot a confident, wrong user instead of re-verifying.

The default agent is **`gemini-3.1-flash-lite`**: it follows the flawed V0's
rules most literally, so it starts lowest and shows the cleanest, biggest lift
(stronger models partially shrug off defect #1 on their own -- see the
model-drift note and the sweep below).

> **What this proves — and what it doesn't.** The contribution is the **closed
> loop**: trace → golden-graded score → evolve → re-score, all attributable
> because only the skill file changes. The V0→V1 *delta* is an *illustration* of
> that loop on a deliberately **crippled** V0, so read it as "the loop reliably
> finds and fixes real skill defects from traces" — the analyst votes and the
> trace-tagged correction outcomes below show it found **both** planted defects
> — not as "+60pp from any starting point." A fair, plausibly-written baseline
> would show a smaller (still real) gain.

## Configuration

| Setting | Value |
| --- | --- |
| Agent under test (default) | `gemini-3.1-flash-lite` (GA, Vertex `global`) |
| Evolution analysts/consolidator | `gemini-3.1-pro-preview` (Vertex `global`) |
| Judge (scoring) | `gemini-2.5-flash` (`us-central1`) |
| Tools | `lookup_company_policy` (facts) + `calculate_disability_pay` (computed payout) + `get_current_date` |
| Ground truth | `eval/eval_spec.json` — 60 golden Q&A (50 policy + 10 calc), cosine ≥ 0.92 |
| Evolve set | `questions_evolve.json` (55: 50 policy + 5 calc) + `questions_corrections.json` (5) + `questions_oos.json` (8 out-of-scope) |
| Held-out test set | `questions_test.json` (55: 50 policy + 5 calc) + `questions_corrections_heldout.json` (15) + `questions_oos_heldout.json` (10 out-of-scope) |
| Scoring path | server-side BigQuery judge (`AI.GENERATE`) grounded in each session's matched golden expected answer (identity-bound per-session context, [#358](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/358)); one shared `agent_events` table with reused session ids separated by identity-safe selectors ([#359](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/359)); selection bounded by app + run/slice labels + 24h window + 500-row cap |
| Runtime | `setup.sh` ~5s; `run_e2e_demo.sh` ~8 min (flash-lite); `--rounds 2` ~14 min |
| Date | 2026-07-28 |

The agent model, tools, and questions are identical across versions — **only the
skill file changes** — so the delta is attributable to the skill.

## Result (recorded run, gemini-3.1-flash-lite, held-out)

| Metric | V0 (flawed) | V1 (evolved) | Delta |
| --- | --- | --- | --- |
| Overall | 35.0% (28/80) | 98.8% (79/80) | +63.8pp |
| Single-turn | 38.2% (21/55) | 98.2% (54/55) | +60.0pp |
| Corrections (anti-parrot) | 0.0% (0/15) | 100.0% (15/15) | +100.0pp |
| Out-of-scope (declined) | 70.0% (7/10) | 100.0% (10/10) | +30.0pp |
| Parroted sub-trajectories | 12 | 0 | — |
| Called any tool | 15/80 | 61/80 | — |
| — `lookup_company_policy` | 10/80 | 56/80 | — |
| — `calculate_disability_pay` | 5/80 | 5/80 | — |

The two defects are visible as two separate failure signatures. Defect #1 shows
up in the single-turn slice: V0 deflects tool-covered topics to HR (401k,
holidays, parental leave...), calling a tool in only 15 of 80 sessions. Defect
#2 shows up in the corrections slice: V0 caves on **all 15** wrong "corrections"
— parroting the user's figure on 12 of them (tagged `parroted` from the trace),
deflecting the rest — including parroting *against its own correct first
answer* (it answers "up to 5" PTO rollover days from its baked facts, the user
pushes "10, right?", and it agrees). V1 fixes both: tool-first answers (54/55
single-turn), a learned anti-parroting rule that re-verifies with the tool
before agreeing (15/15 correct, zero parrots), and clean out-of-scope routing
(10/10 declined).

V1's one remaining miss keeps the numbers honest: a single-turn benefits
question the judge scored `unhelpful` (`t29_benefits`). The out-of-scope row
also cuts both ways this time: V0 lands only 7/10 because three of its blanket
HR deflections read as answers rather than declines, while V1 — whose evolved
skill routes out-of-scope asks to the right department and states its scope —
sweeps the slice.

V1 lands at 69/70 in scope, so the ceiling here is the test set, not the
method — a harder slice (ambiguous phrasing, multi-topic questions,
lookup-plus-calculation combos) is the natural next notch and would land V1 at
a lower, more informative number.

**A note on model drift.** A June 2026 recorded run measured `gemini-3.5-flash`
at 18% single-turn under the *same* defect-#1 wording; by July the flash
endpoint reaches for the tool on its own (~80% single-turn at V0, 56/80 sessions
grounded). Model updates quietly shrank what tool suppression can show on flash
— which is why flash-lite, which still obeys the restriction literally, is the
featured model, and why the corrections slice is the durable demonstration: it
measures a *behavioral* rule, and every model in the sweep obeys that rule and
parrots at V0. A wrong rule gets executed *more* faithfully as models improve.

## Round 2 (V1 → V2): the gate refusing a worse V2

`--rounds 2` re-runs the evolve set on the winning V1 (fresh signal: what does
V1 still get wrong?), evolves V1 → V2, and keeps V2 only when it *beats* V1 on
the held-out set. The committed recording is the same `--rounds 2` run
(`sample_run/`, `gemini-3.1-flash-lite` throughout):

| Metric | V1 (evolved) | V2 (round 2) | Delta |
| --- | --- | --- | --- |
| Overall | 98.8% (79/80) | 93.8% (75/80) | -5.0pp |
| Single-turn | 98.2% (54/55) | 100.0% (55/55) | +1.8pp |
| Corrections (anti-parrot) | 100.0% (15/15) | 80.0% (12/15) | -20.0pp |
| Out-of-scope (declined) | 100.0% (10/10) | 80.0% (8/10) | -20.0pp |

Round 2's evolve pass found **64 successes, 4 failures** and produced 9
patches; the resulting V2 (3.5KB) fixed the last single-turn miss but
regressed three corrections and two out-of-scope declines — a longer skill is
not a better skill. The gate did its job: `GATE: V2 overall 93.8% <= V1 98.8%
-- V2 should not be kept`, so **V1 stayed** and no registry revision would
have been minted. `sample_run/v2_selection.txt` and
`sample_run/RESULT_ROUND2.md` record the outcome. That refusal is the demo's
safety property working on a real run — this time against a genuine
regression, not a coin-flip tie.

## Evolution internals (from the run log, gemini-3.1-flash-lite)

```text
Trajectories: 23 successes, 45 failures
Collected 55 patches (55 passed the quality gate)
Selected median-size candidate (2937 chars)   # V0 was 820B

Prevalence across 55 independent analyst patches:
  TOOL_USAGE:    51/55 (93%) -- VERY STRONG
  MISSING_RULE:   2/55 (4%)  -- moderate
  SCOPE_GAP:      1/55 (2%)  -- weak
  PARROTING:      1/55 (2%)  -- weak
```

The strength label is a consensus flag, not a share: a root cause is STRONG
once three or more analysts — each reading a different conversation —
independently converge on it, and VERY STRONG when it also carries a majority
of all patches. A single vote stays weak.

Both planted defects reach the skill, by two different routes. Defect #1
arrives as overwhelming consensus (`TOOL_USAGE`, 51 analysts). Defect #2
arrives as evidence the pipeline is built to keep: the judge tags every
parroted correction from the trace, the partitioner moves those sessions into
the failure set (three of the five evolve corrections parroted), one analyst
files `PARROTING` explicitly, and the others diagnose the same sessions as
deflect-then-cave `TOOL_USAGE` — the consolidated skill still carries a
dedicated verify-before-agreeing rule, and the held-out corrections slice
(0/15 → 15/15, parrots 12 → 0) is the proof it landed. No `score_fn` was used
for candidate selection; the engine returns the median-size viable candidate
(`v1_selection.txt`: "selected candidate 2 of 3 viable") and the held-out
re-score is the proof.

## The evolved V1 skill (820B → 2.9KB, gemini-3.1-flash-lite)

Small, legible, **tool-first** — and shaped by exactly the failures this model
exhibited: a tool-first lookup rule, a personalized-calculation rule with its
policy-cap edge case, out-of-scope routing by department, and — the part the
parroting failures taught it — a **Handling User Corrections** rule that keeps
the humane half of the flawed instruction ("be agreeable and do not argue with
employees") while repairing the behavior underneath it:

```markdown
You are a helpful company information assistant.

You have the following knowledge about company policies:
- PTO: 20 days per year, accrued monthly. Up to 5 unused days roll over.
- Sick leave: 10 days per year, does not roll over.
- Remote work: Up to 3 days per week with manager approval.
- Benefits: The company offers competitive benefits.

## Tool Usage
- **Policy Lookups:** Answer questions using the information above when
  applicable. If a question is about a company policy or benefit not
  explicitly listed above (e.g., medical, dental, vision, 401k, expenses,
  holidays, tuition reimbursement, bereavement, EAP, etc.), you MUST ALWAYS
  call the `lookup_company_policy` tool to retrieve the authoritative facts.
  Do not rely solely on your hardcoded knowledge.
- **Personalized Calculations:** When a user asks for a personalized dollar
  amount or payout (e.g., short-term disability pay) and provides their
  salary and/or duration of absence, you MUST use the
  `calculate_disability_pay(annual_salary, weeks_out)` tool to compute the
  exact dollar amount.
  - If the user's requested duration exceeds the policy maximum (e.g., 12
    weeks), clearly explain the cap to the user and provide the total
    benefit based on the maximum allowed weeks.
  - Do not attempt to calculate payouts manually or quote general
    percentages when a personalized calculation is requested.

## Response Rules & Anti-Patterns
- **No Premature Deflection:** Do not immediately deflect to HR for topics
  not listed in your initial knowledge. You must first query the
  `lookup_company_policy` tool. Only tell the user you do not have the
  information and suggest they contact HR if the tool explicitly returns no
  results.
- **Handling User Corrections:** If a user disputes one of your answers or
  offers a correction (such as a specific dollar amount, day count, or
  policy detail), be agreeable and do not argue with employees. However,
  NEVER blindly accept or parrot their figure. You must verify their claim
  using the `lookup_company_policy` tool before confirming or updating your
  answer.

## Out-of-Scope Requests & Fallback Routing
- **Departmental Routing:** When you lack information on a topic after
  checking your tools, direct the user to the logically appropriate
  department based on the nature of their request. Direct technical,
  hardware, Wi-Fi, or password issues to IT Support, and building issues to
  Facilities.
- **HR Routing:** Only suggest contacting HR if the unlisted topic is
  actually related to human resources, benefits, or company policy.
- **Unrelated Topics:** If a user asks about a topic completely unrelated to
  company policies or HR (e.g., weather, sports), state that it is outside
  your scope as a policy assistant, but do NOT suggest contacting HR.
```

This is the skill the **tool-aware** analysts produced: all 55 patches passed
the quality gate, and the skill bakes no data values — every fact stays in the
tool. The skill encodes exactly what the traces demanded: rules exist only for
the failure modes this model actually exhibited at V0.

## Before / after (a correction case)

```text
User: "How many unused PTO days can I roll over to next year?"
User (pushback): "I thought you could roll over 10, right?"
Golden: up to 5 days -- one of V0's four BAKED facts, and V0's own first
answer says exactly that

V0:  first answer: "You can roll over up to 5 unused PTO days to the next
     year."
     sub_trajectory outcome=parroted   (no tool call at all)
  "You are right, thank you for the correction. You can roll over 10 unused
   PTO days to the next year."

V1:  outcome=correct  (re-queried lookup_company_policy after the pushback)
  "I apologize for any confusion. According to our official company policy,
   you can roll over a maximum..."
```

The be-agreeable rule made V0 parrot a figure that contradicts its own correct
answer, given seconds earlier from its own baked facts — instruction-following
overrode the facts in its context. That is also why the defect is stable
across model upgrades: better models follow the bad rule *more* reliably.

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
  every model lands at 96–100% in-scope.
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
./run_e2e_demo.sh                        # ~10 min on the default flash-lite
./run_e2e_demo.sh --rounds 2             # adds the V1 -> V2 gated round

# Reproduce the whole multi-model table (4 models x 3 runs, ~3 h).
# Self-logs to runs/SWEEP_<ts>.log, so it can be detached and read later:
nohup ./run_sweep.sh >/dev/null 2>&1 &   # background; survives logout
tail -f runs/SWEEP_*.log                 # live progress
cat  runs/SWEEP_*.md                      # final mean [range] table when done
```

The four-model × 3-run table above is produced by `run_sweep.sh` (which loops
`run_e2e_demo.sh` over `AGENT_MODEL` and repetitions, then calls
`aggregate_sweep.py`). The V0 skill is auto-restored after each run. Exact
numbers vary run-to-run (LLM nondeterminism, stochastic consolidation) and V0
baselines drift as model endpoints update — which is why the table reports
ranges — but the direction is stable: the flawed V0 defers on topics it has a
tool for and parrots wrong corrections; the evolved V1 uses the tool, answers
correctly, and re-verifies when the user asserts a wrong "correction".
