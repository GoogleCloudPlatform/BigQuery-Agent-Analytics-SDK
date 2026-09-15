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
Reads pickled args from stdin, imports the trusted SDK harness,
optionally applies a virtual-memory ``setrlimit`` (POSIX-only;
best-effort elsewhere) as headroom above that import baseline, runs
the candidate extractor against the supplied events, and returns
per-event outcomes via pickled stdout.

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

import os
import pathlib
import pickle
import sys
import traceback


def _current_address_space_bytes() -> int | None:
  """Current virtual address-space size of this process, or ``None``
  when it can't be read. Linux-only via ``/proc/self/statm`` — the
  platform where ``RLIMIT_AS`` is actually enforced."""
  try:
    with open("/proc/self/statm", "rb") as f:
      pages = int(f.read().split()[0])
    return pages * os.sysconf("SC_PAGE_SIZE")
  except (OSError, ValueError, IndexError, AttributeError):
    return None


def _set_memory_limit(memory_limit_mb: int | None) -> None:
  """Best-effort virtual-memory cap. POSIX-only; quietly noop on
  Windows or if ``setrlimit`` rejects the value (e.g., on platforms
  where ``RLIMIT_AS`` isn't honored).

  The cap is *memory_limit_mb* of headroom above the current address
  space, not an absolute ceiling. ``main`` calls this after importing
  the trusted SDK harness, whose footprint depends on which optional
  dependencies are installed (``google-cloud-bigquery`` pulls in
  pandas / pyarrow / numpy when present, each reserving native
  address space). An absolute cap let that baseline alone exhaust
  the budget and fail the gate with ``MemoryError`` before the
  candidate extractor loaded. Falls back to an absolute cap when the
  baseline can't be read.
  """
  if not memory_limit_mb:
    return
  try:
    import resource  # POSIX-only; ImportError on Windows
  except ImportError:
    return
  bytes_limit = memory_limit_mb * 1024 * 1024
  baseline = _current_address_space_bytes()
  if baseline is not None:
    bytes_limit += baseline
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

    # Trusted harness imports happen before the cap so the budget
    # measures the candidate extractor, not the SDK's import footprint.
    from bigquery_agent_analytics.extractor_compilation.smoke_test import load_callable_from_source
    from bigquery_agent_analytics.structured_extraction import StructuredExtractionResult

    _set_memory_limit(args.get("memory_limit_mb"))

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
