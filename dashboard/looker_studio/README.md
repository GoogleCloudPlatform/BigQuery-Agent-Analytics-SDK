# BigQuery Agent Analytics — Looker Studio Dashboard

A Looker Studio (Data Studio) dashboard with tile-level parity to the Looker
[`agent-analytics-block`](https://github.com/looker-open-source/agent-analytics-block),
built directly on the event table populated by the
[ADK BigQuery Agent Analytics plugin](https://adk.dev/observability/bigquery-agent-analytics/).
For teams that run BQAA but do not run Looker. BigQuery Conversational
Analytics (BQCA) Prompt & Response Logging tables use a dedicated 7-page
tool-free template through the [`bqca` profile](#bqca-prompt--response-logging-profile).

**Just want to use the dashboard?** Read the
[User Manual](USER_MANUAL.md) — prerequisites, three-step setup, page guide,
and troubleshooting, written for dashboard users rather than contributors.
This README is the contributor and operations reference.

The implementation and acceptance contract is tracked in
[issue #365](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/365).
This directory is self-contained and has no dependency on the SDK runtime.

## Design in one paragraph

One embedded Looker Studio `CUSTOM_QUERY`
(`sql/events_v1.template.sql`) scans the configured BQAA `agent_events` table
once and directly applies the ADK generated views' JSON extraction semantics
for LLM tokens, latency, and tool fields. The report's 8 pages (7 parity pages
+ Trace Inspector) are built over that stable typed schema. It creates no
persistent BigQuery objects and does not require `create_views=True`. Users
hydrate the canonical template against their own project, dataset, and table
through the browser configurator or validating helper.

## Compatibility

The standard setup asks for only project ID, dataset ID, and BQAA table ID.
It works when the ADK BigQuery Agent Analytics plugin retains the base-table
columns and types checked by `sql/preflight.sql.tmpl`. Generated views are
optional and are never queried by the dashboard.

ADK 1.27.0, 1.36.1, and 2.4.0 are the frozen candidate profiles. Later
releases with the same structural contract can be used after a successful
preflight, with the documented unverified-semantics warning. A separate
billing project is supported as an optional advanced setting.

## Repository layout

| Path | What it is |
|---|---|
| `spec/chart_manifest.yaml` | Reviewed consumer snapshot: 37 chart records, 9 non-data elements, controls, listener matrix, layout, and oracle mappings |
| `spec/product_contract.yaml` | Current product-layer titles, layout, filters, live fixes, and intentional divergences from the pinned block |
| `spec/compatibility_profile.json` | Machine-readable ADK source contract: one base table, 15 required columns, no generated views |
| `spec/bqca_chart_manifest.yaml` | Consumer snapshot of the dedicated 7-page tool-free BQCA template: 7 pages, 34 components (21 scorecards + 13 charts/tables), responsive 12-column section geometry (`DASHBOARD_LAYOUT_MODE_RESPONSIVE`), and 41-field `BlockDatasource` schema |
| `spec/bqca_product_contract.yaml` | Product contract for the dedicated 7-page tool-free BQCA template (`42e79a1f-a979-4de8-911f-80d5a99543d5`, alias `ds0`) |
| `spec/bqca_dashboard_template.json` | Offline Looker Studio report layout & datasource specification bundle consumed by `tools/validate_contracts.py` and CI contract tests to verify 7-page, 34-component, 12-column responsive-grid, and 41-field `BlockDatasource` parity offline |
| `spec/bqca_compatibility_profile.json` | Machine-readable BQCA source contract: `bqca_prompt_response_logs` base table, 15 required columns, 9 allowlisted BQCA event types, 4 excluded tool/request event types |
| `sql/events_v1.sql.tmpl` | Reviewed base-table query (**generated** by `tools/gen_events_tmpl.py`) |
| `sql/events_v1.template.sql` | Sentinel-rendered SQL embedded in the canonical report (**generated** by `tools/render_template.py`) |
| `sql/preflight.sql.tmpl` / `.template.sql` | Structural compatibility check, run by the hydration helper before emitting a link |
| `sql/bqca_events_v1.sql.tmpl` | BQCA Prompt & Response Logging reporting query (**generated** by `tools/gen_bqca_events_tmpl.py`) |
| `sql/bqca_events_v1.template.sql` | Sentinel-rendered BQCA reporting query for a custom-query data source (**generated** by `tools/render_template.py --profile bqca`) |
| `sql/bqca_preflight.sql.tmpl` / `.template.sql` | Advisory 30-day BQCA data profile, run by the hydration helper after the structural check |
| `bindings/template_bindings.yaml` | Executable sentinel bindings (real fixture identifiers, not placeholders) |
| `bindings/bqca_report_template.yaml` | BQCA profile: dedicated 7-page tool-free template (`42e79a1f-a979-4de8-911f-80d5a99543d5`, alias `ds0`), BQCA report and data-source names, default table, custom query |
| `bindings/bqca_template_bindings.yaml` | BQCA sentinel bindings for the embedded `ds0` custom query, so `sqlReplace` can rebind them |
| `tools/gen_events_tmpl.py` | Base-table reporting-query generator |
| `tools/gen_bqca_events_tmpl.py` | BQCA reporting-query generator (nine-event allowlist, one base-table scan) |
| `tools/render_template.py` | Deterministic tmpl → template renderer with sentinel-uniqueness checks (`--profile adk\|bqca\|all`, `--check`) |
| `tools/render_web_config.py` | Renders `docs/report-config.mjs` from the bindings and derives `docs/bqca/index.html` from `docs/index.html` (`--profile adk\|bqca\|all`, `--check`) |
| `tools/validate_contracts.py` | CI assertions over the ADK and BQCA product contracts, chart manifests, and report template bindings (`--profile adk\|bqca\|all`) |
| `tools/validate_live_bqaa.py` | Read-only 37-query smoke test for a real BQAA dataset; writes only a sanitized local receipt |
| `docs/index.html` | Client-only configurator for the public dashboard template, with an ADK Agents / BQCA Prompt & Response Logging surface toggle |
| `docs/bqca/index.html` | The `/bqca/` deep link: the configurator with the BQCA surface preselected (**generated** by `tools/render_web_config.py`) |
| `tools/hydrate_dashboard.py` | Validates a BQAA table and emits a user-owned Looker Studio report URL (`--profile adk\|bqca`) |
| `docs/dashboard-implementation.md` | Looker Studio page, field, formula, and live-validation implementation contract |
| `docs/issue-377-review.md` | Live validation matrix for the UX/design backlog |
| `docs/rendering-and-viewport-support.md` | Native-chart completion protocol and supported desktop viewport contract |
| `oracle/queries/` | 37 independent per-chart SQL contracts used by the live validator |

## Pinned contracts

- Parity target: block commit `fe6423cc9775b6dc61f7f7047dd4424603ddb3a1`
- Candidate ADK profiles: 1.27.0 (minimum), 1.36.1, 2.4.0 (primary target)
- Certification lifecycle, compatibility classes, and the hydration
  provenance rule: see the governing contract issue.

## Validate a real BQAA installation

The dashboard uses one embedded production query. The 37 per-chart oracle
queries remain independent validation artifacts and are **not** added as 37
extra Looker Studio data sources. To execute every tile contract against a
real, preflight-compatible BQAA dataset without recording any result values:

```sh
cd dashboard/looker_studio
python3 tools/validate_live_bqaa.py \
  --project PROJECT_ID \
  --dataset DATASET_ID \
  --table agent_events \
  --location US \
  --end-date YYYY-MM-DD \
  --output /tmp/live-bqaa-validation.json
```

Add `--billing-project BILLING_PROJECT_ID` when BigQuery jobs run in a
different project.

This proves that all 37 query translations execute on the installation and
records only query hashes, row counts, and job IDs. It does not replace
fixture parity certification or M4 visual sign-off.

## Create your dashboard

Canonical published templates:

- **ADK Agents (8 pages, alias `ds230`)**:
  [BigQuery Agent Analytics — Template](https://lookerstudio.google.com/reporting/5a3f85ef-fc9c-4730-8ef2-8ef9129ddb40)
- **BQCA Prompt & Response Logging (7 pages, tool-free, alias `ds0`)**:
  [BigQuery Conversational Analytics (BQCA) — Prompt & Response Logging](https://lookerstudio.google.com/reporting/42e79a1f-a979-4de8-911f-80d5a99543d5)

All eight report pages share one report-level date control. It defaults to a
rolling 90-day window including today; changing the range on any page persists
across navigation, including the Trace Inspector.

The v1 report is a freeform desktop dashboard. Use a viewport at least 1280
CSS pixels wide (1440 recommended). A cold load or page navigation can paint
native charts after the surrounding report controls; allow up to 90 seconds
for non-degenerate chart output. Phone and narrow-tablet layouts require a
separate responsive template and are not supported by v1. See
[`docs/rendering-and-viewport-support.md`](docs/rendering-and-viewport-support.md).
The 2026-08-11 live pass validated every page at the documented 1280-pixel
minimum with the Looker Studio navigation drawer collapsed. At that width the
expanded drawer is viewer chrome that overlays the report canvas; collapse it
to keep the full left edge visible.
Issue
[#388](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/388)
also found that lower charts on Token Consumption and Latency crossed the
freeform page boundary. The pages were resized in the editor (1030 px and
1100 px) and the report was republished on 2026-07-29; a published-version
probe verified 31 px and 30 px of bottom padding, above the required 24 px
minimum. Reports copied through the Linking API before that date keep their
own snapshot of the old geometry — create a fresh copy from the configurator
to pick up the fix.

For the standard BQAA layout, open the
[dashboard configurator](https://googlecloudplatform.github.io/BigQuery-Agent-Analytics-SDK/)
and enter only the **fully qualified BQAA table ID** —
`project.dataset.table`, one value naming all three identifiers (the
standard table segment is `agent_events`). Paste it straight from the
BigQuery console's copy-table-ID control or paste the console table link;
backticks, a legacy colon after the project, and a trailing `;` or `,` are
cleaned up automatically (#448).

For portable Linking API substitution, table IDs may use ASCII letters, digits,
underscores, and hyphens, such as `events_agent_cur-phenix`. Dataset IDs retain
BigQuery's no-hyphen restriction. Commas and backticks remain rejected because
they would alter the `sqlReplace` binding list or the quoted SQL identifier.

The configurator runs entirely in the browser and creates an official Looker
Studio Linking API URL. Project, dataset, and table identifiers can also be
prefilled in a shareable setup link:

```text
https://googlecloudplatform.github.io/BigQuery-Agent-Analytics-SDK/?project=PROJECT_ID&dataset=DATASET_ID&table=agent_events
```

The standard path queries the provided table and uses the source project for
BigQuery billing. Teams with a separate billing project can set it under
**Advanced settings**.

If the new tab shows the terminal dialog **"This report isn't shared with
you"** (offering *Reload* / *Return to report list* / *Go to report
template*), do not wait it out: `/reporting/create` resolves the *template*
report's ACL before any `ds.*` parameter is applied, so this failure is on the
shared template's copy path, not the user's data, IAM, or identifiers.
Confirm the browser's default Google account is the intended one, and note
that the blockage is not necessarily owner-side: a recipient organization's
Workspace sharing policy can prevent its members from receiving Looker
Studio assets owned by external domains, so a managed account can hit this
dialog against a fully public template — try a personal account if
possible. If the dialog persists, report it on
[#445](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/445),
saying only whether the signed-in account is personal or part of an
organization — never post the account's email address (reporter identifiers
are redacted per *Publication safety* below). The link role alone may not
explain a denial — Looker Studio's "disable download, print, and copy for
viewers" control also prevents copying and is invisible to the Permissions
API, so it remains a candidate cause for this dialog until an end-to-end
reproduction confirms it — which is why external access is attested in
`bindings/report_template.yaml` → `external_access_verification` through two
controls: an authenticated link-role read and a dated end-to-end copy canary
from a signed-in, non-owner, out-of-domain account (anonymous HTTP cannot
observe either). While the template is unavailable, the
`tools/hydrate_dashboard.py` preflight below still validates the table — but
its creation URL copies the same template, so it is not an outage workaround;
the [Grafana dashboard](../../dashboards/grafana/) and the Looker Agent Analytics block
(below) remain available.

Looker Studio report parameters are intentionally not used for these values:
BigQuery query parameters represent scalar query values, not project, dataset,
table, or view identifiers. The Linking API's `sqlReplace` is the supported
connector-level mechanism for rebinding the template's custom query.

For preflight validation or non-standard settings, use the command below.

Prerequisites:

- your Google account can read the BQAA table and can run BigQuery jobs in the
  billing project;
- the `bq` CLI is installed and authenticated.

Run one validation command:

```sh
cd dashboard/looker_studio
python3 tools/hydrate_dashboard.py \
  --project YOUR_PROJECT_ID \
  --dataset YOUR_DATASET_ID \
  --table agent_events \
  --location US
```

The command rejects non-BQAA tables, checks the required base-table columns,
and prints a Looker Studio creation URL. Open that URL, authorize BigQuery,
then:

1. select **Edit and share** to save the configured report to your account;
2. keep the new report private;
3. open **Resource → Manage added data sources → Edit**; and
4. verify **Data credentials: Viewer** before sharing the report.

The Linking API creates a new data source with the clicking user's
credentials, and its creation dialog can label that source **Owner's
Credentials**. Changing the saved report to **Viewer's Credentials** ensures
that every viewer must have their own access to the underlying BigQuery data.
See the official
[data credentials documentation](https://docs.cloud.google.com/data-studio/data-credentials).
The new report uses your billing project and does not grant the template owner
access to your data.

A Google sign-in is required because the template deliberately uses Viewer's
Credentials. This is the canonical template's mode; it does not remove the
credential gate for the new data source created by the Linking API. The
template itself is public, manually published, and backed only by the
committed synthetic sentinel fixture.

The report and sentinel fixture are currently contributor-managed pending a
maintainer-approved transfer to Google-managed ownership. The reviewed
repository query's SHA-256, live connector review, and table-only hydration
smoke test are recorded in `bindings/report_template.yaml`. Those attestations
pin what was verified on that date; they cannot prevent a later owner-side
change to the mutable external report. Until ownership is transferred,
publication review must repeat the live check after every template change.

`--table` is both the object validated by the CLI and the only BigQuery object
queried by the dashboard.

## BQCA Prompt & Response Logging profile

BigQuery Conversational Analytics (BQCA) data agents can log each turn —
prompt, response, generated SQL, model usage, and errors — with Prompt &
Response Logging. The BigQuery Agent Analytics plugin writes those rows, so
the logging table has the BQAA base-table schema and passes the same
structural preflight. The configurator and the hydration helper target it
through a second profile, `bqca`; the default `adk` profile and every ADK
URL are unchanged.

**Configurator.** Choose **BQCA Prompt & Response Logging** in the surface
toggle, open the deep link
[`/bqca/`](https://googlecloudplatform.github.io/BigQuery-Agent-Analytics-SDK/bqca/),
or add `profile=bqca` to a setup link:

```text
https://googlecloudplatform.github.io/BigQuery-Agent-Analytics-SDK/?project=PROJECT_ID&dataset=DATASET_ID&table=bqca_prompt_response_logs&profile=bqca
```

The field placeholder names the BQCA default table,
`bqca_prompt_response_logs`; any table ID that validates works. Switching
surfaces keeps the typed ID and revalidates it. **Copy setup link** always
adds `profile=bqca` on the BQCA surface, and adds `profile=adk` when an ADK
link is copied from `/bqca/`, so a setup link reopens the surface it was
copied from. An ADK link copied from the main page keeps the
three-parameter format.

**CLI.** Pass `--profile bqca`; `--table` then defaults to
`bqca_prompt_response_logs`:

```sh
cd dashboard/looker_studio
python3 tools/hydrate_dashboard.py \
  --profile bqca \
  --project YOUR_PROJECT_ID \
  --dataset YOUR_DATASET_ID \
  --location US \
  --custom-sql-out /tmp/bqca_events.sql
```

After the shared structural preflight passes, the helper runs an advisory
30-day data profile (`sql/bqca_preflight.sql.tmpl`): events per BQCA event
type, data-agent attribution coverage, and fast-path events. The profile
reads table data, so it is capped by `--maximum-bytes-billed` (default
10 GiB); it warns — and never blocks the link — when the scan fails or
exceeds the cap, when the table holds no BQCA events in the last 30 days,
or when no event carries a data-agent ID. `--skip-data-profile` reads no
table data. `--custom-sql-out` writes the BQCA reporting query bound to your
table.

**BQCA uses a dedicated 7-page tool-free template.** The 1-click button
opens your BQCA table in the dedicated
[7-page tool-free BQCA template](https://lookerstudio.google.com/reporting/42e79a1f-a979-4de8-911f-80d5a99543d5)
(`report_id: 42e79a1f-a979-4de8-911f-80d5a99543d5`, data source alias `ds0`)
backed by `sql/bqca_events_v1.template.sql` (`spec/bqca_product_contract.yaml`,
`spec/bqca_chart_manifest.yaml`). The report owner turned on public link
sharing (anyone with the link) on 2026-10-09 (`link_access: PUBLIC` in
`bindings/bqca_report_template.yaml`), but the copy check from a signed-in
outside Google account has not run yet (`external_access_verification.status:
PENDING_EXTERNAL_IDENTITY_CHECK`, tracked on
[#515](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/pull/515)),
so external access is not yet verified. External non-owner Google accounts
that see "This report isn't shared with you" should use the
[Self-Hosted Streamlit BQCA Dashboard](../../dashboards/streamlit/)
(`dashboards/streamlit/`) or `--custom-sql-out`.
Because BQCA never logs tool events, all tool-usage pages, tool-latency series,
and tool-error charts are omitted from the template:

- **Token Consumption** (`p_539b9240`) — total, input, output, thoughts, and
  cached tokens over time and by `data_agent_id`;
- **Data Agents & Turns** (`p_a89cfece`) — active data agents (`data_agent_id`),
  total turns (`invocation_id`), completed turns (`completed_turn_id`), generated
  SQL queries (`extracted_sql`), and volume/latency/errors by `data_agent_id`
  (turns are keyed by `invocation_id` — when an event's `invocation_id` is
  blank (`''`), it first inherits any non-blank `invocation_id` logged on the
  same `trace_id` (`FIRST_VALUE(NULLIF(TRIM(invocation_id), '') IGNORE NULLS)
  OVER (PARTITION BY NULLIF(TRIM(trace_id), '') ...)`), and otherwise falls back
  to `trace_id`, `session_id`, then `timestamp` while preserving `NULL` when
  `invocation_id IS NULL`; multiple blank-ID turns that also have no `trace_id`
  within the same `session_id` therefore group as one session-level turn);
- **LLM Interactions & Embedding Suggestions** (`p_97efe693`) — model calls
  (`llm_latency_ms`), average LLM call latency, average time to first token
  (`ttft_ms`), suggested columns proposed (`similar_queries_count`), and token /
  suggested-column trends by `model_version` and `embedding_suggestion_reason`;
- **User & Persona Analytics** (`p_edf06c14`) — distinct personas (`persona`),
  distinct user IDs (`user_id`), distinct conversations (`conversation_id`), and
  activity by `persona` and `data_agent_id`;
- **Latency & Fast-Path ROI** (`p_08774fec`) — average end-to-end turn latency
  (`turn_latency_ms`), LLM latency (`llm_latency_ms`), event latency
  (`total_latency_ms`), and Fast-Path vs standard NL2SQL comparison
  (`fast_path_label`);
- **Errors (BQCA 3-Condition)** (`p_88bcf5f8`) — error events and **Distinct
  Error Signatures** across all three BQCA error conditions (`is_error`),
  attributed by `data_agent_id`, `event_type`, and `error_message` (synthesizing
  `[EVENT_TYPE: status=STATUS]` when `is_error` is true and `error_message` is
  null/blank);
- **Prompt, Response & SQL Inspector** (`p_b87a335e`) — event-level table of
  `event_date`, `session_id`, `data_agent_id`, `persona`, `event_type`,
  `fast_path_label`, `user_prompt_text`, `extracted_sql`, `summary_text`,
  `error_message`, `total_latency_ms`, and `total_tokens`.

You can also run the
[Self-Hosted Streamlit BQCA Dashboard](../../dashboards/streamlit/)
(`dashboards/streamlit/`) or export `sql/bqca_events_v1.sql.tmpl` bound to your
table with `--custom-sql-out`. The query reads only the nine event types BQCA
logs, in one date-pruned scan of your table, and adds:

| Column | Meaning |
|---|---|
| `data_agent_id`, `conversation_id` | From `attributes.session_metadata.state`, propagated across all events of the turn, never from `agent`, `user_id`, or `session_id` |
| `persona` | `custom_labels.persona`, else the local part of a well-formed email `user_id`, else the data agent, else `unattributed`, propagated across the turn |
| `fast_path`, `fast_path_label` | Whether any event in the turn took the fast path (`LOGICAL_OR` over the turn) |
| `is_error` | Any of: status `ERROR`, a non-null/non-blank `error_message`, or an `_ERROR` event type |
| `is_turn_start`, `is_turn_complete`, `completed_turn_id` | `INVOCATION_STARTING` / deduplicated `INVOCATION_COMPLETED` rows and turn ID; a start without a completion is a failed or abandoned turn |
| `turn_latency_ms`, `llm_latency_ms`, `ttft_ms`, `total_latency_ms` | Turn, model-call, time-to-first-token, and per-row latency |
| `model_name`, `model_version`, `input_tokens`, `output_tokens`, `thoughts_tokens`, `cached_tokens`, `total_tokens` | Model usage on `LLM_RESPONSE` rows |
| `user_prompt_text`, `agent_response_text` | Every text part of a prompt in offset order, and every markdown part of a response in order |
| `extracted_sql`, `summary_text` | The first fenced SQL block of the final `AGENT_RESPONSE` of each turn, and a 2,000-character text summary |
| `similar_queries_count`, `is_embedding_hit`, `embedding_suggestion_reason` | Suggested-column / suggestion count (`$.suggested_columns` first in `COALESCE`, before `$.similar_queries_count` and `$.suggestions`), non-empty indicator, and `$.reason` on `EMBEDDING_SUGGESTION` rows |

Every generated file is deterministic and CI-checked for drift. After
editing a source, regenerate and validate from `dashboard/looker_studio`:

```sh
python3 tools/gen_bqca_events_tmpl.py   # sql/bqca_events_v1.sql.tmpl
python3 tools/render_template.py        # every *.template.sql; --profile adk|bqca|all
python3 tools/render_web_config.py      # docs/report-config.mjs, docs/bqca/index.html; --profile adk|bqca|all
python3 tools/validate_contracts.py     # validate ADK and BQCA contracts & manifests; --profile adk|bqca|all
```

Each accepts `--check` to verify without writing. `docs/bqca/index.html` is
derived from `docs/index.html`, so rerun `render_web_config.py` after any
configurator page edit.

## Already on Looker? Natural-language Q&A today

If your organization already runs Looker (not just Looker Studio), the
conversational experience this dashboard's users ask about exists today
without any of this repository's tooling: install the
[Looker Agent Analytics block](https://marketplace.looker.com/marketplace/detail/agent_analytics)
over the same BigQuery table and use Gemini in Looker to ask questions in
plain language over that telemetry. This dashboard exists for teams
*without* Looker; a Conversational Analytics companion for those teams is
under evaluation in
[`docs/conversational-analytics-decision.md`](docs/conversational-analytics-decision.md)
(issue #402).

## Large-table operating guidance

Every chart reads the same date-pruned custom query over the configured
`agent_events` table. Cost therefore scales primarily with the selected date
window, event volume, and number of chart interactions.

- Keep the BQAA table partitioned on its event timestamp and retain the
  dashboard's `@DS_START_DATE` / `@DS_END_DATE` predicate. Do not wrap the
  partition column in a transformation that prevents pruning.
- Use the shortest date range that answers the operational question. The
  90-day default is an onboarding view, not a recommendation for every
  high-volume installation.
- Set BigQuery partition expiration to the retention period your incident and
  compliance policies actually require.
- Monitor bytes processed in BigQuery job history before and after changing
  the dashboard or retention window.
- For repeatedly queried, high-volume installations, evaluate BI Engine or a
  scheduled, date-partitioned rollup table. Keep the raw table for Inspector
  workflows and validate rollup semantics against the independent oracle
  queries before switching dashboard charts.
- Use a separate billing project when teams need dashboard-specific quotas,
  reservations, or cost attribution.

The report footer's “Data Last Updated” value is a Looker Studio connector
refresh time. It is not the latest BQAA event timestamp and must not be treated
as a data-freshness guarantee.

To run the browser configurator from this checkout:

```sh
python3 -m http.server 8000 --directory dashboard/looker_studio/docs
```

Then open `http://localhost:8000` (or `http://localhost:8000/bqca/` for the
BQCA deep link). The page is entirely client-side.

## Publication safety

Everything committed in this directory is synthetic. Never commit production
project IDs, credentials, service-account keys, trace/user identifiers,
prompts, tool arguments/results, error payloads, or live-validation receipts.
