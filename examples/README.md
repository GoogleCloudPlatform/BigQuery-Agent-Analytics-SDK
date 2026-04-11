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
| [context_graph_adcp_demo.ipynb](context_graph_adcp_demo.ipynb) | Property Graph use cases |
| [ontology_graph_v4_demo.ipynb](ontology_graph_v4_demo.ipynb) | Ontology extraction + GQL |
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

## Reference Artifacts

| File | Description |
|------|-------------|
| [e2e_demo_output.txt](e2e_demo_output.txt) | Expected output from e2e_demo.py |
| [ymgo_graph_spec.yaml](ymgo_graph_spec.yaml) | Example ontology YAML specification |
