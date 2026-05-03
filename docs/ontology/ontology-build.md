# `bq-agent-sdk ontology-build` — End-to-End Orchestrator

`bq-agent-sdk ontology-build` runs the SDK's full ontology pipeline end-to-end against a populated `agent_events` table:

1. Load the spec (`--ontology X.yaml --binding Y.yaml`).
2. Extract an `ExtractedGraph` from agent telemetry via `AI.GENERATE`.
3. Create physical entity/relationship tables (`CREATE TABLE IF NOT EXISTS`).
4. Materialize extracted nodes/edges into those tables.
5. Run `CREATE OR REPLACE PROPERTY GRAPH` to wire the BigQuery property graph object.

The Python entry point is `bigquery_agent_analytics.ontology_orchestrator.build_ontology_graph(...)`. The CLI is a thin wrapper.

## Skipping property-graph DDL

Use `--skip-property-graph` when **the caller owns their own `CREATE PROPERTY GRAPH` DDL** — e.g., the property graph is provisioned via Terraform, dbt, or hand-authored SQL — and only wants the SDK to populate base tables.

```
bq-agent-sdk ontology-build \
  --project-id my-project \
  --dataset-id my-dataset \
  --ontology my.ontology.yaml \
  --binding my-bq-prod.binding.yaml \
  --session-ids sess-1,sess-2 \
  --skip-property-graph
```

Behavior with the flag set:

- Phase 5 short-circuits. No `OntologyPropertyGraphCompiler` is constructed, no `CREATE OR REPLACE PROPERTY GRAPH` job runs. The user's existing graph object is unchanged.
- Phases 1–4 run normally. Tables are created (`CREATE TABLE IF NOT EXISTS` is a no-op against pre-existing tables) and rows are materialized.
- The CLI exits 0.
- The output dict reports:

  ```json
  {
    "property_graph_created": false,
    "property_graph_status": "skipped:user_requested",
    ...
  }
  ```

  JSON consumers should read `property_graph_status` (not just `property_graph_created`) to distinguish a deliberate skip from a creation failure.

## Status field reference

The CLI's `property_graph_status` field has three values:

| `property_graph_status` | `property_graph_created` | Exit code | Meaning |
|---|---|---|---|
| `"created"` | `true` | 0 | Phase 5 ran and BigQuery confirmed the graph object. |
| `"failed"` | `false` | 1 | Phase 5 ran but the graph object was not created. The CLI prints "Property Graph creation failed" to stderr. Tables and rows were still materialized. |
| `"skipped:user_requested"` | `false` | 0 | `--skip-property-graph` was set. Phase 5 did not run. No error message. |

Without `--skip-property-graph`, the existing exit-1 behavior on graph-create failure is preserved exactly.

## When to use this

- **You already manage `CREATE PROPERTY GRAPH` in Terraform / dbt / a SQL file.** The SDK's `CREATE OR REPLACE PROPERTY GRAPH` would clobber your DDL on every run.
- **Your property graph definition uses options the SDK doesn't generate.** You hand-authored the graph DDL to express features (custom labels, additional indexes, dialect-specific options) the SDK's compiler doesn't emit.
- **You want to populate your tables on a different cadence than you redefine the graph.** The graph definition rarely changes; the data is refreshed continuously.

For all other cases, leave the flag off and let the SDK manage the property graph end-to-end.

## Python API

The flag is also available on `build_ontology_graph(...)`:

```python
from bigquery_agent_analytics.ontology_orchestrator import build_ontology_graph

result = build_ontology_graph(
    spec=resolved_spec,
    session_ids=["sess-1"],
    project_id="my-project",
    dataset_id="my-dataset",
    skip_property_graph=True,  # phase 5 skipped
)

assert result["property_graph_status"] == "skipped:user_requested"
assert result["skipped_reason"] == "user_requested"
assert result["property_graph_created"] is False
```

`skipped_reason` is only present when the phase was skipped; it is omitted when phase 5 ran (whether or not it succeeded).
