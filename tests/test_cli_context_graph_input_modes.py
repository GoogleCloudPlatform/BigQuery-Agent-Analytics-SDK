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

The command must accept exactly one of (--ontology + --binding) or
--property-graph. These checks fail fast at the CLI boundary, before any
BigQuery work, so they run offline.
"""

from __future__ import annotations

from typer.testing import CliRunner

from bigquery_agent_analytics.cli import bqaa_app

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

# Widen the virtual terminal so the rich-rendered usage error is not wrapped
# across lines, keeping message substrings contiguous for assertions.
_WIDE = {"COLUMNS": "200"}


def test_rejects_both_property_graph_and_separated() -> None:
  result = runner.invoke(
      bqaa_app,
      _BASE
      + [
          "--property-graph",
          "graph.sql",
          "--ontology",
          "o.yaml",
          "--binding",
          "b.yaml",
      ],
      env=_WIDE,
  )
  assert result.exit_code != 0
  assert "not both" in result.output


def test_rejects_neither_mode() -> None:
  result = runner.invoke(bqaa_app, _BASE, env=_WIDE)
  assert result.exit_code != 0
  assert "--property-graph" in result.output


def test_rejects_ontology_without_binding() -> None:
  result = runner.invoke(bqaa_app, _BASE + ["--ontology", "o.yaml"], env=_WIDE)
  assert result.exit_code != 0
