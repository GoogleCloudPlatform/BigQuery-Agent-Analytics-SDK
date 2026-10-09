"""Plotly chart builders for the BQCA dashboard surface.

Every function is pure: a DataFrame in (the result of the matching
``bqca_queries`` panel) and a ``go.Figure`` out, so each chart is testable
headless, without a Streamlit server. They reuse the ADK dashboard's chrome
(``charts.base_figure``), categorical slot allocation (``charts.color_map``)
and "Other" folding, so both surfaces look and behave alike: color follows
the entity rather than its rank, a chart never has two y-scales, and every
chart has a table-view twin in the app (``charts.panel``).

Customer-controlled text (data-agent ids, personas, model names, error
messages) only ever reaches Plotly as *data*: hover text is HTML-escaped, and
series names are read back through ``%{fullData.name}`` instead of being
spliced into a ``hovertemplate``.

Each builder accepts ``ctx`` as a ``models.Context``, a ``models.Theme`` or
``None`` (the active Streamlit theme), and returns an empty, themed figure
for an empty frame.
"""

from __future__ import annotations

from collections.abc import Sequence
import html
from pathlib import Path
import sys
from typing import Any

import pandas as pd
import plotly.graph_objects as go

_DASHBOARD_DIR = str(Path(__file__).resolve().parent)
if _DASHBOARD_DIR not in sys.path:
  sys.path.insert(0, _DASHBOARD_DIR)

from bqca_models import FAST_PATH_LABEL
from bqca_models import STANDARD_LABEL
from charts import active_theme
from charts import base_figure
from charts import color_map
from charts import fold_others
from models import Context
from models import OTHER_LABEL
from models import Theme

__all__ = [
    "ALL_PATHS_LABEL",
    "PATH_DISPLAY",
    "TOKEN_PARTS",
    "data_agent_leaderboard_chart",
    "embedding_suggestions_chart",
    "error_attribution_chart",
    "latency_percentiles_chart",
    "llm_latency_chart",
    "path_latency_chart",
    "persona_breakdown_chart",
    "suggested_columns_chart",
    "token_breakdown_chart",
    "tokens_by_model_chart",
    "turn_volume_chart",
]

# ``fast_path_label`` of the latency rows that cover every path at once.
ALL_PATHS_LABEL = "all"
PATH_DISPLAY: dict[str, str] = {
    STANDARD_LABEL: "Standard NL2SQL",
    FAST_PATH_LABEL: "Fast path",
}
_PATH_ORDER: tuple[str, ...] = tuple(PATH_DISPLAY.values())

# Token parts that add up to a call's total without double counting. Cached
# tokens are a *subset* of the prompt tokens, so stacking the raw columns would
# draw the cached share twice; the stack uses uncached input and cached input
# as separate, non-overlapping segments instead.
TOKEN_PARTS: tuple[tuple[str, str], ...] = (
    ("uncached_input_tokens", "Input (uncached)"),
    ("cached_tokens", "Cached input"),
    ("output_tokens", "Output"),
    ("thoughts_tokens", "Thinking"),
)
_RAW_TOKEN_COLUMNS: tuple[str, ...] = (
    "input_tokens",
    "cached_tokens",
    "output_tokens",
    "thoughts_tokens",
)
_SUGGESTED_COLUMN_BINS: tuple[tuple[str, str], ...] = (
    ("zero_columns", "0"),
    ("one_column", "1"),
    ("two_to_four_columns", "2–4"),
    ("five_plus_columns", "5+"),
)

ChartContext = Context | Theme | None


def _theme(ctx: ChartContext = None) -> Theme:
  """Resolves the palette from a Context, a Theme or the active appearance."""
  if ctx is None:
    return active_theme()
  if isinstance(ctx, Theme):
    return ctx
  return ctx.theme


def _is_empty(df: pd.DataFrame | None) -> bool:
  return df is None or df.empty


def _num(frame: pd.DataFrame, column: str) -> pd.Series:
  """Returns ``column`` as float64 with NaN for missing/NA/garbage values.

  BigQuery frames carry nullable ``Int64`` columns, so a plain ``astype`` can
  trip over ``pd.NA``. An absent column reads as all-NaN rather than raising,
  so a chart degrades to "no data" instead of crashing the tab.
  """
  if column not in frame.columns:
    return pd.Series(float("nan"), index=frame.index, dtype="float64")
  return pd.to_numeric(frame[column], errors="coerce").astype("float64")


def _text(value: Any) -> str:
  """Escapes customer-controlled text for Plotly's hover HTML subset."""
  return html.escape("" if value is None else str(value), quote=False)


def _as_float(value: Any) -> float | None:
  """Coerces a cell to float; None, NaN and ``pd.NA`` become None."""
  try:
    number = float(value)
  except (TypeError, ValueError):
    return None
  return None if number != number else number


def _fmt_int(value: Any) -> str:
  number = _as_float(value)
  return "—" if number is None else f"{int(round(number)):,}"


def _fmt_ms(value: Any) -> str:
  number = _as_float(value)
  return "—" if number is None else f"{number:,.0f} ms"


def _fmt_pct(value: Any) -> str:
  number = _as_float(value)
  return "—" if number is None else f"{number:.1%}"


def _error_color(theme: Theme) -> str:
  """The palette's last slot (red in both modes) reads as "error"."""
  return theme.categorical[-1]


def _path_name(label: Any) -> str:
  """Display name of a ``fast_path_label`` value."""
  key = "" if label is None else str(label)
  return PATH_DISPLAY.get(key, key.replace("_", " ").title() or "Turns")


def _ordered_paths(names: Sequence[str]) -> list[str]:
  """Standard first, then the fast path, then anything unexpected."""
  present = set(names)
  known = [name for name in _PATH_ORDER if name in present]
  return known + sorted(present - set(known))


def _with_path(frame: pd.DataFrame) -> pd.DataFrame:
  """Adds a ``path`` display column derived from ``fast_path_label``."""
  out = frame.copy()
  if "fast_path_label" in out.columns:
    out["path"] = out["fast_path_label"].map(_path_name)
  else:
    out["path"] = "Turns"
  return out


def _bar_marker(theme: Theme, color: str) -> dict[str, Any]:
  return dict(
      color=color,
      cornerradius=4,
      # A surface-colored hairline reads as the 2px gap between stacked
      # segments, not as a border around the marks.
      line=dict(color=theme.surface, width=1),
  )


def _line_figure(
    frame: pd.DataFrame,
    x: str,
    series: Sequence[tuple[str, str]],
    theme: Theme,
    domain: str,
    height: int,
    unit: str = "",
) -> go.Figure:
  """Builds time-series lines sharing one y-axis, direct-labeled at the end.

  Series whose column is absent or entirely empty are skipped, so the legend
  never advertises a line that is not drawn.

  Args:
    frame: Input frame with an ``x`` column.
    x: Column holding the time bucket.
    series: ``(column, label)`` pairs; every column is in the same unit.
    theme: Active palette.
    domain: Color-slot namespace.
    height: Figure height in pixels.
    unit: Optional unit suffix for the hover text.

  Returns:
    A configured line chart (traceless when nothing is plottable).
  """
  fig = base_figure(theme, height)
  if frame.empty or x not in frame.columns:
    return fig
  ordered = frame.sort_values(x).reset_index(drop=True)
  values = {column: _num(ordered, column) for column, _ in series}
  live = [(c, label) for c, label in series if values[c].notna().any()]
  if not live:
    return fig
  colors = color_map(domain, [label for _, label in live], theme)
  # A lone bucket draws no line at all, so mark the points when there are few.
  mode = "lines+markers" if len(ordered) <= 24 else "lines"
  for column, label in live:
    fig.add_scatter(
        x=ordered[x],
        y=values[column],
        mode=mode,
        name=label,
        line=dict(color=colors[label], width=2, shape="linear"),
        marker=dict(size=5, color=colors[label]),
        connectgaps=False,
        hovertemplate=f"%{{fullData.name}}: %{{y:,.0f}}{unit}<extra></extra>",
    )
  # Direct labels on the last real point: with <= 4 series, identity is never
  # carried by color alone even before the legend is read.
  if len(live) <= 4:
    for column, label in live:
      valid = pd.DataFrame({"x": ordered[x], "y": values[column]}).dropna()
      if valid.empty:
        continue
      last = valid.iloc[-1]
      fig.add_annotation(
          x=last["x"],
          y=last["y"],
          text=label,
          showarrow=False,
          xanchor="left",
          xshift=8,
          font=dict(size=11, color=theme.text_secondary),
          bgcolor=theme.surface,
          borderpad=2,
      )
    fig.update_layout(margin=dict(l=8, r=112, t=8, b=8))
  fig.update_layout(showlegend=True, hovermode="x unified")
  return fig


def _ranked_hbars(
    frame: pd.DataFrame,
    key: str,
    value: str,
    theme: Theme,
    height: int,
    top: int,
    hover: Sequence[str] | None = None,
) -> go.Figure:
  """Builds one-series horizontal bars, largest on top.

  Args:
    frame: Input frame with ``key`` and ``value`` columns.
    key: Column holding the category label.
    value: Column holding the bar length.
    theme: Active palette.
    height: Figure height in pixels.
    top: Maximum number of bars.
    hover: Optional pre-escaped hover text, positionally aligned with
      ``frame``.

  Returns:
    A horizontal bar chart (traceless when there is nothing to rank).
  """
  fig = base_figure(theme, height)
  if frame.empty or key not in frame.columns:
    return fig
  work = pd.DataFrame(
      {
          "label": [_text(item) for item in frame[key]],
          "value": _num(frame, value).to_numpy(),
          "hover": list(hover) if hover is not None else [""] * len(frame),
      }
  )
  work = work.dropna(subset=["value"])
  if work.empty:
    return fig
  part = work.nlargest(top, "value").sort_values("value")
  hover_text = [
      text or f"{label}: {_fmt_int(amount)}"
      for label, amount, text in zip(
          part["label"], part["value"], part["hover"]
      )
  ]
  fig.add_bar(
      x=part["value"],
      y=part["label"],
      orientation="h",
      marker=_bar_marker(theme, theme.categorical[0]),
      hovertext=hover_text,
      hovertemplate="%{hovertext}<extra></extra>",
  )
  fig.update_layout(bargap=0.35, showlegend=False)
  fig.update_xaxes(tickformat=",")
  fig.update_yaxes(automargin=True)
  return fig


# ------------------------------------------------------------------ #
# Overview & latency (P2, P3)                                          #
# ------------------------------------------------------------------ #


def turn_volume_chart(
    df: pd.DataFrame | None, ctx: ChartContext = None, *, height: int = 320
) -> go.Figure:
  """Stacked turn volume by path per bucket, with an error-turn line.

  Both the bars and the line count *turns* (distinct invocations), so they
  share one y-axis: the line can never exceed the stack it sits on.

  Args:
    df: ``build_bqca_turn_volume_sql`` result.
    ctx: Context, theme or None for the active theme.
    height: Figure height in pixels.

  Returns:
    A stacked bar chart; traceless for an empty frame.
  """
  theme = _theme(ctx)
  fig = base_figure(theme, height)
  if _is_empty(df) or "bucket" not in df.columns:
    return fig
  frame = _with_path(df)
  frame["turns"] = _num(frame, "turns")
  frame["error_turns"] = _num(frame, "error_turns")
  names = _ordered_paths(list(frame["path"].unique()))
  colors = color_map("bqca_path", names, theme)
  for name in names:
    part = frame[frame["path"] == name].sort_values("bucket")
    fig.add_bar(
        x=part["bucket"],
        y=part["turns"],
        name=name,
        marker=_bar_marker(theme, colors[name]),
        hovertemplate="%{x}<br>%{fullData.name}: %{y:,}<extra></extra>",
    )
  errors = (
      frame.groupby("bucket", as_index=False)["error_turns"]
      .sum()
      .sort_values("bucket")
  )
  if errors["error_turns"].sum() > 0:
    fig.add_scatter(
        x=errors["bucket"],
        y=errors["error_turns"],
        mode="lines+markers",
        name="Error turns",
        line=dict(color=_error_color(theme), width=2),
        marker=dict(size=5, color=_error_color(theme)),
        hovertemplate="%{x}<br>%{fullData.name}: %{y:,}<extra></extra>",
    )
  fig.update_layout(
      barmode="stack",
      bargap=0.25,
      showlegend=True,
      hovermode="x unified",
  )
  return fig


def _overall(df: pd.DataFrame | None) -> pd.DataFrame:
  """The latency rows covering every path (``fast_path_label = 'all'``)."""
  if _is_empty(df):
    return pd.DataFrame()
  if "fast_path_label" not in df.columns:
    return df
  return df[df["fast_path_label"] == ALL_PATHS_LABEL]


def latency_percentiles_chart(
    df: pd.DataFrame | None, ctx: ChartContext = None, *, height: int = 320
) -> go.Figure:
  """Turn latency P50/P95/P99 per bucket across every path.

  Args:
    df: ``build_bqca_latency_sql`` result.
    ctx: Context, theme or None for the active theme.
    height: Figure height in pixels.

  Returns:
    A line chart in milliseconds; traceless when no turn completed.
  """
  theme = _theme(ctx)
  return _line_figure(
      _overall(df),
      "bucket",
      (
          ("turn_p50_ms", "P50"),
          ("turn_p95_ms", "P95"),
          ("turn_p99_ms", "P99"),
      ),
      theme,
      "bqca_percentile",
      height,
      unit=" ms",
  )


def llm_latency_chart(
    df: pd.DataFrame | None, ctx: ChartContext = None, *, height: int = 320
) -> go.Figure:
  """LLM call latency and time-to-first-token per bucket.

  Args:
    df: ``build_bqca_latency_sql`` result.
    ctx: Context, theme or None for the active theme.
    height: Figure height in pixels.

  Returns:
    A line chart in milliseconds; traceless when no LLM call completed.
  """
  theme = _theme(ctx)
  return _line_figure(
      _overall(df),
      "bucket",
      (
          ("llm_p50_ms", "LLM P50"),
          ("llm_p95_ms", "LLM P95"),
          ("tfft_p50_ms", "First token P50"),
          ("tfft_p95_ms", "First token P95"),
      ),
      theme,
      "bqca_llm_latency",
      height,
      unit=" ms",
  )


def path_latency_chart(
    df: pd.DataFrame | None, ctx: ChartContext = None, *, height: int = 320
) -> go.Figure:
  """Turn latency of the fast path against standard NL2SQL.

  Color follows the path (shared with ``turn_volume_chart``); the dash
  carries the percentile, so identity is never color alone.

  Args:
    df: ``build_bqca_latency_sql`` result.
    ctx: Context, theme or None for the active theme.
    height: Figure height in pixels.

  Returns:
    A line chart in milliseconds; traceless when neither path has latency.
  """
  theme = _theme(ctx)
  fig = base_figure(theme, height)
  if _is_empty(df) or "fast_path_label" not in df.columns:
    return fig
  frame = _with_path(df[df["fast_path_label"] != ALL_PATHS_LABEL])
  if frame.empty or "bucket" not in frame.columns:
    return fig
  names = _ordered_paths(list(frame["path"].unique()))
  colors = color_map("bqca_path", names, theme)
  drawn = False
  for name in names:
    part = frame[frame["path"] == name].sort_values("bucket")
    for column, label, dash in (
        ("turn_p50_ms", "P50", "solid"),
        ("turn_p95_ms", "P95", "dash"),
    ):
      values = _num(part, column)
      if values.notna().sum() == 0:
        continue
      drawn = True
      fig.add_scatter(
          x=part["bucket"],
          y=values,
          mode="lines+markers" if len(part) <= 24 else "lines",
          name=f"{name} {label}",
          line=dict(color=colors[name], width=2, dash=dash),
          marker=dict(size=5, color=colors[name]),
          connectgaps=False,
          hovertemplate="%{fullData.name}: %{y:,.0f} ms<extra></extra>",
      )
  if drawn:
    fig.update_layout(showlegend=True, hovermode="x unified")
  return fig


# ------------------------------------------------------------------ #
# Data agents & personas (P5, P6)                                      #
# ------------------------------------------------------------------ #


def data_agent_leaderboard_chart(
    df: pd.DataFrame | None,
    ctx: ChartContext = None,
    *,
    height: int = 320,
    top: int = 12,
) -> go.Figure:
  """Horizontal bars of turns per data agent, hover carries quality stats.

  Args:
    df: ``build_bqca_data_agent_breakdown_sql`` result.
    ctx: Context, theme or None for the active theme.
    height: Figure height in pixels.
    top: Maximum number of data agents drawn.

  Returns:
    A ranked horizontal bar chart; traceless for an empty frame.
  """
  theme = _theme(ctx)
  if _is_empty(df) or "data_agent_id" not in df.columns:
    return base_figure(theme, height)
  hover = [
      f"{_text(agent)}<br>Turns: {_fmt_int(turns)}"
      f"<br>Error rate: {_fmt_pct(error_rate)}"
      f"<br>Fast-path rate: {_fmt_pct(fast_rate)}"
      f"<br>P95 latency: {_fmt_ms(p95)}"
      for agent, turns, error_rate, fast_rate, p95 in zip(
          df["data_agent_id"],
          _num(df, "turns"),
          _num(df, "error_rate"),
          _num(df, "fast_path_rate"),
          _num(df, "p95_latency_ms"),
      )
  ]
  return _ranked_hbars(df, "data_agent_id", "turns", theme, height, top, hover)


def persona_breakdown_chart(
    df: pd.DataFrame | None,
    ctx: ChartContext = None,
    *,
    height: int = 320,
    top: int = 12,
) -> go.Figure:
  """Horizontal bars of turns per persona, hover carries quality stats.

  Args:
    df: ``build_bqca_persona_breakdown_sql`` result.
    ctx: Context, theme or None for the active theme.
    height: Figure height in pixels.
    top: Maximum number of personas drawn.

  Returns:
    A ranked horizontal bar chart; traceless for an empty frame.
  """
  theme = _theme(ctx)
  if _is_empty(df) or "persona" not in df.columns:
    return base_figure(theme, height)
  hover = [
      f"{_text(persona)}<br>Turns: {_fmt_int(turns)}"
      f"<br>Error rate: {_fmt_pct(error_rate)}"
      f"<br>Avg latency: {_fmt_ms(latency)}"
      for persona, turns, error_rate, latency in zip(
          df["persona"],
          _num(df, "turns"),
          _num(df, "error_rate"),
          _num(df, "avg_latency_ms"),
      )
  ]
  return _ranked_hbars(df, "persona", "turns", theme, height, top, hover)


# ------------------------------------------------------------------ #
# Tokens & embedding suggestions (P4, P7)                              #
# ------------------------------------------------------------------ #


def _token_frame(df: pd.DataFrame, by: str) -> pd.DataFrame:
  """Sums the token columns by ``by`` and derives the non-overlapping parts."""
  frame = pd.DataFrame(
      {column: _num(df, column) for column in _RAW_TOKEN_COLUMNS}
  )
  frame[by] = df[by].to_numpy()
  grouped = frame.groupby(by, as_index=False, sort=True)[
      list(_RAW_TOKEN_COLUMNS)
  ].sum()
  grouped["uncached_input_tokens"] = (
      grouped["input_tokens"] - grouped["cached_tokens"]
  ).clip(lower=0)
  grouped["total_parts"] = grouped[[column for column, _ in TOKEN_PARTS]].sum(
      axis=1
  )
  return grouped


def token_breakdown_chart(
    df: pd.DataFrame | None, ctx: ChartContext = None, *, height: int = 320
) -> go.Figure:
  """Stacked token usage per bucket: uncached input, cached, output, thinking.

  The segments do not overlap (cached tokens are carved out of the input
  count), so the stack height is the real token volume. The table twin keeps
  the raw ``input_tokens``/``cached_tokens`` columns.

  Args:
    df: ``build_bqca_token_usage_sql`` result.
    ctx: Context, theme or None for the active theme.
    height: Figure height in pixels.

  Returns:
    A stacked bar chart; traceless when no LLM call reported usage.
  """
  theme = _theme(ctx)
  fig = base_figure(theme, height)
  if _is_empty(df) or "bucket" not in df.columns:
    return fig
  grouped = _token_frame(df, "bucket")
  parts = [(c, label) for c, label in TOKEN_PARTS if grouped[c].sum() > 0]
  if not parts:
    return fig
  colors = color_map("bqca_tokens", [label for _, label in parts], theme)
  for column, label in parts:
    fig.add_bar(
        x=grouped["bucket"],
        y=grouped[column],
        name=label,
        marker=_bar_marker(theme, colors[label]),
        hovertemplate="%{x}<br>%{fullData.name}: %{y:,}<extra></extra>",
    )
  fig.update_layout(
      barmode="stack",
      bargap=0.25,
      showlegend=True,
      hovermode="x unified",
  )
  return fig


def tokens_by_model_chart(
    df: pd.DataFrame | None,
    ctx: ChartContext = None,
    *,
    height: int = 320,
    top: int = 8,
) -> go.Figure:
  """Stacked horizontal token usage per model, largest total on top.

  Models past ``top`` fold into one gray "Other" bar, so the palette never
  needs a ninth hue.

  Args:
    df: ``build_bqca_token_usage_sql`` result.
    ctx: Context, theme or None for the active theme.
    height: Figure height in pixels.
    top: Maximum number of distinct bars before folding into "Other".

  Returns:
    A stacked horizontal bar chart; traceless when no usage was reported.
  """
  theme = _theme(ctx)
  fig = base_figure(theme, height)
  if _is_empty(df) or "model_name" not in df.columns:
    return fig
  frame = df.copy()
  frame["model_name"] = [
      _text(name) if name is not None and not pd.isna(name) else "(unknown)"
      for name in frame["model_name"]
  ]
  for column in _RAW_TOKEN_COLUMNS:
    frame[column] = _num(frame, column)
  if frame["model_name"].nunique() > top:
    # Input already contains the cached tokens, so rank by input + output +
    # thinking to avoid counting the cached share twice.
    volume = frame[["input_tokens", "output_tokens", "thoughts_tokens"]].sum(
        axis=1
    )
    keep = volume.groupby(frame["model_name"]).sum().nlargest(top - 1).index
    frame["model_name"] = frame["model_name"].where(
        frame["model_name"].isin(keep), OTHER_LABEL
    )
  grouped = _token_frame(frame, "model_name")
  grouped = grouped.sort_values("total_parts")
  parts = [(c, label) for c, label in TOKEN_PARTS if grouped[c].sum() > 0]
  if not parts:
    return fig
  colors = color_map("bqca_tokens", [label for _, label in parts], theme)
  for column, label in parts:
    fig.add_bar(
        x=grouped[column],
        y=grouped["model_name"],
        orientation="h",
        name=label,
        marker=_bar_marker(theme, colors[label]),
        hovertemplate="%{y}<br>%{fullData.name}: %{x:,}<extra></extra>",
    )
  fig.update_layout(barmode="stack", bargap=0.35, showlegend=True)
  fig.update_xaxes(tickformat=",")
  fig.update_yaxes(automargin=True)
  return fig


def embedding_suggestions_chart(
    df: pd.DataFrame | None,
    ctx: ChartContext = None,
    *,
    height: int = 320,
    top: int = 8,
) -> go.Figure:
  """Embedding suggestions per bucket, stacked by the reason they were made.

  The logging plugin writes one event per suggestion and tags it with a
  reason, so the stack height is the suggestion count and each segment is one
  reason. The hover adds how many columns those suggestions proposed. Reasons
  past ``top`` fold into one gray "Other" segment.

  Args:
    df: ``build_bqca_embedding_suggestions_sql`` result.
    ctx: Context, theme or None for the active theme.
    height: Figure height in pixels.
    top: Maximum number of distinct reasons before folding into "Other".

  Returns:
    A stacked bar chart; traceless when no suggestion event was logged.
  """
  theme = _theme(ctx)
  fig = base_figure(theme, height)
  if _is_empty(df) or not {"bucket", "embedding_reason"} <= set(df.columns):
    return fig
  frame = pd.DataFrame(
      {
          "bucket": df["bucket"].to_numpy(),
          "reason": [
              _text(reason)
              if reason is not None and not pd.isna(reason)
              else "(unspecified)"
              for reason in df["embedding_reason"]
          ],
          "suggestions": _num(df, "suggestion_events").fillna(0).to_numpy(),
          "columns": _num(df, "suggested_columns").fillna(0).to_numpy(),
      }
  )
  if frame["suggestions"].sum() <= 0:
    return fig
  if frame["reason"].nunique() > top:
    keep = frame.groupby("reason")["suggestions"].sum().nlargest(top - 1).index
    frame["reason"] = frame["reason"].where(
        frame["reason"].isin(keep), OTHER_LABEL
    )
  frame = frame.groupby(["bucket", "reason"], as_index=False)[
      ["suggestions", "columns"]
  ].sum()
  names = sorted(frame["reason"].unique(), key=str)
  colors = color_map("bqca_embedding", names, theme)
  for name in names:
    part = frame[frame["reason"] == name].sort_values("bucket")
    fig.add_bar(
        x=part["bucket"],
        y=part["suggestions"],
        name=name,
        marker=_bar_marker(theme, colors[name]),
        hovertext=[
            f"Suggested columns: {_fmt_int(c)}" for c in part["columns"]
        ],
        hovertemplate=(
            "%{x}<br>%{fullData.name}: %{y:,}<br>%{hovertext}<extra></extra>"
        ),
    )
  fig.update_layout(
      barmode="stack",
      bargap=0.25,
      showlegend=True,
      hovermode="x unified",
  )
  return fig


def suggested_columns_chart(
    df: pd.DataFrame | None, ctx: ChartContext = None, *, height: int = 320
) -> go.Figure:
  """How many columns each suggestion proposed (1, 2–4, 5+).

  A "0" bar appears only when some suggestion carried no columns, which the
  logging plugin does not normally write.

  Args:
    df: ``build_bqca_embedding_suggestions_sql`` result.
    ctx: Context, theme or None for the active theme.
    height: Figure height in pixels.

  Returns:
    A bar chart of suggestion counts per column-count bin; traceless when
    empty.
  """
  theme = _theme(ctx)
  fig = base_figure(theme, height)
  if _is_empty(df):
    return fig
  totals = {
      column: float(_num(df, column).fillna(0).sum())
      for column, _ in _SUGGESTED_COLUMN_BINS
  }
  if sum(totals.values()) <= 0:
    return fig
  bins = [
      (column, label)
      for column, label in _SUGGESTED_COLUMN_BINS
      if column != "zero_columns" or totals[column] > 0
  ]
  fig.add_bar(
      x=[label for _, label in bins],
      y=[totals[column] for column, _ in bins],
      marker=_bar_marker(theme, theme.categorical[0]),
      hovertemplate="Suggestions with %{x} columns: %{y:,}<extra></extra>",
  )
  fig.update_layout(bargap=0.35, showlegend=False)
  fig.update_xaxes(type="category")
  fig.update_yaxes(tickformat=",")
  return fig


# ------------------------------------------------------------------ #
# Error attribution (P10)                                              #
# ------------------------------------------------------------------ #


def error_attribution_chart(
    df: pd.DataFrame | None,
    ctx: ChartContext = None,
    *,
    height: int = 320,
    top: int = 8,
) -> go.Figure:
  """Stacked horizontal error counts per data agent, split by event type.

  Args:
    df: ``build_bqca_error_attribution_sql`` result.
    ctx: Context, theme or None for the active theme.
    height: Figure height in pixels.
    top: Maximum number of distinct data agents before folding into "Other".

  Returns:
    A stacked horizontal bar chart; traceless when no error was logged.
  """
  theme = _theme(ctx)
  fig = base_figure(theme, height)
  if _is_empty(df) or not {"data_agent_id", "event_type"} <= set(df.columns):
    return fig
  frame = pd.DataFrame(
      {
          "data_agent_id": [
              _text(agent)
              if agent is not None and not pd.isna(agent)
              else "unattributed"
              for agent in df["data_agent_id"]
          ],
          "event_type": [
              _text(kind)
              if kind is not None and not pd.isna(kind)
              else "(unknown)"
              for kind in df["event_type"]
          ],
          "errors": _num(df, "errors").fillna(0).to_numpy(),
      }
  )
  frame = frame.groupby(["data_agent_id", "event_type"], as_index=False)[
      "errors"
  ].sum()
  frame = fold_others(
      frame, "data_agent_id", "errors", group_cols=["event_type"], limit=top
  )
  if frame["errors"].sum() <= 0:
    return fig
  order = frame.groupby("data_agent_id")["errors"].sum().sort_values().index
  names = sorted(frame["event_type"].unique())
  colors = color_map("bqca_error_type", names, theme)
  for name in names:
    part = frame[frame["event_type"] == name]
    fig.add_bar(
        x=part["errors"],
        y=part["data_agent_id"],
        orientation="h",
        name=name,
        marker=_bar_marker(theme, colors[name]),
        hovertemplate="%{y}<br>%{fullData.name}: %{x:,}<extra></extra>",
    )
  fig.update_layout(barmode="stack", bargap=0.35, showlegend=len(names) > 1)
  fig.update_xaxes(tickformat=",")
  fig.update_yaxes(
      automargin=True, categoryorder="array", categoryarray=list(order)
  )
  return fig
