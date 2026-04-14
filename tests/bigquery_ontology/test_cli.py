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

from bigquery_ontology.cli import app

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
# gm validate — binding files                                            #
# --------------------------------------------------------------------- #
#
# The binding tests collectively prove three things about the CLI:
#
#   1. Companion-ontology resolution works both ways — auto-discovery
#      (``<ontology>.ontology.yaml`` next to the binding) and an
#      explicit ``--ontology PATH`` flag.
#   2. Each failure mode lands at the exit code the user should
#      handle: exit 2 for "you gave me the wrong filesystem state",
#      exit 1 for "the YAML itself is wrong".
#   3. The ``file`` and ``rule`` fields in errors point at the file
#      that actually contains the mistake, so a user grepping their
#      editor can jump to the right line. This is non-trivial because
#      validating a binding transitively validates its ontology too.


# A minimal-but-complete fixture pair. The ontology declares one
# entity ``Thing`` with two stored properties (``id``, ``label``) —
# enough to trigger coverage errors by omitting one of them, and
# unknown-property errors by adding a bogus one. The binding below
# covers both properties so it's a "green" baseline that individual
# tests mutate as needed.
_TINY_ONTOLOGY = """
  ontology: tiny
  entities:
    - name: Thing
      keys:
        primary: [id]
      properties:
        - name: id
          type: string
        - name: label
          type: string
"""

_MINIMAL_BINDING = """
  binding: tiny-bq-prod
  ontology: tiny
  target: {backend: bigquery, project: p, dataset: d}
  entities:
    - name: Thing
      source: raw.things
      properties:
        - name: id
          column: thing_id
        - name: label
          column: display
"""


def test_validate_binding_with_companion_ontology_succeeds(tmp_path):
  # The happy path for auto-discovery. Drop both files in the same
  # directory, point the CLI at the binding only, and expect silent
  # success — the CLI must find ``tiny.ontology.yaml`` next to the
  # binding, load it, and validate the binding against it.
  _write(tmp_path, "tiny.ontology.yaml", _TINY_ONTOLOGY)
  binding = _write(tmp_path, "tiny.binding.yaml", _MINIMAL_BINDING)

  result = _RUNNER.invoke(app, ["validate", str(binding)])

  assert result.exit_code == 0
  assert result.output == ""


def test_validate_binding_with_explicit_ontology_flag_succeeds(tmp_path):
  # The happy path for the explicit flag. We deliberately stash the
  # ontology in a sibling directory so auto-discovery *cannot* find
  # it — the only way this test passes is if ``--ontology`` is
  # honored and discovery is skipped.
  sibling = tmp_path / "sibling"
  sibling.mkdir()
  ontology = _write(sibling, "tiny.ontology.yaml", _TINY_ONTOLOGY)
  binding = _write(tmp_path, "tiny.binding.yaml", _MINIMAL_BINDING)

  result = _RUNNER.invoke(
      app, ["validate", str(binding), "--ontology", str(ontology)]
  )

  assert result.exit_code == 0
  assert result.output == ""


def test_validate_binding_missing_companion_is_usage_error(tmp_path):
  # Auto-discovery fails: the binding says ``ontology: tiny`` so the
  # CLI goes looking for ``tiny.ontology.yaml`` next to it, but we
  # never created one. This is a *usage* error (exit 2) — the user's
  # filesystem is the problem, not the YAML contents. The error
  # message names the exact path we looked at so the user can either
  # move the ontology into place or add ``--ontology PATH``. ``file``
  # points at the binding because that's the file they invoked the
  # CLI against.
  binding = _write(tmp_path, "tiny.binding.yaml", _MINIMAL_BINDING)

  result = _RUNNER.invoke(app, ["validate", str(binding)])

  expected_path = tmp_path / "tiny.ontology.yaml"
  expected = (
      f"{binding}:0:0: cli-missing-ontology \u2014 Binding references "
      f"ontology 'tiny', but no companion ontology file found at "
      f"{expected_path}.\n"
  )
  assert result.exit_code == 2
  assert result.output == expected


def test_validate_binding_with_explicit_missing_ontology_flag_is_usage_error(
    tmp_path,
):
  # Mirror of the auto-discovery case, but for the explicit flag. The
  # error message is shorter here because there's nothing to explain —
  # the user literally pointed us at a file that does not exist.
  # ``file`` points at the missing ontology path (the user's bad
  # input), not the binding.
  binding = _write(tmp_path, "tiny.binding.yaml", _MINIMAL_BINDING)
  missing = tmp_path / "does-not-exist.ontology.yaml"

  result = _RUNNER.invoke(
      app, ["validate", str(binding), "--ontology", str(missing)]
  )

  expected = (
      f"{missing}:0:0: cli-missing-ontology \u2014 "
      f"Ontology file not found: {missing}\n"
  )
  assert result.exit_code == 2
  assert result.output == expected


def test_validate_binding_with_semantic_error_reports_binding_rule(tmp_path):
  # Companion ontology is valid, but the binding itself violates the
  # total-coverage rule — the ontology declares ``id`` and ``label``
  # on ``Thing`` but the binding only maps ``id``. This is the
  # canonical "binding is wrong" error, and we assert two things:
  # the rule prefix is ``binding-validation`` (not ontology-anything)
  # and the ``file`` points at the binding.
  _write(tmp_path, "tiny.ontology.yaml", _TINY_ONTOLOGY)
  binding = _write(
      tmp_path,
      "tiny.binding.yaml",
      """
      binding: tiny-bq-prod
      ontology: tiny
      target: {backend: bigquery, project: p, dataset: d}
      entities:
        - name: Thing
          source: raw.things
          properties:
            - name: id
              column: thing_id
      """,
  )

  result = _RUNNER.invoke(app, ["validate", str(binding)])

  expected = (
      f"{binding}:0:0: binding-validation \u2014 Entity binding 'Thing': "
      "missing bindings for non-derived properties ['label'].\n"
  )
  assert result.exit_code == 1
  assert result.output == expected


def test_validate_binding_with_broken_companion_ontology_reports_ontology_file(
    tmp_path,
):
  # Regression guard for the fix that motivated splitting ontology
  # loading out of the binding loader in the CLI. The user points at
  # a valid-looking binding; auto-discovery finds the companion; but
  # the companion ontology has its *own* validation error (a primary
  # key that references an undeclared column). Naively, that error
  # would bubble through ``load_binding`` and be reported with
  # ``rule=binding-validation`` and ``file=<binding>`` — misleading,
  # because the binding is fine and the ontology is not. The CLI
  # must instead tag this as ``rule=ontology-validation`` and point
  # ``file`` at the ontology path. A user reading the error should
  # open the ontology file, not the binding.
  ontology = _write(
      tmp_path,
      "tiny.ontology.yaml",
      """
      ontology: tiny
      entities:
        - name: Thing
          keys:
            primary: [missing_col]
          properties:
            - name: id
              type: string
      """,
  )
  binding = _write(tmp_path, "tiny.binding.yaml", _MINIMAL_BINDING)

  result = _RUNNER.invoke(app, ["validate", str(binding)])

  expected = (
      f"{ontology}:0:0: ontology-validation \u2014 "
      "Entity 'Thing': key column 'missing_col' is not a declared property.\n"
  )
  assert result.exit_code == 1
  assert result.output == expected


def test_validate_binding_json_output_uses_binding_rule_prefix(tmp_path):
  # Complements ``_semantic_error_reports_binding_rule`` above: same
  # class of error (binding problem, not ontology problem), this time
  # with ``--json`` to prove the rule prefix propagates into the
  # structured output unchanged. The binding lists a property name
  # ``bogus`` that isn't declared on ``Thing`` — chosen deliberately
  # over the "missing property" variant so both the human and JSON
  # paths cover different coverage-rule branches between them.
  _write(tmp_path, "tiny.ontology.yaml", _TINY_ONTOLOGY)
  binding = _write(
      tmp_path,
      "tiny.binding.yaml",
      """
      binding: tiny-bq-prod
      ontology: tiny
      target: {backend: bigquery, project: p, dataset: d}
      entities:
        - name: Thing
          source: raw.things
          properties:
            - name: id
              column: thing_id
            - name: label
              column: display
            - name: bogus
              column: extra
      """,
  )

  result = _RUNNER.invoke(app, ["validate", str(binding), "--json"])

  expected = textwrap.dedent(
      f"""\
      [
        {{
          "file": "{binding}",
          "line": 0,
          "col": 0,
          "rule": "binding-validation",
          "severity": "error",
          "message": "Entity binding 'Thing': property 'bogus' is not declared on this element."
        }}
      ]
      """
  )
  assert result.exit_code == 1
  assert result.output == expected


def test_validate_binding_unreadable_ontology_flag_emits_structured_json(
    tmp_path,
):
  # Proves that ``--ontology`` uses ``str | None`` (not ``Path``) so
  # Typer does not pre-validate readability and bypass ``--json``
  # structured output.
  binding = _write(tmp_path, "tiny.binding.yaml", _MINIMAL_BINDING)
  unreadable = tmp_path / "locked.ontology.yaml"
  unreadable.write_text("ontology: tiny\n", encoding="utf-8")
  unreadable.chmod(0o000)

  result = _RUNNER.invoke(
      app,
      ["validate", str(binding), "--ontology", str(unreadable), "--json"],
  )

  assert result.exit_code == 2
  assert "cli-missing-ontology" in result.output


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
