# Compiled Structured Extractors — Plan Resolver (PR 4b.2.2.b)

**Status:** Implemented (PR 4b.2.2.b of issue #75 Phase C)
**Parent epic:** [issue #75](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/75)
**Builds on:** [`extractor_compilation_plan_parser.md`](extractor_compilation_plan_parser.md) (PR 4b.2.2.a)
**Working plan:** [issue #96, comment 4363301699](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/96#issuecomment-4363301699), Milestone C1 / PR 4b.2.2.b

---

## What this is

The LLM-driven plan resolver: given a raw extraction rule (the user's intent — "extract entity X from event_type Y") and an event schema (what fields the payload actually contains), produce a `ResolvedExtractorPlan` ready for 4b.2.1's renderer.

**This PR is adapter-free.** No `google-genai` import, no provider-specific glue. The resolver depends on a minimal `LLMClient` Protocol; concrete adapters (`google-genai`, OpenAI, etc.) and retry orchestration land in PR 4b.2.2.c / PR 4c.

## Public API

```python
from bigquery_agent_analytics.extractor_compilation import (
    LLMClient,
    PlanResolver,
    build_resolution_prompt,
)

class MyClient:
    def generate_json(self, prompt: str, schema: dict) -> dict:
        # Wrap whatever LLM client you have so it returns a parsed
        # JSON dict from a prompt + JSON-schema constraint.
        ...

resolver = PlanResolver(MyClient())
plan = resolver.resolve(extraction_rule, event_schema)
# plan is a ResolvedExtractorPlan that has cleared every gate
# the parser checks. Hand it to render_extractor_source(plan) +
# compile_extractor(...) to produce the bundle.
```

### `LLMClient` Protocol

```python
class LLMClient(Protocol):
    def generate_json(self, prompt: str, schema: dict) -> dict: ...
```

Structural typing — any object with a `generate_json` method matching the signature works. The resolver hands a **deep copy** of the exported schema through to the client so adapters that normalize provider-specific quirks (Gemini's `response_schema` not accepting `$schema` / `$defs`, etc.) can mutate their input freely without poisoning the module global for future callers. Providers that support structured-output mode (Gemini's `response_schema`, OpenAI's `response_format`) get the actual schema constraints; providers without that support can ignore `schema` — the parser's structural gate catches malformed responses regardless.

### `build_resolution_prompt(extraction_rule, event_schema) -> str`

**Inputs must be JSON-serializable** — typically plain Python dicts of strings / numbers / lists / nested dicts. Pydantic models, dataclasses, sets, custom classes, and other non-JSON-serializable objects raise `TypeError` with a clear contract message naming the offending field. Normalize them to plain dicts before calling.

Pure function. Same inputs → byte-identical output (`sort_keys=True` on every embedded JSON). The prompt instructs the LLM to:

- Map only fields that exist in `event_schema` (no hallucinated paths).
- Use Python-identifier-shaped names for entity / property / function.
- Avoid function names that would shadow the call-target allowlist (`ExtractedNode`, `len`, etc.).
- Omit uncertain optional fields rather than invent them.
- Emit JSON conforming to `RESOLVED_EXTRACTOR_PLAN_JSON_SCHEMA`.

The prompt embeds the full output-contract JSON Schema and the input rule + event schema verbatim, so the LLM has the actual fields available regardless of whether the provider also enforces `schema` at generation time.

### `PlanResolver(llm_client).resolve(extraction_rule, event_schema)`

Wires prompt → LLM → parser:

1. `build_resolution_prompt(extraction_rule, event_schema)` produces the prompt.
2. `llm_client.generate_json(prompt, RESOLVED_EXTRACTOR_PLAN_JSON_SCHEMA)` returns a dict.
3. `parse_resolved_extractor_plan_json(response)` validates and converts.

Failures propagate unchanged:

- **`PlanParseError`** from the parser surfaces `code`/`path`/`message` so future retry orchestration (PR 4b.2.2.c) can route on them.
- **Anything the LLM client raises** (`RuntimeError`, `KeyboardInterrupt`, provider-specific `APIError`, etc.) propagates unchanged. The resolver doesn't swallow transport / quota / auth errors — that's a deliberate design choice so PR 4b.2.2.c can layer typed retry on top without fighting hidden suppression.

## Out of scope

Per the PR 4b.2.2.b sizing call:

- **No retry logic.** PR 4b.2.2.c adds retry-on-AST/smoke/validator-failure with diagnostics fed back to the LLM.
- **No provider adapters.** PR 4c (or a separate PR) adds concrete `google-genai` / OpenAI clients.
- **No real LLM tests.** Tests use fake `LLMClient` implementations with pre-canned responses.

## Tests (35 cases in `tests/test_extractor_compilation_plan_resolver.py`)

- **`TestBuildResolutionPrompt`** (27) — deterministic for same inputs; dict insertion order doesn't matter; prompt grounds in rule + schema; prompt includes the output contract; prompt includes the no-hallucinated-paths rule; prompt includes identifier-safety rules with the call-target allowlist; **non-JSON-serializable `extraction_rule` raises a clear `TypeError` naming the offending field** (parametrized for rule with set / rule with custom class / rule itself a set); **non-JSON-serializable `event_schema` raises the same way**; **wrong root type rejected with `TypeError`** (parametrized for list / string / int / float / bool, both `extraction_rule` and `event_schema`); **non-string mapping keys rejected recursively** (parametrized for root, nested, inside-list); **non-finite floats rejected** (parametrized for `NaN` / `Infinity` / `-Infinity`).
- **`TestPlanResolver`** (8) — BKA fake response resolves to expected plan; **schema is passed through to client by value (deep copy), not identity** — equal to the exported global but a distinct object; **client mutation of its received schema doesn't leak into the module global**; prompt passed matches builder output; parser structural failure propagates with `code`/`path`; parser semantic failure (`function_name` shadowing) propagates; LLM `RuntimeError` not swallowed; LLM `KeyboardInterrupt` not swallowed.

## Related

- [`extractor_compilation_plan_parser.md`](extractor_compilation_plan_parser.md) — 4b.2.2.a parser + JSON schema (the contract this resolver drives the LLM into).
- [`extractor_compilation_template_renderer.md`](extractor_compilation_template_renderer.md) — 4b.2.1 renderer (consumes the resolved plan).
- [`extractor_compilation_scaffolding.md`](extractor_compilation_scaffolding.md) — 4b.1 compile pipeline.
- [`extractor_compilation_runtime_target.md`](extractor_compilation_runtime_target.md) — Phase 1 runtime-target decision.
