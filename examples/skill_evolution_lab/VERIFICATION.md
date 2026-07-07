# Verification — recorded end-to-end run

A full `./run_e2e_demo.sh` run of this example, captured so the result is
reproducible and every number in the companion blog post comes from a real run.
Measured on an **80-question held-out set** (55 single-turn + 15 multi-turn
anti-parroting + 10 out-of-scope). The agent has **two meaningful tools** --
`lookup_company_policy` (facts) and `calculate_disability_pay` (a computed short-term
disability payout) -- so V1 must learn tool *selection*, not just "use the tool," and
it must also **decline** questions outside its scope.

> **What this proves — and what it doesn't.** The contribution is the **closed
> loop**: trace → golden-graded score → evolve → re-score, all attributable
> because only the skill file changes. The large V0→V1 *delta* is an
> *illustration* of that loop on a deliberately **crippled** V0 (it's told to
> ignore a tool that already holds every answer), so most of the lift is the
> engine learning one rule — "use the tool, don't deflect to HR." Read the delta
> as "the loop reliably finds and fixes a real skill defect," not as "+65pp from
> any starting point." A fair, plausibly-written baseline would show a smaller
> (still real) gain.

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
| Runtime | `setup.sh` ~5s; `run_e2e_demo.sh` ~15–18 min per run at this size |
| Date | 2026-07-07 |

The agent model, tools, and questions are identical for V0 and V1 — **only the
skill file changes** — so the delta is attributable to the skill.

## Result (recorded run, gemini-3.1-flash-lite, held-out)

| Metric | V0 (flawed) | V1 (evolved) | Delta |
| --- | --- | --- | --- |
| Overall | 38.8% (31/80) | 96.2% (77/80) | +57.4pp |
| Single-turn | 30.9% (17/55) | 96.4% (53/55) | +65.5pp |
| Corrections (anti-parrot) | 33.3% (5/15) | 93.3% (14/15) | +60.0pp |
| Out-of-scope (declined) | 90.0% (9/10) | 100.0% (10/10) | +10.0pp |
| Tool-grounded answers | 13% (10/80) | 73% (58/80) | — |

The flawed V0 barely calls the tool on **in-scope** questions (it's told not to), so it
declines on most of them; the evolved V1 uses the tool and answers correctly — including the
multi-turn correction cases, where it re-verifies and holds the right figure instead of caving.
Out-of-scope is the honest edge case, and here V1 *improves* it: V0 cleanly declines 9/10 while
V1 declines all 10 **deliberately** (routing IT questions to IT, refusing unrelated ones) rather
than by reflex.

## Across four models × 3 runs (held-out, golden-graded)

Correctness and grounding as **mean [min–max]** over 3 runs each (analyst + judge
fixed), on the current **multi-tool** demo. (This sweep predates the out-of-scope
slice, so it measures **in-scope** correctness and grounding only; the single-model
reference run above is the one that includes out-of-scope declines.)

```text
Model                     Correctness V1     Grounding V1     V0 baseline
                          mean [range]       mean [range]     (corr)
-----------------------   ----------------   --------------   -----------
gemini-3.5-flash          100% [100-100]     90% [81-95]      63%
gemini-3.1-flash-lite      97% [94-100]      74% [72-76]      31%
gemini-2.5-pro             94% [91-96]       81% [72-85]      52%
gemini-3.1-pro-preview    100% [100-100]     79% [72-89]      52%
```

Every model recovers strongly (V1 94–100%), and the V0 column surfaces an honest
observation about **how the same flawed skill lands very differently across models:**

- **`gemini-3.5-flash` now largely ignores the "don't use the tool" instruction** and
  grounds anyway, so it starts highest (63%) with the least headroom. **`gemini-3.1-flash-lite`
  follows the restriction most literally**, deflects the most, and starts lowest (31%) — the
  biggest, cleanest gain (→97%). The pro models sit in between (~52%). The evolved tool-first
  skill closes the gap: every model ends at 94–100%. Reporting a range (not a single run) keeps
  this honest, and is why best-of-N (and a `score_fn` gate) matter when a consolidation gets
  unlucky. (Model behavior drifts over time — this sweep was recorded 2026-07-07; an earlier
  run had `gemini-3.5-flash` starting near 21%, which is why the featured reference run above
  now uses the more consistently-crippled `gemini-3.1-flash-lite`.)

## Evolution internals (from the run log, gemini-3.1-flash-lite)

```text
Trajectories: 24 successes, 43 failures
Collected 52 patches (52 passed the quality gate)
Selected median-size candidate (2445 chars)
```

Prevalence across the 52 patches: `TOOL_USAGE` 49/52 (94%), with single `MISSING_RULE`,
`RESPONSE_PATTERN`, and `SCOPE_GAP` patches. No `score_fn` was used; the engine returns the
median-size viable candidate and the held-out re-score is the proof. Run with a `score_fn` for
best-of-N selection (and to gate out unlucky candidates like the low `gemini-2.5-pro` run above).

## The evolved V1 skill (675B → 2.4KB, gemini-3.1-flash-lite)

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

Answer questions using the information above or by utilizing your available tools. Do not
restrict your answers only to the initially provided knowledge.

## Tool Usage
- **Policy Lookup:** When asked about any company policy, benefit, or procedure (e.g.,
  expenses, holidays, medical, dental, vision, 401k, leave) that is not explicitly detailed
  in your initial knowledge, you MUST call the `lookup_company_policy` tool. Never deflect to
  HR or claim you lack information without first querying this tool.
- **Disability Calculation:** Use the `calculate_disability_pay` tool whenever a user provides
  salary and/or duration of leave to ask for a specific personalized payout. Do not calculate
  it manually or rely on the lookup tool for personalized amounts.

## Rules & Response Guidelines
- **Policy Maximums:** If a user asks about a quantity that exceeds a stated policy maximum
  (e.g., 4 days of remote work when the limit is "up to 3"), state it is not allowed because
  it exceeds the limit. Do not treat it as an unknown topic and do not deflect to HR.
- **Proactive Details:** When answering about a specific policy, proactively include related
  details (accrual rates, rollover limits) for a complete answer.

## Out of Scope Handling
- **HR Deflection:** Only suggest contacting HR after `lookup_company_policy` confirms the
  information is unavailable for an HR topic.
- **IT Issues:** For non-HR topics (Wi-Fi, hardware, software), clarify you only handle HR
  policies and suggest IT Support, not HR.
```

This is the skill the **tool-aware, scope-aware** analysts produced: all 52 patches passed the
quality gate. The engine learned a *routing* decision (lookup for facts, calculator for
personalized math), a policy-maximum rule, **and** an out-of-scope policy -- not just "use the
tool." (Previously, single-tool deflection failures even returned NO_PATCH because the analysts
couldn't see a tool existed.)

## Before / after (a lookup case)

```text
Q: "If I leave the company mid-year, what happens to my unused PTO?"

V0:  category=unhelpful   (no tool call)
  "I do not have information regarding what happens to unused PTO if you leave the
   company mid-year. Please contact HR for assistance."

V1:  category=meaningful  (calls lookup_company_policy)
  "If you leave the company mid-year, any unused accrued PTO will be paid out to you
   in your final paycheck."
```

## Reproduce (tested)

```bash
cd examples/skill_evolution_lab
./setup.sh YOUR_PROJECT_ID us-central1   # ~5s
./run_e2e_demo.sh                        # one model, ~15-18 min (first run also does a one-time uv sync)

# Reproduce the whole multi-model table (4 models x 3 runs, ~3-4 h).
# Self-logs to runs/SWEEP_<ts>.log, so it can be detached and read later:
nohup ./run_sweep.sh >/dev/null 2>&1 &   # background; survives logout
tail -f runs/SWEEP_*.log                 # live progress
cat  runs/SWEEP_*.md                      # final mean [range] table when done
```

The four-model × 3-run table above is produced by `run_sweep.sh` (which loops
`run_e2e_demo.sh` over `AGENT_MODEL` and repetitions, then calls `aggregate_sweep.py`).
The V0 skill is auto-restored after each run. Exact numbers vary run-to-run (LLM
nondeterminism, stochastic consolidation) — which is why the table reports ranges
— but the direction is stable: the flawed V0 defers/declines on topics it has a
tool for, and the evolved V1 uses the tool and answers correctly, including when
the user asserts a wrong "correction".
