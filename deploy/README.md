# Deployment Guides

This directory contains four deployment surfaces for running SDK capabilities
inside Google Cloud infrastructure. For the CLI (`bq-agent-sdk`), see
[SDK.md](../SDK.md).

| Surface | Directory | Description |
|---------|-----------|-------------|
| Remote Function | [remote_function/](remote_function/) | BigQuery SQL-native access via Cloud Run |
| Python UDF | [python_udf/](python_udf/) | BigQuery Python UDF scoring kernels |
| Streaming Evaluation | [streaming_evaluation/](streaming_evaluation/) | Cloud Scheduler + Cloud Run incremental eval |
| Continuous Queries | [continuous_queries/](continuous_queries/) | Real-time BigQuery continuous query templates |

Each subdirectory contains a README with setup instructions.
