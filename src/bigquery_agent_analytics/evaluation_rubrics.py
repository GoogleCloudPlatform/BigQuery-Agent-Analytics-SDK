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

"""Canonical evaluation rubrics for agent quality assessment.

The metric DATA (names, category vocabularies, judge definitions) and the
INTERPRETER that turns it into ``CategoricalMetricDefinition`` objects --
including the scope-conditional ``declined`` category injection -- extracted
from ``scripts/quality_report.py`` so hosts can consume the canonical rubrics
without vendoring the script (issue reference: canonical-rubrics extraction,
superseding the #91 Phase-1 snapshot).

Two entry points:

- ``builtin_metric_config()`` -- a deep copy of the canonical 8-metric config
  (same schema as an ``--eval-config`` JSON file: ``{"metrics": [...]}``).
- ``build_metrics(eval_config=None, *, scope_context="", has_scope=False)``
  -- interpret a metric config into ``CategoricalMetricDefinition`` objects.
  ``scope_context`` (free text describing the agent's lane) is appended to
  ``scope_aware`` metric definitions; ``has_scope=True`` additionally injects
  each metric's ``declined_category`` so the judge can credit correct
  out-of-scope refusals as wins.

See ``docs/evaluation_rubrics.md`` for the rubric reference.
"""

import copy
import json as _json
import logging
from typing import Optional

from bigquery_agent_analytics.categorical_evaluator import CategoricalMetricCategory
from bigquery_agent_analytics.categorical_evaluator import CategoricalMetricDefinition

logger = logging.getLogger(__name__)

# The canonical rubric data. Schema per metric:
#   name, definition, categories: [{name, definition}],
#   optional scope_aware (bool): append the caller's scope context to the
#     definition;
#   optional declined_category {name, definition, insert_after?}: injected
#     only when the evaluation has a scope (has_scope=True);
#   optional scope_suffix (str): appended alongside the declined injection.
_BUILTIN_METRIC_CONFIG = _json.loads(
    r"""
{
  "metrics": [
    {
      "name": "response_usefulness",
      "definition": "Whether the agent final response provides a genuinely useful, substantive answer to the user question. A response that apologizes, says it cannot help, returns no data, provides only generic filler, or loops without resolving the question is NOT useful. If the conversation contains a user correction and the agent merely repeated or acknowledged the correction without independently verifying it (e.g. re-querying a tool, citing a new source), the response is NOT useful \u2014 the user did the agent's work.",
      "categories": [
        {
          "name": "meaningful",
          "definition": "The response directly and substantively addresses the user question with specific, actionable information."
        },
        {
          "name": "unhelpful",
          "definition": "The response does NOT meaningfully answer the user question. This includes: (1) The agent said 'I don't have that information', gave generic advice, or directed the user elsewhere instead of using its tools. (2) The agent apologized without answering. (3) Empty data results or generic filler text. (4) The agent looped without resolution. (5) The agent only became correct after the user provided the right answer and the agent repeated it without independent verification (e.g. re-querying a tool)."
        },
        {
          "name": "partial",
          "definition": "The response partially addresses the question but is incomplete, missing key details, or only tangentially relevant."
        }
      ],
      "required": true,
      "scope_aware": true,
      "declined_category": {
        "name": "declined",
        "definition": "The TOPIC of the question is explicitly listed as out of scope (see AGENT SCOPE CONTEXT above) and the agent correctly declined. Use this ONLY when the topic itself is out of scope -- NOT when the agent simply failed to find an answer for an in-scope topic.",
        "insert_after": "meaningful"
      },
      "scope_suffix": " UNLESS the question is outside the agent's defined scope, in which case a polite decline IS a correct and meaningful response."
    },
    {
      "name": "task_grounding",
      "definition": "Whether the agent response is grounded in actual data retrieved from its tools, or is fabricated / hallucinated general knowledge.",
      "categories": [
        {
          "name": "grounded",
          "definition": "The response is clearly based on data retrieved from the agent tools (search results, database lookups, API calls)."
        },
        {
          "name": "ungrounded",
          "definition": "The response appears to be fabricated or based on the LLM general knowledge rather than actual tool results. The tool may have returned empty data and the agent filled in anyway."
        },
        {
          "name": "no_tool_needed",
          "definition": "The question did not require tool usage and a direct LLM response was appropriate."
        }
      ],
      "required": true
    },
    {
      "name": "correctness",
      "definition": "Whether the facts stated in the agent response are accurate. Evaluate based on the information the agent retrieved from its tools and whether it was conveyed faithfully.",
      "categories": [
        {
          "name": "correct",
          "definition": "All facts stated by the agent are accurate and consistent with the tool results retrieved."
        },
        {
          "name": "mostly_correct",
          "definition": "The response is mostly correct but contains a minor inaccuracy, omission, or imprecise wording."
        },
        {
          "name": "incorrect",
          "definition": "The response contains wrong facts, hallucinated information, or claims contradicted by the tool results."
        }
      ],
      "required": true
    },
    {
      "name": "tool_usage",
      "definition": "Whether the agent used its available tools correctly to answer the question, rather than relying on general knowledge.",
      "categories": [
        {
          "name": "proper",
          "definition": "The agent used its tools and based the answer on the tool results. Tools were called with appropriate parameters."
        },
        {
          "name": "partial",
          "definition": "The agent partially used tools, or tool usage was unclear or incomplete. Some information may not be tool-derived."
        },
        {
          "name": "none",
          "definition": "The agent answered from general knowledge without looking up information via tools, even though tools were available and the question warranted their use. DECISIVE TEST: if the question was in-scope and a tool could have supplied the answer, but the trace shows no relevant tool call, this is `none` (a failure) -- do NOT use `no_tool_needed` to excuse a missing lookup."
        },
        {
          "name": "no_tool_needed",
          "definition": "The question genuinely required no tool lookup -- e.g. a greeting, a meta/clarification turn, or an out-of-scope topic the agent correctly declined. Not using a tool was the CORRECT behavior here, so this is a positive outcome, not a failure. Use this ONLY when no tool was needed; if the question was an in-scope data lookup the agent should have performed, use `none` instead."
        }
      ],
      "required": true
    },
    {
      "name": "specificity",
      "definition": "Whether the agent response provides specific, concrete details (numbers, dates, dollar amounts, limits) rather than vague or generic statements.",
      "categories": [
        {
          "name": "specific",
          "definition": "The response includes specific and complete details: exact numbers, percentages, dollar amounts, dates, or limits."
        },
        {
          "name": "somewhat_specific",
          "definition": "The response is somewhat specific but missing some key details that would make it fully actionable."
        },
        {
          "name": "vague",
          "definition": "The response is vague, generic, or missing key specifics that the user needs to act on the information."
        }
      ],
      "required": true
    },
    {
      "name": "scope_compliance",
      "definition": "Whether the agent correctly handled the scope of the question. An agent should answer in-scope questions and politely decline out-of-scope ones.",
      "categories": [
        {
          "name": "compliant",
          "definition": "The agent correctly answered an in-scope question OR correctly declined an out-of-scope question."
        },
        {
          "name": "partially_compliant",
          "definition": "The agent answered but with unnecessary caveats, excessive hedging, or was partially out of scope."
        },
        {
          "name": "non_compliant",
          "definition": "The agent tried to answer an out-of-scope question it should have declined, OR refused to answer an in-scope question it should have handled."
        }
      ],
      "required": true,
      "scope_aware": true
    },
    {
      "name": "first_time_right",
      "definition": "Whether the agent's FIRST response in the conversation was satisfactory, without needing user corrections or follow-ups to fix errors. For single-turn conversations, evaluate the only response. For multi-turn, focus on whether the first substantive answer was correct.",
      "categories": [
        {
          "name": "correct",
          "definition": "The first response was correct and complete. No correction or significant clarification was needed from the user."
        },
        {
          "name": "clarification_needed",
          "definition": "The first response was mostly right but needed minor clarification or a follow-up to be fully useful."
        },
        {
          "name": "correction_needed",
          "definition": "The first response was wrong, vague, or incomplete enough that the user had to push back or correct the agent."
        }
      ],
      "required": true
    },
    {
      "name": "failure_attribution",
      "definition": "ROOT CAUSE of a failure: when the agent did NOT give a useful answer, why? Use the AGENT TOOLS / CAPABILITIES context above to decide which fixer is responsible. If the response WAS useful (a substantive answer or a correct decline of an out-of-scope topic), return not_a_failure.",
      "categories": [
        {
          "name": "not_a_failure",
          "definition": "The response was useful -- a substantive answer, or a correct polite decline of a genuinely out-of-scope topic. No failure to attribute."
        },
        {
          "name": "skill_gap",
          "definition": "The agent HAD the means to answer but behaved wrong: it failed to route to the right sub-agent, did not call an available tool, echoed/parroted the user's correction without re-verifying, or stated facts that contradict its tools. The tool and data needed were available -- this is fixable by improving the agent's instructions (skill)."
        },
        {
          "name": "knowledge_gap",
          "definition": "The agent correctly used a tool that DOES cover this topic, but the SPECIFIC fact requested was not present in the data the tool returned (the data source is incomplete on this detail). Fixable by a human adding the missing fact to the existing data source -- not by changing instructions."
        },
        {
          "name": "tool_gap",
          "definition": "No tool or capability could even attempt this request. Either (a) the question is about a topic that NONE of the listed tools has any data source for, or (b) it needs the individual user's personal/account data (their actual balance, enrollment status) or an ACTION (submit, file, enroll) that no tool provides. Fixable only by an engineer building a new tool or data source -- not by skill evolution or by adding a fact."
        }
      ],
      "required": true,
      "scope_aware": true
    }
  ]
}
"""
)


def builtin_metric_config() -> dict:
  """A deep copy of the canonical metric config (mutate freely)."""
  return copy.deepcopy(_BUILTIN_METRIC_CONFIG)


def build_metrics(
    eval_config: Optional[dict] = None,
    *,
    scope_context: str = "",
    has_scope: bool = False,
) -> list[CategoricalMetricDefinition]:
  """Interpret a metric config into categorical metric definitions.

  Args:
    eval_config: A ``{"metrics": [...]}`` dict (same schema as an
      ``--eval-config`` JSON file). Defaults to the builtin canonical config.
    scope_context: Free text describing the agent's scope/ground truth;
      appended to the definitions of ``scope_aware`` metrics when non-empty.
    has_scope: When True, each metric's optional ``declined_category`` is
      injected into its category list (after ``insert_after`` when given,
      else appended) and its ``scope_suffix`` is added -- this is what lets
      the judge credit a clean out-of-scope refusal as a win instead of
      scoring it unhelpful.

  Returns:
    A list of ``CategoricalMetricDefinition`` ready for
    ``CategoricalEvaluationConfig(metrics=...)``.
  """
  if eval_config is None:
    eval_config = _BUILTIN_METRIC_CONFIG
  result = []
  for m in eval_config.get("metrics", []):
    cats = [
        CategoricalMetricCategory(name=c["name"], definition=c["definition"])
        for c in m["categories"]
    ]
    defn = m["definition"]
    if m.get("scope_aware") and scope_context:
      defn += scope_context
    if has_scope and m.get("declined_category"):
      dc = m["declined_category"]
      declined_cat = CategoricalMetricCategory(
          name=dc["name"], definition=dc["definition"]
      )
      insert_after = dc.get("insert_after")
      if insert_after:
        idx = next(
            (i for i, c in enumerate(cats) if c.name == insert_after), -1
        )
        cats.insert(idx + 1, declined_cat)
      else:
        cats.append(declined_cat)
      if m.get("scope_suffix"):
        defn += m["scope_suffix"]
    result.append(
        CategoricalMetricDefinition(
            name=m["name"], definition=defn, categories=cats
        )
    )
  logger.info("Built %d metric definitions from eval config", len(result))
  return result
