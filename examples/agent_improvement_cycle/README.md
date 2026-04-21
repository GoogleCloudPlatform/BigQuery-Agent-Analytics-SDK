# Agent Improvement Cycle Demo

Demonstrates a closed-loop agent improvement cycle powered by the
**BigQuery Agent Analytics SDK**. The cycle learns from real agent
sessions logged in production, not just synthetic test cases.

For a guided walkthrough, see the [Demo Script](DEMO_SCRIPT.md).

## The Problem

When you design eval cases for an agent, you are guessing what users
will ask. You cover the happy paths, maybe some edge cases, but you
cannot anticipate every real question. The agent ships, users interact
with it, and some of those interactions fail in ways your tests never
predicted.

## The Solution: Learn from the Field

This demo shows how to close that gap using two SDK components:

1. **`BigQueryAgentAnalyticsPlugin`** captures every real agent session
   (questions, tool calls, responses) into BigQuery automatically.
2. **`quality_report.py`** (the SDK's evaluation script) reads those
   logged sessions back from BigQuery, evaluates quality, and produces
   structured reports that can drive automated improvement.

The full cycle:

1. **Generate** synthetic user traffic (Gemini produces diverse questions)
2. **Run** the traffic through the agent, logging sessions to BigQuery
3. **Evaluate** logged sessions using the SDK's quality evaluation
4. **Improve** the agent prompt based on what actually failed
5. **Validate** the candidate prompt against the golden eval set
   (regression gate)
6. **Extend** the golden eval set with failed cases from the synthetic
   traffic, so regressions are caught before they reach users
7. **Repeat** until quality stabilizes

The hero moment: quality typically climbs from ~30% to ~90%+ across 3 cycles
(results vary due to non-deterministic LLM output).

### Why This Matters

Static eval suites go stale. Users ask questions you never anticipated.
The plugin captures every real interaction, and the SDK's quality
evaluation scores them automatically. The improver reads those scores,
identifies the failure patterns, fixes the prompt, and extracts the
failures into the golden eval set so they never recur.

Each cycle, the golden eval set grows with cases sourced from actual
failures. Over time, your tests reflect what users actually ask, not
what you imagined they would ask.

## Architecture

```
agent/
  agent.py           # ADK agent (company policy Q&A assistant)
  prompts.py         # Versioned prompts (V1 has intentional flaws)
  prompts_v1.py      # Baseline V1 prompt (used by reset.sh)
  tools.py           # lookup_company_policy, get_current_date

eval/
  eval_cases.json    # Golden eval set (regression gate, grows each cycle)
  eval_cases_v1.json # Baseline 3-case golden set (used by reset.sh)
  generate_traffic.py # Generates synthetic user traffic via Gemini
  run_eval.py        # Runs eval/traffic cases via ADK InMemoryRunner

improver/
  improve_agent.py   # Improves prompt, validates via golden eval gate

reports/             # Generated reports, eval results

run_cycle.sh         # Orchestrator: traffic -> eval -> quality -> improve
setup.sh             # One-time setup (auth, deps, BigQuery dataset)
reset.sh             # Reset to V1 prompt and 3 golden cases
```

## How the Cycle Works

### Two Eval Sets

This demo uses two distinct sets of questions:

- **Golden eval set** (`eval_cases.json`): The regression gate. These
  cases must always pass. The set starts with 3 cases that V1 handles
  correctly and grows each cycle as failed synthetic cases are
  extracted into it.
- **Synthetic traffic**: Generated fresh each cycle by Gemini. These
  simulate diverse, unpredictable user questions that differ from the
  golden set. They are the source of new failures that drive improvement.

### The Agent

A Q&A agent built with Google ADK that answers employee questions about
company policies (PTO, sick leave, expenses, benefits, holidays). It has
two tools:

- `lookup_company_policy(topic)` - retrieves detailed policy data
- `get_current_date()` - returns today's date for relative date questions

Every session is logged to BigQuery via the `BigQueryAgentAnalyticsPlugin`,
capturing the full conversation trace: user question, tool calls, and
agent response.

### V1 Flaws (by design)

The v1 prompt has intentional problems that cause ~70% of sessions to fail:

| Flaw | Effect |
|------|--------|
| "Answer from knowledge above" | Agent ignores its tools entirely |
| No expense/holiday info in prompt | Agent says "I don't know" instead of looking it up |
| Vague "competitive benefits" | Agent deflects or hallucinates benefit details |
| No date handling guidance | Agent cannot resolve "next Friday" |

The tools themselves have all the data. The flaw is that the prompt
discourages the agent from using them.

### Step 1: Generate Synthetic Traffic (generate_traffic.py)

`generate_traffic.py` calls Gemini to produce diverse, realistic
employee questions. The questions are intentionally different from the
golden eval set, covering the same policy topics but with varied
phrasing and scenarios.

### Step 2: Run Traffic (run_eval.py)

`run_eval.py` sends the generated questions to the agent using ADK's
`InMemoryRunner`. The agent runs locally -- no server, no deployment.
Each session is automatically logged to BigQuery via the
`BigQueryAgentAnalyticsPlugin`.

```python
# From run_eval.py - this is how the agent runs locally:
runner = InMemoryRunner(
    agent=root_agent,
    app_name="company_info_agent",
    plugins=[bq_logging_plugin],   # <-- sessions auto-logged to BigQuery
)
# Send a question, get a response - just like a real user interaction
async for event in runner.run_async(user_id, session_id, user_message):
    ...
```

### Step 3: Evaluate Quality (quality_report.py)

The SDK's `quality_report.py` reads the sessions just logged to BigQuery
and scores each one:

- **response_usefulness**: Was the answer meaningful, partial, or unhelpful?
- **task_grounding**: Was it based on tool output, or did the agent hallucinate?

The `--app-name` flag filters to sessions from this agent only (ignoring
other agents sharing the same BigQuery dataset). `--output-json` produces
a structured report that the improver consumes programmatically.

### Step 4: Auto-Improve (improve_agent.py)

`improve_agent.py` reads the quality report JSON and calls Gemini to fix
the prompt:

1. **Extracts failed synthetic cases** from the quality report and
   adds them to the golden eval set (`eval_cases.json`). The golden
   set grows before the candidate is generated.
2. Reads the quality report (which sessions failed and why)
3. Reads the current prompt from `agent/prompts.py`
4. Sends both to Gemini along with the available tool list, asking it
   to fix the identified issues
5. Gemini returns a JSON with an improved prompt and a summary of changes
6. **Runs the full golden eval set** (original + extracted cases)
   against the candidate prompt (no BigQuery logging). A lightweight
   LLM judge scores each response. The candidate must pass ALL cases,
   including the failures it was designed to fix. If any case fails,
   the candidate is rejected and a new one is generated (up to 3
   attempts).
7. The script writes `PROMPT_V{N+1}` to `prompts.py` and updates
   `CURRENT_PROMPT` to point to it

On the next cycle, the agent uses the improved prompt, the golden eval
set has grown, and a fresh batch of synthetic traffic tests new edges.

### Data Flow

```
generate_traffic.py   run_eval.py          quality_report.py    improve_agent.py
  (Gemini)      -->    (agent + BQ)   -->    (BQ -> scores)  -->   |
                                                                   v
                                                             golden eval gate
                                                             (throwaway agent)
                                                                   |
                                                                   v
                                                             prompt fix +
                                                             extract failures
                                                             to golden set
```

### Guardrails

The improvement pipeline includes several safeguards to prevent
degradation:

- **Golden eval gate**: Before accepting a candidate prompt, the full
  golden eval set is run against a throwaway agent with the candidate
  prompt (no BigQuery logging). A lightweight LLM judge scores each
  response. The candidate is rejected if any golden case fails.
  This replaces LLM-based "does this look right?" validation with
  actual behavioral testing.
  - **If golden cases fail**: The candidate prompt is rejected and
    Gemini generates a new one. This retries up to 3 times. If all
    3 attempts fail the golden eval, the improvement step is skipped
    (the prompt is not changed) and the cycle continues. Failed
    synthetic cases are still extracted into the golden set. This
    means the prompt is never degraded -- the golden set is the
    hard floor that every prompt version must clear.
- **Eval case schema validation**: Extracted failure cases are checked
  for required fields (`id`, `question`, `category`, `expected_tool`).
  Malformed cases are skipped rather than written to disk.
- **Question deduplication**: Extracted cases are deduplicated by both
  ID and question text before being added to the golden set.
- **Retry with backoff**: The quality report step retries with backoff
  (up to ~60s) to handle BigQuery write propagation delays.
- **Syntax validation**: The generated `prompts.py` is compiled before
  being written, catching Python syntax errors from malformed LLM output.
- **Length check**: Prompts shorter than 50 characters are rejected as
  likely invalid.

## Quick Start

### Prerequisites

- Python 3.10+
- Google Cloud project with billing enabled
- `gcloud` CLI authenticated (`gcloud auth application-default login`)

The setup script enables the required APIs automatically:
- **BigQuery API** (`bigquery.googleapis.com`)
- **Vertex AI API** (`aiplatform.googleapis.com`)

Your authenticated user or service account needs these IAM roles:

| Role | Why |
|------|-----|
| `roles/bigquery.dataEditor` | Create datasets, write agent session data |
| `roles/bigquery.jobUser` | Run BigQuery queries for evaluation |
| `roles/aiplatform.user` | Call Gemini models (agent + evaluator + improver) |

### 1. Configure environment

Set your GCP project:

```bash
export PROJECT_ID=my-project-id
```

All other variables have sensible defaults. Only set them if you need different values:

```bash
# BigQuery dataset for session logs (defaults shown)
DATASET_ID=agent_logs
DATASET_LOCATION=us-central1
TABLE_ID=agent_events

# Agent model (defaults shown)
DEMO_MODEL_ID=gemini-2.5-flash
DEMO_AGENT_LOCATION=us-central1
```

### 2. Run setup

```bash
./setup.sh
```

This installs dependencies, verifies credentials, and creates the
BigQuery dataset if it does not exist.

### 3. Run the demo

```bash
# Single improvement cycle
./run_cycle.sh

# Full demo: 3 cycles, watch the score climb from ~30% to ~90%
./run_cycle.sh --cycles 3

# Eval only (no improvement step)
./run_cycle.sh --eval-only

# Customize traffic volume
./run_cycle.sh --cycles 3 --traffic-count 20
```

### 4. Inspect results

After a run, check the `reports/` directory:

```bash
# Quality report JSON (consumed by the improver)
cat reports/quality_report_cycle_1.json | python3 -m json.tool | head -20

# Synthetic traffic that was generated
cat eval/synthetic_traffic_cycle_1.json | python3 -m json.tool | head -20

# See how the prompt evolved
cat agent/prompts.py

# See new eval cases extracted from failures
cat eval/eval_cases.json
```

### Reset to V1

To start over, reset everything to the initial state (V1 prompt,
3 golden eval cases, no reports):

```bash
./reset.sh
```

## SDK Features Used

- **`BigQueryAgentAnalyticsPlugin`** - logs every agent session (user
  message, tool calls, agent response) to BigQuery automatically
- **`quality_report.py --app-name`** - scopes evaluation to sessions from
  this specific agent, filtering out other agents sharing the same dataset
- **`quality_report.py --output-json`** - structured quality report for
  automated consumption by the improver
- **Categorical evaluation metrics** - `response_usefulness` (was it
  helpful?) and `task_grounding` (was it based on tool output?)

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `PROJECT_ID` | from `gcloud` | Google Cloud project ID (env var or gcloud config) |
| `DATASET_ID` | `agent_logs` | BigQuery dataset for session logs |
| `DATASET_LOCATION` | `us-central1` | BigQuery dataset location |
| `TABLE_ID` | `agent_events` | BigQuery table name |
| `DEMO_MODEL_ID` | `gemini-2.5-flash` | Model for the demo agent |
| `DEMO_AGENT_LOCATION` | `us-central1` | Vertex AI location |

### Cost notes

Each improvement cycle makes Gemini API calls for traffic generation,
agent execution, quality evaluation, and prompt improvement. The golden
eval gate also calls Gemini once per golden case per attempt (up to 3
attempts per cycle). Since the golden set grows each cycle (failed
synthetic cases are extracted into it), per-cycle cost increases over
time. A typical single cycle uses ~50-80 Gemini calls; a 3-cycle run
uses ~200-300.
