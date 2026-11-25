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

"""Shared fail-closed BigQuery demo table ownership guard and labeler."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

DEMO_OWNERSHIP_LABEL_KEY = "bqaa_demo"
DEMO_OWNERSHIP_LABEL_VALUE = "combined_cuj_trace_search"


def _extract_http_status_code(exc: Exception) -> int | None:
  """Returns an integer HTTP status code from ``exc`` if present."""
  for attr in ("code", "status_code"):
    val = getattr(exc, attr, None)
    if isinstance(val, int) and not isinstance(val, bool):
      return val
  return None


def _is_bq_not_found_error(exc: Exception) -> bool:
  """Returns True only for a confirmed BigQuery 404 NotFound error."""
  status_code = _extract_http_status_code(exc)
  if status_code is not None:
    return status_code == 404
  try:
    from google.api_core.exceptions import NotFound  # noqa: PLC0415

    if isinstance(exc, NotFound):
      return True
  except ImportError:
    pass
  return type(exc).__name__ == "NotFound"


def _is_bq_conflict_error(exc: Exception) -> bool:
  """Returns True only for a confirmed BigQuery 409 Conflict error."""
  status_code = _extract_http_status_code(exc)
  if status_code is not None:
    return status_code == 409
  try:
    from google.api_core.exceptions import Conflict  # noqa: PLC0415

    if isinstance(exc, Conflict):
      return True
  except ImportError:
    pass
  return type(exc).__name__ == "Conflict"


def ensure_demo_table_created_with_label(
    bq_client: Any,
    table_obj: Any,
    project_id: str,
    dataset_id: str,
    table_name: str,
) -> bool:
  """Atomically creates a BigQuery table with the demo ownership label.

  On a 409 Conflict during ``create_table(..., exists_ok=False)``, immediately
  verifies ownership of the existing table via
  ``assert_demo_table_ownership_or_absent`` and fails closed if unowned.
  """
  existing_labels = getattr(table_obj, "labels", None)
  labels = dict(existing_labels) if isinstance(existing_labels, Mapping) else {}
  labels[DEMO_OWNERSHIP_LABEL_KEY] = DEMO_OWNERSHIP_LABEL_VALUE
  table_obj.labels = labels
  try:
    bq_client.create_table(table_obj, exists_ok=False)
    return True
  except Exception as exc:
    if _is_bq_conflict_error(exc):
      owned = assert_demo_table_ownership_or_absent(
          bq_client, project_id, dataset_id, table_name
      )
      if not owned:
        raise RuntimeError(
            f"Refusing to modify table `{project_id}.{dataset_id}.{table_name}`:"
            " concurrent create conflict against absent or unowned table."
        ) from exc
      return True
    raise


def assert_demo_table_ownership_or_absent(
    bq_client: Any,
    project_id: str,
    dataset_id: str,
    table_name: str,
) -> bool:
  """Verifies a BigQuery table is either demo-owned or confirmed absent.

  Returns:
      ``True`` if the table already exists and carries the demo ownership
      label, or ``False`` if ``get_table`` raised a confirmed 404 NotFound.

  Raises:
      RuntimeError: If the table exists without the demo ownership label (or
          with ``labels=None`` / non-mapping labels).
      Exception: Re-raises any non-404 ``get_table`` error (403, 503, network).
  """
  table_fqn = f"{project_id}.{dataset_id}.{table_name}"
  try:
    table_meta = bq_client.get_table(table_fqn)
  except Exception as exc:
    if _is_bq_not_found_error(exc):
      return False
    raise
  if table_meta is None:
    raise RuntimeError(
        f"Refusing to modify table `{table_fqn}`: missing demo "
        f"ownership label {DEMO_OWNERSHIP_LABEL_KEY}={DEMO_OWNERSHIP_LABEL_VALUE}."
    )
  labels = getattr(table_meta, "labels", None)
  if (
      not isinstance(labels, Mapping)
      or labels.get(DEMO_OWNERSHIP_LABEL_KEY) != DEMO_OWNERSHIP_LABEL_VALUE
  ):
    raise RuntimeError(
        f"Refusing to modify pre-existing table `{table_fqn}`: missing demo "
        f"ownership label {DEMO_OWNERSHIP_LABEL_KEY}={DEMO_OWNERSHIP_LABEL_VALUE}."
    )
  return True


def apply_demo_table_label(
    bq_client: Any,
    project_id: str,
    dataset_id: str,
    table_name: str,
) -> bool:
  """Tags a newly created BigQuery table with the demo ownership label."""
  table_fqn = f"{project_id}.{dataset_id}.{table_name}"
  try:
    table_meta = bq_client.get_table(table_fqn)
  except Exception as exc:
    if _is_bq_not_found_error(exc):
      return False
    raise
  if table_meta is None:
    return False
  labels = getattr(table_meta, "labels", None)
  updated = dict(labels) if isinstance(labels, Mapping) else {}
  if updated.get(DEMO_OWNERSHIP_LABEL_KEY) == DEMO_OWNERSHIP_LABEL_VALUE:
    return True
  updated[DEMO_OWNERSHIP_LABEL_KEY] = DEMO_OWNERSHIP_LABEL_VALUE
  table_meta.labels = updated
  bq_client.update_table(table_meta, ["labels"])
  return True
