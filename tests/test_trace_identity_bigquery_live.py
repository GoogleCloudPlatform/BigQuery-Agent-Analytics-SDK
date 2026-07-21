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
