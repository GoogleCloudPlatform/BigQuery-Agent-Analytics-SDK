# EvalBench MVP end to end: one failed session, import → failed-sessions → score

`examples/evalbench_mvp_e2e.sh` walks the EvalBench import bridge
([#435](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/435),
[#97](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/97))
by following **one real session that failed** through the three CLI
steps. Reference for every command is in
[`docs/evalbench.md`](../docs/evalbench.md).

```bash
bash examples/evalbench_mvp_e2e.sh --fixture   # offline: the story below, recordable
bash examples/evalbench_mvp_e2e.sh --synth     # live: build the job from the real traces, then steps 1-3
bash examples/evalbench_mvp_e2e.sh             # live: steps 1-3 on an existing EvalBench job
```

`--fixture` is the path to record: no BigQuery, no live CLI, exit `0`.

## The session

A terse support agent was asked how many widgets are in stock and never
answered. The session is a real trace from `bqaa_e2e_real.agent_events`
(reference project `test-project-0728-467323`, 7 sessions / 77 events of
`support_agent` handling inventory and ticket requests), folded by
`--synth` into scenario `7e352c34` of EvalBench job `mvp-e2e-real-traces`:

| | |
|---|---|
| agent | `support_agent` — *"You are a terse support agent. Use tools when asked about inventory or tickets. Keep answers to one sentence."* |
| user | `real-user-0` |
| session_id | `7e352c34-4c1c-4395-acd5-fb3c8f215346` |
| scenario_id / eval_id | `7e352c34` |
| prompt | `How many widgets are in stock?` |
| events | `USER_MESSAGE_RECEIVED` → `INVOCATION_STARTING` → `AGENT_STARTING`, then silence |
| final_response / tool_calls / error_message | `null` / `[]` / `null`; `returncode` `1` |

Sibling session `ab7535a5` asked the same question and answered
*"There are 0 widgets in stock."* — so the agent could do it; this session
just never did.

## The story, in six beats

The fixture prints these in order, each under an `=== ... ===` banner a
viewer can read; `tests/test_evalbench_mvp_e2e.py` asserts the same order.

1. **`This agent was asked to check widget stock. Here is the session.`** —
   agent, system prompt, user, `session_id`, `scenario_id`.
2. **`What happened`** — the prompt verbatim, `agent: (no response)`, the
   three events and the silence after `AGENT_STARTING`: no
   `check_inventory` tool call, no `LLM_RESPONSE`, no `AGENT_COMPLETED`.
3. **`Import those traces into EvalBench so we can query this failure`**,
   then `=== Step 1: evalbench-import ===` — `bq-agent-sdk evalbench-import`
   mirrors the job's `results`/`scores`/`configs` into the BQAA-owned
   tables as one `import_version`, records the version in
   `evalbench_import_manifest` (`generation_id`, source fingerprints,
   `view_policy`), and pins the `evalbench_failed_sessions` view to it.
   The result: `status: imported`, 7 scenarios → 27 events + 7 score rows,
   `failed_sessions_view`. We import so the next step can list this
   session.
4. **`This session in failed_sessions`**, then
   `=== Step 2: evalbench-failed-sessions ===` — 1 of 7 sessions failed,
   and this is the row:

   ```
   session_id                                        scenario_id  process_failed  missing_completion  score_failed  failing_scores
   evalbench-import:mvp-e2e-real-traces:v1:7e352c34  7e352c34     True            True                True          [{"comparator": "goal_completion", "score": 0.0}]
   ```

   `process_failed` (returncode 1), `missing_completion` (no
   `AGENT_COMPLETED`), `score_failed` (`goal_completion` 0.0 misses
   `--min-score goal_completion=1`). The `session_id` embeds the version,
   so `Client.get_session_trace` drills into `v1`'s rows only.
5. **`Score this session`**, then `=== Step 3: evalbench-score ===` —
   `Client.evaluate` + `LLMAsJudge` (`correctness`) over the same version,
   narrowed to its 7 pinned session ids; `details.evalbench` names the
   job, version, table, and `pinned_sessions: 7`. For this scenario the
   imported `goal_completion` is 0.0, yet the live judge scored the
   unanswered session `1.0` with `llm_feedback: null` — there was nothing
   to judge. That is why `failed_sessions`, not the judge, is the W0.4
   denominator.
6. **`Punchline`** — one sentence:
   *This widget-stock session failed because the agent never answered;
   goal_completion=0.0.*

Step 3 exits `0` even when sessions fail the judge — the demo shows the
scorecard, it does not gate on it. The CI gate form (step 3 alone with
`--exit-code`) stays in
[`examples/evalbench_score_gate.sh`](evalbench_score_gate.sh).

## Modes

**`--fixture`** (or `EVALBENCH_FIXTURE=1`) prints the six beats above with
sample output shaped like each command's real output. Names default to
`analytics-project.bqaa` (mirror) / `benchmark-project.evalbench` (source)
/ job `mvp-e2e-real-traces` / `import_version` `v1`; `BQ_AGENT_PROJECT`,
`BQ_AGENT_DATASET`, `EVALBENCH_PROJECT`, `EVALBENCH_DATASET`,
`EVALBENCH_JOB_ID` and `EVALBENCH_IMPORT_VERSION` rename what is printed
(including the versioned `session_id`); the protagonist scenario stays
`7e352c34`. If `--fixture` and `--synth` are both given, `--fixture` wins
and nothing is written.

**`--synth`** (or `EVALBENCH_SYNTH=1`) replays the story live when no
EvalBench run exists. It adds a step 0 that runs
[`examples/evalbench_synth_from_traces.py`](evalbench_synth_from_traces.py)
and then runs steps 1–3 on the job it produced:

```
=== Step 0: synthesize EvalBench tables from real traces ===
  # bqaa_e2e_real.agent_events -> <project>.bqaa_evalbench_mvp_demo.{configs,results,scores}
  # one scenario per session; prompts and responses are the real trace text
{ "job_id": "mvp-e2e-real-traces", "source_event_count": 77, "scenarios": 7,
  "completed": 6, "not_completed": 1, "skipped_sessions": [], ... }
```

The recorded run on the reference project produced exactly the story
above: 7 scenarios, 6 completed, 1 (`7e352c34`) stopped after
`AGENT_STARTING`; step 1 imported 27 events + 7 score rows; step 2 listed
that one session; step 3's `correctness` judge scored all 7 sessions 1.0,
the unanswered one with `llm_feedback: null`.

**Live** (no flag) runs steps 1–3 on an EvalBench job you already have.
It needs Application Default Credentials with read access to the
EvalBench dataset and write access to the target dataset; step 3
additionally needs the `AI.GENERATE`-capable connection
`bq-agent-sdk evaluate` uses (`--endpoint` / `--connection-id` defaults
apply). Missing required variables stop the script (exit `1`) before any
command runs; a step's own exit code (`2` for invalid input, an
unpublished job/version, or a BigQuery error — see
[`docs/evalbench.md`](../docs/evalbench.md#cli)) stops it with exit `2`.

## Environment variables

| Variable | Live mode | `--synth` default |
|----------|-----------|-------------------|
| `BQ_AGENT_PROJECT` | Required. Project holding the BQAA mirror tables. | gcloud's current project |
| `BQ_AGENT_DATASET` | Required. BQAA-owned target dataset (must exist; tables and view are auto-created). | `bqaa_evalbench_mvp_mirror` |
| `EVALBENCH_PROJECT` | Required. Project holding the EvalBench dataset. | `= BQ_AGENT_PROJECT` (step 0 builds both datasets in one project; a different value is an error) |
| `EVALBENCH_DATASET` | Required. The EvalBench dataset with `results`, `scores`, `configs`. | `bqaa_evalbench_mvp_demo` (built by step 0) |
| `EVALBENCH_JOB_ID` | Required. The one EvalBench `job_id` to walk. | `mvp-e2e-real-traces` |
| `EVALBENCH_SOURCE_TABLE` | — | `bqaa_e2e_real.agent_events` — the real traces (`dataset.table` or `project.dataset.table`) |
| `EVALBENCH_IMPORT_VERSION` | Optional. Pins one version for all three steps; otherwise step 1 mints one and steps 2–3 use the job's latest successful import. | same |
| `EVALBENCH_MIN_SCORE` | Optional `COMPARATOR=MIN` gate passed as `--min-score` to steps 1 and 2. | `goal_completion=1` (`""` to omit) |
| `EVALBENCH_JUDGE` | Optional. `correctness` (default), `hallucination`, or `sentiment` for step 3. | same |
| `EVALBENCH_PYTHON` | — | `python3` — an interpreter with `google-cloud-bigquery` (e.g. the repo venv's) |

## How step 0 folds a trace into a scenario

The synthesizer reads a real BQAA `agent_events` table (the ADK plugin's
output for an agent that actually ran) and folds **each trace into one
EvalBench scenario**. A trace is the full BQAA identity
`(session_id, user_id, root_agent_name)` — the grouping
`Client.list_traces` uses — so a session id reused across users or root
agents yields one scenario per trace, never one row with user A's prompt
and user B's answer:

| EvalBench column | Taken from the trace's events |
|------------------|--------------------------------|
| `results.id` / `eval_id` | The first eight characters of the `session_id` (`7e352c34`); the full id if that would collide; `session_id:user_id:root_agent_name` if session ids themselves are reused (components percent-escaped, `:` → `%3A`, `%` → `%25`, `~` → `%7E`; a NULL component is `~`, so distinct identities never share an id). |
| `results.prompt` / `nl_prompt` | `USER_MESSAGE_RECEIVED` → `content.text_summary`. Required: a trace without one is **skipped**, never given an invented prompt. |
| `results.final_response` / `stdout` | `AGENT_COMPLETED` text if the plugin logged any, else the last `LLM_RESPONSE` → `content.response` (the ADK plugin logs `AGENT_COMPLETED` without content; `AGENT_RESPONSE` is accepted as an alias). Omitted when the trace never answered — as for `7e352c34`. |
| `results.returncode` | `0` if an `AGENT_COMPLETED` event exists, else `1` (*completed*, not *correct*). |
| `results.run_time` | Timestamp of the `USER_MESSAGE_RECEIVED` event. |
| `results.tool_calls` | `TOOL_STARTING` / `TOOL_COMPLETED` (or `TOOL_ERROR`) pairs, matched by `span_id`, as a JSON list of `{tool_name, args, result, error}`. |
| `results.error_message` | The first `error_message` logged in the trace. |
| `results.source_session_id` / `source_user_id` / `source_root_agent_name` / `source_table` | The trace identity and table the row came from (also on `scores`). |
| `scores` | One row per scenario, `comparator = goal_completion`, `score = 1.0` if the session reached `AGENT_COMPLETED`, else `0.0`. |
| `configs` | `experiment_config.orchestrator` = the traces' agent name, `model_config.generator` = the ADK `app_name`, `bqaa.source_table` = the source table; `run_time` = the earliest prompt. |

Every prompt and response is the real trace text. The only value the
synthesizer *decides* is `goal_completion`, and it says only that the
session finished; whether the answer was right is step 3's job. Rows are
deterministic (no "now" timestamps), so re-running step 0 on unchanged
traces reproduces the importer's source fingerprints and step 1 reports
`status: unchanged` instead of minting a new version. The script creates
both datasets when missing, overwrites the three tables on each run,
refuses to write into the source dataset or the ADK plugin's
`agent_analytics` dataset, and validates every project / dataset / table
name as a plain identifier (`^[A-Za-z0-9_-]+$`, the SDK's own policy)
before creating a BigQuery client or building any SQL. `--dry-run` prints
the rows instead of writing.

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
