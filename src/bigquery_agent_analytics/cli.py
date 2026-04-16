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
  )


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
            "token_efficiency|ttft|cost|llm-judge."
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
    strict: bool = typer.Option(
        False,
        help="Fail sessions with unparseable judge output.",
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
      raise typer.Exit(code=1)
  except typer.Exit:
    raise
  except Exception as exc:
    typer.echo(f"Error: {exc}", err=True)
    raise typer.Exit(code=2)


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
    report = client.evaluate_categorical(config=config, filters=filters)
    typer.echo(format_output(report, fmt))
  except typer.Exit:
    raise
  except Exception as exc:
    typer.echo(f"Error: {exc}", err=True)
    raise typer.Exit(code=2)


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
    fmt: str = typer.Option(
        "json",
        "--format",
        help="Output format: json|text|table.",
    ),
) -> None:
  """Run the full ontology graph pipeline end-to-end."""
  try:
    from .ontology_orchestrator import build_ontology_graph

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
    )

    output = {
        "graph_name": result["graph_name"],
        "graph_ref": result["graph_ref"],
        "nodes_extracted": len(result["graph"].nodes),
        "edges_extracted": len(result["graph"].edges),
        "tables_created": result["tables_created"],
        "rows_materialized": result["rows_materialized"],
        "property_graph_created": result["property_graph_created"],
    }
    typer.echo(format_output(output, fmt))

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
