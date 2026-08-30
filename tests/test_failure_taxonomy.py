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

"""Tests for the G1-frozen failure taxonomy v0.1 (#435).

Slices 8/9 landed the mechanical mapper and its ``EvalBenchSession``
consumer; the Week 0 G1 freeze replaced the scaffold vocabulary with the
frozen names (SANA-neighborhood seven + ``unknown``) at
``taxonomy_version: 0.1.0`` / ``g1_frozen: True``. Assignment stays
mechanical: each tripped flag maps to one frozen name, returned in frozen
order.
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
from bigquery_agent_analytics.failure_taxonomy import FLAG_TO_CATEGORY
from bigquery_agent_analytics.failure_taxonomy import FROZEN_CATEGORY_NAMES
from bigquery_agent_analytics.failure_taxonomy import MECHANICAL_FLAGS
from bigquery_agent_analytics.failure_taxonomy import scaffold_taxonomy_config
from bigquery_agent_analytics.failure_taxonomy import taxonomy_config
from bigquery_agent_analytics.failure_taxonomy import TAXONOMY_VERSION

_FLAGS = ("process_failed", "missing_completion", "score_failed")

_FROZEN_NAMES = (
    "task/planning",
    "wrong source",
    "execution/computation",
    "incomplete evidence",
    "turn-waste",
    "finalization",
    "tool blockers",
    "unknown",
)


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


def _expected(*tripped_flags):
  # Frozen names for the tripped flags, in FROZEN_CATEGORY_NAMES order.
  mapped = {FLAG_TO_CATEGORY[flag] for flag in tripped_flags}
  return tuple(name for name in FROZEN_CATEGORY_NAMES if name in mapped)


# --- mapper ---------------------------------------------------------------


@pytest.mark.parametrize(
    "flag,frozen_name",
    [
        ("process_failed", "tool blockers"),
        ("missing_completion", "finalization"),
        ("score_failed", "task/planning"),
    ],
)
def test_each_flag_alone_maps_to_its_frozen_name(flag, frozen_name):
  assert categorize_failed_session(_row(**{flag: True})) == (frozen_name,)


@pytest.mark.parametrize(
    "tripped",
    [
        combo
        for size in (2, 3)
        for combo in itertools.combinations(_FLAGS, size)
    ],
)
def test_flag_combinations_map_to_every_tripped_frozen_name(tripped):
  row = _row(**{flag: True for flag in tripped})
  assert categorize_failed_session(row) == _expected(*tripped)


def test_all_flags_false_returns_empty_never_unknown():
  # unknown is in the frozen vocabulary as the residual bucket of the
  # labeling study; the mechanical mapper never emits it.
  assert categorize_failed_session(_row()) == ()


def test_category_order_follows_the_frozen_order_not_flag_order():
  # score_failed maps to task/planning, which precedes tool blockers in
  # FROZEN_CATEGORY_NAMES even though process_failed precedes score_failed
  # in flag order.
  row = _row(score_failed=True, process_failed=True)
  assert categorize_failed_session(row) == ("task/planning", "tool blockers")
  assert categorize_failed_session(
      _row(process_failed=True, missing_completion=True)
  ) == ("finalization", "tool blockers")


def test_widget_session_with_all_three_flags_maps_in_frozen_order():
  # The widget-stock pilot session 7e352c34 trips all three flags.
  assert categorize_failed_session(_row(**{flag: True for flag in _FLAGS})) == (
      "task/planning",
      "finalization",
      "tool blockers",
  )


def test_extra_fields_are_ignored():
  row = _row(
      process_failed=True,
      session_id="evalbench-import:job:v1:s1",
      scenario_id="s1",
      started_at="2026-08-30T00:00:00Z",
      failed=True,
      failing_scores=[{"comparator": "exact_match", "score": 0.0}],
  )
  assert categorize_failed_session(row) == ("tool blockers",)


def test_session_verdict_objects_are_accepted():
  verdict = _verdict(missing_completion=True, score_failed=True)
  assert categorize_failed_session(verdict) == (
      "task/planning",
      "finalization",
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


def test_mapper_never_returns_flag_ids():
  for size in (1, 2, 3):
    for tripped in itertools.combinations(_FLAGS, size):
      names = categorize_failed_session(_row(**{f: True for f in tripped}))
      assert not set(names).intersection(_FLAGS)
      assert set(names) <= set(FROZEN_CATEGORY_NAMES)
      assert "unknown" not in names


# --- config ---------------------------------------------------------------


def test_config_is_g1_frozen_at_v0_1_0():
  config = taxonomy_config()
  assert TAXONOMY_VERSION == "0.1.0"
  assert config["taxonomy_version"] == "0.1.0"
  assert "scaffold" not in config["taxonomy_version"]
  assert config["g1_frozen"] is True


def test_config_dialects_slot_exists_and_is_empty_by_default():
  # D2: one taxonomy with optional per-benchmark extension categories on
  # the same core. The slot must exist and stays empty at the freeze.
  assert taxonomy_config()["dialects"] == []


def test_frozen_names_are_the_sana_seven_plus_unknown_in_frozen_order():
  assert FROZEN_CATEGORY_NAMES == _FROZEN_NAMES
  assert CORE_CATEGORY_IDS == FROZEN_CATEGORY_NAMES
  assert FROZEN_CATEGORY_NAMES[-1] == "unknown"


def test_mechanical_flags_keep_the_landed_contract_keys():
  assert MECHANICAL_FLAGS == _FLAGS
  assert set(FLAG_TO_CATEGORY) == set(MECHANICAL_FLAGS)
  assert FLAG_TO_CATEGORY["missing_completion"] == "finalization"
  assert FLAG_TO_CATEGORY["process_failed"] == "tool blockers"
  assert FLAG_TO_CATEGORY["score_failed"] == "task/planning"


def test_config_core_categories_are_the_frozen_names_with_definitions():
  config = taxonomy_config()
  (metric,) = config["metrics"]
  assert metric["name"] == "failure_category"
  assert [c["name"] for c in metric["categories"]] == list(
      FROZEN_CATEGORY_NAMES
  )
  for category in metric["categories"]:
    # Every definition states how mechanical assignment works until the
    # labeler study (mapped from a flag, or not emitted by the mapper).
    assert "mechanical" in category["definition"].lower()
    if category["name"] in FLAG_TO_CATEGORY.values():
      assert "mapped from" in category["definition"]
    else:
      assert "not emit" in category["definition"]


def test_unknown_is_residual_in_vocabulary_but_not_mapped():
  config = taxonomy_config()
  (metric,) = config["metrics"]
  unknown = next(c for c in metric["categories"] if c["name"] == "unknown")
  assert "residual" in unknown["definition"].lower()
  assert "unknown" not in FLAG_TO_CATEGORY.values()


def test_config_metrics_shape_is_interpretable_by_build_metrics():
  # #431 schema shape: build_metrics() can turn the core metric into a
  # CategoricalMetricDefinition without special-casing.
  (metric,) = build_metrics(taxonomy_config())
  assert metric.name == "failure_category"
  assert [c.name for c in metric.categories] == list(FROZEN_CATEGORY_NAMES)


def test_scaffold_taxonomy_config_is_a_wrapper_for_the_frozen_config():
  # The pre-freeze name survives as a compatibility wrapper; it returns
  # the same frozen config, not the retired scaffold.
  assert scaffold_taxonomy_config() == taxonomy_config()
  assert scaffold_taxonomy_config()["g1_frozen"] is True
  assert scaffold_taxonomy_config()["taxonomy_version"] == "0.1.0"


def test_config_returns_a_deep_copy():
  first = taxonomy_config()
  first["g1_frozen"] = False
  first["metrics"][0]["categories"].clear()
  first["dialects"].append({"benchmark": "nope"})
  fresh = taxonomy_config()
  assert fresh["g1_frozen"] is True
  assert len(fresh["metrics"][0]["categories"]) == len(FROZEN_CATEGORY_NAMES)
  assert fresh["dialects"] == []
  wrapped = scaffold_taxonomy_config()
  wrapped["g1_frozen"] = False
  assert scaffold_taxonomy_config()["g1_frozen"] is True


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
def test_session_to_dict_carries_the_tripped_frozen_names(tripped):
  session = _session(**{flag: True for flag in tripped})
  assert session.taxonomy_categories == categorize_failed_session(session)
  assert session.taxonomy_categories == _expected(*tripped)
  assert session.to_dict()["taxonomy_categories"] == list(_expected(*tripped))


def test_session_with_all_flags_false_serializes_empty_categories():
  # An include_passed row: empty list, never the residual "unknown" bucket.
  session = _session()
  assert session.taxonomy_categories == ()
  assert session.to_dict()["taxonomy_categories"] == []
