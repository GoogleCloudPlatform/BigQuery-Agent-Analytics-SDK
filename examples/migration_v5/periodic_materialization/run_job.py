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

"""Cloud Run Job entrypoint: periodic graph materialization.

Reads agent events from ``BQAA_EVENTS_DATASET_ID``, materializes
the MAKO graph into ``BQAA_GRAPH_DATASET_ID``, scoped to the
last ``BQAA_LOOKBACK_HOURS`` of events. Designed for a Cloud
Scheduler trigger that fires every N hours; the orchestrator's
state-table checkpoint plus overlap window handles "exactly once"
across consecutive runs.

This wrapper does three things the bare ``bqaa-materialize-window``
CLI doesn't, all needed for a hands-off scheduled deployment:

1. **Retargets the demo binding** to the customer's
   ``BQAA_PROJECT_ID`` / ``BQAA_GRAPH_DATASET_ID`` at run time.
   The committed ``binding.yaml`` hard-codes the canonical demo
   target; rewriting it in-process means the customer only sets
   env vars, never edits YAML.

2. **Bootstraps entity-table DDL** if absent. ``materialize-
   window`` requires the bound entity tables to exist before
   it can insert. ``CREATE TABLE IF NOT EXISTS`` is a no-op on
   subsequent runs.

3. **Emits the JSON report on stdout** so Cloud Logging picks
   it up as a structured log entry (Cloud Run forwards stdout
   to ``run.googleapis.com/stdout``).

Env vars (all set by the deploy script via
``--set-env-vars``):

* ``BQAA_PROJECT_ID`` (required) — GCP project.
* ``BQAA_EVENTS_DATASET_ID`` (required) — source dataset with
  ``agent_events``.
* ``BQAA_GRAPH_DATASET_ID`` (required) — target dataset for
  the materialized graph (entity + relationship tables).
* ``BQAA_LOCATION`` (default ``US``) — BigQuery location;
  must match both datasets.
* ``BQAA_LOOKBACK_HOURS`` (default ``6``) — scan window size.
* ``BQAA_OVERLAP_MINUTES`` (default ``15``) — re-scan window
  for late-arriving events. Set higher (e.g. ``60``) if your
  events table sometimes lags ingestion by tens of minutes.
* ``BQAA_MAX_SESSIONS`` (default unset/unlimited) — cost
  guardrail.

Exit codes mirror the CLI:

* ``0`` — every discovered session materialized cleanly.
* ``1`` — expected failure: at least one session failed, or
  binding-validate detected drift against live BigQuery.
* ``2`` — unexpected internal error (load failure, missing env
  var, programming bug).
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import pathlib
import sys
import tempfile
from typing import Any, Optional

_HERE = pathlib.Path(__file__).resolve().parent


def _find_artifact(name: str) -> pathlib.Path:
  """Locate a demo artifact (``ontology.yaml``, ``binding.yaml``,
  ``table_ddl.sql``).

  Two layouts are supported:

  * **Repo checkout / local dev** — artifacts live in the parent
    directory (``examples/migration_v5/<name>``); this script
    sits in ``examples/migration_v5/periodic_materialization/``.
  * **Cloud Run Job container** — the deploy script bundles the
    artifacts next to ``run_job.py`` to keep the deployed source
    tree flat. Artifacts are at ``<_HERE>/<name>``.

  The container layout takes precedence so a stale parent-dir
  copy can never shadow the bundled one.
  """
  bundled = _HERE / name
  if bundled.is_file():
    return bundled
  repo_local = _HERE.parent / name
  if repo_local.is_file():
    return repo_local
  raise FileNotFoundError(
      f"required artifact {name!r} not found in {_HERE} or {_HERE.parent}"
  )


# The committed artifacts hard-code this prefix; the wrapper
# swaps it for the customer's ``{project}.{graph_dataset}``
# pair at runtime. Single source of truth so both the binding
# rewrite and the DDL rewrite stay in sync.
_CANONICAL_PREFIX = "test-project-0728-467323.migration_v5_demo"


def _require_env(name: str) -> str:
  """Read a required env var; raise ``SystemExit(2)`` with a
  clear message if it's missing or empty. Cloud Run Jobs map
  exit 2 to "configuration error" in the retry policy."""
  value = os.environ.get(name)
  if value is None or not value.strip():
    print(
        json.dumps(
            {
                "severity": "ERROR",
                "message": f"required env var {name} is not set",
                "env_var": name,
            }
        ),
        file=sys.stderr,
    )
    sys.exit(2)
  return value.strip()


def _optional_env_float(name: str, default: float) -> float:
  raw = os.environ.get(name)
  if raw is None or not raw.strip():
    return default
  try:
    return float(raw)
  except ValueError:
    print(
        json.dumps(
            {
                "severity": "ERROR",
                "message": (f"env var {name}={raw!r} is not a valid float"),
                "env_var": name,
            }
        ),
        file=sys.stderr,
    )
    sys.exit(2)


def _optional_env_int(name: str) -> Optional[int]:
  """Returns ``None`` if unset (meaning "unlimited" for
  ``--max-sessions``); raises exit 2 on a non-int value."""
  raw = os.environ.get(name)
  if raw is None or not raw.strip():
    return None
  try:
    return int(raw)
  except ValueError:
    print(
        json.dumps(
            {
                "severity": "ERROR",
                "message": (f"env var {name}={raw!r} is not a valid integer"),
                "env_var": name,
            }
        ),
        file=sys.stderr,
    )
    sys.exit(2)


def _retarget_binding(project_id: str, graph_dataset_id: str) -> pathlib.Path:
  """Render the committed ``binding.yaml`` against the
  customer's ``{project}.{graph_dataset}`` and write to a tmp
  file. Returns the path.

  Why this approach over shipping a Jinja-style template: the
  committed binding is human-readable, parseable by ``binding-
  validate``, and exercised end-to-end by the live test fixture.
  Adding template syntax would diverge those two consumers.
  """
  import yaml

  with _find_artifact("binding.yaml").open() as f:
    binding = yaml.safe_load(f)

  binding["target"]["project"] = project_id
  binding["target"]["dataset"] = graph_dataset_id
  for entity in binding.get("entities", []) or []:
    parts = entity.get("source", "").split(".")
    if len(parts) == 3:
      entity["source"] = f"{project_id}.{graph_dataset_id}.{parts[2]}"
  for rel in binding.get("relationships", []) or []:
    parts = rel.get("source", "").split(".")
    if len(parts) == 3:
      rel["source"] = f"{project_id}.{graph_dataset_id}.{parts[2]}"

  tmp = pathlib.Path(tempfile.mkdtemp()) / "binding.retargeted.yaml"
  tmp.write_text(yaml.safe_dump(binding))
  return tmp


def _bootstrap_entity_tables(
    bq_client: Any, project_id: str, graph_dataset_id: str
) -> int:
  """Run the committed entity-table DDL against the customer's
  graph dataset. ``CREATE TABLE IF NOT EXISTS`` makes this
  idempotent — first run creates, subsequent runs no-op. Returns
  the count of DDL statements executed.

  This is the load-bearing detail for "the cron job just works"
  — without these tables, the materializer's per-table INSERT
  silently fails on every run. Bootstrapping at startup means
  the customer doesn't need a separate one-time setup step.
  """
  ddl_text = (
      _find_artifact("table_ddl.sql")
      .read_text()
      .replace(
          _CANONICAL_PREFIX,
          f"{project_id}.{graph_dataset_id}",
      )
  )
  count = 0
  for stmt in ddl_text.strip().split(";"):
    stmt = stmt.strip()
    if stmt:
      bq_client.query(stmt).result()
      count += 1
  return count


def _ensure_graph_dataset(
    bq_client: Any, project_id: str, graph_dataset_id: str, location: str
) -> bool:
  """Idempotently ensure the graph dataset exists, probe-first.

  Returns ``True`` if the dataset was created on this call,
  ``False`` if it already existed.

  Implementation: ``get_dataset`` first (read-only — the
  runtime SA has ``roles/bigquery.dataEditor`` on the graph
  dataset, which includes read). On ``NotFound``, fall through
  to ``create_dataset``. NEVER call ``create_dataset`` against
  an existing dataset — BigQuery checks IAM before existence,
  so a ``create`` against an existing dataset would raise
  ``PermissionDenied`` for an SA without project-level
  ``datasets.create``, before the would-be ``Conflict`` is
  produced. The probe-first shape sidesteps that.

  In production (Cloud Run Job),
  ``deploy_cloud_run_job.sh`` pre-creates the graph dataset
  before the job ever runs. So the production path is:
  ``get_dataset`` succeeds → return ``False`` → no
  ``create_dataset`` call. The runtime SA never needs
  ``datasets.create``.

  For local dry-run mode the operator uses their own gcloud
  credentials (typically with project-level
  ``datasets.create``). The first invocation against a fresh
  dataset name hits ``NotFound`` and actually creates it; the
  laptop workflow stays zero-setup.

  If the runtime identity lacks ``datasets.create`` AND the
  dataset is missing (someone skipped the deploy script's
  pre-create), the create raises ``PermissionDenied`` and the
  job exits 2 in the catch-all handler in ``main()`` — the
  right shape for a misconfigured deploy.
  """
  from google.api_core import exceptions as gapi_exceptions
  from google.cloud import bigquery

  ds_ref = f"{project_id}.{graph_dataset_id}"
  # Probe with ``get_dataset`` first, NOT ``create_dataset``. The
  # runtime SA in production has only dataset-level
  # ``dataEditor`` on this dataset — no project-level
  # ``datasets.create``. ``create_dataset`` would fail with
  # ``PermissionDenied`` before BigQuery even checks existence,
  # so the ``Conflict``-catch idiom doesn't work for that SA.
  # ``get_dataset`` only needs read access, which the SA has.
  try:
    bq_client.get_dataset(ds_ref)
    return False
  except gapi_exceptions.NotFound:
    pass

  # Dataset doesn't exist. The create path is reached only on
  # local dry-run (operator's gcloud creds, broad perms) or on
  # the very first deploy where someone skipped the deploy
  # script's pre-create step. If the runtime SA lacks
  # ``datasets.create``, the call raises ``PermissionDenied``
  # and the job exits 2 in the catch-all handler — the right
  # shape for a misconfigured deploy.
  ds = bigquery.Dataset(ds_ref)
  ds.location = location
  bq_client.create_dataset(ds, exists_ok=False)
  return True


def _emit(severity: str, **fields: Any) -> None:
  """Structured stdout for Cloud Logging.

  Cloud Logging picks up the ``severity`` key from the JSON
  payload and uses it for the log entry's severity. Other keys
  land in ``jsonPayload``. Valid severities: ``DEFAULT``,
  ``DEBUG``, ``INFO``, ``NOTICE``, ``WARNING``, ``ERROR``,
  ``CRITICAL``, ``ALERT``, ``EMERGENCY``. See
  https://cloud.google.com/logging/docs/structured-logging.
  """
  payload = {"severity": severity, **fields}
  print(json.dumps(payload, default=str))
  sys.stdout.flush()


def main() -> int:
  project_id = _require_env("BQAA_PROJECT_ID")
  events_dataset_id = _require_env("BQAA_EVENTS_DATASET_ID")
  graph_dataset_id = _require_env("BQAA_GRAPH_DATASET_ID")
  location = os.environ.get("BQAA_LOCATION", "US")
  lookback_hours = _optional_env_float("BQAA_LOOKBACK_HOURS", 6.0)
  overlap_minutes = _optional_env_float("BQAA_OVERLAP_MINUTES", 15.0)
  max_sessions = _optional_env_int("BQAA_MAX_SESSIONS")

  _emit(
      "INFO",
      message="periodic materialization starting",
      project_id=project_id,
      events_dataset_id=events_dataset_id,
      graph_dataset_id=graph_dataset_id,
      location=location,
      lookback_hours=lookback_hours,
      overlap_minutes=overlap_minutes,
      max_sessions=max_sessions,
  )

  try:
    from google.cloud import bigquery

    from bigquery_agent_analytics import materialize_window as mw

    bq_client = bigquery.Client(project=project_id, location=location)

    # 1. Ensure the graph dataset exists. Idempotent — created
    # on first run, no-op afterwards. Done here (not in the
    # deploy script) so the local dry-run path also benefits.
    created = _ensure_graph_dataset(
        bq_client, project_id, graph_dataset_id, location
    )
    _emit(
        "INFO",
        message=(
            "graph dataset created" if created else "graph dataset exists"
        ),
        graph_dataset_id=graph_dataset_id,
        created=created,
    )

    # 2. Bootstrap entity tables. Idempotent — no-op after first run.
    ddl_count = _bootstrap_entity_tables(
        bq_client, project_id, graph_dataset_id
    )
    _emit(
        "INFO",
        message="entity-table DDL applied",
        statements=ddl_count,
    )

    # 3. Retarget binding to the customer's graph dataset.
    binding_path = _retarget_binding(project_id, graph_dataset_id)

    # 4. Run the orchestrator. Reads events from
    # ``BQAA_EVENTS_DATASET_ID``; writes entities/relationships
    # AND the state/checkpoint table into
    # ``BQAA_GRAPH_DATASET_ID`` per the retargeted binding.
    # The state-table arg is fully-qualified
    # (``project.dataset.table``) so it ignores the
    # orchestrator's default-dataset fallback (which would point
    # at the read-only events dataset). This keeps the source
    # dataset truly read-only per the README contract.
    state_table_ref = (
        f"{project_id}.{graph_dataset_id}._bqaa_materialization_state"
    )
    result = mw.run_materialize_window(
        project_id=project_id,
        dataset_id=events_dataset_id,
        ontology_path=str(_find_artifact("ontology.yaml")),
        binding_path=str(binding_path),
        lookback_hours=lookback_hours,
        overlap_minutes=overlap_minutes,
        max_sessions=max_sessions,
        state_table=state_table_ref,
        location=location,
        validate_binding=True,
        bq_client=bq_client,
        run_started_at=_dt.datetime.now(_dt.timezone.utc),
    )

    # Structured JSON report for Cloud Logging. Cloud Logging
    # filters / metrics can pivot on the keys here directly
    # (e.g., ``jsonPayload.sessions_failed > 0`` for alerts).
    severity = "INFO" if result.ok else "ERROR"
    _emit(severity, message="materialization complete", **result.to_json())

    return 0 if result.ok else 1

  except Exception as exc:  # noqa: BLE001 — entrypoint is the boundary
    # Unexpected internal error (not a session failure or
    # binding drift — those come back as ``result.ok=False`` and
    # map to exit 1). Cloud Run Jobs treat exit 2 as
    # configuration/code error in the retry policy.
    import traceback

    _emit(
        "ERROR",
        message="unexpected error in periodic materialization",
        error_type=type(exc).__name__,
        error=str(exc),
        traceback=traceback.format_exc(limit=10),
    )
    return 2


if __name__ == "__main__":
  sys.exit(main())
