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
"""Native ``agent_events`` snapshot writer — the EvalBench-adapter exit ramp.

Issue #463 (parent #435). ``NativeAgentEventsRun`` starts from production ADK
``agent_events`` rows and publishes the same BQAA-owned contract
``EvalBenchRun.materialize`` already produces — a pinned, immutable
``(job_id, import_version)`` snapshot (events + scores + manifest) plus the
``failed_sessions`` view pinned to the latest successful publication — with
**no EvalBench source tables anywhere in the path**: no ``configs``, no
``results``, no ``scores`` reads. The ``evalbench-import`` adapter (#97)
stays as an optional on-ramp; this class is the exit ramp, not a removal.

Contracts honored (all frozen by the Week 0 evidence, #455–#461):

* **Read vs write.** The production ``agent_events`` table is only ever
  read. Publishing goes through the inherited ``materialize``, whose
  destination validation rejects the reserved ``agent_events`` name before
  any BigQuery call, so the native writer can never write the plugin's
  production table.
* **Identity.** The joinable scenario id (EvalBench ``results.id`` /
  ``eval_id``) is the first eight characters of the ADK ``session_id``
  (``7e352c34`` from ``7e352c34-4c1c-...``), falling back to the full
  session id on collision — the same rule the slice-5 synthesizer froze.
  Published event and score rows keep the **real** ADK ``session_id``, so
  the same session is the same object with or without the adapter; the
  ``(job_id, import_version)`` columns pin versions, exactly as the
  adapter's snapshot tables are pinned.
* **Denominator.** ``failed_sessions`` is the denominator; a live/LLM
  judge is not. The only synthesized value is the deterministic
  ``goal_completion`` score: ``1.0`` when the session logged
  ``AGENT_COMPLETED``, ``0.0`` otherwise. That mirrors the frozen slice-5
  rule (``returncode`` mirrors completion) and says *completed*, not
  *passed* — only the ``EvalScorePolicy`` gate decides *passed*. For the
  same reason a session that never completed is published with its first
  prompt row marked ``status=ERROR`` (original status preserved in
  ``attributes.bqaa_native_source_status``), which is byte-for-byte the
  process-failure marker the adapter publishes for ``returncode != 0``.
* **G1 labels.** Failed sessions map onto the frozen taxonomy v0.1.0
  (``failure_taxonomy.py``) through the unchanged
  ``classify_sessions`` / ``failed_sessions`` consumers: the widget-stock
  silence session ``7e352c34`` trips all three mechanical flags and yields
  ``task/planning``, ``finalization``, ``tool blockers``. Opting in with
  ``materialize(span_labels_table=...)`` additionally persists the #466
  span-level localization of those categories as pinned rows (#469) under
  ONE effective score policy shared with the session denominator
  (``resolve_span_label_policy``). The opt-in is durable: the dataset's
  span-binding registry (``SPAN_BINDINGS_TABLE``) records the binding
  and its synchronized manifest generation, every later native publish
  of a bound job maintains the span snapshot (or fails closed before the
  denominator advances), and the companion ``{span_labels_table}_pinned``
  view exposes rows only for the exact generation the manifest currently
  pins, so joins never fan out across retained versions and never pair a
  new session snapshot with stale span labels; span rows localize the
  session-level verdict, never replace it.
* **Clock.** Nothing here starts the six-week clock, seals the
  preregistration, or kicks a Week 1 snapshot job.

Everything except the optional BigQuery read is pure and offline
(fixture-testable): ``from_agent_events`` takes in-memory rows.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import dataclasses
from datetime import datetime
import json
from typing import Any, Optional
import uuid

from google.api_core.exceptions import Conflict
from google.api_core.exceptions import NotFound
from google.api_core.exceptions import PreconditionFailed
from google.cloud import bigquery

from ._telemetry import make_bq_client
from ._telemetry import with_sdk_labels
from .evalbench import _as_mapping
from .evalbench import _CONCURRENT_UPDATE_MARKER
from .evalbench import _derived_import_version
from .evalbench import _drop_staging_tables
from .evalbench import _fingerprint_rows
from .evalbench import _GENERATION_ID_PATTERN
from .evalbench import _IMPORT_LOCK_ID
from .evalbench import _import_parameters
from .evalbench import _json_safe
from .evalbench import _load_staging
from .evalbench import _LOCK_MISSING_MESSAGE
from .evalbench import _manifest_generation_id
from .evalbench import _parse_timestamp
from .evalbench import _plain_row
from .evalbench import _policy_column
from .evalbench import _policy_from_column
from .evalbench import _read_latest_manifest
from .evalbench import _read_manifest
from .evalbench import _schema
from .evalbench import _seed_import_lock
from .evalbench import _sql_string_literal
from .evalbench import _structured
from .evalbench import _usable_text
from .evalbench import _validate_destination_table
from .evalbench import _validate_import_version
from .evalbench import _validate_source_segment
from .evalbench import _VIEW_SYNC_ATTEMPTS
from .evalbench import DEFAULT_EVENTS_TABLE
from .evalbench import DEFAULT_FAILED_SESSIONS_VIEW
from .evalbench import DEFAULT_SCORES_TABLE
from .evalbench import EvalBenchImportResult
from .evalbench import EvalBenchRun
from .evalbench import EvalScorePolicy
from .evalbench import LOCK_TABLE
from .evalbench import MANIFEST_TABLE
from .failure_taxonomy import TAXONOMY_VERSION

# The one deterministic native comparator: completion, not correctness.
NATIVE_COMPARATOR = "goal_completion"
_NATIVE_FEATURE = "evalbench-native-import"
_SHORT_ID_LENGTH = 8
_PROMPT_EVENT = "USER_MESSAGE_RECEIVED"
_COMPLETION_EVENT = "AGENT_COMPLETED"
_TOOL_START_EVENT = "TOOL_STARTING"
# Same rule the slice-5 synthesizer + importer composition froze: a session
# that never completed publishes as a process failure (the synthesizer's
# returncode=1, which the importer maps to an ERROR-status prompt row).
_NATIVE_SCORE_RULE = (
    "goal_completion is 1.0 iff the session logged AGENT_COMPLETED;"
    " completed is not passed — only the score policy decides passed"
)
_MISSING_COMPLETION_ERROR = (
    "native snapshot: session never logged AGENT_COMPLETED"
    " (slice-5 returncode=1 equivalent)"
)

# Span-level G1 publication (#469): the BQAA-owned table that persists the
# #466 localization library's labels as pinned snapshot rows. Off by
# default: ``materialize(span_labels_table=...)`` opts in, so the frozen
# #464 publish stays byte-identical for jobs that never asked for span
# labels (including corpora whose rows carry no span_id columns). Opting
# in is durable per job: the dataset's span-binding registry
# (``SPAN_BINDINGS_TABLE``) records the binding, and every later native
# publish of a bound job maintains the span snapshot — or fails closed —
# instead of quietly advancing the session snapshot past it.
DEFAULT_SPAN_LABELS_TABLE = "evalbench_span_labels"
# One row per SpanFailureLabel: the RFC tuple (trace_id, span_id,
# failure_category, evidence, confidence) plus the frozen join identity
# (eval_id / session_id), the target_kind marker, the frozen taxonomy
# version, the (job_id, import_version) pin every sibling table carries,
# and the exact manifest ``generation_id`` the rows were synchronized
# under. A changed-source ``replace`` of the same version label mints a
# new generation, so rows a failed span sync left behind carry a
# generation the manifest no longer holds and can never masquerade as the
# newly committed base snapshot (the pinned view only exposes the
# generation the manifest currently pins).
_SPAN_LABELS_SCHEMA_FIELDS = (
    ("job_id", "STRING", "REQUIRED"),
    ("import_version", "STRING", "REQUIRED"),
    ("generation_id", "STRING", "REQUIRED"),
    ("eval_id", "STRING", "REQUIRED"),
    ("session_id", "STRING", "REQUIRED"),
    ("trace_id", "STRING", "NULLABLE"),
    ("span_id", "STRING", "REQUIRED"),
    ("failure_category", "STRING", "REQUIRED"),
    ("evidence", "STRING", "REQUIRED"),
    ("confidence", "FLOAT64", "REQUIRED"),
    ("target_kind", "STRING", "REQUIRED"),
    ("taxonomy_version", "STRING", "REQUIRED"),
)
# The dataset's span-binding registry (#469): one row per job_id that
# opted in to span labels, recording the span table it publishes to, the
# ONE resolved score policy the span rows and the session denominator
# share (canonical ``view_policy`` JSON), and the (import_version,
# generation_id) whose span rows were last synchronized. Like the
# manifest and the lock, the name is fixed per dataset and never
# caller-selectable: it is what lets a later native publish that does NOT
# pass ``span_labels_table`` discover the binding and keep the span
# snapshot, the score gate, and the failed-sessions denominator moving in
# lockstep. The row is replaced inside the same lock-serialized
# transaction that replaces the span rows, so binding and rows can never
# disagree about the synchronized generation.
SPAN_BINDINGS_TABLE = "evalbench_span_bindings"
_SPAN_BINDINGS_SCHEMA_FIELDS = (
    ("job_id", "STRING", "REQUIRED"),
    ("span_labels_table", "STRING", "REQUIRED"),
    ("view_policy", "STRING", "REQUIRED"),
    ("import_version", "STRING", "REQUIRED"),
    ("generation_id", "STRING", "REQUIRED"),
)
_READ_SPAN_BINDING_QUERY = """\
SELECT *
FROM `{bindings_table}`
WHERE job_id = @job_id
"""
# Span rows are derived data (a pure function of the published version's
# source rows), so the sync converges per pin — the same convergence rule
# as the failed_sessions view, not a second manifest. Unlike a bare
# delete-then-load, the replacement is staged first and then applied by one
# multi-statement transaction that claims the dataset's publish lock (so
# two concurrent syncs of one pin serialize instead of interleaving their
# DELETEs and loads into duplicate rows) and re-checks that the manifest
# row still carries the exact generation AND the exact canonical
# ``view_policy`` this sync derived its rows under (so neither a
# concurrent ``replace`` nor a concurrently re-committed score gate can
# end up pinned to span rows derived under other source content or
# another policy). The same transaction upserts the job's span-binding
# registry row, keeping binding and rows atomically in step. A failure
# anywhere before COMMIT — including a staging load that never ran —
# rolls back and leaves the previously published span rows and binding in
# place.
_SPAN_STALE_PIN_MESSAGE = (
    "evalbench span labels: the manifest row for this (job_id,"
    " import_version) no longer carries the generation and view_policy the"
    " span rows were derived under; the derived span rows are stale and"
    " were not written"
)
_SPAN_SYNC_SCRIPT = """\
DECLARE current_generation_rows INT64 DEFAULT 0;
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
  SET current_generation_rows = (
    SELECT COUNT(*)
    FROM `{manifest_table}`
    WHERE job_id = @job_id
      AND import_version = @import_version
      AND generation_id = @expected_generation_id
      AND view_policy = @expected_view_policy
  );
  IF current_generation_rows = 0 THEN
    RAISE USING MESSAGE = '{stale_pin_message}';
  END IF;
  DELETE FROM `{span_labels_table}`
  WHERE job_id = @job_id AND import_version = @import_version;
  INSERT INTO `{span_labels_table}` ({span_columns})
  SELECT {span_columns} FROM `{span_labels_staging}`;
  DELETE FROM `{bindings_table}`
  WHERE job_id = @job_id;
  INSERT INTO `{bindings_table}`
      (job_id, span_labels_table, view_policy, import_version, generation_id)
  VALUES (@job_id, @span_labels_table_name, @expected_view_policy,
      @import_version, @expected_generation_id);
  COMMIT TRANSACTION;
EXCEPTION WHEN ERROR THEN
  ROLLBACK TRANSACTION;
  RAISE;
END;
"""
# The pin-aware join boundary (#469): the retained span-labels table keeps
# one row set per (job_id, import_version), so a bare eval_id join fans
# out across retained versions. The companion view (the span table's name
# plus this suffix) is kept pinned to the exact manifest generation whose
# span rows were successfully synchronized — the same manifest row the
# failed_sessions view pins when the two are in step — so SQL consumers
# join failed_sessions to the view on eval_id alone, or the base table on
# job_id + import_version + generation_id + eval_id. The rendered guard
# (the pinned generation must still be the job's latest manifest
# generation) makes every base/span skew fail closed AT QUERY TIME: a
# later publish that advanced the session snapshot without its span sync
# completing — a failed sync after a changed-source replace, or a crash
# between the base and span transactions — leaves a view that exposes NO
# rows rather than pairing the new session snapshot with old span labels.
SPAN_LABELS_VIEW_SUFFIX = "_pinned"
_SPAN_VIEW_PIN_MARKER = "-- evalbench_span_labels pin: "
_SPAN_VIEW_BODY = """\
{pin_comment}
SELECT
  job_id,
  import_version,
  generation_id,
  eval_id,
  session_id,
  trace_id,
  span_id,
  failure_category,
  evidence,
  confidence,
  target_kind,
  taxonomy_version
FROM `{span_labels_table}`
WHERE job_id = {job_id_literal}
  AND import_version = {import_version_literal}
  AND generation_id = {generation_id_literal}
  AND {generation_id_literal} = (
    SELECT generation_id
    FROM `{manifest_table}`
    WHERE job_id = {job_id_literal}
    ORDER BY imported_at DESC, import_version DESC
    LIMIT 1
  )
"""
# The frozen Week-0 gate span-label derivation falls back to when the
# caller supplies no policy: localization must see the ``goal_completion``
# score gate, or the ``task/planning`` category is silently dropped (the
# #468 P1 finding). Never an empty policy.
NATIVE_SPAN_LABEL_POLICY = EvalScorePolicy({NATIVE_COMPARATOR: 1.0})

_READ_EVENTS_QUERY = """\
SELECT *
FROM `{source_table}`{snapshot_clause}{where_clause}
ORDER BY session_id, timestamp
"""

# The only table basename the native path may read. Everything else fails
# closed BEFORE any BigQuery client is created or query is built: the exit
# ramp reads production ``agent_events`` and nothing else, so an EvalBench
# source table or a BQAA-owned mirror (e.g. ``evalbench_agent_events``,
# which would resolve to the inherited default destination and self-feed)
# is rejected at parse time.
_SOURCE_TABLE_BASENAME = "agent_events"
# Rejected explicitly (not just by the agent_events rule) so the frozen
# no-EvalBench-tables contract has its own named guard and error.
_EVALBENCH_SOURCE_BASENAMES = frozenset({"configs", "results", "scores"})


def _parse_source_table(source_table: Any) -> tuple[str, str, str]:
  """Split and validate a ``project.dataset.table`` agent_events reference."""
  if not isinstance(source_table, str) or source_table.count(".") != 2:
    raise ValueError(
        "source_table must be a fully-qualified project.dataset.table"
        f" reference, got {source_table!r}"
    )
  project, dataset, table = source_table.split(".")
  _validate_source_segment("source_table project", project)
  _validate_source_segment("source_table dataset", dataset)
  _validate_source_segment("source_table table", table)
  if table in _EVALBENCH_SOURCE_BASENAMES:
    raise ValueError(
        f"source_table {source_table!r} names the EvalBench source table"
        f" {table!r}; the native path reads no EvalBench tables"
        " (#463 exit ramp)"
    )
  if table != _SOURCE_TABLE_BASENAME:
    raise ValueError(
        f"source_table {source_table!r} must reference a production"
        f" {_SOURCE_TABLE_BASENAME!r} table; refusing basename {table!r}"
        " before any query"
    )
  return project, dataset, table


def resolve_span_label_policy(
    policy: Optional[EvalScorePolicy],
) -> EvalScorePolicy:
  """The ONE effective policy of a span-labelled publish (#469).

  Span-level labels are derived under the frozen ``goal_completion >= 1.0``
  gate (``NATIVE_SPAN_LABEL_POLICY``), and the session-level denominator
  the snapshot commits (manifest ``view_policy``, failed_sessions view)
  must record the *same* gate — otherwise the view would say "no score
  gate" while span rows publish ``task/planning`` from that very gate. So
  a missing policy (the thin CLI default) resolves to the frozen gate, any
  other policy — the explicitly-empty ``EvalScorePolicy({})`` included —
  is merged with it (extra comparators keep their thresholds; they gate
  the denominator exactly as without span labels), and a policy that sets
  ``goal_completion`` to anything other than ``1.0`` is rejected: it asks
  for a denominator the frozen span derivation cannot match.
  """
  if policy is None:
    return NATIVE_SPAN_LABEL_POLICY
  gate = policy.min_scores.get(NATIVE_COMPARATOR)
  if gate is not None and gate != 1.0:
    raise ValueError(
        f"policy sets {NATIVE_COMPARATOR}={gate!r}, but span-label"
        " publication requires the frozen gate"
        f" {NATIVE_COMPARATOR}=1.0 (#469): the session denominator and"
        " the span rows must share one policy. Drop the conflicting"
        " threshold, or publish without span_labels_table"
    )
  if gate == 1.0:
    return policy
  return EvalScorePolicy(
      {**policy.min_scores, NATIVE_COMPARATOR: 1.0},
      missing_score_fails=policy.missing_score_fails,
  )


def _span_labels_view_body(
    *,
    span_labels_ref: str,
    manifest_ref: str,
    job_id: str,
    import_version: str,
    generation_id: str,
) -> str:
  """The pinned span-label view's query text: a pure function of its pin.

  The pin comment carries exactly the values the WHERE clause (pin plus
  latest-generation guard) renders, so ownership can be decided by
  re-rendering the claimed pin and comparing byte-for-byte — a view at
  the managed name whose body is anything else was not written by this
  sync and is never replaced.
  """
  pin = {
      "generation_id": generation_id,
      "import_version": import_version,
      "job_id": job_id,
      "manifest_table": manifest_ref,
      "span_labels_table": span_labels_ref,
  }
  return _SPAN_VIEW_BODY.format(
      pin_comment=(
          _SPAN_VIEW_PIN_MARKER
          + json.dumps(pin, sort_keys=True, ensure_ascii=True)
      ),
      span_labels_table=span_labels_ref,
      manifest_table=manifest_ref,
      job_id_literal=_sql_string_literal(job_id),
      import_version_literal=_sql_string_literal(import_version),
      generation_id_literal=_sql_string_literal(generation_id),
  )


def _span_labels_view_description(*, job_id: str, import_version: str) -> str:
  return (
      "EvalBench span-level G1 labels (#469) pinned to job_id"
      f" {job_id!r} import_version {import_version!r}; maintained by"
      " bigquery_agent_analytics.native_events.NativeAgentEventsRun"
      ".materialize"
  )


def _read_managed_span_view(client: Any, *, view_ref: str) -> Optional[Any]:
  """The managed pinned span-label view at ``view_ref``, or ``None``.

  ``None`` means nothing exists there. Anything that does exist must be a
  view whose body is byte-for-byte the rendering of the pin its first line
  claims (over the claimed span table); a table, a foreign view, or an
  edited body raises so the sync never replaces an object it cannot vouch
  for.
  """
  try:
    table = client.get_table(view_ref)
  except NotFound:
    return None
  view_query = getattr(table, "view_query", None)
  if isinstance(view_query, str):
    for raw in view_query.splitlines():
      line = raw.strip()
      if not line:
        continue
      if not line.startswith(_SPAN_VIEW_PIN_MARKER):
        break
      try:
        pin = json.loads(line[len(_SPAN_VIEW_PIN_MARKER) :])
      except json.JSONDecodeError:
        break
      if not (
          isinstance(pin, dict)
          and isinstance(pin.get("job_id"), str)
          and isinstance(pin.get("import_version"), str)
          and isinstance(pin.get("generation_id"), str)
          and _GENERATION_ID_PATTERN.fullmatch(pin["generation_id"])
          and isinstance(pin.get("manifest_table"), str)
          and isinstance(pin.get("span_labels_table"), str)
      ):
        break
      expected = _span_labels_view_body(
          span_labels_ref=pin["span_labels_table"],
          manifest_ref=pin["manifest_table"],
          job_id=pin["job_id"],
          import_version=pin["import_version"],
          generation_id=pin["generation_id"],
      )
      if view_query == expected:
        return table
      break
  raise ValueError(
      f"{view_ref!r} exists but is not a pinned span-labels view created"
      " by materialize() (or its definition was changed); choose another"
      " span_labels_table name rather than replacing it"
  )


def _check_span_view_binding(
    client: Any, *, view_ref: str, job_id: str
) -> Optional[Any]:
  """Refuse a pinned span view owned by another job (fail-fast + authority)."""
  existing = _read_managed_span_view(client, view_ref=view_ref)
  if existing is not None:
    pin = json.loads(
        existing.view_query.splitlines()[0][len(_SPAN_VIEW_PIN_MARKER) :]
    )
    if pin["job_id"] != job_id:
      raise ValueError(
          f"pinned span-labels view {view_ref!r} is pinned to job"
          f" {pin['job_id']!r}, not {job_id!r}; pass a different"
          " span_labels_table for this job"
      )
  return existing


def _read_span_binding(
    client: Any,
    *,
    bindings_ref: str,
    job_id: str,
    location: Optional[str],
) -> Optional[dict[str, Any]]:
  """The job's committed span binding, or ``None`` when it has none.

  A missing registry table means no job in the dataset ever opted in, so
  nothing is queried and the plain native publish stays byte-identical.
  The row is trusted state written only by the span sync transaction and
  is parsed strictly: a malformed table name, policy, or generation raises
  rather than silently unbinding the job.
  """
  try:
    client.get_table(bindings_ref)
  except NotFound:
    return None
  job_config = bigquery.QueryJobConfig(
      query_parameters=[
          bigquery.ScalarQueryParameter("job_id", "STRING", job_id)
      ]
  )
  job_config = with_sdk_labels(job_config, feature=_NATIVE_FEATURE)
  query_args: dict[str, Any] = {"job_config": job_config}
  if location is not None:
    query_args["location"] = location
  query = _READ_SPAN_BINDING_QUERY.format(bindings_table=bindings_ref)
  rows = [_plain_row(row) for row in client.query(query, **query_args).result()]
  if not rows:
    return None
  if len(rows) > 1:
    raise ValueError(
        f"span-binding registry {bindings_ref!r} has {len(rows)} rows for"
        f" job {job_id!r}; expected at most one"
    )
  binding = rows[0]
  _validate_destination_table(
      "span binding span_labels_table", binding.get("span_labels_table")
  )
  if _policy_from_column(binding.get("view_policy")) is None:
    raise ValueError(
        f"span binding for job {job_id!r} in {bindings_ref!r} records no"
        " view_policy; the registry row is corrupt"
    )
  generation_id = binding.get("generation_id")
  if not isinstance(generation_id, str) or not _GENERATION_ID_PATTERN.fullmatch(
      generation_id
  ):
    raise ValueError(
        f"span binding for job {job_id!r} in {bindings_ref!r} has a"
        " malformed generation_id; the registry row is corrupt"
    )
  return binding


@dataclasses.dataclass(frozen=True)
class NativeAgentEventsRun(EvalBenchRun):
  """One production ``agent_events`` corpus loaded for native publication.

  A sibling of ``EvalBenchRun`` that reuses its frozen publish machinery:
  ``materialize`` (pin identity, lock, publish transaction, manifest with
  ``view_policy``, ``failed_sessions`` view sync) is inherited verbatim, so
  the native snapshot carries exactly the adapter's ``(job_id,
  import_version)`` contract. Only the inputs differ: ``source_events``
  holds ADK ``agent_events``-shaped rows read from ``source_table``, and
  the EvalBench source fields (``results`` / ``scores`` / ``config_rows``)
  are required to stay empty — this path reads no EvalBench tables.

  The inherited ``project_id`` / ``evalbench_dataset`` fields record the
  *source* project and dataset of ``source_table`` (they land in the
  manifest as ``source_project`` / ``source_dataset``, so equal rows read
  from another source are a different version, exactly as for the adapter).
  """

  source_table: str = ""
  source_events: tuple[dict[str, Any], ...] = dataclasses.field(
      default_factory=tuple, repr=False
  )

  def __post_init__(self) -> None:
    if self.results or self.scores or self.config_rows:
      raise ValueError(
          "NativeAgentEventsRun reads no EvalBench source tables; results,"
          " scores, and config_rows must stay empty (#463 exit ramp)"
      )
    project, dataset, _ = _parse_source_table(self.source_table)
    if project != self.project_id or dataset != self.evalbench_dataset:
      raise ValueError(
          f"source_table {self.source_table!r} does not live in the declared"
          f" source project/dataset {self.project_id!r}."
          f"{self.evalbench_dataset!r}"
      )

  @property
  def source_project(self) -> str:
    return self.project_id

  @property
  def source_dataset(self) -> str:
    return self.evalbench_dataset

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
      policy: Optional[EvalScorePolicy] = None,
      span_labels_table: Optional[str] = None,
      bq_client: Optional[Any] = None,
  ) -> EvalBenchImportResult:
    """Publish the native snapshot; the read-only source is never a target.

    Defense in depth over ``_parse_source_table``'s agent_events-only rule
    and the inherited reserved-destination check: every fully-qualified
    destination this publish can write (events, scores, manifest, lock,
    the failed-sessions view, the span-labels table, and the span-binding
    registry) is compared against ``source_table`` before anything is
    written, so a self-feeding publish is rejected with zero writes (the
    only earlier query is the read-only span-binding lookup on the fixed
    BQAA-owned registry, skipped entirely when the registry table does
    not exist).

    ``span_labels_table`` opts in to span-level G1 publication (#469): the
    #466 localization library's labels for every failed session of this
    snapshot are kept as rows of ``{target}.{span_labels_table}``, keyed by
    the same ``(job_id, import_version)`` pin — plus the exact manifest
    ``generation_id`` they were synchronized under — and joinable to
    ``failed_sessions`` via the frozen ``eval_id`` rule. Opting in also
    resolves ONE effective score policy for the whole publish
    (``resolve_span_label_policy``): the frozen ``goal_completion >= 1.0``
    gate span derivation runs under is merged into the caller's policy (or
    rejected when the caller pins another ``goal_completion`` threshold)
    and that same policy is what the manifest ``view_policy`` and the
    failed-sessions view record — the session denominator and the span
    rows can never disagree about the gate.

    Opting in is DURABLE: the dataset's span-binding registry
    (``SPAN_BINDINGS_TABLE``) records the job's span table, its resolved
    policy, and the synchronized generation, and every later native call
    for a bound job — ``span_labels_table`` passed or not — resolves the
    same ONE policy and re-derives and re-synchronizes the span rows, so
    an ordinary call can neither rewrite the committed gate to NULL nor
    advance ``failed_sessions`` while the span snapshot stays behind. A
    bound job whose new corpus cannot be span-labelled (no real
    ``span_id``) fails the whole publish closed BEFORE the base snapshot
    or the denominator moves; a bound job cannot switch span tables (one
    binding per job).

    The rows are derived BEFORE anything is written, so a failed session
    whose target row carries no real ``span_id`` fails the whole publish
    closed (no synthetic span identifiers). Like the failed-sessions view
    — and unlike the immutable events/scores snapshot — the span rows are
    derived state and are re-synchronized on every successful call,
    ``unchanged`` included, so a version published before span labels
    existed gains its rows on the next call. The sync stages the rows into
    an expiring staging table and replaces the pin's slice — and the
    binding row — in one lock-serialized transaction keyed to the exact
    manifest generation AND canonical ``view_policy`` it derived from, so
    a failed or concurrent sync leaves the previously published span rows
    and binding in place (never a half-replaced or duplicated slice, and
    never rows derived under another generation's content or gate);
    re-running heals it. A companion view named
    ``{span_labels_table}_pinned`` (``SPAN_LABELS_VIEW_SUFFIX``) is kept
    pinned to the exact generation whose span rows were synchronized, and
    its rendered guard exposes rows only while that generation is still
    the job's latest publication — the same manifest row the
    failed-sessions view pins — so any base/span skew (a span sync that
    failed after the base snapshot committed) yields an EMPTY view rather
    than stale labels joined onto the new session snapshot. The retained
    table keeps every version's rows and a bare ``eval_id`` join would
    fan out across them: join ``failed_sessions`` to the view on
    ``eval_id`` alone, or to the base table on ``job_id + import_version
    + generation_id + eval_id``. Span-level rows only localize; the
    session-level ``failed_sessions`` + G1 contract remains the
    denominator and is untouched by this option.
    """
    resolved_project = target_project or self.project_id
    prefix = f"{resolved_project}.{target_dataset}"
    bindings_ref = f"{prefix}.{SPAN_BINDINGS_TABLE}"
    resolved_version = import_version
    span_rows: Optional[list[dict[str, Any]]] = None
    span_labels_view: Optional[str] = None
    client = bq_client
    if client is None:
      # One client serves the binding lookup, the inherited publish, and
      # the span-label sync.
      client = make_bq_client(resolved_project, location=self.location)
    # The durable opt-in: a job an earlier publish bound to a span table
    # keeps its span snapshot maintained on EVERY later native call —
    # policy resolution and span derivation included — so an ordinary call
    # cannot silently rewrite the committed score gate to NULL or advance
    # failed_sessions past the span rows. The registry read is the only
    # query before validation, and it touches a fixed BQAA-owned table.
    binding = _read_span_binding(
        client,
        bindings_ref=bindings_ref,
        job_id=self.job_id,
        location=self.location,
    )
    if binding is not None:
      bound_table = str(binding["span_labels_table"])
      if span_labels_table is None:
        span_labels_table = bound_table
      elif span_labels_table != bound_table:
        raise ValueError(
            f"native job {self.job_id!r} is bound to span_labels_table"
            f" {bound_table!r} (span-binding registry {bindings_ref!r});"
            f" refusing {span_labels_table!r}. One span table per job —"
            " publish under a new job_id to use a different table"
        )
    if span_labels_table is not None:
      _validate_destination_table("span_labels_table", span_labels_table)
      span_labels_view = span_labels_table + SPAN_LABELS_VIEW_SUFFIX
      _validate_destination_table("span_labels_view", span_labels_view)
      reserved = (
          events_table,
          scores_table,
          MANIFEST_TABLE,
          LOCK_TABLE,
          SPAN_BINDINGS_TABLE,
          failed_sessions_view,
      )
      if span_labels_table in reserved:
        raise ValueError(
            f"span_labels_table {span_labels_table!r} must not name an"
            " import table, the span-binding registry, or the"
            " failed-sessions view"
        )
      if span_labels_view in reserved + (span_labels_table,):
        raise ValueError(
            f"the pinned span-labels view {span_labels_view!r} derived from"
            f" span_labels_table {span_labels_table!r} would name an import"
            " table, the failed-sessions view, or the span table itself;"
            " choose another span_labels_table name"
        )
      # One effective policy for span rows AND the session denominator the
      # inherited publish commits (manifest view_policy + view rendering).
      policy = resolve_span_label_policy(policy)
      # The pin the rows carry must be the version the publish commits, so
      # a missing explicit version resolves to the same content fingerprint
      # the inherited materialize derives.
      if resolved_version is None:
        resolved_version = _derived_import_version(self.fingerprints())
      # Derived before anything is written: a label the localizer refuses
      # (no real span_id) aborts the whole publish — the bound-job
      # maintenance path included — BEFORE failed_sessions can advance
      # past the span snapshot.
      span_rows = self.to_span_label_rows(
          import_version=resolved_version, policy=policy
      )
    destinations = [events_table, scores_table, MANIFEST_TABLE, LOCK_TABLE]
    if failed_sessions_view is not None:
      destinations.append(failed_sessions_view)
    if span_labels_table is not None:
      destinations.append(span_labels_table)
      destinations.append(span_labels_view)
      destinations.append(SPAN_BINDINGS_TABLE)
    for destination in destinations:
      destination_ref = f"{prefix}.{destination}"
      if destination_ref == self.source_table:
        raise ValueError(
            f"destination {destination_ref!r} is the read-only native"
            f" source table {self.source_table!r}; the native path never"
            " writes its source (#463 exit ramp)"
        )
    if span_labels_table is not None:
      # Fail fast on a foreign object at the managed view name before the
      # snapshot commits; the sync re-reads it as the authority.
      _check_span_view_binding(
          client,
          view_ref=f"{prefix}.{span_labels_view}",
          job_id=self.job_id,
      )
    result = super().materialize(
        target_dataset=target_dataset,
        target_project=target_project,
        events_table=events_table,
        scores_table=scores_table,
        import_version=resolved_version,
        replace=replace,
        imported_at=imported_at,
        failed_sessions_view=failed_sessions_view,
        policy=policy,
        bq_client=client,
    )
    if span_labels_table is None:
      return result
    assert span_rows is not None  # Derived above whenever the table is set.
    span_labels_ref = f"{prefix}.{span_labels_table}"
    span_view_ref = f"{prefix}.{span_labels_view}"
    self._sync_span_labels(
        client,
        span_labels_ref=span_labels_ref,
        span_view_ref=span_view_ref,
        bindings_ref=bindings_ref,
        manifest_ref=f"{prefix}.{MANIFEST_TABLE}",
        lock_ref=f"{prefix}.{LOCK_TABLE}",
        import_version=result.import_version,
        rows=span_rows,
        policy=policy,
        status=result.status,
    )
    return dataclasses.replace(
        result,
        span_labels_table=span_labels_ref,
        span_label_row_count=len(span_rows),
        span_labels_view=span_view_ref,
    )

  def to_span_label_rows(
      self,
      *,
      import_version: str,
      policy: Optional[EvalScorePolicy] = None,
  ) -> list[dict[str, Any]]:
    """Span-level G1 rows for this run's failed sessions, offline (#469).

    A pure reuse of the landed #466 localizer: ``label_native_run`` emits
    one ``SpanFailureLabel`` per tripped G1-frozen category of each failed
    session, anchored to a real native ``span_id`` (a row whose target
    carries none fails closed inside the localizer — no synthetic span
    identifiers). Each label becomes one published row carrying the RFC
    tuple, the frozen first-8 ``eval_id`` join identity, and the
    ``(job_id, import_version)`` pin. ``policy`` goes through
    ``resolve_span_label_policy``: ``None`` resolves to the frozen Week-0
    gate (``NATIVE_SPAN_LABEL_POLICY``, ``goal_completion >= 1.0``), any
    other policy — the truthy-but-empty ``EvalScorePolicy({})`` included —
    is merged with that gate, never allowed to drop it, which would
    silently lose ``task/planning`` (the #468 P1 finding).
    """
    # Imported here, not at module level: span_taxonomy imports this
    # module's run class, so the localization layer stays downstream.
    from .span_taxonomy import label_native_run

    _validate_import_version(import_version)
    labels = label_native_run(self, policy=resolve_span_label_policy(policy))
    return [
        {
            "job_id": self.job_id,
            "import_version": import_version,
            "eval_id": label.eval_id,
            "session_id": label.session_id,
            "trace_id": label.trace_id,
            "span_id": label.span_id,
            "failure_category": label.failure_category,
            "evidence": label.evidence,
            "confidence": label.confidence,
            "target_kind": label.target_kind,
            "taxonomy_version": TAXONOMY_VERSION,
        }
        for label in labels
    ]

  def _sync_span_labels(
      self,
      client: Any,
      *,
      span_labels_ref: str,
      span_view_ref: str,
      bindings_ref: str,
      manifest_ref: str,
      lock_ref: str,
      import_version: str,
      rows: list[dict[str, Any]],
      policy: EvalScorePolicy,
      status: str,
  ) -> None:
    """Converge one pin's span rows, binding, and view — or change nothing.

    Runs only after the inherited publish succeeded, so the pin it keys on
    is committed manifest state. The rows are a deterministic function of
    the version's source content and the resolved ``policy``
    (fingerprint-identical sources derive identical rows), so re-running
    is idempotent and an interrupted sync heals on the next call — which
    is exactly what the error asks for. Until that re-run, the pinned
    view's latest-generation guard keeps a base snapshot that advanced
    past its span rows failing closed instead of joining stale labels.
    """
    try:
      generation_id = self._publish_span_labels(
          client,
          span_labels_ref=span_labels_ref,
          bindings_ref=bindings_ref,
          manifest_ref=manifest_ref,
          lock_ref=lock_ref,
          import_version=import_version,
          rows=rows,
          policy=policy,
      )
      self._sync_span_labels_view(
          client,
          span_view_ref=span_view_ref,
          span_labels_ref=span_labels_ref,
          manifest_ref=manifest_ref,
          generation_id=generation_id,
      )
    except Exception as exc:  # noqa: BLE001
      raise ValueError(
          f"native job {self.job_id!r} import_version {import_version!r} is"
          f" published (status {status!r}) but its span labels could not be"
          f" synchronized: {exc}. Previously published span rows for this"
          " pin are unchanged (and the pinned view exposes rows only for"
          " the manifest generation they were synchronized under); re-run"
          " materialize() to retry the sync (the import itself then"
          " reports 'unchanged')"
      ) from exc

  def _publish_span_labels(
      self,
      client: Any,
      *,
      span_labels_ref: str,
      bindings_ref: str,
      manifest_ref: str,
      lock_ref: str,
      import_version: str,
      rows: list[dict[str, Any]],
      policy: EvalScorePolicy,
  ) -> str:
    """Replace one pin's span slice + binding atomically, under the lock.

    Returns the manifest ``generation_id`` the rows were synchronized
    under. The committed manifest row is re-read and must still carry the
    source fingerprints AND the canonical ``view_policy`` of the resolved
    ``policy`` these rows were derived under — a delayed writer that finds
    a newer generation committed under another gate fails closed instead
    of adopting it. The rows (stamped with that generation) are loaded
    into an expiring staging table, then one multi-statement transaction
    claims the dataset's import lock (two concurrent syncs of one pin
    serialize; BigQuery cancels the second), re-checks generation and
    ``view_policy`` inside the transaction, and only then deletes and
    re-inserts the keyed slice and upserts the job's binding row. A
    failure anywhere rolls back, so the previously published span rows
    and binding are preserved; staging is always cleaned up (and expires
    regardless).
    """
    table = bigquery.Table(
        span_labels_ref, schema=_schema(_SPAN_LABELS_SCHEMA_FIELDS)
    )
    table.clustering_fields = ["job_id", "import_version", "session_id"]
    client.create_table(table, exists_ok=True)
    client.create_table(
        bigquery.Table(
            bindings_ref, schema=_schema(_SPAN_BINDINGS_SCHEMA_FIELDS)
        ),
        exists_ok=True,
    )
    manifest = _read_manifest(
        client,
        manifest_ref=manifest_ref,
        job_id=self.job_id,
        import_version=import_version,
        location=self.location,
        feature=_NATIVE_FEATURE,
    )
    if manifest is None:
      raise ValueError(
          f"manifest row for job {self.job_id!r} import_version"
          f" {import_version!r} disappeared from {manifest_ref!r} after"
          " publishing"
      )
    fingerprints = self.fingerprints()
    if any(manifest.get(key) != value for key, value in fingerprints.items()):
      raise ValueError(
          f"import_version {import_version!r} was re-published concurrently"
          " with different source fingerprints; the derived span rows are"
          " stale and were not written"
      )
    expected_policy = _policy_column(policy)
    if manifest.get("view_policy") != expected_policy:
      raise ValueError(
          f"import_version {import_version!r} now records view_policy"
          f" {manifest.get('view_policy')!r}, not the resolved span policy"
          f" {expected_policy!r} these rows were derived under; a"
          " concurrent call re-committed the gate, so the derived span"
          " rows are stale and were not written"
      )
    generation_id = _manifest_generation_id(manifest)
    stamped = [{**row, "generation_id": generation_id} for row in rows]
    staging_ref = f"{span_labels_ref}_staging_{uuid.uuid4().hex[:8]}"
    try:
      _load_staging(
          client, staging_ref, stamped, _schema(_SPAN_LABELS_SCHEMA_FIELDS)
      )
      # The sentinel must exist before the transaction starts (the
      # ``unchanged`` fast path never ran the inherited publish, which
      # otherwise seeds it); seeding is INSERT-only and idempotent.
      _seed_import_lock(client, lock_ref=lock_ref, location=self.location)
      span_columns = ", ".join(
          name for name, _, _ in _SPAN_LABELS_SCHEMA_FIELDS
      )
      script = _SPAN_SYNC_SCRIPT.format(
          lock_table=lock_ref,
          lock_id=_IMPORT_LOCK_ID,
          lock_missing_message=_LOCK_MISSING_MESSAGE,
          manifest_table=manifest_ref,
          stale_pin_message=_SPAN_STALE_PIN_MESSAGE,
          span_labels_table=span_labels_ref,
          span_columns=span_columns,
          span_labels_staging=staging_ref,
          bindings_table=bindings_ref,
      )
      parameters = _import_parameters(self.job_id, import_version)
      parameters.append(
          bigquery.ScalarQueryParameter(
              "expected_generation_id", "STRING", generation_id
          )
      )
      parameters.append(
          bigquery.ScalarQueryParameter(
              "expected_view_policy", "STRING", expected_policy
          )
      )
      parameters.append(
          bigquery.ScalarQueryParameter(
              "span_labels_table_name",
              "STRING",
              span_labels_ref.rsplit(".", 1)[1],
          )
      )
      job_config = bigquery.QueryJobConfig(query_parameters=parameters)
      job_config = with_sdk_labels(job_config, feature=_NATIVE_FEATURE)
      query_args: dict[str, Any] = {"job_config": job_config}
      if self.location is not None:
        query_args["location"] = self.location
      try:
        client.query(script, **query_args).result()
      except ValueError:
        raise
      except Exception as exc:  # noqa: BLE001
        if _SPAN_STALE_PIN_MESSAGE in str(exc):
          raise ValueError(_SPAN_STALE_PIN_MESSAGE) from exc
        if _CONCURRENT_UPDATE_MARKER in str(exc).lower():
          raise ValueError(
              "BigQuery cancelled this span-label sync because a concurrent"
              f" import into {lock_ref.rsplit('.', 1)[0]!r} claimed the"
              f" import lock ({lock_ref!r}) first; span rows were not"
              " changed"
          ) from exc
        raise
    finally:
      _drop_staging_tables(client, (staging_ref,))
    return generation_id

  def _sync_span_labels_view(
      self,
      client: Any,
      *,
      span_view_ref: str,
      span_labels_ref: str,
      manifest_ref: str,
      generation_id: str,
  ) -> None:
    """Pin the view to the exact generation just synchronized — or stand
    down.

    The same reconcile shape as the failed-sessions view: each attempt
    re-reads the view (ownership by byte-for-byte re-rendering, ETag) and
    then the latest manifest row of this job. But unlike the
    failed-sessions view, what gets written is ONLY the rendering of the
    generation whose span rows this call just published — and only while
    that generation is still the job's latest publication. A caller
    re-synchronizing an older retained version stands down (the latest
    generation's own sync owns the view), and a delayed caller superseded
    mid-sync stands down too, so the view can never be repinned to a
    generation whose span rows were not synchronized; any stale pin left
    behind fails closed through the rendered latest-generation guard
    instead of exposing rows. Create is create-if-absent, replace is
    ETag-conditional, and either race re-decides a bounded number of
    times before failing closed.
    """
    for _ in range(_VIEW_SYNC_ATTEMPTS):
      existing = _check_span_view_binding(
          client, view_ref=span_view_ref, job_id=self.job_id
      )
      latest = _read_latest_manifest(
          client,
          manifest_ref=manifest_ref,
          job_id=self.job_id,
          location=self.location,
      )
      if latest is None:
        raise ValueError(
            f"native job {self.job_id!r} has no manifest row in"
            f" {manifest_ref!r} after publishing; cannot pin the"
            " span-labels view"
        )
      if _manifest_generation_id(latest) != generation_id:
        return
      body = _span_labels_view_body(
          span_labels_ref=span_labels_ref,
          manifest_ref=manifest_ref,
          job_id=str(latest["job_id"]),
          import_version=str(latest["import_version"]),
          generation_id=generation_id,
      )
      description = _span_labels_view_description(
          job_id=str(latest["job_id"]),
          import_version=str(latest["import_version"]),
      )
      if existing is None:
        table = bigquery.Table(span_view_ref)
        table.view_query = body
        table.description = description
        try:
          client.create_table(table, exists_ok=False)
        except Conflict:
          continue
        return
      if existing.view_query == body:
        return
      if not getattr(existing, "etag", None):
        raise ValueError(
            f"pinned span-labels view {span_view_ref!r} has no ETag;"
            " refusing an unconditional replace"
        )
      existing.view_query = body
      existing.description = description
      try:
        client.update_table(existing, ["view_query", "description"])
      except PreconditionFailed:
        continue
      return
    raise ValueError(
        f"pinned span-labels view {span_view_ref!r} changed concurrently"
        f" {_VIEW_SYNC_ATTEMPTS} times; re-run materialize() to retry"
    )

  @classmethod
  def from_agent_events(
      cls,
      events: Any,
      *,
      source_table: str,
      job_id: str,
      location: Optional[str] = None,
      snapshot_at: Optional[datetime] = None,
  ) -> "NativeAgentEventsRun":
    """Build a run from in-memory ``agent_events``-shaped rows (offline).

    ``events`` is any iterable of mappings shaped like the ADK plugin's
    ``agent_events`` rows; ``source_table`` is the fully-qualified
    ``project.dataset.table`` they came from (provenance only — nothing is
    read here). This is the fixture-testable path: no BigQuery client is
    created and no query runs.
    """
    project, dataset, _ = _parse_source_table(source_table)
    if not isinstance(job_id, str) or not job_id:
      raise ValueError("job_id must be a non-empty string")
    return cls(
        project_id=project,
        evalbench_dataset=dataset,
        job_id=job_id,
        location=location,
        snapshot_at=snapshot_at,
        source_table=source_table,
        source_events=tuple(dict(event) for event in events),
    )

  # Replaces (does not extend) the EvalBench-source reader: the native path
  # reads exactly one table, the production agent_events, and only reads it.
  @classmethod
  def from_bigquery(  # type: ignore[override]  # pylint: disable=arguments-differ
      cls,
      *,
      source_table: str,
      job_id: str,
      session_ids: Optional[list[str]] = None,
      location: Optional[str] = None,
      snapshot_at: Optional[datetime] = None,
      bq_client: Optional[Any] = None,
  ) -> "NativeAgentEventsRun":
    """Read production ``agent_events`` rows (SELECT only, never DML).

    Args:
      source_table: Fully-qualified ``project.dataset.table`` reference of
        the production ADK ``agent_events`` table to read.
      job_id: The native snapshot's job identifier (the pin's first half).
      session_ids: Optional session ids to restrict the read to.
      location: Optional BigQuery location.
      snapshot_at: Optional timezone-aware timestamp; when set, the read
        uses ``FOR SYSTEM_TIME AS OF`` so a live table cannot mix versions.
      bq_client: Optional test-compatible or caller-configured client.
    """
    project, _, _ = _parse_source_table(source_table)
    if not isinstance(job_id, str) or not job_id:
      raise ValueError("job_id must be a non-empty string")
    if snapshot_at is not None and (
        not isinstance(snapshot_at, datetime) or snapshot_at.tzinfo is None
    ):
      raise ValueError("snapshot_at must be a timezone-aware datetime")

    client = bq_client or make_bq_client(project, location=location)
    parameters: list[bigquery.ScalarQueryParameter] = []
    snapshot_clause = ""
    if snapshot_at is not None:
      snapshot_clause = " FOR SYSTEM_TIME AS OF @snapshot_at"
      parameters.append(
          bigquery.ScalarQueryParameter("snapshot_at", "TIMESTAMP", snapshot_at)
      )
    where_clause = ""
    if session_ids:
      where_clause = "\nWHERE session_id IN UNNEST(@session_ids)"
      parameters.append(
          bigquery.ArrayQueryParameter(
              "session_ids", "STRING", [str(value) for value in session_ids]
          )
      )
    job_config = bigquery.QueryJobConfig(query_parameters=parameters)
    job_config = with_sdk_labels(job_config, feature=_NATIVE_FEATURE)
    query_args: dict[str, Any] = {"job_config": job_config}
    if location is not None:
      query_args["location"] = location
    query = _READ_EVENTS_QUERY.format(
        source_table=source_table,
        snapshot_clause=snapshot_clause,
        where_clause=where_clause,
    )
    rows = tuple(
        _plain_row(row) for row in client.query(query, **query_args).result()
    )
    return cls.from_agent_events(
        rows,
        source_table=source_table,
        job_id=job_id,
        location=location,
        snapshot_at=snapshot_at,
    )

  def skipped_session_ids(self) -> tuple[str, ...]:
    """Sessions dropped from the snapshot: no ``USER_MESSAGE_RECEIVED``.

    Mirrors the slice-5 synthesizer: a trace with no user prompt is skipped
    rather than given one, so the denominator stays the prompted sessions.
    """
    _, skipped = self._kept_and_skipped()
    return skipped

  def to_agent_event_rows(
      self, *, import_version: Optional[str] = None
  ) -> list[dict[str, Any]]:
    """Normalize the source rows to the published snapshot event shape.

    Rows keep their real ``session_id`` / ``user_id`` / timestamps /
    statuses; ``attributes`` additionally carry the joinable
    ``evalbench_scenario_id`` (first-8 identity), ``experiment_id`` (the
    job) and ``bqaa_native_source_table`` provenance. A session that never
    logged ``AGENT_COMPLETED`` has its first prompt row published with
    ``status=ERROR`` (original status preserved in
    ``attributes.bqaa_native_source_status``) — the same process-failure
    marker the adapter publishes for a failed returncode, so
    ``failed_sessions_sql`` reports it as ``process_failed``.
    """
    if import_version is not None:
      _validate_import_version(import_version)
    kept, _ = self._kept_and_skipped()
    scenario_ids = _native_scenario_ids(tuple(sorted(kept)))
    rows: list[dict[str, Any]] = []
    for session_id in sorted(kept):
      events = kept[session_id]
      completed = any(
          event.get("event_type") == _COMPLETION_EVENT for event in events
      )
      marker_index = next(
          (
              index
              for index, event in enumerate(events)
              if event.get("event_type") == _PROMPT_EVENT
          ),
          0,
      )
      for index, source in enumerate(events):
        row = self._native_event_row(
            source, scenario_id=scenario_ids[session_id]
        )
        if not completed and index == marker_index:
          row["attributes"]["bqaa_native_source_status"] = row["status"]
          row["status"] = "ERROR"
          if row["error_message"] is None:
            row["error_message"] = _MISSING_COMPLETION_ERROR
        rows.append(row)
    return rows

  def to_score_rows(self, *, import_version: str) -> list[dict[str, Any]]:
    """One deterministic ``goal_completion`` score row per kept session.

    Derived from the session alone — no live BigQuery, no live/LLM judge:
    ``1.0`` when the session logged ``AGENT_COMPLETED``, ``0.0`` otherwise.
    ``session_id`` is the real ADK id (matching the event rows, so the
    ``failed_sessions_sql`` join stays aligned) and ``scenario_id`` is the
    joinable first-8 identity.
    """
    _validate_import_version(import_version)
    rows: list[dict[str, Any]] = []
    for fact, prompt in self._score_facts_with_prompts():
      rows.append(
          {
              "job_id": self.job_id,
              "import_version": import_version,
              "scenario_id": fact["scenario_id"],
              "session_id": fact["session_id"],
              "comparator": fact["comparator"],
              "score": fact["score"],
              "source_row": {
                  "derived_from": self.source_table,
                  "rule": _NATIVE_SCORE_RULE,
                  "completed": fact["score"] == 1.0,
                  "prompt": prompt,
              },
          }
      )
    return rows

  def fingerprints(self) -> dict[str, str]:
    """Content identity of the native snapshot (adapter guard semantics).

    ``results_fingerprint`` covers the source event rows and
    ``scores_fingerprint`` the deterministic score facts, so a changed
    source (or a changed derivation) is a new derived ``import_version``
    and a conflicting explicit one. ``configs_fingerprint`` pins the
    source table reference — equal rows read from another table are a
    different version, matching the adapter's provenance rule.
    """
    return {
        "results_fingerprint": _fingerprint_rows(self.source_events),
        "scores_fingerprint": _fingerprint_rows(
            tuple(fact for fact, _ in self._score_facts_with_prompts())
        ),
        "configs_fingerprint": _fingerprint_rows(
            ({"bqaa_native_source_table": self.source_table},)
        ),
    }

  def _score_facts_with_prompts(
      self,
  ) -> list[tuple[dict[str, Any], Optional[str]]]:
    """Version-independent score derivation, in ``session_id`` order."""
    kept, _ = self._kept_and_skipped()
    scenario_ids = _native_scenario_ids(tuple(sorted(kept)))
    facts: list[tuple[dict[str, Any], Optional[str]]] = []
    for session_id in sorted(kept):
      events = kept[session_id]
      completed = any(
          event.get("event_type") == _COMPLETION_EVENT for event in events
      )
      fact = {
          "session_id": session_id,
          "scenario_id": scenario_ids[session_id],
          "comparator": NATIVE_COMPARATOR,
          "score": 1.0 if completed else 0.0,
      }
      facts.append((fact, _prompt_text(events)))
    return facts

  def _kept_and_skipped(
      self,
  ) -> tuple[dict[str, list[dict[str, Any]]], tuple[str, ...]]:
    """Group source rows by session; keep prompted sessions only."""
    sessions: dict[str, list[tuple[datetime, int, dict[str, Any]]]] = {}
    for index, source in enumerate(self.source_events):
      if not isinstance(source, Mapping):
        raise ValueError(
            f"agent_events row {index} must be a mapping, got"
            f" {type(source).__name__}"
        )
      session_id = _usable_text(source.get("session_id"))
      if session_id is None:
        raise ValueError(f"agent_events row {index} is missing session_id")
      timestamp = _parse_timestamp(source.get("timestamp"))
      if timestamp is None:
        raise ValueError(
            f"agent_events row {index} (session {session_id!r}) has no"
            " parseable timestamp"
        )
      sessions.setdefault(session_id, []).append(
          (timestamp, index, dict(source))
      )
    kept: dict[str, list[dict[str, Any]]] = {}
    skipped: list[str] = []
    for session_id, entries in sessions.items():
      ordered = [row for _, _, row in sorted(entries, key=lambda e: e[:2])]
      if any(row.get("event_type") == _PROMPT_EVENT for row in ordered):
        kept[session_id] = ordered
      else:
        skipped.append(session_id)
    return kept, tuple(sorted(skipped))

  def _native_event_row(
      self, source: Mapping[str, Any], *, scenario_id: str
  ) -> dict[str, Any]:
    """One source row normalized to the published event columns."""
    attributes = dict(_as_mapping(_structured(source.get("attributes"))))
    attributes["experiment_id"] = self.job_id
    attributes["evalbench_scenario_id"] = scenario_id
    attributes["bqaa_native_source_table"] = self.source_table
    timestamp = _parse_timestamp(source.get("timestamp"))
    assert timestamp is not None  # _kept_and_skipped validated it.
    return {
        "timestamp": timestamp.isoformat(),
        "event_type": _text_or_none(source.get("event_type")),
        "agent": _text_or_none(source.get("agent")),
        "session_id": _usable_text(source.get("session_id")),
        "invocation_id": _text_or_none(source.get("invocation_id")),
        "user_id": _text_or_none(source.get("user_id")),
        "trace_id": _text_or_none(source.get("trace_id")),
        "span_id": _text_or_none(source.get("span_id")),
        "parent_span_id": _text_or_none(source.get("parent_span_id")),
        "content": _json_safe(_structured(source.get("content"))),
        "content_parts": _json_safe(source.get("content_parts") or []),
        "attributes": _json_safe(attributes),
        "latency_ms": _json_safe(
            _as_mapping(_structured(source.get("latency_ms")))
        ),
        "status": _text_or_none(source.get("status")),
        "error_message": _text_or_none(source.get("error_message")),
        "is_truncated": bool(source.get("is_truncated") or False),
    }


def _native_scenario_ids(session_ids: tuple[str, ...]) -> dict[str, str]:
  """First-8 joinable identity, falling back to the full id on collision.

  Same frozen rule as the slice-5 synthesizer and the Week 0 identity table
  (``examples/evalbench_mvp_e2e.md``): the scenario id is the first
  ``_SHORT_ID_LENGTH`` characters of the ADK session id unless two kept
  sessions share that prefix, in which case both keep their full ids.
  """
  prefixes: dict[str, list[str]] = {}
  for session_id in session_ids:
    prefixes.setdefault(session_id[:_SHORT_ID_LENGTH], []).append(session_id)
  return {
      session_id: (prefix if len(owners) == 1 else session_id)
      for prefix, owners in prefixes.items()
      for session_id in owners
  }


def _prompt_text(events: list[dict[str, Any]]) -> Optional[str]:
  """The session's real prompt: first ``USER_MESSAGE_RECEIVED`` text."""
  for event in events:
    if event.get("event_type") != _PROMPT_EVENT:
      continue
    content = _structured(event.get("content"))
    if isinstance(content, Mapping):
      for key in ("text_summary", "text"):
        text = _usable_text(content.get(key))
        if text is not None:
          return text
      return None
    return _usable_text(content)
  return None


def _text_or_none(value: Any) -> Optional[str]:
  return None if value is None else str(value)


def native_next_action(
    session_events: Sequence[Mapping[str, Any]],
    *,
    gold_events: Sequence[Mapping[str, Any]] = (),
) -> str:
  """Deterministic punchline next-action for one session (no judge).

  Derived from the events alone: whether the session completed, and which
  tools a completed gold sibling called that this session never did. For
  the widget-stock silence session with sibling ``ab7535a5`` this yields
  "the agent never answered ... never called check_inventory".
  """
  completed = any(
      event.get("event_type") == _COMPLETION_EVENT for event in session_events
  )
  if completed:
    return (
        "The session completed; only the score policy decides whether it"
        " passed."
    )
  called = _tool_names(session_events)
  missing = sorted(_tool_names(gold_events) - called)
  if missing:
    tools = ", ".join(missing)
    return (
        "The agent never answered (no AGENT_COMPLETED) and never called"
        f" {tools} (the completed sibling did); find why the agent stalled"
        " after its last event."
    )
  if not called:
    return (
        "The agent never answered (no AGENT_COMPLETED) and never called any"
        " tool; find why the agent stalled after its last event."
    )
  return (
      "The agent never answered (no AGENT_COMPLETED); find why the agent"
      " stalled after its last event."
  )


def _tool_names(events: Sequence[Mapping[str, Any]]) -> set[str]:
  names: set[str] = set()
  for event in events:
    if event.get("event_type") != _TOOL_START_EVENT:
      continue
    content = _structured(event.get("content"))
    if isinstance(content, Mapping):
      name = _usable_text(content.get("tool"))
      if name is not None:
        names.add(name)
  return names
