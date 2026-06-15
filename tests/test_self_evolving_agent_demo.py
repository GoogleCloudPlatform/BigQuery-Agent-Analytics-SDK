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

"""Tests for pure helpers in the self-evolving agent demo."""

from __future__ import annotations

from pathlib import Path
import sys

import pytest

_DEMO_DIR = (
    Path(__file__).resolve().parents[1]
    / "examples"
    / "self_evolving_agent_demo"
)
sys.path.insert(0, str(_DEMO_DIR))

from analytics.session_metrics import require_complete_session_metrics  # noqa: E402
from analytics.session_metrics import summarize  # noqa: E402
import analyze_and_evolve  # noqa: E402
import compare_runs  # noqa: E402


def test_summarize_empty_rows_has_stable_shape():
  summary = summarize([])

  assert summary == {
      "sessions": 0,
      "avg_total_tokens": 0.0,
      "avg_input_tokens": 0.0,
      "avg_output_tokens": 0.0,
      "avg_tool_calls": 0.0,
      "avg_llm_calls": 0.0,
      "total_broad_lookup_calls": 0,
      "sessions_with_broad_lookup": 0,
      "broad_lookup_session_rate": 0.0,
      "total_tool_errors": 0,
  }


def test_require_complete_session_metrics_rejects_missing_rows():
  rows = [{"session_id": "s1", "event_count": 2, "total_tokens": 100}]

  with pytest.raises(RuntimeError, match="Only found 1/2 baseline sessions"):
    require_complete_session_metrics(rows, ["s1", "s2"], label="baseline")


def test_require_complete_session_metrics_rejects_zero_token_schema(
    monkeypatch: pytest.MonkeyPatch,
):
  monkeypatch.setenv("PROJECT_ID", "demo-project")
  rows = [{"session_id": "s1", "event_count": 2, "total_tokens": 0}]

  with pytest.raises(RuntimeError, match="Token extraction produced zero"):
    require_complete_session_metrics(rows, ["s1"], label="baseline")


def test_pct_delta_marks_zero_baseline_growth_as_not_applicable():
  assert compare_runs._pct_delta(0, 0) == 0.0
  assert compare_runs._pct_delta(0, 5) is None
  assert compare_runs._format_pct_delta(None) == "n/a"
  assert compare_runs._format_pct_delta(-0.25) == "-25.0%"


def test_observations_use_configured_thresholds():
  summary = {
      "avg_total_tokens": 1500,
      "broad_lookup_session_rate": 0.5,
      "avg_tool_calls": 3.0,
  }

  observations = analyze_and_evolve._observations(
      summary,
      token_budget=1000,
      min_broad_lookup_rate=0.5,
      max_avg_tool_calls=2.0,
  )

  assert observations == [
      "Average total tokens are above the configured session budget.",
      (
          "Most sessions used the broad basketball reference tool even though "
          "each eval case has a narrow tool path."
      ),
      "Average tool calls are high for one-question single-turn tasks.",
  ]
