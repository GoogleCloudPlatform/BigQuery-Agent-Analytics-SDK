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

"""Advisory setup check for the BQAA tracing runtime.

Surfaces:

  * BQAA_PROJECT_ID / BQAA_DATASET — required env vars.
  * BQAA_PYTHON — optional, defaults to the current interpreter; we
    sanity-check it can import the runtime deps.
  * google-cloud-bigquery — always required.
  * google-cloud-bigquery-storage + pyarrow — required only for the
    Storage Write API path; absent is OK (drainer falls back to
    insert_rows_json).
  * bigquery_agent_analytics_tracing itself — sanity check the
    vendored layout works on BQAA_PYTHON.

This script is **advisory-only**. It never mutates the environment,
shell rc files, GCP resources, or IAM. It only reports what is
missing and prints the exact `pip install ...` line you'd run to fix
it. The interactive setup that actually creates a dataset / grants
IAM is intentionally out of scope here — keep that as a separate
explicit step.

Driver: the `bqaa-check-setup` console script and the `/bqaa-setup`
Claude Code slash command both call ``main()``.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import field
import json
import os
import subprocess
import sys
from typing import Iterable

# Modules whose absence blocks the writer entirely.
_REQUIRED_IMPORTS: tuple[tuple[str, str], ...] = (
    ("google.cloud.bigquery", "google-cloud-bigquery"),
)

# Modules whose absence only disables the Storage Write API path;
# drainer falls back to insert_rows_json.
_STORAGE_WRITE_IMPORTS: tuple[tuple[str, str], ...] = (
    ("google.cloud.bigquery_storage_v1", "google-cloud-bigquery-storage"),
    ("pyarrow", "pyarrow"),
)

# Sanity-check the tracing package itself imports on the target
# interpreter — catches a misconfigured PYTHONPATH on vendored
# plugin runtimes before any hook fires.
_PACKAGE_IMPORTS: tuple[tuple[str, str], ...] = (
    ("bigquery_agent_analytics_tracing", "bigquery-agent-analytics-tracing"),
)


@dataclass
class _ImportProbe:
  module: str
  install_name: str
  ok: bool
  error: str | None = None


@dataclass
class _SetupReport:
  python_bin: str
  project_id: str | None
  dataset: str | None
  trace_enabled: bool
  # Non-None when the configured interpreter cannot be executed
  # (FileNotFoundError, permission denied, timeout, non-zero exit on a
  # trivial command). Surfacing this separately tells the user "fix
  # BQAA_PYTHON" instead of N misleading "import failed" lines that
  # all stem from the same root cause.
  interpreter_error: str | None = None
  package_imports: list[_ImportProbe] = field(default_factory=list)
  required_imports: list[_ImportProbe] = field(default_factory=list)
  storage_write_imports: list[_ImportProbe] = field(default_factory=list)

  @property
  def python_ok(self) -> bool:
    return self.interpreter_error is None

  @property
  def env_ok(self) -> bool:
    return bool(self.project_id) and bool(self.dataset)

  @property
  def package_ok(self) -> bool:
    # Empty list when interpreter is broken — explicitly gate on
    # python_ok so vacuous all([]) doesn't report True.
    return self.python_ok and all(p.ok for p in self.package_imports)

  @property
  def required_deps_ok(self) -> bool:
    return self.python_ok and all(p.ok for p in self.required_imports)

  @property
  def storage_write_ok(self) -> bool:
    return self.python_ok and all(p.ok for p in self.storage_write_imports)

  @property
  def all_ok(self) -> bool:
    return (
        self.python_ok
        and self.env_ok
        and self.package_ok
        and self.required_deps_ok
    )


def _resolve_python_bin() -> str:
  """Pick the interpreter we'll check against.

  ``BQAA_PYTHON`` is what hook shell wrappers exec when firing the
  hook, so that's the truth for the runtime. If unset, use whichever
  python invoked us (matches the shell default of ``python3``).
  """
  return os.environ.get("BQAA_PYTHON") or sys.executable


def _probe_interpreter(python_bin: str) -> str | None:
  """Verify ``python_bin`` can be executed. Returns None on success or
  a one-line error string. Catches the common
  misconfigurations (path doesn't exist, not executable, hangs)
  before the import probes shell out N times and trip on the same
  root cause.
  """
  try:
    proc = subprocess.run(
        [python_bin, "-c", "pass"],
        capture_output=True,
        text=True,
        timeout=15,
    )
  except FileNotFoundError:
    return f"interpreter not found: {python_bin}"
  except PermissionError:
    return f"interpreter not executable: {python_bin}"
  except OSError as exc:
    return f"interpreter exec error: {python_bin} ({exc})"
  except subprocess.TimeoutExpired:
    return f"interpreter timed out: {python_bin}"
  if proc.returncode != 0:
    err = (proc.stderr or proc.stdout or "non-zero exit").strip()
    err = err.splitlines()[-1] if err.splitlines() else err
    return f"interpreter exited non-zero: {err}"
  return None


def _probe_imports(
    python_bin: str, modules: Iterable[tuple[str, str]]
) -> list[_ImportProbe]:
  results: list[_ImportProbe] = []
  for module, install_name in modules:
    # Defensive catches for the same conditions ``_probe_interpreter``
    # handles. ``collect_report`` skips this loop entirely when the
    # interpreter is broken, but keep the catches so a direct caller
    # never sees a raw OSError from a misconfigured python_bin.
    try:
      proc = subprocess.run(
          [python_bin, "-c", f"import {module}"],
          capture_output=True,
          text=True,
          timeout=15,
      )
    except (FileNotFoundError, PermissionError, OSError) as exc:
      results.append(
          _ImportProbe(
              module=module,
              install_name=install_name,
              ok=False,
              error=f"interpreter unavailable: {exc}",
          )
      )
      continue
    except subprocess.TimeoutExpired:
      results.append(
          _ImportProbe(
              module=module,
              install_name=install_name,
              ok=False,
              error=f"interpreter timed out: {python_bin}",
          )
      )
      continue
    if proc.returncode == 0:
      results.append(
          _ImportProbe(module=module, install_name=install_name, ok=True)
      )
    else:
      # The first nonempty stderr line is the most useful — strip
      # the traceback noise.
      err = (proc.stderr or proc.stdout or "import failed").strip()
      err = err.splitlines()[-1] if err.splitlines() else err
      results.append(
          _ImportProbe(
              module=module,
              install_name=install_name,
              ok=False,
              error=err,
          )
      )
  return results


def collect_report(python_bin: str | None = None) -> _SetupReport:
  """Run the full set of checks against ``python_bin`` (or BQAA_PYTHON
  fallback) and return a structured report.

  If the interpreter can't be executed (bad ``BQAA_PYTHON`` is the most
  common case), skip the import probes — they'd all fail with the same
  underlying error — and surface the interpreter problem directly
  via ``_SetupReport.interpreter_error``.
  """
  resolved = python_bin or _resolve_python_bin()
  interpreter_error = _probe_interpreter(resolved)
  if interpreter_error is not None:
    package_imports: list[_ImportProbe] = []
    required_imports: list[_ImportProbe] = []
    storage_write_imports: list[_ImportProbe] = []
  else:
    package_imports = _probe_imports(resolved, _PACKAGE_IMPORTS)
    required_imports = _probe_imports(resolved, _REQUIRED_IMPORTS)
    storage_write_imports = _probe_imports(resolved, _STORAGE_WRITE_IMPORTS)
  return _SetupReport(
      python_bin=resolved,
      interpreter_error=interpreter_error,
      project_id=(
          os.environ.get("BQAA_PROJECT_ID")
          or os.environ.get("GCP_PROJECT_ID")
          or os.environ.get("GOOGLE_CLOUD_PROJECT")
          or None
      ),
      dataset=(
          os.environ.get("BQAA_DATASET") or os.environ.get("BQ_DATASET") or None
      ),
      trace_enabled=(
          os.environ.get("BQAA_TRACE_ENABLED", "true").lower() == "true"
      ),
      package_imports=package_imports,
      required_imports=required_imports,
      storage_write_imports=storage_write_imports,
  )


def _format_report(report: _SetupReport) -> str:
  """Human-readable, copy-pasteable advisory output."""
  lines: list[str] = []
  ok_mark = "OK"
  miss_mark = "MISSING"

  lines.append("BQAA tracing setup check")
  lines.append("=" * 32)
  lines.append(f"Python interpreter: {report.python_bin}")
  if report.interpreter_error:
    lines.append(f"  [{miss_mark}] {report.interpreter_error}")
    lines.append(
        "  Fix: point BQAA_PYTHON at a working python3 interpreter,"
        " or unset it to use the default."
    )
  lines.append(f"BQAA_TRACE_ENABLED: {'on' if report.trace_enabled else 'off'}")
  if not report.trace_enabled:
    lines.append(
        "  Note: emission is disabled. Set BQAA_TRACE_ENABLED=true"
        " (or unset it) to enable."
    )
  lines.append("")

  if report.interpreter_error:
    # Skip the per-import sections — they're all empty when the
    # interpreter is broken, and printing empty checklists is noise.
    lines.append("Import + env checks skipped: interpreter unavailable.")
    lines.append("")
    lines.append("Status: ACTION NEEDED")
    return "\n".join(lines) + "\n"

  lines.append("Required env vars:")
  lines.append(
      f"  [{ok_mark if report.project_id else miss_mark}]"
      f" BQAA_PROJECT_ID = {report.project_id or '(unset)'}"
  )
  lines.append(
      f"  [{ok_mark if report.dataset else miss_mark}]"
      f" BQAA_DATASET    = {report.dataset or '(unset)'}"
  )
  if not report.env_ok:
    lines.append("")
    lines.append("Fix: export the missing variables, e.g.")
    if not report.project_id:
      lines.append("  export BQAA_PROJECT_ID=your-gcp-project")
    if not report.dataset:
      lines.append("  export BQAA_DATASET=agent_analytics")
  lines.append("")

  lines.append("Package import:")
  for probe in report.package_imports:
    mark = ok_mark if probe.ok else miss_mark
    line = f"  [{mark}] import {probe.module}"
    if not probe.ok and probe.error:
      line += f" — {probe.error}"
    lines.append(line)
  if not report.package_ok:
    lines.append("")
    lines.append(
        "Fix: install the tracing wheel on BQAA_PYTHON, or point"
        " PYTHONPATH at the vendored plugin's vendor/ directory."
    )
    lines.append(
        f"  {report.python_bin} -m pip install bigquery-agent-analytics-tracing"
    )
  lines.append("")

  lines.append("Required runtime deps:")
  for probe in report.required_imports:
    mark = ok_mark if probe.ok else miss_mark
    line = f"  [{mark}] import {probe.module}"
    if not probe.ok and probe.error:
      line += f" — {probe.error}"
    lines.append(line)
  if not report.required_deps_ok:
    missing = [p.install_name for p in report.required_imports if not p.ok]
    lines.append("")
    lines.append(f"Fix: {report.python_bin} -m pip install {' '.join(missing)}")
  lines.append("")

  lines.append("Storage Write API deps (optional):")
  for probe in report.storage_write_imports:
    mark = ok_mark if probe.ok else miss_mark
    line = f"  [{mark}] import {probe.module}"
    if not probe.ok and probe.error:
      line += f" — {probe.error}"
    lines.append(line)
  if not report.storage_write_ok:
    missing = [p.install_name for p in report.storage_write_imports if not p.ok]
    lines.append("")
    lines.append(
        "Optional fix for the lower-latency Storage Write path"
        " (drainer falls back to insert_rows_json without these):"
    )
    lines.append(f"  {report.python_bin} -m pip install {' '.join(missing)}")
  lines.append("")

  lines.append("Status: " + ("READY" if report.all_ok else "ACTION NEEDED"))
  return "\n".join(lines) + "\n"


def _report_to_json(report: _SetupReport) -> str:
  return json.dumps(
      {
          **asdict(report),
          "python_ok": report.python_ok,
          "env_ok": report.env_ok,
          "package_ok": report.package_ok,
          "required_deps_ok": report.required_deps_ok,
          "storage_write_ok": report.storage_write_ok,
          "all_ok": report.all_ok,
      },
      indent=2,
      sort_keys=True,
  )


def main(argv: list[str] | None = None) -> int:
  """Exit 0 when everything required is in place, 1 otherwise.

  The non-zero exit is what lets the slash command flag "fix this"
  vs. "you're good." Storage Write deps are intentionally not part
  of the exit code: the drainer fallback exists so a missing pyarrow
  is not a hard blocker.
  """
  parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
  parser.add_argument(
      "--json",
      action="store_true",
      help="Emit a JSON report instead of human-readable text.",
  )
  args = parser.parse_args(argv)
  report = collect_report()
  if args.json:
    sys.stdout.write(_report_to_json(report) + "\n")
  else:
    sys.stdout.write(_format_report(report))
  return 0 if report.all_ok else 1


if __name__ == "__main__":
  raise SystemExit(main())
