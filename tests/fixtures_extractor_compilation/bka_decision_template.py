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

"""Hand-authored compile fixture: BKA-decision extractor source.

Source string for one ``StructuredExtractor`` callable that, when
fed through :func:`bigquery_agent_analytics.extractor_compilation.
compile_extractor`, produces a compiled bundle whose extracted
output is equivalent to
``bigquery_agent_analytics.structured_extraction.extract_bka_decision_event``
on the same telemetry events.

PR 4b.1 keeps the source hand-written so the compile harness can
be verified end-to-end without any LLM behavior in scope. PR 4b.2
will replace this hand-written string with output from the
LLM-driven template fill — but the AST allowlist, smoke-test
runner, and #76 validator gate are the same gates the LLM-emitted
source has to clear.
"""

from __future__ import annotations

# Importable string constant rather than a real module body. Pytest
# does not auto-collect this directory as a test module (the
# filename starts with neither ``test_`` nor matches any test glob),
# but a hand-authored fixture imported from a Python package keeps
# the source in version control next to the test that exercises it.
BKA_DECISION_SOURCE = '''
"""Compiled BKA-decision extractor (hand-authored fixture for PR 4b.1)."""

from __future__ import annotations

from bigquery_agent_analytics.extracted_models import ExtractedNode
from bigquery_agent_analytics.extracted_models import ExtractedProperty
from bigquery_agent_analytics.structured_extraction import (
    StructuredExtractionResult,
)


def extract_bka_decision_event_compiled(event, spec):
  """Equivalent to ``extract_bka_decision_event`` but compiled-shaped.

  Same field-pull rules: ``decision_id`` is required; ``outcome``,
  ``confidence``, ``alternatives_considered`` are optional carry-overs.
  Span handling: span fully-handled iff ``reasoning_text`` is absent;
  partially-handled otherwise.
  """
  content = event.get("content")
  if not isinstance(content, dict):
    return StructuredExtractionResult()

  decision_id = content.get("decision_id")
  if decision_id is None:
    return StructuredExtractionResult()

  session_id = event.get("session_id", "")
  span_id = event.get("span_id", "")

  node_id = f"{session_id}:mako_DecisionPoint:decision_id={decision_id}"
  properties = [ExtractedProperty(name="decision_id", value=decision_id)]
  for key in ("outcome", "confidence", "alternatives_considered"):
    if key in content:
      properties.append(ExtractedProperty(name=key, value=content[key]))

  node = ExtractedNode(
      node_id=node_id,
      entity_name="mako_DecisionPoint",
      labels=["mako_DecisionPoint"],
      properties=properties,
  )

  has_reasoning = bool(content.get("reasoning_text"))
  if has_reasoning:
    fully_handled = set()
    partially_handled = {span_id} if span_id else set()
  else:
    fully_handled = {span_id} if span_id else set()
    partially_handled = set()

  return StructuredExtractionResult(
      nodes=[node],
      edges=[],
      fully_handled_span_ids=fully_handled,
      partially_handled_span_ids=partially_handled,
  )
'''
