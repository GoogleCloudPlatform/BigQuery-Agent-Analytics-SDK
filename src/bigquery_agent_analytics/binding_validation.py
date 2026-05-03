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

"""Pre-flight validator: ontology binding vs. existing BigQuery tables.

This validator checks whether the BigQuery tables a binding YAML points
at physically exist with the columns and types the binding requires,
*before* the SDK starts extraction or materialization. It catches the
most common authoring error (binding YAML drifted out of sync with
physical tables) before extraction wastes ``AI.GENERATE`` tokens.

Different from :func:`bigquery_agent_analytics.graph_validation.
validate_extracted_graph` (issue #76): that one validates extracted
graph output against the spec. This one validates the binding against
the live BigQuery schemas.

Usage::

    from bigquery_agent_analytics.binding_validation import (
        validate_binding_against_bigquery,
    )

    report = validate_binding_against_bigquery(
        ontology=loaded_ontology,
        binding=loaded_binding,
        bq_client=bigquery.Client(project="my-project", location="US"),
        strict=False,
    )

    if not report.ok:
        for f in report.failures:
            print(f)
    for w in report.warnings:
        print(f"WARN: {w}")

See ``docs/ontology/binding-validation.md`` for the full failure-code
reference and CI usage patterns.
"""

from __future__ import annotations

import dataclasses
import enum
import logging
from typing import Any, Optional

logger = logging.getLogger("bigquery_agent_analytics." + __name__)


# ------------------------------------------------------------------ #
# Failure codes                                                        #
# ------------------------------------------------------------------ #


class FailureCode(str, enum.Enum):
  """Typed enum of binding-validation failure codes.

  Seven codes always run (default mode). One additional code
  (``KEY_COLUMN_NULLABLE``) emits a warning by default and escalates
  to a failure under ``strict=True`` — the SDK's own
  ``CREATE TABLE IF NOT EXISTS`` DDL emits NULLABLE key columns
  (``ontology_materializer.py:206``), so requiring REQUIRED mode in
  default-mode would reject SDK-created tables.
  """

  # Default-mode (always failures).
  MISSING_TABLE = "missing_table"
  MISSING_COLUMN = "missing_column"
  TYPE_MISMATCH = "type_mismatch"
  ENDPOINT_TYPE_MISMATCH = "endpoint_type_mismatch"
  UNEXPECTED_REPEATED_MODE = "unexpected_repeated_mode"
  MISSING_DATASET = "missing_dataset"
  INSUFFICIENT_PERMISSIONS = "insufficient_permissions"

  # Strict-mode-only (warning by default, failure under strict=True).
  KEY_COLUMN_NULLABLE = "key_column_nullable"


# ------------------------------------------------------------------ #
# Report types                                                         #
# ------------------------------------------------------------------ #


@dataclasses.dataclass(frozen=True)
class BindingValidationFailure:
  """One failure found during validation.

  ``binding_path`` is a ``binding.entities[N].properties[M].column``
  style path so tooling can point users at the exact YAML line. The
  ``binding_element`` field carries the ontology element name for
  human-readable error reporting.
  """

  code: FailureCode
  binding_element: str
  binding_path: str
  bq_ref: str
  expected: Any = None
  observed: Any = None
  detail: str = ""


@dataclasses.dataclass(frozen=True)
class BindingValidationWarning:
  """Same shape as :class:`BindingValidationFailure`.

  Warnings exist so callers can format failures and warnings
  uniformly. Warnings do not flip ``report.ok`` to ``False``; they
  are advisory in default mode and escalate into ``failures`` under
  ``strict=True``.
  """

  code: FailureCode
  binding_element: str
  binding_path: str
  bq_ref: str
  expected: Any = None
  observed: Any = None
  detail: str = ""


@dataclasses.dataclass(frozen=True)
class BindingValidationReport:
  """Result of :func:`validate_binding_against_bigquery`.

  ``failures`` are hard failures (always present in default and
  strict modes). ``warnings`` are strict-only checks that emitted in
  default mode (empty under ``strict=True`` because they got
  escalated to ``failures``). ``ok`` returns ``True`` iff
  ``failures`` is empty — warnings do *not* affect ``ok``.
  """

  failures: tuple[BindingValidationFailure, ...] = ()
  warnings: tuple[BindingValidationWarning, ...] = ()

  @property
  def ok(self) -> bool:
    return not self.failures


# ------------------------------------------------------------------ #
# BQ type compatibility                                                #
# ------------------------------------------------------------------ #

# Maps the SDK's DDL type (per ``ontology_materializer._DDL_TYPE_MAP``)
# to the set of BigQuery ``SchemaField.field_type`` values that the SDK
# accepts as compatible. BigQuery returns legacy names like ``INTEGER``
# and ``FLOAT`` from older table schemas, so each modern name lists its
# legacy alias.
_COMPATIBLE_BQ_TYPES: dict[str, frozenset[str]] = {
    "STRING": frozenset({"STRING"}),
    "INT64": frozenset({"INT64", "INTEGER"}),
    "FLOAT64": frozenset({"FLOAT64", "FLOAT"}),
    "BOOL": frozenset({"BOOL", "BOOLEAN"}),
    "TIMESTAMP": frozenset({"TIMESTAMP"}),
    "DATE": frozenset({"DATE"}),
    "BYTES": frozenset({"BYTES"}),
}


def _expected_ddl_type(sdk_type: str) -> Optional[str]:
  """Return the BQ DDL type the materializer would emit for *sdk_type*.

  Mirrors ``ontology_materializer._DDL_TYPE_MAP`` so the validator
  uses the same expectations the SDK uses when it generates DDL
  itself. Returns ``None`` for unknown SDK types — the validator
  skips type-compatibility checks for those rather than guessing.
  """
  # Local import to avoid a circular dep at module load time.
  from .ontology_materializer import _DDL_TYPE_MAP

  return _DDL_TYPE_MAP.get(sdk_type.strip().lower())


def _bq_type_matches(sdk_type: str, bq_field_type: str) -> bool:
  """Return True if *bq_field_type* is compatible with *sdk_type*."""
  expected = _expected_ddl_type(sdk_type)
  if expected is None:
    return True  # unknown SDK type: skip the check
  return bq_field_type.upper() in _COMPATIBLE_BQ_TYPES.get(
      expected, frozenset()
  )


# ------------------------------------------------------------------ #
# Public API                                                           #
# ------------------------------------------------------------------ #


def validate_binding_against_bigquery(
    *,
    ontology,
    binding,
    bq_client,
    strict: bool = False,
) -> BindingValidationReport:
  """Validate a binding against live BigQuery tables.

  Resolves the ontology + binding to a ``ResolvedGraph``, then
  checks that every entity / relationship table the binding
  references exists and that every bound column is present with a
  compatible type.

  Args:
      ontology: Upstream ``Ontology`` model.
      binding: Upstream ``Binding`` model.
      bq_client: A ``google.cloud.bigquery.Client``-like object with
          ``get_table(table_ref)`` returning an object exposing
          ``.schema`` (an iterable of ``SchemaField``-like records
          with ``.name``, ``.field_type``, ``.mode`` attributes).
      strict: When ``False`` (default), strict-only checks (today:
          ``KEY_COLUMN_NULLABLE``) emit ``BindingValidationWarning``
          entries. When ``True``, they emit
          ``BindingValidationFailure`` entries with the same code.
          Default is permissive so the validator does not reject
          tables produced by the SDK's own ``CREATE TABLE IF NOT
          EXISTS`` DDL.

  Returns:
      A :class:`BindingValidationReport`.
  """
  from .resolved_spec import resolve

  spec = resolve(ontology, binding, lineage_config=None)

  # Index binding entries by name so we can build precise paths.
  entity_index = {b.name: i for i, b in enumerate(binding.entities)}
  relationship_index = {b.name: i for i, b in enumerate(binding.relationships)}

  failures: list[BindingValidationFailure] = []
  warnings: list[BindingValidationWarning] = []
  table_cache: dict[str, Optional[Any]] = {}

  def emit(
      code: FailureCode,
      *,
      binding_element: str,
      binding_path: str,
      bq_ref: str,
      expected: Any = None,
      observed: Any = None,
      detail: str = "",
      strict_only: bool = False,
  ) -> None:
    """Emit either a failure or a warning, honoring strict mode."""
    if strict_only and not strict:
      warnings.append(
          BindingValidationWarning(
              code=code,
              binding_element=binding_element,
              binding_path=binding_path,
              bq_ref=bq_ref,
              expected=expected,
              observed=observed,
              detail=detail,
          )
      )
    else:
      failures.append(
          BindingValidationFailure(
              code=code,
              binding_element=binding_element,
              binding_path=binding_path,
              bq_ref=bq_ref,
              expected=expected,
              observed=observed,
              detail=detail,
          )
      )

  def fetch_table(
      table_ref: str, binding_element: str, binding_path: str
  ) -> Optional[Any]:
    """Fetch a BQ table, classify any error, and cache the result."""
    if table_ref in table_cache:
      return table_cache[table_ref]

    try:
      table = bq_client.get_table(table_ref)
      table_cache[table_ref] = table
      return table
    except Exception as exc:  # noqa: BLE001 - classify by message
      msg = str(exc).lower()
      code: FailureCode
      if "not found" in msg and "dataset" in msg:
        code = FailureCode.MISSING_DATASET
      elif "not found" in msg or "does not exist" in msg:
        code = FailureCode.MISSING_TABLE
      elif "permission" in msg or "forbidden" in msg or "denied" in msg:
        code = FailureCode.INSUFFICIENT_PERMISSIONS
      else:
        # Default to MISSING_TABLE for unknown errors so the user gets
        # an actionable failure rather than an opaque exception.
        code = FailureCode.MISSING_TABLE

      emit(
          code,
          binding_element=binding_element,
          binding_path=binding_path,
          bq_ref=table_ref,
          detail=str(exc),
      )
      table_cache[table_ref] = None
      return None

  # ---- Per-entity checks --------------------------------------- #

  for entity in spec.entities:
    binding_idx = entity_index.get(entity.name)
    if binding_idx is None:
      # Entity not in this binding (e.g. abstract upstream — already
      # filtered by resolve()). Skip silently.
      continue

    binding_root = f"binding.entities[{binding_idx}]"
    table = fetch_table(
        entity.source,
        binding_element=entity.name,
        binding_path=f"{binding_root}.source",
    )
    if table is None:
      continue

    # Index BQ schema by column name.
    bq_columns = {f.name: f for f in table.schema}

    # Check every bound property.
    for j, prop in enumerate(entity.properties):
      prop_path = f"{binding_root}.properties[{j}].column"
      bq_field = bq_columns.get(prop.column)

      if bq_field is None:
        emit(
            FailureCode.MISSING_COLUMN,
            binding_element=entity.name,
            binding_path=prop_path,
            bq_ref=f"{entity.source}.{prop.column}",
            expected=prop.column,
            detail=(
                f"binding declares property {prop.logical_name!r} "
                f"on column {prop.column!r}, not found on table "
                f"{entity.source}"
            ),
        )
        continue

      # REPEATED-mode columns can't carry scalar properties.
      if getattr(bq_field, "mode", "NULLABLE") == "REPEATED":
        emit(
            FailureCode.UNEXPECTED_REPEATED_MODE,
            binding_element=entity.name,
            binding_path=prop_path,
            bq_ref=f"{entity.source}.{prop.column}",
            expected="NULLABLE or REQUIRED",
            observed="REPEATED",
            detail=(
                f"column {prop.column!r} is REPEATED on "
                f"{entity.source}; the SDK can't bind scalar "
                f"properties to ARRAY columns"
            ),
        )

      # Type compatibility.
      if not _bq_type_matches(prop.sdk_type, bq_field.field_type):
        emit(
            FailureCode.TYPE_MISMATCH,
            binding_element=entity.name,
            binding_path=prop_path,
            bq_ref=f"{entity.source}.{prop.column}",
            expected=_expected_ddl_type(prop.sdk_type),
            observed=bq_field.field_type,
            detail=(
                f"binding maps property {prop.logical_name!r} (sdk_type="
                f"{prop.sdk_type!r}) to column {prop.column!r}, but BQ "
                f"reports type {bq_field.field_type!r}"
            ),
        )

    # Per-key-column checks (REPEATED + strict-only nullability).
    for key_col in entity.key_columns:
      key_path = f"{binding_root}.<key>.{key_col}"
      bq_field = bq_columns.get(key_col)
      if bq_field is None:
        # Already reported as MISSING_COLUMN above when the key was
        # also a bound property. If the key isn't a bound property
        # (rare; ontology requires keys to be properties), still
        # surface it.
        if key_col not in {p.column for p in entity.properties}:
          emit(
              FailureCode.MISSING_COLUMN,
              binding_element=entity.name,
              binding_path=key_path,
              bq_ref=f"{entity.source}.{key_col}",
              expected=key_col,
              detail=(
                  f"primary-key column {key_col!r} not found on "
                  f"table {entity.source}"
              ),
          )
        continue

      if getattr(bq_field, "mode", "NULLABLE") == "REPEATED":
        emit(
            FailureCode.UNEXPECTED_REPEATED_MODE,
            binding_element=entity.name,
            binding_path=key_path,
            bq_ref=f"{entity.source}.{key_col}",
            expected="NULLABLE or REQUIRED",
            observed="REPEATED",
            detail=(
                f"primary-key column {key_col!r} is REPEATED on "
                f"{entity.source}; primary keys can't be ARRAY"
            ),
        )
        continue

      if getattr(bq_field, "mode", "NULLABLE") == "NULLABLE":
        emit(
            FailureCode.KEY_COLUMN_NULLABLE,
            binding_element=entity.name,
            binding_path=key_path,
            bq_ref=f"{entity.source}.{key_col}",
            expected="REQUIRED",
            observed="NULLABLE",
            detail=(
                f"primary-key column {key_col!r} on {entity.source} "
                f"is NULLABLE; under --strict this is a hard failure"
            ),
            strict_only=True,
        )

  # ---- Per-relationship checks --------------------------------- #

  # Index entity sdk_types per key_column for endpoint type matching.
  entity_key_types: dict[str, dict[str, str]] = {}
  for ent in spec.entities:
    cols = {p.column: p.sdk_type for p in ent.properties}
    entity_key_types[ent.name] = {
        k: cols.get(k, "string") for k in ent.key_columns
    }

  for rel in spec.relationships:
    binding_idx = relationship_index.get(rel.name)
    if binding_idx is None:
      continue

    binding_root = f"binding.relationships[{binding_idx}]"
    table = fetch_table(
        rel.source,
        binding_element=rel.name,
        binding_path=f"{binding_root}.source",
    )
    if table is None:
      continue

    bq_columns = {f.name: f for f in table.schema}

    # Endpoint columns: from_columns and to_columns.
    def _check_endpoint(
        kind: str,
        rel_columns: tuple[str, ...],
        endpoint_entity_name: str,
    ) -> None:
      """``kind`` is either ``'from_columns'`` or ``'to_columns'``."""
      endpoint_types = entity_key_types.get(endpoint_entity_name, {})
      endpoint_key_cols = list(endpoint_types.keys())
      for j, col in enumerate(rel_columns):
        col_path = f"{binding_root}.{kind}[{j}]"
        bq_field = bq_columns.get(col)

        if bq_field is None:
          emit(
              FailureCode.MISSING_COLUMN,
              binding_element=rel.name,
              binding_path=col_path,
              bq_ref=f"{rel.source}.{col}",
              expected=col,
              detail=(
                  f"endpoint column {col!r} not found on edge table "
                  f"{rel.source}"
              ),
          )
          continue

        if getattr(bq_field, "mode", "NULLABLE") == "REPEATED":
          emit(
              FailureCode.UNEXPECTED_REPEATED_MODE,
              binding_element=rel.name,
              binding_path=col_path,
              bq_ref=f"{rel.source}.{col}",
              expected="NULLABLE or REQUIRED",
              observed="REPEATED",
              detail=(
                  f"endpoint column {col!r} on {rel.source} is "
                  f"REPEATED; endpoint keys can't be ARRAY"
              ),
          )
          continue

        # Endpoint type must match the referenced entity's key
        # column type at the same position.
        if j < len(endpoint_key_cols):
          expected_sdk = endpoint_types[endpoint_key_cols[j]]
          if not _bq_type_matches(expected_sdk, bq_field.field_type):
            emit(
                FailureCode.ENDPOINT_TYPE_MISMATCH,
                binding_element=rel.name,
                binding_path=col_path,
                bq_ref=f"{rel.source}.{col}",
                expected=_expected_ddl_type(expected_sdk),
                observed=bq_field.field_type,
                detail=(
                    f"endpoint column {col!r} on {rel.source} has BQ "
                    f"type {bq_field.field_type!r}, but referenced "
                    f"entity {endpoint_entity_name!r} key "
                    f"{endpoint_key_cols[j]!r} expects sdk_type="
                    f"{expected_sdk!r}"
                ),
            )

        # Strict-only: endpoint keys should be REQUIRED.
        if getattr(bq_field, "mode", "NULLABLE") == "NULLABLE":
          emit(
              FailureCode.KEY_COLUMN_NULLABLE,
              binding_element=rel.name,
              binding_path=col_path,
              bq_ref=f"{rel.source}.{col}",
              expected="REQUIRED",
              observed="NULLABLE",
              detail=(
                  f"endpoint column {col!r} on {rel.source} is "
                  f"NULLABLE; under --strict this is a hard failure"
              ),
              strict_only=True,
          )

    _check_endpoint("from_columns", rel.from_columns, rel.from_entity)
    _check_endpoint("to_columns", rel.to_columns, rel.to_entity)

    # Property column checks.
    for j, prop in enumerate(rel.properties):
      prop_path = f"{binding_root}.properties[{j}].column"
      bq_field = bq_columns.get(prop.column)

      if bq_field is None:
        emit(
            FailureCode.MISSING_COLUMN,
            binding_element=rel.name,
            binding_path=prop_path,
            bq_ref=f"{rel.source}.{prop.column}",
            expected=prop.column,
            detail=(
                f"binding declares property {prop.logical_name!r} on "
                f"column {prop.column!r}, not found on edge table "
                f"{rel.source}"
            ),
        )
        continue

      if getattr(bq_field, "mode", "NULLABLE") == "REPEATED":
        emit(
            FailureCode.UNEXPECTED_REPEATED_MODE,
            binding_element=rel.name,
            binding_path=prop_path,
            bq_ref=f"{rel.source}.{prop.column}",
            expected="NULLABLE or REQUIRED",
            observed="REPEATED",
            detail=(
                f"property column {prop.column!r} on {rel.source} is "
                f"REPEATED; the SDK can't bind scalar properties to "
                f"ARRAY columns"
            ),
        )

      if not _bq_type_matches(prop.sdk_type, bq_field.field_type):
        emit(
            FailureCode.TYPE_MISMATCH,
            binding_element=rel.name,
            binding_path=prop_path,
            bq_ref=f"{rel.source}.{prop.column}",
            expected=_expected_ddl_type(prop.sdk_type),
            observed=bq_field.field_type,
            detail=(
                f"binding maps relationship property "
                f"{prop.logical_name!r} (sdk_type={prop.sdk_type!r}) "
                f"to column {prop.column!r}, but BQ reports type "
                f"{bq_field.field_type!r}"
            ),
        )

  return BindingValidationReport(
      failures=tuple(failures),
      warnings=tuple(warnings),
  )
