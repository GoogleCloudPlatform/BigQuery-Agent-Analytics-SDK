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

"""Command-line interface for export connectors."""

from __future__ import annotations

from datetime import datetime
from datetime import timezone
import json
from pathlib import Path
from typing import Optional

from google.api_core.exceptions import GoogleAPIError
from google.auth.exceptions import GoogleAuthError
import typer
import yaml

export_app = typer.Typer(
    name="export",
    help="Export BigQuery Agent Analytics data to external systems.",
    add_completion=False,
    no_args_is_help=True,
)


def _langsmith_error_types() -> tuple[type[Exception], ...]:
  try:
    from langsmith import utils as langsmith_utils
  except ImportError:
    return ()
  error_type = getattr(langsmith_utils, "LangSmithError", None)
  if isinstance(error_type, type) and issubclass(error_type, Exception):
    return (error_type,)
  return ()


@export_app.callback()
def _export_callback() -> None:
  """Export BigQuery Agent Analytics data."""
  return None


def _parse_datetime(value: str | None, option: str) -> datetime | None:
  """Parse an ISO-8601 CLI timestamp as an aware UTC datetime."""
  if value is None:
    return None
  try:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
  except ValueError as exc:
    raise typer.BadParameter(f"{option} must be an ISO-8601 timestamp") from exc
  if parsed.tzinfo is None:
    parsed = parsed.replace(tzinfo=timezone.utc)
  return parsed.astimezone(timezone.utc)


@export_app.command("langsmith")
def export_langsmith(
    source: str = typer.Option(
        ..., help="BigQuery table reference or arbitrary SQL query."
    ),
    mapping: Optional[Path] = typer.Option(
        None,
        exists=True,
        readable=True,
        help="YAML/JSON source-to-run field mapping.",
    ),
    project_id: Optional[str] = typer.Option(
        None,
        envvar="BQ_AGENT_PROJECT",
        help="GCP project ID. [env: BQ_AGENT_PROJECT]",
    ),
    location: Optional[str] = typer.Option(None, help="BigQuery location."),
    langsmith_project: Optional[str] = typer.Option(
        None,
        envvar="LANGSMITH_PROJECT",
        help="Destination LangSmith project.",
    ),
    langsmith_endpoint: Optional[str] = typer.Option(
        None,
        envvar="LANGSMITH_ENDPOINT",
        help="LangSmith API endpoint.",
    ),
    langsmith_workspace_id: Optional[str] = typer.Option(
        None,
        envvar="LANGSMITH_WORKSPACE_ID",
        help="LangSmith workspace ID for org-scoped keys.",
    ),
    source_id: Optional[str] = typer.Option(
        None,
        help="Stable source namespace for SQL whose text may change.",
    ),
    since: Optional[str] = typer.Option(
        None, help="Inclusive ISO-8601 start timestamp."
    ),
    until: Optional[str] = typer.Option(
        None, help="Exclusive ISO-8601 end timestamp."
    ),
    where: Optional[str] = typer.Option(
        None,
        "--filter",
        help="Additional trusted BigQuery SQL predicate.",
    ),
    incremental: bool = typer.Option(
        False, help="Resume from a persisted timestamp/run-ID watermark."
    ),
    watermark_file: Path = typer.Option(
        Path(".bqaa-langsmith-watermark.json"),
        help="Incremental watermark state file.",
    ),
    batch_size: int = typer.Option(
        100, min=1, help="Maximum source rows per LangSmith batch."
    ),
    requests_per_second: float = typer.Option(
        5.0, min=0.001, help="Maximum exporter batch calls per second."
    ),
    max_retries: int = typer.Option(
        3, min=0, help="Retries for transient LangSmith batch-call errors."
    ),
    max_dropped_rows: int = typer.Option(
        1000,
        min=0,
        help="Maximum dropped-row details retained in the JSON report.",
    ),
) -> None:
  """Export a BigQuery table or SQL result to LangSmith run trees."""
  from . import export
  from . import ExportConfig
  from . import FieldMapping

  try:
    field_mapping = (
        FieldMapping.from_file(mapping)
        if mapping is not None
        else FieldMapping.standard_adk()
    )
    config = ExportConfig(
        mapping=field_mapping,
        project_id=project_id,
        location=location,
        langsmith_project=langsmith_project,
        langsmith_endpoint=langsmith_endpoint,
        langsmith_workspace_id=langsmith_workspace_id,
        source_id=source_id,
        since=_parse_datetime(since, "--since"),
        until=_parse_datetime(until, "--until"),
        where=where,
        incremental=incremental,
        watermark_path=watermark_file,
        batch_size=batch_size,
        requests_per_second=requests_per_second,
        max_retries=max_retries,
        max_dropped_rows=max_dropped_rows,
    )
  except (OSError, ValueError, yaml.YAMLError) as exc:
    typer.echo(f"Error: {exc}", err=True)
    raise typer.Exit(code=2)

  operational_errors = (
      ImportError,
      OSError,
      ValueError,
      GoogleAPIError,
      GoogleAuthError,
      yaml.YAMLError,
      *_langsmith_error_types(),
  )
  try:
    stats = export(source, config=config)
  except operational_errors as exc:
    typer.echo(f"Error: {exc}", err=True)
    raise typer.Exit(code=2)
  typer.echo(json.dumps(stats.to_dict(), sort_keys=True))
  if stats.failed or stats.skipped:
    raise typer.Exit(code=1)
