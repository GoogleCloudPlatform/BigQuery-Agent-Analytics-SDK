# Compiled Structured Extractors — BigQuery Bundle Mirror (PR C2.c.3)

**Status:** Implemented (PR C2.c.3 of issue #75 Phase C / Milestone C2)
**Parent epic:** [issue #75](https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/issues/75)
**Builds on:** [`extractor_compilation_bundle_loader.md`](extractor_compilation_bundle_loader.md) (PR C2.a), [`extractor_compilation_orchestrator_swap.md`](extractor_compilation_orchestrator_swap.md) (PR C2.c.2)
**Working plan:** issue #96, Milestone C2 / PR C2.c.3

---

## What this is

Compiled bundles live on the filesystem and are loaded by `load_bundle` / `discover_bundles` (C2.a). This module adds a **publish/sync utility** so bundles can flow between processes via a BigQuery table — useful for Cloud Run, Cloud Functions, ephemeral CI workers, or any environment where the filesystem isn't shared.

**The mirror is a utility, not a runtime loader.** The runtime path stays unchanged:

```
sync_bundles_from_bq  →  discover_bundles  →  from_bundles_root
```

Sync writes verified files to a local directory and lets C2.a's existing loader do the actual import. There is no "fetch-direct-from-BQ" loader — that would double the trust surface and diverge from the loader's audit fields.

## Public API

```python
from bigquery_agent_analytics.extractor_compilation import (
    publish_bundles_to_bq,
    sync_bundles_from_bq,
    BigQueryBundleStore,
    BUNDLE_MIRROR_TABLE_SCHEMA,
    PublishResult,
    SyncResult,
    MirrorFailure,
    BundleRow,
    BundleStore,
)
from google.cloud import bigquery

# 1. Stand up the store (creates the table if missing).
client = bigquery.Client(project="my-project", location="US")
store = BigQueryBundleStore(
    bq_client=client,
    table_id="my-project.my_dataset.compiled_bundles",
)
store.ensure_table()

# 2. Publish local bundles to BigQuery.
publish: PublishResult = publish_bundles_to_bq(
    bundle_root=pathlib.Path("/var/bqaa/bundles"),
    store=store,
    bundle_fingerprint_allowlist=None,  # or a list of fingerprints
)

# 3. Elsewhere (different process / VM / Cloud Run instance):
sync: SyncResult = sync_bundles_from_bq(
    store=store,
    dest_dir=pathlib.Path("/tmp/synced-bundles"),
    bundle_fingerprint_allowlist=None,
)

# 4. Wire the synced dir into the runtime via C2.a.
from bigquery_agent_analytics.ontology_graph import OntologyGraphManager
manager = OntologyGraphManager.from_bundles_root(
    project_id="my-project",
    dataset_id="my_dataset",
    ontology=ontology,
    binding=binding,
    bundles_root=sync.dest_dir,
    expected_fingerprint=fingerprint,
    fallback_extractors=fallback_extractors,
)
```

## Per-bundle flow

**Publish:**

1. Walk `bundle_root`; for each subdirectory, read `manifest.json` and parse via `Manifest.from_json`.
2. Skip if `bundle_fingerprint_allowlist` is set and the manifest's fingerprint isn't in it (lands in `skipped_fingerprints`).
3. Run `load_bundle(child, expected_fingerprint=manifest.fingerprint)` as a **pre-publish validation gate**. Bundles that wouldn't load at runtime are NOT published; the mirror only distributes working bundles. Failures land in `failures` with code `bundle_load_failed` and the underlying loader code in `detail`.
4. Emit two `BundleRow`s per bundle (manifest + module file) with denormalized `event_types` / `module_filename` / `function_name` for query-side filtering.
5. Call `store.publish_rows(rows)` once for the whole batch. `BigQueryBundleStore` issues `DELETE FROM ... WHERE (fingerprint, path) IN (...)` then `INSERT`, so re-publishing the same fingerprint replaces the prior rows rather than accumulating.

**Sync:**

1. Fetch rows via `store.fetch_rows(bundle_fingerprints=allowlist)`.
2. Shape-check each row (`malformed_row` if wrong types).
3. Group by fingerprint; per fingerprint:
   - Reject if any row's `bundle_path` is unsafe (`invalid_bundle_path`: traversal, absolute path, NUL, backslash).
   - Reject if any `(fingerprint, bundle_path)` pair appears twice (`duplicate_row`).
   - Require the `manifest.json` row (`manifest_row_missing`) and parse it (`manifest_row_unreadable`).
   - Require exactly the manifest's two files (`unexpected_file` if any extra; `module_row_missing` if the module row is absent).
   - Write the two files into `dest_dir/<fingerprint>/`.
   - Run `load_bundle(dest_dir/<fingerprint>, expected_fingerprint=fp)` as a **post-sync validation gate**. Tampered or incomplete bundles fail at sync (`bundle_load_failed`) and the partial directory is scrubbed.
4. Fingerprints in the allowlist that have no rows surface as `fingerprint_not_in_table` failures — the operator knows the publish lag hasn't caught up.

## BQ table schema

`BUNDLE_MIRROR_TABLE_SCHEMA` (tuples of `(name, type, mode)`):

```
bundle_fingerprint  STRING     REQUIRED
bundle_path         STRING     REQUIRED      -- "manifest.json" or the manifest's module_filename
file_content        BYTES      REQUIRED
event_types         STRING     REPEATED      -- denorm from manifest, for query-side filter
module_filename     STRING     NULLABLE      -- denorm from manifest
function_name       STRING     NULLABLE      -- denorm from manifest
published_at        TIMESTAMP  REQUIRED
```

**Logical primary key**: `(bundle_fingerprint, bundle_path)`. BigQuery doesn't enforce uniqueness; `BigQueryBundleStore.publish_rows` enforces it via DELETE+INSERT, and sync rejects duplicates fail-closed.

The denormalized fields exist for query convenience (`SELECT DISTINCT bundle_fingerprint FROM mirror WHERE 'bka_decision' IN UNNEST(event_types)`). They are NOT the source of truth at sync time — sync re-parses `manifest.json` from the row content. The denorm is for query speed; correctness comes from re-validating against `load_bundle`.

## Stable failure codes

Callers can switch on `MirrorFailure.code`:

Publish-side:
- `bundle_root_missing` — `bundle_root` is not a directory.
- `manifest_missing` — bundle subdir has no `manifest.json`.
- `manifest_unreadable` — manifest fails to parse or has wrong shape.
- `bundle_load_failed` — bundle wouldn't load via `load_bundle` pre-publish. `detail` carries the underlying loader code.
- `duplicate_fingerprint` — two or more subdirs of `bundle_root` declare the same manifest fingerprint. The mirror is keyed on `(bundle_fingerprint, bundle_path)`; publishing both would land contents-of-the-loser in the table and corrupt the bundle identity. Fail-closed: every participating subdir gets a failure record and no rows are emitted for that fingerprint.

Sync-side:
- `fingerprint_not_in_table` — allowlist named a fingerprint with no rows.
- `manifest_row_missing` — bundle has rows but no `manifest.json` row.
- `manifest_row_unreadable` — manifest row content isn't a valid `Manifest`. Also fires when the parsed manifest's shape would let a path-escape or write failure slip past `_validate_bundle_path` (`module_filename` containing a path separator, NUL, `.`/`..`, or non-string fields).
- `invalid_bundle_path` — traversal / absolute / NUL / backslash. Offender is never written to disk.
- `unexpected_file` — row whose `bundle_path` isn't `manifest.json` nor the manifest's `module_filename`. Bundles are exactly two files; anything extra is rejected.
- `module_row_missing` — manifest is fine but no row for the module file.
- `duplicate_row` — two rows share the same `(fingerprint, bundle_path)`.
- `malformed_row` — row fields have wrong types (e.g. `file_content` not bytes) **or** the `bundle_fingerprint` isn't a strict 64-char lowercase sha256 hex string. The fingerprint check is load-bearing: sync uses the fingerprint as a directory name (`dest_dir/<fingerprint>/`), so a tampered value like `"../escape"` would otherwise write outside `dest_dir`.
- `bundle_load_failed` — sync wrote files to a *staging* directory but `load_bundle` rejected the reconstruction. The staging directory is removed and any pre-existing `dest_dir/<fingerprint>/` is left intact — a bad mirror row never destroys good local state.

Neither `publish_bundles_to_bq` nor `sync_bundles_from_bq` raises on per-bundle problems; failures accumulate. **Store exceptions** (BQ-side: network, auth, table missing) DO propagate — that's the right boundary for "fix the connection and retry."

## Staged replace during sync

Sync writes each fingerprint's two files to a side-by-side **staging directory** (`dest_dir/.staging-<fingerprint>-<uuid>/`) and runs `load_bundle` on the staged copy **before touching the target**. Only after `load_bundle` accepts the staged reconstruction does sync `rmtree(dest_dir/<fingerprint>)` and `shutil.move(staging, target)`. A corrupt mirror row therefore cannot destroy a previously-good local bundle — the load-bundle gate is the safety boundary.

The replace itself is **staged, not strictly atomic.** Between `rmtree(target)` and `move(staging, target)` there is a brief window where the target is absent; a process crash inside that window leaves the bundle missing on disk (a re-sync recovers it). The load-bundle-failure case — the one the staged flow is designed to protect — is correctly atomic in the failure direction: load-bundle failure leaves the target untouched. Locked by `test_sync_failure_preserves_existing_good_bundle`.

## Idempotency + non-atomic publish

`BigQueryBundleStore.publish_rows` upserts by `(bundle_fingerprint, bundle_path)`. Re-publishing the same bundle replaces the prior rows rather than duplicating them — verified by `test_republishing_same_bundle_does_not_accumulate_rows`.

**Important caveat:** the DELETE + `insert_rows_json` upsert is **not a single atomic transaction**. If INSERT fails after DELETE (network, quota, schema drift), rows for the affected `(fingerprint, bundle_path)` pairs are *missing* from the table until the caller re-runs publish. The mirror is publish-side idempotent, so the recovery is to call `publish_bundles_to_bq` again — but operators should be aware that a transient INSERT failure leaves a recoverable, not silent, gap. A staging-table-plus-MERGE flow would close this gap and is deliberately deferred.

Cross-subdir duplicate fingerprints (two bundles claiming the same fingerprint) are caught **before any DELETE runs** via the `duplicate_fingerprint` publish-side check. `BigQueryBundleStore.publish_rows` also raises `ValueError` on duplicate `(fingerprint, path)` input pairs as defense in depth for direct callers of the store.

## Tests

CI suite — `tests/test_extractor_compilation_bq_bundle_mirror.py`, 24 cases using an in-memory `BundleStore` substitute:

- **`TestRoundTrip`** (2) — publish a local bundle, sync it back, verify `load_bundle` accepts the reconstruction. Plus a multi-bundle variant.
- **`TestAllowlist`** (3) — publish-side allowlist skips unlisted; sync-side allowlist skips unlisted; sync-side allowlist names a fingerprint with no rows → `fingerprint_not_in_table` failure.
- **`TestPathSafety`** (3) — `../escape.py`, `/etc/passwd`, `..\windows-style-escape.py` all rejected with `invalid_bundle_path`; no file written outside `dest_dir`.
- **`TestMissingAndMalformedRows`** (5) — missing manifest row, malformed manifest content, unexpected extra file, wrong field type, duplicate rows.
- **`TestIdempotentRepublish`** (1) — two consecutive publishes of the same bundle leave exactly two rows in the store, not four.
- **`TestPublishFailures`** (4) — subdir without manifest; bundle that would fail `load_bundle` pre-publish; missing `bundle_root`; two subdirs declaring the same fingerprint → `duplicate_fingerprint`, neither published.
- **`TestRoundTwoFindings`** (6) — manifest row with `module_filename` containing a path separator → `manifest_row_unreadable` (no `FileNotFoundError`); existing good local bundle preserved across a corrupt re-sync (staging-then-validate); `BigQueryBundleStore.publish_rows` raises `ValueError` on duplicate input pairs without running DELETE or INSERT; tampered `bundle_fingerprint="../escape"` rejected as `malformed_row` before any path is computed (no write outside `dest_dir`); tampered manifest `fingerprint="../escape"` rejected at publish-side; `BigQueryBundleStore.__init__` raises `ValueError` on malformed `table_id` (backtick, semicolon, whitespace, wrong dot count, empty segment, `--` comment marker, trailing newline) so injection can't reach the SQL.

Live BQ suite — `tests/test_extractor_compilation_bq_bundle_mirror_live.py`, 1 case behind `BQAA_RUN_LIVE_TESTS=1` + `BQAA_RUN_LIVE_BQ_MIRROR_TESTS=1` + `PROJECT_ID` + `DATASET_ID`. Creates a temporary table, runs the publish+sync round-trip, asserts `load_bundle` accepts the reconstruction, deletes the table on the way out.

## Out of scope (deferred)

- **GCS-backed signed-URL fetch** for very large bundles. Bundles are tiny today (a few KB); a streaming path can land later if real bundles grow.
- **Caching / TTL** of synced bundles. Sync overwrites; the caller decides how often to sync.
- **Garbage collection** of stale fingerprints. The mirror's job is publish + fetch; lifecycle policy lives upstream.
- **Multi-region replication.** The mirror table is created in one BQ location.

## Related

- [`extractor_compilation_bundle_loader.md`](extractor_compilation_bundle_loader.md) — `load_bundle` / `discover_bundles` (C2.a). The mirror calls `load_bundle` as both a pre-publish gate and a post-sync gate, so the loader is the single source of truth for "is this bundle usable?"
- [`extractor_compilation_orchestrator_swap.md`](extractor_compilation_orchestrator_swap.md) — `OntologyGraphManager.from_bundles_root` (C2.c.2). Once sync lands bundles on disk, this is the entry point that wires them into the runtime.
- [`extractor_compilation_runtime_registry.md`](extractor_compilation_runtime_registry.md) — `build_runtime_extractor_registry` (C2.c.1). The registry adapter that `from_bundles_root` builds internally.
