# Compiled Structured Extractors — Bundle Loader + Discovery (PR C2.a)

**Status:** Implemented (PR C2.a of issue #75 Phase C / Milestone C2)
**Parent epic:** [issue #75](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/75)
**Builds on:** [`extractor_compilation_runtime_target.md`](extractor_compilation_runtime_target.md) (the runtime-target RFC), [`extractor_compilation_scaffolding.md`](extractor_compilation_scaffolding.md) (compile harness + Manifest)
**Working plan:** issue #96, Milestone C2 / PR C2.a

---

## What this is

The **trust boundary** between on-disk compiled bundles and the runtime that's about to import + execute them. Per the runtime-target RFC, compiled extractors run client-side as plain Python callables plugged into the existing `run_structured_extractors()` hook. This module is what verifies a bundle on disk matches the runtime's *active inputs* (fingerprint + event_types) and that the imported callable has a usable shape, before any callable is registered.

Two distinct concerns:

1. **`load_bundle`** — single-bundle loader. Verifies one bundle directory and returns either `LoadedBundle` (everything passed) or `LoadFailure` (one of the stable failure codes). Never raises.
2. **`discover_bundles`** — directory walker. Loads every child bundle, applies an optional `event_type_allowlist`, detects duplicate-coverage collisions and fails closed on them, and returns a `DiscoveryResult` with the populated `event_type → callable` registry plus an audit trail.

C2.a is loader + discovery only. Validation/fallback semantics, BQ-table mirror, and the ontology-graph call-site swap are explicitly out of scope.

## Public API

```python
from bigquery_agent_analytics.extractor_compilation import (
    load_bundle,
    discover_bundles,
    LoadedBundle,
    LoadFailure,
    DiscoveryResult,
)

# Single bundle
result = load_bundle(
    bundle_dir,
    expected_fingerprint=...,
    expected_event_types=("bka_decision",),  # subset check; None to skip
)
if isinstance(result, LoadedBundle):
    extractor = result.extractor
else:
    log.warning("bundle %s rejected: %s — %s", result.bundle_dir, result.code, result.detail)

# Many bundles
discovery = discover_bundles(
    bundles_root,
    expected_fingerprint=...,
    event_type_allowlist=("bka_decision", "tool_call"),  # None to register everything
)
extractors_dict = discovery.registry         # event_type -> callable
loaded_audit    = discovery.loaded           # tuple[LoadedBundle, ...]
failures        = discovery.failures         # tuple[LoadFailure, ...]
```

## Stable LoadFailure codes

Callers can switch on `failure.code`:

| Code | When |
|---|---|
| `manifest_missing` | `bundle_dir/manifest.json` doesn't exist (or, for `discover_bundles` on a non-existent / unreadable parent, the parent itself is unavailable — a `PermissionError` from `iterdir()` lands here too rather than propagating) |
| `manifest_unreadable` | JSON parse error, schema mismatch (unknown / missing fields), or any field whose type / shape doesn't satisfy the manifest contract. The strict parse rejects: `event_types: "xy"` (would silently become `("x", "y")` under lenient parsing); `event_types` empty / containing duplicates / non-string items; `module_filename` that's not `<identifier>.py` (rejects `../escape.py`, `/etc/passwd.py`, `foo.bar.py`, `class.py`, non-string values); `function_name` that isn't a Python identifier or is a keyword. The loader's strict parser is what makes the trust boundary load-bearing — a permissive parse would let nonsense register at runtime. |
| `fingerprint_mismatch` | manifest fingerprint != caller's `expected_fingerprint` |
| `event_types_mismatch` | `expected_event_types` (when set) isn't a subset of the manifest's `event_types` |
| `module_not_found` | the module file referenced by the manifest is absent on disk (only reachable for shape-valid manifests; the strict parse already rejects path-traversal-shaped names) |
| `import_failed` | importing the module raised — covers `Exception` *and* `BaseException` (e.g., `SystemExit`), so a malicious or buggy bundle can't tear down the loading process |
| `function_not_found` | manifest's `function_name` isn't defined as a callable in the imported module |
| `function_signature_mismatch` | the imported callable can't be called as `f(event, spec)` (best-effort introspection via `inspect.signature` + `sig.bind(None, None)`) |
| `event_type_collision` | discovery only: two valid bundles declare coverage of the same event_type. Fail-closed: that event_type is dropped from the registry; both colliding bundles get a failure record. Other event_types from those bundles still register. |

## Validation order in `load_bundle`

Each gate short-circuits — the first failure wins:

1. `manifest.json` exists.
2. `manifest.json` parses with **strict shape validation** (no unknown / missing fields, every field is the declared type, `event_types` is a non-empty list of distinct non-empty strings, `module_filename` is `<identifier>.py` with no path components, `function_name` is a Python identifier).
3. Manifest fingerprint equals `expected_fingerprint`.
4. `expected_event_types` (when set) is a subset of `manifest.event_types`.
5. Resolved module path is directly inside `bundle_dir` (defense in depth — the shape check at step 2 already catches path traversal; this catches anything the shape check misses, including symlink shenanigans).
6. The module file exists on disk.
7. Importing the module succeeds (no exception). `import_failed` catches both `Exception` and `BaseException` so a bundle that calls `sys.exit` at import time can't tear down the loading process.
8. The manifest's `function_name` is defined as a callable in the imported module.
9. The imported callable accepts `(event, spec)`.

After a successful load, the imported module is **popped from `sys.modules`** — the captured callable retains a reference to the module's globals, so the runtime keeps working without leaking a `<stem>__loaded_<uuid>` entry per call. Repeated `load_bundle` calls don't grow `sys.modules`.

The fingerprint check runs **before** module import, so an attacker can't side-effect via a broken module if their fingerprint doesn't match. A regression test (`test_fingerprint_check_runs_before_module_load`) pins this ordering. Path-traversal attempts are rejected at step 2 (manifest shape) or step 5 (resolved-path containment) — *before* any import attempt — so `module_filename: "../escape.py"` cannot import a sibling file outside the bundle.

## Multi-event bundle semantics

A manifest declaring `event_types=("a", "b")` registers **the same callable** under both keys in the discovery registry. The bundle is loaded once; the registry has one entry per declared event_type.

## Allowlist semantics in `discover_bundles`

`event_type_allowlist`:

- `None` → register every declared event_type.
- A tuple → register only event_types that appear in the tuple (bundle still loads even if some event_types are filtered out).
- An empty tuple → register nothing (degenerate but valid).

A bundle whose entire declared coverage falls outside the allowlist still loads (it's in `discovery.loaded`); none of its event_types reach `discovery.registry`. The bundle isn't "broken" — it's just unwanted by this runtime.

## Collision policy: fail closed

Two bundles claiming the same event_type fail closed:

- The event_type is **dropped** from the registry.
- Each colliding bundle gets a separate `LoadFailure` with code `event_type_collision`.
- Other event_types from those same bundles **still register** if they're unique.

The alternative — silently picking one bundle — would make runtime behavior depend on filesystem ordering, which is a debugging nightmare and a security smell.

## Tests (54 cases in `tests/test_extractor_compilation_bundle_loader.py`)

- **`TestLoadBundleHappyPath`** (3) — valid bundle loads; subset check accepts broader manifest; check is skipped when `expected_event_types=None`.
- **`TestLoadBundleFailureCodes`** (13) — every stable code, including: manifest missing, invalid JSON, missing required field, fingerprint mismatch, event-types mismatch, module not found, import-time `SyntaxError`, import-time `RuntimeError`, import-time `SystemExit`, function not found, function not callable (same code), signature with too few args, signature kwargs-only rejected, signature with `*args` accepted.
- **`TestLoadBundleGateOrdering`** (1) — fingerprint check runs before module import; an attacker bundle with a wrong fingerprint and a broken module fails with `fingerprint_mismatch`, not `import_failed`.
- **`TestDiscoverBundles`** (6) — empty parent, non-existent parent, single bundle, multi-event bundle (both keys point at the same callable), allowlist filters registry without unloading the bundle, empty allowlist registers nothing.
- **`TestDiscoverBundlesCollisions`** (2) — two bundles same event_type fail closed; partial collision preserves unique event_types from each bundle.
- **`TestDiscoverBundlesNonBundleEntries`** (2) — loose files (README, INDEX) at the parent are silently skipped; non-bundle subdirectories fail with `manifest_missing` (every walked directory is accounted for, no silent skips of children).
- **`TestBundleLoaderEndToEnd`** (2) — runs the **real** `compile_extractor` to produce a bundle, then loads it via `load_bundle` AND `discover_bundles`, invokes the loaded callable, and asserts behavioral parity with the handwritten reference. Proves the loader's contract holds for bundles produced by the rest of Phase C, not just hand-built fixtures.

Strict-trust-boundary regression groups (added in review):

- **Strict manifest validation** (20) — an 18-case parametrized test (`test_malformed_manifest_rejected_with_manifest_unreadable`) covers every shape the lenient `Manifest.from_json` would accept silently: `event_types: "xy"` (silent char-tuple coercion), empty / duplicate / non-string / empty-string `event_types` items, non-string `module_filename` / `function_name`, `module_filename` without `.py`, double-dot stems, Python-keyword stems, empty strings, dashed function names, integer / empty `fingerprint`, unknown extra fields, missing required fields. Plus root-array rejection (`test_malformed_manifest_root_array_rejected`) and invalid-UTF-8 rejection (`test_invalid_utf8_manifest_rejected`).
- **Path-traversal defense** (2) — `module_filename: "../escape.py"` doesn't import a sibling file outside the bundle (test plants an `escape.py` that raises if imported and confirms it's never touched); `module_filename: "/etc/passwd.py"` rejected.
- **`sys.modules` cleanup** (1) — five repeated `load_bundle` calls leave the `__loaded_<uuid>` count in `sys.modules` unchanged; the captured callable still works after cleanup.
- **`discover_bundles` `iterdir` failure** (1) — `pathlib.Path.iterdir` monkeypatched to raise `PermissionError`; discovery returns a structured `manifest_missing` failure naming the underlying error rather than propagating.

## Out of scope (deferred to later C2 PRs)

- **Per-event / per-field / per-node / per-edge fallback.** When a compiled extractor is rejected (or a #76 validator failure on its output is recoverable), what does the runtime do? That's C2.b.
- **BigQuery-table bundle mirror.** Cross-process bundle distribution and the in-repo / BQ-mirror choice. Loader stays filesystem-only for C2.a.
- **Ontology-graph call-site swap.** Where in the orchestrator does the discovered registry actually replace the existing extractors? The integration moves once C2.a (loader) and C2.b (fallback) are both in.
- **Revalidation harness.** Scheduled / on-demand agreement check between compiled and reference outputs.

## Related

- [`extractor_compilation_runtime_target.md`](extractor_compilation_runtime_target.md) — the RFC that decided client-side Python is the Phase 1 runtime target. C2.a is the trust boundary that decision needs.
- [`extractor_compilation_scaffolding.md`](extractor_compilation_scaffolding.md) — `Manifest` + `compile_extractor` shape the loader consumes.
- [`extractor_compilation_bka_measurement.md`](extractor_compilation_bka_measurement.md) — PR 4c's measurement utility loads bundles a different way (per-call, for parity comparison); the public loader here is what an *orchestrator* will use.
