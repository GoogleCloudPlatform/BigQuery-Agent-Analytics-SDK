# Recorded run: TheLook analyst week, 2026-10-07

This folder records what one real end-to-end run produced. It is evidence for
the PR, not test input. The unit tests use the synthetic fixture in
[`../fixtures/`](../fixtures/). To reproduce with your own project, follow
[Run it end to end](../README.md#run-it-end-to-end) in the demo README.

This folder is the only place where the real project ID appears.

| | |
|---|---|
| Tables | `test-project-0728-467323.bqaa_agent_memory_demo`: `analyst_events` (written by the plugin), `analyst_memory_extractions`, `analyst_memory_items` and `analyst_task_embeddings` (written by the nightly passes). Dataset location `US`. All four were created for this run; the dataset's `agent_events` table is from an earlier recording of the trip-planner fixture agent and is not part of it |
| Data the agent queried | `bigquery-public-data.thelook_ecommerce`, read-only |
| Model | `gemini-3.8-flash` on Vertex AI, location `global`; extraction with `AI.GENERATE` (`gemini-3.8-flash`) and embeddings with `AI.EMBED` (`text-embedding-005`), both from BigQuery |
| Agent | [`analyst_agent.py`](../analyst_agent.py): google-adk 2.11.0 and `BigQueryAgentAnalyticsPlugin`, with `enable_otel_correlation=True` and `create_views=False` |
| Run tag | `20261007t0834`: 2026-10-07 08:34:04 to 09:34:50 UTC, nightly passes included |
| Scenario | [`analyst_scenario.py`](../analyst_scenario.py): six analysts, five simulated business days (Thursday 2026-10-01 to Wednesday 2026-10-07) |
| Sessions | 36 with memory (40 turns) and 6 without (6 turns). A seventh session without memory, `an-20261007t0834-d3-maya-2-ctl2` (32 rows), is from a re-run that stopped after one session; it is in the table but not in the run record |
| Rows | 1,606 for the record's 42 sessions, all with OpenTelemetry trace and span ids |
| Model usage | Agent: 343 model calls, 2,123,529 input, 83,977 output and 101,938 thinking tokens. Nightly extraction: 40 messages, 24,464 input and 3,827 output tokens. Embeddings: 40 tasks, 3,398 characters |
| Approximate cost | About $2.35 for the run. About $2.76 for the whole day on the project, which adds two smoke runs, an earlier recording, probes and the stopped re-run |

How the cost estimate was built (list prices; the project has no billing
export, so this is not an invoice figure):
- **Gemini.** `gemini-3.8-flash` at $0.75 per million input tokens and $3.75
  per million output tokens, thinking billed as output. The run's agent
  tokens (logged by the plugin in `attributes.usage_metadata`) come to $2.29,
  and the nightly `AI.GENERATE` passes to $0.03.
- **BigQuery.** On demand at $6.25 per TiB billed: the 369 jobs in the run's
  time window billed 4.5 GB, about $0.03 (`INFORMATION_SCHEMA.JOBS_BY_USER`).
- **Embeddings.** `text-embedding-005` read about 2,000 tokens all day;
  negligible.
- **The day.** Cloud Monitoring's
  `aiplatform.googleapis.com/publisher/online_serving/token_count` for the
  project counts 2,423,338 input and 240,884 output tokens of
  `gemini-3.8-flash` on 2026-10-07 ($2.72), and the day's BigQuery jobs
  billed 6.4 GB ($0.04). Logged usage, the stopped re-run included,
  accounts for about 97% of those tokens; the rest is the first smoke run,
  whose scratch table was dropped, and probes.

## Files

| File | What it is |
|---|---|
| `live_run.json` | The run record written by `analyst_agent.py`: days, analysts, every session with its transcript (user turns, tool calls, tool errors, replies, seconds), the before/after pairs, each nightly pass, per-session row counts from a `GROUP BY` over the table, and token usage |
| `demo_output_maya.chen.txt`, `demo_output_lena.okafor.txt` | Output of `agent_memory_demo.py --memory-tables analyst_` for the two sessions the video follows (Maya's Monday question, Lena's Tuesday one). They were read after the run, so they include the whole week; what `recall_memory` returned at the time is in the export (`traces[].recall`) |
| [`../viz/data/memory_export.json`](../viz/data/memory_export.json) | Output of `export_memory.py --memory-tables analyst_ --run-record live_run.json`, with the project shown as `<project>` |
| [`demo.mp4`](demo.mp4) | Narrated walkthrough of the web view on that export: 1 min 41 s, 1280×800 H.264 with an AAC voiceover (macOS `say`, voice Samantha), 16 burned-in captions, 4.4 MB. Recorded with `viz/record_demo.py --narration`, from the export made at 09:55 UTC; see the note below on the later export |
| [`narration.md`](narration.md) | The voiceover script: one section per scene, one bullet per spoken line and caption |
| [`demo.srt`](demo.srt) | The captions, timed to the voiceover, exactly as burned into `demo.mp4` |
| [`../viz/screenshot.png`](../viz/screenshot.png) | Screenshot of the web view, taken with the video |

## What happened

| Analyst | Role | Sessions | Turns | Tool calls | SQL queries (failed) | Recalls | Preference versions | Facts | Entities |
|---|---|---|---|---|---|---|---|---|---|
| Maya Chen | Merchandising lead, women's Jeans and Dresses | 9 | 11 | 51 | 33 | 9 | 4 | 6 | 7 |
| Raj Patel | FP&A analyst | 6 | 7 | 50 | 34 | 7 | 5 | 3 | 5 |
| Lena Okafor | Operations manager, two distribution centers | 7 | 7 | 52 | 34 | 7 | 2 | 3 | 4 |
| Diego Alvarez | Growth marketing lead | 4 | 5 | 39 | 28 | 4 | 2 | 1 | 5 |
| Priya Nair | Customer insights analyst | 5 | 5 | 46 | 30 (2) | 5 | 2 | 4 | 5 |
| Tom Becker | Category manager, men's Outerwear & Coats and Active | 5 | 5 | 23 | 12 | 5 | 4 | 6 | 6 |

Sessions with memory only. The nightly passes extracted 21, 18, 8, 9 and 6
entity mentions and 17, 5, 2, 2 and 0 facts after days 1 to 5; most of what
the analysts said about themselves came on the first day. Of the 26 facts,
3 came back with only a subject and were not stored, which leaves 23.

The questions are scripted; the agent, its SQL, the data and the answers
are real. The days are simulated: every session ran on 2026-10-07 UTC and
carries its simulated date in ADK session state (`sim_date`), which the
agent treats as today and the plugin logs with every row
(`attributes.session_metadata.state`).

## Notes

- **Three runs without memory are not clean controls.** Lena's, Raj's and
  Tom's read the memory tables with `run_sql` (2, 10 and 8 queries); see
  [Memory is data, so scope the tools](../README.md#memory-changes-the-answer).
  The scope check that now stops this (`run_sql` reads only TheLook's
  dataset) was added after the run. The sessions with memory never queried
  outside TheLook; this was checked against their logged SQL.
- **Failed SQL.** Five `run_sql` calls returned `{"status": "error"}`, all
  BigQuery errors in the model's SQL (an ambiguous column, an aggregate in
  `WHERE`, two unknown names, an unknown function). Two were in Priya's
  sessions with memory; the agent fixed the query and went on. Three were in
  the runs without memory. After the run, `run_sql`'s exception handling was
  narrowed to BigQuery API errors and timeouts; it would have returned the
  same five errors.
- **The export was made again after a review fix.** A tool result that
  reports `{"status": "error"}` now fails the trace as well as the tool
  call (see `memory_layers.row_error`). Regenerated from the same rows at
  11:39 UTC, the export changed in four places only. Priya's Day 1 and Day 3
  turns, which each recovered from a failed query, are now answered with
  errors, with the failed `run_sql` bars marked. Raj's and Tom's runs
  without memory are now answered with errors too. The fourth change is the
  export time. No scene of `demo.mp4` shows those traces or labels. The
  header's export time (09:55 UTC in the video and screenshot) is the only
  on-screen difference, so the video was not recorded again.
- **What the agent knew when.** Each session's recall read only what was in
  BigQuery at that moment: preferences right away; facts, entities and
  similar past tasks after the previous night's pass.
