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

"""Compile an ontology + binding into BigQuery ``CREATE PROPERTY GRAPH`` DDL.

The compiler is a single pass split into two stages:

  1. **Resolve.** Cross-reference the ontology and the binding to
     produce an in-memory ``ResolvedGraph``. This is where derived
     expressions get rewritten to reference physical columns, edge
     endpoints pick up their node-table aliases and key columns, and
     the list is sorted alphabetically for deterministic output.
  2. **Emit.** Walk the ``ResolvedGraph`` and produce the DDL text.
     The emitter is intentionally mechanical — all interesting
     decisions happen in stage 1 so that stage 2 is just rendering.

The compiler trusts that the loaders already ran: it does *not*
re-validate shape, name uniqueness, or property coverage. It only
enforces the rules that are specifically compilation-level and that
the loaders don't know about:

  - No ``extends`` anywhere in the ontology (v0 rejects hierarchies;
    inheritance lowering is a separate, future design).
  - No cycles among derived properties.
  - Node-table aliases are unique.

Determinism contract: same ``Ontology`` + ``Binding`` → byte-identical
output. Node and edge tables are emitted in alphabetical order by
label; properties within each table are emitted in ontology
declaration order.

Known limitation (documented, not fixed in v0): derived-expression
substitution uses word-boundary regex rather than a SQL lexer. This
is sufficient for expressions of the form
``first_name || ' ' || last_name`` but will misfire if a property
name happens to appear as a substring inside a single-quoted string
literal (e.g. ``'name'``). Upgrading to a real SQL lexer is a
follow-up when a real user expression exercises the gap.
"""

from __future__ import annotations

import re

from ._fingerprint import compile_fingerprint
from ._fingerprint import compile_id
from ._fingerprint import fingerprint_model
from .binding_models import Binding
from .binding_models import EntityBinding
from .binding_models import PropertyBinding
from .binding_models import RelationshipBinding
from .concept_index import build_rows
from .concept_index import ConceptIndexRow
from .graph_ddl_models import ResolvedEdgeTable
from .graph_ddl_models import ResolvedGraph
from .graph_ddl_models import ResolvedLabelAndProperties
from .graph_ddl_models import ResolvedNodeTable
from .graph_ddl_models import ResolvedProperty
from .ontology_models import Entity
from .ontology_models import Ontology
from .ontology_models import Property
from .ontology_models import Relationship

# --------------------------------------------------------------------- #
# Public entry point                                                    #
# --------------------------------------------------------------------- #


def compile_graph(ontology: Ontology, binding: Binding) -> str:
  """Compile an ontology + binding pair into BigQuery DDL.

  Returns a single ``CREATE PROPERTY GRAPH`` statement terminated by a
  semicolon and a trailing newline. The output is deterministic:
  re-running with the same inputs produces byte-identical text, and
  unrelated ontology changes (e.g. adding a new entity) only show up
  as an additive diff in the emitted DDL.

  Raises:
      ValueError: Any compile-time rule is violated (``extends`` used,
          alias collision, derived-expression cycle).
  """
  graph = _resolve(ontology, binding)
  return _emit_bigquery(graph)


# --------------------------------------------------------------------- #
# Resolution                                                             #
# --------------------------------------------------------------------- #


def _resolve(ontology: Ontology, binding: Binding) -> ResolvedGraph:
  """Produce a ``ResolvedGraph`` from validated ontology+binding inputs."""
  _reject_extends(ontology)

  # Index ontology and binding by name so the per-entity / per-edge
  # resolution steps are straight lookups rather than linear scans.
  entity_map: dict[str, Entity] = {e.name: e for e in ontology.entities}
  rel_map: dict[str, Relationship] = {r.name: r for r in ontology.relationships}
  entity_binding_map: dict[str, EntityBinding] = {
      eb.name: eb for eb in binding.entities
  }

  # Build node tables first. Edge tables reference node tables by
  # alias/key-columns, so node tables have to exist before we can
  # resolve edges.
  node_tables = tuple(
      sorted(
          (
              _resolve_node_table(entity_map[eb.name], eb)
              for eb in binding.entities
          ),
          key=lambda nt: nt.alias,
      )
  )

  # Map alias -> ResolvedNodeTable for edge-resolution lookups. Aliases equal
  # entity names (see ``_resolve_node_table``), so a relationship's
  # ``from_`` / ``to`` values look up directly.
  node_by_alias: dict[str, ResolvedNodeTable] = {
      nt.alias: nt for nt in node_tables
  }

  edge_tables = tuple(
      sorted(
          (
              _resolve_edge_table(rel_map[rb.name], rb, node_by_alias)
              for rb in binding.relationships
          ),
          key=lambda et: et.alias,
      )
  )

  return ResolvedGraph(
      name=ontology.ontology,
      node_tables=node_tables,
      edge_tables=edge_tables,
  )


def _reject_extends(ontology: Ontology) -> None:
  """Compile-time rule: v0 does not support inheritance.

  The ontology loader *accepts* ``extends``, because the ontology
  itself is backend-neutral and inheritance is a legal ontology-level
  concept. Compilation is the layer that has to choose a lowering
  strategy for inheritance (fan-out, union-view, label-referenced
  edges), and v0 explicitly punts on that. Rejecting here — rather
  than earlier — keeps the ontology and binding models reusable by
  future compilers that *do* lower inheritance.
  """
  for entity in ontology.entities:
    if entity.extends is not None:
      raise ValueError(
          f"Entity {entity.name!r} uses 'extends'; v0 compilation "
          "does not support inheritance."
      )
  for rel in ontology.relationships:
    if rel.extends is not None:
      raise ValueError(
          f"Relationship {rel.name!r} uses 'extends'; v0 compilation "
          "does not support inheritance."
      )


def _resolve_node_table(entity: Entity, eb: EntityBinding) -> ResolvedNodeTable:
  """Build one ``ResolvedNodeTable`` from an entity + its binding.

  The binding loader already guaranteed full property coverage, so we
  can walk the entity's declared properties in order and look each
  one up in the binding. Derived properties are substituted here;
  stored properties are copied through with their bound column.
  """
  column_by_property = {pb.name: pb.column for pb in eb.properties}

  # The primary-key property names are translated to physical columns
  # via the same binding map. The loader guarantees every key property
  # is bound, so this is an infallible lookup.
  #
  # ``entity.keys`` is guaranteed non-None by the ontology loader for
  # flat ontologies (v0 rejects ``extends``, so no inherited keys to
  # walk). Belt-and-braces anyway — a defensive check here would
  # shadow real bugs, so we let it blow up loudly if something slips.
  assert entity.keys is not None and entity.keys.primary is not None
  key_columns = tuple(column_by_property[p] for p in entity.keys.primary)

  properties = _resolve_properties(
      declared=entity.properties,
      column_by_property=column_by_property,
      owner=f"entity {entity.name!r}",
  )

  return ResolvedNodeTable(
      # Alias matches the entity name so the emitted DDL reads by
      # logical type (``raw.accounts AS Account``) instead of leaking
      # the physical table basename. Entity names are unique within
      # the ontology and disjoint from relationship names (enforced
      # by the ontology loader), so aliases are collision-free by
      # construction — no runtime uniqueness check needed.
      alias=entity.name,
      source=eb.source,
      key_columns=key_columns,
      # v0 emits one label per node — the entity name — bundled
      # with the entity's full (non-derived + derived) property set.
      # The tuple shape reserves the door for future multi-label
      # work; see ``ResolvedLabelAndProperties`` docstring.
      label_and_properties=(
          ResolvedLabelAndProperties(label=entity.name, properties=properties),
      ),
  )


def _resolve_edge_table(
    rel: Relationship,
    rb: RelationshipBinding,
    node_by_alias: dict[str, ResolvedNodeTable],
) -> ResolvedEdgeTable:
  """Build one ``ResolvedEdgeTable`` from a relationship + its binding.

  The relationship's ``from_`` / ``to`` name the endpoint *entities*;
  we turn those into the endpoint *node tables'* aliases and key
  columns so the emitter can render ``REFERENCES alias (cols)``
  without further lookup.
  """
  from_node = node_by_alias[rel.from_]
  to_node = node_by_alias[rel.to]

  column_by_property = {pb.name: pb.column for pb in rb.properties}
  properties = _resolve_properties(
      declared=rel.properties,
      column_by_property=column_by_property,
      owner=f"relationship {rel.name!r}",
  )

  return ResolvedEdgeTable(
      # Same rationale as ResolvedNodeTable.alias: use the relationship name
      # so the DDL reads by logical name rather than physical table
      # basename.
      alias=rel.name,
      source=rb.source,
      from_columns=tuple(rb.from_columns),
      from_node_alias=from_node.alias,
      from_node_key_columns=from_node.key_columns,
      to_columns=tuple(rb.to_columns),
      to_node_alias=to_node.alias,
      to_node_key_columns=to_node.key_columns,
      key_columns=_resolve_edge_key_columns(rel, rb, column_by_property),
      # v0 emits one label per edge — the relationship name — bundled
      # with the relationship's full property set.
      label_and_properties=(
          ResolvedLabelAndProperties(label=rel.name, properties=properties),
      ),
  )


def _resolve_edge_key_columns(
    rel: Relationship,
    rb: RelationshipBinding,
    column_by_property: dict[str, str],
) -> tuple[str, ...]:
  """Compute the edge's ``KEY (...)`` columns.

  Every edge gets a KEY clause in the emitted DDL — BigQuery's graph
  model needs a row-level identity on edges even when the ontology
  does not spell one out. We pick the identity from the ontology's
  declared keys when available, otherwise fall back to the endpoint
  columns.

  Three cases, mutually exclusive:

    - **``primary`` declared.** The relationship identifies a row
      standalone (e.g. ``TRANSFER`` with primary
      ``[transaction_id]``). KEY = bound primary columns.
    - **``additional`` declared.** The relationship identifies a row
      by uniqueness *within* an endpoint pair (e.g. ``HOLDS`` with
      additional ``[as_of]`` — for a given (account, security), no
      two rows share ``as_of``). BigQuery's KEY wants a globally-
      unique tuple, so we prefix the endpoint columns:
      KEY = from_columns + to_columns + bound additional columns.
    - **No keys declared.** The ontology's Keys docstring reads this
      as "multi-edges permitted," but DDL still needs *some* identity.
      The least-surprising default is "one edge per endpoint pair" —
      KEY = from_columns + to_columns. Authors who actually want
      multi-edges should declare ``keys.additional`` with a
      discriminator property (the ``HOLDS`` case above).

  The mutual exclusion between ``primary`` and ``additional`` is
  enforced by the ontology loader, so at most one of the first two
  branches can fire.
  """
  from_to_columns = tuple(rb.from_columns) + tuple(rb.to_columns)
  if rel.keys is None:
    return from_to_columns
  if rel.keys.primary:
    return tuple(column_by_property[p] for p in rel.keys.primary)
  if rel.keys.additional:
    return from_to_columns + tuple(
        column_by_property[p] for p in rel.keys.additional
    )
  return from_to_columns


def _resolve_properties(
    *,
    declared: list[Property],
    column_by_property: dict[str, str],
    owner: str,
) -> tuple[ResolvedProperty, ...]:
  """Walk declared properties in order, resolving each.

  Derived properties are substituted on demand; each call caches its
  result in ``resolved_sql`` so a derived property referenced by
  multiple other properties (or just this emission) only has its
  substitution computed once. The same cache is also how we detect
  cycles — an in-progress property is tracked in ``resolving`` and a
  re-entry is raised as a cycle rather than blowing the stack.
  """
  # A property-name → Property map so the derived-substitution pass
  # can recurse by name without another linear scan.
  property_map = {p.name: p for p in declared}
  resolved_sql: dict[str, str] = {}
  resolving: set[str] = set()

  out: list[ResolvedProperty] = []
  for prop in declared:
    sql = _resolve_sql(
        prop,
        property_map=property_map,
        column_by_property=column_by_property,
        resolved_sql=resolved_sql,
        resolving=resolving,
        owner=owner,
    )
    out.append(
        ResolvedProperty(
            name=prop.name,
            type=prop.type.value,
            sql=sql,
            derived=prop.expr is not None,
        )
    )
  return tuple(out)


_IDENTIFIER_RE = re.compile(r"\b([a-zA-Z_]\w*)\b")


def _reject_unresolved_names(
    expr: str,
    self_name: str,
    property_map: dict[str, Property],
    owner: str,
) -> None:
  """Raise if ``expr`` contains identifiers not in the property map."""
  unknown = {
      tok
      for tok in _IDENTIFIER_RE.findall(expr)
      if tok != self_name and tok not in property_map
  }
  if unknown:
    names = ", ".join(sorted(unknown))
    raise ValueError(
        f"Derived property {self_name!r} on {owner} references "
        f"unknown name(s): {names}. Every name in a derived "
        f"expression must be a property on the same element."
    )


def _resolve_sql(
    prop: Property,
    *,
    property_map: dict[str, Property],
    column_by_property: dict[str, str],
    resolved_sql: dict[str, str],
    resolving: set[str],
    owner: str,
) -> str:
  """Resolve one property to its SQL string.

  Stored properties return their bound column verbatim. Derived
  properties recursively resolve each property-name reference in
  their ``expr:``, splice the results back in, and cache the final
  string. Cycle detection is scoped to this entity/relationship —
  properties on other elements cannot be referenced, so a per-call
  ``resolving`` set is sufficient.
  """
  if prop.name in resolved_sql:
    return resolved_sql[prop.name]
  if prop.name in resolving:
    raise ValueError(f"Derived property cycle on {owner} at {prop.name!r}.")

  if prop.expr is None:
    # Stored property — the bound column is the SQL. (The loader has
    # already checked that every non-derived property has a binding,
    # so this lookup cannot miss.)
    result = column_by_property[prop.name]
    resolved_sql[prop.name] = result
    return result

  resolving.add(prop.name)
  try:
    _reject_unresolved_names(prop.expr, prop.name, property_map, owner)

    # Build a single alternation pattern for all property names that
    # appear in the expression, then substitute in one pass so that
    # a column name introduced by one replacement can never be
    # re-matched as a different property name.
    names_in_expr = [
        name
        for name in sorted(property_map.keys(), key=len, reverse=True)
        if name != prop.name and re.search(rf"\b{re.escape(name)}\b", prop.expr)
    ]

    if names_in_expr:
      combined = re.compile(
          "|".join(rf"\b{re.escape(n)}\b" for n in names_in_expr)
      )

      def _replacer(match: re.Match[str]) -> str:
        name = match.group(0)
        nested = _resolve_sql(
            property_map[name],
            property_map=property_map,
            column_by_property=column_by_property,
            resolved_sql=resolved_sql,
            resolving=resolving,
            owner=owner,
        )
        if property_map[name].expr is not None:
          nested = f"({nested})"
        return nested

      result = combined.sub(_replacer, prop.expr)
    else:
      result = prop.expr
  finally:
    resolving.discard(prop.name)

  resolved_sql[prop.name] = result
  return result


# --------------------------------------------------------------------- #
# Emission (BigQuery)                                                    #
# --------------------------------------------------------------------- #
#
# Formatting choices, all driven by the spec's worked example:
#
#   - Two-space indent per level.
#   - ``LABEL <name> PROPERTIES (...)`` is emitted as one bundled
#     clause per the GCP grammar's ``LabelAndProperties`` production
#     — not split across lines, even though today each element only
#     carries one label. Keeping them bundled reserves the shape for
#     future multi-label support, where each label has its own
#     property list and the visual grouping stops being optional.
#   - Single-line ``LABEL <name> PROPERTIES (...)`` when the
#     rendered line fits inside ``_INLINE_PROPERTIES_MAX_WIDTH``;
#     otherwise the property list breaks across lines (one property
#     per line) with the closing paren on its own line. The
#     ``LABEL <name> PROPERTIES (`` opening stays on the first line
#     so the label-property association remains visually intact.
#   - Comma-and-newline separator between node-table entries and
#     between edge-table entries; final entry has no trailing comma.
#   - Statement terminator ``;`` on its own trailing line.
#
# These choices give a diff-friendly, grep-friendly shape without
# requiring a full pretty-printer.

# The width threshold at which property lists flip from inline to
# multi-line. 80 columns is enough room for the spec's two-property
# entries to fit inline while still breaking Person's five-property
# entry onto multiple lines. Conservative values below the 120-ish
# common modern width keep output readable in split views and code
# review tools.
_INLINE_PROPERTIES_MAX_WIDTH = 80


def _emit_bigquery(graph: ResolvedGraph) -> str:
  """Render a ``ResolvedGraph`` as BigQuery DDL text."""
  lines: list[str] = []
  lines.append(f"CREATE PROPERTY GRAPH {graph.name}")

  if graph.node_tables:
    lines.append("  NODE TABLES (")
    lines.extend(_emit_node_table_entries(graph.node_tables))
    # Close the NODE TABLES list. EDGE TABLES, when present,
    # continues on the next line at the same indent; otherwise the
    # statement terminator gets tacked onto this line at the end of
    # emission.
    lines.append("  )")

  if graph.edge_tables:
    lines.append("  EDGE TABLES (")
    lines.extend(_emit_edge_table_entries(graph.edge_tables))
    lines.append("  )")

  # The statement terminator goes at the end of the last already-
  # emitted line rather than on a line of its own so the output reads
  # as one coherent SQL statement.
  lines[-1] = lines[-1] + ";"
  return "\n".join(lines) + "\n"


def _emit_node_table_entries(
    node_tables: tuple[ResolvedNodeTable, ...],
) -> list[str]:
  """Render the NODE TABLES list body (entries, no surrounding parens)."""
  entries: list[list[str]] = [_emit_node_table(nt) for nt in node_tables]
  return _join_entries(entries)


def _emit_node_table(nt: ResolvedNodeTable) -> list[str]:
  """One ``NODE TABLE`` entry as a list of lines (no trailing comma)."""
  # Example layout:
  #     raw.accounts AS Account
  #       KEY (acct_id)
  #       LABEL Account PROPERTIES (acct_id AS account_id, created_ts AS opened_at)
  lines = [f"    {nt.source} AS {nt.alias}"]
  lines.append(f"      KEY ({', '.join(nt.key_columns)})")
  # One ``LABEL X PROPERTIES(...)`` clause per bundle. v0 always has
  # exactly one bundle, but iterating the tuple is already the
  # multi-label shape and costs nothing today.
  for lp in nt.label_and_properties:
    lines.extend(_emit_label_clause(lp.label, lp.properties, indent="      "))
  return lines


def _emit_edge_table_entries(
    edge_tables: tuple[ResolvedEdgeTable, ...],
) -> list[str]:
  """Render the EDGE TABLES list body (entries, no surrounding parens)."""
  entries: list[list[str]] = [_emit_edge_table(et) for et in edge_tables]
  return _join_entries(entries)


def _emit_edge_table(et: ResolvedEdgeTable) -> list[str]:
  """One ``EDGE TABLE`` entry as a list of lines (no trailing comma)."""
  # Example layout:
  #     raw.holdings AS HOLDS
  #       KEY (account_id, security_id, snapshot_date)
  #       SOURCE KEY (account_id) REFERENCES Account (acct_id)
  #       DESTINATION KEY (security_id) REFERENCES Security (cusip)
  #       LABEL HOLDS PROPERTIES (snapshot_date AS as_of, qty AS quantity)
  #
  # The ``KEY`` clause is always emitted. The resolver computes a
  # sensible default (endpoint columns) when the ontology did not
  # spell out any keys, so by the time we get here ``key_columns``
  # is guaranteed non-empty.
  lines = [f"    {et.source} AS {et.alias}"]
  lines.append(f"      KEY ({', '.join(et.key_columns)})")
  lines.append(
      f"      SOURCE KEY ({', '.join(et.from_columns)})"
      f" REFERENCES {et.from_node_alias}"
      f" ({', '.join(et.from_node_key_columns)})"
  )
  lines.append(
      f"      DESTINATION KEY ({', '.join(et.to_columns)})"
      f" REFERENCES {et.to_node_alias}"
      f" ({', '.join(et.to_node_key_columns)})"
  )
  # Same multi-label iteration as node tables; v0 length is always 1.
  for lp in et.label_and_properties:
    lines.extend(_emit_label_clause(lp.label, lp.properties, indent="      "))
  return lines


def _emit_label_clause(
    label: str,
    properties: tuple[ResolvedProperty, ...],
    *,
    indent: str,
) -> list[str]:
  """Render a bundled ``LABEL <name> PROPERTIES (...)`` clause.

  The GCP grammar treats label and its property list as one unit
  (``LabelAndProperties``), so we emit them together. Produces a
  single line when the rendered line fits comfortably; otherwise
  wraps the property list across lines while keeping the
  ``LABEL <name> PROPERTIES (`` opener intact on the first line —
  preserving the visual association between the label and the
  properties it governs.

  Multi-line shape:

      LABEL X PROPERTIES (
        prop_1,
        prop_2
      )
  """
  property_strs = [_emit_property(p) for p in properties]
  inline = f"{indent}LABEL {label} PROPERTIES ({', '.join(property_strs)})"
  if len(inline) <= _INLINE_PROPERTIES_MAX_WIDTH:
    return [inline]

  property_indent = indent + "  "
  lines = [f"{indent}LABEL {label} PROPERTIES ("]
  for i, s in enumerate(property_strs):
    suffix = "," if i < len(property_strs) - 1 else ""
    lines.append(f"{property_indent}{s}{suffix}")
  lines.append(f"{indent})")
  return lines


def _emit_property(prop: ResolvedProperty) -> str:
  """Render one property as it appears inside a PROPERTIES(...) list.

  Three shapes:

    - Stored property whose column happens to match the logical
      name: emit just the column.
    - Stored property with a rename: emit ``column AS name``.
    - Derived property: emit ``(expr) AS name`` — the parens make the
      expression a single SQL term regardless of what operators it
      contains, so nesting inside a larger expression is safe.
  """
  if prop.derived:
    return f"({prop.sql}) AS {prop.name}"
  if prop.sql == prop.name:
    return prop.name
  return f"{prop.sql} AS {prop.name}"


def _join_entries(entries: list[list[str]]) -> list[str]:
  """Flatten a list of multi-line entries, adding trailing commas.

  Each entry is itself a list of lines (so an entry can span multiple
  rows without the caller having to thread commas through). We append
  a single ``,`` to the last line of every entry except the final
  one, mirroring how the spec's example formats node / edge lists.
  """
  out: list[str] = []
  for i, entry in enumerate(entries):
    for j, line in enumerate(entry):
      is_last_line = j == len(entry) - 1
      is_last_entry = i == len(entries) - 1
      if is_last_line and not is_last_entry:
        out.append(line + ",")
      else:
        out.append(line)
  return out


# --------------------------------------------------------------------- #
# Concept-index emitter (A3-A5)                                         #
# --------------------------------------------------------------------- #
#
# Companion to ``compile_graph``: emits two ``CREATE OR REPLACE TABLE``
# statements that materialize the concept-index sidecar tables consumed
# by ``OntologyRuntime``'s resolvers and verification layer (B1-B7,
# C1-C6 in the implementation plan). The main table holds one row per
# ``(entity_name, label, label_kind, language, scheme)`` tuple. The
# ``__meta`` sibling holds a single provenance row.
#
# Design constraints (pinned):
# - Pure SQL emission. No BigQuery client calls; no DDL execution.
#   Operators run the emitted SQL via ``bq query``, console, etc.
# - Atomic per statement via ``CREATE OR REPLACE TABLE T AS SELECT ...``
#   so each table flips in one transaction; pair-consistency between
#   main and meta is enforced at runtime via the shared
#   ``compile_fingerprint``.
# - Inline-UNNEST path with explicit ``ARRAY<STRUCT<...>>`` typing so
#   schema is unambiguous even with zero data rows.
# - Byte-identical SQL across runs for the same inputs; A2's row
#   builder is already deterministic, so we only need stable column
#   ordering and stable literal rendering here.
# - No timestamps in the SQL — ``compiled_at`` was deliberately removed
#   in the issue #58 round-9 design review for byte-identical output.

# Column lists are tuples to enforce ordering at the type level; any
# add/remove/rename is a deliberate edit visible in review.
_MAIN_COLUMNS: tuple[tuple[str, str], ...] = (
    ("entity_name", "STRING"),
    ("label", "STRING"),
    ("label_kind", "STRING"),
    ("notation", "STRING"),
    ("scheme", "STRING"),
    ("language", "STRING"),
    ("is_abstract", "BOOL"),
    ("compile_id", "STRING"),
    ("compile_fingerprint", "STRING"),
)

_META_COLUMNS: tuple[tuple[str, str], ...] = (
    ("compile_fingerprint", "STRING"),
    ("compile_id", "STRING"),
    ("ontology_fingerprint", "STRING"),
    ("binding_fingerprint", "STRING"),
    ("target_project", "STRING"),
    ("target_dataset", "STRING"),
    ("compiler_version", "STRING"),
)


def compile_concept_index(
    ontology: Ontology,
    binding: Binding,
    *,
    output_table: str,
    compiler_version: str,
) -> str:
  """Emit ``CREATE OR REPLACE TABLE`` SQL for the concept index + meta.

  Companion to :func:`compile_graph`. Returns SQL text (does not
  execute against BigQuery — operators run the result via ``bq
  query``, the console, an Airflow operator, etc.). Re-running with
  the same inputs produces byte-identical output.

  Args:
      ontology: Validated upstream ``Ontology``.
      binding: Validated upstream ``Binding`` referencing this
          ontology. Used to determine which concrete entities are in
          scope (per A2's "abstract always, concrete iff bound" rule)
          and to populate ``target_project`` / ``target_dataset`` on
          the meta row.
      output_table: Fully-qualified destination for the main table —
          ``project.dataset.table_name``. The ``__meta`` sibling is
          emitted at ``output_table + "__meta"``. Backticks are added
          by the emitter; do not pre-quote.
      compiler_version: Caller-supplied version string flowed into the
          ``compile_fingerprint`` so semver bumps invalidate stale
          meta rows.

  Returns:
      A single string containing both ``CREATE OR REPLACE TABLE``
      statements separated by a blank line, with a trailing newline.

  Raises:
      ValueError: If ``output_table`` is not a fully-qualified
          ``project.dataset.table`` triple, or if the ontology +
          binding produce zero concrete-or-abstract entities (which
          would emit a typeless empty array, losing schema).
  """
  _validate_output_table(output_table)

  rows = build_rows(ontology, binding, compiler_version=compiler_version)
  if not rows:
    raise ValueError(
        f"Cannot compile concept index for {output_table!r}: ontology + "
        f"binding produce no concrete or abstract entities. The emitter "
        f"refuses to write a typeless empty array. Ensure the binding "
        f"references at least one concrete entity from the ontology, or "
        f"that the ontology declares at least one abstract entity."
    )

  ont_fp = fingerprint_model(ontology)
  bnd_fp = fingerprint_model(binding)
  cfp = compile_fingerprint(ont_fp, bnd_fp, compiler_version)
  cid = compile_id(ont_fp, bnd_fp, compiler_version)

  meta_row = (
      cfp,
      cid,
      ont_fp,
      bnd_fp,
      binding.target.project,
      binding.target.dataset,
      compiler_version,
  )

  main_sql = _emit_table(
      table=output_table,
      columns=_MAIN_COLUMNS,
      rows=[_main_row_values(r) for r in rows],
  )
  meta_sql = _emit_table(
      table=output_table + "__meta",
      columns=_META_COLUMNS,
      rows=[meta_row],
  )

  return main_sql + "\n" + meta_sql


def _main_row_values(row: ConceptIndexRow) -> tuple:
  """Project a ``ConceptIndexRow`` to the tuple shape expected by
  :data:`_MAIN_COLUMNS` (positional alignment is part of the
  byte-deterministic contract).
  """
  return (
      row.entity_name,
      row.label,
      row.label_kind,
      row.notation,
      row.scheme,
      row.language,
      row.is_abstract,
      row.compile_id,
      row.compile_fingerprint,
  )


def _emit_table(
    *,
    table: str,
    columns: tuple[tuple[str, str], ...],
    rows: list[tuple],
) -> str:
  """Render one ``CREATE OR REPLACE TABLE T AS SELECT * FROM
  UNNEST(ARRAY<STRUCT<...>>[<rows>])`` statement.

  Explicit ``ARRAY<STRUCT<...>>`` typing keeps the schema stable for
  zero-row arrays (the meta table always has exactly one row, but
  we use the same emitter shape for symmetry and to make the
  contract explicit).
  """
  struct_decl = ", ".join(f"{name} {ty}" for name, ty in columns)
  lines: list[str] = []
  lines.append(f"CREATE OR REPLACE TABLE `{table}`")
  lines.append(f"AS SELECT * FROM UNNEST(ARRAY<STRUCT<{struct_decl}>>[")
  for i, row in enumerate(rows):
    rendered = ", ".join(_render_value(v) for v in row)
    suffix = "," if i < len(rows) - 1 else ""
    lines.append(f"  ({rendered}){suffix}")
  lines.append("]);")
  return "\n".join(lines) + "\n"


def _render_value(value) -> str:
  """Render a Python value to a deterministic GoogleSQL literal.

  - ``None``                → ``NULL``
  - ``bool``                → ``TRUE`` / ``FALSE``  (BigQuery convention,
                              upper case for byte-identical determinism)
  - ``str``                 → single-quoted literal via
                              :func:`_escape_string` — backslashes,
                              single quotes, and ASCII control
                              characters are all escaped so any
                              valid Python string round-trips through
                              GoogleSQL.
  - ``int`` / ``float``     → ``str(value)``  (no concept-index column
                              currently uses these, but the helper is
                              symmetric)
  """
  if value is None:
    return "NULL"
  if isinstance(value, bool):
    return "TRUE" if value else "FALSE"
  if isinstance(value, (int, float)):
    return str(value)
  if isinstance(value, str):
    return _escape_string(value)
  raise TypeError(f"Unsupported SQL literal type: {type(value).__name__}")


# Named C-style escapes BigQuery accepts inside single-quoted strings.
# Anything else in the 0x00-0x1F + 0x7F range goes through ``\xHH``.
_NAMED_CTRL_ESCAPES: dict[str, str] = {
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
    "\b": "\\b",
    "\f": "\\f",
}

_CTRL_RE = re.compile(r"[\x00-\x1F\x7F]")


def _escape_string(value: str) -> str:
  """Render a Python ``str`` as a GoogleSQL single-quoted literal.

  GoogleSQL string literals follow C-style escaping, **not** ANSI
  SQL's quote-doubling. The naive ANSI form
  ``"'" + s.replace("'", "''") + "'"`` produces invalid GoogleSQL
  when the input contains:

  - A single quote — ``'O''Brien'`` parses in GoogleSQL as two
    concatenated string literals (``'O'`` and ``'Brien'``) and
    errors with "concatenated string literals must be separated by
    whitespace or comments." GoogleSQL's escape for ``'`` inside a
    single-quoted literal is ``\\'``.
  - A literal backslash — ``"C:\\Users"`` → ``'C:\\Users'`` →
    BigQuery parses ``\\U`` as the 8-hex-digit Unicode escape and
    fails with "Illegal escape sequence."
  - An unrecognized escape in the input — ``"foo\\qbar"`` →
    ``\\q`` is rejected by the lexer.
  - A raw newline / carriage return / tab — these split the literal
    across source lines and break the surrounding SQL.

  Escape order matters: backslashes first (so the quote and
  control-char escapes added below aren't re-escaped), then single
  quotes via ``\\'``, then control characters.
  """
  out = value.replace("\\", "\\\\")
  out = out.replace("'", "\\'")
  out = _CTRL_RE.sub(_escape_ctrl_char, out)
  return "'" + out + "'"


def _escape_ctrl_char(match: re.Match) -> str:
  ch = match.group(0)
  if ch in _NAMED_CTRL_ESCAPES:
    return _NAMED_CTRL_ESCAPES[ch]
  return f"\\x{ord(ch):02x}"


# Each segment of a fully-qualified BigQuery identifier (project,
# dataset, table) accepts letters, digits, hyphens, and underscores.
# Hyphens are valid in project IDs; underscores in datasets/tables.
# The validator is intentionally a bit broader than the strictest
# interpretation so legitimate names aren't rejected, but it does
# reject characters that would let a caller break out of the
# backtick-wrapped emission (whitespace, slashes, semicolons,
# backticks, dots beyond the three segments).
_OUTPUT_TABLE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _validate_output_table(output_table: str) -> None:
  """Reject anything that's not a clean ``project.dataset.table`` triple.

  Backtick-quoting is added by the emitter, so the input must be
  the unquoted dotted form. The validator rejects:

  - Inputs containing backticks (caller should not pre-quote — and a
    stray backtick would terminate our outer quoting and break SQL).
  - Anything that doesn't split into exactly three non-empty
    dot-separated segments.
  - Segments containing characters outside ``[A-Za-z0-9_-]`` —
    spaces, slashes, semicolons, etc., that could otherwise produce
    malformed SQL inside the emitter's backtick wrapping.
  """
  if "`" in output_table:
    raise ValueError(
        f"output_table must not contain backticks; the emitter adds "
        f"them. Got {output_table!r}."
    )
  parts = output_table.split(".")
  if len(parts) != 3 or any(not p for p in parts):
    raise ValueError(
        f"output_table must be 'project.dataset.table'; got {output_table!r}"
    )
  for part in parts:
    if not _OUTPUT_TABLE_SEGMENT_RE.match(part):
      raise ValueError(
          f"output_table segment {part!r} contains invalid characters; "
          f"each segment must match [A-Za-z0-9_-]+. Got {output_table!r}."
      )
