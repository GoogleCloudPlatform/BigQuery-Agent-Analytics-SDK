"""Chart builders, theme helpers, and panel rendering for Streamlit."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
import sys

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

_DASHBOARD_DIR = str(Path(__file__).resolve().parent)
if _DASHBOARD_DIR not in sys.path:
  sys.path.insert(0, _DASHBOARD_DIR)

from models import Context
from models import DARK_THEME
from models import LIGHT_THEME
from models import OTHER_LABEL
from models import Theme


def active_theme() -> Theme:
  """Returns the palette matching Streamlit's current appearance.

  Returns:
    The active Theme object (DARK_THEME if Streamlit is in dark mode,
    else LIGHT_THEME).
  """
  mode = None
  try:
    mode = st.context.theme.type
  except (
      AttributeError,
      TypeError,
  ):  # pragma: no cover - older Streamlit builds.
    mode = None
  if not mode:
    mode = st.get_option("theme.base") or "light"
  return DARK_THEME if str(mode).lower() == "dark" else LIGHT_THEME


def color_map(
    domain: str, names: Sequence[str], theme: Theme
) -> dict[str, str]:
  """Maps category names to categorical slots, stably across reruns.

  Color follows the entity, not its rank: a name keeps the slot it was
  first given for as long as the session lives, so narrowing a filter
  never repaints the series that survive. Slots are only reassigned when
  two names visible in the *same* chart would otherwise collide.

  Args:
    domain: State namespace for slot allocation (e.g. 'agent', 'event_type').
    names: Category names to assign colors to.
    theme: Active theme containing categorical color palette.

  Returns:
    A mapping from category name to color hex code.
  """
  registry: dict[str, int] = st.session_state.setdefault(
      f"_slots::{domain}", {}
  )
  live = [n for n in dict.fromkeys(names) if n != OTHER_LABEL]
  taken: dict[int, str] = {}
  needs_slot: list[str] = []
  for name in live:
    slot = registry.get(name)
    if slot is None or slot in taken:
      needs_slot.append(name)
    else:
      taken[slot] = name
  for name in needs_slot:
    free = next(
        (i for i in range(len(theme.categorical)) if i not in taken),
        None,
    )
    if free is None:  # More than 8 live names: caller failed to fold.
      free = len(registry) % len(theme.categorical)
    registry[name] = free
    taken[free] = name
  mapping = {n: theme.categorical[registry[n]] for n in live}
  mapping[OTHER_LABEL] = theme.muted
  return mapping


def base_figure(theme: Theme, height: int = 320) -> go.Figure:
  """Creates a Plotly figure with recessive chrome and hairline solid gridlines.

  Args:
    theme: Active color theme.
    height: Desired figure height in pixels.

  Returns:
    A configured go.Figure instance.
  """
  fig = go.Figure()
  fig.update_layout(
      height=height,
      margin=dict(l=8, r=8, t=8, b=8),
      paper_bgcolor=theme.surface,
      plot_bgcolor=theme.surface,
      font=dict(
          family='system-ui, -apple-system, "Segoe UI", sans-serif',
          size=12,
          color=theme.text_secondary,
      ),
      hoverlabel=dict(
          bgcolor=theme.surface,
          bordercolor=theme.axis,
          font=dict(color=theme.text_primary, size=12),
      ),
      legend=dict(
          orientation="h",
          yanchor="bottom",
          y=1.02,
          x=0,
          bgcolor="rgba(0,0,0,0)",
          font=dict(color=theme.text_secondary),
      ),
      showlegend=False,
  )
  axis = dict(
      showgrid=True,
      gridcolor=theme.grid,
      gridwidth=1,
      griddash="solid",
      zeroline=False,
      linecolor=theme.axis,
      tickfont=dict(color=theme.muted, size=11),
      title=None,
  )
  fig.update_xaxes(**axis, showline=True)
  fig.update_yaxes(**axis, showline=False)
  return fig


def fold_others(
    df: pd.DataFrame,
    key: str,
    value: str,
    group_cols: Sequence[str] = (),
    limit: int = 8,
) -> pd.DataFrame:
  """Folds all but the top ``limit`` categories into a single "Other".

  A ninth categorical hue is never generated: past the palette's eight
  slots, the tail becomes one gray series.

  Args:
    df: Input dataframe.
    key: Column name holding the categorical dimension to fold.
    value: Column name holding the numeric measure to sum by.
    group_cols: Optional columns to keep in the group-by aggregation.
    limit: Maximum number of distinct categories before folding.

  Returns:
    A dataframe with the smallest categories aggregated under OTHER_LABEL.
  """
  if df.empty or df[key].nunique() <= limit:
    return df
  keep = df.groupby(key)[value].sum().nlargest(limit - 1).index
  out = df.copy()
  out[key] = out[key].where(out[key].isin(keep), OTHER_LABEL)
  return out.groupby([*group_cols, key], as_index=False)[value].sum()


def stacked_bars(
    df: pd.DataFrame,
    x: str,
    key: str,
    value: str,
    ctx: Context,
    domain: str,
    height: int = 320,
) -> go.Figure:
  """Creates stacked bars over time, one series per category.

  Args:
    df: Input dataframe.
    x: Column name for the x-axis (e.g. bucket timestamp).
    key: Column name for categorical series segmentation.
    value: Column name for numeric bar heights.
    ctx: Active dashboard context.
    domain: Color domain namespace.
    height: Figure height in pixels.

  Returns:
    A configured Plotly stacked bar chart.
  """
  theme = ctx.theme
  folded = fold_others(df, key, value, group_cols=[x])
  names = sorted(folded[key].unique(), key=str)
  colors = color_map(domain, [n for n in names if n != OTHER_LABEL], theme)
  fig = base_figure(theme, height)
  for name in names:
    part = folded[folded[key] == name].sort_values(x)
    fig.add_bar(
        x=part[x],
        y=part[value],
        name=str(name),
        marker=dict(
            color=colors.get(name, theme.muted),
            cornerradius=4,
            # A surface-colored hairline reads as the 2px gap between
            # stacked segments, not as a border around the marks.
            line=dict(color=theme.surface, width=1),
        ),
        hovertemplate=f"%{{x}}<br>{name}: %{{y:,}}<extra></extra>",
    )
  fig.update_layout(
      barmode="stack",
      bargap=0.25,
      showlegend=len(names) > 1,
      hovermode="x unified",
  )
  return fig


def ranked_bars(
    df: pd.DataFrame,
    key: str,
    value: str,
    ctx: Context,
    height: int = 320,
    top: int = 12,
) -> go.Figure:
  """Creates horizontal bars for one measure across nominal categories.

  One series, one color: length already encodes magnitude, so a
  darker-where-bigger ramp would spend the only free channel restating it.

  Args:
    df: Input dataframe.
    key: Column name for categorical labels.
    value: Column name for numeric values.
    ctx: Active dashboard context.
    height: Figure height in pixels.
    top: Number of top categories to display.

  Returns:
    A configured Plotly horizontal bar chart.
  """
  theme = ctx.theme
  part = df.nlargest(top, value).sort_values(value)
  fig = base_figure(theme, height)
  fig.add_bar(
      x=part[value],
      y=part[key].astype(str),
      orientation="h",
      marker=dict(
          color=theme.categorical[0],
          cornerradius=4,
          line=dict(color=theme.surface, width=1),
      ),
      hovertemplate="%{y}: %{x:,}<extra></extra>",
  )
  fig.update_layout(bargap=0.35, showlegend=False)
  fig.update_xaxes(tickformat=",")
  return fig


def grouped_bars(
    df: pd.DataFrame,
    key: str,
    series: Sequence[tuple[str, str]],
    ctx: Context,
    domain: str,
    height: int = 320,
    top: int = 12,
) -> go.Figure:
  """Creates horizontal grouped bars for measures that share one unit.

  Args:
    df: Input dataframe.
    key: Column name for categorical labels.
    series: Sequence of (column_name, display_label) tuples.
    ctx: Active dashboard context.
    domain: Color domain namespace.
    height: Figure height in pixels.
    top: Number of top categories to display.

  Returns:
    A configured Plotly horizontal grouped bar chart.
  """
  theme = ctx.theme
  first_value = series[0][0]
  part = df.nlargest(top, first_value).sort_values(first_value)
  colors = color_map(domain, [label for _, label in series], theme)
  fig = base_figure(theme, height)
  for column, label in series:
    fig.add_bar(
        x=part[column],
        y=part[key].astype(str),
        orientation="h",
        name=label,
        marker=dict(
            color=colors[label],
            cornerradius=4,
            line=dict(color=theme.surface, width=1),
        ),
        hovertemplate=f"%{{y}} — {label}: %{{x:,.0f}}<extra></extra>",
    )
  fig.update_layout(
      barmode="group", bargap=0.3, bargroupgap=0.08, showlegend=True
  )
  fig.update_xaxes(tickformat=",")
  return fig


def lines(
    df: pd.DataFrame,
    x: str,
    series: Sequence[tuple[str, str]],
    ctx: Context,
    domain: str,
    height: int = 320,
    unit: str = "",
) -> go.Figure:
  """Creates time-series lines sharing one y-axis, direct-labeled at the end.

  Never two y-scales: every series passed here is in the same unit.

  Args:
    df: Input dataframe.
    x: Column name for the x-axis time/bucket values.
    series: Sequence of (column_name, display_label) tuples.
    ctx: Active dashboard context.
    domain: Color domain namespace.
    height: Figure height in pixels.
    unit: Optional unit suffix for hover text.

  Returns:
    A configured Plotly line chart.
  """
  theme = ctx.theme
  ordered = df.sort_values(x)
  colors = color_map(domain, [label for _, label in series], theme)
  fig = base_figure(theme, height)
  for column, label in series:
    fig.add_scatter(
        x=ordered[x],
        y=ordered[column],
        mode="lines",
        name=label,
        line=dict(color=colors[label], width=2, shape="linear"),
        connectgaps=False,
        hovertemplate=f"{label}: %{{y:,.0f}}{unit}<extra></extra>",
    )
  # Direct labels on the last real point: with <= 4 series, identity is
  # never carried by color alone even before the legend is read.
  if len(series) <= 4:
    for column, label in series:
      valid = ordered[[x, column]].dropna()
      if valid.empty:
        continue
      last = valid.iloc[-1]
      fig.add_annotation(
          x=last[x],
          y=last[column],
          text=label,
          showarrow=False,
          xanchor="left",
          xshift=8,
          font=dict(size=11, color=theme.text_secondary),
          bgcolor=theme.surface,
          borderpad=2,
      )
    fig.update_layout(margin=dict(l=8, r=96, t=8, b=8))
  fig.update_layout(showlegend=True, hovermode="x unified")
  return fig


def panel(
    title: str,
    fig: go.Figure | None,
    table: pd.DataFrame,
    *,
    empty: str = "No matching data in this range.",
    key: str = "",
) -> None:
  """Renders one chart with its table-view twin behind an expander.

  Every chart has a table twin: it is the WCAG-clean equivalent, and it
  is the relief the light-mode palette's sub-3:1 slots require.

  Args:
    title: Panel title markdown string.
    fig: Optional Plotly figure to render above the table.
    table: Backing dataframe for the table expander.
    empty: Caption displayed if the table is empty.
    key: Streamlit widget key for the chart.
  """
  st.markdown(f"**{title}**")
  if table.empty:
    st.caption(empty)
    return
  if fig is not None:
    st.plotly_chart(fig, width="stretch", key=key or None)
  with st.expander("Table view", expanded=fig is None):
    st.dataframe(table, width="stretch", hide_index=True)