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

"""Live ADK agent that writes the memory this demo reads.

A small trip-planner agent runs five scripted, multi-turn sessions on
Vertex AI with the ``BigQueryAgentAnalyticsPlugin`` streaming every event to
``<project>.<dataset>.agent_events``. The tools are demo inventories, and the
hotel search times out on its first call (a simulated outage) so the table
records a real ``TOOL_ERROR``. Later sessions call ``recall_memory``, which
reads the earlier sessions back from BigQuery with ``load_user_memory()`` and
``get_context()``: the agent uses the events table as its long-term memory.

  pip install "google-adk[bigquery-analytics]"   # the plugin's writer deps
  python examples/agent_memory/live_agent.py --project-id my-project \\
      --dataset-id bqaa_agent_memory_demo

Then read the memory back with ``agent_memory_demo.py --project-id ...`` and
export it for the web view with ``export_memory.py``.
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
from datetime import datetime
from datetime import timedelta
from datetime import timezone
import json
import os
from pathlib import Path
import sys
import time
from typing import Any, Callable, Optional

from google.adk.tools import ToolContext

from bigquery_agent_analytics import Client
from bigquery_agent_analytics import make_bq_client

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from agent_memory_demo import ENTITY_ARGUMENTS  # noqa: E402
import memory_layers  # noqa: E402

APP_NAME = "trip_planner"
ROOT_AGENT = "trip_planner"
DEFAULT_MODEL = "gemini-3.8-flash"
DEFAULT_RECORD = HERE / "recorded_run" / "live_run.json"


# ------------------------------------------------------------------ #
# Tools (demo inventories; no external APIs)                           #
# ------------------------------------------------------------------ #


def save_preference(key: str, value: str, tool_context: ToolContext) -> dict:
  """Remembers a lasting user preference across sessions.

  Args:
    key: short lowercase name of the preference, e.g. "diet" or "seat".
    value: the preference, e.g. "vegetarian" or "window".
  """
  name = key.strip().lower().replace(" ", "_")
  if not name:
    return {"status": "rejected", "reason": "key must not be empty"}
  # ADK's user: prefix shares the key across this user's sessions; the
  # plugin logs the change as a STATE_DELTA row.
  tool_context.state[memory_layers.USER_STATE_PREFIX + name] = value
  return {"status": "saved", "key": name, "value": value}


def search_flights(
    origin: str, destination: str, date: str, seat: str = "any"
) -> dict:
  """Searches the demo flight inventory.

  Args:
    origin: departure airport or city.
    destination: arrival airport or city.
    date: departure date, YYYY-MM-DD.
    seat: seat preference ("window", "aisle" or "any").
  """
  return {
      "origin": origin,
      "destination": destination,
      "date": date,
      "seat": seat,
      "flights": [
          {"flight": "DM101", "departs": "11:05", "window_seats_left": 12},
          {"flight": "DM205", "departs": "16:40", "window_seats_left": 3},
      ],
      "note": "demo inventory",
  }


class HotelInventory:
  """Demo hotel inventory whose first ``failures`` calls time out."""

  def __init__(self, failures: int = 1) -> None:
    self._failures_left = failures

  def search_hotels(
      self, city: str, near: str, check_in: str, check_out: str
  ) -> dict:
    """Searches the demo hotel inventory near a landmark.

    Args:
      city: city to search.
      near: landmark the hotel should be close to.
      check_in: check-in date, YYYY-MM-DD.
      check_out: check-out date, YYYY-MM-DD.
    """
    if self._failures_left > 0:
      self._failures_left -= 1
      raise TimeoutError("hotel inventory API timed out (simulated outage)")
    return {
        "city": city,
        "near": near,
        "check_in": check_in,
        "check_out": check_out,
        "hotels": [
            {
                "name": "Sakura Station Hotel",
                "distance_m": 150,
                "price_per_night_usd": 180,
            },
            {
                "name": "Higashiyama Ryokan",
                "distance_m": 2400,
                "price_per_night_usd": 260,
            },
        ],
        "note": "demo inventory",
    }


_DIETS = ("vegan", "vegetarian", "pescatarian", "any")
_RESTAURANTS = {
    "kyoto": (
        ("Kamo Garden", {"vegan", "vegetarian", "pescatarian", "any"}),
        ("Pontocho Grill", {"pescatarian", "any"}),
        ("Gion Yakitori", {"any"}),
    ),
    "osaka": (
        ("Dotonbori Greens", {"vegan", "vegetarian", "pescatarian", "any"}),
        ("Umeda Sushi Bar", {"pescatarian", "any"}),
        ("Namba Kushikatsu", {"any"}),
    ),
}


def find_restaurants(city: str, diet: str, date: str) -> dict:
  """Finds dinner places in the demo listings that fit a diet.

  Args:
    city: city to search.
    diet: "vegan", "vegetarian", "pescatarian" or "any".
    date: date of the dinner, YYYY-MM-DD.
  """
  wanted = diet.strip().lower()
  if wanted not in _DIETS:
    wanted = "any"
  listings = _RESTAURANTS.get(city.strip().lower(), ())
  return {
      "city": city,
      "diet": wanted,
      "date": date,
      "restaurants": [
          {"name": name, "fits": wanted}
          for name, serves in listings
          if wanted in serves
      ],
      "note": "demo listings",
  }


def make_recall_memory(
    client: Client,
    *,
    lookback_days: int = 30,
    now: Optional[Callable[[], datetime]] = None,
    entity_args: Optional[dict[str, str]] = None,
) -> Callable[..., dict]:
  """Builds the ``recall_memory`` tool over one SDK ``Client``."""
  clock = now or (lambda: datetime.now(timezone.utc))
  arguments = dict(ENTITY_ARGUMENTS if entity_args is None else entity_args)

  def recall_memory(query: str, tool_context: ToolContext) -> dict:
    """Recalls what you know about this user from earlier sessions.

    Returns their saved preferences (latest version), places they asked
    about, similar past tasks and tools that failed before, read from the
    BigQuery agent events table.

    Args:
      query: short description of the current task.
    """
    memory = memory_layers.load_user_memory(
        client,
        tool_context.user_id,
        since=clock() - timedelta(days=lookback_days),
        entity_args=arguments,
    )
    return {
        "memory": memory.get_context(query, session_id=tool_context.session.id)
    }

  return recall_memory


# ------------------------------------------------------------------ #
# Scripted sessions                                                    #
# ------------------------------------------------------------------ #


@dataclasses.dataclass(frozen=True)
class ScriptedSession:
  key: str
  user_id: str
  turns: tuple[str, ...]

  def session_id(self, run_tag: str) -> str:
    return f"mem-{run_tag}-{self.key}"


SESSION_SCRIPT = (
    ScriptedSession(
        "s1",
        "demo-ana",
        (
            "Hi! Please remember two things about me: I'm vegetarian, and I"
            " always want a window seat.",
            "Now find me a flight from SFO to Tokyo on 2026-10-12.",
        ),
    ),
    ScriptedSession(
        "s2",
        "demo-ana",
        (
            "Find me a hotel in Kyoto near Kyoto Station from 2026-10-14 to"
            " 2026-10-16.",
            "The hotel search failed. Please try it again.",
            "What was the nightly price of the first hotel you found?",
        ),
    ),
    ScriptedSession(
        "s3",
        "demo-ana",
        (
            "Find me a dinner place in Kyoto on 2026-10-15 that fits my diet.",
            "Actually, I eat fish now. Please update my diet to pescatarian"
            " and check again.",
        ),
    ),
    ScriptedSession(
        "s4",
        "demo-ben",
        (
            "I'm vegan, please remember that. Find me a dinner place in"
            " Osaka on 2026-10-17.",
        ),
    ),
    ScriptedSession(
        "s5",
        "demo-ana",
        ("Plan dinner in Osaka on 2026-10-17. Do you remember what I" " eat?",),
    ),
)

INSTRUCTION = (
    "You are trip_planner, a travel assistant. The tools return demo"
    " inventories. When the user states a lasting preference such as a diet"
    " or seat, call save_preference once per preference. When a request"
    " depends on something the user may have told you in an earlier session"
    " (their diet, seat or past trips), first call recall_memory with a short"
    " description of the task and use what it returns. Pass preferences to"
    " the search tools explicitly. Answer in at most three sentences."
)


def build_agent(
    model: str, hotels: HotelInventory, recall_memory: Callable[..., dict]
) -> Any:
  from google.adk.agents import Agent
  from google.adk.models import Gemini
  from google.genai import types

  return Agent(
      name=ROOT_AGENT,
      model=Gemini(
          model=model, retry_options=types.HttpRetryOptions(attempts=3)
      ),
      description="Trip planner that keeps its memory in BigQuery.",
      instruction=INSTRUCTION,
      tools=[
          save_preference,
          recall_memory,
          search_flights,
          hotels.search_hotels,
          find_restaurants,
      ],
  )


# ------------------------------------------------------------------ #
# Live run                                                             #
# ------------------------------------------------------------------ #

_COUNTS_SQL = """
SELECT
  session_id,
  user_id,
  COUNT(*) AS row_count,
  COUNT(DISTINCT invocation_id) AS invocations,
  COUNTIF(event_type = 'TOOL_ERROR') AS tool_errors,
  COUNTIF(event_type = 'STATE_DELTA') AS state_deltas,
  COUNTIF(JSON_VALUE(attributes, '$.otel.trace_id') IS NOT NULL) AS otel_rows,
  MIN(timestamp) AS first_event,
  MAX(timestamp) AS last_event
FROM `{table}`
WHERE session_id IN UNNEST(@session_ids)
GROUP BY session_id, user_id
ORDER BY first_event
"""


def _row_counts(
    bq: Any, table: str, session_ids: list[str], *, settle_s: float = 90.0
) -> list[dict]:
  """Per-session row counts, polled until two reads agree."""
  from google.cloud import bigquery

  config = bigquery.QueryJobConfig(
      query_parameters=[
          bigquery.ArrayQueryParameter("session_ids", "STRING", session_ids)
      ]
  )
  deadline = time.monotonic() + settle_s
  previous = None
  while True:
    rows = [
        dict(row)
        for row in bq.query(
            _COUNTS_SQL.format(table=table), job_config=config
        ).result()
    ]
    totals = [(r["session_id"], r["row_count"]) for r in rows]
    if totals == previous or time.monotonic() > deadline:
      break
    previous = totals
    time.sleep(10)
  for row in rows:
    for key in ("first_event", "last_event"):
      row[key] = row[key].astimezone(timezone.utc).isoformat()
  return rows


async def _run_sessions(args: argparse.Namespace, run_tag: str) -> list[dict]:
  from google.adk.plugins.bigquery_agent_analytics_plugin import BigQueryAgentAnalyticsPlugin
  from google.adk.plugins.bigquery_agent_analytics_plugin import BigQueryLoggerConfig
  from google.adk.runners import InMemoryRunner
  from google.genai import types

  plugin = BigQueryAgentAnalyticsPlugin(
      project_id=args.project_id,
      dataset_id=args.dataset_id,
      table_id=args.table_id,
      location=args.bq_location,
      config=BigQueryLoggerConfig(
          enable_otel_correlation=not args.no_otel,
          create_views=False,
          max_content_length=64 * 1024,
          shutdown_timeout=20.0,
      ),
  )
  client = Client(
      project_id=args.project_id,
      dataset_id=args.dataset_id,
      table_id=args.table_id,
      location=args.bq_location,
      verify_schema=False,
      bq_client=make_bq_client(args.project_id, location=args.bq_location),
  )
  agent = build_agent(
      args.model, HotelInventory(failures=1), make_recall_memory(client)
  )
  runner = InMemoryRunner(agent=agent, app_name=APP_NAME, plugins=[plugin])
  transcript = []
  for scripted in SESSION_SCRIPT:
    session_id = scripted.session_id(run_tag)
    await runner.session_service.create_session(
        app_name=APP_NAME, user_id=scripted.user_id, session_id=session_id
    )
    print(f"SESSION {session_id} user={scripted.user_id}")
    for text in scripted.turns:
      turn: dict[str, Any] = {
          "session_id": session_id,
          "user_id": scripted.user_id,
          "user": text,
          "tool_calls": [],
          "reply": None,
          "error": None,
      }
      message = types.Content(role="user", parts=[types.Part(text=text)])
      try:
        async for event in runner.run_async(
            user_id=scripted.user_id,
            session_id=session_id,
            new_message=message,
        ):
          for part in (event.content.parts if event.content else None) or []:
            if part.function_call:
              turn["tool_calls"].append(part.function_call.name)
            elif part.text and not part.thought:
              turn["reply"] = part.text.strip()
      except Exception as e:  # e.g. the simulated hotel outage
        turn["error"] = f"{type(e).__name__}: {e}"
      print(f"  user : {text}")
      print(f"  tools: {', '.join(turn['tool_calls']) or '-'}")
      print(f"  reply: {turn['reply'] or turn['error']}")
      transcript.append(turn)
    # Land this session's rows before the next session recalls them.
    await plugin.flush()
  await runner.close()
  await plugin.shutdown()
  return transcript


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
  parser.add_argument("--table-id", default="agent_events")
  parser.add_argument("--bq-location", default="US")
  parser.add_argument("--model", default=DEFAULT_MODEL)
  parser.add_argument(
      "--vertex-location",
      default="global",
      help="Vertex AI location for the model (default: global)",
  )
  parser.add_argument(
      "--run-tag",
      help="suffix for the session ids (default: UTC time of the run)",
  )
  parser.add_argument(
      "--record",
      type=Path,
      default=DEFAULT_RECORD,
      help="where to write the run record (session ids, row counts)",
  )
  parser.add_argument(
      "--no-otel",
      action="store_true",
      help="do not record OpenTelemetry span ids in attributes.otel",
  )
  args = parser.parse_args(argv)

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

  started = datetime.now(timezone.utc)
  run_tag = args.run_tag or started.strftime("%Y%m%dt%H%M")
  transcript = asyncio.run(_run_sessions(args, run_tag))
  table = f"{args.project_id}.{args.dataset_id}.{args.table_id}"
  session_ids = [s.session_id(run_tag) for s in SESSION_SCRIPT]
  counts = _row_counts(bq, table, session_ids)
  record = {
      "label": "recorded live run (demo inventories, simulated hotel outage)",
      "table": table,
      "model": args.model,
      "vertex_location": args.vertex_location,
      "otel_correlation": not args.no_otel,
      "run_tag": run_tag,
      "started_at": started.isoformat(),
      "finished_at": datetime.now(timezone.utc).isoformat(),
      "sessions": counts,
      "turns": transcript,
  }
  args.record.parent.mkdir(parents=True, exist_ok=True)
  args.record.write_text(
      json.dumps(record, indent=1, ensure_ascii=False) + "\n", encoding="utf-8"
  )
  print(
      f"rows per session: {[(r['session_id'], r['row_count']) for r in counts]}"
  )
  print(f"run record: {args.record}")
  return 0


if __name__ == "__main__":
  sys.exit(main())
