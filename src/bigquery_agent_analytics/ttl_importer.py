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

"""Two-phase OWL/Turtle importer for V5 Context Graph.

Phase 1 (``ttl_import``) parses an OWL/Turtle file via ``rdflib``,
maps classes, datatype properties, and object properties to the
``GraphSpec`` YAML format, and emits a ``*.import.yaml`` artifact
with ``FILL_IN`` placeholders for anything that requires human
review.

Phase 2 (``ttl_resolve``) reads the import artifact, substitutes
``FILL_IN`` placeholders with user-supplied defaults, strips the
``ontology_import:`` metadata block, validates the result against
``load_graph_spec_from_string``, and returns clean GraphSpec YAML.

``rdflib`` is an optional dependency: a clear error is raised when
``ttl_import`` is called without it installed.
"""

from __future__ import annotations

import dataclasses
import datetime
import logging
from pathlib import Path
import re
from typing import Any, Optional

import yaml

from bigquery_agent_analytics.ontology_models import load_graph_spec_from_string

logger = logging.getLogger('bigquery_agent_analytics.' + __name__)

# ------------------------------------------------------------------ #
# Optional rdflib import                                               #
# ------------------------------------------------------------------ #

_RDFLIB_AVAILABLE = False
try:
  import rdflib
  from rdflib import OWL
  from rdflib import RDF
  from rdflib import RDFS
  from rdflib import XSD
  from rdflib.namespace import SKOS

  _RDFLIB_AVAILABLE = True
except ImportError:
  rdflib = None  # type: ignore[assignment]

_FILL_IN = 'FILL_IN'


# ------------------------------------------------------------------ #
# Report data classes                                                  #
# ------------------------------------------------------------------ #


@dataclasses.dataclass
class PlaceholderInfo:
  """Describes a FILL_IN placeholder inserted into the import YAML."""

  location: str
  reason: str


@dataclasses.dataclass
class TypeWarning:
  """Records an XSD-to-runtime type mapping that lost precision."""

  owl_type: str
  mapped_type: str
  property_name: str
  entity_name: str


@dataclasses.dataclass
class DropInfo:
  """Records an OWL construct that could not be mapped."""

  construct: str
  entity_name: str
  detail: str


@dataclasses.dataclass
class ImportReport:
  """Summary of what was imported and what was skipped/warned."""

  classes_mapped: int = 0
  properties_mapped: int = 0
  relationships_mapped: int = 0
  classes_excluded: dict[str, int] = dataclasses.field(default_factory=dict)
  properties_excluded: dict[str, int] = dataclasses.field(default_factory=dict)
  placeholders: list[PlaceholderInfo] = dataclasses.field(default_factory=list)
  type_warnings: list[TypeWarning] = dataclasses.field(default_factory=list)
  drops: list[DropInfo] = dataclasses.field(default_factory=list)


@dataclasses.dataclass
class TTLImportResult:
  """Return value of ``ttl_import``."""

  yaml_text: str
  report: ImportReport


# ------------------------------------------------------------------ #
# XSD -> runtime type mapping                                          #
# ------------------------------------------------------------------ #

# Map of XSD local name -> (runtime_type, optional_warning_reason).
_XSD_TYPE_MAP: dict[str, tuple[str, Optional[str]]] = {
    # Strings
    'string': ('string', None),
    'normalizedString': ('string', None),
    'token': ('string', None),
    'anyURI': ('string', None),
    # Integers
    'integer': ('int64', None),
    'int': ('int64', None),
    'long': ('int64', None),
    'short': ('int64', None),
    'byte': ('int64', None),
    'nonNegativeInteger': ('int64', None),
    'nonPositiveInteger': ('int64', None),
    'positiveInteger': ('int64', None),
    'negativeInteger': ('int64', None),
    'unsignedLong': ('int64', None),
    'unsignedInt': ('int64', None),
    'unsignedShort': ('int64', None),
    'unsignedByte': ('int64', None),
    # Floating point
    'double': ('double', None),
    'float': ('double', None),
    # Decimal (narrowed)
    'decimal': ('double', 'narrowed from numeric (xsd:decimal)'),
    # Boolean
    'boolean': ('boolean', None),
    # Date/time
    'date': ('date', None),
    'dateTime': ('timestamp', 'narrowed from dateTime to timestamp'),
    'dateTimeStamp': ('timestamp', None),
    'time': ('string', 'unsupported xsd:time, narrowed to string'),
    # Binary
    'hexBinary': ('bytes', None),
    'base64Binary': ('bytes', None),
}


def _map_xsd_type(
    xsd_uri: str,
    property_name: str,
    entity_name: str,
    report: ImportReport,
) -> str:
  """Map an XSD datatype URI to a runtime type string."""
  # Extract local name from the URI.
  local_name = str(xsd_uri).rsplit('#', maxsplit=1)[-1]
  local_name = local_name.rsplit('/', maxsplit=1)[-1]

  # Handle rdf:JSON specifically.
  if str(xsd_uri).endswith('JSON') or local_name == 'JSON':
    report.type_warnings.append(
        TypeWarning(
            owl_type=str(xsd_uri),
            mapped_type='string',
            property_name=property_name,
            entity_name=entity_name,
        )
    )
    return 'string'

  entry = _XSD_TYPE_MAP.get(local_name)
  if entry is not None:
    runtime_type, warning_reason = entry
    if warning_reason:
      report.type_warnings.append(
          TypeWarning(
              owl_type=str(xsd_uri),
              mapped_type=runtime_type,
              property_name=property_name,
              entity_name=entity_name,
          )
      )
    return runtime_type

  # Unknown type -> string with warning.
  report.type_warnings.append(
      TypeWarning(
          owl_type=str(xsd_uri),
          mapped_type='string',
          property_name=property_name,
          entity_name=entity_name,
      )
  )
  return 'string'


# ------------------------------------------------------------------ #
# Helpers                                                              #
# ------------------------------------------------------------------ #


def _require_rdflib() -> None:
  """Raise a clear error if rdflib is not installed."""
  if not _RDFLIB_AVAILABLE:
    raise ImportError(
        'rdflib is required for OWL/Turtle import but is not installed. '
        'Install it with: pip install rdflib'
    )


def _local_name(uri: Any) -> str:
  """Extract the local (fragment / last path segment) name from a URI."""
  s = str(uri)
  if '#' in s:
    return s.rsplit('#', maxsplit=1)[-1]
  return s.rsplit('/', maxsplit=1)[-1]


def _matches_namespace(
    uri: Any,
    include_namespaces: list[str],
) -> bool:
  """Return True if the URI starts with any of the given prefixes."""
  s = str(uri)
  return any(s.startswith(ns) for ns in include_namespaces)


def _pluralize(name: str) -> str:
  """Naive English pluralization for table names."""
  if name.endswith('s') or name.endswith('x') or name.endswith('z'):
    return name + 'es'
  if name.endswith('sh') or name.endswith('ch'):
    return name + 'es'
  if name.endswith('y') and len(name) > 1 and name[-2] not in 'aeiou':
    return name[:-1] + 'ies'
  return name + 's'


def _to_table_name(name: str) -> str:
  """Convert a CamelCase or mixed name to a lowercased plural table name."""
  # Insert underscores before uppercase runs: CamelCase -> Camel_Case.
  result = re.sub(r'([a-z0-9])([A-Z])', r'\1_\2', name)
  result = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1_\2', result)
  result = result.lower()
  return _pluralize(result)


# ------------------------------------------------------------------ #
# Phase 1: ttl_import                                                  #
# ------------------------------------------------------------------ #


def ttl_import(
    ttl_path: str,
    include_namespaces: list[str],
    dataset_template: str = '{{ env }}',
) -> TTLImportResult:
  """Import an OWL/Turtle file into a GraphSpec-compatible YAML artifact.

  Args:
      ttl_path: Path to the ``.ttl`` or ``.owl`` file.
      include_namespaces: IRI prefix strings. Only classes and
          properties whose IRI starts with one of these prefixes
          are imported.
      dataset_template: Template for the dataset portion of
          BigQuery table bindings. Defaults to ``{{ env }}``.

  Returns:
      A ``TTLImportResult`` with the generated YAML text and a
      detailed ``ImportReport``.

  Raises:
      ImportError: If ``rdflib`` is not installed.
      FileNotFoundError: If ``ttl_path`` does not exist.
  """
  _require_rdflib()

  path = Path(ttl_path)
  if not path.exists():
    raise FileNotFoundError(f'TTL file not found: {ttl_path}')

  g = rdflib.Graph()
  g.parse(str(path))

  report = ImportReport()

  # ---- Collect classes ---- #
  class_uris: list[Any] = []
  for subj in g.subjects(RDF.type, OWL.Class):
    if not _matches_namespace(subj, include_namespaces):
      ns = str(subj).rsplit('#', maxsplit=1)[0]
      ns = ns.rsplit('/', maxsplit=1)[0]
      report.classes_excluded[ns] = report.classes_excluded.get(ns, 0) + 1
      continue
    class_uris.append(subj)

  # Sort for deterministic output.
  class_uris.sort(key=lambda u: str(u))

  # Build a map of class URI -> entity info.
  class_name_map: dict[str, str] = {}  # URI string -> entity name
  entity_primary_keys: dict[str, list[str]] = {}  # entity name -> keys

  entities: list[dict[str, Any]] = []
  for cls_uri in class_uris:
    name = _local_name(cls_uri)
    class_name_map[str(cls_uri)] = name

    description = ''
    for label_obj in g.objects(cls_uri, RDFS.label):
      description = str(label_obj)
      break

    # Collect skos:altLabel (tracked but not used in GraphSpec).
    alt_labels: list[str] = []
    for alt in g.objects(cls_uri, SKOS.altLabel):
      alt_labels.append(str(alt))
    if alt_labels:
      logger.debug(
          'Class %s has altLabels: %s (not used in GraphSpec)',
          name,
          alt_labels,
      )

    # Inheritance: rdfs:subClassOf.
    parents: list[str] = []
    for parent_uri in g.objects(cls_uri, RDFS.subClassOf):
      # Skip blank nodes (OWL restrictions).
      if isinstance(parent_uri, rdflib.BNode):
        report.drops.append(
            DropInfo(
                construct='owl:Restriction (via rdfs:subClassOf bnode)',
                entity_name=name,
                detail='Blank-node restriction on subClassOf dropped.',
            )
        )
        continue
      parent_name = _local_name(parent_uri)
      if _matches_namespace(parent_uri, include_namespaces):
        parents.append(parent_name)

    extends: Optional[str] = None
    if len(parents) == 1:
      extends = parents[0]
    elif len(parents) > 1:
      extends = _FILL_IN
      report.placeholders.append(
          PlaceholderInfo(
              location=f'entities[{name}].extends',
              reason=(
                  f'Multiple parents found: {parents}. '
                  f'GraphSpec supports single inheritance only.'
              ),
          )
      )

    # owl:hasKey -> primary key.
    primary_keys: list[str] = []
    for key_list in g.objects(cls_uri, OWL.hasKey):
      if isinstance(key_list, rdflib.BNode):
        # It is an RDF list; walk it.
        for item in g.items(key_list):
          primary_keys.append(_local_name(item))

    has_key_placeholder = False
    if not primary_keys:
      primary_keys = [_FILL_IN]
      has_key_placeholder = True
      report.placeholders.append(
          PlaceholderInfo(
              location=f'entities[{name}].keys.primary',
              reason='No owl:hasKey found; primary key must be specified.',
          )
      )

    entity_primary_keys[name] = primary_keys

    # Collect datatype properties for this class.
    properties: list[dict[str, Any]] = []
    for prop_uri in g.subjects(RDF.type, OWL.DatatypeProperty):
      if not _matches_namespace(prop_uri, include_namespaces):
        continue
      # Check if this property has rdfs:domain pointing to this class.
      domains = list(g.objects(prop_uri, RDFS.domain))
      if not domains or cls_uri not in domains:
        continue

      prop_name = _local_name(prop_uri)
      # Determine type from rdfs:range.
      prop_type = 'string'
      ranges = list(g.objects(prop_uri, RDFS.range))
      if ranges:
        prop_type = _map_xsd_type(str(ranges[0]), prop_name, name, report)

      prop_desc = ''
      for label_obj in g.objects(prop_uri, RDFS.label):
        prop_desc = str(label_obj)
        break

      prop_entry: dict[str, Any] = {
          'name': prop_name,
          'type': prop_type,
      }
      if prop_desc:
        prop_entry['description'] = prop_desc

      properties.append(prop_entry)
      report.properties_mapped += 1

    # If we have primary keys that are not FILL_IN, ensure each key
    # column appears in the properties list.
    if not has_key_placeholder:
      existing_prop_names = {p['name'] for p in properties}
      for key_col in primary_keys:
        if key_col not in existing_prop_names:
          properties.insert(
              0,
              {'name': key_col, 'type': 'string'},
          )

    table_name = _to_table_name(name)
    entity: dict[str, Any] = {
        'name': name,
    }
    if description:
      entity['description'] = description
    if extends is not None:
      entity['extends'] = extends
    entity['binding'] = {
        'source': f'{dataset_template}.{table_name}',
    }
    entity['keys'] = {'primary': primary_keys}
    if properties:
      entity['properties'] = properties

    entities.append(entity)
    report.classes_mapped += 1

  # ---- Collect object properties (relationships) ---- #
  relationships: list[dict[str, Any]] = []
  for prop_uri in g.subjects(RDF.type, OWL.ObjectProperty):
    if not _matches_namespace(prop_uri, include_namespaces):
      ns = str(prop_uri).rsplit('#', maxsplit=1)[0]
      ns = ns.rsplit('/', maxsplit=1)[0]
      report.properties_excluded[ns] = report.properties_excluded.get(ns, 0) + 1
      continue

    rel_name = _local_name(prop_uri)

    # domain -> from_entity, range -> to_entity.
    domains = list(g.objects(prop_uri, RDFS.domain))
    ranges = list(g.objects(prop_uri, RDFS.range))

    from_entity: Optional[str] = None
    to_entity: Optional[str] = None

    if domains and str(domains[0]) in class_name_map:
      from_entity = class_name_map[str(domains[0])]
    if ranges and str(ranges[0]) in class_name_map:
      to_entity = class_name_map[str(ranges[0])]

    if from_entity is None:
      from_entity = _FILL_IN
      report.placeholders.append(
          PlaceholderInfo(
              location=f'relationships[{rel_name}].from_entity',
              reason='No rdfs:domain or domain not in included classes.',
          )
      )
    if to_entity is None:
      to_entity = _FILL_IN
      report.placeholders.append(
          PlaceholderInfo(
              location=f'relationships[{rel_name}].to_entity',
              reason='No rdfs:range or range not in included classes.',
          )
      )

    rel_desc = ''
    for label_obj in g.objects(prop_uri, RDFS.label):
      rel_desc = str(label_obj)
      break

    table_name = _to_table_name(rel_name)

    # Derive from_columns / to_columns from entity primary keys.
    from_columns: Optional[list[str]] = None
    to_columns: Optional[list[str]] = None
    if from_entity != _FILL_IN and from_entity in entity_primary_keys:
      from_columns = list(entity_primary_keys[from_entity])
    if to_entity != _FILL_IN and to_entity in entity_primary_keys:
      to_columns = list(entity_primary_keys[to_entity])

    binding: dict[str, Any] = {
        'source': f'{dataset_template}.{table_name}',
    }
    if from_columns is not None:
      binding['from_columns'] = from_columns
    if to_columns is not None:
      binding['to_columns'] = to_columns

    rel: dict[str, Any] = {
        'name': rel_name,
    }
    if rel_desc:
      rel['description'] = rel_desc
    rel['from_entity'] = from_entity
    rel['to_entity'] = to_entity
    rel['binding'] = binding

    relationships.append(rel)
    report.relationships_mapped += 1

  # ---- Track unmappable OWL constructs ---- #
  # owl:equivalentClass
  for subj, _, obj in g.triples((None, OWL.equivalentClass, None)):
    subj_name = _local_name(subj)
    if str(subj) in class_name_map:
      report.drops.append(
          DropInfo(
              construct='owl:equivalentClass',
              entity_name=subj_name,
              detail=f'Equivalent to {_local_name(obj)}; dropped.',
          )
      )

  # owl:disjointWith
  for subj, _, obj in g.triples((None, OWL.disjointWith, None)):
    subj_name = _local_name(subj)
    if str(subj) in class_name_map:
      report.drops.append(
          DropInfo(
              construct='owl:disjointWith',
              entity_name=subj_name,
              detail=f'Disjoint with {_local_name(obj)}; dropped.',
          )
      )

  # owl:unionOf, owl:intersectionOf
  for predicate_name, predicate in [
      ('owl:unionOf', OWL.unionOf),
      ('owl:intersectionOf', OWL.intersectionOf),
  ]:
    for subj, _, _ in g.triples((None, predicate, None)):
      subj_name = _local_name(subj)
      if str(subj) in class_name_map:
        report.drops.append(
            DropInfo(
                construct=predicate_name,
                entity_name=subj_name,
                detail=f'{predicate_name} expression dropped.',
            )
        )

  # ---- Build the YAML output ---- #
  graph_name_parts = []
  for ns in include_namespaces:
    part = ns.rstrip('/').rstrip('#').rsplit('/', maxsplit=1)[-1]
    graph_name_parts.append(part)
  graph_name = '_'.join(graph_name_parts) + '_imported'

  graph_data: dict[str, Any] = {
      'name': graph_name,
  }
  if entities:
    graph_data['entities'] = entities
  if relationships:
    graph_data['relationships'] = relationships

  ontology_import_meta: dict[str, Any] = {
      'status': 'unresolved',
      'source_file': str(path.resolve()),
      'import_timestamp': datetime.datetime.now(
          tz=datetime.timezone.utc
      ).isoformat(),
      'placeholders_remaining': len(report.placeholders),
  }

  # Build the full document dict: ontology_import on top, then graph.
  full_doc: dict[str, Any] = {
      'ontology_import': ontology_import_meta,
      'graph': graph_data,
  }

  yaml_text = yaml.dump(
      full_doc,
      default_flow_style=False,
      sort_keys=False,
      allow_unicode=True,
      width=120,
  )

  return TTLImportResult(yaml_text=yaml_text, report=report)


# ------------------------------------------------------------------ #
# Phase 2: ttl_resolve                                                 #
# ------------------------------------------------------------------ #


def ttl_resolve(
    import_yaml_path: str,
    defaults: Optional[dict[str, str]] = None,
) -> str:
  """Resolve FILL_IN placeholders and produce clean GraphSpec YAML.

  Args:
      import_yaml_path: Path to the ``*.import.yaml`` artifact
          produced by ``ttl_import``.
      defaults: Optional mapping of dotted-path locations to
          replacement values. Paths use the same format as
          ``PlaceholderInfo.location``, e.g.
          ``'entities[Foo].keys.primary'`` maps to a list value
          like ``'["foo_id"]'``.  Scalar values are used as-is;
          list values should be passed as Python lists.

  Returns:
      A clean GraphSpec YAML string (without the
      ``ontology_import:`` metadata block) that passes
      ``load_graph_spec_from_string`` validation.

  Raises:
      FileNotFoundError: If the import YAML does not exist.
      ValueError: If any ``FILL_IN`` placeholders remain after
          applying defaults, or if the resulting YAML fails
          validation.
  """
  path = Path(import_yaml_path)
  if not path.exists():
    raise FileNotFoundError(f'Import YAML not found: {import_yaml_path}')

  raw = path.read_text(encoding='utf-8')
  data = yaml.safe_load(raw)

  # Strip the ontology_import metadata.
  data.pop('ontology_import', None)

  if defaults:
    _apply_defaults(data, defaults)

  # Serialize back to YAML.
  yaml_text = yaml.dump(
      data,
      default_flow_style=False,
      sort_keys=False,
      allow_unicode=True,
      width=120,
  )

  # Check for remaining FILL_IN placeholders.
  remaining = _find_fill_in_locations(data)
  if remaining:
    locations_str = ', '.join(remaining)
    raise ValueError(
        f'Unresolved FILL_IN placeholders remain at: {locations_str}. '
        f'Provide values via the defaults parameter.'
    )

  # Validate against GraphSpec.
  try:
    load_graph_spec_from_string(yaml_text)
  except Exception as exc:
    raise ValueError(
        f'Resolved YAML failed GraphSpec validation: {exc}'
    ) from exc

  return yaml_text


def _apply_defaults(
    data: dict[str, Any],
    defaults: dict[str, Any],
) -> None:
  """Apply default values to FILL_IN placeholders in the parsed YAML.

  Paths follow the ``PlaceholderInfo.location`` format:
  - ``entities[Name].extends`` -> scalar replacement
  - ``entities[Name].keys.primary`` -> list replacement
  - ``relationships[Name].from_entity`` -> scalar replacement
  """
  graph = data.get('graph', {})
  entities_list: list[dict] = graph.get('entities', [])
  rels_list: list[dict] = graph.get('relationships', [])

  entity_by_name = {e['name']: e for e in entities_list}
  rel_by_name = {r['name']: r for r in rels_list}

  for location, value in defaults.items():
    _apply_single_default(location, value, entity_by_name, rel_by_name)


def _apply_single_default(
    location: str,
    value: Any,
    entity_by_name: dict[str, dict],
    rel_by_name: dict[str, dict],
) -> None:
  """Apply a single default value at the given dotted path."""
  # Parse patterns like:
  #   entities[Foo].extends
  #   entities[Foo].keys.primary
  #   relationships[Bar].from_entity
  entity_match = re.match(r'entities\[([^\]]+)\]\.(.+)', location)
  rel_match = re.match(r'relationships\[([^\]]+)\]\.(.+)', location)

  if entity_match:
    name = entity_match.group(1)
    field_path = entity_match.group(2)
    target = entity_by_name.get(name)
    if target is None:
      logger.warning(
          'Default location %r: entity %r not found.', location, name
      )
      return
    _set_nested(target, field_path, value)
  elif rel_match:
    name = rel_match.group(1)
    field_path = rel_match.group(2)
    target = rel_by_name.get(name)
    if target is None:
      logger.warning(
          'Default location %r: relationship %r not found.',
          location,
          name,
      )
      return
    _set_nested(target, field_path, value)
  else:
    logger.warning('Default location %r: unrecognized path format.', location)


def _set_nested(target: dict[str, Any], dotted_path: str, value: Any) -> None:
  """Set a value at a dotted path within a nested dict."""
  parts = dotted_path.split('.')
  for part in parts[:-1]:
    if part not in target or not isinstance(target[part], dict):
      target[part] = {}
    target = target[part]
  target[parts[-1]] = value


def _find_fill_in_locations(
    data: Any,
    prefix: str = '',
) -> list[str]:
  """Recursively find all FILL_IN string values in a data structure."""
  results: list[str] = []
  if isinstance(data, dict):
    for key, val in data.items():
      path = f'{prefix}.{key}' if prefix else key
      results.extend(_find_fill_in_locations(val, path))
  elif isinstance(data, list):
    for i, val in enumerate(data):
      path = f'{prefix}[{i}]'
      results.extend(_find_fill_in_locations(val, path))
  elif data == _FILL_IN:
    results.append(prefix)
  return results


# ------------------------------------------------------------------ #
# Upstream OWL importer bridge                                         #
# ------------------------------------------------------------------ #


def import_owl_to_ontology(
    sources: list[str],
    include_namespaces: list[str],
    ontology_name: Optional[str] = None,
) -> tuple[str, str]:
  """Import OWL sources via the upstream ``bigquery_ontology`` importer.

  This produces a ``*.ontology.yaml`` (not a combined GraphSpec YAML).
  The output can be loaded with ``load_ontology_from_string()`` once
  FILL_IN placeholders are resolved, then combined with a binding via
  ``load_from_ontology_binding()`` to produce a GraphSpec.

  For the legacy two-phase flow that produces GraphSpec directly, use
  ``ttl_import()`` + ``ttl_resolve()`` instead.

  Args:
      sources: Paths to OWL source files (Turtle or RDF/XML).
      include_namespaces: IRI prefixes to include.
      ontology_name: Name for the output ontology. If ``None``,
          derived from the first namespace.

  Returns:
      A ``(ontology_yaml, drop_summary)`` tuple. The YAML is a valid
      ``*.ontology.yaml`` (modulo FILL_IN placeholders). The drop
      summary is a human-readable report of excluded OWL features.

  Raises:
      ImportError: If ``bigquery_ontology`` or ``rdflib`` is not
          installed.
      ValueError: If no sources or no namespaces are provided.
  """
  from bigquery_ontology.owl_importer import import_owl

  return import_owl(
      sources=sources,
      include_namespaces=include_namespaces,
      ontology_name=ontology_name,
  )


def import_owl_to_graph_spec(
    sources: list[str],
    include_namespaces: list[str],
    project_id: str,
    dataset_id: str,
    ontology_name: Optional[str] = None,
    lineage_config: Optional[dict] = None,
) -> tuple['GraphSpec', str]:
  """Import OWL sources and produce an SDK GraphSpec.

  End-to-end bridge: parses OWL via the upstream importer, scaffolds
  a binding, and converts to a ``GraphSpec`` via the runtime adapter.

  The output GraphSpec is runtime-ready only if the upstream output
  has no FILL_IN placeholders. If placeholders remain, this function
  raises ``ValueError`` with a message listing the unresolved sites.

  Args:
      sources: Paths to OWL source files.
      include_namespaces: IRI prefixes to include.
      project_id: BigQuery project for the scaffolded binding.
      dataset_id: BigQuery dataset for the scaffolded binding.
      ontology_name: Name for the output ontology.
      lineage_config: Optional lineage configuration for the adapter.

  Returns:
      A ``(GraphSpec, drop_summary)`` tuple.

  Raises:
      ImportError: If required packages are not installed.
      ValueError: If FILL_IN placeholders remain or conversion fails.
  """
  from bigquery_ontology import load_ontology_from_string
  from bigquery_ontology import scaffold

  from .runtime_spec import graph_spec_from_ontology_binding

  ontology_yaml, drop_summary = import_owl_to_ontology(
      sources=sources,
      include_namespaces=include_namespaces,
      ontology_name=ontology_name,
  )

  # Check for FILL_IN before attempting to load.
  if _FILL_IN in ontology_yaml:
    # Count and list placeholder locations.
    fill_in_lines = [
        f'  line {i + 1}: {line.strip()}'
        for i, line in enumerate(ontology_yaml.splitlines())
        if _FILL_IN in line
    ]
    raise ValueError(
        f'Upstream OWL import produced {len(fill_in_lines)} unresolved '
        f'FILL_IN placeholder(s). Resolve them in the ontology YAML '
        f'before converting to GraphSpec:\n' + '\n'.join(fill_in_lines)
    )

  ontology = load_ontology_from_string(ontology_yaml)

  # Reject ontologies with extends — upstream scaffold (v0) does not
  # support inheritance. Fail here with a clear message rather than
  # leaking a scaffold implementation error.
  entities_with_extends = [e.name for e in ontology.entities if e.extends]
  rels_with_extends = [r.name for r in ontology.relationships if r.extends]
  if entities_with_extends or rels_with_extends:
    parts = []
    if entities_with_extends:
      parts.append(f'entities: {entities_with_extends}')
    if rels_with_extends:
      parts.append(f'relationships: {rels_with_extends}')
    raise ValueError(
        'import_owl_to_graph_spec() requires a flat ontology (no '
        'extends). The imported ontology uses extends on '
        + '; '.join(parts)
        + '. Use import_owl_to_ontology() instead and author the '
        'binding manually.'
    )

  # Scaffold a binding from the ontology.
  ddl_text, binding_yaml = scaffold(
      ontology=ontology,
      project=project_id,
      dataset=dataset_id,
  )

  from bigquery_ontology import load_binding_from_string

  binding = load_binding_from_string(binding_yaml, ontology=ontology)

  spec = graph_spec_from_ontology_binding(
      ontology, binding, lineage_config=lineage_config
  )

  from .ontology_models import _resolve_inheritance
  from .ontology_models import _validate_graph_spec

  _resolve_inheritance(spec)
  _validate_graph_spec(spec)

  return spec, drop_summary
