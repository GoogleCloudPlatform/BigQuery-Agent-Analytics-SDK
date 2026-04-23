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

"""Import OWL ontologies into ``ontology.yaml`` format.

Converts OWL source files (Turtle or RDF/XML) into the YAML ontology
format defined by ``ontology.md``.  The importer is a four-stage
pipeline:

  1. **Parse.** Read OWL source into an RDF graph via ``rdflib``.
  2. **Filter.** Keep only resources whose IRIs match a user-provided
     namespace list.
  3. **Map.** Apply the OWL-to-ontology mapping table to produce
     entities, relationships, and properties.  Ambiguities emit
     ``FILL_IN`` placeholders with inline YAML comments.
  4. **Emit.** Write ``ontology.yaml`` in canonical alphabetical order.

The importer produces ontology files only; bindings are user-authored
separately.  ``rdflib`` is an optional dependency — import this module
only when the user invokes ``gm import-owl``.
"""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import Union

try:
  from rdflib import Graph
  from rdflib import URIRef
  from rdflib.namespace import OWL
  from rdflib.namespace import RDF
  from rdflib.namespace import RDFS
  from rdflib.namespace import SKOS
  from rdflib.namespace import XSD
except ImportError as _rdflib_err:
  raise ImportError(
      "rdflib is required for OWL import. "
      "Install it with: pip install 'bigquery-agent-analytics[owl]'"
  ) from _rdflib_err


# ---------------------------------------------------------------------------
# XSD → ontology type mapping (design doc §6)
# ---------------------------------------------------------------------------

_XSD_TYPE_MAP: dict[URIRef, str] = {
    XSD.string: "string",
    XSD.normalizedString: "string",
    XSD.token: "string",
    XSD.hexBinary: "bytes",
    XSD.base64Binary: "bytes",
    XSD.integer: "integer",
    XSD.int: "integer",
    XSD.long: "integer",
    XSD.short: "integer",
    XSD.byte: "integer",
    XSD.unsignedInt: "integer",
    XSD.unsignedLong: "integer",
    XSD.unsignedShort: "integer",
    XSD.nonNegativeInteger: "integer",
    XSD.positiveInteger: "integer",
    XSD.nonPositiveInteger: "integer",
    XSD.negativeInteger: "integer",
    XSD.double: "double",
    XSD.float: "double",
    XSD.decimal: "numeric",
    XSD.boolean: "boolean",
    XSD.date: "date",
    XSD.time: "time",
    XSD.dateTime: "timestamp",
    XSD.dateTimeStamp: "timestamp",
    URIRef("http://www.w3.org/1999/02/22-rdf-syntax-ns#JSON"): "json",
    XSD.anyURI: "string",
}

_ANYURI = XSD.anyURI


# ---------------------------------------------------------------------------
# IRI → local name (design doc §12)
# ---------------------------------------------------------------------------


def _local_name(iri: URIRef) -> str:
  s = str(iri)
  if "#" in s:
    return s.rsplit("#", 1)[1]
  return s.rsplit("/", 1)[-1]


def _in_namespace(iri: URIRef, namespaces: list[str]) -> bool:
  s = str(iri)
  return any(s.startswith(ns) for ns in namespaces)


def _qname_or_iri(g: "Graph", iri: URIRef) -> str:
  """Return ``prefix:local`` for a predicate whose namespace has a
  bound prefix; otherwise return the full IRI.

  Bare local names collide across vocabularies (``dc:title`` and
  ``dcterms:title`` both have local ``title``), so the annotation key
  must retain enough of the IRI to disambiguate.
  """
  try:
    prefix, _, local = g.namespace_manager.compute_qname(iri, generate=False)
    return f"{prefix}:{local}"
  except (KeyError, ValueError):
    return str(iri)


# ---------------------------------------------------------------------------
# Intermediate dataclasses
# ---------------------------------------------------------------------------

AnnotationValue = Union[str, list[str]]


@dataclass
class _ImportedProperty:
  name: str
  type: str
  xsd_annotation: str | None = None


@dataclass
class _ImportedEntity:
  name: str
  iri: URIRef
  abstract: bool = False
  description: str | None = None
  synonyms: list[str] = field(default_factory=list)
  extends: str | None = None
  extends_fill_in: bool = False
  extends_candidates: list[str] = field(default_factory=list)
  keys_primary: list[str] | None = None
  keys_fill_in: bool = False
  key_candidates: list[str] = field(default_factory=list)
  keys_excluded: list[str] = field(default_factory=list)
  properties: list[_ImportedProperty] = field(default_factory=list)
  annotations: dict[str, AnnotationValue] = field(default_factory=dict)
  comments: list[str] = field(default_factory=list)


@dataclass
class _ImportedRelationship:
  name: str
  iri: URIRef
  abstract: bool = False
  from_entity: str | None = None
  from_fill_in: bool = False
  from_candidates: list[str] = field(default_factory=list)
  to_entity: str | None = None
  to_fill_in: bool = False
  to_candidates: list[str] = field(default_factory=list)
  description: str | None = None
  synonyms: list[str] = field(default_factory=list)
  extends: str | None = None
  extends_fill_in: bool = False
  extends_candidates: list[str] = field(default_factory=list)
  cardinality: str | None = None
  annotations: dict[str, AnnotationValue] = field(default_factory=dict)
  comments: list[str] = field(default_factory=list)


@dataclass
class _DropSummary:
  excluded_by_namespace: dict[str, int] = field(default_factory=dict)
  dropped_features: dict[str, int] = field(default_factory=dict)
  skos_annotations: int = 0
  skos_labels_discarded_by_language: int = 0
  skos_external_matches: int = 0
  skos_concepts_imported: int = 0
  skos_relationships_imported: int = 0
  generic_annotations: int = 0


# ---------------------------------------------------------------------------
# Core extraction
# ---------------------------------------------------------------------------


def _count_lang_labels(lang_anns: dict[str, AnnotationValue]) -> int:
  """Total number of label values (across keys and list entries)."""
  total = 0
  for v in lang_anns.values():
    total += len(v) if isinstance(v, list) else 1
  return total


def _collapse_lang_anns(
    lang_anns: dict[str, list[str]],
) -> dict[str, AnnotationValue]:
  """Collapse a multi-valued language-annotation map to annotation form.

  Values are sorted and deduplicated; a single value becomes a scalar,
  multiple values become a sorted list. Non-selected-language labels
  can legitimately repeat per ``(predicate, language)`` (e.g. two French
  ``skos:altLabel``s), so the intermediate representation is list-valued
  to prevent silent overwrites.
  """
  out: dict[str, AnnotationValue] = {}
  for key, vals in lang_anns.items():
    uniq = sorted(set(vals))
    out[key] = uniq[0] if len(uniq) == 1 else uniq
  return out


def _pick_primary_label(
    labels,
    language: str = "en",
) -> tuple[str | None, list[str], dict[str, list[str]]]:
  """Pick the best label as description, return rest as synonyms.

  Prefers labels matching ``language``, then falls back to untagged or
  other languages. Within the preferred group, picks alphabetically
  first for determinism. Returns ``(primary, synonyms, lang_annotations)``
  where ``lang_annotations`` maps ``@lang`` keys to the list of label
  values seen in that non-selected language.
  """
  selected: list[str] = []
  untagged: list[str] = []
  other_lang: dict[str, list[str]] = {}
  for label in labels:
    lang = getattr(label, "language", None)
    if lang is not None and lang.startswith(language):
      selected.append(str(label))
    elif lang is None or lang == "":
      untagged.append(str(label))
    else:
      other_lang.setdefault(f"@{lang}", []).append(str(label))

  selected.sort()
  untagged.sort()
  if selected:
    return selected[0], selected[1:] + untagged, other_lang
  if untagged:
    return untagged[0], untagged[1:], other_lang
  return None, [], other_lang


def _extract_labels_and_description(
    g: Graph,
    iri: URIRef,
    language: str = "en",
) -> tuple[str | None, list[str], dict[str, AnnotationValue]]:
  """Extract rdfs:label → description, SKOS labels → synonyms.

  Returns ``(description, synonyms, lang_annotations)`` where
  ``lang_annotations`` maps keys like ``rdfs:label@fr`` to a scalar
  value (single label) or a sorted list (multiple labels in the same
  predicate+language combination).
  """
  raw_labels = list(g.objects(iri, RDFS.label))
  description, synonyms, lang_anns = _pick_primary_label(raw_labels, language)
  label_lang_anns: dict[str, list[str]] = {
      f"rdfs:label{k}": list(v) for k, v in lang_anns.items()
  }

  comments: list[str] = []
  for comment in g.objects(iri, RDFS.comment):
    comments.append(str(comment))
  if comments:
    if description:
      description = description + "\n\n" + "\n\n".join(comments)
    else:
      description = "\n\n".join(comments)

  for alt in g.objects(iri, SKOS.altLabel):
    lang = getattr(alt, "language", None)
    if lang is not None and not lang.startswith(language) and lang != "":
      label_lang_anns.setdefault(f"skos:altLabel@{lang}", []).append(str(alt))
    else:
      synonyms.append(str(alt))
  for pref in g.objects(iri, SKOS.prefLabel):
    lang = getattr(pref, "language", None)
    val = str(pref)
    if lang is not None and not lang.startswith(language) and lang != "":
      label_lang_anns.setdefault(f"skos:prefLabel@{lang}", []).append(val)
    elif val not in synonyms and val != description:
      synonyms.append(val)
  for hidden in g.objects(iri, SKOS.hiddenLabel):
    lang = getattr(hidden, "language", None)
    if lang is not None and not lang.startswith(language) and lang != "":
      label_lang_anns.setdefault(f"skos:hiddenLabel@{lang}", []).append(
          str(hidden)
      )
    else:
      synonyms.append(str(hidden))

  synonyms.sort()
  return description, synonyms, _collapse_lang_anns(label_lang_anns)


def _extract_parents(
    g: Graph,
    iri: URIRef,
    predicate: URIRef,
    namespaces: list[str],
    iri_to_name: dict[str, str] | None = None,
) -> tuple[str | None, bool, list[str], dict[str, AnnotationValue]]:
  parents: list[str] = []
  excluded: list[str] = []
  annotations: dict[str, AnnotationValue] = {}

  for parent in g.objects(iri, predicate):
    if not isinstance(parent, URIRef):
      continue
    if parent == OWL.Thing or parent == RDFS.Resource:
      continue
    if _in_namespace(parent, namespaces):
      # Resolve through iri_to_name so cross-kind parents (e.g. an
      # owl:Class whose subClassOf target is a pure SKOS concept
      # renamed to ``skos_<name>``) emit the correct name.
      if iri_to_name is not None and str(parent) in iri_to_name:
        parents.append(iri_to_name[str(parent)])
      else:
        parents.append(_local_name(parent))
    else:
      excluded.append(str(parent))

  if excluded:
    key = (
        "owl:subClassOf_excluded"
        if predicate == RDFS.subClassOf
        else "owl:subPropertyOf_excluded"
    )
    annotations[key] = excluded if len(excluded) > 1 else excluded[0]

  if len(parents) == 1:
    return parents[0], False, [], annotations
  elif len(parents) > 1:
    parents.sort()
    return None, True, parents, annotations
  return None, False, [], annotations


def _extract_keys(
    g: Graph, class_iri: URIRef, namespaces: list[str]
) -> tuple[list[str] | None, bool, list[str], list[str]]:
  for key_list in g.objects(class_iri, OWL.hasKey):
    keys: list[str] = []
    excluded: list[str] = []
    for item in g.items(key_list):
      if isinstance(item, URIRef):
        if _in_namespace(item, namespaces):
          keys.append(_local_name(item))
        else:
          excluded.append(str(item))
    if keys:
      return keys, False, [], excluded
    if excluded:
      return None, False, [], excluded
  return None, False, [], []


def _collect_drop_annotations(
    g: Graph,
    iri: URIRef,
    annotations: dict[str, AnnotationValue],
    comments: list[str],
    drops: _DropSummary,
) -> None:
  _OWL_EQUIV_CLASS = OWL.equivalentClass
  _OWL_EQUIV_PROP = OWL.equivalentProperty
  _OWL_DISJOINT = OWL.disjointWith
  _OWL_SAME_AS = OWL.sameAs
  _OWL_INVERSE = OWL.inverseOf

  drop_map: dict[URIRef, str] = {
      _OWL_EQUIV_CLASS: "owl:equivalentClass",
      _OWL_EQUIV_PROP: "owl:equivalentProperty",
      _OWL_DISJOINT: "owl:disjointWith",
      _OWL_SAME_AS: "owl:sameAs",
      _OWL_INVERSE: "owl:inverseOf",
  }

  for pred, ann_key in drop_map.items():
    values: list[str] = []
    for obj in g.objects(iri, pred):
      values.append(_local_name(obj) if isinstance(obj, URIRef) else str(obj))
    if values:
      values.sort()
      annotations[ann_key] = values if len(values) > 1 else values[0]
      drops.dropped_features[ann_key] = drops.dropped_features.get(
          ann_key, 0
      ) + len(values)

  characteristics: list[str] = []
  char_types = {
      OWL.TransitiveProperty: "Transitive",
      OWL.SymmetricProperty: "Symmetric",
      OWL.AsymmetricProperty: "Asymmetric",
      OWL.ReflexiveProperty: "Reflexive",
      OWL.IrreflexiveProperty: "Irreflexive",
      OWL.InverseFunctionalProperty: "InverseFunctional",
  }
  for char_type, label in char_types.items():
    if (iri, RDF.type, char_type) in g:
      characteristics.append(label)

  if characteristics:
    characteristics.sort()
    annotations["owl:characteristics"] = characteristics
    drops.dropped_features["owl:characteristics"] = drops.dropped_features.get(
        "owl:characteristics", 0
    ) + len(characteristics)

  restriction_preds = {
      OWL.someValuesFrom,
      OWL.allValuesFrom,
      OWL.minCardinality,
      OWL.maxCardinality,
      OWL.qualifiedCardinality,
      OWL.hasValue,
  }
  for pred in restriction_preds:
    for obj in g.objects(iri, pred):
      val = _local_name(obj) if isinstance(obj, URIRef) else str(obj)
      comments.append(f"restriction {_local_name(pred)}: {val}")
      drops.dropped_features["restrictions"] = (
          drops.dropped_features.get("restrictions", 0) + 1
      )


def _precompute_iri_to_name(g: Graph, namespaces: list[str]) -> dict[str, str]:
  """Build an IRI→future-entity-name map for every in-namespace OWL class
  and SKOS concept.

  Pre-computed *before* extraction so passes that resolve references
  (rdfs:subClassOf, rdfs:subPropertyOf, rdfs:domain, rdfs:range) can
  emit the same name the concept will ultimately carry — including the
  ``skos_`` prefix applied to SKOS-only concepts. Without this, an OWL
  class that extends a pure SKOS concept would emit ``extends:
  Category`` while the concept itself is imported as ``skos_Category``.
  """
  mapping: dict[str, str] = {}
  for cls in g.subjects(RDF.type, OWL.Class):
    if isinstance(cls, URIRef) and _in_namespace(cls, namespaces):
      mapping[str(cls)] = _local_name(cls)
  for concept in g.subjects(RDF.type, SKOS.Concept):
    if isinstance(concept, URIRef) and _in_namespace(concept, namespaces):
      iri_str = str(concept)
      if iri_str in mapping:
        continue
      mapping[iri_str] = f"skos_{_local_name(concept)}"
  return mapping


def _extract_entities(
    g: Graph,
    namespaces: list[str],
    drops: _DropSummary,
    language: str = "en",
    iri_to_name: dict[str, str] | None = None,
) -> dict[str, _ImportedEntity]:
  entities: dict[str, _ImportedEntity] = {}

  for cls in g.subjects(RDF.type, OWL.Class):
    if not isinstance(cls, URIRef):
      continue
    if not _in_namespace(cls, namespaces):
      drops.excluded_by_namespace["classes"] = (
          drops.excluded_by_namespace.get("classes", 0) + 1
      )
      continue

    name = _local_name(cls)
    if name in entities:
      raise ValueError(
          f"Name collision: entity {name!r} maps to both "
          f"<{entities[name].iri}> and <{cls}>. Narrow the "
          "namespace filter to resolve."
      )

    description, synonyms, lang_anns = _extract_labels_and_description(
        g,
        cls,
        language,
    )
    drops.skos_labels_discarded_by_language += _count_lang_labels(lang_anns)
    extends, extends_fill_in, extends_candidates, parent_anns = (
        _extract_parents(
            g, cls, RDFS.subClassOf, namespaces, iri_to_name=iri_to_name
        )
    )
    keys_primary, keys_fill_in, key_candidates, keys_excluded = _extract_keys(
        g, cls, namespaces
    )

    annotations: dict[str, AnnotationValue] = {}
    comments: list[str] = []
    annotations.update(lang_anns)
    annotations.update(parent_anns)
    if keys_excluded:
      annotations["owl:hasKey_excluded"] = (
          keys_excluded if len(keys_excluded) > 1 else keys_excluded[0]
      )
    _collect_drop_annotations(g, cls, annotations, comments, drops)

    entities[name] = _ImportedEntity(
        name=name,
        iri=cls,
        description=description,
        synonyms=synonyms,
        extends=extends,
        extends_fill_in=extends_fill_in,
        extends_candidates=extends_candidates,
        keys_primary=keys_primary,
        keys_fill_in=keys_fill_in,
        key_candidates=key_candidates,
        keys_excluded=keys_excluded,
        annotations=annotations,
        comments=comments,
    )

  return entities


def _extract_datatype_properties(
    g: Graph,
    entities: dict[str, _ImportedEntity],
    namespaces: list[str],
    drops: _DropSummary,
    iri_to_name: dict[str, str] | None = None,
) -> None:
  for prop in g.subjects(RDF.type, OWL.DatatypeProperty):
    if not isinstance(prop, URIRef):
      continue
    if not _in_namespace(prop, namespaces):
      drops.excluded_by_namespace["datatype_properties"] = (
          drops.excluded_by_namespace.get("datatype_properties", 0) + 1
      )
      continue

    prop_name = _local_name(prop)
    domains: list[str] = []
    for domain in g.objects(prop, RDFS.domain):
      if isinstance(domain, URIRef) and _in_namespace(domain, namespaces):
        # Resolve IRI through iri_to_name when available (handles SKOS
        # concepts whose entity name carries a ``skos_`` prefix).
        if iri_to_name is not None and str(domain) in iri_to_name:
          domains.append(iri_to_name[str(domain)])
        else:
          domains.append(_local_name(domain))

    ranges: list[URIRef] = []
    for range_ in g.objects(prop, RDFS.range):
      if isinstance(range_, URIRef):
        ranges.append(range_)

    ont_type = "string"
    xsd_annotation: str | None = None
    if len(ranges) == 1:
      xsd_ref = ranges[0]
      if xsd_ref in _XSD_TYPE_MAP:
        ont_type = _XSD_TYPE_MAP[xsd_ref]
        if xsd_ref == _ANYURI:
          xsd_annotation = "anyURI"
    elif len(ranges) > 1:
      ont_type = "FILL_IN"

    imported_prop = _ImportedProperty(
        name=prop_name,
        type=ont_type,
        xsd_annotation=xsd_annotation,
    )

    for domain_name in domains:
      if domain_name in entities:
        existing = {p.name for p in entities[domain_name].properties}
        if prop_name in existing:
          raise ValueError(
              f"Duplicate property {prop_name!r} on entity "
              f"{domain_name!r}. Two OWL properties with the same "
              "local name share a domain."
          )
        entities[domain_name].properties.append(imported_prop)


def _extract_relationships(
    g: Graph,
    entities: dict[str, _ImportedEntity],
    namespaces: list[str],
    drops: _DropSummary,
    language: str = "en",
    iri_to_name: dict[str, str] | None = None,
) -> dict[str, _ImportedRelationship]:
  relationships: dict[str, _ImportedRelationship] = {}

  def _resolve_endpoint(iri: URIRef) -> str:
    """Resolve an IRI to an entity name, using iri_to_name when
    available (handles SKOS concepts with ``skos_`` name prefixes)."""
    if iri_to_name is not None and str(iri) in iri_to_name:
      return iri_to_name[str(iri)]
    return _local_name(iri)

  for prop in g.subjects(RDF.type, OWL.ObjectProperty):
    if not isinstance(prop, URIRef):
      continue
    if not _in_namespace(prop, namespaces):
      drops.excluded_by_namespace["object_properties"] = (
          drops.excluded_by_namespace.get("object_properties", 0) + 1
      )
      continue

    name = _local_name(prop)
    if name in relationships:
      raise ValueError(
          f"Name collision: relationship {name!r} maps to both "
          f"<{relationships[name].iri}> and <{prop}>. Narrow the "
          "namespace filter to resolve."
      )

    description, synonyms, lang_anns = _extract_labels_and_description(
        g,
        prop,
        language,
    )
    drops.skos_labels_discarded_by_language += _count_lang_labels(lang_anns)

    domains: list[str] = []
    domain_excluded: list[str] = []
    for domain in g.objects(prop, RDFS.domain):
      if not isinstance(domain, URIRef):
        continue
      if _in_namespace(domain, namespaces):
        domains.append(_resolve_endpoint(domain))
      else:
        domain_excluded.append(str(domain))

    ranges: list[str] = []
    range_excluded: list[str] = []
    for range_ in g.objects(prop, RDFS.range):
      if not isinstance(range_, URIRef):
        continue
      if _in_namespace(range_, namespaces):
        ranges.append(_resolve_endpoint(range_))
      else:
        range_excluded.append(str(range_))

    from_entity: str | None = None
    from_fill_in = False
    from_candidates: list[str] = []
    if len(domains) == 1:
      from_entity = domains[0]
    elif len(domains) > 1:
      from_fill_in = True
      from_candidates = sorted(domains)
    elif not domain_excluded:
      from_fill_in = True

    to_entity: str | None = None
    to_fill_in = False
    to_candidates: list[str] = []
    if len(ranges) == 1:
      to_entity = ranges[0]
    elif len(ranges) > 1:
      to_fill_in = True
      to_candidates = sorted(ranges)
    elif not range_excluded:
      to_fill_in = True

    cardinality: str | None = None
    if (prop, RDF.type, OWL.FunctionalProperty) in g:
      cardinality = "many_to_one"

    annotations: dict[str, AnnotationValue] = {}
    annotations.update(lang_anns)
    comments: list[str] = []

    if domain_excluded:
      annotations["owl:domain_excluded"] = (
          domain_excluded if len(domain_excluded) > 1 else domain_excluded[0]
      )
    if range_excluded:
      annotations["owl:range_excluded"] = (
          range_excluded if len(range_excluded) > 1 else range_excluded[0]
      )

    _collect_drop_annotations(g, prop, annotations, comments, drops)

    extends, extends_fill_in, extends_candidates, parent_anns = (
        _extract_parents(
            g, prop, RDFS.subPropertyOf, namespaces, iri_to_name=iri_to_name
        )
    )
    annotations.update(parent_anns)

    relationships[name] = _ImportedRelationship(
        name=name,
        iri=prop,
        from_entity=from_entity,
        from_fill_in=from_fill_in,
        from_candidates=from_candidates,
        to_entity=to_entity,
        to_fill_in=to_fill_in,
        to_candidates=to_candidates,
        description=description,
        synonyms=synonyms,
        extends=extends,
        extends_fill_in=extends_fill_in,
        extends_candidates=extends_candidates,
        cardinality=cardinality,
        annotations=annotations,
        comments=comments,
    )

  return relationships


# ---------------------------------------------------------------------------
# Resolve: finalize keys and FILL_IN markers
# ---------------------------------------------------------------------------


def _resolve_keys(entities: dict[str, _ImportedEntity]) -> None:
  for entity in entities.values():
    if entity.keys_primary is not None:
      continue
    if entity.abstract:
      continue
    if entity.extends is not None or entity.extends_fill_in:
      continue
    prop_names = [p.name for p in entity.properties]
    entity.keys_fill_in = True
    entity.key_candidates = prop_names


# ---------------------------------------------------------------------------
# SKOS extraction
# ---------------------------------------------------------------------------

# SKOS literal predicates that map to annotations with ``skos:`` prefix.
_SKOS_LITERAL_PREDICATES: dict[URIRef, str] = {}
# Populated lazily after rdflib is imported (SKOS namespace is module-level).


def _skos_literal_preds() -> dict[URIRef, str]:
  if not _SKOS_LITERAL_PREDICATES:
    _SKOS_LITERAL_PREDICATES.update(
        {
            SKOS.definition: "skos:definition",
            SKOS.notation: "skos:notation",
            SKOS.scopeNote: "skos:scopeNote",
            SKOS.example: "skos:example",
            SKOS.historyNote: "skos:historyNote",
            SKOS.editorialNote: "skos:editorialNote",
            SKOS.changeNote: "skos:changeNote",
        }
    )
  return _SKOS_LITERAL_PREDICATES


# SKOS reference predicates that map to annotations (IRI target stored as
# string value).
_SKOS_REF_ANNOTATION_PREDICATES: dict[URIRef, str] = {}


def _skos_ref_ann_preds() -> dict[URIRef, str]:
  if not _SKOS_REF_ANNOTATION_PREDICATES:
    _SKOS_REF_ANNOTATION_PREDICATES.update(
        {
            SKOS.inScheme: "skos:inScheme",
            SKOS.topConceptOf: "skos:topConceptOf",
        }
    )
  return _SKOS_REF_ANNOTATION_PREDICATES


# SKOS graph-shaped predicates that produce abstract relationships.
_SKOS_BROADER = SKOS.broader
_SKOS_NARROWER = SKOS.narrower
_SKOS_RELATED = SKOS.related

_SKOS_MATCH_PREDICATES: dict[URIRef, str] = {}


def _skos_match_preds() -> dict[URIRef, str]:
  if not _SKOS_MATCH_PREDICATES:
    _SKOS_MATCH_PREDICATES.update(
        {
            SKOS.exactMatch: "skos_exactMatch",
            SKOS.closeMatch: "skos_closeMatch",
            SKOS.broadMatch: "skos_broadMatch",
            SKOS.narrowMatch: "skos_narrowMatch",
            SKOS.relatedMatch: "skos_relatedMatch",
        }
    )
  return _SKOS_MATCH_PREDICATES


def _extract_skos_annotations(
    g: Graph,
    iri: URIRef,
    annotations: dict[str, AnnotationValue],
    drops: _DropSummary,
) -> None:
  """Extract SKOS literal and reference predicates as annotations."""
  for pred, ann_key in _skos_literal_preds().items():
    values: list[str] = []
    for obj in g.objects(iri, pred):
      values.append(str(obj))
    if values:
      values.sort()
      annotations[ann_key] = values if len(values) > 1 else values[0]
      drops.skos_annotations += len(values)

  for pred, ann_key in _skos_ref_ann_preds().items():
    values = []
    for obj in g.objects(iri, pred):
      # Preserve the full IRI — collapsing to local name loses identity
      # when two schemes share a local name (e.g. ``ns1/Scheme`` vs.
      # ``ns2/Scheme``) and defeats downstream consumers that look up
      # scheme membership by IRI.
      values.append(str(obj))
    if values:
      values.sort()
      annotations[ann_key] = values if len(values) > 1 else values[0]
      drops.skos_annotations += len(values)


def _extract_skos_concepts(
    g: Graph,
    entities: dict[str, _ImportedEntity],
    namespaces: list[str],
    drops: _DropSummary,
    language: str = "en",
) -> dict[str, str]:
  """Import standalone ``skos:Concept`` resources as abstract entities.

  Resources already imported as ``owl:Class`` are enriched with SKOS
  metadata but remain concrete. Returns a mapping from SKOS concept
  IRI (as string) to entity name, for use by relationship extraction.
  """
  concept_iri_to_name: dict[str, str] = {}

  # First, register OWL entities that are also SKOS concepts.
  for entity in entities.values():
    if (entity.iri, RDF.type, SKOS.Concept) in g:
      concept_iri_to_name[str(entity.iri)] = entity.name
      _extract_skos_annotations(g, entity.iri, entity.annotations, drops)

  # Then, import standalone SKOS concepts (not owl:Class).
  for concept in g.subjects(RDF.type, SKOS.Concept):
    if not isinstance(concept, URIRef):
      continue
    if not _in_namespace(concept, namespaces):
      drops.excluded_by_namespace["skos_concepts"] = (
          drops.excluded_by_namespace.get("skos_concepts", 0) + 1
      )
      continue

    iri_str = str(concept)
    if iri_str in concept_iri_to_name:
      continue  # Already handled as OWL+SKOS above.

    local = _local_name(concept)
    name = f"skos_{local}"

    if name in entities:
      raise ValueError(
          f"Name collision: SKOS concept {name!r} collides with an "
          f"existing entity. Narrow the namespace filter to resolve."
      )

    # rdfs:label and rdfs:comment still populate description if
    # present on a pure SKOS concept; skos:* labels go to synonyms or
    # language annotations per the label extractor.
    description, synonyms, lang_anns = _extract_labels_and_description(
        g,
        concept,
        language,
    )
    drops.skos_labels_discarded_by_language += _count_lang_labels(lang_anns)

    annotations: dict[str, AnnotationValue] = {}
    annotations.update(lang_anns)
    _extract_skos_annotations(g, concept, annotations, drops)

    entities[name] = _ImportedEntity(
        name=name,
        iri=concept,
        abstract=True,
        description=description,
        synonyms=synonyms,
        annotations=annotations,
    )
    concept_iri_to_name[iri_str] = name
    drops.skos_concepts_imported += 1

  return concept_iri_to_name


def _extract_skos_relationships(
    g: Graph,
    entities: dict[str, _ImportedEntity],
    iri_to_name: dict[str, str],
    namespaces: list[str],
    drops: _DropSummary,
) -> list[_ImportedRelationship]:
  """Extract SKOS graph-shaped predicates as abstract relationships.

  ``iri_to_name`` must cover all imported entities (OWL + SKOS). SKOS
  predicates whose subject or object is not in the map are silently
  skipped (external targets for match predicates are handled further
  below as annotations).

  Returns a list (not dict) because abstract relationships may share
  names with different endpoint pairs.
  """
  skos_rels: list[_ImportedRelationship] = []
  seen: set[tuple[str, str, str]] = set()  # (name, from, to)

  # A sentinel IRI for SKOS-sourced relationships (they have no single
  # OWL IRI; we use the predicate IRI as a reasonable stand-in).
  def _add_rel(
      rel_name: str,
      from_name: str,
      to_name: str,
      pred_iri: URIRef,
  ) -> None:
    key = (rel_name, from_name, to_name)
    if key in seen:
      return  # Deduplicate (e.g. broader + inverse narrower).
    seen.add(key)
    skos_rels.append(
        _ImportedRelationship(
            name=rel_name,
            iri=pred_iri,
            abstract=True,
            from_entity=from_name,
            to_entity=to_name,
        )
    )
    drops.skos_relationships_imported += 1

  # skos:broader — from child to parent.
  for subj, obj in g.subject_objects(_SKOS_BROADER):
    if not isinstance(subj, URIRef) or not isinstance(obj, URIRef):
      continue
    from_name = iri_to_name.get(str(subj))
    to_name = iri_to_name.get(str(obj))
    if from_name and to_name:
      _add_rel("skos_broader", from_name, to_name, _SKOS_BROADER)

  # skos:narrower — normalize to broader (swap direction).
  for subj, obj in g.subject_objects(_SKOS_NARROWER):
    if not isinstance(subj, URIRef) or not isinstance(obj, URIRef):
      continue
    from_name = iri_to_name.get(str(obj))
    to_name = iri_to_name.get(str(subj))
    if from_name and to_name:
      _add_rel("skos_broader", from_name, to_name, _SKOS_BROADER)

  # skos:related — symmetric, emit one direction per pair.
  # Sort endpoints so mutual assertions (:a related :b AND :b related :a)
  # collapse to a single edge under the ordered dedupe key.
  for subj, obj in g.subject_objects(_SKOS_RELATED):
    if not isinstance(subj, URIRef) or not isinstance(obj, URIRef):
      continue
    from_name = iri_to_name.get(str(subj))
    to_name = iri_to_name.get(str(obj))
    if from_name and to_name:
      a, b = sorted((from_name, to_name))
      _add_rel("skos_related", a, b, _SKOS_RELATED)

  # Match predicates — relationship if target is imported, else annotation.
  # Collect external matches per (entity, predicate) to allow sort before
  # assignment (determinism).
  external_matches: dict[tuple[str, str], list[str]] = {}
  for pred, rel_name in _skos_match_preds().items():
    ann_key = rel_name.replace(
        "skos_", "skos:"
    )  # skos_exactMatch → skos:exactMatch
    for subj, obj in g.subject_objects(pred):
      if not isinstance(subj, URIRef):
        continue
      from_name = iri_to_name.get(str(subj))
      if not from_name:
        continue
      if isinstance(obj, URIRef) and str(obj) in iri_to_name:
        to_name = iri_to_name[str(obj)]
        _add_rel(rel_name, from_name, to_name, pred)
      else:
        # External IRI or literal → annotation on the source entity.
        val = str(obj) if isinstance(obj, URIRef) else str(obj)
        external_matches.setdefault((from_name, ann_key), []).append(val)
        drops.skos_external_matches += 1

  for (from_name, ann_key), values in external_matches.items():
    values.sort()
    entity = entities[from_name]
    if ann_key in entity.annotations:
      existing = entity.annotations[ann_key]
      existing_list = existing if isinstance(existing, list) else [existing]
      merged = sorted(set(existing_list) | set(values))
      entity.annotations[ann_key] = merged if len(merged) > 1 else merged[0]
    else:
      entity.annotations[ann_key] = values if len(values) > 1 else values[0]

  return skos_rels


def _extract_generic_annotations(
    g: Graph,
    elements: dict[str, _ImportedEntity | _ImportedRelationship],
    drops: _DropSummary,
) -> None:
  """Preserve unknown literal-valued predicates as annotations.

  Predicates already handled by specific extraction passes (RDF, RDFS,
  OWL, SKOS) are skipped. This fixes the gap where
  ``docs/ontology/owl-import.md`` claims literal annotations are
  preserved but the code only handled a fixed OWL allowlist.
  """
  # Predicates handled by other passes — skip these.
  _HANDLED_PREFIXES = (
      str(RDF),
      str(RDFS),
      str(OWL),
      str(SKOS),
  )

  from rdflib import Literal

  for elem in elements.values():
    # Collect values per predicate first, then sort for determinism.
    collected: dict[str, list[str]] = {}
    for pred, obj in g.predicate_objects(elem.iri):
      if not isinstance(obj, Literal):
        continue
      pred_str = str(pred)
      if any(pred_str.startswith(pfx) for pfx in _HANDLED_PREFIXES):
        continue
      # Build a ``prefix:local`` key so callers can disambiguate
      # vocabularies with colliding local names (e.g. ``dc:title`` vs.
      # ``dcterms:title``). Fall back to the full IRI when no prefix is
      # bound for the predicate's namespace.
      ann_key = _qname_or_iri(g, pred)
      collected.setdefault(ann_key, []).append(str(obj))

    for ann_key, values in collected.items():
      values.sort()
      if ann_key in elem.annotations:
        existing = elem.annotations[ann_key]
        existing_list = existing if isinstance(existing, list) else [existing]
        merged = sorted(set(existing_list) | set(values))
        elem.annotations[ann_key] = merged if len(merged) > 1 else merged[0]
      else:
        elem.annotations[ann_key] = values if len(values) > 1 else values[0]
      drops.generic_annotations += len(values)


# ---------------------------------------------------------------------------
# YAML emitter
# ---------------------------------------------------------------------------


_YAML_BOOL_NULL = frozenset(
    {
        "true",
        "false",
        "yes",
        "no",
        "on",
        "off",
        "True",
        "False",
        "Yes",
        "No",
        "On",
        "Off",
        "TRUE",
        "FALSE",
        "YES",
        "NO",
        "ON",
        "OFF",
        "null",
        "Null",
        "NULL",
        "~",
    }
)

_YAML_NEEDS_QUOTE_CHARS = frozenset(":{}[],\"'#\n\\*&!%@`?|>-")


def _yaml_scalar(value: str) -> str:
  if not value:
    return '""'
  needs_quote = (
      value in _YAML_BOOL_NULL
      or any(c in _YAML_NEEDS_QUOTE_CHARS for c in value)
      or value[0] in (" ", "\t")
      or value[-1] in (" ", "\t")
  )
  if not needs_quote:
    try:
      parsed = float(value)
      needs_quote = True
    except ValueError:
      pass
  if needs_quote:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'
  return value


def _emit_annotation_value(value: AnnotationValue) -> str:
  if isinstance(value, list):
    items = ", ".join(_yaml_scalar(v) for v in value)
    return f"[{items}]"
  return _yaml_scalar(value)


def _emit_ontology_yaml(
    ontology_name: str,
    entities: dict[str, _ImportedEntity],
    all_relationships: list[_ImportedRelationship],
) -> str:
  lines: list[str] = []
  lines.append(f"ontology: {ontology_name}")

  sorted_entities = sorted(entities.values(), key=lambda e: e.name)
  if sorted_entities:
    lines.append("")
    lines.append("entities:")
    for entity in sorted_entities:
      for comment in entity.comments:
        lines.append(f"  # {comment}")
      lines.append(f"  - name: {entity.name}")

      if entity.abstract:
        lines.append("    abstract: true")

      if entity.description:
        lines.append(f"    description: {_yaml_scalar(entity.description)}")

      if entity.extends_fill_in:
        candidates = ", ".join(entity.extends_candidates)
        lines.append(f"    # multi-parent: rdfs:subClassOf [{candidates}]")
        lines.append("    extends: FILL_IN")
      elif entity.extends:
        lines.append(f"    extends: {entity.extends}")

      if entity.keys_fill_in:
        if entity.keys_excluded:
          lines.append(
              "    # owl:hasKey declared but key properties excluded by"
              " namespace filter"
          )
        else:
          lines.append("    # no owl:hasKey in OWL source")
        if entity.key_candidates:
          candidates = ", ".join(entity.key_candidates)
          lines.append(f"    # candidate data properties: {candidates}")
        lines.append("    keys:")
        lines.append("      primary: [FILL_IN]")
      elif entity.keys_primary is not None:
        keys_str = ", ".join(entity.keys_primary)
        lines.append("    keys:")
        lines.append(f"      primary: [{keys_str}]")

      if entity.properties:
        sorted_props = sorted(entity.properties, key=lambda p: p.name)
        lines.append("    properties:")
        for prop in sorted_props:
          lines.append(f"      - name: {prop.name}")
          lines.append(f"        type: {prop.type}")
          if prop.xsd_annotation:
            lines.append(f"        annotations:")
            lines.append(f"          xsd_type: {prop.xsd_annotation}")

      if entity.synonyms:
        lines.append("    synonyms:")
        for syn in entity.synonyms:
          lines.append(f"      - {_yaml_scalar(syn)}")

      if entity.annotations:
        lines.append("    annotations:")
        for key in sorted(entity.annotations):
          val = _emit_annotation_value(entity.annotations[key])
          lines.append(f"      {key}: {val}")

  # Sort relationships by (name, from, to) for stable output when
  # multiple abstract relationships share a name.
  sorted_rels = sorted(
      all_relationships,
      key=lambda r: (r.name, r.from_entity or "", r.to_entity or ""),
  )
  if sorted_rels:
    lines.append("")
    lines.append("relationships:")
    for rel in sorted_rels:
      for comment in rel.comments:
        lines.append(f"  # {comment}")
      lines.append(f"  - name: {rel.name}")

      if rel.abstract:
        lines.append("    abstract: true")

      if rel.description:
        lines.append(f"    description: {_yaml_scalar(rel.description)}")

      if rel.extends_fill_in:
        candidates = ", ".join(rel.extends_candidates)
        lines.append(f"    # multi-parent: rdfs:subPropertyOf [{candidates}]")
        lines.append("    extends: FILL_IN")
      elif rel.extends:
        lines.append(f"    extends: {rel.extends}")

      if rel.from_fill_in:
        if rel.from_candidates:
          candidates = ", ".join(rel.from_candidates)
          lines.append(f"    # multi-domain: [{candidates}]")
        else:
          lines.append("    # no rdfs:domain in OWL source")
        lines.append("    from: FILL_IN")
      elif rel.from_entity:
        lines.append(f"    from: {rel.from_entity}")

      if rel.to_fill_in:
        if rel.to_candidates:
          candidates = ", ".join(rel.to_candidates)
          lines.append(f"    # multi-range: [{candidates}]")
        else:
          lines.append("    # no rdfs:range in OWL source")
        lines.append("    to: FILL_IN")
      elif rel.to_entity:
        lines.append(f"    to: {rel.to_entity}")

      if rel.cardinality:
        lines.append(f"    cardinality: {rel.cardinality}")

      if rel.synonyms:
        lines.append("    synonyms:")
        for syn in rel.synonyms:
          lines.append(f"      - {_yaml_scalar(syn)}")

      if rel.annotations:
        lines.append("    annotations:")
        for key in sorted(rel.annotations):
          val = _emit_annotation_value(rel.annotations[key])
          lines.append(f"      {key}: {val}")

  return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Drop summary
# ---------------------------------------------------------------------------


def _format_drop_summary(
    drops: _DropSummary,
    entities: dict[str, _ImportedEntity],
) -> str:
  lines: list[str] = []
  if drops.excluded_by_namespace:
    lines.append("Excluded by namespace filter:")
    for kind, count in sorted(drops.excluded_by_namespace.items()):
      lines.append(f"  {kind}: {count}")
  if drops.dropped_features:
    lines.append("Dropped OWL features (preserved as annotations/comments):")
    for kind, count in sorted(drops.dropped_features.items()):
      lines.append(f"  {kind}: {count}")
  if drops.skos_concepts_imported:
    lines.append(
        f"SKOS concepts imported as abstract entities: "
        f"{drops.skos_concepts_imported}"
    )
  if drops.skos_relationships_imported:
    lines.append(
        f"SKOS relationships imported as abstract: "
        f"{drops.skos_relationships_imported}"
    )
  if drops.skos_annotations:
    lines.append(
        f"SKOS predicates mapped to annotations: " f"{drops.skos_annotations}"
    )
  if drops.skos_labels_discarded_by_language:
    lines.append(
        f"Labels in non-selected languages (preserved as "
        f"annotations): {drops.skos_labels_discarded_by_language}"
    )
  if drops.skos_external_matches:
    lines.append(
        f"SKOS match targets outside imported namespaces "
        f"(preserved as annotations): {drops.skos_external_matches}"
    )
  if drops.generic_annotations:
    lines.append(
        f"Generic literal annotations preserved: "
        f"{drops.generic_annotations}"
    )
  # Hint for all-abstract ontologies.
  if entities and all(e.abstract for e in entities.values()):
    lines.append(
        "Note: all entities are abstract (SKOS-only). No concrete "
        "entities are available for binding. Consider representing "
        "the taxonomy as dimension columns instead of entity types."
    )
  return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def import_owl(
    sources: list[str | Path],
    *,
    include_namespaces: list[str],
    ontology_name: str | None = None,
    format: str | None = None,
    language: str = "en",
) -> tuple[str, str]:
  """Import OWL sources into ontology YAML.

  Args:
      sources: Paths to OWL source files (Turtle or RDF/XML).
      include_namespaces: IRI prefixes to include. At least one required.
      ontology_name: Name for the output ontology. Defaults to the first
          namespace's last path segment.
      format: Parser format override (``"turtle"`` or ``"xml"``). If
          ``None``, inferred from file extension.
      language: BCP-47 language tag for label selection (default ``"en"``).
          Labels in the selected language are used for names and synonyms;
          labels in other languages become language-suffixed annotations.

  Returns:
      A ``(yaml_text, drop_summary)`` tuple. The YAML text is a valid
      ontology (modulo ``FILL_IN`` placeholders). The drop summary is
      a human-readable report of excluded and dropped OWL/SKOS features.

  Raises:
      ValueError: If no sources or no namespaces are provided.
  """
  if not sources:
    raise ValueError("At least one OWL source file is required.")
  if not include_namespaces:
    raise ValueError("At least one --include-namespace is required.")

  g = Graph()
  for src in sources:
    src_path = Path(src)
    fmt = format
    if fmt is None:
      ext = src_path.suffix.lower()
      if ext == ".ttl":
        fmt = "turtle"
      elif ext in (".owl", ".rdf", ".xml"):
        fmt = "xml"
      else:
        fmt = "turtle"
    g.parse(str(src_path), format=fmt)

  drops = _DropSummary()

  # Pre-compute IRI→future-entity-name for every in-namespace OWL class
  # and SKOS concept. Passes that resolve references (subClassOf,
  # subPropertyOf, domain, range) use this to emit the correct name
  # even when the target is a SKOS-only concept that will be renamed
  # to ``skos_<name>``.
  iri_to_name: dict[str, str] = _precompute_iri_to_name(g, include_namespaces)

  # Stage 1: OWL entity extraction (extends is resolved through iri_to_name).
  entities = _extract_entities(
      g, include_namespaces, drops, language, iri_to_name=iri_to_name
  )

  # Stage 2: SKOS concept extraction — adds abstract entities for
  # SKOS-only concepts and enriches existing OWL+SKOS entities.
  concept_iri_to_name = _extract_skos_concepts(
      g,
      entities,
      include_namespaces,
      drops,
      language,
  )

  # Reconcile: SKOS concepts may have been promoted to abstract entities
  # under ``skos_<name>``. Fold their IRIs into iri_to_name so the later
  # passes use the same names.
  for iri_str, name in concept_iri_to_name.items():
    iri_to_name[iri_str] = name

  # Stage 3: OWL datatype properties and object properties, with SKOS
  # concepts already visible to endpoint resolution.
  _extract_datatype_properties(
      g,
      entities,
      include_namespaces,
      drops,
      iri_to_name=iri_to_name,
  )
  owl_relationships = _extract_relationships(
      g,
      entities,
      include_namespaces,
      drops,
      language,
      iri_to_name=iri_to_name,
  )

  # Stage 4: SKOS graph-shaped predicates → abstract relationships.
  skos_rels = _extract_skos_relationships(
      g,
      entities,
      iri_to_name,
      include_namespaces,
      drops,
  )

  # Stage 3: Generic literal-annotation pass (covers Dublin Core, etc.).
  _extract_generic_annotations(g, entities, drops)
  # Note: OWL relationships are in a dict; generic annotation pass
  # needs the same interface. SKOS rels are abstract with no IRI-based
  # predicates to extract, so we skip them.
  _extract_generic_annotations(g, owl_relationships, drops)

  # Finalize keys (skip abstract entities).
  _resolve_keys(entities)

  # Propagate abstractness: a concrete relationship with an abstract
  # endpoint cannot round-trip through the loader (concrete relationships
  # require concrete endpoints). Mark such relationships abstract so the
  # emitted ontology is loadable. This primarily catches OWL
  # ObjectProperties whose rdfs:range is a pure SKOS concept.
  for rel in owl_relationships.values():
    if rel.abstract:
      continue
    from_abstract = (
        rel.from_entity in entities and entities[rel.from_entity].abstract
    )
    to_abstract = rel.to_entity in entities and entities[rel.to_entity].abstract
    if from_abstract or to_abstract:
      rel.abstract = True

  # Merge OWL and SKOS relationships into a single list.
  all_relationships: list[_ImportedRelationship] = (
      list(owl_relationships.values()) + skos_rels
  )

  # Name collision checks.
  owl_rel_names = set(owl_relationships)
  skos_rel_names = {r.name for r in skos_rels}
  entity_names = set(entities)

  owl_skos_rel_overlap = owl_rel_names & skos_rel_names
  if owl_skos_rel_overlap:
    names = ", ".join(sorted(owl_skos_rel_overlap))
    raise ValueError(
        f"Name collision between OWL and SKOS relationships: {names}. "
        "OWL relationship names and SKOS relationship names must be "
        "disjoint."
    )

  all_rel_names = owl_rel_names | skos_rel_names
  entity_rel_overlap = entity_names & all_rel_names
  if entity_rel_overlap:
    names = ", ".join(sorted(entity_rel_overlap))
    raise ValueError(
        f"Name collision between entities and relationships: {names}. "
        "Entity and relationship names must be disjoint."
    )

  if ontology_name is None:
    ns = include_namespaces[0].rstrip("#/")
    ontology_name = ns.rsplit("/", 1)[-1]

  yaml_text = _emit_ontology_yaml(ontology_name, entities, all_relationships)
  summary = _format_drop_summary(drops, entities)

  return yaml_text, summary
