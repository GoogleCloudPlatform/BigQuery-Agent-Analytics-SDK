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

"""Tests for PR B1: ``extract_graph`` orthogonal-flag refactor +
diagnostics emission (issue #178).

Covers:

* Legacy bool surface (``extract_graph(session_ids, True/False)``)
  preserves the existing extraction semantics and emits no
  diagnostics. ``ExtractedGraph.model_dump()`` does pick up an
  additive ``'diagnostics': []`` key — see the model surface for
  the precise compatibility contract.
* New orthogonal surface (``run_structured`` + ``on_unhandled_span``)
  emits each of the five diagnostic codes on a code path that
  produces it.
* ``capture_extractor_exceptions=False`` (the default on
  ``run_structured_extractors``) propagates extractor exceptions
  — the documented contract that ``runtime_fallback`` /
  ``runtime_registry`` rely on.
* ``capture_extractor_exceptions=True`` catches exceptions and
  records them in ``StructuredExtractionResult.exceptions`` so the
  diagnostics path can emit ``extractor_exception`` codes.
* Incoherent flag combinations raise ``ValueError`` at the
  dispatcher boundary, before any BigQuery work.
"""

from __future__ import annotations

from unittest import mock

import pytest

from bigquery_agent_analytics.extracted_models import ExtractedEdge
from bigquery_agent_analytics.extracted_models import ExtractedGraph
from bigquery_agent_analytics.extracted_models import ExtractedNode
from bigquery_agent_analytics.extracted_models import ExtractionDiagnostic
from bigquery_agent_analytics.structured_extraction import ExtractorException
from bigquery_agent_analytics.structured_extraction import run_structured_extractors
from bigquery_agent_analytics.structured_extraction import StructuredExtractionResult

# ---------------------------------------------------------------- #
# run_structured_extractors: capture_extractor_exceptions contract  #
# ---------------------------------------------------------------- #


class _ToySpec:
  name = "toy"


def _ok_extractor(event: dict, spec) -> StructuredExtractionResult:
  span_id = event.get("span_id", "default-span")
  return StructuredExtractionResult(
      nodes=[ExtractedNode(node_id="n1", entity_name="E")],
      fully_handled_span_ids={span_id},
  )


def _raising_extractor(event: dict, spec) -> StructuredExtractionResult:
  raise RuntimeError("synthetic extractor failure")


class TestRunStructuredExtractorsExceptionContract:
  """The default propagate-exception contract must hold — that's
  what ``runtime_fallback.run_with_fallback`` and
  ``runtime_registry.WrappedRegistry`` rely on. The diagnostics path
  is the only opt-in to exception capture."""

  def test_default_propagates_exception(self):
    """Existing contract: extractor exceptions propagate. Changing
    the default would silently break ``runtime_fallback`` /
    ``runtime_registry``."""
    events = [
        {"event_type": "BOOM", "span_id": "s1"},
    ]
    extractors = {"BOOM": _raising_extractor}
    with pytest.raises(RuntimeError, match="synthetic extractor failure"):
      run_structured_extractors(events, extractors, _ToySpec())

  def test_explicit_false_propagates_exception(self):
    """The explicit ``capture_extractor_exceptions=False`` path
    behaves exactly the same as the default."""
    events = [{"event_type": "BOOM", "span_id": "s1"}]
    extractors = {"BOOM": _raising_extractor}
    with pytest.raises(RuntimeError, match="synthetic extractor failure"):
      run_structured_extractors(
          events, extractors, _ToySpec(), capture_extractor_exceptions=False
      )

  def test_capture_records_exception_and_continues(self):
    """When capturing is on, the exception lands in
    ``StructuredExtractionResult.exceptions`` (not propagated) and
    other extractors continue running so partial results still
    surface."""
    events = [
        {"event_type": "OK", "span_id": "s-ok"},
        {"event_type": "BOOM", "span_id": "s-boom"},
        {"event_type": "OK", "span_id": "s-ok-2"},
    ]
    extractors = {"OK": _ok_extractor, "BOOM": _raising_extractor}
    result = run_structured_extractors(
        events, extractors, _ToySpec(), capture_extractor_exceptions=True
    )
    # Partial results from the OK extractor still surface.
    assert len(result.nodes) >= 1
    assert "s-ok" in result.fully_handled_span_ids
    assert "s-ok-2" in result.fully_handled_span_ids
    # The exception is recorded with the right attribution.
    assert len(result.exceptions) == 1
    exc = result.exceptions[0]
    assert exc.span_id == "s-boom"
    assert exc.event_type == "BOOM"
    assert "RuntimeError" in exc.detail
    assert "synthetic extractor failure" in exc.detail


# ---------------------------------------------------------------- #
# extract_graph dispatcher: legacy back-compat                      #
# ---------------------------------------------------------------- #


def _build_manager(
    *,
    extractors=None,
    raw_events=None,
    ai_graph=None,
    payloads_graph=None,
    spec_name="toy",
):
  """Build a minimal OntologyGraphManager mock that exercises the
  ``extract_graph`` dispatch path without touching BigQuery."""
  from bigquery_agent_analytics.ontology_graph import OntologyGraphManager

  mgr = OntologyGraphManager.__new__(OntologyGraphManager)
  mgr.extractors = extractors or {}
  mgr.spec = mock.Mock()
  mgr.spec.name = spec_name
  mgr._fetch_raw_events = mock.Mock(return_value=raw_events or [])
  mgr._extract_via_ai_generate = mock.Mock(
      return_value=ai_graph
      if ai_graph is not None
      else ExtractedGraph(name=spec_name)
  )
  mgr._extract_payloads = mock.Mock(
      return_value=payloads_graph
      if payloads_graph is not None
      else ExtractedGraph(name=spec_name)
  )
  return mgr


class TestLegacyBoolSurface:
  """``extract_graph(session_ids, True)`` and
  ``extract_graph(session_ids, False)`` must continue to work
  positionally and emit no diagnostics — the back-compat contract
  every existing caller depends on."""

  def test_legacy_true_runs_structured_and_ai(self):
    structured = ExtractedGraph(
        name="toy",
        nodes=[ExtractedNode(node_id="ai-node", entity_name="E")],
    )
    mgr = _build_manager(
        extractors={"E": _ok_extractor},
        raw_events=[{"event_type": "E", "span_id": "s1"}],
        ai_graph=structured,
    )
    result = mgr.extract_graph(["sess-1"], True)
    assert mgr._extract_via_ai_generate.called
    assert mgr._extract_payloads.called is False
    assert (
        result.diagnostics == []
    ), "legacy bool surface must NOT emit diagnostics — back-compat"

  def test_legacy_false_runs_stub_only(self):
    mgr = _build_manager(
        extractors={"E": _ok_extractor},
        payloads_graph=ExtractedGraph(
            name="toy",
            nodes=[ExtractedNode(node_id="stub", entity_name="E")],
        ),
    )
    result = mgr.extract_graph(["sess-1"], False)
    assert mgr._extract_via_ai_generate.called is False
    assert mgr._extract_payloads.called
    assert result.diagnostics == []
    assert result.nodes[0].node_id == "stub"

  def test_legacy_positional_call_still_parses(self):
    """``extract_graph(session_ids, False)`` is a legitimate
    positional call shape used by existing callers. Making the new
    params keyword-only preserves it."""
    mgr = _build_manager()
    # If the signature broke, this would TypeError immediately.
    result = mgr.extract_graph(["s"], False)
    assert isinstance(result, ExtractedGraph)


# ---------------------------------------------------------------- #
# extract_graph dispatcher: validation                              #
# ---------------------------------------------------------------- #


class TestOrthogonalFlagValidation:
  """The new params must be set together, and the combo must be
  coherent. Validation runs at the dispatcher boundary before any
  BigQuery work."""

  def test_run_structured_alone_raises(self):
    mgr = _build_manager()
    with pytest.raises(ValueError, match="must be set together"):
      mgr.extract_graph(["s"], run_structured=True, on_unhandled_span=None)

  def test_on_unhandled_alone_raises(self):
    mgr = _build_manager()
    with pytest.raises(ValueError, match="must be set together"):
      mgr.extract_graph(
          ["s"], run_structured=None, on_unhandled_span="ai_fallback"
      )

  def test_ai_fallback_with_ai_off_raises(self):
    mgr = _build_manager()
    with pytest.raises(
        ValueError,
        match="ai_fallback.*requires use_ai_generate=True",
    ):
      mgr.extract_graph(
          ["s"],
          use_ai_generate=False,
          run_structured=True,
          on_unhandled_span="ai_fallback",
      )

  def test_validation_runs_before_any_bigquery_work(self):
    """``_fetch_raw_events`` / ``_extract_via_ai_generate`` /
    ``_extract_payloads`` must NOT be called when the dispatcher
    rejects the inputs."""
    mgr = _build_manager()
    with pytest.raises(ValueError):
      mgr.extract_graph(["s"], run_structured=True, on_unhandled_span=None)
    assert mgr._fetch_raw_events.called is False
    assert mgr._extract_via_ai_generate.called is False
    assert mgr._extract_payloads.called is False


# ---------------------------------------------------------------- #
# extract_graph dispatcher: diagnostics emission                    #
# ---------------------------------------------------------------- #


class TestDiagnosticEmission:
  """Each diagnostic code is emitted on the code path that produces
  it. The legacy bool surface emits none of them."""

  def _events_two_handled_one_unhandled(self):
    return [
        {"event_type": "E", "span_id": "s-full"},
        {"event_type": "E", "span_id": "s-partial"},
        {"event_type": "UNKNOWN", "span_id": "s-unhandled"},
    ]

  def _extractor_two_codes(self, event, spec):
    # Emits one fully_handled + one partially_handled to exercise
    # both diagnostic codes from one extractor invocation set.
    span_id = event["span_id"]
    if span_id == "s-full":
      return StructuredExtractionResult(
          nodes=[ExtractedNode(node_id="n-full", entity_name="E")],
          fully_handled_span_ids={span_id},
      )
    if span_id == "s-partial":
      return StructuredExtractionResult(
          nodes=[ExtractedNode(node_id="n-partial", entity_name="E")],
          partially_handled_span_ids={span_id},
      )
    return StructuredExtractionResult()

  def test_emits_structured_fully_and_partially_handled(self):
    mgr = _build_manager(
        extractors={"E": self._extractor_two_codes},
        raw_events=self._events_two_handled_one_unhandled(),
    )
    result = mgr.extract_graph(
        ["sess-1"],
        use_ai_generate=True,
        run_structured=True,
        on_unhandled_span="ai_fallback",
    )
    codes = {(d.diagnostic_code, d.span_id) for d in result.diagnostics}
    assert ("structured_fully_handled", "s-full") in codes
    assert ("structured_partially_handled", "s-partial") in codes

  def test_emits_structured_unhandled(self):
    mgr = _build_manager(
        extractors={"E": self._extractor_two_codes},
        raw_events=self._events_two_handled_one_unhandled(),
    )
    result = mgr.extract_graph(
        ["sess-1"],
        run_structured=True,
        use_ai_generate=True,
        on_unhandled_span="ai_fallback",
    )
    unhandled = [
        d
        for d in result.diagnostics
        if d.diagnostic_code == "structured_unhandled"
    ]
    assert len(unhandled) == 1
    assert unhandled[0].span_id == "s-unhandled"
    assert unhandled[0].event_type == "UNKNOWN", (
        "structured_unhandled must carry the event_type so operators "
        "can grep Cloud Logging for the offending shape without "
        "joining the diagnostic back to agent_events"
    )

  def test_emits_extractor_exception(self):
    """When ``on_unhandled_span`` is set and the diagnostics path
    runs, extractor exceptions are caught and recorded — the
    materializer will translate these to typed
    ``empty_extraction`` failures (PR B2)."""
    mgr = _build_manager(
        extractors={"BOOM": _raising_extractor},
        raw_events=[{"event_type": "BOOM", "span_id": "s-boom"}],
    )
    result = mgr.extract_graph(
        ["sess-1"],
        use_ai_generate=False,
        run_structured=True,
        on_unhandled_span="fail",
    )
    exc_diags = [
        d
        for d in result.diagnostics
        if d.diagnostic_code == "extractor_exception"
    ]
    assert len(exc_diags) == 1
    assert exc_diags[0].span_id == "s-boom"
    assert exc_diags[0].event_type == "BOOM"
    assert "RuntimeError" in (exc_diags[0].detail or "")

  def test_emits_session_ai_fallback_attempted(self):
    """The session-level signal is per-session (not per-span),
    because AI.GENERATE returns a graph and per-span AI
    attribution isn't mechanically knowable today."""
    mgr = _build_manager()
    result = mgr.extract_graph(
        ["sess-a", "sess-b"],
        run_structured=True,
        use_ai_generate=True,
        on_unhandled_span="ai_fallback",
    )
    sessions = {
        d.session_id
        for d in result.diagnostics
        if d.diagnostic_code == "session_ai_fallback_attempted"
    }
    assert sessions == {"sess-a", "sess-b"}

  def test_fail_mode_skips_ai_and_skips_stub(self):
    """``on_unhandled_span='fail'`` is compiled-only mode: neither
    AI nor the stub-payload path runs. The materializer reads the
    structured_unhandled diagnostics to flip ``ok=false``."""
    mgr = _build_manager(
        extractors={"E": _ok_extractor},
        raw_events=[
            {"event_type": "E", "span_id": "s1"},
            {"event_type": "UNKNOWN", "span_id": "s-gap"},
        ],
    )
    result = mgr.extract_graph(
        ["sess-1"],
        use_ai_generate=False,
        run_structured=True,
        on_unhandled_span="fail",
    )
    assert mgr._extract_via_ai_generate.called is False
    assert mgr._extract_payloads.called is False
    # No session_ai_fallback_attempted diagnostics either — AI
    # wasn't attempted.
    assert not any(
        d.diagnostic_code == "session_ai_fallback_attempted"
        for d in result.diagnostics
    )
    # The unhandled span surfaces in the diagnostic stream.
    assert any(
        d.diagnostic_code == "structured_unhandled" and d.span_id == "s-gap"
        for d in result.diagnostics
    )


# ---------------------------------------------------------------- #
# Diagnostic model surface                                          #
# ---------------------------------------------------------------- #


class TestExtractionDiagnosticModel:
  """Pydantic surface — default_factory, optional fields, code enum."""

  def test_default_factory_list_for_diagnostics_field(self):
    """Following ``nodes`` / ``edges`` pattern — Field(default_factory=list)
    avoids the mutable-default ambiguity flagged in the v3 review."""
    g = ExtractedGraph(name="x")
    assert g.diagnostics == []
    # Mutation does not leak across instances.
    g.diagnostics.append(
        ExtractionDiagnostic(
            diagnostic_code="structured_unhandled", span_id="s"
        )
    )
    g2 = ExtractedGraph(name="y")
    assert g2.diagnostics == []

  def test_diagnostic_optional_fields_default_to_none(self):
    d = ExtractionDiagnostic(diagnostic_code="session_ai_fallback_attempted")
    assert d.span_id is None
    assert d.session_id is None
    assert d.event_type is None
    assert d.detail is None

  def test_model_dump_includes_diagnostics_field(self):
    """Documenting the serialization-shape change from PR #189
    review (P2): ``ExtractedGraph.model_dump()`` now includes
    ``'diagnostics': []`` even when the caller never opted into
    the diagnostics-emitting path. Existing JSON consumers that
    ignore unknown keys (the standard pattern) keep working; only
    strict-shape consumers need to add a passthrough."""
    g = ExtractedGraph(name="x")
    dumped = g.model_dump()
    assert "diagnostics" in dumped
    assert dumped["diagnostics"] == []


# ---------------------------------------------------------------- #
# structured_unhandled precise semantics (PR #189 review P2)        #
# ---------------------------------------------------------------- #


class TestStructuredUnhandledPreciseSemantics:
  """``structured_unhandled`` means "no registered extractor was
  invoked for this span" — NOT "an extractor matched but produced
  empty output." The distinction matters because:

  * A recognized event whose content is missing a required field
    (e.g. ``reference_extractor._extract_capture_context`` when
    ``context_id`` is absent) returns an empty
    ``StructuredExtractionResult()``. That's a legitimate silent
    outcome.
  * B2's compiled-only failure semantics will translate
    ``structured_unhandled`` to typed ``empty_extraction`` failures
    that flip ``ok=false``. Counting the silent-outcome case as
    "unhandled" would page on-call for events the SDK handled
    correctly."""

  def _empty_extractor(self, event, spec):
    """Recognized event-type, empty result — mirrors the
    reference extractor's missing-required-field path."""
    return StructuredExtractionResult()

  def test_matched_extractor_returning_empty_is_not_unhandled(self):
    """An extractor that matches by event_type and returns empty
    does NOT contribute a ``structured_unhandled`` diagnostic.
    The orchestrator's ``invoked_span_ids`` set records the
    invocation; ``structured_unhandled`` filters against that
    set."""
    mgr = _build_manager(
        extractors={"E": self._empty_extractor},
        raw_events=[
            {"event_type": "E", "span_id": "s-empty-output"},
            {"event_type": "UNKNOWN", "span_id": "s-truly-unhandled"},
        ],
    )
    result = mgr.extract_graph(
        ["sess-1"],
        use_ai_generate=False,
        run_structured=True,
        on_unhandled_span="fail",
    )
    unhandled = {
        d.span_id
        for d in result.diagnostics
        if d.diagnostic_code == "structured_unhandled"
    }
    assert unhandled == {"s-truly-unhandled"}, (
        "only the span with no matching extractor counts as "
        "structured_unhandled; the empty-output path is a "
        "legitimate silent outcome"
    )

  def test_invoked_span_ids_records_every_extractor_call(self):
    """``run_structured_extractors`` populates
    ``invoked_span_ids`` for every event whose event_type matches
    an extractor, whether the extractor returns empty, returns
    full output, or raises (when capturing)."""
    events = [
        {"event_type": "OK", "span_id": "s-ok"},
        {"event_type": "EMPTY", "span_id": "s-empty"},
        {"event_type": "BOOM", "span_id": "s-boom"},
        {"event_type": "UNKNOWN", "span_id": "s-no-match"},
    ]
    extractors = {
        "OK": _ok_extractor,
        "EMPTY": self._empty_extractor,
        "BOOM": _raising_extractor,
    }
    result = run_structured_extractors(
        events, extractors, _ToySpec(), capture_extractor_exceptions=True
    )
    assert result.invoked_span_ids == {"s-ok", "s-empty", "s-boom"}, (
        "every span whose event_type matched a registered extractor "
        "must appear in invoked_span_ids; s-no-match must not "
        "because no extractor was even invoked for it"
    )

  def test_merge_preserves_invoked_span_ids(self):
    """``merge_extraction_results`` unions ``invoked_span_ids``
    just like the other span-id sets so the diagnostic stream
    reflects the full orchestrator view."""
    r1 = StructuredExtractionResult(invoked_span_ids={"a", "b"})
    r2 = StructuredExtractionResult(invoked_span_ids={"b", "c"})
    from bigquery_agent_analytics.structured_extraction import merge_extraction_results

    merged = merge_extraction_results([r1, r2])
    assert merged.invoked_span_ids == {"a", "b", "c"}

  def test_merge_preserves_exceptions_from_both_sources(self):
    """``run_structured_extractors`` concatenates
    ``merged.exceptions + local_exceptions`` — symmetric to how
    nodes / edges / span_ids merge. Earlier drafts replaced
    ``merged.exceptions`` with just the local list, which would
    silently drop any exceptions an extractor already carried in
    its return value (PR #189 review informational finding)."""
    pre_existing_exc = ExtractorException(
        span_id="s-pre", event_type="EARLIER", detail="from extractor"
    )

    def _ext_with_baked_in_exc(event, spec):
      return StructuredExtractionResult(exceptions=[pre_existing_exc])

    events = [
        {"event_type": "BAKED", "span_id": "s-pre"},
        {"event_type": "BOOM", "span_id": "s-boom"},
    ]
    extractors = {
        "BAKED": _ext_with_baked_in_exc,
        "BOOM": _raising_extractor,
    }
    result = run_structured_extractors(
        events, extractors, _ToySpec(), capture_extractor_exceptions=True
    )
    # Both exceptions surface — the baked-in one from the
    # extractor's return value and the orchestrator-caught one.
    spans = {exc.span_id for exc in result.exceptions}
    assert spans == {"s-pre", "s-boom"}
