# Compiled Structured Extractors — Retry-on-Gate-Failure Orchestrator (PR 4b.2.2.c.2)

**Status:** Implemented (PR 4b.2.2.c.2 of issue #75 Phase C)
**Parent epic:** [issue #75](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/75)
**Builds on:** [`extractor_compilation_diagnostics.md`](extractor_compilation_diagnostics.md) (PR 4b.2.2.c.1), [`extractor_compilation_plan_resolver.md`](extractor_compilation_plan_resolver.md) (PR 4b.2.2.b)
**Working plan:** issue #96, Milestone C1 / PR 4b.2.2.c.2

---

## What this is

The orchestration loop that turns a raw extraction rule into a compiled extractor with retry on gate failure. Calls the LLM, parses the response, renders source, compiles, and on any failure feeds the diagnostic back into the next prompt. Stops on success or after `max_attempts`.

This is the piece that makes compiled extractors viable in practice: parser, renderer, and compile-pipeline failures are all *recoverable* through prompt feedback if the LLM gets to see what failed.

## Public API

```python
from bigquery_agent_analytics.extractor_compilation import (
    compile_with_llm,
    build_retry_prompt,
    AttemptRecord,
    RetryCompileResult,
    CompileSource,
)

result: RetryCompileResult = compile_with_llm(
    extraction_rule={...},          # user intent
    event_schema={...},             # event payload structure
    llm_client=my_llm_client,       # implements LLMClient protocol
    compile_source=my_compile_fn,   # (plan, source) -> CompileResult
    max_attempts=5,
)

if result.ok:
    # bundle is on disk at result.bundle_dir
    ...
else:
    # result.attempts captures every iteration's failure
    for record in result.attempts:
        print(record.attempt, record.diagnostic)
```

The `compile_source` callable closes over everything `compile_extractor` needs (sample events, spec, parent_bundle_dir, fingerprint inputs, version strings, …) so the loop signature stays narrow. A typical production wrapper:

```python
from bigquery_agent_analytics.extractor_compilation import compile_extractor

def my_compile_fn(plan, source):
    return compile_extractor(
        source=source,
        module_name=f"{plan.target_entity_name}_extractor",
        function_name=plan.function_name,
        event_types=(plan.event_type,),
        sample_events=sample_events,
        spec=spec,
        resolved_graph=resolved_graph,
        parent_bundle_dir=bundle_root,
        fingerprint_inputs=fingerprint_inputs,
        template_version=TEMPLATE_VERSION,
        compiler_package_version=COMPILER_PACKAGE_VERSION,
    )
```

## Loop semantics

```
attempt 1: prompt = build_resolution_prompt(rule, schema)
loop:
  raw = llm_client.generate_json(prompt, schema)
  try plan = parse_resolved_extractor_plan_json(raw)
    fail (PlanParseError)        -> diagnostic = build_plan_parse_diagnostic(err)
  try source = render_extractor_source(plan)
    fail (ValueError)            -> diagnostic = "RenderError [code=invalid_plan]: <msg>"
  result = compile_source(plan, source)
    fail (result.ok == False)    -> diagnostic = build_compile_result_diagnostic(result)
  ok -> return RetryCompileResult(ok=True, ...)
  failed and attempt == max_attempts -> return RetryCompileResult(ok=False, reason="max_attempts_reached", ...)
  failed and have attempts left -> prompt = build_retry_prompt(original, raw, diagnostic); loop
```

### Three failure channels

A failed `AttemptRecord` has exactly one of these populated:

| Channel | When | Diagnostic source |
|---------|------|-------------------|
| `plan_parse_error` | parser rejects the LLM's response | `build_plan_parse_diagnostic` |
| `render_error` | renderer `ValueError` (defensive — parser already runs `_validate_plan`, so this is the channel for hypothetical future renderer-only rules) | `"RenderError [code=invalid_plan]: <msg>"` synthesized inline |
| `compile_result` | `compile_extractor` returned `result.ok == False` | `build_compile_result_diagnostic` |

Keeping the channels distinct means future telemetry can route on the field name directly instead of switching on a stringly-typed `error_kind`. The successful terminal attempt has `compile_result` populated with `ok=True` and the other channels `None`.

### `max_attempts` semantics

- `max_attempts=1` runs the loop once: one LLM call, one compile, no retry. Used by callers who want the loop's structured `RetryCompileResult` output without retry behavior.
- `max_attempts=N` (N >= 2) attempts up to N times; stops early on success.
- `max_attempts < 1` raises `ValueError` — there's no meaningful interpretation of "zero attempts."

### Exception propagation

Anything the LLM client or `compile_source` raises that *isn't* a `PlanParseError` (which originates in our parser, not the client) propagates unchanged. The loop never silently retries auth / quota / network failures — a future caller can wrap the client with its own retry policy if it wants automatic backoff.

## `build_retry_prompt`

Pure function. Wraps the original resolution prompt with the LLM's prior response and a diagnostic explaining why it was rejected.

```python
build_retry_prompt(
    original_prompt=...,    # output of build_resolution_prompt; reused verbatim
    prior_response=...,     # the dict the LLM returned (allowed to be a non-dict)
    diagnostic=...,         # diagnostic builder output
) -> str
```

Determinism: `prior_response` is serialized with `sort_keys=True`, so two semantically-equal dicts produce byte-identical retry prompts. For *malformed* responses where strict serialization can't proceed — mixed-type keys (which raise `TypeError` on sort), unserializable types (`TypeError`), or circular references (`ValueError`) — the builder falls back to insertion-order JSON, then to `repr()`. Losing byte-stability is acceptable on a degenerate input that already produced a parser failure; without the fallback, the retry-prompt builder would crash on exactly the inputs the loop was designed to recover from.

Output shape:

```
<original_prompt>
# Previous attempt

Your previous response was rejected. ...

## Previous response

{<prior_response as sort_keys JSON>}

## Diagnostic

<diagnostic>

Now emit a new JSON object that addresses the diagnostic above.
```

## `AttemptRecord`

Per-iteration state. Stored in `RetryCompileResult.attempts` in 1-indexed order. Fields:

- `attempt: int`
- `prompt: str` — the prompt sent to the LLM. Stored verbatim. Telemetry / log persistence may want to redact sensitive payloads later; the in-memory record keeps the full string.
- `raw_response: dict | None` — the LLM's response (parser input). `None` only if the LLM call raised — but since LLM exceptions propagate, in practice `raw_response` is always populated for any record that survives.
- `plan_parse_error: PlanParseError | None`
- `render_error: str | None`
- `compile_result: CompileResult | None`
- `diagnostic: str | None` — the diagnostic fed *into the next attempt*. `None` on the successful terminal attempt or when the loop exhausted at this attempt.

## `RetryCompileResult`

- `ok: bool` — True iff the final attempt produced a valid bundle.
- `manifest: Manifest | None`
- `bundle_dir: pathlib.Path | None`
- `attempts: tuple[AttemptRecord, ...]` — in 1-indexed iteration order.
- `reason: str` — `"succeeded"` or `"max_attempts_reached"`.

## Tests (21 cases in `tests/test_extractor_compilation_retry_loop.py`)

- **`TestBuildRetryPrompt`** (7) — original prompt embedded verbatim; `prior_response` serialized with sorted keys (byte-stable for equal dicts); diagnostic embedded verbatim; non-dict `prior_response` (list) renders cleanly; byte-stable for repeated calls; mixed-type keys (the `TypeError` shape from `json.dumps(sort_keys=True)`) handled via the fallback path; circular references (the `ValueError` shape) fall through to the `repr()` tier.
- **`TestCompileWithLlmArgs`** (2) — `max_attempts=0` and negative both raise `ValueError`.
- **`TestCompileWithLlmHappyPath`** (1) — succeeds on first try; one LLM call, one compile call, single `AttemptRecord` with `compile_result.ok==True` and no diagnostic.
- **`TestCompileWithLlmRecovery`** (3) — recovers after parser error, after compile failure (`invalid_event_types`), after renderer `ValueError` (monkeypatched). Each test asserts the right failure channel is populated and that the next attempt's prompt embeds the right diagnostic.
- **`TestCompileWithLlmExhaustion`** (2) — exhausts after `max_attempts` failures; `max_attempts=1` exhausts after exactly one attempt with the failure captured for the caller.
- **`TestCompileWithLlmExceptionPropagation`** (1) — LLM client exceptions propagate unchanged; no silent retry.
- **`TestCompileWithLlmRetryPromptContent`** (1) — second attempt's prompt embeds both the prior response and the diagnostic.
- **`TestCompileWithLlmEndToEnd`** (1) — wires the *real* `compile_extractor` via a closure (not a stub) and proves the full stack lines up: prompt → LLM → parser → renderer → compile → bundle on disk.
- **`TestCompileWithLlmSchemaMutation`** (2) — a mutating client can't corrupt the exported `RESOLVED_EXTRACTOR_PLAN_JSON_SCHEMA` module global; each attempt receives a fresh deep copy so a second attempt isn't fed a schema the first attempt's adapter normalized in place.
- **`TestCompileWithLlmRecoveryNonStringKeys`** (1) — full-loop repro of the mixed-type-keys recovery path: parser rejects, retry-prompt builder doesn't crash, second attempt receives the original (echoed) response plus the parser diagnostic.

## Out of scope

- **Provider adapters.** `LLMClient` is a structural Protocol; concrete `google-genai` / OpenAI / Vertex adapters land separately as part of integration work.
- **Caller-level retry policy.** The loop's retry is for *gate failures* the LLM can fix by trying again. Transport / quota / auth retry is the caller's responsibility (typically wrapped around the `LLMClient` instance).
- **Telemetry persistence.** `AttemptRecord.prompt` keeps the full string in memory; persistent logging may want redaction for prompts that include user data. Out of scope for this PR.

## Related

- [`extractor_compilation_diagnostics.md`](extractor_compilation_diagnostics.md) — the diagnostic builders this loop feeds back into prompts.
- [`extractor_compilation_plan_resolver.md`](extractor_compilation_plan_resolver.md) — single-shot resolver; this loop reuses its prompt builder and parser but bypasses `PlanResolver` itself so retry-prompt control stays in one place.
- [`extractor_compilation_scaffolding.md`](extractor_compilation_scaffolding.md) — `compile_extractor` and `CompileResult` shapes.
