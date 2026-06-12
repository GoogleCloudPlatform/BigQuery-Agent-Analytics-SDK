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

"""Derive an ontology + binding from property-graph DDL.

The capstone of GitHub issue #277, Option A: turn the ``CREATE PROPERTY GRAPH``
DDL the user already authors (the query surface) into the in-memory
``Ontology`` + ``Binding`` the materializer needs -- so ``bqaa context-graph``
no longer requires hand-written ``ontology.yaml`` / ``binding.yaml``.

The DDL text can come from two sources:

* A local ``.sql`` file (``--property-graph``), possibly placeholdered for
  ``envsubst`` (``${PROJECT_ID}`` / ``${DATASET}``).
* The graph the user already deployed to BigQuery (``--graph``):
  :func:`fetch_property_graph_ddl` reads the normalized ``CREATE PROPERTY
  GRAPH`` statement back from ``INFORMATION_SCHEMA.PROPERTY_GRAPHS``, making
  the deployed graph itself the single source of truth.

This ties together the three offline building blocks in ``bigquery_ontology``:

1. :func:`~bigquery_ontology.graph_ddl_parser.parse_property_graph_ddl` -- parse
   the DDL into a types-free AST.
2. :class:`~bigquery_ontology.graph_schema_join.BigQuerySchemaProvider` +
   :func:`~bigquery_ontology.graph_schema_join.resolve_graph_column_types` --
   recover property types from ``INFORMATION_SCHEMA.COLUMNS``.
3. :func:`~bigquery_ontology.graph_to_spec.derive_ontology_binding` --
   synthesise the ``Ontology`` + ``Binding``.

plus shell-style ``${VAR}`` placeholder resolution, since the codelab's
``property_graph.sql`` is written for ``envsubst`` (``${PROJECT_ID}`` /
``${DATASET}``).
"""

from __future__ import annotations

import os
import re
from typing import Any, Mapping, Optional, Sequence

_PLACEHOLDER_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

# Mirrors ``resolved_spec.ResolvedEntity.metadata_columns`` -- the SDK runtime
# columns the resolver re-injects, stripped from the synthesised spec.
_DEFAULT_METADATA_COLUMNS = ("session_id", "extracted_at")


def resolve_placeholders(text: str, mapping: Mapping[str, str]) -> str:
  """Substitute ``${NAME}`` placeholders from ``mapping``; leave unknowns.

  Mirrors ``envsubst`` semantics for the names present in ``mapping``. Unknown
  placeholders are left intact so the downstream schema lookup raises a clear
  "unresolved placeholder" error rather than this function failing silently.
  """

  def _replace(match: "re.Match[str]") -> str:
    return mapping.get(match.group(1), match.group(0))

  return _PLACEHOLDER_RE.sub(_replace, text)


class PropertyGraphLookupError(ValueError):
  """A deployed property graph could not be resolved in INFORMATION_SCHEMA."""


def split_graph_ref(
    graph_ref: str,
    *,
    default_project: str,
    default_dataset: str,
) -> tuple[str, str, str]:
  """Split a graph reference into ``(project, dataset, graph_name)``.

  Accepts a bare graph name, ``dataset.graph``, or ``project.dataset.graph``;
  missing qualifiers fall back to ``default_project`` / ``default_dataset``.
  """
  parts = graph_ref.split(".")
  if len(parts) == 3:
    project, dataset, name = parts
  elif len(parts) == 2:
    project, (dataset, name) = default_project, parts
  elif len(parts) == 1:
    project, dataset, name = default_project, default_dataset, parts[0]
  else:
    raise PropertyGraphLookupError(
        f"Cannot parse graph reference {graph_ref!r}: expected 1-3"
        " dot-separated parts (graph, dataset.graph, or"
        " project.dataset.graph)."
    )
  if not all((project, dataset, name)):
    raise PropertyGraphLookupError(
        f"Graph reference {graph_ref!r} is not fully qualified and no"
        " default project/dataset is available."
    )
  return project, dataset, name


def fetch_property_graph_ddl(
    bq_client: Any,
    *,
    project_id: str,
    dataset_id: str,
    graph_name: str,
) -> str:
  """Fetch a deployed graph's DDL from ``INFORMATION_SCHEMA.PROPERTY_GRAPHS``.

  Args:
    bq_client: A ``google.cloud.bigquery.Client``.
    project_id: Default project for unqualified ``graph_name`` references.
    dataset_id: Default dataset for unqualified ``graph_name`` references.
    graph_name: The deployed graph -- a bare name, ``dataset.graph``, or
      ``project.dataset.graph``.

  Returns:
    The normalized ``CREATE PROPERTY GRAPH`` statement BigQuery recorded for
    the graph. Fully qualified, placeholder-free -- ready for
    :func:`derive_ontology_binding_from_ddl`.

  Raises:
    PropertyGraphLookupError: The graph does not exist in the target dataset;
      the message lists the graphs that do.
  """
  from google.cloud import bigquery

  project, dataset, name = split_graph_ref(
      graph_name, default_project=project_id, default_dataset=dataset_id
  )
  sql = (
      "SELECT ddl\n"
      f"FROM `{project}.{dataset}`.INFORMATION_SCHEMA.PROPERTY_GRAPHS\n"
      "WHERE property_graph_name = @graph_name"
  )
  job_config = bigquery.QueryJobConfig(
      query_parameters=[
          bigquery.ScalarQueryParameter("graph_name", "STRING", name)
      ]
  )
  rows = list(bq_client.query(sql, job_config=job_config).result())
  if rows:
    return rows[0]["ddl"]

  available_sql = (
      "SELECT property_graph_name\n"
      f"FROM `{project}.{dataset}`.INFORMATION_SCHEMA.PROPERTY_GRAPHS\n"
      "ORDER BY property_graph_name"
  )
  available = [
      row["property_graph_name"]
      for row in bq_client.query(available_sql).result()
  ]
  hint = (
      f" Graphs in that dataset: {', '.join(available)}."
      if available
      else " The dataset has no property graphs; apply your CREATE PROPERTY"
      " GRAPH DDL first."
  )
  raise PropertyGraphLookupError(
      f"Property graph `{name}` not found in `{project}.{dataset}`"
      " (INFORMATION_SCHEMA.PROPERTY_GRAPHS)." + hint
  )


def derive_ontology_binding_from_ddl(
    ddl_text: str,
    *,
    project_id: str,
    dataset_id: str,
    bq_client: Any,
    substitutions: Optional[Mapping[str, str]] = None,
    metadata_columns: Sequence[str] = _DEFAULT_METADATA_COLUMNS,
):
  """Derive ``(Ontology, Binding)`` from property-graph DDL text.

  Args:
    ddl_text: The ``CREATE PROPERTY GRAPH`` DDL (may contain ``${VAR}``).
    project_id: BigQuery project; substituted for ``${PROJECT_ID}``, used as the
      binding target and the default project for unqualified table references.
    dataset_id: BigQuery dataset; substituted for ``${DATASET}``, used as the
      binding target and the default dataset for unqualified references.
    bq_client: A BigQuery client used to read ``INFORMATION_SCHEMA.COLUMNS``.
    substitutions: Extra ``${VAR}`` values (override the environment and the
      ``PROJECT_ID`` / ``DATASET`` defaults).
    metadata_columns: SDK runtime columns to strip; the resolver re-injects
      them. Defaults to ``("session_id", "extracted_at")``.

  Returns:
    ``(ontology, binding)`` -- validated upstream models ready for ``resolve()``.

  Raises:
    GraphDDLParseError, GraphSchemaError, GraphSpecSynthesisError: From the
      respective building blocks (parse / schema lookup / synthesis).
  """
  from bigquery_ontology.graph_ddl_parser import parse_property_graph_ddl
  from bigquery_ontology.graph_schema_join import BigQuerySchemaProvider
  from bigquery_ontology.graph_schema_join import resolve_graph_column_types
  from bigquery_ontology.graph_to_spec import derive_ontology_binding

  mapping: dict[str, str] = dict(os.environ)
  mapping["PROJECT_ID"] = project_id
  mapping["DATASET"] = dataset_id
  if substitutions:
    mapping.update(substitutions)

  resolved_ddl = resolve_placeholders(ddl_text, mapping)
  graph = parse_property_graph_ddl(resolved_ddl)
  provider = BigQuerySchemaProvider(
      bq_client, default_project=project_id, default_dataset=dataset_id
  )
  column_types = resolve_graph_column_types(graph, provider)
  return derive_ontology_binding(
      graph,
      column_types,
      project=project_id,
      dataset=dataset_id,
      metadata_columns=metadata_columns,
  )
