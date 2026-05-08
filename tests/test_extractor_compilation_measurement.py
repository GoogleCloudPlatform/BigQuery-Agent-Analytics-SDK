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

"""Deterministic tests for the compile-and-measure utility (#75 PR 4c).

Strategy:

* ``DeterministicBkaPlanClient`` emits the canonical resolved
  plan for the BKA-decision inputs without invoking any real LLM;
* the BKA fixture fingerprint inputs / sample events / ontology
  + binding mirror what 4b.1's compile tests already use;
* one test wires the real ``compile_extractor`` through the loop
  and asserts ``parity_ok=True`` against ``extract_bka_decision_event``;
* additional tests cover the loop-failure path (``measure_compile``
  returns a populated record on failure rather than raising) and
  the report's JSON round-trip contract.

The live-LLM + live-BigQuery proof is in
``test_extractor_compilation_bka_compile_live.py`` and is gated
on opt-in environment variables.
"""

from __future__ import annotations

import json
import pathlib
import tempfile
from typing import Any, Optional
import uuid

import pytest

# ------------------------------------------------------------------ #
# Reused BKA YAML + fingerprint fixtures from PR 4b.1                 #
# ------------------------------------------------------------------ #
#
# Pulled inline rather than imported so this test module doesn't
# depend on test-private helpers in test_extractor_compilation.py.
# Two YAML constants only — keeping them here keeps the BKA spec
# round-trippable without a cross-test-file import.


def _bka_resolved_spec():
  """Resolve the centralized BKA YAML fixtures into a
  ``ResolvedGraph`` for the smoke gate."""
  from bigquery_agent_analytics.resolved_spec import resolve
  from bigquery_ontology import load_binding
  from bigquery_ontology import load_ontology
  from tests.fixtures_extractor_compilation.bka_decision_inputs import BKA_BINDING_YAML
  from tests.fixtures_extractor_compilation.bka_decision_inputs import BKA_ONTOLOGY_YAML

  tmp = pathlib.Path(tempfile.mkdtemp(prefix="bka_measure_test_"))
  (tmp / "ont.yaml").write_text(BKA_ONTOLOGY_YAML, encoding="utf-8")
  (tmp / "bnd.yaml").write_text(BKA_BINDING_YAML, encoding="utf-8")
  ontology = load_ontology(str(tmp / "ont.yaml"))
  binding = load_binding(str(tmp / "bnd.yaml"), ontology=ontology)
  return resolve(ontology, binding)


def _unique_module_name(prefix: str = "bka_measure_") -> str:
  return f"{prefix}{uuid.uuid4().hex[:12]}"


# ------------------------------------------------------------------ #
# Deterministic LLM client                                            #
# ------------------------------------------------------------------ #


class DeterministicBkaPlanClient:
  """Returns a canned plan dict on each call.

  The default fixture (no constructor args) returns the canonical
  ``BKA_RESOLVED_PLAN_DICT`` — what a correct LLM step *should*
  emit for the BKA inputs. Tests that exercise failure paths can
  pass alternate response sequences.
  """

  def __init__(self, responses: Optional[list[Any]] = None) -> None:
    if responses is None:
      from tests.fixtures_extractor_compilation.bka_decision_inputs import BKA_RESOLVED_PLAN_DICT

      responses = [BKA_RESOLVED_PLAN_DICT]
    self._responses = list(responses)
    self._index = 0
    self.call_log: list[tuple[str, dict]] = []

  def generate_json(self, prompt: str, schema: dict) -> dict:
    self.call_log.append((prompt, schema))
    if self._index >= len(self._responses):
      raise AssertionError(
          "DeterministicBkaPlanClient exhausted its response queue"
      )
    item = self._responses[self._index]
    self._index += 1
    if isinstance(item, BaseException):
      raise item
    return item


def _bka_compile_source(*, parent_bundle_dir: pathlib.Path):
  """Return a ``compile_source`` closure wired to the BKA spec
  + sample events. Each call uses a unique module_name so a
  pytest session running multiple measurement tests doesn't
  collide on ``sys.modules`` import cache."""
  from bigquery_agent_analytics.extractor_compilation import compile_extractor
  from tests.fixtures_extractor_compilation.bka_decision_inputs import BKA_FINGERPRINT_INPUTS
  from tests.fixtures_extractor_compilation.bka_decision_inputs import BKA_SAMPLE_EVENTS

  spec = _bka_resolved_spec()

  def _compile(plan, source: str):
    return compile_extractor(
        source=source,
        module_name=_unique_module_name(),
        function_name=plan.function_name,
        event_types=(plan.event_type,),
        sample_events=BKA_SAMPLE_EVENTS,
        spec=None,
        resolved_graph=spec,
        parent_bundle_dir=parent_bundle_dir,
        fingerprint_inputs=BKA_FINGERPRINT_INPUTS,
        template_version="v0.1",
        compiler_package_version="0.0.0",
        isolation=False,
    )

  return _compile


# ------------------------------------------------------------------ #
# Happy path: deterministic plan compiles, parity holds              #
# ------------------------------------------------------------------ #


class TestMeasureCompileBkaHappyPath:

  def test_first_try_compile_with_parity(self, tmp_path: pathlib.Path):
    from bigquery_agent_analytics.extractor_compilation import DETERMINISTIC_FAKE_MODEL
    from bigquery_agent_analytics.extractor_compilation import measure_compile
    from bigquery_agent_analytics.structured_extraction import extract_bka_decision_event
    from tests.fixtures_extractor_compilation.bka_decision_inputs import BKA_EVENT_SCHEMA
    from tests.fixtures_extractor_compilation.bka_decision_inputs import BKA_EXTRACTION_RULE
    from tests.fixtures_extractor_compilation.bka_decision_inputs import BKA_SAMPLE_EVENTS

    measurement = measure_compile(
        extraction_rule=BKA_EXTRACTION_RULE,
        event_schema=BKA_EVENT_SCHEMA,
        sample_events=BKA_SAMPLE_EVENTS,
        reference_extractor=extract_bka_decision_event,
        spec=None,
        llm_client=DeterministicBkaPlanClient(),
        compile_source=_bka_compile_source(parent_bundle_dir=tmp_path),
        captured_at="2026-05-06T00:00:00Z",
    )

    assert measurement.ok is True, (
        f"compile/parity failed: divergences={measurement.parity_divergences}, "
        f"attempt_failures={measurement.attempt_failures}, "
        f"reason={measurement.reason}"
    )
    assert measurement.n_attempts == 1
    assert measurement.reason == "succeeded"
    assert measurement.attempt_failures == ()
    assert measurement.bundle_fingerprint is not None
    assert len(measurement.bundle_fingerprint) == 64  # sha256 hex
    assert measurement.parity_ok is True
    assert measurement.parity_divergences == ()
    assert measurement.n_events == 2
    assert measurement.n_events_with_node_match == 2
    assert measurement.n_events_with_span_match == 2
    assert measurement.model_name == DETERMINISTIC_FAKE_MODEL
    assert measurement.source == "deterministic"
    assert measurement.sample_session_ids == ("sess1",)
    assert measurement.captured_at == "2026-05-06T00:00:00Z"


# ------------------------------------------------------------------ #
# Loop-failure path: malformed first response, exhaustion             #
# ------------------------------------------------------------------ #


class TestMeasureCompileLoopFailure:
  """measure_compile must return a populated CompileMeasurement
  even when the loop fails — callers route on ``ok`` /
  ``attempt_failures`` rather than catching exceptions."""

  def test_exhausted_loop_returns_failure_record(self, tmp_path: pathlib.Path):
    from bigquery_agent_analytics.extractor_compilation import measure_compile
    from bigquery_agent_analytics.structured_extraction import extract_bka_decision_event
    from tests.fixtures_extractor_compilation.bka_decision_inputs import BKA_EVENT_SCHEMA
    from tests.fixtures_extractor_compilation.bka_decision_inputs import BKA_EXTRACTION_RULE
    from tests.fixtures_extractor_compilation.bka_decision_inputs import BKA_SAMPLE_EVENTS

    # Both responses are missing required fields → parser rejects
    # both → loop exhausts.
    bad_response = {"event_type": "bka_decision"}
    llm = DeterministicBkaPlanClient(responses=[bad_response, bad_response])

    measurement = measure_compile(
        extraction_rule=BKA_EXTRACTION_RULE,
        event_schema=BKA_EVENT_SCHEMA,
        sample_events=BKA_SAMPLE_EVENTS,
        reference_extractor=extract_bka_decision_event,
        spec=None,
        llm_client=llm,
        compile_source=_bka_compile_source(parent_bundle_dir=tmp_path),
        max_attempts=2,
    )

    assert measurement.ok is False
    assert measurement.reason == "max_attempts_reached"
    assert measurement.n_attempts == 2
    assert measurement.bundle_fingerprint is None
    assert len(measurement.attempt_failures) == 2
    # Stable codes — the parser's ``missing_required_field``
    # propagates through the helper.
    for code in measurement.attempt_failures:
      assert code.startswith("plan_parse_error:")
    assert measurement.parity_ok is False
    assert measurement.parity_divergences == ()
    assert measurement.n_events == 2
    assert measurement.n_events_with_node_match == 0
    assert measurement.n_events_with_span_match == 0


# ------------------------------------------------------------------ #
# Parity divergence: compiled output differs from reference          #
# ------------------------------------------------------------------ #


class TestMeasureCompileParityDivergence:
  """The first plan compiles cleanly, but the compiled extractor's
  output differs from the reference. measure_compile must report
  ``parity_ok=False`` with a non-empty divergences tuple — *not*
  raise — so callers can route on the parity field."""

  def test_property_set_divergence_surfaces_in_record(
      self, tmp_path: pathlib.Path
  ):
    """Plan omits ``alternatives_considered`` from
    ``property_fields``; the reference handwritten extractor
    *does* carry it over from ``content`` when present (it's
    listed in the reference's carry-over loop), so on an event
    that has the field, parity should diverge.

    None of our default sample events carry
    ``alternatives_considered``, so we add one event that does.
    """
    from bigquery_agent_analytics.extractor_compilation import measure_compile
    from bigquery_agent_analytics.structured_extraction import extract_bka_decision_event
    from tests.fixtures_extractor_compilation.bka_decision_inputs import BKA_EVENT_SCHEMA
    from tests.fixtures_extractor_compilation.bka_decision_inputs import BKA_EXTRACTION_RULE

    plan_omitting_alternatives = {
        "event_type": "bka_decision",
        "target_entity_name": "mako_DecisionPoint",
        "function_name": "extract_bka_decision_event_compiled_partial",
        "key_field": {
            "property_name": "decision_id",
            "source_path": ["content", "decision_id"],
        },
        "property_fields": [
            {"property_name": "outcome", "source_path": ["content", "outcome"]},
            # alternatives_considered intentionally missing
        ],
        "session_id_path": ["session_id"],
        "span_handling": {
            "span_id_path": ["span_id"],
            "partial_when_path": ["content", "reasoning_text"],
        },
    }

    events_with_alternatives = [
        {
            "event_type": "bka_decision",
            "session_id": "sess1",
            "span_id": "span1",
            "content": {
                "decision_id": "d1",
                "outcome": "approved",
                "alternatives_considered": ["plan_A", "plan_B"],
            },
        }
    ]

    measurement = measure_compile(
        extraction_rule=BKA_EXTRACTION_RULE,
        event_schema=BKA_EVENT_SCHEMA,
        sample_events=events_with_alternatives,
        reference_extractor=extract_bka_decision_event,
        spec=None,
        llm_client=DeterministicBkaPlanClient(
            responses=[plan_omitting_alternatives]
        ),
        compile_source=_bka_compile_source(parent_bundle_dir=tmp_path),
    )

    # The compile loop succeeded — only parity should fail.
    assert measurement.reason == "succeeded"
    assert measurement.attempt_failures == ()
    assert measurement.bundle_fingerprint is not None

    # But the compiled extractor's properties are a strict subset
    # of the reference's, so parity reports a divergence.
    assert measurement.parity_ok is False
    assert measurement.ok is False  # rolls up to the top-level field
    assert measurement.n_events_with_node_match == 0
    assert any(
        "property set mismatch" in d for d in measurement.parity_divergences
    )

  def test_extractor_exception_becomes_parity_divergence(
      self, tmp_path: pathlib.Path
  ):
    """When either extractor crashes on an event, ``measure_compile``
    must capture the exception as a structured divergence rather
    than letting it propagate. Otherwise the utility's "always
    returns a populated CompileMeasurement" contract breaks for
    exactly the inputs callers most need a measurement on."""
    from bigquery_agent_analytics.extractor_compilation import measure_compile
    from tests.fixtures_extractor_compilation.bka_decision_inputs import BKA_EVENT_SCHEMA
    from tests.fixtures_extractor_compilation.bka_decision_inputs import BKA_EXTRACTION_RULE
    from tests.fixtures_extractor_compilation.bka_decision_inputs import BKA_SAMPLE_EVENTS

    def crashing_reference(event, spec):
      raise RuntimeError("reference exploded on this event")

    measurement = measure_compile(
        extraction_rule=BKA_EXTRACTION_RULE,
        event_schema=BKA_EVENT_SCHEMA,
        sample_events=BKA_SAMPLE_EVENTS,
        reference_extractor=crashing_reference,
        spec=None,
        llm_client=DeterministicBkaPlanClient(),
        compile_source=_bka_compile_source(parent_bundle_dir=tmp_path),
    )

    # Compile loop succeeded, parity didn't.
    assert measurement.reason == "succeeded"
    assert measurement.attempt_failures == ()
    assert measurement.bundle_fingerprint is not None
    assert measurement.parity_ok is False
    assert measurement.ok is False
    # Both events triggered the reference crash, so two divergences.
    assert len(measurement.parity_divergences) == 2
    for divergence in measurement.parity_divergences:
      assert "reference extractor raised RuntimeError" in divergence
      assert "reference exploded on this event" in divergence
    # Counters stay at 0 — neither axis was checkable.
    assert measurement.n_events_with_node_match == 0
    assert measurement.n_events_with_span_match == 0

  def test_compiled_extractor_exception_becomes_divergence_directly(self):
    """Symmetric branch: when the *compiled* extractor crashes on
    an event, the divergence string must say so. Tested at the
    helper level (``_compare_extractors``) rather than through a
    full ``measure_compile`` run — crafting a real BKA-shaped
    event that crashes the rendered extractor without also
    crashing the reference would require a synthetic plan that
    adds plumbing without coverage. The helper's contract is
    what matters; both try/except blocks are the same shape, and
    the public-API reference-crashes test above plus this
    direct-helper compiled-crashes test pin both branches."""
    from bigquery_agent_analytics.extractor_compilation import measurement as _measurement_module
    from bigquery_agent_analytics.structured_extraction import StructuredExtractionResult

    def good_reference(event, spec):
      return StructuredExtractionResult()

    def crashing_compiled(event, spec):
      raise ValueError(f"compiled extractor blew up on {event['span_id']}")

    result = _measurement_module._compare_extractors(
        reference=good_reference,
        compiled=crashing_compiled,
        events=[
            {"event_type": "x", "span_id": "spA"},
            {"event_type": "x", "span_id": "spB"},
        ],
        spec=None,
    )

    assert result.ok is False
    assert result.n_events_with_node_match == 0
    assert result.n_events_with_span_match == 0
    assert len(result.divergences) == 2
    for index, divergence in enumerate(result.divergences):
      assert (
          f"event[{index}]: compiled extractor raised ValueError" in divergence
      )
    assert "spA" in result.divergences[0]
    assert "spB" in result.divergences[1]


# ------------------------------------------------------------------ #
# CompileMeasurement JSON round-trip                                  #
# ------------------------------------------------------------------ #


class TestCompileMeasurementJson:

  def _example(self):
    from bigquery_agent_analytics.extractor_compilation import CompileMeasurement

    return CompileMeasurement(
        ok=True,
        n_attempts=1,
        reason="succeeded",
        bundle_fingerprint="a" * 64,
        attempt_failures=(),
        parity_ok=True,
        n_events=2,
        n_events_with_node_match=2,
        n_events_with_span_match=2,
        parity_divergences=(),
        captured_at="2026-05-06T00:00:00Z",
        model_name="deterministic-fake",
        source="deterministic",
        sample_session_ids=("sess1",),
    )

  def test_round_trip_byte_stable(self):
    measurement = self._example()
    encoded = measurement.to_json()
    decoded = type(measurement).from_json(encoded)
    assert decoded == measurement
    # Re-encoding the decoded value gives the same bytes.
    assert decoded.to_json() == encoded

  def test_to_json_keys_are_sorted(self):
    encoded = self._example().to_json()
    parsed = json.loads(encoded)
    keys = list(parsed.keys())
    assert keys == sorted(keys)

  def test_round_trip_failure_record(self):
    """A loop-failure measurement also round-trips cleanly."""
    from bigquery_agent_analytics.extractor_compilation import CompileMeasurement

    failure = CompileMeasurement(
        ok=False,
        n_attempts=2,
        reason="max_attempts_reached",
        bundle_fingerprint=None,
        attempt_failures=(
            "plan_parse_error:missing_required_field",
            "plan_parse_error:missing_required_field",
        ),
        parity_ok=False,
        n_events=2,
        n_events_with_node_match=0,
        n_events_with_span_match=0,
        parity_divergences=(),
        captured_at="2026-05-06T00:00:00Z",
        model_name="gemini-2.5-flash",
        source="live:proj.dataset.agent_events",
        sample_session_ids=("real-sess-A", "real-sess-B"),
    )
    encoded = failure.to_json()
    decoded = CompileMeasurement.from_json(encoded)
    assert decoded == failure


# ------------------------------------------------------------------ #
# CompileMeasurement.from_json — strict type checks                  #
# ------------------------------------------------------------------ #


class TestCompileMeasurementFromJsonStrictTypes:
  """``from_json`` is the artifact-schema lock; constructor-style
  coercion (``bool(\"false\") == True`` / ``tuple(\"abc\") ==
  (\"a\", \"b\", \"c\")`` / ``int(\"3\") == 3``) would let
  malformed JSON parse cleanly into well-typed-but-wrong Python
  values, weakening the lock. Each test passes a deliberately
  wrong-typed field and asserts ``TypeError`` with the field
  name in the message."""

  def _valid_payload_dict(self) -> dict:
    return {
        "ok": True,
        "n_attempts": 1,
        "reason": "succeeded",
        "bundle_fingerprint": "a" * 64,
        "attempt_failures": [],
        "parity_ok": True,
        "n_events": 2,
        "n_events_with_node_match": 2,
        "n_events_with_span_match": 2,
        "parity_divergences": [],
        "captured_at": "2026-05-07T00:00:00Z",
        "model_name": "deterministic-fake",
        "source": "deterministic",
        "sample_session_ids": ["sess1"],
    }

  def _from_json(self, payload: dict):
    from bigquery_agent_analytics.extractor_compilation import CompileMeasurement

    return CompileMeasurement.from_json(json.dumps(payload))

  def test_root_must_be_object(self):
    from bigquery_agent_analytics.extractor_compilation import CompileMeasurement

    with pytest.raises(TypeError, match="must be a JSON object"):
      CompileMeasurement.from_json(json.dumps([1, 2, 3]))

  def test_string_for_bool_field_rejected(self):
    """``bool('false')`` would silently be ``True`` under
    constructor coercion. The strict reader rejects it."""
    payload = self._valid_payload_dict()
    payload["ok"] = "false"
    with pytest.raises(TypeError, match="'ok' must be bool"):
      self._from_json(payload)

  def test_int_zero_for_bool_field_rejected(self):
    """``bool(0)`` is ``False`` — but ``0`` isn't valid JSON for
    a bool field. The reader rejects it so a malformed artifact
    can't paper over the schema."""
    payload = self._valid_payload_dict()
    payload["parity_ok"] = 0
    with pytest.raises(TypeError, match="'parity_ok' must be bool"):
      self._from_json(payload)

  def test_string_digit_for_int_field_rejected(self):
    payload = self._valid_payload_dict()
    payload["n_attempts"] = "1"
    with pytest.raises(TypeError, match="'n_attempts' must be int"):
      self._from_json(payload)

  def test_string_for_list_field_rejected(self):
    """``tuple('abc')`` would silently be ``('a', 'b', 'c')``."""
    payload = self._valid_payload_dict()
    payload["attempt_failures"] = "abc"
    with pytest.raises(
        TypeError, match="'attempt_failures' must be a JSON array"
    ):
      self._from_json(payload)

  def test_non_string_item_in_list_field_rejected(self):
    payload = self._valid_payload_dict()
    payload["sample_session_ids"] = ["sess1", 42, "sess3"]
    with pytest.raises(
        TypeError, match=r"'sample_session_ids'\[1\] must be str"
    ):
      self._from_json(payload)

  def test_int_for_str_field_rejected(self):
    payload = self._valid_payload_dict()
    payload["reason"] = 42
    with pytest.raises(TypeError, match="'reason' must be str"):
      self._from_json(payload)

  def test_int_for_optional_str_field_rejected(self):
    """``bundle_fingerprint`` is ``Optional[str]``: ``None`` is
    valid, but a non-string non-None must be rejected."""
    payload = self._valid_payload_dict()
    payload["bundle_fingerprint"] = 12345
    with pytest.raises(
        TypeError, match="'bundle_fingerprint' must be str or null"
    ):
      self._from_json(payload)

  def test_optional_str_field_accepts_null(self):
    """Sanity check: ``None`` is valid for ``Optional[str]``."""
    from bigquery_agent_analytics.extractor_compilation import CompileMeasurement

    payload = self._valid_payload_dict()
    payload["bundle_fingerprint"] = None
    payload["ok"] = False  # null fingerprint pairs with failed compile
    decoded = CompileMeasurement.from_json(json.dumps(payload))
    assert decoded.bundle_fingerprint is None

  def test_unknown_field_rejected(self):
    """A stale or accidental extra field has to fail loudly —
    otherwise the artifact accumulates dead keys that don't
    surface in code review and the schema lock isn't a lock."""
    payload = self._valid_payload_dict()
    payload["accidental_field"] = "drift"
    with pytest.raises(
        TypeError, match=r"unknown fields: \['accidental_field'\]"
    ):
      self._from_json(payload)

  def test_missing_field_rejected(self):
    """A renamed-or-dropped field has to fail loudly. Without
    this check, ``data.get(field)`` would silently treat a
    missing ``bundle_fingerprint`` as ``None`` (a valid
    ``Optional[str]`` value) — the schema would drift without
    review noticing."""
    payload = self._valid_payload_dict()
    del payload["bundle_fingerprint"]
    with pytest.raises(
        TypeError, match=r"missing fields: \['bundle_fingerprint'\]"
    ):
      self._from_json(payload)

  def test_both_missing_and_unknown_reported_together(self):
    """When the artifact has both shapes of drift, the error
    message should name both so a single fix-it round suffices."""
    payload = self._valid_payload_dict()
    payload["accidental_field"] = "drift"
    del payload["captured_at"]
    with pytest.raises(TypeError) as excinfo:
      self._from_json(payload)
    msg = str(excinfo.value)
    assert "missing fields: ['captured_at']" in msg
    assert "unknown fields: ['accidental_field']" in msg


# ------------------------------------------------------------------ #
# Fingerprint coverage of extractor's emitted fields                  #
# ------------------------------------------------------------------ #


class TestBkaFingerprintInputsCoverage:
  """``BKA_FINGERPRINT_INPUTS`` is the basis for the bundle
  fingerprint that C2 will use as "this bundle matches these
  active inputs." If a real compile-input change for a field the
  extractor emits (e.g., the ``alternatives_considered`` carry-
  over property) doesn't move the fingerprint, the contract
  fails silently. These tests pin the coverage."""

  def _baseline_fingerprint(self) -> str:
    import copy

    from bigquery_agent_analytics.extractor_compilation import compute_fingerprint
    from tests.fixtures_extractor_compilation.bka_decision_inputs import BKA_FINGERPRINT_INPUTS

    return compute_fingerprint(
        template_version="v0.1",
        compiler_package_version="0.0.0",
        **copy.deepcopy(BKA_FINGERPRINT_INPUTS),
    )

  def test_changing_event_schema_changes_fingerprint(self):
    """Removing ``alternatives_considered`` from the event_schema
    must produce a different fingerprint. Otherwise a real
    compile-input change for an extractor-relevant field doesn't
    move the bundle's hash."""
    import copy

    from bigquery_agent_analytics.extractor_compilation import compute_fingerprint
    from tests.fixtures_extractor_compilation.bka_decision_inputs import BKA_FINGERPRINT_INPUTS

    altered = copy.deepcopy(BKA_FINGERPRINT_INPUTS)
    del altered["event_schema"]["bka_decision"]["content"][
        "alternatives_considered"
    ]
    altered_fp = compute_fingerprint(
        template_version="v0.1",
        compiler_package_version="0.0.0",
        **altered,
    )
    assert altered_fp != self._baseline_fingerprint()

  def test_changing_extraction_rules_changes_fingerprint(self):
    """Removing ``alternatives_considered`` from
    ``extraction_rules.bka_decision.property_fields`` must also
    move the fingerprint — the rule itself is what tells the
    compile pipeline which fields to emit."""
    import copy

    from bigquery_agent_analytics.extractor_compilation import compute_fingerprint
    from tests.fixtures_extractor_compilation.bka_decision_inputs import BKA_FINGERPRINT_INPUTS

    altered = copy.deepcopy(BKA_FINGERPRINT_INPUTS)
    altered["extraction_rules"]["bka_decision"]["property_fields"] = [
        f
        for f in altered["extraction_rules"]["bka_decision"]["property_fields"]
        if f["name"] != "alternatives_considered"
    ]
    altered_fp = compute_fingerprint(
        template_version="v0.1",
        compiler_package_version="0.0.0",
        **altered,
    )
    assert altered_fp != self._baseline_fingerprint()

  def test_changing_span_handling_changes_fingerprint(self):
    """``span_handling.partial_when_path`` is the rule that picks
    the partial-vs-full branch in the rendered extractor; moving
    that path must move the fingerprint."""
    import copy

    from bigquery_agent_analytics.extractor_compilation import compute_fingerprint
    from tests.fixtures_extractor_compilation.bka_decision_inputs import BKA_FINGERPRINT_INPUTS

    altered = copy.deepcopy(BKA_FINGERPRINT_INPUTS)
    altered["extraction_rules"]["bka_decision"]["span_handling"][
        "partial_when_path"
    ] = ["content", "different_field"]
    altered_fp = compute_fingerprint(
        template_version="v0.1",
        compiler_package_version="0.0.0",
        **altered,
    )
    assert altered_fp != self._baseline_fingerprint()


# ------------------------------------------------------------------ #
# Audit fields                                                        #
# ------------------------------------------------------------------ #


class TestMeasurementAuditFields:

  def test_session_ids_deduplicated_in_iteration_order(
      self, tmp_path: pathlib.Path
  ):
    """Multiple events with overlapping session_ids should produce
    a deduplicated tuple in iteration order — useful for live
    runs where samples may include repeats."""
    from bigquery_agent_analytics.extractor_compilation import measure_compile
    from bigquery_agent_analytics.structured_extraction import extract_bka_decision_event
    from tests.fixtures_extractor_compilation.bka_decision_inputs import BKA_EVENT_SCHEMA
    from tests.fixtures_extractor_compilation.bka_decision_inputs import BKA_EXTRACTION_RULE

    events = [
        {
            "event_type": "bka_decision",
            "session_id": "B",
            "span_id": "spB",
            "content": {"decision_id": "dB"},
        },
        {
            "event_type": "bka_decision",
            "session_id": "A",
            "span_id": "spA1",
            "content": {"decision_id": "dA1"},
        },
        {
            "event_type": "bka_decision",
            "session_id": "A",
            "span_id": "spA2",
            "content": {"decision_id": "dA2"},
        },
        {
            "event_type": "bka_decision",
            "session_id": "",  # empty — must be filtered
            "span_id": "spX",
            "content": {"decision_id": "dX"},
        },
    ]

    measurement = measure_compile(
        extraction_rule=BKA_EXTRACTION_RULE,
        event_schema=BKA_EVENT_SCHEMA,
        sample_events=events,
        reference_extractor=extract_bka_decision_event,
        spec=None,
        llm_client=DeterministicBkaPlanClient(),
        compile_source=_bka_compile_source(parent_bundle_dir=tmp_path),
    )

    # B comes first (iteration order), then A, then dedup, and the
    # empty-string session_id is filtered.
    assert measurement.sample_session_ids == ("B", "A")

  def test_checked_in_artifact_round_trips_into_compile_measurement(self):
    """The committed
    ``tests/fixtures_extractor_compilation/bka_decision_measurement_report.json``
    must parse cleanly via ``CompileMeasurement.from_json`` and
    represent a successful measurement.

    Reviewers can look at the JSON and see the measurement
    contract without re-running the test suite; this assertion
    keeps the artifact and the dataclass schema from drifting.
    """
    from bigquery_agent_analytics.extractor_compilation import CompileMeasurement

    artifact = (
        pathlib.Path(__file__).parent
        / "fixtures_extractor_compilation"
        / "bka_decision_measurement_report.json"
    )
    measurement = CompileMeasurement.from_json(artifact.read_text())
    assert measurement.ok is True
    assert measurement.parity_ok is True
    assert measurement.parity_divergences == ()
    assert measurement.attempt_failures == ()
    # Bundle fingerprint is sha256 hex (locked at 64 chars; the
    # exact value can drift across compiler version bumps and is
    # regenerated by the live run).
    assert measurement.bundle_fingerprint is not None
    assert len(measurement.bundle_fingerprint) == 64

  def test_model_name_and_source_are_passed_through(
      self, tmp_path: pathlib.Path
  ):
    from bigquery_agent_analytics.extractor_compilation import measure_compile
    from bigquery_agent_analytics.structured_extraction import extract_bka_decision_event
    from tests.fixtures_extractor_compilation.bka_decision_inputs import BKA_EVENT_SCHEMA
    from tests.fixtures_extractor_compilation.bka_decision_inputs import BKA_EXTRACTION_RULE
    from tests.fixtures_extractor_compilation.bka_decision_inputs import BKA_SAMPLE_EVENTS

    measurement = measure_compile(
        extraction_rule=BKA_EXTRACTION_RULE,
        event_schema=BKA_EVENT_SCHEMA,
        sample_events=BKA_SAMPLE_EVENTS,
        reference_extractor=extract_bka_decision_event,
        spec=None,
        llm_client=DeterministicBkaPlanClient(),
        compile_source=_bka_compile_source(parent_bundle_dir=tmp_path),
        model_name="test-model-name",
        source="test:custom-source-string",
    )

    assert measurement.model_name == "test-model-name"
    assert measurement.source == "test:custom-source-string"
