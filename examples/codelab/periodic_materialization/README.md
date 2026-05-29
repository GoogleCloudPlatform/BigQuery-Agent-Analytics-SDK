# Codelab artifacts: periodic materialization

These are the artifacts the *Trace AI Agent Decisions with BigQuery Property Graphs* codelab uses. The codelab references them in place; you do not need to author any of them yourself.

## Contents

| File | Purpose |
|---|---|
| `property_graph.sql` | Property-graph DDL. Stitches the node and edge tables into a queryable BigQuery property graph. |
| `table_ddl.sql` | Node and edge table DDL. The materializer writes into these tables every run. |
| `ontology.yaml` | Names the entities and relationships; used by the materializer to construct the `AI.GENERATE` extraction prompt. |
| `binding.yaml` | Maps ontology entities to physical BigQuery tables and columns. Must be rendered with `envsubst` before use. |
| `seed_events.py` | Thin compatibility shim over the maintained `bqaa seed-events` command (SDK module `bigquery_agent_analytics.seed_events`). Writes a small corpus of completed decision sessions so the materializer has something to process. |

## How the codelab uses these

1. The codelab sets two shell variables: `PROJECT_ID` and `DATASET` (a single dataset that holds both raw `agent_events` and the materialized graph tables).
2. `envsubst < table_ddl.sql | bq query --use_legacy_sql=false` creates the node and edge tables.
3. `envsubst < property_graph.sql | bq query --use_legacy_sql=false` creates the property graph.
4. `envsubst < binding.yaml > binding.rendered.yaml` renders the binding once before the materializer reads it.
5. `bqaa seed-events --project-id "$PROJECT_ID" --dataset-id "$DATASET" --sessions 5` populates `agent_events` (the bundled `seed_events.py` remains as a compatibility shim if you are running from the downloaded kit).
6. `bqaa context-graph --project-id "$PROJECT_ID" --dataset-id "$DATASET" --ontology ontology.yaml --binding binding.rendered.yaml --lookback-hours 24` materializes the graph.

## Domain model

The codelab models a generic agent decision flow with three node types and two heterogeneous edges:

```
DecisionRequest --[evaluatesOption]--> DecisionOption
              \--[resultedIn]--------> DecisionOutcome
```

`DecisionRequest` is the question the agent received. `DecisionOption` is one alternative the agent considered (a request typically has several). `DecisionOutcome` records the committed choice and the rationale.

## Adapting these for your domain

For a production deployment you author your own versions of these four files describing your decision domain. The shape of each artifact stays the same:

* `property_graph.sql` — `CREATE OR REPLACE PROPERTY GRAPH ... NODE TABLES (...) EDGE TABLES (... SOURCE KEY (...) REFERENCES ... LABEL ...)`.
* `table_ddl.sql` — `CREATE TABLE IF NOT EXISTS` for each node and edge table, with the SDK metadata columns `session_id` and `extracted_at` included on every bound table.
* `ontology.yaml` — entity definitions with primary-key declarations, plus relationship definitions naming their endpoint entities.
* `binding.yaml` — per-entity mapping from ontology property names to physical column names, plus per-relationship source-and-target column declarations.
