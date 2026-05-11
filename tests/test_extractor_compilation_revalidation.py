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

Covers four scenarios per the working plan:

* Deterministic BKA happy path — every event lands as
  ``compiled_unchanged`` and rates aggregate correctly.
* Drift case — the compiled extractor's output diverges from
  the reference's; the validator catches it and the report
  shows ``compiled_filtered`` counts and a sample-divergence
  entry.
* Compiled exception case — the compiled extractor raises on
  some events; the report shows ``fallback_for_event`` plus a
  separate ``compiled_exceptions`` count.
* Threshold-fails case — a threshold-check on a report with
  drift trips the ``min_compiled_unchanged_rate`` gate.

Plus a few audit-shape tests (skipped-events accounting, JSON
round-trip, sample-divergence cap).
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


def _empty_result():
  from bigquery_agent_analytics.structured_extraction import StructuredExtractionResult

  return StructuredExtractionResult()


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

  def test_all_compiled_unchanged(self):
    """Real BKA fixtures: handwritten extractor and a synthetic
    'compiled' extractor that returns the same shape. Every
    event lands as ``compiled_unchanged`` and the report's
    aggregate counts add up."""
    from bigquery_agent_analytics.extractor_compilation import revalidate_compiled_extractors
    from bigquery_agent_analytics.structured_extraction import extract_bka_decision_event

    # The compiled extractor returns the same output as the
    # handwritten reference. Validation passes, decision is
    # always compiled_unchanged.
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
    assert report.total_compiled_exceptions == 0
    assert report.sample_divergences == ()
    assert report.compiled_unchanged_rate == 1.0

    # Per-event-type breakdown is also populated.
    bka_counts = report.counts_by_event_type["bka_decision"]
    assert bka_counts.event_type == "bka_decision"
    assert bka_counts.total == 3
    assert bka_counts.compiled_unchanged == 3
    assert bka_counts.compiled_unchanged_rate == 1.0


# ------------------------------------------------------------------ #
# Drift: compiled output diverges from the validator                  #
# ------------------------------------------------------------------ #


class TestRevalidationDrift:

  def test_compiled_filtered_drift_surfaces_in_report(self):
    """The compiled extractor emits a node with an
    ``entity_name`` the BKA spec doesn't know about — the
    validator drops the node, the wrapper's decision is
    ``compiled_filtered``, and the report shows the count plus
    a sample-divergence entry."""
    from bigquery_agent_analytics.extracted_models import ExtractedNode
    from bigquery_agent_analytics.extractor_compilation import revalidate_compiled_extractors
    from bigquery_agent_analytics.structured_extraction import extract_bka_decision_event
    from bigquery_agent_analytics.structured_extraction import StructuredExtractionResult

    def drifted_compiled(event, spec):
      # Emit a node whose entity_name isn't in the BKA spec.
      # The validator catches this as NODE-scope
      # ``unknown_entity`` and the wrapper drops the node →
      # compiled_filtered.
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

    assert report.total_events == 2
    assert report.total_compiled_unchanged == 0
    assert report.total_compiled_filtered == 2
    assert report.total_fallback_for_event == 0
    assert report.total_compiled_exceptions == 0
    # Sample divergences captured the drift.
    assert len(report.sample_divergences) == 2
    for divergence in report.sample_divergences:
      assert divergence.startswith("bka_decision: compiled_filtered")


# ------------------------------------------------------------------ #
# Exception: compiled extractor crashes                              #
# ------------------------------------------------------------------ #


class TestRevalidationCompiledException:

  def test_compiled_exception_falls_back_and_counts(self):
    """A compiled extractor that raises on every event lands
    as ``fallback_for_event`` with the ``compiled_exception``
    field set. The report counts those separately from
    validator-driven fallbacks."""
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
    # All fallbacks were exception-driven.
    assert report.total_compiled_exceptions == 2
    assert report.total_compiled_unchanged == 0
    assert report.total_compiled_filtered == 0
    # Sample divergences name the exception type + message.
    assert len(report.sample_divergences) == 2
    for divergence in report.sample_divergences:
      assert "RuntimeError" in divergence
      assert "compiled bundle exploded" in divergence

  def test_validator_driven_fallback_does_not_count_as_exception(self):
    """A ``fallback_for_event`` triggered by an EVENT-scope
    validator failure (not an exception) lands in
    ``total_fallback_for_event`` but NOT in
    ``total_compiled_exceptions``. The two counters are
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

    # Stub the validator to return one EVENT-scope failure per
    # call → wrapper falls back for the whole event.
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
    assert report.total_compiled_exceptions == 0


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
        # First call: clean output (matches reference).
        return extract_bka_decision_event(event, spec)
      # Remaining calls: drift.
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
            # fallback_for_event_rate is 0, so this one passes.
            max_fallback_for_event_rate=0.50,
        ),
    )

    assert result.ok is False
    assert len(result.violations) == 2
    assert any("compiled_unchanged_rate" in v for v in result.violations)
    assert any("compiled_filtered_rate" in v for v in result.violations)


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

    # Two BKA events were revalidated; one was skipped.
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
    # All declared fields land in the JSON.
    assert parsed["total_events"] == 1
    assert parsed["total_compiled_unchanged"] == 1
    assert parsed["counts_by_event_type"]["bka_decision"]["total"] == 1

  def test_sample_divergence_cap_respected(self):
    """The divergence list never exceeds the cap, even if many
    events drift."""
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
    assert len(report.sample_divergences) == 5
