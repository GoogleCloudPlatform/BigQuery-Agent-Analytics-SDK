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

  entities_block: list[dict] = []
  for entity in ontology.entities:
    if entity.name not in scope:
      continue
    table_name = _entity_table_name(entity.name)
    props = [{"name": "id", "column": "id"}]
    # Append every MAKO-declared property except ``id``
    # (which the binding always projects as the primary
    # key) — snake_case the column for BQ conventions.
    for prop in entity.properties:
      if prop.name == "id":
        continue
      props.append(
          {
              "name": prop.name,
              "column": _to_snake_case(prop.name),
          }
      )
    entities_block.append(
        {
            "name": entity.name,
            "source": f"{project}.{dataset}.{table_name}",
            "properties": props,
        }
    )

  # Edge set is derived from MAKO's actual declared
  # relationships — agent picks the ones where BOTH endpoints
  # are in the demo entity set. No hardcoded "demo edges"
  # list: the binding's relationship set is fully TTL-driven.
  relationships_block: list[dict] = []
  for rel in ontology.relationships:
    if rel.from_ not in scope or rel.to not in scope:
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


def make_table_ddl(binding: Binding) -> str:
  """Generate ``CREATE TABLE`` SQL for every node + edge
  table referenced by *binding*.

  Edge tables get explicit semantic source/destination
  column names (e.g. ``session_id``, ``decision_point_id``)
  rather than the scaffolder's ``from_id`` / ``to_id``
  defaults. The property-graph SQL produced by
  :func:`make_property_graph_sql` references those same
  columns; the two SQL artifacts stay in sync because they
  share this binding.
  """
  lines: list[str] = []
  for ebind in binding.entities:
    table = _table_ref_short(ebind.source)
    columns = ", ".join(
        f"{prop.column} STRING"
        if not prop.name.endswith("Timestamp")
        else f"{prop.column} TIMESTAMP"
        for prop in ebind.properties
    )
    lines.append(f"CREATE TABLE IF NOT EXISTS `{ebind.source}` ({columns});")

  for rbind in binding.relationships:
    src_col, dst_col = rbind.from_columns[0], rbind.to_columns[0]
    lines.append(
        f"CREATE TABLE IF NOT EXISTS `{rbind.source}` "
        f"({src_col} STRING, {dst_col} STRING);"
    )

  return "\n".join(lines) + "\n"


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

  node_tables: list[str] = []
  for ebind in binding.entities:
    qualified_source = ebind.source
    short_name = _table_ref_short(qualified_source)
    cols = ", ".join(p.column for p in ebind.properties)
    node_tables.append(
        f"    `{qualified_source}` AS {short_name}\n"
        f"      KEY (id)\n"
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
    src_table = next(e.source for e in binding.entities if e.name == rel.from_)
    dst_table = next(e.source for e in binding.entities if e.name == rel.to)
    edge_tables.append(
        f"    `{qualified_edge_source}` AS {short}\n"
        f"      SOURCE KEY ({src_col}) REFERENCES `{src_table}` (id)\n"
        f"      DESTINATION KEY ({dst_col}) REFERENCES `{dst_table}` (id)\n"
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
  TABLE_DDL_PATH.write_text(make_table_ddl(binding), encoding="utf-8")
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
  """Column-name root for an entity's foreign-key references
  (e.g. ``AgentSession`` → ``session``, used in
  ``session_id``)."""
  snake = _to_snake_case(entity_name)
  # Strip a trailing "_session" / "_point" / "_outcome" /
  # "_snapshot" suffix so the resulting column reads
  # naturally: ``agent_session`` → ``session``,
  # ``decision_point`` → ``decision_point``, etc. Keep the
  # raw snake form when stripping would over-shorten.
  if snake.startswith("agent_") and len(snake) > len("agent_"):
    return snake[len("agent_") :]
  return snake


def _edge_table_name(edge_name: str) -> str:
  return _to_snake_case(edge_name)


def _table_ref_short(qualified: str) -> str:
  return qualified.rsplit(".", 1)[-1]


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
