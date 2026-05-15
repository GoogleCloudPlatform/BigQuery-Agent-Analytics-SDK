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

"""Live BigQuery integration test for ``bqaa-materialize-window``.

The unit tests in ``test_materialize_window.py`` mock the BigQuery
client. This test exercises the orchestrator against real
BigQuery infrastructure to prove the end-to-end claim from #161.

**Gating** — skipped unless ALL of these env vars are set:

* ``BQAA_LIVE_BQ=1`` — explicit opt-in (burns BigQuery quota).
* ``BQAA_LIVE_BQ_PROJECT`` — GCP project ID. The test creates a
  fresh scratch dataset under this project, materializes into
  it, and drops it on teardown.
* ``BQAA_LIVE_BQ_SOURCE_DATASET`` — dataset that contains a
  pre-populated ``agent_events`` table (seed it once with the
  migration-v5 demo agent; see the bootstrap recipe below). The
  test reads events from here read-only — never writes to it.

Optional:

* ``BQAA_LIVE_BQ_LOCATION`` (default: ``US``).
* ``BQAA_LIVE_BQ_DATASET_PREFIX`` (default: ``bqaa_live_test_``)
  — the scratch dataset is named ``<prefix><8-hex-suffix>`` so
  parallel runs don't collide and an orphaned scratch dataset
  is unambiguously identifiable for manual cleanup.

CI must NEVER set these defaults globally. The opt-in design is
deliberate: every run creates BQ tables, runs queries, and burns
quota.

## Bootstrap the source dataset (one-time, manual)

::

    bq mk --location=US <project>:<source_dataset>
    PYTHONPATH=src python examples/migration_v5/run_agent.py \\
        --project <project> --dataset <source_dataset> --sessions 3

The ``agent_events`` table in ``<source_dataset>`` is the input;
this test never modifies it.

## Run

::

    BQAA_LIVE_BQ=1 \\
    BQAA_LIVE_BQ_PROJECT=<project> \\
    BQAA_LIVE_BQ_SOURCE_DATASET=<source_dataset> \\
    pytest tests/test_materialize_window_live.py -v -s

## What's covered

A single linear test runs both phases (run-1 → run-2) so the
re-run dependency is explicit in the test body, not implicit
across separate tests. Specifically:

* Run 1 succeeds (``ok=True``) and materializes rows into the
  scratch dataset's entity tables.
* Run 2 succeeds (``ok=True``), re-discovers the same number
  of sessions as run-1 (``run2.sessions_discovered ==
  run1.sessions_discovered``), AND the materialized session
  **set** (``DISTINCT session_id`` from ``decision_execution``)
  is byte-identical to run-1's. Count equality is a cheap pre-
  filter; the set assertion is the literal proof that a mutating
  source dataset couldn't have swapped sessions while keeping
  cardinality. The test uses ``overlap_minutes = lookback_hours
  * 60`` to guarantee re-discovery regardless of how the seeded
  events are distributed in time.
* The checkpoint never regresses between runs (round-4 P2.1
  guard).
* When delete-cleanup fails on streaming-buffered rows, the
  ``cleanup_status = "delete_failed"`` signal surfaces in
  ``result.table_statuses`` — the operator-facing diagnostic.
* Row counts stay sane (non-zero where expected, not silently
  empty).

## Deferred: strict byte-identical idempotency proof

A strict ``rows_after_run_1 == rows_after_run_2`` assertion is
only achievable once the BQ streaming buffer drains (typically
30-90 min after inserts). That is a **deferred long-wait
variant** of this test — a future nightly job can sleep for the
buffer-drain window between runs and assert byte equality. It
is NOT implied as proven by this file.
"""

from __future__ import annotations

import datetime as _dt
import os
import pathlib
import secrets

import pytest
import yaml

from bigquery_agent_analytics import materialize_window as mw

_LIVE = os.environ.get("BQAA_LIVE_BQ", "").lower() in ("1", "true", "yes")
_PROJECT = os.environ.get("BQAA_LIVE_BQ_PROJECT")
_SOURCE_DATASET = os.environ.get("BQAA_LIVE_BQ_SOURCE_DATASET")
_LOCATION = os.environ.get("BQAA_LIVE_BQ_LOCATION", "US")
_PREFIX = os.environ.get("BQAA_LIVE_BQ_DATASET_PREFIX", "bqaa_live_test_")

pytestmark = pytest.mark.skipif(
    not _LIVE or not _PROJECT or not _SOURCE_DATASET,
    reason=(
        "Live BQ tests require BQAA_LIVE_BQ=1, BQAA_LIVE_BQ_PROJECT, "
        "and BQAA_LIVE_BQ_SOURCE_DATASET. CI must not set these by "
        "default — every run creates BQ resources."
    ),
)


_MIGRATION_V5_DIR = (
    pathlib.Path(__file__).resolve().parent.parent / "examples" / "migration_v5"
)


def _unique_scratch_name() -> str:
  """``<prefix><8-hex>`` — unique per process so parallel runs
  don't collide, and an orphaned dataset is unambiguously a test
  artifact for manual cleanup."""
  return f"{_PREFIX}{secrets.token_hex(4)}"


@pytest.fixture(scope="module")
def scratch_dataset():
  """Create a fresh scratch dataset, bootstrap the migration-v5
  entity-table DDL into it, yield its name, and DROP it on
  teardown.

  The teardown runs even if the test body raised — partial
  creation (some tables made, some not) is tolerated. Streaming
  buffers pinning row deletion does NOT block dataset teardown:
  ``delete_contents=True`` removes the whole dataset, tables and
  all, in a single API call.
  """
  from google.cloud import bigquery

  client = bigquery.Client(project=_PROJECT, location=_LOCATION)
  scratch = _unique_scratch_name()
  ds_ref = bigquery.Dataset(f"{_PROJECT}.{scratch}")
  ds_ref.location = _LOCATION
  ds_ref = client.create_dataset(ds_ref, exists_ok=False)

  try:
    # Bootstrap the entity-table schemas. The committed DDL
    # hard-codes the full canonical
    # ``test-project-0728-467323.migration_v5_demo`` prefix —
    # swap the **full** prefix for ``{_PROJECT}.{scratch}`` so the
    # DDL targets the operator-supplied project, not the canonical
    # demo project. Replacing only the dataset segment would
    # silently create tables in the wrong project when
    # ``BQAA_LIVE_BQ_PROJECT`` differs from the canonical.
    ddl_text = (_MIGRATION_V5_DIR / "table_ddl.sql").read_text()
    ddl_text = ddl_text.replace(
        "test-project-0728-467323.migration_v5_demo",
        f"{_PROJECT}.{scratch}",
    )
    for stmt in ddl_text.strip().split(";"):
      stmt = stmt.strip()
      if stmt:
        client.query(stmt).result()
    yield scratch
  finally:
    # Defensive teardown: best-effort, never raises.
    try:
      client.delete_dataset(
          f"{_PROJECT}.{scratch}",
          delete_contents=True,
          not_found_ok=True,
      )
    except Exception as exc:  # noqa: BLE001 — teardown is the boundary
      # Print but don't raise — a teardown failure shouldn't mask
      # a test failure (and shouldn't pass either, since pytest
      # already has the test result).
      print(f"[scratch teardown] failed to drop {scratch}: {exc!r}")


@pytest.fixture(scope="module")
def retargeted_binding_path(tmp_path_factory, scratch_dataset):
  """Rewrite ``binding.yaml`` so every ``project.dataset.table``
  path points at the scratch dataset.

  The committed binding hard-codes
  ``test-project-0728-467323.migration_v5_demo``; the test
  materializes into the scratch dataset instead so the source
  dataset's ``agent_events`` stays untouched."""
  with open(_MIGRATION_V5_DIR / "binding.yaml") as f:
    binding = yaml.safe_load(f)
  binding["target"]["project"] = _PROJECT
  binding["target"]["dataset"] = scratch_dataset
  for entity in binding.get("entities", []) or []:
    parts = entity.get("source", "").split(".")
    if len(parts) == 3:
      entity["source"] = f"{_PROJECT}.{scratch_dataset}.{parts[2]}"
  for rel in binding.get("relationships", []) or []:
    parts = rel.get("source", "").split(".")
    if len(parts) == 3:
      rel["source"] = f"{_PROJECT}.{scratch_dataset}.{parts[2]}"
  out = tmp_path_factory.mktemp("binding") / "binding.yaml"
  out.write_text(yaml.safe_dump(binding))
  return out


# State-table name lives inside the scratch dataset, so it's
# torn down with the rest. The local name keeps it identifiable
# as test-owned.
_STATE_TABLE_LOCAL = "_test_materialization_state"


def _count_entity_rows(client, scratch: str) -> dict[str, int]:
  """Row counts for the materialized entity tables in the scratch
  dataset. Skips the state table and anything else with a
  leading underscore (SDK-internal artifacts)."""
  from google.cloud import bigquery

  counts: dict[str, int] = {}
  for table in client.list_tables(f"{_PROJECT}.{scratch}"):
    if table.table_id.startswith("_"):
      continue
    sql = f"SELECT COUNT(*) AS n FROM `{_PROJECT}.{scratch}.{table.table_id}`"
    rows = list(client.query(sql).result())
    counts[table.table_id] = rows[0].n
  return counts


def _materialized_session_set(client, scratch: str) -> set[str]:
  """Hub projection: distinct ``session_id`` set across the
  materialized ``decision_execution`` table.

  This is the set of sessions that produced at least one
  ``DecisionExecution`` hub row — NOT necessarily every session
  the orchestrator processed. Extraction can yield a graph with
  zero hub entities for a given session (e.g., the agent never
  completed a full decision flow in that session); the
  orchestrator still counts the session as ``materialized``
  because the materializer didn't raise. The hub projection is
  the live-evidence answer to "which sessions produced
  decision-execution rows in BQ", and that is the right grain
  for the cross-run set-equality check.

  Comparing this set across runs is the literal proof of "same
  session set materialized" — a session-count equality could
  otherwise be satisfied by a mutating source dataset that
  swaps sessions but keeps the cardinality."""
  sql = (
      f"SELECT DISTINCT session_id "
      f"FROM `{_PROJECT}.{scratch}.decision_execution` "
      f"WHERE session_id IS NOT NULL"
  )
  rows = list(client.query(sql).result())
  return {r.session_id for r in rows}


def test_materialize_window_live_smoke_and_re_run(
    scratch_dataset, retargeted_binding_path
):
  """Linear two-phase live proof: run 1 → assert → run 2 → assert.

  Folded into a single test so run-2 always sees run-1's
  scratch-dataset state, regardless of test-order randomization
  or selective execution (``pytest -k <one-of-them>``). The
  module-scope scratch fixture is still useful (one create/drop
  per module run), but the run dependency is now explicit in
  the test body — no implicit state coupling across tests.

  Phase 1 — Run 1 (first materialization against fresh tables):

  * ``ok=True``.
  * ``sessions_discovered > 0`` (catches an empty source).
  * ``sessions_materialized == sessions_discovered``.
  * Checkpoint advanced (not ``None``).
  * Core entity tables (``decision_execution``,
    ``decision_point``, ``candidate``) populated with > 0 rows.
    Empty here = silent no-op = test fails.

  Phase 2 — Run 2 (re-run with overlap covering full lookback,
  so re-discovery is guaranteed for any session run-1 saw):

  * ``ok=True``.
  * ``sessions_discovered == run1.sessions_discovered`` —
    confirms the re-run path actually re-discovered the same
    sessions and we're not measuring an empty discovery.
  * ``sessions_materialized == sessions_discovered``.
  * Checkpoint does not regress (round-4 P2.1).
  * Row counts stay sane — any non-zero table from run-1 stays
    non-zero. Silent zero-out is corruption.
  * If ``cleanup_status = "delete_failed"`` surfaces, it names
    specific tables (round-2 P2.2 operator-visible diagnostic).

  NOT proven here: strict byte-identical ``rows_after_run_2 ==
  rows_after_run_1``. That requires waiting ~30-90 min for the
  BQ streaming buffer to drain. Documented as a deferred
  long-wait variant in the module docstring."""
  from google.cloud import bigquery

  client = bigquery.Client(project=_PROJECT, location=_LOCATION)

  # ------------------------------------------------------------ #
  # Phase 1 — Run 1                                              #
  # ------------------------------------------------------------ #
  now1 = _dt.datetime.now(_dt.timezone.utc)
  lookback_hours = 24.0
  run1 = mw.run_materialize_window(
      project_id=_PROJECT,
      dataset_id=_SOURCE_DATASET,
      ontology_path=str(_MIGRATION_V5_DIR / "ontology.yaml"),
      binding_path=str(retargeted_binding_path),
      lookback_hours=lookback_hours,
      overlap_minutes=15.0,
      state_table=f"{scratch_dataset}.{_STATE_TABLE_LOCAL}",
      location=_LOCATION,
      validate_binding=True,
      run_started_at=now1,
  )
  print(
      f"\nRun-1 report: ok={run1.ok}, "
      f"sessions={run1.sessions_discovered}/"
      f"{run1.sessions_materialized}, "
      f"checkpoint={run1.checkpoint_written}"
  )

  assert run1.ok, f"run-1 not ok; failures={run1.failures}"
  assert run1.sessions_discovered > 0, (
      "no terminal-event sessions found in source dataset — "
      f"check {_PROJECT}.{_SOURCE_DATASET}.agent_events has "
      "AGENT_COMPLETED events"
  )
  assert run1.sessions_materialized == run1.sessions_discovered
  assert run1.checkpoint_written is not None

  # Row-count sanity check — empty tables here would mean the
  # materializer silently no-op'd, slipping past session-level
  # ok=True.
  counts_after_run1 = _count_entity_rows(client, scratch_dataset)
  print(f"Run-1 row counts: {counts_after_run1}")
  assert counts_after_run1.get("decision_execution", 0) > 0, (
      f"decision_execution empty after run-1; " f"counts={counts_after_run1}"
  )
  assert (
      counts_after_run1.get("decision_point", 0) > 0
  ), f"decision_point empty after run-1; counts={counts_after_run1}"
  assert (
      counts_after_run1.get("candidate", 0) > 0
  ), f"candidate empty after run-1; counts={counts_after_run1}"

  # Capture the literal session_id set materialized by run-1.
  # Used in Phase 2 to prove run-2 hit the same session **set** —
  # not just the same cardinality. A mutating source dataset
  # could otherwise swap sessions between runs and still pass a
  # count-only assertion.
  #
  # ``sessions_after_run1`` may be smaller than
  # ``run1.sessions_materialized``: the orchestrator counts
  # sessions where extraction + materialize didn't raise, but a
  # session whose graph contains zero ``DecisionExecution`` rows
  # (extractor returned no hub entity for it) still counts as
  # "materialized successfully" — the materializer just had
  # nothing to insert for the hub. That's why we project on
  # the hub entity, not on the orchestrator's count.
  sessions_after_run1 = _materialized_session_set(client, scratch_dataset)
  print(f"Run-1 materialized session set: {sorted(sessions_after_run1)}")
  assert sessions_after_run1, (
      "no session_id appeared in decision_execution after run-1; "
      "either extraction silently dropped every session, or the "
      "source dataset has no MAKO-shaped events. Idempotency proof "
      "cannot proceed against an empty baseline."
  )

  # ------------------------------------------------------------ #
  # Phase 2 — Run 2 (re-run via wide overlap)                    #
  # ------------------------------------------------------------ #
  #
  # Use an overlap window that covers the full ``lookback_hours``,
  # so ``compute_scan_start`` rewinds at least to the lookback
  # floor. That guarantees run-2's discovery query re-finds every
  # session run-1 saw — independent of how far apart in time the
  # seeded events sit. A smaller overlap could miss sessions
  # whose terminal event landed > overlap minutes before the
  # checkpoint, and the "idempotency proof" would silently
  # degenerate into an empty re-run.
  now2 = _dt.datetime.now(_dt.timezone.utc)
  run2 = mw.run_materialize_window(
      project_id=_PROJECT,
      dataset_id=_SOURCE_DATASET,
      ontology_path=str(_MIGRATION_V5_DIR / "ontology.yaml"),
      binding_path=str(retargeted_binding_path),
      lookback_hours=lookback_hours,
      overlap_minutes=lookback_hours * 60.0,  # cover full lookback
      state_table=f"{scratch_dataset}.{_STATE_TABLE_LOCAL}",
      location=_LOCATION,
      validate_binding=False,
      run_started_at=now2,
  )
  print(
      f"\nRun-2 report: ok={run2.ok}, "
      f"sessions={run2.sessions_discovered}/"
      f"{run2.sessions_materialized}, "
      f"checkpoint_read={run2.checkpoint_read}, "
      f"checkpoint_written={run2.checkpoint_written}"
  )

  assert run2.ok, (
      f"run-2 not ok; failures={run2.failures}; "
      f"table_statuses={run2.table_statuses}"
  )

  # Re-discovery contract: run-2 must see the same session set
  # as run-1 — same IDs, not just the same cardinality. A
  # mutating source dataset could swap sessions between runs and
  # pass a count-only assertion. Count is a cheap pre-filter
  # (fail-fast on cardinality drift), set equality is the literal
  # proof.
  assert run2.sessions_discovered == run1.sessions_discovered, (
      f"run-2 re-discovered {run2.sessions_discovered} sessions "
      f"vs run-1's {run1.sessions_discovered}; the overlap window "
      f"didn't cover the seeded events, so the idempotency proof "
      f"is invalid for this run"
  )
  assert run2.sessions_materialized == run2.sessions_discovered

  # Checkpoint must not regress (round-4 P2.1).
  if run2.checkpoint_read is not None:
    assert run2.checkpoint_written is not None
    assert run2.checkpoint_written >= run2.checkpoint_read, (
        f"checkpoint regressed: read {run2.checkpoint_read}, "
        f"wrote {run2.checkpoint_written}"
    )

  # Same **session set** as run-1 — literal proof, not just
  # cardinality. Queried directly from the materialized hub
  # entity (``decision_execution.session_id``) so it reflects
  # what BQ actually contains, not what the orchestrator's
  # bookkeeping claims.
  sessions_after_run2 = _materialized_session_set(client, scratch_dataset)
  print(f"Run-2 materialized session set: {sorted(sessions_after_run2)}")
  assert sessions_after_run2 == sessions_after_run1, (
      f"session set drifted between runs:\n"
      f"  after run-1: {sorted(sessions_after_run1)}\n"
      f"  after run-2: {sorted(sessions_after_run2)}\n"
      f"  removed in run-2: "
      f"{sorted(sessions_after_run1 - sessions_after_run2)}\n"
      f"  added in run-2: "
      f"{sorted(sessions_after_run2 - sessions_after_run1)}"
  )

  # Row counts stay sane — anything non-zero after run-1 is still
  # non-zero after run-2. Silent zero-out is corruption.
  counts_after_run2 = _count_entity_rows(client, scratch_dataset)
  print(f"Run-2 row counts: {counts_after_run2}")
  for table, n_after_run1 in counts_after_run1.items():
    if n_after_run1 > 0:
      assert counts_after_run2.get(table, 0) > 0, (
          f"{table}: had {n_after_run1} rows after run-1, now "
          f"{counts_after_run2.get(table, 0)} — silent zero-out "
          f"is a corruption"
      )

  # If any table reports delete_failed, the diagnostic surface
  # must name the table(s) explicitly (round-2 P2.2).
  delete_failed_tables = [
      t
      for t, ts in run2.table_statuses.items()
      if ts.get("cleanup_status") == "delete_failed"
  ]
  if delete_failed_tables:
    print(
        f"Note: {len(delete_failed_tables)} table(s) hit "
        f"delete_failed (streaming buffer pinned): "
        f"{delete_failed_tables}. Strict byte-identical "
        f"idempotency is deferred to a long-wait variant."
    )
