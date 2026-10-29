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

"""The plugin's launchers keep the working directory off sys.path.

Every hook runs in the directory the agent session was started in,
typically a checked-out repository, and so does the ``/bqaa-setup``
helper; the drainer a hook spawns inherits that directory. Python can
import from it in two ways:

* ``python -m`` and ``python -c`` put it first on ``sys.path``. Python
  3.11+ leaves it out under ``PYTHONSAFEPATH``, but 3.10 ignores that
  variable, so these tests also run the launchers through an
  interpreter wrapper that drops it, as 3.10 effectively does.
* An empty ``PYTHONPATH`` entry (a leading, trailing or doubled ``:``)
  stands for it, and the interpreter searches ``PYTHONPATH`` for
  ``sitecustomize`` as it starts, before any of the package's code.

A planted ``json.py`` (imported while the package loads) or
``sitecustomize.py`` (imported at start-up) records each process that
ran it.
"""

from __future__ import annotations

import json
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import time

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_DIR = REPO_ROOT / "plugins" / "claude_code"
PACKAGE_SRC = (
    REPO_ROOT / "producers" / "src" / "bigquery_agent_analytics_tracing"
)

# Planted in the working directory. PLANTED_ROLE marks the drainer.
_PLANTED_MODULE = """\
import os
with open(os.environ["PLANTED_SENTINEL"], "a") as sentinel:
  sentinel.write(os.environ.get("PLANTED_ROLE", "launcher") + "\\n")
"""
PLANTED_NAMES = ["json.py", "sitecustomize.py"]

HOOK = "hooks/session_start.sh"
SETUP = "scripts/run_setup_check.sh"


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


def _write_script(path, body):
  path.write_text("#!/bin/bash\n" + body, encoding="utf-8")
  path.chmod(0o755)
  return str(path)


@pytest.fixture(params=["python", "safe-path-ignored"])
def interpreter(request, tmp_path):
  """BQAA_PYTHON: the test interpreter, or a wrapper that drops
  PYTHONSAFEPATH before starting it, which is the Python 3.10 case."""
  if request.param == "python":
    return sys.executable
  python = shlex.quote(sys.executable)
  return _write_script(
      tmp_path / "python-safe-path-ignored",
      f'exec env -u PYTHONSAFEPATH {python} "$@"\n',
  )


def _run(tmp_path, launcher, *args, python, planted, pythonpath=None, **kw):
  """Run ``launcher`` from a directory holding the planted module.

  ``pythonpath`` is the inherited PYTHONPATH (None: unset).
  """
  repo = tmp_path / "repo"
  repo.mkdir(exist_ok=True)
  (repo / planted).write_text(_PLANTED_MODULE, encoding="utf-8")
  env = {
      "PATH": "/usr/bin:/bin",
      "TMPDIR": str(tmp_path),
      "PLANTED_SENTINEL": str(tmp_path / "planted-ran"),
      "BQAA_PYTHON": python,
      "BQAA_PROJECT_ID": "synthetic-project",
      "BQAA_DATASET": "synthetic_dataset",
      "BQAA_DRY_RUN": "true",
      "BQAA_LOG_FILE": str(tmp_path / "bqaa.log"),
      "BQAA_SPOOL_DIR": str(tmp_path / "spool"),
      "BQAA_STATE_DIR": str(tmp_path / "state"),
      **kw.pop("env", {}),
  }
  if pythonpath is not None:
    env["PYTHONPATH"] = pythonpath
  return subprocess.run(
      ["bash", str(launcher), *args],
      env=env,
      cwd=repo,
      capture_output=True,
      text=True,
      timeout=60,
      **kw,
  )


def _planted_runs(tmp_path):
  """The processes that ran the planted module, in order."""
  sentinel = tmp_path / "planted-ran"
  return sentinel.read_text().split() if sentinel.exists() else []


def _fire_hook(tmp_path, plugin_root, **kw):
  payload = json.dumps({"session_id": "planted", "cwd": "repo"})
  return _run(tmp_path, plugin_root / HOOK, input=payload, **kw)


def _assert_hook_ran(tmp_path, result):
  assert result.returncode == 0, f"stderr={result.stderr!r}"
  # SessionStart records state: proof the hook ran the vendored package.
  assert (tmp_path / "state" / "state_planted.json").is_file()


def _assert_setup_ran(result):
  assert result.returncode == 0, f"stderr={result.stderr!r}"
  report = json.loads(result.stdout)
  # The import probes ran in the same directory, and the package resolved.
  assert report["package_ok"] is True
  assert report["all_ok"] is True


@pytest.mark.parametrize("planted", PLANTED_NAMES)
def test_hook_does_not_import_modules_from_the_working_directory(
    tmp_path, plugin_root, interpreter, planted
):
  result = _fire_hook(
      tmp_path, plugin_root, python=interpreter, planted=planted
  )
  ran = _planted_runs(tmp_path)
  assert not ran, f"the hook imported {planted} from its working directory"
  _assert_hook_ran(tmp_path, result)


@pytest.mark.parametrize("planted", PLANTED_NAMES)
def test_setup_check_does_not_import_modules_from_the_working_directory(
    tmp_path, plugin_root, interpreter, planted
):
  result = _run(
      tmp_path,
      plugin_root / SETUP,
      "--json",
      python=interpreter,
      planted=planted,
  )
  ran = _planted_runs(tmp_path)
  assert not ran, f"/bqaa-setup imported {planted} from its working directory"
  _assert_setup_ran(result)


# Inherited PYTHONPATH values with an empty entry: {deps} is a real
# dependency directory the caller meant to add.
_EMPTY_ENTRY_FORMS = {
    "leading": ":{deps}",
    "trailing": "{deps}:",
    "repeated": "{deps}::{deps}",
}


@pytest.mark.parametrize("form", list(_EMPTY_ENTRY_FORMS))
@pytest.mark.parametrize("launcher", [HOOK, SETUP])
def test_launchers_ignore_empty_entries_in_inherited_pythonpath(
    tmp_path, plugin_root, launcher, form
):
  deps = tmp_path / "deps"
  deps.mkdir()
  pythonpath = _EMPTY_ENTRY_FORMS[form].format(deps=deps)
  kw = dict(
      python=sys.executable, planted="sitecustomize.py", pythonpath=pythonpath
  )
  if launcher == HOOK:
    result = _fire_hook(tmp_path, plugin_root, **kw)
  else:
    result = _run(tmp_path, plugin_root / SETUP, "--json", **kw)
  ran = _planted_runs(tmp_path)
  assert not ran, (
      f"{launcher} ran sitecustomize.py from its working directory with a"
      f" {form} ':' in PYTHONPATH"
  )
  if launcher == HOOK:
    _assert_hook_ran(tmp_path, result)
  else:
    _assert_setup_ran(result)


# Inherited PYTHONPATH -> the PYTHONPATH every interpreter start gets:
# vendor/, then the caller's non-empty entries in order and as given.
# <deps> stands for a dependency directory with a space in its name.
_PYTHONPATH_CASES = {
    "unset": (None, "<vendor>"),
    "empty": ("", "<vendor>"),
    "leading": (":<deps>", "<vendor>:<deps>"),
    "trailing": ("<deps>:", "<vendor>:<deps>"),
    "repeated": ("<deps>::relative/lib", "<vendor>:<deps>:relative/lib"),
    "all three": (":<deps>::relative/lib:", "<vendor>:<deps>:relative/lib"),
}


@pytest.mark.parametrize("case", list(_PYTHONPATH_CASES))
@pytest.mark.parametrize("launcher", [HOOK, SETUP])
def test_launchers_start_every_interpreter_without_empty_pythonpath_entries(
    tmp_path, plugin_root, launcher, case
):
  """Covers the setup helper's liveness check, which starts the
  interpreter before the setup module itself runs."""
  inherited, want = _PYTHONPATH_CASES[case]
  paths = {
      "<vendor>": str(plugin_root / "vendor"),
      "<deps>": str(tmp_path / "trusted deps"),
  }
  if inherited is not None:
    inherited = inherited.replace("<deps>", paths["<deps>"])
  record = tmp_path / "pythonpath.log"
  recorder = _write_script(
      tmp_path / "recorder",
      f"printf '%s\\n' \"${{PYTHONPATH-<unset>}}\" >> {shlex.quote(str(record))}\n",
  )
  _run(
      tmp_path,
      plugin_root / launcher,
      python=recorder,
      planted="sitecustomize.py",
      pythonpath=inherited,
      input="{}",
  )
  got = record.read_text().splitlines()
  for name, path in paths.items():
    got = [line.replace(path, name) for line in got]
  starts = 1 if launcher == HOOK else 2  # setup: liveness check, module
  assert got == [want] * starts


@pytest.mark.parametrize("planted", PLANTED_NAMES)
def test_drainer_does_not_import_modules_from_the_working_directory(
    tmp_path, plugin_root, planted
):
  """The real hooks spool a row and spawn the real drainer, through an
  interpreter that ignores PYTHONSAFEPATH and with empty entries in the
  inherited PYTHONPATH. The drainer loads the package and its config,
  then stops at its dry-run check, before any BigQuery client exists."""
  deps = tmp_path / "deps"
  deps.mkdir()
  drainer_log = tmp_path / "drainer.log"
  real = shlex.quote(sys.executable)
  python = _write_script(
      tmp_path / "python-for-hooks",
      "unset PYTHONSAFEPATH\n"
      'case "$*" in\n'
      "  *bigquery_agent_analytics_tracing.drain*)\n"
      f'    BQAA_DRY_RUN=true PLANTED_ROLE=drainer {real} "$@"\n'
      f'    echo "exit $?" >> {shlex.quote(str(drainer_log))}\n'
      "    exit 0 ;;\n"
      "esac\n"
      f'exec {real} "$@"\n',
  )
  live = {"BQAA_DRY_RUN": "false", "BQAA_DIRECT_WRITE": "false"}
  for hook, payload in (
      ("session_start.sh", {"session_id": "drn", "cwd": "repo"}),
      (
          "user_prompt_submit.sh",
          {"session_id": "drn", "prompt": "hi", "transcript_path": ""},
      ),
  ):
    _run(
        tmp_path,
        plugin_root / "hooks" / hook,
        python=python,
        planted=planted,
        pythonpath=f":{deps}::{deps}:",
        input=json.dumps(payload),
        env=live,
    )
  spooled = list((tmp_path / "spool").glob("event-*.json"))
  if spooled:  # The drainer was spawned detached; let it finish.
    deadline = time.monotonic() + 30
    while not drainer_log.exists() and time.monotonic() < deadline:
      time.sleep(0.05)
  ran = _planted_runs(tmp_path)
  assert not ran, f"{planted} from the working directory ran in: {ran}"
  assert spooled, "the prompt hook spooled no row"
  drained = drainer_log.read_text() if drainer_log.exists() else "not run"
  assert drained == "exit 0\n", f"drainer: {drained!r}"
