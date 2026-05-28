# Scripts

Standalone scripts for the BigQuery Agent Analytics SDK.

| Script | Description |
|--------|-------------|
| [quality_report](#quality-report) | LLM-as-a-judge evaluation over agent sessions |
| [latency_report](#latency-report-1) | Timing tree and waterfall for agent traces with A2A stitching |

## Quality Report

Runs LLM-as-a-judge evaluation over agent sessions stored in BigQuery
and produces a quality report with per-agent breakdown, unhelpful session
analysis, and category distributions.

### Prerequisites

- Python 3.11+
- BigQuery Agent Analytics SDK installed (`pip install bigquery-agent-analytics`)
- GCP authentication configured (`gcloud auth application-default login`)
- Agent traces already stored in a BigQuery table

### Environment Variables

Create a `.env` file in the repo root or export these variables:

| Variable | Required | Description |
|----------|----------|-------------|
| `PROJECT_ID` | Yes | GCP project containing the traces table |
| `DATASET_ID` | Yes | BigQuery dataset name |
| `TABLE_ID` | Yes | BigQuery table name (e.g. `agent_events`) |
| `DATASET_LOCATION` | Yes | BigQuery dataset location (e.g. `us-central1`) |
| `EVAL_MODEL_ID` | No | Model for evaluation (default: `gemini-2.5-flash`) |
| `GOOGLE_CLOUD_PROJECT` | No | GCP project for Vertex AI (defaults to `PROJECT_ID`) |
| `GOOGLE_CLOUD_LOCATION` | No | Vertex AI location (default: `global`) |

Example `.env`:

```bash
PROJECT_ID=my-gcp-project
DATASET_ID=agent_logs
TABLE_ID=agent_events
DATASET_LOCATION=us-central1
EVAL_MODEL_ID=gemini-2.5-flash
```

### Usage

```bash
# From the repo root:
./scripts/quality_report.sh                         # evaluate last 100 sessions
./scripts/quality_report.sh --limit 500             # evaluate last 500 sessions
./scripts/quality_report.sh --time-period 7d        # evaluate last 7 days
./scripts/quality_report.sh --report                # also generate markdown report
./scripts/quality_report.sh --no-eval               # browse Q&A only (no evaluation)
./scripts/quality_report.sh --persist               # persist results to BigQuery
./scripts/quality_report.sh --model gemini-2.5-pro  # use a specific model
./scripts/quality_report.sh --samples 20            # show 20 sessions per category
./scripts/quality_report.sh --samples all           # show all sessions per category
./scripts/quality_report.sh --app-name my_agent     # filter to a specific agent app
./scripts/quality_report.sh --label version=v2.1    # filter by custom label
./scripts/quality_report.sh --label version=v2 --label env=prod  # multiple labels (AND)
./scripts/quality_report.sh --session-ids-file ids.json  # evaluate specific sessions
./scripts/quality_report.sh --output-json report.json    # write structured JSON output
./scripts/quality_report.sh --threshold 15          # unhelpful rate warning at 15%
./scripts/quality_report.sh --session <session_id>  # evaluate single session (verbose)

# Scope-aware evaluation (see --agent-context section below)
./scripts/quality_report.sh --agent-context agent_context.json --report

# Full report with all filters
./scripts/quality_report.sh --report --limit 50 --app-name my_agent \
  --label version=v2.1 --label env=prod --time-period 7d \
  --tag-turns --trajectory-samples 5 --agent-context agent_context.json
```

Or run the Python script directly:

```bash
python scripts/quality_report.py --limit 50 --report
```

### Output

**Console output** includes:
- Per-session details grouped by category (unhelpful, partial, meaningful, declined)
- Per-agent quality table with helpful/unhelpful rates and status indicators
- Quality Dimensions summary (0-2 scale with color ratings)
- Multi-turn efficiency metrics (corrections, verifications)
- Unhelpful contribution ranking
- Category distributions
- Execution details — all active filters (`app_name`, `labels`, `time_period`,
  `limit`), plus project, dataset, location, eval model, and elapsed time

When `--session` is used, the console shows **all 7 metrics with full
justifications** for the single session (verbose mode). See
[sample single-session output](sample_quality_report_session.md).

**Markdown report** (`--report` flag) is saved to `scripts/reports/` and includes:
- Summary table and Quality Dimensions scores
- **Dimension drilldowns** — for any dimension rated below 1.50 (needs attention
  or problem area), the report lists the sessions that scored poorly with
  question, response, the judge's justification, and the full conversation
  for multi-turn sessions
- Per-agent breakdown, category distributions
- Unhelpful / Declined / Partial session details with conversations

**Log files** are saved to `scripts/reports/` for each eval run.

### Filtering

By default, the script evaluates the most recent sessions by time. Several
filters are available for targeted evaluation:

- **`--app-name`** filters to sessions from a specific agent. Matches the
  `root_agent_name` attribute set by `BigQueryAgentAnalyticsPlugin`.
- **`--label KEY=VALUE`** filters by custom tags set via
  `BigQueryLoggerConfig.custom_tags`. Repeatable — multiple labels are
  combined with AND logic. Use this to filter by software version, deployment
  environment, experiment ID, or any other custom tag your agent emits.
- **`--session-ids-file`** evaluates only the sessions listed in a JSON file.
  Accepts either a list of `{"session_id": "..."}` objects (the output of
  `run_eval.py`) or a plain list of ID strings. When session IDs are provided,
  the script filters directly by ID instead of relying on time-based queries,
  which avoids picking up stale sessions from prior runs.

These filters can be combined:

```bash
# Evaluate v2.1 sessions from my_agent in the last 7 days
python scripts/quality_report.py --app-name my_agent --label version=v2.1 \
  --time-period 7d --report
```

Active filters are displayed in the **Execution Details** section of both
console and markdown report output, so you can always tell which filters
produced a given report.

### Metrics

The evaluation scores each session on **7 dimensions** using LLM-as-a-judge.

**Primary metrics** classify each session:

| Metric | Categories | What it measures |
|--------|------------|------------------|
| `response_usefulness` | `meaningful`, `declined`, `unhelpful`, `partial` | Whether the response provides a genuinely useful answer |
| `task_grounding` | `grounded`, `ungrounded`, `no_tool_needed` | Whether the response is based on tool-retrieved data or fabricated |

The **`declined`** category is only included when scope context is provided
(via `--agent-context` or auto-discovered `agent_context.json`). Without scope
context, the judge has no basis for distinguishing intentional declines
from failures, so only `meaningful`, `unhelpful`, and `partial` are used.

**Quality dimensions** score each session 0-2 and are averaged across all
sessions to produce the Quality Dimensions table in the report:

| Dimension | 2 (best) | 1 (middle) | 0 (worst) |
|-----------|----------|------------|-----------|
| `correctness` | All facts accurate | Minor inaccuracy | Wrong facts or hallucinations |
| `tool_usage` | Tools used properly | Partial tool use | No tool use when needed |
| `specificity` | Specific numbers, dates, limits | Missing some details | Vague or generic |
| `scope_compliance` | Correctly handled scope | Unnecessary caveats | Wrong scope decision |
| `first_time_right` | Correct on first try | Needed clarification | User had to correct |

**Multi-turn efficiency** metrics are extracted from trace spans:

| Metric | Description |
|--------|-------------|
| Avg user turns | Average number of user messages per session |
| Avg tool calls | Average number of tool calls per session |
| Multi-turn sessions | Sessions with more than one user message |

### Dimension Drilldowns

When the markdown report (`--report`) includes a Quality Dimension rated
below 1.50 (yellow or red), the report automatically adds a drilldown
section listing the sessions that scored poorly on that dimension. Each
entry shows:

- The question and response (last turn for multi-turn sessions)
- The dimension verdict and the judge's justification
- A collapsible conversation block for multi-turn sessions

This makes it easy to go from "Tool Usage is 0.60 — red" to seeing
exactly which sessions had low tool usage and why.

### Single-Session Evaluation (`--session`)

Evaluate a single session and see all 7 metrics with full justifications:

```bash
./scripts/quality_report.sh --session conv_484affd8
```

This is useful for verifying whether the LLM judge scored a specific
session correctly, or for debugging individual conversations.

### Scope-Aware Evaluation (`--agent-context`)

For more accurate scope evaluation, provide a context file that tells the
LLM judge exactly which topics your agent intentionally does not handle.
This is **not** a per-session dictionary — it's a static description of
your agent's scope boundaries that applies to all sessions being evaluated.

```bash
./scripts/quality_report.sh --agent-context agent_context.json --report
```

The script also auto-discovers `eval/data/agent_context.json` relative to
the repo root or script directory, so `--agent-context` is only needed to
point at a non-default location.

**Format:** A JSON file with a `scope_decisions` array. Each entry declares
a topic and whether it is in or out of scope. Only `topic` and `decision`
are used by the judge; `reason` is documentation-only.

```json
{
  "scope_decisions": [
    {
      "topic": "stock_options",
      "decision": "out_of_scope",
      "reason": "No tool or data source covers equity compensation"
    },
    {
      "topic": "salary_bands",
      "decision": "out_of_scope",
      "reason": "Confidential compensation data"
    },
    {
      "topic": "it_support",
      "decision": "out_of_scope",
      "reason": "No tool covers IT support"
    },
    {
      "topic": "pto_policy",
      "decision": "in_scope",
      "reason": "Covered by lookup_company_policy tool"
    }
  ]
}
```

A sample config is provided at `scripts/eval/data/agent_context.example.json`:

```bash
cp scripts/eval/data/agent_context.example.json scripts/eval/data/agent_context.json
# Edit with your agent's scope decisions
```

**Effect on evaluation:** Without scope context, the LLM judge cannot
distinguish an intentional decline ("I can't help with stock options") from
a failure. With the config:
- A polite refusal on an out-of-scope topic is classified as `declined`
  (correct behavior) rather than `unhelpful` (a bug)
- The `scope_compliance` dimension can accurately score whether the agent
  handled scope boundaries correctly

### Custom Labels (`--label`)

Custom labels let you filter quality reports by software version, deployment
environment, experiment ID, or any other tag your agent emits at runtime.

**How it works end-to-end:**

**1. Agent emits labels** — Configure `BigQueryLoggerConfig.custom_tags` when
initializing the ADK plugin. These tags are attached to every event the agent
writes to BigQuery:

```python
from google.adk.plugins.bigquery_agent_analytics_plugin import (
    BigQueryLoggerConfig,
    BigQueryAgentAnalyticsPlugin,
)

bq_config = BigQueryLoggerConfig(
    table_id="agent_events",
    custom_tags={
        "version": "v2.1",
        "env": "prod",
        "experiment_id": "baseline_june",
    },
)

plugin = BigQueryAgentAnalyticsPlugin(
    project_id=PROJECT_ID,
    dataset_id=DATASET_ID,
    config=bq_config,
    location=LOCATION,
)
```

**2. BigQuery stores labels** — The tags are stored in the
`attributes.custom_tags` JSON field of each event row.

**3. Quality report filters by labels** — Use `--label KEY=VALUE` to filter
to sessions that have the matching tag. Multiple labels are combined with AND:

```bash
# Evaluate only v2.1 sessions
./scripts/quality_report.sh --label version=v2.1 --report

# Evaluate v2.1 production sessions from the last 7 days
./scripts/quality_report.sh --label version=v2.1 --label env=prod \
  --time-period 7d --report

# Compare versions: run two reports and diff
./scripts/quality_report.sh --label version=v2.0 --output-json v2.0.json
./scripts/quality_report.sh --label version=v2.1 --output-json v2.1.json
```

Active labels appear in the **Execution Details** section of the output,
so each report is self-documenting about which filters produced it.

### Custom Metrics (`--eval-config`)

Override the built-in metric definitions with your own:

```bash
./scripts/quality_report.sh --eval-config scripts/eval/eval_config.json --report
```

The eval config file is a JSON file with a `metrics` key — a list of metric
definitions that replace the built-in 7 dimensions. Each metric has a `name`,
`definition`, and a list of `categories` with scoring criteria. Metrics with
`scope_aware: true` are automatically enriched with scope context when
`--agent-context` is provided.

A complete example is provided at `scripts/eval/eval_config.json`. Copy it
and customize for your evaluation needs:

```bash
cp scripts/eval/eval_config.json my_eval_config.json
# Edit metric definitions, add/remove dimensions, adjust categories
./scripts/quality_report.sh --eval-config my_eval_config.json
```

When `--eval-config` is not specified, the built-in metrics are used.

### A2A Support

The script automatically detects and resolves responses from remote A2A
(Agent-to-Agent) agents by extracting `A2A_INTERACTION` events from traces.


### Sample output

- [Sample quality report](sample_quality_report.md) — full multi-session report
- [Sample single-session report](sample_quality_report_session.md) — verbose single-session output

---

## Latency Report

Fetches agent traces from BigQuery and renders a hierarchical timing tree
with per-span latency and a waterfall timeline. Automatically stitches
A2A (Agent-to-Agent) remote sessions to show full cross-agent latency
breakdown — including LLM call times inside remote agents that would
otherwise appear as a black box.

### Usage

```bash
./scripts/latency_report.sh                              # latest trace
./scripts/latency_report.sh --limit 5                    # last 5 traces with summary
./scripts/latency_report.sh --time-period 1h             # traces from the last hour
./scripts/latency_report.sh --session <session_id>       # specific session
./scripts/latency_report.sh --app-name my_agent          # filter by root agent name
./scripts/latency_report.sh --verbose                    # show questions and responses
./scripts/latency_report.sh --no-stitch                  # skip A2A session stitching
./scripts/latency_report.sh --env path/to/.env           # use a specific .env file
```

Or run the Python script directly:

```bash
python scripts/latency_report.py --limit 5 --time-period 1h
python scripts/latency_report.py --env path/to/.env --limit 5
```

### Output

The script produces three views for each trace:

1. **Timing tree** — hierarchical span view with latency annotations,
   tool names, and A2A boundary markers
2. **Waterfall chart** — ASCII bar chart showing time distribution
3. **SDK trace tree** — the SDK's built-in `trace.render()` output

When multiple traces are fetched (`--limit > 1`), a **summary table**
shows aggregate latency statistics (avg, P50, P95, min, max) and
per-agent breakdown.

### A2A Session Stitching

When a supervisor agent calls a remote agent via A2A, the parent trace
only records `AGENT_STARTING` and `AGENT_COMPLETED` for the remote
agent — the internal LLM and tool spans are logged in a separate
BigQuery session.

The script automatically:
1. Detects `A2A_INTERACTION` events in the parent trace
2. Extracts the remote session ID from `content.metadata.adk_session_id`
3. Fetches the remote agent's spans and inlines them as children

Use `--no-stitch` to disable this behavior.

### Sample report output

[Sample latency report](sample_latency_report.md)