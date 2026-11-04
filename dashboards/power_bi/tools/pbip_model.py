# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""TMDL and Power Query M semantic model emitter for the BQAA Power BI project."""

from collections.abc import Mapping
import datetime
import importlib.util
from pathlib import Path
import re
import sys
from types import ModuleType
from typing import Any

TOOLS_DIR = Path(__file__).resolve().parent


def _load_pbip_common() -> ModuleType:
  mod_name = "power_bi_pbip_common"
  if mod_name in sys.modules:
    return sys.modules[mod_name]
  module_path = TOOLS_DIR / "pbip_common.py"
  spec = importlib.util.spec_from_file_location(mod_name, module_path)
  assert spec is not None and spec.loader is not None
  module = importlib.util.module_from_spec(spec)
  sys.modules[mod_name] = module
  spec.loader.exec_module(module)
  return module


pbip_common = _load_pbip_common()

EVENT_COLUMN_NAMES: tuple[str, ...] = (
    "timestamp",
    "event_type",
    "agent",
    "session_id",
    "invocation_id",
    "user_id",
    "trace_id",
    "span_id",
    "parent_span_id",
    "status",
    "error_message",
    "is_truncated",
    "usage_prompt_tokens",
    "usage_completion_tokens",
    "usage_total_tokens",
    "llm_total_ms",
    "ttft_ms",
    "model_version",
    "tool_completed_name",
    "tool_completed_origin",
    "tool_completed_total_ms",
    "tool_error_name",
    "tool_error_origin",
    "tool_error_total_ms",
    "event_date",
    "event_pk",
    "llm_response_pk",
    "tool_completed_pk",
    "tool_error_pk",
)

MEASURE_NAMES: tuple[str, ...] = (
    # Base (20)
    "Total Tokens",
    "Total Traces",
    "Total Sessions",
    "Total Users",
    "Total Events",
    "LLM Calls",
    "Tool Completion Events",
    "Distinct Tool Invocations",
    "Tool Errors",
    "Avg Tool Latency (ms)",
    "Avg LLM Latency (ms)",
    "P50 Tool Latency (ms)",
    "P75 Tool Latency (ms)",
    "P90 Tool Latency (ms)",
    "P99 Tool Latency (ms)",
    "P50 LLM Latency (ms)",
    "P75 LLM Latency (ms)",
    "P90 LLM Latency (ms)",
    "P99 LLM Latency (ms)",
    "Traces for Agents With Tokens",
    # Scorecards (18)
    "KPI Total Tokens",
    "KPI Total Tokens Prior",
    "KPI Total Tokens Change %",
    "KPI Total Traces",
    "KPI Total Traces Prior",
    "KPI Total Traces Change %",
    "KPI Total Sessions",
    "KPI Total Sessions Prior",
    "KPI Total Sessions Change %",
    "KPI Total LLM Calls",
    "KPI Total LLM Calls Prior",
    "KPI Total LLM Calls Change %",
    "KPI Total Users",
    "KPI Total Users Prior",
    "KPI Total Users Change %",
    "KPI Tool Errors",
    "KPI Tool Errors Prior",
    "KPI Tool Errors Change %",
    # Charts (12)
    "Token Usage by Agent",
    "Top 5 Users by Token Consumption",
    "Top 5 Users by Traces",
    "Traces by Agent",
    "Top 5 Agents by Sessions",
    "Tool Invocations",
    "Tool Completions by Agent",
    "Top 5 Users by Sessions",
    "Top 5 Users by Events",
    "Top 5 Agents by Tool Errors",
    "Top 5 Tools by Errors",
    "Top 5 Agents by LLM Calls",
)

PARAMETER_NAMES: tuple[str, ...] = (
    "BQAA_ProjectId",
    "BQAA_DatasetId",
    "BQAA_TableId",
    "BQAA_BillingProjectId",
    "BQAA_WindowEndDate",
    "BQAA_LookbackDays",
    "BQAA_UseStorageApi",
)

VALID_TMDL_DATA_TYPES: frozenset[str] = frozenset(
    {
        "string",
        "int64",
        "double",
        "decimal",
        "dateTime",
        "boolean",
    }
)


def escape_m_string(text: str) -> str:
  """Escape a Python string for embedding inside a Power Query M string literal."""
  return text.replace("#(", "#(#)(").replace('"', '""')


def unescape_m_string(m_literal_body: str) -> str:
  """Reverse escape_m_string on the body of a Power Query M string literal."""
  return m_literal_body.replace('""', '"').replace("#(#)(", "#(")


def events_select_lines(contracts: Any = None) -> list[str]:
  """Extract the verbatim SELECT ... (before FROM) lines of events_v1.sql.tmpl."""
  if contracts is None:
    contracts = pbip_common.load_contracts()
  raw_lines = contracts.events_template.replace("\r\n", "\n").splitlines()
  select_idx: int | None = None
  from_idx: int | None = None
  for idx, line in enumerate(raw_lines):
    if select_idx is None and line.strip() == "SELECT":
      select_idx = idx
    elif select_idx is not None and line.startswith("FROM "):
      from_idx = idx
      break
  if select_idx is None or from_idx is None or from_idx <= select_idx:
    raise ValueError(
        "Could not locate SELECT ... FROM block in events_v1.sql.tmpl"
    )
  selected = raw_lines[select_idx:from_idx]
  for line in selected:
    if ";" in line or '"' in line or "#" in line:
      raise ValueError(
          f'Forbidden character (;, ", or #) in events_v1 SELECT line: {line!r}'
      )
  return selected


def render_events_sql(
    project: str,
    dataset: str,
    table: str,
    load_start: datetime.date,
    anchor: datetime.date,
    contracts: Any = None,
) -> str:
  """Render the exact BigQuery SQL executed by the events_v1 M partition (I7, I8)."""
  if not isinstance(project, str) or not pbip_common.PROJECT_RE.fullmatch(
      project
  ):
    raise ValueError(f"Invalid BigQuery project ID: {project!r}")
  if not isinstance(dataset, str) or not pbip_common.DATASET_RE.fullmatch(
      dataset
  ):
    raise ValueError(f"Invalid BigQuery dataset ID: {dataset!r}")
  if not isinstance(table, str) or not pbip_common.TABLE_RE.fullmatch(table):
    raise ValueError(f"Invalid BigQuery table ID: {table!r}")
  if (
      not isinstance(load_start, datetime.date)
      or isinstance(load_start, datetime.datetime)
      or not isinstance(anchor, datetime.date)
      or isinstance(anchor, datetime.datetime)
  ):
    raise ValueError("load_start and anchor must be datetime.date instances")

  span_days = (anchor - load_start).days + 1
  if span_days < 1 or span_days > 732:
    raise ValueError(
        f"Invalid date window [{load_start}, {anchor}] ({span_days} days; must be 1..732)"
    )

  select_lines = events_select_lines(contracts)
  where_lines = [
      f"FROM `{project}.{dataset}.{table}`",
      f"WHERE timestamp >= TIMESTAMP(DATE '{load_start.isoformat()}', 'UTC')",
      (
          "  AND timestamp < TIMESTAMP(DATE_ADD("
          f"DATE '{anchor.isoformat()}', INTERVAL 1 DAY), 'UTC')"
      ),
  ]
  sql = "\n".join(select_lines + where_lines)
  if ";" in sql or '"' in sql or "#" in sql:
    raise ValueError(
        'Rendered events_v1 SQL contains forbidden character (;, ", or #)'
    )
  return sql


def _quote_tmdl_name(name: str) -> str:
  if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
    return name
  escaped = name.replace("'", "''")
  return f"'{escaped}'"


def _validate_model_spec(contracts: Any, model_spec: Mapping[str, Any]) -> None:
  params = model_spec.get("parameters")
  if not isinstance(params, list) or len(params) != 7:
    raise ValueError("semantic_model.yaml must declare 7 M parameters")
  param_names = tuple(p.get("name") for p in params)
  if param_names != PARAMETER_NAMES:
    raise ValueError(
        f"Parameter names/order mismatch: expected {PARAMETER_NAMES}, got {param_names}"
    )

  for p in params:
    pname = p["name"]
    default_val = p["default"]
    if pname in ("BQAA_ProjectId", "BQAA_BillingProjectId"):
      if not isinstance(
          default_val, str
      ) or not pbip_common.PROJECT_RE.fullmatch(default_val):
        raise ValueError(
            f"Default for {pname} fails PROJECT_RE: {default_val!r}"
        )
    elif pname == "BQAA_DatasetId":
      if not isinstance(
          default_val, str
      ) or not pbip_common.DATASET_RE.fullmatch(default_val):
        raise ValueError(
            f"Default for {pname} fails DATASET_RE: {default_val!r}"
        )
    elif pname == "BQAA_TableId":
      if not isinstance(default_val, str) or not pbip_common.TABLE_RE.fullmatch(
          default_val
      ):
        raise ValueError(f"Default for {pname} fails TABLE_RE: {default_val!r}")
    elif pname == "BQAA_WindowEndDate":
      if default_val != "auto":
        if not isinstance(default_val, str) or not re.fullmatch(
            r"[0-9]{4}-[0-9]{2}-[0-9]{2}", default_val
        ):
          raise ValueError(
              f"Default for {pname} must be 'auto' or YYYY-MM-DD: {default_val!r}"
          )
        try:
          datetime.date.fromisoformat(default_val)
        except ValueError:
          raise ValueError(
              f"Default for {pname} is not a real calendar date: {default_val!r}"
          ) from None
    elif pname == "BQAA_LookbackDays":
      if (
          not isinstance(default_val, int)
          or isinstance(default_val, bool)
          or not (1 <= default_val <= 366)
      ):
        raise ValueError(
            f"Default for {pname} must be integer 1..366: {default_val!r}"
        )
    elif pname == "BQAA_UseStorageApi":
      if not isinstance(default_val, bool):
        raise ValueError(
            f"Default for {pname} must be boolean: {default_val!r}"
        )

  tables = model_spec.get("tables", {})
  events_tbl = tables.get("events_v1", {})
  cols = events_tbl.get("columns", [])
  col_names = tuple(c.get("name") for c in cols)
  if col_names != EVENT_COLUMN_NAMES:
    raise ValueError(
        f"events_v1 columns mismatch: expected 29 columns {EVENT_COLUMN_NAMES}, got {col_names}"
    )
  for c in cols:
    dt = c.get("data_type")
    if dt not in VALID_TMDL_DATA_TYPES:
      raise ValueError(
          f"Invalid TMDL dataType {dt!r} on column {c.get('name')!r}"
      )

  measures = model_spec.get("measures")
  if not isinstance(measures, list) or len(measures) != 50:
    raise ValueError(
        f"Expected 50 measures in semantic_model.yaml, got {len(measures) if isinstance(measures, list) else 0}"
    )
  m_names = tuple(m.get("name") for m in measures)
  if m_names != MEASURE_NAMES:
    raise ValueError(
        f"Measure names/order mismatch: expected {MEASURE_NAMES}, got {m_names}"
    )

  for m in measures:
    mname = m["name"]
    dax = m.get("dax", "")
    if not isinstance(dax, str) or not dax.strip() or "\n" in dax:
      raise ValueError(
          f"Measure {mname!r} must have a non-empty single-line DAX expression"
      )
    if "DISTINCTCOUNT(" in dax:
      raise ValueError(
          f"Measure {mname!r} uses DISTINCTCOUNT(; must use DISTINCTCOUNTNOBLANK("
      )
    folder = m.get("display_folder")
    if folder not in ("Base", "Scorecards", "Charts"):
      raise ValueError(
          f"Measure {mname!r} has invalid display_folder {folder!r}"
      )
    oqueries = m.get("oracle_queries")
    if not isinstance(oqueries, list) or not oqueries:
      raise ValueError(f"Measure {mname!r} must cite at least one oracle query")
    for oq in oqueries:
      if oq not in contracts.oracle_files:
        raise ValueError(f"Measure {mname!r} cites unknown oracle query {oq!r}")
    ocol = m.get("oracle_column")
    if not isinstance(ocol, str) or not ocol:
      raise ValueError(f"Measure {mname!r} must name its oracle_column")
    if not any(
        pbip_common.has_output_alias(contracts.oracle_files[oq], ocol)
        for oq in oqueries
    ):
      raise ValueError(
          f"Measure {mname!r} oracle_column {ocol!r} is not an output alias "
          "of any cited oracle query"
      )


def _emit_database_tmdl(model_spec: Mapping[str, Any]) -> str:
  model_cfg = model_spec.get("model", {})
  db_name = model_cfg.get("database_name", "bqaa-agent-analytics")
  compat = int(model_cfg.get("compatibility_level", 1567))
  lines = [
      f"database {_quote_tmdl_name(db_name)}",
      f"\tcompatibilityLevel: {compat}",
  ]
  return "\n".join(lines) + "\n"


def _emit_model_tmdl(model_spec: Mapping[str, Any]) -> str:
  model_cfg = model_spec.get("model", {})
  model_name = model_cfg.get("name", "Model")
  culture = model_cfg.get("culture", "en-US")
  ds_version = model_cfg.get(
      "default_power_bi_data_source_version", "powerBI_V3"
  )
  lines = [
      f"model {_quote_tmdl_name(model_name)}",
      f"\tculture: {culture}",
      f"\tdefaultPowerBIDataSourceVersion: {ds_version}",
      "",
      "ref table events_v1",
      "ref table Date",
  ]
  return "\n".join(lines) + "\n"


def _format_m_parameter_default(param: Mapping[str, Any]) -> str:
  m_type = param["m_type"]
  default = param["default"]
  if m_type == "Text":
    return f'"{escape_m_string(str(default))}"'
  if m_type == "Number":
    return str(int(default))
  if m_type == "Logical":
    return "true" if bool(default) else "false"
  raise ValueError(f"Unsupported M parameter type: {m_type!r}")


def _emit_expressions_tmdl(
    contracts: Any, model_spec: Mapping[str, Any]
) -> str:
  lines: list[str] = []
  for param in model_spec["parameters"]:
    pname = param["name"]
    m_type = param["m_type"]
    default_lit = _format_m_parameter_default(param)
    desc = param.get("description", "")
    # Do not place raw parameter names in /// comments (I3).
    for raw_p in PARAMETER_NAMES:
      if raw_p in desc:
        raise ValueError(
            f"Parameter description must not contain raw parameter name {raw_p}"
        )
    if desc:
      lines.append(f"/// {desc}")
    lines.append(
        f"expression {pname} = {default_lit} meta "
        f'[IsParameterQuery=true, Type="{m_type}", IsParameterQueryRequired=true]'
    )
    lines.append(
        f"\tlineageTag: {pbip_common.stable_uuid('expression', pname)}"
    )
    lines.append("")

  select_lines = events_select_lines(contracts)
  config_tag = pbip_common.stable_uuid("expression", "BQAA_Config")

  lines.append(
      "/// Shared configuration, parameter validation, partition anchor, and SQL builder."
  )
  lines.append("expression BQAA_Config =")
  lines.append("\t\tlet")
  lines.append('\t\t\tLower = Text.ToList("abcdefghijklmnopqrstuvwxyz"),')
  lines.append('\t\t\tUpper = Text.ToList("ABCDEFGHIJKLMNOPQRSTUVWXYZ"),')
  lines.append('\t\t\tDigits = Text.ToList("0123456789"),')
  lines.append(
      '\t\t\tReject = (name as text) => error Error.Record("BQAA.InvalidParameter", '
      'name & " failed validation (see dashboards/power_bi/README.md#parameters)"),'
  )
  lines.append(
      "\t\t\tOnly = (s as text, allowed as list) as logical => Text.Select(s, allowed) = s,"
  )
  lines.append(
      "\t\t\tIsProject = (s as any) as logical => s is text and Text.Length(s) >= 6 "
      'and Text.Length(s) <= 30 and Only(s, Lower & Digits & {"-"}) '
      'and List.Contains(Lower, Text.Start(s, 1)) and Text.End(s, 1) <> "-",'
  )
  lines.append(
      "\t\t\tIsDataset = (s as any) as logical => s is text and Text.Length(s) >= 1 "
      'and Text.Length(s) <= 1024 and Only(s, Lower & Upper & Digits & {"_"}) '
      "and not List.Contains(Digits, Text.Start(s, 1)),"
  )
  lines.append(
      "\t\t\tIsTable = (s as any) as logical => s is text and Text.Length(s) >= 1 "
      'and Text.Length(s) <= 1024 and Only(s, Lower & Upper & Digits & {"_", "-"}) '
      'and Text.Start(s, 1) <> "-",'
  )
  lines.append(
      "\t\t\tIsLookback = (n as any) as logical => n is number "
      "and Number.RoundDown(n) = n and n >= 1 and n <= 366,"
  )
  lines.append("\t\t\tIsLogical = (b as any) as logical => b is logical,")
  lines.append(
      "\t\t\tParseIsoDate = (raw as text) as nullable date => "
      "let s = Text.Trim(raw), ok = Text.Length(s) = 10 "
      'and Text.At(s, 4) = "-" and Text.At(s, 7) = "-" '
      "and Only(Text.Range(s, 0, 4) & Text.Range(s, 5, 2) & Text.Range(s, 8, 2), Digits), "
      "d = if ok then try #date(Number.FromText(Text.Range(s, 0, 4)), "
      "Number.FromText(Text.Range(s, 5, 2)), Number.FromText(Text.Range(s, 8, 2))) "
      "otherwise null else null in d,"
  )
  lines.append(
      "\t\t\tIsWindowEnd = (s as any) as logical => s is text "
      'and (Text.Lower(Text.Trim(s)) = "auto" or ParseIsoDate(s) <> null),'
  )
  # Validator lines (I3: raw parameter names appear ONLY on their declaration line and here).
  lines.append(
      '\t\t\tProject = if IsProject(BQAA_ProjectId) then BQAA_ProjectId else Reject("BQAA_ProjectId"),'
  )
  lines.append(
      '\t\t\tBilling = if IsProject(BQAA_BillingProjectId) then BQAA_BillingProjectId else Reject("BQAA_BillingProjectId"),'
  )
  lines.append(
      '\t\t\tDataset = if IsDataset(BQAA_DatasetId) then BQAA_DatasetId else Reject("BQAA_DatasetId"),'
  )
  lines.append(
      '\t\t\tTable = if IsTable(BQAA_TableId) then BQAA_TableId else Reject("BQAA_TableId"),'
  )
  lines.append(
      '\t\t\tLookback = if IsLookback(BQAA_LookbackDays) then BQAA_LookbackDays else Reject("BQAA_LookbackDays"),'
  )
  lines.append(
      '\t\t\tUseStorageApi = if IsLogical(BQAA_UseStorageApi) then (BQAA_UseStorageApi = true) else Reject("BQAA_UseStorageApi"),'
  )
  lines.append(
      '\t\t\tWindowEndRaw = if IsWindowEnd(BQAA_WindowEndDate) then Text.Trim(BQAA_WindowEndDate) else Reject("BQAA_WindowEndDate"),'
  )
  lines.append(
      "\t\t\tNav = GoogleBigQuery.Database([BillingProject = Billing, UseStorageApi = UseStorageApi]){[Name = Billing]}[Data],"
  )
  lines.append(
      '\t\t\tTableRef = "`" & Project & "." & Dataset & "." & Table & "`",'
  )
  lines.append(
      "\t\t\tUtcToday = Date.From(DateTimeZone.SwitchZone(DateTimeZone.UtcNow(), 0)),"
  )
  lines.append(
      "\t\t\tAnchorSql = \"SELECT MAX(IF(partition_id NOT IN ('__NULL__', '__UNPARTITIONED__') AND total_rows > 0, partition_id, NULL)) AS newest_partition, "
      "COUNTIF(partition_id = '__UNPARTITIONED__' AND total_rows > 0) AS buffered_partitions "
      'FROM `" & Project & "." & Dataset & ".INFORMATION_SCHEMA.PARTITIONS` WHERE table_name = \'" & Table & "\'",'
  )
  lines.append(
      "\t\t\tParsePartitionDate = (pid as nullable text) as nullable date => "
      "if pid <> null and Text.Length(pid) >= 8 and Only(Text.Start(pid, 8), Digits) "
      "then try #date(Number.FromText(Text.Range(pid, 0, 4)), "
      "Number.FromText(Text.Range(pid, 4, 2)), Number.FromText(Text.Range(pid, 6, 2))) "
      "otherwise null else null,"
  )
  lines.append(
      "\t\t\tFallbackAnchor = let fb = try Table.First(Value.NativeQuery(Nav, "
      "\"SELECT DATE(MAX(timestamp), 'UTC') AS newest_date FROM \" & TableRef)) "
      'otherwise null, raw = if fb <> null then Record.FieldOrDefault(fb, "newest_date", null) '
      "else null, d = if raw is date then raw else if raw is datetime then Date.From(raw) "
      "else null in if d <> null then d else UtcToday,"
  )
  lines.append(
      "\t\t\tAutoAnchor = let metaRow = try Table.First(Value.NativeQuery(Nav, AnchorSql)) "
      "otherwise null, partDate = if metaRow <> null then ParsePartitionDate("
      'Record.FieldOrDefault(metaRow, "newest_partition", null)) else null, '
      "buffered = if metaRow <> null then Record.FieldOrDefault(metaRow, "
      '"buffered_partitions", 0) else 0, resolved = if partDate <> null then '
      "(if buffered <> null and buffered > 0 and UtcToday > partDate then UtcToday else partDate) "
      "else FallbackAnchor in resolved,"
  )
  lines.append(
      '\t\t\tAnchor = if Text.Lower(WindowEndRaw) = "auto" then AutoAnchor else ParseIsoDate(WindowEndRaw),'
  )
  lines.append("\t\t\tCurrentStart = Date.AddDays(Anchor, -(Lookback - 1)),")
  lines.append("\t\t\tLoadStart = Date.AddDays(CurrentStart, -Lookback),")
  lines.append(
      '\t\t\tDateLit = (d as date) as text => "DATE \'" & Date.ToText(d, [Format = "yyyy-MM-dd", Culture = "en-US"]) & "\'",'
  )
  lines.append("\t\t\tEventsSelectLines = {")
  for idx, sline in enumerate(select_lines):
    comma = "," if idx < len(select_lines) - 1 else ""
    lines.append(f'\t\t\t\t"{escape_m_string(sline)}"{comma}')
  lines.append("\t\t\t},")
  lines.append(
      '\t\t\tEventsSql = Text.Combine(EventsSelectLines & {"FROM " & TableRef, '
      '"WHERE timestamp >= TIMESTAMP(" & DateLit(LoadStart) & ", \'UTC\')", '
      '"  AND timestamp < TIMESTAMP(DATE_ADD(" & DateLit(Anchor) & ", INTERVAL 1 DAY), \'UTC\')"}, "#(lf)")'
  )
  lines.append("\t\tin")
  lines.append(
      "\t\t\t[Nav = Nav, TableRef = TableRef, Anchor = Anchor, "
      "CurrentStart = CurrentStart, LoadStart = LoadStart, "
      "Lookback = Lookback, EventsSql = EventsSql]"
  )
  lines.append(f"\tlineageTag: {config_tag}")

  return "\n".join(lines) + "\n"


def _emit_relationships_tmdl(model_spec: Mapping[str, Any]) -> str:
  lines: list[str] = []
  rels = model_spec.get("relationships", [])
  for idx, rel in enumerate(rels):
    from_tbl = rel["from_table"]
    from_col = rel["from_column"]
    to_tbl = rel["to_table"]
    to_col = rel["to_column"]
    rel_id = pbip_common.stable_uuid(
        "relationship", from_tbl, from_col, to_tbl, to_col
    )
    lines.append(f"relationship {rel_id}")
    lines.append(f"\tfromColumn: {from_tbl}.{from_col}")
    lines.append(f"\ttoColumn: {to_tbl}.{to_col}")
    if idx < len(rels) - 1:
      lines.append("")
  return "\n".join(lines) + "\n"


def _emit_events_v1_tmdl(contracts: Any, model_spec: Mapping[str, Any]) -> str:
  events_tbl = model_spec["tables"]["events_v1"]
  columns = events_tbl["columns"]
  measures = model_spec["measures"]

  lines: list[str] = [
      "/// Reporting projection over the BigQuery Agent Analytics event table.",
      "table events_v1",
      f"\tlineageTag: {pbip_common.stable_uuid('table', 'events_v1')}",
      "",
  ]

  for m in measures:
    mname = m["name"]
    dax = m["dax"]
    fmt = m["format_string"]
    folder = m["display_folder"]
    oqueries = m["oracle_queries"]
    ocol = m.get("oracle_column", "")
    oracle_str = " and ".join(oqueries)
    oracle_sha = pbip_common.oracle_digest(contracts.oracle_files, oqueries)
    # The /// description includes 'measure <Name>' and space-delimited oracle/queries/<id>.sql
    # so both TMDL object documentation and substring/regex lints succeed.
    lines.append(f"\t/// measure {mname} — {oracle_str} ({ocol})")
    lines.append(f"\tmeasure {_quote_tmdl_name(mname)} = {dax}")
    lines.append(f"\t\tformatString: {fmt}")
    lines.append(f"\t\tdisplayFolder: {folder}")
    lines.append(
        f"\t\tlineageTag: {pbip_common.stable_uuid('measure', 'events_v1', mname)}"
    )
    # Digest of the cited oracle SQL: changing a cited query without
    # regenerating the model makes the generated file differ (drift check).
    lines.append(f"\t\tannotation bqaa_oracle_sha256 = {oracle_sha}")
    lines.append("")

  for col in columns:
    cname = col["name"]
    dtype = col["data_type"]
    fmt = col.get("format_string")
    is_hidden = bool(col.get("is_hidden", False))
    lines.append(
        f"\t/// Column {cname} — sourceColumn={cname} dataType={dtype}"
    )
    lines.append(f"\tcolumn {_quote_tmdl_name(cname)}")
    lines.append(f"\t\tdataType: {dtype}")
    if is_hidden:
      lines.append("\t\tisHidden")
    if fmt:
      lines.append(f"\t\tformatString: {fmt}")
    lines.append(
        f"\t\tlineageTag: {pbip_common.stable_uuid('column', 'events_v1', cname)}"
    )
    lines.append("\t\tsummarizeBy: none")
    lines.append(f"\t\tsourceColumn: {cname}")
    lines.append("")

  lines.append("\tpartition events_v1 = m")
  lines.append("\t\tmode: import")
  lines.append("\t\tsource =")
  lines.append("\t\t\t\tlet")
  lines.append("\t\t\t\t\tConfig = BQAA_Config,")
  lines.append(
      "\t\t\t\t\tRaw = Value.NativeQuery(Config[Nav], Config[EventsSql]),"
  )
  lines.append(
      '\t\t\t\t\tUtc = Table.TransformColumns(Raw, {{"timestamp", '
      "each if _ is datetimezone then DateTimeZone.RemoveZone(DateTimeZone.ToUtc(_)) "
      "else _, type datetime}}),"
  )
  lines.append("\t\t\t\t\tTyped = Table.TransformColumnTypes(Utc, {")
  for idx, col in enumerate(columns):
    cname = col["name"]
    m_type = col["m_type"]
    comma = "," if idx < len(columns) - 1 else ""
    lines.append(f'\t\t\t\t\t\t{{"{cname}", {m_type}}}{comma}')
  lines.append("\t\t\t\t\t})")
  lines.append("\t\t\t\tin")
  lines.append("\t\t\t\t\tTyped")

  return "\n".join(lines) + "\n"


def _emit_date_tmdl(model_spec: Mapping[str, Any]) -> str:
  date_tbl = model_spec["tables"]["Date"]
  columns = date_tbl["columns"]
  lines: list[str] = [
      "/// Contiguous UTC calendar covering the 2L loaded window.",
      "table Date",
      f"\tlineageTag: {pbip_common.stable_uuid('table', 'Date')}",
      f"\tdataCategory: {date_tbl.get('data_category', 'Time')}",
      "",
  ]

  for col in columns:
    cname = col["name"]
    dtype = col["data_type"]
    fmt = col.get("format_string")
    is_key = bool(col.get("is_key", False))
    lines.append(
        f"\t/// Column {cname} — sourceColumn={cname} dataType={dtype}"
    )
    lines.append(f"\tcolumn {_quote_tmdl_name(cname)}")
    lines.append(f"\t\tdataType: {dtype}")
    if is_key:
      lines.append("\t\tisKey")
    if fmt:
      lines.append(f"\t\tformatString: {fmt}")
    lines.append(
        f"\t\tlineageTag: {pbip_common.stable_uuid('column', 'Date', cname)}"
    )
    lines.append("\t\tsummarizeBy: none")
    lines.append(f"\t\tsourceColumn: {cname}")
    lines.append("")

  lines.append("\tpartition Date = m")
  lines.append("\t\tmode: import")
  lines.append("\t\tsource =")
  lines.append("\t\t\t\tlet")
  lines.append("\t\t\t\t\tConfig = BQAA_Config,")
  lines.append(
      "\t\t\t\t\tDays = Duration.Days(Config[Anchor] - Config[LoadStart]) + 1,"
  )
  lines.append(
      "\t\t\t\t\tDates = List.Dates(Config[LoadStart], Days, #duration(1, 0, 0, 0)),"
  )
  lines.append(
      "\t\t\t\t\tT = Table.FromList(Dates, Splitter.SplitByNothing(), type table [Date = date]),"
  )
  lines.append(
      '\t\t\t\t\tW = Table.AddColumn(T, "Window", '
      'each if [Date] >= Config[CurrentStart] then "Current" else "Prior", type text)'
  )
  lines.append("\t\t\t\tin")
  lines.append("\t\t\t\t\tW")

  return "\n".join(lines) + "\n"


def _assert_tmdl_hygiene(files: Mapping[str, str]) -> None:
  uuid_re = re.compile(
      r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
      re.IGNORECASE,
  )
  seen_uuids: set[str] = set()
  for rel_path, text in files.items():
    for lineno, line in enumerate(text.splitlines(), 1):
      if (
          line.startswith(" ")
          or "\t " in line[: len(line) - len(line.lstrip())]
      ):
        raise ValueError(
            f"{rel_path}:{lineno}: TMDL must use tab indentation only"
        )
    for m in uuid_re.finditer(text):
      uid = m.group(0).lower()
      if uid in seen_uuids:
        raise ValueError(f"{rel_path}: duplicate UUID {uid}")
      seen_uuids.add(uid)


def build_model(
    contracts: Any, model_spec: Mapping[str, Any]
) -> dict[str, str]:
  """Build all TMDL files for BQAA_Agent_Analytics.SemanticModel/definition/."""
  pbip_common.validate_contracts(contracts)
  _validate_model_spec(contracts, model_spec)

  files: dict[str, str] = {
      "definition/database.tmdl": _emit_database_tmdl(model_spec),
      "definition/model.tmdl": _emit_model_tmdl(model_spec),
      "definition/expressions.tmdl": _emit_expressions_tmdl(
          contracts, model_spec
      ),
      "definition/relationships.tmdl": _emit_relationships_tmdl(model_spec),
      "definition/tables/events_v1.tmdl": _emit_events_v1_tmdl(
          contracts, model_spec
      ),
      "definition/tables/Date.tmdl": _emit_date_tmdl(model_spec),
  }
  _assert_tmdl_hygiene(files)
  return files
