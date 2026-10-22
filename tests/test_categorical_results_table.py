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

"""Categorical results and view names stay one name inside their quoted path.

``Client.evaluate_categorical`` with ``persist_results=True`` formats
``config.results_table`` (else ``categorical_results``) into the results
table's ``CREATE TABLE`` and ``ALTER TABLE`` statements and streams rows
into it. ``create_categorical_views`` and ``CategoricalViewManager`` format
the results table and every view name (``view_prefix`` followed by the
view) into ``CREATE OR REPLACE VIEW`` statements. A backtick in any of
them closed the quoted path, so the rest became SQL: a second statement
that copied another table, or a join on one. Each is now refused before
any query or DDL unless it is one table name, and so are the view
manager's project and dataset IDs (Client's checks).
"""

import json
from unittest.mock import MagicMock

import pytest

from bigquery_agent_analytics import Client
from bigquery_agent_analytics.categorical_evaluator import CATEGORICAL_RESULTS_MIGRATIONS
from bigquery_agent_analytics.categorical_evaluator import CategoricalEvaluationConfig
from bigquery_agent_analytics.categorical_evaluator import CategoricalMetricCategory
from bigquery_agent_analytics.categorical_evaluator import CategoricalMetricDefinition
from bigquery_agent_analytics.categorical_views import _VIEW_CREATION_ORDER
from bigquery_agent_analytics.categorical_views import CategoricalViewManager
from tests.test_client_project_dataset_ids import _NOT_A_DATASET_ID
from tests.test_client_project_dataset_ids import _NOT_A_PROJECT_ID
from tests.test_client_table_id import _NOT_A_TABLE_NAME

# The review's payload: it turns the CREATE TABLE into a copy of another
# table and starts a second statement.
_TWO_STATEMENTS = (
    "categorical_results` AS SELECT secret FROM `p.private.secrets`; "
    "CREATE TABLE IF NOT EXISTS `p.d.safe_copy"
)
# The review's view payload: it joins another table into each view.
_VIEW_CROSS_JOIN = (
    "categorical_results` AS results CROSS JOIN `p.private.secrets`"
    " AS injected --"
)

# Results tables to refuse. An empty or None results_table means the
# default, so only the other values can name a table.
_NOT_A_RESULTS_TABLE = {
    "two_statements": _TWO_STATEMENTS,
    "view_cross_join": _VIEW_CROSS_JOIN,
    **{name: value for name, value in _NOT_A_TABLE_NAME.items() if value},
}

# View prefixes that make some view name more than one table name.
_NOT_A_VIEW_PREFIX = {
    "backtick": "adk`",
    "cross_join": "adk_` AS v CROSS JOIN `p.private.secrets` AS injected --",
    "dotted": "private.adk_",
    "backslash": "adk\\",
    "line_break": "adk\n",
    "quote": "adk'",
    "semicolon": "adk;",
    "hash_comment": "adk#",
    "block_comment": "adk/*",
    "wildcard": "adk*",
    "decorator": "adk$",
}

# The config's results_table is typed str, so pydantic turns bytes into a
# str itself; only strings reach the check through a config.
_NOT_A_CONFIG_RESULTS_TABLE = {
    name: value
    for name, value in _NOT_A_RESULTS_TABLE.items()
    if isinstance(value, str)
}

_RESULTS_TABLE_ERROR = "^results_table must be one table name"
_VIEW_NAME_ERROR = "^view name must be one table name"

# Results tables that must keep working.
_RESULTS_TABLES = [
    "my_results",
    "categorical-results_v2",
    "categorical results 2026",
    "résultats",
]

_ROW = {
    "session_id": "s1",
    "transcript": "USER: Hello",
    "classifications": json.dumps(
        [{"metric_name": "tone", "category": "positive"}]
    ),
}


def _config(**overrides):
  return CategoricalEvaluationConfig(
      metrics=[
          CategoricalMetricDefinition(
              name="tone",
              definition="Overall tone.",
              categories=[
                  CategoricalMetricCategory(
                      name="positive", definition="Happy."
                  ),
                  CategoricalMetricCategory(
                      name="negative", definition="Upset."
                  ),
              ],
          )
      ],
      **overrides,
  )


def _bq_client(on_evaluate=None):
  """Fake BigQuery client: AI.GENERATE finds one session, DDL succeeds.

  *on_evaluate* runs when the AI.GENERATE query is sent.
  """

  def query(sql, job_config=None):
    job = MagicMock()
    if "AI.GENERATE(" in sql:
      if on_evaluate:
        on_evaluate()
      job.result.return_value = [_ROW]
    else:
      job.result.return_value = []
    return job

  bq = MagicMock()
  bq.query.side_effect = query
  bq.insert_rows_json.return_value = []
  return bq


def _client(bq):
  return Client(
      project_id="p", dataset_id="d", verify_schema=False, bq_client=bq
  )


def _sent_sql(bq):
  return [call.args[0] for call in bq.query.call_args_list]


def _view_manager(bq=None, **options):
  return CategoricalViewManager(
      project_id="p", dataset_id="d", bq_client=bq or MagicMock(), **options
  )


class TestPersistedResultsTable:
  """``evaluate_categorical`` checks the results table it writes."""

  @pytest.mark.parametrize(
      "results_table",
      list(_NOT_A_CONFIG_RESULTS_TABLE.values()),
      ids=list(_NOT_A_CONFIG_RESULTS_TABLE),
  )
  def test_refuses_results_table_before_any_query(self, results_table):
    bq = _bq_client()
    config = _config(persist_results=True, results_table=results_table)
    with pytest.raises(ValueError, match=_RESULTS_TABLE_ERROR):
      _client(bq).evaluate_categorical(config=config)
    assert _sent_sql(bq) == []
    bq.insert_rows_json.assert_not_called()

  def test_results_table_changed_during_evaluation_is_refused_at_ddl(self):
    # Checked again where the DDL is built. The refusal is reported like
    # any persistence error, so the evaluation keeps its results.
    config = _config(persist_results=True, results_table="my_results")

    def change():
      config.results_table = _TWO_STATEMENTS

    bq = _bq_client(on_evaluate=change)
    report = _client(bq).evaluate_categorical(config=config)
    assert [sql for sql in _sent_sql(bq) if "AI.GENERATE(" not in sql] == []
    bq.insert_rows_json.assert_not_called()
    assert report.details["persisted"] is False
    assert report.details["persist_error"].startswith(
        "results_table must be one table name"
    )

  def test_results_table_is_unused_without_persistence(self):
    bq = _bq_client()
    config = _config(results_table=_TWO_STATEMENTS)
    _client(bq).evaluate_categorical(config=config)
    assert not any("secrets" in sql for sql in _sent_sql(bq))
    bq.insert_rows_json.assert_not_called()

  @pytest.mark.parametrize("results_table", [None] + _RESULTS_TABLES)
  def test_valid_results_table_reaches_ddl_and_insert(self, results_table):
    bq = _bq_client()
    config = _config(persist_results=True, results_table=results_table)
    report = _client(bq).evaluate_categorical(config=config)
    name = results_table or "categorical_results"
    [_, create, *migrations] = _sent_sql(bq)
    assert f"CREATE TABLE IF NOT EXISTS `p.d.{name}`" in create
    assert len(migrations) == len(CATEGORICAL_RESULTS_MIGRATIONS)
    assert all(f"`p.d.{name}`" in sql for sql in migrations)
    assert bq.insert_rows_json.call_args.args[0] == f"p.d.{name}"
    assert report.details["persisted"] is True


class TestCreateCategoricalViews:
  """``Client.create_categorical_views`` refuses bad names before DDL."""

  @pytest.mark.parametrize(
      "results_table",
      list(_NOT_A_RESULTS_TABLE.values()),
      ids=list(_NOT_A_RESULTS_TABLE),
  )
  def test_refuses_results_table(self, results_table):
    bq = _bq_client()
    with pytest.raises(ValueError, match=_RESULTS_TABLE_ERROR):
      _client(bq).create_categorical_views(results_table=results_table)
    assert _sent_sql(bq) == []

  @pytest.mark.parametrize(
      "view_prefix",
      list(_NOT_A_VIEW_PREFIX.values()),
      ids=list(_NOT_A_VIEW_PREFIX),
  )
  def test_refuses_view_prefix(self, view_prefix):
    bq = _bq_client()
    with pytest.raises(ValueError, match=_VIEW_NAME_ERROR):
      _client(bq).create_categorical_views(view_prefix=view_prefix)
    assert _sent_sql(bq) == []

  @pytest.mark.parametrize("view_prefix", ["", "adk_", "team-a "])
  @pytest.mark.parametrize("results_table", [None, "my_results"])
  def test_valid_names_create_every_view(self, results_table, view_prefix):
    bq = _bq_client()
    created = _client(bq).create_categorical_views(
        results_table=results_table, view_prefix=view_prefix
    )
    assert created == {
        view: f"{view_prefix}{view}" for view in _VIEW_CREATION_ORDER
    }
    sent = _sent_sql(bq)
    assert len(sent) == len(_VIEW_CREATION_ORDER)
    for view, sql in zip(_VIEW_CREATION_ORDER, sent):
      assert f"CREATE OR REPLACE VIEW `p.d.{view_prefix}{view}`" in sql
    table = results_table or "categorical_results"
    assert f"`p.d.{table}`" in sent[0]


class TestCategoricalViewManager:
  """The view manager checks its names when built and when assigned."""

  @pytest.mark.parametrize(
      "field,value,error",
      [
          *[
              ("results_table", value, _RESULTS_TABLE_ERROR)
              for value in _NOT_A_RESULTS_TABLE.values()
          ],
          *[
              ("view_prefix", value, _VIEW_NAME_ERROR)
              for value in _NOT_A_VIEW_PREFIX.values()
          ],
          *[
              ("dataset_id", value, "^dataset_id must be one")
              for value in _NOT_A_DATASET_ID.values()
          ],
          *[
              ("project_id", value, "^project_id must be one")
              for value in _NOT_A_PROJECT_ID.values()
          ],
      ],
      ids=[
          *[f"results_table-{name}" for name in _NOT_A_RESULTS_TABLE],
          *[f"view_prefix-{name}" for name in _NOT_A_VIEW_PREFIX],
          *[f"dataset_id-{name}" for name in _NOT_A_DATASET_ID],
          *[f"project_id-{name}" for name in _NOT_A_PROJECT_ID],
      ],
  )
  def test_refuses_value_when_built_and_when_assigned(
      self, field, value, error
  ):
    options = {"project_id": "p", "dataset_id": "d", field: value}
    with pytest.raises(ValueError, match=error):
      CategoricalViewManager(**options, bq_client=MagicMock())
    manager = _view_manager()
    before = getattr(manager, field)
    with pytest.raises(ValueError, match=error):
      setattr(manager, field, value)
    assert getattr(manager, field) == before
    # The refused value left no trace in any view's SQL.
    fresh = _view_manager()
    assert [manager.get_view_sql(view) for view in _VIEW_CREATION_ORDER] == [
        fresh.get_view_sql(view) for view in _VIEW_CREATION_ORDER
    ]

  @pytest.mark.parametrize(
      "field,value",
      [
          ("dataset_id", "my-ds"),
          ("project_id", "example.com:my-project"),
          ("results_table", "categorical-results_v2"),
          ("view_prefix", "adk_"),
      ],
  )
  def test_valid_assignment_reaches_the_view_sql(self, field, value):
    manager = _view_manager()
    setattr(manager, field, value)
    sql = manager.get_view_sql(_VIEW_CREATION_ORDER[0])
    assert value in sql
