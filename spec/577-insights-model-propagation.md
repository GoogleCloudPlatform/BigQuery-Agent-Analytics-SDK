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
  - an endpoint URL names the segment after the last `/models/` in its path,
    cut at the first `:` or `/`. The path ends at the URL's first `?` or `#`,
    so a `/models/` in the query or fragment names nothing, and a URL whose
    path has no such segment names nothing;
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
  own project and location.
- **S8. Non-string models fail closed.** Before any query, `insights()` and
  `insights_async()` raise `TypeError` when `text_model` is neither a string
  nor `None` (falsy values such as `0` and `[]` included), or when the
  client's `endpoint` is not a string, whether or not `text_model` is given.
  On main (674973b), a non-string `text_model` was ignored: AI.GENERATE ran
  with the endpoint, and only the ML.GENERATE_TEXT tier saw the value, whose
  error handling dropped it. A non-string endpoint failed with
  `AttributeError` after the session-metadata query when it was an integer
  or a boolean, and a list fell through every BigQuery tier to the Gemini
  API fallback.

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
- **A13.** `text_model=<URL>` with endpoint X: the method, query, fragment and
  trailing-slash suffixes still end the model name. A `/models/` in the query
  or fragment does not replace the model in the path. A URL whose path names
  no model leaves every step on X.
- **A14.** `text_model` of `123`, `True`, `b"..."`, a list, a tuple, a dict,
  `0` or `[]`, and a client endpoint of `123` or a list, with or without a
  string `text_model`: `TypeError` before any BigQuery query or Gemini API
  call.

"Every step" means one facet call per session (when facet extraction reaches
the Gemini API), one call per analysis prompt and one summary call, and no
other Gemini API call.
