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

"""Tests for the ontology runtime reader (issue #58 reader
follow-on to PR #92's concept-index emission).

Coverage layered by component:

* :class:`OntologyRuntime` — construction (in-memory + from
  files), read accessors, SKOS / label / annotation
  traversal, provenance properties.
* :class:`ConceptIndexLookup` — fingerprint verification
  (happy / mismatch / meta missing / meta empty); per-query
  defense-in-depth WHERE clause; label-kind / language /
  case-insensitive filters; label / entity_name / notation
  lookups.
* :class:`ExactEntityResolver` — exact match, case
  sensitivity, missing entity.
* :class:`LabelSynonymResolver` — happy path, label-kind
  priority ranking, no-concept-index rejection.

In-memory fake BigQuery client substitutes for the BQ
roundtrip so the suite stays fast + deterministic.
"""

from __future__ import annotations

import pathlib
import tempfile
import textwrap
from typing import Any, Optional

import pytest

from bigquery_ontology._fingerprint import compile_fingerprint
from bigquery_ontology._fingerprint import compile_id
from bigquery_ontology._fingerprint import fingerprint_model

# ------------------------------------------------------------------ #
# Fixtures                                                            #
# ------------------------------------------------------------------ #


_COMPILER_VERSION = "test-compiler 0.0.1"


_ONTOLOGY_YAML = textwrap.dedent(
    """\
    ontology: skos_test
    version: "0.1"
    entities:
      - name: Region
        abstract: true
        synonyms: [Area, Zone]
        annotations:
          skos:prefLabel: Region
          "skos:prefLabel@fr": Région
          skos:altLabel: [Geographical Area, Locale]
          skos:hiddenLabel: GEO
          skos:notation: REG
          skos:inScheme: [GeoScheme, AdminScheme]
      - name: CaliforniaRegion
        extends: Region
        keys:
          primary: [code]
        properties:
          - name: code
            type: string
        synonyms: ["CA", "Cali"]
        annotations:
          skos:notation: "CA"
          skos:inScheme: GeoScheme
      - name: TexasRegion
        extends: Region
        keys:
          primary: [code]
        properties:
          - name: code
            type: string
        annotations:
          skos:notation: "TX"
    """
)


_BINDING_YAML = textwrap.dedent(
    """\
    binding: skos_test_bq
    ontology: skos_test
    target:
      backend: bigquery
      project: test-proj
      dataset: test_ds
    entities:
      - name: CaliforniaRegion
        source: california_regions
        properties:
          - name: code
            column: code
      - name: TexasRegion
        source: texas_regions
        properties:
          - name: code
            column: code
    """
)


@pytest.fixture
def ontology_files(tmp_path):
  ontology_path = tmp_path / "ont.yaml"
  binding_path = tmp_path / "bnd.yaml"
  ontology_path.write_text(_ONTOLOGY_YAML, encoding="utf-8")
  binding_path.write_text(_BINDING_YAML, encoding="utf-8")
  return ontology_path, binding_path


@pytest.fixture
def loaded_models():
  """Reuse one load for tests that don't need file paths.

  Models are immutable so sharing across tests is safe."""
  from bigquery_ontology import load_binding
  from bigquery_ontology import load_ontology

  with tempfile.TemporaryDirectory() as raw:
    tmp = pathlib.Path(raw)
    (tmp / "ont.yaml").write_text(_ONTOLOGY_YAML, encoding="utf-8")
    (tmp / "bnd.yaml").write_text(_BINDING_YAML, encoding="utf-8")
    ontology = load_ontology(str(tmp / "ont.yaml"))
    binding = load_binding(str(tmp / "bnd.yaml"), ontology=ontology)
  return ontology, binding


# ------------------------------------------------------------------ #
# In-memory fake BigQuery client                                      #
# ------------------------------------------------------------------ #


class _FakeJob:
  """Stands in for ``bigquery.QueryJob``. ``.result()``
  returns the configured row iterable; tests build rows as
  dicts (BigQuery rows support ``row[col]`` access, so a
  dict matches the read pattern the lookup uses)."""

  def __init__(self, rows, exception=None):
    self._rows = rows
    self._exception = exception

  def result(self):
    if self._exception is not None:
      raise self._exception
    return iter(self._rows)


class _FakeBQClient:
  """Routes queries by substring match into pre-configured
  result sets. Tests register patterns: the lookup's meta
  query contains ``__meta``; data queries contain the main
  table id but not ``__meta``."""

  def __init__(self):
    self._handlers: list[tuple[str, list]] = []
    self._exceptions: dict[str, Exception] = {}

  def add_handler(self, contains: str, rows):
    """Register a (substring, rows) pair. The first
    registered substring that matches the query wins."""
    self._handlers.append((contains, rows))

  def add_exception(self, contains: str, exception: Exception):
    self._exceptions[contains] = exception

  def query(self, sql, job_config=None):
    for marker, exc in self._exceptions.items():
      if marker in sql:
        return _FakeJob([], exception=exc)
    for contains, rows in self._handlers:
      if contains in sql:
        return _FakeJob(rows)
    raise AssertionError(f"unexpected SQL hit the fake client: {sql!r}")


def _meta_row(fp: str):
  """Build a ``__meta``-row dict shape the lookup reads."""
  return {"compile_fingerprint": fp}


def _data_row(
    *,
    entity_name="CaliforniaRegion",
    label="California",
    label_kind="name",
    notation="CA",
    scheme="GeoScheme",
    language=None,
    is_abstract=False,
    compile_id_="abc123",
    compile_fingerprint="f" * 64,
):
  return {
      "entity_name": entity_name,
      "label": label,
      "label_kind": label_kind,
      "notation": notation,
      "scheme": scheme,
      "language": language,
      "is_abstract": is_abstract,
      "compile_id": compile_id_,
      "compile_fingerprint": compile_fingerprint,
  }


def _expected_fingerprint(ontology, binding) -> str:
  return compile_fingerprint(
      fingerprint_model(ontology),
      fingerprint_model(binding),
      _COMPILER_VERSION,
  )


# ------------------------------------------------------------------ #
# OntologyRuntime — construction + accessors                          #
# ------------------------------------------------------------------ #


class TestOntologyRuntimeConstruction:

  def test_from_models_without_concept_index(self, loaded_models):
    from bigquery_agent_analytics import OntologyRuntime

    ontology, binding = loaded_models
    runtime = OntologyRuntime.from_models(
        ontology=ontology,
        binding=binding,
        compiler_version=_COMPILER_VERSION,
    )
    assert runtime.ontology is ontology
    assert runtime.binding is binding
    assert runtime.concept_index is None
    assert runtime.compiler_version == _COMPILER_VERSION

  def test_from_files_loads_yaml(self, ontology_files):
    from bigquery_agent_analytics import OntologyRuntime

    ontology_path, binding_path = ontology_files
    runtime = OntologyRuntime.from_files(
        ontology_path=ontology_path,
        binding_path=binding_path,
        compiler_version=_COMPILER_VERSION,
    )
    assert runtime.ontology.ontology == "skos_test"
    assert runtime.binding.target.project == "test-proj"

  def test_concept_index_requires_bq_client(self, loaded_models):
    """Passing ``concept_index_table`` without a
    ``bq_client`` is a configuration error — we can't verify
    the fingerprint without a client."""
    from bigquery_agent_analytics import OntologyRuntime

    ontology, binding = loaded_models
    with pytest.raises(ValueError, match=r"bq_client is None"):
      OntologyRuntime.from_models(
          ontology=ontology,
          binding=binding,
          compiler_version=_COMPILER_VERSION,
          concept_index_table="p.d.t",
      )

  def test_construction_verifies_fingerprint_eagerly(self, loaded_models):
    """When a ``concept_index_table`` is supplied, the
    constructor calls :meth:`ConceptIndexLookup.verify` —
    fingerprint mismatch surfaces at startup, not on first
    query."""
    from bigquery_agent_analytics import FingerprintMismatchError
    from bigquery_agent_analytics import OntologyRuntime

    ontology, binding = loaded_models
    bad_fp = "0" * 64
    fake = _FakeBQClient()
    fake.add_handler("__meta", [_meta_row(bad_fp)])

    with pytest.raises(FingerprintMismatchError) as exc_info:
      OntologyRuntime.from_models(
          ontology=ontology,
          binding=binding,
          compiler_version=_COMPILER_VERSION,
          concept_index_table="p.d.t",
          bq_client=fake,
      )
    err = exc_info.value
    assert err.table_id == "p.d.t"
    assert err.actual_compile_fingerprint == bad_fp
    assert err.expected_compile_fingerprint == _expected_fingerprint(
        ontology, binding
    )

  def test_construction_passes_with_matching_fingerprint(self, loaded_models):
    from bigquery_agent_analytics import OntologyRuntime

    ontology, binding = loaded_models
    expected = _expected_fingerprint(ontology, binding)
    fake = _FakeBQClient()
    fake.add_handler("__meta", [_meta_row(expected)])

    runtime = OntologyRuntime.from_models(
        ontology=ontology,
        binding=binding,
        compiler_version=_COMPILER_VERSION,
        concept_index_table="p.d.t",
        bq_client=fake,
    )
    assert runtime.concept_index is not None
    assert runtime.concept_index.expected_compile_fingerprint == expected


class TestOntologyRuntimeAccessors:

  def _runtime(self, loaded_models):
    from bigquery_agent_analytics import OntologyRuntime

    ontology, binding = loaded_models
    return OntologyRuntime.from_models(
        ontology=ontology,
        binding=binding,
        compiler_version=_COMPILER_VERSION,
    )

  def test_entity_lookup(self, loaded_models):
    runtime = self._runtime(loaded_models)
    region = runtime.entity("Region")
    assert region is not None
    assert region.abstract is True

    missing = runtime.entity("Nope")
    assert missing is None

  def test_entity_lookup_case_insensitive(self, loaded_models):
    runtime = self._runtime(loaded_models)
    assert runtime.entity("region", case_insensitive=True) is not None
    assert runtime.entity("REGION", case_insensitive=True) is not None
    assert runtime.entity("region") is None  # case-sensitive default

  def test_entities_returns_declared_order(self, loaded_models):
    runtime = self._runtime(loaded_models)
    names = [e.name for e in runtime.entities()]
    assert names == ["Region", "CaliforniaRegion", "TexasRegion"]

  def test_relationships_empty_when_none_declared(self, loaded_models):
    runtime = self._runtime(loaded_models)
    assert runtime.relationships() == ()
    assert runtime.relationship("Nothing") is None

  def test_synonyms_for(self, loaded_models):
    runtime = self._runtime(loaded_models)
    assert runtime.synonyms_for("Region") == ("Area", "Zone")
    assert runtime.synonyms_for("CaliforniaRegion") == ("CA", "Cali")
    assert runtime.synonyms_for("TexasRegion") == ()
    assert runtime.synonyms_for("Nope") == ()

  def test_annotations_for(self, loaded_models):
    runtime = self._runtime(loaded_models)
    ann = runtime.annotations_for("Region")
    assert ann["skos:prefLabel"] == "Region"
    assert "skos:prefLabel@fr" in ann

    # Missing entity → empty dict, not error.
    assert runtime.annotations_for("Nope") == {}

  def test_schemes_for_handles_list_and_scalar(self, loaded_models):
    """``skos:inScheme`` can be a single string or a list;
    both forms are normalized to a tuple."""
    runtime = self._runtime(loaded_models)
    assert set(runtime.schemes_for("Region")) == {
        "GeoScheme",
        "AdminScheme",
    }
    assert runtime.schemes_for("CaliforniaRegion") == ("GeoScheme",)
    assert runtime.schemes_for("TexasRegion") == ()

  def test_notation_for(self, loaded_models):
    runtime = self._runtime(loaded_models)
    assert runtime.notation_for("Region") == "REG"
    assert runtime.notation_for("CaliforniaRegion") == "CA"
    assert runtime.notation_for("TexasRegion") == "TX"
    assert runtime.notation_for("Nope") is None

  def test_labels_for_includes_name_synonyms_and_skos(self, loaded_models):
    """``labels_for`` synthesizes the kinds the
    concept-index emission would produce so a caller
    comparing in-memory labels to emitted rows sees the
    same vocabulary."""
    runtime = self._runtime(loaded_models)
    labels = runtime.labels_for("Region")
    label_dict = {
        kind: [v for v, k in labels if k == kind]
        for kind in {k for _, k in labels}
    }
    assert "Region" in label_dict["name"]
    assert "Area" in label_dict["synonym"]
    assert "Zone" in label_dict["synonym"]
    assert "Region" in label_dict["pref"]  # skos:prefLabel
    assert "Région" in label_dict["pref"]  # skos:prefLabel@fr
    assert "Geographical Area" in label_dict["alt"]
    assert "Locale" in label_dict["alt"]
    assert "GEO" in label_dict["hidden"]

  def test_provenance_properties(self, loaded_models):
    runtime = self._runtime(loaded_models)
    fp = runtime.compile_fingerprint
    cid = runtime.compile_id
    assert len(fp) == 64
    assert all(c in "0123456789abcdef" for c in fp)
    assert cid == fp[:12]


# ------------------------------------------------------------------ #
# ConceptIndexLookup — verify + lookups                               #
# ------------------------------------------------------------------ #


def _runtime_with_lookup(loaded_models, fake_client):
  from bigquery_agent_analytics import OntologyRuntime

  ontology, binding = loaded_models
  expected = _expected_fingerprint(ontology, binding)
  fake_client.add_handler("__meta", [_meta_row(expected)])
  runtime = OntologyRuntime.from_models(
      ontology=ontology,
      binding=binding,
      compiler_version=_COMPILER_VERSION,
      concept_index_table="p.d.t",
      bq_client=fake_client,
  )
  return runtime, expected


class TestConceptIndexLookupVerify:

  def test_verify_passes_for_matching_fingerprint(self, loaded_models):
    fake = _FakeBQClient()
    runtime, expected = _runtime_with_lookup(loaded_models, fake)
    # Construction already verified; calling again is a
    # no-op (no second query).
    runtime.concept_index.verify()
    assert runtime.concept_index.expected_compile_fingerprint == expected

  def test_verify_rejects_mismatched_fingerprint(self, loaded_models):
    from bigquery_agent_analytics import FingerprintMismatchError
    from bigquery_agent_analytics import OntologyRuntime

    ontology, binding = loaded_models
    fake = _FakeBQClient()
    fake.add_handler("__meta", [_meta_row("0" * 64)])

    with pytest.raises(FingerprintMismatchError):
      OntologyRuntime.from_models(
          ontology=ontology,
          binding=binding,
          compiler_version=_COMPILER_VERSION,
          concept_index_table="p.d.t",
          bq_client=fake,
      )

  def test_verify_rejects_missing_meta_table(self, loaded_models):
    from bigquery_agent_analytics import MetaTableMissingError
    from bigquery_agent_analytics import OntologyRuntime

    ontology, binding = loaded_models
    fake = _FakeBQClient()
    fake.add_exception("__meta", RuntimeError("404 Table not found"))

    with pytest.raises(MetaTableMissingError) as exc_info:
      OntologyRuntime.from_models(
          ontology=ontology,
          binding=binding,
          compiler_version=_COMPILER_VERSION,
          concept_index_table="p.d.t",
          bq_client=fake,
      )
    assert exc_info.value.table_id == "p.d.t"

  def test_verify_rejects_empty_meta_table(self, loaded_models):
    from bigquery_agent_analytics import MetaTableEmptyError
    from bigquery_agent_analytics import OntologyRuntime

    ontology, binding = loaded_models
    fake = _FakeBQClient()
    fake.add_handler("__meta", [])  # zero rows

    with pytest.raises(MetaTableEmptyError):
      OntologyRuntime.from_models(
          ontology=ontology,
          binding=binding,
          compiler_version=_COMPILER_VERSION,
          concept_index_table="p.d.t",
          bq_client=fake,
      )


class TestConceptIndexLookupQueries:

  def test_lookup_by_label_returns_row_views(self, loaded_models):
    fake = _FakeBQClient()
    runtime, expected = _runtime_with_lookup(loaded_models, fake)
    fake.add_handler(
        "SELECT entity_name",
        [
            _data_row(compile_fingerprint=expected),
            _data_row(
                entity_name="TexasRegion",
                label="California",  # bogus shared label for the test
                label_kind="synonym",
                notation="TX",
                compile_fingerprint=expected,
            ),
        ],
    )

    rows = runtime.concept_index.lookup_by_label("California")
    assert len(rows) == 2
    assert rows[0].entity_name == "CaliforniaRegion"
    assert rows[0].label_kind == "name"
    assert rows[0].is_abstract is False

  def test_lookup_includes_defense_in_depth_where_clause(self, loaded_models):
    """Every data query MUST include
    ``WHERE compile_fingerprint = @expected_fp`` so rows
    with a stale fingerprint can't slip through even if the
    table is partially-corrupted between verify and query."""

    class _CaptureClient(_FakeBQClient):
      captured_sql: list = []

      def query(self, sql, job_config=None):
        self.captured_sql.append(sql)
        return super().query(sql, job_config=job_config)

    fake = _CaptureClient()
    runtime, expected = _runtime_with_lookup(loaded_models, fake)
    fake.add_handler("SELECT entity_name", [])

    runtime.concept_index.lookup_by_label("anything")
    data_queries = [s for s in fake.captured_sql if "SELECT entity_name" in s]
    assert len(data_queries) == 1
    assert "compile_fingerprint = @expected_fp" in data_queries[0]

  def test_lookup_by_label_with_label_kinds_filter(self, loaded_models):
    fake = _FakeBQClient()
    runtime, expected = _runtime_with_lookup(loaded_models, fake)
    fake.add_handler(
        "label_kind IN UNNEST(@label_kinds)",
        [_data_row(compile_fingerprint=expected)],
    )

    rows = runtime.concept_index.lookup_by_label(
        "California", label_kinds=("name", "pref")
    )
    assert len(rows) == 1

  def test_lookup_by_label_with_language_filter(self, loaded_models):
    fake = _FakeBQClient()
    runtime, expected = _runtime_with_lookup(loaded_models, fake)
    fake.add_handler(
        "language = @language",
        [_data_row(language="fr", compile_fingerprint=expected)],
    )

    rows = runtime.concept_index.lookup_by_label("California", language="fr")
    assert rows[0].language == "fr"

  def test_lookup_by_entity_name(self, loaded_models):
    fake = _FakeBQClient()
    runtime, expected = _runtime_with_lookup(loaded_models, fake)
    fake.add_handler(
        "entity_name = @entity_name",
        [
            _data_row(label="California", compile_fingerprint=expected),
            _data_row(
                label="Cali", label_kind="synonym", compile_fingerprint=expected
            ),
        ],
    )

    rows = runtime.concept_index.lookup_by_entity_name("CaliforniaRegion")
    assert {r.label for r in rows} == {"California", "Cali"}

  def test_lookup_by_notation(self, loaded_models):
    fake = _FakeBQClient()
    runtime, expected = _runtime_with_lookup(loaded_models, fake)
    fake.add_handler(
        "notation = @notation",
        [_data_row(compile_fingerprint=expected)],
    )

    rows = runtime.concept_index.lookup_by_notation("CA")
    assert rows[0].notation == "CA"

  def test_lookup_case_insensitive_by_default(self, loaded_models):
    """Default is case-insensitive so operator queries
    aren't tripped by capitalization differences."""
    fake = _FakeBQClient()
    runtime, _ = _runtime_with_lookup(loaded_models, fake)

    captured = []

    class _CaptureClient2(_FakeBQClient):

      def query(self, sql, job_config=None):
        captured.append(sql)
        return super().query(sql, job_config=job_config)

    # Rebuild with a capturing client
    fake2 = _CaptureClient2()
    runtime2, expected2 = _runtime_with_lookup(loaded_models, fake2)
    fake2.add_handler("SELECT entity_name", [])

    runtime2.concept_index.lookup_by_label("california")
    data = [s for s in captured if "SELECT entity_name" in s]
    assert "LOWER(label) = LOWER(@label)" in data[0]

  def test_lookup_empty_result_is_not_an_error(self, loaded_models):
    fake = _FakeBQClient()
    runtime, _ = _runtime_with_lookup(loaded_models, fake)
    fake.add_handler("SELECT entity_name", [])

    rows = runtime.concept_index.lookup_by_label("no-such-label")
    assert rows == []


# ------------------------------------------------------------------ #
# ExactEntityResolver                                                 #
# ------------------------------------------------------------------ #


class TestExactEntityResolver:

  def _runtime(self, loaded_models):
    from bigquery_agent_analytics import OntologyRuntime

    ontology, binding = loaded_models
    return OntologyRuntime.from_models(
        ontology=ontology,
        binding=binding,
        compiler_version=_COMPILER_VERSION,
    )

  def test_resolves_known_entity(self, loaded_models):
    from bigquery_agent_analytics import ExactEntityResolver

    runtime = self._runtime(loaded_models)
    candidates = ExactEntityResolver(runtime).resolve("CaliforniaRegion")
    assert len(candidates) == 1
    c = candidates[0]
    assert c.entity_name == "CaliforniaRegion"
    assert c.matched_label_kind == "name"
    assert c.notation == "CA"
    assert c.compile_fingerprint == runtime.compile_fingerprint

  def test_missing_entity_returns_empty(self, loaded_models):
    from bigquery_agent_analytics import ExactEntityResolver

    runtime = self._runtime(loaded_models)
    assert ExactEntityResolver(runtime).resolve("Nope") == []

  def test_case_sensitive_by_default(self, loaded_models):
    from bigquery_agent_analytics import ExactEntityResolver

    runtime = self._runtime(loaded_models)
    assert ExactEntityResolver(runtime).resolve("californiaregion") == []

  def test_case_insensitive_mode(self, loaded_models):
    from bigquery_agent_analytics import ExactEntityResolver

    runtime = self._runtime(loaded_models)
    resolver = ExactEntityResolver(runtime, case_insensitive=True)
    assert len(resolver.resolve("californiaregion")) == 1

  def test_empty_query_returns_empty(self, loaded_models):
    from bigquery_agent_analytics import ExactEntityResolver

    runtime = self._runtime(loaded_models)
    assert ExactEntityResolver(runtime).resolve("") == []


# ------------------------------------------------------------------ #
# LabelSynonymResolver                                                #
# ------------------------------------------------------------------ #


class TestLabelSynonymResolver:

  def test_requires_concept_index(self, loaded_models):
    """Construction fails fast when the runtime has no
    concept index — fuzzier in-memory resolution isn't
    shipped in this slice."""
    from bigquery_agent_analytics import LabelSynonymResolver
    from bigquery_agent_analytics import OntologyRuntime

    ontology, binding = loaded_models
    runtime = OntologyRuntime.from_models(
        ontology=ontology,
        binding=binding,
        compiler_version=_COMPILER_VERSION,
    )
    with pytest.raises(ValueError, match=r"requires runtime.concept_index"):
      LabelSynonymResolver(runtime)

  def test_resolves_via_concept_index(self, loaded_models):
    from bigquery_agent_analytics import LabelSynonymResolver

    fake = _FakeBQClient()
    runtime, expected = _runtime_with_lookup(loaded_models, fake)
    fake.add_handler(
        "SELECT entity_name",
        [_data_row(compile_fingerprint=expected)],
    )
    resolver = LabelSynonymResolver(runtime)

    candidates = resolver.resolve("California")
    assert len(candidates) == 1
    assert candidates[0].entity_name == "CaliforniaRegion"
    assert candidates[0].matched_label_kind == "name"

  def test_ranks_by_label_kind_priority(self, loaded_models):
    """A mix of label kinds for the same query gets re-
    sorted so ``name`` beats ``pref`` beats ``synonym``
    beats ``notation``."""
    from bigquery_agent_analytics import LabelSynonymResolver

    fake = _FakeBQClient()
    runtime, expected = _runtime_with_lookup(loaded_models, fake)
    # Emission order is arbitrary; the resolver must
    # re-rank.
    fake.add_handler(
        "SELECT entity_name",
        [
            _data_row(
                entity_name="X",
                label="q",
                label_kind="synonym",
                compile_fingerprint=expected,
            ),
            _data_row(
                entity_name="Y",
                label="q",
                label_kind="name",
                compile_fingerprint=expected,
            ),
            _data_row(
                entity_name="Z",
                label="q",
                label_kind="notation",
                compile_fingerprint=expected,
            ),
            _data_row(
                entity_name="W",
                label="q",
                label_kind="pref",
                compile_fingerprint=expected,
            ),
        ],
    )

    resolver = LabelSynonymResolver(runtime)
    candidates = resolver.resolve("q")
    kinds = [c.matched_label_kind for c in candidates]
    assert kinds == ["name", "pref", "synonym", "notation"]

  def test_limit_caps_returned_candidates(self, loaded_models):
    from bigquery_agent_analytics import LabelSynonymResolver

    fake = _FakeBQClient()
    runtime, expected = _runtime_with_lookup(loaded_models, fake)
    fake.add_handler(
        "SELECT entity_name",
        [
            _data_row(
                entity_name=f"E{i}",
                label="q",
                compile_fingerprint=expected,
            )
            for i in range(20)
        ],
    )

    resolver = LabelSynonymResolver(runtime)
    assert len(resolver.resolve("q", limit=5)) == 5

  def test_empty_query_returns_empty(self, loaded_models):
    from bigquery_agent_analytics import LabelSynonymResolver

    fake = _FakeBQClient()
    runtime, _ = _runtime_with_lookup(loaded_models, fake)
    resolver = LabelSynonymResolver(runtime)
    assert resolver.resolve("") == []


# ------------------------------------------------------------------ #
# EntityResolver Protocol                                             #
# ------------------------------------------------------------------ #


class TestEntityResolverProtocol:

  def test_exact_resolver_satisfies_protocol(self, loaded_models):
    from bigquery_agent_analytics import EntityResolver
    from bigquery_agent_analytics import ExactEntityResolver
    from bigquery_agent_analytics import OntologyRuntime

    ontology, binding = loaded_models
    runtime = OntologyRuntime.from_models(
        ontology=ontology,
        binding=binding,
        compiler_version=_COMPILER_VERSION,
    )
    resolver = ExactEntityResolver(runtime)
    assert isinstance(resolver, EntityResolver)

  def test_label_resolver_satisfies_protocol(self, loaded_models):
    from bigquery_agent_analytics import EntityResolver
    from bigquery_agent_analytics import LabelSynonymResolver

    fake = _FakeBQClient()
    runtime, _ = _runtime_with_lookup(loaded_models, fake)
    resolver = LabelSynonymResolver(runtime)
    assert isinstance(resolver, EntityResolver)
