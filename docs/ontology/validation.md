# Extracted-Graph Validation — Reference

`validate_extracted_graph(spec, graph)` checks that an `ExtractedGraph` (output of the extraction pipeline — LLM `AI.GENERATE`, hand-written `structured_extraction`, or future compiled extractors) conforms to the ontology declared in a `ResolvedGraph`. It runs **after extraction, before materialization** — catching ontology drift, malformed extractor output, and orphan-edge bugs that would otherwise silently corrupt the materialized property graph.

This validator is a sibling to [`binding-validation.md`](binding-validation.md):

| | This validator (#76) | Binding validator (#105) |
|---|---|---|
| Inputs | `ResolvedGraph` + `ExtractedGraph` | `Ontology` + `Binding` + live `bq_client` |
| Phase | post-extraction, pre-materialization | pre-extraction |
| Surfaces | extractor output ↔ ontology spec drift | binding ↔ BigQuery schema drift |

Both expose the same public report ergonomics (`ok` / `failures` / typed codes) but keep separate `Failure` types because their context fields differ.

## When to run it

- **In the extraction pipeline** as a gate between `extractor.extract_graph(...)` and `materializer.materialize(...)`. The compiled-extractor runtime in #75 C2 will plug in here.
- **In CI on golden fixtures**: run a sample `ExtractedGraph` through the validator on every ontology-spec change to catch regressions in the extractor authoring contract.
- **Locally during ontology authoring**: write a fixture extracted graph, run the validator, see what your ontology actually requires from extractors.

## Public API

```python
from bigquery_agent_analytics import (
    validate_extracted_graph,
    validate_extracted_graph_from_ontology,
    ValidationReport,
    ValidationFailure,
    FallbackScope,
)
from bigquery_agent_analytics.resolved_spec import resolve

spec = resolve(ontology, binding)
report = validate_extracted_graph(spec, extracted_graph)

if not report.ok:
    for f in report.failures:
        print(f"[{f.scope.value}] {f.code} at {f.path}: {f.detail}")
```

For callers holding upstream `Ontology` + `Binding` instead of a `ResolvedGraph`:

```python
report = validate_extracted_graph_from_ontology(ontology, binding, graph)
```

### Lineage-edge batches: `allow_external_endpoints=True`

Some pipelines materialize lineage edges in a graph with `nodes=[]` after the endpoint nodes were materialized in earlier passes. The default strict mode flags this as `unresolved_endpoint`. Pass `allow_external_endpoints=True` to skip the in-graph node lookup:

```python
report = validate_extracted_graph(
    spec, lineage_edges_only_graph, allow_external_endpoints=True
)
```

The endpoint-key parse (`missing_endpoint_key`) still runs in permissive mode — short-form node-ids that produce no parseable keys still fail, so silent FK-column corruption stays caught.

The `wrong_endpoint_entity` check also still runs in permissive mode by parsing the entity segment from the node-id (`{session}:{entity}:k=v`). An external endpoint id whose entity segment doesn't match the relationship's declared `from_entity` / `to_entity` still fails — only the in-graph node lookup is skipped.

## Fallback scopes

Each failure carries the smallest safe unit of replacement so downstream consumers (notably the compiled-extractor runtime in #75) know whether to re-extract a single field, a whole node, an edge, or — eventually — the whole event:

| Scope | Meaning | Recovery |
|---|---|---|
| `FIELD` | Property type mismatch, unknown property name, unsupported composite value. The rest of the node is recoverable. | Re-extract that one property. |
| `NODE` | Missing `node_id`, duplicate `node_id`, missing primary-key column, unknown `entity_name`. The node's identity is broken. | Re-extract the whole node and any edges referencing it. |
| `EDGE` | Unknown relationship, unresolved `from_node_id`/`to_node_id`, wrong endpoint entity, missing endpoint key. | Re-extract the whole edge. |
| `EVENT` | Reserved for #75 C2's compiled-extractor runtime. **Not emitted by this validator** — event-scope classification needs per-`event_type` expectations from #75's compile-time `event_schema`, which `ResolvedGraph` doesn't carry. | Re-run the entire event through the fallback extractor (when #75 C2 lands). |

Filter by scope with `report.by_scope(FallbackScope.NODE)`.

## Failure codes

12 codes ship in this validator. The `code` field on `ValidationFailure` is a stable string identifier callers can switch on.

### NODE-scope codes

| `code` | Meaning |
|---|---|
| `unknown_entity` | `ExtractedNode.entity_name` doesn't match any declared entity in the spec. |
| `missing_node_id` | `node_id` is empty. |
| `duplicate_node_id` | `node_id` collides with another node in the same graph. |
| `missing_key` | A column listed in `ResolvedEntity.key_columns` is absent (or empty-string) on the node's properties. |
| `key_mismatch` | The materializer-routed primary-key value disagrees with the parsed `node_id` key segment, OR two extracted properties route to the same key column with conflicting values, OR the `node_id` entity segment doesn't match `ExtractedNode.entity_name`. The materializer writes node rows from `node.properties` (with last-wins on duplicate routing) but writes edge FK columns from `parse_key_segment(node_id)`; disagreement produces edges pointing at non-existent rows. The entity-segment check keeps node-id semantics consistent across in-graph edges (which resolve through `entity_name`) and lineage-only batches (which resolve through the parsed segment). Also fires when a key value contains `,` — `_build_key_string` doesn't escape commas, so `parse_key_segment` truncates at the first comma and the parsed value won't equal the property value. (`=` and `:` both round-trip cleanly: `parse_key_segment` splits each pair on the *first* `=`, and splits the node-id on `:` *at most twice*, so `key=a=b` parses as `{"key": "a=b"}` and `sess:Decision:decision_id=a:b` parses as `{"decision_id": "a:b"}`.) |

### FIELD-scope codes

| `code` | Meaning |
|---|---|
| `unknown_property` | `ExtractedProperty.name` doesn't match any property's `logical_name` or `column` on the entity. |
| `type_mismatch` | The value's Python type isn't accepted by the declared `sdk_type`. |
| `unsupported_type` | The value is a list or dict on a scalar property. (Ontology v0 doesn't support composite types — use separate entities + relationships.) |

The validator accepts these Python value shapes per `sdk_type`:

| `sdk_type` | Accepted Python value shapes |
|---|---|
| `string` | `str` |
| `bytes` | `bytes`, `bytearray` |
| `int64` | `int` (and `bool` is **explicitly rejected** despite being an `int` subclass) |
| `double` | `int`, `float` (`bool` rejected) |
| `boolean` | `bool` |
| `date` | `datetime.date` (not `datetime.datetime`) or ISO-8601 `YYYY-MM-DD` string |
| `timestamp` | tz-aware `datetime.datetime` or ISO-8601 string |

Naive `datetime.datetime` (no `tzinfo`) is rejected for `timestamp` per the issue body — the materializer needs an explicit timezone.

The validator narrows date/timestamp strings to the **BigQuery JSON-input shape** before semantic parsing. `datetime.fromisoformat` (Python 3.11+) accepts compact and week-date forms like `20260505`, `2026-W19-2`, `20260505T120000` that BigQuery JSON inserts reject. The validator gates on a regex first (dashed `YYYY-MM-DD` for `date`; dashed date + `T`/space + colon-separated time, **1–6 fractional-second digits**, optional `Z` or `±HH:MM` offset for `timestamp`) and only reaches `fromisoformat` for the semantic check (valid month/day, valid time-of-day). This keeps the validator's contract aligned with what the materializer can actually `INSERT`. BigQuery `TIMESTAMP` is microsecond precision, so nanosecond strings like `2026-05-05T12:00:00.123456789Z` are rejected up front.

### EDGE-scope codes

| `code` | Meaning |
|---|---|
| `unknown_relationship` | `ExtractedEdge.relationship_name` doesn't match any declared relationship. |
| `unresolved_endpoint` | `from_node_id` or `to_node_id` is empty or doesn't match any node in the graph. |
| `wrong_endpoint_entity` | The endpoint node resolves but its `entity_name` doesn't match the relationship's declared `from_entity` / `to_entity`. |
| `missing_endpoint_key` | A column listed in `ResolvedRelationship.from_columns` / `to_columns` cannot be read from the edge's `from_node_id` / `to_node_id`. The validator parses the node-id segment using the same `_parse_key_segment` the materializer uses (expected format `{session}:{entity}:k1=v1,k2=v2`); short-form node-ids like `d1` produce no parseable keys and trigger this code. |

## Property-name resolution

`ExtractedProperty.name` is matched against `ResolvedProperty.logical_name` first (the ontology-level name an LLM extractor naturally produces), then falls back to `ResolvedProperty.column` (the physical column name from the binding). Both forms are accepted on input — extractors emitting either name validate clean.

## Required vs optional

"Required" here means **entity primary keys** (from `ResolvedEntity.key_columns`) and **edge endpoint keys** (from `ResolvedRelationship.from_columns` / `to_columns`) only. A non-key property that isn't present is a valid partial extraction and does **not** produce a failure — hand-written extractors (e.g. `extract_bka_decision_event`) routinely populate only a subset of declared properties.

If the ontology model later grows an explicit `required: bool` on non-key properties, the validator will extend to cover it. Until then, non-key properties are optional by default.

## Deferred

- **Alternate-key validation.** `ResolvedEntity` doesn't currently surface alternate keys (only `key_columns` for the primary key + `ontology_key_primary` for lossless reverse conversion). Extending `resolve()` to populate `alternate_key_columns` is a separate prerequisite. Not blocking this validator's first landing.
- **Enum-membership validation.** `ResolvedProperty` carries no enum value list today. Extending `resolve()` to populate enum metadata is a separate prerequisite.
- **EVENT-scope codes.** Owned by #75 C2's compiled-extractor runtime, not by this validator. See "Fallback scopes" table above.

## CI usage pattern

```python
# tests/test_extractor_regression.py
from bigquery_agent_analytics import validate_extracted_graph
from my_extractor import extract_my_event_type

def test_extractor_output_validates_against_ontology():
    spec = load_resolved_spec()
    graph = build_extracted_graph(extract_my_event_type(sample_event))
    report = validate_extracted_graph(spec, graph)
    assert report.ok, [
        f"{f.code} at {f.path}: {f.detail}" for f in report.failures
    ]
```

## Related

- [#75](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/75) — compile-time code generation for structured trace extractors. This validator is P0.1, the hard prerequisite.
- [`binding-validation.md`](binding-validation.md) — pre-extraction validator (different phase, different inputs, similar API shape).
- [#58](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/58) — runtime entity-resolution primitives. `ValidationReport.ok` is a natural integration point for the resolver's strict-mode checks.
