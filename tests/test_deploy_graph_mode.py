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

"""Deployed-graph (``--graph`` / ``BQAA_GRAPH``) mode: deploy + Terraform wiring.

Static + shell tests mirroring ``test_deploy_property_graph_mode.py`` /
``test_terraform_property_graph_mode.py``: boundary rejections run the script
directly (the validation fires before any gcloud call); the staging and env
contracts are pinned by asserting on the script / module text.
"""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess

import pytest

_DEPLOY_DIR = (
    Path(__file__).resolve().parents[1]
    / "examples"
    / "context_graph"
    / "periodic_materialization"
)
_DEPLOY = _DEPLOY_DIR / "deploy_cloud_run_job.sh"
_DEPLOY_TEXT = _DEPLOY.read_text()
_RUN_JOB_TEXT = (_DEPLOY_DIR / "run_job.py").read_text()

_TF_DIR = _DEPLOY_DIR / "terraform"
_TF_MAIN = (_TF_DIR / "main.tf").read_text()
_TF_VARS = (_TF_DIR / "variables.tf").read_text()
_TFVARS_EXAMPLE = (_TF_DIR / "terraform.tfvars.example").read_text()

_REQUIRED = [
    "--project",
    "p",
    "--region",
    "us-central1",
    "--events-dataset",
    "e",
    "--graph-dataset",
    "g",
    "--schedule",
    "0 */6 * * *",
]


def _run_deploy(args):
  return subprocess.run(
      ["bash", str(_DEPLOY), *_REQUIRED, *args],
      capture_output=True,
      text=True,
      timeout=60,
  )


# --------------------------------------------------------------------------- #
# Deploy script boundary rejections
# --------------------------------------------------------------------------- #


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_rejects_graph_with_compiled_only() -> None:
  result = _run_deploy(
      ["--graph", "agent_decisions_graph", "--extraction-mode", "compiled-only"]
  )
  assert result.returncode != 0
  assert "compiled-only" in result.stderr


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_rejects_graph_with_property_graph(tmp_path) -> None:
  (tmp_path / "property_graph.sql").write_text("x")
  (tmp_path / "table_ddl.sql").write_text("x")
  result = _run_deploy(
      [
          "--graph",
          "agent_decisions_graph",
          "--property-graph",
          str(tmp_path / "property_graph.sql"),
      ]
  )
  assert result.returncode != 0
  assert "mutually exclusive" in result.stderr


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_graph_requires_value() -> None:
  result = _run_deploy(["--graph"])
  assert result.returncode != 0


# --------------------------------------------------------------------------- #
# Deploy script wiring contracts (text pins)
# --------------------------------------------------------------------------- #


def test_deploy_wires_bqaa_graph_env() -> None:
  assert 'ENV_VARS+=("BQAA_GRAPH=${GRAPH}")' in _DEPLOY_TEXT


def test_deploy_graph_mode_stages_nothing() -> None:
  # The graph-mode staging branch ships only run_job.py: no property_graph.sql,
  # no table_ddl.sql, no ontology/binding artifacts.
  graph_branch = _DEPLOY_TEXT.split('if [[ -n "$GRAPH" ]]; then', 2)[2].split(
      "elif"
  )[0]
  assert "cp" not in graph_branch


def test_deploy_help_documents_graph_flag() -> None:
  assert "--graph NAME" in _DEPLOY_TEXT
  assert "INFORMATION_SCHEMA.PROPERTY_GRAPHS" in _DEPLOY_TEXT


# --------------------------------------------------------------------------- #
# run_job.py wiring contracts (text pins)
# --------------------------------------------------------------------------- #


def test_run_job_reads_bqaa_graph() -> None:
  assert 'os.environ.get("BQAA_GRAPH")' in _RUN_JOB_TEXT


def test_run_job_rejects_both_modes() -> None:
  assert "mutually" in _RUN_JOB_TEXT


def test_run_job_skips_bootstrap_in_graph_mode() -> None:
  assert "entity-table bootstrap skipped (deployed-graph mode)" in _RUN_JOB_TEXT


# --------------------------------------------------------------------------- #
# Terraform module wiring
# --------------------------------------------------------------------------- #


def test_tf_graph_variable_declared() -> None:
  assert 'variable "graph"' in _TF_VARS
  graph_block = _TF_VARS.split('variable "graph"')[1].split("variable ")[0]
  assert "type        = string" in graph_block
  assert 'default     = ""' in graph_block


def test_tf_env_var_wired_conditionally() -> None:
  # BQAA_GRAPH is merged only when var.graph is non-empty.
  assert 'var.graph == "" ? {} : {' in _TF_MAIN
  assert "BQAA_GRAPH = var.graph" in _TF_MAIN


def test_tf_compiled_only_precondition() -> None:
  assert (
      '!(var.graph != "" && var.extraction_mode == "compiled-only")' in _TF_MAIN
  )


def test_tf_mutual_exclusion_precondition() -> None:
  assert '!(var.graph != "" && var.property_graph)' in _TF_MAIN


def test_tfvars_example_documents_graph() -> None:
  assert "graph " in _TFVARS_EXAMPLE
  assert "deployed-graph mode" in _TFVARS_EXAMPLE
