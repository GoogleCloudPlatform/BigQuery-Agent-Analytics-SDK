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

"""Tests for the JSON-to-plan parser (issue #75 PR 4b.2.2.a).

Coverage:
- Golden BKA JSON fixture parses to a ``ResolvedExtractorPlan``
  equivalent to the hand-authored plan.
- Parsed BKA plan renders + compiles end-to-end through 4b.1's
  ``compile_extractor`` (subprocess smoke + #76 validator).
- Default values (omitted ``property_fields``,
  ``session_id_path``, ``span_handling``) produce the expected
  fallbacks.
- Schema-level rejections: invalid JSON, wrong root type,
  missing required fields, unknown fields, wrong types, empty
  strings, empty paths, non-string path segments.
- Semantic rejections: invalid identifiers (function_name and
  property_names), function_name shadowing, duplicate property
  names.

The parser is **not** an LLM — every test feeds hand-authored
JSON. PR 4b.2.2.b adds the actual LLM step that produces this
JSON.
"""

from __future__ import annotations

import json
import pathlib
import tempfile
import uuid

import pytest

# ------------------------------------------------------------------ #
# Shared fixtures                                                     #
# ------------------------------------------------------------------ #


_BKA_PLAN_FIXTURE_PATH = (
    pathlib.Path(__file__).parent
    / "fixtures_extractor_compilation"
    / "plan_bka_decision.json"
)


def _load_bka_payload() -> dict:
  return json.loads(_BKA_PLAN_FIXTURE_PATH.read_text(encoding="utf-8"))


def _bka_handwritten_plan():
  """Match the hand-authored BKA plan from
  ``test_extractor_compilation_template.py``. Used to compare
  parser output against the canonical hand-built plan."""
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

  tmp = pathlib.Path(tempfile.mkdtemp(prefix="bka_plan_parser_test_"))
  (tmp / "ont.yaml").write_text(_BKA_ONTOLOGY_YAML, encoding="utf-8")
  (tmp / "bnd.yaml").write_text(_BKA_BINDING_YAML, encoding="utf-8")
  ontology = load_ontology(str(tmp / "ont.yaml"))
  binding = load_binding(str(tmp / "bnd.yaml"), ontology=ontology)
  return resolve(ontology, binding)


def _sample_bka_events():
  return [
      {
          "event_type": "bka_decision",
          "session_id": "sess1",
          "span_id": "span1",
          "content": {
              "decision_id": "d1",
              "outcome": "approved",
              "confidence": 0.92,
              "reasoning_text": "rationale",
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


def _unique_module_name(prefix: str = "parsed_") -> str:
  return f"{prefix}{uuid.uuid4().hex[:12]}"


# ------------------------------------------------------------------ #
# Golden BKA + end-to-end                                             #
# ------------------------------------------------------------------ #


class TestBkaGolden:

  def test_bka_json_parses_to_handwritten_plan(self):
    """The committed JSON fixture parses to the same dataclass
    the renderer tests construct by hand."""
    from bigquery_agent_analytics.extractor_compilation import parse_resolved_extractor_plan_json

    payload = _BKA_PLAN_FIXTURE_PATH.read_text(encoding="utf-8")
    parsed = parse_resolved_extractor_plan_json(payload)
    assert parsed == _bka_handwritten_plan()

  def test_bka_json_parses_from_dict_input(self):
    """Parser also accepts an already-parsed dict (skips
    ``json.loads``) so callers that already deserialized the
    LLM response can reuse the validator."""
    from bigquery_agent_analytics.extractor_compilation import parse_resolved_extractor_plan_json

    parsed = parse_resolved_extractor_plan_json(_load_bka_payload())
    assert parsed == _bka_handwritten_plan()

  def test_bka_parsed_plan_renders_and_compiles(self, tmp_path: pathlib.Path):
    """The parsed BKA plan must render via the 4b.2.1 renderer
    AND compile end-to-end through 4b.1's ``compile_extractor``
    (subprocess smoke + #76 validator). Locks the chain
    parser → renderer → compiler so 4b.2.2.b can plug in the LLM
    step without re-proving the downstream pipeline works."""
    from bigquery_agent_analytics.extractor_compilation import compile_extractor
    from bigquery_agent_analytics.extractor_compilation import parse_resolved_extractor_plan_json
    from bigquery_agent_analytics.extractor_compilation import render_extractor_source

    plan = parse_resolved_extractor_plan_json(_load_bka_payload())
    src = render_extractor_source(plan)
    spec = _bka_resolved_spec()

    result = compile_extractor(
        source=src,
        module_name=_unique_module_name(prefix="bka_parsed_"),
        function_name=plan.function_name,
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
    ), f"compile failed: ast={result.ast_report.failures} smoke={result.smoke_report and (result.smoke_report.exceptions, result.smoke_report.validation_failures)}"


# ------------------------------------------------------------------ #
# Exported JSON Schema                                                #
# ------------------------------------------------------------------ #


class TestExportedJsonSchema:
  """The exported ``RESOLVED_EXTRACTOR_PLAN_JSON_SCHEMA`` is what
  PR 4b.2.2.b will hand to the LLM client's structured-output
  mode (Gemini's ``response_schema``, OpenAI's ``json_schema``
  response format, etc.). Lock down that the golden BKA fixture
  conforms to it AND that obviously-bad payloads don't, so the
  schema doesn't silently drift away from the parser."""

  def test_schema_is_a_well_formed_dict(self):
    from bigquery_agent_analytics.extractor_compilation import RESOLVED_EXTRACTOR_PLAN_JSON_SCHEMA

    assert isinstance(RESOLVED_EXTRACTOR_PLAN_JSON_SCHEMA, dict)
    assert RESOLVED_EXTRACTOR_PLAN_JSON_SCHEMA.get("type") == "object"
    assert (
        RESOLVED_EXTRACTOR_PLAN_JSON_SCHEMA.get("additionalProperties") is False
    ), "schema must reject unknown fields, mirroring parser"

  def test_bka_golden_fixture_conforms_to_schema(self):
    jsonschema = pytest.importorskip("jsonschema")
    from bigquery_agent_analytics.extractor_compilation import RESOLVED_EXTRACTOR_PLAN_JSON_SCHEMA

    payload = _load_bka_payload()
    # No exception → conforms.
    jsonschema.validate(
        instance=payload, schema=RESOLVED_EXTRACTOR_PLAN_JSON_SCHEMA
    )

  def test_minimal_payload_conforms_to_schema(self):
    """Smallest-valid payload (just required fields, all optionals
    omitted) conforms — guards against accidentally tightening the
    schema beyond what the parser accepts."""
    jsonschema = pytest.importorskip("jsonschema")
    from bigquery_agent_analytics.extractor_compilation import RESOLVED_EXTRACTOR_PLAN_JSON_SCHEMA

    payload = {
        "event_type": "x",
        "target_entity_name": "E",
        "function_name": "f",
        "key_field": {"property_name": "k", "source_path": ["k"]},
    }
    jsonschema.validate(
        instance=payload, schema=RESOLVED_EXTRACTOR_PLAN_JSON_SCHEMA
    )

  def test_payload_with_unknown_field_rejected_by_schema(self):
    jsonschema = pytest.importorskip("jsonschema")
    from bigquery_agent_analytics.extractor_compilation import RESOLVED_EXTRACTOR_PLAN_JSON_SCHEMA

    payload = _load_bka_payload()
    payload["extra_field"] = "nope"
    with pytest.raises(jsonschema.ValidationError):
      jsonschema.validate(
          instance=payload, schema=RESOLVED_EXTRACTOR_PLAN_JSON_SCHEMA
      )

  def test_payload_missing_required_rejected_by_schema(self):
    jsonschema = pytest.importorskip("jsonschema")
    from bigquery_agent_analytics.extractor_compilation import RESOLVED_EXTRACTOR_PLAN_JSON_SCHEMA

    payload = _load_bka_payload()
    del payload["event_type"]
    with pytest.raises(jsonschema.ValidationError):
      jsonschema.validate(
          instance=payload, schema=RESOLVED_EXTRACTOR_PLAN_JSON_SCHEMA
      )

  def test_payload_with_empty_source_path_rejected_by_schema(self):
    jsonschema = pytest.importorskip("jsonschema")
    from bigquery_agent_analytics.extractor_compilation import RESOLVED_EXTRACTOR_PLAN_JSON_SCHEMA

    payload = _load_bka_payload()
    payload["key_field"]["source_path"] = []
    with pytest.raises(jsonschema.ValidationError):
      jsonschema.validate(
          instance=payload, schema=RESOLVED_EXTRACTOR_PLAN_JSON_SCHEMA
      )

  def test_payload_with_wrong_type_rejected_by_schema(self):
    jsonschema = pytest.importorskip("jsonschema")
    from bigquery_agent_analytics.extractor_compilation import RESOLVED_EXTRACTOR_PLAN_JSON_SCHEMA

    payload = _load_bka_payload()
    payload["event_type"] = 42  # wrong type
    with pytest.raises(jsonschema.ValidationError):
      jsonschema.validate(
          instance=payload, schema=RESOLVED_EXTRACTOR_PLAN_JSON_SCHEMA
      )

  def test_null_span_handling_conforms_to_schema(self):
    """The schema's ``oneOf: [null, SpanHandlingRule]`` for
    ``span_handling`` is a common LLM-output tripwire — make sure
    it actually accepts ``null``."""
    jsonschema = pytest.importorskip("jsonschema")
    from bigquery_agent_analytics.extractor_compilation import RESOLVED_EXTRACTOR_PLAN_JSON_SCHEMA

    payload = _load_bka_payload()
    payload["span_handling"] = None
    jsonschema.validate(
        instance=payload, schema=RESOLVED_EXTRACTOR_PLAN_JSON_SCHEMA
    )


# ------------------------------------------------------------------ #
# Default values for optional fields                                  #
# ------------------------------------------------------------------ #


class TestDefaults:

  def _minimal_payload(self) -> dict:
    return {
        "event_type": "x",
        "target_entity_name": "E",
        "function_name": "f",
        "key_field": {"property_name": "k", "source_path": ["k"]},
    }

  def test_omitted_property_fields_defaults_to_empty(self):
    from bigquery_agent_analytics.extractor_compilation import parse_resolved_extractor_plan_json

    plan = parse_resolved_extractor_plan_json(self._minimal_payload())
    assert plan.property_fields == ()

  def test_omitted_session_id_path_defaults_to_session_id(self):
    from bigquery_agent_analytics.extractor_compilation import parse_resolved_extractor_plan_json

    plan = parse_resolved_extractor_plan_json(self._minimal_payload())
    assert plan.session_id_path == ("session_id",)

  def test_omitted_span_handling_defaults_to_none(self):
    from bigquery_agent_analytics.extractor_compilation import parse_resolved_extractor_plan_json

    plan = parse_resolved_extractor_plan_json(self._minimal_payload())
    assert plan.span_handling is None

  def test_explicit_null_span_handling_treated_as_none(self):
    from bigquery_agent_analytics.extractor_compilation import parse_resolved_extractor_plan_json

    payload = self._minimal_payload()
    payload["span_handling"] = None
    plan = parse_resolved_extractor_plan_json(payload)
    assert plan.span_handling is None

  def test_span_handling_with_default_span_id_path(self):
    """``span_id_path`` defaults to ``["span_id"]`` inside
    ``span_handling`` when the field is omitted."""
    from bigquery_agent_analytics.extractor_compilation import parse_resolved_extractor_plan_json

    payload = self._minimal_payload()
    payload["span_handling"] = {}
    plan = parse_resolved_extractor_plan_json(payload)
    assert plan.span_handling is not None
    assert plan.span_handling.span_id_path == ("span_id",)
    assert plan.span_handling.partial_when_path is None

  def test_explicit_null_partial_when_path(self):
    from bigquery_agent_analytics.extractor_compilation import parse_resolved_extractor_plan_json

    payload = self._minimal_payload()
    payload["span_handling"] = {
        "span_id_path": ["span_id"],
        "partial_when_path": None,
    }
    plan = parse_resolved_extractor_plan_json(payload)
    assert plan.span_handling.partial_when_path is None


# ------------------------------------------------------------------ #
# Schema-level rejections                                             #
# ------------------------------------------------------------------ #


class TestSchemaRejections:

  def _minimal_payload(self) -> dict:
    return {
        "event_type": "x",
        "target_entity_name": "E",
        "function_name": "f",
        "key_field": {"property_name": "k", "source_path": ["k"]},
    }

  def _import(self):
    from bigquery_agent_analytics.extractor_compilation import parse_resolved_extractor_plan_json
    from bigquery_agent_analytics.extractor_compilation import PlanParseError

    return parse_resolved_extractor_plan_json, PlanParseError

  def test_invalid_json_string(self):
    parse, PlanParseError = self._import()
    with pytest.raises(PlanParseError) as exc_info:
      parse("{not json")
    assert exc_info.value.code == "invalid_json"

  def test_non_string_non_dict_payload(self):
    parse, PlanParseError = self._import()
    with pytest.raises(PlanParseError) as exc_info:
      parse(42)
    assert exc_info.value.code == "wrong_root_type"

  def test_root_is_list_not_object(self):
    parse, PlanParseError = self._import()
    with pytest.raises(PlanParseError) as exc_info:
      parse("[1, 2, 3]")
    assert exc_info.value.code == "wrong_root_type"

  @pytest.mark.parametrize(
      "field",
      ["event_type", "target_entity_name", "function_name", "key_field"],
  )
  def test_missing_required_top_level_field(self, field):
    parse, PlanParseError = self._import()
    payload = self._minimal_payload()
    payload.pop(field)
    with pytest.raises(PlanParseError) as exc_info:
      parse(payload)
    assert exc_info.value.code == "missing_required_field"
    assert exc_info.value.path == field

  def test_unknown_top_level_field(self):
    parse, PlanParseError = self._import()
    payload = self._minimal_payload()
    payload["extra"] = "nope"
    with pytest.raises(PlanParseError) as exc_info:
      parse(payload)
    assert exc_info.value.code == "unknown_field"
    assert exc_info.value.path == "extra"

  def test_unknown_field_inside_key_field(self):
    parse, PlanParseError = self._import()
    payload = self._minimal_payload()
    payload["key_field"]["extra"] = "nope"
    with pytest.raises(PlanParseError) as exc_info:
      parse(payload)
    assert exc_info.value.code == "unknown_field"
    assert exc_info.value.path == "key_field.extra"

  def test_unknown_field_inside_span_handling(self):
    parse, PlanParseError = self._import()
    payload = self._minimal_payload()
    payload["span_handling"] = {"span_id_path": ["s"], "weird": 1}
    with pytest.raises(PlanParseError) as exc_info:
      parse(payload)
    assert exc_info.value.code == "unknown_field"
    assert exc_info.value.path == "span_handling.weird"

  @pytest.mark.parametrize(
      "field, value",
      [
          ("event_type", 1),
          ("event_type", None),
          ("target_entity_name", []),
          ("function_name", {"x": 1}),
      ],
  )
  def test_wrong_type_top_level_string_field(self, field, value):
    parse, PlanParseError = self._import()
    payload = self._minimal_payload()
    payload[field] = value
    with pytest.raises(PlanParseError) as exc_info:
      parse(payload)
    assert exc_info.value.code == "wrong_type"
    assert exc_info.value.path == field

  def test_empty_string_event_type(self):
    parse, PlanParseError = self._import()
    payload = self._minimal_payload()
    payload["event_type"] = ""
    with pytest.raises(PlanParseError) as exc_info:
      parse(payload)
    assert exc_info.value.code == "empty_string"
    assert exc_info.value.path == "event_type"

  def test_property_fields_must_be_list(self):
    parse, PlanParseError = self._import()
    payload = self._minimal_payload()
    payload["property_fields"] = {"not": "a list"}
    with pytest.raises(PlanParseError) as exc_info:
      parse(payload)
    assert exc_info.value.code == "wrong_type"
    assert exc_info.value.path == "property_fields"

  def test_property_field_must_be_object(self):
    parse, PlanParseError = self._import()
    payload = self._minimal_payload()
    payload["property_fields"] = ["not_an_object"]
    with pytest.raises(PlanParseError) as exc_info:
      parse(payload)
    assert exc_info.value.code == "wrong_type"
    assert exc_info.value.path == "property_fields[0]"

  def test_source_path_must_be_list(self):
    parse, PlanParseError = self._import()
    payload = self._minimal_payload()
    payload["key_field"]["source_path"] = "not_a_list"
    with pytest.raises(PlanParseError) as exc_info:
      parse(payload)
    assert exc_info.value.code == "wrong_type"
    assert exc_info.value.path == "key_field.source_path"

  def test_empty_source_path(self):
    parse, PlanParseError = self._import()
    payload = self._minimal_payload()
    payload["key_field"]["source_path"] = []
    with pytest.raises(PlanParseError) as exc_info:
      parse(payload)
    assert exc_info.value.code == "empty_path"
    assert exc_info.value.path == "key_field.source_path"

  def test_non_string_path_segment(self):
    parse, PlanParseError = self._import()
    payload = self._minimal_payload()
    payload["key_field"]["source_path"] = ["ok", 2]
    with pytest.raises(PlanParseError) as exc_info:
      parse(payload)
    assert exc_info.value.code == "wrong_type"
    assert exc_info.value.path == "key_field.source_path[1]"

  def test_empty_path_segment(self):
    parse, PlanParseError = self._import()
    payload = self._minimal_payload()
    payload["key_field"]["source_path"] = ["ok", ""]
    with pytest.raises(PlanParseError) as exc_info:
      parse(payload)
    assert exc_info.value.code == "empty_string"
    assert exc_info.value.path == "key_field.source_path[1]"

  def test_property_field_missing_property_name(self):
    parse, PlanParseError = self._import()
    payload = self._minimal_payload()
    payload["property_fields"] = [{"source_path": ["x"]}]
    with pytest.raises(PlanParseError) as exc_info:
      parse(payload)
    assert exc_info.value.code == "missing_required_field"
    assert exc_info.value.path == "property_fields[0].property_name"


# ------------------------------------------------------------------ #
# Semantic rejections                                                 #
# ------------------------------------------------------------------ #


class TestSemanticRejections:

  def _minimal_payload(self) -> dict:
    return {
        "event_type": "x",
        "target_entity_name": "E",
        "function_name": "f",
        "key_field": {"property_name": "k", "source_path": ["k"]},
    }

  def _import(self):
    from bigquery_agent_analytics.extractor_compilation import parse_resolved_extractor_plan_json
    from bigquery_agent_analytics.extractor_compilation import PlanParseError

    return parse_resolved_extractor_plan_json, PlanParseError

  @pytest.mark.parametrize(
      "name", ["1leading", "with space", "bad-name", "../escape"]
  )
  def test_invalid_function_name(self, name):
    parse, PlanParseError = self._import()
    payload = self._minimal_payload()
    payload["function_name"] = name
    with pytest.raises(PlanParseError) as exc_info:
      parse(payload)
    assert exc_info.value.code == "invalid_identifier"
    assert exc_info.value.path == "function_name"

  @pytest.mark.parametrize("kw", ["class", "def", "for", "return"])
  def test_python_keyword_function_name(self, kw):
    parse, PlanParseError = self._import()
    payload = self._minimal_payload()
    payload["function_name"] = kw
    with pytest.raises(PlanParseError) as exc_info:
      parse(payload)
    assert exc_info.value.code == "invalid_identifier"
    assert exc_info.value.path == "function_name"

  @pytest.mark.parametrize("name", ["len", "isinstance", "ExtractedNode"])
  def test_function_name_shadowing_call_target(self, name):
    parse, PlanParseError = self._import()
    payload = self._minimal_payload()
    payload["function_name"] = name
    with pytest.raises(PlanParseError) as exc_info:
      parse(payload)
    assert exc_info.value.code == "invalid_identifier"
    assert exc_info.value.path == "function_name"

  def test_invalid_target_entity_name(self):
    parse, PlanParseError = self._import()
    payload = self._minimal_payload()
    payload["target_entity_name"] = "Foo's"
    with pytest.raises(PlanParseError) as exc_info:
      parse(payload)
    assert exc_info.value.code == "invalid_identifier"
    assert exc_info.value.path == "target_entity_name"

  def test_invalid_property_name(self):
    parse, PlanParseError = self._import()
    payload = self._minimal_payload()
    payload["property_fields"] = [
        {"property_name": "bad-name", "source_path": ["x"]}
    ]
    with pytest.raises(PlanParseError) as exc_info:
      parse(payload)
    assert exc_info.value.code == "invalid_identifier"
    assert exc_info.value.path == "property_fields[0].property_name"

  def test_duplicate_property_name_within_property_fields(self):
    parse, PlanParseError = self._import()
    payload = self._minimal_payload()
    payload["property_fields"] = [
        {"property_name": "outcome", "source_path": ["a"]},
        {"property_name": "outcome", "source_path": ["b"]},
    ]
    with pytest.raises(PlanParseError) as exc_info:
      parse(payload)
    assert exc_info.value.code == "duplicate_property_name"
    assert exc_info.value.path == "property_fields[1].property_name"

  def test_non_string_dict_keys_rejected_cleanly(self):
    """The public API accepts already-parsed dicts. A caller can
    hand a dict with non-string keys (``{1: "x", "z": "y"}``) —
    the parser used to crash with ``TypeError`` while sorting
    extra keys. It now raises a clean ``PlanParseError`` with a
    structured ``code``/``path``/``message``."""
    parse, PlanParseError = self._import()
    payload = {
        1: "bad_key",
        "event_type": "x",
        "target_entity_name": "E",
        "function_name": "f",
        "key_field": {"property_name": "k", "source_path": ["k"]},
    }
    with pytest.raises(PlanParseError) as exc_info:
      parse(payload)
    assert exc_info.value.code == "wrong_type"
    assert "JSON object keys must be strings" in exc_info.value.message

  def test_duplicate_property_name_collides_with_key(self):
    """A property_field re-using the key_field's ``property_name``
    is the same kind of duplicate — caught here so the manifest
    contract is "one ExtractedProperty per name.""" ""
    parse, PlanParseError = self._import()
    payload = self._minimal_payload()
    payload["property_fields"] = [
        {"property_name": "k", "source_path": ["alt"]}
    ]
    with pytest.raises(PlanParseError) as exc_info:
      parse(payload)
    assert exc_info.value.code == "duplicate_property_name"
    assert exc_info.value.path == "property_fields[0].property_name"
