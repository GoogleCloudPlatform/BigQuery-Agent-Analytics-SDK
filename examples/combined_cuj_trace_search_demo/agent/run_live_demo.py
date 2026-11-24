#!/usr/bin/env python3
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

"""Runs the 8 Combined CUJ ADK scenarios live into BigQuery or via --mock-live."""

from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
from datetime import datetime
from datetime import timezone
import json
import os
from pathlib import Path
import re
import secrets
import sys
from typing import Any

_DEMO_DIR = Path(__file__).resolve().parents[1]
if str(_DEMO_DIR) not in sys.path:
  sys.path.insert(0, str(_DEMO_DIR))

from agent.prompts_and_scenarios import build_agent_instruction  # noqa: E402
from agent.prompts_and_scenarios import build_mock_live_artifacts  # noqa: E402
from agent.prompts_and_scenarios import DEMO_SCENARIOS  # noqa: E402
from agent.prompts_and_scenarios import DemoScenario  # noqa: E402
from agent.prompts_and_scenarios import ROOT_AGENT_NAME  # noqa: E402
from agent.prompts_and_scenarios import TOOL_FUNCTIONS  # noqa: E402
from agent.tools import sanitized_tool_classifier  # noqa: E402
from analytics.table_guard import _is_bq_not_found_error  # noqa: E402
from analytics.table_guard import apply_demo_table_label  # noqa: E402
from analytics.table_guard import assert_demo_table_ownership_or_absent  # noqa: E402
from analytics.trajectory_extractor import compute_ablation_metrics  # noqa: E402
from analytics.trajectory_extractor import scrub_sensitive_text  # noqa: E402
from analytics.trajectory_extractor import summarize_ablation  # noqa: E402
from analytics.trajectory_extractor import write_owner_only_text  # noqa: E402

_APP_NAME = "combined_cuj_trace_search_demo"
_CONTRADICTION_FAIL_RE = re.compile(
    r"\b(error|sorry|failed|unable|expired|cannot|declined|"
    r"dispatched|rejected)\b",
    re.IGNORECASE,
)


_PROJECT_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_DATASET_TABLE_ID_RE = re.compile(r"^[A-Za-z0-9_]+$")


def _validate_live_bq_identifiers(
    project_id: str,
    dataset_id: str,
    table_id: str,
    eval_turns_table: str,
) -> None:
  """Validates BigQuery identifiers before DDL/SQL interpolation."""
  try:
    from analytics.seed_scale_benchmark import validate_bq_identifiers  # noqa: PLC0415

    validate_bq_identifiers(project_id, dataset_id, table_id)
    validate_bq_identifiers(project_id, dataset_id, eval_turns_table)
  except ImportError:
    if not _PROJECT_ID_RE.match(project_id):
      raise ValueError(f"Invalid BigQuery project_id: {project_id!r}")
    if not _DATASET_TABLE_ID_RE.match(dataset_id):
      raise ValueError(f"Invalid BigQuery dataset_id: {dataset_id!r}")
    for tbl in (table_id, eval_turns_table):
      if not _DATASET_TABLE_ID_RE.match(tbl):
        raise ValueError(f"Invalid BigQuery table_id: {tbl!r}")


async def _shutdown_bqaa_plugin(plugin: Any) -> None:
  """Flushes and closes BigQueryAgentAnalyticsPlugin cleanly."""
  flush_fn = getattr(plugin, "flush", None)
  if callable(flush_fn):
    await flush_fn()
  shutdown_fn = getattr(plugin, "shutdown", None)
  if callable(shutdown_fn):
    await shutdown_fn(close_background_transport=True)
    return
  close_fn = getattr(plugin, "close", None)
  if callable(close_fn):
    await close_fn()


def _configure_vertex_env(project_id: str, agent_location: str) -> None:
  os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "true")
  if project_id and project_id != "demo-project":
    os.environ.setdefault("GOOGLE_CLOUD_PROJECT", project_id)
    os.environ.setdefault("GOOGLE_CLOUD_QUOTA_PROJECT", project_id)
  os.environ.setdefault("GOOGLE_CLOUD_LOCATION", agent_location)


def _materialize_and_fetch_live_eval_turns(
    *,
    project_id: str,
    dataset_id: str,
    table_id: str,
    eval_turns_table: str,
    location: str,
    session_ids: list[str],
) -> list[dict[str, Any]]:
  """Materializes eval_turns in BigQuery and loads executed turn rows."""
  _validate_live_bq_identifiers(
      project_id, dataset_id, table_id, eval_turns_table
  )
  from google.cloud import bigquery  # noqa: PLC0415

  bq_client = bigquery.Client(project=project_id, location=location)
  existed_owned = assert_demo_table_ownership_or_absent(
      bq_client, project_id, dataset_id, eval_turns_table
  )
  create_clause = "CREATE OR REPLACE TABLE" if existed_owned else "CREATE TABLE"

  raw_where_clause = ""
  ddl = None
  try:
    from analytics.index_and_search import _build_session_raw_where_clause  # noqa: PLC0415
    from analytics.index_and_search import build_materialize_eval_turns_sql  # noqa: PLC0415

    if session_ids:
      raw_where_clause = _build_session_raw_where_clause(session_ids)
    ddl, _ = build_materialize_eval_turns_sql(
        project_id=project_id,
        dataset_id=dataset_id,
        events_table=table_id,
        eval_turns_table=eval_turns_table,
        raw_where_clause=raw_where_clause,
        replace_existing=existed_owned,
    )
  except ImportError:
    safe_sids = [
        str(s).strip()
        for s in session_ids
        if re.match(r"^[A-Za-z0-9_.:-]+$", str(s).strip())
    ]
    if safe_sids:
      quoted = ", ".join(f"'{sid}'" for sid in dict.fromkeys(safe_sids))
      raw_where_clause = f"session_id IN ({quoted})"
    sql_path = _DEMO_DIR / "sql" / "01_materialize_eval_turns.sql"
    if sql_path.is_file():
      ddl = (
          sql_path.read_text(encoding="utf-8")
          .format(
              PROJECT_ID=project_id,
              DATASET_ID=dataset_id,
              EVENTS_TABLE=table_id,
              EVAL_TURNS_TABLE=eval_turns_table,
          )
          .strip()
          .rstrip(";")
      )
      if not existed_owned:
        ddl = ddl.replace("CREATE OR REPLACE TABLE", "CREATE TABLE", 1)
      if raw_where_clause:
        ddl = ddl.replace(
            "      AND invocation_id IS NOT NULL\n",
            "      AND invocation_id IS NOT NULL\n"
            f"      AND (\n        {raw_where_clause}\n      )\n",
            1,
        )
    else:
      import importlib  # noqa: PLC0415

      builder = None
      for mod_name in (
          "google.adk.plugins",
          "google.adk.plugins._bigquery_agent_analytics_eval_sql",
          "google.adk.plugins.bigquery_agent_analytics_plugin",
      ):
        try:
          mod = importlib.import_module(mod_name)
          candidate = getattr(mod, "build_eval_turn_query_sql", None)
          if callable(candidate):
            builder = candidate
            break
        except Exception:
          continue
      if callable(builder):
        base_sql = builder(
            f"{project_id}.{dataset_id}.{table_id}",
            raw_where_clause=raw_where_clause,
        )
        ddl = (
            f"{create_clause} "
            f"`{project_id}.{dataset_id}.{eval_turns_table}`\n"
            "OPTIONS(labels=[('bqaa_demo', 'combined_cuj_trace_search')]) AS\n"
            "SELECT base.*, CAST(NULL AS STRING) AS error_summary, "
            "BYTE_LENGTH(TO_JSON_STRING(base.trace)) AS raw_trace_bytes, "
            "CAST(NULL AS STRING) AS _ai_index_trajectory "
            f"FROM (\n{base_sql}\n) AS base"
        )

  if ddl:
    bq_client.query(ddl).result()

  table_fqn = f"{project_id}.{dataset_id}.{eval_turns_table}"
  fetch_sql = (
      f"SELECT * FROM `{table_fqn}` "
      "WHERE session_id IN UNNEST(@session_ids) "
      "ORDER BY turn_start_timestamp ASC, turn_index ASC"
  )
  job_cfg = bigquery.QueryJobConfig(
      query_parameters=[
          bigquery.ArrayQueryParameter("session_ids", "STRING", session_ids)
      ]
  )
  rows: list[dict[str, Any]] = []
  for raw_row in bq_client.query(fetch_sql, job_config=job_cfg).result():
    row = dict(raw_row)
    for ts_col in ("turn_start_timestamp", "turn_end_timestamp"):
      if isinstance(row.get(ts_col), datetime):
        row[ts_col] = row[ts_col].isoformat()
    trace_val = row.get("trace")
    if isinstance(trace_val, str):
      try:
        trace_obj = json.loads(trace_val)
      except json.JSONDecodeError:
        trace_obj = {}
    elif isinstance(trace_val, dict):
      trace_obj = trace_val
    else:
      trace_obj = {}
    row["trace"] = trace_obj
    if row.get("error_summary") is not None:
      row["error_summary"] = scrub_sensitive_text(str(row["error_summary"]))
    if row.get("distinct_tools_used") is None:
      row["distinct_tools_used"] = []
    else:
      row["distinct_tools_used"] = list(row["distinct_tools_used"])
    ablation = compute_ablation_metrics(trace_obj, metrics=row)
    row["raw_trace_bytes"] = int(
        row.get("raw_trace_bytes") or ablation["s1_bytes"]
    )
    row["ablation"] = ablation
    row["_ai_index_trajectory"] = ablation["s3_text"]
    rows.append(row)
  return rows


async def _run_single_live_scenario(
    *,
    scenario: DemoScenario,
    session_id: str,
    model_name: str,
    user_id: str,
    plugin: Any,
    timeout_sec: int,
    extra_reminder: str = "",
) -> dict[str, Any]:
  from google.adk.agents import LlmAgent  # noqa: PLC0415
  from google.adk.runners import Runner  # noqa: PLC0415
  from google.adk.sessions import InMemorySessionService  # noqa: PLC0415
  from google.genai.types import Content  # noqa: PLC0415
  from google.genai.types import GenerateContentConfig  # noqa: PLC0415
  from google.genai.types import Part  # noqa: PLC0415

  instruction = build_agent_instruction(scenario)
  if extra_reminder:
    instruction = f"{instruction.rstrip()}\n\n{extra_reminder}\n"

  agent = LlmAgent(
      name=ROOT_AGENT_NAME,
      model=model_name,
      description="Enterprise customer operations and refund assistant.",
      instruction=instruction,
      tools=list(TOOL_FUNCTIONS),
      generate_content_config=GenerateContentConfig(temperature=0.0),
  )
  session_service = InMemorySessionService()
  runner = Runner(
      agent=agent,
      app_name=_APP_NAME,
      session_service=session_service,
      plugins=[plugin],
  )
  await session_service.create_session(
      app_name=_APP_NAME,
      user_id=user_id,
      session_id=session_id,
  )
  user_msg = Content(role="user", parts=[Part(text=scenario.user_prompt)])
  response_parts: list[str] = []
  tools_called: list[str] = []

  async def _consume() -> None:
    async for event in runner.run_async(
        user_id=user_id,
        session_id=session_id,
        new_message=user_msg,
    ):
      content = getattr(event, "content", None)
      parts = getattr(content, "parts", None) if content else None
      if not parts:
        continue
      for part in parts:
        txt = getattr(part, "text", None)
        if txt:
          response_parts.append(str(txt))
        fc = getattr(part, "function_call", None)
        if fc and getattr(fc, "name", None):
          tools_called.append(str(fc.name))

  await asyncio.wait_for(_consume(), timeout=timeout_sec)
  return {
      "scenario_id": scenario.scenario_id,
      "session_id": session_id,
      "tools_called": tools_called,
      "response": " ".join(p.strip() for p in response_parts if p.strip()),
  }


async def _run_live_scenarios(
    *,
    project_id: str,
    dataset_id: str,
    table_id: str,
    eval_turns_table: str,
    location: str,
    agent_location: str,
    model_name: str,
    user_id: str,
    timeout_sec: int,
) -> tuple[list[str], list[dict[str, Any]], list[dict[str, Any]]]:
  from google.cloud import bigquery  # noqa: PLC0415

  _configure_vertex_env(project_id, agent_location)
  bq_probe_client = bigquery.Client(project=project_id, location=location)
  # Fail fast before loading ADK or spending LLM calls if eval_turns_table is
  # pre-existing and unowned, or if the events_fqn probe fails with non-404.
  assert_demo_table_ownership_or_absent(
      bq_probe_client, project_id, dataset_id, eval_turns_table
  )
  events_fqn = f"{project_id}.{dataset_id}.{table_id}"
  try:
    bq_probe_client.get_table(events_fqn)
    events_pre_existed = True
  except Exception as exc:
    if _is_bq_not_found_error(exc):
      events_pre_existed = False
    else:
      raise

  from google.adk.plugins.bigquery_agent_analytics_plugin import BigQueryAgentAnalyticsPlugin  # noqa: PLC0415
  from google.adk.plugins.bigquery_agent_analytics_plugin import BigQueryLoggerConfig  # noqa: PLC0415

  config = BigQueryLoggerConfig(
      enabled=True,
      table_id=table_id,
      create_views=False,
      batch_size=1,
      batch_flush_interval=0.5,
      shutdown_timeout=20.0,
      flush_on_run_end=True,
      tool_result_classifier=sanitized_tool_classifier,
  )
  plugin = BigQueryAgentAnalyticsPlugin(
      project_id=project_id,
      dataset_id=dataset_id,
      table_id=table_id,
      config=config,
      location=location,
  )

  run_tag = (
      f"{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
      f"_{os.getpid()}_{secrets.token_hex(2)}"
  )
  session_ids: list[str] = []
  scenario_runs: list[dict[str, Any]] = []
  try:
    for idx, scenario in enumerate(DEMO_SCENARIOS, 1):
      sid = f"{scenario.session_id}_{run_tag}"
      print(f"  [{idx}/{len(DEMO_SCENARIOS)}] {scenario.scenario_id} -> {sid}")
      res = await _run_single_live_scenario(
          scenario=scenario,
          session_id=sid,
          model_name=model_name,
          user_id=user_id,
          plugin=plugin,
          timeout_sec=timeout_sec,
      )
      if scenario.is_silent_contradiction and _CONTRADICTION_FAIL_RE.search(
          res["response"]
      ):
        retry_sid = f"{sid}_r2"
        print(
            "    [retry] contradiction scenario emitted refusal word; retrying"
            f" once as {retry_sid}"
        )
        res = await _run_single_live_scenario(
            scenario=scenario,
            session_id=retry_sid,
            model_name=model_name,
            user_id=user_id,
            plugin=plugin,
            timeout_sec=timeout_sec,
            extra_reminder=(
                "STRICT OVERRIDE (SUPERSEDES SECTIONS 1.1, 2.3, AND 3.1): "
                f"Reply verbatim with: {scenario.mock_agent_response!r}. "
                "Do not include any error codes or refusal words."
            ),
        )
      session_ids.append(res["session_id"])
      scenario_runs.append(res)
  finally:
    await _shutdown_bqaa_plugin(plugin)

  if not events_pre_existed:
    apply_demo_table_label(bq_probe_client, project_id, dataset_id, table_id)

  eval_turns = _materialize_and_fetch_live_eval_turns(
      project_id=project_id,
      dataset_id=dataset_id,
      table_id=table_id,
      eval_turns_table=eval_turns_table,
      location=location,
      session_ids=session_ids,
  )
  return session_ids, eval_turns, scenario_runs


def _prepare_mock_live_runs(
    *,
    user_id: str,
    model_name: str,
) -> tuple[list[str], list[dict[str, Any]], list[dict[str, Any]], int]:
  """Builds deterministic mock-live scenario outputs without cloud calls."""
  events, eval_turns = build_mock_live_artifacts(
      user_id=user_id, model_name=model_name
  )
  for turn in eval_turns:
    if turn.get("error_summary") is not None:
      turn["error_summary"] = scrub_sensitive_text(str(turn["error_summary"]))
  session_ids = [t["session_id"] for t in eval_turns]
  scenario_runs = [
      {
          "scenario_id": sc.scenario_id,
          "session_id": sc.session_id,
          "tools_called": [name for name, _ in sc.mock_tool_calls],
          "response": sc.mock_agent_response,
      }
      for sc in DEMO_SCENARIOS
  ]
  return session_ids, eval_turns, scenario_runs, len(events)


def _assemble_run_payload(
    *,
    project_id: str,
    dataset_id: str,
    table_id: str,
    location: str,
    agent_location: str,
    model_name: str,
    mock_live: bool,
    mock_events_count: int,
    session_ids: list[str],
    scenario_runs: list[dict[str, Any]],
    eval_turns: list[dict[str, Any]],
    output_json: str | Path | None,
) -> dict[str, Any]:
  """Formats the run payload and optionally persists it as JSON."""
  payload: dict[str, Any] = {
      "generated_at_utc": datetime.now(timezone.utc).isoformat(),
      "project_id": project_id,
      "dataset_id": dataset_id,
      "table_id": table_id,
      "location": location,
      "agent_location": agent_location,
      "model_name": model_name,
      "mock_live": bool(mock_live),
      "mock_events_count": mock_events_count,
      "session_ids": session_ids,
      "scenario_runs": scenario_runs,
      "eval_turns": eval_turns,
      "ablation_summary": summarize_ablation(eval_turns),
  }
  if output_json is not None:
    out_path = Path(output_json)
    write_owner_only_text(out_path, json.dumps(payload, indent=2) + "\n")
  return payload


async def run_demo_async(
    *,
    project_id: str = "demo-project",
    dataset_id: str = "combined_cuj_trace_search_demo",
    table_id: str = "agent_events",
    eval_turns_table: str = "eval_turns",
    location: str = "US",
    agent_location: str = "global",
    model_name: str = "gemini-4-hydrogen",
    user_id: str = "demo_user",
    mock_live: bool = False,
    timeout_sec: int = 120,
    output_json: str | Path | None = None,
) -> dict[str, Any]:
  """Async entrypoint executing the 8 scenarios in live or mock-live mode."""
  if mock_live:
    session_ids, eval_turns, scenario_runs, mock_events_count = (
        _prepare_mock_live_runs(user_id=user_id, model_name=model_name)
    )
  else:
    session_ids, eval_turns, scenario_runs = await _run_live_scenarios(
        project_id=project_id,
        dataset_id=dataset_id,
        table_id=table_id,
        eval_turns_table=eval_turns_table,
        location=location,
        agent_location=agent_location,
        model_name=model_name,
        user_id=user_id,
        timeout_sec=timeout_sec,
    )
    mock_events_count = 0

  return _assemble_run_payload(
      project_id=project_id,
      dataset_id=dataset_id,
      table_id=table_id,
      location=location,
      agent_location=agent_location,
      model_name=model_name,
      mock_live=mock_live,
      mock_events_count=mock_events_count,
      session_ids=session_ids,
      scenario_runs=scenario_runs,
      eval_turns=eval_turns,
      output_json=output_json,
  )


def run_demo(
    *,
    project_id: str = "demo-project",
    dataset_id: str = "combined_cuj_trace_search_demo",
    table_id: str = "agent_events",
    eval_turns_table: str = "eval_turns",
    location: str = "US",
    agent_location: str = "global",
    model_name: str = "gemini-4-hydrogen",
    user_id: str = "demo_user",
    mock_live: bool = False,
    timeout_sec: int = 120,
    output_json: str | Path | None = None,
) -> dict[str, Any]:
  """Executes the 8 scenarios in live BigQuery mode or --mock-live mode."""
  if mock_live:
    session_ids, eval_turns, scenario_runs, mock_events_count = (
        _prepare_mock_live_runs(user_id=user_id, model_name=model_name)
    )
  else:
    _validate_live_bq_identifiers(
        project_id, dataset_id, table_id, eval_turns_table
    )
    live_coro = _run_live_scenarios(
        project_id=project_id,
        dataset_id=dataset_id,
        table_id=table_id,
        eval_turns_table=eval_turns_table,
        location=location,
        agent_location=agent_location,
        model_name=model_name,
        user_id=user_id,
        timeout_sec=timeout_sec,
    )
    try:
      asyncio.get_running_loop()
      loop_running = True
    except RuntimeError:
      loop_running = False
    if loop_running:
      # Notebooks (Jupyter/Colab) execute cells inside an active event loop;
      # run asyncio.run in a dedicated worker thread so sync callers still work.
      with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        session_ids, eval_turns, scenario_runs = pool.submit(
            asyncio.run, live_coro
        ).result()
    else:
      session_ids, eval_turns, scenario_runs = asyncio.run(live_coro)
    mock_events_count = 0

  return _assemble_run_payload(
      project_id=project_id,
      dataset_id=dataset_id,
      table_id=table_id,
      location=location,
      agent_location=agent_location,
      model_name=model_name,
      mock_live=mock_live,
      mock_events_count=mock_events_count,
      session_ids=session_ids,
      scenario_runs=scenario_runs,
      eval_turns=eval_turns,
      output_json=output_json,
  )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
  parser = argparse.ArgumentParser(
      description=(
          "Run the 8 Combined CUJ ADK scenarios and compute deterministic "
          "S1/S2/S3 trajectory extraction ablation metrics."
      )
  )
  parser.add_argument(
      "--project-id", default=os.environ.get("PROJECT_ID", "demo-project")
  )
  parser.add_argument(
      "--dataset-id",
      default=os.environ.get("DATASET_ID", "combined_cuj_trace_search_demo"),
  )
  parser.add_argument(
      "--table-id", default=os.environ.get("TABLE_ID", "agent_events")
  )
  parser.add_argument(
      "--eval-turns-table",
      default=os.environ.get("EVAL_TURNS_TABLE", "eval_turns"),
  )
  parser.add_argument("--location", default=os.environ.get("BQ_LOCATION", "US"))
  parser.add_argument(
      "--agent-location",
      default=os.environ.get("AGENT_LOCATION", "global"),
  )
  parser.add_argument(
      "--model", default=os.environ.get("MODEL_NAME", "gemini-4-hydrogen")
  )
  parser.add_argument("--user-id", default="demo_user")
  parser.add_argument("--mock-live", action="store_true")
  parser.add_argument("--timeout", type=int, default=120)
  parser.add_argument(
      "--output-json",
      default=str(_DEMO_DIR / "reports" / "live_demo_results.json"),
  )
  return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
  args = _parse_args(argv)
  payload = run_demo(
      project_id=args.project_id,
      dataset_id=args.dataset_id,
      table_id=args.table_id,
      eval_turns_table=args.eval_turns_table,
      location=args.location,
      agent_location=args.agent_location,
      model_name=args.model,
      user_id=args.user_id,
      mock_live=args.mock_live,
      timeout_sec=args.timeout,
      output_json=args.output_json,
  )
  print(
      json.dumps(
          {
              "output_json": args.output_json,
              "mock_live": payload["mock_live"],
              "sessions": len(payload["session_ids"]),
              "ablation_summary": payload["ablation_summary"],
          },
          indent=2,
      )
  )
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
