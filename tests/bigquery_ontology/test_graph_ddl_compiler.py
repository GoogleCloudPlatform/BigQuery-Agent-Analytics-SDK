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

"""Tests for the compiler in ``src/bigquery_ontology/compiler.py``.

Style mirrors the other test files in this package: literal YAML
inputs, full-text expected outputs. The large ``test_compiles_finance_…``
test is a *golden* — it asserts byte-identical DDL, which is both a
functional check (the emitter produces valid-looking BigQuery DDL)
and a regression guard for the determinism contract. Small targeted
tests exercise each compile-time rule on its own.
"""

from __future__ import annotations

import textwrap

import pytest

from bigquery_ontology import compile_graph
from bigquery_ontology import load_binding_from_string
from bigquery_ontology import load_ontology_from_string

# --------------------------------------------------------------------- #
# Shared finance fixture                                                 #
# --------------------------------------------------------------------- #
#
# This pair is the compilation spec's worked example, completed with
# the ``Security`` node table (the spec fragment elides it, but a
# binding that edges from ``HOLDS`` to ``Security`` must also bind
# ``Security`` — that's a loader-level rule). Multiple tests reuse
# these strings so changes to the emitted-DDL format land in one place.


_FINANCE_ONTOLOGY = """
  ontology: finance
  entities:
    - name: Person
      keys: {primary: [person_id]}
      properties:
        - {name: person_id, type: string}
        - {name: name, type: string}
        - {name: first_name, type: string}
        - {name: last_name, type: string}
        - name: full_name
          type: string
          expr: "first_name || ' ' || last_name"
    - name: Account
      keys: {primary: [account_id]}
      properties:
        - {name: account_id, type: string}
        - {name: opened_at, type: timestamp}
    - name: Security
      keys: {primary: [security_id]}
      properties:
        - {name: security_id, type: string}
  relationships:
    - name: HOLDS
      from: Account
      to: Security
      properties:
        - {name: as_of, type: timestamp}
        - {name: quantity, type: double}
"""

_FINANCE_BINDING = """
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
        - {name: person_id, column: person_id}
        - {name: name, column: display_name}
        - {name: first_name, column: given_name}
        - {name: last_name, column: family_name}
    - name: Account
      source: raw.accounts
      properties:
        - {name: account_id, column: acct_id}
        - {name: opened_at, column: created_ts}
    - name: Security
      source: ref.securities
      properties:
        - {name: security_id, column: cusip}
  relationships:
    - name: HOLDS
      source: raw.holdings
      from_columns: [account_id]
      to_columns: [security_id]
      properties:
        - {name: as_of, column: snapshot_date}
        - {name: quantity, column: qty}
"""


def _load(ontology_yaml: str, binding_yaml: str):
  ontology = load_ontology_from_string(textwrap.dedent(ontology_yaml).lstrip())
  binding = load_binding_from_string(
      textwrap.dedent(binding_yaml).lstrip(), ontology=ontology
  )
  return ontology, binding


# --------------------------------------------------------------------- #
# Golden: the spec's worked example                                      #
# --------------------------------------------------------------------- #


def test_compiles_finance_worked_example_to_exact_ddl():
  # This is the test that proves the pipeline end-to-end:
  # ontology YAML + binding YAML → one ``CREATE PROPERTY GRAPH``
  # statement matching the compilation spec's worked example. It
  # exercises every interesting path at once — alphabetical node and
  # edge ordering (Account, Person, Security), short vs. wrapped
  # property lists, rename vs. passthrough column mapping, derived
  # expression substitution (``full_name``), and edge-endpoint
  # wiring via the ``REFERENCES <alias> (<key_cols>)`` clauses.
  ontology, binding = _load(_FINANCE_ONTOLOGY, _FINANCE_BINDING)

  ddl = compile_graph(ontology, binding)

  expected = textwrap.dedent(
      """\
      CREATE PROPERTY GRAPH finance
        NODE TABLES (
          raw.accounts AS Account
            KEY (acct_id)
            LABEL Account PROPERTIES (acct_id AS account_id, created_ts AS opened_at),
          raw.persons AS Person
            KEY (person_id)
            LABEL Person PROPERTIES (
              person_id,
              display_name AS name,
              given_name AS first_name,
              family_name AS last_name,
              (given_name || ' ' || family_name) AS full_name
            ),
          ref.securities AS Security
            KEY (cusip)
            LABEL Security PROPERTIES (cusip AS security_id)
        )
        EDGE TABLES (
          raw.holdings AS HOLDS
            KEY (account_id, security_id)
            SOURCE KEY (account_id) REFERENCES Account (acct_id)
            DESTINATION KEY (security_id) REFERENCES Security (cusip)
            LABEL HOLDS PROPERTIES (snapshot_date AS as_of, qty AS quantity)
        );
      """
  )
  assert ddl == expected


def test_compile_is_deterministic():
  # Determinism contract: same inputs, byte-identical output across
  # runs. Not exciting on its own, but it guards against accidental
  # use of dict iteration order or set traversal in the resolver or
  # emitter — both of which would pass a single run but diverge
  # between Python versions or across processes.
  ontology, binding = _load(_FINANCE_ONTOLOGY, _FINANCE_BINDING)
  assert compile_graph(ontology, binding) == compile_graph(ontology, binding)


# --------------------------------------------------------------------- #
# Smaller shape tests                                                    #
# --------------------------------------------------------------------- #


def test_single_entity_without_relationships_produces_node_tables_only():
  # Confirms the emitter skips the EDGE TABLES block cleanly when the
  # binding has no relationships — no stray commas, no empty
  # parentheses, ``;`` lands on the NODE TABLES closing paren.
  ontology_yaml = """
    ontology: tiny
    entities:
      - name: Thing
        keys: {primary: [id]}
        properties:
          - {name: id, type: string}
  """
  binding_yaml = """
    binding: b
    ontology: tiny
    target: {backend: bigquery, project: p, dataset: d}
    entities:
      - name: Thing
        source: raw.things
        properties:
          - {name: id, column: id}
  """
  ontology, binding = _load(ontology_yaml, binding_yaml)
  expected = textwrap.dedent(
      """\
      CREATE PROPERTY GRAPH tiny
        NODE TABLES (
          raw.things AS Thing
            KEY (id)
            LABEL Thing PROPERTIES (id)
        );
      """
  )
  assert compile_graph(ontology, binding) == expected


def test_property_with_same_logical_and_physical_name_omits_as():
  # A stored property whose bound column equals its logical name
  # should emit as the bare column identifier, not ``col AS col``.
  # Covered incidentally by the golden (``person_id``), pinned
  # explicitly here so a format refactor can't silently regress it.
  ontology_yaml = """
    ontology: o
    entities:
      - name: E
        keys: {primary: [id]}
        properties:
          - {name: id, type: string}
  """
  binding_yaml = """
    binding: b
    ontology: o
    target: {backend: bigquery, project: p, dataset: d}
    entities:
      - name: E
        source: t
        properties:
          - {name: id, column: id}
  """
  ddl = compile_graph(*_load(ontology_yaml, binding_yaml))
  assert "LABEL E PROPERTIES (id)" in ddl
  assert "id AS id" not in ddl


def test_derived_expression_recursively_substitutes_derived_dependencies():
  # A derived property that references another derived property must
  # substitute the inner expression and wrap it in parentheses so
  # precedence survives. Here ``label`` depends on ``combined`` which
  # in turn depends on ``a`` and ``b`` (stored). We assert the final
  # emitted SQL splices column names, not property names, into both
  # levels.
  ontology_yaml = """
    ontology: o
    entities:
      - name: E
        keys: {primary: [id]}
        properties:
          - {name: id, type: string}
          - {name: a, type: string}
          - {name: b, type: string}
          - {name: combined, type: string, expr: "a || b"}
          - {name: label, type: string, expr: "'#' || combined"}
  """
  binding_yaml = """
    binding: b
    ontology: o
    target: {backend: bigquery, project: p, dataset: d}
    entities:
      - name: E
        source: t
        properties:
          - {name: id, column: row_id}
          - {name: a, column: col_a}
          - {name: b, column: col_b}
  """
  ddl = compile_graph(*_load(ontology_yaml, binding_yaml))
  assert "(col_a || col_b) AS combined" in ddl
  assert "('#' || (col_a || col_b)) AS label" in ddl


# --------------------------------------------------------------------- #
# Edge key emission                                                      #
# --------------------------------------------------------------------- #
#
# Every edge gets a ``KEY (...)`` clause in the emitted DDL, because
# BigQuery's graph model wants row-level identity on edges even when
# the ontology does not spell one out. The three tests below pin the
# three cases:
#
#   - ``keys.primary`` declared (e.g. ``TRANSFER`` with
#     ``transaction_id``) → KEY is the bound primary columns alone.
#   - ``keys.additional`` declared (e.g. ``HOLDS`` with ``as_of``) →
#     KEY is the endpoint columns plus the additional columns,
#     making the row globally unique as BigQuery expects.
#   - No keys declared → KEY falls back to the endpoint columns
#     alone, expressing "one edge per endpoint pair" as the safest
#     default. Authors who want actual multi-edges add
#     ``keys.additional``.


def test_edge_with_primary_key_emits_standalone_key_clause():
  # TRANSFER-style: ``transaction_id`` alone identifies any row in
  # ``raw.transactions``. The KEY clause maps the primary-key
  # property to its bound column and nothing else.
  ontology_yaml = """
    ontology: o
    entities:
      - name: Account
        keys: {primary: [account_id]}
        properties: [{name: account_id, type: string}]
    relationships:
      - name: TRANSFER
        from: Account
        to: Account
        keys: {primary: [transaction_id]}
        properties:
          - {name: transaction_id, type: string}
          - {name: amount, type: double}
  """
  binding_yaml = """
    binding: b
    ontology: o
    target: {backend: bigquery, project: p, dataset: d}
    entities:
      - name: Account
        source: raw.accounts
        properties:
          - {name: account_id, column: acct_id}
    relationships:
      - name: TRANSFER
        source: raw.transactions
        from_columns: [src_account]
        to_columns: [dst_account]
        properties:
          - {name: transaction_id, column: txn_id}
          - {name: amount, column: amount_usd}
  """
  ddl = compile_graph(*_load(ontology_yaml, binding_yaml))

  expected = textwrap.dedent(
      """\
      CREATE PROPERTY GRAPH o
        NODE TABLES (
          raw.accounts AS Account
            KEY (acct_id)
            LABEL Account PROPERTIES (acct_id AS account_id)
        )
        EDGE TABLES (
          raw.transactions AS TRANSFER
            KEY (txn_id)
            SOURCE KEY (src_account) REFERENCES Account (acct_id)
            DESTINATION KEY (dst_account) REFERENCES Account (acct_id)
            LABEL TRANSFER PROPERTIES (txn_id AS transaction_id, amount_usd AS amount)
        );
      """
  )
  assert ddl == expected


def test_edge_with_additional_key_emits_endpoints_plus_additional():
  # HOLDS-style: for a given ``(account, security)`` pair, ``as_of``
  # is unique. BigQuery's KEY needs a globally-unique tuple, so we
  # prefix the endpoint columns — ``(account_id, security_id,
  # snapshot_date)`` — forming a composite row identifier that
  # matches the ontology's stated uniqueness.
  ontology_yaml = """
    ontology: o
    entities:
      - name: Account
        keys: {primary: [account_id]}
        properties: [{name: account_id, type: string}]
      - name: Security
        keys: {primary: [security_id]}
        properties: [{name: security_id, type: string}]
    relationships:
      - name: HOLDS
        from: Account
        to: Security
        keys: {additional: [as_of]}
        properties:
          - {name: as_of, type: timestamp}
  """
  binding_yaml = """
    binding: b
    ontology: o
    target: {backend: bigquery, project: p, dataset: d}
    entities:
      - name: Account
        source: raw.accounts
        properties: [{name: account_id, column: acct_id}]
      - name: Security
        source: ref.securities
        properties: [{name: security_id, column: cusip}]
    relationships:
      - name: HOLDS
        source: raw.holdings
        from_columns: [account_id]
        to_columns: [security_id]
        properties:
          - {name: as_of, column: snapshot_date}
  """
  ddl = compile_graph(*_load(ontology_yaml, binding_yaml))

  # Assert the precise KEY tuple (order: from_columns, to_columns,
  # then additional).
  assert "KEY (account_id, security_id, snapshot_date)" in ddl
  # And that the KEY clause sits before SOURCE KEY per the BigQuery
  # grammar — we can check ordering by line number.
  lines = ddl.splitlines()
  edge_key_line = next(
      i
      for i, line in enumerate(lines)
      if line.strip().startswith("KEY (account_id, security_id,")
  )
  source_key_line = next(
      i for i, line in enumerate(lines) if "SOURCE KEY" in line
  )
  assert edge_key_line < source_key_line


def test_edge_without_declared_keys_falls_back_to_endpoint_columns():
  # When a relationship declares no ``keys`` block, the compiler
  # still emits a ``KEY`` clause — just using the endpoint columns
  # as an implicit "one edge per endpoint pair" identity. Without
  # this fallback the emitted DDL would lack any row identity on
  # the edge, which BigQuery's graph model needs.
  ontology_yaml = """
    ontology: o
    entities:
      - name: A
        keys: {primary: [id]}
        properties: [{name: id, type: string}]
    relationships:
      - name: R
        from: A
        to: A
  """
  binding_yaml = """
    binding: b
    ontology: o
    target: {backend: bigquery, project: p, dataset: d}
    entities:
      - name: A
        source: t
        properties: [{name: id, column: id}]
    relationships:
      - name: R
        source: edges
        from_columns: [a]
        to_columns: [b]
  """
  ddl = compile_graph(*_load(ontology_yaml, binding_yaml))

  # KEY is the endpoint columns, in from-then-to order, matching
  # the binding's ``from_columns: [a]`` and ``to_columns: [b]``.
  assert "KEY (a, b)" in ddl


# --------------------------------------------------------------------- #
# Compile-time rule violations                                           #
# --------------------------------------------------------------------- #


def test_entity_extends_is_rejected():
  # v0 explicitly punts on inheritance lowering. The ontology loader
  # accepts ``extends`` (it's a legal logical concept), but the
  # compiler refuses to emit DDL for it. This test pins the exact
  # error so downstream tooling can match on it.
  ontology_yaml = """
    ontology: o
    entities:
      - name: Parent
        keys: {primary: [id]}
        properties:
          - {name: id, type: string}
      - name: Child
        extends: Parent
        properties: []
  """
  binding_yaml = """
    binding: b
    ontology: o
    target: {backend: bigquery, project: p, dataset: d}
    entities:
      - name: Parent
        source: t1
        properties: [{name: id, column: id}]
  """
  ontology, binding = _load(ontology_yaml, binding_yaml)
  with pytest.raises(ValueError) as exc_info:
    compile_graph(ontology, binding)
  assert str(exc_info.value) == (
      "Entity 'Child' uses 'extends'; v0 compilation does not support "
      "inheritance."
  )


def test_relationship_extends_is_rejected():
  # The mirror of the entity-extends rule. Worth a separate test
  # because the compiler walks entities and relationships in two
  # independent loops — regressing one wouldn't break the other.
  ontology_yaml = """
    ontology: o
    entities:
      - name: A
        keys: {primary: [id]}
        properties: [{name: id, type: string}]
    relationships:
      - name: R1
        from: A
        to: A
      - name: R2
        extends: R1
        from: A
        to: A
  """
  binding_yaml = """
    binding: b
    ontology: o
    target: {backend: bigquery, project: p, dataset: d}
    entities:
      - name: A
        source: t
        properties: [{name: id, column: id}]
  """
  ontology, binding = _load(ontology_yaml, binding_yaml)
  with pytest.raises(ValueError) as exc_info:
    compile_graph(ontology, binding)
  assert str(exc_info.value) == (
      "Relationship 'R2' uses 'extends'; v0 compilation does not support "
      "inheritance."
  )


def test_derived_property_cycle_is_rejected():
  # Two derived properties that reference each other blow the
  # substitution stack if left to recurse. The resolver detects the
  # cycle on entering a property that's already being resolved, and
  # names the property where the cycle closed.
  ontology_yaml = """
    ontology: o
    entities:
      - name: E
        keys: {primary: [id]}
        properties:
          - {name: id, type: string}
          - {name: a, type: string, expr: "b"}
          - {name: b, type: string, expr: "a"}
  """
  binding_yaml = """
    binding: b
    ontology: o
    target: {backend: bigquery, project: p, dataset: d}
    entities:
      - name: E
        source: t
        properties:
          - {name: id, column: id}
  """
  ontology, binding = _load(ontology_yaml, binding_yaml)
  with pytest.raises(ValueError, match="cycle"):
    compile_graph(ontology, binding)


def test_substitution_does_not_rematch_inserted_column_names():
  # Regression: properties ``a`` (bound to column ``x``) and ``x``
  # (bound to column ``y``), with derived ``d: expr: "a"``. A
  # sequential substitution loop would replace ``a`` → ``x``, then
  # the ``x`` pass would fire on the inserted column name, producing
  # ``(y) AS d`` instead of the correct ``(x) AS d``. Single-pass
  # substitution prevents this.
  ontology_yaml = """
    ontology: o
    entities:
      - name: E
        keys: {primary: [id]}
        properties:
          - {name: id, type: string}
          - {name: a, type: string}
          - {name: x, type: string}
          - {name: d, type: string, expr: "a"}
  """
  binding_yaml = """
    binding: b
    ontology: o
    target: {backend: bigquery, project: p, dataset: d}
    entities:
      - name: E
        source: t
        properties:
          - {name: id, column: row_id}
          - {name: a, column: x}
          - {name: x, column: y}
  """
  ddl = compile_graph(*_load(ontology_yaml, binding_yaml))
  assert "(x) AS d" in ddl
  assert "(y) AS d" not in ddl


def test_unresolved_name_in_derived_expression_is_rejected():
  # A derived expression referencing a name that doesn't exist as a
  # property on the same element must fail at compile time rather
  # than leaking an unsubstituted token into emitted DDL.
  ontology_yaml = """
    ontology: o
    entities:
      - name: E
        keys: {primary: [id]}
        properties:
          - {name: id, type: string}
          - {name: d, type: string, expr: "missing_prop || id"}
  """
  binding_yaml = """
    binding: b
    ontology: o
    target: {backend: bigquery, project: p, dataset: d}
    entities:
      - name: E
        source: t
        properties:
          - {name: id, column: row_id}
  """
  ontology, binding = _load(ontology_yaml, binding_yaml)
  with pytest.raises(ValueError, match="missing_prop"):
    compile_graph(ontology, binding)


def test_entities_sharing_a_source_basename_compile_cleanly():
  # Regression guard for the aliasing approach. Two entities binding
  # to tables whose basenames happen to collide (``customers.users``
  # vs ``hr.users``) used to require a compile-time alias-uniqueness
  # check. Now that aliases come from the ontology labels —
  # ``CustomerUser``, ``InternalUser`` — there's nothing to collide
  # with, and the previously-tricky case compiles without incident.
  ontology_yaml = """
    ontology: o
    entities:
      - name: CustomerUser
        keys: {primary: [id]}
        properties: [{name: id, type: string}]
      - name: InternalUser
        keys: {primary: [id]}
        properties: [{name: id, type: string}]
  """
  binding_yaml = """
    binding: b
    ontology: o
    target: {backend: bigquery, project: p, dataset: d}
    entities:
      - name: CustomerUser
        source: customers.users
        properties: [{name: id, column: id}]
      - name: InternalUser
        source: hr.users
        properties: [{name: id, column: id}]
  """
  ddl = compile_graph(*_load(ontology_yaml, binding_yaml))
  assert "customers.users AS CustomerUser" in ddl
  assert "hr.users AS InternalUser" in ddl
