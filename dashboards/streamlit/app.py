from __future__ import annotations

from collections.abc import Sequence
import dataclasses
import datetime as dt
import os
from pathlib import Path
import sys
from typing import Any, TYPE_CHECKING

from dotenv import load_dotenv

_DASHBOARD_DIR = Path(__file__).resolve().parent
if not os.environ.get("BQAA_DASHBOARD_SKIP_DOTENV"):
  load_dotenv(_DASHBOARD_DIR / ".env")
  load_dotenv()

_LAZY_TABS = os.environ.get("STREAMLIT_LAZY_TABS", "true").lower() == "true"

_sa_creds = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
if _sa_creds and not os.path.isabs(_sa_creds):
  _candidate = _DASHBOARD_DIR / _sa_creds
  if _candidate.exists():
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(_candidate)
  elif (_DASHBOARD_DIR.parent.parent / _sa_creds).exists():
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(
        _DASHBOARD_DIR.parent.parent / _sa_creds
    )

import pandas as pd
import streamlit as st

_DASHBOARD_DIR_STR = str(_DASHBOARD_DIR)
if _DASHBOARD_DIR_STR not in sys.path:
  sys.path.insert(0, _DASHBOARD_DIR_STR)

from charts import active_theme
from charts import grouped_bars
from charts import lines
from charts import panel
from charts import ranked_bars
from charts import stacked_bars
from models import ALL_SENTINEL
from models import as_filter_values
from models import BYTES_CAPS
from models import CACHE_TTL_SECONDS
from models import Context
from models import DEFAULT_BYTES_CAP
from models import DEFAULT_TABLE_ID
from models import DEFAULT_VIEW_PREFIX
from models import Filters
from models import humanize_bytes
from models import make_window
from models import RECENT_SESSIONS_LIMIT
from models import ScanEntry
from models import TableRefs
from models import TIME_RANGES
from models import TOOL_ERRORS_LIMIT
from models import TOP_ERRORS_LIMIT
from models import TRACE_DETAIL_LIMIT
from models import validate_refs
from models import Window
from queries import build_errors_over_time_sql
from queries import build_events_by_agent_sql
from queries import build_events_over_time_sql
from queries import build_llm_calls_total_sql
from queries import build_llm_latency_percentiles_sql
from queries import build_llm_tokens_over_time_sql
from queries import build_overview_totals_sql
from queries import build_recent_sessions_sql
from queries import build_tokens_by_model_sql
from queries import build_tool_errors_sql
from queries import build_tool_latency_sql
from queries import build_tool_usage_sql
from queries import build_top_errors_sql
from queries import build_trace_detail_sql
from queries import fetch
from queries import load_filter_options

if TYPE_CHECKING:
  from bqca_models import BqcaFilterState

APP_TITLE = "BigQuery Agent Analytics"

_FILTER_WIDGETS = (
    ("flt_agent", "agent"),
    ("flt_user_id", "user_id"),
    ("flt_event_type", "event_type"),
    ("flt_session_id", "session_id"),
)

# The two dashboard surfaces. ADK Agents is the default; BQCA Prompt & Response
# Logging is opt-in through the sidebar, ``?profile=bqca`` or
# ``BQAA_PROFILE=bqca``.
ADK_SURFACE = "ADK Agents"
BQCA_SURFACE = "BQCA Prompt & Response Logging"
SURFACES = (ADK_SURFACE, BQCA_SURFACE)
BQCA_PROFILE = "bqca"
BQCA_TABS = (
    "Overview & Latency",
    "Data Agents & Personas",
    "Prompt, Response & SQL Explorer",
    "Tokens & Embedding Suggestions",
    "Error Attribution",
)

# BQCA filter field -> the key of the sidebar widget that edits it. The
# widget keys hold the in-progress edit; ``_bqca_applied`` holds what was last
# applied, mirroring how the ADK filters survive a widget re-creation.
_BQCA_WIDGET_KEYS = {
    "data_agent_ids": "bqca_flt_data_agent",
    "personas": "bqca_flt_persona",
    "event_types": "bqca_flt_event_type",
    "fast_path_mode": "bqca_flt_fast_path",
    "session_search": "bqca_flt_session_search",
    "prompt_search": "bqca_flt_prompt_search",
    "errors_only": "bqca_flt_errors_only",
}


def _seed_options(
    key: str, options: Sequence[str], applied_values: Sequence[str]
) -> list[str]:
  """Ensures applied and in-progress selections stay valid widget options.

  Retention is anchored on ``applied_values`` — the last-*applied* Filters,
  held in ``st.session_state["applied_filters"]`` — not solely on
  ``st.session_state[key]``, the widget's own Streamlit-managed value. A
  selection surviving only in ``st.session_state[key]`` is lost the moment
  Streamlit treats the widget as newly created (a ``key=`` rename, a fresh
  session, ...): a brand-new widget has no prior value to merge. Anchoring
  on ``applied_filters`` — a plain session_state entry the widget machinery
  never rewrites — means the selection survives that identity change. The
  widget's own key is still merged in too, so an in-progress, not-yet-applied
  edit is not clobbered while the form is open.

  Args:
    key: Streamlit session_state key for the widget's own pending value.
    options: Current valid options sequence from BigQuery.
    applied_values: The filter's last-applied selection, or (ALL_SENTINEL,)
      if unset.

  Returns:
    List of options containing fetched options plus any applied or
    in-progress selections.
  """
  seen = set(options)
  result = list(options)

  def _extend(values: Sequence[str]) -> None:
    for item in values:
      if item and item != ALL_SENTINEL and item not in seen:
        seen.add(item)
        result.append(item)

  _extend(applied_values)
  current = st.session_state.get(key, [])
  if not isinstance(current, (list, tuple)):
    current = [current] if current else []
  _extend(current)
  return result


def _default_for(applied_values: Sequence[str]) -> list[str]:
  """Converts an applied Filters field into a multiselect ``default=``.

  Args:
    applied_values: The filter's last-applied selection, e.g.
      ``Filters().agents``.

  Returns:
    An empty list for the "all values" sentinel, else the applied values.
  """
  return (
      [] if tuple(applied_values) == (ALL_SENTINEL,) else list(applied_values)
  )


def sidebar_connection() -> tuple[TableRefs | None, int]:
  """Resolves the BigQuery source from the environment, then the form.

  ``BQ_PROJECT_ID`` / ``BQ_DATASET_ID`` / ``BQ_TABLE_ID`` / ``BQ_VIEW_PREFIX``
  seed the fields; the form is the fallback when they are unset and the override
  for dataset, table, and view prefix when they are wrong. Project ID is not
  overridable when ``BQ_PROJECT_ID`` is set. It is a form so that
  changing multiple configuration settings costs one rerun rather than five.

  ``_connect_attempted`` latches in session state once the Connect button is
  clicked, persisting for the session until a valid connection or reset.

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
    connected = st.form_submit_button("Connect", width="stretch")
    if connected:
      st.session_state["_connect_attempted"] = True

  if env_project:
    project = env_project
  refs, errors = validate_refs(project, dataset, table, prefix)
  if st.session_state.get("_connect_attempted") or bool(dataset):
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


def reset_filters() -> None:
  """Resets applied and widget filter states to defaults.

  Pricing inputs (flt_price_in, flt_price_out) are intentionally preserved
  across connection changes.
  """
  st.session_state["applied_filters"] = Filters()
  st.session_state.pop("_filter_options", None)
  for key, _ in _FILTER_WIDGETS:
    st.session_state.pop(key, None)
  st.session_state.pop("_selected_session_id", None)


def _pending(key: str) -> list[str]:
  """Reads one filter widget's submitted value out of session_state."""
  value = st.session_state.get(key, [])
  if not isinstance(value, (list, tuple)):
    return [str(value)] if value else []
  return [str(item) for item in value]


def _commit_filters() -> None:
  """Promotes the submitted widget values into applied_filters.

  Runs as the Apply button's on_click callback.
  """
  applied = Filters(
      agents=as_filter_values(_pending("flt_agent")),
      user_ids=as_filter_values(_pending("flt_user_id")),
      event_types=as_filter_values(_pending("flt_event_type")),
      session_ids=as_filter_values(_pending("flt_session_id")),
  )
  st.session_state["applied_filters"] = applied
  for key, kind in _FILTER_WIDGETS:
    st.session_state[key] = _default_for(getattr(applied, f"{kind}s"))


def sidebar_filters(
    options: dict[str, list[str]],
) -> tuple[Filters, float, float]:
  """Renders the filter and pricing form in the sidebar.

  A form, so a multi-select that a user is still building does not fire a
  query per keystroke: every panel re-queries once, on Apply filters.

  Args:
    options: Map of filter kinds to available option string lists.

  Returns:
    A tuple of (Filters instance, price_in float, price_out float).
  """
  st.sidebar.subheader("Filters")
  applied: Filters = st.session_state.setdefault("applied_filters", Filters())

  for key, kind in _FILTER_WIDGETS:
    field = f"{kind}s"
    if key not in st.session_state:
      st.session_state[key] = _default_for(getattr(applied, field))

  with st.sidebar.form("filters"):
    st.multiselect(
        "Agent",
        options=_seed_options(
            "flt_agent", options.get("agent", []), applied.agents
        ),
        key="flt_agent",
        accept_new_options=True,
    )
    st.multiselect(
        "User",
        options=_seed_options(
            "flt_user_id", options.get("user_id", []), applied.user_ids
        ),
        key="flt_user_id",
        accept_new_options=True,
    )
    st.multiselect(
        "Event type",
        options=_seed_options(
            "flt_event_type", options.get("event_type", []), applied.event_types
        ),
        key="flt_event_type",
        help=(
            "Honored by Events over time, Events by agent, Recent sessions"
            " and Trace detail. Error panels and view-backed panels are"
            " exempt — see dashboards/grafana/queries/README.md."
        ),
    )
    st.multiselect(
        "Session",
        options=_seed_options(
            "flt_session_id", options.get("session_id", []), applied.session_ids
        ),
        key="flt_session_id",
        accept_new_options=True,
    )
    st.caption("An empty selection means all values.")
    st.divider()
    st.caption("Cost is derived from token counts, not recorded telemetry.")
    price_in = st.number_input(
        "USD per 1M input tokens",
        min_value=0.0,
        value=1.25,
        step=0.25,
        format="%.4f",
        key="flt_price_in",
    )
    price_out = st.number_input(
        "USD per 1M output tokens",
        min_value=0.0,
        value=5.00,
        step=0.25,
        format="%.4f",
        key="flt_price_out",
    )
    st.form_submit_button(
        "Apply filters", width="stretch", on_click=_commit_filters
    )

  return st.session_state["applied_filters"], float(price_in), float(price_out)


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
        "—" if pd.isna(latency) else f"{round(float(latency) + 1e-9):,.0f} ms",
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
  # If a previously chosen session exists and is still in ids, maintain its selection.
  # `_selected_session_id` must remain a non-widget key (stored in session_state,
  # not passed as key="...") so that dynamically computed default_idx does not
  # conflict with Streamlit's internal widget key state tracking or raise
  # StreamlitAPIException when session options change between queries.
  prev_chosen = st.session_state.get("_selected_session_id")
  default_idx = ids.index(prev_chosen) if prev_chosen in ids else 0
  chosen = st.selectbox("Session", options=ids, index=default_idx)
  st.session_state["_selected_session_id"] = chosen
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
  total_billed = 0
  total_processed = 0
  cached_count = 0
  cached_processed = 0
  unknown_billed_count = 0
  unknown_fresh_scan_count = 0
  unknown_cached_scan_count = 0

  for entry in ctx.scan_log:
    label, billed, processed, hit, billed_known, processed_known = entry

    if hit:
      cached_count += 1
      if processed_known:
        cached_processed += processed
      else:
        unknown_cached_scan_count += 1
    else:
      if billed_known:
        total_billed += billed
      else:
        unknown_billed_count += 1

      if processed_known:
        total_processed += processed
      else:
        unknown_fresh_scan_count += 1

  caption = (
      f"{len(ctx.scan_log)} queries this run · "
      f"{humanize_bytes(total_billed)} billed ({humanize_bytes(total_processed)} processed)"
  )
  if cached_count > 0:
    if unknown_cached_scan_count > 0:
      noun = "query" if unknown_cached_scan_count == 1 else "queries"
      caption += (
          f" · {cached_count} served from cache (≥ {humanize_bytes(cached_processed)} scan avoided via cache;"
          f" {unknown_cached_scan_count} {noun} scan size unrecorded)"
      )
    else:
      caption += f" · {cached_count} served from cache ({humanize_bytes(cached_processed)} scan avoided via cache)"

  if unknown_billed_count > 0:
    caption += f" · {unknown_billed_count} with unavailable billing data"
  if unknown_fresh_scan_count > 0:
    caption += f" · {unknown_fresh_scan_count} with unavailable scan data"

  caption += (
      f" · per-query cap {humanize_bytes(ctx.max_bytes)} · "
      f"results cached for {CACHE_TTL_SECONDS // 60} min"
  )

  st.divider()
  st.caption(caption)
  st.caption(
      "10 MB minimum per on-demand query; compute/capacity reservations incur"
      " no per-byte charges."
  )
  st.caption(
      "Due to metadata delivery latency, queries can occasionally succeed"
      " before BigQuery finalizes billing telemetry; unrecorded queries are"
      " excluded from the totals and noted above."
  )


# ------------------------------------------------------------------ #
# BQCA Prompt & Response Logging surface                               #
# ------------------------------------------------------------------ #
# Everything from here to ``main`` runs only when the BQCA surface is
# selected. The bqca_* modules are imported lazily (``_bqca_modules``), so the
# ADK surface, and every test that imports this module to exercise it, never
# loads the BQCA code.


def _bqca_modules() -> tuple[Any, Any, Any]:
  """Imports the BQCA modules on first use.

  Returns:
    A tuple of ``(bqca_models, bqca_queries, bqca_charts)``.
  """
  # pylint: disable=g-import-not-at-top
  import bqca_charts
  import bqca_models
  import bqca_queries

  # pylint: enable=g-import-not-at-top
  return bqca_models, bqca_queries, bqca_charts


def _query_param(name: str) -> str | None:
  """Reads one URL query parameter, or None when unset or not a string."""
  try:
    value = st.query_params.get(name)
  except Exception:  # pylint: disable=broad-exception-caught
    return None
  return value if isinstance(value, str) else None


def _requested_profile() -> str:
  """Returns the profile the URL or the environment asks for, lowercased.

  ``?profile=`` wins over ``BQAA_PROFILE``, so a shared link opens the surface
  it was copied from whatever the server's default is.

  Returns:
    The requested profile, or an empty string when none was requested.
  """
  for candidate in (_query_param("profile"), os.environ.get("BQAA_PROFILE")):
    if isinstance(candidate, str) and candidate.strip():
      return candidate.strip().lower()
  return ""


def sidebar_surface() -> str:
  """Renders the top-of-sidebar dashboard surface selector.

  The URL or environment only picks the *initial* surface; the radio is the
  source of truth after that. ``index`` depends on inputs that cannot change
  within a session, so the widget keeps its identity across reruns.

  Returns:
    ``ADK_SURFACE`` or ``BQCA_SURFACE``.
  """
  index = (
      SURFACES.index(BQCA_SURFACE)
      if _requested_profile() == BQCA_PROFILE
      else 0
  )
  return st.sidebar.radio(
      "Dashboard Surface",
      options=list(SURFACES),
      index=index,
      key="_dashboard_surface",
      help=(
          "ADK Agents: dashboards for ADK agent telemetry. BQCA Prompt &"
          " Response Logging: dashboards for Conversational Analytics prompt"
          " and response logs. Open straight into BQCA with `?profile=bqca`"
          " or `BQAA_PROFILE=bqca`."
      ),
  )


def sidebar_connection_bqca() -> tuple[TableRefs | None, int]:
  """Resolves the BQCA logs table from the environment, then the form.

  The same contract as ``sidebar_connection``: the environment seeds the
  fields, the form overrides them, and Project ID is locked when
  ``BQ_PROJECT_ID`` is set. Two BQCA differences: the table comes from
  ``BQCA_TABLE_ID``, then the shared ``BQ_TABLE_ID``, then
  ``bqca_prompt_response_logs`` (so a ``.env`` that points the ADK surface at
  ``agent_events`` does not have to be edited), and there is no typed-view
  prefix because the BQCA panels read the raw table.

  Returns:
    A tuple containing validated TableRefs (or None if invalid) and the
    selected byte cap limit.
  """
  bqca_models, _, _ = _bqca_modules()
  env_project = os.environ.get("BQ_PROJECT_ID", "")
  env_dataset = os.environ.get("BQ_DATASET_ID", "")
  env_table = (
      os.environ.get("BQCA_TABLE_ID", "")
      or os.environ.get("BQ_TABLE_ID", "")
      or bqca_models.BQCA_DEFAULT_TABLE_ID
  )

  st.sidebar.subheader("BigQuery source")

  with st.sidebar.form("bqca_connection"):
    project = st.text_input(
        "Project ID",
        value=env_project,
        disabled=bool(env_project),
        key="bqca_project",
    )
    dataset = st.text_input("Dataset ID", value=env_dataset, key="bqca_dataset")
    table = st.text_input(
        "Events table",
        value=env_table,
        key="bqca_table",
        help=(
            "The BQCA prompt and response logs table"
            f" (`{bqca_models.BQCA_DEFAULT_TABLE_ID}` by default)."
        ),
    )
    cap_label = st.selectbox(
        "Per-query scan cap",
        options=list(BYTES_CAPS),
        index=list(BYTES_CAPS).index(DEFAULT_BYTES_CAP),
        key="bqca_scan_cap",
        help=(
            "Sets `maximum_bytes_billed` on every job. BigQuery refuses a"
            " query that would exceed it rather than billing for it."
        ),
    )
    connected = st.form_submit_button("Connect", width="stretch")
    if connected:
      st.session_state["_bqca_connect_attempted"] = True

  if env_project:
    project = env_project
  refs, errors = validate_refs(project, dataset, table, "")
  if st.session_state.get("_bqca_connect_attempted") or bool(dataset):
    for message in errors:
      st.sidebar.error(message)
  return refs, BYTES_CAPS[cap_label]


def sidebar_window_bqca(
    state: BqcaFilterState,
) -> tuple[BqcaFilterState, Window]:
  """Renders the BQCA time range picker in the sidebar.

  Presets resolve through ``BqcaFilterState.window`` (snapped, so reruns hit
  the result cache). "Custom range" takes inclusive UTC calendar days.

  Args:
    state: The filter state to complete with the chosen window.

  Returns:
    The state carrying the chosen window, and that window resolved.
  """
  bqca_models, _, _ = _bqca_modules()
  windows = bqca_models.BQCA_TIME_WINDOWS
  token_for = {label: token for token, (label, _) in windows.items()}
  labels = list(token_for)
  st.sidebar.subheader("Time range")
  label = st.sidebar.selectbox(
      "Range",
      options=labels,
      index=labels.index(windows[bqca_models.DEFAULT_TIME_WINDOW][0]),
      label_visibility="collapsed",
      key="bqca_range",
  )
  token = token_for[label]
  start = end = None
  if token == bqca_models.CUSTOM_WINDOW:
    today = dt.datetime.now(dt.timezone.utc).date()
    picked = st.sidebar.date_input(
        "Custom range (UTC days, inclusive)",
        value=(today - dt.timedelta(days=6), today),
        key="bqca_custom_range",
    )
    days = tuple(picked) if isinstance(picked, (list, tuple)) else (picked,)
    if len(days) != 2:
      st.sidebar.info("Pick an end date to apply the custom range.")
      st.stop()
    utc = dt.timezone.utc
    start = dt.datetime.combine(days[0], dt.time.min, tzinfo=utc)
    # The picker's end day is inclusive; the window's end is exclusive.
    end = dt.datetime.combine(days[1], dt.time.min, tzinfo=utc) + dt.timedelta(
        days=1
    )
  state = dataclasses.replace(
      state, time_window=token, custom_start=start, custom_end=end
  )
  try:
    window = state.window()
  except ValueError as exc:
    st.sidebar.error(str(exc))
    st.stop()
  st.sidebar.caption(
      f"{window.start:%Y-%m-%d %H:%M} → {window.end:%Y-%m-%d %H:%M} UTC"
      f" · {window.bucket.lower()} buckets"
  )
  return state, window


def _bqca_default_applied(bqca_models: Any) -> dict[str, Any]:
  """Returns the applied BQCA filters when nothing has been applied yet."""
  return {
      "data_agent_ids": (),
      "personas": (),
      "event_types": (),
      "fast_path_mode": bqca_models.FAST_PATH_ALL,
      "session_search": "",
      "prompt_search": "",
      "errors_only": False,
  }


def reset_bqca_filters() -> None:
  """Resets the BQCA applied and widget filter states to their defaults."""
  st.session_state.pop("_bqca_applied", None)
  st.session_state.pop("_bqca_filter_options", None)
  st.session_state.pop("_bqca_selected_turn", None)
  for key in _BQCA_WIDGET_KEYS.values():
    st.session_state.pop(key, None)


def _commit_bqca_filters() -> None:
  """Promotes the submitted BQCA widget values into ``_bqca_applied``.

  Runs as the Apply button's on_click callback, before the rerun.
  """
  keys = _BQCA_WIDGET_KEYS
  state = st.session_state
  state["_bqca_applied"] = {
      "data_agent_ids": tuple(_pending(keys["data_agent_ids"])),
      "personas": tuple(_pending(keys["personas"])),
      "event_types": tuple(_pending(keys["event_types"])),
      "fast_path_mode": str(state.get(keys["fast_path_mode"], "")),
      "session_search": str(state.get(keys["session_search"], "")).strip(),
      "prompt_search": str(state.get(keys["prompt_search"], "")).strip(),
      "errors_only": bool(state.get(keys["errors_only"], False)),
  }


def sidebar_filters_bqca(
    options: dict[str, list[str]], state: BqcaFilterState
) -> BqcaFilterState:
  """Renders the BQCA filter form in the sidebar.

  A form, so a multi-select that is still being built does not fire a query
  per keystroke: every panel re-queries once, on Apply filters.

  Args:
    options: Map of ``data_agent_id`` / ``persona`` / ``event_type`` to the
      values seen in the window.
    state: The ``BqcaFilterState`` carrying the connection and time window.

  Returns:
    ``state`` carrying the last-applied filters.
  """
  bqca_models, _, _ = _bqca_modules()
  keys = _BQCA_WIDGET_KEYS
  st.sidebar.subheader("Filters")
  applied = st.session_state.setdefault(
      "_bqca_applied", _bqca_default_applied(bqca_models)
  )
  for field, key in keys.items():
    if key not in st.session_state:
      value = applied[field]
      st.session_state[key] = list(value) if isinstance(value, tuple) else value

  with st.sidebar.form("bqca_filters"):
    st.multiselect(
        "Data agent",
        options=_seed_options(
            keys["data_agent_ids"],
            options.get("data_agent_id", []),
            applied["data_agent_ids"],
        ),
        key=keys["data_agent_ids"],
        accept_new_options=True,
        help="The data agent that served the turn (its `data-agent-id` label).",
    )
    st.multiselect(
        "Persona",
        options=_seed_options(
            keys["personas"],
            options.get("persona", []),
            applied["personas"],
        ),
        key=keys["personas"],
        accept_new_options=True,
        help=(
            "The `persona` custom label, else the user's handle, else the"
            " data agent."
        ),
    )
    st.multiselect(
        "Event type",
        options=list(bqca_models.BQCA_ALLOWED_EVENT_TYPES),
        key=keys["event_types"],
        help=(
            "Narrows Error Attribution and the turn timeline. Turn-level"
            " panels always count whole turns, so picking one event type"
            " there would zero them out."
        ),
    )
    st.radio(
        "Fast path",
        options=list(bqca_models.FAST_PATH_MODES),
        key=keys["fast_path_mode"],
        help=(
            "Fast-path turns answer from a saved query: no LLM call and no"
            " tokens."
        ),
    )
    st.text_input(
        "Session / conversation",
        key=keys["session_search"],
        help="Case-insensitive substring of a session ID or conversation ID.",
    )
    st.text_input(
        "Prompt contains",
        key=keys["prompt_search"],
        help=(
            "Case-insensitive substring of the user's prompt. Applies to the"
            " Prompt, Response & SQL Explorer."
        ),
    )
    st.toggle(
        "Errors only",
        key=keys["errors_only"],
        help="Keep only turns that contain at least one error event.",
    )
    st.caption("An empty selection means all values.")
    st.form_submit_button(
        "Apply filters", width="stretch", on_click=_commit_bqca_filters
    )

  applied = st.session_state["_bqca_applied"]
  mode = applied["fast_path_mode"]
  return dataclasses.replace(
      state,
      data_agent_ids=tuple(applied["data_agent_ids"]),
      personas=tuple(applied["personas"]),
      event_types=tuple(applied["event_types"]),
      fast_path_mode=(
          mode
          if mode in bqca_models.FAST_PATH_MODES
          else bqca_models.FAST_PATH_ALL
      ),
      session_search=str(applied["session_search"]),
      prompt_search=str(applied["prompt_search"]),
      errors_only=bool(applied["errors_only"]),
  )


def _bqca_count(value: Any) -> str:
  """Formats a count, with a dash for a missing value."""
  return "—" if pd.isna(value) else f"{int(value):,}"


def _bqca_rate(value: Any) -> str:
  """Formats a 0..1 fraction as a percentage, with a dash when undefined."""
  return "—" if pd.isna(value) else f"{float(value) * 100:.1f}%"


def _bqca_ms(value: Any) -> str:
  """Formats a latency in milliseconds, with a dash when undefined."""
  return "—" if pd.isna(value) else f"{float(value):,.0f} ms"


def _code(value: Any) -> str:
  """Wraps a value in a markdown code span, neutralizing backticks."""
  return f"`{str(value).replace('`', chr(39))}`"


def _fig_or_none(fig: Any) -> Any:
  """Returns ``fig`` when it draws something, else None (table view only)."""
  return fig if fig.data else None


_BQCA_KPIS = (
    (
        "Total Turns",
        "A turn is one invocation (`invocation_id`) in scope, completed or not.",
    ),
    (
        "Turn Error Rate",
        "Share of turns with at least one error event: status ERROR, an error"
        " message, or an event type ending in _ERROR.",
    ),
    (
        "P50 Turn Latency",
        "Approximate median (APPROX_QUANTILES) INVOCATION_COMPLETED latency"
        " over completed turns.",
    ),
    (
        "P95 Turn Latency",
        "Approximate 95th-percentile (APPROX_QUANTILES) INVOCATION_COMPLETED"
        " latency over completed turns.",
    ),
    ("Total Tokens", "Summed over LLM_RESPONSE events."),
    ("Thinking Tokens", "Reasoning tokens, summed over LLM_RESPONSE events."),
    ("Cached Tokens", "Cached prompt tokens, summed over LLM_RESPONSE events."),
    (
        "Fast-Path Rate",
        "Share of completed turns answered from a saved query"
        " (`fast_path = true`), with no LLM call.",
    ),
    (
        "Embedding Suggestion Coverage",
        "Share of turns that received at least one EMBEDDING_SUGGESTION with"
        " suggested columns.",
    ),
)


def _bqca_completion_note(kpi: Any) -> str:
  """Says how many turns completed, and what an incomplete turn is left out of.

  Args:
    kpi: The ``BqcaKpiSummary`` of the KPI strip.

  Returns:
    One caption line: all turns completed, or how many are incomplete.
  """
  if not kpi.incomplete_turns:
    return (
        f"All {kpi.total_turns:,} turns completed (reached"
        " INVOCATION_COMPLETED)."
    )
  return (
      f"{kpi.completed_turns:,} of {kpi.total_turns:,} turns completed;"
      f" {kpi.incomplete_turns:,} incomplete (no INVOCATION_COMPLETED event:"
      " still running, failed before completing, or cut off by the time"
      " range). Latency percentiles (approximate, via BigQuery"
      " APPROX_QUANTILES) and the fast-path rate cover completed turns only."
  )


def row_bqca_kpis(state: BqcaFilterState, ctx: Context) -> None:
  """Renders the KPI header strip (panel P1) above the BQCA tabs.

  Args:
    state: Active BQCA filters.
    ctx: Active dashboard context.
  """
  bqca_models, bqca_queries, _ = _bqca_modules()
  result = bqca_queries.fetch_panel("kpis", state, ctx)
  kpi = bqca_models.BqcaKpiSummary.from_frame(result.df)
  if kpi is None:
    values = ["—"] * len(_BQCA_KPIS)
  else:
    row = result.df.iloc[0]
    # A rate over an empty denominator is NULL in SQL (no completed turn, no
    # turn at all): show a dash, not a confident 0%.
    turn_error_rate = (
        None if pd.isna(row.get("turn_error_rate")) else kpi.turn_error_rate
    )
    fast_path = (
        None if pd.isna(row.get("fast_path_rate")) else kpi.fast_path_rate
    )
    embedding = (
        None
        if pd.isna(row.get("embedding_coverage"))
        else kpi.embedding_coverage
    )
    values = [
        _bqca_count(kpi.total_turns),
        _bqca_rate(turn_error_rate),
        _bqca_ms(kpi.p50_turn_latency_ms),
        _bqca_ms(kpi.p95_turn_latency_ms),
        _bqca_count(kpi.total_tokens),
        _bqca_count(kpi.thoughts_tokens),
        _bqca_count(kpi.cached_tokens),
        _bqca_rate(fast_path),
        _bqca_rate(embedding),
    ]
  columns = [*st.columns(4), *st.columns(5)]
  for column, (label, help_text), value in zip(columns, _BQCA_KPIS, values):
    _metric(column, label, value, help_text)
  if kpi is not None and kpi.total_turns:
    st.caption(_bqca_completion_note(kpi))
  elif kpi is not None and kpi.total_tokens:
    # Events exist but none belongs to a turn: the token totals are real, the
    # turn-based figures are undefined, and the reader should be told why.
    st.caption(
        "0 attributed turns (all events in scope have missing or blank"
        " invocation_id); token totals include unattributed LLM_RESPONSE"
        " events."
    )


def _latency_view(
    df: pd.DataFrame, columns: Sequence[str], *, overall: bool
) -> pd.DataFrame:
  """Slices the latency frame into the rows and columns one chart draws.

  Args:
    df: The ``latency`` panel's result.
    columns: Columns to keep (those present).
    overall: True for the rows that cover every path, False for the
      per-path rows.

  Returns:
    The sliced frame without rows that carry no latency at all.
  """
  _, _, bqca_charts = _bqca_modules()
  if df.empty or "fast_path_label" not in df.columns:
    return df.iloc[0:0]
  is_overall = df["fast_path_label"] == bqca_charts.ALL_PATHS_LABEL
  rows = df[is_overall if overall else ~is_overall]
  keep = [column for column in columns if column in rows.columns]
  metrics = [column for column in keep if column.endswith("_ms")]
  return rows[keep].dropna(how="all", subset=metrics).reset_index(drop=True)


def row_bqca_overview(state: BqcaFilterState, ctx: Context) -> None:
  """Renders the Overview & Latency tab (panels P2 and P3).

  Args:
    state: Active BQCA filters.
    ctx: Active dashboard context.
  """
  _, bqca_queries, bqca_charts = _bqca_modules()
  volume = bqca_queries.fetch_panel("turn_volume", state, ctx)
  latency = bqca_queries.fetch_panel("latency", state, ctx)

  left, right = st.columns(2)
  with left:
    panel(
        "Turn volume by path",
        _fig_or_none(bqca_charts.turn_volume_chart(volume.df, ctx)),
        volume.df,
        key="bqca_turn_volume",
    )
    st.caption(
        "A turn is one invocation. The line counts turns with at least one"
        " error event."
    )
  with right:
    percentiles = _latency_view(
        latency.df,
        ("bucket", "turn_p50_ms", "turn_p95_ms", "turn_p99_ms"),
        overall=True,
    )
    panel(
        "Turn latency percentiles (ms)",
        _fig_or_none(bqca_charts.latency_percentiles_chart(latency.df, ctx)),
        percentiles,
        empty="No completed turns in this range.",
        key="bqca_latency_percentiles",
    )

  left, right = st.columns(2)
  with left:
    by_path = _latency_view(
        latency.df,
        ("bucket", "fast_path_label", "turn_p50_ms", "turn_p95_ms"),
        overall=False,
    )
    panel(
        "Fast path vs standard NL2SQL: turn latency (ms)",
        _fig_or_none(bqca_charts.path_latency_chart(latency.df, ctx)),
        by_path,
        empty="No completed turns in this range.",
        key="bqca_path_latency",
    )
  with right:
    llm = _latency_view(
        latency.df,
        ("bucket", "llm_p50_ms", "llm_p95_ms", "tfft_p50_ms", "tfft_p95_ms"),
        overall=True,
    )
    panel(
        "LLM latency and time to first token (ms)",
        _fig_or_none(bqca_charts.llm_latency_chart(latency.df, ctx)),
        llm,
        empty="No LLM responses in this range.",
        key="bqca_llm_latency",
    )
    st.caption("Fast-path turns make no LLM call, so they have no LLM latency.")
  st.caption(
      "Latency percentiles are approximate (BigQuery APPROX_QUANTILES); on a"
      " small window they can differ visibly from exact values."
  )


def row_bqca_agents(state: BqcaFilterState, ctx: Context) -> None:
  """Renders the Data Agents & Personas tab (panels P5 and P6).

  Args:
    state: Active BQCA filters.
    ctx: Active dashboard context.
  """
  _, bqca_queries, bqca_charts = _bqca_modules()
  agents = bqca_queries.fetch_panel("data_agents", state, ctx)
  personas = bqca_queries.fetch_panel("personas", state, ctx)

  left, right = st.columns(2)
  with left:
    panel(
        "Data agents: turns",
        _fig_or_none(bqca_charts.data_agent_leaderboard_chart(agents.df, ctx)),
        agents.df,
        empty="No data-agent turns in this range.",
        key="bqca_data_agents",
    )
    st.caption(
        "Turns that carry no data-agent id are grouped as unattributed. P95"
        " latency is approximate (BigQuery APPROX_QUANTILES)."
    )
  with right:
    panel(
        "Personas: turns",
        _fig_or_none(bqca_charts.persona_breakdown_chart(personas.df, ctx)),
        personas.df,
        empty="No persona turns in this range.",
        key="bqca_personas",
    )
    st.caption(
        "Persona is the persona custom label, else the user's handle, else"
        " the data agent."
    )


_BQCA_EXPLORER_COLUMNS = (
    "timestamp",
    "invocation_id",
    "data_agent_id",
    "persona",
    "status",
    "fast_path",
    "turn_latency_ms",
    "total_tokens",
    "user_prompt",
)


def _bqca_turn_label(turn: Any) -> str:
  """Describes a turn in one line for the explorer's picker.

  The prompt and data agent are customer-logged text in a widget that can
  render Markdown, so the label is made inert like the other logged text.
  """
  bqca_models, _, _ = _bqca_modules()
  prompt = " ".join((turn.user_prompt or "").split())
  if len(prompt) > 60:
    prompt = f"{prompt[:57]}..."
  stamp = f"{turn.timestamp:%Y-%m-%d %H:%M:%S}"
  agent = turn.data_agent_id or "unattributed"
  return bqca_models.inert_markdown(
      f"{stamp} · {agent} · {prompt or '(no prompt logged)'}"
  )


def _render_bqca_turn(turn: Any, state: BqcaFilterState, ctx: Context) -> None:
  """Renders one turn: prompt, rendered response, SQL, errors and timeline.

  Args:
    turn: The selected ``BqcaTurnRow``.
    state: Active BQCA filters.
    ctx: Active dashboard context.
  """
  bqca_models, bqca_queries, _ = _bqca_modules()
  cols = st.columns(5)
  _metric(cols[0], "Status", turn.status)
  _metric(cols[1], "Path", "Fast path" if turn.fast_path else "Standard NL2SQL")
  _metric(cols[2], "Turn latency", _bqca_ms(turn.turn_latency_ms))
  _metric(cols[3], "Tokens", _bqca_count(turn.total_tokens))
  _metric(cols[4], "Thinking tokens", _bqca_count(turn.thoughts_tokens))
  st.caption(
      f"Data agent {_code(turn.data_agent_id or 'unattributed')} · persona"
      f" {_code(turn.persona)} · conversation"
      f" {_code(turn.conversation_id or '—')} · session"
      f" {_code(turn.session_id or '—')} · invocation"
      f" {_code(turn.invocation_id or '—')}"
  )

  with st.expander("Prompt", expanded=True):
    st.text(turn.user_prompt or "(no prompt logged for this turn)")
  with st.expander("Response", expanded=True):
    # Markdown only: HTML in a logged answer is never rendered, and Markdown
    # images are turned into links so a logged answer cannot make the
    # viewer's browser fetch a remote image.
    st.markdown(
        bqca_models.inert_markdown(turn.agent_response)
        or "_(no response logged for this turn)_"
    )
    if (turn.agent_response_count or 0) > 1:
      st.caption(
          f"This turn logged {turn.agent_response_count} AGENT_RESPONSE"
          " events. The last one, the answer that was served, is shown; the"
          " earlier ones were superseded."
      )
  if turn.extracted_sql:
    st.markdown("**SQL in the response**")
    st.code(turn.extracted_sql, language="sql")
  else:
    st.caption("No SQL block was found in this response.")
  if turn.error_message:
    # ``st.error`` renders its body as Markdown, like the response above.
    st.error(bqca_models.inert_markdown(turn.error_message))

  if not turn.invocation_id or not turn.invocation_id.strip():
    # The timeline is keyed on the invocation id, and the query builder
    # rejects a blank one, so there is nothing to look up for this turn.
    st.caption("No valid invocation ID is associated with this turn.")
  else:
    timeline = bqca_queries.fetch_panel(
        "timeline", state, ctx, invocation_id=turn.invocation_id
    )
    panel(
        "Turn timeline",
        None,
        timeline.df,
        empty="No events for this turn in the range.",
    )


def row_bqca_explorer(state: BqcaFilterState, ctx: Context) -> None:
  """Renders the Prompt, Response & SQL Explorer tab (panels P8 and P9).

  Args:
    state: Active BQCA filters.
    ctx: Active dashboard context.
  """
  bqca_models, bqca_queries, _ = _bqca_modules()
  turns = bqca_queries.fetch_panel("turns", state, ctx)
  shown = (
      turns.df[[c for c in _BQCA_EXPLORER_COLUMNS if c in turns.df.columns]]
      if not turns.df.empty
      else turns.df
  )
  panel(
      f"Turns (most recent {bqca_models.TURN_EXPLORER_LIMIT})",
      None,
      shown,
      empty="No turns match the filters in this range.",
  )
  st.caption(
      "This tab reads each turn's full prompt and response, so it scans more"
      " than the others on a wide range. Prompt search applies here only."
  )
  rows = bqca_models.BqcaTurnRow.from_frame(turns.df)
  if not rows:
    return

  st.divider()
  st.markdown("**Turn detail**")
  labels = {row.invocation_id: _bqca_turn_label(row) for row in rows}
  ids = list(labels)
  # `_bqca_selected_turn` must stay a non-widget key (not passed as key=...),
  # so a dynamically computed index cannot conflict with the widget's own
  # state when the options change between queries.
  previous = st.session_state.get("_bqca_selected_turn")
  chosen = st.selectbox(
      "Turn",
      options=ids,
      index=ids.index(previous) if previous in ids else 0,
      format_func=lambda invocation_id: labels[invocation_id],
  )
  st.session_state["_bqca_selected_turn"] = chosen
  _render_bqca_turn(
      next(row for row in rows if row.invocation_id == chosen), state, ctx
  )


def row_bqca_tokens(state: BqcaFilterState, ctx: Context) -> None:
  """Renders the Tokens & Embedding Suggestions tab (panels P4 and P7).

  Args:
    state: Active BQCA filters.
    ctx: Active dashboard context.
  """
  _, bqca_queries, bqca_charts = _bqca_modules()
  usage = bqca_queries.fetch_panel("token_usage", state, ctx)
  embedding = bqca_queries.fetch_panel("embedding", state, ctx)

  left, right = st.columns(2)
  with left:
    panel(
        "Token usage over time",
        _fig_or_none(bqca_charts.token_breakdown_chart(usage.df, ctx)),
        usage.df,
        empty="No LLM responses in this range.",
        key="bqca_tokens",
    )
    st.caption(
        "Input (uncached) is prompt tokens minus cached tokens, so the"
        " segments add up to the real volume. Table view lists the raw"
        " columns."
    )
  with right:
    panel(
        "Tokens by model",
        _fig_or_none(bqca_charts.tokens_by_model_chart(usage.df, ctx)),
        usage.df,
        empty="No LLM responses in this range.",
        key="bqca_tokens_by_model",
    )

  left, right = st.columns(2)
  with left:
    panel(
        "Embedding suggestions by reason",
        _fig_or_none(
            bqca_charts.embedding_suggestions_chart(embedding.df, ctx)
        ),
        embedding.df,
        empty="No embedding suggestions in this range.",
        key="bqca_embedding",
    )
    st.caption(
        "Each suggestion is one logged EMBEDDING_SUGGESTION event, tagged with"
        " the reason the agent made it. Table view lists the suggested"
        " columns per time bucket and reason."
    )
  with right:
    panel(
        "Columns suggested per suggestion",
        _fig_or_none(bqca_charts.suggested_columns_chart(embedding.df, ctx)),
        embedding.df,
        empty="No embedding suggestions in this range.",
        key="bqca_suggested_columns",
    )


def row_bqca_errors(state: BqcaFilterState, ctx: Context) -> None:
  """Renders the Error Attribution tab (panel P10).

  Args:
    state: Active BQCA filters.
    ctx: Active dashboard context.
  """
  bqca_models, bqca_queries, bqca_charts = _bqca_modules()
  errors = bqca_queries.fetch_panel("errors", state, ctx)
  totals = errors.df
  if not totals.empty and {"data_agent_id", "event_type"} <= set(
      totals.columns
  ):
    totals = (
        totals.groupby(["data_agent_id", "event_type"], as_index=False)[
            "errors"
        ]
        .sum()
        .sort_values("errors", ascending=False)
    )
  panel(
      "Errors by data agent and event type",
      _fig_or_none(bqca_charts.error_attribution_chart(errors.df, ctx)),
      totals,
      empty="No errors in this range.",
      key="bqca_errors",
  )
  panel(
      f"Error groups (top {bqca_models.ERROR_ATTRIBUTION_LIMIT})",
      None,
      errors.df,
      empty="No errors in this range.",
  )
  st.caption(
      "An event counts as an error when its status is ERROR, it carries an"
      " error message, or its event type ends in _ERROR. Groups share an"
      " event type, data agent, persona and message (first 300 characters)."
      " The Event type filter narrows this tab."
  )


def main_bqca() -> None:
  """Runs the BQCA Prompt & Response Logging surface."""
  bqca_models, bqca_queries, _ = _bqca_modules()
  st.caption(
      "Conversational Analytics prompt and response logs: turn volume,"
      " latency, tokens, data agents, personas and errors."
  )
  theme = active_theme()
  refs, max_bytes = sidebar_connection_bqca()
  if refs is None:
    st.info(
        "Set `BQ_PROJECT_ID`, `BQ_DATASET_ID` and `BQCA_TABLE_ID` (or"
        f" `BQ_TABLE_ID`; default `{bqca_models.BQCA_DEFAULT_TABLE_ID}`), or"
        " fill in the sidebar, to connect."
    )
    st.stop()

  prev_refs = st.session_state.get("_bqca_last_refs")
  if prev_refs is not None and prev_refs != refs:
    reset_bqca_filters()
  st.session_state["_bqca_last_refs"] = refs

  state, window = sidebar_window_bqca(
      bqca_models.BqcaFilterState(
          project_id=refs.project,
          dataset_id=refs.dataset,
          table_id=refs.table,
      )
  )

  # BQCA has no per-panel Filters or pricing: the filters travel in ``state``.
  # The context only carries the table, the window, the scan cap and the scan
  # log the footer reports.
  ctx = Context(
      refs=refs,
      window=window,
      filters=Filters(),
      max_bytes=max_bytes,
      theme=theme,
      price_in=0.0,
      price_out=0.0,
  )

  # Options are read unfiltered, so the sidebar can be drawn before any panel
  # runs and picking one data agent never hides the others.
  options, result = bqca_queries.load_bqca_filter_options(state, ctx)
  if result.error is None and options:
    st.session_state["_bqca_filter_options"] = options
  else:
    options = st.session_state.get("_bqca_filter_options", {})

  state = sidebar_filters_bqca(options, state)

  row_bqca_kpis(state, ctx)

  renderers = (
      row_bqca_overview,
      row_bqca_agents,
      row_bqca_explorer,
      row_bqca_tokens,
      row_bqca_errors,
  )
  if _LAZY_TABS:
    tab = st.segmented_control(
        "Dashboard",
        list(BQCA_TABS),
        default=BQCA_TABS[0],
        label_visibility="collapsed",
        required=True,
        key="_bqca_active_tab",
    )
    renderers[BQCA_TABS.index(tab) if tab in BQCA_TABS else 0](state, ctx)
  else:
    for container, render in zip(st.tabs(list(BQCA_TABS)), renderers):
      with container:
        render(state, ctx)

  footer(ctx)


def main() -> None:
  """Runs the main entrypoint for the Streamlit dashboard application."""
  st.set_page_config(page_title=APP_TITLE, page_icon="📊", layout="wide")
  st.title(APP_TITLE)

  if sidebar_surface() == BQCA_SURFACE:
    main_bqca()
    return

  theme = active_theme()
  refs, max_bytes = sidebar_connection()
  if refs is None:
    st.info(
        "Set `BQ_PROJECT_ID`, `BQ_DATASET_ID` and `BQ_TABLE_ID`, or fill in"
        " the sidebar, to connect."
    )
    st.stop()

  prev_refs = st.session_state.get("_last_refs")
  if prev_refs is not None and prev_refs != refs:
    reset_filters()
  st.session_state["_last_refs"] = refs

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
  else:
    options = st.session_state.get("_filter_options", {})

  filters, price_in, price_out = sidebar_filters(options)

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

  if _LAZY_TABS:
    tab = st.segmented_control(
        "Dashboard",
        ["Overview", "LLM & FinOps", "Tools & Execution", "Sessions & Traces"],
        default="Overview",
        label_visibility="collapsed",
        required=True,
        key="_active_tab",
    )
    if tab == "Overview" or tab is None:
      row_overview(ctx)
    elif tab == "LLM & FinOps":
      row_llm(ctx)
    elif tab == "Tools & Execution":
      row_tools(ctx)
    elif tab == "Sessions & Traces":
      row_sessions(ctx)
  else:
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
    "ADK_SURFACE",
    "APP_TITLE",
    "BQCA_SURFACE",
    "BQCA_TABS",
    "_LAZY_TABS",
    "footer",
    "main",
    "main_bqca",
    "reset_bqca_filters",
    "reset_filters",
    "row_bqca_agents",
    "row_bqca_errors",
    "row_bqca_explorer",
    "row_bqca_kpis",
    "row_bqca_overview",
    "row_bqca_tokens",
    "row_llm",
    "row_overview",
    "row_sessions",
    "row_tools",
    "sidebar_connection",
    "sidebar_connection_bqca",
    "sidebar_filters",
    "sidebar_filters_bqca",
    "sidebar_surface",
    "sidebar_window",
    "sidebar_window_bqca",
]

if __name__ == "__main__":
  main()
