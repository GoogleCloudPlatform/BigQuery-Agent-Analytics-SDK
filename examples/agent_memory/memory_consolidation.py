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

"""Memory consolidation: long-term memory extracted in BigQuery.

The plugin logs every user message as a ``USER_MESSAGE_RECEIVED`` row. A
consolidation pass (run nightly in the recorded run) turns a batch of
sessions into long-term memory with SQL that runs where the rows are:

* **Extraction.** ``AI.GENERATE`` reads each message and returns the
  entities it names and the durable facts it states, as typed columns
  (``output_schema``). One row per message, with its token usage, goes to
  the *extractions* table; the entities and facts, each keyed to the span
  of the message they came from, go to the *items* table.
* **Embeddings.** ``AI.EMBED`` embeds each message (the task the user
  asked for) into the *embeddings* table. Later sessions rank past tasks
  by ``ML.DISTANCE`` to the new request.

Both functions report each row's outcome in a ``status`` column, empty on
success. A message counts as processed only once a step has a successful
row for it: an extraction with an empty status, or an embedding with an
empty status and a non-empty vector. A failed attempt stays in its table
with its status, and the next pass tries that message again. Everything
that reads these tables takes the first successful row per message, and
items are keyed by span and position, so a retry never duplicates memory
and a pass can be re-run safely. The pass totals count the messages still
failing. The ``AI.GENERATE`` and ``AI.EMBED`` steps are ``SELECT`` jobs
that append to their table rather than ``MERGE`` statements: in the
recorded run, a ``MERGE`` whose source called ``AI.GENERATE`` stalled in
its output stage for over ten minutes, while the same ``SELECT`` took five
seconds. Preferences are not extracted here:
the agent saves those itself as ADK ``user:`` state, which the plugin logs
as ``STATE_DELTA`` rows.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime
import textwrap
from typing import Any, Iterable

import memory_layers

DEFAULT_ENDPOINT = "gemini-3.8-flash"
DEFAULT_EMBEDDING_ENDPOINT = "text-embedding-005"

ENTITY_TYPES = (
    "PERSON",
    "TEAM",
    "PRODUCT_CATEGORY",
    "BRAND",
    "DISTRIBUTION_CENTER",
    "MARKET",
    "TRAFFIC_SOURCE",
    "METRIC",
    "EVENT",
)

EXTRACTION_INSTRUCTIONS = f"""\
You build long-term memory for TheLook's data-analyst assistant. TheLook is
an online clothing retailer. Read one message that an analyst sent to the
assistant and return:
- entities: what the message names that matters for later analyses, with
  a type from {", ".join(ENTITY_TYPES)};
- facts: durable facts about the speaker and their work that will still
  hold in later conversations: their role and team, the categories,
  brands, markets or distribution centers they own or watch, how they
  define a metric, their fiscal calendar, and upcoming events with dates.
  Give each as subject, predicate (a snake_case verb phrase) and object,
  with the subject's and object's types (DATE for dates, VALUE for other
  literals), and as one sentence in the third person.
Rules: name the speaker by their full name. Use TheLook's names: product
categories as in the catalog (for example "Jeans" or "Outerwear & Coats"),
countries as in the data ("Brasil", "Spain") and distribution centers as
"Memphis TN". Write dates as YYYY-MM-DD, resolving relative dates against
Today. Skip one-off questions, results of analyses, and reporting
preferences such as currency, exchange rates, rounding or time
granularity; the assistant stores those itself. Return empty lists when
the message holds nothing durable."""

_OUTPUT_SCHEMA = (
    "entities ARRAY<STRUCT<name STRING, type STRING>>, facts"
    " ARRAY<STRUCT<subject STRING, subject_type STRING, predicate STRING,"
    " object STRING, object_type STRING, statement STRING>>"
)

_CREATE_TABLES = """\
CREATE TABLE IF NOT EXISTS `{extractions}` (
  span_id STRING NOT NULL,
  user_id STRING,
  session_id STRING,
  invocation_id STRING,
  observed_at TIMESTAMP,
  sim_date STRING,
  speaker STRING,
  message STRING,
  entities ARRAY<STRUCT<name STRING, type STRING>>,
  facts ARRAY<STRUCT<subject STRING, subject_type STRING, predicate STRING,
    object STRING, object_type STRING, statement STRING>>,
  status STRING,
  prompt_tokens INT64,
  output_tokens INT64,
  model STRING,
  extracted_at TIMESTAMP
);
CREATE TABLE IF NOT EXISTS `{items}` (
  item_id STRING NOT NULL,
  kind STRING,
  user_id STRING,
  session_id STRING,
  invocation_id STRING,
  span_id STRING,
  observed_at TIMESTAMP,
  sim_date STRING,
  name STRING,
  entity_type STRING,
  subject STRING,
  subject_type STRING,
  predicate STRING,
  object STRING,
  object_type STRING,
  statement STRING,
  model STRING,
  extracted_at TIMESTAMP
);
CREATE TABLE IF NOT EXISTS `{embeddings}` (
  span_id STRING NOT NULL,
  user_id STRING,
  session_id STRING,
  invocation_id STRING,
  observed_at TIMESTAMP,
  sim_date STRING,
  task STRING,
  embedding ARRAY<FLOAT64>,
  model STRING,
  embedded_at TIMESTAMP,
  status STRING
);
ALTER TABLE `{embeddings}` ADD COLUMN IF NOT EXISTS status STRING"""

# When a row of a step's table means its message is processed.
EXTRACTED = "COALESCE(status, '') = ''"
EMBEDDED = "COALESCE(status, '') = '' AND ARRAY_LENGTH(embedding) > 0"

# The user messages of the given sessions with no successful row in
# `{done}` yet; `{processed}` says which rows succeeded. Rows are delivered
# at least once, so duplicates of a span are dropped. A message with no text
# cannot succeed, so it is not tried.
_NEW_MESSAGES = """\
SELECT
  e.span_id,
  e.user_id,
  e.session_id,
  e.invocation_id,
  e.timestamp AS observed_at,
  JSON_VALUE(e.attributes, '$.session_metadata.state.sim_date') AS sim_date,
  COALESCE(
    JSON_VALUE(e.attributes, '$.session_metadata.state.analyst_name'),
    e.user_id
  ) AS speaker,
  JSON_VALUE(e.content, '$.text_summary') AS message
FROM `{events}` AS e
WHERE e.event_type = 'USER_MESSAGE_RECEIVED'
  AND e.session_id IN UNNEST(@session_ids)
  AND TRIM(COALESCE(JSON_VALUE(e.content, '$.text_summary'), '')) != ''
  AND e.span_id NOT IN (SELECT span_id FROM `{done}` WHERE {processed})
QUALIFY ROW_NUMBER() OVER (PARTITION BY e.span_id ORDER BY e.timestamp) = 1"""

# The first successful row per message of the given sessions in `{table}`.
_SUCCEEDED = """\
SELECT *
FROM `{table}`
WHERE session_id IN UNNEST(@session_ids)
  AND {processed}
QUALIFY ROW_NUMBER() OVER (PARTITION BY span_id ORDER BY {at}) = 1"""

_EXTRACT = """\
SELECT
  m.span_id,
  m.user_id,
  m.session_id,
  m.invocation_id,
  m.observed_at,
  m.sim_date,
  m.speaker,
  m.message,
  m.out.entities,
  m.out.facts,
  m.out.status,
  SAFE_CAST(
    JSON_VALUE(m.out.full_response, '$.usage_metadata.prompt_token_count')
    AS INT64
  ) AS prompt_tokens,
  SAFE_CAST(
    JSON_VALUE(m.out.full_response, '$.usage_metadata.candidates_token_count')
    AS INT64
  ) AS output_tokens,
  '{endpoint}' AS model,
  CURRENT_TIMESTAMP() AS extracted_at
FROM (
  SELECT
    n.*,
    AI.GENERATE(
      CONCAT(
        @instructions,
        '\\n\\nToday: ', COALESCE(n.sim_date, 'unknown'),
        '\\nSpeaker: ', n.speaker,
        '\\nMessage: ', n.message
      ),
      endpoint => '{endpoint}',
      output_schema => '{output_schema}'
    ) AS out
  FROM ({new_messages}) AS n
) AS m"""

_ITEMS = """\
MERGE `{items}` AS t
USING (
  SELECT
    CONCAT(x.span_id, ':entity:', CAST(i AS STRING)) AS item_id,
    'entity' AS kind,
    x.user_id,
    x.session_id,
    x.invocation_id,
    x.span_id,
    x.observed_at,
    x.sim_date,
    TRIM(entity.name) AS name,
    UPPER(TRIM(entity.type)) AS entity_type,
    CAST(NULL AS STRING) AS subject,
    CAST(NULL AS STRING) AS subject_type,
    CAST(NULL AS STRING) AS predicate,
    CAST(NULL AS STRING) AS object,
    CAST(NULL AS STRING) AS object_type,
    CAST(NULL AS STRING) AS statement,
    x.model,
    x.extracted_at
  FROM (
{extracted}
  ) AS x, UNNEST(x.entities) AS entity WITH OFFSET AS i
  WHERE TRIM(COALESCE(entity.name, '')) != ''
  UNION ALL
  SELECT
    CONCAT(x.span_id, ':fact:', CAST(i AS STRING)),
    'fact',
    x.user_id,
    x.session_id,
    x.invocation_id,
    x.span_id,
    x.observed_at,
    x.sim_date,
    CAST(NULL AS STRING),
    CAST(NULL AS STRING),
    TRIM(fact.subject),
    UPPER(TRIM(fact.subject_type)),
    TRIM(fact.predicate),
    TRIM(fact.object),
    UPPER(TRIM(fact.object_type)),
    TRIM(fact.statement),
    x.model,
    x.extracted_at
  FROM (
{extracted}
  ) AS x, UNNEST(x.facts) AS fact WITH OFFSET AS i
  WHERE TRIM(COALESCE(fact.subject, '')) != ''
    AND TRIM(COALESCE(fact.object, '')) != ''
) AS s
ON t.item_id = s.item_id
WHEN NOT MATCHED THEN INSERT (
  item_id, kind, user_id, session_id, invocation_id, span_id, observed_at,
  sim_date, name, entity_type, subject, subject_type, predicate, object,
  object_type, statement, model, extracted_at
) VALUES (
  s.item_id, s.kind, s.user_id, s.session_id, s.invocation_id, s.span_id,
  s.observed_at, s.sim_date, s.name, s.entity_type, s.subject,
  s.subject_type, s.predicate, s.object, s.object_type, s.statement,
  s.model, s.extracted_at
)"""

_EMBED = """\
SELECT
  n.span_id,
  n.user_id,
  n.session_id,
  n.invocation_id,
  n.observed_at,
  n.sim_date,
  n.message AS task,
  n.out.result AS embedding,
  '{embedding_endpoint}' AS model,
  CURRENT_TIMESTAMP() AS embedded_at,
  n.out.status
FROM (
  SELECT
    m.*,
    AI.EMBED(m.message, endpoint => '{embedding_endpoint}') AS out
  FROM ({new_messages}) AS m
) AS n"""

_LOAD_ITEMS = """\
SELECT
  kind,
  session_id,
  span_id,
  observed_at,
  name,
  entity_type,
  subject,
  subject_type,
  predicate,
  object,
  object_type,
  statement
FROM `{items}`
WHERE user_id = @user_id
ORDER BY observed_at, item_id"""

_SIMILAR_TASKS = """\
WITH request AS (
  SELECT AI.EMBED(@query, endpoint => '{embedding_endpoint}').result
    AS embedding
),
tasks AS (
  SELECT invocation_id, session_id, task, embedding
  FROM `{embeddings}`
  WHERE user_id = @user_id
    AND session_id != @session_id
    AND {embedded}
  QUALIFY ROW_NUMBER() OVER (PARTITION BY span_id ORDER BY embedded_at) = 1
)
SELECT
  t.invocation_id,
  t.session_id,
  t.task,
  1 - ML.DISTANCE(t.embedding, request.embedding, 'COSINE') AS similarity
FROM tasks AS t
CROSS JOIN request
ORDER BY similarity DESC
LIMIT @top_k"""

# Totals for the given sessions: messages tried, those whose extraction or
# embedding has not succeeded yet, items from the successful extractions,
# and the tokens of every attempt.
_USAGE = """\
WITH attempts AS (
  SELECT * FROM `{extractions}` WHERE session_id IN UNNEST(@session_ids)
),
extracted AS (
{extracted}
),
embedding_attempts AS (
  SELECT span_id FROM `{embeddings}` WHERE session_id IN UNNEST(@session_ids)
),
embedded AS (
{embedded}
)
SELECT
  (SELECT COUNT(DISTINCT span_id) FROM attempts) AS messages,
  (
    SELECT COUNT(DISTINCT span_id) FROM attempts
    WHERE span_id NOT IN (SELECT span_id FROM extracted)
  ) AS failed,
  (SELECT SUM(ARRAY_LENGTH(entities)) FROM extracted) AS entities,
  (SELECT SUM(ARRAY_LENGTH(facts)) FROM extracted) AS facts,
  (SELECT SUM(prompt_tokens) FROM attempts) AS prompt_tokens,
  (SELECT SUM(output_tokens) FROM attempts) AS output_tokens,
  (
    SELECT COUNT(DISTINCT span_id) FROM embedding_attempts
    WHERE span_id NOT IN (SELECT span_id FROM embedded)
  ) AS embeddings_failed"""


@dataclasses.dataclass(frozen=True)
class MemoryTables:
  """Fully qualified names of the tables consolidation reads and writes."""

  events: str
  extractions: str
  items: str
  embeddings: str

  @classmethod
  def in_dataset(
      cls, project: str, dataset: str, *, events: str, prefix: str
  ) -> "MemoryTables":
    base = f"{project}.{dataset}"
    return cls(
        events=f"{base}.{events}",
        extractions=f"{base}.{prefix}memory_extractions",
        items=f"{base}.{prefix}memory_items",
        embeddings=f"{base}.{prefix}task_embeddings",
    )


def create_tables_sql(tables: MemoryTables) -> str:
  return _CREATE_TABLES.format(
      extractions=tables.extractions,
      items=tables.items,
      embeddings=tables.embeddings,
  )


def _new_messages(tables: MemoryTables, done: str, processed: str) -> str:
  return _NEW_MESSAGES.format(
      events=tables.events, done=done, processed=processed
  )


def _succeeded(table: str, processed: str, at: str, indent: int) -> str:
  sql = _SUCCEEDED.format(table=table, processed=processed, at=at)
  return textwrap.indent(sql, " " * indent)


def extract_sql(tables: MemoryTables, endpoint: str = DEFAULT_ENDPOINT) -> str:
  """Extraction rows for the new messages; appended to ``extractions``."""
  return _EXTRACT.format(
      endpoint=endpoint,
      output_schema=_OUTPUT_SCHEMA,
      new_messages=_new_messages(tables, tables.extractions, EXTRACTED),
  )


def items_sql(tables: MemoryTables) -> str:
  """Merges the entities and facts of successful extractions into items."""
  return _ITEMS.format(
      items=tables.items,
      extracted=_succeeded(tables.extractions, EXTRACTED, "extracted_at", 4),
  )


def embed_sql(
    tables: MemoryTables,
    embedding_endpoint: str = DEFAULT_EMBEDDING_ENDPOINT,
) -> str:
  """Embedding rows for the new messages; appended to ``embeddings``."""
  return _EMBED.format(
      embedding_endpoint=embedding_endpoint,
      new_messages=_new_messages(tables, tables.embeddings, EMBEDDED),
  )


def similar_tasks_sql(
    tables: MemoryTables,
    embedding_endpoint: str = DEFAULT_EMBEDDING_ENDPOINT,
) -> str:
  return _SIMILAR_TASKS.format(
      embeddings=tables.embeddings,
      embedding_endpoint=embedding_endpoint,
      embedded=EMBEDDED,
  )


def usage_sql(tables: MemoryTables) -> str:
  """Totals for sessions: messages, failures, items and tokens."""
  return _USAGE.format(
      extractions=tables.extractions,
      embeddings=tables.embeddings,
      extracted=_succeeded(tables.extractions, EXTRACTED, "extracted_at", 2),
      embedded=_succeeded(tables.embeddings, EMBEDDED, "embedded_at", 2),
  )


def _job_config(destination: str = "", **params: Any) -> Any:
  """Parameters as typed query parameters; ``destination`` appends there."""
  from google.cloud import bigquery

  query_parameters = []
  for name, value in params.items():
    if isinstance(value, (list, tuple)):
      query_parameters.append(
          bigquery.ArrayQueryParameter(name, "STRING", list(value))
      )
    elif isinstance(value, int):
      query_parameters.append(
          bigquery.ScalarQueryParameter(name, "INT64", value)
      )
    else:
      query_parameters.append(
          bigquery.ScalarQueryParameter(name, "STRING", value)
      )
  if destination:
    return bigquery.QueryJobConfig(
        query_parameters=query_parameters,
        destination=destination,
        write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
    )
  return bigquery.QueryJobConfig(query_parameters=query_parameters)


def ensure_tables(bq: Any, tables: MemoryTables) -> None:
  bq.query(create_tables_sql(tables)).result()


def consolidate(
    bq: Any,
    tables: MemoryTables,
    session_ids: Iterable[str],
    *,
    endpoint: str = DEFAULT_ENDPOINT,
    embedding_endpoint: str = DEFAULT_EMBEDDING_ENDPOINT,
) -> dict[str, Any]:
  """Extracts entities and facts and embeds the tasks of these sessions.

  Only messages without a successful row are sent to the models, so a
  message that failed in an earlier pass is tried again. Returns what each
  statement did (rows inserted, bytes billed and job ids) and the totals
  for the sessions: messages, those whose extraction or embedding has not
  succeeded yet (``failed``, ``embeddings_failed``), items and tokens.
  """
  ids = sorted(set(session_ids))
  steps = {}
  for name, sql, params in (
      (
          "extract",
          extract_sql(tables, endpoint),
          {
              "destination": tables.extractions,
              "session_ids": ids,
              "instructions": EXTRACTION_INSTRUCTIONS,
          },
      ),
      ("items", items_sql(tables), {"session_ids": ids}),
      (
          "embed",
          embed_sql(tables, embedding_endpoint),
          {"destination": tables.embeddings, "session_ids": ids},
      ),
  ):
    job = bq.query(sql, job_config=_job_config(**params))
    result = job.result()
    inserted = job.num_dml_affected_rows
    steps[name] = {
        "rows_inserted": result.total_rows if inserted is None else inserted,
        "bytes_billed": job.total_bytes_billed,
        "job_id": job.job_id,
    }
  usage_job = bq.query(
      usage_sql(tables), job_config=_job_config(session_ids=ids)
  )
  totals = dict(next(iter(usage_job.result()), {}) or {})
  return {"sessions": ids, "steps": steps, "extraction": totals}


def load_memory_items(
    bq: Any, tables: MemoryTables, user_id: str
) -> tuple[list[memory_layers.Fact], list[memory_layers.ExtractedEntity]]:
  """One user's extracted facts and entities, oldest first."""
  job = bq.query(
      _LOAD_ITEMS.format(items=tables.items),
      job_config=_job_config(user_id=user_id),
  )
  return items_from_rows(dict(row) for row in job.result())


def items_from_rows(
    rows: Iterable[dict[str, Any]],
) -> tuple[list[memory_layers.Fact], list[memory_layers.ExtractedEntity]]:
  facts, entities = [], []
  for row in rows:
    where = dict(
        session_id=row["session_id"],
        span_id=row["span_id"],
        observed_at=_timestamp(row["observed_at"]),
    )
    if row["kind"] == "fact":
      facts.append(
          memory_layers.Fact(
              subject=row["subject"],
              subject_type=row["subject_type"] or "",
              predicate=row["predicate"] or "",
              object=row["object"],
              object_type=row["object_type"] or "",
              statement=row["statement"] or "",
              **where,
          )
      )
    elif row["kind"] == "entity":
      entities.append(
          memory_layers.ExtractedEntity(
              name=row["name"], entity_type=row["entity_type"] or "", **where
          )
      )
  return facts, entities


def similar_task_scores(
    bq: Any,
    tables: MemoryTables,
    user_id: str,
    query: str,
    *,
    session_id: str,
    top_k: int = 8,
    embedding_endpoint: str = DEFAULT_EMBEDDING_ENDPOINT,
) -> dict[str, float]:
  """Cosine similarity of this user's past tasks to ``query``, by trace id.

  The trace id of a task is its invocation id, as in ``memory_layers``.
  The current session is left out.
  """
  job = bq.query(
      similar_tasks_sql(tables, embedding_endpoint),
      job_config=_job_config(
          query=query, user_id=user_id, session_id=session_id, top_k=top_k
      ),
  )
  return {
      row["invocation_id"]: float(row["similarity"])
      for row in job.result()
      if row["invocation_id"]
  }


def _timestamp(value: Any) -> datetime:
  if isinstance(value, datetime):
    return value
  return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
