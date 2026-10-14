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

"""``--exit-code`` FAIL lines must not carry CI workflow commands.

``_emit_evaluate_failures`` writes one stderr line per failing session for
CI logs (``evaluate`` and ``evalbench-score``). Two of its fields come from
outside the CI job: ``session_id`` is read from the events table, where an
ADK API client chooses it, and ``llm_feedback`` is judge-model text.

The GitHub Actions runner reads a step's stdout and stderr line by line
(``\\n``, ``\\r`` and ``\\r\\n`` all end a line). It runs a line as a
workflow command when the line starts with ``::name::`` after leading
whitespace, or when it contains ``##[name]`` anywhere (actions/runner
``ActionCommand.TryParseV2`` and ``TryParse``). Those commands include
``error``, ``add-mask``, ``set-output`` and ``stop-commands``.

The payloads below use ``debug``, which only shows in debug logs: pytest
prints a failing value raw, and this suite itself runs in GitHub Actions.
"""

from datetime import datetime
from datetime import timezone
import re
from unittest.mock import MagicMock
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from bigquery_agent_analytics.cli import _format_feedback_snippet
from bigquery_agent_analytics.cli import app
from bigquery_agent_analytics.evaluators import EvaluationReport
from bigquery_agent_analytics.evaluators import SessionScore

runner = CliRunner()

_NOW = datetime(2026, 3, 12, 10, 0, 0, tzinfo=timezone.utc)

_FAILED_LATENCY = {
    "metric_latency": {
        "observed": 7500,
        "budget": 5000,
        "threshold": 1.0,
        "score": 0.0,
        "passed": False,
    }
}


def _command_lines(text):
  """Lines of *text* that the runner could take as workflow commands.

  Stricter than the runner, which also checks the command name.
  """
  return [
      line
      for line in re.split(r"\r\n|\r|\n", text)
      if line.lstrip().startswith("::") or "##[" in line
  ]


def _invoke_gate(session_score):
  """Run ``evaluate --exit-code`` the way examples/ci_eval_pipeline.sh does."""
  report = EvaluationReport(
      dataset="test",
      evaluator_name="latency_evaluator",
      total_sessions=1,
      passed_sessions=0,
      failed_sessions=1,
      created_at=_NOW,
      session_scores=[session_score],
  )
  client = MagicMock()
  client.evaluate.return_value = report
  with patch("bigquery_agent_analytics.cli._build_client", return_value=client):
    result = runner.invoke(
        app,
        [
            "evaluate",
            "--project-id=proj",
            "--dataset-id=ds",
            "--evaluator=latency",
            "--exit-code",
            "--format=text",
        ],
    )
  assert result.exit_code == 1
  return result


@pytest.mark.parametrize(
    "session_id, rendered",
    [
        (
            "s1\n::debug::all sessions passed",
            "s1\\n::debug::all sessions passed",
        ),
        ("s2\r::debug::x", "s2\\r::debug::x"),
        ("s3\r\n  ::debug::x", "s3\\r\\n  ::debug::x"),
        ("s4 ##[debug]x", "s4 ##\\x5bdebug]x"),
    ],
    ids=["lf", "cr", "crlf-indented", "legacy-marker"],
)
def test_session_id_cannot_start_or_carry_a_workflow_command(
    session_id, rendered
):
  result = _invoke_gate(
      SessionScore(
          session_id=session_id,
          scores={"latency": 0.0},
          passed=False,
          details=_FAILED_LATENCY,
      )
  )

  assert _command_lines(result.stderr) == []
  assert _command_lines(result.stdout) == []
  # The session is still named, escaped, on its own FAIL line.
  fail_lines = [line for line in result.stderr.splitlines() if "FAIL" in line]
  assert fail_lines == [
      f"  FAIL session={rendered} metric=latency observed=7500"
      " budget=5000 score=0 threshold=1"
  ]


def test_fallback_fail_line_escapes_the_session_id():
  # Every metric claims passed while the session failed, so only the
  # fallback line names the session.
  result = _invoke_gate(
      SessionScore(
          session_id="s5\n::debug::x",
          scores={"latency": 0.0},
          passed=False,
          details={"metric_latency": {"passed": True}},
      )
  )

  assert _command_lines(result.stderr) == []
  assert (
      "  FAIL session=s5\\n::debug::x (no per-metric detail available)"
      in result.stderr.splitlines()
  )


def test_control_and_separator_characters_are_escaped():
  # ANSI escapes and Unicode line separators would rewrite or split the
  # line in a terminal or log viewer.
  result = _invoke_gate(
      SessionScore(
          session_id="s6\x1b[2K\u2028\x85\x00",
          scores={"latency": 0.0},
          passed=False,
          details=_FAILED_LATENCY,
      )
  )

  assert "FAIL session=s6\\x1b[2K\\u2028\\x85\\x00 metric=latency" in (
      result.stderr
  )
  for line in result.stderr.splitlines():
    assert line.isprintable(), line


def test_distinct_session_ids_render_distinctly():
  # A literal backslash-n must not read like an escaped line break.
  fail_lines = {}
  for session_id in ("s7\n", "s7\\n"):
    result = _invoke_gate(
        SessionScore(
            session_id=session_id,
            scores={"latency": 0.0},
            passed=False,
            details=_FAILED_LATENCY,
        )
    )
    fail_lines[session_id] = [
        line for line in result.stderr.splitlines() if "FAIL" in line
    ]

  tail = " metric=latency observed=7500 budget=5000 score=0 threshold=1"
  assert fail_lines["s7\n"] == ["  FAIL session=s7\\n" + tail]
  assert fail_lines["s7\\n"] == ["  FAIL session=s7\\\\n" + tail]


def test_ordinary_session_ids_are_unchanged():
  session_id = "sess-42_üñí-セッション:a/b#[1]"
  result = _invoke_gate(
      SessionScore(
          session_id=session_id,
          scores={"latency": 0.0},
          passed=False,
          details=_FAILED_LATENCY,
      )
  )

  assert f"FAIL session={session_id} metric=latency" in result.stderr


def test_feedback_snippet_cannot_carry_a_workflow_command():
  out = _format_feedback_snippet(
      "Low score. ##[debug]x ::debug::y \x1b[31m red \u202e"
  )

  assert "##[" not in out
  assert out.isprintable()
  assert out == "Low score. ##\\x5bdebug]x ::debug::y \\x1b[31m red \\u202e"


def test_feedback_snippet_truncation_keeps_escapes_whole():
  out = _format_feedback_snippet("a" * 8 + "##[" + "b" * 20, max_chars=14)

  # "##\x5b" is four characters: it either fits whole or is dropped.
  assert out == "aaaaaaaa##\u2026"
  assert "##[" not in _format_feedback_snippet("##[" * 100)


def test_ordinary_feedback_is_unchanged():
  feedback = "Missed the #2 step [tool: search] in the user's request."
  assert _format_feedback_snippet(feedback) == feedback
