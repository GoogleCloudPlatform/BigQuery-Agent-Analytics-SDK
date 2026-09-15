"""Unit tests for the Streamlit dashboard charts module.

Tests chart construction, color mapping, and panel layout using real pandas
and plotly objects (no mock pandas/plotly).
"""

from __future__ import annotations

from pathlib import Path
import sys
from unittest import mock

import pytest

pytest.importorskip("streamlit")
pytest.importorskip("plotly")

import pandas as pd
import plotly.graph_objects as go

DASHBOARDS_DIR = (
    Path(__file__).resolve().parents[1] / "dashboards" / "streamlit"
)
if str(DASHBOARDS_DIR) not in sys.path:
  sys.path.insert(0, str(DASHBOARDS_DIR))

import charts
import models


@pytest.fixture
def sample_context() -> models.Context:
  """Returns a sample context for chart builder tests."""
  refs = models.TableRefs(
      project="test-project",
      dataset="test_dataset",
      table="test_events",
      view_prefix="test_",
  )
  window = models.make_window(models.TIME_RANGES["Last 24 hours"])
  return models.Context(
      refs=refs,
      window=window,
      filters=models.Filters(),
      max_bytes=1_000_000,
      theme=models.LIGHT_THEME,
      price_in=1.25,
      price_out=5.00,
  )


@pytest.fixture(autouse=True)
def clean_session_state():
  """Provides a clean dictionary for streamlit session_state during chart tests."""
  state: dict[str, object] = {}
  with mock.patch.object(charts.st, "session_state", state):
    yield state


def test_base_figure():
  """Test layout height, paper_bgcolor, xaxis/yaxis styling with light and dark themes."""
  # Light theme test
  fig_light = charts.base_figure(models.LIGHT_THEME, height=400)
  assert isinstance(fig_light, go.Figure)
  assert fig_light.layout.height == 400
  assert fig_light.layout.paper_bgcolor == models.LIGHT_THEME.surface
  assert fig_light.layout.plot_bgcolor == models.LIGHT_THEME.surface
  assert fig_light.layout.xaxis.gridcolor == models.LIGHT_THEME.grid
  assert fig_light.layout.xaxis.linecolor == models.LIGHT_THEME.axis
  assert fig_light.layout.xaxis.showline is True
  assert fig_light.layout.yaxis.showline is False
  assert fig_light.layout.showlegend is False

  # Dark theme test
  fig_dark = charts.base_figure(models.DARK_THEME, height=320)
  assert isinstance(fig_dark, go.Figure)
  assert fig_dark.layout.height == 320
  assert fig_dark.layout.paper_bgcolor == models.DARK_THEME.surface
  assert fig_dark.layout.plot_bgcolor == models.DARK_THEME.surface
  assert fig_dark.layout.xaxis.gridcolor == models.DARK_THEME.grid
  assert fig_dark.layout.xaxis.linecolor == models.DARK_THEME.axis
  assert fig_dark.layout.xaxis.showline is True
  assert fig_dark.layout.yaxis.showline is False


def test_ranked_bars(sample_context):
  """Test horizontal bar orientation, sorting, marker color, trace structure."""
  data = {
      "agent": [f"agent_{i}" for i in range(15)],
      "count": [i * 10 for i in range(15)],
  }
  df = pd.DataFrame(data)

  fig = charts.ranked_bars(
      df, key="agent", value="count", ctx=sample_context, height=350, top=5
  )
  assert isinstance(fig, go.Figure)
  assert len(fig.data) == 1

  trace = fig.data[0]
  assert trace.type == "bar"
  assert trace.orientation == "h"
  # top 5 largest items
  assert len(trace.x) == 5
  # Ranked bars are sorted ascending by value for horizontal display
  assert list(trace.x) == sorted(trace.x)
  assert list(trace.x) == [100, 110, 120, 130, 140]
  assert list(trace.y) == [
      "agent_10",
      "agent_11",
      "agent_12",
      "agent_13",
      "agent_14",
  ]
  assert trace.marker.color == sample_context.theme.categorical[0]
  assert fig.layout.bargap == 0.35
  assert fig.layout.showlegend is False


def test_fold_others():
  """Test folding categories exceeding limit into 'Other', aggregation sum, and tie breaking."""
  # Case 1: More than limit categories
  data = {
      "cat": [f"cat_{i}" for i in range(10)],
      "val": [10 * (i + 1) for i in range(10)],  # 10, 20, ..., 100
  }
  df = pd.DataFrame(data)
  folded = charts.fold_others(df, key="cat", value="val", limit=5)

  # Should have 4 top categories + Other
  assert folded["cat"].nunique() == 5
  assert models.OTHER_LABEL in folded["cat"].values
  assert folded["val"].sum() == df["val"].sum()

  # The 4 largest are cat_9(100), cat_8(90), cat_7(80), cat_6(70) = 340
  # The remaining 6 are cat_0..cat_5 sum = 10+20+30+40+50+60 = 210
  other_val = folded[folded["cat"] == models.OTHER_LABEL]["val"].iloc[0]
  assert other_val == 210

  # Case 2: DataFrame with <= limit categories remains unchanged
  df_small = pd.DataFrame({"cat": ["a", "b"], "val": [1, 2]})
  folded_small = charts.fold_others(df_small, key="cat", value="val", limit=5)
  assert len(folded_small) == 2
  assert models.OTHER_LABEL not in folded_small["cat"].values

  # Case 3: Empty DataFrame
  empty_df = pd.DataFrame(columns=["cat", "val"])
  assert charts.fold_others(empty_df, key="cat", value="val").empty

  # Case 4: Tie breaking
  df_ties = pd.DataFrame(
      {
          "cat": [f"tie_{i}" for i in range(10)],
          "val": [50] * 10,
      }
  )
  folded_ties = charts.fold_others(df_ties, key="cat", value="val", limit=4)
  assert folded_ties["cat"].nunique() == 4
  assert models.OTHER_LABEL in folded_ties["cat"].values
  assert folded_ties["val"].sum() == 500

  # Case 5: Group columns preserved
  df_grouped = pd.DataFrame(
      {
          "group": ["g1"] * 5 + ["g2"] * 5,
          "cat": [f"c_{i}" for i in range(5)] * 2,
          "val": [10, 20, 30, 40, 50] * 2,
      }
  )
  folded_grouped = charts.fold_others(
      df_grouped, key="cat", value="val", group_cols=["group"], limit=3
  )
  assert "group" in folded_grouped.columns
  assert folded_grouped["val"].sum() == df_grouped["val"].sum()


def test_fold_others_per_group_exact_values():
  """Folding happens within each group, not across the whole frame."""
  df = pd.DataFrame(
      {
          "bucket": ["b1", "b1", "b1", "b2", "b2", "b2"],
          "cat": ["A", "B", "C", "A", "B", "C"],
          "val": [10, 5, 2, 20, 10, 3],
      }
  )
  res = charts.fold_others(df, "cat", "val", group_cols=["bucket"], limit=2)
  by_group = {(r["bucket"], r["cat"]): r["val"] for _, r in res.iterrows()}
  assert by_group[("b1", "A")] == 10
  assert by_group[("b1", models.OTHER_LABEL)] == 7  # 5 + 2
  assert by_group[("b2", "A")] == 20
  assert by_group[("b2", models.OTHER_LABEL)] == 13  # 10 + 3


def test_stacked_bars(sample_context):
  """Test trace generation, categorical color mapping, stack barmode."""
  df = pd.DataFrame(
      {
          "bucket": ["2026-01-01", "2026-01-01", "2026-01-02", "2026-01-02"],
          "agent": ["agent_a", "agent_b", "agent_a", "agent_b"],
          "count": [10, 20, 30, 40],
      }
  )

  fig = charts.stacked_bars(
      df,
      x="bucket",
      key="agent",
      value="count",
      ctx=sample_context,
      domain="agent",
      height=300,
  )

  assert isinstance(fig, go.Figure)
  assert len(fig.data) == 2
  names = [trace.name for trace in fig.data]
  assert "agent_a" in names
  assert "agent_b" in names
  assert fig.layout.barmode == "stack"
  assert fig.layout.bargap == 0.25
  assert fig.layout.hovermode == "x unified"
  assert fig.layout.showlegend is True

  # Verify colors are from theme categorical
  colors = [trace.marker.color for trace in fig.data]
  assert colors[0] in sample_context.theme.categorical
  assert colors[1] in sample_context.theme.categorical
  assert colors[0] != colors[1]


def test_grouped_bars(sample_context):
  """Test grouped traces and labels."""
  df = pd.DataFrame(
      {
          "tool": ["search", "calculator", "browser"],
          "p50": [100.0, 20.0, 500.0],
          "p99": [250.0, 45.0, 1200.0],
      }
  )

  fig = charts.grouped_bars(
      df,
      key="tool",
      series=[("p50", "Median Latency"), ("p99", "P99 Latency")],
      ctx=sample_context,
      domain="tool_lat",
      height=320,
      top=10,
  )

  assert isinstance(fig, go.Figure)
  assert len(fig.data) == 2
  assert fig.data[0].name == "Median Latency"
  assert fig.data[1].name == "P99 Latency"
  assert fig.data[0].orientation == "h"
  assert fig.data[1].orientation == "h"
  assert fig.layout.barmode == "group"
  assert fig.layout.bargap == 0.3
  assert fig.layout.bargroupgap == 0.08
  assert fig.layout.showlegend is True


def test_lines(sample_context):
  """Test line traces, connectgaps, annotations."""
  df = pd.DataFrame(
      {
          "bucket": [
              "2026-01-01 00:00",
              "2026-01-01 01:00",
              "2026-01-01 02:00",
          ],
          "prompt": [100, 150, 200],
          "completion": [50, 75, 100],
      }
  )

  fig = charts.lines(
      df,
      x="bucket",
      series=[("prompt", "Prompt Tokens"), ("completion", "Completion Tokens")],
      ctx=sample_context,
      domain="tokens",
      height=320,
      unit=" tokens",
  )

  assert isinstance(fig, go.Figure)
  assert len(fig.data) == 2

  for trace in fig.data:
    assert trace.mode == "lines"
    assert trace.connectgaps is False
    assert "tokens" in trace.hovertemplate

  # For series count <= 4, end direct annotations are added
  assert len(fig.layout.annotations) == 2
  annotation_texts = [ann.text for ann in fig.layout.annotations]
  assert "Prompt Tokens" in annotation_texts
  assert "Completion Tokens" in annotation_texts
  assert fig.layout.showlegend is True
  assert fig.layout.hovermode == "x unified"


def test_lines_more_than_four_series_shows_legend(sample_context):
  """Verify lines() with > 4 series suppresses direct annotations and enables showlegend."""
  df = pd.DataFrame(
      {
          "bucket": ["2026-01-01", "2026-01-02", "2026-01-03"],
          "s1": [10, 20, 30],
          "s2": [15, 25, 35],
          "s3": [20, 30, 40],
          "s4": [25, 35, 45],
          "s5": [30, 40, 50],
      }
  )
  series = [
      ("s1", "Series 1"),
      ("s2", "Series 2"),
      ("s3", "Series 3"),
      ("s4", "Series 4"),
      ("s5", "Series 5"),
  ]

  fig = charts.lines(
      df,
      x="bucket",
      series=series,
      ctx=sample_context,
      domain="test_domain",
      height=320,
  )

  assert isinstance(fig, go.Figure)
  assert len(fig.data) == 5
  assert fig.layout.showlegend is True
  assert fig.layout.hovermode == "x unified"
  # For series count > 4, direct labels (annotations) are omitted to avoid crowding
  assert len(fig.layout.annotations) == 0
  # Default right margin is kept (8) instead of the expanded label margin (96)
  assert fig.layout.margin.r == 8


def test_panel():
  """Test empty DataFrame caption vs chart and expander."""
  # Case 1: Empty DataFrame shows caption
  with (
      mock.patch.object(charts.st, "markdown") as mock_markdown,
      mock.patch.object(charts.st, "caption") as mock_caption,
      mock.patch.object(charts.st, "plotly_chart") as mock_plotly,
      mock.patch.object(charts.st, "expander") as mock_expander,
      mock.patch.object(charts.st, "dataframe") as mock_dataframe,
  ):
    charts.panel("Empty Panel", None, pd.DataFrame(), empty="No data here.")
    mock_markdown.assert_called_once_with("**Empty Panel**")
    mock_caption.assert_called_once_with("No data here.")
    mock_plotly.assert_not_called()
    mock_expander.assert_not_called()
    mock_dataframe.assert_not_called()

  # Case 2: Populated DataFrame with figure shows chart and collapsed expander
  fig = go.Figure()
  df = pd.DataFrame([{"a": 1, "b": 2}])

  with (
      mock.patch.object(charts.st, "markdown") as mock_markdown,
      mock.patch.object(charts.st, "caption") as mock_caption,
      mock.patch.object(charts.st, "plotly_chart") as mock_plotly,
      mock.patch.object(charts.st, "expander") as mock_expander,
      mock.patch.object(charts.st, "dataframe") as mock_dataframe,
  ):
    mock_expander.return_value.__enter__ = mock.MagicMock()
    mock_expander.return_value.__exit__ = mock.MagicMock()

    charts.panel("Active Panel", fig, df, key="chart_key")
    mock_markdown.assert_called_once_with("**Active Panel**")
    mock_caption.assert_not_called()
    mock_plotly.assert_called_once_with(fig, width="stretch", key="chart_key")
    mock_expander.assert_called_once_with("Table view", expanded=False)
    mock_dataframe.assert_called_once_with(df, width="stretch", hide_index=True)

  # Case 3: Populated DataFrame with fig=None shows expanded expander
  with (
      mock.patch.object(charts.st, "markdown") as mock_markdown,
      mock.patch.object(charts.st, "caption") as mock_caption,
      mock.patch.object(charts.st, "plotly_chart") as mock_plotly,
      mock.patch.object(charts.st, "expander") as mock_expander,
      mock.patch.object(charts.st, "dataframe") as mock_dataframe,
  ):
    mock_expander.return_value.__enter__ = mock.MagicMock()
    mock_expander.return_value.__exit__ = mock.MagicMock()

    charts.panel("Table Only Panel", None, df)
    mock_markdown.assert_called_once_with("**Table Only Panel**")
    mock_plotly.assert_not_called()
    mock_expander.assert_called_once_with("Table view", expanded=True)
    mock_dataframe.assert_called_once_with(df, width="stretch", hide_index=True)
