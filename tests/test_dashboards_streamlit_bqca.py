"""Tests for the BQCA Prompt & Response Logging Streamlit dashboard.

Covers ``bqca_models``, ``bqca_queries``, ``bqca_charts`` and the BQCA surface
of ``app.py`` with real pandas, plotly and Streamlit objects. BigQuery is never
contacted: SQL builders are asserted as text, the runner is driven through a
fake client, and the app is exercised end to end with
``streamlit.testing.v1.AppTest`` while the single ``bqca_queries.fetch_bqca``
seam is replaced.
"""

from __future__ import annotations

import ast
import collections
import contextlib
import dataclasses
import datetime as dt
import itertools
import os
from pathlib import Path
import re
import subprocess
import sys
import textwrap
from typing import Any, NamedTuple
from unittest import mock

import pytest

pytest.importorskip("streamlit")
pytest.importorskip("plotly")
pytest.importorskip("pandas")
pytest.importorskip("google.cloud.bigquery")

from google.api_core import exceptions as gexc
from google.auth import exceptions as gauth_exc
from google.cloud import bigquery
import pandas as pd
import plotly.graph_objects as go
from streamlit.testing.v1 import AppTest

ROOT = Path(__file__).resolve().parents[1]
DASHBOARDS_DIR = ROOT / "dashboards" / "streamlit"
if str(DASHBOARDS_DIR) not in sys.path:
  sys.path.insert(0, str(DASHBOARDS_DIR))

import bqca_charts
import bqca_models
import bqca_queries
import charts
import models
import queries

APP_PATH = DASHBOARDS_DIR / "app.py"
README_PATH = DASHBOARDS_DIR / "README.md"
MANUAL_PATH = (
    ROOT / "docs" / "guides" / "bqca-prompt-response-logging-manual.md"
)
ROOT_README_PATH = ROOT / "README.md"

UTC = dt.timezone.utc

# The labels the app promises its users (spec: BQCA Prompt & Response Logging
# dashboard). Hard-coded on purpose: they are a user-facing contract.
ADK_SURFACE = "ADK Agents"
BQCA_SURFACE = "BQCA Prompt & Response Logging"
ADK_TABS = [
    "Overview",
    "LLM & FinOps",
    "Tools & Execution",
    "Sessions & Traces",
]
BQCA_TABS = [
    "Overview & Latency",
    "Data Agents & Personas",
    "Prompt, Response & SQL Explorer",
    "Tokens & Embedding Suggestions",
    "Error Attribution",
]

# The nine public event types a BQCA data agent logs: the whole contract.
ALLOWED_EVENT_TYPES = {
    "INVOCATION_STARTING",
    "USER_MESSAGE_RECEIVED",
    "AGENT_RESPONSE",
    "INVOCATION_COMPLETED",
    "LLM_RESPONSE",
    "EMBEDDING_SUGGESTION",
    "INVOCATION_ERROR",
    "AGENT_ERROR",
    "LLM_ERROR",
}

# Event types the BQCA surface must never read, count or name. Assembled from
# parts so this file stays free of the very strings it forbids.
FORBIDDEN_EVENT_TYPES = (
    "TOOL_" + "STARTING",
    "TOOL_" + "COMPLETED",
    "TOOL_" + "ERROR",
    "LLM_" + "REQUEST",
)

WINDOW = models.Window(
    start=dt.datetime(2026, 10, 1, tzinfo=UTC),
    end=dt.datetime(2026, 10, 8, tzinfo=UTC),
)
STATE = bqca_models.BqcaFilterState(
    project_id="my-project", dataset_id="my_dataset"
)
TABLE = "`my-project.my_dataset.bqca_prompt_response_logs`"

# Panel keys of ``bqca_queries.PANELS`` grouped by how they scope their rows.
ALL_PANELS = (
    "filter_options",
    "kpis",
    "turn_volume",
    "latency",
    "token_usage",
    "data_agents",
    "personas",
    "embedding",
    "turns",
    "timeline",
    "errors",
)
SCOPED_PANELS = tuple(
    panel for panel in ALL_PANELS if panel not in ("filter_options", "timeline")
)


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch: pytest.MonkeyPatch):
  """Seeds the connection environment and keeps the developer's out."""
  monkeypatch.setenv("BQAA_DASHBOARD_SKIP_DOTENV", "1")
  monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "")
  monkeypatch.setenv("BQ_PROJECT_ID", "test-project")
  monkeypatch.setenv("BQ_DATASET_ID", "test_dataset")
  monkeypatch.setenv("STREAMLIT_LAZY_TABS", "false")
  for name in (
      "BQCA_TABLE_ID",
      "BQ_TABLE_ID",
      "BQ_VIEW_PREFIX",
      "BQAA_PROFILE",
  ):
    monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _fresh_query_cache():
  """Keeps the runner's process-wide result cache from leaking across tests."""
  bqca_queries._run_bqca_query_cached.clear()
  bqca_queries._SEEN_RUN_IDS.clear()
  yield
  bqca_queries._run_bqca_query_cached.clear()
  bqca_queries._SEEN_RUN_IDS.clear()


@pytest.fixture
def chart_session_state():
  """A throwaway ``st.session_state`` for the chart colour-slot registry.

  Only the chart tests use it: patching ``streamlit.session_state`` would break
  the AppTest runs, which manage their own session.
  """
  state: dict[str, object] = {}
  with mock.patch.object(charts.st, "session_state", state):
    yield state


uses_chart_state = pytest.mark.usefixtures("chart_session_state")


# --------------------------------------------------------------------------- #
# Synthetic result frames                                                      #
# --------------------------------------------------------------------------- #

NAN = float("nan")
T0 = pd.Timestamp("2026-10-05 09:00:00", tz="UTC")
D1 = pd.Timestamp("2026-10-05", tz="UTC")
D2 = pd.Timestamp("2026-10-06", tz="UTC")

RESPONSE_WITH_SQL = "Part A\n\nPart B\n```sql\nSELECT COUNT(*) FROM orders\n```"


def _kpi_frame() -> pd.DataFrame:
  return pd.DataFrame(
      [
          {
              "total_turns": 4,
              "completed_turns": 3,
              "error_events": 2,
              "turn_error_rate": 0.25,
              "p50_turn_latency_ms": 900.0,
              "p95_turn_latency_ms": 5000.0,
              "total_tokens": 130,
              "thoughts_tokens": 10,
              "cached_tokens": 40,
              "fast_path_rate": 1 / 3,
              "embedding_coverage": 0.5,
          }
      ]
  )


def _volume_frame() -> pd.DataFrame:
  return pd.DataFrame(
      {
          "bucket": [D1, D1, D2],
          "fast_path_label": [
              "standard_nl2sql",
              "fast_path",
              "standard_nl2sql",
          ],
          "turns": pd.array([3, 1, 2], dtype="Int64"),
          "completed_turns": pd.array([2, 1, 2], dtype="Int64"),
          "error_turns": pd.array([1, 0, 0], dtype="Int64"),
      }
  )


def _latency_frame() -> pd.DataFrame:
  return pd.DataFrame(
      {
          "bucket": [D1, D1, D1, D2, D2],
          "fast_path_label": [
              "all",
              "fast_path",
              "standard_nl2sql",
              "all",
              "standard_nl2sql",
          ],
          "turn_p50_ms": [900.0, 400.0, 900.0, 1000.0, 1000.0],
          "turn_p95_ms": [5000.0, 400.0, 5000.0, 4000.0, 4000.0],
          "turn_p99_ms": [5000.0, 400.0, 5000.0, 4100.0, 4100.0],
          "llm_p50_ms": [1200.0, NAN, 1200.0, 1100.0, 1100.0],
          "llm_p95_ms": [1200.0, NAN, 1200.0, 1100.0, 1100.0],
          "tfft_p50_ms": [300.0, NAN, 300.0, 250.0, 250.0],
          "tfft_p95_ms": [300.0, NAN, 300.0, 250.0, 250.0],
      }
  )


def _tokens_frame() -> pd.DataFrame:
  return pd.DataFrame(
      {
          "bucket": [D1, D1, D2],
          "model_name": ["gemini-x", "gemini-y", "gemini-x"],
          "llm_calls": [1, 2, 1],
          "input_tokens": [100, 50, 80],
          "output_tokens": [20, 10, 15],
          "thoughts_tokens": [10, 5, 0],
          "cached_tokens": [40, 0, 30],
          "total_tokens": [130, 65, 95],
      }
  )


def _agents_frame() -> pd.DataFrame:
  return pd.DataFrame(
      {
          "data_agent_id": ["DA1", "DA2", "unattributed"],
          "turns": [2, 1, 1],
          "unique_conversations": [2, 1, 1],
          "unique_users": [2, 1, 1],
          "error_rate": [0.0, 1.0, 0.0],
          "fast_path_rate": [0.5, 0.0, 0.0],
          "p50_latency_ms": [400.0, NAN, 900.0],
          "p95_latency_ms": [5000.0, NAN, 900.0],
          "total_tokens": [130, 0, 0],
      }
  )


def _personas_frame() -> pd.DataFrame:
  return pd.DataFrame(
      {
          "persona": ["analyst", "bob", "exec", "dave"],
          "turns": [1, 1, 1, 1],
          "unique_conversations": [1, 1, 1, 1],
          "error_rate": [0.0, 0.0, 1.0, 0.0],
          "avg_latency_ms": [5000.0, 400.0, NAN, 900.0],
          "total_tokens": [130, 0, 0, 0],
      }
  )


def _embedding_frame() -> pd.DataFrame:
  """P7 result: one row per (bucket, reason) with suggested-column counts."""
  return pd.DataFrame(
      {
          "bucket": [D1, D1, D2],
          "embedding_reason": [
              "ai_similarity_skill_fallback",
              "brute_force_keyword_search",
              "ai_similarity_skill_fallback",
          ],
          "suggestion_events": [2, 1, 1],
          "suggestion_turns": [2, 1, 1],
          "suggested_columns": [5, 1, 7],
          "avg_suggested_columns": [2.5, 1.0, 7.0],
          "zero_columns": [0, 0, 0],
          "one_column": [1, 1, 0],
          "two_to_four_columns": [1, 0, 0],
          "five_plus_columns": [0, 0, 1],
      }
  )


def _errors_frame() -> pd.DataFrame:
  return pd.DataFrame(
      {
          "event_type": ["INVOCATION_ERROR", "LLM_ERROR", "LLM_ERROR"],
          "data_agent_id": ["DA2", "DA2", None],
          "persona": ["exec", "exec", "dave"],
          "error_message": ["Invocation failed", "quota exceeded", "boom"],
          "errors": [1, 3, 2],
          "error_turns": [1, 3, 2],
          "first_seen": [D1, D1, D1],
          "last_seen": [D2, D2, D2],
      }
  )


def _turns_frame() -> pd.DataFrame:
  """Four turns, newest first: SQL answer, fast path, error, clarification."""
  return pd.DataFrame(
      [
          dict(
              timestamp=T0 + pd.Timedelta(hours=3),
              invocation_id="inv1",
              session_id="s1",
              conversation_id="c1",
              data_agent_id="DA1",
              persona="analyst",
              user_id="alice@example.com",
              fast_path=False,
              status="OK",
              turn_latency_ms=5000.0,
              total_tokens=130,
              thoughts_tokens=10,
              user_prompt="How many orders were placed? <script>x</script>",
              agent_response=RESPONSE_WITH_SQL,
              extracted_sql="SELECT COUNT(*) FROM orders",
              error_message=None,
          ),
          dict(
              timestamp=T0 + pd.Timedelta(hours=2),
              invocation_id="inv2",
              session_id="s2",
              conversation_id="c2",
              data_agent_id="DA1",
              persona="bob",
              user_id="bob@example.com",
              fast_path=True,
              status="OK",
              turn_latency_ms=400.0,
              total_tokens=None,
              thoughts_tokens=None,
              user_prompt="Top customers by revenue",
              agent_response="Here are the top customers.",
              extracted_sql=None,
              error_message=None,
          ),
          dict(
              timestamp=T0 + pd.Timedelta(hours=1),
              invocation_id="inv3",
              session_id="s3",
              conversation_id="c3",
              data_agent_id="DA2",
              persona="exec",
              user_id="exec@example.com",
              fast_path=False,
              status="ERROR",
              turn_latency_ms=None,
              total_tokens=None,
              thoughts_tokens=None,
              user_prompt="Revenue by region",
              agent_response=None,
              extracted_sql=None,
              error_message="quota exceeded | Invocation failed",
          ),
          dict(
              timestamp=T0,
              invocation_id="inv4",
              session_id="s4",
              conversation_id=None,
              data_agent_id=None,
              persona="dave",
              user_id="dave@example.com",
              fast_path=False,
              status="OK",
              turn_latency_ms=900.0,
              total_tokens=None,
              thoughts_tokens=None,
              user_prompt="Show sales",
              agent_response="Which region?",
              extracted_sql=None,
              error_message=None,
          ),
      ]
  )


def _timeline_frame() -> pd.DataFrame:
  kinds = [
      "INVOCATION_STARTING",
      "USER_MESSAGE_RECEIVED",
      "EMBEDDING_SUGGESTION",
      "LLM_RESPONSE",
      "AGENT_RESPONSE",
      "INVOCATION_COMPLETED",
  ]
  return pd.DataFrame(
      {
          "timestamp": [T0 + pd.Timedelta(seconds=i) for i in range(6)],
          "event_type": kinds,
          "span_id": [f"span-{i}" for i in range(6)],
          "parent_span_id": [None] + ["span-0"] * 5,
          "latency_ms": [NAN, NAN, NAN, 1200.0, NAN, 5000.0],
          "tfft_ms": [NAN, NAN, NAN, 300.0, NAN, NAN],
          "status": ["OK"] * 6,
          "error_message": [None] * 6,
          "model_name": [None, None, None, "gemini-x", None, None],
          "total_tokens": [NAN, NAN, NAN, 130.0, NAN, NAN],
          "thoughts_tokens": [NAN, NAN, NAN, 10.0, NAN, NAN],
          "fast_path": [False] * 6,
          "content_preview": ["", "How many orders?", "", "", "Part A", ""],
      }
  )


def _filter_options_frame() -> pd.DataFrame:
  return pd.DataFrame(
      {
          "kind": ["data_agent_id", "data_agent_id", "persona", "persona"],
          "value": ["DA1", "DA2", "alice", "exec"],
          "last_seen": [T0] * 4,
      }
  )


# Scan-log label of each panel -> the frame its query returns.
PANEL_FRAMES = {
    "Filter options": _filter_options_frame,
    "KPIs": _kpi_frame,
    "Turn volume": _volume_frame,
    "Latency": _latency_frame,
    "Token usage": _tokens_frame,
    "Data agents": _agents_frame,
    "Personas": _personas_frame,
    "Embedding suggestions": _embedding_frame,
    "Turns": _turns_frame,
    "Turn timeline": _timeline_frame,
    "Error attribution": _errors_frame,
}


# --------------------------------------------------------------------------- #
# bqca_models                                                                  #
# --------------------------------------------------------------------------- #


def test_allowlist_is_exactly_the_nine_public_event_types():
  assert set(bqca_models.BQCA_ALLOWED_EVENT_TYPES) == ALLOWED_EVENT_TYPES
  assert len(bqca_models.BQCA_ALLOWED_EVENT_TYPES) == len(ALLOWED_EVENT_TYPES)
  assert not set(FORBIDDEN_EVENT_TYPES) & set(
      bqca_models.BQCA_ALLOWED_EVENT_TYPES
  )


def test_filter_state_defaults_and_value_semantics():
  assert STATE.table_id == bqca_models.BQCA_DEFAULT_TABLE_ID
  assert STATE.table_id == "bqca_prompt_response_logs"
  assert STATE.time_window == "7d"
  assert STATE.fast_path_mode == bqca_models.FAST_PATH_ALL
  assert STATE.errors_only is False
  assert STATE.data_agent_ids == STATE.personas == STATE.event_types == ()
  # Frozen and hashable: it lives in session state and keys comparisons.
  assert dataclasses.replace(STATE) == STATE
  assert hash(dataclasses.replace(STATE)) == hash(STATE)
  with pytest.raises(dataclasses.FrozenInstanceError):
    STATE.project_id = "other"  # pytype: disable=attribute-error


@pytest.mark.parametrize(
    ("mode", "labels"),
    [
        (bqca_models.FAST_PATH_ALL, ("fast_path", "standard_nl2sql")),
        (bqca_models.FAST_PATH_ONLY, ("fast_path",)),
        (bqca_models.STANDARD_ONLY, ("standard_nl2sql",)),
    ],
)
def test_fast_path_mode_selects_labels(mode, labels):
  state = dataclasses.replace(STATE, fast_path_mode=mode)
  assert state.fast_path_labels() == labels


def test_unknown_fast_path_mode_is_rejected():
  state = dataclasses.replace(STATE, fast_path_mode="Everything")
  with pytest.raises(ValueError, match="fast-path mode"):
    state.fast_path_labels()


def test_selected_event_types_keep_only_allowlisted_in_canonical_order():
  state = dataclasses.replace(
      STATE,
      event_types=(
          "LLM_ERROR",
          FORBIDDEN_EVENT_TYPES[0],
          "made_up",
          "AGENT_RESPONSE",
          "LLM_ERROR",
      ),
  )
  assert state.selected_event_types() == ("AGENT_RESPONSE", "LLM_ERROR")
  assert STATE.selected_event_types() == ()
  everything_else = dataclasses.replace(
      STATE, event_types=FORBIDDEN_EVENT_TYPES
  )
  assert everything_else.selected_event_types() == ()


@pytest.mark.parametrize("token", ["1h", "6h", "24h", "3d", "7d", "30d"])
def test_preset_windows_are_snapped_and_span_the_preset(token):
  now = dt.datetime(2026, 10, 8, 12, 34, 56, tzinfo=UTC)
  state = dataclasses.replace(STATE, time_window=token)
  window = bqca_models.resolve_window(state, now)
  assert window.end == dt.datetime(2026, 10, 8, 12, 30, tzinfo=UTC)
  assert window.span == bqca_models.BQCA_TIME_WINDOWS[token][1]
  assert state.window(now) == window


def test_custom_window_is_used_as_given_and_normalized_to_utc():
  naive = dataclasses.replace(
      STATE,
      time_window="custom",
      custom_start=dt.datetime(2026, 9, 1),
      custom_end=dt.datetime(2026, 9, 4),
  )
  assert bqca_models.resolve_window(naive) == models.Window(
      start=dt.datetime(2026, 9, 1, tzinfo=UTC),
      end=dt.datetime(2026, 9, 4, tzinfo=UTC),
  )
  plus_two = dt.timezone(dt.timedelta(hours=2))
  shifted = dataclasses.replace(
      naive,
      custom_start=dt.datetime(2026, 9, 1, tzinfo=plus_two),
      custom_end=dt.datetime(2026, 9, 2, tzinfo=plus_two),
  )
  window = bqca_models.resolve_window(shifted)
  assert window.start == dt.datetime(2026, 8, 31, 22, tzinfo=UTC)
  assert window.start.utcoffset() == dt.timedelta(0)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"time_window": "custom"}, "start and an end"),
        (
            {"time_window": "custom", "custom_start": dt.datetime(2026, 9, 1)},
            "start and an end",
        ),
        (
            {
                "time_window": "custom",
                "custom_start": dt.datetime(2026, 9, 2),
                "custom_end": dt.datetime(2026, 9, 2),
            },
            "start before it ends",
        ),
        (
            {
                "time_window": "custom",
                "custom_start": dt.datetime(2026, 9, 3),
                "custom_end": dt.datetime(2026, 9, 2),
            },
            "start before it ends",
        ),
        ({"time_window": "2d"}, "Unknown time window"),
    ],
)
def test_unresolvable_windows_are_rejected(overrides, message):
  with pytest.raises(ValueError, match=message):
    bqca_models.resolve_window(dataclasses.replace(STATE, **overrides))


def test_kpi_summary_reads_a_result_row():
  kpi = bqca_models.BqcaKpiSummary.from_frame(_kpi_frame())
  assert kpi == bqca_models.BqcaKpiSummary(
      total_turns=4,
      completed_turns=3,
      error_events=2,
      turn_error_rate=0.25,
      p50_turn_latency_ms=900.0,
      p95_turn_latency_ms=5000.0,
      total_tokens=130,
      thoughts_tokens=10,
      cached_tokens=40,
      fast_path_rate=pytest.approx(1 / 3),
      embedding_coverage=0.5,
  )
  assert kpi.incomplete_turns == 1


def test_kpi_summary_turns_nulls_into_zero_and_undefined_figures_into_none():
  kpi = bqca_models.BqcaKpiSummary.from_row(
      {
          "total_turns": 3,
          "p50_turn_latency_ms": NAN,
          "turn_error_rate": NAN,
          "total_tokens": pd.NA,
      }
  )
  assert kpi.total_turns == 3
  assert kpi.p50_turn_latency_ms is None
  assert kpi.p95_turn_latency_ms is None
  # SAFE_DIVIDE over no turn is NULL: undefined, not a confident 0%.
  assert kpi.turn_error_rate is None
  assert kpi.total_tokens == 0
  assert kpi.embedding_coverage == 0.0
  # No completion was counted, so every turn is incomplete.
  assert kpi.completed_turns == 0
  assert kpi.incomplete_turns == 3


def test_kpi_summary_keeps_a_real_zero_error_rate():
  kpi = bqca_models.BqcaKpiSummary.from_row(
      {"total_turns": 3, "turn_error_rate": 0.0}
  )
  # Turns exist and none of them failed: that is 0%, and must not read as
  # "undefined".
  assert kpi.turn_error_rate is not None
  assert kpi.turn_error_rate == 0.0


def test_incomplete_turns_are_the_turns_that_never_completed():
  def kpi(total: int, completed: int) -> bqca_models.BqcaKpiSummary:
    return bqca_models.BqcaKpiSummary.from_row(
        {"total_turns": total, "completed_turns": completed}
    )

  assert kpi(4, 3).incomplete_turns == 1
  assert kpi(4, 4).incomplete_turns == 0
  assert kpi(4, 0).incomplete_turns == 4
  assert kpi(0, 0).incomplete_turns == 0
  # A malformed row must not report a negative number of turns.
  assert kpi(2, 5).incomplete_turns == 0


def test_kpi_summary_has_no_per_event_hit_rate_any_more():
  fields = {f.name for f in dataclasses.fields(bqca_models.BqcaKpiSummary)}
  assert "embedding_coverage" in fields
  assert "embedding_hit_rate" not in fields


def test_kpi_summary_of_an_empty_frame_is_none():
  assert bqca_models.BqcaKpiSummary.from_frame(pd.DataFrame()) is None


def test_turn_row_reads_every_field():
  turn = bqca_models.BqcaTurnRow.from_row(_turns_frame().iloc[0])
  assert turn.invocation_id == "inv1"
  assert turn.timestamp == (T0 + pd.Timedelta(hours=3)).to_pydatetime()
  assert (turn.data_agent_id, turn.persona) == ("DA1", "analyst")
  assert turn.fast_path is False
  assert turn.status == "OK"
  assert turn.turn_latency_ms == 5000.0
  assert (turn.total_tokens, turn.thoughts_tokens) == (130, 10)
  assert turn.extracted_sql == "SELECT COUNT(*) FROM orders"
  assert turn.agent_response == RESPONSE_WITH_SQL
  assert turn.error_message is None


def test_turn_row_defaults_missing_values():
  turn = bqca_models.BqcaTurnRow.from_row(
      {
          "timestamp": T0,
          "invocation_id": "inv-x",
          "persona": None,
          "status": NAN,
          "fast_path": pd.NA,
          "turn_latency_ms": NAN,
          "total_tokens": pd.NA,
          "data_agent_id": None,
      }
  )
  assert turn.persona == "unattributed"
  assert turn.status == "OK"
  assert turn.fast_path is False
  assert turn.turn_latency_ms is None
  assert turn.total_tokens is None
  assert turn.thoughts_tokens is None
  assert turn.data_agent_id is None
  assert turn.user_prompt is None


def test_turn_row_requires_a_timestamp():
  with pytest.raises(ValueError, match="timestamp"):
    bqca_models.BqcaTurnRow.from_row({"invocation_id": "inv-x"})


@pytest.mark.parametrize(
    ("logged", "expected"),
    [
        ("inv-1", "inv-1"),
        ("  inv-1 \n", "inv-1"),
        ("   ", ""),
        ("\t\n", ""),
        ("", ""),
        (None, ""),
        (NAN, ""),
        (pd.NA, ""),
    ],
)
def test_turn_row_reads_a_blank_invocation_id_as_empty(logged, expected):
  row = {"timestamp": T0, "invocation_id": logged}
  # A whitespace-only id is not an id (a NaN is no id at all, and must not
  # become the text "nan"), and a padded one is trimmed.
  assert bqca_models.BqcaTurnRow.from_row(row).invocation_id == expected


def test_turn_rows_keep_frame_order_and_survive_nullable_columns():
  rows = bqca_models.BqcaTurnRow.from_frame(_turns_frame())
  assert [row.invocation_id for row in rows] == ["inv1", "inv2", "inv3", "inv4"]
  assert rows[1].fast_path is True
  assert rows[1].total_tokens is None
  assert rows[2].status == "ERROR"
  assert rows[2].error_message == "quota exceeded | Invocation failed"
  assert rows[3].data_agent_id is None
  assert bqca_models.BqcaTurnRow.from_frame(pd.DataFrame()) == []


def test_turn_row_reports_how_many_responses_the_turn_logged():
  # Without the column (an older result frame) the count is simply unknown.
  assert (
      bqca_models.BqcaTurnRow.from_row(
          _turns_frame().iloc[0]
      ).agent_response_count
      is None
  )
  frame = _turns_frame().assign(agent_response_count=[2, 1, 0, pd.NA])
  rows = bqca_models.BqcaTurnRow.from_frame(frame)
  assert [row.agent_response_count for row in rows] == [2, 1, 0, None]


@pytest.mark.parametrize(
    ("logged", "shown"),
    [
        ("![a](http://x)", "[image: a](http://x)"),
        (
            "![](https://h/p.png?secret=1)",
            "[image: ](https://h/p.png?secret=1)",
        ),
        ("!![x](u)", "[image: x](u)"),
        ("!!![x](u)", "[image: x](u)"),
        (
            "![alt][ref]\n\n[ref]: http://h/i.png",
            "[image: alt][ref]\n\n[ref]: http://h/i.png",
        ),
        (
            "before ![a](u) mid ![b](v) after",
            "before [image: a](u) mid [image: b](v) after",
        ),
        ("[![a](img)](link)", "[[image: a](img)](link)"),
    ],
)
def test_inert_markdown_turns_every_image_opener_into_a_link(logged, shown):
  assert bqca_models.inert_markdown(logged) == shown
  assert "![" not in bqca_models.inert_markdown(logged)


@pytest.mark.parametrize(
    "text",
    [
        "Plain answer with no markup.",
        "A [link](https://example.com) and **bold** and `code`.",
        "Wow! [not an image] because the bang is spaced away.",
        "Mixed: price is $5! Done.",
        "```sql\nSELECT arr[0] FROM t WHERE x != 1\n```",
        "",
    ],
)
def test_inert_markdown_leaves_ordinary_text_untouched(text):
  assert bqca_models.inert_markdown(text) == text


def test_inert_markdown_reads_none_as_empty_and_stringifies_other_values():
  assert bqca_models.inert_markdown(None) == ""
  assert (
      bqca_models.inert_markdown(42) == "42"
  )  # pytype: disable=wrong-arg-types


def test_inert_markdown_is_idempotent():
  once = bqca_models.inert_markdown("!![x](u) and ![y](v)")
  assert bqca_models.inert_markdown(once) == once


def test_inert_markdown_leaves_no_markdown_image_for_a_renderer_to_fetch():
  hostile = (
      "ok ![a](http://evil.example/a.png) ![b](//evil.example/b.png) !![c](d)"
  )
  rendered = bqca_models.inert_markdown(hostile)
  assert not re.search(r"!\[", rendered)
  assert rendered.count("[image: ") == 3


# --------------------------------------------------------------------------- #
# SQL builders                                                                 #
# --------------------------------------------------------------------------- #


def _sql(
    panel: str,
    state: bqca_models.BqcaFilterState = STATE,
    window: models.Window | None = WINDOW,
    **kwargs: Any,
) -> str:
  builder, _ = bqca_queries.PANELS[panel]
  if panel == "timeline":
    kwargs.setdefault("invocation_id", "inv-1")
  return builder(state, window=window, **kwargs)


def _scalars(panel: str) -> dict[str, str]:
  return {"invocation_id": "inv-1"} if panel == "timeline" else {}


def _referenced(panel: str) -> set[str]:
  return set(re.findall(r"@([A-Za-z_]\w*)", _sql(panel)))


def test_every_builder_is_a_registered_panel():
  declared = {
      name
      for name, obj in vars(bqca_queries).items()
      if re.fullmatch(r"build_bqca_\w+_sql", name) and callable(obj)
  }
  registered = {builder.__name__ for builder, _ in bqca_queries.PANELS.values()}
  assert declared == registered
  assert set(bqca_queries.PANELS) == set(ALL_PANELS)
  labels = [label for _, label in bqca_queries.PANELS.values()]
  assert len(labels) == len(set(labels)) == len(ALL_PANELS)
  assert set(labels) == set(PANEL_FRAMES)


@pytest.mark.parametrize("panel", ALL_PANELS)
def test_sql_reads_only_allowlisted_events_from_the_validated_table(panel):
  sql = _sql(panel)
  assert "event_type IN UNNEST(@allowed_event_types)" in sql
  assert TABLE in sql
  assert queries.time_bounds(WINDOW) in sql
  for forbidden in FORBIDDEN_EVENT_TYPES:
    assert forbidden not in sql
  # Every event type spelled out in the SQL text is an allowlisted one.
  spelled = set(re.findall(r"'([A-Z]+(?:_[A-Z]+)+)'", sql))
  assert spelled <= ALLOWED_EVENT_TYPES


@pytest.mark.parametrize("panel", ALL_PANELS)
def test_every_sql_parameter_has_a_binding(panel):
  sql = _sql(panel)
  params = bqca_queries.bqca_query_params(STATE, sql, **_scalars(panel))
  assert {p.name for p in params} == _referenced(panel)


def test_each_filter_reaches_exactly_the_panels_it_applies_to():
  referenced = {panel: _referenced(panel) for panel in ALL_PANELS}

  def users_of(name: str) -> set[str]:
    return {panel for panel, names in referenced.items() if name in names}

  # The event-type multiselect only narrows the panels that list events.
  assert users_of("event_types") == {"timeline", "errors"}
  assert users_of("prompt_search") == {"turns"}
  assert users_of("invocation_id") == {"timeline"}
  for name in (
      "data_agent_ids",
      "personas",
      "fast_path_labels",
      "session_search",
  ):
    assert users_of(name) == set(SCOPED_PANELS), name
  assert users_of("allowed_event_types") == set(ALL_PANELS)


@pytest.mark.parametrize("panel", ALL_PANELS)
def test_user_text_never_reaches_sql_text(panel):
  hostile = dataclasses.replace(
      STATE,
      data_agent_ids=("x') OR 1=1 --",),
      personas=("p'; DROP TABLE t; --",),
      event_types=("AGENT_ERROR", "e'; DELETE FROM t; --"),
      session_search="s'); DROP TABLE t; --",
      prompt_search='" OR "1"="1',
  )
  benign = dataclasses.replace(
      STATE,
      data_agent_ids=("a",),
      personas=("b",),
      event_types=("AGENT_ERROR",),
      session_search="c",
      prompt_search="d",
  )
  sql = _sql(panel, hostile)
  assert sql == _sql(panel, benign)
  for marker in ("DROP TABLE", "DELETE FROM", "OR 1=1", '"1"="1"'):
    assert marker not in sql


@pytest.mark.parametrize("panel", SCOPED_PANELS)
def test_errors_only_keeps_whole_turns_that_hold_an_error(panel):
  narrowed = _sql(panel, dataclasses.replace(STATE, errors_only=True))
  assert "turn_has_error" not in _sql(panel)
  # A turn that has an error somewhere keeps every one of its events: the
  # verdict is read over the turn's partition, where an event with no turn at
  # all is the only row of a partition of its own ...
  assert (
      "LOGICAL_OR(is_error) OVER (PARTITION BY "
      f"{SPEC_TURN_KEY}) AS turn_has_error"
  ) in narrowed
  # ... where "an error" is still the one canonical three-condition predicate.
  assert f"{bqca_queries.IS_ERROR_EXPR} AS is_error" in narrowed
  assert f"LOGICAL_OR({bqca_queries.IS_ERROR_EXPR})" not in narrowed
  # Once as the computed column, once as the predicate that uses it.
  assert narrowed.count("turn_has_error") == 2
  assert "AND turn_has_error" in narrowed


@pytest.mark.parametrize("panel", ["filter_options", "timeline"])
def test_errors_only_does_not_hide_filter_options_or_timeline_rows(panel):
  state = dataclasses.replace(STATE, errors_only=True)
  assert _sql(panel, state) == _sql(panel)


@pytest.mark.parametrize("panel", SCOPED_PANELS)
def test_error_means_the_three_condition_predicate(panel):
  sql = _sql(panel)
  assert bqca_queries.IS_ERROR_EXPR in sql
  for condition in (
      "UPPER(TRIM(status)) = 'ERROR'",
      "NULLIF(TRIM(error_message), '') IS NOT NULL",
      "ENDS_WITH(UPPER(TRIM(event_type)), '_ERROR')",
  ):
    assert condition in bqca_queries.IS_ERROR_EXPR
  assert "status = 'ERROR'" not in sql


def test_attribution_keys_on_the_data_agent_id_label_not_the_agent_column():
  label = 'session_metadata.state."data-agent-id"'
  for panel in ("filter_options", *SCOPED_PANELS):
    assert label in _sql(panel), panel
  for panel in ALL_PANELS:
    without_literals = re.sub(r"'[^']*'", "''", _sql(panel))
    assert not re.search(r"\bagent\b", without_literals), panel


def test_persona_falls_back_from_label_to_user_handle_to_data_agent():
  # Each tier is its own per-row expression ...
  assert "custom_labels.persona" in bqca_queries.RAW_EXPLICIT_PERSONA_EXPR
  assert "REGEXP_EXTRACT(TRIM(user_id)" in bqca_queries.RAW_EMAIL_PERSONA_EXPR
  assert '"data-agent-id"' in bqca_queries.DATA_AGENT_ID_EXPR
  # ... and a turn's persona is the first tier it carries, in that order,
  # with the fallback last.
  for panel in SCOPED_PANELS:
    events = _cte_body(_sql(panel), "events")
    assert (
        "COALESCE(first_explicit_persona, first_email_persona,"
        " first_data_agent_id, 'unattributed') AS persona"
    ) in events, panel


# The Looker Studio BQCA profile (a sibling deliverable) extracts the same
# fields. The parity checks below skip when its template is not in the tree.
LOOKER_BQCA_TEMPLATE = (
    ROOT / "dashboard" / "looker_studio" / "sql" / "bqca_events_v1.sql.tmpl"
)


def _squash(sql: str) -> str:
  """Collapses layout (newlines, indents, space inside brackets) for matching."""
  text = re.sub(r"\s+", " ", sql)
  text = re.sub(r"\(\s+", "(", text)
  text = re.sub(r"\s+([)\]])", r"\1", text)
  return re.sub(r"\s+\[", "[", text)


def _looker_template() -> str:
  if not LOOKER_BQCA_TEMPLATE.exists():
    pytest.skip("the Looker Studio BQCA template is not in this checkout")
  return LOOKER_BQCA_TEMPLATE.read_text(encoding="utf-8")


def _persona_regex() -> str:
  """The regular expression the persona expression applies to ``user_id``."""
  match = re.search(
      r"REGEXP_EXTRACT\(TRIM\(user_id\), r'([^']+)'\)",
      bqca_queries.RAW_EMAIL_PERSONA_EXPR,
  )
  assert match, bqca_queries.RAW_EMAIL_PERSONA_EXPR
  return match.group(1)


def _email_handle(user_id: str | None) -> str | None:
  """What the persona regex extracts from ``user_id`` (``None``: no match)."""
  if user_id is None:
    return None
  match = re.search(_persona_regex(), user_id.strip())
  return match.group(1) if match else None


def test_persona_regex_sits_in_a_raw_sql_string_and_has_no_split_shortcut():
  email = bqca_queries.RAW_EMAIL_PERSONA_EXPR
  pattern = _persona_regex()
  assert "SPLIT(" not in email
  assert f"r'{pattern}'" in email
  # Single backslashes: a doubled one would match a literal backslash.
  assert "\\\\" not in pattern
  assert pattern.count("\\.") == 2
  for panel in ALL_PANELS:
    assert "SPLIT(TRIM(user_id)" not in _sql(panel), panel
  # Both persona tiers that come from the row itself are read by every panel
  # that filters or lists personas, through their one canonical expression.
  for panel in ("filter_options", *SCOPED_PANELS):
    assert email in _sql(panel), panel
    assert bqca_queries.RAW_EXPLICIT_PERSONA_EXPR in _sql(panel), panel


@pytest.mark.parametrize(
    ("user_id", "handle"),
    [
        ("alice@example.com", "alice"),
        ("Bob.Smith+tag@sub.example.co.uk", "Bob.Smith+tag"),
        ("alice@example.com:session-7", "alice"),
        ("  carol@example.com  ", "carol"),
        ("first_last%x@a-b.example.org", "first_last%x"),
        ("ALICE@EXAMPLE.COM", "ALICE"),
    ],
)
def test_persona_handle_is_taken_from_a_real_email_address(user_id, handle):
  assert _email_handle(user_id) == handle


@pytest.mark.parametrize(
    "user_id",
    [
        "service-account",
        "12345",
        "svc-reporting@",
        "@example.com",
        "a@b",
        "user@localhost",
        "alice@example.com extra",
        "alice@example.com@evil.example",
        "projects/123/users/alice@example.com",
        "accounts.google.com:1234567890",
        "",
        "   ",
        None,
    ],
)
def test_persona_never_invents_a_handle_from_an_opaque_user_id(user_id):
  # No handle means the persona falls through to the data agent.
  assert _email_handle(user_id) is None


def test_persona_regex_is_the_one_the_looker_profile_uses():
  assert f"r'{_persona_regex()}'" in _looker_template()


def test_model_name_falls_back_from_model_to_model_version():
  expr = bqca_queries.MODEL_NAME_EXPR
  first = "NULLIF(JSON_VALUE(attributes, '$.model'), '')"
  second = "NULLIF(JSON_VALUE(attributes, '$.model_version'), '')"
  assert expr.startswith("COALESCE(")
  # Blank-safe, so an empty "model" does not shadow the version.
  assert 0 <= expr.index(first) < expr.index(second)
  for panel in ("token_usage", "timeline"):
    assert expr in _sql(panel), panel


TOKEN_PATHS = {
    "INPUT_TOKENS_EXPR": (
        "$.usage_metadata.prompt_token_count",
        "$.usage_metadata.prompt_tokens",
        "$.usage.prompt",
    ),
    "OUTPUT_TOKENS_EXPR": (
        "$.usage_metadata.candidates_token_count",
        "$.usage_metadata.completion_tokens",
        "$.usage.completion",
    ),
    "TOTAL_TOKENS_EXPR": (
        "$.usage_metadata.total_token_count",
        "$.usage_metadata.total_tokens",
        "$.usage.total",
    ),
    "THOUGHTS_TOKENS_EXPR": ("$.usage_metadata.thoughts_token_count",),
    "CACHED_TOKENS_EXPR": ("$.usage_metadata.cached_content_token_count",),
}


@pytest.mark.parametrize(("name", "paths"), TOKEN_PATHS.items())
def test_token_expression_reads_its_paths_in_priority_order(name, paths):
  expr = getattr(bqca_queries, name)
  assert tuple(re.findall(r"'(\$\.[^']+)'", expr)) == paths
  assert expr.count("SAFE_CAST(") == len(paths)
  for path in paths:
    # The model's own keys live in ``attributes``; ``usage`` is in ``content``.
    column = "content" if path.startswith("$.usage.") else "attributes"
    assert f"SAFE_CAST(JSON_VALUE({column}, '{path}') AS INT64)" in expr
  # Fallbacks only chain where an alternative spelling exists.
  assert expr.startswith("COALESCE(") == (len(paths) > 1)
  assert "latency_ms" not in expr


@pytest.mark.parametrize("panel", ALL_PANELS)
def test_the_latency_column_is_only_read_for_timings(panel):
  paths = set(
      re.findall(r"JSON_VALUE\(latency_ms, '(\$\.[^']+)'\)", _sql(panel))
  )
  assert paths <= {"$.total_ms", "$.time_to_first_token_ms"}, panel


def test_model_token_and_embedding_paths_match_the_looker_profile():
  template = _looker_template()
  exprs = [
      bqca_queries.MODEL_NAME_EXPR,
      bqca_queries.SUGGESTED_COLUMNS_COUNT_EXPR,
      bqca_queries.EMBEDDING_REASON_EXPR,
      *(getattr(bqca_queries, name) for name in TOKEN_PATHS),
  ]
  paths = set(re.findall(r"'(\$\.[^']+)'", " ".join(exprs)))
  assert len(paths) >= 12
  for path in sorted(paths):
    assert f"'{path}'" in template, path


def _json_value(payload: dict[str, Any], key: str) -> str | None:
  """JSON_VALUE: a scalar as text; NULL for a missing key, null, array, object."""
  value = payload.get(key)
  if value is None or isinstance(value, (list, dict)):
    return None
  if isinstance(value, bool):
    return "true" if value else "false"
  return str(value)


def _suggested_columns(payload: dict[str, Any]) -> int:
  """Evaluates SUGGESTED_COLUMNS_COUNT_EXPR's COALESCE chain over a payload."""
  expr = bqca_queries.SUGGESTED_COLUMNS_COUNT_EXPR
  terms = re.findall(
      r"ARRAY_LENGTH\(JSON_QUERY_ARRAY\(content, '\$\.(\w+)'\)\)"
      r"|SAFE_CAST\(JSON_VALUE\(content, '\$\.(\w+)'\) AS INT64\)",
      expr,
  )
  assert terms, expr
  for array_key, scalar_key in terms:
    if array_key:
      value = payload.get(array_key)
      if isinstance(value, list):
        return len(value)
    else:
      text = _json_value(payload, scalar_key)
      if text is not None and re.fullmatch(r"-?\d+", text.strip()):
        return int(text)
  # The COALESCE ends in a literal zero: no columns at all is a size of 0.
  assert expr.endswith(", 0)"), expr
  return 0


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        # What the logging plugin really writes: one event per suggestion.
        (
            {
                "reason": "ai_similarity_skill_fallback",
                "suggested_columns": ["orders.id", "orders.total"],
            },
            2,
        ),
        (
            {
                "reason": "filter_refinement_fallback",
                "suggested_columns": ["a"],
            },
            1,
        ),
        (
            {
                "reason": "expensive_ai_operators",
                "suggested_columns": list("abcdef"),
            },
            6,
        ),
        # An empty list is omitted from the payload altogether.
        ({"reason": "brute_force_keyword_search"}, 0),
        ({"reason": "brute_force_keyword_search", "suggested_columns": []}, 0),
        ({}, 0),
        # Older payload shapes still count.
        ({"similar_queries_count": 3}, 3),
        ({"similar_queries_count": "4"}, 4),
        ({"suggestions": [{"q": 1}, {"q": 2}]}, 2),
        # The producer's schema wins over a stray legacy field.
        ({"suggested_columns": ["a"], "similar_queries_count": 9}, 1),
        ({"suggested_columns": [], "similar_queries_count": 9}, 0),
        # Garbage in a legacy field falls through to the next candidate.
        ({"similar_queries_count": "many", "suggestions": [{}]}, 1),
        ({"similar_queries_count": "many"}, 0),
    ],
)
def test_suggested_columns_are_counted_from_the_real_producer_payload(
    payload, expected
):
  assert _suggested_columns(payload) == expected


def test_embedding_reason_is_blank_safe_and_defaults_to_unspecified():
  expr = bqca_queries.EMBEDDING_REASON_EXPR
  assert expr.startswith("COALESCE(")
  assert "NULLIF(JSON_VALUE(content, '$.reason'), '')" in expr
  assert expr.endswith("'(unspecified)')")


def test_the_legacy_similar_queries_name_aliases_the_suggested_columns_expr():
  assert (
      bqca_queries.SIMILAR_QUERIES_COUNT_EXPR
      is bqca_queries.SUGGESTED_COLUMNS_COUNT_EXPR
  )


def _stale_similar_queries(sql: str) -> list[str]:
  """Uses of ``similar_queries`` other than the legacy ``$.`` payload path."""
  return re.findall(r"(?<!\$\.)similar_queries\w*", sql)


def test_embedding_coverage_is_the_share_of_turns_with_suggested_columns():
  sql = _squash(_sql("kpis"))
  # Sized only on suggestion events, with the one canonical expression.
  assert (
      "IF(event_type = 'EMBEDDING_SUGGESTION',"
      f" {bqca_queries.SUGGESTED_COLUMNS_COUNT_EXPR}, NULL)"
      " AS suggested_columns_count"
  ) in sql
  # Every turn is in the denominator: a per-suggestion rate would always read
  # 100% because the logger drops suggestions that carry no column.
  assert (
      "SAFE_DIVIDE("
      "COUNT(DISTINCT IF(suggested_columns_count > 0, invocation_id, NULL)),"
      " COUNT(DISTINCT invocation_id)) AS embedding_coverage"
  ) in sql
  assert "embedding_hit_rate" not in sql
  assert _stale_similar_queries(sql) == []


def test_embedding_panel_groups_by_reason_and_bins_suggested_columns():
  sql = _squash(_sql("embedding"))
  for fragment in (
      f"{bqca_queries.EMBEDDING_REASON_EXPR} AS embedding_reason",
      f"{bqca_queries.SUGGESTED_COLUMNS_COUNT_EXPR} AS suggested_columns_count",
      "GROUP BY bucket, embedding_reason",
      "ORDER BY bucket, embedding_reason",
      "COUNT(*) AS suggestion_events",
      "COUNT(DISTINCT IF(suggested_columns_count > 0, invocation_id, NULL))"
      " AS suggestion_turns",
      "SUM(suggested_columns_count) AS suggested_columns",
      "AVG(suggested_columns_count) AS avg_suggested_columns",
      "COUNTIF(suggested_columns_count = 0) AS zero_columns",
      "COUNTIF(suggested_columns_count = 1) AS one_column",
      "COUNTIF(suggested_columns_count BETWEEN 2 AND 4) AS two_to_four_columns",
      "COUNTIF(suggested_columns_count >= 5) AS five_plus_columns",
      "event_type = 'EMBEDDING_SUGGESTION'",
  ):
    assert fragment in sql, fragment
  assert _stale_similar_queries(sql) == []
  assert "hit_rate" not in sql


def test_explorer_serves_the_last_agent_response_with_the_sql_it_carries():
  sql = _squash(_sql("turns"))
  assert (
      "ARRAY_AGG(IF(event_type = 'AGENT_RESPONSE',"
      " STRUCT(agent_response_text AS response_text,"
      " extracted_sql AS response_sql), NULL)"
      " IGNORE NULLS ORDER BY timestamp DESC LIMIT 1)[SAFE_OFFSET(0)]"
      " AS served_response"
  ) in sql
  assert "served_response.response_text AS agent_response" in sql
  assert "served_response.response_sql AS extracted_sql" in sql
  assert "COUNTIF(event_type = 'AGENT_RESPONSE') AS agent_response_count" in sql
  # The text and the SQL are never aggregated on their own, so they cannot come
  # from two different responses of one turn.
  assert "ARRAY_AGG(agent_response_text" not in sql
  assert "ARRAY_AGG(extracted_sql" not in sql
  assert "STRING_AGG(agent_response_text" not in sql
  # The prompt is still the first one logged; only the answer is the last.
  assert (
      "ARRAY_AGG(user_prompt_text IGNORE NULLS ORDER BY timestamp ASC LIMIT 1)"
      "[SAFE_OFFSET(0)] AS user_prompt"
  ) in sql
  # ``LIMIT 1`` on a word boundary: the explorer's own ``LIMIT 100`` is not one.
  assert len(re.findall(r"ORDER BY timestamp ASC LIMIT 1\b", sql)) == 1
  assert len(re.findall(r"ORDER BY timestamp DESC LIMIT 1\b", sql)) == 1


@pytest.mark.parametrize(
    ("panel", "fragments"),
    [
        (
            "kpis",
            [
                "HAVING COUNT(*) > 0",
                "APPROX_QUANTILES(turn_latency_ms, 100)[SAFE_OFFSET(50)]",
                "APPROX_QUANTILES(turn_latency_ms, 100)[SAFE_OFFSET(95)]",
                "thoughts_token_count",
                "cached_content_token_count",
                "COUNT(DISTINCT IF(is_error, invocation_id, NULL))",
                "suggested_columns_count > 0",
                "AS embedding_coverage",
            ],
        ),
        (
            "turn_volume",
            [
                "LOGICAL_OR(fast_path)",
                "GROUP BY invocation_id",
                "GROUP BY bucket, fast_path_label",
            ],
        ),
        (
            "latency",
            [
                "GROUPING SETS ((bucket), (bucket, fast_path_label))",
                "$.total_ms",
                "$.time_to_first_token_ms",
                "COALESCE(path_label, 'all')",
            ],
        ),
        (
            "token_usage",
            [
                "event_type = 'LLM_RESPONSE'",
                "$.usage_metadata.prompt_token_count",
                "$.usage_metadata.prompt_tokens",
                "$.usage.prompt",
                "$.usage_metadata.candidates_token_count",
                "$.usage_metadata.completion_tokens",
                "$.usage.completion",
                "$.usage_metadata.thoughts_token_count",
                "$.usage_metadata.cached_content_token_count",
                "$.usage_metadata.total_token_count",
                "$.usage_metadata.total_tokens",
                "$.usage.total",
                "$.model_version",
                "IFNULL(model_name, '(unknown)')",
            ],
        ),
        (
            "data_agents",
            [
                "COALESCE(MAX(NULLIF(data_agent_id, 'unattributed')),"
                " 'unattributed')",
                "GROUP BY invocation_id",
                "GROUP BY data_agent_id",
                "LIMIT 100",
            ],
        ),
        ("personas", ["GROUP BY persona", "LIMIT 100"]),
        (
            "embedding",
            [
                "event_type = 'EMBEDDING_SUGGESTION'",
                "COUNTIF(suggested_columns_count BETWEEN 2 AND 4)",
                "COUNTIF(suggested_columns_count >= 5)",
                "GROUP BY bucket, embedding_reason",
            ],
        ),
        (
            "turns",
            [
                "$.response.parts",
                "$.markdown",
                r"```sql\s*(.*?)\s*```",
                "ORDER BY timestamp DESC",
                "LIMIT 100",
                "STRPOS(LOWER(IFNULL(user_prompt, '')), LOWER(@prompt_search))",
            ],
        ),
        (
            "timeline",
            [
                "IF(invocation_id IS NULL, NULL,"
                " COALESCE(NULLIF(TRIM(invocation_id), ''),"
                " NULLIF(TRIM(trace_id), ''),"
                " NULLIF(TRIM(session_id), ''),"
                " CAST(timestamp AS STRING))) = @invocation_id",
                "ORDER BY timestamp, event_type",
                "LIMIT 200",
                "SUBSTR(",
            ],
        ),
        (
            "errors",
            [
                "AND is_error",
                "GROUP BY event_type, data_agent_id, persona, error_message",
                "ORDER BY errors DESC, last_seen DESC",
                "LIMIT 100",
                "(no message)",
            ],
        ),
        (
            "filter_options",
            [
                "GROUP BY o.kind, o.value",
                "STRUCT('persona', e.persona)",
                "STRUCT('event_type', e.event_type)",
            ],
        ),
    ],
)
def test_panel_sql_carries_its_canonical_fragments(panel, fragments):
  sql = _sql(panel)
  for fragment in fragments:
    assert fragment in sql, f"{panel} lost {fragment!r}"


def test_response_text_is_the_markdown_of_every_part_joined_in_order():
  expr = bqca_queries.AGENT_RESPONSE_TEXT_EXPR
  assert "$.response.parts" in expr
  assert "ORDER BY part_offset" in expr
  # Parts are separated by a blank line; text_summary still wins when logged.
  assert "'\\n\\n'" in expr
  assert expr.index("$.text_summary") < expr.index("$.response.parts")


# --------------------------------------------------------------------------- #
# Review fixes: whole prompts, one latency sample per turn, completed-turn     #
# fast-path rate                                                               #
# --------------------------------------------------------------------------- #


def _user_prompt_text(payload: dict[str, Any]) -> str | None:
  """Evaluates USER_PROMPT_TEXT_EXPR's COALESCE chain over a payload.

  The arms are tried in the order the expression lists them, so reordering the
  SQL changes the answer here exactly as it changes it in BigQuery.
  """
  expr = bqca_queries.USER_PROMPT_TEXT_EXPR
  parts = payload.get("parts")
  parts = parts if isinstance(parts, list) else []
  first = parts[0] if parts and isinstance(parts[0], dict) else {}
  texts = [
      _json_value(part, "text") for part in parts if isinstance(part, dict)
  ]
  arms = {
      expr.index("'$.text_summary'"): _json_value(payload, "text_summary"),
      expr.index("'$.prompt'"): _json_value(payload, "prompt"),
      expr.index("'$.parts[0].text'"): _json_value(first, "text"),
      expr.index("ARRAY_TO_STRING("): "\n".join(
          text for text in texts if text is not None
      ),
  }
  # Every arm is a NULLIF(..., ''), so a NULL and an empty string both fall
  # through; an arm this evaluator does not know about fails loudly instead.
  assert expr.count("NULLIF(") == len(arms), expr
  for _, value in sorted(arms.items()):
    if value:
      return value
  return None


def test_prompt_text_reads_every_part_before_the_first_part_alone():
  expr = bqca_queries.USER_PROMPT_TEXT_EXPR
  order = [
      expr.index("'$.text_summary'"),
      expr.index("'$.prompt'"),
      expr.index("ARRAY_TO_STRING(ARRAY(SELECT JSON_VALUE(p, '$.text')"),
      expr.index("'$.parts[0].text'"),
  ]
  # The first part alone is only the last resort: when it came first it cut a
  # multi-part prompt down to part 1 in the explorer and in the prompt search.
  assert order == sorted(order)
  for panel in ("turns", "timeline"):
    assert expr in _sql(panel), panel


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        # A multi-part prompt is every text part, in order.
        (
            {"parts": [{"text": "Show orders"}, {"text": "for EMEA"}]},
            "Show orders\nfor EMEA",
        ),
        ({"parts": [{"text": "a"}, {"text": "b"}, {"text": "c"}]}, "a\nb\nc"),
        # One part, and parts that carry no text (an image), still read.
        ({"parts": [{"text": "only part"}]}, "only part"),
        (
            {
                "parts": [
                    {"inline_data": {"mime_type": "image/png"}},
                    {"text": "after the image"},
                ]
            },
            "after the image",
        ),
        # A logged summary or plain prompt still wins over the parts.
        (
            {
                "text_summary": "summary",
                "parts": [{"text": "a"}, {"text": "b"}],
            },
            "summary",
        ),
        ({"prompt": "plain", "parts": [{"text": "a"}, {"text": "b"}]}, "plain"),
        ({"text_summary": "", "prompt": "plain"}, "plain"),
        ({"text_summary": "", "parts": [{"text": "a"}, {"text": "b"}]}, "a\nb"),
        # Nothing to show.
        ({"parts": []}, None),
        ({"parts": [{"text": ""}]}, None),
        ({}, None),
    ],
)
def test_prompt_text_is_every_part_not_just_the_first(payload, expected):
  assert _user_prompt_text(payload) == expected


def test_a_prompt_search_finds_text_in_a_later_part_of_the_prompt():
  prompt = _user_prompt_text(
      {"parts": [{"text": "Show orders"}, {"text": "placed in EMEA"}]}
  )
  # The explorer matches @prompt_search against this very assembled text.
  assert prompt is not None and "placed in emea" in prompt.lower()
  assert (
      "STRPOS(LOWER(IFNULL(user_prompt, '')), LOWER(@prompt_search))"
      in _sql("turns")
  )


def _cte_body(sql: str, name: str) -> str:
  """Returns the squashed text inside ``name AS (...)`` of a generated query."""
  text = _squash(sql)
  head = re.search(rf"(?<![\w]){re.escape(name)} AS \(", text)
  assert head, f"the query has no {name} CTE"
  depth, in_literal = 1, False
  for end in range(head.end(), len(text)):
    char = text[end]
    if char == "'":
      in_literal = not in_literal
    elif in_literal:
      continue
    elif char == "(":
      depth += 1
    elif char == ")":
      depth -= 1
      if depth == 0:
        return text[head.end() : end]
  raise AssertionError(f"the {name} CTE never closes")


def test_kpi_latency_percentiles_take_one_sample_per_completed_turn():
  sql = _squash(_sql("kpis"))
  per_turn = _cte_body(sql, "completion_per_turn")
  # One row per turn, made of its completion rows only: a turn whose completion
  # was logged more than once must not weigh more than once in a percentile.
  assert "FROM scoped" in per_turn
  assert "event_type = 'INVOCATION_COMPLETED'" in per_turn
  assert "invocation_id IS NOT NULL" in per_turn
  assert "GROUP BY invocation_id" in per_turn
  assert "MAX(turn_latency_ms) AS turn_latency_ms" in per_turn
  for offset in (50, 95):
    assert (
        f"(SELECT APPROX_QUANTILES(turn_latency_ms, 100)[SAFE_OFFSET({offset})]"
        " FROM completion_per_turn)"
    ) in sql
  # No percentile is left reading the raw, repeatable rows.
  assert sql.count("APPROX_QUANTILES(") == 2
  # Every other tile stays a distinct-turn or per-event count over scoped rows.
  assert "COUNT(DISTINCT invocation_id) AS total_turns" in sql
  assert "COUNTIF(is_error) AS error_events" in sql


def test_latency_panel_turn_percentiles_take_one_sample_per_completed_turn():
  sql = _squash(_sql("latency"))
  per_turn = _cte_body(sql, "completion_per_turn")
  assert "FROM scoped" in per_turn
  assert "event_type = 'INVOCATION_COMPLETED'" in per_turn
  assert "invocation_id IS NOT NULL" in per_turn
  assert "GROUP BY invocation_id" in per_turn
  assert "MAX(total_latency_ms) AS turn_ms" in per_turn
  # A turn lands in exactly one bucket and one path however many rows ended it.
  assert "MIN(bucket) AS bucket" in per_turn
  assert "LOGICAL_OR(fast_path)" in per_turn
  assert "FROM completion_per_turn" in sql
  grouped = _cte_body(sql, "grouped")
  for offset in (50, 95, 99):
    assert f"APPROX_QUANTILES(turn_ms, 100)[SAFE_OFFSET({offset})]" in grouped
  # The raw completion rows no longer feed any percentile ...
  assert (
      "IF(event_type = 'INVOCATION_COMPLETED', total_latency_ms, NULL)"
      not in sql
  )
  # ... while LLM calls and first-token times stay one sample per event.
  for column in ("llm_ms", "tfft_ms"):
    for offset in (50, 95):
      fragment = f"APPROX_QUANTILES({column}, 100)[SAFE_OFFSET({offset})]"
      assert fragment in grouped, fragment
  assert "IF(event_type = 'LLM_RESPONSE', total_latency_ms, NULL)" in sql
  assert "IF(event_type = 'LLM_RESPONSE', tfft_ms, NULL)" in sql


@pytest.mark.parametrize("panel", ["data_agents", "personas"])
def test_breakdown_latency_is_read_from_the_one_row_per_turn_relation(panel):
  # Already correct before the review: pinned so it stays that way.
  sql = _squash(_sql(panel))
  per_turn = _cte_body(sql, "per_turn")
  assert "GROUP BY invocation_id" in per_turn
  assert "MAX(turn_latency_ms) AS turn_latency_ms" in per_turn
  assert "FROM per_turn" in sql.split(per_turn, 1)[1]
  assert "FROM scoped" not in sql.split(per_turn, 1)[1]


def test_agent_fast_path_rate_covers_completed_turns_like_the_kpi_tile():
  sql = _squash(_sql("data_agents"))
  per_turn = _cte_body(sql, "per_turn")
  # Completion is the INVOCATION_COMPLETED event itself, never inferred from a
  # latency being present.
  assert (
      "LOGICAL_OR(event_type = 'INVOCATION_COMPLETED') AS is_completed"
      in per_turn
  )
  assert (
      "LOGICAL_OR(event_type = 'INVOCATION_COMPLETED' AND fast_path)"
      " AS is_completed_fast_path"
  ) in per_turn
  assert (
      "SAFE_DIVIDE(COUNTIF(is_completed_fast_path), COUNTIF(is_completed))"
      " AS fast_path_rate"
  ) in sql
  # Turns that never completed are in neither term of the rate.
  assert "COUNTIF(fast_path)" not in sql
  assert "COUNTIF(is_completed_fast_path), COUNT(*)" not in sql
  # The KPI tile counts the very same completion events, so the two agree.
  kpis = _squash(_sql("kpis"))
  assert (
      "COUNT(DISTINCT IF(event_type = 'INVOCATION_COMPLETED' AND fast_path,"
      " invocation_id, NULL))"
  ) in kpis
  assert (
      "COUNT(DISTINCT IF(event_type = 'INVOCATION_COMPLETED', invocation_id,"
      " NULL))"
  ) in kpis


@uses_chart_state
def test_an_agent_without_a_completed_turn_shows_a_dash_not_a_zero_rate():
  frame = _agents_frame()
  # SAFE_DIVIDE over zero completed turns is NULL, which pandas reads as NaN.
  frame.loc[frame["data_agent_id"] == "DA2", "fast_path_rate"] = NAN
  fig = bqca_charts.data_agent_leaderboard_chart(frame, models.LIGHT_THEME)
  hover = {text.split("<br>")[0]: text for text in fig.data[0].hovertext}
  assert "Fast-path rate: —" in hover["DA2"]
  assert "Fast-path rate: 50.0%" in hover["DA1"]


# --------------------------------------------------------------------------- #
# Review fixes, rounds 2 and 3: blank turn ids, attribution filters that keep #
# whole turns, the string-MAX fallback bias, persona precedence               #
# --------------------------------------------------------------------------- #

# What every scoped panel reads per row, before ``events`` resolves it across
# the turn. ``events_raw`` is the only stage that touches the table. The persona
# is read as its two separate tiers, the explicit label and the email handle of
# ``user_id``; the data agent is the third. ``session_id`` and
# ``conversation_id`` are also resolved across the turn so ``@session_search``
# keeps whole turns when only one event of the turn carries them.
RAW_TURN_COLUMNS = {
    "raw_session_id": bqca_queries.SESSION_ID_EXPR,
    "raw_conversation_id": bqca_queries.CONVERSATION_ID_EXPR,
    "raw_data_agent_id": bqca_queries.DATA_AGENT_ID_EXPR,
    "raw_explicit_persona": bqca_queries.RAW_EXPLICIT_PERSONA_EXPR,
    "raw_email_persona": bqca_queries.RAW_EMAIL_PERSONA_EXPR,
    "raw_fast_path": bqca_queries.FAST_PATH_EXPR,
}
# What ``events`` resolves across the turn from the tiers above: each resolved
# column, and the per-row column it is the first real value of.
FIRST_TIERS = {
    "session_id": "raw_session_id",
    "conversation_id": "raw_conversation_id",
    "first_data_agent_id": "raw_data_agent_id",
    "first_explicit_persona": "raw_explicit_persona",
    "first_email_persona": "raw_email_persona",
}
# The partition every turn-resolving window runs over, spelled out so that
# editing it in the query breaks a test: the turn key, except that an event
# with no ``invocation_id`` at all gets a key of its own that includes all of
# its per-row turn dimensions and error verdict.
SPEC_TURN_KEY = (
    "IF(invocation_id IS NULL, CONCAT('__null_inv_', CAST(timestamp AS"
    " STRING), '_', IFNULL(span_id, ''), '_', event_type, '_',"
    " IFNULL(raw_session_id, ''), '_', IFNULL(raw_conversation_id, ''), '_',"
    " IFNULL(raw_data_agent_id, ''), '_', IFNULL(raw_explicit_persona, ''),"
    " '_', IFNULL(raw_email_persona, ''), '_',"
    " IFNULL(CAST(raw_fast_path AS STRING), 'false'), '_',"
    " IFNULL(CAST(is_error AS STRING), 'false')), invocation_id)"
)
ERRORS_ONLY = dataclasses.replace(STATE, errors_only=True)


def test_a_blank_invocation_id_is_a_turn_of_its_own_keyed_by_its_timestamp():
  # NULL stays NULL (no turn at all). A blank or whitespace-only id falls back
  # to trimmed ``trace_id``, else trimmed ``session_id``, else the text of the
  # event's timestamp: a non-NULL key that every turn-grain panel keeps and the
  # timeline can open. A padded id is trimmed.
  assert bqca_queries.INVOCATION_ID_EXPR == (
      "IF(invocation_id IS NULL, NULL,"
      " COALESCE(NULLIF(TRIM(invocation_id), ''),"
      " NULLIF(TRIM(trace_id), ''),"
      " NULLIF(TRIM(session_id), ''),"
      " CAST(timestamp AS STRING)))"
  )
  for panel in SCOPED_PANELS:
    sql = _sql(panel)
    # The raw column is read in exactly one place and always normalized, so
    # every later ``IS NOT NULL`` and ``COUNT(DISTINCT ...)`` sees the key.
    raw = _cte_body(sql, "events_raw")
    assert f"{bqca_queries.INVOCATION_ID_EXPR} AS invocation_id" in raw, panel
    assert sql.count("TRIM(invocation_id)") == 1, panel


@pytest.mark.parametrize(
    ("panel", "relation"),
    [
        ("kpis", "completion_per_turn"),
        ("turn_volume", "per_turn"),
        ("latency", "completion_per_turn"),
        ("data_agents", "per_turn"),
        ("personas", "per_turn"),
        ("turns", "per_turn"),
    ],
)
def test_every_per_turn_relation_drops_rows_that_belong_to_no_turn(
    panel, relation
):
  body = _cte_body(_sql(panel), relation)
  assert "FROM scoped" in body
  assert re.search(r"(?:WHERE|AND) invocation_id IS NOT NULL", body), body
  assert "GROUP BY invocation_id" in body


@pytest.mark.parametrize("panel", SCOPED_PANELS)
def test_attribution_is_resolved_across_the_turn_before_any_filter_runs(panel):
  sql = _sql(panel)
  raw, events, scoped = (
      _cte_body(sql, name) for name in ("events_raw", "events", "scoped")
  )
  # The table is read once, by the first stage; the others only read CTEs.
  assert TABLE in raw
  assert sql.count(TABLE) == 1
  assert "FROM events_raw" in events
  assert "FROM events" in scoped
  # Stage 1 only *reads* each per-row value, with the canonical expression.
  for alias, expression in RAW_TURN_COLUMNS.items():
    assert f"{expression} AS {alias}" in raw, alias
  # Stage 2 resolves every tier across the turn and hides the per-row columns.
  assert (
      "* EXCEPT (raw_session_id, raw_conversation_id, raw_data_agent_id,"
      " raw_explicit_persona, raw_email_persona, raw_fast_path)"
  ) in events
  for alias, column in FIRST_TIERS.items():
    assert (
        f"FIRST_VALUE({column} IGNORE NULLS)"
        f" OVER (PARTITION BY {SPEC_TURN_KEY}"
        f" ORDER BY timestamp ASC, event_type ASC, {column} ASC"
        " ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING)"
        f" AS {alias}"
    ) in events, alias
  assert (
      f"LOGICAL_OR(raw_fast_path) OVER (PARTITION BY {SPEC_TURN_KEY})"
      " AS fast_path"
  ) in events
  # Only then does it choose between the tiers, and name the fast path.
  assert (
      "COALESCE(first_data_agent_id, 'unattributed') AS data_agent_id"
  ) in events
  assert (
      "COALESCE(first_explicit_persona, first_email_persona,"
      " first_data_agent_id, 'unattributed') AS persona"
  ) in events
  assert (
      "* EXCEPT (first_data_agent_id, first_explicit_persona,"
      " first_email_persona)"
  ) in events
  assert (
      "IF(fast_path, 'fast_path', 'standard_nl2sql') AS fast_path_label"
      in events
  )
  # Stage 3 filters on the resolved columns only.
  assert "raw_" not in scoped
  for predicate in (
      "data_agent_id IN UNNEST(@data_agent_ids)",
      "persona IN UNNEST(@personas)",
      "fast_path_label IN UNNEST(@fast_path_labels)",
      "STRPOS(LOWER(IFNULL(session_id, '')), LOWER(@session_search)) > 0",
      "STRPOS(LOWER(IFNULL(conversation_id, '')), LOWER(@session_search)) > 0",
  ):
    assert predicate in scoped, predicate


@pytest.mark.parametrize("panel", SCOPED_PANELS)
def test_rows_without_a_turn_are_judged_on_their_own(panel):
  # An event with no ``invocation_id`` at all (NULL) would otherwise fall into
  # one shared NULL partition and inherit the attribution or the verdict of
  # unrelated rows. It gets a partition key of its own instead, and every
  # window runs over that key.
  assert bqca_queries._TURN_PARTITION_KEY == SPEC_TURN_KEY
  events = _cte_body(_sql(panel, ERRORS_ONLY), "events")
  # Five first-real-value windows, the fast-path "any" and the error "any".
  assert events.count("PARTITION BY") == 7
  assert events.count(f"PARTITION BY {SPEC_TURN_KEY}") == 7
  assert "PARTITION BY invocation_id" not in events


@pytest.mark.parametrize("panel", SCOPED_PANELS)
def test_a_persona_is_the_first_tier_the_turn_carries_in_every_panel(panel):
  # Label, then the email handle of ``user_id``, then the data agent, then the
  # fallback: each tier a per-row expression that is NULL when the row does not
  # carry it, so no tier can hide behind a fallback label.
  assert "custom_labels.persona" in bqca_queries.RAW_EXPLICIT_PERSONA_EXPR
  assert "REGEXP_EXTRACT(TRIM(user_id)" in bqca_queries.RAW_EMAIL_PERSONA_EXPR
  assert "'unattributed'" not in bqca_queries.RAW_EXPLICIT_PERSONA_EXPR
  assert "'unattributed'" not in bqca_queries.RAW_EMAIL_PERSONA_EXPR
  assert "'unattributed'" not in bqca_queries.DATA_AGENT_ID_EXPR
  events = _cte_body(_sql(panel), "events")
  chain = _coalesce_chain(events, "persona")
  assert chain == [
      "first_explicit_persona",
      "first_email_persona",
      "first_data_agent_id",
      "'unattributed'",
  ]


FAST_PATH_SPELLINGS = [
    ("true", True),
    ("True", True),
    ("TRUE", True),
    ("tRuE", True),
    (True, True),
    ("false", False),
    ("FALSE", False),
    ("yes", False),
    ("1", False),
    ("", False),
    (None, False),
    (["true"], False),
]


def _fast_path_flag(payload: dict[str, Any]) -> bool:
  """Evaluates ``FAST_PATH_EXPR`` over an ``attributes`` payload."""
  match = re.fullmatch(
      r"IFNULL\((?P<value>LOWER\()?JSON_VALUE\(attributes, '\$\.(?P<key>\w+)'\)"
      r"\)? = '(?P<wanted>\w+)', FALSE\)",
      bqca_queries.FAST_PATH_EXPR,
  )
  assert match, bqca_queries.FAST_PATH_EXPR
  # Only a case-insensitive comparison reads every spelling of "true".
  assert match["value"], bqca_queries.FAST_PATH_EXPR
  text = _json_value(payload, match["key"])
  return text is not None and text.lower() == match["wanted"]


@pytest.mark.parametrize(("logged", "tagged"), FAST_PATH_SPELLINGS)
def test_the_fast_path_tag_is_read_case_insensitively(logged, tagged):
  assert _fast_path_flag({"fast_path": logged}) is tagged


def test_an_event_without_a_fast_path_attribute_is_not_tagged():
  assert _fast_path_flag({}) is False
  assert _fast_path_flag({"unrelated": "value"}) is False


@pytest.mark.parametrize("panel", ALL_PANELS)
def test_every_panel_reads_the_fast_path_tag_the_same_way(panel):
  assert bqca_queries.FAST_PATH_EXPR == (
      "IFNULL(LOWER(JSON_VALUE(attributes, '$.fast_path')) = 'true', FALSE)"
  )
  # Not only the turn-grain panels: the sidebar's own queries and the timeline
  # read the attribute too, and a spelling one of them misses is a spelling
  # none of them may count.
  assert bqca_queries.FAST_PATH_EXPR in _sql(panel), panel
  assert "JSON_VALUE(attributes, '$.fast_path') = 'true'" not in _sql(panel)


def test_the_models_comment_says_which_event_carries_the_fast_path_tag():
  source = (DASHBOARDS_DIR / "bqca_models.py").read_text(encoding="utf-8")
  lines = source.splitlines()
  declared = lines.index('FAST_PATH_ALL = "All"')
  comment = []
  for line in reversed(lines[:declared]):
    if not line.startswith("#"):
      break
    comment.append(line.lstrip("# ").rstrip())
  text = " ".join(reversed(comment))
  # The producer does not stamp every event of a fast-path turn ...
  assert "tags every event" not in text
  # ... it tags the response, and the query layer spreads that over the turn.
  assert "AGENT_RESPONSE" in text
  assert "LOGICAL_OR" in text


def _normalize_invocation_id(
    logged: str | None,
    timestamp: Any,
    trace_id: str | None = None,
    session_id: str | None = None,
) -> str | None:
  """What ``INVOCATION_ID_EXPR`` makes of a logged ``invocation_id``."""
  assert bqca_queries.INVOCATION_ID_EXPR == (
      "IF(invocation_id IS NULL, NULL,"
      " COALESCE(NULLIF(TRIM(invocation_id), ''),"
      " NULLIF(TRIM(trace_id), ''),"
      " NULLIF(TRIM(session_id), ''),"
      " CAST(timestamp AS STRING)))"
  )
  if logged is None:
    return None
  for candidate in (logged, trace_id, session_id):
    if candidate is not None and candidate.strip():
      return candidate.strip()
  return str(timestamp)


def _asc(value: Any) -> tuple[bool, Any]:
  """``ORDER BY ... ASC`` in BigQuery: NULL sorts first."""
  return (value is not None, "" if value is None else value)


def _window_rule(events: str, column: str, alias: str) -> dict[str, Any]:
  """Reads a first-real-value window expression back out of ``events``."""
  match = re.search(
      rf"FIRST_VALUE\({column} IGNORE NULLS\)"
      r" OVER \(PARTITION BY (?P<partition>.+?)"
      r" ORDER BY (?P<order>[\w ,]+)"
      r" ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING\)"
      rf" AS {alias}\b",
      events,
  )
  assert match, f"{alias} is no longer a first-real-value window: {events}"
  keys = [key.split() for key in match["order"].split(",")]
  assert all(direction == "ASC" for _, direction in keys), keys
  return {
      "partition": match["partition"],
      "order": [name for name, _ in keys],
  }


def _coalesce_chain(events: str, alias: str) -> list[str]:
  """Reads the terms of ``COALESCE(...) AS alias`` back out of ``events``."""
  match = re.search(rf"COALESCE\((?P<args>[^()]+)\) AS {alias}\b", events)
  assert match, f"{alias} is no longer a COALESCE chain: {events}"
  return [term.strip() for term in match["args"].split(",")]


def _resolve_attribution(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
  """Applies the turn-resolving ``events`` stage to synthetic raw rows.

  Each window expression is read back out of the generated SQL (the partition,
  the sort keys, the order of the tiers a result is chosen from, the label a
  turn with nothing real gets) and applied the way BigQuery applies it, so
  editing the SQL changes the answer here exactly as it changes it there.
  """
  events = _cte_body(_sql("kpis", ERRORS_ONLY), "events")
  windows = {
      alias: _window_rule(events, column, alias)
      for alias, column in FIRST_TIERS.items()
  }
  for rule in windows.values():
    assert rule["partition"] == SPEC_TURN_KEY
  agent_chain = _coalesce_chain(events, "data_agent_id")
  persona_chain = _coalesce_chain(events, "persona")
  # An unrecognized shape fails loudly instead of being guessed at.
  assert (
      f"LOGICAL_OR(raw_fast_path) OVER (PARTITION BY {SPEC_TURN_KEY})"
      " AS fast_path"
  ) in events
  assert (
      f"LOGICAL_OR(is_error) OVER (PARTITION BY {SPEC_TURN_KEY})"
      " AS turn_has_error"
  ) in events
  label = re.search(
      r"IF\(fast_path, '(?P<yes>\w+)', '(?P<no>\w+)'\) AS fast_path_label",
      events,
  )
  assert label, events
  assert (label["yes"], label["no"]) == bqca_models.FAST_PATH_LABELS

  normalized = [
      {
          **row,
          "invocation_id": _normalize_invocation_id(
              row["invocation_id"],
              row["timestamp"],
              row.get("trace_id"),
              row.get("raw_session_id"),
          ),
      }
      for row in rows
  ]

  def partition(row: dict[str, Any]) -> Any:
    # An event with no id at all is partitioned by all of its per-row turn
    # dimensions and error verdict so unrelated orphan rows cannot collide.
    if row["invocation_id"] is not None:
      return row["invocation_id"]
    return (
        "no-turn",
        row["timestamp"],
        row["span_id"] or "",
        row["event_type"],
        row["raw_session_id"] or "",
        row["raw_conversation_id"] or "",
        row["raw_data_agent_id"] or "",
        row["raw_explicit_persona"] or "",
        row["raw_email_persona"] or "",
        bool(row["raw_fast_path"]),
        bool(row["is_error"]),
    )

  turns: dict[Any, list[dict[str, Any]]] = {}
  for row in normalized:
    turns.setdefault(partition(row), []).append(row)

  def first_real(row: dict[str, Any], alias: str, column: str) -> str | None:
    ordered = sorted(
        turns[partition(row)],
        key=lambda other: tuple(
            _asc(other[key]) for key in windows[alias]["order"]
        ),
    )
    return next(
        (other[column] for other in ordered if other[column] is not None),
        None,
    )

  def choose(chain: list[str], firsts: dict[str, str | None]) -> str:
    for term in chain:
      if term.startswith("'"):
        return term.strip("'")
      if firsts[term] is not None:
        return firsts[term]
    raise AssertionError(f"no default at the end of the chain: {chain}")

  resolved = []
  for row in normalized:
    turn = turns[partition(row)]
    firsts = {
        alias: first_real(row, alias, column)
        for alias, column in FIRST_TIERS.items()
    }
    fast_path = any(other["raw_fast_path"] for other in turn)
    resolved.append(
        {
            **row,
            "session_id": firsts["session_id"],
            "conversation_id": firsts["conversation_id"],
            "data_agent_id": choose(agent_chain, firsts),
            "persona": choose(persona_chain, firsts),
            "fast_path": fast_path,
            "fast_path_label": label["yes"] if fast_path else label["no"],
            "turn_has_error": any(other["is_error"] for other in turn),
        }
    )
  return resolved


_SPAN_IDS = itertools.count(1)


def _event(
    second: int,
    event_type: str,
    *,
    turn: str | None = "inv-1",
    trace: str | None = None,
    session: str | None = None,
    conversation: str | None = None,
    span: str | None = "auto",
    persona: str | None = None,
    email: str | None = None,
    agent: str | None = None,
    fast: bool = False,
    error: bool = False,
) -> dict[str, Any]:
  """One ``events_raw`` row.

  ``persona`` is the persona custom label, ``email`` the handle taken from
  ``user_id`` and ``agent`` the data agent. Each is NULL when the row does not
  carry it, as in the query. Every event has a span of its own unless ``span``
  is explicitly passed (including ``None`` for a NULL ``span_id``).

  Args:
    second: The event's timestamp.
    event_type: The event type.
    turn: The logged ``invocation_id`` (``None``: no id at all).
    trace: The logged ``trace_id``, if any.
    session: The logged ``session_id``, if any.
    conversation: The logged ``conversation-id`` attribute, if any.
    span: Explicit ``span_id`` (``None`` for NULL, ``"auto"`` for unique).
    persona: The persona custom label the row carries, if any.
    email: The email handle of the row's ``user_id``, if any.
    agent: The data agent the row names, if any.
    fast: Whether the row is tagged as a fast-path event.
    error: Whether the row is an error event.

  Returns:
    The synthetic row.
  """
  clean_session = (
      session.strip() if session is not None and session.strip() else None
  )
  clean_conv = (
      conversation.strip()
      if conversation is not None and conversation.strip()
      else None
  )
  return {
      "timestamp": second,
      "event_type": event_type,
      "span_id": f"span-{next(_SPAN_IDS)}" if span == "auto" else span,
      "invocation_id": turn,
      "trace_id": trace,
      "raw_session_id": clean_session,
      "raw_conversation_id": clean_conv,
      "raw_explicit_persona": persona,
      "raw_email_persona": email,
      "raw_data_agent_id": agent,
      "raw_fast_path": fast,
      "is_error": error,
  }


TURN_EVENT_TYPES = [
    "USER_MESSAGE_RECEIVED",
    "LLM_RESPONSE",
    "INVOCATION_COMPLETED",
]


def test_a_persona_filter_keeps_every_event_of_the_turns_it_matches():
  rows = [
      _event(0, "USER_MESSAGE_RECEIVED", persona="analyst"),
      _event(1, "LLM_RESPONSE"),
      _event(2, "INVOCATION_COMPLETED"),
      _event(0, "USER_MESSAGE_RECEIVED", turn="inv-2", persona="bob"),
      _event(1, "INVOCATION_COMPLETED", turn="inv-2"),
  ]
  resolved = _resolve_attribution(rows)
  assert [r["persona"] for r in resolved] == ["analyst"] * 3 + ["bob"] * 2
  # So ``persona IN UNNEST(@personas)`` keeps all three events of inv-1, its
  # completion included, and none of inv-2's ...
  kept = [r["event_type"] for r in resolved if r["persona"] in {"analyst"}]
  assert kept == TURN_EVENT_TYPES
  # ... where the per-row value kept the prompt alone and made the turn look
  # unfinished.
  per_row = [
      r["event_type"] for r in rows if r["raw_explicit_persona"] == "analyst"
  ]
  assert per_row == ["USER_MESSAGE_RECEIVED"]


def test_a_data_agent_filter_keeps_every_event_of_the_turns_it_matches():
  agent = "ops-agent-東京"
  rows = [
      _event(0, "USER_MESSAGE_RECEIVED", agent=agent),
      _event(1, "LLM_RESPONSE"),
      _event(2, "INVOCATION_COMPLETED"),
      # A turn that never names an agent stays unattributed on every row.
      _event(0, "USER_MESSAGE_RECEIVED", turn="inv-2"),
      _event(1, "INVOCATION_COMPLETED", turn="inv-2"),
  ]
  resolved = _resolve_attribution(rows)
  assert [r["data_agent_id"] for r in resolved] == (
      [agent] * 3 + ["unattributed"] * 2
  )
  kept = [r["event_type"] for r in resolved if r["data_agent_id"] == agent]
  assert kept == TURN_EVENT_TYPES
  assert [r["event_type"] for r in rows if r["raw_data_agent_id"] == agent] == [
      "USER_MESSAGE_RECEIVED"
  ]


def test_a_fast_path_filter_keeps_every_event_of_the_turns_it_matches():
  rows = [
      _event(0, "USER_MESSAGE_RECEIVED", fast=True),
      _event(1, "INVOCATION_COMPLETED"),  # not tagged by the plugin
      _event(0, "USER_MESSAGE_RECEIVED", turn="inv-2"),
      _event(1, "INVOCATION_COMPLETED", turn="inv-2"),
  ]
  resolved = _resolve_attribution(rows)
  assert [r["fast_path"] for r in resolved] == [True, True, False, False]
  assert [r["fast_path_label"] for r in resolved] == (
      [bqca_models.FAST_PATH_LABEL] * 2 + [bqca_models.STANDARD_LABEL] * 2
  )


def test_a_session_or_conversation_filter_keeps_every_event_of_the_turns_it_matches():
  rows = [
      # Only INVOCATION_STARTING carries session_id and conversation-id;
      # later events of the turn omit both.
      _event(
          0,
          "INVOCATION_STARTING",
          turn="inv-partial",
          session="sess-only-on-start",
          conversation="conv-only-on-start",
      ),
      _event(1, "LLM_RESPONSE", turn="inv-partial"),
      _event(2, "INVOCATION_COMPLETED", turn="inv-partial"),
      _event(
          0,
          "INVOCATION_STARTING",
          turn="inv-other",
          session="sess-other",
          conversation="conv-other",
      ),
  ]
  resolved = _resolve_attribution(rows)
  assert [r["session_id"] for r in resolved] == (
      ["sess-only-on-start"] * 3 + ["sess-other"]
  )
  assert [r["conversation_id"] for r in resolved] == (
      ["conv-only-on-start"] * 3 + ["conv-other"]
  )
  # Filtering by either session_id or conversation_id keeps the entire turn,
  # including its INVOCATION_COMPLETED event.
  by_conv = [
      r["event_type"]
      for r in resolved
      if "conv-only-on-start" in (r["conversation_id"] or "")
  ]
  by_sess = [
      r["event_type"]
      for r in resolved
      if "sess-only-on-start" in (r["session_id"] or "")
  ]
  assert by_conv == [
      "INVOCATION_STARTING",
      "LLM_RESPONSE",
      "INVOCATION_COMPLETED",
  ]
  assert by_sess == by_conv


def test_the_first_real_attribution_wins_whatever_order_rows_arrive_in():
  rows = [
      _event(0, "INVOCATION_STARTING", persona="first", agent="DA-first"),
      _event(1, "LLM_RESPONSE", persona="second", agent="DA-second"),
      _event(2, "INVOCATION_COMPLETED"),
      # Two rows share a timestamp and an event type: the value itself breaks
      # the tie, so the answer never depends on which row came first.
      _event(0, "LLM_RESPONSE", turn="inv-2", persona="zeta", agent="DA-z"),
      _event(0, "LLM_RESPONSE", turn="inv-2", persona="alpha", agent="DA-a"),
  ]
  expected = [("first", "DA-first")] * 3 + [("alpha", "DA-a")] * 2
  for ordering in (rows, rows[::-1], rows[2:] + rows[:2], rows[3:] + rows[:3]):
    resolved = _resolve_attribution(list(ordering))
    got = {
        id(row): (r["persona"], r["data_agent_id"])
        for row, r in zip(ordering, resolved)
    }
    assert [got[id(row)] for row in rows] == expected


def test_rows_with_no_invocation_id_keep_their_own_attribution_and_verdict():
  rows = [
      _event(0, "LLM_ERROR", turn=None, persona="alice", error=True),
      _event(1, "LLM_RESPONSE", turn=None, email="bob"),
      _event(2, "AGENT_RESPONSE", turn=None, fast=True),
      _event(3, "LLM_RESPONSE", turn=None),
      # Same timestamp and type, different spans: two rows, not one turn.
      _event(3, "LLM_RESPONSE", turn=None, agent="DA-9"),
      _event(0, "USER_MESSAGE_RECEIVED", persona="carol"),
      _event(1, "AGENT_ERROR", error=True),
      _event(2, "INVOCATION_COMPLETED"),
  ]
  resolved = _resolve_attribution(rows)
  orphans, turn = resolved[:5], resolved[5:]
  assert [r["invocation_id"] for r in orphans] == [None] * 5
  # Nothing is shared between unrelated rows ...
  assert [r["persona"] for r in orphans] == [
      "alice",
      "bob",
      "unattributed",
      "unattributed",
      "DA-9",
  ]
  assert [r["data_agent_id"] for r in orphans] == (
      ["unattributed"] * 4 + ["DA-9"]
  )
  assert [r["fast_path"] for r in orphans] == [False, False, True, False, False]
  # ... and an error among them does not make the others "turns with an error".
  assert [r["turn_has_error"] for r in orphans] == [
      True,
      False,
      False,
      False,
      False,
  ]
  # A real turn still shares everything across its rows.
  assert [r["persona"] for r in turn] == ["carol"] * 3
  assert [r["turn_has_error"] for r in turn] == [True] * 3


def test_null_id_orphan_rows_never_collide_even_with_identical_timestamp_and_null_span():
  # Two unrelated orphan events share timestamp=100, span_id=None, and
  # event_type='LLM_RESPONSE', but differ in persona, data_agent_id, fast_path,
  # and error status. Neither may overwrite the other.
  rows = [
      _event(
          100,
          "LLM_RESPONSE",
          turn=None,
          span=None,
          persona="orphan_alice",
          agent="agent_orphan_1",
          fast=True,
          error=False,
      ),
      _event(
          100,
          "LLM_RESPONSE",
          turn=None,
          span=None,
          persona="orphan_bob",
          agent="agent_orphan_2",
          fast=False,
          error=True,
      ),
  ]
  resolved = _resolve_attribution(rows)
  assert [r["invocation_id"] for r in resolved] == [None, None]
  assert [r["persona"] for r in resolved] == ["orphan_alice", "orphan_bob"]
  assert [r["data_agent_id"] for r in resolved] == [
      "agent_orphan_1",
      "agent_orphan_2",
  ]
  assert [r["fast_path"] for r in resolved] == [True, False]
  assert [r["turn_has_error"] for r in resolved] == [False, True]


def test_rows_with_a_blank_invocation_id_are_turns_keyed_by_their_timestamp():
  rows = [
      _event(10, "USER_MESSAGE_RECEIVED", turn="", persona="dana"),
      _event(10, "INVOCATION_COMPLETED", turn="   ", agent="DA-1"),
      _event(
          20, "USER_MESSAGE_RECEIVED", turn="\t", persona="erin", error=True
      ),
      _event(0, "USER_MESSAGE_RECEIVED", turn=None, persona="orphan"),
  ]
  resolved = _resolve_attribution(rows)
  # A blank id is a turn, not a missing one: events logged together share the
  # key of their timestamp, events logged at another time do not, and the row
  # with no id at all still belongs to no turn.
  assert [r["invocation_id"] for r in resolved] == ["10", "10", "20", None]
  assert [r["persona"] for r in resolved] == ["dana", "dana", "erin", "orphan"]
  assert [r["data_agent_id"] for r in resolved] == [
      "DA-1",
      "DA-1",
      "unattributed",
      "unattributed",
  ]
  assert [r["turn_has_error"] for r in resolved] == [False, False, True, False]


def test_a_blank_invocation_id_falls_back_to_trace_then_session_then_timestamp():
  # Multi-event turns logged with a blank or whitespace invocation_id across
  # different timestamps stay grouped as one turn when they share a trace_id
  # or session_id.
  rows = [
      _event(
          10,
          "INVOCATION_STARTING",
          turn="",
          trace="  tr-blank-1 ",
          session="sess-blank-1",
          persona="alice",
          agent="DA-trace",
      ),
      _event(
          12,
          "INVOCATION_COMPLETED",
          turn="   ",
          trace="tr-blank-1",
          session="sess-blank-1",
      ),
      _event(
          20,
          "USER_MESSAGE_RECEIVED",
          turn="",
          trace="  ",
          session=" sess-only-2 ",
          persona="bob",
      ),
      _event(
          25,
          "INVOCATION_COMPLETED",
          turn="\t",
          trace=None,
          session="sess-only-2",
      ),
  ]
  resolved = _resolve_attribution(rows)
  assert [r["invocation_id"] for r in resolved] == [
      "tr-blank-1",
      "tr-blank-1",
      "sess-only-2",
      "sess-only-2",
  ]
  assert [r["persona"] for r in resolved] == ["alice", "alice", "bob", "bob"]
  assert [r["data_agent_id"] for r in resolved] == [
      "DA-trace",
      "DA-trace",
      "unattributed",
      "unattributed",
  ]


def test_a_scope_shaped_like_the_live_fixture_counts_its_blank_id_turns():
  # Ordinary turns, turns whose events were logged with an empty id, and
  # lifecycle events with no id at all.
  rows = []
  for turn in range(4):
    base = 100 * (turn + 1)
    rows += [
        _event(base, "USER_MESSAGE_RECEIVED", turn=f"inv-{turn}"),
        _event(base + 1, "INVOCATION_COMPLETED", turn=f"inv-{turn}"),
    ]
  for second in (1000, 2000, 3000):
    rows += [
        _event(second, "USER_MESSAGE_RECEIVED", turn=""),
        _event(second, "INVOCATION_COMPLETED", turn=" "),
    ]
  rows += [
      _event(5000, "INVOCATION_STARTING", turn=None),
      _event(5000, "INVOCATION_COMPLETED", turn=None),
  ]
  resolved = _resolve_attribution(rows)
  # The per-turn relations keep ``invocation_id IS NOT NULL`` rows and count
  # distinct ids: four named turns and three blank ones, nothing more.
  keys = {r["invocation_id"] for r in resolved if r["invocation_id"]}
  assert keys == {"inv-0", "inv-1", "inv-2", "inv-3", "1000", "2000", "3000"}
  assert sum(r["invocation_id"] is None for r in resolved) == 2


def test_a_persona_is_the_first_tier_the_turn_carries_not_the_first_event():
  # The persona label beats an email handle, which beats the data agent,
  # whichever event of the turn carries which: each tier is resolved across
  # the whole turn before the tiers are compared.
  rows = [
      # A label logged last still beats an agent and a handle logged first.
      _event(0, "INVOCATION_STARTING", turn="inv-A", agent="DA-1"),
      _event(1, "USER_MESSAGE_RECEIVED", turn="inv-A", email="bob"),
      _event(2, "AGENT_RESPONSE", turn="inv-A", persona="analyst"),
      # No label: the handle beats the agent that was logged before it.
      _event(0, "INVOCATION_STARTING", turn="inv-B", agent="DA-2"),
      _event(1, "USER_MESSAGE_RECEIVED", turn="inv-B", email="carol"),
      # Only an agent: the persona falls back to it.
      _event(0, "INVOCATION_STARTING", turn="inv-C", agent="DA-3"),
      _event(1, "LLM_RESPONSE", turn="inv-C"),
      # Nothing at all.
      _event(0, "INVOCATION_STARTING", turn="inv-D"),
      # Within one tier the earliest event wins, whatever the value.
      _event(5, "USER_MESSAGE_RECEIVED", turn="inv-E", email="zed"),
      _event(4, "INVOCATION_STARTING", turn="inv-E", email="amy"),
  ]
  resolved = _resolve_attribution(rows)
  assert [(r["invocation_id"], r["persona"]) for r in resolved] == [
      *[("inv-A", "analyst")] * 3,
      *[("inv-B", "carol")] * 2,
      *[("inv-C", "DA-3")] * 2,
      ("inv-D", "unattributed"),
      *[("inv-E", "amy")] * 2,
  ]
  # The data agent column is the turn's own first agent, whichever persona won.
  assert [r["data_agent_id"] for r in resolved] == (
      ["DA-1"] * 3 + ["DA-2"] * 2 + ["DA-3"] * 2 + ["unattributed"] * 3
  )
  # So a persona filter matches the winner of a turn and never a loser: "bob"
  # was only ever a handle that lost to a label.
  matching = {r["invocation_id"] for r in resolved if r["persona"] == "bob"}
  assert matching == set()


def test_an_id_padded_with_whitespace_is_the_same_turn():
  rows = [
      _event(0, "USER_MESSAGE_RECEIVED", turn="  inv-1 ", persona="analyst"),
      _event(1, "INVOCATION_COMPLETED", turn="inv-1"),
  ]
  resolved = _resolve_attribution(rows)
  assert [r["invocation_id"] for r in resolved] == ["inv-1", "inv-1"]
  assert [r["persona"] for r in resolved] == ["analyst", "analyst"]


def test_padded_attribute_ids_and_blank_error_messages_are_trimmed_in_sql():
  for expr in (
      bqca_queries.SESSION_ID_EXPR,
      bqca_queries.DATA_AGENT_ID_EXPR,
      bqca_queries.CONVERSATION_ID_EXPR,
      bqca_queries.RAW_EXPLICIT_PERSONA_EXPR,
  ):
    assert expr.startswith("NULLIF(TRIM(") and expr.endswith(", '')"), expr
  assert (
      "STRING_AGG(DISTINCT NULLIF(TRIM(error_message), ''), ' | ')"
      in _squash(_sql("turns"))
  )
  assert "NULLIF(TRIM(error_message), '') AS error_message" in _squash(
      _sql("timeline")
  )


@pytest.mark.parametrize(
    ("panel", "columns"),
    [
        ("data_agents", ("data_agent_id",)),
        ("personas", ("persona",)),
        ("turns", ("data_agent_id", "persona")),
    ],
)
def test_a_turns_real_attribution_beats_the_fallback_label(panel, columns):
  per_turn = _cte_body(_sql(panel), "per_turn")
  for column in columns:
    # ``MAX`` over strings ranks 'unattributed' above 'analyst', so a bare MAX
    # would hide a real value behind the fallback label.
    assert (
        f"COALESCE(MAX(NULLIF({column}, 'unattributed')), 'unattributed')"
        f" AS {column}"
    ) in per_turn, column
    assert f"MAX({column})" not in per_turn, column


def test_timeline_matches_the_turn_key_the_explorer_lists():
  sql = _sql("timeline")
  assert f"AND {bqca_queries.INVOCATION_ID_EXPR} = @invocation_id" in sql
  # The raw column no longer decides: a padded id would list a turn whose
  # timeline is empty.
  assert "AND invocation_id = @invocation_id" not in sql


def test_fetch_panel_binds_the_trimmed_invocation_id():
  seen: dict[str, Any] = {}

  def fake(sql, params, ctx_, label):
    seen.update(params={p.name: p for p in params})
    return models.QueryResult(pd.DataFrame())

  with mock.patch.object(bqca_queries, "fetch_bqca", side_effect=fake):
    bqca_queries.fetch_panel(
        "timeline", STATE, _ctx(), invocation_id="  inv-1 \n"
    )
  assert seen["params"]["invocation_id"].value == "inv-1"


@pytest.mark.parametrize(
    ("panel", "columns"),
    [
        ("data_agents", ("data_agent_id",)),
        ("personas", ("persona",)),
    ],
)
def test_the_breakdown_panels_group_by_the_resolved_columns(panel, columns):
  # They aggregate ``scoped``'s turn-resolved columns: no panel recomputes an
  # attribution from the raw per-row expressions.
  sql = _sql(panel)
  scoped_onward = sql.split("scoped AS (", 1)[1]
  for expression in RAW_TURN_COLUMNS.values():
    assert expression not in scoped_onward
  for column in columns:
    assert f"GROUP BY {column}" in scoped_onward


def test_filter_options_offer_what_the_turn_resolved_filters_can_match():
  sql = _sql("filter_options")
  options_events = _cte_body(sql, "events")
  # One read of the table, through the very ``events`` stage every panel
  # filters on: what the sidebar offers is what a filter can match.
  assert sql.count(TABLE) == 1
  assert options_events == _cte_body(_sql("kpis"), "events")
  assert "FROM events_raw" in options_events
  assert "event_type IN UNNEST(@allowed_event_types)" in _cte_body(
      sql, "events_raw"
  )
  # A turn without a data agent or a persona is offered as 'unattributed',
  # which the filters match. The per-row values are not offered at all: no
  # filter could ever match a persona that only one event of a turn carries.
  assert (
      "COALESCE(first_data_agent_id, 'unattributed') AS data_agent_id"
  ) in options_events
  assert "STRUCT('data_agent_id' AS kind, e.data_agent_id AS value)" in sql
  assert "STRUCT('persona', e.persona)" in sql
  assert "STRUCT('event_type', e.event_type)" in sql
  assert "FROM events AS e" in sql
  assert "e.raw_" not in sql
  assert "WHERE o.value IS NOT NULL" in sql


def test_filter_options_ignore_every_sidebar_filter():
  every_filter = dataclasses.replace(
      STATE,
      data_agent_ids=("DA1",),
      personas=("alice",),
      event_types=("LLM_ERROR",),
      fast_path_mode=bqca_models.FAST_PATH_ONLY,
      session_search="s1",
      prompt_search="orders",
      errors_only=True,
  )
  sql = _sql("filter_options", every_filter)
  # Picking one value must not hide the others, and "errors only" must not
  # turn the lists into the lists of the failing turns.
  assert sql == _sql("filter_options")
  assert "scoped" not in sql
  assert "turn_has_error" not in sql
  assert _referenced("filter_options") == {"allowed_event_types"}


def test_filter_options_are_capped_and_ordered_per_kind():
  sql = _sql("filter_options")
  assert "GROUP BY o.kind, o.value" in sql
  assert "PARTITION BY o.kind ORDER BY MAX(e.timestamp) DESC" in sql
  assert f"<= {models.FILTER_OPTIONS_LIMIT}" in sql
  assert "ORDER BY o.kind, o.value" in sql


@pytest.mark.parametrize(
    ("span", "bucket"),
    [
        (dt.timedelta(hours=1), "MINUTE"),
        (dt.timedelta(hours=24), "HOUR"),
        (dt.timedelta(days=7), "DAY"),
    ],
)
def test_time_bucket_follows_the_window_span(span, bucket):
  window = models.Window(start=WINDOW.end - span, end=WINDOW.end)
  for panel in ("turn_volume", "latency", "token_usage", "embedding"):
    assert f", {bucket})" in _sql(panel, window=window), panel


def test_builders_resolve_the_window_from_state_when_none_is_given():
  start, end = dt.datetime(2026, 9, 1), dt.datetime(2026, 9, 4)
  state = dataclasses.replace(
      STATE, time_window="custom", custom_start=start, custom_end=end
  )
  expected = queries.time_bounds(
      models.Window(
          start=start.replace(tzinfo=UTC), end=end.replace(tzinfo=UTC)
      )
  )
  for panel in ALL_PANELS:
    assert expected in _sql(panel, state, window=None), panel


@pytest.mark.parametrize(
    ("field", "bad"),
    [
        ("project_id", ""),
        ("project_id", "   "),
        ("project_id", "my project"),
        ("project_id", "p`; DROP TABLE x"),
        ("project_id", "p;q"),
        ("project_id", "-leading"),
        ("project_id", "a" * 64),
        ("dataset_id", ""),
        ("dataset_id", "d.s"),
        ("dataset_id", "d`"),
        ("dataset_id", "ds; --"),
        ("dataset_id", "has space"),
        ("dataset_id", "ds-with-dash"),
        ("table_id", ""),
        ("table_id", "t`"),
        ("table_id", "t t"),
        ("table_id", "t;"),
        ("table_id", "t/*"),
        ("table_id", "db.tbl"),
    ],
)
def test_malformed_table_identifiers_never_reach_sql(field, bad):
  state = dataclasses.replace(STATE, **{field: bad})
  for panel in ALL_PANELS:
    with pytest.raises(ValueError, match="Invalid"):
      _sql(panel, state)


@pytest.mark.parametrize(
    "panel", ["data_agents", "personas", "turns", "errors"]
)
def test_row_limits_default_override_and_reject_nonsense(panel):
  builder, _ = bqca_queries.PANELS[panel]
  assert "LIMIT 100" in builder(STATE, window=WINDOW)
  assert "LIMIT 25" in builder(STATE, 25, window=WINDOW)
  assert "LIMIT 1\n" in builder(STATE, 1, window=WINDOW) + "\n"
  for bad in (0, -1, 1001, "abc", "5; DROP TABLE t"):
    with pytest.raises(ValueError):
      builder(STATE, bad, window=WINDOW)


def test_limit_constants_match_the_spec():
  assert bqca_models.TURN_EXPLORER_LIMIT == 100
  assert bqca_models.ERROR_ATTRIBUTION_LIMIT == 100
  assert bqca_models.BREAKDOWN_LIMIT == 100
  assert bqca_models.TIMELINE_LIMIT == 200


@pytest.mark.parametrize("empty", ["", "   ", "\t\n"])
def test_timeline_needs_an_invocation(empty):
  with pytest.raises(ValueError, match="invocation_id"):
    bqca_queries.build_bqca_turn_timeline_sql(STATE, empty, window=WINDOW)


def test_timeline_binds_the_invocation_and_never_embeds_it():
  sql = bqca_queries.build_bqca_turn_timeline_sql(
      STATE, "inv-secret'; DROP TABLE t; --", window=WINDOW
  )
  assert "inv-secret" not in sql
  assert "DROP TABLE" not in sql
  assert f"{bqca_queries.INVOCATION_ID_EXPR} = @invocation_id" in sql


@pytest.mark.parametrize(
    "key", ["2026-10-05 09:00:00+00", "2026-10-05 09:00:00.123456+00"]
)
def test_timeline_opens_a_turn_by_the_timestamp_key_made_for_an_empty_id(key):
  sql = bqca_queries.build_bqca_turn_timeline_sql(STATE, key, window=WINDOW)
  # The key is bound as data, never part of the query text ...
  assert key not in sql
  # ... and is compared with the very expression that makes the key the turn
  # table lists: an empty id is keyed by the text of its timestamp.
  assert f"AND {bqca_queries.INVOCATION_ID_EXPR} = @invocation_id" in sql
  assert "CAST(timestamp AS STRING)" in bqca_queries.INVOCATION_ID_EXPR

  seen: dict[str, Any] = {}

  def fake(sql, params, ctx_, label):
    seen.update(params={p.name: p for p in params})
    return models.QueryResult(pd.DataFrame())

  with mock.patch.object(bqca_queries, "fetch_bqca", side_effect=fake):
    bqca_queries.fetch_panel("timeline", STATE, _ctx(), invocation_id=key)
  assert seen["params"]["invocation_id"].value == key


def test_timeline_honors_only_the_event_type_selection():
  state = dataclasses.replace(
      STATE,
      data_agent_ids=("DA1",),
      personas=("alice",),
      session_search="s1",
      fast_path_mode=bqca_models.FAST_PATH_ONLY,
      event_types=("LLM_RESPONSE",),
  )
  sql = _sql("timeline", state)
  assert "@event_types" in sql
  for name in ("data_agent_ids", "personas", "session_search", "fast_path"):
    assert f"@{name}" not in sql


def test_panels_run_over_the_raw_table_not_typed_views():
  for panel in ALL_PANELS:
    sql = _sql(panel)
    assert "adk_" not in sql, panel
    assert TABLE in sql
    assert sql.count("FROM `") >= 1


# --------------------------------------------------------------------------- #
# Query parameters                                                             #
# --------------------------------------------------------------------------- #


def _params(
    state: bqca_models.BqcaFilterState, panel: str = "kpis", **scalars: str
) -> dict[str, bqca_queries.BqcaParam]:
  sql = _sql(panel, state)
  bound = bqca_queries.bqca_query_params(state, sql, **scalars)
  return {param.name: param for param in bound}


def test_params_are_exactly_the_referenced_ones_in_a_stable_order():
  sql = _sql("kpis")
  names = [p.name for p in bqca_queries.bqca_query_params(STATE, sql)]
  assert names == [
      "allowed_event_types",
      "data_agent_ids",
      "personas",
      "fast_path_labels",
      "session_search",
  ]


def test_allowlist_is_bound_from_the_constant():
  assert _params(STATE)["allowed_event_types"] == bqca_queries.BqcaParam(
      "allowed_event_types",
      "STRING",
      True,
      bqca_models.BQCA_ALLOWED_EVENT_TYPES,
  )


def test_empty_selections_bind_the_all_sentinel():
  params = _params(STATE, "errors")
  for name in ("data_agent_ids", "personas", "event_types"):
    assert params[name].value == (models.ALL_SENTINEL,), name
    assert params[name].is_array
    assert params[name].bq_type == "STRING"


def test_selections_are_deduplicated_and_blank_free():
  state = dataclasses.replace(
      STATE,
      data_agent_ids=("DA2", "", "DA1", "DA2"),
      personas=("alice", ""),
  )
  params = _params(state)
  assert params["data_agent_ids"].value == ("DA2", "DA1")
  assert params["personas"].value == ("alice",)


def test_event_type_selection_is_intersected_with_the_allowlist():
  state = dataclasses.replace(
      STATE,
      event_types=(
          "LLM_ERROR",
          FORBIDDEN_EVENT_TYPES[0],
          "made_up",
          "AGENT_RESPONSE",
      ),
  )
  assert _params(state, "errors")["event_types"].value == (
      "AGENT_RESPONSE",
      "LLM_ERROR",
  )
  only_forbidden = dataclasses.replace(STATE, event_types=FORBIDDEN_EVENT_TYPES)
  # Nothing outside the allowlist is ever bound, even when asked for.
  assert _params(only_forbidden, "errors")["event_types"].value == (
      models.ALL_SENTINEL,
  )
  for param in _params(only_forbidden, "errors").values():
    assert not set(FORBIDDEN_EVENT_TYPES) & set(
        param.value if param.is_array else (param.value,)
    )


def test_fast_path_mode_binds_its_labels_and_rejects_unknown_modes():
  expected = {
      bqca_models.FAST_PATH_ALL: ("fast_path", "standard_nl2sql"),
      bqca_models.FAST_PATH_ONLY: ("fast_path",),
      bqca_models.STANDARD_ONLY: ("standard_nl2sql",),
  }
  for mode, labels in expected.items():
    state = dataclasses.replace(STATE, fast_path_mode=mode)
    assert _params(state)["fast_path_labels"].value == labels
  with pytest.raises(ValueError, match="fast-path mode"):
    _params(dataclasses.replace(STATE, fast_path_mode="Everything"))


def test_search_text_is_stripped_and_bound_as_a_scalar():
  state = dataclasses.replace(
      STATE, session_search="  s1 ", prompt_search="\torders\n"
  )
  params = _params(state, "turns")
  assert params["session_search"] == bqca_queries.BqcaParam(
      "session_search", "STRING", False, "s1"
  )
  assert params["prompt_search"] == bqca_queries.BqcaParam(
      "prompt_search", "STRING", False, "orders"
  )


def test_extra_scalars_bind_only_when_the_sql_references_them():
  sql = _sql("timeline")
  params = {
      p.name: p
      for p in bqca_queries.bqca_query_params(
          STATE, sql, invocation_id="inv-9", unused="x"
      )
  }
  assert params["invocation_id"] == bqca_queries.BqcaParam(
      "invocation_id", "STRING", False, "inv-9"
  )
  assert "unused" not in params


def test_a_parameter_nothing_can_bind_is_an_error():
  with pytest.raises(ValueError, match="nope"):
    bqca_queries.bqca_query_params(STATE, "SELECT @nope")


def test_param_sets_are_hashable_cache_keys_that_track_the_filters():
  sql = _sql("kpis")
  first = bqca_queries.bqca_query_params(STATE, sql)
  again = bqca_queries.bqca_query_params(dataclasses.replace(STATE), sql)
  narrowed = bqca_queries.bqca_query_params(
      dataclasses.replace(STATE, data_agent_ids=("DA1",)), sql
  )
  assert first == again
  assert hash(first) == hash(again)
  assert first != narrowed


def test_params_convert_to_bigquery_parameters():
  array = bqca_queries._to_bigquery(
      bqca_queries.BqcaParam("xs", "STRING", True, ("a", "b"))
  )
  assert isinstance(array, bigquery.ArrayQueryParameter)
  assert (array.name, array.array_type, array.values) == (
      "xs",
      "STRING",
      ["a", "b"],
  )
  scalar = bqca_queries._to_bigquery(
      bqca_queries.BqcaParam("q", "STRING", False, "hello")
  )
  assert isinstance(scalar, bigquery.ScalarQueryParameter)
  assert (scalar.name, scalar.type_, scalar.value) == ("q", "STRING", "hello")


# --------------------------------------------------------------------------- #
# BigQuery execution                                                           #
# --------------------------------------------------------------------------- #


class _FakeJob:
  """The slice of ``bigquery.QueryJob`` the runner reads."""

  def __init__(
      self,
      *,
      frame: pd.DataFrame | None = None,
      processed: int | None = None,
      billed: int | None = None,
      cache_hit: bool = False,
      error: Exception | None = None,
  ):
    self._frame = pd.DataFrame() if frame is None else frame
    self._error = error
    self.total_bytes_processed = processed
    self.total_bytes_billed = billed
    self.cache_hit = cache_hit
    self.ended = None

  def to_dataframe(self, create_bqstorage_client: bool = True) -> pd.DataFrame:
    del create_bqstorage_client
    if self._error is not None:
      raise self._error
    return self._frame

  def reload(self, timeout: float | None = None) -> None:
    del timeout


class _FakeClient:
  """A ``bigquery.Client`` stand-in that records every job it is handed."""

  def __init__(
      self,
      *,
      estimate: int = 1_000,
      frame: pd.DataFrame | None = None,
      billed: int = 1_000,
      cache_hit: bool = False,
      dry_error: Exception | None = None,
      job_error: Exception | None = None,
  ):
    self.estimate = estimate
    self.frame = frame
    self.billed = billed
    self.cache_hit = cache_hit
    self.dry_error = dry_error
    self.job_error = job_error
    self.calls: list[tuple[str, bigquery.QueryJobConfig]] = []

  def query(
      self, sql: str, job_config: bigquery.QueryJobConfig | None = None
  ) -> _FakeJob:
    assert job_config is not None
    self.calls.append((sql, job_config))
    if job_config.dry_run:
      if self.dry_error is not None:
        raise self.dry_error
      return _FakeJob(processed=self.estimate)
    return _FakeJob(
        frame=self.frame,
        processed=self.estimate,
        billed=self.billed,
        cache_hit=self.cache_hit,
        error=self.job_error,
    )

  @property
  def dry_runs(self) -> list[bigquery.QueryJobConfig]:
    return [config for _, config in self.calls if config.dry_run]

  @property
  def real_jobs(self) -> list[bigquery.QueryJobConfig]:
    return [config for _, config in self.calls if not config.dry_run]


def _run(
    client: _FakeClient,
    *,
    tag: str,
    params: tuple[bqca_queries.BqcaParam, ...] = (),
    max_bytes: int = 1_000_000,
) -> models.QueryResult:
  """Runs one query through ``run_bqca_query`` against ``client``.

  ``tag`` makes the SQL unique, so a result cached by another test can never
  answer this one.
  """
  with mock.patch.object(queries, "get_client", return_value=client):
    return bqca_queries.run_bqca_query(
        f"SELECT 1 -- {tag}", params, "my-project", max_bytes
    )


def _ctx(
    max_bytes: int = 1_000_000, theme: models.Theme = models.LIGHT_THEME
) -> models.Context:
  refs = models.TableRefs(
      "my-project", "my_dataset", "bqca_prompt_response_logs", ""
  )
  return models.Context(
      refs=refs,
      window=WINDOW,
      filters=models.Filters(),
      max_bytes=max_bytes,
      theme=theme,
      price_in=0.0,
      price_out=0.0,
  )


def test_job_config_carries_labels_parameters_and_the_cap_on_real_jobs_only():
  params = bqca_queries.bqca_query_params(STATE, _sql("kpis"))
  dry = bqca_queries._job_config(params, 5_000, dry_run=True)
  real = bqca_queries._job_config(params, 5_000)
  assert dry.dry_run is True
  assert dry.maximum_bytes_billed is None
  assert not real.dry_run
  assert real.maximum_bytes_billed == 5_000
  for config in (dry, real):
    assert config.labels == {"app": "bqaa_streamlit", "surface": "bqca"}
    assert config.use_legacy_sql is False
    assert config.use_query_cache is True
    assert [p.name for p in config.query_parameters] == [p.name for p in params]


def test_a_run_dry_runs_first_then_bills_under_the_cap():
  frame = pd.DataFrame({"a": [1, 2]})
  client = _FakeClient(estimate=1_000, billed=2_048, frame=frame)
  params = bqca_queries.bqca_query_params(STATE, _sql("kpis"))
  result = _run(client, tag="success", params=params, max_bytes=10_000)

  assert result.error is None
  pd.testing.assert_frame_equal(result.df, frame)
  assert (result.bytes_processed, result.bytes_billed) == (1_000, 2_048)
  assert result.cache_hit is False
  assert result.stats_known
  assert [config.dry_run for _, config in client.calls] == [True, False]
  assert client.dry_runs[0].maximum_bytes_billed is None
  assert client.real_jobs[0].maximum_bytes_billed == 10_000
  # The same parameters reach the preflight and the real job.
  for config in (client.dry_runs[0], client.real_jobs[0]):
    assert [p.name for p in config.query_parameters] == [p.name for p in params]


def test_a_query_over_the_cap_is_refused_before_a_real_job_starts():
  client = _FakeClient(estimate=5 * 1024**3)
  result = _run(client, tag="guardrail", max_bytes=1024**3)

  assert result.error is not None
  assert "Guardrail" in result.error
  assert "5.0 GB" in result.error
  assert "1.0 GB" in result.error
  assert result.df.empty
  assert (result.bytes_processed, result.bytes_billed) == (0, 0)
  assert len(client.calls) == 1
  assert client.real_jobs == []


def test_an_identical_run_is_served_from_the_cache_at_no_cost():
  frame = pd.DataFrame({"a": [1]})
  client = _FakeClient(frame=frame, billed=2_048)
  first = _run(client, tag="cache")
  second = _run(client, tag="cache")

  assert (first.bytes_billed, first.cache_hit) == (2_048, False)
  assert (second.bytes_billed, second.cache_hit) == (0, True)
  assert second.bytes_processed == first.bytes_processed
  pd.testing.assert_frame_equal(first.df, second.df)
  # One preflight and one job in total: the rerun never reached BigQuery.
  assert len(client.calls) == 2


def test_a_bigquery_cache_hit_bills_nothing():
  client = _FakeClient(
      frame=pd.DataFrame({"a": [1]}), billed=9_999, cache_hit=True
  )
  result = _run(client, tag="bq-cache-hit")
  assert result.cache_hit is True
  assert result.bytes_billed == 0


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            gauth_exc.DefaultCredentialsError("no creds"),
            "gcloud auth application-default login",
        ),
        (gexc.NotFound("Not found: Table x"), "BQ_TABLE_ID"),
        (gexc.Forbidden("denied"), "roles/bigquery.jobUser"),
        (
            gexc.BadRequest("bytesBilledLimitExceeded: too big"),
            "per-query scan cap",
        ),
    ],
)
def test_preflight_failures_become_actionable_advice(error, expected):
  client = _FakeClient(dry_error=error)
  result = _run(client, tag=f"preflight-{type(error).__name__}")
  assert result.error is not None
  assert expected in result.error
  assert result.df.empty
  assert result.stats_known
  assert (result.bytes_processed, result.bytes_billed) == (0, 0)
  assert client.real_jobs == []


def test_not_found_advice_names_the_default_table():
  advice = bqca_queries._explain(gexc.NotFound("Not found: Table x"))
  assert "bqca_prompt_response_logs" in advice
  assert "adk_" not in advice  # no typed-view advice on the BQCA surface


def test_not_found_advice_names_both_table_variables():
  advice = bqca_queries._explain(gexc.NotFound("Not found: Table x"))
  assert "`BQCA_TABLE_ID`" in advice
  assert "`BQ_TABLE_ID`" in advice


def test_unknown_errors_pass_through_unchanged():
  assert bqca_queries._explain(RuntimeError("weird")) == "weird"


def test_credential_failure_while_connecting_is_reported_not_raised():
  with mock.patch.object(
      queries,
      "get_client",
      side_effect=gauth_exc.DefaultCredentialsError("no creds"),
  ):
    result = bqca_queries.run_bqca_query("SELECT 1 -- creds", (), "p", 100)
  assert result.error is not None
  assert "application-default login" in result.error


def test_a_failed_job_still_reports_what_it_scanned():
  client = _FakeClient(
      estimate=4_096, billed=4_096, job_error=RuntimeError("table exploded")
  )
  result = _run(client, tag="job-failure")
  assert result.error == "table exploded"
  assert result.df.empty
  assert (result.bytes_processed, result.bytes_billed) == (4_096, 4_096)
  assert result.stats_known


def test_fetch_logs_the_scan_and_surfaces_errors():
  ctx = _ctx()
  ok = models.QueryResult(pd.DataFrame({"a": [1]}), None, 10, 20, False)
  bad = models.QueryResult(pd.DataFrame(), "boom", 0, 0, False)
  with (
      mock.patch.object(
          bqca_queries, "run_bqca_query", side_effect=[ok, bad]
      ) as run,
      mock.patch.object(
          bqca_queries.st, "spinner", return_value=contextlib.nullcontext()
      ),
      mock.patch.object(bqca_queries.st, "error") as st_error,
  ):
    first = bqca_queries.fetch_bqca("SELECT 1", (), ctx, "KPIs")
    second = bqca_queries.fetch_bqca("SELECT 2", (), ctx, "Latency")

  assert first is ok
  assert second is bad
  assert [entry.label for entry in ctx.scan_log] == ["KPIs", "Latency"]
  assert ctx.scan_log[0] == models.ScanEntry("KPIs", 20, 10, False, True, True)
  run.assert_any_call("SELECT 1", (), "my-project", 1_000_000)
  st_error.assert_called_once()
  message = st_error.call_args.args[0]
  assert "Latency" in message
  assert "boom" in message


def test_fetch_panel_builds_binds_and_runs_a_named_panel():
  ctx = _ctx()
  state = dataclasses.replace(STATE, data_agent_ids=("DA1",))
  seen: dict[str, Any] = {}

  def fake(sql, params, ctx_, label):
    seen.update(sql=sql, params={p.name: p for p in params}, label=label)
    return models.QueryResult(pd.DataFrame())

  with mock.patch.object(bqca_queries, "fetch_bqca", side_effect=fake):
    bqca_queries.fetch_panel("timeline", state, ctx, invocation_id="inv-1")
    assert seen["label"] == "Turn timeline"
    assert seen["params"]["invocation_id"].value == "inv-1"
    # The window is the context's, not a fresh one.
    assert queries.time_bounds(ctx.window) in seen["sql"]

    bqca_queries.fetch_panel("kpis", state, ctx)
    assert seen["label"] == "KPIs"
    assert seen["params"]["data_agent_ids"].value == ("DA1",)
    assert "invocation_id" not in seen["params"]


def test_fetch_panel_rejects_an_unknown_panel():
  with pytest.raises(KeyError):
    bqca_queries.fetch_panel("tool_runs", STATE, _ctx())


def test_filter_options_group_values_by_kind():
  frame = pd.DataFrame(
      {
          "kind": ["data_agent_id", "persona", "data_agent_id", "event_type"],
          "value": ["DA1", "alice", "DA2", "AGENT_RESPONSE"],
      }
  )
  result = models.QueryResult(frame)
  with mock.patch.object(bqca_queries, "fetch_bqca", return_value=result):
    options, returned = bqca_queries.load_bqca_filter_options(STATE, _ctx())
  assert returned is result
  assert options == {
      "data_agent_id": ["DA1", "DA2"],
      "persona": ["alice"],
      "event_type": ["AGENT_RESPONSE"],
  }


def test_filter_options_of_an_empty_result_are_empty():
  result = models.QueryResult(pd.DataFrame(), error="boom")
  with mock.patch.object(bqca_queries, "fetch_bqca", return_value=result):
    options, returned = bqca_queries.load_bqca_filter_options(STATE, _ctx())
  assert options == {}
  assert returned.error == "boom"


# --------------------------------------------------------------------------- #
# bqca_charts                                                                  #
# --------------------------------------------------------------------------- #

THEMES = (models.LIGHT_THEME, models.DARK_THEME)

# Chart builder -> the frame it draws, for every chart the surface ships.
CHARTS = {
    "turn_volume": (bqca_charts.turn_volume_chart, _volume_frame),
    "latency_percentiles": (
        bqca_charts.latency_percentiles_chart,
        _latency_frame,
    ),
    "llm_latency": (bqca_charts.llm_latency_chart, _latency_frame),
    "path_latency": (bqca_charts.path_latency_chart, _latency_frame),
    "token_breakdown": (bqca_charts.token_breakdown_chart, _tokens_frame),
    "tokens_by_model": (bqca_charts.tokens_by_model_chart, _tokens_frame),
    "data_agent_leaderboard": (
        bqca_charts.data_agent_leaderboard_chart,
        _agents_frame,
    ),
    "persona_breakdown": (bqca_charts.persona_breakdown_chart, _personas_frame),
    "embedding_suggestions": (
        bqca_charts.embedding_suggestions_chart,
        _embedding_frame,
    ),
    "suggested_columns": (
        bqca_charts.suggested_columns_chart,
        _embedding_frame,
    ),
    "error_attribution": (bqca_charts.error_attribution_chart, _errors_frame),
}


def _trace(fig: go.Figure, name: str) -> Any:
  matches = [trace for trace in fig.data if trace.name == name]
  assert (
      len(matches) == 1
  ), f"{name!r} not unique in {[t.name for t in fig.data]}"
  return matches[0]


def test_every_chart_builder_is_covered():
  shipped = {
      name
      for name in bqca_charts.__all__
      if name.endswith("_chart") and callable(getattr(bqca_charts, name))
  }
  assert shipped == {builder.__name__ for builder, _ in CHARTS.values()}


@uses_chart_state
@pytest.mark.parametrize("theme", THEMES, ids=["light", "dark"])
@pytest.mark.parametrize("name", list(CHARTS))
def test_every_chart_draws_its_data_in_the_themed_chrome(name, theme):
  builder, frame = CHARTS[name]
  fig = builder(frame(), theme)
  assert isinstance(fig, go.Figure)
  assert len(fig.data) >= 1
  assert fig.layout.paper_bgcolor == theme.surface
  assert fig.layout.plot_bgcolor == theme.surface
  assert fig.layout.height == 320
  fig.to_json()  # Serializable, so Streamlit can ship it to the browser.
  assert builder(frame(), theme, height=410).layout.height == 410


@uses_chart_state
@pytest.mark.parametrize("name", list(CHARTS))
def test_empty_none_and_unrelated_frames_give_a_traceless_themed_figure(name):
  builder, _ = CHARTS[name]
  for empty in (None, pd.DataFrame(), pd.DataFrame({"unrelated": [1, 2]})):
    fig = builder(empty, models.DARK_THEME)
    assert len(fig.data) == 0
    assert fig.layout.paper_bgcolor == models.DARK_THEME.surface


@uses_chart_state
def test_all_null_metrics_draw_nothing_instead_of_zero_lines():
  frame = _latency_frame()
  for column in frame.columns.difference(["bucket", "fast_path_label"]):
    frame[column] = NAN
  for builder in (
      bqca_charts.latency_percentiles_chart,
      bqca_charts.llm_latency_chart,
      bqca_charts.path_latency_chart,
  ):
    assert len(builder(frame, models.LIGHT_THEME).data) == 0, builder.__name__


@uses_chart_state
def test_nullable_integer_columns_with_pd_na_are_tolerated():
  for name, (builder, frame) in CHARTS.items():
    data = frame()
    for column in data.columns:
      if pd.api.types.is_integer_dtype(data[column]):
        data[column] = data[column].astype("Int64")
        data.loc[data.index[-1], column] = pd.NA
    builder(data, models.LIGHT_THEME).to_json()  # Must not raise.


@uses_chart_state
def test_turn_volume_stacks_paths_standard_first_and_draws_an_error_line():
  fig = bqca_charts.turn_volume_chart(_volume_frame(), models.LIGHT_THEME)
  bars = [trace for trace in fig.data if trace.type == "bar"]
  assert [bar.name for bar in bars] == ["Standard NL2SQL", "Fast path"]
  assert list(_trace(fig, "Standard NL2SQL").y) == [3, 2]
  assert list(_trace(fig, "Fast path").y) == [1]
  assert fig.layout.barmode == "stack"
  errors = _trace(fig, "Error turns")
  assert errors.type == "scatter"
  assert list(errors.y) == [1, 0]
  # The error line sits on the same y axis as the bars it counts turns of.
  assert errors.yaxis in (None, "y")
  assert all(bar.yaxis in (None, "y") for bar in bars)
  assert errors.line.color == models.LIGHT_THEME.categorical[-1]


@uses_chart_state
def test_turn_volume_without_errors_has_no_error_line():
  frame = _volume_frame()
  frame["error_turns"] = 0
  fig = bqca_charts.turn_volume_chart(frame, models.LIGHT_THEME)
  assert "Error turns" not in [trace.name for trace in fig.data]


@uses_chart_state
def test_path_colors_are_shared_between_volume_and_latency_charts():
  theme = models.LIGHT_THEME
  volume = bqca_charts.turn_volume_chart(_volume_frame(), theme)
  latency = bqca_charts.path_latency_chart(_latency_frame(), theme)
  for path in ("Standard NL2SQL", "Fast path"):
    bar_color = _trace(volume, path).marker.color
    assert _trace(latency, f"{path} P50").line.color == bar_color
    assert _trace(latency, f"{path} P95").line.color == bar_color
  # Identity is never colour alone: P95 is dashed, P50 solid.
  assert _trace(latency, "Fast path P50").line.dash == "solid"
  assert _trace(latency, "Fast path P95").line.dash == "dash"


@uses_chart_state
def test_latency_charts_read_the_all_paths_rows_only():
  theme = models.LIGHT_THEME
  percentiles = bqca_charts.latency_percentiles_chart(_latency_frame(), theme)
  assert [trace.name for trace in percentiles.data] == ["P50", "P95", "P99"]
  assert list(_trace(percentiles, "P50").y) == [900.0, 1000.0]
  assert list(_trace(percentiles, "P95").y) == [5000.0, 4000.0]
  llm = bqca_charts.llm_latency_chart(_latency_frame(), theme)
  assert [trace.name for trace in llm.data] == [
      "LLM P50",
      "LLM P95",
      "First token P50",
      "First token P95",
  ]
  assert list(_trace(llm, "LLM P50").y) == [1200.0, 1100.0]
  by_path = bqca_charts.path_latency_chart(_latency_frame(), theme)
  assert {trace.name for trace in by_path.data} == {
      "Standard NL2SQL P50",
      "Standard NL2SQL P95",
      "Fast path P50",
      "Fast path P95",
  }


@uses_chart_state
def test_token_segments_do_not_count_cached_input_twice():
  fig = bqca_charts.token_breakdown_chart(_tokens_frame(), models.LIGHT_THEME)
  assert [trace.name for trace in fig.data] == [
      "Input (uncached)",
      "Cached input",
      "Output",
      "Thinking",
  ]
  # Day 1: input 100+50, cached 40 -> 110 uncached; day 2: 80-30 -> 50.
  assert list(_trace(fig, "Input (uncached)").y) == [110, 50]
  assert list(_trace(fig, "Cached input").y) == [40, 30]
  assert list(_trace(fig, "Output").y) == [30, 15]
  assert list(_trace(fig, "Thinking").y) == [15, 0]
  stack = [sum(trace.y[i] for trace in fig.data) for i in range(2)]
  # The stack is the real volume: input + output + thinking, per day.
  assert stack == [150 + 30 + 15, 80 + 15 + 0]
  assert fig.layout.barmode == "stack"


@uses_chart_state
def test_cached_tokens_beyond_input_never_draw_a_negative_segment():
  frame = _tokens_frame().iloc[:1].copy()
  frame["input_tokens"] = 10
  frame["cached_tokens"] = 50
  fig = bqca_charts.token_breakdown_chart(frame, models.LIGHT_THEME)
  assert "Input (uncached)" not in [trace.name for trace in fig.data]
  assert all(min(trace.y) >= 0 for trace in fig.data)


@uses_chart_state
def test_token_segments_present_in_the_data_only():
  frame = _tokens_frame()
  frame["thoughts_tokens"] = 0
  fig = bqca_charts.token_breakdown_chart(frame, models.LIGHT_THEME)
  assert "Thinking" not in [trace.name for trace in fig.data]
  frame[["input_tokens", "output_tokens", "cached_tokens"]] = 0
  assert (
      len(bqca_charts.token_breakdown_chart(frame, models.LIGHT_THEME).data)
      == 0
  )


@uses_chart_state
def test_tokens_by_model_folds_the_long_tail_into_other():
  frame = pd.DataFrame(
      {
          "bucket": [D1] * 12,
          "model_name": [f"model-{i}" for i in range(12)],
          "llm_calls": 1,
          "input_tokens": list(range(100, 112)),
          "output_tokens": 10,
          "thoughts_tokens": 1,
          "cached_tokens": 5,
          "total_tokens": 100,
      }
  )
  fig = bqca_charts.tokens_by_model_chart(frame, models.LIGHT_THEME)
  labels = list(fig.data[0].y)
  assert len(labels) == 8
  assert models.OTHER_LABEL in labels
  # The folded bar is neutral gray, never a ninth hue.
  assert labels.count(models.OTHER_LABEL) == 1
  assert fig.layout.barmode == "stack"


@uses_chart_state
def test_error_chart_orders_agents_by_volume_and_folds_the_tail():
  fig = bqca_charts.error_attribution_chart(_errors_frame(), models.LIGHT_THEME)
  assert {trace.name for trace in fig.data} == {"INVOCATION_ERROR", "LLM_ERROR"}
  agents = set().union(*(set(trace.y) for trace in fig.data))
  assert agents == {"DA2", "unattributed"}
  # Largest total on top: DA2 (4 errors) above unattributed (2).
  assert list(fig.layout.yaxis.categoryarray) == ["unattributed", "DA2"]
  many = pd.DataFrame(
      {
          "event_type": ["LLM_ERROR"] * 12,
          "data_agent_id": [f"DA{i}" for i in range(12)],
          "errors": list(range(1, 13)),
      }
  )
  folded = bqca_charts.error_attribution_chart(many, models.LIGHT_THEME)
  assert len(set().union(*(set(trace.y) for trace in folded.data))) == 8
  assert models.OTHER_LABEL in set().union(*(set(t.y) for t in folded.data))


@uses_chart_state
def test_error_chart_without_errors_draws_nothing():
  frame = _errors_frame()
  frame["errors"] = 0
  assert (
      len(bqca_charts.error_attribution_chart(frame, models.LIGHT_THEME).data)
      == 0
  )


@uses_chart_state
def test_embedding_chart_stacks_suggestions_by_reason_with_column_hover():
  fig = bqca_charts.embedding_suggestions_chart(
      _embedding_frame(), models.LIGHT_THEME
  )
  assert {trace.type for trace in fig.data} == {"bar"}
  assert {trace.name for trace in fig.data} == {
      "ai_similarity_skill_fallback",
      "brute_force_keyword_search",
  }
  skill = _trace(fig, "ai_similarity_skill_fallback")
  assert len(skill.x) == 2
  assert list(skill.y) == [2, 1]
  assert list(skill.hovertext) == [
      "Suggested columns: 5",
      "Suggested columns: 7",
  ]
  brute = _trace(fig, "brute_force_keyword_search")
  assert list(brute.y) == [1]
  assert list(brute.hovertext) == ["Suggested columns: 1"]
  assert "%{fullData.name}" in skill.hovertemplate
  assert fig.layout.barmode == "stack"
  # One colour per reason, identical in every bucket it appears in.
  assert skill.marker.color != brute.marker.color


@uses_chart_state
def test_embedding_chart_names_a_missing_reason_unspecified():
  frame = _embedding_frame()
  frame["embedding_reason"] = [None, "(unspecified)", None]
  fig = bqca_charts.embedding_suggestions_chart(frame, models.LIGHT_THEME)
  assert [trace.name for trace in fig.data] == ["(unspecified)"]
  # Day 1 holds two rows of the one reason: they add up.
  assert list(fig.data[0].y) == [3, 1]
  assert list(fig.data[0].hovertext) == [
      "Suggested columns: 6",
      "Suggested columns: 7",
  ]


@uses_chart_state
def test_embedding_chart_folds_the_long_tail_of_reasons_into_other():
  frame = pd.DataFrame(
      {
          "bucket": [D1] * 12,
          "embedding_reason": [f"reason-{i}" for i in range(12)],
          "suggestion_events": list(range(1, 13)),
          "suggested_columns": [1] * 12,
      }
  )
  fig = bqca_charts.embedding_suggestions_chart(frame, models.LIGHT_THEME)
  names = [trace.name for trace in fig.data]
  assert len(names) == 8
  assert models.OTHER_LABEL in names
  # The five smallest reasons (1 + 2 + 3 + 4 + 5 suggestions) are folded.
  assert list(_trace(fig, models.OTHER_LABEL).y) == [15]
  assert "reason-0" not in names
  assert "reason-11" in names


@uses_chart_state
def test_embedding_chart_without_suggestions_draws_nothing():
  empty = _embedding_frame()
  empty["suggestion_events"] = 0
  assert (
      len(
          bqca_charts.embedding_suggestions_chart(
              empty, models.LIGHT_THEME
          ).data
      )
      == 0
  )
  # Without the reason column there is nothing to stack by.
  unreasoned = _embedding_frame().drop(columns="embedding_reason")
  assert (
      len(
          bqca_charts.embedding_suggestions_chart(
              unreasoned, models.LIGHT_THEME
          ).data
      )
      == 0
  )


@uses_chart_state
def test_suggested_columns_chart_bins_every_suggestion_once():
  fig = bqca_charts.suggested_columns_chart(
      _embedding_frame(), models.LIGHT_THEME
  )
  # Four suggestions in the frame: 1 column x2, 2-4 columns x1, 5+ columns x1.
  assert list(fig.data[0].x) == ["1", "2–4", "5+"]
  assert list(fig.data[0].y) == [2.0, 1.0, 1.0]
  assert sum(fig.data[0].y) == _embedding_frame()["suggestion_events"].sum()
  assert (
      fig.data[0].hovertemplate
      == "Suggestions with %{x} columns: %{y:,}<extra></extra>"
  )
  # A "0" bar shows up only once some suggestion carried no column.
  frame = _embedding_frame()
  frame.loc[0, "zero_columns"] = 3
  with_zero = bqca_charts.suggested_columns_chart(frame, models.LIGHT_THEME)
  assert list(with_zero.data[0].x) == ["0", "1", "2–4", "5+"]
  assert list(with_zero.data[0].y) == [3.0, 2.0, 1.0, 1.0]
  frame[
      ["zero_columns", "one_column", "two_to_four_columns", "five_plus_columns"]
  ] = 0
  assert (
      len(bqca_charts.suggested_columns_chart(frame, models.LIGHT_THEME).data)
      == 0
  )


@uses_chart_state
def test_customer_controlled_text_is_escaped_in_labels_and_hover():
  hostile = "<img src=x onerror=alert(1)>"
  agents = _agents_frame()
  agents.loc[0, "data_agent_id"] = hostile
  personas = _personas_frame()
  personas.loc[0, "persona"] = hostile
  tokens = _tokens_frame()
  tokens["model_name"] = hostile
  errors = _errors_frame()
  errors["event_type"] = hostile
  errors["data_agent_id"] = hostile
  embedding = _embedding_frame()
  embedding["embedding_reason"] = hostile
  figures = [
      bqca_charts.data_agent_leaderboard_chart(agents, models.LIGHT_THEME),
      bqca_charts.persona_breakdown_chart(personas, models.LIGHT_THEME),
      bqca_charts.tokens_by_model_chart(tokens, models.LIGHT_THEME),
      bqca_charts.error_attribution_chart(errors, models.LIGHT_THEME),
      bqca_charts.embedding_suggestions_chart(embedding, models.LIGHT_THEME),
  ]
  for fig in figures:
    rendered = fig.to_json()
    assert hostile not in rendered
    assert "&lt;img" in rendered
  hover = list(figures[0].data[0].hovertext)
  assert any(
      "&lt;img src=x onerror=alert(1)&gt;<br>Turns: " in text for text in hover
  )


@uses_chart_state
def test_series_names_are_read_back_never_spliced_into_hover_templates():
  hostile = "X%{y}<b>"
  errors = _errors_frame()
  errors["event_type"] = hostile
  embedding = _embedding_frame()
  embedding["embedding_reason"] = hostile
  for fig in (
      bqca_charts.error_attribution_chart(errors, models.LIGHT_THEME),
      bqca_charts.embedding_suggestions_chart(embedding, models.LIGHT_THEME),
      bqca_charts.turn_volume_chart(_volume_frame(), models.LIGHT_THEME),
      bqca_charts.token_breakdown_chart(_tokens_frame(), models.LIGHT_THEME),
      bqca_charts.llm_latency_chart(_latency_frame(), models.LIGHT_THEME),
  ):
    for trace in fig.data:
      assert "%{fullData.name}" in trace.hovertemplate
      assert str(trace.name) not in trace.hovertemplate


@uses_chart_state
def test_charts_resolve_their_theme_from_context_theme_or_the_active_theme():
  frame = _volume_frame()
  from_context = bqca_charts.turn_volume_chart(
      frame, _ctx(theme=models.DARK_THEME)
  )
  assert from_context.layout.paper_bgcolor == models.DARK_THEME.surface
  from_theme = bqca_charts.turn_volume_chart(frame, models.DARK_THEME)
  assert from_theme.layout.paper_bgcolor == models.DARK_THEME.surface
  with mock.patch.object(
      bqca_charts, "active_theme", return_value=models.DARK_THEME
  ):
    from_none = bqca_charts.turn_volume_chart(frame)
  assert from_none.layout.paper_bgcolor == models.DARK_THEME.surface
  with mock.patch.object(
      bqca_charts, "active_theme", return_value=models.LIGHT_THEME
  ):
    light = bqca_charts.turn_volume_chart(frame, None)
  assert light.layout.paper_bgcolor == models.LIGHT_THEME.surface


# --------------------------------------------------------------------------- #
# The BQCA surface, end to end (AppTest)                                       #
# --------------------------------------------------------------------------- #

KPI_LABELS = (
    "Total Turns",
    "Turn Error Rate",
    "P50 Turn Latency",
    "P95 Turn Latency",
    "Total Tokens",
    "Thinking Tokens",
    "Cached Tokens",
    "Fast-Path Rate",
    "Embedding Suggestion Coverage",
)
BQCA_TABLE_REF = "`test-project.test_dataset.bqca_prompt_response_logs`"


class _Call(NamedTuple):
  label: str
  sql: str
  params: dict[str, Any]
  ctx: models.Context


class FakeBigQuery:
  """Replaces ``bqca_queries.fetch_bqca``: canned frames, every call recorded.

  ``frames`` and ``errors`` are keyed by the panel's scan-log label; a panel
  without a frame (or with an error) comes back empty, like a real failure.
  """

  def __init__(self) -> None:
    self.frames: dict[str, pd.DataFrame] = {
        label: build() for label, build in PANEL_FRAMES.items()
    }
    self.errors: dict[str, str] = {}
    self.calls: list[_Call] = []

  def __call__(self, sql, params, ctx, label) -> models.QueryResult:
    self.calls.append(_Call(label, sql, {p.name: p.value for p in params}, ctx))
    ctx.scan_log.append(models.ScanEntry(label, 0, 1_024, True, True, True))
    error = self.errors.get(label)
    frame = self.frames.get(label)
    return models.QueryResult(
        df=pd.DataFrame() if error or frame is None else frame.copy(),
        error=error,
        bytes_processed=1_024,
        bytes_billed=0,
        cache_hit=True,
    )

  def labels(self) -> list[str]:
    return [call.label for call in self.calls]

  def last(self, label: str) -> _Call:
    return [call for call in self.calls if call.label == label][-1]


@pytest.fixture
def bq():
  fake = FakeBigQuery()
  with mock.patch.object(bqca_queries, "fetch_bqca", new=fake):
    yield fake


@pytest.fixture
def adk_stubs():
  """Stubs the ADK queries so the default surface stays hermetic."""
  empty = models.QueryResult(pd.DataFrame(), None, 0, 0, True)
  with (
      mock.patch.object(
          queries, "load_filter_options", return_value=({}, empty)
      ),
      mock.patch.object(queries, "fetch", return_value=empty),
  ):
    yield


def _app() -> AppTest:
  return AppTest.from_file(str(APP_PATH), default_timeout=60)


@pytest.fixture
def bqca_app(monkeypatch: pytest.MonkeyPatch, bq: FakeBigQuery) -> AppTest:
  """An un-run AppTest that opens straight into the BQCA surface."""
  monkeypatch.setenv("BQAA_PROFILE", "bqca")
  return _app()


def _surface(at: AppTest):
  return at.sidebar.radio(key="_dashboard_surface")


def _metrics(at: AppTest) -> dict[str, str]:
  return {metric.label: metric.value for metric in at.metric}


def _sidebar_button(at: AppTest, label: str):
  return next(button for button in at.sidebar.button if button.label == label)


def _selectbox(at: AppTest, label: str):
  return next(box for box in at.selectbox if box.label == label)


def _captions(at: AppTest) -> list[str]:
  return [caption.value for caption in at.caption]


def test_default_surface_is_adk_and_never_touches_bqca(bq, adk_stubs):
  at = _app().run()
  assert not at.exception
  assert _surface(at).value == ADK_SURFACE
  assert list(_surface(at).options) == [ADK_SURFACE, BQCA_SURFACE]
  assert [tab.label for tab in at.tabs] == ADK_TABS
  assert bq.calls == []


def test_profile_environment_opens_the_bqca_surface(bqca_app):
  at = bqca_app.run()
  assert not at.exception
  assert [title.value for title in at.title] == ["BigQuery Agent Analytics"]
  assert _surface(at).value == BQCA_SURFACE
  assert [tab.label for tab in at.tabs] == BQCA_TABS


def test_profile_query_parameter_opens_the_bqca_surface(bq):
  at = _app()
  at.query_params["profile"] = "bqca"
  at.run()
  assert not at.exception
  assert _surface(at).value == BQCA_SURFACE
  assert [tab.label for tab in at.tabs] == BQCA_TABS


@pytest.mark.parametrize("spelling", ["BQCA", " bqca ", "Bqca"])
def test_profile_is_case_and_whitespace_insensitive(monkeypatch, bq, spelling):
  monkeypatch.setenv("BQAA_PROFILE", spelling)
  at = _app().run()
  assert not at.exception
  assert _surface(at).value == BQCA_SURFACE


def test_profile_query_parameter_beats_the_environment(
    monkeypatch, bq, adk_stubs
):
  monkeypatch.setenv("BQAA_PROFILE", "bqca")
  at = _app()
  at.query_params["profile"] = "adk"
  at.run()
  assert not at.exception
  assert _surface(at).value == ADK_SURFACE
  assert [tab.label for tab in at.tabs] == ADK_TABS
  assert bq.calls == []


@pytest.mark.parametrize("profile", ["", "grafana", "bqca-v2"])
def test_unknown_or_empty_profile_falls_back_to_adk(bq, adk_stubs, profile):
  at = _app()
  at.query_params["profile"] = profile
  at.run()
  assert not at.exception
  assert _surface(at).value == ADK_SURFACE
  assert bq.calls == []


def test_surface_radio_switches_both_ways_without_widget_collisions(
    bq, adk_stubs
):
  at = _app().run()
  assert [tab.label for tab in at.tabs] == ADK_TABS

  _surface(at).set_value(BQCA_SURFACE).run()
  assert not at.exception
  assert [tab.label for tab in at.tabs] == BQCA_TABS
  assert "KPIs" in bq.labels()

  _surface(at).set_value(ADK_SURFACE).run()
  assert not at.exception
  assert [tab.label for tab in at.tabs] == ADK_TABS

  _surface(at).set_value(BQCA_SURFACE).run()
  assert not at.exception
  assert [tab.label for tab in at.tabs] == BQCA_TABS


def test_kpi_header_shows_the_nine_formatted_metrics(bqca_app):
  at = bqca_app.run()
  assert not at.exception
  metrics = _metrics(at)
  assert {label: metrics[label] for label in KPI_LABELS} == {
      "Total Turns": "4",
      "Turn Error Rate": "25.0%",
      "P50 Turn Latency": "900 ms",
      "P95 Turn Latency": "5,000 ms",
      "Total Tokens": "130",
      "Thinking Tokens": "10",
      "Cached Tokens": "40",
      "Fast-Path Rate": "33.3%",
      "Embedding Suggestion Coverage": "50.0%",
  }
  assert [metric.label for metric in at.metric][: len(KPI_LABELS)] == list(
      KPI_LABELS
  )


def test_embedding_kpi_is_coverage_of_turns_not_a_per_suggestion_hit_rate(
    bqca_app,
):
  at = bqca_app.run()
  assert not at.exception
  assert not any("Hit Rate" in metric.label for metric in at.metric)
  [coverage] = [
      metric
      for metric in at.metric
      if metric.label == "Embedding Suggestion Coverage"
  ]
  assert "turns" in coverage.help
  assert "suggested columns" in coverage.help


def _completion_notes(at: AppTest) -> list[str]:
  return [c for c in _captions(at) if "turns completed" in c]


def test_incomplete_turns_are_called_out_under_the_kpis(bqca_app):
  at = bqca_app.run()
  assert not at.exception
  [note] = _completion_notes(at)
  assert note.startswith("3 of 4 turns completed; 1 incomplete")
  # The cause, and what an incomplete turn is left out of, are both stated.
  assert "no INVOCATION_COMPLETED event" in note
  assert "cover completed turns only" in note
  # Total Turns still counts every turn, completed or not.
  assert _metrics(at)["Total Turns"] == "4"


def test_a_window_where_every_turn_completed_says_so(bqca_app, bq):
  bq.frames["KPIs"] = _kpi_frame().assign(completed_turns=4)
  at = bqca_app.run()
  assert not at.exception
  assert _completion_notes(at) == [
      "All 4 turns completed (reached INVOCATION_COMPLETED)."
  ]


def test_no_completion_note_without_turns(bqca_app, bq):
  bq.frames["KPIs"] = pd.DataFrame()
  at = bqca_app.run()
  assert not at.exception
  assert _completion_notes(at) == []
  bq.frames["KPIs"] = _kpi_frame().assign(total_turns=0, completed_turns=0)
  assert _completion_notes(bqca_app.run()) == []


def test_each_panel_is_queried_once_per_render(bqca_app, bq):
  at = bqca_app.run()
  assert not at.exception
  assert collections.Counter(bq.labels()) == collections.Counter(
      dict.fromkeys(PANEL_FRAMES, 1)
  )


def test_every_rendered_query_is_allowlisted_and_reads_the_default_table(
    bqca_app, bq
):
  bqca_app.run()
  assert bq.calls
  for call in bq.calls:
    assert call.params["allowed_event_types"] == (
        bqca_models.BQCA_ALLOWED_EVENT_TYPES
    ), call.label
    assert "event_type IN UNNEST(@allowed_event_types)" in call.sql
    assert BQCA_TABLE_REF in call.sql
    for forbidden in FORBIDDEN_EVENT_TYPES:
      assert forbidden not in call.sql


def test_all_charts_render_beside_their_table_twins(bqca_app):
  at = bqca_app.run()
  assert not at.exception
  assert len(at.get("plotly_chart")) == len(CHARTS)
  assert [e.label for e in at.expander].count("Table view") >= len(CHARTS)


def test_explorer_shows_prompt_as_text_response_as_markdown_and_the_sql(
    bqca_app,
):
  at = bqca_app.run()
  assert not at.exception
  assert [(code.language, code.value) for code in at.code] == [
      ("sql", "SELECT COUNT(*) FROM orders")
  ]
  assert any("Part A" in m.value and "Part B" in m.value for m in at.markdown)
  # The prompt is data: literal text, never interpreted.
  assert [text.value for text in at.text] == [
      "How many orders were placed? <script>x</script>"
  ]
  assert {"Prompt", "Response"} <= {expander.label for expander in at.expander}
  # Markdown only: HTML in a logged answer is never rendered.
  assert all(not markdown.proto.allow_html for markdown in at.markdown)


def test_turn_picker_switches_the_detail_and_its_timeline(bqca_app, bq):
  at = bqca_app.run()
  assert bq.last("Turn timeline").params["invocation_id"] == "inv1"

  _selectbox(at, "Turn").select_index(2).run()
  assert not at.exception
  assert at.session_state["_bqca_selected_turn"] == "inv3"
  assert bq.last("Turn timeline").params["invocation_id"] == "inv3"
  assert [error.value for error in at.error] == [
      "quota exceeded | Invocation failed"
  ]
  assert len(at.code) == 0
  assert any("No SQL block was found" in caption for caption in _captions(at))
  assert any("no response logged" in m.value for m in at.markdown)
  metrics = _metrics(at)
  assert metrics["Status"] == "ERROR"
  assert metrics["Turn latency"] == "—"

  # The choice survives an unrelated rerun.
  at.run()
  assert _selectbox(at, "Turn").value == "inv3"


def _detail_caption(at: AppTest) -> str:
  """The one-line identity of the turn the explorer is showing."""
  [caption] = [c for c in _captions(at) if c.startswith("Data agent ")]
  return caption


def test_the_turn_picker_is_a_keyed_widget_seeded_from_the_first_turn(
    bqca_app,
):
  at = bqca_app.run()
  assert not at.exception
  picker = at.selectbox(key="_bqca_turn_select")
  assert picker.label == "Turn"
  assert picker.value == "inv1"
  assert at.session_state["_bqca_selected_turn"] == "inv1"


def test_every_consecutive_turn_selection_sticks(bqca_app, bq):
  # The picker used to be handed the previous choice as its ``index``, which
  # changed its identity: the second selection in a row snapped back to the
  # turn chosen before it.
  at = bqca_app.run()
  assert bq.last("Turn timeline").params["invocation_id"] == "inv1"
  for index, turn in ((2, "inv3"), (0, "inv1"), (3, "inv4"), (1, "inv2")):
    _selectbox(at, "Turn").select_index(index).run()
    assert not at.exception
    assert _selectbox(at, "Turn").value == turn
    assert at.session_state["_bqca_turn_select"] == turn
    assert at.session_state["_bqca_selected_turn"] == turn
    assert f"invocation `{turn}`" in _detail_caption(at)
    assert bq.last("Turn timeline").params["invocation_id"] == turn


def test_the_picker_falls_back_when_its_turn_is_no_longer_listed(bqca_app, bq):
  at = bqca_app.run()
  _selectbox(at, "Turn").select_index(2).run()
  assert at.session_state["_bqca_turn_select"] == "inv3"

  # Another scope (a new window or filter) no longer lists inv3.
  bq.frames["Turns"] = _turns_frame().iloc[[0, 1]].reset_index(drop=True)
  at.run()
  assert not at.exception
  assert _selectbox(at, "Turn").value == "inv1"
  assert at.session_state["_bqca_turn_select"] == "inv1"
  assert at.session_state["_bqca_selected_turn"] == "inv1"
  assert bq.last("Turn timeline").params["invocation_id"] == "inv1"


def test_the_picker_remembers_its_turn_across_a_tab_switch(
    monkeypatch, bqca_app, bq
):
  # Lazy tabs render only the active tab, so leaving the explorer drops the
  # picker's own state; the plain key that remembers the turn seeds it again.
  monkeypatch.setenv("STREAMLIT_LAZY_TABS", "true")
  explorer, elsewhere = BQCA_TABS[2], BQCA_TABS[4]
  at = bqca_app.run()
  at.segmented_control(key="_bqca_active_tab").set_value(explorer).run()
  _selectbox(at, "Turn").select_index(2).run()
  assert at.session_state["_bqca_selected_turn"] == "inv3"

  at.segmented_control(key="_bqca_active_tab").set_value(elsewhere).run()
  assert not at.exception
  at.segmented_control(key="_bqca_active_tab").set_value(explorer).run()
  assert not at.exception
  assert _selectbox(at, "Turn").value == "inv3"
  assert bq.last("Turn timeline").params["invocation_id"] == "inv3"


def test_a_new_connection_resets_the_turn_picker(bqca_app, bq):
  at = bqca_app.run()
  _selectbox(at, "Turn").select_index(2).run()
  assert at.session_state["_bqca_turn_select"] == "inv3"

  at.sidebar.text_input(key="bqca_dataset").input("other_dataset")
  _sidebar_button(at, "Connect").click().run()
  assert not at.exception
  # The picker's state and the key that remembers its turn were both cleared,
  # so the new connection opens on its first turn, not on a stale one.
  assert at.session_state["_bqca_turn_select"] == "inv1"
  assert at.session_state["_bqca_selected_turn"] == "inv1"
  assert bq.last("Turn timeline").ctx.refs.dataset == "other_dataset"
  assert bq.last("Turn timeline").params["invocation_id"] == "inv1"


def test_fast_path_turn_is_labelled_and_has_no_token_counts(bqca_app):
  at = bqca_app.run()
  _selectbox(at, "Turn").select_index(1).run()
  assert not at.exception
  metrics = _metrics(at)
  assert metrics["Path"] == "Fast path"
  assert metrics["Tokens"] == "—"
  assert metrics["Thinking tokens"] == "—"


def test_timeline_table_lists_the_turns_events(bqca_app):
  at = bqca_app.run()
  timelines = [
      frame.value
      for frame in at.dataframe
      if {"event_type", "span_id", "content_preview"}
      <= set(frame.value.columns)
  ]
  assert len(timelines) == 1
  assert list(timelines[0]["event_type"]) == list(
      _timeline_frame()["event_type"]
  )


def test_apply_binds_every_filter_into_the_queries_that_honor_it(bqca_app, bq):
  at = bqca_app.run()
  at.sidebar.multiselect(key="bqca_flt_data_agent").select("DA1")
  at.sidebar.multiselect(key="bqca_flt_persona").select("alice")
  at.sidebar.multiselect(key="bqca_flt_event_type").select("LLM_ERROR")
  at.sidebar.radio(key="bqca_flt_fast_path").set_value("Fast Path Only")
  at.sidebar.text_input(key="bqca_flt_session_search").input(" s1 ")
  at.sidebar.text_input(key="bqca_flt_prompt_search").input("orders")
  at.sidebar.toggle(key="bqca_flt_errors_only").set_value(True)
  bq.calls.clear()
  _sidebar_button(at, "Apply filters").click().run()

  assert not at.exception
  assert at.session_state["_bqca_applied"] == {
      "data_agent_ids": ("DA1",),
      "personas": ("alice",),
      "event_types": ("LLM_ERROR",),
      "fast_path_mode": "Fast Path Only",
      "session_search": "s1",
      "prompt_search": "orders",
      "errors_only": True,
  }
  kpis = bq.last("KPIs")
  assert kpis.params["data_agent_ids"] == ("DA1",)
  assert kpis.params["personas"] == ("alice",)
  assert kpis.params["fast_path_labels"] == ("fast_path",)
  assert kpis.params["session_search"] == "s1"
  assert "turn_has_error" in kpis.sql
  assert "event_types" not in kpis.params
  assert "prompt_search" not in kpis.params
  assert bq.last("Error attribution").params["event_types"] == ("LLM_ERROR",)
  assert bq.last("Turns").params["prompt_search"] == "orders"
  assert bq.last("Turn timeline").params["event_types"] == ("LLM_ERROR",)
  # The option lists are never narrowed by the filters.
  assert "data_agent_ids" not in bq.last("Filter options").params


def test_applying_nothing_means_every_value(bqca_app, bq):
  at = bqca_app.run()
  _sidebar_button(at, "Apply filters").click().run()
  assert not at.exception
  assert at.session_state["_bqca_applied"]["data_agent_ids"] == ()
  assert bq.last("KPIs").params["data_agent_ids"] == (models.ALL_SENTINEL,)
  assert bq.last("KPIs").params["fast_path_labels"] == (
      "fast_path",
      "standard_nl2sql",
  )


def test_filters_wait_for_apply(bqca_app, bq):
  at = bqca_app.run()
  at.sidebar.multiselect(key="bqca_flt_data_agent").select("DA1")
  bq.calls.clear()
  at.run()
  assert not at.exception
  assert bq.last("KPIs").params["data_agent_ids"] == (models.ALL_SENTINEL,)


def test_filter_options_come_from_the_query_not_the_ui(bqca_app):
  at = bqca_app.run()
  assert at.sidebar.multiselect(key="bqca_flt_data_agent").options == [
      "DA1",
      "DA2",
  ]
  assert at.sidebar.multiselect(key="bqca_flt_persona").options == [
      "alice",
      "exec",
  ]
  assert at.sidebar.multiselect(key="bqca_flt_event_type").options == list(
      bqca_models.BQCA_ALLOWED_EVENT_TYPES
  )


def test_time_range_presets_drive_the_window_and_bucket(bqca_app, bq):
  at = bqca_app.run()
  assert bq.last("KPIs").ctx.window.span == dt.timedelta(days=7)
  assert bq.last("KPIs").ctx.window.bucket == "DAY"

  at.sidebar.selectbox(key="bqca_range").select("Last 24 hours").run()
  assert not at.exception
  call = bq.last("KPIs")
  assert call.ctx.window.span == dt.timedelta(hours=24)
  assert call.ctx.window.bucket == "HOUR"
  assert queries.time_bounds(call.ctx.window) in call.sql
  assert any("hour buckets" in caption.value for caption in at.sidebar.caption)


def test_custom_range_takes_inclusive_utc_days(bqca_app, bq):
  at = bqca_app.run()
  at.sidebar.selectbox(key="bqca_range").select("Custom range").run()
  assert not at.exception
  picker = at.sidebar.date_input(key="bqca_custom_range")
  picker.set_value((dt.date(2026, 9, 1), dt.date(2026, 9, 3))).run()
  assert not at.exception
  window = bq.last("KPIs").ctx.window
  assert window.start == dt.datetime(2026, 9, 1, tzinfo=UTC)
  # The picker's end day is inclusive; the window's end is exclusive.
  assert window.end == dt.datetime(2026, 9, 4, tzinfo=UTC)


def test_scan_cap_flows_into_every_query_context(bqca_app, bq):
  at = bqca_app.run()
  assert bq.last("KPIs").ctx.max_bytes == models.BYTES_CAPS["1 GB"]
  at.sidebar.selectbox(key="bqca_scan_cap").select("100 MB")
  _sidebar_button(at, "Connect").click().run()
  assert not at.exception
  assert {call.ctx.max_bytes for call in bq.calls[-11:]} == {
      models.BYTES_CAPS["100 MB"]
  }


def test_events_table_defaults_to_the_bqca_table(bqca_app, bq):
  at = bqca_app.run()
  table = at.sidebar.text_input(key="bqca_table")
  assert table.value == "bqca_prompt_response_logs"
  assert BQCA_TABLE_REF in bq.last("KPIs").sql


def test_bq_table_id_overrides_the_default_table(monkeypatch, bqca_app, bq):
  monkeypatch.setenv("BQ_TABLE_ID", "agent_events")
  at = bqca_app.run()
  assert at.sidebar.text_input(key="bqca_table").value == "agent_events"
  assert "`test-project.test_dataset.agent_events`" in bq.last("KPIs").sql


def test_bqca_table_id_overrides_the_default_table(monkeypatch, bqca_app, bq):
  monkeypatch.setenv("BQCA_TABLE_ID", "bqca_only_logs")
  at = bqca_app.run()
  assert not at.exception
  assert at.sidebar.text_input(key="bqca_table").value == "bqca_only_logs"
  assert "`test-project.test_dataset.bqca_only_logs`" in bq.last("KPIs").sql


def test_bqca_table_id_beats_bq_table_id_in_every_query(
    monkeypatch, bqca_app, bq
):
  monkeypatch.setenv("BQ_TABLE_ID", "shared_events")
  monkeypatch.setenv("BQCA_TABLE_ID", "bqca_only_logs")
  at = bqca_app.run()
  assert not at.exception
  assert at.sidebar.text_input(key="bqca_table").value == "bqca_only_logs"
  assert bq.calls
  for call in bq.calls:
    assert "`test-project.test_dataset.bqca_only_logs`" in call.sql, call.label
    assert "shared_events" not in call.sql, call.label


def test_a_blank_bqca_table_id_falls_through_to_bq_table_id(
    monkeypatch, bqca_app, bq
):
  monkeypatch.setenv("BQCA_TABLE_ID", "")
  monkeypatch.setenv("BQ_TABLE_ID", "shared_events")
  at = bqca_app.run()
  assert at.sidebar.text_input(key="bqca_table").value == "shared_events"
  assert "`test-project.test_dataset.shared_events`" in bq.last("KPIs").sql


def test_bqca_table_id_does_not_reach_the_adk_surface(
    monkeypatch, bq, adk_stubs
):
  monkeypatch.setenv("BQCA_TABLE_ID", "bqca_only_logs")
  monkeypatch.setenv("BQ_TABLE_ID", "agent_events")
  at = _app().run()
  assert not at.exception
  assert _surface(at).value == ADK_SURFACE
  table = next(
      box for box in at.sidebar.text_input if box.label == "Events table"
  )
  assert table.value == "agent_events"


def test_project_is_locked_when_the_environment_names_it(bqca_app):
  at = bqca_app.run()
  assert at.sidebar.text_input(key="bqca_project").proto.disabled is True


def test_missing_dataset_asks_for_connection_details(monkeypatch, bqca_app, bq):
  monkeypatch.setenv("BQ_DATASET_ID", "")
  at = bqca_app.run()
  assert not at.exception
  assert any("bqca_prompt_response_logs" in info.value for info in at.info)
  assert bq.calls == []
  assert not at.tabs


def test_malformed_identifiers_are_rejected_before_any_query(bqca_app, bq):
  at = bqca_app.run()
  bq.calls.clear()
  at.sidebar.text_input(key="bqca_table").input("logs`; DROP TABLE x; --")
  _sidebar_button(at, "Connect").click().run()
  assert not at.exception
  assert any("Invalid table ID" in e.value for e in at.sidebar.error)
  assert bq.calls == []


def test_a_new_connection_resets_the_applied_filters(bqca_app, bq):
  at = bqca_app.run()
  at.sidebar.multiselect(key="bqca_flt_data_agent").select("DA1")
  _sidebar_button(at, "Apply filters").click().run()
  assert at.session_state["_bqca_applied"]["data_agent_ids"] == ("DA1",)

  at.sidebar.text_input(key="bqca_dataset").input("other_dataset")
  _sidebar_button(at, "Connect").click().run()
  assert not at.exception
  assert at.session_state["_bqca_applied"]["data_agent_ids"] == ()
  call = bq.last("KPIs")
  assert call.ctx.refs.dataset == "other_dataset"
  assert call.params["data_agent_ids"] == (models.ALL_SENTINEL,)


def test_lazy_mode_queries_only_the_active_tab(monkeypatch, bqca_app, bq):
  monkeypatch.setenv("STREAMLIT_LAZY_TABS", "true")
  at = bqca_app.run()
  assert not at.exception
  assert not at.tabs
  assert set(bq.labels()) == {
      "Filter options",
      "KPIs",
      "Turn volume",
      "Latency",
  }
  control = at.segmented_control(key="_bqca_active_tab")
  assert list(control.options) == BQCA_TABS
  assert control.value == BQCA_TABS[0]

  bq.calls.clear()
  control.set_value("Error Attribution").run()
  assert not at.exception
  assert set(bq.labels()) == {"Filter options", "KPIs", "Error attribution"}
  assert len(at.get("plotly_chart")) == 1


def test_an_empty_window_shows_dashes_and_empty_states(bqca_app, bq):
  bq.frames = {}
  at = bqca_app.run()
  assert not at.exception
  assert len(at.metric) == len(KPI_LABELS)
  assert set(_metrics(at).values()) == {"—"}
  assert len(at.get("plotly_chart")) == 0
  assert len(at.code) == 0
  captions = _captions(at)
  for message in (
      "No matching data in this range.",
      "No completed turns in this range.",
      "No LLM responses in this range.",
      "No data-agent turns in this range.",
      "No persona turns in this range.",
      "No turns match the filters in this range.",
      "No embedding suggestions in this range.",
      "No errors in this range.",
  ):
    assert message in captions, message


def test_failed_queries_never_crash_the_dashboard(bqca_app, bq):
  bq.errors = {label: "boom" for label in PANEL_FRAMES}
  at = bqca_app.run()
  assert not at.exception
  assert set(_metrics(at).values()) == {"—"}
  assert len(at.get("plotly_chart")) == 0


def test_one_failed_query_leaves_the_other_panels_alone(bqca_app, bq):
  bq.errors = {"KPIs": "boom"}
  at = bqca_app.run()
  assert not at.exception
  metrics = _metrics(at)
  assert {metrics[label] for label in KPI_LABELS} == {"—"}
  # The turn detail is fed by another query and keeps its own metrics.
  assert metrics["Status"] == "OK"
  assert len(at.get("plotly_chart")) == len(CHARTS)


def test_undefined_rates_show_dashes_while_counts_stay_numbers(bqca_app, bq):
  frame = _kpi_frame()
  frame[
      [
          "p50_turn_latency_ms",
          "p95_turn_latency_ms",
          "fast_path_rate",
          "embedding_coverage",
      ]
  ] = NAN
  frame["total_turns"] = 2
  frame["turn_error_rate"] = 1.0
  bq.frames["KPIs"] = frame
  metrics = _metrics(bqca_app.run())
  assert metrics["Total Turns"] == "2"
  assert metrics["Turn Error Rate"] == "100.0%"
  for label in (
      "P50 Turn Latency",
      "P95 Turn Latency",
      "Fast-Path Rate",
      "Embedding Suggestion Coverage",
  ):
    assert metrics[label] == "—", label


def test_footer_reports_the_scan_log_of_the_bqca_queries(bqca_app):
  at = bqca_app.run()
  assert any(
      f"{len(PANEL_FRAMES)} queries this run" in caption
      for caption in _captions(at)
  )


def test_hostile_logged_text_never_renders_as_html(bqca_app, bq):
  hostile = "<img src=x onerror=alert(1)>"
  turns = _turns_frame()
  turns["user_prompt"] = hostile
  turns["agent_response"] = f"{hostile} **bold**"
  turns["error_message"] = hostile
  turns["persona"] = hostile
  turns["data_agent_id"] = hostile
  bq.frames["Turns"] = turns
  for label in ("Data agents", "Personas", "Error attribution"):
    frame = bq.frames[label].copy()
    frame[frame.columns[0]] = hostile
    bq.frames[label] = frame
  at = bqca_app.run()
  assert not at.exception
  assert all(not markdown.proto.allow_html for markdown in at.markdown)
  assert [text.value for text in at.text] == [hostile]
  assert [error.value for error in at.error] == [hostile]


def test_logged_markdown_images_are_shown_as_links_never_fetched(bqca_app, bq):
  turns = _turns_frame()
  turns["user_prompt"] = "see ![p](https://tracker.example/p.png) and ![q](u)"
  turns["agent_response"] = (
      "Revenue is up.\n\n![chart](https://tracker.example/pixel.png?u=alice)"
      "\n\nAnd !![x](//tracker.example/b.png) too."
  )
  turns["error_message"] = "failed: ![e](https://tracker.example/e.png)"
  turns["data_agent_id"] = "![a](https://tracker.example/a.png)"
  bq.frames["Turns"] = turns
  at = bqca_app.run()
  assert not at.exception

  # The answer is Markdown, with every image turned into a plain link.
  answers = [m.value for m in at.markdown if "Revenue is up." in m.value]
  assert len(answers) == 1
  assert "![" not in answers[0]
  assert (
      "[image: chart](https://tracker.example/pixel.png?u=alice)" in answers[0]
  )
  assert "[image: x](//tracker.example/b.png)" in answers[0]
  # ``st.error`` renders Markdown too.
  assert [e.value for e in at.error] == [
      "failed: [image: e](https://tracker.example/e.png)"
  ]
  # The picker's labels can render Markdown as well.
  labels = _selectbox(at, "Turn").options
  assert labels
  assert all("![" not in label for label in labels)
  assert any(
      "[image: a](https://tracker.example/a.png)" in label for label in labels
  )
  # The prompt is a literal text element, and an id in the detail line sits in a
  # code span: neither renders Markdown, so neither is touched.
  assert [t.value for t in at.text] == [
      "see ![p](https://tracker.example/p.png) and ![q](u)"
  ]
  [detail] = [c for c in _captions(at) if c.startswith("Data agent ")]
  assert "`![a](https://tracker.example/a.png)`" in detail
  # No Markdown element anywhere carries a live image opener.
  assert not any("![" in m.value for m in at.markdown)


def test_a_turn_with_several_responses_shows_the_last_and_says_so(bqca_app, bq):
  bq.frames["Turns"] = _turns_frame().assign(agent_response_count=[2, 1, 0, 1])
  at = bqca_app.run()
  assert not at.exception
  notes = [c for c in _captions(at) if "AGENT_RESPONSE events" in c]
  assert len(notes) == 1
  assert notes[0].startswith("This turn logged 2 AGENT_RESPONSE events.")
  assert "last one, the answer that was served, is shown" in notes[0]
  # A turn with a single response needs no explanation.
  _selectbox(at, "Turn").select_index(1).run()
  assert not at.exception
  assert not any("AGENT_RESPONSE events" in c for c in _captions(at))


def test_a_frame_without_response_counts_shows_no_superseded_note(bqca_app):
  at = bqca_app.run()
  assert not at.exception
  assert not any("AGENT_RESPONSE events" in c for c in _captions(at))


# --------------------------------------------------------------------------- #
# Review fixes, round 2 (surface): a turn without a usable id, a scope without #
# turns, and percentiles labelled as the estimates they are                    #
# --------------------------------------------------------------------------- #

NO_TURN_ID_NOTE = "No valid invocation ID is associated with this turn."


def _zero_turn_kpi_frame() -> pd.DataFrame:
  """A KPI row for a scope whose events all lack an ``invocation_id``.

  The tokens are real (they come from events outside any turn); everything that
  is defined per turn is zero, or an undefined (NULL) rate.
  """
  return _kpi_frame().assign(
      total_turns=0,
      completed_turns=0,
      error_events=2,
      turn_error_rate=NAN,
      p50_turn_latency_ms=NAN,
      p95_turn_latency_ms=NAN,
      fast_path_rate=NAN,
      embedding_coverage=NAN,
  )


def test_a_turn_without_a_usable_invocation_id_opens_without_a_timeline(
    bqca_app, bq
):
  # The turns query never lists a blank key any more (an empty id is keyed by
  # its timestamp), so this guards a malformed frame, not data the SQL returns.
  turns = _turns_frame()
  turns.loc[0, "invocation_id"] = "   "
  bq.frames["Turns"] = turns
  at = bqca_app.run()
  assert not at.exception
  assert NO_TURN_ID_NOTE in _captions(at)
  # The timeline is keyed on the id: with none there is nothing to look up.
  assert "Turn timeline" not in bq.labels()

  # Picking a turn with a real id brings the timeline back.
  _selectbox(at, "Turn").select_index(1).run()
  assert not at.exception
  assert NO_TURN_ID_NOTE not in _captions(at)
  assert bq.last("Turn timeline").params["invocation_id"] == "inv2"


SYNTHESIZED_TURN_KEY = "2026-10-05 09:00:00.250000+00"


def test_a_turn_logged_with_an_empty_id_opens_by_its_timestamp_key(
    bqca_app, bq
):
  turns = _turns_frame()
  turns.loc[0, "invocation_id"] = SYNTHESIZED_TURN_KEY
  bq.frames["Turns"] = turns
  at = bqca_app.run()
  assert not at.exception
  # The key is a turn like any other: listed, selectable and expandable.
  assert NO_TURN_ID_NOTE not in _captions(at)
  assert _selectbox(at, "Turn").value == SYNTHESIZED_TURN_KEY
  assert bq.last("Turn timeline").params["invocation_id"] == (
      SYNTHESIZED_TURN_KEY
  )
  assert f"invocation `{SYNTHESIZED_TURN_KEY}`" in _detail_caption(at)

  # And it stays selectable between real ids.
  _selectbox(at, "Turn").select_index(1).run()
  assert bq.last("Turn timeline").params["invocation_id"] == "inv2"
  _selectbox(at, "Turn").select_index(0).run()
  assert not at.exception
  assert bq.last("Turn timeline").params["invocation_id"] == (
      SYNTHESIZED_TURN_KEY
  )


def test_a_scope_without_turns_shows_a_dash_and_explains_the_tokens(
    bqca_app, bq
):
  bq.frames["KPIs"] = _zero_turn_kpi_frame()
  at = bqca_app.run()
  assert not at.exception
  metrics = _metrics(at)
  assert metrics["Total Turns"] == "0"
  # Two error events but no turn to be a rate of: a dash, never a 0.0%.
  assert metrics["Turn Error Rate"] == "—"
  # The tokens are real, and the caption says where they come from.
  assert metrics["Total Tokens"] == "130"
  [note] = [c for c in _captions(at) if c.startswith("0 attributed turns")]
  # A blank id is a turn of its own now: only an event with no id at all is
  # left without one.
  assert "have no invocation_id" in note
  assert "blank" not in note and "missing" not in note
  assert "token totals include unattributed LLM_RESPONSE events" in note
  # The "turns completed" line only ever speaks of real turns.
  assert _completion_notes(at) == []


def test_the_no_turns_note_appears_only_for_tokens_without_turns(bqca_app, bq):
  def notes(at: AppTest) -> list[str]:
    return [c for c in _captions(at) if c.startswith("0 attributed turns")]

  at = bqca_app.run()
  assert not at.exception
  assert notes(at) == []

  # No turn and no token either: nothing in scope, so nothing to explain.
  bq.frames["KPIs"] = _zero_turn_kpi_frame().assign(
      total_tokens=0, thoughts_tokens=0, cached_tokens=0, error_events=0
  )
  at = bqca_app.run()
  assert not at.exception
  assert notes(at) == []
  assert _metrics(at)["Turn Error Rate"] == "—"


def test_a_real_zero_error_rate_still_reads_zero_percent(bqca_app, bq):
  bq.frames["KPIs"] = _kpi_frame().assign(error_events=0, turn_error_rate=0.0)
  at = bqca_app.run()
  assert not at.exception
  assert _metrics(at)["Turn Error Rate"] == "0.0%"


def test_the_latency_percentiles_are_labelled_as_approximate(bqca_app):
  at = bqca_app.run()
  assert not at.exception
  helps = {metric.label: metric.help for metric in at.metric}
  for label in ("P50 Turn Latency", "P95 Turn Latency"):
    assert helps[label].startswith("Approximate "), label
    assert "APPROX_QUANTILES" in helps[label], label
    assert "completed turns" in helps[label], label

  captions = _captions(at)
  # The incomplete-turn note, the latency tab and the per-agent P95 all say so.
  [note] = _completion_notes(at)
  assert "approximate" in note
  assert "APPROX_QUANTILES" in note
  assert "cover completed turns only" in note
  [latency] = [c for c in captions if c.startswith("Latency percentiles are")]
  assert "approximate" in latency
  assert "APPROX_QUANTILES" in latency
  [agents] = [
      c for c in captions if c.startswith("Turns that carry no data-agent id")
  ]
  assert "P95 latency is approximate" in agents
  assert "APPROX_QUANTILES" in agents


# --------------------------------------------------------------------------- #
# Governance and hygiene: the BQCA surface may only ever read the nine public
# event types, adds no dependency, and must not weigh on the ADK surface.
# --------------------------------------------------------------------------- #

BQCA_MODULE_FILES = (
    DASHBOARDS_DIR / "bqca_models.py",
    DASHBOARDS_DIR / "bqca_queries.py",
    DASHBOARDS_DIR / "bqca_charts.py",
)

# Besides the standard library, the BQCA modules may import only what the ADK
# dashboard already requires, plus their own siblings.
RUNTIME_PACKAGES = {"google", "pandas", "plotly", "streamlit"}
DASHBOARD_MODULES = {
    "bqca_charts",
    "bqca_models",
    "bqca_queries",
    "charts",
    "models",
    "queries",
}

# Anything shaped like an agent-analytics event type.
EVENT_TYPE_LITERAL = re.compile(
    r"\b[A-Z][A-Z_]*_"
    r"(?:STARTING|COMPLETED|RECEIVED|RESPONSE|REQUEST|SUGGESTION|ERROR)\b"
)


def _between(source: str, start: str, end: str) -> str:
  """Returns ``source`` from the ``start`` marker up to the next ``end``."""
  first = source.find(start)
  assert first >= 0, f"marker {start!r} not found"
  last = source.find(end, first + len(start))
  assert last > first, f"marker {end!r} not found after {start!r}"
  return source[first:last]


def _bqca_app_source() -> str:
  """Returns the parts of ``app.py`` that belong to the BQCA surface.

  ``app.py`` also holds the ADK dashboard, whose own vocabulary is not under
  test here, so only the BQCA constants and the BQCA surface are cut out.
  """
  source = APP_PATH.read_text(encoding="utf-8")
  constants = _between(
      source, 'ADK_SURFACE = "ADK Agents"', "def _seed_options("
  )
  surface = _between(
      source, "# BQCA Prompt & Response Logging surface", "def main() -> None:"
  )
  return constants + surface


@pytest.fixture(scope="module")
def bqca_sources() -> dict[str, str]:
  sources = {
      path.name: path.read_text(encoding="utf-8") for path in BQCA_MODULE_FILES
  }
  sources["app.py (BQCA surface)"] = _bqca_app_source()
  return sources


def test_bqca_sources_never_name_a_forbidden_event_type(bqca_sources):
  for name, text in bqca_sources.items():
    for forbidden in FORBIDDEN_EVENT_TYPES:
      assert forbidden.lower() not in text.lower(), f"{name} names {forbidden}"


def test_bqca_sources_name_exactly_the_nine_public_event_types(bqca_sources):
  named = set()
  for text in bqca_sources.values():
    named.update(EVENT_TYPE_LITERAL.findall(text))
  assert named == ALLOWED_EVENT_TYPES, sorted(named ^ ALLOWED_EVENT_TYPES)


@pytest.mark.parametrize(
    "path", [*BQCA_MODULE_FILES, APP_PATH], ids=lambda path: path.name
)
def test_bqca_surface_adds_no_dependency(path):
  imported = set()
  for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
    if isinstance(node, ast.Import):
      imported.update(alias.name.split(".")[0] for alias in node.names)
    elif isinstance(node, ast.ImportFrom) and node.level == 0:
      imported.add((node.module or "").split(".")[0])
  allowed = set(sys.stdlib_module_names) | RUNTIME_PACKAGES | DASHBOARD_MODULES
  allowed.add("dotenv")  # app.py loads .env; pre-dates the BQCA surface.
  assert imported <= allowed, sorted(imported - allowed)


def _python(code: str, *args: str, cwd: Path) -> subprocess.CompletedProcess:
  """Runs ``code`` in a fresh interpreter that has never seen this test run."""
  env = dict(os.environ)
  env.update(
      BQAA_DASHBOARD_SKIP_DOTENV="1",
      GOOGLE_APPLICATION_CREDENTIALS="",
      PYTHONDONTWRITEBYTECODE="1",
  )
  return subprocess.run(
      [sys.executable, "-c", textwrap.dedent(code), *args],
      capture_output=True,
      check=False,
      cwd=cwd,
      env=env,
      text=True,
      timeout=180,
  )


def test_importing_app_never_loads_the_bqca_modules(tmp_path):
  result = _python(
      """
      import sys

      sys.path.insert(0, sys.argv[1])
      import app

      loaded = sorted(name for name in sys.modules if name.startswith("bqca_"))
      assert not loaded, f"app imported the BQCA modules eagerly: {loaded}"
      assert callable(app.main_bqca)
      missing = [name for name in app.__all__ if not hasattr(app, name)]
      assert not missing, f"__all__ lists undefined names: {missing}"
      """,
      str(DASHBOARDS_DIR),
      cwd=tmp_path,
  )
  assert result.returncode == 0, result.stderr


def test_bqca_modules_import_on_their_own(tmp_path):
  result = _python(
      """
      import sys

      sys.path.insert(0, sys.argv[1])
      import bqca_charts, bqca_models, bqca_queries

      assert "app" not in sys.modules, "the BQCA modules must not import app"
      assert bqca_models.BQCA_DEFAULT_TABLE_ID == "bqca_prompt_response_logs"
      assert callable(bqca_queries.fetch_panel)
      assert callable(bqca_charts.turn_volume_chart)
      """,
      str(DASHBOARDS_DIR),
      cwd=tmp_path,
  )
  assert result.returncode == 0, result.stderr


# --------------------------------------------------------------------------- #
# Documentation: how to open, connect and read the BQCA dashboards, and the
# cross-links between the READMEs and the BQCA manual.
# --------------------------------------------------------------------------- #

README_BQCA_HEADING = "## 6. BQCA Prompt & Response Logging Dashboard"
MANUAL_BQCA_HEADING = (
    "### 7.4 Visualizing Logs with the BQCA Analytics Dashboards"
    " (Looker Studio & Streamlit)"
)
MANUAL_BQCA_ANCHOR = (
    "74-visualizing-logs-with-the-bqca-analytics-dashboards"
    "-looker-studio--streamlit"
)
README_BQCA_ANCHOR = "6-bqca-prompt--response-logging-dashboard"
MARKDOWN_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")


def _markdown_headings(text: str) -> list[tuple[int, str, str]]:
  """Lists ``(line_index, level, title)`` of the headings outside code fences.

  Shell comments inside fenced code blocks look like level-one headings, so the
  fences are tracked.
  """
  headings = []
  in_fence = False
  for index, line in enumerate(text.splitlines()):
    if line.lstrip().startswith("```"):
      in_fence = not in_fence
      continue
    match = re.match(r"(#{1,6}) (.+)", line)
    if match and not in_fence:
      headings.append((index, match.group(1), line.strip()))
  return headings


def _markdown_section(text: str, heading: str) -> str:
  """Returns the section that opens with ``heading``, subsections included."""
  lines = text.splitlines()
  found = [h for h in _markdown_headings(text) if h[2] == heading]
  assert found, f"heading {heading!r} not found"
  start, level, _ = found[0]
  end = len(lines)
  for index, other_level, _ in _markdown_headings(text):
    if index > start and len(other_level) <= len(level):
      end = index
      break
  return "\n".join(lines[start:end])


def _anchors(text: str) -> set[str]:
  """Returns the GitHub-style anchor of every heading."""
  anchors = set()
  for _, _, line in _markdown_headings(text):
    title = line.lstrip("#").strip().lower()
    anchors.add(re.sub(r"[^\w\- ]", "", title).replace(" ", "-"))
  return anchors


def _broken_links(markdown: str, document: Path) -> list[str]:
  """Lists the relative links in ``markdown`` that do not resolve.

  Args:
    markdown: The text to check.
    document: The file the text lives in; relative links resolve against it.

  Returns:
    The targets whose file is missing, or whose ``#anchor`` is not a heading of
    the markdown file they point at.
  """
  broken = []
  for target in MARKDOWN_LINK.findall(markdown):
    if re.match(r"[a-z][a-z0-9+.-]*:", target):
      continue
    path, _, anchor = target.partition("#")
    destination = (document.parent / path).resolve() if path else document
    if not destination.is_file():
      broken.append(target)
    elif anchor and destination.suffix == ".md":
      if anchor not in _anchors(destination.read_text(encoding="utf-8")):
        broken.append(target)
  return broken


def _lines_about(text: str, *words: str) -> str:
  """Returns the lines of ``text`` that mention any of ``words``."""
  return "\n".join(
      line
      for line in text.splitlines()
      if any(word in line.lower() for word in words)
  )


@pytest.fixture(scope="module")
def docs() -> dict[str, str]:
  """The BQCA-specific text of every document the dashboard touches."""
  readme = README_PATH.read_text(encoding="utf-8")
  manual = MANUAL_PATH.read_text(encoding="utf-8")
  root = ROOT_README_PATH.read_text(encoding="utf-8")
  return {
      "readme": readme,
      "readme_intro": readme.split("\n---\n", 1)[0],
      "readme_bqca": _markdown_section(readme, README_BQCA_HEADING),
      "manual": manual,
      "manual_bqca": _markdown_section(manual, MANUAL_BQCA_HEADING),
      "root": root,
      "root_bqca": _lines_about(root, "bqca", "streamlit"),
  }


def test_streamlit_readme_documents_the_bqca_surface(docs):
  for needle in (
      "?profile=bqca",
      "BQAA_PROFILE=bqca",
      "Dashboard Surface",
      "bqca_prompt_response_logs",
      "BQ_TABLE_ID",
      "BQ_VIEW_PREFIX",
      "Apply filters",
      "data-agent-id",
      "maximum_bytes_billed",
      "Table view",
      *BQCA_TABS,
      *sorted(ALLOWED_EVENT_TYPES),
  ):
    assert needle in docs["readme_bqca"], needle
  assert "| `BQAA_PROFILE`" in docs["readme"]


def test_streamlit_readme_intro_names_both_surfaces(docs):
  assert ADK_SURFACE in docs["readme_intro"]
  assert BQCA_SURFACE in docs["readme_intro"]
  assert f"(#{README_BQCA_ANCHOR})" in docs["readme_intro"]
  assert README_BQCA_ANCHOR in _anchors(docs["readme"])


def test_manual_section_7_4_documents_both_dashboards(docs):
  for needle in (
      "hydrate_dashboard.py",
      "--profile bqca",
      "/bqca/",
      "pip install -e '.[streamlit]'",
      "streamlit run app.py",
      "BQAA_PROFILE=bqca",
      "?profile=bqca",
      "bqca_prompt_response_logs",
      "agent_events",
      "Apply filters",
  ):
    assert needle in docs["manual_bqca"], needle


def test_streamlit_readme_documents_the_review_fixes(docs):
  section = docs["readme_bqca"]
  for needle in (
      "BQCA_TABLE_ID",
      "Embedding Suggestion Coverage",
      "INVOCATION_COMPLETED",
      "completed turns only",
      "A turn counts once in a latency percentile",
      "an agent's fast-path rate and latency cover its completed turns",
      "shows the last one, the answer that was served",
      "embedding suggestions by reason",
      "columns suggested per suggestion",
      "never fetched",
      "email address",
      "`model_version`",
      "`suggested_columns`",
      "`usage_metadata`",
  ):
    assert needle in section, needle
  assert "| `BQCA_TABLE_ID`" in docs["readme"]
  # The retired wording about a per-suggestion hit rate is gone.
  for stale in ("Hit Rate", "hits and misses", "similar queries matched"):
    assert stale not in section, stale


def test_streamlit_readme_orders_the_table_variables(docs):
  events_table = _lines_about(docs["readme_bqca"], "events table")
  # BQCA_TABLE_ID is consulted first, then the shared BQ_TABLE_ID, then the
  # default table the same sentence names.
  assert events_table.index("BQCA_TABLE_ID") < events_table.index("BQ_TABLE_ID")
  assert "bqca_prompt_response_logs" in events_table


README_VERIFICATION_HEADING = "### Verification against a live logging table"


def test_streamlit_readme_documents_the_live_table_verification(docs):
  section = _markdown_section(docs["readme"], README_VERIFICATION_HEADING)
  # It sits inside the BQCA section, so the event-type rules cover it too.
  assert section in docs["readme_bqca"]
  # The reproducible test command, the five tabs, the nine tiles, and the
  # turn-key fallback chain.
  for needle in (
      *BQCA_TABS,
      *KPI_LABELS,
      "15-column",
      "pytest tests/test_dashboards_streamlit_bqca.py -q",
      "NULLIF(TRIM(trace_id), '')",
      "NULLIF(TRIM(session_id), '')",
  ):
    assert needle in section, needle
  # A blank id is told apart from a missing one: the first is still a turn.
  assert "empty `invocation_id`" in section


def test_streamlit_readme_verification_names_no_real_project(docs):
  section = _markdown_section(docs["readme"], README_VERIFICATION_HEADING)
  # No address, no project id (they end in digits) and no table path other than
  # the placeholder: the table that was checked is not named.
  assert "@" not in section
  project_id = re.compile(r"\b[a-z][a-z0-9]*(?:-[a-z0-9]+)*-\d{3,}\b")
  assert project_id.findall(section) == []
  paths = re.findall(r"`([^`\s]*\.[^`\s]*\.[^`\s]*)`", section)
  assert set(paths) <= {"<project>.<dataset>.<table>"}, paths


def test_streamlit_readme_says_what_counts_as_a_turn(docs):
  section = _markdown_section(docs["readme"], "### What you see")
  # An empty id is a turn of its own, identified by trace_id, session_id, or
  # timestamp; an event with no id at all belongs to no turn.
  for needle in (
      "empty `invocation_id` counts as a turn of its own",
      "trimmed `trace_id`, else its trimmed `session_id`, else its timestamp",
      "no `invocation_id` at all belongs to no turn",
  ):
    assert needle in section, needle


def test_streamlit_readme_documents_the_per_turn_attribution(docs):
  section = docs["readme_bqca"]
  for needle in (
      "across all the events of the turn",
      "wins over a handle logged by an earlier one",
      "*unattributed* included",
      "ignore every filter",
  ):
    assert needle in section, needle


def test_manual_section_7_4_documents_the_review_fixes(docs):
  section = docs["manual_bqca"]
  assert "BQCA_TABLE_ID=YOUR_LOGS_TABLE" in section
  for needle in (
      "BQCA_TABLE_ID",
      "BQ_TABLE_ID",
      "Embedding Suggestion Coverage",
      "INVOCATION_COMPLETED",
      "completed turns only",
      "the last response of the turn",
      "PENDING_PUBLIC_SHARING_ALLOWLIST",
      "--custom-sql-out",
  ):
    assert needle in section, needle
  for stale in ("how often embedding suggestions match", "Hit Rate"):
    assert stale not in section, stale


def test_manual_section_7_4_sits_inside_section_7(docs):
  manual = docs["manual"]
  assert (
      manual.index("### 7.3 ")
      < manual.index(MANUAL_BQCA_HEADING)
      < manual.index("## 8. FAQ")
  )
  assert MANUAL_BQCA_ANCHOR in _anchors(manual)
  contents = _markdown_section(manual, "## Table of Contents")
  assert f"(#{MANUAL_BQCA_ANCHOR})" in contents
  seven_one = _markdown_section(
      manual,
      "### 7.1 Connecting `v_bqca_customer_turns` to Looker Studio or BI"
      " Dashboards",
  )
  assert f"(#{MANUAL_BQCA_ANCHOR})" in seven_one


def test_root_readme_highlights_the_bqca_dashboards(docs):
  assert "dashboards/streamlit/README.md" in docs["root_bqca"]
  assert (
      f"docs/guides/bqca-prompt-response-logging-manual.md#{MANUAL_BQCA_ANCHOR}"
      in docs["root_bqca"]
  )
  assert "Streamlit" in docs["root"].split("**Evaluation**", 1)[0]


def test_bqca_docs_never_name_a_forbidden_event_type(docs):
  for name in ("readme_intro", "readme_bqca", "manual_bqca", "root_bqca"):
    for forbidden in FORBIDDEN_EVENT_TYPES:
      assert forbidden.lower() not in docs[name].lower(), (name, forbidden)


def test_bqca_docs_only_name_public_event_types(docs):
  for name in ("readme_bqca", "manual_bqca", "root_bqca"):
    named = set(EVENT_TYPE_LITERAL.findall(docs[name]))
    assert named <= ALLOWED_EVENT_TYPES, (name, sorted(named))


def test_bqca_docs_cross_links_resolve(docs):
  manual_contents = _markdown_section(docs["manual"], "## Table of Contents")
  checked = (
      (docs["readme_intro"] + "\n" + docs["readme_bqca"], README_PATH),
      (docs["manual_bqca"] + "\n" + manual_contents, MANUAL_PATH),
      (docs["root_bqca"], ROOT_README_PATH),
  )
  for markdown, document in checked:
    assert MARKDOWN_LINK.findall(markdown), document
    assert _broken_links(markdown, document) == [], document


def test_doc_helpers_understand_fences_and_anchors():
  text = "# Title\n\n```bash\n# not a heading\n```\n\n## 6. A & B (C)\n"
  assert [title for _, _, title in _markdown_headings(text)] == [
      "# Title",
      "## 6. A & B (C)",
  ]
  assert _anchors(text) == {"title", "6-a--b-c"}
  assert _markdown_section(text, "# Title") == text.rstrip("\n")
  assert _markdown_section(text, "## 6. A & B (C)") == "## 6. A & B (C)"
  with pytest.raises(AssertionError, match="not found"):
    _markdown_section(text, "## Missing")
