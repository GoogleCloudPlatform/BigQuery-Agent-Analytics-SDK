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

"""Tests for the Claude Code plugin artifact build script.

Covers:
  * vendor copy lands at the expected path
  * manifest version is stamped from package metadata
  * tarball contains the manifest, hooks, and vendored package
  * a vendored plugin layout can actually fire a hook (PYTHONPATH +
    python -m bigquery_agent_analytics_tracing.claude_code)
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile

import pytest

# scripts/ lives next to tests/ under producers/, but is not on
# sys.path by default — pyproject only puts src/ there. Append it so
# the build module is importable from tests.
_PRODUCERS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_PRODUCERS_DIR / "scripts"))

import build_claude_plugin  # noqa: E402  (after sys.path tweak)

REPO_ROOT = _PRODUCERS_DIR.parent
PLUGIN_DIR = REPO_ROOT / "plugins" / "claude_code"
MANIFEST_PATH = PLUGIN_DIR / ".claude-plugin" / "plugin.json"


@pytest.fixture
def restore_manifest():
  """Snapshot the manifest before each test and restore after.

  Tests call ``build_claude_plugin.build()`` which mutates the on-disk
  manifest in place. Without this fixture the working copy would drift.
  """
  original = MANIFEST_PATH.read_text(encoding="utf-8")
  yield
  MANIFEST_PATH.write_text(original, encoding="utf-8")


@pytest.fixture
def cleanup_vendor():
  yield
  vendor_root = PLUGIN_DIR / "vendor"
  if vendor_root.exists():
    shutil.rmtree(vendor_root)


# ---------------------------------------------------------------------------
# resolve_version
# ---------------------------------------------------------------------------


def test_resolve_version_returns_a_string():
  v = build_claude_plugin.resolve_version()
  assert isinstance(v, str) and len(v) > 0


def test_resolve_version_matches_pyproject_when_package_uninstalled(
    monkeypatch,
):
  # Force the importlib.metadata branch to fail so the pyproject
  # fallback is exercised; matches what CI does in a fresh checkout
  # before `pip install -e .`.
  from importlib import metadata as _md

  def _raise(_name):
    raise _md.PackageNotFoundError("bigquery-agent-analytics-tracing")

  monkeypatch.setattr(build_claude_plugin.metadata, "version", _raise)

  try:
    import tomllib
  except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

  pyproject = _PRODUCERS_DIR / "pyproject.toml"
  with pyproject.open("rb") as fh:
    expected = tomllib.load(fh)["project"]["version"]
  assert build_claude_plugin.resolve_version() == expected


# ---------------------------------------------------------------------------
# vendor_package + stamp_manifest_version
# ---------------------------------------------------------------------------


def test_vendor_package_copies_module_files(cleanup_vendor):
  target = build_claude_plugin.vendor_package()

  assert target.is_dir()
  assert (target / "__init__.py").is_file()
  assert (target / "claude_code.py").is_file()
  assert (target / "drain.py").is_file()
  assert (target / "logger.py").is_file()
  assert (target / "schema.py").is_file()
  assert (target / "config.py").is_file()
  assert (target / "_utils.py").is_file()
  assert (target / "_writer_identity.py").is_file()


def test_vendor_package_ignores_pycache(cleanup_vendor, tmp_path):
  # Drop a fake __pycache__ under the source so we can verify it does
  # not get copied. Use a temp checkout root to avoid polluting the
  # live src/ tree.
  fake_src_root = tmp_path / "producers" / "src"
  fake_pkg = fake_src_root / build_claude_plugin.PACKAGE_IMPORT_NAME
  fake_pkg.mkdir(parents=True)
  (fake_pkg / "__init__.py").write_text("")
  (fake_pkg / "__pycache__").mkdir()
  (fake_pkg / "__pycache__" / "stale.pyc").write_bytes(b"\x00")

  # Run the copy step with PACKAGE_SRC pointed at the fake tree.
  src_backup = build_claude_plugin.PACKAGE_SRC
  build_claude_plugin.PACKAGE_SRC = fake_pkg
  try:
    target = build_claude_plugin.vendor_package()
    assert (target / "__init__.py").is_file()
    assert not (target / "__pycache__").exists()
  finally:
    build_claude_plugin.PACKAGE_SRC = src_backup


def test_write_vendor_dist_info_creates_pep_376_metadata(cleanup_vendor):
  # The METADATA file is what importlib.metadata.version() reads to
  # resolve a vendored install. Generate one alongside the vendored
  # package and verify shape.
  build_claude_plugin.vendor_package()
  dist_info = build_claude_plugin.write_vendor_dist_info("9.9.9-test")

  assert dist_info.name == (
      f"{build_claude_plugin.PACKAGE_IMPORT_NAME}-9.9.9-test.dist-info"
  )
  metadata_text = (dist_info / "METADATA").read_text(encoding="utf-8")
  assert "Metadata-Version: 2.1" in metadata_text
  assert f"Name: {build_claude_plugin.PACKAGE_DIST_NAME}" in metadata_text
  assert "Version: 9.9.9-test" in metadata_text


def test_vendor_package_wipes_stale_dist_info(cleanup_vendor):
  # Drop a stale dist-info to simulate an older build's leftovers,
  # then re-vendor and confirm only the fresh tree is present.
  build_claude_plugin.vendor_package()
  stale = (
      build_claude_plugin.PLUGIN_DIR
      / "vendor"
      / f"{build_claude_plugin.PACKAGE_IMPORT_NAME}-0.0.1-old.dist-info"
  )
  stale.mkdir()
  (stale / "METADATA").write_text(
      "Metadata-Version: 2.1\nName: x\nVersion: 0.0.1-old\n"
  )

  build_claude_plugin.vendor_package()

  assert not stale.exists(), (
      "stale dist-info from an older build must be cleared so"
      " importlib.metadata.version() does not see two distributions"
  )


def test_stamp_manifest_version_writes_resolved_version(restore_manifest):
  build_claude_plugin.stamp_manifest_version("9.9.9-test")

  written = json.loads(MANIFEST_PATH.read_text())
  assert written["version"] == "9.9.9-test"
  # Other manifest fields untouched.
  assert written["name"] == "bigquery-agent-analytics-tracing"
  assert "SessionStart" in written["hooks"]


# ---------------------------------------------------------------------------
# build() end-to-end
# ---------------------------------------------------------------------------


def test_build_produces_vendor_tree_and_tarball(
    tmp_path, restore_manifest, cleanup_vendor
):
  summary = build_claude_plugin.build(dist_dir=tmp_path / "dist")

  assert summary["version"] == build_claude_plugin.resolve_version()
  assert summary["manifest_version"] == summary["version"]
  assert Path(summary["vendor_target"]).is_dir()
  assert Path(summary["tar_path"]).is_file()

  # Tarball name has the version baked in.
  tar_path = Path(summary["tar_path"])
  assert summary["version"] in tar_path.name
  assert tar_path.name.startswith(build_claude_plugin.PLUGIN_ARTIFACT_PREFIX)

  # Tar contents include the manifest, hooks, and vendored module.
  with tarfile.open(tar_path, "r:gz") as tar:
    names = tar.getnames()
  assert any(n.endswith(".claude-plugin/plugin.json") for n in names)
  assert any(n.endswith("hooks/common.sh") for n in names)
  assert any(n.endswith("hooks/session_start.sh") for n in names)
  assert any(
      n.endswith("vendor/bigquery_agent_analytics_tracing/claude_code.py")
      for n in names
  )
  # PEP 376 .dist-info/METADATA must ride along so importlib.metadata
  # can resolve the vendored install's version at runtime.
  assert any(
      n.endswith(".dist-info/METADATA")
      and f"{build_claude_plugin.PACKAGE_IMPORT_NAME}-{summary['version']}" in n
      for n in names
  ), f"vendored .dist-info missing from tarball; names={names!r}"
  # __pycache__ excluded.
  assert not any("__pycache__" in n for n in names)
  # .gitignore excluded so marketplace users don't see internal-only files.
  assert not any(n.endswith("/.gitignore") for n in names)


def test_build_skip_tar_only_vendors_and_stamps(
    tmp_path, restore_manifest, cleanup_vendor
):
  summary = build_claude_plugin.build(dist_dir=tmp_path / "dist", skip_tar=True)
  assert summary["tar_path"] is None
  assert Path(summary["vendor_target"]).is_dir()
  assert not (tmp_path / "dist").exists()


# ---------------------------------------------------------------------------
# Real vendored-plugin hook fires through python -m without wheel install
# ---------------------------------------------------------------------------


def test_vendored_plugin_can_run_claude_hook_via_python_m(
    tmp_path, restore_manifest, cleanup_vendor
):
  """End-to-end sanity: build the plugin, then run the hook entry the
  same way ``hooks/common.sh`` does — PYTHONPATH=vendor + python -m —
  with no tracing wheel installed on the subprocess's Python. Mirrors
  the marketplace install path."""
  build_claude_plugin.build(dist_dir=tmp_path / "dist", skip_tar=True)

  log_file = tmp_path / "bqaa.log"
  spool_dir = tmp_path / "spool"
  state_dir = tmp_path / "state"
  vendor_root = PLUGIN_DIR / "vendor"

  env = {
      # PYTHONPATH points only at the vendor tree — no producers/src/
      # so the test exercises the marketplace install model.
      "PYTHONPATH": str(vendor_root),
      "BQAA_DRY_RUN": "true",
      "BQAA_LOG_FILE": str(log_file),
      "BQAA_SPOOL_DIR": str(spool_dir),
      "BQAA_STATE_DIR": str(state_dir),
      "PATH": "/usr/bin:/bin",
  }
  result = subprocess.run(
      [
          sys.executable,
          "-m",
          "bigquery_agent_analytics_tracing.claude_code",
          "SessionStart",
      ],
      env=env,
      input=json.dumps({"session_id": "vendored", "cwd": str(tmp_path)}),
      capture_output=True,
      text=True,
      timeout=15,
  )
  assert (
      result.returncode == 0
  ), f"stdout={result.stdout!r} stderr={result.stderr!r}"
  # SessionStart only sets state; check the state file landed.
  assert (state_dir / "state_vendored.json").is_file()


def test_vendored_plugin_can_run_user_prompt_submit_and_emit_row(
    tmp_path, restore_manifest, cleanup_vendor
):
  build_claude_plugin.build(dist_dir=tmp_path / "dist", skip_tar=True)

  log_file = tmp_path / "bqaa.log"
  spool_dir = tmp_path / "spool"
  state_dir = tmp_path / "state"
  vendor_root = PLUGIN_DIR / "vendor"

  base_env = {
      "PYTHONPATH": str(vendor_root),
      "BQAA_DRY_RUN": "true",
      "BQAA_LOG_FILE": str(log_file),
      "BQAA_SPOOL_DIR": str(spool_dir),
      "BQAA_STATE_DIR": str(state_dir),
      "PATH": "/usr/bin:/bin",
  }

  def _fire(hook, payload):
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "bigquery_agent_analytics_tracing.claude_code",
            hook,
        ],
        env=base_env,
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=15,
    )

  ss = _fire("SessionStart", {"session_id": "s1", "cwd": str(tmp_path)})
  assert ss.returncode == 0, ss.stderr
  ups = _fire(
      "UserPromptSubmit",
      {"session_id": "s1", "prompt": "hi", "transcript_path": ""},
  )
  assert ups.returncode == 0, ups.stderr

  # One LLM_REQUEST row should have landed in the dry-run log.
  text = log_file.read_text()
  rows = [
      json.loads(line.split("DRY_RUN ", 1)[1])
      for line in text.splitlines()
      if "DRY_RUN " in line
  ]
  assert any(
      r["event_type"] == "LLM_REQUEST" for r in rows
  ), f"no LLM_REQUEST in vendored-plugin log: {text}"


def test_vendored_plugin_spools_and_spawns_drainer_with_inherited_pythonpath(
    tmp_path, restore_manifest, cleanup_vendor
):
  """The full vendored runtime model: hook process imports from
  ``PYTHONPATH=vendor`` (no wheel install), the logger writes a real
  spool envelope (not dry-run), and the drainer subprocess is spawned
  with ``PYTHONPATH`` inherited via ``os.environ.copy()`` — so it can
  import the vendored package on its own.

  Uses a fake recorder as ``BQAA_PYTHON`` so the drainer spawn never
  has to talk to BigQuery. The recorder logs its argv + the inherited
  ``PYTHONPATH`` and exits 0; the test asserts both.
  """
  import time as _time

  build_claude_plugin.build(dist_dir=tmp_path / "dist", skip_tar=True)

  spool_dir = tmp_path / "spool"
  state_dir = tmp_path / "state"
  vendor_root = PLUGIN_DIR / "vendor"
  log_file = tmp_path / "bqaa.log"
  recorder_log = tmp_path / "drainer-recorder.log"
  recorder = tmp_path / "fake_drainer.sh"
  recorder.write_text(
      "#!/bin/bash\n"
      f"printf 'ARGS=%s\\n' \"$*\" >> {recorder_log}\n"
      f"printf 'PYTHONPATH=%s\\n' \"$PYTHONPATH\" >> {recorder_log}\n"
      "exit 0\n"
  )
  recorder.chmod(0o755)

  env = {
      "PYTHONPATH": str(vendor_root),
      # Real routing (not dry-run); project/dataset must be set so the
      # logger does not raise. The recorder catches the drainer spawn
      # before any BigQuery client is built.
      "BQAA_PROJECT_ID": "fake-project",
      "BQAA_DATASET": "fake_dataset",
      "BQAA_DRY_RUN": "false",
      "BQAA_DIRECT_WRITE": "false",
      "BQAA_SPOOL_DIR": str(spool_dir),
      "BQAA_STATE_DIR": str(state_dir),
      "BQAA_LOG_FILE": str(log_file),
      "BQAA_PYTHON": str(recorder),
      "PATH": "/usr/bin:/bin",
  }

  def _fire(hook, payload):
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "bigquery_agent_analytics_tracing.claude_code",
            hook,
        ],
        env=env,
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=15,
    )

  ss = _fire("SessionStart", {"session_id": "vp-d", "cwd": str(tmp_path)})
  assert ss.returncode == 0, ss.stderr
  ups = _fire(
      "UserPromptSubmit",
      {"session_id": "vp-d", "prompt": "hi", "transcript_path": ""},
  )
  assert ups.returncode == 0, ups.stderr

  # The hook is detached from the drainer spawn — give the recorder a
  # short window to flush its log line. ~3s ceiling so this never
  # silently hangs CI.
  deadline = _time.time() + 3.0
  while _time.time() < deadline:
    if recorder_log.exists() and recorder_log.read_text().strip():
      break
    _time.sleep(0.05)

  # 1. Real spool envelope was written (not dry-run path).
  spool_files = list(spool_dir.glob("event-*.json"))
  assert spool_files, (
      "hook should have written a spool envelope under non-dry-run routing;"
      f" log={log_file.read_text() if log_file.exists() else '(none)'}"
  )
  envelope = json.loads(spool_files[0].read_text())
  assert envelope["row"]["event_type"] == "LLM_REQUEST"
  assert envelope["config"]["project_id"] == "fake-project"

  # 1a. attributes.writer.version must reflect the resolved package
  # version, NOT the "0.0.0+local" fallback. This is the
  # adoption-query contract for marketplace installs: every row from
  # the vendored plugin reports the same version that the artifact
  # was built from.
  resolved_version = build_claude_plugin.resolve_version()
  writer = json.loads(envelope["row"]["attributes"])["writer"]
  assert writer["version"] == resolved_version, (
      f"vendored runtime stamped writer.version={writer['version']!r};"
      f" expected {resolved_version!r}. Likely the .dist-info under"
      " vendor/ is missing or stale."
  )
  assert writer["version"] != "0.0.0+local", (
      "writer.version fell back to local sentinel — vendored .dist-info"
      " resolution is broken"
  )
  assert writer["label"].endswith(f"/{resolved_version}")

  # 2. Drainer subprocess was actually spawned with the right -m args.
  assert (
      recorder_log.exists()
  ), "drainer spawn never fired the recorder — vendored runtime is broken"
  log_text = recorder_log.read_text()
  assert (
      "-m bigquery_agent_analytics_tracing.drain" in log_text
  ), f"drainer argv missing -m invocation: {log_text!r}"

  # 3. PYTHONPATH was inherited so the drainer can import the vendored
  # package without a wheel install — the exact runtime contract for
  # the marketplace path.
  assert f"PYTHONPATH={vendor_root}" in log_text, (
      f"drainer did not inherit PYTHONPATH={vendor_root!r};"
      f" got: {log_text!r}"
  )


def test_vendored_plugin_respects_trace_enabled_kill_switch_via_shell(
    tmp_path, restore_manifest, cleanup_vendor
):
  """common.sh checks ``BQAA_TRACE_ENABLED`` before launching python.
  Verify the shell exits 0 without spawning anything when it's off."""
  build_claude_plugin.build(dist_dir=tmp_path / "dist", skip_tar=True)

  common_sh = PLUGIN_DIR / "hooks" / "session_start.sh"
  assert common_sh.is_file()

  env = os.environ.copy()
  env.update(
      {
          "BQAA_TRACE_ENABLED": "false",
          "PYTHONPATH": str(PLUGIN_DIR / "vendor"),
          "BQAA_DRY_RUN": "true",
          "BQAA_LOG_FILE": str(tmp_path / "bqaa.log"),
          "BQAA_SPOOL_DIR": str(tmp_path / "spool"),
          "BQAA_STATE_DIR": str(tmp_path / "state"),
          # Force python_bin to a path that would error if invoked, to prove
          # the shell short-circuits before trying to exec it.
          "BQAA_PYTHON": "/nonexistent/python-that-would-fail",
      }
  )
  result = subprocess.run(
      ["bash", str(common_sh)],
      env=env,
      input="{}",
      capture_output=True,
      text=True,
      timeout=10,
  )
  assert result.returncode == 0, (
      f"BQAA_TRACE_ENABLED=false should short-circuit cleanly;"
      f" stderr={result.stderr!r}"
  )
  # No log file written because we never reached python.
  assert not (tmp_path / "bqaa.log").exists()
