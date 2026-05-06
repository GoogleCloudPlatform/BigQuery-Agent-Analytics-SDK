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

"""LLM-driven resolution of raw extraction rules into
:class:`ResolvedExtractorPlan` instances.

PR 4b.2.2.b: wraps an injectable LLM client to do the *mapping*
step — given an extraction rule (the user's intent: "extract
entity X from this event_type") and an event schema (what fields
exist in the payload), produce a ``ResolvedExtractorPlan`` ready
for 4b.2.1's renderer.

This module is **deterministic except for the LLM call itself**:

* :func:`build_resolution_prompt` is a pure function — same
  inputs → byte-identical output.
* :class:`PlanResolver` does no retry, no fallback, no provider-
  specific glue. Anything the LLM client raises propagates
  unchanged. ``PlanParseError`` from the response parser also
  propagates unchanged.

Concrete provider adapters (``google-genai``, OpenAI, etc.) and
retry-on-gate-failure orchestration land in PR 4b.2.2.c / PR 4c.
This PR ships only the prompt + protocol + resolver glue, tested
end-to-end with fake clients producing pre-canned JSON.
"""

from __future__ import annotations

import json
from typing import Any, Protocol

from .plan_parser import parse_resolved_extractor_plan_json
from .plan_parser import RESOLVED_EXTRACTOR_PLAN_JSON_SCHEMA
from .template_renderer import _ALLOWLIST_CALL_TARGETS
from .template_renderer import ResolvedExtractorPlan


class LLMClient(Protocol):
  """Minimal structural-typing protocol for the LLM call.

  The plan resolver only needs one method: produce a JSON-shaped
  Python ``dict`` from a text prompt + a JSON schema describing
  the response. Any concrete LLM client (``google-genai``'s
  Gemini wrapper, OpenAI's chat-completions with ``json_schema``,
  a thin in-house wrapper, a test fake) that implements this
  method works.

  The resolver passes ``RESOLVED_EXTRACTOR_PLAN_JSON_SCHEMA`` as
  the ``schema`` argument so providers that support structured
  output (Gemini's ``response_schema``, OpenAI's
  ``response_format``) can constrain the LLM at generation time.
  Providers without that support can ignore ``schema``; the
  parser's structural gate catches malformed responses regardless.
  """

  def generate_json(self, prompt: str, schema: dict) -> dict:
    ...  # pragma: no cover — Protocol stub


def build_resolution_prompt(
    extraction_rule: Any,
    event_schema: Any,
) -> str:
  """Build the deterministic prompt for the resolver step.

  The prompt instructs the LLM to:
    * map only fields that exist in *event_schema* (no
      hallucinated paths);
    * use Python-identifier-shaped names for entity / property /
      function;
    * avoid function names that would shadow the call-target
      allowlist;
    * omit uncertain optional fields rather than invent them;
    * emit JSON conforming to
      :data:`RESOLVED_EXTRACTOR_PLAN_JSON_SCHEMA`.

  Output is byte-stable for the same inputs (``sort_keys=True``
  on every JSON serialization), so plan resolution itself doesn't
  add nondeterminism beyond whatever the LLM contributes.
  """
  rule_json = json.dumps(extraction_rule, sort_keys=True, indent=2)
  schema_json = json.dumps(event_schema, sort_keys=True, indent=2)
  output_contract = json.dumps(
      RESOLVED_EXTRACTOR_PLAN_JSON_SCHEMA, sort_keys=True, indent=2
  )
  forbidden_function_names = ", ".join(sorted(_ALLOWLIST_CALL_TARGETS))

  return f"""\
You are compiling a deterministic structured extractor for one
event type. Your job is to map the event_type's payload fields
into a ResolvedExtractorPlan that downstream code will compile
into a Python extractor function.

# Output contract

Emit one JSON object that conforms to this JSON Schema:

{output_contract}

Constraints not captured by the schema (the parser will reject
violations):

- ``function_name``, ``target_entity_name``, and every
  ``property_name`` must be a plain Python identifier (letters,
  digits, underscores; no leading digit; no Python keyword).
- ``function_name`` must NOT shadow any of the call-target
  allowlist names: {forbidden_function_names}.
- Every ``property_name`` must be unique across ``key_field`` +
  ``property_fields``.

# Mapping rules

- Use ONLY paths that exist in the event_schema below. Do NOT
  invent paths. If the event_schema doesn't contain a field for
  an optional property, OMIT that property from
  ``property_fields`` rather than guess.
- ``key_field`` is required. The path you choose must resolve
  to the event payload's stable identifier — extracted nodes
  use this as their primary key.
- ``session_id_path`` defaults to ``["session_id"]``. Only
  override it when the event_schema names the session id under
  a different path.
- ``span_handling`` is optional. Set ``partial_when_path`` only
  when the event_schema contains a free-text field that the AI
  extractor would still need to interpret (e.g. a
  ``reasoning_text`` field). Otherwise omit it or set it to
  ``null``.

# Inputs

## extraction_rule (the user's intent)

{rule_json}

## event_schema (what fields actually exist in the event payload)

{schema_json}

Now emit the JSON object.
"""


class PlanResolver:
  """Wraps an :class:`LLMClient` to resolve raw extraction-rule +
  event-schema pairs into :class:`ResolvedExtractorPlan` instances.

  The class itself is deterministic — only the wrapped LLM call
  introduces nondeterminism. PR 4b.2.2.c will add
  retry-on-gate-failure orchestration that builds on this; this
  PR keeps the resolver as a thin, single-shot wire-up of
  prompt + LLM + parser.
  """

  def __init__(self, llm_client: LLMClient) -> None:
    self._llm_client = llm_client

  def resolve(
      self,
      extraction_rule: Any,
      event_schema: Any,
  ) -> ResolvedExtractorPlan:
    """Build the prompt, call the LLM, parse the response.

    Raises:
      PlanParseError: if the LLM's response doesn't pass the
        parser's structural or semantic gates. The parser's
        ``code``/``path``/``message`` are surfaced unchanged so
        callers can route on them — and PR 4b.2.2.c can feed
        them back into a retry prompt.
      Exception: anything the LLM client raises is propagated
        unchanged. The resolver never swallows transport
        errors, quota errors, or auth errors.
    """
    prompt = build_resolution_prompt(extraction_rule, event_schema)
    response = self._llm_client.generate_json(
        prompt, RESOLVED_EXTRACTOR_PLAN_JSON_SCHEMA
    )
    return parse_resolved_extractor_plan_json(response)
