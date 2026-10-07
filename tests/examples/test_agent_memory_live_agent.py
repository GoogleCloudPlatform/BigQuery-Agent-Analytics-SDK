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

"""Hermetic tests for the live agent's tools in examples/agent_memory.

The tools are plain functions, so they run here without a model, the
BigQuery plugin or network. ``recall_memory`` reads the committed synthetic
fixture through the offline BigQuery stand-in.
"""

from __future__ import annotations

from datetime import datetime
from datetime import timezone
from pathlib import Path
import sys
import types

import pytest

from bigquery_agent_analytics import Client

EXAMPLE_DIR = Path(__file__).resolve().parents[2] / "examples" / "agent_memory"
sys.path.insert(0, str(EXAMPLE_DIR))

import live_agent  # noqa: E402
import offline_bigquery  # noqa: E402

NOW = datetime(2026, 10, 6, 16, 0, 0, tzinfo=timezone.utc)


class _ToolContext:
  """The parts of ADK's ToolContext the tools use."""

  def __init__(self, user_id: str = "demo-ana", session_id: str = "s-1"):
    self.state: dict = {}
    self.user_id = user_id
    self.session = types.SimpleNamespace(id=session_id, user_id=user_id)


def test_save_preference_writes_user_scoped_state():
  ctx = _ToolContext()

  result = live_agent.save_preference(" Diet ", "vegetarian", ctx)

  assert ctx.state == {"user:diet": "vegetarian"}
  assert result == {"status": "saved", "key": "diet", "value": "vegetarian"}


def test_save_preference_rejects_an_empty_key():
  ctx = _ToolContext()

  result = live_agent.save_preference("  ", "window", ctx)

  assert result["status"] == "rejected"
  assert ctx.state == {}


def test_hotel_inventory_times_out_then_answers_on_retry():
  inventory = live_agent.HotelInventory(failures=1)
  args = ("Kyoto", "Kyoto Station", "2026-10-14", "2026-10-16")

  with pytest.raises(TimeoutError, match="timed out"):
    inventory.search_hotels(*args)
  result = inventory.search_hotels(*args)

  assert [h["name"] for h in result["hotels"]] == [
      "Sakura Station Hotel",
      "Higashiyama Ryokan",
  ]
  assert result["hotels"][0]["price_per_night_usd"] == 180


@pytest.mark.parametrize(
    "city,diet,expected",
    [
        ("Kyoto", "vegetarian", ["Kamo Garden"]),
        ("kyoto", "Pescatarian", ["Kamo Garden", "Pontocho Grill"]),
        ("Osaka", "vegan", ["Dotonbori Greens"]),
        ("Osaka", "pescatarian", ["Dotonbori Greens", "Umeda Sushi Bar"]),
        ("Paris", "vegetarian", []),
    ],
)
def test_find_restaurants_filters_by_city_and_diet(city, diet, expected):
  result = live_agent.find_restaurants(city, diet, "2026-10-15")

  assert [r["name"] for r in result["restaurants"]] == expected


def test_search_flights_reports_window_seats_when_asked():
  result = live_agent.search_flights("SFO", "Tokyo", "2026-10-12", "window")

  assert [(f["flight"], f["window_seats_left"]) for f in result["flights"]] == [
      ("DM101", 12),
      ("DM205", 3),
  ]


def test_recall_memory_reads_only_this_users_memory():
  fixture = offline_bigquery.load_fixture()
  client = Client(
      project_id="offline-demo",
      dataset_id="agent_analytics",
      verify_schema=False,
      bq_client=offline_bigquery.OfflineBigQueryClient(fixture.rows),
  )
  recall_memory = live_agent.make_recall_memory(client, now=lambda: NOW)

  result = recall_memory(
      "dinner in Osaka that fits my diet", _ToolContext("u-ana", "s-104")
  )

  memory = result["memory"]
  assert memory.startswith("# Memory for user u-ana")
  assert "- diet = pescatarian (since 2026-10-04T18:00:01Z" in memory
  assert "vegan" not in memory


def test_session_script_runs_the_other_user_before_the_final_recall():
  users = [session.user_id for session in live_agent.SESSION_SCRIPT]

  assert len(users) == 5
  assert users[-1] == "demo-ana"
  assert "demo-ben" in users[:-1]
  assert live_agent.SESSION_SCRIPT[0].session_id("20261006t1200") == (
      "mem-20261006t1200-s1"
  )
