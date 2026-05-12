# Compiled Structured Extractors — End-to-End Rollout Guide

**Status:** Operational playbook (Phase C wrap-up of issue #75)
**Parent epic:** [issue #75](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/75)

---

## What this is

The five Phase C sub-PRs land an end-to-end compiled-extractor pipeline. This guide is the **operational playbook** stitching them together: when to run each stage, what its inputs and failure modes look like, and where the per-stage detailed docs live.

This document is the index. Per-PR docs are the deep dives.

## Pipeline

```
   ┌──────────┐    ┌─────────┐    ┌──────┐    ┌──────┐    ┌────────────┐
   │ Compile  │───▶│ Publish │───▶│ Sync │───▶│ Wire │───▶│ Revalidate │
   └──────────┘    └─────────┘    └──────┘    └──────┘    └────────────┘
       (rare)         (post-      (boot /     (startup)    (periodic)
                       compile)    refresh)
```

| Stage | Trigger | Inputs | Outputs | Detailed doc |
|-------|---------|--------|---------|--------------|
| **Compile** | rule / schema / reference changes | extraction rule, event schema, reference extractor, LLM client | bundle on disk + manifest fingerprint | [bka_measurement](extractor_compilation_bka_measurement.md), [retry loop](extractor_compilation_retry_loop.md) |
| **Publish** | successful compile | local bundle root | rows in BigQuery mirror table | [bq_bundle_mirror](extractor_compilation_bq_bundle_mirror.md) |
| **Sync** | runtime boot or scheduled refresh | mirror table | bundles in local `dest_dir/<fingerprint>/` | [bq_bundle_mirror](extractor_compilation_bq_bundle_mirror.md) |
| **Wire** | runtime startup | synced bundle root + ontology + binding + fallback extractors | `OntologyGraphManager` with compiled+fallback path | [orchestrator_swap](extractor_compilation_orchestrator_swap.md) |
| **Revalidate** | cron / Cloud Scheduler | bundle root + events + reference extractors + thresholds | JSON report; exit 0/1/2 | [revalidate_cli](extractor_compilation_revalidate_cli.md) |

## Local vs distributed deployments

The five-stage flow is the **canonical distributed-runtime** path: the host that compiles is not the host that runs. For **co-located deployments** (one machine builds and runs), Publish + Sync collapse — Compile writes the bundle directly to the runtime's bundle root, and Wire reads from the same path. The shortened flow is:

```
Compile → Wire → Revalidate
```

This guide describes the full path because the failure modes carried by Publish/Sync (table schema drift, mid-deployment row corruption, mixed fingerprints) are real when bundles cross process boundaries. If you don't cross those boundaries, skip those stages.

## Stage 1: Compile

**Purpose:** Turn an extraction rule + event schema into a Python-callable bundle whose output matches a known-good reference extractor.

**API:** `compile_with_llm` for the loop; `measure_compile` for the compile-and-parity-check combo most callers want.

```python
import pathlib
from bigquery_agent_analytics.extractor_compilation import (
    measure_compile, compile_extractor,
)

# ``compile_source`` is a (plan, source) -> CompileResult
# closure that embeds the runtime's parent bundle dir +
# fingerprint inputs. See the BKA live test for the full
# canonical example:
# tests/test_extractor_compilation_bka_compile_live.py
def compile_source(plan, source):
    return compile_extractor(
        source=source,
        module_name="bka_extractor",
        function_name=plan.function_name,
        event_types=(plan.event_type,),
        sample_events=sample_events,
        spec=None,
        resolved_graph=resolved_graph,
        parent_bundle_dir=bundle_root,
        fingerprint_inputs=fingerprint_inputs,  # same dict that feeds compute_fingerprint
        template_version="v0.1",
        compiler_package_version="0.2.3",
    )

# Production: pass your real LLM client.
# Tests: pass a deterministic fake (see DETERMINISTIC_FAKE_MODEL).
measurement = measure_compile(
    extraction_rule=extraction_rules["bka_decision"],
    event_schema=event_schema,
    sample_events=sample_events,
    reference_extractor=extract_bka_decision_event,
    spec=None,
    llm_client=llm_client,
    compile_source=compile_source,
    model_name="gemini-2.5-flash",
)

assert measurement.ok and measurement.parity_ok, measurement.parity_divergences
```

**Output:** A bundle on disk at `bundle_root/<fingerprint>/` containing `manifest.json` + a Python module.

**Failure modes:**
- `measurement.ok=False` — the compile loop exhausted retries. Inspect `measurement.attempt_failures` for stage codes (`plan_parse_error:*` / `compile:*` / `render_error`).
- `measurement.parity_ok=False` — compile loop succeeded but the compiled extractor's output diverges from the reference. Inspect `measurement.parity_divergences`.

**Deep dive:** [bka_measurement](extractor_compilation_bka_measurement.md), [retry_loop](extractor_compilation_retry_loop.md).

## Stage 2: Publish (distributed runtimes only)

**Purpose:** Push the local bundle to a BigQuery mirror table so other processes can fetch it.

**API:** `publish_bundles_to_bq`.

```python
import pathlib
from google.cloud import bigquery
from bigquery_agent_analytics.extractor_compilation import (
    BigQueryBundleStore,
    publish_bundles_to_bq,
)

client = bigquery.Client(project="my-project", location="US")
store = BigQueryBundleStore(
    bq_client=client,
    table_id="my-project.my_dataset.compiled_bundles",
)
store.ensure_table()  # idempotent

result = publish_bundles_to_bq(
    bundle_root=pathlib.Path("/var/bqaa/bundles"),
    store=store,
    bundle_fingerprint_allowlist=None,  # publish every loadable bundle
)
assert not result.failures, result.failures
```

**Trust gate:** Every candidate bundle goes through `load_bundle(...)` before its rows are emitted. A bundle that wouldn't load at runtime is **not** published.

**Failure modes (per-bundle, in `result.failures`):**
- `duplicate_fingerprint` — two subdirectories under `bundle_root` declare the same manifest fingerprint. The publisher fails-closed; neither is published.
- `bundle_load_failed` — the bundle wouldn't load via `load_bundle`. Fix it before re-running.
- `manifest_unreadable` — the manifest's shape doesn't pass `_validate_manifest_shape` (typically a tampered `module_filename` or `fingerprint`).

**Deep dive:** [bq_bundle_mirror](extractor_compilation_bq_bundle_mirror.md).

## Stage 3: Sync (distributed runtimes only)

**Purpose:** Fetch bundles from the BigQuery mirror table into a local directory the runtime can read.

**API:** `sync_bundles_from_bq`.

In a distributed deployment the sync host is a different process from the publish host, so construct a fresh `BigQueryBundleStore` against the same `table_id` instead of reusing the publisher's handle:

```python
import pathlib
from google.cloud import bigquery
from bigquery_agent_analytics.extractor_compilation import (
    BigQueryBundleStore, sync_bundles_from_bq,
)

# Runtime-host process. Same table_id as the publisher; the
# BigQuery client uses Application Default Credentials for
# the runtime's service account.
store = BigQueryBundleStore(
    bq_client=bigquery.Client(project="my-project", location="US"),
    table_id="my-project.my_dataset.compiled_bundles",
)

result = sync_bundles_from_bq(
    store=store,
    dest_dir=pathlib.Path("/tmp/synced-bundles"),
    bundle_fingerprint_allowlist=[fingerprint],  # pin to the active fingerprint
)
assert not result.failures, result.failures
```

**Trust gate:** Each fingerprint is reconstructed in a **staging directory**, then `load_bundle(...)` is run against the staged copy. Only on success does sync replace `dest_dir/<fingerprint>/`. A corrupt mirror row never destroys a previously-good local bundle.

**Failure modes (per-fingerprint, in `result.failures`):**
- `bundle_load_failed` — the reconstructed bundle didn't load. Staging dir is scrubbed; existing `dest_dir/<fingerprint>/` (if any) is untouched.
- `manifest_row_missing` / `manifest_row_unreadable` / `module_row_missing` — bundle rows in BQ are incomplete or malformed.
- `invalid_bundle_path` — a row's `bundle_path` traverses (e.g. `..`) or is absolute. Fail-closed before any write.
- `duplicate_row` — two rows share `(fingerprint, bundle_path)`. BQ doesn't enforce uniqueness; the mirror does at sync time.
- `malformed_row` — wrong column types or non-sha256 `bundle_fingerprint`.
- `fingerprint_not_in_table` — the allowlist named a fingerprint with no rows; publish hasn't caught up.

**Deep dive:** [bq_bundle_mirror](extractor_compilation_bq_bundle_mirror.md).

## Stage 4: Wire

**Purpose:** Hand the synced bundle root to the runtime so compiled extractors replace direct fallback calls in `extract_graph()`.

**API:** `OntologyGraphManager.from_bundles_root`.

```python
import pathlib
from bigquery_agent_analytics.ontology_graph import OntologyGraphManager
from bigquery_agent_analytics.structured_extraction import (
    extract_bka_decision_event,
)

manager = OntologyGraphManager.from_bundles_root(
    project_id="my-project",
    dataset_id="my_dataset",
    ontology=ontology,
    binding=binding,
    bundles_root=pathlib.Path("/tmp/synced-bundles"),
    expected_fingerprint=fingerprint,
    fallback_extractors={
        "bka_decision": extract_bka_decision_event,
        # ...one entry per event_type the runtime handles
    },
)
# manager.extractors is now the wrapped registry: compiled with
# per-element fallback for event_types that have both, original
# fallback callables otherwise.
manager.extract_graph(...)
```

**Trust gate:** `discover_bundles` runs the per-bundle `load_bundle` check; bundles whose fingerprint doesn't match `expected_fingerprint`, whose event_types collide across bundles, or whose module fails to import are rejected fail-closed.

**Audit handles:**
- `manager.runtime_registry.discovery.failures` — per-bundle load failures (mismatched fingerprint, collisions, import errors).
- `manager.runtime_registry.bundles_without_fallback` — compiled-only event_types skipped because no fallback was provided.
- `manager.runtime_registry.fallbacks_without_bundle` — fallback-only event_types (no compiled coverage); pass through unchanged.

**Deep dive:** [orchestrator_swap](extractor_compilation_orchestrator_swap.md), [runtime_registry](extractor_compilation_runtime_registry.md), [runtime_fallback](extractor_compilation_runtime_fallback.md).

## Stage 5: Revalidate

**Purpose:** Periodically check that the deployed compiled extractor still agrees with the handwritten reference on a sample of real events.

**API:** the `bqaa-revalidate-extractors` CLI.

```bash
# Periodic check against local JSONL events:
bqaa-revalidate-extractors \
    --bundles-root /tmp/synced-bundles \
    --events-jsonl sampled_events.jsonl \
    --reference-extractors-module my_project.references \
    --thresholds-json thresholds.json \
    --report-out report.json

# Or against a BigQuery query (the query must produce one
# event_json STRING column per row, fully self-contained
# — no parameter substitution):
bqaa-revalidate-extractors \
    --bundles-root /tmp/synced-bundles \
    --events-bq-query-file events_query.sql \
    --bq-project my-project \
    --bq-location US \
    --reference-extractors-module my_project.references \
    --thresholds-json thresholds.json \
    --report-out report.json
```

The reference module exposes:

```python
EXTRACTORS:     dict[str, Callable[[dict, Any], StructuredExtractionResult]]
RESOLVED_GRAPH: ResolvedGraph     # from resolve(ontology, binding)
SPEC:           Any = None        # optional
```

**Exit codes:**

| Code | Meaning |
|------|---------|
| `0` | Pass (no thresholds OR all thresholds passed) |
| `1` | Threshold violation; report still written with `violations[]` |
| `2` | Usage/input error; report not written |

**Trust gate:** Inside the harness, every event goes through `run_with_fallback` (the same C2.b gate the runtime uses) plus a direct reference call. Two dimensions are reported: runtime decision (`compiled_unchanged` / `compiled_filtered` / `fallback_for_event`) and agreement against reference (`parity_match` / `parity_divergence` / `parity_not_checked`).

**Deep dive:** [revalidate_cli](extractor_compilation_revalidate_cli.md), [revalidation](extractor_compilation_revalidation.md), [runtime_fallback](extractor_compilation_runtime_fallback.md).

## Worked example: BKA decision extractor end-to-end

Assume a single event_type `bka_decision`, handwritten reference `extract_bka_decision_event`, and an LLM client wired to Gemini.

```python
# === Stage 1: Compile ===
import pathlib
from bigquery_agent_analytics.extractor_compilation import (
    measure_compile, compile_extractor, compute_fingerprint,
)
from bigquery_agent_analytics.structured_extraction import extract_bka_decision_event

bundle_root = pathlib.Path("/var/bqaa/bundles")
fingerprint = compute_fingerprint(
    ontology_text=ontology_yaml_str,
    binding_text=binding_yaml_str,
    event_schema=event_schema,
    event_allowlist=["bka_decision"],
    transcript_builder_version="tb-1",
    content_serialization_rules=content_rules,
    extraction_rules=extraction_rules,
    template_version="v0.1",
    compiler_package_version="0.2.3",
)
fingerprint_inputs = {  # same fields passed to compute_fingerprint above
    "ontology_text": ontology_yaml_str,
    "binding_text": binding_yaml_str,
    "event_schema": event_schema,
    "event_allowlist": ["bka_decision"],
    "transcript_builder_version": "tb-1",
    "content_serialization_rules": content_rules,
    "extraction_rules": extraction_rules,
    "template_version": "v0.1",
    "compiler_package_version": "0.2.3",
}

def compile_source(plan, source):
    return compile_extractor(
        source=source,
        module_name="bka_extractor",
        function_name=plan.function_name,
        event_types=(plan.event_type,),
        sample_events=bka_sample_events,
        spec=None,
        resolved_graph=resolved_graph,
        parent_bundle_dir=bundle_root,
        fingerprint_inputs=fingerprint_inputs,
        template_version="v0.1",
        compiler_package_version="0.2.3",
    )

measurement = measure_compile(
    extraction_rule=extraction_rules["bka_decision"],
    event_schema=event_schema,
    sample_events=bka_sample_events,
    reference_extractor=extract_bka_decision_event,
    spec=None,
    llm_client=my_llm_client,
    compile_source=compile_source,
    model_name="gemini-2.5-flash",
)
assert measurement.ok and measurement.parity_ok
```

```python
# === Stage 2: Publish (distributed runtimes only) ===
from google.cloud import bigquery
from bigquery_agent_analytics.extractor_compilation import (
    BigQueryBundleStore, publish_bundles_to_bq,
)

store = BigQueryBundleStore(
    bq_client=bigquery.Client(project="my-project", location="US"),
    table_id="my-project.my_dataset.compiled_bundles",
)
store.ensure_table()
publish_bundles_to_bq(bundle_root=bundle_root, store=store)
```

```python
# === Stage 3: Sync (distributed runtimes only, on the runtime host) ===
# Different process from publish — re-construct the store
# against the same table_id (typically using the runtime's
# service-account ADC).
from google.cloud import bigquery
from bigquery_agent_analytics.extractor_compilation import (
    BigQueryBundleStore, sync_bundles_from_bq,
)

store = BigQueryBundleStore(
    bq_client=bigquery.Client(project="my-project", location="US"),
    table_id="my-project.my_dataset.compiled_bundles",
)
sync_bundles_from_bq(
    store=store,
    dest_dir=pathlib.Path("/tmp/synced-bundles"),
    bundle_fingerprint_allowlist=[fingerprint],
)
```

```python
# === Stage 4: Wire (runtime startup) ===
from bigquery_agent_analytics.ontology_graph import OntologyGraphManager

manager = OntologyGraphManager.from_bundles_root(
    project_id="my-project",
    dataset_id="my_dataset",
    ontology=ontology,
    binding=binding,
    bundles_root=pathlib.Path("/tmp/synced-bundles"),
    expected_fingerprint=fingerprint,
    fallback_extractors={"bka_decision": extract_bka_decision_event},
)
# manager.extract_graph(...) now uses the compiled path with fallback.
```

```bash
# === Stage 5: Revalidate (cron, every 6h) ===
bqaa-revalidate-extractors \
    --bundles-root /tmp/synced-bundles \
    --events-bq-query-file /etc/bqaa/revalidate_query.sql \
    --bq-project my-project \
    --reference-extractors-module my_project.references \
    --thresholds-json /etc/bqaa/revalidate_thresholds.json \
    --report-out /var/log/bqaa/revalidate_$(date +%Y%m%d_%H%M).json
```

## Trust boundaries — the four gates

Four gates run across the pipeline so each handoff catches the next stage's surprises:

1. **Compile-time smoke gate, inside `compile_extractor`** — `load_callable_from_source` + `run_smoke_test[_in_subprocess]` verify the freshly-rendered module imports, the callable accepts `(event, spec)`, and doesn't crash on the sample events. (This is NOT `load_bundle` itself — there's no manifest yet; the smoke check is the compile-time analog.)
2. **Pre-publish gate, inside `publish_bundles_to_bq`** — `load_bundle(...)` runs against the local bundle before its rows are emitted. The mirror never distributes a bundle the runtime would reject.
3. **Post-sync gate, inside `sync_bundles_from_bq`** — `load_bundle(...)` runs against the staged reconstruction before it replaces the previous `dest_dir/<fingerprint>/`. A bad mirror row can't destroy good local state.
4. **Runtime-discovery gate, inside `discover_bundles`** — `load_bundle(...)` runs per child at startup; every loaded bundle's fingerprint must match the runtime's active inputs.

This is why corrupted or drifted bundles don't reach the production extract path: four chances to catch them before a runtime call. Three of the gates are `load_bundle` itself (publish, sync, discovery); the compile-time gate is the smoke check that exists *before* the manifest is written.

## Cadence

| Stage | Typical cadence | What changes between runs |
|-------|-----------------|---------------------------|
| Compile | Rare (days to weeks) | When the ontology, binding, event schema, extraction rules, or reference extractor changes. The fingerprint is the cache key — unchanged inputs short-circuit. |
| Publish | After every successful compile | New bundle in the mirror table; old rows for the same `(fingerprint, path)` are replaced (DELETE+INSERT — see the [non-atomicity caveat](extractor_compilation_bq_bundle_mirror.md#idempotency--non-atomic-publish)). |
| Sync | Runtime boot, plus optional scheduled refresh | New rows from publish are pulled into the local bundle dir; staged-replace guarantees no good bundle is destroyed by a bad row. |
| Wire | Runtime startup | The wrapped registry is rebuilt; existing call sites (`extract_graph`) automatically pick up the new behavior. |
| Revalidate | Periodic (hours to daily) | The same compiled bundle is checked against a fresh event sample; drift in real-world events surfaces here. |

## Failure-recovery playbook

| Symptom | Stage | Action |
|---------|-------|--------|
| `measurement.ok=False`, attempts exhausted | Compile | Inspect `measurement.attempt_failures` for the stage code (parse / render / smoke). Fix the rule or sample events. |
| `measurement.parity_ok=False` | Compile | Inspect `measurement.parity_divergences`. Either the rule is wrong or the reference has drifted. |
| `duplicate_fingerprint` | Publish | Two subdirectories under `bundle_root` declare the same fingerprint. Move one or regenerate; neither is published until the conflict is resolved. |
| `bundle_load_failed` | Publish | The bundle wouldn't load at runtime. Fix the bundle before re-publishing. |
| `bundle_load_failed` | Sync | The mirror rows reconstruct a bundle the loader rejects. Investigate the table state; previous good local bundle is intact. |
| `fingerprint_not_in_table` | Sync | The publisher hasn't pushed for that fingerprint yet. Wait or re-run publish. |
| `manifest_row_unreadable` / `manifest_row_missing` | Sync | The publishing pipeline emitted a corrupt manifest row. Re-run publish for that fingerprint. |
| `invalid_bundle_path` / `duplicate_row` | Sync | Table contents are not in the expected shape. Investigate the publisher's output; the mirror never auto-fixes. |
| `discovery.failures` not empty | Wire | Check the synced dir for fingerprint mismatches or import errors. The runtime falls back to non-compiled paths for affected event_types. |
| Revalidation exits 1 | Revalidate | A threshold tripped. Inspect `threshold_check.violations` in the report. Triage: deploy regression (rebuild + republish), ontology drift (update bindings), or transient noise (relax thresholds). |
| Revalidation exits 2 | Revalidate | Usage / input error. Stderr names the file path and failure type. No report is written. |

## Related

- [extractor_compilation_runtime_target.md](extractor_compilation_runtime_target.md) — why compiled extractors run client-side Python (PR 4a).
- [extractor_compilation_scaffolding.md](extractor_compilation_scaffolding.md) — compile-time scaffolding (PR 4b.1).
- [extractor_compilation_retry_loop.md](extractor_compilation_retry_loop.md) — `compile_with_llm` loop semantics.
- [extractor_compilation_bka_measurement.md](extractor_compilation_bka_measurement.md) — `measure_compile` + the BKA decision case.
- [extractor_compilation_bundle_loader.md](extractor_compilation_bundle_loader.md) — the `load_bundle` gate (PR C2.a).
- [extractor_compilation_runtime_fallback.md](extractor_compilation_runtime_fallback.md) — `run_with_fallback` decision tree (PR C2.b).
- [extractor_compilation_runtime_registry.md](extractor_compilation_runtime_registry.md) — runtime adapter (PR C2.c.1).
- [extractor_compilation_orchestrator_swap.md](extractor_compilation_orchestrator_swap.md) — `from_bundles_root` (PR C2.c.2).
- [extractor_compilation_bq_bundle_mirror.md](extractor_compilation_bq_bundle_mirror.md) — publish/sync utilities (PR C2.c.3).
- [extractor_compilation_revalidation.md](extractor_compilation_revalidation.md) — the harness behind the CLI (PR C2.d).
- [extractor_compilation_revalidate_cli.md](extractor_compilation_revalidate_cli.md) — the CLI itself.
