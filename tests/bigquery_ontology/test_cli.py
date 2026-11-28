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

import errno
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import textwrap
import threading
import types

import pytest
import typer
from typer.testing import CliRunner

from bigquery_ontology import dataplex_glossary_importer
from bigquery_ontology.cli import _is_descriptor_path
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


# --------------------------------------------------------------------- #
# gm compile --emit-concept-index (A7)                                  #
# --------------------------------------------------------------------- #


def test_compile_without_emit_concept_index_is_byte_identical(tmp_path):
  """Regression guard: omitting ``--emit-concept-index`` produces the
  exact same DDL as before A7 landed.
  """
  _write(tmp_path, "tiny.ontology.yaml", _COMPILE_ONTOLOGY)
  binding = _write(tmp_path, "tiny.binding.yaml", _COMPILE_BINDING)
  result = _RUNNER.invoke(app, ["compile", str(binding)])

  assert result.exit_code == 0
  assert result.output == _COMPILE_EXPECTED_DDL


def test_compile_emit_concept_index_requires_table_flag(tmp_path):
  """``--emit-concept-index`` with no ``--concept-index-table`` is
  rejected — no silent global default per the RFC.
  """
  _write(tmp_path, "tiny.ontology.yaml", _COMPILE_ONTOLOGY)
  binding = _write(tmp_path, "tiny.binding.yaml", _COMPILE_BINDING)

  result = _RUNNER.invoke(
      app, ["compile", str(binding), "--emit-concept-index"]
  )

  assert result.exit_code != 0
  assert "--concept-index-table" in result.output


def test_compile_emit_concept_index_appends_two_create_or_replace_statements(
    tmp_path,
):
  """Happy path: both the property-graph DDL and the concept-index
  SQL (main + meta) appear on stdout.
  """
  _write(tmp_path, "tiny.ontology.yaml", _COMPILE_ONTOLOGY)
  binding = _write(tmp_path, "tiny.binding.yaml", _COMPILE_BINDING)

  result = _RUNNER.invoke(
      app,
      [
          "compile",
          str(binding),
          "--emit-concept-index",
          "--concept-index-table",
          "p.d.tiny_concept_index",
      ],
  )

  assert result.exit_code == 0
  # Property-graph DDL comes first, unchanged.
  assert "CREATE PROPERTY GRAPH" in result.output
  # Then two concept-index CREATE OR REPLACE statements (main + meta).
  assert result.output.count("CREATE OR REPLACE TABLE") == 2
  assert "`p.d.tiny_concept_index`" in result.output
  assert "`p.d.tiny_concept_index__meta`" in result.output


def test_compile_emit_concept_index_with_output_flag_writes_combined_sql(
    tmp_path,
):
  """``-o PATH`` with the emit flag lands the combined SQL in the
  named file and leaves stdout empty.
  """
  _write(tmp_path, "tiny.ontology.yaml", _COMPILE_ONTOLOGY)
  binding = _write(tmp_path, "tiny.binding.yaml", _COMPILE_BINDING)
  out_path = tmp_path / "combined.sql"

  result = _RUNNER.invoke(
      app,
      [
          "compile",
          str(binding),
          "--emit-concept-index",
          "--concept-index-table",
          "p.d.tiny_concept_index",
          "-o",
          str(out_path),
      ],
  )

  assert result.exit_code == 0
  assert result.output == ""
  body = out_path.read_text(encoding="utf-8")
  assert "CREATE PROPERTY GRAPH" in body
  assert body.count("CREATE OR REPLACE TABLE") == 2


def test_compile_emit_concept_index_invalid_table_path_fails(tmp_path):
  """An ``--concept-index-table`` value that fails A3-A5 validation
  surfaces as a clean compile-time error.
  """
  _write(tmp_path, "tiny.ontology.yaml", _COMPILE_ONTOLOGY)
  binding = _write(tmp_path, "tiny.binding.yaml", _COMPILE_BINDING)

  result = _RUNNER.invoke(
      app,
      [
          "compile",
          str(binding),
          "--emit-concept-index",
          "--concept-index-table",
          "not-three-segments",
      ],
  )

  assert result.exit_code == 1
  assert "project.dataset.table" in result.output


def test_compile_emit_concept_index_is_deterministic_across_runs(tmp_path):
  """D2 round-trip: re-running with the same inputs produces
  byte-identical combined output.
  """
  _write(tmp_path, "tiny.ontology.yaml", _COMPILE_ONTOLOGY)
  binding = _write(tmp_path, "tiny.binding.yaml", _COMPILE_BINDING)
  flags = [
      "compile",
      str(binding),
      "--emit-concept-index",
      "--concept-index-table",
      "p.d.tiny_concept_index",
  ]
  out1 = _RUNNER.invoke(app, flags).output
  out2 = _RUNNER.invoke(app, flags).output
  assert out1 == out2


def test_compile_emit_concept_index_with_explicit_compiler_version(tmp_path):
  """``--compiler-version`` overrides the default. The version string
  flows into ``compile_fingerprint`` so the meta row reflects it.
  """
  _write(tmp_path, "tiny.ontology.yaml", _COMPILE_ONTOLOGY)
  binding = _write(tmp_path, "tiny.binding.yaml", _COMPILE_BINDING)

  result = _RUNNER.invoke(
      app,
      [
          "compile",
          str(binding),
          "--emit-concept-index",
          "--concept-index-table",
          "p.d.tiny_concept_index",
          "--compiler-version",
          "test-pin-1.2.3",
      ],
  )

  assert result.exit_code == 0
  # The version string appears in the meta row's compiler_version column.
  assert "'test-pin-1.2.3'" in result.output


def test_compile_emit_concept_index_default_compiler_version_is_stable(
    tmp_path,
):
  """Without ``--compiler-version`` the default is the package
  version; same default → same fingerprint → byte-identical SQL.
  """
  _write(tmp_path, "tiny.ontology.yaml", _COMPILE_ONTOLOGY)
  binding = _write(tmp_path, "tiny.binding.yaml", _COMPILE_BINDING)
  flags = [
      "compile",
      str(binding),
      "--emit-concept-index",
      "--concept-index-table",
      "p.d.tiny_concept_index",
  ]
  out1 = _RUNNER.invoke(app, flags).output
  out2 = _RUNNER.invoke(app, flags).output
  assert out1 == out2
  # The meta row carries some non-empty compiler_version string.
  meta_section = out1[out1.index("__meta") :]
  # Find a quoted compiler-version-shaped value (heuristic: any
  # non-empty string literal in the meta section that isn't a
  # fingerprint or short id).
  assert "'" in meta_section


# --------------------------------------------------------------------- #
# gm import-dataplex-glossary                                            #
# --------------------------------------------------------------------- #
#
# The importer's mapping rules are covered in
# ``test_dataplex_glossary_importer.py``; these tests cover the CLI
# surface. The real client factory is replaced with an in-memory fake, so
# neither GCP credentials nor ``google-cloud-dataplex`` are needed.

_DP_GLOSSARY = "projects/demo-project/locations/us-central1/glossaries/retail"

_DP_EXPECTED_YAML = """\
ontology: retail
description: Retail vocabulary.
annotations:
  dataplex:glossary: projects/demo-project/locations/us-central1/glossaries/retail
  dataplex:uid: u-1

entities:
  - name: category_customer
    abstract: true
    annotations:
      dataplex:resource: projects/demo-project/locations/us-central1/glossaries/retail/categories/customer
      skos:inScheme: projects/demo-project/locations/us-central1/glossaries/retail
      skos:notation: customer
      skos:prefLabel: Customer
      skos:topConceptOf: projects/demo-project/locations/us-central1/glossaries/retail
  - name: term_clv
    abstract: true
    description: Projected revenue from one customer.
    annotations:
      dataplex:resource: projects/demo-project/locations/us-central1/glossaries/retail/terms/clv
      skos:inScheme: projects/demo-project/locations/us-central1/glossaries/retail
      skos:notation: clv
      skos:prefLabel: Customer Lifetime Value

relationships:
  - name: skos_broader
    abstract: true
    from: term_clv
    to: category_customer
"""

_DP_EXPECTED_SUMMARY = """\
Imported Dataplex glossary: projects/demo-project/locations/us-central1/glossaries/retail
Categories imported as abstract entities: 1
Terms imported as abstract entities: 1
SKOS relationships imported as abstract: 1 skos_broader
Note: all entities are abstract (SKOS-only), so nothing is bound to tables. \
`gm compile` and OntologyRuntime still need a companion binding; one with no \
entities is enough for `gm compile --emit-concept-index --concept-index-table \
...`.
"""


class _FakeDataplexGlossaryClient:
  """In-memory ``BusinessGlossaryServiceClient`` for the CLI tests."""

  def __init__(self, *, empty: bool = False, error: Exception | None = None):
    self.calls: list[tuple[str, dict]] = []
    self.error = error
    self.categories = [
        types.SimpleNamespace(
            name=f"{_DP_GLOSSARY}/categories/customer",
            parent=_DP_GLOSSARY,
            display_name="Customer",
            description="",
        )
    ]
    self.terms = [
        types.SimpleNamespace(
            name=f"{_DP_GLOSSARY}/terms/clv",
            parent=f"{_DP_GLOSSARY}/categories/customer",
            display_name="Customer Lifetime Value",
            description="Projected revenue from one customer.",
        )
    ]
    if empty:
      self.categories, self.terms = [], []

  def get_glossary(self, request=None, **kwargs):
    self.calls.append(("get_glossary", request))
    if self.error is not None:
      raise self.error
    return types.SimpleNamespace(
        name=_DP_GLOSSARY, uid="u-1", description="Retail vocabulary."
    )

  def list_glossary_categories(self, request=None, **kwargs):
    self.calls.append(("list_glossary_categories", request))
    return iter(self.categories)

  def list_glossary_terms(self, request=None, **kwargs):
    self.calls.append(("list_glossary_terms", request))
    return iter(self.terms)


class PermissionDenied(Exception):
  """Name-compatible with ``google.api_core.exceptions.PermissionDenied``."""

  code = 403


def _use_fake_dataplex_client(monkeypatch, client) -> None:
  monkeypatch.setattr(
      dataplex_glossary_importer,
      "_create_default_glossary_client",
      lambda: client,
  )


def _forbid_dataplex_client(monkeypatch) -> None:
  def _unexpected():
    raise AssertionError("no Dataplex client should be built")

  monkeypatch.setattr(
      dataplex_glossary_importer, "_create_default_glossary_client", _unexpected
  )


@pytest.fixture
def dataplex_client(monkeypatch):
  client = _FakeDataplexGlossaryClient()
  _use_fake_dataplex_client(monkeypatch, client)
  return client


def test_import_dataplex_glossary_writes_yaml_to_stdout(dataplex_client):
  result = _RUNNER.invoke(app, ["import-dataplex-glossary", _DP_GLOSSARY])

  assert result.exit_code == 0
  assert result.stdout == _DP_EXPECTED_YAML
  assert result.stderr == _DP_EXPECTED_SUMMARY
  assert dataplex_client.calls == [
      ("get_glossary", {"name": _DP_GLOSSARY}),
      ("list_glossary_categories", {"parent": _DP_GLOSSARY}),
      ("list_glossary_terms", {"parent": _DP_GLOSSARY}),
  ]


def test_import_dataplex_glossary_json_flag_keeps_summary_and_yaml(
    dataplex_client,
):
  result = _RUNNER.invoke(
      app, ["import-dataplex-glossary", _DP_GLOSSARY, "--json"]
  )

  assert result.exit_code == 0
  assert result.stdout == _DP_EXPECTED_YAML
  assert result.stderr == _DP_EXPECTED_SUMMARY


@pytest.mark.parametrize("flag", ["-o", "--out"])
def test_import_dataplex_glossary_output_flag_writes_to_file(
    tmp_path, dataplex_client, flag
):
  out = tmp_path / "retail.ontology.yaml"

  result = _RUNNER.invoke(
      app, ["import-dataplex-glossary", _DP_GLOSSARY, flag, str(out)]
  )

  assert result.exit_code == 0
  assert result.stdout == ""
  assert result.stderr == _DP_EXPECTED_SUMMARY
  assert out.read_text(encoding="utf-8") == _DP_EXPECTED_YAML


def test_import_dataplex_glossary_ontology_name_flag(dataplex_client):
  result = _RUNNER.invoke(
      app,
      [
          "import-dataplex-glossary",
          _DP_GLOSSARY,
          "--ontology-name",
          "retail-terms",
      ],
  )

  assert result.exit_code == 0
  assert result.stdout.startswith("ontology: retail_terms\n")
  assert (
      "Ontology name sanitized: 'retail-terms' -> 'retail_terms'\n"
      in result.stderr
  )


def test_import_dataplex_glossary_output_validates_and_compiles(
    tmp_path, dataplex_client
):
  """The documented path: import, validate, then emit the concept index
  through an entity-less companion binding."""
  ontology_path = tmp_path / "retail.ontology.yaml"
  imported = _RUNNER.invoke(
      app,
      ["import-dataplex-glossary", _DP_GLOSSARY, "-o", str(ontology_path)],
  )
  assert imported.exit_code == 0

  validated = _RUNNER.invoke(app, ["validate", str(ontology_path)])
  assert validated.exit_code == 0
  assert validated.output == ""

  binding = _write(
      tmp_path,
      "retail-bq.binding.yaml",
      """
      binding: retail-bq
      ontology: retail
      target:
        backend: bigquery
        project: demo-project
        dataset: glossary
      """,
  )
  assert _RUNNER.invoke(app, ["validate", str(binding)]).exit_code == 0
  compiled = _RUNNER.invoke(
      app,
      [
          "compile",
          str(binding),
          "--emit-concept-index",
          "--concept-index-table",
          "demo-project.glossary.retail_concept_index",
      ],
  )
  assert compiled.exit_code == 0
  # Nothing is bound, so the property-graph part is a bare header.
  assert compiled.stdout.startswith("CREATE PROPERTY GRAPH retail;\n")
  assert compiled.stdout.count("CREATE OR REPLACE TABLE") == 2
  assert "'Customer Lifetime Value', 'pref'" in compiled.stdout


def test_import_dataplex_glossary_missing_dependency_exits_2(monkeypatch):
  monkeypatch.setitem(sys.modules, "google.cloud.dataplex_v1", None)

  result = _RUNNER.invoke(app, ["import-dataplex-glossary", _DP_GLOSSARY])

  assert result.exit_code == 2
  assert result.stdout == ""
  assert result.stderr.startswith("<cli>:0:0: cli-missing-dependency \u2014 ")
  assert "pip install 'bigquery-agent-analytics[dataplex]'" in result.stderr


def test_import_dataplex_glossary_missing_dependency_json(monkeypatch):
  monkeypatch.setitem(sys.modules, "google.cloud.dataplex_v1", None)

  result = _RUNNER.invoke(
      app, ["import-dataplex-glossary", _DP_GLOSSARY, "--json"]
  )

  assert result.exit_code == 2
  errors = json.loads(result.stderr)
  assert len(errors) == 1
  assert errors[0]["file"] == "<cli>"
  assert errors[0]["rule"] == "cli-missing-dependency"
  assert errors[0]["severity"] == "error"
  assert "bigquery-agent-analytics[dataplex]" in errors[0]["message"]


def test_import_dataplex_glossary_unloadable_importer_exits_2(monkeypatch):
  monkeypatch.setitem(
      sys.modules, "bigquery_ontology.dataplex_glossary_importer", None
  )

  result = _RUNNER.invoke(app, ["import-dataplex-glossary", _DP_GLOSSARY])

  assert result.exit_code == 2
  assert "cli-missing-dependency" in result.stderr


def test_import_dataplex_glossary_bad_resource_name_is_usage_error(
    monkeypatch,
):
  _forbid_dataplex_client(monkeypatch)

  result = _RUNNER.invoke(
      app, ["import-dataplex-glossary", "glossaries/retail"]
  )

  assert result.exit_code == 2
  assert result.stderr == (
      "<cli>:0:0: cli-usage \u2014 Invalid glossary resource name "
      "'glossaries/retail'; expected "
      "projects/<project>/locations/<location>/glossaries/<glossary_id>.\n"
  )


def test_import_dataplex_glossary_bad_resource_name_json(monkeypatch):
  _forbid_dataplex_client(monkeypatch)

  result = _RUNNER.invoke(
      app,
      ["import-dataplex-glossary", f"{_DP_GLOSSARY}/terms/clv", "--json"],
  )

  assert result.exit_code == 2
  errors = json.loads(result.stderr)
  assert [e["rule"] for e in errors] == ["cli-usage"]


def test_import_dataplex_glossary_blank_ontology_name_is_usage_error(
    monkeypatch,
):
  _forbid_dataplex_client(monkeypatch)

  result = _RUNNER.invoke(
      app,
      ["import-dataplex-glossary", _DP_GLOSSARY, "--ontology-name", "  "],
  )

  assert result.exit_code == 2
  assert result.stderr.startswith("<cli>:0:0: cli-usage \u2014 ontology_name")


def test_import_dataplex_glossary_api_error_exits_1(monkeypatch):
  client = _FakeDataplexGlossaryClient(
      error=PermissionDenied("403 Permission denied on glossary")
  )
  _use_fake_dataplex_client(monkeypatch, client)

  result = _RUNNER.invoke(app, ["import-dataplex-glossary", _DP_GLOSSARY])

  assert result.exit_code == 1
  assert result.stdout == ""
  assert result.stderr.startswith(
      f"{_DP_GLOSSARY}:0:0: import-api-error \u2014 Dataplex GetGlossary "
      "failed: PermissionDenied: 403 Permission denied on glossary"
  )
  assert "roles/dataplex.catalogViewer" in result.stderr


def test_import_dataplex_glossary_api_error_json(monkeypatch):
  client = _FakeDataplexGlossaryClient(
      error=PermissionDenied("403 Permission denied on glossary")
  )
  _use_fake_dataplex_client(monkeypatch, client)

  result = _RUNNER.invoke(
      app, ["import-dataplex-glossary", _DP_GLOSSARY, "--json"]
  )

  assert result.exit_code == 1
  assert result.stdout == ""
  errors = json.loads(result.stderr)
  assert [e["rule"] for e in errors] == ["import-api-error"]
  assert errors[0]["file"] == _DP_GLOSSARY
  assert errors[0]["message"].startswith(
      "Dataplex GetGlossary failed: PermissionDenied: 403 Permission denied"
  )
  assert "roles/dataplex.catalogViewer" in errors[0]["message"]


def test_import_dataplex_glossary_empty_glossary_exits_1(monkeypatch):
  _use_fake_dataplex_client(
      monkeypatch, _FakeDataplexGlossaryClient(empty=True)
  )

  result = _RUNNER.invoke(
      app, ["import-dataplex-glossary", _DP_GLOSSARY, "--json"]
  )

  assert result.exit_code == 1
  assert result.stdout == ""
  errors = json.loads(result.stderr)
  assert errors[0]["rule"] == "import-validation"
  assert errors[0]["file"] == _DP_GLOSSARY
  assert "no categories or terms" in errors[0]["message"]


def test_import_dataplex_glossary_output_write_error_exits_1(
    tmp_path, dataplex_client
):
  bad_output = tmp_path / "no" / "such" / "dir" / "out.yaml"

  result = _RUNNER.invoke(
      app,
      ["import-dataplex-glossary", _DP_GLOSSARY, "-o", str(bad_output)],
  )

  assert result.exit_code == 1
  assert result.stdout == ""
  assert f"{bad_output}:0:0: cli-output-error" in result.stderr


def test_import_dataplex_glossary_output_write_error_json(
    tmp_path, dataplex_client
):
  bad_output = tmp_path / "no" / "such" / "dir" / "out.yaml"

  result = _RUNNER.invoke(
      app,
      [
          "import-dataplex-glossary",
          _DP_GLOSSARY,
          "-o",
          str(bad_output),
          "--json",
      ],
  )

  assert result.exit_code == 1
  assert result.stdout == ""
  # One JSON document and nothing else: the summary is printed only after
  # a successful write.
  errors = json.loads(result.stderr)
  assert [e["rule"] for e in errors] == ["cli-output-error"]
  assert errors[0]["file"] == str(bad_output)


def _write_previous_output(tmp_path: Path) -> Path:
  out = tmp_path / "retail.ontology.yaml"
  out.write_text("previous: content\n", encoding="utf-8")
  return out


class _HalfWrittenFile:
  """Wraps the real temporary file object; its ``write`` stores half of
  the text and then fails the way a full disk would."""

  def __init__(self, real):
    self._real = real

  def write(self, text):
    self._real.write(text[: len(text) // 2])
    self._real.flush()
    raise OSError(28, "No space left on device")

  def flush(self):
    self._real.flush()

  def __enter__(self):
    return self

  def __exit__(self, *exc_info):
    self._real.close()
    return False


@pytest.mark.parametrize("failure", ["write", "replace"])
def test_import_dataplex_glossary_failed_overwrite_keeps_the_previous_file(
    tmp_path, monkeypatch, dataplex_client, failure
):
  out = _write_previous_output(tmp_path)
  if failure == "write":
    real_fdopen = os.fdopen
    monkeypatch.setattr(
        os,
        "fdopen",
        lambda fd, *args, **kwargs: _HalfWrittenFile(
            real_fdopen(fd, *args, **kwargs)
        ),
    )
  else:

    def _refuse_replace(src, dst):
      raise OSError(13, "Permission denied")

    monkeypatch.setattr(os, "replace", _refuse_replace)

  result = _RUNNER.invoke(
      app, ["import-dataplex-glossary", _DP_GLOSSARY, "-o", str(out)]
  )

  assert result.exit_code == 1
  assert result.stdout == ""
  assert result.stderr.startswith(f"{out}:0:0: cli-output-error")
  assert "Imported Dataplex glossary" not in result.stderr
  assert out.read_text(encoding="utf-8") == "previous: content\n"
  # No temporary file is left behind.
  assert [p.name for p in tmp_path.iterdir()] == [out.name]


def test_import_dataplex_glossary_overwrite_keeps_the_file_mode(
    tmp_path, dataplex_client
):
  out = _write_previous_output(tmp_path)
  out.chmod(0o640)

  result = _RUNNER.invoke(
      app, ["import-dataplex-glossary", _DP_GLOSSARY, "-o", str(out)]
  )

  assert result.exit_code == 0
  assert out.read_text(encoding="utf-8") == _DP_EXPECTED_YAML
  assert stat.S_IMODE(out.stat().st_mode) == 0o640
  assert [p.name for p in tmp_path.iterdir()] == [out.name]


def test_import_dataplex_glossary_new_output_file_follows_the_umask(
    tmp_path, dataplex_client
):
  out = tmp_path / "retail.ontology.yaml"

  previous_umask = os.umask(0o027)
  try:
    result = _RUNNER.invoke(
        app, ["import-dataplex-glossary", _DP_GLOSSARY, "-o", str(out)]
    )
  finally:
    os.umask(previous_umask)

  assert result.exit_code == 0
  assert stat.S_IMODE(out.stat().st_mode) == 0o640


def test_import_dataplex_glossary_output_through_a_symlink_keeps_the_link(
    tmp_path, dataplex_client
):
  target = _write_previous_output(tmp_path)
  link = tmp_path / "link.yaml"
  link.symlink_to(target.name)

  result = _RUNNER.invoke(
      app, ["import-dataplex-glossary", _DP_GLOSSARY, "-o", str(link)]
  )

  assert result.exit_code == 0
  assert link.is_symlink()
  assert target.read_text(encoding="utf-8") == _DP_EXPECTED_YAML


def test_import_dataplex_glossary_read_only_output_is_refused(
    tmp_path, dataplex_client
):
  if hasattr(os, "geteuid") and os.geteuid() == 0:
    pytest.skip("root ignores the read-only bit")
  out = _write_previous_output(tmp_path)
  out.chmod(0o444)

  result = _RUNNER.invoke(
      app, ["import-dataplex-glossary", _DP_GLOSSARY, "-o", str(out)]
  )

  assert result.exit_code == 1
  assert result.stdout == ""
  assert result.stderr.startswith(f"{out}:0:0: cli-output-error")
  assert "Permission denied" in result.stderr
  assert out.read_text(encoding="utf-8") == "previous: content\n"
  assert [p.name for p in tmp_path.iterdir()] == [out.name]


@pytest.mark.skipif(
    not os.path.exists("/dev/null"), reason="needs a POSIX device file"
)
def test_import_dataplex_glossary_output_to_dev_null_succeeds(dataplex_client):
  result = _RUNNER.invoke(
      app, ["import-dataplex-glossary", _DP_GLOSSARY, "-o", "/dev/null"]
  )

  assert result.exit_code == 0
  assert result.stdout == ""
  assert stat.S_ISCHR(os.stat("/dev/null").st_mode)


@pytest.mark.skipif(
    not hasattr(os, "mkfifo"), reason="named pipes need a POSIX platform"
)
def test_import_dataplex_glossary_output_to_a_named_pipe_is_written_through(
    tmp_path, dataplex_client
):
  pipe = tmp_path / "out.pipe"
  os.mkfifo(pipe)
  received = []
  reader = threading.Thread(
      target=lambda: received.append(pipe.read_text(encoding="utf-8")),
      daemon=True,
  )
  reader.start()

  result = _RUNNER.invoke(
      app, ["import-dataplex-glossary", _DP_GLOSSARY, "-o", str(pipe)]
  )
  reader.join(timeout=10)

  assert result.exit_code == 0
  assert received == [_DP_EXPECTED_YAML]
  assert stat.S_ISFIFO(pipe.stat().st_mode)


@pytest.mark.skipif(
    not hasattr(os, "pathconf"), reason="needs os.pathconf to size the name"
)
def test_import_dataplex_glossary_output_name_may_be_as_long_as_allowed(
    tmp_path, dataplex_client
):
  name_max = os.pathconf(tmp_path, "PC_NAME_MAX")
  out = tmp_path / ("x" * (name_max - len(".yaml")) + ".yaml")

  result = _RUNNER.invoke(
      app, ["import-dataplex-glossary", _DP_GLOSSARY, "-o", str(out)]
  )

  assert result.exit_code == 0
  assert out.read_text(encoding="utf-8") == _DP_EXPECTED_YAML
  assert [p.name for p in tmp_path.iterdir()] == [out.name]


def test_import_dataplex_glossary_write_survives_a_chmod_refusal(
    tmp_path, monkeypatch, dataplex_client
):
  out = _write_previous_output(tmp_path)

  def _refuse_chmod(path, mode, **kwargs):
    raise PermissionError(errno.EPERM, "Operation not permitted")

  monkeypatch.setattr(os, "chmod", _refuse_chmod)

  result = _RUNNER.invoke(
      app, ["import-dataplex-glossary", _DP_GLOSSARY, "-o", str(out)]
  )

  assert result.exit_code == 0
  assert out.read_text(encoding="utf-8") == _DP_EXPECTED_YAML
  assert [p.name for p in tmp_path.iterdir()] == [out.name]


def test_import_dataplex_glossary_missing_directory_before_dotdot_fails(
    tmp_path, dataplex_client
):
  out = _write_previous_output(tmp_path)
  through_missing = tmp_path / "missing" / ".." / out.name

  result = _RUNNER.invoke(
      app,
      ["import-dataplex-glossary", _DP_GLOSSARY, "-o", str(through_missing)],
  )

  assert result.exit_code == 1
  assert result.stderr.startswith(f"{through_missing}:0:0: cli-output-error")
  assert "No such file or directory" in result.stderr
  assert out.read_text(encoding="utf-8") == "previous: content\n"
  assert [p.name for p in tmp_path.iterdir()] == [out.name]


def test_import_dataplex_glossary_dangling_link_through_missing_dir_fails(
    tmp_path, dataplex_client
):
  out = _write_previous_output(tmp_path)
  link = tmp_path / "link.yaml"
  link.symlink_to(os.path.join("missing", "..", out.name))

  result = _RUNNER.invoke(
      app, ["import-dataplex-glossary", _DP_GLOSSARY, "-o", str(link)]
  )

  assert result.exit_code == 1
  assert result.stderr.startswith(f"{link}:0:0: cli-output-error")
  assert out.read_text(encoding="utf-8") == "previous: content\n"
  assert link.is_symlink()
  assert sorted(p.name for p in tmp_path.iterdir()) == sorted(
      [link.name, out.name]
  )


@pytest.mark.parametrize("form", ["relative", "absolute", "chain"])
def test_import_dataplex_glossary_dangling_link_creates_its_target(
    tmp_path, dataplex_client, form
):
  existing = tmp_path / "existing"
  existing.mkdir()
  new_file = existing / "new.yaml"
  link = tmp_path / "link.yaml"
  if form == "relative":
    link.symlink_to(os.path.join("existing", "new.yaml"))
  elif form == "absolute":
    link.symlink_to(new_file)
  else:
    (tmp_path / "hop.yaml").symlink_to(os.path.join("existing", "new.yaml"))
    link.symlink_to("hop.yaml")

  result = _RUNNER.invoke(
      app, ["import-dataplex-glossary", _DP_GLOSSARY, "-o", str(link)]
  )

  assert result.exit_code == 0
  assert new_file.read_text(encoding="utf-8") == _DP_EXPECTED_YAML
  assert link.is_symlink()
  assert [p.name for p in existing.iterdir()] == ["new.yaml"]


@pytest.mark.parametrize("suffix", ["", "/"], ids=["plain", "trailing_slash"])
def test_import_dataplex_glossary_output_to_a_directory_is_refused(
    tmp_path, dataplex_client, suffix
):
  destination = f"{tmp_path}{suffix}"

  result = _RUNNER.invoke(
      app, ["import-dataplex-glossary", _DP_GLOSSARY, "-o", destination]
  )

  assert result.exit_code == 1
  assert result.stderr.startswith(f"{destination}:0:0: cli-output-error")
  assert "Is a directory" in result.stderr
  assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("suffix", ["/", "/."], ids=["slash", "slash_dot"])
def test_import_dataplex_glossary_file_path_named_as_a_directory_is_kept(
    tmp_path, dataplex_client, suffix
):
  out = _write_previous_output(tmp_path)
  destination = f"{out}{suffix}"

  result = _RUNNER.invoke(
      app, ["import-dataplex-glossary", _DP_GLOSSARY, "-o", destination]
  )

  assert result.exit_code == 1
  assert result.stderr.startswith(f"{destination}:0:0: cli-output-error")
  assert "Not a directory" in result.stderr
  assert out.read_text(encoding="utf-8") == "previous: content\n"
  assert [p.name for p in tmp_path.iterdir()] == [out.name]


@pytest.mark.parametrize(
    "relative, reason",
    [
        ("no_such_dir/", "Is a directory"),
        ("no_such_dir/.", "No such file or directory"),
        ("no_such_dir/new.yaml/", "No such file or directory"),
    ],
    ids=["slash", "slash_dot", "missing_parent_slash"],
)
def test_import_dataplex_glossary_missing_directory_path_creates_nothing(
    tmp_path, dataplex_client, relative, reason
):
  destination = os.path.join(tmp_path, relative)

  result = _RUNNER.invoke(
      app, ["import-dataplex-glossary", _DP_GLOSSARY, "-o", destination]
  )

  assert result.exit_code == 1
  assert result.stderr.startswith(f"{destination}:0:0: cli-output-error")
  assert reason in result.stderr
  assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "target, reason",
    [
        ("no_such_dir/", "Is a directory"),
        ("no_such_dir/.", "No such file or directory"),
        ("no_such_dir/new.yaml/", "No such file or directory"),
    ],
    ids=["slash", "slash_dot", "missing_parent_slash"],
)
def test_import_dataplex_glossary_dangling_link_to_a_directory_path_fails(
    tmp_path, dataplex_client, target, reason
):
  link = tmp_path / "link.yaml"
  link.symlink_to(target)

  result = _RUNNER.invoke(
      app, ["import-dataplex-glossary", _DP_GLOSSARY, "-o", str(link)]
  )

  assert result.exit_code == 1
  assert result.stderr.startswith(f"{link}:0:0: cli-output-error")
  assert reason in result.stderr
  assert link.is_symlink()
  assert [p.name for p in tmp_path.iterdir()] == [link.name]


def test_import_dataplex_glossary_output_symlink_loop_is_an_output_error(
    tmp_path, dataplex_client
):
  loop = tmp_path / "loop.yaml"
  loop.symlink_to(loop.name)

  result = _RUNNER.invoke(
      app, ["import-dataplex-glossary", _DP_GLOSSARY, "-o", str(loop)]
  )

  assert result.exit_code == 1
  assert result.stderr.startswith(f"{loop}:0:0: cli-output-error")
  assert loop.is_symlink()


@pytest.mark.parametrize(
    "links, creates",
    [
        (2, True),
        pytest.param(
            40,
            True,
            marks=pytest.mark.skipif(
                not sys.platform.startswith("linux"),
                reason="needs the limit of 40 symlinks that Linux sets",
            ),
        ),
        (41, False),
    ],
    ids=["two_links", "forty_links", "forty_one_links"],
)
def test_import_dataplex_glossary_dangling_link_chain_is_limited_to_forty_links(
    tmp_path, dataplex_client, links, creates
):
  # As Linux does, follow at most 40 links to a file that is not there.
  target = tmp_path / "real.yaml"
  names = [f"link{n}" for n in range(1, links + 1)]
  for name, points_to in zip(names, names[1:] + [target.name]):
    (tmp_path / name).symlink_to(points_to)

  result = _RUNNER.invoke(
      app,
      [
          "import-dataplex-glossary",
          _DP_GLOSSARY,
          "-o",
          str(tmp_path / names[0]),
      ],
  )

  if creates:
    assert result.exit_code == 0
    assert target.read_text(encoding="utf-8") == _DP_EXPECTED_YAML
  else:
    assert result.exit_code == 1
    assert "cli-output-error" in result.stderr
    assert not target.exists()
  assert all((tmp_path / name).is_symlink() for name in names)
  assert not [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]


def test_import_dataplex_glossary_empty_output_path_is_an_output_error(
    tmp_path, monkeypatch, dataplex_client
):
  monkeypatch.chdir(tmp_path)

  result = _RUNNER.invoke(
      app, ["import-dataplex-glossary", _DP_GLOSSARY, "-o", ""]
  )

  assert result.exit_code == 1
  assert result.stdout == ""
  assert result.stderr.startswith(":0:0: cli-output-error")
  assert "No such file or directory" in result.stderr
  assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "template",
    [
        "/dev/fd/{fd}",
        "//dev//fd/{fd}",
        "/proc/self/fd/{fd}",
        "/proc/thread-self/fd/{fd}",
        "/proc/{pid}/fd/{fd}",
    ],
    ids=[
        "dev_fd",
        "redundant_slashes",
        "proc_self",
        "proc_thread_self",
        "proc_pid",
    ],
)
def test_import_dataplex_glossary_output_to_a_descriptor_path_writes_through(
    tmp_path, dataplex_client, template
):
  target = tmp_path / "descriptor-target.yaml"
  fd = os.open(target, os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o644)
  try:
    descriptor_path = template.format(fd=fd, pid=os.getpid())
    if not os.path.exists(descriptor_path):
      pytest.skip(f"needs {descriptor_path}")
    result = _RUNNER.invoke(
        app, ["import-dataplex-glossary", _DP_GLOSSARY, "-o", descriptor_path]
    )
    inode_behind_fd = os.fstat(fd).st_ino
  finally:
    os.close(fd)

  assert result.exit_code == 0
  assert result.stdout == ""
  assert target.read_text(encoding="utf-8") == _DP_EXPECTED_YAML
  # Written through the descriptor, not replaced by a new file.
  assert target.stat().st_ino == inode_behind_fd
  assert [p.name for p in tmp_path.iterdir()] == [target.name]


@pytest.mark.parametrize(
    "path, expected",
    [
        ("/dev/stdin", True),
        ("/dev/stdout", True),
        ("/dev/stderr", True),
        ("/dev/fd/0", True),
        ("/dev/fd/12", True),
        ("//dev//fd/3", True),
        ("/proc/self/fd/3", True),
        ("/proc/thread-self/fd/3", True),
        ("/proc/1234/fd/3", True),
        ("/dev/null", False),
        ("/dev/fd/", False),
        ("/dev/fd/x", False),
        ("/dev/fd/3/", False),
        ("/dev/stdout/x", False),
        ("/dev/stdout2", False),
        ("/proc/self/fd/", False),
        ("/proc/self/fdinfo/3", False),
        ("/proc/x/fd/3", False),
        ("dev/fd/3", False),
        ("retail.ontology.yaml", False),
    ],
)
def test_descriptor_output_paths_are_recognized_exactly(path, expected):
  assert _is_descriptor_path(path) is expected


_DESCRIPTOR_OUTPUT_SCRIPT = textwrap.dedent(
    """\
    import sys

    from bigquery_ontology import dataplex_glossary_importer
    from bigquery_ontology.cli import app

    dataplex_glossary_importer.import_dataplex_glossary = (
        lambda *args, **kwargs: ("ontology: retail\\n", "")
    )
    app(["import-dataplex-glossary", sys.argv[1], "-o", sys.argv[2]])
    """
)


@pytest.mark.skipif(
    sys.platform == "win32", reason="needs POSIX standard-stream paths"
)
@pytest.mark.parametrize("stream", ["stdin", "stdout", "stderr"])
def test_import_dataplex_glossary_output_to_a_standard_stream_path_writes_through(
    tmp_path, stream
):
  stream_path = f"/dev/{stream}"
  if not os.path.exists(stream_path):
    pytest.skip(f"needs {stream_path}")
  src_dir = str(Path(dataplex_glossary_importer.__file__).resolve().parents[1])
  env = dict(os.environ)
  env["PYTHONPATH"] = os.pathsep.join(
      path for path in (src_dir, env.get("PYTHONPATH")) if path
  )
  target = tmp_path / "stream-target.yaml"
  fd = os.open(target, os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o644)
  try:
    inode_behind_fd = os.fstat(fd).st_ino
    streams = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.PIPE,
    }
    streams[stream] = fd  # The stream the output path names.
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            _DESCRIPTOR_OUTPUT_SCRIPT,
            _DP_GLOSSARY,
            stream_path,
        ],
        env=env,
        timeout=120,
        check=False,
        **streams,
    )
  finally:
    os.close(fd)

  assert proc.returncode == 0, proc.stderr or target.read_text(errors="replace")
  assert target.read_text(encoding="utf-8") == "ontology: retail\n"
  # Written through the stream, not replaced by a new file.
  assert target.stat().st_ino == inode_behind_fd
  assert [p.name for p in tmp_path.iterdir()] == [target.name]


@pytest.mark.parametrize("existing", [False, True], ids=["new", "existing"])
def test_import_dataplex_glossary_write_leaves_the_process_umask_alone(
    tmp_path, monkeypatch, dataplex_client, existing
):
  out = (
      _write_previous_output(tmp_path)
      if existing
      else tmp_path / "retail.ontology.yaml"
  )
  umask = os.umask(0o022)
  os.umask(umask)

  def _forbidden_umask(mask):
    raise AssertionError("writing the output must not change the umask")

  monkeypatch.setattr(os, "umask", _forbidden_umask)

  result = _RUNNER.invoke(
      app, ["import-dataplex-glossary", _DP_GLOSSARY, "-o", str(out)]
  )

  assert result.exit_code == 0
  assert out.read_text(encoding="utf-8") == _DP_EXPECTED_YAML
  assert stat.S_IMODE(out.stat().st_mode) == 0o666 & ~umask


def _fixed_temp_names(monkeypatch, *names):
  """The CLI's temporary file names come from ``names`` in order; the last
  one then repeats."""
  remaining = list(names)

  def _token_hex(nbytes=None):
    return remaining.pop(0) if len(remaining) > 1 else remaining[0]

  monkeypatch.setattr("bigquery_ontology.cli.secrets.token_hex", _token_hex)


def test_import_dataplex_glossary_temporary_file_never_reuses_a_taken_name(
    tmp_path, monkeypatch, dataplex_client
):
  squatter = tmp_path / ".gm-import-aaaaaaaaaaaa.tmp"
  squatter.write_text("squatter\n", encoding="utf-8")
  victim = tmp_path / "victim.txt"
  victim.write_text("victim\n", encoding="utf-8")
  planted = tmp_path / ".gm-import-bbbbbbbbbbbb.tmp"
  planted.symlink_to(victim.name)
  _fixed_temp_names(monkeypatch, "aaaaaaaaaaaa", "bbbbbbbbbbbb", "cccccccccccc")
  out = tmp_path / "retail.ontology.yaml"

  result = _RUNNER.invoke(
      app, ["import-dataplex-glossary", _DP_GLOSSARY, "-o", str(out)]
  )

  assert result.exit_code == 0
  assert out.read_text(encoding="utf-8") == _DP_EXPECTED_YAML
  # Neither the taken name nor the planted link was written through.
  assert squatter.read_text(encoding="utf-8") == "squatter\n"
  assert victim.read_text(encoding="utf-8") == "victim\n"
  assert planted.is_symlink()
  assert sorted(p.name for p in tmp_path.iterdir()) == sorted(
      [out.name, squatter.name, planted.name, victim.name]
  )


def test_import_dataplex_glossary_without_a_free_temporary_name_keeps_the_file(
    tmp_path, monkeypatch, dataplex_client
):
  squatter = tmp_path / ".gm-import-aaaaaaaaaaaa.tmp"
  squatter.write_text("squatter\n", encoding="utf-8")
  _fixed_temp_names(monkeypatch, "aaaaaaaaaaaa")
  out = _write_previous_output(tmp_path)

  result = _RUNNER.invoke(
      app, ["import-dataplex-glossary", _DP_GLOSSARY, "-o", str(out)]
  )

  assert result.exit_code == 1
  assert result.stderr.startswith(f"{out}:0:0: cli-output-error")
  assert "no unused temporary file name" in result.stderr
  assert "Imported Dataplex glossary" not in result.stderr
  assert out.read_text(encoding="utf-8") == "previous: content\n"
  assert squatter.read_text(encoding="utf-8") == "squatter\n"
  assert sorted(p.name for p in tmp_path.iterdir()) == sorted(
      [out.name, squatter.name]
  )


@pytest.mark.parametrize(
    "existing", [True, False], ids=["existing", "dangling"]
)
def test_import_dataplex_glossary_symlinked_output_is_staged_beside_its_target(
    tmp_path, monkeypatch, dataplex_client, existing
):
  links = tmp_path / "links"
  real = tmp_path / "real"
  links.mkdir()
  real.mkdir()
  target = real / "retail.ontology.yaml"
  if existing:
    target.write_text("previous: content\n", encoding="utf-8")
  link = links / "retail.ontology.yaml"
  link.symlink_to(os.path.join("..", "real", target.name))
  replaced = []
  real_replace = os.replace

  def _spy_replace(src, dst):
    replaced.append((Path(src).parent, Path(dst)))
    real_replace(src, dst)

  monkeypatch.setattr(os, "replace", _spy_replace)

  result = _RUNNER.invoke(
      app, ["import-dataplex-glossary", _DP_GLOSSARY, "-o", str(link)]
  )

  assert result.exit_code == 0
  # The temporary file lives in the target's real directory, so the swap
  # stays on one filesystem whatever the link's directory is.
  assert replaced == [(real.resolve(), target.resolve())]
  assert target.read_text(encoding="utf-8") == _DP_EXPECTED_YAML
  assert link.is_symlink()
  assert [p.name for p in real.iterdir()] == [target.name]
  assert [p.name for p in links.iterdir()] == [link.name]


class _Interrupted(BaseException):
  """Stands in for ``KeyboardInterrupt``, which would stop the test run."""


def test_import_dataplex_glossary_interrupted_write_removes_its_temporary_file(
    tmp_path, monkeypatch, dataplex_client
):
  out = _write_previous_output(tmp_path)

  def _interrupt(src, dst):
    raise _Interrupted

  monkeypatch.setattr(os, "replace", _interrupt)

  with pytest.raises(_Interrupted):
    _RUNNER.invoke(
        app, ["import-dataplex-glossary", _DP_GLOSSARY, "-o", str(out)]
    )

  assert out.read_text(encoding="utf-8") == "previous: content\n"
  assert [p.name for p in tmp_path.iterdir()] == [out.name]


def _raise_from_write(monkeypatch):
  def _gremlin(path, text):
    raise RuntimeError("disk gremlin")

  monkeypatch.setattr("bigquery_ontology.cli._write_text_atomically", _gremlin)


def test_import_dataplex_glossary_unexpected_write_error_exits_3(
    tmp_path, monkeypatch, dataplex_client
):
  _raise_from_write(monkeypatch)
  out = tmp_path / "retail.ontology.yaml"

  result = _RUNNER.invoke(
      app, ["import-dataplex-glossary", _DP_GLOSSARY, "-o", str(out)]
  )

  assert result.exit_code == 3
  assert result.stdout == ""
  assert result.stderr == "internal error: disk gremlin\n"
  assert not out.exists()


def test_import_dataplex_glossary_unexpected_write_error_json(
    tmp_path, monkeypatch, dataplex_client
):
  _raise_from_write(monkeypatch)
  out = tmp_path / "retail.ontology.yaml"

  result = _RUNNER.invoke(
      app,
      ["import-dataplex-glossary", _DP_GLOSSARY, "-o", str(out), "--json"],
  )

  assert result.exit_code == 3
  assert result.stdout == ""
  assert json.loads(result.stderr) == [
      {
          "file": str(out),
          "line": 0,
          "col": 0,
          "rule": "cli-internal-error",
          "severity": "error",
          "message": "internal error: disk gremlin",
      }
  ]


def _fail_stdout_writes(monkeypatch, error):
  """Make every ``typer.echo`` to stdout raise ``error``; stderr works."""
  real_echo = typer.echo

  def _echo(message=None, file=None, nl=True, err=False, color=None):
    if not err and file is None:
      raise error
    real_echo(message, file=file, nl=nl, err=err, color=color)

  monkeypatch.setattr(typer, "echo", _echo)


def test_import_dataplex_glossary_stdout_write_error_exits_1(
    monkeypatch, dataplex_client
):
  _fail_stdout_writes(monkeypatch, BrokenPipeError(errno.EPIPE, "Broken pipe"))

  result = _RUNNER.invoke(app, ["import-dataplex-glossary", _DP_GLOSSARY])

  assert result.exit_code == 1
  # The summary follows the YAML, so a failed write leaves only the error.
  assert result.stderr == (
      "<stdout>:0:0: cli-output-error \u2014 Cannot write to stdout: "
      "[Errno 32] Broken pipe\n"
  )


def test_import_dataplex_glossary_stdout_write_error_json(
    monkeypatch, dataplex_client
):
  _fail_stdout_writes(monkeypatch, BrokenPipeError(errno.EPIPE, "Broken pipe"))

  result = _RUNNER.invoke(
      app, ["import-dataplex-glossary", _DP_GLOSSARY, "--json"]
  )

  assert result.exit_code == 1
  assert json.loads(result.stderr) == [
      {
          "file": "<stdout>",
          "line": 0,
          "col": 0,
          "rule": "cli-output-error",
          "severity": "error",
          "message": "Cannot write to stdout: [Errno 32] Broken pipe",
      }
  ]


def test_import_dataplex_glossary_unexpected_stdout_error_json(
    monkeypatch, dataplex_client
):
  _fail_stdout_writes(monkeypatch, RuntimeError("stdout gremlin"))

  result = _RUNNER.invoke(
      app, ["import-dataplex-glossary", _DP_GLOSSARY, "--json"]
  )

  assert result.exit_code == 3
  assert json.loads(result.stderr) == [
      {
          "file": "<stdout>",
          "line": 0,
          "col": 0,
          "rule": "cli-internal-error",
          "severity": "error",
          "message": "internal error: stdout gremlin",
      }
  ]


_BROKEN_PIPE_SCRIPT = textwrap.dedent(
    """\
    import sys

    from bigquery_ontology import dataplex_glossary_importer
    from bigquery_ontology.cli import app

    dataplex_glossary_importer.import_dataplex_glossary = (
        lambda *args, **kwargs: ("ontology: retail\\n", "summary\\n")
    )
    app(["import-dataplex-glossary", sys.argv[1], "--json"])
    """
)


@pytest.mark.skipif(
    sys.platform == "win32", reason="needs POSIX broken-pipe semantics"
)
def test_import_dataplex_glossary_broken_stdout_pipe_leaves_one_json_error():
  src_dir = str(Path(dataplex_glossary_importer.__file__).resolve().parents[1])
  env = dict(os.environ)
  env["PYTHONPATH"] = os.pathsep.join(
      path for path in (src_dir, env.get("PYTHONPATH")) if path
  )
  read_end, write_end = os.pipe()
  os.close(read_end)  # Nobody reads stdout, so writing to it fails.
  try:
    proc = subprocess.run(
        [sys.executable, "-c", _BROKEN_PIPE_SCRIPT, _DP_GLOSSARY],
        stdout=write_end,
        stderr=subprocess.PIPE,
        env=env,
        timeout=120,
        check=False,
    )
  finally:
    os.close(write_end)

  # Without discarding stdout, Python's exit-time flush would add a second
  # error on stderr and turn exit code 1 into 120.
  assert proc.returncode == 1, proc.stderr
  errors = json.loads(proc.stderr)
  assert [(e["file"], e["rule"]) for e in errors] == [
      ("<stdout>", "cli-output-error")
  ]


class _NamelessGlossaryClient(_FakeDataplexGlossaryClient):
  """``GetGlossary`` answers without a resource name."""

  def get_glossary(self, request=None, **kwargs):
    super().get_glossary(request, **kwargs)
    return types.SimpleNamespace(name="", uid="u-1", description="")


def test_import_dataplex_glossary_nameless_response_exits_1(monkeypatch):
  client = _NamelessGlossaryClient()
  _use_fake_dataplex_client(monkeypatch, client)

  result = _RUNNER.invoke(
      app, ["import-dataplex-glossary", _DP_GLOSSARY, "--json"]
  )

  assert result.exit_code == 1
  assert result.stdout == ""
  errors = json.loads(result.stderr)
  assert [e["rule"] for e in errors] == ["import-validation"]
  assert "returned no glossary resource name" in errors[0]["message"]
  assert [method for method, _ in client.calls] == ["get_glossary"]


def test_import_dataplex_glossary_unexpected_error_exits_3(monkeypatch):
  def _boom(*args, **kwargs):
    raise RuntimeError("boom")

  monkeypatch.setattr(
      dataplex_glossary_importer, "import_dataplex_glossary", _boom
  )

  result = _RUNNER.invoke(app, ["import-dataplex-glossary", _DP_GLOSSARY])

  assert result.exit_code == 3
  assert result.stderr == "internal error: boom\n"


def test_import_dataplex_glossary_unexpected_error_json(monkeypatch):
  def _boom(*args, **kwargs):
    raise RuntimeError("boom")

  monkeypatch.setattr(
      dataplex_glossary_importer, "import_dataplex_glossary", _boom
  )

  result = _RUNNER.invoke(
      app, ["import-dataplex-glossary", _DP_GLOSSARY, "--json"]
  )

  assert result.exit_code == 3
  assert result.stdout == ""
  assert json.loads(result.stderr) == [
      {
          "file": _DP_GLOSSARY,
          "line": 0,
          "col": 0,
          "rule": "cli-internal-error",
          "severity": "error",
          "message": "internal error: boom",
      }
  ]


def test_import_dataplex_glossary_client_value_error_is_an_api_error(
    monkeypatch,
):
  class JSONDecodeError(ValueError):
    """Name-compatible with ``requests.exceptions.JSONDecodeError``."""

  client = _FakeDataplexGlossaryClient(
      error=JSONDecodeError("Expecting value: line 1 column 1 (char 0)")
  )
  _use_fake_dataplex_client(monkeypatch, client)

  result = _RUNNER.invoke(
      app, ["import-dataplex-glossary", _DP_GLOSSARY, "--json"]
  )

  assert result.exit_code == 1
  errors = json.loads(result.stderr)
  assert [e["rule"] for e in errors] == ["import-api-error"]
  assert errors[0]["message"].startswith(
      "Dataplex GetGlossary failed: JSONDecodeError: Expecting value"
  )


class _UnreachablePager:
  """A GAPIC-style pager whose only page reports an unreachable location;
  iterating it directly would silently yield nothing."""

  @property
  def pages(self):
    yield types.SimpleNamespace(
        categories=[], unreachable_locations=["us-east1"]
    )

  def __iter__(self):
    return iter(())


def test_import_dataplex_glossary_unreachable_locations_exit_1(
    monkeypatch, tmp_path
):
  client = _FakeDataplexGlossaryClient()
  client.list_glossary_categories = lambda request=None, **kw: (
      _UnreachablePager()
  )
  _use_fake_dataplex_client(monkeypatch, client)
  out = tmp_path / "retail.ontology.yaml"

  result = _RUNNER.invoke(
      app,
      ["import-dataplex-glossary", _DP_GLOSSARY, "-o", str(out), "--json"],
  )

  assert result.exit_code == 1
  errors = json.loads(result.stderr)
  assert [e["rule"] for e in errors] == ["import-api-error"]
  assert "could not reach location(s) us-east1" in errors[0]["message"]
  assert not out.exists()


@pytest.mark.parametrize(
    "glossary",
    [
        "projects/demo!/locations/us-central1/glossaries/retail",
        "projects/demo@corp/locations/us-central1/glossaries/retail",
        "projects/demo-project/locations/us-central1!/glossaries/retail",
        "projects/demo-project/locations/us@central1/glossaries/retail",
        "projects/123abc/locations/us-central1/glossaries/retail",
        "projects/UPPER-project/locations/us-central1/glossaries/retail",
        "projects/demo-project-/locations/us-central1/glossaries/retail",
        "projects/a::b/locations/us-central1/glossaries/retail",
        "projects/demo-project/locations/us-central1-/glossaries/retail",
    ],
)
def test_import_dataplex_glossary_bad_project_or_location_is_usage_error(
    monkeypatch, glossary
):
  _forbid_dataplex_client(monkeypatch)

  result = _RUNNER.invoke(app, ["import-dataplex-glossary", glossary])

  assert result.exit_code == 2
  assert result.stderr.startswith(
      "<cli>:0:0: cli-usage \u2014 Invalid glossary resource name"
  )


def test_root_help_lists_import_dataplex_glossary():
  result = _RUNNER.invoke(app, ["--help"])

  assert result.exit_code == 0
  assert "import-dataplex-glossary" in result.output
