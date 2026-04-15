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

"""One-shot scaffold generator for BigQuery table DDL and binding stubs.

Given an ontology, a dataset name, and a naming convention, this module
emits two artifacts:

  1. ``table_ddl.sql`` — ``CREATE TABLE`` statements for every entity
     and relationship.
  2. ``binding.yaml`` — a matching binding stub that is immediately
     valid as input to ``gm compile``.

The generator is split into two stages following the same pattern as
``graph_ddl_compiler``:

  1. **Resolve.** Walk the ontology and produce intermediate
     ``_ScaffoldEntityTable`` / ``_ScaffoldRelTable`` dataclasses that
     capture every physical decision (column names, types, nullability,
     keys, foreign keys).
  2. **Emit.** Render each resolved table into DDL text and the whole
     set into a binding YAML string. The emitters are mechanical — all
     interesting decisions happen in stage 1.

Scaffold does *not* execute DDL, introspect BigQuery, or participate
in the compile pipeline. Its outputs are user-owned and hand-edited
after generation.
"""

from __future__ import annotations

from dataclasses import dataclass
import re

from .ontology_models import Entity
from .ontology_models import Keys
from .ontology_models import Ontology
from .ontology_models import PropertyType
from .ontology_models import Relationship

# ---------------------------------------------------------------------------
# Naming
# ---------------------------------------------------------------------------

_SNAKE_RE = re.compile(r"(?<=[a-z0-9])([A-Z])|(?<=[A-Z])([A-Z][a-z])")


def _to_snake_case(name: str) -> str:
  return _SNAKE_RE.sub(r"_\1\2", name).lower()


def _apply_naming(name: str, naming: str) -> str:
  if naming == "snake":
    return _to_snake_case(name)
  return name


# ---------------------------------------------------------------------------
# Type mapping
# ---------------------------------------------------------------------------

_ONTOLOGY_TO_BQ_TYPE: dict[PropertyType, str] = {
    PropertyType.STRING: "STRING",
    PropertyType.BYTES: "BYTES",
    PropertyType.INTEGER: "INT64",
    PropertyType.DOUBLE: "FLOAT64",
    PropertyType.NUMERIC: "NUMERIC",
    PropertyType.BOOLEAN: "BOOL",
    PropertyType.DATE: "DATE",
    PropertyType.TIME: "TIME",
    PropertyType.DATETIME: "DATETIME",
    PropertyType.TIMESTAMP: "TIMESTAMP",
    PropertyType.JSON: "JSON",
}

# ---------------------------------------------------------------------------
# Inheritance gate
# ---------------------------------------------------------------------------


def _reject_extends(ontology: Ontology) -> None:
  for entity in ontology.entities:
    if entity.extends is not None:
      raise ValueError(
          f"Entity {entity.name!r} uses 'extends'; v0 scaffold "
          "does not support inheritance."
      )
  for rel in ontology.relationships:
    if rel.extends is not None:
      raise ValueError(
          f"Relationship {rel.name!r} uses 'extends'; v0 scaffold "
          "does not support inheritance."
      )


def _reject_derived_keys(
    keys: Keys,
    prop_map: dict[str, object],
    owner: str,
) -> None:
  groups: list[tuple[str, list[str]]] = []
  if keys.primary:
    groups.append(("primary", keys.primary))
  if keys.additional:
    groups.append(("additional", keys.additional))
  for label, columns in groups:
    for col_name in columns:
      prop = prop_map.get(col_name)
      if prop is not None and getattr(prop, "expr", None) is not None:
        raise ValueError(
            f"{owner}: {label} key column {col_name!r} is a derived "
            "property (has 'expr'). Key columns must be stored."
        )


# ---------------------------------------------------------------------------
# Intermediate dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _ScaffoldColumn:
  name: str
  bq_type: str
  not_null: bool


@dataclass(frozen=True)
class _ScaffoldFK:
  columns: tuple[str, ...]
  ref_table: str
  ref_columns: tuple[str, ...]


@dataclass(frozen=True)
class _ScaffoldEntityTable:
  table_name: str
  columns: tuple[_ScaffoldColumn, ...]
  pk_columns: tuple[str, ...]


@dataclass(frozen=True)
class _ScaffoldRelTable:
  table_name: str
  columns: tuple[_ScaffoldColumn, ...]
  pk_columns: tuple[str, ...] | None
  foreign_keys: tuple[_ScaffoldFK, ...]
  suggested_pk_columns: tuple[str, ...] | None = None


# ---------------------------------------------------------------------------
# Resolve — entity tables
# ---------------------------------------------------------------------------


def _qualify(dataset: str, table: str, project: str) -> str:
  return f"{project}.{dataset}.{table}"


def _resolve_entity_table(
    entity: Entity,
    dataset: str,
    project: str,
    naming: str,
) -> _ScaffoldEntityTable:
  assert entity.keys is not None and entity.keys.primary is not None

  prop_map = {p.name: p for p in entity.properties}
  _reject_derived_keys(entity.keys, prop_map, f"Entity {entity.name!r}")

  pk_set = set(entity.keys.primary)

  pk_columns: list[_ScaffoldColumn] = []
  other_columns: list[_ScaffoldColumn] = []

  for pk_name in entity.keys.primary:
    prop = prop_map[pk_name]
    pk_columns.append(
        _ScaffoldColumn(
            name=_apply_naming(prop.name, naming),
            bq_type=_ONTOLOGY_TO_BQ_TYPE[prop.type],
            not_null=True,
        )
    )

  for prop in entity.properties:
    if prop.name in pk_set or prop.expr is not None:
      continue
    other_columns.append(
        _ScaffoldColumn(
            name=_apply_naming(prop.name, naming),
            bq_type=_ONTOLOGY_TO_BQ_TYPE[prop.type],
            not_null=False,
        )
    )

  table_name = _qualify(dataset, _apply_naming(entity.name, naming), project)
  columns = tuple(pk_columns + other_columns)
  pk_col_names = tuple(c.name for c in pk_columns)

  return _ScaffoldEntityTable(
      table_name=table_name,
      columns=columns,
      pk_columns=pk_col_names,
  )


# ---------------------------------------------------------------------------
# Resolve — relationship tables
# ---------------------------------------------------------------------------


def _endpoint_columns(
    prefix: str,
    entity: Entity,
    naming: str,
) -> list[_ScaffoldColumn]:
  assert entity.keys is not None and entity.keys.primary is not None
  cols: list[_ScaffoldColumn] = []
  prop_map = {p.name: p for p in entity.properties}
  for pk_name in entity.keys.primary:
    prop = prop_map[pk_name]
    col_name = f"{prefix}_{_apply_naming(prop.name, naming)}"
    cols.append(
        _ScaffoldColumn(
            name=col_name,
            bq_type=_ONTOLOGY_TO_BQ_TYPE[prop.type],
            not_null=True,
        )
    )
  return cols


def _resolve_rel_table(
    rel: Relationship,
    entity_map: dict[str, Entity],
    dataset: str,
    project: str,
    naming: str,
) -> _ScaffoldRelTable:
  from_entity = entity_map[rel.from_]
  to_entity = entity_map[rel.to]

  from_cols = _endpoint_columns("from", from_entity, naming)
  to_cols = _endpoint_columns("to", to_entity, naming)

  endpoint_col_names = {c.name for c in from_cols} | {c.name for c in to_cols}

  prop_columns: list[_ScaffoldColumn] = []
  for prop in rel.properties:
    if prop.expr is not None:
      continue
    col_name = _apply_naming(prop.name, naming)
    if col_name in endpoint_col_names:
      raise ValueError(
          f"Relationship {rel.name!r}: property {prop.name!r} "
          f"(column {col_name!r}) collides with a generated endpoint "
          "column. Rename the property or the entity key to resolve."
      )
    prop_columns.append(
        _ScaffoldColumn(
            name=col_name,
            bq_type=_ONTOLOGY_TO_BQ_TYPE[prop.type],
            not_null=False,
        )
    )

  keys: Keys | None = rel.keys
  if keys is not None:
    rel_prop_map = {p.name: p for p in rel.properties}
    _reject_derived_keys(keys, rel_prop_map, f"Relationship {rel.name!r}")
  pk_col_names: tuple[str, ...] | None = None

  if keys is not None and keys.primary is not None:
    # Mode 1: relationship has its own identity.
    pk_prop_map = {p.name: p for p in rel.properties}
    rel_pk_cols: list[_ScaffoldColumn] = []
    for pk_name in keys.primary:
      prop = pk_prop_map[pk_name]
      rel_pk_cols.append(
          _ScaffoldColumn(
              name=_apply_naming(prop.name, naming),
              bq_type=_ONTOLOGY_TO_BQ_TYPE[prop.type],
              not_null=True,
          )
      )
    # Remove PK props from prop_columns (they go first).
    pk_names_set = {_apply_naming(n, naming) for n in keys.primary}
    prop_columns = [c for c in prop_columns if c.name not in pk_names_set]
    columns = tuple(rel_pk_cols + from_cols + to_cols + prop_columns)
    pk_col_names = tuple(c.name for c in rel_pk_cols)

  elif keys is not None and keys.additional is not None:
    # Mode 2: effective key = (from_*, to_*, *additional).
    add_names_set = {_apply_naming(n, naming) for n in keys.additional}
    additional_cols: list[_ScaffoldColumn] = []
    remaining: list[_ScaffoldColumn] = []
    for c in prop_columns:
      if c.name in add_names_set:
        additional_cols.append(
            _ScaffoldColumn(
                name=c.name,
                bq_type=c.bq_type,
                not_null=True,
            )
        )
      else:
        remaining.append(c)
    columns = tuple(from_cols + to_cols + additional_cols + remaining)
    pk_col_names = (
        tuple(c.name for c in from_cols)
        + tuple(c.name for c in to_cols)
        + tuple(c.name for c in additional_cols)
    )

  else:
    # No keys block.
    columns = tuple(from_cols + to_cols + prop_columns)

  suggested_pk: tuple[str, ...] | None = None
  if pk_col_names is None:
    suggested_pk = tuple(c.name for c in from_cols) + tuple(
        c.name for c in to_cols
    )

  table_name = _qualify(dataset, _apply_naming(rel.name, naming), project)

  from_entity_table = _qualify(
      dataset, _apply_naming(from_entity.name, naming), project
  )
  to_entity_table = _qualify(
      dataset, _apply_naming(to_entity.name, naming), project
  )

  assert from_entity.keys is not None and from_entity.keys.primary is not None
  assert to_entity.keys is not None and to_entity.keys.primary is not None

  from_ref_cols = tuple(
      _apply_naming(n, naming) for n in from_entity.keys.primary
  )
  to_ref_cols = tuple(_apply_naming(n, naming) for n in to_entity.keys.primary)

  foreign_keys = (
      _ScaffoldFK(
          columns=tuple(c.name for c in from_cols),
          ref_table=from_entity_table,
          ref_columns=from_ref_cols,
      ),
      _ScaffoldFK(
          columns=tuple(c.name for c in to_cols),
          ref_table=to_entity_table,
          ref_columns=to_ref_cols,
      ),
  )

  return _ScaffoldRelTable(
      table_name=table_name,
      columns=columns,
      pk_columns=pk_col_names,
      foreign_keys=foreign_keys,
      suggested_pk_columns=suggested_pk,
  )


# ---------------------------------------------------------------------------
# Emit — DDL
# ---------------------------------------------------------------------------


def _pad_columns(columns: tuple[_ScaffoldColumn, ...]) -> list[str]:
  name_width = max(len(c.name) for c in columns)
  padded: list[str] = []
  for col in columns:
    nn = " NOT NULL" if col.not_null else ""
    padded.append(f"  {col.name:<{name_width}}  {col.bq_type}{nn}")
  return padded


def _emit_entity_ddl(table: _ScaffoldEntityTable) -> str:
  lines = [f"CREATE TABLE `{table.table_name}` ("]
  parts = _pad_columns(table.columns)
  parts.append(f"  PRIMARY KEY ({', '.join(table.pk_columns)}) NOT ENFORCED")
  lines.append(",\n".join(parts))
  lines.append(");")
  return "\n".join(lines) + "\n"


def _emit_rel_ddl(table: _ScaffoldRelTable) -> str:
  lines = [f"CREATE TABLE `{table.table_name}` ("]
  parts = _pad_columns(table.columns)
  if table.pk_columns is not None:
    parts.append(f"  PRIMARY KEY ({', '.join(table.pk_columns)}) NOT ENFORCED")
  if table.suggested_pk_columns is not None:
    suggested = ", ".join(table.suggested_pk_columns)
    parts.append(
        f"  -- TODO: uncomment if ({suggested}) is unique per row\n"
        f"  -- PRIMARY KEY ({suggested}) NOT ENFORCED"
    )
  for fk in table.foreign_keys:
    fk_cols = ", ".join(fk.columns)
    ref_cols = ", ".join(fk.ref_columns)
    parts.append(
        f"  FOREIGN KEY ({fk_cols}) "
        f"REFERENCES `{fk.ref_table}`({ref_cols}) NOT ENFORCED"
    )
  lines.append(",\n".join(parts))
  lines.append(");")
  return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Emit — binding YAML
# ---------------------------------------------------------------------------


def _emit_binding_yaml(
    ontology: Ontology,
    entity_tables: list[_ScaffoldEntityTable],
    rel_tables: list[_ScaffoldRelTable],
    dataset: str,
    project: str,
    naming: str,
) -> str:
  lines: list[str] = []
  lines.append(
      "# Generated by gm scaffold. "
      "This file is user-owned \u2014 edit freely."
  )
  lines.append(f"binding: {dataset}")
  lines.append(f"ontology: {ontology.ontology}")
  lines.append("target:")
  lines.append("  backend: bigquery")
  lines.append(f"  project: {project}")
  lines.append(f"  dataset: {dataset}")

  if entity_tables:
    lines.append("entities:")
    for entity, et in zip(ontology.entities, entity_tables):
      lines.append(f"  - name: {entity.name}")
      lines.append(f"    source: {et.table_name}")
      non_derived = [p for p in entity.properties if p.expr is None]
      if non_derived:
        lines.append("    properties:")
        for prop in non_derived:
          col = _apply_naming(prop.name, naming)
          lines.append("      - {" f"name: {prop.name}, column: {col}" "}")

  if rel_tables:
    lines.append("relationships:")
    for rel, rt in zip(ontology.relationships, rel_tables):
      from_fk, to_fk = rt.foreign_keys
      lines.append(f"  - name: {rel.name}")
      lines.append(f"    source: {rt.table_name}")
      lines.append(f"    from_columns: [{', '.join(from_fk.columns)}]")
      lines.append(f"    to_columns: [{', '.join(to_fk.columns)}]")
      non_derived = [p for p in rel.properties if p.expr is None]
      if non_derived:
        lines.append("    properties:")
        for prop in non_derived:
          col = _apply_naming(prop.name, naming)
          lines.append("      - {" f"name: {prop.name}, column: {col}" "}")

  return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def scaffold(
    ontology: Ontology,
    *,
    dataset: str,
    project: str,
    naming: str = "snake",
) -> tuple[str, str]:
  """Generate ``(ddl_text, binding_yaml_text)`` from an ontology.

  Raises:
      ValueError: If the ontology uses ``extends`` or an endpoint-column
          name collision is detected on a relationship.
  """
  _reject_extends(ontology)

  entity_map = {e.name: e for e in ontology.entities}

  entity_tables = [
      _resolve_entity_table(e, dataset, project, naming)
      for e in ontology.entities
  ]
  rel_tables = [
      _resolve_rel_table(r, entity_map, dataset, project, naming)
      for r in ontology.relationships
  ]

  ddl_parts: list[str] = []
  for et in entity_tables:
    ddl_parts.append(_emit_entity_ddl(et))
  for rt in rel_tables:
    ddl_parts.append(_emit_rel_ddl(rt))
  ddl_text = "\n".join(ddl_parts)

  binding_text = _emit_binding_yaml(
      ontology, entity_tables, rel_tables, dataset, project, naming
  )

  return ddl_text, binding_text
