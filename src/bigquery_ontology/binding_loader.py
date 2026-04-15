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
  for rb in binding.relationships:
    if rb.name not in rel_map:
      raise ValueError(
          f"Relationship binding {rb.name!r} does not name a declared "
          f"relationship in ontology {binding.ontology!r}."
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
  """``from_columns`` / ``to_columns`` arity must match the endpoint keys."""
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
