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

"""Compile-time scaffolding for structured-extractor compilation (issue #75 PR 4b.1).

This package is the deterministic contract layer the LLM-driven
template fill (PR 4b.2) plugs into. **No LLM call lives here.** The
public surface is:

* :func:`compute_fingerprint` — sha256 over the #75 input tuple
  (ontology + binding + event_schema + ... + compiler_package_version).
* :class:`Manifest` — bundle provenance dataclass with ``to_json`` /
  ``from_json``.
* :func:`validate_source` returning :class:`AstReport` — allowlist-
  based AST safety check on a candidate extractor's Python source.
* :func:`run_smoke_test` returning :class:`SmokeTestReport` — runs
  the candidate against sample events and gates on the #76
  ``validate_extracted_graph`` validator.
* :func:`compile_extractor` returning :class:`CompileResult` — the
  end-to-end pipeline (fingerprint → AST → smoke + validator → write
  bundle). Bundle is on disk iff ``result.ok`` is True; otherwise
  the harness leaves no half-written artifacts.

Runtime loader / orchestrator integration, BQ-table mirror, and
fallback wiring are deferred to C2 per the PR 4a runtime-target
RFC (``docs/extractor_compilation_runtime_target.md``).
"""

from __future__ import annotations

from .ast_validator import AstFailure
from .ast_validator import AstReport
from .ast_validator import validate_source
from .compiler import compile_extractor
from .compiler import CompileResult
from .compiler import default_bundle_dir
from .fingerprint import compute_fingerprint
from .manifest import Manifest
from .manifest import now_iso_utc
from .plan_parser import parse_resolved_extractor_plan_json
from .plan_parser import PlanParseError
from .plan_parser import RESOLVED_EXTRACTOR_PLAN_JSON_SCHEMA
from .plan_resolver import build_resolution_prompt
from .plan_resolver import LLMClient
from .plan_resolver import PlanResolver
from .smoke_test import load_callable_from_source
from .smoke_test import run_smoke_test
from .smoke_test import run_smoke_test_in_subprocess
from .smoke_test import SmokeTestReport
from .template_renderer import FieldMapping
from .template_renderer import render_extractor_source
from .template_renderer import ResolvedExtractorPlan
from .template_renderer import SpanHandlingRule

__all__ = [
    "AstFailure",
    "AstReport",
    "CompileResult",
    "FieldMapping",
    "LLMClient",
    "Manifest",
    "PlanParseError",
    "PlanResolver",
    "RESOLVED_EXTRACTOR_PLAN_JSON_SCHEMA",
    "ResolvedExtractorPlan",
    "SmokeTestReport",
    "SpanHandlingRule",
    "build_resolution_prompt",
    "compile_extractor",
    "compute_fingerprint",
    "default_bundle_dir",
    "load_callable_from_source",
    "now_iso_utc",
    "parse_resolved_extractor_plan_json",
    "render_extractor_source",
    "run_smoke_test",
    "run_smoke_test_in_subprocess",
    "validate_source",
]
