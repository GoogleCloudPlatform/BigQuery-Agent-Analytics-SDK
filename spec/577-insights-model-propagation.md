# Spec: insights model propagation (#577)

## Terms

- **Run model:** `text_model or client.endpoint`, the expression
  `Client._run_insights` already computes.
- **BigQuery ML reference:** a value with two or more dots, by the client's
  existing `_is_legacy_model_ref` rule (unchanged).
- **Endpoint URL:** a value that starts with `https://`.
- **Gemini API model:** the `model=` that a step passes to
  `generate_content`.

## Behavior

- **S1. AI.GENERATE facets.** The tier runs when the run model is not a
  BigQuery ML reference, and its query names the run model in
  `endpoint => '...'`, escaped as today. Before: gated on, and named, the
  client's `endpoint`.
- **S2. ML.GENERATE_TEXT facets.** Unchanged: `MODEL` names the run model.
- **S3. Gemini API model of the run.** The first of `text_model`, then the
  client's `endpoint`, that names a model:
  - a non-empty string that is neither an endpoint URL nor a BigQuery ML
    reference names itself;
  - an endpoint URL names the path segment after its last `/models/`, cut at
    the first `:`, `?`, `#` or `/`; a URL without one names nothing;
  - a BigQuery ML reference, an empty value, `None` or a non-string names
    nothing.

  When neither names a model, the run uses `DEFAULT_ENDPOINT`.
- **S4. Gemini API steps.** The facet fallback, every analysis prompt and the
  executive summary call the S3 model.
- **S5. Helper defaults.** `extract_facets_via_api`, `run_analysis_prompt` and
  `generate_executive_summary` keep their `model` parameter; its default
  becomes the shared `DEFAULT_ENDPOINT` constant, the same value as before.
- **S6. Docs.** The `Client.insights()` docstring for `text_model` describes
  S1 to S4.
- **S7. Unchanged.** SQL escaping, job labels, tier order (AI.GENERATE, then
  ML.GENERATE_TEXT, then the Gemini API), the per-session and per-prompt error
  handling, and the `InsightsReport` shape. The Gemini API client keeps its
  own project and location. A non-string run model still fails before the
  first facet query, as a non-string endpoint already does.

## Acceptance

Tests in `tests/test_insights_model_propagation.py`, through both
`insights()` and `insights_async()` unless noted. X is an endpoint, Y a
text_model.

- **A1.** A default client: every Gemini API step calls `DEFAULT_ENDPOINT`.
- **A2.** `Client(endpoint=X)`: every step calls X.
- **A3.** `insights(text_model=Y)`: every step calls Y.
- **A4.** `Client(endpoint=X).insights(text_model=Y)`: every step calls Y.
- **A5.** `Client(endpoint=<URL whose path ends in /models/X>)`: every step
  calls X.
- **A6.** `Client(endpoint=X).insights(text_model=<BigQuery ML reference>)`:
  every step calls X; no AI.GENERATE query is sent, and ML.GENERATE_TEXT names
  the reference.
- **A7.** `Client(endpoint=<BigQuery ML reference>)`: every step calls
  `DEFAULT_ENDPOINT`.
- **A8.** AI.GENERATE facets succeed: the analysis prompts and the summary
  call X, and the Gemini API extracts no facets.
- **A9.** AI.GENERATE names X for `Client(endpoint=X)`, and Y over X when Y is
  given.
- **A10.** The Remote Function's `insights` operation with endpoint X in its
  context: every step calls X (one entry point).
- **A11.** The three helpers default to `DEFAULT_ENDPOINT`.
- **A12.** The existing insights, client and Remote Function tests still pass.

"Every step" means one facet call per session (when facet extraction reaches
the Gemini API), one call per analysis prompt and one summary call, and no
other Gemini API call.
