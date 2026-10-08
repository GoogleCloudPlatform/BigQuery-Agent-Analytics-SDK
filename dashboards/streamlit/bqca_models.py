"""Data models and constants for the BQCA Prompt & Response Logging dashboard.

BQCA is Conversational Analytics (Macchiato) prompt and response logging: a
BigQuery table written with the BigQuery Agent Analytics schema, restricted to
nine public event types. This module holds everything the BQCA surface shares
that is not SQL or drawing code:

* the event-type allowlist and the fast-path vocabulary,
* the time-window presets and how they resolve to a ``models.Window``,
* ``BqcaFilterState``: every sidebar selection in one frozen, hashable value,
* ``BqcaKpiSummary`` / ``BqcaTurnRow``: typed views over query result rows,
* ``inert_markdown``: how customer-logged text is made safe to render as
  Markdown.

Everything here is pure (no Streamlit, no BigQuery) so it is trivially
testable.
"""

from __future__ import annotations

from collections.abc import Mapping
import dataclasses
import datetime as dt
from pathlib import Path
import re
import sys
from typing import Any

import pandas as pd

_DASHBOARD_DIR = str(Path(__file__).resolve().parent)
if _DASHBOARD_DIR not in sys.path:
  sys.path.insert(0, _DASHBOARD_DIR)

from models import make_window
from models import Window

# ------------------------------------------------------------------ #
# Constants                                                            #
# ------------------------------------------------------------------ #

# BQCA tables are not typed-view backed; the default table name is the one
# the Looker Studio BQCA profile uses, so both surfaces agree out of the box.
BQCA_DEFAULT_TABLE_ID = "bqca_prompt_response_logs"

# The nine public event types a Conversational Analytics data agent logs.
# Every BQCA query filters ``event_type IN UNNEST(@allowed_event_types)``
# bound from this tuple, so no other event type can reach a BQCA panel — not
# through a filter, not through a typo. Do not add to it without updating
# the BQCA governance contract.
BQCA_ALLOWED_EVENT_TYPES: tuple[str, ...] = (
    "INVOCATION_STARTING",
    "USER_MESSAGE_RECEIVED",
    "AGENT_RESPONSE",
    "INVOCATION_COMPLETED",
    "LLM_RESPONSE",
    "EMBEDDING_SUGGESTION",
    "INVOCATION_ERROR",
    "AGENT_ERROR",
    "LLM_ERROR",
)

# Fast-path turns answer from a saved query: no LLM call, no tokens. The
# plugin tags every event of such a turn with ``attributes.fast_path``.
FAST_PATH_ALL = "All"
FAST_PATH_ONLY = "Fast Path Only"
STANDARD_ONLY = "Standard NL2SQL Only"
FAST_PATH_MODES: tuple[str, ...] = (
    FAST_PATH_ALL,
    FAST_PATH_ONLY,
    STANDARD_ONLY,
)

FAST_PATH_LABEL = "fast_path"
STANDARD_LABEL = "standard_nl2sql"
FAST_PATH_LABELS: tuple[str, ...] = (FAST_PATH_LABEL, STANDARD_LABEL)

_FAST_PATH_MODE_LABELS: dict[str, tuple[str, ...]] = {
    FAST_PATH_ALL: FAST_PATH_LABELS,
    FAST_PATH_ONLY: (FAST_PATH_LABEL,),
    STANDARD_ONLY: (STANDARD_LABEL,),
}

CUSTOM_WINDOW = "custom"
DEFAULT_TIME_WINDOW = "7d"

# token -> (sidebar label, span). ``custom`` has no span: its bounds travel
# in ``BqcaFilterState.custom_start`` / ``custom_end``.
BQCA_TIME_WINDOWS: dict[str, tuple[str, dt.timedelta | None]] = {
    "1h": ("Last 1 hour", dt.timedelta(hours=1)),
    "6h": ("Last 6 hours", dt.timedelta(hours=6)),
    "24h": ("Last 24 hours", dt.timedelta(hours=24)),
    "3d": ("Last 3 days", dt.timedelta(days=3)),
    "7d": ("Last 7 days", dt.timedelta(days=7)),
    "30d": ("Last 30 days", dt.timedelta(days=30)),
    CUSTOM_WINDOW: ("Custom range", None),
}

# Rows returned by the two detail tables.
TURN_EXPLORER_LIMIT = 100
ERROR_ATTRIBUTION_LIMIT = 100
BREAKDOWN_LIMIT = 100
TIMELINE_LIMIT = 200

# ------------------------------------------------------------------ #
# Filter state                                                         #
# ------------------------------------------------------------------ #


@dataclasses.dataclass(frozen=True)
class BqcaFilterState:
  """Everything the BQCA surface filters and queries on, as one value.

  Frozen and made of hashables, so it can sit in ``st.session_state`` and be
  compared across reruns. Empty tuples mean "no restriction".

  Attributes:
    project_id: BigQuery project that holds the logs.
    dataset_id: BigQuery dataset that holds the logs.
    table_id: BQCA events table.
    time_window: A key of ``BQCA_TIME_WINDOWS`` (``"7d"``, ``"custom"``...).
    custom_start: Inclusive start of a ``"custom"`` window (naive = UTC).
    custom_end: Exclusive end of a ``"custom"`` window (naive = UTC).
    data_agent_ids: Selected data agents.
    personas: Selected personas.
    event_types: Selected event types. Honored only by the panels that list
      individual events; anything outside the allowlist is dropped.
    fast_path_mode: One of ``FAST_PATH_MODES``.
    session_search: Case-insensitive substring of a session or conversation
      id.
    prompt_search: Case-insensitive substring of a turn's user prompt.
    errors_only: Keep only turns that carry at least one error event.
  """

  project_id: str
  dataset_id: str
  table_id: str = BQCA_DEFAULT_TABLE_ID
  time_window: str = DEFAULT_TIME_WINDOW
  custom_start: dt.datetime | None = None
  custom_end: dt.datetime | None = None
  data_agent_ids: tuple[str, ...] = ()
  personas: tuple[str, ...] = ()
  event_types: tuple[str, ...] = ()
  fast_path_mode: str = FAST_PATH_ALL
  session_search: str = ""
  prompt_search: str = ""
  errors_only: bool = False

  def fast_path_labels(self) -> tuple[str, ...]:
    """Returns the ``fast_path_label`` values the fast-path mode keeps.

    Returns:
      ``("fast_path", "standard_nl2sql")`` for "All", or the single label the
      mode selects.

    Raises:
      ValueError: If ``fast_path_mode`` is not one of ``FAST_PATH_MODES``.
    """
    try:
      return _FAST_PATH_MODE_LABELS[self.fast_path_mode]
    except KeyError:
      raise ValueError(
          f"Unknown fast-path mode {self.fast_path_mode!r}; expected one of"
          f" {list(FAST_PATH_MODES)}."
      ) from None

  def selected_event_types(self) -> tuple[str, ...]:
    """Returns the selected event types that are on the allowlist.

    The selection is the only free-form event-type input the BQCA surface
    has, so it is intersected with the allowlist here, in one place, before
    it can be bound to a query. Order and duplicates are normalized.

    Returns:
      The allowlisted event types in canonical order, or ``()`` for no
      restriction.
    """
    chosen = set(self.event_types)
    return tuple(e for e in BQCA_ALLOWED_EVENT_TYPES if e in chosen)

  def window(self, now: dt.datetime | None = None) -> Window:
    """Resolves ``time_window`` to a concrete ``Window``.

    Args:
      now: Optional "current time" for preset windows (tests).

    Returns:
      The query window.
    """
    return resolve_window(self, now)


def _as_utc(moment: dt.datetime) -> dt.datetime:
  """Normalizes a datetime to UTC; naive datetimes are taken as UTC."""
  if moment.tzinfo is None:
    return moment.replace(tzinfo=dt.timezone.utc)
  return moment.astimezone(dt.timezone.utc)


def resolve_window(
    state: BqcaFilterState, now: dt.datetime | None = None
) -> Window:
  """Resolves a filter state's time window to a ``models.Window``.

  Preset windows are snapped like the ADK dashboard's, so repeated reruns
  produce identical SQL and hit the result cache. A custom window is used
  as given: its bounds are day-aligned by the picker and already stable.

  Args:
    state: The filter state to resolve.
    now: Optional "current time" for preset windows (tests).

  Returns:
    The half-open ``[start, end)`` window.

  Raises:
    ValueError: For an unknown preset, or a custom window that is missing a
      bound or whose start is not before its end.
  """
  token = state.time_window
  if token == CUSTOM_WINDOW:
    if state.custom_start is None or state.custom_end is None:
      raise ValueError("A custom time window needs a start and an end.")
    start = _as_utc(state.custom_start)
    end = _as_utc(state.custom_end)
    if start >= end:
      raise ValueError("The custom time window must start before it ends.")
    return Window(start=start, end=end)
  entry = BQCA_TIME_WINDOWS.get(token)
  if entry is None or entry[1] is None:
    raise ValueError(
        f"Unknown time window {token!r}; expected one of"
        f" {list(BQCA_TIME_WINDOWS)}."
    )
  return make_window(entry[1], now)


# ------------------------------------------------------------------ #
# Result rows                                                          #
# ------------------------------------------------------------------ #


def _is_missing(value: Any) -> bool:
  """Returns whether ``value`` is None / NaN / NaT / pd.NA."""
  if value is None:
    return True
  try:
    return bool(pd.isna(value))
  except (TypeError, ValueError):
    # Array-likes: not a scalar "missing" marker.
    return False


def _opt_str(value: Any) -> str | None:
  return None if _is_missing(value) else str(value)


def _opt_int(value: Any) -> int | None:
  return None if _is_missing(value) else int(value)


def _opt_float(value: Any) -> float | None:
  return None if _is_missing(value) else float(value)


def _int(value: Any) -> int:
  return 0 if _is_missing(value) else int(value)


def _float(value: Any) -> float:
  return 0.0 if _is_missing(value) else float(value)


def _bool(value: Any) -> bool:
  return False if _is_missing(value) else bool(value)


def _datetime(value: Any) -> dt.datetime:
  if _is_missing(value):
    raise ValueError("A turn row needs a timestamp.")
  if isinstance(value, pd.Timestamp):
    return value.to_pydatetime()
  if isinstance(value, dt.datetime):
    return value
  return pd.Timestamp(value).to_pydatetime()


@dataclasses.dataclass(frozen=True)
class BqcaKpiSummary:
  """The KPI header (panel P1) as typed values.

  Rates are fractions in ``0.0..1.0``. Latencies and the turn error rate are
  ``None`` when they are undefined (no completed turn for a latency, no turn at
  all for the error rate), so the UI can show a dash rather than a confident
  zero.

  Attributes:
    total_turns: Distinct invocations in scope.
    completed_turns: Distinct invocations that reached INVOCATION_COMPLETED.
    error_events: Events that satisfy the three-condition error predicate.
    turn_error_rate: Share of turns with at least one error event; ``None``
      when no turn is in scope (``0 / 0`` is undefined, not 0%).
    p50_turn_latency_ms: Median INVOCATION_COMPLETED latency.
    p95_turn_latency_ms: 95th-percentile INVOCATION_COMPLETED latency.
    total_tokens: Tokens over LLM_RESPONSE events.
    thoughts_tokens: Thinking tokens over LLM_RESPONSE events.
    cached_tokens: Cached-prompt tokens over LLM_RESPONSE events.
    fast_path_rate: Share of completed turns that used the fast path.
    embedding_coverage: Share of all turns that received at least one
      EMBEDDING_SUGGESTION carrying suggested columns.
  """

  total_turns: int
  completed_turns: int
  error_events: int
  turn_error_rate: float | None
  p50_turn_latency_ms: float | None
  p95_turn_latency_ms: float | None
  total_tokens: int
  thoughts_tokens: int
  cached_tokens: int
  fast_path_rate: float
  embedding_coverage: float

  @property
  def incomplete_turns(self) -> int:
    """Turns that have no INVOCATION_COMPLETED event in scope.

    They count toward the turn totals and the error rate, but not toward the
    latency percentiles or the fast-path rate, which use completed turns only.
    """
    return max(self.total_turns - self.completed_turns, 0)

  @classmethod
  def from_row(cls, row: Mapping[str, Any]) -> BqcaKpiSummary:
    """Builds the summary from one KPI result row.

    Args:
      row: A mapping (or ``pd.Series``) with the column names the KPI query
        returns. Missing or NULL values become zero (``None`` for the
        latencies and the turn error rate).

    Returns:
      The typed summary.
    """
    return cls(
        total_turns=_int(row.get("total_turns")),
        completed_turns=_int(row.get("completed_turns")),
        error_events=_int(row.get("error_events")),
        turn_error_rate=_opt_float(row.get("turn_error_rate")),
        p50_turn_latency_ms=_opt_float(row.get("p50_turn_latency_ms")),
        p95_turn_latency_ms=_opt_float(row.get("p95_turn_latency_ms")),
        total_tokens=_int(row.get("total_tokens")),
        thoughts_tokens=_int(row.get("thoughts_tokens")),
        cached_tokens=_int(row.get("cached_tokens")),
        fast_path_rate=_float(row.get("fast_path_rate")),
        embedding_coverage=_float(row.get("embedding_coverage")),
    )

  @classmethod
  def from_frame(cls, df: pd.DataFrame) -> BqcaKpiSummary | None:
    """Builds the summary from the KPI query's result frame.

    Args:
      df: The KPI query result.

    Returns:
      The summary, or ``None`` when the frame is empty (no data in scope).
    """
    if df.empty:
      return None
    return cls.from_row(df.iloc[0])


@dataclasses.dataclass(frozen=True)
class BqcaTurnRow:
  """One turn of the Prompt, Response & SQL Explorer (panel P8).

  Attributes:
    timestamp: When the turn started.
    invocation_id: The turn's invocation id, whitespace-trimmed. Empty when
      the row carries none; such a turn has no timeline to open.
    session_id: Session the turn belongs to.
    conversation_id: Conversational Analytics conversation id.
    data_agent_id: Data agent that served the turn.
    persona: Resolved persona (never empty; ``"unattributed"`` at worst).
    user_id: User id as logged.
    fast_path: Whether the turn used the fast path.
    status: ``"OK"`` or ``"ERROR"``.
    turn_latency_ms: INVOCATION_COMPLETED latency.
    total_tokens: Tokens over the turn's LLM_RESPONSE events.
    thoughts_tokens: Thinking tokens over the turn's LLM_RESPONSE events.
    user_prompt: The user's prompt.
    agent_response: The answer that was served: the turn's *last*
      AGENT_RESPONSE, multi-part markdown joined.
    extracted_sql: The SQL fence found in that same response.
    error_message: Distinct error messages of the turn, joined.
    agent_response_count: How many AGENT_RESPONSE events the turn logged
      (``None`` when the query did not report it). More than one means the
      earlier ones were superseded by the last.
  """

  timestamp: dt.datetime
  invocation_id: str
  session_id: str | None
  conversation_id: str | None
  data_agent_id: str | None
  persona: str
  user_id: str | None
  fast_path: bool
  status: str
  turn_latency_ms: float | None
  total_tokens: int | None
  thoughts_tokens: int | None
  user_prompt: str | None
  agent_response: str | None
  extracted_sql: str | None
  error_message: str | None
  agent_response_count: int | None = None

  @classmethod
  def from_row(cls, row: Mapping[str, Any]) -> BqcaTurnRow:
    """Builds a turn from one explorer result row.

    Args:
      row: A mapping (or ``pd.Series``) with the explorer query's columns.

    Returns:
      The typed turn. NULLs become ``None`` (``False`` / ``"OK"`` defaults
      for the flag and status).
    """
    return cls(
        timestamp=_datetime(row.get("timestamp")),
        invocation_id=(_opt_str(row.get("invocation_id")) or "").strip(),
        session_id=_opt_str(row.get("session_id")),
        conversation_id=_opt_str(row.get("conversation_id")),
        data_agent_id=_opt_str(row.get("data_agent_id")),
        persona=_opt_str(row.get("persona")) or "unattributed",
        user_id=_opt_str(row.get("user_id")),
        fast_path=_bool(row.get("fast_path")),
        status=_opt_str(row.get("status")) or "OK",
        turn_latency_ms=_opt_float(row.get("turn_latency_ms")),
        total_tokens=_opt_int(row.get("total_tokens")),
        thoughts_tokens=_opt_int(row.get("thoughts_tokens")),
        user_prompt=_opt_str(row.get("user_prompt")),
        agent_response=_opt_str(row.get("agent_response")),
        extracted_sql=_opt_str(row.get("extracted_sql")),
        error_message=_opt_str(row.get("error_message")),
        agent_response_count=_opt_int(row.get("agent_response_count")),
    )

  @classmethod
  def from_frame(cls, df: pd.DataFrame) -> list[BqcaTurnRow]:
    """Builds every turn of an explorer result frame, in frame order.

    Args:
      df: The explorer query result.

    Returns:
      One ``BqcaTurnRow`` per row (empty for an empty frame).
    """
    return [cls.from_row(row) for _, row in df.iterrows()]


# ------------------------------------------------------------------ #
# Rendering customer-logged text                                       #
# ------------------------------------------------------------------ #

# Markdown image syntax: one or more ``!`` directly before ``[``. Matching the
# whole run of bangs leaves no ``!`` behind for a doubled lead-in such as
# ``!![x](u)``.
_MARKDOWN_IMAGE_OPENER = re.compile(r"!+\[")


def inert_markdown(text: str | None) -> str:
  """Makes customer-logged text safe to render as Markdown.

  A logged answer (or an error message that echoes one) is rendered as
  Markdown, and a Markdown image is fetched by the viewer's browser as soon
  as the page draws: ``![x](https://host/path?secret)`` inside a response
  would make every viewer's browser call that host. Rewriting the ``![``
  opener to ``[image: `` turns each image into an ordinary link that is shown
  but never loaded. Everything else, including real links and formatting, is
  left as it was logged.

  Args:
    text: The logged text; ``None`` reads as empty.

  Returns:
    The text with every Markdown image opener neutralized.
  """
  return _MARKDOWN_IMAGE_OPENER.sub(
      "[image: ", "" if text is None else str(text)
  )
