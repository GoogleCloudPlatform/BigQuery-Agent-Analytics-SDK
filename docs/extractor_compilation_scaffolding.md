# Compiled Structured Extractors — Scaffolding (PR 4b.1)

**Status:** Implemented (PR 4b.1 of issue #75 Phase C)
**Parent epic:** [issue #75](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/75)
**Runtime-target RFC:** [`extractor_compilation_runtime_target.md`](extractor_compilation_runtime_target.md)
**Working plan:** [issue #96, comment 4363301699](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/96#issuecomment-4363301699), Milestone C1 / PR 4b
**Date:** 2026-05-05

---

## What this is

The deterministic contract layer the LLM-driven template fill (PR 4b.2) plugs into. **No LLM call lives here.** This PR ships compile-time plumbing:

- `compute_fingerprint(...)` — sha256 over the #75 input tuple.
- `Manifest` — bundle provenance dataclass with `to_json` / `from_json`.
- `validate_source(source) -> AstReport` — allowlist-based AST safety check.
- `run_smoke_test(extractor, ...) -> SmokeTestReport` — runs a candidate against sample events and gates on the #76 `validate_extracted_graph` validator.
- `compile_extractor(...) -> CompileResult` — end-to-end pipeline (fingerprint → AST → write source → import → smoke + validator → write manifest). Bundle is on disk iff `result.ok`.

Out of scope (deferred to PR 4b.2 and C2 per the runtime-target RFC):

- LLM-driven template fill — 4b.2.
- Runtime loader / orchestrator integration — C2.
- Bundle storage discovery (in-repo vs BQ-table mirror vs both) — C2.
- Per-event / per-field / per-node / per-edge fallback wiring — C2.
- Multiple compiled extractor baselines — later in C1.

## Public API

```python
from bigquery_agent_analytics.extractor_compilation import (
    AstFailure,
    AstReport,
    CompileResult,
    Manifest,
    SmokeTestReport,
    compile_extractor,
    compute_fingerprint,
    run_smoke_test,
    validate_source,
)
```

All names are also re-exported from the top-level `bigquery_agent_analytics` package.

### `compile_extractor`

```python
result = compile_extractor(
    source=...,                      # Python source string
    module_name=...,                 # stable, fingerprint-unique module name
    function_name=...,               # function inside `source` to invoke
    event_types=("bka_decision",),
    sample_events=[...],             # ≥ 1 (#75 expects ≥ 100 in production)
    spec=None,                       # forwarded to the extractor
    resolved_graph=resolved_spec,    # #76 validator gates against this
    parent_bundle_dir=Path(...),
    fingerprint_inputs={
        "ontology_text": ...,
        "binding_text": ...,
        "event_schema": {...},
        "event_allowlist": (...,),
        "transcript_builder_version": ...,
        "content_serialization_rules": {...},
        "extraction_rules": {...},
    },
    template_version="v0.1",
    compiler_package_version="0.0.0",
)

if result.ok:
    # Bundle is on disk at result.bundle_dir/
    #   manifest.json
    #   <module_name>.py
    pass
else:
    # Inspect result.ast_report.failures and/or
    # result.smoke_report.{exceptions, validation_failures}.
    pass
```

Stages, in order. Any failure short-circuits and leaves no half-written artifacts on disk:

1. **Fingerprint.** `compute_fingerprint(**fingerprint_inputs, template_version, compiler_package_version)` is the directory name under `parent_bundle_dir`.
2. **AST validation.** `validate_source(source)` — fails early before any `exec_module` call.
3. **Write source.** Source is written to `bundle_dir/<module_name>.py`.
4. **Import.** `load_callable_from_source(...)` imports the module via `importlib.util.spec_from_file_location` and looks up `function_name`.
5. **Smoke test + validator.** `run_smoke_test(extractor, events=..., spec=..., resolved_graph=...)` runs the callable on each sample event, captures per-event exceptions, merges results, and runs `validate_extracted_graph`.
6. **Manifest.** `manifest.json` is written last — its presence signals a successful compile.

### Local bundle layout

PR 4b.1 commits to one layout:

```
<parent_bundle_dir>/
└── <fingerprint>/
    ├── <module_name>.py
    └── manifest.json
```

The fingerprint is the directory name so two compile runs on identical inputs land in the same directory. `module_name` and `function_name` are validated up front as plain Python identifiers — path-traversal-shaped names like `../x` fail the gate before the harness ever touches the filesystem.

The compile pipeline writes through a sibling staging directory and **stages a replace** of the target on success — `rmtree` of any pre-existing bundle, then `rename` of the staged directory in. Not strictly atomic at the filesystem level (a process crash between the two ops would leave the target absent), but the bundle is reproducible from inputs so the next compile re-creates it. Failed gates leave any pre-existing valid bundle untouched.

A second `compile_extractor` call with identical inputs (fingerprint + `function_name` + `module_name` + `event_types` + on-disk source bytes) is a **cache hit candidate**. The cache hit path:

1. Imports the cached bundle's callable.
2. Re-runs `run_smoke_test(...)` against the *current* `sample_events` / `resolved_graph` / `min_nonempty_results`. A weak historical sample set won't paper over a current-call regression — the gate runs against current inputs.
3. If smoke passes, returns `cache_hit=True` with the cached manifest. Nothing is rewritten on disk; the bundle is byte-identical between consecutive calls and `created_at` is preserved.
4. If smoke fails (e.g., the new call supplies stricter inputs), surfaces the failure. The on-disk bundle is left intact since the source hasn't changed; rewriting wouldn't help.

Runtime discovery — where C2's loader looks for bundles, and whether to mirror them into a BQ table — is deliberately deferred per the runtime-target RFC.

### Manifest fields

```json
{
  "compiler_package_version": "0.0.0",
  "created_at": "2026-05-05T00:00:00+00:00",
  "event_types": ["bka_decision"],
  "fingerprint": "<sha256-hex>",
  "function_name": "extract_bka_decision_event_compiled",
  "module_filename": "<module_name>.py",
  "template_version": "v0.1",
  "transcript_builder_version": "v0.1"
}
```

Round-trips through `Manifest.to_json()` / `Manifest.from_json()`.

## AST allowlist

Compiled extractors must pass [`validate_source`][validator] before the harness imports them. The allowlist is intentionally narrow — extending it as real templates require it (e.g., specific stdlib helpers) is a future PR, not a default.

### Accepted

- `from __future__ import annotations`
- `from bigquery_agent_analytics.extracted_models import ExtractedNode, ExtractedEdge, ExtractedProperty`
- `from bigquery_agent_analytics.structured_extraction import StructuredExtractionResult, StructuredExtractor`
- Module scope: only the docstring, allowlisted imports, and function definitions.
- Pure control flow: `if` / **bounded** `for` / comprehensions.
- Literals, f-strings, allowlisted constructors and method calls.

A `for` (or comprehension generator) is **bounded** when its iterable is one of: a literal tuple/list/set/dict, a constant, a parameter or attribute, a subscript, or a method call (e.g., `dict.items()`). A call to a bare `Name` (`range(...)`, `iter(...)`, a user-defined helper) is **rejected** as `disallowed_for_iter` — those forms can be arbitrarily long and would hang the in-process smoke runner. `iter` is also blocked as a top-level name. (`while` is rejected outright.)

### Rejected (with stable failure codes)

| `code` | Trigger |
|---|---|
| `syntax_error` | `ast.parse(source)` raised. |
| `disallowed_import` | Plain `import x` (any module); `from x import y` where `x` is outside the per-module symbol allowlist; `from <allowed_module> import <not_in_allowlist>`; `from x import *`; or `from x import y as _z` (private/dunder aliases). |
| `disallowed_name` | Reference to `eval`, `exec`, `compile`, `__import__`, `__build_class__`, `globals`, `locals`, `vars`, `setattr`, `getattr`, `delattr`, `open`, `input`, `exit`, `quit`, `breakpoint`. |
| `disallowed_attribute` | Any attribute access whose `attr` starts with `_` (blocks dunder access like `__class__` and private-attribute access). |
| `disallowed_async` | `async def` / `async with` / `async for` / `await`. |
| `disallowed_generator` | `yield` / `yield from`. |
| `disallowed_class` | `class` definition. |
| `disallowed_scope` | `global` / `nonlocal`. |
| `disallowed_decorator` | Any decorator on a function (decorators run at definition time). |
| `disallowed_default` | A function default argument that isn't a constant primitive (str / int / float / bool / None / bytes / unary-minus of int or float). Defaults run at module-import time, so non-constants are a smuggling vector. |
| `disallowed_while` | `while` loop (could hang the smoke-test runner). |
| `disallowed_for_iter` | `for` or comprehension whose iterable is a call to a bare `Name` (e.g., `range(...)`, `iter(...)`, a user-defined helper). Those forms can be unbounded; iterate over a literal tuple/list, a parameter, or a method call (`dict.items()`) instead. |
| `disallowed_call` | `Call(func=Name(...))` whose target isn't in the call-target allowlist (`ExtractedNode`, `ExtractedEdge`, `ExtractedProperty`, `StructuredExtractionResult`, `str`, `int`, `float`, `bool`, `bytes`, `set`, `frozenset`, `dict`, `list`, `tuple`, `isinstance`, `len`). Closes the `list(range(10**100))` / `sum(big_iter)` allocation hole. |
| `disallowed_method` | `Call(func=Attribute(...))` whose method name isn't in the method allowlist (`get`, `items`, `keys`, `values`, `append`). Closes `event.repeat_forever()` / `event.clear()` style hazards. The receiver is bounded, but unknown method calls can still mutate, allocate, or never return. |
| `disallowed_raise` | `raise` statement. `raise SystemExit` would otherwise escape any non-`BaseException` catch; banning `raise` broadly is the simplest rule. |
| `disallowed_try` | `try` / `try*` block. The smoke-test runner is the only layer that catches exceptions. |
| `disallowed_with` | `with` block. Context-manager protocols invoke `__enter__` / `__exit__` (dunder methods). |
| `top_level_side_effect` | Any module-scope statement other than the docstring, an allowlisted import, or a function definition. |

`AstReport.ok` is True iff every check passes. Failures collect rather than fail-fast — callers (templates, LLM fixers in 4b.2) get the full list in one pass.

## Subprocess isolation for the smoke gate

`compile_extractor` runs the smoke gate in a child process by default (`isolation=True`). The child applies a virtual-memory `setrlimit` (POSIX-only, best-effort elsewhere), imports the candidate source via `importlib`, runs the extractor on each event, and returns per-event outcomes (`StructuredExtractionResult`, exception traceback, or wrong-return-type marker) via pickle. The parent applies `subprocess.run(..., timeout=N)` for the wallclock cap and runs the #76 validator on the merged graph in-process — `ResolvedGraph` doesn't cross the process boundary.

Static AST checks are a pre-filter; subprocess isolation is the runtime safety net for hangs and memory blowups the allowlist can't catch (e.g., bounded `for` over a method-call chain that allocates internally). On timeout, the wrapper synthesizes one `TimeoutError` exception per event so callers see the standard `ok=False` shape with a clear cause.

`isolation=False` keeps the in-process path for tests that need to exercise the smoke runner directly with hand-built callables. Defaults: `smoke_timeout_seconds=30.0`, `smoke_memory_limit_mb=512`. The public `run_smoke_test_in_subprocess(...)` helper is exported for callers that want subprocess isolation outside the compile pipeline.

## Hand-authored fixture

`tests/fixtures_extractor_compilation/bka_decision_template.py` ships one hand-authored Python source string equivalent to `extract_bka_decision_event`. The end-to-end test compiles this fixture, asserts every gate passes, re-loads the bundle from disk, and asserts the compiled callable's output matches `extract_bka_decision_event` on the same sample events.

PR 4b.2 will replace this hand-written string with output from the LLM-driven template fill — but the AST allowlist, smoke-test runner, and #76 validator gate are the same gates the LLM-emitted source must clear.

## Testing

`tests/test_extractor_compilation.py` covers:

- **TestFingerprint** (5 tests): determinism, allowlist-order independence, every named input is hashed (parametrized), template_version + compiler_package_version are hashed.
- **TestManifest** (3 tests): JSON round-trip, deterministic serialization for identical fields, sorted keys.
- **TestAstValidator** (24 tests): safe source passes; one negative test per failure code, plus tests for the per-module symbol allowlist, wildcard imports, dunder aliases, decorators, non-constant defaults (and that constant defaults still pass), `while`, `raise`, `try`, `with`, and additional forbidden names (`exit`, `quit`, `__build_class__`, `breakpoint`).
- **TestSmokeTest** (11 tests): rejects empty event list; rejects negative `min_nonempty_results`; captures per-event exceptions including `SystemExit`; surfaces validator failures; rejects wrong return types; fails when every event produces empty results; allows opt-out via `min_nonempty_results=0`; clean run returns `ok=True`; subprocess timeout surfaces as `TimeoutError` exceptions; subprocess happy path runs the BKA fixture against a real spec.
- **TestCompileExtractor** (18 tests): end-to-end compile of the BKA fixture; compiled output equivalence; AST failure leaves nothing on disk; smoke failure leaves nothing on disk; cache hit (no rewrite, `created_at` preserved); cache hit re-runs smoke against current inputs (stricter-sample case fails the gate); cache hit misses when the *source*, *module_name*, or *event_types* differ; corrupt-manifest cache state treated as a miss; cache-hit import failure falls through to fresh compile; failed recompile leaves an existing valid bundle byte-identical; invalid `module_name` / `function_name` rejected (parametrized for path-traversal-shaped names + Python keywords).

109 tests total, all pass against the full repo suite.

## Related

- [`extractor_compilation_runtime_target.md`](extractor_compilation_runtime_target.md) — Phase 1 runtime-target decision (PR 4a).
- [`ontology/validation.md`](ontology/validation.md) — failure-code surface compiled extractors must clear at the smoke-test gate.
- [`structured_extraction.py:198`][hook] — the `run_structured_extractors()` hook compiled bundles will plug into in C2.

[validator]: ../src/bigquery_agent_analytics/extractor_compilation/ast_validator.py
[hook]: ../src/bigquery_agent_analytics/structured_extraction.py
