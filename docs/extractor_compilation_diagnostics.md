# Compiled Structured Extractors — Diagnostic Builders (PR 4b.2.2.c.1)

**Status:** Implemented (PR 4b.2.2.c.1 of issue #75 Phase C)
**Parent epic:** [issue #75](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/75)
**Builds on:** [`extractor_compilation_plan_resolver.md`](extractor_compilation_plan_resolver.md) (PR 4b.2.2.b)
**Working plan:** [issue #96, comment 4363301699](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/96#issuecomment-4363301699), Milestone C1 / PR 4b.2.2.c.1

---

## What this is

Diagnostic builders that turn each gate's failure into a string the LLM can act on. PR 4b.2.2.c.2 will use these to assemble retry prompts; this PR ships the diagnostic format on its own so reviewers can lock down the feedback wording before the orchestration loop depends on it.

## Public API

```python
from bigquery_agent_analytics.extractor_compilation import (
    build_plan_parse_diagnostic,
    build_ast_diagnostic,
    build_smoke_diagnostic,
    build_compile_result_diagnostic,
    build_gate_diagnostic,
)

# Per-gate builders, each returning a string ready for retry-prompt embedding:
diag = build_plan_parse_diagnostic(plan_parse_error)   # PlanParseError
diag = build_ast_diagnostic(ast_report)                # AstReport
diag = build_smoke_diagnostic(smoke_report)            # SmokeTestReport

# Compile-pipeline envelope — covers AST/smoke plus the CompileResult-only
# failure modes (invalid_identifier / invalid_event_types / load_error)
# that don't surface through any single gate's report:
diag = build_compile_result_diagnostic(compile_result)  # CompileResult

# Or the dispatcher (used by the retry orchestrator in PR 4b.2.2.c.2):
diag = build_gate_diagnostic("parse", plan_parse_error)
diag = build_gate_diagnostic("ast", ast_report)
diag = build_gate_diagnostic("smoke", smoke_report)
diag = build_gate_diagnostic("compile", compile_result)  # preferred for the retry loop
```

## Contracts the diagnostic format clears

- **Actionable** — the LLM can act on the message without re-deriving where in its previous response the problem was. Per-failure entries surface the stable `code` from the underlying gate (`PlanParseError.code`, `AstFailure.code`, `ValidationFailure.code`) plus the dotted `path` or source line, so the LLM can grep its own output for the offending field / line.
- **Bounded** — diagnostics get embedded in retry prompts; long tracebacks or hundreds of validator failures would crowd out the rule + schema grounding. Each section is capped at the first **ten** entries with an `... and N more (truncated)` line; tracebacks are reduced to their last informative line (the exception type + message).
- **Deterministic** — same input report → byte-identical output. Sorting where applicable; otherwise input order is preserved (which itself is event-iteration order in the smoke report and `ast.walk` order in the AST report).

The functions never raise — they always return a string.

## Output formats

### `build_plan_parse_diagnostic`

```
PlanParseError [code=<code>] at <path>: <message>
```

Empty paths render as `<root>` so the LLM sees an explicit anchor.

### `build_ast_diagnostic`

```
AST validation failed (<N> issue(s)):
  line <line> col <col>: <code>: <detail>
  line <line>: <code>: <detail>            (when col is None)
  <no-line>: <code>: <detail>              (when both are None)
  ... and <K> more (truncated)             (when total > 10)
```

### `build_smoke_diagnostic`

```
Smoke test failed:

  Per-event exceptions (<K> of <N> events):
    - <last informative line of traceback>
    ... and <M> more (truncated)

  Wrong return types (<K> of <N> events):
    - <wrong-return-type message>

  Non-empty floor: <K> of <N> events produced non-empty output; required >= <floor>.

  #76 graph validator failures (<K>):
    - [<scope>] <code> at <path>: <detail>
```

Sections appear only when they have content. Tracebacks are reduced to their last non-empty line (typically `ExceptionType: message`).

### `build_compile_result_diagnostic`

Single entry point covering every failure mode of the top-level `CompileResult` envelope, so the retry orchestrator can ask "what failed?" with one call regardless of which gate fired. Check order mirrors the pipeline order, so the message names the *earliest* failed stage:

```
CompileError [code=invalid_identifier]: <message>
CompileError [code=invalid_event_types]: <message>
CompileError [code=load_error]: <message>
```

For AST or smoke failures, output falls through to the per-gate builder above (`AST validation failed (...)` / `Smoke test failed: ...`).

These three `CompileError` codes are exactly the `CompileResult` fields that don't surface through `AstReport` or `SmokeTestReport`:

- **`invalid_identifier`** — `module_name` / `function_name` isn't a plain Python identifier (path-traversal-shaped, Python keyword, …).
- **`invalid_event_types`** — declared `event_types` is empty / malformed / duplicated, or has no matching sample event, or never produced non-empty smoke output. The retry loop's most common compile-level failure mode for an LLM-emitted plan: parser passes, AST passes, but the plan declared the wrong `event_type`.
- **`load_error`** — in-process import (`isolation=False`) blew up on the candidate source. Subprocess mode surfaces import failures inside the smoke report instead.

A successful `CompileResult` returns `"Compile succeeded (no diagnostic to render)."`. A defensive `[code=unknown]` fallback covers a hypothetical "ok=False but no field populated" — a logic-bug shape we want labelled in retry feedback rather than silently empty.

### `build_gate_diagnostic(kind, payload)`

Dispatches on `kind ∈ {"parse", "ast", "smoke", "compile"}` to the right per-gate builder. Raises `TypeError` if the payload type doesn't match the kind, `ValueError` for unknown kinds. The retry orchestrator (PR 4b.2.2.c.2) should prefer `kind="compile"` once it has a `CompileResult` in hand — it covers the union without re-deriving which gate fired.

## Out of scope

Per the PR 4b.2.2.c.1 sizing call:

- **No retry-prompt builder.** PR 4b.2.2.c.2 adds `build_retry_prompt(...)` that combines the original prompt + previous response + diagnostic.
- **No orchestration.** PR 4b.2.2.c.2 adds `compile_with_llm(...)` that loops resolver → renderer → `compile_extractor` and feeds gate failures back to the LLM.
- **No real LLM tests.** Every test feeds hand-built reports / errors.

## Tests (`tests/test_extractor_compilation_diagnostics.py`)

- **`TestPlanParseDiagnostic`** (3) — root path renders as `<root>`, simple path, dotted path.
- **`TestAstDiagnostic`** (6) — clean report passthrough; single failure with line + col; failure without col; failure without line; multiple failures preserved in walk order; truncation at ten with the `... and N more (truncated)` summary.
- **`TestSmokeDiagnostic`** (8) — clean report passthrough; tracebacks reduced to last informative line; wrong-return-types section; non-empty-floor section; #76 validator-failures section; all four sections combined preserve order; truncation at ten validator failures; empty traceback renders `<empty traceback>` placeholder.
- **`TestCompileResultDiagnostic`** — `ok` passthrough; `invalid_identifier` / `invalid_event_types` / `load_error` rendering; fall-through to AST and smoke builders when those gates fail; defensive `[code=unknown]` for the hypothetical no-field-populated shape.
- **`TestBuildGateDiagnostic`** — dispatches correctly for each of the four kinds; unknown kind raises `ValueError`; payload-type mismatch (including `kind="compile"` with non-`CompileResult`) raises a clear `TypeError`.

## Related

- [`extractor_compilation_plan_parser.md`](extractor_compilation_plan_parser.md) — `PlanParseError` shape (input to `build_plan_parse_diagnostic`).
- [`extractor_compilation_scaffolding.md`](extractor_compilation_scaffolding.md) — `AstReport` / `SmokeTestReport` shapes.
- [`extractor_compilation_plan_resolver.md`](extractor_compilation_plan_resolver.md) — what PR 4b.2.2.c.2 will combine these diagnostics with.
