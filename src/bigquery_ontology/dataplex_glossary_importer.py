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

Entity names come from the full resource identity: ``category_<id>`` for
``.../categories/<id>`` and ``term_<id>`` for ``.../terms/<id>``, with
every character outside ``[A-Za-z0-9_]`` replaced by ``_``. The kind
prefix keeps the two ID spaces apart; resources of the same kind whose
IDs normalize to the same name each get a ``_<sha256(id)[:8]>`` suffix.

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

# A resource-name segment: non-empty, no ``/`` and no whitespace.
_SEGMENT = r"[^/\s]+"
_GLOSSARY_PREFIX = (
    rf"projects/(?P<project>{_SEGMENT})/locations/(?P<location>{_SEGMENT})"
    rf"/glossaries/(?P<glossary_id>{_SEGMENT})"
)
# All patterns are applied with ``fullmatch`` so a trailing newline can't
# slip through the way it does with ``re.match`` and ``$``.
_GLOSSARY_NAME_RE = re.compile(_GLOSSARY_PREFIX)
_CATEGORY_NAME_RE = re.compile(
    rf"{_GLOSSARY_PREFIX}/categories/(?P<leaf_id>{_SEGMENT})"
)
_TERM_NAME_RE = re.compile(rf"{_GLOSSARY_PREFIX}/terms/(?P<leaf_id>{_SEGMENT})")

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


def _same_project(project_a: str, project_b: str) -> bool:
  """Whether two resource-name project segments can name the same project.

  Resource names may give a project by ID or by number. A project ID
  always starts with a letter and a project number is all ASCII digits,
  so two different IDs, or two different numbers, are two projects. An
  ID and a number can't be compared offline and are accepted as one.
  """
  if project_a == project_b:
    return True
  return _is_project_number(project_a) != _is_project_number(project_b)


def _in_glossary(match: re.Match[str], glossary: _GlossaryRef) -> bool:
  """Whether a parsed glossary, category or term name lies in ``glossary``.

  The location and glossary ID must be equal, and the projects must match
  under :func:`_same_project`.
  """
  return (
      match["location"] == glossary.location
      and match["glossary_id"] == glossary.glossary_id
      and _same_project(match["project"], glossary.project)
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
          ``projects/<project>/locations/<location>/glossaries/<id>``
          (segments non-empty, without ``/`` or whitespace), or
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
  repeated_records: int = 0


def _collect_concepts(
    records: Iterable[Any],
    kind: str,
    glossary: _GlossaryRef,
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
    if not _in_glossary(match, glossary):
      raise ValueError(
          f"Glossary {kind} {resource_name!r} does not belong to glossary "
          f"{glossary.name!r}."
      )
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
  ``_<sha256(raw id)[:8]>`` suffix (longer if ever needed), so the result
  depends only on the set of resources, never on their order.
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
    else:  # pragma: no cover - would need a full SHA-256 collision
      raise RuntimeError(f"cannot disambiguate glossary {kind} {base!r}")
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
    summary: _Summary,
) -> set[tuple[str, str, str]]:
  """Annotate scheme membership and collect ``skos_broader`` edges.

  A parent matches when it lies in ``glossary`` (see :func:`_in_glossary`)
  and, for a category parent, names a category of this import. A
  ``parent`` may name the project by ID or by number, so an ID and a
  number are accepted as the same project; a parent in another project,
  location or glossary is an orphan.
  """
  scheme = glossary.name
  relationships: set[tuple[str, str, str]] = set()
  for concept in concepts:
    concept.annotations["skos:inScheme"] = scheme
    parent = concept.parent
    top = _GLOSSARY_NAME_RE.fullmatch(parent)
    if top and _in_glossary(top, glossary):
      concept.annotations["skos:topConceptOf"] = scheme
      continue
    nested = _CATEGORY_NAME_RE.fullmatch(parent)
    parent_concept = None
    if nested and _in_glossary(nested, glossary):
      parent_concept = categories.get(nested["leaf_id"])
    if parent_concept is not None and parent_concept is not concept:
      relationships.add(
          (_SKOS_BROADER, concept.entity_name, parent_concept.entity_name)
      )
      continue
    summary.orphan_parents += 1
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

  Returns:
      A ``(yaml_text, summary)`` tuple. ``yaml_text`` is an ontology that
      has passed ``load_ontology_from_string``; ``summary`` reports what
      was imported, renamed, deduplicated, or left as an orphan.

  Raises:
      ValueError: Invalid ``ontology_name`` or glossary name, a category or
          term record that is unparseable, outside the glossary, or in
          conflict with another record of the same ID, or a glossary with
          no categories and no terms.
  """
  glossary_ref = _parse_glossary_name(_field(glossary, "name").strip())
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
  category_map = _collect_concepts(categories, _CATEGORY, glossary_ref, summary)
  term_map = _collect_concepts(terms, _TERM, glossary_ref, summary)
  if not category_map and not term_map:
    raise ValueError(
        f"Glossary {glossary_ref.name!r} has no categories or terms to "
        "import; an ontology needs at least one entity."
    )
  summary.categories = len(category_map)
  summary.terms = len(term_map)

  concepts = list(category_map.values()) + list(term_map.values())
  _assign_entity_names(concepts, summary)
  relationships = _map_hierarchy(concepts, category_map, glossary_ref, summary)
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
  """Run ``fn``, wrapping client/API/auth failures in DataplexImportError.

  ``ValueError`` and ``MissingDataplexDependencyError`` keep their type so
  callers can tell validation and dependency problems apart from API
  failures.
  """
  try:
    return fn()
  except (ValueError, MissingDataplexDependencyError):
    raise
  except Exception as exc:  # noqa: BLE001 - re-raised with context
    raise _api_error(operation, exc) from exc


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

  Calls ``GetGlossary`` and then lists the glossary's categories and terms
  under its canonical name (all pages), and delegates mapping, YAML
  emission, and validation to :func:`map_dataplex_glossary`. Read-only:
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
      ValueError: Invalid arguments or glossary content (see
          :func:`map_dataplex_glossary`).
      MissingDataplexDependencyError: No client was injected and
          ``google-cloud-dataplex>=2.12.0`` is not installed.
      DataplexImportError: A Dataplex API, transport, or authentication
          call failed.
  """
  validate_import_arguments(glossary_name, ontology_name=ontology_name)
  if glossary_client is None:
    glossary_client = _create_default_glossary_client()

  glossary = _call_api(
      "GetGlossary",
      lambda: glossary_client.get_glossary(request={"name": glossary_name}),
  )
  canonical_name = _field(glossary, "name").strip() or glossary_name
  categories = _call_api(
      "ListGlossaryCategories",
      lambda: list(
          glossary_client.list_glossary_categories(
              request={"parent": canonical_name}
          )
      ),
  )
  terms = _call_api(
      "ListGlossaryTerms",
      lambda: list(
          glossary_client.list_glossary_terms(
              request={"parent": canonical_name}
          )
      ),
  )
  record = _GlossaryRecord(
      name=canonical_name,
      uid=_field(glossary, "uid"),
      description=_field(glossary, "description"),
  )
  return map_dataplex_glossary(
      record, categories, terms, ontology_name=ontology_name
  )
