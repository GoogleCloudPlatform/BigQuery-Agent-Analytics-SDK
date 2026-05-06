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

"""Retry-on-gate-failure orchestration for compiled extractors
(PR 4b.2.2.c.2 of issue #75).

Wires up:

* :func:`build_retry_prompt` — pure function that wraps the
  original resolution prompt with the LLM's prior response and a
  diagnostic explaining what failed. Same inputs → byte-identical
  output.
* :class:`AttemptRecord` — one row per loop iteration; exactly one
  failure channel is populated for a failed attempt
  (``plan_parse_error`` / ``render_error`` / ``compile_result``)
  so future telemetry can route on the field name without an
  ad-hoc switch.
* :class:`RetryCompileResult` — the loop's return value. ``ok``
  iff the final attempt produced a valid bundle; ``attempts``
  preserves the per-iteration history in order so logging can
  reconstruct what the LLM tried, why it failed, and what
  diagnostic got fed forward.
* :func:`compile_with_llm` — the orchestrator. Calls the LLM,
  parses, renders, compiles; on any gate failure builds a
  diagnostic via :mod:`.diagnostics`, embeds it in the next
  prompt, loops until ``max_attempts``.

Design notes:

* **No PlanResolver indirection.** The loop calls
  :func:`build_resolution_prompt` /
  :func:`parse_resolved_extractor_plan_json` /
  :func:`llm_client.generate_json` directly so it can capture the
  raw response for retry-prompt embedding without
  ``PlanResolver`` acquiring loop-specific state. The parse
  contract is the same.
* **Compile injection.** ``compile_extractor`` takes ten-plus
  required kwargs (sample events, spec, parent_bundle_dir,
  fingerprint inputs, …); rather than re-plumb them through the
  loop signature, callers pass a ``compile_source(plan, source)``
  callable that returns a ``CompileResult``. The default closes
  over a :func:`compile_extractor` invocation; tests can pass a
  fake callable that returns canned ``CompileResult`` instances
  without touching the filesystem.
* **No silent retry on transport / auth / quota.** Anything the
  LLM client raises that isn't a ``PlanParseError`` (which the
  parser raises *after* the call) propagates unchanged. A future
  caller can wrap the client with its own retry policy if it
  wants automatic backoff on rate limits.
* **Renderer ``ValueError``** is its own diagnostic channel
  (``"RenderError [code=invalid_plan]: ..."``) — post-parse,
  pre-compile — so retry telemetry doesn't conflate it with
  parser failures.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import pathlib
from typing import Any, Callable, Optional

from .compiler import CompileResult
from .diagnostics import build_compile_result_diagnostic
from .diagnostics import build_plan_parse_diagnostic
from .manifest import Manifest
from .plan_parser import parse_resolved_extractor_plan_json
from .plan_parser import PlanParseError
from .plan_parser import RESOLVED_EXTRACTOR_PLAN_JSON_SCHEMA
from .plan_resolver import build_resolution_prompt
from .plan_resolver import LLMClient
from .template_renderer import render_extractor_source
from .template_renderer import ResolvedExtractorPlan

_REASON_SUCCEEDED = "succeeded"
_REASON_MAX_ATTEMPTS = "max_attempts_reached"


CompileSource = Callable[[ResolvedExtractorPlan, str], CompileResult]
"""Caller-supplied callable that turns ``(plan, source)`` into a
:class:`CompileResult`. Closes over the compile-time inputs
(sample_events, spec, resolved_graph, parent_bundle_dir,
fingerprint inputs, version strings, isolation flags, …) so the
retry loop's signature stays narrow. Production callers wrap
:func:`compile_extractor`; tests pass a fake that returns canned
results."""


@dataclasses.dataclass(frozen=True)
class AttemptRecord:
  """One iteration of the retry loop.

  For a *failed* attempt, exactly one of ``plan_parse_error`` /
  ``render_error`` / ``compile_result`` is populated; the others
  are ``None``. (When ``compile_result`` is the populated channel
  on a failed attempt, ``compile_result.ok`` is ``False``.) For
  the *successful* terminal attempt, ``plan_parse_error`` and
  ``render_error`` are both ``None`` and ``compile_result`` is
  populated with ``compile_result.ok == True``. Tests / telemetry
  can route on the field name without re-deriving which channel
  fired.

  Fields:

  * ``attempt`` — 1-indexed iteration number.
  * ``prompt`` — the prompt sent to the LLM. Stored verbatim so
    logs can replay what the LLM saw. Telemetry / log persistence
    may want to redact sensitive payloads later; the in-memory
    record keeps the full string.
  * ``raw_response`` — the dict returned by the LLM client
    (the parser input). ``None`` if the LLM call itself raised
    (which propagates rather than being caught, but the field
    stays in the record shape for symmetry).
  * ``plan_parse_error`` — populated if the parser rejected the
    response. Mutually exclusive with ``render_error`` /
    ``compile_result`` for a failed attempt.
  * ``render_error`` — message from a :func:`render_extractor_source`
    ``ValueError`` (post-parse, pre-compile). Rare in practice
    since the parser catches most plan-shape problems, but kept
    distinct from parser errors so retry telemetry doesn't
    conflate the two channels.
  * ``compile_result`` — the ``CompileResult`` from the compile
    step. Always populated when render succeeded; the loop
    inspects ``compile_result.ok`` to decide whether to retry.
  * ``diagnostic`` — the diagnostic string fed into the *next*
    attempt's prompt. ``None`` on the successful terminal attempt
    or when the loop exhausted at this attempt.
  """

  attempt: int
  prompt: str
  raw_response: Optional[dict]
  plan_parse_error: Optional[PlanParseError] = None
  render_error: Optional[str] = None
  compile_result: Optional[CompileResult] = None
  diagnostic: Optional[str] = None


@dataclasses.dataclass(frozen=True)
class RetryCompileResult:
  """Outcome of one :func:`compile_with_llm` run.

  ``ok`` is True iff the final attempt produced a valid bundle.
  ``manifest`` / ``bundle_dir`` mirror the successful
  ``CompileResult`` (and are ``None`` on failure).
  ``attempts`` preserves per-iteration history so logging can
  reconstruct the LLM's progression even on success — useful for
  tuning prompt rules or judging whether a particular failure
  mode is recoverable in N attempts.
  """

  ok: bool
  manifest: Optional[Manifest]
  bundle_dir: Optional[pathlib.Path]
  attempts: tuple[AttemptRecord, ...]
  reason: str


def _serialize_prior_response(value: Any) -> str:
  """Serialize *value* for embedding in the retry prompt.

  Tries the strict path first: ``json.dumps(..., sort_keys=True,
  indent=2)`` for byte-stability across semantically-equal
  inputs. Falls back to insertion-order JSON when ``sort_keys``
  raises (mixed-type keys can't be sorted), and finally to
  ``repr()`` for anything ``json.dumps`` can't handle at all
  (custom objects with weird ``__getitem__``, circular
  references, …).

  Both fallback steps catch ``(TypeError, ValueError)``:
  ``json.dumps`` raises ``TypeError`` for unsortable keys /
  unserializable types and ``ValueError`` for circular
  references. Restricting to one or the other would leave a
  failure mode escaping as an uncaught exception — and the whole
  point of the helper is that a parser-rejected response (which
  the retry loop is *designed* to recover from) must not crash
  one frame above.

  This matters because the parser explicitly handles structurally
  malformed responses (mixed-type keys land as
  ``unknown_field`` / ``missing_required_field`` / similar) and
  the retry loop is supposed to feed those failures back to the
  LLM. Without the fallback, the retry-prompt builder would
  raise on exactly the inputs the loop was designed to recover
  from — turning a recoverable failure into an unrecoverable
  crash one frame above.
  """
  try:
    return json.dumps(value, sort_keys=True, indent=2)
  except (TypeError, ValueError):
    pass
  try:
    return json.dumps(value, indent=2)
  except (TypeError, ValueError):
    return repr(value)


def build_retry_prompt(
    *,
    original_prompt: str,
    prior_response: Any,
    diagnostic: str,
) -> str:
  """Wrap *original_prompt* with the LLM's prior response and a
  diagnostic explaining why it was rejected.

  Pure function. Same well-formed inputs → byte-identical
  output. The embedded ``prior_response`` is serialized with
  ``sort_keys=True`` and ``indent=2`` so two semantically-equal
  dicts produce the same retry prompt — matters for fingerprint /
  cache stability if the loop ever logs prompts as inputs. For
  malformed responses where ``sort_keys`` can't serialize (e.g.,
  a dict with mixed-type keys, which the parser rejects but the
  retry loop still has to echo back), the helper falls back to
  a deterministic-but-not-byte-stable serialization rather than
  raising — losing byte-stability is acceptable on a degenerate
  input that already produced a parser failure.

  Args:
    original_prompt: The output of
      :func:`build_resolution_prompt` for this rule + schema.
      Reused verbatim so the LLM sees the same grounding (output
      contract, mapping rules, inputs) on every retry.
    prior_response: The *raw* response from the LLM's previous
      attempt — the ``dict`` it emitted, not the string. Allowed
      to be a non-dict (str / list / None) when the parser
      rejected the response shape itself; we still echo it back
      so the LLM can see what it produced.
    diagnostic: Output of
      :func:`build_plan_parse_diagnostic` /
      :func:`build_compile_result_diagnostic` (or the renderer-
      error string the orchestrator synthesizes). Fed in
      verbatim — the diagnostic builders already produce
      LLM-actionable text.

  Returns:
    A prompt string with three sections appended after the
    original prompt: the prior response, the diagnostic, and a
    "now emit a new JSON object that fixes this" instruction.
  """
  prior_json = _serialize_prior_response(prior_response)
  return f"""\
{original_prompt}
# Previous attempt

Your previous response was rejected. Below is the JSON you
emitted, followed by the diagnostic from the deterministic
compile pipeline. Fix the failure and emit a new JSON object
that conforms to the same output contract.

## Previous response

{prior_json}

## Diagnostic

{diagnostic}

Now emit a new JSON object that addresses the diagnostic above.
"""


def compile_with_llm(
    *,
    extraction_rule: Any,
    event_schema: Any,
    llm_client: LLMClient,
    compile_source: CompileSource,
    max_attempts: int = 5,
) -> RetryCompileResult:
  """Resolve an extraction rule into a compiled extractor with
  retry on gate failure.

  Loop semantics:

  1. Build the resolution prompt
     (:func:`build_resolution_prompt`). Call
     ``llm_client.generate_json(prompt, schema)``.
  2. Parse the response. On
     :class:`PlanParseError`, build a parser diagnostic, wrap the
     prompt via :func:`build_retry_prompt`, increment the
     attempt counter, and loop.
  3. Render the parsed plan
     (:func:`render_extractor_source`). On ``ValueError``,
     synthesize a ``RenderError [code=invalid_plan]: <msg>``
     diagnostic and loop.
  4. Compile via *compile_source*. Inspect ``CompileResult.ok``;
     on failure, build a compile-result diagnostic
     (:func:`build_compile_result_diagnostic`) and loop.
  5. On success or after *max_attempts*, return a
     :class:`RetryCompileResult` with the per-attempt history.

  Args:
    extraction_rule: The user's intent for one event_type. Same
      shape as :func:`build_resolution_prompt`'s argument.
    event_schema: The event payload's typed structure. Same
      shape as :func:`build_resolution_prompt`'s argument.
    llm_client: Anything implementing the
      :class:`LLMClient` Protocol. Exceptions other than
      :class:`PlanParseError` (which originates in the parser,
      not the client) propagate unchanged — auth / transport /
      quota errors don't get silently retried.
    compile_source: Callable that turns ``(plan, source)`` into a
      :class:`CompileResult`. Production callers wrap
      :func:`compile_extractor`; tests pass a fake.
    max_attempts: Maximum number of LLM calls. Defaults to 5.
      ``max_attempts=1`` runs the loop once and returns whatever
      that single attempt produced (no retry). Values < 1 raise
      :class:`ValueError`.

  Returns:
    A :class:`RetryCompileResult`. ``ok`` iff the final attempt
    succeeded.

  Raises:
    ValueError: if ``max_attempts < 1``.
    Exception: anything the LLM client or *compile_source*
      raises (other than :class:`PlanParseError`, which the loop
      handles internally) propagates unchanged.
  """
  if max_attempts < 1:
    raise ValueError(
        f"max_attempts must be >= 1; got {max_attempts}. "
        f"max_attempts=1 means a single LLM call with no retry."
    )

  original_prompt = build_resolution_prompt(extraction_rule, event_schema)
  attempts: list[AttemptRecord] = []
  prompt = original_prompt

  for attempt_index in range(1, max_attempts + 1):
    is_last_attempt = attempt_index == max_attempts

    # 1. LLM call. Anything raised propagates — no silent retry.
    # Deep-copy the schema before handing it to the client. Some
    # provider adapters normalize schemas in place to work around
    # vendor quirks (Gemini's ``response_schema`` doesn't support
    # ``$schema`` / ``$defs``, etc.); without the copy, one
    # mutating client could corrupt the exported module global
    # for every later caller in the process. Mirrors the same
    # hardening in ``PlanResolver.resolve``.
    schema_copy = copy.deepcopy(RESOLVED_EXTRACTOR_PLAN_JSON_SCHEMA)
    raw_response = llm_client.generate_json(prompt, schema_copy)

    # 2. Parse. PlanParseError → retry with parser diagnostic.
    try:
      plan = parse_resolved_extractor_plan_json(raw_response)
    except PlanParseError as err:
      diagnostic = build_plan_parse_diagnostic(err)
      attempts.append(
          AttemptRecord(
              attempt=attempt_index,
              prompt=prompt,
              raw_response=raw_response,
              plan_parse_error=err,
              diagnostic=None if is_last_attempt else diagnostic,
          )
      )
      if is_last_attempt:
        break
      prompt = build_retry_prompt(
          original_prompt=original_prompt,
          prior_response=raw_response,
          diagnostic=diagnostic,
      )
      continue

    # 3. Render. ValueError → retry with synthesized
    #    "RenderError [code=invalid_plan]: ..." diagnostic.
    try:
      source = render_extractor_source(plan)
    except ValueError as err:
      render_msg = str(err)
      diagnostic = f"RenderError [code=invalid_plan]: {render_msg}"
      attempts.append(
          AttemptRecord(
              attempt=attempt_index,
              prompt=prompt,
              raw_response=raw_response,
              render_error=render_msg,
              diagnostic=None if is_last_attempt else diagnostic,
          )
      )
      if is_last_attempt:
        break
      prompt = build_retry_prompt(
          original_prompt=original_prompt,
          prior_response=raw_response,
          diagnostic=diagnostic,
      )
      continue

    # 4. Compile. CompileResult.ok decides retry.
    compile_result = compile_source(plan, source)
    if compile_result.ok:
      attempts.append(
          AttemptRecord(
              attempt=attempt_index,
              prompt=prompt,
              raw_response=raw_response,
              compile_result=compile_result,
              diagnostic=None,
          )
      )
      return RetryCompileResult(
          ok=True,
          manifest=compile_result.manifest,
          bundle_dir=compile_result.bundle_dir,
          attempts=tuple(attempts),
          reason=_REASON_SUCCEEDED,
      )

    diagnostic = build_compile_result_diagnostic(compile_result)
    attempts.append(
        AttemptRecord(
            attempt=attempt_index,
            prompt=prompt,
            raw_response=raw_response,
            compile_result=compile_result,
            diagnostic=None if is_last_attempt else diagnostic,
        )
    )
    if is_last_attempt:
      break
    prompt = build_retry_prompt(
        original_prompt=original_prompt,
        prior_response=raw_response,
        diagnostic=diagnostic,
    )

  return RetryCompileResult(
      ok=False,
      manifest=None,
      bundle_dir=None,
      attempts=tuple(attempts),
      reason=_REASON_MAX_ATTEMPTS,
  )
