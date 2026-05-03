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

"""Unit tests for binding_validation.validate_binding_against_bigquery.

Tests use a fake BQ client (a small inline class with a ``get_table``
method) so they do not require live BigQuery. Each of the seven
default-mode failure codes has at least one positive case plus the
"clean against SDK-created tables" regression test for the strict-only
KEY_COLUMN_NULLABLE check.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from typing import Optional

import pytest

# ------------------------------------------------------------------ #
# Fakes                                                                #
# ------------------------------------------------------------------ #


@dataclass
class _FakeField:
  """Shape-compatible with google.cloud.bigquery.SchemaField."""

  name: str
  field_type: str
  mode: str = "NULLABLE"


@dataclass
class _FakeTable:
  schema: list[_FakeField] = field(default_factory=list)


class _FakeBQClient:
  """Minimal get_table() impl backed by a {table_ref: schema} map.

  Missing table_refs raise ``NotFound``; entries with ``schema=None``
  raise other classified errors so we can exercise the
  MISSING_DATASET / INSUFFICIENT_PERMISSIONS branches.
  """

  def __init__(
      self,
      tables: dict[str, list[_FakeField]],
      *,
      missing_datasets: Optional[set[str]] = None,
      forbidden: Optional[set[str]] = None,
  ) -> None:
    self._tables = tables
    self._missing_datasets = missing_datasets or set()
    self._forbidden = forbidden or set()

  def get_table(self, table_ref: str):
    if table_ref in self._forbidden:
      raise PermissionError(f"403 Permission denied for table {table_ref}")
    parts = table_ref.split(".")
    if len(parts) >= 2:
      dataset_ref = ".".join(parts[:2])
      if dataset_ref in self._missing_datasets:
        raise LookupError(f"404 Not found: Dataset {dataset_ref} was not found")
    if table_ref not in self._tables:
      raise LookupError(f"404 Not found: Table {table_ref} does not exist")
    return _FakeTable(schema=list(self._tables[table_ref]))


# ------------------------------------------------------------------ #
# Fixture builders — minimal Ontology + Binding for one entity-edge   #
# pair, exercising every code path with small variations.              #
# ------------------------------------------------------------------ #


def _ontology_and_binding(
    project: str = "p",
    dataset: str = "d",
    entity_source: str = "decision_points",
    rel_source: str = "candidate_edges",
):
  from bigquery_ontology import load_binding
  from bigquery_ontology import load_ontology

  ontology_yaml = (
      "ontology: TestGraph\n"
      "entities:\n"
      "  - name: Decision\n"
      "    keys:\n"
      "      primary: [decision_id]\n"
      "    properties:\n"
      "      - name: decision_id\n"
      "        type: string\n"
      "      - name: confidence\n"
      "        type: double\n"
      "  - name: Outcome\n"
      "    keys:\n"
      "      primary: [outcome_id]\n"
      "    properties:\n"
      "      - name: outcome_id\n"
      "        type: string\n"
      "relationships:\n"
      "  - name: HasOutcome\n"
      "    from: Decision\n"
      "    to: Outcome\n"
      "    properties:\n"
      "      - name: weight\n"
      "        type: double\n"
  )
  binding_yaml = (
      "binding: test_bind\n"
      "ontology: TestGraph\n"
      "target:\n"
      "  backend: bigquery\n"
      f"  project: {project}\n"
      f"  dataset: {dataset}\n"
      "entities:\n"
      "  - name: Decision\n"
      f"    source: {entity_source}\n"
      "    properties:\n"
      "      - name: decision_id\n"
      "        column: decision_id\n"
      "      - name: confidence\n"
      "        column: confidence\n"
      "  - name: Outcome\n"
      "    source: outcomes\n"
      "    properties:\n"
      "      - name: outcome_id\n"
      "        column: outcome_id\n"
      "relationships:\n"
      "  - name: HasOutcome\n"
      f"    source: {rel_source}\n"
      "    from_columns: [decision_id]\n"
      "    to_columns: [outcome_id]\n"
      "    properties:\n"
      "      - name: weight\n"
      "        column: weight\n"
  )

  import pathlib
  import tempfile

  tmp = pathlib.Path(tempfile.mkdtemp(prefix="bind_validate_"))
  ont_path = tmp / "test.ontology.yaml"
  bnd_path = tmp / "test.binding.yaml"
  ont_path.write_text(ontology_yaml, encoding="utf-8")
  bnd_path.write_text(binding_yaml, encoding="utf-8")

  ontology = load_ontology(str(ont_path))
  binding = load_binding(str(bnd_path), ontology=ontology)
  return ontology, binding


def _good_schemas() -> dict[str, list[_FakeField]]:
  """All tables present with matching column types — clean baseline.

  Models tables produced by ``OntologyMaterializer.create_tables()``:
  NULLABLE everywhere (no NOT NULL), correct types per
  ``_DDL_TYPE_MAP``.
  """
  return {
      "p.d.decision_points": [
          _FakeField("decision_id", "STRING"),
          _FakeField("confidence", "FLOAT64"),
      ],
      "p.d.outcomes": [
          _FakeField("outcome_id", "STRING"),
      ],
      "p.d.candidate_edges": [
          _FakeField("decision_id", "STRING"),
          _FakeField("outcome_id", "STRING"),
          _FakeField("weight", "FLOAT64"),
      ],
  }


# ------------------------------------------------------------------ #
# Default-mode regression: SDK-created tables validate clean          #
# ------------------------------------------------------------------ #


class TestSdkCreatedTablesRegression:

  def test_default_mode_ok_against_sdk_created_tables(self):
    """Tables matching ``OntologyMaterializer.create_tables()`` output
    must validate clean by default — every key column is NULLABLE
    (the SDK doesn't emit NOT NULL), so a default-mode hard failure
    on KEY_COLUMN_NULLABLE would reject SDK-created tables. Catches
    the "validator rejects SDK-created tables" trap.

    Default mode may still emit advisory ``KEY_COLUMN_NULLABLE``
    warnings for those NULLABLE keys (visible to a CI gate that
    chooses to surface them), but ``report.ok`` must stay True."""
    from bigquery_agent_analytics.binding_validation import FailureCode
    from bigquery_agent_analytics.binding_validation import validate_binding_against_bigquery

    ontology, binding = _ontology_and_binding()
    client = _FakeBQClient(_good_schemas())

    report = validate_binding_against_bigquery(
        ontology=ontology, binding=binding, bq_client=client
    )

    assert report.ok is True
    assert report.failures == ()
    # Warnings are expected (NULLABLE keys), but they must not flip
    # `ok` to False — that's the whole point of the strict-only
    # classification.
    for w in report.warnings:
      assert w.code == FailureCode.KEY_COLUMN_NULLABLE

  def test_expected_types_match_materializer_ddl_type_map(self):
    """Stronger regression: expected types come directly from the
    materializer's `_DDL_TYPE_MAP`, not from a hand-written fixture.
    If a future change updates `_DDL_TYPE_MAP` (e.g. adds NUMERIC
    support), this test forces a corresponding update to
    `_COMPATIBLE_BQ_TYPES` in binding_validation, otherwise the
    default-mode regression would silently start failing."""
    from bigquery_agent_analytics.binding_validation import validate_binding_against_bigquery
    from bigquery_agent_analytics.ontology_materializer import _DDL_TYPE_MAP

    ontology, binding = _ontology_and_binding()
    # Build schemas using the materializer's own map so the test
    # mirrors what the SDK would emit. confidence: double, all keys:
    # string. If _DDL_TYPE_MAP changes type names, this construction
    # automatically picks them up.
    schemas = {
        "p.d.decision_points": [
            _FakeField("decision_id", _DDL_TYPE_MAP["string"]),
            _FakeField("confidence", _DDL_TYPE_MAP["double"]),
        ],
        "p.d.outcomes": [
            _FakeField("outcome_id", _DDL_TYPE_MAP["string"]),
        ],
        "p.d.candidate_edges": [
            _FakeField("decision_id", _DDL_TYPE_MAP["string"]),
            _FakeField("outcome_id", _DDL_TYPE_MAP["string"]),
            _FakeField("weight", _DDL_TYPE_MAP["double"]),
        ],
    }
    client = _FakeBQClient(schemas)

    report = validate_binding_against_bigquery(
        ontology=ontology, binding=binding, bq_client=client
    )

    assert report.ok is True, (
        f"Validator rejected materializer-DDL-derived schemas: "
        f"{[(f.code, f.detail) for f in report.failures]}"
    )

  def test_strict_mode_emits_warnings_as_failures(self):
    """The same SDK-created tables, run under strict=True, must
    surface NULLABLE key columns as KEY_COLUMN_NULLABLE failures."""
    from bigquery_agent_analytics.binding_validation import FailureCode
    from bigquery_agent_analytics.binding_validation import validate_binding_against_bigquery

    ontology, binding = _ontology_and_binding()
    client = _FakeBQClient(_good_schemas())

    report = validate_binding_against_bigquery(
        ontology=ontology,
        binding=binding,
        bq_client=client,
        strict=True,
    )

    assert report.ok is False
    nullable_codes = [
        f.code
        for f in report.failures
        if f.code == FailureCode.KEY_COLUMN_NULLABLE
    ]
    # Decision.decision_id, Outcome.outcome_id (entity primary keys)
    # plus HasOutcome.from_columns[0]=decision_id and
    # HasOutcome.to_columns[0]=outcome_id (relationship endpoints).
    assert len(nullable_codes) == 4


# ------------------------------------------------------------------ #
# Per-failure-code unit tests                                          #
# ------------------------------------------------------------------ #


class TestMissingTable:

  def test_entity_table_missing(self):
    from bigquery_agent_analytics.binding_validation import FailureCode
    from bigquery_agent_analytics.binding_validation import validate_binding_against_bigquery

    ontology, binding = _ontology_and_binding()
    schemas = _good_schemas()
    del schemas["p.d.decision_points"]
    client = _FakeBQClient(schemas)

    report = validate_binding_against_bigquery(
        ontology=ontology, binding=binding, bq_client=client
    )

    decision_failures = [
        f for f in report.failures if f.binding_element == "Decision"
    ]
    assert any(f.code == FailureCode.MISSING_TABLE for f in decision_failures)
    miss = next(
        f for f in decision_failures if f.code == FailureCode.MISSING_TABLE
    )
    assert miss.bq_ref == "p.d.decision_points"
    assert miss.binding_path == "binding.entities[0].source"


class TestMissingColumn:

  def test_property_column_missing(self):
    from bigquery_agent_analytics.binding_validation import FailureCode
    from bigquery_agent_analytics.binding_validation import validate_binding_against_bigquery

    ontology, binding = _ontology_and_binding()
    schemas = _good_schemas()
    # Drop the 'confidence' column from decision_points.
    schemas["p.d.decision_points"] = [
        _FakeField("decision_id", "STRING"),
    ]
    client = _FakeBQClient(schemas)

    report = validate_binding_against_bigquery(
        ontology=ontology, binding=binding, bq_client=client
    )

    miss = [f for f in report.failures if f.code == FailureCode.MISSING_COLUMN]
    assert len(miss) == 1
    assert miss[0].binding_element == "Decision"
    assert miss[0].bq_ref == "p.d.decision_points.confidence"
    assert "properties[1].column" in miss[0].binding_path


class TestTypeMismatch:

  def test_entity_property_type_mismatch(self):
    from bigquery_agent_analytics.binding_validation import FailureCode
    from bigquery_agent_analytics.binding_validation import validate_binding_against_bigquery

    ontology, binding = _ontology_and_binding()
    schemas = _good_schemas()
    # confidence is supposed to be FLOAT64 (sdk_type=double).
    schemas["p.d.decision_points"] = [
        _FakeField("decision_id", "STRING"),
        _FakeField("confidence", "INT64"),  # wrong type
    ]
    client = _FakeBQClient(schemas)

    report = validate_binding_against_bigquery(
        ontology=ontology, binding=binding, bq_client=client
    )

    mismatches = [
        f for f in report.failures if f.code == FailureCode.TYPE_MISMATCH
    ]
    assert len(mismatches) == 1
    assert mismatches[0].expected == "FLOAT64"
    assert mismatches[0].observed == "INT64"

  def test_legacy_bq_type_aliases_accepted(self):
    """BigQuery returns 'INTEGER' / 'FLOAT' / 'BOOLEAN' for older
    schemas; the validator must treat them as compatible with INT64
    / FLOAT64 / BOOL respectively."""
    from bigquery_agent_analytics.binding_validation import validate_binding_against_bigquery

    ontology, binding = _ontology_and_binding()
    schemas = _good_schemas()
    # confidence is double → FLOAT64 expected; legacy BQ returns FLOAT.
    schemas["p.d.decision_points"][1] = _FakeField("confidence", "FLOAT")
    client = _FakeBQClient(schemas)

    report = validate_binding_against_bigquery(
        ontology=ontology, binding=binding, bq_client=client
    )

    assert report.ok is True


class TestEndpointTypeMismatch:

  def test_edge_endpoint_type_does_not_match_referenced_entity_key(
      self,
  ):
    """Edge endpoint type disagrees with the referenced node's
    ontology key type. Two ENDPOINT_TYPE_MISMATCH entries are
    expected — one from the spec-level (1) check (edge BQ type vs
    ontology-derived expected SDK type) and one from the physical
    cross-table (2) check (edge BQ type vs node table's actual key
    BQ type). Both are correct and complementary; users see both
    descriptions and can act on either."""
    from bigquery_agent_analytics.binding_validation import FailureCode
    from bigquery_agent_analytics.binding_validation import validate_binding_against_bigquery

    ontology, binding = _ontology_and_binding()
    schemas = _good_schemas()
    # Decision.decision_id is STRING; if the edge's decision_id
    # column is INT64, that's an endpoint-type mismatch.
    schemas["p.d.candidate_edges"] = [
        _FakeField("decision_id", "INT64"),
        _FakeField("outcome_id", "STRING"),
        _FakeField("weight", "FLOAT64"),
    ]
    client = _FakeBQClient(schemas)

    report = validate_binding_against_bigquery(
        ontology=ontology, binding=binding, bq_client=client
    )

    mismatches = [
        f
        for f in report.failures
        if f.code == FailureCode.ENDPOINT_TYPE_MISMATCH
    ]
    # Two complementary mismatches expected: spec-level + physical.
    assert len(mismatches) == 2
    assert all(m.binding_element == "HasOutcome" for m in mismatches)
    assert all("from_columns[0]" in m.binding_path for m in mismatches)
    assert all(m.observed == "INT64" for m in mismatches)

    spec_level = [
        m for m in mismatches if "physical cross-table" not in m.detail
    ]
    physical = [m for m in mismatches if "physical cross-table" in m.detail]
    assert len(spec_level) == 1
    assert len(physical) == 1
    assert spec_level[0].expected == "STRING"  # ontology-derived DDL type
    assert physical[0].expected == "STRING"  # actual node BQ field type


class TestEndpointPhysicalCrossTableCheck:

  def test_edge_endpoint_disagrees_with_node_actual_field_type(self):
    """When the node table has drifted away from its ontology
    declaration, the per-entity loop flags a TYPE_MISMATCH on the
    node. But the edge endpoint may also disagree with the node's
    *actual* storage type — in which case the join would fail at
    query time. The validator must surface ENDPOINT_TYPE_MISMATCH
    for that edge ↔ node disagreement, not just the spec-level one.

    Setup: node's decision_id column is INT64 in BQ (drifted from
    the ontology's STRING declaration). Edge's decision_id column
    is STRING (matching ontology, but disagreeing with the node's
    actual storage). The edge would fail to join the node.
    """
    from bigquery_agent_analytics.binding_validation import FailureCode
    from bigquery_agent_analytics.binding_validation import validate_binding_against_bigquery

    ontology, binding = _ontology_and_binding()
    schemas = _good_schemas()
    # Node decision_id has drifted to INT64.
    schemas["p.d.decision_points"] = [
        _FakeField("decision_id", "INT64"),
        _FakeField("confidence", "FLOAT64"),
    ]
    # Edge decision_id is still STRING (ontology says so).
    # candidate_edges already has decision_id as STRING in
    # _good_schemas(), so no change needed there.
    client = _FakeBQClient(schemas)

    report = validate_binding_against_bigquery(
        ontology=ontology, binding=binding, bq_client=client
    )

    # Per-entity loop catches the node's drift.
    node_mismatches = [
        f
        for f in report.failures
        if f.code == FailureCode.TYPE_MISMATCH
        and f.binding_element == "Decision"
    ]
    assert len(node_mismatches) == 1

    # Per-relationship loop catches the physical cross-table edge ↔
    # node disagreement (edge=STRING, node=INT64). The detail must
    # call out that this is a physical-table mismatch so users can
    # tell it apart from the spec-level (1) check.
    edge_mismatches = [
        f
        for f in report.failures
        if f.code == FailureCode.ENDPOINT_TYPE_MISMATCH
        and f.binding_element == "HasOutcome"
    ]
    assert len(edge_mismatches) >= 1
    assert any(
        "physical cross-table mismatch" in m.detail for m in edge_mismatches
    ), (
        "Expected at least one ENDPOINT_TYPE_MISMATCH detail to call "
        "out the physical cross-table mismatch; got: "
        f"{[m.detail for m in edge_mismatches]}"
    )


class TestUnexpectedRepeatedMode:

  def test_repeated_mode_on_property_column(self):
    from bigquery_agent_analytics.binding_validation import FailureCode
    from bigquery_agent_analytics.binding_validation import validate_binding_against_bigquery

    ontology, binding = _ontology_and_binding()
    schemas = _good_schemas()
    schemas["p.d.decision_points"] = [
        _FakeField("decision_id", "STRING"),
        _FakeField("confidence", "FLOAT64", mode="REPEATED"),
    ]
    client = _FakeBQClient(schemas)

    report = validate_binding_against_bigquery(
        ontology=ontology, binding=binding, bq_client=client
    )

    repeated = [
        f
        for f in report.failures
        if f.code == FailureCode.UNEXPECTED_REPEATED_MODE
    ]
    assert len(repeated) == 1
    assert repeated[0].observed == "REPEATED"


class TestMissingDataset:

  def test_missing_dataset_classified(self):
    from bigquery_agent_analytics.binding_validation import FailureCode
    from bigquery_agent_analytics.binding_validation import validate_binding_against_bigquery

    ontology, binding = _ontology_and_binding()
    client = _FakeBQClient(_good_schemas(), missing_datasets={"p.d"})

    report = validate_binding_against_bigquery(
        ontology=ontology, binding=binding, bq_client=client
    )

    assert any(f.code == FailureCode.MISSING_DATASET for f in report.failures)


class TestInsufficientPermissions:

  def test_forbidden_table_classified(self):
    from bigquery_agent_analytics.binding_validation import FailureCode
    from bigquery_agent_analytics.binding_validation import validate_binding_against_bigquery

    ontology, binding = _ontology_and_binding()
    client = _FakeBQClient(_good_schemas(), forbidden={"p.d.decision_points"})

    report = validate_binding_against_bigquery(
        ontology=ontology, binding=binding, bq_client=client
    )

    perm = [
        f
        for f in report.failures
        if f.code == FailureCode.INSUFFICIENT_PERMISSIONS
    ]
    assert len(perm) >= 1
    assert perm[0].bq_ref == "p.d.decision_points"


class TestKeyColumnNullable:

  def test_default_mode_emits_warnings(self):
    from bigquery_agent_analytics.binding_validation import FailureCode
    from bigquery_agent_analytics.binding_validation import validate_binding_against_bigquery

    ontology, binding = _ontology_and_binding()
    client = _FakeBQClient(_good_schemas())

    report = validate_binding_against_bigquery(
        ontology=ontology,
        binding=binding,
        bq_client=client,
        strict=False,
    )

    # Warnings are present (NULLABLE keys), but report.ok is True.
    assert report.ok is True
    assert all(
        w.code == FailureCode.KEY_COLUMN_NULLABLE for w in report.warnings
    )
    assert len(report.warnings) >= 2  # 2 entity keys, 2 endpoint keys

  def test_strict_mode_promotes_to_failures(self):
    from bigquery_agent_analytics.binding_validation import FailureCode
    from bigquery_agent_analytics.binding_validation import validate_binding_against_bigquery

    ontology, binding = _ontology_and_binding()
    client = _FakeBQClient(_good_schemas())

    report = validate_binding_against_bigquery(
        ontology=ontology,
        binding=binding,
        bq_client=client,
        strict=True,
    )

    assert report.ok is False
    assert all(
        f.code == FailureCode.KEY_COLUMN_NULLABLE for f in report.failures
    )
    # No warnings — they got escalated.
    assert report.warnings == ()

  def test_required_keys_clean_in_strict_mode(self):
    """When the user's tables already use REQUIRED key columns,
    strict mode reports clean."""
    from bigquery_agent_analytics.binding_validation import validate_binding_against_bigquery

    ontology, binding = _ontology_and_binding()
    schemas = _good_schemas()
    # Mark all key + endpoint columns as REQUIRED.
    for col in schemas["p.d.decision_points"]:
      if col.name == "decision_id":
        col.mode = "REQUIRED"
    for col in schemas["p.d.outcomes"]:
      if col.name == "outcome_id":
        col.mode = "REQUIRED"
    for col in schemas["p.d.candidate_edges"]:
      if col.name in ("decision_id", "outcome_id"):
        col.mode = "REQUIRED"

    client = _FakeBQClient(schemas)
    report = validate_binding_against_bigquery(
        ontology=ontology,
        binding=binding,
        bq_client=client,
        strict=True,
    )

    assert report.ok is True
    assert report.failures == ()
    assert report.warnings == ()


# ------------------------------------------------------------------ #
# Cross-cutting: cross-project source                                  #
# ------------------------------------------------------------------ #


class TestBindingPathYamlOrder:

  def test_path_index_uses_binding_yaml_order_not_resolved_order(self):
    """A binding YAML that lists properties in a different order
    than the ontology must produce paths using the binding's index,
    not the ResolvedEntity's. Otherwise tooling pointed at
    ``binding.entities[0].properties[1].column`` would land on the
    wrong YAML entry.

    Setup: ontology declares (decision_id, confidence). Binding
    YAML lists them in reverse order (confidence, decision_id).
    The bound 'confidence' column is missing on the BQ table. The
    failure path must be ``properties[0]`` (the binding's index for
    confidence), not ``properties[1]`` (the resolved-side index).
    """
    import pathlib
    import tempfile

    from bigquery_agent_analytics.binding_validation import FailureCode
    from bigquery_agent_analytics.binding_validation import validate_binding_against_bigquery
    from bigquery_ontology import load_binding
    from bigquery_ontology import load_ontology

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="bind_validate_order_"))
    (tmp / "ont.yaml").write_text(
        "ontology: TestGraph\n"
        "entities:\n"
        "  - name: Decision\n"
        "    keys:\n"
        "      primary: [decision_id]\n"
        "    properties:\n"
        "      - name: decision_id\n"
        "        type: string\n"
        "      - name: confidence\n"
        "        type: double\n"
        "relationships: []\n",
        encoding="utf-8",
    )
    # Binding lists confidence FIRST, decision_id SECOND.
    (tmp / "bnd.yaml").write_text(
        "binding: test_bind\n"
        "ontology: TestGraph\n"
        "target:\n"
        "  backend: bigquery\n"
        "  project: p\n"
        "  dataset: d\n"
        "entities:\n"
        "  - name: Decision\n"
        "    source: decision_points\n"
        "    properties:\n"
        "      - name: confidence\n"
        "        column: confidence\n"
        "      - name: decision_id\n"
        "        column: decision_id\n"
        "relationships: []\n",
        encoding="utf-8",
    )

    ontology = load_ontology(str(tmp / "ont.yaml"))
    binding = load_binding(str(tmp / "bnd.yaml"), ontology=ontology)

    # Drop confidence — its bound column is missing on BQ.
    schemas = {
        "p.d.decision_points": [_FakeField("decision_id", "STRING")],
    }
    client = _FakeBQClient(schemas)

    report = validate_binding_against_bigquery(
        ontology=ontology, binding=binding, bq_client=client
    )

    miss = [f for f in report.failures if f.code == FailureCode.MISSING_COLUMN]
    assert len(miss) == 1
    # The failure path index must be 0 (binding YAML's index for
    # 'confidence'), not 1 (the resolved-side index — Decision's
    # ontology order is (decision_id, confidence)).
    assert miss[0].binding_path == (
        "binding.entities[0].properties[0].column"
    ), (
        f"Path {miss[0].binding_path!r} should reflect binding YAML "
        f"order (confidence is properties[0]), not resolved-side "
        f"order (where confidence is properties[1])."
    )


class TestCrossProjectSource:

  def test_fully_qualified_entity_source_validated_against_its_project(
      self,
  ):
    """A binding whose entity.source is fully qualified to a project
    different from binding.target.project must be validated against
    the entity's project, not the target's. Catches the trap from
    #105's "validate resolved sources, not only target.project /
    target.dataset" finding."""
    import pathlib
    import tempfile

    from bigquery_agent_analytics.binding_validation import FailureCode
    from bigquery_agent_analytics.binding_validation import validate_binding_against_bigquery
    # Build a binding where Decision.source is fully qualified to a
    # different project. The fixture builder doesn't support that
    # directly, so write one inline.
    from bigquery_ontology import load_binding
    from bigquery_ontology import load_ontology

    tmp = pathlib.Path(tempfile.mkdtemp(prefix="bind_validate_xp_"))
    (tmp / "ont.yaml").write_text(
        "ontology: TestGraph\n"
        "entities:\n"
        "  - name: Decision\n"
        "    keys:\n"
        "      primary: [decision_id]\n"
        "    properties:\n"
        "      - name: decision_id\n"
        "        type: string\n"
        "relationships: []\n",
        encoding="utf-8",
    )
    (tmp / "bnd.yaml").write_text(
        "binding: test_bind\n"
        "ontology: TestGraph\n"
        "target:\n"
        "  backend: bigquery\n"
        "  project: target-project\n"
        "  dataset: target-dataset\n"
        "entities:\n"
        "  - name: Decision\n"
        # Fully qualified to a different project.
        "    source: source-project.source-dataset.decisions\n"
        "    properties:\n"
        "      - name: decision_id\n"
        "        column: decision_id\n"
        "relationships: []\n",
        encoding="utf-8",
    )

    ontology = load_ontology(str(tmp / "ont.yaml"))
    binding = load_binding(str(tmp / "bnd.yaml"), ontology=ontology)

    # Validator should look for the table at source-project, not
    # target-project. Place the table at the correct location and
    # leave target-project empty.
    schemas = {
        "source-project.source-dataset.decisions": [
            _FakeField("decision_id", "STRING"),
        ],
    }
    client = _FakeBQClient(schemas)

    report = validate_binding_against_bigquery(
        ontology=ontology, binding=binding, bq_client=client
    )

    # Should be clean. Any MISSING_TABLE failure would mean the
    # validator looked in the wrong project.
    assert all(f.code != FailureCode.MISSING_TABLE for f in report.failures), (
        "validator looked in target project instead of fully-qualified "
        "entity.source project: "
        f"{[(f.code, f.bq_ref) for f in report.failures]}"
    )


# ------------------------------------------------------------------ #
# Report shape                                                         #
# ------------------------------------------------------------------ #


class TestReportShape:

  def test_failure_carries_required_fields(self):
    from bigquery_agent_analytics.binding_validation import validate_binding_against_bigquery

    ontology, binding = _ontology_and_binding()
    schemas = _good_schemas()
    schemas["p.d.decision_points"] = [
        _FakeField("decision_id", "STRING"),
    ]
    client = _FakeBQClient(schemas)

    report = validate_binding_against_bigquery(
        ontology=ontology, binding=binding, bq_client=client
    )

    f = report.failures[0]
    assert f.code is not None
    assert f.binding_element == "Decision"
    assert f.binding_path.startswith("binding.entities[")
    assert f.bq_ref.startswith("p.d.")
    assert isinstance(f.detail, str) and f.detail

  def test_ok_property_is_failures_empty(self):
    from bigquery_agent_analytics.binding_validation import BindingValidationReport

    empty = BindingValidationReport()
    assert empty.ok is True

    from bigquery_agent_analytics.binding_validation import BindingValidationFailure
    from bigquery_agent_analytics.binding_validation import FailureCode

    not_ok = BindingValidationReport(
        failures=(
            BindingValidationFailure(
                code=FailureCode.MISSING_TABLE,
                binding_element="X",
                binding_path="binding.entities[0].source",
                bq_ref="p.d.x",
            ),
        ),
    )
    assert not_ok.ok is False
