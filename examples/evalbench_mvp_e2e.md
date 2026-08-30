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
bash examples/evalbench_mvp_e2e.sh             # live, against BigQuery
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

## Related

- [`docs/evalbench.md`](../docs/evalbench.md) — data flow, event mapping,
  atomic publish, the failed-session contract (W0.4), judge scoring, CLI.
- [`examples/evalbench_score_gate.sh`](evalbench_score_gate.sh) — CI gate
  on step 3.
- Issues
  [#435](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/435)
  (EvalBench import bridge) and
  [#97](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/97)
  (LLM-judge scoring of EvalBench runs).
