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

"""Loader and validator for binding YAML.

Shape validation (required fields, unknown keys, enum membership, list
min-length) lives in ``binding_models`` and runs at pydantic parse
time. Semantic validation — everything that requires consulting the
referenced ontology — lives here. The two halves together cover the
binding spec end-to-end.

The governing mental model is **partial at the ontology level, total
within each element**:

  - You may leave whole entities or whole relationships out of the
    binding. Anything absent is simply not realized on this target.
  - But once you include an entity or relationship, you must bind
    every one of its non-derived properties (including inherited
    ones) — no cherry-picking. Derived (``expr:``) properties are the
    mirror image and must *never* appear.

Rules enforced by ``_validate_binding``:

  - The binding's declared ontology name matches the injected
    ``Ontology`` object's name.
  - Entity and relationship binding names are unique within the
    binding and each resolves to a declared element in the ontology.
  - Total coverage within each included entity/relationship, per the
    model above.
  - Each included relationship's ``from_columns`` / ``to_columns``
    arity matches the endpoint entity's primary-key arity.
  - Each included relationship's endpoints each have at least one
    bound descendant in the binding — an edge that points at an
    entity tree with no bound node would dangle.

Spanner-specific type checks from the spec are intentionally skipped —
this loader is BigQuery-only, which supports every logical type.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, Optional

import yaml

from .binding_models import Binding
from .binding_models import EntityBinding
from .binding_models import PropertyBinding
from .binding_models import RelationshipBinding
from .ontology_loader import _effective_keys
from .ontology_loader import _effective_properties
from .ontology_loader import _is_entity_subtype
from .ontology_loader import load_ontology
from .ontology_models import Entity
from .ontology_models import Ontology
from .ontology_models import Property
from .ontology_models import Relationship

# --------------------------------------------------------------------- #
# Public entry points                                                    #
# --------------------------------------------------------------------- #


def load_binding(
    path: str | Path, *, ontology: Optional[Ontology] = None
) -> Binding:
  """Load and validate a binding from a YAML file.

  If ``ontology`` is not supplied, the loader reads the binding's
  top-level ``ontology:`` key and looks for ``<name>.ontology.yaml`` in
  the same directory as the binding file. Supply ``ontology``
  explicitly to override that lookup, or to share a single parsed
  ontology across many bindings.

  Raises:
      FileNotFoundError: The binding file, or its auto-discovered
          companion ontology file, does not exist.
      ValueError: Any semantic validation failure.
      pydantic.ValidationError: Shape failures (unknown keys, bad
          enums, missing required fields).
      yaml.YAMLError: Malformed YAML in either file.
  """
  binding_path = Path(path)
  text = binding_path.read_text(encoding="utf-8")

  if ontology is None:
    ontology = _discover_ontology(text, binding_path)

  return load_binding_from_string(text, ontology=ontology)


def load_binding_from_string(
    yaml_string: str, *, ontology: Ontology
) -> Binding:
  """Parse and validate a binding from a YAML string.

  Unlike :func:`load_binding`, the ontology must be supplied
  explicitly here — there is no file context from which to discover
  one.
  """
  data = yaml.safe_load(yaml_string)
  if not isinstance(data, dict):
    raise ValueError("Binding document must be a YAML mapping.")
  binding = Binding(**data)
  _validate_binding(binding, ontology)
  return binding


# --------------------------------------------------------------------- #
# Companion-ontology discovery                                           #
# --------------------------------------------------------------------- #


def _discover_ontology(binding_text: str, binding_path: Path) -> Ontology:
  """Locate and load ``<ontology>.ontology.yaml`` next to the binding.

  Peeks at the binding YAML purely to pull out the ``ontology:`` name;
  any structural errors here are swallowed so that the richer pydantic
  error from ``Binding(**data)`` surfaces instead.
  """
  data = yaml.safe_load(binding_text)
  ontology_name: str | None = None
  if isinstance(data, dict) and isinstance(data.get("ontology"), str):
    ontology_name = data["ontology"]
  if not ontology_name:
    raise ValueError(
        f"Binding {binding_path} does not declare an 'ontology:' name; "
        "cannot auto-discover companion ontology file."
    )
  companion = binding_path.parent / f"{ontology_name}.ontology.yaml"
  if not companion.exists():
    raise FileNotFoundError(
        f"Binding references ontology {ontology_name!r}, but no companion "
        f"ontology file found at {companion}."
    )
  return load_ontology(companion)


# --------------------------------------------------------------------- #
# Validation                                                             #
# --------------------------------------------------------------------- #


def _validate_binding(binding: Binding, ontology: Ontology) -> None:
  """Run every cross-ontology check on a parsed binding."""
  if binding.ontology != ontology.ontology:
    raise ValueError(
        f"Binding declares ontology {binding.ontology!r} but was paired "
        f"with ontology {ontology.ontology!r}."
    )

  entity_map = {e.name: e for e in ontology.entities}
  rel_map = {r.name: r for r in ontology.relationships}

  _check_unique_binding_names(binding)
  _check_binding_names_resolve(binding, entity_map, rel_map)

  for eb in binding.entities:
    _check_entity_property_coverage(eb, entity_map[eb.name], entity_map)

  for rb in binding.relationships:
    rel = rel_map[rb.name]
    _check_relationship_property_coverage(rb, rel, rel_map)
    _check_relationship_endpoint_arity(rb, rel, entity_map)

  bound_entity_names = {eb.name for eb in binding.entities}
  for rb in binding.relationships:
    _check_relationship_endpoint_closure(
        rb, rel_map[rb.name], entity_map, bound_entity_names
    )


# --------------------------------------------------------------------- #
# Individual checks                                                      #
# --------------------------------------------------------------------- #


def _check_unique_binding_names(binding: Binding) -> None:
  """Entity and relationship binding names must be unique across kinds.

  The ontology loader already prevents entity/relationship name
  collisions, so a cross-kind duplicate in a valid binding is
  impossible in practice. We check defensively — the cost is
  negligible and the error message is clearer than a downstream
  name-resolution failure.
  """
  _assert_unique((eb.name for eb in binding.entities), "entity binding")
  _assert_unique(
      (rb.name for rb in binding.relationships), "relationship binding"
  )
  all_names = [eb.name for eb in binding.entities] + [
      rb.name for rb in binding.relationships
  ]
  _assert_unique(iter(all_names), "binding")


def _assert_unique(names: Iterable[str], kind: str) -> None:
  seen: set[str] = set()
  for n in names:
    if n in seen:
      raise ValueError(f"Duplicate {kind} name: {n!r}")
    seen.add(n)


def _check_binding_names_resolve(
    binding: Binding,
    entity_map: dict[str, Entity],
    rel_map: dict[str, Relationship],
) -> None:
  """Every bound name must reference a declared ontology element."""
  for eb in binding.entities:
    if eb.name not in entity_map:
      raise ValueError(
          f"Entity binding {eb.name!r} does not name a declared entity "
          f"in ontology {binding.ontology!r}."
      )
    if entity_map[eb.name].abstract:
      raise ValueError(
          f"Entity binding {eb.name!r} targets an abstract entity "
          f"(declared for documentation but not backed by a table). "
          f"Abstract entities typically come from SKOS-only concepts or "
          f"ontologies that declare structure without physical "
          f"realization. Remove this binding, or promote {eb.name!r} in "
          f"the ontology (drop ``abstract: true`` and give it keys) to "
          f"back it with a table."
      )
  for rb in binding.relationships:
    if rb.name not in rel_map:
      raise ValueError(
          f"Relationship binding {rb.name!r} does not name a declared "
          f"relationship in ontology {binding.ontology!r}."
      )
    if rel_map[rb.name].abstract:
      raise ValueError(
          f"Relationship binding {rb.name!r} targets an abstract "
          f"relationship (declared for documentation but not backed by "
          f"an edge table). Abstract relationships typically come from "
          f"SKOS graph predicates (e.g. ``skos:broader``) or from "
          f"relationships whose endpoints are abstract. Remove this "
          f"binding, or promote {rb.name!r} in the ontology (drop "
          f"``abstract: true`` and ensure its endpoints are concrete) "
          f"to back it with an edge table."
      )


def _check_entity_property_coverage(
    eb: EntityBinding,
    entity: Entity,
    entity_map: dict[str, Entity],
) -> None:
  """Every non-derived property (inherited included) is bound exactly once."""
  effective = _effective_properties(entity, entity_map)
  _check_property_coverage(
      bindings=eb.properties,
      effective=effective,
      owner=f"Entity binding {eb.name!r}",
  )


def _check_relationship_property_coverage(
    rb: RelationshipBinding,
    rel: Relationship,
    rel_map: dict[str, Relationship],
) -> None:
  """Same as entity coverage, applied to a relationship's own properties."""
  effective = _effective_properties(rel, rel_map)
  _check_property_coverage(
      bindings=rb.properties,
      effective=effective,
      owner=f"Relationship binding {rb.name!r}",
  )


def _check_property_coverage(
    *,
    bindings: list[PropertyBinding],
    effective: dict[str, Property],
    owner: str,
) -> None:
  """Enforce total coverage for one included entity or relationship.

  ``effective`` is the element's full property set with inheritance
  flattened. Given that, four failure modes are caught at once:

    1. A PropertyBinding names a property not declared on the element.
    2. A PropertyBinding names a derived (``expr:``) property — those
       are excluded from bindings by design (the compiler substitutes
       the expression).
    3. Two PropertyBindings target the same property name.
    4. A non-derived property has no PropertyBinding — partial coverage
       within an included element is not allowed.
  """
  required = {name for name, prop in effective.items() if prop.expr is None}
  seen: set[str] = set()
  for pb in bindings:
    if pb.name not in effective:
      raise ValueError(
          f"{owner}: property {pb.name!r} is not declared on this element."
      )
    if effective[pb.name].expr is not None:
      raise ValueError(
          f"{owner}: property {pb.name!r} is derived (has 'expr:') and "
          "must not appear in a binding."
      )
    if pb.name in seen:
      raise ValueError(
          f"{owner}: property {pb.name!r} is bound more than once."
      )
    seen.add(pb.name)

  missing = sorted(required - seen)
  if missing:
    raise ValueError(
        f"{owner}: missing bindings for non-derived properties " f"{missing!r}."
    )


def _check_relationship_endpoint_arity(
    rb: RelationshipBinding,
    rel: Relationship,
    entity_map: dict[str, Entity],
) -> None:
  """``from_columns`` / ``to_columns`` arity must match the endpoint keys.

  Works against both legacy ``list[str]`` and the new
  ``list[dict[str, str]]`` shape — each list entry counts as one
  column regardless of which shape it takes. The semantic check
  that every ``target_property`` names an effective primary-key
  property on the endpoint is in
  :func:`normalize_relationship_columns`.
  """
  from_pk = _primary_key_len(rel.from_, entity_map)
  to_pk = _primary_key_len(rel.to, entity_map)
  if len(rb.from_columns) != from_pk:
    raise ValueError(
        f"Relationship binding {rb.name!r}: from_columns has "
        f"{len(rb.from_columns)} column(s) but endpoint entity "
        f"{rel.from_!r} has {from_pk}-column primary key."
    )
  if len(rb.to_columns) != to_pk:
    raise ValueError(
        f"Relationship binding {rb.name!r}: to_columns has "
        f"{len(rb.to_columns)} column(s) but endpoint entity "
        f"{rel.to!r} has {to_pk}-column primary key."
    )


def normalize_relationship_columns(
    column_entries: list,
    endpoint_entity_name: str,
    entity_map: dict[str, Entity],
    *,
    side: str,
    relationship_name: str,
) -> tuple[tuple[str, str], ...]:
  """Resolve a ``RelationshipBinding`` column list to canonical form.

  Returns a tuple of ``(edge_column, target_property)`` pairs in the
  declared order. The two input shapes resolve as follows:

  * ``str`` (legacy) — ``target_property`` defaults to the endpoint
    entity's Nth primary-key property (1-to-1 by position).
  * ``dict[str, str]`` (explicit) — the dict's single key is the
    edge column and its value is the target property name. The
    target property MUST be one of the endpoint's effective
    primary-key properties — this is FK→PK mapping, not FK→any-
    column, because a non-PK target would let a relationship edge
    point at a row that isn't uniquely identified by its endpoint
    columns.

  This is the bridge between the pydantic shape (which accepts both)
  and the canonical form ``ResolvedRelationship.from_column_mapping``
  / ``to_column_mapping`` carry. The shape check in
  :class:`RelationshipBinding._validate_column_entries` already
  guarantees each entry is a non-empty string or a single-key
  ``str → str`` dict; this function does the semantic check that
  ``target_property`` names a real PK property on
  ``endpoint_entity_name``, honoring inherited keys.

  Args:
    column_entries: ``rb.from_columns`` or ``rb.to_columns``.
    endpoint_entity_name: ``rel.from_`` or ``rel.to``.
    entity_map: Map of entity name → :class:`Entity` for property
        lookup. Inheritance is followed via :func:`_effective_keys`
        and :func:`_effective_properties` so an endpoint that
        inherits its PK from a parent entity resolves correctly.
    side: ``"from"`` or ``"to"`` — used in error messages.
    relationship_name: ``rb.name`` — used in error messages.

  Returns:
    A tuple of ``(edge_column, target_property)`` pairs.

  Raises:
    ValueError: If ``target_property`` doesn't name a declared
      primary-key property on the endpoint entity (including
      inherited PKs), or if a legacy ``str``-shape entry's position
      exceeds the endpoint's PK arity.
  """
  endpoint = entity_map.get(endpoint_entity_name)
  if endpoint is None:
    raise ValueError(
        f"Relationship binding {relationship_name!r}: endpoint entity "
        f"{endpoint_entity_name!r} not found in the ontology."
    )
  # Use the inheritance-aware helpers so an endpoint that inherits
  # its PK from a parent entity resolves correctly. Mirrors what the
  # arity check already does via ``_primary_key_len``; not doing it
  # here would silently regress every ontology that uses an
  # ``extends`` chain on the endpoint side of a relationship.
  effective_keys = _effective_keys(endpoint, entity_map)
  if effective_keys is None or not effective_keys.primary:
    raise ValueError(
        f"Relationship binding {relationship_name!r}: endpoint entity "
        f"{endpoint_entity_name!r} has no effective primary key declared "
        "in the ontology (including inherited keys)."
    )
  endpoint_pk_properties = list(effective_keys.primary)
  # FK→PK: explicit mappings must target a PK property. Allowing any
  # property would let C2's materializer fix consume a canonical
  # mapping that points an edge endpoint at a non-key column, which
  # doesn't uniquely identify the target row.
  endpoint_pk_property_set = set(endpoint_pk_properties)

  canonical: list[tuple[str, str]] = []
  for idx, entry in enumerate(column_entries):
    if isinstance(entry, str):
      # Legacy shape: target_property = endpoint's Nth PK property.
      if idx >= len(endpoint_pk_properties):
        # Caught earlier by the arity check, but defend in depth so
        # this helper is safe to call in isolation.
        raise ValueError(
            f"Relationship binding {relationship_name!r}: {side}_columns "
            f"entry [{idx}] is a legacy string entry but endpoint "
            f"{endpoint_entity_name!r} has only "
            f"{len(endpoint_pk_properties)} primary-key column(s)."
        )
      target_property = endpoint_pk_properties[idx]
      canonical.append((entry, target_property))
      continue
    if isinstance(entry, dict):
      # The pydantic validator already guaranteed single-key str→str.
      edge_column, target_property = next(iter(entry.items()))
      if target_property not in endpoint_pk_property_set:
        raise ValueError(
            f"Relationship binding {relationship_name!r}: {side}_columns "
            f"entry [{idx}] maps {edge_column!r} → "
            f"{target_property!r}, but endpoint entity "
            f"{endpoint_entity_name!r} has no primary-key property "
            f"named {target_property!r}. Effective PK properties: "
            f"{sorted(endpoint_pk_property_set)!r}. This is FK→PK "
            "mapping; the target must be a primary-key property "
            "(including inherited PKs) so the edge uniquely "
            "identifies the target row."
        )
      canonical.append((edge_column, target_property))
      continue
    # Unreachable: the pydantic validator rejected anything else.
    raise ValueError(  # pragma: no cover
        f"Relationship binding {relationship_name!r}: {side}_columns "
        f"entry [{idx}] has unexpected type {type(entry).__name__}."
    )

  # Coverage invariant: the canonical mapping's target-property
  # sequence must be a permutation of the endpoint's effective PK
  # properties (each PK property covered exactly once). Arity has
  # already been enforced upstream (``_check_relationship_endpoint_arity``)
  # so ``len(targets) == len(endpoint_pk_properties)``; if every
  # target is also in the PK set (which the per-entry check above
  # guarantees) and the targets are unique, the three conditions
  # together imply the permutation invariant.
  #
  # Without this check a composite PK ``[k1, k2]`` could be bound as
  # two entries that both target ``k1``, leaving ``k2`` uncovered —
  # C2's materializer would then consume a canonical endpoint
  # mapping that cannot uniquely identify the target row.
  target_sequence = [target for _, target in canonical]
  if len(set(target_sequence)) != len(target_sequence):
    # Find the first duplicate for a precise error.
    seen: set[str] = set()
    duplicates: list[str] = []
    for target in target_sequence:
      if target in seen and target not in duplicates:
        duplicates.append(target)
      seen.add(target)
    raise ValueError(
        f"Relationship binding {relationship_name!r}: {side}_columns "
        f"covers some endpoint PK properties more than once "
        f"({duplicates!r}) which necessarily leaves at least one PK "
        "property uncovered. The canonical mapping must be a "
        "permutation of the endpoint's effective primary-key "
        f"properties {sorted(endpoint_pk_property_set)!r} — each PK "
        "property mapped exactly once. C2's materializer needs this "
        "invariant so the edge uniquely identifies the target row."
    )
  return tuple(canonical)


def edge_column_names(
    column_entries: list,
) -> tuple[str, ...]:
  """Extract just the edge column names from a mixed ``ColumnRef`` list.

  Used by downstream surfaces that only need the list-view of edge
  column names (the legacy ``ResolvedRelationship.from_columns`` /
  ``to_columns`` shim). The canonical
  ``(edge_column, target_property)`` form is produced by
  :func:`normalize_relationship_columns`.

  Important: returns column names in **declared binding order**, not
  in endpoint-PK declaration order. Consumers that pair these
  columns positionally with the endpoint's PK columns (DDL emitters,
  property-graph compilers) must NOT use this function for permuted
  dict-shape bindings; they should go through
  :func:`require_legacy_column_shape` to assert the legacy
  list-of-strings shape (which is implicitly PK-ordered) until they
  are wired to consume :func:`normalize_relationship_columns`
  output directly.
  """
  out: list[str] = []
  for entry in column_entries:
    if isinstance(entry, str):
      out.append(entry)
    elif isinstance(entry, dict):
      # Pydantic validator guarantees a single key.
      out.append(next(iter(entry.keys())))
  return tuple(out)


def require_legacy_column_shape(
    column_entries: list,
    *,
    consumer_name: str,
    side: str,
    relationship_name: str,
) -> tuple[str, ...]:
  """Assert ``column_entries`` uses the legacy ``list[str]`` shape.

  Returns the list-view as ``tuple[str, ...]`` when every entry is a
  bare string. Raises ``ValueError`` with a precise, actionable
  error if any entry is a dict.

  Background: PR C1 (issue #179) introduced an explicit
  ``{edge_column: target_property}`` dict-shape entry on
  ``RelationshipBinding.from_columns`` / ``to_columns`` so C2 can
  bind self-edges (same entity on both ends) by giving each side a
  distinct edge column name. The dict shape parses cleanly through
  :class:`RelationshipBinding` and resolves into the canonical
  ``ResolvedRelationship.from_column_mapping`` /
  ``to_column_mapping`` via :func:`normalize_relationship_columns`,
  but a handful of public surfaces — the property-graph DDL
  compiler in ``graph_ddl_compiler.compile_graph``, the legacy
  ``runtime_spec.graph_spec_from_ontology_binding`` converter —
  still read ``rb.from_columns`` / ``rb.to_columns`` as
  ``list[str]`` and would silently mispair edge columns against
  endpoint PK columns (or simply crash at the next ``''.join``)
  when handed a dict.

  Routing those consumers through this helper makes the boundary
  explicit: legacy bindings flow through unchanged; new dict-shape
  bindings get a clear "this consumer needs the canonical mapping"
  error pointing at the offending entry and the migration path
  (route through ``resolve()`` and consume
  ``ResolvedRelationship.from_column_mapping``). C2 lifts the
  restriction on a consumer-by-consumer basis as it wires the
  canonical mapping through them.
  """
  for idx, entry in enumerate(column_entries):
    if isinstance(entry, dict):
      raise ValueError(
          f"{consumer_name}: relationship binding "
          f"{relationship_name!r} {side}_columns[{idx}] uses the "
          f"explicit FK→PK mapping shape ({entry!r}) introduced in "
          "#179, but this consumer is not yet wired through the "
          "canonical mapping. Either revert this entry to the legacy "
          "``[edge_column_name, ...]`` list-of-strings shape, or "
          "route your code through ``resolve()`` and consume "
          "``ResolvedRelationship.from_column_mapping`` / "
          "``to_column_mapping`` directly. C2 (issue #179 follow-up) "
          "will lift this restriction."
      )
  # All entries are str at this point — the pydantic validator
  # already rejected anything else.
  return tuple(column_entries)


def _primary_key_len(entity_name: str, entity_map: dict[str, Entity]) -> int:
  """Primary-key arity of an entity, honoring inherited keys."""
  entity = entity_map[entity_name]
  keys = _effective_keys(entity, entity_map)
  if keys is None or not keys.primary:
    # The ontology loader guarantees every entity has an effective
    # primary key; treat absence as an internal invariant violation.
    raise ValueError(
        f"Entity {entity_name!r} has no effective primary key; "
        "ontology is invalid."
    )
  return len(keys.primary)


def _check_relationship_endpoint_closure(
    rb: RelationshipBinding,
    rel: Relationship,
    entity_map: dict[str, Entity],
    bound_entity_names: set[str],
) -> None:
  """Both endpoints must have ≥1 bound descendant (including themselves).

  A bound edge that points at an entity tree with no bound node has
  nothing to connect — equivalent to leaving the relationship itself
  unbound but paying the compile-time cost anyway. Treat as an error.
  """
  for side_label, endpoint in (("from", rel.from_), ("to", rel.to)):
    if not _has_bound_descendant(endpoint, entity_map, bound_entity_names):
      raise ValueError(
          f"Relationship binding {rb.name!r}: endpoint ({side_label}) "
          f"entity {endpoint!r} has no bound descendant in this binding."
      )


def _has_bound_descendant(
    endpoint: str,
    entity_map: dict[str, Entity],
    bound_entity_names: set[str],
) -> bool:
  """True iff some bound entity equals ``endpoint`` or extends it."""
  for bound in bound_entity_names:
    if _is_entity_subtype(bound, endpoint, entity_map):
      return True
  return False
