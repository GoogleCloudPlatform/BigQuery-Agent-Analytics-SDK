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

"""Discoverability contract for the EvalBench commands (issue #435).

The ``evalbench-import``, ``evalbench-failed-sessions`` and
``evalbench-score`` handlers are covered by their own test modules when
invoked by name. This module pins the *install surface* instead: an
operator who ``pip install``s the package and runs ``bq-agent-sdk --help``
must see the three commands, and the ``bq-agent-sdk`` console script must
still resolve to :func:`bigquery_agent_analytics.cli.main`. Offline only.
"""

from __future__ import annotations

from pathlib import Path

import click
import pytest
import typer.main
from typer.testing import CliRunner

from bigquery_agent_analytics import cli as cli_module
from bigquery_agent_analytics.cli import app

runner = CliRunner()

_EVALBENCH_COMMANDS = (
    "evalbench-import",
    "evalbench-failed-sessions",
    "evalbench-score",
)

_REPO_ROOT = Path(__file__).resolve().parent.parent


def test_root_help_lists_evalbench_commands() -> None:
  """``bq-agent-sdk --help`` shows every evalbench-* command."""
  result = runner.invoke(app, ["--help"])
  assert result.exit_code == 0, result.output
  for name in _EVALBENCH_COMMANDS:
    assert name in result.output, name


def test_evalbench_commands_registered_on_root_app() -> None:
  """The three handlers are registered (and visible) on the root app.

  Rich may wrap ``--help`` output at 80 columns on CI, so this asserts on
  the Click command registry rather than on help text alone.
  """
  # Typer's group type is not guaranteed to subclass ``click.Group``
  # across versions, so use the Click registry API by duck typing.
  root = typer.main.get_command(app)
  ctx = click.Context(root)
  registered = set(root.list_commands(ctx))
  for name in _EVALBENCH_COMMANDS:
    assert name in registered, sorted(registered)
    command = root.get_command(ctx, name)
    assert command is not None, name
    assert not command.hidden, name


def test_console_script_maps_bq_agent_sdk_to_cli_main() -> None:
  """The installed ``bq-agent-sdk`` script targets ``cli:main``.

  ``main()`` runs ``app`` — the Typer app the evalbench-* commands are
  registered on — so this is the link between ``pip install`` and
  ``bq-agent-sdk --help`` listing them.
  """
  try:
    import tomllib
  except ImportError:  # Python < 3.11 — pytest depends on tomli there
    import tomli as tomllib

  pyproject = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text())
  scripts = pyproject["project"]["scripts"]
  assert scripts["bq-agent-sdk"] == "bigquery_agent_analytics.cli:main"
  assert cli_module.main.__module__ == "bigquery_agent_analytics.cli"


def test_module_usage_block_documents_evalbench_commands() -> None:
  """The ``cli`` module docstring's Usage block names each command."""
  doc = cli_module.__doc__ or ""
  for name in _EVALBENCH_COMMANDS:
    assert f"bq-agent-sdk {name}" in doc, name


@pytest.mark.parametrize("name", _EVALBENCH_COMMANDS)
def test_evalbench_subcommand_help_exits_zero(name: str) -> None:
  """``bq-agent-sdk <evalbench-cmd> --help`` works without BigQuery.

  Only the exit code is asserted: Rich wraps and box-draws help output on
  CI, so option flags are not reliably greppable from the captured text.
  ``test_evalbench_subcommand_accepts_project_id`` checks the flag instead.
  """
  result = runner.invoke(app, [name, "--help"])
  assert result.exit_code == 0, result.output


@pytest.mark.parametrize("name", _EVALBENCH_COMMANDS)
def test_evalbench_subcommand_accepts_project_id(name: str) -> None:
  """Each evalbench-* command declares ``--project-id`` (Click registry)."""
  root = typer.main.get_command(app)
  ctx = click.Context(root)
  command = root.get_command(ctx, name)
  assert command is not None, name
  flags = {opt for param in command.get_params(ctx) for opt in param.opts}
  assert "--project-id" in flags, sorted(flags)
