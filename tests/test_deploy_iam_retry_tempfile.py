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

"""``_retry_iam`` in the deploy scripts captures stderr in a private temp file.

The helper used to redirect each IAM grant's stderr to ``/tmp/_iam_err.$$``:
a name any local user can predict from the PID and pre-create. These tests
extract the shell function from each script and run it with a stub command,
so no gcloud call is made.
"""

from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest

_REPO = Path(__file__).resolve().parents[1]
_SCRIPTS = [
    _REPO / "deploy" / "skill_evolution_job" / "deploy.sh",
    _REPO
    / "examples"
    / "context_graph"
    / "periodic_materialization"
    / "deploy_cloud_run_job.sh",
]

pytestmark = [
    pytest.mark.skipif(
        shutil.which("bash") is None, reason="bash not available"
    ),
    pytest.mark.skipif(sys.platform == "win32", reason="POSIX /tmp semantics"),
    pytest.mark.parametrize("script", _SCRIPTS, ids=lambda p: p.name),
]

# Waits for the test to plant files before the first attempt, skips the
# retry backoff, and pins a permissive umask so the file mode is meaningful.
_HARNESS = """
{function}
sleep() {{ :; }}
umask 022
read -r _
_retry_iam "$@"
"""


def _retry_iam_source(script: Path) -> str:
  match = re.search(
      r"^_retry_iam\(\) \{\n.*?^\}\n", script.read_text(), re.M | re.S
  )
  assert match, f"_retry_iam not found in {script}"
  return match.group(0)


def _run_retry_iam(script, tmp_path, stub_code, before_start=None):
  """Runs ``_retry_iam <python> -c <stub_code>``; returns the result."""
  tmpdir = tmp_path / "tmpdir"
  tmpdir.mkdir()
  env = {**os.environ, "TMPDIR": str(tmpdir)}
  proc = subprocess.Popen(
      [
          "bash",
          "-c",
          _HARNESS.format(function=_retry_iam_source(script)),
          "bash",
          sys.executable,
          "-c",
          stub_code,
      ],
      stdin=subprocess.PIPE,
      stdout=subprocess.PIPE,
      stderr=subprocess.PIPE,
      text=True,
      env=env,
  )
  try:
    if before_start is not None:
      before_start(proc.pid)
    stdout, stderr = proc.communicate("go\n", timeout=60)
  finally:
    if proc.poll() is None:
      proc.kill()
      proc.wait()
  return proc.returncode, stdout, stderr, tmpdir


def test_planted_symlink_at_predictable_path_is_not_followed(
    script, tmp_path
) -> None:
  victim = tmp_path / "victim.txt"
  victim.write_text("victim data\n")
  planted = []

  def plant(pid):
    # A local user who predicts the deploy shell's PID plants a symlink.
    path = Path(f"/tmp/_iam_err.{pid}")
    if os.path.lexists(path):
      pytest.skip(f"{path} already exists")
    path.symlink_to(victim)
    planted.append(path)

  try:
    returncode, _, _, tmpdir = _run_retry_iam(
        script,
        tmp_path,
        "import sys; sys.stderr.write('Updated IAM policy.\\n')",
        before_start=plant,
    )
  finally:
    for path in planted:
      if path.is_symlink():
        path.unlink()

  assert returncode == 0
  assert victim.read_text() == "victim data\n"
  assert list(tmpdir.iterdir()) == []


def test_stderr_capture_is_private_and_removed(script, tmp_path) -> None:
  report = tmp_path / "stderr_mode.txt"
  stub = (
      "import os, stat, sys; "
      f"open({str(report)!r}, 'w').write("
      "oct(stat.S_IMODE(os.fstat(2).st_mode))); "
      "sys.stderr.write('PERMISSION_DENIED: boom\\n'); sys.exit(1)"
  )

  returncode, _, stderr, tmpdir = _run_retry_iam(script, tmp_path, stub)

  assert returncode == 1
  assert "PERMISSION_DENIED: boom" in stderr
  assert report.read_text() == "0o600"
  assert list(tmpdir.iterdir()) == []


def test_retries_iam_propagation_error_then_succeeds(script, tmp_path) -> None:
  counter = tmp_path / "attempts"
  stub = (
      "import pathlib, sys; "
      f"p = pathlib.Path({str(counter)!r}); "
      "n = int(p.read_text()) if p.exists() else 0; "
      "p.write_text(str(n + 1)); "
      "n == 0 and sys.stderr.write("
      "'Service account x does not exist.\\n') and sys.exit(1)"
  )

  returncode, _, stderr, tmpdir = _run_retry_iam(script, tmp_path, stub)

  assert returncode == 0, stderr
  assert counter.read_text() == "2"
  assert list(tmpdir.iterdir()) == []
