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
dispatches to the matching loader. For binding files, the companion
ontology is auto-discovered as ``<name>.ontology.yaml`` next to the
binding by default; pass ``--ontology PATH`` to point at a specific
ontology file instead. The ``gm compile`` and ``gm import-owl`` commands
referenced elsewhere will be wired up when their modules land.

Exit codes:

  0 — success
  1 — validation / compilation error
  2 — usage error (bad flag, missing file, missing companion ontology)
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
from .loader import load_ontology
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
    # Type is ``str | None`` rather than ``Path | None`` because Typer
    # maps ``pathlib.Path`` to ``TyperPath(readable=True)``, which
    # pre-validates readability and emits human usage text on failure —
    # bypassing ``--json`` structured output.
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
    # kind is indeterminate (YAML failed before _detect_kind returned),
    # but _collect_errors uses the generic "yaml-parse" rule for
    # yaml.YAMLError regardless of kind, so the value is harmless.
    _emit_errors(
        _collect_errors(str(file), exc, kind="ontology"),
        as_json=json_output,
    )
    raise typer.Exit(code=1)

  if kind == "binding":
    resolved_ontology = Path(ontology_path) if ontology_path is not None else None
    _validate_binding_file(
        file, ontology_path=resolved_ontology, json_output=json_output
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


def _validate_binding_file(
    file: Path,
    *,
    ontology_path: Path | None,
    json_output: bool,
) -> None:
  """Dispatch to the binding loader.

  The CLI resolves and loads the companion ontology itself rather than
  letting ``load_binding`` auto-discover, because otherwise an error
  surfaced inside the ontology file (e.g. a bad key reference) would
  bubble out of ``load_binding`` as a generic ``ValueError`` and get
  reported with ``file=<binding>`` and ``rule=binding-validation`` —
  misleading, since the real fault is in the ontology.

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
  """
  text = file.read_text(encoding="utf-8")

  # If the caller didn't supply --ontology, try to compute the companion
  # path from the binding itself. A failed peek (malformed YAML, or no
  # parseable ontology name) leaves ``ontology_path`` as None; the binding
  # loader below will then surface the real shape/parse error.
  discovered_via_peek = False
  peeked_name: str | None = None
  if ontology_path is None:
    peeked_name = _peek_ontology_name(text)
    if peeked_name is not None:
      ontology_path = file.parent / f"{peeked_name}.ontology.yaml"
      discovered_via_peek = True

  ontology = None
  if ontology_path is not None:
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
            f"Binding references ontology {peeked_name!r}, "
            f"but no companion ontology file found at {ontology_path}."
        )
      else:
        message = f"Ontology file not found: {ontology_path}"
      _emit_errors(
          [
              {
                  "file": str(ontology_path)
                  if not discovered_via_peek
                  else str(file),
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
    if ontology is not None:
      load_binding_from_string(text, ontology=ontology)
    else:
      # Peek failed — defer to load_binding so the underlying yaml /
      # pydantic error surfaces directly against the binding file.
      load_binding(file)
  except FileNotFoundError as exc:
    # Only reachable when ``ontology is None`` (peek failed), and
    # ``load_binding``'s own discovery then raised. Treat the same as
    # the peek-found-but-missing case.
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
  except Exception as exc:  # pragma: no cover - defensive
    typer.echo(f"internal error: {exc}", err=True)
    raise typer.Exit(code=3)
  # Success: nothing on stdout.


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
