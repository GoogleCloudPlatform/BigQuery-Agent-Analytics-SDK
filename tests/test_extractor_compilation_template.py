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

"""Tests for the deterministic source generator (issue #75 PR 4b.2.1).

Coverage:
- Plan validation (function_name, event_type, target_entity_name,
  key_field, property_fields, session_id_path, span_handling).
- Generated source clears 4b.1's ``validate_source`` AST gate.
- Generated source compiles end-to-end via ``compile_extractor``
  (including subprocess smoke + #76 validator).
- BKA-equivalent plan produces output matching
  ``extract_bka_decision_event`` on the same sample events.
- Determinism: identical plans render byte-identical source.
- Plan-shape variations: optional span_handling, no
  property_fields, deep traversal paths, single-step paths.
"""

from __future__ import annotations

import pathlib
import tempfile
import uuid

import pytest

# ------------------------------------------------------------------ #
# Shared fixtures                                                     #
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

  tmp = pathlib.Path(tempfile.mkdtemp(prefix="bka_template_test_"))
  (tmp / "ont.yaml").write_text(_BKA_ONTOLOGY_YAML, encoding="utf-8")
  (tmp / "bnd.yaml").write_text(_BKA_BINDING_YAML, encoding="utf-8")
  ontology = load_ontology(str(tmp / "ont.yaml"))
  binding = load_binding(str(tmp / "bnd.yaml"), ontology=ontology)
  return resolve(ontology, binding)


def _sample_bka_events():
  return [
      {
          "event_type": "bka_decision",
          "session_id": "sess1",
          "span_id": "span1",
          "content": {
              "decision_id": "d1",
              "outcome": "approved",
              "confidence": 0.92,
              "reasoning_text": "rationale",
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
      {
          # Event without decision_id — extractor declines.
          "event_type": "bka_decision",
          "session_id": "sess1",
          "span_id": "span3",
          "content": {"unrelated": "noise"},
      },
      {
          # Event with non-dict content — extractor declines.
          "event_type": "bka_decision",
          "session_id": "sess1",
          "span_id": "span4",
          "content": None,
      },
  ]


def _bka_plan(*, function_name: str = "extract_bka_decision_event_compiled"):
  from bigquery_agent_analytics.extractor_compilation import FieldMapping
  from bigquery_agent_analytics.extractor_compilation import ResolvedExtractorPlan
  from bigquery_agent_analytics.extractor_compilation import SpanHandlingRule

  return ResolvedExtractorPlan(
      event_type="bka_decision",
      target_entity_name="mako_DecisionPoint",
      function_name=function_name,
      key_field=FieldMapping(
          property_name="decision_id",
          source_path=("content", "decision_id"),
      ),
      property_fields=(
          FieldMapping("outcome", ("content", "outcome")),
          FieldMapping("confidence", ("content", "confidence")),
          FieldMapping(
              "alternatives_considered", ("content", "alternatives_considered")
          ),
      ),
      span_handling=SpanHandlingRule(
          span_id_path=("span_id",),
          partial_when_path=("content", "reasoning_text"),
      ),
  )


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


def _unique_module_name(prefix: str = "rendered_") -> str:
  return f"{prefix}{uuid.uuid4().hex[:12]}"


def _result_signature(result):
  """Same structural compare used in
  ``test_extractor_compilation.test_compiled_output_matches_handwritten_extractor``.
  """
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


# ------------------------------------------------------------------ #
# Plan validation                                                     #
# ------------------------------------------------------------------ #


class TestPlanValidation:

  def test_valid_bka_plan_renders(self):
    from bigquery_agent_analytics.extractor_compilation import render_extractor_source

    src = render_extractor_source(_bka_plan())
    assert "def extract_bka_decision_event_compiled" in src

  @pytest.mark.parametrize("name", ["", "1leading", "with space", "../escape"])
  def test_invalid_function_name_rejected(self, name):
    from bigquery_agent_analytics.extractor_compilation import render_extractor_source

    plan = _bka_plan(function_name=name)
    with pytest.raises(ValueError, match="function_name"):
      render_extractor_source(plan)

  @pytest.mark.parametrize("kw", ["class", "def", "for", "return"])
  def test_python_keyword_function_name_rejected(self, kw):
    from bigquery_agent_analytics.extractor_compilation import render_extractor_source

    plan = _bka_plan(function_name=kw)
    with pytest.raises(ValueError, match="function_name"):
      render_extractor_source(plan)

  @pytest.mark.parametrize("name", ["len", "isinstance", "ExtractedNode"])
  def test_function_name_shadowing_call_target_rejected(self, name):
    """function_name in ``_ALLOWED_CALL_TARGETS`` would shadow the
    builtin in the generated module. Catch up front."""
    from bigquery_agent_analytics.extractor_compilation import render_extractor_source

    plan = _bka_plan(function_name=name)
    with pytest.raises(ValueError, match="shadow"):
      render_extractor_source(plan)

  def test_empty_event_type_rejected(self):
    from bigquery_agent_analytics.extractor_compilation import FieldMapping
    from bigquery_agent_analytics.extractor_compilation import render_extractor_source
    from bigquery_agent_analytics.extractor_compilation import ResolvedExtractorPlan

    plan = ResolvedExtractorPlan(
        event_type="",
        target_entity_name="E",
        function_name="f",
        key_field=FieldMapping("k", ("k",)),
    )
    with pytest.raises(ValueError, match="event_type"):
      render_extractor_source(plan)

  def test_empty_target_entity_rejected(self):
    from bigquery_agent_analytics.extractor_compilation import FieldMapping
    from bigquery_agent_analytics.extractor_compilation import render_extractor_source
    from bigquery_agent_analytics.extractor_compilation import ResolvedExtractorPlan

    plan = ResolvedExtractorPlan(
        event_type="x",
        target_entity_name="",
        function_name="f",
        key_field=FieldMapping("k", ("k",)),
    )
    with pytest.raises(ValueError, match="target_entity_name"):
      render_extractor_source(plan)

  def test_empty_key_path_rejected(self):
    from bigquery_agent_analytics.extractor_compilation import FieldMapping
    from bigquery_agent_analytics.extractor_compilation import render_extractor_source
    from bigquery_agent_analytics.extractor_compilation import ResolvedExtractorPlan

    plan = ResolvedExtractorPlan(
        event_type="x",
        target_entity_name="E",
        function_name="f",
        key_field=FieldMapping("k", ()),
    )
    with pytest.raises(ValueError, match="source_path"):
      render_extractor_source(plan)

  def test_duplicate_property_name_rejected(self):
    from bigquery_agent_analytics.extractor_compilation import FieldMapping
    from bigquery_agent_analytics.extractor_compilation import render_extractor_source
    from bigquery_agent_analytics.extractor_compilation import ResolvedExtractorPlan

    plan = ResolvedExtractorPlan(
        event_type="x",
        target_entity_name="E",
        function_name="f",
        key_field=FieldMapping("k", ("k",)),
        property_fields=(
            FieldMapping("dup", ("a",)),
            FieldMapping("dup", ("b",)),
        ),
    )
    with pytest.raises(ValueError, match="duplicate property_name"):
      render_extractor_source(plan)

  def test_target_entity_name_with_quote_rejected(self):
    """``target_entity_name`` is embedded directly in an f-string
    in the generated source. A value containing a quote would
    produce broken Python. The identifier-shape check rejects it
    before any source is generated."""
    from bigquery_agent_analytics.extractor_compilation import FieldMapping
    from bigquery_agent_analytics.extractor_compilation import render_extractor_source
    from bigquery_agent_analytics.extractor_compilation import ResolvedExtractorPlan

    plan = ResolvedExtractorPlan(
        event_type="x",
        target_entity_name="Foo's",
        function_name="f",
        key_field=FieldMapping("decision_id", ("decision_id",)),
    )
    with pytest.raises(ValueError, match="target_entity_name"):
      render_extractor_source(plan)

  @pytest.mark.parametrize(
      "bad_name",
      [
          "bad-name",
          "with space",
          "1leading",
          "weird;chars",
      ],
  )
  def test_property_name_with_non_identifier_chars_rejected(self, bad_name):
    """Property names with characters that aren't identifier-safe
    used to render variable names like ``_op_bad-name_0`` (broken
    Python). The renderer now derives variable names from the
    property index, but the validator still rejects non-identifier
    property names so the manifest contract stays predictable."""
    from bigquery_agent_analytics.extractor_compilation import FieldMapping
    from bigquery_agent_analytics.extractor_compilation import render_extractor_source
    from bigquery_agent_analytics.extractor_compilation import ResolvedExtractorPlan

    plan = ResolvedExtractorPlan(
        event_type="x",
        target_entity_name="E",
        function_name="f",
        key_field=FieldMapping("decision_id", ("decision_id",)),
        property_fields=(FieldMapping(bad_name, ("p",)),),
    )
    with pytest.raises(ValueError, match="property_name"):
      render_extractor_source(plan)

  @pytest.mark.parametrize(
      "field, value",
      [
          ("event_type", 1),
          ("event_type", None),
          ("target_entity_name", 42),
          ("function_name", b"bytes"),
      ],
  )
  def test_non_string_top_level_fields_rejected(self, field, value):
    """``ResolvedExtractorPlan`` doesn't enforce types at runtime
    (no Pydantic). The renderer catches non-string values up front
    so they don't render as broken Python that fails later in
    ``compile_extractor`` or at runtime."""
    from bigquery_agent_analytics.extractor_compilation import FieldMapping
    from bigquery_agent_analytics.extractor_compilation import render_extractor_source
    from bigquery_agent_analytics.extractor_compilation import ResolvedExtractorPlan

    base = dict(
        event_type="x",
        target_entity_name="E",
        function_name="f",
        key_field=FieldMapping("k", ("k",)),
    )
    base[field] = value
    plan = ResolvedExtractorPlan(**base)
    with pytest.raises(ValueError):
      render_extractor_source(plan)

  def test_non_string_path_segment_rejected(self):
    from bigquery_agent_analytics.extractor_compilation import FieldMapping
    from bigquery_agent_analytics.extractor_compilation import render_extractor_source
    from bigquery_agent_analytics.extractor_compilation import ResolvedExtractorPlan

    plan = ResolvedExtractorPlan(
        event_type="x",
        target_entity_name="E",
        function_name="f",
        key_field=FieldMapping("k", ("ok", 1, "x")),
    )
    with pytest.raises(ValueError, match="key_field.source_path"):
      render_extractor_source(plan)

  def test_property_name_collides_with_key_rejected(self):
    """The key property is also in the rendered properties list,
    so a property_field with the same name would emit two
    ExtractedProperty entries with the same ``name`` — confusing
    at materialize time. Reject up front."""
    from bigquery_agent_analytics.extractor_compilation import FieldMapping
    from bigquery_agent_analytics.extractor_compilation import render_extractor_source
    from bigquery_agent_analytics.extractor_compilation import ResolvedExtractorPlan

    plan = ResolvedExtractorPlan(
        event_type="x",
        target_entity_name="E",
        function_name="f",
        key_field=FieldMapping("k", ("k",)),
        property_fields=(FieldMapping("k", ("alt",)),),
    )
    with pytest.raises(ValueError, match="duplicate property_name"):
      render_extractor_source(plan)


# ------------------------------------------------------------------ #
# Generated source clears 4b.1's gates                                #
# ------------------------------------------------------------------ #


class TestGeneratedSourceClearsGates:

  def test_bka_source_passes_ast_validator(self):
    from bigquery_agent_analytics.extractor_compilation import render_extractor_source
    from bigquery_agent_analytics.extractor_compilation import validate_source

    src = render_extractor_source(_bka_plan())
    report = validate_source(src)
    assert (
        report.ok is True
    ), f"failures: {[(f.code, f.detail) for f in report.failures]}"

  def test_bka_source_compiles_end_to_end(self, tmp_path: pathlib.Path):
    """Full pipeline: render → ``compile_extractor`` → bundle on
    disk. Default isolation=True so this exercises the subprocess
    smoke gate too."""
    from bigquery_agent_analytics.extractor_compilation import compile_extractor
    from bigquery_agent_analytics.extractor_compilation import render_extractor_source

    plan = _bka_plan()
    src = render_extractor_source(plan)
    spec = _bka_resolved_spec()

    result = compile_extractor(
        source=src,
        module_name=_unique_module_name(),
        function_name=plan.function_name,
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
    ), f"compile failed: ast={result.ast_report.failures} smoke={result.smoke_report and (result.smoke_report.exceptions, result.smoke_report.validation_failures)}"

  def test_bka_source_matches_handwritten_extractor(
      self, tmp_path: pathlib.Path
  ):
    """The whole point: rendered BKA source produces output
    structurally identical to ``extract_bka_decision_event`` on
    the same sample events."""
    from bigquery_agent_analytics.extractor_compilation import load_callable_from_source
    from bigquery_agent_analytics.extractor_compilation import render_extractor_source
    from bigquery_agent_analytics.structured_extraction import extract_bka_decision_event

    plan = _bka_plan()
    src = render_extractor_source(plan)
    source_path = tmp_path / "bka_rendered.py"
    source_path.write_text(src, encoding="utf-8")

    compiled = load_callable_from_source(
        source_path,
        module_name=_unique_module_name(),
        function_name=plan.function_name,
    )

    for event in _sample_bka_events():
      hand = extract_bka_decision_event(event, None)
      auto = compiled(event, None)
      assert _result_signature(hand) == _result_signature(
          auto
      ), f"rendered vs hand-written diverge on event {event!r}"

  def test_wrong_event_type_returns_empty(self, tmp_path: pathlib.Path):
    """Generated extractors carry a top-of-function event_type
    guard so a plan/manifest mismatch can't silently attach one
    extractor to another extractor's events. Without the guard,
    an extractor for ``"bka_decision"`` would still emit a node
    for ``{"event_type": "wrong", "decision_id": "1"}`` if the
    key path happened to match.
    """
    from bigquery_agent_analytics.extractor_compilation import load_callable_from_source
    from bigquery_agent_analytics.extractor_compilation import render_extractor_source

    plan = _bka_plan()
    src = render_extractor_source(plan)
    source_path = tmp_path / "bka_guard.py"
    source_path.write_text(src, encoding="utf-8")
    extractor = load_callable_from_source(
        source_path,
        module_name=_unique_module_name(prefix="bka_guard_"),
        function_name=plan.function_name,
    )

    # Right event_type: produces output.
    matching = extractor(
        {
            "event_type": "bka_decision",
            "session_id": "s1",
            "span_id": "sp1",
            "content": {"decision_id": "d1"},
        },
        None,
    )
    assert len(matching.nodes) == 1

    # Wrong event_type: declines, even though the key path resolves.
    wrong = extractor(
        {
            "event_type": "tool_completed",
            "session_id": "s1",
            "span_id": "sp1",
            "content": {"decision_id": "d1"},
        },
        None,
    )
    assert wrong.nodes == []
    assert wrong.fully_handled_span_ids == set()
    assert wrong.partially_handled_span_ids == set()

    # Missing event_type: declines.
    missing = extractor(
        {
            "session_id": "s1",
            "span_id": "sp1",
            "content": {"decision_id": "d1"},
        },
        None,
    )
    assert missing.nodes == []

  def test_render_is_deterministic(self):
    """Identical plans produce byte-identical source. Important
    so the compile fingerprint stays stable across consecutive
    renders of the same plan."""
    from bigquery_agent_analytics.extractor_compilation import render_extractor_source

    a = render_extractor_source(_bka_plan())
    b = render_extractor_source(_bka_plan())
    assert a == b


# ------------------------------------------------------------------ #
# Plan-shape variations                                               #
# ------------------------------------------------------------------ #


class TestPlanShapeVariations:

  def test_plan_without_property_fields(self, tmp_path: pathlib.Path):
    """A minimal plan (key only, no optional properties) renders,
    compiles, and emits a node with only the key."""
    from bigquery_agent_analytics.extractor_compilation import compile_extractor
    from bigquery_agent_analytics.extractor_compilation import FieldMapping
    from bigquery_agent_analytics.extractor_compilation import render_extractor_source
    from bigquery_agent_analytics.extractor_compilation import ResolvedExtractorPlan
    from bigquery_agent_analytics.extractor_compilation import SpanHandlingRule

    plan = ResolvedExtractorPlan(
        event_type="bka_decision",
        target_entity_name="mako_DecisionPoint",
        function_name="extract_bka_minimal",
        key_field=FieldMapping("decision_id", ("content", "decision_id")),
        span_handling=SpanHandlingRule(),
    )
    src = render_extractor_source(plan)
    result = compile_extractor(
        source=src,
        module_name=_unique_module_name(prefix="minimal_"),
        function_name=plan.function_name,
        event_types=("bka_decision",),
        sample_events=_sample_bka_events(),
        spec=None,
        resolved_graph=_bka_resolved_spec(),
        parent_bundle_dir=tmp_path,
        fingerprint_inputs=_fingerprint_inputs(),
        template_version="v0.1",
        compiler_package_version="0.0.0",
    )
    assert result.ok is True

  def test_plan_without_span_handling(self, tmp_path: pathlib.Path):
    """When ``span_handling`` is None, the rendered extractor
    leaves both span sets empty."""
    from bigquery_agent_analytics.extractor_compilation import FieldMapping
    from bigquery_agent_analytics.extractor_compilation import load_callable_from_source
    from bigquery_agent_analytics.extractor_compilation import render_extractor_source
    from bigquery_agent_analytics.extractor_compilation import ResolvedExtractorPlan

    plan = ResolvedExtractorPlan(
        event_type="bka_decision",
        target_entity_name="mako_DecisionPoint",
        function_name="extract_no_span",
        key_field=FieldMapping("decision_id", ("content", "decision_id")),
    )
    src = render_extractor_source(plan)
    source_path = tmp_path / "no_span.py"
    source_path.write_text(src, encoding="utf-8")
    extractor = load_callable_from_source(
        source_path,
        module_name=_unique_module_name(prefix="nospan_"),
        function_name=plan.function_name,
    )

    result = extractor(
        {
            "event_type": "bka_decision",
            "session_id": "s1",
            "span_id": "sp1",
            "content": {"decision_id": "d1"},
        },
        None,
    )
    assert result.fully_handled_span_ids == set()
    assert result.partially_handled_span_ids == set()

  def test_plan_with_single_step_paths(self, tmp_path: pathlib.Path):
    """Length-1 paths (key directly on event root) render
    without any traversal guards and still compile."""
    from bigquery_agent_analytics.extractor_compilation import FieldMapping
    from bigquery_agent_analytics.extractor_compilation import load_callable_from_source
    from bigquery_agent_analytics.extractor_compilation import render_extractor_source
    from bigquery_agent_analytics.extractor_compilation import ResolvedExtractorPlan

    plan = ResolvedExtractorPlan(
        event_type="flat",
        target_entity_name="mako_DecisionPoint",
        function_name="extract_flat",
        key_field=FieldMapping("decision_id", ("decision_id",)),
        property_fields=(FieldMapping("outcome", ("outcome",)),),
    )
    src = render_extractor_source(plan)
    source_path = tmp_path / "flat.py"
    source_path.write_text(src, encoding="utf-8")
    extractor = load_callable_from_source(
        source_path,
        module_name=_unique_module_name(prefix="flat_"),
        function_name=plan.function_name,
    )

    result = extractor(
        {
            "event_type": "flat",
            "session_id": "s1",
            "decision_id": "d1",
            "outcome": "approved",
        },
        None,
    )
    assert len(result.nodes) == 1
    properties = {p.name: p.value for p in result.nodes[0].properties}
    assert properties == {"decision_id": "d1", "outcome": "approved"}

  def test_deep_optional_property_path_with_non_dict_intermediate(
      self, tmp_path: pathlib.Path
  ):
    """The renderer's docs promise wrong-shape intermediates
    resolve to "field absent" rather than crashing. Verify with
    a length-3 *optional property* path where the first
    intermediate is a string."""
    from bigquery_agent_analytics.extractor_compilation import FieldMapping
    from bigquery_agent_analytics.extractor_compilation import load_callable_from_source
    from bigquery_agent_analytics.extractor_compilation import render_extractor_source
    from bigquery_agent_analytics.extractor_compilation import ResolvedExtractorPlan

    plan = ResolvedExtractorPlan(
        event_type="deep",
        target_entity_name="mako_DecisionPoint",
        function_name="extract_deep_opt",
        key_field=FieldMapping("decision_id", ("decision_id",)),
        property_fields=(FieldMapping("outcome", ("a", "b", "outcome")),),
    )
    src = render_extractor_source(plan)
    source_path = tmp_path / "deep_opt.py"
    source_path.write_text(src, encoding="utf-8")
    extractor = load_callable_from_source(
        source_path,
        module_name=_unique_module_name(prefix="deep_opt_"),
        function_name=plan.function_name,
    )

    # event["a"] is a string, not a dict — must NOT raise.
    result = extractor(
        {"event_type": "deep", "decision_id": "d1", "a": "notadict"},
        None,
    )
    assert len(result.nodes) == 1
    properties = {p.name for p in result.nodes[0].properties}
    assert properties == {"decision_id"}, "outcome should be absent"

  def test_deep_session_id_path_with_non_dict_intermediate(
      self, tmp_path: pathlib.Path
  ):
    """Same protection on ``session_id_path``: a non-dict
    intermediate falls back to the empty-string default rather
    than crashing."""
    from bigquery_agent_analytics.extractor_compilation import FieldMapping
    from bigquery_agent_analytics.extractor_compilation import load_callable_from_source
    from bigquery_agent_analytics.extractor_compilation import render_extractor_source
    from bigquery_agent_analytics.extractor_compilation import ResolvedExtractorPlan

    plan = ResolvedExtractorPlan(
        event_type="deep",
        target_entity_name="mako_DecisionPoint",
        function_name="extract_deep_session",
        key_field=FieldMapping("decision_id", ("decision_id",)),
        session_id_path=("a", "b", "c"),
    )
    src = render_extractor_source(plan)
    source_path = tmp_path / "deep_session.py"
    source_path.write_text(src, encoding="utf-8")
    extractor = load_callable_from_source(
        source_path,
        module_name=_unique_module_name(prefix="deep_sess_"),
        function_name=plan.function_name,
    )

    result = extractor(
        {"event_type": "deep", "decision_id": "d1", "a": "notadict"},
        None,
    )
    assert len(result.nodes) == 1
    # session_id falls back to '' so the node_id starts with the
    # empty session prefix.
    assert result.nodes[0].node_id == ":mako_DecisionPoint:decision_id=d1"

  def test_deep_partial_when_path_with_non_dict_intermediate(
      self, tmp_path: pathlib.Path
  ):
    """Same protection on ``partial_when_path``: a non-dict
    intermediate resolves to None (i.e., span fully handled)
    rather than crashing."""
    from bigquery_agent_analytics.extractor_compilation import FieldMapping
    from bigquery_agent_analytics.extractor_compilation import load_callable_from_source
    from bigquery_agent_analytics.extractor_compilation import render_extractor_source
    from bigquery_agent_analytics.extractor_compilation import ResolvedExtractorPlan
    from bigquery_agent_analytics.extractor_compilation import SpanHandlingRule

    plan = ResolvedExtractorPlan(
        event_type="deep",
        target_entity_name="mako_DecisionPoint",
        function_name="extract_deep_partial",
        key_field=FieldMapping("decision_id", ("decision_id",)),
        span_handling=SpanHandlingRule(
            span_id_path=("span_id",),
            partial_when_path=("a", "b", "c"),
        ),
    )
    src = render_extractor_source(plan)
    source_path = tmp_path / "deep_partial.py"
    source_path.write_text(src, encoding="utf-8")
    extractor = load_callable_from_source(
        source_path,
        module_name=_unique_module_name(prefix="deep_partial_"),
        function_name=plan.function_name,
    )

    result = extractor(
        {
            "event_type": "deep",
            "decision_id": "d1",
            "span_id": "s1",
            "a": "notadict",
        },
        None,
    )
    assert result.fully_handled_span_ids == {"s1"}
    assert result.partially_handled_span_ids == set()

  def test_plan_with_deep_traversal_path(self, tmp_path: pathlib.Path):
    """Length-3 paths emit nested ``isinstance(..., dict)`` guards
    at every intermediate level."""
    from bigquery_agent_analytics.extractor_compilation import FieldMapping
    from bigquery_agent_analytics.extractor_compilation import load_callable_from_source
    from bigquery_agent_analytics.extractor_compilation import render_extractor_source
    from bigquery_agent_analytics.extractor_compilation import ResolvedExtractorPlan

    plan = ResolvedExtractorPlan(
        event_type="deep",
        target_entity_name="mako_DecisionPoint",
        function_name="extract_deep",
        key_field=FieldMapping(
            "decision_id", ("content", "metadata", "decision_id")
        ),
    )
    src = render_extractor_source(plan)
    source_path = tmp_path / "deep.py"
    source_path.write_text(src, encoding="utf-8")
    extractor = load_callable_from_source(
        source_path,
        module_name=_unique_module_name(prefix="deep_"),
        function_name=plan.function_name,
    )

    # Found: deep path resolves cleanly.
    found = extractor(
        {
            "event_type": "deep",
            "session_id": "s1",
            "content": {"metadata": {"decision_id": "d1"}},
        },
        None,
    )
    assert len(found.nodes) == 1
    assert found.nodes[0].node_id == "s1:mako_DecisionPoint:decision_id=d1"

    # Missing intermediate: extractor declines.
    missing = extractor(
        {
            "event_type": "deep",
            "session_id": "s1",
            "content": {"metadata": "not_a_dict"},
        },
        None,
    )
    assert missing.nodes == []
