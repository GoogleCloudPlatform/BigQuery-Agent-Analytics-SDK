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

"""Executed tests for regen-locks.sh guard sections (#356 round 9):
the canonical-base parser and the transactional copyback run VERBATIM
from the script, with failure injection."""

import hashlib
import pathlib
import subprocess

_SCRIPT = (
    pathlib.Path(__file__).parent.parent.parent
    / "deploy/otlp_receiver/regen-locks.sh"
).read_text()


def _section(name):
  start = _SCRIPT.index(f"# --{name}-start--")
  end = _SCRIPT.index(f"# --{name}-end--")
  return _SCRIPT[start:end]


def _bash(snippet, prelude, cwd):
  return subprocess.run(
      ["bash", "-c", f"set -euo pipefail\n{prelude}\n{snippet}"],
      cwd=cwd,
      capture_output=True,
      text=True,
  )


class TestCanonicalBaseRef:

  def test_single_reference_is_accepted(self, tmp_path):
    df = tmp_path / "Dockerfile"
    ref = "python:3.12-slim@sha256:" + "a" * 64
    df.write_text(f"ARG PYTHON_BASE={ref}\nFROM ${{PYTHON_BASE}} AS b\n")
    r = _bash(
        _section("base-ref") + '\necho "BASE=$BASE"',
        f'DOCKERFILE="{df}"',
        tmp_path,
    )
    assert r.returncode == 0, r.stderr
    assert f"BASE={ref}" in r.stdout

  def test_divergent_stage_references_are_refused(self, tmp_path):
    df = tmp_path / "Dockerfile"
    df.write_text(
        "FROM python:3.12-slim@sha256:" + "a" * 64 + " AS builder\n"
        "FROM python:3.12-slim@sha256:" + "b" * 64 + "\n"
    )
    r = _bash(_section("base-ref"), f'DOCKERFILE="{df}"', tmp_path)
    assert r.returncode != 0
    assert "not a single canonical value" in r.stderr


class TestTransactionalCopyback:

  def _setup(self, tmp_path):
    out = tmp_path / "out"
    out.mkdir()
    dests = {}
    for name, key in (
        ("pip-tools.lock", "DEST_PIP_TOOLS"),
        ("requirements.lock", "DEST_REQUIREMENTS"),
        ("build-requirements.lock", "DEST_BUILD"),
    ):
      (out / name).write_text(f"new {name}\n")
      dest = tmp_path / f"dest-{name}"
      dest.write_text(f"original {name}\n")
      dests[key] = dest
    prelude = f'OUT="{out}"\n' + "\n".join(
        f'{key}="{path}"' for key, path in dests.items()
    )
    return out, dests, prelude

  def test_happy_path_replaces_all_three(self, tmp_path):
    _, dests, prelude = self._setup(tmp_path)
    r = _bash(_section("copyback"), prelude, tmp_path)
    assert r.returncode == 0, r.stderr
    for path in dests.values():
      assert path.read_text().startswith("new ")
      assert not pathlib.Path(str(path) + ".bqaa-bak").exists()
      assert not pathlib.Path(str(path) + ".bqaa-new").exists()

  def test_injected_failure_leaves_every_destination_unchanged(self, tmp_path):
    out, dests, prelude = self._setup(tmp_path)
    # Inject: delete one staged source so the SECOND stage copy fails
    # after the first destination already has a staged file.
    (out / "requirements.lock").unlink()
    before = {
        key: hashlib.sha256(path.read_bytes()).hexdigest()
        for key, path in dests.items()
    }
    r = _bash(_section("copyback"), prelude, tmp_path)
    assert r.returncode != 0
    after = {
        key: hashlib.sha256(path.read_bytes()).hexdigest()
        for key, path in dests.items()
    }
    assert before == after, "a failed copyback mutated a destination"
    for path in dests.values():
      assert not pathlib.Path(str(path) + ".bqaa-bak").exists()
      assert not pathlib.Path(str(path) + ".bqaa-new").exists()
