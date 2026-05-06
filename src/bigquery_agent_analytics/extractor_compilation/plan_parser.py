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

"""JSON parser for :class:`ResolvedExtractorPlan`.

PR 4b.2.2.a: turns a JSON payload (a dict, or a JSON string the
caller already received from somewhere — typically the LLM step
in PR 4b.2.2.b) into a :class:`ResolvedExtractorPlan`. **No LLM
call lives here.** The parser is the deterministic boundary every
LLM-emitted plan has to cross before any source generation
happens — it owns the structural and semantic contract for the
JSON shape the LLM eventually emits.

Errors raise :class:`PlanParseError` with a stable ``code``, the
dotted ``path`` to the offending field, and a human-readable
``message``. Stable codes (callers can switch on them):

* ``invalid_json`` — the input string couldn't be parsed.
* ``wrong_root_type`` — JSON parses but isn't a top-level object.
* ``missing_required_field`` — required field absent.
* ``unknown_field`` — unrecognized field at any nesting level.
* ``wrong_type`` — field value is the wrong shape (e.g. ``str``
  where a list was expected).
* ``empty_string`` — a string field is the empty string.
* ``empty_path`` — a path field (``source_path`` /
  ``session_id_path`` / ``span_id_path`` / ``partial_when_path``)
  is empty.
* ``invalid_identifier`` — ``function_name`` /
  ``target_entity_name`` / a property_name isn't Python-
  identifier-shaped or shadows an allowlisted call target.
* ``duplicate_property_name`` — a property name appears more than
  once across ``key_field`` + ``property_fields``.
* ``invalid_plan`` — defensive: the renderer's
  :func:`_validate_plan` rejected something the parser missed.
  Should never fire in practice — present so a future-renderer
  rule that the parser hasn't learned about still produces a
  clean ``PlanParseError`` rather than escaping as ``ValueError``.

The JSON shape mirrors :class:`ResolvedExtractorPlan` 1:1::

    {
      "event_type": "bka_decision",
      "target_entity_name": "mako_DecisionPoint",
      "function_name": "extract_bka_decision_event_compiled",
      "key_field": {
        "property_name": "decision_id",
        "source_path": ["content", "decision_id"]
      },
      "property_fields": [
        {"property_name": "outcome",
         "source_path": ["content", "outcome"]}
      ],
      "session_id_path": ["session_id"],
      "span_handling": {
        "span_id_path": ["span_id"],
        "partial_when_path": ["content", "reasoning_text"]
      }
    }

Required fields: ``event_type``, ``target_entity_name``,
``function_name``, ``key_field``. Optional fields with defaults:
``property_fields`` (default ``[]``), ``session_id_path``
(default ``["session_id"]``), ``span_handling`` (default
``null``). Inside ``span_handling``: ``span_id_path`` defaults to
``["span_id"]``, ``partial_when_path`` defaults to ``null``.
"""

from __future__ import annotations

import json
import keyword
from typing import Any, Optional, Union

from .template_renderer import _ALLOWLIST_CALL_TARGETS
from .template_renderer import _validate_plan
from .template_renderer import FieldMapping
from .template_renderer import ResolvedExtractorPlan
from .template_renderer import SpanHandlingRule

# JSON Schema (Draft 2020-12) for the payload the LLM step in
# PR 4b.2.2.b will eventually emit. Exported so the LLM client
# can hand it directly to a structured-output mode (Gemini's
# ``response_schema``, OpenAI's ``json_schema`` response format,
# etc.) without re-deriving the contract.
#
# The schema captures *structural* rules: types, required fields,
# unknown-field rejection (``additionalProperties: false``), empty-
# string / empty-array rejection. Semantic rules — Python-
# identifier shape, function-name keyword/allowlist exclusion,
# duplicate property names — aren't expressible in plain JSON
# Schema and stay as parser-only checks. A payload that passes
# this schema may still fail the parser's semantic gate; a payload
# that fails this schema is guaranteed to fail the parser.
RESOLVED_EXTRACTOR_PLAN_JSON_SCHEMA: dict = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "ResolvedExtractorPlan",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "event_type",
        "target_entity_name",
        "function_name",
        "key_field",
    ],
    "properties": {
        "event_type": {"type": "string", "minLength": 1},
        "target_entity_name": {"type": "string", "minLength": 1},
        "function_name": {"type": "string", "minLength": 1},
        "key_field": {"$ref": "#/$defs/FieldMapping"},
        "property_fields": {
            "type": "array",
            "default": [],
            "items": {"$ref": "#/$defs/FieldMapping"},
        },
        "session_id_path": {
            "type": "array",
            "default": ["session_id"],
            "items": {"type": "string", "minLength": 1},
            "minItems": 1,
        },
        "span_handling": {
            "default": None,
            "oneOf": [
                {"type": "null"},
                {"$ref": "#/$defs/SpanHandlingRule"},
            ],
        },
    },
    "$defs": {
        "FieldMapping": {
            "type": "object",
            "additionalProperties": False,
            "required": ["property_name", "source_path"],
            "properties": {
                "property_name": {"type": "string", "minLength": 1},
                "source_path": {
                    "type": "array",
                    "items": {"type": "string", "minLength": 1},
                    "minItems": 1,
                },
            },
        },
        "SpanHandlingRule": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "span_id_path": {
                    "type": "array",
                    "default": ["span_id"],
                    "items": {"type": "string", "minLength": 1},
                    "minItems": 1,
                },
                "partial_when_path": {
                    "default": None,
                    "oneOf": [
                        {"type": "null"},
                        {
                            "type": "array",
                            "items": {"type": "string", "minLength": 1},
                            "minItems": 1,
                        },
                    ],
                },
            },
        },
    },
}


_TOP_LEVEL_FIELDS = frozenset(
    {
        "event_type",
        "target_entity_name",
        "function_name",
        "key_field",
        "property_fields",
        "session_id_path",
        "span_handling",
    }
)
_TOP_LEVEL_REQUIRED = frozenset(
    {"event_type", "target_entity_name", "function_name", "key_field"}
)
_FIELD_MAPPING_FIELDS = frozenset({"property_name", "source_path"})
_FIELD_MAPPING_REQUIRED = frozenset({"property_name", "source_path"})
_SPAN_HANDLING_FIELDS = frozenset({"span_id_path", "partial_when_path"})


class PlanParseError(Exception):
  """One structured failure raised by
  :func:`parse_resolved_extractor_plan_json`.

  ``code`` is a stable string identifier callers can switch on.
  ``path`` is the dotted path into the JSON payload
  (e.g. ``"key_field.source_path[1]"``); empty for failures on
  the top-level payload itself. ``message`` is human-readable.
  """

  def __init__(self, *, code: str, path: str, message: str) -> None:
    location = path or "<root>"
    super().__init__(f"[{code}] {location}: {message}")
    self.code = code
    self.path = path
    self.message = message


def parse_resolved_extractor_plan_json(
    payload: Union[str, dict],
) -> ResolvedExtractorPlan:
  """Parse a JSON payload into a :class:`ResolvedExtractorPlan`.

  *payload* may be either a JSON string (parsed via ``json.loads``)
  or an already-parsed dict (skipping the JSON parse step). Any
  shape the parser rejects raises :class:`PlanParseError` with a
  stable ``code`` and a dotted ``path``.

  The returned plan has already passed both schema-level checks
  (types / required / unknown fields / empty strings / empty
  paths) and the renderer's semantic checks (identifier shape,
  duplicate property names, allowlist-shadowing) — so callers can
  hand it straight to ``render_extractor_source`` and
  ``compile_extractor`` without re-validating.
  """
  if isinstance(payload, str):
    try:
      data = json.loads(payload)
    except json.JSONDecodeError as e:
      raise PlanParseError(
          code="invalid_json",
          path="",
          message=f"payload is not valid JSON: {e.msg} (line {e.lineno}, col {e.colno})",
      )
  elif isinstance(payload, dict):
    data = payload
  else:
    raise PlanParseError(
        code="wrong_root_type",
        path="",
        message=(
            f"payload must be a JSON string or dict, got "
            f"{type(payload).__name__}"
        ),
    )

  if not isinstance(data, dict):
    raise PlanParseError(
        code="wrong_root_type",
        path="",
        message=(
            f"top-level JSON must be an object, got {type(data).__name__}"
        ),
    )

  _check_unknown_fields(data, _TOP_LEVEL_FIELDS, prefix="")
  _check_required(data, _TOP_LEVEL_REQUIRED, prefix="")

  event_type = _expect_nonempty_str(data["event_type"], "event_type")
  target_entity_name = _expect_identifier(
      _expect_nonempty_str(data["target_entity_name"], "target_entity_name"),
      "target_entity_name",
  )
  function_name = _expect_function_name(
      _expect_nonempty_str(data["function_name"], "function_name"),
      "function_name",
  )

  key_field = _parse_field_mapping(data["key_field"], "key_field")

  property_fields_raw = data.get("property_fields", [])
  if not isinstance(property_fields_raw, list):
    raise PlanParseError(
        code="wrong_type",
        path="property_fields",
        message=(f"must be a list, got {type(property_fields_raw).__name__}"),
    )
  property_fields = tuple(
      _parse_field_mapping(item, f"property_fields[{i}]")
      for i, item in enumerate(property_fields_raw)
  )

  # Duplicate-name check across key + property_fields. The
  # renderer's _validate_plan also catches this, but doing it here
  # gives a structured ``code`` and ``path``.
  seen: set[str] = {key_field.property_name}
  for i, fm in enumerate(property_fields):
    if fm.property_name in seen:
      raise PlanParseError(
          code="duplicate_property_name",
          path=f"property_fields[{i}].property_name",
          message=(
              f"property name {fm.property_name!r} appears more than once "
              f"across key_field + property_fields; each must be unique"
          ),
      )
    seen.add(fm.property_name)

  session_id_path = _parse_string_path(
      data.get("session_id_path", ["session_id"]),
      "session_id_path",
  )

  span_handling_raw = data.get("span_handling", None)
  span_handling: Optional[SpanHandlingRule]
  if span_handling_raw is None:
    span_handling = None
  else:
    span_handling = _parse_span_handling(span_handling_raw, "span_handling")

  plan = ResolvedExtractorPlan(
      event_type=event_type,
      target_entity_name=target_entity_name,
      function_name=function_name,
      key_field=key_field,
      property_fields=property_fields,
      session_id_path=session_id_path,
      span_handling=span_handling,
  )

  # Defense-in-depth: the renderer's plan validator owns the
  # canonical semantic-rules contract. If a future rule lands
  # there that the parser doesn't yet know about, surface it as
  # a clean PlanParseError rather than escaping as ValueError.
  try:
    _validate_plan(plan)
  except ValueError as e:
    raise PlanParseError(
        code="invalid_plan",
        path="",
        message=str(e),
    )

  return plan


# ------------------------------------------------------------------ #
# Helpers                                                             #
# ------------------------------------------------------------------ #


def _check_unknown_fields(
    data: dict, allowed: frozenset, *, prefix: str
) -> None:
  # JSON spec says object keys are strings — but the parser also
  # accepts already-parsed dicts, where a caller could have non-
  # string keys. Reject those up front so ``sorted(extra)`` below
  # can't crash with a TypeError on mixed-type comparisons.
  for key in data.keys():
    if not isinstance(key, str):
      location = prefix.rstrip(".") if prefix else ""
      raise PlanParseError(
          code="wrong_type",
          path=location,
          message=(
              f"JSON object keys must be strings; got "
              f"{type(key).__name__}={key!r}"
          ),
      )
  extra = set(data.keys()) - allowed
  if extra:
    first = sorted(extra)[0]
    raise PlanParseError(
        code="unknown_field",
        path=f"{prefix}{first}" if prefix else first,
        message=(
            f"unknown field {first!r}; allowed fields here: "
            f"{sorted(allowed)}"
        ),
    )


def _check_required(data: dict, required: frozenset, *, prefix: str) -> None:
  missing = required - set(data.keys())
  if missing:
    first = sorted(missing)[0]
    raise PlanParseError(
        code="missing_required_field",
        path=f"{prefix}{first}" if prefix else first,
        message=f"required field {first!r} is missing",
    )


def _expect_nonempty_str(value: Any, path: str) -> str:
  if not isinstance(value, str):
    raise PlanParseError(
        code="wrong_type",
        path=path,
        message=f"must be a string, got {type(value).__name__}",
    )
  if not value:
    raise PlanParseError(
        code="empty_string",
        path=path,
        message="must be a non-empty string",
    )
  return value


def _expect_identifier(value: str, path: str) -> str:
  """Validate Python-identifier shape. Used for fields that get
  embedded directly into generated source as raw characters
  (``target_entity_name``, property names) — restricting them to
  identifier shape means the source is always well-formed."""
  if not value.isidentifier():
    raise PlanParseError(
        code="invalid_identifier",
        path=path,
        message=(
            f"{value!r} must be a Python-identifier-shaped string "
            f"(letters/digits/underscore, no leading digit, no spaces "
            f"or special characters)"
        ),
    )
  return value


def _expect_function_name(value: str, path: str) -> str:
  """Stricter than ``_expect_identifier``: also rejects Python
  keywords and names in the call-target allowlist (would shadow a
  builtin in the generated module)."""
  if not value.isidentifier() or keyword.iskeyword(value):
    raise PlanParseError(
        code="invalid_identifier",
        path=path,
        message=(
            f"{value!r} must be a plain Python identifier (not a "
            f"reserved keyword)"
        ),
    )
  if value in _ALLOWLIST_CALL_TARGETS:
    raise PlanParseError(
        code="invalid_identifier",
        path=path,
        message=(
            f"function_name {value!r} would shadow an allowlisted call "
            f"target in the generated module"
        ),
    )
  return value


def _parse_string_path(value: Any, path: str) -> tuple[str, ...]:
  if not isinstance(value, list):
    raise PlanParseError(
        code="wrong_type",
        path=path,
        message=(f"must be a list of strings, got {type(value).__name__}"),
    )
  if not value:
    raise PlanParseError(
        code="empty_path",
        path=path,
        message="must contain at least one segment",
    )
  out: list[str] = []
  for i, segment in enumerate(value):
    if not isinstance(segment, str):
      raise PlanParseError(
          code="wrong_type",
          path=f"{path}[{i}]",
          message=(
              f"path segment must be a string, got " f"{type(segment).__name__}"
          ),
      )
    if not segment:
      raise PlanParseError(
          code="empty_string",
          path=f"{path}[{i}]",
          message="path segments must be non-empty strings",
      )
    out.append(segment)
  return tuple(out)


def _parse_field_mapping(value: Any, path: str) -> FieldMapping:
  if not isinstance(value, dict):
    raise PlanParseError(
        code="wrong_type",
        path=path,
        message=(
            f"must be a JSON object with property_name + source_path; "
            f"got {type(value).__name__}"
        ),
    )
  _check_unknown_fields(value, _FIELD_MAPPING_FIELDS, prefix=f"{path}.")
  _check_required(value, _FIELD_MAPPING_REQUIRED, prefix=f"{path}.")

  property_name = _expect_identifier(
      _expect_nonempty_str(value["property_name"], f"{path}.property_name"),
      f"{path}.property_name",
  )
  source_path = _parse_string_path(value["source_path"], f"{path}.source_path")
  return FieldMapping(property_name=property_name, source_path=source_path)


def _parse_span_handling(value: Any, path: str) -> SpanHandlingRule:
  if not isinstance(value, dict):
    raise PlanParseError(
        code="wrong_type",
        path=path,
        message=(f"must be a JSON object or null; got {type(value).__name__}"),
    )
  _check_unknown_fields(value, _SPAN_HANDLING_FIELDS, prefix=f"{path}.")

  span_id_path = _parse_string_path(
      value.get("span_id_path", ["span_id"]), f"{path}.span_id_path"
  )

  partial_raw = value.get("partial_when_path", None)
  partial_when_path: Optional[tuple[str, ...]]
  if partial_raw is None:
    partial_when_path = None
  else:
    partial_when_path = _parse_string_path(
        partial_raw, f"{path}.partial_when_path"
    )

  return SpanHandlingRule(
      span_id_path=span_id_path,
      partial_when_path=partial_when_path,
  )
