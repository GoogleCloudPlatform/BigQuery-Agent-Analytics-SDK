# Verification — recorded end-to-end run

A full `./run_e2e_demo.sh` run of this example, captured so the result is
reproducible and the numbers reported in the companion blog post are backed by an
actual run (not aspirational). Measured on a **55-question held-out set** (50
single-turn + 5 multi-turn anti-parroting), and swept across four models with
**3 seeds each** to show the (real) run-to-run variance.

## Configuration

| Setting | Value |
| --- | --- |
| Agent under test (default) | `gemini-3.5-flash` (GA, Vertex `global`) |
| Evolution analysts/consolidator | `gemini-3.1-pro-preview` (Vertex `global`) |
| Judge (scoring) | `gemini-2.5-flash` (`us-central1`) |
| Ground truth | `eval/eval_spec.json` — 50 golden Q&A (matched at cosine ≥ 0.92) |
| Evolve set | `questions_evolve.json` (50, rephrased) + `questions_corrections.json` (5) |
| Held-out test set | `questions_test.json` (50) + `questions_corrections_heldout.json` (5) |
| Runtime | `setup.sh` ~5s; `run_e2e_demo.sh` ~15–18 min per run at this size |
| Date | 2026-06-23 |

The agent model, tools, and questions are identical for V0 and V1 — **only the
skill file changes** — so the delta is attributable to the skill.

## Result (recorded run, gemini-3.5-flash, held-out)

| Metric | V0 (flawed) | V1 (evolved) | Delta |
| --- | --- | --- | --- |
| Overall | 16.4% (9/55) | 98.2% (54/55) | +81.8pp |
| Single-turn | 18.0% (9/50) | 98.0% (49/50) | +80.0pp |
| Corrections (anti-parrot) | 0.0% (0/5) | 100.0% (5/5) | +100.0pp |
| Tool-grounded answers | 7% (4/55) | 80% (44/55) | — |

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
gemini-3.5-flash          99% [98-100]       91% [80-100]     17%
gemini-3.1-flash-lite     90% [71-100]       74% [56-84]      16%
gemini-2.5-pro            95% [93-96]        82%              53%
gemini-3.1-pro-preview    99% [96-100]       84% [76-95]      19%
```

Every model recovers strongly. Two honest observations the seeds surface:

- **`gemini-2.5-pro` starts highest (53%)** — it grounds on the tool even under
  the flawed prompt (43% V0 grounding), so it has the least headroom, yet still
  reaches ~95%.
- **`gemini-3.1-flash-lite` has the widest spread (71–100%)** — one of its three
  seeds got an unlucky consolidation. That variance is exactly why we report a
  range and why best-of-N (and a `score_fn` gate) matter; a single run can
  mislead.

## Evolution internals (from the run log, gemini-3.5-flash)

```text
Trajectories: 9 successes, 46 failures
Collected 51 patches (38 passed the quality gate)
Selected median-size candidate (2649 chars)
```

No `score_fn` was used; the engine returns the median-size viable candidate and
the held-out re-score is the proof. Run with a `score_fn` for best-of-N
selection (and to gate out unlucky candidates like the flash-lite seed above).

## The evolved V1 skill (675B → 2.6KB, gemini-3.5-flash)

Small, legible, **tool-first**: it lists which topics require a tool lookup,
forbids premature HR deflection, and bakes **no** specific data values (those
come from the tool at runtime):

```markdown
## Knowledge Base
- PTO: 20 days/year, accrued monthly. Up to 5 unused days roll over. ...
- Tool-Dependent Topics: You know these exist, but MUST use tools for exact
  figures/limits: Expenses & Travel, 401k (match %, vesting), Leave (parental,
  bereavement, jury, disability), EAP sessions, Flex Time, Tuition limits,
  HSA contributions, Enrollment windows.

## Instructions
Answer using the information above. If a question is about a topic not listed —
or needs specific limits/figures not provided — you must first use your search
tools. Do not immediately reply "I do not have that information" or direct the
user to HR; only suggest HR if the tool search returns nothing.
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
./run_e2e_demo.sh                        # ~15-18 min; first run also does a one-time uv sync
```

This was run from a clean checkout on **2026-06-23**, and the four-model × 3-seed
sweep above was produced by re-running with `AGENT_MODEL=<model>`. The V0 skill
is auto-restored on exit. Exact numbers vary run-to-run (LLM nondeterminism,
stochastic consolidation) — which is why the table reports ranges — but the
direction is stable: the flawed V0 defers/declines on topics it has a tool for,
and the evolved V1 uses the tool and answers correctly, including when the user
asserts a wrong "correction".
