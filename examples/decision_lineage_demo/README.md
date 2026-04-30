# Decision Lineage with BigQuery Context Graphs

> Issue [#98](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/98) — *Unboxing the AI Agent: Decision Lineage with BigQuery Context Graphs*.

A demo where the BigQuery Agent Analytics SDK consumes a media-planner
agent's traces, has `AI.GENERATE` extract decisions and the candidates
the agent considered, and persists the extracted facts as a BigQuery
**Property Graph** you query — with GQL — in **BigQuery Studio**.

The demo surface is BigQuery Studio. Setup runs once locally; the
narrated portion is four GQL blocks pasted into BQ Studio query tabs.
Block 2 returns paths and Studio renders them as an interactive
graph diagram.

## The scenario

One full media-planner agent invocation for the Nike Summer Run 2026
campaign, captured as **23 plugin-shape spans**:

- `INVOCATION_STARTING` / `AGENT_STARTING` bookends
- One user message
- **5 decisions**, each as an `LLM_REQUEST` / `LLM_RESPONSE` pair:
  audience selection, budget allocation, creative selection, channel
  strategy, scheduling
- 3 `TOOL_STARTING` / `TOOL_COMPLETED` pairs linked by
  `tool_call_id` (the LLM's function calls committing audience,
  budget, and creative through tools)
- `HITL_CONFIRMATION_REQUEST` + `_COMPLETED` for final user approval
- `AGENT_COMPLETED` + `INVOCATION_COMPLETED` close-out

LLM events carry `attributes.model`, `temperature`, `input_tokens` /
`output_tokens` and `latency_ms.{total_ms, time_to_first_token_ms}`,
the same shape the BQ AA Plugin writes in production.

Each decision LLM_RESPONSE enumerates **3 candidates** (one SELECTED,
two DROPPED with explicit rationale) — 15 candidates total, so
`AI.GENERATE` has a rich extraction surface.

## Pipeline

```
seeded LLM_RESPONSE / TOOL_COMPLETED rows
         ↓                       (Plugin shape — same schema the
   agent_events                   BQ AA Plugin writes in production)
         ↓
mgr.build_context_graph(
    use_ai_generate=True,         (AI.GENERATE → BizNodes,
    include_decisions=True,        AI.GENERATE → DecisionPoints
)                                  + Candidates,
                                   then SQL-only edge tables and
                                   CREATE OR REPLACE PROPERTY GRAPH)
         ↓
agent_context_graph  ← queried with GQL in BigQuery Studio
```

Nothing in the demo path is hand-baked downstream of the traces —
BizNodes, DecisionPoints, Candidates, and rejection rationale all
come from `AI.GENERATE` running against the seeded text.

## What you'll show in BigQuery Studio

| Block | Question | Surface |
|-------|----------|---------|
| 1 | What did the SDK extract? | Four COUNT GQL queries — one per node label. Expect 23 TechNodes plus a non-zero count for each of BizNode / DecisionPoint / CandidateNode (exact counts vary; see "AI.GENERATE non-determinism" below) |
| 2 | Visualize the agent's reasoning | Path GQL — Studio renders the diagram as fan-outs of candidates from each extracted decision |
| 3 | EU-audit traversal | The exact GQL `mgr.get_eu_audit_gql` ships — each extracted decision with its candidates and the rejection rationale on dropped ones |
| 4 | Dropped candidates | Detail view (matches `mgr.get_dropped_candidates_gql`) plus an optional Block 4b roll-up — `COUNT` and `AVG(score)` per decision type |

All four blocks run against `agent_context_graph` and live as a
single annotated bundle in `bq_studio_queries.gql` (rendered by
setup with your project / dataset / session inlined).

## Setup

Prerequisites:

- Python 3.10+
- `gcloud` CLI authenticated (`gcloud auth application-default login`)
- A Google Cloud project with the BigQuery and Vertex AI APIs enabled
- IAM: `roles/bigquery.dataEditor`, `roles/bigquery.jobUser`, `roles/aiplatform.user`

```bash
cd examples/decision_lineage_demo
export PROJECT_ID=your-gcp-project   # or rely on `gcloud config`
./setup.sh
```

`setup.sh` creates a `decision_lineage_demo` dataset, creates a
local virtualenv at `./.venv`, installs the SDK from the local
checkout into it, seeds traces, runs the SDK pipeline (two
`AI.GENERATE` calls — biz entities then decisions — taking 30-60
seconds), and renders `bq_studio_queries.gql` with values inlined.

Override defaults via env vars before `./setup.sh`:

| Var | Default |
|---|---|
| `PROJECT_ID` | `gcloud config get-value project` |
| `DATASET_ID` | `decision_lineage_demo` |
| `DATASET_LOCATION` | `US` |
| `TABLE_ID` | `agent_events` |
| `DEMO_SESSION_ID` | `sess-nike-summer-2026` |
| `DEMO_AI_ENDPOINT` | `gemini-2.5-flash` |

## Running the demo

After setup:

1. Open BigQuery Studio:
   `https://console.cloud.google.com/bigquery?project=<PROJECT_ID>`
2. In the Explorer pane, expand the `decision_lineage_demo` dataset.
   You should see seven tables plus the property graph
   `agent_context_graph`.
3. Open `bq_studio_queries.gql` in a text editor and paste each
   block (delimited by the `==` headers) into a new BQ Studio query
   tab. Run them in order while you narrate.

- Click-by-click steps for BigQuery Studio:
  [`BQ_STUDIO_WALKTHROUGH.md`](BQ_STUDIO_WALKTHROUGH.md)
- Talk track with per-block narration:
  [`DEMO_NARRATION.md`](DEMO_NARRATION.md)

## File map

```
decision_lineage_demo/
├── README.md                  # this file
├── DEMO_NARRATION.md          # talk track + timing
├── setup.sh                   # one-shot bootstrap
├── reset.sh                   # tear down dataset + rendered files
├── seed_traces.py             # INSERTs plugin-shape traces
├── build_graph.py             # mgr.build_context_graph(...) end-to-end
├── render_queries.sh          # sed-renders the .gql template
├── bq_studio_queries.gql.tpl  # GQL bundle template (placeholders)
└── bq_studio_queries.gql      # rendered (gitignored, written by setup)
```

## A note on AI.GENERATE non-determinism

`AI.GENERATE` extraction is non-deterministic. Across runs you may
see:

- Slightly different rejection-rationale wording on dropped
  candidates.
- Variation in `decision_type` strings (e.g.
  `audience_selection` vs `audience_choice`).
- Occasional missed candidates if the model under-summarizes.

The graph **structure** is stable: TechNode, BizNode, DecisionPoint,
CandidateNode + four edge labels. If a run produces zero decisions
(rare), `build_graph.py` prints a warning and you can re-run it
without re-seeding.

## Tear down

```bash
./reset.sh
```

Deletes `DATASET_ID` (the property graph included), the rendered
queries file, and the local `.env`. Re-run `./setup.sh` to start over.

## Cost

One small dataset, 23 trace rows, two `AI.GENERATE` queries during
setup. Expect well under a cent of slot time + token cost per setup
run; the GQL queries the demo runs against the prebuilt graph are
near-free.
