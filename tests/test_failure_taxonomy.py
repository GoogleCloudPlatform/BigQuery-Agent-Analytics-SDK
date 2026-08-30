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

"""Tests for the mechanical failure-taxonomy scaffold (#435 slice 8).

Slice 9 adds the consumer tests: ``EvalBenchSession`` (the row
``failed_sessions`` returns and the CLI serializes) exposes
``taxonomy_categories`` computed from its flags via
``categorize_failed_session``.
"""

from datetime import datetime
from datetime import timezone
import itertools

import pytest

from bigquery_agent_analytics.evalbench import EvalBenchSession
from bigquery_agent_analytics.evalbench import SessionVerdict
from bigquery_agent_analytics.evaluation_rubrics import build_metrics
from bigquery_agent_analytics.failure_taxonomy import categorize_failed_session
from bigquery_agent_analytics.failure_taxonomy import CORE_CATEGORY_IDS
from bigquery_agent_analytics.failure_taxonomy import scaffold_taxonomy_config
from bigquery_agent_analytics.failure_taxonomy import SCAFFOLD_TAXONOMY_VERSION

_FLAGS = ("process_failed", "missing_completion", "score_failed")


def _row(**overrides):
  row = {flag: False for flag in _FLAGS}
  row.update(overrides)
  return row


def _verdict(**overrides):
  return SessionVerdict(
      session_id="evalbench-import:job:v1:s1",
      scenario_id="s1",
      failing_scores={},
      failed=any(overrides.values()),
      **_row(**overrides),
  )


# --- mapper ---------------------------------------------------------------


@pytest.mark.parametrize("flag", _FLAGS)
def test_each_flag_alone_maps_to_its_category(flag):
  assert categorize_failed_session(_row(**{flag: True})) == (flag,)


@pytest.mark.parametrize(
    "tripped",
    [
        combo
        for size in (2, 3)
        for combo in itertools.combinations(_FLAGS, size)
    ],
)
def test_flag_combinations_map_to_every_tripped_category(tripped):
  row = _row(**{flag: True for flag in tripped})
  assert categorize_failed_session(row) == tripped


def test_all_flags_false_returns_empty_not_an_unknown_bucket():
  assert categorize_failed_session(_row()) == ()


def test_category_order_follows_the_core_config_order():
  row = _row(score_failed=True, process_failed=True)
  assert categorize_failed_session(row) == ("process_failed", "score_failed")
  assert categorize_failed_session(
      _row(**{flag: True for flag in _FLAGS})
  ) == tuple(CORE_CATEGORY_IDS)


def test_extra_fields_are_ignored():
  row = _row(
      process_failed=True,
      session_id="evalbench-import:job:v1:s1",
      scenario_id="s1",
      started_at="2026-08-30T00:00:00Z",
      failed=True,
      failing_scores=[{"comparator": "exact_match", "score": 0.0}],
  )
  assert categorize_failed_session(row) == ("process_failed",)


def test_session_verdict_objects_are_accepted():
  verdict = _verdict(missing_completion=True, score_failed=True)
  assert categorize_failed_session(verdict) == (
      "missing_completion",
      "score_failed",
  )
  assert categorize_failed_session(_verdict()) == ()


def test_missing_flag_raises_instead_of_defaulting():
  row = _row(process_failed=True)
  del row["score_failed"]
  with pytest.raises(ValueError, match="missing required flag 'score_failed'"):
    categorize_failed_session(row)


@pytest.mark.parametrize("bad", [None, 0, 1, "true"])
def test_non_bool_flag_raises(bad):
  with pytest.raises(ValueError, match="'process_failed' must be a bool"):
    categorize_failed_session(_row(process_failed=bad))


def test_mapper_is_deterministic():
  row = _row(process_failed=True, missing_completion=True)
  assert categorize_failed_session(row) == categorize_failed_session(row)


# --- config ---------------------------------------------------------------


def test_config_version_is_obviously_scaffold_and_not_g1_frozen():
  config = scaffold_taxonomy_config()
  assert config["taxonomy_version"] == SCAFFOLD_TAXONOMY_VERSION
  assert "scaffold" in config["taxonomy_version"]
  assert config["g1_frozen"] is False


def test_config_dialects_slot_exists_and_is_empty_by_default():
  # D2: one taxonomy with optional per-benchmark extension categories on
  # the same core. The slot must exist and must not ship invented labels.
  assert scaffold_taxonomy_config()["dialects"] == []


def test_config_core_categories_match_the_landed_flags():
  config = scaffold_taxonomy_config()
  (metric,) = config["metrics"]
  assert metric["name"] == "failure_category"
  assert [c["name"] for c in metric["categories"]] == list(CORE_CATEGORY_IDS)
  assert list(CORE_CATEGORY_IDS) == list(_FLAGS)
  for category in metric["categories"]:
    assert category["definition"]


def test_config_does_not_contain_unfrozen_sana_names():
  # The SANA seven-category vocabulary stays out of this scaffold entirely.
  text = repr(scaffold_taxonomy_config()).lower()
  for sana_name in ("planning", "wrong source", "turn-waste", "finalization"):
    assert sana_name not in text


def test_config_metrics_shape_is_interpretable_by_build_metrics():
  # #431 schema shape: build_metrics() can turn the core metric into a
  # CategoricalMetricDefinition without special-casing.
  (metric,) = build_metrics(scaffold_taxonomy_config())
  assert metric.name == "failure_category"
  assert [c.name for c in metric.categories] == list(CORE_CATEGORY_IDS)


def test_config_returns_a_deep_copy():
  first = scaffold_taxonomy_config()
  first["g1_frozen"] = True
  first["metrics"][0]["categories"].clear()
  first["dialects"].append({"benchmark": "nope"})
  fresh = scaffold_taxonomy_config()
  assert fresh["g1_frozen"] is False
  assert len(fresh["metrics"][0]["categories"]) == len(CORE_CATEGORY_IDS)
  assert fresh["dialects"] == []


# --- EvalBenchSession consumer (slice 9) ----------------------------------


def _session(**overrides):
  # The same constructor shape the pre-slice-9 tests use: no taxonomy
  # field exists, so existing callers keep working unchanged.
  return EvalBenchSession(
      job_id="job-123",
      import_version="v1",
      session_id="evalbench-import:job-123:v1:s1",
      trace_id="evalbench-import:job-123:v1:s1",
      scenario_id="s1",
      started_at=datetime(2026, 8, 30, tzinfo=timezone.utc),
      failed=any(overrides.values()),
      **_row(**overrides),
  )


@pytest.mark.parametrize(
    "tripped",
    [
        combo
        for size in (1, 2, 3)
        for combo in itertools.combinations(_FLAGS, size)
    ],
)
def test_session_to_dict_carries_the_tripped_categories(tripped):
  session = _session(**{flag: True for flag in tripped})
  assert session.taxonomy_categories == categorize_failed_session(session)
  assert session.to_dict()["taxonomy_categories"] == list(
      categorize_failed_session(session)
  )
  assert session.to_dict()["taxonomy_categories"] == list(tripped)


def test_session_with_all_flags_false_serializes_empty_categories():
  # An include_passed row: empty list, never an invented "unknown" bucket.
  session = _session()
  assert session.taxonomy_categories == ()
  assert session.to_dict()["taxonomy_categories"] == []
