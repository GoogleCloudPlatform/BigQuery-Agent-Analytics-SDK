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

"""Hermetic tests for the analyst agent, its scenario and consolidation.

No network: BigQuery is replaced by small fakes that record the statements
and parameters they receive, and the memory read path runs on the offline
fixture through the real SDK ``Client``.
"""

from __future__ import annotations

import concurrent.futures
from datetime import date
from datetime import datetime
from datetime import timezone
import decimal
from pathlib import Path
import sys
from types import SimpleNamespace

from google.api_core import exceptions
import pytest

from bigquery_agent_analytics import Client

EXAMPLE_DIR = Path(__file__).resolve().parents[2] / "examples" / "agent_memory"
sys.path.insert(0, str(EXAMPLE_DIR))

import analyst_agent  # noqa: E402
import analyst_scenario as scenario  # noqa: E402
import memory_consolidation  # noqa: E402
import memory_layers  # noqa: E402
import offline_bigquery  # noqa: E402

UTC = timezone.utc
TABLES = memory_consolidation.MemoryTables.in_dataset(
    "p", "d", events="analyst_events", prefix="analyst_"
)


def _params(job_config):
  return {
      p.name: getattr(p, "value", None) or getattr(p, "values", None)
      for p in job_config.query_parameters
  }


# ---- the scenario -----------------------------------------------------------


def test_scenario_days_are_business_days_of_one_month():
  dates = [date.fromisoformat(d.date) for d in scenario.DAYS]

  assert dates == sorted(dates)
  assert [d.weekday for d in scenario.DAYS] == [x.strftime("%A") for x in dates]
  assert all(x.weekday() < 5 for x in dates)
  # "Last month" means the same month on every day of the week.
  assert {(x.year, x.month) for x in dates} == {(2026, 10)}


def test_scenario_sessions_have_known_users_days_and_unique_ids():
  users = {a.user_id for a in scenario.ANALYSTS}
  numbered = scenario.numbered_sessions("t")
  ids = [sid for sid, _ in numbered]

  assert {s.user_id for s in scenario.SESSIONS} == users
  assert {s.day for s in scenario.SESSIONS} == {d.number for d in scenario.DAYS}
  assert len(ids) == len(set(ids)) == len(scenario.SESSIONS)
  assert [s.day for _, s in numbered] == sorted(s.day for _, s in numbered)
  assert ids[0] == "an-t-d1-maya-1"
  assert all(turn.strip() for s in scenario.SESSIONS for turn in s.turns)


def test_every_control_question_comes_after_the_analyst_has_history():
  compared = [s for s in scenario.SESSIONS if s.compare]

  assert len(compared) >= 4
  for session in compared:
    earlier = [
        s
        for s in scenario.SESSIONS
        if s.user_id == session.user_id and s.day < session.day
    ]
    assert earlier, session


def test_session_state_carries_the_simulated_day_and_memory_mode():
  scripted = next(s for s in scenario.SESSIONS if s.compare)

  assert analyst_agent.session_state(scripted, memory=True) == {
      "sim_day": scripted.day,
      "sim_date": scenario.day(scripted.day).date,
      "sim_weekday": scenario.day(scripted.day).weekday,
      "analyst_name": scenario.analyst(scripted.user_id).name,
      "memory": "on",
  }
  assert analyst_agent.session_state(scripted, memory=False)["memory"] == "off"


# ---- the warehouse tools ----------------------------------------------------


class _Field:

  def __init__(self, name):
    self.name = name


class _Result(list):

  def __init__(self, rows, columns, total):
    super().__init__(rows)
    self.schema = [_Field(c) for c in columns]
    self.total_rows = total


def _table(name):
  project, dataset, table = name.split(".", 2)
  return SimpleNamespace(project=project, dataset_id=dataset, table_id=table)


class _Job:

  def __init__(
      self,
      statement_type="SELECT",
      bytes_=1_000,
      rows=(),
      error=None,
      tables=("bigquery-public-data.thelook_ecommerce.order_items",),
  ):
    self.statement_type = statement_type
    self.total_bytes_processed = bytes_
    self.referenced_tables = [_table(name) for name in tables]
    self._rows = list(rows)
    self._error = error

  def result(self, timeout=None):
    if self._error is not None:
      raise self._error
    columns = list(self._rows[0]) if self._rows else []
    return _Result(self._rows, columns, len(self._rows))


class _FakeBigQuery:
  """Records each query; dry runs and real runs get separate answers."""

  def __init__(self, dry=None, run=None):
    self.calls = []
    self._dry = dry or _Job()
    self._run = run or _Job()

  def query(self, sql, job_config=None):
    self.calls.append((sql, job_config))
    if job_config is not None and job_config.dry_run:
      return self._dry
    return self._run


def test_run_sql_runs_select_on_thelook_with_a_byte_cap_and_labels():
  rows = [
      {
          "category": "Jeans",
          "net": decimal.Decimal("123.5"),
          "day": date(2026, 9, 30),
      }
  ] * 60
  bq = _FakeBigQuery(run=_Job(rows=rows))

  out = analyst_agent.Warehouse(bq).run_sql("SELECT 1", "net revenue")

  (dry_sql, dry), (sql, config) = bq.calls
  assert (dry_sql, sql) == ("SELECT 1", "SELECT 1")
  assert dry.dry_run and not config.dry_run
  assert config.maximum_bytes_billed == analyst_agent.MAX_BYTES
  assert config.default_dataset.project == "bigquery-public-data"
  assert config.default_dataset.dataset_id == "thelook_ecommerce"
  assert config.labels == {"bqaa_demo": "agent_memory"}
  assert out["status"] == "ok"
  assert out["columns"] == ["category", "net", "day"]
  assert out["rows"][0] == {
      "category": "Jeans",
      "net": 123.5,
      "day": "2026-09-30",
  }
  assert (len(out["rows"]), out["total_rows"], out["truncated"]) == (
      50,
      60,
      True,
  )


@pytest.mark.parametrize("statement", ["DELETE", "CREATE_TABLE", "SCRIPT"])
def test_run_sql_refuses_anything_but_select_before_running_it(statement):
  bq = _FakeBigQuery(dry=_Job(statement_type=statement))

  out = analyst_agent.Warehouse(bq).run_sql("DELETE FROM x WHERE TRUE", "")

  assert out == {
      "status": "error",
      "message": f"Only SELECT statements may run here, not {statement}.",
  }
  assert len(bq.calls) == 1  # the dry run only


def test_run_sql_refuses_a_query_over_the_byte_cap():
  bq = _FakeBigQuery(dry=_Job(bytes_=3_500_000_000))

  out = analyst_agent.Warehouse(bq).run_sql("SELECT * FROM events", "")

  assert out == {
      "status": "error",
      "message": "The query would scan 3.5 GB; the limit is 2 GB.",
  }
  assert len(bq.calls) == 1


def test_run_sql_reports_bigquery_errors_to_the_model():
  error = exceptions.BadRequest(
      "Unrecognized name: delivery_at at [3:5]",
      errors=[{"message": "Unrecognized name: delivery_at at [3:5]"}],
  )
  bq = _FakeBigQuery(run=_Job(error=error))

  out = analyst_agent.Warehouse(bq).run_sql("SELECT delivery_at", "")

  assert out == {
      "status": "error",
      "message": "Unrecognized name: delivery_at at [3:5]",
  }


def test_describe_table_reads_the_columns_once():
  rows = [
      {"column_name": "id", "data_type": "INT64"},
      {"column_name": "sale_price", "data_type": "FLOAT64"},
  ]
  bq = _FakeBigQuery(run=_Job(rows=rows))
  warehouse = analyst_agent.Warehouse(bq)

  first = warehouse.describe_table("`order_items`")
  again = warehouse.describe_table(
      "bigquery-public-data.thelook_ecommerce.order_items"
  )

  assert (
      first
      == again
      == {
          "status": "ok",
          "table": "bigquery-public-data.thelook_ecommerce.order_items",
          "columns": [
              {"name": "id", "type": "INT64"},
              {"name": "sale_price", "type": "FLOAT64"},
          ],
      }
  )
  ((sql, config),) = bq.calls
  assert "INFORMATION_SCHEMA.COLUMNS" in sql
  assert _params(config) == {"table": "order_items"}


def test_describe_table_rejects_unknown_tables_without_a_query():
  bq = _FakeBigQuery()

  out = analyst_agent.Warehouse(bq).describe_table("customers")

  assert out["status"] == "error"
  assert "Unknown table 'customers'" in out["message"]
  assert bq.calls == []


def test_save_preference_writes_user_scoped_state():
  context = SimpleNamespace(state={})

  out = analyst_agent.save_preference(" Revenue Definition ", "net", context)
  empty = analyst_agent.save_preference("  ", "x", context)

  assert out == {"status": "saved", "key": "revenue_definition", "value": "net"}
  assert context.state == {"user:revenue_definition": "net"}
  assert empty["status"] == "error"


# ---- recall_memory ------------------------------------------------------------


class _ConsolidationBigQuery:
  """Serves the items and similar-task queries from canned rows."""

  def __init__(self, items, similar):
    self.calls = []
    self._items = items
    self._similar = similar

  def query(self, sql, job_config=None):
    self.calls.append((sql, job_config))
    rows = self._items if "FROM `" + TABLES.items in sql else self._similar
    return SimpleNamespace(result=lambda: rows)


def test_recall_memory_combines_traces_extraction_and_embedding_scores():
  fixture = offline_bigquery.load_fixture()
  client = Client(
      project_id="offline-demo",
      dataset_id="agent_analytics",
      table_id="agent_events",
      verify_schema=False,
      bq_client=offline_bigquery.OfflineBigQueryClient(fixture.rows),
  )
  items = [
      {
          "kind": "fact",
          "session_id": "s-103",
          "span_id": "sp-103-inv",
          "observed_at": datetime(2026, 10, 4, 18, 0, 0, tzinfo=UTC),
          "name": None,
          "entity_type": None,
          "subject": "Ana",
          "subject_type": "PERSON",
          "predicate": "follows_diet",
          "object": "pescatarian",
          "object_type": "VALUE",
          "statement": "Ana is pescatarian.",
      }
  ]
  bq = _ConsolidationBigQuery(
      items, [{"invocation_id": "inv-103", "similarity": 0.83}]
  )
  store = analyst_agent.MemoryStore(
      client=client,
      bq=bq,
      tables=TABLES,
      now=lambda: datetime(2026, 10, 6, 16, 0, 0, tzinfo=UTC),
  )
  recall = analyst_agent.make_recall_memory(store)
  context = SimpleNamespace(
      user_id="u-ana", session=SimpleNamespace(id="s-104")
  )

  memory = recall("Find dinner in Osaka that fits my diet", context)["memory"]

  assert "- Ana is pescatarian. [s-103/sp-103-inv]" in memory
  assert '- 0.83 trace inv-103: "I eat fish now' in memory
  assert "## Reasoning: similar past tasks that succeeded" in memory
  similar_sql, similar_config = bq.calls[1]
  assert _params(similar_config) == {
      "query": "Find dinner in Osaka that fits my diet",
      "user_id": "u-ana",
      "session_id": "s-104",
      "top_k": 8,
  }


def test_memory_agent_has_memory_tools_and_the_control_has_none():
  warehouse = analyst_agent.Warehouse(_FakeBigQuery())

  def recall_memory(request: str) -> dict:
    """Recalls."""
    return {}

  agent = analyst_agent.build_agent("gemini-x", warehouse, recall_memory)
  control = analyst_agent.build_agent("gemini-x", warehouse)

  names = lambda a: [getattr(t, "__name__", None) or t.name for t in a.tools]
  assert agent.name == "thelook_analyst"
  assert names(agent) == [
      "recall_memory",
      "save_preference",
      "describe_table",
      "run_sql",
  ]
  assert control.name == "thelook_analyst_no_memory"
  assert names(control) == ["describe_table", "run_sql"]
  assert "recall_memory" in agent.instruction
  assert "recall_memory" not in control.instruction
  assert "save_preference" not in control.instruction
  assert "{sim_date}" in control.instruction


# ---- consolidation ----------------------------------------------------------


def test_memory_tables_are_named_from_the_prefix():
  assert TABLES == memory_consolidation.MemoryTables(
      events="p.d.analyst_events",
      extractions="p.d.analyst_memory_extractions",
      items="p.d.analyst_memory_items",
      embeddings="p.d.analyst_task_embeddings",
  )


def test_extraction_reads_new_user_messages_once_with_ai_generate():
  sql = memory_consolidation.extract_sql(TABLES, "gemini-x")

  # A SELECT whose rows are appended, not a MERGE (see the module docstring).
  assert sql.startswith("SELECT\n  m.span_id,")
  assert "MERGE" not in sql
  assert "FROM `p.d.analyst_events` AS e" in sql
  assert "e.event_type = 'USER_MESSAGE_RECEIVED'" in sql
  assert "e.session_id IN UNNEST(@session_ids)" in sql
  # Messages already processed are skipped, and duplicates are dropped.
  assert (
      "e.span_id NOT IN (SELECT span_id FROM `p.d.analyst_memory_extractions`)"
      in sql
  )
  assert "QUALIFY ROW_NUMBER() OVER (PARTITION BY e.span_id" in sql
  assert "AI.GENERATE(" in sql and "endpoint => 'gemini-x'" in sql
  assert "output_schema => 'entities ARRAY<STRUCT<name STRING" in sql
  assert "@instructions" in sql


def test_items_and_embeddings_merge_on_their_keys():
  items = memory_consolidation.items_sql(TABLES)
  embed = memory_consolidation.embed_sql(TABLES, "embed-x")

  assert items.count("x.session_id IN UNNEST(@session_ids)") == 2
  assert "CONCAT(x.span_id, ':fact:', CAST(i AS STRING))" in items
  assert "ON t.item_id = s.item_id" in items
  assert embed.startswith("SELECT\n  n.span_id,")
  assert "AI.EMBED(n.message, endpoint => 'embed-x')" in embed
  assert (
      "e.span_id NOT IN (SELECT span_id FROM `p.d.analyst_task_embeddings`)"
      in embed
  )


def test_similar_tasks_rank_this_users_other_sessions_by_cosine():
  sql = memory_consolidation.similar_tasks_sql(TABLES, "embed-x")

  assert "AI.EMBED(@query, endpoint => 'embed-x')" in sql
  assert "1 - ML.DISTANCE(t.embedding, request.embedding, 'COSINE')" in sql
  assert "t.user_id = @user_id" in sql
  assert "t.session_id != @session_id" in sql
  assert sql.rstrip().endswith("LIMIT @top_k")


def test_extraction_instructions_leave_preferences_to_the_agent():
  text = memory_consolidation.EXTRACTION_INSTRUCTIONS

  for kind in memory_consolidation.ENTITY_TYPES:
    assert kind in text
  assert "reporting\npreferences" in text or "reporting preferences" in text
  assert "YYYY-MM-DD" in text


class _Rows(list):

  def __init__(self, rows):
    super().__init__(rows)
    self.total_rows = len(rows)


class _ConsolidationJob:

  def __init__(self, number, rows=(), dml=None):
    self.num_dml_affected_rows = dml
    self.total_bytes_billed = 10_485_760
    self.job_id = f"job-{number}"
    self._rows = _Rows(list(rows))

  def result(self):
    return self._rows


class _ConsolidateBigQuery:

  def __init__(self):
    self.calls = []

  def query(self, sql, job_config=None):
    self.calls.append((sql, job_config))
    number = len(self.calls)
    if sql.startswith("MERGE"):
      return _ConsolidationJob(number, dml=4)
    if sql.startswith("SELECT\n  COUNT(*) AS messages"):
      return _ConsolidationJob(
          number, rows=[{"messages": 2, "failed": 0, "facts": 3}]
      )
    return _ConsolidationJob(number, rows=[{}] * 2)


def test_consolidate_appends_extractions_and_embeddings_and_merges_items():
  bq = _ConsolidateBigQuery()

  out = memory_consolidation.consolidate(bq, TABLES, ["s-2", "s-1", "s-2"])

  (extract, x_config), (items, i_config), (embed, e_config), (usage, _) = (
      bq.calls
  )
  assert extract == memory_consolidation.extract_sql(TABLES)
  assert items.startswith("MERGE `p.d.analyst_memory_items` AS t")
  assert embed == memory_consolidation.embed_sql(TABLES)
  assert usage.startswith("SELECT\n  COUNT(*) AS messages")
  assert (x_config.destination.table_id, x_config.write_disposition) == (
      "analyst_memory_extractions",
      "WRITE_APPEND",
  )
  assert e_config.destination.table_id == "analyst_task_embeddings"
  assert i_config.destination is None
  assert _params(x_config) == {
      "session_ids": ["s-1", "s-2"],
      "instructions": memory_consolidation.EXTRACTION_INSTRUCTIONS,
  }
  assert _params(i_config) == {"session_ids": ["s-1", "s-2"]}
  assert out["sessions"] == ["s-1", "s-2"]
  assert out["steps"] == {
      "extract": {
          "rows_inserted": 2,
          "bytes_billed": 10_485_760,
          "job_id": "job-1",
      },
      "items": {
          "rows_inserted": 4,
          "bytes_billed": 10_485_760,
          "job_id": "job-2",
      },
      "embed": {
          "rows_inserted": 2,
          "bytes_billed": 10_485_760,
          "job_id": "job-3",
      },
  }
  assert out["extraction"] == {"messages": 2, "failed": 0, "facts": 3}


def test_items_from_rows_builds_facts_and_entities_with_their_source():
  at = datetime(2026, 10, 1, 16, 0, 0, tzinfo=UTC)
  facts, entities = memory_consolidation.items_from_rows(
      [
          {
              "kind": "entity",
              "session_id": "s",
              "span_id": "sp",
              "observed_at": at,
              "name": "Jeans",
              "entity_type": "PRODUCT_CATEGORY",
          },
          {
              "kind": "fact",
              "session_id": "s",
              "span_id": "sp",
              "observed_at": "2026-10-01T16:00:00Z",
              "subject": "Maya Chen",
              "subject_type": "PERSON",
              "predicate": "leads_merchandising_for",
              "object": "Jeans",
              "object_type": "PRODUCT_CATEGORY",
              "statement": "Maya Chen leads merchandising for Jeans.",
          },
          {
              "kind": "other",
              "session_id": "s",
              "span_id": "x",
              "observed_at": at,
          },
      ]
  )

  assert entities == [
      memory_layers.ExtractedEntity("Jeans", "PRODUCT_CATEGORY", "s", "sp", at)
  ]
  assert facts == [
      memory_layers.Fact(
          "Maya Chen",
          "PERSON",
          "leads_merchandising_for",
          "Jeans",
          "PRODUCT_CATEGORY",
          "Maya Chen leads merchandising for Jeans.",
          "s",
          "sp",
          at,
      )
  ]


def test_run_sql_reports_a_query_timeout_and_lets_other_errors_raise():
  slow = _FakeBigQuery(
      run=_Job(error=concurrent.futures.TimeoutError("still running"))
  )
  broken = _FakeBigQuery(run=_Job(error=KeyError("a bug, not bad SQL")))

  out = analyst_agent.Warehouse(slow).run_sql("SELECT 1", "")

  assert out == {"status": "error", "message": "still running"}
  with pytest.raises(KeyError):
    analyst_agent.Warehouse(broken).run_sql("SELECT 1", "")


@pytest.mark.parametrize(
    "tables,outside",
    [
        (
            [
                "bigquery-public-data.thelook_ecommerce.orders",
                "p.bqaa_agent_memory_demo.analyst_memory_items",
            ],
            "p.bqaa_agent_memory_demo.analyst_memory_items",
        ),
        (
            ["p.region-us.INFORMATION_SCHEMA.SCHEMATA"],
            "p.region-us.INFORMATION_SCHEMA.SCHEMATA",
        ),
        (
            ["bigquery-public-data.samples.shakespeare"],
            "bigquery-public-data.samples.shakespeare",
        ),
    ],
    ids=["memory-table", "project-metadata", "other-public-data"],
)
def test_run_sql_reads_only_thelook_tables(tables, outside):
  # In the recorded run's first pass, memory-off agents searched
  # INFORMATION_SCHEMA and read the memory tables with this tool.
  bq = _FakeBigQuery(dry=_Job(tables=tables))

  out = analyst_agent.Warehouse(bq).run_sql("SELECT ...", "")

  assert out == {
      "status": "error",
      "message": (
          "Only tables in bigquery-public-data.thelook_ecommerce can be read"
          f" here, not {outside}."
      ),
  }
  assert len(bq.calls) == 1  # the dry run only


def test_run_sql_may_read_thelook_metadata_and_no_table_at_all():
  for tables in (
      ["bigquery-public-data.thelook_ecommerce.INFORMATION_SCHEMA.TABLES"],
      [],
  ):
    bq = _FakeBigQuery(dry=_Job(tables=tables), run=_Job(rows=[{"x": 1}]))

    assert analyst_agent.Warehouse(bq).run_sql("SELECT 1", "")["status"] == "ok"


def test_replace_controls_keeps_the_superseded_sessions_with_a_reason():
  record = {
      "run_tag": "t",
      "sessions": [
          {"session_id": "s-1", "memory": "on"},
          {"session_id": "s-1-ctl", "memory": "off"},
          {"session_id": "s-2", "memory": "on"},
      ],
      "comparisons": [
          {"user_id": "u", "with_memory": "s-1", "without_memory": "s-1-ctl"}
      ],
  }
  new = {"s-1": {"session_id": "s-1-ctl2", "memory": "off"}}

  out = analyst_agent.replace_controls(record, new, "read the memory")

  assert [s["session_id"] for s in out["sessions"]] == [
      "s-1",
      "s-2",
      "s-1-ctl2",
  ]
  assert out["comparisons"] == [
      {"user_id": "u", "with_memory": "s-1", "without_memory": "s-1-ctl2"}
  ]
  assert out["superseded_controls"] == {
      "reason": "read the memory",
      "sessions": [{"session_id": "s-1-ctl", "memory": "off"}],
  }
  assert record["comparisons"][0]["without_memory"] == "s-1-ctl"  # unchanged
