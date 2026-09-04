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

"""Offline regressions for report errors, round limits and failure routing."""

import asyncio
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_JOB_DIR = str(_REPO_ROOT / "deploy" / "skill_evolution_job")
if _JOB_DIR not in sys.path:
  sys.path.insert(0, _JOB_DIR)

from skill_evolution_job import bottleneck
from skill_evolution_job import engine
from skill_evolution_job import main as job_main
from skill_evolution_job import registry
from skill_evolution_job import tools


@pytest.fixture(autouse=True)
def _offline_job(monkeypatch):
  # Register restoration even when the original variable is absent: main
  # binds the cap directly in os.environ during every asynchronous run.
  monkeypatch.setenv("EVOLUTION_MAX_ROUNDS", "")
  monkeypatch.delenv("EVOLUTION_MAX_ROUNDS")
  monkeypatch.setenv("QUALITY_SOURCE", "bigquery")
  monkeypatch.setenv("MIN_SESSIONS", "20")
  monkeypatch.setenv("SDK_SCRIPTS_DIR", str(_REPO_ROOT / "scripts"))
  monkeypatch.setattr(tools, "_rounds_run", {})
  monkeypatch.setattr(
      registry,
      "get_registry",
      lambda: SimpleNamespace(agents={}, app_name_for=lambda: None),
  )
  monkeypatch.setattr(
      job_main.hooks, "get_hook", lambda name: (None, "not configured")
  )
  engine.reset_cache()
  yield
  engine.reset_cache()


@pytest.mark.parametrize("traffic_configured", [False, True])
def test_report_error_stops_before_traffic(
    tmp_path, monkeypatch, traffic_configured
):
  report = Mock(return_value={"error": "403 Permission denied"})
  traffic = Mock(return_value={"returncode": 0})
  monkeypatch.setattr(tools, "run_quality_report", report)
  if traffic_configured:
    monkeypatch.setattr(
        job_main.hooks, "get_hook", lambda name: (traffic, "test hook")
    )

  result = asyncio.run(job_main.run_evolution_agent(run_dir=str(tmp_path)))

  assert result.startswith("ERROR:")
  assert "403 Permission denied" in result
  report.assert_called_once()
  traffic.assert_not_called()


def test_report_error_after_traffic_keeps_dependency_failure(
    tmp_path, monkeypatch
):
  report = Mock(
      side_effect=[{"total_sessions": 3}, {"error": "report timed out"}]
  )
  traffic = Mock(return_value={"returncode": 0})
  monkeypatch.setattr(tools, "run_quality_report", report)
  monkeypatch.setattr(
      job_main.hooks, "get_hook", lambda name: (traffic, "test hook")
  )

  result = asyncio.run(job_main.run_evolution_agent(run_dir=str(tmp_path)))

  assert result.startswith("ERROR:")
  assert "report timed out" in result
  assert "fewer than MIN_SESSIONS" not in result
  assert report.call_count == 2
  traffic.assert_called_once_with(str(tmp_path))


def test_successful_small_report_is_still_no_work(tmp_path, monkeypatch):
  monkeypatch.setattr(
      tools, "run_quality_report", lambda **kwargs: {"total_sessions": 3}
  )

  result = asyncio.run(job_main.run_evolution_agent(run_dir=str(tmp_path)))

  assert result.startswith("NOTHING TO DO:")
  assert "3 < 20 MIN_SESSIONS" in result


@pytest.mark.parametrize("after_traffic", [False, True])
def test_report_error_exits_job_nonzero(
    tmp_path, monkeypatch, after_traffic, capsys
):
  results = [{"error": "403 Permission denied"}]
  if after_traffic:
    results.insert(0, {"total_sessions": 3})
    monkeypatch.setattr(
        job_main.hooks,
        "get_hook",
        lambda name: (lambda run_dir: {"returncode": 0}, "test hook"),
    )
  monkeypatch.setattr(tools, "run_quality_report", Mock(side_effect=results))
  monkeypatch.setattr(
      sys, "argv", ["job", "--full-loop", "--run-dir", str(tmp_path)]
  )

  with pytest.raises(SystemExit) as raised:
    job_main.main()

  assert raised.value.code == 1
  assert "403 Permission denied" in capsys.readouterr().out


@pytest.mark.parametrize(
    "env_value,rounds,expected",
    [
        (None, None, 2),
        ("1", None, 1),
        ("0", None, 0),
        ("2", 1, 1),
        ("2", 0, 0),
    ],
)
def test_direct_run_binds_round_cap_before_preflight(
    tmp_path, monkeypatch, env_value, rounds, expected
):
  if env_value is not None:
    monkeypatch.setenv("EVOLUTION_MAX_ROUNDS", env_value)

  def report(**kwargs):
    assert os.environ["EVOLUTION_MAX_ROUNDS"] == str(expected)
    return {"total_sessions": 0}

  monkeypatch.setattr(tools, "run_quality_report", report)
  result = asyncio.run(
      job_main.run_evolution_agent(run_dir=str(tmp_path), rounds=rounds)
  )
  assert result.startswith("NOTHING TO DO:")
  for _ in range(expected):
    assert tools._round_guard("test-agent") is None
  assert tools._round_guard("test-agent")["status"] == "refused"


@pytest.mark.parametrize("rounds", [-1, 3, True, 1.5, "1"])
def test_invalid_direct_round_limit_stops_before_preflight(
    tmp_path, monkeypatch, rounds
):
  report = Mock()
  monkeypatch.setattr(tools, "run_quality_report", report)

  result = asyncio.run(
      job_main.run_evolution_agent(run_dir=str(tmp_path), rounds=rounds)
  )

  assert result.startswith("ERROR:")
  assert "EVOLUTION_MAX_ROUNDS" in result
  report.assert_not_called()


@pytest.mark.parametrize("value", ["", "abc", "1.5", "-1", "3"])
def test_invalid_environment_round_limit_stops_before_preflight(
    tmp_path, monkeypatch, value
):
  monkeypatch.setenv("EVOLUTION_MAX_ROUNDS", value)
  report = Mock()
  monkeypatch.setattr(tools, "run_quality_report", report)

  result = asyncio.run(job_main.run_evolution_agent(run_dir=str(tmp_path)))

  assert result.startswith("ERROR:")
  report.assert_not_called()


def test_cli_rejects_rounds_above_hard_cap(monkeypatch):
  report = Mock()
  monkeypatch.setattr(tools, "run_quality_report", report)
  monkeypatch.setattr(sys, "argv", ["job", "--full-loop", "--rounds", "3"])

  with pytest.raises(SystemExit) as raised:
    job_main.main()

  assert raised.value.code == 2
  report.assert_not_called()


def test_cli_zero_rounds_disables_evolution(tmp_path, monkeypatch):
  monkeypatch.setattr(
      tools, "run_quality_report", lambda **kwargs: {"total_sessions": 0}
  )
  monkeypatch.setattr(
      sys,
      "argv",
      ["job", "--full-loop", "--run-dir", str(tmp_path), "--rounds", "0"],
  )

  job_main.main()

  assert os.environ["EVOLUTION_MAX_ROUNDS"] == "0"
  assert tools._round_guard("test-agent")["status"] == "refused"


def test_independent_agent_runs_have_separate_round_budgets(monkeypatch):
  attempts = []

  class FakeRunner:

    def __init__(self, **kwargs):
      pass

    async def run_async(self, **kwargs):
      attempts.append([tools._round_guard("test-agent") for _ in range(3)])
      yield SimpleNamespace(
          author="assistant",
          partial=False,
          content=SimpleNamespace(parts=[SimpleNamespace(text="done")]),
      )

  monkeypatch.setitem(
      sys.modules,
      "google.adk.runners",
      SimpleNamespace(Runner=FakeRunner),
  )
  monkeypatch.setitem(
      sys.modules,
      "google.adk.sessions.in_memory_session_service",
      SimpleNamespace(InMemorySessionService=lambda: object()),
  )
  monkeypatch.setitem(
      sys.modules, "skill_evolution_job.agent", SimpleNamespace(app=object())
  )

  for _ in range(2):
    assert (
        asyncio.run(job_main.run_evolution_agent(report_path="report.json"))
        == "done"
    )

  assert len(attempts) == 2
  for first, second, third in attempts:
    assert first is None
    assert second is None
    assert third["status"] == "refused"


@pytest.mark.parametrize(
    "evidence_key", ["sub_trajectories", "execution_sub_trajectories"]
)
@pytest.mark.parametrize("category", ["meaningful", "declined"])
def test_bottleneck_classifies_engine_parroted_failures(
    monkeypatch, evidence_key, category
):
  parroted = {
      "question": "corrected question",
      "metrics": {"response_usefulness": {"category": category}},
      evidence_key: [{"outcome": "parroted"}],
  }
  success = {
      "question": "successful question",
      "metrics": {"response_usefulness": {"category": "meaningful"}},
  }
  report = {"sessions": [success, parroted]}
  _, expected_failures = engine.load_engine().partition_trajectories(report)
  assert expected_failures == [parroted]
  classifier = Mock(
      return_value={
          "classification": "SKILL_FAILURE",
          "agent_responsible": "support",
          "confidence": 1.0,
      }
  )
  monkeypatch.setattr(bottleneck, "classify_failure", classifier)

  result = bottleneck.detect_bottleneck(
      report, client=object(), agents={"support": {"label": "Support"}}
  )

  assert result.total_failures == len(expected_failures)
  assert result.recommendation == "support"
  assert len(result.skill_failures) == 1
  classifier.assert_called_once()
  assert classifier.call_args.args[2] is parroted
