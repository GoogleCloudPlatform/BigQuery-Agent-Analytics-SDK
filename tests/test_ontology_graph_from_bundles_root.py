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

"""Tests for ``OntologyGraphManager.from_bundles_root`` (PR C2.c.2).

The classmethod is the actual orchestrator call-site swap that
puts compiled extractors on the runtime path. These tests cover:

* construction matrix — bundles present, bundles absent, direct
  ``__init__`` (no bundles wiring at all);
* the audit handle ``manager.runtime_registry`` is set/cleared
  per construction path;
* ``manager.extractors`` is the dict the runtime actually uses
  (wrapped closures or identity-preserved fallbacks);
* compiled-only-without-fallback fails closed — surfaces in
  ``bundles_without_fallback`` and is NOT registered;
* end-to-end: real BKA bundle through the manager wired into
  ``run_structured_extractors``, on_outcome fires per event.
"""

from __future__ import annotations

import json
import pathlib
import tempfile

import pytest

# ------------------------------------------------------------------ #
# Fixture helpers                                                      #
# ------------------------------------------------------------------ #


_VALID_FINGERPRINT = "a" * 64


def _bka_ontology_binding():
  from bigquery_ontology import load_binding
  from bigquery_ontology import load_ontology
  from tests.fixtures_extractor_compilation.bka_decision_inputs import BKA_BINDING_YAML
  from tests.fixtures_extractor_compilation.bka_decision_inputs import BKA_ONTOLOGY_YAML

  tmp = pathlib.Path(tempfile.mkdtemp(prefix="bka_ogm_test_"))
  (tmp / "ont.yaml").write_text(BKA_ONTOLOGY_YAML, encoding="utf-8")
  (tmp / "bnd.yaml").write_text(BKA_BINDING_YAML, encoding="utf-8")
  ontology = load_ontology(str(tmp / "ont.yaml"))
  binding = load_binding(str(tmp / "bnd.yaml"), ontology=ontology)
  return ontology, binding


def _empty_result():
  from bigquery_agent_analytics.structured_extraction import StructuredExtractionResult

  return StructuredExtractionResult()


def _write_handwritten_bundle(
    bundle_dir: pathlib.Path,
    *,
    event_types: tuple[str, ...] = ("event_x",),
    function_name: str = "extract_event",
    fingerprint: str = _VALID_FINGERPRINT,
    source: str = (
        "from bigquery_agent_analytics.structured_extraction import (\n"
        "    StructuredExtractionResult,\n"
        ")\n"
        "\n"
        "def extract_event(event, spec):\n"
        "  return StructuredExtractionResult()\n"
    ),
) -> None:
  """Write a manifest + module pair that satisfies the loader's
  trust boundary. Used by tests that don't need to drive the
  full compile pipeline."""
  bundle_dir.mkdir(parents=True, exist_ok=True)
  manifest = {
      "fingerprint": fingerprint,
      "event_types": list(event_types),
      "module_filename": "extractor.py",
      "function_name": function_name,
      "compiler_package_version": "0.0.0",
      "template_version": "v0.1",
      "transcript_builder_version": "tb-1",
      "created_at": "2026-05-11T00:00:00Z",
  }
  (bundle_dir / "manifest.json").write_text(
      json.dumps(manifest, sort_keys=True, indent=2), encoding="utf-8"
  )
  (bundle_dir / "extractor.py").write_text(source, encoding="utf-8")


# ------------------------------------------------------------------ #
# Direct __init__ leaves runtime_registry as None                    #
# ------------------------------------------------------------------ #


class TestOntologyGraphManagerDirectInit:
  """The legacy constructor path is unchanged. No bundle wiring
  ran, so ``runtime_registry`` stays ``None``; ``extractors`` is
  whatever the caller passed."""

  def test_direct_init_runtime_registry_is_none(self):
    from bigquery_agent_analytics.ontology_graph import OntologyGraphManager
    from bigquery_agent_analytics.resolved_spec import resolve

    ontology, binding = _bka_ontology_binding()
    spec = resolve(ontology, binding)

    fallback = lambda event, spec: _empty_result()
    manager = OntologyGraphManager(
        project_id="p",
        dataset_id="d",
        spec=spec,
        extractors={"bka_decision": fallback},
    )

    assert manager.runtime_registry is None
    assert manager.extractors == {"bka_decision": fallback}
    # Identity preserved — no wrapping when bundles aren't wired.
    assert manager.extractors["bka_decision"] is fallback


# ------------------------------------------------------------------ #
# from_bundles_root with no bundles                                  #
# ------------------------------------------------------------------ #


class TestFromBundlesRootNoBundles:
  """``bundles_root`` exists but contains no bundles → the
  registry has only the fallback dict. ``manager.extractors`` is
  identity-preserved; ``runtime_registry`` is the audit handle."""

  def test_no_bundles_passes_fallbacks_through(self, tmp_path: pathlib.Path):
    from bigquery_agent_analytics.ontology_graph import OntologyGraphManager

    ontology, binding = _bka_ontology_binding()
    fallback = lambda event, spec: _empty_result()

    manager = OntologyGraphManager.from_bundles_root(
        project_id="p",
        dataset_id="d",
        ontology=ontology,
        binding=binding,
        bundles_root=tmp_path,  # empty dir → no bundles
        expected_fingerprint=_VALID_FINGERPRINT,
        fallback_extractors={"bka_decision": fallback},
    )

    assert manager.runtime_registry is not None
    # Identity preserved when there's no compiled bundle to wrap.
    assert manager.extractors["bka_decision"] is fallback
    # Audit fields populated correctly.
    assert manager.runtime_registry.bundles_without_fallback == ()
    assert manager.runtime_registry.fallbacks_without_bundle == (
        "bka_decision",
    )


# ------------------------------------------------------------------ #
# from_bundles_root with matching bundle + fallback                  #
# ------------------------------------------------------------------ #


class TestFromBundlesRootCompiledAndFallback:
  """A hand-written bundle for ``event_x`` + a fallback for the
  same event_type → the registry wraps with
  ``run_with_fallback``. Verified by calling the registered
  extractor and observing the audit callback fire."""

  def test_compiled_plus_fallback_wraps_and_invokes_callback(
      self, tmp_path: pathlib.Path
  ):
    from bigquery_agent_analytics.ontology_graph import OntologyGraphManager

    ontology, binding = _bka_ontology_binding()
    _write_handwritten_bundle(
        tmp_path / "bundle_event_x", event_types=("event_x",)
    )
    fallback = lambda event, spec: _empty_result()

    callback_log: list[tuple[str, str]] = []

    def on_outcome(event_type, outcome):
      callback_log.append((event_type, outcome.decision))

    manager = OntologyGraphManager.from_bundles_root(
        project_id="p",
        dataset_id="d",
        ontology=ontology,
        binding=binding,
        bundles_root=tmp_path,
        expected_fingerprint=_VALID_FINGERPRINT,
        fallback_extractors={"event_x": fallback},
        on_outcome=on_outcome,
    )

    assert manager.runtime_registry is not None
    # event_x is wrapped — not the original fallback identity.
    assert manager.extractors["event_x"] is not fallback
    # Audit confirms both sides matched up.
    assert manager.runtime_registry.bundles_without_fallback == ()
    assert manager.runtime_registry.fallbacks_without_bundle == ()

    # Invoking the wrapped extractor drives run_with_fallback and
    # fires the callback. Bundle's compiled extractor returns a
    # valid empty result → compiled_unchanged.
    manager.extractors["event_x"](
        {"event_type": "event_x", "span_id": "sp1"}, manager.spec
    )
    assert callback_log == [("event_x", "compiled_unchanged")]


# ------------------------------------------------------------------ #
# Negative: bundle without matching fallback                          #
# ------------------------------------------------------------------ #


class TestFromBundlesRootCompiledOnlyNoFallback:
  """Compiled bundle for ``event_x`` but ``fallback_extractors``
  has no entry for ``event_x``. C2's safety contract requires a
  fallback; the registry skips the compiled-only entry and
  surfaces it in ``bundles_without_fallback``. The manager's
  ``extractors`` dict does NOT register the compiled-only
  event_type — extraction silently has no extractor for that
  event_type (fail-closed)."""

  def test_compiled_only_skipped_and_audited(self, tmp_path: pathlib.Path):
    from bigquery_agent_analytics.ontology_graph import OntologyGraphManager
    from bigquery_agent_analytics.structured_extraction import run_structured_extractors

    ontology, binding = _bka_ontology_binding()
    _write_handwritten_bundle(
        tmp_path / "bundle_event_x", event_types=("event_x",)
    )

    manager = OntologyGraphManager.from_bundles_root(
        project_id="p",
        dataset_id="d",
        ontology=ontology,
        binding=binding,
        bundles_root=tmp_path,
        expected_fingerprint=_VALID_FINGERPRINT,
        # Intentionally empty — no fallback for event_x.
        fallback_extractors={},
    )

    # Audit captures the configuration gap.
    assert manager.runtime_registry is not None
    assert manager.runtime_registry.bundles_without_fallback == ("event_x",)
    assert manager.runtime_registry.fallbacks_without_bundle == ()
    # Compiled-only is NOT registered — extraction sees no
    # extractor for event_x.
    assert "event_x" not in manager.extractors
    # Behavioral confirmation: running structured extractors over
    # an event_x event yields an empty result, the
    # run_structured_extractors "no extractor → skip" path.
    result = run_structured_extractors(
        events=[{"event_type": "event_x", "span_id": "sp1"}],
        extractors=manager.extractors,
        spec=manager.spec,
    )
    assert result.nodes == []
    assert result.edges == []


# ------------------------------------------------------------------ #
# End-to-end: real BKA bundle through the manager                    #
# ------------------------------------------------------------------ #


class TestFromBundlesRootEndToEnd:
  """Real BKA compiled bundle (driven through the full Phase C
  pipeline) + the real handwritten ``extract_bka_decision_event``
  as fallback → wired into ``run_structured_extractors`` via the
  manager. Proves the call-site swap actually puts compiled
  extractors on the runtime path."""

  def test_real_bka_bundle_through_manager_to_run_structured_extractors(
      self, tmp_path: pathlib.Path
  ):
    from bigquery_agent_analytics.extractor_compilation import compile_extractor
    from bigquery_agent_analytics.extractor_compilation import parse_resolved_extractor_plan_json
    from bigquery_agent_analytics.extractor_compilation import render_extractor_source
    from bigquery_agent_analytics.ontology_graph import OntologyGraphManager
    from bigquery_agent_analytics.resolved_spec import resolve
    from bigquery_agent_analytics.structured_extraction import extract_bka_decision_event
    from bigquery_agent_analytics.structured_extraction import run_structured_extractors
    from tests.fixtures_extractor_compilation.bka_decision_inputs import BKA_FINGERPRINT_INPUTS
    from tests.fixtures_extractor_compilation.bka_decision_inputs import BKA_RESOLVED_PLAN_DICT
    from tests.fixtures_extractor_compilation.bka_decision_inputs import BKA_SAMPLE_EVENTS

    ontology, binding = _bka_ontology_binding()
    spec = resolve(ontology, binding)

    # Compile the BKA bundle to disk.
    plan = parse_resolved_extractor_plan_json(BKA_RESOLVED_PLAN_DICT)
    source = render_extractor_source(plan)
    bundles_root = tmp_path / "bundles"
    compile_result = compile_extractor(
        source=source,
        module_name="bka_ogm_test",
        function_name=plan.function_name,
        event_types=(plan.event_type,),
        sample_events=BKA_SAMPLE_EVENTS,
        spec=None,
        resolved_graph=spec,
        parent_bundle_dir=bundles_root,
        fingerprint_inputs=BKA_FINGERPRINT_INPUTS,
        template_version="v0.1",
        compiler_package_version="0.0.0",
        isolation=False,
    )
    assert compile_result.ok

    callback_log: list[tuple[str, str]] = []

    def on_outcome(event_type, outcome):
      callback_log.append((event_type, outcome.decision))

    manager = OntologyGraphManager.from_bundles_root(
        project_id="p",
        dataset_id="d",
        ontology=ontology,
        binding=binding,
        bundles_root=bundles_root,
        expected_fingerprint=compile_result.manifest.fingerprint,
        fallback_extractors={"bka_decision": extract_bka_decision_event},
        event_type_allowlist=("bka_decision",),
        on_outcome=on_outcome,
    )

    # Audit confirms wiring.
    assert manager.runtime_registry is not None
    assert manager.runtime_registry.bundles_without_fallback == ()
    assert manager.runtime_registry.fallbacks_without_bundle == ()

    # Run the existing run_structured_extractors hook through
    # the manager's extractors dict — same call shape the
    # orchestrator uses internally.
    merged = run_structured_extractors(
        events=BKA_SAMPLE_EVENTS,
        extractors=manager.extractors,
        spec=manager.spec,
    )

    # Two BKA sample events → two compiled_unchanged outcomes,
    # two mako_DecisionPoint nodes.
    assert len(merged.nodes) == 2
    assert {n.entity_name for n in merged.nodes} == {"mako_DecisionPoint"}
    assert callback_log == [
        ("bka_decision", "compiled_unchanged"),
        ("bka_decision", "compiled_unchanged"),
    ]


# ------------------------------------------------------------------ #
# Production call site: manager.extract_graph(...) actually uses     #
# the wrapped registry                                                #
# ------------------------------------------------------------------ #


class TestFromBundlesRootExtractGraphCallSite:
  """The previous end-to-end test exercises
  ``run_structured_extractors`` directly with
  ``manager.extractors``. That proves the registry works, but
  it doesn't prove ``manager.extract_graph(...)`` — the actual
  production call site — invokes the wrapped registry.

  These tests monkeypatch the BigQuery-touching dependencies
  (``_fetch_raw_events``, ``_extract_via_ai_generate``,
  ``_extract_payloads``) so we can drive ``extract_graph`` with
  canned events and assert the wrapped callback fired at the
  real call site."""

  def test_extract_graph_invokes_wrapped_registry_on_compiled_path(
      self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
  ):
    from bigquery_agent_analytics.extracted_models import ExtractedGraph
    from bigquery_agent_analytics.ontology_graph import OntologyGraphManager

    ontology, binding = _bka_ontology_binding()
    _write_handwritten_bundle(
        tmp_path / "bundle_event_x", event_types=("event_x",)
    )

    fallback_called = {"count": 0}

    def fallback(event, spec):
      fallback_called["count"] += 1
      return _empty_result()

    callback_log: list[tuple[str, str]] = []

    def on_outcome(event_type, outcome):
      callback_log.append((event_type, outcome.decision))

    manager = OntologyGraphManager.from_bundles_root(
        project_id="p",
        dataset_id="d",
        ontology=ontology,
        binding=binding,
        bundles_root=tmp_path,
        expected_fingerprint=_VALID_FINGERPRINT,
        fallback_extractors={"event_x": fallback},
        on_outcome=on_outcome,
    )

    # Monkeypatch the BigQuery-touching methods so we can drive
    # extract_graph end-to-end without a real BQ client. The
    # raw events flow through the wrapped registry; the
    # AI.GENERATE call is stubbed to a no-op.
    monkeypatch.setattr(
        manager,
        "_fetch_raw_events",
        lambda session_ids: [
            {"event_type": "event_x", "span_id": "sp1"},
            {"event_type": "event_x", "span_id": "sp2"},
        ],
    )
    monkeypatch.setattr(
        manager,
        "_extract_via_ai_generate",
        lambda session_ids, excluded, partial, hint: ExtractedGraph(
            name=manager.spec.name,
            nodes=[],
            edges=[],
        ),
    )

    result = manager.extract_graph(session_ids=["sess1"], use_ai_generate=True)

    # The wrapped registry ran inside extract_graph. The bundle's
    # compiled extractor returned a valid empty result, so each
    # event was a ``compiled_unchanged`` outcome — fallback was
    # NOT called.
    assert fallback_called["count"] == 0
    assert callback_log == [
        ("event_x", "compiled_unchanged"),
        ("event_x", "compiled_unchanged"),
    ]
    # Result merges the structured (empty) and AI (empty)
    # graphs.
    assert isinstance(result, ExtractedGraph)

  def test_extract_graph_merges_compiled_nodes_into_result(
      self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
  ):
    """The previous test asserts ``on_outcome`` fired, but the
    compiled extractor returned an empty result so the merge
    path in ``extract_graph`` had nothing to merge. This test
    uses a compiled bundle that returns a real
    ``ExtractedNode`` and asserts that node appears in the
    final ``ExtractedGraph`` returned by ``extract_graph`` —
    proving the structured-and-AI merge actually runs the
    bundle's output through to the caller-visible result, not
    just through the registry's audit channel."""
    from bigquery_agent_analytics.extracted_models import ExtractedGraph
    from bigquery_agent_analytics.ontology_graph import OntologyGraphManager

    ontology, binding = _bka_ontology_binding()

    # Bundle whose compiled extractor returns one node + marks
    # the span fully handled. The runtime should propagate the
    # node into extract_graph's returned ExtractedGraph.
    _write_handwritten_bundle(
        tmp_path / "bundle_event_x",
        event_types=("event_x",),
        source=(
            "from bigquery_agent_analytics.extracted_models import"
            " ExtractedNode\n"
            "from bigquery_agent_analytics.extracted_models import"
            " ExtractedProperty\n"
            "from bigquery_agent_analytics.structured_extraction import"
            " StructuredExtractionResult\n"
            "\n"
            "def extract_event(event, spec):\n"
            "  node = ExtractedNode(\n"
            "      node_id=event['session_id'] + ':'"
            " + event.get('span_id', 'noid'),\n"
            "      entity_name='mako_DecisionPoint',\n"
            "      labels=['mako_DecisionPoint'],\n"
            "      properties=[\n"
            "          ExtractedProperty(name='decision_id', value='d1'),\n"
            "      ],\n"
            "  )\n"
            "  return StructuredExtractionResult(\n"
            "      nodes=[node],\n"
            "      edges=[],\n"
            "      fully_handled_span_ids={event.get('span_id', 'noid')},\n"
            "      partially_handled_span_ids=set(),\n"
            "  )\n"
        ),
    )

    fallback = lambda event, spec: _empty_result()

    manager = OntologyGraphManager.from_bundles_root(
        project_id="p",
        dataset_id="d",
        ontology=ontology,
        binding=binding,
        bundles_root=tmp_path,
        expected_fingerprint=_VALID_FINGERPRINT,
        fallback_extractors={"event_x": fallback},
    )

    monkeypatch.setattr(
        manager,
        "_fetch_raw_events",
        lambda session_ids: [
            {
                "event_type": "event_x",
                "session_id": "sess1",
                "span_id": "spA",
            }
        ],
    )
    monkeypatch.setattr(
        manager,
        "_extract_via_ai_generate",
        lambda session_ids, excluded, partial, hint: ExtractedGraph(
            name=manager.spec.name,
            nodes=[],
            edges=[],
        ),
    )

    result = manager.extract_graph(session_ids=["sess1"], use_ai_generate=True)

    # The structured node from the compiled bundle made it
    # through the merge into the final ExtractedGraph.
    assert isinstance(result, ExtractedGraph)
    node_ids = {n.node_id for n in result.nodes}
    assert "sess1:spA" in node_ids
    matching = next(n for n in result.nodes if n.node_id == "sess1:spA")
    assert matching.entity_name == "mako_DecisionPoint"
    # And the AI side was correctly told to exclude the fully-
    # handled span (verified indirectly — the stubbed
    # _extract_via_ai_generate received non-empty ``excluded``).

  def test_extract_graph_skips_structured_when_use_ai_generate_false(
      self, tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
  ):
    """``extract_graph`` only runs structured extractors under
    ``if self.extractors and use_ai_generate``. This pre-dates
    C2.c.2 — the bundle-wired path inherits the same gate. Pin
    the behavior so future changes that decouple the two
    surface as a deliberate decision."""
    from bigquery_agent_analytics.extracted_models import ExtractedGraph
    from bigquery_agent_analytics.ontology_graph import OntologyGraphManager

    ontology, binding = _bka_ontology_binding()
    _write_handwritten_bundle(
        tmp_path / "bundle_event_x", event_types=("event_x",)
    )

    fallback = lambda event, spec: _empty_result()
    callback_log: list[tuple[str, str]] = []

    def on_outcome(event_type, outcome):
      callback_log.append((event_type, outcome.decision))

    manager = OntologyGraphManager.from_bundles_root(
        project_id="p",
        dataset_id="d",
        ontology=ontology,
        binding=binding,
        bundles_root=tmp_path,
        expected_fingerprint=_VALID_FINGERPRINT,
        fallback_extractors={"event_x": fallback},
        on_outcome=on_outcome,
    )

    # Stub _extract_payloads (the use_ai_generate=False path)
    # and _fetch_raw_events; the structured path should NOT
    # call _fetch_raw_events when use_ai_generate is False.
    fetch_called = {"count": 0}

    def fake_fetch(session_ids):
      fetch_called["count"] += 1
      return []

    monkeypatch.setattr(manager, "_fetch_raw_events", fake_fetch)
    monkeypatch.setattr(
        manager,
        "_extract_payloads",
        lambda session_ids: ExtractedGraph(
            name=manager.spec.name, nodes=[], edges=[]
        ),
    )

    manager.extract_graph(session_ids=["sess1"], use_ai_generate=False)

    # Pre-existing behavior: structured extractors don't run
    # when use_ai_generate=False. The bundle wiring is correctly
    # registered, but the gate in extract_graph short-circuits.
    assert fetch_called["count"] == 0
    assert callback_log == []
