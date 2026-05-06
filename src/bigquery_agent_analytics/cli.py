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

"""CLI entry point for BigQuery Agent Analytics SDK.

Usage::

    bq-agent-sdk doctor --project-id=P --dataset-id=D
    bq-agent-sdk get-trace --project-id=P --dataset-id=D --session-id=S
    bq-agent-sdk evaluate --project-id=P --dataset-id=D --evaluator=latency
    bq-agent-sdk insights --project-id=P --dataset-id=D
    bq-agent-sdk drift --project-id=P --dataset-id=D --golden-dataset=G
    bq-agent-sdk distribution --project-id=P --dataset-id=D
    bq-agent-sdk hitl-metrics --project-id=P --dataset-id=D
    bq-agent-sdk list-traces --project-id=P --dataset-id=D
    bq-agent-sdk categorical-eval --project-id=P --dataset-id=D --metrics-file=M
    bq-agent-sdk categorical-views --project-id=P --dataset-id=D
    bq-agent-sdk views create-all --project-id=P --dataset-id=D
    bq-agent-sdk views create --project-id=P --dataset-id=D EVENT_TYPE
"""

from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import Optional

import typer

from .evaluators import CodeEvaluator
from .evaluators import EvaluationReport
from .evaluators import LLMAsJudge
from .formatter import format_output
from .trace import TraceFilter

app = typer.Typer(
    name="bq-agent-sdk",
    help="BigQuery Agent Analytics SDK CLI.",
    add_completion=False,
)


# ------------------------------------------------------------------ #
# Shared options                                                       #
# ------------------------------------------------------------------ #

_PROJECT_HELP = "GCP project ID. [env: BQ_AGENT_PROJECT]"
_DATASET_HELP = "BigQuery dataset. [env: BQ_AGENT_DATASET]"


def _build_client(
    project_id: str,
    dataset_id: str,
    table_id: str,
    location: Optional[str] = None,
    endpoint: Optional[str] = None,
    connection_id: Optional[str] = None,
):
  """Lazily import Client and construct an instance."""
  from .client import Client  # defer heavy import

  return Client(
      project_id=project_id,
      dataset_id=dataset_id,
      table_id=table_id,
      location=location,
      verify_schema=False,
      endpoint=endpoint,
      connection_id=connection_id,
      sdk_surface="cli",
  )


def _run_binding_preflight(
    *,
    ontology_path: str | None,
    binding_path: str | None,
    project_id: str,
    location: str | None,
    strict: bool,
) -> None:
  """Run the binding-vs-BQ pre-flight validator.

  On any failure (default mode) or any failure including escalated
  warnings (strict mode), prints a structured report to stderr and
  raises ``typer.Exit(code=1)`` so extraction never starts.

  Advisory warnings in default mode print to stderr but do not flip
  the exit code — they are informational, not blocking.
  """
  if ontology_path is None or binding_path is None:
    raise typer.BadParameter(
        "Binding validation requires --ontology PATH and " "--binding PATH."
    )

  from google.cloud import bigquery

  from bigquery_ontology import load_binding
  from bigquery_ontology import load_ontology

  from .binding_validation import validate_binding_against_bigquery

  ontology = load_ontology(ontology_path)
  binding = load_binding(binding_path, ontology=ontology)
  bq_client = bigquery.Client(project=project_id, location=location)

  report = validate_binding_against_bigquery(
      ontology=ontology,
      binding=binding,
      bq_client=bq_client,
      strict=strict,
  )

  for w in report.warnings:
    typer.echo(
        f"WARN: {w.code.value} at {w.binding_path} "
        f"({w.bq_ref}): {w.detail}",
        err=True,
    )

  if not report.ok:
    typer.echo(
        f"Error: binding validation failed "
        f"({len(report.failures)} failure(s)).",
        err=True,
    )
    for f in report.failures:
      typer.echo(
          f"  {f.code.value} at {f.binding_path} ({f.bq_ref}): " f"{f.detail}",
          err=True,
      )
    raise typer.Exit(code=1)


def _load_spec_from_args(
    spec_path: str | None,
    ontology_path: str | None,
    binding_path: str | None,
    env: str | None,
) -> "ResolvedGraph":
  """Load a ResolvedGraph from either separated or combined YAML.

  Separated (--ontology + --binding) is the primary path.
  Combined (--spec-path) is a deprecated fallback.
  """
  from .resolved_spec import ResolvedGraph

  has_any_separated = ontology_path is not None or binding_path is not None
  has_combined = spec_path is not None

  # Reject any mixing of separated and combined flags.
  if has_any_separated and has_combined:
    raise typer.BadParameter(
        "Cannot mix --ontology/--binding with --spec-path. "
        "Use --ontology PATH --binding PATH (preferred) or "
        "--spec-path PATH (deprecated), not both."
    )

  if has_any_separated:
    # Both must be present together.
    if ontology_path is None or binding_path is None:
      raise typer.BadParameter(
          "--ontology and --binding must be used together."
      )
    # --env is not supported with separated inputs.
    if env is not None:
      raise typer.BadParameter(
          "--env is only supported with --spec-path (deprecated). "
          "Separated ontology/binding YAML does not use {{ env }} "
          "placeholders."
      )
    from bigquery_ontology import load_binding
    from bigquery_ontology import load_ontology

    from .resolved_spec import resolve

    ontology = load_ontology(ontology_path)
    binding = load_binding(binding_path, ontology=ontology)
    return resolve(ontology, binding)

  if has_combined:
    import warnings

    warnings.warn(
        "--spec-path is deprecated. "
        "Use --ontology PATH --binding PATH instead.",
        DeprecationWarning,
        stacklevel=3,
    )
    from .resolved_spec import load_resolved_graph

    return load_resolved_graph(spec_path, env=env)

  raise typer.BadParameter(
      "Provide either --ontology PATH --binding PATH (preferred) "
      "or --spec-path PATH (deprecated)."
  )


# ------------------------------------------------------------------ #
# Evaluator factories                                                  #
# ------------------------------------------------------------------ #

_CODE_EVALUATORS = {
    "latency": (
        lambda t: CodeEvaluator.latency(threshold_ms=t),
        lambda: CodeEvaluator.latency(),
    ),
    "error_rate": (
        lambda t: CodeEvaluator.error_rate(max_error_rate=t),
        lambda: CodeEvaluator.error_rate(),
    ),
    "turn_count": (
        lambda t: CodeEvaluator.turn_count(max_turns=int(t)),
        lambda: CodeEvaluator.turn_count(),
    ),
    "token_efficiency": (
        lambda t: CodeEvaluator.token_efficiency(max_tokens=int(t)),
        lambda: CodeEvaluator.token_efficiency(),
    ),
    "ttft": (
        lambda t: CodeEvaluator.ttft(threshold_ms=t),
        lambda: CodeEvaluator.ttft(),
    ),
    "cost": (
        lambda t: CodeEvaluator.cost_per_session(max_cost_usd=t),
        lambda: CodeEvaluator.cost_per_session(),
    ),
}

_LLM_JUDGES = {
    "correctness": (
        lambda t: LLMAsJudge.correctness(threshold=t),
        lambda: LLMAsJudge.correctness(),
    ),
    "hallucination": (
        lambda t: LLMAsJudge.hallucination(threshold=t),
        lambda: LLMAsJudge.hallucination(),
    ),
    "sentiment": (
        lambda t: LLMAsJudge.sentiment(threshold=t),
        lambda: LLMAsJudge.sentiment(),
    ),
}


# ------------------------------------------------------------------ #
# Commands                                                             #
# ------------------------------------------------------------------ #


@app.command()
def doctor(
    project_id: str = typer.Option(
        ..., envvar="BQ_AGENT_PROJECT", help=_PROJECT_HELP
    ),
    dataset_id: str = typer.Option(
        ..., envvar="BQ_AGENT_DATASET", help=_DATASET_HELP
    ),
    table_id: str = typer.Option("agent_events", help="Events table name."),
    location: Optional[str] = typer.Option(None, help="BQ location."),
    fmt: str = typer.Option(
        "json",
        "--format",
        help="Output format: json|text|table.",
    ),
) -> None:
  """Run diagnostic health check."""
  try:
    client = _build_client(project_id, dataset_id, table_id, location)
    result = client.doctor()
    typer.echo(format_output(result, fmt))
  except Exception as exc:
    typer.echo(f"Error: {exc}", err=True)
    raise typer.Exit(code=2)


@app.command("get-trace")
def get_trace(
    project_id: str = typer.Option(
        ..., envvar="BQ_AGENT_PROJECT", help=_PROJECT_HELP
    ),
    dataset_id: str = typer.Option(
        ..., envvar="BQ_AGENT_DATASET", help=_DATASET_HELP
    ),
    table_id: str = typer.Option("agent_events", help="Events table name."),
    location: Optional[str] = typer.Option(None, help="BQ location."),
    session_id: Optional[str] = typer.Option(
        None, help="Retrieve by session ID."
    ),
    trace_id: Optional[str] = typer.Option(None, help="Retrieve by trace ID."),
    fmt: str = typer.Option(
        "json",
        "--format",
        help="Output format: json|text|table.",
    ),
) -> None:
  """Retrieve and display a session trace."""
  if not session_id and not trace_id:
    typer.echo(
        "Error: provide --session-id or --trace-id.",
        err=True,
    )
    raise typer.Exit(code=2)
  try:
    client = _build_client(project_id, dataset_id, table_id, location)
    if session_id:
      trace = client.get_session_trace(session_id)
    else:
      trace = client.get_trace(trace_id)
    typer.echo(format_output(trace, fmt))
  except Exception as exc:
    typer.echo(f"Error: {exc}", err=True)
    raise typer.Exit(code=2)


@app.command()
def evaluate(
    project_id: str = typer.Option(
        ..., envvar="BQ_AGENT_PROJECT", help=_PROJECT_HELP
    ),
    dataset_id: str = typer.Option(
        ..., envvar="BQ_AGENT_DATASET", help=_DATASET_HELP
    ),
    table_id: str = typer.Option("agent_events", help="Events table name."),
    location: Optional[str] = typer.Option(None, help="BQ location."),
    evaluator: str = typer.Option(
        "latency",
        help=(
            "Evaluator: latency|error_rate|turn_count|"
            "token_efficiency|context_cache_hit_rate|"
            "ttft|cost|llm-judge."
        ),
    ),
    threshold: Optional[float] = typer.Option(
        None, help="Pass/fail threshold (uses evaluator default if omitted)."
    ),
    criterion: str = typer.Option(
        "correctness",
        help=("LLM judge criterion: " "correctness|hallucination|sentiment."),
    ),
    agent_id: Optional[str] = typer.Option(None, help="Filter by agent name."),
    last: Optional[str] = typer.Option(
        None,
        help="Time window: 30m, 1h, 24h, 7d, 30d.",
    ),
    limit: int = typer.Option(100, help="Max sessions to evaluate."),
    exit_code: bool = typer.Option(
        False,
        "--exit-code",
        help="Return exit code 1 on evaluation failure.",
    ),
    fail_on_missing_cache_telemetry: bool = typer.Option(
        False,
        "--fail-on-missing-cache-telemetry",
        help=(
            "For context_cache_hit_rate, fail sessions with input tokens"
            " but no cache telemetry."
        ),
    ),
    strict: bool = typer.Option(
        False,
        help=(
            "Stamp parse-error metadata on AI.GENERATE judge rows with"
            " empty or NULL typed output. Those rows already fail"
            " (empty score < threshold); --strict adds"
            " details['parse_error']=True and a report-level"
            " parse_errors counter so dashboards can tell 'no"
            " parseable score' apart from 'low score' failures."
        ),
    ),
    endpoint: Optional[str] = typer.Option(
        None,
        help="AI.GENERATE endpoint for LLM judge.",
    ),
    connection_id: Optional[str] = typer.Option(
        None,
        help="BQ connection ID for AI.GENERATE.",
    ),
    fmt: str = typer.Option(
        "json",
        "--format",
        help="Output format: json|text|table.",
    ),
) -> None:
  """Run code-based or LLM evaluation over traces."""
  try:
    if (
        fail_on_missing_cache_telemetry
        and evaluator != "context_cache_hit_rate"
    ):
      typer.echo(
          "Error: --fail-on-missing-cache-telemetry only applies to "
          "context_cache_hit_rate.",
          err=True,
      )
      raise typer.Exit(code=2)

    filters = TraceFilter.from_cli_args(
        last=last,
        agent_id=agent_id,
        limit=limit,
    )

    if evaluator == "llm-judge":
      entry = _LLM_JUDGES.get(criterion)
      if not entry:
        typer.echo(
            f"Error: unknown criterion: {criterion!r}.",
            err=True,
        )
        raise typer.Exit(code=2)
      with_t, without_t = entry
      ev = with_t(threshold) if threshold is not None else without_t()
    elif evaluator == "context_cache_hit_rate":
      kwargs = {
          "fail_on_missing_telemetry": fail_on_missing_cache_telemetry,
      }
      if threshold is not None:
        kwargs["min_hit_rate"] = threshold
      ev = CodeEvaluator.context_cache_hit_rate(**kwargs)
    else:
      entry = _CODE_EVALUATORS.get(evaluator)
      if not entry:
        typer.echo(
            f"Error: unknown evaluator: {evaluator!r}.",
            err=True,
        )
        raise typer.Exit(code=2)
      with_t, without_t = entry
      ev = with_t(threshold) if threshold is not None else without_t()

    client = _build_client(
        project_id,
        dataset_id,
        table_id,
        location,
        endpoint=endpoint,
        connection_id=connection_id,
    )
    report = client.evaluate(evaluator=ev, filters=filters, strict=strict)
    typer.echo(format_output(report, fmt))

    if exit_code and report.pass_rate < 1.0:
      _emit_evaluate_failures(report)
      raise typer.Exit(code=1)
  except typer.Exit:
    raise
  except Exception as exc:
    typer.echo(f"Error: {exc}", err=True)
    raise typer.Exit(code=2)


_FEEDBACK_SNIPPET_MAX = 120


def _format_feedback_snippet(
    feedback: Optional[str], max_chars: int = _FEEDBACK_SNIPPET_MAX
) -> Optional[str]:
  """Return a single-line, bounded snippet of an LLM-judge justification.

  Collapses internal whitespace runs (including newlines) to a single
  space so the snippet fits on one CI log line, then truncates to
  ``max_chars`` with a trailing ``…`` when the original was longer.
  Returns ``None`` for empty / whitespace-only input so callers can
  cleanly skip the field.
  """
  if not feedback:
    return None
  collapsed = " ".join(feedback.split())
  if not collapsed:
    return None
  if len(collapsed) <= max_chars:
    return collapsed
  # Reserve one char for the ellipsis to keep the visual width capped.
  return collapsed[: max_chars - 1].rstrip() + "\u2026"


def _emit_evaluate_failures(
    report: EvaluationReport, max_sessions: int = 10
) -> None:
  """Emit readable FAIL lines for failing sessions before --exit-code exits.

  One line per (session_id, metric_name) that failed its threshold.
  Prefers the raw observed + budget pair (``CodeEvaluator`` prebuilts);
  falls back to score + threshold when the metric didn't declare
  observed/budget (custom ``add_metric`` users, ``LLMAsJudge``
  criteria). For LLM-judge failures the line also carries a bounded
  ``feedback="…"`` snippet drawn from ``SessionScore.llm_feedback``
  so CI logs explain *why* the judge said the session failed without
  forcing the reader to chase the JSON output.

  A failing session is guaranteed to produce at least one FAIL line —
  never just the summary header. Capped at ``max_sessions`` most-recent
  failures so CI logs stay scannable.
  """
  failed = [s for s in report.session_scores if not s.passed]
  if not failed:
    return
  typer.echo("", err=True)
  typer.echo(
      f"--exit-code: {len(failed)} session(s) failed (of "
      f"{report.total_sessions} evaluated)",
      err=True,
  )
  shown = failed[:max_sessions]
  for s in shown:
    feedback_snippet = _format_feedback_snippet(s.llm_feedback)
    emitted_for_session = False
    for metric_name, score in s.scores.items():
      detail = s.details.get(f"metric_{metric_name}") or {}
      passed_field = detail.get("passed")
      threshold = detail.get("threshold")
      # Decide pass/fail for this metric. Prefer the stashed ``passed``
      # flag; fall back to score vs threshold when the detail isn't
      # populated (older custom evaluators).
      if passed_field is not None:
        if passed_field:
          continue
      elif threshold is not None:
        if score >= threshold:
          continue
      else:
        # No per-metric metadata at all. Since the session is in the
        # failed bucket, we still want to name the metric; assume it
        # failed unless the score is a perfect 1.0.
        if score >= 1.0:
          continue

      observed = detail.get("observed")
      budget = detail.get("budget")
      parts = [f"FAIL session={s.session_id} metric={metric_name}"]
      if observed is not None:
        if isinstance(observed, float):
          parts.append(f"observed={observed:.4g}")
        else:
          parts.append(f"observed={observed}")
      if budget is not None:
        if isinstance(budget, float):
          parts.append(f"budget={budget:.4g}")
        else:
          parts.append(f"budget={budget}")
      cache_state = detail.get("cache_state")
      if cache_state is not None:
        parts.append(f"cache_state={cache_state}")
      # Always include score + threshold so the reader has numeric
      # context even when observed / budget weren't declared (custom
      # metrics, LLM judges).
      parts.append(f"score={score:.4g}")
      if threshold is not None and isinstance(threshold, (int, float)):
        parts.append(f"threshold={threshold:.4g}")
      # LLM judges populate ``SessionScore.llm_feedback`` with the
      # judge's justification. Surface a bounded snippet on the FAIL
      # line so CI logs explain *why* without dumping the full JSON.
      # Code-based metrics leave ``llm_feedback`` empty and skip this.
      if feedback_snippet is not None:
        parts.append(f'feedback="{feedback_snippet}"')
      typer.echo("  " + " ".join(parts), err=True)
      emitted_for_session = True

    # Safety net: a failing session must produce at least one FAIL line.
    # This triggers only if every metric's details claim passed=True
    # while the session itself is flagged failed (a bug upstream) — we
    # still point the reader at the session id.
    if not emitted_for_session:
      fallback = f"  FAIL session={s.session_id}"
      if feedback_snippet is not None:
        fallback += f' feedback="{feedback_snippet}"'
      else:
        fallback += " (no per-metric detail available)"
      typer.echo(fallback, err=True)
  if len(failed) > len(shown):
    typer.echo(
        f"  ... {len(failed) - len(shown)} more failing session(s) "
        f"(raise --limit or see --format=json for full list)",
        err=True,
    )


@app.command()
def insights(
    project_id: str = typer.Option(
        ..., envvar="BQ_AGENT_PROJECT", help=_PROJECT_HELP
    ),
    dataset_id: str = typer.Option(
        ..., envvar="BQ_AGENT_DATASET", help=_DATASET_HELP
    ),
    table_id: str = typer.Option("agent_events", help="Events table name."),
    location: Optional[str] = typer.Option(None, help="BQ location."),
    agent_id: Optional[str] = typer.Option(None, help="Filter by agent name."),
    last: Optional[str] = typer.Option(
        None,
        help="Time window: 30m, 1h, 24h, 7d, 30d.",
    ),
    limit: int = typer.Option(100, help="Max sessions to analyze."),
    max_sessions: int = typer.Option(
        50, help="Max sessions for insights pipeline."
    ),
    fmt: str = typer.Option(
        "json",
        "--format",
        help="Output format: json|text|table.",
    ),
) -> None:
  """Generate an insights report over recent traces."""
  try:
    from .insights import InsightsConfig

    filters = TraceFilter.from_cli_args(
        last=last,
        agent_id=agent_id,
        limit=limit,
    )
    config = InsightsConfig(max_sessions=max_sessions)
    client = _build_client(project_id, dataset_id, table_id, location)
    report = client.insights(filters=filters, config=config)
    typer.echo(format_output(report, fmt))
  except Exception as exc:
    typer.echo(f"Error: {exc}", err=True)
    raise typer.Exit(code=2)


@app.command()
def drift(
    project_id: str = typer.Option(
        ..., envvar="BQ_AGENT_PROJECT", help=_PROJECT_HELP
    ),
    dataset_id: str = typer.Option(
        ..., envvar="BQ_AGENT_DATASET", help=_DATASET_HELP
    ),
    table_id: str = typer.Option("agent_events", help="Events table name."),
    location: Optional[str] = typer.Option(None, help="BQ location."),
    golden_dataset: str = typer.Option(
        ..., help="Golden dataset name for comparison."
    ),
    agent_id: Optional[str] = typer.Option(None, help="Filter by agent name."),
    last: Optional[str] = typer.Option(
        None,
        help="Time window: 30m, 1h, 24h, 7d, 30d.",
    ),
    limit: int = typer.Option(100, help="Max sessions."),
    fmt: str = typer.Option(
        "json",
        "--format",
        help="Output format: json|text|table.",
    ),
) -> None:
  """Detect drift between golden and production datasets."""
  try:
    filters = TraceFilter.from_cli_args(
        last=last,
        agent_id=agent_id,
        limit=limit,
    )
    client = _build_client(project_id, dataset_id, table_id, location)
    report = client.drift_detection(
        golden_dataset=golden_dataset,
        filters=filters,
    )
    typer.echo(format_output(report, fmt))
  except Exception as exc:
    typer.echo(f"Error: {exc}", err=True)
    raise typer.Exit(code=2)


@app.command()
def distribution(
    project_id: str = typer.Option(
        ..., envvar="BQ_AGENT_PROJECT", help=_PROJECT_HELP
    ),
    dataset_id: str = typer.Option(
        ..., envvar="BQ_AGENT_DATASET", help=_DATASET_HELP
    ),
    table_id: str = typer.Option("agent_events", help="Events table name."),
    location: Optional[str] = typer.Option(None, help="BQ location."),
    agent_id: Optional[str] = typer.Option(None, help="Filter by agent name."),
    last: Optional[str] = typer.Option(
        None,
        help="Time window: 30m, 1h, 24h, 7d, 30d.",
    ),
    limit: int = typer.Option(100, help="Max sessions."),
    mode: str = typer.Option(
        "auto_group_using_semantics",
        help=(
            "Analysis mode: frequently_asked|frequently_unanswered|"
            "auto_group_using_semantics|custom."
        ),
    ),
    top_k: int = typer.Option(20, help="Top items per category."),
    fmt: str = typer.Option(
        "json",
        "--format",
        help="Output format: json|text|table.",
    ),
) -> None:
  """Analyze question distribution across sessions."""
  try:
    from .feedback import AnalysisConfig

    filters = TraceFilter.from_cli_args(
        last=last,
        agent_id=agent_id,
        limit=limit,
    )
    config = AnalysisConfig(mode=mode, top_k=top_k)
    client = _build_client(project_id, dataset_id, table_id, location)
    report = client.deep_analysis(
        filters=filters,
        configuration=config,
    )
    typer.echo(format_output(report, fmt))
  except Exception as exc:
    typer.echo(f"Error: {exc}", err=True)
    raise typer.Exit(code=2)


@app.command("hitl-metrics")
def hitl_metrics(
    project_id: str = typer.Option(
        ..., envvar="BQ_AGENT_PROJECT", help=_PROJECT_HELP
    ),
    dataset_id: str = typer.Option(
        ..., envvar="BQ_AGENT_DATASET", help=_DATASET_HELP
    ),
    table_id: str = typer.Option("agent_events", help="Events table name."),
    location: Optional[str] = typer.Option(None, help="BQ location."),
    agent_id: Optional[str] = typer.Option(None, help="Filter by agent name."),
    last: Optional[str] = typer.Option(
        None,
        help="Time window: 30m, 1h, 24h, 7d, 30d.",
    ),
    limit: int = typer.Option(100, help="Max sessions."),
    fmt: str = typer.Option(
        "json",
        "--format",
        help="Output format: json|text|table.",
    ),
) -> None:
  """Display human-in-the-loop interaction metrics."""
  try:
    filters = TraceFilter.from_cli_args(
        last=last,
        agent_id=agent_id,
        limit=limit,
    )
    client = _build_client(project_id, dataset_id, table_id, location)
    result = client.hitl_metrics(filters=filters)
    typer.echo(format_output(result, fmt))
  except Exception as exc:
    typer.echo(f"Error: {exc}", err=True)
    raise typer.Exit(code=2)


@app.command("list-traces")
def list_traces(
    project_id: str = typer.Option(
        ..., envvar="BQ_AGENT_PROJECT", help=_PROJECT_HELP
    ),
    dataset_id: str = typer.Option(
        ..., envvar="BQ_AGENT_DATASET", help=_DATASET_HELP
    ),
    table_id: str = typer.Option("agent_events", help="Events table name."),
    location: Optional[str] = typer.Option(None, help="BQ location."),
    session_id: Optional[str] = typer.Option(
        None, help="Filter by session ID."
    ),
    agent_id: Optional[str] = typer.Option(None, help="Filter by agent name."),
    last: Optional[str] = typer.Option(
        None,
        help="Time window: 30m, 1h, 24h, 7d, 30d.",
    ),
    limit: int = typer.Option(100, help="Max traces to list."),
    fmt: str = typer.Option(
        "json",
        "--format",
        help="Output format: json|text|table.",
    ),
) -> None:
  """List traces matching filter criteria."""
  try:
    filters = TraceFilter.from_cli_args(
        last=last,
        agent_id=agent_id,
        session_id=session_id,
        limit=limit,
    )
    client = _build_client(project_id, dataset_id, table_id, location)
    traces = client.list_traces(filter_criteria=filters)
    typer.echo(format_output(traces, fmt))
  except Exception as exc:
    typer.echo(f"Error: {exc}", err=True)
    raise typer.Exit(code=2)


# ------------------------------------------------------------------ #
# categorical-eval                                                     #
# ------------------------------------------------------------------ #


@app.command("categorical-eval")
def categorical_eval(
    project_id: str = typer.Option(
        ..., envvar="BQ_AGENT_PROJECT", help=_PROJECT_HELP
    ),
    dataset_id: str = typer.Option(
        ..., envvar="BQ_AGENT_DATASET", help=_DATASET_HELP
    ),
    table_id: str = typer.Option("agent_events", help="Events table name."),
    location: Optional[str] = typer.Option(None, help="BQ location."),
    metrics_file: Path = typer.Option(
        ...,
        help="JSON file with metric definitions.",
        exists=True,
        readable=True,
    ),
    agent_id: Optional[str] = typer.Option(None, help="Filter by agent name."),
    last: Optional[str] = typer.Option(
        None,
        help="Time window: 30m, 1h, 24h, 7d, 30d.",
    ),
    limit: int = typer.Option(100, help="Max sessions to evaluate."),
    endpoint: Optional[str] = typer.Option(
        None,
        help="Model endpoint for classification.",
    ),
    connection_id: Optional[str] = typer.Option(
        None,
        help="BQ connection ID for AI.CLASSIFY / AI.GENERATE.",
    ),
    include_justification: bool = typer.Option(
        True,
        help="Include justification in output.",
    ),
    persist: bool = typer.Option(
        False,
        help="Write results to BigQuery.",
    ),
    results_table: Optional[str] = typer.Option(
        None,
        help="Destination table for persisted results.",
    ),
    prompt_version: Optional[str] = typer.Option(
        None,
        help="Prompt version tag for reproducibility.",
    ),
    pass_category: Optional[list[str]] = typer.Option(
        None,
        "--pass-category",
        help=(
            "Declare the pass category for a metric as"
            " METRIC=CATEGORY. Repeat for multiple metrics. Sessions"
            " whose classification for METRIC equals CATEGORY count"
            " as passing; everything else fails."
        ),
    ),
    min_pass_rate: float = typer.Option(
        1.0,
        "--min-pass-rate",
        help=(
            "Minimum per-metric pass rate required with --exit-code."
            " Applied to every metric that has a --pass-category."
            " Defaults to 1.0 (every classified session must pass)."
        ),
        min=0.0,
        max=1.0,
    ),
    exit_code: bool = typer.Option(
        False,
        "--exit-code",
        help=(
            "Return exit code 1 when any metric's pass rate falls"
            " below --min-pass-rate. Requires at least one"
            " --pass-category."
        ),
    ),
    fmt: str = typer.Option(
        "json",
        "--format",
        help="Output format: json|text|table.",
    ),
) -> None:
  """Run categorical evaluation over agent traces."""
  try:
    from .categorical_evaluator import CategoricalEvaluationConfig
    from .categorical_evaluator import CategoricalMetricCategory
    from .categorical_evaluator import CategoricalMetricDefinition

    raw = json.loads(metrics_file.read_text())
    metrics_list = raw if isinstance(raw, list) else raw.get("metrics", [])

    metrics = []
    for m in metrics_list:
      cats = [
          CategoricalMetricCategory(
              name=c["name"],
              definition=c["definition"],
          )
          for c in m["categories"]
      ]
      metric_kwargs: dict = {
          "name": m["name"],
          "definition": m["definition"],
          "categories": cats,
      }
      if "required" in m:
        metric_kwargs["required"] = m["required"]
      metrics.append(CategoricalMetricDefinition(**metric_kwargs))

    if not metrics:
      typer.echo("Error: no metrics found in metrics file.", err=True)
      raise typer.Exit(code=2)

    config_kwargs: dict = {
        "metrics": metrics,
        "include_justification": include_justification,
        "persist_results": persist,
    }
    if endpoint is not None:
      config_kwargs["endpoint"] = endpoint
    if connection_id is not None:
      config_kwargs["connection_id"] = connection_id
    if results_table is not None:
      config_kwargs["results_table"] = results_table
    if prompt_version is not None:
      config_kwargs["prompt_version"] = prompt_version
    config = CategoricalEvaluationConfig(**config_kwargs)

    filters = TraceFilter.from_cli_args(
        last=last,
        agent_id=agent_id,
        limit=limit,
    )

    client = _build_client(
        project_id,
        dataset_id,
        table_id,
        location,
        endpoint=endpoint,
        connection_id=connection_id,
    )
    # Validate --exit-code configuration before spending any BigQuery /
    # LLM work. A missing or malformed --pass-category is a CI
    # configuration bug; it would be wasteful to run the classification
    # only to reject the flags afterwards.
    pass_map: dict[str, set[str]] = {}
    if exit_code:
      pass_map = _parse_pass_category_flags(pass_category or [])
      if not pass_map:
        typer.echo(
            "Error: --exit-code requires at least one --pass-category"
            " METRIC=CATEGORY (tells the CLI which classification"
            " counts as a pass).",
            err=True,
        )
        raise typer.Exit(code=2)

    report = client.evaluate_categorical(config=config, filters=filters)
    typer.echo(format_output(report, fmt))

    if exit_code:
      _emit_categorical_failures(report, pass_map, min_pass_rate)
      if _categorical_has_failures(report, pass_map, min_pass_rate):
        raise typer.Exit(code=1)
  except typer.Exit:
    raise
  except Exception as exc:
    typer.echo(f"Error: {exc}", err=True)
    raise typer.Exit(code=2)


def _parse_pass_category_flags(flags: list[str]) -> dict[str, set[str]]:
  """Parse ``--pass-category METRIC=CATEGORY`` flags into a lookup.

  Multiple categories for the same metric are allowed (e.g. both
  ``useful`` and ``partially_useful`` pass). Invalid values raise
  ``typer.Exit(code=2)`` with a readable error.
  """
  parsed: dict[str, set[str]] = {}
  for raw in flags:
    if "=" not in raw:
      typer.echo(
          f"Error: invalid --pass-category value '{raw}'; expected"
          " METRIC=CATEGORY.",
          err=True,
      )
      raise typer.Exit(code=2)
    metric, category = raw.split("=", 1)
    metric = metric.strip()
    category = category.strip()
    if not metric or not category:
      typer.echo(
          f"Error: invalid --pass-category value '{raw}'; both"
          " METRIC and CATEGORY must be non-empty.",
          err=True,
      )
      raise typer.Exit(code=2)
    parsed.setdefault(metric, set()).add(category)
  return parsed


def _categorical_metric_pass_rate(
    report, metric_name: str, pass_categories: set[str]
) -> tuple[float, int, int, bool]:
  """Return ``(pass_rate, passing, total, metric_observed)`` for one metric.

  Walks ``report.session_results`` and counts a session as *passing*
  for ``metric_name`` iff it produced a classification with
  ``category in pass_categories``, ``not parse_error``, and
  ``passed_validation``. Sessions with a parse error, a missing
  classification, an ``out_of_bounds`` category, or no
  ``MetricResult`` entry for the metric at all count as **failing** —
  CI must not treat broken runs as passes.

  ``metric_observed`` is ``False`` iff no ``MetricResult`` for
  ``metric_name`` appeared in any session (likely a configuration
  mistake: the user declared a pass category for a metric that isn't
  in the metrics file or run window). The caller uses this to emit
  a WARN rather than a FAIL.

  The denominator is ``report.total_sessions`` when it's set; falls
  back to ``len(report.session_results)`` for older reports. Falls
  back further to summing ``report.category_distributions[metric_name]``
  when ``session_results`` is empty so the gate remains defined for
  minimal / mocked reports.

  ``total == 0`` returns ``(1.0, 0, 0, False)`` — nothing to evaluate.
  """
  session_results = getattr(report, "session_results", None) or []
  total = getattr(report, "total_sessions", 0) or len(session_results)
  metric_observed = False

  if session_results:
    passing = 0
    for sr in session_results:
      matched = False
      for mr in sr.metrics:
        if mr.metric_name != metric_name:
          continue
        metric_observed = True
        matched = True
        if (
            mr.category in pass_categories
            and not mr.parse_error
            and mr.passed_validation
        ):
          passing += 1
        # First matching MetricResult wins for this session.
        break
      # A session with no MetricResult for this metric counts as a
      # fail (parse gap, dropped response, metric missing from the
      # model output). ``matched`` is intentionally unused after the
      # loop — its only role is to document that unmatched sessions
      # don't contribute to ``passing``.
      del matched
    if total == 0:
      total = len(session_results)
    if total == 0:
      return 1.0, 0, 0, metric_observed
    return passing / total, passing, total, metric_observed

  # Fallback: no session_results (minimal mocked reports, older
  # evaluator paths). Use category_distributions but still treat an
  # empty distribution as "metric not observed" rather than a pass.
  distribution = getattr(report, "category_distributions", {}).get(
      metric_name, {}
  )
  dist_total = sum(distribution.values())
  if dist_total == 0:
    return 1.0, 0, 0, False
  metric_observed = True
  passing = sum(
      count for name, count in distribution.items() if name in pass_categories
  )
  denominator = total if total > 0 else dist_total
  return passing / denominator, passing, denominator, metric_observed


def _categorical_has_failures(
    report, pass_map: dict[str, set[str]], min_pass_rate: float
) -> bool:
  for metric_name, pass_categories in pass_map.items():
    rate, _, total, metric_observed = _categorical_metric_pass_rate(
        report, metric_name, pass_categories
    )
    # Metric that never showed up in the report is a WARN, not a FAIL
    # — that decision is handled in ``_emit_categorical_failures``. We
    # don't want a typo in the pass-category flag to turn into a
    # silent green CI.
    if not metric_observed:
      continue
    if total > 0 and rate < min_pass_rate:
      return True
  return False


def _emit_categorical_failures(
    report,
    pass_map: dict[str, set[str]],
    min_pass_rate: float,
    max_lines: int = 10,
) -> None:
  """Emit one FAIL line per metric whose pass rate is under threshold.

  Also warns (not fails) when a --pass-category references a metric
  that has no classifications in the report — a likely configuration
  mistake the reader should notice in CI logs.
  """
  failing: list[tuple[str, float, int, int]] = []
  missing_metrics: list[str] = []
  for metric_name, pass_categories in pass_map.items():
    rate, passing, total, metric_observed = _categorical_metric_pass_rate(
        report, metric_name, pass_categories
    )
    if not metric_observed:
      missing_metrics.append(metric_name)
      continue
    if total > 0 and rate < min_pass_rate:
      failing.append((metric_name, rate, passing, total))

  if not failing and not missing_metrics:
    return
  typer.echo("", err=True)
  if failing:
    typer.echo(
        f"--exit-code: {len(failing)} metric(s) under min-pass-rate"
        f" {min_pass_rate:.3g}",
        err=True,
    )
    for metric_name, rate, passing, total in failing[:max_lines]:
      passes = sorted(pass_map[metric_name])
      typer.echo(
          f"  FAIL metric={metric_name}"
          f" pass_rate={rate:.3g}"
          f" ({passing}/{total})"
          f" min={min_pass_rate:.3g}"
          f" pass_categories={','.join(passes)}",
          err=True,
      )
    if len(failing) > max_lines:
      typer.echo(
          f"  ... {len(failing) - max_lines} more failing metric(s)",
          err=True,
      )
  for metric_name in missing_metrics:
    typer.echo(
        f"  WARN --pass-category referenced metric={metric_name} but no"
        " classifications for it appeared in the report (check your"
        " metrics file and the run window)",
        err=True,
    )


# ------------------------------------------------------------------ #
# categorical-views                                                    #
# ------------------------------------------------------------------ #


@app.command("categorical-views")
def categorical_views(
    project_id: str = typer.Option(
        ..., envvar="BQ_AGENT_PROJECT", help=_PROJECT_HELP
    ),
    dataset_id: str = typer.Option(
        ..., envvar="BQ_AGENT_DATASET", help=_DATASET_HELP
    ),
    results_table: str = typer.Option(
        "categorical_results", help="Source results table name."
    ),
    location: Optional[str] = typer.Option(None, help="BQ location."),
    prefix: str = typer.Option("", help="View name prefix."),
    fmt: str = typer.Option(
        "json",
        "--format",
        help="Output format: json|text|table.",
    ),
) -> None:
  """Create dashboard views over categorical evaluation results."""
  try:
    from .categorical_views import CategoricalViewManager

    vm = CategoricalViewManager(
        project_id=project_id,
        dataset_id=dataset_id,
        results_table=results_table,
        view_prefix=prefix,
        location=location,
    )
    result = vm.create_all_views()
    typer.echo(format_output(result, fmt))
  except Exception as exc:
    typer.echo(f"Error: {exc}", err=True)
    raise typer.Exit(code=2)


# ------------------------------------------------------------------ #
# ontology-property-graph                                              #
# ------------------------------------------------------------------ #


@app.command("ontology-property-graph")
def ontology_property_graph(
    project_id: str = typer.Option(
        ..., envvar="BQ_AGENT_PROJECT", help=_PROJECT_HELP
    ),
    dataset_id: str = typer.Option(
        ..., envvar="BQ_AGENT_DATASET", help=_DATASET_HELP
    ),
    ontology_path: str = typer.Option(
        None,
        "--ontology",
        help="Path to ontology YAML file (use with --binding).",
    ),
    binding_path: str = typer.Option(
        None,
        "--binding",
        help="Path to binding YAML file (use with --ontology).",
    ),
    spec_path: str = typer.Option(
        None,
        "--spec-path",
        help="[Deprecated] Path to combined graph-spec YAML.",
    ),
    env: Optional[str] = typer.Option(
        None, help="Value for {{ env }} placeholder in binding.source."
    ),
    graph_name: Optional[str] = typer.Option(
        None, help="Override the property graph name."
    ),
    execute: bool = typer.Option(
        False, help="Execute the DDL against BigQuery."
    ),
    fmt: str = typer.Option(
        "sql",
        "--format",
        help="Output format: sql|json.",
    ),
) -> None:
  """Generate or create a Property Graph from an ontology YAML spec."""
  try:
    from .ontology_property_graph import OntologyPropertyGraphCompiler

    spec = _load_spec_from_args(spec_path, ontology_path, binding_path, env)
    compiler = OntologyPropertyGraphCompiler(
        project_id=project_id,
        dataset_id=dataset_id,
        spec=spec,
    )
    ddl = compiler.get_ddl(graph_name=graph_name)

    if execute:
      success = compiler.create_property_graph(graph_name=graph_name)
      result = {
          "graph_name": graph_name or spec.name,
          "graph_ref": f"{project_id}.{dataset_id}.{graph_name or spec.name}",
          "success": success,
          "ddl": ddl,
      }
      typer.echo(format_output(result, fmt if fmt != "sql" else "json"))
    else:
      if fmt == "sql":
        typer.echo(ddl)
      else:
        result = {
            "graph_name": graph_name or spec.name,
            "ddl": ddl,
        }
        typer.echo(format_output(result, fmt))
  except Exception as exc:
    typer.echo(f"Error: {exc}", err=True)
    raise typer.Exit(code=2)


# ------------------------------------------------------------------ #
# ontology-build (end-to-end pipeline)                                 #
# ------------------------------------------------------------------ #


@app.command("ontology-build")
def ontology_build(
    project_id: str = typer.Option(
        ..., envvar="BQ_AGENT_PROJECT", help=_PROJECT_HELP
    ),
    dataset_id: str = typer.Option(
        ..., envvar="BQ_AGENT_DATASET", help=_DATASET_HELP
    ),
    ontology_path: str = typer.Option(
        None,
        "--ontology",
        help="Path to ontology YAML file (use with --binding).",
    ),
    binding_path: str = typer.Option(
        None,
        "--binding",
        help="Path to binding YAML file (use with --ontology).",
    ),
    spec_path: str = typer.Option(
        None,
        "--spec-path",
        help="[Deprecated] Path to combined graph-spec YAML.",
    ),
    session_ids: str = typer.Option(
        ..., help="Comma-separated session IDs to extract."
    ),
    env: Optional[str] = typer.Option(
        None, help="Value for {{ env }} placeholder in binding.source."
    ),
    graph_name: Optional[str] = typer.Option(
        None, help="Override the property graph name."
    ),
    table_id: str = typer.Option(
        "agent_events", help="Source telemetry table name."
    ),
    endpoint: str = typer.Option(
        "gemini-2.5-flash", help="AI.GENERATE model endpoint."
    ),
    no_ai_generate: bool = typer.Option(
        False, help="Skip AI.GENERATE; fetch raw payloads instead."
    ),
    skip_property_graph: bool = typer.Option(
        False,
        "--skip-property-graph",
        help=(
            "Skip CREATE OR REPLACE PROPERTY GRAPH. Use when the caller "
            "owns their own property-graph DDL and only wants the SDK to "
            "populate base tables. CLI exits 0 with "
            "property_graph_status='skipped:user_requested'."
        ),
    ),
    validate_binding: bool = typer.Option(
        False,
        "--validate-binding",
        help=(
            "Pre-flight: validate the binding against live BigQuery "
            "tables before extraction. NULLABLE primary-key columns "
            "emit advisory warnings (printed to stderr). Other "
            "failures (missing tables/columns, type mismatches) "
            "short-circuit the build before any AI.GENERATE call "
            "fires. Requires --ontology + --binding."
        ),
    ),
    validate_binding_strict: bool = typer.Option(
        False,
        "--validate-binding-strict",
        help=(
            "Pre-flight: validate the binding in strict mode. Same "
            "as --validate-binding but escalates KEY_COLUMN_NULLABLE "
            "warnings into hard failures. Use in CI when you want "
            "every primary-key column to be REQUIRED. Mutually "
            "exclusive with --validate-binding."
        ),
    ),
    location: Optional[str] = typer.Option(
        None,
        "--location",
        help=(
            "BigQuery location (e.g. 'US', 'EU'). Used by the binding "
            "pre-flight validator and forwarded downstream. Required "
            "for --validate-binding[-strict] when the bound tables "
            "live outside the default location."
        ),
    ),
    fmt: str = typer.Option(
        "json",
        "--format",
        help="Output format: json|text|table.",
    ),
) -> None:
  """Run the full ontology graph pipeline end-to-end."""
  try:
    if validate_binding and validate_binding_strict:
      raise typer.BadParameter(
          "Use --validate-binding OR --validate-binding-strict, " "not both."
      )
    if (validate_binding or validate_binding_strict) and spec_path:
      raise typer.BadParameter(
          "Binding validation requires --ontology + --binding "
          "(separated form). It is not supported with --spec-path."
      )

    from .ontology_orchestrator import build_ontology_graph

    if validate_binding or validate_binding_strict:
      _run_binding_preflight(
          ontology_path=ontology_path,
          binding_path=binding_path,
          project_id=project_id,
          location=location,
          strict=validate_binding_strict,
      )

    loaded_spec = _load_spec_from_args(
        spec_path, ontology_path, binding_path, env
    )
    sids = [s.strip() for s in session_ids.split(",") if s.strip()]
    result = build_ontology_graph(
        session_ids=sids,
        spec=loaded_spec,
        project_id=project_id,
        dataset_id=dataset_id,
        graph_name=graph_name,
        table_id=table_id,
        endpoint=endpoint,
        use_ai_generate=not no_ai_generate,
        skip_property_graph=skip_property_graph,
        location=location,
    )

    output = {
        "graph_name": result["graph_name"],
        "graph_ref": result["graph_ref"],
        "nodes_extracted": len(result["graph"].nodes),
        "edges_extracted": len(result["graph"].edges),
        "tables_created": result["tables_created"],
        "rows_materialized": result["rows_materialized"],
        "property_graph_created": result["property_graph_created"],
        "property_graph_status": result.get(
            "property_graph_status",
            "created" if result["property_graph_created"] else "failed",
        ),
    }
    typer.echo(format_output(output, fmt))

    # Distinguish "user-requested skip" (exit 0) from "creation failed"
    # (exit 1). Same property_graph_created=False, different operator
    # intent — JSON consumers read property_graph_status to tell them
    # apart without parsing stderr.
    if result.get("skipped_reason") == "user_requested":
      return
    if not result["property_graph_created"]:
      typer.echo(
          "Error: Property Graph creation failed. "
          "Tables and data were materialized but the graph object "
          "was not created.",
          err=True,
      )
      raise typer.Exit(code=1)
  except typer.Exit:
    raise
  except Exception as exc:
    typer.echo(f"Error: {exc}", err=True)
    raise typer.Exit(code=2)


# ------------------------------------------------------------------ #
# binding-validate                                                     #
# ------------------------------------------------------------------ #


@app.command("binding-validate")
def binding_validate(
    project_id: str = typer.Option(
        ..., envvar="BQ_AGENT_PROJECT", help=_PROJECT_HELP
    ),
    ontology_path: str = typer.Option(
        ...,
        "--ontology",
        help="Path to ontology YAML file.",
    ),
    binding_path: str = typer.Option(
        ...,
        "--binding",
        help="Path to binding YAML file.",
    ),
    location: Optional[str] = typer.Option(
        None,
        "--location",
        help="BigQuery location (e.g. 'US', 'EU').",
    ),
    strict: bool = typer.Option(
        False,
        "--strict",
        help=(
            "Strict mode: KEY_COLUMN_NULLABLE warnings escalate to "
            "hard failures. Use in CI when you want every primary-key "
            "column to be REQUIRED."
        ),
    ),
    fmt: str = typer.Option(
        "json",
        "--format",
        help="Output format: json|text|table.",
    ),
) -> None:
  """Pre-flight validate a binding YAML against live BigQuery tables.

  Catches the most common authoring error (binding YAML drifted out
  of sync with physical tables) before extraction wastes
  AI.GENERATE tokens. Loads the ontology + binding, queries
  BigQuery for each referenced table's actual schema, and reports
  any drift as structured failures.

  Exit codes:
      0 — report.ok is True (no failures; warnings allowed in
          default mode and printed to stderr).
      1 — report.ok is False (failures present).
      2 — unexpected error (load failure, missing flag, etc.).

  See bq-agent-sdk binding-validate --help for all flags.
  """
  try:
    from google.cloud import bigquery

    from bigquery_ontology import load_binding
    from bigquery_ontology import load_ontology

    from .binding_validation import validate_binding_against_bigquery

    ontology = load_ontology(ontology_path)
    binding = load_binding(binding_path, ontology=ontology)
    bq_client = bigquery.Client(project=project_id, location=location)

    report = validate_binding_against_bigquery(
        ontology=ontology,
        binding=binding,
        bq_client=bq_client,
        strict=strict,
    )

    output = {
        "ok": report.ok,
        "strict": strict,
        "failures": [
            {
                "code": f.code.value,
                "binding_element": f.binding_element,
                "binding_path": f.binding_path,
                "bq_ref": f.bq_ref,
                "expected": f.expected,
                "observed": f.observed,
                "detail": f.detail,
            }
            for f in report.failures
        ],
        "warnings": [
            {
                "code": w.code.value,
                "binding_element": w.binding_element,
                "binding_path": w.binding_path,
                "bq_ref": w.bq_ref,
                "expected": w.expected,
                "observed": w.observed,
                "detail": w.detail,
            }
            for w in report.warnings
        ],
    }
    typer.echo(format_output(output, fmt))

    # Warnings always print to stderr (one line per warning) so CI
    # logs surface advisory drift even in JSON-format runs that
    # consume stdout for the report.
    for w in report.warnings:
      typer.echo(
          f"WARN: {w.code.value} at {w.binding_path} "
          f"({w.bq_ref}): {w.detail}",
          err=True,
      )

    if not report.ok:
      raise typer.Exit(code=1)
  except typer.Exit:
    raise
  except Exception as exc:
    typer.echo(f"Error: {exc}", err=True)
    raise typer.Exit(code=2)


# ------------------------------------------------------------------ #
# ontology-showcase-gql                                                #
# ------------------------------------------------------------------ #


@app.command("ontology-showcase-gql")
def ontology_showcase_gql(
    project_id: str = typer.Option(
        ..., envvar="BQ_AGENT_PROJECT", help=_PROJECT_HELP
    ),
    dataset_id: str = typer.Option(
        ..., envvar="BQ_AGENT_DATASET", help=_DATASET_HELP
    ),
    ontology_path: str = typer.Option(
        None,
        "--ontology",
        help="Path to ontology YAML file (use with --binding).",
    ),
    binding_path: str = typer.Option(
        None,
        "--binding",
        help="Path to binding YAML file (use with --ontology).",
    ),
    spec_path: str = typer.Option(
        None,
        "--spec-path",
        help="[Deprecated] Path to combined graph-spec YAML.",
    ),
    env: Optional[str] = typer.Option(
        None, help="Value for {{ env }} placeholder in binding.source."
    ),
    graph_name: Optional[str] = typer.Option(
        None, help="Override the property graph name."
    ),
    relationship: Optional[str] = typer.Option(
        None, help="Relationship to traverse (default: first in spec)."
    ),
    no_session_filter: bool = typer.Option(
        False, help="Omit the WHERE session_id filter."
    ),
    fmt: str = typer.Option(
        "sql",
        "--format",
        help="Output format: sql|json.",
    ),
) -> None:
  """Generate a GQL showcase query from an ontology YAML spec."""
  try:
    from .ontology_orchestrator import compile_showcase_gql

    spec = _load_spec_from_args(spec_path, ontology_path, binding_path, env)
    gql = compile_showcase_gql(
        spec,
        project_id=project_id,
        dataset_id=dataset_id,
        graph_name=graph_name,
        relationship_name=relationship,
        session_filter=not no_session_filter,
    )

    if fmt == "sql":
      typer.echo(gql)
    else:
      output = {
          "graph_name": graph_name or spec.name,
          "relationship": relationship or spec.relationships[0].name,
          "gql": gql,
      }
      typer.echo(format_output(output, fmt))
  except Exception as exc:
    typer.echo(f"Error: {exc}", err=True)
    raise typer.Exit(code=2)


# ------------------------------------------------------------------ #
# views sub-commands                                                   #
# ------------------------------------------------------------------ #

views_app = typer.Typer(
    name="views",
    help="Manage per-event-type BigQuery views.",
    add_completion=False,
)
app.add_typer(views_app, name="views")


def _build_view_manager(
    project_id: str,
    dataset_id: str,
    table_id: str,
    prefix: str,
):
  """Lazily import ViewManager and construct an instance."""
  from .views import ViewManager

  return ViewManager(
      project_id=project_id,
      dataset_id=dataset_id,
      table_id=table_id,
      view_prefix=prefix,
  )


@views_app.command("create-all")
def views_create_all(
    project_id: str = typer.Option(
        ..., envvar="BQ_AGENT_PROJECT", help=_PROJECT_HELP
    ),
    dataset_id: str = typer.Option(
        ..., envvar="BQ_AGENT_DATASET", help=_DATASET_HELP
    ),
    table_id: str = typer.Option("agent_events", help="Events table name."),
    prefix: str = typer.Option("adk_", help="View name prefix."),
    fmt: str = typer.Option(
        "json",
        "--format",
        help="Output format: json|text|table.",
    ),
) -> None:
  """Create views for all supported event types."""
  try:
    vm = _build_view_manager(project_id, dataset_id, table_id, prefix)
    result = vm.create_all_views()
    typer.echo(format_output(result, fmt))
  except Exception as exc:
    typer.echo(f"Error: {exc}", err=True)
    raise typer.Exit(code=2)


@views_app.command("create")
def views_create(
    event_type: str = typer.Argument(help="Event type to create a view for."),
    project_id: str = typer.Option(
        ..., envvar="BQ_AGENT_PROJECT", help=_PROJECT_HELP
    ),
    dataset_id: str = typer.Option(
        ..., envvar="BQ_AGENT_DATASET", help=_DATASET_HELP
    ),
    table_id: str = typer.Option("agent_events", help="Events table name."),
    prefix: str = typer.Option("adk_", help="View name prefix."),
    fmt: str = typer.Option(
        "json",
        "--format",
        help="Output format: json|text|table.",
    ),
) -> None:
  """Create a view for a single event type."""
  try:
    vm = _build_view_manager(project_id, dataset_id, table_id, prefix)
    vm.create_view(event_type)
    result = {"event_type": event_type, "status": "created"}
    typer.echo(format_output(result, fmt))
  except Exception as exc:
    typer.echo(f"Error: {exc}", err=True)
    raise typer.Exit(code=2)


def main() -> None:
  """Entry point for ``bq-agent-sdk``."""
  app()


if __name__ == "__main__":
  main()
