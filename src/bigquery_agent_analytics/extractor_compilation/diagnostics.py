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

Three contracts the diagnostic text has to clear:

* **Actionable.** The LLM has to be able to act on the message
  without re-deriving where in its previous response the problem
  was. Per-failure entries surface the stable ``code`` from the
  underlying gate (``PlanParseError.code``, ``AstFailure.code``,
  ``ValidationFailure.code``) plus the dotted ``path`` or source
  line, so the LLM can grep its own output for the offending
  field / line.
* **Bounded.** Diagnostics get embedded in retry prompts; long
  tracebacks or hundreds of validator failures would crowd out the
  rule + schema grounding in the prompt itself. Each section is
  capped at the first ten failures with an ``... and N more
  (truncated)`` line; tracebacks are reduced to their last
  informative line (the exception type + message).
* **Deterministic.** Same input report → byte-identical output.
  Sorting where applicable; otherwise preserve input order, which
  itself is event-iteration order in the reports the gates produce.

These functions never raise; they always return a string. PR
4b.2.2.c.2 layers the retry-prompt builder + ``compile_with_llm``
orchestrator on top.
"""

from __future__ import annotations

from typing import Any, Union

from .ast_validator import AstReport
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


def build_gate_diagnostic(
    kind: str,
    payload: Union[PlanParseError, AstReport, SmokeTestReport],
) -> str:
  """Dispatch to the right per-gate diagnostic builder.

  ``kind`` is ``"parse"`` / ``"ast"`` / ``"smoke"``. Used by the
  retry orchestrator in PR 4b.2.2.c.2 so the loop doesn't have to
  switch on type itself.
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
  raise ValueError(
      f"unknown gate kind {kind!r}; allowed: 'parse', 'ast', 'smoke'"
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
