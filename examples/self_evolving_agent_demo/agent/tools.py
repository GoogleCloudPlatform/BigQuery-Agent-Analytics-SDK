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

"""Deterministic basketball fixture tools for the self-evolving demo.

The data below is intentionally synthetic. The point of the demo is the
agent evolution loop and trace analytics, not live sports accuracy.
"""

from __future__ import annotations

from typing import Any

SEASON = "2025-26-demo"

PLAYERS: dict[str, dict[str, Any]] = {
    "nikola jokic": {
        "player": "Nikola Jokic",
        "team": "Denver Nuggets",
        "ppg": 26.4,
        "rpg": 12.4,
        "apg": 9.1,
        "ts_pct": 0.662,
        "usage_pct": 28.8,
        "assist_rate": 43.0,
        "strength": "elite half-court creation through post play and passing",
    },
    "joel embiid": {
        "player": "Joel Embiid",
        "team": "Philadelphia 76ers",
        "ppg": 31.8,
        "rpg": 10.9,
        "apg": 5.6,
        "ts_pct": 0.646,
        "usage_pct": 35.1,
        "assist_rate": 28.4,
        "strength": "dominant scoring pressure, foul generation, and rim defense",
    },
    "shai gilgeous-alexander": {
        "player": "Shai Gilgeous-Alexander",
        "team": "Oklahoma City Thunder",
        "ppg": 30.6,
        "rpg": 5.8,
        "apg": 6.4,
        "ts_pct": 0.635,
        "usage_pct": 32.4,
        "assist_rate": 29.9,
        "strength": "paint pressure, midrange scoring, and low-turnover creation",
    },
    "luka doncic": {
        "player": "Luka Doncic",
        "team": "Dallas Mavericks",
        "ppg": 29.8,
        "rpg": 8.7,
        "apg": 9.4,
        "ts_pct": 0.612,
        "usage_pct": 34.8,
        "assist_rate": 45.5,
        "strength": "pick-and-roll control and skip-pass creation",
    },
    "jayson tatum": {
        "player": "Jayson Tatum",
        "team": "Boston Celtics",
        "ppg": 27.1,
        "rpg": 8.2,
        "apg": 4.9,
        "ts_pct": 0.604,
        "usage_pct": 30.6,
        "assist_rate": 22.5,
        "strength": "two-way wing scoring with switchable defense",
    },
    "anthony edwards": {
        "player": "Anthony Edwards",
        "team": "Minnesota Timberwolves",
        "ppg": 27.8,
        "rpg": 5.5,
        "apg": 5.1,
        "ts_pct": 0.589,
        "usage_pct": 31.8,
        "assist_rate": 24.7,
        "strength": "rim pressure, transition force, and late-clock shot making",
    },
}

TEAMS: dict[str, dict[str, Any]] = {
    "denver nuggets": {
        "team": "Denver Nuggets",
        "wins": 55,
        "losses": 27,
        "off_rating": 119.1,
        "def_rating": 113.6,
        "net_rating": 5.5,
        "pace": 97.2,
        "profile": "methodical half-court offense built around Jokic actions",
        "late_game_edge": "high-value two-man actions and elite decision making",
    },
    "oklahoma city thunder": {
        "team": "Oklahoma City Thunder",
        "wins": 60,
        "losses": 22,
        "off_rating": 118.4,
        "def_rating": 109.2,
        "net_rating": 9.2,
        "pace": 100.5,
        "profile": "drive-heavy offense with aggressive point-of-attack defense",
        "late_game_edge": "Shai isolation plus five-out spacing",
    },
    "boston celtics": {
        "team": "Boston Celtics",
        "wins": 58,
        "losses": 24,
        "off_rating": 120.2,
        "def_rating": 111.1,
        "net_rating": 9.1,
        "pace": 98.9,
        "profile": "spacing, three-point volume, and switchable wing size",
        "late_game_edge": "multiple creators around elite spacing",
    },
    "dallas mavericks": {
        "team": "Dallas Mavericks",
        "wins": 50,
        "losses": 32,
        "off_rating": 117.2,
        "def_rating": 114.5,
        "net_rating": 2.7,
        "pace": 99.4,
        "profile": "pick-and-roll creation and corner spacing",
        "late_game_edge": "Doncic advantage creation against switches",
    },
    "minnesota timberwolves": {
        "team": "Minnesota Timberwolves",
        "wins": 53,
        "losses": 29,
        "off_rating": 115.8,
        "def_rating": 109.8,
        "net_rating": 6.0,
        "pace": 98.0,
        "profile": "rim protection, size, and Edwards downhill creation",
        "late_game_edge": "defense-to-offense swings and Edwards shot pressure",
    },
}

PLAYER_ALIASES = {
    "jokic": "nikola jokic",
    "nikola": "nikola jokic",
    "embiid": "joel embiid",
    "joel": "joel embiid",
    "shai": "shai gilgeous-alexander",
    "sga": "shai gilgeous-alexander",
    "gilgeous-alexander": "shai gilgeous-alexander",
    "luka": "luka doncic",
    "doncic": "luka doncic",
    "tatum": "jayson tatum",
    "jayson": "jayson tatum",
    "edwards": "anthony edwards",
    "anthony edwards": "anthony edwards",
}

TEAM_ALIASES = {
    "nuggets": "denver nuggets",
    "denver": "denver nuggets",
    "thunder": "oklahoma city thunder",
    "okc": "oklahoma city thunder",
    "celtics": "boston celtics",
    "boston": "boston celtics",
    "mavericks": "dallas mavericks",
    "mavs": "dallas mavericks",
    "dallas": "dallas mavericks",
    "timberwolves": "minnesota timberwolves",
    "wolves": "minnesota timberwolves",
    "minnesota": "minnesota timberwolves",
}


def _resolve_player(name: str) -> str:
  key = name.lower().strip()
  if key in PLAYERS:
    return key
  for alias, canonical in PLAYER_ALIASES.items():
    if alias in key:
      return canonical
  raise ValueError(f"Unknown demo player: {name}")


def _resolve_team(name: str) -> str:
  key = name.lower().strip()
  if key in TEAMS:
    return key
  for alias, canonical in TEAM_ALIASES.items():
    if alias in key:
      return canonical
  raise ValueError(f"Unknown demo team: {name}")


def lookup_basketball_reference(query: str) -> dict[str, Any]:
  """Return a broad basketball reference packet for ambiguous questions.

  This tool is intentionally verbose. V1 overuses it, which makes the
  SDK token analysis find a concrete optimization opportunity.
  """
  return {
      "query": query,
      "season": SEASON,
      "usage_note": (
          "Broad reference packet. Prefer narrow tools for player, team, "
          "and comparison questions when possible."
      ),
      "league_principles": [
          "Net rating estimates team strength better than wins alone.",
          "True shooting percentage helps compare scoring efficiency.",
          "Usage rate indicates how much offense a player carries.",
          "Assist rate and turnover context matter for primary creators.",
          "Pace changes counting stats and should be considered in team reads.",
          "Late-game offense rewards shot creation, spacing, and low turnovers.",
          "Playoff defense values rim protection and switchable point-of-attack size.",
          "Synthetic demo fixtures are stable so trace comparisons are repeatable.",
      ],
      "teams": list(TEAMS.values()),
      "players": list(PLAYERS.values()),
      "common_matchup_lenses": [
          "creation burden",
          "efficiency",
          "rim pressure",
          "spacing environment",
          "defensive matchup flexibility",
          "late-clock reliability",
          "transition creation",
          "bench context",
      ],
  }


def get_player_stats(player: str, season: str = SEASON) -> dict[str, Any]:
  """Return compact stats, strengths, and scoring profile for one player."""
  data = dict(PLAYERS[_resolve_player(player)])
  data["season"] = season
  return data


def get_team_profile(team: str, season: str = SEASON) -> dict[str, Any]:
  """Return team profile, strengths, and late-game strategy data."""
  data = dict(TEAMS[_resolve_team(team)])
  data["season"] = season
  return data


def compare_players(
    player_a: str,
    player_b: str,
    season: str = SEASON,
) -> dict[str, Any]:
  """Compare two demo players with a compact recommendation."""
  left = get_player_stats(player_a, season)
  right = get_player_stats(player_b, season)
  left_score = (
      left["ppg"] * 0.35
      + left["apg"] * 0.30
      + left["ts_pct"] * 20
      + left["assist_rate"] * 0.10
  )
  right_score = (
      right["ppg"] * 0.35
      + right["apg"] * 0.30
      + right["ts_pct"] * 20
      + right["assist_rate"] * 0.10
  )
  winner = left if left_score >= right_score else right
  return {
      "season": season,
      "player_a": left,
      "player_b": right,
      "recommended": winner["player"],
      "reason": (
          f"{winner['player']} has the stronger creation profile for this "
          "question because of scoring efficiency plus playmaking load."
      ),
  }


def compare_teams(
    team_a: str,
    team_b: str,
    season: str = SEASON,
) -> dict[str, Any]:
  """Compare two demo teams with a compact recommendation."""
  left = get_team_profile(team_a, season)
  right = get_team_profile(team_b, season)
  winner = left if left["net_rating"] >= right["net_rating"] else right
  return {
      "season": season,
      "team_a": left,
      "team_b": right,
      "recommended": winner["team"],
      "reason": (
          f"{winner['team']} has the better demo profile by net rating "
          "and role clarity."
      ),
  }


DEMO_TOOLS = [
    lookup_basketball_reference,
    get_player_stats,
    get_team_profile,
    compare_players,
    compare_teams,
]
