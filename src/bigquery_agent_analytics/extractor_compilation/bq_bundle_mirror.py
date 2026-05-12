# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""BigQuery-table mirror for compiled extractor bundles
(issue #75 Milestone C2.c.3).

Compiled bundles live on the filesystem and are loaded by
:func:`load_bundle` / :func:`discover_bundles` (C2.a). This
module adds a **publish/sync utility** so bundles can flow
between processes via a BigQuery table — useful for Cloud
Run, Cloud Functions, ephemeral CI workers, or any environment
where the filesystem isn't shared.

**The mirror is a utility, not a runtime loader.** The runtime
path stays unchanged: ``sync_bundles_from_bq → discover_bundles
→ from_bundles_root``. Sync writes verified files to a
local directory and lets C2.a's existing loader do the actual
import. There is no "fetch-direct-from-BQ" loader — that
would double the trust surface and diverge from the loader's
audit fields.

Public surface:

* :func:`publish_bundles_to_bq` — walk a local bundle root,
  validate each candidate via :func:`load_bundle`, and push
  the constituent files as rows.
* :func:`sync_bundles_from_bq` — read rows for the requested
  fingerprints, write files into ``dest_dir/<fingerprint>/``,
  and **call :func:`load_bundle` on the reconstructed bundle
  before the sync is considered successful**. Tampered or
  incomplete rows fail loud here, not at runtime.
* :class:`BundleStore` — Protocol the two functions consume.
  Concrete :class:`BigQueryBundleStore` wraps a
  ``google.cloud.bigquery.Client``; tests can pass any
  Protocol-shaped object (e.g. an in-memory fake).

Stable :class:`MirrorFailure` codes — callers can switch on
them:

Publish-side:

* ``manifest_missing`` — bundle subdir has no
  ``manifest.json``.
* ``manifest_unreadable`` — manifest fails to parse or has
  wrong shape.
* ``bundle_load_failed`` — the bundle would not load via
  :func:`load_bundle` *before* publishing. ``detail`` carries
  the underlying loader code so we don't publish bundles the
  runtime would reject.
* ``duplicate_fingerprint`` — two or more subdirs of
  ``bundle_root`` declare the same manifest fingerprint. The
  mirror is keyed on ``(bundle_fingerprint, bundle_path)``;
  publishing both would land contents-of-the-loser in the
  table and corrupt the bundle identity. Fail-closed: every
  participating subdir gets a failure record and *no* rows
  are emitted for that fingerprint.

Sync-side:

* ``fingerprint_not_in_table`` — caller's allowlist named a
  fingerprint that has no rows.
* ``manifest_row_missing`` — bundle has rows but no row with
  ``bundle_path="manifest.json"``.
* ``manifest_row_unreadable`` — the manifest row's content
  isn't a valid :class:`Manifest`. Also fires when the
  parsed manifest's shape would let a path-escape or write
  failure slip past :func:`_validate_bundle_path` (e.g.
  ``module_filename`` containing a path separator).
* ``invalid_bundle_path`` — a row's ``bundle_path`` traverses
  out of the bundle directory, is absolute, or contains
  forbidden characters. Sync is fail-closed here: the offender
  is never written to disk.
* ``unexpected_file`` — a row exists for the bundle whose
  ``bundle_path`` isn't ``manifest.json`` nor the manifest's
  declared ``module_filename``. Bundles are exactly two files;
  anything extra is rejected rather than written.
* ``module_row_missing`` — manifest is fine but there's no
  row for the module file.
* ``duplicate_row`` — two rows share the same
  ``(bundle_fingerprint, bundle_path)``. The table has no
  unique constraint; the mirror enforces it at sync time.
* ``malformed_row`` — row fields have wrong types (e.g.
  ``file_content`` not bytes, ``event_types`` not a list of
  strings).
* ``bundle_load_failed`` — sync wrote files but
  :func:`load_bundle` rejected the reconstructed bundle. The
  partial directory is removed so the caller doesn't keep
  half-synced bundles around.

Neither :func:`publish_bundles_to_bq` nor
:func:`sync_bundles_from_bq` raises on per-bundle problems;
all failures land in the result's ``failures`` tuple. Store
exceptions (BQ-side: network, auth, etc.) DO propagate — that
is the right boundary for "fix the connection and retry."

Out of scope (deferred):

* **GCS-backed signed-URL fetch** for very large bundles. The
  mirror stores ``BYTES`` directly; bundles are tiny today (a
  few KB) and a streaming path can land later if real bundles
  grow.
* **Caching / TTL of synced bundles.** Sync overwrites; the
  caller decides how often to sync.
* **Garbage collection** of stale fingerprints. The mirror's
  job is publish + fetch; lifecycle policy lives upstream.
* **Multi-region replication.** The mirror table is created
  in one BQ location.
"""

from __future__ import annotations

import collections
import dataclasses
import datetime
import pathlib
import re
import shutil
from typing import Any, Iterable, Iterator, Optional, Protocol
import uuid

from .bundle_loader import load_bundle
from .bundle_loader import LoadFailure
from .manifest import Manifest

# Public — re-exported in __init__.py.
__all__ = [
    "BUNDLE_MIRROR_TABLE_SCHEMA",
    "BigQueryBundleStore",
    "BundleRow",
    "BundleStore",
    "MirrorFailure",
    "PublishResult",
    "SyncResult",
    "publish_bundles_to_bq",
    "sync_bundles_from_bq",
]

# Stable schema for the mirror table. Tuples of
# ``(name, type, mode)`` so a caller can construct a
# ``bigquery.SchemaField`` list without importing this module's
# row type. Keep this in lockstep with :class:`BundleRow` —
# any field added here must also be populated in publish and
# read back in :meth:`BigQueryBundleStore.fetch_rows`.
BUNDLE_MIRROR_TABLE_SCHEMA: tuple[tuple[str, str, str], ...] = (
    ("bundle_fingerprint", "STRING", "REQUIRED"),
    ("bundle_path", "STRING", "REQUIRED"),
    ("file_content", "BYTES", "REQUIRED"),
    # Denormalized from the bundle's manifest. Lets a caller
    # filter by event_type without an unnest+join. Source of
    # truth at sync time is still manifest.json's parsed
    # content, not this column.
    ("event_types", "STRING", "REPEATED"),
    # Also denormalized; nullable so the schema stays stable
    # even if a future bundle layout omits one.
    ("module_filename", "STRING", "NULLABLE"),
    ("function_name", "STRING", "NULLABLE"),
    ("published_at", "TIMESTAMP", "REQUIRED"),
)

# The two file names that constitute a bundle, per C2.a's
# loader contract. ``manifest.json`` is fixed; the module
# filename is read from the manifest itself, so the only
# *constant* allowed path is the manifest.
_MANIFEST_FILENAME = "manifest.json"

# Bundle fingerprints are sha256 hex digests — strict 64-char
# lowercase hex. Used as directory names by sync
# (``dest_dir/<fingerprint>/``) and as path components in the
# staging dir, so any value that isn't a pure hex digest could
# trivially escape the destination (e.g. ``"../escape"``).
# Enforced on every row at sync-side ``_check_row_shape`` and
# on every manifest at both publish and sync, so a tampered
# value never reaches the directory-name computation.
_FINGERPRINT_PATTERN = re.compile(r"^[0-9a-f]{64}$")

# BigQuery table identifiers go into backtick-quoted SQL. The
# constructor accepts ``project.dataset.table``; this pattern
# rejects anything that could break out of the quoted identifier
# or smuggle SQL through. Conservative ASCII-only set; BQ does
# allow broader characters but a mirror table is operator-named
# and there's no reason to permit anything exotic.
#
# * Each segment: letters / digits / ``_`` / ``-``. Project IDs
#   can contain ``-``; dataset and table names cannot per BQ
#   docs, but allowing ``-`` here keeps the check simple and
#   BigQuery itself will reject an invalid dataset name later.
# * Exactly two dots; three non-empty segments.
_TABLE_ID_PATTERN = re.compile(
    r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$"
)


# ------------------------------------------------------------------ #
# Data types                                                          #
# ------------------------------------------------------------------ #


@dataclasses.dataclass(frozen=True)
class BundleRow:
  """One row of the mirror table.

  One row per file inside a bundle; a bundle is two files
  (``manifest.json`` + the module file the manifest names),
  so each bundle is two rows.
  """

  bundle_fingerprint: str
  bundle_path: str
  file_content: bytes
  # Denormalized audit fields read from the bundle's manifest
  # at publish time. Useful for query-side filtering and for
  # debugging mismatches between the table view and the
  # eventual on-disk reconstruction.
  event_types: tuple[str, ...]
  module_filename: Optional[str]
  function_name: Optional[str]
  published_at: str


@dataclasses.dataclass(frozen=True)
class MirrorFailure:
  """Stable failure record produced by publish or sync.

  Never raised — both top-level functions accumulate these
  in the result. The ``code`` is the switch-on key; ``detail``
  is a human-readable description. ``bundle_fingerprint`` and
  ``bundle_path`` are populated when the failure can be
  pinned to one bundle / row.
  """

  code: str
  detail: str
  bundle_fingerprint: Optional[str] = None
  bundle_path: Optional[str] = None


@dataclasses.dataclass(frozen=True)
class PublishResult:
  """Outcome of :func:`publish_bundles_to_bq`."""

  published_fingerprints: tuple[str, ...]
  # Fingerprints found under ``bundle_root`` but excluded by
  # ``bundle_fingerprint_allowlist``. Not a failure; just
  # surfaced for visibility.
  skipped_fingerprints: tuple[str, ...]
  failures: tuple[MirrorFailure, ...]
  rows_written: int


@dataclasses.dataclass(frozen=True)
class SyncResult:
  """Outcome of :func:`sync_bundles_from_bq`."""

  synced_fingerprints: tuple[str, ...]
  # Fingerprints either filtered out by the allowlist (when
  # set and a fetched row didn't match) or skipped because
  # nothing in the table matched.
  skipped_fingerprints: tuple[str, ...]
  failures: tuple[MirrorFailure, ...]
  dest_dir: pathlib.Path


# ------------------------------------------------------------------ #
# BundleStore protocol + concrete BigQuery implementation             #
# ------------------------------------------------------------------ #


class BundleStore(Protocol):
  """Read/write boundary over the mirror table.

  ``publish_rows`` is upsert-by-``(bundle_fingerprint,
  bundle_path)`` — re-publishing the same bundle overwrites
  prior rows for that key pair, so the table stays clean
  across compile rebuilds. ``fetch_rows`` filters by the
  requested fingerprints; ``None`` means "every fingerprint."

  Store-level exceptions (network, auth, BQ table missing)
  propagate. Per-row problems are the publish/sync layer's
  concern, surfaced as :class:`MirrorFailure`.
  """

  def fetch_rows(
      self, *, bundle_fingerprints: Optional[Iterable[str]] = None
  ) -> Iterable[BundleRow]:
    ...

  def publish_rows(self, rows: Iterable[BundleRow]) -> int:
    ...


class BigQueryBundleStore:
  """Concrete :class:`BundleStore` over
  ``google.cloud.bigquery``.

  The table is created lazily by :meth:`ensure_table` (callers
  may invoke it once at setup); ``publish_rows`` and
  ``fetch_rows`` assume the table already exists with the
  schema declared in :data:`BUNDLE_MIRROR_TABLE_SCHEMA`.

  Idempotency contract — important caveats:

  * ``publish_rows`` first DELETEs every
    ``(bundle_fingerprint, bundle_path)`` pair it's about to
    write, then calls ``insert_rows_json`` for the new
    payload. The DELETE is scoped to the keys being written;
    other fingerprints are untouched. Re-publishing the same
    fingerprint replaces the prior copy.
  * **The DELETE and INSERT are NOT a single atomic
    transaction.** ``insert_rows_json`` is a streaming insert
    that BigQuery does not enroll in a multi-statement
    transaction with the DELETE query. If the DELETE
    succeeds and the INSERT fails (network, quota, schema
    drift), rows for the affected
    ``(bundle_fingerprint, bundle_path)`` pairs will be
    *missing* from the table until the caller re-runs
    publish. The mirror is publish-side idempotent, so the
    fix is to call :func:`publish_bundles_to_bq` again — but
    operators should be aware that a transient INSERT failure
    leaves a recoverable, not silent, gap. A
    staging-table-plus-MERGE flow would close this gap and
    is deliberately deferred (see module docstring).
  * Duplicate ``(bundle_fingerprint, bundle_path)`` pairs
    in the input raise ``ValueError`` *before* any DELETE
    runs. BigQuery's ``insert_rows_json`` does not
    deduplicate, so silently accepting duplicates would
    leave the table with logical duplicates that sync later
    rejects fail-closed. The publisher in
    :func:`publish_bundles_to_bq` already guards against
    cross-bundle duplicate fingerprints; this defensive
    raise covers callers that build rows by hand.
  """

  def __init__(
      self,
      *,
      bq_client: Any,
      table_id: str,
  ) -> None:
    """``bq_client`` is a ``google.cloud.bigquery.Client`` or
    test-compatible substitute (anything exposing ``query``,
    ``insert_rows_json``, ``get_table``, ``create_table``).
    ``table_id`` is ``project.dataset.table`` and is validated
    at construction — only ASCII letters / digits / ``_`` /
    ``-`` per segment, exactly three segments. Any value that
    could break out of the backtick-quoted SQL identifier
    (backtick, semicolon, whitespace, ``--``, ``/*``)
    raises :class:`ValueError`."""
    if not isinstance(table_id, str):
      raise ValueError(
          f"table_id must be a string; got {type(table_id).__name__}"
      )
    # ``fullmatch`` (not ``match``) because Python regex's ``$``
    # accepts a trailing newline by default — ``"proj.ds.tbl\n"``
    # would otherwise sneak past and reach the SQL.
    if not _TABLE_ID_PATTERN.fullmatch(table_id):
      raise ValueError(
          f"table_id {table_id!r} is not a well-formed "
          f"'project.dataset.table' identifier "
          f"(allowed per segment: ASCII letters, digits, '_', "
          f"'-'; exactly three segments)"
      )
    self._bq_client = bq_client
    self._table_id = table_id

  # -------------------------------------------------------- #
  # Setup                                                    #
  # -------------------------------------------------------- #

  def ensure_table(self) -> None:
    """Create the mirror table if it doesn't already exist.
    Idempotent. Schema mismatches against an existing table are
    NOT auto-corrected — that would silently rewrite a table the
    caller owns. If the schema diverges, an operator runs the
    DDL fix by hand."""
    # Imports here so the module imports cleanly in environments
    # that don't have google-cloud-bigquery installed (tests
    # using the in-memory fake store).
    from google.cloud import bigquery  # type: ignore

    schema = [
        bigquery.SchemaField(name, type_, mode=mode)
        for name, type_, mode in BUNDLE_MIRROR_TABLE_SCHEMA
    ]
    table = bigquery.Table(self._table_id, schema=schema)
    self._bq_client.create_table(table, exists_ok=True)

  # -------------------------------------------------------- #
  # BundleStore protocol                                     #
  # -------------------------------------------------------- #

  def fetch_rows(
      self, *, bundle_fingerprints: Optional[Iterable[str]] = None
  ) -> Iterator[BundleRow]:
    """Read rows for the requested fingerprints, or every row
    when ``bundle_fingerprints`` is ``None``."""
    fps = (
        None
        if bundle_fingerprints is None
        else sorted({fp for fp in bundle_fingerprints})
    )
    sql, params = self._select_sql(fps)
    job = self._bq_client.query(sql, job_config=self._query_config(params))
    for row in job.result():
      yield BundleRow(
          bundle_fingerprint=row["bundle_fingerprint"],
          bundle_path=row["bundle_path"],
          file_content=bytes(row["file_content"]),
          event_types=tuple(row["event_types"] or ()),
          module_filename=row["module_filename"],
          function_name=row["function_name"],
          published_at=str(row["published_at"]),
      )

  def publish_rows(self, rows: Iterable[BundleRow]) -> int:
    """Upsert rows by ``(bundle_fingerprint, bundle_path)``.
    Returns the count of rows actually written.

    Raises ``ValueError`` if the input contains duplicate
    ``(bundle_fingerprint, bundle_path)`` pairs. See the
    class docstring for the DELETE+INSERT non-atomicity
    contract.
    """
    rows_list = list(rows)
    if not rows_list:
      return 0
    # Defense in depth: refuse duplicate (fp, path) input
    # pairs. The publisher de-duplicates by detecting
    # ``duplicate_fingerprint`` earlier in the pipeline; this
    # raise covers direct callers of the store.
    pairs = [(r.bundle_fingerprint, r.bundle_path) for r in rows_list]
    pair_counts = collections.Counter(pairs)
    dupes = sorted(p for p, n in pair_counts.items() if n > 1)
    if dupes:
      raise ValueError(
          f"publish_rows received duplicate "
          f"(bundle_fingerprint, bundle_path) pairs: {dupes}"
      )
    # 1. DELETE the (fp, path) pairs we're about to overwrite.
    delete_pairs = sorted(set(pairs))
    self._delete_pairs(delete_pairs)
    # 2. INSERT the new rows. Note: this is NOT atomic with
    # the DELETE above. See the class docstring.
    payload = [self._row_to_json(r) for r in rows_list]
    errors = self._bq_client.insert_rows_json(self._table_id, payload)
    if errors:
      # BQ surfaces row-level errors in this return value
      # rather than raising; propagate them so callers don't
      # silently miss a half-inserted batch.
      raise RuntimeError(
          f"BigQuery insert_rows_json returned errors for "
          f"{self._table_id}: {errors!r}"
      )
    return len(rows_list)

  # -------------------------------------------------------- #
  # Helpers                                                  #
  # -------------------------------------------------------- #

  def _select_sql(
      self, fingerprints: Optional[list[str]]
  ) -> tuple[str, dict[str, Any]]:
    base = (
        f"SELECT bundle_fingerprint, bundle_path, file_content, "
        f"event_types, module_filename, function_name, "
        f"published_at "
        f"FROM `{self._table_id}` "
    )
    if fingerprints is None:
      return base + "ORDER BY bundle_fingerprint, bundle_path", {}
    sql = (
        base + "WHERE bundle_fingerprint IN UNNEST(@fps) "
        "ORDER BY bundle_fingerprint, bundle_path"
    )
    return sql, {"fps": fingerprints}

  def _delete_pairs(self, pairs: list[tuple[str, str]]) -> None:
    if not pairs:
      return
    fps = sorted({fp for fp, _ in pairs})
    sql = (
        f"DELETE FROM `{self._table_id}` "
        f"WHERE bundle_fingerprint IN UNNEST(@fps) "
        f"AND CONCAT(bundle_fingerprint, '::', bundle_path) "
        f"IN UNNEST(@pair_keys)"
    )
    pair_keys = [f"{fp}::{path}" for fp, path in pairs]
    self._bq_client.query(
        sql,
        job_config=self._query_config({"fps": fps, "pair_keys": pair_keys}),
    ).result()

  def _query_config(self, params: dict[str, Any]) -> Any:
    from google.cloud import bigquery  # type: ignore

    parameters = []
    for name, value in params.items():
      if isinstance(value, list):
        parameters.append(bigquery.ArrayQueryParameter(name, "STRING", value))
      else:
        parameters.append(bigquery.ScalarQueryParameter(name, "STRING", value))
    return bigquery.QueryJobConfig(query_parameters=parameters)

  def _row_to_json(self, row: BundleRow) -> dict[str, Any]:
    import base64

    return {
        "bundle_fingerprint": row.bundle_fingerprint,
        "bundle_path": row.bundle_path,
        # BQ JSON streaming insert expects base64 for BYTES.
        "file_content": base64.b64encode(row.file_content).decode("ascii"),
        "event_types": list(row.event_types),
        "module_filename": row.module_filename,
        "function_name": row.function_name,
        "published_at": row.published_at,
    }


# ------------------------------------------------------------------ #
# publish                                                              #
# ------------------------------------------------------------------ #


def publish_bundles_to_bq(
    *,
    bundle_root: pathlib.Path,
    store: BundleStore,
    bundle_fingerprint_allowlist: Optional[Iterable[str]] = None,
) -> PublishResult:
  """Walk *bundle_root*, validate each bundle via
  :func:`load_bundle`, and publish the constituent files to
  the mirror store.

  Args:
    bundle_root: Directory containing one subdirectory per
      bundle (the same layout :func:`discover_bundles` walks).
    store: :class:`BundleStore` to publish into. Typically a
      :class:`BigQueryBundleStore`; tests pass in-memory
      fakes.
    bundle_fingerprint_allowlist: Optional set of fingerprints
      to publish. ``None`` publishes every successfully-loaded
      bundle. Fingerprints in the allowlist that don't exist
      under ``bundle_root`` are silently absent from the
      result — caller asked us to publish nothing.

  Returns:
    A populated :class:`PublishResult`. Loader failures and
    parse failures land in ``failures``; bundles excluded by
    the allowlist land in ``skipped_fingerprints``.
  """
  bundle_root = pathlib.Path(bundle_root)
  allowlist = (
      None
      if bundle_fingerprint_allowlist is None
      else set(bundle_fingerprint_allowlist)
  )

  published: list[str] = []
  skipped: list[str] = []
  failures: list[MirrorFailure] = []
  now = _now_iso_utc()
  # First-pass collect candidates by fingerprint so we can
  # detect cross-subdir duplicate fingerprints before publishing
  # any of them. Without this guard, two subdirs claiming the
  # same fingerprint would each emit their own
  # ``(fingerprint, manifest.json)`` + ``(fingerprint, module)``
  # rows; ``BigQueryBundleStore.publish_rows`` would DELETE the
  # composite keys (good) then INSERT both copies, leaving the
  # table with duplicate logical rows that sync later rejects.
  # Better to fail-closed at publish than to corrupt the table.
  candidates: dict[str, list[tuple[str, list[BundleRow]]]] = (
      collections.defaultdict(list)
  )

  if not bundle_root.is_dir():
    failures.append(
        MirrorFailure(
            code="bundle_root_missing",
            detail=(
                f"bundle_root {str(bundle_root)!r} is not a directory; "
                f"nothing to publish."
            ),
        )
    )
    return PublishResult(
        published_fingerprints=(),
        skipped_fingerprints=(),
        failures=tuple(failures),
        rows_written=0,
    )

  for child in sorted(bundle_root.iterdir()):
    if not child.is_dir():
      continue
    manifest_path = child / _MANIFEST_FILENAME
    if not manifest_path.exists():
      failures.append(
          MirrorFailure(
              code="manifest_missing",
              detail=f"{child.name}/manifest.json not found",
          )
      )
      continue
    try:
      manifest_text = manifest_path.read_text(encoding="utf-8")
      manifest = Manifest.from_json(manifest_text)
    # ``Exception`` covers JSON-decode + KeyError on the
    # from_json mapping; both are "this file isn't a usable
    # manifest" failures from the publisher's perspective.
    except Exception as exc:  # noqa: BLE001 — record + continue
      failures.append(
          MirrorFailure(
              code="manifest_unreadable",
              detail=f"{type(exc).__name__}: {exc}",
              bundle_path=str(manifest_path.relative_to(bundle_root)),
          )
      )
      continue

    # Shape-check the manifest before trusting its fingerprint /
    # module_filename anywhere downstream. Specifically: a
    # tampered ``fingerprint`` like ``"../escape"`` would
    # otherwise become a ``bundle_fingerprint`` value in the
    # table, and sync would use it as a directory name. Catch
    # at the source.
    shape_problem = _validate_manifest_shape(manifest)
    if shape_problem is not None:
      failures.append(
          MirrorFailure(
              code="manifest_unreadable",
              detail=shape_problem,
              bundle_path=str(manifest_path.relative_to(bundle_root)),
          )
      )
      continue

    fp = manifest.fingerprint
    if allowlist is not None and fp not in allowlist:
      skipped.append(fp)
      continue

    # Pre-publish validation: load_bundle against the
    # manifest's own fingerprint. If the bundle wouldn't load
    # at the runtime, don't publish it — the mirror's job is
    # to distribute *working* bundles.
    load_outcome = load_bundle(child, expected_fingerprint=fp)
    if isinstance(load_outcome, LoadFailure):
      failures.append(
          MirrorFailure(
              code="bundle_load_failed",
              detail=f"{load_outcome.code}: {load_outcome.detail}",
              bundle_fingerprint=fp,
          )
      )
      continue

    module_path = child / manifest.module_filename
    if not module_path.exists():
      # load_bundle would have already caught this, but be
      # explicit so the failure code is precise.
      failures.append(
          MirrorFailure(
              code="bundle_load_failed",
              detail=(
                  f"module file {manifest.module_filename!r} missing "
                  f"after load_bundle succeeded — racing filesystem?"
              ),
              bundle_fingerprint=fp,
          )
      )
      continue

    bundle_rows = [
        BundleRow(
            bundle_fingerprint=fp,
            bundle_path=_MANIFEST_FILENAME,
            file_content=manifest_path.read_bytes(),
            event_types=manifest.event_types,
            module_filename=manifest.module_filename,
            function_name=manifest.function_name,
            published_at=now,
        ),
        BundleRow(
            bundle_fingerprint=fp,
            bundle_path=manifest.module_filename,
            file_content=module_path.read_bytes(),
            event_types=manifest.event_types,
            module_filename=manifest.module_filename,
            function_name=manifest.function_name,
            published_at=now,
        ),
    ]
    candidates[fp].append((child.name, bundle_rows))

  # Second pass: emit rows for fingerprints that appeared in
  # exactly one subdir. Duplicate fingerprints get a
  # ``duplicate_fingerprint`` failure naming all participating
  # subdirs and contribute zero rows.
  rows_to_publish: list[BundleRow] = []
  for fp, entries in candidates.items():
    if len(entries) > 1:
      names = sorted(name for name, _ in entries)
      failures.append(
          MirrorFailure(
              code="duplicate_fingerprint",
              detail=(
                  f"fingerprint {fp!r} declared by multiple subdirs "
                  f"({names}); refusing to publish either"
              ),
              bundle_fingerprint=fp,
          )
      )
      continue
    _name, rows = entries[0]
    rows_to_publish.extend(rows)
    published.append(fp)

  rows_written = store.publish_rows(rows_to_publish) if rows_to_publish else 0

  return PublishResult(
      published_fingerprints=tuple(sorted(set(published))),
      skipped_fingerprints=tuple(sorted(set(skipped))),
      failures=tuple(failures),
      rows_written=rows_written,
  )


# ------------------------------------------------------------------ #
# sync                                                                #
# ------------------------------------------------------------------ #


def sync_bundles_from_bq(
    *,
    store: BundleStore,
    dest_dir: pathlib.Path,
    bundle_fingerprint_allowlist: Optional[Iterable[str]] = None,
) -> SyncResult:
  """Fetch rows from *store* and reconstruct bundles under
  ``dest_dir/<fingerprint>/``. For each fingerprint the
  reconstructed bundle is passed through :func:`load_bundle`
  before being declared synced; tampered or incomplete bundles
  fail loud at sync time rather than at runtime.

  Args:
    store: :class:`BundleStore` to fetch from. Typically a
      :class:`BigQueryBundleStore`; tests pass in-memory
      fakes.
    dest_dir: Local destination. One subdirectory per
      fingerprint will be (re)written. Files outside those
      subdirectories are not touched; the directory may
      contain other artifacts.
    bundle_fingerprint_allowlist: Optional set of fingerprints
      to sync. ``None`` syncs everything the store returns.
      Any fingerprint in the allowlist for which no rows are
      returned shows up as a ``fingerprint_not_in_table``
      failure.

  Returns:
    A populated :class:`SyncResult`. Per-bundle issues land
    in ``failures``; the ``dest_dir`` field echoes the
    destination so callers (and tests) don't have to thread
    the path back manually.
  """
  dest_dir = pathlib.Path(dest_dir)
  dest_dir.mkdir(parents=True, exist_ok=True)

  allowlist = (
      None
      if bundle_fingerprint_allowlist is None
      else set(bundle_fingerprint_allowlist)
  )

  rows = list(store.fetch_rows(bundle_fingerprints=allowlist))

  # Group rows by fingerprint with row-level shape checks
  # interleaved so a malformed row doesn't poison the whole
  # bundle's parsing.
  rows_by_fp: dict[str, list[BundleRow]] = collections.defaultdict(list)
  failures: list[MirrorFailure] = []
  for row in rows:
    row_problem = _check_row_shape(row)
    if row_problem is not None:
      failures.append(
          MirrorFailure(
              code="malformed_row",
              detail=row_problem,
              bundle_fingerprint=getattr(row, "bundle_fingerprint", None),
              bundle_path=getattr(row, "bundle_path", None),
          )
      )
      continue
    rows_by_fp[row.bundle_fingerprint].append(row)

  # If the allowlist named fingerprints we never saw, record
  # them as failures so the caller knows the publish lag
  # hasn't caught up.
  if allowlist is not None:
    for fp in sorted(allowlist - set(rows_by_fp.keys())):
      failures.append(
          MirrorFailure(
              code="fingerprint_not_in_table",
              detail=f"fingerprint {fp!r} has no rows in the mirror table",
              bundle_fingerprint=fp,
          )
      )

  synced: list[str] = []
  skipped: list[str] = []

  for fp in sorted(rows_by_fp.keys()):
    bundle_rows = rows_by_fp[fp]

    # Per-fingerprint duplicate-path check before writing
    # anything.
    path_counts = collections.Counter(r.bundle_path for r in bundle_rows)
    dupes = sorted(p for p, n in path_counts.items() if n > 1)
    if dupes:
      failures.append(
          MirrorFailure(
              code="duplicate_row",
              detail=f"duplicate bundle_path(s) for {fp!r}: {dupes}",
              bundle_fingerprint=fp,
          )
      )
      skipped.append(fp)
      continue

    by_path = {r.bundle_path: r for r in bundle_rows}

    # Step 1: the manifest row must be present and readable
    # before we can know which other paths are legitimate.
    manifest_row = by_path.get(_MANIFEST_FILENAME)
    if manifest_row is None:
      failures.append(
          MirrorFailure(
              code="manifest_row_missing",
              detail=(
                  f"no row with bundle_path={_MANIFEST_FILENAME!r} for "
                  f"fingerprint {fp!r}"
              ),
              bundle_fingerprint=fp,
          )
      )
      skipped.append(fp)
      continue
    try:
      manifest = Manifest.from_json(manifest_row.file_content.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 — record + continue
      failures.append(
          MirrorFailure(
              code="manifest_row_unreadable",
              detail=f"{type(exc).__name__}: {exc}",
              bundle_fingerprint=fp,
              bundle_path=_MANIFEST_FILENAME,
          )
      )
      skipped.append(fp)
      continue

    # ``Manifest.from_json`` is lenient about field types and
    # values — it just maps keys. A mirrored manifest can
    # therefore carry e.g. ``module_filename="subdir/foo.py"``
    # which passes :func:`_validate_bundle_path` (no ``..``,
    # not absolute) but would later raise ``FileNotFoundError``
    # when sync writes to ``bundle_dir / "subdir/foo.py"``
    # because the parent dir doesn't exist. Catch
    # malformed-shape cases up front so the failure surfaces
    # as a structured ``manifest_row_unreadable`` rather than
    # bubbling out of the write step.
    shape_problem = _validate_manifest_shape(manifest)
    if shape_problem is not None:
      failures.append(
          MirrorFailure(
              code="manifest_row_unreadable",
              detail=shape_problem,
              bundle_fingerprint=fp,
              bundle_path=_MANIFEST_FILENAME,
          )
      )
      skipped.append(fp)
      continue

    # Step 2: validate every row's bundle_path is safe AND
    # is one of the two paths a bundle legitimately contains.
    # Path safety happens *before* checking the file set so
    # a malformed manifest can't introduce a traversal via
    # module_filename.
    allowed_paths = {_MANIFEST_FILENAME, manifest.module_filename}
    rejected = False
    for row in bundle_rows:
      problem = _validate_bundle_path(row.bundle_path)
      if problem is not None:
        failures.append(
            MirrorFailure(
                code="invalid_bundle_path",
                detail=problem,
                bundle_fingerprint=fp,
                bundle_path=row.bundle_path,
            )
        )
        rejected = True
        continue
      if row.bundle_path not in allowed_paths:
        failures.append(
            MirrorFailure(
                code="unexpected_file",
                detail=(
                    f"bundle_path {row.bundle_path!r} is not "
                    f"manifest.json or the manifest's module_filename "
                    f"({manifest.module_filename!r})"
                ),
                bundle_fingerprint=fp,
                bundle_path=row.bundle_path,
            )
        )
        rejected = True
    if rejected:
      skipped.append(fp)
      continue

    if manifest.module_filename not in by_path:
      failures.append(
          MirrorFailure(
              code="module_row_missing",
              detail=(
                  f"no row with bundle_path="
                  f"{manifest.module_filename!r} for fingerprint {fp!r}"
              ),
              bundle_fingerprint=fp,
          )
      )
      skipped.append(fp)
      continue

    # Step 3: write the two files to a *staging* directory and
    # validate the reconstruction via load_bundle BEFORE
    # touching any existing ``dest_dir/<fingerprint>/``. A bad
    # mirror row must not destroy a previously-good local
    # bundle: write somewhere safe, run the loader gate, then
    # staged-replace the target only on success. The
    # replacement itself (rmtree + move) is NOT strictly
    # atomic — a crash between the two leaves the bundle
    # absent on disk — but the load-bundle-failure case (the
    # one the staged flow is designed to protect) is correctly
    # atomic in the failure direction.
    bundle_dir = dest_dir / fp
    staging_dir = dest_dir / f".staging-{fp}-{uuid.uuid4().hex[:8]}"
    if staging_dir.exists():
      # Extraordinarily unlikely (collision on a uuid4 prefix)
      # but be defensive — never reuse a populated staging dir.
      shutil.rmtree(staging_dir)
    staging_dir.mkdir(parents=True)
    try:
      (staging_dir / _MANIFEST_FILENAME).write_bytes(manifest_row.file_content)
      (staging_dir / manifest.module_filename).write_bytes(
          by_path[manifest.module_filename].file_content
      )
      load_outcome = load_bundle(staging_dir, expected_fingerprint=fp)
    except Exception as exc:  # noqa: BLE001 — record + continue
      # write failure (FileNotFoundError for a still-malformed
      # path, disk full, etc.) — leave any pre-existing
      # ``bundle_dir`` intact, scrub the staging dir.
      shutil.rmtree(staging_dir, ignore_errors=True)
      failures.append(
          MirrorFailure(
              code="bundle_load_failed",
              detail=(
                  f"writing staging bundle raised "
                  f"{type(exc).__name__}: {exc}"
              ),
              bundle_fingerprint=fp,
          )
      )
      skipped.append(fp)
      continue

    if isinstance(load_outcome, LoadFailure):
      # Reconstructed bundle doesn't load — keep the old
      # ``bundle_dir`` (if any) and toss the staging copy.
      shutil.rmtree(staging_dir, ignore_errors=True)
      failures.append(
          MirrorFailure(
              code="bundle_load_failed",
              detail=f"{load_outcome.code}: {load_outcome.detail}",
              bundle_fingerprint=fp,
          )
      )
      skipped.append(fp)
      continue

    # Staged replace: remove old ``bundle_dir`` (if any), then
    # move staging into place. The rmtree + move pair is NOT
    # strictly atomic — a crash between the two leaves the
    # bundle absent on disk, recoverable by re-running sync.
    # The crucial property — "don't destroy good local state
    # because of bad mirror rows" — is preserved by the
    # staging-then-validate flow above (load_bundle failure
    # never reaches this point).
    if bundle_dir.exists():
      shutil.rmtree(bundle_dir)
    shutil.move(str(staging_dir), str(bundle_dir))

    synced.append(fp)

  return SyncResult(
      synced_fingerprints=tuple(sorted(set(synced))),
      skipped_fingerprints=tuple(sorted(set(skipped))),
      failures=tuple(failures),
      dest_dir=dest_dir,
  )


# ------------------------------------------------------------------ #
# Helpers                                                              #
# ------------------------------------------------------------------ #


def _validate_bundle_path(path: str) -> Optional[str]:
  """Return a problem string if *path* isn't a safe relative
  bundle-internal path, or ``None`` if it's clean.

  Required shape:

  * Non-empty.
  * No NUL bytes; no backslashes (so a Windows-style path
    can't smuggle a traversal past a POSIX check).
  * Not absolute.
  * No ``..`` segments.
  * No leading ``/``.
  """
  if not path:
    return "empty bundle_path"
  if "\x00" in path:
    return "bundle_path contains NUL"
  if "\\" in path:
    return f"bundle_path contains backslash: {path!r}"
  if path.startswith("/"):
    return f"bundle_path is absolute: {path!r}"
  pure = pathlib.PurePosixPath(path)
  if pure.is_absolute():
    return f"bundle_path is absolute: {path!r}"
  if any(part == ".." for part in pure.parts):
    return f"bundle_path contains '..': {path!r}"
  return None


def _validate_manifest_shape(manifest: Manifest) -> Optional[str]:
  """Return a problem string if *manifest* would let sync write
  outside the bundle directory or otherwise produce a broken
  reconstruction, or ``None`` if it's safe.

  Checks beyond ``Manifest.from_json``'s lenient field-mapping:

  * ``fingerprint`` and ``function_name`` are non-empty
    strings.
  * ``event_types`` is a tuple of strings (the dataclass field
    declares ``tuple[str, ...]`` but the JSON round-trip path
    doesn't enforce element types).
  * ``module_filename`` is a *bare* filename — no path
    separators (forward slash or backslash), no ``..``, no
    ``.``, no NUL, non-empty. C2.a's loader resolves it as
    ``bundle_dir / module_filename``; a name containing
    ``"subdir/foo.py"`` would otherwise raise
    ``FileNotFoundError`` at sync's write step instead of
    surfacing as a structured ``manifest_row_unreadable``.
  """
  if not isinstance(manifest.fingerprint, str) or not manifest.fingerprint:
    return (
        f"manifest fingerprint must be a non-empty string; got "
        f"{type(manifest.fingerprint).__name__}={manifest.fingerprint!r}"
    )
  # Manifest fingerprints become row's ``bundle_fingerprint``
  # and then directory names; same strict sha256-hex enforcement
  # as ``_check_row_shape``. Catching here lets publish reject
  # tampered manifests before they hit the table.
  if not _FINGERPRINT_PATTERN.fullmatch(manifest.fingerprint):
    return (
        f"manifest fingerprint must be 64 lowercase hex characters "
        f"(sha256); got {manifest.fingerprint!r}"
    )
  if not isinstance(manifest.function_name, str) or not manifest.function_name:
    return (
        f"manifest function_name must be a non-empty string; got "
        f"{type(manifest.function_name).__name__}={manifest.function_name!r}"
    )
  if not isinstance(manifest.event_types, tuple):
    return (
        f"manifest event_types must be a tuple; got "
        f"{type(manifest.event_types).__name__}"
    )
  for index, et in enumerate(manifest.event_types):
    if not isinstance(et, str):
      return (
          f"manifest event_types[{index}] must be a string; got "
          f"{type(et).__name__}={et!r}"
      )
  if (
      not isinstance(manifest.module_filename, str)
      or not manifest.module_filename
  ):
    return (
        f"manifest module_filename must be a non-empty string; got "
        f"{type(manifest.module_filename).__name__}="
        f"{manifest.module_filename!r}"
    )
  mf = manifest.module_filename
  if "\x00" in mf:
    return "manifest module_filename contains NUL"
  if "/" in mf or "\\" in mf:
    return (
        f"manifest module_filename must be a bare filename "
        f"(no path separators); got {mf!r}"
    )
  if mf in (".", ".."):
    return f"manifest module_filename must not be {mf!r}"
  return None


def _check_row_shape(row: Any) -> Optional[str]:
  """Return a problem string if *row* isn't a well-formed
  :class:`BundleRow`, or ``None`` if it's clean.

  The store's row constructor handles most shape coercion, but
  the protocol allows arbitrary substitutes. Defensive
  checks keep a malformed fake or a future schema-drift case
  from poisoning sync.
  """
  if not isinstance(row, BundleRow):
    return f"row is not a BundleRow; got {type(row).__name__}"
  if not isinstance(row.bundle_fingerprint, str) or not row.bundle_fingerprint:
    return f"bundle_fingerprint must be a non-empty string"
  # Fingerprints become directory names at sync
  # (``dest_dir/<fingerprint>/`` + the staging-dir name). A
  # tampered value like ``"../escape"`` would otherwise sync
  # successfully and write OUTSIDE dest_dir; reject early.
  if not _FINGERPRINT_PATTERN.fullmatch(row.bundle_fingerprint):
    return (
        f"bundle_fingerprint must be 64 lowercase hex characters "
        f"(sha256); got {row.bundle_fingerprint!r}"
    )
  if not isinstance(row.bundle_path, str) or not row.bundle_path:
    return f"bundle_path must be a non-empty string"
  if not isinstance(row.file_content, (bytes, bytearray)):
    return (
        f"file_content must be bytes; got " f"{type(row.file_content).__name__}"
    )
  if not isinstance(row.event_types, tuple):
    return (
        f"event_types must be a tuple of strings; got "
        f"{type(row.event_types).__name__}"
    )
  for index, et in enumerate(row.event_types):
    if not isinstance(et, str):
      return (
          f"event_types[{index}] must be a string; got " f"{type(et).__name__}"
      )
  return None


def _now_iso_utc() -> str:
  return (
      datetime.datetime.now(datetime.timezone.utc)
      .replace(microsecond=0)
      .isoformat()
      .replace("+00:00", "Z")
  )
