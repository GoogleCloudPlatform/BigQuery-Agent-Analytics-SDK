# Generate the demo data + graph in a new GCP project

End-to-end reproduction. Total wall time: **~5–10 minutes** (the
six live agent invocations dominate). Cost: a few cents of Gemini
tokens + slot time per build.

## Prerequisites

| Item | Why |
|---|---|
| **GCP project** with billing enabled | Hosts the BigQuery dataset and runs the Vertex AI calls |
| `gcloud` CLI authenticated | `gcloud auth application-default login` (one-time) |
| Python ≥ 3.10 + a working `python3` on `$PATH` | `setup.sh` creates a per-demo venv |
| **APIs**: BigQuery + Vertex AI | `setup.sh` enables both for you |
| **IAM** on the running identity | `roles/bigquery.dataEditor`, `roles/bigquery.jobUser`, `roles/aiplatform.user` |

## Step-by-step (clean project)

```bash
# 1. Clone the repo
git clone https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK
cd BigQuery-Agent-Analytics-SDK

# (While PR #99 is open, check it out:)
gh pr checkout 99
# Once merged: git checkout main && git pull

# 2. Point gcloud at the new project (or pass PROJECT_ID inline)
gcloud config set project YOUR_NEW_PROJECT_ID

# 3. Run setup
cd examples/decision_lineage_demo
./setup.sh
```

`setup.sh` performs nine steps in order:

1. Verifies `python3` and `gcloud`.
2. Enables BigQuery + Vertex AI APIs (idempotent).
3. Creates `./.venv/` and installs `google-adk`,
   `google-cloud-aiplatform`, `google-cloud-bigquery`,
   `python-dotenv`, plus the SDK editable from the repo root.
4. Creates a regional dataset (default `decision_lineage_rich_demo` in
   `us-central1` — Vertex AI requires regional, not multi-region).
5. Writes `.env` with project / dataset / model config.
6. **Runs the live ADK media-planner agent against six campaign
   briefs** (the slow step — typically 3-7 minutes; six
   invocations of Gemini 2.5 Pro). The BQ AA Plugin streams every
   span (~27 per session, ~162 total) into `agent_events`. Failures
   abort the run via a non-zero exit so setup does not proceed
   with a partial portfolio.
7. Runs `build_graph.py` — discovers every session in
   `agent_events` and calls `mgr.build_context_graph(
   use_ai_generate=True, include_decisions=True)`. Two
   `AI.GENERATE` calls (biz nodes, then decisions). ~30-90 seconds.
8. Runs `build_rich_graph.py` — creates SQL-only presentation
   tables (`campaign_runs`, `rich_decision_types`,
   `rich_candidate_statuses`, `rich_rejection_reasons`, plus edge
   tables) and creates `rich_agent_context_graph`.
9. Renders `bq_studio_queries.gql` with your project + dataset +
   first-session id inlined for paste-and-run in BQ Studio.

## Override defaults (optional)

Set any of these env vars before `./setup.sh`:

| Var | Default | Effect |
|---|---|---|
| `PROJECT_ID` | `gcloud config get-value project` | Target GCP project |
| `DATASET_ID` | `decision_lineage_rich_demo` | BigQuery dataset name |
| `DATASET_LOCATION` | `us-central1` | Must be a region (multi-region `US` will break Vertex AI calls) |
| `TABLE_ID` | `agent_events` | Plugin write target + extraction read target |
| `DEMO_AGENT_LOCATION` | `us-central1` | Where the live agent calls Vertex AI |
| `DEMO_AGENT_MODEL` | `gemini-2.5-pro` | Model the live agent uses for the 5 decisions |
| `DEMO_AI_ENDPOINT` | `gemini-2.5-flash` | Model `AI.GENERATE` uses to extract entities + decisions |

## Verify it worked

When `setup.sh` finishes, the tail of its output should include:

```
Sessions: 6 succeeded, 0 failed.
  ok  - <uuid-1>
  ok  - <uuid-2>
  ...
property_graph_created   True
decision_points_count    <non-zero>
```

Spot-check from the shell:

```bash
# Fully-qualified — replace YOUR_PROJECT_ID:
bq query --use_legacy_sql=false --location=us-central1 \
  "SELECT COUNT(*) AS spans FROM \`YOUR_PROJECT_ID.decision_lineage_rich_demo.agent_events\`"
# Expected: ~162 (6 × ~27)

bq query --use_legacy_sql=false --location=us-central1 \
  "SELECT COUNT(*) AS decisions FROM \`YOUR_PROJECT_ID.decision_lineage_rich_demo.decision_points\`"
# Expected: ~28-30 (5 per session × 6 sessions; varies with AI.GENERATE)
```

In **BigQuery Studio**:
`https://console.cloud.google.com/bigquery?project=YOUR_PROJECT_ID`
→ expand `decision_lineage_rich_demo` → confirm
`rich_agent_context_graph` shows in the Explorer. The canonical
`agent_context_graph` is also present, alongside the seven SDK
backing tables (`agent_events`, `extracted_biz_nodes`,
`context_cross_links`, `decision_points`, `candidates`,
`made_decision_edges`, `candidate_edges`) and the richer demo
projection tables (`campaign_runs`, `rich_decision_types`,
`rich_candidate_statuses`, `rich_rejection_reasons`, and their
edge tables).

## Run the demo

```bash
cat bq_studio_queries.gql      # six GQL blocks for the walkthrough
cat DEMO_QUESTIONS.md          # five EU-compliance questions
cat DEMO_NARRATION.md          # 5-minute leadership script
cat BQ_STUDIO_WALKTHROUGH.md   # click-by-click in BigQuery Studio
```

For the leadership-pitched five-minute version: open
`DEMO_NARRATION.md` and follow the script. For the click-by-click
version covering the six GQL blocks plus the optional Step 6 EU
Q&A: open `BQ_STUDIO_WALKTHROUGH.md`. Session ids in your project
will differ from the verification run — replace `<SESSION_ID>`
placeholders with ids from `setup.sh`'s output, or run Block 1i
from `bq_studio_queries.gql` to list them.

## Swap in a different scenario

The agent + campaign data are decoupled, so you can repurpose the
bundle without rewriting the SDK side:

- **Different campaigns**: edit `campaigns.py`. Each `CampaignBrief`
  becomes one ADK session. ≥1 brief works; ≥5 keeps the
  cross-session questions interesting.
- **Different decision types**: edit `agent/prompts.py` (the
  system prompt that requires "five decisions in this strict
  order") and `agent/tools.py` (one tool per decision type). The
  GQL queries auto-discover whatever `decision_type` strings
  `AI.GENERATE` assigns; nothing in the queries hard-codes the
  original five.
- **Different agent / model**: replace `agent/agent.py`'s
  `Agent(...)` definition. The plugin attachment + flush pattern
  is what matters — anything you build with
  `google.adk.agents.Agent` and run through
  `InMemoryRunner(plugins=[bq_logging_plugin])` produces a trace
  shape the SDK can extract from.

## Common issues

| Symptom | Recovery |
|---|---|
| `setup.sh` aborts at step 5 with `Sessions: N succeeded, M failed` | Read the per-campaign reason printed above. Typical causes: Vertex AI quota / permission denial (`roles/aiplatform.user`), the agent location not matching `DATASET_LOCATION`, or transient model-endpoint outages. Re-run `./setup.sh`. |
| Step 7 (`build_graph.py`) fails with `Dataset ... not found in location US` | `DATASET_LOCATION` was set to multi-region `US`. Run `./reset.sh` and rerun with `DATASET_LOCATION=us-central1` (or another region). |
| `decision_points_count` is zero | `AI.GENERATE` returned no decisions on this run. Rerun `./.venv/bin/python3 build_graph.py` (no need to rerun the agent — the traces are still in `agent_events`). |
| `rich_agent_context_graph` is missing | Rerun `./.venv/bin/python3 build_rich_graph.py`; it is SQL-only and does not call the live agent or `AI.GENERATE`. |
| `agent_events` is empty after step 6 | The plugin failed to flush. Re-run `./.venv/bin/python3 run_agent.py` and confirm the script's `Flushing BQ AA Plugin so all spans land in BigQuery...` line completes without warnings. |
| BQ Studio's Graph tab is missing on Block 2 | The result needs to render once before the tab appears; rerun the query. |

## Recreate the property graphs from existing tables

If the seven SDK backing tables are already populated (a prior
`./setup.sh` run, a clone of someone else's dataset, or a manual
INSERT-from-SELECT into a new project) you can recreate the graph
layers without rerunning the agent. Recreating the canonical graph
needs only the seven SDK tables. Recreating the rich demo graph also
needs the derived `rich_*` tables, which `build_rich_graph.py`
creates without new AI calls.

The bundle ships `property_graph.gql.tpl` — a parameterized
`CREATE OR REPLACE PROPERTY GRAPH` DDL that wires the seven
backing tables into `agent_context_graph` with the same schema the
SDK emits (same NODE TABLES, EDGE TABLES, KEY / SOURCE KEY /
DESTINATION KEY / LABEL / PROPERTIES — schema-equivalent to
`ContextGraphManager.get_decision_property_graph_ddl()` at default
config; comments, wrapping, and the trailing semicolon differ
because this file is hand-curated for paste-and-run rather than
generated).

It also ships `rich_property_graph.gql.tpl`, which wires the seven
SDK tables plus the SQL-only demo projection tables into
`rich_agent_context_graph`, the graph used by the walkthrough.

Two ways to apply it:

```bash
# Option 1 — use the rendered file written by setup.sh (or by
# render_queries.sh) and apply it with bq:
bq query \
  --use_legacy_sql=false \
  --location="${DATASET_LOCATION:-us-central1}" \
  < property_graph.gql

# Option 2 — paste property_graph.gql into a BigQuery Studio query
# tab and click Run.
```

For the richer presentation graph, first ensure the derived tables
exist, then apply `rich_property_graph.gql`:

```bash
./.venv/bin/python3 build_rich_graph.py

bq query \
  --use_legacy_sql=false \
  --location="${DATASET_LOCATION:-us-central1}" \
  < rich_property_graph.gql
```

The DDL is a single atomic `CREATE OR REPLACE PROPERTY GRAPH`
statement, so re-applying is safe. If your dataset uses non-default
table names (e.g. you overrode `TABLE_ID` or chose different
`ContextGraphConfig` table names), edit `property_graph.gql.tpl`
to match before rendering — the table names are intentionally
inlined to keep the rendered file paste-and-run.

Pre-flight check that all seven SDK tables exist:

```bash
bq query \
  --use_legacy_sql=false \
  --location="${DATASET_LOCATION:-us-central1}" \
  "SELECT table_name FROM \`${PROJECT_ID}.${DATASET_ID}\`.INFORMATION_SCHEMA.TABLES
   WHERE table_name IN ('agent_events','extracted_biz_nodes','context_cross_links',
                        'decision_points','candidates','made_decision_edges',
                        'candidate_edges')
   ORDER BY table_name"
# Expect 7 rows. If any are missing, run setup.sh / build_graph.py
# instead — the property graph DDL needs every backing table to
# exist before it will compile.
```

Pre-flight check for the richer demo graph:

```bash
bq query \
  --use_legacy_sql=false \
  --location="${DATASET_LOCATION:-us-central1}" \
  "SELECT table_name FROM \`${PROJECT_ID}.${DATASET_ID}\`.INFORMATION_SCHEMA.TABLES
   WHERE table_name IN ('campaign_runs','rich_decision_types',
                        'rich_candidate_statuses','rich_rejection_reasons',
                        'rich_campaign_span_edges','rich_campaign_decision_edges',
                        'rich_decision_type_edges','rich_candidate_status_edges',
                        'rich_candidate_reason_edges')
   ORDER BY table_name"
# Expect 9 rows. If any are missing, run build_rich_graph.py.
```

## Tear down

```bash
./reset.sh
```

Drops the dataset (graph + tables), removes `./.venv`, `.env`, and
the rendered `bq_studio_queries.gql`. Re-run `./setup.sh` for a
clean state on the same or a different project.
