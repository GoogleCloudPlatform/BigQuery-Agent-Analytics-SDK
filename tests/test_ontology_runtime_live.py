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

"""Live BigQuery test for the ontology runtime reader
(issue #58 reader follow-on to PR #92).

Skipped by default. To run, set:

    BQAA_RUN_LIVE_TESTS=1
    BQAA_RUN_LIVE_ONTOLOGY_RUNTIME_TESTS=1
    PROJECT_ID=...
    DATASET_ID=...
    BQAA_BQ_LOCATION=US                  # optional, defaults to US

The test:

1. Compiles a tiny ontology + binding to concept-index SQL
   via the existing emission path (PR #92).
2. Executes the emitted DDL to create the main + ``__meta``
   tables in ``DATASET_ID``.
3. Constructs an :class:`OntologyRuntime` against the same
   ontology + binding + the freshly-created table. Fingerprint
   verification runs eagerly; the test asserts it passes.
4. Runs a few representative ``LabelSynonymResolver`` queries
   and asserts the candidates carry the table's actual
   ``compile_fingerprint``.
5. Drops both tables on the way out.

CI does NOT run this — the BigQuery dependency is
intentionally opt-in.
"""

from __future__ import annotations

import os
import pathlib
import tempfile
import textwrap
import uuid

import pytest

_LIVE = (
    os.environ.get("BQAA_RUN_LIVE_TESTS") == "1"
    and os.environ.get("BQAA_RUN_LIVE_ONTOLOGY_RUNTIME_TESTS") == "1"
)

pytestmark = pytest.mark.skipif(
    not _LIVE,
    reason=(
        "Live ontology-runtime tests skipped. Set "
        "BQAA_RUN_LIVE_TESTS=1 plus "
        "BQAA_RUN_LIVE_ONTOLOGY_RUNTIME_TESTS=1 plus "
        "PROJECT_ID + DATASET_ID to opt in. Default CI does NOT "
        "run this — the BigQuery dependency is intentionally opt-in."
    ),
)


_COMPILER_VERSION = "bigquery_agent_analytics live-runtime-test"

_ONTOLOGY_YAML = textwrap.dedent(
    """\
    ontology: live_runtime_test
    version: "0.1"
    entities:
      - name: Region
        abstract: true
        annotations:
          skos:prefLabel: Region
      - name: CaliforniaRegion
        extends: Region
        keys:
          primary: [code]
        properties:
          - name: code
            type: string
        synonyms: ["California", "CA"]
        annotations:
          # Two notations declared in NON-sorted order:
          # PR #92 emits one label_kind='notation' row per
          # value, but the per-row notation column carries
          # the lex-min ("CA", not "ZZ-LATE" — even though
          # "ZZ-LATE" is the first-authored value). The live
          # test asserts both:
          #   * lookup_by_notation finds BOTH values
          #     (round-2 fix), and
          #   * notation_for() returns the lex-min display
          #     token "CA", not the first-authored
          #     "ZZ-LATE" (round-3 fix).
          skos:notation: ["ZZ-LATE", "CA"]
    """
)

_BINDING_YAML = textwrap.dedent(
    """\
    binding: live_runtime_test_bq
    ontology: live_runtime_test
    target:
      backend: bigquery
      project: __PROJECT__
      dataset: __DATASET__
    entities:
      - name: CaliforniaRegion
        source: california_regions
        properties:
          - name: code
            column: code
    """
)


def test_live_runtime_round_trip(tmp_path: pathlib.Path):
  """Emit a concept index against a real BQ dataset, attach
  the runtime, run a few resolver queries, assert provenance
  matches end-to-end."""
  from google.cloud import bigquery

  from bigquery_agent_analytics import LabelSynonymResolver
  from bigquery_agent_analytics import OntologyRuntime
  from bigquery_ontology import compile_concept_index
  from bigquery_ontology import load_binding
  from bigquery_ontology import load_ontology

  project_id = os.environ["PROJECT_ID"]
  dataset_id = os.environ["DATASET_ID"]
  location = os.environ.get("BQAA_BQ_LOCATION", "US")

  ontology_path = tmp_path / "ont.yaml"
  binding_path = tmp_path / "bnd.yaml"
  ontology_path.write_text(_ONTOLOGY_YAML, encoding="utf-8")
  binding_path.write_text(
      _BINDING_YAML.replace("__PROJECT__", project_id).replace(
          "__DATASET__", dataset_id
      ),
      encoding="utf-8",
  )
  ontology = load_ontology(str(ontology_path))
  binding = load_binding(str(binding_path), ontology=ontology)

  table_name = f"bqaa_runtime_test_{uuid.uuid4().hex[:12]}"
  table_id = f"{project_id}.{dataset_id}.{table_name}"
  meta_table_id = table_id + "__meta"

  client = bigquery.Client(project=project_id, location=location)
  sql = compile_concept_index(
      ontology,
      binding,
      output_table=table_id,
      compiler_version=_COMPILER_VERSION,
  )
  # ``compile_concept_index`` emits both CREATE OR REPLACE
  # statements separated by a blank line. Execute as a
  # multi-statement script.
  client.query(sql).result()

  try:
    runtime = OntologyRuntime.from_files(
        ontology_path=ontology_path,
        binding_path=binding_path,
        compiler_version=_COMPILER_VERSION,
        concept_index_table=table_id,
        bq_client=client,
    )
    assert runtime.concept_index is not None

    resolver = LabelSynonymResolver(runtime)
    candidates = resolver.resolve("California")
    assert candidates, "expected at least one candidate for 'California'"
    # Every candidate's provenance matches the runtime's
    # expected fingerprint.
    for c in candidates:
      assert c.compile_fingerprint == runtime.compile_fingerprint

    # Notation lookup roundtrips through the same gate.
    # Primary (lex-min) notation: per-row notation column == "CA".
    rows = runtime.concept_index.lookup_by_notation("CA")
    assert rows, "expected at least one row for primary notation 'CA'"
    assert rows[0].entity_name == "CaliforniaRegion"
    assert rows[0].label == "CA"
    assert rows[0].label_kind == "notation"

    # Secondary notation: lookup_by_notation must STILL find
    # the entity even though the per-row notation column
    # carries "CA" (lex-min). Locks the round-1 P2 fix
    # against real BigQuery, not just the in-memory fake.
    rows_zz = runtime.concept_index.lookup_by_notation("ZZ-LATE")
    assert rows_zz, (
        "expected lookup_by_notation('ZZ-LATE') to find the entity even "
        "though the per-row notation column is 'CA' — querying by "
        "label_kind='notation' AND label is the only correct path"
    )
    assert rows_zz[0].entity_name == "CaliforniaRegion"
    assert rows_zz[0].label == "ZZ-LATE"
    assert rows_zz[0].label_kind == "notation"
    # And the per-row notation column is the display token,
    # NOT the queried value — proving the predicate isn't
    # accidentally matching that column.
    assert rows_zz[0].notation == "CA"

    # Round-3 lex-min display-token lock: runtime.notation_for()
    # must match what the concept-index rows expose, not the
    # first-authored value. The fixture declares
    # skos:notation: ["ZZ-LATE", "CA"] so first-authored is
    # "ZZ-LATE" but lex-min (the rule PR #92's emitter uses) is
    # "CA". Before the fix runtime would have returned
    # "ZZ-LATE" and disagreed with the concept-index column.
    assert runtime.notation_for("CaliforniaRegion") == "CA"
    assert set(runtime.notations_for("CaliforniaRegion")) == {"CA", "ZZ-LATE"}
  finally:
    client.delete_table(table_id, not_found_ok=True)
    client.delete_table(meta_table_id, not_found_ok=True)
