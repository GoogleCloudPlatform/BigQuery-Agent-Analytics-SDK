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

"""Shared deployment runtime helpers.

This module keeps deployment-specific bootstrap logic in one place so
the BigQuery Remote Function path and the streaming evaluation worker
use the same environment conventions when constructing an SDK client.
"""

from __future__ import annotations

import os
from typing import Any

from bigquery_agent_analytics import Client


def resolve_client_options(
    user_defined_context: dict[str, Any] | None = None,
    sdk_surface: str = "remote-function",
) -> dict[str, Any]:
  """Resolve ``Client`` constructor kwargs from request context + env vars.

  Args:
    user_defined_context: Optional request-context dict forwarded by the
      caller (e.g. BigQuery Remote Function ``userDefinedContext``).
    sdk_surface: Value stamped on the ``sdk_surface`` telemetry label.
      Defaults to ``"remote-function"`` because both shipped entry
      points (BQ Remote Function dispatch, streaming-eval worker) are
      remote runtimes. Callers that want a different surface (e.g. a
      future ``"continuous-query"``) pass it explicitly.
  """
  udc = user_defined_context or {}
  project_id = udc.get("project_id", os.environ.get("BQ_AGENT_PROJECT"))
  dataset_id = udc.get("dataset_id", os.environ.get("BQ_AGENT_DATASET"))
  table_id = udc.get(
      "table_id",
      os.environ.get("BQ_AGENT_TABLE", "agent_events"),
  )
  location = udc.get(
      "location",
      os.environ.get("BQ_AGENT_LOCATION"),
  )
  endpoint = udc.get("endpoint") or os.environ.get("BQ_AGENT_ENDPOINT")
  connection_id = udc.get("connection_id") or os.environ.get(
      "BQ_AGENT_CONNECTION_ID"
  )

  if not project_id or not dataset_id:
    raise ValueError("project_id and dataset_id required")

  return {
      "project_id": project_id,
      "dataset_id": dataset_id,
      "table_id": table_id,
      "location": location,
      "verify_schema": False,
      "endpoint": endpoint,
      "connection_id": connection_id,
      "sdk_surface": sdk_surface,
  }


def build_client_from_context(
    user_defined_context: dict[str, Any] | None = None,
    sdk_surface: str = "remote-function",
) -> Client:
  """Build a ``Client`` from request context + deployment env vars."""
  return Client(
      **resolve_client_options(user_defined_context, sdk_surface=sdk_surface)
  )
