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

"""Tests for the REAL Week 0 freeze artifacts (#435).

The freeze PR lands the real partner record, the fail-closed D4 memo, the
G1 taxonomy v0.1.0 freeze, and the sealed preregistration —
distinct from the slice-10 EXAMPLE pack, which stays illustrative
(``example: true`` / ``g1_frozen: false``). Freezing is not a clock start:
every real fixture says ``clock_started: false``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bigquery_agent_analytics import failure_taxonomy

_REPO_ROOT = Path(__file__).resolve().parents[1]
_DOCS = _REPO_ROOT / "docs"
_FIXTURE_DIR = _REPO_ROOT / "examples" / "fixtures"

_REAL_FIXTURES = (
    "week0_real_partner.json",
    "week0_real_rubric.json",
    "week0_real_d4_memo.json",
    "week0_real_taxonomy.json",
    "week0_real_preregistration.json",
)

_EXAMPLE_FIXTURES = (
    "week0_example_partner.json",
    "week0_example_rubric.json",
    "week0_example_d4_memo.json",
    "week0_example_preregistration.json",
    "week0_example_taxonomy_seed.json",
)

_FREEZE_DOCS = (
    "week0_partner.md",
    "week0_d4_memo.md",
    "week0_g1_taxonomy.md",
    "week0_preregistration.md",
)

_SESSION_ID = "7e352c34-4c1c-4395-acd5-fb3c8f215346"


def _real(name: str) -> dict:
  data = json.loads((_FIXTURE_DIR / name).read_text())
  # Every real freeze fixture is example: false and does not start the
  # six-week clock.
  assert data["example"] is False
  assert data["clock_started"] is False
  return data


# --- production module is frozen ------------------------------------------


def test_production_taxonomy_is_g1_frozen_at_v0_1_0() -> None:
  assert failure_taxonomy.TAXONOMY_VERSION == "0.1.0"
  config = failure_taxonomy.taxonomy_config()
  assert config["g1_frozen"] is True
  assert config["taxonomy_version"] == "0.1.0"


def test_widget_session_flags_map_to_frozen_names_in_frozen_order() -> None:
  # The widget-stock pilot session trips all three mechanical flags.
  row = {
      "session_id": _SESSION_ID,
      "process_failed": True,
      "missing_completion": True,
      "score_failed": True,
  }
  assert failure_taxonomy.categorize_failed_session(row) == (
      "task/planning",
      "finalization",
      "tool blockers",
  )


# --- real fixtures --------------------------------------------------------


@pytest.mark.parametrize("name", _REAL_FIXTURES)
def test_real_fixtures_are_not_examples_and_do_not_start_the_clock(
    name: str,
) -> None:
  _real(name)


def test_real_taxonomy_fixture_matches_the_production_freeze() -> None:
  data = _real("week0_real_taxonomy.json")
  assert data["g1_frozen"] is True
  assert data["taxonomy_version"] == "0.1.0"
  assert tuple(data["frozen_category_names"]) == (
      failure_taxonomy.FROZEN_CATEGORY_NAMES
  )
  assert tuple(data["mechanical_flags"]) == failure_taxonomy.MECHANICAL_FLAGS
  assert data["flag_to_category"] == dict(failure_taxonomy.FLAG_TO_CATEGORY)
  assert data["dialects"] == []
  widget = data["widget_session"]
  assert widget["session_id"] == _SESSION_ID
  assert tuple(widget["taxonomy_categories"]) == (
      failure_taxonomy.categorize_failed_session(
          {flag: True for flag in failure_taxonomy.MECHANICAL_FLAGS}
      )
  )


def test_preregistration_is_sealed_and_does_not_start_the_clock() -> None:
  data = _real("week0_real_preregistration.json")
  assert data["sealed"] is True
  assert data["freeze_candidate"] is False
  assert data["clock_started"] is False
  assert data["floors"]["value_gate_pct"] == 50
  assert data["floors"]["replicate_agreement_pct"] == 80
  assert data["floors"]["kappa_point"] == 0.6
  assert data["floors"]["kappa_ci_lower"] == 0.45
  assert "FAILS" in data["decision_rules"]["noisy_small_n_localization"] or "fails" in data["decision_rules"]["noisy_small_n_localization"].lower()
  text = (_DOCS / "week0_preregistration.md").read_text()
  assert "sealed" in text.lower()
  assert ("not started" in text.lower()) or ("has **not** started" in text)


def test_real_partner_is_bqaa_not_acme_and_not_a_sana_fork() -> None:
  data = _real("week0_real_partner.json")
  assert "Google Cloud BigQuery Agent Analytics" in data["partner_name"]
  assert data["not_a_sana_fork"] is True
  assert data["not_a_named_sana_collaboration"] is True
  assert "not duplicating" in data["not_duplicating_lakeqa_kramabench"]
  assert data["route"]["evalbench_only_mvp_route_correct"] is True
  assert data["route"]["d1_needs_redecision"] is False
  assert data["pilot"]["evalbench_job_id"] == "mvp-e2e-real-traces"
  assert "Acme" not in json.dumps(data)


def test_real_partner_doc_has_no_acme() -> None:
  text = (_DOCS / "week0_partner.md").read_text()
  assert "Acme" not in text
  assert "Google Cloud BigQuery Agent Analytics" in text
  assert "not a SANA fork" in text
  assert "not duplicating" in text


def test_real_rubric_pins_the_widget_session_and_gold() -> None:
  data = _real("week0_real_rubric.json")
  assert data["session_id"] == _SESSION_ID
  assert data["eval_id"] == "7e352c34"
  assert data["job_id"] == "mvp-e2e-real-traces"
  assert data["gold"]["sibling_session"] == "ab7535a5"
  assert data["gold"]["gold_answer"] == "There are 0 widgets in stock."
  assert data["score"]["metric"] == "goal_completion"
  assert data["score"]["failed_session_score"] == 0
  assert data["score"]["gold_session_score"] == 1


def test_real_d4_memo_is_fail_closed_with_one_named_consumer() -> None:
  data = _real("week0_real_d4_memo.json")
  assert data["fail_closed"] is True
  assert data["report_consumers"] == [
      "Hai-Yuan Cao (caohy1988 / haiyuan-eng-google)"
  ]
  assert data["scope_project"] == "test-project-0728-467323"
  assert set(data["scope_datasets"]) == {
      "bqaa_e2e_real",
      "bqaa_evalbench_mvp_demo",
      "bqaa_evalbench_mvp_mirror",
  }
  assert data["never_produces"] == "Part II funding recommendation"
  assert data["no_new_live_judge_calls"] is True
  assert data["no_new_bq_jobs"] is True
  assert data["no_labeler_access_expansion"] is True
  assert data["stop_go_memo_is_governed_artifact"] is True
  assert "no IAM API calls" in data["grants_policy"]
  # No fabricated people from the example pack.
  memo_text = json.dumps(data)
  assert "Alex Rivera" not in memo_text
  assert "Jordan Lee" not in memo_text


def test_real_preregistration_copies_the_v4_floors_without_a_clock() -> None:
  data = _real("week0_real_preregistration.json")
  assert data["freeze_candidate"] is False
  assert data["sealed"] is True
  assert data["not_week_1_execution"] is True
  floors = data["floors"]
  assert floors["replicate_agreement_pct"] == 80
  assert floors["non_unknown_coverage_pct"] == 80
  assert floors["kappa_point"] == 0.6
  assert floors["kappa_ci_lower"] == 0.45
  assert floors["localization_coverage_pct"] == 70
  assert floors["hit_at_1_ci_lower_gt_0"] is True
  assert floors["hit_at_1_point_uplift_pp"] == 10
  for rule in (
      "reserved_revision_week",
      "value_gate",
      "noisy_small_n_localization",
      "sealed_sets",
  ):
    assert data["decision_rules"][rule]


# --- the example pack stays example ---------------------------------------


@pytest.mark.parametrize("name", _EXAMPLE_FIXTURES)
def test_example_fixtures_are_still_present_and_still_examples(
    name: str,
) -> None:
  data = json.loads((_FIXTURE_DIR / name).read_text())
  assert data["example"] is True
  assert data["g1_frozen"] is False
  assert data["clock_started"] is False


def test_example_pack_shell_and_md_are_still_present() -> None:
  assert (_REPO_ROOT / "examples" / "evalbench_week0_full_idea.sh").is_file()
  assert (_REPO_ROOT / "examples" / "evalbench_week0_full_idea.md").is_file()


# --- docs -----------------------------------------------------------------


@pytest.mark.parametrize("name", _FREEZE_DOCS)
def test_freeze_docs_exist_and_do_not_start_the_clock(name: str) -> None:
  text = (_DOCS / name).read_text()
  assert (
      "NOT started" in text
      or "not started" in text
      or ("has **not** started" in text)
  )
  assert "first Week 1 snapshot job" in text
