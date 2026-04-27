<!-- TOC -->
* [Agent Improvement Cycle Demo](#agent-improvement-cycle-demo)
  * [The Demo Agent](#the-demo-agent)
  * [The Problem](#the-problem)
  * [The Solution: Learn from the Field](#the-solution-learn-from-the-field)
  * [Architecture](#architecture)
  * [How the Cycle Works](#how-the-cycle-works)
  * [Quick Start](#quick-start)
    * [Prerequisites](#prerequisites)
    * [1. Configure environment](#1-configure-environment)
    * [2. Run setup](#2-run-setup)
    * [3. Run the demo](#3-run-the-demo)
    * [4. Inspect results](#4-inspect-results)
    * [Reset to V1](#reset-to-v1)
  * [Using with Other Agents](#using-with-other-agents)
  * [Configuration](#configuration)
  * [Future / Next Steps](#future--next-steps)
<!-- TOC -->

# Agent Improvement Cycle Demo

A well-designed agent should learn from its own mistakes. This demo
implements that paradigm: a continuous self-improvement cycle where
the agent's real-world failures become the training data for its next
version. It is powered by the **BigQuery Agent Analytics SDK** and
**[Vertex AI Prompt Registry](https://docs.cloud.google.com/vertex-ai/generative-ai/docs/model-reference/prompt-classes)**. Prompts are stored, versioned, and
optimized in Vertex AI.

For a guided walkthrough, see the [Demo Narration](DEMO_NARRATION.md).

![Demo](demo.png)
## The Demo Agent

The agent used in this demo is a **company policy Q&A assistant**,
built with [Google ADK](https://google.github.io/adk-docs/) and the
[BigQuery Agent Analytics Plugin](https://adk.dev/integrations/bigquery-agent-analytics/).

It's deliberately simple: a single LLM agent with just two tools:

- **`lookup_company_policy(topic)`** — retrieves detailed policy data
  on PTO, sick leave, remote work, expenses, benefits, and holidays.
- **`get_current_date()`** — returns today's date and day of the week,
  so the agent can answer date-relative questions like "Is next Friday
  a holiday?"

The agent's job is to answer employee questions — "How many PTO days
do I get?", "What's the meal reimbursement limit?", "When is the next
company holiday?", and so on.

### V1 Flaws (by design)

The V1 prompt is **intentionally flawed**. It tells the agent to
"answer from the knowledge above" — a short, incomplete summary baked
into the prompt — and to say "I don't know, contact HR" for anything
not listed. The result: the agent ignores its own tools, even though
those tools have all the answers. Users get vague deflections instead
of useful information.

| Flaw | Effect |
|------|--------|
| "Answer from knowledge above" | Agent ignores its tools entirely |
| No expense/holiday info in prompt | Agent says "I don't know" instead of looking it up |
| Vague "competitive benefits" | Agent deflects or hallucinates benefit details |
| No date handling guidance | Agent cannot resolve "next Friday" |

The tools have all the data. The flaw is that the prompt discourages
the agent from using them. By running the self-improvement cycle,
the system detects these failures, generates correct answers using a
teacher agent, optimizes the prompt through the Vertex AI Prompt
Optimizer, and produces a new version that actually uses the tools.
The agent fixes itself.

> **Note: observed model behavior with V1.** The model does not fail
> uniformly across all topics. For topics **mentioned** in V1's inline
> knowledge (PTO, sick leave, remote work), the model often calls
> `lookup_company_policy` anyway — even though the prompt says "answer
> from the knowledge above." The inline mention acts as a signal that
> the topic is valid, which encourages the model to explore available
> tools for more detail. For topics **not mentioned** in the prompt
> (expenses, holidays, parental leave), the explicit fallback
> instruction — "tell the user you do not have that information and
> suggest they contact HR" — overrides tool exploration. The model
> obeys the refusal rule because nothing in the prompt hints that the
> tool could answer the question. This means the V1 failures are
> concentrated on topics absent from the prompt, not on all topics.
> The improvement cycle discovers these gaps through synthetic traffic
> and fixes them by rewriting the prompt to always use tools first.

## The Problem

When you design eval cases for an agent, you are guessing what users
will ask. You cover the happy paths, maybe some edge cases, but you
cannot anticipate every real question. The agent ships, users interact
with it, and some of those interactions fail in ways your tests never
predicted.

## The Solution: Learn from the Field

This demo shows how to close that gap using four components:

1. **[`BigQueryAgentAnalyticsPlugin`](https://adk.dev/integrations/bigquery-agent-analytics/)** captures every real agent session
   (questions, tool calls, responses) into BigQuery automatically.
2. **[`SDK quality_report.py`](../../scripts/quality_report.py)** (the SDK's evaluation script) reads those
   logged sessions back from BigQuery, evaluates quality using an LLM
   judge, and produces structured reports that drive automated
   improvement.
3. **[`SDK CodeEvaluator`](../../bigquery_agent_analytics/evaluators.py)** (the SDK's deterministic evaluator) checks
   operational metrics — latency, token efficiency, and turn count —
   on the same sessions. No LLM calls needed, just math on the data
   already in BigQuery. This ensures the improved prompt doesn't trade
   quality for cost.
4. **[Vertex AI Prompt Registry](https://docs.cloud.google.com/vertex-ai/generative-ai/docs/model-reference/prompt-classes)** stores and versions the agent's prompt
   in the cloud. The **[Vertex AI Prompt Optimizer](https://docs.cloud.google.com/vertex-ai/generative-ai/docs/learn/prompts/prompt-optimizer)** generates improved
   prompts using synthetic ground truth from a teacher model.


![Overview](overview.png)


The full cycle:

1. **GENERATE SYNTHETIC TRAFFIC:** Gemini produces diverse user questions to test the agent beyond anticipated scenarios.
2. **RUN TRAFFIC THROUGH AGENT:** Process traffic through the agent and log every trace/session into BigQuery.
3. **EVALUATE SESSION QUALITY:** SDK scripts read logged sessions; an LLM judge scores them for usefulness and grounding.
4. **IMPROVE PROMPT:** The core optimization stage consists of four critical sub-steps:
    - **Extract:** Failed cases are moved into the golden eval set to raise the performance bar.
    - **Teacher Agent:** Generates ground truth by re-answering failed questions with tool-mandated logic.
    - **Optimize:** Vertex AI Prompt Optimizer generates a new candidate prompt.
    - **Validate (Regression Gate):** The candidate is tested against the full golden eval set.
5. **MEASURE IMPROVEMENT:** Verify the improved prompt against fresh traffic to quantify the quality jump.

At each evaluation step (3 and 5), the SDK's deterministic
`CodeEvaluator` also checks latency, token efficiency, and turn count.
Step 3 establishes the operational baseline; Step 5 shows the
before/after comparison to verify the improved prompt didn't regress
on cost or performance. No extra agent runs — just math on the session
data already in BigQuery.

Run multiple cycles with `--cycles N`. By default, the cycle stops
early once all synthetic traffic scores 100% meaningful. Use
`--no-auto` to force all N cycles to run regardless.

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
  operational_metrics.py # Deterministic metrics gate (latency, tokens, turns)

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

The Vertex AI Prompt Optimizer needs **labeled examples** — pairs of
(input, expected_output) — to learn from. This is the same principle
as supervised learning in ML: you can't improve a model without
showing it what "correct" looks like.

But where do the expected outputs come from? You don't have
hand-written reference answers for every possible user question,
especially not for questions discovered from synthetic traffic that
you never anticipated. Writing golden answers manually doesn't
scale — and the whole point is to handle questions you didn't predict.

The solution is the **teacher agent**. It borrows a concept from
**knowledge distillation** in ML, where a "teacher" model generates
training data for a "student" model. Here the teacher isn't a bigger
model — it's the **same model with the same tools**, just with a
different prompt:

```
You are an expert assistant. For EVERY question, you MUST call
the available tools to look up the answer. NEVER say 'I don't
know' or defer the user elsewhere. ALWAYS use the tools first, then answer
based on the tool results. Be specific and thorough.
```

The teacher's job is narrow: **produce correct, tool-grounded answers
to questions the target agent failed on.** It's not a replacement for
the target agent — it's a data generator. Think of it as an oracle
that knows how to use the tools correctly, but has no domain-specific
personality, formatting, or routing logic.

The key insight: the V1 agent fails not because the tools are broken
or the model is incapable, but because the V1 prompt actively
**discourages** tool use. The teacher prompt removes that barrier.
The teacher calls `lookup_company_policy("expenses")` and gets a
correct answer; the target agent with V1 never tries.

The full flow:

```
Failed sessions from quality report
        |
        v
  Teacher agent re-answers each failed question
  (same tools, same model, tool-first prompt)
        |
        v
  Produces labeled triples:
    (question, bad_response, ground_truth)
        |
        v
  Vertex AI Prompt Optimizer
  (target_response mode — learns from the triples)
        |
        v
  Optimized prompt that steers the target agent
  toward tool-grounded answers
```

The teacher's answers are saved to `reports/ground_truth_latest.json`
for inspection. Each entry contains the original question, the bad
response from the target agent, and the teacher's ground truth
answer.

#### Why not just use the teacher prompt directly?

This is the natural question: if the teacher works, why not deploy it?

The teacher prompt is **generic** — "always use tools, be thorough."
It works for producing correct answers but it lacks everything a
production agent needs:

- **Topic routing:** A complex agent with 10+ tools needs to know
  which tool to call for which question. "Use tools" doesn't tell
  the agent to call `lookup_company_policy("benefits")` when someone
  asks about their 401k.
- **Response style:** The teacher gives verbose, unstructured answers.
  A production prompt defines formatting, tone, and what to include
  or omit.
- **Edge case handling:** The teacher doesn't know about policy
  exceptions, date-relative logic, or when to combine multiple tool
  calls.
- **Domain vocabulary:** The teacher doesn't know that "WFH" means
  remote work, or that "time off" maps to PTO.

The optimizer reads the ground truth examples and produces a prompt
that is both **correct** (uses tools) and **tailored** (knows the
domain mappings, response format, and edge cases). The teacher
generates the training data; the optimizer generates the production
prompt.

In this demo, the agent is simple enough that the distinction is
subtle — the teacher's generic prompt happens to work well for 2
tools and 6 topics. In a real system with complex tool routing,
multi-step workflows, and nuanced response requirements, the gap
between "generic tool-first" and "optimized domain-specific" is
significant.

### Two Eval Sets

This demo uses two distinct sets of questions:

- **Golden eval set** (`eval_cases.json`): The regression gate. These
  cases must always pass. The set starts with 3 cases that V1 handles
  correctly and grows each cycle as failed synthetic cases are
  extracted into it.
- **Synthetic traffic**: Generated fresh each cycle by Gemini. These
  simulate diverse, unpredictable user questions that differ from the
  golden set. They are the source of new failures that drive improvement.

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
The SDK's `CodeEvaluator` also runs deterministic checks on the same
sessions — latency, token efficiency, and turn count — to establish
an operational baseline.

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
and scored against the improved prompt via BigQuery. The deterministic
evaluators then compare V1 and V2 sessions side by side:

| Metric | What it checks | Default budget |
|--------|----------------|----------------|
| `latency` | Average response time per session | 10,000 ms |
| `token_efficiency` | Total tokens consumed per session | 50,000 tokens |
| `turn_count` | Number of conversational turns | 10 turns |

This verifies the improved prompt didn't trade quality for cost — a
prompt that makes the agent chattier or triggers more retries would
show up here even if the quality score is 100%. The data is already in
BigQuery from Steps 2 and 5; no additional agent runs are needed. See
`eval/operational_metrics.py`.

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

# Up to 3 cycles, stops early when 100% meaningful (default behavior)
./run_cycle.sh --cycles 3

# Force all 3 cycles even if 100% is reached early
./run_cycle.sh --cycles 3 --no-auto

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
| `prompt_storage` | `python_file` | `vertex` or `python_file` |
| `vertex_prompt_id` | `""` | Vertex AI prompt ID (auto-filled by setup) |
| `vertex_project` | from `gcloud` | GCP project for Vertex AI (defaults to env) |
| `vertex_location` | `us-central1` | Vertex AI region |
| `use_vertex_optimizer` | `false` | Use Vertex AI Prompt Optimizer |
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

**Golden eval set growth:** The golden eval set grows each cycle as
failed synthetic cases are extracted into it. Each improvement attempt
validates the candidate prompt against the full golden set (N agent
calls + N judge calls per attempt, up to `max_attempts` retries).
After several cycles, the golden set can reach 20+ cases, increasing
both cost and runtime of the validation step. For long-running
deployments, consider periodically pruning redundant golden cases.

## Further Reading

- [Your Agent Events Table Is Also a Test Suite](https://medium.com/google-cloud/your-agent-events-table-is-also-a-test-suite-999fbef885ed) — Using the SDK's `CodeEvaluator` and `categorical-eval` CLI to gate PRs against production traces. Covers the same deterministic evaluators (latency, token efficiency, turn count, error rate) this demo uses in Steps 3 and 5.
- [BigQuery Agent Analytics: From Logs to Graphs](https://medium.com/google-cloud/bigquery-agent-analytics-from-logs-to-graphs-ab0bc34e1418) — Visualizing agent session traces as interactive graphs. Shows how the `BigQueryAgentAnalyticsPlugin` captures the data that powers this improvement cycle.

## Future / Next Steps

- **Sentiment analysis integration**: Extend the quality evaluation to
  detect sentiment dips in agent responses. Use sentiment scores as an
  additional signal for the improvement cycle, identifying not just
  factually wrong answers but also responses that leave users frustrated
  or confused. Feed sentiment-flagged sessions to the optimizer alongside
  usefulness failures.
