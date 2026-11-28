# Failure diagnosis: rules, an LLM judge, and when to trust them

RFC #435 asks which of the eight G1-frozen categories explains a failed
session, and at which span. Four modules answer it. Only the judge's
backend calls a model; everything else is offline and deterministic.

| Module | Reads | Gives |
| --- | --- | --- |
| `rule_taxonomy` | the session's `agent_events` rows | `RuleFailureLabel`s from fixed rules (4 categories built in, more through `checks`) |
| `session_dossier` | the same rows | a plain-text dossier with numbered steps |
| `failure_judge` | dossiers, a rubric, a backend (BigQuery built in) | a validated `JudgeFailureLabel` per session |
| `failure_diagnosis` | both | a `SessionDiagnosis`: accepted when both agree |

## Quick start

```python
from bigquery_agent_analytics import Client
from bigquery_agent_analytics.failure_diagnosis import diagnose_failed_session
from bigquery_agent_analytics.failure_judge import BigQueryJudgeBackend
from bigquery_agent_analytics.rule_taxonomy import ToolProfile

client = Client(project_id="my-project", dataset_id="agent_analytics")
backend = BigQueryJudgeBackend.from_client(client)  # gemini-2.5-flash
profile = ToolProfile(compute_tools={"run_python"}, purpose_arg="purpose")

diagnosis = diagnose_failed_session(rows, backend, profile=profile,
                                    reference_answer="42.5")
if diagnosis.accepted:
  print(diagnosis.failure_category, diagnosis.span_id)
else:
  print(diagnosis.outcome)  # disagreed, judge_only, ... : review it
if diagnosis.same_family:
  print("the judge is from the agent's own model family")
```

`rows` are the session's raw `agent_events` rows, as
`label_failed_session_rules` takes them. For many sessions, build the
dossiers with `dossier_from_events` and call `judge_failed_sessions`
once; the backend sends them as one batch. Then call `combine_diagnosis`
per session.

## Backends

A backend only sends prompts and returns the raw text and the model that
answered. Building the dossier, wrapping it as untrusted input,
validating the reply and checking the quote are shared code that runs
the same way on every backend.

- **`BigQueryJudgeBackend`** (built in; start here) keeps trace text in the project.
  - A Gemini endpoint runs through `AI.GENERATE`, with an optional
    `connection_id` and the reply schema as `output_schema`.
  - `AI.GENERATE` only accepts Gemini endpoints. To judge with another
    model family and stay in BigQuery, create a remote model over a
    partner or open model and pass `endpoint="project.dataset.model"`;
    that runs through `ML.GENERATE_TEXT`.
  - One query answers a batch, split when it exceeds
    `max_request_bytes`.
- **`CallableJudgeBackend`** wraps `fn(system, prompt, output_schema)`
  for a judge outside BigQuery. It returns the text, or
  `(text, model_id)`. The SDK adds no provider dependency.

## The dossier

- Steps are `ToolStep.index`, so "step 4" means the same call in a rule's
  evidence and in the judge's answer. `SessionDossier.steps` maps each
  citable step to its `span_id`; a step whose rows have no `span_id` is
  shown but cannot be cited.
- It holds no rule output and no existing label. `build_session_dossier`
  takes no label argument.
- Untrusted text (the task, code, tool output, the final response) cannot
  add a step, cannot close a code fence early, cannot fake a cut marker,
  and cannot close the `<trace_data>` tag the judge prompt wraps the
  dossier in. The agent's `purpose` and the tool name stay on one line.
- Results over `max_result_chars` and code over `max_arg_chars` are cut
  with a `[cut: N of M chars shown]` marker. Over `max_total_chars`, the
  bodies of middle steps are dropped first; every step header stays. The
  task, expected answer, context and final response are cut only by
  `max_result_chars`.
- `task`, `reference_answer` and `context` add what only the caller
  knows. Benchmark-specific notes (for example that a reference answer is
  known to be wrong) belong in `context` or in the rubric.

## The judge

The system prompt states the trust boundary, the reply format and the
rubric (`DEFAULT_RUBRIC`, adapted from the AgentForensics labeling guide;
pass `rubric=` to use your own). A reply becomes a label only when:

- it is one JSON object (a single fenced block is accepted);
- the category is a frozen name, and `unknown` gives status `unknown`
  with no label;
- the step is one the dossier numbers and can cite; a `span` label needs
  one; a `gap_after_span` label without one anchors to the run's last row;
- `evidence_quote` (8 to 200 characters) is found in the dossier with
  whitespace runs compared equal, is not made only of the dossier's own
  headings, and does not include a cut marker. `quote_location` says
  where its first occurrence is (a step, or a section such as `task`,
  `context` or `final_response`).

A rejected reply is sent back with the reason, up to `max_attempts`
replies. Backend errors are not retried. `exclusions` lets the judge set
a run aside instead of labeling it (for example a known-bad reference
answer); it is empty by default.

Every result and label records `judge_model`, the model that answered
(the backend's endpoint when the model did not say), and
`judge_id = "<judge_model>:<prompt hash>"`. The hash covers the system
prompt, the taxonomy version and the dossier format, so a cache keyed on
`(judge_id, dossier.digest)` never mixes prompts.

## The combined rule

| Outcome | When | Accepted |
| --- | --- | --- |
| `excluded` | the judge excluded the run | no |
| `agreed` | the rules' primary label and the judge name the same category | yes |
| `disagreed` | both labeled it, with different categories | no |
| `engine_only` | only the rules labeled it | no |
| `judge_only` | only the judge labeled it | no |
| `none` | neither did | no |

An accepted diagnosis takes the judge's span and target kind;
`anchors_agree` says whether the rules' label is on the same span (two
gap labels count as agreeing). Only the rules' primary label is compared;
the others are in `engine_secondary`.

A judge from the agent's own model family tends to share its blind
spots. `agent_models` lists the models on the session's `LLM_REQUEST`
rows (`attributes.model`), and `same_family` is True when the judge's
model is from one of their families (`model_family()` knows the common
ones; a remote model reference is unknown, so the flag is None). The flag
is for review and does not change `accepted`.

## How well it works

Measured on 44 failed KramaBench runs with human gold labels (#435),
through these modules with the built-in rules and a deepseek-flash judge
through `CallableJudgeBackend`:

| Judge rubric and dossier `context` | Judge category | First error step | `agreed` |
| --- | --- | --- | --- |
| `DEFAULT_RUBRIC`, no context | 66% | 80% | 9 runs, 8 right |
| `DEFAULT_RUBRIC`, scoring rule and reference data sources | 75% | 75% | 8 runs, 7 right |
| Full labeling guide, same context | 77% | 77% | 9 runs, 8 right |
| Two human labelers (13 runs) | 85% | 92% | |

- Tell the judge how the run was scored and what the reference solution
  used, through `context`. Without the data sources, a wrong file often
  reads as a planning or code defect.
- `agreed` is precise but rare with the four built-in rule categories.
  With the contributors' benchmark rules added as `checks` (the built-in
  rules still come first), it covered 27 runs, 23 right. Add `checks`
  for your benchmark to raise coverage.
- n = 44, so differences of a few points are within noise. Measure on
  your own labeled sessions before relying on `agreed`.
