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

"""Unit tests for the skill-evolution lab's compare_runs helpers.

Focus: exact-set gate semantics (review P1 #2) -- sessions missing from a
scored report count as failures, so a run can never improve its rate by
erroring out on hard cases.
"""

import importlib.util
import json
import os

# Load by explicit path under a unique module name: another example
# (self_evolving_agent_demo) ships its own compare_runs.py, and a bare
# `import compare_runs` would collide with it via sys.modules.
_PATH = os.path.join(
    os.path.dirname(__file__),
    "..",
    "examples",
    "skill_evolution_lab",
    "compare_runs.py",
)
_spec = importlib.util.spec_from_file_location("skill_lab_compare_runs", _PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

_load_expected_ids = _mod._load_expected_ids
_summarize = _mod._summarize


def _ok_session(sid, **extra):
  s = {
      "session_id": sid,
      "metrics": {"response_usefulness": {"category": "meaningful"}},
      "golden_eval": {"matched": True},
  }
  s.update(extra)
  return s


def _declined_session(sid):
  return {
      "session_id": sid,
      "metrics": {"response_usefulness": {"category": "declined"}},
  }


def test_summarize_without_expected_is_unchanged():
  report = {"sessions": [_ok_session("t01"), _declined_session("oos_a")]}
  s = _summarize(report)
  assert s["overall"] == {"rate": 100.0, "correct": 2, "total": 2}
  assert s["missing"] == 0


def test_missing_sessions_count_as_failures():
  # Expected set has 4 questions; the report only contains 2 (the other two
  # errored out and never reached scoring). The denominator must stay 4.
  report = {"sessions": [_ok_session("t01"), _ok_session("t02")]}
  expected = ["t01", "t02", "t03", "corr_x"]
  s = _summarize(report, expected)
  assert s["overall"]["total"] == 4
  assert s["overall"]["correct"] == 2
  assert s["missing"] == 2
  # The missing sessions land in their prefix slices and fail there.
  assert s["corrections"]["total"] == 1
  assert s["corrections"]["correct"] == 0
  assert s["single_turn"]["total"] == 3


def test_missing_oos_session_fails_the_oos_slice():
  report = {"sessions": [_declined_session("oos_a")]}
  s = _summarize(report, ["oos_a", "oos_b"])
  assert s["out_of_scope"] == {"rate": 50.0, "correct": 1, "total": 2}
  assert s["missing"] == 1


def test_stray_sessions_outside_expected_set_are_excluded():
  # A leaked session from another run must never inflate the rate.
  report = {"sessions": [_ok_session("t01"), _ok_session("stray")]}
  s = _summarize(report, ["t01", "t02"])
  assert s["overall"]["total"] == 2
  assert s["overall"]["correct"] == 1


def test_dropping_a_hard_case_lowers_the_rate():
  # The survivor-bias scenario from the review: a candidate that times out on
  # a hard question must score WORSE, never better.
  full = {
      "sessions": [
          _ok_session("t01"),
          {
              "session_id": "t02",
              "metrics": {"response_usefulness": {"category": "unhelpful"}},
          },
      ]
  }
  dropped = {"sessions": [_ok_session("t01")]}
  expected = ["t01", "t02"]
  assert (
      _summarize(dropped, expected)["overall"]["rate"]
      <= _summarize(full, expected)["overall"]["rate"]
  )
  assert _summarize(dropped, expected)["overall"]["total"] == 2


def test_load_expected_ids(tmp_path):
  qfile = tmp_path / "questions.json"
  qfile.write_text(
      json.dumps(
          {
              "questions": [
                  {"id": "a", "question": "?"},
                  {"id": "b", "turns": ["?", "!"]},
              ]
          }
      )
  )
  assert _load_expected_ids([str(qfile)]) == ["a", "b"]
  assert _load_expected_ids(None) == []
