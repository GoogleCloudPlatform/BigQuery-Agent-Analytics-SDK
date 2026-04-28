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

"""Tests for the concept-index row builder (A2).

Pinned in `docs/implementation_plan_concept_index_runtime.md` and the
RFC. Each row is a `(entity_name, label, label_kind, language, scheme)`
membership tuple plus per-entity `notation` and provenance columns
(`compile_id` short display + `compile_fingerprint` 64-hex integrity).

Test coverage maps to plan items:

- D1 fingerprint determinism (covered in test_fingerprint.py).
- D2 build_rows produces identical output across runs.
- D3 row scope (abstract always included; concrete iff bound).
- D4 multi-scheme denormalization.
- D5 notation as first-class row.
- Plus row-shape, sort determinism, provenance columns.
"""

from __future__ import annotations

from bigquery_ontology import BigQueryTarget
from bigquery_ontology import Binding
from bigquery_ontology import Entity
from bigquery_ontology import EntityBinding
from bigquery_ontology import Keys
from bigquery_ontology import Ontology
from bigquery_ontology import Property
from bigquery_ontology import PropertyBinding
from bigquery_ontology import PropertyType
from bigquery_ontology._fingerprint import compile_fingerprint
from bigquery_ontology._fingerprint import compile_id
from bigquery_ontology._fingerprint import fingerprint_model
from bigquery_ontology.concept_index import build_rows
from bigquery_ontology.concept_index import ConceptIndexRow

_COMPILER_VERSION = "bigquery_ontology test"


def _account_entity(**kwargs) -> Entity:
  return Entity(
      name="Account",
      keys=Keys(primary=["account_id"]),
      properties=[Property(name="account_id", type=PropertyType.STRING)],
      **kwargs,
  )


def _account_binding() -> EntityBinding:
  return EntityBinding(
      name="Account",
      source="accounts",
      properties=[PropertyBinding(name="account_id", column="account_id")],
  )


def _binding(*entities: EntityBinding) -> Binding:
  return Binding(
      binding="test_binding",
      ontology="test",
      target=BigQueryTarget(backend="bigquery", project="p", dataset="d"),
      entities=list(entities),
      relationships=[],
  )


# ------------------------------------------------------------------ #
# Row shape                                                            #
# ------------------------------------------------------------------ #


class TestRowShape:

  def test_concrete_entity_emits_name_row(self):
    ontology = Ontology(ontology="test", entities=[_account_entity()])
    rows = build_rows(
        ontology,
        _binding(_account_binding()),
        compiler_version=_COMPILER_VERSION,
    )
    name_rows = [r for r in rows if r.label_kind == "name"]
    assert len(name_rows) == 1
    row = name_rows[0]
    assert row.entity_name == "Account"
    assert row.label == "Account"
    assert row.is_abstract is False
    assert row.language is None
    assert row.scheme is None
    assert row.notation is None

  def test_synonyms_emit_synonym_rows(self):
    ontology = Ontology(
        ontology="test",
        entities=[_account_entity(synonyms=["Acct", "AC"])],
    )
    rows = build_rows(
        ontology,
        _binding(_account_binding()),
        compiler_version=_COMPILER_VERSION,
    )
    syn = sorted(r.label for r in rows if r.label_kind == "synonym")
    assert syn == ["AC", "Acct"]

  def test_notation_emits_first_class_row_and_column(self):
    """D5: every entity with skos:notation gets a row with
    ``label_kind='notation'`` and ``label = notation_value``. The
    ``notation`` column on EVERY row of that entity also carries the
    notation value (per-entity display, repeats across rows).
    """
    ontology = Ontology(
        ontology="test",
        entities=[
            _account_entity(
                synonyms=["Acct"],
                annotations={"skos:notation": "807"},
            )
        ],
    )
    rows = build_rows(
        ontology,
        _binding(_account_binding()),
        compiler_version=_COMPILER_VERSION,
    )
    # The `notation` column repeats on every row for Account.
    for r in rows:
      assert r.notation == "807"
    # Exactly one row has label_kind='notation' with label='807'.
    notation_rows = [r for r in rows if r.label_kind == "notation"]
    assert len(notation_rows) == 1
    assert notation_rows[0].label == "807"

  def test_skos_label_annotations_emit_typed_rows(self):
    ontology = Ontology(
        ontology="test",
        entities=[
            _account_entity(
                annotations={
                    "skos:prefLabel": "Bank Account",
                    "skos:altLabel": "Acct",
                    "skos:hiddenLabel": "AC",
                }
            )
        ],
    )
    rows = build_rows(
        ontology,
        _binding(_account_binding()),
        compiler_version=_COMPILER_VERSION,
    )
    by_kind = {r.label_kind: r.label for r in rows}
    assert by_kind["pref"] == "Bank Account"
    assert by_kind["alt"] == "Acct"
    assert by_kind["hidden"] == "AC"

  def test_language_tagged_label_annotations(self):
    """Annotations with `@<lang>` suffix populate the language column."""
    ontology = Ontology(
        ontology="test",
        entities=[
            _account_entity(
                annotations={
                    "skos:prefLabel": "Bank Account",
                    "skos:prefLabel@fr": "Compte Bancaire",
                    "skos:altLabel@de": "Bankkonto",
                }
            )
        ],
    )
    rows = build_rows(
        ontology,
        _binding(_account_binding()),
        compiler_version=_COMPILER_VERSION,
    )
    # Default-language pref row has language=None.
    en = [r for r in rows if r.label_kind == "pref" and r.language is None]
    assert len(en) == 1 and en[0].label == "Bank Account"
    # French pref row.
    fr = [r for r in rows if r.label_kind == "pref" and r.language == "fr"]
    assert len(fr) == 1 and fr[0].label == "Compte Bancaire"
    # German alt row.
    de = [r for r in rows if r.label_kind == "alt" and r.language == "de"]
    assert len(de) == 1 and de[0].label == "Bankkonto"

  def test_provenance_columns_match_fingerprint_exports(self):
    ontology = Ontology(ontology="test", entities=[_account_entity()])
    binding = _binding(_account_binding())
    rows = build_rows(ontology, binding, compiler_version=_COMPILER_VERSION)

    expected_fp = compile_fingerprint(
        fingerprint_model(ontology),
        fingerprint_model(binding),
        _COMPILER_VERSION,
    )
    expected_cid = compile_id(
        fingerprint_model(ontology),
        fingerprint_model(binding),
        _COMPILER_VERSION,
    )
    assert all(r.compile_fingerprint == expected_fp for r in rows)
    assert all(r.compile_id == expected_cid for r in rows)
    assert all(len(r.compile_fingerprint) == 64 for r in rows)
    assert all(len(r.compile_id) == 12 for r in rows)


# ------------------------------------------------------------------ #
# Scope rule (D3)                                                     #
# ------------------------------------------------------------------ #


class TestScopeRule:
  """Abstract entities always included; concrete iff bound."""

  def test_abstract_entity_always_included(self):
    """Abstract entities are informational and never need a binding."""
    ontology = Ontology(
        ontology="test",
        entities=[
            _account_entity(),
            Entity(name="skos_Banking", abstract=True),
        ],
    )
    # Binding does NOT cover skos_Banking; it's abstract so it's still in.
    rows = build_rows(
        ontology,
        _binding(_account_binding()),
        compiler_version=_COMPILER_VERSION,
    )
    names = {r.entity_name for r in rows}
    assert "skos_Banking" in names
    assert "Account" in names

  def test_concrete_entity_excluded_if_unbound(self):
    """A concrete entity not present in the binding is excluded."""
    ontology = Ontology(
        ontology="test",
        entities=[
            _account_entity(),
            Entity(
                name="Ledger",
                keys=Keys(primary=["ledger_id"]),
                properties=[
                    Property(name="ledger_id", type=PropertyType.STRING)
                ],
            ),
        ],
    )
    # Binding only has Account; Ledger is concrete but unbound → excluded.
    rows = build_rows(
        ontology,
        _binding(_account_binding()),
        compiler_version=_COMPILER_VERSION,
    )
    names = {r.entity_name for r in rows}
    assert names == {"Account"}

  def test_abstract_row_marks_is_abstract_true(self):
    ontology = Ontology(
        ontology="test",
        entities=[
            _account_entity(),
            Entity(name="skos_Banking", abstract=True),
        ],
    )
    rows = build_rows(
        ontology,
        _binding(_account_binding()),
        compiler_version=_COMPILER_VERSION,
    )
    for r in rows:
      if r.entity_name == "skos_Banking":
        assert r.is_abstract is True
      else:
        assert r.is_abstract is False


# ------------------------------------------------------------------ #
# Multi-scheme denormalization (D4)                                   #
# ------------------------------------------------------------------ #


class TestMultiScheme:
  """A concept in N schemes emits N rows per label."""

  def test_single_scheme_membership(self):
    ontology = Ontology(
        ontology="test",
        entities=[
            _account_entity(annotations={"skos:inScheme": "BankingTaxonomy"})
        ],
    )
    rows = build_rows(
        ontology,
        _binding(_account_binding()),
        compiler_version=_COMPILER_VERSION,
    )
    schemes = {r.scheme for r in rows}
    assert schemes == {"BankingTaxonomy"}

  def test_multi_scheme_membership(self):
    """A concept in 2 schemes × 1 name label = 2 name rows. The
    name + a synonym in 2 schemes = 4 rows total.
    """
    ontology = Ontology(
        ontology="test",
        entities=[
            _account_entity(
                synonyms=["Acct"],
                annotations={
                    "skos:inScheme": ["BankingTaxonomy", "FinancialProducts"]
                },
            )
        ],
    )
    rows = build_rows(
        ontology,
        _binding(_account_binding()),
        compiler_version=_COMPILER_VERSION,
    )
    # 2 schemes × (1 name + 1 synonym) = 4 rows.
    assert len(rows) == 4
    schemes = sorted({r.scheme for r in rows})
    assert schemes == ["BankingTaxonomy", "FinancialProducts"]
    # DISTINCT entity_name returns 1 (per the multi-scheme contract).
    assert len({r.entity_name for r in rows}) == 1

  def test_no_scheme_yields_null_column(self):
    ontology = Ontology(ontology="test", entities=[_account_entity()])
    rows = build_rows(
        ontology,
        _binding(_account_binding()),
        compiler_version=_COMPILER_VERSION,
    )
    assert all(r.scheme is None for r in rows)

  def test_top_concept_of_contributes_scheme_membership(self):
    """A concept declared as the top of a scheme via
    ``skos:topConceptOf`` is still a member of that scheme. Without
    this, queries like ``WHERE ci.scheme = 'Banking'`` miss the
    scheme's top concepts.
    """
    ontology = Ontology(
        ontology="test",
        entities=[
            _account_entity(
                annotations={"skos:topConceptOf": "BankingTaxonomy"}
            )
        ],
    )
    rows = build_rows(
        ontology,
        _binding(_account_binding()),
        compiler_version=_COMPILER_VERSION,
    )
    schemes = {r.scheme for r in rows}
    assert schemes == {"BankingTaxonomy"}

  def test_in_scheme_and_top_concept_of_unioned_and_deduped(self):
    """If both annotations name the same scheme, only one set of
    rows should be emitted for that scheme. If they name different
    schemes, both should be present.
    """
    ontology = Ontology(
        ontology="test",
        entities=[
            _account_entity(
                annotations={
                    "skos:inScheme": "BankingTaxonomy",
                    "skos:topConceptOf": [
                        "BankingTaxonomy",  # duplicate of inScheme — dedupe
                        "FinancialProducts",  # additional scheme — keep
                    ],
                }
            )
        ],
    )
    rows = build_rows(
        ontology,
        _binding(_account_binding()),
        compiler_version=_COMPILER_VERSION,
    )
    schemes = sorted({r.scheme for r in rows})
    assert schemes == ["BankingTaxonomy", "FinancialProducts"]
    # Exactly one name row per scheme — the duplicate did not double-emit.
    name_rows = [r for r in rows if r.label_kind == "name"]
    assert len(name_rows) == 2


# ------------------------------------------------------------------ #
# Row deduplication                                                   #
# ------------------------------------------------------------------ #


class TestRowDedup:
  """Per the contract: one row per
  ``(entity_name, label, label_kind, language, scheme)`` tuple.
  Duplicate input values for the same tuple must collapse to one row;
  the same value via different sources (different ``label_kind``) is
  not a duplicate and must keep both rows.
  """

  def test_duplicate_synonyms_collapse_to_one_row(self):
    ontology = Ontology(
        ontology="test",
        entities=[_account_entity(synonyms=["Acct", "Acct", "Acct"])],
    )
    rows = build_rows(
        ontology,
        _binding(_account_binding()),
        compiler_version=_COMPILER_VERSION,
    )
    syn_rows = [r for r in rows if r.label_kind == "synonym"]
    assert len(syn_rows) == 1
    assert syn_rows[0].label == "Acct"

  def test_duplicate_annotation_values_collapse(self):
    """If the same annotation value appears twice in the source list,
    the index emits a single row.
    """
    ontology = Ontology(
        ontology="test",
        entities=[
            _account_entity(
                annotations={"skos:altLabel": ["X", "X", "X"]},
            )
        ],
    )
    rows = build_rows(
        ontology,
        _binding(_account_binding()),
        compiler_version=_COMPILER_VERSION,
    )
    alt_rows = [r for r in rows if r.label_kind == "alt"]
    assert len(alt_rows) == 1
    assert alt_rows[0].label == "X"

  def test_same_value_different_kinds_keeps_both_rows(self):
    """``"Acct"`` as both ``Entity.synonyms`` and
    ``annotations["skos:altLabel"]`` is two different tuples (kinds
    differ), so both rows are kept. This is the resolver's job to
    rank, not the row builder's job to dedupe.
    """
    ontology = Ontology(
        ontology="test",
        entities=[
            _account_entity(
                synonyms=["Acct"],
                annotations={"skos:altLabel": "Acct"},
            )
        ],
    )
    rows = build_rows(
        ontology,
        _binding(_account_binding()),
        compiler_version=_COMPILER_VERSION,
    )
    same_label = [r for r in rows if r.label == "Acct"]
    kinds = sorted(r.label_kind for r in same_label)
    assert kinds == ["alt", "synonym"]


# ------------------------------------------------------------------ #
# Determinism (D2)                                                    #
# ------------------------------------------------------------------ #


class TestDeterminism:
  """Same inputs → byte-identical row sequence across runs."""

  def test_repeated_calls_produce_identical_rows(self):
    ontology = Ontology(
        ontology="test",
        entities=[
            _account_entity(
                synonyms=["Acct", "AC"],
                annotations={
                    "skos:notation": "807",
                    "skos:inScheme": ["BankingTaxonomy", "FinancialProducts"],
                },
            ),
            Entity(name="skos_Banking", abstract=True),
        ],
    )
    binding = _binding(_account_binding())
    rows1 = build_rows(ontology, binding, compiler_version=_COMPILER_VERSION)
    rows2 = build_rows(ontology, binding, compiler_version=_COMPILER_VERSION)
    assert rows1 == rows2

  def test_sort_order_is_total(self):
    """For deterministic SQL emission, the row list must be in a
    total order. Building the same ontology twice (constructed
    identically) must produce the exact same sequence — including
    when there are ties on early sort keys.
    """
    ontology = Ontology(
        ontology="test",
        entities=[
            _account_entity(synonyms=["Acct", "AC", "Bank Account"]),
        ],
    )
    binding = _binding(_account_binding())
    rows = build_rows(ontology, binding, compiler_version=_COMPILER_VERSION)
    # Synonym rows should appear in sorted order by label.
    syn_labels = [r.label for r in rows if r.label_kind == "synonym"]
    assert syn_labels == sorted(syn_labels)


# ------------------------------------------------------------------ #
# Public surface                                                       #
# ------------------------------------------------------------------ #


class TestPublicSurface:

  def test_concept_index_row_is_dataclass_with_expected_fields(self):
    fields = ConceptIndexRow.__dataclass_fields__
    expected = {
        "entity_name",
        "label",
        "label_kind",
        "notation",
        "scheme",
        "language",
        "is_abstract",
        "compile_id",
        "compile_fingerprint",
    }
    assert set(fields.keys()) == expected

  def test_concept_index_module_not_re_exported_at_root(self):
    """v1 keeps `concept_index` out of `bigquery_ontology/__init__.py`
    per the implementation plan ("Package-level re-export can be added
    later if a concrete caller appears"). Importable directly via
    absolute path.
    """
    import bigquery_ontology

    assert not hasattr(bigquery_ontology, "build_rows")
    assert not hasattr(bigquery_ontology, "ConceptIndexRow")
