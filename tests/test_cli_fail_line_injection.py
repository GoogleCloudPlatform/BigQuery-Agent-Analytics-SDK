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

"""The ``--exit-code`` gates must not print CI workflow commands.

``evaluate`` and ``evalbench-score`` run as CI gates. Their output carries
text from outside the CI job: ``session_id`` is read from the events table,
where an ADK API client chooses it; ``llm_feedback`` is judge-model text;
and the ML judge tier names a metric after any numeric key in the judge's
reply when the criterion's own key is missing. That text reaches stdout in
the report, in every ``--format``, and stderr in the FAIL lines from
``_emit_evaluate_failures``, in the judge's warning and in the API judge
tier's warning for a trace it cannot judge (the CLI configures no logging,
so ``logging.lastResort`` prints SDK warnings there). The ``Error:`` line
on stderr quotes BigQuery errors, which repeat stored event values, and
EvalBench manifest values.

The GitHub Actions runner reads a step's stdout and stderr line by line
(``\\n``, ``\\r`` and ``\\r\\n`` all end a line). It runs a line as a
workflow command when the line starts with ``::name::`` after leading
whitespace, or when it contains ``##[name]`` anywhere (actions/runner
``ActionCommand.TryParseV2`` and ``TryParse``). Those commands include
``error``, ``add-mask``, ``set-output`` and ``stop-commands``.

The payloads below use ``debug``, which only shows in debug logs: pytest
prints a failing value raw, and this suite itself runs in GitHub Actions.
"""

import ast
import asyncio
import dataclasses
from datetime import datetime
from datetime import timezone
import json
import re
from unittest.mock import AsyncMock
from unittest.mock import MagicMock
from unittest.mock import patch

from google.api_core.exceptions import BadRequest
import pytest
from typer.testing import CliRunner

from bigquery_agent_analytics import client as client_module
from bigquery_agent_analytics import evalbench
from bigquery_agent_analytics import evaluators
from bigquery_agent_analytics.cli import _format_feedback_snippet
from bigquery_agent_analytics.cli import app
from bigquery_agent_analytics.evaluators import EvaluationReport
from bigquery_agent_analytics.evaluators import LLMAsJudge
from bigquery_agent_analytics.evaluators import SessionScore
from bigquery_agent_analytics.trace import Trace

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


# The tests below run each gate on a real ``Client``. Only BigQuery is faked,
# plus the trace read and the Gemini reply for the API judge, so the judge
# tiers, the merged report and the CLI output are the shipped code.

# A BigQuery ML model reference: the ML.GENERATE_TEXT judge tier runs first.
_ML_JUDGE_ENDPOINT = "proj.ds.judge_model"

_PINNED = evalbench.EvalBenchImportSessions(
    job_id="job-1",
    import_version="v1",
    events_table="proj.ds.evalbench_agent_events",
    scores_table="proj.ds.evalbench_scores_imported",
    session_ids=("pinned-session",),
    manifest={"job_id": "job-1", "import_version": "v1"},
)

_GATE_ARGS = {
    "evaluate": [
        "evaluate",
        "--project-id=proj",
        "--dataset-id=ds",
        "--evaluator=llm-judge",
        "--exit-code",
    ],
    "evalbench-score": [
        "evalbench-score",
        "--project-id=proj",
        "--dataset-id=ds",
        "--job-id=job-1",
        "--exit-code",
    ],
}


def _invoke_judge_gate(
    monkeypatch, command, fmt, bq_client, endpoint=None, pinned=_PINNED
):
  """Run *command* with ``--format=fmt`` against a fake BigQuery client."""
  monkeypatch.setattr(
      client_module, "make_bq_client", lambda *args, **kwargs: bq_client
  )
  monkeypatch.setattr(evalbench, "import_sessions", lambda **kwargs: pinned)
  args = _GATE_ARGS[command] + [f"--format={fmt}"]
  if endpoint is not None:
    args.append(f"--endpoint={endpoint}")
  result = runner.invoke(app, args)
  assert result.exit_code == 1, result.output
  return result


def _ml_judge_client(session_id, evaluation):
  """A BigQuery client whose ML.GENERATE_TEXT judge returns one row."""
  bq_client = MagicMock()
  bq_client.query.return_value.result.return_value = [
      {"session_id": session_id, "evaluation": json.dumps(evaluation)}
  ]
  return bq_client


def _decoded_report(stdout, fmt):
  """Decode the report the way its reader would: JSON, or the table's reprs."""
  if fmt == "json":
    return json.loads(stdout)
  values = {}
  for line in stdout.splitlines():
    key, _, value = line.partition(" ")
    values[key] = value.strip()
  return {
      key: ast.literal_eval(values[key])
      for key in ("aggregate_scores", "session_scores")
  }


@pytest.mark.parametrize("fmt", ["json", "table"])
@pytest.mark.parametrize("command", ["evaluate", "evalbench-score"])
@pytest.mark.parametrize(
    "session_id, feedback",
    [
        ("s1##[debug]injected", "Wrong table joined."),
        ("ordinary-session", "Wrong ##[debug]injected table."),
    ],
    ids=["session-id", "feedback"],
)
def test_report_on_stdout_cannot_carry_a_workflow_command(
    monkeypatch, command, fmt, session_id, feedback
):
  bq_client = _ml_judge_client(
      session_id, {"correctness": 2, "justification": feedback}
  )

  result = _invoke_judge_gate(
      monkeypatch, command, fmt, bq_client, endpoint=_ML_JUDGE_ENDPOINT
  )

  assert _command_lines(result.stdout) == []
  assert _command_lines(result.stderr) == []
  # A reader decoding the JSON or the table's reprs still gets the
  # original values.
  (score,) = _decoded_report(result.stdout, fmt)["session_scores"]
  assert (score["session_id"], score["llm_feedback"]) == (session_id, feedback)


@pytest.mark.parametrize("fmt", ["json", "text", "table"])
@pytest.mark.parametrize("command", ["evaluate", "evalbench-score"])
@pytest.mark.parametrize(
    "metric_name, rendered",
    [
        ("evil\n::debug::forged", "evil\\n::debug::forged"),
        ("evil\r::debug::forged", "evil\\r::debug::forged"),
        ("evil ##[debug]x", "evil ##\\x5bdebug]x"),
    ],
    ids=["lf", "cr", "legacy-marker"],
)
def test_judge_chosen_metric_name_cannot_carry_a_workflow_command(
    monkeypatch, command, fmt, metric_name, rendered
):
  # The reply lacks the criterion's "correctness" key, so the ML judge tier
  # names a metric after every numeric key, and the missing criterion fails
  # the session.
  bq_client = _ml_judge_client(
      "ordinary-session", {metric_name: 0, "justification": "low"}
  )

  result = _invoke_judge_gate(
      monkeypatch, command, fmt, bq_client, endpoint=_ML_JUDGE_ENDPOINT
  )

  assert _command_lines(result.stderr) == []
  assert _command_lines(result.stdout) == []
  fail_lines = [line for line in result.stderr.splitlines() if "FAIL" in line]
  assert fail_lines == [
      f"  FAIL session=ordinary-session metric={rendered} score=0"
      ' feedback="low"'
  ]
  if fmt == "text":
    assert f"    {rendered}: 0.000" in result.stdout.splitlines()
  else:
    report = _decoded_report(result.stdout, fmt)
    assert report["aggregate_scores"] == {metric_name: 0.0}


@pytest.mark.parametrize("command", ["evaluate", "evalbench-score"])
@pytest.mark.parametrize(
    "metric_name, rendered",
    [
        ("::debug::forged", "\\x3a:debug::forged"),
        ("  ::debug::forged", "  \\x3a:debug::forged"),
    ],
    ids=["leading", "indented"],
)
def test_text_summary_metric_name_cannot_start_a_workflow_command(
    monkeypatch, command, metric_name, rendered
):
  # The text summary starts a line with each metric name, after spaces
  # that the runner trims.
  bq_client = _ml_judge_client(
      "ordinary-session", {metric_name: 0, "justification": "low"}
  )

  result = _invoke_judge_gate(
      monkeypatch, command, "text", bq_client, endpoint=_ML_JUDGE_ENDPOINT
  )

  assert _command_lines(result.stdout) == []
  assert _command_lines(result.stderr) == []
  assert f"    {rendered}: 0.000" in result.stdout.splitlines()


# The judge's score is a string, so float() fails and the warning quotes it.
_JUDGE_REPLY = '{"correctness": "##[debug]injected", "justification": "bad"}'
_JUDGE_WARNING = (
    "LLM judge failed: could not convert string to float:"
    " '##\\x5bdebug]injected'"
)


def _genai_client():
  """A google-genai client whose judge model returns ``_JUDGE_REPLY``."""
  genai_client = MagicMock()
  genai_client.aio.models.generate_content = AsyncMock(
      return_value=MagicMock(text=_JUDGE_REPLY)
  )
  return genai_client


@pytest.mark.parametrize("fmt", ["json", "text", "table"])
@pytest.mark.parametrize("command", ["evaluate", "evalbench-score"])
def test_judge_warning_cannot_carry_a_workflow_command(
    monkeypatch, command, fmt
):
  # The CLI configures no logging, so the warning reaches stderr through
  # logging.lastResort. Keep pytest's root capture handler from taking it.
  monkeypatch.setattr(evaluators.logger, "propagate", False)
  # Both BigQuery judge tiers fail, so the Gemini API judge parses the reply.
  bq_client = MagicMock()
  bq_client.query.side_effect = RuntimeError("judge tier unavailable")
  monkeypatch.setattr(
      client_module.Client,
      "_fetch_filtered_traces",
      lambda self, **kwargs: [Trace(trace_id="t1", session_id="judged")],
  )

  with patch("google.genai.Client", return_value=_genai_client()):
    result = _invoke_judge_gate(monkeypatch, command, fmt, bq_client)

  assert _command_lines(result.stderr) == []
  assert _command_lines(result.stdout) == []
  assert _JUDGE_WARNING in result.stderr.splitlines()


def test_judge_warning_is_escaped_when_logged(caplog):
  with patch("google.genai.Client", return_value=_genai_client()):
    score = asyncio.run(
        LLMAsJudge.correctness().evaluate_session("trace", "response")
    )

  messages = [
      record.getMessage()
      for record in caplog.records
      if record.name == evaluators.logger.name
  ]
  assert messages == [_JUDGE_WARNING]
  # The score keeps the judge's text; each output path escapes it.
  assert score.llm_feedback == (
      "correctness: could not convert string to float: '##[debug]injected'"
  )


def _llm_response_row(session_id):
  """A trace-listing row for an LLM_RESPONSE event whose response is a number.

  Judging the trace fails: ``Span.summary`` calls ``len()`` on the number.
  """
  return {
      "event_type": "LLM_RESPONSE",
      "agent": "root_agent",
      "timestamp": _NOW,
      "session_id": session_id,
      "invocation_id": "inv-1",
      "user_id": "u1",
      "trace_id": "trace-1",
      "span_id": "span-1",
      "parent_span_id": None,
      "content": {"response": 7},
      "content_parts": [],
      "attributes": {"root_agent_name": "root_agent", "experiment_id": "job-1"},
      "latency_ms": None,
      "status": "OK",
      "error_message": None,
      "is_truncated": False,
      "anchor_user_id": "u1",
      "anchor_root_agent_name": "root_agent",
      "attributes_type": "object",
  }


def _api_judge_tier_client(session_id):
  """A BigQuery client whose judge model fails, so the API judge tier runs.

  The tier lists the session's events from the same client.
  """

  def query(sql, **kwargs):
    if "ML.GENERATE_TEXT" in sql:
      raise RuntimeError("judge model unavailable")
    job = MagicMock()
    job.result.return_value = [_llm_response_row(session_id)]
    return job

  bq_client = MagicMock()
  bq_client.query.side_effect = query
  return bq_client


@pytest.mark.parametrize("fmt", ["json", "text", "table"])
@pytest.mark.parametrize("command", ["evaluate", "evalbench-score"])
@pytest.mark.parametrize(
    "session_id, rendered",
    [
        ("s1\n::debug::injected", "s1\\n::debug::injected"),
        ("s2##[debug]injected", "s2##\\x5bdebug]injected"),
    ],
    ids=["lf", "legacy-marker"],
)
def test_failed_trace_warning_cannot_carry_a_workflow_command(
    monkeypatch, command, fmt, session_id, rendered
):
  # Keep pytest's root capture handler from taking the warning, so it
  # reaches stderr through logging.lastResort as in the CLI.
  monkeypatch.setattr(client_module.logger, "propagate", False)
  bq_client = _api_judge_tier_client(session_id)

  result = _invoke_judge_gate(
      monkeypatch,
      command,
      fmt,
      bq_client,
      endpoint=_ML_JUDGE_ENDPOINT,
      pinned=dataclasses.replace(_PINNED, session_ids=(session_id,)),
  )

  assert _command_lines(result.stderr) == []
  assert _command_lines(result.stdout) == []
  assert result.stderr.splitlines() == [
      f"Evaluation failed for session {rendered}:"
      " object of type 'int' has no len()",
      "",
      "--exit-code: 1 session(s) failed (of 1 evaluated)",
      f"  FAIL session={rendered} (no per-metric detail available)",
  ]
  # One judge-model query, then one trace listing.
  assert bq_client.query.call_count == 2
  if fmt != "text":
    (score,) = _decoded_report(result.stdout, fmt)["session_scores"]
    assert score["session_id"] == session_id
    assert score["details"]["evaluation_error"] == (
        "object of type 'int' has no len()"
    )


def test_failed_trace_warning_escapes_the_error_text(caplog):
  # An evaluator's error can quote event data or a judge reply.
  error = "bad score '##[debug]injected'\n::debug::forged"

  async def evaluate_trace(trace):
    raise ValueError(error)

  client = client_module.Client(
      project_id="proj",
      dataset_id="ds",
      verify_schema=False,
      bq_client=MagicMock(),
  )
  (score,) = asyncio.run(
      client._run_trace_evaluations(
          [Trace(trace_id="t1", session_id="s1")], evaluate_trace
      )
  )

  messages = [
      record.getMessage()
      for record in caplog.records
      if record.name == client_module.logger.name
  ]
  assert messages == [
      "Evaluation failed for session s1:"
      " bad score '##\\x5bdebug]injected'\\n::debug::forged"
  ]
  # The score keeps the raw error; each output path escapes it.
  assert score.details["evaluation_error"] == error


def test_bigquery_error_line_cannot_carry_a_workflow_command(monkeypatch):
  # The code evaluators CAST latency and token fields of the stored events.
  # When a CAST fails, BigQuery's error repeats the stored value: live
  # BigQuery escapes its line breaks but keeps "##[". The client adds the
  # job location and ID on lines of their own.
  bq_client = MagicMock()
  bq_client.query.return_value.result.side_effect = BadRequest(
      "Bad double value: ##[debug]injected; reason: invalidQuery,"
      " location: query, message: Bad double value: ##[debug]injected"
      "\n\nLocation: US\nJob ID: job-1\n"
  )
  monkeypatch.setattr(
      client_module, "make_bq_client", lambda *args, **kwargs: bq_client
  )

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

  assert result.exit_code == 2
  assert _command_lines(result.stderr) == []
  assert result.stderr.splitlines() == [
      "Error: 400 Bad double value: ##\\x5bdebug]injected; reason:"
      " invalidQuery, location: query, message: Bad double value:"
      " ##\\x5bdebug]injected\\n\\nLocation: US\\nJob ID: job-1\\n"
  ]


@pytest.mark.parametrize(
    "stored, rendered",
    [
        (
            {"events_table": "proj.ds.evalbench_agent_events##[debug]x"},
            "Error: EvalBench job 'job-1' import_version 'v1' manifest"
            " events_table 'proj.ds.evalbench_agent_events##\\x5bdebug]x' is"
            " invalid: events_table must contain only ASCII letters,"
            " digits, '_' or '-'",
        ),
        (
            {
                "import_version": "v1##[debug]x",
                "events_table": "proj.ds.other_events",
            },
            "Error: EvalBench job 'job-1' import_version 'v1##\\x5bdebug]x'"
            " is published in 'proj.ds.other_events', not"
            " 'proj.ds.evalbench_agent_events'; pass the events_table it"
            " was published to",
        ),
    ],
    ids=["events-table", "import-version"],
)
def test_evalbench_error_line_cannot_carry_a_workflow_command(
    monkeypatch, stored, rendered
):
  # The scorer reads the job's latest manifest row as untrusted input, and
  # the errors that reject it quote the stored values.
  manifest = {
      "job_id": "job-1",
      "import_version": "v1",
      "events_table": "proj.ds.evalbench_agent_events",
      "scores_table": "proj.ds.evalbench_scores_imported",
      "imported_at": _NOW,
      "generation_id": "gen-1",
      **stored,
  }
  bq_client = MagicMock()
  bq_client.query.return_value.result.return_value = [manifest]
  monkeypatch.setattr(
      client_module, "make_bq_client", lambda *args, **kwargs: bq_client
  )

  result = runner.invoke(app, _GATE_ARGS["evalbench-score"])

  assert result.exit_code == 2
  assert _command_lines(result.stderr) == []
  assert result.stderr.splitlines() == [rendered]
  # Only the manifest was read.
  assert bq_client.query.call_count == 1
