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

"""Top-level compile pipeline for compiled structured extractors.

Stages, executed in order, with any failure short-circuiting and
leaving any pre-existing valid bundle untouched:

  1. **Identifier safety.** ``module_name`` / ``function_name``
     must be plain Python identifiers (path-traversal safety) and
     not Python keywords.
  2. **Declared ``event_types`` validation.** Non-empty tuple of
     non-empty unique strings; sample events are dicts carrying a
     non-empty string ``event_type``; every declared type has at
     least one matching sample event. The manifest's
     ``event_types`` is a public C2 contract, so these rules are
     enforced before any expensive work.
  3. Compute the #75 fingerprint over compile inputs.
  4. **Cache hit candidate:** if ``<bundle_dir>/manifest.json``
     already exists and matches the current request (fingerprint
     + function_name + module_filename + event_types + on-disk
     source bytes), import the cached callable and re-run the
     smoke-test runner against the *current* sample events and
     resolved spec. If it passes, return the cached manifest
     without rewriting anything. If smoke fails on the current
     inputs, surface the failure — the on-disk bundle isn't
     rewritten (same source, only the test inputs are stricter).
  5. AST-validate the candidate source.
  6. **Stage** in a sibling temp directory under
     ``parent_bundle_dir``: write source, run the smoke-test
     runner (subprocess by default; ``isolation=False`` for the
     in-process path) with the #76 validator gate.
  7. **Non-empty coverage gate.** Every declared event_type must
     appear in ``smoke_report.nonempty_event_types`` — i.e., at
     least one sample of that type produced non-empty output. A
     manifest can't claim coverage for ``("x",)`` while only
     ``"y"`` samples did the work.
  8. Write the manifest.
  9. **Staged replace** the (possibly pre-existing) bundle
     directory with the staged one (``rmtree`` then ``rename``).
     Not strictly atomic — a process crash between the two
     filesystem ops would leave the target absent — but the
     bundle is reproducible from inputs, so the next compile
     re-creates it. Failed gates leave the pre-existing bundle
     untouched.

The bundle directory is named after the fingerprint. Two compile
runs on identical inputs land in the same directory; the second
run is a cache hit (stage 3) and writes nothing, so the on-disk
bundle is byte-identical to the first run's output.

Per the PR 4a runtime-target RFC, this module owns the *local*
bundle layout only. Runtime discovery, BQ-table mirror, and the
in-repo / sidecar-table choice are deferred to C2.
"""

from __future__ import annotations

import dataclasses
import json
import keyword
import pathlib
import shutil
import tempfile
from typing import Any, Optional

from .ast_validator import AstReport
from .ast_validator import validate_source
from .fingerprint import compute_fingerprint
from .manifest import Manifest
from .manifest import now_iso_utc
from .smoke_test import load_callable_from_source
from .smoke_test import run_smoke_test
from .smoke_test import run_smoke_test_in_subprocess
from .smoke_test import SmokeTestReport


@dataclasses.dataclass(frozen=True)
class CompileResult:
  """Outcome of one :func:`compile_extractor` run.

  ``ok`` is True iff the bundle is on disk and valid — either
  because every gate passed (fresh compile) or because a previous
  successful compile produced a matching bundle (``cache_hit``).
  Callers must check ``ok`` before assuming ``bundle_dir`` is
  loadable.
  """

  manifest: Optional[Manifest]
  ast_report: AstReport
  smoke_report: Optional[SmokeTestReport]
  bundle_dir: Optional[pathlib.Path]
  load_error: Optional[str] = None
  cache_hit: bool = False
  invalid_identifier: Optional[str] = None
  invalid_event_types: Optional[str] = None

  @property
  def ok(self) -> bool:
    if self.invalid_identifier is not None:
      return False
    if self.invalid_event_types is not None:
      return False
    if self.cache_hit:
      return self.manifest is not None and self.bundle_dir is not None
    return (
        self.ast_report.ok
        and self.smoke_report is not None
        and self.smoke_report.ok
        and self.manifest is not None
        and self.bundle_dir is not None
        and self.load_error is None
    )


def default_bundle_dir(parent: pathlib.Path, fingerprint: str) -> pathlib.Path:
  """Local bundle layout: ``<parent>/<fingerprint>/``.

  Single source of truth for where the harness writes a bundle.
  Runtime discovery (where C2's loader looks for bundles) and any
  remote-mirror layout are deferred per the PR 4a RFC.
  """
  return parent / fingerprint


def compile_extractor(
    *,
    source: str,
    module_name: str,
    function_name: str,
    event_types: tuple[str, ...],
    sample_events: list[dict],
    spec: Any,
    resolved_graph: Any,
    parent_bundle_dir: pathlib.Path,
    fingerprint_inputs: dict,
    template_version: str,
    compiler_package_version: str,
    min_nonempty_results: int = 1,
    isolation: bool = True,
    smoke_timeout_seconds: float = 30.0,
    smoke_memory_limit_mb: Optional[int] = 512,
) -> CompileResult:
  """Run *source* through every gate and write a bundle on success.

  Args:
    source: Hand-authored Python source for the extractor function
      (4b.1) — LLM-driven fill is 4b.2's responsibility, not this
      module's. Source must define a function called *function_name*
      matching the ``StructuredExtractor`` signature.
    module_name: Stable name used for the imported module and as
      the file's stem on disk. Must be a plain Python identifier
      (``str.isidentifier``); ``../x``, ``foo.bar``, and other
      path-traversal-shaped strings are rejected up front.
    function_name: Name of the extractor function inside *source*.
      Same identifier validation as *module_name*.
    event_types: ``event_type`` values this bundle covers. Recorded
      in the manifest.
    sample_events: Events the smoke-test runner will execute the
      compiled callable against. ``run_smoke_test`` requires at
      least one; #75 expects ≥ 100 in production.
    spec: Graph spec forwarded to the extractor (forwarded directly
      to ``run_smoke_test``).
    resolved_graph: ``ResolvedGraph`` the smoke-test merged output
      is validated against via the #76 validator.
    parent_bundle_dir: Directory under which the fingerprint-named
      bundle directory is created.
    fingerprint_inputs: ``ontology_text`` / ``binding_text`` /
      ``event_schema`` / ``event_allowlist`` /
      ``transcript_builder_version`` /
      ``content_serialization_rules`` / ``extraction_rules`` —
      passed through to :func:`compute_fingerprint`. Keyword-only
      so the call site documents which field is which.
    template_version: Hashed into the fingerprint and recorded in
      the manifest.
    compiler_package_version: Hashed into the fingerprint and
      recorded in the manifest.
    min_nonempty_results: Forwarded to :func:`run_smoke_test`.
      Defaults to 1 so a vacuous extractor (returns empty for
      every event) doesn't quietly pass.
  """
  # Stage 1: identifier safety. Reject path-traversal-shaped names
  # before they ever reach the filesystem.
  for label, value in (
      ("module_name", module_name),
      ("function_name", function_name),
  ):
    if not _is_python_identifier(value):
      return CompileResult(
          manifest=None,
          ast_report=AstReport(),
          smoke_report=None,
          bundle_dir=None,
          invalid_identifier=(
              f"{label}={value!r} must be a plain Python identifier "
              f"(letters/digits/underscore, not starting with a digit); "
              f"compiled-extractor harness rejects path-traversal-shaped "
              f"names up front"
          ),
      )

  # Stage 1.5: declared ``event_types`` must be non-empty AND every
  # declared type has at least one matching sample event. Catches
  # an obvious metadata/sample mismatch up front — if the manifest
  # claims a bundle covers ``("wrong_event",)`` but the smoke
  # samples are all ``"bka_decision"``, the bundle's claim is
  # untestable and shouldn't ship.
  event_types_tuple = tuple(event_types)
  if not event_types_tuple:
    return CompileResult(
        manifest=None,
        ast_report=AstReport(),
        smoke_report=None,
        bundle_dir=None,
        invalid_event_types="event_types must be non-empty",
    )

  # Declared event types are themselves a public manifest contract
  # (C2's loader keys on them). Validate each entry directly rather
  # than discover problems indirectly via the sample-coverage check:
  #   - every entry must be a non-empty string;
  #   - no duplicates (a manifest claiming ("x", "x") is just noisy).
  bad_types = [
      (i, t)
      for i, t in enumerate(event_types_tuple)
      if not isinstance(t, str) or not t
  ]
  if bad_types:
    return CompileResult(
        manifest=None,
        ast_report=AstReport(),
        smoke_report=None,
        bundle_dir=None,
        invalid_event_types=(
            f"event_types[{bad_types[0][0]}]={bad_types[0][1]!r} "
            f"must be a non-empty string; every declared event "
            f"type is a public manifest field"
        ),
    )
  if len(set(event_types_tuple)) != len(event_types_tuple):
    seen: set[str] = set()
    duplicates = [t for t in event_types_tuple if t in seen or seen.add(t)]
    return CompileResult(
        manifest=None,
        ast_report=AstReport(),
        smoke_report=None,
        bundle_dir=None,
        invalid_event_types=(
            f"event_types contains duplicates: {duplicates!r}. "
            f"Each declared event type must be unique in the manifest."
        ),
    )

  # Sample events must each be a dict carrying a non-empty string
  # ``event_type``. A mix of int / None / "" event types makes the
  # coverage check unsortable for the error message and means the
  # samples can't actually be classified by type. Catch the
  # malformed shape up front so later code can rely on string keys.
  malformed_samples = [
      i
      for i, e in enumerate(sample_events)
      if not isinstance(e, dict)
      or not isinstance(e.get("event_type"), str)
      or not e.get("event_type")
  ]
  if malformed_samples:
    return CompileResult(
        manifest=None,
        ast_report=AstReport(),
        smoke_report=None,
        bundle_dir=None,
        invalid_event_types=(
            f"sample_events[{malformed_samples[0]}] (and {len(malformed_samples) - 1} "
            f"others) is not a dict with a non-empty string 'event_type'. "
            f"Every smoke sample must declare its event type so the "
            f"harness can verify coverage."
        ),
    )

  sample_types: set[str] = {e["event_type"] for e in sample_events}
  uncovered = [t for t in event_types_tuple if t not in sample_types]
  if uncovered:
    return CompileResult(
        manifest=None,
        ast_report=AstReport(),
        smoke_report=None,
        bundle_dir=None,
        invalid_event_types=(
            f"declared event_types {uncovered!r} have no matching "
            f"sample events; smoke samples cover {sorted(sample_types)!r}. "
            f"Production target is >= 100 samples per event_type "
            f"(per #75); the harness only enforces the floor of 1 "
            f"so misconfiguration is caught."
        ),
    )

  # Stage 2: fingerprint over compile inputs.
  fingerprint = compute_fingerprint(
      template_version=template_version,
      compiler_package_version=compiler_package_version,
      **fingerprint_inputs,
  )
  bundle_dir = default_bundle_dir(parent_bundle_dir, fingerprint)
  module_filename = f"{module_name}.py"

  # Stage 3: cache hit candidate. A previous successful compile
  # with the *exact same compile request* already wrote a valid
  # bundle — source bytes, module_name, function_name, and
  # event_types all have to match in addition to the fingerprint.
  # But matching the request isn't enough: the *current* call may
  # supply a stronger ``sample_events`` / ``resolved_graph`` /
  # ``min_nonempty_results`` than the call that originally wrote
  # the bundle. We re-run the smoke gate against the current
  # inputs so a weaker historical sample set can't paper over a
  # current-call regression. The bundle is **not** rewritten on
  # cache-hit success — same source bytes, same fingerprint,
  # nothing on disk needs to change.
  cached_manifest = _read_cached_manifest(
      bundle_dir,
      fingerprint=fingerprint,
      function_name=function_name,
      module_filename=f"{module_name}.py",
      event_types=tuple(event_types),
      source=source,
  )
  if cached_manifest is not None:
    cached_smoke = _smoke_test_cached_bundle(
        bundle_dir=bundle_dir,
        manifest=cached_manifest,
        sample_events=sample_events,
        spec=spec,
        resolved_graph=resolved_graph,
        min_nonempty_results=min_nonempty_results,
        isolation=isolation,
        smoke_timeout_seconds=smoke_timeout_seconds,
        smoke_memory_limit_mb=smoke_memory_limit_mb,
    )
    if cached_smoke is None:
      # Cache hit candidate but the cached source couldn't be
      # imported (corrupted bundle on disk, missing dependency,
      # whatever). Fall through to a fresh compile; the staged
      # replace will overwrite the broken bundle on success.
      pass
    elif cached_smoke.ok:
      uncovered = _event_types_without_nonempty_coverage(
          event_types_tuple, cached_smoke.nonempty_event_types
      )
      if uncovered:
        return CompileResult(
            manifest=None,
            ast_report=AstReport(),
            smoke_report=cached_smoke,
            bundle_dir=None,
            invalid_event_types=(
                f"declared event_types {uncovered!r} produced no "
                f"non-empty smoke output (cache-hit path). Saw: "
                f"{list(cached_smoke.nonempty_event_types)!r}"
            ),
        )
      return CompileResult(
          manifest=cached_manifest,
          ast_report=AstReport(),
          smoke_report=cached_smoke,
          bundle_dir=bundle_dir,
          cache_hit=True,
      )
    else:
      # Cached bundle exists, source matches the request, but
      # *current* smoke inputs reject it. The on-disk bundle is
      # left in place (rewriting wouldn't change anything: same
      # source, same AST, only the test inputs are stricter).
      return CompileResult(
          manifest=None,
          ast_report=AstReport(),
          smoke_report=cached_smoke,
          bundle_dir=None,
      )

  # Stage 4: AST gate. Failures short-circuit *before* any disk
  # write — the source is untrusted and we won't import it.
  ast_report = validate_source(source)
  if not ast_report.ok:
    return CompileResult(
        manifest=None,
        ast_report=ast_report,
        smoke_report=None,
        bundle_dir=None,
    )

  # Stage 5 + 6: stage in a sibling temp dir, atomically replace
  # ``bundle_dir`` only on success. A failed compile leaves the
  # pre-existing bundle (if any) untouched.
  parent_bundle_dir.mkdir(parents=True, exist_ok=True)
  staging = pathlib.Path(
      tempfile.mkdtemp(
          prefix=f".staging-{fingerprint[:12]}-", dir=parent_bundle_dir
      )
  )
  try:
    source_path = staging / module_filename
    source_path.write_text(source, encoding="utf-8")

    if isolation:
      # Subprocess path: source is loaded inside the child, so a
      # broken import surfaces as a subprocess failure rather than
      # an in-process exception. Wallclock timeout + RLIMIT_AS are
      # the runtime safety net for hangs / memory blowups the AST
      # allowlist can't catch statically.
      smoke_report = run_smoke_test_in_subprocess(
          source_path,
          module_name=module_name,
          function_name=function_name,
          events=sample_events,
          spec=spec,
          resolved_graph=resolved_graph,
          min_nonempty_results=min_nonempty_results,
          timeout_seconds=smoke_timeout_seconds,
          memory_limit_mb=smoke_memory_limit_mb,
      )
    else:
      try:
        extractor = load_callable_from_source(
            source_path,
            module_name=module_name,
            function_name=function_name,
        )
      except BaseException as e:  # noqa: BLE001 — surface in the report
        return CompileResult(
            manifest=None,
            ast_report=ast_report,
            smoke_report=None,
            bundle_dir=None,
            load_error=f"{type(e).__name__}: {e}",
        )

      smoke_report = run_smoke_test(
          extractor,
          events=sample_events,
          spec=spec,
          resolved_graph=resolved_graph,
          min_nonempty_results=min_nonempty_results,
      )
    if not smoke_report.ok:
      return CompileResult(
          manifest=None,
          ast_report=ast_report,
          smoke_report=smoke_report,
          bundle_dir=None,
      )

    # Declared event_types must each have actually demonstrated
    # coverage in the smoke run — at least one sample event of
    # that type must have produced a non-empty result. Without
    # this, a manifest can claim ``("x",)`` while only ``"y"``
    # samples did the work; the bundle's coverage claim is then
    # untestable.
    uncovered = _event_types_without_nonempty_coverage(
        event_types_tuple, smoke_report.nonempty_event_types
    )
    if uncovered:
      return CompileResult(
          manifest=None,
          ast_report=ast_report,
          smoke_report=smoke_report,
          bundle_dir=None,
          invalid_event_types=(
              f"declared event_types {uncovered!r} produced no non-empty "
              f"smoke output; the manifest's coverage claim must match "
              f"what the extractor actually demonstrates. Saw non-empty "
              f"output for: {list(smoke_report.nonempty_event_types)!r}"
          ),
      )

    manifest = Manifest(
        fingerprint=fingerprint,
        event_types=tuple(event_types),
        module_filename=module_filename,
        function_name=function_name,
        compiler_package_version=compiler_package_version,
        template_version=template_version,
        transcript_builder_version=fingerprint_inputs.get(
            "transcript_builder_version", ""
        ),
        created_at=now_iso_utc(),
    )
    (staging / "manifest.json").write_text(manifest.to_json(), encoding="utf-8")

    _staged_replace(staging, bundle_dir)
    # ``staging`` no longer exists after the replace; the finally
    # cleanup is a no-op for the success path.
    return CompileResult(
        manifest=manifest,
        ast_report=ast_report,
        smoke_report=smoke_report,
        bundle_dir=bundle_dir,
    )
  finally:
    if staging.exists():
      shutil.rmtree(staging, ignore_errors=True)


def _event_types_without_nonempty_coverage(
    declared: tuple[str, ...], nonempty_seen: tuple[str, ...]
) -> list[str]:
  """Return the declared event types that didn't produce non-empty
  output in the smoke run. Order preserved so the failure message
  reads naturally."""
  seen = set(nonempty_seen)
  return [t for t in declared if t not in seen]


def _is_python_identifier(value: str) -> bool:
  """Plain Python identifier: letters, digits, underscores; not
  starting with a digit; not a reserved keyword.

  ``str.isidentifier`` returns True for Python keywords (``class``,
  ``def``, ``for``, ...). The harness rejects them too — even
  though ``module_name='class'`` would work as a filename,
  ``function_name='class'`` cannot be defined by valid Python
  source and would only fail later as a load error. Rejecting
  keywords up front keeps "plain Python identifier" honest in
  both fields' validation messages.
  """
  return (
      isinstance(value, str)
      and value.isidentifier()
      and not keyword.iskeyword(value)
  )


def _read_cached_manifest(
    bundle_dir: pathlib.Path,
    *,
    fingerprint: str,
    function_name: str,
    module_filename: str,
    event_types: tuple[str, ...],
    source: str,
) -> Optional[Manifest]:
  """Return the existing manifest iff ``bundle_dir`` holds a bundle
  that exactly matches the *current compile request*.

  The fingerprint alone isn't enough — it covers the #75 input
  tuple (ontology / binding / event_schema / extraction_rules /
  versions) but NOT the candidate source, the chosen
  ``module_name``, or the per-bundle ``event_types``. A second
  call with the same fingerprint but a different module_name,
  different event_types, or different (and possibly broken)
  source must NOT be a cache hit — it has to re-run every gate
  on the actual new request.

  Returns None on any of:
    - bundle_dir doesn't exist
    - manifest.json missing / unreadable
    - fingerprint mismatch
    - function_name mismatch
    - module_filename mismatch (different module_name)
    - event_types mismatch (different per-bundle coverage)
    - module file missing on disk
    - on-disk source bytes don't equal *source*
  """
  if not bundle_dir.is_dir():
    return None
  manifest_path = bundle_dir / "manifest.json"
  if not manifest_path.is_file():
    return None
  try:
    manifest = Manifest.from_json(manifest_path.read_text(encoding="utf-8"))
  except (OSError, json.JSONDecodeError, KeyError, TypeError):
    # ``TypeError`` covers ``Manifest.from_json("42")`` and similar
    # well-formed-but-wrong-shape inputs. Treat any malformed
    # cached manifest as a cache miss rather than crashing the
    # fresh compile path.
    return None
  if manifest.fingerprint != fingerprint:
    return None
  if manifest.function_name != function_name:
    return None
  if manifest.module_filename != module_filename:
    return None
  if manifest.event_types != event_types:
    return None
  source_path = bundle_dir / manifest.module_filename
  if not source_path.is_file():
    return None
  try:
    on_disk_source = source_path.read_text(encoding="utf-8")
  except OSError:
    return None
  if on_disk_source != source:
    return None
  return manifest


def _smoke_test_cached_bundle(
    *,
    bundle_dir: pathlib.Path,
    manifest: Manifest,
    sample_events: list[dict],
    spec: Any,
    resolved_graph: Any,
    min_nonempty_results: int,
    isolation: bool,
    smoke_timeout_seconds: float,
    smoke_memory_limit_mb: Optional[int],
) -> Optional[SmokeTestReport]:
  """Run the smoke gate against a cached bundle's callable.

  The cached bundle's *source* hasn't changed (we already verified
  that against the current ``source`` argument), but the
  *current call* may pass stricter ``sample_events`` /
  ``resolved_graph`` / ``min_nonempty_results`` than the call
  that originally wrote the bundle. Re-running smoke against the
  current inputs prevents a weak historical sample set from
  papering over a current-call regression.

  Returns ``None`` if the cached source can't be imported in
  ``isolation=False`` mode — the caller treats that as a cache
  miss and falls through to a fresh compile. The subprocess path
  surfaces import failures as a ``SubprocessFailure`` exception in
  the returned report instead.
  """
  source_path = bundle_dir / manifest.module_filename
  # Module name (without the ``.py``) — the subprocess child uses
  # this to register in its own ``sys.modules``, so any string
  # that's a valid module name will do.
  module_stem = manifest.module_filename[:-3]
  if isolation:
    return run_smoke_test_in_subprocess(
        source_path,
        module_name=module_stem,
        function_name=manifest.function_name,
        events=sample_events,
        spec=spec,
        resolved_graph=resolved_graph,
        min_nonempty_results=min_nonempty_results,
        timeout_seconds=smoke_timeout_seconds,
        memory_limit_mb=smoke_memory_limit_mb,
    )

  # Per-call unique import name keeps ``sys.modules`` fresh across
  # repeated cache-hit runs in the same process.
  import_name = f"{manifest.fingerprint[:16]}__{manifest.module_filename[:-3]}"
  try:
    extractor = load_callable_from_source(
        source_path,
        module_name=import_name,
        function_name=manifest.function_name,
    )
  except BaseException:  # noqa: BLE001 — treat as cache miss
    return None
  return run_smoke_test(
      extractor,
      events=sample_events,
      spec=spec,
      resolved_graph=resolved_graph,
      min_nonempty_results=min_nonempty_results,
  )


def _staged_replace(src: pathlib.Path, dst: pathlib.Path) -> None:
  """Replace *dst* (a directory or absent) with *src* via a
  rmtree-then-rename sequence.

  Not strictly atomic: a process crash between the ``rmtree`` and
  the ``rename`` would leave *dst* absent. The bundle is
  reproducible from compile inputs, so the next compile re-creates
  it; we accept the small window in exchange for not implementing
  a backup/restore dance. Failed compile gates leave any
  pre-existing bundle untouched (the failure path returns before
  this is called).

  POSIX ``rename`` won't replace a non-empty directory; that's why
  the rmtree comes first.
  """
  if dst.exists():
    shutil.rmtree(dst)
  src.rename(dst)
