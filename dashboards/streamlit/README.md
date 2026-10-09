# Streamlit Dashboard for BigQuery Agent Analytics

Interactive dashboard for monitoring, diagnosing, and evaluating AI agent traces in Google BigQuery. It has two surfaces: **ADK Agents** (the default, Sections 1–5) and **BQCA Prompt & Response Logging** for BigQuery Conversational Analytics data agents ([Section 6](#6-bqca-prompt--response-logging-dashboard)).

---

## 1. Install

Install dependencies with the `streamlit` extra:

```bash
pip install -e '.[streamlit]'
```

---

## 2. Configure

Copy the sample environment file and configure your parameters:

```bash
cp dashboards/streamlit/.env.sample dashboards/streamlit/.env
```

Parameters configured in `dashboards/streamlit/.env`:

| Variable                         | Description                              | Default                            |
| :------------------------------- | :--------------------------------------- | :--------------------------------- |
| `GOOGLE_APPLICATION_CREDENTIALS` | Path to service account JSON key file    | `dashboards/streamlit/sa-key.json` |
| `BQ_PROJECT_ID`                  | Google Cloud project ID                  | `my-gcp-project`                   |
| `BQ_DATASET_ID`                  | BigQuery dataset containing agent events | `agent_analytics`                  |
| `BQ_TABLE_ID`                    | Raw agent events table                   | `agent_events`                     |
| `BQCA_TABLE_ID`                  | Optional. Events table for the BQCA surface only (Section 6); wins over `BQ_TABLE_ID` there. Not in `.env.sample` | unset (BQCA uses `BQ_TABLE_ID`, else `bqca_prompt_response_logs`) |
| `BQ_VIEW_PREFIX`                 | Prefix for typed analytical views        | `adk_`                             |
| `STREAMLIT_LAZY_TABS`            | Only query data for active tab           | `true`                             |
| `BQAA_PROFILE`                   | Initial surface (`bqca` opens the BQCA surface, Section 6) | unset (ADK Agents)   |

---

## 3. Prerequisites & Typed Views

The dashboard queries typed analytical views (`adk_llm_responses`, `adk_tool_starts`, etc.) created over the raw events table.

### IAM Roles

Ensure the query identity (service account or `gcloud auth application-default login`) has:
* **`roles/bigquery.jobUser`** on the Google Cloud project to run query jobs.
* **`roles/bigquery.dataViewer`** on the dataset to read tables and views.

### Create Views

Generate the typed analytical views matching your project, dataset, and events table:

```bash
bq-agent-sdk views create-all \
  --project-id YOUR_PROJECT \
  --dataset-id YOUR_DATASET \
  --table-id YOUR_TABLE
```

> **Note:** Views default to the `adk_` prefix. If you specify a custom prefix with `--prefix`, set `BQ_VIEW_PREFIX` in `.env` (or in the dashboard sidebar) to match.

---

## 4. Run

Run from the dashboard directory (recommended, automatically applies `.streamlit/config.toml`):

```bash
cd dashboards/streamlit
streamlit run app.py
```

Or run from the repository root with explicit localhost binding:

```bash
streamlit run dashboards/streamlit/app.py --server.address 127.0.0.1
```

### Deployment & Access Control

> **Note:**
> * `.streamlit/config.toml` configures loopback binding (`127.0.0.1`), headless mode, and disables telemetry when running from `dashboards/streamlit/`.
> * When launching from the repository root, pass `--server.address 127.0.0.1` to ensure the dashboard binds only to localhost.
> * If remote access is needed, front it with an authenticating reverse proxy or IAP.
> * Every viewer shares the server's BigQuery identity.
> * The per-query scan cap is configurable in the sidebar.

---

## 5. Sidebar Controls

The sidebar provides runtime controls and guardrails for query execution:

* **Time range selector**: Snaps query execution to discrete sliding windows (Last 1 hour, Last 6 hours, Last 24 hours, Last 3 days, Last 7 days, Last 30 days) with bucketed intervals.
* **Per-query scan cap guardrail (`maximum_bytes_billed`)**: Sets a strict byte limit on every BigQuery job. A free dry-run preflight validates query scan size before execution, preventing queries from running if they exceed the selected cap rather than billing for unexpected costs.
* **Token pricing defaults**: Configures input and output token rates for estimated cost calculations (defaults to \$1.25 / 1M input tokens and \$5.00 / 1M output tokens). Note that costs are derived from token counts rather than recorded billing telemetry.
* **Streaming Buffer Scans**: In scenarios where on-demand compute pricing is used and the queried data reside in BigQuery's streaming buffer, BigQuery does not charge for bytes scanned from the streaming buffer.

---

## 6. BQCA Prompt & Response Logging Dashboard

A second dashboard surface for **BigQuery Conversational Analytics (BQCA)** data agents that have Prompt & Response Logging enabled. It shows how many turns your data agents served and how fast, who asked, what was asked and answered (including the SQL), how many tokens were spent, how many turns received embedding suggestions and why, and where errors come from. The **ADK Agents** surface (Sections 1–5) stays the default and is unchanged.

### Open the BQCA surface

| Method                                                                          | Use it to                                                                          |
| :------------------------------------------------------------------------------ | :--------------------------------------------------------------------------------- |
| Sidebar **Dashboard Surface** → **BQCA Prompt & Response Logging**              | Switch while the app is running                                                    |
| `?profile=bqca` on the app URL, e.g. `http://localhost:8501/?profile=bqca`      | Share or bookmark a link that opens BQCA. The URL parameter wins over `BQAA_PROFILE` |
| `BQAA_PROFILE=bqca` in `.env` or the shell                                      | Make BQCA the surface new sessions start on                                        |

```bash
cd dashboards/streamlit
BQAA_PROFILE=bqca streamlit run app.py
```

The URL and the environment only choose the surface a session *starts* on; after that the sidebar radio decides. Any other profile value opens ADK Agents.

### Connect to your logging table

* The BQCA panels read the **raw logging table** directly. They need neither the typed views from Section 3 nor `BQ_VIEW_PREFIX`, which this surface ignores.
* In the sidebar, fill in **BigQuery source** (Project ID, Dataset ID, **Events table**, Per-query scan cap) and press **Connect**. `BQ_PROJECT_ID` and `BQ_DATASET_ID` pre-fill the form, and Project ID is locked while `BQ_PROJECT_ID` is set.
* **Events table** defaults to `bqca_prompt_response_logs`. The field starts from `BQCA_TABLE_ID` when it is set, else from `BQ_TABLE_ID` (which the ADK surface also reads), else from that default. Typing a name into **Events table** and pressing **Connect** overrides all three. The sample `.env` sets `BQ_TABLE_ID=agent_events`, so with that file and no `BQCA_TABLE_ID`, BQCA opens on `agent_events`: set `BQCA_TABLE_ID` to point only this surface at another table, or type the table name into **Events table** (the [BQCA manual](../../docs/guides/bqca-prompt-response-logging-manual.md) calls the logging table `agent_events`).
* IAM is the same as in Section 3: `roles/bigquery.jobUser` on the project and `roles/bigquery.dataViewer` on the dataset.

### What you see

Nine KPI tiles sit above the tabs: **Total Turns**, **Turn Error Rate**, **P50 Turn Latency**, **P95 Turn Latency**, **Total Tokens**, **Thinking Tokens**, **Cached Tokens**, **Fast-Path Rate** and **Embedding Suggestion Coverage**. A turn is one invocation (`invocation_id`): an event logged with an empty `invocation_id` counts as a turn of its own, identified by its timestamp, while an event with no `invocation_id` at all belongs to no turn, so it is left out of the turn counts, rates and latencies (its tokens still count in the token totals). **Embedding Suggestion Coverage** is the share of all turns that received at least one `EMBEDDING_SUGGESTION` with suggested columns. A rate with nothing to divide by shows `—` rather than a misleading 0%.

A line under the tiles says how many turns completed (reached `INVOCATION_COMPLETED`). A turn that is still running, failed before completing, or was cut off by the time range counts toward **Total Turns**, the error rate and the token totals, but not toward the latency percentiles or **Fast-Path Rate**, which cover completed turns only. A turn counts once in a latency percentile even if its completion was logged more than once, and the **Data Agents & Personas** tab follows the same rule: an agent's fast-path rate and latency cover its completed turns, so an agent with none shows `—` rather than 0%.

| Tab                                  | Panels                                                                                                                                                                      |
| :----------------------------------- | :-------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Overview & Latency**               | Turn volume by path with an error-turn line; turn latency P50 / P95 / P99; fast path vs standard NL2SQL latency; LLM latency and time to first token                         |
| **Data Agents & Personas**           | Turns per data agent; turns per persona                                                                                                                                      |
| **Prompt, Response & SQL Explorer**  | The most recent turns, then a turn picker showing the prompt (plain text), the response (Markdown), the SQL extracted from it, any error message, and the turn's event timeline. A turn that logged several `AGENT_RESPONSE` events shows the last one, the answer that was served, with the SQL found in that same response |
| **Tokens & Embedding Suggestions**   | Token usage over time (uncached input, cached input, output, thinking); tokens by model; embedding suggestions by reason (one per logged `EMBEDDING_SUGGESTION` event); columns suggested per suggestion |
| **Error Attribution**                | Errors by data agent and event type; the most frequent error groups                                                                                                          |

Every chart has a **Table view** expander with the numbers behind it. Fast-path turns answer from a saved query, so they make no LLM call: they have no LLM latency and no tokens.

Where the numbers come from: token counts are read from the `usage_metadata` of each `LLM_RESPONSE` (`prompt_token_count`, `candidates_token_count`, `total_token_count`, `thoughts_token_count`, `cached_content_token_count`), with `prompt_tokens` / `completion_tokens` / `total_tokens` and the `usage` entry of the event content as fallbacks. The model name is `model`, else `model_version`. An embedding suggestion's size is the number of its `suggested_columns`, and its reason is the logged `reason` (`(unspecified)` when none was logged).

### Time range and filters

* **Time range**: Last 1 hour to Last 30 days (default Last 7 days), or **Custom range** with inclusive UTC calendar days. Charts bucket by minute (up to 6 hours), hour (up to 3 days) or day.
* **Filters** take effect when you press **Apply filters**, so every panel re-queries once per apply rather than once per click. An empty selection means all values, and connecting to a different table resets the filters.

| Filter                                                                                    | What it narrows                                                                                                                   |
| :---------------------------------------------------------------------------------------- | :-------------------------------------------------------------------------------------------------------------------------------- |
| **Data agent**, **Persona**, **Fast path**, **Session / conversation**, **Errors only**   | Every KPI and panel except the filter lists themselves and the turn timeline. **Errors only** keeps turns with at least one error event |
| **Event type**                                                                            | Error Attribution and the turn timeline only. Turn-level panels count whole turns, so a single event type there would zero them out |
| **Prompt contains**                                                                       | The Prompt, Response & SQL Explorer only                                                                                           |

The **Data agent** and **Persona** lists offer the values the filters match: each turn's data agent and persona, with *unattributed* included when some turn has none. They ignore every filter, **Errors only** included, so picking one value never hides the others.

### Data governance

* **Nine event types, nothing else.** Every query reads only `INVOCATION_STARTING`, `USER_MESSAGE_RECEIVED`, `AGENT_RESPONSE`, `INVOCATION_COMPLETED`, `LLM_RESPONSE`, `EMBEDDING_SUGGESTION`, `INVOCATION_ERROR`, `AGENT_ERROR` and `LLM_ERROR`. The allowlist is bound as a query parameter on every query, so any other event type in the table is never read or counted.
* **Attribution** comes from the `data-agent-id` entry of the logged session metadata, never from the `agent`, `user_id` or `session_id` columns. Turns without one are grouped as *unattributed*. **Persona** is the `persona` custom label, else the handle of the user (the part before the `@`, only when `user_id` is an email address, optionally followed by `:suffix`), else the data agent. Each of the three is looked up across all the events of the turn, so a label logged by any event wins over a handle logged by an earlier one, and a turn that carries none of them is *unattributed*. An opaque user id such as a service-account name or a number is never turned into a persona.
* **Errors.** An event is an error when its status is `ERROR`, it carries an error message, or its event type ends in `_ERROR`. **Turn Error Rate** is the share of turns with at least one such event.
* **Filter values never reach the SQL text.** Data agent, persona, event type, fast path, session and prompt filters are bound as query parameters. Only the validated table identifier and the generated time bounds are part of the SQL.

### Cost and safety notes

* **Scan cost.** Each panel is one BigQuery query. Results are cached for five minutes, and with `STREAMLIT_LAZY_TABS=true` (the default) only the active tab's panels run. The Prompt, Response & SQL Explorer reads each turn's full prompt and response, so on a long range it scans more than the other tabs. Narrow the time range or lower the **Per-query scan cap**, which sets `maximum_bytes_billed` on every job after a free dry run.
* **Logged text is customer content.** Prompts and responses are shown as plain text or Markdown, and HTML in a logged answer is never rendered. A Markdown image in a logged answer, error message or turn-picker entry is shown as a plain link whose text starts with `image:` and is never fetched, so a logged answer cannot make a viewer's browser call another host. Every viewer still sees every prompt and response the server's BigQuery identity can read, so put the app behind authentication before sharing it (see **Deployment & Access Control** above).
* **Token chart.** Cached tokens are carved out of the input count, so the stacked segments add up to the real token volume. The table view lists the raw `input_tokens` and `cached_tokens` columns.

### Verification against a live logging table

The surface was checked end to end against a live Conversational Analytics logging table in a test project (its project, dataset and table names are left out here). To repeat the check on your own table, connect it as described above and compare what the dashboard shows with what the table holds:

| Check     | What was seen                                                                                                                                                                                                              |
| :-------- | :------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Source    | A 15-column base event schema, read directly from `<project>.<dataset>.<table>`; no typed views were created                                                                                                               |
| Tabs      | All 5 tabs rendered: **Overview & Latency**, **Data Agents & Personas**, **Prompt, Response & SQL Explorer**, **Tokens & Embedding Suggestions** and **Error Attribution**                                                 |
| KPI tiles | All 9 tiles rendered: **Total Turns**, **Turn Error Rate**, **P50 Turn Latency**, **P95 Turn Latency**, **Total Tokens**, **Thinking Tokens**, **Cached Tokens**, **Fast-Path Rate** and **Embedding Suggestion Coverage** |
| Turns     | 289 turns across 1,685 events in the selected range                                                                                                                                                                        |
| Empty IDs | 3 of those 289 turns were logged with an empty `invocation_id`; each is counted as a turn, keyed by its timestamp                                                                                                          |

These figures are a snapshot of one table at one time, not a promise for yours. With no filters applied and the nine event types above in the selected range, **Total Turns** should equal the number of distinct non-empty `invocation_id` values (trimmed of surrounding whitespace), plus one turn for each distinct timestamp among the events logged with an empty `invocation_id`.

### Related docs

* [Looker Studio dashboard](../../dashboard/looker_studio/README.md): the Looker Studio flavor of the same logging table (`hydrate_dashboard.py --profile bqca`, or the `/bqca/` configurator link).
* [Section 7.4 of the BQCA Prompt & Response Logging Manual](../../docs/guides/bqca-prompt-response-logging-manual.md#74-visualizing-logs-with-the-bqca-analytics-dashboards-looker-studio--streamlit): setup commands for both dashboards side by side.
