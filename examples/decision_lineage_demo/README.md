# Decision Lineage with BigQuery Context Graphs

> Issue [#98](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/98) — *Unboxing the AI Agent: Decision Lineage with BigQuery Context Graphs*.

A demo where a real ADK media-planner agent runs against a portfolio
of campaign briefs, the **BigQuery Agent Analytics Plugin** captures
every span into `agent_events`, the SDK's `AI.GENERATE` extraction
pipeline pulls business entities + decisions + candidates + rejection
rationale out of the trace text, and you query the resulting
**Property Graph** with **GQL** in **BigQuery Studio**.

Everything in the graph is derived from real SDK output — there is
no hand-baked seed data anywhere on the demo path.

## Pipeline (end to end)

```
agent/      —  google.adk.agents.Agent (Gemini 2.5 Pro) + 5 tools
campaigns.py —  6 campaign briefs (Nike Summer, Nike Winter, Adidas,
                Puma, Reebok, Lululemon)
   │
   ▼
run_agent.py —  google.adk.runners.InMemoryRunner +
                google.adk.plugins.bigquery_agent_analytics_plugin.
                  BigQueryAgentAnalyticsPlugin    ←  writes spans
                                                     directly to BQ
   │
   ▼
agent_events  (TechNodes — every INVOCATION, AGENT, LLM, TOOL,
              HITL span the plugin recorded for every session)
   │
   ▼
build_graph.py —  ContextGraphManager.build_context_graph(
                       session_ids=<every session in agent_events>,
                       use_ai_generate=True,
                       include_decisions=True,
                   )                              ←  AI.GENERATE x2
   │                                                 (biz nodes,
   │                                                  decisions)
   ▼
agent_context_graph  (canonical SDK graph)
   │
   ▼
build_rich_graph.py — SQL-only presentation layer
   │                   (AgentStep, MediaEntity,
   │                    PlanningDecision, DecisionOption,
   │                    DecisionCategory, OptionOutcome, DropReason)
   ▼
rich_agent_context_graph  ←  query with GQL in BigQuery Studio
```

## What you'll show in BigQuery Studio

`bq_studio_queries.gql` (rendered by setup with project / dataset /
session inlined) holds six blocks:

| Block | Surface | Scope |
|-------|---------|-------|
| 1 | Portfolio inventory — counts across CampaignRun, AgentStep, MediaEntity, PlanningDecision, DecisionOption, DecisionCategory, OptionOutcome, DropReason, plus decisions per session | All sessions |
| 2 | Visualize ONE session's reasoning — CampaignRun → PlanningDecision → DecisionOption → OptionOutcome, plus optional DropReason fan-out | First session |
| 3 | EU-audit traversal — same shape `mgr.get_eu_audit_gql` ships, scoped to that session | First session |
| 4 | Dropped candidates — detail view across the portfolio | All sessions |
| 4b | Dropped roll-up — `COUNT(cand)` + `AVG(cand.score)` by decision type | All sessions |
| 5 | Close calls — decisions where the dropped score was within 0.05 of the selected one | All sessions |

## Setup

> **Reproducing this on a different / clean GCP project?** See
> [`SETUP_NEW_PROJECT.md`](SETUP_NEW_PROJECT.md) for the step-by-step
> guide (prerequisites, env-var overrides, post-setup verification,
> common-issue recovery, and how to swap in a different agent /
> campaign mix).

Prerequisites:

- Python 3.10+
- `gcloud` CLI authenticated (`gcloud auth application-default login`)
- A Google Cloud project with the BigQuery API and Vertex AI API enabled
- IAM: `roles/bigquery.dataEditor`, `roles/bigquery.jobUser`, `roles/aiplatform.user`

```bash
cd examples/decision_lineage_demo
export PROJECT_ID=your-gcp-project   # or rely on `gcloud config`
./setup.sh
```

`setup.sh`:

1. Verifies tooling and ADC.
2. Enables BigQuery + Vertex AI APIs.
3. Creates a per-demo `./.venv/` and installs `google-adk`,
   `google-cloud-aiplatform`, `google-cloud-bigquery`,
   `python-dotenv`, plus the SDK editable from the repo root.
4. Creates the `decision_lineage_rich_demo` dataset (regional, default
   `us-central1` so Vertex AI calls work).
5. Writes `.env` with project / dataset / model config.
6. Runs `run_agent.py` — six live ADK invocations against six
   campaign briefs (typically **3-7 minutes** depending on model
   latency). Each invocation produces an `INVOCATION_STARTING` →
   `AGENT_STARTING` → `LLM_REQUEST`/`LLM_RESPONSE` per decision →
   `TOOL_STARTING`/`TOOL_COMPLETED` per tool call →
   `INVOCATION_COMPLETED` chain in `agent_events`, and writes the
   exact session → campaign mapping into `campaign_runs`.
7. Runs `build_graph.py` — discovers every session in
   `agent_events`, calls `mgr.build_context_graph(...)`, prints
   per-session counts.
8. Runs `build_rich_graph.py` — derives demo-only presentation
   tables and creates `rich_agent_context_graph`.
9. Renders `bq_studio_queries.gql` with the first session's id
   inlined for paste-and-run.

Override defaults via env vars before `./setup.sh`:

| Var | Default | Used by |
|---|---|---|
| `PROJECT_ID` | `gcloud config get-value project` | every step |
| `DATASET_ID` | `decision_lineage_rich_demo` | BQ |
| `DATASET_LOCATION` | `us-central1` | BQ |
| `TABLE_ID` | `agent_events` | plugin + extraction |
| `DEMO_AGENT_LOCATION` | `us-central1` | live agent |
| `DEMO_AGENT_MODEL` | `gemini-2.5-pro` | live agent |
| `DEMO_AI_ENDPOINT` | `gemini-2.5-flash` | AI.GENERATE extraction |

## Running the demo

After setup:

1. Open BigQuery Studio:
   `https://console.cloud.google.com/bigquery?project=<PROJECT_ID>`
2. In the Explorer pane, expand the `decision_lineage_rich_demo` dataset.
   You should see the demo property graph
   **`rich_agent_context_graph`**. The canonical SDK graph
   **`agent_context_graph`** is also created as the source layer.
   The dataset includes the seven SDK backing tables plus derived
   demo tables such as `campaign_runs`, `rich_agent_steps`,
   `rich_decision_types`, `rich_candidate_statuses`, and
   `rich_rejection_reasons`.
3. Open `bq_studio_queries.gql` in a text editor and paste each
   block (delimited by `==` headers) into a new BQ Studio query
   tab. Run them in order while you narrate.

A talk track and a click-by-click walkthrough live alongside:

- Click-by-click steps for BigQuery Studio:
  [`BQ_STUDIO_WALKTHROUGH.md`](BQ_STUDIO_WALKTHROUGH.md)
- 5-minute leadership-pitched narrative for the EU-compliance
  Q&A (script + anticipated questions):
  [`DEMO_NARRATION.md`](DEMO_NARRATION.md)
- The five EU-compliance questions (right to explanation, bias
  audit, human oversight, reproducibility, systemic-pattern audit)
  with BQ Conversational Analytics prompts and verified GQL:
  [`DEMO_QUESTIONS.md`](DEMO_QUESTIONS.md)
- How the seven SDK backing tables feed the richer demo graph
  (`CampaignRun`, `AgentStep`, `PlanningDecision`,
  `DecisionOption`, `OptionOutcome`, and `DropReason` included),
  step by step:
  [`DATA_LINEAGE.md`](DATA_LINEAGE.md)
- Leadership-ready slide deck (Marp; renders to PDF / PPTX / HTML
  via `marp SLIDES.md --html --pdf`) covering the same 5-question
  narrative with the ads-domain vocabulary:
  [`SLIDES.md`](SLIDES.md)

## File map

```
decision_lineage_demo/
├── README.md                  # this file
├── SETUP_NEW_PROJECT.md       # reproduction guide for a fresh GCP project
├── DEMO_NARRATION.md          # 5-min leadership pitch around the 5 EU questions
├── BQ_STUDIO_WALKTHROUGH.md   # click-by-click in BQ Studio
├── DEMO_QUESTIONS.md          # 5 EU-compliance questions: BQ CA vs. direct GQL
├── DATA_LINEAGE.md            # canonical graph + richer demo graph lineage
├── SLIDES.md                  # Marp deck source — leadership / Google-Next-style
├── SLIDES.html                # rendered HTML (open in browser, no install needed)
├── SLIDES.pptx                # rendered PowerPoint (editable in Google Slides / Keynote / PPT)
├── setup.sh                   # one-shot bootstrap
├── reset.sh                   # tear down dataset + rendered files
├── render_queries.sh          # sed-renders the .gql template
├── bq_studio_queries.gql.tpl  # 6 GQL blocks (placeholders)
├── bq_studio_queries.gql      # rendered (gitignored, by setup)
├── property_graph.gql.tpl     # standalone CREATE PROPERTY GRAPH DDL (placeholders)
├── property_graph.gql         # rendered (gitignored, by setup)
├── rich_property_graph.gql.tpl # richer demo CREATE PROPERTY GRAPH DDL
├── rich_property_graph.gql    # rendered (gitignored, by setup)
├── agent/                     # the live ADK agent
│   ├── agent.py               # root_agent + bq_logging_plugin
│   ├── tools.py               # 5 decision-commit tools
│   └── prompts.py             # system prompt requiring 3 candidates
├── campaigns.py               # 6 campaign briefs
├── run_agent.py               # multi-session driver (one session
│                              # per brief, real plugin writes spans)
├── build_graph.py             # mgr.build_context_graph(...) over
│                              # every session in agent_events
└── build_rich_graph.py        # SQL-only richer graph for the demo
```

## A note on AI.GENERATE non-determinism

`AI.GENERATE` extraction is non-deterministic. Across runs you may
see:

- Slightly different rejection-rationale wording on dropped
  candidates.
- Variation in `decision_type` strings (e.g.
  `audience_selection` vs `audience_choice`).
- Occasional missed candidates if the model under-summarizes.
- Per-session decision counts that fluctuate around the prompted
  five.

The canonical SDK graph structure is stable: TechNode, BizNode,
DecisionPoint, CandidateNode + four edge labels. The demo graph is
richer but still deterministic: `build_rich_graph.py` projects
ads-domain labels such as CampaignRun / PlanningDecision /
DecisionOption / OptionOutcome / DropReason nodes
from those same tables. If a run produces zero decisions for a
session (rare), `build_graph.py` warns and you can re-run it without
re-running the agent.

## Tear down

```bash
./reset.sh
```

Deletes `DATASET_ID` (the property graph included), the rendered
queries file, the `.venv`, and the local `.env`. Re-run `./setup.sh`
to start over.

## Cost

One small dataset, six live ADK invocations (each making 5 decisions
with tool calls), plus two `AI.GENERATE` queries during build. Expect
a few cents of LLM tokens + slot time per setup run; the GQL queries
the demo runs against the prebuilt graph are near-free.
