"""SQL builders and BigQuery execution helpers for the Streamlit dashboard."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
import re
import sys
import threading

from google.api_core import exceptions as gexc
from google.auth import exceptions as gauth_exc
from google.cloud import bigquery
import pandas as pd
import streamlit as st

_DASHBOARD_DIR = str(Path(__file__).resolve().parent)
if _DASHBOARD_DIR not in sys.path:
  sys.path.insert(0, _DASHBOARD_DIR)

from models import ALL_SENTINEL
from models import CACHE_TTL_SECONDS
from models import Context
from models import FILTER_OPTIONS_LIMIT
from models import Filters
from models import humanize_bytes
from models import QueryResult
from models import TableRefs
from models import TOP_ERRORS_LIMIT
from models import Window

_PARAM_RE = re.compile(r"@([A-Za-z_][A-Za-z0-9_]*)")

# The canonical error predicate: an event is an error if it carries an
# error event type, an error message, or an ERROR status.
_ERROR_PREDICATE = (
    "ENDS_WITH({p}event_type, '_ERROR')"
    " OR {p}error_message IS NOT NULL"
    " OR UPPER({p}status) = 'ERROR'"
)


# ------------------------------------------------------------------ #
# SQL builders (pure)                                                  #
# ------------------------------------------------------------------ #


def _ts_literal(moment: dt.datetime) -> str:
  """Renders a UTC timestamp literal.

  Literal bounds rather than query parameters: BigQuery's partition
  pruning is reliable for constant predicates, and these constants are
  built from ``datetime`` objects the app owns, never from user text.

  Args:
    moment: Datetime object to format in UTC.

  Returns:
    A SQL literal string representation of the timestamp.
  """
  utc = moment.astimezone(dt.timezone.utc)
  return f'TIMESTAMP "{utc.strftime("%Y-%m-%d %H:%M:%S")}+00:00"'


def time_bounds(window: Window, column: str = "timestamp") -> str:
  """Returns a half-open time predicate that prunes ``timestamp`` partitions.

  Args:
    window: Snapped query window with start and end times.
    column: SQL column or expression name to filter against.

  Returns:
    A SQL WHERE clause fragment bounding the column to
    [window.start, window.end).
  """
  return (
      f"{column} >= {_ts_literal(window.start)}\n"
      f"  AND {column} < {_ts_literal(window.end)}"
  )


def _scope(alias: str = "", *, event_type: bool = False) -> str:
  """Renders the filter clauses shared by nearly every panel.

  ``event_type`` defaults off because two exemptions in
  ``grafana/queries/README.md`` apply to most panels: a view-backed query
  is already scoped to one event type, and an error count must stay
  unscoped or it reports zero errors whenever some other type is picked.

  Args:
    alias: Optional table alias to prefix column references with.
    event_type: Whether to include the event_types filter condition.

  Returns:
    A multiline SQL snippet containing AND conditions for filter parameters.
  """
  p = f"{alias}." if alias else ""
  clauses = [
      f"  AND ('{ALL_SENTINEL}' IN UNNEST(@agents)"
      f" OR {p}agent IN UNNEST(@agents))",
      f"  AND ('{ALL_SENTINEL}' IN UNNEST(@user_ids)"
      f" OR {p}user_id IN UNNEST(@user_ids))",
  ]
  if event_type:
    clauses.append(
        f"  AND ('{ALL_SENTINEL}' IN UNNEST(@event_types)"
        f" OR {p}event_type IN UNNEST(@event_types))"
    )
  clauses.append(
      f"  AND ('{ALL_SENTINEL}' IN UNNEST(@session_ids)"
      f" OR {p}session_id IN UNNEST(@session_ids))"
  )
  return "\n".join(clauses)


def build_filter_options_sql(refs: TableRefs, window: Window) -> str:
  """Builds the SQL query for dropdown options in one pass over the raw table.

  Four separate ``SELECT DISTINCT``s would be four table references, and
  BigQuery bills bytes scanned per reference. Cross-joining a literal
  array of structs pivots the four columns into rows instead, so the
  whole sidebar costs one scan of five columns.

  Args:
    refs: Validated BigQuery table references.
    window: Snapped time window.

  Returns:
    The SQL query string.
  """
  return f"""
SELECT
  f.kind,
  f.value,
  MAX(e.timestamp) AS last_seen
FROM {refs.events} AS e,
  UNNEST([
    STRUCT('agent' AS kind, e.agent AS value),
    STRUCT('user_id', e.user_id),
    STRUCT('event_type', e.event_type),
    STRUCT('session_id', e.session_id)
  ]) AS f
WHERE {time_bounds(window, "e.timestamp")}
  AND f.value IS NOT NULL
GROUP BY f.kind, f.value
QUALIFY ROW_NUMBER() OVER (
  PARTITION BY f.kind ORDER BY MAX(e.timestamp) DESC
) <= {FILTER_OPTIONS_LIMIT}
ORDER BY f.kind, f.value
""".strip()


def build_overview_totals_sql(refs: TableRefs, window: Window) -> str:
  """Builds the SQL query for session, event, error, and latency totals.

  ``HAVING COUNT(*) > 0`` keeps the no-data contract: an unaggregated
  SELECT over aggregates always emits a row, so an empty filter
  intersection would otherwise report a confident "0 events, 0% errors".

  Args:
    refs: Validated BigQuery table references.
    window: Snapped time window.

  Returns:
    The SQL query string.
  """
  err = _ERROR_PREDICATE.format(p="e.")
  return f"""
SELECT
  COUNT(DISTINCT e.session_id) AS sessions,
  COUNT(*) AS events,
  SAFE_DIVIDE(COUNTIF({err}), COUNT(*)) AS error_rate,
  (
    SELECT AVG(r.total_ms)
    FROM {refs.view("llm_responses")} AS r
    WHERE {time_bounds(window, "r.timestamp")}
{_scope("r")}
  ) AS avg_llm_latency_ms
FROM {refs.events} AS e
WHERE {time_bounds(window, "e.timestamp")}
{_scope("e")}
HAVING COUNT(*) > 0
""".strip()


def build_events_over_time_sql(refs: TableRefs, window: Window) -> str:
  """Builds the SQL query for event volume per bucket and event_type.

  Args:
    refs: Validated BigQuery table references.
    window: Snapped time window.

  Returns:
    The SQL query string.
  """
  return f"""
SELECT
  TIMESTAMP_TRUNC(timestamp, {window.bucket}) AS bucket,
  event_type,
  COUNT(*) AS events
FROM {refs.events}
WHERE {time_bounds(window)}
{_scope(event_type=True)}
GROUP BY bucket, event_type
ORDER BY bucket
""".strip()


def build_errors_over_time_sql(refs: TableRefs, window: Window) -> str:
  """Builds the SQL query for error volume per bucket.

  No event_type filter: errors arrive as their own event types, so
  honoring a selection like LLM_RESPONSE would chart zero errors in a
  window that had them.

  Args:
    refs: Validated BigQuery table references.
    window: Snapped time window.

  Returns:
    The SQL query string.
  """
  return f"""
SELECT
  TIMESTAMP_TRUNC(timestamp, {window.bucket}) AS bucket,
  event_type,
  COUNT(*) AS errors
FROM {refs.events}
WHERE {time_bounds(window)}
{_scope()}
  AND ({_ERROR_PREDICATE.format(p="")})
GROUP BY bucket, event_type
ORDER BY bucket
""".strip()


def build_events_by_agent_sql(refs: TableRefs, window: Window) -> str:
  """Builds the SQL query for event volume per agent.

  ``agent`` is nullable on the raw table; IFNULL groups those rows under
  "unknown" instead of dropping them, so the bars still add up to the
  Events stat whenever the Event type filter is off.

  Args:
    refs: Validated BigQuery table references.
    window: Snapped time window.

  Returns:
    The SQL query string.
  """
  return f"""
SELECT
  IFNULL(agent, 'unknown') AS agent_name,
  COUNT(*) AS events
FROM {refs.events}
WHERE {time_bounds(window)}
{_scope(event_type=True)}
GROUP BY agent_name
HAVING COUNT(*) > 0
ORDER BY events DESC
""".strip()


def build_top_errors_sql(
    refs: TableRefs, window: Window, limit: int = TOP_ERRORS_LIMIT
) -> str:
  """Builds the SQL query for the loudest failure modes, ranked by occurrence.

  Deliberately narrower than Errors over time: this groups by the message
  text, so an error that recorded no message has no string to group under
  and is absent. These counts are a subset of the chart's.

  Args:
    refs: Validated BigQuery table references.
    window: Snapped time window.
    limit: Maximum number of distinct error messages to return.

  Returns:
    The SQL query string.
  """
  return f"""
SELECT
  error_message,
  COUNT(*) AS errors,
  COUNT(DISTINCT session_id) AS sessions,
  COUNT(DISTINCT agent) AS agents,
  MAX(timestamp) AS last_seen
FROM {refs.events}
WHERE {time_bounds(window)}
{_scope()}
  AND error_message IS NOT NULL
GROUP BY error_message
HAVING COUNT(*) > 0
ORDER BY errors DESC
LIMIT {int(limit)}
""".strip()


def build_llm_tokens_over_time_sql(refs: TableRefs, window: Window) -> str:
  """Builds the SQL query for prompt and completion tokens per bucket.

  Missing usage telemetry degrades to 0 rather than dropping the bucket.

  Args:
    refs: Validated BigQuery table references.
    window: Snapped time window.

  Returns:
    The SQL query string.
  """
  return f"""
SELECT
  TIMESTAMP_TRUNC(timestamp, {window.bucket}) AS bucket,
  IFNULL(SUM(usage_prompt_tokens), 0) AS prompt_tokens,
  IFNULL(SUM(usage_completion_tokens), 0) AS completion_tokens,
  IFNULL(SUM(usage_total_tokens), 0) AS total_tokens
FROM {refs.view("llm_responses")}
WHERE {time_bounds(window)}
{_scope()}
GROUP BY bucket
ORDER BY bucket
""".strip()


def build_llm_latency_percentiles_sql(refs: TableRefs, window: Window) -> str:
  """Builds the SQL query for LLM p50/p95 latency and p50 time-to-first-token.

  Latency aggregates stay NULL when telemetry is missing so the chart
  shows gaps rather than fake zeros.

  Args:
    refs: Validated BigQuery table references.
    window: Snapped time window.

  Returns:
    The SQL query string.
  """
  return f"""
SELECT
  TIMESTAMP_TRUNC(timestamp, {window.bucket}) AS bucket,
  APPROX_QUANTILES(total_ms, 100)[OFFSET(50)] AS p50_total_ms,
  APPROX_QUANTILES(total_ms, 100)[OFFSET(95)] AS p95_total_ms,
  APPROX_QUANTILES(ttft_ms, 100)[OFFSET(50)] AS p50_ttft_ms
FROM {refs.view("llm_responses")}
WHERE {time_bounds(window)}
{_scope()}
GROUP BY bucket
ORDER BY bucket
""".strip()


def build_tokens_by_model_sql(refs: TableRefs, window: Window) -> str:
  """Builds the SQL query for token totals per model.

  Rows without a model group under "unknown".

  Args:
    refs: Validated BigQuery table references.
    window: Snapped time window.

  Returns:
    The SQL query string.
  """
  return f"""
SELECT
  IFNULL(model_version, 'unknown') AS model,
  IFNULL(SUM(usage_prompt_tokens), 0) AS prompt_tokens,
  IFNULL(SUM(usage_completion_tokens), 0) AS completion_tokens,
  IFNULL(SUM(usage_total_tokens), 0) AS total_tokens,
  COUNT(*) AS responses
FROM {refs.view("llm_responses")}
WHERE {time_bounds(window)}
{_scope()}
GROUP BY model
ORDER BY total_tokens DESC
""".strip()


def build_llm_calls_total_sql(refs: TableRefs, window: Window) -> str:
  """Builds the SQL query for LLM call count and token totals in one scan.

  Calls are counted per span, not per row: a streaming installation
  records one LLM_RESPONSE per chunk, so COUNT(*) would report chunks as
  calls. ``trace_id``/``span_id`` are nullable and CONCAT of a NULL is
  NULL, so rows carrying no span key are added back one per row.

  Args:
    refs: Validated BigQuery table references.
    window: Snapped time window.

  Returns:
    The SQL query string.
  """
  return f"""
SELECT
  COUNT(DISTINCT CONCAT(trace_id, '|', span_id))
    + COUNTIF(trace_id IS NULL OR span_id IS NULL) AS llm_calls,
  IFNULL(SUM(usage_prompt_tokens), 0) AS prompt_tokens,
  IFNULL(SUM(usage_completion_tokens), 0) AS completion_tokens,
  IFNULL(SUM(usage_total_tokens), 0) AS total_tokens
FROM {refs.view("llm_responses")}
WHERE {time_bounds(window)}
{_scope()}
HAVING COUNT(*) > 0
""".strip()


def build_tool_usage_sql(refs: TableRefs, window: Window) -> str:
  """Builds the SQL query for invocations per tool.

  Reads tool_starts rather than tool_completions so failed invocations
  still count toward the volume.

  Args:
    refs: Validated BigQuery table references.
    window: Snapped time window.

  Returns:
    The SQL query string.
  """
  return f"""
SELECT
  IFNULL(tool_name, 'unknown') AS tool_name,
  COUNT(*) AS invocations
FROM {refs.view("tool_starts")}
WHERE {time_bounds(window)}
{_scope()}
GROUP BY tool_name
ORDER BY invocations DESC
""".strip()


def build_tool_latency_sql(refs: TableRefs, window: Window) -> str:
  """Builds the SQL query for per-tool latency.

  Latency aggregates stay NULL when telemetry is missing, so gaps do not skew
  the averages.

  Args:
    refs: Validated BigQuery table references.
    window: Snapped time window.

  Returns:
    The SQL query string.
  """
  return f"""
SELECT
  IFNULL(tool_name, 'unknown') AS tool_name,
  COUNT(*) AS completions,
  AVG(total_ms) AS avg_ms,
  APPROX_QUANTILES(total_ms, 100)[OFFSET(50)] AS p50_ms,
  APPROX_QUANTILES(total_ms, 100)[OFFSET(95)] AS p95_ms
FROM {refs.view("tool_completions")}
WHERE {time_bounds(window)}
{_scope()}
GROUP BY tool_name
ORDER BY p95_ms DESC
""".strip()


def build_tool_errors_sql(refs: TableRefs, window: Window, limit: int) -> str:
  """Builds the SQL query for tool failures from error views and completions.

  UNION ALL rather than DISTINCT: when one logical failure emits both a
  TOOL_ERROR and an error-status TOOL_COMPLETED, both telemetry records
  are real and both are kept.

  Args:
    refs: Validated BigQuery table references.
    window: Snapped time window.
    limit: Maximum number of tool error records to return.

  Returns:
    The SQL query string.
  """
  return f"""
SELECT
  timestamp,
  agent,
  session_id,
  tool_name,
  error_message
FROM {refs.view("tool_errors")}
WHERE {time_bounds(window)}
{_scope()}

UNION ALL

SELECT
  timestamp,
  agent,
  session_id,
  tool_name,
  error_message
FROM {refs.view("tool_completions")}
WHERE {time_bounds(window)}
{_scope()}
  AND (error_message IS NOT NULL OR UPPER(status) = 'ERROR')
ORDER BY timestamp DESC
LIMIT {int(limit)}
""".strip()


def build_recent_sessions_sql(
    refs: TableRefs, window: Window, limit: int
) -> str:
  """Builds the SQL query for session-level rollups over the raw table.

  All four filters apply, but Agent / User / Event type only decide
  *which* sessions are listed: each gets its own LOGICAL_OR in the
  HAVING, so a session is kept when it contains a match for each filter
  somewhere in the window. Applying them in the WHERE would drop events
  before the GROUP BY, and every rollup column would then describe a
  filtered slice while still being labelled as the session.

  The consequence is that these columns cover every event the session has
  in the window, including events the filters excluded, so they will not
  sum to the Overview stats.

  Args:
    refs: Validated BigQuery table references.
    window: Snapped time window.
    limit: Maximum number of recent sessions to return.

  Returns:
    The SQL query string.
  """
  err = _ERROR_PREDICATE.format(p="")
  return f"""
SELECT
  session_id,
  STRING_AGG(DISTINCT user_id, ', ' ORDER BY user_id)
    AS session_users_in_window,
  MIN(timestamp) AS started_in_window_at,
  MAX(timestamp) AS last_event_in_window_at,
  TIMESTAMP_DIFF(MAX(timestamp), MIN(timestamp), SECOND)
    AS duration_in_window_s,
  COUNT(DISTINCT agent) AS session_agents_in_window,
  COUNT(*) AS session_events_in_window,
  COUNTIF({err}) AS session_errors_in_window,
  IFNULL(SUM(IF(event_type = 'LLM_RESPONSE',
    SAFE_CAST(JSON_VALUE(content, '$.usage.prompt') AS INT64), NULL)), 0)
    AS session_input_tokens_in_window,
  IFNULL(SUM(IF(event_type = 'LLM_RESPONSE',
    SAFE_CAST(JSON_VALUE(content, '$.usage.completion') AS INT64), NULL)), 0)
    AS session_output_tokens_in_window
FROM {refs.events}
WHERE {time_bounds(window)}
  AND session_id IS NOT NULL
  AND ('{ALL_SENTINEL}' IN UNNEST(@session_ids)
    OR session_id IN UNNEST(@session_ids))
GROUP BY session_id
HAVING LOGICAL_OR('{ALL_SENTINEL}' IN UNNEST(@agents)
    OR agent IN UNNEST(@agents))
  AND LOGICAL_OR('{ALL_SENTINEL}' IN UNNEST(@user_ids)
    OR user_id IN UNNEST(@user_ids))
  AND LOGICAL_OR('{ALL_SENTINEL}' IN UNNEST(@event_types)
    OR event_type IN UNNEST(@event_types))
ORDER BY last_event_in_window_at DESC
LIMIT {int(limit)}
""".strip()


def build_trace_detail_sql(refs: TableRefs, window: Window, limit: int) -> str:
  """Builds the SQL query for one session's event timeline, newest first.

  DESC decides *which* rows survive the LIMIT, and that is the part a
  reader cannot recover from: ascending order would return the oldest
  events in the range, so a busy window would show its first few minutes
  and nothing since. Sort the rendered table by timestamp to read it
  chronologically.

  COALESCE(model, model_version): ``model`` is on LLM_REQUEST attributes,
  ``model_version`` on LLM_RESPONSE — one column covers both.

  Args:
    refs: Validated BigQuery table references.
    window: Snapped time window.
    limit: Maximum number of trace events to return.

  Returns:
    The SQL query string.
  """
  return f"""
SELECT
  timestamp,
  session_id,
  event_type,
  agent,
  invocation_id,
  span_id,
  parent_span_id,
  status,
  COALESCE(
    JSON_VALUE(attributes, '$.model'),
    JSON_VALUE(attributes, '$.model_version')
  ) AS model,
  JSON_VALUE(content, '$.tool') AS tool_name,
  SAFE_CAST(JSON_VALUE(latency_ms, '$.total_ms') AS INT64) AS total_ms,
  error_message
FROM {refs.events}
WHERE {time_bounds(window)}
{_scope(event_type=True)}
ORDER BY timestamp DESC
LIMIT {int(limit)}
""".strip()


# ------------------------------------------------------------------ #
# BigQuery execution                                                   #
# ------------------------------------------------------------------ #


def query_parameters(
    sql: str,
    filters: Filters,
) -> list[bigquery.ArrayQueryParameter]:
  """Binds only the filter parameters the SQL actually references.

  Scanning the text keeps the builders free of bookkeeping about which
  filters they honor, and keeps BigQuery from receiving parameters that
  the query never mentions.

  Args:
    sql: The SQL query string to inspect.
    filters: Active filter tuple selections.

  Returns:
    A list of BigQuery query parameters referenced in the SQL text.
  """
  used = set(_PARAM_RE.findall(sql))
  return [
      bigquery.ArrayQueryParameter(name, "STRING", list(values))
      for name, values in filters._asdict().items()
      if name in used
  ]


def job_config(
    sql: str,
    filters: Filters,
    max_bytes: int,
    *,
    dry_run: bool = False,
) -> bigquery.QueryJobConfig:
  """Returns a job config carrying the per-query scan guardrail.

  Args:
    sql: SQL query string.
    filters: Active filter parameters.
    max_bytes: Maximum allowed bytes billed.
    dry_run: Whether to configure the job as a dry run.

  Returns:
    A configured bigquery.QueryJobConfig instance.
  """
  config = bigquery.QueryJobConfig(
      query_parameters=query_parameters(sql, filters),
      use_legacy_sql=False,
      use_query_cache=True,
      dry_run=dry_run,
      labels={"app": "bqaa_streamlit"},
  )
  # A dry run bills nothing, so the cap only belongs on the real job —
  # where it is the guardrail of record, not merely advice the preflight
  # gives. Both are set: the preflight explains, the cap enforces.
  if not dry_run:
    config.maximum_bytes_billed = int(max_bytes)
  return config


@st.cache_resource(show_spinner=False)
def get_client(project: str) -> bigquery.Client:
  """Returns a BigQuery client per project, reused across reruns.

  Args:
    project: BigQuery project ID.

  Returns:
    A cached bigquery.Client instance.
  """
  return bigquery.Client(project=project)


def _explain(exc: Exception) -> str:
  """Turns the BigQuery errors operators actually hit into actionable advice.

  Args:
    exc: The exception caught during query execution.

  Returns:
    A user-friendly explanation string with remediation advice.
  """
  message = getattr(exc, "message", None) or str(exc)
  if isinstance(exc, gauth_exc.DefaultCredentialsError):
    return (
        f"{message}\n\nRun `gcloud auth application-default login` or set"
        " `GOOGLE_APPLICATION_CREDENTIALS`."
    )
  if isinstance(exc, gexc.NotFound):
    return (
        f"{message}\n\nIf this names a prefixed view, create the typed"
        " views first — `bq-agent-sdk views create-all --project-id P"
        " --dataset-id D --table-id T` — or correct the view prefix in"
        " the sidebar."
    )
  if isinstance(exc, gexc.Forbidden):
    return (
        f"{message}\n\nThe caller needs `roles/bigquery.jobUser` on the"
        " project and `roles/bigquery.dataViewer` on the dataset."
    )
  if "bytesBilledLimitExceeded" in message:
    return (
        f"{message}\n\nRaise the per-query scan cap in the sidebar or"
        " narrow the time range."
    )
  return message


_MAX_SEEN_RUN_IDS = 10_000
_NEXT_RUN_ID = 0
_SEEN_RUN_IDS: set[int] = set()
_RUN_ID_LOCK = threading.Lock()


@st.cache_data(ttl=CACHE_TTL_SECONDS, show_spinner=False, max_entries=256)
def _run_query_cached(
    sql: str,
    filters: Filters,
    project: str,
    max_bytes: int,
) -> tuple[pd.DataFrame, int, bool, int]:
  """Runs the BigQuery job, cached by Streamlit across reruns.

  Args:
    sql: The SQL query to execute.
    filters: Active filter parameters.
    project: BigQuery project ID.
    max_bytes: Maximum allowed bytes billed.

  Returns:
    Tuple of (dataframe, bytes_processed, cache_hit, run_id).
  """
  global _NEXT_RUN_ID
  with _RUN_ID_LOCK:
    _NEXT_RUN_ID += 1
    run_id = _NEXT_RUN_ID

  try:
    client = get_client(project)
    probe = client.query(
        sql,
        job_config=job_config(
            sql,
            filters,
            max_bytes,
            dry_run=True,
        ),
    )
    estimate = int(probe.total_bytes_processed or 0)
  except (gexc.GoogleAPICallError, gauth_exc.DefaultCredentialsError) as exc:
    raise RuntimeError(_explain(exc)) from exc

  if estimate > max_bytes:
    raise RuntimeError(
        f"Guardrail: this query would scan {humanize_bytes(estimate)},"
        f" above the {humanize_bytes(max_bytes)} per-query cap. Narrow"
        " the time range or raise the cap in the sidebar."
    )

  try:
    job = client.query(
        sql,
        job_config=job_config(sql, filters, max_bytes),
    )
    df = job.to_dataframe()
  except (gexc.GoogleAPICallError, gauth_exc.DefaultCredentialsError) as exc:
    raise RuntimeError(_explain(exc)) from exc
  except (
      gexc.RetryError,
      ValueError,
      TypeError,
      RuntimeError,
      OSError,
  ) as exc:  # pragma: no cover - driver/dependency errors.
    raise RuntimeError(str(exc)) from exc

  bytes_billed = 0 if job.cache_hit else int(job.total_bytes_billed or 0)
  bytes_processed = int(job.total_bytes_processed or 0)
  return (
      df,
      bytes_processed,
      bytes_billed,
      bool(job.cache_hit),
      run_id,
  )


def run_query(
    sql: str,
    filters: Filters,
    project: str,
    max_bytes: int,
) -> QueryResult:
  """Runs one query behind a dry-run preflight and the byte cap.

  The dry run is free and tells us the scan size before anything is
  billed, so a query that would blow the cap is reported as a readable
  guardrail message instead of an opaque ``bytesBilledLimitExceeded``.

  Args:
    sql: The SQL query to execute.
    filters: Active filter parameters.
    project: BigQuery project ID.
    max_bytes: Maximum allowed bytes billed.

  Returns:
    QueryResult containing the resulting dataframe and execution metadata.
  """
  try:
    raw = _run_query_cached(
        sql, filters, project, max_bytes
    )
    if len(raw) == 5:
      df, bytes_processed, bytes_billed, cache_hit, run_id = raw
    else:
      df, bytes_processed, cache_hit, run_id = raw
      bytes_billed = 0 if cache_hit else bytes_processed
  except Exception as exc:
    return QueryResult(
        df=pd.DataFrame(),
        error=str(exc),
        bytes_processed=0,
        bytes_billed=0,
        cache_hit=False,
    )

  with _RUN_ID_LOCK:
    if run_id in _SEEN_RUN_IDS:
      return QueryResult(df, None, 0, 0, True)
    if len(_SEEN_RUN_IDS) >= _MAX_SEEN_RUN_IDS:
      evict_count = max(1, _MAX_SEEN_RUN_IDS // 4)
      for _ in range(evict_count):
        _SEEN_RUN_IDS.pop()
    _SEEN_RUN_IDS.add(run_id)

  return QueryResult(df, None, bytes_processed, bytes_billed, cache_hit)


def fetch(sql: str, ctx: Context, label: str) -> QueryResult:
  """Runs a query and records its cost in this rerun's scan log.

  Args:
    sql: The SQL query to execute.
    ctx: Active execution context.
    label: Human-readable label for scan logging and error reporting.

  Returns:
    QueryResult of the execution.
  """
  result = run_query(
      sql,
      ctx.filters,
      ctx.refs.project,
      ctx.max_bytes,
  )
  ctx.scan_log.append(
      (label, result.bytes_billed, result.bytes_processed, result.cache_hit)
  )
  if result.error:
    st.error(f"**{label}** — {result.error}")
  return result


def load_filter_options(
    ctx: Context,
) -> tuple[dict[str, list[str]], QueryResult]:
  """Fetches every dropdown's options in one query.

  Args:
    ctx: Active execution context.

  Returns:
    A tuple of:
      - Dictionary mapping filter kind ('agent', 'user_id', 'event_type',
        'session_id') to a list of available string values.
      - QueryResult containing the execution outcome and any error.
  """
  sql = build_filter_options_sql(ctx.refs, ctx.window)
  result = fetch(sql, ctx, "Filter options")
  options: dict[str, list[str]] = {}
  if result.df.empty:
    return options, result
  for kind, group in result.df.groupby("kind"):
    options[str(kind)] = [str(v) for v in group["value"].tolist()]
  return options, result