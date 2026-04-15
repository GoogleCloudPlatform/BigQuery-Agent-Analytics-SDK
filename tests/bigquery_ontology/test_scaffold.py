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

"""Tests for the scaffold generator in ``src/bigquery_ontology/scaffold.py``."""

from __future__ import annotations

import textwrap

import pytest

from bigquery_ontology import load_binding_from_string
from bigquery_ontology import load_ontology_from_string
from bigquery_ontology import scaffold
from bigquery_ontology.ontology_models import PropertyType
from bigquery_ontology.scaffold import _ONTOLOGY_TO_BQ_TYPE
from bigquery_ontology.scaffold import _to_snake_case

# ===================================================================== #
# Step 1 — snake-case converter                                         #
# ===================================================================== #


class TestToSnakeCase:

  @pytest.mark.parametrize(
      "input_name, expected",
      [
          ("Person", "person"),
          ("firstName", "first_name"),
          ("HTTPRequest", "http_request"),
          ("already_snake", "already_snake"),
          ("FollowsRelation", "follows_relation"),
          ("AccountDay", "account_day"),
          ("XMLParser", "xml_parser"),
          ("parseJSON", "parse_json"),
          ("A", "a"),
          ("myURL", "my_url"),
          ("lowercase", "lowercase"),
      ],
  )
  def test_snake_case_conversion(self, input_name, expected):
    assert _to_snake_case(input_name) == expected


class TestTypeMap:

  def test_all_property_types_covered(self):
    for pt in PropertyType:
      assert pt in _ONTOLOGY_TO_BQ_TYPE, f"Missing mapping for {pt}"

  @pytest.mark.parametrize(
      "ontology_type, bq_type",
      [
          (PropertyType.INTEGER, "INT64"),
          (PropertyType.DOUBLE, "FLOAT64"),
          (PropertyType.BOOLEAN, "BOOL"),
      ],
  )
  def test_non_trivial_mappings(self, ontology_type, bq_type):
    assert _ONTOLOGY_TO_BQ_TYPE[ontology_type] == bq_type


# ===================================================================== #
# Step 2 — entity table DDL                                             #
# ===================================================================== #

_PERSON_ONTOLOGY = """
  ontology: test
  entities:
    - name: Person
      keys: {primary: [party_id]}
      properties:
        - {name: party_id, type: string}
        - {name: name, type: string}
        - {name: dob, type: date}
"""

_COMPOUND_KEY_ONTOLOGY = """
  ontology: test
  entities:
    - name: AccountDay
      keys: {primary: [account_id, as_of]}
      properties:
        - {name: account_id, type: string}
        - {name: as_of, type: date}
        - {name: balance, type: numeric}
"""

_DERIVED_PROP_ONTOLOGY = """
  ontology: test
  entities:
    - name: Person
      keys: {primary: [person_id]}
      properties:
        - {name: person_id, type: string}
        - {name: first_name, type: string}
        - {name: last_name, type: string}
        - name: full_name
          type: string
          expr: "first_name || ' ' || last_name"
"""


class TestEntityDDL:

  def test_single_pk_entity(self):
    ontology = load_ontology_from_string(textwrap.dedent(_PERSON_ONTOLOGY))
    ddl, _ = scaffold(ontology, dataset="my_dataset", project="p")
    expected = textwrap.dedent(
        """\
        CREATE TABLE `p.my_dataset.person` (
          party_id  STRING NOT NULL,
          name      STRING,
          dob       DATE,
          PRIMARY KEY (party_id) NOT ENFORCED
        );
    """
    )
    assert ddl == expected

  def test_compound_pk_entity(self):
    ontology = load_ontology_from_string(
        textwrap.dedent(_COMPOUND_KEY_ONTOLOGY)
    )
    ddl, _ = scaffold(ontology, dataset="my_dataset", project="p")
    expected = textwrap.dedent(
        """\
        CREATE TABLE `p.my_dataset.account_day` (
          account_id  STRING NOT NULL,
          as_of       DATE NOT NULL,
          balance     NUMERIC,
          PRIMARY KEY (account_id, as_of) NOT ENFORCED
        );
    """
    )
    assert ddl == expected

  def test_derived_properties_excluded(self):
    ontology = load_ontology_from_string(
        textwrap.dedent(_DERIVED_PROP_ONTOLOGY)
    )
    ddl, _ = scaffold(ontology, dataset="ds", project="p")
    assert "full_name" not in ddl

  def test_preserve_naming(self):
    ontology = load_ontology_from_string(textwrap.dedent(_PERSON_ONTOLOGY))
    ddl, _ = scaffold(
        ontology, dataset="my_dataset", project="p", naming="preserve"
    )
    assert "`p.my_dataset.Person`" in ddl
    assert "party_id" in ddl and "STRING NOT NULL" in ddl

  def test_project_qualified(self):
    ontology = load_ontology_from_string(textwrap.dedent(_PERSON_ONTOLOGY))
    ddl, _ = scaffold(ontology, dataset="ds", project="proj")
    assert "`proj.ds.person`" in ddl


# ===================================================================== #
# Step 3 — relationship table DDL                                       #
# ===================================================================== #

_FOLLOWS_ONTOLOGY = """
  ontology: test
  entities:
    - name: Person
      keys: {primary: [party_id]}
      properties:
        - {name: party_id, type: string}
        - {name: name, type: string}
        - {name: dob, type: date}
  relationships:
    - name: Follows
      from: Person
      to: Person
      properties:
        - {name: since, type: date}
"""

_HOLDING_ONTOLOGY = """
  ontology: test
  entities:
    - name: Account
      keys: {primary: [account_id, as_of]}
      properties:
        - {name: account_id, type: string}
        - {name: as_of, type: date}
    - name: Security
      keys: {primary: [isin]}
      properties:
        - {name: isin, type: string}
  relationships:
    - name: Holding
      from: Account
      to: Security
      keys: {additional: [as_of]}
      properties:
        - {name: as_of, type: date}
        - {name: quantity, type: numeric}
"""

_REL_OWN_PK_ONTOLOGY = """
  ontology: test
  entities:
    - name: Person
      keys: {primary: [person_id]}
      properties:
        - {name: person_id, type: string}
  relationships:
    - name: Transfer
      from: Person
      to: Person
      keys: {primary: [transfer_id]}
      properties:
        - {name: transfer_id, type: string}
        - {name: amount, type: numeric}
"""

_COLLISION_ONTOLOGY = """
  ontology: test
  entities:
    - name: Person
      keys: {primary: [party_id]}
      properties:
        - {name: party_id, type: string}
  relationships:
    - name: Follows
      from: Person
      to: Person
      properties:
        - {name: from_party_id, type: string}
"""

_REL_DERIVED_ONTOLOGY = """
  ontology: test
  entities:
    - name: Person
      keys: {primary: [person_id]}
      properties:
        - {name: person_id, type: string}
  relationships:
    - name: Knows
      from: Person
      to: Person
      properties:
        - {name: since, type: date}
        - name: label
          type: string
          expr: "'KNOWS'"
"""


class TestRelationshipDDL:

  def test_no_keys_relationship(self):
    ontology = load_ontology_from_string(textwrap.dedent(_FOLLOWS_ONTOLOGY))
    ddl, _ = scaffold(ontology, dataset="my_dataset", project="p")
    expected_rel = textwrap.dedent(
        """\
        CREATE TABLE `p.my_dataset.follows` (
          from_party_id  STRING NOT NULL,
          to_party_id    STRING NOT NULL,
          since          DATE,
          -- TODO: uncomment if (from_party_id, to_party_id) is unique per row
          -- PRIMARY KEY (from_party_id, to_party_id) NOT ENFORCED,
          FOREIGN KEY (from_party_id) REFERENCES `p.my_dataset.person`(party_id) NOT ENFORCED,
          FOREIGN KEY (to_party_id) REFERENCES `p.my_dataset.person`(party_id) NOT ENFORCED
        );
    """
    )
    assert expected_rel in ddl

  def test_keys_additional_relationship(self):
    ontology = load_ontology_from_string(textwrap.dedent(_HOLDING_ONTOLOGY))
    ddl, _ = scaffold(ontology, dataset="my_dataset", project="p")
    expected_rel = textwrap.dedent(
        """\
        CREATE TABLE `p.my_dataset.holding` (
          from_account_id  STRING NOT NULL,
          from_as_of       DATE NOT NULL,
          to_isin          STRING NOT NULL,
          as_of            DATE NOT NULL,
          quantity         NUMERIC,
          PRIMARY KEY (from_account_id, from_as_of, to_isin, as_of) NOT ENFORCED,
          FOREIGN KEY (from_account_id, from_as_of) REFERENCES `p.my_dataset.account`(account_id, as_of) NOT ENFORCED,
          FOREIGN KEY (to_isin) REFERENCES `p.my_dataset.security`(isin) NOT ENFORCED
        );
    """
    )
    assert expected_rel in ddl

  def test_keys_primary_relationship(self):
    ontology = load_ontology_from_string(textwrap.dedent(_REL_OWN_PK_ONTOLOGY))
    ddl, _ = scaffold(ontology, dataset="ds", project="p")
    assert "transfer_id     STRING NOT NULL" in ddl
    assert "PRIMARY KEY (transfer_id) NOT ENFORCED" in ddl
    assert "from_person_id  STRING NOT NULL" in ddl
    assert "to_person_id    STRING NOT NULL" in ddl

  def test_endpoint_column_collision(self):
    ontology = load_ontology_from_string(textwrap.dedent(_COLLISION_ONTOLOGY))
    with pytest.raises(ValueError, match="collides with a generated endpoint"):
      scaffold(ontology, dataset="ds", project="p")

  def test_derived_properties_excluded_from_rel(self):
    ontology = load_ontology_from_string(textwrap.dedent(_REL_DERIVED_ONTOLOGY))
    ddl, _ = scaffold(ontology, dataset="ds", project="p")
    assert "label" not in ddl
    assert "since" in ddl and "DATE" in ddl

  def test_no_keys_emits_suggested_pk_comment(self):
    ontology = load_ontology_from_string(textwrap.dedent(_FOLLOWS_ONTOLOGY))
    ddl, _ = scaffold(ontology, dataset="ds", project="p")
    assert "-- TODO: uncomment if (from_party_id, to_party_id) is unique" in ddl
    assert "-- PRIMARY KEY (from_party_id, to_party_id) NOT ENFORCED" in ddl

  def test_keys_primary_has_no_suggested_pk_comment(self):
    ontology = load_ontology_from_string(textwrap.dedent(_REL_OWN_PK_ONTOLOGY))
    ddl, _ = scaffold(ontology, dataset="ds", project="p")
    assert "-- TODO" not in ddl
    assert "-- PRIMARY KEY" not in ddl

  def test_keys_additional_has_no_suggested_pk_comment(self):
    ontology = load_ontology_from_string(textwrap.dedent(_HOLDING_ONTOLOGY))
    ddl, _ = scaffold(ontology, dataset="ds", project="p")
    assert "-- TODO" not in ddl
    assert "-- PRIMARY KEY" not in ddl


# ===================================================================== #
# Step 4 — binding YAML + public scaffold()                             #
# ===================================================================== #


class TestBindingYAML:

  def test_person_follows_binding(self):
    ontology = load_ontology_from_string(textwrap.dedent(_FOLLOWS_ONTOLOGY))
    _, binding = scaffold(ontology, dataset="my_dataset", project="p")
    expected = textwrap.dedent(
        """\
        # Generated by gm scaffold. This file is user-owned \u2014 edit freely.
        binding: my_dataset
        ontology: test
        target:
          backend: bigquery
          project: p
          dataset: my_dataset
        entities:
          - name: Person
            source: p.my_dataset.person
            properties:
              - {name: party_id, column: party_id}
              - {name: name, column: name}
              - {name: dob, column: dob}
        relationships:
          - name: Follows
            source: p.my_dataset.follows
            from_columns: [from_party_id]
            to_columns: [to_party_id]
            properties:
              - {name: since, column: since}
    """
    )
    assert binding == expected

  def test_binding_round_trip(self):
    ontology = load_ontology_from_string(textwrap.dedent(_FOLLOWS_ONTOLOGY))
    _, binding_text = scaffold(
        ontology, dataset="my_dataset", project="my_project"
    )
    binding = load_binding_from_string(binding_text, ontology=ontology)
    assert binding.binding == "my_dataset"
    assert binding.ontology == "test"
    assert len(binding.entities) == 1
    assert len(binding.relationships) == 1

  def test_binding_with_project(self):
    ontology = load_ontology_from_string(textwrap.dedent(_PERSON_ONTOLOGY))
    _, binding = scaffold(ontology, dataset="ds", project="proj")
    assert "project: proj" in binding
    assert "source: proj.ds.person" in binding

  def test_derived_excluded_from_binding(self):
    ontology = load_ontology_from_string(
        textwrap.dedent(_DERIVED_PROP_ONTOLOGY)
    )
    _, binding = scaffold(ontology, dataset="ds", project="p")
    assert "full_name" not in binding

  def test_entities_only_no_relationships(self):
    ontology = load_ontology_from_string(textwrap.dedent(_PERSON_ONTOLOGY))
    ddl, binding = scaffold(ontology, dataset="ds", project="p")
    assert "relationships:" not in binding
    assert "FOREIGN KEY" not in ddl

  def test_determinism(self):
    ontology = load_ontology_from_string(textwrap.dedent(_FOLLOWS_ONTOLOGY))
    ddl1, b1 = scaffold(ontology, dataset="ds", project="p")
    ddl2, b2 = scaffold(ontology, dataset="ds", project="p")
    assert ddl1 == ddl2
    assert b1 == b2


class TestRejectExtends:

  def test_entity_extends_rejected(self):
    yaml = textwrap.dedent(
        """
      ontology: test
      entities:
        - name: Party
          keys: {primary: [party_id]}
          properties:
            - {name: party_id, type: string}
        - name: Person
          extends: Party
          properties:
            - {name: name, type: string}
    """
    )
    ontology = load_ontology_from_string(yaml)
    with pytest.raises(ValueError, match="extends"):
      scaffold(ontology, dataset="ds", project="p")

  def test_relationship_extends_rejected(self):
    yaml = textwrap.dedent(
        """
      ontology: test
      entities:
        - name: Person
          keys: {primary: [pid]}
          properties:
            - {name: pid, type: string}
      relationships:
        - name: Knows
          from: Person
          to: Person
          properties:
            - {name: since, type: date}
        - name: KnowsWell
          extends: Knows
          from: Person
          to: Person
          properties:
            - {name: depth, type: integer}
    """
    )
    ontology = load_ontology_from_string(yaml)
    with pytest.raises(ValueError, match="extends"):
      scaffold(ontology, dataset="ds", project="p")


class TestRejectDerivedKeys:

  def test_entity_derived_primary_key_rejected(self):
    yaml = textwrap.dedent(
        """
      ontology: test
      entities:
        - name: Person
          keys: {primary: [computed_id]}
          properties:
            - {name: first_name, type: string}
            - {name: last_name, type: string}
            - name: computed_id
              type: string
              expr: "first_name || '_' || last_name"
    """
    )
    ontology = load_ontology_from_string(yaml)
    with pytest.raises(ValueError, match="derived property"):
      scaffold(ontology, dataset="ds", project="p")

  def test_rel_derived_primary_key_rejected(self):
    yaml = textwrap.dedent(
        """
      ontology: test
      entities:
        - name: Person
          keys: {primary: [pid]}
          properties:
            - {name: pid, type: string}
      relationships:
        - name: Knows
          from: Person
          to: Person
          keys: {primary: [rid]}
          properties:
            - {name: since, type: date}
            - name: rid
              type: string
              expr: "'computed'"
    """
    )
    ontology = load_ontology_from_string(yaml)
    with pytest.raises(ValueError, match="derived property"):
      scaffold(ontology, dataset="ds", project="p")

  def test_rel_derived_additional_key_rejected(self):
    yaml = textwrap.dedent(
        """
      ontology: test
      entities:
        - name: Account
          keys: {primary: [account_id]}
          properties:
            - {name: account_id, type: string}
        - name: Security
          keys: {primary: [isin]}
          properties:
            - {name: isin, type: string}
      relationships:
        - name: Holding
          from: Account
          to: Security
          keys: {additional: [as_of]}
          properties:
            - name: as_of
              type: date
              expr: "CURRENT_DATE()"
            - {name: quantity, type: numeric}
    """
    )
    ontology = load_ontology_from_string(yaml)
    with pytest.raises(ValueError, match="derived property"):
      scaffold(ontology, dataset="ds", project="p")
