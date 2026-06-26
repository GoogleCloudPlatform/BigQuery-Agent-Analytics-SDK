# Verification — recorded end-to-end run

A full `./run_e2e_demo.sh` run of this example, captured so the result is
reproducible and the numbers reported in the companion blog post are backed by an
actual run (not aspirational). Measured on a **65-question held-out set** (50
single-turn + 15 multi-turn anti-parroting), and swept across four models with
**3 seeds each** to show the (real) run-to-run variance.

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
| Ground truth | `eval/eval_spec.json` — 50 golden Q&A (matched at cosine ≥ 0.92) |
| Evolve set | `questions_evolve.json` (50, rephrased) + `questions_corrections.json` (5) |
| Held-out test set | `questions_test.json` (50) + `questions_corrections_heldout.json` (15) |
| Runtime | `setup.sh` ~5s; `run_e2e_demo.sh` ~15–18 min per run at this size |
| Date | 2026-06-24 |

The agent model, tools, and questions are identical for V0 and V1 — **only the
skill file changes** — so the delta is attributable to the skill.

## Result (recorded run, gemini-3.5-flash, held-out)

| Metric | V0 (flawed) | V1 (evolved) | Delta |
| --- | --- | --- | --- |
| Overall | 20.0% (13/65) | 100.0% (65/65) | +80.0pp |
| Single-turn | 18.0% (9/50) | 100.0% (50/50) | +82.0pp |
| Corrections (anti-parrot) | 26.7% (4/15) | 100.0% (15/15) | +73.3pp |
| Tool-grounded answers | 9% (6/65) | 100% (65/65) | — |

The flawed V0 barely calls the tool (it's told not to), so it declines on almost
everything; the evolved V1 uses the tool and answers correctly — including the
multi-turn correction cases, where it re-verifies and holds the right figure
instead of caving.

## Across four models × 3 seeds (held-out, golden-grounded)

Correctness and grounding as **mean [min–max]** over 3 runs each (analyst + judge
fixed):

```text
Model                     Correctness V1     Grounding V1     V0 baseline
                          mean [range]       mean [range]     (corr)
-----------------------   ----------------   --------------   -----------
gemini-3.5-flash          100% [100-100]     96% [89-100]     21%
gemini-3.1-flash-lite     97% [95-98]        78% [74-83]      20%
gemini-2.5-pro            93% [92-94]        82% [82-83]      55%
gemini-3.1-pro-preview    100% [98-100]      84% [80-91]      21%
```

This sweep was run on the **post-`format_trajectory`-fix** engine (the analyst now
sees the parrot/recover sub-trajectory labels). Grounding tightened markedly versus
the pre-fix engine — `gemini-3.1-flash-lite`'s correctness spread collapsed from a
prior 71–100% to **95–98%**, because the richer analyst signal yields a more
reliably tool-first skill.

Every model recovers strongly. Two honest observations the seeds surface:

- **`gemini-2.5-pro` starts highest (55%)** — it grounds on the tool even under
  the flawed prompt, so it has the least headroom, yet still reaches ~93%.
- **The flash/lite models start near the floor (~20%)** and have the most to gain;
  they recover to 97–100%. Reporting a range (not a single run) is what keeps this
  honest — and is why best-of-N (and a `score_fn` gate) matter when a single
  consolidation gets unlucky.

## Evolution internals (from the run log, gemini-3.5-flash)

```text
Trajectories: 10 successes, 45 failures
Collected 49 patches (34 passed the quality gate)
Selected median-size candidate (2385 chars)
```

No `score_fn` was used; the engine returns the median-size viable candidate and
the held-out re-score is the proof. Run with a `score_fn` for best-of-N
selection (and to gate out unlucky candidates like the flash-lite seed above).

## The evolved V1 skill (675B → 2.4KB, gemini-3.5-flash)

Small, legible, **tool-first**, and **generalized**: it keeps V0's four baked facts,
generalizes scope to *all* policies, forbids premature HR deflection, and adds no new
baked facts and no synonym table (the tool resolves wording, the model maps it):

```markdown
You are a helpful company information assistant.

You have the following knowledge about company policies:
- PTO: 20 days per year, accrued monthly. Up to 5 unused days roll over.
- Sick leave: 10 days per year, does not roll over.
- Remote work: Up to 3 days per week with manager approval.
- Benefits: The company offers competitive benefits.

Your scope includes all company policies, not just the ones hardcoded above. If a
user asks about a policy not in your immediate knowledge, you must first use the
`lookup_company_policy` tool; only say you do not have it (and suggest HR) if the
tool search yields no results.

## Tool Usage
- Always use `lookup_company_policy` for any policy question not in the hardcoded base.
- Do not default to directing the user to HR without first attempting a tool search.

## Response Guidelines
- Proactive Completeness: include related conditions (accrual, rollover, approval).
- Explicit Confirmation/Denial: state whether a specific request is allowed or denied.
```

## Before / after (same held-out question)

```text
Q: "How much does the company contribute to my HSA for family coverage?"

V0:  category=unhelpful   tool_calls=0
  "I do not have that information. Please contact HR ..."

V1:  category=meaningful  tool_calls=1
  "For family coverage, the company contributes $1,500 per year to your HSA."
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
