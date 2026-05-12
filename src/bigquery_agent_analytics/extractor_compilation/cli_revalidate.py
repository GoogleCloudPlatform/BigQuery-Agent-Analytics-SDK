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

"""``bqaa-revalidate-extractors`` CLI entry point (issue #75
follow-up to Milestone C2.d).

Operationalizes :func:`revalidate_compiled_extractors` so ops
can run periodic revalidation without writing Python.

This first PR keeps the input surface deliberately small —
**local inputs only**. A follow-up adds ``--events-bq-query``
once the CLI contract is stable; that path drags in
auth / location / pagination / error handling and is worth
isolating.

Usage::

    bqaa-revalidate-extractors \\
        --bundles-root /var/bqaa/synced-bundles \\
        --events-jsonl events.jsonl \\
        --reference-extractors-module my_project.references \\
        --thresholds-json thresholds.json \\
        --report-out report.json

Reference module contract:

The dotted-path module passed to
``--reference-extractors-module`` must expose, at module
scope:

* ``EXTRACTORS``: ``dict[str, Callable[[dict, Any],
  StructuredExtractionResult]]`` keyed by event_type. Same
  shape :func:`revalidate_compiled_extractors` accepts.
* ``RESOLVED_GRAPH``: the :class:`ResolvedGraph` produced by
  ``resolve(ontology, binding)``. The CLI doesn't carry
  ontology / binding flags — the reference module owns the
  validator-input contract because it's the same artifact
  that defined the event_type-to-callable mapping.
* ``SPEC`` (optional): forwarded to each extractor's
  ``(event, spec)`` call. Defaults to ``None`` when the
  module doesn't define it.

Exit codes (intentionally narrow):

* ``0`` — revalidation completed; if thresholds were supplied,
  every threshold passed.
* ``1`` — revalidation completed but at least one threshold was
  violated. The report JSON is still written; the caller
  inspects ``threshold_check.violations``.
* ``2`` — usage / load / input error: malformed flags, missing
  files, bad JSONL, bad reference module, mixed-fingerprint
  bundle root, threshold-JSON that fails
  :class:`RevalidationThresholds` validation, etc. The report
  is NOT written in this case.

Report JSON shape::

    {
      "report":         { ...RevalidationReport.to_json()... },
      "threshold_check": null | {
        "ok":         bool,
        "violations": [str, ...]
      }
    }

``threshold_check`` is ``null`` when ``--thresholds-json``
wasn't supplied; the report is still written so an operator
can inspect rates without committing to a gate.

Out of scope (deferred):

* **BigQuery event source** (``--events-bq-query``). Follow-up
  PR; brings auth + location + pagination + error handling.
* **Scheduled execution.** Operator owns cron / Cloud
  Scheduler / GitHub Actions; the CLI is a one-shot.
* **BQ persistence of reports.** ``--report-out`` writes a
  local file; pushing it elsewhere is the caller's concern.
"""

from __future__ import annotations

import argparse
import dataclasses
import importlib
import json
import pathlib
import sys
from typing import Any, Callable, Optional

from ..structured_extraction import StructuredExtractionResult
from .bundle_loader import discover_bundles
from .manifest import Manifest
from .revalidation import check_thresholds
from .revalidation import revalidate_compiled_extractors
from .revalidation import RevalidationReport
from .revalidation import RevalidationThresholds
from .revalidation import ThresholdCheckResult

# Stable exit codes — referenced in the module docstring and
# the CLI doc page.
EXIT_OK = 0
EXIT_THRESHOLD_VIOLATION = 1
EXIT_USAGE_ERROR = 2


class _CliError(Exception):
  """Raised inside :func:`_load_config` and :func:`_run` for
  usage / load / input problems. Caught at the
  :func:`main` boundary and converted into an
  ``EXIT_USAGE_ERROR`` exit code with the message on stderr.
  Plain ``ValueError`` would also work; this subclass exists
  to make intent explicit at the catch site."""


@dataclasses.dataclass(frozen=True)
class _CliConfig:
  """Resolved CLI inputs after argument parsing and module
  loading. Pure-data so tests can construct it directly."""

  events: list[dict]
  compiled_extractors: dict[str, Callable[..., StructuredExtractionResult]]
  reference_extractors: dict[str, Callable[..., StructuredExtractionResult]]
  resolved_graph: Any
  spec: Any
  thresholds: Optional[RevalidationThresholds]
  report_out: pathlib.Path


def main(argv: Optional[list[str]] = None) -> int:
  """CLI entry point. Returns an exit code; ``console_scripts``
  in ``pyproject.toml`` invokes this and propagates the
  return value to the shell.

  All usage / load / input errors funnel through the same
  ``_CliError`` -> ``EXIT_USAGE_ERROR`` boundary, including
  argparse's own "missing required argument" /
  "unrecognized argument" errors. The custom parser raises
  ``_CliError`` from ``error()`` instead of calling
  ``sys.exit(2)``, so ``main(argv)`` reliably **returns**
  an exit code per its documented contract rather than
  raising ``SystemExit`` mid-call. ``--help`` still goes
  through argparse's own ``SystemExit(0)`` path; that's the
  expected terminal behavior. The CLI does not define a
  ``--version`` action (deliberately out of scope until the
  package has a stable version-emission strategy)."""
  parser = _build_parser()
  try:
    args = parser.parse_args(argv)
  except _CliError as exc:
    print(f"bqaa-revalidate-extractors: {exc}", file=sys.stderr)
    return EXIT_USAGE_ERROR

  try:
    config = _load_config(args)
  except _CliError as exc:
    print(f"bqaa-revalidate-extractors: {exc}", file=sys.stderr)
    return EXIT_USAGE_ERROR

  try:
    return _run(config)
  except _CliError as exc:
    # ``_run`` raises ``_CliError`` only for input/usage
    # problems that surface after the harness starts (e.g.
    # an event_type covered by compiled but not reference —
    # the harness would skip; we still want to surface it).
    # Library-level exceptions from the harness propagate.
    print(f"bqaa-revalidate-extractors: {exc}", file=sys.stderr)
    return EXIT_USAGE_ERROR


class _NonExitingArgumentParser(argparse.ArgumentParser):
  """``argparse.ArgumentParser`` whose ``error()`` raises
  :class:`_CliError` instead of calling ``sys.exit(2)``.

  argparse's default ``error()`` writes a usage line + error
  message to stderr and then exits. That bypasses
  :func:`main`'s documented return-code contract — tests that
  call ``main([])`` would catch a ``SystemExit`` instead of
  receiving ``EXIT_USAGE_ERROR``. The override preserves the
  user-facing UX (usage line still goes to stderr) but funnels
  the error through the same ``_CliError`` boundary every
  other usage failure uses.

  ``exit()`` is **not** overridden, so ``--help`` still
  terminates via ``SystemExit(0)`` — that's the expected
  terminal behavior. The CLI does not define a ``--version``
  flag today."""

  def error(self, message: str) -> None:  # type: ignore[override]
    self.print_usage(sys.stderr)
    raise _CliError(f"argument error: {message}")


def _build_parser() -> argparse.ArgumentParser:
  parser = _NonExitingArgumentParser(
      prog="bqaa-revalidate-extractors",
      description=(
          "Run compiled-extractor revalidation against a local "
          "JSONL event corpus and a reference-extractors module."
      ),
  )
  parser.add_argument(
      "--bundles-root",
      type=pathlib.Path,
      required=True,
      help=(
          "Directory containing one subdirectory per compiled "
          "bundle. Auto-detects the expected fingerprint from "
          "the first bundle's manifest and requires every "
          "other bundle to match."
      ),
  )
  parser.add_argument(
      "--events-jsonl",
      type=pathlib.Path,
      required=True,
      help=(
          "Path to a JSONL file (one event JSON object per line). "
          "Each event must have ``event_type``; events for "
          "event_types without a compiled OR reference extractor "
          "are counted under ``skipped_events`` in the report."
      ),
  )
  parser.add_argument(
      "--reference-extractors-module",
      required=True,
      help=(
          "Dotted Python path to a module that exposes "
          "``EXTRACTORS`` (dict[str, callable]), ``RESOLVED_GRAPH`` "
          "(from ``resolve(ontology, binding)``), and optionally "
          "``SPEC`` (forwarded to extractor calls; defaults to "
          "``None``)."
      ),
  )
  parser.add_argument(
      "--thresholds-json",
      type=pathlib.Path,
      default=None,
      help=(
          "Optional path to a JSON file mapping "
          "``RevalidationThresholds`` field names to numeric "
          "rates in [0, 1]. When omitted, no threshold check is "
          "performed and the exit code is 0 on a successful run."
      ),
  )
  parser.add_argument(
      "--report-out",
      type=pathlib.Path,
      required=True,
      help=(
          "Path to write the combined JSON report (RevalidationReport "
          "+ ThresholdCheckResult). Parent directories are NOT "
          "created automatically; the caller owns the destination."
      ),
  )
  return parser


def _load_config(args: argparse.Namespace) -> _CliConfig:
  """Resolve CLI arguments into a :class:`_CliConfig`. Every
  user-input problem raises :class:`_CliError`; the
  :func:`main` boundary converts those into
  ``EXIT_USAGE_ERROR``.

  Order of validation is the cheapest-first / shape-before-
  semantics pattern: file paths exist → file contents parse →
  shapes line up → references resolve. A failure on an earlier
  gate prevents later gates from running with garbage inputs.
  """
  # Preflight ``--report-out`` first so we fail fast on a bad
  # destination *before* doing any work. The docs promise
  # parent directories are not created automatically; if the
  # parent doesn't exist we surface that here at exit 2 rather
  # than letting a ``FileNotFoundError`` escape later from
  # ``path.write_text(...)``. Permission / disk-full errors at
  # write time are wrapped in :func:`_write_report` as a
  # second line of defense.
  report_parent = args.report_out.parent
  # ``Path("foo").parent`` is ``Path(".")`` for a bare
  # filename; treat the current working directory as
  # implicitly present.
  if str(report_parent) not in ("", ".") and not report_parent.is_dir():
    raise _CliError(
        f"--report-out parent directory {str(report_parent)!r} does not "
        f"exist; create it before running revalidation (the CLI does not "
        f"create parent directories automatically)"
    )

  if not args.events_jsonl.is_file():
    raise _CliError(f"--events-jsonl {str(args.events_jsonl)!r} is not a file")
  events = _load_jsonl(args.events_jsonl)

  if not args.bundles_root.is_dir():
    raise _CliError(
        f"--bundles-root {str(args.bundles_root)!r} is not a directory"
    )
  fingerprint = _detect_expected_fingerprint(args.bundles_root)
  compiled_extractors = _load_compiled_extractors(
      args.bundles_root, fingerprint
  )

  ref_module = _import_reference_module(args.reference_extractors_module)
  reference_extractors, resolved_graph, spec = _read_reference_contract(
      ref_module, args.reference_extractors_module
  )

  thresholds = (
      None
      if args.thresholds_json is None
      else _load_thresholds(args.thresholds_json)
  )

  return _CliConfig(
      events=events,
      compiled_extractors=compiled_extractors,
      reference_extractors=reference_extractors,
      resolved_graph=resolved_graph,
      spec=spec,
      thresholds=thresholds,
      report_out=args.report_out,
  )


def _run(config: _CliConfig) -> int:
  """Execute revalidation and write the combined report.
  Returns the exit code."""
  report: RevalidationReport = revalidate_compiled_extractors(
      events=config.events,
      compiled_extractors=config.compiled_extractors,
      reference_extractors=config.reference_extractors,
      resolved_graph=config.resolved_graph,
      spec=config.spec,
  )

  threshold_result: Optional[ThresholdCheckResult] = (
      None
      if config.thresholds is None
      else check_thresholds(report, config.thresholds)
  )

  _write_report(
      path=config.report_out,
      report=report,
      threshold_result=threshold_result,
  )

  if threshold_result is not None and not threshold_result.ok:
    return EXIT_THRESHOLD_VIOLATION
  return EXIT_OK


# ------------------------------------------------------------------ #
# Helpers                                                              #
# ------------------------------------------------------------------ #


def _load_jsonl(path: pathlib.Path) -> list[dict]:
  """Parse a JSONL file into a list of dict events.

  Strictness contract: every non-empty line must parse to a
  JSON object. A malformed line aborts the CLI with
  ``EXIT_USAGE_ERROR`` naming the line number; the harness's
  per-event ``skipped_events`` counter exists for legitimately-
  shaped events whose ``event_type`` lacks coverage, NOT to
  paper over corrupt input."""
  events: list[dict] = []
  # Wrap open + iteration in try/except for OSError (permission
  # denied, file removed between is_file check and open, etc.)
  # and UnicodeError (invalid UTF-8 bytes — the docs promise
  # JSONL but a tampered or wrong-encoding file would raise
  # ``UnicodeDecodeError`` mid-iteration). Both surface as
  # ``_CliError`` so the CLI exits cleanly at code 2 with the
  # file path named, instead of leaking a raw traceback.
  try:
    with path.open("r", encoding="utf-8") as fh:
      for line_no, raw in enumerate(fh, start=1):
        line = raw.strip()
        if not line:
          continue
        try:
          obj = json.loads(line)
        except json.JSONDecodeError as exc:
          raise _CliError(
              f"--events-jsonl line {line_no}: invalid JSON: {exc.msg}"
          ) from exc
        if not isinstance(obj, dict):
          raise _CliError(
              f"--events-jsonl line {line_no}: expected a JSON object, got "
              f"{type(obj).__name__}"
          )
        events.append(obj)
  except UnicodeError as exc:
    raise _CliError(
        f"--events-jsonl {str(path)!r}: not valid UTF-8 "
        f"({type(exc).__name__}: {exc})"
    ) from exc
  except OSError as exc:
    raise _CliError(
        f"--events-jsonl {str(path)!r}: I/O error: "
        f"{type(exc).__name__}: {exc}"
    ) from exc
  return events


def _detect_expected_fingerprint(bundles_root: pathlib.Path) -> str:
  """Auto-detect the fingerprint from the first bundle's
  manifest. Every other bundle must declare the same
  fingerprint — mixed fingerprints are a deployment mistake
  for revalidation and fail-closed.

  Auto-detect rather than a CLI flag because revalidation
  runs against a single deployed configuration; the
  fingerprint is an artifact of the local files, not a value
  the operator should have to thread through every command.
  """
  candidates = sorted(p for p in bundles_root.iterdir() if p.is_dir())
  if not candidates:
    raise _CliError(
        f"--bundles-root {str(bundles_root)!r} contains no bundle "
        f"subdirectories"
    )
  fingerprints: dict[str, pathlib.Path] = {}
  for child in candidates:
    manifest_path = child / "manifest.json"
    if not manifest_path.exists():
      raise _CliError(f"--bundles-root: {child.name}/manifest.json not found")
    try:
      manifest = Manifest.from_json(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 — surface + abort
      raise _CliError(
          f"--bundles-root: {child.name}/manifest.json unreadable: "
          f"{type(exc).__name__}: {exc}"
      ) from exc
    fingerprints.setdefault(manifest.fingerprint, child)
  if len(fingerprints) > 1:
    by_fp = ", ".join(
        f"{fp}={path.name!r}" for fp, path in sorted(fingerprints.items())
    )
    raise _CliError(
        f"--bundles-root contains bundles for multiple fingerprints "
        f"({by_fp}); revalidation needs one active fingerprint per run"
    )
  return next(iter(fingerprints))


def _load_compiled_extractors(
    bundles_root: pathlib.Path,
    expected_fingerprint: str,
) -> dict[str, Callable[..., StructuredExtractionResult]]:
  """Run :func:`discover_bundles` and surface any per-bundle
  failures as a :class:`_CliError`. Empty registry is also a
  failure — the CLI was asked to revalidate a bundle root
  that produced zero usable extractors."""
  discovery = discover_bundles(
      bundles_root, expected_fingerprint=expected_fingerprint
  )
  if discovery.failures:
    detail = "; ".join(
        f"{f.bundle_dir.name if f.bundle_dir else '?'}: {f.code}: {f.detail}"
        for f in discovery.failures
    )
    raise _CliError(f"bundle discovery failed: {detail}")
  if not discovery.registry:
    raise _CliError(
        "bundle discovery produced an empty registry; no compiled "
        "extractors to revalidate"
    )
  return discovery.registry


def _import_reference_module(dotted_path: str) -> Any:
  """Import the reference-extractors module. Surfaces both
  module-not-found and import-time failures (e.g., the module
  raised at top-level) as :class:`_CliError` so the CLI
  doesn't leak a bare traceback for an input error."""
  if not dotted_path:
    raise _CliError("--reference-extractors-module is empty")
  try:
    return importlib.import_module(dotted_path)
  except ModuleNotFoundError as exc:
    raise _CliError(
        f"--reference-extractors-module {dotted_path!r} not importable: "
        f"{exc}"
    ) from exc
  except Exception as exc:  # noqa: BLE001 — top-level import error
    raise _CliError(
        f"--reference-extractors-module {dotted_path!r} raised on import: "
        f"{type(exc).__name__}: {exc}"
    ) from exc


def _read_reference_contract(
    module: Any,
    dotted_path: str,
) -> tuple[
    dict[str, Callable[..., StructuredExtractionResult]],
    Any,
    Any,
]:
  """Extract ``EXTRACTORS`` / ``RESOLVED_GRAPH`` / ``SPEC``
  from a reference module. Validates shape only — the actual
  callability and graph correctness are the harness's
  problem, but we want to fail loud at the CLI boundary if
  the module's surface doesn't match the documented
  contract."""
  if not hasattr(module, "EXTRACTORS"):
    raise _CliError(
        f"reference module {dotted_path!r} does not expose "
        f"`EXTRACTORS` at module scope"
    )
  extractors = module.EXTRACTORS
  if not isinstance(extractors, dict) or not extractors:
    raise _CliError(
        f"reference module {dotted_path!r} `EXTRACTORS` must be a "
        f"non-empty dict; got "
        f"{type(extractors).__name__} of length "
        f"{len(extractors) if hasattr(extractors, '__len__') else '?'}"
    )
  for event_type, fn in extractors.items():
    if not isinstance(event_type, str) or not event_type:
      raise _CliError(
          f"reference module {dotted_path!r} `EXTRACTORS` keys must be "
          f"non-empty strings; got {event_type!r}"
      )
    if not callable(fn):
      raise _CliError(
          f"reference module {dotted_path!r} `EXTRACTORS[{event_type!r}]` "
          f"is not callable; got {type(fn).__name__}"
      )

  if not hasattr(module, "RESOLVED_GRAPH"):
    raise _CliError(
        f"reference module {dotted_path!r} does not expose "
        f"`RESOLVED_GRAPH` at module scope"
    )
  resolved_graph = module.RESOLVED_GRAPH

  # ``SPEC`` is optional. Default to ``None`` matching the
  # harness's keyword default.
  spec = getattr(module, "SPEC", None)

  return extractors, resolved_graph, spec


def _load_thresholds(path: pathlib.Path) -> RevalidationThresholds:
  """Parse ``--thresholds-json`` into a
  :class:`RevalidationThresholds`. Unknown fields are
  rejected so a typo doesn't silently produce a no-op gate;
  bounds enforcement (rates in ``[0, 1]``) comes for free via
  ``RevalidationThresholds.__post_init__``."""
  if not path.is_file():
    raise _CliError(f"--thresholds-json {str(path)!r} is not a file")
  try:
    text = path.read_text(encoding="utf-8")
  except UnicodeError as exc:
    raise _CliError(
        f"--thresholds-json {str(path)!r}: not valid UTF-8 "
        f"({type(exc).__name__}: {exc})"
    ) from exc
  except OSError as exc:
    raise _CliError(
        f"--thresholds-json {str(path)!r}: I/O error: "
        f"{type(exc).__name__}: {exc}"
    ) from exc
  try:
    raw = json.loads(text)
  except json.JSONDecodeError as exc:
    raise _CliError(f"--thresholds-json invalid JSON: {exc.msg}") from exc
  if not isinstance(raw, dict):
    raise _CliError(
        f"--thresholds-json must be a JSON object; got " f"{type(raw).__name__}"
    )
  allowed = {f.name for f in dataclasses.fields(RevalidationThresholds)}
  unknown = sorted(set(raw) - allowed)
  if unknown:
    raise _CliError(
        f"--thresholds-json has unknown fields: {unknown}; allowed: "
        f"{sorted(allowed)}"
    )
  try:
    return RevalidationThresholds(**raw)
  except (TypeError, ValueError) as exc:
    raise _CliError(f"--thresholds-json validation failed: {exc}") from exc


def _write_report(
    *,
    path: pathlib.Path,
    report: RevalidationReport,
    threshold_result: Optional[ThresholdCheckResult],
) -> None:
  """Write the combined JSON report. The shape pins both
  dimensions so a downstream pipeline can index into the
  artifact without reconstructing the dataclasses."""
  payload = {
      "report": json.loads(report.to_json()),
      "threshold_check": (
          None
          if threshold_result is None
          else {
              "ok": threshold_result.ok,
              "violations": list(threshold_result.violations),
          }
      ),
  }
  # Preflight in :func:`_load_config` catches the common
  # missing-parent-dir typo; this catch handles the rest
  # (permissions, disk full, parent removed between preflight
  # and write). Surface as ``_CliError`` so :func:`main`
  # converts it to ``EXIT_USAGE_ERROR`` rather than letting
  # the traceback escape.
  try:
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
    )
  except OSError as exc:
    raise _CliError(
        f"--report-out {str(path)!r}: I/O error writing report: "
        f"{type(exc).__name__}: {exc}"
    ) from exc


if __name__ == "__main__":  # pragma: no cover — invoked via console_scripts
  sys.exit(main())
