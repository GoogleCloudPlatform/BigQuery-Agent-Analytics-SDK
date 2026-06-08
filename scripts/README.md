# Scripts

Standalone scripts for the BigQuery Agent Analytics SDK.

| Script | Description |
|--------|-------------|
| [quality_report](#quality-report) | LLM-as-a-judge evaluation over agent sessions |
| [skill_evolution](#skill-evolution) | Turn a scored quality report into an improved, versioned `SKILL.md` |
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
./scripts/quality_report.sh --session-ids-file ids.json  # evaluate specific sessions
./scripts/quality_report.sh --output-json report.json    # write structured JSON output
./scripts/quality_report.sh --threshold 15          # unhelpful rate warning at 15%
./scripts/quality_report.sh --config config.json    # scope-aware eval with config
```

Or run the Python script directly:

```bash
python scripts/quality_report.py --limit 50 --report
```

### Output

**Console output** includes:
- Per-session details grouped by category (unhelpful, partial, meaningful)
- Per-agent quality table with helpful/unhelpful rates and status indicators
- Unhelpful contribution ranking
- Category distributions
- Execution details (elapsed time, execution mode)

**Markdown report** (`--report` flag) is saved to `scripts/reports/` and includes
all the above in a structured markdown format suitable for sharing or archiving.

**Log files** are saved to `scripts/reports/` for each eval run.

### Filtering

By default, the script evaluates the most recent sessions by time. Two
additional filters are available for targeted evaluation:

- **`--app-name`** filters to sessions from a specific agent. Matches the
  `root_agent_name` attribute set by `BigQueryAgentAnalyticsPlugin`.
- **`--session-ids-file`** evaluates only the sessions listed in a JSON file.
  Accepts either a list of `{"session_id": "..."}` objects (the output of
  `run_eval.py`) or a plain list of ID strings. When session IDs are provided,
  the script filters directly by ID instead of relying on time-based queries,
  which avoids picking up stale sessions from prior runs.

These filters can be combined (e.g. `--app-name my_agent --session-ids-file ids.json`).

### Metrics

The evaluation uses two categorical metrics:

- **response_usefulness** - Whether the agent's response provides a genuinely
  useful answer. Categories: `meaningful`, `declined`, `unhelpful`, `partial`.

- **task_grounding** - Whether the response is grounded in tool-retrieved data
  or fabricated. Categories: `grounded`, `ungrounded`, `no_tool_needed`.

The **`declined`** category is always available — the LLM judge can classify
polite refusals of out-of-scope questions as correct behavior rather than
marking them as `unhelpful`.

### Scope-Aware Evaluation (`--config`)

For more accurate scope evaluation, provide a config file that tells the
LLM judge exactly which topics your agent intentionally does not handle:

```bash
./scripts/quality_report.sh --config agent_context.json --report
```

The script also auto-discovers `eval/data/agent_context.json` relative to
the repo root or script directory, so `--config` is only needed to point
at a non-default location.

Create a JSON config file with `scope_decisions`:

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
      "topic": "promotions",
      "decision": "out_of_scope",
      "reason": "No tool covers career progression"
    }
  ]
}
```

Without a config, the LLM judge can still classify obvious declines as
`declined`, but it won't know which specific topics are out of scope. With
the config, the judge is told exactly which topics are out of scope, so it
can correctly classify polite refusals as `declined` (correct behavior)
rather than `unhelpful` (a bug).

### A2A Support

The script automatically detects and resolves responses from remote A2A
(Agent-to-Agent) agents by extracting `A2A_INTERACTION` events from traces.


### Sample report output

[Sample quality report](sample_quality_report.md)

---

## Skill Evolution

Turns a **scored quality report** (the output of `quality_report.py`) and an
agent's current `SKILL.md` into an improved, versioned `SKILL.md` — with **no
teacher model**. A fleet of parallel, independent LLM "analysts" each reads one
trajectory on a *frozen* copy of the skill and proposes a patch; an **inductive
consolidator** keeps the rules that recur across many analysts and drops the
one-offs. This implements the core of **Trace2Skill** (arXiv:2603.25158 —
parallel analysts + inductive consolidation) and **AutoSkill**
(arXiv:2603.01145 — content-preserving `P_merge`).

The engine is **standalone**: it consumes a report *dict* and returns skill
text, with no agent / traffic / registry dependencies, so it composes with the
scorer without importing it. For a complete, runnable V0 → V1 walkthrough see
[`examples/skill_evolution_lab/`](../examples/skill_evolution_lab/).

### Prerequisites

- Python 3.11+
- Vertex AI access via the `google-genai` client
- `GOOGLE_CLOUD_PROJECT` and `GOOGLE_CLOUD_LOCATION` set (or pass `--project` /
  `--location`), authenticated with ADC (`gcloud auth application-default login`)

### How it works

```text
Quality report (scored conversations)
   │
   ▼
Partition: successes (meaningful/declined) vs failures (unhelpful/partial)
   │   (a "meaningful" session with a PARROTED correction is moved to failures)
   ├── Error analysts   (one per failure):  root cause + "what rule prevents it?"
   └── Success analysts (sampled, max 15):  "what pattern worked? reinforce it?"
   │        (each analyst sees ONE trajectory on a FROZEN copy of the skill)
   ▼
Quality gate (drop weak / category-less patches)
   │
   ▼
Consolidator (prevalence-weighted semantic union) × N candidates (best-of-N)
   │   guardrails reject any candidate that drops a base section or truncates
   ▼
Compaction if over --max-chars
   │
   ▼
Evolved SKILL.md (version bumped, evolved_from recorded)
```

### Usage

```bash
# From the repo root:
python scripts/skill_evolution.py \
  --report report.json \         # scored quality report (quality_report.py --output-json)
  --skill path/to/SKILL.md \     # the current skill (the base to merge into)
  -o path/to/SKILL.v1.md         # where to write the evolved skill

# Useful flags:
python scripts/skill_evolution.py --report r.json --skill SKILL.md -o V1.md \
  --candidates 3 \               # best-of-N consolidation (default 3)
  --max-chars 4000 \             # compact a bloated skill to fit (default: no cap)
  --analyst-mode both \          # both | error-only | success-only (default: both)
  --model gemini-2.5-pro \       # analyst + consolidator model (default)
  --project my-gcp-project --location global
```

The report passed to `--report` must contain `sessions` with
`metrics.response_usefulness.category` and either a `conversation` or
`question`/`response` per session — exactly what `quality_report.py
--output-json` writes. Grounding the score with `--eval-spec` (golden Q&A) and
`--tag-turns` produces the most reliable signal for evolution.

### Python API

```python
import sys
sys.path.insert(0, "scripts")   # make the SDK's scripts/ importable (no copy)
import skill_evolution

new_skill = skill_evolution.evolve_skill(
    report,                 # report dict (or path to its JSON)
    current_skill,          # current SKILL.md contents (str)
    candidates=3,           # best-of-N consolidation
    max_chars=4000,         # optional size cap (compaction)
    analyst_mode="both",    # both | error-only | success-only
    score_fn=my_scorer,     # optional (skill_text) -> float; picks best + gates incumbent
    min_improvement=0.5,    # incumbent gate: ship V1 only if it beats V0 by this margin
)
```

(`examples/skill_evolution_lab/analyze_and_evolve.py` imports it exactly this
way — adding `scripts/` to `sys.path`, then `import skill_evolution`.)

`evolve_skill()` returns the evolved skill as a string (or the unchanged input
when nothing beat the incumbent). Key knobs: `candidates` (best-of-N),
`max_chars` (size cap), `analyst_mode`, `max_success_samples`, `max_workers`
(analyst concurrency), and `score_fn` / `min_improvement` (incumbent guard). The
skill is kept **behavioral** — the consolidator is instructed not to bake
specific data values (numbers, dates, dollar amounts) into the skill; those must
come from tools at runtime.

### Output

The evolved `SKILL.md` keeps the base's YAML frontmatter with the **version
bumped** and `evolved_from` recorded, so each version is a reviewable diff. A
typical run logs its internals — trajectory partition, patches collected vs.
passed the quality gate, candidates generated, and which one was selected.

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