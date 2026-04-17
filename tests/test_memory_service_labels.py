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

"""Tests that memory_service.py wires every query site through SDK labels."""

import asyncio
import logging
from unittest.mock import MagicMock

from google.auth.credentials import AnonymousCredentials
from google.cloud import bigquery
import pytest

from bigquery_agent_analytics.memory_service import BigQueryEpisodicMemory
from bigquery_agent_analytics.memory_service import BigQueryMemoryService
from bigquery_agent_analytics.memory_service import BigQuerySessionMemory
from bigquery_agent_analytics.memory_service import UserProfile
from bigquery_agent_analytics.memory_service import UserProfileBuilder


def _mock_bq_client():
  """Mock client whose .query().result() returns an empty iterable."""
  client = MagicMock()
  job = MagicMock()
  job.result.return_value = []
  client.query.return_value = job
  return client


def _last_labels(mock_bq):
  cfg = mock_bq.query.call_args.kwargs.get("job_config")
  return dict(cfg.labels) if cfg and cfg.labels else {}


def _all_labels(mock_bq):
  return [
      dict(call.kwargs["job_config"].labels or {})
      for call in mock_bq.query.call_args_list
      if call.kwargs.get("job_config") is not None
  ]


def _run(coro):
  loop = asyncio.new_event_loop()
  try:
    return loop.run_until_complete(coro)
  finally:
    loop.close()


class TestSessionMemoryLabels:

  def test_get_recent_context_labels_memory(self):
    mock_bq = _mock_bq_client()
    sm = BigQuerySessionMemory(project_id="p", dataset_id="d", client=mock_bq)
    _run(sm.get_recent_context(user_id="u1", current_session_id="s1"))
    assert _last_labels(mock_bq).get("sdk_feature") == "memory"


class TestEpisodicMemoryLabels:

  def test_vector_similarity_search_labels_memory(self):
    from datetime import datetime
    from datetime import timezone
    from unittest.mock import AsyncMock
    from unittest.mock import patch

    mock_bq = _mock_bq_client()
    em = BigQueryEpisodicMemory(project_id="p", dataset_id="d", client=mock_bq)
    # Skip the off-warehouse embedding call; we only care about the BQ
    # job the SQL dispatch emits.
    with patch.object(
        em, "_generate_embedding", new=AsyncMock(return_value=[0.1, 0.2, 0.3])
    ):
      _run(
          em._vector_similarity_search(
              query="hello",
              user_id="u1",
              top_k=5,
              since_timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc),
          )
      )
    labels = _last_labels(mock_bq)
    assert labels.get("sdk_feature") == "memory"

  def test_keyword_search_labels_memory(self):
    from datetime import datetime
    from datetime import timezone

    mock_bq = _mock_bq_client()
    em = BigQueryEpisodicMemory(project_id="p", dataset_id="d", client=mock_bq)
    _run(
        em._keyword_search(
            query="weather",
            user_id="u1",
            top_k=5,
            since_timestamp=datetime(2024, 1, 1, tzinfo=timezone.utc),
        )
    )
    assert _last_labels(mock_bq).get("sdk_feature") == "memory"


class TestUserProfileBuilderLabels:

  def test_build_profile_labels_memory_on_every_query(self):
    # build_profile runs _USER_STATS_QUERY, then also invokes
    # _analyze_user_messages which runs _USER_MESSAGES_QUERY. Both
    # queries must carry sdk_feature=memory.
    mock_bq = _mock_bq_client()
    builder = UserProfileBuilder(project_id="p", dataset_id="d", client=mock_bq)
    _run(builder.build_profile(user_id="u1"))
    all_labels = _all_labels(mock_bq)
    assert len(all_labels) >= 2, "expected stats + messages queries"
    for labels in all_labels:
      assert labels.get("sdk_feature") == "memory"


class TestVanillaClientWarnOnce:
  """Mirrors the Phase 2a warn-once pattern for each class's client property."""

  @pytest.mark.parametrize(
      "factory",
      [
          lambda vanilla: BigQuerySessionMemory(
              project_id="p", dataset_id="d", client=vanilla
          ),
          lambda vanilla: BigQueryEpisodicMemory(
              project_id="p", dataset_id="d", client=vanilla
          ),
          lambda vanilla: UserProfileBuilder(
              project_id="p", dataset_id="d", client=vanilla
          ),
          lambda vanilla: BigQueryMemoryService(
              project_id="p", dataset_id="d", client=vanilla
          ),
      ],
      ids=[
          "BigQuerySessionMemory",
          "BigQueryEpisodicMemory",
          "UserProfileBuilder",
          "BigQueryMemoryService",
      ],
  )
  def test_vanilla_client_emits_one_warning(self, caplog, factory):
    vanilla = bigquery.Client(project="p", credentials=AnonymousCredentials())
    obj = factory(vanilla)

    with caplog.at_level(logging.WARNING):
      _ = obj.client
      _ = obj.client
      _ = obj.client

    warnings = [
        r
        for r in caplog.records
        if "SDK telemetry labels will not be applied" in r.message
    ]
    assert len(warnings) == 1
