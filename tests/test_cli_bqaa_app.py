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

"""Tests for the product-facing ``bqaa`` CLI surface (issue #245).

Three things to prove:

* The ``bqaa`` Typer app exposes ``context-graph`` as a subcommand.
* ``bqaa context-graph`` and ``bq-agent-sdk materialize-window`` reach
  the same underlying ``materialize_window`` handler with the same flag
  surface.
* ``bqaa-materialize-window`` (the deprecated standalone entry point)
  still works and prints a one-line deprecation notice to stderr.
"""

from __future__ import annotations

import sys
import textwrap

import pytest
from typer.testing import CliRunner

from bigquery_agent_analytics import cli as cli_module
from bigquery_agent_analytics.cli import _DEPRECATION_NOTICE
from bigquery_agent_analytics.cli import _print_deprecation_warning
from bigquery_agent_analytics.cli import app as bq_agent_sdk_app
from bigquery_agent_analytics.cli import bqaa_app

runner = CliRunner()


# ---------------------------------------------------------------------------
# bqaa app surface
# ---------------------------------------------------------------------------


def test_bqaa_help_lists_context_graph() -> None:
  """``bqaa --help`` shows ``context-graph`` in the Commands section."""
  result = runner.invoke(bqaa_app, ["--help"])
  assert result.exit_code == 0, result.output
  assert "context-graph" in result.output
  # The product framing is in the help text so customers see the
  # noun, not the implementation term.
  assert "context graph" in result.output.lower()


def test_bqaa_context_graph_exposes_expected_flags() -> None:
  """``bqaa context-graph`` exposes the materialize-window flags.

  Flags chosen here are the ones the codelab and blog actually document
  to customers, so a rename that accidentally drops one breaks the
  copy-paste path. The full ``materialize-window`` surface has more
  flags (``--dry-run``, ``--bundles-root``, internal opt-ins); those
  are not asserted here because they are dev-tooling and not part of
  the customer-facing surface that the rename has to preserve.

  This inspects the Click command's declared parameters rather than the
  rendered ``--help`` text. Rich wraps long option names to the
  terminal width and emits ANSI/box-drawing characters, so under CI's
  80-column non-TTY rendering a flag like ``--project-id`` is not a
  contiguous substring of the help output — scraping the text is
  brittle, inspecting params is deterministic.
  """
  from typer.main import get_command

  command = get_command(bqaa_app).get_command(None, "context-graph")  # type: ignore[arg-type]
  assert command is not None, "bqaa app missing 'context-graph' subcommand"

  declared_flags = set()
  for param in command.params:
    declared_flags.update(getattr(param, "opts", []))
    declared_flags.update(getattr(param, "secondary_opts", []))

  for flag in (
      "--project-id",
      "--dataset-id",
      "--ontology",
      "--binding",
      "--lookback-hours",
      "--overlap-minutes",
      "--max-sessions",
      "--backfill",
      "--from",
      "--to",
      "--state-key-suffix",
      "--extraction-mode",
      "--max-session-age-hours",
      "--format",
  ):
    assert (
        flag in declared_flags
    ), f"flag {flag!r} missing from `bqaa context-graph` params"


def test_bqaa_context_graph_uses_materialize_window_handler() -> None:
  """The ``context-graph`` subcommand is bound to the same callback the
  legacy ``materialize-window`` subcommand uses.

  Typer wraps each callback when it registers a command, so the two
  Click commands hold distinct wrapper objects even though both
  ultimately call the same source function. Compare by qualified
  name and module to assert "same handler" without being fooled by
  the wrappers.
  """
  from typer.main import get_command

  bqaa_click = get_command(bqaa_app)
  bq_click = get_command(bq_agent_sdk_app)

  bqaa_ctx_graph = bqaa_click.get_command(None, "context-graph")  # type: ignore[arg-type]
  bq_materialize = bq_click.get_command(None, "materialize-window")  # type: ignore[arg-type]

  assert (
      bqaa_ctx_graph is not None
  ), "bqaa app missing 'context-graph' subcommand"
  assert (
      bq_materialize is not None
  ), "bq-agent-sdk app missing 'materialize-window' subcommand"

  bqaa_cb = bqaa_ctx_graph.callback
  bq_cb = bq_materialize.callback
  assert bqaa_cb is not None and bq_cb is not None

  # Both wrappers should report the same source function identity.
  assert (
      bqaa_cb.__module__ == bq_cb.__module__
  ), f"different modules: {bqaa_cb.__module__} vs {bq_cb.__module__}"
  assert bqaa_cb.__qualname__ == bq_cb.__qualname__, (
      f"different qualnames: {bqaa_cb.__qualname__} vs" f" {bq_cb.__qualname__}"
  )
  # And both should be the live ``cli.materialize_window`` symbol.
  assert bqaa_cb.__qualname__ == "materialize_window"
  assert bqaa_cb.__module__.endswith("cli")


def test_bqaa_help_does_not_collapse_into_single_subcommand() -> None:
  """``bqaa --help`` should show a COMMAND-list shape, not promote the
  single subcommand's flags up to the top level.

  Regression guard: without the ``@bqaa_app.callback()`` shim, Typer
  auto-promotes a single subcommand's options to the parent, which
  would make ``bqaa --help`` confusingly identical to
  ``bqaa context-graph --help``.
  """
  result = runner.invoke(bqaa_app, ["--help"])
  assert result.exit_code == 0
  # The flags only appear under the subcommand; the parent help should
  # show a Commands section instead.
  assert "Commands" in result.output
  assert "--project-id" not in result.output


# ---------------------------------------------------------------------------
# Deprecated bqaa-materialize-window entry point
# ---------------------------------------------------------------------------


def test_deprecation_notice_mentions_replacement_command() -> None:
  """The deprecation notice points users at the new command name."""
  assert "bqaa-materialize-window" in _DEPRECATION_NOTICE
  assert "bqaa context-graph" in _DEPRECATION_NOTICE
  assert "deprecated" in _DEPRECATION_NOTICE.lower()


def test_print_deprecation_warning_goes_to_stderr(
    capsys: pytest.CaptureFixture[str],
) -> None:
  """``_print_deprecation_warning`` writes to stderr only.

  Keeps the deprecation noise off stdout so scripts that pipe the
  CLI's JSON output ``| jq`` are not broken by the migration.
  """
  _print_deprecation_warning()
  captured = capsys.readouterr()
  assert captured.out == ""
  assert "bqaa-materialize-window" in captured.err
  assert "bqaa context-graph" in captured.err


def test_materialize_window_entry_prints_deprecation_then_runs(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
  """``_materialize_window_entry`` calls the deprecation notice before
  handing off to the click command.

  Uses a monkeypatched ``click_cmd`` substitute so the test does not
  actually invoke the materializer; we only need to prove the
  sequencing.
  """
  called: list[str] = []

  def fake_get_command_from_info(info, **kwargs):  # noqa: ANN001, ARG001
    def fake_click_cmd() -> None:
      called.append("click_cmd")

    return fake_click_cmd

  # Patch the typer.main import path used inside _materialize_window_entry.
  import typer.main as typer_main

  monkeypatch.setattr(
      typer_main, "get_command_from_info", fake_get_command_from_info
  )

  cli_module._materialize_window_entry()
  captured = capsys.readouterr()
  assert captured.err.strip() == _DEPRECATION_NOTICE
  assert called == [
      "click_cmd"
  ], "the click command was not invoked after the deprecation print"


# ---------------------------------------------------------------------------
# pyproject.toml entry-point sanity
# ---------------------------------------------------------------------------


def test_pyproject_declares_bqaa_console_script() -> None:
  """``pyproject.toml`` should ship the new ``bqaa`` console script
  pointing at ``cli.bqaa_main``.

  Belt-and-suspenders check so the entry point doesn't silently drop
  out of the package distribution.
  """
  from pathlib import Path

  pyproject = (
      Path(__file__).resolve().parents[1] / "pyproject.toml"
  ).read_text(encoding="utf-8")
  expected = textwrap.dedent(
      """\
      bqaa = "bigquery_agent_analytics.cli:bqaa_main"
      """
  ).strip()
  assert expected in pyproject, (
      "Expected entry-point declaration missing from pyproject.toml; the"
      " 'bqaa' console script will not be installed"
  )
