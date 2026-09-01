# Deployment Guides

This directory contains six deployment surfaces for running SDK capabilities
inside Google Cloud infrastructure. For the CLI (`bq-agent-sdk`), see
[SDK.md](../SDK.md).

| Surface | Directory | Description |
|---------|-----------|-------------|
| Remote Function | [remote_function/](remote_function/) | BigQuery SQL-native access via Cloud Run |
| Python UDF | [python_udf/](python_udf/) | BigQuery Python UDF scoring kernels |
| Streaming Evaluation | [streaming_evaluation/](streaming_evaluation/) | Cloud Scheduler + Cloud Run incremental eval |
| Continuous Queries | [continuous_queries/](continuous_queries/) | Real-time BigQuery continuous query templates |
| OTLP Receiver | [otlp_receiver/](otlp_receiver/) | OTel-native receiver for Claude Code / Codex telemetry |
| Skill Evolution Job | [skill_evolution_job/](skill_evolution_job/) | Scheduled Cloud Run Job that evolves agent skills from quality reports |

Each subdirectory contains a README with setup instructions.
