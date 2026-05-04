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

## BigQuery location

`--location` (e.g. `--location=US`, `--location=EU`) is forwarded to the orchestrator's BigQuery client. Required when the bound tables live outside the default location, especially in combination with `--validate-binding[-strict]` (the pre-flight validator uses the BigQuery client with the requested location to fetch each bound table's metadata).

```
bq-agent-sdk ontology-build \
  --project-id my-project \
  --dataset-id my-dataset \
  --ontology my.ontology.yaml \
  --binding my-bq-prod.binding.yaml \
  --session-ids sess-1 \
  --location EU \
  --validate-binding
```

## Pre-flight binding validation

`--validate-binding` and `--validate-binding-strict` run the binding validator before extraction, short-circuiting the build if the binding YAML drifted from the physical tables. See [binding-validation.md](binding-validation.md) for the full reference.

```
bq-agent-sdk ontology-build ... --validate-binding         # default mode
bq-agent-sdk ontology-build ... --validate-binding-strict  # strict mode
```

The two flags are mutually exclusive and incompatible with the deprecated `--spec-path` form.

## When to use `--skip-property-graph`

- **You already manage `CREATE PROPERTY GRAPH` in Terraform / dbt / a SQL file.** The SDK's `CREATE OR REPLACE PROPERTY GRAPH` would clobber your DDL on every run.
- **Your property graph definition uses DDL details the SDK compiler doesn't emit.** You hand-authored the graph DDL to express custom labels or other DDL details the SDK's compiler doesn't generate.
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

## Known limitation: `result["graph_ref"]` in split source/target setups

`build_ontology_graph(...)` accepts a single `dataset_id` and uses it both for extraction (where `agent_events` lives) and for the `graph_ref` reported in the result dict (`{project_id}.{dataset_id}.{name}`). When `--skip-property-graph` is set and the caller's actual property graph lives in `binding.target.dataset` (different from the `dataset_id` used for extraction), `result["graph_ref"]` reports the **extraction dataset**, not the user-owned graph's dataset. The materialized base tables themselves still go to `binding.target.dataset` per the resolved spec — this only affects the reported `graph_ref` string. Tracked as a follow-up; not blocking for `--skip-property-graph` itself since the user already knows where their authored graph lives.
