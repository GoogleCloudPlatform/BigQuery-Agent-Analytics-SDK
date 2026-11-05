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

"""Offline validator for the generated Power BI project.

Validates the PBIP output against pinned JSON schemas, runs a TMDL lint,
and checks chart/visual/measure/oracle parity.

Usage:
  python validate_pbip.py --schemas-dir DIR
  python validate_pbip.py --fetch-schemas DIR
  python validate_pbip.py --no-schemas

Schemas are fetched from the commit recorded in spec/pbir_schemas.lock.json
and verified against the committed sha256 hashes.
"""

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import urllib.request

# ---------------------------------------------------------------------------
# Paths and constants
# ---------------------------------------------------------------------------

POWER_BI_ROOT = Path(__file__).resolve().parents[1]
SPEC_DIR = POWER_BI_ROOT / "spec"
LOCK_FILE = SPEC_DIR / "pbir_schemas.lock.json"
REPO_ROOT = POWER_BI_ROOT.parents[1]
LOOKER_ROOT = REPO_ROOT / "dashboards" / "looker_studio"
MODEL_DIR = POWER_BI_ROOT / "BQAA_Agent_Analytics.SemanticModel"
REPORT_DIR = POWER_BI_ROOT / "BQAA_Agent_Analytics.Report"

# Schema URL prefix used inside generated JSON files.
SCHEMA_URL_PREFIX = "https://developer.microsoft.com/json-schemas/"

# The 29 event columns and their TMDL data types (spec.md §4.3, F1).  This is
# deliberately an independent copy of the generator spec: the lint compares the
# generated `column` declarations against it, so a corrupted declaration is
# reported instead of being echoed back.
EVENTS_COLUMN_TYPES = {
    "timestamp": "dateTime",
    "event_type": "string",
    "agent": "string",
    "session_id": "string",
    "invocation_id": "string",
    "user_id": "string",
    "trace_id": "string",
    "span_id": "string",
    "parent_span_id": "string",
    "status": "string",
    "error_message": "string",
    "is_truncated": "boolean",
    "usage_prompt_tokens": "int64",
    "usage_completion_tokens": "int64",
    "usage_total_tokens": "int64",
    "llm_total_ms": "int64",
    "ttft_ms": "int64",
    "model_version": "string",
    "tool_completed_name": "string",
    "tool_completed_origin": "string",
    "tool_completed_total_ms": "int64",
    "tool_error_name": "string",
    "tool_error_origin": "string",
    "tool_error_total_ms": "int64",
    "event_date": "dateTime",
    "event_pk": "string",
    "llm_response_pk": "string",
    "tool_completed_pk": "string",
    "tool_error_pk": "string",
}
EVENTS_COLUMNS = list(EVENTS_COLUMN_TYPES)

# Valid TMDL data types.
VALID_DATA_TYPES = {
    "string",
    "int64",
    "double",
    "decimal",
    "dateTime",
    "boolean",
}

# The 8 pages and their Looker page ids (spec.md §5.1).
PAGES = [
    ("token-consumption", "p_70hoa89q5d"),
    ("agent-and-sessions", "JjD"),
    ("tool-usage", "KjD"),
    ("llm-interactions", "MjD"),
    ("user-analytics", "00IH"),
    ("latency", "p_9om48zps5d"),
    ("errors", "v0IH"),
    ("trace-inspector", "BKJH"),
]

# The 50 measure names (spec.md §4.5, plan.md §2).
MEASURE_NAMES = [
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
]

# The 16 measures the live check compares with a float tolerance: averages,
# percentiles and period-over-period ratios.  Every other measure is an exact
# count or token sum, so it must declare `comparison_type: int` and format as
# `#,0`.  The list is pinned here, independently of the generator, so a
# display-format edit cannot quietly loosen the live comparison.
FLOAT_COMPARISON_MEASURES = frozenset(
    {
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
        "KPI Total Tokens Change %",
        "KPI Total Traces Change %",
        "KPI Total Sessions Change %",
        "KPI Total LLM Calls Change %",
        "KPI Total Users Change %",
        "KPI Tool Errors Change %",
    }
)
INT_MEASURE_FORMAT = "#,0"
FLOAT_MEASURE_FORMATS = frozenset({"#,0.0", "#,0.00", "+0.0%;-0.0%;0.0%"})

# Top-N visuals that intentionally draw fewer bars than the source query
# returns (README "Known differences", D14): chart id -> (top_n, oracle LIMIT).
# Every other Top-N visual draws exactly the source LIMIT.
TOP_N_BELOW_SOURCE_LIMIT = {
    "usage-top-5-agents-by-llm-calls": (5, 500),
}

# F4 listener exceptions: the 4 charts that do NOT listen to User ID.
NON_UID_LISTENERS = frozenset(
    {
        "usage-top-5-users-with-most-tokens-consumption",
        "usage-token-usage-split-by-agent",
        "usage-top-5-users-with-most-traces",
        "usage-traces-split-by-agent",
    }
)

# The six KPI scorecards (chart id -> current-value measure).  Each also binds
# "<measure> Change %", the comparison against the previous period.
KPI_SCORECARDS = {
    "usage-total-tokens": "KPI Total Tokens",
    "usage-total-traces": "KPI Total Traces",
    "usage-total-sessions": "KPI Total Sessions",
    "usage-total-calls": "KPI Total LLM Calls",
    "usage-total-users": "KPI Total Users",
    "performance-total-errors": "KPI Tool Errors",
}

# Slicer names for visualInteractions check.
SLICER_NAMES = [
    "slicer-date",
    "slicer-agent",
    "slicer-user-id",
    "slicer-trace-id",
    "slicer-span-id",
]

# Sync group names (spec.md §5.4).
SYNC_GROUPS = [
    "bqaa-date",
    "bqaa-agent",
    "bqaa-user-id",
    "bqaa-trace-id",
    "bqaa-span-id",
]

# Report-level filter names (spec.md §5.4).
REPORT_FILTERS = [
    "bqaa-report-window",
    "bqaa-session-id",
    "bqaa-model-version",
]

# Non-data element ids (spec.md §5.6).
NON_DATA_IDS = [
    "usage-nondata-1",
    "usage-nondata-2",
    "usage-nondata-3",
    "usage-nondata-4",
    "performance-nondata-1",
    "performance-nondata-2",
    "performance-nondata-3",
    "performance-nondata-4",
    "performance-nondata-5",
]

# Trace Inspector columns (F10).
TRACE_INSPECTOR_COLUMNS = [
    "timestamp",
    "event_type",
    "agent",
    "user_id",
    "trace_id",
    "span_id",
    "status",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_lock():
  """Load the schema lock file."""
  with open(LOCK_FILE, "r", encoding="utf-8") as f:
    return json.load(f)


def _load_looker_contracts(looker_root=None):
  """Load chart_manifest.yaml and product_contract.yaml."""
  import yaml

  root = Path(looker_root) if looker_root is not None else LOOKER_ROOT
  manifest_path = root / "spec" / "chart_manifest.yaml"
  product_path = root / "spec" / "product_contract.yaml"
  with open(manifest_path, "r", encoding="utf-8") as f:
    manifest = yaml.safe_load(f)
  with open(product_path, "r", encoding="utf-8") as f:
    product = yaml.safe_load(f)
  return manifest, product


def _load_tool_module(name):
  """Dynamically load a sibling tool module."""
  module_path = POWER_BI_ROOT / "tools" / f"{name}.py"
  if not module_path.exists():
    return None
  spec = importlib.util.spec_from_file_location(f"power_bi_{name}", module_path)
  assert spec is not None and spec.loader is not None
  module = importlib.util.module_from_spec(spec)
  spec.loader.exec_module(module)
  return module


_PBIP_COMMON = None


def _pbip_common():
  """Return the shared generator helpers (loaded once)."""
  global _PBIP_COMMON
  if _PBIP_COMMON is None:
    _PBIP_COMMON = _load_tool_module("pbip_common")
  return _PBIP_COMMON


def _read_text(path):
  with open(path, "r", encoding="utf-8") as f:
    return f.read()


def _read_json(path):
  with open(path, "r", encoding="utf-8") as f:
    return json.load(f)


def _iter_json_files(directory):
  """Yield (relative_path, parsed_json) for all JSON files under directory.

  Includes .platform files (JSON without .json extension) and
  definition.pbir / definition.pbism (also JSON without .json extension).
  """
  json_extensions = (".json", ".pbir", ".pbism", ".platform")
  for root, _dirs, files in os.walk(directory):
    for fn in files:
      if fn.endswith(".json") or fn in (".platform",):
        full = Path(root) / fn
        rel = full.relative_to(directory)
        yield rel, _read_json(full)
      elif fn.endswith((".pbir", ".pbism")):
        full = Path(root) / fn
        rel = full.relative_to(directory)
        yield rel, _read_json(full)


def _iter_tmdl_files(directory):
  """Yield (relative_path, text) for all .tmdl files under directory."""
  for root, _dirs, files in os.walk(directory):
    for fn in files:
      if fn.endswith(".tmdl"):
        full = Path(root) / fn
        rel = full.relative_to(directory)
        yield rel, _read_text(full)


# ---------------------------------------------------------------------------
# Schema validation (§7.1-7.2)
# ---------------------------------------------------------------------------


def fetch_schemas(dest_dir):
  """Download the 13 pinned schema files and verify their hashes."""
  lock = _load_lock()
  base_url = lock["base_url"]
  dest = Path(dest_dir)
  dest.mkdir(parents=True, exist_ok=True)
  for entry in lock["files"]:
    url = base_url + entry["path"]
    dest_file = dest / entry["path"]
    dest_file.parent.mkdir(parents=True, exist_ok=True)
    print(f"  Fetching {entry['path']} ...")
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=60) as resp:
      data = resp.read()
    actual_hash = hashlib.sha256(data).hexdigest()
    actual_bytes = len(data)
    if actual_hash != entry["sha256"]:
      return (
          f"Hash mismatch for {entry['path']}: "
          f"expected {entry['sha256']}, got {actual_hash}"
      )
    if actual_bytes != entry["bytes"]:
      return (
          f"Size mismatch for {entry['path']}: "
          f"expected {entry['bytes']}, got {actual_bytes}"
      )
    with open(dest_file, "wb") as f:
      f.write(data)
  print(f"  Fetched {len(lock['files'])} schema files to {dest}")
  return None


def _build_registry(schemas_dir):
  """Build a referencing.Registry mapping canonical URLs to local files.

  Each schema is registered under multiple URL forms because
  Microsoft's schema-embedded files declare $id with a dot
  (schema.embedded.json) while $ref from other schemas use a hyphen
  (schema-embedded.json). We register both the $id and the path-based
  URL so relative $ref resolution works.
  """
  import referencing

  lock = _load_lock()
  schemas_path = Path(schemas_dir)
  registry_data = {}
  for entry in lock["files"]:
    local_file = schemas_path / entry["path"]
    schema = _read_json(local_file)
    # Register under the schema's own $id.
    schema_id = schema.get("$id", "")
    if schema_id:
      registry_data[schema_id] = schema
    # Register under the path-based URL (with extension).
    path_url = SCHEMA_URL_PREFIX + entry["path"]
    registry_data[path_url] = schema
    # Register under the canonical URL (without schema.json suffix).
    canonical = SCHEMA_URL_PREFIX + entry["path"].replace(
        "/schema.json", ""
    ).replace("/schema-embedded.json", "")
    registry_data[canonical] = schema
  resources = {
      uri: referencing.Resource.from_contents(schema)
      for uri, schema in registry_data.items()
  }
  return referencing.Registry().with_resources(resources.items())


def _validate_file_against_schema(instance, schema, registry):
  """Validate a JSON instance against a schema using referencing."""
  import jsonschema

  validator_cls = jsonschema.Draft7Validator
  if registry is not None:
    validator = validator_cls(schema, registry=registry)
  else:
    validator = validator_cls(schema)
  errors = sorted(validator.iter_errors(instance), key=lambda e: e.path)
  return errors


def validate_schemas(schemas_dir, power_bi_root=None):
  """Validate every generated JSON file against its declared $schema."""
  pbi_root = Path(power_bi_root) if power_bi_root is not None else POWER_BI_ROOT
  model_dir = pbi_root / "BQAA_Agent_Analytics.SemanticModel"
  report_dir = pbi_root / "BQAA_Agent_Analytics.Report"
  lock = _load_lock()
  schemas_path = Path(schemas_dir)
  errors = []

  # Build a mapping from $schema URL to local schema file.
  # Register each schema under multiple URL forms to handle the
  # $id vs $ref naming mismatch (schema.embedded.json vs
  # schema-embedded.json) in Microsoft's schema files.
  schema_by_url = {}
  for entry in lock["files"]:
    local_file = schemas_path / entry["path"]
    if not local_file.exists():
      errors.append(f"Schema file missing: {entry['path']}")
      continue
    schema = _read_json(local_file)
    # Register under the schema's own $id.
    schema_id = schema.get("$id", "")
    if schema_id:
      schema_by_url[schema_id] = (entry["path"], schema)
    # Register under the path-based URL (with extension).
    path_url = SCHEMA_URL_PREFIX + entry["path"]
    schema_by_url[path_url] = (entry["path"], schema)
    # Register under the canonical URL (without suffix).
    canonical = SCHEMA_URL_PREFIX + entry["path"].replace(
        "/schema.json", ""
    ).replace("/schema-embedded.json", "")
    schema_by_url[canonical] = (entry["path"], schema)

  registry = _build_registry(schemas_dir)

  # Validate .pbip file at the project root.
  pbip_path = pbi_root / "BQAA_Agent_Analytics.pbip"
  if pbip_path.exists():
    obj = _read_json(pbip_path)
    schema_url = obj.get("$schema", "")
    if schema_url and schema_url in schema_by_url:
      _lock_path, schema = schema_by_url[schema_url]
      file_errors = _validate_file_against_schema(obj, schema, registry)
      for e in file_errors:
        errors.append(f"BQAA_Agent_Analytics.pbip: {e.message}")
    elif schema_url:
      errors.append(
          f"BQAA_Agent_Analytics.pbip: $schema "
          f"'{schema_url}' not in pinned lock"
      )

  # Validate all JSON files under both item folders (includes
  # .platform, definition.pbir, definition.pbism, report.json,
  # version.json, pages.json, page.json, visual.json).
  item_dirs = [d for d in (model_dir, report_dir) if d.exists()]
  if not item_dirs and pbi_root.exists():
    item_dirs = [pbi_root]
  for item_dir in item_dirs:
    for rel, obj in _iter_json_files(item_dir):
      # Skip the theme file (structural checks only, handled below).
      if str(rel).endswith("BQAA.json") or str(rel).endswith(
          "pbir_schemas.lock.json"
      ):
        continue
      schema_url = obj.get("$schema", "")
      if not schema_url:
        # version.json has no $schema; use versionMetadata.
        if rel.name == "version.json":
          schema_url = SCHEMA_URL_PREFIX + (
              "fabric/item/report/definition/versionMetadata/1.0.0"
          )
      if schema_url not in schema_by_url:
        errors.append(f"{rel}: $schema '{schema_url}' not in pinned lock")
        continue
      _lock_path, schema = schema_by_url[schema_url]
      file_errors = _validate_file_against_schema(obj, schema, registry)
      for e in file_errors:
        errors.append(f"{rel}: {e.message}")

  # Theme JSON gets structural checks only.
  theme_path = (
      report_dir / "StaticResources" / "RegisteredResources" / "BQAA.json"
  )
  if theme_path.exists():
    theme = _read_json(theme_path)
    hex_re = re.compile(r"^#[0-9A-Fa-f]{6}$")
    required_keys = [
        "name",
        "dataColors",
        "foreground",
        "background",
        "tableAccent",
    ]
    for key in required_keys:
      if key not in theme:
        errors.append(f"Theme missing key: {key}")
    if "dataColors" in theme:
      for c in theme["dataColors"]:
        if not hex_re.match(c):
          errors.append(f"Theme dataColors: invalid hex '{c}'")

  return errors


def validate_json_schemas(power_bi_root_or_schemas_dir, schemas_dir=None):
  """Validate JSON schemas under a root directory (or with schemas_dir only)."""
  if schemas_dir is None:
    return validate_schemas(power_bi_root_or_schemas_dir)
  return validate_schemas(
      schemas_dir, power_bi_root=power_bi_root_or_schemas_dir
  )


# ---------------------------------------------------------------------------
# TMDL lint (§7.3)
# ---------------------------------------------------------------------------


_TMDL_COLUMN_DECL_RE = re.compile(r"^\tcolumn\s+(.+?)\s*$")
_TMDL_COLUMN_PROP_RE = re.compile(r"^\t\t(dataType|sourceColumn):\s*(.+?)\s*$")
_TMDL_MEASURE_DECL_RE = re.compile(
    r"^\tmeasure\s+('(?:[^']|'')+'|[^\s=']+)\s*=\s*(.*?)\s*$"
)
_TMDL_ANNOTATION_RE = re.compile(r"^\t\tannotation\s+(\S+)\s*=\s*(.*?)\s*$")
_TMDL_FORMAT_STRING_RE = re.compile(r"^\t\tformatString:\s*(.*?)\s*$")
# A line indented by exactly one tab starts the next table-level object.
_TMDL_OBJECT_LINE_RE = re.compile(r"^\t\S")
# An M text literal: double quotes, with "" as the escaped quote.
_M_STRING_LITERAL_RE = re.compile(r'"((?:[^"]|"")*)"')
_M_LIST_TOKEN_RE = re.compile(r'\s*(?:(,)|(\})|"((?:[^"]|"")*)")')
_DISTINCT_COUNT_COL_RE = re.compile(
    r"DISTINCTCOUNTNOBLANK\(\s*'events_v1'\[(trace_id|session_id|user_id|invocation_id)\]\s*\)"
)


def _strip_sql_comments(sql):
  """Strip /* ... */ and -- ... SQL comments."""
  without_block = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
  return re.sub(r"--[^\r\n]*", " ", without_block)


def _oracle_final_limit(sql):
  """Return the integer value of the final LIMIT clause in *sql*, or None."""
  limits = re.findall(
      r"\bLIMIT\s+(\d+)\b", _strip_sql_comments(sql), flags=re.IGNORECASE
  )
  return int(limits[-1]) if limits else None


def _resolve_tmdl_measure_distinct_columns(declared):
  """Map each measure name to its distinct-count column (direct or transitive)."""
  resolved = {}
  for name, info in declared.items():
    dax = info.get("dax", "")
    match = _DISTINCT_COUNT_COL_RE.search(dax)
    if match:
      resolved[name] = match.group(1)
  changed = True
  while changed:
    changed = False
    for name, info in declared.items():
      if name in resolved:
        continue
      dax = info.get("dax", "")
      ref_cols = {
          resolved[ref]
          for ref in re.findall(r"\[([^\[\]]+)\]", dax)
          if ref in resolved
      }
      if len(ref_cols) == 1:
        resolved[name] = next(iter(ref_cols))
        changed = True
  return resolved


def _tmdl_unquote(name):
  """Strip the optional single quotes around a TMDL object name."""
  if len(name) >= 2 and name[0] == "'" and name[-1] == "'":
    return name[1:-1].replace("''", "'")
  return name


def _is_tmdl_doc_line(raw):
  """Whether *raw* is a `///` documentation line (never a declaration)."""
  return raw.lstrip("\t").startswith("///")


def _parse_tmdl_columns(text, label):
  """Parse the real `column` blocks of a TMDL table file.

  `///` documentation lines are skipped, so a comment that merely describes a
  column can neither satisfy nor mask the actual declaration.  Returns
  `(columns, errors)`; each column is a dict with `name`, `line` and the
  `dataType` / `sourceColumn` properties found in its block.
  """
  columns = []
  errors = []
  current = None
  for lineno, raw in enumerate(text.splitlines(), 1):
    if _is_tmdl_doc_line(raw):
      continue
    decl = _TMDL_COLUMN_DECL_RE.match(raw)
    if decl:
      current = {"name": _tmdl_unquote(decl.group(1)), "line": lineno}
      columns.append(current)
    elif _TMDL_OBJECT_LINE_RE.match(raw):
      current = None
    elif current is not None:
      prop = _TMDL_COLUMN_PROP_RE.match(raw)
      if prop:
        key, value = prop.group(1), _tmdl_unquote(prop.group(2))
        if key in current:
          errors.append(
              f"{label}:{lineno}: duplicate {key} on column {current['name']!r}"
          )
        else:
          current[key] = value
  return columns, errors


def _parse_tmdl_measures(text):
  """Parse the real `measure` declarations of a TMDL table file.

  Returns a list of `{"name", "line", "dax", "format_strings", "annotations"}`
  dicts; `format_strings` lists every `formatString` value in the measure block
  and `annotations` maps an annotation name to the list of values declared in
  the measure block.
  """
  measures = []
  current = None
  for lineno, raw in enumerate(text.splitlines(), 1):
    if _is_tmdl_doc_line(raw):
      continue
    decl = _TMDL_MEASURE_DECL_RE.match(raw)
    if decl:
      current = {
          "name": _tmdl_unquote(decl.group(1)),
          "line": lineno,
          "dax": decl.group(2),
          "format_strings": [],
          "annotations": {},
      }
      measures.append(current)
    elif _TMDL_OBJECT_LINE_RE.match(raw):
      current = None
    elif current is not None:
      ann = _TMDL_ANNOTATION_RE.match(raw)
      fmt = _TMDL_FORMAT_STRING_RE.match(raw)
      if ann:
        current["annotations"].setdefault(ann.group(1), []).append(ann.group(2))
      elif fmt:
        current["format_strings"].append(fmt.group(1))
  return measures


def _m_string_list(text, start):
  """Parse the string literals of an M list, just after its opening brace.

  Returns `(literals, error)`; the literals are unescaped (`""` -> `"`) and
  `error` is None only when the list holds nothing but string literals.
  """
  literals = []
  pos = start
  while True:
    token = _M_LIST_TOKEN_RE.match(text, pos)
    if token is None:
      return literals, f"unexpected token near {text[pos:pos + 30]!r}"
    pos = token.end()
    if token.group(2):
      return literals, None
    if token.group(3) is not None:
      literals.append(token.group(3).replace('""', '"'))


def _check_events_columns(events_text):
  """Check the real column declarations of events_v1.tmdl (29 columns)."""
  columns, errors = _parse_tmdl_columns(events_text, "events_v1.tmdl")
  seen = set()
  for col in columns:
    if col["name"] in seen:
      errors.append(
          f"events_v1.tmdl:{col['line']}: duplicate column {col['name']!r}"
      )
    seen.add(col["name"])
  for name in EVENTS_COLUMNS:
    if name not in seen:
      errors.append(f"events_v1.tmdl: missing column '{name}'")
  for col in columns:
    name, line = col["name"], col["line"]
    expected_type = EVENTS_COLUMN_TYPES.get(name)
    if expected_type is None:
      errors.append(f"events_v1.tmdl:{line}: unexpected column '{name}'")
      continue
    source = col.get("sourceColumn")
    if source != name:
      errors.append(
          f"events_v1.tmdl:{line}: column '{name}' has sourceColumn "
          f"{source!r}, expected '{name}'"
      )
    dtype = col.get("dataType")
    if dtype not in VALID_DATA_TYPES:
      errors.append(
          f"events_v1.tmdl:{line}: column '{name}' has invalid "
          f"dataType {dtype!r}"
      )
    elif dtype != expected_type:
      errors.append(
          f"events_v1.tmdl:{line}: column '{name}' has dataType "
          f"'{dtype}', expected '{expected_type}'"
      )
  return errors


def _check_events_measures(events_text):
  """Check the real measure declarations of events_v1.tmdl (50 measures)."""
  errors = []
  parsed = _parse_tmdl_measures(events_text)
  declared = [m["name"] for m in parsed]
  counts = {}
  for name in declared:
    counts[name] = counts.get(name, 0) + 1
  for measure in MEASURE_NAMES:
    if measure not in counts:
      errors.append(f"events_v1.tmdl: missing measure '{measure}'")
  for name, count in counts.items():
    if name not in MEASURE_NAMES:
      errors.append(f"events_v1.tmdl: unexpected measure '{name}'")
    elif count > 1:
      errors.append(f"events_v1.tmdl: duplicate measure '{name}'")
  if len(declared) != len(MEASURE_NAMES):
    errors.append(
        f"events_v1.tmdl: expected {len(MEASURE_NAMES)} measures, "
        f"found {len(declared)}"
    )
  errors.extend(_check_measure_format_strings(parsed))
  return errors


def _check_measure_format_strings(parsed_measures):
  """Pin each measure's TMDL formatString to its live comparison type.

  Exact counts and token sums must format as `#,0`; only the pinned
  FLOAT_COMPARISON_MEASURES may use a fractional or percentage format.
  """
  errors = []
  for measure in parsed_measures:
    name = measure["name"]
    if name not in MEASURE_NAMES:
      continue
    formats = measure.get("format_strings", [])
    where = f"events_v1.tmdl:{measure['line']}: measure '{name}'"
    if len(formats) != 1:
      errors.append(
          f"{where} must declare exactly one formatString, found {formats}"
      )
    elif name in FLOAT_COMPARISON_MEASURES:
      if formats[0] not in FLOAT_MEASURE_FORMATS:
        errors.append(
            f"{where} is compared as a float and must use one of "
            f"{sorted(FLOAT_MEASURE_FORMATS)}, found {formats[0]!r}"
        )
    elif formats[0] != INT_MEASURE_FORMAT:
      errors.append(
          f"{where} is an exact count or sum and must use formatString "
          f"{INT_MEASURE_FORMAT!r}, found {formats[0]!r}"
      )
  return errors


def _check_oracle_refs(events_text, looker_root):
  """Every oracle query cited by a measure description must exist."""
  errors = []
  for raw in events_text.splitlines():
    if not _is_tmdl_doc_line(raw):
      continue
    for ref in re.findall(r"oracle/queries/(\S+\.sql)", raw):
      if not (looker_root / "oracle" / "queries" / ref).exists():
        errors.append(
            f"events_v1.tmdl: references non-existent oracle file '{ref}'"
        )
  return errors


def _check_events_sql(expr_text):
  """I8: no ';', '\"' or unescaped '#(' in the SQL built by expressions.tmdl.

  The SQL is assembled from the string literals of `EventsSelectLines` and
  `EventsSql`; every one of those literals is scanned (after undoing the M
  `""` escape), not just a single quoted assignment.
  """
  errors = []
  sources = []
  select = re.search(r"\bEventsSelectLines\s*=\s*\{", expr_text)
  if select is None:
    errors.append("expressions.tmdl: EventsSelectLines list not found")
  else:
    literals, problem = _m_string_list(expr_text, select.end())
    if problem:
      errors.append(
          "expressions.tmdl: EventsSelectLines is not a plain list of "
          f"string literals ({problem})"
      )
    for index, literal in enumerate(literals, 1):
      sources.append((f"EventsSelectLines literal {index}", literal))
  for expr_name in ("EventsSql", "AnchorSql", "FallbackAnchor"):
    sql_expr = re.search(
        rf"(?m)^[ \t]*{re.escape(expr_name)}\s*=\s*(.*)$", expr_text
    )
    if sql_expr is None:
      errors.append(f"expressions.tmdl: {expr_name} expression not found")
    else:
      for index, match in enumerate(
          _M_STRING_LITERAL_RE.finditer(sql_expr.group(1)), 1
      ):
        literal = match.group(1).replace('""', '"')
        # "#(lf)" is the line separator handed to Text.Combine, not SQL text.
        if literal != "#(lf)":
          sources.append((f"{expr_name} literal {index}", literal))
  for label, literal in sources:
    if ";" in literal:
      errors.append(f"expressions.tmdl: {label} contains ';'")
    if '"' in literal:
      errors.append(f"expressions.tmdl: {label} contains a double quote")
    # "#(#)(" spells a literal "#(" in M; any other "#(" starts an escape.
    if "#(" in literal.replace("#(#)(", ""):
      errors.append(f"expressions.tmdl: {label} contains unescaped '#('")
  return errors


def validate_tmdl(power_bi_root=None, looker_root=None):
  """Run custom TMDL lint checks."""
  errors = []
  looker = Path(looker_root) if looker_root is not None else LOOKER_ROOT
  if power_bi_root is None:
    model_dir = MODEL_DIR / "definition"
  else:
    p = Path(power_bi_root)
    if (p / "BQAA_Agent_Analytics.SemanticModel" / "definition").exists():
      model_dir = p / "BQAA_Agent_Analytics.SemanticModel" / "definition"
    elif (p / "definition").exists():
      model_dir = p / "definition"
    else:
      model_dir = p
  if not model_dir.exists():
    return [f"Model definition directory missing: {model_dir}"]

  # Collect all TMDL files.
  tmdl_files = {}
  for rel, text in _iter_tmdl_files(model_dir):
    tmdl_files[str(rel)] = text

  # Check database.tmdl, model.tmdl and expressions.tmdl exist.
  for required in ("database.tmdl", "model.tmdl", "expressions.tmdl"):
    if required not in tmdl_files:
      errors.append(f"Missing {required}")

  # Tabs-only indentation, no mixed tabs/spaces, no pure-space indentation.
  for rel, text in tmdl_files.items():
    lines = text.splitlines()
    for i, line in enumerate(lines, 1):
      if line.startswith(" "):
        errors.append(f"{rel}:{i}: space indentation (tabs only)")
      if "\t" in line:
        stripped = line.lstrip("\t")
        leading = line[: len(line) - len(stripped)]
        if " " in leading:
          errors.append(f"{rel}:{i}: mixed tabs and spaces in indentation")

  # Check ref table ↔ tables/*.tmdl 1:1.
  model_text = tmdl_files.get("model.tmdl", "")
  ref_tables = set(re.findall(r"ref table (\S+)", model_text))
  table_files = set()
  tables_dir = model_dir / "tables"
  if tables_dir.exists():
    for f in tables_dir.glob("*.tmdl"):
      table_files.add(f.stem)
  if ref_tables != table_files:
    if ref_tables - table_files:
      errors.append(f"ref tables without files: {ref_tables - table_files}")
    if table_files - ref_tables:
      errors.append(f"table files without ref: {table_files - ref_tables}")

  # Validate events_v1.tmdl columns and measures from the real declarations.
  events_path = tables_dir / "events_v1.tmdl"
  expr_text = tmdl_files.get("expressions.tmdl", "")
  if not events_path.exists():
    errors.append("Missing tables/events_v1.tmdl")
  else:
    events_text = _read_text(events_path)
    errors.extend(_check_events_columns(events_text))
    # No DISTINCTCOUNT( (only DISTINCTCOUNTNOBLANK).
    if "DISTINCTCOUNT(" in events_text:
      errors.append(
          "events_v1.tmdl: found DISTINCTCOUNT( — use DISTINCTCOUNTNOBLANK"
      )
    errors.extend(_check_events_measures(events_text))
    errors.extend(_check_oracle_refs(events_text, looker))
  # I8: the SQL that expressions.tmdl assembles must stay free of ; " and #(.
  if expr_text:
    errors.extend(_check_events_sql(expr_text))

  # I3: raw parameter names only in validator lines.
  raw_params = [
      "BQAA_ProjectId",
      "BQAA_DatasetId",
      "BQAA_TableId",
      "BQAA_BillingProjectId",
      "BQAA_WindowEndDate",
      "BQAA_LookbackDays",
      "BQAA_UseStorageApi",
  ]
  for param in raw_params:
    # Find all occurrences of the raw param name.
    for m in re.finditer(re.escape(param), expr_text):
      line_start = expr_text.rfind("\n", 0, m.start()) + 1
      line_end = expr_text.find("\n", m.end())
      if line_end == -1:
        line_end = len(expr_text)
      line = expr_text[line_start:line_end]
      # Raw param should only appear in Is*/Reject/validation lines.
      if not re.search(r"(Is[A-Z]|Reject|IsParameterQuery|meta\s*\[)", line):
        errors.append(
            f"expressions.tmdl: raw parameter '{param}' used "
            f"outside validator line: {line.strip()}"
        )

  # Lineage tags are UUID-shaped and unique.
  uuid_re = re.compile(
      r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
      re.IGNORECASE,
  )
  uuids = set()
  for rel, text in tmdl_files.items():
    for m in uuid_re.finditer(text):
      uid = m.group(0).lower()
      if uid in uuids:
        errors.append(f"{rel}: duplicate lineage tag {uid}")
      uuids.add(uid)

  return errors


# ---------------------------------------------------------------------------
# Parity (§7.4)
# ---------------------------------------------------------------------------


def _projection_measure(projection):
  """Return the measure a visual projection is bound to ('' if none)."""
  if not isinstance(projection, dict):
    return ""
  return (
      projection.get("field", {}).get("Measure", {}).get("Property")
      or projection.get("nativeQueryRef", "")
      or ""
  )


def _check_measure_oracle_binding(
    events_text, measure_queries, measure_columns, looker_root
):
  """Check each measure against the live oracle SQL it cites.

  `measure_queries` / `measure_columns` come from semantic_model.yaml.  Every
  generated measure must (a) name its `oracle_column` as an `AS <column>`
  output alias of a cited query, and (b) carry the `bqaa_oracle_sha256`
  annotation that matches the SQL on disk, so an oracle edited after the
  model was generated is reported until the model is regenerated.
  """
  errors = []
  common = _pbip_common()
  declared = {m["name"]: m for m in _parse_tmdl_measures(events_text)}
  measure_distinct_cols = _resolve_tmdl_measure_distinct_columns(declared)
  for name, queries in measure_queries.items():
    block = declared.get(name)
    if block is None:
      continue  # reported by validate_tmdl as a missing measure
    texts = {}
    for query in queries:
      query_path = looker_root / query
      if not query_path.is_file():
        errors.append(f"Measure '{name}' cites missing oracle query '{query}'")
      else:
        texts[query] = _read_text(query_path)
    if len(texts) != len(queries):
      continue
    ocol = measure_columns.get(name, "")
    if ocol and not any(
        common.has_output_alias(sql, ocol) for sql in texts.values()
    ):
      errors.append(
          f"Measure '{name}' oracle_column '{ocol}' is not an output alias of "
          f"any cited oracle query"
      )
    distinct_col = measure_distinct_cols.get(name)
    if distinct_col is not None:
      col_pat = re.compile(
          rf"COUNT\s*\(\s*DISTINCT\b[^)]*\b{re.escape(distinct_col)}\b",
          re.IGNORECASE,
      )
      for query, sql in texts.items():
        if not col_pat.search(_strip_sql_comments(sql)):
          errors.append(
              f"Measure '{name}' counts distinct '{distinct_col}', but cited "
              f"oracle query '{query}' does not contain "
              f"COUNT(DISTINCT ... {distinct_col})"
          )
    digests = block["annotations"].get("bqaa_oracle_sha256", [])
    expected = common.oracle_digest(texts, queries)
    if len(digests) != 1:
      errors.append(
          f"events_v1.tmdl: measure '{name}' must carry exactly one "
          f"bqaa_oracle_sha256 annotation, found {len(digests)}"
      )
    elif digests[0] != expected:
      errors.append(
          f"events_v1.tmdl: measure '{name}' oracle digest {digests[0]} != "
          f"{expected} for the cited oracle SQL (regenerate with "
          "generate_pbip.py --write)"
      )
  return errors


def _check_oracle_lock(pbi_root, looker_root):
  """Compare the cited oracle SQL with the reviewed lock (spec/oracle_lock.json).

  Unlike the per-measure digest annotation, which every regeneration refreshes,
  the lock only moves when a person runs `generate_pbip.py --write
  --accept-oracle-changes`, so an edited oracle query is reported here until
  the DAX measures that cite it have been reconciled.
  """
  common = _pbip_common()
  try:
    contracts = common.load_contracts(looker_root=looker_root)
    model_spec = common.load_spec("semantic_model.yaml", power_bi_root=pbi_root)
    return common.oracle_lock_problems(pbi_root, contracts, model_spec)
  except (OSError, ValueError, KeyError) as exc:
    return [f"oracle lock could not be evaluated: {exc}"]


def validate_parity(power_bi_root=None, looker_root=None):
  """Check chart_manifest ↔ visuals ↔ measures ↔ oracle parity."""
  errors = []
  looker = Path(looker_root) if looker_root is not None else LOOKER_ROOT
  manifest, product = _load_looker_contracts(looker)
  pbi_root = Path(power_bi_root) if power_bi_root is not None else POWER_BI_ROOT
  if (
      pbi_root / "BQAA_Agent_Analytics.Report" / "definition" / "pages"
  ).exists():
    report_dir = pbi_root / "BQAA_Agent_Analytics.Report"
  elif (pbi_root / "definition" / "pages").exists():
    report_dir = pbi_root
  else:
    report_dir = REPORT_DIR
  if (pbi_root / "BQAA_Agent_Analytics.SemanticModel" / "definition").exists():
    model_dir = pbi_root / "BQAA_Agent_Analytics.SemanticModel"
  else:
    model_dir = MODEL_DIR

  # Extract manifest chart ids.
  manifest_charts = []
  for chart in manifest.get("charts", []):
    manifest_charts.append(chart["id"])
  if len(manifest_charts) != 37:
    errors.append(f"Expected 37 manifest charts, got {len(manifest_charts)}")

  # Check report visuals exist.
  report_pages_dir = report_dir / "definition" / "pages"
  if not report_pages_dir.exists():
    return [f"Report pages directory missing: {report_pages_dir}"]

  # Collect all visual names from all pages.
  visual_names = set()
  visual_by_page = {}
  for page_dir in sorted(report_pages_dir.iterdir()):
    if not page_dir.is_dir() or page_dir.name == "pages.json":
      continue
    page_visuals = set()
    visuals_dir = page_dir / "visuals"
    if visuals_dir.exists():
      for vdir in sorted(visuals_dir.iterdir()):
        if vdir.is_dir():
          visual_json_path = vdir / "visual.json"
          if visual_json_path.exists():
            vobj = _read_json(visual_json_path)
            vname = vobj.get("name", vdir.name)
            visual_names.add(vname)
            page_visuals.add(vname)
    visual_by_page[page_dir.name] = page_visuals

  # 37 manifest ids == 37 parity visual names.
  # Standard non-parity visuals (headers, slicers, trace inspector).
  standard_visuals = set(NON_DATA_IDS) | {
      "page-heading",
      "slicer-date",
      "slicer-agent",
      "slicer-user-id",
      "slicer-trace-id",
      "slicer-span-id",
      "trace-inspector-table",
  }
  manifest_set = set(manifest_charts)
  parity_visuals = manifest_set
  if parity_visuals != visual_names - standard_visuals:
    missing = parity_visuals - visual_names
    extra = (visual_names - standard_visuals) - parity_visuals
    if missing:
      errors.append(f"Missing parity visuals: {missing}")
    if extra:
      errors.append(f"Extra non-data visuals: {extra}")

  # Check non-data elements present.
  for nid in NON_DATA_IDS:
    if nid not in visual_names:
      errors.append(f"Missing non-data visual: {nid}")

  # Check visual names ≤ 50 chars.
  for vname in visual_names:
    if len(vname) > 50:
      errors.append(f"Visual name > 50 chars: '{vname}'")

  # Check visualInteractions NoFilter set = F4 pairs.
  # Each page should only have NoFilter pairs for visuals on that page.
  for page_dir in sorted(report_pages_dir.iterdir()):
    if not page_dir.is_dir():
      continue
    page_json_path = page_dir / "page.json"
    if not page_json_path.exists():
      continue
    page_obj = _read_json(page_json_path)
    interactions = page_obj.get("visualInteractions", [])
    page_name = page_dir.name
    page_visuals = visual_by_page.get(page_name, set())
    # Only count NoFilter pairs where the target is on this page.
    no_filter = {
        (i.get("source"), i.get("target"))
        for i in interactions
        if i.get("type") == "NoFilter" and i.get("target") in page_visuals
    }
    # Expected NoFilter pairs: slicer-user-id → the F4 exception
    # charts that are on this page.
    expected_nf = set()
    for chart_id in NON_UID_LISTENERS:
      if chart_id in page_visuals:
        expected_nf.add(("slicer-user-id", chart_id))
    if expected_nf != no_filter:
      errors.append(
          f"Page '{page_name}': NoFilter set mismatch. "
          f"Expected {expected_nf}, got {no_filter}"
      )

  # Check report-level filters.
  report_json_path = report_dir / "definition" / "report.json"
  if report_json_path.exists():
    report_obj = _read_json(report_json_path)
    filter_config = report_obj.get("filterConfig", {})
    filters = filter_config.get("filters", [])
    filter_names = [f.get("name", "") for f in filters]
    for rf in REPORT_FILTERS:
      if rf not in filter_names:
        errors.append(f"Missing report filter: {rf}")
    # Check window filter is hidden and locked.
    for f in filters:
      if f.get("name") == "bqaa-report-window":
        if not f.get("isHiddenInViewMode"):
          errors.append("Report window filter not hidden")
        if not f.get("isLockedInViewMode"):
          errors.append("Report window filter not locked")

  # Check slicer sync groups.
  for page_dir in sorted(report_pages_dir.iterdir()):
    if not page_dir.is_dir():
      continue
    visuals_dir = page_dir / "visuals"
    if not visuals_dir.exists():
      continue
    found_sync = set()
    for vdir in visuals_dir.iterdir():
      if not vdir.is_dir():
        continue
      visual_json_path = vdir / "visual.json"
      if not visual_json_path.exists():
        continue
      vobj = _read_json(visual_json_path)
      sync = vobj.get("visual", {}).get("syncGroup", {})
      group_name = sync.get("groupName", "")
      if group_name:
        found_sync.add(group_name)
    for sg in SYNC_GROUPS:
      if sg not in found_sync:
        errors.append(f"Page '{page_dir.name}': missing sync group '{sg}'")

  # Check Trace Inspector table columns.
  ti_page = report_pages_dir / "trace-inspector"
  if ti_page.exists():
    ti_visuals = ti_page / "visuals"
    if ti_visuals.exists():
      for vdir in ti_visuals.iterdir():
        if not vdir.is_dir():
          continue
        visual_json_path = vdir / "visual.json"
        if not visual_json_path.exists():
          continue
        vobj = _read_json(visual_json_path)
        if vobj.get("name") == "trace-inspector-table":
          visual_type = vobj.get("visual", {}).get("visualType", "")
          if visual_type != "tableEx":
            errors.append(
                "trace-inspector-table: expected visualType " "'tableEx'"
            )
          # Check the 7 columns via projections.
          projections = (
              vobj.get("visual", {})
              .get("query", {})
              .get("queryState", {})
              .get("Values", {})
              .get("projections", [])
          )
          col_names = [
              p.get("nativeQueryRef", "")
              for p in projections
              if isinstance(p, dict)
          ]
          if col_names != TRACE_INSPECTOR_COLUMNS:
            errors.append(
                f"trace-inspector-table: columns {col_names} "
                f"!= {TRACE_INSPECTOR_COLUMNS}"
            )

  # Check oracle files exist for all 37 chart ids.
  for chart_id in manifest_charts:
    oracle_path = looker / "oracle" / "queries" / f"{chart_id}.sql"
    if not oracle_path.exists():
      errors.append(f"Oracle file missing for chart '{chart_id}'")

  # Check Top-N limit parity across chart_manifest.yaml, report_visuals.yaml,
  # and oracle SQL final LIMIT for all 12 looker_bar / Top-N charts.
  rv_path = (
      pbi_root / "spec" / "report_visuals.yaml"
      if (pbi_root / "spec" / "report_visuals.yaml").exists()
      else POWER_BI_ROOT / "spec" / "report_visuals.yaml"
  )
  visual_top_n_by_id = {}
  if rv_path.exists():
    import yaml

    rv_data = yaml.safe_load(_read_text(rv_path)) or {}
    for v_entry in (
        rv_data.get("parity_visuals") or rv_data.get("visuals") or []
    ):
      if isinstance(v_entry, dict) and v_entry.get("top_n") is not None:
        visual_top_n_by_id[v_entry.get("id", "")] = v_entry.get("top_n")
  manifest_by_id = {
      c["id"]: c
      for c in manifest.get("charts", [])
      if isinstance(c, dict) and c.get("id")
  }
  top_n_chart_ids = sorted(
      {
          cid
          for cid, c in manifest_by_id.items()
          if c.get("looker_type") == "looker_bar"
      }
      | set(visual_top_n_by_id)
  )
  for cid in top_n_chart_ids:
    m_limit = manifest_by_id.get(cid, {}).get("limit")
    v_top_n = visual_top_n_by_id.get(cid)
    oracle_path = looker / "oracle" / "queries" / f"{cid}.sql"
    o_limit = (
        _oracle_final_limit(_read_text(oracle_path))
        if oracle_path.exists()
        else None
    )
    if m_limit is None or v_top_n is None or o_limit is None:
      errors.append(
          f"Top-N limit is missing for '{cid}': "
          f"manifest limit={m_limit}, report_visuals top_n={v_top_n}, "
          f"oracle LIMIT={o_limit}"
      )
    elif m_limit != o_limit:
      errors.append(
          f"Top-N limit mismatch for '{cid}': "
          f"source manifest limit={m_limit} and oracle LIMIT={o_limit} "
          f"disagree"
      )
    elif v_top_n > o_limit:
      errors.append(
          f"Top-N limit mismatch for '{cid}': "
          f"report_visuals top_n={v_top_n} exceeds the available "
          f"source rows (oracle LIMIT={o_limit})"
      )
    elif cid in TOP_N_BELOW_SOURCE_LIMIT:
      if (v_top_n, o_limit) != TOP_N_BELOW_SOURCE_LIMIT[cid]:
        pinned_top_n, pinned_limit = TOP_N_BELOW_SOURCE_LIMIT[cid]
        errors.append(
            f"Top-N limit mismatch for '{cid}': report_visuals "
            f"top_n={v_top_n} and oracle LIMIT={o_limit} must stay "
            f"top_n={pinned_top_n} over a source LIMIT={pinned_limit} "
            f"(README known difference D14)"
        )
    elif v_top_n != o_limit:
      errors.append(
          f"Top-N limit mismatch for '{cid}': report_visuals "
          f"top_n={v_top_n} must equal the source LIMIT={o_limit}"
      )

  # Every other chart whose oracle query ends in a LIMIT must keep the source
  # manifest limit equal to it, so neither side of the source contract can
  # drift on its own (for example the 5,000-row Tool Calls Over Time query).
  for cid in sorted(set(manifest_by_id) - set(top_n_chart_ids)):
    oracle_path = looker / "oracle" / "queries" / f"{cid}.sql"
    if not oracle_path.exists():
      continue
    o_limit = _oracle_final_limit(_read_text(oracle_path))
    if o_limit is None:
      continue
    m_limit = manifest_by_id[cid].get("limit")
    if m_limit != o_limit:
      errors.append(
          f"Source limit mismatch for '{cid}': manifest limit={m_limit}, "
          f"oracle LIMIT={o_limit}"
      )

  # Check parity visual titles in visualContainerObjects.title, cardVisual
  # projection displayName, measure oracle_column in oracle SQL, and overlaps.
  product_titles = {}
  charts_entry = product.get("charts", [])
  if isinstance(charts_entry, list):
    for c in charts_entry:
      if isinstance(c, dict) and c.get("id"):
        product_titles[c["id"]] = c.get("title", "")
  elif isinstance(charts_entry, dict):
    for cid, cinfo in charts_entry.items():
      product_titles[cid] = cinfo.get("title", "")

  sm_path = (
      pbi_root / "spec" / "semantic_model.yaml"
      if (pbi_root / "spec" / "semantic_model.yaml").exists()
      else POWER_BI_ROOT / "spec" / "semantic_model.yaml"
  )
  measure_oracle_col = {}
  measure_oracle_queries = {}
  if not sm_path.exists():
    errors.append(f"Missing spec: {sm_path}")
  else:
    import yaml

    sm_data = yaml.safe_load(_read_text(sm_path))
    for m_entry in sm_data.get("measures", []):
      if isinstance(m_entry, dict) and m_entry.get("name"):
        measure_oracle_col[m_entry["name"]] = m_entry.get("oracle_column", "")
        measure_oracle_queries[m_entry["name"]] = list(
            m_entry.get("oracle_queries") or []
        )
        expected_type = (
            "float" if m_entry["name"] in FLOAT_COMPARISON_MEASURES else "int"
        )
        if m_entry.get("comparison_type") != expected_type:
          errors.append(
              f"semantic_model.yaml: measure '{m_entry['name']}' has "
              f"comparison_type {m_entry.get('comparison_type')!r}, "
              f"expected {expected_type!r}"
          )
    errors.extend(_check_oracle_lock(pbi_root, looker))

  for page_dir in sorted(report_pages_dir.iterdir()):
    if not page_dir.is_dir():
      continue
    visuals_dir = page_dir / "visuals"
    if not visuals_dir.exists():
      continue
    rects = []
    for vdir in sorted(visuals_dir.iterdir()):
      if not vdir.is_dir():
        continue
      vjson_path = vdir / "visual.json"
      if not vjson_path.exists():
        continue
      vobj = _read_json(vjson_path)
      vname = vobj.get("name", vdir.name)
      pos = vobj.get("position", vobj.get("visual", {}).get("position", {}))
      x, y, w, h = (
          pos.get("x"),
          pos.get("y"),
          pos.get("width"),
          pos.get("height"),
      )
      if all(v is not None for v in (x, y, w, h)):
        rects.append((x, y, w, h, vname))
      if vname in manifest_set:
        raw_title = product_titles.get(vname, "")
        title_objs = (
            vobj.get("visual", {})
            .get("visualContainerObjects", {})
            .get("title", [])
        )
        if not title_objs:
          errors.append(
              f"Visual '{vname}' missing visualContainerObjects.title"
          )
        else:
          expected_title = raw_title.replace("'", "''")
          actual_lit = (
              title_objs[0]
              .get("properties", {})
              .get("text", {})
              .get("expr", {})
              .get("Literal", {})
              .get("Value", "")
          )
          if expected_title and actual_lit != f"'{expected_title}'":
            errors.append(
                f"Visual '{vname}' title '{actual_lit}' != '{expected_title}'"
            )

        vtype = vobj.get("visual", {}).get("visualType", "")
        qstate = vobj.get("visual", {}).get("query", {}).get("queryState", {})
        if vtype == "cardVisual":
          projs = qstate.get("Data", {}).get("projections", [])
          if not projs or projs[0].get("displayName") != raw_title:
            errors.append(
                f"Card visual '{vname}' displayName "
                f"{projs[0].get('displayName') if projs else None!r} != {raw_title!r}"
            )
          # The six KPI scorecards show their current value and the
          # "Change %" comparison against the previous period.
          kpi_measure = KPI_SCORECARDS.get(vname)
          if kpi_measure is not None:
            bound = [_projection_measure(p) for p in projs]
            expected_bound = [kpi_measure, f"{kpi_measure} Change %"]
            if bound != expected_bound:
              errors.append(
                  f"KPI scorecard '{vname}' binds {bound}, expected "
                  f"{expected_bound}"
              )
        else:
          projs = qstate.get("Y", {}).get("projections", [])

        oracle_sql_path = looker / "oracle" / "queries" / f"{vname}.sql"
        oracle_sql_text = (
            _read_text(oracle_sql_path) if oracle_sql_path.exists() else ""
        )
        for p in projs:
          m_prop = _projection_measure(p)
          ocol = measure_oracle_col.get(m_prop, "")
          if (
              ocol
              and oracle_sql_text
              and not _pbip_common().has_output_alias(oracle_sql_text, ocol)
          ):
            errors.append(
                f"Visual '{vname}' measure '{m_prop}' oracle_column '{ocol}' "
                f"is not an output alias of {oracle_sql_path.name}"
            )

    for i in range(len(rects)):
      for j in range(i + 1, len(rects)):
        x1, y1, w1, h1, n1 = rects[i]
        x2, y2, w2, h2, n2 = rects[j]
        if x1 < x2 + w2 and x1 + w1 > x2 and y1 < y2 + h2 and y1 + h1 > y2:
          errors.append(f"Overlap on page '{page_dir.name}': {n1} and {n2}")

  # Check F2/F9 COALESCE, F3/F4/F5 KEEPFILTERS, F11 PERCENTILE.INC in events_v1.tmdl.
  events_tmdl_path = model_dir / "definition" / "tables" / "events_v1.tmdl"
  if events_tmdl_path.exists():
    events_tmdl = _read_text(events_tmdl_path)
    if not re.search(
        r"measure\s+'KPI Total Tokens'\s*=\s*\[Total Tokens\]", events_tmdl
    ):
      errors.append(
          "events_v1.tmdl: 'KPI Total Tokens' must equal [Total Tokens]"
      )
    for kpi_name in (
        "KPI Total Traces",
        "KPI Total Sessions",
        "KPI Total Users",
        "KPI Total LLM Calls",
        "KPI Tool Errors",
    ):
      if not re.search(
          rf"measure\s+'{re.escape(kpi_name)}'\s*=\s*COALESCE\(", events_tmdl
      ):
        errors.append(f"events_v1.tmdl: '{kpi_name}' must use COALESCE(..., 0)")
    for kpi_prior_name in (
        "KPI Total Traces Prior",
        "KPI Total Sessions Prior",
        "KPI Total Users Prior",
        "KPI Total LLM Calls Prior",
        "KPI Tool Errors Prior",
    ):
      if not re.search(
          rf"measure\s+'{re.escape(kpi_prior_name)}'\s*=[^\n]*CALCULATE\(COALESCE\(",
          events_tmdl,
      ):
        errors.append(
            f"events_v1.tmdl: '{kpi_prior_name}' must use "
            "CALCULATE(COALESCE(..., 0), ...)"
        )
    for m_name in (
        "Total Traces",
        "Total Sessions",
        "Total Users",
        "LLM Calls",
    ):
      if not re.search(
          rf"measure\s+'{re.escape(m_name)}'\s*=\s*DISTINCTCOUNTNOBLANK\(",
          events_tmdl,
      ):
        errors.append(
            f"events_v1.tmdl: '{m_name}' must use pure DISTINCTCOUNTNOBLANK(...)"
        )
    if "KEEPFILTERS(_top)" not in events_tmdl:
      errors.append("events_v1.tmdl: Top-N measures must use KEEPFILTERS(_top)")
    errors.extend(
        _check_measure_oracle_binding(
            events_tmdl, measure_oracle_queries, measure_oracle_col, looker
        )
    )
    if "PERCENTILE.INC(" not in events_tmdl:
      errors.append(
          "events_v1.tmdl: latency percentile measures missing PERCENTILE.INC"
      )

  # Check web configurator bundle (docs/pbip-bundle.js) when docs/ is present.
  bundle_path = pbi_root / "docs" / "pbip-bundle.js"
  if bundle_path.exists():
    bundle_text = _read_text(bundle_path)
    prefix = "window.BQAA_PBIP_BUNDLE = Object.freeze("
    suffix = ");\n"
    if "Apache License, Version 2.0" not in bundle_text:
      errors.append("docs/pbip-bundle.js: missing Apache-2.0 header")
    if prefix not in bundle_text or not bundle_text.endswith(suffix):
      errors.append("docs/pbip-bundle.js: malformed BQAA_PBIP_BUNDLE wrapper")
    else:
      json_body = bundle_text.split(prefix, 1)[1][: -len(suffix)]
      try:
        bundle_map = json.loads(json_body)
        if not isinstance(bundle_map, dict) or len(bundle_map) != 118:
          errors.append(
              "docs/pbip-bundle.js: expected 118 PBIP files in bundle"
          )
        elif (
            "BQAA_Agent_Analytics.SemanticModel/definition/expressions.tmdl"
            not in bundle_map
        ):
          errors.append(
              "docs/pbip-bundle.js: missing expressions.tmdl in bundle"
          )
      except ValueError as exc:
        errors.append(f"docs/pbip-bundle.js: invalid JSON payload: {exc}")

  return errors


# ---------------------------------------------------------------------------
# Drift check (delegate to generate_pbip --check)
# ---------------------------------------------------------------------------


def validate_drift():
  """Run generate_pbip.py --check to detect drift."""
  gen_path = POWER_BI_ROOT / "tools" / "generate_pbip.py"
  if not gen_path.exists():
    return ["generate_pbip.py not found — cannot check drift"]
  import subprocess

  result = subprocess.run(
      [sys.executable, str(gen_path), "--check"],
      cwd=str(REPO_ROOT),
      capture_output=True,
      text=True,
  )
  if result.returncode != 0:
    # Extract a concise summary from combined output.
    output = (result.stdout + "\n" + result.stderr).strip()
    summary_lines = output.splitlines()[:20]
    return ["Drift detected:\n" + "\n".join(summary_lines)]
  return []


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def run_validation(schemas_dir=None, skip_schemas=False):
  """Run all validation checks and return a list of errors."""
  all_errors = []

  if not skip_schemas:
    if schemas_dir:
      all_errors.extend(validate_schemas(schemas_dir))
    else:
      all_errors.append("Schema validation skipped (no --schemas-dir provided)")

  all_errors.extend(validate_tmdl())
  all_errors.extend(validate_parity())
  all_errors.extend(validate_drift())

  return all_errors


def main(argv=None):
  parser = argparse.ArgumentParser(
      description="Validate the generated Power BI project."
  )
  group = parser.add_mutually_exclusive_group()
  group.add_argument(
      "--schemas-dir",
      metavar="DIR",
      help="Directory containing pre-fetched schema files.",
  )
  group.add_argument(
      "--fetch-schemas",
      metavar="DIR",
      help="Fetch schema files from the pinned commit into DIR.",
  )
  group.add_argument(
      "--no-schemas",
      action="store_true",
      help="Skip JSON schema validation.",
  )
  args = parser.parse_args(argv)

  if args.fetch_schemas:
    err = fetch_schemas(args.fetch_schemas)
    if err:
      print(f"ERROR: {err}", file=sys.stderr)
      return 1
    # After fetching, run validation with the fetched directory.
    args.schemas_dir = args.fetch_schemas

  # Allow env var override for schema directory.
  if not args.schemas_dir and not args.no_schemas:
    env_dir = os.environ.get("BQAA_POWER_BI_SCHEMAS_DIR")
    if env_dir:
      args.schemas_dir = env_dir

  errors = run_validation(
      schemas_dir=args.schemas_dir,
      skip_schemas=args.no_schemas,
  )

  if errors:
    print("\n".join(f"FAIL: {e}" for e in errors), file=sys.stderr)
    return 1

  print("All validation checks passed.")
  return 0


if __name__ == "__main__":
  sys.exit(main())
