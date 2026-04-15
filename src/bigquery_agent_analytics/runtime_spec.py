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

"""Bridge between upstream ``bigquery_ontology`` models and the SDK ``GraphSpec``.

The upstream package separates concerns into an ``Ontology`` (logical schema)
and a ``Binding`` (physical BigQuery mapping).  The SDK runtime
(``OntologyGraphManager``, ``OntologyMaterializer``,
``OntologyPropertyGraphCompiler``, etc.) consumes a single ``GraphSpec``
Pydantic model that fuses both.

This module provides two converter functions:

* ``graph_spec_from_ontology_binding`` -- forward: Ontology + Binding -> GraphSpec
* ``graph_spec_to_ontology_binding`` -- reverse: GraphSpec -> (Ontology, Binding)

Usage::

    from bigquery_ontology import load_ontology, load_binding
    from bigquery_agent_analytics.runtime_spec import (
        graph_spec_from_ontology_binding,
    )

    ont = load_ontology('my_ontology.yaml')
    bnd = load_binding('my_binding.yaml', ont)
    spec = graph_spec_from_ontology_binding(ont, bnd)
    # spec is now a standard GraphSpec usable everywhere in the SDK.
"""

from __future__ import annotations

import dataclasses
import logging

from bigquery_agent_analytics.ontology_models import BindingSpec
from bigquery_agent_analytics.ontology_models import EntitySpec
from bigquery_agent_analytics.ontology_models import GraphSpec
from bigquery_agent_analytics.ontology_models import KeySpec
from bigquery_agent_analytics.ontology_models import PropertySpec
from bigquery_agent_analytics.ontology_models import RelationshipSpec
from bigquery_ontology.binding_models import Backend
from bigquery_ontology.binding_models import BigQueryTarget
from bigquery_ontology.binding_models import Binding
from bigquery_ontology.binding_models import EntityBinding
from bigquery_ontology.binding_models import PropertyBinding
from bigquery_ontology.binding_models import RelationshipBinding
from bigquery_ontology.ontology_models import Cardinality  # noqa: F401 – re-export
from bigquery_ontology.ontology_models import Entity
from bigquery_ontology.ontology_models import Keys
from bigquery_ontology.ontology_models import Ontology
from bigquery_ontology.ontology_models import Property
from bigquery_ontology.ontology_models import PropertyType
from bigquery_ontology.ontology_models import Relationship

logger = logging.getLogger('bigquery_agent_analytics.' + __name__)

# ------------------------------------------------------------------ #
# Type mapping: upstream PropertyType  <-->  SDK type strings          #
# ------------------------------------------------------------------ #

_PROPERTY_TYPE_TO_SDK: dict[PropertyType, str] = {
    PropertyType.STRING: 'string',
    PropertyType.BYTES: 'bytes',
    PropertyType.INTEGER: 'int64',
    PropertyType.DOUBLE: 'double',
    PropertyType.NUMERIC: 'double',  # narrowed, matches ttl_importer
    PropertyType.BOOLEAN: 'boolean',
    PropertyType.DATE: 'date',
    PropertyType.TIME: 'string',  # narrowed
    PropertyType.DATETIME: 'timestamp',  # narrowed
    PropertyType.TIMESTAMP: 'timestamp',
    PropertyType.JSON: 'string',  # narrowed
}

# Reverse map: SDK type string -> PropertyType (picks the canonical member
# when multiple upstream types map to the same SDK string).
_SDK_TO_PROPERTY_TYPE: dict[str, PropertyType] = {
    'string': PropertyType.STRING,
    'bytes': PropertyType.BYTES,
    'int64': PropertyType.INTEGER,
    'double': PropertyType.DOUBLE,
    'boolean': PropertyType.BOOLEAN,
    'date': PropertyType.DATE,
    'timestamp': PropertyType.TIMESTAMP,
}


# ------------------------------------------------------------------ #
# SDK-side extension: lineage edge config                              #
# ------------------------------------------------------------------ #


@dataclasses.dataclass
class LineageEdgeConfig:
  """Cross-session lineage configuration for a relationship edge.

  Specifies which columns in the edge table carry the session keys for
  the source and destination endpoints, enabling the SDK to stitch
  lineage across sessions (V5 feature).
  """

  from_session_column: str
  to_session_column: str


# ------------------------------------------------------------------ #
# Forward: Ontology + Binding -> GraphSpec                             #
# ------------------------------------------------------------------ #


def _resolve_source(raw_source: str, target: BigQueryTarget) -> str:
  """Qualify a binding source against the BigQuery target defaults.

  Rules:
  * ``project.dataset.table`` (2+ dots) -> used verbatim.
  * ``dataset.table`` (1 dot) -> ``{target.project}.dataset.table``.
  * ``table`` (0 dots) -> ``{target.project}.{target.dataset}.table``.
  """
  dot_count = raw_source.count('.')
  if dot_count >= 2:
    return raw_source
  if dot_count == 1:
    return f'{target.project}.{raw_source}'
  return f'{target.project}.{target.dataset}.{raw_source}'


def _build_property_specs(
    properties: list[Property],
    binding_props: list[PropertyBinding] | None = None,
) -> list[PropertySpec]:
  """Convert upstream ``Property`` list to SDK ``PropertySpec`` list.

  When ``binding_props`` is provided, the SDK property name is set to
  the physical column name from the binding (not the ontology property
  name). This is necessary because the SDK runtime uses
  ``PropertySpec.name`` as the physical column name in DDL and
  materialization.

  Derived properties (those with ``expr``) are rejected because the
  SDK runtime would treat them as stored columns.
  """
  # Build column mapping: ontology prop name -> physical column name.
  col_map: dict[str, str] = {}
  if binding_props:
    for bp in binding_props:
      col_map[bp.name] = bp.column

  specs: list[PropertySpec] = []
  for prop in properties:
    if prop.expr is not None:
      raise ValueError(
          f'Property {prop.name!r} has a derived expression '
          f'(expr={prop.expr!r}). The SDK runtime does not support '
          f'derived properties; they would be treated as stored '
          f'columns. Remove the expr or handle it before conversion.'
      )
    sdk_type = _PROPERTY_TYPE_TO_SDK.get(prop.type, 'string')
    # Use the binding column name if available; fall back to ontology name.
    col_name = col_map.get(prop.name, prop.name)
    specs.append(
        PropertySpec(
            name=col_name,
            type=sdk_type,
            description=prop.description or '',
        )
    )
  return specs


def _build_key_spec(
    entity: Entity,
    col_map: dict[str, str],
) -> KeySpec:
  """Extract the primary key from an upstream ``Entity``.

  Key column names are remapped through ``col_map`` (ontology prop name
  → physical column name) so they stay consistent with the renamed
  ``PropertySpec.name`` values produced by ``_build_property_specs``.
  """
  if entity.keys is not None and entity.keys.primary is not None:
    return KeySpec(primary=[col_map.get(k, k) for k in entity.keys.primary])
  raise ValueError(
      f'Entity {entity.name!r} has no primary key defined. '
      'Cannot convert to GraphSpec without a primary key.'
  )


def graph_spec_from_ontology_binding(
    ontology: Ontology,
    binding: Binding,
    lineage_config: dict[str, LineageEdgeConfig] | None = None,
) -> GraphSpec:
  """Convert an upstream Ontology + Binding into an SDK GraphSpec.

  This bridges the separated ontology/binding contract to the SDK's
  combined GraphSpec shape, enabling the SDK runtime (extraction,
  materialization, DDL, GQL) to consume upstream-formatted specs.

  Args:
      ontology: Validated upstream Ontology.
      binding: Validated upstream Binding (must reference this ontology).
      lineage_config: Optional dict mapping relationship names to
          LineageEdgeConfig for cross-session lineage edges (SDK-specific
          extension not present in upstream binding model).

  Returns:
      A GraphSpec that can be passed to OntologyGraphManager,
      OntologyMaterializer, OntologyPropertyGraphCompiler, etc.

  Raises:
      ValueError: If a bound entity/relationship is not found in the
          ontology, or if an entity has no primary key.
  """
  lineage_config = lineage_config or {}
  target = binding.target

  # Index ontology elements by name for O(1) lookup.
  ont_entity_map: dict[str, Entity] = {e.name: e for e in ontology.entities}
  ont_rel_map: dict[str, Relationship] = {
      r.name: r for r in ontology.relationships
  }

  # -- Entities --------------------------------------------------------
  # Track per-entity column mappings for relationship endpoint remapping.
  entity_col_maps: dict[str, dict[str, str]] = {}

  entity_specs: list[EntitySpec] = []
  for eb in binding.entities:
    ont_entity = ont_entity_map.get(eb.name)
    if ont_entity is None:
      raise ValueError(
          f'Binding references entity {eb.name!r} which is not '
          f'defined in ontology {ontology.ontology!r}.'
      )

    # Build column map: ontology prop name -> physical column name.
    col_map: dict[str, str] = {}
    for bp in eb.properties:
      col_map[bp.name] = bp.column
    entity_col_maps[eb.name] = col_map

    # Labels: if the entity extends another, labels = [name, extends].
    if ont_entity.extends:
      labels = [ont_entity.name, ont_entity.extends]
    else:
      labels = [ont_entity.name]

    entity_specs.append(
        EntitySpec(
            name=ont_entity.name,
            description=ont_entity.description or '',
            extends=ont_entity.extends,
            binding=BindingSpec(
                source=_resolve_source(eb.source, target),
                from_columns=None,
                to_columns=None,
            ),
            keys=_build_key_spec(ont_entity, col_map),
            properties=_build_property_specs(
                ont_entity.properties, eb.properties
            ),
            labels=labels,
        )
    )

  # -- Relationships ---------------------------------------------------
  rel_specs: list[RelationshipSpec] = []
  for rb in binding.relationships:
    ont_rel = ont_rel_map.get(rb.name)
    if ont_rel is None:
      raise ValueError(
          f'Binding references relationship {rb.name!r} which is not '
          f'defined in ontology {ontology.ontology!r}.'
      )

    # Lineage session columns (SDK extension).
    lineage = lineage_config.get(rb.name)
    from_session_col: str | None = None
    to_session_col: str | None = None
    if lineage is not None:
      from_session_col = lineage.from_session_column
      to_session_col = lineage.to_session_column

    rel_specs.append(
        RelationshipSpec(
            name=ont_rel.name,
            description=ont_rel.description or '',
            from_entity=ont_rel.from_,
            to_entity=ont_rel.to,
            binding=BindingSpec(
                source=_resolve_source(rb.source, target),
                from_columns=list(rb.from_columns),
                to_columns=list(rb.to_columns),
                from_session_column=from_session_col,
                to_session_column=to_session_col,
            ),
            properties=_build_property_specs(ont_rel.properties, rb.properties),
        )
    )

  # Warn about lineage_config keys that did not match any relationship.
  if lineage_config:
    bound_rel_names = {rb.name for rb in binding.relationships}
    unmatched = set(lineage_config) - bound_rel_names
    if unmatched:
      logger.warning(
          'lineage_config references relationships not found in the '
          'binding: %s. These lineage configurations will have no '
          'effect. Check for typos in relationship names.',
          sorted(unmatched),
      )

  return GraphSpec(
      name=ontology.ontology,
      entities=entity_specs,
      relationships=rel_specs,
  )


# ------------------------------------------------------------------ #
# Reverse: GraphSpec -> (Ontology, Binding)                            #
# ------------------------------------------------------------------ #


def _sdk_type_to_property_type(sdk_type: str) -> PropertyType:
  """Map an SDK type string back to the upstream PropertyType enum."""
  pt = _SDK_TO_PROPERTY_TYPE.get(sdk_type)
  if pt is not None:
    return pt
  # Best-effort fallback: treat unknown types as STRING.
  logger.warning(
      'Unknown SDK property type %r; defaulting to PropertyType.STRING.',
      sdk_type,
  )
  return PropertyType.STRING


def graph_spec_to_ontology_binding(
    spec: GraphSpec,
    ontology_name: str = 'converted',
    binding_name: str = 'converted_binding',
    project_id: str = '',
    dataset_id: str = '',
) -> tuple[Ontology, Binding, dict[str, LineageEdgeConfig]]:
  """Convert a GraphSpec back to separated Ontology + Binding.

  Useful for feeding existing GraphSpec YAML into upstream tools
  (``gm compile``, ``gm validate``).

  Property names are used as column names in the generated binding
  (identity mapping). Fully-qualified source references
  (``project.dataset.table``) are preserved verbatim; shorter
  references are left as-is in the binding source field.

  V5 lineage session columns (``from_session_column``,
  ``to_session_column``) are extracted into a separate
  ``LineageEdgeConfig`` dict keyed by relationship name, since the
  upstream binding model does not have these fields.

  Args:
      spec: The SDK GraphSpec to convert.
      ontology_name: Name for the generated Ontology document.
      binding_name: Name for the generated Binding document.
      project_id: BigQuery project for the binding target. If empty,
          the project is inferred from the first entity binding source
          (first dot-separated segment of a fully-qualified reference).
      dataset_id: BigQuery dataset for the binding target. If empty,
          the dataset is inferred similarly.

  Returns:
      A ``(Ontology, Binding, lineage_config)`` tuple. The
      ``lineage_config`` dict maps relationship names to
      ``LineageEdgeConfig`` for any relationships that had
      ``from_session_column`` / ``to_session_column`` set.
  """
  # -- Infer project/dataset from first entity source if not supplied --
  if (not project_id or not dataset_id) and spec.entities:
    parts = spec.entities[0].binding.source.split('.')
    if len(parts) >= 3:
      if not project_id:
        project_id = parts[0]
      if not dataset_id:
        dataset_id = parts[1]
  project_id = project_id or 'default_project'
  dataset_id = dataset_id or 'default_dataset'

  target_prefix = f'{project_id}.{dataset_id}.'

  # -- Entities --------------------------------------------------------
  ont_entities: list[Entity] = []
  bnd_entities: list[EntityBinding] = []

  for es in spec.entities:
    # Ontology entity.
    ont_props = [
        Property(
            name=ps.name,
            type=_sdk_type_to_property_type(ps.type),
            description=ps.description or None,
        )
        for ps in es.properties
    ]
    ont_entities.append(
        Entity(
            name=es.name,
            extends=es.extends,
            keys=Keys(primary=list(es.keys.primary)),
            properties=ont_props,
            description=es.description or None,
        )
    )

    # Binding entity: identity column mapping.
    bnd_props = [
        PropertyBinding(name=ps.name, column=ps.name) for ps in es.properties
    ]
    # Strip target prefix from source to produce a relative reference
    # when the source starts with the target project.dataset.
    source = es.binding.source
    if source.startswith(target_prefix):
      source = source[len(target_prefix) :]

    bnd_entities.append(
        EntityBinding(
            name=es.name,
            source=source,
            properties=bnd_props,
        )
    )

  # -- Relationships ---------------------------------------------------
  ont_relationships: list[Relationship] = []
  bnd_relationships: list[RelationshipBinding] = []
  lineage_config: dict[str, LineageEdgeConfig] = {}

  for rs in spec.relationships:
    ont_props = [
        Property(
            name=ps.name,
            type=_sdk_type_to_property_type(ps.type),
            description=ps.description or None,
        )
        for ps in rs.properties
    ]
    ont_relationships.append(
        Relationship(
            name=rs.name,
            **{'from': rs.from_entity},
            to=rs.to_entity,
            properties=ont_props,
            description=rs.description or None,
        )
    )

    bnd_props = [
        PropertyBinding(name=ps.name, column=ps.name) for ps in rs.properties
    ]
    source = rs.binding.source
    if source.startswith(target_prefix):
      source = source[len(target_prefix) :]

    bnd_relationships.append(
        RelationshipBinding(
            name=rs.name,
            source=source,
            from_columns=list(rs.binding.from_columns or []),
            to_columns=list(rs.binding.to_columns or []),
            properties=bnd_props,
        )
    )

    # Preserve V5 lineage session columns as a sidecar config.
    if rs.binding.from_session_column and rs.binding.to_session_column:
      lineage_config[rs.name] = LineageEdgeConfig(
          from_session_column=rs.binding.from_session_column,
          to_session_column=rs.binding.to_session_column,
      )

  # -- Assemble --------------------------------------------------------
  ontology = Ontology(
      ontology=ontology_name,
      entities=ont_entities,
      relationships=ont_relationships,
  )

  binding_obj = Binding(
      binding=binding_name,
      ontology=ontology_name,
      target=BigQueryTarget(
          backend=Backend.BIGQUERY,
          project=project_id,
          dataset=dataset_id,
      ),
      entities=bnd_entities,
      relationships=bnd_relationships,
  )

  return ontology, binding_obj, lineage_config
