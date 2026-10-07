# Recorded run: live end-to-end, 2026-10-07

This folder records what one real end-to-end run produced. It is evidence for
the PR, not test input. The tests use the synthetic fixture in
[`../fixtures/`](../fixtures/). To reproduce with your own project, follow
[Run it end to end](../README.md#run-it-end-to-end) in the demo README.

This is the only place where the real project ID appears.

| | |
|---|---|
| Table | `test-project-0728-467323.bqaa_agent_memory_demo.agent_events` (dataset location `US`, created for this run) |
| Model | `gemini-3.8-flash` on Vertex AI, location `global` |
| Agent | [`live_agent.py`](../live_agent.py): google-adk 2.11.0 and `BigQueryAgentAnalyticsPlugin`, with `enable_otel_correlation=True` and `create_views=False` |
| Run tag | `20261007t0603`, 2026-10-07 06:03:52 to 06:05:25 UTC |
| Sessions | 5, with 9 user turns: 4 sessions for `demo-ana`, 1 for `demo-ben` |
| Rows | 119 in total. `mem-20261007t0603-s1` has 27, `-s2` 30, `-s3` 31, `-s4` 15 and `-s5` 16. Every row carries `attributes.otel` IDs |
| Model usage | 18 model calls: 23,777 input tokens, 829 output tokens and 5,612 thinking tokens |
| Approximate cost | About $0.04 |

How the cost estimate was built:
- **Vertex AI:** about $0.04. The published introductory rate for gemini-3.8-flash is $0.75 per 1M input tokens and $3.75 per 1M output tokens, and thinking tokens are counted as output.
- **BigQuery:** every query job in this run billed 0 bytes, per `INFORMATION_SCHEMA.JOBS_BY_USER`. That is 17 jobs in total since 06:00 UTC, 15 of them on this dataset: the agent's `recall_memory` reads, the demo, the export, and these checks. Storing 119 rows costs a negligible amount.
- The billing console was not checked.

## Files

| File | What it is |
|---|---|
| `live_run.json` | The run record written by `live_agent.py`: session IDs, per-session row counts from a `GROUP BY` over the table, and the transcript (user turns, tool calls, replies and errors) |
| `demo_output_demo-ana.txt`, `demo_output_demo-ben.txt` | Output of `agent_memory_demo.py --project-id ...` against the table after the run |
| [`../viz/data/memory_export.json`](../viz/data/memory_export.json) | Output of `export_memory.py` for both users, with the project shown as `<project>` |
| [`../demo.mp4`](../demo.mp4), [`../viz/screenshot.png`](../viz/screenshot.png) | Walkthrough video (63 s) and screenshot of the web view on that export, recorded with `viz/record_demo.py` |

## What happened

| Session | User | What the rows show |
|---|---|---|
| `s1` | `demo-ana` | Saved `diet = vegetarian` and `seat = window` (one `STATE_DELTA` row), then searched flights |
| `s2` | `demo-ana` | The hotel search hit the simulated outage, recorded as `TOOL_ERROR`, `AGENT_ERROR`, `NODE_ERROR` and `INVOCATION_ERROR` rows. The next turn retried and answered. The third turn was answered from the conversation alone |
| `s3` | `demo-ana` | `recall_memory` read `s1` and `s2` back from BigQuery (2.1 s) and found `vegetarian`. The agent then updated the diet to `pescatarian` |
| `s4` | `demo-ben` | Saved `diet = vegan` and searched Osaka |
| `s5` | `demo-ana` | `recall_memory` returned `pescatarian`, the current version, and not the other user's `vegan`. The agent then searched Osaka |

The inventories are demo data, and the hotel outage is simulated. The replies
are real model output, so a rerun will word them differently.
