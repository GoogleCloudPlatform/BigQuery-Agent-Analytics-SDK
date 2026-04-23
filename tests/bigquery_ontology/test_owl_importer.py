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

pytest.importorskip("rdflib")

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

  def test_no_domain_emits_fill_in(self, tmp_path):
    ttl = tmp_path / "test.ttl"
    ttl.write_text(
        textwrap.dedent(
            """\
        @prefix : <http://example.com/test#> .
        @prefix owl: <http://www.w3.org/2002/07/owl#> .
        @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

        :A a owl:Class ; owl:hasKey ( :a_id ) .
        :a_id a owl:DatatypeProperty ; rdfs:domain :A ; rdfs:range <http://www.w3.org/2001/XMLSchema#string> .
        :orphan a owl:ObjectProperty ; rdfs:range :A .
    """
        ),
        encoding="utf-8",
    )
    yaml_text, _ = import_owl(
        [ttl],
        include_namespaces=["http://example.com/test#"],
    )
    data = yaml.safe_load(yaml_text)
    rel = next(r for r in data["relationships"] if r["name"] == "orphan")
    assert rel["from"] == "FILL_IN"
    assert rel["to"] == "A"
    assert "no rdfs:domain" in yaml_text

  def test_no_range_emits_fill_in(self, tmp_path):
    ttl = tmp_path / "test.ttl"
    ttl.write_text(
        textwrap.dedent(
            """\
        @prefix : <http://example.com/test#> .
        @prefix owl: <http://www.w3.org/2002/07/owl#> .
        @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

        :A a owl:Class ; owl:hasKey ( :a_id ) .
        :a_id a owl:DatatypeProperty ; rdfs:domain :A ; rdfs:range <http://www.w3.org/2001/XMLSchema#string> .
        :orphan a owl:ObjectProperty ; rdfs:domain :A .
    """
        ),
        encoding="utf-8",
    )
    yaml_text, _ = import_owl(
        [ttl],
        include_namespaces=["http://example.com/test#"],
    )
    data = yaml.safe_load(yaml_text)
    rel = next(r for r in data["relationships"] if r["name"] == "orphan")
    assert rel["from"] == "A"
    assert rel["to"] == "FILL_IN"
    assert "no rdfs:range" in yaml_text

  def test_relationship_extends(self, tmp_path):
    ttl = tmp_path / "test.ttl"
    ttl.write_text(
        textwrap.dedent(
            """\
        @prefix : <http://example.com/test#> .
        @prefix owl: <http://www.w3.org/2002/07/owl#> .
        @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

        :A a owl:Class ; owl:hasKey ( :a_id ) .
        :a_id a owl:DatatypeProperty ; rdfs:domain :A ; rdfs:range <http://www.w3.org/2001/XMLSchema#string> .
        :parent_rel a owl:ObjectProperty ; rdfs:domain :A ; rdfs:range :A .
        :child_rel a owl:ObjectProperty ; rdfs:subPropertyOf :parent_rel ;
            rdfs:domain :A ; rdfs:range :A .
    """
        ),
        encoding="utf-8",
    )
    yaml_text, _ = import_owl(
        [ttl],
        include_namespaces=["http://example.com/test#"],
    )
    data = yaml.safe_load(yaml_text)
    rel_map = {r["name"]: r for r in data["relationships"]}
    assert rel_map["child_rel"]["extends"] == "parent_rel"

  def test_yaml_scalar_escapes_quotes(self, tmp_path):
    from bigquery_ontology.owl_importer import _yaml_scalar

    assert _yaml_scalar("hello") == "hello"
    assert _yaml_scalar("has: colon") == '"has: colon"'
    assert _yaml_scalar('has "quote"') == '"has \\"quote\\""'
    assert _yaml_scalar("back\\slash") == '"back\\\\slash"'

  def test_yaml_scalar_quotes_booleans_and_nulls(self, tmp_path):
    from bigquery_ontology.owl_importer import _yaml_scalar

    for val in ("true", "false", "yes", "no", "on", "off", "null", "~"):
      quoted = _yaml_scalar(val)
      assert quoted.startswith('"'), f"{val} should be quoted"
      parsed = yaml.safe_load(f"key: {quoted}")
      assert parsed["key"] == val, f"{val} should round-trip as string"

  def test_yaml_scalar_quotes_numbers(self, tmp_path):
    from bigquery_ontology.owl_importer import _yaml_scalar

    for val in ("42", "1.5", "0", "-3"):
      quoted = _yaml_scalar(val)
      assert quoted.startswith('"'), f"{val} should be quoted"
      parsed = yaml.safe_load(f"key: {quoted}")
      assert parsed["key"] == val

  def test_yaml_scalar_quotes_special_leading_chars(self, tmp_path):
    from bigquery_ontology.owl_importer import _yaml_scalar

    for val in ("*alias", "&anchor", "!tag", "%dir"):
      quoted = _yaml_scalar(val)
      assert quoted.startswith('"'), f"{val} should be quoted"

  def test_yaml_scalar_empty_string(self, tmp_path):
    from bigquery_ontology.owl_importer import _yaml_scalar

    assert _yaml_scalar("") == '""'

  def test_entity_relationship_name_overlap_raises(self, tmp_path):
    ttl = tmp_path / "test.ttl"
    ttl.write_text(
        textwrap.dedent(
            """\
        @prefix : <http://example.com/test#> .
        @prefix owl: <http://www.w3.org/2002/07/owl#> .
        @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

        :Foo a owl:Class ; owl:hasKey ( :foo_id ) .
        :foo_id a owl:DatatypeProperty ; rdfs:domain :Foo ;
            rdfs:range <http://www.w3.org/2001/XMLSchema#string> .
        :Foo a owl:ObjectProperty ; rdfs:domain :Foo ; rdfs:range :Foo .
    """
        ),
        encoding="utf-8",
    )
    with pytest.raises(
        ValueError, match="collision between entities and relationships"
    ):
      import_owl([ttl], include_namespaces=["http://example.com/test#"])

  def test_duplicate_property_name_raises(self, tmp_path):
    ttl = tmp_path / "test.ttl"
    ttl.write_text(
        textwrap.dedent(
            """\
        @prefix a: <http://example.com/a#> .
        @prefix b: <http://example.com/b#> .
        @prefix owl: <http://www.w3.org/2002/07/owl#> .
        @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
        @prefix xsd: <http://www.w3.org/2001/XMLSchema#> .

        a:Thing a owl:Class ; owl:hasKey ( a:name ) .
        a:name a owl:DatatypeProperty ; rdfs:domain a:Thing ; rdfs:range xsd:string .
        b:name a owl:DatatypeProperty ; rdfs:domain a:Thing ; rdfs:range xsd:integer .
    """
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="Duplicate property"):
      import_owl(
          [ttl],
          include_namespaces=["http://example.com/a#", "http://example.com/b#"],
      )

  def test_name_collision_raises(self, tmp_path):
    ttl = tmp_path / "test.ttl"
    ttl.write_text(
        textwrap.dedent(
            """\
        @prefix a: <http://example.com/a#> .
        @prefix b: <http://example.com/b#> .
        @prefix owl: <http://www.w3.org/2002/07/owl#> .

        a:Thing a owl:Class .
        b:Thing a owl:Class .
    """
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="Name collision"):
      import_owl(
          [ttl],
          include_namespaces=["http://example.com/a#", "http://example.com/b#"],
      )

  def test_english_label_preferred(self, tmp_path):
    ttl = tmp_path / "test.ttl"
    ttl.write_text(
        textwrap.dedent(
            """\
        @prefix : <http://example.com/test#> .
        @prefix owl: <http://www.w3.org/2002/07/owl#> .
        @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .

        :Thing a owl:Class ;
            rdfs:label "Chose"@fr ;
            rdfs:label "Thing"@en ;
            owl:hasKey ( :thing_id ) .
        :thing_id a owl:DatatypeProperty ;
            rdfs:domain :Thing ;
            rdfs:range <http://www.w3.org/2001/XMLSchema#string> .
    """
        ),
        encoding="utf-8",
    )
    yaml_text, _ = import_owl(
        [ttl],
        include_namespaces=["http://example.com/test#"],
    )
    data = yaml.safe_load(yaml_text)
    entity = data["entities"][0]
    assert entity["description"] == "Thing"
    assert entity["annotations"]["rdfs:label@fr"] == "Chose"

  def test_haskey_excluded_by_namespace(self, tmp_path):
    ttl = tmp_path / "test.ttl"
    ttl.write_text(
        textwrap.dedent(
            """\
        @prefix a: <http://example.com/a#> .
        @prefix ext: <http://external.com/ns#> .
        @prefix owl: <http://www.w3.org/2002/07/owl#> .
        @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
        @prefix xsd: <http://www.w3.org/2001/XMLSchema#> .

        a:Foo a owl:Class ;
            owl:hasKey ( ext:ext_id ) .
        ext:ext_id a owl:DatatypeProperty ;
            rdfs:domain a:Foo ;
            rdfs:range xsd:string .
    """
        ),
        encoding="utf-8",
    )

    yaml_text, _ = import_owl(
        [ttl],
        include_namespaces=["http://example.com/a#"],
    )
    data = yaml.safe_load(yaml_text)
    foo = data["entities"][0]
    assert foo["keys"]["primary"] == ["FILL_IN"]
    assert "excluded by namespace filter" in yaml_text
    assert "owl:hasKey_excluded" in yaml_text


# ------------------------------------------------------------------ #
# SKOS import tests                                                    #
# ------------------------------------------------------------------ #

_SKOS_TTL = _FIXTURES / "skos_taxonomy.ttl"
_MIXED_TTL = _FIXTURES / "mixed_owl_skos.ttl"


class TestPureSkosImport:
  """Import a pure SKOS taxonomy with concepts, broader, related, match."""

  def test_all_entities_are_abstract_with_prefix(self):
    yaml_text, _ = import_owl(
        [_SKOS_TTL],
        include_namespaces=["http://example.com/taxonomy#"],
    )
    data = yaml.safe_load(yaml_text)
    for entity in data["entities"]:
      assert entity["abstract"] is True, f"{entity['name']} should be abstract"
      assert entity["name"].startswith("skos_"), entity["name"]

  def test_broader_relationships_are_abstract(self):
    yaml_text, _ = import_owl(
        [_SKOS_TTL],
        include_namespaces=["http://example.com/taxonomy#"],
    )
    data = yaml.safe_load(yaml_text)
    rels = data["relationships"]
    broader = [r for r in rels if r["name"] == "skos_broader"]
    assert len(broader) >= 2
    for r in broader:
      assert r["abstract"] is True

  def test_related_relationship(self):
    yaml_text, _ = import_owl(
        [_SKOS_TTL],
        include_namespaces=["http://example.com/taxonomy#"],
    )
    data = yaml.safe_load(yaml_text)
    rels = data["relationships"]
    related = [r for r in rels if r["name"] == "skos_related"]
    assert len(related) >= 1
    assert related[0]["abstract"] is True

  def test_related_is_symmetric_single_edge(self, tmp_path):
    # skos:related is symmetric; mutual assertions must collapse to one edge.
    ttl = tmp_path / "mutual_related.ttl"
    ttl.write_text(
        "@prefix : <http://example.com/taxonomy#> .\n"
        "@prefix skos: <http://www.w3.org/2004/02/skos/core#> .\n"
        ":c1 a skos:Concept ; skos:related :c2 .\n"
        ":c2 a skos:Concept ; skos:related :c1 .\n",
        encoding="utf-8",
    )
    yaml_text, _ = import_owl(
        [ttl],
        include_namespaces=["http://example.com/taxonomy#"],
    )
    data = yaml.safe_load(yaml_text)
    related = [r for r in data["relationships"] if r["name"] == "skos_related"]
    assert len(related) == 1
    assert {related[0]["from"], related[0]["to"]} == {"skos_c1", "skos_c2"}

  def test_skos_annotations_preserved(self):
    yaml_text, _ = import_owl(
        [_SKOS_TTL],
        include_namespaces=["http://example.com/taxonomy#"],
    )
    data = yaml.safe_load(yaml_text)
    banking = next(e for e in data["entities"] if e["name"] == "skos_Banking")
    assert banking["annotations"]["skos:definition"] == (
        "Activities of financial institutions."
    )

  def test_synonyms_from_altlabel(self):
    yaml_text, _ = import_owl(
        [_SKOS_TTL],
        include_namespaces=["http://example.com/taxonomy#"],
    )
    data = yaml.safe_load(yaml_text)
    retail = next(
        e for e in data["entities"] if e["name"] == "skos_RetailBanking"
    )
    assert "Consumer Banking" in retail["synonyms"]

  def test_notation_annotation(self):
    yaml_text, _ = import_owl(
        [_SKOS_TTL],
        include_namespaces=["http://example.com/taxonomy#"],
    )
    data = yaml.safe_load(yaml_text)
    retail = next(
        e for e in data["entities"] if e["name"] == "skos_RetailBanking"
    )
    assert retail["annotations"]["skos:notation"] == "RB"

  def test_external_match_becomes_annotation(self):
    yaml_text, _ = import_owl(
        [_SKOS_TTL],
        include_namespaces=["http://example.com/taxonomy#"],
    )
    data = yaml.safe_load(yaml_text)
    wm = next(
        e for e in data["entities"] if e["name"] == "skos_WealthManagement"
    )
    assert "skos:exactMatch" in wm["annotations"]

  def test_no_description_for_pure_skos(self):
    yaml_text, _ = import_owl(
        [_SKOS_TTL],
        include_namespaces=["http://example.com/taxonomy#"],
    )
    data = yaml.safe_load(yaml_text)
    for entity in data["entities"]:
      assert "description" not in entity

  def test_no_keys_on_abstract_entities(self):
    yaml_text, _ = import_owl(
        [_SKOS_TTL],
        include_namespaces=["http://example.com/taxonomy#"],
    )
    data = yaml.safe_load(yaml_text)
    for entity in data["entities"]:
      assert "keys" not in entity

  def test_all_abstract_hint_in_summary(self):
    _, summary = import_owl(
        [_SKOS_TTL],
        include_namespaces=["http://example.com/taxonomy#"],
    )
    assert "all entities are abstract" in summary.lower()

  def test_narrower_normalized_to_broader(self):
    """skos:narrower on WealthManagement → InvestmentBanking should
    produce a skos_broader from InvestmentBanking to WealthManagement."""
    yaml_text, _ = import_owl(
        [_SKOS_TTL],
        include_namespaces=["http://example.com/taxonomy#"],
    )
    data = yaml.safe_load(yaml_text)
    rels = data["relationships"]
    broader = [r for r in rels if r["name"] == "skos_broader"]
    # InvestmentBanking broader WealthManagement (via narrower inverse)
    inv_wm = [
        r
        for r in broader
        if r["from"] == "skos_InvestmentBanking"
        and r["to"] == "skos_WealthManagement"
    ]
    assert len(inv_wm) == 1


class TestMixedOwlSkos:
  """Import files with both OWL classes and SKOS concepts."""

  def test_owl_skos_entity_is_concrete(self):
    yaml_text, _ = import_owl(
        [_MIXED_TTL],
        include_namespaces=["http://example.com/finance#"],
    )
    data = yaml.safe_load(yaml_text)
    account = next(e for e in data["entities"] if e["name"] == "Account")
    assert "abstract" not in account or account.get("abstract") is False

  def test_pure_skos_entity_is_abstract(self):
    yaml_text, _ = import_owl(
        [_MIXED_TTL],
        include_namespaces=["http://example.com/finance#"],
    )
    data = yaml.safe_load(yaml_text)
    fp = next(
        e for e in data["entities"] if e["name"] == "skos_FinancialProduct"
    )
    assert fp["abstract"] is True

  def test_owl_entity_enriched_with_skos_metadata(self):
    yaml_text, _ = import_owl(
        [_MIXED_TTL],
        include_namespaces=["http://example.com/finance#"],
    )
    data = yaml.safe_load(yaml_text)
    account = next(e for e in data["entities"] if e["name"] == "Account")
    assert account["annotations"]["skos:definition"] == (
        "A record of financial transactions."
    )
    assert "Acct" in account["synonyms"]

  def test_external_exactmatch_is_annotation(self):
    yaml_text, _ = import_owl(
        [_MIXED_TTL],
        include_namespaces=["http://example.com/finance#"],
    )
    data = yaml.safe_load(yaml_text)
    account = next(e for e in data["entities"] if e["name"] == "Account")
    assert "skos:exactMatch" in account["annotations"]

  def test_cross_kind_broader(self):
    """Account (concrete OWL) → skos:broader → FinancialProduct (abstract SKOS)."""
    yaml_text, _ = import_owl(
        [_MIXED_TTL],
        include_namespaces=["http://example.com/finance#"],
    )
    data = yaml.safe_load(yaml_text)
    rels = data["relationships"]
    broader = [
        r
        for r in rels
        if r["name"] == "skos_broader"
        and r["from"] == "Account"
        and r["to"] == "skos_FinancialProduct"
    ]
    assert len(broader) == 1
    assert broader[0]["abstract"] is True

  def test_owl_relationship_stays_concrete(self):
    yaml_text, _ = import_owl(
        [_MIXED_TTL],
        include_namespaces=["http://example.com/finance#"],
    )
    data = yaml.safe_load(yaml_text)
    rels = data["relationships"]
    holds = next(r for r in rels if r["name"] == "holds")
    assert "abstract" not in holds or holds.get("abstract") is False

  def test_skos_related_between_owl_entities(self):
    """Account (OWL+SKOS) skos:related Ledger (OWL) → abstract rel."""
    yaml_text, _ = import_owl(
        [_MIXED_TTL],
        include_namespaces=["http://example.com/finance#"],
    )
    data = yaml.safe_load(yaml_text)
    rels = data["relationships"]
    related = [
        r
        for r in rels
        if r["name"] == "skos_related"
        and r["from"] == "Account"
        and r["to"] == "Ledger"
    ]
    assert len(related) == 1
    assert related[0]["abstract"] is True


class TestLanguageSelection:
  """Multilingual label handling via --language."""

  def test_default_english(self):
    yaml_text, _ = import_owl(
        [_SKOS_TTL],
        include_namespaces=["http://example.com/taxonomy#"],
    )
    data = yaml.safe_load(yaml_text)
    banking = next(e for e in data["entities"] if e["name"] == "skos_Banking")
    assert "skos:prefLabel@fr" in banking["annotations"]
    assert banking["annotations"]["skos:prefLabel@fr"] == "Banque"

  def test_french_selection(self):
    yaml_text, _ = import_owl(
        [_SKOS_TTL],
        include_namespaces=["http://example.com/taxonomy#"],
        language="fr",
    )
    data = yaml.safe_load(yaml_text)
    banking = next(e for e in data["entities"] if e["name"] == "skos_Banking")
    # French selected → English goes to annotation
    assert "skos:prefLabel@en" in banking["annotations"]


class TestAbstractOntologyValidation:
  """Ontology loader handles abstract entities and relationships."""

  def test_abstract_entity_no_keys_passes(self):
    from bigquery_ontology import load_ontology_from_string

    yaml_str = textwrap.dedent(
        """\
      ontology: test_abstract
      entities:
        - name: ConcreteEntity
          keys: {primary: [id]}
          properties:
            - {name: id, type: string}
        - name: skos_AbstractEntity
          abstract: true
    """
    )
    ont = load_ontology_from_string(yaml_str)
    assert ont.entities[1].abstract is True

  def test_abstract_rel_duplicate_names_different_endpoints(self):
    from bigquery_ontology import load_ontology_from_string

    yaml_str = textwrap.dedent(
        """\
      ontology: test_abstract_rels
      entities:
        - name: A
          keys: {primary: [id]}
          properties:
            - {name: id, type: string}
        - name: B
          keys: {primary: [id]}
          properties:
            - {name: id, type: string}
        - name: C
          keys: {primary: [id]}
          properties:
            - {name: id, type: string}
      relationships:
        - name: skos_broader
          abstract: true
          from: A
          to: B
        - name: skos_broader
          abstract: true
          from: A
          to: C
    """
    )
    ont = load_ontology_from_string(yaml_str)
    assert len(ont.relationships) == 2

  def test_abstract_rel_duplicate_name_same_endpoints_fails(self):
    from bigquery_ontology import load_ontology_from_string

    yaml_str = textwrap.dedent(
        """\
      ontology: test_dup
      entities:
        - name: A
          keys: {primary: [id]}
          properties:
            - {name: id, type: string}
        - name: B
          keys: {primary: [id]}
          properties:
            - {name: id, type: string}
      relationships:
        - name: skos_broader
          abstract: true
          from: A
          to: B
        - name: skos_broader
          abstract: true
          from: A
          to: B
    """
    )
    with pytest.raises(ValueError, match="Duplicate abstract relationship"):
      load_ontology_from_string(yaml_str)

  def test_concrete_relationship_abstract_endpoint_fails(self):
    """A concrete relationship cannot have an abstract endpoint —
    there is nothing to bind."""
    from bigquery_ontology import load_ontology_from_string

    yaml_str = textwrap.dedent(
        """\
      ontology: test_concrete_abstract_endpoint
      entities:
        - name: Concrete
          keys: {primary: [id]}
          properties:
            - {name: id, type: string}
        - name: skos_AbstractTarget
          abstract: true
      relationships:
        - name: pointsAt
          from: Concrete
          to: skos_AbstractTarget
    """
    )
    with pytest.raises(ValueError, match="abstract endpoint"):
      load_ontology_from_string(yaml_str)

  def test_abstract_relationship_abstract_endpoint_allowed(self):
    """An abstract relationship may have abstract endpoints."""
    from bigquery_ontology import load_ontology_from_string

    yaml_str = textwrap.dedent(
        """\
      ontology: test_abstract_abstract_endpoint
      entities:
        - name: Concrete
          keys: {primary: [id]}
          properties:
            - {name: id, type: string}
        - name: skos_AbstractSource
          abstract: true
        - name: skos_AbstractTarget
          abstract: true
      relationships:
        - name: skos_broader
          abstract: true
          from: skos_AbstractSource
          to: skos_AbstractTarget
    """
    )
    ont = load_ontology_from_string(yaml_str)
    assert len(ont.relationships) == 1

  def test_abstract_relationship_endpoint_must_exist(self):
    """Endpoint existence is still enforced on abstract relationships."""
    from bigquery_ontology import load_ontology_from_string

    yaml_str = textwrap.dedent(
        """\
      ontology: test_missing_endpoint
      entities:
        - name: A
          keys: {primary: [id]}
          properties:
            - {name: id, type: string}
      relationships:
        - name: skos_broader
          abstract: true
          from: A
          to: DoesNotExist
    """
    )
    with pytest.raises(ValueError, match="DoesNotExist"):
      load_ontology_from_string(yaml_str)

  def test_concrete_abstract_name_collision_fails(self):
    from bigquery_ontology import load_ontology_from_string

    yaml_str = textwrap.dedent(
        """\
      ontology: test_collision
      entities:
        - name: A
          keys: {primary: [id]}
          properties:
            - {name: id, type: string}
        - name: B
          keys: {primary: [id]}
          properties:
            - {name: id, type: string}
      relationships:
        - name: rel
          from: A
          to: B
        - name: rel
          abstract: true
          from: A
          to: B
    """
    )
    with pytest.raises(ValueError, match="concrete and an abstract"):
      load_ontology_from_string(yaml_str)


class TestAbstractBindingRejection:
  """Binding loader rejects abstract targets."""

  def test_binding_abstract_entity_fails(self):
    from bigquery_ontology import load_ontology_from_string
    from bigquery_ontology.binding_loader import load_binding_from_string

    ont = load_ontology_from_string(
        textwrap.dedent(
            """\
      ontology: test
      entities:
        - name: Concrete
          keys: {primary: [id]}
          properties:
            - {name: id, type: string}
        - name: Abstract
          abstract: true
    """
        )
    )
    binding_yaml = textwrap.dedent(
        """\
      binding: test_bind
      ontology: test
      target:
        backend: bigquery
        project: p
        dataset: d
      entities:
        - name: Abstract
          source: tbl
          properties:
            - {name: id, column: id}
    """
    )
    with pytest.raises(ValueError, match="abstract entity"):
      load_binding_from_string(binding_yaml, ontology=ont)

  def test_binding_abstract_relationship_fails(self):
    from bigquery_ontology import load_ontology_from_string
    from bigquery_ontology.binding_loader import load_binding_from_string

    ont = load_ontology_from_string(
        textwrap.dedent(
            """\
      ontology: test
      entities:
        - name: A
          keys: {primary: [id]}
          properties:
            - {name: id, type: string}
        - name: B
          keys: {primary: [id]}
          properties:
            - {name: id, type: string}
      relationships:
        - name: skos_broader
          abstract: true
          from: A
          to: B
    """
        )
    )
    binding_yaml = textwrap.dedent(
        """\
      binding: test_bind
      ontology: test
      target:
        backend: bigquery
        project: p
        dataset: d
      entities:
        - name: A
          source: tbl_a
          properties:
            - {name: id, column: id}
        - name: B
          source: tbl_b
          properties:
            - {name: id, column: id}
      relationships:
        - name: skos_broader
          source: tbl_rel
          from_columns: [a_id]
          to_columns: [b_id]
    """
    )
    with pytest.raises(ValueError, match="abstract relationship"):
      load_binding_from_string(binding_yaml, ontology=ont)


class TestAbstractScaffold:
  """Scaffold skips abstract entities and relationships."""

  def test_scaffold_skips_abstract(self):
    from bigquery_ontology import load_ontology_from_string
    from bigquery_ontology.scaffold import scaffold

    ont = load_ontology_from_string(
        textwrap.dedent(
            """\
      ontology: test
      entities:
        - name: Concrete
          keys: {primary: [id]}
          properties:
            - {name: id, type: string}
        - name: skos_Abstract
          abstract: true
      relationships:
        - name: skos_broader
          abstract: true
          from: Concrete
          to: skos_Abstract
    """
        )
    )
    ddl, binding_yaml = scaffold(ont, dataset="ds", project="proj")
    assert "concrete" in ddl
    assert "skos_abstract" not in ddl
    assert "skos_broader" not in ddl
    assert "skos_Abstract" not in binding_yaml
    assert "skos_broader" not in binding_yaml


class TestOwlRefToSkosConcept:
  """An OWL ObjectProperty whose rdfs:range is a pure SKOS concept
  must resolve to the correct ``skos_`` prefixed entity name."""

  def test_owl_objectproperty_range_on_skos_concept(self, tmp_path):
    ttl = tmp_path / "ref.ttl"
    ttl.write_text(
        textwrap.dedent(
            """\
      @prefix : <http://example.com/test#> .
      @prefix owl: <http://www.w3.org/2002/07/owl#> .
      @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
      @prefix skos: <http://www.w3.org/2004/02/skos/core#> .
      @prefix xsd: <http://www.w3.org/2001/XMLSchema#> .

      :Category a skos:Concept ;
          skos:prefLabel "Category"@en .

      :Account a owl:Class ;
          owl:hasKey ( :account_id ) .
      :account_id a owl:DatatypeProperty ;
          rdfs:domain :Account ;
          rdfs:range xsd:string .
    """
        )
    )
    yaml_text, _ = import_owl(
        [ttl],
        include_namespaces=["http://example.com/test#"],
    )
    # When added, an OWL ObjectProperty pointing at :Category should
    # emit ``to: skos_Category``, not ``to: Category``.
    # Add a probe ObjectProperty and reparse.
    ttl.write_text(
        ttl.read_text()
        + textwrap.dedent(
            """
      :belongsTo a owl:ObjectProperty ;
          rdfs:domain :Account ;
          rdfs:range :Category .
    """
        )
    )
    yaml_text, _ = import_owl(
        [ttl],
        include_namespaces=["http://example.com/test#"],
    )
    data = yaml.safe_load(yaml_text)
    rel = next(r for r in data["relationships"] if r["name"] == "belongsTo")
    assert rel["to"] == "skos_Category"
    # Endpoint is abstract → relationship must be marked abstract too,
    # or the loader rejects the ontology.
    assert rel.get("abstract") is True

    # Verify the emitted YAML actually round-trips through the loader.
    from bigquery_ontology import load_ontology_from_string

    load_ontology_from_string(yaml_text)

  def test_owl_subclassof_skos_concept_extends_resolves(self, tmp_path):
    """An OWL class whose rdfs:subClassOf target is a pure SKOS concept
    must emit ``extends: skos_<name>`` so the YAML loads."""
    ttl = tmp_path / "subclass.ttl"
    ttl.write_text(
        textwrap.dedent(
            """\
      @prefix : <http://example.com/test#> .
      @prefix owl: <http://www.w3.org/2002/07/owl#> .
      @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
      @prefix skos: <http://www.w3.org/2004/02/skos/core#> .
      @prefix xsd: <http://www.w3.org/2001/XMLSchema#> .

      :Category a skos:Concept ;
          skos:prefLabel "Category"@en .

      :Account a owl:Class ;
          rdfs:subClassOf :Category ;
          owl:hasKey ( :account_id ) .
      :account_id a owl:DatatypeProperty ;
          rdfs:domain :Account ;
          rdfs:range xsd:string .
    """
        )
    )
    yaml_text, _ = import_owl(
        [ttl],
        include_namespaces=["http://example.com/test#"],
    )
    data = yaml.safe_load(yaml_text)
    account = next(e for e in data["entities"] if e["name"] == "Account")
    assert account["extends"] == "skos_Category"

    # Round-trip through loader — proves the extends target resolves.
    from bigquery_ontology import load_ontology_from_string

    load_ontology_from_string(yaml_text)


class TestAbstractKeyShapeValidation:
  """Abstract entities/relationships with declared keys must still
  satisfy shape rules."""

  def test_abstract_entity_key_column_must_exist(self):
    from bigquery_ontology import load_ontology_from_string

    yaml_str = textwrap.dedent(
        """\
      ontology: test
      entities:
        - name: Real
          keys: {primary: [id]}
          properties:
            - {name: id, type: string}
        - name: skos_Bad
          abstract: true
          keys:
            primary: [nonexistent]
    """
    )
    with pytest.raises(ValueError, match="nonexistent"):
      load_ontology_from_string(yaml_str)

  def test_abstract_entity_forbids_additional_keys(self):
    from bigquery_ontology import load_ontology_from_string

    yaml_str = textwrap.dedent(
        """\
      ontology: test
      entities:
        - name: Real
          keys: {primary: [id]}
          properties:
            - {name: id, type: string}
        - name: skos_Bad
          abstract: true
          properties:
            - {name: x, type: string}
          keys:
            primary: [x]
            additional: [x]
    """
    )
    with pytest.raises(ValueError, match="additional is not allowed"):
      load_ontology_from_string(yaml_str)

  def test_abstract_relationship_cannot_use_extends(self):
    from bigquery_ontology import load_ontology_from_string

    yaml_str = textwrap.dedent(
        """\
      ontology: test
      entities:
        - name: A
          keys: {primary: [id]}
          properties:
            - {name: id, type: string}
        - name: B
          keys: {primary: [id]}
          properties:
            - {name: id, type: string}
      relationships:
        - name: parentRel
          from: A
          to: B
        - name: childRel
          abstract: true
          extends: parentRel
          from: A
          to: B
    """
    )
    with pytest.raises(ValueError, match="abstract.*must not use 'extends'"):
      load_ontology_from_string(yaml_str)


class TestRoundTrip:
  """Emitted YAML with abstract elements loads and validates."""

  def test_pure_skos_roundtrip(self):
    from bigquery_ontology import load_ontology_from_string

    yaml_text, _ = import_owl(
        [_SKOS_TTL],
        include_namespaces=["http://example.com/taxonomy#"],
    )
    ont = load_ontology_from_string(yaml_text)
    assert all(e.abstract for e in ont.entities)
    assert all(r.abstract for r in ont.relationships)

  def test_mixed_owl_skos_roundtrip(self):
    from bigquery_ontology import load_ontology_from_string

    yaml_text, _ = import_owl(
        [_MIXED_TTL],
        include_namespaces=["http://example.com/finance#"],
    )
    ont = load_ontology_from_string(yaml_text)
    concrete_entities = [e for e in ont.entities if not e.abstract]
    abstract_entities = [e for e in ont.entities if e.abstract]
    assert len(concrete_entities) >= 2  # Account, Ledger
    assert len(abstract_entities) >= 1  # skos_FinancialProduct

  def test_concrete_entity_no_abstract_key_emitted(self, tmp_path):
    """Concrete entities must not emit ``abstract: false`` (default is
    False; emitting the default bloats every real-world ontology file)."""
    ttl = tmp_path / "simple.ttl"
    ttl.write_text(
        textwrap.dedent(
            """\
      @prefix : <http://example.com/t#> .
      @prefix owl: <http://www.w3.org/2002/07/owl#> .
      @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
      @prefix xsd: <http://www.w3.org/2001/XMLSchema#> .

      :A a owl:Class ; owl:hasKey ( :a_id ) .
      :a_id a owl:DatatypeProperty ;
          rdfs:domain :A ; rdfs:range xsd:string .
    """
        )
    )
    yaml_text, _ = import_owl(
        [ttl], include_namespaces=["http://example.com/t#"]
    )
    assert "abstract: false" not in yaml_text

  def test_omitted_abstract_key_loads_as_false(self):
    """YAML that omits ``abstract`` must parse back to ``abstract=False``,
    so the emitter's suppression of the default is safe."""
    from bigquery_ontology import load_ontology_from_string

    ont = load_ontology_from_string(
        textwrap.dedent(
            """\
      ontology: t
      entities:
        - name: A
          keys: {primary: [id]}
          properties:
            - {name: id, type: string}
      relationships:
        - name: rel
          from: A
          to: A
    """
        )
    )
    assert ont.entities[0].abstract is False
    assert ont.relationships[0].abstract is False


class TestSkosSchemeAnnotationPreservation:
  """``skos:inScheme`` / ``skos:topConceptOf`` must preserve full IRIs."""

  def test_in_scheme_preserves_full_iri(self, tmp_path):
    ttl = tmp_path / "schemes.ttl"
    ttl.write_text(
        textwrap.dedent(
            """\
      @prefix : <http://example.com/tax#> .
      @prefix skos: <http://www.w3.org/2004/02/skos/core#> .

      :c1 a skos:Concept ;
          skos:inScheme <http://ns1.example/Scheme> ,
                        <http://ns2.example/Scheme> ;
          skos:prefLabel "C1"@en .
    """
        )
    )
    yaml_text, _ = import_owl(
        [ttl], include_namespaces=["http://example.com/tax#"]
    )
    data = yaml.safe_load(yaml_text)
    concept = next(e for e in data["entities"] if e["name"] == "skos_c1")
    schemes = concept["annotations"]["skos:inScheme"]
    # Both full IRIs preserved, not collapsed to the shared local name.
    assert "http://ns1.example/Scheme" in schemes
    assert "http://ns2.example/Scheme" in schemes

  def test_top_concept_of_preserves_full_iri(self, tmp_path):
    ttl = tmp_path / "top.ttl"
    ttl.write_text(
        textwrap.dedent(
            """\
      @prefix : <http://example.com/tax#> .
      @prefix skos: <http://www.w3.org/2004/02/skos/core#> .

      :c1 a skos:Concept ;
          skos:topConceptOf <http://ns1.example/Scheme> ;
          skos:prefLabel "C1"@en .
    """
        )
    )
    yaml_text, _ = import_owl(
        [ttl], include_namespaces=["http://example.com/tax#"]
    )
    data = yaml.safe_load(yaml_text)
    concept = next(e for e in data["entities"] if e["name"] == "skos_c1")
    assert (
        concept["annotations"]["skos:topConceptOf"]
        == "http://ns1.example/Scheme"
    )


class TestMultiLabelNonSelectedLanguage:
  """Multiple labels in the same non-selected (predicate, language)
  must be preserved — not silently overwritten."""

  def test_two_french_alt_labels_both_preserved(self, tmp_path):
    ttl = tmp_path / "multi.ttl"
    ttl.write_text(
        textwrap.dedent(
            """\
      @prefix : <http://example.com/tax#> .
      @prefix skos: <http://www.w3.org/2004/02/skos/core#> .

      :c1 a skos:Concept ;
          skos:prefLabel "C1"@en ;
          skos:altLabel "alpha-fr"@fr , "beta-fr"@fr ;
          skos:hiddenLabel "hidden-fr"@fr .
    """
        )
    )
    yaml_text, _ = import_owl(
        [ttl], include_namespaces=["http://example.com/tax#"]
    )
    data = yaml.safe_load(yaml_text)
    anns = next(e for e in data["entities"] if e["name"] == "skos_c1")[
        "annotations"
    ]
    # Both French altLabels survive as a list, not collapsed to one.
    assert sorted(anns["skos:altLabel@fr"]) == ["alpha-fr", "beta-fr"]
    assert anns["skos:hiddenLabel@fr"] == "hidden-fr"


class TestGenericAnnotationNamespacePrefix:
  """Generic literal-annotation keys must retain their namespace prefix
  so vocabularies with colliding local names don't merge."""

  def test_dc_and_dcterms_title_do_not_collide(self, tmp_path):
    ttl = tmp_path / "dc.ttl"
    ttl.write_text(
        textwrap.dedent(
            """\
      @prefix : <http://example.com/t#> .
      @prefix owl: <http://www.w3.org/2002/07/owl#> .
      @prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
      @prefix xsd: <http://www.w3.org/2001/XMLSchema#> .
      @prefix dc: <http://purl.org/dc/elements/1.1/> .
      @prefix dcterms: <http://purl.org/dc/terms/> .

      :A a owl:Class ;
          owl:hasKey ( :a_id ) ;
          dc:title "DC Title" ;
          dcterms:title "DCterms Title" .
      :a_id a owl:DatatypeProperty ;
          rdfs:domain :A ; rdfs:range xsd:string .
    """
        )
    )
    yaml_text, _ = import_owl(
        [ttl], include_namespaces=["http://example.com/t#"]
    )
    data = yaml.safe_load(yaml_text)
    anns = next(e for e in data["entities"] if e["name"] == "A")["annotations"]
    # Keys retain their prefix and don't collapse to a single "title".
    assert anns.get("dc:title") == "DC Title"
    assert anns.get("dcterms:title") == "DCterms Title"
    assert "title" not in anns
