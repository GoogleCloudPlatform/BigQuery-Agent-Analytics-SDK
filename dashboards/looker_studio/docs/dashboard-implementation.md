# Looker Studio implementation contract

This document connects the generated parity manifest to the canonical report
implementation. It contains no report ID, production project/dataset name, or
result values.

The manifest is an immutable snapshot of the pinned community block. Current
product behavior and intentional divergences are recorded separately in
`../spec/product_contract.yaml`; see `issue-377-review.md` for the live
validation that established the boundary.

## Data-source boundary

The report has exactly one embedded BigQuery data source. Its custom query is
the rendered form of `sql/events_v1.sql.tmpl`; the 37 files under
`oracle/queries/` are independent validation queries and are never added as
extra report data sources.

The public template embeds the executable synthetic sentinel query from
`sql/events_v1.template.sql`. `tools/hydrate_dashboard.py` validates the
caller's BQAA base table, then emits a Linking API URL whose `sqlReplace`
replaces the sentinel project, dataset, and table ID.
The new data source is created with the clicking user's credentials. The
template never exposes or delegates the template owner's BigQuery access.
The Linking API creation review can label that new source as Owner's
Credentials. The generated report must remain private until its owner opens
**Resource → Manage added data sources → Edit** and verifies **Data
credentials: Viewer**. This manual share gate cannot be encoded in a Linking
API parameter.

`docs/index.html` provides the standard-installation path without requiring a
local CLI. It accepts a single **fully qualified BQAA table ID**
(`project.dataset.table` — one input naming all three identifiers, #448),
uses the ID's project segment as the billing project by default with an
optional billing-project override, then constructs the same Linking API URL
entirely in the browser. Setup links keep the existing
`?project=…&dataset=…&table=…` query-parameter contract: all three
parameters compose the field's prefill, but the page never opens the report
without a user click.

The combined field reports two error classes: whole-field errors for input
with no truthful segments to blame (unparseable text, an ambiguous or
unsupported Console link, wrong arity), and segment-level errors — the
project, dataset, or table segment named inline — when exactly three
segments exist and one violates its rule or collides with a template
sentinel. The segment validators are unchanged: dataset segments allow the
established ASCII letter/digit/underscore subset; table segments
additionally allow hyphens, which BigQuery supports and which remain safe
inside the report's backticked table path. Commas and backticks stay invalid
inside a segment because they would change the Linking API replacement list
or SQL identifier boundary — as paste *punctuation* (backticks around the
whole ID, a trailing `;` or `,`) they are stripped before parsing.

Looker Studio report parameters are not the binding mechanism. They can pass
scalar values to BigQuery custom SQL, but BigQuery query parameters cannot
replace identifiers in `FROM` paths. Connector-level Linking API
`sqlReplace` is therefore required to bind the project, dataset, and table
ID. Charts scan that base table once and directly extract the token,
latency, and tool fields exposed by the optional BQAA views. The authenticated
CLI is the only path that preflight-validates the table contract.

The canonical report is shared as Public/Viewer, uses Viewer's Credentials,
and has manual report publishing enabled. Its published title is
`BigQuery Agent Analytics — Template`. A signed-out visitor is sent to Google
sign-in; after sign-in, the Linking API review dialog shows the substituted
custom SQL, the caller's billing project, and the new data source's credential
mode before the caller acknowledges it. The canonical template's Viewer mode
and the generated copy's credential mode are separate settings.

The live report and sentinel fixture are contributor-managed pending transfer
to Google-managed ownership. `bindings/report_template.yaml` records a
SHA-256 and review date for the repository's rendered query. Its deliberately
narrow `repository_artifact_only` scope must not be represented as an
attestation of the mutable live report. Until transfer, a release reviewer
must manually compare the live embedded query with the reviewed artifact.

Token fields accept both BQAA `usage_metadata` shapes observed in supported
installations, in this order:

1. pinned LookML keys: `prompt_token_count`, `candidates_token_count`,
   `total_token_count`;
2. alternate BQAA keys: `prompt_tokens`, `completion_tokens`, `total_tokens`;
3. content-derived token counts (`content.usage`).

Malformed metadata values use `SAFE_CAST` and fall through instead of
aborting every report chart.

## Configurator release checks

Every configurator release and every template republish re-runs the dated
acknowledgement-dialog comparison required by issue #399: a maintainer
opens the real authenticated Linking API flow, confirms the configurator's
step-02 description still matches the acknowledgement dialog Looker Studio
actually shows, and records the date and result on #399. The step-02 copy
must not drift from the live dialog between releases. The stated
provisioning duration in the configurator comes from measured cold-cache
runs recorded on #398; the test suite forces the static note and the
click-time status message to state the same value.

## Pages and chart inventory

The report implements all 37 manifest charts plus one non-parity Trace
Inspector table:

| Page | Manifest charts |
|---|---:|
| Token Consumption | 4 |
| Agent & Sessions | 7 |
| Tool Usage | 3 |
| LLM Interactions | 3 |
| User Analytics | 4 |
| Latency | 12 |
| Errors | 4 |
| **Total parity charts** | **37** |
| Trace Inspector | 1 additional table |

The Inspector exposes timestamp, event type, agent, user, trace, span, and
status from the same production data source. It is a drill-through aid, not a
38th parity tile.

Every page has a visible page heading, and every non-scorecard chart has an
explicit chart title. Product titles use title case, “Over Time,” uppercase
“LLM,” plural “Sessions,” and `(ms)` for latency units. The Tool Usage chart
formerly inherited as “Events By Agent” is labeled **Tool Completions by
Agent** because its metric intentionally counts only `TOOL_COMPLETED` rows.

All eight report pages share one report-level date control with a rolling
90-day window including today. Looker Studio sends those bounds through
`@DS_START_DATE` and `@DS_END_DATE`; the production query applies the frozen
half-open UTC predicate. Changing the range on any page persists across page
navigation, including the Trace Inspector. The generated manifest retains the
pinned LookML's original 14-day Usage and 7-day Performance defaults as
source-provenance metadata; the published template intentionally overrides
them for the product default.

The LLM Call Volume chart uses `event_date`, not raw `timestamp`. The raw
timestamp dimension exceeded Looker Studio's chart row limit on the canonical
fixture and rendered “Too Many Rows.”

* **Time-Series Charts:** Must have "Show points" enabled in the Looker Studio Style panel to ensure single-day data points render visibly.

## Viewer rendering and viewport boundary

The report uses native Looker Studio bar, time-series, scorecard, and table
components. It does not use Vega or third-party community visualizations.
Google-owned `usercontent.goog` render frames do not change that architecture
and must not be used alone to diagnose a community-visualization failure.

Viewer QA waits for non-degenerate chart output rather than treating page
chrome, a loading indicator, or an empty chart container as completion.
Release-candidate validation uses three fresh loads, three complete navigation
loops, and a per-page timeout of 90 seconds. The complete evidence and failure
rules are defined in
[`rendering-and-viewport-support.md`](rendering-and-viewport-support.md).
The dated baseline in that document records a 70-second cold render and
warm-navigation renders within 18 seconds as observations, not performance
guarantees.

Version 1 is a freeform desktop report with a minimum supported viewport width
of 1280 CSS pixels and a recommended width of 1440 CSS pixels. A responsive
mobile report is a separate template: the current multi-component pages cannot
be converted in place without rebuilding their section layout and repeating
parity, hydration, credential, and visual acceptance.
The 2026-08-11 live pass validated all eight pages at the documented
1280-pixel minimum with the Looker Studio navigation drawer collapsed. At that
width the expanded drawer is viewer chrome that overlays the report canvas;
collapse it to keep the full left edge visible.

Freeform layout acceptance includes vertical containment as well as
non-overlap. For every page, each component must satisfy
`top + height <= page height - 24 px`. Issue #388 found that the lower charts
on Token Consumption and Latency violated this rule; those pages were resized
(1030 px and 1100 px) and the report was republished on 2026-07-29, with a
published-version probe verifying 31 px and 30 px of bottom padding.
`spec/product_contract.yaml#layout.page_bounds` records the verified
page-local measurements. The report remains mutable, so any later geometry
edit requires a fresh published-version probe before the contract may keep
claiming containment.

## Looker Studio measure mappings

The stable source fields map to report measures as follows:

| Manifest measure | Looker Studio implementation |
|---|---|
| total events | `Record Count` |
| total invocations | `COUNT_DISTINCT(invocation_id)` |
| total traces | `COUNT_DISTINCT(trace_id)` |
| total sessions | `COUNT_DISTINCT(session_id)` |
| total users | `COUNT_DISTINCT(user_id)` |
| total tokens | `SUM(usage_total_tokens)` |
| LLM calls | `COUNT_DISTINCT(llm_response_pk)` |
| tool calls | `COUNT_DISTINCT(tool_completed_pk)` |
| tool errors | `COUNT_DISTINCT(tool_error_pk)` |
| average LLM latency | `AVG(llm_total_ms)` |
| average tool latency | `AVG(tool_completed_total_ms)` |
| LLM percentile Pn | `PERCENTILE(llm_total_ms, n)` |
| tool percentile Pn | `PERCENTILE(tool_completed_total_ms, n)` |

Dimensions use the stable fields directly. Tool-only and error-only charts
use the typed `tool_completed_*` and `tool_error_*` fields so null rows from
other event types do not contribute to the measure.

## Real-data smoke validation

On 2026-07-24, the base-table-only implementation:

- passed the table/column preflight against a physical table clone;
- dry-ran the exact production custom query successfully;
- returned all 100,004 fixture rows, including 263 event rows omitted by the
  historical generated-view intersection;
- matched the generated views exactly for LLM token, LLM-row, tool-row,
  tool-latency, error-row, and error-latency aggregates; and
- executed all 37 independent base-table oracle queries successfully.

No live result rows or source identifiers are committed. This smoke result
proves installation compatibility and query executability; it does not replace
seeded parity certification or M4 visual sign-off.

The 2026-08-11 90-day publication gate repeated the table-only hydration and
exact-production-query checks against a sanctioned real BQAA test table. With
`@DS_START_DATE=20260514` and `@DS_END_DATE=20260811`, the uncached query
processed 60,248 bytes and billed 10,485,760 bytes. A subsequent authenticated
**Refresh data** walk of all eight published pages produced five query-cache
hits and billed zero additional bytes. The absolute 90-day measurement is
environment-specific; it does not imply a linear cost reduction from the
former 365-day default. The same exact-query check verified that events dated
2026-08-11 were included, preserving the include-today contract.

## Dedicated 7-page BQCA Prompt & Response Logging template

BigQuery Conversational Analytics (BQCA) Prompt & Response Logging uses a
separate, dedicated 7-page tool-free Looker Studio report template
(`42e79a1f-a979-4de8-911f-80d5a99543d5`, data source alias `ds0`, embedded
`BlockDatasource` ID `70e5ac1e-65bb-4c89-a9df-fa9990cb6029`) rather than the
8-page ADK template (`5a3f85ef-fc9c-4730-8ef2-8ef9129ddb40`). The report is a
copy that its owner made on 2026-10-09 of the first published BQCA template,
whose organization allowed sharing only inside the organization. The copy's
`BlockDatasource` ID and its `ds0` alias were re-recorded on 2026-10-29 from
the copy's report configuration; the live verification evidence was recorded
on the source report and has not been re-recorded for the copy. The owner
turned on public link sharing for the copy on 2026-10-09; the copy check from
a signed-in outside Google account is still pending.

### Contract and manifest artifacts

- **Product contract**: `spec/bqca_product_contract.yaml` defines the surface
  metadata, the nine allowed BQCA event types (`INVOCATION_STARTING`,
  `USER_MESSAGE_RECEIVED`, `AGENT_RESPONSE`, `INVOCATION_COMPLETED`,
  `LLM_RESPONSE`, `EMBEDDING_SUGGESTION`, `INVOCATION_ERROR`, `AGENT_ERROR`,
  `LLM_ERROR`), the 7 report pages, and all 34 components (21 scorecards and
  13 charts/tables).
- **Consumer chart manifest**: `spec/bqca_chart_manifest.yaml` is the BQCA
  counterpart to `spec/chart_manifest.yaml`, recording every component's
  Looker Studio `component_id` (`cd-...`), `given_id`, page assignment,
  responsive 12-column section geometry (`DASHBOARD_LAYOUT_MODE_RESPONSIVE`
  with explicit `top`, `left`, `width`, `height` coordinates inside
  12-column page sections), dimensions, metrics, and the 41-field remediated
  `BlockDatasource` schema.
- **Report template binding**: `bindings/bqca_report_template.yaml` and
  `bindings/bqca_template_bindings.yaml` pin the report ID, `ds0` alias,
  sentinel replacement identifiers, `reviewed_template_sql.sha256`,
  `live_template_verification` (`repository_sql_sha256`, `pages_sha256`,
  `manifest_sha256`, `template_bundle_sha256`, the
  `verified_manifest_sha256` and `verified_template_bundle_sha256` digests of
  the last live verification, and `pending_live_sync`), and
  `external_access_verification`.
- **Contract validator**: `python3 tools/validate_contracts.py --profile bqca`
  verifies 1:1 parity across `spec/bqca_product_contract.yaml`,
  `spec/bqca_chart_manifest.yaml`, and `bindings/bqca_report_template.yaml`.

### 7 pages and 34 tool-free components

| Page | Page ID | Scorecards | Charts / Tables | Total |
|---|---|---|---|---|
| 1. Token Consumption | `p_539b9240` | 5 (`kpi_total_tokens`, `kpi_input_tokens`, `kpi_output_tokens`, `kpi_thoughts_tokens`, `kpi_cached_tokens`) | 2 (`chart_tokens_by_agent`, `chart_tokens_over_time`) | 7 |
| 2. Data Agents & Turns | `p_a89cfece` | 4 (`kpi_distinct_agents`, `kpi_total_sessions`, `kpi_turn_completes`, `kpi_extracted_sql_count`) | 2 (`chart_turns_by_agent`, `chart_events_by_agent`) | 6 |
| 3. LLM Interactions & Embedding Suggestions | `p_97efe693` | 4 (`kpi_llm_calls`, `kpi_llm_latency_avg`, `kpi_ttft_avg`, `kpi_similar_queries`) | 2 (`chart_tokens_by_model`, `chart_embedding_reasons`) | 6 |
| 4. User & Persona Analytics | `p_edf06c14` | 3 (`kpi_distinct_personas`, `kpi_distinct_users`, `kpi_distinct_conversations`) | 2 (`chart_turns_by_persona`, `chart_persona_breakdown`) | 5 |
| 5. Latency & Fast-Path ROI | `p_08774fec` | 3 (`kpi_turn_latency_avg`, `kpi_llm_latency_p5`, `kpi_total_latency_avg`) | 2 (`chart_fast_path_pie`, `chart_latency_by_fast_path`) | 5 |
| 6. Errors (BQCA 3-Condition) | `p_88bcf5f8` | 2 (`kpi_total_errors`, `kpi_distinct_errors`) | 2 (`chart_errors_by_event_type`, `chart_errors_by_agent`) | 4 |
| 7. Prompt, Response & SQL Inspector | `p_b87a335e` | 0 | 1 (`tbl_prompt_response_sql`) | 1 |
| **Total** | **7 pages** | **21 scorecards** | **13 charts/tables** | **34 components** |

### BlockDatasource native date/time and parameter invariants

The embedded `CUSTOM_QUERY` (`sql/bqca_events_v1.template.sql`) filters `timestamp`
using `PARSE_DATE('%Y%m%d', @DS_START_DATE)` and `PARSE_DATE('%Y%m%d', @DS_END_DATE)`.
On the published `BlockDatasource` (`70e5ac1e-65bb-4c89-a9df-fa9990cb6029`):

1. `use_datetime_type: true` and `parameter_configuration: [DS_START_DATE, DS_END_DATE]`
   must remain enabled so Looker Studio binds the report date window instead of
   substituting `''`.
2. `_event_date_` (`DATE` / `YEAR_MONTH_DAY`), `_timestamp_` (`TIMESTAMP` /
   `YEAR_MONTH_DAY_SECOND`), and `_event_hour_` (`TIMESTAMP` /
   `YEAR_MONTH_DAY_HOUR`) must retain `use_native_date_time: true` across the
   41-field schema.

### Report-wide unique `qt_` column IDs

This repository requires every column (concept) of the BQCA manifest and
bundle to have a `qt_` ID, `qt_` followed by 3 to 10 lowercase letters or
digits (`^qt_[a-z0-9]{3,10}$`), that is unique across the whole report. This
is the repository's convention, not a Looker Studio rule: Looker Studio also
accepts generic names, and the live template
(`42e79a1f-a979-4de8-911f-80d5a99543d5`) still reuses generic IDs across its
34 components (`met`, `dim`, `m0`..`m3`, `d0`..`d9`). With those IDs,
components on the same page of a report created from it with the 1-click link
can show each other's values: for example, the Total, Input, Output, and
Thinking Tokens scorecards can all sum `total_tokens`.

The repository contracts, `spec/bqca_chart_manifest.yaml` and
`spec/bqca_dashboard_template.json`, give each of the 65 column slots the ID
that `bqca_derive_report_column_ids` in `tools/validate_contracts.py` derives
from the slot's digest:

```python
sha256(f"{component_id}:{slot_index}:{field_name}".encode()).hexdigest()
```

where `component_id` is the component's `cd-...` ID, `slot_index` is the
column's position in `spec.<kind>.dataset.columns`, and `field_name` is its
`fieldName`. The ID is `qt_` followed by the first 8 hex digits of the digest
(`bqca_qt_column_id`), as it is for all 65 slots today. If the digests of two
slots share their first 8 hex digits, both IDs instead take the shortest
prefix, 9 or 10 hex digits long, that no other digest starts with, so the IDs
do not depend on the order of the slots. If two digests share their first 10
hex digits (for 65 slots, a chance of about 2 in a billion), the validator
fails and names both slots; moving one of the two columns to another position
in its `dataset.columns` gives it a new digest (or, if neither column can move
— for example, two single-column scorecards — extend `_QT_DERIVED_HEX_LENGTHS`
and the upper bound of `_QT_COLUMN_ID_RE` in `tools/validate_contracts.py` and
sync the live template). A slot's ID changes whenever
its column moves to another position, its `fieldName` changes, or a new slot
collides with it, and each such change needs the live sync described below.

`python3 tools/validate_contracts.py --profile bqca` rejects:

- a column ID that is not a `qt_` ID, or is not its slot's derived ID;
- a column ID that repeats inside a component or anywhere in the report, or
  that differs between `spec/bqca_chart_manifest.yaml` and
  `spec/bqca_dashboard_template.json`;
- a `*Id` or `*Ids` key under `spec.<kind>.fields` other than those the kind
  reads its columns from: `primaryMetricId` for a scorecard;
  `primaryAxis.fieldId`, `metricSeries.metricIds`, and
  `primaryAxis.sort.sortFieldId` for a bar or line chart; `dimensionId`,
  `metricId`, and `sort.sortFieldId` for a pie chart; and `dimensionIds`,
  `metricIds`, and `sort.sortFieldId` for a table;
- a reference from one of those keys to a column that its component does not
  define, and a column that none of them references;
- an embedded spec `sort` that is not a list;
- a manifest top-level `components` list that is not exactly the components
  of its pages, in page order;
- two scorecards on one page that show the same `(field, aggregation)`.

The live template keeps its legacy generic IDs, and its report-level
`datasource_id` still names the source report's data source, until it is
synced from these contracts
([#559](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/559)).
The sync must rename the 65 concepts of its 34 components to exactly the `qt_`
IDs in `spec/bqca_chart_manifest.yaml`, keeping each concept's source field
and aggregation and updating every `concept_names` and `sort_column` that
refers to it, and repoint the report-level `datasource_id` to
`70e5ac1e-65bb-4c89-a9df-fa9990cb6029`. The live audit below compares names
exactly, so it fails on any other name. Until the sync,
`live_template_verification` in `bindings/bqca_report_template.yaml` keeps
`verified_date: "2026-10-09"` and the `verified_manifest_sha256` and
`verified_template_bundle_sha256` digests of that verification, and
`pending_live_sync` records the sync (`reason`, `since_date`,
`tracking_issue`, and `required_actions`). The validator requires
`pending_live_sync` while `manifest_sha256` or `template_bundle_sha256`
differs from its verified digest, and rejects it once both match again. A
report copied from the template before the sync can keep the generic IDs it
was copied with, so after the sync, re-create any such report from the
1-click link.

To audit the live template itself, save a Looker Studio report textproto dump
of it (its report configuration in protobuf text format) and run:

```bash
python3 tools/validate_contracts.py --live-report-textproto /path/to/report.txt
```

Looker Studio does not expose a public API for exporting a report's raw
textproto configuration; maintainers with read access to the template report
export the dump with internal inspection tooling and strip non-contract fields,
while anyone can run `--live-report-textproto` on the checked-in sanitized
fixtures described below.

The command exits 1 and prints one error for each of these defects:

- the dump cannot be read: the reader accepts the subset of protobuf text
  format that a saved report uses (`#` comments, a leading UTF-8 byte order
  mark, `{}` and `<>` messages, `[...]` lists after a `:`, and `,` or `;`
  separators), and rejects `[type.googleapis.com/...]` extension or `Any`
  field names, a message list without a `:` (`field [ {...} ]`), and messages
  nested more than 64 levels deep;
- a component appears twice;
- a `concept_defs.name` is not a `qt_` ID, or its `id` is not `t0.<name>`;
- a name repeats inside its component, or repeats the name of an earlier
  component;
- a `concept_names` or `sort_column` field appears under `component_property`
  outside `dimensions.labeled_concepts.value.concept_names`,
  `metrics.labeled_concepts.value.concept_names`, or `sort.sort_column`;
- a chart reference (`concept_names`, `sort_column`) at one of those paths
  names no concept of its component, or a concept is not referenced there;
- one of the 34 manifest components is missing;
- a manifest component's `(name, source_field_name, aggregation)` concepts are
  not exactly the `(id, fieldName, aggregation)` columns that
  `spec/bqca_chart_manifest.yaml` lists for it;
- a manifest component's `dimensions` or `metrics` `labeled_concepts` entries
  (each a single `primary` entry of `concept_names`), `sort_column`s,
  `sort_dir`s, or `reverse_sort_order` differ, in order, from the dimension
  IDs, metric IDs, `sortFieldId`s, `sortDir`s, or `reverseSortOrder` of its
  `spec.<kind>.fields`;
- a manifest component's `chart_type` is not one whose dimension, metric, and
  sort references the audit can read, so the comparison above cannot run;
- a manifest component's `datasource_id` or `dataset_id` is missing or is not
  its manifest `datasource_id`;
- a component that defines concepts is not in the manifest;
- the report-level `datasource_id` is missing or is not the `datasource_id` in
  `bindings/bqca_report_template.yaml`;
- the report has no `ds0` alias, or the alias points at another data source.

**Not audited by `--live-report-textproto`:** chart row limits (`limit` /
`row`), pie-chart other-slice grouping (`includeOthers` /
`include_total_others`), visual styling, and page layout geometry. Those stay
governed by the offline manifest and bundle validators.

`tests/fixtures/bqca_live_report_42e79a1f_sanitized.textproto` is a sanitized
cut of a dump of the live template saved on 2026-10-29, before the sync. It
keeps only the fields that the audit reads. Its report-level `datasource_id`, which named the
source report's retired data source, reads
`00000000-0000-4000-8000-000000000000`. The audit reports 209 errors on it:
65 generic concept names, 49 repeated names, 34 components whose concepts
differ from the manifest's columns, 34 metric `concept_names` lists, 13
dimension `concept_names` lists, 13 `sort_column` lists, and the report-level
`datasource_id`. Its twin,
`tests/fixtures/bqca_live_report_42e79a1f_sanitized_remapped.textproto`,
applies the sync's rename and repoint to the same text (the 65 concept names,
their `id`s and every reference to them, and the report-level
`datasource_id`) and changes nothing else, and the audit reports no errors on
it. `tests/test_looker_studio_dashboard.py` pins both verdicts.

### Fallback paths

When a user's Workspace organization blocks external Looker Studio templates or
while the outside-account copy check of the publicly shared template is pending
(`#515`), users have two alternatives over the same 15-column BQAA base schema:

1. **Self-Hosted Streamlit BQCA Dashboard** (`dashboards/streamlit/`) — renders
   5 BQCA tabs and 9 KPI tiles locally or on Cloud Run using its own
   parameterized BigQuery SQL builders (`dashboards/streamlit/bqca_queries.py`).
2. **Bound custom query export** — `python3 tools/hydrate_dashboard.py --profile bqca --project PROJECT --dataset DATASET --custom-sql-out /tmp/bqca_events.sql`
   writes the validated single-scan BQCA query bound to the user's table.

