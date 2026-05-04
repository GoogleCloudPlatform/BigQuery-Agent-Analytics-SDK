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

Different from the planned extracted-graph validator in #76, which
will validate ``ExtractedGraph`` output against the resolved spec
after extraction. This validator runs before extraction.

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

The user-facing CLI surface (``bq-agent-sdk binding-validate``,
``ontology-build --validate-binding[-strict]``) and the full
failure-code documentation land in PR 2b of issue #105.
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
  # Critical: derive property/column indices from the binding YAML's
  # own ordering (binding.entities[i].properties), NOT from the
  # ResolvedEntity's properties tuple, because ResolvedEntity orders
  # properties by ontology / effective-property order. With
  # inheritance or a different binding-side ordering, the two
  # orderings can diverge and a path like
  # ``binding.entities[0].properties[1].column`` would point to the
  # wrong YAML entry.
  entity_index = {b.name: i for i, b in enumerate(binding.entities)}
  relationship_index = {b.name: i for i, b in enumerate(binding.relationships)}

  # Per-binding-element {logical_property_name: yaml_index} maps so
  # paths reflect what the user actually wrote in YAML.
  entity_prop_yaml_index: dict[str, dict[str, int]] = {
      b.name: {p.name: j for j, p in enumerate(b.properties)}
      for b in binding.entities
  }
  rel_prop_yaml_index: dict[str, dict[str, int]] = {
      b.name: {p.name: j for j, p in enumerate(b.properties)}
      for b in binding.relationships
  }

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

    # Check every bound property. Path indices come from the binding
    # YAML's ordering, not the ResolvedEntity's ordering, so paths
    # point at the actual YAML entry the user wrote.
    prop_yaml_idx = entity_prop_yaml_index.get(entity.name, {})
    for prop in entity.properties:
      yaml_j = prop_yaml_idx.get(prop.logical_name)
      if yaml_j is None:
        # Resolved property exists but has no matching binding-YAML
        # entry — should not happen for a properly resolved spec, but
        # if it does, fall back to a name-keyed path so the failure
        # is still actionable.
        prop_path = f"{binding_root}.properties[{prop.logical_name}].column"
      else:
        prop_path = f"{binding_root}.properties[{yaml_j}].column"
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
    # Build a {column: yaml_index} map for keys that map to a bound
    # property, so REPEATED / NULLABLE failures on those columns
    # carry a real binding YAML path. Falls back to a pseudo path
    # only for keys that are not bound properties (ontology
    # generally requires keys to be properties; this is defensive).
    column_to_yaml_idx: dict[str, int] = {}
    for prop in entity.properties:
      yaml_j = prop_yaml_idx.get(prop.logical_name)
      if yaml_j is not None:
        column_to_yaml_idx[prop.column] = yaml_j

    for key_col in entity.key_columns:
      yaml_j = column_to_yaml_idx.get(key_col)
      if yaml_j is not None:
        key_path = f"{binding_root}.properties[{yaml_j}].column"
      else:
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

    # SDK metadata columns. The materializer's _entity_columns()
    # (ontology_materializer.py:154) hard-codes session_id STRING +
    # extracted_at TIMESTAMP for every entity table, and routing
    # writes those fields unconditionally on every materialize() call
    # (ontology_materializer.py:258). A user-predefined table missing
    # either column would validate clean here without this check,
    # then fail at load_table_from_json / INSERT time.
    for meta_col, meta_type in (
        ("session_id", "STRING"),
        ("extracted_at", "TIMESTAMP"),
    ):
      meta_path = f"{binding_root}.<metadata>.{meta_col}"
      meta_field = bq_columns.get(meta_col)
      if meta_field is None:
        emit(
            FailureCode.MISSING_COLUMN,
            binding_element=entity.name,
            binding_path=meta_path,
            bq_ref=f"{entity.source}.{meta_col}",
            expected=meta_col,
            detail=(
                f"SDK metadata column {meta_col!r} not found on "
                f"{entity.source}; the materializer writes this on "
                f"every materialize() call (ontology_materializer.py:"
                f"159) so the table must carry it"
            ),
        )
        continue
      if getattr(meta_field, "mode", "NULLABLE") == "REPEATED":
        emit(
            FailureCode.UNEXPECTED_REPEATED_MODE,
            binding_element=entity.name,
            binding_path=meta_path,
            bq_ref=f"{entity.source}.{meta_col}",
            expected="NULLABLE or REQUIRED",
            observed="REPEATED",
            detail=(
                f"SDK metadata column {meta_col!r} on {entity.source} "
                f"is REPEATED; metadata columns must be scalar"
            ),
        )
        continue
      if meta_field.field_type.upper() not in _COMPATIBLE_BQ_TYPES.get(
          meta_type, frozenset()
      ):
        emit(
            FailureCode.TYPE_MISMATCH,
            binding_element=entity.name,
            binding_path=meta_path,
            bq_ref=f"{entity.source}.{meta_col}",
            expected=meta_type,
            observed=meta_field.field_type,
            detail=(
                f"SDK metadata column {meta_col!r} on {entity.source} "
                f"has BQ type {meta_field.field_type!r}, but the "
                f"materializer writes {meta_type}"
            ),
        )

  # ---- Per-relationship checks --------------------------------- #

  # Index entity sdk_types and source tables per key_column so the
  # endpoint check can do BOTH (a) the spec-level expected-type
  # comparison and (b) the physical cross-table comparison against
  # the referenced node table's actual BQ field type. Catches the
  # case where a node table has drifted out of sync with its own
  # ontology (the per-entity loop above flags that as TYPE_MISMATCH
  # on the node), but the edge endpoint also disagrees with the
  # node's actual storage type — we want the edge to surface
  # ENDPOINT_TYPE_MISMATCH even when the node is itself broken.
  entity_key_types: dict[str, dict[str, str]] = {}
  entity_source_by_name: dict[str, str] = {}
  for ent in spec.entities:
    cols = {p.column: p.sdk_type for p in ent.properties}
    entity_key_types[ent.name] = {
        k: cols.get(k, "string") for k in ent.key_columns
    }
    entity_source_by_name[ent.name] = ent.source

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

        # Endpoint type checks. Two comparisons:
        #
        # (1) Spec-level: edge endpoint BQ type must match the
        #     ontology-derived expected SDK type for the referenced
        #     node's primary-key column at the same position.
        # (2) Physical cross-table: when the referenced node table
        #     is fetchable, the edge endpoint's BQ type must match
        #     the actual BQ field type of the referenced key column
        #     in the node table. Catches cases where the node table
        #     has drifted away from its ontology declaration but
        #     the edge has not — those would slip past (1) alone.
        #
        # Both comparisons emit ENDPOINT_TYPE_MISMATCH; (1)
        # describes the spec-level disagreement, (2) describes the
        # physical-table disagreement. The two checks are
        # complementary, not redundant: (1) fires when only the
        # edge is wrong; (2) fires when both are wrong but in
        # different ways.
        if j < len(endpoint_key_cols):
          expected_sdk = endpoint_types[endpoint_key_cols[j]]
          # (1) Spec-level type check.
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

          # (2) Physical cross-table check. Fires only when it adds
          # information beyond (1) — specifically, only when the
          # node table has *drifted* from its ontology declaration
          # AND that drift causes the edge to disagree with the
          # node's actual storage. In the common edge-only-drift
          # case (edge wrong, node correct), (1) already conveys the
          # same expected/observed pair, so emitting this would be
          # pure double-reporting.
          node_source = entity_source_by_name.get(endpoint_entity_name)
          if node_source is not None:
            node_table = table_cache.get(node_source)
            if node_table is not None:
              node_columns = {f.name: f for f in node_table.schema}
              node_field = node_columns.get(endpoint_key_cols[j])
              if node_field is not None:
                edge_t = bq_field.field_type.upper()
                node_t = node_field.field_type.upper()
                expected_ddl = _expected_ddl_type(expected_sdk)
                # Map each BQ type to its canonical (modern) form
                # so legacy aliases like INTEGER/INT64 don't trip
                # the comparison.
                edge_canonical = next(
                    (
                        canon
                        for canon, aliases in _COMPATIBLE_BQ_TYPES.items()
                        if edge_t in aliases
                    ),
                    edge_t,
                )
                node_canonical = next(
                    (
                        canon
                        for canon, aliases in _COMPATIBLE_BQ_TYPES.items()
                        if node_t in aliases
                    ),
                    node_t,
                )
                # Only emit when the node has actually drifted from
                # the ontology spec AND the edge disagrees with the
                # node. If node_canonical == expected_ddl, the node
                # is on-spec and (1) already covers the edge's
                # disagreement with the same expected/observed pair.
                node_has_drifted = (
                    expected_ddl is not None and node_canonical != expected_ddl
                )
                if node_has_drifted and edge_canonical != node_canonical:
                  emit(
                      FailureCode.ENDPOINT_TYPE_MISMATCH,
                      binding_element=rel.name,
                      binding_path=col_path,
                      bq_ref=f"{rel.source}.{col}",
                      expected=node_field.field_type,
                      observed=bq_field.field_type,
                      detail=(
                          f"physical cross-table mismatch: edge "
                          f"column {col!r} on {rel.source} has BQ "
                          f"type {bq_field.field_type!r}, but the "
                          f"referenced node table "
                          f"{node_source}.{endpoint_key_cols[j]} has "
                          f"BQ type {node_field.field_type!r} "
                          f"(node has drifted from ontology spec, "
                          f"which expected {expected_ddl!r}). "
                          f"Edges with this mismatch will fail to "
                          f"join the node at query time."
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

    # Property column checks. Same path-correctness rule as
    # entities: indices come from the binding YAML's ordering.
    prop_yaml_idx = rel_prop_yaml_index.get(rel.name, {})
    for prop in rel.properties:
      yaml_j = prop_yaml_idx.get(prop.logical_name)
      if yaml_j is None:
        prop_path = f"{binding_root}.properties[{prop.logical_name}].column"
      else:
        prop_path = f"{binding_root}.properties[{yaml_j}].column"
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

    # SDK metadata columns on the edge table. The materializer's
    # _relationship_columns() (ontology_materializer.py:164) hard-
    # codes session_id STRING + extracted_at TIMESTAMP for every
    # relationship table. Same trap as on entities — a user-
    # predefined edge table missing these columns would validate
    # clean here and fail at INSERT time.
    for meta_col, meta_type in (
        ("session_id", "STRING"),
        ("extracted_at", "TIMESTAMP"),
    ):
      meta_path = f"{binding_root}.<metadata>.{meta_col}"
      meta_field = bq_columns.get(meta_col)
      if meta_field is None:
        emit(
            FailureCode.MISSING_COLUMN,
            binding_element=rel.name,
            binding_path=meta_path,
            bq_ref=f"{rel.source}.{meta_col}",
            expected=meta_col,
            detail=(
                f"SDK metadata column {meta_col!r} not found on "
                f"edge table {rel.source}; the materializer writes "
                f"this on every materialize() call "
                f"(ontology_materializer.py:164) so the table must "
                f"carry it"
            ),
        )
        continue
      if getattr(meta_field, "mode", "NULLABLE") == "REPEATED":
        emit(
            FailureCode.UNEXPECTED_REPEATED_MODE,
            binding_element=rel.name,
            binding_path=meta_path,
            bq_ref=f"{rel.source}.{meta_col}",
            expected="NULLABLE or REQUIRED",
            observed="REPEATED",
            detail=(
                f"SDK metadata column {meta_col!r} on {rel.source} "
                f"is REPEATED; metadata columns must be scalar"
            ),
        )
        continue
      if meta_field.field_type.upper() not in _COMPATIBLE_BQ_TYPES.get(
          meta_type, frozenset()
      ):
        emit(
            FailureCode.TYPE_MISMATCH,
            binding_element=rel.name,
            binding_path=meta_path,
            bq_ref=f"{rel.source}.{meta_col}",
            expected=meta_type,
            observed=meta_field.field_type,
            detail=(
                f"SDK metadata column {meta_col!r} on {rel.source} "
                f"has BQ type {meta_field.field_type!r}, but the "
                f"materializer writes {meta_type}"
            ),
        )

  return BindingValidationReport(
      failures=tuple(failures),
      warnings=tuple(warnings),
  )
