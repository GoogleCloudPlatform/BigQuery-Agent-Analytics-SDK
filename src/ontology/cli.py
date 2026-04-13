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

"""``gm`` command-line interface (v0).

Implements the surface described in ``docs/ontology/cli.md``. Only
``gm validate`` is wired up in this revision; the ``gm compile`` and
``gm import-owl`` commands referenced by the spec will be added when
the binding and OWL-import modules land.

Exit codes:

  0 — success
  1 — validation / compilation error
  2 — usage error (bad flag, missing file)
  3 — internal error
"""

from __future__ import annotations

import json
from pathlib import Path
import sys

from pydantic import ValidationError
import typer
import yaml

from .loader import load_ontology_from_string

app = typer.Typer(
    name="gm",
    help="Graph-model CLI. Currently supports: validate.",
    add_completion=False,
    no_args_is_help=True,
)


@app.callback()
def _root() -> None:
  """Keep Typer in multi-command mode even when only one subcommand exists."""


# --------------------------------------------------------------------- #
# Error reporting                                                        #
# --------------------------------------------------------------------- #


def _emit_errors(
    errors: list[dict],
    *,
    as_json: bool,
) -> None:
  """Write structured errors to stderr in the requested format."""
  if as_json:
    typer.echo(json.dumps(errors, indent=2), err=True)
    return
  for e in errors:
    line = e.get("line") or 0
    col = e.get("col") or 0
    typer.echo(
        f"{e['file']}:{line}:{col}: {e['rule']} \u2014 {e['message']}",
        err=True,
    )


def _collect_errors(
    file: str,
    exc: BaseException,
) -> list[dict]:
  """Convert an exception raised during loading into structured errors."""
  if isinstance(exc, ValidationError):
    out: list[dict] = []
    for err in exc.errors():
      loc = ".".join(str(p) for p in err.get("loc", ())) or "<root>"
      out.append(
          {
              "file": file,
              "line": 0,
              "col": 0,
              "rule": f"ontology-shape:{err.get('type', 'invalid')}",
              "severity": "error",
              "message": f"{loc}: {err.get('msg', '')}",
          }
      )
    return out

  if isinstance(exc, yaml.YAMLError):
    line = 0
    col = 0
    mark = getattr(exc, "problem_mark", None)
    if mark is not None:
      line = mark.line + 1
      col = mark.column + 1
    return [
        {
            "file": file,
            "line": line,
            "col": col,
            "rule": "yaml-parse",
            "severity": "error",
            "message": str(exc),
        }
    ]

  return [
      {
          "file": file,
          "line": 0,
          "col": 0,
          "rule": "ontology-validation",
          "severity": "error",
          "message": str(exc),
      }
  ]


# --------------------------------------------------------------------- #
# File-kind detection                                                    #
# --------------------------------------------------------------------- #


def _detect_kind(text: str) -> str:
  """Return ``'ontology'``, ``'binding'``, or ``'unknown'``.

  Raises ``yaml.YAMLError`` on parse failure so the caller can route it
  through the ``yaml-parse`` error path.
  """
  # TODO: this re-parses the YAML that ``load_ontology_from_string`` will
  # parse again. Negligible for typical hand-authored specs, but for
  # large ontologies consider returning the parsed dict and threading it
  # into a ``load_ontology_from_dict`` variant.
  data = yaml.safe_load(text)
  if not isinstance(data, dict):
    return "unknown"
  if "ontology" in data and "binding" not in data:
    return "ontology"
  if "binding" in data:
    return "binding"
  return "unknown"


# --------------------------------------------------------------------- #
# gm validate                                                            #
# --------------------------------------------------------------------- #


@app.command("validate")
def validate(
    # Existence/readability are validated inside the command (not via
    # ``exists=True``) so that ``--json`` can produce a structured error
    # instead of falling through to Typer's human usage text.
    file: Path = typer.Argument(
        ...,
        help="Path to an ontology or binding YAML file.",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit structured JSON errors on stderr.",
    ),
) -> None:
  """Validate a single ontology or binding YAML file."""
  if not file.exists() or not file.is_file():
    _emit_errors(
        [
            {
                "file": str(file),
                "line": 0,
                "col": 0,
                "rule": "cli-missing-file",
                "severity": "error",
                "message": f"File not found: {file}",
            }
        ],
        as_json=json_output,
    )
    raise typer.Exit(code=2)

  text = file.read_text(encoding="utf-8")
  try:
    kind = _detect_kind(text)
  except yaml.YAMLError as exc:
    _emit_errors(_collect_errors(str(file), exc), as_json=json_output)
    raise typer.Exit(code=1)

  if kind == "binding":
    # Binding validation is part of the documented surface in cli.md but
    # depends on the binding loader/validator (binding.md), which is not
    # yet in the tree. Surface a deliberate "deferred" error rather than
    # silently treating bindings like ontologies. Update both this branch
    # and cli.md when the binding implementation lands.
    _emit_errors(
        [
            {
                "file": str(file),
                "line": 0,
                "col": 0,
                "rule": "cli-binding-deferred",
                "severity": "error",
                "message": (
                    "Binding validation is not yet implemented; only ontology "
                    "files are supported in this revision of gm validate."
                ),
            }
        ],
        as_json=json_output,
    )
    raise typer.Exit(code=2)

  if kind != "ontology":
    _emit_errors(
        [
            {
                "file": str(file),
                "line": 0,
                "col": 0,
                "rule": "cli-unknown-kind",
                "severity": "error",
                "message": (
                    "File is neither an ontology (top-level 'ontology:') nor a "
                    "binding (top-level 'binding:')."
                ),
            }
        ],
        as_json=json_output,
    )
    raise typer.Exit(code=2)

  try:
    load_ontology_from_string(text)
  except (ValueError, ValidationError, yaml.YAMLError) as exc:
    _emit_errors(_collect_errors(str(file), exc), as_json=json_output)
    raise typer.Exit(code=1)
  except Exception as exc:  # pragma: no cover - defensive
    typer.echo(f"internal error: {exc}", err=True)
    raise typer.Exit(code=3)
  # Success: nothing on stdout (cli.md §4).


def main() -> None:
  """Entry point for the ``gm`` console script."""
  app()


if __name__ == "__main__":
  sys.exit(app() or 0)
