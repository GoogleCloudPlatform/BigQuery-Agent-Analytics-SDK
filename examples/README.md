# Examples

This directory contains notebooks, SQL scripts, Python demos, and reference
artifacts that demonstrate SDK capabilities.

## Notebooks

| Notebook | Description |
|----------|-------------|
| [dashboard_v2.ipynb](dashboard_v2.ipynb) | Observability dashboard (2-layer SQL, no SDK dependency) |
| [dashboard_v2_bigframes.ipynb](dashboard_v2_bigframes.ipynb) | BigFrames companion for Dashboard V2 |
| [e2e_notebook_demo.ipynb](e2e_notebook_demo.ipynb) | End-to-end SDK workflow |
| [ai_ml_integration_demo.ipynb](ai_ml_integration_demo.ipynb) | AI.GENERATE, AI.EMBED, anomaly detection |
| [categorical_evaluation_demo.ipynb](categorical_evaluation_demo.ipynb) | Hatteras categorical evaluation |
| [context_graph_adcp_demo.ipynb](context_graph_adcp_demo.ipynb) | Agent Context Graph decision-trace use cases |
| [ontology_graph_v5_demo.ipynb](ontology_graph_v5_demo.ipynb) | OWL import, mixed extraction, temporal lineage, GQL |
| [_archive/context_graph_historical_notebook.ipynb](_archive/context_graph_historical_notebook.ipynb) | Archived: the original MAKO context-graph pipeline (explicit ontology + binding), kept as frozen evidence |
| [ontology_graph_v4_demo.ipynb](ontology_graph_v4_demo.ipynb) | Ontology extraction + GQL **(legacy)** |
| [memory_service_demo.ipynb](memory_service_demo.ipynb) | Cross-session memory |
| [event_semantics_views_bigframes_demo.ipynb](event_semantics_views_bigframes_demo.ipynb) | Event views + BigFrames |
| [nba_agent_trace_analysis_notebook.ipynb](nba_agent_trace_analysis_notebook.ipynb) | Real-world trace analysis |

## SQL — BigQuery AI Operators

| File | Description |
|------|-------------|
| [ai_classify_side_by_side.sql](ai_classify_side_by_side.sql) | AI.CLASSIFY vs AI.GENERATE comparison |
| [ai_forecast_side_by_side.sql](ai_forecast_side_by_side.sql) | AI.FORECAST vs ML.FORECAST comparison |
| [ai_similarity_validation.sql](ai_similarity_validation.sql) | AI.SIMILARITY vs AI.EMBED + ML.DISTANCE |

## SQL — Deployment Surfaces

| File | Description |
|------|-------------|
| [categorical_dashboard.sql](categorical_dashboard.sql) | Categorical metrics dashboard queries |
| [python_udf_evaluation.sql](python_udf_evaluation.sql) | UDF-based evaluation queries |
| [python_udf_eval_summary.sql](python_udf_eval_summary.sql) | UDF summary metrics |
| [python_udf_event_semantics.sql](python_udf_event_semantics.sql) | Event semantic UDFs |
| [remote_function_dashboard.sql](remote_function_dashboard.sql) | Remote function queries |
| [continuous_query_alerting.sql](continuous_query_alerting.sql) | Continuous query patterns |

## Python Scripts

| File | Description |
|------|-------------|
| [e2e_demo.py](e2e_demo.py) | Complete end-to-end workflow |
| [cli_agent_tool.py](cli_agent_tool.py) | CLI agent tool example |
| [ci_eval_pipeline.sh](ci_eval_pipeline.sh) | CI evaluation pipeline |

## Demo Bundles

| Directory | Description |
|-----------|-------------|
| [context_graph/](context_graph/) | Agent Context Graph: extract decision traces from your agent's context graph — a runnable ADK agent + BQ AA plugin streaming events, the codelab artifacts ([codelab/](context_graph/codelab/)), and the scheduled Cloud Run + Cloud Scheduler deploy ([periodic_materialization/](context_graph/periodic_materialization/)). Start with the [codelab](../docs/codelabs/periodic_materialization.md). |
| [agent_improvement_cycle/](agent_improvement_cycle/) | LoopAgent-driven prompt improvement cycle |
| [self_evolving_agent_demo/](self_evolving_agent_demo/) | Metric-driven self-evolution demo for a single ADK agent. Uses trace signals to generate and gate a bounded prompt evolution. |
| [skill_evolution_lab/](skill_evolution_lab/) | An agent that rewrites its own versioned `SKILL.md` from its conversation traces (no teacher model): flawed V0 → `evolve_skill()` → tool-first V1, golden-Q&A scored, with the anti-parroting rule and Skill Registry versioning. See the dedicated section below. |
| [decision_lineage_demo/](decision_lineage_demo/) | Decision-lineage property graph (issue #98): live ADK media-planner agent + BQ AA Plugin running across 6 campaign sessions → SDK `build_context_graph(use_ai_generate=True, include_decisions=True)` → six GQL blocks pasted into BigQuery Studio (one renders an interactive graph diagram, one is a portfolio roll-up) |

### Skill Evolution Lab — a self-improving agent

[`skill_evolution_lab/`](skill_evolution_lab/) is the runnable companion to the
blog post *"Your Agent Can Learn From Its Own Conversations."* One company-policy Q&A agent
reads its own conversation traces — successes and failures — and extracts a
structured, versioned `SKILL.md`. No teacher model, no managed optimizer.

- **The flaw with headroom.** V0 is a deliberately flawed skill (a few facts
  baked in plus *"answer only from the above, else contact HR"*) that suppresses
  a tool which already knows every answer. Only the skill is wrong — the model,
  tools, and questions stay fixed across V0 and V1, so any delta is attributable
  to the skill.
- **The engine, imported not copied.** `analyze_and_evolve.py` imports the SDK's
  reusable [`scripts/skill_evolution.py`](../../scripts/skill_evolution.py) (the
  same `evolve_skill()` the quality lab uses): it partitions scored
  conversations, runs a fleet of parallel analysts, and consolidates recurring
  rules into a new skill version.
- **Ground-truth scoring.** Quality is graded against a golden Q&A answer key
  (`eval/eval_spec.json`) via [`scripts/quality_report.py`](../../scripts/quality_report.py)
  (`--eval-spec`), not a no-ground-truth "usefulness" guess.
- **The anti-parroting rule.** Multi-turn cases where the user asserts a *wrong*
  correction; a good agent re-verifies with its tool and holds the right figure
  instead of caving. The engine detects parroting (`--tag-turns`) and learns a
  "re-verify, don't just agree" rule.
- **Skill Registry versioning.** The evolved skill is mirrored to the Gemini
  Enterprise Agent Platform Skill Registry as a new immutable revision
  (V0 = revision 1, V1 = revision 2); `reset.sh` reverts both the local copy and
  the registry to V0.

```bash
cd skill_evolution_lab
./setup.sh YOUR_PROJECT_ID us-central1   # writes .env, resets to V0
./run_e2e_demo.sh                        # V0 -> evolve -> V1 -> compare, restore V0
```

A verified run (gemini-3.5-flash, golden-grounded, 55-question held-out set):
**V0 18.2% → V1 100%** overall; corrections (anti-parroting) **0% → 100%**;
evolved skill 2.9KB. Across four models × 3 seeds, mean V1 correctness is 90–99%
per model (V0 16–53%). See the example's
[README](skill_evolution_lab/README.md) and
[VERIFICATION](skill_evolution_lab/VERIFICATION.md).

## Reference Artifacts

| File | Description |
|------|-------------|
| [e2e_demo_output.txt](e2e_demo_output.txt) | Expected output from e2e_demo.py |
| [ymgo_graph_spec.yaml](ymgo_graph_spec.yaml) | Example ontology YAML specification **(legacy)** |

> **Note:** `ontology_graph_v4_demo.ipynb`, `ontology_graph_v5_demo.ipynb`, and
> `ymgo_graph_spec.yaml` are kept for reference. The current Agent Context Graph approach
> needs none of these files: deploy your property graph to BigQuery and
> `bqaa context-graph --graph` derives everything from it — start with the
> [codelab](../docs/codelabs/periodic_materialization.md).
