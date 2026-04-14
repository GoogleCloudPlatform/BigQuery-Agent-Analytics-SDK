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

"""Unit tests for the scaffold generator in ``src/bigquery_ontology/scaffold.py``.

Each test builds an ontology from a YAML string, runs ``scaffold()``, and
asserts against the exact DDL and/or binding output.  Full-text comparisons
catch regressions in whitespace, ordering, and constraint syntax.
"""

from __future__ import annotations

import textwrap

import pytest
import yaml

from bigquery_ontology.loader import load_ontology_from_string
from bigquery_ontology.scaffold import NamingMode, scaffold


# ------------------------------------------------------------------ #
# Helpers                                                             #
# ------------------------------------------------------------------ #


def _ont(yaml_text: str):
    return load_ontology_from_string(textwrap.dedent(yaml_text).lstrip())


def _run(yaml_text: str, *, dataset="ds", project="proj", naming=NamingMode.SNAKE):
    ontology = _ont(yaml_text)
    return scaffold(ontology, dataset=dataset, project=project, naming=naming)


# ------------------------------------------------------------------ #
# Entity DDL — basic cases                                            #
# ------------------------------------------------------------------ #


def test_entity_single_pk_and_properties():
    ddl, _ = _run(
        """
        ontology: social
        entities:
          - name: Person
            keys:
              primary: [party_id]
            properties:
              - name: party_id
                type: string
              - name: name
                type: string
              - name: dob
                type: date
        """
    )
    assert ddl == (
        "CREATE TABLE `proj.ds.person` (\n"
        "  party_id STRING NOT NULL,\n"
        "  name STRING,\n"
        "  dob DATE,\n"
        "  PRIMARY KEY (party_id) NOT ENFORCED\n"
        ");\n"
    )


def test_entity_compound_pk():
    ddl, _ = _run(
        """
        ontology: finance
        entities:
          - name: AccountDay
            keys:
              primary: [account_id, as_of]
            properties:
              - name: account_id
                type: string
              - name: as_of
                type: date
              - name: balance
                type: numeric
        """
    )
    assert ddl == (
        "CREATE TABLE `proj.ds.account_day` (\n"
        "  account_id STRING NOT NULL,\n"
        "  as_of DATE NOT NULL,\n"
        "  balance NUMERIC,\n"
        "  PRIMARY KEY (account_id, as_of) NOT ENFORCED\n"
        ");\n"
    )


def test_entity_pk_columns_first_regardless_of_declaration_order():
    """PK columns are emitted first even if declared after non-key properties."""
    ddl, _ = _run(
        """
        ontology: o
        entities:
          - name: Thing
            keys:
              primary: [id]
            properties:
              - name: label
                type: string
              - name: id
                type: integer
        """
    )
    lines = ddl.splitlines()
    assert lines[1].strip().startswith("id INT64")
    assert lines[2].strip().startswith("label STRING")


def test_entity_zero_non_pk_properties():
    """An entity with only its PK property emits a table with one column."""
    ddl, _ = _run(
        """
        ontology: o
        entities:
          - name: Tag
            keys:
              primary: [tag_id]
            properties:
              - name: tag_id
                type: string
        """
    )
    assert ddl == (
        "CREATE TABLE `proj.ds.tag` (\n"
        "  tag_id STRING NOT NULL,\n"
        "  PRIMARY KEY (tag_id) NOT ENFORCED\n"
        ");\n"
    )


def test_entity_derived_properties_excluded_from_ddl():
    ddl, _ = _run(
        """
        ontology: o
        entities:
          - name: Item
            keys:
              primary: [id]
            properties:
              - name: id
                type: string
              - name: label
                type: string
                expr: id
        """
    )
    assert "label" not in ddl


# ------------------------------------------------------------------ #
# Type mapping                                                        #
# ------------------------------------------------------------------ #


def test_all_property_types_map_correctly():
    ddl, _ = _run(
        """
        ontology: o
        entities:
          - name: AllTypes
            keys:
              primary: [pk]
            properties:
              - name: pk
                type: string
              - name: b
                type: bytes
              - name: i
                type: integer
              - name: d
                type: double
              - name: n
                type: numeric
              - name: bo
                type: boolean
              - name: da
                type: date
              - name: ti
                type: time
              - name: dt
                type: datetime
              - name: ts
                type: timestamp
              - name: j
                type: json
        """
    )
    assert "STRING" in ddl
    assert "BYTES" in ddl
    assert "INT64" in ddl
    assert "FLOAT64" in ddl
    assert "NUMERIC" in ddl
    assert "BOOL" in ddl
    assert "DATE" in ddl
    assert "TIME" in ddl
    assert "DATETIME" in ddl
    assert "TIMESTAMP" in ddl
    assert "JSON" in ddl


# ------------------------------------------------------------------ #
# Naming                                                              #
# ------------------------------------------------------------------ #


def test_snake_naming_converts_pascal_and_camel():
    ddl, _ = _run(
        """
        ontology: o
        entities:
          - name: PersonAccount
            keys:
              primary: [accountId]
            properties:
              - name: accountId
                type: string
              - name: firstName
                type: string
        """
    )
    assert "`proj.ds.person_account`" in ddl
    assert "account_id STRING NOT NULL" in ddl
    assert "first_name STRING" in ddl


def test_preserve_naming_keeps_identifiers_verbatim():
    ddl, _ = _run(
        """
        ontology: o
        entities:
          - name: PersonAccount
            keys:
              primary: [accountId]
            properties:
              - name: accountId
                type: string
              - name: firstName
                type: string
        """,
        naming=NamingMode.PRESERVE,
    )
    assert "`proj.ds.PersonAccount`" in ddl
    assert "accountId STRING NOT NULL" in ddl
    assert "firstName STRING" in ddl


# ------------------------------------------------------------------ #
# Relationship DDL — no keys                                          #
# ------------------------------------------------------------------ #


def test_relationship_no_keys():
    ddl, _ = _run(
        """
        ontology: social
        entities:
          - name: Person
            keys:
              primary: [party_id]
            properties:
              - name: party_id
                type: string
        relationships:
          - name: Follows
            from: Person
            to: Person
            properties:
              - name: since
                type: date
        """
    )
    assert ddl == (
        "CREATE TABLE `proj.ds.person` (\n"
        "  party_id STRING NOT NULL,\n"
        "  PRIMARY KEY (party_id) NOT ENFORCED\n"
        ");\n"
        "\n"
        "CREATE TABLE `proj.ds.follows` (\n"
        "  from_party_id STRING NOT NULL,\n"
        "  to_party_id STRING NOT NULL,\n"
        "  since DATE,\n"
        "  FOREIGN KEY (from_party_id) REFERENCES `proj.ds.person`(party_id) NOT ENFORCED,\n"
        "  FOREIGN KEY (to_party_id) REFERENCES `proj.ds.person`(party_id) NOT ENFORCED\n"
        ");\n"
    )


def test_relationship_no_keys_no_primary_key_constraint():
    """No-keys relationships must not have a PRIMARY KEY constraint."""
    _, _ = _run(
        """
        ontology: o
        entities:
          - name: A
            keys:
              primary: [id]
            properties:
              - name: id
                type: string
        relationships:
          - name: Rel
            from: A
            to: A
        """
    )
    ddl, _ = _run(
        """
        ontology: o
        entities:
          - name: A
            keys:
              primary: [id]
            properties:
              - name: id
                type: string
        relationships:
          - name: Rel
            from: A
            to: A
        """
    )
    rel_block = ddl.split("\n\n")[1]
    assert "PRIMARY KEY" not in rel_block


# ------------------------------------------------------------------ #
# Relationship DDL — keys.primary (Mode 1)                           #
# ------------------------------------------------------------------ #


def test_relationship_keys_primary_mode1():
    ddl, _ = _run(
        """
        ontology: fin
        entities:
          - name: Account
            keys:
              primary: [account_id]
            properties:
              - name: account_id
                type: string
          - name: Security
            keys:
              primary: [isin]
            properties:
              - name: isin
                type: string
        relationships:
          - name: Transfer
            from: Account
            to: Account
            keys:
              primary: [txn_id]
            properties:
              - name: txn_id
                type: string
              - name: amount
                type: numeric
        """
    )
    rel_block = ddl.split("\n\n")[2]
    lines = rel_block.splitlines()
    # PK col first, then endpoints, then remaining
    assert lines[1].strip() == "txn_id STRING NOT NULL,"
    assert lines[2].strip() == "from_account_id STRING NOT NULL,"
    assert lines[3].strip() == "to_account_id STRING NOT NULL,"
    assert lines[4].strip() == "amount NUMERIC,"
    assert "PRIMARY KEY (txn_id) NOT ENFORCED" in rel_block
    assert "FOREIGN KEY" in rel_block


# ------------------------------------------------------------------ #
# Relationship DDL — keys.additional (Mode 2)                        #
# ------------------------------------------------------------------ #


def test_relationship_keys_additional_mode2():
    ddl, _ = _run(
        """
        ontology: fin
        entities:
          - name: Account
            keys:
              primary: [account_id, as_of]
            properties:
              - name: account_id
                type: string
              - name: as_of
                type: date
          - name: Security
            keys:
              primary: [isin]
            properties:
              - name: isin
                type: string
        relationships:
          - name: Holding
            from: Account
            to: Security
            keys:
              additional: [as_of]
            properties:
              - name: as_of
                type: date
              - name: quantity
                type: numeric
        """
    )
    rel_block = ddl.split("\n\n")[2]
    assert "  from_account_id STRING NOT NULL," in rel_block
    assert "  from_as_of DATE NOT NULL," in rel_block
    assert "  to_isin STRING NOT NULL," in rel_block
    assert "  as_of DATE NOT NULL," in rel_block
    assert "  quantity NUMERIC" in rel_block
    assert (
        "PRIMARY KEY (from_account_id, from_as_of, to_isin, as_of) NOT ENFORCED"
        in rel_block
    )
    assert (
        "FOREIGN KEY (from_account_id, from_as_of) REFERENCES `proj.ds.account`(account_id, as_of) NOT ENFORCED"
        in rel_block
    )
    assert (
        "FOREIGN KEY (to_isin) REFERENCES `proj.ds.security`(isin) NOT ENFORCED"
        in rel_block
    )


# ------------------------------------------------------------------ #
# Self-edge                                                           #
# ------------------------------------------------------------------ #


def test_self_edge():
    """A relationship where from and to are the same entity."""
    ddl, _ = _run(
        """
        ontology: o
        entities:
          - name: Node
            keys:
              primary: [id]
            properties:
              - name: id
                type: string
        relationships:
          - name: Links
            from: Node
            to: Node
        """
    )
    rel_block = ddl.split("\n\n")[1]
    assert "from_id STRING NOT NULL" in rel_block
    assert "to_id STRING NOT NULL" in rel_block
    # Both FK constraints reference the same node table.
    assert rel_block.count("REFERENCES `proj.ds.node`(id) NOT ENFORCED") == 2


# ------------------------------------------------------------------ #
# Inheritance guard                                                   #
# ------------------------------------------------------------------ #


def test_entity_extends_raises():
    ontology = _ont(
        """
        ontology: o
        entities:
          - name: Base
            keys:
              primary: [id]
            properties:
              - name: id
                type: string
          - name: Child
            extends: Base
            properties: []
        """
    )
    with pytest.raises(ValueError, match="'Child'.*extends"):
        scaffold(ontology, dataset="ds", project="proj")


def test_relationship_extends_raises():
    ontology = _ont(
        """
        ontology: o
        entities:
          - name: A
            keys:
              primary: [id]
            properties:
              - name: id
                type: string
        relationships:
          - name: Base
            from: A
            to: A
          - name: Child
            extends: Base
            from: A
            to: A
        """
    )
    with pytest.raises(ValueError, match="'Child'.*extends"):
        scaffold(ontology, dataset="ds", project="proj")


# ------------------------------------------------------------------ #
# Binding YAML                                                        #
# ------------------------------------------------------------------ #


def test_binding_yaml_is_valid_and_has_header():
    _, binding = _run(
        """
        ontology: social
        entities:
          - name: Person
            keys:
              primary: [party_id]
            properties:
              - name: party_id
                type: string
              - name: name
                type: string
        relationships:
          - name: Follows
            from: Person
            to: Person
            properties:
              - name: since
                type: date
        """
    )
    assert binding.startswith(
        "# This file is user-owned. scaffold will not re-run against it.\n"
    )
    doc = yaml.safe_load(binding)
    assert doc["binding"] == "social-scaffold"
    assert doc["ontology"] == "social"
    assert doc["target"]["backend"] == "bigquery"
    assert doc["target"]["project"] == "proj"
    assert doc["target"]["dataset"] == "ds"

    assert len(doc["entities"]) == 1
    ent = doc["entities"][0]
    assert ent["name"] == "Person"
    assert ent["source"] == "proj.ds.person"
    assert {"name": "party_id", "column": "party_id"} in ent["properties"]
    assert {"name": "name", "column": "name"} in ent["properties"]

    assert len(doc["relationships"]) == 1
    rel = doc["relationships"][0]
    assert rel["name"] == "Follows"
    assert rel["from_columns"] == ["from_party_id"]
    assert rel["to_columns"] == ["to_party_id"]
    assert {"name": "since", "column": "since"} in rel["properties"]


def test_binding_naming_matches_ddl():
    """Binding column names must use the same naming rule as the DDL."""
    _, binding_snake = _run(
        """
        ontology: o
        entities:
          - name: MyEntity
            keys:
              primary: [entityId]
            properties:
              - name: entityId
                type: string
        """,
        naming=NamingMode.SNAKE,
    )
    doc = yaml.safe_load(binding_snake)
    assert doc["entities"][0]["properties"][0]["column"] == "entity_id"

    _, binding_preserve = _run(
        """
        ontology: o
        entities:
          - name: MyEntity
            keys:
              primary: [entityId]
            properties:
              - name: entityId
                type: string
        """,
        naming=NamingMode.PRESERVE,
    )
    doc2 = yaml.safe_load(binding_preserve)
    assert doc2["entities"][0]["properties"][0]["column"] == "entityId"


# ------------------------------------------------------------------ #
# Determinism                                                         #
# ------------------------------------------------------------------ #


def test_output_is_deterministic():
    yaml_text = """
        ontology: fin
        entities:
          - name: Account
            keys:
              primary: [account_id]
            properties:
              - name: account_id
                type: string
          - name: Security
            keys:
              primary: [isin]
            properties:
              - name: isin
                type: string
        relationships:
          - name: Holds
            from: Account
            to: Security
            properties:
              - name: quantity
                type: numeric
        """
    first = _run(yaml_text)
    second = _run(yaml_text)
    assert first == second
