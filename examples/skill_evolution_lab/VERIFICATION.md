# Verification — recorded end-to-end run

A full `./run_e2e_demo.sh` run of this example, captured so the result is
reproducible and the numbers in [`DEMO_NARRATION.md`](DEMO_NARRATION.md) are
backed by an actual run (not aspirational).

## Configuration

| Setting | Value |
| --- | --- |
| Agent under test | `gemini-3-flash-preview` (Vertex `global`) |
| Evolution analysts/consolidator | `gemini-3.1-pro-preview` (Vertex `global`) |
| Judge (scoring) | `gemini-2.5-flash` (`us-central1`) |
| Ground truth | `eval/eval_spec.json` golden Q&A (matched at cosine ≥ 0.92) |
| Evolve set | `questions_evolve.json` (28) + `questions_corrections.json` (5) |
| Held-out test set | `questions_test.json` (18) + `questions_corrections_heldout.json` (3) |
| Date | 2026-06-05 |

The agent model, tools, and questions are identical for V0 and V1 — **only the
skill file changes** — so the delta is attributable to the skill.

## Result (held-out set, golden-grounded correctness)

| Metric | V0 (flawed) | V1 (evolved) | Delta |
| --- | --- | --- | --- |
| Overall | 23.8% (5/21) | 100.0% (21/21) | +76.2pp |
| Single-turn | 22.2% (4/18) | 100.0% (18/18) | +77.8pp |
| Corrections (anti-parrot) | 33.3% (1/3) | 100.0% (3/3) | +66.7pp |
| Tool-grounded answers | 6/21 | 18/21 | — |

Parroted sub-trajectories: V0 = 0, V1 = 0. In this run the flawed V0 *declined*
on the correction cases ("I don't have that, contact HR") rather than caving to
the user's wrong number, so the engine learned the tool-first rule that
subsumes the correction cases; the explicit `PARROTING` detection/learning
machinery (in `quality_report.py` and `skill_evolution.py`) is the safety net
that prevents the opposite failure — learning to agree with a confident, wrong
user.

## Evolution internals (from the run log)

```text
Trajectories: 6 successes, 27 failures
Collected 29 patches (19 passed the quality gate)
Generating 3 candidate(s)...
Selected median-size candidate (2519 chars)
```

No `score_fn` was used; the engine returns the median-size viable candidate and
the held-out re-score is the proof. Run with a `score_fn` for best-of-N
selection.

## The evolved V1 skill (675B → 2519B)

The engine rewrote the flawed "answer only from the baked summary, else contact
HR" prompt into a small, legible, tool-first skill. Notably it learned a
**"Premature HR Deflection"** anti-pattern and a tool-first fallback rule:

```markdown
---
name: company-policy
description: Answers employee questions about company policies.
metadata:
  version: "1"
  author: skill-evolution
  evolvable: true
  evolved_from: "0"
---

You are a helpful company information assistant.

## Knowledge Base
You have the following knowledge about company policies:
- **PTO:** 20 days per year, accrued monthly. Up to 5 unused days roll over. ...
- **Sick leave:** 10 days per year, does not roll over. (For specific details ...
  use your tools to search the policy database).
- **Remote work:** Up to 3 days per week with manager approval. ...
- **Benefits:** ... For exact monetary limits, match percentages, or session
  limits, use your tools to search or advise the user to check the Benefits Handbook.
- **Expenses and Travel:** ... There is a daily meal reimbursement limit on
  business travel (use tools to find the exact amount).
- **Flex time / Work hours:** Employees may adjust their daily start and end times ...

## Instructions
- **Tool Use & Fallback:** If a user asks about a company policy or detail not
  explicitly listed in your provided knowledge above ..., you MUST first use your
  available tools to search for the information. Only tell the user you do not
  have the information ... if your tool search yields no relevant results.
- **Policy Evaluation:** When a user asks if a specific amount or scenario is
  allowed ..., explicitly compare their request to the policy limits ...

## Anti-Patterns
- **Premature HR Deflection:** Do not immediately tell the user you lack
  information or direct them to HR for policy topics not listed in your static
  knowledge. You must always attempt to use your available tools first.
```

## Reproduce

```bash
cd examples/skill_evolution_lab
./setup.sh YOUR_PROJECT_ID us-central1
./run_e2e_demo.sh
```

Exact numbers vary run-to-run (LLM nondeterminism, golden-match set), but the
direction is stable: the flawed V0 defers/declines on topics it has a tool for,
and the evolved V1 uses the tool and answers correctly, including when the user
asserts a wrong "correction".
