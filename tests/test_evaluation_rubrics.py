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

import importlib.util
import json
import os
from pathlib import Path
import warnings

import pytest

from bigquery_agent_analytics import evaluation_rubrics
import bigquery_agent_analytics as bqaa
from bigquery_agent_analytics.categorical_evaluator import build_categorical_prompt
from bigquery_agent_analytics.categorical_evaluator import CategoricalEvaluationConfig
from bigquery_agent_analytics.categorical_evaluator import CategoricalMetricDefinition
from bigquery_agent_analytics.evaluation_rubrics import _BUILTIN_METRIC_CONFIG
from bigquery_agent_analytics.evaluation_rubrics import build_metrics
from bigquery_agent_analytics.evaluation_rubrics import builtin_metric_config
from bigquery_agent_analytics.evaluation_rubrics import policy_compliance_metric
from bigquery_agent_analytics.evaluation_rubrics import response_usefulness_metric
from bigquery_agent_analytics.evaluation_rubrics import task_grounding_metric
from bigquery_agent_analytics.evaluation_rubrics import three_pillar_scorecard_metrics

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


@pytest.fixture(scope="module")
def quality_report_module():
  # Load the real script by its file path; scripts/ need not be a package
  # or depend on pytest adding the repository root to sys.path.
  path = Path(__file__).resolve().parents[1] / "scripts" / "quality_report.py"
  spec = importlib.util.spec_from_file_location("rubric_quality_report", path)
  module = importlib.util.module_from_spec(spec)
  with warnings.catch_warnings():
    spec.loader.exec_module(module)
  return module


@pytest.mark.parametrize(
    "scope_context,has_scope",
    [("", False), (" Scope: HR only.", False), (" Scope: HR only.", True)],
)
def test_primary_factories_reuse_full_canonical_definitions(
    scope_context, has_scope
):
  expected = {
      metric.name: metric
      for metric in build_metrics(
          scope_context=scope_context, has_scope=has_scope
      )
  }
  for factory in (response_usefulness_metric, task_grounding_metric):
    metric = factory(scope_context=scope_context, has_scope=has_scope)
    assert metric == expected[metric.name]


@pytest.mark.parametrize(
    "eval_spec",
    [None, {"scope": "HR only."}, {"ground_truth": "PTO is 20 days."}],
)
def test_factories_and_quality_report_share_scope_and_vocabulary(
    quality_report_module, eval_spec
):
  metrics = quality_report_module.get_eval_metrics(eval_spec=eval_spec)
  assert [metric.name for metric in metrics] == _EXPECTED_METRICS
  scope_context = quality_report_module._build_scope_context(eval_spec)
  has_scope = bool(eval_spec and eval_spec.get("scope"))
  assert (
      response_usefulness_metric(
          scope_context=scope_context, has_scope=has_scope
      )
      == metrics[0]
  )
  assert (
      task_grounding_metric(scope_context=scope_context, has_scope=has_scope)
      == metrics[1]
  )


def test_quality_report_keeps_custom_config_interpretation(
    quality_report_module,
):
  config = builtin_metric_config()
  config["metrics"] = [config["metrics"][2]]
  config["metrics"][0]["definition"] = "Custom correctness instruction."
  assert quality_report_module.get_eval_metrics(
      eval_config=config
  ) == build_metrics(config)


@pytest.mark.parametrize(
    "factory",
    [
        response_usefulness_metric,
        task_grounding_metric,
        policy_compliance_metric,
    ],
)
def test_factories_return_valid_independent_metric_objects(factory):
  first = factory()
  second = factory()
  assert isinstance(first, CategoricalMetricDefinition)
  assert first.definition
  assert first.required is True
  assert first == second
  assert first is not second
  assert first.categories[0] is not second.categories[0]
  first.definition = "mutated definition"
  first.categories[0].definition = "mutated category"
  assert factory() == second


def test_bundle_order_scope_and_mutable_objects():
  kwargs = {"scope_context": " Scope: HR only.", "has_scope": True}
  metrics = three_pillar_scorecard_metrics(**kwargs)
  assert [metric.name for metric in metrics] == [
      "response_usefulness",
      "task_grounding",
      "policy_compliance",
  ]
  assert metrics == [
      response_usefulness_metric(**kwargs),
      task_grounding_metric(**kwargs),
      policy_compliance_metric(),
  ]
  assert [category.name for category in metrics[0].categories] == [
      "meaningful",
      "declined",
      "unhelpful",
      "partial",
  ]
  metrics[0].categories[0].name = "mutated"
  metrics.pop()
  assert len(three_pillar_scorecard_metrics(**kwargs)) == 3
  assert (
      three_pillar_scorecard_metrics(**kwargs)[0].categories[0].name
      == "meaningful"
  )
  assert [metric.name for metric in build_metrics()] == _EXPECTED_METRICS


def test_policy_checklist_reaches_the_judge_prompt():
  metric = policy_compliance_metric()
  assert [category.name for category in metric.categories] == [
      "compliant",
      "violation",
  ]
  prompt = build_categorical_prompt(
      CategoricalEvaluationConfig(metrics=[metric])
  )
  for phrase in (
      "Personally identifiable information",
      "government identifiers",
      "national identity",
      "financial account numbers",
      "IBANs",
      "health identifiers",
      "confidentiality",
      "data minimization",
      "redaction or masking",
      "do not infer hidden authorization",
      "not legal certification",
  ):
    assert phrase in prompt
  assert "Definition: " + metric.definition in prompt


def test_factory_exports_extend_the_existing_canonical_api():
  names = {
      "builtin_metric_config",
      "build_metrics",
      "response_usefulness_metric",
      "task_grounding_metric",
      "policy_compliance_metric",
      "three_pillar_scorecard_metrics",
  }
  assert set(evaluation_rubrics.__all__) == names
  for name in names:
    assert name in bqaa.__all__
    assert getattr(bqaa, name) is getattr(evaluation_rubrics, name)
