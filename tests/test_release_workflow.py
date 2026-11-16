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

"""Tests for `.github/workflows/release.yml` and `twine-requirements.lock`."""

import hashlib
import json
import os
import pathlib
import re
import shlex
import subprocess
import sys

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
RELEASE_WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"
CI_WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
TWINE_LOCK = ROOT / ".github" / "workflows" / "twine-requirements.lock"
BASH = ("bash", "--noprofile", "--norc", "-eo", "pipefail")


def _load_yaml(path):
  return yaml.safe_load(path.read_text(encoding="utf-8"))


def _step_by_id(job, step_id):
  return next(s for s in job["steps"] if s.get("id") == step_id)


def _step_by_name(job, substr):
  return next(s for s in job["steps"] if substr in (s.get("name") or ""))


def test_twine_lockfile_pins_twine_7_and_modern_packaging():
  """twine>=7.0.0 and packaging>=26.1 are required for Metadata-Version 2.5."""
  text = TWINE_LOCK.read_text(encoding="utf-8")
  twine_match = re.search(r"(?m)^twine==(\d+)\.(\d+)\.(\d+)\b", text)
  pkg_match = re.search(r"(?m)^packaging==(\d+)\.(\d+)\b", text)

  assert twine_match is not None, "twine is not pinned in lockfile"
  assert int(twine_match.group(1)) >= 7, (
      f"twine=={twine_match.group(0)} does not support Metadata-Version 2.5"
  )
  assert pkg_match is not None, "packaging is not pinned in lockfile"
  assert (int(pkg_match.group(1)), int(pkg_match.group(2))) >= (26, 1)


def test_ci_and_release_build_jobs_run_twine_check_strict():
  """Both ci.yml and release.yml validate distributions with twine check."""
  lock_arg = "-r .github/workflows/twine-requirements.lock"
  for wf_path in (CI_WORKFLOW, RELEASE_WORKFLOW):
    wf = _load_yaml(wf_path)
    build_steps = wf["jobs"]["build"]["steps"]
    runs = "\n".join(s.get("run") or "" for s in build_steps)
    assert f"--require-hashes --no-deps {lock_arg}" in runs
    assert "python -m twine check --strict dist/*" in runs


def test_publish_pypi_installs_pinned_tools_before_google_auth():
  """Supply-chain guard: Twine and Cloud SDK are installed before WIF auth."""
  wf = _load_yaml(RELEASE_WORKFLOW)
  steps = wf["jobs"]["publish-pypi"]["steps"]
  twine_idx = next(
      i
      for i, s in enumerate(steps)
      if "twine-requirements.lock" in (s.get("run") or "")
  )
  gcloud_prefix = "google-github-actions/setup-gcloud@"
  gcloud_idx = next(
      i
      for i, s in enumerate(steps)
      if str(s.get("uses", "")).startswith(gcloud_prefix)
  )
  auth_idx = next(
      i
      for i, s in enumerate(steps)
      if str(s.get("uses", "")).startswith("google-github-actions/auth@")
  )

  assert twine_idx < auth_idx
  assert gcloud_idx < auth_idx
  gcloud_version = (steps[gcloud_idx].get("with") or {}).get("version", "")
  assert gcloud_version and gcloud_version != "latest"


class _ReleaseHarness:
  """Offline harness with stub `gcloud`, `curl`, and `python`."""

  def __init__(self, tmp_path):
    self.tmp_path = tmp_path
    self.work_dir = tmp_path / "work"
    self.dist_dir = self.work_dir / "dist"
    self.bin_dir = tmp_path / "bin"
    self.log_file = tmp_path / "calls.log"
    self.gh_output = tmp_path / "github_output.txt"
    self.pypi_response = tmp_path / "pypi_mock.json"
    self.ar_describe_err = tmp_path / "ar_describe.err"

    for d in (self.work_dir, self.dist_dir, self.bin_dir):
      d.mkdir(parents=True)

    (self.work_dir / "pyproject.toml").write_text(
        '[project]\nname = "bigquery-agent-analytics"\nversion = "0.6.2"\n',
        encoding="utf-8",
    )
    self.wheel_bytes = b"fake-wheel-payload-0.6.2"
    self.sdist_bytes = b"fake-sdist-payload-0.6.2"
    whl_name = "bigquery_agent_analytics-0.6.2-py3-none-any.whl"
    (self.dist_dir / whl_name).write_bytes(self.wheel_bytes)
    (self.dist_dir / "bigquery_agent_analytics-0.6.2.tar.gz").write_bytes(
        self.sdist_bytes
    )

    self._write_stubs()

  @property
  def matching_pypi_payload(self):
    return {
        "urls": [
            {
                "filename": "bigquery_agent_analytics-0.6.2-py3-none-any.whl",
                "digests": {
                    "sha256": hashlib.sha256(self.wheel_bytes).hexdigest()
                },
            },
            {
                "filename": "bigquery_agent_analytics-0.6.2.tar.gz",
                "digests": {
                    "sha256": hashlib.sha256(self.sdist_bytes).hexdigest()
                },
            },
        ]
    }

  def _write_stubs(self):
    log_q = shlex.quote(str(self.log_file))
    err_q = shlex.quote(str(self.ar_describe_err))
    pypi_q = shlex.quote(str(self.pypi_response))
    py_q = shlex.quote(sys.executable)

    python_stub = self.bin_dir / "python"
    python_stub.write_text(
        f"""#!/bin/sh
if [ "$1" = "-m" ] && [ "$2" = "twine" ]; then
  echo "twine $*" >> {log_q}
  exit "${{MOCK_TWINE_RC:-0}}"
fi
exec {py_q} "$@"
""",
        encoding="utf-8",
    )
    python_stub.chmod(0o755)

    gcloud_stub = self.bin_dir / "gcloud"
    gcloud_stub.write_text(
        f"""#!/bin/sh
echo "gcloud $*" >> {log_q}
if [ "$1" = "auth" ] && [ "$2" = "print-access-token" ]; then
  if [ "${{MOCK_TOKEN_RC:-0}}" -ne 0 ]; then
    echo "ERROR: (gcloud.auth.print-access-token) failed" >&2
    exit "${{MOCK_TOKEN_RC}}"
  fi
  echo "${{MOCK_TOKEN:-stub-oauth-token}}"
  exit 0
fi
if [ "$3" = "describe" ]; then
  if [ -f {err_q} ]; then
    cat {err_q} >&2
  fi
  exit "${{MOCK_DESCRIBE_RC:-0}}"
fi
if [ "$3" = "delete" ]; then
  exit "${{MOCK_DELETE_RC:-0}}"
fi
exit 1
""",
        encoding="utf-8",
    )
    gcloud_stub.chmod(0o755)

    curl_stub = self.bin_dir / "curl"
    curl_stub.write_text(
        f"""#!/bin/sh
echo "curl $*" >> {log_q}
for arg in "$@"; do
  case "$arg" in
    https://pypi.org/pypi/*)
      cp {pypi_q} pypi_version.json 2>/dev/null || true
      printf '%s' "${{MOCK_PYPI_HTTP:-404}}"
      exit "${{MOCK_PYPI_CURL_RC:-0}}"
      ;;
    https://storage.googleapis.com/upload/storage/v1/b/*)
      exit "${{MOCK_GCS_POST_RC:-0}}"
      ;;
  esac
done
exit 1
""",
        encoding="utf-8",
    )
    curl_stub.chmod(0o755)

  def run_step(self, step_script, **extra_env):
    script_file = self.tmp_path / "step.sh"
    script_file.write_text(step_script, encoding="utf-8")
    env = dict(
        os.environ,
        PATH=f"{self.bin_dir}{os.pathsep}{os.environ.get('PATH', os.defpath)}",
        GITHUB_OUTPUT=str(self.gh_output),
        TAG="v0.6.2",
        EVENT_NAME="release",
        DRY_RUN="false",
        RUN_ID="1001",
        RUN_ATTEMPT="1",
    )
    env.update({k: str(v) for k, v in extra_env.items()})
    return subprocess.run(
        [*BASH, str(script_file)],
        cwd=self.work_dir,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

  def calls(self):
    if not self.log_file.exists():
      return []
    return self.log_file.read_text(encoding="utf-8").splitlines()


@pytest.fixture
def harness(tmp_path):
  return _ReleaseHarness(tmp_path)


@pytest.fixture
def publish_job():
  return _load_yaml(RELEASE_WORKFLOW)["jobs"]["publish-pypi"]


def test_fresh_release_stages_and_uploads_manifest_via_single_post(
    harness, publish_job
):
  cleanup_script = _step_by_id(publish_job, "cleanup")["run"]
  stage_script = _step_by_name(publish_job, "Stage distributions")["run"]
  harness.ar_describe_err.write_text(
      "ERROR: (gcloud.artifacts.versions.describe) NOT_FOUND: "
      "Requested entity was not found.\n",
      encoding="utf-8",
  )

  r1 = harness.run_step(
      cleanup_script, MOCK_PYPI_HTTP="404", MOCK_DESCRIBE_RC="1"
  )
  assert r1.returncode == 0, r1.stdout + r1.stderr
  assert "skip_stage=false" in harness.gh_output.read_text(encoding="utf-8")

  r2 = harness.run_step(stage_script, VERSION="0.6.2", SKIP_STAGE="false")
  assert r2.returncode == 0, r2.stdout + r2.stderr

  calls = harness.calls()
  assert not any("versions delete" in c for c in calls)
  assert any("twine upload" in c for c in calls)
  expected_post = (
      "uploadType=media&name=bigquery-agent-analytics/pypi/manifests/"
      "manifest-0.6.2-1001-1.json"
  )
  assert any(expected_post in c for c in calls)
  manifest = json.loads(
      (harness.work_dir / "manifest.json").read_text(encoding="utf-8")
  )
  assert manifest == {
      "publish_all": False,
      "publishing_groups": [
          {
              "packages": [
                  {"name": "bigquery-agent-analytics", "version": "0.6.2"}
              ]
          }
      ],
  }


def test_already_published_version_with_identical_digests_skips_cleanly(
    harness, publish_job
):
  cleanup_script = _step_by_id(publish_job, "cleanup")["run"]
  harness.pypi_response.write_text(
      json.dumps(harness.matching_pypi_payload), encoding="utf-8"
  )

  r1 = harness.run_step(cleanup_script, MOCK_PYPI_HTTP="200")
  assert r1.returncode == 0, r1.stdout + r1.stderr
  assert "skip_stage=true" in harness.gh_output.read_text(encoding="utf-8")
  assert not any("artifacts versions" in c for c in harness.calls())


def test_already_published_version_with_different_digests_fails(
    harness, publish_job
):
  cleanup_script = _step_by_id(publish_job, "cleanup")["run"]
  tampered = harness.matching_pypi_payload
  tampered["urls"][0]["digests"]["sha256"] = "0" * 64
  harness.pypi_response.write_text(json.dumps(tampered), encoding="utf-8")

  r1 = harness.run_step(cleanup_script, MOCK_PYPI_HTTP="200")
  assert r1.returncode != 0
  assert "already exists on PyPI with different artifact digests" in r1.stderr
  assert not any("artifacts versions" in c for c in harness.calls())


def test_workflow_dispatch_dry_run_performs_lookups_without_mutating(
    harness, publish_job
):
  cleanup_script = _step_by_id(publish_job, "cleanup")["run"]
  stage_script = _step_by_name(publish_job, "Stage distributions")["run"]

  r1 = harness.run_step(
      cleanup_script,
      EVENT_NAME="workflow_dispatch",
      DRY_RUN="true",
      MOCK_PYPI_HTTP="404",
      MOCK_DESCRIBE_RC="0",
  )
  assert r1.returncode == 0, r1.stdout + r1.stderr
  assert "[DRY RUN]" in r1.stdout
  assert not any("versions delete" in c for c in harness.calls())

  r2 = harness.run_step(stage_script, DRY_RUN="true", SKIP_STAGE="false")
  assert r2.returncode == 0
  assert not any("twine upload" in c for c in harness.calls())


def test_command_not_found_exit_127_is_not_treated_as_absent(
    harness, publish_job
):
  cleanup_script = _step_by_id(publish_job, "cleanup")["run"]
  harness.ar_describe_err.write_text(
      "gcloud: command not found\n", encoding="utf-8"
  )

  r1 = harness.run_step(
      cleanup_script, MOCK_PYPI_HTTP="404", MOCK_DESCRIBE_RC="127"
  )
  assert r1.returncode == 127


def test_failed_token_minting_stops_staging_before_twine_runs(
    harness, publish_job
):
  stage_script = _step_by_name(publish_job, "Stage distributions")["run"]

  r2 = harness.run_step(
      stage_script, VERSION="0.6.2", SKIP_STAGE="false", MOCK_TOKEN_RC="1"
  )
  assert r2.returncode != 0
  assert not any("twine upload" in c for c in harness.calls())
