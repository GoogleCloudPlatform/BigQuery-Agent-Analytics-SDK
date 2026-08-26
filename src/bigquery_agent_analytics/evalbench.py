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
"""Read EvalBench BigQuery runs and map them to BQAA event rows.

The reader is deliberately pull-only: EvalBench keeps ownership of its
``configs``, ``results``, and ``scores`` tables, while BQAA converts one
``job_id`` at a time into the mirror-table row contract tracked by issue #97.
Writing those rows is a separate phase so idempotency and table ownership can
be reviewed independently from source-schema normalization.
"""

from __future__ import annotations

import ast
from collections.abc import Mapping
import dataclasses
from datetime import date
from datetime import datetime
from datetime import timedelta
from datetime import timezone
import hashlib
import json
import re
from typing import Any, Optional

from google.cloud import bigquery

from ._telemetry import make_bq_client
from ._telemetry import with_sdk_labels

_SOURCE_SEGMENT_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")
_MISSING_TEXT = frozenset({"", "<na>", "nan", "none", "null"})
_NO_GENERATED_OUTPUT = frozenset({"skipped"})
_UNKNOWN_RUN_TIME = datetime(1970, 1, 1, tzinfo=timezone.utc)

_READ_SOURCE_TABLE_QUERY = """\
SELECT *
FROM `{table_id}`
WHERE job_id = @job_id
"""


@dataclasses.dataclass(frozen=True)
class EvalBenchRun:
  """One EvalBench job loaded for conversion to BQAA trace rows.

  ``results`` and ``scores`` retain the source rows as plain Python mappings.
  ``config_rows`` carries EvalBench's flattened experiment/model settings,
  which provide the run timestamp and agent identity for synthetic events.
  """

  project_id: str
  evalbench_dataset: str
  job_id: str
  location: Optional[str] = None
  results: tuple[dict[str, Any], ...] = dataclasses.field(
      default_factory=tuple, repr=False
  )
  scores: tuple[dict[str, Any], ...] = dataclasses.field(
      default_factory=tuple, repr=False
  )
  config_rows: tuple[dict[str, Any], ...] = dataclasses.field(
      default_factory=tuple, repr=False
  )

  @classmethod
  def from_bigquery(
      cls,
      *,
      project_id: str,
      evalbench_dataset: str,
      job_id: str,
      location: Optional[str] = None,
      bq_client: Optional[Any] = None,
  ) -> "EvalBenchRun":
    """Load one EvalBench run's configs, results, and scores.

    Every source query filters on ``job_id`` in BigQuery rather than loading
    a whole table and filtering in Python. The current contract intentionally
    loads one run into memory; paging very large runs is a future extension.

    Args:
      project_id: Project containing the EvalBench tables.
      evalbench_dataset: Dataset containing ``configs``, ``results``, and
        ``scores``.
      job_id: EvalBench job identifier to load.
      location: Optional BigQuery location.
      bq_client: Optional test-compatible or caller-configured BigQuery client.

    Returns:
      An ``EvalBenchRun`` containing plain in-memory source rows.
    """
    _validate_source_segment("project_id", project_id)
    _validate_source_segment("evalbench_dataset", evalbench_dataset)
    if not isinstance(job_id, str) or not job_id:
      raise ValueError("job_id must be a non-empty string")

    client = bq_client or make_bq_client(project_id, location=location)
    table_prefix = f"{project_id}.{evalbench_dataset}"
    return cls(
        project_id=project_id,
        evalbench_dataset=evalbench_dataset,
        job_id=job_id,
        location=location,
        results=_read_source_rows(
            client,
            table_id=f"{table_prefix}.results",
            job_id=job_id,
            location=location,
        ),
        scores=_read_source_rows(
            client,
            table_id=f"{table_prefix}.scores",
            job_id=job_id,
            location=location,
        ),
        config_rows=_read_source_rows(
            client,
            table_id=f"{table_prefix}.configs",
            job_id=job_id,
            location=location,
        ),
    )

  def to_agent_event_rows(self) -> list[dict[str, Any]]:
    """Convert loaded results to BQAA-compatible synthetic event rows.

    Supports both EvalBench's NL2SQL field names
    (``id``/``nl_prompt``/``generated_sql``) and its current agentic names
    (``eval_id``/``prompt``/``stdout``). Missing tool calls emit no tool rows;
    missing final output omits ``AGENT_COMPLETED``. A missing prompt or
    scenario identifier is a hard error because no valid session can be
    constructed without them.
    """
    config = _config_values(self.config_rows)
    agent = _agent_name(config)
    config_run_time = _first_run_time(self.config_rows)

    prepared: list[tuple[str, int, dict[str, Any]]] = []
    scenario_indexes: dict[str, int] = {}
    for source_index, result in enumerate(self.results):
      scenario_id = _scenario_id(result)
      previous_index = scenario_indexes.get(scenario_id)
      if previous_index is not None:
        raise ValueError(
            f"EvalBench job {self.job_id!r} contains duplicate scenario id "
            f"{scenario_id!r} at result indexes {previous_index} and "
            f"{source_index}"
        )
      scenario_indexes[scenario_id] = source_index
      prepared.append((scenario_id, source_index, result))

    rows: list[dict[str, Any]] = []
    for scenario_id, _, result in sorted(
        prepared, key=lambda item: (item[0], item[1])
    ):
      prompt = _prompt(result)
      if prompt is None:
        raise ValueError(
            f"EvalBench scenario {scenario_id!r} is missing nl_prompt/prompt"
        )

      run_time = _result_run_time(result) or config_run_time
      missing_run_time = run_time is None
      run_time = run_time or _UNKNOWN_RUN_TIME
      session_id = f"evalbench:{self.job_id}:{scenario_id}"
      invocation_id = _stable_id(session_id, "invocation", length=32)
      root_span_id = _stable_id(session_id, "user", length=16)
      attributes = _base_attributes(
          result=result,
          project_id=self.project_id,
          dataset_id=self.evalbench_dataset,
          job_id=self.job_id,
          scenario_id=scenario_id,
          agent=agent,
      )
      if missing_run_time:
        attributes["evalbench_run_time_missing"] = True

      error_fields = _source_error_fields(result)
      if error_fields:
        attributes["evalbench_error_fields"] = error_fields
      source_error = _source_error_message(result, error_fields)
      source_status = "ERROR" if source_error else "OK"
      final_response = _final_response(result)
      usage, response_latency = _usage_and_latency(result)
      prompt_attributes = dict(attributes)
      if final_response is None:
        prompt_attributes.update(usage)

      rows.append(
          _event_row(
              event_type="USER_MESSAGE_RECEIVED",
              timestamp=run_time,
              agent=agent,
              session_id=session_id,
              invocation_id=invocation_id,
              span_id=root_span_id,
              parent_span_id=None,
              content={"text": prompt, "text_summary": prompt},
              attributes=prompt_attributes,
              latency_ms=(response_latency if final_response is None else None),
              status=source_status if final_response is None else "OK",
              error_message=source_error if final_response is None else None,
          )
      )

      sequence = 1
      for tool_index, tool_call in enumerate(_tool_calls(result)):
        tool_name = tool_call["tool_name"]
        tool_args = tool_call.get("args") or {}
        tool_result = tool_call.get("result")
        tool_error = _usable_text(tool_call.get("error"))
        tool_status = "ERROR" if tool_error else "OK"
        tool_span_id = _stable_id(
            session_id, "tool", str(tool_index), length=16
        )
        start_summary = f"{tool_name}({_compact_json(tool_args)})"
        rows.append(
            _event_row(
                event_type="TOOL_STARTING",
                timestamp=run_time + timedelta(microseconds=sequence),
                agent=agent,
                session_id=session_id,
                invocation_id=invocation_id,
                span_id=tool_span_id,
                parent_span_id=root_span_id,
                content={
                    "tool": tool_name,
                    "args": _json_safe(tool_args),
                    "text_summary": start_summary,
                },
                attributes=attributes,
            )
        )
        sequence += 1

        rendered_result = tool_result if tool_result is not None else tool_error
        result_summary = f"{tool_name} -> {_one_line(rendered_result)}"
        rows.append(
            _event_row(
                event_type="TOOL_ERROR" if tool_error else "TOOL_COMPLETED",
                timestamp=run_time + timedelta(microseconds=sequence),
                agent=agent,
                session_id=session_id,
                invocation_id=invocation_id,
                span_id=tool_span_id,
                parent_span_id=root_span_id,
                content={
                    "tool": tool_name,
                    "result": _json_safe(rendered_result),
                    "text_summary": result_summary,
                },
                attributes=attributes,
                latency_ms=_tool_latency(tool_call),
                status=tool_status,
                error_message=tool_error,
            )
        )
        sequence += 1

      if final_response is None:
        continue

      response_attributes = dict(attributes)
      response_attributes.update(usage)
      rows.append(
          _event_row(
              event_type="AGENT_COMPLETED",
              timestamp=run_time + timedelta(microseconds=sequence),
              agent=agent,
              session_id=session_id,
              invocation_id=invocation_id,
              span_id=_stable_id(session_id, "agent-completed", length=16),
              parent_span_id=root_span_id,
              content={
                  "response": final_response,
                  "text_summary": final_response,
              },
              attributes=response_attributes,
              latency_ms=response_latency,
              status=source_status,
              error_message=source_error,
          )
      )

    return rows


def _read_source_rows(
    client: Any,
    *,
    table_id: str,
    job_id: str,
    location: Optional[str],
) -> tuple[dict[str, Any], ...]:
  query = _READ_SOURCE_TABLE_QUERY.format(table_id=table_id)
  job_config = bigquery.QueryJobConfig(
      query_parameters=[
          bigquery.ScalarQueryParameter("job_id", "STRING", job_id)
      ]
  )
  job_config = with_sdk_labels(job_config, feature="evalbench-import")
  query_args: dict[str, Any] = {"job_config": job_config}
  if location is not None:
    query_args["location"] = location
  result = client.query(query, **query_args).result()
  return tuple(_plain_row(row) for row in result)


def _plain_row(row: Any) -> dict[str, Any]:
  if isinstance(row, Mapping):
    items = row.items()
  elif hasattr(row, "items"):
    items = row.items()
  else:
    items = dict(row).items()
  return {str(key): _plain_value(value) for key, value in items}


def _plain_value(value: Any) -> Any:
  if isinstance(value, Mapping):
    return {str(key): _plain_value(item) for key, item in value.items()}
  if isinstance(value, (list, tuple)):
    return [_plain_value(item) for item in value]
  return value


def _validate_source_segment(name: str, value: Any) -> None:
  if not isinstance(value, str) or not _SOURCE_SEGMENT_PATTERN.fullmatch(value):
    raise ValueError(
        f"{name} must contain only ASCII letters, digits, '_' or '-'"
    )


def _scenario_id(result: Mapping[str, Any]) -> str:
  scenario = _as_mapping(_structured(result.get("scenario")))
  nested_result = _as_mapping(_structured(result.get("eval_results")))
  nested_scenario = _as_mapping(_structured(nested_result.get("scenario")))
  for value in (
      result.get("id"),
      result.get("eval_id"),
      scenario.get("id"),
      nested_result.get("eval_id"),
      nested_result.get("id"),
      nested_scenario.get("id"),
      result.get("prompt_id"),
  ):
    text = _usable_text(value)
    if text is not None:
      return text
  raise ValueError("EvalBench result is missing id/eval_id")


def _prompt(result: Mapping[str, Any]) -> Optional[str]:
  scenario = _as_mapping(_structured(result.get("scenario")))
  nested_result = _as_mapping(_structured(result.get("eval_results")))
  nested_scenario = _as_mapping(_structured(nested_result.get("scenario")))
  for value in (
      result.get("nl_prompt"),
      result.get("prompt"),
      scenario.get("starting_prompt"),
      nested_result.get("nl_prompt"),
      nested_result.get("prompt"),
      nested_scenario.get("starting_prompt"),
  ):
    text = _usable_text(value)
    if text is not None:
      return text
  return None


def _final_response(result: Mapping[str, Any]) -> Optional[str]:
  for key in ("final_response", "response", "generated_output", "output"):
    text = _usable_text(result.get(key))
    if text is not None:
      return text

  stdout = result.get("stdout")
  stdout_value = _structured(stdout)
  if isinstance(stdout_value, Mapping):
    for key in ("response", "final_response", "output"):
      text = _usable_text(stdout_value.get(key))
      if text is not None:
        return text
  else:
    text = _usable_text(stdout)
    if text is not None:
      return text

  nested_result = _as_mapping(_structured(result.get("eval_results")))
  for key in ("final_response", "response", "generated_output", "output"):
    text = _usable_text(nested_result.get(key))
    if text is not None:
      return text
  nested_stdout = nested_result.get("stdout")
  nested_stdout_value = _structured(nested_stdout)
  if isinstance(nested_stdout_value, Mapping):
    for key in ("response", "final_response", "output"):
      text = _usable_text(nested_stdout_value.get(key))
      if text is not None:
        return text
  else:
    text = _usable_text(nested_stdout)
    if text is not None:
      return text

  generated_sql = _usable_text(
      result.get("generated_sql"), rejected=_NO_GENERATED_OUTPUT
  )
  if generated_sql is not None:
    return generated_sql
  return _usable_text(
      nested_result.get("generated_sql"), rejected=_NO_GENERATED_OUTPUT
  )


def _tool_calls(result: Mapping[str, Any]) -> list[dict[str, Any]]:
  nested_result = _as_mapping(_structured(result.get("eval_results")))
  stdout_payload = _as_mapping(_structured(result.get("stdout")))
  nested_stdout_payload = _as_mapping(_structured(nested_result.get("stdout")))
  for value in (
      result.get("tool_calls"),
      stdout_payload.get("tool_calls"),
      nested_result.get("tool_calls"),
      nested_stdout_payload.get("tool_calls"),
      result.get("accumulated_tools"),
      nested_result.get("accumulated_tools"),
  ):
    calls = _normalize_tool_calls(value)
    if calls:
      return calls
  return []


def _normalize_tool_calls(value: Any) -> list[dict[str, Any]]:
  value = _structured(value)
  if not isinstance(value, (list, tuple)):
    return []

  calls: list[dict[str, Any]] = []
  for item in value:
    if isinstance(item, str):
      name = _usable_text(item)
      if name is not None:
        calls.append({"tool_name": name, "args": {}, "result": None})
      continue
    call = _as_mapping(_structured(item))
    name = None
    for key in ("tool_name", "name", "tool"):
      name = _usable_text(call.get(key))
      if name is not None:
        break
    if name is None:
      continue
    args = call.get("parameters", call.get("args", call.get("arguments", {})))
    result = call.get("response", call.get("result", call.get("output")))
    error = call.get("error")
    status = _usable_text(call.get("status"))
    if (
        error is None
        and status is not None
        and status.lower()
        not in {
            "completed",
            "ok",
            "success",
        }
    ):
      error = status
    calls.append(
        {
            "tool_name": name,
            "args": _json_safe(_structured(args)) or {},
            "result": _json_safe(_structured(result)),
            "error": _json_safe(_structured(error)),
            "timestamp": call.get("timestamp"),
            "result_timestamp": call.get("result_timestamp"),
            "duration_ms": call.get("duration_ms", call.get("latency_ms")),
        }
    )
  return calls


def _result_run_time(result: Mapping[str, Any]) -> Optional[datetime]:
  nested_result = _as_mapping(_structured(result.get("eval_results")))
  for value in (result.get("run_time"), nested_result.get("run_time")):
    parsed = _parse_timestamp(value)
    if parsed is not None:
      return parsed
  return None


def _first_run_time(
    config_rows: tuple[dict[str, Any], ...],
) -> Optional[datetime]:
  for row in config_rows:
    parsed = _parse_timestamp(row.get("run_time"))
    if parsed is not None:
      return parsed
  return None


def _parse_timestamp(value: Any) -> Optional[datetime]:
  if isinstance(value, datetime):
    return (
        value
        if value.tzinfo is not None
        else value.replace(tzinfo=timezone.utc)
    )
  if isinstance(value, date):
    return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
  if not isinstance(value, str):
    return None
  text = value.strip()
  if not text or text.lower() in _MISSING_TEXT:
    return None
  if text.endswith("Z"):
    text = text[:-1] + "+00:00"
  try:
    parsed = datetime.fromisoformat(text)
  except ValueError:
    return None
  return (
      parsed
      if parsed.tzinfo is not None
      else parsed.replace(tzinfo=timezone.utc)
  )


def _config_values(config_rows: tuple[dict[str, Any], ...]) -> dict[str, Any]:
  values: dict[str, Any] = {}
  for row in config_rows:
    key = _usable_text(row.get("config"))
    if key is not None:
      values[key] = row.get("value")
  return values


def _agent_name(config: Mapping[str, Any]) -> str:
  orchestrator = _usable_text(config.get("experiment_config.orchestrator"))
  generator = _usable_text(config.get("model_config.generator"))
  return f"evalbench:{orchestrator or 'unknown'}:{generator or 'unknown'}"


def _base_attributes(
    *,
    result: Mapping[str, Any],
    project_id: str,
    dataset_id: str,
    job_id: str,
    scenario_id: str,
    agent: str,
) -> dict[str, Any]:
  attributes: dict[str, Any] = {
      "experiment_id": job_id,
      "evalbench_scenario_id": scenario_id,
      "evalbench_source_project": project_id,
      "evalbench_source_dataset": dataset_id,
      "root_agent_name": agent,
  }
  for source_key, target_key in (
      ("database", "evalbench_database"),
      ("dialects", "evalbench_dialects"),
      ("query_type", "evalbench_query_type"),
  ):
    value = result.get(source_key)
    if _usable_text(value) is not None or isinstance(
        value, (list, tuple, dict)
    ):
      attributes[target_key] = _json_safe(_structured(value))
  return attributes


def _source_error_fields(result: Mapping[str, Any]) -> dict[str, Any]:
  fields: dict[str, Any] = {}
  for key in (
      "prompt_generator_error",
      "sql_generator_error",
      "generated_error",
      "golden_error",
      "error",
      "stderr",
  ):
    value = result.get(key)
    if _usable_text(value) is not None:
      fields[key] = _json_safe(_structured(value))
  returncode = result.get("returncode")
  if _failed_returncode(returncode):
    fields["returncode"] = _json_safe(_structured(returncode))
  return fields


def _source_error_message(
    result: Mapping[str, Any], error_fields: Mapping[str, Any]
) -> Optional[str]:
  parts: list[str] = []
  for key in (
      "prompt_generator_error",
      "sql_generator_error",
      "generated_error",
      "golden_error",
      "error",
  ):
    text = _usable_text(error_fields.get(key))
    if text is not None:
      parts.append(f"{key}: {text}")

  returncode = result.get("returncode")
  if _failed_returncode(returncode):
    parts.append(f"returncode: {returncode}")
    stderr = _usable_text(error_fields.get("stderr"))
    if stderr is not None:
      parts.append(f"stderr: {stderr}")
  return "; ".join(parts) if parts else None


def _failed_returncode(returncode: Any) -> bool:
  try:
    return returncode is not None and int(returncode) != 0
  except (TypeError, ValueError):
    return _usable_text(returncode) is not None


def _usage_and_latency(
    result: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
  payload = _as_mapping(_structured(result.get("stdout")))
  if not payload:
    nested_result = _as_mapping(_structured(result.get("eval_results")))
    payload = _as_mapping(_structured(nested_result.get("stdout")))

  stats = _as_mapping(payload.get("stats"))
  models = _as_mapping(stats.get("models"))
  token_totals = {
      "input": 0,
      "output": 0,
      "total": 0,
      "cached": 0,
  }
  found_tokens = {key: False for key in token_totals}
  total_latency_ms = 0
  found_latency = False

  for model_data_value in models.values():
    model_data = _as_mapping(_structured(model_data_value))
    tokens = _as_mapping(_structured(model_data.get("tokens")))
    for target, aliases in (
        ("input", ("input", "prompt", "input_tokens")),
        ("output", ("candidates", "output", "output_tokens")),
        ("total", ("total", "total_tokens")),
        ("cached", ("cached", "cached_tokens")),
    ):
      number = _first_int(tokens, aliases)
      if number is not None:
        token_totals[target] += number
        found_tokens[target] = True
    api = _as_mapping(_structured(model_data.get("api")))
    latency_value = _first_int(api, ("totalLatencyMs", "total_latency_ms"))
    if latency_value is not None:
      # Some EvalBench producers repeat one run-level duration for each model.
      total_latency_ms = max(total_latency_ms, latency_value)
      found_latency = True

  direct_values = {
      "input": _first_int(
          result, ("input_tokens", "prompt_tokens", "prompt_token_count")
      ),
      "output": _first_int(
          result,
          ("output_tokens", "completion_tokens", "candidates_token_count"),
      ),
      "total": _first_int(result, ("total_tokens", "total_token_count")),
      "cached": _first_int(
          result, ("cached_tokens", "cached_content_token_count")
      ),
  }
  for key, value in direct_values.items():
    if not found_tokens[key] and value is not None:
      token_totals[key] = value
      found_tokens[key] = True

  if not found_tokens["total"] and (
      found_tokens["input"] or found_tokens["output"]
  ):
    token_totals["total"] = token_totals["input"] + token_totals["output"]
    found_tokens["total"] = True

  usage: dict[str, Any] = {}
  metadata: dict[str, int] = {}
  if found_tokens["input"]:
    usage["input_tokens"] = token_totals["input"]
    metadata["prompt_token_count"] = token_totals["input"]
  if found_tokens["output"]:
    usage["output_tokens"] = token_totals["output"]
    metadata["candidates_token_count"] = token_totals["output"]
  if found_tokens["total"]:
    metadata["total_token_count"] = token_totals["total"]
  if found_tokens["cached"]:
    metadata["cached_content_token_count"] = token_totals["cached"]
  if metadata:
    usage["usage_metadata"] = metadata

  latency: dict[str, Any] = {}
  if found_latency:
    latency["total_ms"] = total_latency_ms
  return usage, latency


def _first_int(
    mapping: Mapping[str, Any], keys: tuple[str, ...]
) -> Optional[int]:
  for key in keys:
    value = mapping.get(key)
    if value is None or isinstance(value, bool):
      continue
    try:
      return int(value)
    except (TypeError, ValueError):
      continue
  return None


def _tool_latency(tool_call: Mapping[str, Any]) -> dict[str, Any]:
  duration = tool_call.get("duration_ms")
  if duration is not None:
    try:
      return {"total_ms": int(duration)}
    except (TypeError, ValueError):
      pass
  started = _parse_timestamp(tool_call.get("timestamp"))
  completed = _parse_timestamp(tool_call.get("result_timestamp"))
  if started is not None and completed is not None:
    return {
        "total_ms": max(0, int((completed - started).total_seconds() * 1000))
    }
  return {}


def _event_row(
    *,
    event_type: str,
    timestamp: datetime,
    agent: str,
    session_id: str,
    invocation_id: str,
    span_id: str,
    parent_span_id: Optional[str],
    content: Mapping[str, Any],
    attributes: Mapping[str, Any],
    latency_ms: Optional[Mapping[str, Any]] = None,
    status: str = "OK",
    error_message: Optional[str] = None,
) -> dict[str, Any]:
  return {
      "session_id": session_id,
      "event_type": event_type,
      "timestamp": timestamp.isoformat(),
      "agent": agent,
      "invocation_id": invocation_id,
      "trace_id": session_id,
      "span_id": span_id,
      "parent_span_id": parent_span_id,
      "user_id": None,
      "content": _json_safe(content),
      "content_parts": [],
      "attributes": _json_safe(attributes),
      "latency_ms": _json_safe(latency_ms or {}),
      "status": status,
      "error_message": error_message,
      "is_truncated": False,
  }


def _stable_id(*parts: str, length: int) -> str:
  digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
  return digest[:length]


def _structured(value: Any) -> Any:
  if not isinstance(value, str):
    return _plain_value(value)
  text = value.strip()
  if not text or text[0] not in "[{(" or text[-1] not in "]})":
    return value
  try:
    return _plain_value(json.loads(text))
  except (json.JSONDecodeError, TypeError):
    pass
  try:
    return _plain_value(ast.literal_eval(text))
  except (SyntaxError, ValueError, TypeError):
    return value


def _as_mapping(value: Any) -> dict[str, Any]:
  if isinstance(value, Mapping):
    return {str(key): item for key, item in value.items()}
  return {}


def _usable_text(
    value: Any, *, rejected: frozenset[str] = frozenset()
) -> Optional[str]:
  if value is None:
    return None
  if isinstance(value, str):
    if value.strip().lower() in _MISSING_TEXT | rejected:
      return None
    return value
  if isinstance(value, (Mapping, list, tuple)):
    return json.dumps(_json_safe(value), sort_keys=True, separators=(",", ":"))
  text = str(value)
  if text.strip().lower() in _MISSING_TEXT | rejected:
    return None
  return text


def _json_safe(value: Any) -> Any:
  if isinstance(value, Mapping):
    return {str(key): _json_safe(item) for key, item in value.items()}
  if isinstance(value, (list, tuple)):
    return [_json_safe(item) for item in value]
  if isinstance(value, datetime):
    return value.isoformat()
  if isinstance(value, date):
    return value.isoformat()
  if value is None or isinstance(value, (str, int, float, bool)):
    return value
  return str(value)


def _compact_json(value: Any) -> str:
  safe = _json_safe(value)
  if safe in ({}, [], None):
    return ""
  return json.dumps(safe, sort_keys=True, separators=(",", ":"))


def _one_line(value: Any) -> str:
  text = _usable_text(value)
  if text is None:
    return ""
  return " ".join(text.splitlines())
