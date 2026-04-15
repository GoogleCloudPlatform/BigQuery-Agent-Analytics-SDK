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


# ---------------------------------------------------------------------------
# Core extraction
# ---------------------------------------------------------------------------


def _pick_primary_label(labels) -> tuple[str | None, list[str]]:
  """Pick the best label as description, return rest as synonyms.

  Prefers English-tagged labels (``@en*``), then falls back to
  untagged or other languages.  Within the preferred group, picks
  alphabetically first for determinism.
  """
  en: list[str] = []
  other: list[str] = []
  for label in labels:
    lang = getattr(label, "language", None)
    if lang is not None and lang.startswith("en"):
      en.append(str(label))
    else:
      other.append(str(label))

  en.sort()
  other.sort()
  if en:
    return en[0], en[1:] + other
  if other:
    return other[0], other[1:]
  return None, []


def _extract_labels_and_description(
    g: Graph, iri: URIRef
) -> tuple[str | None, list[str]]:
  raw_labels = list(g.objects(iri, RDFS.label))
  description, synonyms = _pick_primary_label(raw_labels)

  comments: list[str] = []
  for comment in g.objects(iri, RDFS.comment):
    comments.append(str(comment))
  if comments:
    if description:
      description = description + "\n\n" + "\n\n".join(comments)
    else:
      description = "\n\n".join(comments)

  for alt in g.objects(iri, SKOS.altLabel):
    synonyms.append(str(alt))
  for pref in g.objects(iri, SKOS.prefLabel):
    val = str(pref)
    if val not in synonyms and val != description:
      synonyms.append(val)

  synonyms.sort()
  return description, synonyms


def _extract_parents(
    g: Graph, iri: URIRef, predicate: URIRef, namespaces: list[str]
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


def _extract_entities(
    g: Graph, namespaces: list[str], drops: _DropSummary
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

    description, synonyms = _extract_labels_and_description(g, cls)
    extends, extends_fill_in, extends_candidates, parent_anns = (
        _extract_parents(g, cls, RDFS.subClassOf, namespaces)
    )
    keys_primary, keys_fill_in, key_candidates, keys_excluded = _extract_keys(
        g, cls, namespaces
    )

    annotations: dict[str, AnnotationValue] = {}
    comments: list[str] = []
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
) -> dict[str, _ImportedRelationship]:
  relationships: dict[str, _ImportedRelationship] = {}

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

    description, synonyms = _extract_labels_and_description(g, prop)

    domains: list[str] = []
    domain_excluded: list[str] = []
    for domain in g.objects(prop, RDFS.domain):
      if not isinstance(domain, URIRef):
        continue
      if _in_namespace(domain, namespaces):
        domains.append(_local_name(domain))
      else:
        domain_excluded.append(str(domain))

    ranges: list[str] = []
    range_excluded: list[str] = []
    for range_ in g.objects(prop, RDFS.range):
      if not isinstance(range_, URIRef):
        continue
      if _in_namespace(range_, namespaces):
        ranges.append(_local_name(range_))
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
        _extract_parents(g, prop, RDFS.subPropertyOf, namespaces)
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
    if entity.extends is not None or entity.extends_fill_in:
      continue
    prop_names = [p.name for p in entity.properties]
    entity.keys_fill_in = True
    entity.key_candidates = prop_names


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
    relationships: dict[str, _ImportedRelationship],
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

  sorted_rels = sorted(relationships.values(), key=lambda r: r.name)
  if sorted_rels:
    lines.append("")
    lines.append("relationships:")
    for rel in sorted_rels:
      for comment in rel.comments:
        lines.append(f"  # {comment}")
      lines.append(f"  - name: {rel.name}")

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


def _format_drop_summary(drops: _DropSummary) -> str:
  lines: list[str] = []
  if drops.excluded_by_namespace:
    lines.append("Excluded by namespace filter:")
    for kind, count in sorted(drops.excluded_by_namespace.items()):
      lines.append(f"  {kind}: {count}")
  if drops.dropped_features:
    lines.append("Dropped OWL features (preserved as annotations/comments):")
    for kind, count in sorted(drops.dropped_features.items()):
      lines.append(f"  {kind}: {count}")
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
) -> tuple[str, str]:
  """Import OWL sources into ontology YAML.

  Args:
      sources: Paths to OWL source files (Turtle or RDF/XML).
      include_namespaces: IRI prefixes to include. At least one required.
      ontology_name: Name for the output ontology. Defaults to the first
          namespace's last path segment.
      format: Parser format override (``"turtle"`` or ``"xml"``). If
          ``None``, inferred from file extension.

  Returns:
      A ``(yaml_text, drop_summary)`` tuple. The YAML text is a valid
      ontology (modulo ``FILL_IN`` placeholders). The drop summary is
      a human-readable report of excluded and dropped OWL features.

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

  entities = _extract_entities(g, include_namespaces, drops)
  _extract_datatype_properties(g, entities, include_namespaces, drops)
  relationships = _extract_relationships(g, entities, include_namespaces, drops)
  _resolve_keys(entities)

  overlap = set(entities) & set(relationships)
  if overlap:
    names = ", ".join(sorted(overlap))
    raise ValueError(
        f"Name collision between entities and relationships: {names}. "
        "Entity and relationship names must be disjoint."
    )

  if ontology_name is None:
    ns = include_namespaces[0].rstrip("#/")
    ontology_name = ns.rsplit("/", 1)[-1]

  yaml_text = _emit_ontology_yaml(ontology_name, entities, relationships)
  summary = _format_drop_summary(drops)

  return yaml_text, summary
