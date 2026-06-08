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

"""Generic ontology-artifact pipeline for the context graph demo.

Reads exactly one input — an OWL TTL file — and produces
every TTL-derived artifact the demo consumes:

* ``ontology.yaml`` — :func:`import_owl` output with
  ``FILL_IN`` primary keys resolved programmatically and
  cross-namespace dangling relationships dropped.
* ``binding.yaml`` — generated for a configurable
  ``(project, dataset)`` over a configurable entity allowlist.
* ``table_ddl.sql`` — companion to the binding.
* ``property_graph.sql`` — ``CREATE PROPERTY GRAPH`` SQL.
  Edge-column names align with ``table_ddl.sql``.

This module is **ontology-agnostic**: it accepts any OWL TTL
plus a small :class:`OntologyConfig` describing the namespace
to pull from, the entity allowlist for the binding, the
annotation prefix for audit-trail keys, and the local
property-graph name.

The MAKO demo's config lives in :mod:`mako_artifacts` (the
canonical reference example). A tiny second ontology that
exercises the same pipeline lives under ``example_ontologies/``.

**Events are NOT generated here.** Events come from whatever
agent populates the BQ AA plugin's ``agent_events`` table.
The MAKO demo wires this via ``mako_demo_agent.py`` +
``run_agent.py``.

Transformation policy:

The pipeline applies three post-import normalizations so any
reasonable OWL TTL loads cleanly through
:func:`bigquery_ontology.load_ontology_from_string`:

1. ``FILL_IN`` primary keys → synthesized ``id: string``
   property + primary key. The OWL importer marks every
   concrete entity's primary key as ``FILL_IN`` when the TTL
   doesn't declare ``owl:hasKey``; this resolver synthesizes
   one. Entities that already declare ``owl:hasKey`` (and
   hence don't have ``FILL_IN``) are left untouched.
2. Cross-namespace dangling relationships → dropped. If a
   TTL extends an upstream ontology (e.g. PROV-O) and
   declares relationships pointing at upstream entities the
   importer didn't pull in, the relationship's ``to`` field
   is missing and the Ontology model rejects it. The
   pipeline drops these and records the loss under a
   top-level annotation keyed by the config's
   ``annotation_prefix``.
3. Inheritance stripped. ``gm compile`` v0 doesn't support
   inheritance, so any ``extends:`` clauses are dropped and
   recorded under the config's ``annotation_prefix``.
   Entities whose only path to a primary key was via the
   stripped parent get the same synthesized ``id: string``
   PK the FILL_IN resolver uses.

These transformations are general; they apply to any OWL TTL
with those quirks. MAKO exercises all three; simpler TTLs may
exercise only the first or none.
"""

from __future__ import annotations

import dataclasses
import pathlib
from typing import Any, Iterable, Optional

import yaml

from bigquery_ontology import Binding
from bigquery_ontology import load_binding_from_string
from bigquery_ontology import load_ontology_from_string
from bigquery_ontology import Ontology
from bigquery_ontology.owl_importer import import_owl


@dataclasses.dataclass(frozen=True)
class OntologyConfig:
  """Per-ontology configuration for the artifact pipeline.

  Attributes:
    ttl_path: Path to the authored OWL TTL file.
    include_namespace: IRI prefix passed to ``import_owl``.
      Only entities under this namespace are pulled in from
      the TTL, so upstream imports (PROV-O, PKO, etc.) don't
      leak into the binding.
    entity_allowlist: Tuple of entity names to include in the
      binding. The TTL may declare more entities; the binding
      scope is narrower so the demo narrative stays focused.
    annotation_prefix: Prefix used for audit-trail annotation
      keys the pipeline writes when it drops cross-namespace
      relationships or strips inheritance
      (e.g. ``"mako_demo"`` →
      ``mako_demo:stripped_inheritance``).
    graph_name: Local property-graph name passed to
      ``CREATE OR REPLACE PROPERTY GRAPH``.
    snapshot_dir: Directory where :func:`regenerate_snapshots`
      writes the four output files
      (``ontology.yaml`` / ``binding.yaml`` /
      ``table_ddl.sql`` / ``property_graph.sql``).
  """

  ttl_path: pathlib.Path
  include_namespace: str
  entity_allowlist: tuple[str, ...]
  annotation_prefix: str
  graph_name: str
  snapshot_dir: pathlib.Path

  @property
  def ontology_path(self) -> pathlib.Path:
    return self.snapshot_dir / "ontology.yaml"

  @property
  def binding_path(self) -> pathlib.Path:
    return self.snapshot_dir / "binding.yaml"

  @property
  def table_ddl_path(self) -> pathlib.Path:
    return self.snapshot_dir / "table_ddl.sql"

  @property
  def property_graph_path(self) -> pathlib.Path:
    return self.snapshot_dir / "property_graph.sql"


# ------------------------------------------------------------------ #
# Step 1: load + normalize the ontology                                #
# ------------------------------------------------------------------ #


def load_ontology(config: OntologyConfig) -> tuple[Ontology, str]:
  """Import the TTL and resolve FILL_IN primary keys.

  Returns:
    A ``(Ontology, yaml_text)`` tuple. The ``yaml_text`` is
    the *resolved* YAML — i.e. the OWL importer's output
    with FILL_INs replaced, cross-namespace relationships
    dropped, and inheritance stripped — and is suitable for
    writing straight to ``ontology.yaml``.
  """
  yaml_text, _drop_summary = import_owl(
      sources=[str(config.ttl_path)],
      include_namespaces=[config.include_namespace],
  )
  resolved_yaml = _normalize_imported_ontology(yaml_text, config)
  ontology = load_ontology_from_string(resolved_yaml)
  return ontology, resolved_yaml


def _normalize_imported_ontology(yaml_text: str, config: OntologyConfig) -> str:
  """Post-process the OWL importer's output so it loads
  cleanly via :func:`load_ontology_from_string`.

  Three passes (see module docstring for rationale):

  1. Resolve ``FILL_IN`` primary keys to ``id``.
  2. Drop cross-namespace dangling relationships.
  3. Strip ``extends`` (gm compile v0 limitation).
  """
  data = yaml.safe_load(yaml_text)
  data = _resolve_fill_in_primary_keys_dict(data)
  data = _drop_dangling_relationships(data, config)
  data = _strip_inheritance(data, config)
  return yaml.safe_dump(data, sort_keys=False)


def _resolve_fill_in_primary_keys_dict(data: dict) -> dict:
  """Walk every entity; for each one whose ``keys.primary`` is
  ``[FILL_IN]``, replace it with ``[id]`` and ensure an
  ``id: string`` property exists.

  Matches the "every artifact has a stable identifier"
  contract most well-formed TTLs follow. Entities that already
  declare an ``owl:hasKey`` (and hence don't have ``FILL_IN``)
  are left untouched.
  """
  for entity in data.get("entities", []):
    keys = entity.get("keys")
    if keys is None:
      continue
    primary = keys.get("primary")
    if primary == ["FILL_IN"]:
      keys["primary"] = ["id"]
      props = entity.setdefault("properties", [])
      if not any(p.get("name") == "id" for p in props):
        props.insert(0, {"name": "id", "type": "string"})
  return data


def _drop_dangling_relationships(data: dict, config: OntologyConfig) -> dict:
  """Remove relationships missing either endpoint.

  TTLs that extend upstream ontologies often declare
  relationships that cross into those upstream namespaces
  (e.g. MAKO's ``delegatedTo → prov:Agent``). The importer
  pulls only the configured namespace, so those
  cross-namespace endpoints aren't materialized as entities;
  the OWL importer leaves the relationship with a missing
  ``to`` (or ``from``). The Ontology model rejects those as
  malformed. The pipeline drops these edges and records them
  under ``{annotation_prefix}:dropped_cross_namespace_relationships``
  so the loss is auditable from the loaded model.
  """
  entity_names = {ent["name"] for ent in data.get("entities", [])}
  surviving: list[dict] = []
  dropped: list[str] = []
  for rel in data.get("relationships", []):
    to = rel.get("to")
    frm = rel.get("from")
    if not to or not frm or to not in entity_names or frm not in entity_names:
      dropped.append(rel.get("name", "<anonymous>"))
      continue
    surviving.append(rel)
  data["relationships"] = surviving
  if dropped:
    annotations = data.setdefault("annotations", {})
    annotations[
        f"{config.annotation_prefix}:dropped_cross_namespace_relationships"
    ] = dropped
  return data


def _strip_inheritance(data: dict, config: OntologyConfig) -> dict:
  """Strip ``extends`` from every entity post-import.

  The v0 ``gm compile`` (used by the notebook's Section 4
  concept-index emission) doesn't support inheritance, so any
  ``extends:`` clause breaks compile-validation:
  ``Entity 'X' uses 'extends'; v0 compilation does not
  support inheritance.``

  The discarded inheritance is recorded under
  ``{annotation_prefix}:stripped_inheritance`` on each
  affected entity AND in a top-level summary, so the loss is
  visible from a loaded model. Entities whose only path to a
  primary key was through the parent class get the same
  synthesized ``id: string`` PK the FILL_IN resolver uses.
  """
  # Ontology annotations are typed ``dict[str, str]``; the
  # audit trail therefore serializes to strings. Per-entity
  # records carry the ``extended`` parent in a flat key; the
  # top-level summary is a comma-joined ``entity:parent`` list.
  stripped: list[str] = []
  for entity in data.get("entities", []):
    if "extends" not in entity:
      continue
    stripped.append(f"{entity['name']}:{entity['extends']}")
    annotations = entity.setdefault("annotations", {}) or {}
    annotations[f"{config.annotation_prefix}:stripped_inheritance"] = entity[
        "extends"
    ]
    entity["annotations"] = annotations
    del entity["extends"]
    # Stripping ``extends`` removes the entity's only path
    # to a primary key (the parent class declared one). Add
    # the same ``id: string`` PK the importer adds to every
    # other concrete entity so the ontology still loads.
    keys = entity.setdefault("keys", {})
    if "primary" not in keys:
      keys["primary"] = ["id"]
    props = entity.setdefault("properties", []) or []
    if not any(p.get("name") == "id" for p in props):
      props.insert(0, {"name": "id", "type": "string"})
      entity["properties"] = props
  if stripped:
    top_annotations = data.setdefault("annotations", {}) or {}
    top_annotations[f"{config.annotation_prefix}:stripped_inheritance"] = (
        ",".join(stripped)
    )
    data["annotations"] = top_annotations
  return data


# ------------------------------------------------------------------ #
# Step 2: generate a binding for a target (project, dataset)         #
# ------------------------------------------------------------------ #


def make_binding(
    ontology: Ontology,
    config: OntologyConfig,
    *,
    project: str,
    dataset: str,
    entity_filter: Optional[Iterable[str]] = None,
) -> Binding:
  """Construct a ``Binding`` for the given target.

  Args:
    ontology: The resolved ontology
      (:func:`load_ontology` output).
    config: The per-ontology config. Provides the default
      ``entity_allowlist`` when ``entity_filter`` is None.
    project: BigQuery project ID.
    dataset: BigQuery dataset name.
    entity_filter: Optional override of the entity scope;
      defaults to ``config.entity_allowlist``.

  Returns:
    A validated ``Binding`` instance. Property columns use
    the snake_case-of-camelCase convention since BigQuery's
    identifier conventions are snake_case.
  """
  scope = (
      set(config.entity_allowlist)
      if entity_filter is None
      else set(entity_filter)
  )

  # Each entity's PK column is named ``{entity_short}_id``
  # rather than a bare ``id``. Heterogeneous edges still keep the
  # legacy ``list[str]`` binding shape (``from_columns:
  # [<src_entity>_id]``), so the PK column name has to be unique
  # per entity — otherwise ``from_columns + to_columns`` would
  # land ``id, id`` on the edge table (duplicate column).
  # Self-edges go through the dict-shape ``[{src_<col>_id:
  # <pk_prop>}]`` (C2 / #179 follow-up) so they can disambiguate
  # by naming explicit ``src_/dst_`` prefixed FK columns; the
  # canonical FK→PK mapping resolves those into the endpoint's
  # PK property type at materialization time. Per-entity PK names
  # give every heterogeneous edge a clean
  # ``{src_entity}_id, {dst_entity}_id`` shape.
  entities_block: list[dict] = []
  for entity in ontology.entities:
    if entity.name not in scope:
      continue
    # The PK property name comes from the ontology — either the
    # synthesized ``id`` the FILL_IN resolver added, or the real
    # property declared by ``owl:hasKey`` in the TTL. Hard-coding
    # ``"id"`` here broke TTLs that declared their own keys (the
    # binding ended up declaring an ``id`` property the entity
    # didn't have). Single-column PK is assumed; composite PKs
    # would need extra bind logic + property-graph KEY handling
    # and are out of scope for the current demo.
    pk_property_name = _primary_key_property_name(entity)
    table_name = _entity_table_name(entity.name)
    pk_column = f"{_entity_id_column(entity.name)}_id"
    props = [{"name": pk_property_name, "column": pk_column}]
    # Append every ontology-declared property except the PK
    # (already added). The binding validator requires every
    # non-derived ontology property to have a binding.
    for prop in entity.properties:
      if prop.name == pk_property_name:
        continue
      props.append({"name": prop.name, "column": _to_snake_case(prop.name)})
    entities_block.append(
        {
            "name": entity.name,
            "source": f"{project}.{dataset}.{table_name}",
            "properties": props,
        }
    )

  # Edge set is derived from the ontology's declared
  # relationships — pick relationships whose endpoints are
  # both in scope. Two emission paths, no relationships dropped:
  #
  # 1. **Heterogeneous edges** (``rel.from_ != rel.to``) use
  #    ``{entity_short}_id`` as the FK column on both sides —
  #    same name as the source/destination entity's PK column.
  #    The materializer resolves the type via ``src_prop_map[col]``
  #    on a property whose ``column == col``. Legacy
  #    ``list[str]`` binding shape.
  # 2. **Self-edges** (``rel.from_ == rel.to``) use
  #    ``src_<entity_short>_id`` / ``dst_<entity_short>_id`` as
  #    disambiguated edge-table FK columns. The dict-shape
  #    binding ``[{src_<col>: <pk_prop>}]`` introduced in #179
  #    (with C2 wiring it through the materializer + DDL
  #    compiler) tells the SDK that ``src_<col>`` references
  #    the endpoint's ``<pk_prop>`` PK property. Without the
  #    canonical mapping the materializer would look up
  #    ``src_prop_map[src_<col>]`` and ``KeyError``; with it,
  #    self-edges materialize correctly.
  relationships_block: list[dict] = []
  for rel in ontology.relationships:
    if rel.from_ not in scope or rel.to not in scope:
      continue
    if rel.from_ == rel.to:
      entity_short = _entity_id_column(rel.from_)
      endpoint_entity = next(
          e for e in ontology.entities if e.name == rel.from_
      )
      pk_prop = _primary_key_property_name(endpoint_entity)
      relationships_block.append(
          {
              "name": rel.name,
              "source": f"{project}.{dataset}.{_edge_table_name(rel.name)}",
              "from_columns": [{f"src_{entity_short}_id": pk_prop}],
              "to_columns": [{f"dst_{entity_short}_id": pk_prop}],
          }
      )
      continue
    src_col = f"{_entity_id_column(rel.from_)}_id"
    dst_col = f"{_entity_id_column(rel.to)}_id"
    relationships_block.append(
        {
            "name": rel.name,
            "source": f"{project}.{dataset}.{_edge_table_name(rel.name)}",
            "from_columns": [src_col],
            "to_columns": [dst_col],
        }
    )

  binding_dict = {
      "binding": f"{dataset}_binding",
      "ontology": ontology.ontology,
      "target": {
          "backend": "bigquery",
          "project": project,
          "dataset": dataset,
      },
      "entities": entities_block,
      "relationships": relationships_block,
  }
  binding_yaml = yaml.safe_dump(binding_dict, sort_keys=False)
  return load_binding_from_string(binding_yaml, ontology=ontology)


# ------------------------------------------------------------------ #
# Step 3: derive table DDL + property-graph SQL from the binding     #
# ------------------------------------------------------------------ #


def make_table_ddl(binding: Binding, *, ontology: Ontology) -> str:
  """Generate ``CREATE TABLE`` SQL for every node + edge
  table referenced by *binding*.

  Column types are mapped from the ontology's
  ``Property.type`` (which the OWL importer set from each
  property's ``xsd:`` range) through :func:`_bq_type_for`.

  Every node + edge table also carries the two SDK metadata
  columns the materializer writes on every ``materialize()``
  call: ``session_id STRING`` and ``extracted_at TIMESTAMP``.
  The binding validator requires both columns on every bound
  table.
  """
  prop_types: dict[tuple[str, str], str] = {}
  for entity in ontology.entities:
    for prop in entity.properties:
      prop_types[(entity.name, prop.name)] = _bq_type_for(prop.type)

  lines: list[str] = []
  for ebind in binding.entities:
    bound_columns = {prop.column for prop in ebind.properties}
    cols = []
    for prop in ebind.properties:
      bq_type = prop_types.get((ebind.name, prop.name), "STRING")
      cols.append(f"{prop.column} {bq_type}")
    cols.extend(_sdk_metadata_columns(bound_columns))
    lines.append(
        f"CREATE TABLE IF NOT EXISTS `{ebind.source}` ({', '.join(cols)});"
    )

  # ``from_columns`` / ``to_columns`` accept both ``list[str]``
  # (legacy heterogeneous edges) and ``list[dict[str, str]]`` (the
  # dict-shape introduced in #179 — used here for self-edges where
  # the FK column name must differ from the endpoint's PK column).
  # ``edge_column_names`` normalizes either shape to the list of
  # edge-column names.
  from bigquery_ontology.binding_loader import edge_column_names

  for rbind in binding.relationships:
    src_names = edge_column_names(list(rbind.from_columns))
    dst_names = edge_column_names(list(rbind.to_columns))
    src_col = src_names[0]
    dst_col = dst_names[0]
    edge_cols = [f"{src_col} STRING", f"{dst_col} STRING"]
    edge_cols.extend(_sdk_metadata_columns({src_col, dst_col}))
    lines.append(
        f"CREATE TABLE IF NOT EXISTS `{rbind.source}` "
        f"({', '.join(edge_cols)});"
    )

  return "\n".join(lines) + "\n"


def _sdk_metadata_columns(already_present: set[str]) -> list[str]:
  """Return DDL fragments for SDK metadata columns not yet
  present in *already_present*.

  Domain bindings can legitimately map a property onto
  ``session_id`` (MAKO's ``AgentSession.sessionId`` is one
  example). The materializer's writes for those rows still
  land in the same column, so skipping the SDK metadata copy
  avoids a duplicate-column error.
  """
  return [
      ddl
      for col, ddl in _SDK_METADATA_DDL_BY_COLUMN.items()
      if col not in already_present
  ]


# SDK metadata columns that the materializer
# (``ontology_materializer._entity_columns`` /
# ``_relationship_columns``) writes on every ``materialize()``
# call. Binding validation requires both columns on every
# bound table.
_SDK_METADATA_DDL_BY_COLUMN = {
    "session_id": "session_id STRING",
    "extracted_at": "extracted_at TIMESTAMP",
}


def make_property_graph_sql(
    binding: Binding,
    *,
    ontology: Ontology,
    graph_name: str,
) -> str:
  """Generate ``CREATE OR REPLACE PROPERTY GRAPH`` SQL.

  Edge columns match :func:`make_table_ddl`'s output so
  applying both in sequence works without column-name
  mismatches.

  Args:
    binding: A validated ``Binding`` (see :func:`make_binding`).
    ontology: The bound ontology — used to resolve each
      relationship's source/destination entity for the
      ``SOURCE KEY`` / ``DESTINATION KEY REFERENCES`` clauses.
    graph_name: Local property-graph name.
  """
  project = binding.target.project
  dataset = binding.target.dataset
  qualified_graph = f"{project}.{dataset}.{graph_name}"

  # The PK column for each entity is set by ``make_binding`` to
  # ``{entity_short}_id``; the bound *property* name is whatever
  # the ontology's primary key declares (synthesized ``id`` for
  # the FILL_IN path, or the real property declared by
  # ``owl:hasKey`` otherwise). Both the ``KEY (...)`` of the node
  # table and the ``REFERENCES <alias> (...)`` of every edge
  # endpoint must use the COLUMN name, so we look the property
  # up by its ontology-derived name first, then read its column.
  pk_name_by_entity = {
      e.name: _primary_key_property_name(e) for e in ontology.entities
  }
  pk_column_by_entity: dict[str, str] = {}
  node_tables: list[str] = []
  for ebind in binding.entities:
    qualified_source = ebind.source
    short_name = _table_ref_short(qualified_source)
    pk_property_name = pk_name_by_entity[ebind.name]
    pk_col = next(
        p.column for p in ebind.properties if p.name == pk_property_name
    )
    pk_column_by_entity[ebind.name] = pk_col
    cols = ", ".join(p.column for p in ebind.properties)
    node_tables.append(
        f"    `{qualified_source}` AS {short_name}\n"
        f"      KEY ({pk_col})\n"
        f"      LABEL {ebind.name} PROPERTIES ({cols})"
    )

  rel_map = {r.name: r for r in ontology.relationships}

  from bigquery_ontology.binding_loader import edge_column_names

  edge_tables: list[str] = []
  for rbind in binding.relationships:
    rel = rel_map.get(rbind.name)
    if rel is None:
      # Defensive — should never happen given the binding
      # passed validation.
      continue
    # ``edge_column_names`` accepts both the legacy ``list[str]``
    # and the new dict-shape ``list[dict[str, str]]`` (#179).
    # Self-edges use the dict shape so the src/dst FK columns can
    # be disambiguated.
    src_col = edge_column_names(list(rbind.from_columns))[0]
    dst_col = edge_column_names(list(rbind.to_columns))[0]
    qualified_edge_source = rbind.source
    short = _table_ref_short(qualified_edge_source)
    # ``SOURCE KEY ... REFERENCES`` and ``DESTINATION KEY ...
    # REFERENCES`` name the **alias** the node table is
    # declared under inside the same property graph, not the
    # fully-qualified BigQuery table.
    src_alias = _table_ref_short(
        next(e.source for e in binding.entities if e.name == rel.from_)
    )
    dst_alias = _table_ref_short(
        next(e.source for e in binding.entities if e.name == rel.to)
    )
    # Edge tables require an explicit ``KEY (...)``
    # declaration alongside ``SOURCE KEY`` / ``DESTINATION
    # KEY``. The natural composite key is the pair of FK
    # columns the source + destination references point at.
    src_pk = pk_column_by_entity[rel.from_]
    dst_pk = pk_column_by_entity[rel.to]
    edge_tables.append(
        f"    `{qualified_edge_source}` AS {short}\n"
        f"      KEY ({src_col}, {dst_col})\n"
        f"      SOURCE KEY ({src_col}) REFERENCES {src_alias} ({src_pk})\n"
        f"      DESTINATION KEY ({dst_col}) REFERENCES {dst_alias} ({dst_pk})\n"
        f"      LABEL {rbind.name}"
    )

  return (
      f"CREATE OR REPLACE PROPERTY GRAPH `{qualified_graph}`\n"
      f"  NODE TABLES (\n" + ",\n".join(node_tables) + "\n  )\n"
      f"  EDGE TABLES (\n" + ",\n".join(edge_tables) + "\n  );\n"
  )


# ------------------------------------------------------------------ #
# Step 4: regenerate the snapshot files                                #
# ------------------------------------------------------------------ #


def regenerate_snapshots(
    config: OntologyConfig,
    *,
    project: str,
    dataset: str,
) -> dict:
  """Regenerate every TTL-derived artifact snapshot for *config*.

  Idempotent: byte-identical output across runs for the same
  ``(config, project, dataset)`` triple. Returns a small
  summary dict.

  Does NOT produce events — events come from whichever agent
  populates the BQ AA plugin's ``agent_events`` table.
  """
  ontology, yaml_text = load_ontology(config)
  config.ontology_path.write_text(yaml_text, encoding="utf-8")

  binding = make_binding(ontology, config, project=project, dataset=dataset)
  config.binding_path.write_text(_binding_yaml(binding), encoding="utf-8")
  config.table_ddl_path.write_text(
      make_table_ddl(binding, ontology=ontology), encoding="utf-8"
  )
  config.property_graph_path.write_text(
      make_property_graph_sql(
          binding, ontology=ontology, graph_name=config.graph_name
      ),
      encoding="utf-8",
  )

  return {
      "ontology_entities": len(ontology.entities),
      "binding_entities": len(binding.entities),
      "binding_relationships": len(binding.relationships),
  }


# ------------------------------------------------------------------ #
# Helpers                                                              #
# ------------------------------------------------------------------ #


def _binding_yaml(binding: Binding) -> str:
  """Serialize a Binding to YAML.

  Pydantic's ``model_dump`` keeps enum members as enum
  instances by default; PyYAML's ``safe_dump`` can't
  represent those. ``mode='json'`` coerces enums to their
  string values plus normalizes other non-YAML primitives,
  matching how the loader expects to read the YAML back.
  """
  payload = binding.model_dump(by_alias=True, exclude_none=True, mode="json")
  return yaml.safe_dump(payload, sort_keys=False)


def _primary_key_property_name(entity: Any) -> str:
  """Return the entity's primary-key property name.

  After the normalization passes have run, every entity has
  ``keys.primary`` populated — either the synthesized ``id``
  the FILL_IN resolver added, or the real property the TTL
  declared via ``owl:hasKey``. This helper centralizes the
  single-column-PK assumption: the binding generator and the
  property-graph SQL generator both need the same name.

  Raises ``ValueError`` if the entity has no primary key (a
  malformed-input guard; the normalization passes should
  prevent this).
  """
  keys = getattr(entity, "keys", None)
  primary = getattr(keys, "primary", None) if keys is not None else None
  if not primary:
    raise ValueError(
        f"Entity {entity.name!r} has no primary key. The "
        "ontology-artifact pipeline assumes every entity declares "
        "one (synthesized via FILL_IN resolution or declared via "
        "owl:hasKey)."
    )
  return primary[0]


def _entity_table_name(entity_name: str) -> str:
  """Canonical BQ table name for an entity."""
  return _to_snake_case(entity_name)


def _entity_id_column(entity_name: str) -> str:
  """Column-name root for an entity's PK + foreign-key
  references (e.g. ``AgentSession`` → ``agent_session``,
  used in ``agent_session_id``).

  Earlier drafts stripped a leading prefix to shorten FK
  column names — but that collided with both the SDK metadata
  column ``session_id`` and naturally-named ``sessionId``
  data properties (also bound to column ``session_id``),
  producing duplicate columns the validator rejects. Keeping
  the full snake form gives every entity a unique PK column
  and lets SDK metadata + ontology-declared ``sessionId``
  co-exist cleanly.
  """
  return _to_snake_case(entity_name)


def _edge_table_name(edge_name: str) -> str:
  return _to_snake_case(edge_name)


def _table_ref_short(qualified: str) -> str:
  return qualified.rsplit(".", 1)[-1]


def _bq_type_for(property_type: Any) -> str:
  """Map an ontology ``PropertyType`` (or its string value)
  to a BigQuery column type.

  Defaults to ``STRING`` for unknown values; the only types
  the OWL importer can currently emit are those in
  ``PropertyType`` (string / bytes / integer / double /
  numeric / boolean / date / time / datetime / timestamp /
  json), and they map 1:1 to BigQuery legacy SQL types.
  """
  value = getattr(property_type, "value", property_type)
  return {
      "string": "STRING",
      "bytes": "BYTES",
      "integer": "INT64",
      "double": "FLOAT64",
      "numeric": "NUMERIC",
      "boolean": "BOOL",
      "date": "DATE",
      "time": "TIME",
      "datetime": "DATETIME",
      "timestamp": "TIMESTAMP",
      "json": "JSON",
  }.get(value, "STRING")


def _to_snake_case(camel: str) -> str:
  out: list[str] = []
  for i, ch in enumerate(camel):
    if ch.isupper() and i > 0 and not camel[i - 1].isupper():
      out.append("_")
    out.append(ch.lower())
  return "".join(out)
