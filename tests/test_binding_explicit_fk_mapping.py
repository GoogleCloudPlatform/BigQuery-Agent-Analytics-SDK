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

"""Tests for PR C1: explicit FK→PK mapping in ``RelationshipBinding``
(issue #179).

Covers:

* The pydantic shape boundary (``RelationshipBinding`` accepts both
  the legacy ``list[str]`` and the new ``list[dict[str, str]]``
  shapes; malformed entries are rejected with a precise error).
* The loader's canonical normalization
  (``normalize_relationship_columns`` resolves both shapes to a
  tuple of ``(edge_column, target_property)`` pairs; legacy entries
  default to the endpoint's Nth PK property).
* The list-view shim (``ResolvedRelationship.from_columns`` /
  ``to_columns`` remain ``tuple[str, ...]`` so downstream surfaces
  that only need column names keep working unchanged).
* Byte-identical SQL emission for the existing migration v5 binding
  (the canonical form is only consumed by callers that opt in;
  legacy callers see no SQL drift).
"""

from __future__ import annotations

import textwrap

import pytest
import yaml

from bigquery_ontology import load_binding_from_string
from bigquery_ontology import load_ontology_from_string
from bigquery_ontology.binding_loader import edge_column_names
from bigquery_ontology.binding_loader import normalize_relationship_columns
from bigquery_ontology.binding_models import RelationshipBinding

# Local ontology used by most of these tests. Two entities with a
# single-column PK each, one relationship between them.
_TOY_ONTOLOGY = textwrap.dedent(
    """
    ontology: toy
    entities:
      - name: Source
        keys:
          primary: [id]
        properties:
          - {name: id, type: string}
      - name: Target
        keys:
          primary: [id]
        properties:
          - {name: id, type: string}
    relationships:
      - name: linksTo
        from: Source
        to: Target
    """
).strip()


def _binding_with_columns(from_columns_yaml: str, to_columns_yaml: str) -> str:
  """Build a binding YAML fragment with the given column shapes
  embedded into a complete document the loader will accept."""
  return textwrap.dedent(
      f"""
      binding: toy_bq
      ontology: toy
      target:
        backend: bigquery
        project: p
        dataset: d
      entities:
        - name: Source
          source: p.d.source
          properties:
            - {{name: id, column: id}}
        - name: Target
          source: p.d.target
          properties:
            - {{name: id, column: id}}
      relationships:
        - name: linksTo
          source: p.d.linksto
          from_columns: {from_columns_yaml}
          to_columns: {to_columns_yaml}
      """
  ).strip()


# ------------------------------------------------------------------ #
# Pydantic shape boundary                                              #
# ------------------------------------------------------------------ #


class TestRelationshipBindingColumnShapes:
  """``RelationshipBinding.from_columns`` / ``to_columns`` accept
  both shapes. The pydantic validator enforces the structural
  contract; the semantic check (target_property exists on the
  endpoint) is in the loader."""

  def test_legacy_list_of_strings_parses(self):
    rb = RelationshipBinding(
        name="r",
        source="p.d.t",
        from_columns=["src_id"],
        to_columns=["dst_id"],
    )
    assert rb.from_columns == ["src_id"]
    assert rb.to_columns == ["dst_id"]

  def test_explicit_mapping_dict_parses(self):
    rb = RelationshipBinding(
        name="r",
        source="p.d.t",
        from_columns=[{"src_decision_execution_id": "id"}],
        to_columns=[{"dst_decision_execution_id": "id"}],
    )
    assert rb.from_columns == [{"src_decision_execution_id": "id"}]
    assert rb.to_columns == [{"dst_decision_execution_id": "id"}]

  def test_mixed_str_and_dict_entries_parses(self):
    """Composite endpoint where some columns use the legacy
    shape (default to endpoint's Nth PK) and others use the
    explicit shape."""
    rb = RelationshipBinding(
        name="r",
        source="p.d.t",
        from_columns=["src_col_a", {"src_col_b": "prop_b"}],
        to_columns=["dst_col_a", {"dst_col_b": "prop_b"}],
    )
    assert rb.from_columns == ["src_col_a", {"src_col_b": "prop_b"}]

  def test_empty_dict_rejected(self):
    with pytest.raises(
        ValueError, match=r"column entry \[0\] must be a single-key dict"
    ):
      RelationshipBinding(
          name="r",
          source="p.d.t",
          from_columns=[{}],
          to_columns=["dst_id"],
      )

  def test_multi_key_dict_rejected(self):
    with pytest.raises(ValueError, match=r"got dict with 2 key\(s\)"):
      RelationshipBinding(
          name="r",
          source="p.d.t",
          from_columns=[{"a": "p1", "b": "p2"}],
          to_columns=["dst_id"],
      )

  def test_empty_string_entry_rejected(self):
    with pytest.raises(
        ValueError, match=r"column entry \[0\] is an empty string"
    ):
      RelationshipBinding(
          name="r",
          source="p.d.t",
          from_columns=[""],
          to_columns=["dst_id"],
      )

  def test_non_string_dict_value_rejected(self):
    """A dict value of the wrong type is rejected. Pydantic's
    structural ``dict[str, str]`` check fires before my custom
    validator, so the error message is Pydantic's generic
    "Input should be a valid string" rather than my custom
    text — that's fine; the rejection still happens at the
    boundary with a clear message naming the offending entry."""
    with pytest.raises(Exception, match=r"should be a valid string"):
      RelationshipBinding(
          name="r",
          source="p.d.t",
          from_columns=[{"src_id": 42}],
          to_columns=["dst_id"],
      )

  def test_error_message_points_at_offending_entry(self):
    """The validator surfaces the index of the offending entry so
    operators can fix the typo without binary-searching the list."""
    with pytest.raises(ValueError) as excinfo:
      RelationshipBinding(
          name="r",
          source="p.d.t",
          from_columns=["src_a", "src_b", {}],
          to_columns=["dst_id"],
      )
    assert "[2]" in str(
        excinfo.value
    ), "the error should name index 2 (the empty dict), not 0 or 1"


# ------------------------------------------------------------------ #
# Loader's canonical normalization                                     #
# ------------------------------------------------------------------ #


class TestNormalizeRelationshipColumns:
  """``normalize_relationship_columns`` is the bridge between the
  pydantic shape (both forms accepted) and the canonical
  ``(edge_column, target_property)`` form ``ResolvedRelationship``
  carries. Tested directly so the contract is documented at the
  helper level too."""

  def _entity_map(self):
    ont = load_ontology_from_string(_TOY_ONTOLOGY)
    return {e.name: e for e in ont.entities}

  def test_legacy_str_entries_default_to_endpoint_pk(self):
    """Legacy ``list[str]`` resolves each entry to the endpoint's
    Nth PK property — the implicit convention every existing
    binding YAML relies on."""
    mapping = normalize_relationship_columns(
        ["src_id"],
        endpoint_entity_name="Source",
        entity_map=self._entity_map(),
        side="from",
        relationship_name="linksTo",
    )
    assert mapping == (("src_id", "id"),)

  def test_explicit_dict_entries_pass_through(self):
    mapping = normalize_relationship_columns(
        [{"src_decision_execution_id": "id"}],
        endpoint_entity_name="Source",
        entity_map=self._entity_map(),
        side="from",
        relationship_name="linksTo",
    )
    assert mapping == (("src_decision_execution_id", "id"),)

  def test_target_property_must_exist_on_endpoint(self):
    """Semantic check: if a dict entry references a property that
    isn't an effective primary-key property on the endpoint,
    raise with the bad name. ``not_a_real_property`` isn't
    declared at all on ``Source``; the inheritance fixture below
    covers the more nuanced "declared but not a PK" case."""
    with pytest.raises(
        ValueError,
        match=r"no primary-key property named 'not_a_real_property'",
    ):
      normalize_relationship_columns(
          [{"src_x": "not_a_real_property"}],
          endpoint_entity_name="Source",
          entity_map=self._entity_map(),
          side="from",
          relationship_name="linksTo",
      )

  def test_edge_column_names_helper(self):
    """``edge_column_names`` extracts just the list-view of edge
    column names — used by surfaces that don't care about the
    target property."""
    assert edge_column_names(["a", "b"]) == ("a", "b")
    assert edge_column_names([{"a": "p1"}, {"b": "p2"}]) == ("a", "b")
    assert edge_column_names(["a", {"b": "p2"}]) == ("a", "b")


# ------------------------------------------------------------------ #
# End-to-end via load_binding_from_string                              #
# ------------------------------------------------------------------ #


class TestEndToEndBindingLoad:
  """Full loader path: YAML → Binding → arity check → SDK
  ``ResolvedRelationship`` with the canonical mapping populated."""

  def test_legacy_binding_loads_with_default_mapping(self):
    binding_yaml = _binding_with_columns("[src_id]", "[dst_id]")
    ont = load_ontology_from_string(_TOY_ONTOLOGY)
    binding = load_binding_from_string(binding_yaml, ontology=ont)
    rb = binding.relationships[0]
    # Pydantic surface preserves the original list.
    assert rb.from_columns == ["src_id"]
    assert rb.to_columns == ["dst_id"]

  def test_dict_binding_loads(self):
    """The new shape parses cleanly when target_property names a
    real endpoint property."""
    binding_yaml = _binding_with_columns("[{src_id: id}]", "[{dst_id: id}]")
    ont = load_ontology_from_string(_TOY_ONTOLOGY)
    binding = load_binding_from_string(binding_yaml, ontology=ont)
    rb = binding.relationships[0]
    assert rb.from_columns == [{"src_id": "id"}]
    assert rb.to_columns == [{"dst_id": "id"}]

  def test_dict_binding_with_bad_target_property_rejected_at_resolve(self):
    """Semantic mistake (target_property doesn't exist on the
    endpoint) surfaces when the SDK's ``resolve()`` builds the
    canonical mapping. The pydantic + binding-loader shape pass
    is intentionally permissive here so the failure mode reads
    consistently with other ``ResolvedRelationship`` build errors."""
    from bigquery_agent_analytics.resolved_spec import resolve

    binding_yaml = _binding_with_columns(
        "[{src_id: not_a_real_property}]", "[{dst_id: id}]"
    )
    ont = load_ontology_from_string(_TOY_ONTOLOGY)
    binding = load_binding_from_string(binding_yaml, ontology=ont)
    with pytest.raises(
        ValueError,
        match=r"no primary-key property named 'not_a_real_property'",
    ):
      resolve(ontology=ont, binding=binding)


# ------------------------------------------------------------------ #
# ResolvedRelationship list-view shim + canonical mapping              #
# ------------------------------------------------------------------ #


class TestResolvedRelationshipShape:
  """``ResolvedRelationship`` keeps ``from_columns`` / ``to_columns``
  as the list view (downstream compat) AND carries the canonical
  mapping under ``from_column_mapping`` / ``to_column_mapping`` for
  callers that need the target property."""

  def _resolve_with(self, from_yaml: str, to_yaml: str):
    from bigquery_agent_analytics.resolved_spec import resolve

    binding_yaml = _binding_with_columns(from_yaml, to_yaml)
    ont = load_ontology_from_string(_TOY_ONTOLOGY)
    binding = load_binding_from_string(binding_yaml, ontology=ont)
    return resolve(ontology=ont, binding=binding)

  def test_legacy_binding_populates_both_views(self):
    g = self._resolve_with("[src_id]", "[dst_id]")
    rel = g.relationships[0]
    # List view — what downstream callers (DDL compiler, validators,
    # scaffolders) keep reading. Same shape as before C1.
    assert rel.from_columns == ("src_id",)
    assert rel.to_columns == ("dst_id",)
    # Canonical mapping — populated even for the legacy shape so
    # newer callers (e.g. C2's self-edge materializer fix) don't
    # need a fallback branch for legacy bindings.
    assert rel.from_column_mapping == (("src_id", "id"),)
    assert rel.to_column_mapping == (("dst_id", "id"),)

  def test_explicit_dict_binding_populates_both_views(self):
    g = self._resolve_with(
        "[{src_decision_execution_id: id}]",
        "[{dst_decision_execution_id: id}]",
    )
    rel = g.relationships[0]
    assert rel.from_columns == ("src_decision_execution_id",)
    assert rel.to_columns == ("dst_decision_execution_id",)
    assert rel.from_column_mapping == (("src_decision_execution_id", "id"),)
    assert rel.to_column_mapping == (("dst_decision_execution_id", "id"),)

  def test_list_view_matches_canonical_first_components(self):
    """For any binding, the list-view ``from_columns`` is the
    first component of each canonical mapping pair. The shim is
    derived, not duplicated."""
    g = self._resolve_with("[src_id]", "[dst_id]")
    rel = g.relationships[0]
    assert rel.from_columns == tuple(c for c, _ in rel.from_column_mapping)
    assert rel.to_columns == tuple(c for c, _ in rel.to_column_mapping)


# ------------------------------------------------------------------ #
# Byte-identical SQL for existing bindings (migration v5)              #
# ------------------------------------------------------------------ #


class TestExistingMigrationV5BindingByteIdenticalSQL:
  """The migration v5 binding uses the legacy ``list[str]`` shape
  exclusively. C1 must produce byte-identical resolved relationships
  for it — the field shape grew but the values that flow into
  downstream SQL compilers stay the same."""

  def test_migration_v5_legacy_binding_round_trips(self):
    """Load the committed migration v5 binding (legacy shape) and
    confirm every relationship's list-view ``from_columns`` /
    ``to_columns`` are unchanged, byte-for-byte."""
    import pathlib

    repo_root = pathlib.Path(__file__).resolve().parents[1]
    ont_path = repo_root / "examples" / "migration_v5" / "ontology.yaml"
    binding_path = repo_root / "examples" / "migration_v5" / "binding.yaml"
    if not ont_path.exists() or not binding_path.exists():
      pytest.skip("migration_v5 snapshots not checked in")
    ont = load_ontology_from_string(ont_path.read_text())
    binding = load_binding_from_string(binding_path.read_text(), ontology=ont)
    from bigquery_agent_analytics.resolved_spec import resolve

    resolved = resolve(ontology=ont, binding=binding)
    # Each relationship's list-view columns equal the original
    # binding YAML values exactly.
    binding_rels = {r.name: r for r in binding.relationships}
    for rel in resolved.relationships:
      rb = binding_rels[rel.name]
      assert rel.from_columns == tuple(
          rb.from_columns
      ), f"list-view drift on {rel.name}.from_columns"
      assert rel.to_columns == tuple(
          rb.to_columns
      ), f"list-view drift on {rel.name}.to_columns"
      # Canonical mapping is populated even for the legacy shape.
      assert rel.from_column_mapping is not None
      assert rel.to_column_mapping is not None
      assert len(rel.from_column_mapping) == len(rb.from_columns)
      assert len(rel.to_column_mapping) == len(rb.to_columns)


# ------------------------------------------------------------------ #
# PR #191 review fixes — inherited PK + PK-only target restriction    #
# ------------------------------------------------------------------ #


_INHERITANCE_ONTOLOGY = textwrap.dedent(
    """
    ontology: inherits
    entities:
      - name: Party
        keys:
          primary: [party_id]
        properties:
          - {name: party_id, type: string}
          - {name: display_name, type: string}
      - name: Person
        extends: Party
        properties:
          - {name: email, type: string}
      - name: Account
        keys:
          primary: [account_id]
        properties:
          - {name: account_id, type: string}
    relationships:
      - name: ownsAccount
        from: Person
        to: Account
    """
).strip()


_INHERITANCE_BINDING = textwrap.dedent(
    """
    binding: inherits_bq
    ontology: inherits
    target:
      backend: bigquery
      project: p
      dataset: d
    entities:
      - name: Person
        source: p.d.person
        properties:
          - {name: party_id, column: party_id}
          - {name: display_name, column: display_name}
          - {name: email, column: email}
      - name: Account
        source: p.d.account
        properties:
          - {name: account_id, column: account_id}
    relationships:
      - name: ownsAccount
        source: p.d.owns_account
        from_columns: [src_party_id]
        to_columns: [dst_account_id]
    """
).strip()


class TestInheritedKeyRegression:
  """Regression for PR #191 review (P1): a relationship whose
  endpoint inherits its PK from a parent (e.g. ``Person extends
  Party`` where ``Party`` owns ``keys.primary: [party_id]``) must
  resolve cleanly. Previously ``normalize_relationship_columns``
  read ``endpoint.keys`` directly and bailed with "no primary key
  declared" for inherited keys; mirroring the arity check's
  ``_effective_keys`` call fixes it.
  """

  def test_relationship_endpoint_with_inherited_pk_resolves(self):
    """Person inherits ``party_id`` from Party. The legacy
    ``list[str]`` shape must resolve to the inherited PK as the
    default target property."""
    from bigquery_agent_analytics.resolved_spec import resolve

    ont = load_ontology_from_string(_INHERITANCE_ONTOLOGY)
    binding = load_binding_from_string(_INHERITANCE_BINDING, ontology=ont)
    resolved = resolve(ontology=ont, binding=binding)
    rel = resolved.relationships[0]
    # Legacy str entry on the from side resolves to the inherited
    # PK property name.
    assert rel.from_column_mapping == (("src_party_id", "party_id"),)
    # Account is not inherited; its PK resolves normally.
    assert rel.to_column_mapping == (("dst_account_id", "account_id"),)

  def test_inherited_pk_accepted_as_explicit_target(self):
    """Explicit dict entries can target inherited PKs too."""
    binding_yaml = textwrap.dedent(
        """
        binding: inherits_bq
        ontology: inherits
        target:
          backend: bigquery
          project: p
          dataset: d
        entities:
          - name: Person
            source: p.d.person
            properties:
              - {name: party_id, column: party_id}
              - {name: display_name, column: display_name}
              - {name: email, column: email}
          - name: Account
            source: p.d.account
            properties:
              - {name: account_id, column: account_id}
        relationships:
          - name: ownsAccount
            source: p.d.owns_account
            from_columns: [{src_party_id: party_id}]
            to_columns:   [{dst_account_id: account_id}]
        """
    ).strip()
    from bigquery_agent_analytics.resolved_spec import resolve

    ont = load_ontology_from_string(_INHERITANCE_ONTOLOGY)
    binding = load_binding_from_string(binding_yaml, ontology=ont)
    resolved = resolve(ontology=ont, binding=binding)
    rel = resolved.relationships[0]
    assert rel.from_column_mapping == (("src_party_id", "party_id"),)


class TestExplicitMappingPKOnly:
  """Regression for PR #191 review (P2): explicit
  ``target_property`` must be one of the endpoint's effective
  primary-key properties. The PR is explicitly FK→PK; allowing
  any-declared-property would let C2's materializer fix consume a
  canonical mapping that points an edge endpoint at a non-key
  column, which doesn't uniquely identify the target row."""

  def test_explicit_mapping_to_non_pk_property_rejected(self):
    """``display_name`` is a real declared (non-PK) property on Party (and
    thus on Person via inheritance), but it is NOT a PK property.
    A binding that targets it must be rejected at the
    normalization step before C2 ever sees it."""
    binding_yaml = textwrap.dedent(
        """
        binding: inherits_bq
        ontology: inherits
        target:
          backend: bigquery
          project: p
          dataset: d
        entities:
          - name: Person
            source: p.d.person
            properties:
              - {name: party_id, column: party_id}
              - {name: display_name, column: display_name}
              - {name: email, column: email}
          - name: Account
            source: p.d.account
            properties:
              - {name: account_id, column: account_id}
        relationships:
          - name: ownsAccount
            source: p.d.owns_account
            from_columns: [{src_display_name: display_name}]
            to_columns:   [{dst_account_id: account_id}]
        """
    ).strip()
    from bigquery_agent_analytics.resolved_spec import resolve

    ont = load_ontology_from_string(_INHERITANCE_ONTOLOGY)
    binding = load_binding_from_string(binding_yaml, ontology=ont)
    with pytest.raises(
        ValueError, match=r"has no primary-key property named 'display_name'"
    ):
      resolve(ontology=ont, binding=binding)

  def test_non_pk_target_error_lists_effective_pk_properties(self):
    """The error message names the effective PK property set so the
    operator can fix the typo without inspecting the ontology by
    hand."""
    binding_yaml = textwrap.dedent(
        """
        binding: inherits_bq
        ontology: inherits
        target:
          backend: bigquery
          project: p
          dataset: d
        entities:
          - name: Person
            source: p.d.person
            properties:
              - {name: party_id, column: party_id}
              - {name: display_name, column: display_name}
              - {name: email, column: email}
          - name: Account
            source: p.d.account
            properties:
              - {name: account_id, column: account_id}
        relationships:
          - name: ownsAccount
            source: p.d.owns_account
            from_columns: [{src_email: email}]
            to_columns:   [{dst_account_id: account_id}]
        """
    ).strip()
    from bigquery_agent_analytics.resolved_spec import resolve

    ont = load_ontology_from_string(_INHERITANCE_ONTOLOGY)
    binding = load_binding_from_string(binding_yaml, ontology=ont)
    with pytest.raises(ValueError) as excinfo:
      resolve(ontology=ont, binding=binding)
    # Error names the available PK set (inherited from Party).
    assert "['party_id']" in str(excinfo.value)
