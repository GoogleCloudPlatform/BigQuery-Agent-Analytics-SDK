# Intent: insights calls the model the caller configured (#577)

## Problem

Issue #577: `Client(location="us", endpoint="gemini-3.8-flash").insights()`
logs `Facet extraction failed ... 404 NOT_FOUND` for
`publishers/google/models/gemini-2.5-flash` in location `us`. Passing
`insights(text_model=...)` gives the same error. The reporter's workaround is
to point the Gemini API location at one that serves `gemini-2.5-flash`.

## Cause (confirmed on 674973b)

Three insights steps call the Gemini API with a fixed model, whatever the
client or the run configured:

1. `Client._run_insights` calls `run_analysis_prompt(..., model="gemini-2.5-flash")`
   for every analysis prompt.
2. `Client._extract_facets` ends with `extract_facets_via_api(transcripts)`, so
   the helper's default `gemini-2.5-flash` wins in the facet fallback.
3. `Client._run_insights` calls `generate_executive_summary(report)` without a
   model, so the helper's default wins.

Facet extraction in BigQuery also ignores the run's `text_model` on its first
tier: AI.GENERATE always names the client's `endpoint`, so when AI.GENERATE
works, `insights(text_model=Y)` never reaches facet extraction.

## Outcome

One model per insights run: `text_model` when given, else the client's
`endpoint`. Every LLM step of the run calls that model. A step falls back to
the SDK default model only when neither value names a model that the step can
call.

## Out of scope

- Issue #576.
- Which project and location the Gemini API client uses (environment driven,
  unchanged).
- Model defaults of other features (memory service, evaluators, categorical
  evaluation, context graph, CLI).
- `Client._is_legacy_model_ref` counting the dots of a full endpoint URL, which
  sends a URL endpoint past AI.GENERATE in every feature (pre-existing).
- A `--endpoint` option for the `insights` CLI command.
