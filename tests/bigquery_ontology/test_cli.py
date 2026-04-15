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

import pytest
from typer.testing import CliRunner

from bigquery_ontology.cli import app

_has_rdflib = True
try:
  import rdflib  # noqa: F401
except ImportError:
  _has_rdflib = False

_requires_rdflib = pytest.mark.skipif(
    not _has_rdflib, reason="rdflib not installed"
)

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


# --------------------------------------------------------------------- #
# gm compile                                                             #
# --------------------------------------------------------------------- #
#
# ``gm compile`` reuses the binding-plus-ontology loading path that
# ``gm validate`` set up, so most of the failure modes (missing
# companion, broken companion ontology, binding shape/semantic errors)
# are guaranteed-equivalent by construction and aren't re-tested here.
# The compile-specific surface the tests below pin:
#
#   - Happy path writes the exact DDL the compiler emits to stdout.
#   - ``-o PATH`` routes the DDL to a file and leaves stdout empty.
#   - Compile-time rule violations (an entity with ``extends``) route
#     through ``rule=compile-validation``, distinguishing them from
#     loader-level binding/ontology errors.
#   - An ontology file passed to ``gm compile`` is a usage error —
#     you can't compile without a physical mapping.


_COMPILE_ONTOLOGY = """
  ontology: tiny
  entities:
    - name: Thing
      keys: {primary: [id]}
      properties:
        - {name: id, type: string}
        - {name: label, type: string}
"""

_COMPILE_BINDING = """
  binding: tiny-bq-prod
  ontology: tiny
  target: {backend: bigquery, project: p, dataset: d}
  entities:
    - name: Thing
      source: raw.things
      properties:
        - {name: id, column: thing_id}
        - {name: label, column: display}
"""

# The exact DDL the compiler produces for the fixture pair above.
# Kept in sync with the emitter's formatting rules; any change to the
# compiler's output shape will fail this test and the golden in
# ``test_compiler.py`` simultaneously — that's deliberate.
_COMPILE_EXPECTED_DDL = textwrap.dedent(
    """\
    CREATE PROPERTY GRAPH tiny
      NODE TABLES (
        raw.things AS Thing
          KEY (thing_id)
          LABEL Thing PROPERTIES (thing_id AS id, display AS label)
      );
    """
)


def test_compile_binding_writes_ddl_to_stdout(tmp_path):
  # Happy path: drop a binding and its companion ontology into the
  # same directory, run ``gm compile``, expect the exact DDL on
  # stdout with exit 0 and nothing on stderr.
  _write(tmp_path, "tiny.ontology.yaml", _COMPILE_ONTOLOGY)
  binding = _write(tmp_path, "tiny.binding.yaml", _COMPILE_BINDING)

  result = _RUNNER.invoke(app, ["compile", str(binding)])

  assert result.exit_code == 0
  assert result.output == _COMPILE_EXPECTED_DDL


def test_compile_binding_with_output_flag_writes_file_and_no_stdout(tmp_path):
  # ``-o PATH`` should land the DDL in the named file and leave
  # stdout empty — important so the command is pipeable into build
  # systems that redirect stdout but care about exit codes.
  _write(tmp_path, "tiny.ontology.yaml", _COMPILE_ONTOLOGY)
  binding = _write(tmp_path, "tiny.binding.yaml", _COMPILE_BINDING)
  out_path = tmp_path / "out.sql"

  result = _RUNNER.invoke(app, ["compile", str(binding), "-o", str(out_path)])

  assert result.exit_code == 0
  assert result.output == ""
  assert out_path.read_text(encoding="utf-8") == _COMPILE_EXPECTED_DDL


def test_compile_binding_with_explicit_ontology_flag(tmp_path):
  # Mirror of the validate test: stash the ontology where
  # auto-discovery can't find it, pass it explicitly, and confirm
  # the DDL comes through.
  sibling = tmp_path / "sibling"
  sibling.mkdir()
  ontology = _write(sibling, "tiny.ontology.yaml", _COMPILE_ONTOLOGY)
  binding = _write(tmp_path, "tiny.binding.yaml", _COMPILE_BINDING)

  result = _RUNNER.invoke(
      app, ["compile", str(binding), "--ontology", str(ontology)]
  )

  assert result.exit_code == 0
  assert result.output == _COMPILE_EXPECTED_DDL


def test_compile_routes_compile_time_errors_with_compile_validation_rule(
    tmp_path,
):
  # A binding can be loader-valid but still fail at compile time —
  # the canonical case is an ontology that uses ``extends``, which
  # v0 compile rejects. These errors must be tagged
  # ``rule=compile-validation`` (not ``binding-validation`` or
  # ``ontology-validation``) so tooling can tell them apart.
  _write(
      tmp_path,
      "tiny.ontology.yaml",
      """
      ontology: tiny
      entities:
        - name: Parent
          keys: {primary: [id]}
          properties: [{name: id, type: string}]
        - name: Child
          extends: Parent
          properties: []
      """,
  )
  binding = _write(
      tmp_path,
      "tiny.binding.yaml",
      """
      binding: b
      ontology: tiny
      target: {backend: bigquery, project: p, dataset: d}
      entities:
        - name: Parent
          source: t
          properties: [{name: id, column: id}]
      """,
  )

  result = _RUNNER.invoke(app, ["compile", str(binding)])

  expected = (
      f"{binding}:0:0: compile-validation \u2014 Entity 'Child' uses "
      "'extends'; v0 compilation does not support inheritance.\n"
  )
  assert result.exit_code == 1
  assert result.output == expected


def test_compile_on_ontology_file_is_usage_error(tmp_path):
  # Passing an ontology file to ``gm compile`` is nonsensical — an
  # ontology alone has no physical mapping. Fail fast (exit 2) with
  # a clear message rather than accepting the file and then dying
  # deep inside the loader.
  ontology = _write(tmp_path, "tiny.ontology.yaml", _COMPILE_ONTOLOGY)

  result = _RUNNER.invoke(app, ["compile", str(ontology)])

  expected = (
      f"{ontology}:0:0: cli-wrong-kind \u2014 "
      "gm compile requires a binding file; got an ontology.\n"
  )
  assert result.exit_code == 2
  assert result.output == expected


def test_compile_missing_binding_file_is_usage_error(tmp_path):
  # Same surface as ``gm validate`` for a missing input, kept as a
  # sanity test so the compile command doesn't accidentally skip
  # the existence check.
  missing = tmp_path / "nope.binding.yaml"

  result = _RUNNER.invoke(app, ["compile", str(missing)])

  expected = (
      f"{missing}:0:0: cli-missing-file \u2014 File not found: {missing}\n"
  )
  assert result.exit_code == 2
  assert result.output == expected


def test_compile_json_error_output_uses_compile_validation_rule(tmp_path):
  # ``--json`` for compile errors flows through the same emitter as
  # validate, so the routing is inherited; this test pins that the
  # rule prefix survives into the JSON payload unchanged.
  _write(
      tmp_path,
      "tiny.ontology.yaml",
      """
      ontology: tiny
      entities:
        - name: Parent
          keys: {primary: [id]}
          properties: [{name: id, type: string}]
        - name: Child
          extends: Parent
          properties: []
      """,
  )
  binding = _write(
      tmp_path,
      "tiny.binding.yaml",
      """
      binding: b
      ontology: tiny
      target: {backend: bigquery, project: p, dataset: d}
      entities:
        - name: Parent
          source: t
          properties: [{name: id, column: id}]
      """,
  )

  result = _RUNNER.invoke(app, ["compile", str(binding), "--json"])

  expected = textwrap.dedent(
      f"""\
      [
        {{
          "file": "{binding}",
          "line": 0,
          "col": 0,
          "rule": "compile-validation",
          "severity": "error",
          "message": "Entity 'Child' uses 'extends'; v0 compilation does not support inheritance."
        }}
      ]
      """
  )
  assert result.exit_code == 1
  assert result.output == expected


def test_compile_missing_companion_ontology_is_usage_error(tmp_path):
  # Regression guard shared with validate: the compile command must
  # also surface cli-missing-ontology (not a generic binding error)
  # when the companion is absent, with the same message format.
  binding = _write(tmp_path, "tiny.binding.yaml", _COMPILE_BINDING)

  result = _RUNNER.invoke(app, ["compile", str(binding)])

  expected_path = tmp_path / "tiny.ontology.yaml"
  expected = (
      f"{binding}:0:0: cli-missing-ontology \u2014 Binding references "
      f"ontology 'tiny', but no companion ontology file found at "
      f"{expected_path}.\n"
  )
  assert result.exit_code == 2
  assert result.output == expected


def test_compile_unreadable_file_with_json_emits_structured_json(tmp_path):
  spec = tmp_path / "locked.binding.yaml"
  spec.write_text("binding: x\n", encoding="utf-8")
  spec.chmod(0o000)

  result = _RUNNER.invoke(app, ["compile", str(spec), "--json"])

  assert result.exit_code == 2
  assert "cli-missing-file" in result.output


def test_compile_unreadable_ontology_with_json_emits_structured_json(tmp_path):
  binding = _write(tmp_path, "tiny.binding.yaml", _COMPILE_BINDING)
  unreadable = tmp_path / "locked.ontology.yaml"
  unreadable.write_text("ontology: tiny\n", encoding="utf-8")
  unreadable.chmod(0o000)

  result = _RUNNER.invoke(
      app,
      ["compile", str(binding), "--ontology", str(unreadable), "--json"],
  )

  assert result.exit_code == 2
  assert "cli-missing-ontology" in result.output


def test_compile_output_to_nonexistent_dir_emits_structured_error(tmp_path):
  _write(tmp_path, "tiny.ontology.yaml", _COMPILE_ONTOLOGY)
  binding = _write(tmp_path, "tiny.binding.yaml", _COMPILE_BINDING)
  bad_output = tmp_path / "no" / "such" / "dir" / "out.sql"

  result = _RUNNER.invoke(app, ["compile", str(binding), "-o", str(bad_output)])

  assert result.exit_code == 1
  assert "cli-output-error" in result.output


def test_compile_output_to_nonexistent_dir_json_emits_structured_json(
    tmp_path,
):
  _write(tmp_path, "tiny.ontology.yaml", _COMPILE_ONTOLOGY)
  binding = _write(tmp_path, "tiny.binding.yaml", _COMPILE_BINDING)
  bad_output = tmp_path / "no" / "such" / "dir" / "out.sql"

  result = _RUNNER.invoke(
      app, ["compile", str(binding), "-o", str(bad_output), "--json"]
  )

  assert result.exit_code == 1
  assert "cli-output-error" in result.output


def test_validate_unreadable_file_with_json_emits_structured_json(tmp_path):
  spec = tmp_path / "locked.ontology.yaml"
  spec.write_text("ontology: tiny\n", encoding="utf-8")
  spec.chmod(0o000)

  result = _RUNNER.invoke(app, ["validate", str(spec), "--json"])

  assert result.exit_code == 2
  assert "cli-missing-file" in result.output


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


# --------------------------------------------------------------------- #
# gm import-owl                                                          #
# --------------------------------------------------------------------- #
#
# The import-owl tests require rdflib, which is an optional dependency.
# Tests that exercise the importer's core logic are in
# ``test_owl_importer.py``; these tests verify the CLI surface:
# argument parsing, output routing, error formatting, and exit codes.

_TINY_TURTLE = """\
@prefix : <http://example.com/test#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .

:Thing a owl:Class ;
    rdfs:label "Thing" ;
    owl:hasKey ( :thing_id ) .

:thing_id a owl:DatatypeProperty ;
    rdfs:domain :Thing ;
    rdfs:range xsd:string .
"""

_TURTLE_WITH_DROPS = """\
@prefix : <http://example.com/test#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .

:Alpha a owl:Class ;
    rdfs:label "Alpha" ;
    owl:hasKey ( :alpha_id ) ;
    owl:disjointWith :Beta .

:Beta a owl:Class ;
    rdfs:label "Beta" ;
    owl:hasKey ( :beta_id ) .

:alpha_id a owl:DatatypeProperty ;
    rdfs:domain :Alpha ;
    rdfs:range xsd:string .

:beta_id a owl:DatatypeProperty ;
    rdfs:domain :Beta ;
    rdfs:range xsd:string .
"""


@_requires_rdflib
def test_import_owl_golden_path_writes_yaml_to_stdout(tmp_path):
  ttl = _write(tmp_path, "test.ttl", _TINY_TURTLE)

  result = _RUNNER.invoke(
      app,
      [
          "import-owl",
          str(ttl),
          "--include-namespace",
          "http://example.com/test#",
      ],
  )

  assert result.exit_code == 0
  assert "ontology: test" in result.output
  assert "name: Thing" in result.output
  assert "name: thing_id" in result.output


@_requires_rdflib
def test_import_owl_output_flag_writes_to_file(tmp_path):
  ttl = _write(tmp_path, "test.ttl", _TINY_TURTLE)
  out = tmp_path / "out.ontology.yaml"

  result = _RUNNER.invoke(
      app,
      [
          "import-owl",
          str(ttl),
          "--include-namespace",
          "http://example.com/test#",
          "-o",
          str(out),
      ],
  )

  assert result.exit_code == 0
  content = out.read_text(encoding="utf-8")
  assert "ontology: test" in content
  assert "name: Thing" in content


@_requires_rdflib
def test_import_owl_drop_summary_on_stderr(tmp_path):
  ttl = _write(tmp_path, "test.ttl", _TURTLE_WITH_DROPS)

  result = _RUNNER.invoke(
      app,
      [
          "import-owl",
          str(ttl),
          "--include-namespace",
          "http://example.com/test#",
      ],
  )

  assert result.exit_code == 0
  assert "Dropped OWL features" in result.output
  assert "owl:disjointWith: 1" in result.output


@_requires_rdflib
def test_import_owl_format_flag_ttl(tmp_path):
  ttl = _write(tmp_path, "test.owl", _TINY_TURTLE)

  result = _RUNNER.invoke(
      app,
      [
          "import-owl",
          str(ttl),
          "--include-namespace",
          "http://example.com/test#",
          "--format",
          "ttl",
      ],
  )

  assert result.exit_code == 0
  assert "ontology: test" in result.output


@_requires_rdflib
def test_import_owl_multiple_namespaces(tmp_path):
  multi_ns = """\
@prefix a: <http://example.com/ns_a#> .
@prefix b: <http://example.com/ns_b#> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix xsd: <http://www.w3.org/2001/XMLSchema#> .

a:Foo a owl:Class ; rdfs:label "Foo" ; owl:hasKey ( a:foo_id ) .
a:foo_id a owl:DatatypeProperty ; rdfs:domain a:Foo ; rdfs:range xsd:string .

b:Bar a owl:Class ; rdfs:label "Bar" ; owl:hasKey ( b:bar_id ) .
b:bar_id a owl:DatatypeProperty ; rdfs:domain b:Bar ; rdfs:range xsd:string .
"""
  ttl = _write(tmp_path, "multi.ttl", multi_ns)

  result = _RUNNER.invoke(
      app,
      [
          "import-owl",
          str(ttl),
          "--include-namespace",
          "http://example.com/ns_a#",
          "--include-namespace",
          "http://example.com/ns_b#",
      ],
  )

  assert result.exit_code == 0
  assert "name: Foo" in result.output
  assert "name: Bar" in result.output


def test_import_owl_missing_source_file_is_usage_error(tmp_path):
  missing = tmp_path / "nope.ttl"

  result = _RUNNER.invoke(
      app,
      [
          "import-owl",
          str(missing),
          "--include-namespace",
          "http://example.com/test#",
      ],
  )

  expected = (
      f"{missing}:0:0: cli-missing-file \u2014 File not found: {missing}\n"
  )
  assert result.exit_code == 2
  assert result.output == expected


def test_import_owl_missing_file_json_emits_structured_json(tmp_path):
  missing = tmp_path / "nope.ttl"

  result = _RUNNER.invoke(
      app,
      [
          "import-owl",
          str(missing),
          "--include-namespace",
          "http://example.com/test#",
          "--json",
      ],
  )

  assert result.exit_code == 2
  assert "cli-missing-file" in result.output


def test_import_owl_bad_format_is_usage_error(tmp_path):
  ttl = _write(tmp_path, "test.ttl", _TINY_TURTLE)

  result = _RUNNER.invoke(
      app,
      [
          "import-owl",
          str(ttl),
          "--include-namespace",
          "http://example.com/test#",
          "--format",
          "badvalue",
      ],
  )

  assert result.exit_code == 2
  assert "cli-usage" in result.output
  assert "badvalue" in result.output


@_requires_rdflib
def test_import_owl_output_to_nonexistent_dir_emits_error(tmp_path):
  ttl = _write(tmp_path, "test.ttl", _TINY_TURTLE)
  bad_output = tmp_path / "no" / "such" / "dir" / "out.yaml"

  result = _RUNNER.invoke(
      app,
      [
          "import-owl",
          str(ttl),
          "--include-namespace",
          "http://example.com/test#",
          "-o",
          str(bad_output),
      ],
  )

  assert result.exit_code == 1
  assert "cli-output-error" in result.output


@_requires_rdflib
def test_import_owl_missing_rdflib_exits_2(tmp_path):
  ttl = _write(tmp_path, "test.ttl", _TINY_TURTLE)

  import unittest.mock

  with unittest.mock.patch.dict(
      "sys.modules", {"bigquery_ontology.owl_importer": None}
  ):
    result = _RUNNER.invoke(
        app,
        [
            "import-owl",
            str(ttl),
            "--include-namespace",
            "http://example.com/test#",
        ],
    )

  assert result.exit_code == 2
  assert "cli-missing-dependency" in result.output
  assert "pip install" in result.output


@_requires_rdflib
def test_import_owl_output_to_existing_directory_emits_error(tmp_path):
  ttl = _write(tmp_path, "test.ttl", _TINY_TURTLE)
  existing_dir = tmp_path / "a_directory"
  existing_dir.mkdir()

  result = _RUNNER.invoke(
      app,
      [
          "import-owl",
          str(ttl),
          "--include-namespace",
          "http://example.com/test#",
          "-o",
          str(existing_dir),
      ],
  )

  assert result.exit_code == 1
  assert "cli-output-error" in result.output
