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

"""Live BigQuery round-trip test for the bundle mirror
(#75 PR C2.c.3).

Skipped by default. To run, set:

    BQAA_RUN_LIVE_TESTS=1
    BQAA_RUN_LIVE_BQ_MIRROR_TESTS=1
    PROJECT_ID=...
    DATASET_ID=...
    BQAA_BQ_LOCATION=US                  # optional, defaults to US

The test creates a temporary table in ``DATASET_ID``, runs
``publish_bundles_to_bq`` + ``sync_bundles_from_bq`` against
it, asserts the reconstructed bundle is loader-acceptable,
then drops the table. No bundle content leaves the local
machine outside of the BQ table the test owns.

CI does NOT run this — the BQ dependency is intentionally
opt-in.
"""

from __future__ import annotations

import json
import os
import pathlib
import textwrap
import uuid

import pytest

_LIVE = (
    os.environ.get("BQAA_RUN_LIVE_TESTS") == "1"
    and os.environ.get("BQAA_RUN_LIVE_BQ_MIRROR_TESTS") == "1"
)

pytestmark = pytest.mark.skipif(
    not _LIVE,
    reason=(
        "Live BQ mirror tests skipped. Set BQAA_RUN_LIVE_TESTS=1 plus "
        "BQAA_RUN_LIVE_BQ_MIRROR_TESTS=1 plus PROJECT_ID + DATASET_ID "
        "to opt in. Default CI does NOT run this — the BigQuery "
        "dependency is intentionally opt-in."
    ),
)


_VALID_FINGERPRINT = "c" * 64

_MINIMAL_VALID_SOURCE = textwrap.dedent(
    """\
    def extract_bka(event, spec):
        return None
    """
)


def _write_bundle(bundle_dir: pathlib.Path) -> None:
  bundle_dir.mkdir(parents=True, exist_ok=True)
  manifest = {
      "fingerprint": _VALID_FINGERPRINT,
      "event_types": ["bka_decision"],
      "module_filename": "extractor.py",
      "function_name": "extract_bka",
      "compiler_package_version": "0.0.0",
      "template_version": "v0.1",
      "transcript_builder_version": "tb-1",
      "created_at": "2026-05-12T00:00:00Z",
  }
  (bundle_dir / "manifest.json").write_text(
      json.dumps(manifest, sort_keys=True, indent=2), encoding="utf-8"
  )
  (bundle_dir / "extractor.py").write_text(
      _MINIMAL_VALID_SOURCE, encoding="utf-8"
  )


def test_live_bigquery_round_trip(tmp_path: pathlib.Path):
  """Publish + sync against a real BigQuery table.

  Assertions are contract-level: round-trip succeeds with no
  failures, the reconstructed bundle is loader-acceptable. The
  test doesn't pin BQ-specific behavior (job IDs, latency) —
  those are BigQuery's concern.
  """
  from google.cloud import bigquery

  from bigquery_agent_analytics.extractor_compilation import BigQueryBundleStore
  from bigquery_agent_analytics.extractor_compilation import load_bundle
  from bigquery_agent_analytics.extractor_compilation import LoadedBundle
  from bigquery_agent_analytics.extractor_compilation import publish_bundles_to_bq
  from bigquery_agent_analytics.extractor_compilation import sync_bundles_from_bq

  project_id = os.environ["PROJECT_ID"]
  dataset_id = os.environ["DATASET_ID"]
  location = os.environ.get("BQAA_BQ_LOCATION", "US")

  table_name = f"bqaa_mirror_test_{uuid.uuid4().hex[:12]}"
  table_id = f"{project_id}.{dataset_id}.{table_name}"
  client = bigquery.Client(project=project_id, location=location)

  store = BigQueryBundleStore(bq_client=client, table_id=table_id)
  store.ensure_table()
  try:
    bundle_root = tmp_path / "bundles"
    _write_bundle(bundle_root / "bka")

    publish = publish_bundles_to_bq(bundle_root=bundle_root, store=store)
    assert publish.failures == ()
    assert publish.published_fingerprints == (_VALID_FINGERPRINT,)
    assert publish.rows_written == 2

    sync_dir = tmp_path / "synced"
    sync = sync_bundles_from_bq(store=store, dest_dir=sync_dir)
    assert sync.failures == ()
    assert sync.synced_fingerprints == (_VALID_FINGERPRINT,)

    outcome = load_bundle(
        sync_dir / _VALID_FINGERPRINT,
        expected_fingerprint=_VALID_FINGERPRINT,
    )
    assert isinstance(outcome, LoadedBundle)
    assert outcome.manifest.fingerprint == _VALID_FINGERPRINT
    assert callable(outcome.extractor)
  finally:
    # Always clean up so consecutive runs don't accumulate
    # tables in the test dataset.
    client.delete_table(table_id, not_found_ok=True)
