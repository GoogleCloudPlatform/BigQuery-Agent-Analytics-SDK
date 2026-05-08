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

"""Bundle loader + minimal runtime discovery for compiled
structured extractors (issue #75 Milestone C2.a).

This is the **trust boundary** before runtime execution. Per the
PR 4a runtime-target RFC
(``docs/extractor_compilation_runtime_target.md``), compiled
extractors run client-side as plain Python callables plugged into
the existing :func:`run_structured_extractors` hook. Before any
callable is plugged in, this module is what verifies the bundle
on disk matches the runtime's *active inputs* (fingerprint +
event_types) and that the imported callable has a usable shape.

Public surface:

* :func:`load_bundle` — single-bundle loader. Returns
  :class:`LoadedBundle` on success or :class:`LoadFailure` on
  any failure mode, with a stable failure ``code``. **Never
  raises.**
* :func:`discover_bundles` — directory walker. Loads every child
  bundle, applies an optional ``event_type_allowlist``, detects
  duplicate-coverage collisions and fails closed on them, and
  returns a :class:`DiscoveryResult` with the populated registry
  and an audit trail of failures.

Stable LoadFailure codes — callers can switch on them:

* ``manifest_missing`` — ``manifest.json`` doesn't exist.
* ``manifest_unreadable`` — JSON parse error or wrong-shape
  payload.
* ``fingerprint_mismatch`` — manifest's fingerprint differs from
  the caller's active inputs.
* ``event_types_mismatch`` — caller's ``expected_event_types``
  isn't a subset of the manifest's coverage.
* ``module_not_found`` — the module file referenced by the
  manifest is absent on disk.
* ``import_failed`` — importing the module raised.
* ``function_not_found`` — manifest's ``function_name`` isn't
  defined in the imported module.
* ``function_signature_mismatch`` — the imported callable can't
  be called as ``f(event, spec)`` (best-effort introspection).
* ``event_type_collision`` — discovery only: two valid bundles
  declare coverage of the same event_type. Fail-closed: that
  event_type is dropped from the registry; both colliding
  bundles get a failure record. Other event_types from those
  bundles still register.

The loader is deliberately conservative: when in doubt, surface
a failure rather than load. The runtime never gets a callable
unless every gate above passed.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import inspect
import pathlib
import sys
from typing import Any, Callable, Optional, Union
import uuid

from .manifest import Manifest


@dataclasses.dataclass(frozen=True)
class LoadedBundle:
  """Successfully-loaded bundle.

  ``extractor`` is the imported callable, ready to be called as
  ``extractor(event, spec)``. ``manifest`` is the parsed
  manifest exactly as stored on disk. ``bundle_dir`` is the
  directory the bundle was loaded from.
  """

  bundle_dir: pathlib.Path
  manifest: Manifest
  extractor: Callable[..., Any]


@dataclasses.dataclass(frozen=True)
class LoadFailure:
  """One failure record from :func:`load_bundle` or
  :func:`discover_bundles`.

  ``code`` is one of the stable identifiers documented in the
  module docstring. ``detail`` is a human-readable message;
  callers can show it directly to operators or aggregate
  ``code`` for telemetry. ``bundle_dir`` is the bundle the
  failure pertains to (or one of the bundles, in the
  collision case).
  """

  bundle_dir: pathlib.Path
  code: str
  detail: str


@dataclasses.dataclass(frozen=True)
class DiscoveryResult:
  """Outcome of one :func:`discover_bundles` walk.

  * ``registry`` — the ``event_type → callable`` map ready to
    feed into ``run_structured_extractors``. Only event_types
    with exactly one valid covering bundle land here.
  * ``loaded`` — every bundle that parsed + imported + validated.
    A bundle in ``loaded`` may still have *some* of its declared
    event_types missing from ``registry`` (they collided with
    another bundle) — both views are kept distinct so logging
    can distinguish "bundle didn't load" from "bundle's
    event_type wasn't unique."
  * ``failures`` — every per-bundle failure (parse / import /
    fingerprint / signature) plus per-event-type collision
    failures. Stable codes are documented at module scope.
  """

  registry: dict[str, Callable[..., Any]]
  loaded: tuple[LoadedBundle, ...]
  failures: tuple[LoadFailure, ...]


def load_bundle(
    bundle_dir: pathlib.Path,
    *,
    expected_fingerprint: str,
    expected_event_types: Optional[tuple[str, ...]] = None,
) -> Union[LoadedBundle, LoadFailure]:
  """Load *bundle_dir* and verify it matches the caller's active
  inputs.

  Validation order — each gate short-circuits:

  1. ``manifest.json`` exists.
  2. ``manifest.json`` parses + has the right shape.
  3. Manifest fingerprint equals ``expected_fingerprint``.
  4. ``expected_event_types`` (when not ``None``) is a subset of
     the manifest's ``event_types`` — i.e., the bundle covers
     every event_type the caller asked for. The bundle is
     allowed to cover more.
  5. The module file referenced by the manifest exists on disk.
  6. Importing the module succeeds (no exception).
  7. The manifest's ``function_name`` is defined in the imported
     module.
  8. The imported callable accepts ``(event, spec)``.

  Args:
    bundle_dir: Directory containing ``manifest.json`` and the
      module file the manifest references. The path is treated
      as opaque — no recursion, no globbing.
    expected_fingerprint: The fingerprint the caller has computed
      from the active ``(ontology, binding, event_schema, …)``
      inputs. The loader refuses any bundle whose manifest
      doesn't match.
    expected_event_types: Optional subset check. ``None`` skips
      the check; a tuple requires every entry to appear in
      ``manifest.event_types``. Used by single-bundle callers
      that want to verify the bundle covers their target event;
      :func:`discover_bundles` passes ``None`` here and applies
      its own allowlist downstream.

  Returns:
    :class:`LoadedBundle` on success; :class:`LoadFailure` with a
    stable ``code`` on any failure. Never raises.
  """
  manifest_path = bundle_dir / "manifest.json"
  if not manifest_path.is_file():
    return LoadFailure(
        bundle_dir=bundle_dir,
        code="manifest_missing",
        detail=f"manifest.json not found at {manifest_path}",
    )

  try:
    manifest_text = manifest_path.read_text(encoding="utf-8")
    manifest = Manifest.from_json(manifest_text)
  # ``Manifest.from_json`` can raise on JSON parse errors,
  # missing/extra fields, wrong types, etc. We don't care which
  # — the artifact is unreadable as a Manifest. ``OSError`` covers
  # filesystem errors that slipped past ``is_file()`` (race, etc.).
  except (OSError, Exception) as e:  # noqa: BLE001 — surface in record
    return LoadFailure(
        bundle_dir=bundle_dir,
        code="manifest_unreadable",
        detail=f"{type(e).__name__}: {e}",
    )

  if manifest.fingerprint != expected_fingerprint:
    return LoadFailure(
        bundle_dir=bundle_dir,
        code="fingerprint_mismatch",
        detail=(
            f"manifest fingerprint {manifest.fingerprint!r} != "
            f"expected {expected_fingerprint!r}"
        ),
    )

  if expected_event_types is not None:
    missing = [
        et for et in expected_event_types if et not in manifest.event_types
    ]
    if missing:
      return LoadFailure(
          bundle_dir=bundle_dir,
          code="event_types_mismatch",
          detail=(
              f"manifest event_types {list(manifest.event_types)!r} "
              f"don't cover expected {list(expected_event_types)!r} "
              f"(missing: {missing!r})"
          ),
      )

  module_path = bundle_dir / manifest.module_filename
  if not module_path.is_file():
    return LoadFailure(
        bundle_dir=bundle_dir,
        code="module_not_found",
        detail=f"module file {manifest.module_filename!r} not found at {module_path}",
    )

  try:
    module = _import_module_from_path(
        module_path, module_stem=manifest.module_filename[:-3]
    )
  except BaseException as e:  # noqa: BLE001 — surface in record
    # Catch BaseException (not just Exception) so SystemExit /
    # KeyboardInterrupt raised by malicious or buggy bundle code
    # at import time is captured as a failure rather than
    # tearing down the runtime that's loading the bundle.
    return LoadFailure(
        bundle_dir=bundle_dir,
        code="import_failed",
        detail=f"{type(e).__name__}: {e}",
    )

  extractor = getattr(module, manifest.function_name, None)
  if extractor is None or not callable(extractor):
    return LoadFailure(
        bundle_dir=bundle_dir,
        code="function_not_found",
        detail=(
            f"function_name {manifest.function_name!r} not "
            f"defined as callable in module "
            f"{manifest.module_filename!r}"
        ),
    )

  signature_problem = _signature_compatible(extractor)
  if signature_problem is not None:
    return LoadFailure(
        bundle_dir=bundle_dir,
        code="function_signature_mismatch",
        detail=signature_problem,
    )

  return LoadedBundle(
      bundle_dir=bundle_dir,
      manifest=manifest,
      extractor=extractor,
  )


def discover_bundles(
    parent_dir: pathlib.Path,
    *,
    expected_fingerprint: str,
    event_type_allowlist: Optional[tuple[str, ...]] = None,
) -> DiscoveryResult:
  """Walk *parent_dir*, load every child bundle, build the
  ``event_type → callable`` registry.

  Per-child semantics:

  * Each subdirectory of ``parent_dir`` is treated as a candidate
    bundle. Files / non-bundle directories are silently ignored
    (the caller's bundle root may legitimately contain other
    artifacts). A child that doesn't contain a ``manifest.json``
    fails with ``manifest_missing`` rather than being silently
    skipped — discovery is opt-in (passing the directory) but
    every candidate child is accounted for.
  * Each child goes through :func:`load_bundle` with
    ``expected_event_types=None``; per-bundle event-type checks
    don't apply at discovery time. The allowlist filter happens
    after load.

  Per-event-type semantics:

  * After successful load, each declared ``event_type`` in the
    manifest is a *candidate*. If ``event_type_allowlist`` is
    set, only event_types in the allowlist remain candidates;
    others are silently dropped (the bundle's coverage is
    broader than the runtime cares about — that's not a
    failure).
  * If exactly one bundle covers a given event_type, the
    callable registers under that key.
  * If two or more bundles cover the same event_type, **all**
    bundles claiming it produce an
    ``event_type_collision`` failure record and the event_type
    is dropped from the registry. Other event_types from those
    same bundles still register if they're unique. **Fail
    closed** — silently picking one bundle would mean the
    runtime's behavior depended on filesystem ordering.

  Args:
    parent_dir: Directory containing zero or more bundle
      subdirectories.
    expected_fingerprint: Forwarded to :func:`load_bundle` for
      every child.
    event_type_allowlist: Optional. ``None`` → register every
      declared event_type. A tuple → only register event_types
      that appear in the tuple. An empty tuple → register
      nothing (degenerate case; valid).

  Returns:
    A :class:`DiscoveryResult`. Never raises.
  """
  failures: list[LoadFailure] = []
  loaded: list[LoadedBundle] = []

  if not parent_dir.is_dir():
    return DiscoveryResult(
        registry={},
        loaded=(),
        failures=(
            LoadFailure(
                bundle_dir=parent_dir,
                code="manifest_missing",
                detail=(
                    f"parent directory {parent_dir} does not exist or is "
                    f"not a directory; no bundles to discover"
                ),
            ),
        ),
    )

  for child in sorted(parent_dir.iterdir()):
    if not child.is_dir():
      continue
    result = load_bundle(
        child,
        expected_fingerprint=expected_fingerprint,
        expected_event_types=None,
    )
    if isinstance(result, LoadFailure):
      failures.append(result)
    else:
      loaded.append(result)

  # Map event_type → list of bundles claiming it (after allowlist).
  candidates: dict[str, list[LoadedBundle]] = {}
  for bundle in loaded:
    for event_type in bundle.manifest.event_types:
      if (
          event_type_allowlist is not None
          and event_type not in event_type_allowlist
      ):
        continue
      candidates.setdefault(event_type, []).append(bundle)

  registry: dict[str, Callable[..., Any]] = {}
  for event_type, bundles in candidates.items():
    if len(bundles) == 1:
      registry[event_type] = bundles[0].extractor
      continue
    bundle_paths = ", ".join(repr(str(b.bundle_dir)) for b in bundles)
    detail = (
        f"event_type {event_type!r} declared by {len(bundles)} bundles: "
        f"{bundle_paths}; failing closed (registering none)"
    )
    for bundle in bundles:
      failures.append(
          LoadFailure(
              bundle_dir=bundle.bundle_dir,
              code="event_type_collision",
              detail=detail,
          )
      )

  return DiscoveryResult(
      registry=registry,
      loaded=tuple(loaded),
      failures=tuple(failures),
  )


# ------------------------------------------------------------------ #
# Helpers                                                              #
# ------------------------------------------------------------------ #


def _import_module_from_path(
    module_path: pathlib.Path,
    *,
    module_stem: str,
) -> Any:
  """Import the module at *module_path* under a per-call unique
  ``sys.modules`` name.

  Each call uses a ``module_stem + uuid`` name so reloading the
  same bundle (e.g., across two ``load_bundle`` calls in the
  same process) doesn't recycle a stale ``sys.modules`` entry.
  """
  unique_name = f"{module_stem}__loaded_{uuid.uuid4().hex[:12]}"
  spec = importlib.util.spec_from_file_location(unique_name, module_path)
  if spec is None or spec.loader is None:
    raise ImportError(f"could not build spec for {module_path}")
  module = importlib.util.module_from_spec(spec)
  sys.modules[unique_name] = module
  try:
    spec.loader.exec_module(module)
  except BaseException:
    # If exec_module raised, the partial module object in
    # sys.modules is unsafe for any future import. Remove it so
    # the failure is fully contained.
    sys.modules.pop(unique_name, None)
    raise
  return module


def _signature_compatible(extractor: Callable[..., Any]) -> Optional[str]:
  """Return ``None`` if *extractor* can be called as
  ``extractor(event, spec)``; otherwise a short detail string
  naming the problem.

  Implementation: ask ``inspect.signature`` for the parameters,
  then ``sig.bind(None, None)``. ``bind`` raises ``TypeError``
  iff the call would fail at runtime — handles missing required
  args, no positional slots, conflicting kwargs-only signatures,
  and the like in one shot.

  Best-effort: when ``inspect.signature`` itself raises (some
  C-implemented callables / builtins), we don't block the load.
  The caller's first runtime invocation will surface the real
  problem if there is one.
  """
  try:
    sig = inspect.signature(extractor)
  except (TypeError, ValueError):
    return None
  try:
    sig.bind(None, None)
  except TypeError as e:
    return f"callable does not accept (event, spec) as positional args: {e}"
  return None
