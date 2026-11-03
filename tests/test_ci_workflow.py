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

"""Tests that CI runs the BQCA sync_generation gate in a required check.

`validate_contracts.py --profile bqca --previous-binding PATH` is the only
check that the BQCA binding's `sync_generation` never moves back. A binding
that sets it back to an earlier sync, whose committed receipt it still holds,
passes the validator on its own; only the comparison with the previous
binding refuses it. A failing step blocks a merge only when its job is a
status check that main's ruleset requires, so the gate has to run in one of
those jobs, with nothing there that can skip it, and on each event that
brings a change into main: a pull request, a push, and a merge group once
main has a merge queue.

The first tests read the workflow. The others take the gate step's script
from ci.yml and run it the way Actions does, in temporary git repositories
that hold a copy of the Looker Studio dashboard: the head keeps the
repository's binding, and each test chooses the previous one.
"""

import itertools
import os
import pathlib
import re
import shlex
import shutil
import subprocess
import sys

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
DASHBOARD = "dashboards/looker_studio"
BINDING = f"{DASHBOARD}/bindings/bqca_report_template.yaml"
VALIDATOR = f"{DASHBOARD}/tools/validate_contracts.py"
GATE_FLAG = "--previous-binding"

# The status checks that main's ruleset requires, by exact name. The tests
# stay offline, so this copies the repository rules for main: keep the two
# in step.
REQUIRED_CHECKS = frozenset(
    {
        "Format check",
        "Test (Python 3.10)",
        "Test (Python 3.11)",
        "Test (Python 3.12)",
        "Test (Python 3.13)",
        "Test (Python 3.14)",
    }
)

# How Actions runs a step with `shell: bash`; the script file goes last.
BASH = ("bash", "--noprofile", "--norc", "-eo", "pipefail")

# A commit that none of the test repositories holds.
ABSENT_COMMIT = "0123456789abcdef0123456789abcdef01234567"

# The events that bring a change into main. A merge queue checks a merge
# group, whose event has neither a pull request's base_ref nor a push's
# before: only merge_group.base_sha names the commit it merges into.
EVENTS = ("pull_request", "push", "merge_group")

_MATRIX_VALUE = re.compile(r"\$\{\{\s*matrix\.([\w-]+)\s*\}\}")


def _check_names(job_id, job):
  """The names of the status checks that a ci.yml job reports.

  A job reports under its `name`, or else its id. A matrix job reports once
  for each combination: with the `matrix.*` values filled into its name, or,
  for a name without them, appended in parentheses. A matrix this does not
  expand (`include`, `exclude` or an expression) yields no name, and a name
  left with any other expression matches no required check.
  """
  name = str(job.get("name", job_id))
  matrix = (job.get("strategy") or {}).get("matrix")
  if matrix is None:
    return [name]
  if (
      not isinstance(matrix, dict)
      or {"include", "exclude"} & matrix.keys()
      or not all(isinstance(values, list) for values in matrix.values())
  ):
    return []
  keys = list(matrix)
  names = []
  for values in itertools.product(*(matrix[key] for key in keys)):
    combination = dict(zip(keys, map(str, values)))
    if _MATRIX_VALUE.search(name):
      names.append(
          _MATRIX_VALUE.sub(
              lambda match: combination.get(match[1], match[0]), name
          )
      )
    else:
      names.append(f"{name} ({', '.join(combination.values())})")
  return names


def _is_gate(step):
  run = step.get("run") or ""
  return VALIDATOR in run and GATE_FLAG in run


def _workflow():
  return yaml.safe_load(CI_WORKFLOW.read_text(encoding="utf-8"))


def _jobs():
  return _workflow()["jobs"]


def _triggers():
  """Maps each event in ci.yml's `on:` to its filters ({} for none)."""
  workflow = _workflow()
  # YAML 1.1, which PyYAML reads, loads the bare key `on` as True.
  on = workflow["on"] if "on" in workflow else workflow[True]
  if isinstance(on, str):
    on = [on]
  if isinstance(on, list):
    on = dict.fromkeys(on)
  return {event: filters or {} for event, filters in on.items()}


def _gate_jobs():
  """Maps each ci.yml job that runs the gate to its check names and job."""
  return {
      job_id: (_check_names(job_id, job), job)
      for job_id, job in _jobs().items()
      if any(_is_gate(step) for step in job.get("steps") or [])
  }


def _required_gate_jobs():
  """(required check names, job) for each required job that runs the gate."""
  required = [
      (sorted(REQUIRED_CHECKS.intersection(names)), job)
      for names, job in _gate_jobs().values()
      if REQUIRED_CHECKS.intersection(names)
  ]
  assert required, "no required status check runs the sync_generation gate"
  return required


def test_a_required_status_check_runs_the_sync_generation_gate():
  reported = {job_id: names for job_id, (names, _) in _gate_jobs().items()}

  assert reported, f"no ci.yml job runs {VALIDATOR} {GATE_FLAG}"
  assert any(REQUIRED_CHECKS.intersection(n) for n in reported.values()), (
      f"the jobs that run {VALIDATOR} {GATE_FLAG} report {reported}, and"
      f" none of those is a required status check ({sorted(REQUIRED_CHECKS)}),"
      " so a failing sync_generation gate does not block a merge"
  )


def test_only_the_exact_required_check_names_count():
  """A job named `Test (optional gate)` is not `Test (Python 3.12)`."""
  reported = {
      name
      for job_id, job in _jobs().items()
      for name in _check_names(job_id, job)
  }
  optional = {"name": "Test (optional gate)", "steps": []}
  unnamed = {"strategy": {"matrix": {"python-version": ["3.12"]}}}

  assert REQUIRED_CHECKS <= reported, (
      f"no ci.yml job reports {sorted(REQUIRED_CHECKS - reported)}: main's"
      " ruleset and REQUIRED_CHECKS change with the workflow"
  )
  assert _check_names("optional", optional) == ["Test (optional gate)"]
  assert not REQUIRED_CHECKS.intersection(_check_names("optional", optional))
  assert _check_names("test", unnamed) == ["test (3.12)"]


def test_every_event_that_brings_a_change_into_main_runs_the_workflow():
  """Each event in EVENTS is in ci.yml's `on:`, with no path filter.

  A merge queue merges a group only once the required checks pass on the
  group's commit, and Actions runs a workflow for a group only on
  merge_group, which is separate from pull_request and push.
  """
  triggers = _triggers()

  for event in EVENTS:
    assert event in triggers, f"ci.yml does not run on {event}"
    assert not {"paths", "paths-ignore"} & triggers[event].keys(), (
        f"ci.yml filters {event} by path: the sync_generation gate has to"
        f" run for every {event}"
    )
  assert "checks_requested" in triggers["merge_group"].get(
      "types", ["checks_requested"]
  ), "a merge queue asks for a group's checks with checks_requested"


def test_the_gate_job_checks_out_the_full_history():
  """The previous binding is read at an earlier commit, so it must exist."""
  for names, job in _required_gate_jobs():
    steps = job["steps"]
    gate = next(index for index, step in enumerate(steps) if _is_gate(step))
    checkouts = [
        step
        for step in steps[:gate]
        if str(step.get("uses", "")).startswith("actions/checkout@")
    ]

    assert checkouts, f"{names} run the gate before any checkout"
    for checkout in checkouts:
      assert str((checkout.get("with") or {}).get("fetch-depth")) == "0", (
          f"{names} must check out with fetch-depth: 0: the gate reads the"
          " binding at the merge base with the base branch, or at the commit"
          " before the push, and a shallow clone has neither"
      )


def test_nothing_in_the_required_job_can_skip_or_soften_the_gate():
  """A job skipped by `if:` or a failed `needs:` passes its required check."""
  for names, job in _required_gate_jobs():
    assert "needs" not in job, f"{names} must not wait on other jobs"
    assert "if" not in job, f"{names} must not be conditional"
    assert not job.get("continue-on-error"), names
    for step in job["steps"]:
      if _is_gate(step):
        assert "if" not in step, f"{names} must not make the gate conditional"
        assert not step.get("continue-on-error"), names
        # The tests below run the script as Actions runs `shell: bash`.
        assert step.get("shell") == "bash", (
            f"{names} must run the gate with `shell: bash` (bash --noprofile"
            " --norc -eo pipefail), so that a failing command fails the step"
        )


def test_the_gate_compares_with_the_base_branch_or_the_commit_before_a_push():
  """A pull request compares with its merge base, a push with `before`."""
  for names, job in _required_gate_jobs():
    for step in job["steps"]:
      if not _is_gate(step):
        continue
      env = step.get("env") or {}

      assert env.get("BASE_REF") == "${{ github.base_ref }}", names
      assert env.get("BEFORE") == "${{ github.event.before }}", names
      assert "$BASE_REF" in step["run"] and "$BEFORE" in step["run"], names


def test_the_gate_step_gets_the_merge_group_base_sha():
  """A merge group has no base_ref or before; its base_sha is its parent."""
  for names, job in _required_gate_jobs():
    for step in job["steps"]:
      if not _is_gate(step):
        continue
      env = step.get("env") or {}

      assert (
          env.get("MERGE_GROUP_BASE")
          == "${{ github.event.merge_group.base_sha }}"
      ), names
      assert "$MERGE_GROUP_BASE" in step["run"], names


# The gate step's script, run against temporary git histories.


def _repository_binding():
  return (ROOT / BINDING).read_text(encoding="utf-8")


def _repository_generation():
  binding = yaml.safe_load(_repository_binding())
  return binding["live_template_verification"]["sync_generation"]


def _binding_with_generation(generation):
  """The repository's binding with `sync_generation` set, or dropped (None)."""
  line = "" if generation is None else f"  sync_generation: {generation}\n"
  binding, count = re.subn(
      r"(?m)^  sync_generation: \d+\n", line, _repository_binding()
  )
  assert count == 1, f"{BINDING} has no single sync_generation line"
  return binding


class _Repository:
  """A temporary git repository, used with an isolated git configuration."""

  def __init__(self, path, env):
    self.path = path
    self.env = env

  def git(self, *args):
    return subprocess.run(
        ["git", *args],
        cwd=self.path,
        env=self.env,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

  def has(self, revision):
    found = subprocess.run(
        ["git", "cat-file", "-e", revision],
        cwd=self.path,
        env=self.env,
        capture_output=True,
    )
    return found.returncode == 0

  def show(self, revision, path):
    return subprocess.run(
        ["git", "show", f"{revision}:{path}"],
        cwd=self.path,
        env=self.env,
        check=True,
        capture_output=True,
    ).stdout

  def commit(self, binding, message):
    """Commits the dashboard with `binding`, or with no dashboard for None."""
    dashboard = self.path / DASHBOARD
    if binding is None:
      shutil.rmtree(dashboard, ignore_errors=True)
      (self.path / "README.md").write_text(f"{message}\n", encoding="utf-8")
    else:
      if not dashboard.exists():
        shutil.copytree(
            ROOT / DASHBOARD,
            dashboard,
            ignore=shutil.ignore_patterns("__pycache__"),
        )
      (self.path / BINDING).write_text(binding, encoding="utf-8")
    self.git("add", "--all")
    self.git("commit", "--quiet", "--allow-empty", "--message", message)
    return self.git("rev-parse", "HEAD")


class _Workspace:
  """Repositories, a runner temp directory and the gate's environment."""

  def __init__(self, tmp_path):
    self.tmp_path = tmp_path
    self.runner_temp = tmp_path / "runner-temp"
    home = tmp_path / "home"
    bin_dir = tmp_path / "bin"
    for directory in (self.runner_temp, home, bin_dir):
      directory.mkdir()
    # The step runs `python`: this interpreter, which has PyYAML.
    python = bin_dir / "python"
    python.write_text(
        f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n',
        encoding="utf-8",
    )
    python.chmod(0o755)
    # Actions sets GITHUB_EVENT_NAME, so a CI run of these tests has its own;
    # run_gate sets the gate's inputs for each test's event.
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("GIT_")
        and key
        not in {
            "BASH_ENV",
            "ENV",
            "BASE_REF",
            "BEFORE",
            "GITHUB_EVENT_NAME",
            "MERGE_GROUP_BASE",
            "RUNNER_TEMP",
        }
    }
    env.update(
        PATH=f"{bin_dir}{os.pathsep}{env.get('PATH', os.defpath)}",
        HOME=str(home),
        XDG_CONFIG_HOME=str(home),
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_AUTHOR_NAME="sync gate test",
        GIT_AUTHOR_EMAIL="sync-gate-test@example.invalid",
        GIT_COMMITTER_NAME="sync gate test",
        GIT_COMMITTER_EMAIL="sync-gate-test@example.invalid",
    )
    self.env = env

  @property
  def previous_binding(self):
    return self.runner_temp / "previous-binding.yaml"

  def repository(self, name="checkout"):
    path = self.tmp_path / name
    path.mkdir()
    repository = _Repository(path, self.env)
    repository.git("-c", "init.defaultBranch=main", "init", "--quiet")
    repository.git("symbolic-ref", "HEAD", "refs/heads/main")
    return repository

  def clone(self, origin, name="checkout"):
    """A full clone of `origin`: only the commits that its branches reach."""
    path = self.tmp_path / name
    subprocess.run(
        ["git", "clone", "--quiet", "--no-local", str(origin.path), str(path)],
        env=self.env,
        check=True,
        capture_output=True,
    )
    return _Repository(path, self.env)

  def run_gate(
      self, step, repository, *, event, base_ref="", before="", base_sha=""
  ):
    """Runs the gate's script for `event` the way Actions runs the step.

    `base_ref`, `before` and `base_sha` are the step's BASE_REF, BEFORE and
    MERGE_GROUP_BASE: github.base_ref, github.event.before and
    github.event.merge_group.base_sha. An event without one of them gives
    the step an empty string.
    """
    script = self.tmp_path / "gate.sh"
    script.write_text(step["run"], encoding="utf-8")
    return subprocess.run(
        [*BASH, str(script)],
        cwd=repository.path,
        env=dict(
            self.env,
            GITHUB_EVENT_NAME=event,
            BASE_REF=base_ref,
            BEFORE=before,
            MERGE_GROUP_BASE=base_sha,
            RUNNER_TEMP=str(self.runner_temp),
        ),
        capture_output=True,
        text=True,
        timeout=300,
    )


def _event(repository, event, previous):
  """The gate's inputs for `event`, comparing with commit `previous`."""
  if event == "pull_request":
    repository.git("update-ref", "refs/remotes/origin/main", previous)
    # A synchronize event's `before` is the pull request's old head, which a
    # force push may have dropped; only the merge base counts.
    return {"event": event, "base_ref": "main", "before": ABSENT_COMMIT}
  if event == "merge_group":
    # The first group in the queue has main's head commit as its parent.
    repository.git("update-ref", "refs/remotes/origin/main", previous)
    return {"event": event, "base_sha": previous}
  return {"event": event, "before": previous}


@pytest.fixture
def workspace(tmp_path):
  return _Workspace(tmp_path)


@pytest.fixture
def gate_step():
  _, job = _required_gate_jobs()[0]
  return next(step for step in job["steps"] if _is_gate(step))


SSH_KEYGEN = shutil.which("ssh-keygen")
# What ssh-keygen prints, whatever it was asked to do, when it refuses to
# start because the current user ID has no account entry (as in some
# containers).
SSH_KEYGEN_NO_USER = b"No user exists for uid"


@pytest.fixture(scope="session")
def openssh():
  """Skip a test that needs ssh-keygen where OpenSSH cannot run.

  The repository's BQCA binding is finalized, so the gate's validation
  verifies the signature of its committed capture receipt with ssh-keygen
  (OpenSSH 8.2 or later), as CI does. A test that needs the gate to pass
  lists this fixture in `usefixtures`. It is skipped where ssh-keygen is not
  on PATH, or refuses to start because the current user ID has no account
  entry; anywhere else it runs, and any error, an old ssh-keygen's included,
  fails it.
  """
  if SSH_KEYGEN is None:
    pytest.skip("ssh-keygen (OpenSSH) is not installed")
  started = subprocess.run(
      [SSH_KEYGEN, "-l", "-f", os.devnull], capture_output=True, check=False
  )
  if SSH_KEYGEN_NO_USER in started.stderr:
    pytest.skip(
        "ssh-keygen cannot start: "
        + started.stderr.decode("utf-8", "replace").strip()
    )


@pytest.mark.parametrize("event", EVENTS)
def test_the_gate_refuses_a_generation_moved_back(gate_step, workspace, event):
  generation = _repository_generation()
  repository = workspace.repository()
  previous = repository.commit(
      _binding_with_generation(generation + 1), "previous sync"
  )
  repository.commit(_repository_binding(), "moved back")

  result = workspace.run_gate(
      gate_step, repository, **_event(repository, event, previous)
  )

  assert result.returncode != 0, result.stdout + result.stderr
  assert (
      f"sync_generation {generation} is less than {generation + 1}, the"
      " previous binding's: it only moves forward"
  ) in result.stderr
  # The head was compared with the previous commit's binding, not itself.
  assert workspace.previous_binding.read_bytes() == repository.show(
      previous, BINDING
  )


@pytest.mark.parametrize("event", EVENTS)
@pytest.mark.parametrize(
    "previous_binding",
    [
        pytest.param(_repository_binding, id="unchanged"),
        # No sync_generation, as before #561, counts as generation 0.
        pytest.param(lambda: _binding_with_generation(None), id="forward"),
    ],
)
@pytest.mark.usefixtures("openssh")
def test_the_gate_passes_a_generation_kept_or_moved_forward(
    gate_step, workspace, event, previous_binding
):
  repository = workspace.repository()
  previous = repository.commit(previous_binding(), "previous")
  repository.commit(_repository_binding(), "head")

  result = workspace.run_gate(
      gate_step, repository, **_event(repository, event, previous)
  )

  assert result.returncode == 0, result.stdout + result.stderr
  assert "ok: contracts validated (bqca)" in result.stdout
  assert workspace.previous_binding.read_bytes() == repository.show(
      previous, BINDING
  )


@pytest.mark.parametrize("before", ["", "0" * 40], ids=["empty", "all-zero"])
def test_a_push_with_nothing_before_it_skips_the_comparison(
    gate_step, workspace, before
):
  repository = workspace.repository()
  repository.commit(_repository_binding(), "first push")

  result = workspace.run_gate(
      gate_step, repository, event="push", base_ref="", before=before
  )

  assert result.returncode == 0, result.stdout + result.stderr
  assert f"no previous {BINDING} to compare with" in result.stdout
  assert "contracts validated" not in result.stdout
  assert not workspace.previous_binding.exists()


@pytest.mark.parametrize("event", EVENTS)
def test_a_previous_commit_without_the_binding_skips_the_comparison(
    gate_step, workspace, event
):
  repository = workspace.repository()
  previous = repository.commit(None, "before the dashboard")
  repository.commit(_repository_binding(), "adds the dashboard")

  result = workspace.run_gate(
      gate_step, repository, **_event(repository, event, previous)
  )

  assert result.returncode == 0, result.stdout + result.stderr
  assert f"no {BINDING} at {previous} to compare with" in result.stdout
  assert not workspace.previous_binding.exists()


def test_a_push_whose_previous_commit_is_missing_fails(gate_step, workspace):
  """A rewritten history leaves `before` naming a commit the clone lacks."""
  origin = workspace.repository("origin")
  before = origin.commit(
      _binding_with_generation(_repository_generation() + 1), "previous sync"
  )
  origin.git("checkout", "--quiet", "--orphan", "rewritten")
  origin.commit(_repository_binding(), "rewritten history")
  origin.git("branch", "--quiet", "--delete", "--force", "main")
  origin.git("branch", "--quiet", "--move", "main")
  checkout = workspace.clone(origin)
  assert not checkout.has(f"{before}^{{commit}}")

  result = workspace.run_gate(
      gate_step, checkout, event="push", base_ref="", before=before
  )

  assert result.returncode != 0, result.stdout + result.stderr
  assert f"{before} is not in this clone" in result.stdout
  assert not workspace.previous_binding.exists()


def test_a_pull_request_without_a_merge_base_fails(gate_step, workspace):
  repository = workspace.repository()
  repository.commit(_repository_binding(), "head")
  unrelated = repository.git("commit-tree", "HEAD^{tree}", "-m", "unrelated")
  repository.git("update-ref", "refs/remotes/origin/main", unrelated)

  result = workspace.run_gate(
      gate_step,
      repository,
      event="pull_request",
      base_ref="main",
      before=ABSENT_COMMIT,
  )

  assert result.returncode != 0, result.stdout + result.stderr
  assert "no merge base of HEAD and origin/main in this clone" in result.stdout
  assert not workspace.previous_binding.exists()


def test_a_merge_group_compares_with_its_base_sha_not_with_main(
    gate_step, workspace
):
  """A group queued behind another one merges into that group, not main.

  Its base_sha is the earlier group, which moved the generation forward and
  which main has not merged yet; HEAD^ is a commit of the group's own pull
  request. Only the comparison with base_sha refuses the head.
  """
  generation = _repository_generation()
  repository = workspace.repository()
  main = repository.commit(_repository_binding(), "main")
  earlier_group = repository.commit(
      _binding_with_generation(generation + 1), "the earlier merge group"
  )
  repository.commit(_repository_binding(), "moved back")
  repository.commit(_repository_binding(), "the pull request's next commit")
  repository.git("update-ref", "refs/remotes/origin/main", main)

  result = workspace.run_gate(
      gate_step, repository, event="merge_group", base_sha=earlier_group
  )

  assert result.returncode != 0, result.stdout + result.stderr
  assert (
      f"sync_generation {generation} is less than {generation + 1}, the"
      " previous binding's: it only moves forward"
  ) in result.stderr
  assert workspace.previous_binding.read_bytes() == repository.show(
      earlier_group, BINDING
  )


@pytest.mark.parametrize(
    "base_sha",
    ["", "0" * 40, ABSENT_COMMIT],
    ids=["empty", "all-zero", "absent"],
)
def test_a_merge_group_without_its_base_commit_fails(
    gate_step, workspace, base_sha
):
  """Unlike a first push, a merge group always has a parent to compare with."""
  repository = workspace.repository()
  previous = repository.commit(
      _binding_with_generation(_repository_generation() + 1), "previous sync"
  )
  repository.commit(_repository_binding(), "moved back")
  repository.git("update-ref", "refs/remotes/origin/main", previous)

  result = workspace.run_gate(
      gate_step, repository, event="merge_group", base_sha=base_sha
  )

  assert result.returncode != 0, result.stdout + result.stderr
  assert (
      f"::error::cannot compare {BINDING} with the merge group's base:"
      f" base_sha '{base_sha}' is not a commit in this clone"
  ) in result.stdout
  assert not workspace.previous_binding.exists()
