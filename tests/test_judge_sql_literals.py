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

"""The judge's endpoint and connection stay inside their quoted SQL tokens.

``Client.evaluate(LLMAsJudge(...))`` renders the AI.GENERATE judge query
with the client's ``endpoint`` and ``connection_id`` between single
quotes, and sends an endpoint with two or more dots to the legacy
ML.GENERATE_TEXT judge as ``MODEL `{endpoint}```. Both values are client
configuration, which the BigQuery Remote Function takes from the
request's ``userDefinedContext``. A quote or backslash in either value
used to end its token early, so the rest of the value became SQL, such as
a second ``request_type => 'SHARED'`` argument.
"""

from unittest.mock import MagicMock

import pytest

from bigquery_agent_analytics import Client
from bigquery_agent_analytics import LLMAsJudge
from bigquery_agent_analytics import TraceFilter
from bigquery_agent_analytics.evaluators import render_ai_generate_judge_query

# What follows each literal in the AI.GENERATE judge template.
_THEN_CONNECTION = ",\n      connection_id => '"
_THEN_MODEL_PARAMS = ",\n      model_params => JSON '"
# What follows the model reference in the legacy judge template.
_THEN_STRUCT = ",\n    STRUCT(\n"

# Each value ended the endpoint literal early, or changed its value, in the
# old renderer. The first is the audit's: it adds a second AI.GENERATE
# argument. One dot keeps them on the AI.GENERATE path.
_ENDPOINT_PAYLOADS = {
    "quote_adds_argument": "gemini-2.5-flash', request_type => 'SHARED",
    # Caught only if backslashes are escaped before quotes.
    "backslash_quote": "gemini-2.5-flash\\', request_type => \\'SHARED",
    "trailing_backslash": "gemini-2.5-flash\\",
    "line_break": "gemini-2.5-flash',\n      request_type => 'SHARED",
    "carriage_return": "gemini-2.5-flash" + chr(13) + "x",
    "tab": "gemini-2.5-flash" + chr(9),
    "nul": "gemini-2.5-flash" + chr(0),
    "delete": "gemini-2.5-flash" + chr(0x7F),
}

_CONNECTION_PAYLOADS = {
    "quote_adds_argument": (
        "audit-project.us.connection', request_type => 'SHARED"
    ),
    "backslash_quote": "proj.us.conn\\', request_type => \\'SHARED",
    "trailing_backslash": "proj.us.conn\\",
    "line_break": "proj.us.conn',\n      request_type => 'SHARED",
    "bell": "proj.us.conn" + chr(7),
}

# Two dots send these to the legacy judge's backtick-quoted MODEL slot.
_LEGACY_MODEL_PAYLOADS = {
    "backtick_adds_sql": (
        "proj.ds.judge_model`, (SELECT 'forged' AS prompt)) --"
    ),
    # Caught only if backslashes are escaped before backticks.
    "backslash_backtick": "proj.ds.judge_model\\`x",
    "trailing_backslash": "proj.ds.judge_model\\",
    "line_break": "proj.ds.judge_model`\n--",
}

# How GoogleSQL decodes the escapes the renderer may emit.
_NAMED_ESCAPES = {
    "\\": "\\",
    "'": "'",
    '"': '"',
    "`": "`",
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "b": "\b",
    "f": "\f",
}


def _read_quoted(sql, start):
  """Decodes the GoogleSQL quoted token that opens at ``sql[start]``.

  Returns the token's value and the index just past its closing quote. A
  raw control character can't be part of one token, so it fails the test,
  as does an escape GoogleSQL rejects or a token that never closes.
  """
  quote = sql[start]
  chars = []
  index = start + 1
  while index < len(sql):
    char = sql[index]
    if char == quote:
      return "".join(chars), index + 1
    if ord(char) < 0x20 or ord(char) == 0x7F:
      pytest.fail(f"raw {char!r} inside the token at {start}: {sql!r}")
    if char != "\\":
      chars.append(char)
      index += 1
    elif sql[index + 1 : index + 2] == "x":
      chars.append(chr(int(sql[index + 2 : index + 4], 16)))
      index += 4
    elif sql[index + 1 : index + 2] in _NAMED_ESCAPES:
      chars.append(_NAMED_ESCAPES[sql[index + 1]])
      index += 2
    else:
      pytest.fail(f"illegal escape at {index}: {sql!r}")
  pytest.fail(f"the token at {start} never closes: {sql!r}")


def _token_after(sql, marker):
  """Decodes the quoted token that follows the first *marker*."""
  return _read_quoted(sql, sql.index(marker) + len(marker))


def _judge_sql(endpoint, connection_id=None):
  """The one query ``Client.evaluate`` sends for an LLM judge."""
  bq = MagicMock()
  bq.query.return_value.result.return_value = []
  client = Client(
      project_id="p",
      dataset_id="d",
      verify_schema=False,
      bq_client=bq,
      endpoint=endpoint,
      connection_id=connection_id,
  )
  client.evaluate(
      evaluator=LLMAsJudge.correctness(), filters=TraceFilter(limit=1)
  )
  [call] = bq.query.call_args_list
  return call.args[0]


class TestAIGenerateJudge:
  """``endpoint => '...'`` and ``connection_id => '...'`` stay one literal."""

  @pytest.mark.parametrize(
      "endpoint",
      list(_ENDPOINT_PAYLOADS.values()),
      ids=list(_ENDPOINT_PAYLOADS),
  )
  def test_endpoint_stays_one_string_literal(self, endpoint):
    sql = _judge_sql(endpoint)
    assert "AI.GENERATE(" in sql
    value, end = _token_after(sql, "endpoint => ")
    assert value == endpoint
    assert sql[end:].startswith(_THEN_MODEL_PARAMS)

  @pytest.mark.parametrize(
      "connection_id",
      list(_CONNECTION_PAYLOADS.values()),
      ids=list(_CONNECTION_PAYLOADS),
  )
  def test_connection_id_stays_one_string_literal(self, connection_id):
    sql = _judge_sql("gemini-2.5-flash", connection_id)
    value, end = _token_after(sql, "endpoint => ")
    assert value == "gemini-2.5-flash"
    assert sql[end:].startswith(_THEN_CONNECTION)
    value, end = _token_after(sql, "connection_id => ")
    assert value == connection_id
    assert sql[end:].startswith(_THEN_MODEL_PARAMS)

  def test_audit_payloads_render_escaped(self):
    sql = _judge_sql(
        _ENDPOINT_PAYLOADS["quote_adds_argument"],
        _CONNECTION_PAYLOADS["quote_adds_argument"],
    )
    assert (
        "endpoint => 'gemini-2.5-flash\\', request_type => \\'SHARED',\n"
        "      connection_id => 'audit-project.us.connection\\',"
        " request_type => \\'SHARED',\n"
        "      model_params => JSON '"
    ) in sql

  @pytest.mark.parametrize(
      "connection_id",
      [None, "us.my-conn", "project.us.conn", "my-project.us.my_connection"],
  )
  @pytest.mark.parametrize(
      "endpoint",
      ["gemini-2.5-flash", "gemini-2.5-pro", "gemini-3-flash-preview"],
  )
  def test_valid_endpoint_and_connection_render_unchanged(
      self, endpoint, connection_id
  ):
    sql = _judge_sql(endpoint, connection_id)
    expected = f"endpoint => '{endpoint}',"
    if connection_id:
      expected += f"\n      connection_id => '{connection_id}',"
    assert expected + "\n      model_params => JSON '" in sql


class TestDeprecatedRenderer:
  """The public ``render_ai_generate_judge_query`` gets the same encoding."""

  @staticmethod
  def _render(endpoint, connection_id=None):
    with pytest.deprecated_call():
      return render_ai_generate_judge_query(
          project="p",
          dataset="d",
          table="agent_events",
          where="TRUE",
          endpoint=endpoint,
          connection_id=connection_id,
      )

  def test_escapes_endpoint_and_connection(self):
    endpoint = _ENDPOINT_PAYLOADS["trailing_backslash"]
    connection_id = _CONNECTION_PAYLOADS["quote_adds_argument"]
    sql = self._render(endpoint, connection_id)
    value, end = _token_after(sql, "endpoint => ")
    assert value == endpoint
    assert sql[end:].startswith(_THEN_CONNECTION)
    value, end = _token_after(sql, "connection_id => ")
    assert value == connection_id
    assert sql[end:].startswith(_THEN_MODEL_PARAMS)

  def test_full_endpoint_url_renders_unchanged(self):
    url = (
        "https://aiplatform.googleapis.com/v1/projects/p/locations/global"
        "/publishers/google/models/gemini-3-flash-preview"
    )
    sql = self._render(url, "us.my-conn")
    assert (
        f"endpoint => '{url}',\n      connection_id => 'us.my-conn',\n" in sql
    )


class TestLegacyJudge:
  """A legacy model endpoint stays one backtick-quoted identifier."""

  @pytest.mark.parametrize(
      "endpoint",
      list(_LEGACY_MODEL_PAYLOADS.values()),
      ids=list(_LEGACY_MODEL_PAYLOADS),
  )
  def test_model_reference_stays_one_quoted_identifier(self, endpoint):
    sql = _judge_sql(endpoint)
    assert "ML.GENERATE_TEXT(" in sql
    value, end = _token_after(sql, "MODEL ")
    assert value == endpoint
    assert sql[end:].startswith(_THEN_STRUCT)

  @pytest.mark.parametrize(
      "endpoint",
      [
          "proj.ds.my_model",
          "my-project.my_dataset.my_model",
          "example.com:my-project.my_dataset.my_model",
      ],
  )
  def test_valid_model_reference_renders_unchanged(self, endpoint):
    sql = _judge_sql(endpoint)
    assert f"MODEL `{endpoint}`{_THEN_STRUCT}" in sql
