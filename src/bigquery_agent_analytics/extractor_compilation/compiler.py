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
leaving no half-written artifacts on disk:

  1. Compute the #75 fingerprint over compile inputs.
  2. AST-validate the candidate source.
  3. Write source to ``bundle_dir/<module_filename>``.
  4. Import the bundle, look up the function by name.
  5. Run the smoke-test runner on sample events with the #76
     validator gate.
  6. Write ``bundle_dir/manifest.json``.

The bundle directory is named after the fingerprint. Two compile
runs on identical inputs land in the same directory; per stage 6's
sort_keys-stable manifest, that directory is byte-identical
afterwards too.

Per the PR 4a runtime-target RFC, this module owns the *local*
bundle layout only. Runtime discovery, BQ-table mirror, and the
in-repo / sidecar-table choice are deferred to C2.
"""

from __future__ import annotations

import dataclasses
import pathlib
import shutil
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

  ``ok`` is True iff every gate (AST + import + smoke + validator)
  passed and the bundle is on disk. Callers must check ``ok``
  before assuming ``bundle_dir`` is loadable.
  """

  manifest: Optional[Manifest]
  ast_report: AstReport
  smoke_report: Optional[SmokeTestReport]
  bundle_dir: Optional[pathlib.Path]
  load_error: Optional[str] = None

  @property
  def ok(self) -> bool:
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
) -> CompileResult:
  """Run *source* through every gate and write a bundle on success.

  Args:
    source: Hand-authored Python source for the extractor function
      (4b.1) — LLM-driven fill is 4b.2's responsibility, not this
      module's. Source must define a function called *function_name*
      matching the ``StructuredExtractor`` signature.
    module_name: Stable name used for the imported module and as
      the file's stem on disk. Should be unique per
      ``(event_type, fingerprint)`` pair.
    function_name: Name of the extractor function inside *source*.
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
  """
  fingerprint = compute_fingerprint(
      template_version=template_version,
      compiler_package_version=compiler_package_version,
      **fingerprint_inputs,
  )
  bundle_dir = default_bundle_dir(parent_bundle_dir, fingerprint)

  ast_report = validate_source(source)
  if not ast_report.ok:
    # AST gate fails before we touch the disk — the source is
    # untrusted and we won't import it.
    return CompileResult(
        manifest=None,
        ast_report=ast_report,
        smoke_report=None,
        bundle_dir=None,
    )

  # Track whether we created the bundle dir so cleanup on failure
  # can nuke it without disturbing a pre-existing one (e.g., from a
  # previous successful compile with the same fingerprint, whose
  # artifacts would be byte-identical anyway).
  bundle_pre_existed = bundle_dir.exists()
  bundle_dir.mkdir(parents=True, exist_ok=True)
  module_filename = f"{module_name}.py"
  source_path = bundle_dir / module_filename
  written: list[pathlib.Path] = []

  source_path.write_text(source, encoding="utf-8")
  written.append(source_path)

  try:
    extractor = load_callable_from_source(
        source_path,
        module_name=module_name,
        function_name=function_name,
    )
  except Exception as e:  # noqa: BLE001 — surface in the report
    _cleanup(bundle_dir, written=written, pre_existed=bundle_pre_existed)
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
  )
  if not smoke_report.ok:
    _cleanup(bundle_dir, written=written, pre_existed=bundle_pre_existed)
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
  manifest_path = bundle_dir / "manifest.json"
  manifest_path.write_text(manifest.to_json(), encoding="utf-8")

  return CompileResult(
      manifest=manifest,
      ast_report=ast_report,
      smoke_report=smoke_report,
      bundle_dir=bundle_dir,
  )


def _cleanup(
    bundle_dir: pathlib.Path,
    *,
    written: list[pathlib.Path],
    pre_existed: bool,
) -> None:
  """Remove anything the harness wrote on a failed compile run.

  If *bundle_dir* didn't exist before the compile run, the whole
  directory is removed via ``shutil.rmtree`` — that catches
  ``__pycache__`` and any byproducts of the import step in addition
  to *written*.

  If *bundle_dir* pre-existed (e.g., a successful compile with the
  same fingerprint already populated it), only the explicit
  *written* paths are removed; pre-existing artifacts are left
  alone. The pre-existing case is rare in practice — fingerprints
  are deterministic, so a cached successful bundle wouldn't trigger
  a re-compile — but leaving someone else's files alone is the
  safer default.
  """
  if not pre_existed:
    shutil.rmtree(bundle_dir, ignore_errors=True)
    return
  for path in written:
    try:
      path.unlink()
    except OSError:
      pass
