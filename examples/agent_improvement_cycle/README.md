# Agent Improvement Cycle Demo

Demonstrates a closed-loop agent improvement cycle powered by the
**BigQuery Agent Analytics SDK**. The cycle learns from real agent
sessions logged in production, not just synthetic test cases.

## The Problem

When you design eval cases for an agent, you are guessing what users
will ask. You cover the happy paths, maybe some edge cases, but you
cannot anticipate every real question. The agent ships, users interact
with it, and some of those interactions fail in ways your tests never
predicted.

## The Solution: Learn from the Field

This demo shows how to close that gap:

1. **Run** a Q&A agent that answers employee policy questions
2. **Log** every session to BigQuery via the `BigQueryAgentAnalyticsPlugin`
3. **Evaluate** logged sessions with LLM-as-a-judge quality metrics
   (`response_usefulness`, `task_grounding`) using the SDK's `quality_report.py`
4. **Improve** the agent prompt based on what actually failed in production
5. **Extend** the eval test suite with new cases derived from real failures,
   so future regressions are caught before they reach users
6. **Repeat** until quality stabilizes

The hero moment: quality climbs from ~30% to ~90%+ across 3 cycles.

### Why This Matters

Static eval suites go stale. Users ask questions you never anticipated.
The BigQuery Agent Analytics plugin captures every real interaction, and
the SDK's quality evaluation scores them automatically. The improver reads
those scores, identifies the failure patterns, fixes the prompt, and
generates new eval cases so the same failures never recur.

Each cycle, the eval suite grows with cases sourced from actual production
failures. Over time, your tests reflect what users actually ask, not what
you imagined they would ask.

## Architecture

```
agent/
  agent.py       # ADK agent (company policy Q&A assistant)
  prompts.py     # Versioned prompts (V1 has intentional flaws)
  tools.py       # lookup_company_policy, get_current_date

eval/
  eval_cases.json   # Test questions with expected behavior
  run_eval.py       # Runs eval cases via ADK InMemoryRunner

improver/
  improve_agent.py  # Reads quality report, calls Gemini to fix prompt

reports/            # Generated reports and eval results

run_cycle.sh        # Orchestrator: eval -> quality report -> improve
setup.sh            # One-time setup (auth, deps, BigQuery dataset)
```

## How the Cycle Works

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

### The Fix Loop

Each cycle:

1. **Eval** - `run_eval.py` sends questions to the agent. Sessions are
   logged to BigQuery via the analytics plugin.
2. **Analyze** - `quality_report.py --app-name --output-json` reads the
   logged sessions from BigQuery and scores each one with LLM-as-a-judge
   metrics: was the response helpful? Was it grounded in tool output?
3. **Improve** - `improve_agent.py` reads the quality report JSON, calls
   Gemini to generate a fixed prompt addressing the specific failures, and
   adds new eval cases targeting those failure modes.
4. **Version** - The new prompt is appended to `prompts.py` as `PROMPT_VN`
   and `CURRENT_PROMPT` is updated. The eval suite grows with each cycle.

### Data Flow

```
Agent sessions  -->  BigQuery  -->  quality_report.py  -->  improve_agent.py
(via plugin)         (storage)      (LLM-as-judge)          (prompt fix +
                                                             new eval cases)
```

## Quick Start

### Prerequisites

- Python 3.10+
- Google Cloud project with BigQuery enabled
- `gcloud` CLI authenticated

### Setup

```bash
./setup.sh
```

### Run the Demo

```bash
# Single cycle
./run_cycle.sh

# Full demo: 3 cycles, watch the score climb
./run_cycle.sh --cycles 3

# Eval only (no improvement)
./run_cycle.sh --eval-only
```

### Reset to V1

To start over, reset the prompt and eval cases:

```bash
git checkout -- agent/prompts.py eval/eval_cases.json
```

## SDK Features Used

- **`BigQueryAgentAnalyticsPlugin`** - logs every agent session (user
  message, tool calls, agent response) to BigQuery automatically
- **`quality_report.py --app-name`** - scopes evaluation to sessions from
  this specific agent, filtering out other agents sharing the same dataset
- **`quality_report.py --output-json`** - structured quality report for
  automated consumption by the improver
- **LLM-as-a-judge metrics** - `response_usefulness` (was it helpful?) and
  `task_grounding` (was it based on tool output, not hallucination?)

## Configuration

Environment variables (set in `src/.env` or export directly):

| Variable | Default | Description |
|----------|---------|-------------|
| `TEST_DATASET_ID` | `agent_logs` | BigQuery dataset for session logs |
| `TEST_BQ_LOCATION` | `us-central1` | BigQuery dataset location |
| `TEST_TABLE_ID` | `agent_events` | BigQuery table name |
| `DEMO_MODEL_ID` | `gemini-2.5-flash` | Model for the demo agent |
| `DEMO_AGENT_LOCATION` | `us-central1` | Vertex AI location |
