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

"""Shape-level tests for the v0 binding pydantic models.

Style mirrors ``test_ontology_models.py``: each test embeds the entire
YAML input as a literal string and asserts against either

  - the entire round-tripped JSON dump of the parsed ``Binding``, or
  - the entire error message raised by validation.

Scope is shape only — required fields, enum membership, unknown-key
rejection, and list min-length constraints. Cross-ontology semantics
(property coverage, endpoint arity, derived-property exclusion,
partial-binding closure, type compat) belong to the future binding
loader and are **not** tested here.
"""

from __future__ import annotations

import json
import textwrap

from pydantic import ValidationError
import pytest
import yaml

from bigquery_ontology import Binding


def _load(yaml_text: str) -> Binding:
  return Binding(**yaml.safe_load(textwrap.dedent(yaml_text).lstrip()))


def _dump(binding: Binding) -> str:
  return json.dumps(
      binding.model_dump(
          by_alias=True, exclude_none=True, exclude_defaults=True
      ),
      indent=2,
      sort_keys=False,
  )


# --------------------------------------------------------------------- #
# Valid bindings — full JSON round-trip                                  #
# --------------------------------------------------------------------- #


def test_minimal_binding_round_trips_to_exact_json():
  yaml_input = """
    binding: empty
    ontology: x
    target:
      backend: bigquery
      project: p
      dataset: d
  """
  expected_json = textwrap.dedent(
      """\
    {
      "binding": "empty",
      "ontology": "x",
      "target": {
        "backend": "bigquery",
        "project": "p",
        "dataset": "d"
      }
    }"""
  )

  assert _dump(_load(yaml_input)) == expected_json


def test_finance_example_round_trips_to_exact_json():
  """The BigQuery example from the binding design doc."""
  yaml_input = """
    binding: finance-bq-prod
    ontology: finance
    target:
      backend: bigquery
      project: my-proj
      dataset: finance

    entities:
      - name: Person
        source: raw.persons
        properties:
          - name: party_id
            column: person_id
          - name: name
            column: display_name

    relationships:
      - name: HOLDS
        source: raw.holdings
        from_columns:
          - account_id
        to_columns:
          - security_id
        properties:
          - name: as_of
            column: snapshot_date
          - name: quantity
            column: qty
  """
  expected_json = textwrap.dedent(
      """\
    {
      "binding": "finance-bq-prod",
      "ontology": "finance",
      "target": {
        "backend": "bigquery",
        "project": "my-proj",
        "dataset": "finance"
      },
      "entities": [
        {
          "name": "Person",
          "source": "raw.persons",
          "properties": [
            {
              "name": "party_id",
              "column": "person_id"
            },
            {
              "name": "name",
              "column": "display_name"
            }
          ]
        }
      ],
      "relationships": [
        {
          "name": "HOLDS",
          "source": "raw.holdings",
          "from_columns": [
            "account_id"
          ],
          "to_columns": [
            "security_id"
          ],
          "properties": [
            {
              "name": "as_of",
              "column": "snapshot_date"
            },
            {
              "name": "quantity",
              "column": "qty"
            }
          ]
        }
      ]
    }"""
  )

  assert _dump(_load(yaml_input)) == expected_json


def test_composite_key_columns_round_trip():
  yaml_input = """
    binding: composite
    ontology: x
    target:
      backend: bigquery
      project: p
      dataset: d
    relationships:
      - name: R
        source: edges
        from_columns: [tenant_id, account_id]
        to_columns: [tenant_id, security_id]
  """
  expected_json = textwrap.dedent(
      """\
    {
      "binding": "composite",
      "ontology": "x",
      "target": {
        "backend": "bigquery",
        "project": "p",
        "dataset": "d"
      },
      "relationships": [
        {
          "name": "R",
          "source": "edges",
          "from_columns": [
            "tenant_id",
            "account_id"
          ],
          "to_columns": [
            "tenant_id",
            "security_id"
          ]
        }
      ]
    }"""
  )

  assert _dump(_load(yaml_input)) == expected_json


def test_entity_with_empty_properties_round_trips():
  """Coverage against the ontology is a loader concern; shape accepts []."""
  yaml_input = """
    binding: b
    ontology: x
    target:
      backend: bigquery
      project: p
      dataset: d
    entities:
      - name: E
        source: t
        properties: []
  """
  expected_json = textwrap.dedent(
      """\
    {
      "binding": "b",
      "ontology": "x",
      "target": {
        "backend": "bigquery",
        "project": "p",
        "dataset": "d"
      },
      "entities": [
        {
          "name": "E",
          "source": "t",
          "properties": []
        }
      ]
    }"""
  )

  assert _dump(_load(yaml_input)) == expected_json


# --------------------------------------------------------------------- #
# Shape errors — pydantic ValidationError with key-name substring check  #
# --------------------------------------------------------------------- #


def _assert_validation_error(yaml_text: str, must_contain: str) -> None:
  with pytest.raises(ValidationError) as exc_info:
    _load(yaml_text)
  assert must_contain in str(exc_info.value)


def test_entity_missing_properties_is_error():
  yaml_input = """
    binding: b
    ontology: x
    target: {backend: bigquery, project: p, dataset: d}
    entities:
      - name: E
        source: t
  """
  _assert_validation_error(yaml_input, "properties")


def test_missing_top_level_binding_is_error():
  yaml_input = """
    ontology: x
    target: {backend: bigquery, project: p, dataset: d}
  """
  _assert_validation_error(yaml_input, "binding")


def test_missing_top_level_ontology_is_error():
  yaml_input = """
    binding: b
    target: {backend: bigquery, project: p, dataset: d}
  """
  _assert_validation_error(yaml_input, "ontology")


def test_unknown_top_level_key_is_error():
  yaml_input = """
    binding: b
    ontology: x
    target: {backend: bigquery, project: p, dataset: d}
    bogus: nope
  """
  _assert_validation_error(yaml_input, "bogus")


def test_unknown_target_key_is_error():
  yaml_input = """
    binding: b
    ontology: x
    target:
      backend: bigquery
      project: p
      dataset: d
      region: US
  """
  _assert_validation_error(yaml_input, "region")


def test_unsupported_backend_is_error():
  yaml_input = """
    binding: b
    ontology: x
    target: {backend: spanner, instance: i, database: d}
  """
  _assert_validation_error(yaml_input, "spanner")


def test_missing_bigquery_target_fields_is_error():
  yaml_input = """
    binding: b
    ontology: x
    target: {backend: bigquery}
  """
  _assert_validation_error(yaml_input, "project")


def test_unknown_property_binding_key_is_error():
  yaml_input = """
    binding: b
    ontology: x
    target: {backend: bigquery, project: p, dataset: d}
    entities:
      - name: E
        source: t
        properties:
          - name: x
            column: y
            cast: STRING
  """
  _assert_validation_error(yaml_input, "cast")


def test_empty_from_columns_is_error():
  yaml_input = """
    binding: b
    ontology: x
    target: {backend: bigquery, project: p, dataset: d}
    relationships:
      - name: R
        source: t
        from_columns: []
        to_columns: [c]
  """
  _assert_validation_error(yaml_input, "from_columns")


def test_empty_to_columns_is_error():
  yaml_input = """
    binding: b
    ontology: x
    target: {backend: bigquery, project: p, dataset: d}
    relationships:
      - name: R
        source: t
        from_columns: [c]
        to_columns: []
  """
  _assert_validation_error(yaml_input, "to_columns")
