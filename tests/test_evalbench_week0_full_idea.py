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
"""Tests for ``examples/evalbench_week0_full_idea.sh`` (#435 slice 10).

The EXAMPLE Week 0 scenario pack is ``--fixture`` only: nothing here
reaches BigQuery or the network, and every asserted artifact says
``example: true`` / ``g1_frozen: false`` and that the six-week clock has
not started. The pack itself freezes nothing — it stays illustrative even
now that the Week 0 freeze landed for real: production
``failure_taxonomy.py`` is G1-frozen at v0.1.0 and the real freeze
artifacts live in ``examples/fixtures/week0_real_*.json``
(``tests/test_week0_real_freeze.py``), distinct from this pack.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from bigquery_agent_analytics import failure_taxonomy

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO_ROOT / "examples" / "evalbench_week0_full_idea.sh"
_FIXTURE_DIR = _REPO_ROOT / "examples" / "fixtures"
_TAXONOMY_SRC = (
    _REPO_ROOT / "src" / "bigquery_agent_analytics" / "failure_taxonomy.py"
)

_SESSION_ID = "7e352c34-4c1c-4395-acd5-fb3c8f215346"
_EVAL_ID = "7e352c34"

_SANA_CATEGORIES = (
    "task/planning",
    "wrong source",
    "execution/computation",
    "incomplete evidence",
    "turn-waste",
    "finalization",
    "tool blockers",
)

# The five Week 0 gates, then the widget-session through-line, in order.
_BANNERS = (
    "=== EXAMPLE — Week 0 is not a freeze. Clock has not started. ===",
    "=== EXAMPLE partner + SANA relationship ===",
    "=== EXAMPLE runtime + route ===",
    "=== EXAMPLE pilot-benchmark rubric ===",
    "=== EXAMPLE D4 boundary memo ===",
    "=== EXAMPLE preregistration (not a week-1 freeze) ===",
    "=== This agent was asked to check widget stock. Here is the session. ===",
    "=== This session in failed_sessions (mechanical taxonomy_categories) ===",
    "=== EXAMPLE mapping of mechanical flags onto SANA-seeded names ===",
    "=== Punchline ===",
)

_PUNCHLINE = (
    "This widget-stock session failed because the agent never answered;"
    " goal_completion=0.0."
)

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None, reason="bash not available"
)


def _run(*args: str, env: dict[str, str] | None = None):
  # Start from a clean environment so a developer's exported
  # EVALBENCH_* / BQ_AGENT_* values cannot change what is asserted.
  clean = {
      k: v
      for k, v in os.environ.items()
      if not k.startswith(("EVALBENCH_", "BQ_AGENT_"))
  }
  return subprocess.run(
      ["bash", str(_SCRIPT), *args],
      capture_output=True,
      text=True,
      timeout=60,
      env={**clean, **(env or {})},
  )


def _fixture(name: str) -> dict:
  data = json.loads((_FIXTURE_DIR / name).read_text())
  # Every fixture in the pack is labeled example-only and frozen-nothing.
  assert data["example"] is True
  assert data["g1_frozen"] is False
  assert data["clock_started"] is False
  return data


def _assert_walkthrough(stdout: str) -> None:
  for banner in _BANNERS:
    assert banner in stdout
  # The gates and the through-line appear in narrative order.
  positions = [stdout.index(b) for b in _BANNERS]
  assert positions == sorted(positions)
  opening = stdout[positions[0] : positions[1]]
  partner = stdout[positions[1] : positions[2]]
  runtime = stdout[positions[2] : positions[3]]
  rubric = stdout[positions[3] : positions[4]]
  d4 = stdout[positions[4] : positions[5]]
  prereg = stdout[positions[5] : positions[6]]
  session = stdout[positions[6] : positions[7]]
  failed = stdout[positions[7] : positions[8]]
  mapping = stdout[positions[8] : positions[9]]
  punchline = stdout[positions[9] :]
  # Opening disclaimer: example-only, nothing frozen, clock not started.
  assert "EXAMPLE" in opening
  assert "illustrative" in opening
  assert "not a freeze" in opening
  assert "g1_frozen: false" in opening
  assert "clock has not started" in opening
  assert "clock_started: false" in opening
  # Gate 1: partner + SANA relationship.
  assert "Acme Retail Support" in partner
  assert "SANA-adjacent" in partner
  assert "not a SANA fork" in partner
  for category in _SANA_CATEGORIES:
    assert category in partner
  assert "LakeQA" in partner
  assert "KramaBench" in partner
  assert "not duplicating" in partner
  # Gate 2: runtime + route.
  assert "ADK" in runtime
  assert "bqaa_e2e_real.agent_events" in runtime
  assert "mvp-e2e-real-traces" in runtime
  assert "EvalBench-hosted" in runtime
  assert "EvalBench-only MVP route is CORRECT" in runtime
  assert "D1 does not need re-decision" in runtime
  # Gate 3: pilot-benchmark rubric, scored per criterion.
  for criterion in (
      "collaborator relevance",
      "failure-mode coverage",
      "score availability / threshold-definability",
      "ground-truth depth",
      "trace fidelity",
  ):
    assert criterion in rubric
  assert "pass" in rubric
  assert _EVAL_ID in rubric
  assert "goal_completion" in rubric
  assert "0.0" in rubric
  assert "threshold 1" in rubric
  assert "ab7535a5" in rubric
  assert "There are 0 widgets in stock." in rubric
  assert "USER_MESSAGE_RECEIVED" in rubric
  assert "INVOCATION_STARTING" in rubric
  assert "AGENT_STARTING" in rubric
  # Gate 4: D4 boundary memo.
  assert "fail-closed" in d4.lower()
  assert "Alex Rivera (example collaborator)" in d4
  assert "Jordan Lee (example collaborator)" in d4
  assert "ingestion" in d4
  assert "taxonomy mechanics" in d4
  assert "stability" in d4
  assert "ONLY" in d4
  assert "never" in d4
  assert "Part II funding recommendation" in d4
  assert "test-project-0728-467323" in d4
  assert "pre-redacted" in d4
  # Gate 5: preregistration floors, copied as an example.
  assert "EXAMPLE" in prereg
  assert "not week-1 freeze" in prereg
  assert "clock not started" in prereg
  assert "80%" in prereg
  assert "0.6" in prereg
  assert "0.45" in prereg
  assert "70%" in prereg
  assert "+10pp" in prereg
  assert ">0" in prereg
  # Through-line: the widget session.
  assert "support_agent" in session
  assert "real-user-0" in session
  assert "How many widgets are in stock?" in session
  assert _SESSION_ID in session
  assert _EVAL_ID in session
  # Its failed_sessions row with the mechanical categories.
  assert "process_failed" in failed
  assert "missing_completion" in failed
  assert "score_failed" in failed
  assert (
      'taxonomy_categories: ["process_failed", "missing_completion",'
      ' "score_failed"]'
  ) in failed
  # The EXAMPLE mapping onto SANA-seeded names — labeled not-G1.
  assert "EXAMPLE" in mapping
  assert "not G1" in mapping
  assert "g1_frozen: false" in mapping
  assert "task/planning" in mapping
  assert "finalization" in mapping
  assert "tool blockers" in mapping
  assert "never called check_inventory" in mapping
  # Punchline: exactly one sentence, then nothing else.
  assert punchline.strip().splitlines()[1:] == [_PUNCHLINE]


def test_fixture_flag_walks_all_five_gates_and_exits_zero() -> None:
  result = _run("--fixture")
  assert result.returncode == 0, result.stderr
  assert result.stderr == ""
  assert "fixture mode" in result.stdout
  assert _PUNCHLINE in result.stdout
  _assert_walkthrough(result.stdout)


def test_fixture_env_var_is_honored() -> None:
  result = _run(env={"EVALBENCH_FIXTURE": "1"})
  assert result.returncode == 0, result.stderr
  _assert_walkthrough(result.stdout)


def test_without_fixture_flag_exits_two_and_launches_nothing() -> None:
  result = _run()
  assert result.returncode == 2
  assert result.stdout == ""
  assert "--fixture only" in result.stderr
  # No live job is mentioned, let alone launched.
  assert "bq " not in result.stderr
  assert "Step" not in result.stderr


def test_synth_flag_is_rejected_without_launching_anything() -> None:
  result = _run("--synth")
  assert result.returncode == 2
  assert result.stdout == ""
  assert "--fixture only" in result.stderr


def test_partner_fixture_names_the_example_partner_and_sana_seeds() -> None:
  data = _fixture("week0_example_partner.json")
  assert data["illustrative"] is True
  assert data["not_a_freeze"] is True
  assert data["partner_name"] == "Acme Retail Support"
  assert data["sana_runtime"] == "Strands"
  assert data["this_pilot_runtime"] == "ADK+EvalBench"
  assert tuple(data["sana_categories"]) == _SANA_CATEGORIES
  assert "not duplicating" in data["not_duplicating_lakeqa_kramabench"]


def test_rubric_fixture_scores_all_five_criteria_on_the_session() -> None:
  data = _fixture("week0_example_rubric.json")
  assert data["selected_benchmark"] == "widget-stock support"
  assert data["session_id"] == _SESSION_ID
  assert data["eval_id"] == _EVAL_ID
  assert data["job_id"] == "mvp-e2e-real-traces"
  criteria = data["criteria"]
  assert len(criteria) == 5
  for entry in criteria.values():
    assert entry["result"] == "pass"
    assert entry["reason"]
  names = {entry["name"] for entry in criteria.values()}
  assert names == {
      "collaborator relevance",
      "failure-mode coverage",
      "score availability / threshold-definability",
      "ground-truth depth",
      "trace fidelity",
  }


def test_d4_fixture_is_fail_closed_with_example_consumers() -> None:
  data = _fixture("week0_example_d4_memo.json")
  assert data["fail_closed"] is True
  assert data["report_consumers"]
  for consumer in data["report_consumers"]:
    assert "example" in consumer
  assert set(data["fixture_validates"]) == {
      "ingestion",
      "taxonomy mechanics",
      "stability",
  }
  assert "Part II" in data["never_produces"]
  assert data["reference_project"] == "test-project-0728-467323"
  assert data["pre_redacted"] is True
  assert data["stop_go_memo_is_governed_artifact"] is True


def test_preregistration_fixture_copies_the_plan_floors() -> None:
  data = _fixture("week0_example_preregistration.json")
  assert data["not_week_1_freeze"] is True
  floors = data["floors"]
  assert floors["replicate_agreement_pct"] == 80
  assert floors["non_unknown_coverage_pct"] == 80
  assert floors["kappa_point"] == 0.6
  assert floors["kappa_ci_lower"] == 0.45
  assert floors["localization_coverage_pct"] == 70
  assert floors["hit_at_1_ci_lower_gt_0"] is True
  assert floors["hit_at_1_point_uplift_pp"] == 10
  assert "reserved_revision_week" in data["decision_rules"]
  assert "value_gate" in data["decision_rules"]
  assert "noisy_small_n_localization" in data["decision_rules"]


def test_taxonomy_seed_fixture_maps_this_session_without_freezing() -> None:
  data = _fixture("week0_example_taxonomy_seed.json")
  assert data["not_g1"] is True
  assert data["taxonomy_version"] == "0.1.0-example"
  assert data["taxonomy_version"] != failure_taxonomy.TAXONOMY_VERSION
  assert tuple(data["sana_categories"]) == _SANA_CATEGORIES
  assert tuple(data["mechanical_flags"]) == failure_taxonomy.MECHANICAL_FLAGS
  widget = data["widget_session"]
  assert widget["session_id"] == _SESSION_ID
  assert widget["eval_id"] == _EVAL_ID
  # The EXAMPLE narrative predates the G1 freeze and hardcodes the
  # mechanical flag ids as its categories — correct for an example pack.
  assert tuple(widget["taxonomy_categories"]) == (
      failure_taxonomy.MECHANICAL_FLAGS
  )
  mapping = data["example_mapping"]
  assert mapping["missing_completion"] == "finalization"
  assert mapping["process_failed"] == "tool blockers"
  assert "task/planning" in mapping.values()
  for seeded in mapping.values():
    assert seeded in _SANA_CATEGORIES


def test_example_pack_stays_example_while_production_is_frozen() -> None:
  # The pack stays illustrative (example: true, g1_frozen: false — the
  # _fixture helper asserts both) while production froze G1 at v0.1.0.
  # The example fixtures are NOT the freeze artifacts; the real freeze
  # lives in examples/fixtures/week0_real_*.json and failure_taxonomy.py.
  for name in (
      "week0_example_partner.json",
      "week0_example_rubric.json",
      "week0_example_d4_memo.json",
      "week0_example_preregistration.json",
      "week0_example_taxonomy_seed.json",
  ):
    _fixture(name)
  assert failure_taxonomy.TAXONOMY_VERSION == "0.1.0"
  config = failure_taxonomy.taxonomy_config()
  assert config["g1_frozen"] is True
  assert config["taxonomy_version"] == "0.1.0"
  assert config["dialects"] == []
  category_names = {
      category["name"]
      for metric in config["metrics"]
      for category in metric["categories"]
  }
  assert category_names == set(_SANA_CATEGORIES) | {"unknown"}
  # The example pack's mapping agrees with the frozen FLAG_TO_CATEGORY,
  # so the story it tells matches what production now does.
  seed = json.loads(
      (_FIXTURE_DIR / "week0_example_taxonomy_seed.json").read_text()
  )
  assert seed["example_mapping"] == dict(failure_taxonomy.FLAG_TO_CATEGORY)
  # And as source text: the freeze is in the production module.
  source = _TAXONOMY_SRC.read_text()
  assert '"g1_frozen": True' in source
  assert 'TAXONOMY_VERSION = "0.1.0"' in source
