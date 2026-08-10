# Conversational Analytics over `agent_events` — decision document

Status: **awaiting product decision** (issue #402; plan #404 rev 5.1).
Nothing in this document is implemented; it defines the options, the
criteria a selection must satisfy, and the evaluation that must happen
before selecting.

## Request and constraint

Field feedback asked for natural-language Q&A over the same telemetry the
37 charts read — the Looker demo experience (Gemini in Looker plus the
Agent Analytics block). Looker Studio has no **in-report** conversational
pane, so this cannot be a straight port; it needs a product decision first.

**However, the first-party landscape has moved and the decision must
account for it:** Looker Studio (Data Studio) now ships a first-party
[Conversational Analytics experience](https://docs.cloud.google.com/data-studio/conversational-analytics-overview)
— in Preview, available to all Data Studio users (Code Interpreter remains
Pro/Gemini-gated) — that chats with **data agents created in BigQuery and
shared to Data Studio**; agents cannot be created inside Data Studio
itself. See also the
[setup contract](https://docs.cloud.google.com/data-studio/conversational-analytics-setup)
(names `bigquery.jobs.create`, `roles/bigquery.dataViewer`, and the
separate agent/conversation permissions) and the
[data agents page](https://docs.cloud.google.com/data-studio/conversational-analytics-data-agents).
This is best understood as the concrete first-party implementation of
Option B's persistent-agent shape, not a fourth top-level option.

## Primary decision: A, B, or C

**A. In-report chat tile (community visualization + Conversational
Analytics API).** Rejected for v1. Third-party community visualizations
require Owner's Credentials data sources, while this report's security
contract requires generated copies to run on Viewer's Credentials with a
manual share gate (`dashboard-implementation.md`). A repo-hosted
visualization is third-party unless it passes the separate Google-built
approval path. Secondary cost: the report attests
`community_visualizations: not_used`, the property the rendering-triage
protocol (#381/#383) is built on; reversing it reopens that protocol for a
chat tile.

**B. Companion Conversational Analytics surface, linked from the report
and the configurator.** Recommended. Works for every user of this repo, no
hosting or embedding problem, and the schema/golden-question assets are
reusable (#396).

**C. Document the Looker path.** For teams already on Looker: the Agent
Analytics block plus Gemini in Looker is literally the demo. Zero build.
Ships regardless of the A/B decision — see the README's "Already on
Looker?" section added alongside this document.

## Nested decision (only if B): three candidate shapes

To be compared — not assumed:

1. **First-party route (new baseline)**: create the data agent in BigQuery
   over the user's `agent_events` table and share it to Data Studio's
   built-in Conversational Analytics experience. Potentially removes the
   custom provisioning surface and companion UI this document previously
   implied. Must be evaluated on: Preview status and its limited-support
   terms; the publish/share flow (who shares, to whom); the IAM contract
   from the setup page; whether report context (table, date window,
   filters) transfers; retention/auditability; and what remains for this
   repo to build (agent definition + golden questions + provisioning
   path only).
2. **Custom persistent CA data agent**: provisioned once per installation
   via the CA API, linked from the report. Now justified only where the
   first-party route falls short (e.g. context transfer or entry-point
   placement).
3. **Stateless chat**: the shape already prototyped in
   `haiyuan-eng-google/bigquery_agent_analytics_skill#5` — CA through
   stateless chat with inline schema context per request. The comparison
   evaluates that prototype **at a pinned commit**; the PR is a moving
   branch and must not be cited unpinned.

**Pre-selection evaluation:** run the pinned prototype and the first-party
route against a small frozen fixture of `agent_events` rows and a
representative subset of the golden questions. The recommendation must
rest on observed answers, not description.

## Evaluation record — REQUIRED before the option pick (currently empty)

The selection cannot happen until this section is filled in. Slots:

- Prototype commit evaluated (SHA): _pending_
- First-party experience version/date evaluated: _pending_
- Frozen fixture (table snapshot id + row count + date range): _pending_
- Golden-question subset (IDs and count): _pending_
- Per-question results (answer correct? SQL sane? latency): _pending_
- Evidence-backed recommendation: _pending_
- Evaluator + date: _pending_

## Decision criteria (both B-shapes must answer all of these)

- **Viewer flow.** How a report viewer reaches the surface; whether
  project/dataset/table, the selected date window, and agent-filter context
  transfer or must be re-entered.
- **Path states.** Provisioned, unprovisioned, permission-denied,
  first-question, and return-to-report — each defined, none left implicit.
- **Caller identity and IAM.** Who calls the CA API (end user vs service
  account), the exact grants required, and how that composes with the
  dashboard's Viewer's Credentials posture.
- **Data egress.** Exactly what telemetry, schema, and query text leaves
  BigQuery in each request; conversation persistence, retention, deletion,
  data residency, and auditability.
- **Field allowlist — with enforcement.** A curated allowlist that excludes
  raw content and authorization details by default. Per the CA
  known-limitations documentation, configured table selection is **not a
  security boundary** when the caller holds broader permissions — so
  enforcement must be IAM-side (a dedicated service account, an authorized
  view, or column-level access control), never agent-config-side.
- **Cost controls.** `big_query_max_billed_bytes` set on CA-issued queries,
  plus documented per-user/per-project quota expectations from the CA
  cost-control documentation.
- **Answer lifecycle and accessibility.** Loading, success, no-data,
  partial-result, error, cancellation/retry, keyboard operation, and
  screen-reader output for whichever surface links out.
- **Sharp edges in agent context.** The #382 confusions must be encoded in
  the agent's context so NL answers do not repeat them: thinking tokens are
  not part of prompt+completion totals, and zero-latency local tool calls
  are real events, not data errors.

## Shared asset with #396 (MCP app) — specified here, built after the decision

One canonical, versioned artifact (JSON or YAML), built once and consumed
by both this feature and the #396 MCP app:

- BQAA schema description, including the JSON extraction conventions
  (`attributes.usage_metadata.*`, `latency_ms.total_ms`, `content.tool` /
  `tool_origin`, `model_version`, …).
- Golden questions derived from the 37 charts, each with expected SQL and
  expected result semantics — usable as in-product examples and as an eval
  set.
- A declared host-neutral core vs per-host adapter split (Looker Studio
  link-out vs MCP host).
- A named owner and a compatibility-test plan tying the artifact version to
  the `events_v1` schema version.

Building the artifact and its tests is implementation work, gated on the
product decision above.
