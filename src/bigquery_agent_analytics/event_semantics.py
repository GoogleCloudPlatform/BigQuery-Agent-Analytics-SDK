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

"""Canonical event semantic layer for BigQuery Agent Analytics SDK.

Centralizes the logic for interpreting ADK plugin events so that
every module (evaluators, memory, insights, trace) uses consistent
definitions for "final response", "error event", "tool outcome",
etc.  Import helpers from this module instead of re-implementing
event-type checks in each module.

The core predicate logic lives in :mod:`udf_kernels` (the single
source of truth shared with BigQuery Python UDFs).  This module
re-exports those functions and adds SDK-specific constants and
dict-based convenience wrappers.

Example usage::

    from bigquery_agent_analytics.event_semantics import (
        is_error_event,
        extract_response_text,
    )

    for span in trace.spans:
        if is_error_event(span.event_type, span.error_message,
                          span.status):
            print("Error:", span.error_message)
"""

from __future__ import annotations

from typing import Any, Optional

from bigquery_agent_analytics.udf_kernels import extract_response_text_from_dict
from bigquery_agent_analytics.udf_kernels import is_error_event
from bigquery_agent_analytics.udf_kernels import tool_outcome

# Re-export kernel functions so existing callers keep working.
__all__ = [
    "is_error_event",
    "tool_outcome",
    "extract_response_text",
    "is_tool_event",
    "is_hitl_event",
    "is_hitl_completed",
    "ERROR_SQL_PREDICATE",
    "NO_ERROR_SQL_PREDICATE",
    "RESPONSE_EVENT_TYPES",
    "EVENT_FAMILIES",
    "ALL_KNOWN_EVENT_TYPES",
]


# SQL fragment for use in BigQuery WHERE clauses.
ERROR_SQL_PREDICATE = (
    "(ENDS_WITH(event_type, '_ERROR')"
    " OR error_message IS NOT NULL"
    " OR status = 'ERROR')"
)

# Negated version for filtering out errors.
NO_ERROR_SQL_PREDICATE = (
    "NOT ENDS_WITH(event_type, '_ERROR')"
    " AND error_message IS NULL"
    " AND status != 'ERROR'"
)


# ------------------------------------------------------------------ #
# Response Extraction                                                  #
# ------------------------------------------------------------------ #


def extract_response_text(content: dict[str, Any]) -> Optional[str]:
  """Extracts user-visible response text from a content dict.

  Delegates to :func:`udf_kernels.extract_response_text_from_dict`
  (the single source of truth for key-priority logic).

  Args:
      content: The parsed ``content`` JSON column.

  Returns:
      The response text or ``None``.
  """
  return extract_response_text_from_dict(content)


# Event types that carry the final agent response, in priority order.
RESPONSE_EVENT_TYPES = ("LLM_RESPONSE", "AGENT_COMPLETED")


# ------------------------------------------------------------------ #
# Tool Outcome                                                         #
# ------------------------------------------------------------------ #


def is_tool_event(event_type: str) -> bool:
  """Returns True for tool-related event types."""
  return event_type in (
      "TOOL_STARTING",
      "TOOL_COMPLETED",
      "TOOL_ERROR",
      "TOOL_PAUSED",
  )


# tool_outcome is imported from udf_kernels above.


# ------------------------------------------------------------------ #
# Event Classification                                                 #
# ------------------------------------------------------------------ #


def is_hitl_event(event_type: str) -> bool:
  """Returns True for Human-in-the-Loop event types."""
  return event_type.startswith("HITL_")


def is_hitl_completed(event_type: str) -> bool:
  """Returns True for completed HITL events."""
  return event_type.startswith("HITL_") and event_type.endswith("_COMPLETED")


# All event types known to the SDK, grouped by family.
EVENT_FAMILIES = {
    "user": ["USER_MESSAGE_RECEIVED"],
    "invocation": [
        "INVOCATION_STARTING",
        "INVOCATION_COMPLETED",
    ],
    "agent": [
        "AGENT_STARTING",
        "AGENT_COMPLETED",
        "AGENT_TRANSFER",
        "AGENT_STATE_CHECKPOINT",
    ],
    "llm": ["LLM_REQUEST", "LLM_RESPONSE", "LLM_ERROR"],
    "tool": [
        "TOOL_STARTING",
        "TOOL_COMPLETED",
        "TOOL_ERROR",
        "TOOL_PAUSED",
    ],
    "state": ["STATE_DELTA"],
    "hitl": [
        "HITL_CONFIRMATION_REQUEST",
        "HITL_CONFIRMATION_REQUEST_COMPLETED",
        "HITL_CREDENTIAL_REQUEST",
        "HITL_CREDENTIAL_REQUEST_COMPLETED",
        "HITL_INPUT_REQUEST",
        "HITL_INPUT_REQUEST_COMPLETED",
    ],
    # ADK 2.0 (producer #293): session-history compaction + workflow
    # node boundaries (boundary derivation tracked in #207).
    "compaction": ["EVENT_COMPACTION"],
    "workflow": ["WORKFLOW_NODE_STARTING", "WORKFLOW_NODE_COMPLETED"],
}

ALL_KNOWN_EVENT_TYPES = [
    et for family in EVENT_FAMILIES.values() for et in family
]
