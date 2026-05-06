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

  1. Validate ``module_name`` / ``function_name`` are plain Python
     identifiers (path-traversal safety).
  2. Compute the #75 fingerprint over compile inputs.
  3. **Cache hit:** if ``<bundle_dir>/manifest.json`` already
     exists with a matching fingerprint and function_name, return
     the cached bundle without re-running any gates or re-writing
     any files.
  4. AST-validate the candidate source.
  5. **Stage** in a sibling temp directory under ``parent_bundle_dir``:
     write source, import the module, look up the function, run the
     smoke-test runner with the #76 validator gate, write the
     manifest.
  6. **Atomically replace** the (possibly pre-existing) bundle
     directory with the staged one. A failed compile leaves the
     pre-existing bundle untouched; the staging directory is
     removed on every error path.

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

  @property
  def ok(self) -> bool:
    if self.invalid_identifier is not None:
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

  # Stage 2: fingerprint over compile inputs.
  fingerprint = compute_fingerprint(
      template_version=template_version,
      compiler_package_version=compiler_package_version,
      **fingerprint_inputs,
  )
  bundle_dir = default_bundle_dir(parent_bundle_dir, fingerprint)
  module_filename = f"{module_name}.py"

  # Stage 3: cache hit. A previous successful compile with the
  # same fingerprint already wrote a valid bundle. Returning here
  # guarantees the on-disk bundle is byte-identical between
  # consecutive ``compile_extractor`` calls — only the first call
  # writes anything.
  cached_manifest = _read_cached_manifest(
      bundle_dir, fingerprint=fingerprint, function_name=function_name
  )
  if cached_manifest is not None:
    return CompileResult(
        manifest=cached_manifest,
        ast_report=AstReport(),
        smoke_report=None,
        bundle_dir=bundle_dir,
        cache_hit=True,
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

    _atomic_replace(staging, bundle_dir)
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


def _is_python_identifier(value: str) -> bool:
  """``str.isidentifier`` is exactly the rule the harness wants:
  letters, digits, underscores; not starting with a digit. Rejects
  ``../x``, ``foo.bar``, ``foo-bar``, the empty string, and Python
  keywords (handled by ``isidentifier`` returning True for keywords
  but failing later — we treat keywords as valid here since the
  user controls the file's stem; ``def`` as a stem would just
  produce ``def.py`` which is fine on disk)."""
  return isinstance(value, str) and value.isidentifier()


def _read_cached_manifest(
    bundle_dir: pathlib.Path,
    *,
    fingerprint: str,
    function_name: str,
) -> Optional[Manifest]:
  """Return the existing manifest iff ``bundle_dir`` holds a valid
  bundle whose fingerprint and function_name match the active
  compile inputs.

  Returns None on any of:
    - bundle_dir doesn't exist
    - manifest.json missing or unreadable
    - fingerprint mismatch (stale or unrelated bundle in this dir)
    - function_name mismatch (different extractor under same
      fingerprint, which shouldn't happen — fingerprint covers
      template inputs — but treat defensively)
    - module file missing
  """
  if not bundle_dir.is_dir():
    return None
  manifest_path = bundle_dir / "manifest.json"
  if not manifest_path.is_file():
    return None
  try:
    manifest = Manifest.from_json(manifest_path.read_text(encoding="utf-8"))
  except (OSError, json.JSONDecodeError, KeyError):
    return None
  if manifest.fingerprint != fingerprint:
    return None
  if manifest.function_name != function_name:
    return None
  if not (bundle_dir / manifest.module_filename).is_file():
    return None
  return manifest


def _atomic_replace(src: pathlib.Path, dst: pathlib.Path) -> None:
  """Replace *dst* (a directory or absent) with *src* atomically
  *enough* for compile bundles.

  POSIX ``rename`` won't replace a non-empty directory, so the
  sequence is: rmtree-old → rename-new. The window between the
  two is small; a process crash mid-replace leaves *dst* absent
  (the next compile re-creates it). That's acceptable for
  compile bundles, which are reproducible from inputs.
  """
  if dst.exists():
    shutil.rmtree(dst)
  src.rename(dst)
