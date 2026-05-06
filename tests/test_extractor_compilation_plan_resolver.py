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

"""Tests for the LLM-driven plan resolver (issue #75 PR 4b.2.2.b).

Coverage:
- ``build_resolution_prompt`` is deterministic — same inputs
  produce byte-identical output, dict insertion order doesn't
  matter.
- The prompt grounds the LLM in the rule, the schema, the
  output contract, identifier-safety constraints, and the
  "no hallucinated paths" mapping rule.
- ``PlanResolver.resolve`` wires prompt → LLM call → parser:
  schema is passed through verbatim; canonical BKA response
  resolves to the expected plan; parser failures propagate
  unchanged with ``code``/``path``; LLM client exceptions are
  not swallowed.

The resolver is **not** exercised against a real LLM here. PR
4b.2.2.c adds retry-on-gate-failure orchestration on top; PR 4c
introduces concrete provider adapters.
"""

from __future__ import annotations

import json
import pathlib

import pytest

# ------------------------------------------------------------------ #
# Shared fixtures                                                     #
# ------------------------------------------------------------------ #


_BKA_PLAN_FIXTURE_PATH = (
    pathlib.Path(__file__).parent
    / "fixtures_extractor_compilation"
    / "plan_bka_decision.json"
)


def _bka_response_dict() -> dict:
  return json.loads(_BKA_PLAN_FIXTURE_PATH.read_text(encoding="utf-8"))


def _bka_handwritten_plan():
  from bigquery_agent_analytics.extractor_compilation import FieldMapping
  from bigquery_agent_analytics.extractor_compilation import ResolvedExtractorPlan
  from bigquery_agent_analytics.extractor_compilation import SpanHandlingRule

  return ResolvedExtractorPlan(
      event_type="bka_decision",
      target_entity_name="mako_DecisionPoint",
      function_name="extract_bka_decision_event_compiled",
      key_field=FieldMapping(
          property_name="decision_id",
          source_path=("content", "decision_id"),
      ),
      property_fields=(
          FieldMapping("outcome", ("content", "outcome")),
          FieldMapping("confidence", ("content", "confidence")),
          FieldMapping(
              "alternatives_considered",
              ("content", "alternatives_considered"),
          ),
      ),
      session_id_path=("session_id",),
      span_handling=SpanHandlingRule(
          span_id_path=("span_id",),
          partial_when_path=("content", "reasoning_text"),
      ),
  )


def _bka_extraction_rule() -> dict:
  return {
      "event_type": "bka_decision",
      "target_entity": "mako_DecisionPoint",
      "key_field_hint": "decision_id",
      "function_name": "extract_bka_decision_event_compiled",
  }


def _bka_event_schema() -> dict:
  return {
      "bka_decision": {
          "session_id": "string",
          "span_id": "string",
          "content": {
              "decision_id": "string",
              "outcome": "string",
              "confidence": "double",
              "alternatives_considered": "string",
              "reasoning_text": "string",
          },
      }
  }


class _RecordingLLMClient:
  """Test fake that records every call's prompt + schema and
  returns a pre-canned response. Implements the ``LLMClient``
  protocol structurally."""

  def __init__(self, response):
    self.response = response
    self.calls: list[dict] = []

  def generate_json(self, prompt: str, schema: dict) -> dict:
    self.calls.append({"prompt": prompt, "schema": schema})
    return self.response


class _RaisingLLMClient:
  """Test fake that always raises. Implements ``LLMClient``
  structurally."""

  def __init__(self, exc: BaseException):
    self.exc = exc

  def generate_json(self, prompt: str, schema: dict) -> dict:
    raise self.exc


# ------------------------------------------------------------------ #
# build_resolution_prompt                                             #
# ------------------------------------------------------------------ #


class TestBuildResolutionPrompt:

  def test_deterministic_for_same_inputs(self):
    """Same inputs → byte-identical output. Locks down that the
    prompt itself doesn't add nondeterminism beyond the LLM call."""
    from bigquery_agent_analytics.extractor_compilation import build_resolution_prompt

    a = build_resolution_prompt(_bka_extraction_rule(), _bka_event_schema())
    b = build_resolution_prompt(_bka_extraction_rule(), _bka_event_schema())
    assert a == b

  def test_dict_insertion_order_doesnt_change_output(self):
    """``sort_keys=True`` on every JSON serialization means dict
    insertion order in the inputs is irrelevant."""
    from bigquery_agent_analytics.extractor_compilation import build_resolution_prompt

    rule_a = {"a": 1, "b": 2, "c": 3}
    rule_b = {"c": 3, "b": 2, "a": 1}
    schema = {"x": {"y": "string"}}
    assert build_resolution_prompt(rule_a, schema) == build_resolution_prompt(
        rule_b, schema
    )

  def test_prompt_grounds_in_rule_and_schema(self):
    """The prompt must surface both the rule and the schema verbatim
    (in JSON form) so the LLM has the actual fields to map."""
    from bigquery_agent_analytics.extractor_compilation import build_resolution_prompt

    rule = _bka_extraction_rule()
    schema = _bka_event_schema()
    prompt = build_resolution_prompt(rule, schema)
    # extraction rule is in the prompt
    assert "bka_decision" in prompt
    assert "mako_DecisionPoint" in prompt
    # event schema is in the prompt
    assert "alternatives_considered" in prompt
    assert "reasoning_text" in prompt

  def test_prompt_includes_output_contract(self):
    """The exported JSON Schema must be embedded so providers
    that don't support structured-output mode still see the
    contract."""
    from bigquery_agent_analytics.extractor_compilation import build_resolution_prompt

    prompt = build_resolution_prompt(
        _bka_extraction_rule(), _bka_event_schema()
    )
    assert "ResolvedExtractorPlan" in prompt
    assert "additionalProperties" in prompt
    assert "FieldMapping" in prompt

  def test_prompt_includes_no_hallucinated_paths_rule(self):
    """The mapping-rules section is what stops the LLM from
    inventing paths the event_schema doesn't have."""
    from bigquery_agent_analytics.extractor_compilation import build_resolution_prompt

    prompt = build_resolution_prompt(
        _bka_extraction_rule(), _bka_event_schema()
    )
    assert "ONLY paths that exist in the event_schema" in prompt
    assert "Do NOT" in prompt

  @pytest.mark.parametrize(
      "label, payload",
      [
          ("rule with set", {"key": {1, 2, 3}}),
          ("rule with custom class", {"key": object()}),
          ("rule itself a set", {1, 2, 3}),
      ],
  )
  def test_non_json_serializable_extraction_rule_raises_clear_typeerror(
      self, label, payload
  ):
    """The public contract is "JSON-serializable mappings".
    Anything ``json.dumps`` can't handle (set, custom class,
    Pydantic model, etc.) gets a clear ``TypeError`` naming the
    offending field and the contract — not the bare default
    ``Object of type X is not JSON serializable``."""
    from bigquery_agent_analytics.extractor_compilation import build_resolution_prompt

    with pytest.raises(TypeError) as exc_info:
      build_resolution_prompt(payload, _bka_event_schema())
    assert "extraction_rule" in str(exc_info.value)
    assert "JSON-serializable" in str(exc_info.value)

  @pytest.mark.parametrize("bad_root", [["a", "b"], "string", 123, 3.14, True])
  def test_extraction_rule_must_be_a_mapping_at_the_root(self, bad_root):
    """Wrong root type — list, primitive — is JSON-serializable
    but breaks the prompt's anchoring contract: the
    no-hallucinated-paths rule needs a key→value structure to
    reference. Reject at the boundary."""
    from bigquery_agent_analytics.extractor_compilation import build_resolution_prompt

    with pytest.raises(TypeError) as exc_info:
      build_resolution_prompt(bad_root, _bka_event_schema())
    assert "extraction_rule" in str(exc_info.value)
    assert "mapping" in str(exc_info.value)

  @pytest.mark.parametrize("bad_root", [["a", "b"], "string", 123, 3.14, True])
  def test_event_schema_must_be_a_mapping_at_the_root(self, bad_root):
    from bigquery_agent_analytics.extractor_compilation import build_resolution_prompt

    with pytest.raises(TypeError) as exc_info:
      build_resolution_prompt(_bka_extraction_rule(), bad_root)
    assert "event_schema" in str(exc_info.value)
    assert "mapping" in str(exc_info.value)

  @pytest.mark.parametrize(
      "label, payload",
      [
          ("non-string root key", {1: "intent"}),
          (
              "non-string nested key",
              {"outer": {2: "value"}},
          ),
          ("non-string key inside list", {"xs": [{3: "v"}]}),
      ],
  )
  def test_non_string_mapping_keys_rejected(self, label, payload):
    """``json.dumps`` silently coerces non-string keys
    (``{1: "x"}`` → ``{"1": "x"}``). For an API whose inputs are
    JSON object contracts, that coercion would let the LLM see a
    key the caller never wrote. Reject at the boundary,
    recursively, so root and nested non-string keys both fail."""
    from bigquery_agent_analytics.extractor_compilation import build_resolution_prompt

    with pytest.raises(TypeError) as exc_info:
      build_resolution_prompt(payload, _bka_event_schema())
    assert "mapping keys must be strings" in str(exc_info.value)

  def test_non_string_keys_in_event_schema_rejected(self):
    from bigquery_agent_analytics.extractor_compilation import build_resolution_prompt

    with pytest.raises(TypeError) as exc_info:
      build_resolution_prompt(
          _bka_extraction_rule(),
          {"bka_decision": {1: "string"}},
      )
    assert "mapping keys must be strings" in str(exc_info.value)

  @pytest.mark.parametrize(
      "bad_value", [float("nan"), float("inf"), float("-inf")]
  )
  def test_non_finite_floats_rejected(self, bad_value):
    """``json.dumps(float('nan'))`` defaults to ``NaN`` which
    isn't valid JSON per RFC 8259 — some structured-output
    providers reject it outright. The boundary now passes
    ``allow_nan=False`` and the wrapper translates the resulting
    ``ValueError`` into a clear ``TypeError``."""
    from bigquery_agent_analytics.extractor_compilation import build_resolution_prompt

    with pytest.raises(TypeError) as exc_info:
      build_resolution_prompt({"value": bad_value}, _bka_event_schema())
    assert "JSON-serializable" in str(exc_info.value)

  def test_non_json_serializable_event_schema_raises_clear_typeerror(self):
    from bigquery_agent_analytics.extractor_compilation import build_resolution_prompt

    with pytest.raises(TypeError) as exc_info:
      build_resolution_prompt(
          _bka_extraction_rule(),
          {"x": {"y": {1, 2}}},  # set, not serializable
      )
    assert "event_schema" in str(exc_info.value)
    assert "JSON-serializable" in str(exc_info.value)

  def test_prompt_includes_identifier_safety_rules(self):
    from bigquery_agent_analytics.extractor_compilation import build_resolution_prompt

    prompt = build_resolution_prompt(
        _bka_extraction_rule(), _bka_event_schema()
    )
    assert "plain Python identifier" in prompt
    # The forbidden function-name list is generated from the
    # call-target allowlist; check a couple of representative
    # entries.
    assert "ExtractedNode" in prompt
    assert "isinstance" in prompt
    assert "len" in prompt


# ------------------------------------------------------------------ #
# PlanResolver.resolve                                                #
# ------------------------------------------------------------------ #


class TestPlanResolver:

  def test_bka_response_resolves_to_expected_plan(self):
    """End-to-end: fake client returns the canonical BKA JSON,
    resolver hands back the same dataclass the parser tests
    construct by hand."""
    from bigquery_agent_analytics.extractor_compilation import PlanResolver

    fake = _RecordingLLMClient(_bka_response_dict())
    resolver = PlanResolver(fake)
    plan = resolver.resolve(_bka_extraction_rule(), _bka_event_schema())
    assert plan == _bka_handwritten_plan()

  def test_schema_passed_through_to_client_by_value(self):
    """The resolver passes a copy of the exported schema (not
    the global itself) so adapters can normalize provider-
    specific quirks in place without mutating the module global.
    ``==`` equality, not ``is`` identity."""
    from bigquery_agent_analytics.extractor_compilation import PlanResolver
    from bigquery_agent_analytics.extractor_compilation import RESOLVED_EXTRACTOR_PLAN_JSON_SCHEMA

    fake = _RecordingLLMClient(_bka_response_dict())
    resolver = PlanResolver(fake)
    resolver.resolve(_bka_extraction_rule(), _bka_event_schema())
    assert len(fake.calls) == 1
    received = fake.calls[0]["schema"]
    assert received == RESOLVED_EXTRACTOR_PLAN_JSON_SCHEMA
    assert received is not RESOLVED_EXTRACTOR_PLAN_JSON_SCHEMA

  def test_client_mutation_does_not_affect_global_schema(self):
    """Adapters that normalize provider-specific schema quirks
    (Gemini's ``response_schema`` doesn't accept ``$schema`` /
    ``$defs``, etc.) sometimes mutate their input. The resolver
    deep-copies the exported global so a misbehaving adapter
    can't poison every future caller in the process. Verified
    concretely with a fake client that mutates its received
    schema."""
    import copy

    from bigquery_agent_analytics.extractor_compilation import PlanResolver
    from bigquery_agent_analytics.extractor_compilation import RESOLVED_EXTRACTOR_PLAN_JSON_SCHEMA

    snapshot = copy.deepcopy(RESOLVED_EXTRACTOR_PLAN_JSON_SCHEMA)

    class _MutatingClient:

      def __init__(self, response):
        self.response = response

      def generate_json(self, prompt, schema):
        # An adapter "normalizing for provider X" by adding
        # vendor-specific keys. With a deep copy this affects
        # only the local schema; without one it would leak into
        # the module global.
        schema["__vendor_quirk__"] = True
        schema["properties"]["__quirk__"] = {"type": "boolean"}
        return self.response

    resolver = PlanResolver(_MutatingClient(_bka_response_dict()))
    resolver.resolve(_bka_extraction_rule(), _bka_event_schema())

    assert (
        RESOLVED_EXTRACTOR_PLAN_JSON_SCHEMA == snapshot
    ), "client mutation leaked into the module-global schema"

  def test_prompt_passed_through_matches_builder(self):
    """The prompt the resolver hands to the client must equal
    what ``build_resolution_prompt`` produces directly."""
    from bigquery_agent_analytics.extractor_compilation import build_resolution_prompt
    from bigquery_agent_analytics.extractor_compilation import PlanResolver

    rule = _bka_extraction_rule()
    schema = _bka_event_schema()
    expected_prompt = build_resolution_prompt(rule, schema)

    fake = _RecordingLLMClient(_bka_response_dict())
    resolver = PlanResolver(fake)
    resolver.resolve(rule, schema)
    assert fake.calls[0]["prompt"] == expected_prompt

  def test_parser_failure_propagates_with_structured_code(self):
    """The LLM returns a malformed response (missing required
    field). The parser's ``PlanParseError`` propagates verbatim
    so future retry orchestration (PR 4b.2.2.c) can route on
    ``code`` and ``path``."""
    from bigquery_agent_analytics.extractor_compilation import PlanParseError
    from bigquery_agent_analytics.extractor_compilation import PlanResolver

    bad_response = {
        # missing target_entity_name + function_name + key_field
        "event_type": "bka_decision",
    }
    fake = _RecordingLLMClient(bad_response)
    resolver = PlanResolver(fake)
    with pytest.raises(PlanParseError) as exc_info:
      resolver.resolve(_bka_extraction_rule(), _bka_event_schema())
    assert exc_info.value.code == "missing_required_field"
    assert exc_info.value.path  # non-empty dotted path

  def test_parser_semantic_failure_propagates(self):
    """The LLM returns structurally-valid but semantically-bad
    JSON (function_name shadows ``len``). The parser's
    ``invalid_identifier`` propagates."""
    from bigquery_agent_analytics.extractor_compilation import PlanParseError
    from bigquery_agent_analytics.extractor_compilation import PlanResolver

    bad_response = {
        "event_type": "bka_decision",
        "target_entity_name": "mako_DecisionPoint",
        "function_name": "len",  # shadows allowlist
        "key_field": {
            "property_name": "decision_id",
            "source_path": ["content", "decision_id"],
        },
    }
    fake = _RecordingLLMClient(bad_response)
    resolver = PlanResolver(fake)
    with pytest.raises(PlanParseError) as exc_info:
      resolver.resolve(_bka_extraction_rule(), _bka_event_schema())
    assert exc_info.value.code == "invalid_identifier"
    assert exc_info.value.path == "function_name"

  def test_llm_client_exception_not_swallowed(self):
    """Anything the LLM client raises propagates unchanged. The
    resolver doesn't wrap transport / quota / auth errors —
    that's a deliberate design choice; PR 4b.2.2.c can layer
    typed retry on top."""
    from bigquery_agent_analytics.extractor_compilation import PlanResolver

    fake = _RaisingLLMClient(RuntimeError("LLM unreachable"))
    resolver = PlanResolver(fake)
    with pytest.raises(RuntimeError, match="LLM unreachable"):
      resolver.resolve(_bka_extraction_rule(), _bka_event_schema())

  def test_llm_client_baseexception_not_swallowed(self):
    """Even ``KeyboardInterrupt`` / ``SystemExit`` propagate —
    we only catch what the parser raises."""
    from bigquery_agent_analytics.extractor_compilation import PlanResolver

    fake = _RaisingLLMClient(KeyboardInterrupt())
    resolver = PlanResolver(fake)
    with pytest.raises(KeyboardInterrupt):
      resolver.resolve(_bka_extraction_rule(), _bka_event_schema())
