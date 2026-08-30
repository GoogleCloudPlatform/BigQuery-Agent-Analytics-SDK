#!/usr/bin/env python
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

"""Synthesize an EvalBench-shaped dataset from real BQAA agent traces.

The EvalBench MVP demo (`examples/evalbench_mvp_e2e.sh`, #435 slice 5, #97)
needs an EvalBench job to import. When no EvalBench run is available this
script builds one from traces an agent really produced: it reads a BQAA
``agent_events`` table, folds each session into one EvalBench ``results``
row plus one ``scores`` row, and writes ``configs`` / ``results`` /
``scores`` tables that ``EvalBenchRun.from_bigquery`` reads unchanged.

Nothing textual is invented. Every prompt is the session's real
``USER_MESSAGE_RECEIVED`` text, every final response is the real
``AGENT_COMPLETED`` / ``AGENT_RESPONSE`` text, and sessions with no user
prompt are skipped rather than given one. The only synthesized value is the
``goal_completion`` score: ``1.0`` when the session reached
``AGENT_COMPLETED``, ``0.0`` otherwise (``returncode`` mirrors this as
``0`` / ``1``). It says *completed*, not *correct* -- that is what step 3 of
the demo, the LLM judge, is for.

Mapping (one result per session, keyed by the first eight characters of the
session id unless that would collide, then the full session id):

  results.id / eval_id       scenario id
  results.prompt / nl_prompt first USER_MESSAGE_RECEIVED content.text_summary
  results.final_response     AGENT_COMPLETED text, else last AGENT_RESPONSE
  results.stdout             same text (EvalBench's agentic field name)
  results.returncode         0 if AGENT_COMPLETED was logged, else 1
  results.run_time           timestamp of the USER_MESSAGE_RECEIVED event
  results.tool_calls         JSON list of TOOL_STARTING / TOOL_COMPLETED pairs
  results.error_message      first error_message logged in the session
  scores                     comparator=goal_completion, score=1.0|0.0
  configs                    experiment_config.orchestrator=<agent>,
                             model_config.generator=<adk app_name>

Usage (defaults are the demo's own names; only the project is required):

  python examples/evalbench_synth_from_traces.py \
      --project test-project-0728-467323 \
      --source-table bqaa_e2e_real.agent_events \
      --evalbench-dataset bqaa_evalbench_mvp_demo \
      --mirror-dataset bqaa_evalbench_mvp_mirror \
      --job-id mvp-e2e-real-traces

  --dry-run prints the synthesized rows as JSON and writes nothing.

The EvalBench-shaped dataset and the mirror dataset are created when
missing. The script refuses to write into the source dataset or into the
ADK plugin's ``agent_analytics`` dataset.
"""

from __future__ import annotations

import argparse
import dataclasses
from datetime import datetime
from datetime import timezone
import json
import os
import sys
from typing import Any, Iterable, Mapping, Optional, Sequence

DEFAULT_SOURCE_TABLE = "bqaa_e2e_real.agent_events"
DEFAULT_EVALBENCH_DATASET = "bqaa_evalbench_mvp_demo"
DEFAULT_MIRROR_DATASET = "bqaa_evalbench_mvp_mirror"
DEFAULT_JOB_ID = "mvp-e2e-real-traces"
DEFAULT_LOCATION = "US"
COMPARATOR = "goal_completion"
_SHORT_ID_LENGTH = 8
_RESERVED_TARGET_DATASETS = frozenset({"agent_analytics"})

_RESULT_SCHEMA = (
    ("job_id", "STRING", "REQUIRED"),
    ("id", "STRING", "REQUIRED"),
    ("eval_id", "STRING", "REQUIRED"),
    ("prompt", "STRING", "REQUIRED"),
    ("nl_prompt", "STRING", "REQUIRED"),
    ("final_response", "STRING", "NULLABLE"),
    ("stdout", "STRING", "NULLABLE"),
    ("returncode", "INT64", "REQUIRED"),
    ("run_time", "TIMESTAMP", "REQUIRED"),
    ("tool_calls", "STRING", "REQUIRED"),
    ("error_message", "STRING", "NULLABLE"),
    ("source_session_id", "STRING", "REQUIRED"),
    ("source_table", "STRING", "REQUIRED"),
)
_SCORE_SCHEMA = (
    ("job_id", "STRING", "REQUIRED"),
    ("id", "STRING", "REQUIRED"),
    ("eval_id", "STRING", "REQUIRED"),
    ("comparator", "STRING", "REQUIRED"),
    ("score", "FLOAT64", "REQUIRED"),
    ("run_time", "TIMESTAMP", "REQUIRED"),
    ("source_session_id", "STRING", "REQUIRED"),
)
_CONFIG_SCHEMA = (
    ("job_id", "STRING", "REQUIRED"),
    ("run_time", "TIMESTAMP", "REQUIRED"),
    ("config", "STRING", "REQUIRED"),
    ("value", "STRING", "NULLABLE"),
)

_READ_EVENTS_QUERY = """\
SELECT
  timestamp,
  event_type,
  agent,
  session_id,
  span_id,
  TO_JSON_STRING(content) AS content,
  TO_JSON_STRING(attributes) AS attributes,
  status,
  error_message
FROM `{source_table}`
ORDER BY session_id, timestamp, span_id
"""


@dataclasses.dataclass(frozen=True)
class SynthTables:
  """The three EvalBench-shaped row lists plus the sessions left out."""

  results: list[dict[str, Any]]
  scores: list[dict[str, Any]]
  configs: list[dict[str, Any]]
  skipped_sessions: list[str]

  @property
  def completed(self) -> int:
    return sum(1 for row in self.results if row["returncode"] == 0)


@dataclasses.dataclass
class _Session:
  session_id: str
  prompt: Optional[str] = None
  run_time: Optional[datetime] = None
  agent: Optional[str] = None
  app_name: Optional[str] = None
  completed: bool = False
  completed_text: Optional[str] = None
  last_response: Optional[str] = None
  error_message: Optional[str] = None
  tool_calls: list[dict[str, Any]] = dataclasses.field(default_factory=list)
  _open_by_span: dict[str, dict[str, Any]] = dataclasses.field(
      default_factory=dict
  )


def check_targets(
    *, source_table: str, evalbench_dataset: str, mirror_dataset: str
) -> None:
  """Refuse to write into the source dataset or the ADK plugin's dataset."""
  source_dataset = source_table.split(".")[-2] if "." in source_table else ""
  for name, dataset in (
      ("--evalbench-dataset", evalbench_dataset),
      ("--mirror-dataset", mirror_dataset),
  ):
    if dataset == source_dataset:
      raise ValueError(
          f"{name} {dataset!r} must not be the source dataset of"
          f" {source_table!r}"
      )
    if dataset.lower() in _RESERVED_TARGET_DATASETS:
      raise ValueError(
          f"{name} {dataset!r} must not be the ADK plugin's dataset"
      )
  if evalbench_dataset == mirror_dataset:
    raise ValueError(
        "--evalbench-dataset and --mirror-dataset must differ (the importer"
        " reads the first and writes the second)"
    )


def synthesize(
    events: Iterable[Mapping[str, Any]], *, job_id: str, source_table: str
) -> SynthTables:
  """Fold ``agent_events`` rows into EvalBench ``results``/``scores``/``configs``.

  ``events`` are plain mappings shaped like ``agent_events`` rows;
  ``content`` / ``attributes`` may be dicts, JSON strings (as
  ``TO_JSON_STRING`` returns them), or ``None``. Rows are processed in
  ``(session_id, timestamp)`` order. Sessions without a usable
  ``USER_MESSAGE_RECEIVED`` text are reported in ``skipped_sessions``.
  """
  sessions: dict[str, _Session] = {}
  ordered = sorted(
      events,
      key=lambda row: (
          str(row.get("session_id") or ""),
          _timestamp(row.get("timestamp"))
          or datetime.min.replace(tzinfo=timezone.utc),
      ),
  )
  for row in ordered:
    session_id = _text(row.get("session_id"))
    if session_id is None:
      continue
    session = sessions.setdefault(session_id, _Session(session_id=session_id))
    _fold_event(session, row)

  prompted = [s for s in sessions.values() if s.prompt is not None]
  skipped = sorted(s.session_id for s in sessions.values() if s.prompt is None)
  if not prompted:
    raise ValueError(
        f"no session in {source_table!r} has a USER_MESSAGE_RECEIVED text;"
        " nothing to synthesize (prompts are never invented)"
    )

  scenario_ids = _scenario_ids([s.session_id for s in prompted])
  results: list[dict[str, Any]] = []
  scores: list[dict[str, Any]] = []
  for session in prompted:
    scenario_id = scenario_ids[session.session_id]
    final_text = session.completed_text or session.last_response
    result: dict[str, Any] = {
        "job_id": job_id,
        "id": scenario_id,
        "eval_id": scenario_id,
        "prompt": session.prompt,
        "nl_prompt": session.prompt,
    }
    if final_text is not None:
      result["final_response"] = final_text
      result["stdout"] = final_text
    result.update(
        {
            "returncode": 0 if session.completed else 1,
            "run_time": session.run_time,
            "tool_calls": json.dumps(session.tool_calls, sort_keys=True),
            "error_message": session.error_message,
            "source_session_id": session.session_id,
            "source_table": source_table,
        }
    )
    results.append(result)
    scores.append(
        {
            "job_id": job_id,
            "id": scenario_id,
            "eval_id": scenario_id,
            "comparator": COMPARATOR,
            "score": 1.0 if session.completed else 0.0,
            "run_time": session.run_time,
            "source_session_id": session.session_id,
        }
    )
  results.sort(key=lambda row: row["id"])
  scores.sort(key=lambda row: row["id"])

  first_run_time = min(s.run_time for s in prompted if s.run_time is not None)
  agent = _most_common(s.agent for s in prompted) or "unknown"
  app_name = _most_common(s.app_name for s in prompted)
  configs = [
      {
          "job_id": job_id,
          "run_time": first_run_time,
          "config": "experiment_config.orchestrator",
          "value": agent,
      },
      {
          "job_id": job_id,
          "run_time": first_run_time,
          "config": "model_config.generator",
          "value": app_name,
      },
      {
          "job_id": job_id,
          "run_time": first_run_time,
          "config": "bqaa.source_table",
          "value": source_table,
      },
  ]
  return SynthTables(
      results=results, scores=scores, configs=configs, skipped_sessions=skipped
  )


def _fold_event(session: _Session, row: Mapping[str, Any]) -> None:
  event_type = _text(row.get("event_type")) or ""
  content = _structured(row.get("content"))
  content_map = content if isinstance(content, Mapping) else {}
  attributes = _structured(row.get("attributes"))
  attributes_map = attributes if isinstance(attributes, Mapping) else {}

  if session.agent is None:
    session.agent = _text(row.get("agent")) or _text(
        attributes_map.get("root_agent_name")
    )
  if session.app_name is None:
    adk = attributes_map.get("adk")
    if isinstance(adk, Mapping):
      session.app_name = _text(adk.get("app_name"))
  error = _text(row.get("error_message"))
  if error is not None and session.error_message is None:
    session.error_message = error

  if event_type == "USER_MESSAGE_RECEIVED":
    if session.prompt is None:
      prompt = _first_text(content_map, ("text_summary", "text"))
      if prompt is None and isinstance(content, str):
        prompt = _text(content)
      if prompt is not None:
        session.prompt = prompt
        session.run_time = _timestamp(row.get("timestamp"))
  elif event_type == "AGENT_COMPLETED":
    session.completed = True
    text = _first_text(content_map, ("text_summary", "response", "text"))
    if text is None and isinstance(content, str):
      text = _text(content)
    if text is not None:
      session.completed_text = text
  elif event_type == "AGENT_RESPONSE":
    text = _first_text(content_map, ("response", "text_summary", "text"))
    if text is None and isinstance(content, str):
      text = _text(content)
    if text is not None:
      session.last_response = text
  elif event_type == "TOOL_STARTING":
    name = _first_text(content_map, ("tool", "tool_name", "name"))
    if name is None:
      return
    call: dict[str, Any] = {
        "tool_name": name,
        "args": content_map.get("args") or {},
    }
    session.tool_calls.append(call)
    span_id = _text(row.get("span_id"))
    if span_id is not None:
      session._open_by_span[span_id] = call
  elif event_type in ("TOOL_COMPLETED", "TOOL_ERROR"):
    name = _first_text(content_map, ("tool", "tool_name", "name"))
    call = _match_tool_call(session, _text(row.get("span_id")), name)
    if call is None:
      return
    if "result" in content_map:
      call["result"] = content_map.get("result")
    if event_type == "TOOL_ERROR" or _text(row.get("status")) == "ERROR":
      call["error"] = error or content_map.get("error") or "TOOL_ERROR"


def _match_tool_call(
    session: _Session, span_id: Optional[str], name: Optional[str]
) -> Optional[dict[str, Any]]:
  if span_id is not None and span_id in session._open_by_span:
    return session._open_by_span.pop(span_id)
  for call in session.tool_calls:
    if "result" in call or "error" in call:
      continue
    if name is None or call["tool_name"] == name:
      return call
  return None


def _scenario_ids(session_ids: Sequence[str]) -> dict[str, str]:
  short = {sid: sid[:_SHORT_ID_LENGTH] for sid in session_ids}
  if len(set(short.values())) == len(session_ids):
    return short
  return {sid: sid for sid in session_ids}


def _first_text(
    mapping: Mapping[str, Any], keys: Sequence[str]
) -> Optional[str]:
  for key in keys:
    text = _text(mapping.get(key))
    if text is not None:
      return text
  return None


def _text(value: Any) -> Optional[str]:
  if value is None or isinstance(value, bool):
    return None
  if not isinstance(value, str):
    return None
  stripped = value.strip()
  return stripped or None


def _structured(value: Any) -> Any:
  if isinstance(value, str):
    stripped = value.strip()
    if stripped.startswith(("{", "[", '"')) or stripped == "null":
      try:
        return json.loads(stripped)
      except ValueError:
        return value
  return value


def _timestamp(value: Any) -> Optional[datetime]:
  if isinstance(value, datetime):
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
  if isinstance(value, str) and value.strip():
    text = value.strip().replace(" ", "T", 1)
    if text.endswith("Z"):
      text = text[:-1] + "+00:00"
    try:
      parsed = datetime.fromisoformat(text)
    except ValueError:
      return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
  return None


def _most_common(values: Iterable[Optional[str]]) -> Optional[str]:
  counts: dict[str, int] = {}
  for value in values:
    if value is not None:
      counts[value] = counts.get(value, 0) + 1
  if not counts:
    return None
  return sorted(counts.items(), key=lambda item: (-item[1], item[0]))[0][0]


# --------------------------------------------------------------------------- #
# BigQuery I/O (only reached from main()).
# --------------------------------------------------------------------------- #
def _qualify(project: str, table: str) -> str:
  parts = table.split(".")
  if len(parts) == 2:
    return f"{project}.{table}"
  if len(parts) == 3:
    return table
  raise ValueError(
      f"--source-table must be dataset.table or project.dataset.table, got {table!r}"
  )


def load_events(
    client: Any, *, source_table: str, location: str
) -> list[dict[str, Any]]:
  query = _READ_EVENTS_QUERY.format(source_table=source_table)
  return [
      dict(row.items())
      for row in client.query(query, location=location).result()
  ]


def _schema(fields: Sequence[tuple[str, str, str]]) -> list:
  from google.cloud import bigquery

  return [
      bigquery.SchemaField(name, kind, mode=mode) for name, kind, mode in fields
  ]


def _json_ready(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
  ready = []
  for row in rows:
    ready.append(
        {
            key: (value.isoformat() if isinstance(value, datetime) else value)
            for key, value in row.items()
        }
    )
  return ready


def write_tables(
    client: Any,
    *,
    project: str,
    evalbench_dataset: str,
    mirror_dataset: str,
    tables: SynthTables,
    location: str,
) -> dict[str, int]:
  """Create both datasets if missing and (re)load the three source tables."""
  from google.cloud import bigquery

  for dataset in (evalbench_dataset, mirror_dataset):
    ref = bigquery.Dataset(f"{project}.{dataset}")
    ref.location = location
    client.create_dataset(ref, exists_ok=True)

  written: dict[str, int] = {}
  for name, rows, fields in (
      ("configs", tables.configs, _CONFIG_SCHEMA),
      ("results", tables.results, _RESULT_SCHEMA),
      ("scores", tables.scores, _SCORE_SCHEMA),
  ):
    table_id = f"{project}.{evalbench_dataset}.{name}"
    job_config = bigquery.LoadJobConfig(
        schema=_schema(fields),
        write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
        source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
    )
    client.load_table_from_json(
        _json_ready(rows), table_id, job_config=job_config, location=location
    ).result()
    written[table_id] = len(rows)
  return written


def _parse_args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
  parser = argparse.ArgumentParser(
      description=(
          "Synthesize EvalBench-shaped configs/results/scores tables from a"
          " real BQAA agent_events table (#435 slice 5, #97)."
      )
  )
  parser.add_argument(
      "--project",
      default=os.environ.get("BQ_AGENT_PROJECT")
      or os.environ.get("GOOGLE_CLOUD_PROJECT"),
      help="Project holding the source traces and both target datasets"
      " (default: $BQ_AGENT_PROJECT or $GOOGLE_CLOUD_PROJECT).",
  )
  parser.add_argument(
      "--source-table",
      default=os.environ.get("EVALBENCH_SOURCE_TABLE", DEFAULT_SOURCE_TABLE),
      help="Real agent_events table, dataset.table or project.dataset.table"
      f" (default: {DEFAULT_SOURCE_TABLE}).",
  )
  parser.add_argument(
      "--evalbench-dataset",
      default=os.environ.get("EVALBENCH_DATASET", DEFAULT_EVALBENCH_DATASET),
      help="EvalBench-shaped dataset to (re)build"
      f" (default: {DEFAULT_EVALBENCH_DATASET}).",
  )
  parser.add_argument(
      "--mirror-dataset",
      default=os.environ.get("BQ_AGENT_DATASET", DEFAULT_MIRROR_DATASET),
      help="BQAA mirror dataset evalbench-import will write to; created if"
      f" missing (default: {DEFAULT_MIRROR_DATASET}).",
  )
  parser.add_argument(
      "--job-id",
      default=os.environ.get("EVALBENCH_JOB_ID", DEFAULT_JOB_ID),
      help=f"EvalBench job_id stamped on every row (default: {DEFAULT_JOB_ID}).",
  )
  parser.add_argument(
      "--location",
      default=os.environ.get("BQ_AGENT_LOCATION", DEFAULT_LOCATION),
      help=f"BigQuery location for new datasets (default: {DEFAULT_LOCATION}).",
  )
  parser.add_argument(
      "--dry-run",
      action="store_true",
      help="Read the traces and print the synthesized rows as JSON; write nothing.",
  )
  args = parser.parse_args(argv)
  if not args.project:
    parser.error(
        "--project (or $BQ_AGENT_PROJECT / $GOOGLE_CLOUD_PROJECT) is required"
    )
  return args


def main(argv: Optional[Sequence[str]] = None) -> int:
  args = _parse_args(argv)
  source_table = _qualify(args.project, args.source_table)
  check_targets(
      source_table=source_table,
      evalbench_dataset=args.evalbench_dataset,
      mirror_dataset=args.mirror_dataset,
  )

  from google.cloud import bigquery

  client = bigquery.Client(project=args.project)
  events = load_events(
      client, source_table=source_table, location=args.location
  )
  tables = synthesize(events, job_id=args.job_id, source_table=source_table)

  summary = {
      "job_id": args.job_id,
      "source_table": source_table,
      "source_event_count": len(events),
      "scenarios": len(tables.results),
      "completed": tables.completed,
      "not_completed": len(tables.results) - tables.completed,
      "skipped_sessions": tables.skipped_sessions,
  }
  if args.dry_run:
    print(
        json.dumps(
            {
                **summary,
                "configs": _json_ready(tables.configs),
                "results": _json_ready(tables.results),
                "scores": _json_ready(tables.scores),
            },
            indent=2,
        )
    )
    return 0

  written = write_tables(
      client,
      project=args.project,
      evalbench_dataset=args.evalbench_dataset,
      mirror_dataset=args.mirror_dataset,
      tables=tables,
      location=args.location,
  )
  print(
      json.dumps(
          {
              **summary,
              "evalbench_dataset": f"{args.project}.{args.evalbench_dataset}",
              "mirror_dataset": f"{args.project}.{args.mirror_dataset}",
              "written": written,
          },
          indent=2,
      )
  )
  return 0


if __name__ == "__main__":
  sys.exit(main())
