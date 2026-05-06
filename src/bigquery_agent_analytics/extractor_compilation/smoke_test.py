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

"""Smoke-test runner for compiled structured extractors.

Two responsibilities:

1. **Import a generated callable from disk.** Bundles are Python
   source files; the runtime loader (deferred to C2) and the
   compile harness both reach for the function via
   :func:`load_callable_from_source`. Loading from a real file path
   means tracebacks point at the generated source on disk — the
   natural debugging surface for compiled extractors.

2. **Execute the callable on sample events and gate on the #76
   validator plus result-shape checks.** The callable is invoked
   once per event under a ``BaseException`` catch so even
   ``SystemExit`` is captured rather than escaping. Wrong return
   types fail the gate. Empty-result-on-every-event fails the gate
   too — by default at least one event must produce output, so an
   extractor that vacuously returns ``StructuredExtractionResult()``
   for every input doesn't quietly pass.

PR 4b.1 keeps the runner ABI-only. C2 will plumb compiled callables
into the orchestrator's ``run_structured_extractors()`` hook; until
then the smoke-test runner is the only caller that imports a
generated module.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import pathlib
import sys
import traceback
from typing import Any, Callable, Optional

from bigquery_agent_analytics.extracted_models import ExtractedGraph
from bigquery_agent_analytics.graph_validation import validate_extracted_graph
from bigquery_agent_analytics.graph_validation import ValidationFailure
from bigquery_agent_analytics.structured_extraction import merge_extraction_results
from bigquery_agent_analytics.structured_extraction import StructuredExtractionResult


@dataclasses.dataclass(frozen=True)
class SmokeTestReport:
  """Result of one smoke-test run.

  ``ok`` is True iff:
    - every sample event produced a result without an exception
      (BaseException, including ``SystemExit``);
    - every result was a ``StructuredExtractionResult`` (no wrong
      return types);
    - at least ``min_nonempty_results`` events produced a non-empty
      result;
    - the merged graph validates clean against the resolved spec.

  Any one of those flips ``ok`` to False.
  """

  events_processed: int
  events_with_exception: int
  exceptions: tuple[str, ...]
  events_with_wrong_return_type: int
  wrong_return_types: tuple[str, ...]
  events_with_nonempty_result: int
  min_nonempty_results: int
  validation_failures: tuple[ValidationFailure, ...]

  @property
  def ok(self) -> bool:
    return (
        not self.exceptions
        and not self.wrong_return_types
        and self.events_with_nonempty_result >= self.min_nonempty_results
        and not self.validation_failures
    )


def load_callable_from_source(
    source_path: pathlib.Path,
    *,
    module_name: str,
    function_name: str,
) -> Callable:
  """Import *source_path* as a fresh module and return its named
  function.

  *module_name* must be unique per call (the harness uses the
  bundle fingerprint) so re-imports don't pick up a stale entry
  out of ``sys.modules``.
  """
  spec = importlib.util.spec_from_file_location(module_name, str(source_path))
  if spec is None or spec.loader is None:
    raise RuntimeError(
        f"could not load module spec for compiled bundle at {source_path}"
    )
  module = importlib.util.module_from_spec(spec)
  sys.modules[module_name] = module
  spec.loader.exec_module(module)

  fn = getattr(module, function_name, None)
  if fn is None:
    raise RuntimeError(
        f"compiled bundle {source_path.name!r} does not define a function "
        f"named {function_name!r}"
    )
  return fn


def run_smoke_test(
    extractor: Callable[[dict, Any], StructuredExtractionResult],
    *,
    events: list[dict],
    spec: Any,
    resolved_graph: Optional[Any] = None,
    min_nonempty_results: int = 1,
) -> SmokeTestReport:
  """Run *extractor* on every event in *events* and gate on the
  #76 validator + result-shape checks.

  Args:
    extractor: A callable matching the ``StructuredExtractor``
      signature ``(event: dict, spec: Any) -> StructuredExtractionResult``.
    events: Sample events to run against. Empty lists are rejected
      so a misconfigured smoke test can't pass vacuously. #75's
      compile harness expects ≥ 100 real events per covered
      ``event_type``; this runner only enforces the floor of 1 so
      it's reusable in tests.
    spec: Graph spec forwarded to *extractor* — the
      ``StructuredExtractor`` signature already accepts ``Any`` here.
    resolved_graph: ``ResolvedGraph`` to validate the merged result
      against. ``None`` skips the validator gate (useful for
      isolated tests of the runner itself).
    min_nonempty_results: Minimum number of events that must
      produce a non-empty ``StructuredExtractionResult``. Defaults
      to 1 so an extractor that returns empty for every event
      doesn't vacuously pass. Set to 0 only when the test is
      deliberately exercising the empty-result path.

  Per-event exceptions are captured via ``traceback.format_exc()``
  under a ``BaseException`` catch — even ``SystemExit`` and
  ``KeyboardInterrupt`` are surfaced in the report rather than
  escaping the runner.
  """
  if not events:
    raise ValueError(
        "smoke test requires at least one sample event; got an empty list"
    )
  if min_nonempty_results < 0:
    raise ValueError(
        f"min_nonempty_results must be >= 0; got "
        f"{min_nonempty_results!r}. Use 0 to opt out of the non-empty "
        f"floor; negative values would let the gate trivially pass."
    )

  exceptions: list[str] = []
  wrong_return_types: list[str] = []
  results: list[StructuredExtractionResult] = []
  events_with_nonempty_result = 0

  for event in events:
    try:
      result = extractor(event, spec)
    except BaseException:  # noqa: BLE001 — by design, surface in the report
      exceptions.append(traceback.format_exc())
      continue

    if not isinstance(result, StructuredExtractionResult):
      wrong_return_types.append(
          f"extractor returned {type(result).__name__!r}, expected "
          f"StructuredExtractionResult"
      )
      continue

    results.append(result)
    if (
        result.nodes
        or result.edges
        or result.fully_handled_span_ids
        or result.partially_handled_span_ids
    ):
      events_with_nonempty_result += 1

  merged = (
      merge_extraction_results(results)
      if results
      else StructuredExtractionResult()
  )
  graph = ExtractedGraph(
      name="smoke_test",
      nodes=list(merged.nodes),
      edges=list(merged.edges),
  )

  validation_failures: tuple[ValidationFailure, ...] = ()
  if resolved_graph is not None:
    report = validate_extracted_graph(resolved_graph, graph)
    validation_failures = report.failures

  return SmokeTestReport(
      events_processed=len(events),
      events_with_exception=len(exceptions),
      exceptions=tuple(exceptions),
      events_with_wrong_return_type=len(wrong_return_types),
      wrong_return_types=tuple(wrong_return_types),
      events_with_nonempty_result=events_with_nonempty_result,
      min_nonempty_results=min_nonempty_results,
      validation_failures=validation_failures,
  )
