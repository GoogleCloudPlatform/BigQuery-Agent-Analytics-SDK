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

"""In-memory intermediate models produced by the compiler's resolution stage.

A ``ResolvedGraph`` sits between the validated-but-abstract
``Ontology`` + ``Binding`` pair and the string-typed DDL emitted for a
specific backend. It collapses two concerns:

  - **Cross-object lookup.** An entity binding knows its source table
    and columns; a relationship binding knows its endpoint *entity
    names*. To emit DDL we need the endpoint's *node table alias and
    key columns* — a resolved graph pre-computes that wiring so the
    emitter can walk one tree and produce text.

  - **Derived-expression substitution.** Ontology-level derived
    properties carry an ``expr:`` in terms of logical property names.
    DDL has to reference physical column names. Resolution substitutes
    each property-name token with the corresponding column (recursing
    through chains of derived properties), so the emitter sees an
    already-SQL-ready expression string.

Everything here is a frozen dataclass. The intermediate is produced,
consumed, and thrown away in a single compile call — no persistence,
no mutation, no validation rules. The loaders did validation; the
compiler did resolution; the emitter just types out text.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ResolvedProperty:
  """One property in the form the emitter actually writes to DDL.

  ``name`` is the logical (ontology-declared) property name that
  becomes the SQL alias after ``AS``. ``sql`` is what sits on the
  left of the ``AS``:

    - For a bound (non-derived) property, ``sql`` is the physical
      column name as declared in the binding.
    - For a derived property, ``sql`` is the ``expr:`` with every
      property-name token recursively substituted to physical column
      names — the emitter wraps it in parentheses so SQL precedence is
      safe regardless of what operators the user wrote.

  ``derived`` preserves which of the two paths produced this property
  so the emitter can decide whether to add parentheses and to skip the
  ``AS`` rename when the logical and physical names happen to match.

  ``type`` is the logical type (from the ontology). It is not emitted
  in ``CREATE PROPERTY GRAPH`` — property types are inferred from the
  underlying table columns — but the compiler keeps it around for
  target-compat checks (relevant on Spanner, no-op on BigQuery).
  """

  name: str
  type: str
  sql: str
  derived: bool


@dataclass(frozen=True)
class ResolvedLabelAndProperties:
  """One label on a node or edge, paired with its property list.

  Mirrors the GCP ``CREATE PROPERTY GRAPH`` grammar's
  ``LabelAndProperties`` production. A node or edge table may carry
  multiple labels in general (``LabelAndPropertiesList``), each with
  its own subset of physical columns surfaced as properties — the
  pairing is what makes multi-label schemas unambiguous.

  v0 populates exactly one of these per table (each entity or
  relationship contributes one label whose property list is the full
  set bound to that element), but the nested type reflects the
  grammar's coupling regardless. Future multi-label work becomes a
  data-model population change rather than an emitter rewrite — the
  emitter already iterates the tuple and renders each bundle.
  """

  label: str
  properties: tuple[ResolvedProperty, ...]


@dataclass(frozen=True)
class ResolvedNodeTable:
  """One ``NODE TABLE`` entry.

  ``alias`` is the identifier used after ``AS`` in the emitted DDL
  and again in any edge table's ``REFERENCES`` clause. We use the
  ontology entity name verbatim (``Account``, ``Person``) rather than
  deriving something from the physical source name, so the emitted
  DDL reads by logical type instead of leaking table basenames. The
  ontology loader already guarantees entity names are unique within
  the ontology *and* disjoint from relationship names, so aliases
  are unique by construction — no runtime check needed.

  ``key_columns`` are the *physical* columns backing the entity's
  primary-key properties, in declaration order. They land both in
  this node's own ``KEY (...)`` clause and in any edge table's
  ``REFERENCES alias (...)`` for this node.

  ``label_and_properties`` is a tuple of ``ResolvedLabelAndProperties``
  bundles. Exactly one in v0 (whose ``label`` equals the entity name,
  whose ``properties`` is the full declared-order property list for
  the entity). The tuple shape is what the grammar calls for in
  general, so carrying it here — even at length one — keeps the model
  honest and leaves the door open for inheritance-lowering work that
  emits several labels per table.
  """

  alias: str
  source: str
  key_columns: tuple[str, ...]
  label_and_properties: tuple[ResolvedLabelAndProperties, ...]


@dataclass(frozen=True)
class ResolvedEdgeTable:
  """One ``EDGE TABLE`` entry.

  ``from_columns`` / ``to_columns`` are columns on the *edge* table's
  own source (e.g. ``raw.holdings``). ``from_node_alias`` /
  ``to_node_alias`` name the NODE TABLE aliases that those keys
  reference, and ``from_node_key_columns`` / ``to_node_key_columns``
  are the physical key columns on those node tables. Denormalizing
  this onto the edge saves the emitter from a per-edge lookup into
  the node-table map — emission becomes a straight-line render.

  ``key_columns`` holds the edge's own primary-key columns. It is
  always non-empty — every edge in the emitted DDL gets a ``KEY``
  clause, because BigQuery needs a row-level identity on edges even
  when the ontology didn't spell one out. The resolver picks the
  columns by these rules:

    - ``keys.primary`` declared → exactly the bound physical columns
      for those properties (standalone row identity, e.g. ``TRANSFER``
      keyed by ``transaction_id``).
    - ``keys.additional`` declared → endpoint columns followed by
      the bound additional-key columns; together they form the
      row's unique tuple (e.g. ``HOLDS`` keyed by ``(account_id,
      security_id, as_of)``).
    - No keys declared → just the endpoint columns, expressing "one
      edge per endpoint pair" as a safe default. Authors who need
      actual multi-edges should declare ``keys.additional`` with a
      discriminator property.

  ``label_and_properties`` — same shape and rationale as
  ``ResolvedNodeTable.label_and_properties``: always length-1 in v0,
  tuple because the GCP grammar allows a list and modeling it here
  means future multi-label work is a resolver change, not a schema
  one.
  """

  alias: str
  source: str
  from_columns: tuple[str, ...]
  from_node_alias: str
  from_node_key_columns: tuple[str, ...]
  to_columns: tuple[str, ...]
  to_node_alias: str
  to_node_key_columns: tuple[str, ...]
  key_columns: tuple[str, ...]
  label_and_properties: tuple[ResolvedLabelAndProperties, ...]


@dataclass(frozen=True)
class ResolvedGraph:
  """One ``CREATE PROPERTY GRAPH`` statement, ready to render.

  ``name`` is copied from the ontology's ``ontology:`` field — the
  binding does not re-declare a graph name, so there's no way for the
  two to disagree.

  ``node_tables`` and ``edge_tables`` are already alphabetized by
  alias at resolution time so the emitter can rely on the iteration
  order without a re-sort. This matters for the determinism guarantee
  (the same inputs must produce byte-identical DDL) and also makes
  diffs of emitted output readable when an ontology grows.
  """

  name: str
  node_tables: tuple[ResolvedNodeTable, ...]
  edge_tables: tuple[ResolvedEdgeTable, ...]
