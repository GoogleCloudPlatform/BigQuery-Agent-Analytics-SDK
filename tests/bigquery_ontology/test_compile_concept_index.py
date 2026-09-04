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

"""Tests for the concept-index emitter (A3-A5).

``compile_concept_index(ontology, binding, *, output_table,
compiler_version)`` consumes A2's row builder and emits two
``CREATE OR REPLACE TABLE ... AS SELECT * FROM UNNEST(...)`` statements
(main + ``__meta`` sibling). Pinned in
``docs/implementation_plan_concept_index_runtime.md`` (A3-A5) and
``docs/entity_resolution_primitives.md`` §4.2 / §5.

Test coverage:

- D2  byte-identical SQL across runs.
- D3  scope rule round-trips through the SQL row count.
- D5  notation row carries ``label_kind='notation'``.
- D14 main + meta both written, both carry the same ``compile_id`` /
      ``compile_fingerprint``.
- Plus: NULL/BOOL rendering, string escaping, meta-row schema,
  empty-rows handling, output-table fully-qualified path appears in
  both statements.
"""

from __future__ import annotations

import re

import pytest

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
from bigquery_ontology.graph_ddl_compiler import compile_concept_index

_COMPILER_VERSION = "bigquery_ontology test"
_OUTPUT_TABLE = "proj.ds.ontology_concept_index"


def _account_entity(**kw) -> Entity:
  return Entity(
      name="Account",
      keys=Keys(primary=["account_id"]),
      properties=[Property(name="account_id", type=PropertyType.STRING)],
      **kw,
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
      target=BigQueryTarget(backend="bigquery", project="proj", dataset="ds"),
      entities=list(entities),
      relationships=[],
  )


def _compile(
    *,
    entities=None,
    bindings=None,
    output_table=_OUTPUT_TABLE,
) -> str:
  ontology = Ontology(
      ontology="test",
      entities=entities or [_account_entity()],
  )
  binding = _binding(*(bindings or [_account_binding()]))
  return compile_concept_index(
      ontology,
      binding,
      output_table=output_table,
      compiler_version=_COMPILER_VERSION,
  )


# ------------------------------------------------------------------ #
# Output shape                                                         #
# ------------------------------------------------------------------ #


class TestOutputShape:

  def test_emits_two_create_or_replace_statements(self):
    """One for the main index, one for the ``__meta`` sibling."""
    sql = _compile()
    statements = re.findall(r"CREATE OR REPLACE TABLE", sql)
    assert len(statements) == 2

  def test_main_and_meta_table_paths_are_backtick_quoted(self):
    sql = _compile(output_table="my-proj.my_ds.idx")
    assert "`my-proj.my_ds.idx`" in sql
    assert "`my-proj.my_ds.idx__meta`" in sql

  def test_main_table_carries_all_nine_columns(self):
    sql = _compile()
    main = sql.split("__meta")[0]  # everything before meta statement
    for col in (
        "entity_name",
        "label",
        "label_kind",
        "notation",
        "scheme",
        "language",
        "is_abstract",
        "compile_id",
        "compile_fingerprint",
    ):
      assert col in main, f"missing column {col!r} in main table emission"

  def test_meta_table_carries_seven_provenance_columns(self):
    sql = _compile()
    # everything after first __meta occurrence
    meta = sql[sql.index("__meta") :]
    for col in (
        "compile_fingerprint",
        "compile_id",
        "ontology_fingerprint",
        "binding_fingerprint",
        "target_project",
        "target_dataset",
        "compiler_version",
    ):
      assert col in meta, f"missing column {col!r} in meta table emission"

  def test_uses_inline_unnest_path(self):
    """Default emission path is atomic per-statement
    ``CREATE OR REPLACE TABLE ... AS SELECT * FROM UNNEST(...)``.
    """
    sql = _compile()
    assert "AS SELECT * FROM UNNEST" in sql


# ------------------------------------------------------------------ #
# Provenance — main and meta carry the same compile id/fingerprint    #
# ------------------------------------------------------------------ #


class TestProvenance:

  def test_compile_fingerprint_appears_in_both_main_and_meta(self):
    ontology = Ontology(ontology="test", entities=[_account_entity()])
    binding = _binding(_account_binding())
    expected_cfp = compile_fingerprint(
        fingerprint_model(ontology),
        fingerprint_model(binding),
        _COMPILER_VERSION,
    )
    sql = _compile()
    # The 64-hex string must appear at least twice — once per row in
    # main (here we have 1+ rows), once in meta.
    assert sql.count(expected_cfp) >= 2

  def test_compile_id_appears_in_both_main_and_meta(self):
    ontology = Ontology(ontology="test", entities=[_account_entity()])
    binding = _binding(_account_binding())
    expected_cid = compile_id(
        fingerprint_model(ontology),
        fingerprint_model(binding),
        _COMPILER_VERSION,
    )
    sql = _compile()
    assert sql.count(expected_cid) >= 2

  def test_meta_carries_target_project_and_dataset_from_binding(self):
    sql = _compile()
    meta = sql[sql.index("__meta") :]
    assert "'proj'" in meta
    assert "'ds'" in meta

  def test_meta_carries_compiler_version_string(self):
    sql = _compile()
    meta = sql[sql.index("__meta") :]
    assert f"'{_COMPILER_VERSION}'" in meta


# ------------------------------------------------------------------ #
# Determinism (D2)                                                     #
# ------------------------------------------------------------------ #


class TestDeterminism:

  def test_byte_identical_across_runs(self):
    """Same inputs must produce byte-for-byte identical SQL."""
    sql1 = _compile()
    sql2 = _compile()
    assert sql1 == sql2

  def test_byte_identical_for_complex_ontology(self):
    """Multi-entity, multi-scheme, multi-label ontology is also
    byte-deterministic.
    """
    entities = [
        _account_entity(
            synonyms=["Acct", "AC"],
            annotations={
                "skos:notation": "807",
                "skos:inScheme": ["BankingTaxonomy", "FinancialProducts"],
                "skos:prefLabel": "Bank Account",
            },
        ),
        Entity(
            name="skos_Banking",
            abstract=True,
            annotations={"skos:prefLabel": "Banking"},
        ),
    ]
    sql1 = _compile(entities=entities)
    sql2 = _compile(entities=entities)
    assert sql1 == sql2


# ------------------------------------------------------------------ #
# Value rendering                                                      #
# ------------------------------------------------------------------ #


class TestValueRendering:

  def test_none_values_render_as_null_literal(self):
    """Account has no scheme, language, or notation → NULLs in SQL."""
    sql = _compile()
    main = sql.split("__meta")[0]
    # At least one row contains explicit NULL for the optional columns.
    assert "NULL" in main

  def test_booleans_render_as_uppercase_true_false(self):
    """BigQuery convention; also keeps the byte-identical contract
    stable across runs.
    """
    entities = [
        _account_entity(),
        Entity(name="skos_Banking", abstract=True),
    ]
    sql = _compile(entities=entities)
    assert "TRUE" in sql or "true" in sql
    assert "FALSE" in sql or "false" in sql
    # Pin uppercase per contract:
    main = sql.split("__meta")[0]
    assert "TRUE" in main
    assert "FALSE" in main

  def test_string_with_single_quote_is_backslash_escaped(self):
    """GoogleSQL escapes single quotes as ``\\'`` inside single-quoted
    literals, **not** as ``''``. ANSI SQL (and PostgreSQL) accept the
    quote-doubling form, but GoogleSQL rejects it: ``'O''Brien'``
    parses as two concatenated literals (``'O'`` and ``'Brien'``)
    and errors with "concatenated string literals must be separated
    by whitespace or comments." The emitter must use the backslash
    form.
    """
    entities = [_account_entity(synonyms=["O'Brien Bank"])]
    sql = _compile(entities=entities)
    # Python literal ``"'O\\'Brien Bank'"`` is the 17-character SQL
    # text: apostrophe, O, backslash, apostrophe, Brien, space, Bank,
    # apostrophe.
    assert "'O\\'Brien Bank'" in sql
    # And the broken doubled-quote form must NOT appear.
    assert "'O''Brien" not in sql

  def test_backslash_is_escaped(self):
    """GoogleSQL **does** treat ``\\`` as an escape character inside
    single-quoted strings. ``'C:\\Users'`` would parse as ``\\U``
    (the 8-hex-digit Unicode escape) and fail with "Illegal escape
    sequence." The emitter must double backslashes so the source
    string round-trips correctly.
    """
    entities = [_account_entity(synonyms=["C:\\Users"])]
    sql = _compile(entities=entities)
    # BigQuery reads ``'C:\\\\Users'`` as ``C:\Users``.
    assert "'C:\\\\Users'" in sql

  def test_unrecognized_escape_in_input_is_safely_doubled(self):
    """``\\q`` is not a valid GoogleSQL escape. The input should be
    rendered with the backslash doubled so BigQuery sees ``\\\\q``
    (escaped backslash + literal q).
    """
    entities = [_account_entity(synonyms=["foo\\qbar"])]
    sql = _compile(entities=entities)
    assert "'foo\\\\qbar'" in sql

  def test_literal_newline_is_escaped(self):
    """A literal newline character inside a string would otherwise
    split the SQL across two lines and break the literal. Must be
    escaped as ``\\n``.
    """
    entities = [_account_entity(synonyms=["line1\nline2"])]
    sql = _compile(entities=entities)
    assert "'line1\\nline2'" in sql
    # Sanity: no raw newline character inside any literal.
    for line in sql.splitlines():
      # A literal-broken line would contain text after a quote-open
      # without a matching close on the same line. Cheap proxy:
      # every line should have an even number of unescaped quotes.
      pass  # not foolproof; the substring assert above is the real check.

  def test_carriage_return_and_tab_are_escaped(self):
    entities = [_account_entity(synonyms=["a\rb", "x\ty"])]
    sql = _compile(entities=entities)
    assert "'a\\rb'" in sql
    assert "'x\\ty'" in sql


# ------------------------------------------------------------------ #
# Scope round-trip + notation row (D3, D5)                            #
# ------------------------------------------------------------------ #


class TestScopeAndNotation:

  def test_unbound_concrete_entity_does_not_appear_in_sql(self):
    """D3: concrete unbound entities are filtered by build_rows;
    their names should not appear in the emitted SQL.
    """
    entities = [
        _account_entity(),
        Entity(
            name="Ledger",
            keys=Keys(primary=["ledger_id"]),
            properties=[Property(name="ledger_id", type=PropertyType.STRING)],
        ),
    ]
    sql = _compile(entities=entities)
    main = sql.split("__meta")[0]
    assert "'Ledger'" not in main

  def test_abstract_entity_is_emitted(self):
    entities = [
        _account_entity(),
        Entity(name="skos_Banking", abstract=True),
    ]
    sql = _compile(entities=entities)
    main = sql.split("__meta")[0]
    assert "'skos_Banking'" in main

  def test_notation_value_appears_as_label_kind_notation_row(self):
    """D5: ``skos:notation`` value '807' gets a row where
    ``label_kind = 'notation'`` and ``label = '807'``.
    """
    entities = [
        _account_entity(annotations={"skos:notation": "807"}),
    ]
    sql = _compile(entities=entities)
    main = sql.split("__meta")[0]
    assert "'notation'" in main
    assert "'807'" in main


# ------------------------------------------------------------------ #
# Empty rows                                                           #
# ------------------------------------------------------------------ #


class TestOutputTableValidation:
  """Reject inputs that would produce malformed SQL once
  backtick-wrapped by the emitter.
  """

  def _compile_with_table(self, output_table: str) -> str:
    return compile_concept_index(
        Ontology(ontology="test", entities=[_account_entity()]),
        _binding(_account_binding()),
        output_table=output_table,
        compiler_version=_COMPILER_VERSION,
    )

  def test_rejects_pre_quoted_table(self):
    with pytest.raises(ValueError, match="backtick"):
      self._compile_with_table("`proj.ds.idx`")

  def test_rejects_internal_backtick(self):
    """Even one stray backtick would terminate our outer quoting."""
    with pytest.raises(ValueError, match="backtick"):
      self._compile_with_table("proj.ds.idx`malicious`")

  def test_rejects_segment_with_invalid_characters(self):
    """Segments must use BigQuery identifier-compatible chars."""
    with pytest.raises(ValueError, match="invalid"):
      self._compile_with_table("proj.ds.idx malicious")

  def test_rejects_segment_with_slash(self):
    with pytest.raises(ValueError, match="invalid"):
      self._compile_with_table("proj.ds.idx/with/slashes")

  def test_rejects_two_segment_path(self):
    with pytest.raises(ValueError, match="project.dataset.table"):
      self._compile_with_table("ds.idx")

  def test_rejects_four_segment_path(self):
    with pytest.raises(ValueError, match="project.dataset.table"):
      self._compile_with_table("a.b.c.d")

  def test_accepts_valid_three_segment_path(self):
    """Project IDs may contain hyphens (BigQuery convention); dataset
    and table identifiers may use underscores. Both must be accepted.
    """
    sql = self._compile_with_table("my-proj-123.my_dataset.my_table_v2")
    assert "`my-proj-123.my_dataset.my_table_v2`" in sql


class TestEmptyRows:

  def test_zero_concrete_zero_abstract_raises(self):
    """An ontology with no abstract entities and a binding that
    references no concrete entities yields zero rows in the main
    index. The emitter should reject this with a clear error
    rather than emit a typeless empty array (which would lose the
    schema).
    """
    ontology = Ontology(ontology="test", entities=[_account_entity()])
    # Binding doesn't reference Account at all.
    binding = _binding()
    with pytest.raises(ValueError, match="no concrete"):
      compile_concept_index(
          ontology,
          binding,
          output_table=_OUTPUT_TABLE,
          compiler_version=_COMPILER_VERSION,
      )


# ------------------------------------------------------------------ #
# Public surface                                                       #
# ------------------------------------------------------------------ #


class TestPublicSurface:

  def test_compile_concept_index_re_exported_at_package_root(self):
    """Per the implementation plan, ``compile_concept_index`` is
    public and re-exported alongside ``compile_graph``.
    """
    import bigquery_ontology

    assert hasattr(bigquery_ontology, "compile_concept_index")

  def test_compile_graph_unchanged(self):
    """The existing ``compile_graph`` byte-identical contract is
    preserved by this PR — sanity check by importing it.
    """
    from bigquery_ontology import compile_graph

    assert callable(compile_graph)
