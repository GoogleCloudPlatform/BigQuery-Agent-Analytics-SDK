# Compiled Structured Extractors — Plan Parser (PR 4b.2.2.a)

**Status:** Implemented (PR 4b.2.2.a of issue #75 Phase C)
**Parent epic:** [issue #75](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/75)
**Builds on:** [`extractor_compilation_template_renderer.md`](extractor_compilation_template_renderer.md) (PR 4b.2.1)
**Working plan:** [issue #96, comment 4363301699](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/96#issuecomment-4363301699), Milestone C1 / PR 4b.2.2.a

---

## What this is

The deterministic JSON-to-`ResolvedExtractorPlan` parser. PR 4b.2.2.b will add the LLM step that produces this JSON; this PR owns the schema and validation contract that step must clear.

**No LLM call lives here.** Every test feeds hand-authored JSON to lock down the parser independently of probabilistic mapping.

## Public API

```python
from bigquery_agent_analytics.extractor_compilation import (
    parse_resolved_extractor_plan_json,
    PlanParseError,
    RESOLVED_EXTRACTOR_PLAN_JSON_SCHEMA,
)

plan = parse_resolved_extractor_plan_json(json_string_or_dict)
# plan is now a ResolvedExtractorPlan that has cleared every gate
# the renderer's _validate_plan checks. Hand it directly to
# render_extractor_source(plan) + compile_extractor(...).
```

## JSON Schema for structured-output mode

`RESOLVED_EXTRACTOR_PLAN_JSON_SCHEMA` is a Draft-2020-12 JSON Schema dict that captures every *structural* rule the parser enforces (types, required fields, `additionalProperties: false`, `minLength: 1` for non-empty strings, `minItems: 1` for non-empty paths). PR 4b.2.2.b can hand it directly to the LLM client's structured-output mode (Gemini's `response_schema`, OpenAI's `json_schema` response format, etc.) so the LLM is constrained to emit **structurally valid JSON** — JSON that clears the parser's schema checks before reaching the semantic gate.

Semantic rules (Python-identifier shape, function-name keyword/allowlist exclusion, duplicate property names) aren't expressible in plain JSON Schema and stay as parser-only checks. **A payload that passes the schema may still fail the parser's semantic gate; a payload that fails the schema is guaranteed to fail the parser.**

The golden BKA fixture is asserted to conform to the exported schema in `tests/test_extractor_compilation_plan_parser.py::TestExportedJsonSchema`, alongside negative cases (unknown field, missing required, empty path, wrong type) — so the schema can't silently drift away from the parser.

`parse_resolved_extractor_plan_json` accepts either a JSON string (parsed via `json.loads`) or an already-parsed dict. On any failure it raises `PlanParseError` with three structured fields:

- **`code`** — stable string identifier callers can switch on.
- **`path`** — dotted path to the offending field (e.g., `"key_field.source_path[1]"`); empty for root-level failures.
- **`message`** — human-readable detail.

`str(error)` is `"[<code>] <path>: <message>"` for easy logging.

## JSON shape

The JSON payload mirrors `ResolvedExtractorPlan` 1:1:

```json
{
  "event_type": "bka_decision",
  "target_entity_name": "mako_DecisionPoint",
  "function_name": "extract_bka_decision_event_compiled",
  "key_field": {
    "property_name": "decision_id",
    "source_path": ["content", "decision_id"]
  },
  "property_fields": [
    {"property_name": "outcome",
     "source_path": ["content", "outcome"]}
  ],
  "session_id_path": ["session_id"],
  "span_handling": {
    "span_id_path": ["span_id"],
    "partial_when_path": ["content", "reasoning_text"]
  }
}
```

| Field | Required | Default | Notes |
|---|---|---|---|
| `event_type` | yes | — | Non-empty string. Used in the generated extractor's top-of-function guard. |
| `target_entity_name` | yes | — | Python-identifier-shaped (embedded directly into the `node_id` f-string in generated source). |
| `function_name` | yes | — | Python identifier, not a reserved keyword, not in the call-target allowlist. |
| `key_field` | yes | — | Object with `property_name` (Python identifier) + `source_path` (non-empty list of non-empty strings). |
| `property_fields` | no | `[]` | List of `FieldMapping` objects. Each `property_name` must be unique across the key + property_fields. |
| `session_id_path` | no | `["session_id"]` | Non-empty list of non-empty strings. |
| `span_handling` | no | `null` | Object with `span_id_path` (default `["span_id"]`) and optional `partial_when_path` (default `null`). |

The hand-authored BKA fixture lives at `tests/fixtures_extractor_compilation/plan_bka_decision.json` and is the canonical golden round-trip case.

## Failure codes

Every `PlanParseError` carries one of these stable codes:

| `code` | Trigger |
|---|---|
| `invalid_json` | Input string couldn't be parsed by `json.loads`. |
| `wrong_root_type` | Payload isn't a JSON string / dict, or top-level value isn't a JSON object. |
| `missing_required_field` | A required field is absent (`event_type`, `target_entity_name`, `function_name`, `key_field`, `key_field.property_name`, `key_field.source_path`, etc.). |
| `unknown_field` | An unrecognized field at any nesting level. The parser is strict about unknown fields so LLM typos surface immediately. |
| `wrong_type` | A field value is the wrong shape (e.g., string where a list was expected; object where a string was expected). |
| `empty_string` | A non-empty string field is `""`, or a path segment is `""`. |
| `empty_path` | A path field (`source_path` / `session_id_path` / `span_id_path` / `partial_when_path`) is `[]`. |
| `invalid_identifier` | `function_name` / `target_entity_name` / a property name isn't Python-identifier-shaped, or `function_name` is a reserved keyword, or `function_name` shadows an allowlisted call target. |
| `duplicate_property_name` | A property name appears more than once across `key_field` + `property_fields`. |
| `invalid_plan` | Defensive: the renderer's `_validate_plan` rejected something the parser missed. Should never fire in practice; present so a future renderer rule the parser hasn't learned about still produces a clean `PlanParseError` rather than escaping as `ValueError`. |

## What this PR is *not*

Per the PR 4b.2.2.a sizing call:

- **No LLM client.** PR 4b.2.2.b adds the prompt template and `PlanResolver(llm_client).resolve(extraction_rule, event_schema) -> ResolvedExtractorPlan`.
- **No retry orchestration.** PR 4b.2.2.c adds retry-on-AST/smoke/validator-failure with diagnostics fed back to the LLM.
- **No prompt template.** The schema documented above is the contract; the prompt that produces JSON matching it is 4b.2.2.b's concern.

## Tests (55 cases in `tests/test_extractor_compilation_plan_parser.py`)

- **`TestBkaGolden`** (3 cases) — JSON fixture parses to the same `ResolvedExtractorPlan` as the hand-authored plan; parser accepts dict input directly; parsed plan renders + compiles end-to-end through 4b.2.1's `render_extractor_source` and 4b.1's `compile_extractor`.
- **`TestExportedJsonSchema`** (8 cases) — schema is a well-formed dict with `additionalProperties: false`; golden BKA fixture conforms; minimal payload conforms; payloads with unknown field / missing required / empty `source_path` / wrong type get rejected by the schema; explicit `null` `span_handling` conforms.
- **`TestDefaults`** (6 cases) — omitted `property_fields` / `session_id_path` / `span_handling`; explicit `null` `span_handling`; `span_handling` with default `span_id_path`; explicit `null` `partial_when_path`.
- **`TestSchemaRejections`** (25 cases) — invalid JSON; wrong root type (int, list); missing each required top-level field (parametrized); unknown field at top level / inside `key_field` / inside `span_handling`; wrong type for top-level string fields (parametrized); empty string `event_type`; `property_fields` not a list; `property_fields[i]` not an object; `source_path` not a list / empty / non-string segment / empty segment; missing `property_name` inside `property_fields[i]`; **non-string dict keys** rejected with a clean `PlanParseError` (no `TypeError` on `sorted`).
- **`TestSemanticRejections`** (13 cases) — invalid `function_name` (parametrized for path-traversal-shaped, leading digit, whitespace, hyphens); reserved-keyword `function_name` (parametrized for `class`, `def`, `for`, `return`); `function_name` shadowing `len` / `isinstance` / `ExtractedNode`; non-identifier `target_entity_name`; non-identifier property name; duplicate property name within `property_fields`; property name colliding with key.

## Related

- [`extractor_compilation_scaffolding.md`](extractor_compilation_scaffolding.md) — 4b.1 scaffolding (what the parsed plan eventually compiles through).
- [`extractor_compilation_template_renderer.md`](extractor_compilation_template_renderer.md) — 4b.2.1 source generator (what consumes the parsed plan).
- [`extractor_compilation_runtime_target.md`](extractor_compilation_runtime_target.md) — Phase 1 runtime-target decision.
