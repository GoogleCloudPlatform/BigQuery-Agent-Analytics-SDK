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

"""Pydantic models for binding YAML.

A binding document attaches a logical ontology (see ``ontology_models``)
to physical tables and columns on a specific backend. One file describes
one deployment target; it says *where* the data lives and never *how* it
is transformed.

These models capture shape only: required fields, enum membership,
unknown-key rejection, and list min-length constraints. Anything that
needs to consult the referenced ontology — checking that every
non-derived property is bound, that derived properties are *not* bound,
that relationship endpoint arities match the endpoint entity's primary
key, that bound types are representable on the target backend — belongs
to the binding loader, not here.

Only the BigQuery target is modeled today. Spanner lands alongside the
SDK's Spanner support.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Union

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from pydantic import field_validator


class Backend(str, Enum):
  """Backend identifier carried by the ``target`` block.

  Kept as a single-member enum rather than a literal so the YAML-level
  error message on an unsupported backend reads like any other enum
  mismatch, and so adding Spanner later is a one-line change here
  instead of a type swap at every call site.
  """

  BIGQUERY = "bigquery"


class BigQueryTarget(BaseModel):
  """Where the bound tables live on BigQuery.

  ``project`` and ``dataset`` double as (1) the physical location of the
  target dataset and (2) the defaults used to resolve bare ``table`` or
  ``dataset.table`` source names in each entity/relationship binding. A
  fully-qualified ``project.dataset.table`` source overrides both.
  """

  model_config = ConfigDict(extra="forbid")

  backend: Backend
  project: str
  dataset: str


class PropertyBinding(BaseModel):
  """Maps one ontology property to one physical column.

  ``name`` must name a property declared on the enclosing entity or
  relationship in the referenced ontology (inherited properties count);
  ``column`` is the physical column in that binding's ``source``. Type
  compatibility is not checked here — the physical column type must
  already match the ontology property type, upstream.
  """

  model_config = ConfigDict(extra="forbid")

  name: str = Field(min_length=1)
  column: str = Field(min_length=1)


class EntityBinding(BaseModel):
  """Realizes one ontology entity against a physical table or view.

  ``source`` is the physical table; to expose a filtered or joined slice
  (``type = 'customer'``, etc.) build a view in the warehouse and bind
  to that view rather than extending this model with expressions.

  Coverage is all-or-nothing per entity: deciding to include the entity
  in a binding commits you to binding *every* one of its non-derived
  properties (including inherited ones), no cherry-picking. If you want
  to omit the entity from this target, leave it out of the parent
  ``Binding`` entirely. Derived (``expr:``) properties are the mirror
  image — they must *never* appear in a binding, since the compiler
  substitutes their expression at DDL emission.

  The primary key is implicit: the ontology names the key properties,
  and the matching ``PropertyBinding`` entries here supply the columns.
  """

  model_config = ConfigDict(extra="forbid")

  name: str
  source: str
  properties: list[PropertyBinding]


# An entry in ``RelationshipBinding.from_columns`` / ``to_columns``.
# Two equivalent shapes are accepted:
#
# 1. ``str`` (legacy) — the edge-table column name. The target
#    property defaults to the endpoint entity's primary-key property
#    in declaration order: the Nth string entry maps to the endpoint's
#    Nth PK property. This is what every existing binding YAML uses.
#
# 2. ``dict[str, str]`` (explicit FK→PK mapping, new in #179) — a
#    single-key dict ``{edge_column: target_property}``. Lets a
#    relationship that needs distinct edge column names (e.g. a
#    self-edge like ``evolvedFrom`` where ``from_entity == to_entity``
#    so the legacy shape would collide on a single ``decision_execution_id``
#    column) declare both endpoints explicitly:
#
#       from_columns: [{src_decision_execution_id: id}]
#       to_columns:   [{dst_decision_execution_id: id}]
#
# Both shapes resolve to a canonical (edge_column, target_property)
# tuple stream in the binding loader. The validator below enforces
# the structural shape only (single-key + str→str). The loader
# enforces the semantic check that target_property names an
# *effective primary-key property* on the endpoint entity
# (honoring inherited PKs). This is FK→PK mapping, not FK→any-
# column: only a PK target uniquely identifies the row the edge
# endpoint points at.
ColumnRef = Union[str, dict[str, str]]


class RelationshipBinding(BaseModel):
  """Realizes one ontology relationship against a physical edge table.

  Coverage rules match ``EntityBinding``: once a relationship appears
  in a binding, every one of its non-derived properties must be bound
  (no cherry-picking), and derived (``expr:``) properties must not
  appear. To omit a relationship from a target, leave it out of the
  parent ``Binding`` entirely.

  ``from_columns`` and ``to_columns`` are the columns in ``source`` that
  carry the source and target endpoint keys — a list because primary
  keys may be composite. Each entry is either a bare column-name
  string (legacy; target property defaults to the endpoint's PK in
  declaration order) or a single-key ``{edge_column: target_property}``
  dict (explicit FK→PK mapping; required for self-edges and any case
  where the edge column name differs from the endpoint's PK property).
  Arity must equal the corresponding endpoint entity's primary-key
  arity, which only the loader can check against the ontology; the
  ``min_length=1`` guard here just rejects the structurally-invalid
  empty list.
  """

  model_config = ConfigDict(extra="forbid")

  name: str
  source: str
  from_columns: list[ColumnRef] = Field(min_length=1)
  to_columns: list[ColumnRef] = Field(min_length=1)
  properties: list[PropertyBinding] = Field(default_factory=list)

  @field_validator("from_columns", "to_columns")
  @classmethod
  def _validate_column_entries(cls, value: list[Any]) -> list[ColumnRef]:
    """Reject malformed dict entries at the shape boundary.

    Each entry must be either a bare string or a single-key dict
    mapping str → str. A multi-key dict, an empty dict, or a dict
    with non-string keys / values would slip past pydantic's
    structural type-check and surface as a confusing failure later
    in the loader; rejecting here gives the operator a precise
    error message anchored to the offending entry.
    """
    for idx, entry in enumerate(value):
      if isinstance(entry, str):
        if not entry:
          raise ValueError(
              f"column entry [{idx}] is an empty string; "
              "edge column names must be non-empty"
          )
        continue
      if isinstance(entry, dict):
        if len(entry) != 1:
          raise ValueError(
              f"column entry [{idx}] must be a single-key dict "
              "mapping ``edge_column`` (str) → ``target_property`` "
              f"(str); got dict with {len(entry)} key(s): {entry!r}. "
              "Composite endpoints are expressed by listing multiple "
              "single-key dicts in order."
          )
        edge_col, target_prop = next(iter(entry.items()))
        if not isinstance(edge_col, str) or not edge_col:
          raise ValueError(
              f"column entry [{idx}] dict key must be a non-empty str "
              f"naming the edge column; got {type(edge_col).__name__} "
              f"{edge_col!r}"
          )
        if not isinstance(target_prop, str) or not target_prop:
          raise ValueError(
              f"column entry [{idx}] dict value must be a non-empty str "
              f"naming the target property; got {type(target_prop).__name__} "
              f"{target_prop!r}"
          )
        continue
      raise ValueError(
          f"column entry [{idx}] must be a string or a single-key dict; "
          f"got {type(entry).__name__}: {entry!r}"
      )
    return value


class Binding(BaseModel):
  """Root of a binding YAML document.

  ``binding`` is this document's own name (typically suffixed with the
  environment, e.g. ``finance-bq-prod``). ``ontology`` is the *name* of
  the logical ontology this binding realizes — not a path; the loader
  resolves it to an ontology file.

  A binding may realize a *subset* of the referenced ontology at the
  entity/relationship level — any element left out of ``entities`` or
  ``relationships`` is simply absent from this target. Within each
  element you choose to include, however, coverage is total: see
  ``EntityBinding`` and ``RelationshipBinding``. Both list fields
  default to empty so that parse-only shape checks succeed on minimal
  stub files; a binding that realizes nothing is semantically
  pointless but not shape-invalid.
  """

  model_config = ConfigDict(extra="forbid")

  binding: str = Field(min_length=1)
  ontology: str = Field(min_length=1)
  target: BigQueryTarget
  entities: list[EntityBinding] = Field(default_factory=list)
  relationships: list[RelationshipBinding] = Field(default_factory=list)
