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

"""MAKO artifact pipeline for the migration v5 demo.

Reads exactly one input — ``mako_core.ttl`` — and produces
every TTL-derived artifact the demo consumes:

* ``ontology.yaml`` — :func:`gm import-owl` output with
  ``FILL_IN`` primary keys resolved programmatically and
  cross-namespace dangling relationships dropped.
* ``binding.yaml`` — generated for a configurable
  ``(project, dataset)``.
* ``table_ddl.sql`` — companion to the binding.
* ``property_graph.sql`` — ``CREATE PROPERTY GRAPH`` SQL.
  Edge-column names align with ``table_ddl.sql`` so Beat 1
  of the notebook can apply both cleanly.

**Events are NOT generated here.** The event stream's
source of truth is the BQ AA plugin's ``agent_events``
table, populated by the runnable agent in
``mako_demo_agent.py`` talking to
``BigQueryAgentAnalyticsPlugin``. An optional captured
offline snapshot (for revalidation tests that need
determinism) is produced by ``export_events_jsonl.py`` —
that path's job is to export FROM the populated BQ table,
not to synthesize events.

Authored input contract — the only files in this
directory that are user-authored:

1. ``mako_core.ttl`` — the MAKO ontology.
2. ``mako_artifacts.py`` (this file) — the TTL → artifacts
   pipeline.
3. ``mako_demo_agent.py`` — the runnable agent that emits
   real plugin traces through ``BigQueryAgentAnalyticsPlugin``.
4. ``run_agent.py`` — the driver that runs the agent for N
   sessions.
5. ``export_events_jsonl.py`` — optional helper that
   captures a deterministic offline snapshot from
   ``agent_events`` for revalidation tests.

Everything else under ``examples/migration_v5/`` is a
reproducibility snapshot produced by
:func:`regenerate_snapshots` or by running the agent.

FILL_IN resolution policy:

The MAKO TTL doesn't declare ``owl:hasKey`` on most
entities, so the OWL importer marks every concrete entity's
primary key as ``FILL_IN``. The artifact pipeline resolves
this by synthesizing an ``id: string`` property + primary
key on every entity that lacks one. This matches MAKO's
"every artifact has a stable identifier" design contract;
if a future TTL revision adds ``owl:hasKey`` declarations,
the resolver leaves those alone.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any, Iterable, Optional

import yaml

from bigquery_ontology import Binding
from bigquery_ontology import load_binding_from_string
from bigquery_ontology import load_ontology_from_string
from bigquery_ontology import Ontology
from bigquery_ontology.owl_importer import import_owl

# Authored-input path. Resolved relative to this file so the
# agent works regardless of the caller's CWD.
_FIXTURE_DIR = pathlib.Path(__file__).parent
TTL_PATH = _FIXTURE_DIR / "mako_core.ttl"

# Snapshot-output paths. :func:`regenerate_snapshots` writes
# these; the notebook can either consume them as-is or call
# the pure APIs at runtime against a fresh dataset.
#
# Note: ``events.jsonl`` is NOT in this list. The event
# stream's source of truth is the BQ AA plugin's
# ``agent_events`` table, populated by the runnable
# ``mako_demo_agent.py`` agent. An optional captured
# offline snapshot (for revalidation tests) is produced by
# ``export_events_jsonl.py``.
ONTOLOGY_PATH = _FIXTURE_DIR / "ontology.yaml"
BINDING_PATH = _FIXTURE_DIR / "binding.yaml"
TABLE_DDL_PATH = _FIXTURE_DIR / "table_ddl.sql"
PROPERTY_GRAPH_PATH = _FIXTURE_DIR / "property_graph.sql"

# MAKO namespace — passed to ``import_owl`` so we only pull
# entities under that IRI prefix (not the imported PROV-O /
# PKO / etc. classes).
_MAKO_NAMESPACE = "https://ontology.yahoo.com/mako/"

# Demo-focused entity allowlist. ``make_binding`` /
# ``make_property_graph_sql`` only consider these six. This
# is **artifact configuration**, not ontology curation — the
# full imported ``ontology.yaml`` still contains the 18
# MAKO-namespace entities; the binding scope is narrower so
# the notebook's four-guarantee narrative stays focused.
#
# Why these six: in MAKO, ``DecisionExecution`` is the
# central hub that ties everything together (per the TTL,
# it's ``partOfSession`` an AgentSession,
# ``atContextSnapshot`` a ContextSnapshot,
# ``executedAtDecisionPoint`` a DecisionPoint,
# ``hasSelectionOutcome`` a SelectionOutcome). The
# decision-flow story doesn't hold together without
# ``DecisionExecution`` in the binding.
DEMO_ENTITIES: tuple[str, ...] = (
    "AgentSession",
    "DecisionExecution",
    "DecisionPoint",
    "Candidate",
    "SelectionOutcome",
    "ContextSnapshot",
)


# ------------------------------------------------------------------ #
# Step 1: load + normalize the MAKO ontology                          #
# ------------------------------------------------------------------ #


def load_mako_ontology() -> tuple[Ontology, str]:
  """Import the MAKO TTL and resolve FILL_IN primary keys.

  Returns:
    A ``(Ontology, yaml_text)`` tuple. The ``yaml_text`` is
    the *resolved* YAML — i.e. the OWL importer's output
    with FILL_INs replaced — and is suitable for writing
    straight to ``ontology.yaml``.
  """
  yaml_text, _drop_summary = import_owl(
      sources=[str(TTL_PATH)],
      include_namespaces=[_MAKO_NAMESPACE],
  )
  resolved_yaml = _normalize_imported_ontology(yaml_text)
  ontology = load_ontology_from_string(resolved_yaml)
  return ontology, resolved_yaml


def _normalize_imported_ontology(yaml_text: str) -> str:
  """Post-process the OWL importer's output so it loads
  cleanly via :func:`load_ontology_from_string`.

  Two passes:

  1. Resolve ``FILL_IN`` primary keys to ``id`` (matches
     MAKO's "every artifact has a stable identifier"
     contract).
  2. Drop cross-namespace dangling relationships. MAKO
     extends PROV-O / PKO / etc., and some relationships
     point to entities outside the MAKO namespace
     (e.g. ``delegatedTo → prov:Agent``). The OWL importer
     leaves those declared but without a ``to`` field
     (because the target wasn't imported); the Ontology
     model then rejects them as malformed. The demo doesn't
     model the external namespaces, so we drop these
     edges.
  """
  data = yaml.safe_load(yaml_text)
  data = _resolve_fill_in_primary_keys_dict(data)
  data = _drop_dangling_relationships(data)
  data = _strip_inheritance(data)
  return yaml.safe_dump(data, sort_keys=False)


def _resolve_fill_in_primary_keys_dict(data: dict) -> dict:
  """Walk every entity; for each one whose ``keys.primary`` is
  ``[FILL_IN]``, replace it with ``[id]`` and ensure an
  ``id: string`` property exists.

  Matches MAKO's "every artifact has a stable identifier"
  design contract. Entities that already declare an
  ``owl:hasKey`` (and hence don't have ``FILL_IN``) are left
  untouched.
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


def _drop_dangling_relationships(data: dict) -> dict:
  """Remove relationships missing either endpoint.

  The MAKO TTL declares relationships that cross into
  PROV-O / PKO / etc. (``delegatedTo → prov:Agent``). The
  agent imports only the MAKO namespace, so those
  cross-namespace endpoints aren't materialized as
  entities; the OWL importer leaves the relationship with
  a missing ``to`` (or ``from``). The Ontology model
  rejects those as malformed. The demo doesn't model the
  external namespaces, so the agent drops these edges and
  documents them in a synthesized annotation so the loss is
  visible.
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
    # Stash the drop list in the top-level ontology annotation
    # so the loss is auditable from the loaded model.
    annotations = data.setdefault("annotations", {})
    annotations["mako_demo:dropped_cross_namespace_relationships"] = dropped
  return data


def _strip_inheritance(data: dict) -> dict:
  """Strip ``extends`` from every entity post-import.

  The MAKO TTL marks ``mako:Candidate rdfs:subClassOf
  mako:RoleTrait``; the OWL importer carries that through
  as ``extends: RoleTrait``. The v0 ``gm compile`` (used by
  the notebook's Section 4 concept-index emission) doesn't
  support inheritance, so the binding compile fails with
  ``compile-validation — Entity 'Candidate' uses 'extends';
  v0 compilation does not support inheritance.``

  ``RoleTrait`` is a marker class in MAKO (REQ-ONT-022 —
  "single-primary-parent inheritance discipline"); it
  carries no properties beyond the ``id`` PK that the
  importer already added to every entity. Stripping the
  ``extends`` clause has no semantic effect on the demo's
  six-entity scope. The discarded inheritance is recorded
  under ``mako_demo:stripped_inheritance`` on the entity
  so the loss is visible.
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
    annotations["mako_demo:stripped_inheritance"] = entity["extends"]
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
    top_annotations["mako_demo:stripped_inheritance"] = ",".join(stripped)
    data["annotations"] = top_annotations
  return data


# ------------------------------------------------------------------ #
# Step 2: generate a binding for a target (project, dataset)         #
# ------------------------------------------------------------------ #


def make_binding(
    ontology: Ontology,
    *,
    project: str,
    dataset: str,
    entity_filter: Optional[Iterable[str]] = None,
) -> Binding:
  """Construct a ``Binding`` for the given target.

  Args:
    ontology: The resolved MAKO ontology
      (:func:`load_mako_ontology` output).
    project: BigQuery project ID.
    dataset: BigQuery dataset name.
    entity_filter: Optional iterable of entity names to
      include in the binding. Defaults to
      :data:`DEMO_ENTITIES`. The notebook narrows the scope
      to keep the four-guarantee narrative focused; the
      full 41-entity ontology is still loadable.

  Returns:
    A validated ``Binding`` instance. Property columns use
    the snake_case-of-camelCase convention
    (``snapshotPayload`` → ``snapshot_payload``) since
    BigQuery's identifier conventions are snake_case.
  """
  scope = set(DEMO_ENTITIES) if entity_filter is None else set(entity_filter)

  # Each entity's PK column is named ``{entity_short}_id``
  # rather than a bare ``id``. The materializer
  # (``_relationship_columns`` in ``ontology_materializer.py``)
  # populates an edge's FK column type from the source
  # entity's ``src_prop_map[col].sdk_type`` — that lookup
  # requires the FK column name to exactly match a property
  # column on the source entity. Using a bare ``id`` would
  # work for one entity per edge but collide when both
  # endpoints share the same PK column name (``id, id`` ─
  # duplicate column). Per-entity PK names give every edge
  # a clean ``{src_entity}_id, {dst_entity}_id`` shape and
  # match the convention the original V5 spec used
  # (``YMGO_Context_Graph_V3``: ``decision_id``,
  # ``adUnitId``, etc.).
  entities_block: list[dict] = []
  for entity in ontology.entities:
    if entity.name not in scope:
      continue
    table_name = _entity_table_name(entity.name)
    pk_column = f"{_entity_id_column(entity.name)}_id"
    props = [{"name": "id", "column": pk_column}]
    # Append every MAKO-declared property except ``id``
    # (PK, already added). The binding validator
    # (``_check_property_coverage``) requires every non-
    # derived ontology property to have a binding; dropping
    # ``sessionId`` to avoid a column-name collision broke
    # that check. Since ``_entity_id_column`` no longer
    # strips ``agent_``, AgentSession's PK column is
    # ``agent_session_id`` and ``sessionId`` keeps its
    # natural ``session_id`` column without collision.
    for prop in entity.properties:
      if prop.name == "id":
        continue
      props.append({"name": prop.name, "column": _to_snake_case(prop.name)})
    entities_block.append(
        {
            "name": entity.name,
            "source": f"{project}.{dataset}.{table_name}",
            "properties": props,
        }
    )

  # Edge set is derived from MAKO's actual declared
  # relationships — pick relationships whose endpoints are
  # both in the demo scope. Two filters:
  #
  # 1. ``rel.from_ != rel.to`` — self-edges (MAKO's
  #    ``evolvedFrom`` and ``supersededBy``,
  #    DecisionExecution → DecisionExecution) are dropped.
  #    The materializer's ``_relationship_columns`` requires
  #    the edge's ``from_columns`` to name a property column
  #    on the source entity. For a self-edge that would
  #    mean two identical PK column names on the edge table
  #    (duplicate-column error), and the workarounds
  #    (``src_/dst_`` prefixes) miss the property lookup.
  #    A future binding revision (or an SDK change that
  #    accepts FK-to-PK column mapping) could re-add them;
  #    for the demo, the heterogeneous edges carry the
  #    decision-flow narrative.
  # 2. Heterogeneous edges use ``{entity_short}_id`` as both
  #    source and destination FK columns — the same name as
  #    the source/destination entity's PK column. The
  #    materializer can then resolve ``src_prop_map[col]``
  #    cleanly.
  relationships_block: list[dict] = []
  dropped_self_edges: list[str] = []
  for rel in ontology.relationships:
    if rel.from_ not in scope or rel.to not in scope:
      continue
    if rel.from_ == rel.to:
      dropped_self_edges.append(rel.name)
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
  property's ``xsd:`` range) through :func:`_bq_type_for`
  — ``integer`` → ``INT64``, ``double`` → ``FLOAT64``,
  ``boolean`` → ``BOOL``, ``timestamp`` → ``TIMESTAMP``,
  ``date`` → ``DATE``, ``string`` and unrecognized → ``STRING``.
  Using ``STRING`` for everything silently lost the typing
  the TTL declared (e.g. MAKO's
  ``DecisionExecution.latencyMs`` is ``xsd:integer``).

  Edge tables use the binding's ``from_columns`` /
  ``to_columns``. The property-graph SQL produced by
  :func:`make_property_graph_sql` references those same
  columns; the two SQL artifacts stay in sync because they
  share this binding.

  Every node + edge table also carries the two SDK metadata
  columns the materializer writes on every ``materialize()``
  call: ``session_id STRING`` and ``extracted_at
  TIMESTAMP``. The binding validator
  (``binding_validation.py``) requires both columns on every
  bound table — without them, the notebook's binding-validate
  step would fail before ontology-build.
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

  for rbind in binding.relationships:
    src_col, dst_col = rbind.from_columns[0], rbind.to_columns[0]
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
  ``session_id`` — MAKO's ``AgentSession.sessionId`` is the
  canonical example. The materializer's writes for those
  rows still land in the same column, so it's safe to skip
  the SDK metadata copy rather than emit ``CREATE TABLE
  agent_session (session_id STRING, session_id STRING, ...)``.
  ``extracted_at`` is unlikely to collide but is handled the
  same way for symmetry.
  """
  return [
      ddl
      for col, ddl in _SDK_METADATA_DDL_BY_COLUMN.items()
      if col not in already_present
  ]


# SDK metadata columns that the materializer
# (``ontology_materializer._entity_columns`` /
# ``_relationship_columns``) writes on every ``materialize()``
# call. Binding validation
# (``binding_validation.py:488,806``) requires both columns
# on every bound table.
_SDK_METADATA_DDL_BY_COLUMN = {
    "session_id": "session_id STRING",
    "extracted_at": "extracted_at TIMESTAMP",
}


def make_property_graph_sql(
    binding: Binding,
    *,
    ontology: Ontology,
    graph_name: str = "mako_demo_graph",
) -> str:
  """Generate ``CREATE OR REPLACE PROPERTY GRAPH`` SQL.

  Beat 1 of the notebook is "you own the graph definition" —
  this output is what the platform team would author. Edge
  columns match :func:`make_table_ddl`'s output so applying
  both in sequence works without column-name mismatches.

  Args:
    binding: A validated ``Binding`` (see :func:`make_binding`).
    graph_name: Local property-graph name. Default
      ``"mako_demo_graph"``.
  """
  project = binding.target.project
  dataset = binding.target.dataset
  qualified_graph = f"{project}.{dataset}.{graph_name}"

  # The PK column for each entity is the ``column`` of the
  # property whose ``name`` is ``id`` (set by ``make_binding``
  # to ``{entity_short}_id``). Both the ``KEY (...)`` of the
  # node table and the ``REFERENCES <alias> (...)`` of every
  # edge endpoint must use that column name; hard-coding
  # ``id`` (the property's *logical* name) trips
  # ``Unrecognized name: id`` because the underlying table
  # column has the entity-specific name.
  pk_column_by_entity: dict[str, str] = {}
  node_tables: list[str] = []
  for ebind in binding.entities:
    qualified_source = ebind.source
    short_name = _table_ref_short(qualified_source)
    pk_col = next(p.column for p in ebind.properties if p.name == "id")
    pk_column_by_entity[ebind.name] = pk_col
    cols = ", ".join(p.column for p in ebind.properties)
    node_tables.append(
        f"    `{qualified_source}` AS {short_name}\n"
        f"      KEY ({pk_col})\n"
        f"      LABEL {ebind.name} PROPERTIES ({cols})"
    )

  # Look up the source/destination entity for each edge by
  # consulting the bound ontology passed in — same TTL-driven
  # lookup the binding generator used.
  rel_map = {r.name: r for r in ontology.relationships}

  edge_tables: list[str] = []
  for rbind in binding.relationships:
    rel = rel_map.get(rbind.name)
    if rel is None:
      # Defensive — should never happen given the binding
      # passed validation.
      continue
    src_col = rbind.from_columns[0]
    dst_col = rbind.to_columns[0]
    qualified_edge_source = rbind.source
    short = _table_ref_short(qualified_edge_source)
    # ``SOURCE KEY ... REFERENCES`` and ``DESTINATION KEY ...
    # REFERENCES`` name the **alias** the node table is declared
    # under inside the same property graph, not the
    # fully-qualified BigQuery table. BQ rejects qualified refs
    # ("The referenced node table 'proj.ds.foo' is not defined in
    # the property graph") because the alias is the in-graph
    # identifier. Same shape ``gm compile`` emits.
    src_alias = _table_ref_short(
        next(e.source for e in binding.entities if e.name == rel.from_)
    )
    dst_alias = _table_ref_short(
        next(e.source for e in binding.entities if e.name == rel.to)
    )
    # Edge tables require an explicit ``KEY (...)`` declaration
    # alongside ``SOURCE KEY`` / ``DESTINATION KEY``. BigQuery
    # rejects edge declarations without it
    # ("graph element table keys must be explicitly defined").
    # The natural composite key for an edge is the pair of FK
    # columns the source + destination references point at —
    # same shape ``gm compile`` emits.
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
# Step 5: regenerate the snapshot files                               #
# ------------------------------------------------------------------ #


def regenerate_snapshots(
    *,
    project: str = "test-project-0728-467323",
    dataset: str = "migration_v5_demo",
) -> dict:
  """Regenerate every TTL-derived artifact snapshot.

  Idempotent: byte-identical output across runs for the
  same ``(project, dataset)`` pair. Returns a small summary
  dict for the notebook's setup cell to display.

  Does NOT produce events — events come from running
  ``mako_demo_agent.py`` against this same
  ``(project, dataset)`` with the BQ AA plugin enabled.
  """
  ontology, yaml_text = load_mako_ontology()
  ONTOLOGY_PATH.write_text(yaml_text, encoding="utf-8")

  binding = make_binding(ontology, project=project, dataset=dataset)
  BINDING_PATH.write_text(_binding_yaml(binding), encoding="utf-8")
  TABLE_DDL_PATH.write_text(
      make_table_ddl(binding, ontology=ontology), encoding="utf-8"
  )
  PROPERTY_GRAPH_PATH.write_text(
      make_property_graph_sql(binding, ontology=ontology), encoding="utf-8"
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


def _entity_table_name(entity_name: str) -> str:
  """Canonical BQ table name for a MAKO entity."""
  return _to_snake_case(entity_name)


def _entity_id_column(entity_name: str) -> str:
  """Column-name root for an entity's PK + foreign-key
  references (e.g. ``AgentSession`` → ``agent_session``,
  used in ``agent_session_id``).

  Earlier drafts stripped a leading ``agent_`` so the FK
  read ``session_id`` — but that collides with both the
  SDK metadata column ``session_id`` and MAKO's
  ``AgentSession.sessionId`` data property (also bound to
  column ``session_id``), producing duplicate columns the
  validator rejects. Keeping the full snake form gives
  every entity a unique PK column and lets the SDK
  metadata + ontology-declared ``sessionId`` co-exist
  cleanly.
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
  # ``Property.type`` is a ``PropertyType`` enum; ``.value``
  # gets the wire string. Handle bare strings too in case a
  # caller passes one.
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


if __name__ == "__main__":  # pragma: no cover
  import argparse

  parser = argparse.ArgumentParser(
      description=(
          "Regenerate the migration v5 demo snapshot files "
          "from the authored mako_core.ttl input."
      ),
  )
  parser.add_argument("--project", default="test-project-0728-467323")
  parser.add_argument("--dataset", default="migration_v5_demo")
  args = parser.parse_args()
  summary = regenerate_snapshots(project=args.project, dataset=args.dataset)
  print(json.dumps(summary, indent=2, sort_keys=True))
