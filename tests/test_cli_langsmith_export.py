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

"""CLI tests for ``bq-agent-sdk export langsmith``."""

from __future__ import annotations

from datetime import datetime
from datetime import timezone
import json
from pathlib import Path
from unittest.mock import patch

import click
from google.api_core import exceptions as google_exceptions
import pytest
from typer.main import get_command
from typer.testing import CliRunner

from bigquery_agent_analytics.cli import app
from bigquery_agent_analytics.cli import bqaa_app
from bigquery_agent_analytics.export import DroppedRow
from bigquery_agent_analytics.export import export
from bigquery_agent_analytics.export import ExportConfig
from bigquery_agent_analytics.export import ExportStats
from bigquery_agent_analytics.export import FieldMapping

runner = CliRunner()


def test_langsmith_export_command_builds_config_and_mapping(
    tmp_path: Path, monkeypatch
) -> None:
  mapping_path = tmp_path / "mapping.yaml"
  mapping_path.write_text(
      "fields:\n"
      "  run_id: event_key\n"
      "  trace_id: trace_key\n"
      "  start_time: occurred_at\n"
      "  inputs: payload\n",
      encoding="utf-8",
  )
  watermark_path = tmp_path / "state.json"
  monkeypatch.setenv("LANGSMITH_API_KEY", "test-key")
  monkeypatch.setenv("LANGSMITH_PROJECT", "test-project")
  stats = ExportStats(
      rows_read=2,
      exported=2,
      skipped=0,
      failed=0,
      traces_exported=1,
      batches=1,
      dropped_rows=(),
      watermark=datetime(2026, 8, 10, tzinfo=timezone.utc),
  )

  with patch(
      "bigquery_agent_analytics.export.export", return_value=stats
  ) as run_export:
    result = runner.invoke(
        app,
        [
            "export",
            "langsmith",
            "--source=project.dataset.events",
            f"--mapping={mapping_path}",
            "--project-id=project",
            "--location=US",
            "--since=2026-08-01T00:00:00Z",
            "--until=2026-08-11T00:00:00Z",
            "--filter=status = 'OK'",
            "--incremental",
            f"--watermark-file={watermark_path}",
            "--batch-size=25",
            "--requests-per-second=4",
            "--max-retries=2",
            "--max-dropped-rows=10",
        ],
    )

  assert result.exit_code == 0, result.output
  assert json.loads(result.stdout)["exported"] == 2
  source = run_export.call_args.args[0]
  config = run_export.call_args.kwargs["config"]
  assert source == "project.dataset.events"
  assert config.mapping.run_id == ("event_key",)
  assert config.mapping.status is None
  assert config.mapping.error is None
  assert config.mapping.latency_ms is None
  assert config.project_id == "project"
  assert config.location == "US"
  # The CLI leaves authentication to LangSmith's standard environment
  # handling instead of copying a secret from argv or environment.
  assert config.langsmith_api_key is None
  assert config.langsmith_project == "test-project"
  assert config.since == datetime(2026, 8, 1, tzinfo=timezone.utc)
  assert config.until == datetime(2026, 8, 11, tzinfo=timezone.utc)
  assert config.where == "status = 'OK'"
  assert config.incremental is True
  assert config.watermark_path == watermark_path
  assert config.batch_size == 25
  assert config.requests_per_second == 4
  assert config.max_retries == 2
  assert config.max_dropped_rows == 10


@pytest.mark.parametrize(("skipped", "failed"), [(1, 0), (0, 1)])
def test_langsmith_export_command_returns_nonzero_for_dropped_rows(
    skipped: int, failed: int
) -> None:
  stats = ExportStats(
      rows_read=1,
      exported=0,
      skipped=skipped,
      failed=failed,
      traces_exported=0,
      batches=1,
      dropped_rows=(),
  )
  with patch("bigquery_agent_analytics.export.export", return_value=stats):
    result = runner.invoke(
        app,
        ["export", "langsmith", "--source=project.dataset.events"],
    )

  assert result.exit_code == 1
  assert json.loads(result.stdout)["skipped"] == skipped
  assert json.loads(result.stdout)["failed"] == failed


def test_langsmith_export_help_is_nested() -> None:
  result = runner.invoke(app, ["export", "langsmith", "--help"])
  root_command = get_command(app)
  export_command = root_command.get_command(None, "export")
  assert export_command is not None
  langsmith_command = export_command.get_command(None, "langsmith")
  assert langsmith_command is not None
  option_names = {
      option
      for parameter in langsmith_command.params
      for option in getattr(parameter, "opts", ())
  }
  help_text = click.unstyle(result.stdout)

  assert result.exit_code == 0
  assert "--source" in help_text
  assert "--mapping" in help_text
  assert "--incremental" in help_text
  assert "--max-dropped-rows" in option_names
  assert "--langsmith-api-key" not in option_names


def test_langsmith_export_rejects_api_key_option() -> None:
  result = runner.invoke(
      app,
      [
          "export",
          "langsmith",
          "--source=project.dataset.events",
          "--langsmith-api-key=must-not-enter-argv",
      ],
  )

  assert result.exit_code != 0
  assert "No such option" in result.stderr
  assert "must-not-enter-argv" not in result.stderr


def test_langsmith_export_preserves_typer_datetime_validation() -> None:
  result = runner.invoke(
      app,
      [
          "export",
          "langsmith",
          "--source=project.dataset.events",
          "--since=not-a-timestamp",
      ],
  )

  assert result.exit_code == 2
  assert "Invalid value" in result.stderr
  assert "--since must be an ISO-8601 timestamp" in result.stderr
  assert "Error:" not in result.stderr


def test_langsmith_export_does_not_mask_unexpected_programming_errors() -> None:
  with patch(
      "bigquery_agent_analytics.export.export",
      side_effect=RuntimeError("unexpected implementation bug"),
  ):
    result = runner.invoke(
        app,
        ["export", "langsmith", "--source=project.dataset.events"],
    )

  assert result.exit_code == 1
  assert isinstance(result.exception, RuntimeError)
  assert "Error: unexpected implementation bug" not in result.output


def test_langsmith_export_reports_google_operational_errors() -> None:
  with patch(
      "bigquery_agent_analytics.export.export",
      side_effect=google_exceptions.ServiceUnavailable("service unavailable"),
  ):
    result = runner.invoke(
        app,
        ["export", "langsmith", "--source=project.dataset.events"],
    )

  assert result.exit_code == 2
  assert "Error: 503 service unavailable" in result.stderr


def test_langsmith_export_reports_malformed_mapping_yaml(
    tmp_path: Path,
) -> None:
  mapping_path = tmp_path / "invalid.yaml"
  mapping_path.write_text("fields: [", encoding="utf-8")

  result = runner.invoke(
      app,
      [
          "export",
          "langsmith",
          "--source=project.dataset.events",
          f"--mapping={mapping_path}",
      ],
  )

  assert result.exit_code == 2
  assert "Error:" in result.stderr
  assert result.exception is not None


def test_langsmith_export_is_not_added_to_bqaa() -> None:
  result = runner.invoke(bqaa_app, ["--help"])

  assert result.exit_code == 0
  assert "langsmith" not in result.stdout.lower()
  assert "export" not in result.stdout.lower()


def test_export_package_exposes_stable_public_surface() -> None:
  assert callable(export)
  assert ExportConfig.__name__ == "ExportConfig"
  assert ExportStats.__name__ == "ExportStats"
  assert FieldMapping.__name__ == "FieldMapping"
  assert DroppedRow.__name__ == "DroppedRow"
