# Agent Improvement Cycle - Demo Script

**Duration:** ~6 minutes (single cycle), ~18 minutes (3 cycles)
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
evaluates its own quality, and uses the **Vertex AI Prompt Optimizer**
to fix what failed. Prompts are stored in the **Vertex AI Prompt
Registry**, versioned automatically with every improvement.

---

## Setup (1 min)

**Command:**
```shell
./setup.sh
```

The setup script performs six checks:

1. **Python version:** Verifies Python 3.10+ is installed.
2. **Google Cloud auth:** Confirms `gcloud` is authenticated and a
   project is set.
3. **APIs:** Enables BigQuery and Vertex AI APIs if not already active.
4. **Dependencies:** Installs Python packages (`google-cloud-aiplatform`,
   `google-adk`, `google-genai`, `pandas`, `python-dotenv`).
5. **Configuration:** Creates the `.env` file with your project ID,
   BigQuery dataset, and table name. Creates the BQ dataset if needed.
6. **Vertex AI prompt:** Creates the V1 prompt in the Vertex AI Prompt
   Registry and writes the prompt ID to `.env` and `config.json`.

---

## Show the Config (30s)

**Command:**
```shell
cat config.json
```

This is the declarative config that drives the entire cycle. Key fields:

- `prompt_storage: "vertex"` -- the prompt lives in Vertex AI, not a
  local file
- `use_vertex_optimizer: true` -- improvements use the Vertex AI Prompt
  Optimizer with synthetic ground truth from a teacher model
- `vertex_prompt_id` -- the Vertex AI prompt resource (auto-filled by
  setup)

The same config works for any ADK agent. Change the `agent_module` and
paths to point at a different agent.

---

## Show the V1 Prompt (30s)

**Command:**
```shell
cat agent/prompts.py
```

This is the V1 seed prompt. It has intentional flaws that mirror common
real-world mistakes:

- It tells the agent to "answer from the knowledge above" instead of
  calling its tools.
- It covers PTO, sick leave, and remote work, but says nothing about
  expenses or holidays. Those tools exist, but the prompt ignores them.
- Benefits are described as "competitive" with no details. The agent
  will guess or deflect.
- There is no mention of the `get_current_date` tool, so date-related
  questions like "Is next Friday a holiday?" will fail.

The tools can answer all of these questions. The prompt simply does not
guide the agent to use them. The agent reads this prompt from the
Vertex AI Prompt Registry at startup (not from the file directly).

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

## Run One Cycle (~6 min)

**Command:**
```shell
./run_cycle.sh
```

### Pre-flight: Golden Eval (~25s)

The script starts by running the golden eval set against the current
prompt. This verifies the starting point: all 3 cases should pass
with V1. If any fail, the script auto-improves to fix them before
proceeding.

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

*(point to the quality summary)* Around 40% meaningful. The agent had
the right tools all along -- the prompt just did not let it use them.

### Step 4: Improve Prompt (~1-2 min)

This step uses an ADK **LoopAgent** -- an agent that improves the
agent. It has six tools and decides its own workflow:

1. **Extract failures:** Failed synthetic cases are pulled from the
   quality report and added to the golden eval set. The golden set
   grows from 3 to ~10 cases.

2. **Generate ground truth:** A "teacher agent" (same tools, better
   prompt) re-answers each failed question to produce what the correct
   response should have been. This is the synthetic ground truth.

3. **Optimize prompt:** The current prompt + (question, bad_response,
   ground_truth) triples are sent to the **Vertex AI Prompt Optimizer**
   in `target_response` mode. The optimizer generates a structurally
   improved prompt.

4. **Regression gate:** The optimized prompt is tested against the
   FULL golden set (original 3 + extracted failures). All cases must
   pass. If any fail, the LLM analyzes why and generates a better
   candidate. The loop exits when all cases pass (via `exit_loop`)
   or after 3 iterations.

5. **Write to Vertex AI:** The validated prompt is written to the
   Vertex AI Prompt Registry as a new version.

The improvement module is reusable -- it works with any ADK agent, not
just this demo. You provide a `config.json` with your agent module,
tools, and eval set.

*(point to output)* V1 becomes V2. The candidate passed all 10 cases.

### Step 5: Measure Improvement (~2-3 min)

Step 5 mirrors Steps 1-3 but with the improved prompt:

1. **Fresh traffic:** Gemini generates a NEW batch of 10 questions.
   Re-running the Step 1 traffic would be circular -- the prompt was
   specifically fixed to handle those questions.

2. **Run through agent:** The fresh questions are sent to the V2
   agent and logged to BigQuery -- exactly like Step 2.

3. **Score from BigQuery:** The SDK's quality report reads the new
   sessions from BigQuery and scores them -- exactly like Step 3.

*(point to the results box)*

```
  Before (V1):   40% meaningful  (4/10 sessions)
  After  (V2): 100% meaningful (10/10 sessions)
```

From 40% to 100% in one automated cycle, scored from BigQuery on
entirely new questions.

---

## Multi-Cycle Run (optional, ~18 min)

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
cat eval/eval_cases.json
```

The prompt evolved from V1 to V2 (or V4 with 3 cycles), each version
stored as a new version in the Vertex AI Prompt Registry. The golden
eval set grew from 3 cases to 10+ cases, each new case extracted from
a real failure.

The key idea: the golden eval set is the regression gate. Synthetic
traffic discovers new failures. The Vertex AI Prompt Optimizer fixes
the prompt using teacher-generated ground truth. The golden eval
ensures nothing breaks. Failed cases are extracted into the golden set
so they never recur. Over time, your tests reflect what users actually
ask -- not what you guessed they would.

To reset and run again:
```shell
./reset.sh
```
