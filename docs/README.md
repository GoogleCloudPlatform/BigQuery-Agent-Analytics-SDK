# Design Documents

This directory contains design documents and proposals that describe the
architecture, rationale, and implementation plans behind key SDK features.

## Architecture & Vision

| Document | Description |
|----------|-------------|
| [design.md](design.md) | Original SDK architecture and design rationale |
| [prd_unified_analytics_interface.md](prd_unified_analytics_interface.md) | PRD for unified analytics interface |

## Evaluation

| Document | Description |
|----------|-------------|
| [hatteras_evaluation.md](hatteras_evaluation.md) | Hatteras-style categorical evaluation design |

## Context & Ontology

| Document | Description |
|----------|-------------|
| [context_graph_v2_design.md](context_graph_v2_design.md) | Property Graph V2 design |
| [context_graph_v3_design.md](context_graph_v3_design.md) | Property Graph V3 with GQL and world-change detection |
| [ontology_graph_v4_design.md](ontology_graph_v4_design.md) | YAML-driven ontology extraction and materialization |
| [ontology_graph_v5_design.md](ontology_graph_v5_design.md) | V5: TTL import, mixed extraction, temporal lineage |
| [learning_ontology_and_context_graph.md](learning_ontology_and_context_graph.md) | Learning guide for ontology and context graph |

## Ontology Reference

| Document | Description |
|----------|-------------|
| [ontology/ontology.md](ontology/ontology.md) | Ontology core design — logical ontology spec |
| [ontology/binding.md](ontology/binding.md) | Binding design — attaching ontology to physical tables |
| [ontology/compilation.md](ontology/compilation.md) | Compilation — resolving ontology + binding into backend DDL |
| [ontology/cli.md](ontology/cli.md) | CLI design for the `gm` tool (validate, compile, import-owl) |
| [ontology/owl-import.md](ontology/owl-import.md) | OWL import — converting OWL ontologies to YAML format |

## Deployment Surfaces

| Document | Description |
|----------|-------------|
| [proposal_bigquery_agent_cli.md](proposal_bigquery_agent_cli.md) | CLI proposal and command design |
| [python_udf_support_design.md](python_udf_support_design.md) | BigQuery Python UDF architecture |
| [remote_function_rationale.md](remote_function_rationale.md) | Cloud Run remote function rationale |
| [implementation_plan_remote_function.md](implementation_plan_remote_function.md) | Remote function implementation plan |
