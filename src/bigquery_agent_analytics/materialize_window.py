# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Time-window-driven graph refresh.

The customer-facing entry point for cron-scheduled materialization.
``ontology-build`` expects an explicit ``--session-ids`` list; this
module takes a time window and discovers the sessions itself.

Customer model: "materialize the last N hours". Operator wires a
Cloud Scheduler trigger to a Cloud Run Job that invokes:

    bqaa-materialize-window \\
        --project-id P --dataset-id D \\
        --ontology O.yaml --binding B.yaml \\
        --lookback-hours 6 --completion-event-type AGENT_COMPLETED \\
        --state-table P.D._bqaa_materialization_state

Design contract (per #161):

* **Pinned ``run_started_at``** at entry; both discovery and state
  writes see the same snapshot of "now". Prevents the discovery
  query's row set from drifting across the run.
* **Append-only state table** keyed on a content-derived
  ``state_key`` (sha256 of project + dataset + graph_name +
  events_table + ontology_fingerprint + binding_fingerprint +
  discovery_mode, where ``discovery_mode`` is
  ``terminal:<event_type>`` for normal cron runs and ``active``
  for ``--include-active-sessions`` debug runs). A config change
  — including a swap of ``--completion-event-type`` or a debug
  mode flip — auto-invalidates the previous checkpoint so the
  new predicate's run cannot inherit the old predicate's high-
  water mark.
* **Terminal-event-driven discovery**: query directly for events
  with ``event_type = @completion_event_type`` in the
  ``[scan_start, scan_end)`` window. Partition pruning falls out
  automatically because the predicate is on the partition column.
* **Per-session loop with checkpoint advance**: on each session
  success, the checkpoint moves to that session's completion
  timestamp. Partial failure leaves a tight high-water mark for
  the next run.
* **At-least-once with idempotent retries**: session-level
  delete-then-insert in the materializer means re-processing a
  session is safe; the ``--overlap-minutes`` window re-claims
  late-arriving events.
* **C2 outcome counts** use the runtime-registry names
  (``compiled_unchanged`` / ``compiled_filtered`` /
  ``fallback_for_event``) — operators reading the JSON report
  cross-reference them with the same names in extractor-
  compilation telemetry.

The CLI is a thin wrapper (``cli.materialize_window``); orchestration
lives here so it's unit-testable without Typer.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import hashlib
import json
import pathlib
import re
import secrets
import traceback
from typing import Any, Callable, Optional, Sequence

from ._streaming_evaluation import compute_scan_start
from ._streaming_evaluation import DEFAULT_OVERLAP_MINUTES

# ------------------------------------------------------------------ #
# Constants                                                            #
# ------------------------------------------------------------------ #


# Default completion-event predicate. The BQ AA plugin emits
# ``AGENT_COMPLETED`` as the terminal event for an agent invocation;
# other plugins / emitters can override via ``--completion-event-type``.
DEFAULT_COMPLETION_EVENT_TYPE = "AGENT_COMPLETED"

# Default state-table local name (relative to ``--dataset-id``).
# Picked with a leading underscore so it sorts away from user
# tables; same convention as other SDK-internal artifacts.
DEFAULT_STATE_TABLE_NAME = "_bqaa_materialization_state"

# Default property-graph name when the binding's ``ontology`` field
# is the spec name. ``ontology-build`` falls back to the same name
# unless ``--graph-name`` overrides it.
_DEFAULT_GRAPH_NAME_SENTINEL = "<from-spec>"


_STATE_TABLE_DDL = """\
CREATE TABLE IF NOT EXISTS `{table_ref}` (
  state_key STRING NOT NULL,
  run_id STRING NOT NULL,
  run_started_at TIMESTAMP NOT NULL,
  scan_start TIMESTAMP NOT NULL,
  scan_end TIMESTAMP NOT NULL,
  last_completion_at TIMESTAMP,
  sessions_discovered INT64,
  sessions_materialized INT64,
  sessions_failed INT64,
  ok BOOL NOT NULL,
  error_detail STRING,
  report_json STRING,
  mode STRING,
  orphan_watermark TIMESTAMP,
  flagged_session_ids ARRAY<STRING>
)
PARTITION BY DATE(run_started_at)
CLUSTER BY state_key, run_started_at
"""

# Additive schema migrations. Older state tables were created
# without these columns; running ``ALTER TABLE ... ADD COLUMN IF
# NOT EXISTS`` on every ``ensure_state_table`` call is idempotent
# in BigQuery and brings pre-existing tables up to the current
# schema without a destructive migration. Reads of the migrated
# columns on rows written by older SDK versions return ``NULL``;
# callers default missing values per-column:
#
# * ``mode`` -> ``STATE_MODE_STEADY`` (issue #177, backfill follow-up).
# * ``orphan_watermark`` -> ``None`` (issue #180, orphan watchdog).
# * ``flagged_session_ids`` -> ``[]`` (issue #180).
_STATE_TABLE_MODE_MIGRATIONS = (
    "ALTER TABLE `{table_ref}` ADD COLUMN IF NOT EXISTS mode STRING",
    "ALTER TABLE `{table_ref}` ADD COLUMN IF NOT EXISTS orphan_watermark TIMESTAMP",
    (
        "ALTER TABLE `{table_ref}` ADD COLUMN IF NOT EXISTS"
        " flagged_session_ids ARRAY<STRING>"
    ),
)

STATE_MODE_STEADY = "steady"
STATE_MODE_BACKFILL = "backfill"
# Orphan watchdog modes (issue #180).
#
# ``orphan_scan`` is a per-scan audit row — one row per cron pass
# when ``max_session_age_hours`` is enabled. ``sessions_discovered``
# holds the count of orphan sessions newly flagged in THIS scan,
# ``flagged_session_ids`` holds those new session_ids, and
# ``orphan_watermark`` holds the upper bound (``cutoff_at``) of the
# scan window so the next pass uses strict ``>`` on the same column.
#
# ``orphan_ledger`` is the cumulative ledger row written alongside
# every ``orphan_scan``. ``flagged_session_ids`` holds the running
# set of all unresolved orphan session_ids known for this state_key.
# Append-only by run, with reads picking the most recent row per
# state_key — same shape as the steady checkpoint, so a future
# ``orphan_scan`` filters its discovery against the read-back
# ledger's flagged set even on scans that find zero new orphans.
STATE_MODE_ORPHAN_SCAN = "orphan_scan"
STATE_MODE_ORPHAN_LEDGER = "orphan_ledger"

# Extraction modes (B2, issue #178 follow-up).
#
# ``ai-fallback`` is the default and the existing behavior: structured
# extractors run when registered, ``AI.GENERATE`` fills any gaps. The
# call into :func:`OntologyGraphManager.extract_graph` stays on the
# legacy bool surface so existing call-stacks are unaffected.
#
# ``compiled-only`` opts into B1's orthogonal-flag surface with
# ``on_unhandled_span="fail"``: structured extractors run, ``AI.GENERATE``
# is NOT called, and unhandled spans surface as
# ``ExtractionDiagnostic`` codes (``structured_unhandled`` or
# ``extractor_exception``). The orchestrator translates those codes
# into a typed ``empty_extraction`` failure on the session ahead of
# the materializer's row-count classifier so the failure surface
# carries the diagnostic samples directly. Customers running with
# this mode can drop ``roles/aiplatform.user`` from the runtime
# service account (zero AI calls is asserted by a covering test).
EXTRACTION_MODE_AI_FALLBACK = "ai-fallback"
EXTRACTION_MODE_COMPILED_ONLY = "compiled-only"
_VALID_EXTRACTION_MODES = frozenset(
    {EXTRACTION_MODE_AI_FALLBACK, EXTRACTION_MODE_COMPILED_ONLY}
)


# Identifier validation for BQ identifiers we interpolate into SQL.
# Mirrors ``bq_bundle_mirror._TABLE_ID_PATTERN``. Each segment of
# ``project.dataset.table`` is constrained to ASCII letters/digits
# plus ``_`` and ``-`` (project IDs can carry hyphens).
_BQ_SEGMENT_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
# Same shape, but two segments — used for the state table's
# ``dataset.table`` short form when the user opts not to qualify it.
_BQ_IDENT_LOOSE = re.compile(r"^[A-Za-z0-9_-]+(\.[A-Za-z0-9_-]+){0,2}$")


# ------------------------------------------------------------------ #
# Data shapes                                                          #
# ------------------------------------------------------------------ #


@dataclasses.dataclass(frozen=True)
class DiscoveredSession:
  """One session-id returned by the discovery query."""

  session_id: str
  completion_timestamp: _dt.datetime


@dataclasses.dataclass(frozen=True)
class SessionResult:
  """Outcome of materializing one session."""

  session_id: str
  ok: bool
  completion_timestamp: _dt.datetime
  rows_materialized: dict[str, int] = dataclasses.field(default_factory=dict)
  table_statuses: dict[str, dict[str, Any]] = dataclasses.field(
      default_factory=dict
  )
  error_code: Optional[str] = None
  error_detail: Optional[str] = None


@dataclasses.dataclass(frozen=True)
class StateRow:
  """Row written to the checkpoint state table.

  ``last_completion_at`` is the watermark for the next run's
  ``compute_scan_start`` lower bound. On partial failure it's the
  MAX completion timestamp among *successfully* materialized
  sessions — never advancing past a failure.

  ``mode`` identifies the orchestrator mode that wrote the row:
  :data:`STATE_MODE_STEADY` for normal cron-driven advances,
  :data:`STATE_MODE_BACKFILL` for ``--backfill --from/--to`` runs.
  Backfill rows are written under a distinct ``state_key`` (via the
  ``--state-key-suffix`` plumbing) so they never advance the
  steady-state checkpoint; the ``mode`` column is the audit-trail
  signal that distinguishes the two streams when an operator joins
  them in a single query.
  """

  state_key: str
  run_id: str
  run_started_at: _dt.datetime
  scan_start: _dt.datetime
  scan_end: _dt.datetime
  last_completion_at: Optional[_dt.datetime]
  sessions_discovered: int
  sessions_materialized: int
  sessions_failed: int
  ok: bool
  error_detail: Optional[str] = None
  report_json: Optional[str] = None
  mode: str = STATE_MODE_STEADY
  # Orphan-watchdog fields (issue #180). Populated only for rows
  # whose ``mode`` is :data:`STATE_MODE_ORPHAN_SCAN` (the per-scan
  # audit row carries that scan's newly-flagged session_ids and the
  # cutoff watermark) or :data:`STATE_MODE_ORPHAN_LEDGER` (the
  # cumulative ledger carries the running set of all unresolved
  # orphan session_ids and the advancing scan watermark). Left at
  # the dataclass defaults for steady / backfill rows.
  orphan_watermark: Optional[_dt.datetime] = None
  flagged_session_ids: Optional[tuple[str, ...]] = None


@dataclasses.dataclass(frozen=True)
class MaterializeWindowResult:
  """Full JSON-serializable report from one run."""

  run_id: str
  state_key: str
  window_start: _dt.datetime
  window_end: _dt.datetime
  checkpoint_read: Optional[_dt.datetime]
  checkpoint_written: Optional[_dt.datetime]
  sessions_discovered: int
  sessions_materialized: int
  sessions_failed: int
  rows_materialized: dict[str, int]
  table_statuses: dict[str, dict[str, Any]]
  compiled_outcomes: dict[str, int]
  failures: list[dict[str, Any]]
  ok: bool
  # Bundle fingerprint resolved from ``--bundles-root`` (only
  # set when the compiled-bundle path is wired). Operators
  # reading ``compiled_outcomes`` cross-reference with this to
  # confirm which bundle ran.
  compile_bundle_fingerprint: Optional[str] = None
  # Orphan-watchdog result (issue #180). Populated only when the
  # caller passes ``max_session_age_hours``. ``None`` means the
  # watchdog was disabled for this run (default). The string
  # forward-reference resolves at runtime because the actual class
  # is declared further down in the module.
  orphan_result: Optional["OrphanScanResult"] = None

  def to_json(self) -> dict[str, Any]:
    """JSON-serializable dict (timestamps → ISO 8601 UTC strings)."""
    payload = {
        "run_id": self.run_id,
        "state_key": self.state_key,
        "window_start": _iso(self.window_start),
        "window_end": _iso(self.window_end),
        "checkpoint_read": _iso_optional(self.checkpoint_read),
        "checkpoint_written": _iso_optional(self.checkpoint_written),
        "sessions_discovered": self.sessions_discovered,
        "sessions_materialized": self.sessions_materialized,
        "sessions_failed": self.sessions_failed,
        "rows_materialized": dict(self.rows_materialized),
        "table_statuses": dict(self.table_statuses),
        "compiled_outcomes": dict(self.compiled_outcomes),
        "failures": list(self.failures),
        "ok": self.ok,
        "compile_bundle_fingerprint": self.compile_bundle_fingerprint,
    }
    if self.orphan_result is not None:
      payload["orphan"] = {
          "cutoff_at": _iso(self.orphan_result.cutoff_at),
          "previous_watermark": _iso_optional(
              self.orphan_result.previous_watermark
          ),
          "new_orphans": list(self.orphan_result.new_orphans),
          "resolved_orphans": list(self.orphan_result.resolved_orphans),
          "cumulative_flagged": list(self.orphan_result.cumulative_flagged),
      }
    return payload


# ------------------------------------------------------------------ #
# Pure helpers                                                         #
# ------------------------------------------------------------------ #


def compute_state_key(
    *,
    project_id: str,
    dataset_id: str,
    graph_name: str,
    events_table: str,
    ontology_fingerprint: str,
    binding_fingerprint: str,
    discovery_mode: str,
    state_key_suffix: Optional[str] = None,
) -> str:
  """Content-derived hex key for the state table.

  Stable across re-runs against the same config. A change to ANY
  of the inputs (e.g. binding rename, new event source, ontology
  bump, terminal event swap) produces a new key, so the prior
  checkpoint is *implicitly* invalidated. Operators don't need to
  manage a versioning column by hand — the SDK's existing
  fingerprint helpers already canonicalize the model contents, so
  equivalent YAML (different whitespace, key order) produces the
  same fingerprint and the same state key.

  ``discovery_mode`` is one of ``terminal:<event_type>`` (normal
  cron run) or ``active`` (``--include-active-sessions`` debug
  mode). Including it in the key prevents two regressions:

  * Operator switches from ``AGENT_COMPLETED`` to a custom terminal
    event — the new predicate's run would otherwise inherit the
    old predicate's high-water mark and skip historical
    completions for the new event type.
  * Debug run with ``--include-active-sessions`` shares state with
    the production cron — the debug run discovers different
    sessions (it has no terminal-event filter) and could advance
    the production checkpoint past sessions production hasn't
    seen as completed yet.

  ``state_key_suffix`` (optional) is folded into the same hash. It
  exists so backfill or ad-hoc re-extraction runs can carve out a
  separate state-key namespace from the steady-state cron — the
  backfill writes its own ``_bqaa_materialization_state`` rows
  without polluting the steady checkpoint stream. When unset, the
  hash is byte-identical to the prior (suffix-less) computation,
  so existing state keys do not drift.
  """
  components = [
      project_id,
      dataset_id,
      graph_name,
      events_table,
      ontology_fingerprint,
      binding_fingerprint,
      discovery_mode,
  ]
  if state_key_suffix:
    components.append(f"suffix:{state_key_suffix}")
  payload = "\x00".join(components).encode("utf-8")
  return hashlib.sha256(payload).hexdigest()


def generate_run_id() -> str:
  """ULID-ish: 12 hex chars from a fresh random source. Globally
  unique without requiring callers to pass a clock-driven seed.
  Sortable-by-time is not a property we need — the state table
  already partitions on ``run_started_at``."""
  return secrets.token_hex(6)


def validated_table_ref(
    project_id: str,
    dataset_id: str,
    table: str,
) -> str:
  """Validate each segment + return the BQ-quoted FQN.

  Backtick-wraps when called from SQL builders. The check is the
  same per-segment regex used by other CLI surfaces; it rejects
  whitespace, backticks, semicolons, dots inside a segment, etc.
  """
  for label, value in (
      ("project_id", project_id),
      ("dataset_id", dataset_id),
      ("table", table),
  ):
    if not isinstance(value, str) or not _BQ_SEGMENT_PATTERN.fullmatch(value):
      raise ValueError(
          f"--{label.replace('_', '-')} {value!r} is not a well-formed "
          f"BigQuery identifier segment (allowed: ASCII letters, digits, "
          f"'_', '-'; no whitespace, backticks, semicolons, or comment "
          f"markers)"
      )
  return f"{project_id}.{dataset_id}.{table}"


def parse_state_table_ref(
    raw: str, default_project: str, default_dataset: str
) -> tuple[str, str, str]:
  """Accept ``table`` / ``dataset.table`` / ``project.dataset.table``;
  fill from defaults; validate each segment.

  Returns ``(project, dataset, table)`` with the same identifier
  guarantees ``validated_table_ref`` enforces.
  """
  if not isinstance(raw, str) or not _BQ_IDENT_LOOSE.fullmatch(raw):
    raise ValueError(
        f"--state-table {raw!r} must be ``[project.[dataset.]]table`` "
        f"(allowed: ASCII letters/digits/'_'/'-' per segment)"
    )
  parts = raw.split(".")
  if len(parts) == 1:
    project, dataset, table = default_project, default_dataset, parts[0]
  elif len(parts) == 2:
    project, (dataset, table) = default_project, parts
  else:
    project, dataset, table = parts
  validated_table_ref(project, dataset, table)
  return project, dataset, table


def build_discovery_sql(
    *,
    events_table_ref: str,
    completion_event_type: str,
    max_sessions: Optional[int] = None,
) -> str:
  """Discovery query: terminal events in ``[scan_start, scan_end)``.

  Partition pruning falls out because the predicate on
  ``timestamp`` is the partition column for the BQ AA plugin's
  table. The ``MAX(timestamp)`` per ``session_id`` is the
  session's completion watermark (one terminal event per session
  in normal traffic; we coalesce duplicates).

  Parameters are bound at query time so the SQL string itself is
  static and safe to interpolate the FQN into.
  """
  events_table_ref_quoted = "`" + events_table_ref + "`"
  limit_clause = (
      f"\n      LIMIT {int(max_sessions)}" if max_sessions is not None else ""
  )
  return (
      f"SELECT\n"
      f"  session_id,\n"
      f"  MAX(timestamp) AS completion_timestamp\n"
      f"FROM {events_table_ref_quoted}\n"
      f"WHERE timestamp >= @scan_start\n"
      f"  AND timestamp < @scan_end\n"
      f"  AND event_type = @completion_event_type\n"
      f"  AND session_id IS NOT NULL\n"
      f"GROUP BY session_id\n"
      f"ORDER BY completion_timestamp{limit_clause}\n"
  )


def build_state_select_sql(state_table_ref: str) -> str:
  """Highest-watermark state row for a given ``state_key``.

  Earlier round filtered on non-NULL ``last_completion_at`` and
  ordered by ``run_started_at DESC``. That was insufficient for
  two interacting cases:

  * **Carry-forward rows write the prior checkpoint** (not NULL)
    on failure / empty-window, so the non-NULL filter alone no
    longer separates "real advance" from "no advance".
  * **Overlapping runs**: if a later run somehow recorded an
    *older* ``last_completion_at`` than an earlier run (e.g. an
    out-of-order rerun against the same state-key), ordering by
    ``run_started_at`` would shadow the higher watermark.

  Ordering by ``last_completion_at DESC, run_started_at DESC``
  picks the highest watermark first and breaks ties by recency.
  The non-NULL filter is retained for defense-in-depth — older
  state rows pre-carry-forward may still carry NULLs, and we
  never want one of those to win the ordering."""
  state_table_ref_quoted = "`" + state_table_ref + "`"
  return (
      f"SELECT\n"
      f"  state_key,\n"
      f"  run_id,\n"
      f"  run_started_at,\n"
      f"  scan_start,\n"
      f"  scan_end,\n"
      f"  last_completion_at,\n"
      f"  sessions_discovered,\n"
      f"  sessions_materialized,\n"
      f"  sessions_failed,\n"
      f"  ok,\n"
      f"  error_detail\n"
      f"FROM {state_table_ref_quoted}\n"
      f"WHERE state_key = @state_key\n"
      f"  AND last_completion_at IS NOT NULL\n"
      f"ORDER BY last_completion_at DESC, run_started_at DESC\n"
      f"LIMIT 1\n"
  )


# ------------------------------------------------------------------ #
# State table I/O (DDL + read + append)                                #
# ------------------------------------------------------------------ #


def ensure_state_table(bq_client: Any, state_table_ref: str) -> None:
  """Create the state table if it doesn't exist + apply additive
  schema migrations. Idempotent.

  Newer SDK versions add columns to the state table (currently:
  ``mode``). The ``ALTER TABLE ... ADD COLUMN IF NOT EXISTS``
  statements below run every time and bring pre-existing tables up
  to the current schema without a destructive migration. Reads of
  the migrated column on rows written by older SDK versions return
  ``NULL``; callers default missing values to ``STATE_MODE_STEADY``.
  """
  bq_client.query(_STATE_TABLE_DDL.format(table_ref=state_table_ref)).result()
  for migration in _STATE_TABLE_MODE_MIGRATIONS:
    bq_client.query(migration.format(table_ref=state_table_ref)).result()


def read_last_checkpoint(
    bq_client: Any, state_table_ref: str, state_key: str
) -> Optional[_dt.datetime]:
  """Return the ``last_completion_at`` of the most recent row for
  this state-key, or ``None`` if no row exists yet (bootstrap).

  Note: we read ``last_completion_at`` (the terminal-event
  high-water mark), not ``run_started_at``. The next-run lower
  bound advances only as far as we've actually materialized.
  """
  from google.cloud import bigquery  # local import: optional dep at module load

  rows = list(
      bq_client.query(
          build_state_select_sql(state_table_ref),
          job_config=bigquery.QueryJobConfig(
              query_parameters=[
                  bigquery.ScalarQueryParameter(
                      "state_key", "STRING", state_key
                  ),
              ]
          ),
      ).result()
  )
  if not rows:
    return None
  return rows[0].last_completion_at


def append_state_row(
    bq_client: Any, state_table_ref: str, row: StateRow
) -> None:
  """Append a state row. Uses ``insert_rows_json`` (streaming
  insert) — cheaper than an INSERT job for tiny single-row writes."""
  payload = {
      "state_key": row.state_key,
      "run_id": row.run_id,
      "run_started_at": _iso(row.run_started_at),
      "scan_start": _iso(row.scan_start),
      "scan_end": _iso(row.scan_end),
      "last_completion_at": _iso_optional(row.last_completion_at),
      "sessions_discovered": row.sessions_discovered,
      "sessions_materialized": row.sessions_materialized,
      "sessions_failed": row.sessions_failed,
      "ok": row.ok,
      "error_detail": row.error_detail,
      "report_json": row.report_json,
      "mode": row.mode,
      "orphan_watermark": _iso_optional(row.orphan_watermark),
      # ``insert_rows_json`` accepts a list of strings for an
      # ARRAY<STRING> column. ``None`` is encoded as a NULL ARRAY
      # at the column level — distinct from the empty array which
      # signals "scan ran, zero unresolved orphans".
      "flagged_session_ids": (
          list(row.flagged_session_ids)
          if row.flagged_session_ids is not None
          else None
      ),
  }
  errors = bq_client.insert_rows_json(state_table_ref, [payload])
  if errors:
    raise RuntimeError(f"insert into {state_table_ref} failed: {errors!r}")


# ------------------------------------------------------------------ #
# Orphan watchdog (issue #180)                                          #
# ------------------------------------------------------------------ #
#
# Three SQL helpers + one orchestrator-level scan function. The
# helpers are kept as pure SQL-builders so tests can pin the
# query shape without round-tripping through a fake BigQuery
# client, and so the discovery query is auditable.
#
# The watchdog model has three durable shapes in the state table:
#
# 1. ``orphan_ledger`` — one most-recent row per ``state_key``,
#    carrying the cumulative set of unresolved orphan session_ids
#    in ``flagged_session_ids`` and the upper-bound scan watermark
#    in ``orphan_watermark``.
# 2. ``orphan_scan`` — one row per cron pass, carrying that scan's
#    *newly* flagged session_ids (the delta) and the same watermark.
# 3. ``ok=false`` failures threaded into the regular run's
#    ``failures[]`` with ``error_code='session_orphaned'`` so
#    existing Cloud Monitoring alerts on the failure surface fire
#    immediately — no separate alert wiring per #167's classifier.


@dataclasses.dataclass(frozen=True)
class OrphanLedger:
  """Read-side view of the most recent ``orphan_ledger`` row."""

  orphan_watermark: Optional[_dt.datetime]
  flagged_session_ids: tuple[str, ...]

  @classmethod
  def empty(cls) -> "OrphanLedger":
    """Initial state for a fresh deploy / state_key never scanned."""
    return cls(orphan_watermark=None, flagged_session_ids=())


@dataclasses.dataclass(frozen=True)
class OrphanScanResult:
  """Result of one orphan-watchdog scan."""

  cutoff_at: _dt.datetime
  previous_watermark: Optional[_dt.datetime]
  new_orphans: tuple[str, ...]
  resolved_orphans: tuple[str, ...]
  cumulative_flagged: tuple[str, ...]


def build_orphan_select_sql(events_table_ref: str) -> str:
  """SQL: sessions whose ``MIN(timestamp)`` falls in
  ``(@orphan_watermark, @cutoff_at]`` and which never emitted
  ``AGENT_COMPLETED``.

  ``@orphan_watermark`` uses strict ``>`` so the boundary session
  from the previous scan isn't reconsidered — matches the
  steady checkpoint's exclusive-lower-bound semantics. ``@cutoff_at``
  is the inclusive upper bound (the orchestrator computes it as
  ``now - max_session_age_hours`` so anything more recent has had
  enough time to emit a terminal event).

  The discovery is by ``MIN(timestamp)`` (first event) rather than
  ``MAX``: an in-flight session that started before the cutoff but
  is still emitting events is the orphan-watchdog's concern; a
  session whose newest event is before the cutoff but which started
  even earlier was already covered by a prior scan.

  ``@already_flagged`` (the previous ledger's cumulative set) is
  excluded with ``NOT IN UNNEST(...)`` so the per-scan ``orphan_scan``
  row records only the delta — operators reading the audit trail
  see "how many new orphans appeared this scan" without having to
  diff against the previous scan's set themselves. The
  ``orphan_ledger`` row written alongside ``orphan_scan`` carries
  the full cumulative set after the resolve step.
  """
  return f"""\
WITH session_envelope AS (
  SELECT
    session_id,
    MIN(timestamp) AS first_event_at,
    COUNTIF(event_type = @completion_event_type) AS terminal_event_count
  FROM `{events_table_ref}`
  WHERE timestamp > COALESCE(@orphan_watermark, TIMESTAMP('1970-01-01'))
    AND timestamp <= @cutoff_at
  GROUP BY session_id
)
SELECT session_id, first_event_at
FROM session_envelope
WHERE terminal_event_count = 0
  AND session_id NOT IN UNNEST(@already_flagged)
ORDER BY first_event_at, session_id
"""


def build_resolved_orphan_sql(events_table_ref: str) -> str:
  """SQL: subset of the previously-flagged orphan session_ids
  that have since emitted the configured terminal event.

  Run after the discovery query so the orchestrator can drop
  resolved orphans from the cumulative ledger. The "resolved"
  signal is real-world: a late terminal event arrived, the
  steady cron will pick the session up on its next pass, and the
  watchdog's running ledger should not keep flagging it as
  unresolved.

  Two predicates jointly bound the scan cost:

  * ``session_id IN UNNEST(@previously_flagged)`` keeps the row
    fan-out small even on huge events tables.
  * ``timestamp > @resolve_lower_bound AND timestamp <=
    @resolve_upper_bound`` enables partition pruning on the
    plugin's timestamp-partitioned table. Without it, BigQuery
    would scan every partition once the ledger held any flagged
    session — turning the watchdog into a query-byte regression
    as the customer's event history grows.

  The orchestrator passes ``resolve_lower_bound`` =
  ``previous_orphan_watermark`` (advances every scan; events
  before that cutoff were either resolved earlier or never had
  a terminal event in scope) and ``resolve_upper_bound`` =
  ``run_started_at``. Late terminal events whose ``timestamp``
  is more than ``max_session_age_hours`` in the past
  (theoretically possible if an agent emits with a stale
  emission time) won't be picked up — operators with that
  pattern should widen the bound, but the common case keeps the
  partition predicate tight.
  """
  return f"""\
SELECT DISTINCT session_id
FROM `{events_table_ref}`
WHERE session_id IN UNNEST(@previously_flagged)
  AND event_type = @completion_event_type
  AND timestamp > @resolve_lower_bound
  AND timestamp <= @resolve_upper_bound
"""


def build_orphan_ledger_select_sql(state_table_ref: str) -> str:
  """Most recent ``orphan_ledger`` row for a given ``state_key``.

  Mirrors :func:`build_state_select_sql` (mode='steady') but
  filters on ``mode = 'orphan_ledger'`` so the steady checkpoint
  doesn't interfere — they share the same state_key but live in
  disjoint row groups.

  Returns ``flagged_session_ids`` (ARRAY<STRING>) and
  ``orphan_watermark`` (TIMESTAMP). The
  :class:`OrphanLedger.empty` factory is used when no row exists
  yet (fresh deploy / state_key never scanned).
  """
  state_table_ref_quoted = "`" + state_table_ref + "`"
  return (
      f"SELECT\n"
      f"  state_key,\n"
      f"  run_started_at,\n"
      f"  orphan_watermark,\n"
      f"  flagged_session_ids\n"
      f"FROM {state_table_ref_quoted}\n"
      f"WHERE state_key = @state_key\n"
      f"  AND mode = '{STATE_MODE_ORPHAN_LEDGER}'\n"
      f"ORDER BY run_started_at DESC\n"
      f"LIMIT 1\n"
  )


def read_orphan_ledger(
    bq_client: Any, state_table_ref: str, state_key: str
) -> OrphanLedger:
  """Load the most recent ``orphan_ledger`` row for ``state_key``,
  or :meth:`OrphanLedger.empty` when none exists."""
  from google.cloud import bigquery

  rows = list(
      bq_client.query(
          build_orphan_ledger_select_sql(state_table_ref),
          job_config=bigquery.QueryJobConfig(
              query_parameters=[
                  bigquery.ScalarQueryParameter(
                      "state_key", "STRING", state_key
                  ),
              ]
          ),
      ).result()
  )
  if not rows:
    return OrphanLedger.empty()
  row = rows[0]
  flagged_raw = getattr(row, "flagged_session_ids", None) or ()
  return OrphanLedger(
      orphan_watermark=getattr(row, "orphan_watermark", None),
      flagged_session_ids=tuple(flagged_raw),
  )


def discover_orphan_sessions(
    bq_client: Any,
    events_table_ref: str,
    *,
    orphan_watermark: Optional[_dt.datetime],
    cutoff_at: _dt.datetime,
    already_flagged: tuple[str, ...],
    completion_event_type: str,
) -> tuple[str, ...]:
  """Query for new orphan session_ids in
  ``(orphan_watermark, cutoff_at]``.

  ``completion_event_type`` mirrors ``run_materialize_window``'s
  ``--completion-event-type`` flag — the terminal-event name the
  watchdog scans for. Defaults flow from the orchestrator so a
  deploy configured for a custom terminal event (e.g.
  ``MY_TERMINAL``) doesn't get false ``session_orphaned``
  failures the steady cron just successfully materialized.
  """
  from google.cloud import bigquery

  rows = list(
      bq_client.query(
          build_orphan_select_sql(events_table_ref),
          job_config=bigquery.QueryJobConfig(
              query_parameters=[
                  bigquery.ScalarQueryParameter(
                      "orphan_watermark", "TIMESTAMP", orphan_watermark
                  ),
                  bigquery.ScalarQueryParameter(
                      "cutoff_at", "TIMESTAMP", cutoff_at
                  ),
                  bigquery.ArrayQueryParameter(
                      "already_flagged", "STRING", list(already_flagged)
                  ),
                  bigquery.ScalarQueryParameter(
                      "completion_event_type",
                      "STRING",
                      completion_event_type,
                  ),
              ]
          ),
      ).result()
  )
  return tuple(r.session_id for r in rows)


def probe_resolved_orphans(
    bq_client: Any,
    events_table_ref: str,
    *,
    previously_flagged: tuple[str, ...],
    completion_event_type: str,
    resolve_lower_bound: _dt.datetime,
    resolve_upper_bound: _dt.datetime,
) -> tuple[str, ...]:
  """Subset of ``previously_flagged`` that now has terminal events
  in ``(resolve_lower_bound, resolve_upper_bound]``.

  The timestamp bounds let BigQuery prune partitions on the
  plugin's timestamp-partitioned events table. Without them, the
  probe scans every partition once the ledger has any flagged
  session — turning the watchdog into a query-byte regression as
  event history grows.
  """
  if not previously_flagged:
    return ()
  from google.cloud import bigquery

  rows = list(
      bq_client.query(
          build_resolved_orphan_sql(events_table_ref),
          job_config=bigquery.QueryJobConfig(
              query_parameters=[
                  bigquery.ArrayQueryParameter(
                      "previously_flagged",
                      "STRING",
                      list(previously_flagged),
                  ),
                  bigquery.ScalarQueryParameter(
                      "completion_event_type",
                      "STRING",
                      completion_event_type,
                  ),
                  bigquery.ScalarQueryParameter(
                      "resolve_lower_bound",
                      "TIMESTAMP",
                      resolve_lower_bound,
                  ),
                  bigquery.ScalarQueryParameter(
                      "resolve_upper_bound",
                      "TIMESTAMP",
                      resolve_upper_bound,
                  ),
              ]
          ),
      ).result()
  )
  return tuple(r.session_id for r in rows)


def run_orphan_watchdog(
    bq_client: Any,
    *,
    events_table_ref: str,
    state_table_ref: str,
    state_key: str,
    run_id: str,
    run_started_at: _dt.datetime,
    max_session_age_hours: float,
    completion_event_type: str = DEFAULT_COMPLETION_EVENT_TYPE,
) -> OrphanScanResult:
  """End-to-end orphan-watchdog scan.

  Reads the most recent ``orphan_ledger`` row, runs the discovery
  query against ``(orphan_watermark, cutoff_at]``, probes for
  late-arriving terminal events on previously-flagged session_ids,
  and writes two state rows:

  * ``orphan_scan`` — audit row for this scan: new orphans only,
    cutoff watermark.
  * ``orphan_ledger`` — cumulative set after the resolve step:
    ``(previous_flagged - resolved) ∪ new_orphans``.

  The cutoff is computed as ``run_started_at - max_session_age_hours``
  and stored as both ``scan_end`` (this scan's audit window upper
  bound) and ``orphan_watermark`` (the next scan's exclusive lower
  bound — strict ``>`` skips the boundary session). The advance is
  unconditional: even if zero new orphans are found, the watermark
  advances so the next scan does less work.

  Returns the result so the orchestrator can thread the new orphans
  into the run's ``failures[]`` as ``session_orphaned`` typed
  failures.
  """
  if max_session_age_hours <= 0:
    raise ValueError(
        f"max_session_age_hours must be > 0; got {max_session_age_hours!r}"
    )
  cutoff_at = run_started_at - _dt.timedelta(hours=max_session_age_hours)

  ledger = read_orphan_ledger(bq_client, state_table_ref, state_key)
  new_orphans = discover_orphan_sessions(
      bq_client,
      events_table_ref,
      orphan_watermark=ledger.orphan_watermark,
      cutoff_at=cutoff_at,
      already_flagged=ledger.flagged_session_ids,
      completion_event_type=completion_event_type,
  )
  # Resolved-orphan probe lower bound: use the previous orphan
  # watermark when present (events written before that cutoff
  # were already in scope of a prior scan); fall back to the
  # epoch for the bootstrap case where the previous ledger never
  # advanced a watermark — when ``previously_flagged`` is empty
  # the probe short-circuits and the bound isn't sent anyway, so
  # this fallback path is only exercised by a future code path
  # that pre-seeds the ledger.
  resolve_lower_bound = ledger.orphan_watermark or _dt.datetime(
      1970, 1, 1, tzinfo=_dt.timezone.utc
  )
  resolved_orphans = probe_resolved_orphans(
      bq_client,
      events_table_ref,
      previously_flagged=ledger.flagged_session_ids,
      completion_event_type=completion_event_type,
      resolve_lower_bound=resolve_lower_bound,
      resolve_upper_bound=run_started_at,
  )

  # Cumulative ledger: drop resolved, union new. Preserve previous
  # declaration order for stable diffs, then append new orphans in
  # the order discovery returned them (sorted by ``first_event_at``).
  resolved_set = set(resolved_orphans)
  carry_forward = tuple(
      s for s in ledger.flagged_session_ids if s not in resolved_set
  )
  cumulative_flagged = carry_forward + new_orphans

  audit_row = StateRow(
      state_key=state_key,
      run_id=run_id,
      run_started_at=run_started_at,
      scan_start=ledger.orphan_watermark
      or _dt.datetime(1970, 1, 1, tzinfo=_dt.timezone.utc),
      scan_end=cutoff_at,
      last_completion_at=None,
      sessions_discovered=len(new_orphans),
      sessions_materialized=0,
      sessions_failed=len(new_orphans),
      ok=len(new_orphans) == 0,
      error_detail=None,
      report_json=None,
      mode=STATE_MODE_ORPHAN_SCAN,
      orphan_watermark=cutoff_at,
      flagged_session_ids=new_orphans,
  )
  ledger_row = StateRow(
      state_key=state_key,
      run_id=run_id,
      run_started_at=run_started_at,
      scan_start=ledger.orphan_watermark
      or _dt.datetime(1970, 1, 1, tzinfo=_dt.timezone.utc),
      scan_end=cutoff_at,
      last_completion_at=None,
      sessions_discovered=len(cumulative_flagged),
      sessions_materialized=0,
      sessions_failed=len(cumulative_flagged),
      ok=len(cumulative_flagged) == 0,
      error_detail=None,
      report_json=None,
      mode=STATE_MODE_ORPHAN_LEDGER,
      orphan_watermark=cutoff_at,
      flagged_session_ids=cumulative_flagged,
  )
  append_state_row(bq_client, state_table_ref, audit_row)
  append_state_row(bq_client, state_table_ref, ledger_row)

  return OrphanScanResult(
      cutoff_at=cutoff_at,
      previous_watermark=ledger.orphan_watermark,
      new_orphans=new_orphans,
      resolved_orphans=resolved_orphans,
      cumulative_flagged=cumulative_flagged,
  )


# ------------------------------------------------------------------ #
# Outcome callback (compiled-extractor telemetry)                       #
# ------------------------------------------------------------------ #


def make_outcome_counter() -> tuple[Callable[[str, Any], None], dict[str, int]]:
  """Returns ``(callback, counts)``.

  Wire ``callback`` into ``OntologyGraphManager.from_bundles_root(
  ..., on_outcome=callback)`` to count C2 fallback decisions in
  the JSON report. ``counts`` accumulates ``compiled_unchanged``
  / ``compiled_filtered`` / ``fallback_for_event`` over the run.
  Unknown decisions land in a catch-all so a future SDK addition
  doesn't silently drop telemetry.
  """
  counts: dict[str, int] = {
      "compiled_unchanged": 0,
      "compiled_filtered": 0,
      "fallback_for_event": 0,
  }

  def _callback(event_type: str, outcome: Any) -> None:
    del event_type  # outcome.decision is the keying field
    decision = getattr(outcome, "decision", None)
    if isinstance(decision, str) and decision:
      counts[decision] = counts.get(decision, 0) + 1
    else:
      counts.setdefault("unknown", 0)
      counts["unknown"] += 1

  return _callback, counts


# ------------------------------------------------------------------ #
# Small datetime helpers                                               #
# ------------------------------------------------------------------ #


def _classify_compiled_only_diagnostics(
    session: Any, graph: Any
) -> Optional[Any]:
  """Translate B1's ``ExtractionDiagnostic`` codes into a typed
  ``empty_extraction`` ``SessionResult`` for compiled-only mode.

  Returns ``None`` when the session's diagnostic stream is clean
  (no ``structured_unhandled`` or ``extractor_exception``) — the
  caller should proceed with materialization. Returns a populated
  ``SessionResult`` otherwise.

  Compiled-only mode treats any ``structured_unhandled`` or
  ``extractor_exception`` count as a session-level failure: the
  whole point of opting into compiled-only is to forbid silent
  AI.GENERATE billing on extractor gaps. Partial coverage is not
  acceptable in this mode; operators wanting "best-effort" should
  use ``ai-fallback``. The translation runs ahead of
  ``materialize_with_status`` so the failure surface carries the
  diagnostic samples directly (specific span_id / event_type) —
  more actionable than the row-count classifier's generic
  "extraction returned empty" string.

  Args:
    session: The ``DiscoveredSession`` being processed (used for
      ``session_id`` / ``completion_timestamp`` on the result).
    graph: The ``ExtractedGraph`` returned by ``extract_graph(...,
      on_unhandled_span="fail")``.

  Returns:
    ``None`` when no unhandled / exception diagnostics fired, or a
    ``SessionResult`` with ``ok=False``, ``error_code='empty_extraction'``,
    and ``error_detail`` carrying up to 10 diagnostic samples for
    log readability.
  """
  unhandled_count = 0
  exception_count = 0
  samples: list[str] = []
  for diag in getattr(graph, "diagnostics", []) or []:
    code = getattr(diag, "diagnostic_code", None)
    if code not in ("structured_unhandled", "extractor_exception"):
      continue
    if code == "structured_unhandled":
      unhandled_count += 1
    else:
      exception_count += 1
    if len(samples) < 10:
      sample = (
          f"{code} span_id={diag.span_id!r} " f"event_type={diag.event_type!r}"
      )
      detail = getattr(diag, "detail", None)
      if detail:
        sample += f" detail={detail!r}"
      samples.append(sample)
  if unhandled_count == 0 and exception_count == 0:
    return None
  total = unhandled_count + exception_count
  more_note = ""
  if total > len(samples):
    more_note = f" (+{total - len(samples)} more not shown)"
  return SessionResult(
      session_id=session.session_id,
      ok=False,
      completion_timestamp=session.completion_timestamp,
      error_code="empty_extraction",
      error_detail=(
          f"compiled-only extraction-mode: "
          f"{unhandled_count} structured_unhandled + "
          f"{exception_count} extractor_exception diagnostic(s) "
          f"fired on this session{more_note}. The compiled "
          "extractor set does not cover every event type seen "
          "here. To resolve, either extend the compiled "
          "extractors (preferred) or re-run with "
          "``--extraction-mode=ai-fallback`` to let AI.GENERATE "
          "fill the gaps. Sample diagnostics: " + "; ".join(samples)
      ),
  )


def _iso(value: _dt.datetime) -> str:
  """Coerce to UTC isoformat with a trailing ``Z`` for JSON."""
  if value.tzinfo is None:
    value = value.replace(tzinfo=_dt.timezone.utc)
  return value.astimezone(_dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _iso_optional(value: Optional[_dt.datetime]) -> Optional[str]:
  return _iso(value) if value is not None else None


def _parse_backfill_timestamp(
    flag_name: str, value: Optional[str]
) -> Optional[_dt.datetime]:
  """Parse a backfill window bound from the CLI / env-var surface.

  Accepts ``None`` or empty string as "unset" (the materializer's
  own validator then enforces the required-when-backfill contract).
  Empty-string handling matters for env-var pass-through: a
  defaulted-but-unset env var in ``run_job.py`` arrives as ``""``,
  not ``None``.

  Accepts UTC ISO 8601 with either ``Z`` or ``+00:00`` suffix and
  normalizes to a tz-aware UTC datetime. ``Z`` is handled
  explicitly so the parser works on Python 3.10 (3.11+ added
  native ``Z`` support to ``fromisoformat``).

  Raises ``ValueError`` with the flag name embedded in the
  message so the operator can fix the typo without digging.
  """
  if value is None or value == "":
    return None
  normalized = value.strip()
  if not normalized:
    return None
  # Python 3.10 ``datetime.fromisoformat`` doesn't recognize ``Z``;
  # rewrite to ``+00:00`` so the parse succeeds across all
  # supported versions.
  if normalized.endswith("Z"):
    normalized = normalized[:-1] + "+00:00"
  try:
    parsed = _dt.datetime.fromisoformat(normalized)
  except ValueError as exc:
    raise ValueError(
        f"{flag_name} must be a UTC ISO 8601 timestamp "
        f"(e.g. 2026-05-01T00:00:00Z); got {value!r}"
    ) from exc
  if parsed.tzinfo is None:
    parsed = parsed.replace(tzinfo=_dt.timezone.utc)
  return parsed.astimezone(_dt.timezone.utc)


# ------------------------------------------------------------------ #
# Orchestrator                                                         #
# ------------------------------------------------------------------ #


def run_materialize_window(
    *,
    project_id: str,
    dataset_id: str,
    ontology_path: str,
    binding_path: str,
    events_table: str = "agent_events",
    lookback_hours: float,
    overlap_minutes: float = DEFAULT_OVERLAP_MINUTES,
    completion_event_type: str = DEFAULT_COMPLETION_EVENT_TYPE,
    include_active_sessions: bool = False,
    state_table: Optional[str] = None,
    graph_name: Optional[str] = None,
    bundles_root: Optional[str] = None,
    reference_extractors_module: Optional[str] = None,
    max_sessions: Optional[int] = None,
    location: Optional[str] = None,
    validate_binding: bool = True,
    dry_run: bool = False,
    bq_client: Optional[Any] = None,
    run_started_at: Optional[_dt.datetime] = None,
    backfill: bool = False,
    from_time: Optional[_dt.datetime] = None,
    to_time: Optional[_dt.datetime] = None,
    state_key_suffix: Optional[str] = None,
    extraction_mode: str = EXTRACTION_MODE_AI_FALLBACK,
    max_session_age_hours: Optional[float] = None,
) -> MaterializeWindowResult:
  """The end-to-end run.

  Returns the structured result; caller decides what to do with
  the exit code (CLI translates ``result.ok`` to 0/1).

  Args:
    project_id, dataset_id: BigQuery target.
    ontology_path, binding_path: YAML paths.
    events_table: Source telemetry table name (relative to
      ``--dataset-id``).
    lookback_hours: Window size. The discovery query lower bound
      is ``max(last_checkpoint - overlap, run_started_at -
      lookback_hours)``. **Ignored when ``backfill=True``**: the
      backfill window comes from ``from_time`` / ``to_time``
      directly and is not bounded by the lookback cap.
    overlap_minutes: Re-process events newer than
      ``last_checkpoint - overlap_minutes``. Default 15.
        completion_event_type: ``event_type`` that marks a session
      as done. Defaults to BQ AA plugin's ``AGENT_COMPLETED``.
    include_active_sessions: If True, drop the completion-event
      filter and materialize every session seen in the window
      (partial coverage; useful for debugging, not production).
    state_table: Checkpoint table ref. Defaults to
      ``{project}.{dataset}._bqaa_materialization_state``.
    graph_name: Property-graph name. Defaults to the binding's
      ``ontology`` field.
    bundles_root: Compiled-bundle directory. Optional; if set,
      C2 wrapper is wired and outcome counts populate.
    reference_extractors_module: Dotted module path for the
      reference fallback. Required when ``bundles_root`` is set.
    max_sessions: Hard cap on sessions per run (cost guardrail).
      ``None`` = unlimited.
    location: BigQuery location.
    dry_run: Discover + binding-validate; don't extract or
      materialize.
    bq_client: Optional pre-configured BigQuery client.
    run_started_at: Test seam. Defaults to UTC ``now``.
    backfill: If True, runs in backfill mode. ``from_time``,
      ``to_time``, and ``state_key_suffix`` are all required.
      The window ``[from_time, to_time)`` is the absolute scan
      range; the steady-state checkpoint is neither read nor
      advanced (the run uses a distinct ``state_key`` carved
      out by the suffix). State rows written by the run carry
      ``mode=STATE_MODE_BACKFILL`` as an audit signal.
    from_time, to_time: UTC datetimes defining the backfill scan
      window ``[from_time, to_time)``. Required when
      ``backfill=True``; ignored otherwise.
    state_key_suffix: Optional string folded into
      :func:`compute_state_key`. Recommended for any non-
      steady-state run (backfill, ad-hoc re-extraction) so the
      run's state rows occupy a distinct ``state_key`` namespace
      and do not advance the steady-state checkpoint.
    extraction_mode: One of :data:`EXTRACTION_MODE_AI_FALLBACK`
      (default) or :data:`EXTRACTION_MODE_COMPILED_ONLY`. Default
      preserves the legacy ``extract_graph(..., use_ai_generate=True)``
      path. ``compiled-only`` routes through B1's orthogonal-flag
      surface with ``on_unhandled_span="fail"`` so ``AI.GENERATE``
      is never called and structured-extractor gaps surface as
      typed ``empty_extraction`` failures (with sample diagnostics
      in ``error_detail``) ahead of the materializer's row-count
      classifier.
    max_session_age_hours: If set (and > 0), enables the
      orphan-session watchdog (issue #180). After the steady
      discover+materialize pass, the orchestrator additionally
      scans for sessions whose first event is older than the
      cutoff but which never emitted ``AGENT_COMPLETED``. Each
      new orphan surfaces as a typed
      ``error_code='session_orphaned'`` entry in ``failures[]``
      and flips ``ok`` to False — slotting into PR #167's
      failure-mode classifier so existing Cloud Monitoring
      alerts fire without separate wiring. The watchdog state
      lives in the same state table under
      ``mode='orphan_scan'`` (per-pass audit) +
      ``mode='orphan_ledger'`` (cumulative ledger) rows; the
      scan watermark advances unconditionally so the next pass
      does less work. Skipped in ``backfill=True`` mode
      (backfill runs scan a fixed historical window where the
      "what's still in-flight?" question is undefined).
      ``None`` (default) disables the watchdog.
  """
  # Orphan-watchdog validation. Like the numeric guardrails below,
  # a typo at the boundary (``--max-session-age-hours=-1`` or ``0``)
  # is rejected here rather than producing a "scan into the future"
  # cutoff or a degenerate "everything is an orphan" cutoff.
  if max_session_age_hours is not None and max_session_age_hours <= 0:
    raise ValueError(
        f"--max-session-age-hours must be > 0 when set; got "
        f"{max_session_age_hours!r}"
    )

  # Extraction-mode validation. Reject the typo at the boundary so
  # downstream code never has to handle an unknown mode silently.
  if extraction_mode not in _VALID_EXTRACTION_MODES:
    raise ValueError(
        f"--extraction-mode must be one of "
        f"{sorted(_VALID_EXTRACTION_MODES)!r}; got {extraction_mode!r}."
    )

  # Compiled-only requires an extractor source. Without one,
  # ``_build_manager`` would take the legacy no-extractors path and
  # ``extract_graph(..., run_structured=True, use_ai_generate=False,
  # on_unhandled_span='fail')`` would emit an empty graph with no
  # diagnostics — defeating the typed ``empty_extraction`` failure
  # surface compiled-only mode is supposed to guarantee. Catch the
  # silent-empty failure mode at the boundary, before any BigQuery
  # work runs.
  if (
      extraction_mode == EXTRACTION_MODE_COMPILED_ONLY
      and bundles_root is None
      and reference_extractors_module is None
  ):
    raise ValueError(
        "--extraction-mode=compiled-only requires either "
        "--bundles-root or --reference-extractors-module (or both). "
        "Neither is set, so the orchestrator would build a manager "
        "with no structured extractors and silently emit empty "
        "graphs for every session. Pass --reference-extractors-module "
        "for the simple reference-only path (e.g. "
        "--reference-extractors-module=reference_extractor on the "
        "migration v5 demo), or pre-compile bundles and pass "
        "--bundles-root alongside --reference-extractors-module."
    )

  # Numeric guardrails — reject nonsense at the boundary so the
  # orchestrator's downstream arithmetic (timedeltas, LIMIT clauses)
  # never produces a "scan into the future" or an unbounded loop.
  # A typo like ``--lookback-hours=-6`` would otherwise compute a
  # negative window and silently scan zero rows.
  if lookback_hours <= 0:
    raise ValueError(f"--lookback-hours must be > 0; got {lookback_hours!r}")
  if overlap_minutes < 0:
    raise ValueError(f"--overlap-minutes must be >= 0; got {overlap_minutes!r}")
  if max_sessions is not None and max_sessions <= 0:
    raise ValueError(
        f"--max-sessions must be unset or > 0; got {max_sessions!r}"
    )

  # Normalize ``state_key_suffix`` at the boundary so a whitespace-
  # only value ("   ") doesn't slip past the missing-suffix check
  # and become an opaque component of the state-key hash. The CLI's
  # ``--state-key-suffix`` and the env-var pass-through in
  # ``run_job.py`` both deliver raw operator input here; the strip
  # happens once, applies to both surfaces, and propagates into
  # ``compute_state_key`` cleanly. Net behavior for a callable
  # passing a non-trivial suffix is unchanged.
  if state_key_suffix is not None:
    stripped = state_key_suffix.strip()
    state_key_suffix = stripped if stripped else None

  # Backfill mode validation. Both bounds are required, the window
  # must be non-empty (from < to), AND ``state_key_suffix`` must be
  # set (post-normalization above). The suffix is what carves the
  # backfill out of the steady-state ``state_key`` namespace;
  # ``read_last_checkpoint`` filters only by ``state_key``, so a
  # backfill that shares the steady-state key would write a row
  # that the next cron run reads as its checkpoint.
  # ``mode='backfill'`` on the row is an audit signal, not a
  # filter — it does not protect the checkpoint stream. Reject the
  # missing-suffix configuration loudly at the boundary; this is
  # the contract the CLI's ``--backfill`` help text promises
  # ("without reading or advancing the steady-state checkpoint").
  # PR #188 review.
  if backfill:
    if from_time is None or to_time is None:
      raise ValueError(
          "--backfill requires both --from and --to (UTC ISO 8601). "
          f"Got from_time={from_time!r}, to_time={to_time!r}."
      )
    if not state_key_suffix:
      raise ValueError(
          "--backfill requires --state-key-suffix so the backfill's "
          "state rows occupy a distinct state_key namespace from the "
          "steady-state cron. Without a suffix a successful backfill "
          "row would later be read by the steady-state checkpoint "
          "and silently rewind the cron's high-water mark. "
          "Recommended: a short stable name like 'backfill-may-w1'."
      )
    # Normalize tz-naive boundaries to UTC so comparisons + state
    # writes don't depend on the caller's locale.
    if from_time.tzinfo is None:
      from_time = from_time.replace(tzinfo=_dt.timezone.utc)
    if to_time.tzinfo is None:
      to_time = to_time.replace(tzinfo=_dt.timezone.utc)
    if from_time >= to_time:
      raise ValueError(
          f"--backfill requires --from < --to; got {from_time!r} >= {to_time!r}."
      )
  else:
    # ``from_time`` / ``to_time`` are meaningless outside backfill.
    # Reject the misconfiguration loudly rather than silently
    # ignoring it — an operator who passed them without ``--backfill``
    # probably intended backfill mode.
    if from_time is not None or to_time is not None:
      raise ValueError(
          "--from and --to are only valid with --backfill. "
          f"Got from_time={from_time!r}, to_time={to_time!r}, backfill=False."
      )
  # Empty/whitespace completion-event-type silently turns the run
  # into a no-op: the discovery query would bind ``event_type =
  # ""``, match nothing, write a clean heartbeat row, and look
  # healthy. Reject the operator typo at the boundary. The
  # ``include_active_sessions`` path drops the event-type filter
  # entirely, so the check is bypassed there.
  #
  # Reject impure whitespace too — `` AGENT_COMPLETED `` would
  # otherwise bind a spaced value into ``event_type =
  # @completion_event_type`` and produce the same clean no-op
  # heartbeat. Stripping silently would diverge from what the
  # operator saw on the command line; explicit rejection forces
  # them to fix the typo.
  if not include_active_sessions:
    if not isinstance(completion_event_type, str):
      raise ValueError(
          f"--completion-event-type must be a non-empty string; "
          f"got {completion_event_type!r}"
      )
    if not completion_event_type.strip():
      raise ValueError(
          f"--completion-event-type must be a non-empty string; "
          f"got {completion_event_type!r}"
      )
    if completion_event_type != completion_event_type.strip():
      raise ValueError(
          f"--completion-event-type must not have leading or trailing "
          f"whitespace; got {completion_event_type!r}"
      )

  from google.cloud import bigquery

  # Pin a single timestamp for the whole run.
  run_started = (
      run_started_at
      if run_started_at is not None
      else _dt.datetime.now(_dt.timezone.utc)
  )
  if run_started.tzinfo is None:
    run_started = run_started.replace(tzinfo=_dt.timezone.utc)
  run_id = generate_run_id()

  client = bq_client or bigquery.Client(project=project_id, location=location)

  # Resolve identifiers + qualified refs.
  events_table_ref = validated_table_ref(project_id, dataset_id, events_table)
  state_project, state_dataset, state_table_local = parse_state_table_ref(
      state_table or DEFAULT_STATE_TABLE_NAME,
      default_project=project_id,
      default_dataset=dataset_id,
  )
  state_table_ref = f"{state_project}.{state_dataset}.{state_table_local}"

  # Load ontology + binding (raw text for the fingerprint helper +
  # parsed objects for the orchestrator).
  from bigquery_ontology import Binding
  from bigquery_ontology import load_binding
  from bigquery_ontology import load_ontology
  from bigquery_ontology import Ontology
  from bigquery_ontology._fingerprint import fingerprint_model

  ontology_obj: Ontology = load_ontology(ontology_path)
  binding_obj: Binding = load_binding(binding_path, ontology=ontology_obj)
  ontology_fp = fingerprint_model(ontology_obj)
  binding_fp = fingerprint_model(binding_obj)

  resolved_graph_name = graph_name or binding_obj.ontology

  # Discovery-mode component of the state key. A switch from
  # ``terminal:AGENT_COMPLETED`` to a custom event, or a swap to
  # ``active`` (``--include-active-sessions`` debug mode), produces
  # a new key so the prior predicate's high-water mark cannot
  # advance the new predicate past historical completions it
  # hasn't seen.
  discovery_mode = (
      "active"
      if include_active_sessions
      else f"terminal:{completion_event_type}"
  )

  state_key = compute_state_key(
      project_id=project_id,
      dataset_id=dataset_id,
      graph_name=resolved_graph_name,
      events_table=events_table,
      ontology_fingerprint=ontology_fp,
      binding_fingerprint=binding_fp,
      discovery_mode=discovery_mode,
      state_key_suffix=state_key_suffix,
  )

  # Tag every state-row write with the orchestrator mode. The
  # ``state_key_suffix`` keeps the storage namespaces apart; ``mode``
  # is the audit-trail signal so an operator joining the two streams
  # in a single query can tell them apart at a glance.
  current_state_mode = STATE_MODE_BACKFILL if backfill else STATE_MODE_STEADY

  # State table must exist for read/write. Idempotent CREATE.
  ensure_state_table(client, state_table_ref)
  # In backfill mode the steady-state checkpoint is irrelevant to
  # the scan window (``from_time`` / ``to_time`` are absolute).
  # Reading it is still cheap and surfaces in the JSON report as
  # ``checkpoint_read`` for audit clarity, but a fresh ``state_key``
  # (when ``state_key_suffix`` is set) means the read returns
  # ``None`` and the bootstrap path triggers — that's expected.
  last_checkpoint = read_last_checkpoint(client, state_table_ref, state_key)

  # Pre-flight binding validation against live BQ — the "fail
  # before AI.GENERATE spend" contract from #161. Skipped on
  # ``--dry-run`` (caller already opted out of side effects) and
  # when explicitly disabled via ``--no-validate-binding``.
  #
  # On failure we return a structured ``ok=False`` result and
  # append a state row with the drift details, instead of raising.
  # The CLI maps ``not result.ok`` to exit 1 (expected non-zero) —
  # raising would map to exit 2 (unexpected internal error), which
  # is the wrong signal for an operator: a binding drift is the
  # expected failure mode this validator was added to catch.
  if validate_binding and not dry_run:
    from .binding_validation import validate_binding_against_bigquery

    report = validate_binding_against_bigquery(
        ontology=ontology_obj, binding=binding_obj, bq_client=client
    )
    if not report.ok:
      failure_msgs = "; ".join(
          f"{f.code.value}:{f.binding_path}" for f in report.failures[:10]
      )
      drift_detail = (
          f"binding-validate failed before extraction: "
          f"{len(report.failures)} failures. {failure_msgs}"
      )
      drift_failure = {
          "session_id": None,
          "error_code": "binding_validate_failed",
          "error_detail": drift_detail,
      }
      drift_result = MaterializeWindowResult(
          run_id=run_id,
          state_key=state_key,
          window_start=run_started,
          window_end=run_started,
          checkpoint_read=last_checkpoint,
          checkpoint_written=last_checkpoint,
          sessions_discovered=0,
          sessions_materialized=0,
          sessions_failed=0,
          rows_materialized={},
          table_statuses={},
          compiled_outcomes={
              "compiled_unchanged": 0,
              "compiled_filtered": 0,
              "fallback_for_event": 0,
          },
          failures=[drift_failure],
          ok=False,
          compile_bundle_fingerprint=None,
      )
      append_state_row(
          client,
          state_table_ref,
          StateRow(
              state_key=state_key,
              run_id=run_id,
              run_started_at=run_started,
              scan_start=run_started,
              scan_end=run_started,
              last_completion_at=last_checkpoint,
              sessions_discovered=0,
              sessions_materialized=0,
              sessions_failed=0,
              ok=False,
              error_detail=drift_detail,
              report_json=json.dumps(drift_result.to_json()),
              mode=current_state_mode,
          ),
      )
      return drift_result

  if backfill:
    # Backfill mode: ``from_time`` / ``to_time`` are the absolute
    # scan window. The lookback cap intentionally does NOT apply —
    # an operator backfilling six weeks of history must not have
    # the window silently clipped to ``lookback_hours``. The
    # numeric guardrail (``lookback_hours > 0``) still ran at the
    # boundary above; the value is unused here.
    assert from_time is not None and to_time is not None  # validated above
    scan_start = from_time
    scan_end = to_time
  else:
    # Steady-state mode: original behavior.
    #
    # Bootstrap (no checkpoint) → use ``--lookback-hours`` as the
    # initial scan window. The previous draft used a hard-coded
    # 30min default which made ``--lookback-hours 6`` actually
    # scan 30 minutes on first run.
    # Subsequent runs → ``compute_scan_start`` returns
    # ``last_checkpoint - overlap_minutes`` (the bootstrap window
    # arg is ignored when ``checkpoint_timestamp`` is set).
    scan_start = compute_scan_start(
        run_started,
        checkpoint_timestamp=last_checkpoint,
        overlap=_dt.timedelta(minutes=overlap_minutes),
        initial_lookback=_dt.timedelta(hours=lookback_hours),
    )
    # ``lookback_hours`` is also the hard upper bound on how far
    # back we ever scan — applies on subsequent runs when the
    # checkpoint is very stale.
    hard_floor = run_started - _dt.timedelta(hours=lookback_hours)
    if scan_start < hard_floor:
      scan_start = hard_floor
    scan_end = run_started

  # Bind the discovery parameters. ``completion_event_type`` and
  # ``include_active_sessions`` interact here.
  if include_active_sessions:
    # Drop the event-type filter. Any session with at least one
    # event in the window counts.
    discovery_sql = (
        "SELECT session_id, MAX(timestamp) AS completion_timestamp\n"
        f"FROM `{events_table_ref}`\n"
        "WHERE timestamp >= @scan_start\n"
        "  AND timestamp < @scan_end\n"
        "  AND session_id IS NOT NULL\n"
        "GROUP BY session_id\n"
        "ORDER BY completion_timestamp\n"
        + (f"LIMIT {int(max_sessions)}\n" if max_sessions else "")
    )
    discovery_params = [
        bigquery.ScalarQueryParameter("scan_start", "TIMESTAMP", scan_start),
        bigquery.ScalarQueryParameter("scan_end", "TIMESTAMP", scan_end),
    ]
  else:
    discovery_sql = build_discovery_sql(
        events_table_ref=events_table_ref,
        completion_event_type=completion_event_type,
        max_sessions=max_sessions,
    )
    discovery_params = [
        bigquery.ScalarQueryParameter("scan_start", "TIMESTAMP", scan_start),
        bigquery.ScalarQueryParameter("scan_end", "TIMESTAMP", scan_end),
        bigquery.ScalarQueryParameter(
            "completion_event_type", "STRING", completion_event_type
        ),
    ]

  discovered = [
      DiscoveredSession(
          session_id=row.session_id,
          completion_timestamp=row.completion_timestamp,
      )
      for row in client.query(
          discovery_sql,
          job_config=bigquery.QueryJobConfig(query_parameters=discovery_params),
      ).result()
  ]

  if dry_run:
    # Dry-run resolves the fingerprint too so the preview report
    # reflects which bundle *would* run. Cost: a directory walk;
    # no BQ side effects.
    dryrun_fingerprint: Optional[str] = None
    if bundles_root is not None:
      dryrun_fingerprint = _pre_scan_bundle_fingerprint(
          pathlib.Path(bundles_root)
      )
    return _build_result(
        run_id=run_id,
        state_key=state_key,
        scan_start=scan_start,
        scan_end=scan_end,
        checkpoint_read=last_checkpoint,
        checkpoint_written=None,
        sessions_discovered=len(discovered),
        session_results=[],
        compiled_outcomes={
            "compiled_unchanged": 0,
            "compiled_filtered": 0,
            "fallback_for_event": 0,
        },
        ok=True,
        compile_bundle_fingerprint=dryrun_fingerprint,
    )

  # Build a runtime manager + outcome counter (only meaningful
  # when bundles_root is set). For dry-run we already returned.
  outcomes_cb, outcomes_counts = make_outcome_counter()
  # Resolve the bundle fingerprint up here (not inside
  # ``_build_manager``) so we can record it in the JSON report
  # even when the manager is mocked out in tests.
  compile_bundle_fingerprint: Optional[str] = None
  if bundles_root is not None:
    compile_bundle_fingerprint = _pre_scan_bundle_fingerprint(
        pathlib.Path(bundles_root)
    )
  manager = _build_manager(
      project_id=project_id,
      dataset_id=dataset_id,
      ontology=ontology_obj,
      binding=binding_obj,
      location=location,
      bq_client=client,
      bundles_root=bundles_root,
      reference_extractors_module=reference_extractors_module,
      outcome_callback=outcomes_cb,
      table_id=events_table,
      expected_fingerprint=compile_bundle_fingerprint,
  )

  # Materialize per session so a single-session failure doesn't
  # cascade. Checkpoint advances after each success; on failure
  # we stop, record the failure, and exit non-zero.
  from .ontology_materializer import OntologyMaterializer

  materializer = OntologyMaterializer(
      spec=manager.spec,
      project_id=project_id,
      dataset_id=dataset_id,
      location=location,
      bq_client=client,
  )

  session_results: list[SessionResult] = []
  for session in discovered:
    try:
      if extraction_mode == EXTRACTION_MODE_COMPILED_ONLY:
        # Orthogonal-flag surface (B1): structured extractors only,
        # no ``AI.GENERATE`` call, unhandled spans surface as
        # ``structured_unhandled`` / ``extractor_exception``
        # diagnostics in ``graph.diagnostics``. We translate those
        # to a typed ``empty_extraction`` failure below — before
        # the row-count classifier so compiled-only's failure
        # detail carries the diagnostic samples directly.
        graph = manager.extract_graph(
            session_ids=[session.session_id],
            use_ai_generate=False,
            run_structured=True,
            on_unhandled_span="fail",
        )
      else:
        # Default ``ai-fallback`` mode keeps the legacy bool path —
        # zero behavior change vs pre-#178 for every existing
        # caller (notebook, run_job.py, anyone calling the CLI
        # without ``--extraction-mode``).
        graph = manager.extract_graph(
            session_ids=[session.session_id], use_ai_generate=True
        )

      # Compiled-only failure surface: translate B1 diagnostics
      # into a typed ``empty_extraction`` row when the structured
      # extractors didn't cover the session.
      if extraction_mode == EXTRACTION_MODE_COMPILED_ONLY:
        failure_result = _classify_compiled_only_diagnostics(session, graph)
        if failure_result is not None:
          session_results.append(failure_result)
          # Conservative stop, same shape as exception path below —
          # the checkpoint advances only to the prior success.
          break

      mat = materializer.materialize_with_status(graph, [session.session_id])
      # Capture per-session table statuses so the JSON report can
      # show cleanup_status / insert_status per bound table — the
      # operational signal that lets customers see streaming-
      # buffer-pinned delete failures in the right granularity.
      table_statuses_dict: dict[str, dict[str, Any]] = {}
      for tbl_name, ts in (mat.table_statuses or {}).items():
        table_statuses_dict[tbl_name] = {
            "table_ref": ts.table_ref,
            "rows_attempted": ts.rows_attempted,
            "rows_inserted": ts.rows_inserted,
            "cleanup_status": ts.cleanup_status,
            "insert_status": ts.insert_status,
            "idempotent": ts.idempotent,
        }
      # Zero-rows guard. ``row_counts`` only includes tables
      # whose insert SUCCEEDED (per ``ontology_materializer.py``
      # — see the ``inserted_tables`` filter at the build site).
      # So ``total_rows == 0`` collapses two distinct failure
      # modes that operators must triage differently:
      #
      #   * **empty_extraction** — the graph itself was empty.
      #     Extraction (AI.GENERATE or compiled bundle) returned
      #     no rows; nothing to insert. ``table_statuses`` will
      #     be empty or have ``rows_attempted == 0`` everywhere.
      #     Common cause: missing ``roles/aiplatform.user`` on
      #     the runtime SA (surfaced by #166 live deploy), or
      #     the session's events legitimately had no MAKO
      #     content.
      #
      #   * **materialization_failed** — the graph HAD rows but
      #     every insert returned an error. ``table_statuses``
      #     will have entries with ``rows_attempted > 0`` and
      #     ``insert_status == "insert_failed"``. Common cause:
      #     dataset write-perm regression, streaming-buffer
      #     pinning that hits a delete-then-insert idempotency
      #     boundary, or a schema mismatch the binding-validate
      #     pre-flight didn't catch.
      #
      # Operators chasing the wrong failure mode is the failure
      # mode this PR was meant to prevent. Classify here.
      total_rows = sum(int(v) for v in (mat.row_counts or {}).values())
      if total_rows == 0:
        any_attempted = any(
            int(ts.get("rows_attempted", 0)) > 0
            for ts in table_statuses_dict.values()
        )
        any_insert_failed = any(
            ts.get("insert_status") == "insert_failed"
            for ts in table_statuses_dict.values()
        )
        if any_attempted and any_insert_failed:
          error_code = "materialization_failed"
          error_detail = (
              "session extracted rows but every insert failed: "
              + "; ".join(
                  f"{name}: rows_attempted={ts.get('rows_attempted')}, "
                  f"insert_status={ts.get('insert_status')!r}, "
                  f"cleanup_status={ts.get('cleanup_status')!r}"
                  for name, ts in sorted(table_statuses_dict.items())
              )
              + ". Common causes: dataset write-permission "
              "regression on the runtime SA, schema mismatch the "
              "binding-validate pre-flight didn't catch, or "
              "streaming-buffer pinning blocking a delete-then-"
              "insert cycle."
          )
        else:
          error_code = "empty_extraction"
          error_detail = (
              "session materialized zero rows across every entity "
              "table and no inserts were attempted; usually means "
              "extraction (AI.GENERATE or compiled bundle) returned "
              "an empty graph. Common causes: missing "
              "roles/aiplatform.user on the runtime SA, transient "
              "AI.GENERATE rate limit, or the session's events did "
              "not contain any extractable ontology content."
          )
        session_results.append(
            SessionResult(
                session_id=session.session_id,
                ok=False,
                completion_timestamp=session.completion_timestamp,
                rows_materialized=dict(mat.row_counts),
                table_statuses=table_statuses_dict,
                error_code=error_code,
                error_detail=error_detail,
            )
        )
        break
      session_results.append(
          SessionResult(
              session_id=session.session_id,
              ok=True,
              completion_timestamp=session.completion_timestamp,
              rows_materialized=dict(mat.row_counts),
              table_statuses=table_statuses_dict,
          )
      )
    except Exception as exc:  # noqa: BLE001 — orchestrator is the boundary
      session_results.append(
          SessionResult(
              session_id=session.session_id,
              ok=False,
              completion_timestamp=session.completion_timestamp,
              error_code=type(exc).__name__,
              error_detail=(
                  f"{type(exc).__name__}: {exc}\n"
                  + traceback.format_exc(limit=5)
              ),
          )
      )
      # Conservative stop: don't try to materialize subsequent
      # sessions. The checkpoint advances only to the highest
      # successfully-materialized completion timestamp; next run
      # picks up here.
      break

  last_success_ts = _max_success_completion(session_results)
  ok = (
      all(r.ok for r in session_results)
      and len(session_results) > 0
      or (not session_results)
  )
  # Note: an empty window is "ok" with no checkpoint advance, but
  # we still write a heartbeat row so operators can see the run
  # happened.
  #
  # The written checkpoint is the **maximum** of the prior
  # watermark and this run's last successful completion. Picking
  # whichever value is higher prevents two regressions:
  #
  # * **Overlap rewind.** ``--overlap-minutes`` re-scans events
  #   slightly older than the last checkpoint to catch late-
  #   arriving rows. If a session inside that overlap succeeds
  #   but a *later* session fails, ``last_success_ts`` is the
  #   re-scanned (older) timestamp; writing that would move the
  #   high-water mark backwards. Taking ``max`` keeps the prior
  #   advance.
  # * **No-advance carry-forward.** When no session succeeded,
  #   ``last_success_ts`` is ``None`` — the prior checkpoint
  #   carries forward so the most-recent state row is self-
  #   documenting ("still at X" rather than NULL).
  if last_success_ts is None:
    checkpoint_written = last_checkpoint
  elif last_checkpoint is None:
    checkpoint_written = last_success_ts
  else:
    checkpoint_written = max(last_checkpoint, last_success_ts)

  failures = [
      {
          "session_id": r.session_id,
          "error_code": r.error_code,
          "error_detail": r.error_detail,
      }
      for r in session_results
      if not r.ok
  ]

  # Orphan-watchdog scan (issue #180). Runs after the steady pass
  # so a partial steady failure doesn't block the watchdog signal,
  # and so the steady state row is written with its own counts
  # before the orphan rows are appended. Disabled in backfill mode
  # (the "what's still in-flight?" question is undefined when the
  # scan window is a fixed historical slice).
  orphan_result: Optional[OrphanScanResult] = None
  orphan_failures: list[dict[str, Any]] = []
  if max_session_age_hours is not None and not backfill:
    qualified_events_ref = (
        events_table
        if events_table.count(".") >= 2
        else f"{project_id}.{dataset_id}.{events_table}"
    )
    orphan_result = run_orphan_watchdog(
        client,
        events_table_ref=qualified_events_ref,
        state_table_ref=state_table_ref,
        state_key=state_key,
        run_id=run_id,
        run_started_at=run_started,
        max_session_age_hours=max_session_age_hours,
        completion_event_type=completion_event_type,
    )
    cutoff_iso = orphan_result.cutoff_at.isoformat()
    for orphan_session_id in orphan_result.new_orphans:
      orphan_failures.append(
          {
              "session_id": orphan_session_id,
              "error_code": "session_orphaned",
              "error_detail": (
                  f"session {orphan_session_id!r} has first event older than "
                  f"{max_session_age_hours} hours (cutoff {cutoff_iso}) but "
                  f"never emitted '{completion_event_type}'. The orphan "
                  f"watchdog flagged it on this scan and recorded it in the "
                  f"cumulative ledger (state_key={state_key!r}, "
                  f"mode='{STATE_MODE_ORPHAN_LEDGER}'). Resolve by either "
                  f"emitting the missing terminal event (the watchdog will "
                  f"drop the session from the ledger on its next pass) or "
                  f"triaging the underlying agent failure."
              ),
          }
      )

  combined_failures = failures + orphan_failures
  result = _build_result(
      run_id=run_id,
      state_key=state_key,
      scan_start=scan_start,
      scan_end=scan_end,
      checkpoint_read=last_checkpoint,
      checkpoint_written=checkpoint_written,
      sessions_discovered=len(discovered),
      session_results=session_results,
      compiled_outcomes=outcomes_counts,
      ok=ok and not combined_failures,
      compile_bundle_fingerprint=compile_bundle_fingerprint,
      extra_failures=orphan_failures,
      orphan_result=orphan_result,
  )

  # Append-only state row for the steady pass. The orphan watchdog
  # writes its own ``orphan_scan`` + ``orphan_ledger`` rows inside
  # ``run_orphan_watchdog`` above, so the steady row only reports
  # the steady-pass counts here — keeping the audit trail per-row
  # type.
  append_state_row(
      client,
      state_table_ref,
      StateRow(
          state_key=state_key,
          run_id=run_id,
          run_started_at=run_started,
          scan_start=scan_start,
          scan_end=scan_end,
          last_completion_at=checkpoint_written,
          sessions_discovered=len(discovered),
          sessions_materialized=sum(1 for r in session_results if r.ok),
          sessions_failed=len(failures),
          ok=ok and not failures,
          error_detail=(failures[0]["error_detail"] if failures else None),
          report_json=json.dumps(result.to_json()),
          mode=current_state_mode,
      ),
  )

  return result


# ------------------------------------------------------------------ #
# Internals                                                            #
# ------------------------------------------------------------------ #


def _pre_scan_bundle_fingerprint(bundles_root: pathlib.Path) -> str:
  """Read ``manifest.json`` from every candidate bundle under
  *bundles_root* and return the fingerprint shared by all.

  ``discover_bundles`` requires an ``expected_fingerprint`` —
  passing ``None`` silently rejects every bundle with
  ``fingerprint_mismatch``. The CLI doesn't ask the operator to
  type the 64-hex fingerprint by hand, so we read it from the
  manifests on disk. The contract is "one root, one fingerprint";
  mixed-fingerprint roots fail fast with a clear error.
  """
  import json as _json

  if not bundles_root.is_dir():
    raise ValueError(f"--bundles-root {str(bundles_root)!r} is not a directory")
  fingerprints: dict[str, list[str]] = {}
  for child in sorted(bundles_root.iterdir()):
    manifest_path = child / "manifest.json"
    if not manifest_path.is_file():
      continue
    try:
      manifest = _json.loads(manifest_path.read_text())
    except (OSError, _json.JSONDecodeError) as exc:
      raise ValueError(
          f"--bundles-root {str(bundles_root)!r}: unreadable manifest "
          f"in {child.name}: {type(exc).__name__}: {exc}"
      )
    fp = manifest.get("fingerprint")
    if not isinstance(fp, str) or not fp:
      raise ValueError(
          f"--bundles-root {str(bundles_root)!r}: {child.name}/manifest.json "
          f"has no ``fingerprint`` field"
      )
    fingerprints.setdefault(fp, []).append(child.name)
  if not fingerprints:
    raise ValueError(
        f"--bundles-root {str(bundles_root)!r} contains no bundles "
        f"(no manifest.json files found in immediate subdirectories)"
    )
  if len(fingerprints) > 1:
    summary = "; ".join(
        f"{fp[:12]}=({', '.join(bundles)})"
        for fp, bundles in fingerprints.items()
    )
    raise ValueError(
        f"--bundles-root {str(bundles_root)!r} contains bundles with "
        f"mixed fingerprints; one root, one fingerprint. Got: {summary}"
    )
  return next(iter(fingerprints))


def _build_manager(
    *,
    project_id: str,
    dataset_id: str,
    ontology: Any,
    binding: Any,
    location: Optional[str],
    bq_client: Any,
    bundles_root: Optional[str],
    reference_extractors_module: Optional[str],
    outcome_callback: Callable[[str, Any], None],
    table_id: str,
    expected_fingerprint: Optional[str] = None,
) -> Any:
  """Construct the OntologyGraphManager — with compiled bundles
  wired when ``bundles_root`` is set, otherwise the plain
  ``from_ontology_binding`` path.

  ``table_id`` is the source telemetry table the manager reads
  from during extraction. Must match ``--events-table`` — a
  previous draft hard-coded ``agent_events`` here while discovery
  read from the configured table, producing the silent split
  ("discover from X, extract from Y") that #161 reviewer flagged
  as P1.
  """
  from .ontology_graph import OntologyGraphManager

  if bundles_root is None:
    # Two sub-paths when no compiled-bundle directory is configured:
    #
    # 1. ``reference_extractors_module`` is also unset — legacy
    #    AI-only path. The manager has no structured extractors;
    #    ``extract_graph`` falls through to ``AI.GENERATE``.
    # 2. ``reference_extractors_module`` IS set — reference-only
    #    path (added for the compiled-only deploy follow-up). The
    #    reference module's ``EXTRACTORS`` dict goes straight into
    #    the manager's extractor registry. Equivalent to compiled-
    #    bundle mode for the customer's purposes — same handlers,
    #    same diagnostic surface — without the offline bundle-
    #    compilation step. Customers who want fingerprint-stable
    #    compiled bundles still get that path by also setting
    #    ``bundles_root``.
    extractors = None
    if reference_extractors_module is not None:
      import importlib

      ref_module = importlib.import_module(reference_extractors_module)
      extractors = getattr(ref_module, "EXTRACTORS", None)
      if not isinstance(extractors, dict) or not extractors:
        raise ValueError(
            f"reference module {reference_extractors_module!r} must expose "
            f"a non-empty EXTRACTORS dict"
        )
    return OntologyGraphManager.from_ontology_binding(
        project_id=project_id,
        dataset_id=dataset_id,
        ontology=ontology,
        binding=binding,
        location=location,
        bq_client=bq_client,
        table_id=table_id,
        extractors=extractors,
    )

  if reference_extractors_module is None:
    raise ValueError(
        "--reference-extractors-module is required when --bundles-root is set"
    )

  import importlib
  import pathlib

  ref_module = importlib.import_module(reference_extractors_module)
  fallback_extractors = getattr(ref_module, "EXTRACTORS", None)
  if not isinstance(fallback_extractors, dict) or not fallback_extractors:
    raise ValueError(
        f"reference module {reference_extractors_module!r} must expose "
        f"a non-empty EXTRACTORS dict"
    )

  # The orchestrator pre-scans the bundles root for the manifest
  # fingerprint and threads it down here. The SDK's
  # ``discover_bundles`` requires an ``expected_fingerprint``;
  # passing ``None`` silently rejects every bundle as
  # ``fingerprint_mismatch``. If the caller didn't pre-resolve
  # (defensive — orchestrator always does when ``bundles_root`` is
  # set), fall back to a local scan.
  if expected_fingerprint is None:
    expected_fingerprint = _pre_scan_bundle_fingerprint(
        pathlib.Path(bundles_root)
    )

  from .extractor_compilation import discover_bundles

  discovery = discover_bundles(
      pathlib.Path(bundles_root),
      expected_fingerprint=expected_fingerprint,
  )
  if not discovery.registry:
    # Every candidate bundle must have failed for some other
    # reason (manifest_missing, smoke_failed, etc.). Surface the
    # discovery failures so the operator can diagnose.
    failure_summary = ", ".join(
        f"{f.bundle_dir.name}={f.code}" for f in discovery.failures
    )
    raise ValueError(
        f"--bundles-root {bundles_root!r} matched fingerprint "
        f"{expected_fingerprint!r} but produced no loadable bundles. "
        f"Discovery failures: {failure_summary or '(none reported)'}"
    )

  return OntologyGraphManager.from_bundles_root(
      project_id=project_id,
      dataset_id=dataset_id,
      ontology=ontology,
      binding=binding,
      bundles_root=pathlib.Path(bundles_root),
      expected_fingerprint=expected_fingerprint,
      fallback_extractors=fallback_extractors,
      location=location,
      bq_client=bq_client,
      table_id=table_id,
      on_outcome=outcome_callback,
  )


def _max_success_completion(
    session_results: Sequence[SessionResult],
) -> Optional[_dt.datetime]:
  """MAX completion-timestamp among successfully materialized
  sessions. ``None`` if there are no successes."""
  ts = [r.completion_timestamp for r in session_results if r.ok]
  return max(ts) if ts else None


# Precedence for aggregating per-session ``cleanup_status`` and
# ``insert_status`` across multiple sessions touching the same
# bound table. The "worst" status wins, so a single
# ``delete_failed`` in session A is never masked by a clean
# ``deleted`` in session B. Operators rely on these surfaces as
# the "did anything go wrong" signal in the JSON report.
_CLEANUP_RANK = {"delete_failed": 2, "skipped": 1, "deleted": 0}
_INSERT_RANK = {"insert_failed": 1, "inserted": 0}


def _merge_table_status(
    existing: dict[str, Any], incoming: dict[str, Any]
) -> dict[str, Any]:
  """Combine two per-table status dicts using worst-status wins.

  Row counts (``rows_attempted`` / ``rows_inserted``) sum.
  ``idempotent`` is AND-ed across sessions — one non-idempotent
  session contaminates the table's overall idempotency claim.
  ``table_ref`` is copied from whichever side has a non-empty
  value (they should agree; if not, the existing one is kept).
  """
  cur_cleanup = existing.get("cleanup_status", "deleted")
  new_cleanup = incoming.get("cleanup_status", "deleted")
  cleanup = (
      new_cleanup
      if _CLEANUP_RANK.get(new_cleanup, -1) > _CLEANUP_RANK.get(cur_cleanup, -1)
      else cur_cleanup
  )
  cur_insert = existing.get("insert_status", "inserted")
  new_insert = incoming.get("insert_status", "inserted")
  insert = (
      new_insert
      if _INSERT_RANK.get(new_insert, -1) > _INSERT_RANK.get(cur_insert, -1)
      else cur_insert
  )
  return {
      "table_ref": existing.get("table_ref") or incoming.get("table_ref"),
      "rows_attempted": (
          existing.get("rows_attempted", 0) + incoming.get("rows_attempted", 0)
      ),
      "rows_inserted": (
          existing.get("rows_inserted", 0) + incoming.get("rows_inserted", 0)
      ),
      "cleanup_status": cleanup,
      "insert_status": insert,
      "idempotent": bool(
          existing.get("idempotent", True) and incoming.get("idempotent", True)
      ),
  }


def _build_result(
    *,
    run_id: str,
    state_key: str,
    scan_start: _dt.datetime,
    scan_end: _dt.datetime,
    checkpoint_read: Optional[_dt.datetime],
    checkpoint_written: Optional[_dt.datetime],
    sessions_discovered: int,
    session_results: Sequence[SessionResult],
    compiled_outcomes: dict[str, int],
    ok: bool,
    compile_bundle_fingerprint: Optional[str] = None,
    extra_failures: Optional[Sequence[dict[str, Any]]] = None,
    orphan_result: Optional[OrphanScanResult] = None,
) -> MaterializeWindowResult:
  rows_materialized: dict[str, int] = {}
  # Aggregate per-session table_statuses into the report with
  # worst-status-wins semantics. A previous draft used "latest
  # seen status wins", which could hide an earlier
  # ``cleanup_status = 'delete_failed'`` behind a later session's
  # clean ``deleted``. Operators rely on the report as the
  # "did anything go wrong" signal — any delete failure must
  # bubble up.
  # Aggregate ``table_statuses`` from EVERY session — including
  # failed ones. The status surface ("did the delete succeed?
  # did the insert succeed?") is operator-visible regardless of
  # session-level success. Dropping failed sessions' statuses
  # was the gap #167's reviewer flagged: when a session fails
  # with ``materialization_failed``, the per-table diagnostic
  # (rows_attempted=N, insert_status=insert_failed) belongs in
  # the report so operators don't have to dig into log payloads
  # to see which table broke.
  #
  # ``rows_materialized`` still aggregates only successful
  # sessions — that's the "rows that actually landed" view.
  table_statuses_agg: dict[str, dict[str, Any]] = {}
  for r in session_results:
    if r.ok:
      for table, n in r.rows_materialized.items():
        rows_materialized[table] = rows_materialized.get(table, 0) + n
    for table, ts in r.table_statuses.items():
      if table in table_statuses_agg:
        table_statuses_agg[table] = _merge_table_status(
            table_statuses_agg[table], ts
        )
      else:
        table_statuses_agg[table] = dict(ts)

  failures = [
      {
          "session_id": r.session_id,
          "error_code": r.error_code,
          "error_detail": r.error_detail,
      }
      for r in session_results
      if not r.ok
  ]
  # Orphan-watchdog failures (issue #180) are merged into the
  # report's ``failures[]`` so the existing #167 classifier +
  # Cloud Monitoring alerts surface them without separate wiring.
  # They also count toward ``sessions_failed`` so the operator-
  # visible "did this run cleanly?" signal is honest.
  if extra_failures:
    failures.extend(extra_failures)

  return MaterializeWindowResult(
      run_id=run_id,
      state_key=state_key,
      window_start=scan_start,
      window_end=scan_end,
      checkpoint_read=checkpoint_read,
      checkpoint_written=checkpoint_written,
      sessions_discovered=sessions_discovered,
      sessions_materialized=sum(1 for r in session_results if r.ok),
      sessions_failed=len(failures),
      rows_materialized=rows_materialized,
      table_statuses=table_statuses_agg,
      compiled_outcomes=compiled_outcomes,
      failures=failures,
      ok=ok,
      compile_bundle_fingerprint=compile_bundle_fingerprint,
      orphan_result=orphan_result,
  )
