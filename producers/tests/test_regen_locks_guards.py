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


REF = "python:3.12-slim@sha256:" + "a" * 64
GOOD_DOCKERFILE = (
    f"ARG PYTHON_BASE={REF}\n"
    "FROM ${PYTHON_BASE} AS builder\n"
    "FROM ${PYTHON_BASE}\n"
)


class TestCanonicalBaseRef:
  """Consumers are validated, not merely a matching literal — review
  round 10 replaced both stages with ubuntu while the unused ARG kept
  passing the old check."""

  def _check(self, tmp_path, dockerfile):
    df = tmp_path / "Dockerfile"
    df.write_text(dockerfile)
    return _bash(
        _section("base-ref") + '\necho "BASE=$BASE"',
        f'DOCKERFILE="{df}"',
        tmp_path,
    )

  def test_canonical_arg_with_two_consuming_stages_is_accepted(self, tmp_path):
    r = self._check(tmp_path, GOOD_DOCKERFILE)
    assert r.returncode == 0, r.stderr
    assert f"BASE={REF}" in r.stdout

  def test_stages_ignoring_the_arg_are_refused(self, tmp_path):
    r = self._check(
        tmp_path,
        f"ARG PYTHON_BASE={REF}\n"
        "FROM ubuntu:latest AS builder\nFROM ubuntu:latest\n",
    )
    assert r.returncode != 0

  def test_unpinned_arg_is_refused(self, tmp_path):
    r = self._check(
        tmp_path,
        "ARG PYTHON_BASE=python:3.12-slim\n"
        "FROM ${PYTHON_BASE} AS builder\nFROM ${PYTHON_BASE}\n",
    )
    assert r.returncode != 0
    assert "not digest-pinned" in r.stderr

  def test_second_variable_stage_is_refused(self, tmp_path):
    r = self._check(
        tmp_path,
        f"ARG PYTHON_BASE={REF}\nARG OTHER={REF}\n"
        "FROM ${PYTHON_BASE} AS builder\nFROM ${OTHER}\n",
    )
    assert r.returncode != 0

  def test_duplicate_arg_declarations_are_refused(self, tmp_path):
    r = self._check(
        tmp_path,
        f"ARG PYTHON_BASE={REF}\nARG PYTHON_BASE={REF}\n"
        "FROM ${PYTHON_BASE} AS builder\nFROM ${PYTHON_BASE}\n",
    )
    assert r.returncode != 0

  def test_wrong_stage_count_is_refused(self, tmp_path):
    for stages in (
        "FROM ${PYTHON_BASE}\n",
        "FROM ${PYTHON_BASE} AS a\nFROM ${PYTHON_BASE} AS b\n"
        "FROM ${PYTHON_BASE}\n",
    ):
      r = self._check(tmp_path, f"ARG PYTHON_BASE={REF}\n{stages}")
      assert r.returncode != 0, stages


class TestTransactionalCopyback:

  def _setup(self, tmp_path):
    tmp_path.mkdir(parents=True, exist_ok=True)
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

  def test_signal_after_any_rename_never_mixes_generations(self, tmp_path):
    # Reproduced in review: TERM immediately after the first backup
    # deletion exited 0 with one new + two old locks. Inject TERM after
    # every rename and every cleanup deletion; the result must always
    # be all-old (pre-commit, nonzero exit) or all-new (post-commit).
    for nth in (1, 2, 3):  # TERM after the nth rename (pre/at commit)
      out, dests, prelude = self._setup(tmp_path / f"mv{nth}")
      hook = (
          f'MV_CALLS=0\nmv() {{ command mv "$@"; MV_CALLS=$((MV_CALLS+1));'
          f' if [ "$MV_CALLS" = "{nth}" ]; then kill -TERM $$; fi; }}'
      )
      r = _bash(_section("copyback"), prelude + "\n" + hook, tmp_path)
      contents = {k: path.read_text() for k, path in dests.items()}
      kinds = {v.split()[0] for v in contents.values()}
      assert len(kinds) == 1, f"mixed generations after mv#{nth}: {contents}"
      if kinds == {"original"}:
        assert r.returncode != 0
      else:
        assert kinds == {"new"}

    for nth in (1, 2, 3):  # TERM during the nth backup cleanup (post-commit)
      out, dests, prelude = self._setup(tmp_path / f"rm{nth}")
      hook = (
          f'RM_CALLS=0\nrm() {{ command rm "$@"; RM_CALLS=$((RM_CALLS+1));'
          f' if [ "$RM_CALLS" = "{nth}" ]; then kill -TERM $$; fi; }}'
      )
      _bash(_section("copyback"), prelude + "\n" + hook, tmp_path)
      contents = {k: path.read_text() for k, path in dests.items()}
      assert all(
          v.startswith("new") for v in contents.values()
      ), f"post-commit TERM undid the transaction at rm#{nth}: {contents}"

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


class TestBoundedConvergence:
  """The fixed-point loop with a stubbed generator: multi-phase
  convergence succeeds; bounded exhaustion fails WITH preserved
  diagnostics (a rerun from an unchanged seed is a dead end without
  them)."""

  def _run(self, tmp_path, stub_body, max_phases=5):
    repo = tmp_path / "repo"
    (repo / "deploy/otlp_receiver").mkdir(parents=True)
    (repo / "deploy/otlp_receiver/pip-tools.lock").write_text("# seed\nv1\n")
    out = tmp_path / "out"
    out.mkdir()
    home = tmp_path / "home"
    (home / ".cache").mkdir(parents=True)
    stub = tmp_path / "docker-stub"
    stub.write_text("#!/usr/bin/env bash\n" + stub_body)
    stub.chmod(0o755)
    prelude = (
        f'REPO_ROOT="{repo}"\nOUT="{out}"\nBASE=ignored\n'
        f'HOME="{home}"\nDOCKER="{stub}"\nMAX_PHASES={max_phases}\n'
        f'STUB_STATE="{tmp_path}/calls"\nexport STUB_STATE'
    )
    return out, home, _bash(_section("convergence"), prelude, tmp_path)

  def test_multi_phase_convergence_succeeds(self, tmp_path):
    # The stub resolves v1 -> v2 -> v3 -> v3: needs three phases.
    stub = """
N=$(cat "$STUB_STATE" 2>/dev/null || echo 0); N=$((N+1)); echo $N > "$STUB_STATE"
OUT_DIR=$(echo "$@" | grep -oE '[^ ]+:/out' | cut -d: -f1)
SEED=$(grep -v '^#' "$OUT_DIR/seed.lock")
case "$SEED" in
  v1) echo "v2" > "$OUT_DIR/candidate.lock" ;;
  v2) echo "v3" > "$OUT_DIR/candidate.lock" ;;
  *)  echo "$SEED" > "$OUT_DIR/candidate.lock" ;;
esac
"""
    out, home, r = self._run(tmp_path, stub)
    assert r.returncode == 0, r.stderr
    assert (out / "pip-tools.lock").read_text().strip() == "v3"

  def test_bounded_exhaustion_preserves_diagnostics(self, tmp_path):
    # The stub never converges: every phase produces a new value.
    stub = """
N=$(cat "$STUB_STATE" 2>/dev/null || echo 0); N=$((N+1)); echo $N > "$STUB_STATE"
OUT_DIR=$(echo "$@" | grep -oE '[^ ]+:/out' | cut -d: -f1)
echo "v$N-unique" > "$OUT_DIR/candidate.lock"
"""
    out, home, r = self._run(tmp_path, stub, max_phases=3)
    assert r.returncode != 0
    assert "did not converge within 3" in r.stderr
    diag_dirs = list((home / ".cache").glob("bqaa-lock-diagnostics.*"))
    assert diag_dirs, "diagnostics were not preserved"
    kept = {f.name for f in diag_dirs[0].iterdir()}
    assert {"previous-seed.lock", "candidate.lock"} <= kept
    # The reviewer reproduced the erased delta: reseeding overwrote the
    # seed with the candidate, so both preserved locks were byte-equal.
    # The diagnostics must be the last two DISTINCT resolutions — the
    # final phase's input and its output.
    previous = (diag_dirs[0] / "previous-seed.lock").read_text().strip()
    candidate = (diag_dirs[0] / "candidate.lock").read_text().strip()
    assert previous != candidate, "diagnostic locks are byte-identical"
    assert previous == "v2-unique"  # phase 2's output = phase 3's input
    assert candidate == "v3-unique"  # phase 3's output

  def test_first_phase_exhaustion_keeps_the_checked_in_seed(self, tmp_path):
    # MAX_PHASES=1: the preserved pair is the checked-in seed and the
    # single candidate it produced.
    stub = """
N=$(cat "$STUB_STATE" 2>/dev/null || echo 0); N=$((N+1)); echo $N > "$STUB_STATE"
OUT_DIR=$(echo "$@" | grep -oE '[^ ]+:/out' | cut -d: -f1)
echo "v$N-unique" > "$OUT_DIR/candidate.lock"
"""
    out, home, r = self._run(tmp_path, stub, max_phases=1)
    assert r.returncode != 0
    diag_dirs = list((home / ".cache").glob("bqaa-lock-diagnostics.*"))
    assert diag_dirs
    previous = (diag_dirs[0] / "previous-seed.lock").read_text()
    candidate = (diag_dirs[0] / "candidate.lock").read_text().strip()
    assert "v1" in previous  # the checked-in seed body
    assert candidate == "v1-unique"
    assert previous.strip() != candidate
