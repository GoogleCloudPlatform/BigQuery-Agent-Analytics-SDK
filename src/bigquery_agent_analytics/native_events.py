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
  ``task/planning``, ``finalization``, ``tool blockers``.
* **Clock.** Nothing here starts the six-week clock, seals the
  preregistration, or kicks a Week 1 snapshot job.

Everything except the optional BigQuery read is pure and offline
(fixture-testable): ``from_agent_events`` takes in-memory rows.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import dataclasses
from datetime import datetime
from typing import Any, Optional

from google.cloud import bigquery

from ._telemetry import make_bq_client
from ._telemetry import with_sdk_labels
from .evalbench import _as_mapping
from .evalbench import _fingerprint_rows
from .evalbench import _json_safe
from .evalbench import _parse_timestamp
from .evalbench import _plain_row
from .evalbench import _structured
from .evalbench import _usable_text
from .evalbench import _validate_import_version
from .evalbench import _validate_source_segment
from .evalbench import EvalBenchRun

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

_READ_EVENTS_QUERY = """\
SELECT *
FROM `{source_table}`{snapshot_clause}{where_clause}
ORDER BY session_id, timestamp
"""


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
  return project, dataset, table


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
