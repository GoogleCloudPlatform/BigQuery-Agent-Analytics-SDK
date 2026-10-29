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

"""The plugin's shell launchers keep the working directory off sys.path.

Every hook runs in the directory the agent session was started in,
typically a checked-out repository, and so does the ``/bqaa-setup``
helper. ``python -m`` prepends that directory to ``sys.path``, and so
does an empty ``PYTHONPATH`` entry, so a ``json.py`` at the root of a
repository would be imported in place of the standard library module,
and run, by every hook event. The interpreter also searches an empty
``PYTHONPATH`` entry for ``sitecustomize`` when it starts.
"""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_DIR = REPO_ROOT / "plugins" / "claude_code"
PACKAGE_SRC = (
    REPO_ROOT / "producers" / "src" / "bigquery_agent_analytics_tracing"
)

# Planted in the working directory; it only records that it ran.
_PLANTED_MODULE = """\
import os
with open(os.environ["PLANTED_SENTINEL"], "w") as sentinel:
  sentinel.write("imported from the working directory")
"""

_PLANTED_NAMES = [
    # A standard library module the package imports while it loads.
    # Only PYTHONSAFEPATH keeps ``python -m`` from finding it here.
    pytest.param(
        "json.py",
        marks=pytest.mark.skipif(
            sys.version_info < (3, 11),
            reason="PYTHONSAFEPATH, which drops the python -m entry, needs 3.11+",
        ),
    ),
    # Imported at interpreter start-up; found here only through an empty
    # PYTHONPATH entry, on every Python version.
    "sitecustomize.py",
]


@pytest.fixture
def plugin_root(tmp_path):
  """The plugin tree with the package vendored the way the build does it
  (``build_claude_plugin.vendor_package``), outside the checkout."""
  root = tmp_path / "plugin"
  shutil.copytree(PLUGIN_DIR, root, ignore=shutil.ignore_patterns("vendor"))
  shutil.copytree(
      PACKAGE_SRC,
      root / "vendor" / PACKAGE_SRC.name,
      ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
  )
  return root


def _run_in_planted_repo(tmp_path, planted, launcher, *args, payload=""):
  """Run ``launcher`` from a directory holding the planted module.

  PYTHONPATH is left unset, as in a default install. Returns the
  completed process and whether the planted module ran.
  """
  repo = tmp_path / "repo"
  repo.mkdir()
  (repo / planted).write_text(_PLANTED_MODULE, encoding="utf-8")
  sentinel = tmp_path / "planted-module-ran"
  env = {
      "PATH": "/usr/bin:/bin",
      "TMPDIR": str(tmp_path),
      "PLANTED_SENTINEL": str(sentinel),
      "BQAA_PYTHON": sys.executable,
      "BQAA_PROJECT_ID": "synthetic-project",
      "BQAA_DATASET": "synthetic_dataset",
      "BQAA_DRY_RUN": "true",
      "BQAA_LOG_FILE": str(tmp_path / "bqaa.log"),
      "BQAA_SPOOL_DIR": str(tmp_path / "spool"),
      "BQAA_STATE_DIR": str(tmp_path / "state"),
  }
  result = subprocess.run(
      ["bash", str(launcher), *args],
      env=env,
      cwd=repo,
      input=payload,
      capture_output=True,
      text=True,
      timeout=30,
  )
  return result, sentinel.exists()


@pytest.mark.parametrize("planted", _PLANTED_NAMES)
def test_hook_does_not_import_modules_from_the_working_directory(
    tmp_path, plugin_root, planted
):
  payload = json.dumps({"session_id": "planted", "cwd": "repo"})
  result, planted_ran = _run_in_planted_repo(
      tmp_path,
      planted,
      plugin_root / "hooks" / "session_start.sh",
      payload=payload,
  )
  assert (
      not planted_ran
  ), f"the hook imported {planted} from its working directory"
  assert result.returncode == 0, f"stderr={result.stderr!r}"
  # SessionStart records state: proof the hook ran the vendored package.
  assert (tmp_path / "state" / "state_planted.json").is_file()


@pytest.mark.parametrize("planted", _PLANTED_NAMES)
def test_setup_check_does_not_import_modules_from_the_working_directory(
    tmp_path, plugin_root, planted
):
  result, planted_ran = _run_in_planted_repo(
      tmp_path,
      planted,
      plugin_root / "scripts" / "run_setup_check.sh",
      "--json",
  )
  assert (
      not planted_ran
  ), f"/bqaa-setup imported {planted} from its working directory"
  assert result.returncode == 0, f"stderr={result.stderr!r}"
  report = json.loads(result.stdout)
  # The probes ran in the same directory, and the package still resolved.
  assert report["package_ok"] is True
  assert report["all_ok"] is True
