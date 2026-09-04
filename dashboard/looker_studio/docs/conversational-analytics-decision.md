# Conversational Analytics over `agent_events` — decision document

Status: **evaluation complete; first-party route recommended, awaiting
maintainer acceptance and an implementation owner** (issue #402; plan
#404). The evaluation below chooses the shape; it does not claim that the
report link, provisioning asset, or user-facing capability has shipped.

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
   via the CA API, linked from the report.
3. **Stateless chat**: the shape already prototyped in
   `haiyuan-eng-google/bigquery_agent_analytics_skill#5` — CA through
   stateless chat with inline schema context per request. The comparison
   evaluates that prototype **at a pinned commit**; the PR is a moving
   branch and must not be cited unpinned.

**Two-stage evaluation gate.** Stage 1 evaluates shapes 1 and 3 — the
first-party route and the stateless prototype — against a small frozen
fixture of `agent_events` rows and a representative subset of the golden
questions, **and** against every mandatory criterion in the per-shape
matrix below. Shape 2 (the custom persistent agent) is deliberately *not*
evaluated in stage 1: it enters a stage-2 evaluation **only if the
stage-1 record names a specific first-party failure** (a mandatory
criterion the first-party route cannot satisfy — e.g. context transfer or
entry-point placement). This is the explicit trigger; without a named
failure, shape 2 is out and the pick is between shapes 1 and 3. The
recommendation must rest on observed answers and filled criteria rows,
not description.

## Evaluation record — completed 2026-08-11 UTC

A valid selection requires **both** completed parts: the run metadata *and*
the per-shape criteria matrix. A record with filled metadata but empty matrix
rows does not satisfy this gate.

**Run metadata:**

- Prototype commit evaluated (SHA):
  [`62f794fdd6e38d622235d87fc9fb438a7b029795`](https://github.com/haiyuan-eng-google/bigquery_agent_analytics_skill/tree/62f794fdd6e38d622235d87fc9fb438a7b029795/mcp-apps/bqaa-dashboard).
- First-party experience version/date evaluated: Data Studio Conversational
  Analytics **Preview**, Data Studio app version `20260802_0000`, exercised
  in an authenticated browser on 2026-08-11 UTC. The published agent appeared
  automatically under **All agents** and one question was completed through
  the real Data Studio UI.
- Frozen fixture: snapshot
  `test-project-0728-467323.bqaa_e2e_real.ca_eval_agent_events_20260810`,
  77 rows, evaluated over
  `[2026-07-27T20:06:27Z, 2026-07-27T20:30:44Z)`. Queries used the expiring
  curated view `ca_eval_agent_events_allowlist_20260810`, which exposes 14
  analysis fields and excludes raw content, prompts, user IDs, error text,
  authorization data, and trace identifiers. Both resources expire after
  30 days; they are reproducibility evidence, not production assets.
  The evaluation DataAgent `bqaa-issue-402-eval-20260810` remains published
  only for reviewer reproducibility; delete it after this PR merges and the
  #402 option pick is recorded, and do not promote it as a production asset.
- Golden-question subset: `GQ-01` through `GQ-06` (six questions): event row
  count, distinct sessions, prompt+completion tokens, p95 LLM-response
  latency, tool failure rate, and highest-volume event types with ties.
- Evidence-backed recommendation: choose **B1, the first-party BigQuery data
  agent shared to Data Studio**. Both shapes were 6/6 correct, but B1 already
  supplies discovery, consent, a persistent conversation surface, SQL/result
  inspection, and viewer-credential execution without a custom service or UI.
  Report date/filter context does not transfer automatically and must be made
  explicit in the link-out UX.
- Named first-party failure triggering stage 2: **none**. Manual re-entry of
  report filters is a documented flow constraint, not a criterion B1 cannot
  satisfy. Shape 2 therefore remains out of scope.
- Evaluator + date: authenticated test-project run by `@caohy1988`, assisted
  by Codex, 2026-08-11 UTC.

**Per-question results:** each row used the same fixture, bounds, and 10 MiB
per-generated-query cap. "SQL sane" means the generated SQL referenced only
the curated view, included a timestamp predicate, and avoided excluded raw
fields. Latency is end-to-end API wall time; it is directional evidence, not
a performance SLO.

| ID | Expected result | 1. First-party | 3. Stateless prototype |
| --- | --- | --- | --- |
| GQ-01 | 77 event rows | Correct; SQL sane; 14.5 s | Correct; SQL sane; 19.5 s |
| GQ-02 | 7 distinct sessions | Correct; SQL sane; 14.4 s | Correct; SQL sane; 16.2 s |
| GQ-03 | 1,703 prompt+completion tokens; thinking excluded | Correct; SQL sane; 22.4 s | Correct; SQL sane; 19.7 s |
| GQ-04 | 2,562 ms p95 LLM-response latency | Correct; SQL sane; 18.5 s | Correct; SQL sane; 19.7 s |
| GQ-05 | 7 runs, 0 failures, 0% | Correct; SQL sane; 18.4 s | Correct; SQL sane; 18.3 s |
| GQ-06 | `LLM_REQUEST` and `LLM_RESPONSE`, 12 each | Correct; SQL sane; 33.3 s | Correct; SQL sane; 24.9 s |

The authenticated Data Studio GQ-01 run separately showed the visible
`Analyzing` -> query-completed -> answer/SQL/result lifecycle in about 15
seconds and created a returnable Recent conversation. The exact pinned
stateless implementation also returned 1,703 for GQ-03 with
`scope.verified: true`; its `ca.test.mjs` suite passed 24/24.

**Per-shape criteria matrix** — one evidence cell per mandatory criterion
per evaluated shape. Every cell must cite observed behavior or a primary
document, not an assumption:

| Mandatory criterion | 1. First-party | 3. Stateless prototype | 2. Custom agent (stage 2 only) |
| --- | --- | --- | --- |
| Viewer flow & context transfer | **Pass with an explicit boundary.** Authenticated observation: a published BigQuery agent automatically appeared on Data Studio's Chat with your data page; its detail panel exposed project, knowledge source, labels, publish time, and a copy-link control. A report link can carry project+agent and open a new tab, but report date/agent filters do not transfer and must be shown for re-entry. This matches the documented [Data Studio agent flow](https://docs.cloud.google.com/data-studio/conversational-analytics-data-agents). | **Pass, more repo work.** The companion can accept table/window/filter parameters and keep a report-return link, but requires deploying and authenticating the pinned [web/MCP surface](https://github.com/haiyuan-eng-google/bigquery_agent_analytics_skill/blob/62f794fdd6e38d622235d87fc9fb438a7b029795/mcp-apps/bqaa-dashboard/DESIGN.md). | — |
| Path states (all five) | **Defined.** Provisioned: agent card. Unprovisioned: empty Recent/agent-discovery state. Permission-denied: inactive card with the missing grant named by Data Studio. First-question: one-time interaction disclosure, then prompt. Return: Recent conversation; the report stays open in the originating tab. The first, third, and fourth states are also described in [Converse with Data Studio data](https://docs.cloud.google.com/data-studio/conversational-analytics-data). | **Defined in the pinned surface.** Provisioned: authenticated Ask UI. Unprovisioned: CA-disabled/configuration error. Permission-denied: HTTP/API error without partial answer. First-question: bounded Ask request. Return: host or report link. These states are owned by this repo/service rather than Data Studio. | — |
| Executing principal & IAM grants | **Pass.** Data Studio disclosed that the agent uses the viewer's credentials. Required grants are agent-level `roles/geminidataanalytics.dataAgentUser`, BigQuery data access plus `bigquery.jobs.create`, and `cloudaicompanion.topics.create` through `roles/cloudaicompanion.user` or `roles/bigquery.studioUser`; see [BigQuery agent sharing](https://docs.cloud.google.com/bigquery/docs/create-data-agents#share_data_studio) and [CA IAM](https://docs.cloud.google.com/gemini/data-agents/conversational-analytics-api/access-control). This composes directly with Viewer's Credentials. | **Pass with a different principal.** The deployed service uses its runtime service account/ADC with `roles/geminidataanalytics.dataAgentStatelessUser`, BigQuery read, and job creation; the caller separately authenticates to the companion. That is an additional trust surface. | — |
| Data egress from BigQuery | **Pass with the curated view.** The question, agent context/schema, generated SQL, and aggregate result transit Google's CA/Data Studio services; the evaluated view made raw content and identifiers unavailable to the datasource. The UI warns that generated queries are visible to project administrators. | **Pass with an extra hop.** The same CA material also traverses the custom web/MCP service and up to three bounded history exchanges are resent. The prototype limits displayed rows to 100, but service/host logging must be configured as part of deployment. | — |
| Retention / residency / auditability | **Pass with deployment choices recorded.** Data Studio persisted the conversation and exposed Recent/delete controls. DataAgent and Conversation state is covered by [CA data residency](https://docs.cloud.google.com/gemini/data-agents/conversational-analytics-api/data-residency); the test used `global`, which has no residency commitment, so production must select `us`, `eu`, or an approved regional endpoint when required. CA emits Admin Activity and Data Access logs per its [security and audit contract](https://docs.cloud.google.com/gemini/data-agents/conversational-analytics-api/security-privacy-compliance). | **Pass only if host retention is specified.** Inline chat does not create a managed Conversation; the caller resends bounded history. CA endpoint residency still applies, while Cloud Run/MCP host request logs, retention, deletion, and audit access become repo/operator responsibilities. | — |
| Allowlist enforcement (IAM-side) | **Pass as a required provisioning contract.** Evaluation used a curated view, but the evaluator retained broader test-project access. Production viewers must receive BigQuery access to the view only; agent IAM controls agent access, not underlying BigQuery access ([CA IAM](https://docs.cloud.google.com/gemini/data-agents/conversational-analytics-api/access-control)). Agent instructions/table selection are defense in depth, not the boundary. | **Pass as a required service-account contract.** Configure the runtime service account with view-only BigQuery access. The pinned prototype's table configuration and prompt checks are useful defense in depth but cannot replace IAM. | — |
| Cost controls & quotas | **Pass.** Published and staging contexts used `big_query_max_billed_bytes=10485760`; every scored query completed under it. Production also needs project/user BigQuery quotas and awareness of the 30 chat requests/minute project/user limits; see [CA cost controls](https://docs.cloud.google.com/gemini/data-agents/conversational-analytics-api/manage-costs) and [quotas](https://docs.cloud.google.com/gemini/data-agents/conversational-analytics-api/quotas). | **Pass.** Same 10 MiB cap; the pinned server adds a 150-second whole-request deadline and three-request concurrency admission cap. BigQuery and CA project/user quotas remain mandatory because the byte cap is per generated query, not per question. | — |
| Answer lifecycle & accessibility | **Pass.** Observed keyboard-addressable tabs, agent cards, consent controls, prompt textbox, send/stop controls, details, SQL, result table, success text, and persistent conversation. Loading and query-complete states were announced in text. Permission/no-data/error states remain product-owned and must be included in release QA. | **Pass at the pinned commit.** The UI supplies loading, success, table fallbacks, errors, cancellation, keyboard operation, and screen-reader output; the pinned [CA tests](https://github.com/haiyuan-eng-google/bigquery_agent_analytics_skill/blob/62f794fdd6e38d622235d87fc9fb438a7b029795/mcp-apps/bqaa-dashboard/tests/ca.test.mjs) show parser/scope validation failing closed without an uncertified partial result. Deployments must preserve these tests. | — |
| Sharp edges encoded in agent context | **Pass.** The published evaluation context explicitly says prompt+completion is `prompt_token_count + candidates_token_count` with thinking separate, and that zero-latency local tool events are valid. GQ-03 returned 1,703 correctly. The production agent asset must retain both statements. | **Gap in the pinned commit.** It describes token and latency fields but does not encode either #382 rule explicitly. The explicit GQ-03 wording produced the right result, but that does not satisfy the context contract; adopting this shape would require an asset/code change before ship. | — |

The stage-2 column stays "—" unless the named-failure trigger fires; it
must then be filled completely before shape 2 can be recommended.

## Recommendation and implementation boundary

Select **B1 (first-party BigQuery data agent -> Data Studio)**. There is no
named first-party mandatory failure, so the custom persistent-agent stage is
not triggered. The stateless prototype remains a useful implementation for
#396 and a fallback if the first-party route later fails a mandatory
criterion, but it does not justify adding a custom service to this feature.

This decision does **not** close #402 by itself. Closure requires either the
capability to ship or a maintainer to accept a named implementation handoff.
The implementation handoff must own all of the following:

1. a versioned agent-definition/golden-question artifact that preserves the
   curated view, timestamp, cost, and #382 sharp-edge contracts;
2. a provisioning guide or command for the view, least-privilege IAM, agent,
   publish/share operation, and approved regional endpoint;
3. report and configurator links that open Data Studio in a new tab, make
   the non-transferred date/agent filters explicit, and set the expectation
   that this evaluation's answers took 14.4–33.3 seconds (directional, not a
   performance SLO); and
4. tests and a named owner for compatibility with `events_v1` and Data
   Studio's Preview surface.

## Decision criteria (every evaluated shape must answer all of these)

- **Viewer flow.** How a report viewer reaches the surface; whether
  project/dataset/table, the selected date window, and agent-filter context
  transfer or must be re-entered.
- **Path states.** Provisioned, unprovisioned, permission-denied,
  first-question, and return-to-report — each defined, none left implicit.
- **Executing principal and IAM.** Which principal executes the query or
  service call (end user, service account, or shared agent identity), the
  exact grants required, and how that composes with the dashboard's
  Viewer's Credentials posture.
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

## Shared asset with #396 (MCP app) — selected implementation dependency

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

Building the artifact and its tests is the next implementation step after a
maintainer accepts the B1 recommendation and names its owner.
