# EvalBench Import Bridge

`bigquery_agent_analytics.evalbench` reads one EvalBench BigQuery job and
converts its result rows into the BQAA `agent_events` shape. This is a
pull-only bridge: EvalBench remains the source of truth for benchmark results,
and the SDK does not write into the ADK plugin's production `agent_events`
table.

This first implementation phase provides the reader and deterministic row
mapping. Mirror-table materialization, idempotent replacement, score-table
writes, CLI commands, and live integration tests remain later phases of
[issue #97](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/97).

## Data Flow

```text
EvalBench BigQuery dataset                    In-memory BQAA mapping

configs  -- job_id = @job_id --+             agent identity + run time
results  -- job_id = @job_id --+--> EvalBenchRun --> synthetic event rows
scores   -- job_id = @job_id --+         |          USER_MESSAGE_RECEIVED
                                           |          TOOL_* (when present)
                                           |          AGENT_COMPLETED (when present)
                                           +--> source score rows (unchanged)
```

All three source queries filter by the parameterized `job_id` in SQL. The
reader currently loads that single run into memory.

## Read And Map A Run

```python
from bigquery_agent_analytics.evalbench import EvalBenchRun

run = EvalBenchRun.from_bigquery(
    project_id="benchmark-project",
    evalbench_dataset="evalbench",
    job_id="abc123",
    location="US",
)

event_rows = run.to_agent_event_rows()
print(f"Mapped {len(run.results)} scenarios and loaded {len(run.scores)} scores")
```

The reader accepts both major EvalBench result shapes:

| Meaning | NL2SQL fields | Agentic fields |
|---|---|---|
| Scenario | `id` | `eval_id` |
| Prompt | `nl_prompt` | `prompt` or `scenario.starting_prompt` |
| Final output | `generated_sql` | `stdout.response` |
| Tool calls | optional | `stdout.tool_calls` or `accumulated_tools` |

EvalBench's DataFrame writer can store nested objects as JSON or Python-literal
strings. The mapper parses both structured encodings and leaves ordinary text
unchanged.

## Event Mapping

Each scenario uses this identity:

```text
session_id = trace_id = evalbench:{job_id}:{scenario_id}
agent      = evalbench:{orchestrator}:{generator}
```

`orchestrator`, `generator`, and the run timestamp come from EvalBench's
flattened `configs` rows. If a historical run lacks config metadata, the agent
components become `unknown`; if no timestamp is available, the mapper uses the
Unix epoch and sets `attributes.evalbench_run_time_missing = true` rather than
inventing a current timestamp.

| Source data | Synthetic event | Required content |
|---|---|---|
| Prompt | `USER_MESSAGE_RECEIVED` | `text`, `text_summary` |
| Tool call | `TOOL_STARTING` | `tool`, `args`, `text_summary` |
| Tool result | `TOOL_COMPLETED` or `TOOL_ERROR` | `tool`, `result`, `text_summary` |
| Final response or generated SQL | `AGENT_COMPLETED` | `response`, `text_summary` |

Missing tool data emits no `TOOL_*` rows. Missing final output omits
`AGENT_COMPLETED`. Missing `nl_prompt`/`prompt` is a hard error because a valid
scenario trace cannot be built without the user message. Duplicate scenario IDs
are also rejected because they would otherwise produce colliding trace and span
identifiers.

Every row includes `attributes.experiment_id = job_id` and
`attributes.evalbench_scenario_id = scenario_id`. Agentic token and latency
metadata found in `stdout.stats.models` is normalized onto the terminal row as
`usage_metadata`, `input_tokens`, `output_tokens`, and `latency_ms.total_ms`.
Token counts are summed across model entries. Latency uses the maximum reported
model duration because some EvalBench producers repeat one run-level duration
for every model used by the run.

## Why These Fields Matter

The mapping follows the SDK queries that consume the future mirror table:

- `client.py:138-158` projects the complete trace row contract in
  `_GET_TRACE_QUERY`.
- `trace.py:758-768` reads `attributes.experiment_id` for `TraceFilter`.
- `evaluators.py:923-1076` reads latency and token counters in
  `SESSION_SUMMARY_QUERY`.
- `evaluators.py:1079-1102` builds judge text from `content.text_summary`,
  selects `content.response`, and drops traces whose assembled text is not
  longer than ten characters.

The last constraint is easy to miss: a row can satisfy the BigQuery schema but
remain invisible to `LLMAsJudge` when `text_summary` is absent. The mapper
therefore populates it on every emitted event.

## Current Boundaries

- No BigQuery writes are performed by `to_agent_event_rows()`.
- No `evalbench-import` or `evalbench-score` command exists yet.
- `run.scores` preserves source score rows in memory; the
  `evalbench_scores_imported` writer belongs to the materialization phase.
- The future writer must target a BQAA-owned mirror table, accept a distinct
  target project, and delete an existing `experiment_id` before append so a
  repeated import cannot duplicate a run.
