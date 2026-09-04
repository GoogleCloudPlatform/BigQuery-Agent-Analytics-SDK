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

"""Session metric helpers backed by BigQuery and SDK evaluators."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
import time
from typing import Any

_DEMO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ENV_PATH = os.path.join(_DEMO_DIR, ".env")


@dataclass(frozen=True)
class DemoConfig:
  project_id: str
  dataset_id: str
  table_id: str
  location: str

  @property
  def table_ref(self) -> str:
    return f"{self.project_id}.{self.dataset_id}.{self.table_id}"


def load_config() -> DemoConfig:
  """Load demo BigQuery configuration from ``.env`` and ADC."""
  if os.path.exists(_ENV_PATH):
    try:
      from dotenv import load_dotenv

      load_dotenv(dotenv_path=_ENV_PATH)
    except ImportError:
      pass
  try:
    import google.auth

    _, auth_project = google.auth.default()
  except Exception:
    auth_project = None
  project_id = (
      os.getenv("PROJECT_ID")
      or os.getenv("GOOGLE_CLOUD_PROJECT")
      or auth_project
  )
  if not project_id:
    raise RuntimeError(
        "PROJECT_ID is not set and no default Google Cloud project was found."
    )
  return DemoConfig(
      project_id=project_id,
      dataset_id=os.getenv(
          "SELF_EVOLVING_DATASET_ID", "self_evolving_agent_demo"
      ),
      table_id=os.getenv("SELF_EVOLVING_TABLE_ID", "agent_events"),
      location=os.getenv("DATASET_LOCATION", "us-central1"),
  )


def load_session_ids(path: str) -> list[str]:
  """Load non-empty session IDs from a run-agent result file."""
  with open(path) as f:
    data = json.load(f)
  if isinstance(data, dict):
    data = data.get("sessions", [])
  return [str(r["session_id"]) for r in data if r.get("session_id")]


def load_quality_summary(path: str) -> dict[str, Any]:
  """Summarize deterministic quality fields from run-agent results."""
  with open(path) as f:
    rows = json.load(f)
  if isinstance(rows, dict):
    rows = rows.get("sessions", [])
  total = len(rows)
  passed = sum(1 for r in rows if r.get("quality_passed"))
  expected_tool_used = sum(1 for r in rows if r.get("expected_tool_used"))
  avoid_tool_used = sum(1 for r in rows if r.get("avoid_tool_used"))
  return {
      "total": total,
      "passed": passed,
      "pass_rate": passed / total if total else 0.0,
      "expected_tool_used": expected_tool_used,
      "avoid_tool_used": avoid_tool_used,
  }


def _bq_client(config: DemoConfig) -> Any:
  from google.cloud import bigquery

  return bigquery.Client(project=config.project_id, location=config.location)


def fetch_session_metrics(
    session_ids: list[str],
    *,
    attempts: int = 1,
    wait_seconds: int = 0,
) -> list[dict[str, Any]]:
  """Fetch per-session token/tool metrics from the raw event table."""
  if not session_ids:
    return []
  from google.cloud import bigquery

  config = load_config()
  client = _bq_client(config)
  query = f"""
    SELECT
      session_id,
      COUNT(*) AS event_count,
      COUNTIF(event_type = 'LLM_REQUEST') AS llm_calls,
      COUNTIF(event_type = 'LLM_RESPONSE') AS llm_responses,
      COUNTIF(event_type = 'TOOL_STARTING') AS tool_calls,
      COUNTIF(event_type = 'TOOL_ERROR') AS tool_errors,
      COUNTIF(
        event_type = 'TOOL_STARTING'
        AND JSON_VALUE(content, '$.tool') = 'lookup_basketball_reference'
      ) AS broad_lookup_calls,
      SUM(COALESCE(
        SAFE_CAST(JSON_VALUE(
          attributes, '$.usage_metadata.prompt_token_count'
        ) AS INT64),
        SAFE_CAST(JSON_VALUE(content, '$.usage.prompt') AS INT64),
        SAFE_CAST(JSON_VALUE(attributes, '$.input_tokens') AS INT64),
        0
      )) AS input_tokens,
      SUM(COALESCE(
        SAFE_CAST(JSON_VALUE(
          attributes, '$.usage_metadata.candidates_token_count'
        ) AS INT64),
        SAFE_CAST(JSON_VALUE(content, '$.usage.completion') AS INT64),
        SAFE_CAST(JSON_VALUE(attributes, '$.output_tokens') AS INT64),
        0
      )) AS output_tokens,
      SUM(COALESCE(
        SAFE_CAST(JSON_VALUE(
          attributes, '$.usage_metadata.total_token_count'
        ) AS INT64),
        SAFE_CAST(JSON_VALUE(content, '$.usage.total') AS INT64),
        COALESCE(
          SAFE_CAST(JSON_VALUE(attributes, '$.input_tokens') AS INT64),
          0
        ) + COALESCE(
          SAFE_CAST(JSON_VALUE(attributes, '$.output_tokens') AS INT64),
          0
        )
      )) AS total_tokens
    FROM `{config.table_ref}`
    WHERE session_id IN UNNEST(@session_ids)
    GROUP BY session_id
    ORDER BY session_id
  """
  job_config = bigquery.QueryJobConfig(
      query_parameters=[
          bigquery.ArrayQueryParameter("session_ids", "STRING", session_ids),
      ]
  )
  rows: list[dict[str, Any]] = []
  for attempt in range(1, attempts + 1):
    if wait_seconds and attempt > 1:
      time.sleep(wait_seconds)
    rows = [
        dict(r) for r in client.query(query, job_config=job_config).result()
    ]
    if len(rows) >= len(set(session_ids)):
      break
  return rows


def require_complete_session_metrics(
    rows: list[dict[str, Any]],
    session_ids: list[str],
    *,
    label: str,
) -> None:
  """Validate that BigQuery returned complete and usable metric rows."""
  expected = {str(session_id) for session_id in session_ids if session_id}
  observed = {str(row.get("session_id", "")) for row in rows}
  missing = sorted(expected - observed)
  if missing:
    raise RuntimeError(
        f"Only found {len(observed)}/{len(expected)} {label} sessions in "
        "BigQuery after retries. Missing session IDs: " + ", ".join(missing)
    )

  total_events = sum(int(row.get("event_count") or 0) for row in rows)
  total_tokens = sum(float(row.get("total_tokens") or 0) for row in rows)
  if total_events and total_tokens == 0:
    config = load_config()
    raise RuntimeError(
        "Token extraction produced zero total tokens even though trace events "
        f"exist. The analytics plugin schema may have changed; inspect "
        f"LLM_RESPONSE rows in `{config.table_ref}`."
    )


def fetch_tool_counts(session_ids: list[str]) -> list[dict[str, Any]]:
  """Fetch aggregate tool-call counts for the selected sessions."""
  if not session_ids:
    return []
  from google.cloud import bigquery

  config = load_config()
  client = _bq_client(config)
  query = f"""
    SELECT
      JSON_VALUE(content, '$.tool') AS tool_name,
      COUNT(*) AS calls
    FROM `{config.table_ref}`
    WHERE session_id IN UNNEST(@session_ids)
      AND event_type = 'TOOL_STARTING'
    GROUP BY tool_name
    ORDER BY calls DESC, tool_name
  """
  job_config = bigquery.QueryJobConfig(
      query_parameters=[
          bigquery.ArrayQueryParameter("session_ids", "STRING", session_ids),
      ]
  )
  return [dict(r) for r in client.query(query, job_config=job_config).result()]


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
  """Aggregate per-session metrics into a compact summary."""
  if not rows:
    return {
        "sessions": 0,
        "avg_total_tokens": 0.0,
        "avg_input_tokens": 0.0,
        "avg_output_tokens": 0.0,
        "avg_tool_calls": 0.0,
        "avg_llm_calls": 0.0,
        "total_broad_lookup_calls": 0,
        "sessions_with_broad_lookup": 0,
        "broad_lookup_session_rate": 0.0,
        "total_tool_errors": 0,
    }
  count = len(rows)

  def total(name: str) -> float:
    return sum(float(r.get(name) or 0) for r in rows)

  broad_sessions = sum(1 for r in rows if int(r.get("broad_lookup_calls") or 0))
  return {
      "sessions": count,
      "avg_total_tokens": round(total("total_tokens") / count, 1),
      "avg_input_tokens": round(total("input_tokens") / count, 1),
      "avg_output_tokens": round(total("output_tokens") / count, 1),
      "avg_tool_calls": round(total("tool_calls") / count, 1),
      "avg_llm_calls": round(total("llm_calls") / count, 1),
      "total_broad_lookup_calls": int(total("broad_lookup_calls")),
      "sessions_with_broad_lookup": broad_sessions,
      "broad_lookup_session_rate": round(broad_sessions / count, 3),
      "total_tool_errors": int(total("tool_errors")),
  }


def run_sdk_evaluators(
    session_ids: list[str],
    *,
    token_budget: int,
    max_cost_usd: float,
    max_turns: int,
) -> dict[str, Any]:
  """Run SDK deterministic evaluator gates over the selected sessions."""
  from bigquery_agent_analytics import Client
  from bigquery_agent_analytics.trace import TraceFilter

  try:
    from bigquery_agent_analytics.evaluators import SystemEvaluator
  except ImportError:
    from bigquery_agent_analytics.evaluators import CodeEvaluator as SystemEvaluator

  config = load_config()
  client = Client(
      project_id=config.project_id,
      dataset_id=config.dataset_id,
      table_id=config.table_id,
      location=config.location,
  )
  filters = TraceFilter(session_ids=session_ids)
  evaluators = {
      "token_efficiency": SystemEvaluator.token_efficiency(
          max_tokens=token_budget
      ),
      "cost": SystemEvaluator.cost_per_session(max_cost_usd=max_cost_usd),
      "turn_count": SystemEvaluator.turn_count(max_turns=max_turns),
      "error_rate": SystemEvaluator.error_rate(max_error_rate=0.0),
  }
  reports = {}
  for name, evaluator in evaluators.items():
    report = client.evaluate(evaluator=evaluator, filters=filters)
    observed = []
    for session_score in report.session_scores:
      for detail in session_score.details.values():
        if isinstance(detail, dict) and detail.get("observed") is not None:
          observed.append(detail["observed"])
    reports[name] = {
        "total_sessions": report.total_sessions,
        "passed_sessions": report.passed_sessions,
        "failed_sessions": report.failed_sessions,
        "pass_rate": report.pass_rate,
        "avg_observed": (
            round(sum(observed) / len(observed), 4) if observed else None
        ),
    }
  return reports
