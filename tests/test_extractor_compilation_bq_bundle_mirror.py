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

"""Tests for the BigQuery bundle mirror (#75 PR C2.c.3).

Coverage:

* Round-trip — publish a local bundle, sync it back into a
  fresh directory, verify ``load_bundle`` accepts the
  reconstruction.
* Fingerprint allowlist — publish and sync both honor it.
* Path-safety — bundle_path values that traverse out of the
  bundle dir, are absolute, or contain forbidden characters
  are rejected at sync (never written to disk).
* Missing manifest — sync rejects bundles whose rows lack
  ``manifest.json``.
* Malformed rows — wrong content type / wrong types on the
  denorm fields surface as ``malformed_row`` failures.
* Idempotent republish — publishing the same bundle twice
  ends up with one copy in the store, not two.

Tests use an in-memory ``BundleStore`` substitute so the
suite stays fast and deterministic. The live BQ path is
covered separately by ``test_extractor_compilation_bq_bundle_mirror_live.py``.
"""

from __future__ import annotations

import json
import pathlib
import textwrap

import pytest

# ------------------------------------------------------------------ #
# Hand-built bundle helpers (mirror tests don't depend on the         #
# full compile pipeline; we just need a loader-acceptable bundle)    #
# ------------------------------------------------------------------ #


_VALID_FINGERPRINT_A = "a" * 64
_VALID_FINGERPRINT_B = "b" * 64


def _write_manifest(
    bundle_dir: pathlib.Path,
    *,
    fingerprint: str,
    event_types: tuple[str, ...] = ("bka_decision",),
    module_filename: str = "extractor.py",
    function_name: str = "extract_bka",
) -> None:
  bundle_dir.mkdir(parents=True, exist_ok=True)
  manifest = {
      "fingerprint": fingerprint,
      "event_types": list(event_types),
      "module_filename": module_filename,
      "function_name": function_name,
      "compiler_package_version": "0.0.0",
      "template_version": "v0.1",
      "transcript_builder_version": "tb-1",
      "created_at": "2026-05-08T00:00:00Z",
  }
  (bundle_dir / "manifest.json").write_text(
      json.dumps(manifest, sort_keys=True, indent=2), encoding="utf-8"
  )


_MINIMAL_VALID_SOURCE = textwrap.dedent(
    """\
    def extract_bka(event, spec):
        return None
    """
)


def _build_bundle(
    parent: pathlib.Path,
    *,
    name: str,
    fingerprint: str,
    event_types: tuple[str, ...] = ("bka_decision",),
) -> pathlib.Path:
  bundle_dir = parent / name
  _write_manifest(bundle_dir, fingerprint=fingerprint, event_types=event_types)
  (bundle_dir / "extractor.py").write_text(
      _MINIMAL_VALID_SOURCE, encoding="utf-8"
  )
  return bundle_dir


# ------------------------------------------------------------------ #
# In-memory store                                                      #
# ------------------------------------------------------------------ #


class _InMemoryStore:
  """Minimal :class:`BundleStore` substitute. Upserts by
  ``(bundle_fingerprint, bundle_path)`` so re-publishing the
  same bundle replaces the prior rows."""

  def __init__(self):
    self._rows: dict[tuple[str, str], "BundleRow"] = {}

  def fetch_rows(self, *, bundle_fingerprints=None):
    if bundle_fingerprints is None:
      return list(self._rows.values())
    allow = set(bundle_fingerprints)
    return [r for r in self._rows.values() if r.bundle_fingerprint in allow]

  def publish_rows(self, rows):
    count = 0
    for r in rows:
      self._rows[(r.bundle_fingerprint, r.bundle_path)] = r
      count += 1
    return count

  # Test-only inspection helpers.
  def all_rows(self):
    return list(self._rows.values())

  def force_insert(self, row):
    """Bypass the upsert dedup. Used to construct deliberately
    malformed table states (duplicate rows, malformed payloads)
    that the BQ-side schema would normally forbid."""
    # Use a sentinel composite key so the row is preserved
    # alongside the well-keyed copy.
    self._rows[
        (
            "__force__",
            f"{row.bundle_fingerprint}::{row.bundle_path}::{len(self._rows)}",
        )
    ] = row


# ------------------------------------------------------------------ #
# Round-trip                                                          #
# ------------------------------------------------------------------ #


class TestRoundTrip:

  def test_publish_then_sync_round_trips(self, tmp_path: pathlib.Path):
    """Publish a hand-built bundle, sync it back into a fresh
    directory, verify load_bundle accepts the reconstructed
    bundle and reports the same fingerprint."""
    from bigquery_agent_analytics.extractor_compilation import BundleRow
    from bigquery_agent_analytics.extractor_compilation import load_bundle
    from bigquery_agent_analytics.extractor_compilation import LoadedBundle
    from bigquery_agent_analytics.extractor_compilation import publish_bundles_to_bq
    from bigquery_agent_analytics.extractor_compilation import sync_bundles_from_bq

    bundle_root = tmp_path / "bundles"
    bundle_root.mkdir()
    _build_bundle(bundle_root, name="bka", fingerprint=_VALID_FINGERPRINT_A)

    store = _InMemoryStore()

    publish = publish_bundles_to_bq(
        bundle_root=bundle_root,
        store=store,
    )
    assert publish.published_fingerprints == (_VALID_FINGERPRINT_A,)
    assert publish.skipped_fingerprints == ()
    assert publish.failures == ()
    assert publish.rows_written == 2  # manifest + module

    # The denormalized event_types column was populated from
    # the manifest at publish time.
    for row in store.all_rows():
      assert isinstance(row, BundleRow)
      assert row.event_types == ("bka_decision",)
      assert row.module_filename == "extractor.py"
      assert row.function_name == "extract_bka"

    sync_dir = tmp_path / "synced"
    sync = sync_bundles_from_bq(
        store=store,
        dest_dir=sync_dir,
    )
    assert sync.synced_fingerprints == (_VALID_FINGERPRINT_A,)
    assert sync.skipped_fingerprints == ()
    assert sync.failures == ()
    assert sync.dest_dir == sync_dir

    # The reconstructed bundle is loadable by C2.a's loader.
    bundle_dir = sync_dir / _VALID_FINGERPRINT_A
    outcome = load_bundle(bundle_dir, expected_fingerprint=_VALID_FINGERPRINT_A)
    assert isinstance(outcome, LoadedBundle)
    assert outcome.manifest.fingerprint == _VALID_FINGERPRINT_A
    assert callable(outcome.extractor)

  def test_round_trip_multiple_bundles(self, tmp_path: pathlib.Path):
    """Two bundles with distinct fingerprints publish + sync
    independently; sync writes each into its own
    ``<fingerprint>`` subdir."""
    from bigquery_agent_analytics.extractor_compilation import publish_bundles_to_bq
    from bigquery_agent_analytics.extractor_compilation import sync_bundles_from_bq

    bundle_root = tmp_path / "bundles"
    bundle_root.mkdir()
    _build_bundle(bundle_root, name="a", fingerprint=_VALID_FINGERPRINT_A)
    _build_bundle(
        bundle_root,
        name="b",
        fingerprint=_VALID_FINGERPRINT_B,
        event_types=("other_event",),
    )

    store = _InMemoryStore()
    publish = publish_bundles_to_bq(
        bundle_root=bundle_root,
        store=store,
    )
    assert set(publish.published_fingerprints) == {
        _VALID_FINGERPRINT_A,
        _VALID_FINGERPRINT_B,
    }
    assert publish.rows_written == 4

    sync_dir = tmp_path / "synced"
    sync = sync_bundles_from_bq(store=store, dest_dir=sync_dir)
    assert set(sync.synced_fingerprints) == {
        _VALID_FINGERPRINT_A,
        _VALID_FINGERPRINT_B,
    }
    assert (sync_dir / _VALID_FINGERPRINT_A / "manifest.json").exists()
    assert (sync_dir / _VALID_FINGERPRINT_B / "manifest.json").exists()


# ------------------------------------------------------------------ #
# Allowlist                                                           #
# ------------------------------------------------------------------ #


class TestAllowlist:

  def test_publish_allowlist_skips_unlisted(self, tmp_path: pathlib.Path):
    from bigquery_agent_analytics.extractor_compilation import publish_bundles_to_bq

    bundle_root = tmp_path / "bundles"
    bundle_root.mkdir()
    _build_bundle(bundle_root, name="a", fingerprint=_VALID_FINGERPRINT_A)
    _build_bundle(
        bundle_root,
        name="b",
        fingerprint=_VALID_FINGERPRINT_B,
        event_types=("other_event",),
    )

    store = _InMemoryStore()
    publish = publish_bundles_to_bq(
        bundle_root=bundle_root,
        store=store,
        bundle_fingerprint_allowlist=[_VALID_FINGERPRINT_A],
    )
    assert publish.published_fingerprints == (_VALID_FINGERPRINT_A,)
    assert publish.skipped_fingerprints == (_VALID_FINGERPRINT_B,)
    # Only fp A's two rows were written; fp B was skipped.
    assert publish.rows_written == 2
    fps_in_store = {r.bundle_fingerprint for r in store.all_rows()}
    assert fps_in_store == {_VALID_FINGERPRINT_A}

  def test_sync_allowlist_skips_unlisted(self, tmp_path: pathlib.Path):
    from bigquery_agent_analytics.extractor_compilation import publish_bundles_to_bq
    from bigquery_agent_analytics.extractor_compilation import sync_bundles_from_bq

    bundle_root = tmp_path / "bundles"
    bundle_root.mkdir()
    _build_bundle(bundle_root, name="a", fingerprint=_VALID_FINGERPRINT_A)
    _build_bundle(
        bundle_root,
        name="b",
        fingerprint=_VALID_FINGERPRINT_B,
        event_types=("other_event",),
    )

    store = _InMemoryStore()
    publish_bundles_to_bq(bundle_root=bundle_root, store=store)

    sync = sync_bundles_from_bq(
        store=store,
        dest_dir=tmp_path / "synced",
        bundle_fingerprint_allowlist=[_VALID_FINGERPRINT_A],
    )
    assert sync.synced_fingerprints == (_VALID_FINGERPRINT_A,)
    assert sync.skipped_fingerprints == ()
    assert sync.failures == ()
    # B's directory was never created.
    assert not (tmp_path / "synced" / _VALID_FINGERPRINT_B).exists()

  def test_sync_allowlist_missing_fingerprint_recorded(self, tmp_path):
    """Allowlist names a fingerprint that has no rows in the
    table. Surfaces as a ``fingerprint_not_in_table``
    failure rather than silently succeeding with zero
    synced bundles."""
    from bigquery_agent_analytics.extractor_compilation import sync_bundles_from_bq

    store = _InMemoryStore()
    sync = sync_bundles_from_bq(
        store=store,
        dest_dir=tmp_path / "synced",
        bundle_fingerprint_allowlist=[_VALID_FINGERPRINT_A],
    )
    assert sync.synced_fingerprints == ()
    assert len(sync.failures) == 1
    assert sync.failures[0].code == "fingerprint_not_in_table"
    assert sync.failures[0].bundle_fingerprint == _VALID_FINGERPRINT_A


# ------------------------------------------------------------------ #
# Path safety                                                         #
# ------------------------------------------------------------------ #


class TestPathSafety:

  def _build_store_with_path(
      self, *, fingerprint: str, bad_path: str
  ) -> "_InMemoryStore":
    """Helper: store has a valid manifest row plus a row whose
    bundle_path is malicious. Module file uses the bad path as
    its declared filename so we can isolate sync's
    path-validation behavior."""
    from bigquery_agent_analytics.extractor_compilation import BundleRow

    store = _InMemoryStore()
    manifest_payload = json.dumps(
        {
            "fingerprint": fingerprint,
            "event_types": ["bka_decision"],
            # module_filename intentionally normal so the
            # path check focuses on the actual row's
            # bundle_path.
            "module_filename": "extractor.py",
            "function_name": "extract_bka",
            "compiler_package_version": "0.0.0",
            "template_version": "v0.1",
            "transcript_builder_version": "tb-1",
            "created_at": "2026-05-08T00:00:00Z",
        },
        sort_keys=True,
        indent=2,
    ).encode("utf-8")
    store.publish_rows(
        [
            BundleRow(
                bundle_fingerprint=fingerprint,
                bundle_path="manifest.json",
                file_content=manifest_payload,
                event_types=("bka_decision",),
                module_filename="extractor.py",
                function_name="extract_bka",
                published_at="2026-05-08T00:00:00Z",
            ),
            BundleRow(
                bundle_fingerprint=fingerprint,
                bundle_path=bad_path,
                file_content=b"def extract_bka(event, spec): return None\n",
                event_types=("bka_decision",),
                module_filename="extractor.py",
                function_name="extract_bka",
                published_at="2026-05-08T00:00:00Z",
            ),
        ]
    )
    return store

  def test_sync_rejects_traversal_path(self, tmp_path: pathlib.Path):
    """``../`` in bundle_path is rejected before any file is
    written. The sync must NOT create
    ``dest_dir/../something``."""
    from bigquery_agent_analytics.extractor_compilation import sync_bundles_from_bq

    store = self._build_store_with_path(
        fingerprint=_VALID_FINGERPRINT_A, bad_path="../escape.py"
    )
    sync_dir = tmp_path / "synced"
    sync = sync_bundles_from_bq(store=store, dest_dir=sync_dir)

    assert sync.synced_fingerprints == ()
    assert sync.skipped_fingerprints == (_VALID_FINGERPRINT_A,)
    codes = {f.code for f in sync.failures}
    assert "invalid_bundle_path" in codes
    # No file was written outside the dest_dir.
    assert not (sync_dir.parent / "escape.py").exists()

  def test_sync_rejects_absolute_path(self, tmp_path: pathlib.Path):
    from bigquery_agent_analytics.extractor_compilation import sync_bundles_from_bq

    store = self._build_store_with_path(
        fingerprint=_VALID_FINGERPRINT_A, bad_path="/etc/passwd"
    )
    sync = sync_bundles_from_bq(store=store, dest_dir=tmp_path / "synced")

    assert sync.synced_fingerprints == ()
    codes = {f.code for f in sync.failures}
    assert "invalid_bundle_path" in codes

  def test_sync_rejects_backslash_path(self, tmp_path: pathlib.Path):
    """Windows-style backslashes can hide traversal past a
    POSIX-only check. Rejected explicitly."""
    from bigquery_agent_analytics.extractor_compilation import sync_bundles_from_bq

    store = self._build_store_with_path(
        fingerprint=_VALID_FINGERPRINT_A,
        bad_path="..\\windows-style-escape.py",
    )
    sync = sync_bundles_from_bq(store=store, dest_dir=tmp_path / "synced")

    codes = {f.code for f in sync.failures}
    assert "invalid_bundle_path" in codes


# ------------------------------------------------------------------ #
# Missing / malformed rows                                            #
# ------------------------------------------------------------------ #


class TestMissingAndMalformedRows:

  def test_sync_rejects_when_manifest_row_missing(self, tmp_path: pathlib.Path):
    """Bundle has a module row but no manifest row → fail
    closed with ``manifest_row_missing`` and skip the
    fingerprint."""
    from bigquery_agent_analytics.extractor_compilation import BundleRow
    from bigquery_agent_analytics.extractor_compilation import sync_bundles_from_bq

    store = _InMemoryStore()
    store.publish_rows(
        [
            BundleRow(
                bundle_fingerprint=_VALID_FINGERPRINT_A,
                bundle_path="extractor.py",
                file_content=b"def extract_bka(event, spec): return None\n",
                event_types=("bka_decision",),
                module_filename="extractor.py",
                function_name="extract_bka",
                published_at="2026-05-08T00:00:00Z",
            )
        ]
    )

    sync = sync_bundles_from_bq(store=store, dest_dir=tmp_path / "synced")
    assert sync.synced_fingerprints == ()
    assert sync.skipped_fingerprints == (_VALID_FINGERPRINT_A,)
    assert len(sync.failures) == 1
    assert sync.failures[0].code == "manifest_row_missing"

  def test_sync_rejects_malformed_manifest_row(self, tmp_path: pathlib.Path):
    """Manifest row exists but isn't valid JSON → fail closed
    with ``manifest_row_unreadable``."""
    from bigquery_agent_analytics.extractor_compilation import BundleRow
    from bigquery_agent_analytics.extractor_compilation import sync_bundles_from_bq

    store = _InMemoryStore()
    store.publish_rows(
        [
            BundleRow(
                bundle_fingerprint=_VALID_FINGERPRINT_A,
                bundle_path="manifest.json",
                file_content=b"this is not json",
                event_types=("bka_decision",),
                module_filename="extractor.py",
                function_name="extract_bka",
                published_at="2026-05-08T00:00:00Z",
            )
        ]
    )

    sync = sync_bundles_from_bq(store=store, dest_dir=tmp_path / "synced")
    assert sync.synced_fingerprints == ()
    codes = {f.code for f in sync.failures}
    assert "manifest_row_unreadable" in codes

  def test_sync_rejects_unexpected_file_in_bundle(self, tmp_path: pathlib.Path):
    """Bundle has manifest + module + an extra file. Bundles
    are exactly two files; the extra one is rejected via
    ``unexpected_file``."""
    from bigquery_agent_analytics.extractor_compilation import BundleRow
    from bigquery_agent_analytics.extractor_compilation import sync_bundles_from_bq

    store = _InMemoryStore()
    manifest_payload = json.dumps(
        {
            "fingerprint": _VALID_FINGERPRINT_A,
            "event_types": ["bka_decision"],
            "module_filename": "extractor.py",
            "function_name": "extract_bka",
            "compiler_package_version": "0.0.0",
            "template_version": "v0.1",
            "transcript_builder_version": "tb-1",
            "created_at": "2026-05-08T00:00:00Z",
        },
        sort_keys=True,
        indent=2,
    ).encode("utf-8")
    store.publish_rows(
        [
            BundleRow(
                bundle_fingerprint=_VALID_FINGERPRINT_A,
                bundle_path="manifest.json",
                file_content=manifest_payload,
                event_types=("bka_decision",),
                module_filename="extractor.py",
                function_name="extract_bka",
                published_at="2026-05-08T00:00:00Z",
            ),
            BundleRow(
                bundle_fingerprint=_VALID_FINGERPRINT_A,
                bundle_path="extractor.py",
                file_content=_MINIMAL_VALID_SOURCE.encode("utf-8"),
                event_types=("bka_decision",),
                module_filename="extractor.py",
                function_name="extract_bka",
                published_at="2026-05-08T00:00:00Z",
            ),
            BundleRow(
                bundle_fingerprint=_VALID_FINGERPRINT_A,
                bundle_path="README.md",
                file_content=b"# extra file the mirror should refuse",
                event_types=("bka_decision",),
                module_filename="extractor.py",
                function_name="extract_bka",
                published_at="2026-05-08T00:00:00Z",
            ),
        ]
    )

    sync = sync_bundles_from_bq(store=store, dest_dir=tmp_path / "synced")
    assert sync.synced_fingerprints == ()
    codes = {f.code for f in sync.failures}
    assert "unexpected_file" in codes
    # The bundle dir for that fingerprint must not exist —
    # sync rejected the fingerprint before writing.
    assert not (tmp_path / "synced" / _VALID_FINGERPRINT_A).exists()

  def test_sync_rejects_malformed_row_type(self, tmp_path: pathlib.Path):
    """A row whose ``file_content`` isn't bytes / ``event_types``
    isn't a tuple of strings fails the shape check."""
    from bigquery_agent_analytics.extractor_compilation import BundleRow
    from bigquery_agent_analytics.extractor_compilation import sync_bundles_from_bq

    # BundleRow is frozen but we can construct one with a
    # malformed field; the shape check at sync time is what
    # catches it.
    bad_row = BundleRow(
        bundle_fingerprint=_VALID_FINGERPRINT_A,
        bundle_path="manifest.json",
        # str instead of bytes
        file_content="not bytes",  # type: ignore[arg-type]
        event_types=("bka_decision",),
        module_filename="extractor.py",
        function_name="extract_bka",
        published_at="2026-05-08T00:00:00Z",
    )
    store = _InMemoryStore()
    store.publish_rows([bad_row])

    sync = sync_bundles_from_bq(store=store, dest_dir=tmp_path / "synced")
    assert sync.synced_fingerprints == ()
    codes = {f.code for f in sync.failures}
    assert "malformed_row" in codes

  def test_sync_rejects_duplicate_rows(self, tmp_path: pathlib.Path):
    """The BQ table has no unique constraint; if two rows
    share the same ``(bundle_fingerprint, bundle_path)``,
    sync rejects rather than picking one."""
    from bigquery_agent_analytics.extractor_compilation import BundleRow
    from bigquery_agent_analytics.extractor_compilation import sync_bundles_from_bq

    manifest_payload = json.dumps(
        {
            "fingerprint": _VALID_FINGERPRINT_A,
            "event_types": ["bka_decision"],
            "module_filename": "extractor.py",
            "function_name": "extract_bka",
            "compiler_package_version": "0.0.0",
            "template_version": "v0.1",
            "transcript_builder_version": "tb-1",
            "created_at": "2026-05-08T00:00:00Z",
        },
        sort_keys=True,
        indent=2,
    ).encode("utf-8")

    base_row = BundleRow(
        bundle_fingerprint=_VALID_FINGERPRINT_A,
        bundle_path="manifest.json",
        file_content=manifest_payload,
        event_types=("bka_decision",),
        module_filename="extractor.py",
        function_name="extract_bka",
        published_at="2026-05-08T00:00:00Z",
    )

    store = _InMemoryStore()
    store.publish_rows([base_row])
    # Force a duplicate row past the upsert dedup so we can
    # exercise sync's defense.
    store.force_insert(base_row)

    sync = sync_bundles_from_bq(store=store, dest_dir=tmp_path / "synced")
    codes = {f.code for f in sync.failures}
    assert "duplicate_row" in codes
    assert sync.synced_fingerprints == ()


# ------------------------------------------------------------------ #
# Idempotent republish                                                #
# ------------------------------------------------------------------ #


class TestIdempotentRepublish:

  def test_republishing_same_bundle_does_not_accumulate_rows(
      self, tmp_path: pathlib.Path
  ):
    """``publish_rows`` is upsert-by-``(fingerprint, path)``.
    Two consecutive publishes of the same bundle produce the
    same final row set, not double rows."""
    from bigquery_agent_analytics.extractor_compilation import publish_bundles_to_bq

    bundle_root = tmp_path / "bundles"
    bundle_root.mkdir()
    _build_bundle(bundle_root, name="bka", fingerprint=_VALID_FINGERPRINT_A)

    store = _InMemoryStore()
    publish_bundles_to_bq(bundle_root=bundle_root, store=store)
    rows_after_first = len(store.all_rows())
    publish_bundles_to_bq(bundle_root=bundle_root, store=store)
    rows_after_second = len(store.all_rows())

    assert rows_after_first == 2
    assert rows_after_second == 2


# ------------------------------------------------------------------ #
# Publish-side failure surfaces                                       #
# ------------------------------------------------------------------ #


class TestPublishFailures:

  def test_publish_skips_subdir_without_manifest(self, tmp_path: pathlib.Path):
    """A subdir without ``manifest.json`` lands in
    ``failures`` rather than crashing publish."""
    from bigquery_agent_analytics.extractor_compilation import publish_bundles_to_bq

    bundle_root = tmp_path / "bundles"
    bundle_root.mkdir()
    (bundle_root / "not-a-bundle").mkdir()

    store = _InMemoryStore()
    publish = publish_bundles_to_bq(bundle_root=bundle_root, store=store)
    codes = {f.code for f in publish.failures}
    assert "manifest_missing" in codes
    assert publish.published_fingerprints == ()
    assert publish.rows_written == 0

  def test_publish_skips_bundle_that_would_not_load(
      self, tmp_path: pathlib.Path
  ):
    """A bundle whose source doesn't define the manifest's
    ``function_name`` fails pre-publish via ``load_bundle``.
    Surfaces as ``bundle_load_failed``; nothing is published."""
    from bigquery_agent_analytics.extractor_compilation import publish_bundles_to_bq

    bundle_root = tmp_path / "bundles"
    bundle_root.mkdir()
    bundle_dir = bundle_root / "broken"
    _write_manifest(bundle_dir, fingerprint=_VALID_FINGERPRINT_A)
    (bundle_dir / "extractor.py").write_text(
        # No function named ``extract_bka``.
        "def something_else(event, spec): return None\n",
        encoding="utf-8",
    )

    store = _InMemoryStore()
    publish = publish_bundles_to_bq(bundle_root=bundle_root, store=store)
    codes = {f.code for f in publish.failures}
    assert "bundle_load_failed" in codes
    assert publish.published_fingerprints == ()
    assert publish.rows_written == 0
    assert store.all_rows() == []

  def test_publish_handles_missing_bundle_root(self, tmp_path: pathlib.Path):
    """A non-existent ``bundle_root`` yields a single
    ``bundle_root_missing`` failure rather than an
    exception."""
    from bigquery_agent_analytics.extractor_compilation import publish_bundles_to_bq

    publish = publish_bundles_to_bq(
        bundle_root=tmp_path / "does-not-exist",
        store=_InMemoryStore(),
    )
    assert publish.rows_written == 0
    codes = {f.code for f in publish.failures}
    assert "bundle_root_missing" in codes

  def test_publish_rejects_duplicate_fingerprints_across_subdirs(
      self, tmp_path: pathlib.Path
  ):
    """Two subdirs of ``bundle_root`` declaring the SAME
    manifest fingerprint must NOT both publish — the mirror is
    keyed on ``(fingerprint, bundle_path)`` and both would
    INSERT logical duplicates that sync later rejects. The
    fix: fail-closed at publish, neither subdir's rows are
    written."""
    from bigquery_agent_analytics.extractor_compilation import publish_bundles_to_bq

    bundle_root = tmp_path / "bundles"
    bundle_root.mkdir()
    _build_bundle(bundle_root, name="copy-a", fingerprint=_VALID_FINGERPRINT_A)
    _build_bundle(bundle_root, name="copy-b", fingerprint=_VALID_FINGERPRINT_A)

    store = _InMemoryStore()
    publish = publish_bundles_to_bq(bundle_root=bundle_root, store=store)

    assert publish.published_fingerprints == ()
    assert publish.rows_written == 0
    codes = {f.code for f in publish.failures}
    assert "duplicate_fingerprint" in codes
    # The detail names both participating subdirs so an
    # operator can find them.
    detail = next(
        f.detail for f in publish.failures if f.code == "duplicate_fingerprint"
    )
    assert "copy-a" in detail and "copy-b" in detail
    # No rows leaked into the store.
    assert store.all_rows() == []


# ------------------------------------------------------------------ #
# Round-2 reviewer findings                                           #
# ------------------------------------------------------------------ #


class TestRoundTwoFindings:
  """Reproducers + locks for the four PR #148 reviewer findings.

  The first three are functional bugs (sync would raise on a
  malformed manifest, sync would destroy good local state on a
  bad mirror row, publish would silently emit logical
  duplicates). The fourth is a defensive raise on the store.
  """

  def _good_bundle(
      self,
      tmp_path: pathlib.Path,
      *,
      fingerprint: str = _VALID_FINGERPRINT_A,
  ) -> pathlib.Path:
    bundle_root = tmp_path / "bundles"
    bundle_root.mkdir(exist_ok=True)
    _build_bundle(bundle_root, name="bka", fingerprint=fingerprint)
    return bundle_root

  def test_sync_rejects_manifest_with_path_separator_in_module_filename(
      self, tmp_path: pathlib.Path
  ):
    """Manifest row whose ``module_filename`` contains ``/``
    used to slip past ``_validate_bundle_path`` and trigger
    ``FileNotFoundError`` at the write step (the parent dir
    doesn't exist). The shape check now catches it at
    ``manifest_row_unreadable`` with a clear message."""
    from bigquery_agent_analytics.extractor_compilation import BundleRow
    from bigquery_agent_analytics.extractor_compilation import sync_bundles_from_bq

    # bundle_path equals the malformed module_filename so the
    # path-traversal check (which doesn't reject simple
    # subdirs) would otherwise let this through.
    manifest_payload = json.dumps(
        {
            "fingerprint": _VALID_FINGERPRINT_A,
            "event_types": ["bka_decision"],
            "module_filename": "subdir/extractor.py",
            "function_name": "extract_bka",
            "compiler_package_version": "0.0.0",
            "template_version": "v0.1",
            "transcript_builder_version": "tb-1",
            "created_at": "2026-05-08T00:00:00Z",
        },
        sort_keys=True,
        indent=2,
    ).encode("utf-8")
    store = _InMemoryStore()
    store.publish_rows(
        [
            BundleRow(
                bundle_fingerprint=_VALID_FINGERPRINT_A,
                bundle_path="manifest.json",
                file_content=manifest_payload,
                event_types=("bka_decision",),
                module_filename="subdir/extractor.py",
                function_name="extract_bka",
                published_at="2026-05-08T00:00:00Z",
            ),
            BundleRow(
                bundle_fingerprint=_VALID_FINGERPRINT_A,
                bundle_path="subdir/extractor.py",
                file_content=_MINIMAL_VALID_SOURCE.encode("utf-8"),
                event_types=("bka_decision",),
                module_filename="subdir/extractor.py",
                function_name="extract_bka",
                published_at="2026-05-08T00:00:00Z",
            ),
        ]
    )

    sync = sync_bundles_from_bq(store=store, dest_dir=tmp_path / "synced")
    assert sync.synced_fingerprints == ()
    codes = {f.code for f in sync.failures}
    assert "manifest_row_unreadable" in codes
    # The bundle dir was never written — staging stays
    # internal to sync.
    assert not (tmp_path / "synced" / _VALID_FINGERPRINT_A).exists()

  def test_sync_failure_preserves_existing_good_bundle(
      self, tmp_path: pathlib.Path
  ):
    """If a previously-good bundle exists at
    ``dest_dir/<fingerprint>/`` and a later sync brings back
    corrupt rows for the same fingerprint, the existing good
    bundle must NOT be destroyed. The staging-then-validate
    flow writes to a side directory; the target is replaced
    only after ``load_bundle`` succeeds on the staged copy."""
    from bigquery_agent_analytics.extractor_compilation import BundleRow
    from bigquery_agent_analytics.extractor_compilation import load_bundle
    from bigquery_agent_analytics.extractor_compilation import LoadedBundle
    from bigquery_agent_analytics.extractor_compilation import publish_bundles_to_bq
    from bigquery_agent_analytics.extractor_compilation import sync_bundles_from_bq

    # Step 1: publish a good bundle, sync it locally so we
    # have an established good local state.
    bundle_root = self._good_bundle(tmp_path)
    store = _InMemoryStore()
    publish_bundles_to_bq(bundle_root=bundle_root, store=store)
    sync_dir = tmp_path / "synced"
    initial_sync = sync_bundles_from_bq(store=store, dest_dir=sync_dir)
    assert initial_sync.synced_fingerprints == (_VALID_FINGERPRINT_A,)

    good_bundle = sync_dir / _VALID_FINGERPRINT_A
    good_module_bytes = (good_bundle / "extractor.py").read_bytes()

    # Step 2: corrupt the store — replace the module row with
    # garbage source the loader will reject. Same fingerprint
    # in the manifest row, so the corrupt re-sync targets the
    # exact local directory we want to protect.
    corrupt_manifest = (good_bundle / "manifest.json").read_bytes()
    store_with_corruption = _InMemoryStore()
    store_with_corruption.publish_rows(
        [
            BundleRow(
                bundle_fingerprint=_VALID_FINGERPRINT_A,
                bundle_path="manifest.json",
                file_content=corrupt_manifest,
                event_types=("bka_decision",),
                module_filename="extractor.py",
                function_name="extract_bka",
                published_at="2026-05-08T00:00:00Z",
            ),
            BundleRow(
                bundle_fingerprint=_VALID_FINGERPRINT_A,
                bundle_path="extractor.py",
                # No function named extract_bka → load_bundle
                # rejects with function_not_found.
                file_content=b"def something_else(event, spec): return None\n",
                event_types=("bka_decision",),
                module_filename="extractor.py",
                function_name="extract_bka",
                published_at="2026-05-08T00:00:00Z",
            ),
        ]
    )

    # Step 3: re-sync from the corrupt store. The bundle dir
    # exists; the staged reconstruction must fail load_bundle;
    # the existing good bundle must NOT be destroyed.
    second_sync = sync_bundles_from_bq(
        store=store_with_corruption, dest_dir=sync_dir
    )
    assert second_sync.synced_fingerprints == ()
    codes = {f.code for f in second_sync.failures}
    assert "bundle_load_failed" in codes

    # Local good bundle is intact.
    assert good_bundle.exists()
    assert (good_bundle / "extractor.py").read_bytes() == good_module_bytes
    outcome = load_bundle(
        good_bundle, expected_fingerprint=_VALID_FINGERPRINT_A
    )
    assert isinstance(outcome, LoadedBundle)

    # Staging directories were cleaned up — no orphaned
    # ".staging-*" dirs left behind.
    leftover_staging = [
        p for p in sync_dir.iterdir() if p.name.startswith(".staging-")
    ]
    assert leftover_staging == []

  def test_publish_rows_rejects_duplicate_input_pairs(self):
    """``BigQueryBundleStore.publish_rows`` raises
    ``ValueError`` on duplicate ``(fingerprint, path)`` input
    pairs — defense in depth on top of the publisher-side
    ``duplicate_fingerprint`` guard."""
    from bigquery_agent_analytics.extractor_compilation import BigQueryBundleStore
    from bigquery_agent_analytics.extractor_compilation import BundleRow

    row = BundleRow(
        bundle_fingerprint=_VALID_FINGERPRINT_A,
        bundle_path="manifest.json",
        file_content=b"{}",
        event_types=("bka_decision",),
        module_filename="extractor.py",
        function_name="extract_bka",
        published_at="2026-05-08T00:00:00Z",
    )

    class _FakeBQClient:

      def query(self, *args, **kwargs):
        raise AssertionError("DELETE must not run on duplicate input")

      def insert_rows_json(self, *args, **kwargs):
        raise AssertionError("INSERT must not run on duplicate input")

    store = BigQueryBundleStore(bq_client=_FakeBQClient(), table_id="p.d.t")
    with pytest.raises(ValueError, match=r"duplicate.*manifest\.json"):
      store.publish_rows([row, row])

  def test_sync_rejects_tampered_bundle_fingerprint_path_escape(
      self, tmp_path: pathlib.Path
  ):
    """A row whose ``bundle_fingerprint="../escape"`` used to
    sync successfully and write OUTSIDE ``dest_dir`` because
    the fingerprint flows straight into ``dest_dir /
    fingerprint``. The shape check now requires strict 64-char
    lowercase hex (sha256) and rejects anything else as
    ``malformed_row`` BEFORE any path is computed."""
    from bigquery_agent_analytics.extractor_compilation import BundleRow
    from bigquery_agent_analytics.extractor_compilation import sync_bundles_from_bq

    tampered_row = BundleRow(
        bundle_fingerprint="../escape",
        bundle_path="manifest.json",
        file_content=b"{}",
        event_types=("bka_decision",),
        module_filename="extractor.py",
        function_name="extract_bka",
        published_at="2026-05-08T00:00:00Z",
    )
    store = _InMemoryStore()
    store.publish_rows([tampered_row])

    sync_dir = tmp_path / "synced"
    sync = sync_bundles_from_bq(store=store, dest_dir=sync_dir)

    assert sync.synced_fingerprints == ()
    codes = {f.code for f in sync.failures}
    assert "malformed_row" in codes
    # Crucially: nothing was written outside dest_dir.
    assert not (sync_dir.parent / "escape").exists()
    # And no ``../escape`` subdir of dest_dir either.
    assert not any(p.name == "escape" for p in sync_dir.parent.iterdir())

  def test_publish_rejects_tampered_manifest_fingerprint(
      self, tmp_path: pathlib.Path
  ):
    """Defense in depth: the publish-side shape check now
    catches a manifest whose ``fingerprint`` isn't a clean
    sha256 hex string, so a tampered local manifest can never
    introduce a path-escape value into the table."""
    from bigquery_agent_analytics.extractor_compilation import publish_bundles_to_bq

    bundle_root = tmp_path / "bundles"
    bundle_root.mkdir()
    _write_manifest(
        bundle_root / "tampered",
        fingerprint="../escape",
    )
    (bundle_root / "tampered" / "extractor.py").write_text(
        _MINIMAL_VALID_SOURCE, encoding="utf-8"
    )

    store = _InMemoryStore()
    publish = publish_bundles_to_bq(bundle_root=bundle_root, store=store)

    assert publish.published_fingerprints == ()
    assert publish.rows_written == 0
    codes = {f.code for f in publish.failures}
    assert "manifest_unreadable" in codes
    # Detail names the offending fingerprint shape so an
    # operator can spot the tampered file.
    detail = next(
        f.detail for f in publish.failures if f.code == "manifest_unreadable"
    )
    assert "fingerprint" in detail
    assert store.all_rows() == []

  def test_bigquery_store_rejects_malformed_table_id(self):
    """``BigQueryBundleStore.__init__`` validates ``table_id``
    at construction so a malformed value (backtick, semicolon,
    wrong dot count, injection attempt) raises ``ValueError``
    before any SQL interpolation. The constructor is the only
    place where ``table_id`` enters a backticked SQL string;
    catching here prevents downstream injection."""
    from bigquery_agent_analytics.extractor_compilation import BigQueryBundleStore

    class _FakeBQClient:
      pass

    fake = _FakeBQClient()

    # Wrong dot count.
    with pytest.raises(ValueError, match=r"not a well-formed"):
      BigQueryBundleStore(bq_client=fake, table_id="onlyone")
    with pytest.raises(ValueError, match=r"not a well-formed"):
      BigQueryBundleStore(bq_client=fake, table_id="two.parts")
    with pytest.raises(ValueError, match=r"not a well-formed"):
      BigQueryBundleStore(bq_client=fake, table_id="four.parts.here.tbl")

    # Backtick — would break out of the quoted identifier.
    with pytest.raises(ValueError, match=r"not a well-formed"):
      BigQueryBundleStore(
          bq_client=fake, table_id="proj.ds.tbl`; DROP TABLE x; --"
      )

    # Semicolon / SQL injection markers.
    with pytest.raises(ValueError, match=r"not a well-formed"):
      BigQueryBundleStore(bq_client=fake, table_id="proj.ds.tbl;DROP")
    with pytest.raises(ValueError, match=r"not a well-formed"):
      BigQueryBundleStore(bq_client=fake, table_id="proj.ds.tbl --comment")

    # Whitespace.
    with pytest.raises(ValueError, match=r"not a well-formed"):
      BigQueryBundleStore(bq_client=fake, table_id="proj. ds.tbl")
    with pytest.raises(ValueError, match=r"not a well-formed"):
      BigQueryBundleStore(bq_client=fake, table_id="proj.ds.tbl\n")

    # Empty segment.
    with pytest.raises(ValueError, match=r"not a well-formed"):
      BigQueryBundleStore(bq_client=fake, table_id="proj..tbl")

    # Non-string.
    with pytest.raises(ValueError, match=r"must be a string"):
      BigQueryBundleStore(bq_client=fake, table_id=None)  # type: ignore[arg-type]

    # Valid forms — should NOT raise.
    BigQueryBundleStore(
        bq_client=fake, table_id="my-project.my_dataset.compiled_bundles"
    )
    BigQueryBundleStore(bq_client=fake, table_id="p.d.t")
