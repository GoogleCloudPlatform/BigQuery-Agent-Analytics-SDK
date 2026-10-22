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

"""``BigQueryAIClient.generate_text`` formats only numbers into SQL.

``generate_text`` formats ``temperature`` and ``max_tokens`` into the
AI.GENERATE query's ``model_params => JSON '...'`` literal. A string such
as ``0.3}', request_type => 'SHARED`` ended the literal, so the rest of
the value became SQL, and an ``int`` or ``float`` subclass could format
itself as anything. ``temperature`` is now coerced to a ``float`` and
``max_tokens`` to an ``int`` before the query is formatted. Anything
else, including ``bool``, NaN and infinities, raises ``ValueError``
naming the field, and no query is sent.
"""

import asyncio
from fractions import Fraction
from unittest.mock import MagicMock

import pytest

from bigquery_agent_analytics.ai_ml_integration import BigQueryAIClient

_INJECTED = "', request_type => 'SHARED', model_params => JSON '{"


class _FloatThatFormatsAsSql(float):
  """A float whose ``format()`` text closes the JSON literal."""

  def __format__(self, spec):
    return "0.3}" + _INJECTED


class _IntThatFormatsAsSql(int):
  """An int whose ``format()`` text closes the JSON literal."""

  def __format__(self, spec):
    return "1024}" + _INJECTED


_NOT_A_TEMPERATURE = {
    "sql_string": "0.3}" + _INJECTED,
    "numeric_string": "0.3",
    "none": None,
    "true": True,
    "false": False,
    "nan": float("nan"),
    "inf": float("inf"),
    "negative_inf": float("-inf"),
    "huge_int": 10**400,
    "list": [0.3],
}

_NOT_A_MAX_TOKENS = {
    "sql_string": "1024}" + _INJECTED,
    "numeric_string": "1024",
    "none": None,
    "true": True,
    "false": False,
    "nan": float("nan"),
    "inf": float("inf"),
    "fractional": 1024.5,
    "list": [1024],
}


def _generate(bq, **kwargs):
  ai = BigQueryAIClient(project_id="p", dataset_id="d", client=bq)
  return asyncio.run(ai.generate_text("hello", **kwargs))


def _bq_client():
  bq = MagicMock()
  bq.query.return_value.result.return_value = []
  return bq


def _model_params(**kwargs):
  """The ``model_params`` argument of the one query sent."""
  bq = _bq_client()
  _generate(bq, **kwargs)
  [call] = bq.query.call_args_list
  sql = call.args[0]
  start = sql.index("model_params => ")
  return sql[start : sql.index("\n", start)]


def _rendered(temperature, max_tokens):
  return (
      "model_params => JSON"
      f' \'{{"temperature": {temperature}, "max_output_tokens": {max_tokens}}}\''
  )


class TestTemperature:

  @pytest.mark.parametrize(
      "temperature",
      list(_NOT_A_TEMPERATURE.values()),
      ids=list(_NOT_A_TEMPERATURE),
  )
  def test_rejects_value_that_is_not_a_finite_number(self, temperature):
    bq = _bq_client()
    with pytest.raises(ValueError, match="^temperature must be"):
      _generate(bq, temperature=temperature)
    bq.query.assert_not_called()

  @pytest.mark.parametrize(
      "temperature,rendered",
      [
          (0.3, "0.3"),
          (0, "0.0"),
          (1, "1.0"),
          (Fraction(1, 4), "0.25"),
          (_FloatThatFormatsAsSql(0.5), "0.5"),
      ],
      ids=["float", "zero", "one", "fraction", "float_subclass"],
  )
  def test_renders_as_a_float(self, temperature, rendered):
    assert _model_params(temperature=temperature) == _rendered(rendered, 1024)


class TestMaxTokens:

  @pytest.mark.parametrize(
      "max_tokens",
      list(_NOT_A_MAX_TOKENS.values()),
      ids=list(_NOT_A_MAX_TOKENS),
  )
  def test_rejects_value_that_is_not_a_whole_number(self, max_tokens):
    bq = _bq_client()
    with pytest.raises(ValueError, match="^max_tokens must be"):
      _generate(bq, max_tokens=max_tokens)
    bq.query.assert_not_called()

  @pytest.mark.parametrize(
      "max_tokens,rendered",
      [
          (1024, "1024"),
          (512.0, "512"),
          (_IntThatFormatsAsSql(256), "256"),
      ],
      ids=["int", "whole_float", "int_subclass"],
  )
  def test_renders_as_an_int(self, max_tokens, rendered):
    assert _model_params(max_tokens=max_tokens) == _rendered(0.3, rendered)


def test_defaults_render_unchanged():
  assert _model_params() == _rendered(0.3, 1024)
