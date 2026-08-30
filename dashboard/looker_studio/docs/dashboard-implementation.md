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
