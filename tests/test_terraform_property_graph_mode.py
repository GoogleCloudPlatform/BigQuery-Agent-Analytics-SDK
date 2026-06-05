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

"""Terraform property-graph parity: variable + env + guard wiring (issue #286).

Static assertions on the HCL pin the same contract the bash deploy exposes (so
the two surfaces don't drift), plus a ``terraform fmt`` gate when the binary is
available (no provider download / network needed). Full ``terraform validate``
is exercised locally during development; it needs provider init and is left out
of CI to stay offline-safe.
"""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

import pytest

_TF_DIR = (
    Path(__file__).resolve().parents[1]
    / "examples"
    / "migration_v5"
    / "periodic_materialization"
    / "terraform"
)
_MAIN = (_TF_DIR / "main.tf").read_text()
_VARS = (_TF_DIR / "variables.tf").read_text()
_TFVARS = (_TF_DIR / "terraform.tfvars.example").read_text()


def test_property_graph_variable_declared() -> None:
  assert 'variable "property_graph"' in _VARS
  assert "type        = bool" in _VARS


def test_env_var_wired_conditionally() -> None:
  # BQAA_PROPERTY_GRAPH is set only when var.property_graph is true.
  assert "var.property_graph ?" in _MAIN
  assert 'BQAA_PROPERTY_GRAPH = "property_graph.sql"' in _MAIN


def test_compiled_only_precondition() -> None:
  # Plan-time guard mirrors the bash deploy boundary rejection.
  assert "precondition" in _MAIN
  assert (
      '!(var.property_graph && var.extraction_mode == "compiled-only")' in _MAIN
  )


def test_tfvars_example_documents_property_graph() -> None:
  assert "property_graph" in _TFVARS


@pytest.mark.skipif(
    shutil.which("terraform") is None, reason="terraform binary not available"
)
def test_terraform_fmt_clean() -> None:
  result = subprocess.run(
      ["terraform", "fmt", "-check", "-recursive"],
      cwd=str(_TF_DIR),
      capture_output=True,
      text=True,
      timeout=60,
  )
  assert result.returncode == 0, f"terraform fmt would change:\n{result.stdout}"
