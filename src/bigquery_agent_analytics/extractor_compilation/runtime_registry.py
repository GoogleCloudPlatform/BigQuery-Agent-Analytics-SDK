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

"""Runtime extractor-registry adapter (issue #75 Milestone C2.c.1).

Glues the C2.a bundle loader (:func:`discover_bundles`) and the
C2.b runtime fallback wrapper (:func:`run_with_fallback`)
together so callers can construct an ``event_type → extractor``
registry suitable for the existing
:func:`run_structured_extractors` hook in one call. **This PR
ships the adapter, not the orchestrator call-site swap** —
deciding which orchestrator paths adopt the registry is a
separate scope (C2.c.2).

Per-event-type wiring rules:

* **Compiled bundle present + handwritten fallback present** →
  the registry entry is a closure that calls
  :func:`run_with_fallback(compiled, fallback)` and (optionally)
  invokes a caller-supplied ``on_outcome`` callback for every
  event before returning the result.
* **Fallback present, no compiled bundle** → the original
  fallback callable is registered unchanged. No safety-net
  wrapping (the fallback IS the baseline; there's nothing to
  validate against itself).
* **Compiled bundle present, no fallback** → **skipped** and
  recorded in ``bundles_without_fallback``. C2.b's safety
  contract requires a fallback; without one, the compiled
  extractor would run without the validator-driven safety net,
  which inverts the C2 guarantees. Fail-closed default.
* **Neither present** → not registered (trivially).

The :class:`WrappedRegistry` return value carries both the
ready-to-use ``extractors`` dict and audit fields callers can
use to build coverage telemetry. ``bundles_without_fallback``
is the strict configuration-error signal (compiled bundle
discovered, no matching fallback). ``fallbacks_without_bundle``
is the wider "no usable compiled registry entry" signal — it
includes "bundle never built" *and* "bundle exists but
discovery rejected it" (fingerprint mismatch,
``manifest_unreadable``, event-type collision, etc.). Rollout
telemetry that wants to distinguish those cases should cross-
reference ``discovery.failures`` for the underlying reason.

The ``on_outcome`` callback fires on **every** wrapped
invocation including ``compiled_unchanged`` outcomes — that's
the only way callers can compute denominator metrics
(compiled-unchanged rate, filtered rate, fallback rate,
exception rate). Callbacks that raise propagate to the caller;
the adapter does *not* swallow them, since telemetry callbacks
should be correct and silently ignoring instrumentation bugs is
exactly the kind of thing C2's audit trail exists to prevent.

Out of scope (deferred):

* **The actual orchestrator call-site swap.** Where in
  ``ontology_graph.py`` / the runtime does the registry
  produced here actually replace direct extractor calls? C2.c.2.
* **BigQuery-table bundle mirror** for cross-process
  distribution. C2.c.3.
* **Revalidation harness** (scheduled / on-demand agreement
  check between compiled and reference outputs). C2.d.
* **``AI.GENERATE``-backed fallback adapter** that fits the
  ``StructuredExtractor`` signature. The registry wires
  arbitrary fallback callables; constructing an
  ``AI.GENERATE``-backed one is the orchestrator integration's
  concern.
"""

from __future__ import annotations

import dataclasses
import pathlib
from typing import Any, Callable, Optional

from ..structured_extraction import StructuredExtractionResult
from .bundle_loader import _signature_compatible
from .bundle_loader import discover_bundles
from .bundle_loader import DiscoveryResult
from .runtime_fallback import FallbackOutcome
from .runtime_fallback import run_with_fallback

# Type alias for the per-event audit callback. Callers write
# their telemetry / logging / metric-emission code against this
# shape and pass it as ``on_outcome``.
OutcomeCallback = Callable[[str, FallbackOutcome], None]
"""``(event_type, outcome) -> None``. Invoked once per event
from a wrapped extractor — *after* :func:`run_with_fallback`
produces the outcome but *before* the wrapped extractor returns
``outcome.result`` to the runtime. Exceptions propagate."""


@dataclasses.dataclass(frozen=True)
class WrappedRegistry:
  """Result of :func:`build_runtime_extractor_registry`.

  Fields:

  * ``extractors`` — the ``event_type → extractor`` dict, ready
    to pass into :func:`run_structured_extractors` as the
    ``extractors`` argument. Each entry is either the original
    fallback callable (when no compiled bundle covers the
    event_type) or a wrapped closure that invokes
    :func:`run_with_fallback` under the hood.
  * ``discovery`` — the full :class:`DiscoveryResult` from C2.a's
    :func:`discover_bundles`. Includes loaded bundles and any
    per-bundle / collision failures so callers can route on
    them.
  * ``bundles_without_fallback`` — event_types for which a
    compiled bundle was discovered (and matched the
    ``event_type_allowlist``, when set) but no matching
    fallback was registered. The bundle was *not* registered;
    the runtime won't invoke it. C2's safety contract requires
    a fallback for every wrapped extractor; this audit field
    surfaces configuration gaps so they don't fail silently.
  * ``fallbacks_without_bundle`` — event_types for which a
    fallback was registered (and matched the allowlist) but no
    *usable* compiled registry entry was produced by discovery.
    The fallback IS registered unchanged. The "no usable
    entry" set is wider than "no bundle on disk": a bundle
    can exist but be excluded by fingerprint mismatch,
    ``manifest_unreadable``, or event-type collision — those
    cases all surface here too, with the underlying reason in
    ``discovery.failures``. Rollout telemetry that wants to
    distinguish "bundle never built" from "bundle exists but
    rejected" should cross-reference ``discovery.failures``
    rather than treat ``fallbacks_without_bundle`` as a pure
    "no coverage yet" signal.

  Sorted tuples for deterministic audit output.
  """

  extractors: dict[str, Callable[..., StructuredExtractionResult]]
  discovery: DiscoveryResult
  bundles_without_fallback: tuple[str, ...]
  fallbacks_without_bundle: tuple[str, ...]


def build_runtime_extractor_registry(
    *,
    bundles_root: pathlib.Path,
    expected_fingerprint: str,
    fallback_extractors: dict[str, Callable[..., StructuredExtractionResult]],
    resolved_graph: Any,
    event_type_allowlist: Optional[tuple[str, ...]] = None,
    on_outcome: Optional[OutcomeCallback] = None,
) -> WrappedRegistry:
  """Build an ``event_type → extractor`` registry from compiled
  bundles + handwritten fallbacks.

  Args:
    bundles_root: Directory passed to
      :func:`discover_bundles`. Each subdirectory is a candidate
      bundle.
    expected_fingerprint: The fingerprint the runtime computed
      from its active inputs. Bundles whose manifest fingerprint
      doesn't match are skipped at discovery time (per C2.a).
    fallback_extractors: ``event_type → handwritten callable``.
      Required: C2.b's safety contract uses the fallback as the
      authoritative baseline against which the compiled output
      is compared. Event types absent here can't be wrapped.
    resolved_graph: The :class:`ResolvedGraph` the validator
      compares compiled output against, forwarded to
      :func:`run_with_fallback` per event.
    event_type_allowlist: Optional. ``None`` means "consider
      every event_type"; a tuple restricts the registry to
      those event_types. The allowlist filters both compiled-
      bundle discovery (via :func:`discover_bundles`) AND
      fallback registration — so an event_type outside the
      allowlist is silently dropped from both candidate pools
      and never appears in any audit field.
    on_outcome: Optional. ``(event_type, outcome) -> None``,
      invoked from inside each wrapped extractor on **every**
      event including ``compiled_unchanged`` outcomes — that's
      the denominator metric for compiled-vs-fallback rate
      analysis. Exceptions raised by the callback propagate.

  Returns:
    A :class:`WrappedRegistry`. The ``extractors`` dict is
    ready to be passed straight into
    :func:`run_structured_extractors`.

  Raises:
    TypeError: if any ``fallback_extractors`` entry is
      structurally invalid. Each of the following is rejected
      with a message naming the offending key:

      * key isn't a ``str`` (the runtime keys event_type
        lookups by string; non-str keys silently never match,
        and mixed key types crash audit-tuple sorting);
      * key is the empty string ``""`` (registers an entry no
        real event can match);
      * value isn't callable (``None`` would be silently
        skipped by ``run_structured_extractors``; other
        non-callables would raise far from the misconfig site);
      * callable signature can't accept ``(event, spec)``
        (verified via the same ``_signature_compatible`` check
        the bundle loader applies to compiled extractors, so
        the ``StructuredExtractor`` contract has one source of
        truth at every trust boundary).

      The dict is validated *before* allowlist scoping, so
      misconfigured entries surface even when they'd be
      filtered out — the caller's input contract has to hold
      for the whole dict, not just the scoped subset.
  """
  # Validate the full input dict before any other work. Bad
  # entries surface here, with the offending key named, rather
  # than silently producing a registry the runtime will skip
  # (None-valued), crash on (non-callable invoked), can't sort
  # for audit (mixed-type keys), or call with the wrong number
  # of args (one-arg lambda).
  for event_type, fallback in fallback_extractors.items():
    if not isinstance(event_type, str):
      raise TypeError(
          f"fallback_extractors keys must be strings (event_type "
          f"names); got {type(event_type).__name__}={event_type!r}"
      )
    if not event_type:
      raise TypeError("fallback_extractors contains an empty-string key")
    if not callable(fallback):
      raise TypeError(
          f"fallback_extractors[{event_type!r}] must be callable; "
          f"got {type(fallback).__name__}={fallback!r}"
      )
    sig_problem = _signature_compatible(fallback)
    if sig_problem is not None:
      raise TypeError(f"fallback_extractors[{event_type!r}] {sig_problem}")

  discovery = discover_bundles(
      bundles_root,
      expected_fingerprint=expected_fingerprint,
      event_type_allowlist=event_type_allowlist,
  )

  if event_type_allowlist is None:
    scoped_fallbacks = dict(fallback_extractors)
  else:
    allow = set(event_type_allowlist)
    scoped_fallbacks = {
        et: callable_
        for et, callable_ in fallback_extractors.items()
        if et in allow
    }

  compiled_event_types = set(discovery.registry.keys())
  fallback_event_types = set(scoped_fallbacks.keys())

  extractors: dict[str, Callable[..., StructuredExtractionResult]] = {}
  for event_type, fallback in scoped_fallbacks.items():
    compiled = discovery.registry.get(event_type)
    if compiled is None:
      # No compiled coverage for this event_type — pass the
      # fallback through unchanged. No wrapper, no callback;
      # the runtime hasn't gained a compiled extractor here yet.
      extractors[event_type] = fallback
    else:
      extractors[event_type] = _make_wrapped_extractor(
          event_type=event_type,
          compiled=compiled,
          fallback=fallback,
          resolved_graph=resolved_graph,
          on_outcome=on_outcome,
      )

  bundles_without_fallback = tuple(
      sorted(compiled_event_types - fallback_event_types)
  )
  fallbacks_without_bundle = tuple(
      sorted(fallback_event_types - compiled_event_types)
  )

  return WrappedRegistry(
      extractors=extractors,
      discovery=discovery,
      bundles_without_fallback=bundles_without_fallback,
      fallbacks_without_bundle=fallbacks_without_bundle,
  )


# ------------------------------------------------------------------ #
# Helpers                                                              #
# ------------------------------------------------------------------ #


def _make_wrapped_extractor(
    *,
    event_type: str,
    compiled: Callable[..., StructuredExtractionResult],
    fallback: Callable[..., StructuredExtractionResult],
    resolved_graph: Any,
    on_outcome: Optional[OutcomeCallback],
) -> Callable[..., StructuredExtractionResult]:
  """Build the per-event wrapper that calls
  :func:`run_with_fallback` and (optionally) the audit callback.

  The closure captures ``event_type`` so the callback can
  attribute outcomes correctly. ``resolved_graph`` is captured
  by reference — the wrapper uses whatever the caller passed at
  registry-build time on every invocation; if the spec evolves
  at runtime, the registry needs to be rebuilt.
  """

  def wrapped(event: dict, spec: Any) -> StructuredExtractionResult:
    outcome = run_with_fallback(
        event=event,
        spec=spec,
        resolved_graph=resolved_graph,
        compiled_extractor=compiled,
        fallback_extractor=fallback,
    )
    if on_outcome is not None:
      # Callback exceptions propagate. Telemetry callbacks
      # should be correct; silently swallowing here would hide
      # instrumentation bugs and defeat the purpose of the
      # audit channel. A future caller that wants non-blocking
      # telemetry can layer their own try/except inside the
      # callback.
      on_outcome(event_type, outcome)
    return outcome.result

  return wrapped
