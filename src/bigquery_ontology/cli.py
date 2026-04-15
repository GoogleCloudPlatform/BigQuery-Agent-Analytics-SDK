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

"""``gm`` command-line interface.

``gm validate`` accepts either an ontology YAML or a binding YAML and
dispatches to the matching loader. ``gm compile`` takes a binding YAML
and emits the corresponding ``CREATE PROPERTY GRAPH`` DDL on stdout
(or to ``-o PATH``). ``gm scaffold`` generates starter ``CREATE TABLE``
DDL and a matching binding stub from an ontology. Both ``validate`` and
``compile`` resolve a binding's companion ontology by auto-discovering
``<name>.ontology.yaml`` next to the binding; ``--ontology PATH``
overrides that lookup.

Exit codes:

  0 — success
  1 — validation / compilation error
  2 — usage error (bad flag, missing file, missing companion ontology,
      compile invoked on a non-binding file)
  3 — internal error
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys

from pydantic import ValidationError
import typer
import yaml

from .binding_loader import load_binding
from .binding_loader import load_binding_from_string
from .binding_models import Binding
from .graph_ddl_compiler import compile_graph
from .ontology_loader import load_ontology
from .ontology_loader import load_ontology_from_string
from .ontology_models import Ontology
from .scaffold import scaffold

app = typer.Typer(
    name="gm",
    help="Graph-model CLI. Commands: validate, compile, scaffold.",
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
    *,
    kind: str,
) -> list[dict]:
  """Convert an exception raised during loading into structured errors.

  ``kind`` is either ``"ontology"`` or ``"binding"`` and is used purely
  to tag the ``rule`` field on shape and semantic errors so downstream
  tooling can tell which validator produced them. YAML-parse errors
  share a single ``yaml-parse`` rule regardless of kind.
  """
  if isinstance(exc, ValidationError):
    out: list[dict] = []
    for err in exc.errors():
      loc = ".".join(str(p) for p in err.get("loc", ())) or "<root>"
      out.append(
          {
              "file": file,
              "line": 0,
              "col": 0,
              "rule": f"{kind}-shape:{err.get('type', 'invalid')}",
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
          "rule": f"{kind}-validation",
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
    # Type is ``str`` rather than ``Path`` because Typer maps
    # ``pathlib.Path`` to ``click.Path(readable=True)``, which
    # pre-validates readability and emits human usage text on failure —
    # bypassing ``--json`` structured output.
    file: str = typer.Argument(
        ...,
        help="Path to an ontology or binding YAML file.",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit structured JSON errors on stderr.",
    ),
    # Same ``str`` rationale as ``file`` above.
    ontology_path: str | None = typer.Option(
        None,
        "--ontology",
        help=(
            "For binding files: path to the companion ontology YAML. "
            "Defaults to <ontology>.ontology.yaml next to the binding."
        ),
    ),
) -> None:
  """Validate a single ontology or binding YAML file."""
  file_path = Path(file)
  if not file_path.exists() or not file_path.is_file():
    _emit_errors(
        [
            {
                "file": file,
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
  if not os.access(file_path, os.R_OK):
    _emit_errors(
        [
            {
                "file": file,
                "line": 0,
                "col": 0,
                "rule": "cli-missing-file",
                "severity": "error",
                "message": f"File not readable: {file}",
            }
        ],
        as_json=json_output,
    )
    raise typer.Exit(code=2)

  text = file_path.read_text(encoding="utf-8")
  try:
    kind = _detect_kind(text)
  except yaml.YAMLError as exc:
    # kind is indeterminate (YAML failed before _detect_kind returned),
    # but _collect_errors uses the generic "yaml-parse" rule for
    # yaml.YAMLError regardless of kind, so the value is harmless.
    _emit_errors(
        _collect_errors(str(file), exc, kind="ontology"),
        as_json=json_output,
    )
    raise typer.Exit(code=1)

  if kind == "binding":
    resolved_ontology = (
        Path(ontology_path) if ontology_path is not None else None
    )
    _validate_binding_file(
        file_path, ontology_path=resolved_ontology, json_output=json_output
    )
    return

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
    _emit_errors(
        _collect_errors(str(file), exc, kind="ontology"),
        as_json=json_output,
    )
    raise typer.Exit(code=1)
  except Exception as exc:  # pragma: no cover - defensive
    typer.echo(f"internal error: {exc}", err=True)
    raise typer.Exit(code=3)
  # Success: nothing on stdout.


# --------------------------------------------------------------------- #
# gm compile                                                             #
# --------------------------------------------------------------------- #


@app.command("compile")
def compile_command(
    # All path params use ``str`` (not ``Path``) so Typer does not
    # pre-validate readability and bypass ``--json`` structured output.
    file: str = typer.Argument(
        ...,
        help="Path to a binding YAML file.",
    ),
    ontology_path: str | None = typer.Option(
        None,
        "--ontology",
        help=(
            "Path to the companion ontology YAML. Defaults to "
            "<ontology>.ontology.yaml next to the binding."
        ),
    ),
    output_path: str | None = typer.Option(
        None,
        "-o",
        "--output",
        help=(
            "Write DDL to this file instead of stdout. The file is "
            "overwritten if it already exists."
        ),
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit structured JSON errors on stderr.",
    ),
) -> None:
  """Compile a binding to BigQuery ``CREATE PROPERTY GRAPH`` DDL.

  On success, writes the DDL to stdout (or to ``--output PATH`` if
  provided) and exits 0 with nothing on stderr. On any failure,
  structured errors land on stderr and the DDL is not written.

  The input must be a binding YAML file. Ontology files cannot be
  compiled on their own (they're backend-neutral; they need a
  binding to pick up physical tables and columns).
  """
  file_path = Path(file)
  if not file_path.exists() or not file_path.is_file():
    _emit_errors(
        [
            {
                "file": file,
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
  if not os.access(file_path, os.R_OK):
    _emit_errors(
        [
            {
                "file": file,
                "line": 0,
                "col": 0,
                "rule": "cli-missing-file",
                "severity": "error",
                "message": f"File not readable: {file}",
            }
        ],
        as_json=json_output,
    )
    raise typer.Exit(code=2)

  text = file_path.read_text(encoding="utf-8")
  try:
    kind = _detect_kind(text)
  except yaml.YAMLError as exc:
    _emit_errors(
        _collect_errors(file, exc, kind="binding"),
        as_json=json_output,
    )
    raise typer.Exit(code=1)

  if kind != "binding":
    if kind == "ontology":
      message = "gm compile requires a binding file; got an ontology."
    else:
      message = (
          "gm compile requires a binding file (top-level "
          "'binding:'); got neither an ontology nor a binding."
      )
    _emit_errors(
        [
            {
                "file": file,
                "line": 0,
                "col": 0,
                "rule": "cli-wrong-kind",
                "severity": "error",
                "message": message,
            }
        ],
        as_json=json_output,
    )
    raise typer.Exit(code=2)

  resolved_ontology = Path(ontology_path) if ontology_path is not None else None
  ontology, binding = _load_ontology_and_binding(
      file_path, ontology_path=resolved_ontology, json_output=json_output
  )

  try:
    ddl = compile_graph(ontology, binding)
  except ValueError as exc:
    _emit_errors(
        _collect_errors(file, exc, kind="compile"),
        as_json=json_output,
    )
    raise typer.Exit(code=1)
  except Exception as exc:  # pragma: no cover - defensive
    typer.echo(f"internal error: {exc}", err=True)
    raise typer.Exit(code=3)

  if output_path is not None:
    resolved_output = Path(output_path)
    try:
      resolved_output.write_text(ddl, encoding="utf-8")
    except (FileNotFoundError, PermissionError) as exc:
      _emit_errors(
          [
              {
                  "file": str(resolved_output),
                  "line": 0,
                  "col": 0,
                  "rule": "cli-output-error",
                  "severity": "error",
                  "message": f"Cannot write output file: {exc}",
              }
          ],
          as_json=json_output,
      )
      raise typer.Exit(code=1)
  else:
    typer.echo(ddl, nl=False)


# --------------------------------------------------------------------- #
# gm scaffold                                                            #
# --------------------------------------------------------------------- #

_VALID_NAMING = {"snake", "preserve"}


@app.command("scaffold")
def scaffold_command(
    ontology_path: str = typer.Option(
        ...,
        "--ontology",
        help="Path to an ontology YAML file.",
    ),
    dataset: str = typer.Option(
        ...,
        "--dataset",
        help="BigQuery dataset name for generated tables.",
    ),
    out: str = typer.Option(
        ...,
        "--out",
        help="Output directory for table_ddl.sql and binding.yaml.",
    ),
    naming: str = typer.Option(
        "snake",
        "--naming",
        help="Column/table naming: 'snake' (default) or 'preserve'.",
    ),
    project: str = typer.Option(
        ...,
        "--project",
        help="BigQuery project ID.",
    ),
    json_output: bool = typer.Option(
        False,
        "--json",
        help="Emit structured JSON errors on stderr.",
    ),
) -> None:
  """Generate starter CREATE TABLE DDL and a binding stub from an ontology.

  Writes ``table_ddl.sql`` and ``binding.yaml`` to the ``--out``
  directory. The output is user-owned — edit freely after generation.
  The generated binding is immediately valid as input to ``gm compile``.
  """
  if naming not in _VALID_NAMING:
    _emit_errors(
        [
            {
                "file": "<cli>",
                "line": 0,
                "col": 0,
                "rule": "cli-usage",
                "severity": "error",
                "message": (
                    f"Invalid --naming value {naming!r}; "
                    f"expected one of: {', '.join(sorted(_VALID_NAMING))}."
                ),
            }
        ],
        as_json=json_output,
    )
    raise typer.Exit(code=2)

  ont_path = Path(ontology_path)
  if not ont_path.exists() or not ont_path.is_file():
    _emit_errors(
        [
            {
                "file": ontology_path,
                "line": 0,
                "col": 0,
                "rule": "cli-missing-file",
                "severity": "error",
                "message": f"File not found: {ontology_path}",
            }
        ],
        as_json=json_output,
    )
    raise typer.Exit(code=2)
  if not os.access(ont_path, os.R_OK):
    _emit_errors(
        [
            {
                "file": ontology_path,
                "line": 0,
                "col": 0,
                "rule": "cli-missing-file",
                "severity": "error",
                "message": f"File not readable: {ontology_path}",
            }
        ],
        as_json=json_output,
    )
    raise typer.Exit(code=2)

  out_path = Path(out)
  if out_path.exists() and not out_path.is_dir():
    _emit_errors(
        [
            {
                "file": str(out_path),
                "line": 0,
                "col": 0,
                "rule": "cli-output-error",
                "severity": "error",
                "message": (
                    f"Output path exists and is not a directory: {out_path}"
                ),
            }
        ],
        as_json=json_output,
    )
    raise typer.Exit(code=2)
  if out_path.is_dir() and any(out_path.iterdir()):
    _emit_errors(
        [
            {
                "file": str(out_path),
                "line": 0,
                "col": 0,
                "rule": "cli-non-empty-dir",
                "severity": "error",
                "message": (
                    f"Output directory is not empty: {out_path}. "
                    "Delete or move its contents before running scaffold."
                ),
            }
        ],
        as_json=json_output,
    )
    raise typer.Exit(code=2)

  try:
    ontology = load_ontology(ont_path)
  except (ValueError, ValidationError, yaml.YAMLError) as exc:
    _emit_errors(
        _collect_errors(ontology_path, exc, kind="ontology"),
        as_json=json_output,
    )
    raise typer.Exit(code=1)

  try:
    ddl_text, binding_text = scaffold(
        ontology, dataset=dataset, project=project, naming=naming
    )
  except ValueError as exc:
    _emit_errors(
        _collect_errors(ontology_path, exc, kind="scaffold"),
        as_json=json_output,
    )
    raise typer.Exit(code=1)
  except Exception as exc:  # pragma: no cover - defensive
    typer.echo(f"internal error: {exc}", err=True)
    raise typer.Exit(code=3)

  try:
    out_path.mkdir(parents=True, exist_ok=True)
    (out_path / "table_ddl.sql").write_text(ddl_text, encoding="utf-8")
    (out_path / "binding.yaml").write_text(binding_text, encoding="utf-8")
  except (FileNotFoundError, PermissionError, OSError) as exc:
    _emit_errors(
        [
            {
                "file": str(out_path),
                "line": 0,
                "col": 0,
                "rule": "cli-output-error",
                "severity": "error",
                "message": f"Cannot write to output directory: {exc}",
            }
        ],
        as_json=json_output,
    )
    raise typer.Exit(code=1)


def _validate_binding_file(
    file: Path,
    *,
    ontology_path: Path | None,
    json_output: bool,
) -> None:
  """Validate a binding file. Thin wrapper: load pair and discard."""
  _load_ontology_and_binding(
      file, ontology_path=ontology_path, json_output=json_output
  )
  # Success: nothing on stdout.


def _load_ontology_and_binding(
    file: Path,
    *,
    ontology_path: Path | None,
    json_output: bool,
) -> tuple[Ontology, Binding]:
  """Resolve, load, and return both sides of a binding + ontology pair.

  Shared by ``gm validate`` and ``gm compile``. The CLI resolves the
  companion ontology itself (rather than letting ``load_binding``
  auto-discover) so that errors surfaced inside the ontology file are
  reported against the ontology path with ``rule=ontology-validation``
  — not masked as a binding error.

  Resolution order:

    - ``--ontology PATH`` explicit flag, if supplied.
    - Otherwise peek at the binding YAML for its ``ontology:`` name
      and expect ``<name>.ontology.yaml`` next to the binding.

  Errors route by *which file* they originated in:

    - Missing companion file → ``cli-missing-ontology`` (exit 2).
    - Ontology parse/shape/validation error → tagged ``kind=ontology``
      with ``file`` set to the ontology path (exit 1).
    - Binding parse/shape/validation error → tagged ``kind=binding``
      with ``file`` set to the binding path (exit 1).

  Returns the pair on success. Any failure calls ``_emit_errors`` and
  raises ``typer.Exit`` — callers never see a partial result.
  """
  text = file.read_text(encoding="utf-8")

  # Peek at the binding to compute the companion path, unless the
  # caller supplied --ontology. A failed peek (malformed YAML, or no
  # parseable ontology name) leaves ``ontology_path`` as None; we
  # then defer to ``load_binding`` below to surface the real binding
  # error with proper kind-tagging.
  discovered_via_peek = False
  peeked_name: str | None = None
  if ontology_path is None:
    peeked_name = _peek_ontology_name(text)
    if peeked_name is not None:
      ontology_path = file.parent / f"{peeked_name}.ontology.yaml"
      discovered_via_peek = True

  if ontology_path is None:
    try:
      load_binding(file)
    except FileNotFoundError as exc:
      _emit_errors(
          [
              {
                  "file": str(file),
                  "line": 0,
                  "col": 0,
                  "rule": "cli-missing-ontology",
                  "severity": "error",
                  "message": str(exc),
              }
          ],
          as_json=json_output,
      )
      raise typer.Exit(code=2)
    except (ValueError, ValidationError, yaml.YAMLError) as exc:
      _emit_errors(
          _collect_errors(str(file), exc, kind="binding"),
          as_json=json_output,
      )
      raise typer.Exit(code=1)
    # If load_binding somehow succeeded without a peek path, the
    # caller lost the ontology object. Defensive: should not happen.
    raise typer.Exit(code=3)  # pragma: no cover

  if (
      not ontology_path.exists()
      or not ontology_path.is_file()
      or not os.access(ontology_path, os.R_OK)
  ):
    # Auto-discovery and explicit-flag paths get distinct messages —
    # the former explains *why* we looked where we did, the latter
    # simply reports what the user asked us to open.
    if discovered_via_peek:
      message = (
          f"Binding references ontology {_peek_ontology_name(text)!r}, "
          f"but no companion ontology file found at {ontology_path}."
      )
      reported_file = str(file)
    else:
      message = f"Ontology file not found: {ontology_path}"
      reported_file = str(ontology_path)
    _emit_errors(
        [
            {
                "file": reported_file,
                "line": 0,
                "col": 0,
                "rule": "cli-missing-ontology",
                "severity": "error",
                "message": message,
            }
        ],
        as_json=json_output,
    )
    raise typer.Exit(code=2)

  try:
    ontology = load_ontology(ontology_path)
  except (ValueError, ValidationError, yaml.YAMLError) as exc:
    _emit_errors(
        _collect_errors(str(ontology_path), exc, kind="ontology"),
        as_json=json_output,
    )
    raise typer.Exit(code=1)

  try:
    binding = load_binding_from_string(text, ontology=ontology)
  except (ValueError, ValidationError, yaml.YAMLError) as exc:
    _emit_errors(
        _collect_errors(str(file), exc, kind="binding"),
        as_json=json_output,
    )
    raise typer.Exit(code=1)
  except Exception as exc:  # pragma: no cover - defensive
    typer.echo(f"internal error: {exc}", err=True)
    raise typer.Exit(code=3)

  return ontology, binding


def _peek_ontology_name(binding_text: str) -> str | None:
  """Extract the ``ontology:`` name from a binding YAML string, or None."""
  try:
    data = yaml.safe_load(binding_text)
  except yaml.YAMLError:
    return None
  if isinstance(data, dict) and isinstance(data.get("ontology"), str):
    name = data["ontology"]
    return name if name else None
  return None


def main() -> None:
  """Entry point for the ``gm`` console script."""
  app()


if __name__ == "__main__":
  sys.exit(app() or 0)
