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

"""Runtime wrapper tests for the Cloud Run Job's spec input modes (issue #286).

Loads ``examples/migration_v5/periodic_materialization/run_job.py`` and drives
``main()`` with BigQuery, dataset bootstrap, and the orchestrator all faked, so
we can assert how each mode calls ``run_materialize_window`` without touching
BigQuery.
"""

from __future__ import annotations

import importlib.util
import pathlib

import pytest

_RUN_JOB = (
    pathlib.Path(__file__).resolve().parents[1]
    / "examples"
    / "migration_v5"
    / "periodic_materialization"
    / "run_job.py"
)


class _FakeResult:
  ok = True

  def to_json(self):
    return {}


def _drive(monkeypatch, env):
  """Run ``run_job.main()`` with all I/O faked; return (rc, mw_kwargs, retargets)."""
  spec = importlib.util.spec_from_file_location("_run_job_under_test", _RUN_JOB)
  run_job = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(run_job)

  import google.cloud.bigquery as bq

  import bigquery_agent_analytics.materialize_window as mw

  captured: dict = {}

  def _fake_run(**kwargs):
    captured.update(kwargs)
    return _FakeResult()

  monkeypatch.setattr(mw, "run_materialize_window", _fake_run)
  monkeypatch.setattr(bq, "Client", lambda *a, **k: object())
  monkeypatch.setattr(run_job, "_ensure_graph_dataset", lambda *a, **k: False)
  monkeypatch.setattr(run_job, "_bootstrap_entity_tables", lambda *a, **k: 0)
  retargets: list = []
  monkeypatch.setattr(
      run_job,
      "_retarget_binding",
      lambda project_id, graph_dataset_id: retargets.append(
          (project_id, graph_dataset_id)
      )
      or pathlib.Path("/tmp/binding.retargeted.yaml"),
  )
  monkeypatch.setattr(
      run_job, "_find_artifact", lambda name: pathlib.Path("/staged") / name
  )

  for key in ("BQAA_PROPERTY_GRAPH",):
    monkeypatch.delenv(key, raising=False)
  for key, value in env.items():
    monkeypatch.setenv(key, value)

  rc = run_job.main()
  return rc, captured, retargets


_BASE_ENV = {
    "BQAA_PROJECT_ID": "proj",
    "BQAA_EVENTS_DATASET_ID": "events_ds",
    "BQAA_GRAPH_DATASET_ID": "graph_ds",
    "BQAA_LOOKBACK_HOURS": "6",
}


def test_property_graph_mode(monkeypatch) -> None:
  rc, kwargs, retargets = _drive(
      monkeypatch, {**_BASE_ENV, "BQAA_PROPERTY_GRAPH": "property_graph.sql"}
  )
  assert rc == 0
  # Derives from the staged property graph; no ontology/binding, no retarget.
  assert kwargs["property_graph_path"].endswith("property_graph.sql")
  assert "ontology_path" not in kwargs
  assert "binding_path" not in kwargs
  assert retargets == []
  # Events stay read-only in events_ds; the graph targets graph_ds.
  assert kwargs["dataset_id"] == "events_ds"
  assert kwargs["graph_project_id"] == "proj"
  assert kwargs["graph_dataset_id"] == "graph_ds"
  # State table is the writable graph dataset.
  assert kwargs["state_table"] == "proj.graph_ds._bqaa_materialization_state"


def test_explicit_mode_unchanged(monkeypatch) -> None:
  rc, kwargs, retargets = _drive(
      monkeypatch, _BASE_ENV
  )  # no BQAA_PROPERTY_GRAPH
  assert rc == 0
  # Explicit ontology + binding, with the binding retargeted to the graph ds.
  assert kwargs["ontology_path"].endswith("ontology.yaml")
  assert kwargs["binding_path"].endswith("binding.retargeted.yaml")
  assert "property_graph_path" not in kwargs
  assert retargets == [("proj", "graph_ds")]
  assert kwargs["dataset_id"] == "events_ds"
