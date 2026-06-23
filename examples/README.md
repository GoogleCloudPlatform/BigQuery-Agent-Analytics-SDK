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
| [decision_lineage_demo/](decision_lineage_demo/) | Decision-lineage property graph (issue #98): live ADK media-planner agent + BQ AA Plugin running across 6 campaign sessions → SDK `build_context_graph(use_ai_generate=True, include_decisions=True)` → six GQL blocks pasted into BigQuery Studio (one renders an interactive graph diagram, one is a portfolio roll-up) |

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
