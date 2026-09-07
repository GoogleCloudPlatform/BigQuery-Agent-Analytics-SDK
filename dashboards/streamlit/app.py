from __future__ import annotations

from collections.abc import Sequence
import dataclasses
import os
from pathlib import Path
import sys
from typing import Any

from dotenv import load_dotenv

load_dotenv()

import pandas as pd
import streamlit as st

_DASHBOARD_DIR = str(Path(__file__).resolve().parent)
if _DASHBOARD_DIR not in sys.path:
  sys.path.insert(0, _DASHBOARD_DIR)

from charts import (
  active_theme,
  grouped_bars,
  lines,
  panel,
  ranked_bars,
  stacked_bars,
)

from models import (
  as_filter_values,
  BYTES_CAPS,
  CACHE_TTL_SECONDS,
  Context,
  DEFAULT_BYTES_CAP,
  DEFAULT_TABLE_ID,
  DEFAULT_VIEW_PREFIX,
  Filters,
  humanize_bytes,
  make_window,
  RECENT_SESSIONS_LIMIT,
  TableRefs,
  TIME_RANGES,
  TOOL_ERRORS_LIMIT,
  TOP_ERRORS_LIMIT,
  TRACE_DETAIL_LIMIT,
  validate_refs,
  Window,
)

from queries import (
  build_errors_over_time_sql,
  build_events_by_agent_sql,
  build_events_over_time_sql,
  build_llm_calls_total_sql,
  build_llm_latency_percentiles_sql,
  build_llm_tokens_over_time_sql,
  build_overview_totals_sql,
  build_recent_sessions_sql,
  build_tokens_by_model_sql,
  build_tool_errors_sql,
  build_tool_latency_sql,
  build_tool_usage_sql,
  build_top_errors_sql,
  build_trace_detail_sql,
  fetch,
  load_filter_options,
)

APP_TITLE = "BigQuery Agent Analytics"

_FILTER_WIDGETS = (
    ("flt_agent", "agent"),
    ("flt_user_id", "user_id"),
    ("flt_event_type", "event_type"),
    ("flt_session_id", "session_id"),
)


def _prune_selection(key: str, options: Sequence[str]) -> None:
  """Drops selected values that the current options no longer offer.

  Args:
    key: Streamlit session_state key.
    options: Current valid options sequence.
  """
  current = st.session_state.get(key)
  if not current:
    return
  available = set(options)
  st.session_state[key] = [v for v in current if v in available]


def sidebar_connection() -> tuple[TableRefs | None, int]:
  """Resolves the BigQuery source from the environment, then the form.

  ``BQ_PROJECT_ID`` / ``BQ_DATASET_ID`` / ``BQ_TABLE_ID`` / ``BQ_VIEW_PREFIX``
  seed the fields; the form is the fallback when they are unset and the override
  for dataset, table, and view prefix when they are wrong. Project ID is not
  overridable when ``BQ_PROJECT_ID`` is set. It is a form so that
  changing multiple configuration settings costs one rerun rather than five.

  Returns:
    A tuple containing validated TableRefs (or None if invalid) and the
    selected byte cap limit.
  """
  env_project = os.environ.get("BQ_PROJECT_ID", "")
  env_dataset = os.environ.get("BQ_DATASET_ID", "")
  env_table = os.environ.get("BQ_TABLE_ID", "") or DEFAULT_TABLE_ID
  env_prefix = os.environ.get("BQ_VIEW_PREFIX", DEFAULT_VIEW_PREFIX)

  st.sidebar.subheader("BigQuery source")

  with st.sidebar.form("connection"):
    project = st.text_input(
        "Project ID", value=env_project, disabled=bool(env_project)
    )
    dataset = st.text_input("Dataset ID", value=env_dataset)
    table = st.text_input("Events table", value=env_table)
    prefix = st.text_input(
        "Typed view prefix",
        value=env_prefix,
        help=(
            "The prefix ViewManager applied to the typed views"
            " (`adk_` by default)."
        ),
    )
    cap_label = st.selectbox(
        "Per-query scan cap",
        options=list(BYTES_CAPS),
        index=list(BYTES_CAPS).index(DEFAULT_BYTES_CAP),
        help=(
            "Sets `maximum_bytes_billed` on every job. BigQuery refuses a"
            " query that would exceed it rather than billing for it."
        ),
    )
    st.form_submit_button("Connect", width="stretch")

  if env_project:
    project = env_project
  refs, errors = validate_refs(project, dataset, table, prefix)
  for message in errors:
    st.sidebar.error(message)
  return refs, BYTES_CAPS[cap_label]


def sidebar_window() -> Window:
  """Renders the time range picker widget in the sidebar.

  A selectbox rather than a slider or free-text: one discrete choice per
  rerun, so a query fires on a committed selection instead of on every
  intermediate value.

  Returns:
    A snapped Window instance for the selected time range.
  """
  st.sidebar.subheader("Time range")
  label = st.sidebar.selectbox(
      "Range",
      options=list(TIME_RANGES),
      index=list(TIME_RANGES).index("Last 24 hours"),
      label_visibility="collapsed",
  )
  window = make_window(TIME_RANGES[label])
  st.sidebar.caption(
      f"{window.start:%Y-%m-%d %H:%M} → {window.end:%Y-%m-%d %H:%M} UTC"
      f" · {window.bucket.lower()} buckets"
  )
  return window


def sidebar_filters(
    options: dict[str, list[str]],
    prune: bool = True,
) -> tuple[Filters, float, float]:
  """Renders the filter and pricing form in the sidebar.

  A form, so a multi-select that a user is still building does not fire a
  query per keystroke: every panel re-queries once, on Apply filters.

  Args:
    options: Map of filter kinds to available option string lists.
    prune: Whether to drop selected values no longer offered in options.

  Returns:
    A tuple of (Filters instance, price_in float, price_out float).
  """
  st.sidebar.subheader("Filters")
  # Prune before the widgets render: a narrowed time range can retire an
  # option that is still selected, and a multiselect whose stored value
  # is not in its options is an error rather than a silent drop.
  if prune:
    for key, kind in _FILTER_WIDGETS:
      _prune_selection(key, options.get(kind, []))

  with st.sidebar.form("filters"):
    agents = st.multiselect(
        "Agent", options=options.get("agent", []), key="flt_agent"
    )
    user_ids = st.multiselect(
        "User", options=options.get("user_id", []), key="flt_user_id"
    )
    event_types = st.multiselect(
        "Event type",
        options=options.get("event_type", []),
        key="flt_event_type",
        help=(
            "Honored by Events over time, Events by agent, Recent sessions"
            " and Trace detail. Error panels and view-backed panels are"
            " exempt — see grafana/queries/README.md."
        ),
    )
    session_ids = st.multiselect(
        "Session",
        options=options.get("session_id", []),
        key="flt_session_id",
    )
    st.caption("An empty selection means all values.")
    st.divider()
    st.caption("Cost is derived from token counts, not recorded telemetry.")
    price_in = st.number_input(
        "USD per 1M input tokens",
        min_value=0.0,
        value=3.0,
        step=0.25,
        format="%.4f",
        key="flt_price_in",
    )
    price_out = st.number_input(
        "USD per 1M output tokens",
        min_value=0.0,
        value=15.0,
        step=0.25,
        format="%.4f",
        key="flt_price_out",
    )
    st.form_submit_button("Apply filters", width="stretch")

  filters = Filters(
      agents=as_filter_values(agents),
      user_ids=as_filter_values(user_ids),
      event_types=as_filter_values(event_types),
      session_ids=as_filter_values(session_ids),
  )
  return filters, float(price_in), float(price_out)


def _metric(column: Any, label: str, value: str, help_text: str = "") -> None:
  """Renders a single metric widget in a layout column.

  Args:
    column: Streamlit column layout element.
    label: Metric label.
    value: Metric value string.
    help_text: Optional tooltip help text.
  """
  column.metric(label, value, help=help_text or None)


def row_overview(ctx: Context) -> None:
  """Renders the Overview tab (top KPIs, events over time, errors, top errors).

  Args:
    ctx: Active dashboard context.
  """
  totals = fetch(
      build_overview_totals_sql(ctx.refs, ctx.window), ctx, "Overview stats"
  )
  cols = st.columns(4)
  if totals.df.empty:
    for col, label in zip(
        cols, ("Sessions", "Events", "Error rate", "Avg LLM latency")
    ):
      _metric(col, label, "—")
  else:
    row = totals.df.iloc[0]
    _metric(cols[0], "Sessions", f"{int(row['sessions']):,}")
    _metric(cols[1], "Events", f"{int(row['events']):,}")
    rate = row["error_rate"]
    _metric(
        cols[2],
        "Error rate",
        "—" if pd.isna(rate) else f"{float(rate) * 100:.2f}%",
        "An event counts as an error when its type ends in _ERROR, it"
        " carries an error message, or its status is ERROR.",
    )
    latency = row["avg_llm_latency_ms"]
    _metric(
        cols[3],
        "Avg LLM latency",
        "—" if pd.isna(latency) else f"{float(latency):,.0f} ms",
    )

  left, right = st.columns(2)
  with left:
    events = fetch(
        build_events_over_time_sql(ctx.refs, ctx.window),
        ctx,
        "Events over time",
    )
    fig = (
        stacked_bars(
            events.df, "bucket", "event_type", "events", ctx, "event_type"
        )
        if not events.df.empty
        else None
    )
    panel("Events over time", fig, events.df, key="events_over_time")
  with right:
    errors = fetch(
        build_errors_over_time_sql(ctx.refs, ctx.window),
        ctx,
        "Errors over time",
    )
    fig = (
        stacked_bars(
            errors.df, "bucket", "event_type", "errors", ctx, "event_type"
        )
        if not errors.df.empty
        else None
    )
    panel(
        "Errors over time",
        fig,
        errors.df,
        empty="No errors in this range.",
        key="errors_over_time",
    )

  left, right = st.columns(2)
  with left:
    by_agent = fetch(
        build_events_by_agent_sql(ctx.refs, ctx.window),
        ctx,
        "Events by agent",
    )
    fig = (
        ranked_bars(by_agent.df, "agent_name", "events", ctx)
        if not by_agent.df.empty
        else None
    )
    panel("Events by agent", fig, by_agent.df, key="events_by_agent")
  with right:
    top_errors = fetch(
        build_top_errors_sql(ctx.refs, ctx.window, TOP_ERRORS_LIMIT),
        ctx,
        "Top error messages",
    )
    panel(
        "Top error messages",
        None,
        top_errors.df,
        empty="No error messages in this range.",
    )
    st.caption(
        "Only events carrying a message are listed, so these counts are a"
        " subset of Errors over time."
    )


def row_llm(ctx: Context) -> None:
  """Renders the LLM & FinOps tab (totals, token trends, latency, models).

  Args:
    ctx: Active dashboard context.
  """
  summary = fetch(
      build_llm_calls_total_sql(ctx.refs, ctx.window),
      ctx,
      "LLM totals",
  )
  cols = st.columns(4)
  if summary.df.empty:
    for col, label in zip(
        cols, ("LLM calls", "Total tokens", "Output tokens", "Estimated cost")
    ):
      _metric(col, label, "—")
  else:
    row = summary.df.iloc[0]
    prompt_tokens = float(row.get("prompt_tokens", 0) or 0)
    completion_tokens = float(row.get("completion_tokens", 0) or 0)
    estimated_cost = (prompt_tokens / 1e6 * ctx.price_in) + (
        completion_tokens / 1e6 * ctx.price_out
    )
    _metric(
        cols[0],
        "LLM calls",
        f"{int(row['llm_calls']):,}",
        "Counted per distinct span, so streaming chunks do not inflate it.",
    )
    _metric(cols[1], "Total tokens", f"{int(row['total_tokens']):,}")
    _metric(cols[2], "Output tokens", f"{int(row['completion_tokens']):,}")
    _metric(
        cols[3],
        "Estimated cost",
        f"${estimated_cost:,.2f}",
        "Derived from token counts at the sidebar rates — not telemetry.",
    )

  left, right = st.columns(2)
  with left:
    tokens = fetch(
        build_llm_tokens_over_time_sql(ctx.refs, ctx.window),
        ctx,
        "Token usage over time",
    )
    fig = None
    if not tokens.df.empty:
      melted = tokens.df.melt(
          id_vars="bucket",
          value_vars=["prompt_tokens", "completion_tokens"],
          var_name="kind",
          value_name="tokens",
      )
      melted["kind"] = melted["kind"].map(
          {"prompt_tokens": "Input", "completion_tokens": "Output"}
      )
      fig = stacked_bars(melted, "bucket", "kind", "tokens", ctx, "token_kind")
    panel("Token usage over time", fig, tokens.df, key="tokens_over_time")
  with right:
    latency = fetch(
        build_llm_latency_percentiles_sql(ctx.refs, ctx.window),
        ctx,
        "LLM latency",
    )
    fig = (
        lines(
            latency.df,
            "bucket",
            [
                ("p50_total_ms", "p50"),
                ("p95_total_ms", "p95"),
                ("p50_ttft_ms", "TTFT p50"),
            ],
            ctx,
            "llm_latency",
            unit=" ms",
        )
        if not latency.df.empty
        else None
    )
    panel("LLM latency (ms)", fig, latency.df, key="llm_latency")
    st.caption("Gaps are missing telemetry, not zero latency.")

  models = fetch(
      build_tokens_by_model_sql(ctx.refs, ctx.window), ctx, "Tokens by model"
  )
  fig = (
      grouped_bars(
          models.df,
          "model",
          [("prompt_tokens", "Input"), ("completion_tokens", "Output")],
          ctx,
          "token_kind",
      )
      if not models.df.empty
      else None
  )
  panel("Tokens by model", fig, models.df, key="tokens_by_model")


def row_tools(ctx: Context) -> None:
  """Renders the Tools & Execution tab (tool invocations, latency, errors).

  Args:
    ctx: Active dashboard context.
  """
  left, right = st.columns(2)
  with left:
    usage = fetch(
        build_tool_usage_sql(ctx.refs, ctx.window), ctx, "Tool invocations"
    )
    fig = (
        ranked_bars(usage.df, "tool_name", "invocations", ctx)
        if not usage.df.empty
        else None
    )
    panel("Tool invocations", fig, usage.df, key="tool_usage")
    st.caption("Read from tool_starts, so failed invocations still count.")
  with right:
    latency = fetch(
        build_tool_latency_sql(ctx.refs, ctx.window), ctx, "Tool latency"
    )
    fig = (
        grouped_bars(
            latency.df,
            "tool_name",
            [("p95_ms", "p95"), ("p50_ms", "p50")],
            ctx,
            "tool_latency",
        )
        if not latency.df.empty
        else None
    )
    panel("Tool latency (ms)", fig, latency.df, key="tool_latency")

  errors = fetch(
      build_tool_errors_sql(ctx.refs, ctx.window, TOOL_ERRORS_LIMIT),
      ctx,
      "Tool errors",
  )
  panel(
      "Tool errors",
      None,
      errors.df,
      empty="No tool errors in this range.",
  )


def row_sessions(ctx: Context) -> None:
  """Renders the Sessions & Traces tab (recent sessions table, trace details).

  Args:
    ctx: Active dashboard context.
  """
  sessions = fetch(
      build_recent_sessions_sql(ctx.refs, ctx.window, RECENT_SESSIONS_LIMIT),
      ctx,
      "Recent sessions",
  )
  panel(
      f"Recent sessions (most recent {RECENT_SESSIONS_LIMIT})",
      None,
      sessions.df,
      empty="No sessions in this range.",
  )
  st.caption(
      "Each row rolls up the whole session in the window, including events"
      " the Agent / User / Event type filters exclude, so these numbers"
      " will not sum to the Overview stats."
  )

  st.divider()
  st.markdown("**Trace detail**")
  ids = (
      [str(v) for v in sessions.df["session_id"].tolist()]
      if not sessions.df.empty
      else []
  )
  if not ids:
    st.caption("Pick a session once one is listed above.")
    return
  # A selectbox rather than a text input: one committed choice per rerun,
  # so the trace query runs once instead of on every keystroke.
  chosen = st.selectbox("Session", options=ids, index=0)
  # Pin the trace to one session without disturbing the shared filters.
  # `replace` carries the same `scan_log` list over, so this query is
  # still counted once in the footer.
  detail_ctx = dataclasses.replace(
      ctx, filters=ctx.filters._replace(session_ids=(chosen,))
  )
  trace = fetch(
      build_trace_detail_sql(ctx.refs, ctx.window, TRACE_DETAIL_LIMIT),
      detail_ctx,
      "Trace detail",
  )
  if trace.df.empty:
    st.caption("No events for this session in the range.")
  else:
    st.dataframe(trace.df, width="stretch", hide_index=True)
    st.caption(
        f"Newest {TRACE_DETAIL_LIMIT} events first. Sort by timestamp to"
        " read the session chronologically."
    )


def footer(ctx: Context) -> None:
  """Reports what this rerun actually scanned and cached.

  Args:
    ctx: Active dashboard context.
  """
  if not ctx.scan_log:
    return
  total = sum(size for _, size, hit in ctx.scan_log if not hit)
  cached = sum(1 for _, _, hit in ctx.scan_log if hit)
  st.divider()
  st.caption(
      f"{len(ctx.scan_log)} queries this run · {humanize_bytes(total)}"
      f" billed · {cached} served from cache · per-query cap"
      f" {humanize_bytes(ctx.max_bytes)} · results cached for"
      f" {CACHE_TTL_SECONDS // 60} min"
  )


def main() -> None:
  """Runs the main entrypoint for the Streamlit dashboard application."""
  st.set_page_config(page_title=APP_TITLE, page_icon="📊", layout="wide")
  st.title(APP_TITLE)

  theme = active_theme()
  refs, max_bytes = sidebar_connection()
  if refs is None:
    st.info(
        "Set `BQ_PROJECT_ID`, `BQ_DATASET_ID` and `BQ_TABLE_ID`, or fill in"
        " the sidebar, to connect."
    )
    st.stop()

  window = sidebar_window()

  # Options are read unfiltered, so the sidebar can be drawn before any
  # panel runs and picking one agent never hides the others.
  probe = Context(
      refs=refs,
      window=window,
      filters=Filters(),
      max_bytes=max_bytes,
      theme=theme,
      price_in=0.0,
      price_out=0.0,
  )
  options, result = load_filter_options(probe)
  if result.error is None and options:
    st.session_state["_filter_options"] = options
    prune = True
  else:
    options = st.session_state.get("_filter_options", {})
    prune = False

  filters, price_in, price_out = sidebar_filters(options, prune=prune)

  ctx = Context(
      refs=refs,
      window=window,
      filters=filters,
      max_bytes=max_bytes,
      theme=theme,
      price_in=price_in,
      price_out=price_out,
      scan_log=probe.scan_log,
  )

  overview, llm, tools, sessions = st.tabs(
      ["Overview", "LLM & FinOps", "Tools & Execution", "Sessions & Traces"]
  )
  with overview:
    row_overview(ctx)
  with llm:
    row_llm(ctx)
  with tools:
    row_tools(ctx)
  with sessions:
    row_sessions(ctx)

  footer(ctx)


__all__ = [
    "APP_TITLE",
    "footer",
    "main",
    "row_llm",
    "row_overview",
    "row_sessions",
    "row_tools",
    "sidebar_connection",
    "sidebar_filters",
    "sidebar_window",
]

if __name__ == "__main__":
  main()