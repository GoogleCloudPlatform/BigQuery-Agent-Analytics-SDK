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

Setup writes `bq_studio_queries.gql` next to this file with your
project / dataset / session inlined. Keep that file open in a side
editor — you'll copy each block out of it during the demo.

The demo also assumes:

- BigQuery API and Vertex AI API are enabled on the project.
- The active user / service account has
  `roles/bigquery.dataEditor`, `roles/bigquery.jobUser`, and
  `roles/aiplatform.user`.
- `setup.sh` printed `property_graph_created   True` and a non-zero
  `decision_points_count` (5 expected; it can vary slightly with
  `AI.GENERATE` non-determinism — re-run `python3 build_graph.py`
  if the count is zero).

## Step 0 — Open BigQuery Studio (10s)

1. In a browser, open
   `https://console.cloud.google.com/bigquery?project=<YOUR_PROJECT_ID>`.
2. In the **Explorer** pane on the left, expand the
   `decision_lineage_demo` dataset.
3. You should see the property graph **`agent_context_graph`** in
   the dataset listing alongside seven backing tables
   (`agent_events`, `extracted_biz_nodes`, `context_cross_links`,
   `decision_points`, `candidates`, `made_decision_edges`,
   `candidate_edges`).

> *Optional:* click the property graph itself — Studio shows the
> schema in the details pane. Use this to anchor the demo: "this is
> the graph we'll query in a moment."

## Step 1 — Confirm the SDK populated the graph (~30s)

This step is the "is anything actually here?" check. Four tiny GQL
queries, each counts one node label in the session.

1. In the BigQuery Studio top bar, click **+ Compose new query**
   (or hit `Cmd / Ctrl + Enter` after pasting).
2. Open `bq_studio_queries.gql` in your editor and copy
   **block 1a** (TechNode count). Paste it into the query tab.
3. Click **Run**. Expect `techNodes = 23`.
4. Replace the query with **block 1b** (BizNode count) and run.
   Expect a positive number — typically 6-12, depending on what
   `AI.GENERATE` extracts.
5. Replace with **block 1c** (DecisionPoint count) and run. Expect
   `decisions ≈ 5`.
6. Replace with **block 1d** (CandidateNode count) and run. Expect
   `candidates ≈ 15`.

> Talk track: "Setup ran the SDK's `build_context_graph` against 23
> seeded plugin-shape spans. `AI.GENERATE` did the rest — five
> decisions extracted, fifteen candidates pulled out with rationale."

## Step 2 — Visualize the agent's reasoning (~80s)

The visual hook of the demo. BigQuery Studio renders graph paths as
an interactive diagram.

1. **+ Compose new query** in BigQuery Studio.
2. Copy **block 2** from `bq_studio_queries.gql` (the path query
   `MATCH p = (s:TechNode)-[:MadeDecision]->(dp:DecisionPoint)-[:CandidateEdge]->(c:CandidateNode)`).
3. Paste and **Run**.
4. After the query completes, Studio shows results in the bottom
   pane. Click the **Graph** tab (next to **Table**, **JSON**,
   **Execution details**).
5. The result renders as five fan-outs — one per decision —
   branching from the LLM_RESPONSE TechNode, through the
   DecisionPoint, out to three CandidateNode endpoints.
6. Click any **CandidateNode**. The right-hand properties pane
   shows `name`, `score`, `status`, and `rejection_rationale`.
   Click a DROPPED candidate (e.g. `Casual fitness enthusiasts`,
   `TikTok Spark Ads`, `YouTube Shorts`) to surface the rationale.

> Talk track: "Each fan-out is one decision. The selected candidate
> sits next to the dropped ones with the same edge weight — the
> graph treats rejections as first-class facts. Click a dropped
> node and the rationale pops up: this is what the agent reasoned
> through, extracted from raw trace text by `AI.GENERATE`."

## Step 3 — Run the EU-audit GQL (~80s)

The same GQL the SDK ships as `mgr.get_eu_audit_gql(session_id=...)`.

1. **+ Compose new query**.
2. Copy **block 3** from `bq_studio_queries.gql`. Paste and **Run**.
3. Studio shows a tabular result: one row per (decision, candidate)
   with `decision_type`, `candidate_name`, `candidate_score`,
   `candidate_status`, `rejection_rationale`, and the linked TechNode
   span info.
4. Walk the room through the table from top to bottom — five
   decisions, three candidates each, ten of fifteen are DROPPED.

> Talk track: see the per-decision narration in
> [`DEMO_NARRATION.md`](DEMO_NARRATION.md), Block 3 section. Read
> off two or three rejection rationales aloud — the wording is the
> demo: "channel overlap on the same demographic; Q1 retention 18%
> lower at matched spend."

## Step 4 — Operational view: just the rejections (~45s)

1. **+ Compose new query**.
2. Copy **block 4** from `bq_studio_queries.gql`. Paste and **Run**.
3. The result is ten rows — every dropped candidate, ordered by
   decision and score.

> Talk track: "Ten rejections, two per decision. Drop the
> `session_id` filter and pivot on a date range and you have a
> portfolio metric — across every agent run last quarter, which
> decision categories reject the most candidates and at what
> scores. That's the line product teams want to track."

## Step 5 (optional) — Improvise an ad-hoc query (~30s)

Useful if the audience asks "can you find X?". Two starter queries
you can adapt live:

**5a. Most-rejected decision type:**

```sql
GRAPH `<PROJECT>.<DATASET>.agent_context_graph`
MATCH (dp:DecisionPoint)-[ce:CandidateEdge]->(c:CandidateNode)
WHERE ce.edge_type = 'DROPPED_CANDIDATE'
RETURN dp.decision_type, COUNT(c) AS rejected
GROUP BY dp.decision_type
ORDER BY rejected DESC;
```

**5b. Decisions where the top dropped candidate was within 0.05
of the selected one:**

```sql
GRAPH `<PROJECT>.<DATASET>.agent_context_graph`
MATCH
  (dp:DecisionPoint)-[:CandidateEdge]->(sel:CandidateNode),
  (dp)-[:CandidateEdge]->(drop:CandidateNode)
WHERE sel.status = 'SELECTED'
  AND drop.status = 'DROPPED'
  AND sel.score - drop.score < 0.05
RETURN dp.decision_type, sel.name, sel.score, drop.name, drop.score
ORDER BY sel.score - drop.score ASC;
```

(Substitute `<PROJECT>` / `<DATASET>` to match your environment, or
copy from the rendered `bq_studio_queries.gql`.)

## Recovering from common issues

| Symptom | Fix |
|---|---|
| Block 1c returns `decisions = 0` | `AI.GENERATE` non-determinism; re-run `python3 build_graph.py` (no need to re-seed traces) |
| Block 2 shows no Graph tab | Make sure the result rendered — sometimes the tab takes a second to appear after the first run; or re-run the query |
| "Property graph not found" | `setup.sh` did not finish; check that `build_graph.py` printed `property_graph_created True` |
| Permission errors on `AI.GENERATE` | Missing `roles/aiplatform.user` on the running identity |

## Tear down

```bash
./reset.sh
```

Drops the dataset (graph + tables) and removes the rendered
`bq_studio_queries.gql` and `.env`. Re-run `./setup.sh` for a clean
state.
