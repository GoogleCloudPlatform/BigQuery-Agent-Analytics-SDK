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

"""Tests for the OWL importer in ``src/bigquery_ontology/owl_importer.py``."""

from __future__ import annotations

from pathlib import Path
import textwrap

import pytest
import yaml

from bigquery_ontology.owl_importer import import_owl

_FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"
_YAMO_TTL = _FIXTURES / "yamo_sample.ttl"


# ===================================================================== #
# Basic import                                                           #
# ===================================================================== #


class TestYamoSample:

  def test_imports_all_classes_as_entities(self):
    yaml_text, _ = import_owl(
        [_YAMO_TTL],
        include_namespaces=["https://example.com/yamo#"],
    )
    data = yaml.safe_load(yaml_text)
    names = [e["name"] for e in data["entities"]]
    assert "Party" in names
    assert "AdUnit" in names
    assert "Campaign" in names
    assert "DecisionPoint" in names
    assert "RejectionReason" in names

  def test_entities_sorted_alphabetically(self):
    yaml_text, _ = import_owl(
        [_YAMO_TTL],
        include_namespaces=["https://example.com/yamo#"],
    )
    data = yaml.safe_load(yaml_text)
    names = [e["name"] for e in data["entities"]]
    assert names == sorted(names)

  def test_properties_assigned_to_correct_entities(self):
    yaml_text, _ = import_owl(
        [_YAMO_TTL],
        include_namespaces=["https://example.com/yamo#"],
    )
    data = yaml.safe_load(yaml_text)
    entity_map = {e["name"]: e for e in data["entities"]}

    party_props = [p["name"] for p in entity_map["Party"]["properties"]]
    assert "party_id" in party_props
    assert "name" in party_props

    campaign_props = [p["name"] for p in entity_map["Campaign"]["properties"]]
    assert "campaign_id" in campaign_props
    assert "budget" in campaign_props
    assert "start_date" in campaign_props

  def test_owl_haskey_maps_to_primary_key(self):
    yaml_text, _ = import_owl(
        [_YAMO_TTL],
        include_namespaces=["https://example.com/yamo#"],
    )
    data = yaml.safe_load(yaml_text)
    entity_map = {e["name"]: e for e in data["entities"]}
    assert entity_map["Party"]["keys"]["primary"] == ["party_id"]
    assert entity_map["Campaign"]["keys"]["primary"] == ["campaign_id"]

  def test_missing_haskey_emits_fill_in(self):
    yaml_text, _ = import_owl(
        [_YAMO_TTL],
        include_namespaces=["https://example.com/yamo#"],
    )
    data = yaml.safe_load(yaml_text)
    entity_map = {e["name"]: e for e in data["entities"]}
    assert entity_map["DecisionPoint"]["keys"]["primary"] == ["FILL_IN"]
    assert "no owl:hasKey" in yaml_text

  def test_subclass_maps_to_extends(self):
    yaml_text, _ = import_owl(
        [_YAMO_TTL],
        include_namespaces=["https://example.com/yamo#"],
    )
    data = yaml.safe_load(yaml_text)
    entity_map = {e["name"]: e for e in data["entities"]}
    assert entity_map["AdUnit"]["extends"] == "Party"

  def test_relationships_extracted(self):
    yaml_text, _ = import_owl(
        [_YAMO_TTL],
        include_namespaces=["https://example.com/yamo#"],
    )
    data = yaml.safe_load(yaml_text)
    rel_map = {r["name"]: r for r in data["relationships"]}
    assert "evaluates" in rel_map
    assert "rejectedBy" in rel_map
    assert rel_map["evaluates"]["from"] == "DecisionPoint"
    assert rel_map["evaluates"]["to"] == "AdUnit"
    assert rel_map["rejectedBy"]["from"] == "AdUnit"
    assert rel_map["rejectedBy"]["to"] == "RejectionReason"

  def test_xsd_decimal_maps_to_numeric(self):
    yaml_text, _ = import_owl(
        [_YAMO_TTL],
        include_namespaces=["https://example.com/yamo#"],
    )
    data = yaml.safe_load(yaml_text)
    entity_map = {e["name"]: e for e in data["entities"]}
    budget = next(
        p for p in entity_map["Campaign"]["properties"] if p["name"] == "budget"
    )
    assert budget["type"] == "numeric"

  def test_xsd_date_maps_to_date(self):
    yaml_text, _ = import_owl(
        [_YAMO_TTL],
        include_namespaces=["https://example.com/yamo#"],
    )
    data = yaml.safe_load(yaml_text)
    entity_map = {e["name"]: e for e in data["entities"]}
    start_date = next(
        p
        for p in entity_map["Campaign"]["properties"]
        if p["name"] == "start_date"
    )
    assert start_date["type"] == "date"

  def test_ontology_name_from_namespace(self):
    yaml_text, _ = import_owl(
        [_YAMO_TTL],
        include_namespaces=["https://example.com/yamo#"],
    )
    data = yaml.safe_load(yaml_text)
    assert data["ontology"] == "yamo"

  def test_explicit_ontology_name(self):
    yaml_text, _ = import_owl(
        [_YAMO_TTL],
        include_namespaces=["https://example.com/yamo#"],
        ontology_name="my_ontology",
    )
    data = yaml.safe_load(yaml_text)
    assert data["ontology"] == "my_ontology"

  def test_description_from_label(self):
    yaml_text, _ = import_owl(
        [_YAMO_TTL],
        include_namespaces=["https://example.com/yamo#"],
    )
    data = yaml.safe_load(yaml_text)
    entity_map = {e["name"]: e for e in data["entities"]}
    assert entity_map["Party"]["description"] == "Party"
    rel_map = {r["name"]: r for r in data["relationships"]}
    assert rel_map["evaluates"]["description"] == "evaluates"


# ===================================================================== #
# Design doc worked example (§18)                                        #
# ===================================================================== #

_FINANCE_TTL = """\
@prefix : <https://example.com/finance#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .

:Party  a owl:Class ; rdfs:label "Party" ;
    owl:hasKey ( :party_id ) .

:Person  a owl:Class ;
    rdfs:subClassOf :Party ;
    rdfs:label "Person" ;
    owl:disjointWith :Organization .

:Organization  a owl:Class ;
    rdfs:subClassOf :Party ;
    rdfs:label "Organization" .

:party_id  a owl:DatatypeProperty ;
    rdfs:domain :Party ;
    rdfs:range xsd:string .

:name  a owl:DatatypeProperty ;
    rdfs:domain :Party ;
    rdfs:range xsd:string .

:Account  a owl:Class ;
    rdfs:label "Account" .

:heldBy  a owl:ObjectProperty, owl:TransitiveProperty ;
    owl:inverseOf :holds ;
    rdfs:domain :Account ;
    rdfs:range :Party .
"""


class TestDesignDocWorkedExample:

  def _import(self, tmp_path):
    ttl = tmp_path / "finance.ttl"
    ttl.write_text(_FINANCE_TTL, encoding="utf-8")
    return import_owl(
        [ttl],
        include_namespaces=["https://example.com/finance#"],
        ontology_name="finance",
    )

  def test_entities_present(self, tmp_path):
    yaml_text, _ = self._import(tmp_path)
    data = yaml.safe_load(yaml_text)
    names = [e["name"] for e in data["entities"]]
    assert names == ["Account", "Organization", "Party", "Person"]

  def test_account_has_fill_in_key(self, tmp_path):
    yaml_text, _ = self._import(tmp_path)
    data = yaml.safe_load(yaml_text)
    account = next(e for e in data["entities"] if e["name"] == "Account")
    assert account["keys"]["primary"] == ["FILL_IN"]

  def test_party_has_key(self, tmp_path):
    yaml_text, _ = self._import(tmp_path)
    data = yaml.safe_load(yaml_text)
    party = next(e for e in data["entities"] if e["name"] == "Party")
    assert party["keys"]["primary"] == ["party_id"]

  def test_inheritance(self, tmp_path):
    yaml_text, _ = self._import(tmp_path)
    data = yaml.safe_load(yaml_text)
    entity_map = {e["name"]: e for e in data["entities"]}
    assert entity_map["Person"]["extends"] == "Party"
    assert entity_map["Organization"]["extends"] == "Party"

  def test_inherited_entities_no_key_placeholder(self, tmp_path):
    yaml_text, _ = self._import(tmp_path)
    data = yaml.safe_load(yaml_text)
    person = next(e for e in data["entities"] if e["name"] == "Person")
    assert "keys" not in person

  def test_disjoint_annotation(self, tmp_path):
    yaml_text, _ = self._import(tmp_path)
    data = yaml.safe_load(yaml_text)
    person = next(e for e in data["entities"] if e["name"] == "Person")
    assert person["annotations"]["owl:disjointWith"] == "Organization"

  def test_relationship_heldby(self, tmp_path):
    yaml_text, _ = self._import(tmp_path)
    data = yaml.safe_load(yaml_text)
    rel_map = {r["name"]: r for r in data["relationships"]}
    assert "heldBy" in rel_map
    assert rel_map["heldBy"]["from"] == "Account"
    assert rel_map["heldBy"]["to"] == "Party"

  def test_relationship_drop_annotations(self, tmp_path):
    yaml_text, _ = self._import(tmp_path)
    data = yaml.safe_load(yaml_text)
    rel_map = {r["name"]: r for r in data["relationships"]}
    held = rel_map["heldBy"]
    assert held["annotations"]["owl:inverseOf"] == "holds"
    assert "Transitive" in held["annotations"]["owl:characteristics"]

  def test_drop_summary_not_empty(self, tmp_path):
    _, summary = self._import(tmp_path)
    assert "Dropped OWL features" in summary

  def test_determinism(self, tmp_path):
    y1, s1 = self._import(tmp_path)
    y2, s2 = self._import(tmp_path)
    assert y1 == y2
    assert s1 == s2


# ===================================================================== #
# Edge cases                                                             #
# ===================================================================== #


class TestEdgeCases:

  def test_no_sources_raises(self):
    with pytest.raises(ValueError, match="source"):
      import_owl([], include_namespaces=["http://example.com/"])

  def test_no_namespaces_raises(self):
    with pytest.raises(ValueError, match="namespace"):
      import_owl([_YAMO_TTL], include_namespaces=[])

  def test_namespace_filter_excludes(self, tmp_path):
    ttl = tmp_path / "test.ttl"
    ttl.write_text(
        textwrap.dedent(
            """\
        @prefix a: <http://example.com/a#> .
        @prefix b: <http://example.com/b#> .
        @prefix owl: <http://www.w3.org/2002/07/owl#> .
        @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
        @prefix xsd: <http://www.w3.org/2001/XMLSchema#> .

        a:Foo a owl:Class ; owl:hasKey ( a:foo_id ) .
        a:foo_id a owl:DatatypeProperty ; rdfs:domain a:Foo ; rdfs:range xsd:string .
        b:Bar a owl:Class ; owl:hasKey ( b:bar_id ) .
        b:bar_id a owl:DatatypeProperty ; rdfs:domain b:Bar ; rdfs:range xsd:string .
    """
        ),
        encoding="utf-8",
    )

    yaml_text, summary = import_owl(
        [ttl],
        include_namespaces=["http://example.com/a#"],
    )
    data = yaml.safe_load(yaml_text)
    names = [e["name"] for e in data["entities"]]
    assert "Foo" in names
    assert "Bar" not in names
    assert "Excluded by namespace" in summary

  def test_multi_parent_emits_fill_in(self, tmp_path):
    ttl = tmp_path / "test.ttl"
    ttl.write_text(
        textwrap.dedent(
            """\
        @prefix : <http://example.com/test#> .
        @prefix owl: <http://www.w3.org/2002/07/owl#> .
        @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
        @prefix xsd: <http://www.w3.org/2001/XMLSchema#> .

        :A a owl:Class ; owl:hasKey ( :a_id ) .
        :a_id a owl:DatatypeProperty ; rdfs:domain :A ; rdfs:range xsd:string .
        :B a owl:Class ; owl:hasKey ( :b_id ) .
        :b_id a owl:DatatypeProperty ; rdfs:domain :B ; rdfs:range xsd:string .
        :C a owl:Class ;
            rdfs:subClassOf :A, :B .
    """
        ),
        encoding="utf-8",
    )

    yaml_text, _ = import_owl(
        [ttl],
        include_namespaces=["http://example.com/test#"],
    )
    data = yaml.safe_load(yaml_text)
    c = next(e for e in data["entities"] if e["name"] == "C")
    assert c["extends"] == "FILL_IN"
    assert "multi-parent" in yaml_text

  def test_functional_property_cardinality(self, tmp_path):
    ttl = tmp_path / "test.ttl"
    ttl.write_text(
        textwrap.dedent(
            """\
        @prefix : <http://example.com/test#> .
        @prefix owl: <http://www.w3.org/2002/07/owl#> .
        @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

        :A a owl:Class ; owl:hasKey ( :a_id ) .
        :a_id a owl:DatatypeProperty ; rdfs:domain :A ; rdfs:range <http://www.w3.org/2001/XMLSchema#string> .
        :B a owl:Class ; owl:hasKey ( :b_id ) .
        :b_id a owl:DatatypeProperty ; rdfs:domain :B ; rdfs:range <http://www.w3.org/2001/XMLSchema#string> .
        :rel a owl:ObjectProperty, owl:FunctionalProperty ;
            rdfs:domain :A ; rdfs:range :B .
    """
        ),
        encoding="utf-8",
    )

    yaml_text, _ = import_owl(
        [ttl],
        include_namespaces=["http://example.com/test#"],
    )
    data = yaml.safe_load(yaml_text)
    rel = next(r for r in data["relationships"] if r["name"] == "rel")
    assert rel["cardinality"] == "many_to_one"
