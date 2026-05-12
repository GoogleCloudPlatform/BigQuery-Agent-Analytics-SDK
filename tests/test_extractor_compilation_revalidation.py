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

"""Tests for the revalidation harness (#75 PR C2.d).

Covers both dimensions C2.d aggregates:

* **Runtime decision** (from ``run_with_fallback``):
  ``compiled_unchanged`` / ``compiled_filtered`` /
  ``fallback_for_event``, plus the ``compiled_path_faults``
  subset that distinguishes bundle bugs from ontology drift.
* **Agreement against reference**: ``parity_match`` /
  ``parity_divergence`` / ``parity_not_checked``. The
  schema-valid-but-semantically-wrong case is the load-
  bearing one — without parity it would silently aggregate
  as ``compiled_unchanged``.

Plus threshold checks (including rate-bounds validation),
reference-exception safety, and audit-shape coverage
(skipped events, malformed events, JSON determinism,
sample-divergence caps).
"""

from __future__ import annotations

import json
import pathlib
import tempfile

import pytest

# ------------------------------------------------------------------ #
# Fixture helpers                                                      #
# ------------------------------------------------------------------ #


def _bka_resolved_spec():
  from bigquery_agent_analytics.resolved_spec import resolve
  from bigquery_ontology import load_binding
  from bigquery_ontology import load_ontology
  from tests.fixtures_extractor_compilation.bka_decision_inputs import BKA_BINDING_YAML
  from tests.fixtures_extractor_compilation.bka_decision_inputs import BKA_ONTOLOGY_YAML

  tmp = pathlib.Path(tempfile.mkdtemp(prefix="bka_reval_test_"))
  (tmp / "ont.yaml").write_text(BKA_ONTOLOGY_YAML, encoding="utf-8")
  (tmp / "bnd.yaml").write_text(BKA_BINDING_YAML, encoding="utf-8")
  ontology = load_ontology(str(tmp / "ont.yaml"))
  binding = load_binding(str(tmp / "bnd.yaml"), ontology=ontology)
  return resolve(ontology, binding)


def _bka_event(*, span_id: str, decision_id: str = "d1") -> dict:
  return {
      "event_type": "bka_decision",
      "session_id": "sess1",
      "span_id": span_id,
      "content": {
          "decision_id": decision_id,
          "outcome": "approved",
          "confidence": 0.9,
      },
  }


# ------------------------------------------------------------------ #
# Happy path: every event compiles cleanly                            #
# ------------------------------------------------------------------ #


class TestRevalidationHappyPath:

  def test_all_compiled_unchanged_and_parity_matches(self):
    """Real BKA fixtures: handwritten extractor used as both
    'compiled' and 'reference'. Every event lands as
    ``compiled_unchanged`` (schema validates) AND
    ``parity_match`` (output equals reference)."""
    from bigquery_agent_analytics.extractor_compilation import revalidate_compiled_extractors
    from bigquery_agent_analytics.structured_extraction import extract_bka_decision_event

    compiled = extract_bka_decision_event
    reference = extract_bka_decision_event

    events = [
        _bka_event(span_id="sp1"),
        _bka_event(span_id="sp2"),
        _bka_event(span_id="sp3"),
    ]

    report = revalidate_compiled_extractors(
        events=events,
        compiled_extractors={"bka_decision": compiled},
        reference_extractors={"bka_decision": reference},
        resolved_graph=_bka_resolved_spec(),
    )

    assert report.total_events == 3
    assert report.skipped_events == 0
    assert report.total_compiled_unchanged == 3
    assert report.total_compiled_filtered == 0
    assert report.total_fallback_for_event == 0
    assert report.total_compiled_path_faults == 0
    assert report.sample_decision_divergences == ()
    assert report.compiled_unchanged_rate == 1.0

    # Parity dimension agrees: matches on all 3 events.
    assert report.total_parity_matches == 3
    assert report.total_parity_divergences == 0
    assert report.total_parity_not_checked == 0
    assert report.parity_match_rate == 1.0
    assert report.sample_parity_divergences == ()

    bka_counts = report.counts_by_event_type["bka_decision"]
    assert bka_counts.event_type == "bka_decision"
    assert bka_counts.total == 3
    assert bka_counts.compiled_unchanged == 3
    assert bka_counts.compiled_unchanged_rate == 1.0
    assert bka_counts.parity_matches == 3
    assert bka_counts.parity_match_rate == 1.0


# ------------------------------------------------------------------ #
# Parity: schema-valid but semantically wrong                          #
# ------------------------------------------------------------------ #


class TestRevalidationParity:

  def test_schema_valid_wrong_output_caught_by_parity(self):
    """The P1 blocker reproducer: a compiled extractor that
    emits a BKA decision node with the **wrong decision_id**.
    The output is schema-valid (entity_name / labels match the
    ontology, property names match), so the validator returns
    no failures and the runtime decision is
    ``compiled_unchanged``. Without parity, the run would look
    perfect. WITH parity (the C2.d agreement check), the
    divergence surfaces as ``parity_divergence`` and the
    ``parity_match_rate`` drops."""
    from bigquery_agent_analytics.extracted_models import ExtractedNode
    from bigquery_agent_analytics.extracted_models import ExtractedProperty
    from bigquery_agent_analytics.extractor_compilation import revalidate_compiled_extractors
    from bigquery_agent_analytics.structured_extraction import extract_bka_decision_event
    from bigquery_agent_analytics.structured_extraction import StructuredExtractionResult

    def wrong_decision_id_compiled(event, spec):
      # Same shape as the handwritten reference would produce
      # (entity_name + labels are exactly what
      # ``extract_bka_decision_event`` emits, so the schema
      # validator accepts it), but with a *wrong* decision_id.
      # Reference emits "d1" (from event["content"]); this
      # emits "WRONG_VALUE".
      sess = event["session_id"]
      node = ExtractedNode(
          node_id=f"{sess}:mako_DecisionPoint:decision_id=WRONG_VALUE",
          entity_name="mako_DecisionPoint",
          labels=["mako_DecisionPoint"],
          properties=[
              ExtractedProperty(name="decision_id", value="WRONG_VALUE"),
              ExtractedProperty(name="outcome", value="approved"),
              ExtractedProperty(name="confidence", value=0.9),
          ],
      )
      return StructuredExtractionResult(
          nodes=[node],
          edges=[],
          fully_handled_span_ids={event["span_id"]},
          partially_handled_span_ids=set(),
      )

    events = [
        _bka_event(span_id="sp1"),
        _bka_event(span_id="sp2"),
    ]

    report = revalidate_compiled_extractors(
        events=events,
        compiled_extractors={"bka_decision": wrong_decision_id_compiled},
        reference_extractors={"bka_decision": extract_bka_decision_event},
        resolved_graph=_bka_resolved_spec(),
    )

    # Schema dimension says everything's fine.
    assert report.total_events == 2
    assert report.total_compiled_unchanged == 2
    assert report.total_compiled_filtered == 0
    assert report.total_fallback_for_event == 0
    assert report.compiled_unchanged_rate == 1.0

    # Parity dimension catches the drift.
    assert report.total_parity_matches == 0
    assert report.total_parity_divergences == 2
    assert report.total_parity_not_checked == 0
    assert report.parity_match_rate == 0.0
    # Sample-divergence list names the parity failures
    # so an operator can drill in.
    assert len(report.sample_parity_divergences) == 2
    for divergence in report.sample_parity_divergences:
      assert divergence.startswith("bka_decision:")

  def test_parity_not_checked_for_fallback_for_event(self):
    """When the wrapper falls back for the whole event (e.g.
    compiled extractor crashed), the compiled output never
    reaches downstream — parity is recorded as
    ``parity_not_checked`` and excluded from the parity-match
    denominator. Otherwise a noisy bundle would tank the
    parity_match_rate via events the wrapper already filtered
    out for safety."""
    from bigquery_agent_analytics.extractor_compilation import revalidate_compiled_extractors
    from bigquery_agent_analytics.structured_extraction import extract_bka_decision_event

    def crashing_compiled(event, spec):
      raise RuntimeError("compiled bundle exploded")

    report = revalidate_compiled_extractors(
        events=[_bka_event(span_id="sp1"), _bka_event(span_id="sp2")],
        compiled_extractors={"bka_decision": crashing_compiled},
        reference_extractors={"bka_decision": extract_bka_decision_event},
        resolved_graph=_bka_resolved_spec(),
    )

    assert report.total_fallback_for_event == 2
    assert report.total_parity_matches == 0
    assert report.total_parity_divergences == 0
    assert report.total_parity_not_checked == 2
    # Rate is 0 over empty denominator, not NaN or division
    # error.
    assert report.parity_match_rate == 0.0

  def test_reference_exception_recorded_as_parity_divergence(self):
    """A reference extractor that crashes on one event must
    NOT abort the batch (P2 #2). The crash is recorded as a
    parity divergence with the exception type + message; the
    remaining events keep getting revalidated."""
    from bigquery_agent_analytics.extractor_compilation import revalidate_compiled_extractors
    from bigquery_agent_analytics.structured_extraction import extract_bka_decision_event

    call_counter = {"n": 0}

    def flaky_reference(event, spec):
      call_counter["n"] += 1
      if call_counter["n"] == 1:
        raise ValueError("reference blew up on sp1")
      return extract_bka_decision_event(event, spec)

    events = [
        _bka_event(span_id="sp1"),
        _bka_event(span_id="sp2"),
    ]

    report = revalidate_compiled_extractors(
        events=events,
        compiled_extractors={"bka_decision": extract_bka_decision_event},
        reference_extractors={"bka_decision": flaky_reference},
        resolved_graph=_bka_resolved_spec(),
    )

    # Both events were processed — the batch didn't abort.
    assert report.total_events == 2
    assert report.total_compiled_unchanged == 2

    # First event: reference crashed → parity divergence.
    # Second event: reference returned, output matched →
    # parity match.
    assert report.total_parity_divergences == 1
    assert report.total_parity_matches == 1

    # Sample-divergence captured the exception type + message.
    assert len(report.sample_parity_divergences) == 1
    assert "ValueError" in report.sample_parity_divergences[0]
    assert "reference blew up" in report.sample_parity_divergences[0]

  def test_edge_drift_caught_by_parity(self):
    """Edge-emitting extractor whose endpoints disagree with
    the reference. Without edge parity the divergence would be
    invisible — the node sets match, so ``_compare_nodes`` and
    ``_compare_span_handling`` both return ``None``. With edge
    parity (this round's P1 fix) the wrong endpoint surfaces
    as a divergence."""
    from bigquery_agent_analytics.extracted_models import ExtractedEdge
    from bigquery_agent_analytics.extracted_models import ExtractedNode
    from bigquery_agent_analytics.extractor_compilation import revalidate_compiled_extractors
    from bigquery_agent_analytics.structured_extraction import StructuredExtractionResult

    def _two_nodes_with_edge(*, span_id: str, to_node_id: str):
      a = ExtractedNode(
          node_id="N1", entity_name="X", labels=["X"], properties=[]
      )
      b = ExtractedNode(
          node_id="N2", entity_name="X", labels=["X"], properties=[]
      )
      edge = ExtractedEdge(
          edge_id="E1",
          relationship_name="rel",
          from_node_id="N1",
          to_node_id=to_node_id,
          properties=[],
      )
      return StructuredExtractionResult(
          nodes=[a, b],
          edges=[edge],
          fully_handled_span_ids={span_id},
          partially_handled_span_ids=set(),
      )

    def compiled(event, spec):
      # Edge points to N2 in reference, but to N1 in compiled.
      return _two_nodes_with_edge(span_id=event["span_id"], to_node_id="N1")

    def reference(event, spec):
      return _two_nodes_with_edge(span_id=event["span_id"], to_node_id="N2")

    # Stub the validator: this synthetic graph isn't in the
    # BKA spec, but we want to focus on parity. Acceptance of
    # any compiled output keeps ``compiled_unchanged`` so the
    # parity check actually runs.
    import bigquery_agent_analytics.extractor_compilation.runtime_fallback as rf
    from bigquery_agent_analytics.graph_validation import ValidationReport

    real_validator = rf.validate_extracted_graph
    rf.validate_extracted_graph = lambda spec, graph: ValidationReport(
        failures=()
    )
    try:
      report = revalidate_compiled_extractors(
          events=[_bka_event(span_id="sp1")],
          compiled_extractors={"bka_decision": compiled},
          reference_extractors={"bka_decision": reference},
          resolved_graph=_bka_resolved_spec(),
      )
    finally:
      rf.validate_extracted_graph = real_validator

    assert report.total_compiled_unchanged == 1
    assert report.total_parity_matches == 0
    assert report.total_parity_divergences == 1
    # Sample-divergence names the offending edge field so an
    # operator can drill in.
    assert "to_node_id" in report.sample_parity_divergences[0]

  def test_duplicate_edge_id_caught_by_parity(self):
    """Compiled extractor emits two ExtractedEdge values
    sharing the same ``edge_id``. Without an explicit
    duplicate check, the dict-keyed comparator would collapse
    them and a coincidental shape-match against the
    reference's single edge would look like
    ``parity_match``. #76's validator catches
    ``duplicate_node_id`` but not ``duplicate_edge_id``, so
    the protection has to live in ``_compare_edges``."""
    from bigquery_agent_analytics.extracted_models import ExtractedEdge
    from bigquery_agent_analytics.extracted_models import ExtractedNode
    from bigquery_agent_analytics.extractor_compilation import revalidate_compiled_extractors
    from bigquery_agent_analytics.structured_extraction import StructuredExtractionResult

    nodes = [
        ExtractedNode(
            node_id="N1", entity_name="X", labels=["X"], properties=[]
        ),
        ExtractedNode(
            node_id="N2", entity_name="X", labels=["X"], properties=[]
        ),
    ]

    def compiled(event, spec):
      # Two edges sharing edge_id "E1" — the dict-keyed
      # comparator would silently take only the last one.
      e_a = ExtractedEdge(
          edge_id="E1",
          relationship_name="rel",
          from_node_id="N1",
          to_node_id="N2",
          properties=[],
      )
      e_b = ExtractedEdge(
          edge_id="E1",
          relationship_name="rel",
          from_node_id="N2",
          to_node_id="N1",
          properties=[],
      )
      return StructuredExtractionResult(
          nodes=nodes,
          edges=[e_a, e_b],
          fully_handled_span_ids={event["span_id"]},
          partially_handled_span_ids=set(),
      )

    def reference(event, spec):
      # Single edge matching e_b's endpoints — without the
      # duplicate check the comparison would succeed (e_b
      # overwrites e_a in the dict).
      edge = ExtractedEdge(
          edge_id="E1",
          relationship_name="rel",
          from_node_id="N2",
          to_node_id="N1",
          properties=[],
      )
      return StructuredExtractionResult(
          nodes=nodes,
          edges=[edge],
          fully_handled_span_ids={event["span_id"]},
          partially_handled_span_ids=set(),
      )

    import bigquery_agent_analytics.extractor_compilation.runtime_fallback as rf
    from bigquery_agent_analytics.graph_validation import ValidationReport

    real_validator = rf.validate_extracted_graph
    rf.validate_extracted_graph = lambda spec, graph: ValidationReport(
        failures=()
    )
    try:
      report = revalidate_compiled_extractors(
          events=[_bka_event(span_id="sp1")],
          compiled_extractors={"bka_decision": compiled},
          reference_extractors={"bka_decision": reference},
          resolved_graph=_bka_resolved_spec(),
      )
    finally:
      rf.validate_extracted_graph = real_validator

    assert report.total_compiled_unchanged == 1
    assert report.total_parity_matches == 0
    assert report.total_parity_divergences == 1
    # Divergence string names the duplicate edge_id and which
    # side has the duplicate.
    divergence = report.sample_parity_divergences[0]
    assert "duplicate edge_id" in divergence
    assert "compiled duplicates" in divergence
    assert "E1" in divergence

  def test_reference_duplicate_node_id_caught_by_parity(self):
    """Reference extractor emits two ExtractedNode values
    sharing the same ``node_id``. ``_compare_nodes`` keys
    nodes by ``node_id`` via ``{n.node_id: n for n in ...}``
    and would silently collapse the duplicates — a malformed
    reference whose last duplicate happens to match the
    compiled output would otherwise report ``parity_match``.
    #76's validator catches duplicate_node_id on the compiled
    side, but reference output isn't validated, so the local
    guard in ``_check_parity`` has to cover it."""
    from bigquery_agent_analytics.extracted_models import ExtractedNode
    from bigquery_agent_analytics.extractor_compilation import revalidate_compiled_extractors
    from bigquery_agent_analytics.structured_extraction import StructuredExtractionResult

    def compiled(event, spec):
      node = ExtractedNode(
          node_id="N1",
          entity_name="X",
          labels=["X"],
          properties=[],
      )
      return StructuredExtractionResult(
          nodes=[node],
          edges=[],
          fully_handled_span_ids={event["span_id"]},
          partially_handled_span_ids=set(),
      )

    def reference(event, spec):
      # Two nodes sharing node_id "N1" — the dict-keyed
      # comparator would silently take only the last one,
      # which matches compiled's single node.
      a = ExtractedNode(
          node_id="N1",
          entity_name="WrongEntity",
          labels=["WrongEntity"],
          properties=[],
      )
      b = ExtractedNode(
          node_id="N1",
          entity_name="X",
          labels=["X"],
          properties=[],
      )
      return StructuredExtractionResult(
          nodes=[a, b],
          edges=[],
          fully_handled_span_ids={event["span_id"]},
          partially_handled_span_ids=set(),
      )

    import bigquery_agent_analytics.extractor_compilation.runtime_fallback as rf
    from bigquery_agent_analytics.graph_validation import ValidationReport

    real_validator = rf.validate_extracted_graph
    rf.validate_extracted_graph = lambda spec, graph: ValidationReport(
        failures=()
    )
    try:
      report = revalidate_compiled_extractors(
          events=[_bka_event(span_id="sp1")],
          compiled_extractors={"bka_decision": compiled},
          reference_extractors={"bka_decision": reference},
          resolved_graph=_bka_resolved_spec(),
      )
    finally:
      rf.validate_extracted_graph = real_validator

    assert report.total_compiled_unchanged == 1
    assert report.total_parity_matches == 0
    assert report.total_parity_divergences == 1
    divergence = report.sample_parity_divergences[0]
    assert "duplicate node_id" in divergence
    assert "reference duplicates" in divergence
    assert "N1" in divergence

  def test_reference_returns_none_recorded_as_parity_divergence(self):
    """A reference that returns ``None`` (rather than raising)
    must NOT abort the batch with ``AttributeError`` on
    ``.nodes``. Recorded as a parity divergence naming the
    wrong return type."""
    from bigquery_agent_analytics.extractor_compilation import revalidate_compiled_extractors
    from bigquery_agent_analytics.structured_extraction import extract_bka_decision_event

    def none_returning_reference(event, spec):
      return None

    report = revalidate_compiled_extractors(
        events=[_bka_event(span_id="sp1"), _bka_event(span_id="sp2")],
        compiled_extractors={"bka_decision": extract_bka_decision_event},
        reference_extractors={"bka_decision": none_returning_reference},
        resolved_graph=_bka_resolved_spec(),
    )

    # Both events processed; both flagged as parity
    # divergences naming the wrong return type.
    assert report.total_events == 2
    assert report.total_parity_divergences == 2
    assert report.total_parity_matches == 0
    for divergence in report.sample_parity_divergences:
      assert "NoneType" in divergence
      assert "StructuredExtractionResult" in divergence

  def test_comparator_exception_recorded_as_parity_divergence(self):
    """If the comparator itself raises (e.g. a malformed
    internal field bypasses the isinstance check and trips
    the comparator's iteration), the crash must NOT abort
    the batch. Recorded as a parity divergence naming the
    comparator that exploded."""
    from bigquery_agent_analytics.extractor_compilation import revalidate_compiled_extractors
    import bigquery_agent_analytics.extractor_compilation.revalidation as reval
    from bigquery_agent_analytics.structured_extraction import extract_bka_decision_event

    real_compare_nodes = reval._compare_nodes

    def exploding_compare_nodes(ref_nodes, cmp_nodes):
      raise RuntimeError("comparator boom")

    reval._compare_nodes = exploding_compare_nodes
    try:
      report = revalidate_compiled_extractors(
          events=[_bka_event(span_id="sp1")],
          compiled_extractors={"bka_decision": extract_bka_decision_event},
          reference_extractors={"bka_decision": extract_bka_decision_event},
          resolved_graph=_bka_resolved_spec(),
      )
    finally:
      reval._compare_nodes = real_compare_nodes

    assert report.total_events == 1
    assert report.total_parity_divergences == 1
    assert "parity comparator raised" in report.sample_parity_divergences[0]
    assert "RuntimeError" in report.sample_parity_divergences[0]


# ------------------------------------------------------------------ #
# Drift: compiled output diverges from the validator                  #
# ------------------------------------------------------------------ #


class TestRevalidationDrift:

  def test_compiled_filtered_drift_surfaces_in_both_dimensions(self):
    """The compiled extractor emits a node with an
    ``entity_name`` the BKA spec doesn't know about — the
    validator drops the node (decision: ``compiled_filtered``)
    AND the filtered output disagrees with the reference's
    real BKA decision node (parity: ``parity_divergence``).
    Both signals are populated."""
    from bigquery_agent_analytics.extracted_models import ExtractedNode
    from bigquery_agent_analytics.extractor_compilation import revalidate_compiled_extractors
    from bigquery_agent_analytics.structured_extraction import extract_bka_decision_event
    from bigquery_agent_analytics.structured_extraction import StructuredExtractionResult

    def drifted_compiled(event, spec):
      bad_node = ExtractedNode(
          node_id=f"{event['session_id']}:Ghost:id=g1",
          entity_name="GhostEntity",  # not in spec
          labels=["GhostEntity"],
          properties=[],
      )
      return StructuredExtractionResult(
          nodes=[bad_node],
          edges=[],
          fully_handled_span_ids={event["span_id"]},
          partially_handled_span_ids=set(),
      )

    events = [
        _bka_event(span_id="sp1"),
        _bka_event(span_id="sp2"),
    ]

    report = revalidate_compiled_extractors(
        events=events,
        compiled_extractors={"bka_decision": drifted_compiled},
        reference_extractors={"bka_decision": extract_bka_decision_event},
        resolved_graph=_bka_resolved_spec(),
    )

    # Decision dimension.
    assert report.total_events == 2
    assert report.total_compiled_unchanged == 0
    assert report.total_compiled_filtered == 2
    assert report.total_fallback_for_event == 0
    assert report.total_compiled_path_faults == 0
    assert len(report.sample_decision_divergences) == 2
    for divergence in report.sample_decision_divergences:
      assert divergence.startswith("bka_decision: compiled_filtered")

    # Parity dimension.
    assert report.total_parity_matches == 0
    assert report.total_parity_divergences == 2
    assert report.total_parity_not_checked == 0


# ------------------------------------------------------------------ #
# Exception: compiled extractor crashes                              #
# ------------------------------------------------------------------ #


class TestRevalidationCompiledException:

  def test_compiled_exception_falls_back_and_counts_path_faults(self):
    """A compiled extractor that raises on every event lands
    as ``fallback_for_event`` with the ``compiled_exception``
    field set on the underlying outcome. The report counts
    those as ``compiled_path_faults`` separately so operators
    can distinguish bundle bugs from ontology drift."""
    from bigquery_agent_analytics.extractor_compilation import revalidate_compiled_extractors
    from bigquery_agent_analytics.structured_extraction import extract_bka_decision_event

    def crashing_compiled(event, spec):
      raise RuntimeError("compiled bundle exploded")

    events = [
        _bka_event(span_id="sp1"),
        _bka_event(span_id="sp2"),
    ]

    report = revalidate_compiled_extractors(
        events=events,
        compiled_extractors={"bka_decision": crashing_compiled},
        reference_extractors={"bka_decision": extract_bka_decision_event},
        resolved_graph=_bka_resolved_spec(),
    )

    assert report.total_events == 2
    assert report.total_fallback_for_event == 2
    # All fallbacks were path-fault-driven.
    assert report.total_compiled_path_faults == 2
    assert report.total_compiled_unchanged == 0
    assert report.total_compiled_filtered == 0
    assert len(report.sample_decision_divergences) == 2
    for divergence in report.sample_decision_divergences:
      assert "RuntimeError" in divergence
      assert "compiled bundle exploded" in divergence

  def test_validator_driven_fallback_does_not_count_as_path_fault(self):
    """A ``fallback_for_event`` triggered by an EVENT-scope
    validator failure (not an exception) lands in
    ``total_fallback_for_event`` but NOT in
    ``total_compiled_path_faults``. The two counters are
    separate so operators can distinguish bundle-bug from
    ontology-drift."""
    from bigquery_agent_analytics.extractor_compilation import revalidate_compiled_extractors
    from bigquery_agent_analytics.graph_validation import FallbackScope
    from bigquery_agent_analytics.graph_validation import ValidationFailure
    from bigquery_agent_analytics.graph_validation import ValidationReport
    from bigquery_agent_analytics.structured_extraction import extract_bka_decision_event
    from bigquery_agent_analytics.structured_extraction import StructuredExtractionResult

    def benign_compiled(event, spec):
      return StructuredExtractionResult()

    import bigquery_agent_analytics.extractor_compilation.runtime_fallback as rf

    real_validator = rf.validate_extracted_graph

    def fake_validator(spec, graph):
      return ValidationReport(
          failures=(
              ValidationFailure(
                  scope=FallbackScope.EVENT,
                  code="hand_crafted",
                  path="<root>",
              ),
          )
      )

    rf.validate_extracted_graph = fake_validator
    try:
      report = revalidate_compiled_extractors(
          events=[_bka_event(span_id="sp1")],
          compiled_extractors={"bka_decision": benign_compiled},
          reference_extractors={"bka_decision": extract_bka_decision_event},
          resolved_graph=_bka_resolved_spec(),
      )
    finally:
      rf.validate_extracted_graph = real_validator

    assert report.total_fallback_for_event == 1
    assert report.total_compiled_path_faults == 0


# ------------------------------------------------------------------ #
# Threshold checks                                                    #
# ------------------------------------------------------------------ #


class TestRevalidationThresholds:

  def _build_report_with_drift(self):
    """Helper: 4 events, 1 compiled_unchanged, 3 compiled_filtered →
    25% unchanged rate, 75% filtered rate."""
    from bigquery_agent_analytics.extracted_models import ExtractedNode
    from bigquery_agent_analytics.extractor_compilation import revalidate_compiled_extractors
    from bigquery_agent_analytics.structured_extraction import extract_bka_decision_event
    from bigquery_agent_analytics.structured_extraction import StructuredExtractionResult

    call_counter = {"n": 0}

    def maybe_drifted(event, spec):
      call_counter["n"] += 1
      if call_counter["n"] == 1:
        return extract_bka_decision_event(event, spec)
      bad_node = ExtractedNode(
          node_id="ghost", entity_name="GhostEntity", labels=[], properties=[]
      )
      return StructuredExtractionResult(
          nodes=[bad_node],
          edges=[],
          fully_handled_span_ids={event["span_id"]},
          partially_handled_span_ids=set(),
      )

    events = [_bka_event(span_id=f"sp{i}") for i in range(1, 5)]

    return revalidate_compiled_extractors(
        events=events,
        compiled_extractors={"bka_decision": maybe_drifted},
        reference_extractors={"bka_decision": extract_bka_decision_event},
        resolved_graph=_bka_resolved_spec(),
    )

  def test_threshold_fails_when_unchanged_rate_below_min(self):
    """The flagship gate: a revalidation run with 25%
    unchanged-rate fails a ``min_compiled_unchanged_rate=0.95``
    threshold. Violations name the rate and the bound so
    operators can see *why* the gate tripped."""
    from bigquery_agent_analytics.extractor_compilation import check_thresholds
    from bigquery_agent_analytics.extractor_compilation import RevalidationThresholds

    report = self._build_report_with_drift()
    assert report.compiled_unchanged_rate == 0.25  # sanity

    result = check_thresholds(
        report, RevalidationThresholds(min_compiled_unchanged_rate=0.95)
    )

    assert result.ok is False
    assert len(result.violations) == 1
    assert "compiled_unchanged_rate" in result.violations[0]
    assert "0.2500" in result.violations[0]
    assert "0.9500" in result.violations[0]

  def test_threshold_passes_when_no_thresholds_set(self):
    """Unset thresholds are ignored. An empty
    ``RevalidationThresholds`` always passes regardless of
    report contents."""
    from bigquery_agent_analytics.extractor_compilation import check_thresholds
    from bigquery_agent_analytics.extractor_compilation import RevalidationThresholds

    report = self._build_report_with_drift()
    result = check_thresholds(report, RevalidationThresholds())
    assert result.ok is True
    assert result.violations == ()

  def test_multiple_thresholds_all_evaluated(self):
    """Several thresholds tripped at once → all violations
    surface in the result, not just the first."""
    from bigquery_agent_analytics.extractor_compilation import check_thresholds
    from bigquery_agent_analytics.extractor_compilation import RevalidationThresholds

    report = self._build_report_with_drift()
    result = check_thresholds(
        report,
        RevalidationThresholds(
            min_compiled_unchanged_rate=0.95,
            max_compiled_filtered_rate=0.10,
            max_fallback_for_event_rate=0.50,
            # parity_match_rate is 0/3=0.0 here (3 filtered
            # events all diverged from reference; the 1
            # compiled_unchanged matched). With 3 divergences
            # and 1 match, rate is 1/4 = 0.25 < 0.95.
            min_parity_match_rate=0.95,
        ),
    )

    assert result.ok is False
    # 3 violations: unchanged, filtered, parity. fallback
    # threshold passes (rate is 0).
    assert len(result.violations) == 3
    assert any("compiled_unchanged_rate" in v for v in result.violations)
    assert any("compiled_filtered_rate" in v for v in result.violations)
    assert any("parity_match_rate" in v for v in result.violations)

  def test_threshold_rate_out_of_range_rejected(self):
    """``RevalidationThresholds`` enforces ``[0, 1]`` at
    construction. A typo like ``max_fallback_for_event_rate=5``
    (intended as 5%) must fail loudly instead of silently
    disabling the gate (no observed rate can ever exceed 5)."""
    from bigquery_agent_analytics.extractor_compilation import RevalidationThresholds

    with pytest.raises(ValueError, match=r"max_fallback_for_event_rate"):
      RevalidationThresholds(max_fallback_for_event_rate=5.0)
    with pytest.raises(ValueError, match=r"min_compiled_unchanged_rate"):
      RevalidationThresholds(min_compiled_unchanged_rate=-0.1)
    with pytest.raises(ValueError, match=r"max_compiled_filtered_rate"):
      RevalidationThresholds(max_compiled_filtered_rate=1.5)
    # NaN must also be rejected — every comparison with NaN
    # returns False, so a NaN threshold silently passes
    # every report.
    with pytest.raises(ValueError, match=r"min_parity_match_rate"):
      RevalidationThresholds(min_parity_match_rate=float("nan"))
    # Bool must be rejected even though it's a numeric
    # subclass in Python: ``True == 1.0`` makes
    # ``min_compiled_unchanged_rate=True`` look like 100%
    # which is almost certainly not the caller's intent.
    with pytest.raises(ValueError, match=r"min_compiled_unchanged_rate"):
      RevalidationThresholds(min_compiled_unchanged_rate=True)

  def test_threshold_rate_boundary_values_accepted(self):
    """0.0 and 1.0 are the boundary values for valid rates;
    both must be accepted. (Otherwise a gate at exactly
    ``min_compiled_unchanged_rate=1.0`` couldn't be
    expressed.)"""
    from bigquery_agent_analytics.extractor_compilation import RevalidationThresholds

    # Should not raise.
    RevalidationThresholds(
        min_compiled_unchanged_rate=0.0,
        max_compiled_filtered_rate=1.0,
        max_fallback_for_event_rate=0.0,
        max_compiled_path_fault_rate=1.0,
        min_parity_match_rate=1.0,
    )


# ------------------------------------------------------------------ #
# Audit-shape: skipped events, JSON, sample cap                       #
# ------------------------------------------------------------------ #


class TestRevalidationAuditShape:

  def test_skipped_events_when_no_compiled_or_reference(self):
    """Events whose event_type isn't in ``compiled_extractors``
    or ``reference_extractors`` are skipped (no compiled path
    to revalidate). They're counted in ``skipped_events`` but
    don't enter the rate denominators."""
    from bigquery_agent_analytics.extractor_compilation import revalidate_compiled_extractors
    from bigquery_agent_analytics.structured_extraction import extract_bka_decision_event

    report = revalidate_compiled_extractors(
        events=[
            _bka_event(span_id="sp1"),
            {"event_type": "uncovered", "span_id": "spU"},
            {"event_type": "bka_decision", "span_id": "spX"},
        ],
        compiled_extractors={"bka_decision": extract_bka_decision_event},
        reference_extractors={"bka_decision": extract_bka_decision_event},
        resolved_graph=_bka_resolved_spec(),
    )

    assert report.total_events == 2
    assert report.skipped_events == 1

  def test_malformed_event_skipped(self):
    """An event that isn't a dict (or lacks an event_type)
    can't be revalidated. Counted as skipped, not as a
    failure."""
    from bigquery_agent_analytics.extractor_compilation import revalidate_compiled_extractors
    from bigquery_agent_analytics.structured_extraction import extract_bka_decision_event

    report = revalidate_compiled_extractors(
        events=[
            "not a dict",
            {"no_event_type_field": True},
            {"event_type": ""},
            _bka_event(span_id="sp1"),
        ],
        compiled_extractors={"bka_decision": extract_bka_decision_event},
        reference_extractors={"bka_decision": extract_bka_decision_event},
        resolved_graph=_bka_resolved_spec(),
    )

    assert report.total_events == 1
    assert report.skipped_events == 3

  def test_report_to_json_is_deterministic(self):
    """JSON serialization sorts keys so two reports with
    identical contents produce byte-identical JSON. Important
    if reports get persisted and diffed across runs."""
    from bigquery_agent_analytics.extractor_compilation import revalidate_compiled_extractors
    from bigquery_agent_analytics.structured_extraction import extract_bka_decision_event

    events = [_bka_event(span_id="sp1")]
    report = revalidate_compiled_extractors(
        events=events,
        compiled_extractors={"bka_decision": extract_bka_decision_event},
        reference_extractors={"bka_decision": extract_bka_decision_event},
        resolved_graph=_bka_resolved_spec(),
    )

    encoded = report.to_json()
    parsed = json.loads(encoded)
    # Top-level keys are sorted.
    assert list(parsed.keys()) == sorted(parsed.keys())
    # Both dimensions land in the JSON.
    assert parsed["total_events"] == 1
    assert parsed["total_compiled_unchanged"] == 1
    assert parsed["total_parity_matches"] == 1
    assert parsed["total_parity_divergences"] == 0
    assert parsed["counts_by_event_type"]["bka_decision"]["total"] == 1
    assert parsed["counts_by_event_type"]["bka_decision"]["parity_matches"] == 1

  def test_sample_decision_divergence_cap_respected(self):
    """The decision-divergence list never exceeds the cap,
    even if many events drift."""
    from bigquery_agent_analytics.extracted_models import ExtractedNode
    from bigquery_agent_analytics.extractor_compilation import revalidate_compiled_extractors
    from bigquery_agent_analytics.structured_extraction import extract_bka_decision_event
    from bigquery_agent_analytics.structured_extraction import StructuredExtractionResult

    def always_drift(event, spec):
      return StructuredExtractionResult(
          nodes=[
              ExtractedNode(
                  node_id="ghost",
                  entity_name="GhostEntity",
                  labels=[],
                  properties=[],
              )
          ],
          edges=[],
          fully_handled_span_ids={event["span_id"]},
          partially_handled_span_ids=set(),
      )

    events = [_bka_event(span_id=f"sp{i}") for i in range(20)]
    report = revalidate_compiled_extractors(
        events=events,
        compiled_extractors={"bka_decision": always_drift},
        reference_extractors={"bka_decision": extract_bka_decision_event},
        resolved_graph=_bka_resolved_spec(),
        sample_divergence_cap=5,
    )

    assert report.total_events == 20
    assert report.total_compiled_filtered == 20
    assert len(report.sample_decision_divergences) == 5
    # Parity-divergence cap applies independently — the
    # filtered output (empty after node drop) disagrees with
    # the reference's real output on every event, so the
    # parity-divergence list is also at cap.
    assert len(report.sample_parity_divergences) == 5
