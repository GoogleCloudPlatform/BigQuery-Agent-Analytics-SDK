# Compiled Structured Extractors — Template Renderer (PR 4b.2.1)

**Status:** Implemented (PR 4b.2.1 of issue #75 Phase C)
**Parent epic:** [issue #75](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/75)
**Builds on:** [`extractor_compilation_scaffolding.md`](extractor_compilation_scaffolding.md) (PR 4b.1)
**Working plan:** [issue #96, comment 4363301699](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/96#issuecomment-4363301699), Milestone C1 / PR 4b.2.1

---

## What this is

The deterministic source generator the LLM-driven template fill (PR 4b.2.2) will plug into. Turns a pre-resolved `ResolvedExtractorPlan` into a Python source string that 4b.1's `compile_extractor` can run through every gate (AST allowlist, smoke runner, #76 validator).

**No LLM call lives here.** PR 4b.2.2 owns the LLM step that *resolves* a raw extraction-rule + event-schema pair into a `ResolvedExtractorPlan`; this module is the deterministic boundary the LLM output has to cross before any source generation happens.

## Public API

```python
from bigquery_agent_analytics.extractor_compilation import (
    FieldMapping,
    ResolvedExtractorPlan,
    SpanHandlingRule,
    render_extractor_source,
)

plan = ResolvedExtractorPlan(
    event_type="bka_decision",
    target_entity_name="mako_DecisionPoint",
    function_name="extract_bka_decision_event_compiled",
    key_field=FieldMapping(
        property_name="decision_id",
        source_path=("content", "decision_id"),
    ),
    property_fields=(
        FieldMapping("outcome", ("content", "outcome")),
        FieldMapping("confidence", ("content", "confidence")),
        FieldMapping(
            "alternatives_considered", ("content", "alternatives_considered")
        ),
    ),
    span_handling=SpanHandlingRule(
        span_id_path=("span_id",),
        partial_when_path=("content", "reasoning_text"),
    ),
)

source = render_extractor_source(plan)
# Hand `source` to compile_extractor(source=..., ...) — it clears
# every 4b.1 gate by construction.
```

`render_extractor_source(plan)` raises `ValueError` for malformed plans (empty strings, duplicate property names, invalid `function_name`, `function_name` shadowing an allowlisted call target). All checks are at the renderer boundary so callers see a clear plan-level error rather than an opaque AST-gate failure later.

## Plan model

`ResolvedExtractorPlan` is deliberately boring. Every field-level decision is already made by the caller; the renderer is pure source-generation. Fields:

| Field | Description |
|---|---|
| `event_type` | The event type the bundle covers. Recorded in the docstring; the manifest's `event_types` is set independently by `compile_extractor`. |
| `target_entity_name` | The ontology entity the extractor produces (e.g., `mako_DecisionPoint`). Becomes the `ExtractedNode.entity_name`. |
| `function_name` | Name of the generated callable. Must be a Python identifier, not a keyword, not a name in 4b.1's call-target allowlist. |
| `key_field` | The required key — extractor declines (returns empty result) when this path is missing or `None`. |
| `property_fields` | Optional fields appended to the result's properties list when present. Each `property_name` must be unique (and distinct from the key's). |
| `session_id_path` | Where to find the session id in the event. Default `("session_id",)`. |
| `span_handling` | Optional. When set, the rendered extractor populates the result's `fully_handled_span_ids` / `partially_handled_span_ids` sets. When unset, both stay empty. |

`FieldMapping(property_name, source_path)`: `source_path` is a tuple of dict keys. Length 1 means the field is at the event root (`event["x"]`); length ≥2 means nested (`event["content"]["x"]`). Missing or wrong-shape intermediates resolve to "field absent" rather than raising.

`SpanHandlingRule(span_id_path, partial_when_path)`: `partial_when_path` is optional. When set, the span is marked **partially** handled if the path's value is truthy (matches the BKA pattern of "free-text reasoning still needs the AI extractor"). When unset, the span is always **fully** handled.

## Generated-source contract

The output:

- Imports only the three symbols it actually uses (`ExtractedNode`, `ExtractedProperty`, `StructuredExtractionResult`) from 4b.1's per-module symbol allowlist. `ExtractedEdge` isn't imported since the renderer doesn't emit edges yet.
- Calls only allowlisted Names (`isinstance`, `len`, `set`, `ExtractedNode`, etc.) and allowlisted method names (`get`, `items`, `keys`, `values`, `append`).
- Has no shadowing of allowlisted call targets, no decorators, no non-constant defaults, no halt/escape constructs (`while`, `raise`, `try`, `with`, `match`).
- Returns well-formed `StructuredExtractionResult` instances — `nodes` is a `list[ExtractedNode]`, `edges` is `list[ExtractedEdge]`, span sets are `set[str]`. Passes the smoke runner's well-formed-result check.
- Is **deterministic**: identical plans render byte-identical source. Useful so the compile fingerprint stays stable across consecutive renders of the same plan.

## Equivalence with the BKA fixture

The BKA-equivalent plan (above) renders source whose runtime output is structurally identical to `extract_bka_decision_event` on the same sample events. The end-to-end test compiles the rendered source through `compile_extractor`, re-loads the bundle, and asserts equivalence on every BKA sample.

## Out of scope

Per the runtime-target RFC and the PR 4b.2.1 sizing call:

- **LLM prompt/schema** for resolving raw extraction rules → `ResolvedExtractorPlan` — PR 4b.2.2.
- **Retry-on-AST/smoke/validator failure** — PR 4b.2.2.
- **Diagnostics** showing which gate failed and what was changed on retry — PR 4b.2.2.
- **Edge extractors** — only nodes for now. `ExtractedEdge` import is in the call-target allowlist but the renderer doesn't emit edge construction; that's a future plan-shape extension.
- **Composite property values** (lists, dicts) — ontology v0 explicitly defers these.

## Tests (39 cases in `tests/test_extractor_compilation_template.py`)

- **`TestPlanValidation`** (27 cases): valid BKA plan renders; parametrized rejection of bad function names (path-traversal, leading digit, whitespace, empty, Python keywords, allowlist shadowing); empty / non-string `event_type` / `target_entity_name` / `key_field`; duplicate `property_name` (within `property_fields` and against the key); `target_entity_name` containing a quote rejected; non-identifier-shaped property names rejected (parametrized); non-string top-level fields and path segments rejected (parametrized).
- **`TestGeneratedSourceClearsGates`** (5 cases): BKA source passes `validate_source`; BKA source compiles end-to-end via `compile_extractor` (subprocess smoke + #76 validator); rendered output matches `extract_bka_decision_event` on every sample event; **wrong-event-type input returns empty** (top-of-function guard); render is deterministic (byte-identical for identical plans).
- **`TestPlanShapeVariations`** (7 cases): plan with no `property_fields`; plan with no `span_handling`; plan with single-step paths; plan with deep traversal path (length 3, including missing-intermediate negative case); deep optional property / `session_id_path` / `partial_when_path` with non-dict intermediate at depth 1.

## Related

- [`extractor_compilation_scaffolding.md`](extractor_compilation_scaffolding.md) — 4b.1 scaffolding (the gates the rendered source must clear).
- [`extractor_compilation_runtime_target.md`](extractor_compilation_runtime_target.md) — Phase 1 runtime-target decision.
- `tests/fixtures_extractor_compilation/bka_decision_template.py` — the hand-authored fixture this PR's renderer must match.
