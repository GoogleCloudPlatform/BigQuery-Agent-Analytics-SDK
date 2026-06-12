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

"""Deploy-script property-graph mode: boundary validation + staging contract.

Static + shell tests for the Cloud Run deploy/build scripts (issue #286, PR 3).
The validation runs before any gcloud call, so the rejection paths can be
exercised by invoking the script directly; the staging contract is pinned by
asserting on the script text (no live gcloud / Docker needed).
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
_BUILD = _DEPLOY_DIR / "build_image.sh"

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


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_rejects_property_graph_with_compiled_only(tmp_path) -> None:
  (tmp_path / "property_graph.sql").write_text("x")
  (tmp_path / "table_ddl.sql").write_text("x")
  result = _run_deploy(
      [
          "--property-graph",
          str(tmp_path / "property_graph.sql"),
          "--extraction-mode",
          "compiled-only",
      ]
  )
  assert result.returncode != 0
  assert "compiled-only" in result.stderr


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_rejects_property_graph_without_sibling_table_ddl(tmp_path) -> None:
  (tmp_path / "property_graph.sql").write_text("x")  # no table_ddl.sql sibling
  result = _run_deploy(
      ["--property-graph", str(tmp_path / "property_graph.sql")]
  )
  assert result.returncode != 0
  assert "table_ddl.sql" in result.stderr


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_rejects_missing_property_graph_file(tmp_path) -> None:
  result = _run_deploy(
      ["--property-graph", str(tmp_path / "does_not_exist.sql")]
  )
  assert result.returncode != 0
  assert "not found" in result.stderr


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_rejects_hardcoded_property_graph_without_placeholders(
    tmp_path,
) -> None:
  # The original #286 failure mode: a hardcoded graph DDL (no
  # ${PROJECT_ID}/${DATASET}) would derive against the wrong dataset. Both
  # files exist and mode is ai-fallback, so this must be caught by the
  # placeholder check, not the earlier existence checks.
  (tmp_path / "property_graph.sql").write_text(
      "CREATE PROPERTY GRAPH `proj.ds.g` NODE TABLES ()"
  )
  (tmp_path / "table_ddl.sql").write_text(
      "CREATE TABLE `${PROJECT_ID}.${DATASET}.t` (id STRING)"
  )
  result = _run_deploy(
      ["--property-graph", str(tmp_path / "property_graph.sql")]
  )
  assert result.returncode != 0
  assert "${PROJECT_ID}" in result.stderr or "placeholder" in result.stderr


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_rejects_hardcoded_table_ddl_without_placeholders(tmp_path) -> None:
  # The graph is placeholdered but the companion table DDL is not -> still a
  # wrong-dataset risk at bootstrap time -> reject.
  (tmp_path / "property_graph.sql").write_text(
      "CREATE PROPERTY GRAPH `${PROJECT_ID}.${DATASET}.g` NODE TABLES ()"
  )
  (tmp_path / "table_ddl.sql").write_text(
      "CREATE TABLE `proj.ds.t` (id STRING)"
  )
  result = _run_deploy(
      ["--property-graph", str(tmp_path / "property_graph.sql")]
  )
  assert result.returncode != 0
  assert "${PROJECT_ID}" in result.stderr or "placeholder" in result.stderr


# --------------------------------------------------------------------------- #
# Staging / env-var contract (static text assertions)
# --------------------------------------------------------------------------- #


def test_deploy_script_property_graph_staging_contract() -> None:
  text = _DEPLOY.read_text()
  # mode arg + boundary env var
  assert "--property-graph)" in text
  assert "BQAA_PROPERTY_GRAPH=property_graph.sql" in text
  # property-graph mode stages the graph + its table DDL, not ontology/binding
  assert 'cp "$PROPERTY_GRAPH" "$STAGING/property_graph.sql"' in text
  assert 'cp "$TABLE_DDL_SRC" "$STAGING/table_ddl.sql"' in text
  # placeholder contract is enforced for both artifacts
  assert "grep -qF '${PROJECT_ID}'" in text
  assert "grep -qF '${DATASET}'" in text


def test_build_image_property_graph_staging_contract() -> None:
  text = _BUILD.read_text()
  assert "--property-graph)" in text
  assert 'cp "$PROPERTY_GRAPH" "$STAGING/property_graph.sql"' in text
  assert 'cp "$TABLE_DDL_SRC" "$STAGING/table_ddl.sql"' in text
  # Terraform-built images can't bake hardcoded artifacts either.
  assert "grep -qF '${PROJECT_ID}'" in text
  assert "grep -qF '${DATASET}'" in text


def test_deploy_script_endpoint_wiring() -> None:
  # --endpoint MODEL must wire BQAA_ENDPOINT on the Job, and only when set
  # (unset leaves the runtime's gemini-2.5-flash default in place) — so an
  # operator can pick Gemini 3.x on the scheduled path, not just locally.
  text = _DEPLOY.read_text()
  assert "--endpoint)" in text
  assert '[[ -n "$ENDPOINT" ]]' in text
  assert 'ENV_VARS+=("BQAA_ENDPOINT=${ENDPOINT}")' in text
