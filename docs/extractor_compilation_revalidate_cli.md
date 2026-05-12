# Compiled Structured Extractors — `bqaa-revalidate-extractors` CLI

**Status:** Implemented (Phase C operationalization, follow-up to issue #75 Milestone C2.d)
**Parent epic:** [issue #75](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/75)
**Builds on:** [`extractor_compilation_revalidation.md`](extractor_compilation_revalidation.md), [`extractor_compilation_bundle_loader.md`](extractor_compilation_bundle_loader.md)

---

## What this is

A one-shot CLI binary that runs `revalidate_compiled_extractors` against local OR BigQuery inputs so operators can periodically check the compiled extractor path without writing Python.

## Usage

**Local JSONL events:**

```bash
bqaa-revalidate-extractors \
    --bundles-root /var/bqaa/synced-bundles \
    --events-jsonl events.jsonl \
    --reference-extractors-module my_project.references \
    --thresholds-json thresholds.json \
    --report-out report.json
```

**BigQuery events:**

```bash
bqaa-revalidate-extractors \
    --bundles-root /var/bqaa/synced-bundles \
    --events-bq-query-file events_query.sql \
    --bq-project my-project \
    --bq-location US \
    --reference-extractors-module my_project.references \
    --thresholds-json thresholds.json \
    --report-out report.json
```

Where `events_query.sql` returns exactly one column named `event_json` (STRING) containing a JSON-encoded event dict per row. The SQL file must be **fully self-contained** — the CLI does not accept query parameters, so substitute concrete literals before invoking:

```sql
-- events_query.sql — bake in literal time bounds before running.
SELECT TO_JSON_STRING(STRUCT(
  event_type,
  span_id,
  session_id,
  content
)) AS event_json
FROM `my-project.my_dataset.agent_events`
WHERE event_timestamp BETWEEN TIMESTAMP('2026-05-01') AND TIMESTAMP('2026-05-12')
LIMIT 10000
```

## Flags

| Flag | Required | Description |
|------|----------|-------------|
| `--bundles-root` | yes | Directory containing one subdirectory per compiled bundle (the layout `discover_bundles` walks). Fingerprint is **auto-detected** from the first bundle's manifest; every other bundle must declare the same fingerprint or sync fails with exit 2. |
| `--events-jsonl` | one of | Path to a JSONL file (one event JSON object per line). Empty lines are skipped; malformed lines abort with exit 2 naming the line number. Mutually exclusive with `--events-bq-query-file`; exactly one must be supplied. |
| `--events-bq-query-file` | one of | Path to a `.sql` file whose query returns one column named `event_json` (STRING) per row. The CLI does not auto-shape `bigquery.Row` objects — the query writer controls projection. Mutually exclusive with `--events-jsonl`; exactly one must be supplied. |
| `--bq-project` | no | BigQuery project ID for `--events-bq-query-file`. Optional: when omitted, the BigQuery client falls back to Application Default Credentials / environment for project inference. If both the flag is absent AND the inferred project is empty, the CLI exits 2 with `Set --bq-project explicitly`. |
| `--bq-location` | no | BigQuery location for `--events-bq-query-file`. Defaults to `US`. Ignored when `--events-jsonl` is used. |
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

## `--events-bq-query-file` contract

The SQL must produce **exactly one column** named `event_json` (STRING) per row. The column contains a JSON-encoded event dict — same shape `--events-jsonl` consumes line-by-line. The CLI does not auto-shape `bigquery.Row` objects, which keeps the path predictable: the query writer is the one place that knows the table schema, and `TO_JSON_STRING(STRUCT(...))` is the standard wrap.

**Error handling:**

| Failure | Behavior |
|---------|----------|
| BigQuery client construction fails (auth, ADC, invalid credentials, network) | exit 2 with `BigQuery client construction failed: <Type>: <message>` |
| BigQuery query raises (syntax, table-not-found, permission) | exit 2 with `BigQuery query failed: <Type>: <message>` |
| Query returns extra columns beyond `event_json` (checked via `job.schema`, so caught even when the result is empty) | exit 2 with `query must produce exactly one column named 'event_json'; got [...]` |
| Row missing the `event_json` column | exit 2 with `row N: missing required column 'event_json'` |
| `event_json` value isn't a STRING (e.g. STRUCT projected without `TO_JSON_STRING`) | exit 2 with `row N: 'event_json' must be STRING; got <type>` |
| `event_json` STRING isn't valid JSON | exit 2 with `row N: invalid JSON in 'event_json': <msg>` |
| `event_json` decodes to non-object (array, scalar) | exit 2 with `row N: 'event_json' decodes to <type>, expected a JSON object` |
| Empty `.sql` file | exit 2 with `... is empty` |
| Invalid UTF-8 in `.sql` file | exit 2 with `not valid UTF-8` |

The row index is the **0-based position in the result set**, so an operator can `LIMIT N OFFSET row_index` to find the offending row.

**Project resolution:** `--bq-project` is optional. The BigQuery client tries Application Default Credentials / environment when the flag isn't set. If both fall through to an empty project, the CLI exits 2 with `--bq-project not provided and the BigQuery client could not infer a project ... Set --bq-project explicitly.` rather than letting a downstream BigQuery API error confuse the operator.

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

CI suite — `tests/test_extractor_compilation_cli_revalidate.py` (35 cases, 1 skipped under dev-install):

- **`TestCliEndToEnd`** (3) — happy path (exit 0, report written, `threshold_check: null`); threshold pass (exit 0, `ok: true`); threshold violation (exit 1, report still written with violations listed).
- **`TestCliUsageErrors`** (18) — missing events file; malformed JSONL line; missing bundles root; mixed-fingerprint bundle root; empty bundle root; reference module not importable; reference module missing `EXTRACTORS`; reference module missing `RESOLVED_GRAPH`; bad `EXTRACTORS` shape; thresholds JSON with unknown field; thresholds JSON with out-of-range rate; missing `--report-out` parent directory (preflight catches it before any work runs); invalid UTF-8 in `--events-jsonl`; invalid UTF-8 in `--thresholds-json`; missing required flag returns 2 (not `SystemExit`); unrecognized flag returns 2 (not `SystemExit`); **both `--events-jsonl` and `--events-bq-query-file` returns 2** (argparse mutex `not allowed with`); **neither event source returns 2** (argparse mutex `one of the arguments ... is required`).
- **`TestCliEventsBQ`** (13) — BigQuery event-source paths, monkeypatching `_make_bq_client` to inject an in-memory fake:
  - Happy path: 2 `event_json` rows, valid JSON, BKA shape → exit 0, report includes both events.
  - Project inferred from ADC: `_FakeBQClient(project="adc-project")` is accepted without `--bq-project`.
  - No project anywhere: `bigquery.Client()` returns a project-less client AND `--bq-project` absent → exit 2 with `Set --bq-project explicitly`.
  - **Client construction failure** (`_make_bq_client` raises `RuntimeError("could not authenticate")`) → exit 2 with `BigQuery client construction failed: ...`. Distinguishes auth/ADC failures from query-time failures.
  - Query exception (`RuntimeError("table not found")`) → exit 2 with `BigQuery query failed: RuntimeError: table not found`.
  - **Extra column rejected (non-empty result)**: row with `event_json` + `extra_col` keys → exit 2 with `exactly one column ... got ['event_json', 'extra_col']`. Enforces the documented contract; report not written.
  - **Extra column rejected on empty result set**: zero-row query (`SELECT event_json, extra_col FROM t WHERE FALSE`) with a wrong schema → still exit 2. Locks the choice to validate via `job.schema` (populated regardless of row count) rather than just first-row keys, which would silently accept a wrong-schema empty result.
  - **Correct schema + empty result set**: zero rows with the correct one-column schema → exit 0 with a zero-event report. An empty-but-well-shaped result is a valid revalidation outcome, not an error.
  - Row missing `event_json` column → exit 2 with `row 1` (the second row).
  - `event_json` non-string (dict) → exit 2 with `row 0: 'event_json' must be STRING`.
  - `event_json` invalid JSON → exit 2 with `row 0: invalid JSON`.
  - `event_json` decodes to JSON array (not object) → exit 2 with `row 0: ... expected a JSON object`.
  - Empty `.sql` file → exit 2 with `is empty`.
- **`test_console_script_entry_point_registered`** (1) — locks the `pyproject.toml` `[project.scripts]` entry so a typo in the entry-point string fails CI rather than breaking the binary at user-install time.

Live BQ suite — `tests/test_extractor_compilation_cli_revalidate_bq_live.py` (1 case), gated behind `BQAA_RUN_LIVE_TESTS=1` + `BQAA_RUN_LIVE_BQ_REVALIDATE_TESTS=1` + `PROJECT_ID` + `DATASET_ID`. Creates a temp table, inserts two `event_json` rows, runs the CLI, asserts the report is written with both events as compiled_unchanged + parity_matches, deletes the table on the way out.

## Out of scope (deferred)

- **Pagination strategy** — `client.query(...).result()` already paginates; the CLI iterates the full result set into memory. For ultra-large corpora a follow-up could add `--events-row-limit` or stream-based aggregation, but the current shape handles tens of thousands of events comfortably.
- **Scheduled execution** — operator owns cron / Cloud Scheduler / GitHub Actions; the CLI is a one-shot.
- **BQ persistence of reports** — `--report-out` writes a local file; pushing it elsewhere is the caller's concern.
- **Multiple bundle roots** — one fingerprint per run; the harness is designed for "what's currently deployed."
- **Auto-row-shape inference** — explicit non-goal. The `event_json` single-column contract keeps the CLI predictable; the query writer owns projection.

## Related

- [`extractor_compilation_revalidation.md`](extractor_compilation_revalidation.md) — the underlying `revalidate_compiled_extractors` + `check_thresholds` API. The CLI is a thin operational wrapper around it.
- [`extractor_compilation_bundle_loader.md`](extractor_compilation_bundle_loader.md) — `discover_bundles` is what the CLI uses internally to load compiled extractors.
- [`extractor_compilation_bq_bundle_mirror.md`](extractor_compilation_bq_bundle_mirror.md) — `sync_bundles_from_bq` is the typical upstream of `--bundles-root` for Cloud-Run-style deployments.
