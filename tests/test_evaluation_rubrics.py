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

"""Tests for the canonical evaluation rubrics."""

import json
import os

from bigquery_agent_analytics.evaluation_rubrics import _BUILTIN_METRIC_CONFIG
from bigquery_agent_analytics.evaluation_rubrics import build_metrics
from bigquery_agent_analytics.evaluation_rubrics import builtin_metric_config

_EXPECTED_METRICS = [
    "response_usefulness",
    "task_grounding",
    "correctness",
    "tool_usage",
    "specificity",
    "scope_compliance",
    "first_time_right",
    "failure_attribution",
]


def test_builtin_has_the_canonical_eight_metrics():
  metrics = build_metrics()
  assert [m.name for m in metrics] == _EXPECTED_METRICS


def test_declined_category_injected_only_with_scope():
  no_scope = {m.name: m for m in build_metrics()}
  with_scope = {m.name: m for m in build_metrics(has_scope=True)}
  assert [c.name for c in no_scope["response_usefulness"].categories] == [
      "meaningful",
      "unhelpful",
      "partial",
  ]
  # insert_after places declined right after the category it credits against.
  assert [c.name for c in with_scope["response_usefulness"].categories] == [
      "meaningful",
      "declined",
      "unhelpful",
      "partial",
  ]


def test_scope_context_appends_to_scope_aware_definitions_only():
  marker = " <<SCOPE-CONTEXT>>"
  metrics = {m.name: m for m in build_metrics(scope_context=marker)}
  assert metrics["response_usefulness"].definition.endswith(marker)
  assert marker not in metrics["correctness"].definition


def test_builtin_config_is_a_deep_copy():
  cfg = builtin_metric_config()
  cfg["metrics"][0]["name"] = "mutated"
  assert _BUILTIN_METRIC_CONFIG["metrics"][0]["name"] == "response_usefulness"


def test_custom_eval_config_passthrough():
  cfg = {
      "metrics": [
          {
              "name": "custom",
              "definition": "Base.",
              "scope_aware": True,
              "categories": [
                  {"name": "yes", "definition": "Y."},
                  {"name": "no", "definition": "N."},
              ],
              "declined_category": {
                  "name": "declined",
                  "definition": "D.",
              },
          }
      ]
  }
  m = build_metrics(cfg, scope_context=" CTX", has_scope=True)[0]
  assert m.name == "custom"
  assert m.definition == "Base. CTX"
  # No insert_after: declined is appended.
  assert [c.name for c in m.categories] == ["yes", "no", "declined"]


def test_builtin_matches_the_shipped_eval_config_file():
  # Drift guard: the canonical builtin and scripts/eval/eval_config.json are
  # the same data -- an edit to either without the other fails here.
  path = os.path.join(
      os.path.dirname(__file__), "..", "scripts", "eval", "eval_config.json"
  )
  with open(path) as f:
    shipped = json.load(f)
  assert builtin_metric_config() == shipped
