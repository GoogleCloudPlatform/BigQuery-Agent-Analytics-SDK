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

"""Subprocess entry point for isolated compiled-extractor smoke tests.

Invoked as ``python -m
bigquery_agent_analytics.extractor_compilation.subprocess_runner``.
Reads pickled args from stdin, optionally applies a virtual-memory
``setrlimit`` (POSIX-only; best-effort elsewhere), runs the candidate
extractor against the supplied events, and returns per-event
outcomes via pickled stdout.

The parent (``smoke_test.run_smoke_test_in_subprocess``) is
responsible for the wallclock timeout via ``subprocess.run(timeout=)``,
plus #76 validation of the merged graph — both stay in the parent
so the ``ResolvedGraph`` doesn't have to cross the process
boundary.

Static AST checks (``validate_source``) close many hazards but
can't cover every allocation / hang shape in untrusted source. A
subprocess with a wallclock cap is the runtime safety net for the
smoke gate; this module is the child half.
"""

from __future__ import annotations

import pathlib
import pickle
import sys
import traceback


def _set_memory_limit(memory_limit_mb: int | None) -> None:
  """Best-effort virtual-memory cap. POSIX-only; quietly noop on
  Windows or if ``setrlimit`` rejects the value (e.g., on platforms
  where ``RLIMIT_AS`` isn't honored)."""
  if not memory_limit_mb:
    return
  try:
    import resource  # POSIX-only; ImportError on Windows
  except ImportError:
    return
  bytes_limit = memory_limit_mb * 1024 * 1024
  try:
    resource.setrlimit(resource.RLIMIT_AS, (bytes_limit, bytes_limit))
  except (ValueError, OSError):
    return


def main() -> int:
  """Read args from stdin, run per-event extraction, write outcomes.

  Stdout is the pickled tuple ``("ok", per_event)`` on success or
  ``("harness_error", type_name, msg, traceback_str)`` on
  unexpected failure. The parent's ``run_smoke_test_in_subprocess``
  knows both shapes.
  """
  try:
    args = pickle.loads(sys.stdin.buffer.read())
    _set_memory_limit(args.get("memory_limit_mb"))

    from bigquery_agent_analytics.extractor_compilation.smoke_test import load_callable_from_source
    from bigquery_agent_analytics.structured_extraction import StructuredExtractionResult

    extractor = load_callable_from_source(
        pathlib.Path(args["source_path"]),
        module_name=args["module_name"],
        function_name=args["function_name"],
    )

    per_event: list[tuple] = []
    for event in args["events"]:
      try:
        result = extractor(event, args["spec"])
      except BaseException:  # noqa: BLE001 — surface in the report
        per_event.append(("exception", traceback.format_exc()))
        continue
      if not isinstance(result, StructuredExtractionResult):
        per_event.append(("wrong_type", type(result).__name__))
        continue
      per_event.append(("result", result))

    sys.stdout.buffer.write(pickle.dumps(("ok", per_event)))
    return 0
  except BaseException as e:  # noqa: BLE001 — surface to parent
    try:
      sys.stdout.buffer.write(
          pickle.dumps(
              (
                  "harness_error",
                  type(e).__name__,
                  str(e),
                  traceback.format_exc(),
              )
          )
      )
    except BaseException:  # noqa: BLE001 — last-resort
      pass
    return 1


if __name__ == "__main__":
  sys.exit(main())
