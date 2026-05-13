# Migration v5 Demo — Fixture Foundation

**Status:** Phase 1 of [issue #107](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/107) — fixtures only. The four-guarantee notebook (`examples/migration_v5_demo_notebook.ipynb`) is a follow-up commit on this branch.

Per the [round-2 product clarification](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/pull/155#issuecomment-4437670647), the demo's event source of truth is **a runnable agent talking to the BQ AA plugin**, not a hand-coded event generator. This directory's authored inputs are split accordingly.

## Authorship boundary

| File | Authored? | What it does |
|------|-----------|--------------|
| `mako_core.ttl` | **Authored.** | The real MAKO ontology, pulled from the [reference gist](https://gist.github.com/haiyuan-eng-google/a69ff6282ebcc877f77f9aa4e3db1afd). Domain-agnostic decision semantics for Yahoo Monetization Platform. |
| `mako_artifacts.py` | **Authored.** | Pure-Python pipeline: imports the TTL → resolves `FILL_IN` primary keys → drops dangling cross-namespace relationships → generates ontology / binding / table DDL / property-graph SQL for any `(project, dataset)`. **Does not generate events.** |
| `mako_demo_agent.py` | **Authored.** | Runnable ADK agent + `BigQueryAgentAnalyticsPlugin` wiring. Defines five MAKO decision-flow tools (`capture_context`, `propose_decision_point`, `evaluate_candidate`, `commit_outcome`, `complete_execution`) and a system prompt that walks the agent through them. Real plugin traces land in `agent_events` when the agent runs. |
| `run_agent.py` | **Authored.** | Driver. `python run_agent.py --sessions 50 --project X --dataset Y` runs the agent for N sessions and lets the plugin populate `agent_events`. |
| `export_events_jsonl.py` | **Authored.** | Optional. Exports a pinned subset of `agent_events` to a local JSONL file for the notebook's deterministic offline revalidation tests. Not an event generator — it reads from BigQuery. |
| `ontology.yaml` | **Generated** by `mako_artifacts.regenerate_snapshots()`. | TTL-import output with `FILL_IN`s resolved. |
| `binding.yaml` | **Generated.** | Derived from the ontology + `(project, dataset)`. |
| `table_ddl.sql` | **Generated.** | Companion to the binding. |
| `property_graph.sql` | **Generated.** | Edge columns match `table_ddl.sql`. |
| `events.jsonl` | **Captured.** | Optional checked-in offline snapshot exported from a previous `agent_events` populate. Not present in this PR; populated by the notebook via `export_events_jsonl.py` after the agent runs. |

## Why the split

The first round of this PR conflated three responsibilities:

1. Loading the TTL and shaping it into runtime artifacts.
2. Generating events the notebook would consume.
3. Defining the demo's "agent" behavior.

The product clarification pinned the contract: the demo's external proof-point is **the SDK consuming real plugin traces produced by an agent that follows the real MAKO ontology**. A hand-coded event generator (`seed_events.py` in the round-1 shape) silently bypassed the BQ AA plugin's event-emission contract; a curated subset YAML (`ontology_demo.yaml`) silently bypassed the TTL-only authoring contract.

The reshape draws the line cleanly:

- **TTL-driven artifacts** belong in `mako_artifacts.py`.
- **Real agent behavior** belongs in `mako_demo_agent.py` + the plugin.
- **Captured snapshots** are exported via `export_events_jsonl.py`, not synthesized.

## Demo flow (what the notebook will do)

```
mako_core.ttl                                                    ┐
       │                                                         │
       ▼                                                         │ Beat 0 — setup
mako_artifacts.regenerate_snapshots(project, dataset)            │
       │                                                         │
       ├── ontology.yaml                                         │
       ├── binding.yaml                                          │
       ├── table_ddl.sql       ────► BigQuery (CREATE TABLE)     │
       └── property_graph.sql  ────► BigQuery (CREATE PROPERTY   │
                                              GRAPH)             ┘

run_agent.py --sessions N    ────► ADK runner + BQ AA plugin     ┐ Beat 0 — populate
                                          │                      │     agent_events
                                          ▼                      │
                                   agent_events table             ┘

ontology-build --skip-property-graph    ────► populates the      ┐
binding-validate                              MAKO node + edge   │ Beats 1–4
ontology-build (extracts the graph)           tables             │ consume
revalidate compiled extractors                                   │
OntologyRuntime + LabelSynonymResolver                           ┘
```

## Design decisions — open for review

### 1. MAKO `DecisionExecution` is the central hub

The demo entity allowlist (`DEMO_ENTITIES` in `mako_artifacts.py`) is six entities: `AgentSession`, `DecisionExecution`, `DecisionPoint`, `Candidate`, `SelectionOutcome`, `ContextSnapshot`. `DecisionExecution` is non-obvious but load-bearing — per MAKO's TTL, it's the entity that's `partOfSession` an `AgentSession`, `atContextSnapshot` a `ContextSnapshot`, `executedAtDecisionPoint` a `DecisionPoint`, `hasSelectionOutcome` a `SelectionOutcome`. The decision-flow story doesn't hold together without it.

The edge set is **fully TTL-driven**: `make_binding` walks `ontology.relationships` and picks every relationship whose endpoints both fall within `DEMO_ENTITIES`. No hardcoded edge list. The agent picks up nine real MAKO relationships (`atContextSnapshot`, `evaluatesCandidate`, `executedAtDecisionPoint`, `hasSelectionOutcome`, `partOfSession`, `rejectedCandidate`, `selectedCandidate`, +2 others).

### 2. FILL_IN resolution: synthesize `id: string`

The MAKO TTL doesn't declare `owl:hasKey` on most entities, so the OWL importer marks 17 concrete entities' primary keys as `FILL_IN`. `mako_artifacts.py` resolves each one to a synthesized `id: string` property + primary key. Matches MAKO's "every artifact has a stable identifier" design contract. If a future MAKO revision adds `owl:hasKey` declarations, the resolver leaves those alone — only `FILL_IN` placeholders get rewritten.

### 3. Cross-namespace relationships dropped (with audit trail)

MAKO extends PROV-O + PKO + DCAT. Four relationships in the TTL point to entities outside MAKO's own namespace (`delegatedBy → prov:Agent`, etc.). The artifact pipeline drops these so the ontology loads cleanly and records the dropped names under the ontology's top-level `mako_demo:dropped_cross_namespace_relationships` annotation. The loss is auditable from a loaded model.

### 4. Agent uses realistic tool names; mapping is explicit

A real ADK agent exposes business/task-oriented tools, not tools whose argument names mirror TTL property names. The demo follows that convention — tool names are imperative business verbs (`capture_context`, `propose_decision_point`, `evaluate_candidate`, `commit_outcome`, `complete_execution`) and tool argument / return-value keys use ordinary snake_case (`audience_size`, `budget_remaining_usd`, `business_entity_id`).

The **explicit mapping** between what the agent emits and what extraction materializes into the MAKO graph:

| Tool field (trace) | Materialized → MAKO property | Materialization rule |
|---|---|---|
| `capture_context.audience_size` | `ContextSnapshot.snapshotPayload` (component) | Folded into the JSON `snapshotPayload` blob. |
| `capture_context.budget_remaining_usd` | `ContextSnapshot.snapshotPayload` (component) | Same. |
| `capture_context.context_id` | `ContextSnapshot.id` (primary key) | 1:1. |
| `propose_decision_point.decision_point_id` | `DecisionPoint.id` (primary key) | 1:1. |
| `propose_decision_point.reversibility` | `DecisionPoint.reversibility` | 1:1. |
| `propose_decision_point.decision_type` | — | **Trace-only.** MAKO does not declare `decisionType` on `DecisionPoint`; the field exists in the trace for analytics but isn't materialized. |
| `evaluate_candidate.candidate_id` | `Candidate.id` (primary key) | 1:1. |
| `evaluate_candidate.candidate_label` | — | **Trace-only.** `Candidate` has no MAKO-declared data properties; the label exists in the trace as reasoning context. |
| `evaluate_candidate.decision_point_id` | `evaluatesCandidate` edge (DecisionPoint → Candidate) | Edge endpoint. |
| `commit_outcome.outcome_id` | `SelectionOutcome.id` | 1:1. |
| `commit_outcome.selected_candidate_id` | `selectedCandidate` edge (SelectionOutcome → Candidate) | Edge endpoint. |
| `commit_outcome.rationale` | — | **Trace-only.** `SelectionOutcome` has no MAKO-declared rationale field. |
| `complete_execution.execution_id` | `DecisionExecution.id` | 1:1. |
| `complete_execution.business_entity_id` | `DecisionExecution.businessEntityId` | 1:1 (column `business_entity_id`). |
| `complete_execution.latency_ms` | `DecisionExecution.latencyMs` (INT64) | 1:1 (column `latency_ms`, **typed INT64** in `table_ddl.sql`). |
| `complete_execution.{decision_point,context,outcome}_id` | `executedAtDecisionPoint` / `atContextSnapshot` / `hasSelectionOutcome` edges | Each is an edge endpoint pointing at the parent `DecisionExecution`. |
| `session_id` (envelope) | `partOfSession` edge (DecisionExecution → AgentSession) | Plugin envelope; the extractor reads the BQ AA `session_id` field. |

**Rule of thumb:** only fields with a TTL-declared target property are materialized; everything else stays in the raw `agent_events` trace as reasoning context. The mapping above is enforced by the reference extractor (lands in a follow-up commit) — the notebook can compare its output against this table to verify the contract.

The agent uses Vertex AI Gemini by default (`DEMO_AGENT_MODEL=gemini-2.5-flash`). Same wiring pattern as `examples/decision_lineage_demo/agent/agent.py`.

### 5. `(project, dataset)` is a parameter, not a baked-in value

`mako_artifacts.regenerate_snapshots(project=..., dataset=...)` and `run_agent.py --project X --dataset Y` both take the target as input. The checked-in snapshots use `test-project-0728-467323` / `migration_v5_demo` as defaults so reviewers can `cat` them, but the notebook regenerates everything against a fresh `migration_v5_demo_<8-hex>` dataset at runtime.

### 6. `events.jsonl` is captured, not synthesized

If kept, `events.jsonl` is the output of `export_events_jsonl.py` reading from `agent_events`. The notebook may use it as an offline corpus for Beat 3's revalidation tests (deterministic input the threshold gates can lock against), but the demo's primary event surface is the live `agent_events` table.

## Validation commands run (all pass)

```bash
# Artifact pipeline runs end-to-end and regenerates snapshots.
PYTHONPATH=src python examples/migration_v5/mako_artifacts.py
# → {"ontology_entities": 18, "binding_entities": 6,
#    "binding_relationships": 9}

# Generated ontology validates clean.
python -m bigquery_ontology.cli validate examples/migration_v5/ontology.yaml

# Generated binding validates against the generated ontology.
python -m bigquery_ontology.cli validate examples/migration_v5/binding.yaml \
    --ontology examples/migration_v5/ontology.yaml

# Demo agent + plugin import cleanly.
PYTHONPATH=src:examples/migration_v5 python -c "
import mako_demo_agent
print(type(mako_demo_agent.root_agent).__name__,
      len(mako_demo_agent.root_agent.tools),
      type(mako_demo_agent.bq_logging_plugin).__name__)"
# → LlmAgent 5 BigQueryAgentAnalyticsPlugin

# Driver --help works without live BQ / Vertex.
PYTHONPATH=src python examples/migration_v5/run_agent.py --help
```

A live end-to-end run (`run_agent.py --sessions 50`) requires Vertex AI access for the configured model and BigQuery write access on the target dataset. The notebook commit will execute this against a fresh dataset and inline the resulting evidence.

## What's NOT in this commit

- The notebook itself.
- Reference extractor for `mako_decision` events. Will be regenerated by `mako_artifacts.py` (or a sibling helper) once the binding shape is locked.
- NODE/FIELD/EDGE synthetic failure fixtures for Beat 3.6.
- A live end-to-end run of `run_agent.py` (the notebook will do this).
- `docs/README.md` / `CHANGELOG.md` entries — land with the notebook PR.

## Related

- [#107 storyboard](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/107) — per-cell plan the notebook implements.
- [#107 MAKO requirement](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/107#issuecomment-4435535476) — "test with the real ontology" comment.
- [Round-2 reshape clarification](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/pull/155#issuecomment-4437670647) — pinned the "TTL + runnable agent" contract this PR now implements.
- [`examples/decision_lineage_demo/`](../decision_lineage_demo/) — reference pattern for ADK agent + BQ AA plugin wiring.
- [Rollout guide](../../docs/extractor_compilation_rollout_guide.md) — Phase C pipeline reference for Beat 3 cells.
- [Ontology runtime reader](../../docs/ontology_runtime_reader.md) — #58 reader API used in Beat 4.
