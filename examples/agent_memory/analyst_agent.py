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

"""Live run: a data-analyst agent whose memory lives in BigQuery.

An ADK agent answers analysts' questions about TheLook, the online clothing
retailer in ``bigquery-public-data.thelook_ecommerce``, by writing and
running real BigQuery SQL. ``analyst_scenario.py`` scripts a week of
sessions for six analysts. The ``BigQueryAgentAnalyticsPlugin`` logs every
event to ``<dataset>.analyst_events``, and that table is the agent's only
memory store:

* during a session the agent saves preferences as ADK ``user:`` state
  (logged as ``STATE_DELTA`` rows) and starts by calling ``recall_memory``,
  which reads this user's memory back from BigQuery;
* after each simulated day, ``memory_consolidation.consolidate`` extracts
  entities and facts from the day's messages with ``AI.GENERATE`` and
  embeds them with ``AI.EMBED``, in BigQuery.

For the sessions the scenario marks, the same first question also goes to
an agent with no memory tools, on the same day, as a control.

  pip install "google-adk[bigquery-analytics]"   # the plugin's writer deps
  python examples/agent_memory/analyst_agent.py --project-id my-project \\
      --dataset-id bqaa_agent_memory_demo

Each query the agent runs is a BigQuery job billed to the project, capped
at 2 GB scanned (``MAX_BYTES``) and labeled ``bqaa_demo=agent_memory``.
"""

from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import dataclasses
from datetime import datetime
from datetime import timedelta
from datetime import timezone
import datetime as dt
import decimal
import faulthandler
import json
import os
from pathlib import Path
import signal
import sys
import time
from typing import Any, Callable, Optional

from google.adk.tools import ToolContext
from google.api_core.exceptions import GoogleAPIError

from bigquery_agent_analytics import Client
from bigquery_agent_analytics import make_bq_client

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import analyst_scenario as scenario  # noqa: E402
import memory_consolidation  # noqa: E402
import memory_layers  # noqa: E402

THELOOK = "bigquery-public-data.thelook_ecommerce"
THELOOK_TABLES = (
    "order_items",
    "orders",
    "products",
    "users",
    "inventory_items",
    "distribution_centers",
    "events",
)
APP_NAME = "thelook_analyst"
ROOT_AGENT = "thelook_analyst"
CONTROL_AGENT = "thelook_analyst_no_memory"
CONTROL_USER_SUFFIX = "+no-memory"
DEFAULT_MODEL = "gemini-3.8-flash"
EVENTS_TABLE = "analyst_events"
TABLE_PREFIX = "analyst_"
DEFAULT_RECORD = HERE / "recorded_run" / "live_run.json"
MAX_BYTES = 2_000_000_000
MAX_ROWS = 50
JOB_LABELS = {"bqaa_demo": "agent_memory"}


# ------------------------------------------------------------------ #
# Tools                                                                #
# ------------------------------------------------------------------ #


def _jsonable(value: Any) -> Any:
  if isinstance(value, decimal.Decimal):
    return float(value)
  if isinstance(value, (dt.datetime, dt.date, dt.time)):
    return value.isoformat()
  if isinstance(value, bytes):
    return value.hex()
  if isinstance(value, dict):
    return {key: _jsonable(item) for key, item in value.items()}
  if isinstance(value, (list, tuple)):
    return [_jsonable(item) for item in value]
  return value


def _error_message(error: Exception) -> str:
  errors = getattr(error, "errors", None) or []
  message = errors[0].get("message") if errors else None
  return (message or str(error)).split("\n")[0][:400]


class Warehouse:
  """Read-only BigQuery access to TheLook's data, for the agent's tools."""

  def __init__(
      self,
      bq: Any,
      *,
      dataset: str = THELOOK,
      max_bytes: int = MAX_BYTES,
      max_rows: int = MAX_ROWS,
      labels: Optional[dict[str, str]] = None,
  ) -> None:
    self._bq = bq
    self._dataset = dataset
    self._max_bytes = max_bytes
    self._max_rows = max_rows
    self._labels = dict(JOB_LABELS if labels is None else labels)
    self._columns: dict[str, list[dict[str, str]]] = {}

  def _config(self, **kwargs: Any) -> Any:
    from google.cloud import bigquery

    return bigquery.QueryJobConfig(
        default_dataset=self._dataset, labels=self._labels, **kwargs
    )

  def run_sql(self, sql: str, purpose: str) -> dict:
    """Runs a read-only BigQuery Standard SQL query on TheLook's data.

    Unqualified table names resolve to bigquery-public-data.thelook_ecommerce,
    and only that dataset's tables can be read. Only SELECT statements run,
    and each may scan at most 2 GB. Returns the first 50 rows.

    Args:
      sql: the query, in BigQuery Standard SQL.
      purpose: one line saying what the query answers.
    """
    try:
      dry = self._bq.query(
          sql, job_config=self._config(dry_run=True, use_query_cache=False)
      )
      if dry.statement_type != "SELECT":
        return {
            "status": "error",
            "message": (
                "Only SELECT statements may run here, not"
                f" {dry.statement_type}."
            ),
        }
      # Least privilege: the agent's memory lives in BigQuery too, and must
      # stay out of reach of its SQL tool.
      outside = sorted(
          f"{t.project}.{t.dataset_id}.{t.table_id}"
          for t in dry.referenced_tables or []
          if f"{t.project}.{t.dataset_id}" != self._dataset
      )
      if outside:
        return {
            "status": "error",
            "message": (
                f"Only tables in {self._dataset} can be read here, not"
                f" {', '.join(outside)}."
            ),
        }
      scanned = dry.total_bytes_processed or 0
      if scanned > self._max_bytes:
        return {
            "status": "error",
            "message": (
                f"The query would scan {scanned / 1e9:.1f} GB; the limit is"
                f" {self._max_bytes / 1e9:.0f} GB."
            ),
        }
      job = self._bq.query(
          sql, job_config=self._config(maximum_bytes_billed=self._max_bytes)
      )
      result = job.result(timeout=180)
      rows = []
      for row in result:
        if len(rows) == self._max_rows:
          break
        rows.append({key: _jsonable(value) for key, value in row.items()})
      total = result.total_rows if result.total_rows is not None else len(rows)
      return {
          "status": "ok",
          "columns": [field.name for field in result.schema],
          "rows": rows,
          "total_rows": total,
          "truncated": total > len(rows),
          "bytes_processed": job.total_bytes_processed,
      }
    except (GoogleAPIError, concurrent.futures.TimeoutError) as error:
      # Bad SQL and slow queries go back to the model, which can fix them.
      return {"status": "error", "message": _error_message(error)}

  def describe_table(self, table_name: str) -> dict:
    """Lists the columns and types of one TheLook table.

    Args:
      table_name: order_items, orders, products, users, inventory_items,
        distribution_centers or events.
    """
    name = table_name.strip("` ").split(".")[-1]
    if name not in THELOOK_TABLES:
      return {
          "status": "error",
          "message": (
              f"Unknown table {table_name!r}. Tables: {', '.join(THELOOK_TABLES)}."
          ),
      }
    if name not in self._columns:
      from google.cloud import bigquery

      project, dataset = self._dataset.split(".")
      job = self._bq.query(
          f"SELECT column_name, data_type FROM `{project}.{dataset}`"
          ".INFORMATION_SCHEMA.COLUMNS WHERE table_name = @table"
          " ORDER BY ordinal_position",
          job_config=bigquery.QueryJobConfig(
              labels=self._labels,
              query_parameters=[
                  bigquery.ScalarQueryParameter("table", "STRING", name)
              ],
          ),
      )
      self._columns[name] = [
          {"name": row["column_name"], "type": row["data_type"]}
          for row in job.result()
      ]
    return {
        "status": "ok",
        "table": f"{self._dataset}.{name}",
        "columns": self._columns[name],
    }


def save_preference(key: str, value: str, tool_context: ToolContext) -> dict:
  """Remembers a lasting preference or scope of this user across sessions.

  Args:
    key: short snake_case name, e.g. "currency", "revenue_definition",
      "my_categories" or "price_bands".
    value: the preference, in a few words.
  """
  name = key.strip().lower().replace(" ", "_").replace("-", "_")
  if not name:
    return {"status": "error", "message": "key must not be empty"}
  # ADK's user: prefix shares the key across this user's sessions; the
  # plugin logs the change as a STATE_DELTA row.
  tool_context.state[memory_layers.USER_STATE_PREFIX + name] = value
  return {"status": "saved", "key": name, "value": value}


@dataclasses.dataclass
class MemoryStore:
  """Reads one user's memory back from BigQuery for ``recall_memory``."""

  client: Client  # SDK client over the events table
  bq: Any
  tables: memory_consolidation.MemoryTables
  lookback_days: int = 30
  similarity_threshold: float = 0.55
  max_items: int = 6
  now: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
  # Whether the embeddings table has its status column; read once.
  has_status: Optional[bool] = None

  def context(self, user_id: str, session_id: str, request: str) -> str:
    facts, entities = memory_consolidation.load_memory_items(
        self.bq, self.tables, user_id
    )
    if self.has_status is None:
      self.has_status = memory_consolidation.has_status_column(
          self.bq, self.tables.embeddings
      )
    scores = memory_consolidation.similar_task_scores(
        self.bq,
        self.tables,
        user_id,
        request,
        session_id=session_id,
        has_status=self.has_status,
    )
    memory = memory_layers.load_user_memory(
        self.client,
        user_id,
        since=self.now() - timedelta(days=self.lookback_days),
        facts=facts,
        extracted_entities=entities,
    )
    return memory.get_context(
        request,
        session_id=session_id,
        max_items=self.max_items,
        scores=scores,
        threshold=self.similarity_threshold,
        reuse_tools=("run_sql",),
    )


def make_recall_memory(store: MemoryStore) -> Callable[..., dict]:
  """Builds the ``recall_memory`` tool over one ``MemoryStore``."""

  def recall_memory(request: str, tool_context: ToolContext) -> dict:
    """Recalls what you know about this user from earlier sessions.

    Reads BigQuery and returns their saved preferences (latest version),
    facts and entities from past conversations, similar past analyses with
    the SQL that answered them, and tool calls that failed before.

    Args:
      request: the user's request, in their words.
    """
    return {
        "memory": store.context(
            tool_context.user_id, tool_context.session.id, request
        )
    }

  return recall_memory


# ------------------------------------------------------------------ #
# Agents                                                               #
# ------------------------------------------------------------------ #

_DATA = """\
You are TheLook's data-analyst assistant. TheLook is an online clothing
retailer. Its data is in the BigQuery dataset
bigquery-public-data.thelook_ecommerce: order_items (one row per item
sold, with status, sale_price and order, shipping, delivery and return
times), orders, products (category, brand, department, retail price),
users (age, country, traffic_source, signup time), inventory_items (each
item's product_distribution_center_id), distribution_centers and events
(web sessions).

Today is {sim_weekday}, {sim_date}. Treat it as the current date: yesterday,
last week and last month are relative to it. The data also holds rows dated
after today; leave them out."""

_MEMORY = """\
Memory:
- Start every conversation by calling recall_memory with the user's
  request, before anything else. It returns what you know about this user
  from earlier sessions: saved preferences, facts from past conversations,
  similar past analyses with the SQL that answered them, and tool calls that
  failed before.
- Apply remembered preferences, definitions and scope without asking again.
  When a past analysis matches the request, adapt its SQL instead of
  exploring the schema again.
- When the user states a lasting preference or scope (currency and exchange
  rate, how a metric is defined, comparison periods, time granularity,
  price bands, or the categories, brands, markets, distribution centers or
  customer segment they cover), call save_preference with a short snake_case
  key and the value before answering. Save the same key again to update
  it."""

_ANSWER = """\
Answering:
- Use describe_table when you need column names, and run_sql to query. If a
  query fails, read the error, fix the SQL and run it again.
- Reply concisely: the key numbers first (a small table is fine), then one
  line with the definitions, filters and dates you used."""

INSTRUCTION = "\n\n".join((_DATA, _MEMORY, _ANSWER))
CONTROL_INSTRUCTION = "\n\n".join((_DATA, _ANSWER))


def build_agent(
    model: str,
    warehouse: Warehouse,
    recall_memory: Optional[Callable[..., dict]] = None,
) -> Any:
  """The analyst agent; without ``recall_memory`` it has no memory tools."""
  from google.adk.agents import Agent
  from google.adk.models import Gemini
  from google.genai import types

  tools: list[Any] = [warehouse.describe_table, warehouse.run_sql]
  if recall_memory is not None:
    tools = [recall_memory, save_preference] + tools
  return Agent(
      name=ROOT_AGENT if recall_memory is not None else CONTROL_AGENT,
      model=Gemini(
          model=model, retry_options=types.HttpRetryOptions(attempts=3)
      ),
      description="Data analyst for TheLook that keeps its memory in BigQuery.",
      instruction=INSTRUCTION
      if recall_memory is not None
      else CONTROL_INSTRUCTION,
      tools=tools,
  )


def session_state(scripted: scenario.ScriptedSession, memory: bool) -> dict:
  """The session state for a scripted session (logged with every row)."""
  day = scenario.day(scripted.day)
  return {
      "sim_day": day.number,
      "sim_date": day.date,
      "sim_weekday": day.weekday,
      "analyst_name": scenario.analyst(scripted.user_id).name,
      "memory": "on" if memory else "off",
  }


# ------------------------------------------------------------------ #
# Live run                                                             #
# ------------------------------------------------------------------ #

_COUNTS_SQL = """
SELECT
  session_id,
  user_id,
  COUNT(*) AS row_count,
  COUNT(DISTINCT invocation_id) AS invocations,
  COUNTIF(event_type = 'LLM_RESPONSE') AS llm_responses,
  COUNTIF(event_type = 'TOOL_STARTING') AS tool_calls,
  COUNTIF(event_type = 'STATE_DELTA') AS state_deltas,
  COUNTIF(JSON_VALUE(attributes, '$.otel.trace_id') IS NOT NULL) AS otel_rows,
  MIN(timestamp) AS first_event,
  MAX(timestamp) AS last_event
FROM `{table}`
WHERE session_id IN UNNEST(@session_ids)
GROUP BY session_id, user_id
ORDER BY first_event
"""

_USAGE_SQL = """
SELECT
  JSON_VALUE(attributes, '$.root_agent_name') AS agent,
  COUNT(*) AS model_calls,
  SUM(SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.prompt_token_count') AS INT64)) AS input_tokens,
  SUM(SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.candidates_token_count') AS INT64)) AS output_tokens,
  SUM(SAFE_CAST(JSON_VALUE(attributes, '$.usage_metadata.thoughts_token_count') AS INT64)) AS thinking_tokens
FROM `{table}`
WHERE session_id IN UNNEST(@session_ids)
  AND event_type = 'LLM_RESPONSE'
GROUP BY agent
"""


def _query(bq: Any, sql: str, session_ids: list[str]) -> list[dict]:
  from google.cloud import bigquery

  config = bigquery.QueryJobConfig(
      labels=JOB_LABELS,
      query_parameters=[
          bigquery.ArrayQueryParameter("session_ids", "STRING", session_ids)
      ],
  )
  return [dict(row) for row in bq.query(sql, job_config=config).result()]


def _row_counts(
    bq: Any, table: str, session_ids: list[str], *, settle_s: float = 90.0
) -> list[dict]:
  """Per-session row counts, polled until two reads agree."""
  deadline = time.monotonic() + settle_s
  previous = None
  while True:
    rows = _query(bq, _COUNTS_SQL.format(table=table), session_ids)
    totals = [(r["session_id"], r["row_count"]) for r in rows]
    if totals == previous or time.monotonic() > deadline:
      break
    previous = totals
    time.sleep(10)
  for row in rows:
    for key in ("first_event", "last_event"):
      row[key] = row[key].astimezone(timezone.utc).isoformat()
  return rows


async def _run_turns(
    runner: Any,
    user_id: str,
    session_id: str,
    state: dict,
    turns: tuple[str, ...],
) -> list[dict]:
  from google.genai import types

  await runner.session_service.create_session(
      app_name=APP_NAME, user_id=user_id, session_id=session_id, state=state
  )
  out = []
  for text in turns:
    turn: dict[str, Any] = {
        "user": text,
        "tool_calls": [],
        "tool_errors": [],
        "reply": None,
        "error": None,
    }
    started = time.monotonic()
    message = types.Content(role="user", parts=[types.Part(text=text)])
    try:
      async for event in runner.run_async(
          user_id=user_id, session_id=session_id, new_message=message
      ):
        for part in (event.content.parts if event.content else None) or []:
          if part.function_call:
            turn["tool_calls"].append(part.function_call.name)
          elif part.function_response:
            response = part.function_response.response or {}
            if isinstance(response, dict) and response.get("status") == "error":
              turn["tool_errors"].append(
                  f"{part.function_response.name}: {response.get('message')}"
              )
          elif part.text and not part.thought:
            turn["reply"] = part.text.strip()
    except Exception as e:  # recorded, and the run goes on
      turn["error"] = f"{type(e).__name__}: {e}"
    turn["seconds"] = round(time.monotonic() - started, 1)
    print(f"  user : {text}")
    print(f"  tools: {', '.join(turn['tool_calls']) or '-'}")
    for problem in turn["tool_errors"]:
      print(f"  error: {problem[:200]}")
    reply = turn["reply"] or turn["error"] or ""
    print(f"  reply: {reply[:300]}{'...' if len(reply) > 300 else ''}")
    out.append(turn)
  return out


def _plugin(args: argparse.Namespace) -> Any:
  from google.adk.plugins.bigquery_agent_analytics_plugin import BigQueryAgentAnalyticsPlugin
  from google.adk.plugins.bigquery_agent_analytics_plugin import BigQueryLoggerConfig

  return BigQueryAgentAnalyticsPlugin(
      project_id=args.project_id,
      dataset_id=args.dataset_id,
      table_id=args.events_table,
      location=args.bq_location,
      config=BigQueryLoggerConfig(
          enable_otel_correlation=not args.no_otel,
          create_views=False,
          max_content_length=64 * 1024,
          shutdown_timeout=30.0,
      ),
  )


async def _run_week(
    args: argparse.Namespace, run_tag: str, bq: Any
) -> dict[str, Any]:
  from google.adk.runners import InMemoryRunner

  tables = memory_consolidation.MemoryTables.in_dataset(
      args.project_id,
      args.dataset_id,
      events=args.events_table,
      prefix=args.table_prefix,
  )
  events = tables.events
  memory_consolidation.ensure_tables(bq, tables)
  client = Client(
      project_id=args.project_id,
      dataset_id=args.dataset_id,
      table_id=args.events_table,
      location=args.bq_location,
      verify_schema=False,
      bq_client=make_bq_client(args.project_id, location=args.bq_location),
  )
  warehouse = Warehouse(bq)
  store = MemoryStore(client=client, bq=bq, tables=tables)
  from google.api_core import exceptions

  try:
    bq.get_table(events)
    new_table = False
  except exceptions.NotFound:
    new_table = True
  sessions, comparisons, nights = [], [], []
  numbered = scenario.numbered_sessions(run_tag)
  days = [d for d in scenario.DAYS if d.number <= args.days]
  # Entering a plugin creates the events table if it is missing; leaving it
  # stops its writer, also when the run fails.
  async with _plugin(args) as memory_plugin, _plugin(args) as control_plugin:
    if new_table:
      # A brand-new table can reject writes for a short while.
      print(
          f"created {events}; waiting {args.new_table_wait_s}s before writing"
      )
      await asyncio.sleep(args.new_table_wait_s)
    memory_runner = InMemoryRunner(
        agent=build_agent(args.model, warehouse, make_recall_memory(store)),
        app_name=APP_NAME,
        plugins=[memory_plugin],
    )
    control_runner = InMemoryRunner(
        agent=build_agent(args.model, warehouse),
        app_name=APP_NAME,
        plugins=[control_plugin],
    )
    try:
      await _run_days(
          args,
          days,
          numbered,
          (memory_runner, control_runner),
          (memory_plugin, control_plugin),
          bq,
          tables,
          (sessions, comparisons, nights),
      )
    finally:
      await memory_runner.close()
      await control_runner.close()
  return {
      "tables": dataclasses.asdict(tables),
      "sessions": sessions,
      "comparisons": comparisons,
      "consolidation": nights,
  }


async def _run_days(
    args: argparse.Namespace,
    days: list[scenario.Day],
    numbered: list[tuple[str, scenario.ScriptedSession]],
    runners: tuple[Any, Any],
    plugins: tuple[Any, Any],
    bq: Any,
    tables: memory_consolidation.MemoryTables,
    out: tuple[list, list, list],
) -> None:
  memory_runner, control_runner = runners
  memory_plugin, control_plugin = plugins
  sessions, comparisons, nights = out
  # The memory sessions run so far; each night consolidates all of them.
  so_far: list[str] = []
  for day in days:
    today = [
        (sid, s)
        for sid, s in numbered
        if s.day == day.number and (not args.users or s.user_id in args.users)
    ]
    print(f"=== Day {day.number}: {day.weekday} {day.date}")
    for session_id, scripted in today:
      print(f"SESSION {session_id} user={scripted.user_id}")
      turns = await _run_turns(
          memory_runner,
          scripted.user_id,
          session_id,
          session_state(scripted, memory=True),
          scripted.turns,
      )
      sessions.append(
          {
              "session_id": session_id,
              "user_id": scripted.user_id,
              "day": day.number,
              "sim_date": day.date,
              "memory": "on",
              "turns": turns,
          }
      )
      if scripted.compare:
        control_id = f"{session_id}-ctl"
        control_user = scripted.user_id + CONTROL_USER_SUFFIX
        print(f"CONTROL {control_id} user={control_user}")
        control_turns = await _run_turns(
            control_runner,
            control_user,
            control_id,
            session_state(scripted, memory=False),
            scripted.turns[:1],
        )
        sessions.append(
            {
                "session_id": control_id,
                "user_id": control_user,
                "day": day.number,
                "sim_date": day.date,
                "memory": "off",
                "turns": control_turns,
            }
        )
        comparisons.append(
            {
                "user_id": scripted.user_id,
                "day": day.number,
                "question": scripted.turns[0],
                "with_memory": session_id,
                "without_memory": control_id,
            }
        )
    await memory_plugin.flush()
    await control_plugin.flush()
    # Nightly consolidation of every memory session so far (not the
    # controls). Only messages without a successful row are sent to the
    # models: today's, and any that failed on an earlier night, which are
    # tried again. The night's totals cover the run so far.
    so_far += [sid for sid, _ in today]
    started = time.monotonic()
    night = memory_consolidation.consolidate(bq, tables, so_far)
    night.update(day=day.number, seconds=round(time.monotonic() - started, 1))
    print(f"NIGHT {day.number}: {json.dumps(night['extraction'], default=str)}")
    nights.append(night)


async def _rerun_controls(
    args: argparse.Namespace, record: dict[str, Any], bq: Any
) -> dict[str, dict[str, Any]]:
  """Asks each compared question again without memory, as in the run.

  Returns the new control session of each pair, by the session id of its
  with-memory side.
  """
  from google.adk.runners import InMemoryRunner

  numbered = dict(scenario.numbered_sessions(record["run_tag"]))
  out = {}
  async with _plugin(args) as control_plugin:
    runner = InMemoryRunner(
        agent=build_agent(args.model, Warehouse(bq)),
        app_name=APP_NAME,
        plugins=[control_plugin],
    )
    try:
      for pair in record["comparisons"]:
        scripted = numbered[pair["with_memory"]]
        control_id = f"{pair['with_memory']}-ctl2"
        user_id = scripted.user_id + CONTROL_USER_SUFFIX
        print(f"CONTROL {control_id} user={user_id}")
        turns = await _run_turns(
            runner,
            user_id,
            control_id,
            session_state(scripted, memory=False),
            scripted.turns[:1],
        )
        day = scenario.day(scripted.day)
        out[pair["with_memory"]] = {
            "session_id": control_id,
            "user_id": user_id,
            "day": day.number,
            "sim_date": day.date,
            "memory": "off",
            "turns": turns,
        }
    finally:
      await runner.close()
  return out


def replace_controls(
    record: dict[str, Any],
    controls: dict[str, dict[str, Any]],
    reason: str,
) -> dict[str, Any]:
  """The run record with its control sessions replaced by a new pass.

  ``controls`` maps each pair's with-memory session id to its new control
  session. The replaced control sessions stay listed under
  ``superseded_controls``, with ``reason``; their rows stay in the table.
  """
  out = dict(record)
  out["sessions"] = [s for s in record["sessions"] if s["memory"] == "on"]
  out["sessions"] += [controls[p["with_memory"]] for p in record["comparisons"]]
  out["comparisons"] = [
      dict(pair, without_memory=controls[pair["with_memory"]]["session_id"])
      for pair in record["comparisons"]
  ]
  out["superseded_controls"] = {
      "reason": reason,
      "sessions": [s for s in record["sessions"] if s["memory"] == "off"],
  }
  return out


def _enable_otel_spans() -> None:
  """Gives ADK a real tracer so the plugin has span contexts to record."""
  from opentelemetry import trace
  from opentelemetry.sdk.trace import TracerProvider

  trace.set_tracer_provider(TracerProvider())


def main(argv: Optional[list[str]] = None) -> int:
  parser = argparse.ArgumentParser(
      description=__doc__,
      formatter_class=argparse.RawDescriptionHelpFormatter,
  )
  parser.add_argument("--project-id", required=True)
  parser.add_argument("--dataset-id", default="bqaa_agent_memory_demo")
  parser.add_argument("--events-table", default=EVENTS_TABLE)
  parser.add_argument(
      "--table-prefix",
      default=TABLE_PREFIX,
      help="prefix of the consolidation tables (default: analyst_)",
  )
  parser.add_argument("--bq-location", default="US")
  parser.add_argument("--model", default=DEFAULT_MODEL)
  parser.add_argument(
      "--vertex-location",
      default="global",
      help="Vertex AI location for the model (default: global)",
  )
  parser.add_argument(
      "--days",
      type=int,
      default=len(scenario.DAYS),
      help="run only the first N simulated days",
  )
  parser.add_argument(
      "--user",
      dest="users",
      action="append",
      help="run only this analyst's sessions (repeatable)",
  )
  parser.add_argument(
      "--run-tag",
      help="part of the session ids (default: UTC time of the run)",
  )
  parser.add_argument(
      "--record",
      type=Path,
      default=DEFAULT_RECORD,
      help="where to write the run record",
  )
  parser.add_argument(
      "--no-otel",
      action="store_true",
      help="do not record OpenTelemetry span ids in attributes.otel",
  )
  parser.add_argument(
      "--rerun-controls",
      metavar="REASON",
      help=(
          "ask each compared question again without memory and replace the"
          " control sessions in the run record (--record), keeping the old"
          " ones listed with this reason"
      ),
  )
  parser.add_argument(
      "--new-table-wait-s",
      type=float,
      default=60.0,
      help="pause after creating a new events table (default: 60)",
  )
  args = parser.parse_args(argv)
  # `kill -USR1 <pid>` prints every thread's stack if a long run stalls.
  faulthandler.register(signal.SIGUSR1, all_threads=True)

  os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "True"
  os.environ["GOOGLE_CLOUD_PROJECT"] = args.project_id
  os.environ["GOOGLE_CLOUD_LOCATION"] = args.vertex_location
  if not args.no_otel:
    _enable_otel_spans()

  from google.cloud import bigquery

  bq = bigquery.Client(project=args.project_id, location=args.bq_location)
  dataset = bigquery.Dataset(f"{args.project_id}.{args.dataset_id}")
  dataset.location = args.bq_location
  bq.create_dataset(dataset, exists_ok=True)

  table = f"{args.project_id}.{args.dataset_id}.{args.events_table}"
  started = datetime.now(timezone.utc)
  if args.rerun_controls:
    record = json.loads(args.record.read_text("utf-8"))
    controls = asyncio.run(_rerun_controls(args, record, bq))
    record = replace_controls(record, controls, args.rerun_controls)
    record["controls_rerun"] = {
        "started_at": started.isoformat(),
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }
  else:
    run_tag = args.run_tag or started.strftime("%Y%m%dt%H%M")
    week = asyncio.run(_run_week(args, run_tag, bq))
    record = {
        "label": "Recorded live run: TheLook analyst week",
        "scenario": "analyst_scenario.py",
        "model": args.model,
        "vertex_location": args.vertex_location,
        "otel_correlation": not args.no_otel,
        "run_tag": run_tag,
        "started_at": started.isoformat(),
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "days": [dataclasses.asdict(d) for d in scenario.DAYS],
        "analysts": [dataclasses.asdict(a) for a in scenario.ANALYSTS],
        **week,
    }
  session_ids = [s["session_id"] for s in record["sessions"]]
  record["row_counts"] = _row_counts(bq, table, session_ids)
  record["usage"] = _query(bq, _USAGE_SQL.format(table=table), session_ids)
  args.record.parent.mkdir(parents=True, exist_ok=True)
  args.record.write_text(
      json.dumps(record, indent=1, ensure_ascii=False, default=str) + "\n",
      encoding="utf-8",
  )
  rows = sum(r["row_count"] for r in record["row_counts"])
  print(f"sessions: {len(session_ids)}, rows: {rows}")
  print(f"run record: {args.record}")
  return 0


if __name__ == "__main__":
  sys.exit(main())
