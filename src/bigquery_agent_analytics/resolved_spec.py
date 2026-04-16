# src/bigquery_agent_analytics/resolved_spec.py
"""Resolved runtime specification built from Ontology + Binding.

A ``ResolvedGraph`` is the internal runtime currency of the SDK. It
fuses an upstream ``Ontology`` (logical schema) with a ``Binding``
(physical mapping) into a single resolved view where:

  - Sources are fully qualified (``project.dataset.table``).
  - Property names are physical column names (from the binding).
  - Key columns are remapped to physical column names.
  - Labels are derived from ``extends`` chains.
  - Lineage session columns are carried as SDK-specific config.
  - Metadata columns (``session_id``, ``extracted_at``) are declared.

The ``resolve()`` builder is the single place where ontology/binding
impedance matching happens. All downstream consumers read resolved
fields without reimplementing the mapping logic.
"""

from __future__ import annotations

import dataclasses
import logging
from typing import Optional

logger = logging.getLogger("bigquery_agent_analytics." + __name__)


@dataclasses.dataclass(frozen=True)
class ResolvedProperty:
  """One property in the resolved runtime view.

  ``column`` is the physical column name (from the binding).
  ``logical_name`` is the ontology property name (may differ from
  column when the binding renames). ``sdk_type`` is the SDK type
  string (e.g. ``"string"``, ``"int64"``, ``"timestamp"``).
  """

  column: str
  logical_name: str
  sdk_type: str
  description: str = ""


@dataclasses.dataclass(frozen=True)
class ResolvedEntity:
  """One entity in the resolved runtime view.

  ``source`` is the fully qualified BigQuery table reference.
  ``key_columns`` are physical column names for the primary key.
  ``labels`` are derived from the entity name and ``extends`` chain.
  ``properties`` are in ontology declaration order.
  ``metadata_columns`` lists runtime columns the SDK injects
  (default: ``session_id``, ``extracted_at``).
  """

  name: str
  source: str
  key_columns: tuple[str, ...]
  labels: tuple[str, ...]
  properties: tuple[ResolvedProperty, ...]
  description: str = ""
  extends: Optional[str] = None
  metadata_columns: tuple[str, ...] = ("session_id", "extracted_at")
  # Logical ontology key names for lossless reverse conversion.
  ontology_key_primary: Optional[tuple[str, ...]] = None


@dataclasses.dataclass(frozen=True)
class ResolvedRelationship:
  """One relationship in the resolved runtime view.

  ``from_columns`` / ``to_columns`` are the binding's endpoint join
  columns. ``from_session_column`` / ``to_session_column`` are the
  SDK-specific lineage session overrides (None if not configured).
  ``properties`` are in ontology declaration order.

  ``ontology_key_primary`` / ``ontology_key_additional`` carry the
  original ontology-level key declarations (using logical property
  names) so that ``resolved_graph_to_ontology_binding()`` can
  reconstruct a valid upstream Ontology without information loss.
  """

  name: str
  source: str
  from_entity: str
  to_entity: str
  from_columns: tuple[str, ...]
  to_columns: tuple[str, ...]
  properties: tuple[ResolvedProperty, ...]
  description: str = ""
  from_session_column: Optional[str] = None
  to_session_column: Optional[str] = None
  metadata_columns: tuple[str, ...] = ("session_id", "extracted_at")
  ontology_key_primary: Optional[tuple[str, ...]] = None
  ontology_key_additional: Optional[tuple[str, ...]] = None


@dataclasses.dataclass(frozen=True)
class ResolvedGraph:
  """Complete resolved runtime specification.

  Built once from ``Ontology`` + ``Binding`` via ``resolve()``.
  All downstream SDK modules consume this — extraction, materialization,
  DDL compilation, GQL generation.
  """

  name: str
  entities: tuple[ResolvedEntity, ...]
  relationships: tuple[ResolvedRelationship, ...]


@dataclasses.dataclass(frozen=True)
class LineageEdgeConfig:
  """Cross-session lineage configuration for a relationship edge."""

  from_session_column: str
  to_session_column: str


# -- Type mapping: upstream PropertyType -> SDK type string ------------

_PROPERTY_TYPE_TO_SDK: dict[str, str] = {
    "string": "string",
    "bytes": "bytes",
    "integer": "int64",
    "double": "double",
    "numeric": "double",
    "boolean": "boolean",
    "date": "date",
    "time": "string",
    "datetime": "timestamp",
    "timestamp": "timestamp",
    "json": "string",
}


# -- Source qualification -----------------------------------------------


def _qualify_source(raw_source: str, project: str, dataset: str) -> str:
  """Qualify a binding source to a fully-qualified BQ table reference.

  Rules (matching runtime_spec._resolve_source):
  * 2+ dots -> used verbatim (already fully qualified).
  * 1 dot   -> ``{project}.{raw_source}`` (dataset.table).
  * 0 dots  -> ``{project}.{dataset}.{raw_source}`` (bare table).
  """
  dot_count = raw_source.count(".")
  if dot_count >= 2:
    return raw_source
  if dot_count == 1:
    return f"{project}.{raw_source}"
  return f"{project}.{dataset}.{raw_source}"


# -- Inheritance helpers (inlined to avoid depending on private upstream API)


def _ancestors(name, item_map):
  """Yield ancestor items (excluding self) walking ``extends``."""
  cur = item_map[name].extends
  while cur is not None:
    parent = item_map[cur]
    yield parent
    cur = parent.extends


def _effective_keys(item, item_map):
  """Resolve keys, walking up ``extends`` if not declared locally."""
  keys = getattr(item, "keys", None)
  if keys is not None and (keys.primary or keys.additional or keys.alternate):
    return keys
  for ancestor in _ancestors(item.name, item_map):
    akeys = getattr(ancestor, "keys", None)
    if akeys is not None and (
        akeys.primary or akeys.additional or akeys.alternate
    ):
      return akeys
  return None


def _effective_properties(item, item_map):
  """All properties visible on ``item`` including inherited ones."""
  out = {}
  for ancestor in reversed(list(_ancestors(item.name, item_map))):
    for p in ancestor.properties:
      out[p.name] = p
  for p in item.properties:
    out[p.name] = p
  return out


# -- Builder ------------------------------------------------------------


def resolve(
    ontology,
    binding,
    lineage_config: dict[str, LineageEdgeConfig] | None = None,
) -> ResolvedGraph:
  """Build a ``ResolvedGraph`` from an upstream Ontology + Binding.

  This is the single place where ontology/binding impedance matching
  happens. All downstream SDK modules should consume the resolved
  output rather than re-implementing the mapping.

  Args:
      ontology: A validated ``bigquery_ontology.Ontology``.
      binding: A validated ``bigquery_ontology.Binding`` referencing
          this ontology.
      lineage_config: Optional dict mapping relationship names to
          ``LineageEdgeConfig`` for cross-session lineage edges.

  Returns:
      A frozen ``ResolvedGraph`` ready for consumption by SDK runtime.

  Raises:
      ValueError: If a bound entity/relationship is not found in the
          ontology, or if an entity has no primary key.
  """
  lineage_config = lineage_config or {}
  project = binding.target.project
  dataset = binding.target.dataset

  ont_entity_map = {e.name: e for e in ontology.entities}
  ont_rel_map = {r.name: r for r in ontology.relationships}

  # -- Entities --------------------------------------------------------
  resolved_entities: list[ResolvedEntity] = []
  for eb in binding.entities:
    ont_entity = ont_entity_map.get(eb.name)
    if ont_entity is None:
      raise ValueError(
          f"Binding references entity {eb.name!r} which is not "
          f"defined in ontology {ontology.ontology!r}."
      )

    col_map: dict[str, str] = {bp.name: bp.column for bp in eb.properties}

    # Use _effective_keys to walk extends chain for inherited keys.
    eff_keys = _effective_keys(ont_entity, ont_entity_map)
    if eff_keys is None or eff_keys.primary is None:
      raise ValueError(
          f"Entity {eb.name!r} has no effective primary key defined "
          f"(checked own keys and ancestor chain)."
      )
    key_columns = tuple(col_map.get(k, k) for k in eff_keys.primary)

    labels: tuple[str, ...]
    if ont_entity.extends:
      labels = (ont_entity.name, ont_entity.extends)
    else:
      labels = (ont_entity.name,)

    # Use _effective_properties to include inherited properties.
    eff_props = _effective_properties(ont_entity, ont_entity_map)
    properties: list[ResolvedProperty] = []
    for prop in eff_props.values():
      if prop.expr is not None:
        raise ValueError(
            f"Property {prop.name!r} on entity {ont_entity.name!r} has "
            f"a derived expression (expr={prop.expr!r}). The SDK "
            f"runtime does not support derived properties; they would "
            f"be treated as stored columns. Remove the expr or handle "
            f"it before conversion."
        )
      sdk_type = _PROPERTY_TYPE_TO_SDK.get(prop.type.value, "string")
      properties.append(
          ResolvedProperty(
              column=col_map.get(prop.name, prop.name),
              logical_name=prop.name,
              sdk_type=sdk_type,
              description=prop.description or "",
          )
      )

    resolved_entities.append(
        ResolvedEntity(
            name=ont_entity.name,
            source=_qualify_source(eb.source, project, dataset),
            key_columns=key_columns,
            labels=labels,
            properties=tuple(properties),
            description=ont_entity.description or "",
            extends=ont_entity.extends,
            ontology_key_primary=tuple(eff_keys.primary),
        )
    )

  # -- Relationships ---------------------------------------------------
  resolved_rels: list[ResolvedRelationship] = []
  for rb in binding.relationships:
    ont_rel = ont_rel_map.get(rb.name)
    if ont_rel is None:
      raise ValueError(
          f"Binding references relationship {rb.name!r} which is not "
          f"defined in ontology {ontology.ontology!r}."
      )

    col_map = {bp.name: bp.column for bp in rb.properties}

    # Use _effective_properties to include inherited relationship properties.
    eff_props = _effective_properties(ont_rel, ont_rel_map)
    properties = []
    for prop in eff_props.values():
      if prop.expr is not None:
        raise ValueError(
            f"Property {prop.name!r} on relationship {ont_rel.name!r} "
            f"has a derived expression (expr={prop.expr!r}). The SDK "
            f"runtime does not support derived properties."
        )
      sdk_type = _PROPERTY_TYPE_TO_SDK.get(prop.type.value, "string")
      properties.append(
          ResolvedProperty(
              column=col_map.get(prop.name, prop.name),
              logical_name=prop.name,
              sdk_type=sdk_type,
              description=prop.description or "",
          )
      )

    lineage = lineage_config.get(rb.name)
    from_session: str | None = None
    to_session: str | None = None
    if lineage is not None:
      from_session = lineage.from_session_column
      to_session = lineage.to_session_column

    # Carry ontology-level key declarations for lossless reverse conversion.
    ont_key_primary: tuple[str, ...] | None = None
    ont_key_additional: tuple[str, ...] | None = None
    if ont_rel.keys is not None:
      if ont_rel.keys.primary:
        ont_key_primary = tuple(ont_rel.keys.primary)
      if ont_rel.keys.additional:
        ont_key_additional = tuple(ont_rel.keys.additional)

    resolved_rels.append(
        ResolvedRelationship(
            name=ont_rel.name,
            source=_qualify_source(rb.source, project, dataset),
            from_entity=ont_rel.from_,
            to_entity=ont_rel.to,
            from_columns=tuple(rb.from_columns),
            to_columns=tuple(rb.to_columns),
            properties=tuple(properties),
            description=ont_rel.description or "",
            from_session_column=from_session,
            to_session_column=to_session,
            ontology_key_primary=ont_key_primary,
            ontology_key_additional=ont_key_additional,
        )
    )

  # Warn about lineage_config keys that did not match any relationship.
  if lineage_config:
    bound_rel_names = {rb.name for rb in binding.relationships}
    unmatched = set(lineage_config) - bound_rel_names
    if unmatched:
      logger.warning(
          "lineage_config references relationships not found in the "
          "binding: %s. These lineage configurations will have no "
          "effect. Check for typos in relationship names.",
          sorted(unmatched),
      )

  return ResolvedGraph(
      name=ontology.ontology,
      entities=tuple(resolved_entities),
      relationships=tuple(resolved_rels),
  )


# -- Legacy bridge: GraphSpec → ResolvedGraph --------------------------------


def resolve_from_graph_spec(spec) -> ResolvedGraph:
  """Convert a legacy ``GraphSpec`` into a ``ResolvedGraph``.

  This is a transitional bridge for code paths that still load from
  combined graph-spec YAML (e.g. CLI commands).  Once all entry points
  are migrated to separated ontology+binding YAML, this function is
  deleted.

  The ``GraphSpec`` is already a resolved view (property names are
  physical column names, sources are fully qualified), so this is a
  straightforward field-by-field copy with type changes.
  """
  entities: list[ResolvedEntity] = []
  for e in spec.entities:
    props = tuple(
        ResolvedProperty(
            column=p.name,
            logical_name=p.name,
            sdk_type=p.type,
            description=getattr(p, "description", ""),
        )
        for p in e.properties
    )
    session_col = getattr(e.binding, "from_session_column", None)
    # GraphSpec has already lost logical names — key_columns and
    # ontology_key_primary are both the physical column name.
    key_cols = tuple(e.keys.primary)
    entities.append(
        ResolvedEntity(
            name=e.name,
            source=e.binding.source,
            key_columns=key_cols,
            labels=tuple(getattr(e, "labels", [e.name])),
            properties=props,
            description=getattr(e, "description", ""),
            extends=getattr(e, "extends", None),
            ontology_key_primary=key_cols,
        )
    )

  relationships: list[ResolvedRelationship] = []
  for r in spec.relationships:
    props = tuple(
        ResolvedProperty(
            column=p.name,
            logical_name=p.name,
            sdk_type=p.type,
            description=getattr(p, "description", ""),
        )
        for p in r.properties
    )
    relationships.append(
        ResolvedRelationship(
            name=r.name,
            source=r.binding.source,
            from_entity=r.from_entity,
            to_entity=r.to_entity,
            from_columns=tuple(r.binding.from_columns or []),
            to_columns=tuple(r.binding.to_columns or []),
            properties=props,
            description=getattr(r, "description", ""),
            from_session_column=getattr(r.binding, "from_session_column", None),
            to_session_column=getattr(r.binding, "to_session_column", None),
        )
    )

  return ResolvedGraph(
      name=spec.name,
      entities=tuple(entities),
      relationships=tuple(relationships),
  )


def load_resolved_graph(path: str, env: str | None = None) -> ResolvedGraph:
  """Load a combined graph-spec YAML file into a ResolvedGraph.

  This is the migration-compatible replacement for ``load_graph_spec()``.
  It reads the combined YAML, builds the legacy GraphSpec internally,
  and converts it to a ResolvedGraph.
  """
  from .ontology_models import load_graph_spec

  return resolve_from_graph_spec(load_graph_spec(path, env=env))
