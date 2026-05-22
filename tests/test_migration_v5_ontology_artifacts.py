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

"""Tests for the migration v5 ontology-agnostic artifact pipeline.

Covers two things:

1. **MAKO regression**: the refactor that pulled the generic
   pipeline out of ``mako_artifacts.py`` produces
   byte-identical output for the MAKO config. The checked-in
   snapshot files (``ontology.yaml`` / ``binding.yaml`` /
   ``table_ddl.sql`` / ``property_graph.sql``) must match
   what ``regenerate_snapshots(MAKO_CONFIG, ...)`` produces.

2. **Pluggability**: the same generic pipeline accepts a
   different OntologyConfig (the Simple Request Flow smoke
   fixture) and produces a valid binding + DDL + property
   graph with the right shape (3 entities, 2 relationships,
   per-entity snake_case PK columns).
"""

from __future__ import annotations

import dataclasses
import importlib
import pathlib
import sys

import pytest
import yaml

# The ontology-artifact pipeline pulls
# ``bigquery_ontology.owl_importer``, which requires
# ``rdflib`` (an optional dep behind the ``[owl]`` extra).
# CI's default install is ``.[dev]`` only, so skip the whole
# module when rdflib isn't present — mirrors the pattern in
# ``tests/test_owl_import_bridge.py`` and
# ``tests/bigquery_ontology/test_owl_importer.py``.
pytest.importorskip("rdflib")

# ``mako_artifacts`` and ``ontology_artifacts`` are sibling
# modules inside ``examples/migration_v5/`` that import each
# other top-level (the notebook + run_agent.py do the same).
# This test uses the same sibling-style import surface those
# callers do — only ``examples/migration_v5/`` needs to be on
# sys.path. Don't mix package-style (``examples.migration_v5.X``)
# imports here: that path requires both the repo root AND the
# v5 dir on sys.path, and silently masks regressions in the
# notebook's import contract (see PR #172 review).
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_V5_DIR = _REPO_ROOT / "examples" / "migration_v5"
if str(_V5_DIR) not in sys.path:
  sys.path.insert(0, str(_V5_DIR))

ontology_artifacts = importlib.import_module("ontology_artifacts")
mako_artifacts = importlib.import_module("mako_artifacts")
simple_config_mod = importlib.import_module(
    "example_ontologies.simple_request_flow_config"
)

from bigquery_ontology import load_binding_from_string
from bigquery_ontology import load_ontology_from_string

# ------------------------------------------------------------------ #
# Section 1: MAKO refactor preserves byte-identical snapshots         #
# ------------------------------------------------------------------ #


_MAKO_CHECKED_IN_PROJECT = "test-project-0728-467323"
_MAKO_CHECKED_IN_DATASET = "migration_v5_demo"


def _temp_mako_config(tmp_path: pathlib.Path):
  return dataclasses.replace(mako_artifacts.MAKO_CONFIG, snapshot_dir=tmp_path)


def test_mako_regenerate_matches_checked_in_snapshots(tmp_path):
  """The refactor must not drift the MAKO snapshot output.

  Regenerates against a tmpdir using the same
  ``(project, dataset)`` the checked-in snapshots use, then
  diffs every file byte-for-byte against the checked-in
  version. Catches accidental whitespace / ordering /
  annotation-key changes.
  """
  tmp_config = _temp_mako_config(tmp_path)
  ontology_artifacts.regenerate_snapshots(
      tmp_config,
      project=_MAKO_CHECKED_IN_PROJECT,
      dataset=_MAKO_CHECKED_IN_DATASET,
  )

  for filename in (
      "ontology.yaml",
      "binding.yaml",
      "table_ddl.sql",
      "property_graph.sql",
  ):
    checked_in = (_V5_DIR / filename).read_text(encoding="utf-8")
    regenerated = (tmp_path / filename).read_text(encoding="utf-8")
    assert regenerated == checked_in, (
        f"{filename} drifted after refactor — first 200 chars:\n"
        f"checked-in: {checked_in[:200]!r}\n"
        f"regenerated: {regenerated[:200]!r}"
    )


def test_mako_shim_regenerate_still_works():
  """Back-compat: ``mako_artifacts.regenerate_snapshots()`` is
  the notebook's entry point. It must keep returning the same
  summary shape after the refactor."""
  ontology, _yaml = mako_artifacts.load_mako_ontology()
  binding = mako_artifacts.make_binding(
      ontology, project="any-proj", dataset="any_ds"
  )
  assert len(ontology.entities) == 18
  # Beat 5 expansion: the demo binding now covers 11 of the 18
  # MAKO entities — the six-entity Beat 1–4 hub plus five
  # feedback/reward loop entities (BusinessConstraint,
  # ConstraintApplication, RejectionReason, OutcomeSignal,
  # RewardComputation).
  assert len(binding.entities) == 11
  # Twelve heterogeneous + two ``DecisionExecution`` self-edges
  # (``evolvedFrom``, ``supersededBy``). The Beat 5 entities add
  # five new edges (appliedConstraint, derivedReward,
  # filteredByConstraint, hasRejectionReason, producedOutcome) on
  # top of the seven Beat 1–4 edges.
  assert len(binding.relationships) == 14
  rel_by_name = {r.name: r for r in binding.relationships}
  for self_edge in ("evolvedFrom", "supersededBy"):
    rb = rel_by_name[self_edge]
    assert rb.from_columns == [{"src_decision_execution_id": "id"}]
    assert rb.to_columns == [{"dst_decision_execution_id": "id"}]
  # Beat 5 edges present in the regenerated binding.
  for beat5_edge in (
      "appliedConstraint",
      "derivedReward",
      "filteredByConstraint",
      "hasRejectionReason",
      "producedOutcome",
  ):
    assert (
        beat5_edge in rel_by_name
    ), f"Beat 5 edge {beat5_edge!r} missing from regenerated binding"


# ------------------------------------------------------------------ #
# Section 2: Pluggable pipeline accepts a different ontology config   #
# ------------------------------------------------------------------ #


def test_simple_request_flow_pipeline_produces_valid_binding(tmp_path):
  """The generic pipeline accepts any OntologyConfig. Smoke
  test against the Simple Request Flow ontology: 3 entities,
  2 relationships, no inheritance, no cross-namespace refs.
  Validates the binding loads through the SDK's binding
  loader."""
  tmp_config = dataclasses.replace(
      simple_config_mod.SIMPLE_REQUEST_FLOW_CONFIG, snapshot_dir=tmp_path
  )
  summary = ontology_artifacts.regenerate_snapshots(
      tmp_config, project="smoke-proj", dataset="smoke_ds"
  )

  assert summary == {
      "ontology_entities": 3,
      "binding_entities": 3,
      "binding_relationships": 2,
  }

  ontology_yaml = (tmp_path / "ontology.yaml").read_text(encoding="utf-8")
  binding_yaml = (tmp_path / "binding.yaml").read_text(encoding="utf-8")
  ontology = load_ontology_from_string(ontology_yaml)
  binding = load_binding_from_string(binding_yaml, ontology=ontology)

  entity_names = sorted(e.name for e in binding.entities)
  assert entity_names == ["Action", "Outcome", "Request"]
  rel_names = sorted(r.name for r in binding.relationships)
  assert rel_names == ["hasAction", "producesOutcome"]


def test_simple_request_flow_pk_columns_are_per_entity(tmp_path):
  """The pipeline names each entity's PK column
  ``{entity_short}_id`` (not bare ``id``) so edge tables get
  clean ``{src}_id, {dst}_id`` shapes without column-name
  collisions. Cover the simple ontology to make sure the
  convention isn't accidentally MAKO-specific.

  PK property names are entity-declared (``requestId`` etc.
  via ``owl:hasKey``) — they're the FIRST entry in each
  binding entity's property list (``make_binding`` inserts the
  PK at position 0)."""
  tmp_config = dataclasses.replace(
      simple_config_mod.SIMPLE_REQUEST_FLOW_CONFIG, snapshot_dir=tmp_path
  )
  ontology_artifacts.regenerate_snapshots(
      tmp_config, project="smoke-proj", dataset="smoke_ds"
  )
  binding_yaml = (tmp_path / "binding.yaml").read_text(encoding="utf-8")
  parsed = yaml.safe_load(binding_yaml)

  pk_column_by_entity = {
      ent["name"]: ent["properties"][0]["column"] for ent in parsed["entities"]
  }
  assert pk_column_by_entity == {
      "Action": "action_id",
      "Outcome": "outcome_id",
      "Request": "request_id",
  }


def test_simple_request_flow_property_graph_sql_uses_configured_graph_name(
    tmp_path,
):
  """``graph_name`` is per-config. Verify the simple
  ontology's snapshot uses its own graph name, not MAKO's."""
  tmp_config = dataclasses.replace(
      simple_config_mod.SIMPLE_REQUEST_FLOW_CONFIG, snapshot_dir=tmp_path
  )
  ontology_artifacts.regenerate_snapshots(
      tmp_config, project="smoke-proj", dataset="smoke_ds"
  )
  pg_sql = (tmp_path / "property_graph.sql").read_text(encoding="utf-8")
  assert "simple_request_flow_graph" in pg_sql
  assert "mako_demo_graph" not in pg_sql


def test_simple_request_flow_binds_declared_owl_haskey_property(tmp_path):
  """Regression for PR #172 review (P1): the generic pipeline
  must bind the entity's declared primary-key property, not a
  hard-coded ``id``.

  The Simple Request Flow TTL declares
  ``owl:hasKey ( rf:requestId )`` (and similar for Action /
  Outcome), so each entity's PK property name is
  ``requestId`` / ``actionId`` / ``outcomeId`` — NOT the
  synthesized ``id`` MAKO's FILL_IN path produces. Before the
  fix, ``make_binding`` always emitted
  ``{name: id, column: request_id}``, which made
  ``load_binding_from_string`` raise ``Entity binding
  'Request': property 'id' is not declared on this element``.
  """
  tmp_config = dataclasses.replace(
      simple_config_mod.SIMPLE_REQUEST_FLOW_CONFIG, snapshot_dir=tmp_path
  )
  ontology_artifacts.regenerate_snapshots(
      tmp_config, project="smoke-proj", dataset="smoke_ds"
  )
  binding_yaml = (tmp_path / "binding.yaml").read_text(encoding="utf-8")
  parsed = yaml.safe_load(binding_yaml)

  props_by_entity = {
      ent["name"]: {p["name"] for p in ent["properties"]}
      for ent in parsed["entities"]
  }
  # Declared PK property names show up in the binding.
  assert "requestId" in props_by_entity["Request"]
  assert "actionId" in props_by_entity["Action"]
  assert "outcomeId" in props_by_entity["Outcome"]
  # And the synthesized ``id`` is NOT injected when a real
  # ``owl:hasKey`` is present.
  for entity_name in ("Request", "Action", "Outcome"):
    assert "id" not in props_by_entity[entity_name], (
        f"Entity {entity_name} should not have a synthesized 'id' "
        "property when the TTL declares owl:hasKey."
    )


def test_simple_request_flow_property_graph_resolves_owl_haskey_pk_column(
    tmp_path,
):
  """The property-graph SQL generator must look up each
  entity's PK property by its declared name, not by a
  hard-coded ``id``. Verifies the second half of the PR #172
  P1 fix — ``KEY (...)`` and ``REFERENCES ... (...)`` resolve
  to the right column for an ``owl:hasKey`` ontology."""
  tmp_config = dataclasses.replace(
      simple_config_mod.SIMPLE_REQUEST_FLOW_CONFIG, snapshot_dir=tmp_path
  )
  ontology_artifacts.regenerate_snapshots(
      tmp_config, project="smoke-proj", dataset="smoke_ds"
  )
  pg_sql = (tmp_path / "property_graph.sql").read_text(encoding="utf-8")
  # Per-entity PK columns appear as node-table KEYs.
  assert "KEY (request_id)" in pg_sql
  assert "KEY (action_id)" in pg_sql
  assert "KEY (outcome_id)" in pg_sql
  # Edge endpoints reference the per-entity PK column.
  assert "REFERENCES request (request_id)" in pg_sql
  assert "REFERENCES action (action_id)" in pg_sql
  assert "REFERENCES outcome (outcome_id)" in pg_sql


def test_simple_request_flow_uses_its_own_annotation_prefix():
  """Annotation prefix is per-config. The simple ontology has
  no cross-namespace refs and no inheritance to strip, so the
  pipeline writes no audit annotations — but the prefix
  threading itself is observable through the inheritance code
  path. We only assert the prefix doesn't leak MAKO's name."""
  ontology, yaml_text = ontology_artifacts.load_ontology(
      simple_config_mod.SIMPLE_REQUEST_FLOW_CONFIG
  )
  assert "mako_demo:" not in yaml_text


# ------------------------------------------------------------------ #
# Section 3: OntologyConfig surface                                    #
# ------------------------------------------------------------------ #


def test_ontology_config_path_properties_derive_from_snapshot_dir(tmp_path):
  """The four snapshot path properties (``ontology_path``,
  ``binding_path``, ``table_ddl_path``, ``property_graph_path``)
  derive from ``snapshot_dir``. Caller-facing surface, worth
  asserting explicitly."""
  cfg = ontology_artifacts.OntologyConfig(
      ttl_path=tmp_path / "fake.ttl",
      include_namespace="https://example.com/x/",
      entity_allowlist=("X",),
      annotation_prefix="x_demo",
      graph_name="x_graph",
      snapshot_dir=tmp_path,
  )
  assert cfg.ontology_path == tmp_path / "ontology.yaml"
  assert cfg.binding_path == tmp_path / "binding.yaml"
  assert cfg.table_ddl_path == tmp_path / "table_ddl.sql"
  assert cfg.property_graph_path == tmp_path / "property_graph.sql"


def test_ontology_config_is_frozen():
  """Frozen so callers can't accidentally mutate a shared
  config and surprise other call sites."""
  with pytest.raises(dataclasses.FrozenInstanceError):
    mako_artifacts.MAKO_CONFIG.annotation_prefix = "other"  # type: ignore[misc]
