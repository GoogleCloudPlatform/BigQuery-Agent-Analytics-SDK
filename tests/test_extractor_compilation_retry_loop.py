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

"""Tests for the retry-on-gate-failure orchestrator (#75 PR 4b.2.2.c.2).

Strategy:

* A ``FakeLLMClient`` returns canned dict responses in a fixed
  sequence; ``call_log`` lets tests inspect the prompts.
* A ``StubCompileSource`` returns canned ``CompileResult`` instances
  per call so we exercise the loop's failure-routing without
  touching the filesystem.
* One end-to-end test wires the real ``compile_extractor`` with a
  tiny graph spec and sample events to prove the wiring still
  works through the real component stack.
"""

from __future__ import annotations

import pathlib

import pytest

# ------------------------------------------------------------------ #
# Test fixtures                                                       #
# ------------------------------------------------------------------ #


def _valid_plan_dict(*, event_type: str = "bka_decision") -> dict:
  """A minimal-but-valid ``ResolvedExtractorPlan`` JSON shape."""
  return {
      "event_type": event_type,
      "target_entity_name": "DecisionPoint",
      "function_name": "extract_decision_compiled",
      "key_field": {
          "property_name": "decision_id",
          "source_path": ["content", "decision_id"],
      },
  }


def _missing_required_field_dict() -> dict:
  """A response shape that triggers ``PlanParseError`` (missing
  required fields). Used to exercise the parser-failure retry
  branch."""
  return {"event_type": "bka_decision"}


class FakeLLMClient:
  """LLM client stand-in that returns canned dicts in order.

  ``call_log`` captures every ``(prompt, schema)`` pair so tests
  can assert prompt contents and call ordering without depending
  on a real LLM.
  """

  def __init__(self, responses: list) -> None:
    self._responses = list(responses)
    self._index = 0
    self.call_log: list[tuple[str, dict]] = []

  def generate_json(self, prompt: str, schema: dict) -> dict:
    self.call_log.append((prompt, schema))
    if self._index >= len(self._responses):
      raise AssertionError(
          f"FakeLLMClient ran out of responses after {self._index} "
          f"calls; test likely set up the wrong sequence"
      )
    item = self._responses[self._index]
    self._index += 1
    if isinstance(item, BaseException):
      raise item
    return item


def _make_ok_compile_result():
  """Build a fake successful ``CompileResult``. The orchestrator
  only inspects ``ok``, ``manifest``, and ``bundle_dir``, so we
  don't need to populate the gate reports."""
  from bigquery_agent_analytics.extractor_compilation import AstReport
  from bigquery_agent_analytics.extractor_compilation import CompileResult
  from bigquery_agent_analytics.extractor_compilation import Manifest
  from bigquery_agent_analytics.extractor_compilation import SmokeTestReport

  manifest = Manifest(
      fingerprint="f" * 64,
      event_types=("bka_decision",),
      module_filename="extract_decision.py",
      function_name="extract_decision_compiled",
      compiler_package_version="0.0.1",
      template_version="t-1",
      transcript_builder_version="tb-1",
      created_at="2026-05-06T00:00:00Z",
  )
  smoke = SmokeTestReport(
      events_processed=1,
      events_with_exception=0,
      exceptions=(),
      events_with_wrong_return_type=0,
      wrong_return_types=(),
      events_with_nonempty_result=1,
      min_nonempty_results=1,
      validation_failures=(),
      nonempty_event_types=("bka_decision",),
  )
  return CompileResult(
      manifest=manifest,
      ast_report=AstReport(),
      smoke_report=smoke,
      bundle_dir=pathlib.Path("/tmp/bundle"),
  )


def _make_failing_compile_result(*, invalid_event_types: str):
  """Build a fake failing ``CompileResult`` with the
  ``invalid_event_types`` field populated — the most common
  retry-loop failure shape (LLM declared the wrong event_type)."""
  from bigquery_agent_analytics.extractor_compilation import AstReport
  from bigquery_agent_analytics.extractor_compilation import CompileResult

  return CompileResult(
      manifest=None,
      ast_report=AstReport(),
      smoke_report=None,
      bundle_dir=None,
      invalid_event_types=invalid_event_types,
  )


class StubCompileSource:
  """Returns canned ``CompileResult`` instances per call.

  Allows tests to drive the loop's failure-routing without
  invoking the real ``compile_extractor`` (which would need
  sample events, a resolved graph, and a writable bundle dir).
  """

  def __init__(self, results: list) -> None:
    self._results = list(results)
    self._index = 0
    self.call_log: list = []

  def __call__(self, plan, source: str):
    self.call_log.append((plan, source))
    result = self._results[self._index]
    self._index += 1
    return result


# ------------------------------------------------------------------ #
# build_retry_prompt                                                  #
# ------------------------------------------------------------------ #


class TestBuildRetryPrompt:

  def test_includes_original_prompt_verbatim(self):
    from bigquery_agent_analytics.extractor_compilation import build_retry_prompt

    out = build_retry_prompt(
        original_prompt="ORIGINAL_GROUNDING",
        prior_response={"event_type": "x"},
        diagnostic="PlanParseError [code=missing_required_field] at "
        "function_name: required field 'function_name' is missing",
    )
    assert "ORIGINAL_GROUNDING" in out

  def test_serializes_prior_response_with_sorted_keys(self):
    """Two semantically-equal dicts must produce byte-identical
    retry prompts — matters for any future fingerprint /
    cache layer that keys on prompt bytes."""
    from bigquery_agent_analytics.extractor_compilation import build_retry_prompt

    a = build_retry_prompt(
        original_prompt="P",
        prior_response={"b": 2, "a": 1},
        diagnostic="D",
    )
    b = build_retry_prompt(
        original_prompt="P",
        prior_response={"a": 1, "b": 2},
        diagnostic="D",
    )
    assert a == b

  def test_includes_diagnostic_verbatim(self):
    from bigquery_agent_analytics.extractor_compilation import build_retry_prompt

    diagnostic = "CompileError [code=invalid_event_types]: declared event_types ['x'] have no matching sample events"
    out = build_retry_prompt(
        original_prompt="P", prior_response={"a": 1}, diagnostic=diagnostic
    )
    assert diagnostic in out

  def test_handles_non_dict_prior_response(self):
    """When the parser rejected the response shape itself, the
    LLM may have emitted a list or string. The retry prompt
    still echoes it back so the LLM sees what it produced."""
    from bigquery_agent_analytics.extractor_compilation import build_retry_prompt

    out = build_retry_prompt(
        original_prompt="P",
        prior_response=["not", "an", "object"],
        diagnostic="D",
    )
    # JSON list is in the prompt
    assert '["not"' in out or '"not"' in out

  def test_byte_stable_for_same_inputs(self):
    from bigquery_agent_analytics.extractor_compilation import build_retry_prompt

    a = build_retry_prompt(
        original_prompt="P", prior_response={"a": 1}, diagnostic="D"
    )
    b = build_retry_prompt(
        original_prompt="P", prior_response={"a": 1}, diagnostic="D"
    )
    assert a == b

  def test_handles_mixed_type_keys_without_crashing(self):
    """``json.dumps(..., sort_keys=True)`` raises ``TypeError`` on
    a dict with mixed-type keys (Python can't compare ``str < int``).
    The parser explicitly handles such responses as recoverable
    failures (mixed-type keys land as ``unknown_field`` etc.), so
    the retry-prompt builder must NOT crash on them — otherwise a
    recoverable failure becomes an unrecoverable one frame above."""
    from bigquery_agent_analytics.extractor_compilation import build_retry_prompt

    # No exception. Output must include both keys so the LLM can
    # see what it produced.
    out = build_retry_prompt(
        original_prompt="P",
        prior_response={1: "bad", "event_type": "bka_decision"},
        diagnostic="D",
    )
    assert "bka_decision" in out
    assert "bad" in out

  def test_handles_circular_reference_without_crashing(self):
    """``json.dumps`` raises ``ValueError`` (not ``TypeError``)
    for circular references. The serialization helper has to fall
    through to ``repr()`` rather than escaping the exception —
    otherwise a pathological LLM client (or a test fake that
    accidentally constructs a cycle) crashes the retry loop one
    frame above the parser's structured rejection."""
    from bigquery_agent_analytics.extractor_compilation import build_retry_prompt

    cyclic = {"event_type": "bka_decision"}
    cyclic["self"] = cyclic  # circular

    # No exception, even though sort_keys AND insertion-order
    # json.dumps both raise ValueError on this input.
    out = build_retry_prompt(
        original_prompt="P", prior_response=cyclic, diagnostic="D"
    )
    # Final fallback (repr) is always renderable.
    assert "bka_decision" in out


# ------------------------------------------------------------------ #
# compile_with_llm — argument validation                              #
# ------------------------------------------------------------------ #


class TestCompileWithLlmArgs:

  def test_max_attempts_zero_raises_value_error(self):
    from bigquery_agent_analytics.extractor_compilation import compile_with_llm

    with pytest.raises(ValueError, match="max_attempts must be >= 1"):
      compile_with_llm(
          extraction_rule={"x": 1},
          event_schema={"y": "str"},
          llm_client=FakeLLMClient([]),
          compile_source=StubCompileSource([]),
          max_attempts=0,
      )

  def test_max_attempts_negative_raises_value_error(self):
    from bigquery_agent_analytics.extractor_compilation import compile_with_llm

    with pytest.raises(ValueError, match="max_attempts must be >= 1"):
      compile_with_llm(
          extraction_rule={"x": 1},
          event_schema={"y": "str"},
          llm_client=FakeLLMClient([]),
          compile_source=StubCompileSource([]),
          max_attempts=-1,
      )


# ------------------------------------------------------------------ #
# compile_with_llm — happy path                                       #
# ------------------------------------------------------------------ #


class TestCompileWithLlmHappyPath:

  def test_succeeds_on_first_try(self):
    from bigquery_agent_analytics.extractor_compilation import compile_with_llm

    llm = FakeLLMClient([_valid_plan_dict()])
    compile_source = StubCompileSource([_make_ok_compile_result()])

    result = compile_with_llm(
        extraction_rule={"intent": "extract decision"},
        event_schema={"content.decision_id": "string"},
        llm_client=llm,
        compile_source=compile_source,
    )

    assert result.ok is True
    assert result.reason == "succeeded"
    assert result.manifest is not None
    assert result.bundle_dir == pathlib.Path("/tmp/bundle")
    assert len(result.attempts) == 1
    only = result.attempts[0]
    assert only.attempt == 1
    assert only.plan_parse_error is None
    assert only.render_error is None
    assert only.compile_result is not None and only.compile_result.ok
    assert only.diagnostic is None
    # LLM was called exactly once.
    assert len(llm.call_log) == 1
    assert len(compile_source.call_log) == 1


# ------------------------------------------------------------------ #
# compile_with_llm — recover after each failure mode                  #
# ------------------------------------------------------------------ #


class TestCompileWithLlmRecovery:
  """Each test seeds a FakeLLMClient with [bad, good] and asserts
  the loop reaches success on attempt 2 with the right per-attempt
  failure channel populated."""

  def test_recovers_after_parser_error(self):
    from bigquery_agent_analytics.extractor_compilation import compile_with_llm

    llm = FakeLLMClient([_missing_required_field_dict(), _valid_plan_dict()])
    # Only the second attempt reaches compile.
    compile_source = StubCompileSource([_make_ok_compile_result()])

    result = compile_with_llm(
        extraction_rule={"intent": "extract decision"},
        event_schema={"content.decision_id": "string"},
        llm_client=llm,
        compile_source=compile_source,
    )

    assert result.ok is True
    assert len(result.attempts) == 2
    first = result.attempts[0]
    assert first.attempt == 1
    assert first.plan_parse_error is not None
    assert first.compile_result is None
    assert first.render_error is None
    assert first.diagnostic is not None
    assert first.diagnostic.startswith("PlanParseError [")
    second = result.attempts[1]
    assert second.attempt == 2
    assert second.plan_parse_error is None
    assert second.compile_result is not None and second.compile_result.ok
    # Compile only happened once (attempt 2); attempt 1 didn't reach it.
    assert len(compile_source.call_log) == 1

  def test_recovers_after_compile_failure(self):
    """Parser + renderer pass, but compile_extractor rejects the
    plan (e.g., invalid_event_types). The retry feedback uses the
    compile-level diagnostic builder."""
    from bigquery_agent_analytics.extractor_compilation import compile_with_llm

    llm = FakeLLMClient(
        [
            _valid_plan_dict(event_type="wrong_event"),
            _valid_plan_dict(event_type="bka_decision"),
        ]
    )
    compile_source = StubCompileSource(
        [
            _make_failing_compile_result(
                invalid_event_types=(
                    "declared event_types ['wrong_event'] have no "
                    "matching sample events"
                )
            ),
            _make_ok_compile_result(),
        ]
    )

    result = compile_with_llm(
        extraction_rule={"intent": "extract decision"},
        event_schema={"content.decision_id": "string"},
        llm_client=llm,
        compile_source=compile_source,
    )

    assert result.ok is True
    assert len(result.attempts) == 2
    first = result.attempts[0]
    assert first.compile_result is not None
    assert not first.compile_result.ok
    assert first.compile_result.invalid_event_types is not None
    # Diagnostic fed forward uses the compile-result builder shape.
    assert first.diagnostic is not None
    assert first.diagnostic.startswith(
        "CompileError [code=invalid_event_types]:"
    )

  def test_recovers_after_renderer_value_error(self, monkeypatch):
    """Render-time ``ValueError`` is defensive (parser already
    runs ``_validate_plan``), but the orchestrator must still
    handle it. Monkeypatch the renderer the loop imported so the
    first plan triggers a synthesized
    ``RenderError [code=invalid_plan]: ...`` diagnostic, then the
    second succeeds."""
    from bigquery_agent_analytics.extractor_compilation import compile_with_llm
    from bigquery_agent_analytics.extractor_compilation import retry_loop

    real_render = retry_loop.render_extractor_source
    calls = {"n": 0}

    def fake_render(plan):
      calls["n"] += 1
      if calls["n"] == 1:
        raise ValueError("hypothetical-future renderer rule rejected this plan")
      return real_render(plan)

    monkeypatch.setattr(retry_loop, "render_extractor_source", fake_render)

    llm = FakeLLMClient([_valid_plan_dict(), _valid_plan_dict()])
    compile_source = StubCompileSource([_make_ok_compile_result()])

    result = compile_with_llm(
        extraction_rule={"intent": "extract decision"},
        event_schema={"content.decision_id": "string"},
        llm_client=llm,
        compile_source=compile_source,
    )

    assert result.ok is True
    assert len(result.attempts) == 2
    first = result.attempts[0]
    assert first.render_error == (
        "hypothetical-future renderer rule rejected this plan"
    )
    assert first.compile_result is None
    assert first.plan_parse_error is None
    assert first.diagnostic == (
        "RenderError [code=invalid_plan]: hypothetical-future renderer "
        "rule rejected this plan"
    )
    # Render-error attempts skip compile.
    assert len(compile_source.call_log) == 1


# ------------------------------------------------------------------ #
# compile_with_llm — exhaustion                                       #
# ------------------------------------------------------------------ #


class TestCompileWithLlmExhaustion:

  def test_exhausts_after_max_attempts_failures(self):
    from bigquery_agent_analytics.extractor_compilation import compile_with_llm

    llm = FakeLLMClient(
        [_missing_required_field_dict(), _missing_required_field_dict()]
    )
    compile_source = StubCompileSource([])  # never reached

    result = compile_with_llm(
        extraction_rule={"intent": "extract decision"},
        event_schema={"content.decision_id": "string"},
        llm_client=llm,
        compile_source=compile_source,
        max_attempts=2,
    )

    assert result.ok is False
    assert result.reason == "max_attempts_reached"
    assert result.manifest is None
    assert result.bundle_dir is None
    assert len(result.attempts) == 2
    # Last attempt's diagnostic is None (nothing to feed forward).
    assert result.attempts[-1].diagnostic is None

  def test_max_attempts_one_exhausts_after_one_attempt(self):
    """``max_attempts=1`` runs the loop once with no retry: one
    LLM call, one compile call, and exhaustion if the result
    isn't ``ok``."""
    from bigquery_agent_analytics.extractor_compilation import compile_with_llm

    llm = FakeLLMClient([_valid_plan_dict(event_type="wrong_event")])
    compile_source = StubCompileSource(
        [
            _make_failing_compile_result(
                invalid_event_types="declared event_types ['wrong_event'] "
                "have no matching sample events"
            )
        ]
    )

    result = compile_with_llm(
        extraction_rule={"intent": "extract decision"},
        event_schema={"content.decision_id": "string"},
        llm_client=llm,
        compile_source=compile_source,
        max_attempts=1,
    )

    assert result.ok is False
    assert result.reason == "max_attempts_reached"
    assert len(result.attempts) == 1
    assert len(llm.call_log) == 1
    # The single attempt's diagnostic is None — no next attempt to feed.
    assert result.attempts[0].diagnostic is None
    # The compile failure is captured for the caller to inspect.
    assert result.attempts[0].compile_result is not None
    assert result.attempts[0].compile_result.invalid_event_types is not None


# ------------------------------------------------------------------ #
# compile_with_llm — exception propagation                            #
# ------------------------------------------------------------------ #


class TestCompileWithLlmExceptionPropagation:

  def test_llm_client_exception_propagates(self):
    """Auth / quota / network errors from the LLM client must NOT
    be silently retried. The exception bubbles out of
    compile_with_llm unchanged so the caller's own retry policy
    (or surfacing) takes over."""
    from bigquery_agent_analytics.extractor_compilation import compile_with_llm

    class TransportError(RuntimeError):
      pass

    llm = FakeLLMClient([TransportError("connection reset")])

    with pytest.raises(TransportError, match="connection reset"):
      compile_with_llm(
          extraction_rule={"intent": "extract decision"},
          event_schema={"content.decision_id": "string"},
          llm_client=llm,
          compile_source=StubCompileSource([]),
          max_attempts=3,
      )


# ------------------------------------------------------------------ #
# compile_with_llm — retry prompt content                             #
# ------------------------------------------------------------------ #


class TestCompileWithLlmRetryPromptContent:

  def test_retry_prompt_embeds_prior_response_and_diagnostic(self):
    """On retry, the second LLM call's prompt must contain both
    the LLM's prior raw response and the diagnostic that explains
    what went wrong. This is the contract that lets the LLM see
    what to fix."""
    from bigquery_agent_analytics.extractor_compilation import compile_with_llm

    bad = _missing_required_field_dict()
    good = _valid_plan_dict()
    llm = FakeLLMClient([bad, good])
    compile_source = StubCompileSource([_make_ok_compile_result()])

    compile_with_llm(
        extraction_rule={"intent": "extract decision"},
        event_schema={"content.decision_id": "string"},
        llm_client=llm,
        compile_source=compile_source,
    )

    assert len(llm.call_log) == 2
    second_prompt, _schema = llm.call_log[1]
    # Prior response is present (event_type="bka_decision" from the
    # malformed first response is in the embedded JSON).
    assert '"event_type": "bka_decision"' in second_prompt
    # Diagnostic from the parser is present.
    assert "PlanParseError [" in second_prompt
    assert "missing" in second_prompt.lower()


# ------------------------------------------------------------------ #
# compile_with_llm — end-to-end with real compile_extractor           #
# ------------------------------------------------------------------ #


class TestCompileWithLlmEndToEnd:
  """One integration test that wires the *real* compile_extractor
  via a closure. Proves that build_resolution_prompt → parser →
  render_extractor_source → compile_extractor all line up at the
  type/contract level. Most loop semantics are covered by the
  faster stub tests above."""

  def test_real_compile_extractor_first_try_success(self, tmp_path):
    from bigquery_agent_analytics.extractor_compilation import compile_extractor
    from bigquery_agent_analytics.extractor_compilation import compile_with_llm

    plan_dict = _valid_plan_dict(event_type="bka_decision")
    sample_events = [
        {
            "event_type": "bka_decision",
            "session_id": "s1",
            "span_id": "sp1",
            "content": {"decision_id": "d1"},
        }
    ]

    def real_compile(plan, source):
      return compile_extractor(
          source=source,
          module_name="extract_decision_compiled",
          function_name=plan.function_name,
          event_types=(plan.event_type,),
          sample_events=sample_events,
          spec=None,
          resolved_graph=None,
          parent_bundle_dir=tmp_path,
          fingerprint_inputs={
              "ontology_text": "",
              "binding_text": "",
              "event_schema": {},
              "event_allowlist": ("bka_decision",),
              "transcript_builder_version": "tb-1",
              "content_serialization_rules": "",
              "extraction_rules": {},
          },
          template_version="t-1",
          compiler_package_version="0.0.1",
          isolation=False,
      )

    llm = FakeLLMClient([plan_dict])

    result = compile_with_llm(
        extraction_rule={"intent": "extract decision"},
        event_schema={
            "content.decision_id": "string",
            "session_id": "string",
            "span_id": "string",
        },
        llm_client=llm,
        compile_source=real_compile,
    )

    assert result.ok is True, (
        f"end-to-end compile failed; reason={result.reason}, "
        f"last attempt={result.attempts[-1]}"
    )
    assert result.bundle_dir is not None
    assert (result.bundle_dir / "manifest.json").is_file()
    assert (result.bundle_dir / "extract_decision_compiled.py").is_file()


# ------------------------------------------------------------------ #
# compile_with_llm — schema mutation hardening                        #
# ------------------------------------------------------------------ #


class TestCompileWithLlmSchemaMutation:
  """Mirrors the resolver's mutation-hardening test. A provider
  adapter that normalizes the schema in place (Gemini's
  ``response_schema`` doesn't support ``$schema`` / ``$defs``,
  for example) must not be able to corrupt the exported module
  global for later callers in the process."""

  def test_module_global_schema_unchanged_after_mutating_client(self):
    import copy as _copy

    from bigquery_agent_analytics.extractor_compilation import compile_with_llm
    from bigquery_agent_analytics.extractor_compilation import RESOLVED_EXTRACTOR_PLAN_JSON_SCHEMA

    baseline = _copy.deepcopy(RESOLVED_EXTRACTOR_PLAN_JSON_SCHEMA)

    captured: list[dict] = []

    class MutatingClient:
      """Stand-in for a provider adapter that strips fields off the
      schema in place. If ``compile_with_llm`` hands over the
      module global directly, this corrupts it for everyone."""

      def __init__(self, response: dict) -> None:
        self._response = response

      def generate_json(self, prompt: str, schema: dict) -> dict:
        captured.append(schema)
        # Mutate the schema — both at the top level and one level
        # deep — to detect shallow-copy regressions too.
        schema["__mutated_top_level__"] = "yes"
        if "properties" in schema and isinstance(schema["properties"], dict):
          schema["properties"]["__mutated_nested__"] = "yes"
        return self._response

    compile_source = StubCompileSource([_make_ok_compile_result()])
    compile_with_llm(
        extraction_rule={"intent": "extract decision"},
        event_schema={"content.decision_id": "string"},
        llm_client=MutatingClient(_valid_plan_dict()),
        compile_source=compile_source,
    )

    # Adapter received a mutated copy (proves the mutation actually
    # happened — without this, the test could pass trivially even
    # if the schema was never used).
    assert captured and captured[0].get("__mutated_top_level__") == "yes"
    # The module global is unchanged.
    assert RESOLVED_EXTRACTOR_PLAN_JSON_SCHEMA == baseline

  def test_each_attempt_gets_a_fresh_schema_copy(self):
    """Across two attempts of the same loop, the second call must
    receive a schema that hasn't been mutated by the first call.
    Regression-locks the per-attempt deep-copy."""
    import copy as _copy

    from bigquery_agent_analytics.extractor_compilation import compile_with_llm
    from bigquery_agent_analytics.extractor_compilation import RESOLVED_EXTRACTOR_PLAN_JSON_SCHEMA

    baseline = _copy.deepcopy(RESOLVED_EXTRACTOR_PLAN_JSON_SCHEMA)
    captured: list[dict] = []

    class MutatingClient:

      def __init__(self, responses: list) -> None:
        self._responses = list(responses)
        self._i = 0

      def generate_json(self, prompt: str, schema: dict) -> dict:
        captured.append(_copy.deepcopy(schema))
        # Mutate AFTER snapshot so the captured value reflects
        # what the call site handed us, then make the schema
        # unusable for the next call if it isn't a fresh copy.
        schema.clear()
        item = self._responses[self._i]
        self._i += 1
        return item

    compile_with_llm(
        extraction_rule={"intent": "extract decision"},
        event_schema={"content.decision_id": "string"},
        llm_client=MutatingClient(
            [_missing_required_field_dict(), _valid_plan_dict()]
        ),
        compile_source=StubCompileSource([_make_ok_compile_result()]),
        max_attempts=2,
    )

    assert len(captured) == 2
    # Both attempts saw the full, unmodified schema.
    assert captured[0] == baseline
    assert captured[1] == baseline
    # And the module global is still pristine.
    assert RESOLVED_EXTRACTOR_PLAN_JSON_SCHEMA == baseline


# ------------------------------------------------------------------ #
# compile_with_llm — recovery from non-string-keys retry crash        #
# ------------------------------------------------------------------ #


class TestCompileWithLlmRecoveryNonStringKeys:
  """Reviewer's exact repro: first response has a non-string key
  alongside ``event_type``. Without the ``_serialize_prior_response``
  fallback, the loop builds the parser diagnostic correctly, then
  ``build_retry_prompt`` raises ``TypeError`` on
  ``json.dumps(sort_keys=True)`` — turning a recoverable failure
  into an unrecoverable crash one frame above."""

  def test_recovers_after_response_with_mixed_type_keys(self):
    from bigquery_agent_analytics.extractor_compilation import compile_with_llm

    bad = {1: "bad", "event_type": "bka_decision"}
    good = _valid_plan_dict()
    llm = FakeLLMClient([bad, good])
    compile_source = StubCompileSource([_make_ok_compile_result()])

    result = compile_with_llm(
        extraction_rule={"intent": "extract decision"},
        event_schema={"content.decision_id": "string"},
        llm_client=llm,
        compile_source=compile_source,
        max_attempts=2,
    )

    assert result.ok is True
    assert len(result.attempts) == 2
    first = result.attempts[0]
    assert first.plan_parse_error is not None
    # The retry prompt that went out on attempt 2 must contain the
    # bad response (echoed via the helper's fallback path), not a
    # crash.
    second_prompt, _schema = llm.call_log[1]
    assert "bka_decision" in second_prompt
    assert "bad" in second_prompt
