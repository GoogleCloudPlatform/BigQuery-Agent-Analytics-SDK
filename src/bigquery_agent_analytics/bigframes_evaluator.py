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

"""BigFrames-based evaluation for BigQuery Agent Analytics.

Provides a Pythonic DataFrame API for evaluating agent traces using
BigFrames (``bigframes.pandas``) and BigQuery's ``AI.GENERATE``
operator.  This is useful in notebook environments where users
prefer working with DataFrames over raw SQL.

Example usage::

    from bigquery_agent_analytics import (
        BigFramesEvaluator,
    )

    evaluator = BigFramesEvaluator(
        project_id="my-project",
        dataset_id="analytics",
        table_id="agent_events",
    )

    scores_df = evaluator.evaluate_sessions(max_sessions=50)
    print(scores_df[["session_id", "score", "justification"]])

Requires the optional ``bigframes`` dependency::

    pip install bigframes
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger("bigquery_agent_analytics." + __name__)

_DEFAULT_ENDPOINT = "gemini-2.5-flash"

_JUDGE_PROMPT = (
    "You are evaluating an AI agent session. "
    "Score the session on a scale of 1 to 10 for overall "
    "quality, considering task completion, efficiency, and "
    "correctness.\\n"
    "Session trace:\\n"
)

_FACET_PROMPT = (
    "Analyze this agent conversation transcript and extract "
    "structured facets.\\n\\nTranscript:\\n"
)


class BigFramesEvaluator:
  """Evaluate agent sessions using BigFrames + AI.GENERATE.

  Wraps ``bigframes.pandas.read_gbq`` for data loading and
  ``bigframes.bigquery.ai.generate`` for LLM evaluation with
  typed ``output_schema``.

  Args:
      project_id: Google Cloud project ID.
      dataset_id: BigQuery dataset ID.
      table_id: Events table name.
      endpoint: AI.GENERATE endpoint (default
          ``gemini-2.5-flash``).
      connection_id: Optional BigQuery connection resource ID.
  """

  def __init__(
      self,
      project_id: str,
      dataset_id: str,
      table_id: str = "agent_events",
      endpoint: Optional[str] = None,
      connection_id: Optional[str] = None,
  ) -> None:
    self.project_id = project_id
    self.dataset_id = dataset_id
    self.table_id = table_id
    self.endpoint = endpoint or _DEFAULT_ENDPOINT
    self.connection_id = connection_id

  def evaluate_sessions(
      self,
      max_sessions: int = 50,
      judge_prompt: Optional[str] = None,
  ):
    """Evaluates sessions and returns a DataFrame with scores.

    Reads traces via ``bigframes.pandas.read_gbq``, constructs
    prompts, and calls ``bigframes.bigquery.ai.generate`` with
    ``output_schema`` for typed ``score`` and ``justification``
    columns.

    Args:
        max_sessions: Maximum sessions to evaluate.
        judge_prompt: Custom judge prompt prefix.  Defaults to
            a built-in quality evaluation prompt.

    Returns:
        A ``bigframes.dataframe.DataFrame`` with columns
        ``session_id``, ``score``, and ``justification``.
    """
    import bigframes.bigquery as bbq
    import bigframes.pandas as bpd

    prompt_prefix = judge_prompt or _JUDGE_PROMPT

    query = (
        "SELECT session_id, STRING_AGG("
        "CONCAT(event_type, ': ', "
        "COALESCE(JSON_EXTRACT_SCALAR("
        "content, '$.text_summary'), '')), "
        "'\\n' ORDER BY timestamp) AS trace_text "
        f"FROM `{self.project_id}.{self.dataset_id}"
        f".{self.table_id}` "
        "GROUP BY session_id "
        "HAVING LENGTH(trace_text) > 10 "
        f"LIMIT {max_sessions}"
    )

    df = bpd.read_gbq(query)
    df["prompt"] = prompt_prefix + df["trace_text"]

    result = bbq.ai.generate(
        df["prompt"],
        endpoint=self.endpoint,
        output_schema={
            "score": "INT64",
            "justification": "STRING",
        },
    )

    result["session_id"] = df["session_id"]
    return result[["session_id", "score", "justification"]]

  def extract_facets(
      self,
      session_ids: Optional[list[str]] = None,
      max_sessions: int = 50,
  ):
    """Extracts structured facets for sessions.

    Uses ``output_schema`` with AI.GENERATE to produce typed
    facet columns directly in the returned DataFrame.

    Args:
        session_ids: Optional list of session IDs.  If ``None``,
            the most recent *max_sessions* are used.
        max_sessions: Maximum sessions when *session_ids* is
            ``None``.

    Returns:
        A ``bigframes.dataframe.DataFrame`` with typed facet
        columns.
    """
    import bigframes.bigquery as bbq
    import bigframes.pandas as bpd

    if session_ids:
      import re

      _SESSION_ID_RE = re.compile(r"^[\w\-\.]+$")
      for sid in session_ids:
        if not _SESSION_ID_RE.match(sid):
          raise ValueError(
              f"Invalid session_id: {sid!r}. "
              "Only alphanumerics, hyphens, underscores, "
              "and dots are allowed."
          )
      ids_str = ", ".join(f"'{s}'" for s in session_ids)
      where = f"session_id IN ({ids_str})"
    else:
      where = "TRUE"

    query = (
        "SELECT session_id, STRING_AGG("
        "CONCAT(event_type, "
        "COALESCE(CONCAT(' [', agent, ']'), ''), "
        "': ', COALESCE("
        "JSON_EXTRACT_SCALAR(content, '$.text_summary'), "
        "JSON_EXTRACT_SCALAR(content, '$.response'), "
        "JSON_EXTRACT_SCALAR(content, '$.tool'), "
        "'')), "
        "'\\n' ORDER BY timestamp) AS transcript "
        f"FROM `{self.project_id}.{self.dataset_id}"
        f".{self.table_id}` "
        f"WHERE {where} "
        "GROUP BY session_id "
        f"LIMIT {max_sessions}"
    )

    df = bpd.read_gbq(query)
    df["prompt"] = _FACET_PROMPT + df["transcript"]

    result = bbq.ai.generate(
        df["prompt"],
        endpoint=self.endpoint,
        output_schema={
            "goal_categories": "ARRAY<STRING>",
            "outcome": "STRING",
            "satisfaction": "STRING",
            "friction_types": "ARRAY<STRING>",
            "session_type": "STRING",
            "agent_effectiveness": "INT64",
            "primary_success": "BOOL",
            "key_topics": "ARRAY<STRING>",
            "summary": "STRING",
        },
    )

    result["session_id"] = df["session_id"]
    return result
