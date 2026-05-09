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

"""Tests for runtime fallback wiring (#75 PR C2.b).

Strategy:

* Most tests construct synthetic ``compiled_extractor`` /
  ``fallback_extractor`` callables that return hand-built
  ``StructuredExtractionResult``s; the real validator runs
  against the BKA spec from PR 4b.1's fixtures.
* One end-to-end test wires the real BKA-decision compiled
  bundle as ``compiled_extractor`` and ``extract_bka_decision_event``
  as ``fallback_extractor``; both produce identical output, so
  the wrapper returns ``decision="compiled_unchanged"``.
"""

from __future__ import annotations

import pathlib
import tempfile

import pytest

# ------------------------------------------------------------------ #
# Fixtures — BKA spec for the validator                               #
# ------------------------------------------------------------------ #


def _bka_resolved_spec():
  """Reuse the centralized BKA YAML to build a ``ResolvedGraph``."""
  from bigquery_agent_analytics.resolved_spec import resolve
  from bigquery_ontology import load_binding
  from bigquery_ontology import load_ontology
  from tests.fixtures_extractor_compilation.bka_decision_inputs import BKA_BINDING_YAML
  from tests.fixtures_extractor_compilation.bka_decision_inputs import BKA_ONTOLOGY_YAML

  tmp = pathlib.Path(tempfile.mkdtemp(prefix="bka_fallback_test_"))
  (tmp / "ont.yaml").write_text(BKA_ONTOLOGY_YAML, encoding="utf-8")
  (tmp / "bnd.yaml").write_text(BKA_BINDING_YAML, encoding="utf-8")
  ontology = load_ontology(str(tmp / "ont.yaml"))
  binding = load_binding(str(tmp / "bnd.yaml"), ontology=ontology)
  return resolve(ontology, binding)


def _valid_bka_event() -> dict:
  return {
      "event_type": "bka_decision",
      "session_id": "sess1",
      "span_id": "span1",
      "content": {
          "decision_id": "d1",
          "outcome": "approved",
          "confidence": 0.92,
      },
  }


def _valid_compiled_result() -> "StructuredExtractionResult":
  """Mirror what the handwritten BKA extractor would produce for
  ``_valid_bka_event``."""
  from bigquery_agent_analytics.extracted_models import ExtractedNode
  from bigquery_agent_analytics.extracted_models import ExtractedProperty
  from bigquery_agent_analytics.structured_extraction import StructuredExtractionResult

  node = ExtractedNode(
      node_id="sess1:mako_DecisionPoint:decision_id=d1",
      entity_name="mako_DecisionPoint",
      labels=["mako_DecisionPoint"],
      properties=[
          ExtractedProperty(name="decision_id", value="d1"),
          ExtractedProperty(name="outcome", value="approved"),
          ExtractedProperty(name="confidence", value=0.92),
      ],
  )
  return StructuredExtractionResult(
      nodes=[node],
      edges=[],
      fully_handled_span_ids={"span1"},
      partially_handled_span_ids=set(),
  )


def _empty_result() -> "StructuredExtractionResult":
  from bigquery_agent_analytics.structured_extraction import StructuredExtractionResult

  return StructuredExtractionResult()


# ------------------------------------------------------------------ #
# compiled_unchanged path                                             #
# ------------------------------------------------------------------ #


class TestRunWithFallbackCompiledUnchanged:

  def test_valid_compiled_result_passes_through(self):
    from bigquery_agent_analytics.extractor_compilation import run_with_fallback

    valid_result = _valid_compiled_result()

    def compiled(event, spec):
      return valid_result

    def fallback(event, spec):
      raise AssertionError("fallback must not be called when compiled is valid")

    outcome = run_with_fallback(
        event=_valid_bka_event(),
        spec=None,
        resolved_graph=_bka_resolved_spec(),
        compiled_extractor=compiled,
        fallback_extractor=fallback,
    )

    assert outcome.decision == "compiled_unchanged"
    assert outcome.result is valid_result
    assert outcome.compiled_exception is None
    assert outcome.dropped_node_ids == ()
    assert outcome.dropped_edge_ids == ()
    assert outcome.validation_failures == ()
    # Span-handling unchanged when compiled output is valid.
    assert outcome.result.fully_handled_span_ids == {"span1"}
    assert outcome.result.partially_handled_span_ids == set()

  def test_empty_compiled_result_passes_through(self):
    """An empty result is vacuously valid — no nodes / edges to
    validate. The wrapper must not call the fallback or otherwise
    treat empty as a failure."""
    from bigquery_agent_analytics.extractor_compilation import run_with_fallback

    empty = _empty_result()

    def compiled(event, spec):
      return empty

    def fallback(event, spec):
      raise AssertionError("fallback must not be called for empty-but-valid")

    outcome = run_with_fallback(
        event=_valid_bka_event(),
        spec=None,
        resolved_graph=_bka_resolved_spec(),
        compiled_extractor=compiled,
        fallback_extractor=fallback,
    )

    assert outcome.decision == "compiled_unchanged"
    assert outcome.result is empty


# ------------------------------------------------------------------ #
# fallback_for_event triggers                                         #
# ------------------------------------------------------------------ #


class TestRunWithFallbackForEventTriggers:

  def test_compiled_raises_falls_back(self):
    from bigquery_agent_analytics.extractor_compilation import run_with_fallback

    fallback_result = _valid_compiled_result()

    def compiled(event, spec):
      raise RuntimeError("compiled exploded")

    def fallback(event, spec):
      return fallback_result

    outcome = run_with_fallback(
        event=_valid_bka_event(),
        spec=None,
        resolved_graph=_bka_resolved_spec(),
        compiled_extractor=compiled,
        fallback_extractor=fallback,
    )

    assert outcome.decision == "fallback_for_event"
    assert outcome.result is fallback_result
    assert outcome.compiled_exception == "RuntimeError: compiled exploded"
    # Validation never ran — so no validation_failures captured.
    assert outcome.validation_failures == ()

  def test_compiled_returns_wrong_type_falls_back(self):
    from bigquery_agent_analytics.extractor_compilation import run_with_fallback

    fallback_result = _valid_compiled_result()

    def compiled(event, spec):
      return {"nodes": []}  # not a StructuredExtractionResult

    def fallback(event, spec):
      return fallback_result

    outcome = run_with_fallback(
        event=_valid_bka_event(),
        spec=None,
        resolved_graph=_bka_resolved_spec(),
        compiled_extractor=compiled,
        fallback_extractor=fallback,
    )

    assert outcome.decision == "fallback_for_event"
    assert outcome.result is fallback_result
    assert outcome.compiled_exception == "WrongReturnType: dict"
    assert outcome.validation_failures == ()

  def test_compiled_returns_none_falls_back(self):
    """``None`` is a wrong-type return; must trigger fallback."""
    from bigquery_agent_analytics.extractor_compilation import run_with_fallback

    fallback_result = _valid_compiled_result()

    outcome = run_with_fallback(
        event=_valid_bka_event(),
        spec=None,
        resolved_graph=_bka_resolved_spec(),
        compiled_extractor=lambda e, s: None,
        fallback_extractor=lambda e, s: fallback_result,
    )

    assert outcome.decision == "fallback_for_event"
    assert outcome.compiled_exception == "WrongReturnType: NoneType"

  def test_event_scope_failure_falls_back(self):
    """A hand-built ``ValidationFailure`` with
    ``FallbackScope.EVENT`` must trigger whole-event fallback,
    even though #76 itself doesn't currently emit EVENT scope —
    the wrapper handles it defensively."""
    from bigquery_agent_analytics.extractor_compilation import run_with_fallback
    from bigquery_agent_analytics.graph_validation import FallbackScope
    from bigquery_agent_analytics.graph_validation import ValidationFailure
    from bigquery_agent_analytics.graph_validation import ValidationReport

    valid_result = _valid_compiled_result()
    fallback_result = _empty_result()

    # Stub the validator to return an EVENT-scope failure on the
    # otherwise-valid compiled result.
    event_failure = ValidationFailure(
        scope=FallbackScope.EVENT,
        code="hand_crafted_event_failure",
        path="<root>",
        detail="synthesized failure for test",
    )

    import bigquery_agent_analytics.extractor_compilation.runtime_fallback as runtime_fallback_mod

    real_validator = runtime_fallback_mod.validate_extracted_graph

    def fake_validator(spec, graph):
      return ValidationReport(failures=(event_failure,))

    runtime_fallback_mod.validate_extracted_graph = fake_validator
    try:
      outcome = run_with_fallback(
          event=_valid_bka_event(),
          spec=None,
          resolved_graph=_bka_resolved_spec(),
          compiled_extractor=lambda e, s: valid_result,
          fallback_extractor=lambda e, s: fallback_result,
      )
    finally:
      runtime_fallback_mod.validate_extracted_graph = real_validator

    assert outcome.decision == "fallback_for_event"
    assert outcome.result is fallback_result
    assert outcome.validation_failures == (event_failure,)
    assert outcome.dropped_node_ids == ()
    assert outcome.dropped_edge_ids == ()

  def test_unpinpointable_failure_falls_back(self):
    """A NODE/FIELD/EDGE failure with neither ``node_id`` nor
    ``edge_id`` set can't be pinpointed for selective drop.
    The defensive policy is to fall back for the whole event."""
    from bigquery_agent_analytics.extractor_compilation import run_with_fallback
    from bigquery_agent_analytics.graph_validation import FallbackScope
    from bigquery_agent_analytics.graph_validation import ValidationFailure
    from bigquery_agent_analytics.graph_validation import ValidationReport

    valid_result = _valid_compiled_result()
    fallback_result = _empty_result()

    unpinpointable = ValidationFailure(
        scope=FallbackScope.NODE,
        code="hand_crafted",
        path="nodes[0]",
        detail="node_id intentionally absent",
    )

    import bigquery_agent_analytics.extractor_compilation.runtime_fallback as runtime_fallback_mod

    real = runtime_fallback_mod.validate_extracted_graph

    def fake(spec, graph):
      return ValidationReport(failures=(unpinpointable,))

    runtime_fallback_mod.validate_extracted_graph = fake
    try:
      outcome = run_with_fallback(
          event=_valid_bka_event(),
          spec=None,
          resolved_graph=_bka_resolved_spec(),
          compiled_extractor=lambda e, s: valid_result,
          fallback_extractor=lambda e, s: fallback_result,
      )
    finally:
      runtime_fallback_mod.validate_extracted_graph = real

    assert outcome.decision == "fallback_for_event"
    assert outcome.result is fallback_result

  def test_mixed_event_and_per_element_event_wins(self):
    """If a report contains an EVENT-scope failure alongside a
    pinpointable per-element one, the EVENT scope's whole-event
    fallback must take precedence — partial filtering would
    leave the EVENT-level rejection unaddressed."""
    from bigquery_agent_analytics.extractor_compilation import run_with_fallback
    from bigquery_agent_analytics.graph_validation import FallbackScope
    from bigquery_agent_analytics.graph_validation import ValidationFailure
    from bigquery_agent_analytics.graph_validation import ValidationReport

    valid_result = _valid_compiled_result()
    fallback_result = _empty_result()

    failures = (
        ValidationFailure(
            scope=FallbackScope.EVENT,
            code="hand_crafted_event_failure",
            path="<root>",
        ),
        ValidationFailure(
            scope=FallbackScope.NODE,
            code="hand_crafted_node_failure",
            path="nodes[0]",
            node_id="sess1:mako_DecisionPoint:decision_id=d1",
        ),
    )

    import bigquery_agent_analytics.extractor_compilation.runtime_fallback as runtime_fallback_mod

    real = runtime_fallback_mod.validate_extracted_graph
    runtime_fallback_mod.validate_extracted_graph = lambda spec, graph: (
        ValidationReport(failures=failures)
    )
    try:
      outcome = run_with_fallback(
          event=_valid_bka_event(),
          spec=None,
          resolved_graph=_bka_resolved_spec(),
          compiled_extractor=lambda e, s: valid_result,
          fallback_extractor=lambda e, s: fallback_result,
      )
    finally:
      runtime_fallback_mod.validate_extracted_graph = real

    assert outcome.decision == "fallback_for_event"
    assert outcome.result is fallback_result
    assert outcome.validation_failures == failures

  def test_fallback_extractor_exception_propagates(self):
    """The wrapper does not catch fallback-extractor exceptions —
    the fallback is the trusted runtime baseline; if it raises,
    that's the existing runtime's problem."""
    from bigquery_agent_analytics.extractor_compilation import run_with_fallback

    def compiled(event, spec):
      raise RuntimeError("compiled also fails")

    def fallback(event, spec):
      raise RuntimeError("fallback fails too")

    with pytest.raises(RuntimeError, match="fallback fails too"):
      run_with_fallback(
          event=_valid_bka_event(),
          spec=None,
          resolved_graph=_bka_resolved_spec(),
          compiled_extractor=compiled,
          fallback_extractor=fallback,
      )


# ------------------------------------------------------------------ #
# compiled_filtered path — per-element drops                          #
# ------------------------------------------------------------------ #


class TestRunWithFallbackCompiledFiltered:

  def test_node_scope_failure_drops_node(self):
    """A real validator run: compiled output includes a node
    whose ``entity_name`` isn't in the BKA spec. The validator
    produces a NODE-scope ``unknown_entity`` failure with the
    ghost node's ``node_id`` set. The wrapper drops just that
    node and keeps the rest."""
    from bigquery_agent_analytics.extracted_models import ExtractedNode
    from bigquery_agent_analytics.extracted_models import ExtractedProperty
    from bigquery_agent_analytics.extractor_compilation import run_with_fallback
    from bigquery_agent_analytics.structured_extraction import StructuredExtractionResult

    good_node = ExtractedNode(
        node_id="sess1:mako_DecisionPoint:decision_id=d1",
        entity_name="mako_DecisionPoint",
        labels=["mako_DecisionPoint"],
        properties=[
            ExtractedProperty(name="decision_id", value="d1"),
        ],
    )
    bad_node = ExtractedNode(
        node_id="sess1:GhostEntity:id=g1",
        entity_name="GhostEntity",  # not in BKA spec
        labels=["GhostEntity"],
        properties=[],
    )

    compiled_result = StructuredExtractionResult(
        nodes=[good_node, bad_node],
        edges=[],
        fully_handled_span_ids={"span1"},
        partially_handled_span_ids=set(),
    )

    def compiled(event, spec):
      return compiled_result

    def fallback(event, spec):
      raise AssertionError("fallback must not be called for filterable failure")

    outcome = run_with_fallback(
        event=_valid_bka_event(),
        spec=None,
        resolved_graph=_bka_resolved_spec(),
        compiled_extractor=compiled,
        fallback_extractor=fallback,
    )

    assert outcome.decision == "compiled_filtered"
    assert outcome.dropped_node_ids == ("sess1:GhostEntity:id=g1",)
    assert outcome.dropped_edge_ids == ()
    assert len(outcome.validation_failures) >= 1
    # Filtered result keeps the good node and drops the bad one.
    surviving_ids = {n.node_id for n in outcome.result.nodes}
    assert surviving_ids == {good_node.node_id}

  def test_drop_node_orphan_cleans_referencing_edges(self):
    """When a node is dropped, any edge that referenced it
    becomes an orphan and must also be removed — otherwise the
    filtered output would have edges pointing at non-existent
    nodes (an ``unresolved_endpoint`` shape)."""
    from bigquery_agent_analytics.extracted_models import ExtractedEdge
    from bigquery_agent_analytics.extracted_models import ExtractedNode
    from bigquery_agent_analytics.extracted_models import ExtractedProperty
    from bigquery_agent_analytics.extractor_compilation import run_with_fallback
    from bigquery_agent_analytics.graph_validation import FallbackScope
    from bigquery_agent_analytics.graph_validation import ValidationFailure
    from bigquery_agent_analytics.graph_validation import ValidationReport
    from bigquery_agent_analytics.structured_extraction import StructuredExtractionResult

    bad_node = ExtractedNode(
        node_id="bad_node",
        entity_name="mako_DecisionPoint",
        labels=["mako_DecisionPoint"],
        properties=[ExtractedProperty(name="decision_id", value="d1")],
    )
    orphan_edge = ExtractedEdge(
        edge_id="edge_to_bad",
        relationship_name="rel",
        from_node_id="bad_node",
        to_node_id="some_other_node",
        properties=[],
    )

    compiled_result = StructuredExtractionResult(
        nodes=[bad_node],
        edges=[orphan_edge],
        fully_handled_span_ids={"span1"},
        partially_handled_span_ids=set(),
    )

    failure = ValidationFailure(
        scope=FallbackScope.NODE,
        code="hand_crafted_drop_node",
        path="nodes[0]",
        node_id="bad_node",
    )

    import bigquery_agent_analytics.extractor_compilation.runtime_fallback as runtime_fallback_mod

    real = runtime_fallback_mod.validate_extracted_graph
    runtime_fallback_mod.validate_extracted_graph = lambda spec, graph: (
        ValidationReport(failures=(failure,))
    )
    try:
      outcome = run_with_fallback(
          event=_valid_bka_event(),
          spec=None,
          resolved_graph=_bka_resolved_spec(),
          compiled_extractor=lambda e, s: compiled_result,
          fallback_extractor=lambda e, s: _empty_result(),
      )
    finally:
      runtime_fallback_mod.validate_extracted_graph = real

    assert outcome.decision == "compiled_filtered"
    assert outcome.dropped_node_ids == ("bad_node",)
    # The orphan edge appears in dropped_edge_ids even though
    # the validator didn't directly fail on it.
    assert outcome.dropped_edge_ids == ("edge_to_bad",)
    assert outcome.result.nodes == []
    assert outcome.result.edges == []

  def test_edge_scope_failure_drops_edge_keeps_nodes(self):
    from bigquery_agent_analytics.extracted_models import ExtractedEdge
    from bigquery_agent_analytics.extracted_models import ExtractedNode
    from bigquery_agent_analytics.extracted_models import ExtractedProperty
    from bigquery_agent_analytics.extractor_compilation import run_with_fallback
    from bigquery_agent_analytics.graph_validation import FallbackScope
    from bigquery_agent_analytics.graph_validation import ValidationFailure
    from bigquery_agent_analytics.graph_validation import ValidationReport
    from bigquery_agent_analytics.structured_extraction import StructuredExtractionResult

    good_node_a = ExtractedNode(
        node_id="node_a",
        entity_name="mako_DecisionPoint",
        labels=["mako_DecisionPoint"],
        properties=[ExtractedProperty(name="decision_id", value="da")],
    )
    good_node_b = ExtractedNode(
        node_id="node_b",
        entity_name="mako_DecisionPoint",
        labels=["mako_DecisionPoint"],
        properties=[ExtractedProperty(name="decision_id", value="db")],
    )
    bad_edge = ExtractedEdge(
        edge_id="bad_edge",
        relationship_name="rel",
        from_node_id="node_a",
        to_node_id="node_b",
        properties=[],
    )

    compiled_result = StructuredExtractionResult(
        nodes=[good_node_a, good_node_b],
        edges=[bad_edge],
        fully_handled_span_ids={"span1"},
        partially_handled_span_ids=set(),
    )

    failure = ValidationFailure(
        scope=FallbackScope.EDGE,
        code="hand_crafted_drop_edge",
        path="edges[0]",
        edge_id="bad_edge",
    )

    import bigquery_agent_analytics.extractor_compilation.runtime_fallback as runtime_fallback_mod

    real = runtime_fallback_mod.validate_extracted_graph
    runtime_fallback_mod.validate_extracted_graph = lambda spec, graph: (
        ValidationReport(failures=(failure,))
    )
    try:
      outcome = run_with_fallback(
          event=_valid_bka_event(),
          spec=None,
          resolved_graph=_bka_resolved_spec(),
          compiled_extractor=lambda e, s: compiled_result,
          fallback_extractor=lambda e, s: _empty_result(),
      )
    finally:
      runtime_fallback_mod.validate_extracted_graph = real

    assert outcome.decision == "compiled_filtered"
    assert outcome.dropped_node_ids == ()
    assert outcome.dropped_edge_ids == ("bad_edge",)
    surviving_node_ids = {n.node_id for n in outcome.result.nodes}
    assert surviving_node_ids == {"node_a", "node_b"}
    assert outcome.result.edges == []

  def test_field_scope_with_node_id_drops_whole_node(self):
    """FIELD-scope failure on a node's property drops the whole
    containing node — conservative drop-whole-element policy."""
    from bigquery_agent_analytics.extracted_models import ExtractedNode
    from bigquery_agent_analytics.extracted_models import ExtractedProperty
    from bigquery_agent_analytics.extractor_compilation import run_with_fallback
    from bigquery_agent_analytics.graph_validation import FallbackScope
    from bigquery_agent_analytics.graph_validation import ValidationFailure
    from bigquery_agent_analytics.graph_validation import ValidationReport
    from bigquery_agent_analytics.structured_extraction import StructuredExtractionResult

    bad_node = ExtractedNode(
        node_id="node_with_bad_field",
        entity_name="mako_DecisionPoint",
        labels=["mako_DecisionPoint"],
        properties=[
            ExtractedProperty(name="decision_id", value="d1"),
            ExtractedProperty(name="confidence", value="not-a-number"),
        ],
    )
    compiled_result = StructuredExtractionResult(
        nodes=[bad_node],
        edges=[],
        fully_handled_span_ids={"span1"},
        partially_handled_span_ids=set(),
    )

    failure = ValidationFailure(
        scope=FallbackScope.FIELD,
        code="hand_crafted_field_failure",
        path="nodes[0].properties[1].value",
        node_id="node_with_bad_field",
    )

    import bigquery_agent_analytics.extractor_compilation.runtime_fallback as runtime_fallback_mod

    real = runtime_fallback_mod.validate_extracted_graph
    runtime_fallback_mod.validate_extracted_graph = lambda spec, graph: (
        ValidationReport(failures=(failure,))
    )
    try:
      outcome = run_with_fallback(
          event=_valid_bka_event(),
          spec=None,
          resolved_graph=_bka_resolved_spec(),
          compiled_extractor=lambda e, s: compiled_result,
          fallback_extractor=lambda e, s: _empty_result(),
      )
    finally:
      runtime_fallback_mod.validate_extracted_graph = real

    assert outcome.decision == "compiled_filtered"
    assert outcome.dropped_node_ids == ("node_with_bad_field",)
    assert outcome.result.nodes == []


# ------------------------------------------------------------------ #
# Span-handling downgrade — load-bearing for C2.b                     #
# ------------------------------------------------------------------ #


class TestRunWithFallbackSpanDowngrade:
  """The whole point of ``compiled_filtered`` is to keep valid
  pieces while still letting AI recover the dropped pieces.
  ``fully_handled_span_ids`` means "exclude this span from
  AI.GENERATE input"; if we drop a node and leave the span fully
  handled, the AI never sees the source data for the missing
  fact. The downgrade is what makes per-element fallback real."""

  def test_compiled_filtered_downgrades_fully_handled_to_partial(self):
    """The reviewer's load-bearing test: compiled output marks
    ``span1`` as fully handled, validation drops a node, the
    filtered result must move ``span1`` to
    ``partially_handled_span_ids`` so AI still sees the span."""
    from bigquery_agent_analytics.extracted_models import ExtractedNode
    from bigquery_agent_analytics.extractor_compilation import run_with_fallback
    from bigquery_agent_analytics.structured_extraction import StructuredExtractionResult

    bad_node = ExtractedNode(
        node_id="ghost_node",
        entity_name="GhostEntity",
        labels=["GhostEntity"],
        properties=[],
    )
    compiled_result = StructuredExtractionResult(
        nodes=[bad_node],
        edges=[],
        fully_handled_span_ids={"span1"},
        partially_handled_span_ids=set(),
    )

    outcome = run_with_fallback(
        event=_valid_bka_event(),  # span_id="span1"
        spec=None,
        resolved_graph=_bka_resolved_spec(),
        compiled_extractor=lambda e, s: compiled_result,
        fallback_extractor=lambda e, s: _empty_result(),
    )

    assert outcome.decision == "compiled_filtered"
    # The downgrade: span1 left fully_handled, joined partially.
    assert "span1" not in outcome.result.fully_handled_span_ids
    assert "span1" in outcome.result.partially_handled_span_ids

  def test_compiled_filtered_with_no_event_span_id_is_a_noop_for_spans(self):
    """When the event itself has no ``span_id``, there's nothing
    to downgrade. The dropped nodes/edges still come out, but
    the span sets stay exactly as the compiled output produced
    them."""
    from bigquery_agent_analytics.extracted_models import ExtractedNode
    from bigquery_agent_analytics.extractor_compilation import run_with_fallback
    from bigquery_agent_analytics.structured_extraction import StructuredExtractionResult

    bad_node = ExtractedNode(
        node_id="ghost_node",
        entity_name="GhostEntity",
        labels=["GhostEntity"],
        properties=[],
    )
    compiled_result = StructuredExtractionResult(
        nodes=[bad_node],
        edges=[],
        fully_handled_span_ids={"some_span"},
        partially_handled_span_ids=set(),
    )

    event_without_span = {
        "event_type": "bka_decision",
        "session_id": "sess1",
        # no span_id
        "content": {"decision_id": "d1"},
    }

    outcome = run_with_fallback(
        event=event_without_span,
        spec=None,
        resolved_graph=_bka_resolved_spec(),
        compiled_extractor=lambda e, s: compiled_result,
        fallback_extractor=lambda e, s: _empty_result(),
    )

    assert outcome.decision == "compiled_filtered"
    # Span sets unchanged — there's no event span_id to downgrade.
    assert outcome.result.fully_handled_span_ids == {"some_span"}
    assert outcome.result.partially_handled_span_ids == set()


# ------------------------------------------------------------------ #
# End-to-end with real BKA bundle                                     #
# ------------------------------------------------------------------ #


class TestRunWithFallbackEndToEnd:
  """Real compiled BKA bundle as compiled_extractor + the
  handwritten ``extract_bka_decision_event`` as
  fallback_extractor. They produce identical output for the BKA
  sample events, so the wrapper returns
  ``decision="compiled_unchanged"``. Proves the wrapper plays
  nicely with the rest of Phase C."""

  def test_compiled_bka_matches_handwritten_unchanged(
      self, tmp_path: pathlib.Path
  ):
    from bigquery_agent_analytics.extractor_compilation import compile_extractor
    from bigquery_agent_analytics.extractor_compilation import load_bundle
    from bigquery_agent_analytics.extractor_compilation import parse_resolved_extractor_plan_json
    from bigquery_agent_analytics.extractor_compilation import render_extractor_source
    from bigquery_agent_analytics.extractor_compilation import run_with_fallback
    from bigquery_agent_analytics.structured_extraction import extract_bka_decision_event
    from tests.fixtures_extractor_compilation.bka_decision_inputs import BKA_FINGERPRINT_INPUTS
    from tests.fixtures_extractor_compilation.bka_decision_inputs import BKA_RESOLVED_PLAN_DICT
    from tests.fixtures_extractor_compilation.bka_decision_inputs import BKA_SAMPLE_EVENTS

    resolved_graph = _bka_resolved_spec()

    plan = parse_resolved_extractor_plan_json(BKA_RESOLVED_PLAN_DICT)
    source = render_extractor_source(plan)
    bundle_root = tmp_path / "bundles"
    compile_result = compile_extractor(
        source=source,
        module_name="bka_runtime_fallback_test",
        function_name=plan.function_name,
        event_types=(plan.event_type,),
        sample_events=BKA_SAMPLE_EVENTS,
        spec=None,
        resolved_graph=resolved_graph,
        parent_bundle_dir=bundle_root,
        fingerprint_inputs=BKA_FINGERPRINT_INPUTS,
        template_version="v0.1",
        compiler_package_version="0.0.0",
        isolation=False,
    )
    assert compile_result.ok

    loaded = load_bundle(
        compile_result.bundle_dir,
        expected_fingerprint=compile_result.manifest.fingerprint,
        expected_event_types=("bka_decision",),
    )
    assert hasattr(loaded, "extractor")
    compiled_extractor = loaded.extractor

    for event in BKA_SAMPLE_EVENTS:
      outcome = run_with_fallback(
          event=event,
          spec=None,
          resolved_graph=resolved_graph,
          compiled_extractor=compiled_extractor,
          fallback_extractor=extract_bka_decision_event,
      )
      assert outcome.decision == "compiled_unchanged", (
          f"event {event['span_id']}: expected compiled_unchanged, "
          f"got {outcome.decision} with failures="
          f"{outcome.validation_failures}"
      )
      assert outcome.dropped_node_ids == ()
      assert outcome.dropped_edge_ids == ()
      assert outcome.compiled_exception is None


# ------------------------------------------------------------------ #
# Malformed result internals (review P1 #1)                           #
# ------------------------------------------------------------------ #


class TestRunWithFallbackMalformedInternals:
  """``StructuredExtractionResult`` is a ``@dataclass`` with no
  runtime type validation, so ``StructuredExtractionResult(nodes=[{}])``
  succeeds. The wrapper only realizes the internals are wrong
  when it tries to build an ``ExtractedGraph`` from them — which
  Pydantic rejects with ``ValidationError``. The wrapper must
  catch that and fall back, otherwise the "never raises on
  compiled / validator failure" contract leaks."""

  def test_compiled_returns_result_with_dict_nodes_falls_back(self):
    """The reviewer's exact repro: a compiled extractor that
    returns ``StructuredExtractionResult(nodes=[{}])``. Building
    ``ExtractedGraph`` with that nodes list raises a Pydantic
    ``ValidationError``; the wrapper must catch it and return
    ``fallback_for_event``, not propagate."""
    from bigquery_agent_analytics.extractor_compilation import run_with_fallback
    from bigquery_agent_analytics.structured_extraction import StructuredExtractionResult

    fallback_result = _empty_result()

    def compiled(event, spec):
      return StructuredExtractionResult(nodes=[{}])

    outcome = run_with_fallback(
        event=_valid_bka_event(),
        spec=None,
        resolved_graph=_bka_resolved_spec(),
        compiled_extractor=compiled,
        fallback_extractor=lambda e, s: fallback_result,
    )

    assert outcome.decision == "fallback_for_event"
    assert outcome.result is fallback_result
    # The audit record names the failure shape so logs can route
    # malformed-internals separately from extractor exceptions.
    assert outcome.compiled_exception is not None
    assert outcome.compiled_exception.startswith("MalformedResultInternals:")
    # No ValidationReport produced — validation never returned.
    assert outcome.validation_failures == ()


# ------------------------------------------------------------------ #
# EDGE failures with both node_id and edge_id (review P1 #2)         #
# ------------------------------------------------------------------ #


class TestRunWithFallbackEdgeFailureWithBothIds:
  """#76's ``missing_endpoint_key`` populates both ``node_id``
  (the referenced endpoint id) and ``edge_id`` (the offending
  edge). Earlier, the wrapper checked ``node_id`` first via
  ``elif`` and dropped the wrong element. The fix switches on
  ``failure.scope``: EDGE always drops by ``edge_id``."""

  def test_edge_failure_with_both_ids_drops_edge_not_node(self):
    from bigquery_agent_analytics.extracted_models import ExtractedEdge
    from bigquery_agent_analytics.extracted_models import ExtractedNode
    from bigquery_agent_analytics.extracted_models import ExtractedProperty
    from bigquery_agent_analytics.extractor_compilation import run_with_fallback
    from bigquery_agent_analytics.graph_validation import FallbackScope
    from bigquery_agent_analytics.graph_validation import ValidationFailure
    from bigquery_agent_analytics.graph_validation import ValidationReport
    from bigquery_agent_analytics.structured_extraction import StructuredExtractionResult

    node_a = ExtractedNode(
        node_id="node_a",
        entity_name="mako_DecisionPoint",
        labels=["mako_DecisionPoint"],
        properties=[ExtractedProperty(name="decision_id", value="da")],
    )
    bad_edge = ExtractedEdge(
        edge_id="bad_edge",
        relationship_name="rel",
        from_node_id="node_a",
        to_node_id="node_b",
        properties=[],
    )
    compiled_result = StructuredExtractionResult(
        nodes=[node_a],
        edges=[bad_edge],
        fully_handled_span_ids={"span1"},
        partially_handled_span_ids=set(),
    )

    # Mirror #76's missing_endpoint_key shape: EDGE scope, both
    # node_id (the referenced endpoint id) AND edge_id populated.
    failure = ValidationFailure(
        scope=FallbackScope.EDGE,
        code="missing_endpoint_key",
        path="edges[0].from_node_id.<key:decision_id>",
        node_id="node_a",  # the endpoint reference
        edge_id="bad_edge",  # the offending edge
    )

    import bigquery_agent_analytics.extractor_compilation.runtime_fallback as rf

    real = rf.validate_extracted_graph
    rf.validate_extracted_graph = lambda spec, graph: ValidationReport(
        failures=(failure,)
    )
    try:
      outcome = run_with_fallback(
          event=_valid_bka_event(),
          spec=None,
          resolved_graph=_bka_resolved_spec(),
          compiled_extractor=lambda e, s: compiled_result,
          fallback_extractor=lambda e, s: _empty_result(),
      )
    finally:
      rf.validate_extracted_graph = real

    assert outcome.decision == "compiled_filtered"
    # The fix: dropped the EDGE, not the node referenced in
    # node_id. Pre-fix, the elif dropped node_a here.
    assert outcome.dropped_edge_ids == ("bad_edge",)
    assert outcome.dropped_node_ids == ()
    surviving_node_ids = {n.node_id for n in outcome.result.nodes}
    assert surviving_node_ids == {"node_a"}

  def test_node_failure_without_node_id_is_unpinpointable(self):
    """Symmetric pinpointability check: a NODE-scope failure
    that's missing ``node_id`` is unpinpointable even if
    ``edge_id`` is set (an edge id can't pinpoint a node-scope
    failure). Falls back for the event."""
    from bigquery_agent_analytics.extracted_models import ExtractedNode
    from bigquery_agent_analytics.extracted_models import ExtractedProperty
    from bigquery_agent_analytics.extractor_compilation import run_with_fallback
    from bigquery_agent_analytics.graph_validation import FallbackScope
    from bigquery_agent_analytics.graph_validation import ValidationFailure
    from bigquery_agent_analytics.graph_validation import ValidationReport
    from bigquery_agent_analytics.structured_extraction import StructuredExtractionResult

    compiled_result = StructuredExtractionResult(
        nodes=[
            ExtractedNode(
                node_id="some_node",
                entity_name="mako_DecisionPoint",
                labels=["mako_DecisionPoint"],
                properties=[ExtractedProperty(name="decision_id", value="d1")],
            )
        ],
        edges=[],
        fully_handled_span_ids=set(),
        partially_handled_span_ids=set(),
    )

    failure = ValidationFailure(
        scope=FallbackScope.NODE,
        code="hand_crafted",
        path="nodes[0]",
        node_id=None,  # NODE scope without node_id is unpinpointable
        edge_id="some_edge",  # edge_id doesn't help here
    )

    import bigquery_agent_analytics.extractor_compilation.runtime_fallback as rf

    real = rf.validate_extracted_graph
    rf.validate_extracted_graph = lambda spec, graph: ValidationReport(
        failures=(failure,)
    )
    try:
      outcome = run_with_fallback(
          event=_valid_bka_event(),
          spec=None,
          resolved_graph=_bka_resolved_spec(),
          compiled_extractor=lambda e, s: compiled_result,
          fallback_extractor=lambda e, s: _empty_result(),
      )
    finally:
      rf.validate_extracted_graph = real

    assert outcome.decision == "fallback_for_event"


# ------------------------------------------------------------------ #
# SystemExit / KeyboardInterrupt (review P2)                          #
# ------------------------------------------------------------------ #


class TestRunWithFallbackSystemExit:
  """``SystemExit`` is a ``BaseException`` subclass. A bundle's
  compiled extractor calling ``sys.exit()`` at runtime would
  otherwise tear down the runtime; the wrapper catches it and
  treats it as a fallback signal — same shape C2.a's loader
  uses at import time. ``KeyboardInterrupt`` is *not* caught
  so operator cancellation still works."""

  def test_compiled_calls_sys_exit_falls_back(self):
    from bigquery_agent_analytics.extractor_compilation import run_with_fallback

    fallback_result = _empty_result()

    def compiled(event, spec):
      raise SystemExit("compiled bundle decided to exit")

    outcome = run_with_fallback(
        event=_valid_bka_event(),
        spec=None,
        resolved_graph=_bka_resolved_spec(),
        compiled_extractor=compiled,
        fallback_extractor=lambda e, s: fallback_result,
    )

    assert outcome.decision == "fallback_for_event"
    assert outcome.result is fallback_result
    assert outcome.compiled_exception is not None
    assert outcome.compiled_exception.startswith("SystemExit:")

  def test_compiled_keyboard_interrupt_propagates(self):
    """KeyboardInterrupt must NOT be caught — operator
    cancellation has to remain functional."""
    from bigquery_agent_analytics.extractor_compilation import run_with_fallback

    def compiled(event, spec):
      raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
      run_with_fallback(
          event=_valid_bka_event(),
          spec=None,
          resolved_graph=_bka_resolved_spec(),
          compiled_extractor=compiled,
          fallback_extractor=lambda e, s: _empty_result(),
      )


# ------------------------------------------------------------------ #
# Span-set shape validation (review P1)                               #
# ------------------------------------------------------------------ #


class TestRunWithFallbackSpanSetShape:
  """``StructuredExtractionResult`` is a ``@dataclass``; its
  ``fully_handled_span_ids`` / ``partially_handled_span_ids``
  fields are declared ``set[str]`` but the dataclass enforces
  nothing at runtime. Bad shapes either silently leak downstream
  via ``compiled_unchanged`` (where existing runtime code
  expects iterables of strings) or break the
  ``compiled_filtered`` path's ``set(...)`` coercion at
  span-handling-downgrade time.

  The wrapper now validates the shape up front and routes
  malformed span containers to ``fallback_for_event`` with
  ``compiled_exception`` starting ``"MalformedResultInternals:"``.
  """

  def _bad_span_compiled(self, **field_overrides):
    """Construct a compiled extractor returning a result with
    one or both span-handling fields swapped to a malformed
    value. Other fields default to a clean valid shape."""
    from bigquery_agent_analytics.structured_extraction import StructuredExtractionResult

    fields: dict = {
        "nodes": [],
        "edges": [],
        "fully_handled_span_ids": set(),
        "partially_handled_span_ids": set(),
    }
    fields.update(field_overrides)
    bad = StructuredExtractionResult(**fields)

    def compiled(event, spec):
      return bad

    return compiled

  def _run_with_bad_span(self, **field_overrides):
    from bigquery_agent_analytics.extractor_compilation import run_with_fallback

    return run_with_fallback(
        event=_valid_bka_event(),
        spec=None,
        resolved_graph=_bka_resolved_spec(),
        compiled_extractor=self._bad_span_compiled(**field_overrides),
        fallback_extractor=lambda e, s: _empty_result(),
    )

  def test_fully_handled_none_falls_back(self):
    """Reviewer's repro #1: ``fully_handled_span_ids=None``.
    Pre-fix this returned ``compiled_unchanged`` with ``None``
    leaking downstream."""
    outcome = self._run_with_bad_span(fully_handled_span_ids=None)
    assert outcome.decision == "fallback_for_event"
    assert outcome.compiled_exception is not None
    assert outcome.compiled_exception.startswith("MalformedResultInternals:")
    assert "fully_handled_span_ids" in outcome.compiled_exception

  def test_partially_handled_string_falls_back(self):
    """Reviewer's repro #2: ``partially_handled_span_ids="span1"``.
    Strings are iterable, so without an explicit type check the
    set-coercion path would corrupt this into
    ``{"s", "p", "a", "n", "1"}``."""
    outcome = self._run_with_bad_span(partially_handled_span_ids="span1")
    assert outcome.decision == "fallback_for_event"
    assert outcome.compiled_exception is not None
    assert outcome.compiled_exception.startswith("MalformedResultInternals:")
    assert "partially_handled_span_ids" in outcome.compiled_exception

  def test_list_instead_of_set_falls_back(self):
    """List has the right element types but the wrong container
    type — the dataclass field declares ``set``."""
    outcome = self._run_with_bad_span(fully_handled_span_ids=["sp1", "sp2"])
    assert outcome.decision == "fallback_for_event"
    assert "MalformedResultInternals" in outcome.compiled_exception

  def test_set_with_non_string_item_falls_back(self):
    outcome = self._run_with_bad_span(fully_handled_span_ids={"sp1", 42})
    assert outcome.decision == "fallback_for_event"
    assert "non-string entry" in outcome.compiled_exception

  def test_set_with_empty_string_item_falls_back(self):
    outcome = self._run_with_bad_span(fully_handled_span_ids={"sp1", ""})
    assert outcome.decision == "fallback_for_event"
    assert "empty-string entry" in outcome.compiled_exception

  def test_frozenset_accepted(self):
    """``frozenset`` is a valid container — the type annotation
    is ``set[str]`` but immutable variants are equivalent for
    membership / iteration / union."""
    outcome = self._run_with_bad_span(
        fully_handled_span_ids=frozenset({"span1"})
    )
    assert outcome.decision == "compiled_unchanged"

  def test_filtered_path_safe_when_validation_drops_node_with_clean_spans(self):
    """Reviewer's repro #3 covered: if validation fails AND a
    span container is malformed, the filtered path's
    ``set(compiled_result.fully_handled_span_ids)`` would raise.
    With span-set validation up front, the loader falls back
    *before* that path runs. This test pins the no-crash
    behavior by setting up a NODE-failure scenario AND
    ``fully_handled_span_ids=None`` simultaneously."""
    from bigquery_agent_analytics.extracted_models import ExtractedNode
    from bigquery_agent_analytics.extractor_compilation import run_with_fallback
    from bigquery_agent_analytics.structured_extraction import StructuredExtractionResult

    bad_node = ExtractedNode(
        node_id="ghost",
        entity_name="GhostEntity",
        labels=["GhostEntity"],
        properties=[],
    )
    # Build by-passing ``__init__`` semantics: the dataclass
    # accepts ``None`` for the span field.
    compiled_result = StructuredExtractionResult(
        nodes=[bad_node],
        edges=[],
        fully_handled_span_ids=None,
        partially_handled_span_ids=set(),
    )

    outcome = run_with_fallback(
        event=_valid_bka_event(),
        spec=None,
        resolved_graph=_bka_resolved_spec(),
        compiled_extractor=lambda e, s: compiled_result,
        fallback_extractor=lambda e, s: _empty_result(),
    )

    # Span-set validation runs before the validator, so the
    # outcome is fallback_for_event with MalformedResultInternals
    # — *not* a TypeError raised from the filtered path.
    assert outcome.decision == "fallback_for_event"
    assert outcome.compiled_exception.startswith("MalformedResultInternals:")
