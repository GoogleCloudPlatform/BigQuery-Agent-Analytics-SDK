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

"""Import Knowledge Catalog (Dataplex) Business Glossaries into ontology YAML.

``gm import-dataplex-glossary`` reads one Business Glossary through the
read-only ``dataplex.googleapis.com`` v1 API and emits a validated,
deterministic, SKOS-aligned ``ontology.yaml``. The module has two layers:

* :func:`map_dataplex_glossary` — a pure function (no network, no SDK, no
  I/O) that maps a glossary plus its categories and terms to ontology
  YAML and a human-readable summary.
* :func:`import_dataplex_glossary` — a thin wrapper that fetches those
  records with ``GetGlossary``, ``ListGlossaryCategories`` and
  ``ListGlossaryTerms`` and hands them to :func:`map_dataplex_glossary`.
  It checks that ``GetGlossary`` returned the requested glossary (a
  response without a resource name is rejected) before listing anything,
  and fails instead of emitting a partial ontology when a list page
  reports unreachable locations or can't be read.

Mapping:

* The glossary becomes the ontology root (a SKOS concept scheme): its
  description, plus ``dataplex:glossary`` (the canonical resource name)
  and ``dataplex:uid`` annotations.
* Every category and term becomes an abstract entity with
  ``skos:prefLabel`` (display name), ``skos:notation`` (raw resource ID),
  ``dataplex:resource`` (resource name) and ``skos:inScheme`` annotations.
  Children of the glossary are also ``skos:topConceptOf`` it; children of
  a category get an abstract ``skos_broader`` relationship to it; any
  other parent is kept as ``dataplex:orphan_parent`` and reported.

Scope: every category and term must name the glossary's own project,
location and glossary ID, or the import fails; a parent anywhere else is
an orphan. Projects must match exactly. A project ID and a project number
count as one project only when the pair is verified:
:func:`import_dataplex_glossary` learns it when ``GetGlossary`` answers a
request by one form with a name in the other, and
:func:`map_dataplex_glossary` takes it as ``project_alias``.

Entity names come from the full resource identity: ``category_<id>`` for
``.../categories/<id>`` and ``term_<id>`` for ``.../terms/<id>``, with
every character outside ``[A-Za-z0-9_]`` replaced by ``_``. The kind
prefix keeps the two ID spaces apart; resources of the same kind whose
IDs normalize to the same name each get a ``_<sha256(id)[:8]>`` suffix,
lengthened (up to the full digest, then with a ``_<n>`` counter) only
when that name is already taken.

``google-cloud-dataplex`` is an optional dependency (install
``bigquery-agent-analytics[dataplex]``). It is imported lazily, and only
when :func:`import_dataplex_glossary` has to build its own client.
"""

from __future__ import annotations

import dataclasses
import hashlib
import importlib
import json
import re
from typing import Any, Callable, Iterable, Mapping, Protocol

import yaml

from .ontology_loader import load_ontology_from_string

__all__ = [
    "BusinessGlossaryClientProtocol",
    "DataplexImportError",
    "GlossaryCategoryProtocol",
    "GlossaryProtocol",
    "GlossaryTermProtocol",
    "MissingDataplexDependencyError",
    "import_dataplex_glossary",
    "map_dataplex_glossary",
    "validate_import_arguments",
]


# --------------------------------------------------------------------- #
# Errors and protocols                                                   #
# --------------------------------------------------------------------- #


class MissingDataplexDependencyError(ImportError):
  """``google-cloud-dataplex`` is not installed (or is too old)."""


class DataplexImportError(RuntimeError):
  """A Dataplex API, transport, or authentication call failed."""


class GlossaryProtocol(Protocol):
  """Fields read from a ``dataplex_v1.Glossary``."""

  name: str
  uid: str
  description: str


class GlossaryCategoryProtocol(Protocol):
  """Fields read from a ``dataplex_v1.GlossaryCategory``."""

  name: str
  parent: str
  display_name: str
  description: str


class GlossaryTermProtocol(Protocol):
  """Fields read from a ``dataplex_v1.GlossaryTerm``."""

  name: str
  parent: str
  display_name: str
  description: str


class BusinessGlossaryClientProtocol(Protocol):
  """The subset of ``dataplex_v1.BusinessGlossaryServiceClient`` used here.

  Each method is called with a single ``request`` mapping, which the real
  GAPIC client accepts in place of a request message. The list methods
  return an iterable (the real client returns an auto-paginating pager).
  """

  def get_glossary(self, request: Any = None, **kwargs: Any) -> Any:
    ...

  def list_glossary_categories(
      self, request: Any = None, **kwargs: Any
  ) -> Iterable[Any]:
    ...

  def list_glossary_terms(
      self, request: Any = None, **kwargs: Any
  ) -> Iterable[Any]:
    ...


# --------------------------------------------------------------------- #
# Resource names and identifiers                                         #
# --------------------------------------------------------------------- #

_INSTALL_HINT = "pip install 'bigquery-agent-analytics[dataplex]'"
_MIN_DATAPLEX_VERSION = "2.12.0"

# Resource-name segments. A project is a project number, or a project ID:
# lowercase letters, digits and hyphens, starting with a letter and not
# ending with a hyphen, optionally after a legacy domain prefix such as
# ``example.com:``. A location is lowercase letters, digits and hyphens,
# starting with a letter and not ending with a hyphen, such as ``global``
# or ``us-central1``. Glossary, category and term IDs only exclude ``/``
# and whitespace, so unusual IDs still reach sanitization.
_PROJECT_ID_PART = r"[a-z](?:[a-z0-9-]*[a-z0-9])?"
_DOMAIN_LABEL = r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?"
_PROJECT_SEGMENT = (
    rf"(?:[0-9]+|(?:{_DOMAIN_LABEL}(?:\.{_DOMAIN_LABEL})+:)?"
    rf"{_PROJECT_ID_PART})"
)
_LOCATION_SEGMENT = r"[a-z](?:[a-z0-9-]*[a-z0-9])?"
_ID_SEGMENT = r"[^/\s]+"
_GLOSSARY_PREFIX = (
    rf"projects/(?P<project>{_PROJECT_SEGMENT})"
    rf"/locations/(?P<location>{_LOCATION_SEGMENT})"
    rf"/glossaries/(?P<glossary_id>{_ID_SEGMENT})"
)
# All patterns are applied with ``fullmatch`` so a trailing newline can't
# slip through the way it does with ``re.match`` and ``$``.
_PROJECT_RE = re.compile(_PROJECT_SEGMENT)
_GLOSSARY_NAME_RE = re.compile(_GLOSSARY_PREFIX)
_CATEGORY_NAME_RE = re.compile(
    rf"{_GLOSSARY_PREFIX}/categories/(?P<leaf_id>{_ID_SEGMENT})"
)
_TERM_NAME_RE = re.compile(
    rf"{_GLOSSARY_PREFIX}/terms/(?P<leaf_id>{_ID_SEGMENT})"
)

_CATEGORY = "category"
_TERM = "term"
_NAME_PATTERNS = {_CATEGORY: _CATEGORY_NAME_RE, _TERM: _TERM_NAME_RE}

_SKOS_BROADER = "skos_broader"
_NON_IDENTIFIER_CHARS_RE = re.compile(r"[^A-Za-z0-9_]")
_HASH_SUFFIX_LENGTH = 8

_PERMISSION_HINT = (
    "Hint: importing a glossary is read-only and needs "
    "roles/dataplex.catalogViewer (or equivalent read permissions) on the "
    "glossary's project."
)
_CREDENTIALS_HINT = (
    "Hint: configure Application Default Credentials, for example with "
    "'gcloud auth application-default login'."
)


@dataclasses.dataclass(frozen=True)
class _GlossaryRef:
  name: str
  project: str
  location: str
  glossary_id: str


def _parse_glossary_name(glossary_name: Any) -> _GlossaryRef:
  match = (
      _GLOSSARY_NAME_RE.fullmatch(glossary_name)
      if isinstance(glossary_name, str)
      else None
  )
  if match is None:
    raise ValueError(
        f"Invalid glossary resource name {glossary_name!r}; expected "
        "projects/<project>/locations/<location>/glossaries/<glossary_id>."
    )
  return _GlossaryRef(
      name=glossary_name,
      project=match["project"],
      location=match["location"],
      glossary_id=match["glossary_id"],
  )


def _is_project_number(project: str) -> bool:
  """Whether a resource-name project segment is a project number."""
  return bool(project) and project.isascii() and project.isdigit()


def _allowed_projects(
    glossary: _GlossaryRef, project_alias: Any
) -> frozenset[str]:
  """The project segments accepted as the glossary's own project.

  Exactly the glossary's project, plus ``project_alias`` when the caller
  has verified that it is the same project under its other form: its
  project ID for a glossary named by project number, or the other way
  round. A project ID always starts with a letter and a project number is
  all digits, so two different IDs, or two different numbers, are never
  one project.

  Raises:
      ValueError: ``project_alias`` is not a project ID or number, or it
          is a different project of the glossary project's own form.
  """
  if project_alias is None:
    return frozenset({glossary.project})
  if not isinstance(project_alias, str) or not _PROJECT_RE.fullmatch(
      project_alias
  ):
    raise ValueError(
        f"project_alias {project_alias!r} is not a project ID or project "
        "number."
    )
  same_form = _is_project_number(project_alias) == _is_project_number(
      glossary.project
  )
  if project_alias != glossary.project and same_form:
    forms = (
        "project numbers"
        if _is_project_number(project_alias)
        else "project IDs"
    )
    raise ValueError(
        f"project_alias {project_alias!r} cannot name the same project as "
        f"{glossary.project!r}: two different {forms} are two projects. "
        "Pass the other form of the glossary's project instead (its number "
        "for a glossary named by ID, or the other way round)."
    )
  return frozenset({glossary.project, project_alias})


def _in_glossary(
    match: re.Match[str], glossary: _GlossaryRef, projects: frozenset[str]
) -> bool:
  """Whether a parsed glossary, category or term name lies in ``glossary``.

  The project must be one of ``projects`` (see :func:`_allowed_projects`),
  and the location and glossary ID must be equal.
  """
  return (
      match["project"] in projects
      and match["location"] == glossary.location
      and match["glossary_id"] == glossary.glossary_id
  )


def _unpaired_other_form(
    match: re.Match[str], glossary: _GlossaryRef, projects: frozenset[str]
) -> bool:
  """Whether ``match`` names the glossary but for an unverifiable project.

  True when the location and glossary ID are equal, no alias is known, and
  one project segment is an ID while the other is a number. The two may be
  the same project, but that can't be checked offline.
  """
  return (
      len(projects) == 1
      and match["location"] == glossary.location
      and match["glossary_id"] == glossary.glossary_id
      and _is_project_number(match["project"])
      != _is_project_number(glossary.project)
  )


def _sanitize(raw: str) -> str:
  """Replace every character outside ``[A-Za-z0-9_]`` with ``_``."""
  return _NON_IDENTIFIER_CHARS_RE.sub("_", raw)


def _ontology_identifier(raw: str) -> str:
  """``_sanitize`` plus a ``_`` prefix when the result starts with a digit."""
  name = _sanitize(raw)
  return "_" + name if name[:1].isdigit() else name


def _normalize_ontology_name(ontology_name: Any) -> str:
  if not isinstance(ontology_name, str) or not ontology_name.strip():
    raise ValueError("ontology_name must be a non-blank string.")
  stripped = ontology_name.strip()
  if not re.search(r"[A-Za-z0-9]", stripped):
    raise ValueError(
        f"ontology_name {ontology_name!r} has no ASCII letters or digits, so "
        "it cannot be sanitized into an identifier."
    )
  return _ontology_identifier(stripped)


def validate_import_arguments(
    glossary_name: str, *, ontology_name: str | None = None
) -> None:
  """Validate caller-supplied arguments without calling any API.

  :func:`import_dataplex_glossary` runs the same checks; the CLI calls
  this first so argument mistakes surface as usage errors.

  Raises:
      ValueError: ``glossary_name`` is not
          ``projects/<project>/locations/<location>/glossaries/<id>`` (a
          project number or a lowercase project ID, optionally
          domain-scoped; a lowercase location such as ``us-central1``;
          and a glossary ID without ``/`` or whitespace), or
          ``ontology_name`` is blank or has no ASCII letter or digit.
  """
  _parse_glossary_name(glossary_name)
  if ontology_name is not None:
    _normalize_ontology_name(ontology_name)


# --------------------------------------------------------------------- #
# Pure mapping                                                           #
# --------------------------------------------------------------------- #


def _field(record: Any, name: str) -> str:
  if isinstance(record, Mapping):
    value = record.get(name)
  else:
    value = getattr(record, name, None)
  return value if isinstance(value, str) else ""


@dataclasses.dataclass
class _Concept:
  """One glossary category or term on its way to becoming an entity."""

  kind: str
  leaf_id: str
  resource_name: str
  parent: str
  display_name: str
  description: str
  entity_name: str = ""
  annotations: dict[str, str] = dataclasses.field(default_factory=dict)

  def output_fields(self) -> tuple[str, ...]:
    """Every record field that reaches the output; used to spot conflicts."""
    return (
        self.resource_name,
        self.parent,
        self.display_name,
        self.description,
    )


@dataclasses.dataclass
class _Summary:
  glossary: str = ""
  ontology_name: str = ""
  ontology_name_source: str = ""
  categories: int = 0
  terms: int = 0
  broader_relationships: int = 0
  sanitized_identifiers: int = 0
  disambiguated_identifiers: int = 0
  orphan_parents: int = 0
  unpaired_project_parents: int = 0
  repeated_records: int = 0


def _collect_concepts(
    records: Iterable[Any],
    kind: str,
    glossary: _GlossaryRef,
    projects: frozenset[str],
    summary: _Summary,
) -> dict[str, _Concept]:
  """Parse category or term records into concepts keyed by leaf ID.

  Every record must lie in ``glossary`` (see :func:`_in_glossary`).
  Records repeated across pages are deduplicated by ``(kind, leaf_id)``;
  two records with the same identity but different fields are an error.
  """
  concepts: dict[str, _Concept] = {}
  name_re = _NAME_PATTERNS[kind]
  for record in records:
    resource_name = _field(record, "name").strip()
    match = name_re.fullmatch(resource_name)
    if match is None:
      raise ValueError(
          f"Glossary {kind} record has an unparseable resource name "
          f"{resource_name!r}; expected <glossary>/"
          f"{'categories' if kind == _CATEGORY else 'terms'}/<id>."
      )
    if not _in_glossary(match, glossary, projects):
      message = (
          f"Glossary {kind} {resource_name!r} does not belong to glossary "
          f"{glossary.name!r}."
      )
      if _unpaired_other_form(match, glossary, projects):
        message += (
            f" If {match['project']!r} is the same project as "
            f"{glossary.project!r}, import "
            f"projects/{match['project']}/locations/{glossary.location}"
            f"/glossaries/{glossary.glossary_id} instead so the importer "
            "can pair the two (or pass project_alias to "
            "map_dataplex_glossary)."
        )
      raise ValueError(message)
    concept = _Concept(
        kind=kind,
        leaf_id=match["leaf_id"],
        resource_name=resource_name,
        parent=_field(record, "parent").strip(),
        display_name=_field(record, "display_name").strip(),
        description=_field(record, "description").strip(),
    )
    existing = concepts.get(concept.leaf_id)
    if existing is None:
      concepts[concept.leaf_id] = concept
    elif existing.output_fields() == concept.output_fields():
      summary.repeated_records += 1
    else:
      raise ValueError(
          f"Conflicting records for glossary {kind} {concept.leaf_id!r}: "
          "the same resource ID was returned twice with different fields."
      )
  return concepts


def _assign_entity_names(concepts: list[_Concept], summary: _Summary) -> None:
  """Name every concept ``category_<id>`` / ``term_<id>``.

  The kind prefix keeps the category and term ID spaces apart (and every
  name clear of the ``skos_broader`` relationship name). Resources of the
  same kind whose sanitized IDs coincide all get a
  ``_<sha256(raw id)[:8]>`` suffix. When another entity already has such a
  name, the suffix grows one hex digit at a time up to the full digest,
  and then a ``_<n>`` counter (from 2) is appended. Every step depends
  only on the set of resources, never on their order.
  """
  groups: dict[tuple[str, str], list[_Concept]] = {}
  for concept in concepts:
    groups.setdefault((concept.kind, _sanitize(concept.leaf_id)), []).append(
        concept
    )
  taken: set[str] = set()
  for (kind, base), members in groups.items():
    if len(members) == 1:
      members[0].entity_name = f"{kind}_{base}"
      taken.add(members[0].entity_name)
  for key in sorted(k for k, members in groups.items() if len(members) > 1):
    kind, base = key
    members = groups[key]
    digests = {
        c.leaf_id: hashlib.sha256(c.leaf_id.encode("utf-8")).hexdigest()
        for c in members
    }
    for length in range(_HASH_SUFFIX_LENGTH, 65):
      names = {
          c.leaf_id: f"{kind}_{base}_{digests[c.leaf_id][:length]}"
          for c in members
      }
      unique = set(names.values())
      if len(unique) == len(members) and not unique & taken:
        break
    else:
      # Other names hold every prefix length: append a counter to the full
      # digest, member by member in ID order. Each member skips a finite
      # set of names, so this always ends.
      names = {}
      for concept in sorted(members, key=lambda c: c.leaf_id):
        counter = 2
        candidate = f"{kind}_{base}_{digests[concept.leaf_id]}_{counter}"
        while candidate in taken or candidate in names.values():
          counter += 1
          candidate = f"{kind}_{base}_{digests[concept.leaf_id]}_{counter}"
        names[concept.leaf_id] = candidate
      unique = set(names.values())
    for concept in members:
      concept.entity_name = names[concept.leaf_id]
    taken |= unique
  for (_, base), members in groups.items():
    for concept in members:
      if base != concept.leaf_id:
        summary.sanitized_identifiers += 1
      if len(members) > 1:
        summary.disambiguated_identifiers += 1


def _map_hierarchy(
    concepts: list[_Concept],
    categories: dict[str, _Concept],
    glossary: _GlossaryRef,
    projects: frozenset[str],
    summary: _Summary,
) -> set[tuple[str, str, str]]:
  """Annotate scheme membership and collect ``skos_broader`` edges.

  A parent matches when it lies in ``glossary`` (see :func:`_in_glossary`)
  and, for a category parent, names another category of this import. Any
  other parent, including one in another project, location or glossary,
  is an orphan.
  """
  scheme = glossary.name
  relationships: set[tuple[str, str, str]] = set()
  for concept in concepts:
    concept.annotations["skos:inScheme"] = scheme
    parent = concept.parent
    top = _GLOSSARY_NAME_RE.fullmatch(parent)
    nested = _CATEGORY_NAME_RE.fullmatch(parent)
    parent_category = categories.get(nested["leaf_id"]) if nested else None
    if parent_category is concept:
      parent_category = None
    # The parent this concept would hang from if the parent is in scope.
    target = top or (nested if parent_category is not None else None)
    if target and _in_glossary(target, glossary, projects):
      if top:
        concept.annotations["skos:topConceptOf"] = scheme
      else:
        relationships.add(
            (_SKOS_BROADER, concept.entity_name, parent_category.entity_name)
        )
      continue
    summary.orphan_parents += 1
    if target and _unpaired_other_form(target, glossary, projects):
      summary.unpaired_project_parents += 1
    if parent:
      concept.annotations["dataplex:orphan_parent"] = parent
  return relationships


# Plain (unquoted) scalars are limited to a conservative ASCII shape that
# YAML can only read back as the same string; everything else is quoted.
_PLAIN_SCALAR_RE = re.compile(
    r"[A-Za-z_][A-Za-z0-9_./@-]*(?: [A-Za-z0-9_./@-]+)*"
)
_PLAIN_KEY_RE = re.compile(
    r"[A-Za-z_][A-Za-z0-9_]*(?::[A-Za-z_][A-Za-z0-9_]*)?"
)
# Words YAML 1.1 / 1.2 parsers resolve to booleans or null.
_YAML_SPECIAL_WORDS = frozenset(
    {"y", "n", "yes", "no", "true", "false", "on", "off", "null"}
)
# Code points ``json.dumps(..., ensure_ascii=False)`` leaves raw that YAML
# either rejects (DEL, C1 controls, non-characters, lone surrogates) or
# reads as line breaks inside quoted scalars (NEL, LS, PS); a BOM is
# escaped too. They are rewritten as ``\uXXXX`` escapes.
_YAML_UNSAFE_CHARS_RE = re.compile(
    "[\x7f-\x9f\u2028\u2029\ufeff\ufffe\uffff\ud800-\udfff]"
)


def _quote(value: str) -> str:
  encoded = json.dumps(value, ensure_ascii=False)
  return _YAML_UNSAFE_CHARS_RE.sub(
      lambda m: f"\\u{ord(m.group()):04x}", encoded
  )


def _is_plain_safe(value: str) -> bool:
  if value.lower() in _YAML_SPECIAL_WORDS:
    return False
  try:
    float(value)
  except ValueError:
    return True
  return False


def _yaml_scalar(value: str) -> str:
  if _PLAIN_SCALAR_RE.fullmatch(value) and _is_plain_safe(value):
    return value
  return _quote(value)


def _yaml_key(key: str) -> str:
  if _PLAIN_KEY_RE.fullmatch(key) and _is_plain_safe(key):
    return key
  return _quote(key)


def _emit_yaml(document: dict[str, Any]) -> str:
  lines = [f"ontology: {_yaml_scalar(document['ontology'])}"]
  if "description" in document:
    lines.append(f"description: {_yaml_scalar(document['description'])}")
  lines.append("annotations:")
  for key, value in document["annotations"].items():
    lines.append(f"  {_yaml_key(key)}: {_yaml_scalar(value)}")
  lines.append("")
  lines.append("entities:")
  for entity in document["entities"]:
    lines.append(f"  - name: {_yaml_scalar(entity['name'])}")
    lines.append("    abstract: true")
    if "description" in entity:
      lines.append(f"    description: {_yaml_scalar(entity['description'])}")
    lines.append("    annotations:")
    for key, value in entity["annotations"].items():
      lines.append(f"      {_yaml_key(key)}: {_yaml_scalar(value)}")
  if "relationships" in document:
    lines.append("")
    lines.append("relationships:")
    for rel in document["relationships"]:
      lines.append(f"  - name: {_yaml_scalar(rel['name'])}")
      lines.append("    abstract: true")
      lines.append(f"    from: {_yaml_scalar(rel['from'])}")
      lines.append(f"    to: {_yaml_scalar(rel['to'])}")
  return "\n".join(lines) + "\n"


def _build_document(
    ontology_name: str,
    glossary: Any,
    glossary_ref: _GlossaryRef,
    concepts: list[_Concept],
    relationships: set[tuple[str, str, str]],
) -> dict[str, Any]:
  document: dict[str, Any] = {"ontology": ontology_name}
  description = _field(glossary, "description").strip()
  if description:
    document["description"] = description
  root_annotations = {"dataplex:glossary": glossary_ref.name}
  uid = _field(glossary, "uid").strip()
  if uid:
    root_annotations["dataplex:uid"] = uid
  document["annotations"] = dict(sorted(root_annotations.items()))

  entities = []
  for concept in sorted(concepts, key=lambda c: c.entity_name):
    annotations = dict(concept.annotations)
    annotations["skos:notation"] = concept.leaf_id
    annotations["dataplex:resource"] = concept.resource_name
    if concept.display_name:
      annotations["skos:prefLabel"] = concept.display_name
    entity: dict[str, Any] = {"name": concept.entity_name, "abstract": True}
    if concept.description:
      entity["description"] = concept.description
    entity["annotations"] = dict(sorted(annotations.items()))
    entities.append(entity)
  document["entities"] = entities
  if relationships:
    document["relationships"] = [
        {"name": name, "abstract": True, "from": src, "to": dst}
        for name, src, dst in sorted(relationships)
    ]
  return document


def _format_summary(summary: _Summary) -> str:
  lines = [f"Imported Dataplex glossary: {summary.glossary}"]
  if summary.ontology_name != summary.ontology_name_source:
    lines.append(
        f"Ontology name sanitized: {summary.ontology_name_source!r} -> "
        f"{summary.ontology_name!r}"
    )
  lines.append(
      f"Categories imported as abstract entities: {summary.categories}"
  )
  lines.append(f"Terms imported as abstract entities: {summary.terms}")
  if summary.broader_relationships:
    lines.append(
        "SKOS relationships imported as abstract: "
        f"{summary.broader_relationships} skos_broader"
    )
  if summary.sanitized_identifiers:
    lines.append(
        "Resource IDs with characters outside [A-Za-z0-9_] replaced by '_' "
        f"(raw IDs kept in skos:notation): {summary.sanitized_identifiers}"
    )
  if summary.disambiguated_identifiers:
    lines.append(
        "Entity names given a hash suffix after a same-kind name collision "
        f"(raw IDs kept in skos:notation): {summary.disambiguated_identifiers}"
    )
  if summary.orphan_parents:
    lines.append(
        "Orphan parents (entity kept in the scheme without a broader edge; "
        f"parent kept as dataplex:orphan_parent): {summary.orphan_parents}"
    )
  if summary.unpaired_project_parents:
    lines.append(
        "Orphan parents naming this glossary under an unverified project ID "
        "or number (if it is the same project, import the glossary by that "
        f"form to pair them): {summary.unpaired_project_parents}"
    )
  if summary.repeated_records:
    lines.append(
        "Identical repeated category/term records deduplicated: "
        f"{summary.repeated_records}"
    )
  lines.append(
      "Note: all entities are abstract (SKOS-only), so nothing is bound to "
      "tables. `gm compile` and OntologyRuntime still need a companion "
      "binding; one with no entities is enough for "
      "`gm compile --emit-concept-index --concept-index-table ...`."
  )
  return "\n".join(lines)


def map_dataplex_glossary(
    glossary: GlossaryProtocol,
    categories: Iterable[GlossaryCategoryProtocol],
    terms: Iterable[GlossaryTermProtocol],
    *,
    ontology_name: str | None = None,
    project_alias: str | None = None,
) -> tuple[str, str]:
  """Map a Business Glossary and its categories and terms to ontology YAML.

  Pure: no network calls, no ``google-cloud-dataplex`` import, no I/O.
  Records may be the proto-plus messages returned by
  ``google-cloud-dataplex`` or any objects (or mappings) exposing the same
  fields. Output is canonical: the same glossary content produces
  byte-identical YAML regardless of record order or repeated records.

  Args:
      glossary: The ``Glossary``; ``name`` must be its canonical resource
          name, ``projects/<project>/locations/<location>/glossaries/<id>``.
      categories: The glossary's ``GlossaryCategory`` records.
      terms: The glossary's ``GlossaryTerm`` records.
      ontology_name: Name for the emitted ontology. Stripped, then every
          character outside ``[A-Za-z0-9_]`` is replaced by ``_`` (and a
          leading digit gets a ``_`` prefix). Defaults to the glossary ID,
          sanitized the same way.
      project_alias: The glossary's project under its other form, when the
          caller has verified that both name one project: its project ID
          for a glossary named by project number, or the other way round.
          Records and parents may then use either form. By default the
          project must equal the glossary's exactly, so a record from any
          other project is rejected and a parent in any other project is
          an orphan.

  Returns:
      A ``(yaml_text, summary)`` tuple. ``yaml_text`` is an ontology that
      has passed ``load_ontology_from_string``; ``summary`` reports what
      was imported, renamed, deduplicated, or left as an orphan.

  Raises:
      ValueError: Invalid ``ontology_name``, ``project_alias`` or glossary
          name, a category or term record that is unparseable, outside the
          glossary, or in conflict with another record of the same ID, or
          a glossary with no categories and no terms.
  """
  glossary_ref = _parse_glossary_name(_field(glossary, "name").strip())
  projects = _allowed_projects(glossary_ref, project_alias)
  if ontology_name is not None:
    name = _normalize_ontology_name(ontology_name)
    name_source = ontology_name.strip()
  else:
    name = _ontology_identifier(glossary_ref.glossary_id)
    name_source = glossary_ref.glossary_id

  summary = _Summary(
      glossary=glossary_ref.name,
      ontology_name=name,
      ontology_name_source=name_source,
  )
  category_map = _collect_concepts(
      categories, _CATEGORY, glossary_ref, projects, summary
  )
  term_map = _collect_concepts(terms, _TERM, glossary_ref, projects, summary)
  if not category_map and not term_map:
    raise ValueError(
        f"Glossary {glossary_ref.name!r} has no categories or terms to "
        "import; an ontology needs at least one entity."
    )
  summary.categories = len(category_map)
  summary.terms = len(term_map)

  concepts = list(category_map.values()) + list(term_map.values())
  _assign_entity_names(concepts, summary)
  relationships = _map_hierarchy(
      concepts, category_map, glossary_ref, projects, summary
  )
  summary.broader_relationships = len(relationships)

  document = _build_document(
      name, glossary, glossary_ref, concepts, relationships
  )
  yaml_text = _emit_yaml(document)
  # The emitter is hand-written for canonical output; refuse to return
  # text that doesn't read back as exactly the intended document.
  if yaml.safe_load(yaml_text) != document:
    raise RuntimeError(
        "internal error: emitted ontology YAML does not round-trip."
    )
  load_ontology_from_string(yaml_text)
  return yaml_text, _format_summary(summary)


# --------------------------------------------------------------------- #
# API orchestration                                                      #
# --------------------------------------------------------------------- #


def _create_default_glossary_client() -> Any:
  """Build a ``BusinessGlossaryServiceClient``, importing the SDK lazily."""
  try:
    dataplex_v1 = importlib.import_module("google.cloud.dataplex_v1")
  except ImportError as exc:
    raise MissingDataplexDependencyError(
        "google-cloud-dataplex is required for gm import-dataplex-glossary "
        f"({exc}). Install it with: {_INSTALL_HINT}"
    ) from exc
  client_cls = getattr(dataplex_v1, "BusinessGlossaryServiceClient", None)
  if client_cls is None:
    raise MissingDataplexDependencyError(
        f"google-cloud-dataplex>={_MIN_DATAPLEX_VERSION} is required: the "
        "installed version has no BusinessGlossaryServiceClient. Upgrade "
        f"with: pip install -U 'bigquery-agent-analytics[dataplex]'"
    )
  try:
    return client_cls()
  except Exception as exc:  # noqa: BLE001 - surfaced with context
    raise _api_error("client construction", exc) from exc


def _error_hint(exc: BaseException) -> str:
  names = {cls.__name__ for cls in type(exc).__mro__}
  code = getattr(exc, "code", None)
  http_code = (
      code if isinstance(code, int) and not isinstance(code, bool) else None
  )
  text = str(exc)
  if (
      names & {"PermissionDenied", "Forbidden"}
      or http_code == 403
      or "PERMISSION_DENIED" in text
  ):
    return _PERMISSION_HINT
  if (
      names
      & {
          "Unauthenticated",
          "Unauthorized",
          "DefaultCredentialsError",
          "RefreshError",
      }
      or http_code == 401
      or "UNAUTHENTICATED" in text
  ):
    return _CREDENTIALS_HINT
  return ""


def _api_error(operation: str, exc: BaseException) -> DataplexImportError:
  message = f"Dataplex {operation} failed: {type(exc).__name__}: {exc}"
  hint = _error_hint(exc)
  if hint:
    message = f"{message} {hint}"
  return DataplexImportError(message)


def _call_api(operation: str, fn: Callable[[], Any]) -> Any:
  """Run ``fn``, wrapping every client failure in DataplexImportError.

  Anything the client raises is an API, transport, or credential failure,
  including ``ValueError`` subclasses such as JSON decode errors and
  malformed-credential errors. Only ``DataplexImportError`` (for example
  an incomplete list) and ``MissingDataplexDependencyError`` keep their
  type.
  """
  try:
    return fn()
  except (MissingDataplexDependencyError, DataplexImportError):
    raise
  except Exception as exc:  # noqa: BLE001 - re-raised with context
    raise _api_error(operation, exc) from exc


# Sentinels: an exhausted page iterator (a page itself could be anything,
# even ``None``) and a list result that has no ``pages`` at all.
_NO_PAGE = object()
_NO_PAGES = object()


def _check_reachable(operation: str, page: Any) -> None:
  """Raise when a list page reports locations the service couldn't read.

  Any entry counts, even a blank one: an empty or whitespace-only marker
  still says the result is partial, so it is shown by its ``repr``.
  """
  unreachable = list(getattr(page, "unreachable_locations", None) or ())
  if unreachable:
    shown = sorted({str(loc).strip() or repr(loc) for loc in unreachable})
    raise DataplexImportError(
        f"Dataplex {operation} could not reach location(s) "
        f"{', '.join(shown)}, so its result is incomplete; refusing to emit "
        "a partial ontology. Retry the import later."
    )


def _plain_page_items(operation: str, page: Any) -> Iterable[Any]:
  """Return the items of a page that is a plain collection of items."""
  if not isinstance(page, (str, bytes, bytearray, Mapping)):
    try:
      return iter(page)
    except TypeError:
      pass
  raise DataplexImportError(
      f"Dataplex {operation} returned a page that is not a collection of "
      "items; refusing to emit a partial ontology."
  )


def _collect_pager_items(operation: str, pager: Any, items_attr: str) -> list:
  """Read every item of a list call, refusing partial results.

  A GAPIC pager's ``pages`` yields the raw ``List...Response`` messages,
  each holding its items in ``items_attr`` and, in
  ``unreachable_locations``, the locations the service could not read.
  Any such location, on any page, means the result is incomplete, so this
  raises instead of returning it. The pages are read as such responses
  when the result exposes response fields itself, as a GAPIC pager does,
  or its first page has ``items_attr`` or ``unreachable_locations``: then
  a page without ``items_attr`` is an error, never an empty page. A
  result without ``pages``, or whose pages are plain collections of
  items, is read item by item.

  ``pages`` is read once. When it is an iterator, which can be consumed
  only once, its plain pages are read as the items, and the result is
  never iterated again, so no item is lost or read twice.

  Raises:
      DataplexImportError: A page reports unreachable locations, or can't
          be read: a GAPIC-style result has a page without ``items_attr``,
          or a plain page is not a collection of items.
  """
  pages_attr = getattr(pager, "pages", _NO_PAGES)
  if pages_attr is _NO_PAGES:
    return list(pager)
  # A GAPIC pager also exposes the current response's fields.
  response_like = hasattr(pager, items_attr) or hasattr(
      pager, "unreachable_locations"
  )
  try:
    pages = iter(pages_attr)
  except TypeError:
    if not response_like:
      return list(pager)
    raise DataplexImportError(
        f"Dataplex {operation} returned pages that cannot be iterated; "
        "refusing to emit a partial ontology."
    ) from None
  # An iterator is its own iterator, and can be consumed only once.
  single_use = pages is pages_attr
  page = next(pages, _NO_PAGE)
  if page is _NO_PAGE:
    return [] if single_use else list(pager)
  plain = (
      not response_like
      and not hasattr(page, items_attr)
      and not hasattr(page, "unreachable_locations")
  )
  if plain and not single_use:
    # Plain pages of items, not responses, that can be read again: iterate
    # the items instead.
    return list(pager)
  items: list[Any] = []
  while page is not _NO_PAGE:
    # First, so a page that reports unreachable locations always fails as
    # incomplete, whatever else it holds.
    _check_reachable(operation, page)
    if plain:
      items.extend(_plain_page_items(operation, page))
    elif not hasattr(page, items_attr):
      raise DataplexImportError(
          f"Dataplex {operation} returned an unexpected page object "
          f"without {items_attr!r}; refusing to emit a partial ontology."
      )
    else:
      items.extend(getattr(page, items_attr) or ())
    page = next(pages, _NO_PAGE)
  return items


def _verified_project_alias(
    requested: _GlossaryRef, returned: _GlossaryRef
) -> str | None:
  """Check that ``GetGlossary`` returned the requested glossary.

  The location and glossary ID must be equal. The project must be equal
  too, or be the same project under its other form: a request by project
  ID answered with a name by project number (or the other way round),
  which the service itself resolved, so the pair is verified.

  Returns:
      The requested project when the two forms differ (the verified alias
      of the returned project), else ``None``.

  Raises:
      ValueError: The response names another location, glossary ID, or
          project.
  """
  same_project = returned.project == requested.project or (
      _is_project_number(returned.project)
      != _is_project_number(requested.project)
  )
  if not same_project or (returned.location, returned.glossary_id) != (
      requested.location,
      requested.glossary_id,
  ):
    raise ValueError(
        f"GetGlossary for {requested.name!r} returned {returned.name!r}, "
        "which names a different glossary; nothing was listed."
    )
  return requested.project if returned.project != requested.project else None


@dataclasses.dataclass(frozen=True)
class _GlossaryRecord:
  name: str
  uid: str
  description: str


def import_dataplex_glossary(
    glossary_name: str,
    *,
    ontology_name: str | None = None,
    glossary_client: BusinessGlossaryClientProtocol | None = None,
) -> tuple[str, str]:
  """Fetch a Business Glossary from Dataplex and map it to ontology YAML.

  Calls ``GetGlossary`` and checks that the response names the requested
  glossary (the same location and glossary ID, and the same project or
  the same project under its other form) before listing the glossary's
  categories and terms under its canonical name, all pages; a response
  without a resource name is rejected before anything is listed. A
  request by project ID answered by project number (or the other way
  round) verifies that pair, so records and parents may use either form.
  Mapping, YAML emission, and validation are delegated to
  :func:`map_dataplex_glossary`. Read-only:
  ``roles/dataplex.catalogViewer`` is sufficient.

  Args:
      glossary_name: Glossary resource name,
          ``projects/<project>/locations/<location>/glossaries/<id>``.
      ontology_name: See :func:`map_dataplex_glossary`.
      glossary_client: Optional ``BusinessGlossaryServiceClient`` or a
          compatible fake. When omitted, one is created from Application
          Default Credentials (requires ``google-cloud-dataplex``).

  Returns:
      ``(yaml_text, summary)``, as returned by
      :func:`map_dataplex_glossary`.

  Raises:
      ValueError: Invalid arguments, a ``GetGlossary`` response without a
          glossary resource name or naming another glossary, or invalid
          glossary content (see :func:`map_dataplex_glossary`).
      MissingDataplexDependencyError: No client was injected and
          ``google-cloud-dataplex>=2.12.0`` is not installed.
      DataplexImportError: A Dataplex API, transport, or authentication
          call failed, or a list page reported unreachable locations or
          could not be read.
  """
  validate_import_arguments(glossary_name, ontology_name=ontology_name)
  requested = _parse_glossary_name(glossary_name)
  if glossary_client is None:
    glossary_client = _create_default_glossary_client()

  glossary = _call_api(
      "GetGlossary",
      lambda: glossary_client.get_glossary(request={"name": glossary_name}),
  )
  canonical_name = _field(glossary, "name").strip()
  if not canonical_name:
    raise ValueError(
        f"GetGlossary for {glossary_name!r} returned no glossary resource "
        "name; nothing was listed."
    )
  try:
    returned = _parse_glossary_name(canonical_name)
  except ValueError:
    raise ValueError(
        f"GetGlossary for {glossary_name!r} returned the unexpected resource "
        f"name {canonical_name!r}; nothing was listed."
    ) from None
  project_alias = _verified_project_alias(requested, returned)

  categories = _call_api(
      "ListGlossaryCategories",
      lambda: _collect_pager_items(
          "ListGlossaryCategories",
          glossary_client.list_glossary_categories(
              request={"parent": canonical_name}
          ),
          "categories",
      ),
  )
  terms = _call_api(
      "ListGlossaryTerms",
      lambda: _collect_pager_items(
          "ListGlossaryTerms",
          glossary_client.list_glossary_terms(
              request={"parent": canonical_name}
          ),
          "terms",
      ),
  )
  record = _GlossaryRecord(
      name=canonical_name,
      uid=_field(glossary, "uid"),
      description=_field(glossary, "description"),
  )
  return map_dataplex_glossary(
      record,
      categories,
      terms,
      ontology_name=ontology_name,
      project_alias=project_alias,
  )
