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

"""Tests for the ``gm`` CLI in ``src/ontology/cli.py``.

These tests favor **full-text** input/output comparisons over field-by-field
assertions. Each test embeds the entire YAML input as a literal string,
writes it to a temp file, invokes the CLI, and asserts against the exact
exit code plus the exact stdout / stderr text. That way each test reads
top-to-bottom as "given this exact YAML and these exact flags, you get
this exact result."
"""

from __future__ import annotations

from pathlib import Path
import textwrap

from typer.testing import CliRunner

from ontology.cli import app

# NOTE: Click's CliRunner merges stderr into ``result.output`` by default
# (``mix_stderr=True``). The CLI writes errors to stderr, so all
# error-path assertions below match against ``result.output``. If a
# future Typer/Click upgrade flips the default or this runner is
# constructed with ``mix_stderr=False``, switch error assertions to
# ``result.stderr`` to keep them honest.
_RUNNER = CliRunner()


def _write(tmp_path: Path, name: str, body: str) -> Path:
  path = tmp_path / name
  path.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
  return path


# --------------------------------------------------------------------- #
# gm validate — ontology files                                           #
# --------------------------------------------------------------------- #


def test_validate_valid_ontology_emits_nothing_and_exits_zero(tmp_path):
  spec = _write(
      tmp_path,
      "tiny.ontology.yaml",
      """
      ontology: tiny
      entities:
        - name: Thing
          keys:
            primary:
              - id
          properties:
            - name: id
              type: string
      """,
  )

  result = _RUNNER.invoke(app, ["validate", str(spec)])

  assert result.exit_code == 0
  assert result.output == ""


def test_validate_invalid_ontology_emits_human_error(tmp_path):
  spec = _write(
      tmp_path,
      "bad.ontology.yaml",
      """
      ontology: bad
      entities:
        - name: Thing
          keys:
            primary:
              - missing_col
          properties:
            - name: id
              type: string
      """,
  )

  result = _RUNNER.invoke(app, ["validate", str(spec)])

  expected = (
      f"{spec}:0:0: ontology-validation \u2014 "
      "Entity 'Thing': key column 'missing_col' is not a declared "
      "property.\n"
  )
  assert result.exit_code == 1
  assert result.output == expected


def test_validate_invalid_ontology_json_output(tmp_path):
  spec = _write(
      tmp_path,
      "bad.ontology.yaml",
      """
      ontology: bad
      entities:
        - name: Thing
          keys:
            primary:
              - missing_col
          properties:
            - name: id
              type: string
      """,
  )

  result = _RUNNER.invoke(app, ["validate", str(spec), "--json"])

  expected = textwrap.dedent(
      f"""\
      [
        {{
          "file": "{spec}",
          "line": 0,
          "col": 0,
          "rule": "ontology-validation",
          "severity": "error",
          "message": "Entity 'Thing': key column 'missing_col' is not a declared property."
        }}
      ]
      """
  )
  assert result.exit_code == 1
  assert result.output == expected


def test_validate_malformed_yaml_reports_line_and_column(tmp_path):
  spec = tmp_path / "broken.ontology.yaml"
  # Unmatched flow bracket — guaranteed yaml.scanner / parser error
  # with a populated problem_mark.
  spec.write_text("ontology: tiny\nentities: [\n", encoding="utf-8")

  result = _RUNNER.invoke(app, ["validate", str(spec)])

  assert result.exit_code == 1
  # Don't full-match the multi-line YAML message; assert the leading
  # location prefix and rule code, which are the contract.
  first_line = result.output.splitlines()[0]
  assert first_line.startswith(f"{spec}:")
  assert "yaml-parse \u2014" in first_line


# --------------------------------------------------------------------- #
# gm validate — file-kind detection                                      #
# --------------------------------------------------------------------- #


def test_validate_binding_file_is_deferred(tmp_path):
  spec = _write(
      tmp_path,
      "x.binding.yaml",
      """
      binding: x
      ontology: tiny
      """,
  )

  result = _RUNNER.invoke(app, ["validate", str(spec)])

  expected = (
      f"{spec}:0:0: cli-binding-deferred \u2014 "
      "Binding validation is not yet implemented; only ontology files are "
      "supported in this revision of gm validate.\n"
  )
  assert result.exit_code == 2
  assert result.output == expected


def test_validate_unknown_kind_is_usage_error(tmp_path):
  spec = _write(
      tmp_path,
      "weird.yaml",
      """
      something: else
      """,
  )

  result = _RUNNER.invoke(app, ["validate", str(spec)])

  expected = (
      f"{spec}:0:0: cli-unknown-kind \u2014 "
      "File is neither an ontology (top-level 'ontology:') nor a binding "
      "(top-level 'binding:').\n"
  )
  assert result.exit_code == 2
  assert result.output == expected


def test_validate_missing_file_emits_structured_error(tmp_path):
  missing = tmp_path / "does-not-exist.ontology.yaml"

  result = _RUNNER.invoke(app, ["validate", str(missing)])

  expected = (
      f"{missing}:0:0: cli-missing-file \u2014 File not found: {missing}\n"
  )
  assert result.exit_code == 2
  assert result.output == expected


def test_validate_missing_file_with_json_emits_structured_json(tmp_path):
  missing = tmp_path / "does-not-exist.ontology.yaml"

  result = _RUNNER.invoke(app, ["validate", str(missing), "--json"])

  expected = textwrap.dedent(
      f"""\
      [
        {{
          "file": "{missing}",
          "line": 0,
          "col": 0,
          "rule": "cli-missing-file",
          "severity": "error",
          "message": "File not found: {missing}"
        }}
      ]
      """
  )
  assert result.exit_code == 2
  assert result.output == expected
