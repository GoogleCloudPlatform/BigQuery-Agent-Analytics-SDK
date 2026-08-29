# Evaluation Rubrics Reference

The canonical rubrics for agent quality assessment: what each metric
measures, its category vocabulary, and how to use the set for grading real
agent traffic. The rubric data and its interpreter live in the SDK
(`bigquery_agent_analytics.evaluation_rubrics`); `scripts/quality_report.py`
consumes them, and `scripts/eval/eval_config.json` ships the same data as an
editable override template (a test pins the two together).

## The three layers of a quality verdict

1. **Primary metrics** — `response_usefulness` (the headline verdict) and
   `task_grounding` (is the answer anchored in tool evidence). Running only
   these (`--dimensions primary`) cuts judge cost roughly 4x.
2. **Quality dimensions** — `correctness`, `tool_usage`, `specificity`,
   `scope_compliance`, `first_time_right`. Each is a 3-4 category drilldown
   that explains *why* a session scored the way it did; reports average them
   on a 0-2 scale.
3. **Triage** — `failure_attribution` names the *owner* of a failure:
   a `skill_gap` is fixable by editing the agent's instructions, a
   `knowledge_gap` needs data added behind a tool, a `tool_gap` needs
   engineering. This is what turns a score into a backlog.

## The rubrics

### `response_usefulness`

Whether the agent final response provides a genuinely useful, substantive answer to the user question. A response that apologizes, says it cannot help, returns no data, provides only generic filler, or loops without resolving the question is NOT useful. If the conversation contains a user correction and the agent merely repeated or acknowledged the correction without independently verifying it (e.g. re-querying a tool, citing a new source), the response is NOT useful — the user did the agent's work.

| Category | Meaning |
| --- | --- |
| `meaningful` | The response directly and substantively addresses the user question with specific, actionable information. |
| `unhelpful` | The response does NOT meaningfully answer the user question. This includes: (1) The agent said 'I don't have that information', gave generic advice, or directed the user elsewhere instead of using its tools. (2) The agent apologized without answering. (3) Empty data results or generic filler text. (4) The agent looped without resolution. (5) The agent only became correct after the user provided the right answer and the agent repeated it without independent verification (e.g. re-querying a tool). |
| `partial` | The response partially addresses the question but is incomplete, missing key details, or only tangentially relevant. |

**Scope-aware:** when the eval spec provides a `scope`, its text is appended to this metric's judge definition, so the judge grades against the agent's declared lane.
**Scope-conditional category:** with a scope present, `declined` is inserted after `meaningful` — The TOPIC of the question is explicitly listed as out of scope (see AGENT SCOPE CONTEXT above) and the agent correctly declined. Use this ONLY when the topic itself is out of scope -- NOT when the agent simply failed to find an answer for an in-scope topic.
**Scope suffix added to the definition:** UNLESS the question is outside the agent's defined scope, in which case a polite decline IS a correct and meaningful response.

### `task_grounding`

Whether the agent response is grounded in actual data retrieved from its tools, or is fabricated / hallucinated general knowledge.

| Category | Meaning |
| --- | --- |
| `grounded` | The response is clearly based on data retrieved from the agent tools (search results, database lookups, API calls). |
| `ungrounded` | The response appears to be fabricated or based on the LLM general knowledge rather than actual tool results. The tool may have returned empty data and the agent filled in anyway. |
| `no_tool_needed` | The question did not require tool usage and a direct LLM response was appropriate. |

### `correctness`

Whether the facts stated in the agent response are accurate. Evaluate based on the information the agent retrieved from its tools and whether it was conveyed faithfully.

| Category | Meaning |
| --- | --- |
| `correct` | All facts stated by the agent are accurate and consistent with the tool results retrieved. |
| `mostly_correct` | The response is mostly correct but contains a minor inaccuracy, omission, or imprecise wording. |
| `incorrect` | The response contains wrong facts, hallucinated information, or claims contradicted by the tool results. |

### `tool_usage`

Whether the agent used its available tools correctly to answer the question, rather than relying on general knowledge.

| Category | Meaning |
| --- | --- |
| `proper` | The agent used its tools and based the answer on the tool results. Tools were called with appropriate parameters. |
| `partial` | The agent partially used tools, or tool usage was unclear or incomplete. Some information may not be tool-derived. |
| `none` | The agent answered from general knowledge without looking up information via tools, even though tools were available and the question warranted their use. DECISIVE TEST: if the question was in-scope and a tool could have supplied the answer, but the trace shows no relevant tool call, this is `none` (a failure) -- do NOT use `no_tool_needed` to excuse a missing lookup. |
| `no_tool_needed` | The question genuinely required no tool lookup -- e.g. a greeting, a meta/clarification turn, or an out-of-scope topic the agent correctly declined. Not using a tool was the CORRECT behavior here, so this is a positive outcome, not a failure. Use this ONLY when no tool was needed; if the question was an in-scope data lookup the agent should have performed, use `none` instead. |

### `specificity`

Whether the agent response provides specific, concrete details (numbers, dates, dollar amounts, limits) rather than vague or generic statements.

| Category | Meaning |
| --- | --- |
| `specific` | The response includes specific and complete details: exact numbers, percentages, dollar amounts, dates, or limits. |
| `somewhat_specific` | The response is somewhat specific but missing some key details that would make it fully actionable. |
| `vague` | The response is vague, generic, or missing key specifics that the user needs to act on the information. |

### `scope_compliance`

Whether the agent correctly handled the scope of the question. An agent should answer in-scope questions and politely decline out-of-scope ones.

| Category | Meaning |
| --- | --- |
| `compliant` | The agent correctly answered an in-scope question OR correctly declined an out-of-scope question. |
| `partially_compliant` | The agent answered but with unnecessary caveats, excessive hedging, or was partially out of scope. |
| `non_compliant` | The agent tried to answer an out-of-scope question it should have declined, OR refused to answer an in-scope question it should have handled. |

**Scope-aware:** when the eval spec provides a `scope`, its text is appended to this metric's judge definition, so the judge grades against the agent's declared lane.

### `first_time_right`

Whether the agent's FIRST response in the conversation was satisfactory, without needing user corrections or follow-ups to fix errors. For single-turn conversations, evaluate the only response. For multi-turn, focus on whether the first substantive answer was correct.

| Category | Meaning |
| --- | --- |
| `correct` | The first response was correct and complete. No correction or significant clarification was needed from the user. |
| `clarification_needed` | The first response was mostly right but needed minor clarification or a follow-up to be fully useful. |
| `correction_needed` | The first response was wrong, vague, or incomplete enough that the user had to push back or correct the agent. |

### `failure_attribution`

ROOT CAUSE of a failure: when the agent did NOT give a useful answer, why? Use the AGENT TOOLS / CAPABILITIES context above to decide which fixer is responsible. If the response WAS useful (a substantive answer or a correct decline of an out-of-scope topic), return not_a_failure.

| Category | Meaning |
| --- | --- |
| `not_a_failure` | The response was useful -- a substantive answer, or a correct polite decline of a genuinely out-of-scope topic. No failure to attribute. |
| `skill_gap` | The agent HAD the means to answer but behaved wrong: it failed to route to the right sub-agent, did not call an available tool, echoed/parroted the user's correction without re-verifying, or stated facts that contradict its tools. The tool and data needed were available -- this is fixable by improving the agent's instructions (skill). |
| `knowledge_gap` | The agent correctly used a tool that DOES cover this topic, but the SPECIFIC fact requested was not present in the data the tool returned (the data source is incomplete on this detail). Fixable by a human adding the missing fact to the existing data source -- not by changing instructions. |
| `tool_gap` | No tool or capability could even attempt this request. Either (a) the question is about a topic that NONE of the listed tools has any data source for, or (b) it needs the individual user's personal/account data (their actual balance, enrollment status) or an ACTION (submit, file, enroll) that no tool provides. Fixable only by an engineer building a new tool or data source -- not by skill evolution or by adding a fact. |

**Scope-aware:** when the eval spec provides a `scope`, its text is appended to this metric's judge definition, so the judge grades against the agent's declared lane.

## Scope, declines, and why they matter

A judge without scope knowledge punishes correct refusals: an agent that
cleanly declines an out-of-scope request ("I can't advise on stock picks")
reads as unhelpful. When your eval spec carries a `scope` string, two things
happen:

- scope-aware metric definitions gain that text, and
- the `declined` category is injected into `response_usefulness`, so a clean,
  correctly-routed refusal is a **win**, distinct from `unhelpful`.

Without a scope, the vocabulary stays 3-way and nothing about your existing
dashboards changes — category names are stable either way.

## Golden Q&A grounding

The rubrics define *what* the judge grades; a golden Q&A defines *the truth
it grades against*. Supply per-session expected answers through
`Client.evaluate_categorical(per_session_context=...,
context_source=CategoricalContextSource.GOLDEN_EXPECTED_ANSWER)` (or via
`scripts/quality_report.py --eval-spec`, which matches session questions to
the golden list by embedding similarity and threads the matches for you).
Grounded grading is what stops a fluent wrong answer from scoring as
`meaningful`.

## Using the rubrics

### Python (SDK)

```python
from bigquery_agent_analytics import (
    CategoricalEvaluationConfig,
    Client,
    build_metrics,
)

metrics = build_metrics(
    scope_context=" The agent answers employee HR policy questions only.",
    has_scope=True,          # enables the `declined` category
)
config = CategoricalEvaluationConfig(metrics=metrics, endpoint="gemini-2.5-flash")
report = Client(project_id=..., dataset_id=...).evaluate_categorical(config)
```

### CLI (`scripts/quality_report.py`)

```bash
# Full rubric set against BigQuery traffic, golden-grounded:
python scripts/quality_report.py --eval-spec eval/eval_spec.json \
    --app-name my-agent --time-period 24h --dimensions full --report

# Primary metrics only (~4x cheaper):
python scripts/quality_report.py --eval-spec eval/eval_spec.json --dimensions primary
```

The script derives `scope_context`/`has_scope` from the eval spec's `scope`
and `ground_truth` fields automatically.

### Customizing

Pass `--eval-config my_config.json` (CLI) or `build_metrics(my_config)`
(Python) with the same schema to add, remove, or reword metrics. Start from
`builtin_metric_config()` — it returns a deep copy you can mutate — or from
`scripts/eval/eval_config.json`. Per-metric fields:

| Field | Meaning |
| --- | --- |
| `name`, `definition` | Metric id and the judge instruction. |
| `categories[]` | `{name, definition}` — the closed category vocabulary. |
| `scope_aware` | Append the caller's scope text to the definition. |
| `declined_category` | Injected only when a scope is present; optional `insert_after` fixes its position. |
| `scope_suffix` | Extra definition text added alongside the declined injection. |

Keep category *names* stable if you feed existing dashboards — the reports
and views key on them.
