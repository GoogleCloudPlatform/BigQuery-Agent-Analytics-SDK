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

"""Concept-index row builder (A2).

Transforms a validated ``(Ontology, Binding)`` pair into a list of
``ConceptIndexRow`` instances that the downstream emitter
(``compile_concept_index``, A3) writes as
``CREATE OR REPLACE TABLE ... AS SELECT * FROM UNNEST([STRUCT(...)])``.

Pinned in ``docs/implementation_plan_concept_index_runtime.md`` (A2)
and the RFC (``docs/entity_resolution_primitives.md`` §4.2).

Schema (matches the ``CREATE TABLE`` in the RFC):

- ``entity_name`` — the entity this row belongs to.
- ``label`` — the searchable text. For ``label_kind='notation'`` rows
  this is the notation value itself, so resolvers searching by label
  catch notation matches without a separate predicate.
- ``label_kind`` — one of ``name | pref | alt | hidden | synonym | notation``.
- ``notation`` — per-entity display token, repeats across all rows
  of the same entity (so a caller with a winning candidate row can
  read the entity's notation from the row directly without a join).
- ``scheme`` — concept-scheme membership; ``None`` means "entity is
  not declared as a member of any scheme."
- ``language`` — BCP-47 tag from a language-suffixed annotation
  key (e.g. ``skos:prefLabel@fr`` → ``"fr"``); ``None`` for the
  default-language label or notation rows.
- ``is_abstract`` — mirrors ``Entity.abstract``.
- ``compile_id`` — 12-hex display token, repeats across all rows of
  this compile.
- ``compile_fingerprint`` — 64-hex canonical integrity key.

Scope rule (per the RFC):

- **Abstract entities** are always included regardless of binding —
  they're informational and never need a binding row.
- **Concrete entities** are included iff they appear in
  ``binding.entities``.

Multiplicity contract:

- One row per ``(entity_name, label, label_kind, language, scheme)``
  membership tuple. A concept in 3 schemes × 5 labels emits 15 rows.
- One row per ``skos:notation`` value with ``label_kind='notation'``.
- The ``notation`` *column* on every row of an entity carries the
  entity's notation (or ``None`` if absent).

Sort order:

Rows are sorted lexicographically by
``(scheme, entity_name, label_kind, language, label, notation, is_abstract)``
with ``None`` last in each key. The sort exists purely for
deterministic emission so re-running ``gm compile`` produces
byte-identical SQL (a Phase 1 acceptance criterion). The
*semantic* priority on ``label_kind`` (``name > pref > alt > hidden
> synonym > notation``) is applied at query time via a CASE
expression — see RFC §6 finding 6.

Not re-exported from ``bigquery_ontology/__init__.py`` in v1 — the
module is importable directly via
``from bigquery_ontology.concept_index import build_rows``.

Known v1 limitation — selected-language SKOS labels collapse to
``label_kind='synonym'``. The current OWL importer
(``owl_importer.py``) flattens selected-language ``skos:prefLabel``
/ ``skos:altLabel`` / ``skos:hiddenLabel`` into ``Entity.synonyms``,
losing the kind distinction. Non-selected-language labels keep
their kind via ``@<lang>``-suffixed annotation keys (e.g.
``skos:prefLabel@fr``), so the row builder produces ``pref`` /
``alt`` / ``hidden`` rows correctly for those. The mismatch is
upstream of this module; a fix would preserve selected-language
kinds via annotations rather than flat ``synonyms``. Until that
ships, B7's winning-label priority (``name > pref > alt > hidden >
synonym > notation``) demotes selected-language SKOS labels to
``synonym``, which is acceptable for v1 because the resolver still
returns them — they just don't outrank a plain ``name`` match.
"""

from __future__ import annotations

import dataclasses
from typing import Optional

from bigquery_ontology._fingerprint import compile_fingerprint
from bigquery_ontology._fingerprint import compile_id
from bigquery_ontology._fingerprint import fingerprint_model
from bigquery_ontology.binding_models import Binding
from bigquery_ontology.ontology_models import Entity
from bigquery_ontology.ontology_models import Ontology

_LABEL_PREFIX_MAP: dict[str, str] = {
    "skos:prefLabel": "pref",
    "skos:altLabel": "alt",
    "skos:hiddenLabel": "hidden",
}


@dataclasses.dataclass(frozen=True)
class ConceptIndexRow:
  """One row in the emitted concept index. See module docstring."""

  entity_name: str
  label: str
  label_kind: str
  notation: Optional[str]
  scheme: Optional[str]
  language: Optional[str]
  is_abstract: bool
  compile_id: str
  compile_fingerprint: str


def build_rows(
    ontology: Ontology,
    binding: Binding,
    *,
    compiler_version: str,
) -> list[ConceptIndexRow]:
  """Build the deterministic row list for the concept index.

  Args:
      ontology: Validated upstream ``Ontology``.
      binding: Validated upstream ``Binding`` referencing this ontology.
          Used only to decide which concrete entities are bindable;
          abstract entities are always included regardless of binding.
      compiler_version: Caller-supplied version string flowed into
          ``compile_fingerprint`` so semver bumps with behavior
          changes invalidate older meta rows.

  Returns:
      A sorted list of ``ConceptIndexRow``. Empty list is legal
      (e.g., a binding that references no concrete entities and an
      ontology with no abstract entities).
  """
  ont_fp = fingerprint_model(ontology)
  bnd_fp = fingerprint_model(binding)
  cfp = compile_fingerprint(ont_fp, bnd_fp, compiler_version)
  cid = compile_id(ont_fp, bnd_fp, compiler_version)

  bound_entity_names = {eb.name for eb in binding.entities}

  rows: list[ConceptIndexRow] = []
  for entity in ontology.entities:
    if not entity.abstract and entity.name not in bound_entity_names:
      continue
    rows.extend(_rows_for_entity(entity, cid, cfp))

  rows = _dedup_rows(rows)
  rows.sort(key=_sort_key)
  return rows


def _dedup_rows(rows: list[ConceptIndexRow]) -> list[ConceptIndexRow]:
  """Collapse rows that share the contract tuple
  ``(entity_name, label, label_kind, language, scheme)`` into one.

  Duplicate inputs (e.g. ``synonyms=["Acct", "Acct"]`` or an
  annotation value list with repeats) would otherwise emit duplicate
  rows that the downstream resolver would have to dedupe at query
  time. Same value via different ``label_kind`` (e.g. ``"Acct"``
  declared in both ``synonyms`` and ``skos:altLabel``) is **not** a
  duplicate — different kinds, different rows, kept.
  """
  seen: set[tuple] = set()
  out: list[ConceptIndexRow] = []
  for row in rows:
    key = (row.entity_name, row.label, row.label_kind, row.language, row.scheme)
    if key in seen:
      continue
    seen.add(key)
    out.append(row)
  return out


def _rows_for_entity(
    entity: Entity,
    cid: str,
    cfp: str,
) -> list[ConceptIndexRow]:
  """Emit all rows for one entity (cross-product over schemes)."""
  notation_value = _entity_notation(entity)
  schemes = _entity_schemes(entity)  # always non-empty: at minimum [None].

  rows: list[ConceptIndexRow] = []
  for scheme in schemes:
    rows.extend(
        _rows_for_entity_in_scheme(entity, scheme, notation_value, cid, cfp)
    )
  return rows


def _rows_for_entity_in_scheme(
    entity: Entity,
    scheme: Optional[str],
    notation_value: Optional[str],
    cid: str,
    cfp: str,
) -> list[ConceptIndexRow]:
  """Emit one row per (label, label_kind, language) tuple for the
  given (entity, scheme) pair. Plus one notation row per notation
  value if any are declared.
  """
  rows: list[ConceptIndexRow] = []

  def _emit(label: str, kind: str, language: Optional[str]) -> None:
    rows.append(
        ConceptIndexRow(
            entity_name=entity.name,
            label=label,
            label_kind=kind,
            notation=notation_value,
            scheme=scheme,
            language=language,
            is_abstract=entity.abstract,
            compile_id=cid,
            compile_fingerprint=cfp,
        )
    )

  # 1. The canonical name row, always emitted.
  _emit(entity.name, "name", None)

  # 2. SKOS-typed labels from annotations: skos:prefLabel /
  # skos:altLabel / skos:hiddenLabel, with optional ``@<lang>`` suffix
  # for non-default languages.
  annotations = entity.annotations or {}
  for ann_key, ann_value in annotations.items():
    base, language = _split_lang_suffix(ann_key)
    kind = _LABEL_PREFIX_MAP.get(base)
    if kind is None:
      continue
    for label in _as_list(ann_value):
      _emit(label, kind, language)

  # 3. Plain synonyms (Entity.synonyms — kind unknown, treated as
  # 'synonym'). SKOS imports for the selected language flatten
  # pref/alt/hidden into here, so this is where most demos hit.
  for synonym in entity.synonyms or ():
    _emit(synonym, "synonym", None)

  # 4. Notation rows: one per skos:notation value. The same value
  # also appears in the per-row ``notation`` column on every row of
  # the entity (set above via ``notation_value``), so resolvers can
  # match by label OR look up the notation from the candidate row.
  notation_ann = annotations.get("skos:notation")
  for notation in _as_list(notation_ann):
    _emit(notation, "notation", None)

  return rows


def _entity_notation(entity: Entity) -> Optional[str]:
  """Return the notation value to repeat in the per-row ``notation``
  column. If multiple notations are declared, the lexicographically
  smallest is chosen so the column value is deterministic.
  """
  ann = (entity.annotations or {}).get("skos:notation")
  values = _as_list(ann)
  return min(values) if values else None


def _entity_schemes(entity: Entity) -> list[Optional[str]]:
  """Return the scheme membership list. Never empty: an entity not in
  any scheme yields ``[None]`` so a single set of rows is emitted.

  Both ``skos:inScheme`` and ``skos:topConceptOf`` are treated as
  scheme membership: a top concept of a scheme is still a member of
  that scheme, and queries like ``WHERE ci.scheme = 'X'`` should
  catch it. Values are unioned, deduped, and sorted so output is
  deterministic and a concept declared as both ``inScheme S`` and
  ``topConceptOf S`` produces a single row set, not two.
  """
  ann = entity.annotations or {}
  values = set(_as_list(ann.get("skos:inScheme")))
  values.update(_as_list(ann.get("skos:topConceptOf")))
  if not values:
    return [None]
  return sorted(values)


def _as_list(value) -> list[str]:
  """Normalize an ``AnnotationValue`` (str | list[str] | None) to a
  list of strings, dropping empties.
  """
  if value is None:
    return []
  if isinstance(value, list):
    return [v for v in value if v]
  if isinstance(value, str):
    return [value] if value else []
  return []


def _split_lang_suffix(key: str) -> tuple[str, Optional[str]]:
  """Split ``"skos:prefLabel@fr"`` → ``("skos:prefLabel", "fr")``.
  Plain ``"skos:prefLabel"`` → ``("skos:prefLabel", None)``.
  """
  if "@" not in key:
    return key, None
  base, _, lang = key.partition("@")
  if not lang:
    return base, None
  return base, lang


def _sort_key(row: ConceptIndexRow) -> tuple:
  """Total order over rows: ``(scheme, entity_name, label_kind,
  language, label, notation, is_abstract)`` with ``None`` last via a
  per-field ``(is_none, value)`` pair so Python's tuple compare
  cannot blow up on heterogeneous types.
  """

  def _none_last(v):
    return (v is None, v if v is not None else "")

  return (
      _none_last(row.scheme),
      row.entity_name,
      row.label_kind,
      _none_last(row.language),
      row.label,
      _none_last(row.notation),
      row.is_abstract,
  )
