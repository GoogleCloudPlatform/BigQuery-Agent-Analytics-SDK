# Agent Improvement Cycle - Demo Script

**Duration:** ~5-7 minutes
**Format:** Live terminal walkthrough

---

## Introduction (30s)

Agents break in production. You write eval cases, you ship, and then
users ask questions you never thought of. The eval suite goes stale.
Failures pile up silently.

This demo shows a way to fix that: a closed-loop improvement cycle
using the
[BigQuery Agent Analytics SDK](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK).
The agent runs, logs sessions to BigQuery using the
[BigQuery Agent Analytics plugin for ADK](https://adk.dev/integrations/bigquery-agent-analytics/),
evaluates its own quality, and rewrites its prompt to fix what failed.
Four steps, fully automated.

---

## Show the V1 Prompt (30s)

**Command:**
```shell
cat agent/prompts.py
```

This is the starting prompt, version 1. It has intentional flaws that
mirror common real-world mistakes:

- It tells the agent to "answer from the knowledge above" instead of
  calling its tools.
- It covers PTO, sick leave, and remote work, but says nothing about
  expenses or holidays. Those tools exist, but the prompt ignores them.
- Benefits are described as "competitive" with no details. The agent
  will guess or deflect.
- There is no mention of the `get_current_date` tool, so date-related
  questions like "Is next Friday a holiday?" will fail.

The tools can answer all of these questions. The prompt simply does not
guide the agent to use them.

---

## Show the Golden Eval Set (20s)

**Command:**
```shell
cat eval/eval_cases.json
```

This is the golden eval set -- the regression gate. Ten cases that the
agent must always pass. Three are straightforward ("How many PTO days
do I get?"), seven target the blind spots. The golden set starts here
and grows each cycle as failed cases are extracted into it.

---

## Cycle 1 - Step 1: Generate Synthetic Traffic (20s)

**Command:**
```shell
./run_cycle.sh --cycles 3
```

First, the script calls Gemini to generate 15 diverse user questions.
These are intentionally different from the golden set -- varied
phrasing, edge cases, situational questions. They simulate real-world
traffic the agent has not been tuned for.

---

## Cycle 1 - Step 2: Run Traffic (30s)

The generated questions are sent to the agent using ADK's
`InMemoryRunner`. The agent runs locally and executes its tools
against local policy data.

Every session is automatically logged to BigQuery by the
[BigQuery Agent Analytics plugin](https://adk.dev/integrations/bigquery-agent-analytics/).
The full trace is captured: user question, tool calls, LLM responses.
No extra logging code required.

*(as output scrolls)* Some questions get proper answers, others get
"I don't have that information, contact HR."

---

## Cycle 1 - Step 3: Evaluate Quality (30s)

The SDK's quality report reads those sessions back from BigQuery and
scores each one on two dimensions:

- **Response usefulness:** Was the answer meaningful, partial, or
  unhelpful?
- **Task grounding:** Was the answer based on tool output, or did the
  agent make something up?

*(point to the quality summary)* Low meaningful rate. The agent had the
right tools all along -- the prompt just did not let it use them.

---

## Cycle 1 - Step 4: Auto-Improve (45s)

The improver sends three things to Gemini: the current prompt, the
quality report, and a list of the agent's available tools
(`lookup_company_policy`, `get_current_date`) with their signatures.
Gemini analyzes what went wrong and rewrites the prompt.

Before accepting, the golden eval gate runs: every case in the golden
set is tested against the candidate prompt using a throwaway agent
(no BigQuery logging). If any golden case fails, the candidate is
rejected and Gemini generates a new one.

Once the golden eval passes, the improved prompt is written to disk.
Then the failed synthetic cases are extracted from the quality report
and added to the golden eval set. Next cycle, those cases become part
of the regression gate.

*(point to output)* V1 becomes V2. The golden set grows from 10 to
roughly 14-16 cases.

---

## Cycle 2 (30s)

Cycle 2 runs the same four steps. Fresh synthetic traffic is generated,
the agent now runs with V2, and the golden set includes the cases that
failed in cycle 1.

*(as it runs)* Questions about expenses and benefits now get real
answers via `lookup_company_policy`.

*(point to quality score)* Quality jumps. A few edge cases may remain,
like date-dependent questions.

---

## Cycle 3 (30s)

The prompt is refined one more time. Date handling instructions are
added. Holiday lookup is combined with `get_current_date`.

*(point to final score)* Over 90% meaningful. From low scores to 90%
in three automated cycles, with no manual prompt engineering.

---

## Wrap-Up (30s)

**Command:**
```shell
git diff agent/prompts.py
git diff eval/eval_cases.json
```

Three prompt versions, each one addressing specific failures from the
previous cycle's synthetic traffic. The golden eval set grew from 10
cases to roughly 20+, each new case extracted from a real failure.

The key idea: the golden eval set is the regression gate. Synthetic
traffic discovers new failures. The improver fixes the prompt. The
golden eval ensures nothing breaks. Failed cases are extracted into the
golden set so they never recur. Over time, your tests reflect what
users actually ask -- not what you guessed they would.
