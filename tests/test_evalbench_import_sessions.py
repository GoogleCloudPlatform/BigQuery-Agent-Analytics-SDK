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
"""Tests for ``evalbench.import_sessions`` (#435 slice 3, #97)."""

from __future__ import annotations

from datetime import datetime
from datetime import timezone
from typing import Any

import pytest

from bigquery_agent_analytics import evalbench
from bigquery_agent_analytics.trace import TraceFilter

_MANIFEST_REF = "analytics-project.bqaa.evalbench_import_manifest"
_EVENTS_REF = "analytics-project.bqaa.evalbench_agent_events"
_SCORES_REF = "analytics-project.bqaa.evalbench_scores_imported"


def _manifest(import_version: str, imported_at: datetime) -> dict[str, Any]:
  return {
      "job_id": "job-123",
      "import_version": import_version,
      "events_table": _EVENTS_REF,
      "scores_table": _SCORES_REF,
      "imported_at": imported_at,
      "generation_id": f"gen-{import_version}",
  }


class _FakeResult:

  def __init__(self, rows: list[dict[str, Any]]) -> None:
    self._rows = rows

  def result(self) -> list[dict[str, Any]]:
    return list(self._rows)


class _FakeClient:
  """Answers the manifest and session queries; records every call."""

  def __init__(
      self,
      manifests: list[dict[str, Any]],
      sessions: dict[str, list[str]],
  ) -> None:
    self._manifests = manifests
    self._sessions = sessions
    self.calls: list[dict[str, Any]] = []

  def query(self, sql: str, **kwargs: Any) -> _FakeResult:
    job_config = kwargs["job_config"]
    params = {p.name: p.value for p in job_config.query_parameters}
    self.calls.append(
        {"sql": sql, "params": params, "labels": dict(job_config.labels or {})}
    )
    if sql.startswith("SELECT DISTINCT session_id"):
      assert f"`{_EVENTS_REF}`" in sql
      rows = self._sessions.get(params["import_version"], [])
      return _FakeResult([{"session_id": s} for s in rows])
    assert f"`{_MANIFEST_REF}`" in sql
    rows = [m for m in self._manifests if m["job_id"] == params["job_id"]]
    if "import_version" in params:
      rows = [
          m for m in rows if m["import_version"] == params["import_version"]
      ]
    else:
      rows.sort(
          key=lambda m: (m["imported_at"], m["import_version"]), reverse=True
      )
      rows = rows[:1]
    return _FakeResult(rows)


def _client(
    sessions: dict[str, list[str]] | None = None,
) -> _FakeClient:
  return _FakeClient(
      manifests=[
          _manifest("v1", datetime(2026, 4, 1, tzinfo=timezone.utc)),
          _manifest("v2", datetime(2026, 4, 2, tzinfo=timezone.utc)),
      ],
      sessions=sessions
      if sessions is not None
      else {
          "v1": ["evalbench-import:job-123:v1:a"],
          "v2": [
              "evalbench-import:job-123:v2:a",
              "evalbench-import:job-123:v2:b",
          ],
      },
  )


def test_defaults_to_latest_successful_import() -> None:
  client = _client()

  pinned = evalbench.import_sessions(
      target_project="analytics-project",
      target_dataset="bqaa",
      job_id="job-123",
      bq_client=client,
  )

  assert pinned.job_id == "job-123"
  assert pinned.import_version == "v2"
  assert pinned.events_table == _EVENTS_REF
  assert pinned.scores_table == _SCORES_REF
  assert pinned.session_ids == (
      "evalbench-import:job-123:v2:a",
      "evalbench-import:job-123:v2:b",
  )
  assert pinned.session_count == 2
  assert pinned.manifest["generation_id"] == "gen-v2"
  # Manifest first, then the version's sessions from the bound table.
  assert [c["params"] for c in client.calls] == [
      {"job_id": "job-123"},
      {"job_id": "job-123", "import_version": "v2"},
  ]
  assert all(
      c["labels"].get("sdk_feature") == "evalbench-score" for c in client.calls
  ), client.calls


def test_pins_explicit_version() -> None:
  client = _client()

  pinned = evalbench.import_sessions(
      target_project="analytics-project",
      target_dataset="bqaa",
      job_id="job-123",
      import_version="v1",
      bq_client=client,
  )

  assert pinned.import_version == "v1"
  assert pinned.session_ids == ("evalbench-import:job-123:v1:a",)
  assert [c["params"] for c in client.calls] == [
      {"job_id": "job-123", "import_version": "v1"},
      {"job_id": "job-123", "import_version": "v1"},
  ]


def test_passes_location_to_every_query() -> None:
  client = _client()
  seen: list[Any] = []
  original = client.query

  def query(sql: str, **kwargs: Any) -> _FakeResult:
    seen.append(kwargs.get("location"))
    return original(sql, **kwargs)

  client.query = query  # type: ignore[method-assign]

  evalbench.import_sessions(
      target_project="analytics-project",
      target_dataset="bqaa",
      job_id="job-123",
      location="US",
      bq_client=client,
  )

  assert seen == ["US", "US"]


def test_unpublished_job_raises_before_reading_events() -> None:
  client = _client()

  with pytest.raises(ValueError, match="no published import"):
    evalbench.import_sessions(
        target_project="analytics-project",
        target_dataset="bqaa",
        job_id="job-999",
        bq_client=client,
    )
  assert len(client.calls) == 1


def test_unpublished_version_raises_before_reading_events() -> None:
  client = _client()

  with pytest.raises(ValueError, match="is not published"):
    evalbench.import_sessions(
        target_project="analytics-project",
        target_dataset="bqaa",
        job_id="job-123",
        import_version="v9",
        bq_client=client,
    )
  assert len(client.calls) == 1


def test_malformed_version_and_job_are_rejected() -> None:
  client = _client()

  with pytest.raises(ValueError):
    evalbench.import_sessions(
        target_project="analytics-project",
        target_dataset="bqaa",
        job_id="job-123",
        import_version="bad version!",
        bq_client=client,
    )
  with pytest.raises(ValueError, match="job_id"):
    evalbench.import_sessions(
        target_project="analytics-project",
        target_dataset="bqaa",
        job_id="",
        bq_client=client,
    )
  assert client.calls == []


def test_trace_filter_pins_job_and_exact_sessions() -> None:
  pinned = evalbench.import_sessions(
      target_project="analytics-project",
      target_dataset="bqaa",
      job_id="job-123",
      bq_client=_client(),
  )

  filters = pinned.trace_filter()

  assert isinstance(filters, TraceFilter)
  assert filters.experiment_id == "job-123"
  assert list(filters.session_ids) == list(pinned.session_ids)
  assert filters.limit == 2
  where, params = filters.to_sql_conditions()
  assert "experiment_id" in where
  assert "session_id" in where
  values = {p.name: getattr(p, "values", None) or p.value for p in params}
  assert values["experiment_id"] == "job-123"


def test_trace_filter_refuses_empty_session_set() -> None:
  pinned = evalbench.import_sessions(
      target_project="analytics-project",
      target_dataset="bqaa",
      job_id="job-123",
      bq_client=_client(sessions={}),
  )

  assert pinned.session_ids == ()
  with pytest.raises(ValueError, match="no sessions"):
    pinned.trace_filter()


def test_to_dict_is_json_safe() -> None:
  pinned = evalbench.import_sessions(
      target_project="analytics-project",
      target_dataset="bqaa",
      job_id="job-123",
      bq_client=_client(),
  )

  payload = pinned.to_dict()

  assert payload["import_version"] == "v2"
  assert payload["session_ids"] == list(pinned.session_ids)
  assert isinstance(payload["manifest"]["imported_at"], str)
