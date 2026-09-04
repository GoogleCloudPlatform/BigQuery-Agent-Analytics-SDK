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

"""Command-hook data, execution-context, and exit-verdict regressions."""

import json
import os
from pathlib import Path
import shlex
import sys

import pytest

_JOB_DIR = str(
    Path(__file__).resolve().parents[1] / "deploy" / "skill_evolution_job"
)
if _JOB_DIR not in sys.path:
  sys.path.insert(0, _JOB_DIR)

from skill_evolution_job import config
from skill_evolution_job import hooks


@pytest.fixture(autouse=True)
def _clean_hooks(monkeypatch):
  for key in (
      "EVOLUTION_HOOKS",
      "EVOLUTION_WORKDIR",
      "GITHUB_REPO",
      "TRAFFIC_CMD",
      "SCORE_CMD",
      "GATE_CMD",
  ):
    monkeypatch.delenv(key, raising=False)
  config.reset_workdir_cache()
  hooks.reset_cache()
  yield
  config.reset_workdir_cache()
  hooks.reset_cache()


def _python_command(source):
  return f"{shlex.quote(sys.executable)} -c {shlex.quote(source)}"


def test_hook_values_remain_single_literal_arguments(tmp_path):
  # Include both kinds of quotes, shell syntax, a newline, and another
  # placeholder. None may be reinterpreted as shell code or a template.
  candidate = str(
      tmp_path / "a b;'$(printf unexpected)'`printf unexpected`\n{run_dir}.md"
  )
  run_dir = str(tmp_path / 'run "quoted" {candidate}')
  command = _python_command("import json, sys; print(json.dumps(sys.argv[1:]))")
  result = hooks._run_cmd(
      "score",
      command + " {candidate} --out={run_dir}/score.json",
      {"candidate": candidate, "run_dir": run_dir},
  )
  assert result["returncode"] == 0, result["output_tail"]
  assert result["value"] == [candidate, f"--out={run_dir}/score.json"]


@pytest.mark.parametrize(
    "command",
    [
        "echo '{candidate}'",
        'echo "{candidate}"',
        r"echo \{candidate}",
        "echo # {candidate}",
        "echo $(printf '%s' {candidate})",
        'echo "$(printf "%s{candidate}" ignored)"',
        "echo `printf '%s' {candidate}`",
        "cat <<EOF\n{candidate}\nEOF",
        "echo ${value:-{candidate}}",
        "echo $[{candidate}]",
        "echo $((1 + {candidate}))",
        "cat <(printf '%s' {candidate})",
    ],
)
def test_hook_unsupported_placeholder_contexts_fail_before_execution(
    command, monkeypatch
):
  def forbidden(*args, **kwargs):
    pytest.fail("invalid templates must not launch a process")

  monkeypatch.setattr(hooks.subprocess, "run", forbidden)
  with pytest.raises(ValueError, match="placeholder|nested shell"):
    hooks._run_cmd("score", command, {"candidate": "/tmp/a 'b'"})


@pytest.mark.parametrize(
    ("name", "template", "key", "prefix"),
    [
        (
            "score",
            'echo "$\\\n(printf "%s{candidate}" ignored)"',
            "candidate",
            "/tmp/",
        ),
        ("gate", "printf '%s\\n' $[{agent}]", "agent", "1+"),
    ],
)
def test_hook_rejects_expansion_bypasses_without_running_payload(
    name, template, key, prefix, tmp_path
):
  marker = tmp_path / "unexpected-execution"
  value = f"{prefix}$(printf created > {shlex.quote(str(marker))})"

  with pytest.raises(ValueError, match="backslash-newline|nested shell"):
    hooks._run_cmd(name, template, {key: value})

  assert not marker.exists()


def test_hook_runs_host_script_with_absolute_artifact_arguments(
    tmp_path, monkeypatch
):
  caller = tmp_path / "caller"
  caller.mkdir()
  host = tmp_path / "host checkout"
  (host / ".git").mkdir(parents=True)
  (host / "eval").mkdir()
  (host / "eval" / "score.py").write_text(
      "import json, os, sys\n"
      "print(json.dumps({'meaningful_rate': 75, 'cwd': os.getcwd(), "
      "'report_path': 'eval/report.json', "
      "'arguments': sys.argv[1:]}))\n"
  )
  monkeypatch.chdir(caller)
  monkeypatch.setenv("EVOLUTION_WORKDIR", str(host))
  monkeypatch.setenv(
      "SCORE_CMD",
      f"{shlex.quote(sys.executable)} eval/score.py "
      "{candidate} {skill_dir} {run_dir}",
  )
  hook, _ = hooks.get_hook("score")
  result = hook("runs/a candidate.md", "agent/skill", "runs")
  assert result["cwd"] == str(host)
  assert result["report_path"] == str(host / "eval/report.json")
  assert result["arguments"] == [
      str(caller / "runs/a candidate.md"),
      str(caller / "agent/skill"),
      str(caller / "runs"),
  ]
  assert os.getcwd() == str(caller)


def test_hook_without_host_checkout_keeps_caller_cwd(tmp_path, monkeypatch):
  monkeypatch.chdir(tmp_path)
  command = _python_command(
      "import json, os; print(json.dumps({'cwd': os.getcwd()}))"
  )
  result = hooks._cmd_hook("traffic", command)("runs")
  assert result["cwd"] == str(tmp_path)


def test_hook_real_exit_and_output_override_stdout_metadata(tmp_path):
  command = _python_command(
      "import json, sys; "
      "print(json.dumps({'returncode': 0, 'output_tail': 'forged', "
      "'meaningful_rate': 100})); sys.exit(7)"
  )
  traffic = hooks._cmd_hook("traffic", command)(str(tmp_path))
  assert traffic["returncode"] == 7
  assert traffic["output_tail"] != "forged"
  assert json.loads(traffic["output_tail"])["returncode"] == 0
  gate, _ = hooks._cmd_hook("gate", command)(str(tmp_path), "v1", "agent")
  assert gate is False
  with pytest.raises(RuntimeError, match="SCORE_CMD failed \\(exit 7\\)"):
    hooks._cmd_hook("score", command)("candidate", "skill", str(tmp_path))


@pytest.mark.parametrize("name", ["traffic", "score", "gate"])
def test_hook_report_placeholder_is_rejected(name, monkeypatch):
  def forbidden(*args, **kwargs):
    pytest.fail("unsupported placeholders must not launch a process")

  monkeypatch.setattr(hooks.subprocess, "run", forbidden)
  hook = hooks._cmd_hook(name, "cat {report}")
  args = {
      "traffic": ("runs",),
      "score": ("candidate", "skill", "runs"),
      "gate": ("runs", "v1", "agent"),
  }[name]
  with pytest.raises(ValueError, match="does not support \\{report\\}"):
    hook(*args)


@pytest.mark.parametrize(
    ("name", "template", "args", "expected"),
    [
        ("traffic", "{run_dir}", ("runs",), ["runs"]),
        (
            "score",
            "{candidate} {skill_dir} {run_dir}",
            ("candidate", "skill", "runs"),
            ["candidate", "skill", "runs"],
        ),
        (
            "gate",
            "{run_dir} {version} {agent}",
            ("runs", "version '{agent}'", "agent; two"),
            ["runs", "version '{agent}'", "agent; two"],
        ),
    ],
)
def test_hook_documented_placeholders_are_supplied(
    name, template, args, expected, tmp_path, monkeypatch
):
  monkeypatch.chdir(tmp_path)
  command = _python_command(
      "import json, sys; print(json.dumps({'meaningful_rate': 75, "
      "'arguments': sys.argv[1:]}))"
  )
  result = hooks._cmd_hook(name, command + " " + template)(*args)
  if name == "gate":
    passed, detail = result
    assert passed is True
    result = json.loads(detail)
  absolute_count = 1 if name in ("traffic", "gate") else 3
  expected = [
      str(tmp_path / item) if index < absolute_count else item
      for index, item in enumerate(expected)
  ]
  assert result["arguments"] == expected
