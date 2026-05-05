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

"""Unit tests for the extractor-compilation scaffolding (issue #75 PR 4b.1).

Coverage:
- Fingerprint determinism + sensitivity to each named input.
- Manifest JSON round-trip.
- AST validator: accepts safe source; rejects each forbidden
  category (import, name, attribute, async, generator, class,
  scope, top-level side effect, syntax error).
- Smoke-test runner: rejects empty event lists; captures per-event
  exceptions; surfaces validator failures.
- End-to-end ``compile_extractor`` against the BKA-decision
  hand-authored fixture; bundle ends up on disk; cleanup leaves
  no half-written artifacts on AST / smoke-test failure.
- Equivalence: the compiled BKA fixture's output matches
  ``extract_bka_decision_event`` on the same events.
"""

from __future__ import annotations

import json
import pathlib
import tempfile
import uuid

import pytest

# ------------------------------------------------------------------ #
# Fixtures + helpers                                                   #
# ------------------------------------------------------------------ #


_BKA_ONTOLOGY_YAML = (
    "ontology: BkaTest\n"
    "entities:\n"
    "  - name: mako_DecisionPoint\n"
    "    keys:\n"
    "      primary: [decision_id]\n"
    "    properties:\n"
    "      - name: decision_id\n"
    "        type: string\n"
    "      - name: outcome\n"
    "        type: string\n"
    "      - name: confidence\n"
    "        type: double\n"
    "      - name: alternatives_considered\n"
    "        type: string\n"
    "relationships: []\n"
)
_BKA_BINDING_YAML = (
    "binding: bka_test\n"
    "ontology: BkaTest\n"
    "target:\n"
    "  backend: bigquery\n"
    "  project: p\n"
    "  dataset: d\n"
    "entities:\n"
    "  - name: mako_DecisionPoint\n"
    "    source: decision_points\n"
    "    properties:\n"
    "      - name: decision_id\n"
    "        column: decision_id\n"
    "      - name: outcome\n"
    "        column: outcome\n"
    "      - name: confidence\n"
    "        column: confidence\n"
    "      - name: alternatives_considered\n"
    "        column: alternatives_considered\n"
    "relationships: []\n"
)


def _bka_resolved_spec():
  from bigquery_agent_analytics.resolved_spec import resolve
  from bigquery_ontology import load_binding
  from bigquery_ontology import load_ontology

  tmp = pathlib.Path(tempfile.mkdtemp(prefix="bka_compile_test_"))
  (tmp / "ont.yaml").write_text(_BKA_ONTOLOGY_YAML, encoding="utf-8")
  (tmp / "bnd.yaml").write_text(_BKA_BINDING_YAML, encoding="utf-8")
  ontology = load_ontology(str(tmp / "ont.yaml"))
  binding = load_binding(str(tmp / "bnd.yaml"), ontology=ontology)
  return resolve(ontology, binding)


def _sample_bka_events():
  """Two events: one with reasoning_text (partial), one without
  (fully handled)."""
  return [
      {
          "event_type": "bka_decision",
          "session_id": "sess1",
          "span_id": "span1",
          "content": {
              "decision_id": "d1",
              "outcome": "approved",
              "confidence": 0.92,
              "reasoning_text": "free-form rationale",
          },
      },
      {
          "event_type": "bka_decision",
          "session_id": "sess1",
          "span_id": "span2",
          "content": {
              "decision_id": "d2",
              "outcome": "rejected",
              "confidence": 0.4,
          },
      },
  ]


def _fingerprint_inputs():
  return {
      "ontology_text": _BKA_ONTOLOGY_YAML,
      "binding_text": _BKA_BINDING_YAML,
      "event_schema": {
          "bka_decision": {
              "content": {
                  "decision_id": "string",
                  "outcome": "string",
                  "confidence": "double",
                  "reasoning_text": "string",
              }
          }
      },
      "event_allowlist": ("bka_decision",),
      "transcript_builder_version": "v0.1",
      "content_serialization_rules": {"strip_ansi": True},
      "extraction_rules": {
          "bka_decision": {
              "entity": "mako_DecisionPoint",
              "key_field": "decision_id",
          }
      },
  }


def _unique_module_name(prefix: str = "bka_compiled_") -> str:
  """Per-test unique module name so importlib doesn't recycle a
  stale ``sys.modules`` entry from a previous test in the same
  pytest session."""
  return f"{prefix}{uuid.uuid4().hex[:12]}"


# ------------------------------------------------------------------ #
# Fingerprint                                                          #
# ------------------------------------------------------------------ #


class TestFingerprint:

  def test_identical_inputs_produce_identical_fingerprint(self):
    from bigquery_agent_analytics.extractor_compilation import compute_fingerprint

    inputs = _fingerprint_inputs()
    a = compute_fingerprint(
        template_version="v0.1",
        compiler_package_version="0.0.0",
        **inputs,
    )
    b = compute_fingerprint(
        template_version="v0.1",
        compiler_package_version="0.0.0",
        **inputs,
    )
    assert a == b
    assert len(a) == 64
    assert all(c in "0123456789abcdef" for c in a)

  def test_event_allowlist_order_does_not_matter(self):
    """The allowlist is sorted before hashing — caller-side order
    is irrelevant."""
    from bigquery_agent_analytics.extractor_compilation import compute_fingerprint

    inputs = _fingerprint_inputs()
    a = compute_fingerprint(
        template_version="v0.1",
        compiler_package_version="0.0.0",
        **(inputs | {"event_allowlist": ("a", "b", "c")}),
    )
    b = compute_fingerprint(
        template_version="v0.1",
        compiler_package_version="0.0.0",
        **(inputs | {"event_allowlist": ("c", "b", "a")}),
    )
    assert a == b

  @pytest.mark.parametrize(
      "field,override",
      [
          ("ontology_text", "ontology: Different\n"),
          ("binding_text", "binding: different\n"),
          ("event_schema", {"different": {}}),
          ("event_allowlist", ("other_event",)),
          ("transcript_builder_version", "v0.2"),
          ("content_serialization_rules", {"strip_ansi": False}),
          ("extraction_rules", {"other": {}}),
      ],
  )
  def test_each_input_field_is_hashed(self, field, override):
    """Changing any of the seven ``fingerprint_inputs`` fields
    invalidates the fingerprint."""
    from bigquery_agent_analytics.extractor_compilation import compute_fingerprint

    base = _fingerprint_inputs()
    a = compute_fingerprint(
        template_version="v0.1",
        compiler_package_version="0.0.0",
        **base,
    )
    b = compute_fingerprint(
        template_version="v0.1",
        compiler_package_version="0.0.0",
        **(base | {field: override}),
    )
    assert a != b, f"changing {field!r} did not change the fingerprint"

  def test_template_version_and_compiler_version_are_hashed(self):
    from bigquery_agent_analytics.extractor_compilation import compute_fingerprint

    inputs = _fingerprint_inputs()
    base = compute_fingerprint(
        template_version="v0.1",
        compiler_package_version="0.0.0",
        **inputs,
    )
    bumped_template = compute_fingerprint(
        template_version="v0.2",
        compiler_package_version="0.0.0",
        **inputs,
    )
    bumped_compiler = compute_fingerprint(
        template_version="v0.1",
        compiler_package_version="0.0.1",
        **inputs,
    )
    assert base != bumped_template
    assert base != bumped_compiler
    assert bumped_template != bumped_compiler


# ------------------------------------------------------------------ #
# Manifest                                                             #
# ------------------------------------------------------------------ #


class TestManifest:

  def test_round_trip_through_json(self):
    from bigquery_agent_analytics.extractor_compilation import Manifest

    m = Manifest(
        fingerprint="a" * 64,
        event_types=("bka_decision", "tool_completed"),
        module_filename="bka_compiled.py",
        function_name="extract_bka_decision_event_compiled",
        compiler_package_version="0.0.0",
        template_version="v0.1",
        transcript_builder_version="v0.1",
        created_at="2026-05-05T00:00:00+00:00",
    )
    text = m.to_json()
    parsed = Manifest.from_json(text)
    assert parsed == m

  def test_to_json_is_byte_stable(self):
    """Two manifests with identical fields produce identical JSON.
    A bundle directory's manifest.json must be byte-stable so a
    re-compile with no input changes is genuinely a no-op."""
    from bigquery_agent_analytics.extractor_compilation import Manifest

    m1 = Manifest(
        fingerprint="a" * 64,
        event_types=("bka_decision",),
        module_filename="m.py",
        function_name="f",
        compiler_package_version="0.0.0",
        template_version="v0.1",
        transcript_builder_version="v0.1",
        created_at="2026-05-05T00:00:00+00:00",
    )
    m2 = Manifest(**{**m1.__dict__})
    assert m1.to_json() == m2.to_json()

  def test_json_keys_are_sorted(self):
    from bigquery_agent_analytics.extractor_compilation import Manifest

    m = Manifest(
        fingerprint="a" * 64,
        event_types=("bka_decision",),
        module_filename="m.py",
        function_name="f",
        compiler_package_version="0.0.0",
        template_version="v0.1",
        transcript_builder_version="v0.1",
        created_at="2026-05-05T00:00:00+00:00",
    )
    parsed = json.loads(m.to_json())
    assert list(parsed.keys()) == sorted(parsed.keys())


# ------------------------------------------------------------------ #
# AST validator                                                        #
# ------------------------------------------------------------------ #


class TestAstValidator:

  def test_safe_source_passes(self):
    from bigquery_agent_analytics.extractor_compilation import validate_source
    from tests.fixtures_extractor_compilation.bka_decision_template import BKA_DECISION_SOURCE

    report = validate_source(BKA_DECISION_SOURCE)
    assert (
        report.ok is True
    ), f"failures: {[(f.code, f.detail) for f in report.failures]}"

  def test_disallowed_import_outside_allowlist(self):
    from bigquery_agent_analytics.extractor_compilation import validate_source

    src = "from os import system\n" "def f(event, spec):\n" "    return None\n"
    report = validate_source(src)
    assert any(f.code == "disallowed_import" for f in report.failures)

  def test_plain_import_rejected_even_for_allowlisted_module(self):
    from bigquery_agent_analytics.extractor_compilation import validate_source

    src = (
        "import bigquery_agent_analytics\n"
        "def f(event, spec):\n"
        "    return None\n"
    )
    report = validate_source(src)
    assert any(f.code == "disallowed_import" for f in report.failures)

  @pytest.mark.parametrize(
      "name",
      ["eval", "exec", "compile", "__import__", "open", "input", "getattr"],
  )
  def test_disallowed_name(self, name):
    from bigquery_agent_analytics.extractor_compilation import validate_source

    src = f"def f(event, spec):\n    return {name}('x')\n"
    report = validate_source(src)
    assert any(f.code == "disallowed_name" for f in report.failures)

  def test_disallowed_dunder_attribute(self):
    from bigquery_agent_analytics.extractor_compilation import validate_source

    src = "def f(event, spec):\n" "    return event.__class__\n"
    report = validate_source(src)
    assert any(f.code == "disallowed_attribute" for f in report.failures)

  def test_async_def_rejected(self):
    from bigquery_agent_analytics.extractor_compilation import validate_source

    src = "async def f(event, spec):\n    return None\n"
    report = validate_source(src)
    assert any(f.code == "disallowed_async" for f in report.failures)

  def test_yield_rejected(self):
    from bigquery_agent_analytics.extractor_compilation import validate_source

    src = "def f(event, spec):\n    yield 1\n"
    report = validate_source(src)
    assert any(f.code == "disallowed_generator" for f in report.failures)

  def test_class_definition_rejected(self):
    from bigquery_agent_analytics.extractor_compilation import validate_source

    src = "class Foo:\n    pass\n"
    report = validate_source(src)
    assert any(f.code == "disallowed_class" for f in report.failures)

  def test_top_level_assignment_rejected(self):
    from bigquery_agent_analytics.extractor_compilation import validate_source

    src = "X = 5\n" "def f(event, spec):\n" "    return None\n"
    report = validate_source(src)
    assert any(f.code == "top_level_side_effect" for f in report.failures)

  def test_syntax_error_reported(self):
    from bigquery_agent_analytics.extractor_compilation import validate_source

    report = validate_source("def f(:\n")
    assert len(report.failures) == 1
    assert report.failures[0].code == "syntax_error"


# ------------------------------------------------------------------ #
# Smoke-test runner                                                    #
# ------------------------------------------------------------------ #


class TestSmokeTest:

  def test_empty_events_list_rejected(self):
    from bigquery_agent_analytics.extractor_compilation import run_smoke_test
    from bigquery_agent_analytics.structured_extraction import StructuredExtractionResult

    def extractor(event, spec):
      return StructuredExtractionResult()

    with pytest.raises(ValueError):
      run_smoke_test(extractor, events=[], spec=None, resolved_graph=None)

  def test_per_event_exceptions_captured(self):
    from bigquery_agent_analytics.extractor_compilation import run_smoke_test

    def extractor(event, spec):
      raise RuntimeError("boom")

    report = run_smoke_test(
        extractor,
        events=[{"event_type": "x"}, {"event_type": "y"}],
        spec=None,
        resolved_graph=None,
    )
    assert report.ok is False
    assert report.events_with_exception == 2
    assert all("boom" in e for e in report.exceptions)

  def test_validator_failures_surfaced(self):
    """Smoke-test fails when the merged graph doesn't validate
    against the resolved spec — even if every per-event call
    completed without an exception."""
    from bigquery_agent_analytics.extracted_models import ExtractedNode
    from bigquery_agent_analytics.extracted_models import ExtractedProperty
    from bigquery_agent_analytics.extractor_compilation import run_smoke_test
    from bigquery_agent_analytics.structured_extraction import StructuredExtractionResult

    spec = _bka_resolved_spec()

    def extractor(event, spec_):
      # Decision_id should be a string per the ontology, but emit
      # an int — the #76 validator will flag this.
      return StructuredExtractionResult(
          nodes=[
              ExtractedNode(
                  node_id="sess1:mako_DecisionPoint:decision_id=42",
                  entity_name="mako_DecisionPoint",
                  labels=["mako_DecisionPoint"],
                  properties=[
                      ExtractedProperty(name="decision_id", value=42),
                  ],
              )
          ]
      )

    report = run_smoke_test(
        extractor,
        events=[{"event_type": "bka_decision"}],
        spec=None,
        resolved_graph=spec,
    )
    assert report.ok is False
    assert report.events_with_exception == 0
    assert any(f.code == "type_mismatch" for f in report.validation_failures)

  def test_clean_run_returns_ok(self):
    from bigquery_agent_analytics.extracted_models import ExtractedNode
    from bigquery_agent_analytics.extracted_models import ExtractedProperty
    from bigquery_agent_analytics.extractor_compilation import run_smoke_test
    from bigquery_agent_analytics.structured_extraction import StructuredExtractionResult

    spec = _bka_resolved_spec()

    def extractor(event, spec_):
      content = event.get("content", {})
      did = content.get("decision_id")
      if did is None:
        return StructuredExtractionResult()
      return StructuredExtractionResult(
          nodes=[
              ExtractedNode(
                  node_id=f"sess1:mako_DecisionPoint:decision_id={did}",
                  entity_name="mako_DecisionPoint",
                  labels=["mako_DecisionPoint"],
                  properties=[
                      ExtractedProperty(name="decision_id", value=did),
                  ],
              )
          ]
      )

    report = run_smoke_test(
        extractor,
        events=_sample_bka_events(),
        spec=None,
        resolved_graph=spec,
    )
    assert report.ok is True
    assert report.events_with_exception == 0
    assert report.validation_failures == ()


# ------------------------------------------------------------------ #
# End-to-end compile_extractor                                         #
# ------------------------------------------------------------------ #


class TestCompileExtractor:

  def test_bka_fixture_compiles_clean(self, tmp_path: pathlib.Path):
    """Hand-authored BKA fixture clears every gate (AST + import
    + smoke + #76 validator) and produces an on-disk bundle."""
    from bigquery_agent_analytics.extractor_compilation import compile_extractor
    from tests.fixtures_extractor_compilation.bka_decision_template import BKA_DECISION_SOURCE

    spec = _bka_resolved_spec()
    result = compile_extractor(
        source=BKA_DECISION_SOURCE,
        module_name=_unique_module_name(),
        function_name="extract_bka_decision_event_compiled",
        event_types=("bka_decision",),
        sample_events=_sample_bka_events(),
        spec=None,
        resolved_graph=spec,
        parent_bundle_dir=tmp_path,
        fingerprint_inputs=_fingerprint_inputs(),
        template_version="v0.1",
        compiler_package_version="0.0.0",
    )
    assert (
        result.ok is True
    ), f"compile failed: ast={result.ast_report.failures} smoke={result.smoke_report and result.smoke_report.exceptions or []} validator={result.smoke_report and result.smoke_report.validation_failures or []}"
    assert result.bundle_dir is not None
    assert result.manifest is not None
    assert (result.bundle_dir / "manifest.json").exists()
    assert (result.bundle_dir / result.manifest.module_filename).exists()

  def test_compiled_output_matches_handwritten_extractor(
      self, tmp_path: pathlib.Path
  ):
    """The whole point of the BKA fixture: its compiled output is
    semantically equivalent to ``extract_bka_decision_event``."""
    from bigquery_agent_analytics.extractor_compilation import compile_extractor
    from bigquery_agent_analytics.extractor_compilation import load_callable_from_source
    from bigquery_agent_analytics.structured_extraction import extract_bka_decision_event
    from tests.fixtures_extractor_compilation.bka_decision_template import BKA_DECISION_SOURCE

    module_name = _unique_module_name()
    spec = _bka_resolved_spec()
    result = compile_extractor(
        source=BKA_DECISION_SOURCE,
        module_name=module_name,
        function_name="extract_bka_decision_event_compiled",
        event_types=("bka_decision",),
        sample_events=_sample_bka_events(),
        spec=None,
        resolved_graph=spec,
        parent_bundle_dir=tmp_path,
        fingerprint_inputs=_fingerprint_inputs(),
        template_version="v0.1",
        compiler_package_version="0.0.0",
    )
    assert result.ok is True

    # Re-load the bundle by file path (matches what C2's loader
    # will do once it lands).
    compiled = load_callable_from_source(
        result.bundle_dir / result.manifest.module_filename,
        module_name=_unique_module_name(prefix="bka_reload_"),
        function_name="extract_bka_decision_event_compiled",
    )
    for event in _sample_bka_events():
      hand = extract_bka_decision_event(event, None)
      auto = compiled(event, None)
      assert _result_signature(hand) == _result_signature(
          auto
      ), f"compiled vs hand-written diverge on event {event!r}"

  def test_ast_failure_short_circuits_no_bundle_on_disk(
      self, tmp_path: pathlib.Path
  ):
    from bigquery_agent_analytics.extractor_compilation import compile_extractor

    bad_source = (
        "from os import system\n" "def f(event, spec):\n" "    return None\n"
    )
    result = compile_extractor(
        source=bad_source,
        module_name=_unique_module_name(prefix="bad_"),
        function_name="f",
        event_types=("x",),
        sample_events=[{"event_type": "x"}],
        spec=None,
        resolved_graph=None,
        parent_bundle_dir=tmp_path,
        fingerprint_inputs=_fingerprint_inputs(),
        template_version="v0.1",
        compiler_package_version="0.0.0",
    )
    assert result.ok is False
    assert result.bundle_dir is None
    assert result.smoke_report is None
    # No bundle directories created under tmp_path.
    assert not list(tmp_path.iterdir())

  def test_smoke_failure_cleans_up_partial_bundle(self, tmp_path: pathlib.Path):
    """When the smoke-test runner reports a validator failure, the
    harness must remove the source file it wrote and leave the
    bundle directory empty (or absent)."""
    from bigquery_agent_analytics.extractor_compilation import compile_extractor

    # Source clears the AST gate but produces a node with a
    # type_mismatch (decision_id as int instead of string).
    bad_source = '''
"""Compiled extractor that emits a type_mismatch node."""

from __future__ import annotations

from bigquery_agent_analytics.extracted_models import ExtractedNode
from bigquery_agent_analytics.extracted_models import ExtractedProperty
from bigquery_agent_analytics.structured_extraction import (
    StructuredExtractionResult,
)


def f(event, spec):
  return StructuredExtractionResult(
      nodes=[
          ExtractedNode(
              node_id="sess1:mako_DecisionPoint:decision_id=99",
              entity_name="mako_DecisionPoint",
              labels=["mako_DecisionPoint"],
              properties=[ExtractedProperty(name="decision_id", value=99)],
          )
      ]
  )
'''
    spec = _bka_resolved_spec()
    result = compile_extractor(
        source=bad_source,
        module_name=_unique_module_name(prefix="smoke_fail_"),
        function_name="f",
        event_types=("bka_decision",),
        sample_events=[{"event_type": "bka_decision"}],
        spec=None,
        resolved_graph=spec,
        parent_bundle_dir=tmp_path,
        fingerprint_inputs=_fingerprint_inputs(),
        template_version="v0.1",
        compiler_package_version="0.0.0",
    )
    assert result.ok is False
    assert result.smoke_report is not None
    assert any(
        f.code == "type_mismatch"
        for f in result.smoke_report.validation_failures
    )
    # Partial bundle dir was created and then removed.
    assert not list(tmp_path.iterdir())

  def test_identical_inputs_produce_identical_bundle_directory(
      self, tmp_path: pathlib.Path
  ):
    """Two compile runs on the same inputs land in the same
    fingerprint-named directory and write byte-identical
    artifacts (modulo ``created_at``)."""
    from bigquery_agent_analytics.extractor_compilation import compile_extractor
    from tests.fixtures_extractor_compilation.bka_decision_template import BKA_DECISION_SOURCE

    spec = _bka_resolved_spec()
    kwargs = dict(
        source=BKA_DECISION_SOURCE,
        module_name="bka_stable",
        function_name="extract_bka_decision_event_compiled",
        event_types=("bka_decision",),
        sample_events=_sample_bka_events(),
        spec=None,
        resolved_graph=spec,
        parent_bundle_dir=tmp_path,
        fingerprint_inputs=_fingerprint_inputs(),
        template_version="v0.1",
        compiler_package_version="0.0.0",
    )
    a = compile_extractor(**kwargs)
    b = compile_extractor(**kwargs)
    assert a.ok and b.ok
    assert a.bundle_dir == b.bundle_dir
    assert a.manifest.fingerprint == b.manifest.fingerprint


# ------------------------------------------------------------------ #
# Helpers                                                              #
# ------------------------------------------------------------------ #


def _result_signature(result):
  """Tuple form that compares two ``StructuredExtractionResult``
  instances structurally — order of properties matters here so an
  extractor that re-orders fields would be flagged."""
  nodes = tuple(
      (
          n.node_id,
          n.entity_name,
          tuple(n.labels),
          tuple((p.name, p.value) for p in n.properties),
      )
      for n in result.nodes
  )
  edges = tuple(
      (
          e.edge_id,
          e.relationship_name,
          e.from_node_id,
          e.to_node_id,
          tuple((p.name, p.value) for p in e.properties),
      )
      for e in result.edges
  )
  return (
      nodes,
      edges,
      frozenset(result.fully_handled_span_ids),
      frozenset(result.partially_handled_span_ids),
  )
