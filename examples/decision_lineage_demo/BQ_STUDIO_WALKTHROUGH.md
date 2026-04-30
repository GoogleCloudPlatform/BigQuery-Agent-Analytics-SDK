# BigQuery Studio Walkthrough — step by step

A click-by-click guide for running the decision-lineage demo in
BigQuery Studio. Pair this with [`DEMO_NARRATION.md`](DEMO_NARRATION.md)
for the talk-track wording at each step.

## Before you start

Make sure setup has finished:

```bash
cd examples/decision_lineage_demo
./setup.sh
```

Setup:

1. Runs the live ADK media-planner agent against every campaign
   brief in `campaigns.py` (6 sessions, ~3-7 minutes total). The
   BQ AA Plugin writes every span to `agent_events`.
2. Calls `mgr.build_context_graph(...)` across every session in
   `agent_events` (two `AI.GENERATE` calls — biz nodes, then
   decisions, ~30-90s).
3. Builds `rich_agent_context_graph`, a SQL-only presentation graph
   over the canonical SDK tables.
4. Renders `bq_studio_queries.gql` next to this file with your
   project / dataset / first-session id inlined.

The demo also assumes:

- BigQuery API and Vertex AI API are enabled on the project.
- The active user / service account has
  `roles/bigquery.dataEditor`, `roles/bigquery.jobUser`, and
  `roles/aiplatform.user` (the live agent + `AI.GENERATE` both
  hit Vertex AI).
- `setup.sh` printed `property_graph_created   True` and a non-zero
  `decision_points_count`.

## Step 0 — Open BigQuery Studio (10s)

1. In a browser, open
   `https://console.cloud.google.com/bigquery?project=<YOUR_PROJECT_ID>`.
2. In the **Explorer** pane on the left, expand the
   `decision_lineage_rich_demo` dataset.
3. You should see the property graph **`rich_agent_context_graph`** in
   the dataset listing. The canonical **`agent_context_graph`** and
   seven SDK backing tables are also present. The richer graph adds
   ads-domain labels for campaign runs, agent steps, media entities,
   planning decisions, decision options, option outcomes, and drop
   reasons.

> *Optional:* click the property graph itself — Studio shows the
> schema in the details pane.

## Step 1 — Confirm the SDK populated the graph (~45s)

Run blocks 1a → 1i from `bq_studio_queries.gql` to confirm the
extraction landed on every layer of the graph.

1. **+ Compose new query** in BigQuery Studio.
2. Paste **block 1a** (CampaignRun count). **Run.** Expect 6
   campaign runs by default.
3. Paste **block 1b** (AgentStep count). **Run.** Expect an AgentStep
   count in the low hundreds — the live agent runs produced this
   many spans, deterministic given how many sessions ran.
4. Paste **block 1c** (MediaEntity count). **Run.** Expect a positive
   number; exact count varies with `AI.GENERATE`.
5. Paste **block 1d** (PlanningDecision count). **Run.** Expect a
   non-zero count — typically several per session, so a few dozen
   total across 6 sessions. Some variance.
6. Paste **block 1e** (DecisionOption count). **Run.** Expect
   roughly 3 × the PlanningDecision count.
7. Paste **blocks 1f → 1h**. **Run.** These count the richer
   presentation labels: decision categories, option outcomes, and
   drop-reason nodes.
8. Paste **block 1i** (PlanningDecisions per session). **Run.** Expect
   one row per session that actually produced decisions, with the
   per-session decision count in the right column. Note the session
   ids — Block 2 visualizes one of them (the first by default; you
   can swap in any other).

> Talk track: "Setup ran the live agent against six campaign briefs
> — every span the BQ AA Plugin wrote is here, the SDK extracted
> decisions and candidates from each session, and Block 1i shows
> how many decisions came out of each campaign run."

## Step 2 — Visualize ONE session (~75s)

The visual hook of the demo. BigQuery Studio renders graph paths as
an interactive diagram.

1. **+ Compose new query** in BigQuery Studio.
2. Copy **block 2** from `bq_studio_queries.gql` (the path query
   filtered to the first session).
3. Paste and **Run**.
4. After the query completes, click the **Graph** tab in the
   results pane (next to **Table**, **JSON**, **Execution details**).
5. The result renders as one fan-out per extracted decision in the
   chosen session — CampaignRun → PlanningDecision → DecisionOption
   → OptionOutcome.
6. Click any **DecisionOption**. The right-hand properties pane
   shows `name`, `score`, `status`, and `rejection_rationale`.
   Click a DROPPED candidate to surface the rationale.
7. Optionally run the second Block 2 query to show the
   DecisionOption → DropReason fan-out. It keeps the main graph
   readable but gives reviewers a visible "why rejected" node when
   needed.
8. To visualize a different campaign run instead, swap the
   `__SESSION_ID__` value in Block 2 with any other id Block 1i
   returned and re-run.

> Talk track: "Each fan-out is one decision. Selected and dropped
> candidates are first-class nodes with the same edge weight.
> Click any dropped node and the rationale pops up — extracted
> from raw trace text by `AI.GENERATE`."

## Step 3 — Run the EU-audit GQL (~75s)

The same GQL the SDK ships as `mgr.get_eu_audit_gql(session_id=...)`.

1. **+ Compose new query**.
2. Copy **block 3** from `bq_studio_queries.gql`. Paste and **Run**.
3. Studio shows a tabular result: one row per (decision, candidate)
   the SDK extracted for the session, with `decision_type`,
   `candidate_name`, `candidate_score`, `candidate_status`,
   `rejection_rationale`, and the linked AgentStep span info.
4. Walk the room through the table from top to bottom. Roughly
   two-thirds of rows are DROPPED (the agent's prompt asked for
   one SELECTED + two DROPPED per decision, so each decision row
   group has one selected and two rejection rationales).

## Step 4 — Just the rejections, across the portfolio (~45s)

1. **+ Compose new query**.
2. Copy **block 4** from `bq_studio_queries.gql`. Paste and **Run**.
3. Result: one row per dropped candidate **across every session**,
   ordered by session, decision, and score.
4. Optional follow-up — paste **block 4b** in a new tab and **Run**.
   That's the GQL aggregation: dropped count + average score per
   decision type, across the portfolio.

> Talk track: "Block 4 spans every session. Block 4b is the
> portfolio metric — which decision categories reject the most
> candidates and at what scores."

## Step 5 — Close calls (optional, ~40s)

1. **+ Compose new query**.
2. Copy **block 5** from `bq_studio_queries.gql`. Paste and **Run**.
3. Result: every decision where the agent picked one candidate over
   another by less than 0.05 points. The first column is the
   session, so you can drill back into Block 2 / Block 3 for any
   of these "close call" sessions.

## Step 6 — EU-compliance Q&A in BQ Conversational Analytics (optional, ~3-5 min)

Five business-shaped questions that frame the same graph as an
audit surface for the EU AI Act, GDPR, and DSA — covering right to
explanation, bias audit, human-oversight trigger, decision
reproducibility, and systemic-pattern audit.

1. Open the **Gemini / Conversational Analytics** panel in BQ
   Studio with the `decision_lineage_rich_demo` dataset selected.
2. Open [`DEMO_QUESTIONS.md`](DEMO_QUESTIONS.md) in a side editor.
3. For each Q1-Q5, paste the natural-language prompt into BQ CA,
   run, then paste the explicit GQL into a query tab and compare.
   The "regulatory anchor" line under each question names the
   article(s) the answer addresses.

## Recovering from common issues

| Symptom | Fix |
|---|---|
| Block 1d (decisions) returns 0 | `AI.GENERATE` returned no decisions; re-run `./.venv/bin/python3 build_graph.py` and then `./.venv/bin/python3 build_rich_graph.py` (no need to re-run the agent) |
| Block 1d is much smaller than 5 × session count | Some sessions had few extracted decisions; either accept it (the talk track is count-agnostic) or re-run `build_graph.py` and then `build_rich_graph.py` |
| Block 2 shows no Graph tab | Make sure the result rendered; the tab takes a second to appear after the first run |
| Block 2 has nothing to draw | The `__SESSION_ID__` baked in by `setup.sh` may not have produced decisions — swap with a different session id from Block 1i |
| "Property graph not found" | `setup.sh` did not finish; check that `build_graph.py` printed `property_graph_created True`, then rerun `./.venv/bin/python3 build_rich_graph.py` |
| `run_agent.py` fails with permission errors | Missing `roles/aiplatform.user` on the running identity (the live agent calls Vertex AI) |
| `agent_events` table is empty after `run_agent.py` | The plugin may not have flushed; re-run `./.venv/bin/python3 run_agent.py` and verify the script's "Flushing BQ AA Plugin..." line completes without warnings |

## Tear down

```bash
./reset.sh
```

Drops the dataset (graph + tables), removes the rendered
`bq_studio_queries.gql`, the `.venv`, and the `.env`. Re-run
`./setup.sh` for a clean state.
