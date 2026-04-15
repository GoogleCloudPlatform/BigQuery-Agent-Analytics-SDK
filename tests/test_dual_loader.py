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

"""Tests for the dual loader path (Step 3 migration)."""

from __future__ import annotations

import logging
import os

import pytest

from bigquery_agent_analytics.ontology_models import GraphSpec
from bigquery_agent_analytics.ontology_models import load_from_ontology_binding
from bigquery_agent_analytics.ontology_models import load_graph_spec

_FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")
_ONTOLOGY_PATH = os.path.join(_FIXTURES, "test_ontology.yaml")
_BINDING_PATH = os.path.join(_FIXTURES, "test_binding.yaml")
_DEMO_SPEC_PATH = os.path.join(
    os.path.dirname(__file__), "..", "examples", "ymgo_graph_spec.yaml"
)


class TestDualLoader:
  """Both load_graph_spec and load_from_ontology_binding produce GraphSpec."""

  def test_load_graph_spec_still_works(self):
    """Existing combined GraphSpec YAML path is unchanged."""
    spec = load_graph_spec(_DEMO_SPEC_PATH, env="p.d")
    assert isinstance(spec, GraphSpec)
    assert len(spec.entities) == 3
    assert len(spec.relationships) == 2

  def test_load_from_ontology_binding_produces_graph_spec(self):
    """Separated ontology + binding files produce a valid GraphSpec."""
    spec = load_from_ontology_binding(_ONTOLOGY_PATH, _BINDING_PATH)
    assert isinstance(spec, GraphSpec)
    assert spec.name == "shop"

  def test_entities_loaded(self):
    spec = load_from_ontology_binding(_ONTOLOGY_PATH, _BINDING_PATH)
    entity_names = {e.name for e in spec.entities}
    assert "Customer" in entity_names
    assert "Order" in entity_names

  def test_relationships_loaded(self):
    spec = load_from_ontology_binding(_ONTOLOGY_PATH, _BINDING_PATH)
    rel_names = {r.name for r in spec.relationships}
    assert "Placed" in rel_names

  def test_binding_column_names_used(self):
    """PropertySpec.name uses physical column from binding, not ontology."""
    spec = load_from_ontology_binding(_ONTOLOGY_PATH, _BINDING_PATH)
    customer = next(e for e in spec.entities if e.name == "Customer")
    prop_names = {p.name for p in customer.properties}
    # Binding maps customer_id->cust_id, display_name->name.
    assert "cust_id" in prop_names
    assert "name" in prop_names
    assert "customer_id" not in prop_names
    assert "display_name" not in prop_names

  def test_keys_use_physical_columns(self):
    """Primary key columns use physical names from binding."""
    spec = load_from_ontology_binding(_ONTOLOGY_PATH, _BINDING_PATH)
    customer = next(e for e in spec.entities if e.name == "Customer")
    # Binding maps customer_id->cust_id.
    assert customer.keys.primary == ["cust_id"]

  def test_source_fully_qualified(self):
    """Table sources are fully qualified from the binding target."""
    spec = load_from_ontology_binding(_ONTOLOGY_PATH, _BINDING_PATH)
    customer = next(e for e in spec.entities if e.name == "Customer")
    assert customer.binding.source == "test-proj.test_ds.customers"

  def test_relationship_endpoints(self):
    spec = load_from_ontology_binding(_ONTOLOGY_PATH, _BINDING_PATH)
    placed = next(r for r in spec.relationships if r.name == "Placed")
    assert placed.from_entity == "Customer"
    assert placed.to_entity == "Order"

  def test_relationship_join_columns(self):
    spec = load_from_ontology_binding(_ONTOLOGY_PATH, _BINDING_PATH)
    placed = next(r for r in spec.relationships if r.name == "Placed")
    assert placed.binding.from_columns == ["cust_id"]
    assert placed.binding.to_columns == ["order_id"]

  def test_graph_spec_validates(self):
    """The produced GraphSpec passes _validate_graph_spec."""
    from bigquery_agent_analytics.ontology_models import _validate_graph_spec

    spec = load_from_ontology_binding(_ONTOLOGY_PATH, _BINDING_PATH)
    # Must not raise.
    _validate_graph_spec(spec)

  def test_no_session_columns_by_default(self):
    """Without lineage_config, session columns are None."""
    spec = load_from_ontology_binding(_ONTOLOGY_PATH, _BINDING_PATH)
    for rel in spec.relationships:
      assert rel.binding.from_session_column is None
      assert rel.binding.to_session_column is None

  def test_lineage_config_threaded_to_adapter(self):
    """lineage_config is passed through to the adapter layer.

    Full lineage validation requires matching properties on the
    relationship (tested in test_runtime_spec.py). Here we verify
    the parameter is accepted and that a typo in the relationship
    name produces a warning.
    """
    import logging

    from bigquery_agent_analytics.runtime_spec import LineageEdgeConfig

    # Config for a relationship not in this spec — should warn.
    spec = load_from_ontology_binding(
        _ONTOLOGY_PATH,
        _BINDING_PATH,
        lineage_config={
            "NonExistentRel": LineageEdgeConfig(
                from_session_column="from_sid",
                to_session_column="to_sid",
            ),
        },
    )
    # Placed should have no session columns since it wasn't in config.
    placed = next(r for r in spec.relationships if r.name == "Placed")
    assert placed.binding.from_session_column is None
    assert placed.binding.to_session_column is None

  def test_lineage_config_typo_warns(self, caplog):
    """Mistyped relationship name in lineage_config produces a warning."""
    from bigquery_agent_analytics.runtime_spec import LineageEdgeConfig

    with caplog.at_level(logging.WARNING):
      load_from_ontology_binding(
          _ONTOLOGY_PATH,
          _BINDING_PATH,
          lineage_config={
              "PlacedTypo": LineageEdgeConfig(
                  from_session_column="from_sid",
                  to_session_column="to_sid",
              ),
          },
      )
    assert "PlacedTypo" in caplog.text
    assert "not found in the binding" in caplog.text

  def test_invalid_ontology_path_raises(self):
    with pytest.raises(FileNotFoundError):
      load_from_ontology_binding("/nonexistent.yaml", _BINDING_PATH)

  def test_runtime_usable(self):
    """GraphSpec from dual loader works with SDK runtime modules."""
    from bigquery_agent_analytics.ontology_schema_compiler import compile_extraction_prompt
    from bigquery_agent_analytics.ontology_schema_compiler import compile_output_schema

    spec = load_from_ontology_binding(_ONTOLOGY_PATH, _BINDING_PATH)
    prompt = compile_extraction_prompt(spec)
    assert "Customer" in prompt
    assert "Order" in prompt

    schema = compile_output_schema(spec)
    assert "cust_id" in schema
    assert "order_id" in schema
