# Compiled Structured Extractors — BKA-Decision Measurement (PR 4c)

**Status:** Implemented (PR 4c of issue #75 Phase C)
**Parent epic:** [issue #75](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/75)
**Builds on:** [`extractor_compilation_retry_loop.md`](extractor_compilation_retry_loop.md) (PR 4b.2.2.c.2), [`extractor_compilation_diagnostics.md`](extractor_compilation_diagnostics.md) (PR 4b.2.2.c.1)
**Working plan:** issue #96, Milestone C1 / PR 4c

---

## What this is

The **end-to-end proof** that the Phase C compile-with-LLM pipeline can produce a working compiled extractor for our canonical example: `extract_bka_decision_event`. Plus a generic **compile-and-measure utility** future Phase-C extractor baselines can reuse.

Two parts:

1. **Generic utility** — `measure_compile(...)` in `extractor_compilation.measurement`. Runs `compile_with_llm`, loads the resulting bundle's compiled callable, runs both that callable and a *reference* extractor on the same sample events, and produces a structured `CompileMeasurement` record. Loop failure is captured in the record (no exception); parity divergences are surfaced as human-readable strings.
2. **BKA-specific concrete measurement** — fixtures + tests + a checked-in measurement artifact that proves the pipeline produces parity with `extract_bka_decision_event` on the canonical sample events.

The CI path is **deterministic** — uses a fake `LLMClient` that returns the canonical resolved plan dict, so the merge-blocking proof requires no API key. A separate **gated live path** runs the same pipeline against real BigQuery rows and a real LLM.

## Public API

```python
from bigquery_agent_analytics.extractor_compilation import (
    CompileMeasurement,
    measure_compile,
)

measurement: CompileMeasurement = measure_compile(
    extraction_rule={...},
    event_schema={...},
    sample_events=[...],
    reference_extractor=extract_bka_decision_event,
    spec=spec,
    llm_client=my_llm_client,
    compile_source=my_compile_fn,
    max_attempts=5,
    model_name="gemini-2.5-flash",         # or DETERMINISTIC_FAKE_MODEL
    source="live:proj.dataset.agent_events",
)

assert measurement.ok            # compile succeeded AND parity held
assert measurement.parity_ok
print(measurement.to_json())      # JSON-serializable for artifact storage
```

## `CompileMeasurement` shape

```
ok                          : bool        # compile loop ok AND parity ok
n_attempts                  : int         # 1 iff first plan compiled clean
reason                      : str         # "succeeded" | "max_attempts_reached"
bundle_fingerprint          : str | None  # sha256 hex; None on loop failure
attempt_failures            : tuple[str]  # one stable code per failed attempt
                                          # e.g. "plan_parse_error:missing_required_field"
                                          #      "compile:invalid_event_types"
                                          #      "render_error"

parity_ok                   : bool
n_events                    : int
n_events_with_node_match    : int         # split per axis so single-axis
n_events_with_span_match    : int         # divergences are easy to triage
parity_divergences          : tuple[str]  # human-readable per-event diffs

# Audit fields (live runs in particular benefit from these)
captured_at                 : str         # UTC ISO timestamp
model_name                  : str         # "deterministic-fake" or e.g. "gemini-2.5-flash"
source                      : str         # "deterministic" or "live:proj.dataset.table"
sample_session_ids          : tuple[str]  # session_ids in the sample events
```

`CompileMeasurement.to_json()` / `.from_json(...)` round-trip is byte-stable for equal inputs.

## What "parity" means

For each sample event, both the reference and the compiled extractor are called with `(event, spec)`. The resulting `StructuredExtractionResult`s are compared on three axes:

- **Node set** — same `node_id` values, and for each shared `node_id` the `entity_name`, sorted `labels`, and property `(name, value)` set are equal.
- **Fully-handled span IDs** — set equality.
- **Partially-handled span IDs** — set equality.

Edges aren't compared (the renderer doesn't emit edges yet, per the renderer docstring); a future renderer extension that emits edges will need to extend the comparator alongside.

A divergence in *any* axis sets `parity_ok=False` and appends a human-readable string to `parity_divergences`. The per-axis match counts (`n_events_with_node_match`, `n_events_with_span_match`) let a reviewer triage which axis broke without parsing the divergence strings.

## CI path (deterministic, merge-blocking)

`tests/test_extractor_compilation_measurement.py` (26 tests):

- **`TestMeasureCompileBkaHappyPath`** — first-try success: `DeterministicBkaPlanClient` emits the canonical plan; `compile_with_llm` produces a valid bundle in 1 attempt; the compiled extractor's output matches `extract_bka_decision_event` on both sample events.
- **`TestMeasureCompileLoopFailure`** — measurement returns a populated record (not an exception) when the loop exhausts; `attempt_failures` contains one stable code per failed attempt.
- **`TestMeasureCompileParityDivergence`** — property-set divergence rendered correctly; reference-extractor exception captured as a structured divergence (`event[i]: reference extractor raised X: msg`); compiled-extractor exception captured at the helper level (symmetric branch).
- **`TestCompileMeasurementJson`** — `to_json` / `from_json` round-trip is byte-stable; sorted keys; failure records round-trip cleanly.
- **`TestCompileMeasurementFromJsonStrictTypes`** — the artifact's schema lock is **load-bearing**:
  - **Exact keys.** JSON top-level keys must equal the dataclass field set; unknown fields raise `TypeError` (listing names), missing fields raise `TypeError` (listing names), both shapes of drift are reported together.
  - **Strict types.** Each field is checked via `isinstance` (and `type(v) is bool` for the bool fields, so `int 0` doesn't sneak past as `False`). Constructor-style coercion (`bool("false")`, `tuple("abc")`, `int("3")`) is rejected with `TypeError` naming the offending field.
- **`TestBkaFingerprintInputsCoverage`** — `BKA_FINGERPRINT_INPUTS` covers every field the extractor emits, so a real compile-input change moves the bundle hash. Three regression tests: changing `event_schema.bka_decision.content.alternatives_considered`, removing it from `extraction_rules.bka_decision.property_fields`, and re-pathing `span_handling.partial_when_path` each produce a different `compute_fingerprint` output. Pins C2's "fingerprint matches active inputs" contract.
- **`TestMeasurementAuditFields`** — `sample_session_ids` dedup'd in iteration order with empty strings filtered; `model_name` / `source` pass through verbatim; the checked-in measurement artifact parses via the strict `CompileMeasurement.from_json` (locks both the artifact schema and the dataclass shape in one assertion).

These run on every PR. No API key required.

## Live path (gated, regenerates the artifact)

`tests/test_extractor_compilation_bka_compile_live.py` is gated on **both**:

- `BQAA_RUN_LIVE_TESTS=1` (the project-wide live-test gate already used by `test_ai_generate_judge_live.py`)
- `BQAA_RUN_LIVE_LLM_COMPILE_TESTS=1` (this test specifically — opting into LLM cost)

Plus runtime config:

- `PROJECT_ID` / `DATASET_ID` — BigQuery project + dataset containing `agent_events`
- `BQAA_LLM_COMPILE_MODEL` — defaults to `gemini-2.5-flash`

When all gates pass, the live test:

1. Pulls a pool of `bka_decision` rows from the live `agent_events` table (default pool: 50).
2. Filters out rows without `content.decision_id` (the reference can't produce a node for those — they'd be dead weight in parity).
3. Partitions the remaining rows by `content.reasoning_text` presence and **`pytest.skip`s if either partition is empty**. Running the live LLM compile without both branches represented would leave the doc claim ("both span-handling branches proven") unverified; the test names the missing branch in the skip message.
4. Takes a balanced sample (up to 5 from each branch) and runs `measure_compile(...)` against `extract_bka_decision_event` as the reference.
5. Constructs a thin `LLMClient` adapter around `google.genai` (in-test; provider adapters are out of scope for the SDK core).
6. Writes the resulting `CompileMeasurement` JSON to `tests/fixtures_extractor_compilation/bka_decision_measurement_report.json`.
7. Asserts **contract-level invariants only** — *not* exact LLM wording:
   - `ok=True`
   - `n_attempts <= 3`
   - `parity_ok=True`
   - `parity_divergences=()`
   - `n_events >= 2`
   - sample covers both span-handling branches (defense in depth: the fixture should already have skipped, but a future refactor that loosens the partition guard would fail the test rather than silently weaken the live proof)
   - bundle directory exists and the compiled module is importable

The live test exists because there's a class of failure (prompt drift, model regression) that mocks can't catch — and it's the natural artifact-regeneration path. It is **not** part of the PR-merging contract.

## Measurement artifact

[`tests/fixtures_extractor_compilation/bka_decision_measurement_report.json`](../tests/fixtures_extractor_compilation/bka_decision_measurement_report.json) is the most recent successful measurement, checked into the repo. It's:

- **Generated** by the deterministic happy-path test capture (initial commit) or the live run (subsequent regenerations).
- **Asserted against** by `test_checked_in_artifact_round_trips_into_compile_measurement` — locks the artifact's schema and the `ok=True` / `parity_ok=True` shape so a schema break can't slip through.
- **Diffable** across runs — sorted JSON keys mean any change shows up as a localized diff in code review.

The bundle fingerprint changes with compiler / template / spec version bumps; the test asserts shape (sha256 hex, 64 chars), not value, so version bumps don't churn the artifact unnecessarily.

## Out of scope

- **Provider adapters in the SDK core.** The live test wires up `google.genai` inline — the SDK core stays adapter-agnostic. Concrete adapters land separately if and when there's enough cross-test reuse to justify a public surface.
- **Multi-extractor measurement suite.** This PR ships one concrete measurement (BKA decision). Future Phase-C extractor baselines reuse `measure_compile` with their own fixtures.
- **Cost / token instrumentation.** The measurement record captures attempt count and outcome but not LLM token usage. Adding token / cost fields is a follow-up; the dataclass is `frozen=True` so any extension is a deliberate breaking change.

## Related

- [`extractor_compilation_retry_loop.md`](extractor_compilation_retry_loop.md) — `compile_with_llm` and `RetryCompileResult`, which `measure_compile` wraps.
- [`extractor_compilation_diagnostics.md`](extractor_compilation_diagnostics.md) — diagnostic builders that produce the `attempt_failures` codes via the loop.
- [`extractor_compilation_scaffolding.md`](extractor_compilation_scaffolding.md) — `compile_extractor` / `Manifest` shapes the measurement record snapshots.
