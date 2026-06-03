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

r"""Offline parser for a subset of BigQuery ``CREATE PROPERTY GRAPH`` DDL.

This is the inverse of the two emitters in this repo and parses the output of
both:

* :mod:`bigquery_ontology.graph_ddl_compiler` -- the upstream v0 compiler,
  which emits a **bare** graph name, **bare/dotted** table references
  (``raw.accounts AS Account``), property **renames** (``acct_id AS
  account_id``), and **derived expressions** (``(given_name || ' ' ||
  family_name) AS full_name``).
* ``bigquery_agent_analytics.ontology_property_graph`` -- the SDK transpiler,
  which **backtick-quotes** references (``\`proj.ds.t\` AS T``) and injects
  ``session_id`` / ``extracted_at`` into ``KEY`` / ``PROPERTIES``.

The parser produces a faithful, **types-free** abstract syntax tree.

Why types-free? BigQuery property-graph DDL does not declare property *types* --
a ``PROPERTIES`` clause lists bare column names (optionally ``col AS name``, or
``(expr) AS name`` for derived properties). Types live in the underlying table
columns and are recovered separately from ``INFORMATION_SCHEMA.COLUMNS`` (a
later step). Inventing a type here would be lossy and misleading, so the AST
deliberately carries only what the DDL text actually states.

This module performs **no I/O** and has no BigQuery dependency: it is a pure
string -> dataclass transform, which makes it deterministic and unit-testable
offline. It is the first building block of deriving a materialization spec from
the property graph alone, removing the need for hand-written ``ontology.yaml`` /
``binding.yaml`` (see GitHub issue #277).

Supported grammar::

    CREATE [OR REPLACE] PROPERTY GRAPH <graph-ref>
      NODE TABLES (
        <table-ref> AS <alias>
          KEY (<col>, ...)
          LABEL <Label> [LABEL <Label> ...]
          [PROPERTIES (<prop>, ...)]
        [, ... more node tables]
      )
      [EDGE TABLES (
        <table-ref> AS <alias>
          KEY (<col>, ...)
          SOURCE KEY (<col>, ...) REFERENCES <node-alias> (<col>, ...)
          DESTINATION KEY (<col>, ...) REFERENCES <node-alias> (<col>, ...)
          LABEL <Label> [LABEL <Label> ...]
          [PROPERTIES (<prop>, ...)]
        [, ... more edge tables]
      )]
    [;]

where ``<graph-ref>`` and ``<table-ref>`` are either a backtick-quoted body
(preserved verbatim, including ``${VAR}`` placeholders) or a bare/dotted
identifier path (``raw.accounts``), and each ``<prop>`` is one of:

    <column>                  -- stored, name == column
    <column> AS <name>        -- stored, renamed
    (<expression>) AS <name>  -- derived; the raw expression text is captured
                                 for later degradation/diagnostics, since
                                 derived properties are not materializable in
                                 schema-derived mode

``--`` line comments and ``/* */`` block comments are ignored; keywords are
matched case-insensitively. Constructs outside this subset (e.g. multiple
``PROPERTIES`` lists per node) raise :class:`GraphDDLParseError` with the
offending ``line:col`` position rather than being silently mis-parsed.
"""

from __future__ import annotations

import dataclasses
import re
from typing import Optional

__all__ = [
    "GraphDDLParseError",
    "ParsedProperty",
    "ParsedNodeTable",
    "ParsedEdgeTable",
    "ParsedPropertyGraph",
    "parse_property_graph_ddl",
]


class GraphDDLParseError(ValueError):
  """Raised when the DDL is malformed or uses an unsupported construct."""


# --------------------------------------------------------------------------- #
# AST
# --------------------------------------------------------------------------- #


@dataclasses.dataclass(frozen=True)
class ParsedProperty:
  """One entry of a ``PROPERTIES (...)`` list.

  Stored property (``column`` set, ``derived`` False):
    ``column`` is the physical column named in the DDL (left of ``AS``);
    ``name`` is the graph-exposed property name (right of ``AS``, or the same
    as ``column`` when there is no ``AS``).

  Derived property (``derived`` True): ``column`` is ``None`` and
  ``expression`` holds the raw SQL expression text from inside ``(...)``.
  Derived properties are captured for diagnostics but are not materializable in
  schema-derived mode (they have no physical column to read or write).
  """

  column: Optional[str]
  name: str
  expression: Optional[str] = None
  derived: bool = False


@dataclasses.dataclass(frozen=True)
class ParsedNodeTable:
  """A ``NODE TABLES`` entry."""

  alias: str
  source: str  # backtick body or bare/dotted path, e.g. "raw.accounts"
  key_columns: tuple[str, ...]
  labels: tuple[str, ...]
  properties: tuple[ParsedProperty, ...]


@dataclasses.dataclass(frozen=True)
class ParsedEdgeTable:
  """An ``EDGE TABLES`` entry."""

  alias: str
  source: str
  key_columns: tuple[str, ...]
  source_key_columns: tuple[str, ...]
  source_ref_alias: str
  source_ref_columns: tuple[str, ...]
  dest_key_columns: tuple[str, ...]
  dest_ref_alias: str
  dest_ref_columns: tuple[str, ...]
  labels: tuple[str, ...]
  properties: tuple[ParsedProperty, ...]


@dataclasses.dataclass(frozen=True)
class ParsedPropertyGraph:
  """A parsed ``CREATE PROPERTY GRAPH`` statement.

  ``name`` is the trailing identifier of the graph reference (e.g.
  ``agent_decisions_graph`` or ``finance``); ``name_raw`` is the full reference
  as written (e.g. ``${PROJECT_ID}.${DATASET}.agent_decisions_graph``).
  """

  name: str
  name_raw: str
  or_replace: bool
  nodes: tuple[ParsedNodeTable, ...]
  edges: tuple[ParsedEdgeTable, ...]


# --------------------------------------------------------------------------- #
# Tokenizer
# --------------------------------------------------------------------------- #


@dataclasses.dataclass(frozen=True)
class _Token:
  kind: str  # WORD BACKTICK STRING LP RP COMMA SEMI DOT SYMBOL
  value: str  # token text (backtick body has the backticks stripped)
  pos: int  # start offset in the source
  end: int  # end offset in the source (exclusive)


_TOKEN_RE = re.compile(
    r"""
      (?P<ws>\s+)
    | (?P<line_comment>--[^\n]*)
    | (?P<block_comment>/\*.*?\*/)
    | (?P<backtick>`[^`]*`)
    | (?P<string>'(?:[^']|'')*')
    | (?P<lp>\()
    | (?P<rp>\))
    | (?P<comma>,)
    | (?P<semi>;)
    | (?P<dot>\.)
    | (?P<word>[A-Za-z_][A-Za-z0-9_]*)
    | (?P<symbol>[^\s])
    """,
    re.VERBOSE | re.DOTALL,
)

_SKIP = {"ws", "line_comment", "block_comment"}
_KIND = {
    "lp": "LP",
    "rp": "RP",
    "comma": "COMMA",
    "semi": "SEMI",
    "dot": "DOT",
    "string": "STRING",
    "symbol": "SYMBOL",
}


def _line_col(text: str, pos: int) -> str:
  """Render ``pos`` as a 1-based ``line:col`` for error messages."""
  prefix = text[:pos]
  line = prefix.count("\n") + 1
  col = pos - (prefix.rfind("\n") + 1) + 1
  return f"{line}:{col}"


def _tokenize(text: str) -> list[_Token]:
  tokens: list[_Token] = []
  i = 0
  n = len(text)
  while i < n:
    m = _TOKEN_RE.match(text, i)
    if not m or m.end() == i:
      # The catch-all ``symbol`` group matches any single non-space
      # character, so reaching here means an unterminated backtick or
      # block comment swallowed the rest of the input.
      snippet = text[i : i + 20].replace("\n", "\\n")
      raise GraphDDLParseError(
          f"Unable to tokenize at {_line_col(text, i)}: {snippet!r}"
          " (unterminated backtick or comment?)."
      )
    kind = m.lastgroup
    assert kind is not None
    if kind not in _SKIP:
      if kind == "backtick":
        tokens.append(_Token("BACKTICK", m.group()[1:-1].strip(), i, m.end()))
      elif kind == "word":
        tokens.append(_Token("WORD", m.group(), i, m.end()))
      else:
        tokens.append(_Token(_KIND[kind], m.group(), i, m.end()))
    i = m.end()
  return tokens


# --------------------------------------------------------------------------- #
# Parser
# --------------------------------------------------------------------------- #


class _Parser:
  """Recursive-descent parser over the token stream."""

  def __init__(self, tokens: list[_Token], text: str) -> None:
    self._tokens = tokens
    self._text = text
    self._i = 0

  # -- low-level cursor helpers -------------------------------------------- #

  def _peek(self) -> Optional[_Token]:
    return self._tokens[self._i] if self._i < len(self._tokens) else None

  def _advance(self) -> _Token:
    tok = self._peek()
    if tok is None:
      raise GraphDDLParseError("Unexpected end of DDL.")
    self._i += 1
    return tok

  def _at_word(self, word: str) -> bool:
    tok = self._peek()
    return tok is not None and tok.kind == "WORD" and tok.value.upper() == word

  def _at_kind(self, kind: str) -> bool:
    tok = self._peek()
    return tok is not None and tok.kind == kind

  def _error(self, expected: str) -> GraphDDLParseError:
    tok = self._peek()
    if tok is None:
      return GraphDDLParseError(f"Expected {expected} but reached end of DDL.")
    where = _line_col(self._text, tok.pos)
    return GraphDDLParseError(
        f"Expected {expected} at {where}, found {tok.kind} {tok.value!r}."
    )

  def _expect_word(self, word: str) -> _Token:
    if not self._at_word(word):
      raise self._error(f"keyword {word!r}")
    return self._advance()

  def _expect_kind(self, kind: str, label: str) -> _Token:
    if not self._at_kind(kind):
      raise self._error(label)
    return self._advance()

  def _expect_identifier(self, label: str) -> str:
    """A single bare identifier (alias, column, label, referenced node)."""
    tok = self._peek()
    if tok is None or tok.kind != "WORD":
      raise self._error(label)
    return self._advance().value

  def _parse_reference(self, label: str) -> str:
    """A graph/table reference: a backtick body or a bare/dotted path."""
    tok = self._peek()
    if tok is None:
      raise self._error(label)
    if tok.kind == "BACKTICK":
      return self._advance().value
    if tok.kind == "WORD":
      parts = [self._advance().value]
      while self._at_kind("DOT"):
        self._advance()
        parts.append(self._expect_identifier(f"{label} segment"))
      return ".".join(parts)
    raise self._error(label)

  # -- grammar ------------------------------------------------------------- #

  def parse(self) -> ParsedPropertyGraph:
    self._expect_word("CREATE")
    or_replace = False
    if self._at_word("OR"):
      self._advance()
      self._expect_word("REPLACE")
      or_replace = True
    self._expect_word("PROPERTY")
    self._expect_word("GRAPH")
    name_raw = self._parse_reference("graph name")
    name = name_raw.split(".")[-1].strip()

    self._expect_word("NODE")
    self._expect_word("TABLES")
    nodes = tuple(self._parse_entry_list(self._parse_node))
    if not nodes:
      raise GraphDDLParseError("Property graph has no NODE TABLES.")

    edges: tuple[ParsedEdgeTable, ...] = ()
    if self._at_word("EDGE"):
      self._advance()
      self._expect_word("TABLES")
      edges = tuple(self._parse_entry_list(self._parse_edge))

    if self._at_kind("SEMI"):
      self._advance()
    if self._peek() is not None:
      raise self._error("end of DDL")
    return ParsedPropertyGraph(
        name=name,
        name_raw=name_raw,
        or_replace=or_replace,
        nodes=nodes,
        edges=edges,
    )

  def _parse_entry_list(self, parse_entry):
    """Parse ``( entry [, entry]* )`` using ``parse_entry`` per element."""
    self._expect_kind("LP", "'('")
    entries = []
    while True:
      entries.append(parse_entry())
      if self._at_kind("COMMA"):
        self._advance()
        continue
      if self._at_kind("RP"):
        self._advance()
        break
      raise self._error("',' or ')'")
    return entries

  def _parse_table_header(self) -> tuple[str, str]:
    """Parse ``<table-ref> AS <alias>`` -> (source, alias)."""
    source = self._parse_reference("table reference")
    self._expect_word("AS")
    alias = self._expect_identifier("table alias")
    return source, alias

  def _parse_paren_columns(self) -> tuple[str, ...]:
    """Parse ``( col [, col]* )`` -> tuple of column names."""
    self._expect_kind("LP", "'('")
    cols: list[str] = []
    while True:
      cols.append(self._expect_identifier("column name"))
      if self._at_kind("COMMA"):
        self._advance()
        continue
      if self._at_kind("RP"):
        self._advance()
        break
      raise self._error("',' or ')'")
    return tuple(cols)

  def _parse_derived_expression(self) -> str:
    """Capture the raw text inside a balanced ``( ... )`` expression.

    String literals are single tokens, so commas and parentheses inside a
    quoted string do not disturb the paren-depth count.
    """
    open_tok = self._expect_kind("LP", "'('")
    depth = 1
    close_tok = open_tok
    while depth > 0:
      tok = self._peek()
      if tok is None:
        raise GraphDDLParseError(
            "Unterminated derived property expression: missing ')'."
        )
      self._advance()
      if tok.kind == "LP":
        depth += 1
      elif tok.kind == "RP":
        depth -= 1
      close_tok = tok
    raw = self._text[open_tok.end : close_tok.pos].strip()
    if not raw:
      raise GraphDDLParseError("Empty derived property expression '()'.")
    return raw

  def _parse_property(self) -> ParsedProperty:
    if self._at_kind("LP"):
      expression = self._parse_derived_expression()
      self._expect_word("AS")
      name = self._expect_identifier("derived property name")
      return ParsedProperty(
          column=None, name=name, expression=expression, derived=True
      )
    column = self._parse_reference("property column")
    name = column
    if self._at_word("AS"):
      self._advance()
      name = self._expect_identifier("property name")
    return ParsedProperty(column=column, name=name)

  def _parse_paren_properties(self) -> tuple[ParsedProperty, ...]:
    """Parse ``( prop [, prop]* )`` -> tuple of properties."""
    self._expect_kind("LP", "'('")
    props: list[ParsedProperty] = []
    while True:
      props.append(self._parse_property())
      if self._at_kind("COMMA"):
        self._advance()
        continue
      if self._at_kind("RP"):
        self._advance()
        break
      raise self._error("',' or ')'")
    return tuple(props)

  def _parse_node(self) -> ParsedNodeTable:
    source, alias = self._parse_table_header()
    self._expect_word("KEY")
    key_columns = self._parse_paren_columns()
    labels: list[str] = []
    properties: tuple[ParsedProperty, ...] = ()
    seen_properties = False
    while True:
      if self._at_word("LABEL"):
        self._advance()
        labels.append(self._expect_identifier("label name"))
      elif self._at_word("PROPERTIES"):
        if seen_properties:
          raise self._error("single PROPERTIES list per node")
        self._advance()
        properties = self._parse_paren_properties()
        seen_properties = True
      else:
        break
    if not labels:
      raise GraphDDLParseError(f"Node table {alias!r} has no LABEL clause.")
    return ParsedNodeTable(
        alias=alias,
        source=source,
        key_columns=key_columns,
        labels=tuple(labels),
        properties=properties,
    )

  def _parse_edge(self) -> ParsedEdgeTable:
    source, alias = self._parse_table_header()
    self._expect_word("KEY")
    key_columns = self._parse_paren_columns()

    source_key: Optional[tuple[str, ...]] = None
    source_ref_alias: Optional[str] = None
    source_ref_cols: tuple[str, ...] = ()
    dest_key: Optional[tuple[str, ...]] = None
    dest_ref_alias: Optional[str] = None
    dest_ref_cols: tuple[str, ...] = ()
    labels: list[str] = []
    properties: tuple[ParsedProperty, ...] = ()
    seen_properties = False

    while True:
      if self._at_word("SOURCE"):
        self._advance()
        self._expect_word("KEY")
        source_key = self._parse_paren_columns()
        self._expect_word("REFERENCES")
        source_ref_alias = self._expect_identifier("referenced node alias")
        source_ref_cols = self._parse_paren_columns()
      elif self._at_word("DESTINATION"):
        self._advance()
        self._expect_word("KEY")
        dest_key = self._parse_paren_columns()
        self._expect_word("REFERENCES")
        dest_ref_alias = self._expect_identifier("referenced node alias")
        dest_ref_cols = self._parse_paren_columns()
      elif self._at_word("LABEL"):
        self._advance()
        labels.append(self._expect_identifier("label name"))
      elif self._at_word("PROPERTIES"):
        if seen_properties:
          raise self._error("single PROPERTIES list per edge")
        self._advance()
        properties = self._parse_paren_properties()
        seen_properties = True
      else:
        break

    if source_key is None or source_ref_alias is None:
      raise GraphDDLParseError(
          f"Edge table {alias!r} is missing a SOURCE KEY ... REFERENCES clause."
      )
    if dest_key is None or dest_ref_alias is None:
      raise GraphDDLParseError(
          f"Edge table {alias!r} is missing a DESTINATION KEY ... REFERENCES"
          " clause."
      )
    if not labels:
      raise GraphDDLParseError(f"Edge table {alias!r} has no LABEL clause.")

    return ParsedEdgeTable(
        alias=alias,
        source=source,
        key_columns=key_columns,
        source_key_columns=source_key,
        source_ref_alias=source_ref_alias,
        source_ref_columns=source_ref_cols,
        dest_key_columns=dest_key,
        dest_ref_alias=dest_ref_alias,
        dest_ref_columns=dest_ref_cols,
        labels=tuple(labels),
        properties=properties,
    )


def parse_property_graph_ddl(ddl: str) -> ParsedPropertyGraph:
  """Parse a single ``CREATE PROPERTY GRAPH`` statement into an AST.

  Args:
    ddl: The DDL text. May contain ``--``/``/* */`` comments and (within
      backtick references) ``${VAR}`` placeholders, both of which are preserved
      or ignored as documented in the module docstring.

  Returns:
    A :class:`ParsedPropertyGraph` describing the nodes and edges exactly as the
    DDL states them (no property types -- those are recovered separately).

  Raises:
    GraphDDLParseError: If the DDL is empty, malformed, or uses a construct
      outside the supported subset.
  """
  if ddl is None or not ddl.strip():
    raise GraphDDLParseError("Empty DDL.")
  tokens = _tokenize(ddl)
  if not tokens:
    raise GraphDDLParseError("DDL contains no statements (only comments?).")
  return _Parser(tokens, ddl).parse()
