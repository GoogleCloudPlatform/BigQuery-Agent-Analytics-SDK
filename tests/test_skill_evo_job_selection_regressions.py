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

"""Selection regressions using the real SDK engine and offline host hooks."""

import json
from pathlib import Path
import sys

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_JOB_DIR = _REPO_ROOT / "deploy" / "skill_evolution_job"
if str(_JOB_DIR) not in sys.path:
  sys.path.insert(0, str(_JOB_DIR))

from skill_evolution_job import coevolve
from skill_evolution_job import engine
from skill_evolution_job import evolve
from skill_evolution_job import hooks
from skill_evolution_job import scoring
from skill_evolution_job import tools

_BASE = (
    "---\nname: example\n---\n## Rules\nKeep evidence.\n## Output\nAnswer.\n"
)
_CANDIDATE = _BASE + "Check the requested inventory before answering.\n"
_UNUSABLE = [
    None,
    [],
    {},
    {"status": "error", "meaningful_rate": 99},
    {"error": "evaluation failed", "meaningful_rate": 99},
    {"skipped": True, "meaningful_rate": 99},
    {"unmeasurable": True, "meaningful_rate": 99},
    {"meaningful_rate": None},
    {"meaningful_rate": float("nan")},
    {"meaningful_rate": float("inf")},
    {"meaningful_rate": "invalid"},
    {"returncode": 7, "meaningful_rate": 99},
]


@pytest.fixture
def scenario(tmp_path, monkeypatch):
  for name in (
      "SDK_SCRIPTS_DIR",
      "EVOLUTION_CANDIDATES",
      "EVOLUTION_MAX_ANALYSTS",
      "EVOLUTION_TARGET_AGENTS",
      "EVOLUTION_MAX_ROUNDS",
  ):
    monkeypatch.delenv(name, raising=False)
  engine.reset_cache()
  tools._rounds_run.clear()
  module = engine.load_engine()
  monkeypatch.setattr(evolve, "_vertex_client", lambda: object())
  monkeypatch.setattr(evolve, "_derive_toolbox", lambda *_: None)
  monkeypatch.setattr(evolve, "_agent_for_skill_dir", lambda *_: None)
  monkeypatch.setattr(evolve, "_resolve_error_analyst", lambda *_: None)
  monkeypatch.setattr(
      module, "collect_patches", lambda *a, **kw: ["## Root Cause\n[skill]"]
  )
  monkeypatch.setattr(module, "run_consolidator", lambda *a, **kw: _CANDIDATE)
  monkeypatch.setattr(coevolve, "_evolution_order", lambda names: names)
  skill_dir = tmp_path / "agent" / "skill"
  skill_dir.mkdir(parents=True)
  (skill_dir / "SKILL.md").write_text(_BASE)
  report = tmp_path / "production_report.json"
  report.write_text(
      json.dumps({"summary": {"meaningful_rate": 20}, "sessions": []})
  )
  run_dir = tmp_path / "run"
  run_dir.mkdir()
  yield module, skill_dir, report, run_dir
  engine.reset_cache()
  tools._rounds_run.clear()


def _run(mode, scenario, monkeypatch, hook):
  _, skill_dir, report, run_dir = scenario
  monkeypatch.setattr(
      hooks,
      "get_hook",
      lambda name: (hook, "test") if name == "score" else (None, "test"),
  )
  if mode == "single":
    return tools.run_evolution(
        str(report), str(skill_dir), run_dir=str(run_dir), candidates=1
    )
  monkeypatch.setenv("EVOLUTION_TARGET_AGENTS", "agent")
  result = coevolve.coevolve(
      str(report),
      agent_configs={"agent": {"skill_dir": str(skill_dir)}},
      output_dir=str(run_dir),
      candidates=1,
  )
  return result.evolved_agents["agent"]


@pytest.mark.parametrize("mode", ["single", "coevolve"])
@pytest.mark.parametrize("candidate_rate", [80, 95])
@pytest.mark.parametrize("older_engine_contract", [False, True])
def test_same_eval_baseline_and_single_candidate_score_record(
    scenario, monkeypatch, mode, candidate_rate, older_engine_contract
):
  module, skill_dir, _, run_dir = scenario
  if older_engine_contract:
    # Older engines drop incumbent_score and call score_fn(V0) themselves.
    # The real engine follows that same path when this argument is absent.
    def older_compat(*args, **kwargs):
      kwargs.pop("incumbent_score", None)
      return module.evolve_skill(*args, **kwargs)

    monkeypatch.setattr(engine, "evolve_skill_compat", older_compat)
  calls = []

  def score(candidate, live_dir, _run_dir):
    assert (Path(live_dir) / "SKILL.md").read_text() == _BASE
    content = Path(candidate).read_text()
    calls.append(content)
    (Path(live_dir) / "SKILL.md").write_text(content)
    return {"meaningful_rate": 90 if content == _BASE else candidate_rate}

  result = _run(mode, scenario, monkeypatch, score)
  assert "error" not in result
  expected = _CANDIDATE if candidate_rate == 95 else _BASE
  assert (skill_dir / "SKILL.md").read_text() == expected
  assert calls == [_BASE, _CANDIDATE]
  record = json.loads((run_dir / "evolved_score.json").read_text())
  assert record["meaningful_rate"] == max(90, candidate_rate)


@pytest.mark.parametrize("mode", ["single", "coevolve"])
@pytest.mark.parametrize("response", _UNUSABLE)
def test_unusable_candidate_result_cannot_win(
    scenario, monkeypatch, mode, response
):
  _, skill_dir, _, run_dir = scenario

  def score(candidate, live_dir, _run_dir):
    content = Path(candidate).read_text()
    (Path(live_dir) / "SKILL.md").write_text(content)
    return {"meaningful_rate": 90} if content == _BASE else response

  result = _run(mode, scenario, monkeypatch, score)
  assert "error" not in result
  assert (skill_dir / "SKILL.md").read_text() == _BASE
  record = json.loads((run_dir / "evolved_score.json").read_text())
  assert record["meaningful_rate"] == 90


@pytest.mark.parametrize("mode", ["single", "coevolve"])
@pytest.mark.parametrize("response", [None, {"status": "error"}])
def test_unmeasurable_incumbent_fails_before_generation(
    scenario, monkeypatch, mode, response
):
  module, skill_dir, _, run_dir = scenario
  generated = []
  monkeypatch.setattr(
      module, "collect_patches", lambda *a, **kw: generated.append(True)
  )

  def score(_candidate, live_dir, _run_dir):
    (Path(live_dir) / "SKILL.md").write_text("hook mutation")
    return response

  result = _run(mode, scenario, monkeypatch, score)
  assert "unmeasurable" in result["error"]
  assert generated == []
  assert (skill_dir / "SKILL.md").read_text() == _BASE
  record = json.loads((run_dir / "evolved_score.json").read_text())
  assert record["meaningful_rate"] is None
  assert record["unmeasurable"] is True


@pytest.mark.parametrize("mode", ["single", "coevolve"])
def test_hook_exception_restores_incumbent_before_retry(
    scenario, monkeypatch, mode
):
  module, skill_dir, _, run_dir = scenario
  calls = []
  generated = []

  def patches(*args, **kwargs):
    generated.append(True)
    return ["## Root Cause\n[skill]"] if len(generated) == 1 else []

  monkeypatch.setattr(module, "collect_patches", patches)

  def score(candidate, live_dir, _run_dir):
    assert (Path(live_dir) / "SKILL.md").read_text() == _BASE
    content = Path(candidate).read_text()
    calls.append(content)
    (Path(live_dir) / "SKILL.md").write_text(content)
    if content == _CANDIDATE:
      raise RuntimeError("evaluation timeout after installation")
    return {"meaningful_rate": 90}

  result = _run(mode, scenario, monkeypatch, score)
  assert (skill_dir / "SKILL.md").read_text() == _BASE
  assert (
      json.loads((run_dir / "evolved_score.json").read_text())[
          "meaningful_rate"
      ]
      == 90
  )
  if mode == "single":
    assert "evaluation timeout" in result["error"]
    assert calls == [_BASE, _CANDIDATE]
  else:
    assert "error" not in result
    assert result["attempt"] == 2
    assert calls == [_BASE, _CANDIDATE, _BASE]


@pytest.mark.parametrize("mode", ["single", "coevolve"])
def test_incumbent_exception_clears_a_previous_rounds_score(
    scenario, monkeypatch, mode
):
  _, skill_dir, _, run_dir = scenario
  record_path = run_dir / "evolved_score.json"
  record_path.write_text(json.dumps({"meaningful_rate": 99}))

  def score(_candidate, live_dir, _run_dir):
    (Path(live_dir) / "SKILL.md").write_text("failed incumbent install")
    raise RuntimeError("baseline evaluation failed")

  result = _run(mode, scenario, monkeypatch, score)
  assert "baseline evaluation failed" in result["error"]
  assert (skill_dir / "SKILL.md").read_text() == _BASE
  record = json.loads(record_path.read_text())
  assert record["meaningful_rate"] is None
  assert record["unmeasurable"] is True


def test_scoring_restores_exact_bytes_and_uses_unique_artifacts(tmp_path):
  skill_dir = tmp_path / "skill"
  skill_dir.mkdir()
  skill_path = skill_dir / "SKILL.md"
  original = b"original\r\nwith CRLF\r\n"
  skill_path.write_bytes(original)
  paths = []

  def hook(candidate, live_dir, run_dir):
    paths.append(candidate)
    (Path(live_dir) / "SKILL.md").write_bytes(b"mutated")
    if len(paths) == 1:
      raise RuntimeError("hook failed")
    return {"meaningful_rate": 95}

  first = scoring.make_score_fn(hook, str(skill_dir), str(tmp_path))
  with pytest.raises(RuntimeError, match="hook failed"):
    first("candidate one")
  assert skill_path.read_bytes() == original
  second = scoring.make_score_fn(hook, str(skill_dir), str(tmp_path))
  assert second("candidate two") == 95
  assert skill_path.read_bytes() == original
  assert paths[0] != paths[1]
  assert [Path(path).read_text() for path in paths] == [
      "candidate one",
      "candidate two",
  ]


@pytest.mark.parametrize("mode", ["single", "coevolve"])
def test_report_exclusions_make_candidate_unmeasurable(
    scenario, monkeypatch, mode
):
  _, skill_dir, _, run_dir = scenario
  report = run_dir / "excluded.json"
  report.write_text(
      json.dumps({"summary": {"excluded_error_shaped": {"count": 1}}})
  )

  def score(candidate, *_):
    if Path(candidate).read_text() == _BASE:
      return {"meaningful_rate": 90}
    return {"meaningful_rate": 99, "report_path": str(report)}

  result = _run(mode, scenario, monkeypatch, score)
  assert "error" not in result
  assert (skill_dir / "SKILL.md").read_text() == _BASE


def test_missing_scored_report_is_not_a_measured_score(tmp_path):
  assert (
      scoring.normalize_score_result(
          {"meaningful_rate": 99, "report_path": str(tmp_path / "missing.json")}
      )
      is None
  )


@pytest.mark.parametrize("returncode", [0, False])
def test_zero_returncode_preserves_measured_score(returncode):
  assert (
      scoring.normalize_score_result(
          {"returncode": returncode, "meaningful_rate": 95}
      )
      == 95
  )


@pytest.mark.parametrize("returncode", [7, -9, True, None, "7"])
def test_nonzero_or_invalid_returncode_is_unmeasurable(returncode):
  assert (
      scoring.normalize_score_result(
          {"returncode": returncode, "meaningful_rate": 99}
      )
      is None
  )


@pytest.mark.parametrize("mode", ["single", "coevolve"])
def test_omitted_artifact_directory_keeps_configured_scoring(
    scenario, monkeypatch, mode
):
  _, skill_dir, report, run_dir = scenario
  automatic_dir = run_dir / "automatic"
  calls = []

  def allocate(**kwargs):
    automatic_dir.mkdir()
    return str(automatic_dir)

  monkeypatch.setattr(coevolve.tempfile, "mkdtemp", allocate)

  def score(candidate, live_dir, artifacts):
    assert artifacts == str(automatic_dir)
    content = Path(candidate).read_text()
    calls.append(content)
    (Path(live_dir) / "SKILL.md").write_text(content)
    return {"meaningful_rate": 90 if content == _BASE else 80}

  monkeypatch.setattr(hooks, "get_hook", lambda name: (score, "test"))
  if mode == "single":
    result = tools.run_evolution(str(report), str(skill_dir), candidates=1)
    assert "error" not in result
    artifacts = result["run_dir"]
  else:
    monkeypatch.setenv("EVOLUTION_TARGET_AGENTS", "agent")
    result = coevolve.coevolve(
        str(report),
        agent_configs={"agent": {"skill_dir": str(skill_dir)}},
        candidates=1,
    )
    assert "error" not in result.evolved_agents["agent"]
    artifacts = result.output_dir
  assert artifacts == str(automatic_dir)
  assert calls == [_BASE, _CANDIDATE]
  assert (skill_dir / "SKILL.md").read_text() == _BASE
  assert (
      json.loads((Path(artifacts) / "evolved_score.json").read_text())[
          "meaningful_rate"
      ]
      == 90
  )


def test_unmeasurable_candidate_cannot_win_with_zero_margin(
    scenario, monkeypatch
):
  _, skill_dir, report, run_dir = scenario
  selected = evolve.evolve(
      str(report),
      str(skill_dir),
      candidates=1,
      candidates_dir=str(run_dir / "candidates"),
      score_fn=lambda content: 90 if content == _BASE else None,
      min_improvement=0,
  )
  assert selected == _BASE
  assert (
      json.loads((run_dir / "evolved_score.json").read_text())[
          "meaningful_rate"
      ]
      == 90
  )


def test_legacy_incumbent_argument_cannot_bypass_current_measurement(scenario):
  _, skill_dir, report, run_dir = scenario
  calls = []

  def score(content):
    calls.append(content)
    return 90 if content == _BASE else 80

  selected = evolve.evolve(
      str(report),
      str(skill_dir),
      candidates=1,
      candidates_dir=str(run_dir / "candidates"),
      score_fn=score,
      incumbent_score=20,
  )
  assert selected == _BASE
  assert calls == [_BASE, _CANDIDATE]


def test_each_coevolved_agent_measures_the_current_joint_incumbent(
    scenario, monkeypatch
):
  _, first_dir, report, run_dir = scenario
  second_dir = first_dir.parent.parent / "second" / "skill"
  second_dir.mkdir(parents=True)
  (second_dir / "SKILL.md").write_text(_BASE)
  calls = []

  def score(candidate, skill_dir, _run_dir):
    content = Path(candidate).read_text()
    calls.append((skill_dir, content))
    if skill_dir == str(first_dir):
      return {"meaningful_rate": 90 if content == _BASE else 95}
    assert (first_dir / "SKILL.md").read_text() == _CANDIDATE
    return {"meaningful_rate": 98 if content == _BASE else 96}

  monkeypatch.setattr(hooks, "get_hook", lambda _: (score, "test"))
  monkeypatch.setenv("EVOLUTION_TARGET_AGENTS", "first,second")
  result = coevolve.coevolve(
      str(report),
      agent_configs={
          "first": {"skill_dir": str(first_dir)},
          "second": {"skill_dir": str(second_dir)},
      },
      output_dir=str(run_dir),
      candidates=1,
  )
  assert all("error" not in value for value in result.evolved_agents.values())
  assert calls == [
      (str(first_dir), _BASE),
      (str(first_dir), _CANDIDATE),
      (str(second_dir), _BASE),
      (str(second_dir), _CANDIDATE),
  ]
  assert (second_dir / "SKILL.md").read_text() == _BASE
  assert (
      json.loads((run_dir / "evolved_score.json").read_text())[
          "meaningful_rate"
      ]
      == 98
  )
