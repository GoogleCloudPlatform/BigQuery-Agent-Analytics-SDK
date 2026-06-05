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

"""Tests for the offline CREATE PROPERTY GRAPH DDL parser (issue #277, PR 1).

Two emitted styles must parse identically into the AST:
  * the compact hand-written codelab form (inline ``LABEL ... PROPERTIES``,
    bare columns, no ``session_id``, edges without ``PROPERTIES``), and
  * the SDK transpiler form (multi-line, ``session_id``/``extracted_at`` in
    ``KEY``/``PROPERTIES``, edges with ``SOURCE``/``DESTINATION`` + props).
"""

from __future__ import annotations

from pathlib import Path
import textwrap

import pytest

from bigquery_ontology import compile_graph
from bigquery_ontology import load_binding_from_string
from bigquery_ontology import load_ontology_from_string
from bigquery_ontology.graph_ddl_parser import GraphDDLParseError
from bigquery_ontology.graph_ddl_parser import parse_property_graph_ddl
from bigquery_ontology.graph_ddl_parser import ParsedProperty

_REPO = Path(__file__).resolve().parents[1]
_CODELAB_DDL = (
    _REPO
    / "examples"
    / "codelab"
    / "periodic_materialization"
    / "property_graph.sql"
)


# --------------------------------------------------------------------------- #
# Codelab (hand-written) form
# --------------------------------------------------------------------------- #


def test_parses_codelab_property_graph_file() -> None:
  graph = parse_property_graph_ddl(_CODELAB_DDL.read_text(encoding="utf-8"))

  assert graph.or_replace is True
  assert graph.name == "agent_decisions_graph"
  assert graph.name_raw == "${PROJECT_ID}.${DATASET}.agent_decisions_graph"

  # Three node tables, parsed in source order.
  assert [n.alias for n in graph.nodes] == [
      "decision_request",
      "decision_option",
      "decision_outcome",
  ]
  req = graph.nodes[0]
  assert req.source == "${PROJECT_ID}.${DATASET}.decision_request"
  assert req.key_columns == ("request_id",)
  assert req.labels == ("DecisionRequest",)
  assert req.properties == (
      ParsedProperty("request_id", "request_id"),
      ParsedProperty("request_text", "request_text"),
      ParsedProperty("requested_at", "requested_at"),
  )

  # Two edge tables; the second one (resulted_in) has no PROPERTIES clause.
  assert [e.alias for e in graph.edges] == ["evaluates_option", "resulted_in"]
  eo = graph.edges[0]
  assert eo.key_columns == ("request_id", "option_id")
  assert eo.source_key_columns == ("request_id",)
  assert eo.source_ref_alias == "decision_request"
  assert eo.source_ref_columns == ("request_id",)
  assert eo.dest_key_columns == ("option_id",)
  assert eo.dest_ref_alias == "decision_option"
  assert eo.labels == ("evaluatesOption",)
  assert eo.properties == ()


# --------------------------------------------------------------------------- #
# SDK transpiler form (session_id, multi-line, col AS name, edge PROPERTIES)
# --------------------------------------------------------------------------- #

_SDK_DDL = """
CREATE OR REPLACE PROPERTY GRAPH `proj.ds.agent_decisions_graph`
  NODE TABLES (
    `proj.ds.decision_request` AS DecisionRequest
      KEY (request_id, session_id)
      LABEL DecisionRequest
      PROPERTIES (
        request_id,
        request_text,
        session_id,
        extracted_at
      ),
    `proj.ds.decision_option` AS DecisionOption
      KEY (option_id, session_id)
      LABEL DecisionOption
      PROPERTIES (
        option_id,
        option_label,
        session_id,
        extracted_at
      )
  )
  EDGE TABLES (
    `proj.ds.evaluates_option` AS evaluatesOption
      KEY (request_id, option_id, session_id)
      SOURCE KEY (request_id, session_id) REFERENCES DecisionRequest (request_id, session_id)
      DESTINATION KEY (option_id, session_id) REFERENCES DecisionOption (option_id, session_id)
      LABEL evaluatesOption
      PROPERTIES (
        extracted_at
      )
  )
"""


def test_parses_sdk_transpiler_form() -> None:
  graph = parse_property_graph_ddl(_SDK_DDL)
  assert graph.name == "agent_decisions_graph"
  assert [n.alias for n in graph.nodes] == ["DecisionRequest", "DecisionOption"]
  req = graph.nodes[0]
  # session_id is captured verbatim from the DDL (stripping it is a later step).
  assert req.key_columns == ("request_id", "session_id")
  assert [p.column for p in req.properties] == [
      "request_id",
      "request_text",
      "session_id",
      "extracted_at",
  ]
  edge = graph.edges[0]
  assert edge.source_key_columns == ("request_id", "session_id")
  assert edge.source_ref_alias == "DecisionRequest"
  assert edge.source_ref_columns == ("request_id", "session_id")
  assert edge.dest_ref_alias == "DecisionOption"
  assert edge.properties == (ParsedProperty("extracted_at", "extracted_at"),)


# --------------------------------------------------------------------------- #
# Syntax features
# --------------------------------------------------------------------------- #


def test_property_alias_with_as() -> None:
  ddl = """
  CREATE PROPERTY GRAPH `g`
    NODE TABLES (
      `t` AS N KEY (id) LABEL N PROPERTIES (phys_col AS logical_name, id)
    )
  """
  graph = parse_property_graph_ddl(ddl)
  assert graph.or_replace is False
  assert graph.nodes[0].properties[0] == ParsedProperty(
      column="phys_col", name="logical_name"
  )


def test_multiple_labels_on_node() -> None:
  ddl = """
  CREATE PROPERTY GRAPH `g`
    NODE TABLES (
      `t` AS N KEY (id) LABEL Child LABEL Parent PROPERTIES (id)
    )
  """
  graph = parse_property_graph_ddl(ddl)
  assert graph.nodes[0].labels == ("Child", "Parent")


def test_comments_and_case_insensitive_keywords() -> None:
  ddl = """
  -- leading comment
  create property graph `g`  /* inline */
    node tables (
      `t` as N key (id) label N properties (id)  -- trailing
    )
  ;
  """
  graph = parse_property_graph_ddl(ddl)
  assert graph.name == "g"
  assert graph.nodes[0].alias == "N"


def test_graph_with_no_edges() -> None:
  ddl = "CREATE PROPERTY GRAPH `g` NODE TABLES (`t` AS N KEY (id) LABEL N)"
  graph = parse_property_graph_ddl(ddl)
  assert graph.nodes[0].labels == ("N",)
  assert graph.nodes[0].properties == ()
  assert graph.edges == ()


# --------------------------------------------------------------------------- #
# Error handling
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("bad", ["", "   ", "-- only a comment\n"])
def test_empty_or_comment_only_raises(bad: str) -> None:
  with pytest.raises(GraphDDLParseError):
    parse_property_graph_ddl(bad)


def test_bare_dotted_reference_parses() -> None:
  # The upstream compiler emits bare/dotted refs (no backticks); they parse.
  ddl = "CREATE PROPERTY GRAPH g NODE TABLES (proj.ds.t AS N KEY (id) LABEL N)"
  graph = parse_property_graph_ddl(ddl)
  assert graph.name == "g"
  assert graph.nodes[0].source == "proj.ds.t"


def test_missing_node_tables_raises() -> None:
  with pytest.raises(GraphDDLParseError):
    parse_property_graph_ddl("CREATE PROPERTY GRAPH `g` EDGE TABLES ()")


def test_node_without_label_raises() -> None:
  ddl = "CREATE PROPERTY GRAPH `g` NODE TABLES (`t` AS N KEY (id))"
  with pytest.raises(GraphDDLParseError, match="no LABEL"):
    parse_property_graph_ddl(ddl)


def test_edge_missing_destination_raises() -> None:
  ddl = """
  CREATE PROPERTY GRAPH `g`
    NODE TABLES (`a` AS A KEY (id) LABEL A, `b` AS B KEY (id) LABEL B)
    EDGE TABLES (
      `e` AS E KEY (x) SOURCE KEY (x) REFERENCES A (id) LABEL E
    )
  """
  with pytest.raises(GraphDDLParseError, match="DESTINATION"):
    parse_property_graph_ddl(ddl)


def test_unterminated_paren_raises() -> None:
  ddl = "CREATE PROPERTY GRAPH `g` NODE TABLES (`t` AS N KEY (id LABEL N"
  with pytest.raises(GraphDDLParseError):
    parse_property_graph_ddl(ddl)


# --------------------------------------------------------------------------- #
# Compatibility with the upstream graph_ddl_compiler output
# --------------------------------------------------------------------------- #

# The exact golden DDL emitted by ``bigquery_ontology.compile_graph`` for the
# finance worked example (see tests/bigquery_ontology/test_graph_ddl_compiler.py
# ::test_compiles_finance_worked_example_to_exact_ddl). Pinned here so the
# parser is provably the inverse of the compiler: bare graph name, bare/dotted
# table refs, renames, and a derived ``(expr) AS name`` property.
_COMPILER_GOLDEN_FINANCE_DDL = """\
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


def test_parses_compiler_golden_finance_ddl() -> None:
  graph = parse_property_graph_ddl(_COMPILER_GOLDEN_FINANCE_DDL)

  assert graph.or_replace is False  # compiler emits CREATE, not OR REPLACE
  assert graph.name == "finance"
  assert [n.alias for n in graph.nodes] == ["Account", "Person", "Security"]

  account = graph.nodes[0]
  assert account.source == "raw.accounts"  # bare/dotted, no backticks
  assert account.properties == (
      ParsedProperty(column="acct_id", name="account_id"),
      ParsedProperty(column="created_ts", name="opened_at"),
  )

  person = graph.nodes[1]
  # The derived property is captured (not dropped, not mis-parsed).
  full_name = person.properties[-1]
  assert full_name.derived is True
  assert full_name.column is None
  assert full_name.name == "full_name"
  assert full_name.expression == "given_name || ' ' || family_name"
  # The four stored properties are still parsed correctly alongside it.
  assert [p.name for p in person.properties] == [
      "person_id",
      "name",
      "first_name",
      "last_name",
      "full_name",
  ]

  holds = graph.edges[0]
  assert holds.source == "raw.holdings"
  assert holds.source_ref_alias == "Account"
  assert holds.source_ref_columns == ("acct_id",)
  assert holds.dest_ref_alias == "Security"
  assert holds.properties == (
      ParsedProperty(column="snapshot_date", name="as_of"),
      ParsedProperty(column="qty", name="quantity"),
  )


def test_derived_expression_with_nested_parens_and_strings() -> None:
  # Commas/parens inside a string literal or function call must not split the
  # property list or close the expression early.
  ddl = (
      "CREATE PROPERTY GRAPH g NODE TABLES ("
      "  t AS N KEY (id) LABEL N PROPERTIES ("
      "    id, (CONCAT(first, ', ', last)) AS label"
      "  )"
      ")"
  )
  graph = parse_property_graph_ddl(ddl)
  label = graph.nodes[0].properties[-1]
  assert label.derived is True
  assert label.name == "label"
  assert label.expression == "CONCAT(first, ', ', last)"


def test_round_trips_live_compile_graph_output() -> None:
  # Strongest guard: compile a real ontology+binding with the upstream
  # compiler, then parse its output. Stays in sync with the compiler
  # automatically (no copied golden string).
  ontology_yaml = """
  ontology: mini
  entities:
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
        - {name: quantity, type: integer}
  """
  binding_yaml = """
  binding: mini_binding
  ontology: mini
  target: {backend: bigquery, project: p, dataset: d}
  entities:
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
        - {name: quantity, column: qty}
  """
  ontology = load_ontology_from_string(textwrap.dedent(ontology_yaml).lstrip())
  binding = load_binding_from_string(
      textwrap.dedent(binding_yaml).lstrip(), ontology=ontology
  )
  ddl = compile_graph(ontology, binding)

  graph = parse_property_graph_ddl(ddl)
  assert graph.name == "mini"
  assert {n.alias for n in graph.nodes} == {"Account", "Security"}
  account = next(n for n in graph.nodes if n.alias == "Account")
  assert account.source == "raw.accounts"
  assert account.properties == (
      ParsedProperty(column="acct_id", name="account_id"),
      ParsedProperty(column="created_ts", name="opened_at"),
  )
  holds = graph.edges[0]
  assert holds.alias == "HOLDS"
  assert holds.source == "raw.holdings"
  assert holds.source_ref_alias == "Account"
  assert holds.dest_ref_alias == "Security"
