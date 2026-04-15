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

"""Semantic tests for the binding loader.

Each test pairs a literal ontology YAML with a literal binding YAML,
and asserts either successful validation or the exact ``ValueError``
message. Shape-level validation is covered by
``test_binding_models.py``; this file exercises only cross-ontology
semantics.

The mental model the tests collectively prove: **partial at the
ontology level, total within each element**. A binding may leave whole
entities or relationships out, but whatever it *does* include must
bind every non-derived property (and no derived ones).

Most tests share the ``ONTOLOGY`` fixture below, which was shaped to
cover the dimensions the loader cares about:

  - ``Party`` — a parent entity with its own primary key and two
    plain properties, so children can exercise inherited coverage.
  - ``Person extends Party`` — adds stored properties (``dob``,
    ``first_name``, ``last_name``) plus a *derived* property
    (``full_name`` with ``expr:``). Used to test that derived
    properties must never appear in a binding.
  - ``Account``, ``Security`` — separate single-key entities, used
    as endpoints of ``HOLDS``.
  - ``HOLDS`` — a simple edge with one property for relationship
    coverage checks.

Tests that need a different shape (e.g. abstract-parent endpoints)
declare a local ontology inline rather than extending this one.
"""

from __future__ import annotations

import textwrap

import pytest

from bigquery_ontology import Binding
from bigquery_ontology import load_binding
from bigquery_ontology import load_binding_from_string
from bigquery_ontology import load_ontology_from_string

ONTOLOGY = """
  ontology: finance
  entities:
    - name: Party
      keys:
        primary: [party_id]
      properties:
        - name: party_id
          type: string
        - name: name
          type: string
    - name: Person
      extends: Party
      properties:
        - name: dob
          type: date
        - name: first_name
          type: string
        - name: last_name
          type: string
        - name: full_name
          type: string
          expr: "first_name || ' ' || last_name"
    - name: Account
      keys:
        primary: [account_id]
      properties:
        - name: account_id
          type: string
    - name: Security
      keys:
        primary: [security_id]
      properties:
        - name: security_id
          type: string
  relationships:
    - name: HOLDS
      from: Account
      to: Security
      properties:
        - name: quantity
          type: double
"""


def _ontology():
  return load_ontology_from_string(textwrap.dedent(ONTOLOGY).lstrip())


def _load(binding_yaml: str):
  return load_binding_from_string(
      textwrap.dedent(binding_yaml).lstrip(), ontology=_ontology()
  )


def _assert_value_error(binding_yaml: str, expected_message: str) -> None:
  with pytest.raises(ValueError) as exc_info:
    _load(binding_yaml)
  assert str(exc_info.value) == expected_message


# --------------------------------------------------------------------- #
# Valid bindings                                                         #
# --------------------------------------------------------------------- #


def test_full_binding_validates():
  binding_yaml = """
    binding: finance-bq-prod
    ontology: finance
    target:
      backend: bigquery
      project: p
      dataset: d
    entities:
      - name: Person
        source: raw.persons
        properties:
          - name: party_id
            column: person_id
          - name: name
            column: display_name
          - name: dob
            column: date_of_birth
          - name: first_name
            column: given_name
          - name: last_name
            column: family_name
      - name: Account
        source: raw.accounts
        properties:
          - name: account_id
            column: acct_id
      - name: Security
        source: ref.securities
        properties:
          - name: security_id
            column: cusip
    relationships:
      - name: HOLDS
        source: raw.holdings
        from_columns: [acct_id]
        to_columns: [cusip]
        properties:
          - name: quantity
            column: qty
  """
  b = _load(binding_yaml)
  assert b.binding == "finance-bq-prod"
  assert {e.name for e in b.entities} == {"Person", "Account", "Security"}
  assert [r.name for r in b.relationships] == ["HOLDS"]


def test_partial_binding_without_relationship_validates():
  # Demonstrates the "partial at the ontology level" half of the
  # rule. We include ``Account`` and leave everything else out —
  # including ``Security`` and ``HOLDS``. A reader might worry that
  # omitting ``Security`` is a problem because the fixture's ``HOLDS``
  # points at it, but ``HOLDS`` itself is also omitted, so no edge
  # in this binding has any expectation of ``Security`` being bound.
  binding_yaml = """
    binding: partial
    ontology: finance
    target: {backend: bigquery, project: p, dataset: d}
    entities:
      - name: Account
        source: raw.accounts
        properties:
          - name: account_id
            column: acct_id
  """
  _load(binding_yaml)


def test_unbound_parent_with_bound_child_satisfies_endpoint_closure():
  # Endpoint closure is descendant-aware: binding a concrete child
  # (``Person``) is enough to satisfy an edge whose declared endpoint
  # is the abstract parent (``Party``). We use a fresh ontology here
  # because the shared fixture's ``HOLDS`` endpoints are already
  # concrete — there would be nothing to test.
  ontology_yaml = """
    ontology: tiny
    entities:
      - name: Party
        keys: {primary: [party_id]}
        properties: [{name: party_id, type: string}]
      - name: Person
        extends: Party
        properties: []
    relationships:
      - name: KNOWS
        from: Party
        to: Party
  """
  binding_yaml = """
    binding: b
    ontology: tiny
    target: {backend: bigquery, project: p, dataset: d}
    entities:
      - name: Person
        source: t
        properties:
          - name: party_id
            column: id
    relationships:
      - name: KNOWS
        source: edges
        from_columns: [a]
        to_columns: [b]
  """
  ontology = load_ontology_from_string(textwrap.dedent(ontology_yaml).lstrip())
  load_binding_from_string(
      textwrap.dedent(binding_yaml).lstrip(), ontology=ontology
  )


# --------------------------------------------------------------------- #
# Cross-ontology errors                                                  #
# --------------------------------------------------------------------- #


def test_binding_ontology_name_mismatch_is_error():
  binding_yaml = """
    binding: b
    ontology: not-finance
    target: {backend: bigquery, project: p, dataset: d}
  """
  _assert_value_error(
      binding_yaml,
      "Binding declares ontology 'not-finance' but was paired with "
      "ontology 'finance'.",
  )


def test_duplicate_entity_binding_name_is_error():
  binding_yaml = """
    binding: b
    ontology: finance
    target: {backend: bigquery, project: p, dataset: d}
    entities:
      - name: Account
        source: t1
        properties: [{name: account_id, column: c}]
      - name: Account
        source: t2
        properties: [{name: account_id, column: c}]
  """
  _assert_value_error(binding_yaml, "Duplicate entity binding name: 'Account'")


def test_duplicate_relationship_binding_name_is_error():
  binding_yaml = """
    binding: b
    ontology: finance
    target: {backend: bigquery, project: p, dataset: d}
    entities:
      - name: Account
        source: t
        properties: [{name: account_id, column: c}]
      - name: Security
        source: t
        properties: [{name: security_id, column: c}]
    relationships:
      - name: HOLDS
        source: e1
        from_columns: [a]
        to_columns: [s]
        properties: [{name: quantity, column: q}]
      - name: HOLDS
        source: e2
        from_columns: [a]
        to_columns: [s]
        properties: [{name: quantity, column: q}]
  """
  _assert_value_error(
      binding_yaml, "Duplicate relationship binding name: 'HOLDS'"
  )


def test_cross_kind_duplicate_binding_name_is_error():
  """Defensive: same name used for an entity binding and a relationship."""
  from bigquery_ontology.binding_loader import _check_unique_binding_names
  from bigquery_ontology.binding_models import Backend
  from bigquery_ontology.binding_models import BigQueryTarget
  from bigquery_ontology.binding_models import EntityBinding
  from bigquery_ontology.binding_models import RelationshipBinding

  binding = Binding(
      binding="b",
      ontology="x",
      target=BigQueryTarget(backend=Backend.BIGQUERY, project="p", dataset="d"),
      entities=[EntityBinding(name="Foo", source="t", properties=[])],
      relationships=[
          RelationshipBinding(
              name="Foo", source="e", from_columns=["a"], to_columns=["b"]
          )
      ],
  )
  with pytest.raises(ValueError, match="Duplicate binding name: 'Foo'"):
    _check_unique_binding_names(binding)


def test_entity_binding_references_unknown_entity():
  binding_yaml = """
    binding: b
    ontology: finance
    target: {backend: bigquery, project: p, dataset: d}
    entities:
      - name: Ghost
        source: t
        properties: []
  """
  _assert_value_error(
      binding_yaml,
      "Entity binding 'Ghost' does not name a declared entity in "
      "ontology 'finance'.",
  )


def test_relationship_binding_references_unknown_relationship():
  binding_yaml = """
    binding: b
    ontology: finance
    target: {backend: bigquery, project: p, dataset: d}
    relationships:
      - name: MISSING
        source: t
        from_columns: [a]
        to_columns: [b]
  """
  _assert_value_error(
      binding_yaml,
      "Relationship binding 'MISSING' does not name a declared relationship "
      "in ontology 'finance'.",
  )


def test_missing_non_derived_property_is_error():
  # The "total within each element" half of the rule. ``Person``'s
  # effective (flattened) properties are: ``party_id``, ``name`` (both
  # inherited from ``Party``), plus ``dob``, ``first_name``,
  # ``last_name``, and the derived ``full_name``. The binding below
  # covers every stored one *except* ``name`` — partial coverage
  # inside an included entity is not allowed, so the loader rejects.
  binding_yaml = """
    binding: b
    ontology: finance
    target: {backend: bigquery, project: p, dataset: d}
    entities:
      - name: Person
        source: t
        properties:
          - name: party_id
            column: pid
          - name: dob
            column: d
          - name: first_name
            column: f
          - name: last_name
            column: l
  """
  _assert_value_error(
      binding_yaml,
      "Entity binding 'Person': missing bindings for non-derived "
      "properties ['name'].",
  )


def test_binding_derived_property_is_error():
  # ``Person.full_name`` is declared with ``expr:`` in the ontology,
  # so the compiler will substitute the expression at DDL-emission
  # time. A binding that lists it would shadow that substitution —
  # the loader rejects it rather than silently accepting a column
  # that the emitted query will never consult.
  binding_yaml = """
    binding: b
    ontology: finance
    target: {backend: bigquery, project: p, dataset: d}
    entities:
      - name: Person
        source: t
        properties:
          - name: party_id
            column: pid
          - name: name
            column: n
          - name: dob
            column: d
          - name: first_name
            column: f
          - name: last_name
            column: l
          - name: full_name
            column: fn
  """
  _assert_value_error(
      binding_yaml,
      "Entity binding 'Person': property 'full_name' is derived (has "
      "'expr:') and must not appear in a binding.",
  )


def test_unknown_property_name_in_binding_is_error():
  binding_yaml = """
    binding: b
    ontology: finance
    target: {backend: bigquery, project: p, dataset: d}
    entities:
      - name: Account
        source: t
        properties:
          - name: account_id
            column: c
          - name: bogus
            column: c2
  """
  _assert_value_error(
      binding_yaml,
      "Entity binding 'Account': property 'bogus' is not declared on "
      "this element.",
  )


def test_property_bound_twice_is_error():
  # A PropertyBinding list with two entries for the same ontology
  # property is ambiguous — which column wins? The loader refuses to
  # guess and flags the duplicate up front.
  binding_yaml = """
    binding: b
    ontology: finance
    target: {backend: bigquery, project: p, dataset: d}
    entities:
      - name: Account
        source: t
        properties:
          - name: account_id
            column: c1
          - name: account_id
            column: c2
  """
  _assert_value_error(
      binding_yaml,
      "Entity binding 'Account': property 'account_id' is bound more than once.",
  )


def test_relationship_from_columns_arity_mismatch_is_error():
  # ``Account``'s primary key is a single column, but the binding
  # supplies two ``from_columns``. The loader surfaces both the
  # binding's arity and the endpoint's expected arity so the author
  # can tell which side is wrong.
  binding_yaml = """
    binding: b
    ontology: finance
    target: {backend: bigquery, project: p, dataset: d}
    entities:
      - name: Account
        source: t
        properties: [{name: account_id, column: c}]
      - name: Security
        source: t
        properties: [{name: security_id, column: c}]
    relationships:
      - name: HOLDS
        source: e
        from_columns: [a, b]
        to_columns: [s]
        properties: [{name: quantity, column: q}]
  """
  _assert_value_error(
      binding_yaml,
      "Relationship binding 'HOLDS': from_columns has 2 column(s) but "
      "endpoint entity 'Account' has 1-column primary key.",
  )


def test_relationship_to_columns_arity_mismatch_is_error():
  # Mirror of the ``from_columns`` test, to make sure the check also
  # fires against the ``to`` endpoint (both sides are validated
  # independently, not short-circuited after one side passes).
  binding_yaml = """
    binding: b
    ontology: finance
    target: {backend: bigquery, project: p, dataset: d}
    entities:
      - name: Account
        source: t
        properties: [{name: account_id, column: c}]
      - name: Security
        source: t
        properties: [{name: security_id, column: c}]
    relationships:
      - name: HOLDS
        source: e
        from_columns: [a]
        to_columns: [s, s2]
        properties: [{name: quantity, column: q}]
  """
  _assert_value_error(
      binding_yaml,
      "Relationship binding 'HOLDS': to_columns has 2 column(s) but "
      "endpoint entity 'Security' has 1-column primary key.",
  )


def test_bound_relationship_with_unbound_endpoint_is_error():
  # ``HOLDS`` goes from ``Account`` to ``Security``. The binding
  # realizes ``Account`` and ``HOLDS`` but forgets ``Security``,
  # leaving the edge pointing at an entity tree with no bound node.
  # The loader flags the ``to`` endpoint specifically.
  binding_yaml = """
    binding: b
    ontology: finance
    target: {backend: bigquery, project: p, dataset: d}
    entities:
      - name: Account
        source: t
        properties: [{name: account_id, column: c}]
    relationships:
      - name: HOLDS
        source: e
        from_columns: [a]
        to_columns: [s]
        properties: [{name: quantity, column: q}]
  """
  _assert_value_error(
      binding_yaml,
      "Relationship binding 'HOLDS': endpoint (to) entity 'Security' has "
      "no bound descendant in this binding.",
  )


# --------------------------------------------------------------------- #
# File-based entry point: companion ontology auto-discovery              #
# --------------------------------------------------------------------- #


def test_load_binding_autodiscovers_companion_ontology(tmp_path):
  # Drop a binding and its companion ``<name>.ontology.yaml`` into
  # the same directory, then call ``load_binding`` without passing
  # an ontology explicitly — the loader should find the companion,
  # parse it, and validate the binding against it.
  (tmp_path / "finance.ontology.yaml").write_text(
      textwrap.dedent(ONTOLOGY).lstrip(), encoding="utf-8"
  )
  binding_file = tmp_path / "finance-bq-prod.binding.yaml"
  binding_file.write_text(
      textwrap.dedent(
          """
          binding: finance-bq-prod
          ontology: finance
          target: {backend: bigquery, project: p, dataset: d}
          entities:
            - name: Account
              source: raw.accounts
              properties:
                - name: account_id
                  column: acct_id
          """
      ).lstrip(),
      encoding="utf-8",
  )
  b = load_binding(binding_file)
  assert b.binding == "finance-bq-prod"


def test_load_binding_raises_when_companion_ontology_missing(tmp_path):
  # Binding points at ``nowhere``, but we never create a
  # ``nowhere.ontology.yaml`` next to it — the loader should raise a
  # FileNotFoundError that names the missing ontology, not a generic
  # OSError.
  binding_file = tmp_path / "b.binding.yaml"
  binding_file.write_text(
      textwrap.dedent(
          """
          binding: b
          ontology: nowhere
          target: {backend: bigquery, project: p, dataset: d}
          """
      ).lstrip(),
      encoding="utf-8",
  )
  with pytest.raises(FileNotFoundError, match="nowhere"):
    load_binding(binding_file)


def test_load_binding_accepts_explicit_ontology(tmp_path):
  # The inverse of the auto-discovery path: no companion file exists
  # in ``tmp_path``, but the caller passes a pre-loaded ``Ontology``
  # directly, so the loader should skip discovery entirely. This is
  # the path a CLI would take when the ontology was loaded from an
  # explicit flag rather than a sibling file.
  binding_file = tmp_path / "b.binding.yaml"
  binding_file.write_text(
      textwrap.dedent(
          """
          binding: b
          ontology: finance
          target: {backend: bigquery, project: p, dataset: d}
          entities:
            - name: Account
              source: t
              properties:
                - name: account_id
                  column: c
          """
      ).lstrip(),
      encoding="utf-8",
  )
  load_binding(binding_file, ontology=_ontology())
