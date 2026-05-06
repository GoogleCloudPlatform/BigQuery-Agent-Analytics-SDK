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

"""Unit tests for the extractor-compilation scaffolding (issue #75 PR 4b.1).

Coverage:
- Fingerprint determinism + sensitivity to each named input.
- Manifest JSON round-trip.
- AST validator: accepts safe source; rejects each forbidden
  category (import, name, attribute, async, generator, class,
  scope, top-level side effect, syntax error).
- Smoke-test runner: rejects empty event lists; captures per-event
  exceptions; surfaces validator failures.
- End-to-end ``compile_extractor`` against the BKA-decision
  hand-authored fixture; bundle ends up on disk; cleanup leaves
  no half-written artifacts on AST / smoke-test failure.
- Equivalence: the compiled BKA fixture's output matches
  ``extract_bka_decision_event`` on the same events.
"""

from __future__ import annotations

import json
import pathlib
import tempfile
import uuid

import pytest

# ------------------------------------------------------------------ #
# Fixtures + helpers                                                   #
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

  tmp = pathlib.Path(tempfile.mkdtemp(prefix="bka_compile_test_"))
  (tmp / "ont.yaml").write_text(_BKA_ONTOLOGY_YAML, encoding="utf-8")
  (tmp / "bnd.yaml").write_text(_BKA_BINDING_YAML, encoding="utf-8")
  ontology = load_ontology(str(tmp / "ont.yaml"))
  binding = load_binding(str(tmp / "bnd.yaml"), ontology=ontology)
  return resolve(ontology, binding)


def _sample_bka_events():
  """Two events: one with reasoning_text (partial), one without
  (fully handled)."""
  return [
      {
          "event_type": "bka_decision",
          "session_id": "sess1",
          "span_id": "span1",
          "content": {
              "decision_id": "d1",
              "outcome": "approved",
              "confidence": 0.92,
              "reasoning_text": "free-form rationale",
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
  ]


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


def _unique_module_name(prefix: str = "bka_compiled_") -> str:
  """Per-test unique module name so importlib doesn't recycle a
  stale ``sys.modules`` entry from a previous test in the same
  pytest session."""
  return f"{prefix}{uuid.uuid4().hex[:12]}"


# ------------------------------------------------------------------ #
# Fingerprint                                                          #
# ------------------------------------------------------------------ #


class TestFingerprint:

  def test_identical_inputs_produce_identical_fingerprint(self):
    from bigquery_agent_analytics.extractor_compilation import compute_fingerprint

    inputs = _fingerprint_inputs()
    a = compute_fingerprint(
        template_version="v0.1",
        compiler_package_version="0.0.0",
        **inputs,
    )
    b = compute_fingerprint(
        template_version="v0.1",
        compiler_package_version="0.0.0",
        **inputs,
    )
    assert a == b
    assert len(a) == 64
    assert all(c in "0123456789abcdef" for c in a)

  def test_event_allowlist_order_does_not_matter(self):
    """The allowlist is sorted before hashing — caller-side order
    is irrelevant."""
    from bigquery_agent_analytics.extractor_compilation import compute_fingerprint

    inputs = _fingerprint_inputs()
    a = compute_fingerprint(
        template_version="v0.1",
        compiler_package_version="0.0.0",
        **(inputs | {"event_allowlist": ("a", "b", "c")}),
    )
    b = compute_fingerprint(
        template_version="v0.1",
        compiler_package_version="0.0.0",
        **(inputs | {"event_allowlist": ("c", "b", "a")}),
    )
    assert a == b

  @pytest.mark.parametrize(
      "field,override",
      [
          ("ontology_text", "ontology: Different\n"),
          ("binding_text", "binding: different\n"),
          ("event_schema", {"different": {}}),
          ("event_allowlist", ("other_event",)),
          ("transcript_builder_version", "v0.2"),
          ("content_serialization_rules", {"strip_ansi": False}),
          ("extraction_rules", {"other": {}}),
      ],
  )
  def test_each_input_field_is_hashed(self, field, override):
    """Changing any of the seven ``fingerprint_inputs`` fields
    invalidates the fingerprint."""
    from bigquery_agent_analytics.extractor_compilation import compute_fingerprint

    base = _fingerprint_inputs()
    a = compute_fingerprint(
        template_version="v0.1",
        compiler_package_version="0.0.0",
        **base,
    )
    b = compute_fingerprint(
        template_version="v0.1",
        compiler_package_version="0.0.0",
        **(base | {field: override}),
    )
    assert a != b, f"changing {field!r} did not change the fingerprint"

  def test_template_version_and_compiler_version_are_hashed(self):
    from bigquery_agent_analytics.extractor_compilation import compute_fingerprint

    inputs = _fingerprint_inputs()
    base = compute_fingerprint(
        template_version="v0.1",
        compiler_package_version="0.0.0",
        **inputs,
    )
    bumped_template = compute_fingerprint(
        template_version="v0.2",
        compiler_package_version="0.0.0",
        **inputs,
    )
    bumped_compiler = compute_fingerprint(
        template_version="v0.1",
        compiler_package_version="0.0.1",
        **inputs,
    )
    assert base != bumped_template
    assert base != bumped_compiler
    assert bumped_template != bumped_compiler


# ------------------------------------------------------------------ #
# Manifest                                                             #
# ------------------------------------------------------------------ #


class TestManifest:

  def test_round_trip_through_json(self):
    from bigquery_agent_analytics.extractor_compilation import Manifest

    m = Manifest(
        fingerprint="a" * 64,
        event_types=("bka_decision", "tool_completed"),
        module_filename="bka_compiled.py",
        function_name="extract_bka_decision_event_compiled",
        compiler_package_version="0.0.0",
        template_version="v0.1",
        transcript_builder_version="v0.1",
        created_at="2026-05-05T00:00:00+00:00",
    )
    text = m.to_json()
    parsed = Manifest.from_json(text)
    assert parsed == m

  def test_to_json_is_byte_stable(self):
    """Two manifests with identical fields produce identical JSON.
    A bundle directory's manifest.json must be byte-stable so a
    re-compile with no input changes is genuinely a no-op."""
    from bigquery_agent_analytics.extractor_compilation import Manifest

    m1 = Manifest(
        fingerprint="a" * 64,
        event_types=("bka_decision",),
        module_filename="m.py",
        function_name="f",
        compiler_package_version="0.0.0",
        template_version="v0.1",
        transcript_builder_version="v0.1",
        created_at="2026-05-05T00:00:00+00:00",
    )
    m2 = Manifest(**{**m1.__dict__})
    assert m1.to_json() == m2.to_json()

  def test_json_keys_are_sorted(self):
    from bigquery_agent_analytics.extractor_compilation import Manifest

    m = Manifest(
        fingerprint="a" * 64,
        event_types=("bka_decision",),
        module_filename="m.py",
        function_name="f",
        compiler_package_version="0.0.0",
        template_version="v0.1",
        transcript_builder_version="v0.1",
        created_at="2026-05-05T00:00:00+00:00",
    )
    parsed = json.loads(m.to_json())
    assert list(parsed.keys()) == sorted(parsed.keys())


# ------------------------------------------------------------------ #
# AST validator                                                        #
# ------------------------------------------------------------------ #


class TestAstValidator:

  def test_safe_source_passes(self):
    from bigquery_agent_analytics.extractor_compilation import validate_source
    from tests.fixtures_extractor_compilation.bka_decision_template import BKA_DECISION_SOURCE

    report = validate_source(BKA_DECISION_SOURCE)
    assert (
        report.ok is True
    ), f"failures: {[(f.code, f.detail) for f in report.failures]}"

  def test_disallowed_import_outside_allowlist(self):
    from bigquery_agent_analytics.extractor_compilation import validate_source

    src = "from os import system\n" "def f(event, spec):\n" "    return None\n"
    report = validate_source(src)
    assert any(f.code == "disallowed_import" for f in report.failures)

  def test_plain_import_rejected_even_for_allowlisted_module(self):
    from bigquery_agent_analytics.extractor_compilation import validate_source

    src = (
        "import bigquery_agent_analytics\n"
        "def f(event, spec):\n"
        "    return None\n"
    )
    report = validate_source(src)
    assert any(f.code == "disallowed_import" for f in report.failures)

  @pytest.mark.parametrize(
      "name",
      ["eval", "exec", "compile", "__import__", "open", "input", "getattr"],
  )
  def test_disallowed_name(self, name):
    from bigquery_agent_analytics.extractor_compilation import validate_source

    src = f"def f(event, spec):\n    return {name}('x')\n"
    report = validate_source(src)
    assert any(f.code == "disallowed_name" for f in report.failures)

  def test_disallowed_dunder_attribute(self):
    from bigquery_agent_analytics.extractor_compilation import validate_source

    src = "def f(event, spec):\n" "    return event.__class__\n"
    report = validate_source(src)
    assert any(f.code == "disallowed_attribute" for f in report.failures)

  def test_async_def_rejected(self):
    from bigquery_agent_analytics.extractor_compilation import validate_source

    src = "async def f(event, spec):\n    return None\n"
    report = validate_source(src)
    assert any(f.code == "disallowed_async" for f in report.failures)

  def test_yield_rejected(self):
    from bigquery_agent_analytics.extractor_compilation import validate_source

    src = "def f(event, spec):\n    yield 1\n"
    report = validate_source(src)
    assert any(f.code == "disallowed_generator" for f in report.failures)

  def test_class_definition_rejected(self):
    from bigquery_agent_analytics.extractor_compilation import validate_source

    src = "class Foo:\n    pass\n"
    report = validate_source(src)
    assert any(f.code == "disallowed_class" for f in report.failures)

  def test_top_level_assignment_rejected(self):
    from bigquery_agent_analytics.extractor_compilation import validate_source

    src = "X = 5\n" "def f(event, spec):\n" "    return None\n"
    report = validate_source(src)
    assert any(f.code == "top_level_side_effect" for f in report.failures)

  def test_syntax_error_reported(self):
    from bigquery_agent_analytics.extractor_compilation import validate_source

    report = validate_source("def f(:\n")
    assert len(report.failures) == 1
    assert report.failures[0].code == "syntax_error"

  def test_imported_symbol_must_be_in_per_module_allowlist(self):
    """``from <allowed_module> import <not_allowed_symbol>`` is
    rejected even though the module itself is fine — closes the
    'smuggle ``__builtins__`` from a valid module' gap."""
    from bigquery_agent_analytics.extractor_compilation import validate_source

    src = (
        "from bigquery_agent_analytics.extracted_models import os_path\n"
        "def f(event, spec):\n"
        "    return None\n"
    )
    report = validate_source(src)
    assert any(f.code == "disallowed_import" for f in report.failures)

  def test_wildcard_import_rejected(self):
    from bigquery_agent_analytics.extractor_compilation import validate_source

    src = (
        "from bigquery_agent_analytics.extracted_models import *\n"
        "def f(event, spec):\n"
        "    return None\n"
    )
    report = validate_source(src)
    assert any(f.code == "disallowed_import" for f in report.failures)

  def test_imported_alias_starting_with_underscore_rejected(self):
    """No hidden dunder smuggling: ``from x import y as _z`` fails
    even though ``y`` is allowed."""
    from bigquery_agent_analytics.extractor_compilation import validate_source

    src = (
        "from bigquery_agent_analytics.extracted_models import "
        "ExtractedNode as _hidden\n"
        "def f(event, spec):\n"
        "    return None\n"
    )
    report = validate_source(src)
    assert any(f.code == "disallowed_import" for f in report.failures)

  def test_decorator_rejected(self):
    """Decorators run at definition time and can do anything."""
    from bigquery_agent_analytics.extractor_compilation import validate_source

    src = (
        "def deco(fn):\n"
        "    return fn\n"
        "@deco\n"
        "def f(event, spec):\n"
        "    return None\n"
    )
    report = validate_source(src)
    assert any(f.code == "disallowed_decorator" for f in report.failures)

  def test_non_constant_default_rejected(self):
    """``def f(x=open('p').read()): ...`` would run at module
    import time. Defaults must be primitive constants."""
    from bigquery_agent_analytics.extractor_compilation import validate_source

    src = "def f(event, spec, _cache=[]):\n    return None\n"
    report = validate_source(src)
    assert any(f.code == "disallowed_default" for f in report.failures)

  def test_constant_defaults_accepted(self):
    """Constant primitives are fine — no side effects at import."""
    from bigquery_agent_analytics.extractor_compilation import validate_source

    src = "def f(event, spec, x=42, y=None, z='ok', n=-1):\n    return None\n"
    report = validate_source(src)
    assert (
        report.ok is True
    ), f"failures: {[(f.code, f.detail) for f in report.failures]}"

  def test_while_loop_rejected(self):
    """``while True:`` could hang the smoke runner."""
    from bigquery_agent_analytics.extractor_compilation import validate_source

    src = "def f(event, spec):\n    while True:\n        pass\n"
    report = validate_source(src)
    assert any(f.code == "disallowed_while" for f in report.failures)

  def test_raise_rejected(self):
    """``raise SystemExit`` would escape any non-BaseException
    catch; banning 'raise' broadly is the simplest rule and frees
    the smoke runner from worrying about SystemExit at all."""
    from bigquery_agent_analytics.extractor_compilation import validate_source

    src = "def f(event, spec):\n    raise SystemExit\n"
    report = validate_source(src)
    assert any(f.code == "disallowed_raise" for f in report.failures)

  def test_try_except_rejected(self):
    from bigquery_agent_analytics.extractor_compilation import validate_source

    src = (
        "def f(event, spec):\n"
        "    try:\n"
        "        return None\n"
        "    except Exception:\n"
        "        return None\n"
    )
    report = validate_source(src)
    assert any(f.code == "disallowed_try" for f in report.failures)

  def test_with_statement_rejected(self):
    """Context-manager protocols invoke __enter__/__exit__ — dunder
    methods we want to keep out of compiled extractors."""
    from bigquery_agent_analytics.extractor_compilation import validate_source

    src = (
        "def f(event, spec):\n" "    with event as e:\n" "        return None\n"
    )
    report = validate_source(src)
    assert any(f.code == "disallowed_with" for f in report.failures)

  def test_breakpoint_rejected(self):
    from bigquery_agent_analytics.extractor_compilation import validate_source

    src = "def f(event, spec):\n    breakpoint()\n    return None\n"
    report = validate_source(src)
    assert any(f.code == "disallowed_name" for f in report.failures)

  @pytest.mark.parametrize("name", ["exit", "quit", "__build_class__"])
  def test_additional_forbidden_names(self, name):
    from bigquery_agent_analytics.extractor_compilation import validate_source

    src = f"def f(event, spec):\n    return {name}\n"
    report = validate_source(src)
    assert any(f.code == "disallowed_name" for f in report.failures)

  def test_iter_name_rejected(self):
    """``iter(callable, sentinel)`` is the documented unbounded-
    iteration construct. Block by name."""
    from bigquery_agent_analytics.extractor_compilation import validate_source

    src = "def f(event, spec):\n" "    return iter(int, 1)\n"
    report = validate_source(src)
    assert any(f.code == "disallowed_name" for f in report.failures)

  @pytest.mark.parametrize(
      "iterable",
      [
          "range(10)",
          "range(10**100)",
          "iter(int, 1)",
          "some_helper()",
      ],
  )
  def test_for_iter_name_call_rejected(self, iterable):
    """``for _ in <Call to a Name>`` is rejected — covers
    ``range(...)``, ``iter(...)``, and any user-defined helper."""
    from bigquery_agent_analytics.extractor_compilation import validate_source

    src = f"def f(event, spec):\n    for _ in {iterable}:\n        pass\n"
    report = validate_source(src)
    assert any(
        f.code in ("disallowed_for_iter", "disallowed_name")
        for f in report.failures
    ), (
        f"expected disallowed_for_iter or disallowed_name; got "
        f"{[(f.code, f.detail) for f in report.failures]}"
    )

  def test_for_iter_tuple_literal_accepted(self):
    """The BKA fixture iterates a Tuple literal — that must keep
    working."""
    from bigquery_agent_analytics.extractor_compilation import validate_source

    src = (
        "def f(event, spec):\n"
        "    for key in ('outcome', 'confidence'):\n"
        "        pass\n"
        "    return None\n"
    )
    report = validate_source(src)
    assert (
        report.ok is True
    ), f"failures: {[(f.code, f.detail) for f in report.failures]}"

  def test_for_iter_method_call_accepted(self):
    """Method calls are bounded by their receiver's runtime size,
    which itself comes from event payload — accept ``dict.items()``
    and friends."""
    from bigquery_agent_analytics.extractor_compilation import validate_source

    src = (
        "def f(event, spec):\n"
        "    for k, v in event.items():\n"
        "        pass\n"
        "    return None\n"
    )
    report = validate_source(src)
    assert (
        report.ok is True
    ), f"failures: {[(f.code, f.detail) for f in report.failures]}"

  @pytest.mark.parametrize(
      "expr",
      [
          "list(range(10))",
          "sum([1, 2, 3])",
          "max(1, 2, 3)",
          "some_helper()",
          "abs(-5)",
      ],
  )
  def test_disallowed_call_target_rejected(self, expr):
    """Names like ``list``, ``sum``, ``max``, user helpers, and
    other builtins not in ``_ALLOWED_CALL_TARGETS`` fail with
    ``disallowed_call`` — even though the names themselves aren't
    in ``_FORBIDDEN_NAMES``. Closes the
    ``list(range(10**100))`` / ``sum(big_iter)`` allocation hole."""
    from bigquery_agent_analytics.extractor_compilation import validate_source

    src = f"def f(event, spec):\n    return {expr}\n"
    report = validate_source(src)
    assert any(f.code == "disallowed_call" for f in report.failures), (
        f"expected disallowed_call; got "
        f"{[(f.code, f.detail) for f in report.failures]}"
    )

  @pytest.mark.parametrize(
      "expr",
      [
          "isinstance(event, dict)",
          "bool(event)",
          "len(event)",
          "set()",
          "list()",
          "dict()",
          "tuple()",
          "ExtractedNode(node_id='x', entity_name='E', labels=['E'], properties=[])",
      ],
  )
  def test_allowed_call_targets_accepted(self, expr):
    """Allowlisted constructors and safe builtins still work."""
    from bigquery_agent_analytics.extractor_compilation import validate_source

    src = (
        "from bigquery_agent_analytics.extracted_models import ExtractedNode\n"
        f"def f(event, spec):\n    return {expr}\n"
    )
    report = validate_source(src)
    assert (
        report.ok is True
    ), f"failures: {[(f.code, f.detail) for f in report.failures]}"

  def test_method_calls_still_allowed(self):
    """Allowlisted methods (``get``, ``items``, ...) on bounded
    receivers pass — those are the patterns the BKA fixture and
    most real extractors use."""
    from bigquery_agent_analytics.extractor_compilation import validate_source

    src = (
        "def f(event, spec):\n"
        "    x = event.get('x')\n"
        "    y = event.get('items', {}).items()\n"
        "    return None\n"
    )
    report = validate_source(src)
    assert (
        report.ok is True
    ), f"failures: {[(f.code, f.detail) for f in report.failures]}"

  @pytest.mark.parametrize(
      "method", ["clear", "repeat_forever", "pop", "update", "fromkeys"]
  )
  def test_method_outside_allowlist_rejected(self, method):
    """The reviewer's repro: ``event.repeat_forever()`` and
    ``event.clear()`` previously passed. Method-name allowlist
    now blocks them."""
    from bigquery_agent_analytics.extractor_compilation import validate_source

    src = f"def f(event, spec):\n    return event.{method}()\n"
    report = validate_source(src)
    assert any(f.code == "disallowed_method" for f in report.failures), (
        f"expected disallowed_method on event.{method}(); got "
        f"{[(f.code, f.detail) for f in report.failures]}"
    )

  @pytest.mark.parametrize(
      "method", ["get", "items", "keys", "values", "append"]
  )
  def test_allowlisted_methods_accepted(self, method):
    """The BKA fixture uses ``get``, ``items``, ``append``;
    ``keys`` and ``values`` round out the read-only dict access
    set."""
    from bigquery_agent_analytics.extractor_compilation import validate_source

    src = f"def f(event, spec):\n    return event.{method}()\n"
    report = validate_source(src)
    assert (
        report.ok is True
    ), f"failures: {[(f.code, f.detail) for f in report.failures]}"

  @pytest.mark.parametrize(
      "code",
      [
          # Local assignment shadows the call-target allowlist.
          "def f(event, spec):\n    len = event.get('cb')\n    return len()\n",
          # AnnAssign.
          "def f(event, spec):\n    len: int = 5\n    return None\n",
          # AugAssign.
          (
              "def f(event, spec):\n"
              "    isinstance = 0\n"
              "    isinstance += 1\n"
              "    return None\n"
          ),
          # For-loop target.
          (
              "def f(event, spec):\n"
              "    for tuple in (1, 2):\n"
              "        pass\n"
              "    return None\n"
          ),
          # Comprehension target.
          (
              "def f(event, spec):\n"
              "    xs = [list for list in (1, 2)]\n"
              "    return None\n"
          ),
          # Walrus.
          (
              "def f(event, spec):\n"
              "    if (set := event.get('cb')):\n"
              "        return set()\n"
              "    return None\n"
          ),
      ],
  )
  def test_shadowing_call_target_allowlist_rejected(self, code):
    """Rebinding any name in ``_ALLOWED_CALL_TARGETS`` (via
    assignment, for-target, comprehension target, walrus, etc.)
    would let unsafe callables slip past the static check.
    Reject all binding shapes."""
    from bigquery_agent_analytics.extractor_compilation import validate_source

    report = validate_source(code)
    assert any(
        f.code == "disallowed_shadowing" for f in report.failures
    ), f"got {[(f.code, f.detail) for f in report.failures]}"

  def test_shadowing_via_function_arg_rejected(self):
    """``def f(event, spec, len=...): ...`` would shadow ``len``."""
    from bigquery_agent_analytics.extractor_compilation import validate_source

    src = "def f(event, spec, len=0):\n    return None\n"
    report = validate_source(src)
    assert any(f.code == "disallowed_shadowing" for f in report.failures)

  def test_shadowing_via_nested_function_def_rejected(self):
    """A nested ``def len(): ...`` would shadow the outer
    ``len`` allowlist entry."""
    from bigquery_agent_analytics.extractor_compilation import validate_source

    src = (
        "def f(event, spec):\n"
        "    def len(x):\n"
        "        return 0\n"
        "    return len(event)\n"
    )
    report = validate_source(src)
    assert any(f.code == "disallowed_shadowing" for f in report.failures)

  def test_match_statement_rejected(self):
    """``match`` carries pattern captures (``case {"x": len}``)
    that bind names without going through ``Name(ctx=Store)`` —
    the shadowing check would miss them and the call-target
    allowlist could be bypassed. Reject ``match`` outright."""
    from bigquery_agent_analytics.extractor_compilation import validate_source

    src = (
        "def f(event, spec):\n"
        "    match event:\n"
        "        case {'x': len}:\n"
        "            return len()\n"
        "    return None\n"
    )
    report = validate_source(src)
    assert any(
        f.code == "disallowed_match" for f in report.failures
    ), f"got {[(f.code, f.detail) for f in report.failures]}"

  def test_lambda_call_rejected(self):
    """``(lambda: X)()`` previously slipped past the call-target
    allowlist because ``func`` was an ``ast.Lambda``, neither
    Name nor Attribute. Both the lambda definition AND the call
    fail now."""
    from bigquery_agent_analytics.extractor_compilation import validate_source

    src = (
        "from bigquery_agent_analytics.structured_extraction import (\n"
        "    StructuredExtractionResult,\n"
        ")\n"
        "def f(event, spec):\n"
        "    return (lambda: StructuredExtractionResult())()\n"
    )
    report = validate_source(src)
    codes = {f.code for f in report.failures}
    assert "disallowed_lambda" in codes
    assert "disallowed_call" in codes

  def test_chained_call_target_rejected(self):
    """``(event.get('cb'))()`` — the callable is the result of a
    method call, not a static name. Static allowlists can't cover
    this; reject it."""
    from bigquery_agent_analytics.extractor_compilation import validate_source

    src = "def f(event, spec):\n" "    return (event.get('cb'))()\n"
    report = validate_source(src)
    assert any(f.code == "disallowed_call" for f in report.failures)

  def test_conditional_call_target_rejected(self):
    """``(a if cond else b)()`` — IfExp callable. Same problem as
    chained calls; static allowlists can't cover it."""
    from bigquery_agent_analytics.extractor_compilation import validate_source

    src = (
        "def f(event, spec):\n"
        "    a = list\n"
        "    b = dict\n"
        "    return (a if event.get('x') else b)()\n"
    )
    report = validate_source(src)
    assert any(f.code == "disallowed_call" for f in report.failures)

  def test_comprehension_with_name_call_iter_rejected(self):
    """Same rule as for-loops applies to comprehensions:
    ``[x for x in range(10**100)]`` could allocate forever."""
    from bigquery_agent_analytics.extractor_compilation import validate_source

    src = (
        "def f(event, spec):\n"
        "    xs = [x for x in range(10)]\n"
        "    return None\n"
    )
    report = validate_source(src)
    assert any(
        f.code in ("disallowed_for_iter", "disallowed_name")
        for f in report.failures
    )


# ------------------------------------------------------------------ #
# Smoke-test runner                                                    #
# ------------------------------------------------------------------ #


class TestSmokeTest:

  def test_empty_events_list_rejected(self):
    from bigquery_agent_analytics.extractor_compilation import run_smoke_test
    from bigquery_agent_analytics.structured_extraction import StructuredExtractionResult

    def extractor(event, spec):
      return StructuredExtractionResult()

    with pytest.raises(ValueError):
      run_smoke_test(extractor, events=[], spec=None, resolved_graph=None)

  def test_per_event_exceptions_captured(self):
    from bigquery_agent_analytics.extractor_compilation import run_smoke_test

    def extractor(event, spec):
      raise RuntimeError("boom")

    report = run_smoke_test(
        extractor,
        events=[{"event_type": "x"}, {"event_type": "y"}],
        spec=None,
        resolved_graph=None,
    )
    assert report.ok is False
    assert report.events_with_exception == 2
    assert all("boom" in e for e in report.exceptions)

  def test_validator_failures_surfaced(self):
    """Smoke-test fails when the merged graph doesn't validate
    against the resolved spec — even if every per-event call
    completed without an exception."""
    from bigquery_agent_analytics.extracted_models import ExtractedNode
    from bigquery_agent_analytics.extracted_models import ExtractedProperty
    from bigquery_agent_analytics.extractor_compilation import run_smoke_test
    from bigquery_agent_analytics.structured_extraction import StructuredExtractionResult

    spec = _bka_resolved_spec()

    def extractor(event, spec_):
      # Decision_id should be a string per the ontology, but emit
      # an int — the #76 validator will flag this.
      return StructuredExtractionResult(
          nodes=[
              ExtractedNode(
                  node_id="sess1:mako_DecisionPoint:decision_id=42",
                  entity_name="mako_DecisionPoint",
                  labels=["mako_DecisionPoint"],
                  properties=[
                      ExtractedProperty(name="decision_id", value=42),
                  ],
              )
          ]
      )

    report = run_smoke_test(
        extractor,
        events=[{"event_type": "bka_decision"}],
        spec=None,
        resolved_graph=spec,
    )
    assert report.ok is False
    assert report.events_with_exception == 0
    assert any(f.code == "type_mismatch" for f in report.validation_failures)

  def test_clean_run_returns_ok(self):
    from bigquery_agent_analytics.extracted_models import ExtractedNode
    from bigquery_agent_analytics.extracted_models import ExtractedProperty
    from bigquery_agent_analytics.extractor_compilation import run_smoke_test
    from bigquery_agent_analytics.structured_extraction import StructuredExtractionResult

    spec = _bka_resolved_spec()

    def extractor(event, spec_):
      content = event.get("content", {})
      did = content.get("decision_id")
      if did is None:
        return StructuredExtractionResult()
      return StructuredExtractionResult(
          nodes=[
              ExtractedNode(
                  node_id=f"sess1:mako_DecisionPoint:decision_id={did}",
                  entity_name="mako_DecisionPoint",
                  labels=["mako_DecisionPoint"],
                  properties=[
                      ExtractedProperty(name="decision_id", value=did),
                  ],
              )
          ]
      )

    report = run_smoke_test(
        extractor,
        events=_sample_bka_events(),
        spec=None,
        resolved_graph=spec,
    )
    assert report.ok is True
    assert report.events_with_exception == 0
    assert report.validation_failures == ()

  def test_system_exit_captured_not_escaped(self):
    """``raise SystemExit`` would escape an ``except Exception``
    catch — the runner now catches ``BaseException`` so the harness
    survives. (The AST validator also rejects ``raise`` outright in
    bundle source; this test exercises the runtime guarantee in
    case a future template path bypasses the AST gate.)"""
    from bigquery_agent_analytics.extractor_compilation import run_smoke_test

    def extractor(event, spec):
      raise SystemExit("compiled extractor tried to exit")

    report = run_smoke_test(
        extractor,
        events=[{"event_type": "x"}],
        spec=None,
        resolved_graph=None,
    )
    assert report.ok is False
    assert report.events_with_exception == 1
    assert any("SystemExit" in e for e in report.exceptions)

  def test_wrong_return_type_fails(self):
    """An extractor that returns the wrong type (e.g., a dict
    instead of a ``StructuredExtractionResult``) used to be
    silently dropped from the merged result. It now fails the
    smoke gate explicitly."""
    from bigquery_agent_analytics.extractor_compilation import run_smoke_test

    def extractor(event, spec):
      return {"nodes": []}

    report = run_smoke_test(
        extractor,
        events=[{"event_type": "x"}],
        spec=None,
        resolved_graph=None,
    )
    assert report.ok is False
    assert report.events_with_wrong_return_type == 1
    assert "dict" in report.wrong_return_types[0]

  def test_all_empty_results_fail_default(self):
    """An extractor that returns ``StructuredExtractionResult()``
    for every event used to vacuously pass — #76 has no event-level
    expectations. The smoke runner now requires
    ``min_nonempty_results >= 1`` by default."""
    from bigquery_agent_analytics.extractor_compilation import run_smoke_test
    from bigquery_agent_analytics.structured_extraction import StructuredExtractionResult

    def extractor(event, spec):
      return StructuredExtractionResult()

    report = run_smoke_test(
        extractor,
        events=[{"event_type": "x"}, {"event_type": "y"}],
        spec=None,
        resolved_graph=None,
    )
    assert report.ok is False
    assert report.events_with_nonempty_result == 0
    assert report.min_nonempty_results == 1

  def test_negative_min_nonempty_results_rejected(self):
    """Negative values would make the floor trivially pass —
    explicit ``ValueError`` so callers know 0 is the opt-out."""
    from bigquery_agent_analytics.extractor_compilation import run_smoke_test
    from bigquery_agent_analytics.structured_extraction import StructuredExtractionResult

    def extractor(event, spec):
      return StructuredExtractionResult()

    with pytest.raises(ValueError):
      run_smoke_test(
          extractor,
          events=[{"event_type": "x"}],
          spec=None,
          resolved_graph=None,
          min_nonempty_results=-1,
      )

  def test_subprocess_timeout_surfaces_as_exceptions(
      self, tmp_path: pathlib.Path
  ):
    """Subprocess isolation is the runtime safety net for hangs
    the AST allowlist can't catch. Source with ``while True``
    bypasses the AST gate when fed straight to the runner — a
    real-world hang would come from an LLM-emitted bundle that
    cleared the AST gate but still allocated/looped without
    bound. The wallclock cap must surface the hang as a
    ``TimeoutError`` exception in the report."""
    from bigquery_agent_analytics.extractor_compilation import run_smoke_test_in_subprocess

    hanging_source = (
        "from bigquery_agent_analytics.structured_extraction import (\n"
        "    StructuredExtractionResult,\n"
        ")\n"
        "def f(event, spec):\n"
        "    while True:\n"
        "        pass\n"
        "    return StructuredExtractionResult()\n"
    )
    source_path = tmp_path / "hangs.py"
    source_path.write_text(hanging_source, encoding="utf-8")

    report = run_smoke_test_in_subprocess(
        source_path,
        module_name="hangs",
        function_name="f",
        events=[{"event_type": "x"}],
        spec=None,
        resolved_graph=None,
        timeout_seconds=1.0,
        memory_limit_mb=None,  # keep the test platform-portable
    )
    assert report.ok is False
    assert report.events_with_exception == 1
    assert any("TimeoutError" in e for e in report.exceptions)

  def test_unpicklable_inputs_surfaced_as_harness_failure(
      self, tmp_path: pathlib.Path
  ):
    """The wrapper docstring promises a ``SmokeTestReport`` even
    on harness failure. Pickling a lambda used to escape the
    ``try`` and crash the caller; now it produces a
    ``PickleError`` exception per event."""
    from bigquery_agent_analytics.extractor_compilation import run_smoke_test_in_subprocess

    source_path = tmp_path / "noop.py"
    source_path.write_text(
        "from bigquery_agent_analytics.structured_extraction import (\n"
        "    StructuredExtractionResult,\n"
        ")\n"
        "def f(event, spec):\n"
        "    return StructuredExtractionResult()\n",
        encoding="utf-8",
    )
    report = run_smoke_test_in_subprocess(
        source_path,
        module_name="noop",
        function_name="f",
        events=[{"event_type": "x"}],
        spec=lambda x: x,  # not picklable
        resolved_graph=None,
        memory_limit_mb=None,
    )
    assert report.ok is False
    assert report.events_with_exception == 1
    assert any("PickleError" in e for e in report.exceptions)

  def test_negative_memory_limit_rejected(self, tmp_path: pathlib.Path):
    """``memory_limit_mb=-1`` used to silently no-op in the child;
    parent now raises ``ValueError`` so the cap can't be disabled
    by accident."""
    from bigquery_agent_analytics.extractor_compilation import run_smoke_test_in_subprocess

    source_path = tmp_path / "noop.py"
    source_path.write_text(
        "from bigquery_agent_analytics.structured_extraction import (\n"
        "    StructuredExtractionResult,\n"
        ")\n"
        "def f(event, spec):\n"
        "    return StructuredExtractionResult()\n",
        encoding="utf-8",
    )
    with pytest.raises(ValueError):
      run_smoke_test_in_subprocess(
          source_path,
          module_name="noop",
          function_name="f",
          events=[{"event_type": "x"}],
          spec=None,
          resolved_graph=None,
          memory_limit_mb=-1,
      )

  def test_malformed_extraction_result_internals_caught(self):
    """``isinstance(result, StructuredExtractionResult)`` doesn't
    enforce field types. A generated extractor returning
    ``StructuredExtractionResult(fully_handled_span_ids=("s",))``
    used to crash inside ``merge_extraction_results``. The smoke
    runner now reports it as a wrong-return-type failure."""
    from bigquery_agent_analytics.extractor_compilation import run_smoke_test
    from bigquery_agent_analytics.structured_extraction import StructuredExtractionResult

    def extractor_with_tuple_span_ids(event, spec):
      return StructuredExtractionResult(
          nodes=[],
          edges=[],
          fully_handled_span_ids=("s1",),  # tuple not set
      )

    report = run_smoke_test(
        extractor_with_tuple_span_ids,
        events=[{"event_type": "x"}],
        spec=None,
        resolved_graph=None,
    )
    assert report.ok is False
    assert report.events_with_wrong_return_type == 1
    assert any(
        "fully_handled_span_ids must be set" in m
        for m in report.wrong_return_types
    )

  def test_malformed_extraction_result_with_dict_node_caught(self):
    """``nodes=[{}]`` (dict instead of ExtractedNode) used to
    crash ``merge_extraction_results`` with an opaque
    ``AttributeError``. Now caught at the smoke gate."""
    from bigquery_agent_analytics.extractor_compilation import run_smoke_test
    from bigquery_agent_analytics.structured_extraction import StructuredExtractionResult

    def extractor_with_dict_node(event, spec):
      return StructuredExtractionResult(nodes=[{}])

    report = run_smoke_test(
        extractor_with_dict_node,
        events=[{"event_type": "x"}],
        spec=None,
        resolved_graph=None,
    )
    assert report.ok is False
    assert report.events_with_wrong_return_type == 1
    assert any(
        "nodes[0] is not ExtractedNode" in m for m in report.wrong_return_types
    )

  def test_subprocess_runs_clean_extractor_against_real_spec(
      self, tmp_path: pathlib.Path
  ):
    """End-to-end happy path through the subprocess runner: the
    BKA fixture compiles, the child loads the source, runs against
    sample events, returns ``StructuredExtractionResult`` objects
    via pickle, and the parent runs #76 validation in-process."""
    from bigquery_agent_analytics.extractor_compilation import run_smoke_test_in_subprocess
    from tests.fixtures_extractor_compilation.bka_decision_template import BKA_DECISION_SOURCE

    source_path = tmp_path / "bka_subprocess.py"
    source_path.write_text(BKA_DECISION_SOURCE, encoding="utf-8")

    report = run_smoke_test_in_subprocess(
        source_path,
        module_name="bka_subprocess",
        function_name="extract_bka_decision_event_compiled",
        events=_sample_bka_events(),
        spec=None,
        resolved_graph=_bka_resolved_spec(),
        memory_limit_mb=None,
    )
    assert (
        report.ok is True
    ), f"failures: exc={report.exceptions} val={report.validation_failures}"
    assert report.events_with_nonempty_result == 2

  def test_min_nonempty_results_zero_allows_empty(self):
    """Callers can opt out of the non-empty floor for tests that
    deliberately exercise the empty-result path."""
    from bigquery_agent_analytics.extractor_compilation import run_smoke_test
    from bigquery_agent_analytics.structured_extraction import StructuredExtractionResult

    def extractor(event, spec):
      return StructuredExtractionResult()

    report = run_smoke_test(
        extractor,
        events=[{"event_type": "x"}],
        spec=None,
        resolved_graph=None,
        min_nonempty_results=0,
    )
    assert report.ok is True


# ------------------------------------------------------------------ #
# End-to-end compile_extractor                                         #
# ------------------------------------------------------------------ #


class TestCompileExtractor:

  def test_bka_fixture_compiles_clean(self, tmp_path: pathlib.Path):
    """Hand-authored BKA fixture clears every gate (AST + import
    + smoke + #76 validator) and produces an on-disk bundle."""
    from bigquery_agent_analytics.extractor_compilation import compile_extractor
    from tests.fixtures_extractor_compilation.bka_decision_template import BKA_DECISION_SOURCE

    spec = _bka_resolved_spec()
    result = compile_extractor(
        source=BKA_DECISION_SOURCE,
        module_name=_unique_module_name(),
        function_name="extract_bka_decision_event_compiled",
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
    ), f"compile failed: ast={result.ast_report.failures} smoke={result.smoke_report and result.smoke_report.exceptions or []} validator={result.smoke_report and result.smoke_report.validation_failures or []}"
    assert result.bundle_dir is not None
    assert result.manifest is not None
    assert (result.bundle_dir / "manifest.json").exists()
    assert (result.bundle_dir / result.manifest.module_filename).exists()

  def test_compiled_output_matches_handwritten_extractor(
      self, tmp_path: pathlib.Path
  ):
    """The whole point of the BKA fixture: its compiled output is
    semantically equivalent to ``extract_bka_decision_event``."""
    from bigquery_agent_analytics.extractor_compilation import compile_extractor
    from bigquery_agent_analytics.extractor_compilation import load_callable_from_source
    from bigquery_agent_analytics.structured_extraction import extract_bka_decision_event
    from tests.fixtures_extractor_compilation.bka_decision_template import BKA_DECISION_SOURCE

    module_name = _unique_module_name()
    spec = _bka_resolved_spec()
    result = compile_extractor(
        source=BKA_DECISION_SOURCE,
        module_name=module_name,
        function_name="extract_bka_decision_event_compiled",
        event_types=("bka_decision",),
        sample_events=_sample_bka_events(),
        spec=None,
        resolved_graph=spec,
        parent_bundle_dir=tmp_path,
        fingerprint_inputs=_fingerprint_inputs(),
        template_version="v0.1",
        compiler_package_version="0.0.0",
    )
    assert result.ok is True

    # Re-load the bundle by file path (matches what C2's loader
    # will do once it lands).
    compiled = load_callable_from_source(
        result.bundle_dir / result.manifest.module_filename,
        module_name=_unique_module_name(prefix="bka_reload_"),
        function_name="extract_bka_decision_event_compiled",
    )
    for event in _sample_bka_events():
      hand = extract_bka_decision_event(event, None)
      auto = compiled(event, None)
      assert _result_signature(hand) == _result_signature(
          auto
      ), f"compiled vs hand-written diverge on event {event!r}"

  def test_ast_failure_short_circuits_no_bundle_on_disk(
      self, tmp_path: pathlib.Path
  ):
    from bigquery_agent_analytics.extractor_compilation import compile_extractor

    bad_source = (
        "from os import system\n" "def f(event, spec):\n" "    return None\n"
    )
    result = compile_extractor(
        source=bad_source,
        module_name=_unique_module_name(prefix="bad_"),
        function_name="f",
        event_types=("x",),
        sample_events=[{"event_type": "x"}],
        spec=None,
        resolved_graph=None,
        parent_bundle_dir=tmp_path,
        fingerprint_inputs=_fingerprint_inputs(),
        template_version="v0.1",
        compiler_package_version="0.0.0",
    )
    assert result.ok is False
    assert result.bundle_dir is None
    assert result.smoke_report is None
    # No bundle directories created under tmp_path.
    assert not list(tmp_path.iterdir())

  def test_smoke_failure_cleans_up_partial_bundle(self, tmp_path: pathlib.Path):
    """When the smoke-test runner reports a validator failure, the
    harness must remove the source file it wrote and leave the
    bundle directory empty (or absent)."""
    from bigquery_agent_analytics.extractor_compilation import compile_extractor

    # Source clears the AST gate but produces a node with a
    # type_mismatch (decision_id as int instead of string).
    bad_source = '''
"""Compiled extractor that emits a type_mismatch node."""

from __future__ import annotations

from bigquery_agent_analytics.extracted_models import ExtractedNode
from bigquery_agent_analytics.extracted_models import ExtractedProperty
from bigquery_agent_analytics.structured_extraction import (
    StructuredExtractionResult,
)


def f(event, spec):
  return StructuredExtractionResult(
      nodes=[
          ExtractedNode(
              node_id="sess1:mako_DecisionPoint:decision_id=99",
              entity_name="mako_DecisionPoint",
              labels=["mako_DecisionPoint"],
              properties=[ExtractedProperty(name="decision_id", value=99)],
          )
      ]
  )
'''
    spec = _bka_resolved_spec()
    result = compile_extractor(
        source=bad_source,
        module_name=_unique_module_name(prefix="smoke_fail_"),
        function_name="f",
        event_types=("bka_decision",),
        sample_events=[{"event_type": "bka_decision"}],
        spec=None,
        resolved_graph=spec,
        parent_bundle_dir=tmp_path,
        fingerprint_inputs=_fingerprint_inputs(),
        template_version="v0.1",
        compiler_package_version="0.0.0",
    )
    assert result.ok is False
    assert result.smoke_report is not None
    assert any(
        f.code == "type_mismatch"
        for f in result.smoke_report.validation_failures
    )
    # Partial bundle dir was created and then removed.
    assert not list(tmp_path.iterdir())

  def test_second_compile_is_a_cache_hit(self, tmp_path: pathlib.Path):
    """Two compile runs on the same inputs land in the same
    fingerprint-named directory. The second run is a cache hit:
    it reads the existing manifest, validates fingerprint +
    function_name match, and returns without writing anything.
    The on-disk bundle is therefore byte-identical between
    consecutive ``compile_extractor`` calls — verified by
    asserting the ``created_at`` timestamp doesn't change."""
    from bigquery_agent_analytics.extractor_compilation import compile_extractor
    from tests.fixtures_extractor_compilation.bka_decision_template import BKA_DECISION_SOURCE

    spec = _bka_resolved_spec()
    kwargs = dict(
        source=BKA_DECISION_SOURCE,
        module_name="bka_stable",
        function_name="extract_bka_decision_event_compiled",
        event_types=("bka_decision",),
        sample_events=_sample_bka_events(),
        spec=None,
        resolved_graph=spec,
        parent_bundle_dir=tmp_path,
        fingerprint_inputs=_fingerprint_inputs(),
        template_version="v0.1",
        compiler_package_version="0.0.0",
    )
    a = compile_extractor(**kwargs)
    b = compile_extractor(**kwargs)
    assert a.ok and b.ok
    assert a.cache_hit is False
    assert b.cache_hit is True
    assert a.bundle_dir == b.bundle_dir
    assert a.manifest.fingerprint == b.manifest.fingerprint
    # Cache hit doesn't rewrite the manifest, so created_at is
    # preserved — proves the second call wrote nothing.
    assert a.manifest.created_at == b.manifest.created_at

  def test_corrupt_manifest_treated_as_cache_miss(self, tmp_path: pathlib.Path):
    """A pre-existing ``manifest.json`` that's syntactically valid
    JSON but the wrong shape (``42``, ``"oops"``, etc.) used to
    crash ``Manifest.from_json`` with ``TypeError``. The cache-read
    path now catches it and falls through to a fresh compile."""
    from bigquery_agent_analytics.extractor_compilation import compile_extractor
    from bigquery_agent_analytics.extractor_compilation import compute_fingerprint
    from tests.fixtures_extractor_compilation.bka_decision_template import BKA_DECISION_SOURCE

    fingerprint_inputs = _fingerprint_inputs()
    fp = compute_fingerprint(
        template_version="v0.1",
        compiler_package_version="0.0.0",
        **fingerprint_inputs,
    )
    bundle_dir = tmp_path / fp
    bundle_dir.mkdir()
    (bundle_dir / "manifest.json").write_text("42", encoding="utf-8")

    spec = _bka_resolved_spec()
    result = compile_extractor(
        source=BKA_DECISION_SOURCE,
        module_name="bka_corrupt_manifest",
        function_name="extract_bka_decision_event_compiled",
        event_types=("bka_decision",),
        sample_events=_sample_bka_events(),
        spec=None,
        resolved_graph=spec,
        parent_bundle_dir=tmp_path,
        fingerprint_inputs=fingerprint_inputs,
        template_version="v0.1",
        compiler_package_version="0.0.0",
        isolation=False,  # keep this test fast; the bug is in the cache-read path
    )
    assert result.ok is True
    assert result.cache_hit is False
    assert (result.bundle_dir / "manifest.json").exists()
    # Manifest is now well-formed, not the literal "42".
    assert (result.bundle_dir / "manifest.json").read_text() != "42"

  def test_cache_hit_import_failure_falls_through(self, tmp_path: pathlib.Path):
    """If the cached bundle's source can't be imported (e.g., the
    .py file disappeared while the manifest stuck around), the
    cache-hit path falls through to a fresh compile rather than
    returning a misleading failure."""
    from bigquery_agent_analytics.extractor_compilation import compile_extractor
    from tests.fixtures_extractor_compilation.bka_decision_template import BKA_DECISION_SOURCE

    spec = _bka_resolved_spec()
    common = dict(
        source=BKA_DECISION_SOURCE,
        module_name="bka_resurrect",
        function_name="extract_bka_decision_event_compiled",
        event_types=("bka_decision",),
        sample_events=_sample_bka_events(),
        spec=None,
        resolved_graph=spec,
        parent_bundle_dir=tmp_path,
        fingerprint_inputs=_fingerprint_inputs(),
        template_version="v0.1",
        compiler_package_version="0.0.0",
        isolation=False,
    )
    a = compile_extractor(**common)
    assert a.ok is True

    # Corrupt the cached bundle: remove the .py while keeping
    # manifest.json. Cache-match passes (manifest fields all good)
    # but ``_smoke_test_cached_bundle`` returns None since import
    # fails. The compiler must fall through to a fresh compile,
    # not bubble up an ``ok=False``.
    (a.bundle_dir / a.manifest.module_filename).unlink()

    b = compile_extractor(**common)
    assert b.ok is True
    assert b.cache_hit is False
    assert (b.bundle_dir / b.manifest.module_filename).exists()

  def test_cache_hit_re_runs_smoke_against_current_inputs(
      self, tmp_path: pathlib.Path
  ):
    """A cached bundle that passed against a weak sample set must
    not silently pass against a stricter one. The cache hit path
    re-runs ``run_smoke_test`` against the current inputs."""
    from bigquery_agent_analytics.extractor_compilation import compile_extractor
    from tests.fixtures_extractor_compilation.bka_decision_template import BKA_DECISION_SOURCE

    spec = _bka_resolved_spec()
    common = dict(
        source=BKA_DECISION_SOURCE,
        module_name="bka_smoke_re_run",
        function_name="extract_bka_decision_event_compiled",
        event_types=("bka_decision",),
        spec=None,
        resolved_graph=spec,
        parent_bundle_dir=tmp_path,
        fingerprint_inputs=_fingerprint_inputs(),
        template_version="v0.1",
        compiler_package_version="0.0.0",
    )

    # First compile against the BKA samples — passes.
    a = compile_extractor(sample_events=_sample_bka_events(), **common)
    assert a.ok and not a.cache_hit

    # Second compile with the same source/module/event_types but
    # an event that hits the empty-result path. Cache match
    # passes (source bytes equal), but the smoke gate now reports
    # ``events_with_nonempty_result == 0`` and the cache hit
    # fails. Bundle is left intact on disk.
    empty_event = [{"event_type": "bka_decision", "content": {}}]
    b = compile_extractor(sample_events=empty_event, **common)
    assert b.ok is False
    assert b.cache_hit is False
    assert b.smoke_report is not None
    assert b.smoke_report.events_with_nonempty_result == 0
    # The cached bundle is still on disk (rewriting wouldn't
    # change anything; same source).
    assert (a.bundle_dir / "manifest.json").exists()

  def test_cache_hit_misses_when_source_differs(self, tmp_path: pathlib.Path):
    """The fingerprint covers the #75 input tuple but NOT the
    candidate source. A second compile with different source must
    not be a cache hit — it has to re-run AST + smoke + validator
    on the *actual* new source."""
    from bigquery_agent_analytics.extractor_compilation import compile_extractor
    from tests.fixtures_extractor_compilation.bka_decision_template import BKA_DECISION_SOURCE

    spec = _bka_resolved_spec()
    common = dict(
        module_name="bka_src_diff",
        function_name="extract_bka_decision_event_compiled",
        event_types=("bka_decision",),
        sample_events=_sample_bka_events(),
        spec=None,
        resolved_graph=spec,
        parent_bundle_dir=tmp_path,
        fingerprint_inputs=_fingerprint_inputs(),
        template_version="v0.1",
        compiler_package_version="0.0.0",
    )

    a = compile_extractor(source=BKA_DECISION_SOURCE, **common)
    assert a.ok and not a.cache_hit

    # Same fingerprint inputs, same module/function/event_types,
    # but different source. Must NOT be a cache hit.
    altered_source = BKA_DECISION_SOURCE + "\n# trailing comment\n"
    b = compile_extractor(source=altered_source, **common)
    assert b.ok is True
    assert (
        b.cache_hit is False
    ), "second compile with different source must not cache-hit"

  def test_cache_hit_misses_when_module_name_differs(
      self, tmp_path: pathlib.Path
  ):
    """A second compile with the same fingerprint but a different
    module_name lands in the same bundle dir but with a different
    on-disk filename. Must not be a cache hit."""
    from bigquery_agent_analytics.extractor_compilation import compile_extractor
    from tests.fixtures_extractor_compilation.bka_decision_template import BKA_DECISION_SOURCE

    spec = _bka_resolved_spec()
    common = dict(
        source=BKA_DECISION_SOURCE,
        function_name="extract_bka_decision_event_compiled",
        event_types=("bka_decision",),
        sample_events=_sample_bka_events(),
        spec=None,
        resolved_graph=spec,
        parent_bundle_dir=tmp_path,
        fingerprint_inputs=_fingerprint_inputs(),
        template_version="v0.1",
        compiler_package_version="0.0.0",
    )

    a = compile_extractor(module_name="module_a", **common)
    assert a.ok and not a.cache_hit
    b = compile_extractor(module_name="module_b", **common)
    assert b.ok is True
    assert b.cache_hit is False
    # The atomic-replace put module_b.py on disk; module_a.py is gone.
    assert (b.bundle_dir / "module_b.py").exists()
    assert not (b.bundle_dir / "module_a.py").exists()

  def test_cache_hit_misses_when_event_types_differ(
      self, tmp_path: pathlib.Path
  ):
    """A second compile with different per-bundle ``event_types``
    coverage is a different request. Must not be a cache hit."""
    from bigquery_agent_analytics.extractor_compilation import compile_extractor
    from tests.fixtures_extractor_compilation.bka_decision_template import BKA_DECISION_SOURCE

    spec = _bka_resolved_spec()
    # The BKA fixture's extractor emits a node for any event
    # whose ``content`` carries a ``decision_id`` (it doesn't
    # branch on event_type). Including a tool_completed sample
    # with a decision_id lets compile B clear the new "declared
    # event_types must demonstrate non-empty coverage" gate.
    extended_samples = _sample_bka_events() + [
        {
            "event_type": "tool_completed",
            "session_id": "sess1",
            "span_id": "span_tc",
            "content": {"decision_id": "tc1"},
        }
    ]
    common = dict(
        source=BKA_DECISION_SOURCE,
        module_name="bka_event_diff",
        function_name="extract_bka_decision_event_compiled",
        sample_events=extended_samples,
        spec=None,
        resolved_graph=spec,
        parent_bundle_dir=tmp_path,
        fingerprint_inputs=_fingerprint_inputs(),
        template_version="v0.1",
        compiler_package_version="0.0.0",
    )

    a = compile_extractor(event_types=("bka_decision",), **common)
    assert a.ok and not a.cache_hit
    b = compile_extractor(
        event_types=("bka_decision", "tool_completed"), **common
    )
    assert b.ok is True
    assert b.cache_hit is False
    assert b.manifest.event_types == ("bka_decision", "tool_completed")

  def test_failed_recompile_leaves_existing_bundle_intact(
      self, tmp_path: pathlib.Path
  ):
    """A successful first compile, followed by a second compile
    with broken source under the *same* fingerprint inputs but a
    different ``compiler_package_version`` (so it's not a cache
    hit), must NOT corrupt the original bundle. Atomic-replace via
    a staging dir is what guarantees this."""
    from bigquery_agent_analytics.extractor_compilation import compile_extractor
    from tests.fixtures_extractor_compilation.bka_decision_template import BKA_DECISION_SOURCE

    spec = _bka_resolved_spec()
    common = dict(
        module_name="bka_intact",
        function_name="extract_bka_decision_event_compiled",
        event_types=("bka_decision",),
        sample_events=_sample_bka_events(),
        spec=None,
        resolved_graph=spec,
        parent_bundle_dir=tmp_path,
        fingerprint_inputs=_fingerprint_inputs(),
        template_version="v0.1",
    )
    good = compile_extractor(
        source=BKA_DECISION_SOURCE,
        compiler_package_version="0.0.0",
        **common,
    )
    assert good.ok is True
    good_dir = good.bundle_dir
    good_module = (good_dir / good.manifest.module_filename).read_text()
    good_manifest = (good_dir / "manifest.json").read_text()

    # Second compile with broken source: but keep the same
    # fingerprint inputs and module/function names. Since AST
    # would short-circuit before any bundle is touched, force the
    # failure later by failing the smoke test instead — emit a
    # type_mismatch.
    bad_source = '''
"""Bad compiled extractor that produces a type_mismatch."""

from __future__ import annotations

from bigquery_agent_analytics.extracted_models import ExtractedNode
from bigquery_agent_analytics.extracted_models import ExtractedProperty
from bigquery_agent_analytics.structured_extraction import (
    StructuredExtractionResult,
)


def extract_bka_decision_event_compiled(event, spec):
  return StructuredExtractionResult(
      nodes=[
          ExtractedNode(
              node_id="sess1:mako_DecisionPoint:decision_id=99",
              entity_name="mako_DecisionPoint",
              labels=["mako_DecisionPoint"],
              properties=[ExtractedProperty(name="decision_id", value=99)],
          )
      ]
  )
'''
    # Bump the compiler version so the cache-hit short-circuit
    # doesn't fire; we want to actually re-run the gates.
    bad = compile_extractor(
        source=bad_source,
        compiler_package_version="0.0.1",
        **common,
    )
    assert bad.ok is False
    assert bad.smoke_report is not None
    assert any(
        f.code == "type_mismatch" for f in bad.smoke_report.validation_failures
    )

    # Original bundle must still be on disk and byte-identical.
    assert good_dir.exists()
    assert (good_dir / good.manifest.module_filename).read_text() == good_module
    assert (good_dir / "manifest.json").read_text() == good_manifest

  @pytest.mark.parametrize(
      "module_name",
      [
          "../escape",
          "foo.bar",
          "foo-bar",
          "1leading_digit",
          "",
          "with space",
      ],
  )
  def test_invalid_module_name_rejected(
      self, module_name: str, tmp_path: pathlib.Path
  ):
    """``module_filename = f'{module_name}.py'`` is used directly
    as a path; path-traversal-shaped names must be rejected before
    the harness ever touches the filesystem."""
    from bigquery_agent_analytics.extractor_compilation import compile_extractor
    from tests.fixtures_extractor_compilation.bka_decision_template import BKA_DECISION_SOURCE

    result = compile_extractor(
        source=BKA_DECISION_SOURCE,
        module_name=module_name,
        function_name="extract_bka_decision_event_compiled",
        event_types=("bka_decision",),
        sample_events=_sample_bka_events(),
        spec=None,
        resolved_graph=_bka_resolved_spec(),
        parent_bundle_dir=tmp_path,
        fingerprint_inputs=_fingerprint_inputs(),
        template_version="v0.1",
        compiler_package_version="0.0.0",
    )
    assert result.ok is False
    assert result.invalid_identifier is not None
    assert "module_name" in result.invalid_identifier
    # No filesystem writes happened.
    assert not list(tmp_path.iterdir())

  def test_empty_event_types_rejected(self, tmp_path: pathlib.Path):
    """A bundle has to claim coverage for *something*. Empty
    ``event_types`` means the manifest's coverage claim is
    vacuous."""
    from bigquery_agent_analytics.extractor_compilation import compile_extractor
    from tests.fixtures_extractor_compilation.bka_decision_template import BKA_DECISION_SOURCE

    result = compile_extractor(
        source=BKA_DECISION_SOURCE,
        module_name="empty_event_types",
        function_name="extract_bka_decision_event_compiled",
        event_types=(),
        sample_events=_sample_bka_events(),
        spec=None,
        resolved_graph=_bka_resolved_spec(),
        parent_bundle_dir=tmp_path,
        fingerprint_inputs=_fingerprint_inputs(),
        template_version="v0.1",
        compiler_package_version="0.0.0",
    )
    assert result.ok is False
    assert result.invalid_event_types is not None
    assert "non-empty" in result.invalid_event_types

  @pytest.mark.parametrize(
      "bad_event_types",
      [
          (1,),
          ("",),
          (None,),
          ("ok", 2),
          ("ok", ""),
      ],
  )
  def test_non_string_event_types_rejected(
      self, bad_event_types, tmp_path: pathlib.Path
  ):
    """Every declared event type is a public manifest field; non-
    string or empty entries make the manifest contract incoherent.
    Catch them directly rather than indirectly via 'no matching
    sample'."""
    from bigquery_agent_analytics.extractor_compilation import compile_extractor
    from tests.fixtures_extractor_compilation.bka_decision_template import BKA_DECISION_SOURCE

    result = compile_extractor(
        source=BKA_DECISION_SOURCE,
        module_name="bad_event_types",
        function_name="extract_bka_decision_event_compiled",
        event_types=bad_event_types,
        sample_events=_sample_bka_events(),
        spec=None,
        resolved_graph=_bka_resolved_spec(),
        parent_bundle_dir=tmp_path,
        fingerprint_inputs=_fingerprint_inputs(),
        template_version="v0.1",
        compiler_package_version="0.0.0",
    )
    assert result.ok is False
    assert result.invalid_event_types is not None
    assert "non-empty string" in result.invalid_event_types

  def test_duplicate_event_types_rejected(self, tmp_path: pathlib.Path):
    """A manifest claiming ``("x", "x")`` is just noisy. Reject
    duplicates so the C2 loader sees a clean ``set``-equivalent
    contract."""
    from bigquery_agent_analytics.extractor_compilation import compile_extractor
    from tests.fixtures_extractor_compilation.bka_decision_template import BKA_DECISION_SOURCE

    result = compile_extractor(
        source=BKA_DECISION_SOURCE,
        module_name="dup_event_types",
        function_name="extract_bka_decision_event_compiled",
        event_types=("bka_decision", "bka_decision"),
        sample_events=_sample_bka_events(),
        spec=None,
        resolved_graph=_bka_resolved_spec(),
        parent_bundle_dir=tmp_path,
        fingerprint_inputs=_fingerprint_inputs(),
        template_version="v0.1",
        compiler_package_version="0.0.0",
    )
    assert result.ok is False
    assert result.invalid_event_types is not None
    assert "duplicates" in result.invalid_event_types

  def test_malformed_sample_event_types_rejected(self, tmp_path: pathlib.Path):
    """The previous validator sorted sample event_type values
    directly — mixing ``int`` and ``str`` made
    ``sorted(...)`` raise ``TypeError`` while formatting the
    error message. Now malformed event_type values are caught
    *before* the missing-coverage check with a clear message."""
    from bigquery_agent_analytics.extractor_compilation import compile_extractor
    from tests.fixtures_extractor_compilation.bka_decision_template import BKA_DECISION_SOURCE

    result = compile_extractor(
        source=BKA_DECISION_SOURCE,
        module_name="malformed_sample_types",
        function_name="extract_bka_decision_event_compiled",
        event_types=("bka_decision",),
        sample_events=[
            {
                "event_type": 1,
                "session_id": "sess1",
                "content": {},
            },
            {
                "event_type": "a",
                "session_id": "sess1",
                "content": {"decision_id": "d1"},
            },
        ],
        spec=None,
        resolved_graph=_bka_resolved_spec(),
        parent_bundle_dir=tmp_path,
        fingerprint_inputs=_fingerprint_inputs(),
        template_version="v0.1",
        compiler_package_version="0.0.0",
    )
    assert result.ok is False
    assert result.invalid_event_types is not None
    assert "non-empty string 'event_type'" in result.invalid_event_types
    # No partial bundle written.
    assert not list(tmp_path.iterdir())

  def test_event_types_must_have_nonempty_smoke_coverage(
      self, tmp_path: pathlib.Path
  ):
    """A declared event_type with a sample but no non-empty
    smoke output is still vacuous coverage. The reviewer's
    repro: declare ``("x",)`` with one empty x sample plus a
    nonempty y sample. The bundle's claim of x coverage is
    untestable; the harness must reject it."""
    from bigquery_agent_analytics.extractor_compilation import compile_extractor

    # The BKA fixture emits when ``content.decision_id`` is set,
    # regardless of event_type. So an x sample with empty content
    # produces nothing; a y sample with a decision_id produces
    # output. Declaring x as the bundle's event_types is then
    # vacuous coverage.
    samples = [
        {
            "event_type": "x",
            "session_id": "sess1",
            "span_id": "spx",
            "content": {},  # no decision_id => empty result
        },
        {
            "event_type": "y",
            "session_id": "sess1",
            "span_id": "spy",
            "content": {"decision_id": "y1"},
        },
    ]

    from tests.fixtures_extractor_compilation.bka_decision_template import BKA_DECISION_SOURCE

    result = compile_extractor(
        source=BKA_DECISION_SOURCE,
        module_name="bka_no_x_coverage",
        function_name="extract_bka_decision_event_compiled",
        event_types=("x",),
        sample_events=samples,
        spec=None,
        resolved_graph=_bka_resolved_spec(),
        parent_bundle_dir=tmp_path,
        fingerprint_inputs=_fingerprint_inputs(),
        template_version="v0.1",
        compiler_package_version="0.0.0",
    )
    assert result.ok is False
    assert result.invalid_event_types is not None
    assert "no non-empty smoke output" in result.invalid_event_types
    assert "'x'" in result.invalid_event_types

  def test_event_types_without_sample_coverage_rejected(
      self, tmp_path: pathlib.Path
  ):
    """``event_types=("wrong_event",)`` while every sample is
    ``"bka_decision"`` is an obvious metadata/sample mismatch.
    The manifest's coverage claim would be untestable."""
    from bigquery_agent_analytics.extractor_compilation import compile_extractor
    from tests.fixtures_extractor_compilation.bka_decision_template import BKA_DECISION_SOURCE

    result = compile_extractor(
        source=BKA_DECISION_SOURCE,
        module_name="event_type_lie",
        function_name="extract_bka_decision_event_compiled",
        event_types=("wrong_event",),
        sample_events=_sample_bka_events(),
        spec=None,
        resolved_graph=_bka_resolved_spec(),
        parent_bundle_dir=tmp_path,
        fingerprint_inputs=_fingerprint_inputs(),
        template_version="v0.1",
        compiler_package_version="0.0.0",
    )
    assert result.ok is False
    assert result.invalid_event_types is not None
    assert "wrong_event" in result.invalid_event_types
    # No bundle should have been written.
    assert not list(tmp_path.iterdir())

  @pytest.mark.parametrize("kw", ["class", "def", "for", "return"])
  def test_python_keyword_rejected_as_module_name(
      self, kw: str, tmp_path: pathlib.Path
  ):
    """``str.isidentifier`` returns True for keywords; the harness
    rejects them too. ``module_name='class'`` would work as a
    filename but is misleading; ``function_name='class'`` can't be
    defined by valid source at all."""
    from bigquery_agent_analytics.extractor_compilation import compile_extractor
    from tests.fixtures_extractor_compilation.bka_decision_template import BKA_DECISION_SOURCE

    result = compile_extractor(
        source=BKA_DECISION_SOURCE,
        module_name=kw,
        function_name="extract_bka_decision_event_compiled",
        event_types=("bka_decision",),
        sample_events=_sample_bka_events(),
        spec=None,
        resolved_graph=_bka_resolved_spec(),
        parent_bundle_dir=tmp_path,
        fingerprint_inputs=_fingerprint_inputs(),
        template_version="v0.1",
        compiler_package_version="0.0.0",
    )
    assert result.ok is False
    assert result.invalid_identifier is not None
    assert "module_name" in result.invalid_identifier

  def test_invalid_function_name_rejected(self, tmp_path: pathlib.Path):
    from bigquery_agent_analytics.extractor_compilation import compile_extractor
    from tests.fixtures_extractor_compilation.bka_decision_template import BKA_DECISION_SOURCE

    result = compile_extractor(
        source=BKA_DECISION_SOURCE,
        module_name="ok_module",
        function_name="../escape",
        event_types=("bka_decision",),
        sample_events=_sample_bka_events(),
        spec=None,
        resolved_graph=_bka_resolved_spec(),
        parent_bundle_dir=tmp_path,
        fingerprint_inputs=_fingerprint_inputs(),
        template_version="v0.1",
        compiler_package_version="0.0.0",
    )
    assert result.ok is False
    assert result.invalid_identifier is not None
    assert "function_name" in result.invalid_identifier
    assert not list(tmp_path.iterdir())


# ------------------------------------------------------------------ #
# Helpers                                                              #
# ------------------------------------------------------------------ #


def _result_signature(result):
  """Tuple form that compares two ``StructuredExtractionResult``
  instances structurally — order of properties matters here so an
  extractor that re-orders fields would be flagged."""
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
