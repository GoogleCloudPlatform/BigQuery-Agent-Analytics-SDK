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

"""Compile-and-compare measurement utility (PR 4c of issue #75).

Wraps :func:`compile_with_llm` with a parity check against a known-
good *reference* extractor. The first concrete consumer is the
BKA-decision compile (``extract_bka_decision_event``); the utility
itself is generic so future Phase-C extractor baselines can reuse
it without re-implementing the parity logic.

Two distinct concerns this module handles:

* **Compile loop outcome.** ``ok``, ``n_attempts``, ``reason``,
  bundle fingerprint, per-attempt failure codes — captured directly
  from :class:`RetryCompileResult` so a failed loop produces a
  structured measurement instead of an exception. Useful both for
  CI assertions and for analyzing why a particular rule + schema
  pair takes N attempts to converge.
* **Behavioral parity.** Both the compiled extractor and the
  reference extractor are run on the same sample events; their
  ``StructuredExtractionResult`` outputs are compared field-by-
  field. Divergences (different node IDs, missing properties,
  span-handling-set mismatch) are surfaced as human-readable
  strings the caller can log or assert on.

The module is the merge-blocking surface; live BigQuery + LLM
runs are gated test-side and feed their measurements into the same
:class:`CompileMeasurement` shape.
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import pathlib
from typing import Any, Callable, Optional
import uuid

from .compiler import CompileResult
from .manifest import Manifest
from .plan_resolver import LLMClient
from .retry_loop import AttemptRecord
from .retry_loop import compile_with_llm
from .retry_loop import CompileSource
from .retry_loop import RetryCompileResult
from .smoke_test import load_callable_from_source

# Public default for cases where the caller doesn't know the
# model name (deterministic test fakes, in-process clients).
DETERMINISTIC_FAKE_MODEL = "deterministic-fake"


@dataclasses.dataclass(frozen=True)
class CompileMeasurement:
  """One run's measurement record.

  Designed to be JSON-serializable end-to-end so a live run can
  write its output as a checked-in artifact and a deterministic
  test can compare a freshly-built record against an expected
  schema.

  Fields:

  * ``ok`` — True iff the compile loop succeeded *and* the
    compiled extractor's output matched the reference on every
    sample event. A loop-success-but-parity-failure case sets
    ``ok=False`` and populates ``parity_divergences``.
  * ``n_attempts`` — How many LLM attempts the loop made (1
    iff the first plan compiled clean).
  * ``reason`` — ``"succeeded"`` or ``"max_attempts_reached"``
    from the underlying :class:`RetryCompileResult`.
  * ``bundle_fingerprint`` — sha256 fingerprint from the bundle's
    manifest, ``None`` if the loop didn't reach a successful
    compile.
  * ``attempt_failures`` — short stable codes (one per failed
    attempt) like ``"plan_parse_error:missing_required_field"`` /
    ``"compile:invalid_event_types"`` / ``"render_error"`` so
    failure-mode analysis doesn't have to traverse the full
    AttemptRecord tuple.
  * ``parity_ok`` — True iff the compiled extractor and the
    reference produced the same output on every sample event
    (node-set / property-set / span-handling-set equality).
  * ``n_events`` — Total sample events compared.
  * ``n_events_with_node_match`` / ``n_events_with_span_match`` —
    Per-axis match counts. A future divergence in just one axis
    is easier to triage with these split out.
  * ``parity_divergences`` — Human-readable per-event divergence
    strings; empty iff ``parity_ok``.
  * ``captured_at`` — UTC ISO timestamp of the run.
  * ``model_name`` — Identifier of the LLM that produced the
    plan. ``"deterministic-fake"`` for in-test runs;
    a model string like ``"gemini-2.5-flash"`` for live runs.
  * ``source`` — Audit string identifying the data source,
    e.g. ``"deterministic"`` / ``"live:project.dataset.table"``.
  * ``sample_session_ids`` — ``session_id`` values present in
    the sample events. Lets a reviewer of a live run trace
    measurements back to actual session traces in BigQuery.
  """

  ok: bool
  n_attempts: int
  reason: str
  bundle_fingerprint: Optional[str]
  attempt_failures: tuple[str, ...]
  parity_ok: bool
  n_events: int
  n_events_with_node_match: int
  n_events_with_span_match: int
  parity_divergences: tuple[str, ...]
  captured_at: str
  model_name: str
  source: str
  sample_session_ids: tuple[str, ...]

  def to_json(self) -> str:
    """Serialize to a stable, sorted JSON string. Suitable for
    writing as a checked-in measurement artifact and diffing
    across runs."""
    return json.dumps(dataclasses.asdict(self), sort_keys=True, indent=2)

  @classmethod
  def from_json(cls, payload: str) -> "CompileMeasurement":
    """Inverse of :meth:`to_json`. Round-trip is byte-stable for
    the same input.

    The artifact contract is **exact-keys**: the JSON object's
    top-level keys must equal the dataclass's field set.
    Unknown fields (a stale or accidental extra key) fail with a
    :class:`TypeError` listing the unknown names; missing fields
    fail with a :class:`TypeError` listing the missing names.
    A schema-lock that silently ignored unknown keys would let
    the artifact accumulate dead fields review couldn't see.

    Type-strict: every field is also checked against its declared
    Python type. ``bool``, ``int``, ``str``, ``list``, and
    ``None`` are accepted only when the JSON literal matches —
    no constructor-style coercion (``bool("false") == True`` /
    ``tuple("abc") == ("a", "b", "c")``) is allowed. Each
    failure raises :class:`TypeError` with the offending field
    name; a malformed artifact fails loudly at parse time
    rather than silently shifting under a future test
    assertion.
    """
    data = json.loads(payload)
    if not isinstance(data, dict):
      raise TypeError(
          f"CompileMeasurement payload must be a JSON object; got "
          f"{type(data).__name__}"
      )
    allowed = {f.name for f in dataclasses.fields(cls)}
    keys = set(data)
    if keys != allowed:
      missing = sorted(allowed - keys)
      extra = sorted(keys - allowed)
      parts: list[str] = []
      if missing:
        parts.append(f"missing fields: {missing}")
      if extra:
        parts.append(f"unknown fields: {extra}")
      raise TypeError(
          "CompileMeasurement payload schema mismatch — " + "; ".join(parts)
      )
    return cls(
        ok=_require_bool(data, "ok"),
        n_attempts=_require_int(data, "n_attempts"),
        reason=_require_str(data, "reason"),
        bundle_fingerprint=_require_optional_str(data, "bundle_fingerprint"),
        attempt_failures=_require_tuple_of_str(data, "attempt_failures"),
        parity_ok=_require_bool(data, "parity_ok"),
        n_events=_require_int(data, "n_events"),
        n_events_with_node_match=_require_int(data, "n_events_with_node_match"),
        n_events_with_span_match=_require_int(data, "n_events_with_span_match"),
        parity_divergences=_require_tuple_of_str(data, "parity_divergences"),
        captured_at=_require_str(data, "captured_at"),
        model_name=_require_str(data, "model_name"),
        source=_require_str(data, "source"),
        sample_session_ids=_require_tuple_of_str(data, "sample_session_ids"),
    )


def measure_compile(
    *,
    extraction_rule: dict,
    event_schema: dict,
    sample_events: list[dict],
    reference_extractor: Callable,
    spec: Any,
    llm_client: LLMClient,
    compile_source: CompileSource,
    max_attempts: int = 5,
    model_name: str = DETERMINISTIC_FAKE_MODEL,
    source: str = "deterministic",
    captured_at: Optional[str] = None,
) -> CompileMeasurement:
  """Run :func:`compile_with_llm`; on success, load the compiled
  callable and check parity against *reference_extractor*.

  Args:
    extraction_rule: The user's intent for the target event_type
      (forwarded to ``compile_with_llm``).
    event_schema: The event payload's typed structure
      (forwarded).
    sample_events: Events both extractors are run against. Same
      events the loop's smoke gate uses, so a loop-success
      guarantees the compiled extractor doesn't crash on any of
      them.
    reference_extractor: Known-good extractor for parity. The
      first concrete consumer is
      :func:`extract_bka_decision_event`; future baselines plug
      in their own.
    spec: Forwarded to both extractors. Treated as opaque.
    llm_client: Same protocol :func:`compile_with_llm` accepts.
    compile_source: The ``(plan, source) -> CompileResult``
      callable; closes over the per-call compile inputs (parent
      bundle dir, fingerprint inputs, etc.).
    max_attempts: Max LLM attempts (forwarded).
    model_name: Recorded in the measurement for auditability.
      Defaults to ``"deterministic-fake"``; live runs should pass
      the actual model identifier.
    source: Audit string identifying where ``sample_events`` came
      from (``"deterministic"`` / ``"live:..."``).
    captured_at: Override the timestamp (mostly for reproducible
      tests). Defaults to ``datetime.datetime.now(timezone.utc)``
      ISO string.

  Returns:
    A populated :class:`CompileMeasurement`. Returned even when
    the compile loop fails — callers inspect ``ok`` /
    ``attempt_failures`` to distinguish.
  """
  compile_result: RetryCompileResult = compile_with_llm(
      extraction_rule=extraction_rule,
      event_schema=event_schema,
      llm_client=llm_client,
      compile_source=compile_source,
      max_attempts=max_attempts,
  )

  attempt_failures = tuple(
      _attempt_failure_code(record)
      for record in compile_result.attempts
      if not _attempt_succeeded(record)
  )

  bundle_fingerprint: Optional[str] = None
  if compile_result.manifest is not None:
    bundle_fingerprint = compile_result.manifest.fingerprint

  session_ids = tuple(_extract_session_ids(sample_events))
  ts = captured_at or _now_iso_utc()

  if not compile_result.ok or compile_result.bundle_dir is None:
    # Loop failed; no compiled extractor to compare. Parity is
    # vacuously False; all per-event match counts are 0.
    return CompileMeasurement(
        ok=False,
        n_attempts=len(compile_result.attempts),
        reason=compile_result.reason,
        bundle_fingerprint=bundle_fingerprint,
        attempt_failures=attempt_failures,
        parity_ok=False,
        n_events=len(sample_events),
        n_events_with_node_match=0,
        n_events_with_span_match=0,
        parity_divergences=(),
        captured_at=ts,
        model_name=model_name,
        source=source,
        sample_session_ids=session_ids,
    )

  compiled_extractor = _load_compiled_extractor(
      bundle_dir=compile_result.bundle_dir,
      manifest=compile_result.manifest,
  )

  parity = _compare_extractors(
      reference=reference_extractor,
      compiled=compiled_extractor,
      events=sample_events,
      spec=spec,
  )

  overall_ok = compile_result.ok and parity.ok

  return CompileMeasurement(
      ok=overall_ok,
      n_attempts=len(compile_result.attempts),
      reason=compile_result.reason,
      bundle_fingerprint=bundle_fingerprint,
      attempt_failures=attempt_failures,
      parity_ok=parity.ok,
      n_events=len(sample_events),
      n_events_with_node_match=parity.n_events_with_node_match,
      n_events_with_span_match=parity.n_events_with_span_match,
      parity_divergences=parity.divergences,
      captured_at=ts,
      model_name=model_name,
      source=source,
      sample_session_ids=session_ids,
  )


# ------------------------------------------------------------------ #
# Helpers — kept module-private; tests cover them through            #
# ``measure_compile`` rather than re-binding internals.              #
# ------------------------------------------------------------------ #


@dataclasses.dataclass(frozen=True)
class _ParityResult:
  ok: bool
  n_events_with_node_match: int
  n_events_with_span_match: int
  divergences: tuple[str, ...]


def _attempt_succeeded(record: AttemptRecord) -> bool:
  """An attempt is the *successful* terminal one iff its
  ``compile_result`` is populated and ok, with no other failure
  channel set. Mirrors the AttemptRecord docstring's contract."""
  if record.plan_parse_error is not None:
    return False
  if record.render_error is not None:
    return False
  if record.compile_result is None:
    return False
  return record.compile_result.ok


def _attempt_failure_code(record: AttemptRecord) -> str:
  """Short, stable code for one failed attempt. The two-segment
  ``stage:detail`` shape is friendly to log-aggregation tooling
  (group-by-prefix gives stage-level histograms)."""
  if record.plan_parse_error is not None:
    return f"plan_parse_error:{record.plan_parse_error.code}"
  if record.render_error is not None:
    return "render_error"
  cr = record.compile_result
  if cr is None:
    return "unknown"
  if cr.invalid_identifier is not None:
    return "compile:invalid_identifier"
  if cr.invalid_event_types is not None:
    return "compile:invalid_event_types"
  if not cr.ast_report.ok:
    return "compile:ast_failure"
  if cr.load_error is not None:
    return "compile:load_error"
  if cr.smoke_report is not None and not cr.smoke_report.ok:
    return "compile:smoke_failure"
  return "compile:unknown"


def _extract_session_ids(events: list[dict]) -> list[str]:
  """Collect ``session_id`` values from the events, in iteration
  order, deduplicated. Empty / missing values are skipped — the
  audit field's job is to *trace back* to real sessions, and an
  empty string isn't traceable."""
  seen: set[str] = set()
  out: list[str] = []
  for event in events:
    sid = event.get("session_id") if isinstance(event, dict) else None
    if isinstance(sid, str) and sid and sid not in seen:
      seen.add(sid)
      out.append(sid)
  return out


def _now_iso_utc() -> str:
  return (
      datetime.datetime.now(datetime.timezone.utc)
      .replace(microsecond=0)
      .isoformat()
      .replace("+00:00", "Z")
  )


def _load_compiled_extractor(
    *,
    bundle_dir: pathlib.Path,
    manifest: Manifest,
) -> Callable:
  """Import the compiled module from the bundle and return its
  extractor callable. Each call uses a unique ``module_name`` so
  ``sys.modules`` doesn't recycle a stale entry from an earlier
  measurement run in the same process."""
  source_path = bundle_dir / manifest.module_filename
  unique_name = (
      f"{manifest.fingerprint[:12]}__"
      f"{manifest.module_filename[:-3]}__"
      f"measure_{uuid.uuid4().hex[:8]}"
  )
  return load_callable_from_source(
      source_path,
      module_name=unique_name,
      function_name=manifest.function_name,
  )


def _compare_extractors(
    *,
    reference: Callable,
    compiled: Callable,
    events: list[dict],
    spec: Any,
) -> _ParityResult:
  """Run both extractors on every event; collect per-event
  divergences with stable, human-readable strings.

  Both extractor calls are wrapped in ``try/except Exception`` —
  if either raises on a given event, that event is recorded as a
  divergence (``event[i]: reference extractor raised X: msg`` /
  ``event[i]: compiled extractor raised X: msg``) and neither
  axis counts as a match. The contract is that
  :func:`measure_compile` always returns a populated
  :class:`CompileMeasurement`; if a callable crash escaped, the
  utility would raise and break that contract.

  ``Exception`` (not ``BaseException``) is the catch boundary so
  ``KeyboardInterrupt`` / ``SystemExit`` still propagate.

  Edges aren't compared because the renderer doesn't emit edges
  (per the renderer docstring); future renderer extensions will
  need to extend this comparator alongside the new emit-edges
  rules.
  """
  divergences: list[str] = []
  n_node_match = 0
  n_span_match = 0

  for index, event in enumerate(events):
    try:
      ref_result = reference(event, spec)
    except Exception as exc:  # noqa: BLE001 — record + continue
      divergences.append(
          f"event[{index}]: reference extractor raised "
          f"{type(exc).__name__}: {exc}"
      )
      # Reference output isn't available so neither axis can be
      # checked for this event; leave the counters as-is and move
      # to the next event.
      continue

    try:
      cmp_result = compiled(event, spec)
    except Exception as exc:  # noqa: BLE001 — record + continue
      divergences.append(
          f"event[{index}]: compiled extractor raised "
          f"{type(exc).__name__}: {exc}"
      )
      continue

    node_divergence = _compare_nodes(ref_result.nodes, cmp_result.nodes)
    if node_divergence is None:
      n_node_match += 1
    else:
      divergences.append(f"event[{index}]: {node_divergence}")

    span_divergence = _compare_span_handling(ref_result, cmp_result)
    if span_divergence is None:
      n_span_match += 1
    else:
      divergences.append(f"event[{index}]: {span_divergence}")

  return _ParityResult(
      ok=not divergences,
      n_events_with_node_match=n_node_match,
      n_events_with_span_match=n_span_match,
      divergences=tuple(divergences),
  )


def _compare_nodes(ref_nodes, cmp_nodes) -> Optional[str]:
  """Return a divergence string or ``None`` if the node sets
  match.

  Match criterion: same set of ``node_id`` values, and for each
  shared ``node_id`` the ``entity_name``, sorted ``labels``, and
  property ``(name, value)`` set are equal.
  """
  ref_by_id = {n.node_id: n for n in ref_nodes}
  cmp_by_id = {n.node_id: n for n in cmp_nodes}
  if set(ref_by_id) != set(cmp_by_id):
    only_ref = sorted(set(ref_by_id) - set(cmp_by_id))
    only_cmp = sorted(set(cmp_by_id) - set(ref_by_id))
    parts = []
    if only_ref:
      parts.append(f"only in reference: {only_ref}")
    if only_cmp:
      parts.append(f"only in compiled: {only_cmp}")
    return "node_id mismatch — " + "; ".join(parts)
  for node_id in sorted(ref_by_id):
    ref_node = ref_by_id[node_id]
    cmp_node = cmp_by_id[node_id]
    if ref_node.entity_name != cmp_node.entity_name:
      return (
          f"node {node_id!r} entity_name "
          f"{ref_node.entity_name!r} (ref) != "
          f"{cmp_node.entity_name!r} (compiled)"
      )
    if sorted(ref_node.labels) != sorted(cmp_node.labels):
      return (
          f"node {node_id!r} labels "
          f"{sorted(ref_node.labels)!r} (ref) != "
          f"{sorted(cmp_node.labels)!r} (compiled)"
      )
    ref_props = {(p.name, _hashable(p.value)) for p in ref_node.properties}
    cmp_props = {(p.name, _hashable(p.value)) for p in cmp_node.properties}
    if ref_props != cmp_props:
      only_ref = sorted(ref_props - cmp_props)
      only_cmp = sorted(cmp_props - ref_props)
      bits = []
      if only_ref:
        bits.append(f"only in reference: {only_ref}")
      if only_cmp:
        bits.append(f"only in compiled: {only_cmp}")
      return f"node {node_id!r} property set mismatch — " + "; ".join(bits)
  return None


def _compare_span_handling(ref_result, cmp_result) -> Optional[str]:
  """Return a divergence string or ``None`` when both span sets
  match. Sets must be equal by element membership; ordering
  isn't meaningful (the underlying type is ``set``)."""
  ref_full = set(ref_result.fully_handled_span_ids)
  cmp_full = set(cmp_result.fully_handled_span_ids)
  if ref_full != cmp_full:
    return (
        f"fully_handled_span_ids {sorted(ref_full)!r} (ref) != "
        f"{sorted(cmp_full)!r} (compiled)"
    )
  ref_partial = set(ref_result.partially_handled_span_ids)
  cmp_partial = set(cmp_result.partially_handled_span_ids)
  if ref_partial != cmp_partial:
    return (
        f"partially_handled_span_ids {sorted(ref_partial)!r} (ref) != "
        f"{sorted(cmp_partial)!r} (compiled)"
    )
  return None


def _hashable(value: Any):
  """Coerce an extracted property's value into something
  hashable for set comparison. Lists / dicts are turned into
  ``repr`` strings — order-sensitive but deterministic; that's
  acceptable for parity checks where we expect byte-equal
  outputs from two extractors."""
  try:
    hash(value)
    return value
  except TypeError:
    return repr(value)


# ------------------------------------------------------------------ #
# Strict-type field readers for CompileMeasurement.from_json         #
# ------------------------------------------------------------------ #
#
# Constructor-style coercion (``bool("false") == True``,
# ``int("3") == 3``, ``tuple("abc") == ("a", "b", "c")``) silently
# turns malformed JSON into well-typed-but-wrong Python values, so
# the artifact's "schema lock" promise has to be enforced manually.
# Each helper takes ``(data, field_name)`` and either returns the
# correctly-typed value or raises a TypeError naming the field.


def _require_bool(data: dict, field: str) -> bool:
  value = data.get(field)
  # ``bool`` is a subclass of ``int`` in Python; the strict check
  # is ``type(value) is bool`` so ``isinstance(0, bool) is False``
  # but ``isinstance(False, int) is True`` doesn't sneak past us.
  if type(value) is not bool:
    raise TypeError(
        f"CompileMeasurement field {field!r} must be bool; got "
        f"{type(value).__name__}={value!r}"
    )
  return value


def _require_int(data: dict, field: str) -> int:
  value = data.get(field)
  if type(value) is not int:
    raise TypeError(
        f"CompileMeasurement field {field!r} must be int; got "
        f"{type(value).__name__}={value!r}"
    )
  return value


def _require_str(data: dict, field: str) -> str:
  value = data.get(field)
  if not isinstance(value, str):
    raise TypeError(
        f"CompileMeasurement field {field!r} must be str; got "
        f"{type(value).__name__}={value!r}"
    )
  return value


def _require_optional_str(data: dict, field: str) -> Optional[str]:
  value = data.get(field)
  if value is None:
    return None
  if not isinstance(value, str):
    raise TypeError(
        f"CompileMeasurement field {field!r} must be str or null; "
        f"got {type(value).__name__}={value!r}"
    )
  return value


def _require_tuple_of_str(data: dict, field: str) -> tuple[str, ...]:
  """Reads a JSON array of strings.

  ``tuple(...)`` over a string would silently return a tuple of
  characters; ``tuple(...)`` over a dict would silently return a
  tuple of keys. Both are bugs the artifact-schema lock has to
  reject — the explicit ``isinstance(value, list)`` check is what
  makes the lock load-bearing.
  """
  value = data.get(field)
  if not isinstance(value, list):
    raise TypeError(
        f"CompileMeasurement field {field!r} must be a JSON array; "
        f"got {type(value).__name__}={value!r}"
    )
  for index, item in enumerate(value):
    if not isinstance(item, str):
      raise TypeError(
          f"CompileMeasurement field {field!r}[{index}] must be str; "
          f"got {type(item).__name__}={item!r}"
      )
  return tuple(value)
