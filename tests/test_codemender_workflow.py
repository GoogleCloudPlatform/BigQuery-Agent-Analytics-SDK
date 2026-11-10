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

"""Tests for the CodeMender pull request scan and pre-commit hook.

`.github/workflows/codemender.yml` scans each pull request into main with the
CodeMender CLI, uploads the SARIF report to code scanning, and fails on a
vulnerability with a failing severity. Until the Workload Identity Federation
secrets are set, it skips the scan and passes; it doesn't run for pull
requests from forks or Dependabot, whose workflows can't read the secrets.
`scripts/codemender_pre_commit.sh` scans the staged changes before a commit.

The first tests read the workflow. The others run its step scripts the way
Actions runs `shell: bash`, and the hook the way Git runs it, in temporary git
repositories with a stub `cm` that records how it was called: nothing here
reaches the network or Google Cloud.
"""

import json
import os
import pathlib
import re
import shlex
import shutil
import subprocess
import sys
import zipfile

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "codemender.yml"
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
HOOK = ROOT / "scripts" / "codemender_pre_commit.sh"
SCRIPTS_README = ROOT / "scripts" / "README.md"

# How Actions runs a step with `shell: bash`; the script file goes last.
BASH = ("bash", "--noprofile", "--norc", "-eo", "pipefail")
DASH = shutil.which("dash")

REPOSITORY = "GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK"
CODE_SCANNING = f"https://github.com/{REPOSITORY}/security/code-scanning"
SETUP_URL = (
    "https://docs.cloud.google.com/gemini-enterprise-agent-platform/agents"
    "/codemender/integrate-with-cicd#github-wif"
)

SECRETS = ("GCP_PROJECT_ID", "GCP_WIF_PROVIDER", "GCP_WIF_SERVICE_ACCOUNT")
# Values in the shape of real ones, which no step may print.
CONFIGURED = {
    "GCP_PROJECT_ID": "bqaa-ci-project",
    "GCP_WIF_PROVIDER": (
        "projects/123456789/locations/global/workloadIdentityPools/github"
        "/providers/bqaa-sdk"
    ),
    "GCP_WIF_SERVICE_ACCOUNT": (
        "codemender-ci@bqaa-ci-project.iam.gserviceaccount.com"
    ),
}
NOTICE = (
    "::notice::CodeMender WIF secrets (GCP_PROJECT_ID, GCP_WIF_PROVIDER,"
    " GCP_WIF_SERVICE_ACCOUNT) are not configured; skipping CodeMender scan."
)
# A secret that isn't set reads as "" in a step, and the auth action trims
# its inputs, so one of only whitespace is not set either. "unset" drops the
# variable, which a step's `env:` never does, to cover the script on its own.
NOT_SET = {
    "unset": None,
    "empty": "",
    "space": " ",
    "tab": "\t",
    "newline": "\n",
    "whitespace": " \t\r\n ",
}

# `cm find` as the scan step runs it for a pull request into main.
SCAN = [
    "find",
    ".",
    "--diff=origin/main",
    "--diff-workers=16",
    "--fail-on=CRITICAL,HIGH",
    "--format=sarif",
    "--output=codemender.sarif",
    "--sandbox=false",
    "--yes",
    "--bypass-warning",
]
# `cm find` as the pre-commit hook runs it by default.
HOOK_SCAN = [
    "find",
    ".",
    "--diff",
    "--staged",
    "--fail-on=CRITICAL,HIGH",
    "--yes",
    "--bypass-warning",
]

# What the stub cm finds: a vulnerability in the changed code, and one in code
# that the change didn't touch. Neither may reach the job summary or the log,
# which anyone can read in a public repository.
FINDINGS = (
    "Command injection in the new export helper",
    "[LEGACY / UNTOUCHED] SQL injection in an unchanged query builder",
)

# A stand-in for the CodeMender CLI. It appends each call (its arguments and
# working directory) to $CM_STUB_LOG. `cm init` exits $CM_STUB_INIT_STATUS;
# `cm find` follows $CM_STUB_FIND: "clean" and "finding" write a SARIF report
# to --output= and exit 0 and 1, as cm does for a vulnerability with a failing
# severity. "error" writes no report and exits $CM_STUB_STATUS (default 1, the
# status of a finding too); "late-error" writes the report, then exits
# $CM_STUB_STATUS (default 2). install_cm() fills in __FINDINGS__.
CM_STUB = """\
import json
import os
import sys

FINDINGS = __FINDINGS__
args = sys.argv[1:]
with open(os.environ["CM_STUB_LOG"], "a", encoding="utf-8") as log:
  log.write(json.dumps({"args": args, "cwd": os.getcwd()}) + "\\n")
command = args[0] if args else ""
if command == "init":
  sys.exit(int(os.environ.get("CM_STUB_INIT_STATUS", "0")))
if command == "report":
  print("\\n".join(FINDINGS))
  sys.exit(0)
if command != "find":
  sys.exit(64)
mode = os.environ.get("CM_STUB_FIND", "clean")
if mode == "error":
  print("cm: the scan could not finish", file=sys.stderr)
  sys.exit(int(os.environ.get("CM_STUB_STATUS", "1")))
results = [] if mode == "clean" else [
    {"ruleId": "codemender", "level": "error", "message": {"text": title}}
    for title in FINDINGS
]
for arg in args:
  if arg.startswith("--output="):
    with open(arg.split("=", 1)[1], "w", encoding="utf-8") as report:
      json.dump({"version": "2.1.0", "runs": [{"results": results}]}, report)
if mode == "late-error":
  print("cm: the scan failed after writing its report", file=sys.stderr)
  sys.exit(int(os.environ.get("CM_STUB_STATUS", "2")))
sys.exit(1 if mode == "finding" else 0)
"""

_PINNED = re.compile(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}")
_EXPRESSION = re.compile(r"\$\{\{\s*(.*?)\s*\}\}")
_TOKEN = re.compile(
    r"\s*(?:(?P<operator>==|!=|&&|\|\||[()])"
    r"|'(?P<string>(?:[^']|'')*)'"
    r"|(?P<name>[A-Za-z_][\w.-]*))"
)


def _workflow():
  return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _triggers():
  """Maps each event in codemender.yml's `on:` to its filters ({} for none)."""
  workflow = _workflow()
  # YAML 1.1, which PyYAML reads, loads the bare key `on` as True.
  on = workflow["on"] if "on" in workflow else workflow[True]
  if isinstance(on, str):
    on = [on]
  if isinstance(on, list):
    on = dict.fromkeys(on)
  return {event: filters or {} for event, filters in on.items()}


def _job():
  return _workflow()["jobs"]["scan"]


def _step(key):
  """The scan job's step whose `id` or `name` is `key`."""
  for step in _job()["steps"]:
    if key in (step.get("id"), step.get("name")):
      return step
  raise AssertionError(f"codemender.yml has no step {key!r}")


def _condition(value):
  """An `if:` without the `${{ }}` around it, with its spaces collapsed."""
  text = str(value).strip()
  match = re.fullmatch(r"\$\{\{(.*)\}\}", text, flags=re.DOTALL)
  return " ".join((match[1] if match else text).split())


def _evaluate(condition, context):
  """Evaluates an `if:` against `context` the way Actions does.

  This covers what the job's condition uses: parentheses, `||`, `&&`, `==`,
  `!=`, quoted strings and context properties, where a missing property is
  null. Actions compares strings ignoring case, and null equals only null.
  Anything else, such as a function or `!`, fails the test instead of being
  guessed at.
  """
  text = _condition(condition)
  tokens = []
  position = 0
  while text[position:].strip():
    match = _TOKEN.match(text, position)
    assert match, f"cannot evaluate {text[position:]!r} in {text!r}"
    position = match.end()
    if match["operator"]:
      tokens.append(match["operator"])
    elif match["string"] is not None:
      tokens.append(("value", match["string"].replace("''", "'")))
    else:
      value = context
      for part in match["name"].split("."):
        value = value.get(part) if isinstance(value, dict) else None
      tokens.append(("value", value))
  index = 0

  def peek():
    return tokens[index] if index < len(tokens) else None

  def take():
    nonlocal index
    index += 1
    return tokens[index - 1]

  def disjunction():
    value = conjunction()
    while peek() == "||":
      take()
      right = conjunction()
      value = value or right
    return value

  def conjunction():
    value = comparison()
    while peek() == "&&":
      take()
      right = comparison()
      value = value and right
    return value

  def comparison():
    left = operand()
    if peek() not in ("==", "!="):
      return left
    operator = take()
    right = operand()
    if isinstance(left, str) and isinstance(right, str):
      equal = left.casefold() == right.casefold()
    else:
      equal = left == right
    return equal if operator == "==" else not equal

  def operand():
    token = take()
    if token == "(":
      value = disjunction()
      assert take() == ")", f"unbalanced parentheses in {text!r}"
      return value
    assert isinstance(token, tuple), f"unexpected {token!r} in {text!r}"
    return token[1]

  result = disjunction()
  assert index == len(tokens), f"cannot evaluate {text!r}"
  return bool(result)


def _github(event, *, head=REPOSITORY, actor="octocat"):
  """The `github` context of a run, as the job's `if:` reads it."""
  github = {
      "event_name": event,
      "repository": REPOSITORY,
      "actor": actor,
      "event": {},
  }
  if event == "pull_request":
    github["event"]["pull_request"] = {"head": {"repo": {"full_name": head}}}
  return {"github": github}


def _system_path():
  """The directories on PATH, without any that holds a real `cm`."""
  return [
      directory
      for directory in os.environ.get("PATH", os.defpath).split(os.pathsep)
      if directory and not os.access(os.path.join(directory, "cm"), os.X_OK)
  ]


def _install_command():
  """The command that scripts/README.md gives to install the hook."""
  commands = [
      line.strip()
      for line in SCRIPTS_README.read_text(encoding="utf-8").splitlines()
      if line.strip().startswith("ln -s ") and "codemender_pre_commit" in line
  ]
  assert len(commands) == 1, commands
  return commands[0]


def test_the_workflow_runs_on_pull_requests_into_main_and_on_demand():
  """Never pull_request_target, which gives fork pull requests the secrets."""
  assert _triggers() == {
      "pull_request": {"branches": ["main"]},
      "workflow_dispatch": {},
  }


def test_the_workflow_grants_only_what_the_scan_needs():
  workflow = _workflow()

  assert workflow["permissions"] == {}, "the default token gets no permissions"
  for job_id, job in workflow["jobs"].items():
    assert "permissions" in job, f"{job_id} must ask for its own permissions"
  assert _job()["permissions"] == {
      "contents": "read",
      "id-token": "write",
      "security-events": "write",
      "actions": "read",
  }


def test_every_action_is_pinned_to_a_commit_with_its_version_noted():
  """Only these actions run in a job that can get Google Cloud credentials."""
  text = WORKFLOW.read_text(encoding="utf-8")
  lines = [
      line for line in text.splitlines() if re.match(r"\s*-?\s*uses:", line)
  ]
  uses = [step["uses"] for step in _job()["steps"] if "uses" in step]

  assert len(lines) == len(uses)
  for action in uses:
    assert _PINNED.fullmatch(action), f"{action} is not pinned to a commit SHA"
  for line in lines:
    assert re.search(r"#\s*v\d", line), f"no version comment: {line.strip()}"
  assert sorted(action.split("@")[0] for action in uses) == [
      "actions/checkout",
      "github/codeql-action/upload-sarif",
      "google-github-actions/auth",
  ]


def test_the_checkout_keeps_no_token_and_has_the_base_branch_history():
  checkout = _job()["steps"][0]

  assert checkout["uses"].startswith("actions/checkout@")
  assert checkout["with"]["persist-credentials"] is False
  assert (
      str(checkout["with"]["fetch-depth"]) == "0"
  ), "cm find --diff=origin/<base> needs the base branch in the clone"


def test_every_step_runs_with_bash_and_pipefail():
  """The tests below run the scripts as Actions runs `shell: bash`."""
  assert _workflow()["defaults"] == {"run": {"shell": "bash"}}
  assert "defaults" not in _job()
  for step in _job()["steps"]:
    assert step.get("shell", "bash") == "bash", step.get("name")


def test_only_a_configured_repository_authenticates_installs_and_scans():
  steps = _job()["steps"]
  configured = "steps.check_config.outputs.configured == 'true'"

  assert steps[1].get("id") == "check_config", "check right after checkout"
  assert "if" not in steps[1]
  for step in steps[2:]:
    assert configured in _condition(step.get("if", "")), step.get("name")
  # Without a status function, `if:` implies success(): a failed step before
  # these stops them.
  for key in ("Authenticate to Google Cloud", "Install the CodeMender CLI"):
    assert _condition(_step(key)["if"]) == configured
  assert _condition(_step("scan")["if"]) == configured


def test_the_auth_step_uses_the_secrets_that_the_check_reads():
  auth = _step("Authenticate to Google Cloud")

  assert auth["uses"].startswith("google-github-actions/auth@")
  assert auth["with"] == {
      "project_id": "${{ secrets.GCP_PROJECT_ID }}",
      "workload_identity_provider": "${{ secrets.GCP_WIF_PROVIDER }}",
      "service_account": "${{ secrets.GCP_WIF_SERVICE_ACCOUNT }}",
  }
  assert _step("check_config")["env"] == {
      name: f"${{{{ secrets.{name} }}}}" for name in SECRETS
  }


def test_the_cli_is_downloaded_over_https_from_artifact_registry():
  url = _workflow()["env"]["CM_DOWNLOAD_URL"]

  assert url.startswith(
      "https://artifactregistry.googleapis.com/download/v1/projects/cmoc-prod/"
  )
  assert '"$CM_DOWNLOAD_URL"' in _step("Install the CodeMender CLI")["run"]


def test_the_scan_compares_with_the_base_branch_and_fails_on_critical_high():
  assert _step("scan")["env"] == {
      "BASE_REF": "${{ github.base_ref }}",
      "FAIL_ON": "CRITICAL,HIGH",
  }


def test_the_sandbox_is_off_only_on_a_new_github_hosted_runner():
  """--sandbox=false is for runners that start each job in a new VM."""
  job = _job()

  assert "--sandbox=false" in _step("scan")["run"]
  assert job["runs-on"] == "ubuntu-24.04"
  assert job["timeout-minutes"] == 15


def test_the_upload_runs_after_a_failed_scan_and_never_fails_the_job():
  upload = _step("Upload results to code scanning")

  assert upload["uses"].startswith("github/codeql-action/upload-sarif@")
  assert _condition(upload["if"]) == (
      "!cancelled() && steps.check_config.outputs.configured == 'true'"
      " && hashFiles('codemender.sarif') != ''"
  )
  # A repository without code scanning fails the upload, not the job.
  assert upload["continue-on-error"] is True
  assert upload["with"] == {
      "sarif_file": "codemender.sarif",
      "category": "codemender",
  }


def test_the_report_is_not_published_where_anyone_can_read_it():
  """A public repository's artifacts are public; its alert list isn't."""
  for step in _job()["steps"]:
    assert not str(step.get("uses", "")).startswith("actions/upload-artifact@")


@pytest.mark.parametrize(
    "context, runs",
    [
        pytest.param(_github("pull_request"), True, id="pull-request"),
        pytest.param(
            _github("pull_request", head="contributor/fork"),
            False,
            id="fork-pull-request",
        ),
        pytest.param(
            _github("pull_request", actor="dependabot[bot]"),
            False,
            id="dependabot-pull-request",
        ),
        pytest.param(_github("workflow_dispatch"), True, id="manual-run"),
    ],
)
def test_the_job_runs_only_where_it_can_read_the_secrets(context, runs):
  assert _evaluate(_job()["if"], context) is runs


def test_the_required_ci_checks_do_not_depend_on_codemender():
  """Missing secrets or a CodeMender outage never block a merge."""
  text = CI_WORKFLOW.read_text(encoding="utf-8")

  assert "codemender" not in text.lower()
  assert "CM_DOWNLOAD_URL" not in text


# The workflow's step scripts and the pre-commit hook, run in temporary git
# repositories.


class _Repository:
  """A temporary git repository, used with an isolated git configuration."""

  def __init__(self, path, env):
    self.path = path
    self.env = env

  @property
  def top(self):
    """The work tree as a process sees it, with symbolic links resolved."""
    return os.path.realpath(self.path)

  def git(self, *args):
    return subprocess.run(
        ["git", *args],
        cwd=self.path,
        env=self.env,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

  def commit(self, path, text, message):
    """Commits `path` with `text` in it, and returns the new commit."""
    file = self.path / path
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(text, encoding="utf-8")
    self.git("add", "--all")
    self.git("commit", "--quiet", "--message", message)
    return self.git("rev-parse", "HEAD")


class _Workspace:
  """A runner's files and environment, a stub cm and temporary repositories."""

  def __init__(self, tmp_path):
    self.tmp_path = tmp_path
    self.runner_temp = tmp_path / "runner-temp"
    self.bin = tmp_path / "bin"
    home = tmp_path / "home"
    for directory in (self.runner_temp, self.bin, home):
      directory.mkdir()
    self.output = tmp_path / "github-output"
    self.summary = tmp_path / "step-summary.md"
    self.github_path = tmp_path / "github-path"
    for file in (self.output, self.summary, self.github_path):
      file.touch()
    self.cm_log = tmp_path / "cm-calls.jsonl"
    path = os.pathsep.join([str(self.bin), *_system_path()])
    if shutil.which("git", path=path) is None:
      pytest.skip("git is not on PATH")
    # Nothing from the environment that runs the tests reaches git, the
    # steps or the hook: not a CI run's own GITHUB_* and RUNNER_* variables,
    # nor a developer's CodeMender settings or git configuration.
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(
            ("GIT_", "GITHUB_", "RUNNER_", "CM_", "GCP_", "BQAA_CODEMENDER_")
        )
        and key
        not in {
            "BASH_ENV",
            "ENV",
            "CDPATH",
            "PWD",
            "OLDPWD",
            "BASE_REF",
            "FAIL_ON",
        }
    }
    env.update(
        PATH=path,
        HOME=str(home),
        XDG_CONFIG_HOME=str(home),
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_AUTHOR_NAME="codemender test",
        GIT_AUTHOR_EMAIL="codemender-test@example.invalid",
        GIT_COMMITTER_NAME="codemender test",
        GIT_COMMITTER_EMAIL="codemender-test@example.invalid",
        CM_STUB_LOG=str(self.cm_log),
        GITHUB_OUTPUT=str(self.output),
        GITHUB_STEP_SUMMARY=str(self.summary),
        GITHUB_PATH=str(self.github_path),
        GITHUB_REPOSITORY=REPOSITORY,
        GITHUB_SERVER_URL="https://github.com",
        RUNNER_TEMP=str(self.runner_temp),
    )
    self.env = env

  def install_cm(self):
    """Puts the stub cm first on PATH."""
    stub = self.tmp_path / "cm_stub.py"
    source = CM_STUB.replace("__FINDINGS__", repr(FINDINGS))
    stub.write_text(source, encoding="utf-8")
    cm = self.bin / "cm"
    cm.write_text(
        "#!/bin/sh\n"
        f'exec {shlex.quote(sys.executable)} {shlex.quote(str(stub))} "$@"\n',
        encoding="utf-8",
    )
    cm.chmod(0o755)

  def repository(self, name="checkout"):
    path = self.tmp_path / name
    path.mkdir()
    repository = _Repository(path, self.env)
    repository.git("-c", "init.defaultBranch=main", "init", "--quiet")
    repository.git("symbolic-ref", "HEAD", "refs/heads/main")
    return repository

  def calls(self):
    """Each call of the stub cm, in order: its arguments and directory."""
    if not self.cm_log.exists():
      return []
    lines = self.cm_log.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines]

  def outputs(self):
    """What the step wrote to $GITHUB_OUTPUT."""
    lines = self.output.read_text(encoding="utf-8").splitlines()
    return dict(line.split("=", 1) for line in lines)

  def _environment(self, env, **variables):
    """self.env with `variables`, then `env`, where None drops a variable."""
    result = dict(self.env, **variables)
    for name, value in (env or {}).items():
      if value is None:
        result.pop(name, None)
      else:
        result[name] = value
    return result

  def run_step(
      self, step, repository, *, event="pull_request", context=None, env=None
  ):
    """Runs a step's script the way Actions does, in `repository`.

    The workflow's and the step's `env:` take each `${{ expression }}` from
    `context`, or "" for one it doesn't have, as Actions gives for a secret
    that isn't set or github.base_ref outside a pull request.
    """
    context = context or {}
    variables = {"GITHUB_EVENT_NAME": event}
    for block in (_workflow().get("env"), step.get("env")):
      for name, value in (block or {}).items():
        variables[name] = _EXPRESSION.sub(
            lambda match: context.get(match[1], ""), str(value)
        )
    script = self.tmp_path / "step.sh"
    script.write_text(step["run"], encoding="utf-8")
    return subprocess.run(
        [*BASH, str(script)],
        cwd=repository.path,
        env=self._environment(env, **variables),
        capture_output=True,
        text=True,
        timeout=120,
    )

  def run_hook(self, repository, *, cwd=None, env=None, shell=None):
    """Runs the pre-commit hook as Git does, or with `shell`."""
    return subprocess.run(
        [str(HOOK)] if shell is None else [shell, str(HOOK)],
        cwd=cwd or repository.path,
        env=self._environment(env),
        capture_output=True,
        text=True,
        timeout=120,
    )


@pytest.fixture
def workspace(tmp_path):
  return _Workspace(tmp_path)


def _secrets(**values):
  """The secrets in a check step's context: CONFIGURED, updated by `values`."""
  secrets = dict(CONFIGURED, **values)
  return {f"secrets.{name}": value for name, value in secrets.items()}


@pytest.mark.parametrize(
    "path, ignored",
    [
        (".cm_project", True),
        ("codemender.sarif", True),
        ("gha-creds-0123456789abcdef.json", True),
        (".github/workflows/codemender.yml", False),
        ("scripts/codemender_pre_commit.sh", False),
    ],
)
def test_gitignore_keeps_codemender_state_and_reports_out(
    workspace, path, ignored
):
  repository = workspace.repository()
  shutil.copyfile(ROOT / ".gitignore", repository.path / ".gitignore")

  checked = subprocess.run(
      ["git", "check-ignore", "--quiet", "--no-index", path],
      cwd=repository.path,
      env=workspace.env,
      capture_output=True,
      text=True,
  )

  assert checked.returncode == (0 if ignored else 1), checked.stderr


def test_the_check_passes_a_configured_repository_on_to_the_scan(workspace):
  repository = workspace.repository()

  result = workspace.run_step(
      _step("check_config"), repository, context=_secrets()
  )

  assert result.returncode == 0, result.stdout + result.stderr
  assert workspace.outputs() == {"configured": "true"}
  assert NOTICE not in result.stdout
  assert workspace.summary.read_text(encoding="utf-8") == ""
  for value in CONFIGURED.values():
    assert value not in result.stdout + result.stderr


@pytest.mark.parametrize("value", list(NOT_SET.values()), ids=list(NOT_SET))
@pytest.mark.parametrize("secret", SECRETS)
def test_a_secret_that_is_not_set_skips_the_scan(workspace, secret, value):
  repository = workspace.repository()
  if value is None:
    context, env = _secrets(), {secret: None}
  else:
    context, env = _secrets(**{secret: value}), {}

  result = workspace.run_step(
      _step("check_config"), repository, context=context, env=env
  )

  assert result.returncode == 0, result.stdout + result.stderr
  assert NOTICE in result.stdout.splitlines()
  assert workspace.outputs() == {"configured": "false"}
  summary = workspace.summary.read_text(encoding="utf-8")
  assert f"Skipped: these repository secrets are not set: {secret}." in summary
  assert SETUP_URL in summary


def test_the_skip_names_every_secret_that_is_not_set(workspace):
  repository = workspace.repository()

  result = workspace.run_step(_step("check_config"), repository)

  assert result.returncode == 0, result.stdout + result.stderr
  assert workspace.outputs() == {"configured": "false"}
  assert (
      "Skipped: these repository secrets are not set: GCP_PROJECT_ID,"
      " GCP_WIF_PROVIDER, GCP_WIF_SERVICE_ACCOUNT."
  ) in workspace.summary.read_text(encoding="utf-8")


def test_secrets_with_surrounding_whitespace_are_set(workspace):
  """The auth action trims them, as the check does."""
  repository = workspace.repository()
  padded = {name: f" {value}\n" for name, value in CONFIGURED.items()}

  result = workspace.run_step(
      _step("check_config"), repository, context=_secrets(**padded)
  )

  assert result.returncode == 0, result.stdout + result.stderr
  assert workspace.outputs() == {"configured": "true"}


def _pull_request(workspace):
  """A pull request's checkout: origin/main, and a commit on top of it."""
  repository = workspace.repository()
  base = repository.commit("app.py", "print('main')\n", "main")
  repository.git("update-ref", "refs/remotes/origin/main", base)
  repository.commit("app.py", "print('pull request')\n", "pull request")
  return repository


def _scan(
    workspace, repository, *, event="pull_request", base_ref="main", **env
):
  """Runs the scan step with the stub cm, for `event` into `base_ref`."""
  workspace.install_cm()
  return workspace.run_step(
      _step("scan"),
      repository,
      event=event,
      context={"github.base_ref": base_ref},
      env=env,
  )


def test_a_clean_pull_request_passes(workspace):
  repository = _pull_request(workspace)

  result = _scan(workspace, repository, CM_STUB_FIND="clean")

  assert result.returncode == 0, result.stdout + result.stderr
  assert workspace.calls() == [
      {"args": ["init"], "cwd": repository.top},
      {"args": SCAN, "cwd": repository.top},
  ]
  assert (repository.path / "codemender.sarif").is_file()
  summary = workspace.summary.read_text(encoding="utf-8")
  assert "## CodeMender scan" in summary
  assert (
      "Compared with `origin/main`. Failing severities: CRITICAL,HIGH."
      in summary
  )
  assert "Passed: no vulnerability with a failing severity." in summary
  assert CODE_SCANNING in summary


def test_a_finding_fails_the_check_and_stays_out_of_the_summary(workspace):
  repository = _pull_request(workspace)

  result = _scan(workspace, repository, CM_STUB_FIND="finding")

  assert result.returncode == 1, result.stdout + result.stderr
  # The upload step sends the report to code scanning.
  report = json.loads(
      (repository.path / "codemender.sarif").read_text(encoding="utf-8")
  )
  assert len(report["runs"][0]["results"]) == len(FINDINGS)
  summary = workspace.summary.read_text(encoding="utf-8")
  assert (
      "Failed (exit status 1): CodeMender reported a vulnerability with a"
      " failing severity."
  ) in summary
  assert CODE_SCANNING in summary
  for title in FINDINGS:
    assert title not in summary
    assert title not in result.stdout + result.stderr
  assert [call["args"][0] for call in workspace.calls()] == ["init", "find"]


@pytest.mark.parametrize("status", [1, 4])
def test_a_scan_that_writes_no_report_fails_with_its_exit_status(
    workspace, status
):
  """Exit status 1 without a report is an error, not a finding."""
  repository = _pull_request(workspace)

  result = _scan(
      workspace, repository, CM_STUB_FIND="error", CM_STUB_STATUS=str(status)
  )

  assert result.returncode == status, result.stdout + result.stderr
  assert not (repository.path / "codemender.sarif").exists()
  summary = workspace.summary.read_text(encoding="utf-8")
  assert (
      f"Failed (exit status {status}): the scan wrote no report; see this"
      " step's log."
  ) in summary
  assert "reported a vulnerability" not in summary
  assert CODE_SCANNING not in summary


def test_a_passing_scan_without_a_report_does_not_point_to_code_scanning(
    workspace,
):
  repository = _pull_request(workspace)

  result = _scan(
      workspace, repository, CM_STUB_FIND="error", CM_STUB_STATUS="0"
  )

  assert result.returncode == 0, result.stdout + result.stderr
  assert not (repository.path / "codemender.sarif").exists()
  summary = workspace.summary.read_text(encoding="utf-8")
  assert "Passed: no vulnerability with a failing severity." in summary
  assert CODE_SCANNING not in summary


def test_a_scan_that_fails_after_its_report_is_not_a_finding(workspace):
  repository = _pull_request(workspace)

  result = _scan(workspace, repository, CM_STUB_FIND="late-error")

  assert result.returncode == 2, result.stdout + result.stderr
  # The upload step still sends the report to code scanning.
  assert (repository.path / "codemender.sarif").is_file()
  summary = workspace.summary.read_text(encoding="utf-8")
  assert (
      "Failed (exit status 2): the scan failed after writing its report; see"
      " this step's log."
  ) in summary
  assert "reported a vulnerability" not in summary
  assert CODE_SCANNING in summary
  for title in FINDINGS:
    assert title not in summary
    assert title not in result.stdout + result.stderr


def test_a_report_left_in_the_checkout_is_not_summarized_or_uploaded(
    workspace,
):
  repository = _pull_request(workspace)
  (repository.path / "codemender.sarif").write_text("{}", encoding="utf-8")

  # Exit status 1, as for a finding, but this scan writes no report.
  result = _scan(workspace, repository, CM_STUB_FIND="error")

  assert result.returncode == 1, result.stdout + result.stderr
  # The upload step's hashFiles('codemender.sarif') finds nothing.
  assert not (repository.path / "codemender.sarif").exists()
  summary = workspace.summary.read_text(encoding="utf-8")
  assert "the scan wrote no report" in summary
  assert "reported a vulnerability" not in summary
  assert CODE_SCANNING not in summary


def test_an_empty_fail_on_reports_without_failing(workspace):
  repository = _pull_request(workspace)

  result = _scan(workspace, repository, FAIL_ON="")

  assert result.returncode == 0, result.stdout + result.stderr
  assert workspace.calls()[-1]["args"][4] == "--fail-on="
  assert (
      "Failing severities: none (findings are only reported)."
      in workspace.summary.read_text(encoding="utf-8")
  )


def test_a_manual_run_scans_the_last_commit(workspace):
  repository = workspace.repository()
  repository.commit("app.py", "print('one')\n", "one")
  repository.commit("app.py", "print('two')\n", "two")

  result = _scan(workspace, repository, event="workflow_dispatch", base_ref="")

  assert result.returncode == 0, result.stdout + result.stderr
  assert workspace.calls()[-1]["args"][2] == "--diff=HEAD~1"
  assert "Compared with `HEAD~1`." in workspace.summary.read_text(
      encoding="utf-8"
  )


def test_a_manual_run_on_a_first_commit_compares_with_main(workspace):
  repository = workspace.repository()
  first = repository.commit("app.py", "print('one')\n", "one")
  repository.git("update-ref", "refs/remotes/origin/main", first)

  result = _scan(workspace, repository, event="workflow_dispatch", base_ref="")

  assert result.returncode == 0, result.stdout + result.stderr
  assert workspace.calls()[-1]["args"][2] == "--diff=origin/main"


@pytest.mark.parametrize(
    "event, base_ref, origin_main, ref",
    [
        pytest.param(
            "pull_request", "main", False, "origin/main", id="no-base"
        ),
        pytest.param("pull_request", "", True, "origin/", id="no-base-ref"),
        pytest.param(
            "workflow_dispatch", "", False, "origin/main", id="first-commit"
        ),
    ],
)
def test_the_scan_fails_without_the_commit_it_compares_with(
    workspace, event, base_ref, origin_main, ref
):
  repository = workspace.repository()
  first = repository.commit("app.py", "print('one')\n", "one")
  if origin_main:
    repository.git("update-ref", "refs/remotes/origin/main", first)

  result = _scan(workspace, repository, event=event, base_ref=base_ref)

  assert result.returncode != 0, result.stdout + result.stderr
  assert (
      f"::error::cannot scan the changes: '{ref}' is not a commit in this clone"
      in result.stdout.splitlines()
  )
  assert workspace.calls() == [], "cm must not run"


def test_a_failed_cm_init_stops_the_scan(workspace):
  repository = _pull_request(workspace)

  result = _scan(workspace, repository, CM_STUB_INIT_STATUS="3")

  assert result.returncode == 3, result.stdout + result.stderr
  assert [call["args"] for call in workspace.calls()] == [["init"]]


@pytest.mark.skipif(shutil.which("unzip") is None, reason="needs unzip")
def test_the_install_step_puts_the_downloaded_cli_on_the_path(workspace):
  archive = workspace.tmp_path / "download.zip"
  with zipfile.ZipFile(archive, "w") as bundle:
    entry = zipfile.ZipInfo("cm")
    entry.external_attr = 0o100755 << 16
    bundle.writestr(entry, "#!/bin/sh\necho 'cm version 0.0.0'\n")
  curl_args = workspace.tmp_path / "curl-args"
  curl = workspace.bin / "curl"
  curl.write_text(
      "#!/bin/sh\n"
      f"printf '%s\\n' \"$@\" > {shlex.quote(str(curl_args))}\n"
      "while [ $# -gt 1 ]; do\n"
      '  if [ "$1" = -o ]; then out="$2"; fi\n'
      "  shift\n"
      "done\n"
      f'cp {shlex.quote(str(archive))} "$out"\n',
      encoding="utf-8",
  )
  curl.chmod(0o755)
  repository = workspace.repository()

  result = workspace.run_step(_step("Install the CodeMender CLI"), repository)

  assert result.returncode == 0, result.stdout + result.stderr
  assert curl_args.read_text(encoding="utf-8").splitlines() == [
      "-fsSL",
      "-o",
      f"{workspace.runner_temp}/cm.zip",
      _workflow()["env"]["CM_DOWNLOAD_URL"],
  ]
  assert os.access(workspace.runner_temp / "cm" / "cm", os.X_OK)
  assert workspace.github_path.read_text(encoding="utf-8").splitlines() == [
      str(workspace.runner_temp / "cm")
  ]


def _staged_change(workspace):
  """A repository with a commit and a staged change to it."""
  repository = workspace.repository()
  repository.commit("app.py", "print('main')\n", "main")
  (repository.path / "app.py").write_text("print('staged')\n", encoding="utf-8")
  repository.git("add", "app.py")
  return repository


def test_the_hook_is_an_executable_posix_shell_script():
  assert HOOK.read_text(encoding="utf-8").startswith("#!/bin/sh\n")
  assert os.access(HOOK, os.X_OK), f"chmod +x {HOOK.relative_to(ROOT)}"


def test_the_hook_lets_a_clean_change_through(workspace):
  workspace.install_cm()
  repository = _staged_change(workspace)

  result = workspace.run_hook(repository, env={"CM_STUB_FIND": "clean"})

  assert result.returncode == 0, result.stderr
  assert workspace.calls() == [{"args": HOOK_SCAN, "cwd": repository.top}]
  assert "CodeMender blocked this commit" not in result.stderr


@pytest.mark.parametrize("find, status", [("finding", 1), ("error", 4)])
def test_the_hook_blocks_the_commit_when_the_scan_fails(
    workspace, find, status
):
  workspace.install_cm()
  repository = _staged_change(workspace)

  result = workspace.run_hook(
      repository, env={"CM_STUB_FIND": find, "CM_STUB_STATUS": str(status)}
  )

  assert result.returncode == 1, result.stderr
  assert result.stderr.splitlines()[-3:] == [
      f"CodeMender blocked this commit (cm find exited with status {status})."
      " To list the open findings, run:",
      "  cm report --status=OPEN",
      "To commit anyway, run git commit --no-verify.",
  ]


@pytest.mark.parametrize(
    "fail_on, flag",
    [
        pytest.param(None, "--fail-on=CRITICAL,HIGH", id="unset"),
        pytest.param("CRITICAL", "--fail-on=CRITICAL", id="critical"),
        pytest.param(
            "CRITICAL,HIGH,MEDIUM",
            "--fail-on=CRITICAL,HIGH,MEDIUM",
            id="medium",
        ),
        # As in the workflow and in cm, "" reports without blocking.
        pytest.param("", "--fail-on=", id="empty"),
    ],
)
def test_the_hook_blocks_on_the_severities_in_its_setting(
    workspace, fail_on, flag
):
  workspace.install_cm()
  repository = _staged_change(workspace)

  result = workspace.run_hook(
      repository, env={"BQAA_CODEMENDER_FAIL_ON": fail_on}
  )

  assert result.returncode == 0, result.stderr
  assert workspace.calls()[-1]["args"][4] == flag


def test_the_hook_ignores_an_unrelated_fail_on_variable(workspace):
  workspace.install_cm()
  repository = _staged_change(workspace)

  result = workspace.run_hook(repository, env={"FAIL_ON": "LOW"})

  assert result.returncode == 0, result.stderr
  assert workspace.calls()[-1]["args"] == HOOK_SCAN


def test_the_hook_scans_the_whole_work_tree_from_a_subdirectory(workspace):
  workspace.install_cm()
  repository = _staged_change(workspace)
  subdirectory = repository.path / "src" / "pkg"
  subdirectory.mkdir(parents=True)

  result = workspace.run_hook(repository, cwd=subdirectory)

  assert result.returncode == 0, result.stderr
  assert workspace.calls() == [{"args": HOOK_SCAN, "cwd": repository.top}]


def test_the_hook_does_not_scan_when_nothing_is_staged(workspace):
  """An empty commit, or an amend that only rewords the message."""
  workspace.install_cm()
  repository = workspace.repository()
  repository.commit("app.py", "print('main')\n", "main")

  result = workspace.run_hook(repository)

  assert result.returncode == 0, result.stderr
  assert workspace.calls() == []


@pytest.mark.parametrize(
    "require",
    [None, "", "0", " 1", "true"],
    ids=["unset", "empty", "zero", "padded-one", "true"],
)
def test_without_cm_the_hook_skips_the_scan(workspace, require):
  """Only BQAA_CODEMENDER_REQUIRE=1 makes cm required."""
  repository = _staged_change(workspace)
  assert shutil.which("cm", path=workspace.env["PATH"]) is None

  result = workspace.run_hook(
      repository, env={"BQAA_CODEMENDER_REQUIRE": require}
  )

  assert result.returncode == 0, result.stderr
  assert "cm is not on PATH; skipping the CodeMender scan." in result.stderr


def test_without_cm_the_hook_blocks_when_cm_is_required(workspace):
  repository = _staged_change(workspace)
  assert shutil.which("cm", path=workspace.env["PATH"]) is None

  result = workspace.run_hook(repository, env={"BQAA_CODEMENDER_REQUIRE": "1"})

  assert result.returncode == 1
  assert "BQAA_CODEMENDER_REQUIRE=1 requires the scan" in result.stderr


@pytest.mark.skipif(DASH is None, reason="needs dash")
@pytest.mark.parametrize("find, status", [("clean", 0), ("finding", 1)])
def test_the_hook_runs_in_a_strict_posix_shell(workspace, find, status):
  """Git runs the hook with /bin/sh, which is dash on Debian and Ubuntu."""
  workspace.install_cm()
  repository = _staged_change(workspace)

  result = workspace.run_hook(
      repository, env={"CM_STUB_FIND": find}, shell=DASH
  )

  assert result.returncode == status, result.stderr
  assert [call["args"] for call in workspace.calls()] == [HOOK_SCAN]


@pytest.mark.parametrize(
    "find, committed", [("clean", True), ("finding", False)]
)
def test_the_installed_hook_gates_git_commit(workspace, find, committed):
  """The README's install command, then `git commit` as a developer runs it."""
  workspace.install_cm()
  repository = workspace.repository()
  hook = repository.path / "scripts" / "codemender_pre_commit.sh"
  hook.parent.mkdir()
  shutil.copy(HOOK, hook)
  base = repository.commit("app.py", "print('main')\n", "main")
  (repository.path / ".git" / "hooks").mkdir(exist_ok=True)
  subprocess.run(
      ["sh", "-c", _install_command()],
      cwd=repository.path,
      env=workspace.env,
      check=True,
  )
  (repository.path / "app.py").write_text("print('change')\n", encoding="utf-8")
  repository.git("add", "app.py")

  result = subprocess.run(
      ["git", "commit", "--quiet", "--message", "change"],
      cwd=repository.path,
      env=dict(workspace.env, CM_STUB_FIND=find),
      capture_output=True,
      text=True,
      timeout=120,
  )

  assert [call["args"] for call in workspace.calls()] == [HOOK_SCAN]
  if committed:
    assert result.returncode == 0, result.stderr
    assert repository.git("rev-parse", "HEAD") != base
  else:
    assert result.returncode != 0
    assert "CodeMender blocked this commit" in result.stderr
    assert repository.git("rev-parse", "HEAD") == base
    # As the message says, --no-verify commits without the scan.
    repository.git("commit", "--quiet", "--no-verify", "--message", "change")
    assert repository.git("rev-parse", "HEAD") != base
    assert len(workspace.calls()) == 1


def test_the_readme_documents_the_hook_and_its_settings():
  readme = SCRIPTS_README.read_text(encoding="utf-8")
  hook = HOOK.read_text(encoding="utf-8")
  setting = re.compile(r"BQAA_CODEMENDER_[A-Z_]+")

  assert (
      set(setting.findall(readme))
      == set(setting.findall(hook))
      == {
          "BQAA_CODEMENDER_FAIL_ON",
          "BQAA_CODEMENDER_REQUIRE",
      }
  )
  assert _install_command() in hook
  assert "(#codemender-pre-commit-hook)" in readme
