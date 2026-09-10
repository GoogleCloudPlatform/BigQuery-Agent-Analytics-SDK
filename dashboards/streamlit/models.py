"""Data models, configuration, and types for the Streamlit dashboard."""

from __future__ import annotations

from collections.abc import Sequence
import dataclasses
import datetime as dt
import re
from typing import Any, NamedTuple

import pandas as pd

# ------------------------------------------------------------------ #
# Constants                                                            #
# ------------------------------------------------------------------ #

# Sentinel that means "no filter applied", matching dashboards/grafana/queries. The
# clause `('___ALL___' IN UNNEST(@agents) OR agent IN UNNEST(@agents))`
# is injection-safe and cannot crash on an empty array the way an
# `IN ()` list would.
ALL_SENTINEL = "___ALL___"

DEFAULT_TABLE_ID = "agent_events"
DEFAULT_VIEW_PREFIX = "adk_"

OTHER_LABEL = "Other"

# Identifier grammars. Neither pattern admits a backtick, so a validated
# identifier is safe to interpolate into a backtick-quoted table path.
_PROJECT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9\-_.:]{0,62}$")
_NAME_RE = re.compile(r"^[A-Za-z0-9_]{1,1024}$")
_PREFIX_RE = re.compile(r"^[A-Za-z0-9_]{0,1024}$")

TIME_RANGES: dict[str, dt.timedelta] = {
    "Last 1 hour": dt.timedelta(hours=1),
    "Last 6 hours": dt.timedelta(hours=6),
    "Last 24 hours": dt.timedelta(hours=24),
    "Last 3 days": dt.timedelta(days=3),
    "Last 7 days": dt.timedelta(days=7),
    "Last 30 days": dt.timedelta(days=30),
}

# Per-query scan caps offered in the sidebar. The selected value is set as
# `maximum_bytes_billed` on every job, so BigQuery refuses a query that
# would exceed it rather than billing for it.
BYTES_CAPS: dict[str, int] = {
    "100 MB": 100 * 1024**2,
    "1 GB": 1024**3,
    "10 GB": 10 * 1024**3,
    "100 GB": 100 * 1024**3,
    "1 TB": 1024**4,
}
DEFAULT_BYTES_CAP = "1 GB"

# Results are cached for this long, and the window's upper bound is
# snapped to the same interval. Without the snap, "Last 24 hours" would
# produce a new `end` timestamp on every rerun, so the SQL string — and
# therefore the cache key — would never repeat and every widget change
# would re-bill every panel.
CACHE_TTL_SECONDS = 300

# Rows returned by the detail tables, matching the Grafana caps.
RECENT_SESSIONS_LIMIT = 250
TRACE_DETAIL_LIMIT = 500
TOOL_ERRORS_LIMIT = 100
FILTER_OPTIONS_LIMIT = 1000
TOP_ERRORS_LIMIT = 50


# ------------------------------------------------------------------ #
# Theme                                                                #
# ------------------------------------------------------------------ #


@dataclasses.dataclass(frozen=True)
class Theme:
  """Chart palette for one appearance mode.

  Attributes:
    surface: Background surface hex color.
    text_primary: Primary text hex color.
    text_secondary: Secondary text hex color.
    muted: Muted text or element hex color.
    grid: Grid line hex color.
    axis: Axis line hex color.
    categorical: Categorical color palette sequence.
  """

  surface: str
  text_primary: str
  text_secondary: str
  muted: str
  grid: str
  axis: str
  categorical: tuple[str, ...]


# Both orderings are validated for the adjacent-pair gates (stacked bars,
# grouped bars, lines) against their own surface: worst adjacent CVD
# delta-E 9.1 light / 8.4 dark, worst normal-vision 19.6 / 19.3.
LIGHT_THEME = Theme(
    surface="#fcfcfb",
    text_primary="#0b0b0b",
    text_secondary="#52514e",
    muted="#898781",
    grid="#e1e0d9",
    axis="#c3c2b7",
    categorical=(
        "#2a78d6",
        "#eb6834",
        "#1baf7a",
        "#eda100",
        "#e87ba4",
        "#008300",
        "#4a3aa7",
        "#e34948",
    ),
)

DARK_THEME = Theme(
    surface="#1a1a19",
    text_primary="#ffffff",
    text_secondary="#c3c2b7",
    muted="#898781",
    grid="#2c2c2a",
    axis="#383835",
    categorical=(
        "#3987e5",
        "#d95926",
        "#199e70",
        "#c98500",
        "#d55181",
        "#008300",
        "#9085e9",
        "#e66767",
    ),
)


# ------------------------------------------------------------------ #
# Configuration & Context Models                                       #
# ------------------------------------------------------------------ #


@dataclasses.dataclass(frozen=True)
class TableRefs:
  """Validated BigQuery identifiers for the raw table and typed views.

  Attributes:
    project: BigQuery project ID.
    dataset: BigQuery dataset ID.
    table: Events table ID.
    view_prefix: Prefix applied to typed views.
  """

  project: str
  dataset: str
  table: str
  view_prefix: str = DEFAULT_VIEW_PREFIX

  @property
  def events(self) -> str:
    """Returns the backtick-quoted full table path for the raw events table."""
    return f"`{self.project}.{self.dataset}.{self.table}`"

  def view(self, suffix: str) -> str:
    """Returns the backtick-quoted full table path for a typed view.

    Args:
      suffix: Suffix of the typed view (e.g. 'llm_responses').

    Returns:
      The backtick-quoted view path.
    """
    return f"`{self.project}.{self.dataset}.{self.view_prefix}{suffix}`"


def validate_refs(
    project: str, dataset: str, table: str, view_prefix: str
) -> tuple[TableRefs | None, list[str]]:
  """Validates identifiers before they reach a SQL string.

  Returns the refs and an empty error list, or ``None`` and the reasons.
  BigQuery cannot parameterize a table path, so this is the boundary that
  keeps user-supplied text out of the FROM clause.

  Args:
    project: BigQuery project ID.
    dataset: BigQuery dataset ID.
    table: Events table ID.
    view_prefix: Prefix applied to typed views.

  Returns:
    A tuple containing the validated TableRefs instance (or None if validation
    failed) and a list of error description strings.
  """
  errors: list[str] = []
  if not _PROJECT_RE.match(project or ""):
    errors.append(f"Invalid project ID: {project!r}")
  if not _NAME_RE.match(dataset or ""):
    errors.append(f"Invalid dataset ID: {dataset!r}")
  if not _NAME_RE.match(table or ""):
    errors.append(f"Invalid table ID: {table!r}")
  if not _PREFIX_RE.match(view_prefix or ""):
    errors.append(f"Invalid view prefix: {view_prefix!r}")
  if errors:
    return None, errors
  return TableRefs(project, dataset, table, view_prefix), []


class Filters(NamedTuple):
  """Filter selections, as tuples so the whole value is hashable.

  Being hashable is what lets ``@st.cache_data`` key on the filters: they
  live in query parameters rather than in the SQL text, so the SQL string
  alone would be an incomplete cache key.

  Attributes:
    agents: Selected agent names, or (ALL_SENTINEL,).
    user_ids: Selected user IDs, or (ALL_SENTINEL,).
    event_types: Selected event types, or (ALL_SENTINEL,).
    session_ids: Selected session IDs, or (ALL_SENTINEL,).
  """

  agents: tuple[str, ...] = (ALL_SENTINEL,)
  user_ids: tuple[str, ...] = (ALL_SENTINEL,)
  event_types: tuple[str, ...] = (ALL_SENTINEL,)
  session_ids: tuple[str, ...] = (ALL_SENTINEL,)


def as_filter_values(selected: Sequence[str]) -> tuple[str, ...]:
  """Converts a sequence of selected filter items into a hashable tuple.

  An empty selection means "everything" and maps to (ALL_SENTINEL,).

  Args:
    selected: Selected filter values from the UI.

  Returns:
    A tuple of filter values, or (ALL_SENTINEL,) if empty.
  """
  return tuple(selected) if selected else (ALL_SENTINEL,)


@dataclasses.dataclass(frozen=True)
class Window:
  """Half-open ``[start, end)`` query window, snapped for cacheability.

  Attributes:
    start: Beginning timestamp of the window (inclusive, UTC).
    end: End timestamp of the window (exclusive, UTC).
  """

  start: dt.datetime
  end: dt.datetime

  @property
  def span(self) -> dt.timedelta:
    """Returns the total duration of the window."""
    return self.end - self.start

  @property
  def bucket(self) -> str:
    """Returns the ``TIMESTAMP_TRUNC`` unit that keeps a chart readable."""
    if self.span <= dt.timedelta(hours=6):
      return "MINUTE"
    if self.span <= dt.timedelta(days=3):
      return "HOUR"
    return "DAY"


def snap(moment: dt.datetime, seconds: int = CACHE_TTL_SECONDS) -> dt.datetime:
  """Floors ``moment`` to a multiple of ``seconds`` past the hour.

  Args:
    moment: The datetime object to snap.
    seconds: Interval in seconds to floor by (defaults to CACHE_TTL_SECONDS).

  Returns:
    The snapped UTC datetime without microseconds.
  """
  floored = moment.replace(microsecond=0)
  drop = (floored.minute * 60 + floored.second) % seconds
  return floored - dt.timedelta(seconds=drop)


def make_window(span: dt.timedelta, now: dt.datetime | None = None) -> Window:
  """Builds a snapped query window of the requested span ending at ``now``.

  Args:
    span: Duration of the query window.
    now: Optional timestamp to base the end time on. Defaults to current UTC.

  Returns:
    A Window instance with snapped end and start boundaries.
  """
  end = snap(now or dt.datetime.now(dt.timezone.utc))
  return Window(start=end - span, end=end)


def humanize_bytes(num: float) -> str:
  """Formats a byte count into a human-readable string (B, KB, MB, GB, TB).

  Args:
    num: Byte count to format.

  Returns:
    Formatted string representation.
  """
  for unit in ("B", "KB", "MB", "GB"):
    if abs(num) < 1024:
      return f"{num:,.1f} {unit}" if unit != "B" else f"{int(num)} B"
    num /= 1024
  return f"{num:,.1f} TB"


class QueryResult(NamedTuple):
  """A dataframe plus the outcome metadata the UI needs to explain it.

  Attributes:
    df: Result DataFrame containing query rows.
    error: Error message if query failed, or None.
    bytes_processed: Number of bytes processed by the query job.
    bytes_billed: Number of bytes billed by the query job.
    cache_hit: Whether the query result was served from BigQuery cache.
  """

  df: pd.DataFrame
  error: str | None = None
  bytes_processed: int = 0
  bytes_billed: int = 0
  cache_hit: bool = False


@dataclasses.dataclass
class Context:
  """Everything a panel needs to run and price a query.

  Attributes:
    refs: Validated BigQuery table and view references.
    window: Snapped time window for the query.
    filters: Active filter parameter values.
    max_bytes: Maximum billed bytes cap per query.
    theme: Active color theme.
    price_in: Price in USD per 1M input tokens.
    price_out: Price in USD per 1M output tokens.
    scan_log: Log of query executions, bytes billed/processed, and cache hit status.
  """

  refs: TableRefs
  window: Window
  filters: Filters
  max_bytes: int
  theme: Theme
  price_in: float
  price_out: float
  scan_log: list[tuple[Any, ...]] = dataclasses.field(default_factory=list)
