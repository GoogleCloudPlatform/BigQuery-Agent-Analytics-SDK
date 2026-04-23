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

"""Pydantic models for ontology YAML.

An ontology is a logical, backend-neutral description of a domain as a
graph: entity types (nodes), relationship types (edges), the properties
they carry, which properties act as keys, and single-parent inheritance
between types. Ontologies do not know where the data physically lives —
that is the binding's job (see ``binding_models``).

These models capture shape only. Cross-element rules (unique names,
inheritance cycles, key-column references, covariant endpoint
narrowing, key-mode combinations, etc.) are enforced by ``loader.py``
because they require walking the whole document.

The docstring for each model records the shape-level constraints it
owns and, where relevant, the semantic rule the loader enforces on top.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional, Union

from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field

# Annotations are an open-ended bag of string metadata attached to any
# ontology element — the modeled equivalent of free-form docstrings
# that downstream tools (catalogs, lineage, search) can consume without
# the ontology itself having to understand them. A value is either a
# single string or a list of strings; richer structure is deliberately
# excluded so annotations never grow into a rival schema language.
AnnotationValue = Union[str, list[str]]


class PropertyType(str, Enum):
  """The semantic type of a property.

  These are *logical* types, chosen to be representable on every
  supported backend; each one maps unambiguously to a GoogleSQL type
  shared by BigQuery and Spanner. Backend-specific gaps (e.g. Spanner
  not supporting ``time`` / ``datetime``) are surfaced later at binding
  time against a concrete target, not here.
  """

  STRING = "string"
  BYTES = "bytes"
  INTEGER = "integer"
  DOUBLE = "double"
  NUMERIC = "numeric"
  BOOLEAN = "boolean"
  DATE = "date"
  TIME = "time"
  DATETIME = "datetime"
  TIMESTAMP = "timestamp"
  JSON = "json"


class Cardinality(str, Enum):
  """Multiplicity of a relationship's endpoints.

  Read as ``<from-side>_to_<to-side>``: ``one_to_many`` means each
  ``from`` entity connects to many ``to`` entities but each ``to``
  connects back to at most one ``from``. Cardinality is optional on a
  relationship; absent, the loader treats the relationship as
  unconstrained (many-to-many in effect).
  """

  ONE_TO_ONE = "one_to_one"
  ONE_TO_MANY = "one_to_many"
  MANY_TO_ONE = "many_to_one"
  MANY_TO_MANY = "many_to_many"


class Property(BaseModel):
  """A typed attribute on an entity or relationship.

  ``expr`` marks a *derived* property: its value is computed from other
  properties on the same element rather than stored directly, and the
  binding must not list a column for it — the compiler substitutes the
  expression in its place. Stored (non-derived) properties leave
  ``expr`` unset.
  """

  model_config = ConfigDict(extra="forbid")

  name: str
  type: PropertyType
  expr: Optional[str] = None
  description: Optional[str] = None
  synonyms: Optional[list[str]] = None
  annotations: Optional[dict[str, AnnotationValue]] = None


class Keys(BaseModel):
  """Uniqueness constraints on an entity or relationship.

  Three roles, not three independent fields:

  - ``primary`` — the identity of a row; required on entities, optional
    on relationships (relationships may legally have no uniqueness, i.e.
    multi-edges are permitted).
  - ``alternate`` — additional unique tuples, each a list of property
    names; only meaningful alongside a ``primary``.
  - ``additional`` — uniqueness *without* picking a primary. Allowed on
    relationships only, and mutually exclusive with ``primary`` there.

  Each list's ``min_length=1`` constraint rejects the ``primary: []``
  /``additional: []`` /``alternate: []`` cases at parse time so the
  loader never has to disambiguate "empty list" from "field absent".
  The XOR between ``primary`` and ``additional``, the context rules
  (no ``additional`` on entities), and the requirement that every
  referenced name is a declared property all live in the loader.
  """

  model_config = ConfigDict(extra="forbid")

  primary: Optional[list[str]] = Field(default=None, min_length=1)
  alternate: Optional[list[list[str]]] = Field(default=None, min_length=1)
  additional: Optional[list[str]] = Field(default=None, min_length=1)


class Entity(BaseModel):
  """A node type in the ontology graph.

  ``extends`` names a parent entity (single inheritance); the child
  inherits the parent's properties and keys and is forbidden from
  redeclaring either. ``keys`` is marked optional here because a child
  inherits its parent's keys and must leave the field unset — the
  loader separately enforces that every entity ends up with an
  effective ``keys.primary``, whether declared or inherited.
  """

  model_config = ConfigDict(extra="forbid")

  name: str
  abstract: bool = False
  extends: Optional[str] = None
  keys: Optional[Keys] = None
  properties: list[Property] = Field(default_factory=list)
  description: Optional[str] = None
  synonyms: Optional[list[str]] = None
  annotations: Optional[dict[str, AnnotationValue]] = None


class Relationship(BaseModel):
  """An edge type connecting two entity types.

  ``from_`` and ``to`` name the endpoint entities; the trailing
  underscore on ``from_`` dodges the Python keyword, and
  ``populate_by_name=True`` together with the ``from`` alias lets
  callers write ``Relationship(from=..., to=...)`` in YAML-parsed
  mappings as well as ``from_=...`` in Python.

  Inheritance is single-parent (``extends``). A child relationship may
  narrow its endpoints *covariantly* — an endpoint must equal or be a
  subtype of the parent's corresponding endpoint — which the loader
  enforces. A child may not redefine the parent's cardinality; it may
  only inherit it silently or restate the same value.
  """

  model_config = ConfigDict(extra="forbid", populate_by_name=True)

  name: str
  abstract: bool = False
  extends: Optional[str] = None
  keys: Optional[Keys] = None
  from_: str = Field(alias="from")
  to: str
  cardinality: Optional[Cardinality] = None
  properties: list[Property] = Field(default_factory=list)
  description: Optional[str] = None
  synonyms: Optional[list[str]] = None
  annotations: Optional[dict[str, AnnotationValue]] = None


class Ontology(BaseModel):
  """Root of an ontology YAML document.

  ``ontology`` is the ontology's identifier — bindings reference it by
  this name, not by file path. ``entities`` is required and non-empty
  because an ontology with no node types has nothing for relationships
  or bindings to attach to; ``relationships`` is optional so pure
  taxonomies (entities + inheritance, no edges) remain legal.

  ``coerce_numbers_to_str`` lets authors write ``version: 0.1`` unquoted
  in YAML (which the parser would otherwise surface as a float) without
  hand-quoting every version string.
  """

  model_config = ConfigDict(extra="forbid", coerce_numbers_to_str=True)

  ontology: str
  version: Optional[str] = None
  entities: list[Entity] = Field(min_length=1)
  relationships: list[Relationship] = Field(default_factory=list)
  description: Optional[str] = None
  synonyms: Optional[list[str]] = None
  annotations: Optional[dict[str, AnnotationValue]] = None
