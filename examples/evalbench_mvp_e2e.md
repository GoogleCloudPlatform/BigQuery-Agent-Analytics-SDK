# EvalBench MVP end to end: import → failed-sessions → score

`examples/evalbench_mvp_e2e.sh` walks the EvalBench import bridge
([#435](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/435),
[#97](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/97))
on **one EvalBench job**, in the order a user would run it: mirror the job
into BigQuery Agent Analytics, list the failed sessions of that one
published version, then score that same version with the LLM judge.
Reference for every command is in [`docs/evalbench.md`](../docs/evalbench.md).

```bash
bash examples/evalbench_mvp_e2e.sh --fixture   # offline, recordable
bash examples/evalbench_mvp_e2e.sh --synth     # live, job built from real traces
bash examples/evalbench_mvp_e2e.sh             # live, against an EvalBench job
```

## The three steps

| Step | Command | What it does | What you should see |
|------|---------|--------------|---------------------|
| 1 | `bq-agent-sdk evalbench-import` | Reads the job's `results`/`scores`/`configs` rows from the EvalBench dataset and publishes them atomically as one `import_version` into the BQAA-owned mirror tables (`evalbench_agent_events`, `evalbench_scores_imported`), records the version in `evalbench_import_manifest`, and pins the `evalbench_failed_sessions` view to it. | The `EvalBenchImportResult`: `status` (`imported` / `replaced` / `unchanged`), row counts, the `manifest` row (source fingerprints, `generation_id`, `view_policy`), and `failed_sessions_view`. |
| 2 | `bq-agent-sdk evalbench-failed-sessions` | Resolves exactly one published version from the manifest (the job's latest successful import unless `EVALBENCH_IMPORT_VERSION` pins one) and lists its failed sessions — the W0.4 denominator. | One row per failed session: versioned `session_id` (`evalbench-import:{job_id}:{import_version}:{scenario_id}`), `process_failed`, `missing_completion`, `score_failed`, `failing_scores`. Each `session_id` drills into `Client.get_session_trace` without mixing versions. |
| 3 | `bq-agent-sdk evalbench-score` | Runs `LLMAsJudge` (default `correctness`) through the ordinary `Client.evaluate` path over the mirror table, narrowed to the pinned session ids of that same version. The ADK plugin's `agent_events` table is never read. | The ordinary `EvaluationReport` (`total_sessions`, `pass_rate`, `aggregate_scores`) plus `details.evalbench` naming the scored `job_id`, `import_version`, `events_table`, and `pinned_sessions`. |

Each step prints a banner (`=== Step 1: evalbench-import ===`, and so on)
followed by the command it runs and that command's output, so a recording
and `tests/test_evalbench_mvp_e2e.py` can both follow along.

Step 3 here exits `0` even when sessions fail the judge — the demo shows
the scorecard, it does not gate on it. The CI gate form (step 3 alone with
`--exit-code`) stays in
[`examples/evalbench_score_gate.sh`](evalbench_score_gate.sh).

## Environment variables (live mode)

| Variable | Meaning |
|----------|---------|
| `BQ_AGENT_PROJECT` | Project holding the BQAA mirror tables (import target; steps 2 and 3 read from here). |
| `BQ_AGENT_DATASET` | BQAA-owned target dataset (must exist; the tables and view are auto-created). |
| `EVALBENCH_PROJECT` | Project holding the EvalBench BigQuery dataset (import source). |
| `EVALBENCH_DATASET` | The EvalBench dataset with `results`, `scores`, `configs`. |
| `EVALBENCH_JOB_ID` | The one EvalBench `job_id` to walk. |
| `EVALBENCH_IMPORT_VERSION` | Optional. Pins one version for all three steps; otherwise step 1 mints one and steps 2–3 use the job's latest successful import. |
| `EVALBENCH_JUDGE` | Optional. `correctness` (default), `hallucination`, or `sentiment` for step 3. |
| `EVALBENCH_MIN_SCORE` | Optional. A `COMPARATOR=MIN` gate passed as `--min-score` to steps 1 and 2 (rendered into the failed-session view). `--synth` defaults it to `goal_completion=1`; set it to `""` to omit. |

Missing required variables stop the script (exit `1`) before any command
runs. A step's own exit code (`2` for invalid input, an unpublished
job/version, or a BigQuery error — see the CLI section of
[`docs/evalbench.md`](../docs/evalbench.md#cli)) stops the script with exit `2`.

The intended job family is **gemini-cli-tools**: supply a real
`EVALBENCH_JOB_ID` from an EvalBench run of the gemini-cli tools suite and
the script mirrors, lists, and scores that run. Any EvalBench job whose
`results`/`scores`/`configs` rows carry the `job_id` works the same way.

## `--fixture` vs live

`--fixture` (or `EVALBENCH_FIXTURE=1`) is the offline path: **no BigQuery
call is made and the live CLI is not invoked.** The script prints the same
three banners and, under each, the command it would run and annotated
sample output shaped like that command's real output (an import result with
`manifest` and `failed_sessions_view`, a small failed-sessions table, a
score report with `details.evalbench`), then exits `0`. Sample identities
default to `analytics-project.bqaa` / `benchmark-project.evalbench` / job
`gemini-cli-tools-2026-08-30`; set the environment variables above to
change the names that appear. This is the path to record, and the path
`tests/test_evalbench_mvp_e2e.py` runs.

Live mode requires the environment variables, needs Application Default
Credentials with read access to the EvalBench dataset and write access to
the target dataset, and calls the three CLIs in order. Step 3 additionally
needs the `AI.GENERATE`-capable connection `bq-agent-sdk evaluate` uses
(`--endpoint` / `--connection-id` defaults apply).

## `--synth`: the live demo built from real traces

`--synth` (or `EVALBENCH_SYNTH=1`) is live mode for when **no EvalBench
run exists**. It adds a step 0 that runs
[`examples/evalbench_synth_from_traces.py`](evalbench_synth_from_traces.py)
and then runs steps 1–3 unchanged on the job it produced:

```
=== Step 0: synthesize EvalBench tables from real traces ===
  # bqaa_e2e_real.agent_events -> <project>.bqaa_evalbench_mvp_demo.{configs,results,scores}
  # one scenario per session; prompts and responses are the real trace text
{ "job_id": "mvp-e2e-real-traces", "source_event_count": 77, "scenarios": 7,
  "completed": 6, "not_completed": 1, "skipped_sessions": [], ... }
```

The synthesizer reads a real BQAA `agent_events` table (the ADK plugin's
output for an agent that actually ran) and folds **each session into one
EvalBench scenario**:

| EvalBench column | Taken from the session's events |
|------------------|----------------------------------|
| `results.id` / `eval_id` | The first eight characters of the `session_id` (the full id if that would collide). |
| `results.prompt` / `nl_prompt` | `USER_MESSAGE_RECEIVED` → `content.text_summary`. Required: a session without one is **skipped**, never given an invented prompt. |
| `results.final_response` / `stdout` | `AGENT_COMPLETED` text if the plugin logged any, else the last `AGENT_RESPONSE` → `content.response`. Omitted when the session never answered. |
| `results.returncode` | `0` if an `AGENT_COMPLETED` event exists, else `1` (*completed*, not *correct*). |
| `results.run_time` | Timestamp of the `USER_MESSAGE_RECEIVED` event. |
| `results.tool_calls` | `TOOL_STARTING` / `TOOL_COMPLETED` (or `TOOL_ERROR`) pairs, matched by `span_id`, as a JSON list of `{tool_name, args, result, error}`. |
| `results.error_message` | The first `error_message` logged in the session. |
| `scores` | One row per scenario, `comparator = goal_completion`, `score = 1.0` if the session reached `AGENT_COMPLETED`, else `0.0`. |
| `configs` | `experiment_config.orchestrator` = the traces' agent name, `model_config.generator` = the ADK `app_name`, `bqaa.source_table` = the source table; `run_time` = the earliest prompt. |

Every prompt and response is the real trace text. The only value the
synthesizer *decides* is `goal_completion`, and it says only that the
session finished; whether the answer was right is step 3's job. Rows are
deterministic (no "now" timestamps), so re-running step 0 on unchanged
traces reproduces the importer's source fingerprints and step 1 reports
`status: unchanged` instead of minting a new version. The script creates
both datasets when missing, overwrites the three tables on each run, and
refuses to write into the source dataset or the ADK plugin's
`agent_analytics` dataset. `--dry-run` prints the rows instead of writing.

Every variable has a default so the whole thing works with only `gcloud`
configured (`BQ_AGENT_PROJECT` falls back to `gcloud config get-value
project`):

| Variable | `--synth` default |
|----------|-------------------|
| `BQ_AGENT_PROJECT` | gcloud's current project |
| `EVALBENCH_PROJECT` | `= BQ_AGENT_PROJECT` (step 0 builds both datasets in one project; a different value is an error) |
| `EVALBENCH_SOURCE_TABLE` | `bqaa_e2e_real.agent_events` — the real traces (`dataset.table` or `project.dataset.table`) |
| `EVALBENCH_DATASET` | `bqaa_evalbench_mvp_demo` — the EvalBench-shaped dataset step 0 builds |
| `BQ_AGENT_DATASET` | `bqaa_evalbench_mvp_mirror` — the mirror dataset steps 1–3 use |
| `EVALBENCH_JOB_ID` | `mvp-e2e-real-traces` |
| `EVALBENCH_MIN_SCORE` | `goal_completion=1` |
| `EVALBENCH_PYTHON` | `python3` — an interpreter with `google-cloud-bigquery` (e.g. the repo venv's) |

On the reference project (`test-project-0728-467323`, dataset
`bqaa_e2e_real`, 7 sessions / 77 events of a terse support agent handling
inventory and ticket requests) the recorded run produced 7 scenarios, 6
completed and 1 (`7e352c34`, "How many widgets are in stock?") that stopped
after `AGENT_STARTING`. Step 1 imported them as 27 events and 7 score rows;
step 2 listed exactly that one session with `process_failed`,
`missing_completion`, and `score_failed` (`goal_completion: 0.0`) all true;
step 3's `correctness` judge scored all 7 sessions 1.0 with per-session
feedback — including 1.0 with `llm_feedback: null` for the session that
never answered, which is why the failed-session view of step 2, not the
judge, is the W0.4 denominator.

`--synth` and `--fixture` are exclusive in purpose: `--fixture` prints
sample output without BigQuery; `--synth` calls BigQuery four times. If
both are given, `--fixture` wins (nothing is written).

## Related

- [`docs/evalbench.md`](../docs/evalbench.md) — data flow, event mapping,
  atomic publish, the failed-session contract (W0.4), judge scoring, CLI.
- [`examples/evalbench_synth_from_traces.py`](evalbench_synth_from_traces.py)
  — step 0 of `--synth`: real traces → EvalBench-shaped tables.
- [`examples/evalbench_score_gate.sh`](evalbench_score_gate.sh) — CI gate
  on step 3.
- Issues
  [#435](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/435)
  (EvalBench import bridge) and
  [#97](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/97)
  (LLM-judge scoring of EvalBench runs).
