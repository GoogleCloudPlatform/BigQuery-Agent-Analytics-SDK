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

"""Canonical BKA-decision inputs for the compile-and-measure path
(PR 4c of issue #75).

Importable from:

* the deterministic measurement test
  (``tests/test_extractor_compilation_measurement.py``);
* the gated live BigQuery + LLM test
  (``tests/test_extractor_compilation_bka_compile_live.py``);
* future demo / docs scripts.

Keeping the inputs in one place ensures the measurement artifact
under version control is reproducible — change either constant
here and *both* test paths exercise the change.

The constants mirror the shape of
:func:`bigquery_agent_analytics.structured_extraction.extract_bka_decision_event`'s
field-pull rules: ``decision_id`` is the key field, the optional
``outcome`` / ``confidence`` / ``alternatives_considered`` carry
over as properties, and ``reasoning_text`` is the
``partial_when_path`` for span handling.
"""

from __future__ import annotations

# ------------------------------------------------------------------ #
# Inputs to the resolver step                                         #
# ------------------------------------------------------------------ #


BKA_EXTRACTION_RULE: dict = {
    "event_type": "bka_decision",
    "target_entity_name": "mako_DecisionPoint",
    "key_field": "decision_id",
    "key_field_path": ["content", "decision_id"],
    "carry_over_properties": [
        {"name": "outcome", "source_path": ["content", "outcome"]},
        {"name": "confidence", "source_path": ["content", "confidence"]},
        {
            "name": "alternatives_considered",
            "source_path": ["content", "alternatives_considered"],
        },
    ],
    "span_handling": {
        "span_id_path": ["span_id"],
        "partial_when_path": ["content", "reasoning_text"],
    },
    "session_id_path": ["session_id"],
}
"""User-intent payload the resolver consumes. Plain dict (JSON-
serializable) so it works with the resolver's prompt builder
without any normalization step."""


BKA_EVENT_SCHEMA: dict = {
    "event_type": "string",
    "session_id": "string",
    "span_id": "string",
    "content": {
        "decision_id": "string",
        "outcome": "string",
        "confidence": "double",
        "alternatives_considered": "array<string>",
        "reasoning_text": "string",
    },
}
"""Typed structure of a ``bka_decision`` event payload. The
resolver uses this to constrain the LLM to only paths that
actually exist in the event."""


# ------------------------------------------------------------------ #
# Sample events                                                       #
# ------------------------------------------------------------------ #


BKA_SAMPLE_EVENTS: list[dict] = [
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
"""Two events covering both span-handling branches: ``span1`` has
``reasoning_text`` so the span is *partially* handled; ``span2``
omits it so the span is *fully* handled."""


# ------------------------------------------------------------------ #
# Resolved plan dict                                                  #
# ------------------------------------------------------------------ #
#
# What a correct LLM step *should* emit for the inputs above. The
# deterministic test client returns this verbatim so the test
# isolates the compile-and-measure logic from any prompt-quality
# concern.


BKA_RESOLVED_PLAN_DICT: dict = {
    "event_type": "bka_decision",
    "target_entity_name": "mako_DecisionPoint",
    "function_name": "extract_bka_decision_event_compiled",
    "key_field": {
        "property_name": "decision_id",
        "source_path": ["content", "decision_id"],
    },
    "property_fields": [
        {"property_name": "outcome", "source_path": ["content", "outcome"]},
        {
            "property_name": "confidence",
            "source_path": ["content", "confidence"],
        },
        {
            "property_name": "alternatives_considered",
            "source_path": ["content", "alternatives_considered"],
        },
    ],
    "session_id_path": ["session_id"],
    "span_handling": {
        "span_id_path": ["span_id"],
        "partial_when_path": ["content", "reasoning_text"],
    },
}
"""Pre-resolved plan in the shape the parser accepts. Used by the
deterministic test client and as a fallback in any test that
needs a known-good plan without invoking the resolver step."""
