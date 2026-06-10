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

"""`bqaa context-graph` input-mode validation (issue #277, PR 4).

Exactly one of (--ontology + --binding) or --property-graph must be supplied.
The rule lives in a pure helper so it is asserted directly on the raised
exception message (stable), not on Rich-rendered CLI output (which wraps
differently under CI's non-TTY width). A thin CliRunner smoke confirms the
wiring exits non-zero.
"""

from __future__ import annotations

from unittest import mock

import pytest
import typer
from typer.testing import CliRunner

from bigquery_agent_analytics.cli import _validate_context_graph_input_mode
from bigquery_agent_analytics.cli import bqaa_app

# --------------------------------------------------------------------------- #
# Pure helper (stable message assertions)
# --------------------------------------------------------------------------- #


def test_rejects_both_modes() -> None:
  with pytest.raises(typer.BadParameter, match="exactly one"):
    _validate_context_graph_input_mode("o.yaml", "b.yaml", "g.sql")


def test_rejects_neither_mode() -> None:
  with pytest.raises(typer.BadParameter, match="Provide --graph"):
    _validate_context_graph_input_mode(None, None, None)


def test_rejects_ontology_without_binding() -> None:
  with pytest.raises(typer.BadParameter):
    _validate_context_graph_input_mode("o.yaml", None, None)


def test_rejects_property_graph_with_partial_separated() -> None:
  with pytest.raises(typer.BadParameter, match="exactly one"):
    _validate_context_graph_input_mode("o.yaml", None, "g.sql")


def test_accepts_separated_mode() -> None:
  _validate_context_graph_input_mode("o.yaml", "b.yaml", None)  # no raise


def test_accepts_property_graph_mode() -> None:
  _validate_context_graph_input_mode(None, None, "g.sql")  # no raise


def test_accepts_graph_mode() -> None:
  _validate_context_graph_input_mode(None, None, None, "my_graph")  # no raise


def test_rejects_graph_with_property_graph() -> None:
  with pytest.raises(typer.BadParameter, match="exactly one"):
    _validate_context_graph_input_mode(None, None, "g.sql", "my_graph")


def test_rejects_graph_with_separated() -> None:
  with pytest.raises(typer.BadParameter, match="exactly one"):
    _validate_context_graph_input_mode("o.yaml", "b.yaml", None, "my_graph")


# --------------------------------------------------------------------------- #
# CliRunner smoke (exit code only; no rendered-output substring assertions)
# --------------------------------------------------------------------------- #

runner = CliRunner()
_BASE = [
    "context-graph",
    "--project-id",
    "p",
    "--dataset-id",
    "d",
    "--lookback-hours",
    "1",
]


def test_cli_exits_nonzero_when_no_input_mode() -> None:
  result = runner.invoke(bqaa_app, _BASE)
  assert result.exit_code != 0


def test_cli_exits_nonzero_when_both_modes() -> None:
  result = runner.invoke(
      bqaa_app,
      _BASE + ["--property-graph", "g.sql", "--ontology", "o.yaml"],
  )
  assert result.exit_code != 0


# --------------------------------------------------------------------------- #
# --endpoint forwarding: the operator's AI.GENERATE model selection must reach
# run_materialize_window, else `bqaa context-graph` users stay pinned to the
# SDK default regardless of --endpoint.
# --------------------------------------------------------------------------- #


class _FakeResult:
  ok = True

  def to_json(self):
    return {}


def _invoke_capturing_endpoint(extra_args):
  captured: dict = {}

  def _fake_run(**kwargs):
    captured.update(kwargs)
    return _FakeResult()

  with mock.patch(
      "bigquery_agent_analytics.materialize_window.run_materialize_window",
      _fake_run,
  ):
    result = runner.invoke(
        bqaa_app, _BASE + ["--property-graph", "g.sql"] + extra_args
    )
  assert result.exit_code == 0, result.output
  return captured["endpoint"]


def test_cli_endpoint_forwarded() -> None:
  assert (
      _invoke_capturing_endpoint(["--endpoint", "gemini-3.5-flash"])
      == "gemini-3.5-flash"
  )


def test_cli_endpoint_defaults_to_gemini_25_flash() -> None:
  assert _invoke_capturing_endpoint([]) == "gemini-2.5-flash"
