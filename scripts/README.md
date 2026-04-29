# Scripts

Standalone scripts for the BigQuery Agent Analytics SDK.

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
  useful answer. Categories: `meaningful`, `unhelpful`, `partial`.

- **task_grounding** - Whether the response is grounded in tool-retrieved data
  or fabricated. Categories: `grounded`, `ungrounded`, `no_tool_needed`.

### A2A Support

The script automatically detects and resolves responses from remote A2A
(Agent-to-Agent) agents by extracting `A2A_INTERACTION` events from traces.


### Sample report output

[Sample report output](sample_report.md)