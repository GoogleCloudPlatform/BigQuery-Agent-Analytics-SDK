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

"""Scaffold generator for ``gm scaffold``.

Given a validated ontology, emits two artifacts:

- ``table_ddl.sql`` — ``CREATE TABLE`` statements for every entity and
  relationship in the ontology.
- ``binding.yaml`` — a matching binding stub that is an immediate valid
  input to ``gm compile`` with no edits required.

The generator is a pure function: same ontology, same flags → byte-identical
output. It does not read or write files; the CLI handles all I/O.

Limitations in v0:

- Ontologies that use ``extends`` on any entity or relationship are
  rejected with a ``ValueError``. Flatten inheritance by hand before
  scaffolding.
"""

from __future__ import annotations

import re
from enum import Enum

import yaml

from .ontology_models import Ontology, PropertyType


# --------------------------------------------------------------------- #
# Type mapping                                                           #
# --------------------------------------------------------------------- #

_BQ_TYPE: dict[str, str] = {
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


# --------------------------------------------------------------------- #
# Naming                                                                 #
# --------------------------------------------------------------------- #


class NamingMode(str, Enum):
    """Column and table naming convention applied to ontology identifiers."""

    SNAKE = "snake"
    PRESERVE = "preserve"


def _to_snake(name: str) -> str:
    """Convert PascalCase or camelCase to snake_case."""
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name)
    return s.lower()


def _n(name: str, mode: NamingMode) -> str:
    """Apply the active naming rule to a single identifier."""
    return _to_snake(name) if mode == NamingMode.SNAKE else name


# --------------------------------------------------------------------- #
# Table reference                                                        #
# --------------------------------------------------------------------- #


def _table_ref(table_name: str, dataset: str, project: str) -> str:
    return f"`{project}.{dataset}.{table_name}`"


# --------------------------------------------------------------------- #
# Public entry point                                                     #
# --------------------------------------------------------------------- #


def scaffold(
    ontology: Ontology,
    *,
    dataset: str,
    project: str,
    naming: NamingMode = NamingMode.SNAKE,
) -> tuple[str, str]:
    """Generate ``(table_ddl_sql, binding_yaml)`` from a validated ontology.

    Both strings end with a newline. Given the same ontology, dataset,
    project, and naming mode the output is byte-identical across runs.

    Raises:
        ValueError: If any entity or relationship uses ``extends``
            (inheritance is out of scope for v0).
    """
    # Guard: inheritance is not supported in v0.
    for ent in ontology.entities:
        if ent.extends is not None:
            raise ValueError(
                f"Entity {ent.name!r} uses 'extends'; "
                "scaffold does not support inheritance in v0."
            )
    for rel in ontology.relationships:
        if rel.extends is not None:
            raise ValueError(
                f"Relationship {rel.name!r} uses 'extends'; "
                "scaffold does not support inheritance in v0."
            )

    entity_map = {e.name: e for e in ontology.entities}
    ddl_blocks: list[str] = []
    binding_entities: list[dict] = []
    binding_relationships: list[dict] = []

    # ----------------------------------------------------------------- #
    # Entities                                                           #
    # ----------------------------------------------------------------- #

    for entity in ontology.entities:
        table_name = _n(entity.name, naming)
        ref = _table_ref(table_name, dataset, project)

        pk_cols = entity.keys.primary  # guaranteed non-None by the ontology loader
        pk_set = set(pk_cols)

        # Only stored (non-derived) properties participate in DDL.
        stored = [p for p in entity.properties if p.expr is None]
        prop_by_name = {p.name: p for p in stored}

        # Column order: PK columns first (in keys.primary order),
        # then remaining stored non-PK properties in declaration order.
        pk_props = [prop_by_name[k] for k in pk_cols]
        non_pk_props = [p for p in stored if p.name not in pk_set]
        ordered = pk_props + non_pk_props

        col_lines: list[str] = []
        for prop in ordered:
            col_name = _n(prop.name, naming)
            bq_type = _BQ_TYPE[prop.type]
            null_suffix = " NOT NULL" if prop.name in pk_set else ""
            col_lines.append(f"  {col_name} {bq_type}{null_suffix}")

        pk_col_names = [_n(k, naming) for k in pk_cols]
        pk_constraint = f"  PRIMARY KEY ({', '.join(pk_col_names)}) NOT ENFORCED"

        ddl_blocks.append(
            f"CREATE TABLE {ref} (\n"
            + ",\n".join(col_lines + [pk_constraint])
            + "\n);"
        )

        source = f"{project}.{dataset}.{table_name}"
        binding_entities.append(
            {
                "name": entity.name,
                "source": source,
                "properties": [
                    {"name": p.name, "column": _n(p.name, naming)} for p in ordered
                ],
            }
        )

    # ----------------------------------------------------------------- #
    # Relationships                                                      #
    # ----------------------------------------------------------------- #

    for rel in ontology.relationships:
        table_name = _n(rel.name, naming)
        ref = _table_ref(table_name, dataset, project)

        from_entity = entity_map[rel.from_]
        to_entity = entity_map[rel.to]
        from_pk = from_entity.keys.primary
        to_pk = to_entity.keys.primary

        from_ep_props = {p.name: p for p in from_entity.properties}
        to_ep_props = {p.name: p for p in to_entity.properties}

        # Endpoint columns: from_<col> and to_<col>, NOT NULL.
        from_ep: list[tuple[str, str]] = [
            (f"from_{_n(k, naming)}", _BQ_TYPE[from_ep_props[k].type])
            for k in from_pk
        ]
        to_ep: list[tuple[str, str]] = [
            (f"to_{_n(k, naming)}", _BQ_TYPE[to_ep_props[k].type])
            for k in to_pk
        ]

        # Relationship key mode.
        keys = rel.keys
        rel_mode: str | None = None
        rel_pk_names: list[str] = []
        rel_add_names: list[str] = []
        if keys is not None:
            if keys.primary:
                rel_mode = "primary"
                rel_pk_names = list(keys.primary)
            elif keys.additional:
                rel_mode = "additional"
                rel_add_names = list(keys.additional)

        key_name_set = set(rel_pk_names) | set(rel_add_names)
        stored_rel = [p for p in rel.properties if p.expr is None]
        rel_prop_by_name = {p.name: p for p in stored_rel}
        rel_remaining = [p for p in stored_rel if p.name not in key_name_set]

        # Build column lines per mode.
        col_lines = []
        if rel_mode == "primary":
            # Mode 1: rel PK columns first, then endpoints, then remaining.
            for pk_name in rel_pk_names:
                prop = rel_prop_by_name[pk_name]
                col_lines.append(
                    f"  {_n(pk_name, naming)} {_BQ_TYPE[prop.type]} NOT NULL"
                )
            for col_name, bq_type in from_ep:
                col_lines.append(f"  {col_name} {bq_type} NOT NULL")
            for col_name, bq_type in to_ep:
                col_lines.append(f"  {col_name} {bq_type} NOT NULL")
            for prop in rel_remaining:
                col_lines.append(f"  {_n(prop.name, naming)} {_BQ_TYPE[prop.type]}")

        elif rel_mode == "additional":
            # Mode 2: endpoints first, then additional-key columns (NOT NULL),
            # then remaining.
            for col_name, bq_type in from_ep:
                col_lines.append(f"  {col_name} {bq_type} NOT NULL")
            for col_name, bq_type in to_ep:
                col_lines.append(f"  {col_name} {bq_type} NOT NULL")
            for add_name in rel_add_names:
                prop = rel_prop_by_name[add_name]
                col_lines.append(
                    f"  {_n(add_name, naming)} {_BQ_TYPE[prop.type]} NOT NULL"
                )
            for prop in rel_remaining:
                col_lines.append(f"  {_n(prop.name, naming)} {_BQ_TYPE[prop.type]}")

        else:
            # No keys: endpoints (NOT NULL), then remaining properties (nullable).
            for col_name, bq_type in from_ep:
                col_lines.append(f"  {col_name} {bq_type} NOT NULL")
            for col_name, bq_type in to_ep:
                col_lines.append(f"  {col_name} {bq_type} NOT NULL")
            for prop in rel_remaining:
                col_lines.append(f"  {_n(prop.name, naming)} {_BQ_TYPE[prop.type]}")

        # Constraint lines.
        constraint_lines: list[str] = []

        if rel_mode == "primary":
            pk_col_names = [_n(n, naming) for n in rel_pk_names]
            constraint_lines.append(
                f"  PRIMARY KEY ({', '.join(pk_col_names)}) NOT ENFORCED"
            )
        elif rel_mode == "additional":
            pk_col_names = (
                [c for c, _ in from_ep]
                + [c for c, _ in to_ep]
                + [_n(n, naming) for n in rel_add_names]
            )
            constraint_lines.append(
                f"  PRIMARY KEY ({', '.join(pk_col_names)}) NOT ENFORCED"
            )

        from_table = _n(from_entity.name, naming)
        to_table = _n(to_entity.name, naming)
        from_ref = _table_ref(from_table, dataset, project)
        to_ref = _table_ref(to_table, dataset, project)
        from_fk_cols = [c for c, _ in from_ep]
        to_fk_cols = [c for c, _ in to_ep]
        from_ref_cols = [_n(k, naming) for k in from_pk]
        to_ref_cols = [_n(k, naming) for k in to_pk]

        constraint_lines.append(
            f"  FOREIGN KEY ({', '.join(from_fk_cols)}) "
            f"REFERENCES {from_ref}({', '.join(from_ref_cols)}) NOT ENFORCED"
        )
        constraint_lines.append(
            f"  FOREIGN KEY ({', '.join(to_fk_cols)}) "
            f"REFERENCES {to_ref}({', '.join(to_ref_cols)}) NOT ENFORCED"
        )

        ddl_blocks.append(
            f"CREATE TABLE {ref} (\n"
            + ",\n".join(col_lines + constraint_lines)
            + "\n);"
        )

        # Binding: properties = all stored non-endpoint properties,
        # in the same order they appear in the DDL columns.
        if rel_mode == "primary":
            bound_props = [rel_prop_by_name[n] for n in rel_pk_names] + rel_remaining
        elif rel_mode == "additional":
            bound_props = [rel_prop_by_name[n] for n in rel_add_names] + rel_remaining
        else:
            bound_props = rel_remaining

        source = f"{project}.{dataset}.{table_name}"
        binding_relationships.append(
            {
                "name": rel.name,
                "source": source,
                "from_columns": [c for c, _ in from_ep],
                "to_columns": [c for c, _ in to_ep],
                "properties": [
                    {"name": p.name, "column": _n(p.name, naming)}
                    for p in bound_props
                ],
            }
        )

    # ----------------------------------------------------------------- #
    # Assemble outputs                                                   #
    # ----------------------------------------------------------------- #

    ddl = "\n\n".join(ddl_blocks) + "\n"

    binding_doc = {
        "binding": f"{ontology.ontology}-scaffold",
        "ontology": ontology.ontology,
        "target": {
            "backend": "bigquery",
            "project": project,
            "dataset": dataset,
        },
        "entities": binding_entities,
        "relationships": binding_relationships,
    }
    binding_yaml = (
        "# This file is user-owned. scaffold will not re-run against it.\n"
        + yaml.dump(binding_doc, default_flow_style=False, sort_keys=False)
    )

    return ddl, binding_yaml
