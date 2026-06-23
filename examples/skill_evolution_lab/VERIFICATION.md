# Verification — recorded end-to-end run

A full `./run_e2e_demo.sh` run of this example, captured so the result is
reproducible and the numbers reported in the companion blog post are backed by an
actual run (not aspirational).

## Configuration

| Setting | Value |
| --- | --- |
| Agent under test (default) | `gemini-3.5-flash` (GA, Vertex `global`) |
| Evolution analysts/consolidator | `gemini-3.1-pro-preview` (Vertex `global`) |
| Judge (scoring) | `gemini-2.5-flash` (`us-central1`) |
| Ground truth | `eval/eval_spec.json` golden Q&A (matched at cosine ≥ 0.92) |
| Evolve set | `questions_evolve.json` (28) + `questions_corrections.json` (5) |
| Held-out test set | `questions_test.json` (18) + `questions_corrections_heldout.json` (3) |
| Runtime | `setup.sh` ~5s; `run_e2e_demo.sh` ~6 min (first run adds a one-time `uv` sync) |
| Date | 2026-06-23 |

The agent model, tools, and questions are identical for V0 and V1 — **only the
skill file changes** — so the delta is attributable to the skill.

## Result (held-out set, golden-grounded correctness)

| Metric | V0 (flawed) | V1 (evolved) | Delta |
| --- | --- | --- | --- |
| Overall | 14.3% (3/21) | 100.0% (21/21) | +85.7pp |
| Single-turn | 16.7% (3/18) | 100.0% (18/18) | +83.3pp |
| Corrections (anti-parrot) | 0.0% (0/3) | 100.0% (3/3) | +100.0pp |
| Tool-grounded answers | 2/21 (10%) | 18/21 (86%) | — |

The flawed V0 barely calls the tool (it's told not to), so it declines on almost
everything; the evolved V1 uses the tool and answers correctly — including the
multi-turn correction cases, where it re-verifies and holds the right figure
instead of caving (sub-trajectory outcome `recovered`, not `parroted`).

## Across four models (held-out, golden-grounded)

The same loop, run per agent model (analyst + judge fixed):

```text
Model                     Correctness     Grounding
                          V0  ->  V1      V0  ->  V1
-----------------------   -----------     -----------
gemini-3.5-flash          14.3% -> 100%   10% -> 86%
gemini-3.1-flash-lite     14.3% ->  95%    0% -> 86%
gemini-2.5-pro            57.1% ->  95%   52% -> 86%
gemini-3.1-pro-preview    14.3% ->  67%    0% -> 52%
```

Every model improves substantially. `gemini-2.5-pro` starts highest (it grounds
on the tool even under the flawed prompt, 52%); the `gemini-3.1-pro-preview`
agent recovers the least (67%) — its evolved skill was tool-first but kept a soft
"check the benefits portal" clause, so it still deferred on some benefits
questions. Numbers vary run-to-run (consolidation is stochastic).

## Evolution internals (from the run log, gemini-3.5-flash)

```text
Trajectories: 6 successes, 27 failures
Collected 29 patches (21 passed the quality gate)
Generating 3 candidate(s)...
Selected median-size candidate (2039 chars)
```

No `score_fn` was used; the engine returns the median-size viable candidate and
the held-out re-score is the proof. Run with a `score_fn` for best-of-N
selection.

## The evolved V1 skill (675B → 2039B, gemini-3.5-flash)

The engine rewrote the flawed "answer only from the baked summary, else contact
HR" prompt into a small, legible, **tool-first** skill — it lists exactly which
topics to look up with tools, and explicitly forbids premature HR deflection. It
does **not** bake specific data values (those come from the tool at runtime):

```markdown
## Instructions
Answer questions using the information above when applicable.

If a question is about a topic not explicitly listed above, do NOT immediately
tell the user you lack the information. You must first use your available tools
to search for the policy details before falling back to suggesting they contact
HR. Always use tools to look up details for in-scope topics such as:
- Specific Benefits: health/dental/vision insurance, HSA contributions, EAP.
- Leave Policies: parental leave, bereavement leave, jury duty, paid holidays.
- Financial Policies: 401k matching, meal reimbursement limits, tuition reimbursement.

If your tool search yields no results, only then tell the user you do not have
that information and suggest they contact HR.
```

## Before / after (same held-out question)

```text
Q: "How much does the company contribute to my HSA for family coverage?"

V0:  category=unhelpful   tool_calls=0
  "I do not have that information. I suggest you contact HR ..."

V1:  category=meaningful  tool_calls=1
  "For family coverage, the company contributes $1,500 per year to your HSA."
```

## Reproduce (tested)

```bash
cd examples/skill_evolution_lab
./setup.sh YOUR_PROJECT_ID us-central1   # ~5s
./run_e2e_demo.sh                        # ~6 min; first run also does a one-time uv sync
```

This was run from a clean checkout (no `.env`, no `runs/`) on **2026-06-23**
following only the two documented commands, reproducing the table above
(Overall **14.3% → 100%, +85.7pp**); the V0 skill was auto-restored on exit. The
four-model sweep above was produced by re-running with `AGENT_MODEL=<model>`.

Exact numbers vary run-to-run (LLM nondeterminism, golden-match set, stochastic
consolidation), but the direction is stable: the flawed V0 defers/declines on
topics it has a tool for, and the evolved V1 uses the tool and answers correctly,
including when the user asserts a wrong "correction".
