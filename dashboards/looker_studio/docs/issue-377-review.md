# Issue #377 validation and resolution

Issue #377 was initially derived from the pinned Looker block snapshot in
`spec/chart_manifest.yaml`. That file is a parity/provenance contract, not a
serialization of the current Looker Studio report. On 2026-07-25, the
canonical report was reviewed directly in an authenticated Chrome session,
page by page, at report ID `5a3f85ef-fc9c-4730-8ef2-8ef9129ddb40`.
The published report was reviewed again on 2026-07-27 after PR feedback found
that the first title-only smoke test could miss incomplete chart renders.

The review keeps the source snapshot unchanged and records current product
behavior in `spec/product_contract.yaml`.

## Confirmed and fixed

- The LLM Call Volume chart used raw `timestamp` and rendered **Too Many
  Rows**. Its dimension is now `event_date`; the chart renders successfully.
- Every page now has a visible page heading.
- Every non-scorecard chart now has an explicit, title-cased title. This
  includes **Tool Completions by Agent**, whose query is intentionally scoped
  to `TOOL_COMPLETED`.
- Date controls are aligned in the upper-right. Scorecards on LLM
  Interactions, User Analytics, Latency, and Errors no longer overlap them.
- Both percentile rows are aligned four-card grids. The trend titles and
  charts sit below them without overlapping either row.
- Single-series chart legends are hidden when the nearby title already names
  the metric, so source field IDs such as `llm_response_pk`, `invocation_id`,
  and `tool_error_pk` no longer leak into the presentation.
- The four Top-5 user charts no longer group all remaining users into an
  **Others** bucket that overwhelms the named users.
- Tool Invocations, Tool Calls Over Time, and Tool Latency Over Time explicitly
  use the existing **Tool completed rows** filter. This removes blank and
  `null` tool-name series and preserves the pinned `TOOL_COMPLETED` semantics.
- User-facing copy uses “Over Time,” “LLM,” plural “Sessions,” and one `(ms)`
  unit style.
- The web configurator now has a favicon, OpenGraph/Twitter metadata,
  field-specific format guidance and error placement, a dark color scheme,
  and a copyable post-create security checklist.
- Large-table operating guidance is documented in the README.

## Already correct or not present in the live report

- Both live percentile rows were already P50 → P75 → P90 → P99. The claimed
  LLM P50 → P99 → P75 → P90 order exists only in the pinned manifest.
- The live report contained none of the empty buttons/text elements or the
  single “Agent” section header described from the LookML snapshot.
- All seven dashboard pages already used the product default of rolling 365
  days ending yesterday.
- Single-series charts use Google blue. Breakdown charts correctly use a
  categorical multi-color palette. The earlier `single_google_blue` contract
  wording was inaccurate and has been replaced with separate single-series
  and multi-series rules.
- Live legends were not centered on all 37 charts. The more important live
  problem was missing chart titles, which this change fixes.
- The live component geometry does not use the manifest's 24-column
  coordinates. Ratio, width, and height claims based only on those coordinates
  were not applied to the report.

## Filter decision

The live report has a report filter bar backed by the single `agent_events`
data source. Agent, User ID, Trace ID, Span ID, Session ID, and Model Version
are available there and apply to compatible charts.

A predefined Tool Name filter is deliberately not published yet. The current
typed schema has separate `tool_completed_name` and `tool_error_name` fields.
Using either one as a report-wide control would silently exclude the other
event family. The accepted follow-up is a unified `tool_name` field plus a
page-scoped control verified across Tool Usage, Latency, and Errors.

## Follow-up published-report verification

The second visual pass initially observed empty or degenerate rendering for
**LLM Call Volume Over Time**, **Top 5 Agents by LLM Calls**, and **Token Usage
by Agent**. A clean published reload showed their persisted bindings and data
were intact:

- LLM Call Volume uses `event_date` with distinct `llm_response_pk`;
- Top 5 Agents uses `agent` with distinct `llm_response_pk`; and
- Token Usage by Agent uses `agent` with summed `usage_total_tokens`.

This was an incomplete-render/stale-update state, not persisted field loss.
The 2026-07-27 verification therefore checks the editor bindings and requires
non-degenerate rendered data before passing; title presence alone is no longer
sufficient.

Tool Usage also retained a partial-update footer after publication even though
all three charts rendered. Running the standard viewer **Refresh data** action
cleared the footer, and a separate 30-second viewer load confirmed it did not
return.

## Valid enhancements that require separate acceptance work

The remaining ideas are useful but are not safe one-line UX fixes:

- semantic colors, value labels, and compact number formats require contrast,
  color-blind, truncation, and precision QA;
- percentile trends and hourly grain add query-cost and layout decisions;
- error rates require frozen denominator semantics;
- LLM error visibility requires explicit `LLM_ERROR` measures before a rate
  can be defined;
- estimated spend requires a maintained, user-configurable pricing source;
- Inspector drill-through requires copied-report filter propagation testing;
- true data freshness must use `MAX(timestamp)`, not Looker Studio's connector
  refresh footer; and
- Google-managed ownership requires maintainer action outside the repository.

These dependencies are executable entries in
`spec/product_contract.yaml#deferred_enhancements`; they are not represented
as completed work.
