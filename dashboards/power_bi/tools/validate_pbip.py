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

# The 29 event columns (spec.md §4.3, F1).
EVENTS_COLUMNS = [
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
]

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

# The 49 measure names (spec.md §4.5, plan.md §2).
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
]

# F4 listener exceptions: the 4 charts that do NOT listen to User ID.
NON_UID_LISTENERS = frozenset(
    {
        "usage-top-5-users-with-most-tokens-consumption",
        "usage-token-usage-split-by-agent",
        "usage-top-5-users-with-most-traces",
        "usage-traces-split-by-agent",
    }
)

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


def _load_looker_contracts():
  """Load chart_manifest.yaml and product_contract.yaml."""
  import yaml

  manifest_path = LOOKER_ROOT / "spec" / "chart_manifest.yaml"
  product_path = LOOKER_ROOT / "spec" / "product_contract.yaml"
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


def validate_tmdl(power_bi_root=None):
  """Run custom TMDL lint checks."""
  errors = []
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

  # Check database.tmdl and model.tmdl exist.
  if "database.tmdl" not in tmdl_files:
    errors.append("Missing database.tmdl")
  if "model.tmdl" not in tmdl_files:
    errors.append("Missing model.tmdl")

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

  # Validate events_v1.tmdl columns and measures.
  events_path = tables_dir / "events_v1.tmdl"
  if events_path.exists():
    events_text = _read_text(events_path)
    # Check all 29 columns present.
    for col in EVENTS_COLUMNS:
      if f"sourceColumn={col}" not in events_text:
        errors.append(f"events_v1.tmdl: missing column '{col}'")
    # Check column data types.
    for m in re.finditer(r"sourceColumn=(\S+)\s+dataType=(\S+)", events_text):
      col_name, dtype = m.group(1), m.group(2)
      if dtype not in VALID_DATA_TYPES:
        errors.append(
            f"events_v1.tmdl: column '{col_name}' has invalid "
            f"dataType '{dtype}'"
        )
    # No DISTINCTCOUNT( (only DISTINCTCOUNTNOBLANK).
    if "DISTINCTCOUNT(" in events_text:
      errors.append(
          "events_v1.tmdl: found DISTINCTCOUNT( — use " "DISTINCTCOUNTNOBLANK"
      )
    # Check all 49 measures present.
    for measure in MEASURE_NAMES:
      if not re.search(
          rf"^\tmeasure\s+(?:'{re.escape(measure)}'|{re.escape(measure)})\s*=",
          events_text,
          re.MULTILINE,
      ):
        errors.append(f"events_v1.tmdl: missing measure '{measure}'")
    # Check measure descriptions cite oracle files.
    for m in re.finditer(r"///.*?oracle/queries/(\S+\.sql)", events_text):
      oracle_ref = m.group(1)
      oracle_path = LOOKER_ROOT / "oracle" / "queries" / oracle_ref
      if not oracle_path.exists():
        errors.append(
            f"events_v1.tmdl: references non-existent oracle "
            f"file '{oracle_ref}'"
        )
    # I8: no ; or " or # in embedded SQL.
    # Look for Value.NativeQuery SQL lines in both events_v1.tmdl
    # and expressions.tmdl.
    expr_text = tmdl_files.get("expressions.tmdl", "")
    for scan_text in (events_text, expr_text):
      for m in re.finditer(r'EventsSql\s*=\s*"([^"]*(?:""[^"]*)*)"', scan_text):
        sql_text = m.group(1).replace('""', '"')
        if ";" in sql_text:
          errors.append(f"TMDL: EventsSql contains ';'")
        if "#(" in sql_text.replace("#(#)(", ""):
          errors.append(f"TMDL: EventsSql contains unescaped '#('")

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
  expr_text = tmdl_files.get("expressions.tmdl", "")
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


def validate_parity(power_bi_root=None):
  """Check chart_manifest ↔ visuals ↔ measures ↔ oracle parity."""
  errors = []
  manifest, product = _load_looker_contracts()
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
    oracle_path = LOOKER_ROOT / "oracle" / "queries" / f"{chart_id}.sql"
    if not oracle_path.exists():
      errors.append(f"Oracle file missing for chart '{chart_id}'")

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
  if sm_path.exists():
    import yaml

    sm_data = yaml.safe_load(_read_text(sm_path))
    for m_entry in sm_data.get("measures", []):
      if isinstance(m_entry, dict) and m_entry.get("name"):
        measure_oracle_col[m_entry["name"]] = m_entry.get("oracle_column", "")

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
        else:
          projs = qstate.get("Y", {}).get("projections", [])

        oracle_sql_path = LOOKER_ROOT / "oracle" / "queries" / f"{vname}.sql"
        oracle_sql_text = (
            _read_text(oracle_sql_path) if oracle_sql_path.exists() else ""
        )
        for p in projs:
          m_prop = p.get("field", {}).get("Measure", {}).get(
              "Property"
          ) or p.get("nativeQueryRef", "")
          ocol = measure_oracle_col.get(m_prop, "")
          if (
              ocol
              and oracle_sql_text
              and not re.search(rf"\b{re.escape(ocol)}\b", oracle_sql_text)
          ):
            errors.append(
                f"Visual '{vname}' measure '{m_prop}' oracle_column '{ocol}' "
                f"not found in {oracle_sql_path.name}"
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
