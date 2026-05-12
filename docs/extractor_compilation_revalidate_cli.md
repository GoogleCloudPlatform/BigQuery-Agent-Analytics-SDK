# Compiled Structured Extractors — `bqaa-revalidate-extractors` CLI

**Status:** Implemented (Phase C operationalization, follow-up to issue #75 Milestone C2.d)
**Parent epic:** [issue #75](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/75)
**Builds on:** [`extractor_compilation_revalidation.md`](extractor_compilation_revalidation.md), [`extractor_compilation_bundle_loader.md`](extractor_compilation_bundle_loader.md)

---

## What this is

A one-shot CLI binary that runs `revalidate_compiled_extractors` against local inputs so operators can periodically check the compiled extractor path without writing Python. This first PR keeps the input surface deliberately small — **local inputs only**. A follow-up adds `--events-bq-query` once the CLI contract is stable; that path drags in auth / location / pagination / error handling and is worth isolating.

## Usage

```bash
bqaa-revalidate-extractors \
    --bundles-root /var/bqaa/synced-bundles \
    --events-jsonl events.jsonl \
    --reference-extractors-module my_project.references \
    --thresholds-json thresholds.json \
    --report-out report.json
```

## Flags

| Flag | Required | Description |
|------|----------|-------------|
| `--bundles-root` | yes | Directory containing one subdirectory per compiled bundle (the layout `discover_bundles` walks). Fingerprint is **auto-detected** from the first bundle's manifest; every other bundle must declare the same fingerprint or sync fails with exit 2. |
| `--events-jsonl` | yes | Path to a JSONL file (one event JSON object per line). Empty lines are skipped; malformed lines abort with exit 2 naming the line number. |
| `--reference-extractors-module` | yes | Dotted Python path to a module exposing the reference-module contract below. |
| `--thresholds-json` | no | Optional JSON file mapping `RevalidationThresholds` field names to numeric rates in `[0, 1]`. When omitted, no threshold check is performed and exit is 0 on a successful run. |
| `--report-out` | yes | Path to write the combined JSON report. Parent directories are NOT created automatically; a missing parent directory fails at preflight with exit 2 before any work runs (no report written). Other write errors (permissions, disk full) also surface as clean exit 2. |

## Reference module contract

The dotted-path module passed to `--reference-extractors-module` must expose, at module scope:

```python
EXTRACTORS: dict[str, Callable[[dict, Any], StructuredExtractionResult]]
RESOLVED_GRAPH: ResolvedGraph     # output of resolve(ontology, binding)
SPEC: Any = None                  # optional; forwarded to extractor calls
```

- **`EXTRACTORS`** — same shape `revalidate_compiled_extractors` accepts (event_type → callable).
- **`RESOLVED_GRAPH`** — the validator-input artifact. The CLI doesn't carry ontology / binding flags because the reference module is the operational contract that defined both the event_type-to-callable mapping AND the spec they validate against. One module, one contract.
- **`SPEC`** — optional. Defaults to `None` to match the harness's keyword default.

A module missing either `EXTRACTORS` or `RESOLVED_GRAPH`, or with `EXTRACTORS` of the wrong shape, fails fast at the CLI boundary (exit 2) — the harness never sees a malformed registry.

## Exit codes

Intentionally narrow so cron / GitHub Actions can branch on them:

| Code | Meaning |
|------|---------|
| `0` | Revalidation completed; if thresholds were supplied, every threshold passed. |
| `1` | Revalidation completed but at least one threshold was violated. The report JSON is still written; the caller inspects `threshold_check.violations`. |
| `2` | Usage / load / input error: bad flags (missing required, unrecognized), missing files, malformed JSONL, missing reference module surface, mixed-fingerprint bundle root, threshold validation failure, etc. The report is **not** written. `main(argv)` *returns* this code rather than raising `SystemExit` (argparse's own `error()` is routed through the same `_CliError` boundary). `--help` still terminates via `SystemExit(0)` — that's the expected behavior. The CLI does not define a `--version` action today. |

## Report JSON shape

```json
{
  "report": {
    "total_events": ...,
    "total_compiled_unchanged": ...,
    "total_compiled_filtered": ...,
    "total_fallback_for_event": ...,
    "total_compiled_path_faults": ...,
    "total_parity_matches": ...,
    "total_parity_divergences": ...,
    "total_parity_not_checked": ...,
    "skipped_events": ...,
    "counts_by_event_type": { ... },
    "sample_decision_divergences": [ ... ],
    "sample_parity_divergences":   [ ... ],
    "started_at": "...",
    "finished_at": "..."
  },
  "threshold_check": null | {
    "ok":         true|false,
    "violations": ["compiled_unchanged_rate 0.2500 < min 0.9500", ...]
  }
}
```

`threshold_check` is `null` when `--thresholds-json` wasn't supplied; the raw report is still written so an operator can inspect rates without committing to a gate.

## Thresholds JSON shape

Any subset of `RevalidationThresholds` fields, with numeric rates in `[0, 1]`:

```json
{
  "min_compiled_unchanged_rate":    0.95,
  "max_compiled_filtered_rate":     0.05,
  "max_fallback_for_event_rate":    0.05,
  "max_compiled_path_fault_rate":   0.01,
  "min_parity_match_rate":          0.99
}
```

Unknown fields, out-of-range rates (`5.0` intended as 5%), NaN, and bool all fail at the CLI boundary with exit 2 — same `__post_init__` validation that `RevalidationThresholds` enforces in-process.

## What gets skipped

- **Events whose `event_type` isn't in `EXTRACTORS` or the compiled registry** land in `report.skipped_events`; they don't enter the rate denominators.
- **Empty JSONL lines** are silently skipped; that's whitespace, not data.
- **Malformed JSONL lines** are **not** skipped — they abort the run with exit 2 to distinguish corrupt input from legitimately-uncovered event_types.

## Tests

`tests/test_extractor_compilation_cli_revalidate.py` (20 cases):

- **`TestCliEndToEnd`** (3) — happy path (exit 0, report written, `threshold_check: null`); threshold pass (exit 0, `ok: true`); threshold violation (exit 1, report still written with violations listed).
- **`TestCliUsageErrors`** (16) — missing events file; malformed JSONL line; missing bundles root; mixed-fingerprint bundle root; empty bundle root; reference module not importable; reference module missing `EXTRACTORS`; reference module missing `RESOLVED_GRAPH`; bad `EXTRACTORS` shape; thresholds JSON with unknown field; thresholds JSON with out-of-range rate; missing `--report-out` parent directory (preflight catches it before any work runs); invalid UTF-8 in `--events-jsonl`; invalid UTF-8 in `--thresholds-json`; **missing required flag returns 2 (not `SystemExit`)**; **unrecognized flag returns 2 (not `SystemExit`)** — argparse's default `error()` is overridden to route through `_CliError` so `main(argv)` reliably *returns* an exit code rather than raising `SystemExit` mid-call.
- **`test_console_script_entry_point_registered`** (1) — locks the `pyproject.toml` `[project.scripts]` entry so a typo in the entry-point string fails CI rather than breaking the binary at user-install time.

## Out of scope (deferred)

- **`--events-bq-query`** — load events from a BigQuery query. Follow-up PR; brings auth + location + pagination + error handling.
- **Scheduled execution** — operator owns cron / Cloud Scheduler / GitHub Actions; the CLI is a one-shot.
- **BQ persistence of reports** — `--report-out` writes a local file; pushing it elsewhere is the caller's concern.
- **Multiple bundle roots** — one fingerprint per run; the harness is designed for "what's currently deployed."

## Related

- [`extractor_compilation_revalidation.md`](extractor_compilation_revalidation.md) — the underlying `revalidate_compiled_extractors` + `check_thresholds` API. The CLI is a thin operational wrapper around it.
- [`extractor_compilation_bundle_loader.md`](extractor_compilation_bundle_loader.md) — `discover_bundles` is what the CLI uses internally to load compiled extractors.
- [`extractor_compilation_bq_bundle_mirror.md`](extractor_compilation_bq_bundle_mirror.md) — `sync_bundles_from_bq` is the typical upstream of `--bundles-root` for Cloud-Run-style deployments.
