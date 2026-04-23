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

"""Adapter-layer contract: SDK never materializes abstract upstream elements.

Both SDK adapters (``graph_spec_from_ontology_binding`` producing a
``GraphSpec`` and ``resolve`` producing a ``ResolvedGraph``) must filter
``abstract=True`` entities/relationships out of the upstream
``Ontology`` before building name-indexed maps. This is the
single choke-point that protects every downstream SDK consumer from
PR #62's relaxed ``(name, from, to)`` uniqueness for abstract
relationships, which would otherwise collapse under ``{r.name: r}``
last-write-wins.

The validated ``load_binding(...)`` path already rejects bindings that
target abstract elements (``binding_loader.py:229, :245``), so in the
normal user flow these tests do not exercise a live regression. They
are defense-in-depth against callers that construct ``Ontology`` and
``Binding`` programmatically and bypass the upstream loader.
"""

from __future__ import annotations

import pytest

from bigquery_agent_analytics.resolved_spec import resolve
from bigquery_agent_analytics.runtime_spec import graph_spec_from_ontology_binding
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


# ------------------------------------------------------------------ #
# Fixtures                                                            #
# ------------------------------------------------------------------ #


def _concrete_account_entity() -> Entity:
  return Entity(
      name="Account",
      keys=Keys(primary=["account_id"]),
      properties=[Property(name="account_id", type=PropertyType.STRING)],
  )


def _concrete_account_binding() -> EntityBinding:
  return EntityBinding(
      name="Account",
      source="accounts",
      properties=[PropertyBinding(name="account_id", column="account_id")],
  )


def _target() -> BigQueryTarget:
  return BigQueryTarget(backend="bigquery", project="p", dataset="d")


def _binding_with_account_only() -> Binding:
  return Binding(
      binding="test_binding",
      ontology="test",
      target=_target(),
      entities=[_concrete_account_binding()],
      relationships=[],
  )


# ------------------------------------------------------------------ #
# graph_spec_from_ontology_binding                                    #
# ------------------------------------------------------------------ #


class TestGraphSpecAbstractFilter:
  """Forward adapter drops abstract upstream elements."""

  def test_abstract_entity_filtered(self):
    """An abstract sibling must not appear in the produced GraphSpec."""
    ontology = Ontology(
        ontology="test",
        entities=[
            _concrete_account_entity(),
            Entity(name="skos_Banking", abstract=True),
        ],
        relationships=[],
    )
    spec = graph_spec_from_ontology_binding(
        ontology, _binding_with_account_only()
    )
    entity_names = {e.name for e in spec.entities}
    assert entity_names == {"Account"}

  def test_abstract_relationship_filtered(self):
    """An abstract relationship must not appear in the produced GraphSpec."""
    ontology = Ontology(
        ontology="test",
        entities=[
            _concrete_account_entity(),
            Entity(name="skos_Banking", abstract=True),
        ],
        relationships=[
            Relationship(
                name="skos_broader",
                abstract=True,
                **{"from": "Account"},
                to="skos_Banking",
            ),
        ],
    )
    spec = graph_spec_from_ontology_binding(
        ontology, _binding_with_account_only()
    )
    rel_names = {r.name for r in spec.relationships}
    assert rel_names == set()

  def test_all_abstract_raises_clear_error(self):
    """All-abstract ontology raises a clear adapter-layer error."""
    ontology = Ontology(
        ontology="test",
        entities=[
            Entity(name="skos_Banking", abstract=True),
            Entity(name="skos_RetailBanking", abstract=True),
        ],
        relationships=[],
    )
    binding = Binding(
        binding="test_binding",
        ontology="test",
        target=_target(),
        entities=[],
        relationships=[],
    )
    with pytest.raises(ValueError) as exc:
      graph_spec_from_ontology_binding(ontology, binding)
    msg = str(exc.value)
    assert "abstract" in msg.lower()
    assert "no concrete entities" in msg.lower()

  def test_duplicate_endpoint_abstract_relationships_defense_in_depth(self):
    """Two abstract rels with the same name but different endpoints must
    not collapse under ``{r.name: r}`` last-write-wins.

    This scenario is only reachable by programmatic construction that
    bypasses ``load_binding()`` — the validated path rejects bindings
    targeting abstract elements. Kept to prove the adapter filter is
    the choke point even when upstream validation is skipped.
    """
    ontology = Ontology(
        ontology="test",
        entities=[
            _concrete_account_entity(),
            Entity(name="skos_RetailBanking", abstract=True),
            Entity(name="skos_InvestmentBanking", abstract=True),
            Entity(name="skos_Banking", abstract=True),
        ],
        relationships=[
            Relationship(
                name="skos_broader",
                abstract=True,
                **{"from": "skos_RetailBanking"},
                to="skos_Banking",
            ),
            Relationship(
                name="skos_broader",
                abstract=True,
                **{"from": "skos_InvestmentBanking"},
                to="skos_Banking",
            ),
        ],
    )
    spec = graph_spec_from_ontology_binding(
        ontology, _binding_with_account_only()
    )
    # Both abstract relationships are filtered; neither appears in GraphSpec.
    assert spec.relationships == []
    assert {e.name for e in spec.entities} == {"Account"}

  def test_programmatic_binding_targeting_abstract_entity_rejected(self):
    """Programmatic Binding that targets an abstract entity must be
    rejected by the adapter with the same ``not defined`` error used
    when the name doesn't exist at all.

    ``load_binding()`` already rejects this at upstream validation
    time; this test exercises the adapter's own guarantee for callers
    that instantiate ``Binding`` directly in Python.
    """
    ontology = Ontology(
        ontology="test",
        entities=[
            _concrete_account_entity(),
            Entity(name="skos_Banking", abstract=True),
        ],
        relationships=[],
    )
    binding = Binding(
        binding="test_binding",
        ontology="test",
        target=_target(),
        entities=[
            _concrete_account_binding(),
            EntityBinding(
                name="skos_Banking",
                source="banking",
                properties=[],
            ),
        ],
        relationships=[],
    )
    with pytest.raises(ValueError, match="not defined"):
      graph_spec_from_ontology_binding(ontology, binding)

  def test_programmatic_binding_targeting_abstract_relationship_rejected(self):
    """Symmetric test: a programmatic Binding targeting an abstract
    relationship must be rejected by the adapter, not silently
    succeed by reading a collapsed name-map entry.
    """
    from bigquery_ontology import RelationshipBinding

    ontology = Ontology(
        ontology="test",
        entities=[
            _concrete_account_entity(),
            Entity(name="skos_Banking", abstract=True),
        ],
        relationships=[
            Relationship(
                name="skos_broader",
                abstract=True,
                **{"from": "Account"},
                to="skos_Banking",
            ),
        ],
    )
    binding = Binding(
        binding="test_binding",
        ontology="test",
        target=_target(),
        entities=[_concrete_account_binding()],
        relationships=[
            RelationshipBinding(
                name="skos_broader",
                source="broader_edges",
                from_columns=["account_id"],
                to_columns=["account_id"],
            ),
        ],
    )
    with pytest.raises(ValueError, match="not defined"):
      graph_spec_from_ontology_binding(ontology, binding)


# ------------------------------------------------------------------ #
# resolve                                                             #
# ------------------------------------------------------------------ #


class TestResolveAbstractFilter:
  """Reverse-adapter symmetry: ``resolve()`` drops abstract elements too."""

  def test_abstract_entity_filtered(self):
    ontology = Ontology(
        ontology="test",
        entities=[
            _concrete_account_entity(),
            Entity(name="skos_Banking", abstract=True),
        ],
        relationships=[],
    )
    graph = resolve(ontology, _binding_with_account_only())
    entity_names = {e.name for e in graph.entities}
    assert entity_names == {"Account"}

  def test_abstract_relationship_filtered(self):
    ontology = Ontology(
        ontology="test",
        entities=[
            _concrete_account_entity(),
            Entity(name="skos_Banking", abstract=True),
        ],
        relationships=[
            Relationship(
                name="skos_broader",
                abstract=True,
                **{"from": "Account"},
                to="skos_Banking",
            ),
        ],
    )
    graph = resolve(ontology, _binding_with_account_only())
    rel_names = {r.name for r in graph.relationships}
    assert rel_names == set()

  def test_all_abstract_raises_clear_error(self):
    ontology = Ontology(
        ontology="test",
        entities=[
            Entity(name="skos_Banking", abstract=True),
            Entity(name="skos_RetailBanking", abstract=True),
        ],
        relationships=[],
    )
    binding = Binding(
        binding="test_binding",
        ontology="test",
        target=_target(),
        entities=[],
        relationships=[],
    )
    with pytest.raises(ValueError) as exc:
      resolve(ontology, binding)
    msg = str(exc.value)
    assert "abstract" in msg.lower()
    assert "no concrete entities" in msg.lower()

  def test_duplicate_endpoint_abstract_relationships_defense_in_depth(self):
    """Symmetric defense-in-depth test for resolve(). See the twin test
    above for context — abstract rels with duplicate names across
    endpoints must not collapse in the name-index map.
    """
    ontology = Ontology(
        ontology="test",
        entities=[
            _concrete_account_entity(),
            Entity(name="skos_RetailBanking", abstract=True),
            Entity(name="skos_InvestmentBanking", abstract=True),
            Entity(name="skos_Banking", abstract=True),
        ],
        relationships=[
            Relationship(
                name="skos_broader",
                abstract=True,
                **{"from": "skos_RetailBanking"},
                to="skos_Banking",
            ),
            Relationship(
                name="skos_broader",
                abstract=True,
                **{"from": "skos_InvestmentBanking"},
                to="skos_Banking",
            ),
        ],
    )
    graph = resolve(ontology, _binding_with_account_only())
    assert graph.relationships == ()
    assert {e.name for e in graph.entities} == {"Account"}

  def test_programmatic_binding_targeting_abstract_entity_rejected(self):
    """Symmetric: programmatic Binding targeting an abstract entity
    must raise the ``not defined`` error in ``resolve()`` too.
    """
    ontology = Ontology(
        ontology="test",
        entities=[
            _concrete_account_entity(),
            Entity(name="skos_Banking", abstract=True),
        ],
        relationships=[],
    )
    binding = Binding(
        binding="test_binding",
        ontology="test",
        target=_target(),
        entities=[
            _concrete_account_binding(),
            EntityBinding(
                name="skos_Banking",
                source="banking",
                properties=[],
            ),
        ],
        relationships=[],
    )
    with pytest.raises(ValueError, match="not defined"):
      resolve(ontology, binding)

  def test_programmatic_binding_targeting_abstract_relationship_rejected(self):
    """Symmetric: programmatic Binding targeting an abstract
    relationship must raise the ``not defined`` error in ``resolve()``
    too.
    """
    from bigquery_ontology import RelationshipBinding

    ontology = Ontology(
        ontology="test",
        entities=[
            _concrete_account_entity(),
            Entity(name="skos_Banking", abstract=True),
        ],
        relationships=[
            Relationship(
                name="skos_broader",
                abstract=True,
                **{"from": "Account"},
                to="skos_Banking",
            ),
        ],
    )
    binding = Binding(
        binding="test_binding",
        ontology="test",
        target=_target(),
        entities=[_concrete_account_binding()],
        relationships=[
            RelationshipBinding(
                name="skos_broader",
                source="broader_edges",
                from_columns=["account_id"],
                to_columns=["account_id"],
            ),
        ],
    )
    with pytest.raises(ValueError, match="not defined"):
      resolve(ontology, binding)

  def test_concrete_child_extends_abstract_parent_inherits_keys(self):
    """A concrete entity may extend an abstract parent and inherit its
    keys and properties. Upstream validation allows this shape; the
    adapter must preserve it.

    The filter must not remove abstract parents from the inheritance-
    traversal map, or ``_effective_keys`` / ``_effective_properties``
    will crash with a ``KeyError`` when walking the ancestor chain.
    """
    ontology = Ontology(
        ontology="test",
        entities=[
            Entity(
                name="AbstractParent",
                abstract=True,
                keys=Keys(primary=["pid"]),
                properties=[
                    Property(name="pid", type=PropertyType.STRING),
                    Property(name="label", type=PropertyType.STRING),
                ],
            ),
            Entity(
                name="Child",
                extends="AbstractParent",
                # No own keys or properties -- inherited from parent.
            ),
        ],
        relationships=[],
    )
    binding = Binding(
        binding="test_binding",
        ontology="test",
        target=_target(),
        entities=[
            EntityBinding(
                name="Child",
                source="children",
                properties=[
                    PropertyBinding(name="pid", column="child_id"),
                    PropertyBinding(name="label", column="child_label"),
                ],
            ),
        ],
        relationships=[],
    )
    graph = resolve(ontology, binding)
    # AbstractParent must not appear in the output -- it is not bindable.
    assert {e.name for e in graph.entities} == {"Child"}
    # Child inherits pid as its primary key (remapped to child_id).
    child = graph.entities[0]
    assert child.key_columns == ("child_id",)
    # Child also inherits the label property.
    col_names = {p.column for p in child.properties}
    assert col_names == {"child_id", "child_label"}
