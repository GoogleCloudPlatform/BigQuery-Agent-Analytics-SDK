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

"""Live BigQuery test for ``bqaa-revalidate-extractors
--events-bq-query-file``.

Skipped by default. To run, set:

    BQAA_RUN_LIVE_TESTS=1
    BQAA_RUN_LIVE_BQ_REVALIDATE_TESTS=1
    PROJECT_ID=...
    DATASET_ID=...
    BQAA_BQ_LOCATION=US                  # optional, defaults to US

The test creates a temporary table in ``DATASET_ID``,
populates it with two ``event_json`` rows shaped like BKA
decision events, runs the CLI against a SQL file selecting
that table, asserts the report is written with the expected
shape, then drops the table.

CI does NOT run this — the BigQuery dependency is
intentionally opt-in.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import textwrap
import types
import uuid

import pytest

_LIVE = (
    os.environ.get("BQAA_RUN_LIVE_TESTS") == "1"
    and os.environ.get("BQAA_RUN_LIVE_BQ_REVALIDATE_TESTS") == "1"
)

pytestmark = pytest.mark.skipif(
    not _LIVE,
    reason=(
        "Live BQ revalidate-CLI tests skipped. Set "
        "BQAA_RUN_LIVE_TESTS=1 plus BQAA_RUN_LIVE_BQ_REVALIDATE_TESTS=1 "
        "plus PROJECT_ID + DATASET_ID to opt in. Default CI does NOT "
        "run this — the BigQuery dependency is intentionally opt-in."
    ),
)


_VALID_FINGERPRINT = "d" * 64

_MINIMAL_COMPILED_SOURCE = textwrap.dedent(
    """\
    from bigquery_agent_analytics.structured_extraction import extract_bka_decision_event

    def extract_bka(event, spec):
        return extract_bka_decision_event(event, spec)
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
      _MINIMAL_COMPILED_SOURCE, encoding="utf-8"
  )


def _install_reference_module() -> str:
  """Synthesize a reference-extractors module backed by the
  real BKA fixtures."""
  import tempfile

  from bigquery_agent_analytics.resolved_spec import resolve
  from bigquery_agent_analytics.structured_extraction import extract_bka_decision_event
  from bigquery_ontology import load_binding
  from bigquery_ontology import load_ontology
  from tests.fixtures_extractor_compilation.bka_decision_inputs import BKA_BINDING_YAML
  from tests.fixtures_extractor_compilation.bka_decision_inputs import BKA_ONTOLOGY_YAML

  tmp = pathlib.Path(tempfile.mkdtemp(prefix="bka_live_cli_"))
  (tmp / "ont.yaml").write_text(BKA_ONTOLOGY_YAML, encoding="utf-8")
  (tmp / "bnd.yaml").write_text(BKA_BINDING_YAML, encoding="utf-8")
  ontology = load_ontology(str(tmp / "ont.yaml"))
  binding = load_binding(str(tmp / "bnd.yaml"), ontology=ontology)

  module_name = "_bqaa_live_cli_bq_ref"
  mod = types.ModuleType(module_name)
  mod.EXTRACTORS = {"bka_decision": extract_bka_decision_event}
  mod.RESOLVED_GRAPH = resolve(ontology, binding)
  sys.modules[module_name] = mod
  return module_name


def test_live_bq_query_round_trip(tmp_path: pathlib.Path):
  """Minimal live round-trip: create a temp table with two
  ``event_json`` rows shaped like BKA decisions, run the CLI
  against it, assert the report has two compiled_unchanged
  events with parity matches. Drops the table on the way
  out."""
  from google.cloud import bigquery

  from bigquery_agent_analytics.extractor_compilation.cli_revalidate import main

  project_id = os.environ["PROJECT_ID"]
  dataset_id = os.environ["DATASET_ID"]
  location = os.environ.get("BQAA_BQ_LOCATION", "US")

  table_name = f"bqaa_revalidate_cli_test_{uuid.uuid4().hex[:12]}"
  table_id = f"{project_id}.{dataset_id}.{table_name}"
  client = bigquery.Client(project=project_id, location=location)

  schema = [bigquery.SchemaField("event_json", "STRING", mode="REQUIRED")]
  table = bigquery.Table(table_id, schema=schema)
  client.create_table(table)
  try:
    # Two BKA events.
    rows = [
        {
            "event_json": json.dumps(
                {
                    "event_type": "bka_decision",
                    "session_id": "live-sess-1",
                    "span_id": "live-sp-1",
                    "content": {
                        "decision_id": "live-d-1",
                        "outcome": "approved",
                        "confidence": 0.9,
                    },
                }
            )
        },
        {
            "event_json": json.dumps(
                {
                    "event_type": "bka_decision",
                    "session_id": "live-sess-1",
                    "span_id": "live-sp-2",
                    "content": {
                        "decision_id": "live-d-2",
                        "outcome": "approved",
                        "confidence": 0.95,
                    },
                }
            )
        },
    ]
    errors = client.insert_rows_json(table_id, rows)
    assert not errors, f"insert errors: {errors!r}"

    bundles_root = tmp_path / "bundles"
    _write_bundle(bundles_root / "bka")

    query_path = tmp_path / "events.sql"
    query_path.write_text(
        f"SELECT event_json FROM `{table_id}`", encoding="utf-8"
    )

    ref_module = _install_reference_module()
    report_out = tmp_path / "report.json"

    code = main(
        [
            "--bundles-root",
            str(bundles_root),
            "--events-bq-query-file",
            str(query_path),
            "--bq-project",
            project_id,
            "--bq-location",
            location,
            "--reference-extractors-module",
            ref_module,
            "--report-out",
            str(report_out),
        ]
    )

    assert code == 0
    payload = json.loads(report_out.read_text(encoding="utf-8"))
    assert payload["report"]["total_events"] == 2
    assert payload["report"]["total_compiled_unchanged"] == 2
    assert payload["report"]["total_parity_matches"] == 2
  finally:
    client.delete_table(table_id, not_found_ok=True)
    sys.modules.pop("_bqaa_live_cli_bq_ref", None)
