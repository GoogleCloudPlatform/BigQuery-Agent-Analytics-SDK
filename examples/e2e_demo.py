#!/usr/bin/env python
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

"""End-to-End Demo: Agent -> BigQuery Traces -> SDK Evaluation & Insights.

Demonstrates the full lifecycle:
  Phase 1 - Run a real ADK agent with tools, logging traces to BigQuery.
  Phase 2 - Retrieve traces and run evaluations (code, LLM-as-judge,
            trajectory matching) via the BigQuery Agent Analytics SDK.
  Phase 3 - Generate an Insights report from the logged traces.

Usage:
    export GOOGLE_CLOUD_PROJECT="test-project-0728-467323"
    python BigQuery-Agent-Analytics-SDK/examples/e2e_demo.py
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from datetime import timezone
import hashlib
import json
import logging
import os
import random
import sys
import time
from typing import Any
import uuid

# ---------------------------------------------------------------------------
# ADK imports
# ---------------------------------------------------------------------------
from google.adk.agents import LlmAgent
# ---------------------------------------------------------------------------
# BigQuery Analytics Plugin (producer side)
# ---------------------------------------------------------------------------
from google.adk.plugins.bigquery_agent_analytics_plugin import BigQueryAgentAnalyticsPlugin
from google.adk.plugins.bigquery_agent_analytics_plugin import BigQueryLoggerConfig
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

# ---------------------------------------------------------------------------
# BigQuery Agent Analytics SDK (consumer side)
# ---------------------------------------------------------------------------
from bigquery_agent_analytics import BigQueryTraceEvaluator
from bigquery_agent_analytics import Client
from bigquery_agent_analytics import InsightsConfig
from bigquery_agent_analytics import LLMAsJudge
from bigquery_agent_analytics import SystemEvaluator
from bigquery_agent_analytics import TraceFilter
from bigquery_agent_analytics.trace_evaluator import MatchType

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT", "test-project-0728-467323")
DATASET_ID = os.environ.get("BQ_DATASET", "agent_analytics")
TABLE_ID = os.environ.get("BQ_TABLE", "agent_events")
MODEL_NAME = os.environ.get("MODEL_NAME", "gemini-3-flash-preview")
GCP_LOCATION = os.environ.get("GOOGLE_CLOUD_LOCATION", "global")
LOCATION = "US"
APP_NAME = "e2e_demo"
USER_ID = "demo_user"

# Ensure Vertex AI backend is configured for the ADK model client.
os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "true")
os.environ.setdefault("GOOGLE_CLOUD_PROJECT", PROJECT_ID)
os.environ.setdefault("GOOGLE_CLOUD_LOCATION", GCP_LOCATION)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s  %(message)s",
)
logger = logging.getLogger(__name__)


# ===================================================================== #
#  PHASE 1 — Agent Definition & Trace Generation                        #
# ===================================================================== #

# ---- Tool definitions ------------------------------------------------ #


async def search_flights(
    origin: str,
    destination: str,
    date: str,
    max_results: int = 5,
) -> dict[str, Any]:
  """Search for available flights between two cities.

  Args:
      origin: Departure city or airport code.
      destination: Arrival city or airport code.
      date: Travel date in YYYY-MM-DD format.
      max_results: Maximum number of results to return.

  Returns:
      Dictionary with flight search results.
  """
  seed = int(
      hashlib.md5(f"{origin}{destination}{date}".encode()).hexdigest()[:8],
      16,
  )
  rng = random.Random(seed)
  airlines = [
      "United Airlines",
      "Delta Air Lines",
      "American Airlines",
      "JetBlue Airways",
      "Southwest Airlines",
      "Alaska Airlines",
  ]
  flights = []
  for i in range(min(max_results, 5)):
    dep_hour = rng.randint(6, 20)
    duration_h = rng.randint(2, 14)
    flights.append(
        {
            "flight_id": f"FL-{seed + i:06d}",
            "airline": rng.choice(airlines),
            "origin": origin,
            "destination": destination,
            "date": date,
            "departure_time": f"{dep_hour:02d}:{rng.choice(['00','15','30','45'])}",
            "arrival_time": (
                f"{(dep_hour + duration_h) % 24:02d}:{rng.choice(['00','15','30','45'])}"
            ),
            "duration_hours": duration_h,
            "price_usd": round(rng.uniform(150, 1200), 2),
            "class": rng.choice(["Economy", "Premium Economy", "Business"]),
            "stops": rng.choice([0, 0, 0, 1, 1, 2]),
        }
    )
  return {
      "query": {
          "origin": origin,
          "destination": destination,
          "date": date,
      },
      "results_count": len(flights),
      "flights": flights,
  }


async def search_hotels(
    city: str,
    check_in: str,
    check_out: str,
    max_results: int = 5,
) -> dict[str, Any]:
  """Search for hotels in a given city.

  Args:
      city: City name to search hotels in.
      check_in: Check-in date (YYYY-MM-DD).
      check_out: Check-out date (YYYY-MM-DD).
      max_results: Maximum number of results to return.

  Returns:
      Dictionary with hotel search results.
  """
  seed = int(hashlib.md5(f"{city}{check_in}".encode()).hexdigest()[:8], 16)
  rng = random.Random(seed)
  hotel_names = [
      f"Grand {city} Hotel",
      f"{city} Plaza",
      f"The {city} Marriott",
      f"Hilton {city} Downtown",
      f"Hyatt Regency {city}",
      f"Four Seasons {city}",
      f"Holiday Inn {city}",
  ]
  hotels = []
  for i in range(min(max_results, 5)):
    rating = round(rng.uniform(3.5, 5.0), 1)
    hotels.append(
        {
            "hotel_id": f"HT-{seed + i:06d}",
            "name": hotel_names[i % len(hotel_names)],
            "city": city,
            "check_in": check_in,
            "check_out": check_out,
            "rating": rating,
            "price_per_night_usd": round(rng.uniform(80, 500), 2),
            "amenities": rng.sample(
                [
                    "WiFi",
                    "Pool",
                    "Gym",
                    "Spa",
                    "Restaurant",
                    "Bar",
                    "Room Service",
                    "Parking",
                    "Airport Shuttle",
                    "Business Center",
                ],
                k=rng.randint(3, 7),
            ),
            "distance_to_center_km": round(rng.uniform(0.2, 8.0), 1),
        }
    )
  return {
      "query": {"city": city, "check_in": check_in, "check_out": check_out},
      "results_count": len(hotels),
      "hotels": hotels,
  }


async def get_weather_forecast(
    city: str,
    date: str,
) -> dict[str, Any]:
  """Get weather forecast for a city on a specific date.

  Args:
      city: City name.
      date: Date in YYYY-MM-DD format.

  Returns:
      Dictionary with weather forecast data.
  """
  seed = int(hashlib.md5(f"{city}{date}".encode()).hexdigest()[:8], 16)
  rng = random.Random(seed)
  conditions = [
      "Sunny",
      "Partly Cloudy",
      "Cloudy",
      "Light Rain",
      "Rain",
      "Thunderstorms",
      "Clear",
      "Overcast",
  ]
  return {
      "city": city,
      "date": date,
      "temperature_high_c": rng.randint(15, 35),
      "temperature_low_c": rng.randint(5, 20),
      "condition": rng.choice(conditions),
      "humidity_pct": rng.randint(30, 90),
      "wind_speed_kmh": rng.randint(5, 40),
      "precipitation_chance_pct": rng.randint(0, 80),
      "uv_index": rng.randint(1, 11),
  }


async def calculate_trip_budget(
    flights: float,
    hotels: float,
    daily_expenses: float,
    num_days: int,
) -> dict[str, Any]:
  """Calculate total trip budget from component costs.

  Args:
      flights: Total flight cost in USD.
      hotels: Total hotel cost in USD.
      daily_expenses: Estimated daily expenses (food, transport, etc.).
      num_days: Number of trip days.

  Returns:
      Dictionary with itemised budget breakdown.
  """
  total_daily = daily_expenses * num_days
  subtotal = flights + hotels + total_daily
  tax_and_fees = round(subtotal * 0.12, 2)
  total = round(subtotal + tax_and_fees, 2)
  return {
      "breakdown": {
          "flights": round(flights, 2),
          "hotels": round(hotels, 2),
          "daily_expenses_total": round(total_daily, 2),
          "daily_expenses_per_day": round(daily_expenses, 2),
          "num_days": num_days,
          "tax_and_fees": tax_and_fees,
      },
      "subtotal_usd": round(subtotal, 2),
      "total_usd": total,
      "currency": "USD",
  }


# ---- Agent ---------------------------------------------------------- #

TRAVEL_PLANNER_INSTRUCTION = """\
You are a helpful travel planning assistant. You help users plan trips by
searching for flights, hotels, checking weather forecasts, and calculating
budgets.

Guidelines:
- Always search for flights and hotels when the user asks to plan a trip.
- Check the weather at the destination when relevant.
- Provide a budget estimate when enough cost information is available.
- Be concise but informative in your responses.
- Present results in a clear, organized format.
- When multiple tools are needed, call them as appropriate and then
  synthesize the results into a cohesive plan.
"""


def build_agent() -> LlmAgent:
  """Build the travel planner agent."""
  return LlmAgent(
      name="travel_planner",
      model=MODEL_NAME,
      instruction=TRAVEL_PLANNER_INSTRUCTION,
      tools=[
          search_flights,
          search_hotels,
          get_weather_forecast,
          calculate_trip_budget,
      ],
      generate_content_config=types.GenerateContentConfig(
          temperature=1.0,
      ),
  )


# ---- Runner with BigQuery plugin ------------------------------------ #


def build_runner(
    agent: LlmAgent,
) -> tuple[Runner, InMemorySessionService, BigQueryAgentAnalyticsPlugin]:
  """Build a Runner wired to the BigQuery analytics plugin."""
  session_service = InMemorySessionService()

  plugin = BigQueryAgentAnalyticsPlugin(
      project_id=PROJECT_ID,
      dataset_id=DATASET_ID,
      config=BigQueryLoggerConfig(
          table_id=TABLE_ID,
          batch_size=1,
          batch_flush_interval=1.0,
      ),
      location=LOCATION,
  )

  runner = Runner(
      agent=agent,
      app_name=APP_NAME,
      session_service=session_service,
      plugins=[plugin],
  )

  return runner, session_service, plugin


# ---- Conversation helper -------------------------------------------- #


async def run_conversation(
    runner: Runner,
    session_service: InMemorySessionService,
    messages: list[str],
    label: str = "",
) -> str:
  """Run a multi-turn conversation and return the session_id."""
  session_id = f"e2e-{uuid.uuid4().hex[:12]}"

  await session_service.create_session(
      app_name=APP_NAME,
      user_id=USER_ID,
      session_id=session_id,
  )

  print(f"\n{'=' * 64}")
  print(f"  Session: {session_id}  {label}")
  print(f"{'=' * 64}")

  for i, message in enumerate(messages, 1):
    print(f"\n[Turn {i}] User: {message}")
    print("-" * 48)

    user_content = types.Content(
        role="user",
        parts=[types.Part(text=message)],
    )

    response_parts: list[str] = []
    async for event in runner.run_async(
        user_id=USER_ID,
        session_id=session_id,
        new_message=user_content,
    ):
      if event.content and event.content.parts:
        for part in event.content.parts:
          if hasattr(part, "text") and part.text:
            response_parts.append(part.text)
          elif hasattr(part, "function_call") and part.function_call:
            print(f"  -> Tool call: {part.function_call.name}")

    if response_parts:
      text = "\n".join(response_parts)
      preview = text[:1500]
      print(f"\n[Agent]: {preview}")
      if len(text) > 1500:
        print(f"  ... (truncated, {len(text)} chars total)")

  return session_id


# ===================================================================== #
#  PHASE 2 — SDK Evaluation                                              #
# ===================================================================== #


async def phase2_evaluate(
    session_ids: list[str],
) -> None:
  """Retrieve traces, run code/LLM evaluators and trajectory matching."""
  print("\n")
  print("#" * 64)
  print("#  PHASE 2: Evaluate with BigQuery Agent Analytics SDK")
  print("#" * 64)

  client = Client(
      project_id=PROJECT_ID,
      dataset_id=DATASET_ID,
      table_id=TABLE_ID,
      location=LOCATION,
      endpoint=MODEL_NAME,
  )

  # ---- 2a. Trace retrieval & visualisation -------------------------- #
  print("\n--- 2a. Trace Retrieval & Visualisation ---\n")
  for sid in session_ids:
    try:
      trace = client.get_trace(sid)
      trace.render()
      tool_calls = trace.tool_calls
      final = trace.final_response or ""
      print(f"  Tool calls: {len(tool_calls)}")
      for tc in tool_calls:
        print(f"    - {tc.get('tool_name', '?')}")
      print(f"  Final response preview: {final[:200]}")
    except Exception as exc:
      logger.warning("Could not retrieve trace %s: %s", sid, exc)
    print()

  # ---- 2b. Code-based evaluation ------------------------------------ #
  print("\n--- 2b. Code-Based Evaluation ---\n")
  trace_filter = TraceFilter(session_ids=session_ids)
  presets = [
      ("latency", SystemEvaluator.latency(threshold_ms=30000)),
      ("turn_count", SystemEvaluator.turn_count(max_turns=10)),
      ("error_rate", SystemEvaluator.error_rate(max_error_rate=0.1)),
      (
          "token_efficiency",
          SystemEvaluator.token_efficiency(max_tokens=100000),
      ),
  ]
  for preset_name, evaluator in presets:
    try:
      report = await asyncio.to_thread(
          client.evaluate, evaluator=evaluator, filters=trace_filter
      )
      print(f"[{preset_name}]")
      print(report.summary())
    except Exception as exc:
      logger.warning("Evaluator %s failed: %s", preset_name, exc)
    print()

  # ---- 2c. LLM-as-Judge --------------------------------------------- #
  print("\n--- 2c. LLM-as-Judge Evaluation ---\n")
  try:
    judge = LLMAsJudge.correctness(threshold=0.6)
    report = await asyncio.to_thread(
        client.evaluate, evaluator=judge, filters=trace_filter
    )
    print(report.summary())
  except Exception as exc:
    logger.warning("LLM-as-Judge failed: %s", exc)

  # ---- 2d. Trajectory matching --------------------------------------- #
  print("\n--- 2d. Trajectory Matching ---\n")
  try:
    evaluator = BigQueryTraceEvaluator(
        project_id=PROJECT_ID,
        dataset_id=DATASET_ID,
        table_id=TABLE_ID,
    )
    # Tokyo trip (session index 1) should have called all four tools
    result = await evaluator.evaluate_session(
        session_id=session_ids[1],
        golden_trajectory=[
            {"tool_name": "search_flights"},
            {"tool_name": "search_hotels"},
            {"tool_name": "get_weather_forecast"},
            {"tool_name": "calculate_trip_budget"},
        ],
        match_type=MatchType.IN_ORDER,
    )
    print(f"  Session:  {result.session_id}")
    print(f"  Status:   {result.eval_status}")
    print(f"  Scores:   {result.scores}")
    if result.details:
      print(f"  Details:  {json.dumps(result.details, indent=2)}")
  except Exception as exc:
    logger.warning("Trajectory matching failed: %s", exc)


# ===================================================================== #
#  PHASE 3 — Insights                                                    #
# ===================================================================== #


async def phase3_insights(session_ids: list[str]) -> None:
  """Generate an Insights report over the traced sessions."""
  print("\n")
  print("#" * 64)
  print("#  PHASE 3: Generate Insights Report")
  print("#" * 64)

  client = Client(
      project_id=PROJECT_ID,
      dataset_id=DATASET_ID,
      table_id=TABLE_ID,
      location=LOCATION,
      endpoint=MODEL_NAME,
  )

  try:
    report = await asyncio.to_thread(
        client.insights,
        filters=TraceFilter(session_ids=session_ids),
        config=InsightsConfig(
            max_sessions=10,
            min_events_per_session=3,
            min_turns_per_session=1,
        ),
    )

    print("\n--- Insights Summary ---\n")
    print(report.summary())

    if hasattr(report, "executive_summary") and report.executive_summary:
      print("\n--- Executive Summary ---\n")
      print(report.executive_summary)

    if hasattr(report, "analysis_sections"):
      for section in report.analysis_sections:
        print(f"\n## {section.title}")
        print(section.content[:2000])
        if len(section.content) > 2000:
          print("  ... (truncated)")
  except Exception as exc:
    logger.warning("Insights generation failed: %s", exc)


# ===================================================================== #
#  Main                                                                  #
# ===================================================================== #


async def main() -> None:
  print("=" * 64)
  print("  End-to-End Demo: Agent -> BigQuery -> SDK Evaluation")
  print("=" * 64)
  print(f"  Project  : {PROJECT_ID}")
  print(f"  Dataset  : {DATASET_ID}")
  print(f"  Table    : {TABLE_ID}")
  print(f"  Model    : {MODEL_NAME}")
  print(f"  Location : {LOCATION}")
  print()

  # ---- Phase 1: Run agent conversations ----------------------------- #
  print("#" * 64)
  print("#  PHASE 1: Run Agent & Log Traces to BigQuery")
  print("#" * 64)

  agent = build_agent()
  runner, session_service, plugin = build_runner(agent)

  conversations = [
      {
          "label": "(Simple trip)",
          "messages": [
              (
                  "Plan a weekend trip from San Francisco to New York"
                  " departing 2025-04-12 and returning 2025-04-14."
                  " Search flights for April 12 and hotels checking"
                  " in April 12, checking out April 14."
              ),
          ],
      },
      {
          "label": "(Complex trip)",
          "messages": [
              (
                  "I want to plan a 5-day vacation to Tokyo from"
                  " 2025-05-01 to 2025-05-06. Search flights from"
                  " Los Angeles departing 2025-05-01, find hotels in"
                  " Tokyo checking in 2025-05-01 and checking out"
                  " 2025-05-06, check the weather for 2025-05-02,"
                  " and calculate the budget with the flight and"
                  " hotel prices you find plus $150/day expenses"
                  " for 5 days."
              ),
          ],
      },
      {
          "label": "(Multi-turn)",
          "messages": [
              ("What's the weather like in Paris on 2025-04-20?"),
              ("Find me flights from Chicago to Paris on" " 2025-04-20."),
              (
                  "Now find hotels in Paris checking in 2025-04-20"
                  " and checking out 2025-04-25."
              ),
          ],
      },
  ]

  session_ids: list[str] = []
  for conv in conversations:
    sid = await run_conversation(
        runner,
        session_service,
        conv["messages"],
        label=conv["label"],
    )
    session_ids.append(sid)

  # Flush and wait for data to land in BigQuery
  print("\n\nFlushing traces to BigQuery ...")
  try:
    await plugin.flush()
  except Exception as exc:
    logger.warning("Flush warning: %s", exc)

  settle_seconds = 15
  print(f"Waiting {settle_seconds}s for BigQuery data to settle ...")
  await asyncio.sleep(settle_seconds)

  # ---- Phase 2: Evaluate -------------------------------------------- #
  await phase2_evaluate(session_ids)

  # ---- Phase 3: Insights -------------------------------------------- #
  await phase3_insights(session_ids)

  # ---- Cleanup ------------------------------------------------------- #
  try:
    await plugin.shutdown(timeout=10.0)
  except Exception:
    pass

  # ---- Final summary ------------------------------------------------- #
  print("\n")
  print("=" * 64)
  print("  Demo Complete!")
  print("=" * 64)
  print(f"  Sessions created: {len(session_ids)}")
  for sid in session_ids:
    print(f"    - {sid}")
  print(f"  Traces logged to: {PROJECT_ID}.{DATASET_ID}.{TABLE_ID}")
  print()


if __name__ == "__main__":
  asyncio.run(main())
