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

"""Tests for the runtime extractor-registry adapter (#75 PR C2.c.1).

Covers the four wiring outcomes:

* compiled bundle + handwritten fallback → wrapped extractor that
  invokes ``run_with_fallback``;
* fallback only → original callable registered unchanged;
* compiled only → not registered, surfaced in
  ``bundles_without_fallback``;
* neither → not registered (trivially).

Plus the audit-surface invariants
(``fallbacks_without_bundle``), allowlist filtering of both
candidate pools, the ``on_outcome`` callback's invocation
discipline (fires on every wrapped event, exceptions
propagate), and one end-to-end test wiring real BKA bundles +
the handwritten ``extract_bka_decision_event`` through the
existing ``run_structured_extractors`` hook.
"""

from __future__ import annotations

import json
import pathlib
import textwrap

import pytest

# ------------------------------------------------------------------ #
# Fixture helpers                                                      #
# ------------------------------------------------------------------ #
#
# Build hand-written bundles on disk so most tests don't depend
# on the full compile pipeline. Mirrors the helpers in
# test_extractor_compilation_bundle_loader.py — kept inline here
# to avoid cross-test-file fixture imports.


_VALID_FINGERPRINT = "a" * 64


def _write_bundle(
    bundle_dir: pathlib.Path,
    *,
    event_types: tuple[str, ...] = ("event_a",),
    function_name: str = "extract_event",
    module_filename: str = "extractor.py",
    source: str = "def extract_event(event, spec):\n  return None\n",
    fingerprint: str = _VALID_FINGERPRINT,
) -> None:
  bundle_dir.mkdir(parents=True, exist_ok=True)
  manifest = {
      "fingerprint": fingerprint,
      "event_types": list(event_types),
      "module_filename": module_filename,
      "function_name": function_name,
      "compiler_package_version": "0.0.0",
      "template_version": "v0.1",
      "transcript_builder_version": "tb-1",
      "created_at": "2026-05-08T00:00:00Z",
  }
  (bundle_dir / "manifest.json").write_text(
      json.dumps(manifest, sort_keys=True, indent=2), encoding="utf-8"
  )
  (bundle_dir / module_filename).write_text(source, encoding="utf-8")


def _empty_result():
  from bigquery_agent_analytics.structured_extraction import StructuredExtractionResult

  return StructuredExtractionResult()


# ------------------------------------------------------------------ #
# Wiring matrix: compiled+fallback / fallback-only /                  #
# compiled-only / neither                                              #
# ------------------------------------------------------------------ #


class TestRegistryWiringMatrix:

  def test_compiled_plus_fallback_wraps_with_run_with_fallback(
      self, tmp_path: pathlib.Path
  ):
    """Both present → registry entry is a wrapper that invokes
    ``run_with_fallback`` under the hood. Verified by passing a
    fallback callable that asserts it's never called for a clean
    compiled output (the wrapper should consume the compiled
    result via the ``compiled_unchanged`` path)."""
    from bigquery_agent_analytics.extractor_compilation import build_runtime_extractor_registry

    _write_bundle(tmp_path / "bundle_a", event_types=("event_a",))

    fallback_called = {"count": 0}

    def fallback_a(event, spec):
      fallback_called["count"] += 1
      return _empty_result()

    registry = build_runtime_extractor_registry(
        bundles_root=tmp_path,
        expected_fingerprint=_VALID_FINGERPRINT,
        fallback_extractors={"event_a": fallback_a},
        resolved_graph=None,  # validation runs but produces no failures
    )

    assert "event_a" in registry.extractors
    # The wrapper is NOT the original fallback (different identity).
    assert registry.extractors["event_a"] is not fallback_a
    # Invoking the wrapper drives run_with_fallback. With a
    # ``return None`` compiled extractor (returns None →
    # WrongReturnType), the wrapper falls back to the fallback.
    result = registry.extractors["event_a"](
        {"event_type": "event_a", "span_id": "sp1"}, None
    )
    # Compiled returns None → fallback was called.
    assert fallback_called["count"] == 1
    assert result is not None  # fallback's empty result

  def test_fallback_only_passes_through_unchanged(self, tmp_path: pathlib.Path):
    """No compiled bundle for an event_type → the original
    fallback callable is registered unchanged (same identity).
    No wrapping, no callback invocations."""
    from bigquery_agent_analytics.extractor_compilation import build_runtime_extractor_registry

    # No bundle for event_a.
    fallback_a = lambda e, s: _empty_result()
    on_outcome_log: list = []

    def on_outcome(event_type, outcome):
      on_outcome_log.append((event_type, outcome))

    registry = build_runtime_extractor_registry(
        bundles_root=tmp_path,
        expected_fingerprint=_VALID_FINGERPRINT,
        fallback_extractors={"event_a": fallback_a},
        resolved_graph=None,
        on_outcome=on_outcome,
    )

    # Identity preserved — no wrapping when there's no compiled
    # extractor to wrap.
    assert registry.extractors["event_a"] is fallback_a
    # Calling the registered extractor doesn't invoke on_outcome
    # (there's no FallbackOutcome to report — fallback is the
    # only path).
    registry.extractors["event_a"]({"event_type": "event_a"}, None)
    assert on_outcome_log == []

  def test_compiled_only_skipped_and_recorded(self, tmp_path: pathlib.Path):
    """Compiled bundle for event_a but no fallback → not
    registered, surfaced in ``bundles_without_fallback``. C2's
    safety contract requires a fallback; without one, fail
    closed."""
    from bigquery_agent_analytics.extractor_compilation import build_runtime_extractor_registry

    _write_bundle(tmp_path / "bundle_a", event_types=("event_a",))

    registry = build_runtime_extractor_registry(
        bundles_root=tmp_path,
        expected_fingerprint=_VALID_FINGERPRINT,
        fallback_extractors={},  # empty
        resolved_graph=None,
    )

    assert registry.extractors == {}
    assert registry.bundles_without_fallback == ("event_a",)
    assert registry.fallbacks_without_bundle == ()
    # The bundle still loaded successfully — discovery captured
    # it. Just not registered.
    assert "event_a" in registry.discovery.registry

  def test_neither_present_is_empty_registry(self, tmp_path: pathlib.Path):
    """Empty fallbacks + empty bundle dir → empty registry, no
    audit entries."""
    from bigquery_agent_analytics.extractor_compilation import build_runtime_extractor_registry

    registry = build_runtime_extractor_registry(
        bundles_root=tmp_path,
        expected_fingerprint=_VALID_FINGERPRINT,
        fallback_extractors={},
        resolved_graph=None,
    )

    assert registry.extractors == {}
    assert registry.bundles_without_fallback == ()
    assert registry.fallbacks_without_bundle == ()


# ------------------------------------------------------------------ #
# Audit surfaces                                                      #
# ------------------------------------------------------------------ #


class TestRegistryAuditSurfaces:

  def test_fallbacks_without_bundle_surfaces_no_compiled_coverage(
      self, tmp_path: pathlib.Path
  ):
    """Fallback registered for event_a but no bundle covers it
    → registered unchanged AND listed in
    ``fallbacks_without_bundle``. This is the "rollout coverage"
    audit shape — distinct from ``bundles_without_fallback``."""
    from bigquery_agent_analytics.extractor_compilation import build_runtime_extractor_registry

    fallback_a = lambda e, s: _empty_result()

    registry = build_runtime_extractor_registry(
        bundles_root=tmp_path,
        expected_fingerprint=_VALID_FINGERPRINT,
        fallback_extractors={"event_a": fallback_a},
        resolved_graph=None,
    )

    assert registry.fallbacks_without_bundle == ("event_a",)
    assert registry.bundles_without_fallback == ()
    # Event_a is still in extractors (registered unchanged).
    assert registry.extractors["event_a"] is fallback_a

  def test_audit_lists_are_sorted(self, tmp_path: pathlib.Path):
    """Audit tuples are deterministic — sorted alphabetically so
    code review and telemetry diffs stay stable."""
    from bigquery_agent_analytics.extractor_compilation import build_runtime_extractor_registry

    _write_bundle(tmp_path / "bundle_z", event_types=("event_z",))
    _write_bundle(tmp_path / "bundle_a", event_types=("event_a",))
    _write_bundle(tmp_path / "bundle_m", event_types=("event_m",))

    registry = build_runtime_extractor_registry(
        bundles_root=tmp_path,
        expected_fingerprint=_VALID_FINGERPRINT,
        fallback_extractors={},
        resolved_graph=None,
    )

    assert registry.bundles_without_fallback == (
        "event_a",
        "event_m",
        "event_z",
    )

  def test_mixed_audit_state(self, tmp_path: pathlib.Path):
    """Three event_types: compiled+fallback / compiled-only /
    fallback-only. Each lands in the right bucket."""
    from bigquery_agent_analytics.extractor_compilation import build_runtime_extractor_registry

    _write_bundle(tmp_path / "bundle_both", event_types=("both",))
    _write_bundle(
        tmp_path / "bundle_compiled_only", event_types=("compiled_only",)
    )

    fallback_both = lambda e, s: _empty_result()
    fallback_only = lambda e, s: _empty_result()

    registry = build_runtime_extractor_registry(
        bundles_root=tmp_path,
        expected_fingerprint=_VALID_FINGERPRINT,
        fallback_extractors={
            "both": fallback_both,
            "fallback_only": fallback_only,
        },
        resolved_graph=None,
    )

    assert set(registry.extractors.keys()) == {"both", "fallback_only"}
    # `both` is wrapped; `fallback_only` passes through.
    assert registry.extractors["fallback_only"] is fallback_only
    assert registry.extractors["both"] is not fallback_both
    assert registry.bundles_without_fallback == ("compiled_only",)
    assert registry.fallbacks_without_bundle == ("fallback_only",)


# ------------------------------------------------------------------ #
# event_type_allowlist                                                #
# ------------------------------------------------------------------ #


class TestRegistryAllowlist:

  def test_allowlist_filters_both_pools(self, tmp_path: pathlib.Path):
    """Allowlist=('a',) with bundles for ('a', 'b', 'c') and
    fallbacks for ('a', 'b', 'd') → only 'a' considered.
    'b' is allowed-by-fallback but disallowed-by-allowlist; it
    appears in NEITHER ``bundles_without_fallback`` nor
    ``fallbacks_without_bundle`` since it's outside scope."""
    from bigquery_agent_analytics.extractor_compilation import build_runtime_extractor_registry

    _write_bundle(tmp_path / "bundle_a", event_types=("a",))
    _write_bundle(tmp_path / "bundle_b", event_types=("b",))
    _write_bundle(tmp_path / "bundle_c", event_types=("c",))

    fallback_a = lambda e, s: _empty_result()
    fallback_b = lambda e, s: _empty_result()
    fallback_d = lambda e, s: _empty_result()

    registry = build_runtime_extractor_registry(
        bundles_root=tmp_path,
        expected_fingerprint=_VALID_FINGERPRINT,
        fallback_extractors={
            "a": fallback_a,
            "b": fallback_b,
            "d": fallback_d,
        },
        resolved_graph=None,
        event_type_allowlist=("a",),
    )

    # Only 'a' makes it into the registry.
    assert set(registry.extractors.keys()) == {"a"}
    # 'b', 'c', 'd' are silently dropped — not in any audit
    # field since they're outside the caller's scope.
    assert registry.bundles_without_fallback == ()
    assert registry.fallbacks_without_bundle == ()

  def test_empty_allowlist_registers_nothing(self, tmp_path: pathlib.Path):
    from bigquery_agent_analytics.extractor_compilation import build_runtime_extractor_registry

    _write_bundle(tmp_path / "bundle_a", event_types=("a",))
    fallback_a = lambda e, s: _empty_result()

    registry = build_runtime_extractor_registry(
        bundles_root=tmp_path,
        expected_fingerprint=_VALID_FINGERPRINT,
        fallback_extractors={"a": fallback_a},
        resolved_graph=None,
        event_type_allowlist=(),
    )

    assert registry.extractors == {}
    assert registry.bundles_without_fallback == ()
    assert registry.fallbacks_without_bundle == ()

  def test_allowlist_none_considers_everything(self, tmp_path: pathlib.Path):
    from bigquery_agent_analytics.extractor_compilation import build_runtime_extractor_registry

    _write_bundle(tmp_path / "bundle_a", event_types=("a",))
    fallback_a = lambda e, s: _empty_result()
    fallback_b = lambda e, s: _empty_result()

    registry = build_runtime_extractor_registry(
        bundles_root=tmp_path,
        expected_fingerprint=_VALID_FINGERPRINT,
        fallback_extractors={"a": fallback_a, "b": fallback_b},
        resolved_graph=None,
        event_type_allowlist=None,
    )

    assert set(registry.extractors.keys()) == {"a", "b"}
    assert registry.fallbacks_without_bundle == ("b",)


# ------------------------------------------------------------------ #
# on_outcome callback invocation                                      #
# ------------------------------------------------------------------ #


class TestRegistryOnOutcomeCallback:

  def _setup_with_callback(self, tmp_path: pathlib.Path):
    """Helper: registry with one wrapped extractor and a
    list-recording callback. Returns (registry, callback_log)."""
    from bigquery_agent_analytics.extractor_compilation import build_runtime_extractor_registry

    _write_bundle(tmp_path / "bundle", event_types=("event_a",))

    fallback = lambda e, s: _empty_result()
    callback_log: list = []

    def on_outcome(event_type, outcome):
      callback_log.append((event_type, outcome.decision))

    registry = build_runtime_extractor_registry(
        bundles_root=tmp_path,
        expected_fingerprint=_VALID_FINGERPRINT,
        fallback_extractors={"event_a": fallback},
        resolved_graph=None,
        on_outcome=on_outcome,
    )
    return registry, callback_log

  def test_callback_fires_on_compiled_unchanged(self, tmp_path: pathlib.Path):
    """The compiled extractor in ``_write_bundle``'s default
    source returns ``None``, which trips ``WrongReturnType``.
    To exercise ``compiled_unchanged``, write a bundle that
    returns a real (empty) ``StructuredExtractionResult`` and
    pass a real ``ResolvedGraph`` for validation. An empty
    result has no nodes / edges to validate against any spec,
    so the validator returns a clean report → ``compiled_unchanged``."""
    from bigquery_agent_analytics.extractor_compilation import build_runtime_extractor_registry
    from bigquery_agent_analytics.resolved_spec import resolve
    from bigquery_ontology import load_binding
    from bigquery_ontology import load_ontology
    from tests.fixtures_extractor_compilation.bka_decision_inputs import BKA_BINDING_YAML
    from tests.fixtures_extractor_compilation.bka_decision_inputs import BKA_ONTOLOGY_YAML

    # Compiled extractor returns a real (empty) result.
    _write_bundle(
        tmp_path / "bundle",
        event_types=("event_a",),
        source=textwrap.dedent(
            """\
            from bigquery_agent_analytics.structured_extraction import (
                StructuredExtractionResult,
            )

            def extract_event(event, spec):
                return StructuredExtractionResult()
        """
        ),
    )

    # Real ResolvedGraph so validate_extracted_graph runs cleanly.
    spec_dir = tmp_path / "spec"
    spec_dir.mkdir()
    (spec_dir / "ont.yaml").write_text(BKA_ONTOLOGY_YAML, encoding="utf-8")
    (spec_dir / "bnd.yaml").write_text(BKA_BINDING_YAML, encoding="utf-8")
    ontology = load_ontology(str(spec_dir / "ont.yaml"))
    binding = load_binding(str(spec_dir / "bnd.yaml"), ontology=ontology)
    resolved_graph = resolve(ontology, binding)

    fallback_called = {"count": 0}

    def fallback(event, spec):
      fallback_called["count"] += 1
      return _empty_result()

    callback_log: list = []

    def on_outcome(event_type, outcome):
      callback_log.append((event_type, outcome.decision))

    registry = build_runtime_extractor_registry(
        bundles_root=tmp_path,
        expected_fingerprint=_VALID_FINGERPRINT,
        fallback_extractors={"event_a": fallback},
        resolved_graph=resolved_graph,
        on_outcome=on_outcome,
    )

    registry.extractors["event_a"](
        {"event_type": "event_a", "span_id": "sp1"}, None
    )

    # Compiled output validates clean → compiled_unchanged.
    # Fallback was NOT called.
    assert fallback_called["count"] == 0
    assert callback_log == [("event_a", "compiled_unchanged")]

  def test_callback_fires_on_fallback_for_event(self, tmp_path: pathlib.Path):
    """Compiled extractor returns ``None`` (default
    ``_write_bundle`` source) → ``WrongReturnType`` →
    ``fallback_for_event``. Callback fires with that decision."""
    registry, callback_log = self._setup_with_callback(tmp_path)
    registry.extractors["event_a"](
        {"event_type": "event_a", "span_id": "sp1"}, None
    )

    assert len(callback_log) == 1
    assert callback_log[0][0] == "event_a"
    assert callback_log[0][1] == "fallback_for_event"

  def test_callback_invocation_count_matches_event_count(
      self, tmp_path: pathlib.Path
  ):
    """Three sequential invocations → three callback calls.
    Important for telemetry: the callback IS the denominator
    metric."""
    registry, callback_log = self._setup_with_callback(tmp_path)
    for _ in range(3):
      registry.extractors["event_a"](
          {"event_type": "event_a", "span_id": "sp1"}, None
      )
    assert len(callback_log) == 3

  def test_callback_exception_propagates(self, tmp_path: pathlib.Path):
    """Telemetry callbacks should be correct; the adapter does
    NOT wrap the call in try/except. An instrumentation bug
    surfaces as a real exception, not silent data loss."""
    from bigquery_agent_analytics.extractor_compilation import build_runtime_extractor_registry

    _write_bundle(tmp_path / "bundle", event_types=("event_a",))
    fallback = lambda e, s: _empty_result()

    def bad_callback(event_type, outcome):
      raise RuntimeError("telemetry adapter is broken")

    registry = build_runtime_extractor_registry(
        bundles_root=tmp_path,
        expected_fingerprint=_VALID_FINGERPRINT,
        fallback_extractors={"event_a": fallback},
        resolved_graph=None,
        on_outcome=bad_callback,
    )

    with pytest.raises(RuntimeError, match="telemetry adapter is broken"):
      registry.extractors["event_a"](
          {"event_type": "event_a", "span_id": "sp1"}, None
      )


# ------------------------------------------------------------------ #
# End-to-end with real BKA bundle                                     #
# ------------------------------------------------------------------ #


class TestRegistryEndToEnd:
  """Real BKA-decision compiled bundle as ``compiled_extractor``,
  real ``extract_bka_decision_event`` as ``fallback_extractor``,
  fed through the existing ``run_structured_extractors`` hook
  using the registry from this PR. Proves the adapter
  composes cleanly with the pieces it sits between (C2.a +
  C2.b) and the existing runtime entry point."""

  def test_real_bka_bundle_through_run_structured_extractors(
      self, tmp_path: pathlib.Path
  ):
    from bigquery_agent_analytics.extractor_compilation import build_runtime_extractor_registry
    from bigquery_agent_analytics.extractor_compilation import compile_extractor
    from bigquery_agent_analytics.extractor_compilation import parse_resolved_extractor_plan_json
    from bigquery_agent_analytics.extractor_compilation import render_extractor_source
    from bigquery_agent_analytics.resolved_spec import resolve
    from bigquery_agent_analytics.structured_extraction import extract_bka_decision_event
    from bigquery_agent_analytics.structured_extraction import run_structured_extractors
    from bigquery_ontology import load_binding
    from bigquery_ontology import load_ontology
    from tests.fixtures_extractor_compilation.bka_decision_inputs import BKA_BINDING_YAML
    from tests.fixtures_extractor_compilation.bka_decision_inputs import BKA_FINGERPRINT_INPUTS
    from tests.fixtures_extractor_compilation.bka_decision_inputs import BKA_ONTOLOGY_YAML
    from tests.fixtures_extractor_compilation.bka_decision_inputs import BKA_RESOLVED_PLAN_DICT
    from tests.fixtures_extractor_compilation.bka_decision_inputs import BKA_SAMPLE_EVENTS

    # Resolve the BKA spec.
    spec_dir = tmp_path / "spec"
    spec_dir.mkdir()
    (spec_dir / "ont.yaml").write_text(BKA_ONTOLOGY_YAML, encoding="utf-8")
    (spec_dir / "bnd.yaml").write_text(BKA_BINDING_YAML, encoding="utf-8")
    ontology = load_ontology(str(spec_dir / "ont.yaml"))
    binding = load_binding(str(spec_dir / "bnd.yaml"), ontology=ontology)
    resolved_graph = resolve(ontology, binding)

    # Compile the BKA bundle to disk.
    plan = parse_resolved_extractor_plan_json(BKA_RESOLVED_PLAN_DICT)
    source = render_extractor_source(plan)
    bundles_root = tmp_path / "bundles"
    compile_result = compile_extractor(
        source=source,
        module_name="bka_registry_test",
        function_name=plan.function_name,
        event_types=(plan.event_type,),
        sample_events=BKA_SAMPLE_EVENTS,
        spec=None,
        resolved_graph=resolved_graph,
        parent_bundle_dir=bundles_root,
        fingerprint_inputs=BKA_FINGERPRINT_INPUTS,
        template_version="v0.1",
        compiler_package_version="0.0.0",
        isolation=False,
    )
    assert compile_result.ok

    # Build the registry with the real handwritten extractor as
    # fallback.
    callback_log: list = []

    def on_outcome(event_type, outcome):
      callback_log.append((event_type, outcome.decision))

    registry = build_runtime_extractor_registry(
        bundles_root=bundles_root,
        expected_fingerprint=compile_result.manifest.fingerprint,
        fallback_extractors={"bka_decision": extract_bka_decision_event},
        resolved_graph=resolved_graph,
        event_type_allowlist=("bka_decision",),
        on_outcome=on_outcome,
    )

    # Audit shape: bundle + fallback both present, no orphans.
    assert "bka_decision" in registry.extractors
    assert registry.bundles_without_fallback == ()
    assert registry.fallbacks_without_bundle == ()

    # Run through the existing run_structured_extractors hook.
    merged = run_structured_extractors(
        events=BKA_SAMPLE_EVENTS,
        extractors=registry.extractors,
        spec=None,
    )

    # Both sample events produced nodes via the compiled path.
    assert len(merged.nodes) == 2
    assert {n.entity_name for n in merged.nodes} == {"mako_DecisionPoint"}
    # Callback fired once per event with compiled_unchanged
    # (compiled output validates clean against the spec).
    assert callback_log == [
        ("bka_decision", "compiled_unchanged"),
        ("bka_decision", "compiled_unchanged"),
    ]


# ------------------------------------------------------------------ #
# Fallback callable validation (review P2)                            #
# ------------------------------------------------------------------ #


class TestRegistryFallbackCallableValidation:
  """``run_structured_extractors`` silently skips ``None`` entries
  (treats them as "no extractor") and raises ``TypeError`` when it
  tries to invoke a non-callable value — both far from the
  misconfiguration site. The adapter validates the whole
  ``fallback_extractors`` dict at build time so the error
  surfaces with the offending event_type named."""

  def test_none_fallback_rejected(self, tmp_path: pathlib.Path):
    """Reviewer's repro #1: ``fallback_extractors={\"a\": None}``
    used to land in the registry as-is; the runtime would silently
    skip it. Build-time validation rejects with a clear error."""
    from bigquery_agent_analytics.extractor_compilation import build_runtime_extractor_registry

    with pytest.raises(
        TypeError, match=r"fallback_extractors\['a'\] must be callable"
    ):
      build_runtime_extractor_registry(
          bundles_root=tmp_path,
          expected_fingerprint=_VALID_FINGERPRINT,
          fallback_extractors={"a": None},
          resolved_graph=None,
      )

  def test_non_callable_fallback_rejected(self, tmp_path: pathlib.Path):
    """Reviewer's repro #2: ``fallback_extractors={\"a\": 123}``."""
    from bigquery_agent_analytics.extractor_compilation import build_runtime_extractor_registry

    with pytest.raises(
        TypeError, match=r"fallback_extractors\['a'\] must be callable"
    ):
      build_runtime_extractor_registry(
          bundles_root=tmp_path,
          expected_fingerprint=_VALID_FINGERPRINT,
          fallback_extractors={"a": 123},
          resolved_graph=None,
      )

  def test_invalid_fallback_outside_allowlist_still_rejected(
      self, tmp_path: pathlib.Path
  ):
    """The full ``fallback_extractors`` dict is validated, not
    just the scoped subset. A misconfig in an out-of-allowlist
    entry is still a misconfig — surfacing it now beats hiding
    it until the allowlist widens."""
    from bigquery_agent_analytics.extractor_compilation import build_runtime_extractor_registry

    fallback_a = lambda e, s: _empty_result()

    with pytest.raises(
        TypeError, match=r"fallback_extractors\['b'\] must be callable"
    ):
      build_runtime_extractor_registry(
          bundles_root=tmp_path,
          expected_fingerprint=_VALID_FINGERPRINT,
          fallback_extractors={"a": fallback_a, "b": None},
          resolved_graph=None,
          # 'b' is OUT of allowlist but still has to be callable.
          event_type_allowlist=("a",),
      )

  def test_non_string_key_rejected(self, tmp_path: pathlib.Path):
    """``run_structured_extractors`` keys event_type lookups by
    string. A non-string key would silently never match — and
    mixing string + non-string keys would crash later when the
    audit tuples are sorted."""
    from bigquery_agent_analytics.extractor_compilation import build_runtime_extractor_registry

    fallback = lambda e, s: _empty_result()

    with pytest.raises(
        TypeError, match=r"fallback_extractors keys must be strings"
    ):
      build_runtime_extractor_registry(
          bundles_root=tmp_path,
          expected_fingerprint=_VALID_FINGERPRINT,
          fallback_extractors={1: fallback},
          resolved_graph=None,
      )

  def test_empty_string_key_rejected(self, tmp_path: pathlib.Path):
    """Event type ``\"\"`` is meaningless — silently registering
    an empty-string event_type would create a registry entry no
    real event could match."""
    from bigquery_agent_analytics.extractor_compilation import build_runtime_extractor_registry

    fallback = lambda e, s: _empty_result()

    with pytest.raises(TypeError, match=r"empty-string key"):
      build_runtime_extractor_registry(
          bundles_root=tmp_path,
          expected_fingerprint=_VALID_FINGERPRINT,
          fallback_extractors={"": fallback},
          resolved_graph=None,
      )

  def test_mixed_key_types_rejected_before_sort_crash(
      self, tmp_path: pathlib.Path
  ):
    """``{\"a\": fn, 1: fn}`` would otherwise crash at audit-
    tuple ``sorted(...)`` time with a confusing
    ``\"'<' not supported between str and int\"`` error. The
    pre-sort key-type check fails the whole call earlier with a
    clear message."""
    from bigquery_agent_analytics.extractor_compilation import build_runtime_extractor_registry

    fallback = lambda e, s: _empty_result()

    with pytest.raises(
        TypeError, match=r"fallback_extractors keys must be strings"
    ):
      build_runtime_extractor_registry(
          bundles_root=tmp_path,
          expected_fingerprint=_VALID_FINGERPRINT,
          fallback_extractors={"a": fallback, 1: fallback},
          resolved_graph=None,
      )

  def test_one_arg_callable_rejected(self, tmp_path: pathlib.Path):
    """``StructuredExtractor`` is a ``(event, spec) -> ...``
    contract. A one-arg lambda would pass build-time
    ``callable()`` check but crash when invoked. Reuse the
    bundle loader's ``_signature_compatible`` check at the
    runtime trust boundary."""
    from bigquery_agent_analytics.extractor_compilation import build_runtime_extractor_registry

    one_arg = lambda event: _empty_result()

    with pytest.raises(TypeError, match=r"fallback_extractors\['a'\]"):
      build_runtime_extractor_registry(
          bundles_root=tmp_path,
          expected_fingerprint=_VALID_FINGERPRINT,
          fallback_extractors={"a": one_arg},
          resolved_graph=None,
      )
