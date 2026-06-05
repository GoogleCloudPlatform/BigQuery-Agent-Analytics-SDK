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

"""Synthesise an in-memory ontology + binding from a property graph.

Building block 3 of deriving a materialization spec from the property graph
alone (GitHub issue #277). Given the types-free
:class:`~bigquery_ontology.graph_ddl_parser.ParsedPropertyGraph` (PR 1) and the
:class:`~bigquery_ontology.graph_schema_join.GraphColumnTypes` recovered from
the table schemas (PR 2), this module produces the upstream
:class:`~bigquery_ontology.ontology_models.Ontology` and
:class:`~bigquery_ontology.binding_models.Binding` objects that the SDK's
existing ``resolve()`` already consumes -- so nothing downstream changes.

Mapping rules
-------------

* **Entity** = one node table; its name is the node ``LABEL``. The node alias
  (the DDL-local table handle) is used only to wire edges (``REFERENCES``).
* **Relationship** = one edge table; its name is the edge ``LABEL``; its
  endpoints are the entity names of the referenced node aliases.
* **Property types** come from the joined table schema (PR 2), never from a
  hand-written ``type:`` field.
* **Primary key** = the node ``KEY`` columns, mapped to their property names; a
  key column without a stored property gets a passthrough property.
* **SDK metadata columns** (``session_id`` / ``extracted_at``) are stripped from
  properties and keys -- the resolver re-injects them as ``metadata_columns``.
* **Derived ``(expr) AS name`` properties are skipped**: they have no physical
  column to read or write and ``resolve()`` rejects ``expr`` for
  materialization. (Keep an explicit ontology/binding if you need them.)

Limitations (raise rather than guess; provide an explicit ontology/binding for
these): multi-label nodes (label inheritance) are not synthesised, and free-text
descriptions/synonyms are not recoverable from the DDL or schema. Descriptions
are absent in derived mode (best paired with ``--extraction-mode=compiled-only``,
where they are unused).

Scope (per #277): produce the Ontology + Binding. Wiring this into the CLI
(``bqaa context-graph --property-graph ...``) is PR 4.
"""

from __future__ import annotations

from typing import Optional, Sequence

from .binding_models import Backend
from .binding_models import BigQueryTarget
from .binding_models import Binding
from .binding_models import EntityBinding
from .binding_models import PropertyBinding
from .binding_models import RelationshipBinding
from .graph_ddl_parser import ParsedEdgeTable
from .graph_ddl_parser import ParsedNodeTable
from .graph_ddl_parser import ParsedPropertyGraph
from .graph_schema_join import GraphColumnTypes
from .ontology_models import Entity
from .ontology_models import Keys
from .ontology_models import Ontology
from .ontology_models import Property
from .ontology_models import Relationship

__all__ = [
    "GraphSpecSynthesisError",
    "derive_ontology_binding",
]

_DEFAULT_METADATA_COLUMNS = ("session_id", "extracted_at")


class GraphSpecSynthesisError(ValueError):
  """Raised when a property graph cannot be turned into an ontology+binding."""


def _single_label(alias: str, labels: Sequence[str], kind: str) -> str:
  """Return the sole label, or fail: multi-label is not synthesised."""
  if len(labels) != 1:
    raise GraphSpecSynthesisError(
        f"{kind} {alias!r} has {len(labels)} labels {list(labels)}; "
        "schema-derived mode supports exactly one label per table. Provide an "
        "explicit ontology/binding to model label inheritance."
    )
  return labels[0]


def _build_entity(
    node: ParsedNodeTable,
    entity_name: str,
    column_types: dict,
    metadata: frozenset,
) -> tuple[Entity, EntityBinding, dict[str, str]]:
  """Build the Entity + EntityBinding for one node table.

  Also returns the node's ``{column -> property_name}`` map, which edges use to
  translate their ``REFERENCES`` physical PK columns into endpoint property
  names.
  """
  # Stored, non-metadata properties (derived properties have column=None).
  column_to_name: dict[str, str] = {}
  ordered: list[tuple[str, str]] = []  # (property_name, column)
  for prop in node.properties:
    if prop.column is None or prop.column in metadata:
      continue
    column_to_name[prop.column] = prop.name
    ordered.append((prop.name, prop.column))

  # Every non-metadata key column must be a property; synthesise a passthrough
  # for any key column that is not already a stored property.
  key_columns = [c for c in node.key_columns if c not in metadata]
  if not key_columns:
    raise GraphSpecSynthesisError(
        f"Node {node.alias!r} has no non-metadata KEY column to use as a "
        "primary key."
    )
  for column in key_columns:
    if column not in column_to_name:
      column_to_name[column] = column
      ordered.append((column, column))

  properties: list[Property] = []
  property_bindings: list[PropertyBinding] = []
  for name, column in ordered:
    if column not in column_types:
      # resolve_graph_column_types types every referenced column, so this only
      # fires if a caller passes mismatched parsed-graph / types objects.
      raise GraphSpecSynthesisError(
          f"Node {node.alias!r} column {column!r} has no resolved type; the "
          "parsed graph and the column types do not match."
      )
    properties.append(Property(name=name, type=column_types[column]))
    property_bindings.append(PropertyBinding(name=name, column=column))

  primary = [column_to_name[c] for c in key_columns]
  entity = Entity(
      name=entity_name,
      keys=Keys(primary=primary),
      properties=properties,
  )
  entity_binding = EntityBinding(
      name=entity_name, source=node.source, properties=property_bindings
  )
  return entity, entity_binding, column_to_name


def _endpoint_column_refs(
    edge_alias: str,
    side: str,
    fk_columns: tuple[str, ...],
    ref_columns: tuple[str, ...],
    target_column_to_name: dict[str, str],
    metadata: frozenset,
) -> list[dict[str, str]]:
  """Build explicit ``{edge_fk_column: endpoint_pk_property}`` ColumnRefs.

  The property graph's ``SOURCE/DESTINATION KEY (fk...) REFERENCES Node
  (pk...)`` clause pairs each edge FK column with a specific physical PK column
  of the endpoint, positionally. We preserve that pairing as dict-shape
  ColumnRefs rather than a bare column list: a bare list is interpreted by the
  resolver positionally against the endpoint's PK *declaration* order, which
  silently mismatches whenever the REFERENCES order differs from the KEY order
  (composite-key permutation) or the PK column is a renamed property.
  Metadata columns (e.g. ``session_id``) are dropped as paired entries.
  """
  if len(fk_columns) != len(ref_columns):
    raise GraphSpecSynthesisError(
        f"Edge {edge_alias!r} {side} KEY has {len(fk_columns)} columns but its"
        f" REFERENCES has {len(ref_columns)}; they must pair one-to-one."
    )
  refs: list[dict[str, str]] = []
  for fk_column, ref_column in zip(fk_columns, ref_columns):
    if fk_column in metadata or ref_column in metadata:
      continue
    target_property = target_column_to_name.get(ref_column)
    if target_property is None:
      raise GraphSpecSynthesisError(
          f"Edge {edge_alias!r} {side} REFERENCES column {ref_column!r}, which"
          " is not a key property of the referenced entity."
      )
    refs.append({fk_column: target_property})
  if not refs:
    raise GraphSpecSynthesisError(
        f"Edge {edge_alias!r} has no non-metadata {side} key columns to bind"
        " its endpoint."
    )
  return refs


def _build_relationship(
    edge: ParsedEdgeTable,
    rel_name: str,
    from_entity: str,
    to_entity: str,
    from_column_to_name: dict[str, str],
    to_column_to_name: dict[str, str],
    column_types: dict,
    metadata: frozenset,
) -> tuple[Relationship, RelationshipBinding]:
  """Build the Relationship + RelationshipBinding for one edge table."""
  properties: list[Property] = []
  property_bindings: list[PropertyBinding] = []
  for prop in edge.properties:
    if prop.column is None or prop.column in metadata:
      continue
    if prop.column not in column_types:
      raise GraphSpecSynthesisError(
          f"Edge {edge.alias!r} column {prop.column!r} has no resolved type; "
          "the parsed graph and the column types do not match."
      )
    properties.append(Property(name=prop.name, type=column_types[prop.column]))
    property_bindings.append(
        PropertyBinding(name=prop.name, column=prop.column)
    )

  from_columns = _endpoint_column_refs(
      edge.alias,
      "SOURCE",
      edge.source_key_columns,
      edge.source_ref_columns,
      from_column_to_name,
      metadata,
  )
  to_columns = _endpoint_column_refs(
      edge.alias,
      "DESTINATION",
      edge.dest_key_columns,
      edge.dest_ref_columns,
      to_column_to_name,
      metadata,
  )

  relationship = Relationship(
      name=rel_name, from_=from_entity, to=to_entity, properties=properties
  )
  relationship_binding = RelationshipBinding(
      name=rel_name,
      source=edge.source,
      from_columns=from_columns,
      to_columns=to_columns,
      properties=property_bindings,
  )
  return relationship, relationship_binding


def derive_ontology_binding(
    graph: ParsedPropertyGraph,
    column_types: GraphColumnTypes,
    *,
    project: str,
    dataset: str,
    binding_name: Optional[str] = None,
    metadata_columns: Sequence[str] = _DEFAULT_METADATA_COLUMNS,
) -> tuple[Ontology, Binding]:
  """Synthesise an ``Ontology`` + ``Binding`` from a parsed graph and its types.

  Args:
    graph: The parsed property graph (see :mod:`graph_ddl_parser`).
    column_types: Resolved column types (see :mod:`graph_schema_join`).
    project: BigQuery project for the binding target.
    dataset: BigQuery dataset for the binding target.
    binding_name: Name for the binding document (defaults to
      ``"<graph>_binding"``).
    metadata_columns: SDK runtime columns to strip from properties/keys; the
      resolver re-injects them. Defaults to ``("session_id", "extracted_at")``.

  Returns:
    ``(ontology, binding)`` -- validated upstream models ready for the SDK's
    ``resolve(ontology, binding)``.

  Raises:
    GraphSpecSynthesisError: For graphs outside the synthesisable subset
      (multi-label nodes, duplicate entity/relationship labels, missing primary
      key, dangling edge endpoints, mismatched SOURCE/DESTINATION key vs.
      REFERENCES arity, or a parsed-graph / column-types mismatch).
  """
  metadata = frozenset(metadata_columns)

  alias_to_entity: dict[str, str] = {}
  alias_to_column_name: dict[str, dict[str, str]] = {}
  entity_names: set[str] = set()
  entities: list[Entity] = []
  entity_bindings: list[EntityBinding] = []
  for node in graph.nodes:
    entity_name = _single_label(node.alias, node.labels, "Node")
    if node.alias in alias_to_entity:
      raise GraphSpecSynthesisError(
          f"Duplicate node alias {node.alias!r} in the property graph."
      )
    if entity_name in entity_names:
      raise GraphSpecSynthesisError(
          f"Duplicate entity name {entity_name!r}: two node tables share the"
          " LABEL. Give them distinct labels or provide an explicit"
          " ontology/binding."
      )
    types = column_types.node(node.alias).column_types
    entity, entity_binding, column_to_name = _build_entity(
        node, entity_name, dict(types), metadata
    )
    alias_to_entity[node.alias] = entity_name
    alias_to_column_name[node.alias] = column_to_name
    entity_names.add(entity_name)
    entities.append(entity)
    entity_bindings.append(entity_binding)

  relationship_names: set[str] = set()
  relationships: list[Relationship] = []
  relationship_bindings: list[RelationshipBinding] = []
  for edge in graph.edges:
    rel_name = _single_label(edge.alias, edge.labels, "Edge")
    if rel_name in relationship_names:
      raise GraphSpecSynthesisError(
          f"Duplicate relationship name {rel_name!r}: two edge tables share the"
          " LABEL. Give them distinct labels or provide an explicit"
          " ontology/binding."
      )
    if edge.source_ref_alias not in alias_to_entity:
      raise GraphSpecSynthesisError(
          f"Edge {edge.alias!r} SOURCE references unknown node alias "
          f"{edge.source_ref_alias!r}."
      )
    if edge.dest_ref_alias not in alias_to_entity:
      raise GraphSpecSynthesisError(
          f"Edge {edge.alias!r} DESTINATION references unknown node alias "
          f"{edge.dest_ref_alias!r}."
      )
    types = column_types.edge(edge.alias).column_types
    relationship, relationship_binding = _build_relationship(
        edge,
        rel_name,
        alias_to_entity[edge.source_ref_alias],
        alias_to_entity[edge.dest_ref_alias],
        alias_to_column_name[edge.source_ref_alias],
        alias_to_column_name[edge.dest_ref_alias],
        dict(types),
        metadata,
    )
    relationship_names.add(rel_name)
    relationships.append(relationship)
    relationship_bindings.append(relationship_binding)

  ontology = Ontology(
      ontology=graph.name, entities=entities, relationships=relationships
  )
  binding = Binding(
      binding=binding_name or f"{graph.name}_binding",
      ontology=graph.name,
      target=BigQueryTarget(
          backend=Backend.BIGQUERY, project=project, dataset=dataset
      ),
      entities=entity_bindings,
      relationships=relationship_bindings,
  )
  return ontology, binding
