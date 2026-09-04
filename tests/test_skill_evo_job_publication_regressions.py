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

"""Exercise publication against real Git trees; only network writes are fake."""

import json
from pathlib import Path
import subprocess
import sys
import uuid

import pytest

_JOB_DIR = str(
    Path(__file__).resolve().parents[1] / "deploy" / "skill_evolution_job"
)
if _JOB_DIR not in sys.path:
  sys.path.insert(0, _JOB_DIR)

from skill_evolution_job import config
from skill_evolution_job import hooks
from skill_evolution_job import registry
from skill_evolution_job import tools


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch):
  for key in (
      "EVOLUTION_HOOKS",
      "EVOLUTION_WORKDIR",
      "GITHUB_REPO",
      "GITHUB_BASE_BRANCH",
      "AGENT_REGISTRY",
      "GATE_CMD",
      "GATE_POLICY",
      "EVOLUTION_PUBLISH",
      "EVOLUTION_ORDER",
      "GH_TOKEN",
      "GITHUB_TOKEN",
  ):
    monkeypatch.delenv(key, raising=False)
  config.reset_workdir_cache()
  hooks.reset_cache()
  registry.reset_cache()
  yield
  config.reset_workdir_cache()
  hooks.reset_cache()
  registry.reset_cache()


# The fixture agent B can only answer when agent A has its required
# capability. This gate executes that acceptance condition, exposing the
# co-evolution dependency even when A1+B1 passed before PR construction.
_HOST_HOOKS = """
import json
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parent
PREFIX = {prefix!r}

def git(*args):
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()

def gate(run_dir, version, agent):
    run = Path(run_dir)
    a_path = ROOT / PREFIX / "skills/a/SKILL.md"
    b_path = ROOT / PREFIX / "skills/b/SKILL.md"
    a = json.loads(a_path.read_text())
    b = json.loads(b_path.read_text())
    answer_available = a["capability"] >= b["requires"]
    mode = (run / "mode").read_text()
    observation = dict(root=str(ROOT), a=a, b=b, answer_available=answer_available,
                       version=version, agent=agent, head=git("rev-parse", "HEAD"),
                       tree=git("rev-parse", "HEAD^{{tree}}"),
                       status=git("status", "--porcelain", "--untracked-files=no"))
    (run / "gate.json").write_text(json.dumps(observation))
    if mode == "mutate":
        a_path.write_text('{{"capability": 99}}\\n')
        return True, "mutated a tracked skill"
    if mode == "stage":
        a_path.write_text('{{"capability": 99}}\\n')
        git("add", str(a_path))
        return True, "staged a tracked skill"
    if mode == "commit":
        a_path.write_text('{{"capability": 99}}\\n')
        git("add", str(a_path))
        git("commit", "-m", "hook unexpectedly changed HEAD")
        return True, "committed a tracked skill"
    if mode == "raise":
        a_path.write_text('{{"capability": 99}}\\n')
        raise RuntimeError("acceptance test crashed after modifying skill")
    if mode == "none":
        return None, "acceptance unavailable"
    return answer_available, "B can answer only with the required A capability"

def publish(skill_dir, run_dir):
    observation = dict(skill_dir=skill_dir,
                       content=(Path(skill_dir) / "SKILL.md").read_text())
    (Path(run_dir) / "publish.json").write_text(json.dumps(observation))
    return observation
"""


@pytest.fixture
def publication_repo(tmp_path, monkeypatch):
  def build(*, prefix=".", mode="evaluate", selected_requires=1):
    repo = tmp_path / "host"
    repo.mkdir()
    run = tmp_path / "run"
    run.mkdir()
    real_run = subprocess.run

    def git(*args):
      return real_run(
          ["git", *args], cwd=repo, check=True, capture_output=True, text=True
      ).stdout.strip()

    git("init", "-b", "main")
    git("config", "user.email", "fixture@example.invalid")
    git("config", "user.name", "Publication Fixture")
    git("config", "commit.gpgsign", "false")
    git("config", "core.hooksPath", str(repo / ".git" / "hooks"))
    a_path = repo / prefix / "skills/a/SKILL.md"
    b_path = repo / prefix / "skills/b/SKILL.md"
    a_path.parent.mkdir(parents=True)
    b_path.parent.mkdir(parents=True)
    a_path.write_text('{"capability": 0}\n')
    b_path.write_text('{"requires": 0, "version": "base"}\n')
    module_name = "publication_host_" + uuid.uuid4().hex
    (repo / (module_name + ".py")).write_text(_HOST_HOOKS.format(prefix=prefix))
    (repo / "agent_registry.json").write_text(
        json.dumps(
            {
                "repo_root": prefix,
                "agents": {
                    "a": {"skill_dir": "skills/a"},
                    "b": {"skill_dir": "skills/b"},
                },
            }
        )
    )
    git("add", ".")
    git("commit", "-m", "base A0+B0")
    git("update-ref", "refs/remotes/origin/main", "HEAD")
    # The host has a staged A change followed by a different worktree A,
    # plus unstaged B. A plain stash pop would lose this index split.
    a_path.write_text('{"capability": 2}\n')
    git("add", str(a_path))
    a_path.write_text('{"capability": 1}\n')
    b_path.write_text('{"requires": 1, "version": "current-B2"}\n')
    (run / "v1_b_skill.md").write_text(
        json.dumps({"requires": selected_requires, "version": "selected-B1"})
        + "\n"
    )
    (run / "mode").write_text(mode)
    monkeypatch.setenv("AGENT_REGISTRY", "agent_registry.json")
    monkeypatch.setenv("EVOLUTION_WORKDIR", str(repo))
    monkeypatch.setenv("EVOLUTION_HOOKS", module_name)
    monkeypatch.setenv("EVOLUTION_PUBLISH", "true")
    monkeypatch.setenv("GATE_POLICY", "require")
    monkeypatch.syspath_prepend(str(repo))
    pushes = []
    prs = []

    def network_stub(cmd, *args, **kwargs):
      if list(cmd[:2]) == ["git", "push"]:
        pushes.append(
            {
                "command": list(cmd),
                "head": git("rev-parse", "HEAD"),
                "tree": git("rev-parse", "HEAD^{tree}"),
                "paths": git(
                    "diff-tree", "--no-commit-id", "--name-only", "-r", "HEAD"
                ).splitlines(),
                "content": git(
                    "show", "HEAD:" + b_path.relative_to(repo).as_posix()
                ),
            }
        )
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
      if list(cmd[:3]) == ["gh", "pr", "create"]:
        prs.append(list(cmd))
        return subprocess.CompletedProcess(
            cmd, 0, stdout="https://github.com/fixture/host/pull/7\n", stderr=""
        )
      return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", network_stub)
    return dict(
        repo=repo,
        run=run,
        a=a_path,
        b=b_path,
        git=git,
        pushes=pushes,
        prs=prs,
        network_stub=network_stub,
    )

  return build


def _state(host):
  git = host["git"]
  return {
      "head": git("rev-parse", "HEAD"),
      "branch": git("rev-parse", "--abbrev-ref", "HEAD"),
      "status": git("status", "--porcelain", "--untracked-files=no"),
      "staged": git("diff", "--cached"),
      "unstaged": git("diff"),
      "stash": git("stash", "list"),
      "a": host["a"].read_text(),
      "b": host["b"].read_text(),
  }


def _publish(host):
  return tools.create_evolution_pr(str(host["run"]), version="v1", agent="b")


def test_publication_gate_rejects_standalone_coevolution_dependency(
    publication_repo,
):
  host = publication_repo()
  before = _state(host)
  assert (
      json.loads(before["a"])["capability"]
      >= json.loads(before["b"])["requires"]
  )
  result = _publish(host)
  observed = json.loads((host["run"] / "gate.json").read_text())
  assert observed["a"]["capability"] == 0
  assert observed["b"] == {"requires": 1, "version": "selected-B1"}
  assert observed["answer_available"] is False
  assert observed["status"] == ""
  assert result["status"] == "refused_by_gate"
  assert not host["pushes"] and not host["prs"]
  assert _state(host) == before


def test_publication_nested_registry_gate_and_push_see_identical_selected_tree(
    publication_repo,
):
  host = publication_repo(prefix="services/foo", selected_requires=0)
  before = _state(host)
  result = _publish(host)
  assert result["status"] == "success", result
  observed = json.loads((host["run"] / "gate.json").read_text())
  (pushed,) = host["pushes"]
  assert observed["root"] == str(host["repo"].resolve())
  assert observed["status"] == ""
  assert observed["answer_available"] is True
  assert observed["head"] == pushed["head"]
  assert observed["tree"] == pushed["tree"]
  assert observed["version"] == "v1" and observed["agent"] == "b"
  assert pushed["paths"] == ["services/foo/skills/b/SKILL.md"]
  assert json.loads(pushed["content"]) == {
      "requires": 0,
      "version": "selected-B1",
  }
  published = json.loads((host["run"] / "publish.json").read_text())
  assert published["skill_dir"] == str(host["b"].parent.resolve())
  assert published["content"] == (host["run"] / "v1_b_skill.md").read_text()
  assert len(host["prs"]) == 1
  assert _state(host) == before


@pytest.mark.parametrize("mode", ["mutate", "stage", "commit", "raise"])
def test_publication_gate_failure_restores_index_and_host_tree(
    publication_repo, mode
):
  host = publication_repo(mode=mode, selected_requires=0)
  before = _state(host)
  result = _publish(host)
  assert result["status"] == (
      "error" if mode == "raise" else "refused_by_gate"
  ), result
  assert not host["pushes"] and not host["prs"]
  assert _state(host) == before


def test_publication_restores_detached_original_head(publication_repo):
  host = publication_repo(selected_requires=0)
  host["git"]("checkout", "--detach")
  before = _state(host)
  result = _publish(host)
  assert result["status"] == "success", result
  assert before["branch"] == "HEAD"
  assert _state(host) == before


def test_publication_failed_stash_does_not_switch_or_drop_host_changes(
    publication_repo, monkeypatch
):
  host = publication_repo(selected_requires=0)
  before = _state(host)

  def fail_stash(cmd, *args, **kwargs):
    if list(cmd[:3]) == ["git", "stash", "push"]:
      return subprocess.CompletedProcess(
          cmd, 1, stdout="", stderr="fixture stash failure"
      )
    return host["network_stub"](cmd, *args, **kwargs)

  monkeypatch.setattr(subprocess, "run", fail_stash)
  result = _publish(host)
  assert result["status"] == "error", result
  assert "stash" in result["error"]
  assert not host["pushes"] and not host["prs"]
  assert not (host["run"] / "gate.json").exists()
  assert _state(host) == before


def test_publication_commit_failure_restores_host_before_gate(publication_repo):
  host = publication_repo(selected_requires=0)
  before = _state(host)
  hook = host["repo"] / ".git/hooks/pre-commit"
  hook.write_text("#!/bin/sh\nexit 1\n")
  hook.chmod(0o755)
  result = _publish(host)
  assert result["status"] == "error", result
  assert "git commit failed" in result["error"]
  assert not host["pushes"] and not host["prs"]
  assert not (host["run"] / "gate.json").exists()
  assert _state(host) == before


def test_publication_preserves_preexisting_stash(publication_repo):
  host = publication_repo(selected_requires=0)
  git = host["git"]
  git("stash", "push", "-m", "preexisting host stash")
  host["a"].write_text('{"capability": 3}\n')
  git("add", str(host["a"]))
  before = _state(host)
  assert "preexisting host stash" in before["stash"]
  result = _publish(host)
  assert result["status"] == "success", result
  assert _state(host) == before


def test_publication_explicit_skip_without_hook_remains_available(
    publication_repo, monkeypatch
):
  host = publication_repo()
  monkeypatch.delenv("EVOLUTION_HOOKS")
  monkeypatch.setenv("GATE_POLICY", "skip")
  before = _state(host)
  result = _publish(host)
  assert result["status"] == "success", result
  assert not (host["run"] / "gate.json").exists()
  assert len(host["pushes"]) == 1
  assert _state(host) == before


def test_publication_inconclusive_gate_keeps_documented_behavior(
    publication_repo,
):
  host = publication_repo(mode="none", selected_requires=0)
  before = _state(host)
  result = _publish(host)
  assert result["status"] == "success", result
  assert (host["run"] / "gate.json").exists()
  assert len(host["pushes"]) == 1
  assert _state(host) == before
