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

"""Schema compiler for ontology-driven AI.GENERATE extraction.

Converts a validated ``ResolvedGraph`` into a BigQuery ``output_schema``
JSON string compatible with ``AI.GENERATE``.  The generated schema
instructs the LLM to return a structured object with ``nodes`` and
``edges`` arrays, each typed according to the ontology.

The schema uses a JSON Schema dialect (nested ARRAY<OBJECT> with
typed property fields).

The current BigQuery ``AI.GENERATE`` parser rejects JSON-Schema
strings passed via ``output_schema =>``; consumers must instead
concatenate the schema text into the prompt itself, ask the model
to return JSON conforming to it, and parse the result with
``JSON_EXTRACT_*``. This is what ``ontology_graph.OntologyGraphManager``
already does.

Example usage::

    from bigquery_agent_analytics.resolved_spec import load_resolved_graph
    from bigquery_agent_analytics.ontology_schema_compiler import (
        compile_output_schema,
    )

    spec = load_resolved_graph("examples/ymgo_graph_spec.yaml", env="p.d")
    schema_json = compile_output_schema(spec)
    # Concatenate into the AI.GENERATE prompt; do NOT pass it via
    # `output_schema =>` (current BQ rejects JSON-Schema strings).
    prompt = base_prompt + "\\n\\nReturn a single JSON object:\\n" + schema_json
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from .resolved_spec import ResolvedEntity
from .resolved_spec import ResolvedGraph
from .resolved_spec import ResolvedRelationship

logger = logging.getLogger("bigquery_agent_analytics." + __name__)

# ------------------------------------------------------------------ #
# Type mapping: YAML property types -> BQ JSON Schema types            #
# ------------------------------------------------------------------ #

_TYPE_MAP: dict[str, str] = {
    "string": "STRING",
    "int64": "INTEGER",
    "double": "NUMBER",
    "float64": "NUMBER",
    "bool": "BOOLEAN",
    "boolean": "BOOLEAN",
    "timestamp": "STRING",
    "date": "STRING",
    "bytes": "STRING",
}


def _bq_schema_type(yaml_type: str) -> str:
  """Map a YAML property type to a BQ JSON Schema type.

  Raises:
      ValueError: If the type is not recognized.
  """
  normalized = yaml_type.strip().lower()
  if normalized not in _TYPE_MAP:
    raise ValueError(
        f"Unsupported property type {yaml_type!r}. "
        f"Supported types: {sorted(_TYPE_MAP.keys())}."
    )
  return _TYPE_MAP[normalized]


# ------------------------------------------------------------------ #
# Schema compilation                                                   #
# ------------------------------------------------------------------ #


def _compile_entity_schema(entity: ResolvedEntity) -> dict:
  """Build the JSON Schema object for a single entity type."""
  props: dict = {
      "entity_name": {"type": "STRING"},
  }
  for prop in entity.properties:
    props[prop.column] = {"type": _bq_schema_type(prop.sdk_type)}
  return {
      "type": "OBJECT",
      "properties": props,
  }


def _compile_relationship_schema(rel: ResolvedRelationship) -> dict:
  """Build the JSON Schema object for a single relationship type."""
  props: dict = {
      "relationship_name": {"type": "STRING"},
      "from_entity_name": {"type": "STRING"},
      "to_entity_name": {"type": "STRING"},
      "from_keys": {
          "type": "OBJECT",
          "properties": {},
      },
      "to_keys": {
          "type": "OBJECT",
          "properties": {},
      },
  }
  for prop in rel.properties:
    props[prop.column] = {"type": _bq_schema_type(prop.sdk_type)}
  return {
      "type": "OBJECT",
      "properties": props,
  }


def compile_output_schema(
    spec: ResolvedGraph,
    entity_names: Optional[list[str]] = None,
) -> str:
  """Compile a ``ResolvedGraph`` into a BQ ``output_schema`` JSON string.

  The output schema instructs ``AI.GENERATE`` to return::

      {
        "nodes": [
          {"entity_name": "...", <typed properties>},
          ...
        ],
        "edges": [
          {"relationship_name": "...", "from_entity_name": "...",
           "to_entity_name": "...",
           "from_keys": {"col1": "val1", ...},
           "to_keys": {"col1": "val1", ...},
           <typed properties>},
          ...
        ]
      }

  ``from_keys`` and ``to_keys`` are typed OBJECT fields whose
  properties match the source/target entity primary key columns.
  This preserves composite keys for downstream routing/DDL phases.

  Args:
      spec: A validated ``ResolvedGraph``.
      entity_names: Optional subset of entity names to include.
          If ``None``, all entities and relationships are included.

  Returns:
      A compact JSON string suitable for ``output_schema =>``.

  Raises:
      ValueError: If an entity name is not found in the spec, a
          property type is unsupported, or property names collide
          with incompatible types across entities/relationships.
  """
  entity_map = {e.name: e for e in spec.entities}

  if entity_names is not None:
    for name in entity_names:
      if name not in entity_map:
        raise ValueError(
            f"Entity {name!r} not found in spec. "
            f"Available: {sorted(entity_map.keys())}."
        )
    entities = [entity_map[n] for n in entity_names]
    entity_set = set(entity_names)
    relationships = [
        r
        for r in spec.relationships
        if r.from_entity in entity_set and r.to_entity in entity_set
    ]
  else:
    entities = list(spec.entities)
    relationships = list(spec.relationships)

  # Build per-entity schemas as a union (anyOf-style via items).
  # BQ AI.GENERATE does not support anyOf, so we merge all entity
  # properties into a single flat object schema.  The entity_name
  # field disambiguates at hydration time.
  #
  # Collision detection: if the same property name appears with
  # different types across entities, raise immediately.
  node_props: dict = {"entity_name": {"type": "STRING"}}
  node_type_registry: dict[str, tuple[str, str]] = {}
  for entity in entities:
    for prop in entity.properties:
      bq_type = _bq_schema_type(prop.sdk_type)
      if prop.column in node_type_registry:
        prev_type, prev_entity = node_type_registry[prop.column]
        if prev_type != bq_type:
          raise ValueError(
              f"Property name collision: {prop.column!r} has type "
              f"{prev_type} in entity {prev_entity!r} but type "
              f"{bq_type} in entity {entity.name!r}."
          )
      node_type_registry[prop.column] = (bq_type, entity.name)
      node_props[prop.column] = {"type": bq_type}

  # Edge structural fields + composite key objects.
  # from_keys/to_keys are typed per-relationship based on the
  # source/target entity key columns.
  from_key_props: dict = {}
  to_key_props: dict = {}
  for rel in relationships:
    src = entity_map[rel.from_entity]
    tgt = entity_map[rel.to_entity]
    src_prop_map = {p.column: p for p in src.properties}
    tgt_prop_map = {p.column: p for p in tgt.properties}
    for col in src.key_columns:
      prop = src_prop_map[col]
      from_key_props[col] = {"type": _bq_schema_type(prop.sdk_type)}
    for col in tgt.key_columns:
      prop = tgt_prop_map[col]
      to_key_props[col] = {"type": _bq_schema_type(prop.sdk_type)}

  edge_props: dict = {
      "relationship_name": {"type": "STRING"},
      "from_entity_name": {"type": "STRING"},
      "to_entity_name": {"type": "STRING"},
      "from_keys": {
          "type": "OBJECT",
          "properties": from_key_props,
      },
      "to_keys": {
          "type": "OBJECT",
          "properties": to_key_props,
      },
  }

  edge_type_registry: dict[str, tuple[str, str]] = {}
  for rel in relationships:
    for prop in rel.properties:
      bq_type = _bq_schema_type(prop.sdk_type)
      if prop.column in edge_type_registry:
        prev_type, prev_rel = edge_type_registry[prop.column]
        if prev_type != bq_type:
          raise ValueError(
              f"Property name collision: {prop.column!r} has type "
              f"{prev_type} in relationship {prev_rel!r} but type "
              f"{bq_type} in relationship {rel.name!r}."
          )
      edge_type_registry[prop.column] = (bq_type, rel.name)
      edge_props[prop.column] = {"type": bq_type}

  schema = {
      "type": "OBJECT",
      "properties": {
          "nodes": {
              "type": "ARRAY",
              "items": {
                  "type": "OBJECT",
                  "properties": node_props,
              },
          },
          "edges": {
              "type": "ARRAY",
              "items": {
                  "type": "OBJECT",
                  "properties": edge_props,
              },
          },
      },
  }

  return json.dumps(schema, separators=(",", ":"))


def compile_extraction_prompt(
    spec: ResolvedGraph,
    entity_names: Optional[list[str]] = None,
) -> str:
  """Generate an extraction prompt for ``AI.GENERATE``.

  The prompt instructs the LLM to extract nodes and edges
  conforming to the ontology definition.

  Args:
      spec: A validated ``ResolvedGraph``.
      entity_names: Optional subset of entity names.

  Returns:
      A prompt string for use with ``AI.GENERATE``.
  """
  entity_map = {e.name: e for e in spec.entities}

  if entity_names is not None:
    entities = [entity_map[n] for n in entity_names if n in entity_map]
    entity_set = set(entity_names)
    relationships = [
        r
        for r in spec.relationships
        if r.from_entity in entity_set and r.to_entity in entity_set
    ]
  else:
    entities = list(spec.entities)
    relationships = list(spec.relationships)

  lines = [
      "Extract nodes and edges from the agent telemetry below "
      "according to the provided ontology.",
      "",
      "Entity types:",
  ]
  for entity in entities:
    prop_list = ", ".join(p.column for p in entity.properties)
    desc = f" — {entity.description}" if entity.description else ""
    lines.append(f"  - {entity.name}{desc} (properties: {prop_list})")

  if relationships:
    lines.append("")
    lines.append("Relationship types:")
    for rel in relationships:
      prop_list = ", ".join(p.column for p in rel.properties)
      desc = f" — {rel.description}" if rel.description else ""
      props_str = f" (properties: {prop_list})" if prop_list else ""
      lines.append(
          f"  - {rel.name}: {rel.from_entity} -> {rel.to_entity}"
          f"{desc}{props_str}"
      )

  lines.extend(
      [
          "",
          "Rules:",
          "- Only emit entity and relationship types declared above.",
          "- Populate all typed properties when present in the data.",
          "- Set from_keys / to_keys to objects mapping each primary key "
          "column name to its value for the connected nodes.",
          "- Do not invent unknown entity types.",
          "",
          "Payload:",
      ]
  )

  return "\n".join(lines)
