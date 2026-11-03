# Looker Studio implementation contract

This document connects the generated parity manifest to the canonical report
implementation. It contains no report ID, production project, or dataset name.

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
the copy's report configuration, and on 2026-11-01 the live template's 65
concepts and report-level `datasource_id` were synced from the repository
contracts and verified at revision `1793504835696`
([#559](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/559)).
The owner turned on public link sharing for the copy on 2026-10-09; the copy
check from a signed-in outside Google account is still pending.

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
- **Report template binding**: `bindings/bqca_report_template.yaml`,
  `bindings/bqca_template_bindings.yaml`, and
  `bindings/bqca_live_capture_receipt.json` (plus `.sig` and
  `bindings/bqca_live_capture_signers`) pin the report ID, `ds0` alias,
  sentinel replacement identifiers, `reviewed_template_sql.sha256`,
  `live_template_verification` (`sync_generation`, `verified_date`,
  `repository_sql_sha256`, `pages_sha256`, `manifest_sha256`,
  `template_bundle_sha256`, `verified_manifest_sha256`,
  `verified_template_bundle_sha256`, `verified_revision`,
  `verified_dump_sha256`, `capture_receipt_sha256`, `captured_by`, and
  `pending_live_sync` whenever a contract change is awaiting a live sync), and
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
accepts generic names, and before the 2026-11-01 live sync
([#559](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/559))
the live template (`42e79a1f-a979-4de8-911f-80d5a99543d5`) reused generic IDs
across its 34 components (`met`, `dim`, `m0`..`m3`, `d0`..`d9`). With those
IDs, components on the same page of a report created from it with the 1-click
link could show each other's values: for example, the Total, Input, Output, and
Thinking Tokens scorecards could all sum `total_tokens`.

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
- a key of a component's embedded `spec` other than `id`, `fixedLayout`, and
  the `spec.<kind>` block that its `chart_type` reads, such as a second chart
  block (`spec.barChart` on a scorecard): the column checks above read
  `spec.<kind>` alone, so a column in any other block would escape them;
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

On 2026-11-01 (`sync_generation: 1`, revision `1793504835696`,
[#559](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/559)),
the live template was synced from these contracts: all 65 concepts of its 34
components were renamed to the exact `qt_` IDs in `spec/bqca_chart_manifest.yaml`
(keeping each concept's source field and aggregation and updating every
`concept_names` and `sort_column` that refers to it), and its report-level
`datasource_id` was repointed to `70e5ac1e-65bb-4c89-a9df-fa9990cb6029`. The
live audit below compares names exactly, so it fails on any other name. Whenever
a contract change alters `manifest_sha256` or `template_bundle_sha256` before
the live template is re-synced, `live_template_verification` in
`bindings/bqca_report_template.yaml` keeps the `verified_date`,
`verified_manifest_sha256`, and `verified_template_bundle_sha256` digests of the
last live verification, increments `sync_generation`, and adds
`pending_live_sync` (`reason`, `since_date`, `tracking_issue`, and
`required_actions`). The validator requires `pending_live_sync` while
`manifest_sha256` or `template_bundle_sha256` differs from its verified digest,
and rejects it once both match again. A report copied from the template before
the 2026-11-01 sync can keep the generic IDs it was copied with, so any such
report should be re-created from the 1-click link.

To audit the live template itself, save a Looker Studio report textproto dump
of it (its report configuration in protobuf text format) and run:

```bash
python3 tools/validate_contracts.py --live-report-textproto /path/to/report.txt
```

Looker Studio does not expose a public API for exporting a report's raw
textproto configuration; maintainers with read access to the template report
export the dump with internal inspection tooling. The checked-in sanitized
fixtures under `tests/fixtures/` strip non-contract fields for public audit
reproducibility, whereas a private live dump passed to
`tools/finalize_live_sync.py` must retain its identity fields
(`report_config.shareable.id`, `report_config.report.datasource_id`, and the
`ds0` entry of `report_config.report.resource.datasource_alias`). Anyone can
run `--live-report-textproto` on the checked-in sanitized fixtures described
below.

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

#### Finalizing the live template sync

Four tools support the sync; none of them changes the live template.
`tools/gen_bqca_rename_map.py` lists the rename, one row per concept in page,
component, and column order: the old name, the new `qt_` ID, the source field
and aggregation to keep, and the chart references to update. It fails,
writing nothing, when a concept or reference of the dump would be left out of
the map: a component that is not in the manifest but defines concepts or
holds references, or a reference, in any component, that names no concept of
its own component. By default it reads the sanitized cut above and checks the
map against its remapped twin, which must have the same components with
concepts or references; `--dump PATH` reads a newer dump taken before the
sync:

```bash
python3 tools/gen_bqca_rename_map.py --csv rename-map.csv --md rename-map.md
```

`tools/apply_bqca_rename_map.py` applies that rename to a dump taken before the
sync and writes the result, the dump's remapped twin, so the sync can be
reviewed before anyone touches the live template. In the dump it renames each
`concept_defs` `name` to its new `qt_` ID and its `id` to `t0.` followed by
that ID, renames each dimension or metric `concept_names` entry and each
`sort_column` of the same component that refers to the concept, and sets
`report_config.report.datasource_id` to the `datasource_id` of
`bindings/bqca_report_template.yaml`. Every other byte, comments and spacing
included, is kept: each edit replaces one quoted literal in place, and the tool
prepends `# bqca-offline-preview: written by tools/apply_bqca_rename_map.py;
not a dump of a synced live template.` as the first line (after any UTF-8 byte
order mark). The tool never writes or reads a capture receipt (below). The
capture tool and `tools/finalize_live_sync.py` refuse its output as the
capture after the write-back whatever its marker, comments, or spacing say:
they compare that capture's parsed content with the preview's, ignoring the
order of fields and of repeated values.
The live names repeat across components, so a rename is keyed by the component
and the old name, never by the name alone. The data source IDs of the
components and of the `ds0` alias are not edited; the audit refuses a dump
where one of them names another data source, and nothing is written for it.

```bash
python3 tools/apply_bqca_rename_map.py --dump <dump> --check
python3 tools/apply_bqca_rename_map.py --dump <dump> \
    --out <path outside the repository>
```

The map is the one `gen_bqca_rename_map.py` derives from the dump and the
manifest. `--map PATH` also reads a CSV that tool wrote, and fails, writing
nothing, unless the CSV matches the derived map exactly. Before the tool writes
anything, it checks the result four ways:

- its tokens equal the dump's, except for the edited literals, and exactly the
  planned scalars differ;
- it parses to the parsed dump with the map applied, by a traversal that does
  not use the edits;
- `tools/gen_bqca_rename_map.py` accepts it as the remapped twin of the dump;
- `tools/validate_contracts.py --profile bqca --live-report-textproto` exits 0
  on it.

The tool reads the dump once. Every check reads a private, read-only copy of
those bytes, never the path again, so a file that replaces the dump while the
tool runs cannot be mixed in, and the file written is the bytes that passed. It
is written with mode 0600, atomically. The tool refuses an `--out` inside this
repository by its path, its directory, or the place a link leads to (checked
both by path prefix and by `os.path.samestat` so a case-insensitive spelling
cannot bypass it; a dump of the live template does not belong in the
repository), an `--out` that is the dump, and an `--out` that exists, unless
`--force` replaces it. `--check` runs every check, says whether a write would
be refused and why, and writes nothing; it needs no `--out`. The tool exits 0
when it wrote the remapped dump, and with `--check` when every check passed,
even if a write would be refused; 1 when it rejected the dump or the map, a
check failed, or the write was refused; and 2 on bad arguments.

A dump that already carries the new IDs maps to itself, and the tool writes a
copy (adding the `# bqca-offline-preview:` header comment line once if it is
not already present, so applying the tool a second time to an already-marked
preview maps it to itself byte for byte). Applied to the sanitized cut above,
the tool writes the `# bqca-offline-preview:` line, the cut's own header
comments, since it keeps every comment and the twin has others, and from the
first line that is not a comment on, the checked-in twin byte for byte;
`tests/test_looker_studio_live_sync.py` pins that. The result previews the
sync; it is not a dump of a synced template. `tools/finalize_live_sync.py`,
below, needs two captures of the live template itself, made by
`tools/sign_live_capture.py` before and after the write-back.

##### The capture, apply, write-back, capture, finalize steps

`tools/sign_live_capture.py` replaces the bare export command: it runs the
internal export command given after `--`, which must write the report
textproto of the live template to standard output, and keeps the bytes it
reads from that pipe, never a file's. It writes them to `--out` (mode 0600;
never inside the repository) with a signed capture receipt of those bytes.
Run it before the write-back and again after it, with `--before` naming the
first capture:

```bash
python3 tools/sign_live_capture.py --out <D0> --revision <revision before> \
    --signer <signer> --key <SSH key file> -- <export command>
python3 tools/apply_bqca_rename_map.py --dump <D0> --out <offline preview>
# Write the offline preview back to the live template with the internal
# tooling, then:
python3 tools/sign_live_capture.py --out <D1> --before <D0> \
    --revision <revision after> --signer <signer> --key <SSH key file> \
    -- <export command>
python3 tools/finalize_live_sync.py --dump <D1> --before <D0> \
    --revision <revision after> --check
python3 tools/finalize_live_sync.py --dump <D1> --before <D0> \
    --revision <revision after>
```

A capture is three files: the export, `<export>.receipt.json`, and
`<export>.receipt.json.sig`. Keep both exports privately (outside the
repository); finalizing commits only the receipt of the second.

##### Capture receipts

Nothing in a dump proves where it came from, and the offline preview from
`tools/apply_bqca_rename_map.py` has exactly the audited content of a
capture of the synced template (the audit compares that content with the
manifest). A marker comment cannot tell them apart either: changing its letter
case or deleting it is a one-line edit. So a write needs evidence from outside
the dump, and the capture after the write-back must differ, in some field
value, from the offline preview of the capture before it.

A capture receipt is a JSON object with exactly these fields, each once
(`tools/sign_live_capture.py` writes them in this order):

| Field | Value |
|---|---|
| `format` | `bqca-live-capture-receipt/v3` |
| `report_id` | the `report_id` of `bindings/bqca_report_template.yaml` |
| `revision` | the revision of the live report that the export captured, as the export tooling or the report's version history shows it: 1 to 128 letters, digits, `.`, `_`, `:`, `+`, or `-` |
| `dump_sha256` | the SHA-256 of the exported bytes |
| `signed_at` | when the receipt was signed, in UTC, `YYYY-MM-DDTHH:MM:SSZ` |
| `signer` | the signer's name in `bindings/bqca_live_capture_signers` |
| `export_command_sha256` | the SHA-256 of the export command as a JSON list, so that no internal command, path, or token is published |
| `manifest_sha256`, `template_bundle_sha256` | the binding's pinned digests when the export was captured |
| `pending_since_date` | the `since_date` of the binding's `pending_live_sync` when the export was captured |
| `sync_generation` | the binding's `live_template_verification.sync_generation` when the export was captured: the generation of the sync it is for |
| `before_dump_sha256`, `before_revision` | for the capture after the write-back, the SHA-256 and revision of the `--before` capture; `null` for the capture before it |

Its signature, `<receipt>.sig`, is an OpenSSH signature (`ssh-keygen -Y
sign`) of the receipt's exact bytes in the `bqca-live-capture-receipt`
namespace, so no signature that the same key made for anything else (a commit,
a file) verifies as one. `--key` is an SSH private key file, or the public key
file of a key that ssh-agent holds; ssh-keygen asks for any passphrase.

The capture tool writes nothing for an export that does not decode, parse, and
name the live template in `report_config.shareable.id`, or that carries the
offline-preview marker in any form; after the write-back, the report-level
data source and the `ds0` alias must be the binding's too. It writes nothing
unless the binding has a pending sync and `bindings/bqca_live_capture_signers`
lists the key for `--signer` (that error prints the line that would list it).
With `--before`, it also writes nothing unless the `--before` capture has a
trusted receipt of a capture before any write-back for the same pending sync,
`--revision` differs from that capture's, the export differs from it, and the
export's parsed content (comments, spacing, and the order of fields and
repeated values aside) differs from that of the offline preview of the
`--before` export. It refuses an existing capture
unless `--force`.

It writes the three files in steps. It stages each next to its path, then,
with `--force`, copies each earlier file, bytes and mode, to a
`.<name>.*.backup` file next to it, and only then puts the staged files in
place, one at a time. If a step fails (a full disk, a file that cannot be
replaced) or the tool is interrupted (Ctrl-C, SIGINT) before the last file is
in place, it puts every path back as it was, each earlier file from its
backup, removes any file that was not there, and ends with `not captured:
nothing written`. SIGINT is held while the files are written or put back, and
acts once the current step ends. What it does not undo:

- A file it cannot put back: the error names it and the backup that keeps its
  original (`cannot put back <path>: <reason>; its original is kept at
  <backup>`, or `cannot tell what <path> holds now; ...` if the path holds
  neither the new file nor its original). That backup is never removed, and
  the output ends with `not captured: not every file above could be put back
  as it was`; move the backup to its path by hand. A new file that cannot be
  removed is named alone, as there was no earlier one.
- A process that is killed outright (SIGKILL, a crash) puts nothing back: a
  `.<name>.*.backup` file next to a capture file holds the earlier file, and
  a `.<name>.*.tmp` file is a staged new one. The tool does not sync the
  backups to disk before the replacements, so after a power loss a backup
  can be incomplete.
- An interrupt once all three files are in place: the capture stands, and
  the tool exits 0 with `warning: interrupted once every file was in place;
  the capture stands`.

A staged file or backup that cannot be removed afterwards is named in a
warning.

`bindings/bqca_live_capture_signers` lists the trusted keys in the OpenSSH
allowed signers format (see `ssh-keygen(1)`), one line per maintainer who
exports the live template with the internal report tooling, in exactly this
form:

```text
<signer> namespaces="bqca-live-capture-receipt" <key type> <public key>
```

`<signer>` is a public name, for example a GitHub username: letters, digits,
`.`, `_`, `@`, `+`, or `-`. ssh-keygen also reads a pattern (`*`, `?`), a
comma-separated list, or a quoted name there, takes other options (or none)
that widen what a line trusts, and accepts a second line for the same signer
with another key. So the tools trust no receipt unless every line that is not
blank or a `#` comment has one literal `<signer>`, exactly that `namespaces`
option, a plain key type (`ssh-ed25519`, `ssh-rsa`, `ecdsa-sha2-nistp256`,
`ecdsa-sha2-nistp384`, `ecdsa-sha2-nistp521`, `sk-ssh-ed25519@openssh.com`,
or `sk-ecdsa-sha2-nistp256@openssh.com`; never a certificate), and a key in
canonical base64 (whole groups of four characters, padded only where base64
needs it) that decodes to the OpenSSH wire form of a public key of that type,
with at most a key comment after it, and no two lines list the same
signer in any letter case. To change a signer's key, replace that signer's
line. A line is added, changed, or removed only in a reviewed change; no
command-line option or environment variable names another signers file. Only
receipts signed by a key listed in `bindings/bqca_live_capture_signers` are
trusted (`haiyuan-eng-google` is listed for the `sync_generation: 1` capture
of #559, with the key whose fingerprint is
`SHA256:pum9Cf+yARVa64p0awyJRYegBJJL0AkCBKjAONAwLkY`). Before approving a
change to a signer's line, confirm its fingerprint with that signer by another
channel than the change itself; this prints the fingerprint of each listed
key:

```bash
awk '!/^#/ && NF {print $3, $4}' bindings/bqca_live_capture_signers |
    ssh-keygen -l -f -
```

Anyone can check a receipt by hand:

```bash
ssh-keygen -Y verify -f bindings/bqca_live_capture_signers -I <signer> \
    -n bqca-live-capture-receipt -s <receipt>.sig < <receipt>
shasum -a 256 <export>  # the receipt's dump_sha256
```

##### What finalizing checks and records

`tools/finalize_live_sync.py` runs `tools/validate_contracts.py --profile bqca
--live-report-textproto` on the capture after the write-back (`--dump`) and
edits `bindings/bqca_report_template.yaml` only if it exits 0. In
`live_template_verification` it sets `verified_date` (`--verified-date`,
default today in UTC), copies `manifest_sha256` and `template_bundle_sha256`
into `verified_manifest_sha256` and `verified_template_bundle_sha256`, records
the capture (`verified_revision`, the `--revision`; `verified_dump_sha256`,
the SHA-256 of the export; `capture_receipt_sha256`, the SHA-256 of its
receipt; and `captured_by`, the receipt's signer), and removes
`pending_live_sync`, keeping every other line and comment. It also commits the
receipt and its signature as `bindings/bqca_live_capture_receipt.json` and
`bindings/bqca_live_capture_receipt.json.sig`. The new files replace the old
ones atomically (staged files and `os.replace`; the binding last); the tool
audits the export again after the write and puts the original bytes of all
three back (or removes a receipt that was new) if that audit fails, raises, or
is interrupted. It reads each export, receipt, and signature once: the checks
below, both audits (through a private, read-only copy of the export), and the
recorded digests use those bytes, never the paths again. It refuses a binding
without a `live_template_verification` mapping, exiting 1.

With nothing pending, --check writes nothing but still runs the audit: it
refuses an export that is not UTF-8 text or that carries the offline-preview
marker, exiting 1; otherwise it runs the audit, which checks the binding, its
committed receipt, and that receipt's signature and signer along with the
export's content, and exits 1 if the audit fails. A pass reports
`nothing to finalize`, but confirms the
recorded sync only for the capture on record: the export whose SHA-256 is
`verified_dump_sha256`, outside `tests/fixtures/`, whose identity fields name
the binding's report (see below). `--check --dump <export>` needs neither
`--before` nor `--revision` here; it exits 0 on any export that passes the
audit and says `the capture that it records` for the capture on record, or
`content audit only, not the capture that it records` with the reasons for any
other export (a later export of the live template, or a fixture). A write
still needs `--before` and `--revision`, and exits 0 only for the capture on
record with the inputs of the write that recorded it: the receipt beside it
that `capture_receipt_sha256` records, its `verified_revision` as
`--revision`, and the `--before` export whose SHA-256 that receipt names. It
exits 1 for any other export or input.

A write fails closed, exiting 1 and leaving every file unchanged, unless:

- `ssh-keygen -Y verify` verifies both receipts' signatures in the
  `bqca-live-capture-receipt` namespace, made by keys that
  `bindings/bqca_live_capture_signers` lists for their signers;
- both receipts are exactly v3 receipts of the exports' bytes (a receipt or
  signature that cannot be read, other or repeated fields, and malformed
  values are refused), for the binding's `report_id`, its pinned manifest and
  bundle digests, its pending sync (`pending_since_date`), and its
  `sync_generation`, signed on or
  after the day that sync began (inclusive, in UTC) and not in the future
  (with five minutes of clock skew); the dump's receipt is for `--revision`;
- the dump's receipt is of a capture after a write-back that names the
  `--before` capture (its SHA-256 and revision); the `--before` receipt is of
  a capture before any write-back, signed no later; and the two are of
  different revisions;
- the dump's parsed content differs from that of the offline preview that
  `tools/apply_bqca_rename_map.py` writes for the `--before` export
  (`preview_tree`), compared as the multiset of its fields and values
  (`canonical_content`): comments, spacing, quoting, the marker, and the order
  of fields and repeated values aside, the live report gained, lost, or
  changed a field value after the write-back;
- the export decodes as UTF-8, parses as protobuf text format (with
  universal-newline normalization matching `tools/validate_contracts.py`), and
  its identity fields each have exactly one value equal to the binding's:
  `report_config.shareable.id` is the `report_id`, and
  `report_config.report.datasource_id` and the `ds0` entry of
  `report_config.report.resource.datasource_alias` are the `datasource_id`
  (`70e5ac1e-65bb-4c89-a9df-fa9990cb6029`); the report ID in any other field,
  inside a longer value, or in a comment does not count;
- the export carries no offline-preview marker of
  `tools/apply_bqca_rename_map.py`, in any letter case or spacing, anywhere,
  and is not under `tests/fixtures/` (checked both by path prefix and by
  `os.path.samestat`);
- `--verified-date` is not in the future, nor before `since_date` or the day
  the dump's receipt was signed.

A write without `--before` or `--revision` exits 2 before any audit, also when
nothing is pending; the free-text `--captured-from` attestation of earlier
revisions of this tool is gone, and the dump's receipt is always the one next
to it. With a sync pending, `--check` runs the same checks and the audit,
prints the change as a diff (with placeholders for the values that only a
write's arguments give), lists every reason a write would be refused, and
writes nothing; it needs no receipt and previews with the checked-in fixtures
and with offline previews too. It exits 0 when the audit passes, even if a
write would be refused.

If the export of the synced template has exactly the fields and values of the
offline preview, in any order, so that no field of the live report (a
revision or update time, for example) changed with the write-back, the
capture tool and finalize refuse it: nothing then tells it apart from the
preview. Report that on the
tracking issue instead of editing the export.

##### The committed receipt and the sync generation

`live_template_verification.sync_generation` numbers the live syncs of the
template. It is 1 for the sync that #559 tracked; whoever reopens a finalized
sync (adds `pending_live_sync` again) also sets it to the next generation, and
it stays when finalize records the sync. Both capture receipts carry the
generation, and finalize writes only with receipts for the binding's.

`tools/validate_contracts.py` checks every binding whose verified digests
equal the pinned ones. Such a binding records a live sync: it must have no
`pending_live_sync` key at all (a null, empty, or other value that is not a
pending record still counts, and fails), and it needs
`bindings/bqca_live_capture_receipt.json` and its signature, which must verify
against `bindings/bqca_live_capture_signers`, and the receipt must be of a
capture after a write-back, for the binding's report, pinned digests, and
`sync_generation`, signed on or after its `pending_since_date` and not in the
future. `verified_revision`, `verified_dump_sha256`, `capture_receipt_sha256`,
and `captured_by` must be the receipt's, and `verified_date` must not be
before the receipt was signed. While the digests differ, `pending_live_sync`
must be a well-formed pending record, and `sync_generation` must always be an
integer of at least 1.

So a hand edit that re-pins the verified digests and deletes
`pending_live_sync`, or sets it to null or an empty value, fails unless the
committed receipt is a valid one for the same generation. After a reopen, the
receipt of the earlier sync is for the earlier generation and fails. That
leaves one way back: moving `sync_generation` back to the earlier generation as
well, which a check of the binding alone cannot tell from the earlier,
finalized binding. `--previous-binding PATH` checks that `sync_generation`
only moves forward from the binding at `PATH`, and that a sync that was not
pending there and is pending here took a greater one. CI runs it against the
binding on the base branch (or before the push):

```bash
git show origin/main:dashboards/looker_studio/bindings/bqca_report_template.yaml \
    > previous-binding.yaml
python3 tools/validate_contracts.py --profile bqca \
    --previous-binding previous-binding.yaml
```

What validation guarantees, then: a binding without `pending_live_sync` passes
only with a committed receipt that a listed signer signed for a capture after
a write-back, for the binding's report, pinned digests, and own generation,
with the capture record copied from it, and with that generation not moved
back (when `--previous-binding` runs, as in CI). The validator cannot see the
export itself, which stays private, so it cannot re-run the live audit or the
offline-preview comparison: finalize runs those before it writes, and review
of the change is what shows that finalize, not a hand edit, wrote the binding.

##### The trust boundary

The tools refuse the offline preview as the capture after the write-back,
whether its marker is kept, edited, or removed, and any edit of its comments,
spacing, or quoting, or reordering of its fields or repeated values; they
refuse a receipt that is not trusted, not for this pending sync, stale, in the
future, or for other bytes; and the capture tool signs only bytes that an
export command printed. What no check can refuse is a listed signer who signs
for content that the export command did not fetch from the live report: one
who passes as the export command a program that prints a file and also edits
a field value of the preview (writing a scalar another way, such as an enum
by its number or an integer in hex, counts as an edit, since the tools do not
know the report's protobuf schema), or who signs receipt bytes directly with
`ssh-keygen`. That is a false signed statement, which the receipt and the
binding (`captured_by`) attribute to the signer. Review of each change to the
signers file, and of the committed receipt, is the last check;
`tests/test_looker_studio_live_sync.py` pins this boundary.

The checked-in fixtures have no `report_config.shareable` entry, so a write
refuses them, copies of them anywhere, and any reordering or audit-inert edit
of them, including one that adds the report ID to another field, even with a
receipt. The data source fields are those a saved report keeps and the live
audit reads. No dump in this repository keeps the report ID (the sanitized
cuts drop it), so `report_config.shareable.id`, the report's own entry, is
where the tools expect it (`REPORT_ID_FIELD` in
`tools/finalize_live_sync.py`). If a real export names the report in another
field, a capture or write is refused with that field's path, and
`REPORT_ID_FIELD` needs to name it; an export that does not name its report at
all is refused.

The tool does not edit documentation or tests. In the same change, commit the
binding with `bindings/bqca_live_capture_receipt.json` and its signature,
which it wrote (they hold no secret; both exports stay private), and the line
that lists the signer in `bindings/bqca_live_capture_signers` if it is new;
remove the known-issue notes that `pending_live_sync.required_actions` listed
(and run `tools/render_web_config.py` if `docs/index.html` changed); update the
comments above `live_template_verification`; and update the tests in
`tests/test_looker_studio_dashboard.py` that pin the pending sync:
`test_bqca_block_datasource_invariants_and_live_attestation_are_recorded`,
`test_validate_contracts_requires_a_pending_live_sync_record`,
`test_validate_contracts_rejects_generic_or_repeated_column_ids`, and
`test_validate_contracts_cli_rejects_a_column_id_that_is_not_derived`. Then
check, in a report created with the 1-click link, that every page renders and
that each scorecard of the synced template shows its own field's value over
the bound query and each chart and table returns the rows that BigQuery
gives (up to its `limit`), and record the dated result (with the copy's
report ID) in the comments above `live_template_verification`, as for the
check below.

##### Post-sync scorecard check (2026-11-01)

After the `sync_generation: 1` sync (live revision `1793504835696`), the 13
scorecards of pages 1 to 3 were checked on two fresh copies of the live
template on 2026-11-01. On 2026-11-02, all 7 pages of the live template and of
both copies were rendered, and all 34 components (the 21 scorecards of pages 1
to 6 and the 13 charts and tables of pages 1 to 7) were checked against
BigQuery. Dates are in US Pacific time.

- The signed capture after the write-back gives each scorecard its own
  concept: a `qt_` ID of its own on its own field (all 65 concepts of the
  report are distinct).
- On 2026-11-01, `DataStudioWebService.CreateTempReport` with the Linking API,
  with the options of the 1-click link for the BQCA profile
  (`ds.ds0.refreshFields=false`: `creation_type: LINKING_API`,
  `datasource_alias: "ds0"`, `use_fresh_schema: false`, `fix_fields: false`,
  `connector_block_config.alias: "t0"` and `use_datetime_type: true`), created
  a fresh copy of the live template for each of two tables: the canonical
  sentinel table (dates `20200101` to `20261231`) and a hydrated BQCA Prompt &
  Response Logging table (13,054 rows from 2026-07-31 to 2026-10-22; dates
  `20250101` to `20261101`). Their report IDs are in the table below. Each
  fresh copy took the template's published `BlockDatasource` schema
  (`_event_date_` `DATE`/`YEAR_MONTH_DAY`, `_timestamp_`
  `TIMESTAMP`/`YEAR_MONTH_DAY_SECOND`, and `_event_hour_`
  `TIMESTAMP`/`YEAR_MONTH_DAY_HOUR`) and its `DS_START_DATE` and
  `DS_END_DATE` parameter configuration, and has all 65 distinct `qt_`
  concepts. They supersede copies made earlier that day with other options
  (`use_fresh_schema: true`, `fix_fields: true`,
  `connector_block_config.alias: "ds0"`, and no `use_datetime_type`), on which
  nothing below was measured.
- `DataStudioWebService.BatchedDataV2` (`VIEW` mode) on each fresh copy
  returned for each scorecard the exact value that BigQuery gives for its
  field over the bound query (`sql/bqca_events_v1.template.sql`) for the same
  table and dates. On the live template, whose `ds0` reads the sentinel table,
  it returned the same values as on the sentinel table's fresh copy.
- On 2026-11-02, `LegoMidtierService.RenderDashboardPdf` rendered all 7 pages
  of each of the three reports (each rendered without errors, with
  `contains_user_configuration_error: false`), and `BatchedDataV2` (`VIEW`
  mode) returned all 34 components of each without an error, each equal to
  BigQuery over the bound query.

The reports, each the template or a copy that reads one table through its own
`ds0` data source:

| Report | Report ID | `ds0` data source | Table | Dates |
| --- | --- | --- | --- | --- |
| Live template | `42e79a1f-a979-4de8-911f-80d5a99543d5` | `70e5ac1e-65bb-4c89-a9df-fa9990cb6029` | canonical sentinel table | `20200101` to `20261231` |
| Fresh copy | `920ba558-cc2e-44f9-bc35-96158b3ccb7e` | `9663f7a6-a609-431a-ba31-e27f345d8ea6` | canonical sentinel table | `20200101` to `20261231` |
| Fresh copy | `35ebd06d-4d15-4ca9-ad23-0631cbb87ca3` | `180a6d04-ab80-43c0-bf59-c48f3efa90ee` | hydrated table | `20250101` to `20261101` |

`LegoMidtierService.RenderDashboardPdf`, on 2026-11-02, rendered pages
`p_539b9240`, `p_a89cfece`, `p_97efe693`, `p_edf06c14`, `p_08774fec`,
`p_88bcf5f8`, and `p_b87a335e` of each report, in that order, into a 7-page
PDF. The size and SHA-256 are of the saved response, which stays private:

| Report ID | Rendered pages | `contains_user_configuration_error` | Response (bytes) | Response SHA-256 |
| --- | --- | --- | --- | --- |
| `42e79a1f-a979-4de8-911f-80d5a99543d5` | 7 | false | 257,802 | `d9a155ab8e4dba2a1c2fe1c0a7817e4238d334005405fb0935b3aa2c6b988b64` |
| `920ba558-cc2e-44f9-bc35-96158b3ccb7e` | 7 | false | 257,802 | `25e4e659103a2dcf7a76e0a56e8094587378f47ec7f124a48ed8e2b5c03d92c8` |
| `35ebd06d-4d15-4ca9-ad23-0631cbb87ca3` | 7 | false | 320,312 | `04572a8af8b670905a4135810907c94d194cab4599a542d066b7803ad0d91d9a` |

`DataStudioWebService.BatchedDataV2` (`VIEW` mode), on 2026-11-02, took one
request for all 34 components of each report. The sizes and SHA-256 digests
are of the saved request and response, which stay private:

| Report ID | Components | Errors | Request (bytes) | Request SHA-256 | Response (bytes) | Response SHA-256 |
| --- | --- | --- | --- | --- | --- | --- |
| `42e79a1f-a979-4de8-911f-80d5a99543d5` | 34 | 0 | 42,731 | `8eda477c127df752ff32ae378a2b250646cd4d17ddec02a686fb3687b1ac9a3b` | 139,002 | `55ebbdc9fde401a242b3bea18017fb6de6066779ebe70fb68346d7eb6e4142ec` |
| `920ba558-cc2e-44f9-bc35-96158b3ccb7e` | 34 | 0 | 42,731 | `6e8accf2a3c904a45097248a9d70345dd48add06464b47114b3be072f9e9ba57` | 137,504 | `d642044bcabffec5485a43a789cc031fa627d8608b6836c7fd58233780b3c4bf` |
| `35ebd06d-4d15-4ca9-ad23-0631cbb87ca3` | 34 | 0 | 42,731 | `751d13762476a0711802396b1fe1071be65a62e24fa192d7a0d7039cc769cfbd` | 153,553 | `7d7caf72ad68a22ca786793bbd14e8f0b908b065abecbd2dce6b27478b683ced` |

The three capture digests of the sync (each a SHA-256 of the exported
textproto bytes; sizes before encoding):

- **D0** (pre-write, revision `1793385843864`, 84,190 bytes) SHA-256
  `90e6f259ec923c2213e99c2a1b4cda79065c13753268bec65c68b51fbd40084a`
- **D0p** (offline preview patched export, 86,069 bytes) SHA-256
  `b9be2dae36ecfe21b6867247aa115295622b729db4b0f24b6f6f8c87200b9e92`
- **D1** (post-write, revision `1793504835696`, 86,045 bytes) SHA-256
  `7d0a64974f20d0412bd56a2e6c4a410bb85f7d7ae7798f434846bcf8358c1f86`

The `2026-11-01` fresh-copy run (`use_fresh_schema: false`, `fix_fields: false`)
supersedes the earlier `use_fresh_schema: true` / template-query run described
in the same-day completion comment on issue #559; the values above were
measured on the fresh copies that run created, and nothing was measured on the
earlier run. The signer of the capture receipt (`haiyuan-eng-google`) is listed
in `bindings/bqca_live_capture_signers` with the key whose fingerprint is
`SHA256:pum9Cf+yARVa64p0awyJRYegBJJL0AkCBKjAONAwLkY`.

The successful pre-write finalizer check on the D1 capture printed this final
line:

```
check: the live audit of /tmp/D1.textproto passed; bindings/bqca_report_template.yaml would change as above, and bindings/bqca_live_capture_receipt.json and its signature would be written; nothing written
```

The reports measured above are `42e79a1f-a979-4de8-911f-80d5a99543d5`
(template `ds0` data source `70e5ac1e-65bb-4c89-a9df-fa9990cb6029`) and its two
fresh copies, `920ba558-cc2e-44f9-bc35-96158b3ccb7e`
(`9663f7a6-a609-431a-ba31-e27f345d8ea6`) and
`35ebd06d-4d15-4ca9-ad23-0631cbb87ca3`
(`180a6d04-ab80-43c0-bf59-c48f3efa90ee`); all three report IDs and both data
source IDs appear in the binding's post-sync comments too.

The 21 scorecards of pages 1 to 6. Observed is the value that `BatchedDataV2`
returned; expected is the value that BigQuery returns for the scorecard's
field and aggregation over the bound query, for the same table and dates.
`null` is a field without values in that table and date range. Averages agree
to within floating-point rounding and are shown to two decimal places.

| Page | Page ID | ID | Scorecard | Component ID | Concept | Field, aggregation | Observed, fresh copy `920ba558` (sentinel) | Observed, live template `42e79a1f` (sentinel) | Expected, BigQuery (sentinel) | Observed, fresh copy `35ebd06d` (hydrated) | Expected, BigQuery (hydrated) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | `p_539b9240` | `kpi_total_tokens` | Total Tokens | `cd-vWRq2EdpQyI` | `qt_d779815a` | `total_tokens`, SUM | 39,146,504 | 39,146,504 | 39,146,504 | 59,097,881 | 59,097,881 |
| 1 | `p_539b9240` | `kpi_input_tokens` | Input Tokens | `cd-gQM6-qRztkI` | `qt_c90b69a4` | `input_tokens`, SUM | 32,708,861 | 32,708,861 | 32,708,861 | 57,677,125 | 57,677,125 |
| 1 | `p_539b9240` | `kpi_output_tokens` | Output Tokens | `cd-tGVUrOkdidM` | `qt_893d21f1` | `output_tokens`, SUM | 6,437,643 | 6,437,643 | 6,437,643 | 364,388 | 364,388 |
| 1 | `p_539b9240` | `kpi_thoughts_tokens` | Thinking Tokens | `cd-mNCe2wVAOSU` | `qt_a6d27140` | `thoughts_tokens`, SUM | null | null | null | 1,047,032 | 1,047,032 |
| 1 | `p_539b9240` | `kpi_cached_tokens` | Cached Tokens | `cd-q_nXnTUZkLs` | `qt_31dd80a8` | `cached_tokens`, SUM | null | null | null | 1,147,570 | 1,147,570 |
| 2 | `p_a89cfece` | `kpi_distinct_agents` | Active Data Agents | `cd-soVsNR_PepA` | `qt_a96c5e06` | `data_agent_id`, COUNT_DISTINCT | 1 | 1 | 1 | 3 | 3 |
| 2 | `p_a89cfece` | `kpi_total_sessions` | Total Turns | `cd-jzLBUUj5DHY` | `qt_d206edb0` | `invocation_id`, COUNT_DISTINCT | 14,100 | 14,100 | 14,100 | 2,041 | 2,041 |
| 2 | `p_a89cfece` | `kpi_turn_completes` | Completed Turns | `cd-i7aDBnf14Bo` | `qt_f0daf8fa` | `completed_turn_id`, COUNT_DISTINCT | 14,093 | 14,093 | 14,093 | 2,039 | 2,039 |
| 2 | `p_a89cfece` | `kpi_extracted_sql_count` | Generated SQL Queries | `cd-lGEjuCElXpw` | `qt_d4c2022b` | `extracted_sql`, COUNT | 0 | 0 | 0 | 0 | 0 |
| 3 | `p_97efe693` | `kpi_llm_calls` | LLM Responses | `cd-ldwCpp7xYjA` | `qt_581cc2cf` | `llm_latency_ms`, COUNT | 15,604 | 15,604 | 15,604 | 2,876 | 2,876 |
| 3 | `p_97efe693` | `kpi_llm_latency_avg` | Avg LLM Call Latency (ms) | `cd-qiFfbAM3qa0` | `qt_f4c85165` | `llm_latency_ms`, AVG | 5,941.28 | 5,941.28 | 5,941.28 | 7,429.72 | 7,429.72 |
| 3 | `p_97efe693` | `kpi_ttft_avg` | Avg Time to First Token (ms) | `cd-vvYZt2if5WY` | `qt_1444bfec` | `ttft_ms`, AVG | 762.83 | 762.83 | 762.83 | 7,429.71 | 7,429.71 |
| 3 | `p_97efe693` | `kpi_similar_queries` | Suggested Columns Proposed | `cd-gc3G-O-taBE` | `qt_291ed8e1` | `similar_queries_count`, SUM | null | null | null | null | null |
| 4 | `p_edf06c14` | `kpi_distinct_personas` | Distinct Personas | `cd-oQfDfyi8Lv0` | `qt_d6385c41` | `persona`, COUNT_DISTINCT | 1 | 1 | 1 | 4 | 4 |
| 4 | `p_edf06c14` | `kpi_distinct_users` | Distinct User IDs | `cd-uRA2-9wgZ2Y` | `qt_aeca08f6` | `user_id`, COUNT_DISTINCT | 500 | 500 | 500 | 7 | 7 |
| 4 | `p_edf06c14` | `kpi_distinct_conversations` | Distinct Conversations | `cd-g1BkEGfLfKs` | `qt_a42e18b1` | `conversation_id`, COUNT_DISTINCT | 0 | 0 | 0 | 5 | 5 |
| 5 | `p_08774fec` | `kpi_turn_latency_avg` | Avg End-to-End Turn Latency (ms) | `cd-vHrTArr4HTQ` | `qt_1e5c0a97` | `turn_latency_ms`, AVG | null | null | null | 26,073.58 | 26,073.58 |
| 5 | `p_08774fec` | `kpi_llm_latency_p5` | Avg LLM Latency (ms) | `cd-siIPn3A0DhI` | `qt_235cb0ae` | `llm_latency_ms`, AVG | 5,941.28 | 5,941.28 | 5,941.28 | 7,429.72 | 7,429.72 |
| 5 | `p_08774fec` | `kpi_total_latency_avg` | Avg Event Latency (ms) | `cd-l9SJjy2jR-o` | `qt_16a686b5` | `total_latency_ms`, AVG | 5,940.97 | 5,940.97 | 5,940.97 | 18,344.53 | 18,344.53 |
| 6 | `p_88bcf5f8` | `kpi_total_errors` | Error Events (3-Condition) | `cd-piLo6w29Khs` | `qt_a813429f` | `error_message`, COUNT | 1 | 1 | 1 | 3 | 3 |
| 6 | `p_88bcf5f8` | `kpi_distinct_errors` | Distinct Error Signatures | `cd-m7c2OcIbUxo` | `qt_a8ab9425` | `error_message`, COUNT_DISTINCT | 1 | 1 | 1 | 1 | 1 |

The 13 charts and tables of pages 1 to 7. The request for each named its `qt_`
concepts and sorted by its `sort_column` in its direction, as the signed
capture has them, and each returned without an error (`OK`) from all three
reports. Observed is the number of rows that `BatchedDataV2` returned;
expected is the number of rows that BigQuery returns for the component's
dimensions over the bound query, for the same table and dates. The page 7
table returns its first 100 rows (`limit: 100` in
`spec/bqca_chart_manifest.yaml`) of the 56,380 and 10,184 that BigQuery
counts.

| Page | Page ID | ID | Chart or table | Component ID | Type | Concepts | Sort | Status | Observed rows, fresh copy `920ba558` (sentinel) | Observed rows, live template `42e79a1f` (sentinel) | Expected rows, BigQuery (sentinel) | Observed rows, fresh copy `35ebd06d` (hydrated) | Expected rows, BigQuery (hydrated) |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | `p_539b9240` | `chart_tokens_by_agent` | Token Usage by Data Agent | `cd-kctXIOqrPyQ` | `bar_chart` | `qt_47948fbd`, `qt_3fcfd7ea` | `qt_3fcfd7ea` DESC | OK | 1 | 1 | 1 | 3 | 3 |
| 1 | `p_539b9240` | `chart_tokens_over_time` | Daily Token Usage (Input / Output / Thinking) | `cd-kM4cFH3pweE` | `bar_chart` | `qt_e7eb0560`, `qt_af86847d`, `qt_42018c0c`, `qt_0db8b52b` | `qt_e7eb0560` ASC | OK | 32 | 32 | 32 | 55 | 55 |
| 2 | `p_a89cfece` | `chart_turns_by_agent` | Turns by Data Agent | `cd-mfKlsOmAHaM` | `bar_chart` | `qt_9fb169eb`, `qt_e056d0a6` | `qt_e056d0a6` DESC | OK | 1 | 1 | 1 | 3 | 3 |
| 2 | `p_a89cfece` | `chart_events_by_agent` | Data Agent Volume, Latency & Errors | `cd-rVzCGnjYxo0` | `table` | `qt_d0a517af`, `qt_a7ce9b32`, `qt_d188b546`, `qt_38e443e7`, `qt_abd056f8` | `qt_a7ce9b32` DESC | OK | 1 | 1 | 1 | 3 | 3 |
| 3 | `p_97efe693` | `chart_tokens_by_model` | Tokens by Model Version | `cd-hqCAkMPqIPw` | `bar_chart` | `qt_09afcfdb`, `qt_f8bf601a` | `qt_f8bf601a` DESC | OK | 3 | 3 | 3 | 4 | 4 |
| 3 | `p_97efe693` | `chart_embedding_reasons` | Suggested Columns by Reason | `cd-sL-0J4IkqLU` | `bar_chart` | `qt_69a5beaa`, `qt_63d8de68` | `qt_63d8de68` DESC | OK | 1 | 1 | 1 | 1 | 1 |
| 4 | `p_edf06c14` | `chart_turns_by_persona` | Turns by Persona | `cd-raUF_PPS3LM` | `bar_chart` | `qt_c4ad495e`, `qt_dd7d7d10` | `qt_dd7d7d10` DESC | OK | 1 | 1 | 1 | 4 | 4 |
| 4 | `p_edf06c14` | `chart_persona_breakdown` | Persona & Data Agent Activity | `cd-rMs1c5cC9Fw` | `table` | `qt_8070d8ac`, `qt_2b3c89df`, `qt_cbce3e14`, `qt_c1b8027d`, `qt_174cfaac` | `qt_cbce3e14` DESC | OK | 1 | 1 | 1 | 4 | 4 |
| 5 | `p_08774fec` | `chart_fast_path_pie` | Fast-Path vs Standard NL2SQL Turns | `cd-lC6BhR7QWAA` | `pie_chart` | `qt_6d704d5c`, `qt_0a6310c4` | `qt_0a6310c4` DESC | OK | 1 | 1 | 1 | 2 | 2 |
| 5 | `p_08774fec` | `chart_latency_by_fast_path` | Avg Turn Latency: Fast-Path vs Standard NL2SQL | `cd-i2nWigxiP7g` | `bar_chart` | `qt_a286c105`, `qt_cac76a3a` | `qt_cac76a3a` DESC | OK | 1 | 1 | 1 | 2 | 2 |
| 6 | `p_88bcf5f8` | `chart_errors_by_event_type` | Error Events by BQCA Event Type | `cd-oZj1Bbl96QQ` | `bar_chart` | `qt_8eff7d70`, `qt_79ac4183` | `qt_79ac4183` DESC | OK | 5 | 5 | 5 | 8 | 8 |
| 6 | `p_88bcf5f8` | `chart_errors_by_agent` | Error Events by Data Agent | `cd-tSjvZNK6aLc` | `bar_chart` | `qt_3859cfd6`, `qt_891b1a3f` | `qt_891b1a3f` DESC | OK | 1 | 1 | 1 | 3 | 3 |
| 7 | `p_b87a335e` | `tbl_prompt_response_sql` | Event-Level Prompt, Response & Extracted SQL | `cd-oUw97rpumWM` | `table` | `qt_40f9b407`, `qt_b684d2ed`, `qt_54bd6219`, `qt_7a28e588`, `qt_800b7f96`, `qt_bbf261e0`, `qt_1ada8666`, `qt_abb7920b`, `qt_a04329d9`, `qt_42c77379`, `qt_a5d93a61`, `qt_e3fa0159` | `qt_40f9b407` DESC | OK | 100 | 100 | 56,380 | 100 | 10,184 |

The five token sums differ from each other on the hydrated table, and on the
sentinel table all but the two empty ones (Thinking and Cached Tokens) do, so
a token scorecard that showed another one's value, as copies made before the
sync could, would not match. Generated SQL Queries (0 on both tables) and
Suggested Columns Proposed (null on both) match BigQuery too, but their fields
are empty in both tables, so this check shows nothing about which field they
read; the signed capture is what shows that each has its own concept on its
own field. Avg LLM Call Latency (page 3) and Avg LLM Latency (page 5) both
average `llm_latency_ms`, so they show the same value.

### Fallback paths

When a user's Workspace organization blocks external Looker Studio templates or
while the outside-account copy check of the publicly shared template is pending
(`#515`), users have two alternatives over the same 15-column BQAA base schema:

1. **Self-Hosted Streamlit BQCA Dashboard** (`dashboards/streamlit/`) — renders
   5 BQCA tabs and 9 KPI tiles locally or on Cloud Run using its own
   parameterized BigQuery SQL builders (`dashboards/streamlit/bqca_queries.py`).
2. **Bound custom query export** — `python3 tools/hydrate_dashboard.py --profile bqca --project PROJECT --dataset DATASET --custom-sql-out /tmp/bqca_events.sql`
   writes the validated single-scan BQCA query bound to the user's table.

