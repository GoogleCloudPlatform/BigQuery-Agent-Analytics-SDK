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

"""Derive an ontology + binding from a property-graph DDL file.

The capstone of GitHub issue #277, Option A: turn the ``CREATE PROPERTY GRAPH``
DDL the user already authors (the query surface) into the in-memory
``Ontology`` + ``Binding`` the materializer needs -- so ``bqaa context-graph``
no longer requires hand-written ``ontology.yaml`` / ``binding.yaml``.

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
