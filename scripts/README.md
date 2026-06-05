# Scripts

Standalone scripts for the BigQuery Agent Analytics SDK.

| Script | Description |
|--------|-------------|
| [quality_report](#quality-report) | LLM-as-a-judge evaluation over agent sessions |
| [latency_report](#latency-report-1) | Timing tree and waterfall for agent traces with A2A stitching |

## Quality Report

Runs LLM-as-a-judge evaluation over agent sessions and produces a diagnostic
quality report — not just a pass/fail scorecard. On top of the per-agent
breakdown, unhelpful-session analysis, and category distributions, it scores
**5 quality dimensions**, grades **factual correctness against ground truth**
(golden Q&A), attributes each failure to a **cause** (skill / knowledge / tool),
analyzes **multi-turn corrections**, and renders **execution traces** so you can
see *where* a session went wrong.

Sessions can come from **BigQuery** (the default) or from a **local JSON file**
of conversations (`--conversations-file`, no BigQuery required) — see
[Adding evals](#adding-evals-grounding-the-report-in-ground-truth) for the
recommended workflow.

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
# From the repo root — basics:
./scripts/quality_report.sh                         # evaluate last 100 sessions
./scripts/quality_report.sh --limit 500             # evaluate last 500 sessions
./scripts/quality_report.sh --time-period 7d        # evaluate last 7 days
./scripts/quality_report.sh --report                # also generate markdown report
./scripts/quality_report.sh --no-eval               # browse Q&A only (no evaluation)
./scripts/quality_report.sh --persist               # persist results to BigQuery
./scripts/quality_report.sh --model gemini-2.5-pro  # use a specific model
./scripts/quality_report.sh --env path/to/.env      # load a specific .env file

# Add ground truth — the most important usage (see "Adding evals" below):
./scripts/quality_report.sh --eval-spec eval_spec.json --report  # scope + golden Q&A
./scripts/quality_report.sh --conversations-file traffic.json \
  --eval-spec eval_spec.json --report              # score local cases, no BigQuery
./scripts/quality_report.sh --conversations-file traffic.json --concurrency 20
./scripts/quality_report.sh --golden-threshold 0.85  # looser golden_qa matching
./scripts/quality_report.sh --eval-config my_metrics.json  # custom metric definitions

# Choose how much to score:
./scripts/quality_report.sh --dimensions full       # 8 metrics (default)
./scripts/quality_report.sh --dimensions primary    # 2 primary metrics only (~4x cheaper)
./scripts/quality_report.sh --tag-turns             # classify each user turn (multi-turn)
./scripts/quality_report.sh --trajectory-samples 5  # include N execution traces

# Filter which sessions to evaluate:
./scripts/quality_report.sh --app-name my_agent     # filter to a specific agent app
./scripts/quality_report.sh --label version=v2.1    # filter by custom label
./scripts/quality_report.sh --label version=v2 --label env=prod  # multiple labels (AND)
./scripts/quality_report.sh --session-ids-file ids.json  # evaluate specific sessions
./scripts/quality_report.sh --session <session_id>  # evaluate single session (verbose)

# Control the report:
./scripts/quality_report.sh --samples 20            # show 20 sessions per category
./scripts/quality_report.sh --samples all           # show all sessions per category
./scripts/quality_report.sh --samples unhelpful=10,partial=5,low=3  # per-category caps
./scripts/quality_report.sh --output-json report.json    # write structured JSON output
./scripts/quality_report.sh --threshold 15          # unhelpful rate warning at 15%

# Full ground-truth report with all the trimmings:
./scripts/quality_report.sh --report --limit 50 --app-name my_agent \
  --label version=v2.1 --label env=prod --time-period 7d \
  --tag-turns --trajectory-samples 5 \
  --eval-spec eval_spec.json --output-json results.json
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

When `--session` is used, the console shows **all 8 metrics with full
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

### Adding evals: grounding the report in ground truth

This is the single most important way to use the quality report. Without
ground truth, `response_usefulness` and `task_grounding` are **LLM estimates** —
the judge guesses whether an answer is good. That can mislabel a verbose,
tool-grounded answer as "meaningful" when it is actually wrong, or flag a correct
decline as a failure. Adding evals turns the report into a **trustworthy
regression signal**.

There are two things you "add", and they compose:

1. **An eval spec** (`--eval-spec`) — describes what the agent should do and the
   facts it should know: `scope`, `tools`, `ground_truth`, and `golden_qa`.
   See [Grounding the judge](#grounding-the-judge---eval-spec) below for the
   full schema. Golden Q&A is the highest-value field: each session's question is
   matched to a known question and the **expected answer** is injected into the
   judge, so it grades factual correctness against ground truth instead of
   guessing. The output gains a `golden_eval_summary` — the headline number for
   regression testing.

2. **A set of conversations to score** — either pulled from BigQuery (the
   default) or supplied directly as a **local JSON file** with
   `--conversations-file` (no BigQuery, no GCP credentials). This is what lets you
   score eval cases offline, in CI, or before anything is deployed.

**Recommended workflow:**

```bash
# 1. Create an eval spec for your agent (scope + tools + ground truth + golden Q&A)
cp scripts/eval/data/eval_spec.example.json scripts/eval/data/eval_spec.json
#   edit it — see "Grounding the judge" below

# 2a. Score live sessions from BigQuery against that spec
./scripts/quality_report.sh --eval-spec scripts/eval/data/eval_spec.json --report

# 2b. OR score a local set of conversations offline (no BigQuery)
./scripts/quality_report.sh --conversations-file traffic.json \
  --eval-spec scripts/eval/data/eval_spec.json --report --output-json results.json
```

#### Local conversations (`--conversations-file`)

`--conversations-file PATH` evaluates conversations from a local JSON file using
the Gemini API directly — no BigQuery table and no GCP/BQ credentials required
(you still need `GOOGLE_API_KEY`/Vertex auth for the judge model). The report
format is identical to the BigQuery path, so every flag below
(`--eval-spec`, `--dimensions`, `--tag-turns`, `--report`, `--output-json`, …)
works the same way.

The file is either a list of conversation objects or `{"conversations": [...]}`.
Each conversation is multi-turn (`conversation` array) or single-turn
(`question` + `final_response`):

```json
{
  "conversations": [
    {
      "session_id": "case_001",
      "answered_by": "hr_agent",
      "question": "How many PTO days do I get per year?",
      "final_response": "You get 20 PTO days per year, accrued monthly.",
      "tool_calls": 1
    },
    {
      "session_id": "case_002",
      "answered_by": "hr_agent",
      "conversation": [
        {"role": "user", "text": "How many sick days?"},
        {"role": "agent", "text": "You get 5 sick days."},
        {"role": "user", "text": "I thought it was 10?", "tag": "CORRECTION"},
        {"role": "agent", "text": "You're right — 10 sick days per year."}
      ],
      "tool_calls": 2,
      "corrections": 1
    }
  ]
}
```

Optional per-conversation fields: `session_id` (auto-generated if omitted),
`answered_by`, `tool_calls`, `corrections`, `verifications`, and per-turn `tag`.
When corrections/verifications are not provided for a multi-turn conversation,
they are inferred concurrently (tune parallelism with `--concurrency`, default
`10`). `--limit` caps how many conversations from the file are scored.

#### Failure-cause taxonomy (who fixes it)

When an eval spec is provided, the judge attributes each failure to a **cause**,
so the report tells you *who* should fix it rather than just *that* it failed:

| Cause | Meaning | Fix |
|-------|---------|-----|
| `skill_gap` | Had the tool **and** the data but misbehaved | A skill / prompt fix (evolution) |
| `knowledge_gap` | Used the tool correctly but the fact is missing | Add data to the knowledge source |
| `tool_gap` | No tool/data source, or a personal-data / action request | Build a new tool |

The `tools` field in the eval spec is what lets the judge tell a `knowledge_gap`
(a covered topic with a missing fact) from a `tool_gap` (no data source at all).
The report also detects **routing failures** (a supervisor answered from LLM
knowledge instead of routing to a specialist) and **parroting** (the agent echoed
the user's correction without re-verifying via a tool — penalized as unhelpful so
it can't inflate the score).

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

The evaluation scores each session on **8 metrics** using LLM-as-a-judge:
2 primary, 5 quality dimensions, and `failure_attribution`.

> **Cost:** the default `--dimensions full` makes **8 LLM-judge calls per
> session** (2 primary + 5 quality dimensions + failure_attribution). A
> 100-session run is ~800 calls; a 1000-session bulk eval is ~8000. If you only
> need the pass/fail view, pass `--dimensions primary` to score just the 2
> primary metrics (~2 calls/session, roughly **4x cheaper**) at the cost of the
> Quality Dimensions table. Use `--no-eval` to skip LLM scoring entirely and
> only browse Q&A pairs.

**Primary metrics** classify each session:

| Metric | Categories | What it measures |
|--------|------------|------------------|
| `response_usefulness` | `meaningful`, `declined`, `unhelpful`, `partial` | Whether the response provides a genuinely useful answer |
| `task_grounding` | `grounded`, `ungrounded`, `no_tool_needed` | Whether the response is based on tool-retrieved data or fabricated |

The **`declined`** category is only included when a `scope` is provided in the
eval spec (via `--eval-spec` or auto-discovered `eval/data/eval_spec.json`).
Without scope, the judge has no basis for distinguishing intentional declines
from failures, so only `meaningful`, `unhelpful`, and `partial` are used.

**Quality dimensions** score each session 0-2 and are averaged across all
sessions to produce the Quality Dimensions table in the report:

| Dimension | 2 (best) | 1 (middle) | 0 (worst) |
|-----------|----------|------------|-----------|
| `correctness` | All facts accurate | Minor inaccuracy | Wrong facts or hallucinations |
| `tool_usage` | Tools used properly, **or no tool was needed** | Partial tool use | No tool use when needed |
| `specificity` | Specific numbers, dates, limits | Missing some details | Vague or generic |
| `scope_compliance` | Correctly handled scope | Unnecessary caveats | Wrong scope decision |
| `first_time_right` | Correct on first try | Needed clarification | User had to correct |

`tool_usage` includes a `no_tool_needed` category that also scores 2 — a
greeting, clarification, or a correctly-declined out-of-scope question did not
require a tool, so it is not counted as a Tool Usage failure. In the per-session
scorecard it renders as a neutral `➖` rather than `❌`.

`first_time_right` is primarily a **multi-turn** signal: it measures whether the
agent's first answer held up without the user correcting it. For single-turn
sessions it has no follow-up to look at and effectively mirrors `correctness`,
so read it alongside the multi-turn efficiency stats below.

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

Evaluate a single session and see all 8 metrics with full justifications:

```bash
./scripts/quality_report.sh --session conv_484affd8
```

This is useful for verifying whether the LLM judge scored a specific
session correctly, or for debugging individual conversations. The execution
trace for the session is fetched automatically — no extra flags needed.

### Choosing what to score (`--dimensions`)

Controls how many LLM-judge metrics run per session:

| Value | Metrics | Cost | Use when |
|-------|---------|------|----------|
| `full` (default) | All 8 (2 primary + 5 quality dimensions + failure_attribution) | ~8 calls/session | You want the full diagnostic |
| `primary` | Only `response_usefulness` + `task_grounding` | ~2 calls/session (~4x cheaper) | You only need the pass/fail view |

Use `--no-eval` to skip LLM scoring entirely and just browse Q&A pairs.

### Multi-turn analysis and execution traces

Two flags add deeper diagnostics on top of the scores:

- **`--tag-turns`** runs the full turn tagger on multi-turn conversations,
  classifying each user turn as `CORRECTION`, `VERIFY`, `SPECIFICS`, `SCOPE`,
  `FOLLOWUP`, or `END`. This drives correction-boundary detection and
  sub-trajectory segmentation — for a corrected session the report shows what
  the agent claimed, what the user corrected, and whether it recovered (vs.
  parroted the correction without re-verifying).

- **`--trajectory-samples N`** fetches `N` execution traces from BigQuery and
  renders the full routing tree — per-span tool calls, latency, and TTFT —
  prioritizing unhelpful and correction sessions so the traces shown are the
  ones worth debugging. (With `--session`, the trace is fetched automatically.)

```bash
./scripts/quality_report.sh --report --tag-turns --trajectory-samples 5
```

### Grounding the judge (`--eval-spec`)

For more accurate scoring, provide an **eval spec** — a single JSON file that
grounds the LLM judge. All four fields are optional:

```json
{
  "scope": "Answers HR policy questions: PTO, benefits, expenses, holidays. Does not handle salary, equity, or IT support.",
  "tools": "lookup_company_policy(topic) returns policy text for PTO, sick leave, expenses, benefits, holidays only. No tool can read personal/account data or perform actions.",
  "ground_truth": "PTO: 20 days/year. 401k match: 4%, vested after 1 year.",
  "golden_qa": [
    {"question": "How many PTO days?", "expected_answer": "20/year", "topic": "pto"},
    {"question": "What are the salary bands?", "expected_behavior": "decline", "topic": "out_of_scope"}
  ]
}
```

```bash
./scripts/quality_report.sh --eval-spec eval_spec.json --report
```

The script auto-discovers `eval/data/eval_spec.json` relative to the repo root
or script directory, so `--eval-spec` is only needed to point at a non-default
location. Pass `--eval-spec none` to disable.

**`scope`** — a free-text description of what the agent is designed to handle.
Define scope *positively*; out-of-scope is the complement, so you do **not**
enumerate out-of-scope topics. This lets the judge:
- classify a polite refusal of an out-of-scope question as `declined` (correct)
  rather than `unhelpful` (a bug), and
- score the `scope_compliance` dimension accurately.

**`tools`** — a free-text description of what the agent's tools can and cannot
do. This is what lets the failure-cause taxonomy distinguish a `knowledge_gap`
(a covered topic with a missing fact → add data) from a `tool_gap` (no data
source at all, or a personal-data / action request → build a tool). See
[Failure-cause taxonomy](#failure-cause-taxonomy-who-fixes-it).

**`ground_truth`** — authoritative facts injected into every judge prompt for
correctness checking.

**`golden_qa`** — a list of `{question, expected_answer, topic?,
expected_behavior?}`. Each session's question is matched to the closest golden
question by embedding similarity (cosine ≥ `--golden-threshold`, default 0.92;
lower the threshold to match more aggressively); on a match, the expected answer
is injected into the judge prompt to ground correctness, and the report gains a
`golden_eval_summary` block (matched/unmatched split, `matched_meaningful_rate`,
and the golden-matched questions the agent got wrong — the trustworthy headline
for regression testing). Entries with `expected_behavior: "decline"` (or
`topic: "out_of_scope"`) double as scope-boundary examples. Golden Q&A is
something teams usually already have; it is the most reliable correctness signal.

> **No golden Q&A?** When the spec has no `golden_qa`, the report prints a
> warning that usefulness/grounding are LLM estimates without ground truth (they
> can mislabel verbose, tool-grounded answers) and points you back here.

A sample spec is provided at `scripts/eval/data/eval_spec.example.json`:

```bash
cp scripts/eval/data/eval_spec.example.json scripts/eval/data/eval_spec.json
# Edit with your agent's scope, ground truth, and golden Q&A
```

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
definitions that replace the built-in 8 metrics. Each metric has a `name`,
`definition`, and a list of `categories` with scoring criteria. Metrics with
`scope_aware: true` are automatically enriched with scope context when an
eval spec with a `scope` is provided (`--eval-spec`).

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