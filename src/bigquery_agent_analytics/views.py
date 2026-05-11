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

"""Event-specific BigQuery views for agent analytics.

Creates standard (non-materialized) BigQuery views that unnest the
generic ``agent_events`` table into per-event-type views with typed
columns.  Every view retains the standard identity headers:
``timestamp``, ``event_type``, ``agent``, ``session_id``,
``invocation_id``.

Example usage::

    from bigquery_agent_analytics.views import ViewManager

    vm = ViewManager(
        project_id="my-project",
        dataset_id="analytics",
        table_id="agent_events",
    )
    vm.create_all_views()              # create all per-event views
    vm.create_view("LLM_REQUEST")      # create a single view
    print(vm.get_view_sql("TOOL_STARTING"))  # inspect SQL without creating
"""

from __future__ import annotations

import logging
from typing import Optional

from google.cloud import bigquery

from ._telemetry import LabeledBigQueryClient
from ._telemetry import make_bq_client
from ._telemetry import with_sdk_labels

logger = logging.getLogger("bigquery_agent_analytics." + __name__)

# ------------------------------------------------------------------ #
# Standard header columns included in every view                       #
# ------------------------------------------------------------------ #

_STANDARD_HEADERS = """\
  timestamp,
  event_type,
  agent,
  session_id,
  invocation_id,
  user_id,
  trace_id,
  span_id,
  parent_span_id,
  status,
  error_message,
  is_truncated"""

# ------------------------------------------------------------------ #
# Per-event-type column definitions                                    #
# ------------------------------------------------------------------ #
# Each entry maps an event_type string to a tuple of
# (view_suffix, extra_columns_sql).  ``extra_columns_sql`` extracts
# event-specific fields from the ``content`` and ``attributes`` JSON
# columns into typed top-level columns.  An empty string means the
# view has only the standard headers.

_EVENT_VIEW_DEFS: dict[str, tuple[str, str]] = {
    "USER_MESSAGE_RECEIVED": (
        "user_messages",
        "",
    ),
    "LLM_REQUEST": (
        "llm_requests",
        """\
  JSON_VALUE(attributes, '$.model') AS model,
  content AS request_content,
  JSON_QUERY(attributes, '$.llm_config') AS llm_config,
  JSON_QUERY(attributes, '$.tools') AS tools""",
    ),
    "LLM_RESPONSE": (
        "llm_responses",
        """\
  JSON_QUERY(content, '$.response') AS response,
  CAST(JSON_VALUE(content, '$.usage.prompt') AS INT64) AS usage_prompt_tokens,
  CAST(JSON_VALUE(content, '$.usage.completion') AS INT64) AS usage_completion_tokens,
  CAST(JSON_VALUE(content, '$.usage.total') AS INT64) AS usage_total_tokens,
  CAST(JSON_VALUE(latency_ms, '$.total_ms') AS INT64) AS total_ms,
  CAST(JSON_VALUE(
    latency_ms, '$.time_to_first_token_ms'
  ) AS INT64) AS ttft_ms,
  JSON_VALUE(attributes, '$.model_version') AS model_version,
  JSON_QUERY(attributes, '$.usage_metadata') AS usage_metadata""",
    ),
    "LLM_ERROR": (
        "llm_errors",
        """\
  CAST(JSON_VALUE(latency_ms, '$.total_ms') AS INT64) AS total_ms""",
    ),
    "TOOL_STARTING": (
        "tool_starts",
        """\
  JSON_VALUE(content, '$.tool') AS tool_name,
  JSON_QUERY(content, '$.args') AS tool_args,
  JSON_VALUE(content, '$.tool_origin') AS tool_origin""",
    ),
    "TOOL_COMPLETED": (
        "tool_completions",
        """\
  JSON_VALUE(content, '$.tool') AS tool_name,
  JSON_QUERY(content, '$.result') AS tool_result,
  JSON_VALUE(content, '$.tool_origin') AS tool_origin,
  CAST(JSON_VALUE(latency_ms, '$.total_ms') AS INT64) AS total_ms""",
    ),
    "TOOL_ERROR": (
        "tool_errors",
        """\
  JSON_VALUE(content, '$.tool') AS tool_name,
  JSON_QUERY(content, '$.args') AS tool_args,
  JSON_VALUE(content, '$.tool_origin') AS tool_origin,
  CAST(JSON_VALUE(latency_ms, '$.total_ms') AS INT64) AS total_ms""",
    ),
    "AGENT_STARTING": (
        "agent_starts",
        """\
  JSON_VALUE(content, '$.text_summary') AS agent_instruction""",
    ),
    "AGENT_COMPLETED": (
        "agent_completions",
        """\
  CAST(JSON_VALUE(latency_ms, '$.total_ms') AS INT64) AS total_ms""",
    ),
    "INVOCATION_STARTING": (
        "invocation_starts",
        "",
    ),
    "INVOCATION_COMPLETED": (
        "invocation_completions",
        "",
    ),
    "STATE_DELTA": (
        "state_deltas",
        """\
  JSON_QUERY(attributes, '$.state_delta') AS state_delta""",
    ),
    "HITL_CREDENTIAL_REQUEST": (
        "hitl_credential_requests",
        """\
  JSON_VALUE(content, '$.tool') AS tool_name,
  JSON_QUERY(content, '$.args') AS tool_args""",
    ),
    "HITL_CONFIRMATION_REQUEST": (
        "hitl_confirmation_requests",
        """\
  JSON_VALUE(content, '$.tool') AS tool_name,
  JSON_QUERY(content, '$.args') AS tool_args""",
    ),
    "HITL_INPUT_REQUEST": (
        "hitl_input_requests",
        """\
  JSON_VALUE(content, '$.tool') AS tool_name,
  JSON_QUERY(content, '$.args') AS tool_args""",
    ),
    "HITL_CREDENTIAL_REQUEST_COMPLETED": (
        "hitl_credential_completions",
        """\
  JSON_VALUE(content, '$.tool') AS tool_name,
  JSON_QUERY(content, '$.result') AS tool_result""",
    ),
    "HITL_CONFIRMATION_REQUEST_COMPLETED": (
        "hitl_confirmation_completions",
        """\
  JSON_VALUE(content, '$.tool') AS tool_name,
  JSON_QUERY(content, '$.result') AS tool_result""",
    ),
    "HITL_INPUT_REQUEST_COMPLETED": (
        "hitl_input_completions",
        """\
  JSON_VALUE(content, '$.tool') AS tool_name,
  JSON_QUERY(content, '$.result') AS tool_result""",
    ),
    # A2A_INTERACTION is emitted by the BQ AA Plugin whenever a
    # supervisor agent invokes a `RemoteA2aAgent` sub-agent. The
    # ADK plugin populates these fields under
    # `attributes.a2a_metadata` when `event.custom_metadata`
    # carries `a2a:request` / `a2a:response`. The SDK side surfaces
    # the lineage IDs (task_id / context_id) plus the full
    # request / response payloads as typed top-level columns so
    # downstream consumers can join without re-extracting JSON.
    #
    # `receiver_session_id` is the receiver-side ADK session id
    # echoed back to the caller via response metadata. The COALESCE
    # handles both A2A response shapes:
    #   - Task-shaped responses: the executor populates
    #     `task.metadata.adk_session_id` and the BQ AA Plugin
    #     stores the full task object as `a2a:response`. The plugin
    #     also uses that task object as the row's `content` column,
    #     so the same value is reachable via either path.
    #   - `A2AMessage`-shaped responses: when populated, the value
    #     is at the same nested path; when not populated, the
    #     COALESCE returns NULL and downstream consumers should
    #     treat the receiver_session_id as diagnostic only and
    #     fall back to the context-level join
    #     (caller.a2a_context_id == receiver.session_id) for the
    #     primary stitch.
    "A2A_INTERACTION": (
        "a2a_interactions",
        """\
  JSON_VALUE(
    attributes, '$.a2a_metadata."a2a:task_id"'
  ) AS a2a_task_id,
  JSON_VALUE(
    attributes, '$.a2a_metadata."a2a:context_id"'
  ) AS a2a_context_id,
  JSON_QUERY(
    attributes, '$.a2a_metadata."a2a:request"'
  ) AS a2a_request,
  JSON_QUERY(
    attributes, '$.a2a_metadata."a2a:response"'
  ) AS a2a_response,
  COALESCE(
    JSON_VALUE(content, '$.metadata.adk_session_id'),
    JSON_VALUE(
      attributes,
      '$.a2a_metadata."a2a:response".metadata.adk_session_id'
    )
  ) AS receiver_session_id""",
    ),
}

# ------------------------------------------------------------------ #
# View Template                                                        #
# ------------------------------------------------------------------ #

_VIEW_SQL_TEMPLATE = """\
CREATE OR REPLACE VIEW `{project}.{dataset}.{view_name}` AS
SELECT
{select_clause}
FROM `{project}.{dataset}.{table}`
WHERE event_type = '{event_type}'
"""


def _build_view_sql(
    project: str,
    dataset: str,
    table: str,
    event_type: str,
    view_name: str,
    extra_columns: str,
) -> str:
  """Builds the CREATE OR REPLACE VIEW SQL for one event type."""
  if extra_columns:
    select_clause = f"{_STANDARD_HEADERS},\n{extra_columns}"
  else:
    select_clause = _STANDARD_HEADERS
  return _VIEW_SQL_TEMPLATE.format(
      project=project,
      dataset=dataset,
      table=table,
      view_name=view_name,
      event_type=event_type,
      select_clause=select_clause,
  )


# ------------------------------------------------------------------ #
# ViewManager                                                          #
# ------------------------------------------------------------------ #


class ViewManager:
  """Manages per-event-type BigQuery views over the agent events table.

  Args:
      project_id: Google Cloud project ID.
      dataset_id: BigQuery dataset containing agent events.
      table_id: Source table name (default ``agent_events``).
      view_prefix: Optional prefix for view names (e.g. ``"adk_"``).
      bq_client: Optional pre-configured BigQuery client.
  """

  def __init__(
      self,
      project_id: str,
      dataset_id: str,
      table_id: str = "agent_events",
      view_prefix: str = "adk_",
      bq_client: Optional[bigquery.Client] = None,
  ) -> None:
    self.project_id = project_id
    self.dataset_id = dataset_id
    self.table_id = table_id
    self.view_prefix = view_prefix
    self._bq_client = bq_client
    self._warned_unlabeled_client = False

  @property
  def bq_client(self) -> bigquery.Client:
    if self._bq_client is None:
      self._bq_client = make_bq_client(self.project_id)
    elif isinstance(self._bq_client, bigquery.Client) and not isinstance(
        self._bq_client, LabeledBigQueryClient
    ):
      if not self._warned_unlabeled_client:
        logger.warning(
            "User-provided bigquery.Client is not a "
            "LabeledBigQueryClient; SDK telemetry labels will not be "
            "applied to jobs from this client. To opt in, construct "
            "the client via bigquery_agent_analytics.make_bq_client() "
            "or pass a LabeledBigQueryClient directly."
        )
        self._warned_unlabeled_client = True
    return self._bq_client

  @property
  def available_event_types(self) -> list[str]:
    """Returns the list of event types with view definitions."""
    return sorted(_EVENT_VIEW_DEFS.keys())

  def get_view_name(self, event_type: str) -> str:
    """Returns the fully-qualified view name for an event type."""
    suffix = _EVENT_VIEW_DEFS[event_type][0]
    return f"{self.view_prefix}{suffix}"

  def get_view_sql(self, event_type: str) -> str:
    """Returns the SQL for a single event-type view.

    Args:
        event_type: One of the supported event type strings.

    Returns:
        The CREATE OR REPLACE VIEW SQL statement.

    Raises:
        KeyError: If the event_type is not recognized.
    """
    if event_type not in _EVENT_VIEW_DEFS:
      raise KeyError(
          f"Unknown event_type '{event_type}'. "
          f"Available: {self.available_event_types}"
      )
    suffix, extra_columns = _EVENT_VIEW_DEFS[event_type]
    view_name = f"{self.view_prefix}{suffix}"
    return _build_view_sql(
        project=self.project_id,
        dataset=self.dataset_id,
        table=self.table_id,
        event_type=event_type,
        view_name=view_name,
        extra_columns=extra_columns,
    )

  def create_view(self, event_type: str) -> None:
    """Creates (or replaces) the view for one event type.

    Args:
        event_type: The event type to create a view for.
    """
    sql = self.get_view_sql(event_type)
    view_name = self.get_view_name(event_type)
    logger.info(
        "Creating view %s.%s.%s", self.project_id, self.dataset_id, view_name
    )
    job_config = with_sdk_labels(bigquery.QueryJobConfig(), feature="views")
    self.bq_client.query(sql, job_config=job_config).result()
    logger.info("View %s created successfully.", view_name)

  def create_all_views(self) -> dict[str, str]:
    """Creates views for all supported event types.

    Returns:
        A dict mapping event_type to view name for each created view.
    """
    created = {}
    for event_type in _EVENT_VIEW_DEFS:
      try:
        self.create_view(event_type)
        created[event_type] = self.get_view_name(event_type)
      except Exception as e:
        logger.error(
            "Failed to create view for %s: %s",
            event_type,
            e,
            exc_info=True,
        )
    return created
