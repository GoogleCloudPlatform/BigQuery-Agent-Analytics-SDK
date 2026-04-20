# Agent Improvement Cycle Demo

Demonstrates a full agent improvement cycle using the BigQuery Agent Analytics SDK:

1. **Build** a simple agent with intentional flaws
2. **Run** it against test questions (eval cases)
3. **Analyze** sessions using LLM-as-a-judge quality evaluation
4. **Improve** the agent prompt automatically and extend eval tests
5. **Repeat** and watch the quality score climb

The hero moment: quality goes from ~40% to ~90%+ across 3 cycles.

## Architecture

```
agent/
  agent.py       # ADK agent (company info assistant)
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

### V1 Flaws (by design)

The v1 prompt has 4 intentional problems:

| Flaw | Effect | Expected Fix |
|------|--------|-------------|
| No expense info in prompt | Agent can't answer expense questions | Add tool-use guidance for expenses |
| Vague "competitive benefits" | Agent hallucinates benefit details | Add instruction to use lookup tool |
| No date handling guidance | Agent asks user to clarify dates | Add get_current_date tool guidance |
| No tool-grounding enforcement | Agent answers from LLM knowledge | Add "always use tools" instruction |

### The Fix Loop

Each cycle:
1. `run_eval.py` sends 10+ questions to the agent, sessions logged to BigQuery
2. `quality_report.py --output-json` evaluates sessions with LLM-as-a-judge
3. `improve_agent.py` reads the JSON report, calls Gemini to:
   - Generate an improved prompt fixing the identified issues
   - Create 2-4 new eval cases testing the fixes
4. New prompt is appended to `prompts.py` as `PROMPT_VN`, `CURRENT_PROMPT` updated

## Quick Start

### Prerequisites

- Python 3.10+
- Google Cloud project with BigQuery enabled
- `gcloud` CLI authenticated

### Setup

```bash
# One-time setup
./setup.sh
```

### Run the Demo

```bash
# Single cycle
./run_cycle.sh

# Full demo: 3 cycles
./run_cycle.sh --cycles 3

# Eval only (no improvement)
./run_cycle.sh --eval-only
```

### Reset to V1

To start over, reset `prompts.py` and `eval_cases.json` to their original state:

```bash
git checkout -- agent/prompts.py eval/eval_cases.json
```

## SDK Features Used

- **BigQueryAgentAnalyticsPlugin**: Logs all agent sessions to BigQuery
- **quality_report.py --app-name**: Scopes evaluation to this agent only
- **quality_report.py --output-json**: Structured output for automated consumption
- **LLM-as-a-judge metrics**: `response_usefulness` and `task_grounding`

## Configuration

Environment variables (set in `src/.env` or export directly):

| Variable | Default | Description |
|----------|---------|-------------|
| `TEST_DATASET_ID` | `agent_logs` | BigQuery dataset for session logs |
| `TEST_BQ_LOCATION` | `us-central1` | BigQuery dataset location |
| `TEST_TABLE_ID` | `agent_events` | BigQuery table name |
| `DEMO_MODEL_ID` | `gemini-2.5-flash` | Model for the demo agent |
| `DEMO_AGENT_LOCATION` | `us-central1` | Vertex AI location |
