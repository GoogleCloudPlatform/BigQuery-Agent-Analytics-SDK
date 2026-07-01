# Verification — recorded end-to-end run

A full `./run_e2e_demo.sh` run of this example, captured so the result is
reproducible and every number in the companion blog post comes from a real run.
Measured on an **80-question held-out set** (55 single-turn + 15 multi-turn
anti-parroting + 10 out-of-scope). The agent has **two meaningful tools** --
`lookup_company_policy` (facts) and `calculate_disability_pay` (a computed short-term
disability payout) -- so V1 must learn tool *selection*, not just "use the tool," and
it must also **decline** questions outside its scope.

> **What this proves — and what it doesn't.** The contribution is the **closed
> loop**: trace → golden-grounded score → evolve → re-score, all attributable
> because only the skill file changes. The large V0→V1 *delta* is an
> *illustration* of that loop on a deliberately **crippled** V0 (it's told to
> ignore a tool that already holds every answer), so most of the lift is the
> engine learning one rule — "use the tool, don't deflect to HR." Read the delta
> as "the loop reliably finds and fixes a real skill defect," not as "+80pp from
> any starting point." A fair, plausibly-written baseline would show a smaller
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
| Runtime | `setup.sh` ~5s; `run_e2e_demo.sh` ~15–18 min per run at this size |
| Date | 2026-07-01 |

The agent model, tools, and questions are identical for V0 and V1 — **only the
skill file changes** — so the delta is attributable to the skill.

## Result (recorded run, gemini-3.5-flash, held-out)

| Metric | V0 (flawed) | V1 (evolved) | Delta |
| --- | --- | --- | --- |
| Overall | 28.7% (23/80) | 100.0% (80/80) | +71.3pp |
| Single-turn | 18.2% (10/55) | 100.0% (55/55) | +81.8pp |
| Corrections (anti-parrot) | 26.7% (4/15) | 100.0% (15/15) | +73.3pp |
| Out-of-scope (declined) | 90.0% (9/10) | 100.0% (10/10) | +10.0pp |
| Tool-grounded answers | 18% (14/80) | 88% (70/80) | — |

The flawed V0 barely calls the tool on **in-scope** questions (it's told not to), so it
declines on almost everything; the evolved V1 uses the tool and answers correctly —
including the multi-turn correction cases, where it re-verifies and holds the right figure
instead of caving. Out-of-scope is the honest edge case: V0 already scores well there
(deflecting *everything* happens to be the right call for a genuinely out-of-scope
question), and V1 learns to decline those **deliberately** (routing IT questions to IT,
refusing unrelated ones) rather than by reflex.

## Across four models × 3 seeds (held-out, golden-grounded)

Correctness and grounding as **mean [min–max]** over 3 runs each (analyst + judge
fixed), on the current **multi-tool** demo. (This sweep predates the out-of-scope
slice, so it measures **in-scope** correctness and grounding only; the single-model
reference run above is the one that includes out-of-scope declines.)

```text
Model                     Correctness V1     Grounding V1     V0 baseline
                          mean [range]       mean [range]     (corr)
-----------------------   ----------------   --------------   -----------
gemini-3.5-flash          100% [100-100]     89% [81-96]      21%
gemini-3.1-flash-lite     98% [96-100]       89% [83-100]     34%
gemini-2.5-pro            93% [89-97]        84% [83-84]      51%
gemini-3.1-pro-preview    100% [100-100]     83% [81-86]      47%
```

Every model recovers strongly (V1 93–100%), and the V0 column surfaces an honest
observation about **how the same flawed skill lands differently on different models:**

- **The stronger (pro) models reach for the tool even when told not to.** Under the
  "answer only from the summary, else contact HR" skill, `gemini-2.5-pro` and
  `gemini-3.1-pro-preview` still ground 41% / 32% of the time and start highest
  (51% / 47%), where the flash models obey the restriction (16–19% grounding) and
  start lower (21% / 34%). `gemini-2.5-pro` starts highest, so it has the least
  headroom and lands lowest (~93%). The evolved tool-first skill closes the gap —
  every model ends up at 93–100%. Reporting a range (not a single run) is what keeps
  this honest, and is why best-of-N (and a `score_fn` gate) matter when a
  consolidation gets unlucky.

## Evolution internals (from the run log, gemini-3.5-flash)

```text
Trajectories: 17 successes, 51 failures
Collected 53 patches (53 passed the quality gate)
Selected median-size candidate (2230 chars)
```

No `score_fn` was used; the engine returns the median-size viable candidate and
the held-out re-score is the proof. Run with a `score_fn` for best-of-N
selection (and to gate out unlucky candidates like the flash-lite seed above).

## The evolved V1 skill (675B → 2.2KB, gemini-3.5-flash)

Small, legible, **tool-first**, and -- the new parts -- it learned both **tool selection**
(lookup for facts vs. calculator for a computed payout) and **out-of-scope handling**
(decline cleanly, route IT questions to IT, don't reflexively deflect to HR):

```markdown
You are a helpful company information assistant.

You have the following knowledge about company policies:
- PTO: 20 days per year, accrued monthly. Up to 5 unused days roll over.
- Sick leave: 10 days per year, does not roll over.
- Remote work: Up to 3 days per week with manager approval.
- Benefits: The company offers competitive benefits.

## Instructions
- Answer questions using the information above when applicable.
- **Do not restrict your answers only to the hardcoded information above.** If a question
  is about a company policy, procedure, or benefit not explicitly listed, you MUST use
  your tools to retrieve the authoritative facts.
- Never immediately deflect to HR or claim you lack information for unlisted topics
  without first querying your tools.

## Tool Usage
- **`lookup_company_policy`**: Always call this for any company policy, benefit, or HR
  topic not in your initial knowledge (medical/dental/vision, HSA, 401k, expenses,
  holidays, bereavement, parental leave, tuition, EAP, flex time, short-term disability).
- **`calculate_disability_pay`**: If a user provides salary and duration and asks for a
  specific payout, use this tool to compute the exact amount.

## Out-of-Scope Handling & Deflections
- **HR Deflection**: Only suggest contacting HR after `lookup_company_policy` returns
  nothing for an HR/policy topic.
- **IT Support**: Direct technical, password, or hardware issues to IT Support, not HR.
- **Unrelated Topics**: If a request is completely unrelated to company policy or benefits
  (writing code, general trivia), state that you cannot assist. Do not suggest HR.
```

This is the skill the **tool-aware, scope-aware** analysts produced: all 53 patches
passed the quality gate. The engine learned a *routing* decision (lookup for facts,
calculator for personalized math) **and** an out-of-scope policy -- not just "use the
tool." (Previously, single-tool deflection failures even returned NO_PATCH because the
analysts couldn't see a tool existed.)

## Before / after (a tool-selection case)

```text
Q: "I earn $52,000. What's my total short-term disability payout if I'm out 4 weeks?"

V0:  category=unhelpful   (never calls the calculator)
  "I do not have information regarding short-term disability payouts. Please
   contact HR for assistance with this topic."

V1:  category=meaningful  (calls calculate_disability_pay)
  "Based on your $52,000 salary and a 4-week absence -- Weekly Benefit: $600 (60%
   of salary); Total Payout: $2,400 (within the 12-week cap)."
```

## Reproduce (tested)

```bash
cd examples/skill_evolution_lab
./setup.sh YOUR_PROJECT_ID us-central1   # ~5s
./run_e2e_demo.sh                        # one model, ~15-18 min (first run also does a one-time uv sync)

# Reproduce the whole multi-model table (4 models x 3 seeds, ~3-4 h).
# Self-logs to runs/SWEEP_<ts>.log, so it can be detached and read later:
nohup ./run_sweep.sh >/dev/null 2>&1 &   # background; survives logout
tail -f runs/SWEEP_*.log                 # live progress
cat  runs/SWEEP_*.md                      # final mean [range] table when done
```

The four-model × 3-seed table above is produced by `run_sweep.sh` (which loops
`run_e2e_demo.sh` over `AGENT_MODEL` and seeds, then calls `aggregate_sweep.py`).
The V0 skill is auto-restored after each run. Exact numbers vary run-to-run (LLM
nondeterminism, stochastic consolidation) — which is why the table reports ranges
— but the direction is stable: the flawed V0 defers/declines on topics it has a
tool for, and the evolved V1 uses the tool and answers correctly, including when
the user asserts a wrong "correction".
