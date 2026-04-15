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

"""Tests for ``gm scaffold`` CLI command."""

from __future__ import annotations

from pathlib import Path
import textwrap

from typer.testing import CliRunner

from bigquery_ontology.cli import app

_RUNNER = CliRunner()


def _write(tmp_path: Path, name: str, body: str) -> Path:
  path = tmp_path / name
  path.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
  return path


_TINY_ONTOLOGY = """\
  ontology: test
  entities:
    - name: Person
      keys: {primary: [party_id]}
      properties:
        - {name: party_id, type: string}
        - {name: name, type: string}
  relationships:
    - name: Follows
      from: Person
      to: Person
      properties:
        - {name: since, type: date}
"""

_EXTENDS_ONTOLOGY = """\
  ontology: test
  entities:
    - name: Party
      keys: {primary: [party_id]}
      properties:
        - {name: party_id, type: string}
    - name: Person
      extends: Party
      properties:
        - {name: name, type: string}
"""


# --------------------------------------------------------------------- #
# Happy path                                                             #
# --------------------------------------------------------------------- #


def test_scaffold_writes_ddl_and_binding(tmp_path):
  ont = _write(tmp_path, "test.ontology.yaml", _TINY_ONTOLOGY)
  out = tmp_path / "out"

  result = _RUNNER.invoke(
      app,
      [
          "scaffold",
          "--ontology",
          str(ont),
          "--dataset",
          "ds",
          "--project",
          "p",
          "--out",
          str(out),
      ],
  )
  assert result.exit_code == 0, result.output

  ddl = (out / "table_ddl.sql").read_text(encoding="utf-8")
  binding = (out / "binding.yaml").read_text(encoding="utf-8")

  assert "CREATE TABLE `p.ds.person`" in ddl
  assert "CREATE TABLE `p.ds.follows`" in ddl
  assert "PRIMARY KEY (party_id) NOT ENFORCED" in ddl
  assert "FOREIGN KEY" in ddl

  assert "binding: ds" in binding
  assert "ontology: test" in binding
  assert "project: p" in binding
  assert "source: p.ds.person" in binding
  assert "from_columns: [from_party_id]" in binding


def test_scaffold_creates_output_dir(tmp_path):
  ont = _write(tmp_path, "test.ontology.yaml", _TINY_ONTOLOGY)
  out = tmp_path / "nested" / "deep" / "out"

  result = _RUNNER.invoke(
      app,
      [
          "scaffold",
          "--ontology",
          str(ont),
          "--dataset",
          "ds",
          "--project",
          "p",
          "--out",
          str(out),
      ],
  )
  assert result.exit_code == 0, result.output
  assert (out / "table_ddl.sql").exists()
  assert (out / "binding.yaml").exists()


def test_scaffold_with_project(tmp_path):
  ont = _write(tmp_path, "test.ontology.yaml", _TINY_ONTOLOGY)
  out = tmp_path / "out"

  result = _RUNNER.invoke(
      app,
      [
          "scaffold",
          "--ontology",
          str(ont),
          "--dataset",
          "ds",
          "--project",
          "proj",
          "--out",
          str(out),
      ],
  )
  assert result.exit_code == 0, result.output

  ddl = (out / "table_ddl.sql").read_text(encoding="utf-8")
  binding = (out / "binding.yaml").read_text(encoding="utf-8")

  assert "`proj.ds.person`" in ddl
  assert "project: proj" in binding


def test_scaffold_preserve_naming(tmp_path):
  ont = _write(tmp_path, "test.ontology.yaml", _TINY_ONTOLOGY)
  out = tmp_path / "out"

  result = _RUNNER.invoke(
      app,
      [
          "scaffold",
          "--ontology",
          str(ont),
          "--dataset",
          "ds",
          "--project",
          "p",
          "--naming",
          "preserve",
          "--out",
          str(out),
      ],
  )
  assert result.exit_code == 0, result.output

  ddl = (out / "table_ddl.sql").read_text(encoding="utf-8")
  assert "`p.ds.Person`" in ddl


# --------------------------------------------------------------------- #
# Error paths                                                            #
# --------------------------------------------------------------------- #


def test_scaffold_non_empty_dir_is_usage_error(tmp_path):
  ont = _write(tmp_path, "test.ontology.yaml", _TINY_ONTOLOGY)
  out = tmp_path / "out"
  out.mkdir()
  (out / "existing.txt").write_text("x")

  result = _RUNNER.invoke(
      app,
      [
          "scaffold",
          "--ontology",
          str(ont),
          "--dataset",
          "ds",
          "--project",
          "p",
          "--out",
          str(out),
      ],
  )
  assert result.exit_code == 2
  assert "cli-non-empty-dir" in result.output


def test_scaffold_missing_ontology_is_usage_error(tmp_path):
  out = tmp_path / "out"
  result = _RUNNER.invoke(
      app,
      [
          "scaffold",
          "--ontology",
          str(tmp_path / "nope.yaml"),
          "--dataset",
          "ds",
          "--project",
          "p",
          "--out",
          str(out),
      ],
  )
  assert result.exit_code == 2
  assert "cli-missing-file" in result.output


def test_scaffold_invalid_naming_is_usage_error(tmp_path):
  ont = _write(tmp_path, "test.ontology.yaml", _TINY_ONTOLOGY)
  out = tmp_path / "out"

  result = _RUNNER.invoke(
      app,
      [
          "scaffold",
          "--ontology",
          str(ont),
          "--dataset",
          "ds",
          "--project",
          "p",
          "--naming",
          "kebab",
          "--out",
          str(out),
      ],
  )
  assert result.exit_code == 2
  assert "cli-usage" in result.output


def test_scaffold_out_is_file_is_usage_error(tmp_path):
  ont = _write(tmp_path, "test.ontology.yaml", _TINY_ONTOLOGY)
  out = tmp_path / "out"
  out.write_text("not a directory")

  result = _RUNNER.invoke(
      app,
      [
          "scaffold",
          "--ontology",
          str(ont),
          "--dataset",
          "ds",
          "--project",
          "p",
          "--out",
          str(out),
      ],
  )
  assert result.exit_code == 2
  assert "cli-output-error" in result.output


def test_scaffold_extends_is_validation_error(tmp_path):
  ont = _write(tmp_path, "test.ontology.yaml", _EXTENDS_ONTOLOGY)
  out = tmp_path / "out"

  result = _RUNNER.invoke(
      app,
      [
          "scaffold",
          "--ontology",
          str(ont),
          "--dataset",
          "ds",
          "--project",
          "p",
          "--out",
          str(out),
      ],
  )
  assert result.exit_code == 1
  assert "scaffold-validation" in result.output
  assert "extends" in result.output


def test_scaffold_json_error_output(tmp_path):
  out = tmp_path / "out"
  result = _RUNNER.invoke(
      app,
      [
          "scaffold",
          "--ontology",
          str(tmp_path / "nope.yaml"),
          "--dataset",
          "ds",
          "--project",
          "p",
          "--out",
          str(out),
          "--json",
      ],
  )
  assert result.exit_code == 2
  assert '"rule": "cli-missing-file"' in result.output


# --------------------------------------------------------------------- #
# Round-trip integration                                                 #
# --------------------------------------------------------------------- #


def test_scaffold_then_compile_succeeds(tmp_path):
  ont = _write(
      tmp_path,
      "test.ontology.yaml",
      """\
    ontology: test
    entities:
      - name: Person
        keys: {primary: [person_id]}
        properties:
          - {name: person_id, type: string}
          - {name: name, type: string}
    relationships:
      - name: Knows
        from: Person
        to: Person
        properties:
          - {name: since, type: date}
  """,
  )
  scaffold_out = tmp_path / "graph"

  result = _RUNNER.invoke(
      app,
      [
          "scaffold",
          "--ontology",
          str(ont),
          "--dataset",
          "ds",
          "--project",
          "p",
          "--out",
          str(scaffold_out),
      ],
  )
  assert result.exit_code == 0, result.output

  binding_path = scaffold_out / "binding.yaml"
  compile_result = _RUNNER.invoke(
      app,
      [
          "compile",
          str(binding_path),
          "--ontology",
          str(ont),
      ],
  )
  assert compile_result.exit_code == 0, compile_result.output
  assert "CREATE PROPERTY GRAPH" in compile_result.output
