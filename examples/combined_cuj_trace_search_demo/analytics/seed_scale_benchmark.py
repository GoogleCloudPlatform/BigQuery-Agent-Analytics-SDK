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

"""Deterministic 5,000-row synthetic `eval_turns` generator for IVF scale runs.

BigQuery requires at least 5,000 rows in a table before creating an `IVF`
`VECTOR INDEX`. This module generates deterministic synthetic turn rows matching
the 25-column materialized `eval_turns` schema so `--with-vector-index-scale`
can test live `CREATE VECTOR INDEX ... STORING (...)` end to end without
polluting default small demo runs.
"""

from __future__ import annotations

import argparse
from datetime import datetime
from datetime import timedelta
from datetime import timezone
import json
import re
import sys
from typing import Any

_PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_DATASET_TABLE_ID_RE = re.compile(r"^[A-Za-z0-9_]+$")


def validate_bq_identifiers(
    project_id: str, dataset_id: str, table_id: str
) -> None:
  """Validates BigQuery project, dataset, and table identifiers."""
  if not _PROJECT_ID_RE.match(project_id):
    raise ValueError(f"Invalid BigQuery project_id: {project_id!r}")
  if not _DATASET_TABLE_ID_RE.match(dataset_id):
    raise ValueError(f"Invalid BigQuery dataset_id: {dataset_id!r}")
  if not _DATASET_TABLE_ID_RE.match(table_id):
    raise ValueError(f"Invalid BigQuery table_id: {table_id!r}")


_ARCHETYPES: tuple[dict[str, Any], ...] = (
    {
        "tag": "healthy_refund",
        "user": "Please issue a refund for order ORD-9012.",
        "tool": "issue_refund",
        "tool_res": "ok=true status=refunded",
        "agent": "Your refund for order ORD-9012 has been completed.",
        "has_error": False,
        "tool_calls": 2,
        "tool_errors": 0,
        "has_tool_loop": False,
        "max_consec": 1,
        "error_summary": None,
        "prompt_tokens": 1120,
        "completion_tokens": 88,
        "latency_ms": 1350,
    },
    {
        "tag": "exact_card_expired",
        "user": "Please refund order ORD-8841 to my card.",
        "tool": "issue_refund",
        "tool_res": "ERROR: CARD_EXPIRED",
        "agent": "Sorry, your refund failed because the card has expired.",
        "has_error": True,
        "tool_calls": 1,
        "tool_errors": 1,
        "has_tool_loop": False,
        "max_consec": 1,
        "error_summary": "CARD_EXPIRED",
        "prompt_tokens": 1240,
        "completion_tokens": 92,
        "latency_ms": 1620,
    },
    {
        "tag": "silent_contradiction",
        "user": "Process refund for order ORD-8841.",
        "tool": "issue_refund",
        "tool_res": "ERROR: CARD_EXPIRED",
        "agent": "Your refund for order ORD-8841 has been processed!",
        "has_error": True,
        "tool_calls": 1,
        "tool_errors": 1,
        "has_tool_loop": False,
        "max_consec": 1,
        "error_summary": "CARD_EXPIRED",
        "prompt_tokens": 1280,
        "completion_tokens": 76,
        "latency_ms": 1700,
    },
    {
        "tag": "retry_loop",
        "user": "Check warehouse delay status for order ORD-7710.",
        "tool": "search_knowledge_base",
        "tool_res": "ERROR: DEADLINE_EXCEEDED",
        "agent": "Sorry, the knowledge base lookup failed after multiple retries.",
        "has_error": True,
        "tool_calls": 4,
        "tool_errors": 4,
        "has_tool_loop": True,
        "max_consec": 4,
        "error_summary": "DEADLINE_EXCEEDED",
        "prompt_tokens": 4180,
        "completion_tokens": 210,
        "latency_ms": 6400,
    },
    {
        "tag": "verbose_audit",
        "user": "Export diagnostic audit summary for account ACCT-10.",
        "tool": "export_audit_blob",
        "tool_res": "ok=true rows=128 summary=clean",
        "agent": "Audit summary exported successfully for account ACCT-10.",
        "has_error": False,
        "tool_calls": 1,
        "tool_errors": 0,
        "has_tool_loop": False,
        "max_consec": 1,
        "error_summary": None,
        "prompt_tokens": 5600,
        "completion_tokens": 140,
        "latency_ms": 2050,
    },
)


def generate_synthetic_eval_turns(
    count: int = 5000,
    *,
    base_timestamp: str = "2026-11-24T00:00:00+00:00",
    prefix: str = "scale_synth",
) -> list[dict[str, Any]]:
  """Generates ``count`` deterministic 25-column ``eval_turns`` dicts."""
  if count < 0:
    raise ValueError("count must be non-negative")
  start_dt = datetime.fromisoformat(base_timestamp)
  if start_dt.tzinfo is None:
    start_dt = start_dt.replace(tzinfo=timezone.utc)

  rows: list[dict[str, Any]] = []
  boiler = "Customer support policy instructions. " * 180
  for i in range(count):
    arch = _ARCHETYPES[i % len(_ARCHETYPES)]
    ts_start = start_dt + timedelta(seconds=i)
    ts_end = ts_start + timedelta(milliseconds=int(arch["latency_ms"]))
    session_id = f"{prefix}_{i:05d}"
    invocation_id = f"inv_{prefix}_{i:05d}"
    root_agent = "support_agent"
    trajectory = (
        f"[OUTCOME: root_agent={root_agent} "
        f"has_error={str(arch['has_error']).lower()} "
        f"tool_calls={arch['tool_calls']} tool_errors={arch['tool_errors']} "
        f"loop={str(arch['has_tool_loop']).lower()} "
        f"latency_ms={arch['latency_ms']}]\n"
        f"USER: {arch['user']} (sample {i})\n"
        f"TOOL: {arch['tool']}() -> {arch['tool_res']}\n"
        f"AGENT: {arch['agent']}"
    )
    trace_obj = {
        "agents": {
            root_agent: {
                "agent_id": root_agent,
                "instruction": boiler,
                "description": "Synthetic scale benchmark support agent",
                "sub_agents": [],
                "tools": [{"function_declarations": [{"name": arch["tool"]}]}],
            }
        },
        "turns": [
            {
                "turn_index": 0,
                "turn_id": invocation_id,
                "events": [
                    {
                        "author": "user",
                        "content": {
                            "role": "user",
                            "parts": [{"text": arch["user"]}],
                        },
                        "event_time": ts_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    },
                    {
                        "author": root_agent,
                        "content": {
                            "role": "model",
                            "parts": [{"text": arch["agent"]}],
                        },
                        "event_time": ts_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    },
                ],
            }
        ],
    }
    raw_trace_bytes = len(
        json.dumps(trace_obj, separators=(",", ":")).encode("utf-8")
    )
    rows.append(
        {
            "session_id": session_id,
            "invocation_id": invocation_id,
            "turn_index": 0,
            "user_id": "scale_benchmark_user",
            "root_agent": root_agent,
            "turn_start_timestamp": ts_start.isoformat(),
            "turn_end_timestamp": ts_end.isoformat(),
            "tool_call_count": int(arch["tool_calls"]),
            "tool_error_count": int(arch["tool_errors"]),
            "llm_error_count": 0,
            "agent_error_count": 0,
            "has_error": bool(arch["has_error"]),
            "has_truncated_content": False,
            "has_tool_loop": bool(arch["has_tool_loop"]),
            "max_consecutive_identical_tool_calls": int(arch["max_consec"]),
            "distinct_tools_used": [str(arch["tool"])],
            "total_prompt_tokens": int(arch["prompt_tokens"]),
            "total_completion_tokens": int(arch["completion_tokens"]),
            "total_tokens": int(arch["prompt_tokens"])
            + int(arch["completion_tokens"]),
            "turn_latency_ms": int(arch["latency_ms"]),
            "eval_ready": True,
            "trace": trace_obj,
            "error_summary": arch["error_summary"],
            "raw_trace_bytes": raw_trace_bytes,
            "_ai_index_trajectory": trajectory,
        }
    )
  return rows


def seed_scale_rows_to_bigquery(
    bq_client: Any,
    *,
    project_id: str,
    dataset_id: str,
    eval_turns_table: str = "eval_turns",
    count: int = 5000,
    prefix: str = "scale_synth",
    base_timestamp: str | None = None,
    location: str = "US",
) -> int:
  """Idempotently seeds ``count`` synthetic rows into ``eval_turns``."""
  from google.cloud import bigquery

  validate_bq_identifiers(project_id, dataset_id, eval_turns_table)
  _ = location  # Caller initializes BigQuery client in the target location.
  effective_base_ts = (
      base_timestamp
      if base_timestamp is not None
      else (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
  )
  table_fqn = f"{project_id}.{dataset_id}.{eval_turns_table}"
  delete_sql = (
      f"DELETE FROM `{table_fqn}` WHERE STARTS_WITH(session_id, @prefix)"
  )
  bq_client.query(
      delete_sql,
      job_config=bigquery.QueryJobConfig(
          query_parameters=[
              bigquery.ScalarQueryParameter("prefix", "STRING", f"{prefix}_")
          ]
      ),
  ).result()

  rows = generate_synthetic_eval_turns(
      count=count, base_timestamp=effective_base_ts, prefix=prefix
  )
  if not rows:
    return 0

  target_table = bq_client.get_table(table_fqn)
  writable_schema = [
      field
      for field in target_table.schema
      if field.name != "_ai_index_trajectory_embedding"
  ]
  job_config = bigquery.LoadJobConfig(
      schema=writable_schema,
      write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
  )
  chunk_size = 1000
  inserted = 0
  for idx in range(0, len(rows), chunk_size):
    chunk = rows[idx : idx + chunk_size]
    bq_client.load_table_from_json(
        chunk, table_fqn, job_config=job_config
    ).result()
    inserted += len(chunk)
  return inserted


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
  parser = argparse.ArgumentParser(
      description="Seed 5,000 synthetic eval_turns rows for IVF vector indexing."
  )
  parser.add_argument("--project-id", required=True)
  parser.add_argument("--dataset-id", required=True)
  parser.add_argument("--eval-turns-table", default="eval_turns")
  parser.add_argument("--location", default="US")
  parser.add_argument("--count", type=int, default=5000)
  parser.add_argument("--dry-run", action="store_true")
  return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
  args = _parse_args(argv)
  validate_bq_identifiers(
      args.project_id, args.dataset_id, args.eval_turns_table
  )
  if args.dry_run:
    rows = generate_synthetic_eval_turns(count=args.count)
    print(json.dumps({"generated_rows": len(rows), "dry_run": True}, indent=2))
    return 0

  from google.cloud import bigquery

  client = bigquery.Client(project=args.project_id, location=args.location)
  inserted = seed_scale_rows_to_bigquery(
      client,
      project_id=args.project_id,
      dataset_id=args.dataset_id,
      eval_turns_table=args.eval_turns_table,
      count=args.count,
      location=args.location,
  )
  print(json.dumps({"inserted_rows": inserted, "dry_run": False}, indent=2))
  return 0


if __name__ == "__main__":
  sys.exit(main())
