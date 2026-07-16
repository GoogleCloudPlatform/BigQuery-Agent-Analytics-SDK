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

"""Live BigQuery behavior tests for the #359 identity contract.

Unit tests assert the SQL text and parameters the SDK generates; this
module proves the generated JSONPath actually selects the intended
member in real BigQuery, because the JSONPath grammar (backslashes
matched literally, quotes escaped with ``\\"``) cannot be verified
offline. See PR #362 round 8: JSON-string escaping of backslashes
silently matched nothing.

**Gating** — skipped unless BOTH env vars are set (burns quota):

* ``BQAA_LIVE_BQ=1`` — explicit opt-in.
* ``BQAA_LIVE_BQ_PROJECT`` — GCP project ID to bill the queries to.

The probes query literal JSON built with ``JSON_OBJECT``; no datasets
or tables are read or written.

## Run

::

    BQAA_LIVE_BQ=1 BQAA_LIVE_BQ_PROJECT=<project> \\
    pytest tests/test_trace_identity_bigquery_live.py -v -s
"""

import os

import pytest

from bigquery_agent_analytics.trace import _jsonpath_member_segment

_OPTED_IN = os.environ.get("BQAA_LIVE_BQ") == "1" and bool(
    os.environ.get("BQAA_LIVE_BQ_PROJECT")
)

pytestmark = pytest.mark.skipif(
    not _OPTED_IN,
    reason=(
        "live BigQuery tests are opt-in: set BQAA_LIVE_BQ=1 and"
        " BQAA_LIVE_BQ_PROJECT"
    ),
)

# The production shape from TraceFilter.to_sql_conditions():
# JSON_VALUE(attributes, CONCAT('$.custom_tags.', @label_key_N)).
_PROBE_QUERY = """
SELECT JSON_VALUE(
    JSON_OBJECT('custom_tags', JSON_OBJECT(@key, 'expected')),
    CONCAT('$.custom_tags.', @segment)
) AS v
"""

# Keys exercising every character class the segment helper handles.
_HOSTILE_KEYS = [
    "run",
    "a.b",
    "a[0]",
    'a"b',
    "a\\b",  # the round-8 P1: literal backslash must match
    "a\\\\b",  # interior double backslash: also literal
    "\\a",  # leading backslash
    "a\\\\",  # round-10: EVEN trailing run is valid
    'a\\\\"b',  # round-10: EVEN run before a quote is valid
    "",
    "a b",
    "a\nb",
    "a\tb",
]

# Round-9 P1: these key shapes have NO valid quoted-member encoding —
# BigQuery aborts with "Invalid token in JSONPath". The SDK rejects
# them before query submission (unit-tested); the live tests below
# prove the underlying unaddressability.
_UNADDRESSABLE_KEYS = [
    "a\\",  # odd trailing run (1)
    "a\\\\\\",  # odd trailing run (3)
    'a\\"b',  # odd run (1) before a quote
    'a\\\\\\"b',  # odd run (3) before a quote
]


def _probe(client, key: str, segment: str):
  from google.cloud import bigquery

  job_config = bigquery.QueryJobConfig(
      query_parameters=[
          bigquery.ScalarQueryParameter("key", "STRING", key),
          bigquery.ScalarQueryParameter("segment", "STRING", segment),
      ]
  )
  rows = list(client.query(_PROBE_QUERY, job_config=job_config).result())
  return rows[0].v


@pytest.fixture(scope="module")
def bq_client():
  from google.cloud import bigquery

  return bigquery.Client(project=os.environ["BQAA_LIVE_BQ_PROJECT"])


class TestJsonPathMemberSegmentLive:
  """The generated segment must select the literal member."""

  @pytest.mark.parametrize("key", _HOSTILE_KEYS, ids=repr)
  def test_segment_selects_the_member(self, bq_client, key):
    assert _probe(bq_client, key, _jsonpath_member_segment(key)) == "expected"

  @pytest.mark.parametrize("key", _UNADDRESSABLE_KEYS, ids=repr)
  def test_unaddressable_keys_rejected_before_submission(self, key):
    with pytest.raises(ValueError, match="cannot be addressed"):
      _jsonpath_member_segment(key)

  @pytest.mark.parametrize("key", _UNADDRESSABLE_KEYS, ids=repr)
  def test_unaddressable_keys_abort_live_queries(self, bq_client, key):
    # Raw probe with quote-only escaping (what the pre-round-9 helper
    # emitted): live BigQuery rejects the whole query, proving these
    # keys must be rejected client-side rather than encoded.
    from google.api_core import exceptions as gexc

    raw_segment = '"' + key.replace('"', '\\"') + '"'
    with pytest.raises(gexc.BadRequest, match="Invalid token"):
      _probe(bq_client, key, raw_segment)

  def test_doubled_backslash_regression(self, bq_client):
    # The pre-fix encoding doubled backslashes; BigQuery matches
    # backslashes literally inside quoted members, so the doubled
    # form selects nothing. Guard against reintroducing it.
    key = "a\\b"
    doubled_segment = '"a\\\\b"'
    assert _probe(bq_client, key, doubled_segment) is None
    assert _probe(bq_client, key, _jsonpath_member_segment(key)) == "expected"


# ------------------------------------------------------------------ #
# U2: dry runs and the live collision fixture (issue #359)            #
# ------------------------------------------------------------------ #


@pytest.fixture(scope="module")
def collision_dataset(bq_client):
  """Scratch dataset seeded with colliding sessions, dropped on exit.

  Sessions:
    * ``collide``: same session id for users alice and bob.
    * ``nulls``: reused id where one candidate has NULL user and
      root agent and the other has both set.
    * ``passes``: one identity carrying run=v0 rows, run=v1 rows,
      an enrichment row (v0 + subagent), and one untagged shared row.
  """
  import uuid

  from google.cloud import bigquery as bq

  project = os.environ["BQAA_LIVE_BQ_PROJECT"]
  dataset_id = f"bqaa_live_test_u2_{uuid.uuid4().hex[:8]}"
  dataset = bq.Dataset(f"{project}.{dataset_id}")
  dataset.location = os.environ.get("BQAA_LIVE_BQ_LOCATION", "US")
  bq_client.create_dataset(dataset)
  table = f"{project}.{dataset_id}.agent_events"
  bq_client.query(
      f"""
      CREATE TABLE `{table}` (
        timestamp TIMESTAMP, event_type STRING, agent STRING,
        session_id STRING, invocation_id STRING, user_id STRING,
        trace_id STRING, span_id STRING, parent_span_id STRING,
        content JSON, content_parts ARRAY<STRUCT<mime_type STRING>>,
        attributes JSON, latency_ms JSON, status STRING,
        error_message STRING, is_truncated BOOL
      )
  """
  ).result()
  bq_client.query(
      f"""
      INSERT INTO `{table}`
        (timestamp, event_type, agent, session_id, user_id, trace_id,
         span_id, content, attributes, status)
      VALUES
        -- collide: alice vs bob
        ('2026-07-01 10:00:00', 'USER_MESSAGE_RECEIVED', 'a', 'collide',
         'alice', 'tr-a', 'a1', JSON '{{}}', JSON '{{}}', 'OK'),
        ('2026-07-01 10:00:01', 'LLM_RESPONSE', 'a', 'collide',
         'alice', 'tr-a', 'a2', JSON '{{}}', JSON '{{}}', 'OK'),
        ('2026-07-01 11:00:00', 'USER_MESSAGE_RECEIVED', 'a', 'collide',
         'bob', 'tr-b', 'b1', JSON '{{}}', JSON '{{}}', 'OK'),
        -- nulls: NULL identity vs fully set identity
        ('2026-07-01 12:00:00', 'LLM_RESPONSE', 'a', 'nulls',
         NULL, 'tr-n', 'n1', JSON '{{}}', JSON '{{}}', 'OK'),
        ('2026-07-01 12:00:01', 'LLM_RESPONSE', 'a', 'nulls',
         'carol', 'tr-c', 'c1', JSON '{{}}',
         JSON '{{"root_agent_name": "rooty"}}', 'OK'),
        -- passes: v0 rows, v1 rows, enrichment, shared untagged
        ('2026-07-01 13:00:00', 'LLM_RESPONSE', 'a', 'passes',
         'eve', 'tr-p', 'p0', JSON '{{}}',
         JSON '{{"custom_tags": {{"run": "v0"}}}}', 'OK'),
        ('2026-07-01 13:00:01', 'LLM_RESPONSE', 'a', 'passes',
         'eve', 'tr-p', 'p0e', JSON '{{}}',
         JSON '{{"custom_tags": {{"run": "v0", "subagent_id": "sx"}}}}',
         'OK'),
        ('2026-07-01 13:00:02', 'LLM_RESPONSE', 'a', 'passes',
         'eve', 'tr-p', 'p1', JSON '{{}}',
         JSON '{{"custom_tags": {{"run": "v1"}}}}', 'OK'),
        ('2026-07-01 13:00:03', 'USER_MESSAGE_RECEIVED', 'a', 'passes',
         'eve', 'tr-p', 'shared', JSON '{{}}', JSON '{{}}', 'OK')
  """
  ).result()
  yield project, dataset_id
  bq_client.delete_dataset(
      f"{project}.{dataset_id}", delete_contents=True, not_found_ok=True
  )


@pytest.fixture(scope="module")
def sdk_client(collision_dataset):
  from bigquery_agent_analytics.client import Client

  project, dataset_id = collision_dataset
  return Client(project_id=project, dataset_id=dataset_id, verify_schema=False)


class TestIdentityQueriesDryRun:
  """The generated queries parse and bind with real parameters."""

  def test_list_query_dry_runs_with_scope(self, collision_dataset, bq_client):
    from google.cloud import bigquery as bq

    from bigquery_agent_analytics.client import _LIST_TRACES_QUERY
    from bigquery_agent_analytics.trace import TraceFilter

    project, dataset_id = collision_dataset
    filt = TraceFilter(custom_labels={"run": "v0"}, experiment_id="e1")
    where, params = filt.to_sql_conditions()
    query = _LIST_TRACES_QUERY.format(
        project=project,
        dataset=dataset_id,
        table="agent_events",
        where=where,
        row_where=filt.row_scope_where(),
    )
    job = bq_client.query(
        query,
        job_config=bq.QueryJobConfig(query_parameters=params, dry_run=True),
    )
    assert job.total_bytes_processed is not None

  def test_singular_queries_dry_run(self, collision_dataset, bq_client):
    from google.cloud import bigquery as bq

    from bigquery_agent_analytics.client import _GET_SESSION_TRACE_QUERY
    from bigquery_agent_analytics.client import _RESOLVE_SESSION_CANDIDATES_QUERY
    from bigquery_agent_analytics.trace import TraceFilter

    project, dataset_id = collision_dataset
    resolve = _RESOLVE_SESSION_CANDIDATES_QUERY.format(
        project=project, dataset=dataset_id, table="agent_events"
    )
    bq_client.query(
        resolve,
        job_config=bq.QueryJobConfig(
            query_parameters=[
                bq.ScalarQueryParameter("session_id", "STRING", "collide")
            ],
            dry_run=True,
        ),
    )
    row_filter = TraceFilter(custom_labels={"run": "v0"})
    fetch = _GET_SESSION_TRACE_QUERY.format(
        project=project,
        dataset=dataset_id,
        table="agent_events",
        row_where=row_filter.row_scope_where(),
    )
    bq_client.query(
        fetch,
        job_config=bq.QueryJobConfig(
            query_parameters=[
                bq.ScalarQueryParameter("session_id", "STRING", "passes"),
                bq.ScalarQueryParameter("anchor_user_id", "STRING", None),
                bq.ScalarQueryParameter(
                    "anchor_root_agent_name", "STRING", None
                ),
                bq.ScalarQueryParameter("label_key_0", "STRING", '"run"'),
                bq.ScalarQueryParameter("label_val_0", "STRING", "v0"),
            ],
            dry_run=True,
        ),
    )


class TestLiveCollisionFixture:
  """Reused session ids must not cross-contaminate (issue #359)."""

  def test_list_traces_separates_users(self, sdk_client):
    from bigquery_agent_analytics.trace import TraceFilter

    traces = [
        t
        for t in sdk_client.list_traces(TraceFilter(limit=50))
        if t.session_id == "collide"
    ]
    assert len(traces) == 2
    by_user = {t.identity.user_id: t for t in traces}
    assert set(by_user) == {"alice", "bob"}
    assert {s.span_id for s in by_user["alice"].spans} == {"a1", "a2"}
    assert {s.span_id for s in by_user["bob"].spans} == {"b1"}

  def test_null_identity_stays_separate(self, sdk_client):
    from bigquery_agent_analytics.trace import TraceFilter

    traces = [
        t
        for t in sdk_client.list_traces(TraceFilter(limit=50))
        if t.session_id == "nulls"
    ]
    assert len(traces) == 2
    null_trace = next(t for t in traces if t.identity.user_id is None)
    named = next(t for t in traces if t.identity.user_id == "carol")
    assert {s.span_id for s in null_trace.spans} == {"n1"}
    assert {s.span_id for s in named.spans} == {"c1"}
    assert named.identity.root_agent_name == "rooty"

  def test_bare_singular_read_ambiguous(self, sdk_client):
    from bigquery_agent_analytics.trace import AmbiguousSessionError

    with pytest.raises(AmbiguousSessionError) as exc_info:
      sdk_client.get_session_trace("collide")
    assert "user_id" in exc_info.value.retry_dimensions

  def test_pinned_singular_read_isolated(self, sdk_client):
    trace = sdk_client.get_session_trace("collide", user_id="alice")
    assert {s.span_id for s in trace.spans} == {"a1", "a2"}
    assert trace.identity.user_id == "alice"

  def test_null_pin_selects_null_candidate(self, sdk_client):
    trace = sdk_client.get_session_trace(
        "nulls", user_id=None, root_agent_name=None
    )
    assert {s.span_id for s in trace.spans} == {"n1"}
    assert trace.identity.user_id is None

  def test_pass_selection_and_ambiguity(self, sdk_client):
    from bigquery_agent_analytics.trace import AmbiguousSessionError

    with pytest.raises(AmbiguousSessionError) as exc_info:
      sdk_client.get_session_trace("passes")
    assert "custom_labels" in exc_info.value.retry_dimensions

    v0 = sdk_client.get_session_trace("passes", custom_labels={"run": "v0"})
    # v0 pass: its rows, the enrichment row, and the shared row —
    # never the v1 row.
    assert {s.span_id for s in v0.spans} == {"p0", "p0e", "shared"}
    assert v0.scope.labels_dict == {"run": "v0"}

    v1 = sdk_client.get_session_trace("passes", custom_labels={"run": "v1"})
    assert {s.span_id for s in v1.spans} == {"p1", "shared"}

  def test_ambiguity_payload_retry_round_trip(self, sdk_client):
    from bigquery_agent_analytics.trace import AmbiguousSessionError
    from bigquery_agent_analytics.trace import TraceSelector

    with pytest.raises(AmbiguousSessionError) as exc_info:
      sdk_client.get_session_trace("collide")
    payload = exc_info.value.to_dict()
    selectors = [TraceSelector(**c["selector"]) for c in payload["candidates"]]
    seen_users = set()
    for selector in selectors:
      trace = sdk_client.get_trace_by_selector(selector)
      seen_users.add(trace.identity.user_id)
    assert seen_users == {"alice", "bob"}
