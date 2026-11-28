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

"""Tests for ``bigquery_ontology.dataplex_glossary_importer``.

Everything runs offline: :func:`map_dataplex_glossary` is pure, and
:func:`import_dataplex_glossary` is driven by an in-memory fake that
implements the client protocol with the same ``request={...}`` call shape
the real GAPIC client accepts, so neither GCP credentials nor
``google-cloud-dataplex`` are needed. Coverage:

* Mapping: glossary root, categories and terms as abstract SKOS
  entities, hierarchy (top concepts, ``skos_broader``, orphan parents),
  project matching (exact, unless a project ID / project number pair is
  verified, by ``GetGlossary`` or as ``project_alias``).
* Naming: kind-prefixed names, sanitization, category/term pairs that
  normalize alike, same-kind hash-suffixed collisions.
* Records: repeated pages, conflicting records, malformed records.
* API orchestration: request shape, canonical names and the verified
  project pair, nameless responses, error wrapping, pages with
  unreachable locations or without items, item-iterable clients,
  missing dependency, lazy client construction.
* YAML: quoting, multiline / Unicode round-trips, byte-identical output
  and ``compile_fingerprint`` across shuffled and repeated pages.
* Downstream parity: ``concept_index.build_rows``,
  ``compile_concept_index``, ``OntologyRuntime`` and
  ``LabelSynonymResolver``.
"""

from __future__ import annotations

import ast
import dataclasses
import hashlib
import pathlib
import random
import sys
import types
from typing import Any
from unittest import mock

import pytest

from bigquery_ontology import dataplex_glossary_importer as importer
from bigquery_ontology import load_binding_from_string
from bigquery_ontology import load_ontology_from_string
from bigquery_ontology._fingerprint import compile_fingerprint
from bigquery_ontology._fingerprint import fingerprint_model
from bigquery_ontology.concept_index import build_rows
from bigquery_ontology.dataplex_glossary_importer import DataplexImportError
from bigquery_ontology.dataplex_glossary_importer import import_dataplex_glossary
from bigquery_ontology.dataplex_glossary_importer import map_dataplex_glossary
from bigquery_ontology.dataplex_glossary_importer import MissingDataplexDependencyError
from bigquery_ontology.dataplex_glossary_importer import validate_import_arguments
from bigquery_ontology.graph_ddl_compiler import compile_concept_index

# --------------------------------------------------------------------- #
# Resource names                                                         #
# --------------------------------------------------------------------- #

_PROJECT = "demo-project"
_PROJECT_NUMBER = "123456789012"
_LOCATION = "us-central1"
_GLOSSARY_ID = "retail-glossary"
_GLOSSARY = (
    f"projects/{_PROJECT}/locations/{_LOCATION}/glossaries/{_GLOSSARY_ID}"
)
_GLOSSARY_BY_NUMBER = (
    f"projects/{_PROJECT_NUMBER}/locations/{_LOCATION}/glossaries/"
    f"{_GLOSSARY_ID}"
)
_OTHER_GLOSSARY = (
    f"projects/{_PROJECT}/locations/{_LOCATION}/glossaries/finance"
)

_COMPILER_VERSION = "test-compiler 0.0.1"
_CONCEPT_INDEX_TABLE = "demo-project.glossary.retail_concept_index"


def _category_name(category_id: str, glossary: str = _GLOSSARY) -> str:
  return f"{glossary}/categories/{category_id}"


def _term_name(term_id: str, glossary: str = _GLOSSARY) -> str:
  return f"{glossary}/terms/{term_id}"


def _hash8(raw_id: str) -> str:
  return hashlib.sha256(raw_id.encode("utf-8")).hexdigest()[:8]


# --------------------------------------------------------------------- #
# Records and fake client                                                #
# --------------------------------------------------------------------- #


@dataclasses.dataclass
class FakeGlossary:
  name: str = _GLOSSARY
  uid: str = ""
  display_name: str = ""
  description: str = ""


@dataclasses.dataclass
class FakeConcept:
  """Stands in for ``GlossaryCategory`` / ``GlossaryTerm``."""

  name: str
  parent: str = _GLOSSARY
  display_name: str = ""
  description: str = ""
  uid: str = ""


def _category(category_id: str, **kwargs: Any) -> FakeConcept:
  return FakeConcept(name=_category_name(category_id), **kwargs)


def _term(term_id: str, **kwargs: Any) -> FakeConcept:
  return FakeConcept(name=_term_name(term_id), **kwargs)


@dataclasses.dataclass
class FakePage:
  """A ``List...Response`` page, as a GAPIC pager's ``pages`` yields it."""

  categories: list = dataclasses.field(default_factory=list)
  terms: list = dataclasses.field(default_factory=list)
  unreachable_locations: list = dataclasses.field(default_factory=list)


class FakePager:
  """A GAPIC-style pager: ``pages`` yields the responses lazily, while
  iterating the pager yields only items (and so hides
  ``unreachable_locations``)."""

  def __init__(self, pages: list[FakePage], items_attr: str) -> None:
    self._pages = pages
    self._items_attr = items_attr

  @property
  def pages(self):
    yield from self._pages

  def __iter__(self):
    for page in self.pages:
      yield from getattr(page, self._items_attr)


class FakeGlossaryClient:
  """In-memory ``BusinessGlossaryServiceClient``.

  List calls return a lazy generator over pre-built pages, like a GAPIC
  pager, so a test can shuffle, repeat, or break pages. Pages given as
  :class:`FakePage` objects come back as a :class:`FakePager` instead.
  """

  def __init__(
      self,
      *,
      glossary: FakeGlossary | None = None,
      categories: list | tuple = (),
      terms: list | tuple = (),
      category_pages: list[Any] | None = None,
      term_pages: list[Any] | None = None,
      errors: dict[str, Exception] | None = None,
  ) -> None:
    self.glossary = glossary or FakeGlossary()
    self.category_pages = (
        category_pages if category_pages is not None else [list(categories)]
    )
    self.term_pages = term_pages if term_pages is not None else [list(terms)]
    self.errors = errors or {}
    self.calls: list[tuple[str, Any]] = []

  def _record(self, method: str, request: Any) -> None:
    self.calls.append((method, request))
    if method in self.errors:
      raise self.errors[method]

  @staticmethod
  def _pager(pages, items_attr):
    if any(isinstance(page, FakePage) for page in pages):
      return FakePager(pages, items_attr)
    return (item for page in pages for item in page)

  def get_glossary(self, request=None, **kwargs):
    self._record("get_glossary", request)
    return self.glossary

  def list_glossary_categories(self, request=None, **kwargs):
    self._record("list_glossary_categories", request)
    return self._pager(self.category_pages, "categories")

  def list_glossary_terms(self, request=None, **kwargs):
    self._record("list_glossary_terms", request)
    return self._pager(self.term_pages, "terms")


class PermissionDenied(Exception):
  """Name-compatible with ``google.api_core.exceptions.PermissionDenied``."""

  code = 403


class Unauthenticated(Exception):
  code = 401


def _map(categories=(), terms=(), *, glossary=None, **kwargs):
  return map_dataplex_glossary(
      glossary or FakeGlossary(), categories, terms, **kwargs
  )


def _entity(ontology, name: str):
  matches = [e for e in ontology.entities if e.name == name]
  assert len(matches) == 1, [e.name for e in ontology.entities]
  return matches[0]


def _relationships(ontology) -> list[tuple[str, str, str]]:
  return [(r.name, r.from_, r.to) for r in ontology.relationships]


def _notations(ontology) -> dict[str, str]:
  return {e.name: e.annotations["skos:notation"] for e in ontology.entities}


def _binding_for(ontology):
  """A binding with no entity bindings; abstract entities need none."""
  return load_binding_from_string(
      f"""\
binding: {ontology.ontology}_bq
ontology: {ontology.ontology}
target:
  backend: bigquery
  project: demo-project
  dataset: glossary
""",
      ontology=ontology,
  )


# --------------------------------------------------------------------- #
# A small nested retail glossary                                         #
# --------------------------------------------------------------------- #


def _retail_glossary() -> FakeGlossary:
  return FakeGlossary(
      name=_GLOSSARY,
      uid="7f3c2a10-uid",
      display_name="Retail Glossary",
      description="  Shared retail vocabulary.  ",
  )


def _retail_categories() -> list[FakeConcept]:
  return [
      _category("customer", display_name="Customer"),
      _category(
          "metrics",
          parent=_category_name("customer"),
          display_name="Metrics",
          description="Business KPIs.",
      ),
  ]


def _retail_terms() -> list[FakeConcept]:
  return [
      _term(
          "clv-2024",
          parent=_category_name("metrics"),
          display_name="Customer Lifetime Value",
          description="Projected revenue from one customer.",
      ),
      _term("churn", parent=_category_name("customer"), display_name="Churn"),
      _term("retention", parent=_GLOSSARY, display_name="Retention"),
  ]


def _map_retail():
  return map_dataplex_glossary(
      _retail_glossary(), _retail_categories(), _retail_terms()
  )


_RETAIL_EXPECTED_YAML = """\
ontology: retail_glossary
description: Shared retail vocabulary.
annotations:
  dataplex:glossary: projects/demo-project/locations/us-central1/glossaries/retail-glossary
  dataplex:uid: "7f3c2a10-uid"

entities:
  - name: category_customer
    abstract: true
    annotations:
      dataplex:resource: projects/demo-project/locations/us-central1/glossaries/retail-glossary/categories/customer
      skos:inScheme: projects/demo-project/locations/us-central1/glossaries/retail-glossary
      skos:notation: customer
      skos:prefLabel: Customer
      skos:topConceptOf: projects/demo-project/locations/us-central1/glossaries/retail-glossary
  - name: category_metrics
    abstract: true
    description: Business KPIs.
    annotations:
      dataplex:resource: projects/demo-project/locations/us-central1/glossaries/retail-glossary/categories/metrics
      skos:inScheme: projects/demo-project/locations/us-central1/glossaries/retail-glossary
      skos:notation: metrics
      skos:prefLabel: Metrics
  - name: term_churn
    abstract: true
    annotations:
      dataplex:resource: projects/demo-project/locations/us-central1/glossaries/retail-glossary/terms/churn
      skos:inScheme: projects/demo-project/locations/us-central1/glossaries/retail-glossary
      skos:notation: churn
      skos:prefLabel: Churn
  - name: term_clv_2024
    abstract: true
    description: Projected revenue from one customer.
    annotations:
      dataplex:resource: projects/demo-project/locations/us-central1/glossaries/retail-glossary/terms/clv-2024
      skos:inScheme: projects/demo-project/locations/us-central1/glossaries/retail-glossary
      skos:notation: clv-2024
      skos:prefLabel: Customer Lifetime Value
  - name: term_retention
    abstract: true
    annotations:
      dataplex:resource: projects/demo-project/locations/us-central1/glossaries/retail-glossary/terms/retention
      skos:inScheme: projects/demo-project/locations/us-central1/glossaries/retail-glossary
      skos:notation: retention
      skos:prefLabel: Retention
      skos:topConceptOf: projects/demo-project/locations/us-central1/glossaries/retail-glossary

relationships:
  - name: skos_broader
    abstract: true
    from: category_metrics
    to: category_customer
  - name: skos_broader
    abstract: true
    from: term_churn
    to: category_customer
  - name: skos_broader
    abstract: true
    from: term_clv_2024
    to: category_metrics
"""

_RETAIL_EXPECTED_SUMMARY = """\
Imported Dataplex glossary: projects/demo-project/locations/us-central1/glossaries/retail-glossary
Ontology name sanitized: 'retail-glossary' -> 'retail_glossary'
Categories imported as abstract entities: 2
Terms imported as abstract entities: 3
SKOS relationships imported as abstract: 3 skos_broader
Resource IDs with characters outside [A-Za-z0-9_] replaced by '_' (raw IDs kept in skos:notation): 1
Note: all entities are abstract (SKOS-only), so nothing is bound to tables. \
`gm compile` and OntologyRuntime still need a companion binding; one with no \
entities is enough for `gm compile --emit-concept-index --concept-index-table \
...`."""


# --------------------------------------------------------------------- #
# map_dataplex_glossary: golden output and root                          #
# --------------------------------------------------------------------- #


def test_retail_glossary_golden_yaml_and_summary():
  yaml_text, summary = _map_retail()

  assert yaml_text == _RETAIL_EXPECTED_YAML
  assert summary == _RETAIL_EXPECTED_SUMMARY
  load_ontology_from_string(yaml_text)


def test_root_metadata_maps_to_ontology_fields():
  ontology = load_ontology_from_string(_map_retail()[0])

  assert ontology.ontology == "retail_glossary"
  assert ontology.description == "Shared retail vocabulary."
  assert ontology.annotations == {
      "dataplex:glossary": _GLOSSARY,
      "dataplex:uid": "7f3c2a10-uid",
  }
  assert ontology.version is None


def test_blank_glossary_metadata_is_omitted():
  glossary = FakeGlossary(uid="  ", display_name="", description="\n\t ")
  ontology = load_ontology_from_string(
      _map(terms=[_term("customer")], glossary=glossary)[0]
  )

  assert ontology.description is None
  assert ontology.annotations == {"dataplex:glossary": _GLOSSARY}


def test_records_may_be_mappings_and_generators():
  glossary = {"name": _GLOSSARY, "uid": "u-1", "description": "Retail."}
  terms = (
      {"name": _term(t).name, "parent": _GLOSSARY, "display_name": t.title()}
      for t in ("churn", "retention")
  )
  ontology = load_ontology_from_string(
      map_dataplex_glossary(glossary, [], terms)[0]
  )

  assert [e.name for e in ontology.entities] == ["term_churn", "term_retention"]
  assert _entity(ontology, "term_churn").annotations["skos:prefLabel"] == (
      "Churn"
  )


# --------------------------------------------------------------------- #
# Entities and hierarchy                                                 #
# --------------------------------------------------------------------- #


def test_categories_and_terms_become_abstract_skos_entities():
  ontology = load_ontology_from_string(
      _map(
          categories=[_category("party", display_name=" Party ")],
          terms=[
              _term(
                  "customer",
                  display_name="Customer",
                  description="  A person who buys.\n",
              ),
              _term("anonymous", display_name="   ", description="   "),
          ],
      )[0]
  )

  assert [e.name for e in ontology.entities] == [
      "category_party",
      "term_anonymous",
      "term_customer",
  ]
  assert all(e.abstract for e in ontology.entities)
  assert all(not e.properties and e.keys is None for e in ontology.entities)
  customer = _entity(ontology, "term_customer")
  assert customer.description == "A person who buys."
  assert customer.annotations == {
      "dataplex:resource": _term_name("customer"),
      "skos:inScheme": _GLOSSARY,
      "skos:notation": "customer",
      "skos:prefLabel": "Customer",
      "skos:topConceptOf": _GLOSSARY,
  }
  assert _entity(ontology, "category_party").annotations["skos:prefLabel"] == (
      "Party"
  )
  anonymous = _entity(ontology, "term_anonymous")
  assert anonymous.description is None
  assert "skos:prefLabel" not in anonymous.annotations


def test_top_level_and_nested_concepts():
  ontology = load_ontology_from_string(_map_retail()[0])

  for name in ("category_customer", "term_retention"):
    annotations = _entity(ontology, name).annotations
    assert annotations["skos:topConceptOf"] == _GLOSSARY
    assert annotations["skos:inScheme"] == _GLOSSARY
  for name in ("category_metrics", "term_clv_2024", "term_churn"):
    annotations = _entity(ontology, name).annotations
    assert annotations["skos:inScheme"] == _GLOSSARY
    assert "skos:topConceptOf" not in annotations
  assert _relationships(ontology) == [
      ("skos_broader", "category_metrics", "category_customer"),
      ("skos_broader", "term_churn", "category_customer"),
      ("skos_broader", "term_clv_2024", "category_metrics"),
  ]
  assert all(r.abstract for r in ontology.relationships)


def test_relationships_are_sorted_by_child_whatever_the_input_order():
  # Twelve edges whose parents cycle through three categories, fed in reverse
  # order. The expected order (by child, so not grouped by parent) is neither
  # the input order nor the hash order of the edge set: an unsorted emission
  # would match it only with probability 1/12!.
  parents = ["c", "a", "b"]
  terms = [
      _term(f"t{i:02d}", parent=_category_name(parents[i % 3]))
      for i in reversed(range(12))
  ]

  ontology = load_ontology_from_string(
      _map(categories=[_category(p) for p in parents], terms=terms)[0]
  )

  assert _relationships(ontology) == [
      ("skos_broader", f"term_t{i:02d}", f"category_{parents[i % 3]}")
      for i in range(12)
  ]


def test_project_alias_admits_the_other_form_of_the_glossary_project():
  """With a verified pair, records and parents may name the glossary's
  project by ID or by number."""
  glossary = FakeGlossary(name=_GLOSSARY_BY_NUMBER)
  ontology = load_ontology_from_string(
      _map(
          categories=[
              FakeConcept(
                  name=_category_name("customer", _GLOSSARY_BY_NUMBER),
                  parent=_GLOSSARY,
              )
          ],
          terms=[
              FakeConcept(
                  name=_term_name("buyer", _GLOSSARY_BY_NUMBER),
                  parent=_category_name("customer", _GLOSSARY),
              ),
              FakeConcept(
                  name=_term_name("visit", _GLOSSARY),
                  parent=_GLOSSARY_BY_NUMBER,
              ),
          ],
          glossary=glossary,
          project_alias=_PROJECT,
      )[0]
  )

  assert ontology.annotations["dataplex:glossary"] == _GLOSSARY_BY_NUMBER
  for name in ("category_customer", "term_visit"):
    annotations = _entity(ontology, name).annotations
    assert annotations["skos:topConceptOf"] == _GLOSSARY_BY_NUMBER
    assert annotations["skos:inScheme"] == _GLOSSARY_BY_NUMBER
  assert _relationships(ontology) == [
      ("skos_broader", "term_buyer", "category_customer")
  ]
  # The resource name is kept exactly as returned.
  assert _entity(ontology, "term_visit").annotations["dataplex:resource"] == (
      _term_name("visit", _GLOSSARY)
  )


def test_without_project_alias_the_other_form_is_another_project():
  """An ID and a number can't be compared offline, so without a verified
  pair they never match; the error and the summary say how to pair them."""
  with pytest.raises(ValueError, match="does not belong to glossary") as info:
    _map(
        terms=[FakeConcept(name=_term_name("visit", _GLOSSARY))],
        glossary=FakeGlossary(name=_GLOSSARY_BY_NUMBER),
    )
  assert f"import {_GLOSSARY} instead" in str(info.value)

  yaml_text, summary = _map(
      categories=[
          FakeConcept(
              name=_category_name("customer", _GLOSSARY_BY_NUMBER),
              parent=_GLOSSARY,
          )
      ],
      terms=[
          FakeConcept(
              name=_term_name("buyer", _GLOSSARY_BY_NUMBER),
              parent=_category_name("customer", _GLOSSARY),
          ),
          FakeConcept(
              name=_term_name("ghost", _GLOSSARY_BY_NUMBER),
              parent=_category_name("missing", _GLOSSARY),
          ),
      ],
      glossary=FakeGlossary(name=_GLOSSARY_BY_NUMBER),
  )
  ontology = load_ontology_from_string(yaml_text)

  assert ontology.relationships == []
  expected_orphans = {
      "category_customer": _GLOSSARY,
      "term_buyer": _category_name("customer", _GLOSSARY),
      "term_ghost": _category_name("missing", _GLOSSARY),
  }
  for name, parent in expected_orphans.items():
    annotations = _entity(ontology, name).annotations
    assert annotations["dataplex:orphan_parent"] == parent
    assert "skos:topConceptOf" not in annotations
  lines = summary.splitlines()
  assert (
      "Orphan parents (entity kept in the scheme without a broader edge; "
      "parent kept as dataplex:orphan_parent): 3" in lines
  )
  # ``missing`` is no category of this glossary, so pairing wouldn't help.
  assert (
      "Orphan parents naming this glossary under an unverified project ID "
      "or number (if it is the same project, import the glossary by that "
      "form to pair them): 2" in lines
  )


def test_orphan_parents_are_preserved_and_reported():
  unknown_category = _category_name("ghost")
  other_glossary_parent = _category_name("party", _OTHER_GLOSSARY)
  yaml_text, summary = _map(
      categories=[
          _category("party"),
          _category("loop", parent=_category_name("loop")),
      ],
      terms=[
          _term("a", parent=unknown_category),
          _term("b", parent=other_glossary_parent),
          _term("c", parent=""),
          _term("d", parent=_term_name("a")),
      ],
  )
  ontology = load_ontology_from_string(yaml_text)

  expected_orphans = {
      "term_a": unknown_category,
      "term_b": other_glossary_parent,
      "term_d": _term_name("a"),
      "category_loop": _category_name("loop"),
  }
  for name, parent in expected_orphans.items():
    annotations = _entity(ontology, name).annotations
    assert annotations["dataplex:orphan_parent"] == parent
    assert annotations["skos:inScheme"] == _GLOSSARY
    assert "skos:topConceptOf" not in annotations
  blank = _entity(ontology, "term_c").annotations
  assert "dataplex:orphan_parent" not in blank
  assert blank["skos:inScheme"] == _GLOSSARY
  assert ontology.relationships == []
  assert (
      "Orphan parents (entity kept in the scheme without a broader edge; "
      "parent kept as dataplex:orphan_parent): 5" in summary.splitlines()
  )


# Two different project IDs, or two different project numbers, are always
# two projects (an ID and a number match only through ``project_alias``).
_OTHER_PROJECT_PAIRS = pytest.mark.parametrize(
    "glossary_project, other_project",
    [("proj-a", "proj-b"), ("111111", "222222")],
    ids=["project_ids", "project_numbers"],
)


def _glossary_in(project: str) -> str:
  return f"projects/{project}/locations/{_LOCATION}/glossaries/{_GLOSSARY_ID}"


@_OTHER_PROJECT_PAIRS
def test_parents_in_another_project_are_orphans(
    glossary_project, other_project
):
  glossary = _glossary_in(glossary_project)
  other_glossary = _glossary_in(other_project)
  # Same location, glossary ID and category ID as this glossary's own
  # ``customer`` category; only the project differs.
  other_customer = _category_name("customer", other_glossary)
  yaml_text, summary = _map(
      categories=[
          FakeConcept(
              name=_category_name("customer", glossary), parent=glossary
          ),
          FakeConcept(
              name=_category_name("metrics", glossary), parent=other_customer
          ),
      ],
      terms=[
          FakeConcept(name=_term_name("t1", glossary), parent=other_glossary),
          FakeConcept(name=_term_name("t2", glossary), parent=other_customer),
      ],
      glossary=FakeGlossary(name=glossary),
  )
  ontology = load_ontology_from_string(yaml_text)

  customer = _entity(ontology, "category_customer").annotations
  assert customer["skos:topConceptOf"] == glossary
  expected_orphans = {
      "category_metrics": other_customer,
      "term_t1": other_glossary,
      "term_t2": other_customer,
  }
  for name, parent in expected_orphans.items():
    annotations = _entity(ontology, name).annotations
    assert annotations["dataplex:orphan_parent"] == parent
    assert annotations["skos:inScheme"] == glossary
    assert "skos:topConceptOf" not in annotations
  assert ontology.relationships == []
  assert (
      "Orphan parents (entity kept in the scheme without a broader edge; "
      "parent kept as dataplex:orphan_parent): 3" in summary.splitlines()
  )


@_OTHER_PROJECT_PAIRS
def test_records_from_another_project_are_rejected(
    glossary_project, other_project
):
  record = FakeConcept(
      name=_term_name("t1", _glossary_in(other_project)),
      parent=_glossary_in(other_project),
  )

  with pytest.raises(ValueError, match="does not belong to glossary"):
    _map(
        terms=[record],
        glossary=FakeGlossary(name=_glossary_in(glossary_project)),
    )


def _glossary_in_location(location: str) -> str:
  return f"projects/{_PROJECT}/locations/{location}/glossaries/{_GLOSSARY_ID}"


def test_parents_in_another_location_are_orphans():
  other_glossary = _glossary_in_location("europe-west1")
  # Same project, glossary ID and category ID as this glossary's own
  # ``customer`` category; only the location differs.
  other_customer = _category_name("customer", other_glossary)
  yaml_text, summary = _map(
      categories=[
          _category("customer"),
          _category("metrics", parent=other_customer),
      ],
      terms=[
          _term("t1", parent=other_glossary),
          _term("t2", parent=other_customer),
      ],
  )
  ontology = load_ontology_from_string(yaml_text)

  customer = _entity(ontology, "category_customer").annotations
  assert customer["skos:topConceptOf"] == _GLOSSARY
  expected_orphans = {
      "category_metrics": other_customer,
      "term_t1": other_glossary,
      "term_t2": other_customer,
  }
  for name, parent in expected_orphans.items():
    annotations = _entity(ontology, name).annotations
    assert annotations["dataplex:orphan_parent"] == parent
    assert annotations["skos:inScheme"] == _GLOSSARY
    assert "skos:topConceptOf" not in annotations
  assert ontology.relationships == []
  assert (
      "Orphan parents (entity kept in the scheme without a broader edge; "
      "parent kept as dataplex:orphan_parent): 3" in summary.splitlines()
  )


@pytest.mark.parametrize(
    "kind, collection", [("category", "categories"), ("term", "terms")]
)
def test_records_from_another_location_are_rejected(kind, collection):
  other_glossary = _glossary_in_location("europe-west1")
  record = FakeConcept(
      name=f"{other_glossary}/{collection}/x", parent=other_glossary
  )

  with pytest.raises(ValueError, match="does not belong to glossary"):
    _map(**{collection: [record]})


@pytest.mark.parametrize(
    "glossary_project, project_alias, expected",
    [
        ("demo-project", None, {"demo-project"}),
        ("123456789012", None, {"123456789012"}),
        ("demo-project", "demo-project", {"demo-project"}),
        ("demo-project", "123456789012", {"demo-project", "123456789012"}),
        ("123456789012", "demo-project", {"demo-project", "123456789012"}),
        (
            "example.com:demo",
            "123456789012",
            {"example.com:demo", "123456789012"},
        ),
    ],
)
def test_projects_match_exactly_unless_a_verified_alias_is_given(
    glossary_project, project_alias, expected
):
  glossary = importer._parse_glossary_name(_glossary_in(glossary_project))

  assert importer._allowed_projects(glossary, project_alias) == expected


@pytest.mark.parametrize(
    "glossary_name, project_alias",
    [
        (_GLOSSARY, "project-b"),  # Two different project IDs.
        (_GLOSSARY_BY_NUMBER, "999999999999"),  # Two different numbers.
        (_GLOSSARY, ""),
        (_GLOSSARY, "bad!project"),
        (_GLOSSARY, " 123456789012"),
        (_GLOSSARY, 123456789012),
    ],
)
def test_invalid_project_alias_is_rejected(glossary_name, project_alias):
  with pytest.raises(ValueError, match="project_alias"):
    _map(
        terms=[FakeConcept(name=_term_name("t1", glossary_name))],
        glossary=FakeGlossary(name=glossary_name),
        project_alias=project_alias,
    )


@pytest.mark.parametrize("third_project", ["project-b", "999999999999"])
def test_project_alias_still_rejects_records_from_a_third_project(
    third_project,
):
  record = FakeConcept(name=_term_name("t1", _glossary_in(third_project)))

  with pytest.raises(ValueError, match="does not belong to glossary") as info:
    _map(
        terms=[record],
        glossary=FakeGlossary(name=_GLOSSARY_BY_NUMBER),
        project_alias=_PROJECT,
    )
  # Both forms of the glossary's project are known, so no pairing hint.
  assert "instead" not in str(info.value)


@pytest.mark.parametrize("third_project", ["project-b", "999999999999"])
def test_project_alias_still_orphans_parents_in_a_third_project(
    third_project,
):
  other_glossary = _glossary_in(third_project)
  other_customer = _category_name("customer", other_glossary)
  yaml_text, summary = _map(
      categories=[
          FakeConcept(
              name=_category_name("customer", _GLOSSARY_BY_NUMBER),
              parent=_GLOSSARY,
          )
      ],
      terms=[
          FakeConcept(
              name=_term_name("t1", _GLOSSARY_BY_NUMBER), parent=other_glossary
          ),
          FakeConcept(
              name=_term_name("t2", _GLOSSARY_BY_NUMBER), parent=other_customer
          ),
      ],
      glossary=FakeGlossary(name=_GLOSSARY_BY_NUMBER),
      project_alias=_PROJECT,
  )
  ontology = load_ontology_from_string(yaml_text)

  customer = _entity(ontology, "category_customer").annotations
  assert customer["skos:topConceptOf"] == _GLOSSARY_BY_NUMBER
  assert ontology.relationships == []
  for name, parent in {
      "term_t1": other_glossary,
      "term_t2": other_customer,
  }.items():
    assert _entity(ontology, name).annotations["dataplex:orphan_parent"] == (
        parent
    )
  lines = summary.splitlines()
  assert (
      "Orphan parents (entity kept in the scheme without a broader edge; "
      "parent kept as dataplex:orphan_parent): 2" in lines
  )
  assert not any("unverified project" in line for line in lines)


# A glossary named by project ID must not absorb records that name an
# unrelated project number, whatever the ID is.
@pytest.mark.parametrize("glossary_project", ["project-a", "project-b"])
def test_record_in_an_unrelated_project_number_is_rejected(glossary_project):
  foreign_glossary = _glossary_in(_PROJECT_NUMBER)
  record = FakeConcept(
      name=_term_name("foreign", foreign_glossary), parent=foreign_glossary
  )

  with pytest.raises(ValueError, match="does not belong to glossary"):
    _map(
        terms=[record],
        glossary=FakeGlossary(name=_glossary_in(glossary_project)),
    )


@pytest.mark.parametrize("glossary_project", ["project-a", "project-b"])
def test_parent_in_an_unrelated_project_number_stays_an_orphan(
    glossary_project,
):
  glossary = _glossary_in(glossary_project)
  foreign_customer = _category_name("customer", _glossary_in(_PROJECT_NUMBER))
  yaml_text, summary = _map(
      categories=[
          FakeConcept(
              name=_category_name("customer", glossary), parent=glossary
          )
      ],
      terms=[
          FakeConcept(
              name=_term_name("churn", glossary), parent=foreign_customer
          )
      ],
      glossary=FakeGlossary(name=glossary),
  )
  ontology = load_ontology_from_string(yaml_text)

  assert ontology.relationships == []
  annotations = _entity(ontology, "term_churn").annotations
  assert annotations["dataplex:orphan_parent"] == foreign_customer
  assert any(
      line.startswith("Orphan parents naming this glossary under an unverified")
      for line in summary.splitlines()
  )


def test_term_under_a_category_with_the_same_id():
  ontology = load_ontology_from_string(
      _map(
          categories=[_category("customer")],
          terms=[_term("customer", parent=_category_name("customer"))],
      )[0]
  )

  assert _relationships(ontology) == [
      ("skos_broader", "term_customer", "category_customer")
  ]


# --------------------------------------------------------------------- #
# Entity naming                                                          #
# --------------------------------------------------------------------- #


def test_names_are_kind_prefixed_and_sanitized():
  yaml_text, summary = _map(
      categories=[_category("kpis.v2")],
      terms=[
          _term("2026-q1"),
          _term("caf\u00e9"),
          _term("plain_id"),
          _term("skos_broader"),
      ],
  )
  ontology = load_ontology_from_string(yaml_text)

  assert _notations(ontology) == {
      "category_kpis_v2": "kpis.v2",
      "term_2026_q1": "2026-q1",
      "term_caf_": "caf\u00e9",
      "term_plain_id": "plain_id",
      "term_skos_broader": "skos_broader",
  }
  assert (
      "Resource IDs with characters outside [A-Za-z0-9_] replaced by '_' "
      "(raw IDs kept in skos:notation): 3" in summary.splitlines()
  )
  assert "hash suffix" not in summary


def test_category_and_term_normalizing_alike_never_collide():
  yaml_text, summary = _map(
      categories=[_category("net-revenue", display_name="Net Revenue")],
      terms=[
          _term(
              "net_revenue",
              parent=_category_name("net-revenue"),
              display_name="Net revenue",
          )
      ],
  )
  ontology = load_ontology_from_string(yaml_text)

  assert _notations(ontology) == {
      "category_net_revenue": "net-revenue",
      "term_net_revenue": "net_revenue",
  }
  assert _relationships(ontology) == [
      ("skos_broader", "term_net_revenue", "category_net_revenue")
  ]
  assert "hash suffix" not in summary


def test_same_kind_collisions_get_order_independent_hash_suffixes():
  records = [_term("net-revenue"), _term("net_revenue"), _term("net.revenue")]
  yaml_text, summary = _map(terms=records)
  ontology = load_ontology_from_string(yaml_text)

  assert _notations(ontology) == {
      f"term_net_revenue_{_hash8('net-revenue')}": "net-revenue",
      f"term_net_revenue_{_hash8('net_revenue')}": "net_revenue",
      f"term_net_revenue_{_hash8('net.revenue')}": "net.revenue",
  }
  assert (
      "Entity names given a hash suffix after a same-kind name collision "
      "(raw IDs kept in skos:notation): 3" in summary.splitlines()
  )
  for order in (records[::-1], [records[1], records[2], records[0]]):
    assert _map(terms=order)[0] == yaml_text


def test_hash_suffixes_never_steal_a_natural_name():
  natural = f"a_b_{_hash8('a-b')}"
  ontology = load_ontology_from_string(
      _map(terms=[_term("a-b"), _term("a.b"), _term(natural)])[0]
  )

  digest_ab = hashlib.sha256(b"a-b").hexdigest()
  digest_adb = hashlib.sha256(b"a.b").hexdigest()
  assert _notations(ontology) == {
      f"term_{natural}": natural,
      f"term_a_b_{digest_ab[:9]}": "a-b",
      f"term_a_b_{digest_adb[:9]}": "a.b",
  }


def test_hash_suffix_falls_back_to_a_counter_when_every_length_is_taken():
  """Natural IDs can hold every suffix length from 8 to 64 hex digits; the
  colliding pair then gets the full digest plus ``_2``, in any order."""
  digest_ab = hashlib.sha256(b"a-b").hexdigest()
  digest_a_b = hashlib.sha256(b"a_b").hexdigest()
  squatters = [_term(f"a_b_{digest_ab[:n]}") for n in range(8, 65)]
  assert len(squatters) == 57

  outputs = set()
  for seed in range(4):
    terms = squatters + [_term("a-b"), _term("a_b")]
    random.Random(seed).shuffle(terms)
    outputs.add(_map(terms=terms)[0])

  assert len(outputs) == 1
  notations = _notations(load_ontology_from_string(outputs.pop()))
  names = {raw_id: name for name, raw_id in notations.items()}
  assert names["a-b"] == f"term_a_b_{digest_ab}_2"
  assert names["a_b"] == f"term_a_b_{digest_a_b}_2"
  for n in range(8, 65):
    assert names[f"a_b_{digest_ab[:n]}"] == f"term_a_b_{digest_ab[:n]}"


def test_counter_skips_names_that_are_already_taken():
  digest_ab = hashlib.sha256(b"a-b").hexdigest()
  squatters = [_term(f"a_b_{digest_ab[:n]}") for n in range(8, 65)]
  squatters.append(_term(f"a_b_{digest_ab}_2"))

  notations = _notations(
      load_ontology_from_string(
          _map(terms=squatters + [_term("a-b"), _term("a_b")])[0]
      )
  )

  assert notations[f"term_a_b_{digest_ab}_3"] == "a-b"
  assert notations[f"term_a_b_{digest_ab}_2"] == f"a_b_{digest_ab}_2"


def test_custom_ontology_name_is_stripped_and_sanitized():
  terms = [_term("customer")]
  yaml_text, summary = _map(terms=terms, ontology_name="  retail-terms v2 ")
  assert load_ontology_from_string(yaml_text).ontology == "retail_terms_v2"
  assert (
      "Ontology name sanitized: 'retail-terms v2' -> 'retail_terms_v2'"
      in summary.splitlines()
  )

  yaml_text, summary = _map(terms=terms, ontology_name="finance")
  assert load_ontology_from_string(yaml_text).ontology == "finance"
  assert "Ontology name sanitized" not in summary

  yaml_text, _ = _map(terms=terms, ontology_name="2026 glossary")
  assert load_ontology_from_string(yaml_text).ontology == "_2026_glossary"


def test_glossary_id_with_a_leading_digit_gets_an_underscore_prefix():
  glossary_name = f"projects/{_PROJECT}/locations/{_LOCATION}/glossaries/2026"
  ontology = load_ontology_from_string(
      _map(
          terms=[FakeConcept(name=f"{glossary_name}/terms/a", parent="")],
          glossary=FakeGlossary(name=glossary_name),
      )[0]
  )

  assert ontology.ontology == "_2026"


def test_yaml_keyword_ontology_name_is_quoted():
  yaml_text, _ = _map(terms=[_term("customer")], ontology_name="yes")

  assert yaml_text.startswith('ontology: "yes"\n')
  assert load_ontology_from_string(yaml_text).ontology == "yes"


@pytest.mark.parametrize("bad_name", ["", "   ", "---", "!!!"])
def test_blank_or_unsanitizable_ontology_name_is_rejected(bad_name):
  with pytest.raises(ValueError, match="ontology_name"):
    _map(terms=[_term("customer")], ontology_name=bad_name)
  with pytest.raises(ValueError, match="ontology_name"):
    validate_import_arguments(_GLOSSARY, ontology_name=bad_name)


# --------------------------------------------------------------------- #
# Records: repeats, conflicts, malformed input                           #
# --------------------------------------------------------------------- #


def test_identical_repeated_records_are_deduplicated():
  baseline_yaml, _ = _map_retail()
  categories = _retail_categories()
  terms = _retail_terms()
  yaml_text, summary = map_dataplex_glossary(
      _retail_glossary(),
      categories + [dataclasses.replace(categories[0])],
      terms + [dataclasses.replace(t) for t in terms],
  )

  assert yaml_text == baseline_yaml
  assert (
      "Identical repeated category/term records deduplicated: 4"
      in summary.splitlines()
  )


def test_whitespace_only_differences_are_not_conflicts():
  yaml_text, _ = _map(
      terms=[
          _term("customer", display_name="Customer"),
          _term("customer", display_name=" Customer \n"),
      ]
  )

  ontology = load_ontology_from_string(yaml_text)
  assert [e.name for e in ontology.entities] == ["term_customer"]


@pytest.mark.parametrize(
    "conflict",
    [
        {"display_name": "Client"},
        {"description": "Someone else."},
        {"parent": _category_name("other")},
        {"name": _term_name("customer", _GLOSSARY_BY_NUMBER)},
    ],
)
def test_conflicting_records_with_the_same_id_are_rejected(conflict):
  first = _term("customer", display_name="Customer", description="A buyer.")
  second = dataclasses.replace(first, **conflict)

  with pytest.raises(ValueError, match="Conflicting records for glossary term"):
    # The alias keeps the number-form name in scope, so it is a conflict.
    _map(terms=[first, second], project_alias=_PROJECT_NUMBER)


def test_same_id_in_different_kinds_is_not_a_conflict():
  ontology = load_ontology_from_string(
      _map(
          categories=[_category("customer", display_name="Customers")],
          terms=[_term("customer", display_name="Customer")],
      )[0]
  )

  assert [e.name for e in ontology.entities] == [
      "category_customer",
      "term_customer",
  ]


@pytest.mark.parametrize(
    "kind, record",
    [
        ("category", FakeConcept(name="categories/broken")),
        ("category", FakeConcept(name=_term_name("not-a-category"))),
        ("term", FakeConcept(name="")),
        ("term", FakeConcept(name=f"{_GLOSSARY}/terms/a/b")),
    ],
)
def test_unparseable_records_are_rejected(kind, record):
  categories = [record] if kind == "category" else []
  terms = [record] if kind == "term" else [_term("ok")]

  with pytest.raises(ValueError, match="unparseable resource name"):
    _map(categories=categories, terms=terms)


def test_records_from_another_glossary_are_rejected():
  with pytest.raises(ValueError, match="does not belong to glossary"):
    _map(terms=[FakeConcept(name=_term_name("client", _OTHER_GLOSSARY))])


def test_empty_glossary_is_a_validation_error():
  with pytest.raises(ValueError, match="no categories or terms"):
    _map()


@pytest.mark.parametrize(
    "bad_name",
    [
        "",
        "retail-glossary",
        "projects/demo-project",
        "projects/demo-project/locations/us-central1/glossaries/",
        "projects//locations/us-central1/glossaries/retail",
        f"{_GLOSSARY}/terms/customer",
        f"{_GLOSSARY}\n",
        f" {_GLOSSARY}",
        "projects/demo project/locations/us-central1/glossaries/retail",
        "locations/us-central1/glossaries/retail",
        # Project and location segments are checked before any API call.
        "projects/demo!/locations/us-central1/glossaries/retail",
        "projects/demo@corp/locations/us-central1/glossaries/retail",
        "projects/-demo/locations/us-central1/glossaries/retail",
        "projects/demo-project/locations/us-central1!/glossaries/retail",
        "projects/demo-project/locations/us@central1/glossaries/retail",
        "projects/demo-project/locations/US-CENTRAL1/glossaries/retail",
        "projects/demo-project/locations/1us/glossaries/retail",
        # A project ID is lowercase, starts with a letter and doesn't end
        # with a hyphen; a domain prefix needs a dotted domain.
        "projects/123abc/locations/us-central1/glossaries/retail",
        "projects/UPPER-project/locations/us-central1/glossaries/retail",
        "projects/Demo-Project/locations/us-central1/glossaries/retail",
        "projects/demo-project-/locations/us-central1/glossaries/retail",
        "projects/a::b/locations/us-central1/glossaries/retail",
        "projects/example:demo/locations/us-central1/glossaries/retail",
        "projects/example.com:/locations/us-central1/glossaries/retail",
        "projects/example.com:Demo/locations/us-central1/glossaries/retail",
        "projects/demo-project/locations/us-central1-/glossaries/retail",
    ],
)
def test_invalid_glossary_resource_name_is_rejected(bad_name):
  with pytest.raises(ValueError, match="Invalid glossary resource name"):
    validate_import_arguments(bad_name)

  client = FakeGlossaryClient(terms=[_term("customer")])
  with pytest.raises(ValueError, match="Invalid glossary resource name"):
    import_dataplex_glossary(bad_name, glossary_client=client)
  assert client.calls == []


def test_glossary_record_with_an_invalid_name_is_rejected():
  with pytest.raises(ValueError, match="Invalid glossary resource name"):
    _map(terms=[_term("customer")], glossary=FakeGlossary(name="glossary"))


def test_validate_import_arguments_accepts_valid_input():
  validate_import_arguments(_GLOSSARY)
  validate_import_arguments(_GLOSSARY_BY_NUMBER, ontology_name="retail")


@pytest.mark.parametrize(
    "glossary_name",
    [
        "projects/example.com:my-proj/locations/us/glossaries/retail",
        "projects/example.co.uk:my-proj/locations/us/glossaries/retail",
        "projects/123456789012/locations/us-central1/glossaries/retail",
        "projects/demo-project/locations/global/glossaries/retail_2026",
        "projects/p/locations/us/glossaries/retail",
        "projects/proj-a/locations/us-central1/glossaries/retail",
        "projects/demo-project/locations/europe-west1/glossaries/\u8ca9\u58f2",
    ],
)
def test_domain_scoped_projects_and_standard_locations_are_accepted(
    glossary_name,
):
  validate_import_arguments(glossary_name)
  ontology = load_ontology_from_string(
      _map(
          terms=[FakeConcept(name=_term_name("t1", glossary_name))],
          glossary=FakeGlossary(name=glossary_name),
      )[0]
  )

  assert ontology.annotations["dataplex:glossary"] == glossary_name


# --------------------------------------------------------------------- #
# YAML emission                                                          #
# --------------------------------------------------------------------- #

_TRICKY_STRINGS = [
    "line one\nline two",
    "windows\r\nline",
    "tab\tseparated",
    'double " quote and back\\slash',
    "single ' quote",
    "colon: value # not a comment",
    "true",
    "Yes",
    "null",
    "~",
    "123",
    "1e3",
    "0x1F",
    ".inf",
    "- dash first",
    "? question first",
    "@at first",
    "`backtick first",
    "!tag first",
    "&anchor first",
    "*alias first",
    "%percent first",
    "{flow: map}",
    "[flow, seq]",
    "internal  double  spaces",
    "unicode \uace0\uac1d \uc0dd\uc560 \uac00\uce58 \u2014 R\u00e9gion \u2713 \U0001f680",
    "line\u2028separator",
    "paragraph\u2029separator",
    "next\u0085line",
    "delete\x7fchar",
    "bell\x07char",
    "nul\x00char",
    "bom\ufeffinside",
]


@pytest.mark.parametrize("value", _TRICKY_STRINGS)
def test_tricky_strings_round_trip_exactly(value):
  ontology = load_ontology_from_string(
      _map(
          terms=[_term("customer", display_name=value, description=value)],
          glossary=FakeGlossary(description=value, uid=value),
      )[0]
  )

  assert ontology.description == value
  assert ontology.annotations["dataplex:uid"] == value
  customer = _entity(ontology, "term_customer")
  assert customer.description == value
  assert customer.annotations["skos:prefLabel"] == value


def test_simple_strings_stay_plain_and_special_strings_are_quoted():
  assert importer._yaml_scalar("Customer Lifetime Value") == (
      "Customer Lifetime Value"
  )
  assert importer._yaml_scalar(_GLOSSARY) == _GLOSSARY
  assert importer._yaml_scalar("true") == '"true"'
  assert importer._yaml_scalar("nan") == '"nan"'
  assert importer._yaml_scalar("2026-q1") == '"2026-q1"'
  assert importer._yaml_scalar("a: b") == '"a: b"'
  assert importer._yaml_scalar("line\nbreak") == '"line\\nbreak"'
  assert importer._yaml_scalar("x\u2028y") == '"x\\u2028y"'
  assert importer._yaml_scalar("R\u00e9gion") == '"R\u00e9gion"'


# --------------------------------------------------------------------- #
# import_dataplex_glossary: API orchestration                            #
# --------------------------------------------------------------------- #


def _retail_client(**kwargs) -> FakeGlossaryClient:
  return FakeGlossaryClient(
      glossary=_retail_glossary(),
      categories=_retail_categories(),
      terms=_retail_terms(),
      **kwargs,
  )


def test_import_fetches_then_delegates_to_the_pure_mapping():
  client = _retail_client()

  assert import_dataplex_glossary(_GLOSSARY, glossary_client=client) == (
      _RETAIL_EXPECTED_YAML,
      _RETAIL_EXPECTED_SUMMARY,
  )
  assert client.calls == [
      ("get_glossary", {"name": _GLOSSARY}),
      ("list_glossary_categories", {"parent": _GLOSSARY}),
      ("list_glossary_terms", {"parent": _GLOSSARY}),
  ]


def test_canonical_glossary_name_drives_lists_and_scheme():
  client = FakeGlossaryClient(
      glossary=FakeGlossary(name=_GLOSSARY_BY_NUMBER),
      terms=[
          FakeConcept(
              name=_term_name("visit", _GLOSSARY_BY_NUMBER), parent=_GLOSSARY
          )
      ],
  )
  yaml_text, summary = import_dataplex_glossary(
      _GLOSSARY, glossary_client=client
  )
  ontology = load_ontology_from_string(yaml_text)

  assert client.calls[1:] == [
      ("list_glossary_categories", {"parent": _GLOSSARY_BY_NUMBER}),
      ("list_glossary_terms", {"parent": _GLOSSARY_BY_NUMBER}),
  ]
  assert ontology.annotations["dataplex:glossary"] == _GLOSSARY_BY_NUMBER
  assert _entity(ontology, "term_visit").annotations["skos:topConceptOf"] == (
      _GLOSSARY_BY_NUMBER
  )
  assert summary.startswith(
      f"Imported Dataplex glossary: {_GLOSSARY_BY_NUMBER}"
  )


@pytest.mark.parametrize(
    "response",
    [
        None,
        {},
        {"name": None},
        {"name": ""},
        {"name": "   "},
        {"name": 123},
        FakeGlossary(name=""),
    ],
    ids=[
        "none",
        "empty_mapping",
        "null_name",
        "empty_name",
        "blank_name",
        "non_string_name",
        "record_with_empty_name",
    ],
)
def test_nameless_get_glossary_response_is_rejected_before_listing(response):
  client = FakeGlossaryClient(terms=[_term("customer")])
  client.glossary = response

  with pytest.raises(ValueError) as info:
    import_dataplex_glossary(_GLOSSARY, glossary_client=client)
  assert str(info.value) == (
      f"GetGlossary for {_GLOSSARY!r} returned no glossary resource name; "
      "nothing was listed."
  )
  assert [method for method, _ in client.calls] == ["get_glossary"]


def test_import_pairs_the_requested_id_with_the_returned_number():
  """A request by project ID answered by project number verifies the
  pair: either form is in scope, and nothing else is."""
  requested = _glossary_in("project-a")
  returned = _glossary_in("123456789012")
  stray_parent = _category_name("customer", _glossary_in("project-b"))
  client = FakeGlossaryClient(
      glossary=FakeGlossary(name=returned),
      categories=[
          FakeConcept(
              name=_category_name("customer", returned), parent=requested
          )
      ],
      terms=[
          FakeConcept(
              name=_term_name("churn", requested),
              parent=_category_name("customer", requested),
          ),
          FakeConcept(name=_term_name("visit", returned), parent=returned),
          FakeConcept(name=_term_name("stray", returned), parent=stray_parent),
      ],
  )

  yaml_text, summary = import_dataplex_glossary(
      requested, glossary_client=client
  )
  ontology = load_ontology_from_string(yaml_text)

  assert client.calls[1:] == [
      ("list_glossary_categories", {"parent": returned}),
      ("list_glossary_terms", {"parent": returned}),
  ]
  for name in ("category_customer", "term_visit"):
    assert _entity(ontology, name).annotations["skos:topConceptOf"] == returned
  assert _relationships(ontology) == [
      ("skos_broader", "term_churn", "category_customer")
  ]
  stray = _entity(ontology, "term_stray").annotations
  assert stray["dataplex:orphan_parent"] == stray_parent
  assert "unverified project" not in summary


@pytest.mark.parametrize("foreign_project", ["project-b", "999999999999"])
def test_import_rejects_records_from_a_third_project(foreign_project):
  returned = _glossary_in("123456789012")
  client = FakeGlossaryClient(
      glossary=FakeGlossary(name=returned),
      categories=[FakeConcept(name=_category_name("customer", returned))],
      terms=[
          FakeConcept(
              name=_term_name("foreign", _glossary_in(foreign_project)),
              parent=returned,
          )
      ],
  )

  with pytest.raises(ValueError, match="does not belong to glossary"):
    import_dataplex_glossary(_glossary_in("project-a"), glossary_client=client)


@pytest.mark.parametrize(
    "requested, returned",
    [
        (
            _glossary_in("project-a"),
            "projects/project-a/locations/europe-west1/glossaries/"
            f"{_GLOSSARY_ID}",
        ),
        (
            _glossary_in("project-a"),
            "projects/project-a/locations/us-central1/glossaries/finance",
        ),
        (_glossary_in("project-a"), _glossary_in("project-b")),
        (_glossary_in("111111111111"), _glossary_in("222222222222")),
        (_glossary_in("project-a"), "glossaries/retail-glossary"),
    ],
    ids=["location", "glossary_id", "project_ids", "project_numbers", "bad"],
)
def test_mismatched_get_glossary_response_is_rejected_before_listing(
    requested, returned
):
  client = FakeGlossaryClient(
      glossary=FakeGlossary(name=returned), terms=[_term("customer")]
  )

  with pytest.raises(ValueError, match="GetGlossary for") as info:
    import_dataplex_glossary(requested, glossary_client=client)
  assert "nothing was listed" in str(info.value)
  assert [method for method, _ in client.calls] == ["get_glossary"]


def _shuffled_repeated_pages(records, rng, page_size=2):
  """Shuffle records into pages, then replay a few pages the way a flaky
  pager might."""
  shuffled = list(records)
  rng.shuffle(shuffled)
  pages = [
      shuffled[i : i + page_size] for i in range(0, len(shuffled), page_size)
  ]
  for _ in range(2):
    replay = [dataclasses.replace(r) for r in rng.choice(pages)]
    pages.insert(rng.randrange(len(pages) + 1), replay)
  return pages


def test_shuffled_and_repeated_pages_give_byte_identical_output():
  outputs = set()
  fingerprints = set()
  concept_sql = set()
  for seed in range(10):
    rng = random.Random(seed)
    client = FakeGlossaryClient(
        glossary=_retail_glossary(),
        category_pages=_shuffled_repeated_pages(_retail_categories(), rng),
        term_pages=_shuffled_repeated_pages(_retail_terms(), rng),
    )
    yaml_text, _ = import_dataplex_glossary(_GLOSSARY, glossary_client=client)
    ontology = load_ontology_from_string(yaml_text)
    binding = _binding_for(ontology)
    outputs.add(yaml_text)
    fingerprints.add(
        compile_fingerprint(
            fingerprint_model(ontology),
            fingerprint_model(binding),
            _COMPILER_VERSION,
        )
    )
    concept_sql.add(
        compile_concept_index(
            ontology,
            binding,
            output_table=_CONCEPT_INDEX_TABLE,
            compiler_version=_COMPILER_VERSION,
        )
    )

  assert outputs == {_RETAIL_EXPECTED_YAML}
  assert len(fingerprints) == 1
  assert len(concept_sql) == 1


def test_permission_denied_is_wrapped_with_a_read_only_iam_hint():
  denied = PermissionDenied("403 Permission 'dataplex.glossaries.get' denied")
  client = FakeGlossaryClient(errors={"get_glossary": denied})

  with pytest.raises(DataplexImportError) as excinfo:
    import_dataplex_glossary(_GLOSSARY, glossary_client=client)
  message = str(excinfo.value)
  assert message.startswith("Dataplex GetGlossary failed: PermissionDenied:")
  assert "roles/dataplex.catalogViewer" in message
  assert excinfo.value.__cause__ is denied


def test_unauthenticated_is_wrapped_with_a_credentials_hint():
  client = FakeGlossaryClient(
      terms=[_term("customer")],
      errors={"list_glossary_terms": Unauthenticated("401 no credentials")},
  )

  with pytest.raises(DataplexImportError, match="ListGlossaryTerms") as info:
    import_dataplex_glossary(_GLOSSARY, glossary_client=client)
  assert "gcloud auth application-default login" in str(info.value)


def test_errors_raised_while_paging_are_wrapped():
  def broken_pages():
    yield _category("first")
    raise ConnectionError("stream reset")

  client = FakeGlossaryClient(terms=[_term("customer")])
  client.list_glossary_categories = lambda request=None, **kw: broken_pages()

  with pytest.raises(
      DataplexImportError,
      match="ListGlossaryCategories failed: ConnectionError: stream reset",
  ):
    import_dataplex_glossary(_GLOSSARY, glossary_client=client)


@pytest.mark.parametrize("bad_page", [0, 1])
def test_unreachable_locations_on_any_page_fail_the_import(bad_page):
  pages = [
      FakePage(categories=[_category("customer")]),
      FakePage(categories=[_category("party")]),
  ]
  pages[bad_page].unreachable_locations = ["us-east1", "", "eu", "us-east1"]
  client = FakeGlossaryClient(category_pages=pages, terms=[_term("churn")])

  with pytest.raises(DataplexImportError) as info:
    import_dataplex_glossary(_GLOSSARY, glossary_client=client)
  assert str(info.value).startswith(
      "Dataplex ListGlossaryCategories could not reach location(s) "
      "'', eu, us-east1, so its result is incomplete; refusing to emit a "
      "partial ontology."
  )
  # The import stopped at the incomplete list.
  assert [method for method, _ in client.calls] == [
      "get_glossary",
      "list_glossary_categories",
  ]


def test_unreachable_locations_in_term_pages_fail_the_import():
  client = FakeGlossaryClient(
      categories=[_category("customer")],
      term_pages=[
          FakePage(terms=[_term("churn")]),
          FakePage(terms=[_term("visit")], unreachable_locations=["asia1"]),
      ],
  )

  with pytest.raises(
      DataplexImportError,
      match=r"^Dataplex ListGlossaryTerms could not reach location\(s\) asia1",
  ):
    import_dataplex_glossary(_GLOSSARY, glossary_client=client)


def test_pages_without_unreachable_locations_are_all_read():
  client = FakeGlossaryClient(
      category_pages=[
          FakePage(categories=[_category("customer")]),
          FakePage(categories=[_category("party")], unreachable_locations=[]),
      ],
      term_pages=[
          FakePage(terms=[_term("churn", parent=_category_name("customer"))]),
          FakePage(terms=[_term("visit")]),
      ],
  )

  ontology = load_ontology_from_string(
      import_dataplex_glossary(_GLOSSARY, glossary_client=client)[0]
  )

  assert [e.name for e in ontology.entities] == [
      "category_customer",
      "category_party",
      "term_churn",
      "term_visit",
  ]
  assert _relationships(ontology) == [
      ("skos_broader", "term_churn", "category_customer")
  ]


@pytest.mark.parametrize(
    "marker, shown", [([" "], "' '"), ([""], "''")], ids=["blank", "empty"]
)
def test_blank_unreachable_location_markers_still_fail_the_import(
    marker, shown
):
  client = FakeGlossaryClient(
      category_pages=[
          FakePage(
              categories=[_category("customer")], unreachable_locations=marker
          )
      ],
      terms=[_term("churn")],
  )

  with pytest.raises(DataplexImportError) as info:
    import_dataplex_glossary(_GLOSSARY, glossary_client=client)
  assert str(info.value).startswith(
      f"Dataplex ListGlossaryCategories could not reach location(s) {shown}, "
      "so its result is incomplete"
  )


class _ItemIterablePager:
  """An iterable-of-items list result that also keeps ``pages``, as plain
  lists of items rather than ``List...Response`` messages."""

  def __init__(self, items, pages):
    self._items = items
    self.pages = pages

  def __iter__(self):
    return iter(self._items)


@pytest.mark.parametrize(
    "pages",
    [[[_category("customer")], [_category("party")]], [], None],
    ids=["plain_list_pages", "no_pages", "non_iterable_pages"],
)
def test_item_iterable_results_with_plain_pages_are_read_item_by_item(pages):
  categories = [_category("customer"), _category("party")]
  client = FakeGlossaryClient(
      terms=[_term("churn", parent=_category_name("customer"))]
  )
  client.list_glossary_categories = lambda request=None, **kw: (
      _ItemIterablePager(categories, pages)
  )

  ontology = load_ontology_from_string(
      import_dataplex_glossary(_GLOSSARY, glossary_client=client)[0]
  )

  assert [e.name for e in ontology.entities] == [
      "category_customer",
      "category_party",
      "term_churn",
  ]
  assert _relationships(ontology) == [
      ("skos_broader", "term_churn", "category_customer")
  ]


class _SharedPagesPager:
  """An item-iterable list result whose ``pages`` is the very iterator its
  own item iteration draws from."""

  def __init__(self, pages):
    self.pages = iter(pages)

  def __iter__(self):
    for page in self.pages:
      yield from page


class _SingleUsePager:
  """An item-iterable list result that can be started only once, as
  ``google.api_core`` page iterators are: reading ``pages`` and iterating
  the result both start it."""

  def __init__(self, pages):
    self._pages = pages
    self._started = False

  def _start(self):
    if self._started:
      raise ValueError("Iterator has already started")
    self._started = True

  @property
  def pages(self):
    self._start()
    return iter(self._pages)

  def __iter__(self):
    self._start()
    return (item for page in self._pages for item in page)


@pytest.mark.parametrize(
    "pager_class",
    [_SharedPagesPager, _SingleUsePager],
    ids=["shared_pages_iterator", "single_use_iterator"],
)
@pytest.mark.parametrize(
    "pages, expected",
    [
        (
            [[_category("customer"), _category("party")]],
            ["category_customer", "category_party"],
        ),
        (
            [[_category("customer")], [_category("party")]],
            ["category_customer", "category_party"],
        ),
        (
            [[_category("customer")], [], [_category("party")]],
            ["category_customer", "category_party"],
        ),
        ([[]], []),
        ([], []),
    ],
    ids=[
        "one_page",
        "two_pages",
        "empty_page_between",
        "empty_page",
        "no_pages",
    ],
)
def test_single_use_item_pages_are_read_once_without_losing_items(
    pager_class, pages, expected
):
  client = FakeGlossaryClient(terms=[_term("churn")])
  client.list_glossary_categories = lambda request=None, **kw: pager_class(
      pages
  )

  ontology = load_ontology_from_string(
      import_dataplex_glossary(_GLOSSARY, glossary_client=client)[0]
  )

  assert [e.name for e in ontology.entities] == expected + ["term_churn"]


@pytest.mark.parametrize(
    "pages",
    [
        [[_category("customer")], _category("party")],
        [[_category("customer")], "party"],
        [[_category("customer")], {"categories": []}],
        [[_category("customer")], None],
        [_category("customer")],
    ],
    ids=[
        "item_page",
        "str_page",
        "mapping_page",
        "none_page",
        "items_as_pages",
    ],
)
def test_single_use_pages_that_are_not_item_collections_fail(pages):
  client = FakeGlossaryClient(terms=[_term("churn")])
  client.list_glossary_categories = lambda request=None, **kw: (
      _SharedPagesPager(pages)
  )

  with pytest.raises(DataplexImportError) as info:
    import_dataplex_glossary(_GLOSSARY, glossary_client=client)
  assert str(info.value) == (
      "Dataplex ListGlossaryCategories returned a page that is not a "
      "collection of items; refusing to emit a partial ontology."
  )


def test_api_core_page_iterators_are_read_once():
  page_iterator = pytest.importorskip("google.api_core.page_iterator")
  responses = iter(
      [
          types.SimpleNamespace(
              categories=[_category("customer")], next_page_token="more"
          ),
          types.SimpleNamespace(
              categories=[_category("party")], next_page_token=""
          ),
      ]
  )
  pager = page_iterator.GRPCIterator(
      client=None,
      method=lambda request, **kwargs: next(responses),
      request=types.SimpleNamespace(page_token=""),
      items_field="categories",
  )
  client = FakeGlossaryClient(terms=[_term("churn")])
  client.list_glossary_categories = lambda request=None, **kw: pager

  ontology = load_ontology_from_string(
      import_dataplex_glossary(_GLOSSARY, glossary_client=client)[0]
  )

  assert [e.name for e in ontology.entities] == [
      "category_customer",
      "category_party",
      "term_churn",
  ]


def test_mock_list_results_are_read_item_by_item():
  pager = mock.MagicMock()
  pager.__iter__.return_value = iter(
      [_category("customer"), _category("party")]
  )
  client = FakeGlossaryClient(terms=[_term("churn")])
  client.list_glossary_categories = lambda request=None, **kw: pager

  ontology = load_ontology_from_string(
      import_dataplex_glossary(_GLOSSARY, glossary_client=client)[0]
  )

  assert [e.name for e in ontology.entities] == [
      "category_customer",
      "category_party",
      "term_churn",
  ]


class _PagesOnlyPager:
  """A list result read only through ``pages``; iterating it directly
  yields nothing."""

  def __init__(self, pages):
    self.pages = pages

  def __iter__(self):
    return iter(())


@pytest.mark.parametrize("reusable", [True, False], ids=["list", "iterator"])
@pytest.mark.parametrize(
    "unreachable, problem",
    [
        (
            ["us-west1"],
            "could not reach location(s) us-west1, so its result is "
            "incomplete",
        ),
        ([], "returned an unexpected page object without 'categories'"),
    ],
    ids=["unreachable", "no_items"],
)
def test_pages_with_unreachable_locations_are_never_read_as_plain(
    reusable, unreachable, problem
):
  pages = [types.SimpleNamespace(unreachable_locations=unreachable)]
  client = FakeGlossaryClient(terms=[_term("churn")])
  client.list_glossary_categories = lambda request=None, **kw: (
      _PagesOnlyPager(pages if reusable else iter(pages))
  )

  with pytest.raises(DataplexImportError) as info:
    import_dataplex_glossary(_GLOSSARY, glossary_client=client)
  assert str(info.value).startswith(
      f"Dataplex ListGlossaryCategories {problem}"
  )


class _UnreachableItems(list):
  """A plain page of items that still reports an unreachable location."""

  unreachable_locations = ("eu",)


def test_a_later_plain_page_with_unreachable_locations_fails():
  pages = [[_category("customer")], _UnreachableItems([_category("party")])]
  client = FakeGlossaryClient(terms=[_term("churn")])
  client.list_glossary_categories = lambda request=None, **kw: (
      _SharedPagesPager(pages)
  )

  with pytest.raises(DataplexImportError) as info:
    import_dataplex_glossary(_GLOSSARY, glossary_client=client)
  assert str(info.value).startswith(
      "Dataplex ListGlossaryCategories could not reach location(s) eu, so "
      "its result is incomplete"
  )


class _ResponseFieldsPager:
  """Exposes a response field itself, as a GAPIC pager does, so its pages
  have to be responses too."""

  categories = ()

  def __init__(self, pages):
    self.pages = pages

  def __iter__(self):
    return iter(())


class _ReportsUnreachableLocationsPager:
  """Exposes ``unreachable_locations`` itself, as a GAPIC pager does for its
  current response, so its pages have to be responses too."""

  unreachable_locations = ("eu",)

  def __init__(self, pages):
    self.pages = pages

  def __iter__(self):
    return iter(())


@pytest.mark.parametrize(
    "pager, problem",
    [
        (
            FakePager(
                [
                    FakePage(categories=[_category("customer")]),
                    [_category("party")],
                ],
                "categories",
            ),
            "an unexpected page object without 'categories'",
        ),
        (
            _ResponseFieldsPager([[_category("customer")]]),
            "an unexpected page object without 'categories'",
        ),
        (_ResponseFieldsPager(None), "pages that cannot be iterated"),
        (
            _ReportsUnreachableLocationsPager([[_category("customer")]]),
            "an unexpected page object without 'categories'",
        ),
    ],
    ids=[
        "later_page_is_not_a_response",
        "first_page_is_not_a_response",
        "pages_are_not_iterable",
        "result_reports_unreachable_locations",
    ],
)
def test_unreadable_pages_fail_instead_of_reading_as_empty(pager, problem):
  client = FakeGlossaryClient(terms=[_term("churn")])
  client.list_glossary_categories = lambda request=None, **kw: pager

  with pytest.raises(DataplexImportError) as info:
    import_dataplex_glossary(_GLOSSARY, glossary_client=client)
  assert str(info.value) == (
      f"Dataplex ListGlossaryCategories returned {problem}; refusing to emit "
      "a partial ontology."
  )


class MalformedError(ValueError):
  """Name-compatible with ``google.auth.exceptions.MalformedError``."""


class JSONDecodeError(ValueError):
  """Name-compatible with ``requests.exceptions.JSONDecodeError``."""


@pytest.mark.parametrize(
    "method, operation, error",
    [
        ("get_glossary", "GetGlossary", ValueError("bad request")),
        (
            "get_glossary",
            "GetGlossary",
            MalformedError("malformed service account info"),
        ),
        (
            "list_glossary_terms",
            "ListGlossaryTerms",
            JSONDecodeError("Expecting value: line 1 column 1 (char 0)"),
        ),
    ],
    ids=["value_error", "malformed_credentials", "json_decode"],
)
def test_value_errors_from_the_client_are_api_errors(method, operation, error):
  client = FakeGlossaryClient(terms=[_term("customer")], errors={method: error})

  with pytest.raises(DataplexImportError) as info:
    import_dataplex_glossary(_GLOSSARY, glossary_client=client)
  assert str(info.value) == (
      f"Dataplex {operation} failed: {type(error).__name__}: {error}"
  )
  assert info.value.__cause__ is error


def test_mapping_errors_from_the_import_stay_validation_errors():
  client = FakeGlossaryClient(
      terms=[_term("customer"), _term("customer", display_name="Other")]
  )

  with pytest.raises(ValueError, match="Conflicting records") as info:
    import_dataplex_glossary(_GLOSSARY, glossary_client=client)
  assert not isinstance(info.value, DataplexImportError)


def test_missing_dependency_raises_an_import_error(monkeypatch):
  monkeypatch.setitem(sys.modules, "google.cloud.dataplex_v1", None)

  with pytest.raises(MissingDataplexDependencyError) as excinfo:
    import_dataplex_glossary(_GLOSSARY)
  assert isinstance(excinfo.value, ImportError)
  assert "pip install 'bigquery-agent-analytics[dataplex]'" in str(
      excinfo.value
  )


def test_invalid_arguments_fail_before_any_client_is_built(monkeypatch):
  def _unexpected():
    raise AssertionError("client must not be built for invalid arguments")

  monkeypatch.setattr(importer, "_create_default_glossary_client", _unexpected)

  with pytest.raises(ValueError, match="Invalid glossary resource name"):
    import_dataplex_glossary("glossaries/retail")
  with pytest.raises(ValueError, match="ontology_name"):
    import_dataplex_glossary(_GLOSSARY, ontology_name=" ")


def _fake_dataplex_module(client_cls=None) -> types.ModuleType:
  module = types.ModuleType("google.cloud.dataplex_v1")
  if client_cls is not None:
    module.BusinessGlossaryServiceClient = client_cls
  return module


def test_default_client_is_built_lazily_from_the_sdk(monkeypatch):
  built = []

  class BusinessGlossaryServiceClient(FakeGlossaryClient):

    def __init__(self):
      super().__init__(terms=[_term("customer", display_name="Customer")])
      built.append(self)

  monkeypatch.setitem(
      sys.modules,
      "google.cloud.dataplex_v1",
      _fake_dataplex_module(BusinessGlossaryServiceClient),
  )

  ontology = load_ontology_from_string(import_dataplex_glossary(_GLOSSARY)[0])
  assert len(built) == 1
  assert [e.name for e in ontology.entities] == ["term_customer"]


def test_old_sdk_without_the_glossary_client_needs_an_upgrade(monkeypatch):
  monkeypatch.setitem(
      sys.modules, "google.cloud.dataplex_v1", _fake_dataplex_module()
  )

  with pytest.raises(MissingDataplexDependencyError, match="2.12.0"):
    import_dataplex_glossary(_GLOSSARY)


def test_client_construction_failures_are_api_errors(monkeypatch):
  class DefaultCredentialsError(Exception):
    pass

  def _no_credentials():
    raise DefaultCredentialsError("Your default credentials were not found")

  monkeypatch.setitem(
      sys.modules,
      "google.cloud.dataplex_v1",
      _fake_dataplex_module(_no_credentials),
  )

  with pytest.raises(DataplexImportError, match="client construction") as info:
    import_dataplex_glossary(_GLOSSARY)
  assert "gcloud auth application-default login" in str(info.value)


def _dataplex_imports(source: str) -> list[str]:
  """Every imported module path in ``source`` that mentions dataplex,
  including ``from google.cloud import dataplex_v1``."""
  found = []
  for node in ast.walk(ast.parse(source)):
    if isinstance(node, ast.Import):
      found += [alias.name for alias in node.names if "dataplex" in alias.name]
    elif isinstance(node, ast.ImportFrom):
      module = node.module or ""
      for alias in node.names:
        qualified = f"{module}.{alias.name}" if module else alias.name
        if "dataplex" in module or "dataplex" in qualified:
          found.append(qualified)
  return found


def test_dataplex_import_check_catches_every_import_form():
  assert _dataplex_imports("from google.cloud import dataplex_v1") == [
      "google.cloud.dataplex_v1"
  ]
  assert _dataplex_imports("import google.cloud.dataplex_v1 as dp") == [
      "google.cloud.dataplex_v1"
  ]
  assert _dataplex_imports("from google.cloud.dataplex_v1 import Glossary") == [
      "google.cloud.dataplex_v1.Glossary"
  ]
  assert _dataplex_imports("from google.cloud import storage") == []


def test_module_never_imports_google_cloud_dataplex_eagerly():
  """Static guard: the only reference to ``google.cloud.dataplex_v1`` is
  the lazy ``importlib`` call inside ``_create_default_glossary_client``."""
  source = pathlib.Path(importer.__file__).read_text(encoding="utf-8")
  assert _dataplex_imports(source) == []
  tree = ast.parse(source)
  references = [
      node
      for node in ast.walk(tree)
      if isinstance(node, ast.Constant)
      and node.value == "google.cloud.dataplex_v1"
  ]
  factory = next(
      node
      for node in tree.body
      if isinstance(node, ast.FunctionDef)
      and node.name == "_create_default_glossary_client"
  )
  assert len(references) == 1
  assert references[0] in list(ast.walk(factory))


def test_real_dataplex_messages_are_supported():
  """Parity with the proto-plus types when the extra is installed."""
  dataplex_v1 = pytest.importorskip("google.cloud.dataplex_v1")
  client = FakeGlossaryClient(
      glossary=dataplex_v1.Glossary(
          name=_GLOSSARY, uid="u-1", description="Retail."
      ),
      categories=[
          dataplex_v1.GlossaryCategory(
              name=_category_name("customer"),
              parent=_GLOSSARY,
              display_name="Customer",
          )
      ],
      terms=[
          dataplex_v1.GlossaryTerm(
              name=_term_name("churn"),
              parent=_category_name("customer"),
              display_name="Churn",
          )
      ],
  )
  ontology = load_ontology_from_string(
      import_dataplex_glossary(_GLOSSARY, glossary_client=client)[0]
  )

  assert ontology.annotations == {
      "dataplex:glossary": _GLOSSARY,
      "dataplex:uid": "u-1",
  }
  assert _relationships(ontology) == [
      ("skos_broader", "term_churn", "category_customer")
  ]


def test_real_gapic_pagers_are_read_page_by_page():
  """Parity with the real pagers: every page is read, and an unreachable
  location on a later page still fails the import."""
  dataplex_v1 = pytest.importorskip("google.cloud.dataplex_v1")
  pagers = pytest.importorskip(
      "google.cloud.dataplex_v1.services.business_glossary_service.pagers"
  )

  def real_pager(pager_cls, request_cls, response_cls, field, pages):
    responses = [
        response_cls(
            **{field: items},
            unreachable_locations=unreachable,
            next_page_token=f"page-{i + 1}" if i + 1 < len(pages) else "",
        )
        for i, (items, unreachable) in enumerate(pages)
    ]
    later = iter(responses[1:])
    return pager_cls(
        method=lambda request, **kwargs: next(later),
        request=request_cls(parent=_GLOSSARY),
        response=responses[0],
    )

  def client(term_unreachable):
    fake = FakeGlossaryClient(glossary=dataplex_v1.Glossary(name=_GLOSSARY))
    fake.list_glossary_categories = lambda request=None, **kwargs: real_pager(
        pagers.ListGlossaryCategoriesPager,
        dataplex_v1.ListGlossaryCategoriesRequest,
        dataplex_v1.ListGlossaryCategoriesResponse,
        "categories",
        [
            ([dataplex_v1.GlossaryCategory(name=_category_name("a"))], []),
            ([dataplex_v1.GlossaryCategory(name=_category_name("b"))], []),
        ],
    )
    fake.list_glossary_terms = lambda request=None, **kwargs: real_pager(
        pagers.ListGlossaryTermsPager,
        dataplex_v1.ListGlossaryTermsRequest,
        dataplex_v1.ListGlossaryTermsResponse,
        "terms",
        [
            (
                [
                    dataplex_v1.GlossaryTerm(
                        name=_term_name("churn"), parent=_category_name("a")
                    )
                ],
                [],
            ),
            (
                [dataplex_v1.GlossaryTerm(name=_term_name("visit"))],
                term_unreachable,
            ),
        ],
    )
    return fake

  ontology = load_ontology_from_string(
      import_dataplex_glossary(_GLOSSARY, glossary_client=client([]))[0]
  )
  assert [e.name for e in ontology.entities] == [
      "category_a",
      "category_b",
      "term_churn",
      "term_visit",
  ]
  assert _relationships(ontology) == [
      ("skos_broader", "term_churn", "category_a")
  ]

  with pytest.raises(
      DataplexImportError,
      match=r"ListGlossaryTerms could not reach location\(s\) us-east1",
  ):
    import_dataplex_glossary(_GLOSSARY, glossary_client=client(["us-east1"]))


# --------------------------------------------------------------------- #
# Downstream parity: concept index, runtime, resolver                    #
# --------------------------------------------------------------------- #


@pytest.fixture
def retail_models():
  ontology = load_ontology_from_string(_map_retail()[0])
  return ontology, _binding_for(ontology)


def test_concept_index_rows_carry_pref_and_notation_labels(retail_models):
  ontology, binding = retail_models
  rows = build_rows(ontology, binding, compiler_version=_COMPILER_VERSION)

  assert {r.scheme for r in rows} == {_GLOSSARY}
  assert all(r.is_abstract for r in rows)
  assert {r.entity_name for r in rows} == {e.name for e in ontology.entities}
  assert [
      (r.label, r.label_kind, r.notation)
      for r in rows
      if r.entity_name == "term_clv_2024"
  ] == [
      ("term_clv_2024", "name", "clv-2024"),
      ("clv-2024", "notation", "clv-2024"),
      ("Customer Lifetime Value", "pref", "clv-2024"),
  ]


def test_compile_concept_index_emits_the_glossary_rows(retail_models):
  ontology, binding = retail_models
  sql = compile_concept_index(
      ontology,
      binding,
      output_table=_CONCEPT_INDEX_TABLE,
      compiler_version=_COMPILER_VERSION,
  )

  assert sql.count("CREATE OR REPLACE TABLE") == 2
  assert "'Customer Lifetime Value'" in sql
  assert "'clv-2024'" in sql
  assert f"'{_GLOSSARY}'" in sql


def test_ontology_runtime_traversal(retail_models):
  from bigquery_agent_analytics import OntologyRuntime

  ontology, binding = retail_models
  runtime = OntologyRuntime.from_models(
      ontology=ontology,
      binding=binding,
      compiler_version=_COMPILER_VERSION,
  )

  assert runtime.schemes_for("term_clv_2024") == (_GLOSSARY,)
  assert runtime.schemes_for("category_customer") == (_GLOSSARY,)
  assert runtime.in_scheme(_GLOSSARY) == (
      "category_customer",
      "category_metrics",
      "term_churn",
      "term_clv_2024",
      "term_retention",
  )
  assert runtime.broader("term_clv_2024") == ("category_metrics",)
  assert runtime.broader("category_metrics") == ("category_customer",)
  assert runtime.broader("term_retention") == ()
  assert runtime.narrower("category_customer") == (
      "category_metrics",
      "term_churn",
  )
  assert runtime.narrower("category_metrics") == ("term_clv_2024",)
  assert runtime.notation_for("term_clv_2024") == "clv-2024"
  assert set(runtime.labels_for("term_clv_2024")) == {
      ("term_clv_2024", "name"),
      ("Customer Lifetime Value", "pref"),
      ("clv-2024", "notation"),
  }


class _Job:

  def __init__(self, rows):
    self._rows = rows

  def result(self):
    return iter(self._rows)


class _ConceptIndexBQ:
  """Fake BigQuery client answering concept-index queries from the rows
  ``build_rows`` materializes, honoring the bound query parameters."""

  def __init__(self, ontology, binding):
    self.rows = [
        dataclasses.asdict(row)
        for row in build_rows(
            ontology, binding, compiler_version=_COMPILER_VERSION
        )
    ]

  def query(self, sql, job_config=None):
    if "__meta" in sql:
      return _Job(
          [{"compile_fingerprint": self.rows[0]["compile_fingerprint"]}]
      )
    params = {}
    for param in job_config.query_parameters:
      params[param.name] = getattr(param, "values", None) or param.value
    case_insensitive = "LOWER(label) = LOWER(@label)" in sql
    matches = []
    for row in self.rows:
      if row["compile_fingerprint"] != params["expected_fp"]:
        continue
      label, wanted = row["label"], params["label"]
      if case_insensitive:
        label, wanted = label.lower(), wanted.lower()
      if label == wanted:
        matches.append(row)
    return _Job(matches[: params["row_limit"]])


def test_label_synonym_resolver_ranks_pref_before_notation(retail_models):
  from bigquery_agent_analytics import LabelSynonymResolver
  from bigquery_agent_analytics import OntologyRuntime

  ontology, binding = retail_models
  runtime = OntologyRuntime.from_models(
      ontology=ontology,
      binding=binding,
      compiler_version=_COMPILER_VERSION,
      concept_index_table=_CONCEPT_INDEX_TABLE,
      bq_client=_ConceptIndexBQ(ontology, binding),
  )
  resolver = LabelSynonymResolver(runtime)

  def resolved(query):
    return [
        (c.entity_name, c.matched_label_kind, c.scheme, c.notation)
        for c in resolver.resolve(query)
    ]

  assert resolved("customer lifetime value") == [
      ("term_clv_2024", "pref", _GLOSSARY, "clv-2024")
  ]
  assert resolved("clv-2024") == [
      ("term_clv_2024", "notation", _GLOSSARY, "clv-2024")
  ]
  # "Churn" is both the display name and (case-folded) the resource ID.
  assert resolved("churn") == [
      ("term_churn", "pref", _GLOSSARY, "churn"),
      ("term_churn", "notation", _GLOSSARY, "churn"),
  ]
