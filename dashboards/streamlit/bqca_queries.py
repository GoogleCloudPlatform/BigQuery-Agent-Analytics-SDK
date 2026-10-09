"""SQL builders and BigQuery execution for the BQCA dashboard.

Everything the BQCA surface asks BigQuery lives here, kept apart from
``queries.py`` on purpose: ``scripts/check_streamlit_queries_sync.py`` pins
every ``build_*_sql`` in ``queries`` to a canonical Grafana query, and BQCA
has no Grafana twin. Builders here are pure ``str`` producers; execution
reuses the ADK dashboard's client, cost guardrail and error vocabulary.

Governance (the BQCA SQL contract):

* Only the nine allowlisted event types are ever read: every query filters
  ``event_type IN UNNEST(@allowed_event_types)``, bound from
  ``bqca_models.BQCA_ALLOWED_EVENT_TYPES``.
* Attribution never keys on the ``agent``, ``user_id`` or ``session_id``
  columns: a row's data agent is
  ``attributes.session_metadata.state."data-agent-id"``. (``user_id`` only
  feeds the email-handle tier of the persona.)
* An error is the three-condition predicate ``IS_ERROR_EXPR``, never
  ``status = 'ERROR'`` alone.
* A turn is a distinct ``invocation_id`` (``INVOCATION_ID_EXPR``): an id padded
  with whitespace is trimmed, an event logged with a *blank* id is a turn of
  its own keyed by its timestamp (the BQCA customer notebook's rule), and only
  an event with no id at all (NULL) belongs to no turn.
* The data-agent, persona and fast-path filters decide at *turn* grain. The
  plugin does not stamp every event of a turn with the same attribution, so
  ``_prelude`` resolves each value across the turn's events before any filter
  looks at it; filtering never keeps a turn's prompt but drops its completion.
* User-controlled values (filters, search text, the selected invocation) are
  BigQuery query parameters, never SQL text. Only identifiers that passed
  ``models.validate_refs``, a ``Window`` the app built, and ``int()``-clamped
  limits are interpolated.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path
import re
import sys
import threading
from typing import Any, NamedTuple

from google.api_core import exceptions as gexc
from google.auth import exceptions as gauth_exc
from google.cloud import bigquery
import pandas as pd
import streamlit as st

_DASHBOARD_DIR = str(Path(__file__).resolve().parent)
if _DASHBOARD_DIR not in sys.path:
  sys.path.insert(0, _DASHBOARD_DIR)

from bqca_models import BQCA_ALLOWED_EVENT_TYPES
from bqca_models import BqcaFilterState
from bqca_models import BREAKDOWN_LIMIT
from bqca_models import ERROR_ATTRIBUTION_LIMIT
from bqca_models import resolve_window
from bqca_models import TIMELINE_LIMIT
from bqca_models import TURN_EXPLORER_LIMIT
from models import ALL_SENTINEL
from models import CACHE_MAX_ENTRIES
from models import CACHE_TTL_SECONDS
from models import Context
from models import FILTER_OPTIONS_LIMIT
from models import humanize_bytes
from models import QueryResult
from models import ScanEntry
from models import validate_refs
from models import Window
import queries

_PARAM_RE = re.compile(r"@([A-Za-z_][A-Za-z0-9_]*)")

# ------------------------------------------------------------------ #
# Canonical extraction expressions                                     #
# ------------------------------------------------------------------ #
# One definition of each BQCA field, shared by every builder, so a panel can
# never disagree with another about what "persona" or "error" means. Each is
# a plain expression over the raw table's columns.

DATA_AGENT_ID_EXPR = (
    "NULLIF(JSON_VALUE(attributes,"
    " '$.session_metadata.state.\"data-agent-id\"'), '')"
)
CONVERSATION_ID_EXPR = (
    "NULLIF(JSON_VALUE(attributes,"
    " '$.session_metadata.state.\"conversation-id\"'), '')"
)
# A turn is a distinct ``invocation_id``. An id padded with whitespace is
# trimmed, so the turn table and the timeline spell it the same way. An event
# logged with a *blank* id (``''`` or only whitespace) is a turn of its own,
# keyed by ``CAST(timestamp AS STRING)``: a non-NULL key that is distinct per
# event timestamp, the rule the BQCA customer notebook uses, so the turn table
# lists it and the timeline opens it by the very same key instead of dropping
# it. Only an event with no id at all (NULL) belongs to no turn: the per-turn
# panels leave it out, and ``_TURN_PARTITION_KEY`` keeps it from merging with
# other NULL-id rows.
INVOCATION_ID_EXPR = (
    "IF(invocation_id IS NULL, NULL,"
    " IFNULL(NULLIF(TRIM(invocation_id), ''), CAST(timestamp AS STRING)))"
)
# ``user_id`` is often an opaque id (a service-account name, a numeric id), and
# the text before an ``@`` in it must not become a persona. Only a real email
# address yields a handle; an optional ``:suffix`` after the address is
# tolerated. Same shape as the Looker Studio BQCA profile. A raw string: the
# backslashes belong to the SQL regular expression.
_EMAIL_HANDLE_PATTERN = (
    r"^([A-Za-z0-9._%+-]+)@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*"
    r"\.[A-Za-z]{2,}(?::|$)"
)
_USER_ID_EMAIL_HANDLE = (
    f"REGEXP_EXTRACT(TRIM(user_id), r'{_EMAIL_HANDLE_PATTERN}')"
)
# A persona has three tiers, in this order of precedence: the persona custom
# label, the email handle of ``user_id``, and the data agent
# (``DATA_AGENT_ID_EXPR``); a turn with none of them is ``'unattributed'``. Each
# tier is read per row here, as NULL when the row does not carry it, and
# ``_prelude`` resolves each one across the whole turn *before* choosing between
# them, so a label logged by a later event still beats an email handle logged by
# an earlier one.
RAW_EXPLICIT_PERSONA_EXPR = (
    "NULLIF(JSON_VALUE(attributes,"
    " '$.session_metadata.state.custom_labels.persona'), '')"
)
RAW_EMAIL_PERSONA_EXPR = f"NULLIF({_USER_ID_EMAIL_HANDLE}, '')"
IS_ERROR_EXPR = (
    "(UPPER(status) = 'ERROR' OR error_message IS NOT NULL"
    " OR ENDS_WITH(event_type, '_ERROR'))"
)
FAST_PATH_EXPR = (
    "IFNULL(LOWER(JSON_VALUE(attributes, '$.fast_path')) = 'true', FALSE)"
)
MODEL_NAME_EXPR = (
    "COALESCE(NULLIF(JSON_VALUE(attributes, '$.model'), ''),"
    " NULLIF(JSON_VALUE(attributes, '$.model_version'), ''))"
)
MODEL_VERSION_EXPR = "NULLIF(JSON_VALUE(attributes, '$.model_version'), '')"
# Token counts live in ``attributes.usage_metadata`` (the model's own key
# names), with the shorter ``*_tokens`` spellings and ``content.usage`` as
# fallbacks. Thinking and cached tokens only exist under their
# ``*_token_count`` key. The ``latency_ms`` column holds timings, not tokens.
INPUT_TOKENS_EXPR = (
    "COALESCE("
    "SAFE_CAST(JSON_VALUE(attributes,"
    " '$.usage_metadata.prompt_token_count') AS INT64),"
    " SAFE_CAST(JSON_VALUE(attributes,"
    " '$.usage_metadata.prompt_tokens') AS INT64),"
    " SAFE_CAST(JSON_VALUE(content, '$.usage.prompt') AS INT64))"
)
OUTPUT_TOKENS_EXPR = (
    "COALESCE("
    "SAFE_CAST(JSON_VALUE(attributes,"
    " '$.usage_metadata.candidates_token_count') AS INT64),"
    " SAFE_CAST(JSON_VALUE(attributes,"
    " '$.usage_metadata.completion_tokens') AS INT64),"
    " SAFE_CAST(JSON_VALUE(content, '$.usage.completion') AS INT64))"
)
THOUGHTS_TOKENS_EXPR = (
    "SAFE_CAST(JSON_VALUE(attributes,"
    " '$.usage_metadata.thoughts_token_count') AS INT64)"
)
CACHED_TOKENS_EXPR = (
    "SAFE_CAST(JSON_VALUE(attributes,"
    " '$.usage_metadata.cached_content_token_count') AS INT64)"
)
TOTAL_TOKENS_EXPR = (
    "COALESCE("
    "SAFE_CAST(JSON_VALUE(attributes,"
    " '$.usage_metadata.total_token_count') AS INT64),"
    " SAFE_CAST(JSON_VALUE(attributes,"
    " '$.usage_metadata.total_tokens') AS INT64),"
    " SAFE_CAST(JSON_VALUE(content, '$.usage.total') AS INT64))"
)
TOTAL_LATENCY_MS_EXPR = (
    "SAFE_CAST(JSON_VALUE(latency_ms, '$.total_ms') AS FLOAT64)"
)
TFFT_MS_EXPR = (
    "SAFE_CAST(JSON_VALUE(latency_ms, '$.time_to_first_token_ms') AS FLOAT64)"
)
# ``\\n`` below is the two characters backslash-n in Python and therefore a
# newline escape inside the SQL string literal. The text parts are joined
# before ``$.parts[0].text`` is tried alone: reading the first part first would
# cut a multi-part prompt down to part 1 in the explorer and in the prompt
# search.
USER_PROMPT_TEXT_EXPR = (
    "COALESCE("
    "NULLIF(JSON_VALUE(content, '$.text_summary'), ''),"
    " NULLIF(JSON_VALUE(content, '$.prompt'), ''),"
    " NULLIF(ARRAY_TO_STRING(ARRAY(SELECT JSON_VALUE(p, '$.text')"
    " FROM UNNEST(JSON_QUERY_ARRAY(content, '$.parts')) AS p"
    " WITH OFFSET AS o WHERE JSON_VALUE(p, '$.text') IS NOT NULL"
    " ORDER BY o), '\\n'), ''),"
    " NULLIF(JSON_VALUE(content, '$.parts[0].text'), ''))"
)
# A multi-part answer is the markdown of every ``response.parts`` entry, in
# order, separated by a blank line. ``text_summary`` wins when present.
_RESPONSE_MARKDOWN_EXPR = (
    "NULLIF(ARRAY_TO_STRING(ARRAY(SELECT JSON_VALUE(part, '$.markdown')"
    " FROM UNNEST(JSON_QUERY_ARRAY(content, '$.response.parts')) AS part"
    " WITH OFFSET AS part_offset"
    " WHERE JSON_VALUE(part, '$.markdown') IS NOT NULL"
    " ORDER BY part_offset), '\\n\\n'), '')"
)
AGENT_RESPONSE_TEXT_EXPR = (
    "COALESCE("
    "NULLIF(JSON_VALUE(content, '$.text_summary'), ''),"
    f" {_RESPONSE_MARKDOWN_EXPR},"
    " NULLIF(TRIM(JSON_VALUE(content, '$.response.clarifying_question')), ''),"
    " NULLIF(JSON_VALUE(content, '$.parts[0].text'), ''),"
    " JSON_VALUE(content, '$.response'))"
)
EXTRACTED_SQL_EXPR = (
    "REGEXP_EXTRACT(COALESCE("
    "NULLIF(JSON_VALUE(content, '$.text_summary'), ''),"
    f" {_RESPONSE_MARKDOWN_EXPR},"
    " ''),"
    " r'(?is)```sql\\s*(.*?)\\s*```')"
)
# The logging plugin writes one EMBEDDING_SUGGESTION event per suggestion with
# ``{"reason": ..., "suggested_columns": [...]}`` as its content and omits an
# empty list, so the suggestion's size is the length of ``suggested_columns``.
# The match-count paths are legacy fallbacks; ``COALESCE`` ends in 0 so an event
# with no columns at all counts as a suggestion of zero columns.
SUGGESTED_COLUMNS_COUNT_EXPR = (
    "COALESCE("
    "ARRAY_LENGTH(JSON_QUERY_ARRAY(content, '$.suggested_columns')),"
    " SAFE_CAST(JSON_VALUE(content, '$.similar_queries_count') AS INT64),"
    " ARRAY_LENGTH(JSON_QUERY_ARRAY(content, '$.suggestions')),"
    " 0)"
)
# The pre-fix name, kept as an alias for any caller that still imports it.
SIMILAR_QUERIES_COUNT_EXPR = SUGGESTED_COLUMNS_COUNT_EXPR
EMBEDDING_REASON_EXPR = (
    "COALESCE(NULLIF(JSON_VALUE(content, '$.reason'), ''), '(unspecified)')"
)

# Raw columns every panel needs, and the canonical dimensions every panel
# filters on. Derived here once so the filters apply identically everywhere.
# ``invocation_id`` is not a raw column here: it is the turn key and always
# goes through ``INVOCATION_ID_EXPR``. ``span_id`` is only read to tell apart
# the events that belong to no turn (see ``_TURN_PARTITION_KEY``).
_BASE_RAW: tuple[str, ...] = (
    "timestamp",
    "event_type",
    "session_id",
    "span_id",
)
# Facts that belong to one row, however many rows the turn has.
_ROW_DIMENSIONS: tuple[tuple[str, str], ...] = (
    ("conversation_id", CONVERSATION_ID_EXPR),
    ("is_error", IS_ERROR_EXPR),
)
# Facts that belong to a *turn*, but that the plugin may not stamp on every one
# of the turn's events. They are read per row as ``raw_<name>`` and resolved
# across the turn by ``_prelude``; the panels and the filters only ever see the
# resolved ``<name>`` columns. The persona is read as its separate tiers
# (explicit label, email handle) plus the data agent, so each can be resolved
# across the turn before the tiers are chosen between.
_TURN_DIMENSIONS: tuple[tuple[str, str], ...] = (
    ("raw_data_agent_id", DATA_AGENT_ID_EXPR),
    ("raw_explicit_persona", RAW_EXPLICIT_PERSONA_EXPR),
    ("raw_email_persona", RAW_EMAIL_PERSONA_EXPR),
    ("raw_fast_path", FAST_PATH_EXPR),
)
# What ``_prelude`` resolves per turn from the tiers above, as ``(resolved
# alias, raw column)`` pairs, before it decides the turn's ``data_agent_id`` and
# ``persona``.
_FIRST_TIERS: tuple[tuple[str, str], ...] = (
    ("first_data_agent_id", "raw_data_agent_id"),
    ("first_explicit_persona", "raw_explicit_persona"),
    ("first_email_persona", "raw_email_persona"),
)
# The partition every turn-resolving window runs over, evaluated on
# ``events_raw`` where ``invocation_id`` is already ``INVOCATION_ID_EXPR``: the
# turn key, except that an event with no ``invocation_id`` at all (NULL) gets a
# key of its own. Left alone, every such event would fall into one shared NULL
# partition and inherit the attribution or the error verdict of unrelated
# events; with its own key it is judged alone, and the per-turn panels still
# leave it out because its ``invocation_id`` stays NULL.
_TURN_PARTITION_KEY = (
    "IF(invocation_id IS NULL,"
    " CONCAT('__null_inv_', CAST(timestamp AS STRING), '_',"
    " IFNULL(span_id, ''), '_', event_type),"
    " invocation_id)"
)

_MAX_LIMIT = 1000


# ------------------------------------------------------------------ #
# SQL builders (pure)                                                  #
# ------------------------------------------------------------------ #


def _validate_ident(project_id: str, dataset_id: str, table_id: str) -> str:
  """Validates the table identifiers and renders the backtick table path.

  BigQuery cannot parameterize a table path, so this is the one boundary that
  keeps user-supplied text out of a ``FROM`` clause.

  Args:
    project_id: BigQuery project ID.
    dataset_id: BigQuery dataset ID.
    table_id: BQCA events table ID.

  Returns:
    The backtick-quoted ``project.dataset.table`` path.

  Raises:
    ValueError: If any identifier is malformed.
  """
  refs, errors = validate_refs(project_id, dataset_id, table_id, "")
  if refs is None:
    raise ValueError("; ".join(errors))
  return refs.events


def _limit(value: int) -> int:
  """Clamps a row limit to a sane positive integer, rejecting garbage."""
  number = int(value)
  if not 1 <= number <= _MAX_LIMIT:
    raise ValueError(f"limit must be between 1 and {_MAX_LIMIT}, got {value}.")
  return number


def _window(state: BqcaFilterState, window: Window | None) -> Window:
  return window if window is not None else resolve_window(state)


def _scope_sql(*, honor_event_types: bool = False) -> str:
  """Renders the filter predicates shared by every panel.

  The sentinel idiom (``'___ALL___' IN UNNEST(@x) OR col IN UNNEST(@x)``)
  is injection-safe and cannot crash on an empty array the way an ``IN ()``
  list would. The data-agent, persona and fast-path predicates read the
  turn-resolved columns of ``events`` (see ``_prelude``), so each one keeps or
  drops a turn's events together.

  Args:
    honor_event_types: Whether to apply the event-type multi-select. Off for
      aggregate panels: picking, say, ``LLM_RESPONSE`` there would zero out
      turn counts and error rates instead of narrowing anything useful.

  Returns:
    The predicates joined with ``AND``.
  """
  clauses = [
      f"('{ALL_SENTINEL}' IN UNNEST(@data_agent_ids)"
      " OR data_agent_id IN UNNEST(@data_agent_ids))",
      f"('{ALL_SENTINEL}' IN UNNEST(@personas)"
      " OR persona IN UNNEST(@personas))",
      "fast_path_label IN UNNEST(@fast_path_labels)",
      "(@session_search = ''"
      " OR STRPOS(LOWER(IFNULL(session_id, '')), LOWER(@session_search)) > 0"
      " OR STRPOS(LOWER(IFNULL(conversation_id, '')),"
      " LOWER(@session_search)) > 0)",
  ]
  if honor_event_types:
    clauses.append(
        f"('{ALL_SENTINEL}' IN UNNEST(@event_types)"
        " OR event_type IN UNNEST(@event_types))"
    )
  return "\n    AND ".join(clauses)


def _first_attributed(column: str) -> str:
  """Renders the earliest real value of ``column`` across a row's turn.

  A real value is a non-NULL one: ``FIRST_VALUE ... IGNORE NULLS`` takes the
  first of them in time order, whichever event of the turn carries it.
  ``event_type`` and then the value itself break ties, so the answer never
  depends on row order; and the frame spans the whole turn, so *every* row of
  it (not only the rows after the first real value) gets the same answer. A
  turn that carries no real value anywhere is NULL: the caller (``_prelude``)
  chooses the fallback, which keeps each persona tier separate until all three
  have been resolved. An event with no turn is the only row of its own
  partition (``_TURN_PARTITION_KEY``), so it keeps its own value.

  Args:
    column: The per-row ``raw_*`` column of ``events_raw`` to resolve.

  Returns:
    A SQL expression over ``events_raw``.
  """
  return (
      f"FIRST_VALUE({column} IGNORE NULLS)"
      f" OVER (PARTITION BY {_TURN_PARTITION_KEY}"
      f" ORDER BY timestamp ASC, event_type ASC, {column} ASC"
      " ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING)"
  )


def _turn_any(column: str) -> str:
  """Renders whether *any* event of a row's turn satisfies a boolean column.

  An event with no turn (a NULL ``invocation_id``) is judged on its own: it is
  the only row of its own partition (``_TURN_PARTITION_KEY``), where all such
  rows would otherwise share one NULL partition and inherit the verdict of an
  unrelated row.

  Args:
    column: A boolean column of ``events_raw``.

  Returns:
    A SQL expression over ``events_raw``.
  """
  return f"LOGICAL_OR({column}) OVER (PARTITION BY {_TURN_PARTITION_KEY})"


def _prelude(
    state: BqcaFilterState,
    window: Window,
    *,
    raw: Sequence[str] = (),
    derived: Sequence[tuple[str, str]] = (),
    honor_event_types: bool = False,
    extra_scope: Sequence[str] = (),
    apply_filters: bool = True,
) -> str:
  """Renders the ``events_raw``, ``events`` and ``scoped`` CTEs of every panel.

  ``events_raw`` is the only place the raw table is read: allowlisted rows in
  the window, with the normalized turn key (``INVOCATION_ID_EXPR``), the
  per-row dimensions, each turn-grain dimension as a per-row ``raw_*`` column,
  and any panel-specific columns, all computed once.

  ``events`` then resolves the turn-grain dimensions across each turn's rows
  (see ``_first_attributed`` and ``_turn_any``): ``data_agent_id`` is the
  turn's first logged data agent, ``persona`` is the first tier the turn
  carries (the persona custom label, else the email handle of ``user_id``,
  else the data agent), ``fast_path`` is "any event of the turn is tagged", and
  ``fast_path_label`` names that path. A turn with no data agent is
  ``'unattributed'``, and so is the persona of a turn with none of the three
  tiers. Each tier is resolved across the *whole* turn before the tiers are
  compared, so a label logged by a later event still beats a handle logged by
  an earlier one. Resolving before filtering is the point: the plugin does not
  stamp every event of a turn with the same attribution, so a row-level filter
  would keep a turn's prompt but drop its completion and make the turn look
  unfinished. With ``state.errors_only`` it also adds ``turn_has_error``, so
  *every* event of a turn that has an error somewhere survives, not just the
  error events. An event with no ``invocation_id`` at all (NULL) has no turn:
  it is resolved on its own (``_TURN_PARTITION_KEY``).

  ``scoped`` applies the sidebar filters to the resolved columns, a separate
  step because BigQuery cannot reference a select-list alias in ``WHERE``.

  Args:
    state: Active filters and connection.
    window: Query window.
    raw: Extra raw columns the panel needs beyond the base set.
    derived: Extra ``(alias, expression)`` columns the panel needs.
    honor_event_types: Whether to apply the event-type multi-select.
    extra_scope: Extra predicates AND-ed into ``scoped``.
    apply_filters: Whether to apply the sidebar filters at all. ``False``
      renders only ``events_raw`` and ``events`` (no ``scoped``, and no
      ``turn_has_error`` even with ``state.errors_only``); the sidebar's own
      option lists use that, because they must ignore every filter.

  Returns:
    ``WITH events_raw AS (...), events AS (...)``, followed by
    ``, scoped AS (...)`` when ``apply_filters``, with no trailing newline.
  """
  table = _validate_ident(state.project_id, state.dataset_id, state.table_id)
  columns = [
      column
      for column in dict.fromkeys((*_BASE_RAW, *raw))
      if column != "invocation_id"
  ]
  items = [f"    {column}" for column in columns]
  items.append(f"    {INVOCATION_ID_EXPR} AS invocation_id")
  items += [
      f"    {expression} AS {alias}"
      for alias, expression in (
          *_ROW_DIMENSIONS,
          *_TURN_DIMENSIONS,
          *derived,
      )
  ]
  resolved = [
      f"      {_first_attributed(raw_column)} AS {alias}"
      for alias, raw_column in _FIRST_TIERS
  ]
  resolved.append(f"      {_turn_any('raw_fast_path')} AS fast_path")
  where = [_scope_sql(honor_event_types=honor_event_types)]
  if apply_filters and state.errors_only:
    # Turn-level: keep *every* event of a turn that has an error somewhere,
    # not just the error events themselves.
    resolved.append(f"      {_turn_any('is_error')} AS turn_has_error")
    where.append("turn_has_error")
  where.extend(extra_scope)
  select = ",\n".join(items)
  turn_select = ",\n".join(resolved)
  raw_turn_columns = ", ".join(alias for alias, _ in _TURN_DIMENSIONS)
  first_columns = ", ".join(alias for alias, _ in _FIRST_TIERS)
  predicates = "\n    AND ".join(where)
  prelude = f"""
WITH events_raw AS (
  SELECT
{select}
  FROM {table}
  WHERE {queries.time_bounds(window)}
    AND event_type IN UNNEST(@allowed_event_types)
),
events AS (
  SELECT
    * EXCEPT ({first_columns}),
    COALESCE(first_data_agent_id, 'unattributed') AS data_agent_id,
    COALESCE(
      first_explicit_persona, first_email_persona, first_data_agent_id,
      'unattributed'
    ) AS persona,
    IF(fast_path, 'fast_path', 'standard_nl2sql') AS fast_path_label
  FROM (
    SELECT
      * EXCEPT ({raw_turn_columns}),
{turn_select}
    FROM events_raw
  )
)"""
  if apply_filters:
    prelude += f""",
scoped AS (
  SELECT *
  FROM events
  WHERE {predicates}
)"""
  return prelude.strip()


def build_bqca_filter_options_sql(
    f: BqcaFilterState, *, window: Window | None = None
) -> str:
  """Builds the sidebar option lists: data agents, personas, event types.

  The lists come from the same turn-resolved ``events`` CTE every panel
  filters on (see ``_prelude``): a persona is the turn's resolved persona, and
  ``'unattributed'`` is offered, for a data agent or a persona, when some turn
  has none. Offering the per-row values instead would list a persona no filter
  can ever match and could never offer ``'unattributed'`` at all.

  Still one pass over the raw table: crossing the rows with a literal array of
  structs pivots the three columns into ``(kind, value)`` rows, so the whole
  sidebar costs one scan rather than three.

  Args:
    f: Active connection (every sidebar filter is deliberately ignored, the
      errors-only switch included: picking one data agent must not hide the
      others).
    window: Query window; resolved from ``f`` when omitted.

  Returns:
    The SQL query string.
  """
  window = _window(f, window)
  prelude = _prelude(f, window, apply_filters=False)
  return f"""
{prelude}
SELECT
  o.kind,
  o.value,
  MAX(e.timestamp) AS last_seen
FROM events AS e,
  UNNEST([
    STRUCT('data_agent_id' AS kind, e.data_agent_id AS value),
    STRUCT('persona', e.persona),
    STRUCT('event_type', e.event_type)
  ]) AS o
WHERE o.value IS NOT NULL
GROUP BY o.kind, o.value
QUALIFY ROW_NUMBER() OVER (
  PARTITION BY o.kind ORDER BY MAX(e.timestamp) DESC
) <= {FILTER_OPTIONS_LIMIT}
ORDER BY o.kind, o.value
""".strip()


def build_bqca_kpis_sql(
    f: BqcaFilterState, *, window: Window | None = None
) -> str:
  """Builds the KPI header (panel P1).

  A *turn* is a distinct ``invocation_id``. ``turn_error_rate`` is turns with
  at least one error event over all turns (not error events over events), so
  one noisy turn cannot read as many failures. ``total_turns`` minus
  ``completed_turns`` are the *incomplete* turns (no ``INVOCATION_COMPLETED``
  event): they count in the turn totals and error rate but not in latency or
  the fast-path rate, and the app says so. The latency percentiles take one
  sample per completed turn: completion rows are folded per ``invocation_id``
  first (the slowest logged latency), so a turn whose completion was logged
  more than once is not weighted more than once. A turn is on the fast path if
  any of its events is tagged; ``_prelude`` resolves that across the whole turn
  before any filter runs, so the rate agrees with the per-data-agent table.
  ``embedding_coverage`` is the share of all turns that received at least one
  ``EMBEDDING_SUGGESTION`` with suggested columns; the denominator is every
  turn because the logging plugin drops a suggestion that has no columns, so a
  per-suggestion "hit rate" would always read 100%. ``HAVING COUNT(*) > 0``
  keeps the no-data contract: an unaggregated SELECT over aggregates always
  emits a row, so an empty filter intersection would otherwise report a
  confident "0 turns, 0% errors". When events exist but none belongs to a turn
  (every ``invocation_id`` is NULL; an empty one is a turn of its own, see
  ``INVOCATION_ID_EXPR``) the row reads 0 turns, an *undefined* (NULL) error
  rate and the orphan events' token totals; the app shows the rate as a dash
  and says why.

  Args:
    f: Active filters and connection.
    window: Query window; resolved from ``f`` when omitted.

  Returns:
    The SQL query string.
  """
  window = _window(f, window)
  prelude = _prelude(
      f,
      window,
      derived=(
          (
              "turn_latency_ms",
              f"IF(event_type = 'INVOCATION_COMPLETED', {TOTAL_LATENCY_MS_EXPR},"
              " NULL)",
          ),
          (
              "total_tokens",
              f"IF(event_type = 'LLM_RESPONSE', {TOTAL_TOKENS_EXPR}, NULL)",
          ),
          (
              "thoughts_tokens",
              f"IF(event_type = 'LLM_RESPONSE', {THOUGHTS_TOKENS_EXPR}, NULL)",
          ),
          (
              "cached_tokens",
              f"IF(event_type = 'LLM_RESPONSE', {CACHED_TOKENS_EXPR}, NULL)",
          ),
          (
              "suggested_columns_count",
              "IF(event_type = 'EMBEDDING_SUGGESTION',"
              f" {SUGGESTED_COLUMNS_COUNT_EXPR}, NULL)",
          ),
      ),
  )
  return f"""
{prelude},
completion_per_turn AS (
  SELECT
    invocation_id,
    MAX(turn_latency_ms) AS turn_latency_ms
  FROM scoped
  WHERE event_type = 'INVOCATION_COMPLETED'
    AND invocation_id IS NOT NULL
  GROUP BY invocation_id
)
SELECT
  COUNT(DISTINCT invocation_id) AS total_turns,
  COUNT(DISTINCT IF(event_type = 'INVOCATION_COMPLETED', invocation_id, NULL))
    AS completed_turns,
  COUNTIF(is_error) AS error_events,
  SAFE_DIVIDE(
    COUNT(DISTINCT IF(is_error, invocation_id, NULL)),
    COUNT(DISTINCT invocation_id)
  ) AS turn_error_rate,
  (
    SELECT APPROX_QUANTILES(turn_latency_ms, 100)[SAFE_OFFSET(50)]
    FROM completion_per_turn
  ) AS p50_turn_latency_ms,
  (
    SELECT APPROX_QUANTILES(turn_latency_ms, 100)[SAFE_OFFSET(95)]
    FROM completion_per_turn
  ) AS p95_turn_latency_ms,
  IFNULL(SUM(total_tokens), 0) AS total_tokens,
  IFNULL(SUM(thoughts_tokens), 0) AS thoughts_tokens,
  IFNULL(SUM(cached_tokens), 0) AS cached_tokens,
  SAFE_DIVIDE(
    COUNT(DISTINCT IF(
      event_type = 'INVOCATION_COMPLETED' AND fast_path, invocation_id, NULL
    )),
    COUNT(DISTINCT IF(event_type = 'INVOCATION_COMPLETED', invocation_id, NULL))
  ) AS fast_path_rate,
  SAFE_DIVIDE(
    COUNT(DISTINCT IF(suggested_columns_count > 0, invocation_id, NULL)),
    COUNT(DISTINCT invocation_id)
  ) AS embedding_coverage
FROM scoped
HAVING COUNT(*) > 0
""".strip()


def build_bqca_turn_volume_sql(
    f: BqcaFilterState, *, window: Window | None = None
) -> str:
  """Builds turn volume per time bucket and path (panel P2).

  Aggregated at turn grain first (one row per ``invocation_id``, bucketed by
  the turn's first event), so a turn counts once however many events it has,
  and a turn is "fast path" if any of its events is tagged.

  Args:
    f: Active filters and connection.
    window: Query window; resolved from ``f`` when omitted.

  Returns:
    The SQL query string.
  """
  window = _window(f, window)
  prelude = _prelude(f, window)
  return f"""
{prelude},
per_turn AS (
  SELECT
    invocation_id,
    MIN(timestamp) AS started_at,
    LOGICAL_OR(fast_path) AS fast_path,
    LOGICAL_OR(event_type = 'INVOCATION_COMPLETED') AS completed,
    LOGICAL_OR(is_error) AS has_error
  FROM scoped
  WHERE invocation_id IS NOT NULL
  GROUP BY invocation_id
)
SELECT
  TIMESTAMP_TRUNC(started_at, {window.bucket}) AS bucket,
  IF(fast_path, 'fast_path', 'standard_nl2sql') AS fast_path_label,
  COUNT(*) AS turns,
  COUNTIF(completed) AS completed_turns,
  COUNTIF(has_error) AS error_turns
FROM per_turn
GROUP BY bucket, fast_path_label
ORDER BY bucket, fast_path_label
""".strip()


def build_bqca_latency_sql(
    f: BqcaFilterState, *, window: Window | None = None
) -> str:
  """Builds latency percentiles per time bucket (panel P3).

  Turn latency is ``INVOCATION_COMPLETED``'s ``total_ms``; LLM latency and
  time-to-first-token come from ``LLM_RESPONSE``. Each bucket appears once
  for ``fast_path_label = 'all'`` and once per path, so the chart can compare
  the fast path with standard NL2SQL without a second query.

  The turn percentiles take one sample per turn: a turn whose completion was
  logged more than once is folded into a single row first (its slowest logged
  latency, in the first bucket it completed in, on the fast path if any of
  those rows is tagged), so duplicates cannot weigh a turn more than once.
  LLM latency and time-to-first-token stay one sample per ``LLM_RESPONSE``
  event, because every response is a call of its own.

  Args:
    f: Active filters and connection.
    window: Query window; resolved from ``f`` when omitted.

  Returns:
    The SQL query string.
  """
  window = _window(f, window)
  prelude = _prelude(
      f,
      window,
      derived=(
          ("bucket", f"TIMESTAMP_TRUNC(timestamp, {window.bucket})"),
          ("total_latency_ms", TOTAL_LATENCY_MS_EXPR),
          ("tfft_ms", TFFT_MS_EXPR),
      ),
  )
  llm = "IF(event_type = 'LLM_RESPONSE', total_latency_ms, NULL)"
  tfft = "IF(event_type = 'LLM_RESPONSE', tfft_ms, NULL)"
  # ``latency_samples`` is one row per sample: each turn's folded completion
  # (turn latency only) and every scoped event (LLM latency and time to first
  # token only, NULL for event types that carry neither). Keeping every event
  # as a row leaves the bucket/path grid exactly as it was, so a bucket that
  # has events but no latency sample still gets its all-NULL row.
  return f"""
{prelude},
completion_per_turn AS (
  SELECT
    invocation_id,
    MIN(bucket) AS bucket,
    IF(LOGICAL_OR(fast_path), 'fast_path', 'standard_nl2sql')
      AS fast_path_label,
    MAX(total_latency_ms) AS turn_ms
  FROM scoped
  WHERE event_type = 'INVOCATION_COMPLETED'
    AND invocation_id IS NOT NULL
  GROUP BY invocation_id
),
latency_samples AS (
  SELECT
    bucket,
    fast_path_label,
    turn_ms,
    CAST(NULL AS FLOAT64) AS llm_ms,
    CAST(NULL AS FLOAT64) AS tfft_ms
  FROM completion_per_turn
  UNION ALL
  SELECT
    bucket,
    fast_path_label,
    CAST(NULL AS FLOAT64) AS turn_ms,
    {llm} AS llm_ms,
    {tfft} AS tfft_ms
  FROM scoped
),
grouped AS (
  SELECT
    bucket,
    fast_path_label AS path_label,
    APPROX_QUANTILES(turn_ms, 100)[SAFE_OFFSET(50)] AS turn_p50_ms,
    APPROX_QUANTILES(turn_ms, 100)[SAFE_OFFSET(95)] AS turn_p95_ms,
    APPROX_QUANTILES(turn_ms, 100)[SAFE_OFFSET(99)] AS turn_p99_ms,
    APPROX_QUANTILES(llm_ms, 100)[SAFE_OFFSET(50)] AS llm_p50_ms,
    APPROX_QUANTILES(llm_ms, 100)[SAFE_OFFSET(95)] AS llm_p95_ms,
    APPROX_QUANTILES(tfft_ms, 100)[SAFE_OFFSET(50)] AS tfft_p50_ms,
    APPROX_QUANTILES(tfft_ms, 100)[SAFE_OFFSET(95)] AS tfft_p95_ms
  FROM latency_samples
  GROUP BY GROUPING SETS ((bucket), (bucket, fast_path_label))
)
SELECT
  bucket,
  COALESCE(path_label, 'all') AS fast_path_label,
  turn_p50_ms,
  turn_p95_ms,
  turn_p99_ms,
  llm_p50_ms,
  llm_p95_ms,
  tfft_p50_ms,
  tfft_p95_ms
FROM grouped
ORDER BY bucket, fast_path_label
""".strip()


def build_bqca_token_usage_sql(
    f: BqcaFilterState, *, window: Window | None = None
) -> str:
  """Builds token usage per time bucket and model (panel P4).

  Over ``LLM_RESPONSE`` only: that is the one event type that carries
  ``usage_metadata``. Fast-path turns make no LLM call, so they contribute
  nothing here by construction. One result serves both the over-time and the
  per-model views (the app groups it client-side).

  Args:
    f: Active filters and connection.
    window: Query window; resolved from ``f`` when omitted.

  Returns:
    The SQL query string.
  """
  window = _window(f, window)
  prelude = _prelude(
      f,
      window,
      derived=(
          ("bucket", f"TIMESTAMP_TRUNC(timestamp, {window.bucket})"),
          ("model_name", MODEL_NAME_EXPR),
          ("input_tokens", INPUT_TOKENS_EXPR),
          ("output_tokens", OUTPUT_TOKENS_EXPR),
          ("thoughts_tokens", THOUGHTS_TOKENS_EXPR),
          ("cached_tokens", CACHED_TOKENS_EXPR),
          ("total_tokens", TOTAL_TOKENS_EXPR),
      ),
      extra_scope=("event_type = 'LLM_RESPONSE'",),
  )
  return f"""
{prelude},
llm AS (
  SELECT
    bucket,
    IFNULL(model_name, '(unknown)') AS model_name,
    input_tokens,
    output_tokens,
    thoughts_tokens,
    cached_tokens,
    total_tokens
  FROM scoped
)
SELECT
  bucket,
  model_name,
  COUNT(*) AS llm_calls,
  IFNULL(SUM(input_tokens), 0) AS input_tokens,
  IFNULL(SUM(output_tokens), 0) AS output_tokens,
  IFNULL(SUM(thoughts_tokens), 0) AS thoughts_tokens,
  IFNULL(SUM(cached_tokens), 0) AS cached_tokens,
  IFNULL(SUM(total_tokens), 0) AS total_tokens
FROM llm
GROUP BY bucket, model_name
ORDER BY bucket, model_name
""".strip()


def build_bqca_data_agent_breakdown_sql(
    f: BqcaFilterState,
    limit: int = BREAKDOWN_LIMIT,
    *,
    window: Window | None = None,
) -> str:
  """Builds the per-data-agent leaderboard (panel P5).

  Turn grain first, then grouped by the turn's data agent, so counts are
  turns, not events. A turn's data agent is the first real id any of its events
  carries (the plugin does not stamp every event). Turns that carry no
  data-agent id are grouped under ``'unattributed'`` rather than dropped, so
  the table still adds up to the KPI header. As in the KPI header, latency and
  the fast-path rate cover completed turns only (a turn that reached
  ``INVOCATION_COMPLETED``): the rate is the completed fast-path turns over the
  completed turns, so a turn that never completed weighs in neither term and an
  agent with no completed turn has no rate at all.

  Args:
    f: Active filters and connection.
    limit: Maximum number of data agents to return.
    window: Query window; resolved from ``f`` when omitted.

  Returns:
    The SQL query string.
  """
  window = _window(f, window)
  prelude = _prelude(
      f,
      window,
      raw=("user_id",),
      derived=(
          (
              "turn_latency_ms",
              f"IF(event_type = 'INVOCATION_COMPLETED', {TOTAL_LATENCY_MS_EXPR},"
              " NULL)",
          ),
          (
              "total_tokens",
              f"IF(event_type = 'LLM_RESPONSE', {TOTAL_TOKENS_EXPR}, NULL)",
          ),
      ),
  )
  return f"""
{prelude},
per_turn AS (
  SELECT
    invocation_id,
    COALESCE(MAX(NULLIF(data_agent_id, 'unattributed')), 'unattributed')
      AS data_agent_id,
    MAX(conversation_id) AS conversation_id,
    MAX(user_id) AS user_id,
    LOGICAL_OR(event_type = 'INVOCATION_COMPLETED') AS is_completed,
    LOGICAL_OR(event_type = 'INVOCATION_COMPLETED' AND fast_path)
      AS is_completed_fast_path,
    LOGICAL_OR(is_error) AS has_error,
    MAX(turn_latency_ms) AS turn_latency_ms,
    SUM(total_tokens) AS total_tokens
  FROM scoped
  WHERE invocation_id IS NOT NULL
  GROUP BY invocation_id
)
SELECT
  data_agent_id,
  COUNT(*) AS turns,
  COUNT(DISTINCT conversation_id) AS unique_conversations,
  COUNT(DISTINCT user_id) AS unique_users,
  SAFE_DIVIDE(COUNTIF(has_error), COUNT(*)) AS error_rate,
  SAFE_DIVIDE(COUNTIF(is_completed_fast_path), COUNTIF(is_completed))
    AS fast_path_rate,
  APPROX_QUANTILES(turn_latency_ms, 100)[SAFE_OFFSET(50)] AS p50_latency_ms,
  APPROX_QUANTILES(turn_latency_ms, 100)[SAFE_OFFSET(95)] AS p95_latency_ms,
  IFNULL(SUM(total_tokens), 0) AS total_tokens
FROM per_turn
GROUP BY data_agent_id
ORDER BY turns DESC, data_agent_id
LIMIT {_limit(limit)}
""".strip()


def build_bqca_persona_breakdown_sql(
    f: BqcaFilterState,
    limit: int = BREAKDOWN_LIMIT,
    *,
    window: Window | None = None,
) -> str:
  """Builds the per-persona breakdown (panel P6).

  Turn grain first, then grouped by the turn's persona: its first real persona,
  or ``'unattributed'`` only when none of its events carries one.

  Args:
    f: Active filters and connection.
    limit: Maximum number of personas to return.
    window: Query window; resolved from ``f`` when omitted.

  Returns:
    The SQL query string.
  """
  window = _window(f, window)
  prelude = _prelude(
      f,
      window,
      derived=(
          (
              "turn_latency_ms",
              f"IF(event_type = 'INVOCATION_COMPLETED', {TOTAL_LATENCY_MS_EXPR},"
              " NULL)",
          ),
          (
              "total_tokens",
              f"IF(event_type = 'LLM_RESPONSE', {TOTAL_TOKENS_EXPR}, NULL)",
          ),
      ),
  )
  return f"""
{prelude},
per_turn AS (
  SELECT
    invocation_id,
    COALESCE(MAX(NULLIF(persona, 'unattributed')), 'unattributed') AS persona,
    MAX(conversation_id) AS conversation_id,
    LOGICAL_OR(is_error) AS has_error,
    MAX(turn_latency_ms) AS turn_latency_ms,
    SUM(total_tokens) AS total_tokens
  FROM scoped
  WHERE invocation_id IS NOT NULL
  GROUP BY invocation_id
)
SELECT
  persona,
  COUNT(*) AS turns,
  COUNT(DISTINCT conversation_id) AS unique_conversations,
  SAFE_DIVIDE(COUNTIF(has_error), COUNT(*)) AS error_rate,
  AVG(turn_latency_ms) AS avg_latency_ms,
  IFNULL(SUM(total_tokens), 0) AS total_tokens
FROM per_turn
GROUP BY persona
ORDER BY turns DESC, persona
LIMIT {_limit(limit)}
""".strip()


def build_bqca_embedding_suggestions_sql(
    f: BqcaFilterState, *, window: Window | None = None
) -> str:
  """Builds embedding-suggestion activity per time bucket and reason (panel P7).

  The logging plugin writes one ``EMBEDDING_SUGGESTION`` event per suggestion,
  with ``{"reason": ..., "suggested_columns": [...]}`` as its content, so a
  suggestion is measured by the columns it proposes, not by a match count
  (the payload carries none). ``suggestion_events`` counts suggestions,
  ``suggestion_turns`` the distinct turns that received one with columns, and
  ``suggested_columns`` / ``avg_suggested_columns`` how many columns they
  proposed. The ``*_column(s)`` bin columns count the same events by how many
  columns each proposed, which is the distribution the Tokens & Embedding
  Suggestions tab charts.

  Args:
    f: Active filters and connection.
    window: Query window; resolved from ``f`` when omitted.

  Returns:
    The SQL query string.
  """
  window = _window(f, window)
  prelude = _prelude(
      f,
      window,
      derived=(
          ("bucket", f"TIMESTAMP_TRUNC(timestamp, {window.bucket})"),
          ("suggested_columns_count", SUGGESTED_COLUMNS_COUNT_EXPR),
          ("embedding_reason", EMBEDDING_REASON_EXPR),
      ),
      extra_scope=("event_type = 'EMBEDDING_SUGGESTION'",),
  )
  return f"""
{prelude}
SELECT
  bucket,
  embedding_reason,
  COUNT(*) AS suggestion_events,
  COUNT(DISTINCT IF(suggested_columns_count > 0, invocation_id, NULL))
    AS suggestion_turns,
  SUM(suggested_columns_count) AS suggested_columns,
  AVG(suggested_columns_count) AS avg_suggested_columns,
  COUNTIF(suggested_columns_count = 0) AS zero_columns,
  COUNTIF(suggested_columns_count = 1) AS one_column,
  COUNTIF(suggested_columns_count BETWEEN 2 AND 4) AS two_to_four_columns,
  COUNTIF(suggested_columns_count >= 5) AS five_plus_columns
FROM scoped
GROUP BY bucket, embedding_reason
ORDER BY bucket, embedding_reason
""".strip()


def build_bqca_conversation_turns_sql(
    f: BqcaFilterState,
    limit: int = TURN_EXPLORER_LIMIT,
    *,
    window: Window | None = None,
) -> str:
  """Builds the Prompt, Response & SQL Explorer's turn table (panel P8).

  One row per turn (``invocation_id``), newest first: the prompt from the
  turn's ``USER_MESSAGE_RECEIVED`` / ``INVOCATION_STARTING`` event, the
  answer from its *last* ``AGENT_RESPONSE`` event, and the turn's latency,
  tokens and error messages. A turn can log several ``AGENT_RESPONSE`` events
  and only the last one is the answer that was served, so the response and
  the SQL fence found in it are both read from that one event: a discarded
  draft's SQL never appears next to the served answer. ``agent_response_count``
  says how many were logged. A multi-part answer is its parts' markdown
  joined with a blank line, so an answer is *not* one scalar. This is the
  only panel that reads the wide ``content`` column, so a large window costs
  more here than anywhere else.

  ``prompt_search`` is applied per turn, after the prompt is assembled.

  Args:
    f: Active filters and connection.
    limit: Maximum number of turns to return.
    window: Query window; resolved from ``f`` when omitted.

  Returns:
    The SQL query string.
  """
  window = _window(f, window)
  prelude = _prelude(
      f,
      window,
      raw=("user_id", "error_message"),
      derived=(
          (
              "turn_latency_ms",
              f"IF(event_type = 'INVOCATION_COMPLETED', {TOTAL_LATENCY_MS_EXPR},"
              " NULL)",
          ),
          (
              "total_tokens",
              f"IF(event_type = 'LLM_RESPONSE', {TOTAL_TOKENS_EXPR}, NULL)",
          ),
          (
              "thoughts_tokens",
              f"IF(event_type = 'LLM_RESPONSE', {THOUGHTS_TOKENS_EXPR}, NULL)",
          ),
          (
              "user_prompt_text",
              "IF(event_type IN ('USER_MESSAGE_RECEIVED',"
              f" 'INVOCATION_STARTING'), {USER_PROMPT_TEXT_EXPR}, NULL)",
          ),
          (
              "agent_response_text",
              "IF(event_type = 'AGENT_RESPONSE',"
              f" {AGENT_RESPONSE_TEXT_EXPR}, NULL)",
          ),
          (
              "extracted_sql",
              f"IF(event_type = 'AGENT_RESPONSE', {EXTRACTED_SQL_EXPR}, NULL)",
          ),
      ),
  )
  return f"""
{prelude},
per_turn AS (
  SELECT
    invocation_id,
    MIN(timestamp) AS timestamp,
    MAX(session_id) AS session_id,
    MAX(conversation_id) AS conversation_id,
    COALESCE(MAX(NULLIF(data_agent_id, 'unattributed')), 'unattributed')
      AS data_agent_id,
    COALESCE(MAX(NULLIF(persona, 'unattributed')), 'unattributed') AS persona,
    MAX(user_id) AS user_id,
    LOGICAL_OR(fast_path) AS fast_path,
    IF(LOGICAL_OR(is_error), 'ERROR', 'OK') AS status,
    MAX(turn_latency_ms) AS turn_latency_ms,
    SUM(total_tokens) AS total_tokens,
    SUM(thoughts_tokens) AS thoughts_tokens,
    ARRAY_AGG(user_prompt_text IGNORE NULLS ORDER BY timestamp ASC LIMIT 1)
      [SAFE_OFFSET(0)] AS user_prompt,
    ARRAY_AGG(
      IF(event_type = 'AGENT_RESPONSE',
        STRUCT(
          agent_response_text AS response_text,
          extracted_sql AS response_sql
        ),
        NULL)
      IGNORE NULLS ORDER BY timestamp DESC LIMIT 1
    )[SAFE_OFFSET(0)] AS served_response,
    COUNTIF(event_type = 'AGENT_RESPONSE') AS agent_response_count,
    STRING_AGG(DISTINCT error_message, ' | ') AS error_message
  FROM scoped
  WHERE invocation_id IS NOT NULL
  GROUP BY invocation_id
)
SELECT
  timestamp,
  invocation_id,
  session_id,
  conversation_id,
  data_agent_id,
  persona,
  user_id,
  fast_path,
  status,
  turn_latency_ms,
  total_tokens,
  thoughts_tokens,
  user_prompt,
  served_response.response_text AS agent_response,
  served_response.response_sql AS extracted_sql,
  agent_response_count,
  error_message
FROM per_turn
WHERE (@prompt_search = ''
  OR STRPOS(LOWER(IFNULL(user_prompt, '')), LOWER(@prompt_search)) > 0)
ORDER BY timestamp DESC
LIMIT {_limit(limit)}
""".strip()


def build_bqca_turn_timeline_sql(
    f: BqcaFilterState,
    invocation_id: str,
    *,
    window: Window | None = None,
) -> str:
  """Builds the ordered event timeline of one turn (panel P9).

  The invocation is bound as ``@invocation_id`` and compared with the same
  normalized turn key the explorer lists (``INVOCATION_ID_EXPR``), so a turn
  whose events were logged with an empty ``invocation_id`` opens by the
  timestamp key the explorer shows for it; this builder only checks the key is
  not blank. The sidebar's event-type selection narrows the timeline, but the
  other filters do not: once a turn is picked, every one of its events belongs
  in the picture. The query is still bounded by the window, so partition
  pruning keeps it cheap.

  Args:
    f: Active connection and event-type selection.
    invocation_id: The turn to expand. Bound as a parameter, not embedded.
    window: Query window; resolved from ``f`` when omitted.

  Returns:
    The SQL query string.

  Raises:
    ValueError: If ``invocation_id`` is empty or only whitespace.
  """
  if not str(invocation_id).strip():
    raise ValueError("invocation_id is required for a turn timeline.")
  table = _validate_ident(f.project_id, f.dataset_id, f.table_id)
  window = _window(f, window)
  preview = (
      "SUBSTR(COALESCE("
      f"{USER_PROMPT_TEXT_EXPR},"
      f" {AGENT_RESPONSE_TEXT_EXPR},"
      " error_message,"
      " IF(content IS NULL, NULL, TO_JSON_STRING(content)),"
      " ''), 1, 500)"
  )
  return f"""
WITH events AS (
  SELECT
    timestamp,
    event_type,
    span_id,
    parent_span_id,
    status,
    error_message,
    {TOTAL_LATENCY_MS_EXPR} AS total_latency_ms,
    {TFFT_MS_EXPR} AS tfft_ms,
    {MODEL_NAME_EXPR} AS model_name,
    {TOTAL_TOKENS_EXPR} AS total_tokens,
    {THOUGHTS_TOKENS_EXPR} AS thoughts_tokens,
    {FAST_PATH_EXPR} AS fast_path,
    {preview} AS content_preview
  FROM {table}
  WHERE {queries.time_bounds(window)}
    AND event_type IN UNNEST(@allowed_event_types)
    AND {INVOCATION_ID_EXPR} = @invocation_id
)
SELECT
  timestamp,
  event_type,
  span_id,
  parent_span_id,
  total_latency_ms AS latency_ms,
  tfft_ms,
  status,
  error_message,
  model_name,
  total_tokens,
  thoughts_tokens,
  fast_path,
  content_preview
FROM events
WHERE ('{ALL_SENTINEL}' IN UNNEST(@event_types)
  OR event_type IN UNNEST(@event_types))
ORDER BY timestamp, event_type
LIMIT {TIMELINE_LIMIT}
""".strip()


def build_bqca_error_attribution_sql(
    f: BqcaFilterState,
    limit: int = ERROR_ATTRIBUTION_LIMIT,
    *,
    window: Window | None = None,
) -> str:
  """Builds the error attribution breakdown (panel P10).

  An error event satisfies the three-condition predicate: an ERROR status,
  an error message, *or* an ``*_ERROR`` event type. Keying on status alone
  misses the typed error events that carry no status. Grouped by event
  type, data agent, persona and message (truncated so a long stack trace
  does not split one failure mode into many rows), most frequent first.

  Args:
    f: Active filters and connection.
    limit: Maximum number of error groups to return.
    window: Query window; resolved from ``f`` when omitted.

  Returns:
    The SQL query string.
  """
  window = _window(f, window)
  prelude = _prelude(
      f,
      window,
      raw=("error_message",),
      honor_event_types=True,
      extra_scope=("is_error",),
  )
  return f"""
{prelude},
error_rows AS (
  SELECT
    event_type,
    IFNULL(data_agent_id, 'unattributed') AS data_agent_id,
    persona,
    SUBSTR(IFNULL(NULLIF(TRIM(error_message), ''), '(no message)'), 1, 300)
      AS error_message,
    invocation_id,
    timestamp
  FROM scoped
)
SELECT
  event_type,
  data_agent_id,
  persona,
  error_message,
  COUNT(*) AS errors,
  COUNT(DISTINCT invocation_id) AS error_turns,
  MIN(timestamp) AS first_seen,
  MAX(timestamp) AS last_seen
FROM error_rows
GROUP BY event_type, data_agent_id, persona, error_message
ORDER BY errors DESC, last_seen DESC
LIMIT {_limit(limit)}
""".strip()


# ------------------------------------------------------------------ #
# Query parameters                                                     #
# ------------------------------------------------------------------ #


class BqcaParam(NamedTuple):
  """One BigQuery query parameter, as a hashable value.

  Hashable so the whole parameter set can key ``@st.cache_data`` next to the
  SQL text: filters live in parameters rather than in the SQL, so the SQL
  string alone would be an incomplete cache key.

  Attributes:
    name: Parameter name, without the ``@``.
    bq_type: BigQuery scalar type (``"STRING"``).
    is_array: Whether the parameter is an ``ARRAY<bq_type>``.
    value: A tuple of values for an array, else the scalar value.
  """

  name: str
  bq_type: str
  is_array: bool
  value: Any


def _array(values: Sequence[str]) -> tuple[str, ...]:
  """An empty selection means "everything": the sentinel keeps ``IN`` safe."""
  cleaned = tuple(dict.fromkeys(str(v) for v in values if str(v)))
  return cleaned or (ALL_SENTINEL,)


def bqca_query_params(
    f: BqcaFilterState, sql: str, **scalars: str
) -> tuple[BqcaParam, ...]:
  """Binds exactly the parameters ``sql`` references.

  Scanning the text keeps the builders free of bookkeeping about which
  filters they honor, and keeps BigQuery from receiving parameters the query
  never mentions.

  Args:
    f: Active filters.
    sql: The SQL text to inspect.
    **scalars: Extra STRING parameters (for example ``invocation_id``).

  Returns:
    The referenced parameters, in a stable order.

  Raises:
    ValueError: If ``sql`` references a parameter nothing can bind.
  """
  available: dict[str, BqcaParam] = {
      "allowed_event_types": BqcaParam(
          "allowed_event_types", "STRING", True, BQCA_ALLOWED_EVENT_TYPES
      ),
      "data_agent_ids": BqcaParam(
          "data_agent_ids", "STRING", True, _array(f.data_agent_ids)
      ),
      "personas": BqcaParam("personas", "STRING", True, _array(f.personas)),
      "event_types": BqcaParam(
          "event_types", "STRING", True, _array(f.selected_event_types())
      ),
      "fast_path_labels": BqcaParam(
          "fast_path_labels", "STRING", True, f.fast_path_labels()
      ),
      "session_search": BqcaParam(
          "session_search", "STRING", False, f.session_search.strip()
      ),
      "prompt_search": BqcaParam(
          "prompt_search", "STRING", False, f.prompt_search.strip()
      ),
  }
  for name, value in scalars.items():
    available[name] = BqcaParam(name, "STRING", False, str(value))
  used = set(_PARAM_RE.findall(sql))
  unbound = used - set(available)
  if unbound:
    raise ValueError(f"SQL references unbound parameter(s): {sorted(unbound)}")
  return tuple(p for name, p in available.items() if name in used)


# ------------------------------------------------------------------ #
# BigQuery execution                                                   #
# ------------------------------------------------------------------ #


def _to_bigquery(
    param: BqcaParam,
) -> bigquery.ArrayQueryParameter | bigquery.ScalarQueryParameter:
  if param.is_array:
    return bigquery.ArrayQueryParameter(
        param.name, param.bq_type, list(param.value)
    )
  return bigquery.ScalarQueryParameter(param.name, param.bq_type, param.value)


def _job_config(
    params: Sequence[BqcaParam],
    max_bytes: int,
    *,
    dry_run: bool = False,
) -> bigquery.QueryJobConfig:
  """Returns a job config carrying the per-query scan guardrail.

  Args:
    params: Bound parameters.
    max_bytes: Maximum allowed bytes billed.
    dry_run: Whether to configure the job as a dry run.

  Returns:
    A configured ``bigquery.QueryJobConfig``.
  """
  config = bigquery.QueryJobConfig(
      query_parameters=[_to_bigquery(p) for p in params],
      use_legacy_sql=False,
      use_query_cache=True,
      dry_run=dry_run,
      labels={"app": "bqaa_streamlit", "surface": "bqca"},
  )
  # A dry run bills nothing, so the cap only belongs on the real job, where
  # it is the guardrail of record rather than advice from the preflight.
  if not dry_run:
    config.maximum_bytes_billed = int(max_bytes)
  return config


def _explain(exc: Exception) -> str:
  """Turns the BigQuery errors operators actually hit into actionable advice.

  Args:
    exc: The exception caught during query execution.

  Returns:
    A user-friendly explanation with remediation advice.
  """
  message = getattr(exc, "message", None) or str(exc)
  if isinstance(exc, gauth_exc.DefaultCredentialsError):
    return (
        f"{message}\n\nRun `gcloud auth application-default login` or set"
        " `GOOGLE_APPLICATION_CREDENTIALS`."
    )
  if isinstance(exc, gexc.NotFound):
    return (
        f"{message}\n\nCheck the project, dataset and events table in the"
        " sidebar. The BQCA surface defaults to the table"
        " `bqca_prompt_response_logs`; set `BQCA_TABLE_ID` (or `BQ_TABLE_ID`)"
        " if your logs live in a differently named table."
    )
  if isinstance(exc, gexc.Forbidden):
    return (
        f"{message}\n\nThe caller needs `roles/bigquery.jobUser` on the"
        " project and `roles/bigquery.dataViewer` on the dataset."
    )
  if "bytesBilledLimitExceeded" in message:
    return (
        f"{message}\n\nRaise the per-query scan cap in the sidebar or narrow"
        " the time range."
    )
  return message


_MAX_SEEN_RUN_IDS = 10_000
_NEXT_RUN_ID = 0
_SEEN_RUN_IDS: set[int] = set()
_RUN_ID_LOCK = threading.Lock()


@st.cache_data(
    ttl=CACHE_TTL_SECONDS, show_spinner=False, max_entries=CACHE_MAX_ENTRIES
)
def _run_bqca_query_cached(
    sql: str,
    params: tuple[BqcaParam, ...],
    project: str,
    max_bytes: int,
) -> tuple[pd.DataFrame, int, int, bool, bool, bool, int]:
  """Runs the BigQuery job, cached by Streamlit across reruns.

  Args:
    sql: The SQL query to execute.
    params: Bound query parameters (part of the cache key).
    project: BigQuery project ID.
    max_bytes: Maximum allowed bytes billed.

  Returns:
    Tuple of (dataframe, bytes_processed, bytes_billed, cache_hit,
    proc_known, bill_known, run_id).

  Raises:
    QueryExecutionError: If the preflight, the guardrail or the job fails.
  """
  global _NEXT_RUN_ID
  with _RUN_ID_LOCK:
    _NEXT_RUN_ID += 1
    run_id = _NEXT_RUN_ID

  try:
    client = queries.get_client(project)
    probe = client.query(
        sql, job_config=_job_config(params, max_bytes, dry_run=True)
    )
    estimate = int(probe.total_bytes_processed or 0)
  except (gexc.GoogleAPICallError, gauth_exc.DefaultCredentialsError) as exc:
    raise queries.QueryExecutionError(
        _explain(exc),
        bytes_processed=0,
        bytes_billed=0,
        cache_hit=False,
        bytes_processed_known=True,
        bytes_billed_known=True,
    ) from exc

  if estimate > max_bytes:
    raise queries.QueryExecutionError(
        f"Guardrail: this query would scan {humanize_bytes(estimate)}, above"
        f" the {humanize_bytes(max_bytes)} per-query cap. Narrow the time"
        " range or raise the cap in the sidebar.",
        bytes_processed=0,
        bytes_billed=0,
        cache_hit=False,
        bytes_processed_known=True,
        bytes_billed_known=True,
    )

  job = None
  try:
    job = client.query(sql, job_config=_job_config(params, max_bytes))
    df = job.to_dataframe(create_bqstorage_client=False)
  except (gexc.GoogleAPICallError, gauth_exc.DefaultCredentialsError) as exc:
    raise queries._query_failure(_explain(exc), job) from exc  # pylint: disable=protected-access
  except Exception as exc:
    raise queries._query_failure(str(exc), job) from exc  # pylint: disable=protected-access

  stats = queries._extract_job_stats(job)  # pylint: disable=protected-access
  if stats is None:
    processed, billed, cache_hit, proc_known, bill_known = (
        0,
        0,
        False,
        False,
        False,
    )
  else:
    processed, billed, cache_hit, proc_known, bill_known = stats
  return df, processed, billed, cache_hit, proc_known, bill_known, run_id


def run_bqca_query(
    sql: str,
    params: tuple[BqcaParam, ...],
    project: str,
    max_bytes: int,
) -> QueryResult:
  """Runs one BQCA query behind a dry-run preflight and the byte cap.

  The dry run is free and reports the scan size before anything is billed,
  so a query that would blow the cap becomes a readable guardrail message
  instead of an opaque ``bytesBilledLimitExceeded``.

  Args:
    sql: The SQL query to execute.
    params: Bound query parameters.
    project: BigQuery project ID.
    max_bytes: Maximum allowed bytes billed.

  Returns:
    A ``QueryResult`` with the frame and execution metadata; on failure the
    frame is empty and ``error`` is set.
  """
  try:
    (
        df,
        bytes_processed,
        bytes_billed,
        cache_hit,
        proc_known,
        bill_known,
        run_id,
    ) = _run_bqca_query_cached(sql, params, project, max_bytes)
  except Exception as exc:  # pylint: disable=broad-exception-caught
    if isinstance(exc, queries.QueryExecutionError):
      return QueryResult(
          df=pd.DataFrame(),
          error=str(exc),
          bytes_processed=int(exc.bytes_processed),
          bytes_billed=int(exc.bytes_billed),
          cache_hit=bool(exc.cache_hit),
          bytes_processed_known=bool(exc.bytes_processed_known),
          bytes_billed_known=bool(exc.bytes_billed_known),
      )
    return QueryResult(
        df=pd.DataFrame(),
        error=str(exc),
        bytes_processed=0,
        bytes_billed=0,
        cache_hit=False,
        bytes_processed_known=False,
        bytes_billed_known=False,
    )

  # A cached rerun of an already-seen run costs nothing: report it as a
  # cache hit with nothing billed instead of billing the same job twice in
  # the footer.
  with _RUN_ID_LOCK:
    if run_id in _SEEN_RUN_IDS:
      return QueryResult(
          df,
          None,
          bytes_processed,
          0,
          True,
          bytes_processed_known=proc_known,
          bytes_billed_known=True,
      )
    if len(_SEEN_RUN_IDS) >= _MAX_SEEN_RUN_IDS:
      evict_count = max(1, _MAX_SEEN_RUN_IDS // 4)
      for stale in sorted(_SEEN_RUN_IDS)[:evict_count]:
        _SEEN_RUN_IDS.discard(stale)
    _SEEN_RUN_IDS.add(run_id)

  return QueryResult(
      df,
      None,
      bytes_processed,
      bytes_billed,
      cache_hit,
      bytes_processed_known=proc_known,
      bytes_billed_known=bill_known,
  )


def fetch_bqca(
    sql: str,
    params: tuple[BqcaParam, ...],
    ctx: Context,
    label: str,
) -> QueryResult:
  """Runs a BQCA query and records its cost in this rerun's scan log.

  Every BQCA panel reaches BigQuery through this one function, which is also
  the seam the tests replace.

  Args:
    sql: The SQL query to execute.
    params: Bound query parameters.
    ctx: Active execution context.
    label: Human-readable label for scan logging and error reporting.

  Returns:
    The ``QueryResult`` of the execution.
  """
  with st.spinner(f"Loading {label}..."):
    result = run_bqca_query(sql, params, ctx.refs.project, ctx.max_bytes)
  ctx.scan_log.append(
      ScanEntry(
          label=label,
          bytes_billed=result.bytes_billed or 0,
          bytes_processed=result.bytes_processed or 0,
          cache_hit=result.cache_hit,
          bytes_billed_known=result.bytes_billed_known,
          bytes_processed_known=result.bytes_processed_known,
      )
  )
  if result.error:
    st.error(f"**{label}** — {result.error}")
  return result


# Panel name -> (builder, scan-log label).
PANELS: dict[str, tuple[Callable[..., str], str]] = {
    "filter_options": (build_bqca_filter_options_sql, "Filter options"),
    "kpis": (build_bqca_kpis_sql, "KPIs"),
    "turn_volume": (build_bqca_turn_volume_sql, "Turn volume"),
    "latency": (build_bqca_latency_sql, "Latency"),
    "token_usage": (build_bqca_token_usage_sql, "Token usage"),
    "data_agents": (build_bqca_data_agent_breakdown_sql, "Data agents"),
    "personas": (build_bqca_persona_breakdown_sql, "Personas"),
    "embedding": (
        build_bqca_embedding_suggestions_sql,
        "Embedding suggestions",
    ),
    "turns": (build_bqca_conversation_turns_sql, "Turns"),
    "timeline": (build_bqca_turn_timeline_sql, "Turn timeline"),
    "errors": (build_bqca_error_attribution_sql, "Error attribution"),
}


def fetch_panel(
    panel: str, f: BqcaFilterState, ctx: Context, **kwargs: Any
) -> QueryResult:
  """Builds, binds and runs one named panel's query.

  Args:
    panel: A key of ``PANELS``.
    f: Active filters and connection.
    ctx: Active execution context; its window is the query window.
    **kwargs: Builder arguments: ``limit`` for the capped panels,
      ``invocation_id`` for ``"timeline"``.

  Returns:
    The ``QueryResult`` of the execution.

  Raises:
    KeyError: If ``panel`` is not a known panel name.
  """
  builder, label = PANELS[panel]
  sql = builder(f, window=ctx.window, **kwargs)
  scalars = (
      {"invocation_id": str(kwargs["invocation_id"]).strip()}
      if "invocation_id" in kwargs
      else {}
  )
  params = bqca_query_params(f, sql, **scalars)
  return fetch_bqca(sql, params, ctx, label)


def load_bqca_filter_options(
    f: BqcaFilterState, ctx: Context
) -> tuple[dict[str, list[str]], QueryResult]:
  """Fetches every sidebar option list in one query.

  Args:
    f: Active connection.
    ctx: Active execution context.

  Returns:
    A tuple of a map from kind (``data_agent_id``, ``persona``,
    ``event_type``) to its values, and the query result.
  """
  result = fetch_panel("filter_options", f, ctx)
  options: dict[str, list[str]] = {}
  if result.df.empty:
    return options, result
  for kind, group in result.df.groupby("kind"):
    options[str(kind)] = [str(v) for v in group["value"].tolist()]
  return options, result
