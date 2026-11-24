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

"""Materializes `eval_turns`, creates BigQuery dual indexes, and runs search.

Supports both:
  1. Live BigQuery execution (using ADK's ``build_eval_turn_query_sql`` when
     installed, falling back cleanly to ``sql/01_materialize_eval_turns.sql``;
     populating deterministic S3 ``_ai_index_trajectory``; creating autonomous
     ``AI.EMBED`` + ``SEARCH INDEX`` + scale-gated ``IVF VECTOR INDEX``; and
     executing 4-layer routing + ``AI.GENERATE`` contradiction verification +
     ``INFORMATION_SCHEMA.JOBS`` telemetry collection), and
  2. Deterministic ``--offline`` execution over ``live_demo_results.json`` (or
     built-in offline turn samples) with zero network or GCP dependencies.
"""

from __future__ import annotations

import argparse
from collections.abc import Mapping
from datetime import datetime
from datetime import timedelta
from datetime import timezone
import importlib
import json
import os
from pathlib import Path
import re
import sys
import time
from typing import Any, Callable
import uuid
import warnings

from analytics.query_router import model_uses_thinking_level
from analytics.query_router import QueryRouter
from analytics.query_router import ROUTE_HYBRID
from analytics.query_router import ROUTE_KEYWORD
from analytics.query_router import ROUTE_NATURAL_LANGUAGE
from analytics.seed_scale_benchmark import generate_synthetic_eval_turns
from analytics.seed_scale_benchmark import seed_scale_rows_to_bigquery
from analytics.seed_scale_benchmark import validate_bq_identifiers
from analytics.trajectory_extractor import write_owner_only_text

DEMO_OWNERSHIP_LABEL_KEY = "bqaa_demo"
DEMO_OWNERSHIP_LABEL_VALUE = "combined_cuj_trace_search"

_IVF_MIN_ROWS = 5000
_TEN_GIB_BYTES = 10 * 1024 * 1024 * 1024
_DEFAULT_TOP_K = 5

_SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]+$")
_CONNECTION_RE = re.compile(r"^[A-Za-z0-9_./-]+$")
_ENDPOINT_RE = re.compile(r"^[A-Za-z0-9_./:-]+$")
_WORD_TOKEN_RE = re.compile(r"[a-z0-9_]+")
_QUOTED_SPAN_RE = re.compile(r'"([^"]+)"')
_SEARCH_STRIP_CHARS_RE = re.compile(r"[?!()\[\]{};:,.*/\\'+=<>&|~^`]+")
_HYBRID_STOPWORDS = frozenset(
    {
        "why",
        "did",
        "do",
        "does",
        "the",
        "and",
        "for",
        "with",
        "even",
        "though",
        "that",
        "what",
        "when",
        "where",
        "how",
        "after",
        "before",
        "from",
        "this",
        "which",
        "their",
        "there",
        "here",
        "been",
        "has",
        "have",
        "had",
        "was",
        "were",
        "are",
        "not",
        "into",
        "onto",
        "about",
        "then",
        "than",
        "only",
        "can",
        "could",
        "would",
        "should",
        "will",
        "show",
        "find",
        "turn",
        "turns",
    }
)
_SEARCH_RESERVED_OPS = frozenset({"AND", "OR", "NOT"})
_SEARCH_RESERVED_QUOTE_WORDS = frozenset({"IN", "NEAR"})
_CONTRADICTION_FAIL_MARKER_RE = re.compile(
    r"\b(error|sorry|failed|unable|expired|cannot|declined|denied|restricted|"
    r"unauthorized)\b",
    re.IGNORECASE,
)
_AGENT_LINE_RE = re.compile(r"(?m)^AGENT:\s*(.+)$")

DEFAULT_BENCHMARK_QUERIES: tuple[dict[str, str], ...] = (
    {
        "query_id": "q1_exact_error",
        "query_text": "CARD_EXPIRED",
    },
    {
        "query_id": "q2_quoted_phrase",
        "query_text": '"refund has been processed"',
    },
    {
        "query_id": "q3_nl_contradiction",
        "query_text": (
            "agent told the customer their refund succeeded even though the "
            "tool failed"
        ),
    },
    {
        "query_id": "q4_nl_retry_loop",
        "query_text": (
            "agent got stuck calling the same lookup tool repeatedly after "
            "timeouts"
        ),
    },
    {
        "query_id": "q5_hybrid_mixed",
        "query_text": "why did issue_refund succeed after CARD_EXPIRED?",
    },
)


def _locate_demo_root() -> Path:
  """Locates the demo root across direct checkouts and runfiles trees."""
  candidates: list[Path] = []
  abs_here = Path(os.path.abspath(__file__))
  candidates.extend(abs_here.parents)
  resolved_here = Path(__file__).resolve()
  candidates.extend(resolved_here.parents)
  cwd = Path.cwd()
  candidates.append(cwd)
  candidates.extend(cwd.parents)
  for cand in candidates:
    if (cand / "sql" / "03_search_keyword_and_semantic.sql").is_file():
      return cand
    nested = cand / "examples" / "combined_cuj_trace_search_demo"
    if (nested / "sql" / "03_search_keyword_and_semantic.sql").is_file():
      return nested
  return resolved_here.parents[1]


def _sql_dir() -> Path:
  return _locate_demo_root() / "sql"


def assert_pure_ascii_sql(sql_text: str, *, name: str = "<sql>") -> None:
  """Raises ``ValueError`` if ``sql_text`` contains any non-ASCII byte."""
  try:
    sql_text.encode("ascii")
  except UnicodeEncodeError as exc:
    raise ValueError(
        f"SQL template {name} contains non-ASCII characters at offset "
        f"{exc.start}: {exc.reason}"
    ) from exc


def render_sql_tokens(sql_text: str, replacements: dict[str, str]) -> str:
  """Replaces `{KEY}` tokens in SQL body lines without touching `--` comments."""
  out_lines: list[str] = []
  for line in sql_text.splitlines():
    if line.lstrip().startswith("--"):
      out_lines.append(line)
      continue
    updated = line
    for key, value in replacements.items():
      updated = updated.replace("{" + key + "}", value)
    out_lines.append(updated)
  return "\n".join(out_lines)


def split_sql_statements(sql_text: str) -> list[str]:
  """Splits a multi-statement SQL file cleanly, ignoring `--` comments."""
  stripped_lines = [
      line
      for line in sql_text.splitlines()
      if not line.lstrip().startswith("--")
  ]
  body = "\n".join(stripped_lines)
  return [part.strip() for part in body.split(";") if part.strip()]


def load_named_sql_queries(sql_path: Path) -> dict[str, str]:
  """Parses `-- [QUERY: <NAME>]` blocks from a SQL file into a dict."""
  text = sql_path.read_text(encoding="utf-8")
  assert_pure_ascii_sql(text, name=sql_path.name)
  markers = list(
      re.finditer(r"^--\s*\[QUERY:\s*([A-Z0-9_]+)\s*\]\s*$", text, re.M)
  )
  queries: dict[str, str] = {}
  for idx, match in enumerate(markers):
    name = match.group(1)
    start = match.end()
    end = markers[idx + 1].start() if idx + 1 < len(markers) else len(text)
    block = text[start:end].strip().rstrip(";").strip()
    queries[name] = block
  return queries


def sanitize_search_query_for_bq(
    query: str, *, mode: str = ROUTE_KEYWORD
) -> str:
  """Sanitizes a user query for GoogleSQL `SEARCH()` query-parser syntax.

  Strips parser-breaking punctuation (such as `?`, `!`, parentheses, brackets,
  colons, and semicolons) while preserving quoted phrases. In ``HYBRID`` mode,
  drops English filler stopwords and joins remaining identifier/content terms
  with ``OR`` so keyword search augments semantic vector recall safely.
  """
  raw = (query or "").strip()
  if not raw:
    return ""

  quoted_phrases: list[str] = []
  seen_lower: set[str] = set()
  for match in _QUOTED_SPAN_RE.finditer(raw):
    inner = _SEARCH_STRIP_CHARS_RE.sub(" ", match.group(1))
    inner = " ".join(inner.split())
    if inner:
      phrase = f'"{inner}"'
      key = phrase.lower()
      if key not in seen_lower:
        seen_lower.add(key)
        quoted_phrases.append(phrase)

  unquoted = _QUOTED_SPAN_RE.sub(" ", raw).replace('"', " ")
  cleaned_unquoted = _SEARCH_STRIP_CHARS_RE.sub(" ", unquoted)
  raw_tokens = [tok for tok in cleaned_unquoted.split() if tok]

  if mode == ROUTE_HYBRID:
    hybrid_terms: list[str] = list(quoted_phrases)
    for tok in raw_tokens:
      clean_tok = tok.strip("-")
      if not clean_tok:
        continue
      low = clean_tok.lower()
      if clean_tok.upper() in _SEARCH_RESERVED_OPS:
        continue
      is_ident = (
          "_" in clean_tok
          or clean_tok.isupper()
          or any(ch.isdigit() for ch in clean_tok)
      )
      if not is_ident and (len(clean_tok) < 3 or low in _HYBRID_STOPWORDS):
        continue
      must_quote = (
          "-" in clean_tok or clean_tok.upper() in _SEARCH_RESERVED_QUOTE_WORDS
      )
      formatted = f'"{clean_tok}"' if must_quote else clean_tok
      key = formatted.lower()
      if key not in seen_lower:
        seen_lower.add(key)
        hybrid_terms.append(formatted)
    if hybrid_terms:
      return " OR ".join(hybrid_terms)
    fallback_tokens = [
        f'"{t}"' if t.upper() in _SEARCH_RESERVED_QUOTE_WORDS else t
        for t in re.findall(r"[A-Za-z0-9_]+", raw)
        if t.upper() not in _SEARCH_RESERVED_OPS
    ]
    return " OR ".join(dict.fromkeys(fallback_tokens))

  # KEYWORD mode: preserve quoted phrases + non-operator tokens safely,
  # quoting BigQuery SEARCH reserved words IN/NEAR so they match text safely.
  parts: list[str] = list(quoted_phrases)
  for tok in raw_tokens:
    clean_tok = tok.strip("-")
    if not clean_tok or clean_tok.upper() in _SEARCH_RESERVED_OPS:
      continue
    if "-" in clean_tok or clean_tok.upper() in _SEARCH_RESERVED_QUOTE_WORDS:
      parts.append(f'"{clean_tok}"')
    else:
      parts.append(clean_tok)
  if parts:
    return " ".join(parts)
  fallback_kw = [
      f'"{t}"' if t.upper() in _SEARCH_RESERVED_QUOTE_WORDS else t
      for t in re.findall(r"[A-Za-z0-9_]+", raw)
      if t.upper() not in _SEARCH_RESERVED_OPS
  ]
  return " ".join(fallback_kw)


def _build_session_raw_where_clause(session_ids: list[str]) -> str:
  """Builds a validated `session_id IN (...)` raw_where_clause filter."""
  cleaned: list[str] = []
  seen: set[str] = set()
  for sid in session_ids:
    s = str(sid).strip()
    if not s:
      continue
    if not _SESSION_ID_RE.match(s):
      raise ValueError(f"Unsafe session_id value: {s!r}")
    if s not in seen:
      seen.add(s)
      cleaned.append(s)
  if not cleaned:
    return ""
  quoted = ", ".join(f"'{sid}'" for sid in cleaned)
  return f"session_id IN ({quoted})"


_validate_bq_identifiers = validate_bq_identifiers


def _resolve_adk_eval_turn_builder() -> Callable[..., str] | None:
  """Returns ADK's ``build_eval_turn_query_sql`` without storage-write deps."""
  for module_name in (
      "google.adk.plugins",
      "google.adk.plugins._bigquery_agent_analytics_eval_sql",
      "google.adk.plugins.bigquery_agent_analytics_plugin",
  ):
    try:
      mod = importlib.import_module(module_name)
      builder = getattr(mod, "build_eval_turn_query_sql", None)
      if callable(builder):
        return builder
    except Exception:
      continue
  return None


def assert_demo_table_ownership_or_absent(
    bq_client: Any,
    project_id: str,
    dataset_id: str,
    table_name: str,
) -> None:
  """Refuses to overwrite or drop a pre-existing non-demo BigQuery table."""
  table_fqn = f"{project_id}.{dataset_id}.{table_name}"
  try:
    table_meta = bq_client.get_table(table_fqn)
  except Exception:
    return
  labels = getattr(table_meta, "labels", None)
  if (
      table_meta is not None
      and isinstance(labels, Mapping)
      and labels.get(DEMO_OWNERSHIP_LABEL_KEY) != DEMO_OWNERSHIP_LABEL_VALUE
  ):
    raise RuntimeError(
        f"Refusing to modify pre-existing table `{table_fqn}`: missing demo "
        f"ownership label {DEMO_OWNERSHIP_LABEL_KEY}={DEMO_OWNERSHIP_LABEL_VALUE}."
    )


def apply_demo_table_label(
    bq_client: Any,
    project_id: str,
    dataset_id: str,
    table_name: str,
) -> None:
  """Tags a newly created BigQuery table with the demo ownership label."""
  table_fqn = f"{project_id}.{dataset_id}.{table_name}"
  try:
    table_meta = bq_client.get_table(table_fqn)
  except Exception:
    return
  labels = getattr(table_meta, "labels", None)
  if table_meta is None or not isinstance(labels, Mapping):
    return
  if labels.get(DEMO_OWNERSHIP_LABEL_KEY) == DEMO_OWNERSHIP_LABEL_VALUE:
    return
  updated = dict(labels)
  updated[DEMO_OWNERSHIP_LABEL_KEY] = DEMO_OWNERSHIP_LABEL_VALUE
  table_meta.labels = updated
  bq_client.update_table(table_meta, ["labels"])


def build_materialize_eval_turns_sql(
    *,
    project_id: str,
    dataset_id: str,
    events_table: str = "agent_events",
    eval_turns_table: str = "eval_turns",
    raw_where_clause: str = "",
) -> tuple[str, str]:
  """Builds the ``CREATE OR REPLACE TABLE ... eval_turns`` DDL statement.

  Uses ``google.adk.plugins.bigquery_agent_analytics_plugin``'s
  ``build_eval_turn_query_sql`` when available, wrapping it with the three
  demo columns (``error_summary``, ``raw_trace_bytes``,
  ``_ai_index_trajectory``). Falls back cleanly to
  ``sql/01_materialize_eval_turns.sql`` on older ADK versions.
  """
  _validate_bq_identifiers(project_id, dataset_id, events_table)
  _validate_bq_identifiers(project_id, dataset_id, eval_turns_table)
  events_fqn = f"{project_id}.{dataset_id}.{events_table}"
  eval_turns_fqn = f"{project_id}.{dataset_id}.{eval_turns_table}"

  builder = _resolve_adk_eval_turn_builder()
  if builder is not None:
    base_sql = builder(events_fqn, raw_where_clause=raw_where_clause)
    extra_raw_filter = ""
    if raw_where_clause.strip():
      cond = raw_where_clause.strip()
      if cond.upper().startswith("WHERE "):
        cond = cond[6:].strip()
      extra_raw_filter = f"\n      AND (\n        {cond}\n      )"
    ddl = (
        f"CREATE OR REPLACE TABLE `{eval_turns_fqn}`\n"
        "PARTITION BY DATE(turn_start_timestamp)\n"
        "CLUSTER BY root_agent, has_error, session_id\n"
        f"OPTIONS(labels=[('{DEMO_OWNERSHIP_LABEL_KEY}', "
        f"'{DEMO_OWNERSHIP_LABEL_VALUE}')]) AS\n"
        "WITH\n"
        "  BaseEvalTurns AS (\n"
        f"{base_sql}\n"
        "  ),\n"
        "  EventErrorSummary AS (\n"
        "    SELECT\n"
        "      session_id,\n"
        "      invocation_id,\n"
        "      NULLIF(\n"
        "        ARRAY_TO_STRING(\n"
        "          ARRAY_AGG(\n"
        "            DISTINCT COALESCE(\n"
        "              NULLIF(TRIM(error_message), ''),\n"
        "              IF(\n"
        "                event_type = 'TOOL_COMPLETED',\n"
        "                COALESCE(\n"
        "                  NULLIF(TRIM(JSON_VALUE(content, '$.result.error_code')), ''),\n"
        "                  NULLIF(TRIM(JSON_VALUE(content, '$.result.error')), '')\n"
        "                ),\n"
        "                NULL\n"
        "              )\n"
        "            )\n"
        "            IGNORE NULLS\n"
        "            ORDER BY COALESCE(\n"
        "              NULLIF(TRIM(error_message), ''),\n"
        "              IF(\n"
        "                event_type = 'TOOL_COMPLETED',\n"
        "                COALESCE(\n"
        "                  NULLIF(TRIM(JSON_VALUE(content, '$.result.error_code')), ''),\n"
        "                  NULLIF(TRIM(JSON_VALUE(content, '$.result.error')), '')\n"
        "                ),\n"
        "                NULL\n"
        "              )\n"
        "            )\n"
        "          ),\n"
        "          '; '\n"
        "        ),\n"
        "        ''\n"
        "      ) AS error_summary\n"
        f"    FROM `{events_fqn}`\n"
        "    WHERE session_id IS NOT NULL\n"
        "      AND invocation_id IS NOT NULL\n"
        "      AND JSON_VALUE(attributes, '$.adk.parent_session_id') IS NULL"
        f"{extra_raw_filter}\n"
        "    GROUP BY session_id, invocation_id\n"
        "  )\n"
        "SELECT\n"
        "  b.*,\n"
        "  e.error_summary,\n"
        "  BYTE_LENGTH(TO_JSON_STRING(b.trace)) AS raw_trace_bytes,\n"
        "  CAST(NULL AS STRING) AS _ai_index_trajectory\n"
        "FROM BaseEvalTurns AS b\n"
        "LEFT JOIN EventErrorSummary AS e\n"
        "  USING (session_id, invocation_id)"
    )
    return ddl, "adk_build_eval_turn_query_sql"

  sql_path = _sql_dir() / "01_materialize_eval_turns.sql"
  template = sql_path.read_text(encoding="utf-8")
  assert_pure_ascii_sql(template, name=sql_path.name)
  rendered = template.format(
      PROJECT_ID=project_id,
      DATASET_ID=dataset_id,
      EVENTS_TABLE=events_table,
      EVAL_TURNS_TABLE=eval_turns_table,
  )
  if raw_where_clause.strip():
    cond = raw_where_clause.strip()
    if cond.upper().startswith("WHERE "):
      cond = cond[6:].strip()
    rendered = rendered.replace(
        "      AND invocation_id IS NOT NULL\n",
        f"      AND invocation_id IS NOT NULL\n      AND (\n        {cond}\n      )\n",
        1,
    )
  return rendered.strip().rstrip(";"), "sql_01_materialize_eval_turns_fallback"


def _extract_s3_trajectory_fallback(
    trace_obj: dict[str, Any], *, row: dict[str, Any], max_chars: int = 4800
) -> str:
  """Deterministic outcome-first fallback when trajectory_extractor is absent."""
  root_agent = str(row.get("root_agent") or "support_agent")
  has_error = str(bool(row.get("has_error"))).lower()
  tool_calls = int(row.get("tool_call_count") or 0)
  tool_errors = int(row.get("tool_error_count") or 0)
  has_loop = str(bool(row.get("has_tool_loop"))).lower()
  latency_ms = int(row.get("turn_latency_ms") or 0)
  lines = [
      f"[OUTCOME: root_agent={root_agent} has_error={has_error} "
      f"tool_calls={tool_calls} tool_errors={tool_errors} "
      f"loop={has_loop} latency_ms={latency_ms}]"
  ]
  turns = trace_obj.get("turns") if isinstance(trace_obj, dict) else []
  for turn in turns or []:
    for ev in turn.get("events", []):
      content = ev.get("content") or {}
      role = content.get("role")
      for part in content.get("parts", []):
        if "text" in part and part["text"]:
          label = "USER" if role == "user" else "AGENT"
          txt = re.sub(r"<[^>]+>", " ", str(part["text"]))
          txt = re.sub(r"\s+", " ", txt).strip()
          lines.append(f"{label}: {txt[:400]}")
        elif "function_call" in part:
          fc = part["function_call"]
          args_str = json.dumps(fc.get("args") or {}, sort_keys=True)
          lines.append(f"TOOL_CALL: {fc.get('name')}({args_str[:180]})")
        elif "function_response" in part:
          fr = part["function_response"]
          resp_str = json.dumps(fr.get("response") or {}, sort_keys=True)
          if len(resp_str) > 360:
            resp_str = f"{resp_str[:140]} ...[truncated]... {resp_str[-180:]}"
          lines.append(f"TOOL_RES: {fr.get('name')} -> {resp_str}")
  out = "\n".join(lines)
  if len(out) > max_chars:
    out = out[: max_chars - 24] + "\n...[truncated]..."
  return out


def _extract_s3_text(trace_obj: Any, row: dict[str, Any]) -> str:
  """Uses Slice 1's trajectory_extractor lazily when present, else fallback."""
  if isinstance(trace_obj, str):
    try:
      trace_dict = json.loads(trace_obj)
    except json.JSONDecodeError:
      trace_dict = {}
  elif isinstance(trace_obj, dict):
    trace_dict = trace_obj
  else:
    trace_dict = {}

  try:
    from analytics.trajectory_extractor import extract_s3_outcome_first

    return extract_s3_outcome_first(trace_dict, metrics=row)
  except ImportError:
    return _extract_s3_trajectory_fallback(trace_dict, row=row)


def populate_trajectories_in_bigquery(
    bq_client: Any,
    *,
    project_id: str,
    dataset_id: str,
    eval_turns_table: str = "eval_turns",
) -> int:
  """Populates NULL ``_ai_index_trajectory`` values via staging load + MERGE."""
  from google.cloud import bigquery

  _validate_bq_identifiers(project_id, dataset_id, eval_turns_table)
  table_fqn = f"{project_id}.{dataset_id}.{eval_turns_table}"
  fetch_sql = (
      "SELECT session_id, invocation_id, root_agent, has_error, "
      "tool_call_count, tool_error_count, has_tool_loop, turn_latency_ms, "
      f"error_summary, trace FROM `{table_fqn}` "
      "WHERE _ai_index_trajectory IS NULL"
  )
  rows = list(bq_client.query(fetch_sql).result())
  if not rows:
    return 0

  staged: list[dict[str, str]] = []
  for r in rows:
    row_dict = dict(r)
    s3_text = _extract_s3_text(row_dict.get("trace"), row_dict)
    staged.append(
        {
            "session_id": str(row_dict["session_id"]),
            "invocation_id": str(row_dict["invocation_id"]),
            "_ai_index_trajectory": s3_text,
        }
    )

  stage_fqn = (
      f"{project_id}.{dataset_id}.{eval_turns_table}"
      f"__stage_{uuid.uuid4().hex[:8]}"
  )
  stage_schema = [
      bigquery.SchemaField("session_id", "STRING", mode="REQUIRED"),
      bigquery.SchemaField("invocation_id", "STRING", mode="REQUIRED"),
      bigquery.SchemaField("_ai_index_trajectory", "STRING"),
  ]
  load_cfg = bigquery.LoadJobConfig(
      schema=stage_schema,
      write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
  )
  bq_client.load_table_from_json(
      staged, stage_fqn, job_config=load_cfg
  ).result()
  try:
    merge_sql = (
        f"MERGE `{table_fqn}` AS t\n"
        f"USING `{stage_fqn}` AS s\n"
        "ON t.session_id = s.session_id AND t.invocation_id = s.invocation_id\n"
        "WHEN MATCHED THEN\n"
        "  UPDATE SET t._ai_index_trajectory = s._ai_index_trajectory"
    )
    bq_client.query(merge_sql).result()
  finally:
    bq_client.delete_table(stage_fqn, not_found_ok=True)
  return len(staged)


def setup_dual_indexes(
    bq_client: Any,
    *,
    project_id: str,
    dataset_id: str,
    eval_turns_table: str = "eval_turns",
    embed_wait_seconds: int = 65,
) -> dict[str, Any]:
  """Runs DDL steps 1-2 unconditionally and gates IVF Step 3 at >= 5,000 rows."""
  _validate_bq_identifiers(project_id, dataset_id, eval_turns_table)
  sql_path = _sql_dir() / "02_setup_dual_indexes.sql"
  template = sql_path.read_text(encoding="utf-8")
  assert_pure_ascii_sql(template, name=sql_path.name)
  rendered = render_sql_tokens(
      template,
      {
          "PROJECT_ID": project_id,
          "DATASET_ID": dataset_id,
          "EVAL_TURNS_TABLE": eval_turns_table,
      },
  )
  stmts = split_sql_statements(rendered)
  if len(stmts) != 3:
    raise ValueError(
        f"Expected 3 DDL statements in {sql_path.name}, found {len(stmts)}"
    )

  embed_ddl, search_ddl, vector_ddl = stmts
  bq_client.query(embed_ddl).result()
  bq_client.query(search_ddl).result()

  table_fqn = f"{project_id}.{dataset_id}.{eval_turns_table}"
  count_sql = (
      "SELECT COUNT(*) AS row_count, "
      "COALESCE(COUNTIF(_ai_index_trajectory_embedding IS NOT NULL "
      "AND ARRAY_LENGTH(_ai_index_trajectory_embedding.result) > 0), 0) "
      f"AS ready FROM `{table_fqn}`"
  )

  row_count = 0
  ready_count = 0
  deadline = time.monotonic() + max(0, embed_wait_seconds)
  while True:
    counts = list(bq_client.query(count_sql).result())
    if counts:
      first = dict(counts[0])
      row_count = int(first.get("row_count") or 0)
      ready_count = int(first.get("ready") or 0)
    if (
        row_count == 0
        or ready_count >= row_count
        or time.monotonic() >= deadline
    ):
      break
    time.sleep(min(3.0, max(0.2, deadline - time.monotonic())))

  if row_count >= _IVF_MIN_ROWS:
    bq_client.query(vector_ddl).result()
    vector_status = "CREATED"
  else:
    vector_status = "SKIPPED_BELOW_5000_ROWS"

  search_status = "CREATED"
  try:
    table_meta = bq_client.get_table(table_fqn)
    num_bytes = int(getattr(table_meta, "num_bytes", 0) or 0)
    if 0 < num_bytes < _TEN_GIB_BYTES:
      search_status = "SCAN_FALLBACK_BELOW_10GIB"
  except Exception:
    pass

  status_warnings: list[str] = []
  if row_count > 0 and ready_count < row_count:
    msg = (
        f"Partial embeddings ready: {ready_count}/{row_count} rows have "
        "populated embeddings; AI.SEARCH only covers embedded rows."
    )
    status_warnings.append(msg)
    warnings.warn(msg, RuntimeWarning, stacklevel=2)

  return {
      "ai_embed_column": "OK",
      "search_index": search_status,
      "vector_index": vector_status,
      "row_count": row_count,
      "embeddings_ready": ready_count,
      "warnings": status_warnings,
  }


def _extract_job_stats(query_job: Any) -> dict[str, Any]:
  props = getattr(query_job, "_properties", None) or {}
  qstats = props.get("statistics", {}).get("query", {})
  search_reasons = [
      str(r.get("code") or r.get("message") or r)
      for r in (qstats.get("searchStatistics") or {}).get(
          "indexUnusedReasons", []
      )
  ]
  vector_reasons = [
      str(r.get("code") or r.get("message") or r)
      for r in (qstats.get("vectorSearchStatistics") or {}).get(
          "indexUnusedReasons", []
      )
  ]
  return {
      "bytes_processed": int(
          getattr(query_job, "total_bytes_processed", 0) or 0
      ),
      "slot_ms": int(getattr(query_job, "slot_millis", 0) or 0),
      "search_unused_reasons": search_reasons,
      "vector_unused_reasons": vector_reasons,
  }


def _offline_contradiction_check(row: dict[str, Any]) -> dict[str, Any]:
  traj = str(row.get("_ai_index_trajectory") or "")
  has_err = bool(row.get("has_error")) or bool(
      str(row.get("error_summary") or "").strip()
  )
  has_loop = bool(row.get("has_tool_loop"))
  raw_bytes = int(row.get("raw_trace_bytes") or 0)
  agent_matches = _AGENT_LINE_RE.findall(traj)
  agent_text = agent_matches[-1].strip() if agent_matches else ""

  is_contradiction = bool(
      has_err
      and agent_text
      and not _CONTRADICTION_FAIL_MARKER_RE.search(agent_text)
  )
  if is_contradiction:
    label = "SILENT_CONTRADICTION"
    conf = 0.94
    snippet = agent_text[:160]
  elif has_loop:
    label = "TOOL_LOOP"
    conf = 0.97
    snippet = (
        f"max_consecutive={row.get('max_consecutive_identical_tool_calls', 0)}"
    )
  elif has_err:
    label = "EXACT_ERROR"
    conf = 0.96
    snippet = str(row.get("error_summary") or "error recorded")
  elif raw_bytes >= 18000:
    label = "TOKEN_BLOAT"
    conf = 0.90
    snippet = f"raw_trace_bytes={raw_bytes}"
  else:
    label = "HEALTHY"
    conf = 0.98
    snippet = agent_text[:160]
  return {
      "session_id": str(row["session_id"]),
      "invocation_id": str(row["invocation_id"]),
      "is_silent_contradiction": is_contradiction,
      "pattern_label": label,
      "confidence": conf,
      "evidence_snippet": snippet,
  }


def _offline_search_rows(
    turn_rows: list[dict[str, Any]],
    *,
    mode: str,
    query_text: str,
    top_k: int,
) -> list[dict[str, Any]]:
  q_lower = query_text.strip().lower()
  q_tokens = set(_WORD_TOKEN_RE.findall(q_lower))
  # Strip literal quotes for exact phrase matching.
  phrase = q_lower.strip('"').strip()

  kw_scores: dict[tuple[str, str], float] = {}
  sem_scores: dict[tuple[str, str], float] = {}
  by_key: dict[tuple[str, str], dict[str, Any]] = {}

  for row in turn_rows:
    key = (str(row["session_id"]), str(row["invocation_id"]))
    by_key[key] = row
    haystack = " ".join(
        [
            str(row.get("_ai_index_trajectory") or ""),
            str(row.get("session_id") or ""),
            str(row.get("invocation_id") or ""),
            str(row.get("error_summary") or ""),
            str(row.get("root_agent") or ""),
        ]
    ).lower()
    if not q_lower:
      kw_scores[key] = 1.0
    elif phrase and phrase in haystack:
      kw_scores[key] = 2.0 + haystack.count(phrase) * 0.25
    else:
      h_tokens = set(_WORD_TOKEN_RE.findall(haystack))
      overlap = len(q_tokens & h_tokens)
      if overlap > 0:
        kw_scores[key] = overlap / max(1, len(q_tokens))

    # Semantic scoring maps contradiction/loop intent onto matching turn cues.
    h_tokens = set(_WORD_TOKEN_RE.findall(haystack))
    base_sim = len(q_tokens & h_tokens) / max(1, len(q_tokens))
    verdict = _offline_contradiction_check(row)
    bonus = 0.0
    if (
        "succeed" in q_tokens
        or "contradiction" in q_tokens
        or "falsely" in q_tokens
    ) and verdict["is_silent_contradiction"]:
      bonus += 0.65
    if (
        "stuck" in q_tokens
        or "repeatedly" in q_tokens
        or "loop" in q_tokens
        or "timeouts" in q_tokens
    ) and bool(row.get("has_tool_loop")):
      bonus += 0.65
    if "refund" in q_tokens and "refund" in haystack:
      bonus += 0.15
    sem_scores[key] = min(0.99, base_sim * 0.5 + bonus)

  if mode == ROUTE_KEYWORD:
    ranked_keys = [
        k
        for k, s in sorted(
            kw_scores.items(), key=lambda kv: (-kv[1], kv[0][0], kv[0][1])
        )
        if s > 0
    ]
  elif mode == ROUTE_NATURAL_LANGUAGE:
    ranked_keys = [
        k
        for k, s in sorted(
            sem_scores.items(), key=lambda kv: (-kv[1], kv[0][0], kv[0][1])
        )
        if s > 0
    ]
  else:
    kw_order = [
        k
        for k, s in sorted(
            kw_scores.items(), key=lambda kv: (-kv[1], kv[0][0], kv[0][1])
        )
        if s > 0
    ]
    sem_order = [
        k
        for k, s in sorted(
            sem_scores.items(), key=lambda kv: (-kv[1], kv[0][0], kv[0][1])
        )
        if s > 0
    ]
    kw_rank = {k: i + 1 for i, k in enumerate(kw_order)}
    sem_rank = {k: i + 1 for i, k in enumerate(sem_order)}
    all_keys = set(kw_rank) | set(sem_rank)
    rrf = {
        k: (1.0 / (60 + kw_rank[k]) if k in kw_rank else 0.0)
        + (1.0 / (60 + sem_rank[k]) if k in sem_rank else 0.0)
        for k in all_keys
    }
    ranked_keys = sorted(rrf, key=lambda k: (-rrf[k], k[0], k[1]))

  hits: list[dict[str, Any]] = []
  for k in ranked_keys[:top_k]:
    row = by_key[k]
    sim = sem_scores.get(k, 0.0)
    hits.append(
        {
            "session_id": row["session_id"],
            "invocation_id": row["invocation_id"],
            "turn_index": int(row.get("turn_index") or 0),
            "turn_start_timestamp": str(row.get("turn_start_timestamp") or ""),
            "root_agent": str(row.get("root_agent") or "support_agent"),
            "has_error": bool(row.get("has_error")),
            "turn_latency_ms": int(row.get("turn_latency_ms") or 0),
            "error_summary": row.get("error_summary"),
            "_ai_index_trajectory": str(row.get("_ai_index_trajectory") or ""),
            "distance": round(max(0.01, 1.0 - sim), 4),
            "rerank_score": round(sim, 4),
        }
    )
  return hits


def _normalize_bq_row(row: Any) -> dict[str, Any]:
  out = dict(row)
  ts = out.get("turn_start_timestamp")
  if isinstance(ts, datetime):
    out["turn_start_timestamp"] = ts.isoformat()
  return out


def build_candidate_keys_param(rows: list[dict[str, Any]]) -> Any:
  """Builds a JSON-serializable BigQuery ARRAY<STRUCT> query parameter."""
  from google.cloud import bigquery

  key_params = [
      bigquery.StructQueryParameter(
          None,
          bigquery.ScalarQueryParameter(
              "session_id", "STRING", str(r["session_id"])
          ),
          bigquery.ScalarQueryParameter(
              "invocation_id", "STRING", str(r["invocation_id"])
          ),
      )
      for r in rows
  ]
  return bigquery.ArrayQueryParameter("candidate_keys", "STRUCT", key_params)


def _combine_job_stats(
    first: dict[str, Any], second: dict[str, Any]
) -> dict[str, Any]:
  return {
      "bytes_processed": int(first.get("bytes_processed") or 0)
      + int(second.get("bytes_processed") or 0),
      "slot_ms": int(first.get("slot_ms") or 0)
      + int(second.get("slot_ms") or 0),
      "search_unused_reasons": list(
          dict.fromkeys(
              list(first.get("search_unused_reasons") or [])
              + list(second.get("search_unused_reasons") or [])
          )
      ),
      "vector_unused_reasons": list(
          dict.fromkeys(
              list(first.get("vector_unused_reasons") or [])
              + list(second.get("vector_unused_reasons") or [])
          )
      ),
  }


def _execute_hybrid_python_fallback(
    bq_client: Any,
    *,
    search_queries: dict[str, str],
    replacements: dict[str, str],
    start_ts: datetime,
    end_ts: datetime,
    kw_text: str,
    sem_job_cfg: Any,
    top_k: int,
    candidate_k: int,
) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
  from google.cloud import bigquery

  kw_qname = "PATH_A_KEYWORD" if kw_text.strip() else "PATH_A_METADATA_FILTER"
  kw_sql = render_sql_tokens(search_queries[kw_qname], replacements)
  kw_cfg = bigquery.QueryJobConfig(
      query_parameters=[
          bigquery.ScalarQueryParameter("start_ts", "TIMESTAMP", start_ts),
          bigquery.ScalarQueryParameter("end_ts", "TIMESTAMP", end_ts),
          bigquery.ScalarQueryParameter("has_error", "BOOL", None),
          bigquery.ScalarQueryParameter("min_latency_ms", "INT64", None),
          bigquery.ScalarQueryParameter("search_query", "STRING", kw_text),
          bigquery.ScalarQueryParameter("top_k", "INT64", candidate_k),
      ],
      use_query_cache=False,
  )
  sem_params = [
      (
          bigquery.ScalarQueryParameter("top_k", "INT64", candidate_k)
          if getattr(p, "name", "") == "top_k"
          else p
      )
      for p in (getattr(sem_job_cfg, "query_parameters", None) or [])
  ]
  expanded_sem_cfg = bigquery.QueryJobConfig(
      query_parameters=sem_params,
      use_query_cache=False,
  )
  sem_sql = render_sql_tokens(
      search_queries["PATH_B_SYNC_EMBED_FALLBACK"], replacements
  )
  kw_job = bq_client.query(kw_sql, job_config=kw_cfg)
  kw_rows = [_normalize_bq_row(r) for r in kw_job.result()]
  sem_job = bq_client.query(sem_sql, job_config=expanded_sem_cfg)
  sem_rows = [_normalize_bq_row(r) for r in sem_job.result()]
  combined_stats = _combine_job_stats(
      _extract_job_stats(kw_job), _extract_job_stats(sem_job)
  )
  fused_rows = _fuse_rrf_rows(kw_rows, sem_rows, top_k=top_k)
  return "PATH_C_HYBRID_PYTHON_FALLBACK", fused_rows, combined_stats


def _run_live_bigquery_search(
    bq_client: Any,
    *,
    project_id: str,
    dataset_id: str,
    eval_turns_table: str,
    router: QueryRouter,
    queries: list[dict[str, str]],
    top_k: int,
    rerank: bool,
    embeddings_ready: int,
    endpoint: str,
    connection_id: str | None,
) -> list[dict[str, Any]]:
  from google.cloud import bigquery

  search_queries = load_named_sql_queries(
      _sql_dir() / "03_search_keyword_and_semantic.sql"
  )
  verify_sql_path = _sql_dir() / "04_verify_contradictions_ai_generate.sql"
  verify_template = verify_sql_path.read_text(encoding="utf-8")
  assert_pure_ascii_sql(verify_template, name=verify_sql_path.name)

  if not _ENDPOINT_RE.match(endpoint):
    raise ValueError(f"Invalid AI.GENERATE endpoint: {endpoint!r}")
  conn_arg = ""
  if connection_id:
    if not _CONNECTION_RE.match(connection_id):
      raise ValueError(f"Invalid connection_id: {connection_id!r}")
    conn_arg = f"connection_id => '{connection_id}', "

  replacements = {
      "PROJECT_ID": project_id,
      "DATASET_ID": dataset_id,
      "EVAL_TURNS_TABLE": eval_turns_table,
      "ENDPOINT": endpoint,
      "CONNECTION_ARG": conn_arg,
  }
  rendered_verify = render_sql_tokens(verify_template, replacements)
  if not model_uses_thinking_level(endpoint):
    rendered_verify = rendered_verify.replace(
        '{"thinking_level": "LOW"}',
        '{"thinking_budget": 0}',
    )

  # Measure dry-run raw trace baseline bytes once for comparison.
  table_fqn = f"{project_id}.{dataset_id}.{eval_turns_table}"
  baseline_cfg = bigquery.QueryJobConfig(dry_run=True, use_query_cache=False)
  baseline_job = bq_client.query(
      f"SELECT session_id, invocation_id, trace FROM `{table_fqn}`",
      job_config=baseline_cfg,
  )
  raw_baseline_bytes = int(
      getattr(baseline_job, "total_bytes_processed", 0) or 0
  )

  now_utc = datetime.now(timezone.utc)
  start_ts = now_utc - timedelta(days=30)
  end_ts = now_utc + timedelta(days=1)

  results: list[dict[str, Any]] = []
  for q_item in queries:
    qid = q_item["query_id"]
    qtext = q_item["query_text"]
    decision = router.classify(qtext)
    mode = decision.mode
    kw_text = sanitize_search_query_for_bq(qtext, mode=mode)

    if mode == ROUTE_KEYWORD:
      qname = "PATH_A_KEYWORD" if kw_text.strip() else "PATH_A_METADATA_FILTER"
    elif mode == ROUTE_NATURAL_LANGUAGE:
      if embeddings_ready <= 0:
        qname = "PATH_B_SYNC_EMBED_FALLBACK"
      elif rerank:
        qname = "PATH_B_AI_SEARCH_RERANK"
      else:
        qname = "PATH_B_AI_SEARCH_NO_RERANK"
    else:
      if embeddings_ready <= 0:
        qname = (
            "PATH_C_HYBRID_PYTHON_FALLBACK"
            if kw_text.strip()
            else "PATH_B_SYNC_EMBED_FALLBACK"
        )
      elif kw_text.strip():
        qname = "PATH_C_HYBRID_RRF" if rerank else "PATH_C_HYBRID_RRF_NO_RERANK"
      else:
        qname = (
            "PATH_B_AI_SEARCH_RERANK"
            if rerank
            else "PATH_B_AI_SEARCH_NO_RERANK"
        )

    candidate_k = max(top_k * 10, 100)
    search_param_val = kw_text if mode == ROUTE_KEYWORD else qtext
    params = [
        bigquery.ScalarQueryParameter("start_ts", "TIMESTAMP", start_ts),
        bigquery.ScalarQueryParameter("end_ts", "TIMESTAMP", end_ts),
        bigquery.ScalarQueryParameter("has_error", "BOOL", None),
        bigquery.ScalarQueryParameter("min_latency_ms", "INT64", None),
        bigquery.ScalarQueryParameter(
            "search_query", "STRING", search_param_val
        ),
        bigquery.ScalarQueryParameter("keyword_query", "STRING", kw_text),
        bigquery.ScalarQueryParameter("top_k", "INT64", top_k),
        bigquery.ScalarQueryParameter("candidate_k", "INT64", candidate_k),
    ]
    job_cfg = bigquery.QueryJobConfig(
        query_parameters=params, use_query_cache=False
    )
    if qname == "PATH_C_HYBRID_PYTHON_FALLBACK":
      qname, rows, stats = _execute_hybrid_python_fallback(
          bq_client,
          search_queries=search_queries,
          replacements=replacements,
          start_ts=start_ts,
          end_ts=end_ts,
          kw_text=kw_text,
          sem_job_cfg=job_cfg,
          top_k=top_k,
          candidate_k=candidate_k,
      )
    else:
      sql_stmt = render_sql_tokens(search_queries[qname], replacements)
      try:
        job = bq_client.query(sql_stmt, job_config=job_cfg)
        rows = [_normalize_bq_row(r) for r in job.result()]
        stats = _extract_job_stats(job)
      except Exception:
        if (
            mode == ROUTE_NATURAL_LANGUAGE
            and qname != "PATH_B_SYNC_EMBED_FALLBACK"
        ):
          qname = "PATH_B_SYNC_EMBED_FALLBACK"
          fallback_sql = render_sql_tokens(search_queries[qname], replacements)
          job = bq_client.query(fallback_sql, job_config=job_cfg)
          rows = [_normalize_bq_row(r) for r in job.result()]
          stats = _extract_job_stats(job)
        elif mode == ROUTE_HYBRID:
          qname, rows, stats = _execute_hybrid_python_fallback(
              bq_client,
              search_queries=search_queries,
              replacements=replacements,
              start_ts=start_ts,
              end_ts=end_ts,
              kw_text=kw_text,
              sem_job_cfg=job_cfg,
              top_k=top_k,
              candidate_k=candidate_k,
          )
        else:
          raise

    step2_lazy_trace = None
    if rows:
      top_hit = rows[0]
      step2_sql = render_sql_tokens(
          search_queries["STEP2_LAZY_TRACE_HYDRATION"], replacements
      )
      step2_cfg = bigquery.QueryJobConfig(
          query_parameters=[
              bigquery.ScalarQueryParameter(
                  "session_id", "STRING", str(top_hit["session_id"])
              ),
              bigquery.ScalarQueryParameter(
                  "invocation_id", "STRING", str(top_hit["invocation_id"])
              ),
          ]
      )
      hydrated = list(bq_client.query(step2_sql, job_config=step2_cfg).result())
      if hydrated:
        step2_lazy_trace = _normalize_bq_row(hydrated[0])

    verifications: list[dict[str, Any]] = []
    if rows:
      v_cfg = bigquery.QueryJobConfig(
          query_parameters=[
              build_candidate_keys_param(rows[:top_k]),
              bigquery.ScalarQueryParameter("top_k", "INT64", top_k),
          ]
      )
      v_rows = list(bq_client.query(rendered_verify, job_config=v_cfg).result())
      for vr in v_rows:
        vd = dict(vr)
        ai_status = str(vd.get("ai_generate_status") or "").strip()
        raw_label = str(vd.get("pattern_label") or "").strip()
        if ai_status or not raw_label:
          verifications.append(
              {
                  "session_id": str(vd["session_id"]),
                  "invocation_id": str(vd["invocation_id"]),
                  "is_silent_contradiction": False,
                  "pattern_label": "VERIFIER_ERROR",
                  "confidence": 0.0,
                  "evidence_snippet": str(
                      vd.get("evidence_snippet")
                      or ai_status
                      or "AI.GENERATE returned empty or error output"
                  ),
                  "ai_generate_status": ai_status or "EMPTY_OUTPUT",
              }
          )
        else:
          verifications.append(
              {
                  "session_id": str(vd["session_id"]),
                  "invocation_id": str(vd["invocation_id"]),
                  "is_silent_contradiction": bool(
                      vd.get("is_silent_contradiction")
                  ),
                  "pattern_label": raw_label,
                  "confidence": float(vd.get("confidence") or 0.0),
                  "evidence_snippet": str(vd.get("evidence_snippet") or ""),
                  "ai_generate_status": "",
              }
          )

    results.append(
        {
            "query_id": qid,
            "query_text": qtext,
            "sql_template": qname,
            "router_decision": decision.to_dict(),
            "step1_bytes_processed": stats["bytes_processed"],
            "raw_trace_baseline_bytes_processed": raw_baseline_bytes,
            "slot_ms": stats["slot_ms"],
            "search_unused_reasons": stats["search_unused_reasons"],
            "vector_unused_reasons": stats["vector_unused_reasons"],
            "step1_hits": rows,
            "step2_lazy_trace": step2_lazy_trace,
            "layer3_verifications": verifications,
        }
    )
  return results


def _fuse_rrf_rows(
    kw_rows: list[dict[str, Any]],
    sem_rows: list[dict[str, Any]],
    *,
    top_k: int,
) -> list[dict[str, Any]]:
  kw_rank = {
      (str(r["session_id"]), str(r["invocation_id"])): i + 1
      for i, r in enumerate(kw_rows)
  }
  sem_rank = {
      (str(r["session_id"]), str(r["invocation_id"])): i + 1
      for i, r in enumerate(sem_rows)
  }
  by_key = {
      (str(r["session_id"]), str(r["invocation_id"])): r
      for r in kw_rows + sem_rows
  }
  scored = []
  for key, row in by_key.items():
    score = (1.0 / (60 + kw_rank[key]) if key in kw_rank else 0.0) + (
        1.0 / (60 + sem_rank[key]) if key in sem_rank else 0.0
    )
    copy_row = dict(row)
    copy_row["rrf_score"] = round(score, 6)
    scored.append(copy_row)
  scored.sort(
      key=lambda r: (
          -float(r.get("rrf_score", 0.0)),
          float(1.0 if r.get("distance") is None else r.get("distance", 1.0)),
          str(r.get("session_id") or ""),
          str(r.get("invocation_id") or ""),
      )
  )
  return scored[:top_k]


def _default_offline_turn_rows() -> list[dict[str, Any]]:
  return generate_synthetic_eval_turns(count=10, prefix="offline_demo")


def run_index_and_search(
    *,
    project_id: str = "demo-project",
    dataset_id: str = "agent_ops",
    events_table: str = "agent_events",
    eval_turns_table: str = "eval_turns",
    location: str = "US",
    vertex_location: str = "global",
    connection_id: str | None = None,
    ai_generate_endpoint: str = "gemini-4-hydrogen",
    input_json: str | Path | None = None,
    output_json: str | Path = "search_results.json",
    top_k: int = _DEFAULT_TOP_K,
    rerank: bool = True,
    with_vector_index_scale: bool = False,
    embed_wait_seconds: int = 65,
    offline: bool = False,
    bq_client: Any = None,
    router: QueryRouter | None = None,
) -> dict[str, Any]:
  """Runs materialization, dual indexing, 5-query search, and L3 verify."""
  queries = [dict(q) for q in DEFAULT_BENCHMARK_QUERIES]
  active_router = router or QueryRouter(
      backend="offline" if offline else "auto",
      project_id=project_id,
      location=vertex_location,
      vertex_model=ai_generate_endpoint,
      offline=offline,
  )

  if offline:
    turn_rows: list[dict[str, Any]] = []
    if input_json is not None and Path(input_json).is_file():
      payload = json.loads(Path(input_json).read_text(encoding="utf-8"))
      turn_rows = list(payload.get("eval_turns") or [])
    if not turn_rows:
      turn_rows = _default_offline_turn_rows()

    for row in turn_rows:
      if not row.get("_ai_index_trajectory"):
        row["_ai_index_trajectory"] = _extract_s3_text(row.get("trace"), row)

    raw_baseline = sum(
        int(r.get("raw_trace_bytes") or 12000) for r in turn_rows
    )
    compact_bytes = sum(
        len(str(r.get("_ai_index_trajectory") or "").encode("utf-8"))
        for r in turn_rows
    )

    query_results: list[dict[str, Any]] = []
    for q_item in queries:
      qid = q_item["query_id"]
      qtext = q_item["query_text"]
      decision = active_router.classify(qtext)
      hits = _offline_search_rows(
          turn_rows, mode=decision.mode, query_text=qtext, top_k=top_k
      )
      step2_lazy = None
      if hits:
        top_key = (hits[0]["session_id"], hits[0]["invocation_id"])
        match = next(
            (
                r
                for r in turn_rows
                if (r["session_id"], r["invocation_id"]) == top_key
            ),
            None,
        )
        if match is not None:
          step2_lazy = {
              "session_id": match["session_id"],
              "invocation_id": match["invocation_id"],
              "turn_index": int(match.get("turn_index") or 0),
              "root_agent": str(match.get("root_agent") or "support_agent"),
              "has_error": bool(match.get("has_error")),
              "raw_trace_bytes": int(match.get("raw_trace_bytes") or 0),
              "trace": match.get("trace"),
          }
      verifications = [
          _offline_contradiction_check(
              next(
                  r
                  for r in turn_rows
                  if (r["session_id"], r["invocation_id"])
                  == (h["session_id"], h["invocation_id"])
              )
          )
          for h in hits[:top_k]
      ]
      kw_text = sanitize_search_query_for_bq(qtext, mode=decision.mode)
      if decision.mode == ROUTE_KEYWORD:
        offline_template = (
            "PATH_A_KEYWORD" if kw_text.strip() else "PATH_A_METADATA_FILTER"
        )
      elif decision.mode == ROUTE_NATURAL_LANGUAGE:
        offline_template = (
            "PATH_B_AI_SEARCH_RERANK"
            if rerank
            else "PATH_B_AI_SEARCH_NO_RERANK"
        )
      elif kw_text.strip():
        offline_template = (
            "PATH_C_HYBRID_RRF" if rerank else "PATH_C_HYBRID_RRF_NO_RERANK"
        )
      else:
        offline_template = (
            "PATH_B_AI_SEARCH_RERANK"
            if rerank
            else "PATH_B_AI_SEARCH_NO_RERANK"
        )
      query_results.append(
          {
              "query_id": qid,
              "query_text": qtext,
              "sql_template": offline_template,
              "router_decision": decision.to_dict(),
              "step1_bytes_processed": max(256, compact_bytes),
              "raw_trace_baseline_bytes_processed": max(1024, raw_baseline),
              "slot_ms": 0,
              "search_unused_reasons": ["OFFLINE_MOCK_EXECUTION"],
              "vector_unused_reasons": ["SKIPPED_BELOW_5000_ROWS"],
              "step1_hits": hits,
              "step2_lazy_trace": step2_lazy,
              "layer3_verifications": verifications,
          }
      )

    report = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "project_id": project_id,
        "dataset_id": dataset_id,
        "eval_turns_table": eval_turns_table,
        "offline": True,
        "materialization_source": "offline_fixture",
        "index_status": {
            "ai_embed_column": "MOCK",
            "search_index": "SCAN_FALLBACK_BELOW_10GIB",
            "vector_index": (
                "CREATED"
                if len(turn_rows) >= _IVF_MIN_ROWS
                else "SKIPPED_BELOW_5000_ROWS"
            ),
            "row_count": len(turn_rows),
            "embeddings_ready": len(turn_rows),
        },
        "queries": query_results,
    }
  else:
    from google.cloud import bigquery

    raw_where_clause = ""
    if (
        not with_vector_index_scale
        and input_json is not None
        and Path(input_json).is_file()
    ):
      payload = json.loads(Path(input_json).read_text(encoding="utf-8"))
      session_ids = [str(s) for s in (payload.get("session_ids") or []) if s]
      if not session_ids:
        session_ids = [
            str(t["session_id"])
            for t in (payload.get("eval_turns") or [])
            if t.get("session_id")
        ]
      if session_ids:
        raw_where_clause = _build_session_raw_where_clause(session_ids)

    client = bq_client or bigquery.Client(project=project_id, location=location)
    assert_demo_table_ownership_or_absent(
        client, project_id, dataset_id, eval_turns_table
    )
    materialize_ddl, mat_source = build_materialize_eval_turns_sql(
        project_id=project_id,
        dataset_id=dataset_id,
        events_table=events_table,
        eval_turns_table=eval_turns_table,
        raw_where_clause=raw_where_clause,
    )
    client.query(materialize_ddl).result()
    populate_trajectories_in_bigquery(
        client,
        project_id=project_id,
        dataset_id=dataset_id,
        eval_turns_table=eval_turns_table,
    )
    if with_vector_index_scale:
      seed_scale_rows_to_bigquery(
          client,
          project_id=project_id,
          dataset_id=dataset_id,
          eval_turns_table=eval_turns_table,
          count=_IVF_MIN_ROWS,
          location=location,
      )
    index_status = setup_dual_indexes(
        client,
        project_id=project_id,
        dataset_id=dataset_id,
        eval_turns_table=eval_turns_table,
        embed_wait_seconds=embed_wait_seconds,
    )
    query_results = _run_live_bigquery_search(
        client,
        project_id=project_id,
        dataset_id=dataset_id,
        eval_turns_table=eval_turns_table,
        router=active_router,
        queries=queries,
        top_k=top_k,
        rerank=rerank,
        embeddings_ready=int(index_status["embeddings_ready"]),
        endpoint=ai_generate_endpoint,
        connection_id=connection_id,
    )
    report = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "project_id": project_id,
        "dataset_id": dataset_id,
        "eval_turns_table": eval_turns_table,
        "offline": False,
        "materialization_source": mat_source,
        "index_status": index_status,
        "queries": query_results,
    }

  out_path = Path(output_json)
  write_owner_only_text(out_path, json.dumps(report, indent=2))
  return report


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
  parser = argparse.ArgumentParser(
      description=(
          "Materialize eval_turns, build BigQuery dual indexes, and run the "
          "4-layer cost-aware search + contradiction verification suite."
      )
  )
  parser.add_argument("--project-id", default="demo-project")
  parser.add_argument("--dataset-id", default="agent_ops")
  parser.add_argument("--events-table", default="agent_events")
  parser.add_argument("--eval-turns-table", default="eval_turns")
  parser.add_argument("--location", default="US")
  parser.add_argument("--vertex-location", default="global")
  parser.add_argument("--connection-id", default=None)
  parser.add_argument("--ai-generate-endpoint", default="gemini-4-hydrogen")
  parser.add_argument("--input-json", default=None)
  parser.add_argument("--output-json", default="search_results.json")
  parser.add_argument("--top-k", type=int, default=_DEFAULT_TOP_K)
  parser.add_argument("--no-rerank", action="store_true")
  parser.add_argument("--with-vector-index-scale", action="store_true")
  parser.add_argument("--embed-wait-seconds", type=int, default=65)
  parser.add_argument("--offline", action="store_true")
  return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
  args = _parse_args(argv)
  report = run_index_and_search(
      project_id=args.project_id,
      dataset_id=args.dataset_id,
      events_table=args.events_table,
      eval_turns_table=args.eval_turns_table,
      location=args.location,
      vertex_location=args.vertex_location,
      connection_id=args.connection_id,
      ai_generate_endpoint=args.ai_generate_endpoint,
      input_json=args.input_json,
      output_json=args.output_json,
      top_k=args.top_k,
      rerank=not args.no_rerank,
      with_vector_index_scale=args.with_vector_index_scale,
      embed_wait_seconds=args.embed_wait_seconds,
      offline=args.offline,
  )
  print(
      json.dumps(
          {
              "output_json": args.output_json,
              "index_status": report["index_status"],
              "queries_executed": len(report["queries"]),
          },
          indent=2,
      )
  )
  return 0


if __name__ == "__main__":
  sys.exit(main())
