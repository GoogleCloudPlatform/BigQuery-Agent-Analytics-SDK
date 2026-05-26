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

"""Tests for the advisory setup-check module."""

from __future__ import annotations

import io
import json
import sys

import pytest

from bigquery_agent_analytics_tracing import setup_check
from bigquery_agent_analytics_tracing.setup_check import _format_report
from bigquery_agent_analytics_tracing.setup_check import _ImportProbe
from bigquery_agent_analytics_tracing.setup_check import _resolve_python_bin
from bigquery_agent_analytics_tracing.setup_check import _SetupReport
from bigquery_agent_analytics_tracing.setup_check import collect_report
from bigquery_agent_analytics_tracing.setup_check import main

_ALL_BQAA_ENV = [
    "BQAA_PROJECT_ID",
    "BQAA_DATASET",
    "BQAA_PYTHON",
    "BQAA_TRACE_ENABLED",
    "GCP_PROJECT_ID",
    "GOOGLE_CLOUD_PROJECT",
    "BQ_DATASET",
]


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
  for name in _ALL_BQAA_ENV:
    monkeypatch.delenv(name, raising=False)
  yield


@pytest.fixture
def fake_probes(monkeypatch):
  """Replace the subprocess-driven probe with an in-memory mapping so
  tests don't shell out to python."""
  state: dict[str, dict[str, str | None]] = {"missing": {}, "extras": {}}

  def _fake_probe(python_bin, modules):
    results = []
    for module, install_name in modules:
      missing_map: dict[str, str | None] = state["missing"]
      err = missing_map.get(module)
      ok = module not in missing_map
      results.append(
          _ImportProbe(
              module=module,
              install_name=install_name,
              ok=ok,
              error=err if not ok else None,
          )
      )
    return results

  monkeypatch.setattr(setup_check, "_probe_imports", _fake_probe)
  return state


# ---------------------------------------------------------------------------
# _resolve_python_bin
# ---------------------------------------------------------------------------


def test_resolve_python_bin_prefers_bqaa_python(monkeypatch):
  monkeypatch.setenv("BQAA_PYTHON", "/custom/python")
  assert _resolve_python_bin() == "/custom/python"


def test_resolve_python_bin_falls_back_to_sys_executable():
  assert _resolve_python_bin() == sys.executable


# ---------------------------------------------------------------------------
# collect_report
# ---------------------------------------------------------------------------


def test_report_all_ok(monkeypatch, fake_probes):
  monkeypatch.setenv("BQAA_PROJECT_ID", "proj")
  monkeypatch.setenv("BQAA_DATASET", "ds")

  report = collect_report()

  assert report.env_ok
  assert report.package_ok
  assert report.required_deps_ok
  assert report.storage_write_ok
  assert report.all_ok
  assert report.trace_enabled is True


def test_report_env_missing(fake_probes):
  report = collect_report()
  assert not report.env_ok
  assert not report.all_ok
  assert report.project_id is None
  assert report.dataset is None


def test_report_falls_back_to_legacy_gcp_env_vars(monkeypatch, fake_probes):
  monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "legacy-proj")
  monkeypatch.setenv("BQ_DATASET", "legacy-ds")

  report = collect_report()

  assert report.project_id == "legacy-proj"
  assert report.dataset == "legacy-ds"
  assert report.env_ok


def test_report_required_dep_missing_breaks_all_ok(monkeypatch, fake_probes):
  monkeypatch.setenv("BQAA_PROJECT_ID", "p")
  monkeypatch.setenv("BQAA_DATASET", "d")
  fake_probes["missing"][
      "google.cloud.bigquery"
  ] = "ModuleNotFoundError: No module named 'google'"

  report = collect_report()

  assert not report.required_deps_ok
  assert not report.all_ok
  assert any(
      not p.ok and p.install_name == "google-cloud-bigquery"
      for p in report.required_imports
  )


def test_report_storage_write_missing_does_not_break_all_ok(
    monkeypatch, fake_probes
):
  monkeypatch.setenv("BQAA_PROJECT_ID", "p")
  monkeypatch.setenv("BQAA_DATASET", "d")
  fake_probes["missing"]["pyarrow"] = "No module named 'pyarrow'"

  report = collect_report()

  assert not report.storage_write_ok
  # all_ok is True because pyarrow is optional — drainer falls back to
  # insert_rows_json without it.
  assert report.all_ok


def test_report_package_import_missing_breaks_all_ok(monkeypatch, fake_probes):
  monkeypatch.setenv("BQAA_PROJECT_ID", "p")
  monkeypatch.setenv("BQAA_DATASET", "d")
  fake_probes["missing"][
      "bigquery_agent_analytics_tracing"
  ] = "ModuleNotFoundError"

  report = collect_report()

  assert not report.package_ok
  assert not report.all_ok


def test_report_trace_disabled_via_env(monkeypatch, fake_probes):
  monkeypatch.setenv("BQAA_PROJECT_ID", "p")
  monkeypatch.setenv("BQAA_DATASET", "d")
  monkeypatch.setenv("BQAA_TRACE_ENABLED", "false")

  report = collect_report()

  assert report.trace_enabled is False
  # Disabled trace doesn't change all_ok — that's about prerequisites,
  # not runtime intent.
  assert report.all_ok


# ---------------------------------------------------------------------------
# _format_report / main output
# ---------------------------------------------------------------------------


def test_format_report_includes_pip_install_hints_for_missing_required(
    monkeypatch, fake_probes
):
  monkeypatch.setenv("BQAA_PROJECT_ID", "p")
  monkeypatch.setenv("BQAA_DATASET", "d")
  fake_probes["missing"]["google.cloud.bigquery"] = "missing"

  text = _format_report(collect_report())

  assert "pip install google-cloud-bigquery" in text
  assert "ACTION NEEDED" in text


def test_format_report_includes_env_export_hints_for_missing_env(fake_probes):
  text = _format_report(collect_report())

  assert "export BQAA_PROJECT_ID=" in text
  assert "export BQAA_DATASET=" in text
  assert "ACTION NEEDED" in text


def test_format_report_ready_when_all_good(monkeypatch, fake_probes):
  monkeypatch.setenv("BQAA_PROJECT_ID", "p")
  monkeypatch.setenv("BQAA_DATASET", "d")

  text = _format_report(collect_report())

  assert "READY" in text
  assert "ACTION NEEDED" not in text


def test_format_report_warns_when_trace_disabled(monkeypatch, fake_probes):
  monkeypatch.setenv("BQAA_PROJECT_ID", "p")
  monkeypatch.setenv("BQAA_DATASET", "d")
  monkeypatch.setenv("BQAA_TRACE_ENABLED", "false")

  text = _format_report(collect_report())

  assert "BQAA_TRACE_ENABLED" in text
  assert "off" in text.lower()


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------


def test_main_exits_zero_when_ready(monkeypatch, fake_probes, capsys):
  monkeypatch.setenv("BQAA_PROJECT_ID", "p")
  monkeypatch.setenv("BQAA_DATASET", "d")

  rc = main(argv=[])

  assert rc == 0
  out = capsys.readouterr().out
  assert "READY" in out


def test_main_exits_one_when_required_missing(monkeypatch, fake_probes, capsys):
  monkeypatch.setenv("BQAA_PROJECT_ID", "p")
  # No BQAA_DATASET -> env_ok is False -> exit 1.
  rc = main(argv=[])
  assert rc == 1


def test_main_json_mode_emits_structured_report(
    monkeypatch, fake_probes, capsys
):
  monkeypatch.setenv("BQAA_PROJECT_ID", "p")
  monkeypatch.setenv("BQAA_DATASET", "d")

  rc = main(argv=["--json"])

  assert rc == 0
  parsed = json.loads(capsys.readouterr().out)
  assert parsed["project_id"] == "p"
  assert parsed["dataset"] == "d"
  assert parsed["all_ok"] is True
  assert parsed["env_ok"] is True
  assert parsed["required_deps_ok"] is True
  # Probe lists round-trip.
  assert any(
      p["module"] == "google.cloud.bigquery" and p["ok"]
      for p in parsed["required_imports"]
  )


def test_main_json_mode_exit_one_with_actionable_payload(
    monkeypatch, fake_probes, capsys
):
  fake_probes["missing"]["google.cloud.bigquery"] = "missing"

  rc = main(argv=["--json"])

  assert rc == 1
  parsed = json.loads(capsys.readouterr().out)
  assert parsed["all_ok"] is False
  missing = [p for p in parsed["required_imports"] if not p["ok"]]
  assert any(p["install_name"] == "google-cloud-bigquery" for p in missing)
