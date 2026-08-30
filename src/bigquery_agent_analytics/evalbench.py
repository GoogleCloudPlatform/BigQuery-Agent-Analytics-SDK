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
"""Read EvalBench BigQuery runs, map them to BQAA event rows, and snapshot them.

The reader is pull-only: EvalBench keeps ownership of its ``configs``,
``results``, and ``scores`` tables, while BQAA converts one ``job_id`` at a
time into the mirror-table row contract tracked by issue #97.

``EvalBenchRun.materialize`` (issue #435) publishes that mapping as an
immutable, versioned snapshot into BQAA-owned tables -- never the ADK
plugin's production ``agent_events`` table. Events, imported scores, and a
manifest row are staged with load jobs and then published by one BigQuery
multi-statement transaction that first claims a pre-existing lock row and
then decides, against its own snapshot, whether the version is *absent*
(publish), *identical* (leave the first publish in place) or *conflicting*
(raise), so a failed re-import cannot leave a partial corpus behind and two
first-time imports of one version cannot both commit. ``failed_sessions_sql``
reads one pinned ``import_version`` and implements the W0.4 contract:
``returncode == 0`` means *completed*, and only the per-benchmark score
policy decides *passed*.

Slice 2 of #435 makes that denominator queryable without mixing versions:
``materialize`` also maintains one ``evalbench_failed_sessions`` VIEW per
target dataset whose body is ``failed_sessions_sql`` with the job's latest
successful import rendered as literals, and ``failed_sessions`` is the
version-pinned Python consumer whose rows carry the versioned
``session_id`` that ``Client.get_session_trace`` drills into.
"""

from __future__ import annotations

import ast
from collections.abc import Mapping
import dataclasses
from datetime import date
from datetime import datetime
from datetime import timedelta
from datetime import timezone
import hashlib
import json
import logging
import math
import re
from typing import Any, Optional, TYPE_CHECKING
import uuid

from google.api_core.exceptions import Conflict
from google.api_core.exceptions import NotFound
from google.api_core.exceptions import PreconditionFailed
from google.cloud import bigquery

from ._telemetry import make_bq_client
from ._telemetry import with_sdk_labels
from .failure_taxonomy import categorize_failed_session

if TYPE_CHECKING:
  from .trace import TraceFilter

_SOURCE_SEGMENT_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
_MISSING_TEXT = frozenset({"", "<na>", "nan", "none", "null"})
_NO_GENERATED_OUTPUT = frozenset({"skipped"})
_UNKNOWN_RUN_TIME = datetime(1970, 1, 1, tzinfo=timezone.utc)

_READ_SOURCE_TABLE_QUERY = """\
SELECT *
FROM `{table_id}`
WHERE job_id = @job_id
"""

_READ_SOURCE_TABLE_SNAPSHOT_QUERY = """\
SELECT *
FROM `{table_id}` FOR SYSTEM_TIME AS OF @snapshot_at
WHERE job_id = @job_id
"""

DEFAULT_EVENTS_TABLE = "evalbench_agent_events"
DEFAULT_SCORES_TABLE = "evalbench_scores_imported"
# The one import registry per target dataset. It is deliberately not
# caller-selectable: every import into ``{target_project}.{target_dataset}``
# consults this table before deleting rows from *any* events/scores table in
# that dataset, so a second manifest can never route around the version
# immutability guard of the first.
MANIFEST_TABLE = "evalbench_import_manifest"
# The dataset's publish lock. It holds one sentinel row that every publish
# transaction UPDATEs before touching the manifest, events, or scores.
# BigQuery cancels a transaction that mutates rows another concurrent
# transaction has already mutated, but reads, appends, and keyed DELETEs that
# match nothing never conflict -- so under snapshot isolation two first-time
# imports that both began before either committed would otherwise both
# observe "no manifest row" and both commit. Like the manifest, the lock
# table is fixed per dataset and never caller-selectable.
LOCK_TABLE = "evalbench_import_lock"
_IMPORT_LOCK_ID = "evalbench-import"
# The queryable failed-session denominator (#435 slice 2): one VIEW per
# target dataset, pinned to the *latest successful import* of one job_id
# (``_LATEST_IMPORT_QUERY`` over the manifest). Its body is
# ``failed_sessions_sql`` with ``@job_id``/``@import_version`` rendered as
# string literals, so the view never scans another version. The pin is
# recorded in a leading SQL comment (``_VIEW_PIN_MARKER``) that also names
# the manifest generation (``generation_id``) rendered and the rendered
# score policy. Both are committed manifest state, never view state: every
# publish (a ``replace`` of the same version label included) and every
# committed policy change mints an opaque ``generation_id`` for the row,
# and the row's ``view_policy`` is the gate the view renders. The view body
# is a pure function of the latest manifest row. Nothing the view says
# about itself proves ownership: a view is the importer's only when the
# pin names a version this job committed to the manifest and the body is
# byte-for-byte what the importer renders for that manifest row from
# committed metadata alone -- the row's own policy when the pin names the
# row's current generation, or, for a view left behind by a superseded
# generation, the policy the row's ``superseded_generations`` history
# recorded for that generation when it was replaced (every publish and
# policy change appends the generation it supersedes). A generation the
# manifest never committed cannot be verified and is refused; a
# superseded view that verifies is only ever advanced to the committed
# rendering, never preserved. (BigQuery returns a view's query text
# verbatim, and whitespace inside a literal is significant, so no
# normalization is applied.) Anything else at that name -- a table, a
# foreign view, a copied marker (however self-consistent) over other SQL, a
# rendering for an unpublished version, a canonical rendering of the
# current generation under another policy, a rendering under a generation
# or policy the manifest does not hold, or a managed view somebody edited
# or reformatted -- is refused, never replaced. Writes go through
# the tables API rather than ``CREATE OR REPLACE VIEW``: create-if-absent,
# and ETag-conditional replace after re-reading the view and the latest
# manifest, so two concurrent imports cannot overwrite each other's pin.
DEFAULT_FAILED_SESSIONS_VIEW = "evalbench_failed_sessions"
_VIEW_PIN_MARKER = "-- evalbench_failed_sessions pin: "
# Bounded retries when the view changes underneath a conditional write.
_VIEW_SYNC_ATTEMPTS = 3
_VIEW_FEATURE = "evalbench-failed-sessions"
# The ADK plugin's production table. EvalBench imports publish only to
# BQAA-owned mirror tables, so this name is rejected before any BigQuery call.
_RESERVED_DESTINATION_TABLES = frozenset({"agent_events"})
_IMPORT_FEATURE = "evalbench-import"
_SCORE_FEATURE = "evalbench-score"
_STAGING_TABLE_TTL = timedelta(hours=6)
_LOGGER = logging.getLogger(__name__)
_IMPORT_VERSION_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_COMPARATOR_PATTERN = re.compile(r"^[A-Za-z0-9_.:/-]{1,128}$")

# Mirror of the ADK plugin ``agent_events`` shape (see ``_GET_TRACE_QUERY`` in
# client.py) plus the two import-binding columns BQAA owns. The extra columns
# are appended last so explicit-column consumers keep working unchanged.
_EVENT_COLUMNS = (
    "timestamp",
    "event_type",
    "agent",
    "session_id",
    "invocation_id",
    "user_id",
    "trace_id",
    "span_id",
    "parent_span_id",
    "content",
    "content_parts",
    "attributes",
    "latency_ms",
    "status",
    "error_message",
    "is_truncated",
    "job_id",
    "import_version",
)
_SCORE_SCHEMA_FIELDS = (
    ("job_id", "STRING", "REQUIRED"),
    ("import_version", "STRING", "REQUIRED"),
    ("scenario_id", "STRING", "NULLABLE"),
    ("session_id", "STRING", "NULLABLE"),
    ("comparator", "STRING", "NULLABLE"),
    ("score", "FLOAT64", "NULLABLE"),
    ("source_row", "JSON", "NULLABLE"),
)
_MANIFEST_SCHEMA_FIELDS = (
    ("job_id", "STRING", "REQUIRED"),
    ("import_version", "STRING", "REQUIRED"),
    ("source_project", "STRING", "REQUIRED"),
    ("source_dataset", "STRING", "REQUIRED"),
    ("source_snapshot_at", "TIMESTAMP", "NULLABLE"),
    ("results_count", "INT64", "REQUIRED"),
    ("scores_count", "INT64", "REQUIRED"),
    ("configs_count", "INT64", "REQUIRED"),
    ("results_fingerprint", "STRING", "REQUIRED"),
    ("scores_fingerprint", "STRING", "REQUIRED"),
    ("configs_fingerprint", "STRING", "REQUIRED"),
    ("events_table", "STRING", "REQUIRED"),
    ("scores_table", "STRING", "REQUIRED"),
    ("event_row_count", "INT64", "REQUIRED"),
    ("score_row_count", "INT64", "REQUIRED"),
    ("imported_at", "TIMESTAMP", "REQUIRED"),
    # Opaque id of this committed generation of the row: minted by every
    # publish and by every committed change of ``view_policy``. Callers
    # choose ``imported_at``, so two publishes may share it; nothing shares
    # a ``generation_id``. Declared NULLABLE only so the column can be
    # added to a manifest created before generations existed (slice 1);
    # ``_ensure_manifest_schema`` backfills every row, and every row the
    # importer writes carries one.
    ("generation_id", "STRING", "NULLABLE"),
    # The score gate the failed_sessions view renders for this version
    # (canonical JSON of ``_policy_pin``, NULL for none). The view carries
    # what this column says, never what the view itself claims.
    ("view_policy", "STRING", "NULLABLE"),
    # Committed history of the generations this row has had, oldest first:
    # one ``TO_JSON_STRING(STRUCT(generation_id, view_policy))`` per
    # superseded generation, appended by the publish transaction (a
    # ``replace``) and by ``_RECORD_VIEW_POLICY_QUERY``. It is what lets a
    # view a superseded generation left behind be authenticated from
    # committed metadata rather than from its own pin.
    ("superseded_generations", "STRING", "REPEATED"),
)
# Manifest columns the importer stages and inserts verbatim; the history
# column is computed inside the publish transaction from the row it
# replaces (``_PUBLISH_SCRIPT``), never taken from the staged row.
_STAGED_MANIFEST_COLUMNS = tuple(
    name
    for name, _, _ in _MANIFEST_SCHEMA_FIELDS
    if name != "superseded_generations"
)
_LOCK_SCHEMA_FIELDS = (
    ("lock_id", "STRING", "REQUIRED"),
    ("claim_count", "INT64", "REQUIRED"),
    ("claimed_at", "TIMESTAMP", "NULLABLE"),
    ("claimed_job_id", "STRING", "NULLABLE"),
    ("claimed_import_version", "STRING", "NULLABLE"),
)
_FINGERPRINT_KEYS = (
    "results_fingerprint",
    "scores_fingerprint",
    "configs_fingerprint",
)
# Manifest columns that, together with the fingerprints and destination
# refs, fully determine what a published version contains: the source
# project/dataset land in the manifest and in every event row's
# ``attributes.evalbench_source_project`` / ``evalbench_source_dataset``, so
# equal source rows read from another source are a *different* version.
# ``source_snapshot_at`` and ``imported_at`` are read-time bookkeeping that
# do not change any published row, and the counts are derived from the
# fingerprinted rows, so none of them takes part in the identity decision.
_PROVENANCE_KEYS = ("source_project", "source_dataset")

_READ_MANIFEST_QUERY = """\
SELECT *
FROM `{manifest_table}`
WHERE job_id = @job_id AND import_version = @import_version
"""

# "Latest successful import" of one job: the manifest holds only committed
# publishes, so the newest ``imported_at`` (ties broken by version label for
# determinism) is the version the failed_sessions view tracks and the
# version ``failed_sessions()`` resolves when none is pinned.
_LATEST_IMPORT_QUERY = """\
SELECT *
FROM `{manifest_table}`
WHERE job_id = @job_id
ORDER BY imported_at DESC, import_version DESC
LIMIT 1
"""

# Session identities of one published import version, read from the events
# table the manifest bound that version to. ``TraceFilter`` has no
# import-version dimension, so ``Client.evaluate`` is pinned to a version
# through these exact ``session_id`` values (plus the ``experiment_id`` job
# pin); retained versions of one job never share a session id
# (``_session_identity``), so the pin cannot admit another version's rows.
_IMPORT_SESSIONS_QUERY = """\
SELECT DISTINCT session_id
FROM `{events_table}`
WHERE job_id = @job_id AND import_version = @import_version
ORDER BY session_id
"""

# Commits another score gate for an already-published, identical version
# (an ``unchanged`` re-import that adds, changes, or drops the policy). The
# change is conditional on the generation the caller found, so it can never
# land on a row a concurrent ``replace`` has since re-published, and it
# mints a new generation so the view pinned to the old one is recognized as
# superseded and re-rendered from the row.
_RECORD_VIEW_POLICY_QUERY = """\
UPDATE `{manifest_table}`
SET view_policy = @view_policy,
    generation_id = @generation_id,
    superseded_generations = ARRAY_CONCAT(
        IFNULL(superseded_generations, []),
        [TO_JSON_STRING(STRUCT(
            generation_id AS generation_id, view_policy AS view_policy))])
WHERE job_id = @job_id
  AND import_version = @import_version
  AND generation_id = @expected_generation_id
"""

# Gives every manifest row published before generations existed (slice 1)
# a ``generation_id``. The id is derived from the row key rather than
# minted at random so that two importers upgrading one dataset at once
# converge on the same value: the backfill is idempotent whichever of them
# commits first (or both do). ``import_version`` cannot contain ``:``
# (``_IMPORT_VERSION_PATTERN``), so the key is unambiguous.
_BACKFILL_GENERATION_QUERY = """\
UPDATE `{manifest_table}`
SET generation_id = TO_HEX(MD5(CONCAT(
    'evalbench-import-manifest:', import_version, ':', job_id)))
WHERE generation_id IS NULL
"""

# The managed view's query text (``Table.view_query``): the pin comment on
# the first line, the pinned ``failed_sessions_sql`` below it.
_VIEW_BODY = """\
{pin_comment}
{query}"""

# Seeds the lock sentinel outside the publish transaction. Two importers that
# both find the table empty may each insert a sentinel (INSERTs never
# conflict); that is harmless because the claim UPDATE below matches every
# sentinel row, so both transactions still mutate the same row(s).
_SEED_LOCK_QUERY = """\
INSERT INTO `{lock_table}` (lock_id, claim_count)
SELECT '{lock_id}', 0
FROM UNNEST([1])
WHERE NOT EXISTS (
  SELECT 1 FROM `{lock_table}` WHERE lock_id = '{lock_id}'
)
"""

# One multi-statement script publishes events, scores, and the manifest
# together. Its first statement claims the dataset lock by UPDATE-ing the
# pre-existing sentinel row. Per BigQuery's transaction concurrency contract
# ("if a transaction mutates rows in a table, other transactions that mutate
# rows in the same table cannot run concurrently; conflicting transactions
# are cancelled") at most one of two concurrent publishes survives, even when
# both began from a snapshot that showed no manifest row. The manifest guard
# then runs inside the same transaction as defence in depth for an importer
# whose *pre-publish* read was stale but whose transaction started after the
# other commit. It classifies the committed manifest row for this
# ``(job_id, import_version)`` as one of three cases:
#
# * absent      -- publish the staged rows;
# * conflicting -- fingerprints, source provenance (without ``replace``) or
#                  destination tables differ: RAISE, nothing is written;
# * identical   -- without ``replace`` the first publish is left exactly as
#                  it is: RAISE ``_PUBLISH_UNCHANGED_MESSAGE`` so the script
#                  rolls back (including the lock claim) and the caller
#                  reports ``unchanged`` instead of deleting and re-inserting
#                  equal rows under a new ``imported_at``.
#
# DML inside BEGIN/COMMIT is all-or-nothing, so a failure anywhere leaves the
# previously published version (or nothing) in place.
_PUBLISH_CONFLICT_MESSAGE = (
    "evalbench import conflict: this import_version was already published"
    " with different source fingerprints, source project/dataset, or"
    " destination tables"
)
_PUBLISH_UNCHANGED_MESSAGE = (
    "evalbench import unchanged: this import_version is already published"
    " with identical source fingerprints, source project/dataset, and"
    " destination tables"
)
_LOCK_MISSING_MESSAGE = (
    "evalbench import lock sentinel is missing; the publish transaction"
    " cannot serialize against concurrent imports"
)
# Substring of the error BigQuery raises for the transaction it cancels when
# two transactions mutate the same rows ("Transaction is aborted due to
# concurrent update against table ...").
_CONCURRENT_UPDATE_MARKER = "concurrent update"
_DESTINATION_CONFLICT_PREDICATE = (
    "events_table != @events_table OR scores_table != @scores_table"
)
_FINGERPRINT_CONFLICT_PREDICATE = (
    "results_fingerprint != @results_fingerprint"
    " OR scores_fingerprint != @scores_fingerprint"
    " OR configs_fingerprint != @configs_fingerprint"
)
_PROVENANCE_CONFLICT_PREDICATE = (
    "source_project != @source_project OR source_dataset != @source_dataset"
)
# Rendered into ``_PUBLISH_SCRIPT`` without ``replace``: an identical,
# already-published version must not be rewritten.
_IDENTICAL_GUARD = """\
  IF existing_manifest_rows > 0 THEN
    RAISE USING MESSAGE = '{unchanged_message}';
  END IF;
"""
_REPLACE_IDENTICAL_NOTE = (
    "  -- replace=True: an identical version is re-published in place.\n"
)
_PUBLISH_SCRIPT = """\
DECLARE existing_manifest_rows INT64 DEFAULT 0;
DECLARE conflicting_manifest_rows INT64 DEFAULT 0;
DECLARE prior_generations ARRAY<STRING> DEFAULT [];
BEGIN
  BEGIN TRANSACTION;
  UPDATE `{lock_table}`
  SET claim_count = claim_count + 1,
      claimed_at = CURRENT_TIMESTAMP(),
      claimed_job_id = @job_id,
      claimed_import_version = @import_version
  WHERE lock_id = '{lock_id}';
  IF @@row_count = 0 THEN
    RAISE USING MESSAGE = '{lock_missing_message}';
  END IF;
  SET existing_manifest_rows = (
    SELECT COUNT(*)
    FROM `{manifest_table}`
    WHERE job_id = @job_id
      AND import_version = @import_version
  );
  SET conflicting_manifest_rows = (
    SELECT COUNT(*)
    FROM `{manifest_table}`
    WHERE job_id = @job_id
      AND import_version = @import_version
      AND ({conflict_predicate})
  );
  IF conflicting_manifest_rows > 0 THEN
    RAISE USING MESSAGE = '{conflict_message}';
  END IF;
{identical_guard}\
  -- The generation this publish supersedes (if any) joins the row's
  -- committed history, so a view still pinned to it stays verifiable.
  SET prior_generations = IFNULL((
    SELECT ARRAY_CONCAT(
        IFNULL(superseded_generations, []),
        [TO_JSON_STRING(STRUCT(
            generation_id AS generation_id, view_policy AS view_policy))])
    FROM `{manifest_table}`
    WHERE job_id = @job_id AND import_version = @import_version
  ), []);
  DELETE FROM `{events_table}`
  WHERE job_id = @job_id AND import_version = @import_version;
  DELETE FROM `{scores_table}`
  WHERE job_id = @job_id AND import_version = @import_version;
  DELETE FROM `{manifest_table}`
  WHERE job_id = @job_id AND import_version = @import_version;
  INSERT INTO `{events_table}` ({event_columns})
  SELECT {event_columns} FROM `{events_staging}`;
  INSERT INTO `{scores_table}` ({score_columns})
  SELECT {score_columns} FROM `{scores_staging}`;
  INSERT INTO `{manifest_table}` ({manifest_columns}, superseded_generations)
  SELECT {manifest_columns}, prior_generations FROM `{manifest_staging}`;
  COMMIT TRANSACTION;
EXCEPTION WHEN ERROR THEN
  ROLLBACK TRANSACTION;
  RAISE;
END;
"""

_FAILED_SESSIONS_BASE = """\
WITH sessions AS (
  SELECT
    session_id,
    ANY_VALUE(JSON_VALUE(attributes, '$.evalbench_scenario_id')) AS scenario_id,
    MIN(timestamp) AS started_at,
    LOGICAL_OR(status = 'ERROR') AS process_failed,
    NOT LOGICAL_OR(event_type = 'AGENT_COMPLETED') AS missing_completion
  FROM `{events_table}`
  WHERE job_id = @job_id AND import_version = @import_version
  GROUP BY session_id
)"""

_FAILED_SESSIONS_POLICY = """,
policy AS (
  SELECT comparator, min_score
  FROM UNNEST([
{policy_rows}
  ])
),
comparator_gate AS (
  SELECT
    s.session_id,
    p.comparator,
    LOGICAL_OR({fail_predicate}) AS failing,
    MIN(IF(sc.score < p.min_score, sc.score, NULL)) AS failing_score
  FROM sessions AS s
  CROSS JOIN policy AS p
  LEFT JOIN `{scores_table}` AS sc
    ON sc.job_id = @job_id
    AND sc.import_version = @import_version
    AND sc.session_id = s.session_id
    AND sc.comparator = p.comparator
  GROUP BY s.session_id, p.comparator
),
score_gate AS (
  SELECT
    session_id,
    COUNTIF(failing) AS failing_score_count,
    ARRAY_AGG(
      IF(
        failing,
        STRUCT(comparator AS comparator, failing_score AS score),
        NULL
      )
      IGNORE NULLS
      ORDER BY comparator
    ) AS failing_scores
  FROM comparator_gate
  GROUP BY session_id
)
SELECT
  s.session_id,
  s.scenario_id,
  s.started_at,
  s.process_failed,
  s.missing_completion,
  COALESCE(g.failing_score_count, 0) > 0 AS score_failed,
  (
    s.process_failed
    OR s.missing_completion
    OR COALESCE(g.failing_score_count, 0) > 0
  ) AS failed,
  g.failing_scores
FROM sessions AS s
LEFT JOIN score_gate AS g USING (session_id)
ORDER BY s.session_id
"""

_FAILED_SESSIONS_NO_POLICY = """
SELECT
  session_id,
  scenario_id,
  started_at,
  process_failed,
  missing_completion,
  FALSE AS score_failed,
  (process_failed OR missing_completion) AS failed,
  ARRAY<STRUCT<comparator STRING, score FLOAT64>>[] AS failing_scores
FROM sessions
ORDER BY session_id
"""


@dataclasses.dataclass(frozen=True)
class EvalBenchRun:
  """One EvalBench job loaded for conversion to BQAA trace rows.

  ``results`` and ``scores`` retain the source rows as plain Python mappings.
  ``config_rows`` carries EvalBench's flattened experiment/model settings,
  which provide the run timestamp and agent identity for synthetic events.
  ``snapshot_at`` records the BigQuery time-travel timestamp the three source
  tables were read at, when the caller pinned one.
  """

  project_id: str
  evalbench_dataset: str
  job_id: str
  location: Optional[str] = None
  results: tuple[dict[str, Any], ...] = dataclasses.field(
      default_factory=tuple, repr=False
  )
  scores: tuple[dict[str, Any], ...] = dataclasses.field(
      default_factory=tuple, repr=False
  )
  config_rows: tuple[dict[str, Any], ...] = dataclasses.field(
      default_factory=tuple, repr=False
  )
  # Declared last so the v0.5.1 positional order
  # (project, dataset, job, location, results, scores, config_rows) is stable.
  snapshot_at: Optional[datetime] = None

  @classmethod
  def from_bigquery(
      cls,
      *,
      project_id: str,
      evalbench_dataset: str,
      job_id: str,
      location: Optional[str] = None,
      snapshot_at: Optional[datetime] = None,
      bq_client: Optional[Any] = None,
  ) -> "EvalBenchRun":
    """Load one EvalBench run's configs, results, and scores.

    Every source query filters on ``job_id`` in BigQuery rather than loading
    a whole table and filtering in Python. The current contract intentionally
    loads one run into memory; paging very large runs is a future extension.

    The three tables are read sequentially. If the EvalBench job is still
    writing, results, scores, and configs can come from different moments;
    either wait for the job to complete before importing or pass
    ``snapshot_at`` so every read uses ``FOR SYSTEM_TIME AS OF`` the same
    instant. ``materialize`` records the content fingerprints either way, so
    a source that changed after import produces a new ``import_version``
    rather than silently reproducing the first one.

    Args:
      project_id: Project containing the EvalBench tables.
      evalbench_dataset: Dataset containing ``configs``, ``results``, and
        ``scores``.
      job_id: EvalBench job identifier to load.
      location: Optional BigQuery location.
      snapshot_at: Optional timezone-aware timestamp; when set, all three
        source reads use BigQuery time travel as of this instant.
      bq_client: Optional test-compatible or caller-configured BigQuery client.

    Returns:
      An ``EvalBenchRun`` containing plain in-memory source rows.
    """
    _validate_source_segment("project_id", project_id)
    _validate_source_segment("evalbench_dataset", evalbench_dataset)
    if not isinstance(job_id, str) or not job_id:
      raise ValueError("job_id must be a non-empty string")
    if snapshot_at is not None and (
        not isinstance(snapshot_at, datetime) or snapshot_at.tzinfo is None
    ):
      raise ValueError("snapshot_at must be a timezone-aware datetime")

    client = bq_client or make_bq_client(project_id, location=location)
    table_prefix = f"{project_id}.{evalbench_dataset}"
    read_args: dict[str, Any] = {
        "job_id": job_id,
        "location": location,
        "snapshot_at": snapshot_at,
    }
    return cls(
        project_id=project_id,
        evalbench_dataset=evalbench_dataset,
        job_id=job_id,
        location=location,
        snapshot_at=snapshot_at,
        results=_read_source_rows(
            client, table_id=f"{table_prefix}.results", **read_args
        ),
        scores=_read_source_rows(
            client, table_id=f"{table_prefix}.scores", **read_args
        ),
        config_rows=_read_source_rows(
            client, table_id=f"{table_prefix}.configs", **read_args
        ),
    )

  def to_agent_event_rows(
      self, *, import_version: Optional[str] = None
  ) -> list[dict[str, Any]]:
    """Convert loaded results to BQAA-compatible synthetic event rows.

    Supports both EvalBench's NL2SQL field names
    (``id``/``nl_prompt``/``generated_sql``) and its current agentic names
    (``eval_id``/``prompt``/``stdout``). Missing tool calls emit no tool rows;
    missing final output omits ``AGENT_COMPLETED``. A missing prompt or
    scenario identifier is a hard error because no valid session can be
    constructed without them.

    ``session_id``/``trace_id`` are ``evalbench:{job_id}:{scenario_id}`` for
    a plain read, and the invocation/span ids hash that identity exactly as
    v0.5.1 did, so re-mapping an existing run keeps its stored trace
    references. When ``import_version`` is given (as ``materialize`` does)
    the identity moves to the distinct published namespace
    ``evalbench-import:{job_id}:{import_version}:{scenario_id}`` and every
    synthetic span id derives from it with injective framing, so retained
    import versions of one job never share a trace or session in the mirror
    table and readers that filter only by ``trace_id`` see exactly one
    version. Each published component is escaped (see ``_session_identity``)
    so a ``:`` inside ``import_version`` or ``scenario_id`` cannot make two
    different ``(job_id, import_version, scenario_id)`` tuples share an
    identity, and no published identity can equal a plain-read one.
    """
    if import_version is not None:
      _validate_import_version(import_version)
      stable_id = _published_stable_id
    else:
      stable_id = _stable_id
    config = _config_values(self.config_rows)
    agent = _agent_name(config)
    config_run_time = _first_run_time(self.config_rows)

    prepared: list[tuple[str, int, dict[str, Any]]] = []
    scenario_indexes: dict[str, int] = {}
    for source_index, result in enumerate(self.results):
      scenario_id = _scenario_id(result)
      previous_index = scenario_indexes.get(scenario_id)
      if previous_index is not None:
        raise ValueError(
            f"EvalBench job {self.job_id!r} contains duplicate scenario id "
            f"{scenario_id!r} at result indexes {previous_index} and "
            f"{source_index}"
        )
      scenario_indexes[scenario_id] = source_index
      prepared.append((scenario_id, source_index, result))

    rows: list[dict[str, Any]] = []
    for scenario_id, _, result in sorted(
        prepared, key=lambda item: (item[0], item[1])
    ):
      prompt = _prompt(result)
      if prompt is None:
        raise ValueError(
            f"EvalBench scenario {scenario_id!r} is missing nl_prompt/prompt"
        )

      run_time = _result_run_time(result) or config_run_time
      missing_run_time = run_time is None
      run_time = run_time or _UNKNOWN_RUN_TIME
      session_id = _session_identity(
          self.job_id, scenario_id, import_version=import_version
      )
      invocation_id = stable_id(session_id, "invocation", length=32)
      root_span_id = stable_id(session_id, "user", length=16)
      attributes = _base_attributes(
          result=result,
          project_id=self.project_id,
          dataset_id=self.evalbench_dataset,
          job_id=self.job_id,
          scenario_id=scenario_id,
          agent=agent,
      )
      if import_version is not None:
        attributes["evalbench_import_version"] = import_version
      if missing_run_time:
        attributes["evalbench_run_time_missing"] = True

      error_fields = _source_error_fields(result)
      if error_fields:
        attributes["evalbench_error_fields"] = error_fields
      source_error = _source_error_message(result, error_fields)
      source_status = "ERROR" if source_error else "OK"
      final_response = _final_response(result)
      usage, response_latency = _usage_and_latency(result)
      prompt_attributes = dict(attributes)
      if final_response is None:
        prompt_attributes.update(usage)

      rows.append(
          _event_row(
              event_type="USER_MESSAGE_RECEIVED",
              timestamp=run_time,
              agent=agent,
              session_id=session_id,
              invocation_id=invocation_id,
              span_id=root_span_id,
              parent_span_id=None,
              content={"text": prompt, "text_summary": prompt},
              attributes=prompt_attributes,
              latency_ms=(response_latency if final_response is None else None),
              status=source_status if final_response is None else "OK",
              error_message=source_error if final_response is None else None,
          )
      )

      sequence = 1
      for tool_index, tool_call in enumerate(_tool_calls(result)):
        tool_name = tool_call["tool_name"]
        tool_args = tool_call.get("args") or {}
        tool_result = tool_call.get("result")
        tool_error = _usable_text(tool_call.get("error"))
        tool_status = "ERROR" if tool_error else "OK"
        tool_span_id = stable_id(session_id, "tool", str(tool_index), length=16)
        start_summary = f"{tool_name}({_compact_json(tool_args)})"
        rows.append(
            _event_row(
                event_type="TOOL_STARTING",
                timestamp=run_time + timedelta(microseconds=sequence),
                agent=agent,
                session_id=session_id,
                invocation_id=invocation_id,
                span_id=tool_span_id,
                parent_span_id=root_span_id,
                content={
                    "tool": tool_name,
                    "args": _json_safe(tool_args),
                    "text_summary": start_summary,
                },
                attributes=attributes,
            )
        )
        sequence += 1

        rendered_result = tool_result if tool_result is not None else tool_error
        result_summary = f"{tool_name} -> {_one_line(rendered_result)}"
        rows.append(
            _event_row(
                event_type="TOOL_ERROR" if tool_error else "TOOL_COMPLETED",
                timestamp=run_time + timedelta(microseconds=sequence),
                agent=agent,
                session_id=session_id,
                invocation_id=invocation_id,
                span_id=tool_span_id,
                parent_span_id=root_span_id,
                content={
                    "tool": tool_name,
                    "result": _json_safe(rendered_result),
                    "text_summary": result_summary,
                },
                attributes=attributes,
                latency_ms=_tool_latency(tool_call),
                status=tool_status,
                error_message=tool_error,
            )
        )
        sequence += 1

      if final_response is None:
        continue

      response_attributes = dict(attributes)
      response_attributes.update(usage)
      rows.append(
          _event_row(
              event_type="AGENT_COMPLETED",
              timestamp=run_time + timedelta(microseconds=sequence),
              agent=agent,
              session_id=session_id,
              invocation_id=invocation_id,
              span_id=stable_id(session_id, "agent-completed", length=16),
              parent_span_id=root_span_id,
              content={
                  "response": final_response,
                  "text_summary": final_response,
              },
              attributes=response_attributes,
              latency_ms=response_latency,
              status=source_status,
              error_message=source_error,
          )
      )

    return rows

  def to_score_rows(self, *, import_version: str) -> list[dict[str, Any]]:
    """Normalize loaded EvalBench score rows for ``evalbench_scores_imported``.

    Each source row is preserved verbatim in ``source_row``; ``scenario_id``,
    ``session_id``, ``comparator``, and a float ``score`` are lifted out so the
    failed-session view can join scores to events without parsing JSON.
    ``session_id`` uses the same version-specific identity as
    ``to_agent_event_rows(import_version=...)`` so score joins stay aligned.
    Unparseable scores become ``NULL`` rather than being dropped.
    """
    _validate_import_version(import_version)
    prepared: list[tuple[tuple[str, str, str], dict[str, Any]]] = []
    for score in self.scores:
      scenario_id = _score_scenario_id(score)
      comparator = _score_comparator(score)
      source_row = _json_safe(score)
      row = {
          "job_id": self.job_id,
          "import_version": import_version,
          "scenario_id": scenario_id,
          "session_id": (
              _session_identity(
                  self.job_id, scenario_id, import_version=import_version
              )
              if scenario_id is not None
              else None
          ),
          "comparator": comparator,
          "score": _score_value(score.get("score")),
          "source_row": source_row,
      }
      sort_key = (
          scenario_id or "",
          comparator or "",
          _canonical_json(source_row),
      )
      prepared.append((sort_key, row))
    return [row for _, row in sorted(prepared, key=lambda item: item[0])]

  def fingerprints(self) -> dict[str, str]:
    """Order-independent SHA-256 fingerprints of the loaded source rows."""
    return {
        "results_fingerprint": _fingerprint_rows(self.results),
        "scores_fingerprint": _fingerprint_rows(self.scores),
        "configs_fingerprint": _fingerprint_rows(self.config_rows),
    }

  def materialize(
      self,
      *,
      target_dataset: str,
      target_project: Optional[str] = None,
      events_table: str = DEFAULT_EVENTS_TABLE,
      scores_table: str = DEFAULT_SCORES_TABLE,
      import_version: Optional[str] = None,
      replace: bool = False,
      imported_at: Optional[datetime] = None,
      failed_sessions_view: Optional[str] = DEFAULT_FAILED_SESSIONS_VIEW,
      policy: Optional["EvalScorePolicy"] = None,
      bq_client: Optional[Any] = None,
  ) -> "EvalBenchImportResult":
    """Publish this run as one immutable import version in BQAA-owned tables.

    The target is always a BQAA-owned mirror (default
    ``evalbench_agent_events`` / ``evalbench_scores_imported``); the ADK
    plugin's production ``agent_events`` table is never written.
    ``target_project`` may differ from the source ``project_id``. The target
    dataset must already exist; the three tables are created on first use.

    The manifest is the single import registry of the target dataset
    (``MANIFEST_TABLE``, ``evalbench_import_manifest``) and is not
    caller-selectable: every import into the dataset, whatever
    ``events_table``/``scores_table`` it writes, checks the same registry
    before deleting any published rows, so one version cannot be
    re-published around an earlier manifest row. The dataset's publish lock
    (``LOCK_TABLE``, ``evalbench_import_lock``) is fixed for the same reason.

    Publishing is atomic: event, score, and manifest rows are loaded into
    per-import staging tables, then a single multi-statement transaction
    deletes any prior rows for ``(job_id, import_version)`` and inserts the
    staged rows. A failure before ``COMMIT`` leaves the target untouched, and
    staging tables are always dropped.

    Idempotency is driven by the manifest. ``import_version`` defaults to a
    content fingerprint of the source rows, so an unchanged source is a
    no-op (``status == "unchanged"``) and a changed source becomes a new
    version. An explicit ``import_version`` whose stored fingerprints, or
    whose stored source project/dataset, no longer match this run raises
    ``ValueError`` unless ``replace=True``: equal rows read from another
    EvalBench project or dataset publish different
    ``attributes.evalbench_source_*`` values, so they are a different
    version, not the same one. The publish transaction first claims the
    dataset lock by updating its pre-existing sentinel row, so two importers
    racing on a first-time version cannot both commit: BigQuery cancels the
    second transaction that mutates the same row, and the cancelled importer
    raises ``ValueError`` with nothing written. The same
    absent/identical/conflicting decision is then repeated inside the
    transaction against its own snapshot, so an importer whose pre-read was
    stale still reports ``unchanged`` for an identical version (leaving the
    first publish untouched) and raises for a conflicting one. A published
    version is bound to the ``events_table`` /
    ``scores_table`` recorded in its manifest: re-importing it with other
    destination tables raises (even with ``replace=True``) instead of
    reporting a no-op against tables that were never written or orphaning
    the old rows; use a new ``import_version`` to publish elsewhere.

    Every successful call (``imported``, ``replaced`` *and* ``unchanged``)
    also keeps the dataset's failed-session VIEW (``failed_sessions_view``,
    default ``evalbench_failed_sessions``) pinned to this job's latest
    successful import: ``failed_sessions_sql`` rendered with the
    ``(job_id, import_version)`` of the newest manifest row as literals, so
    SQL consumers never scan another version. The view is only rewritten
    when its rendered definition would change (pinned version, ``policy``,
    or the SQL itself), so a no-op re-import leaves it untouched, and it is
    created on the next call for corpora published before views existed.
    Ownership is proven by the definition, not by the pin comment alone: a
    view at that name pinned to *another* job, or any object there whose
    query is not byte-for-byte what the importer rendered (a table, a
    foreign view, a copied pin over other SQL, an edited or reformatted
    managed view), raises ``ValueError`` before anything is written -- pass
    a different ``failed_sessions_view`` per job, or ``None`` to manage no
    view. The gate the view renders is committed manifest state, not view
    state: every publish records ``policy`` in its manifest row
    (``view_policy``) under a fresh opaque ``generation_id`` (a ``replace``
    of the same version label is a new generation), an ``unchanged``
    re-import with another policy commits it to the row it found -- only
    if that row's generation is still the one it found -- under a new
    generation, and the view is always rendered from the latest manifest
    row. The view is written with create-if-absent and ETag-conditional
    replace after re-reading it and the latest manifest, so concurrent
    imports of one job cannot leave a stale pin or an older caller's
    policy behind.

    Args:
      target_dataset: BQAA-owned dataset that receives the mirror tables.
      target_project: Target project; defaults to the source ``project_id``.
      events_table: Mirror event table name in ``target_dataset``.
      scores_table: Imported score table name in ``target_dataset``.
      import_version: Optional caller-chosen version label. Defaults to the
        first 16 hex digits of the combined source fingerprint.
      replace: Re-publish even when an identical version already exists
        (``status == "replaced"``), or when an explicit version's source
        fingerprints changed.
      imported_at: Manifest timestamp; defaults to now (UTC).
      failed_sessions_view: Name of the failed-session view kept in
        ``target_dataset`` (see above); ``None`` skips view maintenance.
      policy: Optional ``EvalScorePolicy`` rendered into the view so that
        score failures count alongside process failures.
      bq_client: Optional test-compatible or caller-configured BigQuery
        client for the target project.

    Returns:
      An ``EvalBenchImportResult`` with the published version and manifest.
    """
    target_project = target_project or self.project_id
    _validate_source_segment("target_project", target_project)
    _validate_source_segment("target_dataset", target_dataset)
    _validate_destination_table("events_table", events_table)
    _validate_destination_table("scores_table", scores_table)
    if failed_sessions_view is not None:
      _validate_destination_table("failed_sessions_view", failed_sessions_view)
      if failed_sessions_view in (
          events_table,
          scores_table,
          MANIFEST_TABLE,
          LOCK_TABLE,
      ):
        raise ValueError(
            f"failed_sessions_view {failed_sessions_view!r} must not name an"
            " import table"
        )
    if imported_at is None:
      imported_at = datetime.now(timezone.utc)
    elif imported_at.tzinfo is None:
      raise ValueError("imported_at must be a timezone-aware datetime")

    fingerprints = self.fingerprints()
    if import_version is None:
      import_version = _derived_import_version(fingerprints)
    _validate_import_version(import_version)
    generation_id = _new_generation_id()

    prefix = f"{target_project}.{target_dataset}"
    events_ref = f"{prefix}.{events_table}"
    scores_ref = f"{prefix}.{scores_table}"
    manifest_ref = f"{prefix}.{MANIFEST_TABLE}"
    lock_ref = f"{prefix}.{LOCK_TABLE}"
    view_ref = (
        f"{prefix}.{failed_sessions_view}"
        if failed_sessions_view is not None
        else None
    )

    # Map before touching BigQuery so mapping errors never leave staging
    # tables behind or partially created targets.
    event_rows = [
        {**row, "job_id": self.job_id, "import_version": import_version}
        for row in self.to_agent_event_rows(import_version=import_version)
    ]
    score_rows = self.to_score_rows(import_version=import_version)
    manifest = {
        "job_id": self.job_id,
        "import_version": import_version,
        "source_project": self.project_id,
        "source_dataset": self.evalbench_dataset,
        "source_snapshot_at": (
            self.snapshot_at.isoformat() if self.snapshot_at else None
        ),
        "results_count": len(self.results),
        "scores_count": len(self.scores),
        "configs_count": len(self.config_rows),
        **fingerprints,
        "events_table": events_ref,
        "scores_table": scores_ref,
        "event_row_count": len(event_rows),
        "score_row_count": len(score_rows),
        "imported_at": imported_at.isoformat(),
        "generation_id": generation_id,
        "view_policy": _policy_column(policy),
        # Staged as empty; the publish transaction carries the replaced
        # row's history forward (``_PUBLISH_SCRIPT``).
        "superseded_generations": [],
    }

    client = bq_client or make_bq_client(target_project, location=self.location)

    # A manifest created by an earlier release (slice 1: no generations, no
    # view policy, no history) is upgraded in place before anything reads
    # or writes it, so an unchanged re-import finds a generation and a
    # publish finds its columns. Nothing is created here.
    _ensure_manifest_schema(
        client, manifest_ref=manifest_ref, location=self.location
    )

    # The view binding is checked before any table is created or written so
    # a name clash with another job's view (or a foreign object) aborts
    # before the importer touches the dataset. ``_sync_failed_sessions_view``
    # re-reads it right before writing; this early read is the fail-fast
    # path, not the authority.
    if view_ref is not None:
      _check_view_binding(
          client,
          view_ref=view_ref,
          manifest_ref=manifest_ref,
          job_id=self.job_id,
          location=self.location,
      )

    _ensure_import_tables(
        client,
        events_ref=events_ref,
        scores_ref=scores_ref,
        manifest_ref=manifest_ref,
        lock_ref=lock_ref,
    )

    def finish(result: "EvalBenchImportResult") -> "EvalBenchImportResult":
      if view_ref is None:
        return result
      return dataclasses.replace(
          result,
          failed_sessions_view=_sync_failed_sessions_view(
              client,
              view_ref=view_ref,
              manifest_ref=manifest_ref,
              job_id=self.job_id,
              policy=policy,
              location=self.location,
              status=result.status,
              import_version=import_version,
              # The row this call handled: the one it committed, or for
              # ``unchanged`` the committed row it found.
              manifest=result.manifest,
          ),
      )

    existing = _read_manifest(
        client,
        manifest_ref=manifest_ref,
        job_id=self.job_id,
        import_version=import_version,
        location=self.location,
    )
    if existing is not None and existing.get("generation_id") is None:
      # An upgrade interrupted between the schema change and the backfill
      # left this row without a generation; finish it (idempotently).
      _backfill_generation_ids(
          client, manifest_ref=manifest_ref, location=self.location
      )
      existing = _read_manifest(
          client,
          manifest_ref=manifest_ref,
          job_id=self.job_id,
          import_version=import_version,
          location=self.location,
      )
    status = "imported"
    if existing is not None:
      _check_destination_binding(
          existing,
          job_id=self.job_id,
          import_version=import_version,
          events_ref=events_ref,
          scores_ref=scores_ref,
      )
      drift = _manifest_drift(existing, manifest)
      if drift and not replace:
        raise ValueError(
            f"EvalBench job {self.job_id!r} import_version "
            f"{import_version!r} already exists with different "
            f"{' and '.join(drift)}; pass a new import_version (or omit it "
            "to derive one from the source content), or replace=True to "
            "overwrite it"
        )
      if not drift and not replace:
        return finish(
            self._unchanged_result(
                existing,
                import_version=import_version,
                events_ref=events_ref,
                scores_ref=scores_ref,
                manifest_ref=manifest_ref,
            )
        )
      status = "replaced"

    staging_suffix = f"_staging_{uuid.uuid4().hex[:8]}"
    events_staging = events_ref + staging_suffix
    scores_staging = scores_ref + staging_suffix
    manifest_staging = manifest_ref + staging_suffix
    try:
      _load_staging(client, events_staging, event_rows, _event_schema())
      _load_staging(
          client, scores_staging, score_rows, _schema(_SCORE_SCHEMA_FIELDS)
      )
      _load_staging(
          client,
          manifest_staging,
          [manifest],
          _schema(_MANIFEST_SCHEMA_FIELDS),
      )
      # The sentinel must exist *before* the transaction starts so the
      # claim UPDATE mutates a row visible in the transaction snapshot.
      _seed_import_lock(client, lock_ref=lock_ref, location=self.location)
      script = _publish_script(
          events_ref=events_ref,
          scores_ref=scores_ref,
          manifest_ref=manifest_ref,
          lock_ref=lock_ref,
          events_staging=events_staging,
          scores_staging=scores_staging,
          manifest_staging=manifest_staging,
          replace=replace,
      )
      job_config = bigquery.QueryJobConfig(
          query_parameters=_publish_parameters(
              job_id=self.job_id,
              import_version=import_version,
              fingerprints=fingerprints,
              source_project=self.project_id,
              source_dataset=self.evalbench_dataset,
              events_ref=events_ref,
              scores_ref=scores_ref,
          )
      )
      job_config = with_sdk_labels(job_config, feature=_IMPORT_FEATURE)
      query_args: dict[str, Any] = {"job_config": job_config}
      if self.location is not None:
        query_args["location"] = self.location
      try:
        client.query(script, **query_args).result()
      except ValueError:
        raise
      except Exception as exc:  # noqa: BLE001
        if _PUBLISH_UNCHANGED_MESSAGE in str(exc):
          # The transaction found an identical committed version that the
          # stale pre-read missed and rolled itself back. Report the
          # version that is actually published; the manifest row cannot
          # have vanished since (nothing deletes manifest rows), so the
          # local copy is only a defensive fallback.
          committed = _read_manifest(
              client,
              manifest_ref=manifest_ref,
              job_id=self.job_id,
              import_version=import_version,
              location=self.location,
          )
          return finish(
              self._unchanged_result(
                  committed if committed is not None else manifest,
                  import_version=import_version,
                  events_ref=events_ref,
                  scores_ref=scores_ref,
                  manifest_ref=manifest_ref,
              )
          )
        if _PUBLISH_CONFLICT_MESSAGE in str(exc):
          raise ValueError(
              f"EvalBench job {self.job_id!r} import_version "
              f"{import_version!r} was published concurrently with different "
              "source fingerprints, source project/dataset, or destination "
              "tables; nothing was written. Re-run to get status "
              "'unchanged', or pass a new import_version (or omit it to "
              "derive one from the source)"
          ) from exc
        if _CONCURRENT_UPDATE_MARKER in str(exc).lower():
          raise ValueError(
              f"EvalBench job {self.job_id!r} import_version "
              f"{import_version!r}: BigQuery cancelled this publish because a "
              f"concurrent import into {prefix!r} claimed the import lock "
              f"({lock_ref!r}) first; nothing was written. Re-run: the "
              "result is 'unchanged' if the other import published identical "
              "content, otherwise the fingerprint conflict is reported"
          ) from exc
        raise
    finally:
      # Staging cleanup is best-effort: a failed DROP must not turn a
      # committed publish into an error (or mask the real failure), and the
      # staging tables carry an expiration so leftovers cannot accumulate.
      _drop_staging_tables(
          client, (events_staging, scores_staging, manifest_staging)
      )

    # The committed row is what the result reports: the transaction adds
    # the generation history a ``replace`` supersedes, which is not known
    # from the (possibly stale) pre-read. Nothing deletes manifest rows, so
    # the local copy is only a defensive fallback.
    committed = _read_manifest(
        client,
        manifest_ref=manifest_ref,
        job_id=self.job_id,
        import_version=import_version,
        location=self.location,
    )
    return finish(
        EvalBenchImportResult(
            job_id=self.job_id,
            import_version=import_version,
            status=status,
            events_table=events_ref,
            scores_table=scores_ref,
            manifest_table=manifest_ref,
            event_row_count=len(event_rows),
            score_row_count=len(score_rows),
            manifest=committed if committed is not None else manifest,
        )
    )

  def _unchanged_result(
      self,
      existing: Mapping[str, Any],
      *,
      import_version: str,
      events_ref: str,
      scores_ref: str,
      manifest_ref: str,
  ) -> "EvalBenchImportResult":
    """The ``unchanged`` outcome for an already-published identical version."""
    return EvalBenchImportResult(
        job_id=self.job_id,
        import_version=import_version,
        status="unchanged",
        events_table=events_ref,
        scores_table=scores_ref,
        manifest_table=manifest_ref,
        event_row_count=int(existing.get("event_row_count") or 0),
        score_row_count=int(existing.get("score_row_count") or 0),
        manifest=dict(existing),
    )


@dataclasses.dataclass(frozen=True)
class EvalBenchImportResult:
  """Outcome of ``EvalBenchRun.materialize``.

  ``status`` is ``"imported"`` for a new version, ``"replaced"`` when an
  existing version was atomically overwritten, and ``"unchanged"`` when the
  manifest already recorded this version with identical fingerprints and
  nothing was written.

  ``failed_sessions_view`` is the fully-qualified failed-session view the
  call left pinned to this job's latest successful import, or ``None`` when
  view maintenance was disabled.
  """

  job_id: str
  import_version: str
  status: str
  events_table: str
  scores_table: str
  manifest_table: str
  event_row_count: int
  score_row_count: int
  manifest: dict[str, Any] = dataclasses.field(default_factory=dict)
  failed_sessions_view: Optional[str] = None

  def to_dict(self) -> dict[str, Any]:
    return _json_safe(dataclasses.asdict(self))


@dataclasses.dataclass(frozen=True)
class EvalScorePolicy:
  """Per-benchmark pass policy over imported EvalBench scores.

  ``min_scores`` maps a score ``comparator`` name to the minimum score a
  session must reach on that comparator to pass. A session fails the score
  gate when any listed comparator scores below its threshold. With
  ``missing_score_fails`` (the default) a session with no score row for a
  listed comparator, or a ``NULL`` score, also fails: an EvalBench run that
  completed without being scored is not evidence of success.
  """

  min_scores: Mapping[str, float] = dataclasses.field(default_factory=dict)
  missing_score_fails: bool = True

  def __post_init__(self) -> None:
    normalized: dict[str, float] = {}
    for comparator, min_score in self.min_scores.items():
      if not isinstance(comparator, str) or not _COMPARATOR_PATTERN.fullmatch(
          comparator
      ):
        raise ValueError(
            f"comparator name {comparator!r} must match "
            f"{_COMPARATOR_PATTERN.pattern}"
        )
      if isinstance(min_score, bool) or not isinstance(min_score, (int, float)):
        raise ValueError(f"min_score for {comparator!r} must be a number")
      value = float(min_score)
      if not math.isfinite(value):
        raise ValueError(f"min_score for {comparator!r} must be finite")
      normalized[comparator] = value
    object.__setattr__(self, "min_scores", normalized)


@dataclasses.dataclass(frozen=True)
class SessionVerdict:
  """Failed-session classification for one imported EvalBench session.

  ``failed`` is true when the process failed (any ``ERROR`` event, which is
  how non-zero ``returncode`` and source error fields surface), when no
  ``AGENT_COMPLETED`` event exists, or when the score policy is not met.
  ``returncode == 0`` on its own never makes a session pass.
  """

  session_id: str
  scenario_id: Optional[str]
  process_failed: bool
  missing_completion: bool
  score_failed: bool
  failing_scores: dict[str, Optional[float]]
  failed: bool


def classify_sessions(
    event_rows: list[dict[str, Any]],
    score_rows: list[dict[str, Any]],
    policy: Optional[EvalScorePolicy] = None,
) -> list[SessionVerdict]:
  """In-memory reference implementation of ``failed_sessions_sql``.

  ``event_rows`` are ``to_agent_event_rows()`` rows and ``score_rows`` are
  ``to_score_rows()`` rows for one ``(job_id, import_version)``. The SQL and
  this function must agree; tests pin the contract here.
  """
  policy = policy or EvalScorePolicy()
  sessions: dict[str, dict[str, Any]] = {}
  for row in event_rows:
    session_id = row.get("session_id")
    if not session_id:
      continue
    state = sessions.setdefault(
        session_id,
        {
            "scenario_id": None,
            "process_failed": False,
            "missing_completion": True,
        },
    )
    attributes = _as_mapping(_structured(row.get("attributes")))
    scenario_id = _usable_text(attributes.get("evalbench_scenario_id"))
    if state["scenario_id"] is None and scenario_id is not None:
      state["scenario_id"] = scenario_id
    if row.get("status") == "ERROR":
      state["process_failed"] = True
    if row.get("event_type") == "AGENT_COMPLETED":
      state["missing_completion"] = False

  scores: dict[str, dict[str, list[Optional[float]]]] = {}
  for row in score_rows:
    session_id = row.get("session_id")
    comparator = row.get("comparator")
    if not session_id or comparator is None:
      continue
    scores.setdefault(session_id, {}).setdefault(comparator, []).append(
        _score_value(row.get("score"))
    )

  verdicts: list[SessionVerdict] = []
  for session_id in sorted(sessions):
    state = sessions[session_id]
    failing_scores: dict[str, Optional[float]] = {}
    for comparator, min_score in policy.min_scores.items():
      values = scores.get(session_id, {}).get(comparator, [])
      if not values:
        if policy.missing_score_fails:
          failing_scores[comparator] = None
        continue
      failing = [
          value for value in values if value is not None and value < min_score
      ]
      if failing:
        failing_scores[comparator] = min(failing)
      elif policy.missing_score_fails and any(v is None for v in values):
        failing_scores[comparator] = None
    score_failed = bool(failing_scores)
    verdicts.append(
        SessionVerdict(
            session_id=session_id,
            scenario_id=state["scenario_id"],
            process_failed=state["process_failed"],
            missing_completion=state["missing_completion"],
            score_failed=score_failed,
            failing_scores=failing_scores,
            failed=(
                state["process_failed"]
                or state["missing_completion"]
                or score_failed
            ),
        )
    )
  return verdicts


def failed_sessions_sql(
    *,
    target_project: str,
    target_dataset: str,
    events_table: str = DEFAULT_EVENTS_TABLE,
    scores_table: str = DEFAULT_SCORES_TABLE,
    policy: Optional[EvalScorePolicy] = None,
    job_id: Optional[str] = None,
    import_version: Optional[str] = None,
) -> str:
  """Return the failed-session denominator query for one pinned import.

  By default the query takes two parameters, ``@job_id`` and
  ``@import_version`` (see ``import_query_parameters``). Passing both
  ``job_id`` and ``import_version`` renders them as string literals instead,
  which is the form a BigQuery VIEW (no query parameters) needs; passing
  only one of them is an error, so a query can never be half-pinned.

  Each returned row is one session with ``process_failed``,
  ``missing_completion``, ``score_failed``, and the combined ``failed``
  flag plus the offending ``failing_scores``. Sessions whose EvalBench
  process exited with ``returncode == 0`` are *completed*; they count as
  passed only when ``policy`` is satisfied. Without a policy only process
  failures and missing completions are counted.
  """
  _validate_source_segment("target_project", target_project)
  _validate_source_segment("target_dataset", target_dataset)
  _validate_source_segment("events_table", events_table)
  _validate_source_segment("scores_table", scores_table)
  return _failed_sessions_sql_for_refs(
      events_ref=f"{target_project}.{target_dataset}.{events_table}",
      scores_ref=f"{target_project}.{target_dataset}.{scores_table}",
      policy=policy,
      job_id=job_id,
      import_version=import_version,
  )


def _failed_sessions_sql_for_refs(
    *,
    events_ref: str,
    scores_ref: str,
    policy: Optional[EvalScorePolicy],
    job_id: Optional[str] = None,
    import_version: Optional[str] = None,
) -> str:
  """``failed_sessions_sql`` over fully-qualified (manifest-bound) refs."""
  if (job_id is None) != (import_version is None):
    raise ValueError(
        "job_id and import_version must be pinned together (or both omitted"
        " to use @job_id/@import_version query parameters)"
    )
  policy = policy or EvalScorePolicy()
  sql = _FAILED_SESSIONS_BASE.format(events_table=events_ref)
  if not policy.min_scores:
    sql += _FAILED_SESSIONS_NO_POLICY
  else:
    policy_rows = ",\n".join(
        f"    STRUCT('{comparator}' AS comparator, {min_score!r} AS min_score)"
        for comparator, min_score in policy.min_scores.items()
    )
    if policy.missing_score_fails:
      fail_predicate = "(sc.score IS NULL OR sc.score < p.min_score)"
    else:
      fail_predicate = "(sc.score IS NOT NULL AND sc.score < p.min_score)"
    sql += _FAILED_SESSIONS_POLICY.format(
        policy_rows=policy_rows,
        fail_predicate=fail_predicate,
        scores_table=scores_ref,
    )
  if job_id is None:
    return sql
  if not isinstance(job_id, str) or not job_id:
    raise ValueError("job_id must be a non-empty string")
  _validate_import_version(import_version)
  return sql.replace("@job_id", _sql_string_literal(job_id)).replace(
      "@import_version", _sql_string_literal(import_version)
  )


def _sql_string_literal(value: str) -> str:
  """Render ``value`` as a GoogleSQL double-quoted string literal.

  JSON string escaping (backslash, double quote, newline, and ``\\u``
  escapes for the other control characters) is a subset of GoogleSQL's,
  and non-ASCII text is emitted verbatim rather than as ``\\u`` surrogate
  pairs, which GoogleSQL rejects. The result is always a single line.
  """
  return json.dumps(value, ensure_ascii=False)


# --- Failed-session view and version-pinned consumer (#435 slice 2) ---


def _policy_pin(policy: Optional[EvalScorePolicy]) -> Optional[dict[str, Any]]:
  """The score gate a view renders, in canonical form (``None`` = no gate)."""
  if policy is None or not policy.min_scores:
    return None
  return {
      "min_scores": {
          comparator: float(min_score)
          for comparator, min_score in sorted(policy.min_scores.items())
      },
      "missing_score_fails": bool(policy.missing_score_fails),
  }


def _canonical_policy(
    policy: Optional[EvalScorePolicy],
) -> Optional[EvalScorePolicy]:
  """``policy`` in the one form the view renders (sorted, float, or none).

  Rendering from the canonical form makes the view body a pure function of
  (manifest row, policy pin), whatever key order the caller used.
  """
  pin = _policy_pin(policy)
  if pin is None:
    return None
  return EvalScorePolicy(
      pin["min_scores"], missing_score_fails=pin["missing_score_fails"]
  )


def _policy_from_pin(value: Any) -> Optional[EvalScorePolicy]:
  """Parse a pin's ``policy`` strictly; ``ValueError`` if it is not one.

  Only exactly what ``_policy_pin`` emits is accepted (and it must
  round-trip), so a view carrying anything else is not the importer's.
  """
  if value is None:
    return None
  if not isinstance(value, dict) or set(value) != {
      "min_scores",
      "missing_score_fails",
  }:
    raise ValueError("policy pin must have min_scores and missing_score_fails")
  min_scores = value["min_scores"]
  missing_score_fails = value["missing_score_fails"]
  if (
      not isinstance(min_scores, dict)
      or not min_scores
      or not isinstance(missing_score_fails, bool)
  ):
    raise ValueError("policy pin is malformed")
  for comparator, min_score in min_scores.items():
    if (
        not isinstance(comparator, str)
        or isinstance(min_score, bool)
        or not isinstance(min_score, (int, float))
    ):
      raise ValueError("policy pin min_scores is malformed")
  policy = EvalScorePolicy(min_scores, missing_score_fails=missing_score_fails)
  if _policy_pin(policy) != value:
    raise ValueError("policy pin is not canonical")
  return policy


def _policy_column(policy: Optional[EvalScorePolicy]) -> Optional[str]:
  """``view_policy`` manifest column: the canonical pin as JSON, or NULL."""
  pin = _policy_pin(policy)
  if pin is None:
    return None
  return json.dumps(pin, sort_keys=True)


def _policy_from_column(value: Any) -> Optional[EvalScorePolicy]:
  """Parse a manifest row's ``view_policy`` strictly (it is trusted state)."""
  if value is None:
    return None
  try:
    if not isinstance(value, str):
      raise ValueError("view_policy must be a string")
    policy = _policy_from_pin(json.loads(value))
  except (ValueError, TypeError) as exc:
    raise ValueError(
        f"manifest view_policy {value!r} is not a canonical score policy;"
        " re-publish the version with replace=True"
    ) from exc
  if _policy_column(policy) != value:
    raise ValueError(
        f"manifest view_policy {value!r} is not canonical; re-publish the"
        " version with replace=True"
    )
  return policy


_GENERATION_ID_PATTERN = re.compile(r"[0-9a-f]{32}")


def _new_generation_id() -> str:
  return uuid.uuid4().hex


def _manifest_generation_id(manifest: Mapping[str, Any]) -> str:
  """The opaque id of one committed generation of a manifest row.

  A ``replace`` re-publishes the same version label as a new generation,
  and so does a committed policy change; ``imported_at`` is caller-chosen
  and may repeat, so it never identifies the generation. Only this does.
  """
  value = manifest.get("generation_id")
  if not isinstance(value, str) or not _GENERATION_ID_PATTERN.fullmatch(value):
    raise ValueError(
        f"manifest row for job {manifest.get('job_id')!r} import_version "
        f"{manifest.get('import_version')!r} has no generation_id;"
        " re-run materialize() to upgrade the manifest, or re-publish the"
        " version with replace=True"
    )
  return value


def _superseded_generations(
    manifest: Mapping[str, Any],
) -> dict[str, Optional[str]]:
  """The row's committed history: superseded ``generation_id`` -> policy.

  Parsed strictly (it is trusted state written only by the publish
  transaction and ``_record_view_policy``). Entries without a generation
  (a legacy row replaced before its backfill) name nothing a view could
  have been pinned to and are skipped.
  """
  history: dict[str, Optional[str]] = {}
  for raw in manifest.get("superseded_generations") or ():
    try:
      entry = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError as exc:
      raise ValueError(
          f"manifest superseded_generations entry {raw!r} is not JSON"
      ) from exc
    if not isinstance(entry, Mapping):
      raise ValueError(
          f"manifest superseded_generations entry {raw!r} is malformed"
      )
    generation_id = entry.get("generation_id")
    if generation_id is None:
      continue
    if not isinstance(
        generation_id, str
    ) or not _GENERATION_ID_PATTERN.fullmatch(generation_id):
      raise ValueError(
          f"manifest superseded_generations entry {raw!r} has a malformed"
          " generation_id"
      )
    policy = entry.get("view_policy")
    if policy is not None and not isinstance(policy, str):
      raise ValueError(
          f"manifest superseded_generations entry {raw!r} has a malformed"
          " view_policy"
      )
    history[generation_id] = policy
  return history


def _view_pin(
    job_id: str,
    import_version: str,
    *,
    generation_id: str,
    policy: Optional[EvalScorePolicy],
) -> dict[str, Any]:
  """The pin comment payload: what the view is bound to."""
  return {
      "job_id": job_id,
      "import_version": import_version,
      "generation_id": generation_id,
      "policy": _policy_pin(policy),
  }


def _view_pin_comment(pin: Mapping[str, Any]) -> str:
  # ``ensure_ascii`` keeps the comment on one line whatever the job_id holds.
  return _VIEW_PIN_MARKER + json.dumps(pin, sort_keys=True, ensure_ascii=True)


def _parse_view_pin(
    view_query: str,
) -> Optional[tuple[dict[str, Any], Optional[EvalScorePolicy]]]:
  """Parse the pin comment a view starts with: what it *claims* to be.

  Returns the pin and its policy, or ``None`` when the first nonblank line
  is not a well-formed pin. This is a claim only; ``_read_managed_view``
  decides ownership by re-rendering the claimed definition.
  """
  for raw in view_query.splitlines():
    line = raw.strip()
    if not line:
      continue
    if not line.startswith(_VIEW_PIN_MARKER):
      return None
    try:
      pin = json.loads(line[len(_VIEW_PIN_MARKER) :])
    except json.JSONDecodeError:
      return None
    if not (
        isinstance(pin, dict)
        and isinstance(pin.get("job_id"), str)
        and isinstance(pin.get("import_version"), str)
        and isinstance(pin.get("generation_id"), str)
        # The generation must be in the one form the importer mints, or
        # the re-rendered body could never match anyway.
        and _GENERATION_ID_PATTERN.fullmatch(pin["generation_id"])
    ):
      return None
    try:
      policy = _policy_from_pin(pin.get("policy"))
    except ValueError:
      return None
    return (
        {
            "job_id": pin["job_id"],
            "import_version": pin["import_version"],
            "generation_id": pin["generation_id"],
            "policy": pin.get("policy"),
        },
        policy,
    )
  return None


@dataclasses.dataclass(frozen=True)
class _ManagedView:
  """A view at the managed name that the importer recognizes as its own.

  ``pin`` is what the view claims, verified against the manifest; the gate
  it renders was checked against committed state and is not carried here,
  since nothing downstream may act on a view's own policy claim.
  """

  table: Any
  pin: dict[str, Any]

  @property
  def view_query(self) -> str:
    return str(self.table.view_query)


def _read_managed_view(
    client: Any,
    *,
    view_ref: str,
    manifest_ref: str,
    location: Optional[str],
) -> Optional[_ManagedView]:
  """Return the managed view at ``view_ref`` with its pin and ETag.

  ``None`` means nothing exists at ``view_ref``. The view is the importer's
  only if its pin names a version the pinned job committed to the manifest
  *and* its body is byte-for-byte what the importer renders for that
  manifest row from committed metadata alone. When the pin names the row's
  *current* generation the rendering uses the row's committed
  ``view_policy``; when it names a generation the row's
  ``superseded_generations`` history records (the row was re-published or
  its policy re-committed after the view was written) the rendering uses
  the policy that history committed for that generation. The pin's own
  policy claim is never the reference, so a canonical rendering of any
  generation under another gate is foreign -- and so is a rendering under
  a generation the manifest never committed, however well-formed: nothing
  trusted can vouch for it. A view of a superseded generation that does
  verify is only ever advanced to the committed rendering by
  ``_reconcile_failed_sessions_view``, so whatever gate it carries is
  never preserved. The comparison is exact: BigQuery returns a view's
  query text verbatim, and whitespace inside a rendered literal (a
  ``job_id``) changes which rows the view reads, so no normalization is
  safe. A table, a view somebody else wrote, a copied or forged marker
  over other SQL, a rendering for an unpublished version or over other
  tables, or an edited or reformatted body all raise. The importer only
  ever replaces views whose definition it can vouch for.
  """
  try:
    table = client.get_table(view_ref)
  except NotFound:
    return None
  view_query = getattr(table, "view_query", None)
  parsed = _parse_view_pin(view_query) if isinstance(view_query, str) else None
  if parsed is not None:
    pin, _ = parsed
    try:
      pinned = _read_manifest(
          client,
          manifest_ref=manifest_ref,
          job_id=pin["job_id"],
          import_version=pin["import_version"],
          location=location,
          feature=_VIEW_FEATURE,
      )
    except NotFound:
      # No manifest table: nothing was ever published here, so the view
      # cannot be one the importer wrote.
      pinned = None
    if pinned is not None:
      expected = _committed_generation_view_body(pinned, pin["generation_id"])
      if expected is not None and view_query == expected:
        return _ManagedView(table=table, pin=pin)
  raise ValueError(
      f"{view_ref!r} exists but is not an evalbench failed_sessions view"
      " created by materialize() (or its definition was changed); choose"
      " another failed_sessions_view name (or None to skip the view)"
      " rather than replacing it"
  )


def _check_view_binding(
    client: Any,
    *,
    view_ref: str,
    manifest_ref: str,
    job_id: str,
    location: Optional[str],
) -> Optional[_ManagedView]:
  """Refuse a view the importer does not own or that belongs to another job."""
  existing = _read_managed_view(
      client, view_ref=view_ref, manifest_ref=manifest_ref, location=location
  )
  if existing is not None and existing.pin["job_id"] != job_id:
    raise ValueError(
        f"failed_sessions view {view_ref!r} is pinned to EvalBench job "
        f"{existing.pin['job_id']!r}, not {job_id!r}; pass a "
        "different failed_sessions_view for this job, or None to skip"
        " the view"
    )
  return existing


def _committed_view_body(manifest: Mapping[str, Any]) -> str:
  """The view the manifest row says the importer renders for it.

  Generation and policy come from the row (``generation_id``,
  ``view_policy``): this is the only rendering the importer ever writes.
  """
  return _failed_sessions_view_body(
      manifest=manifest,
      policy=_policy_from_column(manifest.get("view_policy")),
      generation_id=_manifest_generation_id(manifest),
  )


def _committed_generation_view_body(
    manifest: Mapping[str, Any], generation_id: str
) -> Optional[str]:
  """The rendering the importer wrote for ``generation_id`` of this row.

  The row's current generation renders with its ``view_policy``; a
  generation in the row's ``superseded_generations`` history renders with
  the policy committed for it there, over the row's tables (a version's
  destination never changes). ``None`` for a generation the manifest never
  committed -- including a row that has no generation yet, which no view
  was ever written for -- so the caller cannot vouch for it.
  """
  current = manifest.get("generation_id")
  if isinstance(current, str) and generation_id == current:
    return _committed_view_body(manifest)
  history = _superseded_generations(manifest)
  if generation_id not in history:
    return None
  return _failed_sessions_view_body(
      manifest=manifest,
      policy=_policy_from_column(history[generation_id]),
      generation_id=generation_id,
  )


def _failed_sessions_view_body(
    *,
    manifest: Mapping[str, Any],
    policy: Optional[EvalScorePolicy],
    generation_id: Optional[str] = None,
) -> str:
  """The managed view's query text pinned to one manifest row's version.

  A pure function of the manifest row's version and tables, the generation
  (the row's own unless a pin's claim is being re-rendered) and the
  canonical policy, which is what lets ``_read_managed_view`` re-render
  and compare.
  """
  job_id = str(manifest["job_id"])
  import_version = str(manifest["import_version"])
  policy = _canonical_policy(policy)
  query = _failed_sessions_sql_for_refs(
      events_ref=str(manifest["events_table"]),
      scores_ref=str(manifest["scores_table"]),
      policy=policy,
      job_id=job_id,
      import_version=import_version,
  )
  pin = _view_pin(
      job_id,
      import_version,
      generation_id=(
          _manifest_generation_id(manifest)
          if generation_id is None
          else generation_id
      ),
      policy=policy,
  )
  return _VIEW_BODY.format(pin_comment=_view_pin_comment(pin), query=query)


def _failed_sessions_view_description(manifest: Mapping[str, Any]) -> str:
  return (
      "EvalBench failed sessions (W0.4 contract) pinned to job_id "
      f"{str(manifest['job_id'])!r} import_version "
      f"{str(manifest['import_version'])!r}; maintained by"
      " bigquery_agent_analytics.evalbench.EvalBenchRun.materialize"
  )


def _sync_failed_sessions_view(
    client: Any,
    *,
    view_ref: str,
    manifest_ref: str,
    job_id: str,
    policy: Optional[EvalScorePolicy],
    location: Optional[str],
    status: str,
    import_version: str,
    manifest: Mapping[str, Any],
) -> str:
  """Point ``view_ref`` at the job's latest successful import.

  Runs after the publish decision. ``manifest`` is the row this call
  handled: the one it committed (which already records ``policy`` under
  its fresh generation), or for ``unchanged`` the committed row it found.
  In the latter case a policy that differs from the row's is committed to
  that row first (``_record_view_policy``), conditional on the row still
  being the generation this call found, so an ``unchanged`` re-import can
  add, change, or drop the gate of the version it names -- and nothing
  else. The view itself is then reconciled from the latest manifest row
  (not the version just handled), so an ``unchanged`` no-op on an old
  version does not move the view backwards, and a ``replace`` of an old
  version -- which refreshes ``imported_at`` -- does; see
  ``_reconcile_failed_sessions_view``.
  """
  try:
    if status == "unchanged" and _policy_column(policy) != manifest.get(
        "view_policy"
    ):
      _record_view_policy(
          client,
          manifest_ref=manifest_ref,
          manifest=manifest,
          policy=policy,
          location=location,
      )
    _reconcile_failed_sessions_view(
        client,
        view_ref=view_ref,
        manifest_ref=manifest_ref,
        job_id=job_id,
        location=location,
    )
  except Exception as exc:  # noqa: BLE001
    raise ValueError(
        f"EvalBench job {job_id!r} import_version {import_version!r} is"
        f" published (status {status!r}) but the failed_sessions view"
        f" {view_ref!r} could not be created or updated: {exc}. Re-run"
        " materialize() to retry the view; the import itself then reports"
        " 'unchanged'"
    ) from exc
  return view_ref


def _record_view_policy(
    client: Any,
    *,
    manifest_ref: str,
    manifest: Mapping[str, Any],
    policy: Optional[EvalScorePolicy],
    location: Optional[str],
) -> None:
  """Commit ``policy`` to the manifest row this call found, if unchanged.

  The UPDATE is keyed on the row's ``generation_id`` as read, so it lands
  on nothing when a concurrent ``replace`` (or another policy change)
  re-published the row since -- that generation's own caller decided its
  gate. It mints a new generation either way, so a view still pinned to
  the old one is recognized as superseded and re-rendered from the row.
  """
  job_config = bigquery.QueryJobConfig(
      query_parameters=[
          bigquery.ScalarQueryParameter(
              "job_id", "STRING", str(manifest["job_id"])
          ),
          bigquery.ScalarQueryParameter(
              "import_version", "STRING", str(manifest["import_version"])
          ),
          bigquery.ScalarQueryParameter(
              "expected_generation_id",
              "STRING",
              _manifest_generation_id(manifest),
          ),
          bigquery.ScalarQueryParameter(
              "generation_id", "STRING", _new_generation_id()
          ),
          bigquery.ScalarQueryParameter(
              "view_policy", "STRING", _policy_column(policy)
          ),
      ]
  )
  job_config = with_sdk_labels(job_config, feature=_VIEW_FEATURE)
  query_args: dict[str, Any] = {"job_config": job_config}
  if location is not None:
    query_args["location"] = location
  query = _RECORD_VIEW_POLICY_QUERY.format(manifest_table=manifest_ref)
  client.query(query, **query_args).result()


def _reconcile_failed_sessions_view(
    client: Any,
    *,
    view_ref: str,
    manifest_ref: str,
    job_id: str,
    location: Optional[str],
) -> None:
  """Create or conditionally replace the managed view, never blindly.

  Each attempt re-reads the view (ownership, job, ETag) and *then* the
  latest manifest row, so a version committed by a concurrent import after
  the view was read is still seen. What gets written is always the
  rendering of that row -- its version, tables, ``generation_id`` and
  ``view_policy`` -- and nothing this caller asked for: the caller's
  policy is already committed manifest state (or was refused there) by the
  time the view is reconciled. A missing view is created with
  create-if-absent (``Conflict`` if another import created it first); an
  existing one is replaced only if its ETag still matches
  (``PreconditionFailed`` otherwise). Either race re-reads and re-decides,
  a bounded number of times, then fails closed -- a stale pin is never
  written over a newer one, and success is never reported for a view that
  was not verified. Because every generation is rendered into the pin, the
  writer of a newer generation always bumps the ETag under a delayed older
  caller, whose conditional replace then fails and re-decides from the
  committed row instead of landing its stale rendering unchallenged.
  """
  for _ in range(_VIEW_SYNC_ATTEMPTS):
    existing = _check_view_binding(
        client,
        view_ref=view_ref,
        manifest_ref=manifest_ref,
        job_id=job_id,
        location=location,
    )
    latest = _read_latest_manifest(
        client, manifest_ref=manifest_ref, job_id=job_id, location=location
    )
    if latest is None:
      raise ValueError(
          f"EvalBench job {job_id!r} has no manifest row in {manifest_ref!r}"
          " after publishing; cannot pin the failed_sessions view"
      )
    if latest.get("generation_id") is None:
      # The latest row predates generations and its backfill was
      # interrupted; finish it and re-decide.
      _backfill_generation_ids(
          client, manifest_ref=manifest_ref, location=location
      )
      continue
    body = _committed_view_body(latest)
    description = _failed_sessions_view_description(latest)
    if existing is None:
      table = bigquery.Table(view_ref)
      table.view_query = body
      table.description = description
      try:
        client.create_table(table, exists_ok=False)
      except Conflict:
        continue
      return
    if existing.view_query == body:
      return
    table = existing.table
    if not getattr(table, "etag", None):
      raise ValueError(
          f"failed_sessions view {view_ref!r} has no ETag; refusing an"
          " unconditional replace"
      )
    table.view_query = body
    table.description = description
    try:
      client.update_table(table, ["view_query", "description"])
    except PreconditionFailed:
      continue
    return
  raise ValueError(
      f"failed_sessions view {view_ref!r} changed concurrently"
      f" {_VIEW_SYNC_ATTEMPTS} times; re-run materialize() to retry"
  )


def _read_latest_manifest(
    client: Any,
    *,
    manifest_ref: str,
    job_id: str,
    location: Optional[str],
    feature: str = _VIEW_FEATURE,
) -> Optional[dict[str, Any]]:
  job_config = bigquery.QueryJobConfig(
      query_parameters=[
          bigquery.ScalarQueryParameter("job_id", "STRING", job_id)
      ]
  )
  job_config = with_sdk_labels(job_config, feature=feature)
  query_args: dict[str, Any] = {"job_config": job_config}
  if location is not None:
    query_args["location"] = location
  query = _LATEST_IMPORT_QUERY.format(manifest_table=manifest_ref)
  rows = [_plain_row(row) for row in client.query(query, **query_args).result()]
  return rows[0] if rows else None


@dataclasses.dataclass(frozen=True)
class EvalBenchSession:
  """One session of a pinned EvalBench import, as ``failed_sessions`` returns.

  The classification fields mirror ``SessionVerdict`` (and therefore
  ``failed_sessions_sql``). ``session_id`` and ``trace_id`` are the
  version-specific published identity
  ``evalbench-import:{job_id}:{import_version}:{scenario_id}``, so handing
  them to ``Client.get_session_trace`` (see ``trace_selector``) can only
  ever return rows of this ``import_version``.
  """

  job_id: str
  import_version: str
  session_id: str
  trace_id: str
  scenario_id: Optional[str]
  started_at: Optional[datetime]
  process_failed: bool
  missing_completion: bool
  score_failed: bool
  failed: bool
  failing_scores: dict[str, Optional[float]] = dataclasses.field(
      default_factory=dict
  )

  def trace_selector(self) -> dict[str, Any]:
    """Keyword arguments that drill this session into ``get_session_trace``.

    ``session_id`` alone already pins the import version (it is part of the
    identity); ``experiment_id`` additionally pins the job scope, matching
    ``attributes.experiment_id`` that every published row carries. Use with
    a ``Client`` whose ``table_id`` is the mirror events table::

        client.get_session_trace(**session.trace_selector())
    """
    return {"session_id": self.session_id, "experiment_id": self.job_id}

  def get_trace(self, client: Any, **overrides: Any) -> Any:
    """Thin wrapper: ``client.get_session_trace(**trace_selector())``."""
    return client.get_session_trace(**{**self.trace_selector(), **overrides})

  def verdict(self) -> SessionVerdict:
    return SessionVerdict(
        session_id=self.session_id,
        scenario_id=self.scenario_id,
        process_failed=self.process_failed,
        missing_completion=self.missing_completion,
        score_failed=self.score_failed,
        failing_scores=dict(self.failing_scores),
        failed=self.failed,
    )

  @property
  def taxonomy_categories(self) -> tuple[str, ...]:
    """G1-frozen taxonomy names tripped by this session's mechanical flags.

    Computed from the three flags via
    ``failure_taxonomy.categorize_failed_session`` (a property, not a stored
    field, so it can never drift from the flags). Frozen vocabulary
    (taxonomy v0.1.0): each tripped flag maps to its frozen name and the
    names come back in ``FROZEN_CATEGORY_NAMES`` order — assignment stays
    mechanical until the labeler study. All flags false (an
    ``include_passed`` row) yields ``()``, never ``("unknown",)``.
    """
    return categorize_failed_session(self)

  def to_dict(self) -> dict[str, Any]:
    payload = _json_safe(dataclasses.asdict(self))
    payload["taxonomy_categories"] = list(self.taxonomy_categories)
    return payload


@dataclasses.dataclass(frozen=True)
class EvalBenchFailedSessions:
  """Outcome of ``failed_sessions``: one pinned import and its session rows.

  ``sessions`` holds the failed sessions only unless ``include_passed`` was
  requested; ``failed_count``/``session_count`` are computed over the same
  pinned version regardless.
  """

  job_id: str
  import_version: str
  events_table: str
  scores_table: str
  sessions: list[EvalBenchSession]
  session_count: int
  failed_count: int
  manifest: dict[str, Any] = dataclasses.field(default_factory=dict)

  def to_dict(self) -> dict[str, Any]:
    # Nested sessions go through EvalBenchSession.to_dict so each row also
    # carries the computed taxonomy_categories (asdict only sees fields).
    payload = _json_safe(dataclasses.asdict(self))
    payload["sessions"] = [session.to_dict() for session in self.sessions]
    return payload


def failed_sessions(
    *,
    target_project: str,
    target_dataset: str,
    job_id: str,
    import_version: Optional[str] = None,
    policy: Optional[EvalScorePolicy] = None,
    include_passed: bool = False,
    location: Optional[str] = None,
    bq_client: Optional[Any] = None,
) -> EvalBenchFailedSessions:
  """List the failed sessions of exactly one published import version.

  The version is pinned before any event row is read: an explicit
  ``import_version`` must have a manifest row, and when omitted the job's
  latest successful import (newest ``imported_at``, the same pin the
  ``evalbench_failed_sessions`` view tracks) is used. The query is
  ``failed_sessions_sql`` over the events/scores tables recorded in that
  manifest row -- a version stays bound to the tables it was published to
  -- executed with ``import_query_parameters``, so rows of another version
  can never be mixed in. ``policy`` applies the per-benchmark score gate;
  without it only process failures and missing completions count.

  Returns an ``EvalBenchFailedSessions`` whose ``sessions`` are the failed
  rows (all rows with ``include_passed=True``), each carrying the versioned
  ``session_id``/``trace_id`` for ``Client.get_session_trace``.
  """
  _validate_source_segment("target_project", target_project)
  _validate_source_segment("target_dataset", target_dataset)
  if not isinstance(job_id, str) or not job_id:
    raise ValueError("job_id must be a non-empty string")
  manifest_ref = f"{target_project}.{target_dataset}.{MANIFEST_TABLE}"
  client = bq_client or make_bq_client(target_project, location=location)
  if import_version is None:
    manifest = _read_latest_manifest(
        client, manifest_ref=manifest_ref, job_id=job_id, location=location
    )
    if manifest is None:
      raise ValueError(
          f"EvalBench job {job_id!r} has no published import in"
          f" {manifest_ref!r}; run materialize() first"
      )
    import_version = str(manifest["import_version"])
  else:
    _validate_import_version(import_version)
    manifest = _read_manifest(
        client,
        manifest_ref=manifest_ref,
        job_id=job_id,
        import_version=import_version,
        location=location,
    )
    if manifest is None:
      raise ValueError(
          f"EvalBench job {job_id!r} import_version {import_version!r} is not"
          f" published in {manifest_ref!r}"
      )
  events_ref = str(manifest["events_table"])
  scores_ref = str(manifest["scores_table"])
  sql = _failed_sessions_sql_for_refs(
      events_ref=events_ref, scores_ref=scores_ref, policy=policy
  )
  job_config = bigquery.QueryJobConfig(
      query_parameters=import_query_parameters(job_id, import_version)
  )
  job_config = with_sdk_labels(job_config, feature=_VIEW_FEATURE)
  query_args: dict[str, Any] = {"job_config": job_config}
  if location is not None:
    query_args["location"] = location
  rows = [
      _session_from_row(
          _plain_row(row), job_id=job_id, import_version=import_version
      )
      for row in client.query(sql, **query_args).result()
  ]
  failed = [row for row in rows if row.failed]
  return EvalBenchFailedSessions(
      job_id=job_id,
      import_version=import_version,
      events_table=events_ref,
      scores_table=scores_ref,
      sessions=rows if include_passed else failed,
      session_count=len(rows),
      failed_count=len(failed),
      manifest=dict(manifest),
  )


def _resolve_manifest(
    client: Any,
    *,
    manifest_ref: str,
    job_id: str,
    import_version: Optional[str],
    location: Optional[str],
    feature: str,
) -> tuple[str, dict[str, Any]]:
  """Pin ``(job_id, import_version)`` to one committed manifest row.

  An explicit ``import_version`` must have a manifest row; ``None`` resolves
  the job's latest successful import (newest ``imported_at``, the same pin
  the ``evalbench_failed_sessions`` view tracks). Raises ``ValueError``
  before any event row is read when nothing is published.
  """
  if import_version is None:
    manifest = _read_latest_manifest(
        client,
        manifest_ref=manifest_ref,
        job_id=job_id,
        location=location,
        feature=feature,
    )
    if manifest is None:
      raise ValueError(
          f"EvalBench job {job_id!r} has no published import in"
          f" {manifest_ref!r}; run materialize() first"
      )
    return str(manifest["import_version"]), manifest
  _validate_import_version(import_version)
  manifest = _read_manifest(
      client,
      manifest_ref=manifest_ref,
      job_id=job_id,
      import_version=import_version,
      location=location,
      feature=feature,
  )
  if manifest is None:
    raise ValueError(
        f"EvalBench job {job_id!r} import_version {import_version!r} is not"
        f" published in {manifest_ref!r}"
    )
  return import_version, manifest


@dataclasses.dataclass(frozen=True)
class EvalBenchImportSessions:
  """The sessions of exactly one published EvalBench import version.

  Returned by ``import_sessions``. ``session_ids`` are the versioned
  identities (``evalbench-import:{job_id}:{import_version}:{scenario_id}``)
  the mirror ``events_table`` holds for this version, in ``session_id``
  order; ``manifest`` is the committed manifest row the version was
  resolved from. ``trace_filter()`` turns the listing into the
  ``TraceFilter`` that scores this version -- and only this version -- with
  ``Client.evaluate``.
  """

  job_id: str
  import_version: str
  events_table: str
  scores_table: str
  session_ids: tuple[str, ...]
  manifest: dict[str, Any] = dataclasses.field(default_factory=dict)

  @property
  def session_count(self) -> int:
    return len(self.session_ids)

  def trace_filter(self) -> "TraceFilter":
    """``TraceFilter`` selecting exactly this version's sessions.

    Pins ``experiment_id`` to the job and ``session_ids`` to the version's
    identities, with ``limit`` raised to the session count so no session is
    truncated. Refuses an empty session set: ``TraceFilter`` treats no
    ``session_ids`` as "unfiltered", which would silently widen the
    evaluation to every retained version of the job.
    """
    from .trace import TraceFilter  # local: keep evalbench import-light

    if not self.session_ids:
      raise ValueError(
          f"EvalBench job {self.job_id!r} import_version"
          f" {self.import_version!r} has no sessions in"
          f" {self.events_table!r}; nothing to score"
      )
    return TraceFilter(
        experiment_id=self.job_id,
        session_ids=list(self.session_ids),
        limit=len(self.session_ids),
    )

  def to_dict(self) -> dict[str, Any]:
    return _json_safe(dataclasses.asdict(self))


def import_sessions(
    *,
    target_project: str,
    target_dataset: str,
    job_id: str,
    import_version: Optional[str] = None,
    events_table: Optional[str] = None,
    location: Optional[str] = None,
    bq_client: Optional[Any] = None,
) -> EvalBenchImportSessions:
  """Resolve one published import version to its session identities.

  The version is pinned from the manifest before any event row is read
  (``failed_sessions`` semantics: an explicit ``import_version`` must be
  published, ``None`` means the job's latest successful import), then the
  version's distinct ``session_id`` values are read from the
  ``events_table`` recorded in that manifest row, so a version stays bound
  to the table it was published to. The result feeds ``Client.evaluate``
  through ``EvalBenchImportSessions.trace_filter()`` -- the
  ``bq-agent-sdk evalbench-score`` path (#97).

  The manifest is written by the importer and read here under the scorer's
  identity, so the persisted ``events_table`` is never trusted as-is: it
  must be the canonical ``target_project.target_dataset.<mirror table>``
  reference (every segment re-validated, the ADK plugin's ``agent_events``
  rejected) before it is formatted into the session query. When
  ``events_table`` names the mirror table the caller expects (the CLI's
  ``--table-id``), the stored binding must match it, again before any
  events-table statement is submitted.

  Args:
      events_table: Optional mirror table name in ``target_dataset`` the
        version is expected to be published in. Validated before any
        BigQuery call; a mismatch with the manifest raises ``ValueError``
        before the events table is read.
  """
  _validate_source_segment("target_project", target_project)
  _validate_source_segment("target_dataset", target_dataset)
  if not isinstance(job_id, str) or not job_id:
    raise ValueError("job_id must be a non-empty string")
  expected_events_ref: Optional[str] = None
  if events_table is not None:
    _validate_destination_table("events_table", events_table)
    expected_events_ref = f"{target_project}.{target_dataset}.{events_table}"
  manifest_ref = f"{target_project}.{target_dataset}.{MANIFEST_TABLE}"
  client = bq_client or make_bq_client(target_project, location=location)
  import_version, manifest = _resolve_manifest(
      client,
      manifest_ref=manifest_ref,
      job_id=job_id,
      import_version=import_version,
      location=location,
      feature=_SCORE_FEATURE,
  )
  events_ref = _validate_manifest_events_table(
      manifest.get("events_table"),
      target_project=target_project,
      target_dataset=target_dataset,
      job_id=job_id,
      import_version=import_version,
  )
  if expected_events_ref is not None and events_ref != expected_events_ref:
    raise ValueError(
        f"EvalBench job {job_id!r} import_version {import_version!r} is"
        f" published in {events_ref!r}, not {expected_events_ref!r}; pass"
        " the events_table it was published to"
    )
  job_config = bigquery.QueryJobConfig(
      query_parameters=_import_parameters(job_id, import_version)
  )
  job_config = with_sdk_labels(job_config, feature=_SCORE_FEATURE)
  query_args: dict[str, Any] = {"job_config": job_config}
  if location is not None:
    query_args["location"] = location
  query = _IMPORT_SESSIONS_QUERY.format(events_table=events_ref)
  session_ids = tuple(
      str(row["session_id"])
      for row in (
          _plain_row(raw) for raw in client.query(query, **query_args).result()
      )
      if row.get("session_id") is not None
  )
  return EvalBenchImportSessions(
      job_id=job_id,
      import_version=import_version,
      events_table=events_ref,
      scores_table=str(manifest["scores_table"]),
      session_ids=session_ids,
      manifest=dict(manifest),
  )


def _session_from_row(
    row: Mapping[str, Any], *, job_id: str, import_version: str
) -> EvalBenchSession:
  """Build an ``EvalBenchSession`` from one ``failed_sessions_sql`` row."""
  failing_scores: dict[str, Optional[float]] = {}
  for item in row.get("failing_scores") or ():
    entry = _as_mapping(item) if isinstance(item, Mapping) else _row_items(item)
    comparator = entry.get("comparator")
    if isinstance(comparator, str):
      failing_scores[comparator] = _score_value(entry.get("score"))
  started_at = row.get("started_at")
  if started_at is not None and not isinstance(started_at, datetime):
    started_at = _parse_timestamp(started_at)
  session_id = str(row["session_id"])
  return EvalBenchSession(
      job_id=job_id,
      import_version=import_version,
      session_id=session_id,
      trace_id=session_id,
      scenario_id=_usable_text(row.get("scenario_id")),
      started_at=started_at,
      process_failed=bool(row.get("process_failed")),
      missing_completion=bool(row.get("missing_completion")),
      score_failed=bool(row.get("score_failed")),
      failed=bool(row.get("failed")),
      failing_scores=failing_scores,
  )


def _row_items(value: Any) -> dict[str, Any]:
  """``bigquery.Row``-like STRUCT values expose ``items()`` without Mapping."""
  if hasattr(value, "items"):
    return {str(key): item for key, item in value.items()}
  return {}


def import_query_parameters(
    job_id: str, import_version: str
) -> list[bigquery.ScalarQueryParameter]:
  """Query parameters that pin ``failed_sessions_sql`` to one import."""
  _validate_import_version(import_version)
  return _import_parameters(job_id, import_version)


def _import_parameters(
    job_id: str, import_version: str
) -> list[bigquery.ScalarQueryParameter]:
  return [
      bigquery.ScalarQueryParameter("job_id", "STRING", job_id),
      bigquery.ScalarQueryParameter("import_version", "STRING", import_version),
  ]


def _publish_parameters(
    *,
    job_id: str,
    import_version: str,
    fingerprints: Mapping[str, str],
    source_project: str,
    source_dataset: str,
    events_ref: str,
    scores_ref: str,
) -> list[bigquery.ScalarQueryParameter]:
  """``_import_parameters`` plus the values the transaction guard compares."""
  parameters = _import_parameters(job_id, import_version)
  for key in _FINGERPRINT_KEYS:
    parameters.append(
        bigquery.ScalarQueryParameter(key, "STRING", fingerprints[key])
    )
  parameters.append(
      bigquery.ScalarQueryParameter("events_table", "STRING", events_ref)
  )
  parameters.append(
      bigquery.ScalarQueryParameter("scores_table", "STRING", scores_ref)
  )
  parameters.append(
      bigquery.ScalarQueryParameter("source_project", "STRING", source_project)
  )
  parameters.append(
      bigquery.ScalarQueryParameter("source_dataset", "STRING", source_dataset)
  )
  return parameters


def _manifest_drift(
    existing: Mapping[str, Any], manifest: Mapping[str, Any]
) -> list[str]:
  """Describe how a stored manifest row differs from this import's manifest.

  Compares exactly the inputs the publish transaction's guard compares
  without ``replace`` (fingerprints and ``_PROVENANCE_KEYS``); destination
  refs are checked separately by ``_check_destination_binding`` because they
  are enforced even with ``replace``. Returns an empty list when the stored
  version is identical.
  """
  drift: list[str] = []
  if any(existing.get(key) != manifest[key] for key in _FINGERPRINT_KEYS):
    drift.append("source fingerprints")
  for key in _PROVENANCE_KEYS:
    if existing.get(key) != manifest[key]:
      drift.append(f"{key} ({existing.get(key)!r} vs {manifest[key]!r})")
  return drift


def _publish_script(
    *,
    events_ref: str,
    scores_ref: str,
    manifest_ref: str,
    lock_ref: str,
    events_staging: str,
    scores_staging: str,
    manifest_staging: str,
    replace: bool,
) -> str:
  """Render the publish transaction: lock claim, manifest guard, DML.

  The transaction always starts by UPDATE-ing the lock sentinel, which is
  the only statement guaranteed to mutate an existing row (the keyed DELETEs
  match nothing on a first import and INSERTs never conflict). Without
  ``replace`` the guard rejects an existing manifest row whose fingerprints,
  source project/dataset, *or* destination tables differ from this import,
  and leaves an identical row untouched (``_PUBLISH_UNCHANGED_MESSAGE``
  rolls the transaction back); with ``replace`` fingerprints and provenance
  may drift and an identical version is re-published, but the destination
  binding still holds, so a version can never be silently relocated.
  """
  if replace:
    conflict_predicate = _DESTINATION_CONFLICT_PREDICATE
    identical_guard = _REPLACE_IDENTICAL_NOTE
  else:
    conflict_predicate = " OR ".join(
        (
            _FINGERPRINT_CONFLICT_PREDICATE,
            _PROVENANCE_CONFLICT_PREDICATE,
            _DESTINATION_CONFLICT_PREDICATE,
        )
    )
    identical_guard = _IDENTICAL_GUARD.format(
        unchanged_message=_PUBLISH_UNCHANGED_MESSAGE
    )
  return _PUBLISH_SCRIPT.format(
      events_table=events_ref,
      scores_table=scores_ref,
      manifest_table=manifest_ref,
      lock_table=lock_ref,
      lock_id=_IMPORT_LOCK_ID,
      lock_missing_message=_LOCK_MISSING_MESSAGE,
      events_staging=events_staging,
      scores_staging=scores_staging,
      manifest_staging=manifest_staging,
      conflict_predicate=conflict_predicate,
      conflict_message=_PUBLISH_CONFLICT_MESSAGE,
      identical_guard=identical_guard,
      event_columns=", ".join(_EVENT_COLUMNS),
      score_columns=", ".join(name for name, _, _ in _SCORE_SCHEMA_FIELDS),
      manifest_columns=", ".join(_STAGED_MANIFEST_COLUMNS),
  )


def _check_destination_binding(
    existing: Mapping[str, Any],
    *,
    job_id: str,
    import_version: str,
    events_ref: str,
    scores_ref: str,
) -> None:
  """Reject a re-import whose destination tables differ from the manifest."""
  stored = (existing.get("events_table"), existing.get("scores_table"))
  if stored == (events_ref, scores_ref):
    return
  raise ValueError(
      f"EvalBench job {job_id!r} import_version {import_version!r} is "
      f"published in events_table={stored[0]!r} scores_table={stored[1]!r}, "
      f"not the requested events_table={events_ref!r} "
      f"scores_table={scores_ref!r}; a version stays bound to the tables in "
      "its manifest. Point at those tables, or publish to the new tables "
      "under a new import_version"
  )


# Two identity families, deliberately kept apart:
#
# * Plain reads (``to_agent_event_rows()`` without ``import_version``) keep
#   the v0.5.1 public contract verbatim: ``evalbench:{job_id}:{scenario_id}``
#   with no escaping, and span/invocation ids hashed by the legacy
#   ``_stable_id``. Changing either would break stored trace references of
#   runs mapped before this release.
# * Published versions use the ``evalbench-import`` namespace, whose
#   components are joined with ":" and escaped ("\:" and "\\") so the join
#   is injective and decodes back to the original tuple. ``import_version``
#   admits ":" and ``scenario_id`` is arbitrary source text, so without
#   escaping ``(import_version="release:1", scenario_id="case")`` and
#   ``(import_version="release", scenario_id="1:case")`` would collide.
#
# The namespaces cannot alias each other: every plain identity starts with
# ``evalbench:`` and every published one with ``evalbench-import:``.
_LEGACY_IDENTITY_PREFIX = "evalbench"
_PUBLISHED_IDENTITY_PREFIX = "evalbench-import"
_IDENTITY_SEPARATOR = ":"
_IDENTITY_ESCAPES = str.maketrans({"\\": "\\\\", _IDENTITY_SEPARATOR: "\\:"})


def _identity_component(value: str) -> str:
  return value.translate(_IDENTITY_ESCAPES)


def _session_identity(
    job_id: str, scenario_id: str, *, import_version: Optional[str]
) -> str:
  """Session/trace identity shared by event rows and score rows.

  ``evalbench:{job_id}:{scenario_id}`` (unescaped, the v0.5.1 contract) for
  a plain read and ``evalbench-import:{job_id}:{import_version}:{scenario_id}``
  with every component escaped for a published version. Published
  components without ``:`` or ``\\`` (the common case) render verbatim.
  """
  if import_version is None:
    return _IDENTITY_SEPARATOR.join(
        (_LEGACY_IDENTITY_PREFIX, job_id, scenario_id)
    )
  return _IDENTITY_SEPARATOR.join(
      [_PUBLISHED_IDENTITY_PREFIX]
      + [
          _identity_component(part)
          for part in (job_id, import_version, scenario_id)
      ]
  )


def _validate_import_version(import_version: Any) -> None:
  if not isinstance(
      import_version, str
  ) or not _IMPORT_VERSION_PATTERN.fullmatch(import_version):
    raise ValueError(
        f"import_version must match {_IMPORT_VERSION_PATTERN.pattern}"
    )


def _derived_import_version(fingerprints: Mapping[str, str]) -> str:
  combined = "\x1f".join(fingerprints[key] for key in _FINGERPRINT_KEYS)
  return hashlib.sha256(combined.encode("utf-8")).hexdigest()[:16]


def _canonical_json(value: Any) -> str:
  return json.dumps(
      _json_safe(value), sort_keys=True, separators=(",", ":"), default=str
  )


def _fingerprint_rows(rows: tuple[dict[str, Any], ...]) -> str:
  digests = sorted(
      hashlib.sha256(_canonical_json(row).encode("utf-8")).hexdigest()
      for row in rows
  )
  return hashlib.sha256("\n".join(digests).encode("utf-8")).hexdigest()


def _score_scenario_id(score: Mapping[str, Any]) -> Optional[str]:
  return _find_scenario_id(score)


def _score_comparator(score: Mapping[str, Any]) -> Optional[str]:
  for key in ("comparator", "scorer", "metric", "evaluator"):
    text = _usable_text(score.get(key))
    if text is not None:
      return text
  return None


def _score_value(value: Any) -> Optional[float]:
  """Coerce an EvalBench score to a finite float; anything else is NULL."""
  if value is None:
    return None
  if isinstance(value, bool):
    return 1.0 if value else 0.0
  try:
    number = float(value)
  except (TypeError, ValueError):
    return None
  return number if math.isfinite(number) else None


def _schema(fields: tuple[tuple[str, str, str], ...]) -> list:
  return [
      bigquery.SchemaField(name, field_type, mode=mode)
      for name, field_type, mode in fields
  ]


def _event_schema() -> list:
  """Mirror of the ADK plugin ``agent_events`` schema plus import binding."""
  return [
      bigquery.SchemaField("timestamp", "TIMESTAMP", mode="REQUIRED"),
      bigquery.SchemaField("event_type", "STRING"),
      bigquery.SchemaField("agent", "STRING"),
      bigquery.SchemaField("session_id", "STRING"),
      bigquery.SchemaField("invocation_id", "STRING"),
      bigquery.SchemaField("user_id", "STRING"),
      bigquery.SchemaField("trace_id", "STRING"),
      bigquery.SchemaField("span_id", "STRING"),
      bigquery.SchemaField("parent_span_id", "STRING"),
      bigquery.SchemaField("content", "JSON"),
      bigquery.SchemaField(
          "content_parts",
          "RECORD",
          mode="REPEATED",
          fields=[
              bigquery.SchemaField("mime_type", "STRING"),
              bigquery.SchemaField("uri", "STRING"),
              bigquery.SchemaField(
                  "object_ref",
                  "RECORD",
                  fields=[
                      bigquery.SchemaField("uri", "STRING"),
                      bigquery.SchemaField("version", "STRING"),
                      bigquery.SchemaField("authorizer", "STRING"),
                      bigquery.SchemaField("details", "JSON"),
                  ],
              ),
              bigquery.SchemaField("text", "STRING"),
              bigquery.SchemaField("part_index", "INTEGER"),
              bigquery.SchemaField("part_attributes", "STRING"),
              bigquery.SchemaField("storage_mode", "STRING"),
          ],
      ),
      bigquery.SchemaField("attributes", "JSON"),
      bigquery.SchemaField("latency_ms", "JSON"),
      bigquery.SchemaField("status", "STRING"),
      bigquery.SchemaField("error_message", "STRING"),
      bigquery.SchemaField("is_truncated", "BOOLEAN"),
      bigquery.SchemaField("job_id", "STRING", mode="REQUIRED"),
      bigquery.SchemaField("import_version", "STRING", mode="REQUIRED"),
  ]


_SCHEMA_UPGRADE_ATTEMPTS = 3


def _ensure_manifest_schema(
    client: Any, *, manifest_ref: str, location: Optional[str]
) -> None:
  """Upgrade a manifest table an earlier release created, idempotently.

  Slice 1 (#451) created ``evalbench_import_manifest`` without
  ``generation_id``, ``view_policy`` and ``superseded_generations``;
  ``create_table(exists_ok=True)`` returns such a table unchanged. Any
  column of ``_MANIFEST_SCHEMA_FIELDS`` the live table lacks is added
  through the tables API (ETag-conditional, re-read and retried when a
  concurrent upgrade lands first), then every row without a generation is
  given one (``_BACKFILL_GENERATION_QUERY``). A table that already has the
  full schema costs one metadata read and nothing else; a manifest that
  does not exist yet is left for ``_ensure_import_tables`` to create with
  the full schema.
  """
  try:
    table = client.get_table(manifest_ref)
  except NotFound:
    return
  wanted = _schema(_MANIFEST_SCHEMA_FIELDS)
  for _ in range(_SCHEMA_UPGRADE_ATTEMPTS):
    present = {field.name for field in table.schema}
    missing = [field for field in wanted if field.name not in present]
    if not missing:
      return
    table.schema = list(table.schema) + missing
    try:
      client.update_table(table, ["schema"])
    except PreconditionFailed:
      table = client.get_table(manifest_ref)
      continue
    _backfill_generation_ids(
        client, manifest_ref=manifest_ref, location=location
    )
    return
  raise ValueError(
      f"manifest table {manifest_ref!r} changed concurrently"
      f" {_SCHEMA_UPGRADE_ATTEMPTS} times while adding"
      f" {[field.name for field in missing]}; re-run materialize()"
  )


def _backfill_generation_ids(
    client: Any, *, manifest_ref: str, location: Optional[str]
) -> None:
  """Give every manifest row without a ``generation_id`` its derived one."""
  job_config = with_sdk_labels(
      bigquery.QueryJobConfig(), feature=_IMPORT_FEATURE
  )
  query_args: dict[str, Any] = {"job_config": job_config}
  if location is not None:
    query_args["location"] = location
  query = _BACKFILL_GENERATION_QUERY.format(manifest_table=manifest_ref)
  client.query(query, **query_args).result()


def _ensure_import_tables(
    client: Any,
    *,
    events_ref: str,
    scores_ref: str,
    manifest_ref: str,
    lock_ref: str,
) -> None:
  events = bigquery.Table(events_ref, schema=_event_schema())
  events.time_partitioning = bigquery.TimePartitioning(field="timestamp")
  events.clustering_fields = ["job_id", "import_version", "session_id"]
  client.create_table(events, exists_ok=True)

  scores = bigquery.Table(scores_ref, schema=_schema(_SCORE_SCHEMA_FIELDS))
  scores.clustering_fields = ["job_id", "import_version", "session_id"]
  client.create_table(scores, exists_ok=True)

  manifest = bigquery.Table(
      manifest_ref, schema=_schema(_MANIFEST_SCHEMA_FIELDS)
  )
  client.create_table(manifest, exists_ok=True)

  lock = bigquery.Table(lock_ref, schema=_schema(_LOCK_SCHEMA_FIELDS))
  client.create_table(lock, exists_ok=True)


def _seed_import_lock(
    client: Any, *, lock_ref: str, location: Optional[str]
) -> None:
  """Insert the lock sentinel if the dataset does not have one yet.

  Runs as its own committed DML job before the publish transaction, so the
  transaction's snapshot contains the row its claim UPDATE mutates.
  """
  job_config = with_sdk_labels(
      bigquery.QueryJobConfig(), feature=_IMPORT_FEATURE
  )
  query_args: dict[str, Any] = {"job_config": job_config}
  if location is not None:
    query_args["location"] = location
  query = _SEED_LOCK_QUERY.format(lock_table=lock_ref, lock_id=_IMPORT_LOCK_ID)
  client.query(query, **query_args).result()


def _drop_staging_tables(client: Any, staging_refs: tuple[str, ...]) -> None:
  for staging_ref in staging_refs:
    try:
      client.delete_table(staging_ref, not_found_ok=True)
    except Exception as exc:  # noqa: BLE001
      _LOGGER.warning(
          "evalbench import: could not drop staging table %s (%s); it expires"
          " automatically after %s",
          staging_ref,
          exc,
          _STAGING_TABLE_TTL,
      )


def _read_manifest(
    client: Any,
    *,
    manifest_ref: str,
    job_id: str,
    import_version: str,
    location: Optional[str],
    feature: str = _IMPORT_FEATURE,
) -> Optional[dict[str, Any]]:
  job_config = bigquery.QueryJobConfig(
      query_parameters=_import_parameters(job_id, import_version)
  )
  job_config = with_sdk_labels(job_config, feature=feature)
  query_args: dict[str, Any] = {"job_config": job_config}
  if location is not None:
    query_args["location"] = location
  query = _READ_MANIFEST_QUERY.format(manifest_table=manifest_ref)
  rows = [_plain_row(row) for row in client.query(query, **query_args).result()]
  if not rows:
    return None
  if len(rows) > 1:
    raise ValueError(
        f"manifest table {manifest_ref!r} has {len(rows)} rows for job "
        f"{job_id!r} import_version {import_version!r}; expected at most one"
    )
  return rows[0]


def _load_staging(
    client: Any, staging_ref: str, rows: list[dict[str, Any]], schema: list
) -> None:
  """Load ``rows`` into a fresh staging table with an explicit schema.

  Load jobs write to managed storage, so the publish transaction that runs
  next sees every row (streaming inserts would sit in a buffer the DML
  cannot read). Nested ``content``/``attributes`` objects are loaded as JSON
  values via newline-delimited JSON. The staging table is created with an
  expiration first, so a staging table that outlives a crashed import (or a
  failed cleanup) is garbage-collected by BigQuery.
  """
  table = bigquery.Table(staging_ref, schema=schema)
  table.expires = datetime.now(timezone.utc) + _STAGING_TABLE_TTL
  client.create_table(table, exists_ok=True)
  if not rows:
    return
  job_config = bigquery.LoadJobConfig(
      write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
      source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
      autodetect=False,
      schema=schema,
  )
  job_config = with_sdk_labels(job_config, feature=_IMPORT_FEATURE)
  client.load_table_from_json(rows, staging_ref, job_config=job_config).result()


def _read_source_rows(
    client: Any,
    *,
    table_id: str,
    job_id: str,
    location: Optional[str],
    snapshot_at: Optional[datetime] = None,
) -> tuple[dict[str, Any], ...]:
  parameters = [bigquery.ScalarQueryParameter("job_id", "STRING", job_id)]
  if snapshot_at is None:
    query = _READ_SOURCE_TABLE_QUERY.format(table_id=table_id)
  else:
    query = _READ_SOURCE_TABLE_SNAPSHOT_QUERY.format(table_id=table_id)
    parameters.append(
        bigquery.ScalarQueryParameter("snapshot_at", "TIMESTAMP", snapshot_at)
    )
  job_config = bigquery.QueryJobConfig(query_parameters=parameters)
  job_config = with_sdk_labels(job_config, feature=_IMPORT_FEATURE)
  query_args: dict[str, Any] = {"job_config": job_config}
  if location is not None:
    query_args["location"] = location
  result = client.query(query, **query_args).result()
  return tuple(_plain_row(row) for row in result)


def _plain_row(row: Any) -> dict[str, Any]:
  if isinstance(row, Mapping):
    items = row.items()
  elif hasattr(row, "items"):
    items = row.items()
  else:
    items = dict(row).items()
  return {str(key): _plain_value(value) for key, value in items}


def _plain_value(value: Any) -> Any:
  if isinstance(value, Mapping):
    return {str(key): _plain_value(item) for key, item in value.items()}
  if isinstance(value, (list, tuple)):
    return [_plain_value(item) for item in value]
  return value


def _validate_source_segment(name: str, value: Any) -> None:
  if not isinstance(value, str) or not _SOURCE_SEGMENT_PATTERN.fullmatch(value):
    raise ValueError(
        f"{name} must contain only ASCII letters, digits, '_' or '-'"
    )


def _validate_destination_table(name: str, value: Any) -> None:
  """Validate a mirror table name and reject the ADK plugin's ``agent_events``.

  Runs before any BigQuery operation so the production telemetry table can
  never be created, staged into, or published to by an EvalBench import.
  """
  _validate_source_segment(name, value)
  if value.lower() in _RESERVED_DESTINATION_TABLES:
    raise ValueError(
        f"{name} must not be the reserved ADK plugin table {value!r}; "
        "EvalBench imports publish only to BQAA-owned mirror tables such as "
        f"{DEFAULT_EVENTS_TABLE!r}"
    )


def _validate_manifest_events_table(
    value: Any,
    *,
    target_project: str,
    target_dataset: str,
    job_id: str,
    import_version: str,
) -> str:
  """Re-validate a persisted manifest ``events_table`` before querying it.

  The importer validates every segment before publishing, but the manifest
  is read back under a different identity (the scorer), so the stored
  value is treated as untrusted input: it must be exactly
  ``target_project.target_dataset.<table>`` with the table segment passing
  ``_validate_destination_table`` (segment charset, reserved
  ``agent_events``). Anything else -- a closed backtick, a semicolon, a
  reference into another project or dataset -- raises before the value can
  be formatted into a statement.
  """
  context = (
      f"EvalBench job {job_id!r} import_version {import_version!r} manifest"
      " events_table"
  )
  if not isinstance(value, str) or not value:
    raise ValueError(f"{context} must be a non-empty string, got {value!r}")
  segments = value.split(".")
  if len(segments) != 3:
    raise ValueError(
        f"{context} must be a canonical project.dataset.table reference,"
        f" got {value!r}"
    )
  project, dataset, table = segments
  try:
    _validate_destination_table("events_table", table)
  except ValueError as exc:
    raise ValueError(f"{context} {value!r} is invalid: {exc}") from exc
  if project != target_project or dataset != target_dataset:
    raise ValueError(
        f"{context} {value!r} is outside the scored dataset"
        f" {target_project!r}.{target_dataset!r}"
    )
  return f"{project}.{dataset}.{table}"


def _find_scenario_id(row: Mapping[str, Any]) -> Optional[str]:
  """Locate the scenario id in a result or score row (same lookup order)."""
  scenario = _as_mapping(_structured(row.get("scenario")))
  nested_result = _as_mapping(_structured(row.get("eval_results")))
  nested_scenario = _as_mapping(_structured(nested_result.get("scenario")))
  for value in (
      row.get("id"),
      row.get("eval_id"),
      row.get("scenario_id"),
      scenario.get("id"),
      nested_result.get("eval_id"),
      nested_result.get("id"),
      nested_result.get("scenario_id"),
      nested_scenario.get("id"),
      row.get("prompt_id"),
  ):
    text = _usable_text(value)
    if text is not None:
      return text
  return None


def _scenario_id(result: Mapping[str, Any]) -> str:
  scenario_id = _find_scenario_id(result)
  if scenario_id is None:
    raise ValueError("EvalBench result is missing id/eval_id")
  return scenario_id


def _prompt(result: Mapping[str, Any]) -> Optional[str]:
  scenario = _as_mapping(_structured(result.get("scenario")))
  nested_result = _as_mapping(_structured(result.get("eval_results")))
  nested_scenario = _as_mapping(_structured(nested_result.get("scenario")))
  for value in (
      result.get("nl_prompt"),
      result.get("prompt"),
      scenario.get("starting_prompt"),
      nested_result.get("nl_prompt"),
      nested_result.get("prompt"),
      nested_scenario.get("starting_prompt"),
  ):
    text = _usable_text(value)
    if text is not None:
      return text
  return None


def _final_response(result: Mapping[str, Any]) -> Optional[str]:
  for key in ("final_response", "response", "generated_output", "output"):
    text = _usable_text(result.get(key))
    if text is not None:
      return text

  stdout = result.get("stdout")
  stdout_value = _structured(stdout)
  if isinstance(stdout_value, Mapping):
    for key in ("response", "final_response", "output"):
      text = _usable_text(stdout_value.get(key))
      if text is not None:
        return text
  else:
    text = _usable_text(stdout)
    if text is not None:
      return text

  nested_result = _as_mapping(_structured(result.get("eval_results")))
  for key in ("final_response", "response", "generated_output", "output"):
    text = _usable_text(nested_result.get(key))
    if text is not None:
      return text
  nested_stdout = nested_result.get("stdout")
  nested_stdout_value = _structured(nested_stdout)
  if isinstance(nested_stdout_value, Mapping):
    for key in ("response", "final_response", "output"):
      text = _usable_text(nested_stdout_value.get(key))
      if text is not None:
        return text
  else:
    text = _usable_text(nested_stdout)
    if text is not None:
      return text

  generated_sql = _usable_text(
      result.get("generated_sql"), rejected=_NO_GENERATED_OUTPUT
  )
  if generated_sql is not None:
    return generated_sql
  return _usable_text(
      nested_result.get("generated_sql"), rejected=_NO_GENERATED_OUTPUT
  )


def _tool_calls(result: Mapping[str, Any]) -> list[dict[str, Any]]:
  nested_result = _as_mapping(_structured(result.get("eval_results")))
  stdout_payload = _as_mapping(_structured(result.get("stdout")))
  nested_stdout_payload = _as_mapping(_structured(nested_result.get("stdout")))
  for value in (
      result.get("tool_calls"),
      stdout_payload.get("tool_calls"),
      nested_result.get("tool_calls"),
      nested_stdout_payload.get("tool_calls"),
      result.get("accumulated_tools"),
      nested_result.get("accumulated_tools"),
  ):
    calls = _normalize_tool_calls(value)
    if calls:
      return calls
  return []


def _normalize_tool_calls(value: Any) -> list[dict[str, Any]]:
  value = _structured(value)
  if not isinstance(value, (list, tuple)):
    return []

  calls: list[dict[str, Any]] = []
  for item in value:
    if isinstance(item, str):
      name = _usable_text(item)
      if name is not None:
        calls.append({"tool_name": name, "args": {}, "result": None})
      continue
    call = _as_mapping(_structured(item))
    name = None
    for key in ("tool_name", "name", "tool"):
      name = _usable_text(call.get(key))
      if name is not None:
        break
    if name is None:
      continue
    args = call.get("parameters", call.get("args", call.get("arguments", {})))
    result = call.get("response", call.get("result", call.get("output")))
    error = call.get("error")
    status = _usable_text(call.get("status"))
    if (
        error is None
        and status is not None
        and status.lower()
        not in {
            "completed",
            "ok",
            "success",
        }
    ):
      error = status
    calls.append(
        {
            "tool_name": name,
            "args": _json_safe(_structured(args)) or {},
            "result": _json_safe(_structured(result)),
            "error": _json_safe(_structured(error)),
            "timestamp": call.get("timestamp"),
            "result_timestamp": call.get("result_timestamp"),
            "duration_ms": call.get("duration_ms", call.get("latency_ms")),
        }
    )
  return calls


def _result_run_time(result: Mapping[str, Any]) -> Optional[datetime]:
  nested_result = _as_mapping(_structured(result.get("eval_results")))
  for value in (result.get("run_time"), nested_result.get("run_time")):
    parsed = _parse_timestamp(value)
    if parsed is not None:
      return parsed
  return None


def _first_run_time(
    config_rows: tuple[dict[str, Any], ...],
) -> Optional[datetime]:
  for row in config_rows:
    parsed = _parse_timestamp(row.get("run_time"))
    if parsed is not None:
      return parsed
  return None


def _parse_timestamp(value: Any) -> Optional[datetime]:
  if isinstance(value, datetime):
    return (
        value
        if value.tzinfo is not None
        else value.replace(tzinfo=timezone.utc)
    )
  if isinstance(value, date):
    return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
  if not isinstance(value, str):
    return None
  text = value.strip()
  if not text or text.lower() in _MISSING_TEXT:
    return None
  if text.endswith("Z"):
    text = text[:-1] + "+00:00"
  try:
    parsed = datetime.fromisoformat(text)
  except ValueError:
    return None
  return (
      parsed
      if parsed.tzinfo is not None
      else parsed.replace(tzinfo=timezone.utc)
  )


def _config_values(config_rows: tuple[dict[str, Any], ...]) -> dict[str, Any]:
  values: dict[str, Any] = {}
  for row in config_rows:
    key = _usable_text(row.get("config"))
    if key is not None:
      values[key] = row.get("value")
  return values


def _agent_name(config: Mapping[str, Any]) -> str:
  orchestrator = _usable_text(config.get("experiment_config.orchestrator"))
  generator = _usable_text(config.get("model_config.generator"))
  return f"evalbench:{orchestrator or 'unknown'}:{generator or 'unknown'}"


def _base_attributes(
    *,
    result: Mapping[str, Any],
    project_id: str,
    dataset_id: str,
    job_id: str,
    scenario_id: str,
    agent: str,
) -> dict[str, Any]:
  attributes: dict[str, Any] = {
      "experiment_id": job_id,
      "evalbench_scenario_id": scenario_id,
      "evalbench_source_project": project_id,
      "evalbench_source_dataset": dataset_id,
      "root_agent_name": agent,
  }
  for source_key, target_key in (
      ("database", "evalbench_database"),
      ("dialects", "evalbench_dialects"),
      ("query_type", "evalbench_query_type"),
  ):
    value = result.get(source_key)
    if _usable_text(value) is not None or isinstance(
        value, (list, tuple, dict)
    ):
      attributes[target_key] = _json_safe(_structured(value))
  return attributes


def _source_error_fields(result: Mapping[str, Any]) -> dict[str, Any]:
  fields: dict[str, Any] = {}
  for key in (
      "prompt_generator_error",
      "sql_generator_error",
      "generated_error",
      "golden_error",
      "error",
      "stderr",
  ):
    value = result.get(key)
    if _usable_text(value) is not None:
      fields[key] = _json_safe(_structured(value))
  returncode = result.get("returncode")
  if _failed_returncode(returncode):
    fields["returncode"] = _json_safe(_structured(returncode))
  return fields


def _source_error_message(
    result: Mapping[str, Any], error_fields: Mapping[str, Any]
) -> Optional[str]:
  parts: list[str] = []
  for key in (
      "prompt_generator_error",
      "sql_generator_error",
      "generated_error",
      "golden_error",
      "error",
  ):
    text = _usable_text(error_fields.get(key))
    if text is not None:
      parts.append(f"{key}: {text}")

  returncode = result.get("returncode")
  if _failed_returncode(returncode):
    parts.append(f"returncode: {returncode}")
  # stderr is a process failure in its own right: a process that exited 0
  # and produced a final response but also wrote to stderr is still not a
  # clean completion, so it must not be published as status OK.
  stderr = _usable_text(error_fields.get("stderr"))
  if stderr is not None:
    parts.append(f"stderr: {stderr}")
  return "; ".join(parts) if parts else None


def _failed_returncode(returncode: Any) -> bool:
  try:
    return returncode is not None and int(returncode) != 0
  except (TypeError, ValueError):
    return _usable_text(returncode) is not None


def _usage_and_latency(
    result: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
  payload = _as_mapping(_structured(result.get("stdout")))
  if not payload:
    nested_result = _as_mapping(_structured(result.get("eval_results")))
    payload = _as_mapping(_structured(nested_result.get("stdout")))

  stats = _as_mapping(payload.get("stats"))
  models = _as_mapping(stats.get("models"))
  token_totals = {
      "input": 0,
      "output": 0,
      "total": 0,
      "cached": 0,
  }
  found_tokens = {key: False for key in token_totals}
  total_latency_ms = 0
  found_latency = False

  for model_data_value in models.values():
    model_data = _as_mapping(_structured(model_data_value))
    tokens = _as_mapping(_structured(model_data.get("tokens")))
    for target, aliases in (
        ("input", ("input", "prompt", "input_tokens")),
        ("output", ("candidates", "output", "output_tokens")),
        ("total", ("total", "total_tokens")),
        ("cached", ("cached", "cached_tokens")),
    ):
      number = _first_int(tokens, aliases)
      if number is not None:
        token_totals[target] += number
        found_tokens[target] = True
    api = _as_mapping(_structured(model_data.get("api")))
    latency_value = _first_int(api, ("totalLatencyMs", "total_latency_ms"))
    if latency_value is not None:
      # Some EvalBench producers repeat one run-level duration for each model.
      total_latency_ms = max(total_latency_ms, latency_value)
      found_latency = True

  direct_values = {
      "input": _first_int(
          result, ("input_tokens", "prompt_tokens", "prompt_token_count")
      ),
      "output": _first_int(
          result,
          ("output_tokens", "completion_tokens", "candidates_token_count"),
      ),
      "total": _first_int(result, ("total_tokens", "total_token_count")),
      "cached": _first_int(
          result, ("cached_tokens", "cached_content_token_count")
      ),
  }
  for key, value in direct_values.items():
    if not found_tokens[key] and value is not None:
      token_totals[key] = value
      found_tokens[key] = True

  if not found_tokens["total"] and (
      found_tokens["input"] or found_tokens["output"]
  ):
    token_totals["total"] = token_totals["input"] + token_totals["output"]
    found_tokens["total"] = True

  usage: dict[str, Any] = {}
  metadata: dict[str, int] = {}
  if found_tokens["input"]:
    usage["input_tokens"] = token_totals["input"]
    metadata["prompt_token_count"] = token_totals["input"]
  if found_tokens["output"]:
    usage["output_tokens"] = token_totals["output"]
    metadata["candidates_token_count"] = token_totals["output"]
  if found_tokens["total"]:
    metadata["total_token_count"] = token_totals["total"]
  if found_tokens["cached"]:
    metadata["cached_content_token_count"] = token_totals["cached"]
  if metadata:
    usage["usage_metadata"] = metadata

  latency: dict[str, Any] = {}
  if found_latency:
    latency["total_ms"] = total_latency_ms
  return usage, latency


def _first_int(
    mapping: Mapping[str, Any], keys: tuple[str, ...]
) -> Optional[int]:
  for key in keys:
    value = mapping.get(key)
    if value is None or isinstance(value, bool):
      continue
    try:
      return int(value)
    except (TypeError, ValueError):
      continue
  return None


def _tool_latency(tool_call: Mapping[str, Any]) -> dict[str, Any]:
  duration = tool_call.get("duration_ms")
  if duration is not None:
    try:
      return {"total_ms": int(duration)}
    except (TypeError, ValueError):
      pass
  started = _parse_timestamp(tool_call.get("timestamp"))
  completed = _parse_timestamp(tool_call.get("result_timestamp"))
  if started is not None and completed is not None:
    return {
        "total_ms": max(0, int((completed - started).total_seconds() * 1000))
    }
  return {}


def _event_row(
    *,
    event_type: str,
    timestamp: datetime,
    agent: str,
    session_id: str,
    invocation_id: str,
    span_id: str,
    parent_span_id: Optional[str],
    content: Mapping[str, Any],
    attributes: Mapping[str, Any],
    latency_ms: Optional[Mapping[str, Any]] = None,
    status: str = "OK",
    error_message: Optional[str] = None,
) -> dict[str, Any]:
  return {
      "session_id": session_id,
      "event_type": event_type,
      "timestamp": timestamp.isoformat(),
      "agent": agent,
      "invocation_id": invocation_id,
      "trace_id": session_id,
      "span_id": span_id,
      "parent_span_id": parent_span_id,
      "user_id": None,
      "content": _json_safe(content),
      "content_parts": [],
      "attributes": _json_safe(attributes),
      "latency_ms": _json_safe(latency_ms or {}),
      "status": status,
      "error_message": error_message,
      "is_truncated": False,
  }


def _stable_id(*parts: str, length: int) -> str:
  """Legacy (v0.5.1) deterministic hex id used by plain-read identities.

  Frozen on purpose: ``to_agent_event_rows()`` without ``import_version``
  must keep producing the invocation/span ids it produced in v0.5.1.
  """
  digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
  return digest[:length]


def _published_stable_id(*parts: str, length: int) -> str:
  """Deterministic hex id for published (versioned) identities.

  Parts are length-prefixed before hashing so the encoding is injective even
  when a part (such as a session id built from arbitrary scenario text)
  contains the joiner: ``("a\\x1fb", "c")`` and ``("a", "b\\x1fc")`` hash
  differently. The framing is tagged with the published namespace so it can
  never reproduce a legacy digest either.
  """
  encoded = "\x1f".join(
      [_PUBLISHED_IDENTITY_PREFIX] + [f"{len(part)}:{part}" for part in parts]
  )
  digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
  return digest[:length]


def _structured(value: Any) -> Any:
  if not isinstance(value, str):
    return _plain_value(value)
  text = value.strip()
  if not text or text[0] not in "[{(" or text[-1] not in "]})":
    return value
  try:
    return _plain_value(json.loads(text))
  except (json.JSONDecodeError, TypeError):
    pass
  try:
    return _plain_value(ast.literal_eval(text))
  except (SyntaxError, ValueError, TypeError):
    return value


def _as_mapping(value: Any) -> dict[str, Any]:
  if isinstance(value, Mapping):
    return {str(key): item for key, item in value.items()}
  return {}


def _usable_text(
    value: Any, *, rejected: frozenset[str] = frozenset()
) -> Optional[str]:
  if value is None:
    return None
  if isinstance(value, str):
    if value.strip().lower() in _MISSING_TEXT | rejected:
      return None
    return value
  if isinstance(value, (Mapping, list, tuple)):
    return json.dumps(_json_safe(value), sort_keys=True, separators=(",", ":"))
  text = str(value)
  if text.strip().lower() in _MISSING_TEXT | rejected:
    return None
  return text


def _json_safe(value: Any) -> Any:
  if isinstance(value, Mapping):
    return {str(key): _json_safe(item) for key, item in value.items()}
  if isinstance(value, (list, tuple)):
    return [_json_safe(item) for item in value]
  if isinstance(value, datetime):
    return value.isoformat()
  if isinstance(value, date):
    return value.isoformat()
  if isinstance(value, float) and not math.isfinite(value):
    # JSON has no NaN/Infinity; BigQuery's NDJSON loader rejects the Python
    # extensions, so keep the value as text rather than failing the load.
    if math.isnan(value):
      return "NaN"
    return "Infinity" if value > 0 else "-Infinity"
  if value is None or isinstance(value, (str, int, float, bool)):
    return value
  return str(value)


def _compact_json(value: Any) -> str:
  safe = _json_safe(value)
  if safe in ({}, [], None):
    return ""
  return json.dumps(safe, sort_keys=True, separators=(",", ":"))


def _one_line(value: Any) -> str:
  text = _usable_text(value)
  if text is None:
    return ""
  return " ".join(text.splitlines())
