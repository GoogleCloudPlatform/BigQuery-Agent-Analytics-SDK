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

"""Tests for the v0 ontology spec implementation in ``src/ontology``.

These tests favor **full-text** input/output comparisons over field-by-field
assertions. Each test embeds the entire YAML input as a literal string and
asserts against either:

  - the entire round-tripped JSON dump of the parsed ``Ontology``, or
  - the entire error message raised by validation.

That way the test reads top-to-bottom as "given this exact YAML, you get
this exact result."
"""

from __future__ import annotations

import json
import textwrap

import pytest

from bigquery_ontology import load_ontology_from_string


def _load(yaml_text: str):
  return load_ontology_from_string(textwrap.dedent(yaml_text).lstrip())


def _dump(ont) -> str:
  return json.dumps(
      ont.model_dump(by_alias=True, exclude_none=True, exclude_defaults=True),
      indent=2,
      sort_keys=False,
  )


# --------------------------------------------------------------------- #
# Valid ontologies — full JSON round-trip                                #
# --------------------------------------------------------------------- #


def test_minimal_ontology_round_trips_to_exact_json():
  yaml_input = """
    ontology: tiny
    entities:
      - name: Thing
        keys:
          primary:
            - id
        properties:
          - name: id
            type: string
  """
  expected_json = textwrap.dedent(
      """\
    {
      "ontology": "tiny",
      "entities": [
        {
          "name": "Thing",
          "keys": {
            "primary": [
              "id"
            ]
          },
          "properties": [
            {
              "name": "id",
              "type": "string"
            }
          ]
        }
      ]
    }"""
  )

  assert _dump(_load(yaml_input)) == expected_json


def test_finance_spec_example_round_trips_to_exact_json():
  """The exact example from §2 of ontology.md."""
  yaml_input = """
    ontology: finance
    version: 0.1

    entities:
      - name: Party
        keys:
          primary:
            - party_id
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

      - name: Organization
        extends: Party
        properties:
          - name: tax_id
            type: string

      - name: Account
        keys:
          primary:
            - account_id
        properties:
          - name: account_id
            type: string
          - name: opened_at
            type: timestamp

      - name: Security
        keys:
          primary:
            - security_id
        properties:
          - name: security_id
            type: string

    relationships:
      - name: HOLDS
        keys:
          additional:
            - as_of
        from: Account
        to: Security
        cardinality: many_to_many
        properties:
          - name: as_of
            type: timestamp
          - name: quantity
            type: double

      - name: TRANSFER
        keys:
          primary:
            - transaction_id
        from: Account
        to: Account
        properties:
          - name: transaction_id
            type: string
          - name: amount
            type: double
          - name: executed_at
            type: timestamp

      - name: RELATED_TO
        from: Party
        to: Party

    description: Party, account, and security model for finance domain.
    synonyms:
      - finance-core
  """
  expected_json = textwrap.dedent(
      """\
    {
      "ontology": "finance",
      "version": "0.1",
      "entities": [
        {
          "name": "Party",
          "keys": {
            "primary": [
              "party_id"
            ]
          },
          "properties": [
            {
              "name": "party_id",
              "type": "string"
            },
            {
              "name": "name",
              "type": "string"
            }
          ]
        },
        {
          "name": "Person",
          "extends": "Party",
          "properties": [
            {
              "name": "dob",
              "type": "date"
            },
            {
              "name": "first_name",
              "type": "string"
            },
            {
              "name": "last_name",
              "type": "string"
            },
            {
              "name": "full_name",
              "type": "string",
              "expr": "first_name || ' ' || last_name"
            }
          ]
        },
        {
          "name": "Organization",
          "extends": "Party",
          "properties": [
            {
              "name": "tax_id",
              "type": "string"
            }
          ]
        },
        {
          "name": "Account",
          "keys": {
            "primary": [
              "account_id"
            ]
          },
          "properties": [
            {
              "name": "account_id",
              "type": "string"
            },
            {
              "name": "opened_at",
              "type": "timestamp"
            }
          ]
        },
        {
          "name": "Security",
          "keys": {
            "primary": [
              "security_id"
            ]
          },
          "properties": [
            {
              "name": "security_id",
              "type": "string"
            }
          ]
        }
      ],
      "relationships": [
        {
          "name": "HOLDS",
          "keys": {
            "additional": [
              "as_of"
            ]
          },
          "from": "Account",
          "to": "Security",
          "cardinality": "many_to_many",
          "properties": [
            {
              "name": "as_of",
              "type": "timestamp"
            },
            {
              "name": "quantity",
              "type": "double"
            }
          ]
        },
        {
          "name": "TRANSFER",
          "keys": {
            "primary": [
              "transaction_id"
            ]
          },
          "from": "Account",
          "to": "Account",
          "properties": [
            {
              "name": "transaction_id",
              "type": "string"
            },
            {
              "name": "amount",
              "type": "double"
            },
            {
              "name": "executed_at",
              "type": "timestamp"
            }
          ]
        },
        {
          "name": "RELATED_TO",
          "from": "Party",
          "to": "Party"
        }
      ],
      "description": "Party, account, and security model for finance domain.",
      "synonyms": [
        "finance-core"
      ]
    }"""
  )

  assert _dump(_load(yaml_input)) == expected_json


def test_alternate_keys_round_trip():
  yaml_input = """
    ontology: alts
    entities:
      - name: User
        keys:
          primary:
            - user_id
          alternate:
            - [email]
            - [tenant_id, external_id]
        properties:
          - name: user_id
            type: string
          - name: email
            type: string
          - name: tenant_id
            type: string
          - name: external_id
            type: string
  """
  expected_json = textwrap.dedent(
      """\
    {
      "ontology": "alts",
      "entities": [
        {
          "name": "User",
          "keys": {
            "primary": [
              "user_id"
            ],
            "alternate": [
              [
                "email"
              ],
              [
                "tenant_id",
                "external_id"
              ]
            ]
          },
          "properties": [
            {
              "name": "user_id",
              "type": "string"
            },
            {
              "name": "email",
              "type": "string"
            },
            {
              "name": "tenant_id",
              "type": "string"
            },
            {
              "name": "external_id",
              "type": "string"
            }
          ]
        }
      ]
    }"""
  )

  assert _dump(_load(yaml_input)) == expected_json


def test_relationship_without_keys_allows_multi_edges():
  yaml_input = """
    ontology: graph
    entities:
      - name: A
        keys:
          primary: [id]
        properties:
          - name: id
            type: string
      - name: B
        keys:
          primary: [id]
        properties:
          - name: id
            type: string
    relationships:
      - name: TOUCHES
        from: A
        to: B
  """
  ont = _load(yaml_input)
  assert ont.relationships[0].keys is None  # multi-edges permitted (§6).


# --------------------------------------------------------------------- #
# Validation errors — full message comparisons                           #
# --------------------------------------------------------------------- #


def _assert_value_error(yaml_text: str, expected_message: str) -> None:
  with pytest.raises(ValueError) as exc_info:
    _load(yaml_text)
  assert str(exc_info.value) == expected_message


def test_duplicate_entity_name_is_error():
  yaml_input = """
    ontology: bad
    entities:
      - name: Thing
        keys: {primary: [id]}
        properties: [{name: id, type: string}]
      - name: Thing
        keys: {primary: [id]}
        properties: [{name: id, type: string}]
  """
  _assert_value_error(yaml_input, "Duplicate entity name: 'Thing'")


def test_duplicate_relationship_name_is_error():
  yaml_input = """
    ontology: bad
    entities:
      - name: A
        keys: {primary: [id]}
        properties: [{name: id, type: string}]
    relationships:
      - name: R
        from: A
        to: A
      - name: R
        from: A
        to: A
  """
  _assert_value_error(yaml_input, "Duplicate relationship name: 'R'")


def test_duplicate_property_name_is_error():
  yaml_input = """
    ontology: bad
    entities:
      - name: A
        keys: {primary: [id]}
        properties:
          - name: id
            type: string
          - name: id
            type: string
  """
  _assert_value_error(yaml_input, "Duplicate property name 'id' on entity 'A'.")


def test_extends_unknown_entity_is_error():
  yaml_input = """
    ontology: bad
    entities:
      - name: Child
        extends: Ghost
        keys: {primary: [id]}
        properties: [{name: id, type: string}]
  """
  _assert_value_error(
      yaml_input,
      "Entity 'Child' extends 'Ghost', which is not a declared entity.",
  )


def test_extends_cycle_is_error():
  yaml_input = """
    ontology: bad
    entities:
      - name: A
        extends: B
        keys: {primary: [id]}
        properties: [{name: id, type: string}]
      - name: B
        extends: A
        keys: {primary: [id]}
        properties: [{name: id, type: string}]
  """
  _assert_value_error(yaml_input, "Cycle in entity extends chain at 'A'.")


def test_redeclaring_inherited_property_is_error():
  yaml_input = """
    ontology: bad
    entities:
      - name: Parent
        keys: {primary: [id]}
        properties:
          - name: id
            type: string
          - name: name
            type: string
      - name: Child
        extends: Parent
        properties:
          - name: name
            type: string
  """
  _assert_value_error(
      yaml_input,
      "Entity 'Child' redeclares inherited property 'name'.",
  )


def test_redeclaring_inherited_keys_is_error():
  yaml_input = """
    ontology: bad
    entities:
      - name: Parent
        keys: {primary: [id]}
        properties: [{name: id, type: string}]
      - name: Child
        extends: Parent
        keys: {primary: [id]}
  """
  _assert_value_error(yaml_input, "Entity 'Child' redeclares inherited keys.")


def test_key_column_must_be_a_property():
  yaml_input = """
    ontology: bad
    entities:
      - name: A
        keys: {primary: [missing]}
        properties:
          - name: id
            type: string
  """
  _assert_value_error(
      yaml_input,
      "Entity 'A': key column 'missing' is not a declared property.",
  )


def test_alternate_key_duplicates_primary_is_error():
  yaml_input = """
    ontology: bad
    entities:
      - name: A
        keys:
          primary: [id]
          alternate:
            - [id]
        properties:
          - name: id
            type: string
  """
  _assert_value_error(
      yaml_input,
      "Entity 'A': alternate key ['id'] duplicates another key.",
  )


def test_entity_additional_keys_forbidden():
  yaml_input = """
    ontology: bad
    entities:
      - name: A
        keys:
          additional: [id]
        properties:
          - name: id
            type: string
  """
  _assert_value_error(yaml_input, "Entity 'A': keys.primary is required.")


def test_relationship_primary_and_additional_mutually_exclusive():
  yaml_input = """
    ontology: bad
    entities:
      - name: A
        keys: {primary: [id]}
        properties: [{name: id, type: string}]
    relationships:
      - name: R
        from: A
        to: A
        keys:
          primary: [tx]
          additional: [as_of]
        properties:
          - name: tx
            type: string
          - name: as_of
            type: timestamp
  """
  _assert_value_error(
      yaml_input,
      "Relationship 'R': primary and additional are mutually exclusive.",
  )


def test_relationship_endpoint_must_be_declared_entity():
  yaml_input = """
    ontology: bad
    entities:
      - name: A
        keys: {primary: [id]}
        properties: [{name: id, type: string}]
    relationships:
      - name: R
        from: A
        to: Ghost
  """
  _assert_value_error(
      yaml_input,
      "Relationship 'R': to 'Ghost' is not a declared entity.",
  )


def test_covariant_narrowing_violation_is_error():
  yaml_input = """
    ontology: bad
    entities:
      - name: Party
        keys: {primary: [id]}
        properties: [{name: id, type: string}]
      - name: Person
        extends: Party
        properties: [{name: dob, type: date}]
      - name: Account
        keys: {primary: [id]}
        properties: [{name: id, type: string}]
    relationships:
      - name: memberOf
        from: Party
        to: Party
      - name: alumni
        extends: memberOf
        from: Person
        to: Account
  """
  _assert_value_error(
      yaml_input,
      "Relationship 'alumni': to 'Account' does not narrow parent to 'Party'.",
  )


def test_unknown_top_level_key_is_error():
  yaml_input = """
    ontology: bad
    entities:
      - name: A
        keys: {primary: [id]}
        properties: [{name: id, type: string}]
    nonsense: 1
  """
  with pytest.raises(Exception) as exc_info:
    _load(yaml_input)
  # Pydantic raises ValidationError; just confirm the offending key is named.
  assert "nonsense" in str(exc_info.value)


def test_unknown_property_type_is_error():
  yaml_input = """
    ontology: bad
    entities:
      - name: A
        keys: {primary: [id]}
        properties:
          - name: id
            type: uuid
  """
  with pytest.raises(Exception) as exc_info:
    _load(yaml_input)
  assert "uuid" in str(exc_info.value)


def test_empty_primary_key_list_is_error():
  yaml_input = """
    ontology: bad
    entities:
      - name: A
        keys: {primary: []}
        properties: [{name: id, type: string}]
  """
  with pytest.raises(Exception) as exc_info:
    _load(yaml_input)
  msg = str(exc_info.value).lower()
  assert "primary" in msg and (
      "at least 1" in msg or "min_length" in msg or "too_short" in msg
  )


def test_relationship_cardinality_must_match_parent():
  yaml_input = """
    ontology: bad
    entities:
      - name: A
        keys: {primary: [id]}
        properties: [{name: id, type: string}]
      - name: B
        keys: {primary: [id]}
        properties: [{name: id, type: string}]
    relationships:
      - name: parent_rel
        from: A
        to: B
        cardinality: many_to_many
      - name: child_rel
        extends: parent_rel
        from: A
        to: B
        cardinality: one_to_one
  """
  with pytest.raises(ValueError, match="cardinality"):
    _load(yaml_input)


def test_relationship_child_may_omit_cardinality():
  yaml_input = """
    ontology: ok
    entities:
      - name: A
        keys: {primary: [id]}
        properties: [{name: id, type: string}]
      - name: B
        keys: {primary: [id]}
        properties: [{name: id, type: string}]
    relationships:
      - name: parent_rel
        from: A
        to: B
        cardinality: many_to_many
      - name: child_rel
        extends: parent_rel
        from: A
        to: B
  """
  # Should validate: child inherits parent cardinality silently.
  _load(yaml_input)


def test_entity_and_relationship_cannot_share_name():
  yaml_input = """
    ontology: bad
    entities:
      - name: Link
        keys: {primary: [id]}
        properties: [{name: id, type: string}]
      - name: A
        keys: {primary: [id]}
        properties: [{name: id, type: string}]
      - name: B
        keys: {primary: [id]}
        properties: [{name: id, type: string}]
    relationships:
      - name: Link
        from: A
        to: B
  """
  with pytest.raises(ValueError, match="unique within the ontology"):
    _load(yaml_input)
