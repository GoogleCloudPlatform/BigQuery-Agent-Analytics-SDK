# Agent Improvement Cycle - Demo Script

**Duration:** ~5 minutes (single cycle), ~15 minutes (3 cycles)
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

## Show the Golden Eval Set (30s)

**Command:**
```shell
cat eval/eval_cases.json
```

This is the golden eval set -- the regression gate. Three cases that
the V1 prompt already handles correctly: PTO days, sick leave, and
remote work. The golden set starts small and grows each cycle as
failed synthetic cases are extracted into it.

These cases are the floor. No prompt change is accepted unless every
golden case still passes. As the cycle runs, failed synthetic cases
get added here, raising the bar each iteration.

---

## Run One Cycle (~5 min)

**Command:**
```shell
./run_cycle.sh
```

### Pre-flight: Golden Eval (~25s)

The script starts by running the golden eval set against the current
prompt. This verifies the starting point: all 3 cases should pass
with V1. If any fail, the script stops with a non-zero exit code --
fix the prompt first.

### Step 1: Generate Synthetic Traffic (~20s)

The script calls Gemini to generate 10 diverse user questions. These
are intentionally different from the golden set -- varied phrasing,
situational questions, covering all six policy topics. They simulate
real-world traffic the agent has not been tuned for.

### Step 2: Run Traffic Through Agent (~30-40s)

The generated questions are sent to the agent using ADK's
`InMemoryRunner`. The agent runs locally and executes its tools
against local policy data.

Every session is automatically logged to BigQuery by the
[BigQuery Agent Analytics plugin](https://adk.dev/integrations/bigquery-agent-analytics/).
The full trace is captured: user question, tool calls, LLM responses.
No extra logging code required.

*(as output scrolls)* Some questions get proper answers, others get
"I don't have that information, contact HR."

### Step 3: Evaluate Quality (~25s)

The SDK's quality report reads those sessions back from BigQuery and
scores each one on two dimensions:

- **Response usefulness:** Was the answer meaningful, partial, or
  unhelpful?
- **Task grounding:** Was the answer based on tool output, or did the
  agent make something up?

*(point to the quality summary)* Around 30% meaningful. The agent had
the right tools all along -- the prompt just did not let it use them.

### Step 4: Improve Prompt (~1-2 min)

This step does three things:

1. **Extract failures:** Failed synthetic cases are pulled from the
   quality report and added to the golden eval set. The golden set
   grows from 3 to ~10 cases.

2. **Rewrite:** Gemini receives the current prompt, the quality report,
   and the available tool signatures. It analyzes what went wrong and
   generates a new candidate prompt.

3. **Regression gate:** The candidate is tested against the FULL
   golden set (original 3 + extracted failures). The candidate must
   pass ALL cases -- not just the original 3, but also the failures
   it was designed to fix. If any case fails, the candidate is
   rejected and Gemini generates a new one (up to 3 attempts).

*(point to output)* V1 becomes V2. The candidate passed all 10 cases.

### Step 5: Measure Improvement (~2-3 min)

Step 5 mirrors Steps 1-3 but with the improved prompt: generate
fresh traffic, run it through the agent, and score from BigQuery.

1. **Regression check:** Run the extended golden eval (10 cases) to
   verify the improved prompt still passes everything.

2. **Fresh traffic:** Gemini generates a NEW batch of 10 questions.
   Re-running the Step 1 traffic would be circular -- the prompt was
   specifically fixed to handle those questions.

3. **Run through agent:** The fresh questions are sent to the V2
   agent and logged to BigQuery -- exactly like Step 2.

4. **Score from BigQuery:** The SDK's quality report reads the new
   sessions from BigQuery and scores them -- exactly like Step 3.

*(point to the results box)*

```
  Before (V1):   30% meaningful  (3/10 sessions)
  After  (V2):  100% meaningful  (10/10 sessions)
```

From 30% to 100% in one automated cycle, scored from BigQuery on
entirely new questions.

---

## Multi-Cycle Run (optional, ~15 min)

To show the full loop with prompt refinement across cycles:

```shell
./reset.sh
./run_cycle.sh --cycles 3
```

Each cycle generates fresh synthetic traffic, evaluates, improves, and
measures. The golden eval set grows with each cycle as new edge cases
are discovered and locked in.

---

## Wrap-Up (30s)

**Command:**
```shell
git diff agent/prompts.py
git diff eval/eval_cases.json
```

The prompt evolved from V1 to V2 (or V4 with 3 cycles), each version
addressing specific failures from the previous cycle's synthetic
traffic. The golden eval set grew from 3 cases to 10+ cases, each new
case extracted from a real failure.

The key idea: the golden eval set is the regression gate. Synthetic
traffic discovers new failures. The improver fixes the prompt. The
golden eval ensures nothing breaks. Failed cases are extracted into the
golden set so they never recur. Over time, your tests reflect what
users actually ask -- not what you guessed they would.

To reset and run again:
```shell
./reset.sh
```
