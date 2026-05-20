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
    """The new shape parses cleanly when target_property names an
    effective primary-key property on the endpoint."""
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


# ------------------------------------------------------------------ #
# Composite-PK coverage invariant (PR #191 review P2)                  #
# ------------------------------------------------------------------ #


_COMPOSITE_ONTOLOGY = textwrap.dedent(
    """
    ontology: composite
    entities:
      - name: ABody
        keys:
          primary: [a_id]
        properties:
          - {name: a_id, type: string}
      - name: BPair
        keys:
          primary: [k1, k2]
        properties:
          - {name: k1, type: string}
          - {name: k2, type: string}
    relationships:
      - name: refs
        from: ABody
        to: BPair
    """
).strip()


def _composite_binding(to_columns_yaml: str) -> str:
  return textwrap.dedent(
      f"""
      binding: composite_bq
      ontology: composite
      target:
        backend: bigquery
        project: p
        dataset: d
      entities:
        - name: ABody
          source: p.d.a
          properties:
            - {{name: a_id, column: a_id}}
        - name: BPair
          source: p.d.b
          properties:
            - {{name: k1, column: k1}}
            - {{name: k2, column: k2}}
      relationships:
        - name: refs
          source: p.d.refs
          from_columns: [a_id]
          to_columns: {to_columns_yaml}
      """
  ).strip()


class TestCompositePKCoverageInvariant:
  """Regression for PR #191 review (P2): the canonical mapping must
  be a permutation of the endpoint's effective PK properties — each
  PK property covered exactly once. Arity already matches by the
  time the per-entry PK check runs; without an additional "no
  duplicate target" check, a composite PK ``[k1, k2]`` could be
  bound as two entries that both target ``k1``, silently dropping
  ``k2``. C2 would then consume a canonical endpoint mapping that
  cannot uniquely identify the target row.
  """

  def test_legitimate_composite_pk_permutation_resolves(self):
    """The intended use of the explicit shape for composite PKs:
    each PK property mapped exactly once, in either declaration
    order or any permutation."""
    from bigquery_agent_analytics.resolved_spec import resolve

    binding_yaml = _composite_binding("[{b_k1: k1}, {b_k2: k2}]")
    ont = load_ontology_from_string(_COMPOSITE_ONTOLOGY)
    binding = load_binding_from_string(binding_yaml, ontology=ont)
    resolved = resolve(ontology=ont, binding=binding)
    rel = resolved.relationships[0]
    assert rel.to_column_mapping == (("b_k1", "k1"), ("b_k2", "k2"))

  def test_reversed_composite_pk_permutation_resolves(self):
    """Permutation, not strict declaration order — the explicit
    shape lets the binding author choose whichever edge column
    name they want for each PK."""
    from bigquery_agent_analytics.resolved_spec import resolve

    binding_yaml = _composite_binding("[{b_k2: k2}, {b_k1: k1}]")
    ont = load_ontology_from_string(_COMPOSITE_ONTOLOGY)
    binding = load_binding_from_string(binding_yaml, ontology=ont)
    resolved = resolve(ontology=ont, binding=binding)
    rel = resolved.relationships[0]
    assert rel.to_column_mapping == (("b_k2", "k2"), ("b_k1", "k1"))

  def test_duplicate_target_property_rejected(self):
    """The original bug: both entries target the same PK
    property. With arity matching, that necessarily leaves one
    PK property uncovered."""
    from bigquery_agent_analytics.resolved_spec import resolve

    binding_yaml = _composite_binding("[{b_k1_left: k1}, {b_k1_right: k1}]")
    ont = load_ontology_from_string(_COMPOSITE_ONTOLOGY)
    binding = load_binding_from_string(binding_yaml, ontology=ont)
    with pytest.raises(
        ValueError,
        match=(r"covers some endpoint PK properties more than once " r".*'k1'"),
    ):
      resolve(ontology=ont, binding=binding)

  def test_duplicate_target_error_names_offending_property(self):
    """The error message names the duplicated PK property and the
    full effective PK set so the operator can fix the typo
    without inspecting the ontology by hand."""
    from bigquery_agent_analytics.resolved_spec import resolve

    binding_yaml = _composite_binding("[{b_k2_left: k2}, {b_k2_right: k2}]")
    ont = load_ontology_from_string(_COMPOSITE_ONTOLOGY)
    binding = load_binding_from_string(binding_yaml, ontology=ont)
    with pytest.raises(ValueError) as excinfo:
      resolve(ontology=ont, binding=binding)
    msg = str(excinfo.value)
    assert "'k2'" in msg, "duplicated PK property must appear in the error"
    assert "'k1'" in msg, (
        "effective PK set must appear in the error so the operator can "
        "see what was supposed to be covered"
    )

  def test_legacy_str_shape_unaffected_by_permutation_check(self):
    """The duplicate-target check is for the explicit dict shape.
    The legacy ``list[str]`` shape resolves entries to the
    endpoint's Nth PK property by position — it's structurally
    impossible to produce a duplicate target through that path
    when the arity check already matched. Sanity-check that
    composite-PK legacy bindings still resolve cleanly."""
    from bigquery_agent_analytics.resolved_spec import resolve

    binding_yaml = _composite_binding("[b_k1, b_k2]")
    ont = load_ontology_from_string(_COMPOSITE_ONTOLOGY)
    binding = load_binding_from_string(binding_yaml, ontology=ont)
    resolved = resolve(ontology=ont, binding=binding)
    rel = resolved.relationships[0]
    assert rel.to_column_mapping == (("b_k1", "k1"), ("b_k2", "k2"))


# ------------------------------------------------------------------ #
# Raw-binding consumer guards (PR #191 review P1)                      #
# ------------------------------------------------------------------ #


class TestRawBindingConsumerGuards:
  """C1 introduced the dict-shape ``ColumnRef`` at the binding-model
  boundary, but a couple of consumers (``compile_graph``,
  ``graph_spec_from_ontology_binding``) read ``rb.from_columns`` /
  ``rb.to_columns`` directly as ``list[str]`` and would silently
  mispair (or crash) on a dict-shape binding. C2 will wire them
  through the canonical mapping; until then, those boundaries raise
  with a precise error pointing at the migration path. This test
  class pins the contract."""

  def _dict_shape_binding(self):
    binding_yaml = _binding_with_columns("[{src_id: id}]", "[{dst_id: id}]")
    ont = load_ontology_from_string(_TOY_ONTOLOGY)
    binding = load_binding_from_string(binding_yaml, ontology=ont)
    return ont, binding

  def test_compile_graph_rejects_dict_shape(self):
    """``compile_graph`` pairs edge columns positionally with the
    endpoint's PK columns when emitting ``SOURCE KEY ...
    REFERENCES from_node (...)``. Until C2 wires the canonical
    mapping through, a dict-shape binding must raise — not crash
    at ``str.join`` or mispair silently."""
    from bigquery_ontology.graph_ddl_compiler import compile_graph

    ont, binding = self._dict_shape_binding()
    with pytest.raises(
        ValueError,
        match=r"compile_graph: relationship binding 'linksTo' from_columns",
    ):
      compile_graph(ont, binding)

  def test_graph_spec_from_ontology_binding_rejects_dict_shape(self):
    """The legacy ``GraphSpec`` converter targets
    ``BindingSpec.from_columns: list[str]``; a dict-shape entry
    would pydantic-error there. Catch at the boundary with the
    same actionable error as ``compile_graph``."""
    from bigquery_agent_analytics.runtime_spec import graph_spec_from_ontology_binding

    ont, binding = self._dict_shape_binding()
    with pytest.raises(
        ValueError,
        match=(
            r"graph_spec_from_ontology_binding: relationship "
            r"binding 'linksTo' from_columns"
        ),
    ):
      graph_spec_from_ontology_binding(ont, binding)

  def test_consumer_guard_error_names_migration_path(self):
    """The error message tells the operator how to fix this: route
    through ``resolve()`` and read
    ``ResolvedRelationship.from_column_mapping``."""
    from bigquery_ontology.graph_ddl_compiler import compile_graph

    ont, binding = self._dict_shape_binding()
    with pytest.raises(ValueError) as excinfo:
      compile_graph(ont, binding)
    msg = str(excinfo.value)
    assert "resolve()" in msg
    assert "from_column_mapping" in msg

  def test_legacy_shape_passes_through_both_consumers(self):
    """The whole point of the guard is to keep legacy bindings
    working unchanged — sanity-check that ``list[str]`` bindings
    still flow through both consumers."""
    from bigquery_agent_analytics.runtime_spec import graph_spec_from_ontology_binding
    from bigquery_ontology.graph_ddl_compiler import compile_graph

    binding_yaml = _binding_with_columns("[src_id]", "[dst_id]")
    ont = load_ontology_from_string(_TOY_ONTOLOGY)
    binding = load_binding_from_string(binding_yaml, ontology=ont)
    # Both consumers run cleanly.
    compile_graph(ont, binding)
    graph_spec_from_ontology_binding(ont, binding)
