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

"""Deterministic source generator for compiled structured extractors.

PR 4b.2.1: turns a pre-resolved :class:`ResolvedExtractorPlan` into
a Python source string that 4b.1's
:func:`bigquery_agent_analytics.extractor_compilation.compile_extractor`
can run through every gate (AST allowlist, smoke runner, #76
validator). **No LLM call lives here.** PR 4b.2.2 owns the LLM
step that *resolves* a raw extraction-rule + event-schema pair
into a :class:`ResolvedExtractorPlan`; this module is the
deterministic boundary the LLM output has to cross before any
source generation happens.

Design constraints:

* Output passes 4b.1's :func:`validate_source` allowlist —
  imports only the three symbols actually used (``ExtractedNode``,
  ``ExtractedProperty``, ``StructuredExtractionResult``); calls
  only allowlisted Names and method names; no shadowing of
  allowlisted call targets, no halt/escape constructs, no
  decorators, no non-constant defaults. ``ExtractedEdge`` isn't
  imported since the renderer doesn't emit edges yet — adding
  edge support is a future plan-shape extension.
* Output, when run on sample events, emits well-formed
  :class:`StructuredExtractionResult` instances (lists for
  ``nodes`` / ``edges``, sets for the span-id fields) so the
  smoke runner's well-formed-result check accepts them.
* Output is **deterministic**: identical plans produce
  byte-identical source. Useful so the compile fingerprint stays
  stable across consecutive renders.

The plan model is intentionally narrow. Every field-level
decision is already resolved before the renderer runs — there is
no "look at the event_schema to figure out the path." That keeps
the renderer's contract small and makes the LLM step in 4b.2.2 a
pure mapping problem (raw rule + schema → ResolvedExtractorPlan).
"""

from __future__ import annotations

import dataclasses
import keyword
from typing import Optional

# Names that ``compile_extractor`` and the AST allowlist agree
# can be safely imported / called inside generated source. Keeping
# this list aligned with ``_ALLOWED_CALL_TARGETS`` in
# ``ast_validator.py`` is what makes generated source pass
# validation at all.
_GENERATED_HEADER = (
    '"""Compiled structured extractor (rendered by '
    "bigquery_agent_analytics.extractor_compilation.template_renderer)."
    '"""\n'
    "\n"
    "from __future__ import annotations\n"
    "\n"
    "from bigquery_agent_analytics.extracted_models import ExtractedNode\n"
    "from bigquery_agent_analytics.extracted_models import ExtractedProperty\n"
    "from bigquery_agent_analytics.structured_extraction import (\n"
    "    StructuredExtractionResult,\n"
    ")\n"
)

# 4b.1's call-target allowlist. Imported names must not shadow
# any of these; the renderer's identifier validator enforces the
# rule for ``function_name``. Mirrors ``_ALLOWED_CALL_TARGETS``
# in ``ast_validator.py``.
_ALLOWLIST_CALL_TARGETS = frozenset(
    {
        "ExtractedNode",
        "ExtractedEdge",
        "ExtractedProperty",
        "StructuredExtractionResult",
        "str",
        "int",
        "float",
        "bool",
        "bytes",
        "set",
        "frozenset",
        "dict",
        "list",
        "tuple",
        "isinstance",
        "len",
    }
)


@dataclasses.dataclass(frozen=True)
class FieldMapping:
  """Maps an ontology property name to a path in the event payload.

  ``source_path`` is a sequence of dict keys: ``("content",
  "decision_id")`` means ``event["content"]["decision_id"]``.
  Each intermediate value must be a ``dict`` for the lookup to
  succeed; the renderer emits ``isinstance`` guards at every
  level so missing or wrong-shape intermediates produce ``None``
  (which the caller treats as "field absent") rather than raising.
  """

  property_name: str
  source_path: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class SpanHandlingRule:
  """How to populate the result's ``fully_handled_span_ids`` and
  ``partially_handled_span_ids`` sets.

  ``span_id_path`` is where to find the span id in the event
  (default ``("span_id",)``).

  ``partial_when_path`` is optional. When set, the span is marked
  *partially* handled when the value at that path is truthy —
  matches the BKA pattern of "free-text reasoning still needs the
  AI extractor." When unset, the span is always *fully* handled
  whenever it's present.
  """

  span_id_path: tuple[str, ...] = ("span_id",)
  partial_when_path: Optional[tuple[str, ...]] = None


@dataclasses.dataclass(frozen=True)
class ResolvedExtractorPlan:
  """A pre-resolved plan for one compiled extractor.

  The plan is deliberately boring — every field-level decision is
  already made by the caller. PR 4b.2.2 will populate this from a
  raw extraction rule + event schema via an LLM step; PR 4b.2.1
  exists so that source-generation can be reviewed independently
  of any probabilistic mapping.
  """

  event_type: str
  target_entity_name: str
  function_name: str
  key_field: FieldMapping
  property_fields: tuple[FieldMapping, ...] = ()
  session_id_path: tuple[str, ...] = ("session_id",)
  span_handling: Optional[SpanHandlingRule] = None


def render_extractor_source(plan: ResolvedExtractorPlan) -> str:
  """Render a Python source string for one compiled extractor.

  The returned source is a complete module that defines a single
  ``StructuredExtractor`` callable named ``plan.function_name``.
  When fed to ``compile_extractor(source=..., ...)`` it must
  clear every gate; the tests in
  ``tests/test_extractor_compilation_template.py`` lock that
  behavior in.

  Raises:
    ValueError: if any plan field violates the renderer's
      contract (empty strings, duplicate property names, invalid
      function name, etc.). Failures are caught here rather than
      delegated to ``compile_extractor`` so callers see a clear
      plan-level error.
  """
  _validate_plan(plan)

  body_lines: list[str] = []
  body_lines.append(
      f'  """Generated extractor for event_type='
      f'{plan.event_type!r} (entity={plan.target_entity_name!r})."""'
  )
  # Top-of-function event_type guard: the orchestrator (C2) routes
  # events by ``event.get("event_type")`` against the manifest's
  # declared types, but a plan/manifest mismatch could silently
  # attach an extractor to the wrong event type. Layering the
  # check inside the generated body means the extractor itself
  # enforces its declared coverage — even if invoked directly on
  # a stray event.
  body_lines.append(f"  if event.get('event_type') != {plan.event_type!r}:")
  body_lines.append("    return StructuredExtractionResult()")
  body_lines.extend(_render_key_traversal(plan))
  body_lines.append("")
  body_lines.extend(_render_session_and_span_id(plan))
  body_lines.append("")
  body_lines.extend(_render_node_id_and_properties(plan))
  body_lines.append("")
  body_lines.extend(_render_node_construction(plan))
  body_lines.append("")
  body_lines.extend(_render_span_handling(plan))
  body_lines.append("")
  body_lines.append("  return StructuredExtractionResult(")
  body_lines.append("      nodes=[node],")
  body_lines.append("      edges=[],")
  body_lines.append("      fully_handled_span_ids=fully_handled,")
  body_lines.append("      partially_handled_span_ids=partially_handled,")
  body_lines.append("  )")

  source = (
      _GENERATED_HEADER
      + "\n"
      + f"def {plan.function_name}(event, spec):\n"
      + "\n".join(body_lines)
      + "\n"
  )
  return source


def _validate_plan(plan: ResolvedExtractorPlan) -> None:
  """Reject malformed plans before any source is generated.

  Type hints on the dataclasses don't enforce types at runtime —
  ``ResolvedExtractorPlan(event_type=1)`` is accepted by Python
  even though the renderer expects a string. The validator
  catches every shape that would produce broken Python source or
  an opaque downstream failure (Pydantic ``ExtractedNode``
  construction, AST-gate rejection, etc.) and raises
  ``ValueError`` at the renderer boundary so callers see one
  consistent error shape.
  """
  if not _is_python_identifier(plan.function_name):
    raise ValueError(
        f"function_name={plan.function_name!r} must be a plain Python "
        f"identifier (letters/digits/underscore, not starting with a "
        f"digit, not a Python keyword)"
    )
  if plan.function_name in _ALLOWLIST_CALL_TARGETS:
    raise ValueError(
        f"function_name={plan.function_name!r} would shadow an "
        f"allowlisted call target; pick a different name"
    )
  _require_nonempty_str(plan.event_type, "event_type")
  _require_safe_identifier(plan.target_entity_name, "target_entity_name")
  if not plan.key_field.source_path:
    raise ValueError("key_field.source_path must be non-empty")
  _require_safe_identifier(
      plan.key_field.property_name, "key_field.property_name"
  )
  _require_string_path(plan.key_field.source_path, "key_field.source_path")
  seen_properties: set[str] = {plan.key_field.property_name}
  for index, fm in enumerate(plan.property_fields):
    field_label = f"property_fields[{index}]"
    _require_safe_identifier(fm.property_name, f"{field_label}.property_name")
    if not fm.source_path:
      raise ValueError(f"{field_label}.source_path must be non-empty")
    _require_string_path(fm.source_path, f"{field_label}.source_path")
    if fm.property_name in seen_properties:
      raise ValueError(
          f"duplicate property_name {fm.property_name!r} in plan; the key "
          f"field plus property_fields must be a set of distinct names"
      )
    seen_properties.add(fm.property_name)
  if not plan.session_id_path:
    raise ValueError("session_id_path must be non-empty")
  _require_string_path(plan.session_id_path, "session_id_path")
  if plan.span_handling is not None:
    if not plan.span_handling.span_id_path:
      raise ValueError("span_handling.span_id_path must be non-empty")
    _require_string_path(
        plan.span_handling.span_id_path, "span_handling.span_id_path"
    )
    if plan.span_handling.partial_when_path is not None:
      if not plan.span_handling.partial_when_path:
        raise ValueError(
            "span_handling.partial_when_path must be non-empty when set"
        )
      _require_string_path(
          plan.span_handling.partial_when_path,
          "span_handling.partial_when_path",
      )


def _require_nonempty_str(value, label: str) -> None:
  if not isinstance(value, str):
    raise ValueError(
        f"{label} must be a string; got {type(value).__name__}={value!r}"
    )
  if not value:
    raise ValueError(f"{label} must be a non-empty string")


def _require_safe_identifier(value, label: str) -> None:
  """Validate that *value* is a non-empty string AND a Python
  identifier-shape (letters/digits/underscore, no leading digit).
  Used for fields that get embedded directly into generated
  Python source as raw characters (e.g., ``target_entity_name``
  in the ``node_id`` f-string) — restricting them to identifier
  shape means the source is well-formed regardless of input.
  """
  _require_nonempty_str(value, label)
  if not value.isidentifier():
    raise ValueError(
        f"{label}={value!r} must be a Python-identifier-shaped string "
        f"(letters/digits/underscore, no leading digit, no spaces or "
        f"special characters); the renderer embeds it directly into "
        f"generated Python source"
    )


def _require_string_path(value, label: str) -> None:
  """Each path segment must be a non-empty string. Path segments
  go through ``repr()`` when emitted into ``.get(...)`` calls, so
  identifier shape isn't required — but mixed-type paths
  (``("a", 1)``) or empty segments would render confusing source.
  """
  for i, segment in enumerate(value):
    _require_nonempty_str(segment, f"{label}[{i}]")


def _is_python_identifier(value: str) -> bool:
  return (
      isinstance(value, str)
      and value.isidentifier()
      and not keyword.iskeyword(value)
  )


def _render_key_traversal(plan: ResolvedExtractorPlan) -> list[str]:
  """Emit the traversal that pulls the key value from
  ``plan.key_field.source_path``. Returns an empty result if any
  intermediate is not a dict, or if the leaf is ``None`` — the
  same "extractor declines this event" contract the BKA fixture
  uses."""
  lines: list[str] = []
  path = plan.key_field.source_path
  if len(path) == 1:
    lines.append(f"  key_value = event.get({path[0]!r})")
  else:
    last_dict = "event"
    for i, step in enumerate(path[:-1]):
      var = f"_p{i}"
      lines.append(f"  {var} = {last_dict}.get({step!r})")
      lines.append(f"  if not isinstance({var}, dict):")
      lines.append("    return StructuredExtractionResult()")
      last_dict = var
    lines.append(f"  key_value = {last_dict}.get({path[-1]!r})")
  lines.append("  if key_value is None:")
  lines.append("    return StructuredExtractionResult()")
  return lines


def _render_session_and_span_id(plan: ResolvedExtractorPlan) -> list[str]:
  """``session_id`` and (optionally) ``span_id`` come from the
  event root via simple ``.get(..., '')`` calls — missing values
  fall back to ``''`` so the generated node_id and span sets are
  always well-shaped."""
  lines: list[str] = []
  lines.extend(
      _render_optional_path_get(
          plan.session_id_path,
          target_var="session_id",
          var_prefix="_sid",
          default_repr="''",
      )
  )
  if plan.span_handling is not None:
    lines.extend(
        _render_optional_path_get(
            plan.span_handling.span_id_path,
            target_var="span_id",
            var_prefix="_spn",
            default_repr="''",
        )
    )
  else:
    lines.append("  span_id = ''")
  return lines


def _render_optional_path_get(
    path: tuple[str, ...],
    *,
    target_var: str,
    var_prefix: str,
    default_repr: str,
) -> list[str]:
  """Emit ``target_var = event[path]`` style traversal that falls
  back to ``default_repr`` when any intermediate isn't a dict or
  the leaf is missing.

  Each ``.get(...)`` is emitted *inside* the previous step's
  ``isinstance(..., dict)`` guard — so a string / list / None
  intermediate can never crash the next ``.get(...)`` call. The
  previous flat pattern checked all isinstances *after* the
  ``.get()`` chain had already run, which was the P1.1 bug.

  Pattern (for path ``("a", "b", "c")``)::

      target = default          # safe baseline
      v0 = event.get('a')       # fails fast for missing keys
      if isinstance(v0, dict):
        v1 = v0.get('b')
        if isinstance(v1, dict):
          target = v1.get('c', default)
  """
  if len(path) == 1:
    return [f"  {target_var} = event.get({path[0]!r}, {default_repr})"]

  lines: list[str] = [f"  {target_var} = {default_repr}"]
  last_dict = "event"
  indent = "  "
  for depth in range(len(path) - 1):
    var = f"{var_prefix}_{depth}"
    lines.append(f"{indent}{var} = {last_dict}.get({path[depth]!r})")
    lines.append(f"{indent}if isinstance({var}, dict):")
    last_dict = var
    indent = indent + "  "
  lines.append(
      f"{indent}{target_var} = {last_dict}.get({path[-1]!r}, {default_repr})"
  )
  return lines


def _render_node_id_and_properties(
    plan: ResolvedExtractorPlan,
) -> list[str]:
  """Emit ``node_id`` and ``properties`` list construction. Each
  optional property field is added under an ``in``-check guard so
  missing fields stay absent from the materialized row."""
  lines: list[str] = []
  key_property = plan.key_field.property_name
  lines.append(
      f"  node_id = "
      f"f'{{session_id}}:{plan.target_entity_name}:"
      f"{key_property}={{key_value}}'"
  )
  lines.append(
      f"  properties = [ExtractedProperty(name={key_property!r}, value=key_value)]"
  )
  for index, fm in enumerate(plan.property_fields):
    lines.extend(_render_optional_property(fm, index))
  return lines


def _render_optional_property(fm: FieldMapping, index: int) -> list[str]:
  """Emit the conditional append for one optional property field.

  Each ``.get(...)`` lives inside the previous step's
  ``isinstance(..., dict)`` guard so a non-dict intermediate
  doesn't raise. *index* is the property's position in
  ``plan.property_fields``; helper variable names derive from the
  index (``_op0_0``, ``_op0_1``, ...) instead of from
  ``fm.property_name`` so a property name that isn't a Python
  identifier can't pollute the generated source.

  Pattern (for path ``("content", "outcome")``)::

      _op0_0 = event.get('content')
      if isinstance(_op0_0, dict):
        if 'outcome' in _op0_0:
          properties.append(ExtractedProperty(
              name='outcome', value=_op0_0['outcome']))
  """
  path = fm.source_path
  if len(path) == 1:
    leaf_key = path[0]
    return [
        f"  if {leaf_key!r} in event:",
        f"    properties.append(ExtractedProperty("
        f"name={fm.property_name!r}, value=event[{leaf_key!r}]))",
    ]

  lines: list[str] = []
  last_dict = "event"
  indent = "  "
  var_prefix = f"_op{index}"
  for depth in range(len(path) - 1):
    var = f"{var_prefix}_{depth}"
    lines.append(f"{indent}{var} = {last_dict}.get({path[depth]!r})")
    lines.append(f"{indent}if isinstance({var}, dict):")
    last_dict = var
    indent = indent + "  "
  leaf_key = path[-1]
  lines.append(f"{indent}if {leaf_key!r} in {last_dict}:")
  lines.append(
      f"{indent}  properties.append(ExtractedProperty("
      f"name={fm.property_name!r}, value={last_dict}[{leaf_key!r}]))"
  )
  return lines


def _render_node_construction(plan: ResolvedExtractorPlan) -> list[str]:
  return [
      "  node = ExtractedNode(",
      f"      node_id=node_id,",
      f"      entity_name={plan.target_entity_name!r},",
      f"      labels=[{plan.target_entity_name!r}],",
      "      properties=properties,",
      "  )",
  ]


def _render_span_handling(plan: ResolvedExtractorPlan) -> list[str]:
  """Emit ``fully_handled`` / ``partially_handled`` set
  construction. When ``span_handling`` is ``None`` the span sets
  are always empty (the extractor doesn't claim any span
  handling); otherwise the partial-when path determines partial vs
  full."""
  if plan.span_handling is None:
    return [
        "  fully_handled = set()",
        "  partially_handled = set()",
    ]
  rule = plan.span_handling
  if rule.partial_when_path is None:
    return [
        "  fully_handled = {span_id} if span_id else set()",
        "  partially_handled = set()",
    ]
  # Partial-when path: pull the value via the same safe-traversal
  # helper used for ``span_id`` itself, then treat as partial iff
  # truthy. The helper returns ``None`` when any intermediate
  # isn't a dict, so we don't need a separate raise/missing path.
  lines: list[str] = list(
      _render_optional_path_get(
          rule.partial_when_path,
          target_var="_partial_v",
          var_prefix="_pw",
          default_repr="None",
      )
  )
  lines.append("  if _partial_v:")
  lines.append("    fully_handled = set()")
  lines.append("    partially_handled = {span_id} if span_id else set()")
  lines.append("  else:")
  lines.append("    fully_handled = {span_id} if span_id else set()")
  lines.append("    partially_handled = set()")
  return lines
