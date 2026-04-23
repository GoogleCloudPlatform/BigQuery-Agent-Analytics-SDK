# Agent Improvement Cycle Demo

Demonstrates a closed-loop agent improvement cycle powered by the
**BigQuery Agent Analytics SDK** and **Vertex AI Prompt Registry**.
The cycle learns from real agent sessions logged in production, not
just synthetic test cases. Prompts are stored, versioned, and
optimized in Vertex AI.

For a guided walkthrough, see the [Demo Script](DEMO_SCRIPT.md).

## The Problem

When you design eval cases for an agent, you are guessing what users
will ask. You cover the happy paths, maybe some edge cases, but you
cannot anticipate every real question. The agent ships, users interact
with it, and some of those interactions fail in ways your tests never
predicted.

## The Solution: Learn from the Field

This demo shows how to close that gap using three components:

1. **`BigQueryAgentAnalyticsPlugin`** captures every real agent session
   (questions, tool calls, responses) into BigQuery automatically.
2. **`quality_report.py`** (the SDK's evaluation script) reads those
   logged sessions back from BigQuery, evaluates quality, and produces
   structured reports that can drive automated improvement.
3. **Vertex AI Prompt Registry** stores and versions the agent's prompt
   in the cloud. The **Vertex AI Prompt Optimizer** generates improved
   prompts using synthetic ground truth from a teacher model.

The full cycle:

1. **Generate** synthetic user traffic (Gemini produces diverse questions)
2. **Run** the traffic through the agent, logging sessions to BigQuery
3. **Evaluate** logged sessions using the SDK's quality evaluation
4. **Improve** the agent prompt using the Vertex AI Prompt Optimizer
   with synthetic ground truth from a teacher model
5. **Validate** the candidate prompt against the golden eval set
   (regression gate)
6. **Extend** the golden eval set with failed cases from the synthetic
   traffic, so regressions are caught before they reach users
7. **Repeat** until quality stabilizes

The hero moment: quality typically climbs from ~40% to ~100% in a single cycle
(results vary due to non-deterministic LLM output).

### Why This Matters

Static eval suites go stale. Users ask questions you never anticipated.
The plugin captures every real interaction, and the SDK's quality
evaluation scores them automatically. The Vertex AI Prompt Optimizer
reads those scores, generates ground truth via a teacher model,
optimizes the prompt, and the pipeline extracts the failures into the
golden eval set so they never recur.

Each cycle, the golden eval set grows with cases sourced from actual
failures. Over time, your tests reflect what users actually ask, not
what you imagined they would ask.

## Architecture

```
config.json              # Declarative config: agent module, prompt storage,
                         # model, eval paths, optimizer settings

agent/
  agent.py               # ADK agent (company policy Q&A assistant)
                         # Reads prompt from Vertex AI Prompt Registry
  prompts.py             # V1 seed prompt (used by setup/reset only)
  tools.py               # lookup_company_policy, get_current_date

eval/
  eval_cases.json        # Golden eval set (regression gate, grows each cycle)
  generate_traffic.py    # Generates synthetic user traffic via Gemini
  run_eval.py            # Runs eval/traffic cases via ADK InMemoryRunner

agent_improvement/       # Reusable improvement module (works with any ADK agent)
  config.py              # ImprovementConfig dataclass
  config_loader.py       # Loads config.json, builds ImprovementConfig
  improver_agent.py      # LoopAgent + LlmAgent with tool-based workflow
  eval_runner.py         # Run eval cases + LLM judge
  prompt_adapter.py      # PromptAdapter ABC + VertexPromptAdapter +
                         # PythonFilePromptAdapter
  tool_introspection.py  # Auto-extract tool signatures from agent tools
  prompts.py             # Default judge/improver prompt templates

run_improvement.py       # Entry point: loads config.json, runs improvement
setup_vertex.py          # Creates/resets Vertex AI prompt (called by setup.sh)
reports/                 # Generated reports, eval results, ground truth

run_cycle.sh             # Orchestrator: traffic -> eval -> quality -> improve
setup.sh                 # One-time setup (auth, deps, BigQuery, Vertex AI prompt)
reset.sh                 # Reset to V1 prompt, prompts.py, and 3 golden cases
show_prompt.sh           # Display current prompt from Vertex AI (curl + jq)
```

### config.json

All agent-specific settings live in a single declarative config file:

```json
{
  "app_name": "company_info_agent",
  "agent_module": "agent.agent",
  "prompts_path": "agent/prompts.py",
  "prompt_variable": "CURRENT_PROMPT",
  "version_variable": "CURRENT_VERSION",
  "eval_cases_path": "eval/eval_cases.json",
  "traffic_generator": "eval/generate_traffic.py",
  "model_id": "gemini-2.5-flash",
  "max_attempts": 3,
  "prompt_storage": "vertex",
  "vertex_prompt_id": "1234567890",
  "use_vertex_optimizer": true,
  "teacher_model_id": null
}
```

To point the cycle at a different agent, create a `config.json` for it
and pass `--agent-config /path/to/config.json`.

## How the Cycle Works

### Prompt Storage: Vertex AI Prompt Registry

The agent's prompt is stored in the
[Vertex AI Prompt Registry](https://cloud.google.com/vertex-ai/docs/generative-ai/prompts/prompt-management),
not in a local file. This gives you:

- **Cloud-native versioning**: each improvement creates a new version
- **Audit trail**: full history of prompt changes with metadata
- **API access**: read/write via `vertexai.Client().prompts`
- **Local mirroring**: each update is also written to `agent/prompts.py`
  so changes are visible in `git diff`

The `VertexPromptAdapter` handles all reads and writes. On startup,
`agent.py` fetches the current prompt from the registry via the
`VERTEX_PROMPT_ID` environment variable. The improvement cycle writes
new versions back through the same adapter and mirrors them to
`agent/prompts.py` via the `PythonFilePromptAdapter`.

To inspect the current prompt from the command line:

```bash
./show_prompt.sh              # Display current prompt text
./show_prompt.sh --versions   # List all versions
```

`setup.sh` creates the initial prompt resource automatically.
`reset.sh` deletes it and creates a fresh one at V1, and restores
`agent/prompts.py` to its original state.

The cycle displays the current prompt at the start and end of each
run so you can see exactly what changed.

### Prompt Optimization: Vertex AI Prompt Optimizer

When the cycle identifies failed sessions, it uses the
**Vertex AI Prompt Optimizer** to generate improved prompts:

1. **Identify failures**: Extract sessions scored as "unhelpful" or
   "partial" from the quality report.
2. **Generate ground truth**: A "teacher agent" (same tools, better
   prompt) re-answers each failed question to produce what the correct
   response should have been. See below for details.
3. **Optimize**: Feed the current prompt + (question, bad_response,
   ground_truth) triples to the Vertex AI Prompt Optimizer in
   `target_response` mode.
4. **Validate**: Test the optimized prompt against the full golden
   eval set before accepting it.

This replaces raw "ask Gemini to rewrite the prompt" with a
structured optimization pipeline backed by real failure data.

### Teacher Agent and Synthetic Ground Truth

The Vertex AI Prompt Optimizer needs examples of correct output to
learn from. But where do those come from? You do not have
hand-written reference answers for every possible user question ...
especially not for questions discovered from synthetic traffic that
you never anticipated.

The solution is the **teacher agent**. It is built from the same
`agent_factory` and runs with the **same tools and the same model**
as the target agent. The only difference is the prompt: instead of
the flawed V1 instruction, the teacher gets a short, direct
instruction that explicitly requires tool usage:

```
You are an expert assistant. For EVERY question, you MUST call
the available tools to look up the answer. NEVER say 'I don't
know' or 'contact HR'. ALWAYS use the tools first, then answer
based on the tool results. Be specific and thorough.
```

The insight: the V1 agent fails not because the tools are broken or
the model is incapable, but because the V1 prompt actively
**discourages** tool use ("answer from the knowledge above"). The
teacher prompt removes that barrier, so the teacher produces correct,
tool-grounded answers to the same questions the target agent failed.

The full flow:

```
Failed sessions from quality report
        |
        v
  Teacher agent re-answers each failed question
  (same tools, same model, better prompt)
        |
        v
  Produces triples:
    (question, bad_response, ground_truth)
        |
        v
  Vertex AI Prompt Optimizer
  (target_response mode)
        |
        v
  Optimized prompt that steers the agent
  toward tool-grounded answers
```

The teacher's answers are saved to `reports/ground_truth_latest.json`
for inspection. Each entry contains the original question, the bad
response from the target agent, and the teacher's ground truth
answer.

**Why not just use the teacher prompt directly?** The teacher prompt
is generic ... it works for any agent with any tools. The optimized
prompt is specific: it learns the domain vocabulary, the tool names,
the response style, and the edge cases from the ground truth
examples. The optimizer produces a prompt that is both correct and
tailored to the agent's actual use case.

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

### Step-by-Step

**Step 1: Generate Synthetic Traffic** -- `generate_traffic.py` calls
Gemini to produce diverse, realistic employee questions, intentionally
different from the golden eval set.

**Step 2: Run Traffic** -- `run_eval.py` sends questions to the agent
using ADK's `InMemoryRunner`. Sessions are logged to BigQuery via the
`BigQueryAgentAnalyticsPlugin`.

**Step 3: Evaluate Quality** -- The SDK's `quality_report.py` reads
sessions from BigQuery and scores each one on response_usefulness
(meaningful/partial/unhelpful) and task_grounding (grounded/ungrounded).

**Step 4: Improve Prompt** -- An ADK LoopAgent wrapping an LlmAgent
with six tools:

```
LoopAgent("prompt_improver", max_iterations=3)
  +-- LlmAgent("prompt_engineer")
        tools: read_quality_report, read_current_prompt,
               generate_candidate, test_candidate,
               write_prompt, exit_loop
```

The `generate_candidate` tool uses the Vertex AI Prompt Optimizer with
synthetic ground truth from a teacher agent. The `test_candidate` tool
runs the full golden eval set. The `write_prompt` tool persists the
validated prompt to the Vertex AI Prompt Registry.

**Step 5: Measure Improvement** -- Fresh synthetic traffic is generated
and scored against the improved prompt via BigQuery.

### Guardrails

- **Golden eval gate**: Candidate prompts must pass ALL golden cases.
  Rejected if any fail, retried up to 3 times.
- **Eval case extraction**: Failed synthetic cases are added to the
  golden set before improvement, raising the bar each cycle.
- **Question deduplication**: Extracted cases are deduplicated by both
  ID and question text.
- **Length check**: Prompts shorter than 50 characters are rejected.
- **Retry with backoff**: Quality report step retries for BigQuery
  write propagation delays.

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
| `roles/aiplatform.user` | Call Gemini models and Vertex AI Prompt APIs |

### 1. Configure environment

Set your GCP project:

```bash
export PROJECT_ID=my-project-id
```

### 2. Run setup

```bash
./setup.sh
```

This installs dependencies (`google-cloud-aiplatform`, `google-adk`,
`google-genai`, etc.), verifies credentials, creates the BigQuery
dataset, and creates the initial V1 prompt in the Vertex AI Prompt
Registry. Improved prompts are mirrored to `agent/prompts.py` for
git tracking.

### 3. Run the demo

```bash
# Single improvement cycle
./run_cycle.sh

# Full demo: 3 cycles, watch the score climb from ~40% to ~100%
./run_cycle.sh --cycles 3

# Eval only (no improvement step)
./run_cycle.sh --eval-only

# Customize traffic volume
./run_cycle.sh --cycles 3 --traffic-count 20

# Use a different agent's config
./run_cycle.sh --agent-config /path/to/other/config.json
```

### 4. Inspect results

After a run, check the `reports/` directory:

```bash
# Quality report JSON (consumed by the improver)
cat reports/quality_report_cycle_1.json | python3 -m json.tool | head -20

# Synthetic traffic that was generated
cat reports/synthetic_traffic_cycle_1.json | python3 -m json.tool | head -20

# See new eval cases extracted from failures
cat eval/eval_cases.json
```

### Reset to V1

To start over, reset everything to the initial state (fresh V1
prompt in Vertex AI, 3 golden eval cases, no reports):

```bash
./reset.sh
```

This deletes the old Vertex AI prompt and creates a fresh one at V1.

## Using with Other Agents

The `agent_improvement` module is reusable. To apply it to a different
agent:

1. Create a `config.json` with your agent's settings
2. Ensure your agent module exports `create_agent(prompt) -> Agent`,
   `AGENT_TOOLS`, `root_agent`, and `bq_logging_plugin`
3. Run: `./run_cycle.sh --agent-config /path/to/your/config.json`

## Configuration

### config.json fields

| Field | Default | Description |
|-------|---------|-------------|
| `app_name` | required | Agent name for BQ filtering |
| `agent_module` | required | Python module path (e.g. `agent.agent`) |
| `prompts_path` | required | Path to prompts.py (for V1 seed text and local mirroring) |
| `prompt_variable` | `CURRENT_PROMPT` | Variable name in prompts.py holding the active prompt |
| `version_variable` | `CURRENT_VERSION` | Variable name in prompts.py holding the version number |
| `eval_cases_path` | required | Path to golden eval set JSON |
| `traffic_generator` | required | Path to traffic generation script |
| `model_id` | `gemini-2.5-flash` | Gemini model for agent and judge |
| `max_attempts` | `3` | Max prompt improvement attempts per cycle |
| `prompt_storage` | `vertex` | `vertex` or `python_file` |
| `vertex_prompt_id` | `""` | Vertex AI prompt ID (auto-filled by setup) |
| `use_vertex_optimizer` | `true` | Use Vertex AI Prompt Optimizer |
| `teacher_model_id` | `null` | Model for teacher agent (null = same as `model_id`) |

### Environment variables (.env)

| Variable | Default | Description |
|----------|---------|-------------|
| `PROJECT_ID` | from `gcloud` | Google Cloud project ID |
| `DATASET_ID` | `agent_logs` | BigQuery dataset for session logs |
| `DATASET_LOCATION` | `us-central1` | BigQuery dataset location |
| `TABLE_ID` | `agent_events` | BigQuery table name |
| `DEMO_MODEL_ID` | `gemini-2.5-flash` | Model for the demo agent |
| `VERTEX_PROMPT_ID` | from setup | Vertex AI prompt resource ID |

### Cost notes

Each improvement cycle makes Gemini API calls for traffic generation,
agent execution, quality evaluation, and prompt optimization. The
Vertex AI Prompt Optimizer also runs the teacher model to generate
ground truth for failed sessions. A typical single cycle uses ~50-80
Gemini calls; a 3-cycle run uses ~200-300.

## Future / Next Steps

- **Sentiment analysis integration**: Extend the quality evaluation to
  detect sentiment dips in agent responses. Use sentiment scores as an
  additional signal for the improvement cycle, identifying not just
  factually wrong answers but also responses that leave users frustrated
  or confused. Feed sentiment-flagged sessions to the optimizer alongside
  usefulness failures.
