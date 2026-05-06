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

"""Diagnostic builders for retry-prompt feedback (PR 4b.2.2.c.1).

When the compile pipeline rejects an LLM-emitted plan, the retry
orchestrator (PR 4b.2.2.c.2) feeds a *diagnostic* string back into
the next prompt so the LLM can fix what failed. These functions
own the diagnostic format.

Coverage:

* :func:`build_plan_parse_diagnostic` for ``PlanParseError`` from
  4b.2.2.a.
* :func:`build_ast_diagnostic` for ``AstReport`` from 4b.1.
* :func:`build_smoke_diagnostic` for ``SmokeTestReport`` from 4b.1
  (per-event exceptions, wrong return types, non-empty floor, #76
  graph-validator failures).
* :func:`build_compile_result_diagnostic` for the top-level
  ``CompileResult`` envelope, which can additionally fail with
  ``invalid_identifier`` / ``invalid_event_types`` / ``load_error``
  *before* or *after* the AST and smoke gates. This is the entry
  point the retry orchestrator should prefer — it has the full
  ``CompileResult`` in hand, so handing it back the right
  diagnostic is one call rather than a hand-rolled switch.
* :func:`build_gate_diagnostic` is a thin dispatcher so the loop
  doesn't have to switch on payload type itself.

Three contracts the diagnostic text has to clear:

* **Actionable.** The LLM has to be able to act on the message
  without re-deriving where in its previous response the problem
  was. Per-failure entries surface the stable ``code`` from the
  underlying gate (``PlanParseError.code``, ``AstFailure.code``,
  ``ValidationFailure.code``, or the ``CompileResult`` field name)
  plus the dotted ``path`` or source line, so the LLM can grep its
  own output for the offending field / line.
* **Bounded.** Diagnostics get embedded in retry prompts; long
  tracebacks or hundreds of validator failures would crowd out the
  rule + schema grounding in the prompt itself. Each section is
  capped at the first ten failures with an ``... and N more
  (truncated)`` line; tracebacks are reduced to their last
  informative line (the exception type + message).
* **Deterministic.** Same input report → byte-identical output.
  Sorting where applicable; otherwise preserve input order, which
  itself is event-iteration order in the reports the gates produce.

The per-gate builders never raise — they always return a string,
even on degenerate input (empty traceback, missing line numbers,
``ok=False`` with no field populated). The dispatcher
:func:`build_gate_diagnostic` is the only function in this module
that *does* raise: it validates caller input and raises
``TypeError`` for a payload type that doesn't match ``kind``, and
``ValueError`` for an unknown ``kind``. Both errors are caller-
misuse signals, not gate-failure signals; the retry orchestrator
in PR 4b.2.2.c.2 should never see them in practice.

PR 4b.2.2.c.2 layers the retry-prompt builder +
``compile_with_llm`` orchestrator on top.
"""

from __future__ import annotations

from typing import Any, Union

from .ast_validator import AstReport
from .compiler import CompileResult
from .plan_parser import PlanParseError
from .smoke_test import SmokeTestReport

# Cap on per-section failure listings. Hard-coded for 4b.2.2.c.1;
# the retry orchestrator (4b.2.2.c.2) can expose a knob if real-
# world prompts need a different tradeoff.
_MAX_LISTED_FAILURES = 10


def build_plan_parse_diagnostic(error: PlanParseError) -> str:
  """Return a one-line diagnostic for a parser failure.

  Format: ``PlanParseError [code=<code>] at <path>: <message>``.
  Empty paths render as ``<root>`` so the LLM sees an explicit
  anchor instead of blank space — a common readability trip
  with structured-output failures.
  """
  location = error.path if error.path else "<root>"
  return (
      f"PlanParseError [code={error.code}] at {location}: " f"{error.message}"
  )


def build_ast_diagnostic(report: AstReport) -> str:
  """Return a multi-line diagnostic listing each AST failure.

  Each failure renders as one line with the source location
  (``line N col M:`` when both are known, ``line N:`` when only
  line is known, ``<no-line>`` otherwise), the stable failure
  ``code``, and the short ``detail``.

  The list is capped at ten entries; the eleventh and beyond are
  summarized as ``... and N more (truncated)``. Order matches the
  ``ast.walk`` traversal in the validator (roughly top-to-bottom),
  which is what the LLM sees when scanning its own source.
  """
  if report.ok:
    return "AST validation passed (no diagnostic to render)."

  total = len(report.failures)
  shown = report.failures[:_MAX_LISTED_FAILURES]
  truncated = total > _MAX_LISTED_FAILURES

  lines: list[str] = [
      f"AST validation failed ({total} issue{_pluralize(total)}):"
  ]
  for failure in shown:
    location = _format_source_location(failure.line, failure.col)
    lines.append(f"  {location} {failure.code}: {failure.detail}")
  if truncated:
    lines.append(f"  ... and {total - _MAX_LISTED_FAILURES} more (truncated)")
  return "\n".join(lines)


def build_smoke_diagnostic(report: SmokeTestReport) -> str:
  """Return a multi-section diagnostic covering everything the
  smoke runner can flag.

  Sections (each appears only when it has content):

  * **Per-event exceptions** — extractor crashed at runtime.
    Tracebacks are reduced to their last informative line (the
    exception type + message) so a 30-line stack frame doesn't
    crowd out the actionable bit.
  * **Wrong return types** — extractor returned the wrong shape
    or malformed ``StructuredExtractionResult`` internals.
  * **Non-empty floor** — extractor produced empty results for
    every event, below the configured ``min_nonempty_results``.
  * **#76 graph validator failures** — extractor produced a
    structurally-valid result that violates the ontology.

  Each list is capped at ten entries with truncation summary.
  """
  if report.ok:
    return "Smoke test passed (no diagnostic to render)."

  sections: list[str] = ["Smoke test failed:"]

  if report.exceptions:
    sections.append("")
    sections.extend(
        _render_capped_section(
            heading=(
                f"  Per-event exceptions "
                f"({report.events_with_exception} of "
                f"{report.events_processed} events):"
            ),
            entries=[_traceback_last_line(tb) for tb in report.exceptions],
        )
    )

  if report.wrong_return_types:
    sections.append("")
    sections.extend(
        _render_capped_section(
            heading=(
                f"  Wrong return types "
                f"({report.events_with_wrong_return_type} of "
                f"{report.events_processed} events):"
            ),
            entries=list(report.wrong_return_types),
        )
    )

  if report.events_with_nonempty_result < report.min_nonempty_results:
    sections.append("")
    sections.append(
        f"  Non-empty floor: {report.events_with_nonempty_result} of "
        f"{report.events_processed} events produced non-empty output; "
        f"required >= {report.min_nonempty_results}."
    )

  if report.validation_failures:
    sections.append("")
    sections.extend(
        _render_capped_section(
            heading=(
                f"  #76 graph validator failures "
                f"({len(report.validation_failures)}):"
            ),
            entries=[
                f"[{vf.scope.value}] {vf.code} at {vf.path}: {vf.detail}"
                for vf in report.validation_failures
            ],
        )
    )

  return "\n".join(sections)


def build_compile_result_diagnostic(result: CompileResult) -> str:
  """Return a diagnostic for any :class:`CompileResult` failure mode.

  ``compile_extractor`` short-circuits on the first failed gate, so
  a rejected ``CompileResult`` produced by the canonical pipeline
  carries exactly one populated failure source. This builder
  collapses the union back into a single string the retry
  orchestrator (PR 4b.2.2.c.2) can feed back to the LLM regardless
  of *which* gate rejected the candidate. The check order mirrors
  the pipeline's own stage order, so for a canonical
  ``CompileResult`` the message names the *earliest* failed stage:

  1. ``invalid_identifier`` — bad ``module_name`` / ``function_name``
     (path-traversal-shaped, Python keyword, …) rejected at
     pipeline stage 1. Rendered as
     ``CompileError [code=invalid_identifier]: <message>``.
  2. ``invalid_event_types`` — declared ``event_types`` empty,
     malformed, duplicated, or with no matching sample event,
     rejected at pipeline stage 1.5. Rendered as
     ``CompileError [code=invalid_event_types]: <message>``.
  3. AST failure — pipeline stage 4. Falls through to
     :func:`build_ast_diagnostic`.
  4. ``load_error`` — in-process import (``isolation=False``) blew
     up on the candidate source at pipeline stage 5, *after* AST
     passed. Rendered as ``CompileError [code=load_error]:
     <message>``.
  5. Smoke failure — pipeline stage 5/6. Falls through to
     :func:`build_smoke_diagnostic`.

  Note that ``invalid_event_types`` can *also* fire after smoke as
  the post-coverage check (pipeline stage 7), and in that case the
  ``smoke_report`` will be attached and ``ok``. The
  ``invalid_event_types`` branch above wins, which is the right
  call: the LLM needs to fix the rule's declared event_type, not
  re-derive it from a passing smoke run.

  Earliest-stage ordering is a guarantee for ``CompileResult``
  values produced by :func:`compile_extractor` (which never sets
  more than one failure field at a time). For a hand-built
  ``CompileResult`` with multiple failure fields populated — a
  caller-error shape we don't expect in production — the order
  above is what wins; it matches canonical pipeline order so the
  diagnostic stays internally consistent.

  An ``ok`` result returns the passthrough message. The defensive
  fallback at the end handles a hypothetical "ok=False but no
  field populated" — a logic-bug shape we want labelled, not
  silently empty, in retry feedback.
  """
  if result.ok:
    return "Compile succeeded (no diagnostic to render)."

  if result.invalid_identifier is not None:
    return (
        f"CompileError [code=invalid_identifier]: {result.invalid_identifier}"
    )

  if result.invalid_event_types is not None:
    return (
        f"CompileError [code=invalid_event_types]: {result.invalid_event_types}"
    )

  # AST is checked before load_error so the diagnostic order matches
  # canonical pipeline order: AST gate (stage 4) runs before in-process
  # load (stage 5). compile_extractor never sets both at once, but a
  # hand-built CompileResult with both populated would otherwise get
  # the load_error message and contradict the "earliest stage" claim
  # above.
  if not result.ast_report.ok:
    return build_ast_diagnostic(result.ast_report)

  if result.load_error is not None:
    return f"CompileError [code=load_error]: {result.load_error}"

  if result.smoke_report is not None and not result.smoke_report.ok:
    return build_smoke_diagnostic(result.smoke_report)

  return (
      "CompileError [code=unknown]: compile failed but no diagnostic "
      "field was populated on the CompileResult"
  )


def build_gate_diagnostic(
    kind: str,
    payload: Union[PlanParseError, AstReport, SmokeTestReport, CompileResult],
) -> str:
  """Dispatch to the right per-gate diagnostic builder.

  ``kind`` is ``"parse"`` / ``"ast"`` / ``"smoke"`` / ``"compile"``.
  Used by the retry orchestrator in PR 4b.2.2.c.2 so the loop
  doesn't have to switch on type itself. ``"compile"`` is the
  general "the whole pipeline returned a CompileResult, render
  whatever failed" entry point — preferred over the per-gate kinds
  for the retry loop, which has the full ``CompileResult`` in hand
  and shouldn't re-derive which gate fired.
  """
  if kind == "parse":
    if not isinstance(payload, PlanParseError):
      raise TypeError(
          f"kind='parse' expects PlanParseError, got "
          f"{type(payload).__name__}"
      )
    return build_plan_parse_diagnostic(payload)
  if kind == "ast":
    if not isinstance(payload, AstReport):
      raise TypeError(
          f"kind='ast' expects AstReport, got {type(payload).__name__}"
      )
    return build_ast_diagnostic(payload)
  if kind == "smoke":
    if not isinstance(payload, SmokeTestReport):
      raise TypeError(
          f"kind='smoke' expects SmokeTestReport, got "
          f"{type(payload).__name__}"
      )
    return build_smoke_diagnostic(payload)
  if kind == "compile":
    if not isinstance(payload, CompileResult):
      raise TypeError(
          f"kind='compile' expects CompileResult, got "
          f"{type(payload).__name__}"
      )
    return build_compile_result_diagnostic(payload)
  raise ValueError(
      f"unknown gate kind {kind!r}; allowed: 'parse', 'ast', "
      f"'smoke', 'compile'"
  )


# ------------------------------------------------------------------ #
# Helpers                                                              #
# ------------------------------------------------------------------ #


def _pluralize(n: int) -> str:
  return "" if n == 1 else "s"


def _format_source_location(line, col) -> str:
  """``line N col M:`` / ``line N:`` / ``<no-line>:`` depending on
  what the AST node carried."""
  if line is None:
    return "<no-line>:"
  if col is None:
    return f"line {line}:"
  return f"line {line} col {col}:"


def _traceback_last_line(tb: str) -> str:
  """Reduce a multi-line traceback to its last non-empty line —
  typically the ``ExceptionType: message`` summary that's the
  actionable bit."""
  lines = [line.rstrip() for line in tb.splitlines() if line.strip()]
  return lines[-1] if lines else "<empty traceback>"


def _render_capped_section(*, heading: str, entries: list[str]) -> list[str]:
  """Render one section: heading + capped list + optional
  truncation note."""
  total = len(entries)
  shown = entries[:_MAX_LISTED_FAILURES]
  out: list[str] = [heading]
  for entry in shown:
    out.append(f"    - {entry}")
  if total > _MAX_LISTED_FAILURES:
    out.append(f"    ... and {total - _MAX_LISTED_FAILURES} more (truncated)")
  return out
