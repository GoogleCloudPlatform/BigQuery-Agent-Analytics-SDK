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

"""Tests for the RuntimeSpec adapter (ontology package bridge)."""

from __future__ import annotations

import os

import pytest

from bigquery_agent_analytics.ontology_models import GraphSpec
from bigquery_agent_analytics.ontology_models import load_graph_spec
from bigquery_agent_analytics.runtime_spec import graph_spec_from_ontology_binding
from bigquery_agent_analytics.runtime_spec import graph_spec_to_ontology_binding
from bigquery_agent_analytics.runtime_spec import LineageEdgeConfig
from bigquery_ontology import BigQueryTarget
from bigquery_ontology import Binding
from bigquery_ontology import Entity
from bigquery_ontology import EntityBinding
from bigquery_ontology import Keys
from bigquery_ontology import Ontology
from bigquery_ontology import Property
from bigquery_ontology import PropertyBinding
from bigquery_ontology import PropertyType
from bigquery_ontology import Relationship
from bigquery_ontology import RelationshipBinding

_DEMO_SPEC_PATH = os.path.join(
    os.path.dirname(__file__), "..", "examples", "ymgo_graph_spec.yaml"
)


# ------------------------------------------------------------------ #
# graph_spec_to_ontology_binding (reverse)                             #
# ------------------------------------------------------------------ #


class TestGraphSpecToOntologyBinding:
  """Convert SDK GraphSpec → upstream Ontology + Binding."""

  def test_round_trip_preserves_entity_names(self):
    spec = load_graph_spec(_DEMO_SPEC_PATH, env="p.d")
    ontology, binding = graph_spec_to_ontology_binding(
        spec, project_id="p", dataset_id="d"
    )
    ont_entity_names = {e.name for e in ontology.entities}
    spec_entity_names = {e.name for e in spec.entities}
    assert ont_entity_names == spec_entity_names

  def test_round_trip_preserves_relationship_names(self):
    spec = load_graph_spec(_DEMO_SPEC_PATH, env="p.d")
    ontology, binding = graph_spec_to_ontology_binding(
        spec, project_id="p", dataset_id="d"
    )
    ont_rel_names = {r.name for r in ontology.relationships}
    spec_rel_names = {r.name for r in spec.relationships}
    assert ont_rel_names == spec_rel_names

  def test_binding_has_all_entities(self):
    spec = load_graph_spec(_DEMO_SPEC_PATH, env="p.d")
    _, binding = graph_spec_to_ontology_binding(
        spec, project_id="p", dataset_id="d"
    )
    binding_entity_names = {eb.name for eb in binding.entities}
    spec_entity_names = {e.name for e in spec.entities}
    assert binding_entity_names == spec_entity_names

  def test_binding_target(self):
    spec = load_graph_spec(_DEMO_SPEC_PATH, env="p.d")
    _, binding = graph_spec_to_ontology_binding(
        spec, project_id="my-proj", dataset_id="my-ds"
    )
    assert binding.target.project == "my-proj"
    assert binding.target.dataset == "my-ds"

  def test_property_types_mapped(self):
    spec = load_graph_spec(_DEMO_SPEC_PATH, env="p.d")
    ontology, _ = graph_spec_to_ontology_binding(
        spec, project_id="p", dataset_id="d"
    )
    # All properties should have valid PropertyType values.
    for entity in ontology.entities:
      for prop in entity.properties:
        assert isinstance(prop.type, PropertyType)


# ------------------------------------------------------------------ #
# graph_spec_from_ontology_binding (forward)                           #
# ------------------------------------------------------------------ #


class TestOntologyBindingToGraphSpec:
  """Convert upstream Ontology + Binding → SDK GraphSpec."""

  def _make_simple_ontology(self):
    return Ontology(
        ontology="test",
        entities=[
            Entity(
                name="Customer",
                keys=Keys(primary=["cid"]),
                properties=[
                    Property(name="cid", type=PropertyType.STRING),
                    Property(name="name", type=PropertyType.STRING),
                ],
            ),
        ],
        relationships=[],
    )

  def _make_simple_binding(self):
    return Binding(
        binding="test_binding",
        ontology="test",
        target=BigQueryTarget(
            backend="bigquery",
            project="proj",
            dataset="ds",
        ),
        entities=[
            EntityBinding(
                name="Customer",
                source="customers",
                properties=[
                    PropertyBinding(name="cid", column="customer_id"),
                    PropertyBinding(name="name", column="display_name"),
                ],
            ),
        ],
        relationships=[],
    )

  def test_produces_valid_graph_spec(self):
    ontology = self._make_simple_ontology()
    binding = self._make_simple_binding()
    spec = graph_spec_from_ontology_binding(ontology, binding)
    assert isinstance(spec, GraphSpec)
    assert spec.name == "test"
    assert len(spec.entities) == 1
    assert spec.entities[0].name == "Customer"

  def test_source_fully_qualified(self):
    ontology = self._make_simple_ontology()
    binding = self._make_simple_binding()
    spec = graph_spec_from_ontology_binding(ontology, binding)
    source = spec.entities[0].binding.source
    assert source == "proj.ds.customers"

  def test_keys_preserved(self):
    ontology = self._make_simple_ontology()
    binding = self._make_simple_binding()
    spec = graph_spec_from_ontology_binding(ontology, binding)
    assert spec.entities[0].keys.primary == ["cid"]

  def test_properties_have_correct_types(self):
    ontology = self._make_simple_ontology()
    binding = self._make_simple_binding()
    spec = graph_spec_from_ontology_binding(ontology, binding)
    prop_types = {p.name: p.type for p in spec.entities[0].properties}
    assert prop_types["cid"] == "string"
    assert prop_types["name"] == "string"

  def test_lineage_config_applied(self):
    ontology = Ontology(
        ontology="test",
        entities=[
            Entity(
                name="AdUnit",
                keys=Keys(primary=["ad_id"]),
                properties=[
                    Property(name="ad_id", type=PropertyType.STRING),
                ],
            ),
        ],
        relationships=[
            Relationship(
                name="AdUnitEvolvedFrom",
                **{"from": "AdUnit"},
                to="AdUnit",
                properties=[
                    Property(
                        name="from_session_id",
                        type=PropertyType.STRING,
                    ),
                    Property(
                        name="to_session_id",
                        type=PropertyType.STRING,
                    ),
                ],
            ),
        ],
    )
    binding = Binding(
        binding="test_binding",
        ontology="test",
        target=BigQueryTarget(backend="bigquery", project="p", dataset="d"),
        entities=[
            EntityBinding(
                name="AdUnit",
                source="ad_units",
                properties=[
                    PropertyBinding(name="ad_id", column="ad_id"),
                ],
            ),
        ],
        relationships=[
            RelationshipBinding(
                name="AdUnitEvolvedFrom",
                source="ad_unit_lineage",
                from_columns=["ad_id"],
                to_columns=["ad_id"],
            ),
        ],
    )
    spec = graph_spec_from_ontology_binding(
        ontology,
        binding,
        lineage_config={
            "AdUnitEvolvedFrom": LineageEdgeConfig(
                from_session_column="from_session_id",
                to_session_column="to_session_id",
            ),
        },
    )
    rel = spec.relationships[0]
    assert rel.binding.from_session_column == "from_session_id"
    assert rel.binding.to_session_column == "to_session_id"

  def test_no_lineage_config_defaults_none(self):
    ontology = self._make_simple_ontology()
    binding = self._make_simple_binding()
    spec = graph_spec_from_ontology_binding(ontology, binding)
    # No relationships, but check entity bindings have no session columns.
    for entity in spec.entities:
      assert entity.binding.from_session_column is None
      assert entity.binding.to_session_column is None


# ------------------------------------------------------------------ #
# Round-trip: GraphSpec → Ontology+Binding → GraphSpec                 #
# ------------------------------------------------------------------ #


class TestRoundTrip:
  """Full round-trip preserves runtime-critical fields."""

  def test_round_trip_entity_properties(self):
    spec = load_graph_spec(_DEMO_SPEC_PATH, env="p.d")
    ontology, binding = graph_spec_to_ontology_binding(
        spec, project_id="p", dataset_id="d"
    )
    roundtrip = graph_spec_from_ontology_binding(ontology, binding)
    for orig, rt in zip(spec.entities, roundtrip.entities):
      orig_props = {p.name for p in orig.properties}
      rt_props = {p.name for p in rt.properties}
      assert orig_props == rt_props, f"Entity {orig.name}: properties differ"

  def test_round_trip_relationship_endpoints(self):
    spec = load_graph_spec(_DEMO_SPEC_PATH, env="p.d")
    ontology, binding = graph_spec_to_ontology_binding(
        spec, project_id="p", dataset_id="d"
    )
    roundtrip = graph_spec_from_ontology_binding(ontology, binding)
    for orig, rt in zip(spec.relationships, roundtrip.relationships):
      assert orig.from_entity == rt.from_entity
      assert orig.to_entity == rt.to_entity

  def test_round_trip_keys(self):
    spec = load_graph_spec(_DEMO_SPEC_PATH, env="p.d")
    ontology, binding = graph_spec_to_ontology_binding(
        spec, project_id="p", dataset_id="d"
    )
    roundtrip = graph_spec_from_ontology_binding(ontology, binding)
    for orig, rt in zip(spec.entities, roundtrip.entities):
      assert orig.keys.primary == rt.keys.primary

  def test_upstream_ddl_works_for_flat_spec(self):
    """Upstream compile_graph works on a flat (no extends) spec."""
    from bigquery_ontology import compile_graph

    ontology = Ontology(
        ontology="flat_test",
        entities=[
            Entity(
                name="A",
                keys=Keys(primary=["a_id"]),
                properties=[
                    Property(name="a_id", type=PropertyType.STRING),
                    Property(name="val", type=PropertyType.STRING),
                ],
            ),
            Entity(
                name="B",
                keys=Keys(primary=["b_id"]),
                properties=[
                    Property(name="b_id", type=PropertyType.STRING),
                ],
            ),
        ],
        relationships=[
            Relationship(
                name="AToB",
                **{"from": "A"},
                to="B",
            ),
        ],
    )
    binding = Binding(
        binding="flat_binding",
        ontology="flat_test",
        target=BigQueryTarget(backend="bigquery", project="p", dataset="d"),
        entities=[
            EntityBinding(
                name="A",
                source="a_table",
                properties=[
                    PropertyBinding(name="a_id", column="a_id"),
                    PropertyBinding(name="val", column="val"),
                ],
            ),
            EntityBinding(
                name="B",
                source="b_table",
                properties=[
                    PropertyBinding(name="b_id", column="b_id"),
                ],
            ),
        ],
        relationships=[
            RelationshipBinding(
                name="AToB",
                source="a_to_b",
                from_columns=["a_id"],
                to_columns=["b_id"],
            ),
        ],
    )
    ddl = compile_graph(ontology, binding)
    assert "CREATE PROPERTY GRAPH" in ddl
    assert "NODE TABLES" in ddl
    assert "EDGE TABLES" in ddl

  def test_upstream_ddl_rejects_extends(self):
    """Upstream compiler rejects ontologies with extends (v0)."""
    from bigquery_ontology import compile_graph

    spec = load_graph_spec(_DEMO_SPEC_PATH, env="p.d")
    ontology, binding = graph_spec_to_ontology_binding(
        spec, project_id="p", dataset_id="d"
    )
    # YMGO has sup_YahooAdUnit extends mako_Candidate
    with pytest.raises(ValueError, match="extends"):
      compile_graph(ontology, binding)
