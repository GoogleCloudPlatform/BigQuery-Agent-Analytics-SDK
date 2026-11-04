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

"""CLI generator for the BQAA Power BI Project (PBIP)."""

from __future__ import annotations

import argparse
import datetime
import difflib
import importlib.util
import json
from pathlib import Path
import re
import sys
from types import ModuleType
from typing import Sequence
import zipfile

TOOLS_DIR = Path(__file__).resolve().parent

BUNDLE_REL_PATH = "docs/pbip-bundle.js"
BUNDLE_HEADER = """\
// Copyright 2026 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//     https://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

"""
BUNDLE_PREFIX = BUNDLE_HEADER + "window.BQAA_PBIP_BUNDLE = Object.freeze("
BUNDLE_SUFFIX = ");\n"
ZIP_FIXED_DATETIME = (2026, 1, 1, 0, 0, 0)
ZIP_ROOT_PREFIX = "BQAA_Agent_Analytics"


def _load_sibling_module(name: str) -> ModuleType | None:
  """Load a sibling module from dashboards/power_bi/tools/."""
  mod_name = f"power_bi_{name}"
  if mod_name in sys.modules:
    return sys.modules[mod_name]
  module_path = TOOLS_DIR / f"{name}.py"
  if not module_path.exists():
    return None
  spec = importlib.util.spec_from_file_location(mod_name, module_path)
  if spec is None or spec.loader is None:
    return None
  module = importlib.util.module_from_spec(spec)
  sys.modules[mod_name] = module
  spec.loader.exec_module(module)
  return module


def _require_sibling_module(name: str) -> ModuleType:
  """Load a sibling module the generator cannot produce correct output without."""
  module = _load_sibling_module(name)
  if module is None:
    raise FileNotFoundError(f"Missing required module: {TOOLS_DIR / name}.py")
  return module


pbip_common = _load_sibling_module("pbip_common")
assert (
    pbip_common is not None
), "pbip_common.py must exist alongside generate_pbip.py"

PBIP_SCHEMA = (
    "https://developer.microsoft.com/json-schemas/fabric/pbip/"
    "pbipProperties/1.0.0/schema.json"
)
PLATFORM_SCHEMA = (
    "https://developer.microsoft.com/json-schemas/fabric/gitIntegration/"
    "platformProperties/2.0.0/schema.json"
)
PBISM_SCHEMA = (
    "https://developer.microsoft.com/json-schemas/fabric/item/semanticModel/"
    "definitionProperties/1.0.0/schema.json"
)
PBIR_SCHEMA = (
    "https://developer.microsoft.com/json-schemas/fabric/item/report/"
    "definitionProperties/2.0.0/schema.json"
)

MODEL_FOLDER_NAME = "BQAA_Agent_Analytics.SemanticModel"
REPORT_FOLDER_NAME = "BQAA_Agent_Analytics.Report"

# Spec inputs the generator cannot run without.  A missing one is an error (or
# drift), never a reason to silently skip half of the generated project.
REQUIRED_SPEC_RELS = ("spec/semantic_model.yaml", "spec/report_visuals.yaml")


def build_core_files() -> dict[str, str]:
  """Build the root .pbip shortcut and item metadata (.platform, .pbism, .pbir)."""
  display_name = pbip_common.DISPLAY_NAME
  files: dict[str, str] = {}

  files["BQAA_Agent_Analytics.pbip"] = pbip_common.dump_json(
      {
          "$schema": PBIP_SCHEMA,
          "version": "1.0",
          "artifacts": [{"report": {"path": REPORT_FOLDER_NAME}}],
      }
  )

  files[f"{MODEL_FOLDER_NAME}/.platform"] = pbip_common.dump_json(
      {
          "$schema": PLATFORM_SCHEMA,
          "metadata": {
              "type": "SemanticModel",
              "displayName": display_name,
          },
          "config": {
              "version": "2.0",
              "logicalId": pbip_common.stable_uuid(
                  "platform", "SemanticModel", "BQAA_Agent_Analytics"
              ),
          },
      }
  )

  files[f"{MODEL_FOLDER_NAME}/definition.pbism"] = pbip_common.dump_json(
      {
          "$schema": PBISM_SCHEMA,
          "version": "4.0",
          "settings": {"qnaEnabled": False},
      }
  )

  files[f"{REPORT_FOLDER_NAME}/.platform"] = pbip_common.dump_json(
      {
          "$schema": PLATFORM_SCHEMA,
          "metadata": {
              "type": "Report",
              "displayName": display_name,
          },
          "config": {
              "version": "2.0",
              "logicalId": pbip_common.stable_uuid(
                  "platform", "Report", "BQAA_Agent_Analytics"
              ),
          },
      }
  )

  files[f"{REPORT_FOLDER_NAME}/definition.pbir"] = pbip_common.dump_json(
      {
          "$schema": PBIR_SCHEMA,
          "version": "4.0",
          "datasetReference": {"byPath": {"path": f"../{MODEL_FOLDER_NAME}"}},
      }
  )

  return files


def _normalize_item_key(rel_path: str, item_folder: str) -> str:
  clean = rel_path.strip().replace("\\", "/").lstrip("/")
  if clean.startswith(f"{item_folder}/"):
    return clean
  return f"{item_folder}/{clean}"


def build_pbip_bundle_js(pbip_files: dict[str, str]) -> str:
  """Build docs/pbip-bundle.js with the generated PBIP files for browser ZIP packaging."""
  ordered = {
      k: pbip_files[k] for k in sorted(pbip_files) if k != BUNDLE_REL_PATH
  }
  payload = json.dumps(ordered, indent=2, ensure_ascii=False)
  return f"{BUNDLE_PREFIX}{payload}{BUNDLE_SUFFIX}"


def rewrite_expressions_parameters(
    expr_text: str,
    *,
    project: str,
    dataset: str,
    table: str,
    billing_project: str | None = None,
    window_end: str = "auto",
    lookback_days: int = 90,
    use_storage_api: bool = True,
) -> str:
  """Validate and rewrite the 7 BQAA_* M parameter defaults in expressions.tmdl."""
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
  billing = (billing_project or "").strip() or project
  if not pbip_common.PROJECT_RE.fullmatch(billing):
    raise ValueError(f"Invalid BigQuery billing project ID: {billing!r}")

  win_clean = str(window_end or "auto").strip()
  if win_clean.lower() == "auto":
    win_clean = "auto"
  else:
    if not re.fullmatch(r"^\d{4}-\d{2}-\d{2}$", win_clean):
      raise ValueError(
          f"Invalid window end date (expected 'auto' or YYYY-MM-DD): {window_end!r}"
      )
    try:
      datetime.date.fromisoformat(win_clean)
    except ValueError as exc:
      raise ValueError(
          f"Invalid calendar date for window end '{window_end}': {exc}"
      ) from exc

  if (
      isinstance(lookback_days, bool)
      or not isinstance(lookback_days, int)
      or lookback_days < 1
      or lookback_days > 366
  ):
    raise ValueError(
        f"Lookback days must be an integer in 1..366, got {lookback_days!r}"
    )
  if not isinstance(use_storage_api, bool):
    raise ValueError(f"use_storage_api must be a bool, got {use_storage_api!r}")

  replacements = {
      "BQAA_ProjectId": f'"{project}"',
      "BQAA_DatasetId": f'"{dataset}"',
      "BQAA_TableId": f'"{table}"',
      "BQAA_BillingProjectId": f'"{billing}"',
      "BQAA_WindowEndDate": f'"{win_clean}"',
      "BQAA_LookbackDays": str(lookback_days),
      "BQAA_UseStorageApi": "true" if use_storage_api else "false",
  }
  updated = expr_text
  # Pre-pass: count every declaration header independently of the right-hand
  # side and metadata, so a metadata-free duplicate (e.g.
  # `expression BQAA_ProjectId = "stale"`) is caught before the annotated
  # rewrite runs.  TMDL comment lines are skipped.
  non_comment_text = "\n".join(
      ln
      for ln in updated.splitlines()
      if not ln.lstrip().startswith("///") and not ln.lstrip().startswith("//")
  )
  for param_name in replacements:
    decl_re = re.compile(
        rf"^[ \t]*expression[ \t]+(?:'{re.escape(param_name)}'|{re.escape(param_name)})[ \t]*=",
        re.MULTILINE,
    )
    found = len(decl_re.findall(non_comment_text))
    if found != 1:
      raise ValueError(
          f"Expected exactly 1 declaration for {param_name} in"
          f" expressions.tmdl, found {found}"
      )
  for param_name, literal_val in replacements.items():
    pattern = re.compile(
        rf"^(expression {re.escape(param_name)} = ).*?( meta \[IsParameterQuery=true, .*\])$",
        re.MULTILINE,
    )
    updated, count = pattern.subn(rf"\g<1>{literal_val}\g<2>", updated)
    if count != 1:
      raise ValueError(
          f"Expected exactly 1 declaration for {param_name} in expressions.tmdl, found {count}"
      )
  return updated


def write_pbip_zip(
    output_zip: Path | str,
    *,
    project: str = "test-project-0728-467323",
    dataset: str = "bqaa_fixture_adk_1_27_0",
    table: str = "agent_events",
    billing_project: str | None = None,
    window_end: str = "auto",
    lookback_days: int = 90,
    use_storage_api: bool = True,
    power_bi_root: Path | None = None,
    looker_root: Path | None = None,
) -> Path:
  """Generate a deterministic, pre-configured PBIP .zip archive."""
  all_files = generate_all(power_bi_root=power_bi_root, looker_root=looker_root)
  pbip_files = {k: v for k, v in all_files.items() if k != BUNDLE_REL_PATH}
  expr_key = f"{MODEL_FOLDER_NAME}/definition/expressions.tmdl"
  # Fail before the destination is touched if the generated project lacks a
  # file the archive cannot be correct without.  The parameter defaults are
  # rewritten in expressions.tmdl, so a project without it must not be zipped
  # as if it had been configured.
  missing_files = sorted(
      (set(build_core_files()) | {expr_key}) - set(pbip_files)
  )
  if missing_files:
    raise FileNotFoundError(
        "Generated project is missing required file(s): "
        + ", ".join(missing_files)
    )
  pbip_files[expr_key] = rewrite_expressions_parameters(
      pbip_files[expr_key],
      project=project,
      dataset=dataset,
      table=table,
      billing_project=billing_project,
      window_end=window_end,
      lookback_days=lookback_days,
      use_storage_api=use_storage_api,
  )

  out_path = Path(output_zip)
  out_path.parent.mkdir(parents=True, exist_ok=True)
  with zipfile.ZipFile(
      out_path, mode="w", compression=zipfile.ZIP_DEFLATED
  ) as zf:
    for rel_path in sorted(pbip_files):
      arcname = f"{ZIP_ROOT_PREFIX}/{rel_path}"
      info = zipfile.ZipInfo(filename=arcname, date_time=ZIP_FIXED_DATETIME)
      info.compress_type = zipfile.ZIP_DEFLATED
      info.external_attr = 0o644 << 16
      normalized = (
          pbip_files[rel_path].replace("\r\n", "\n").replace("\r", "\n")
      )
      zf.writestr(info, normalized.encode("utf-8"))
  return out_path


def generate_all(
    power_bi_root: Path | None = None,
    looker_root: Path | None = None,
    *,
    enforce_oracle_lock: bool = True,
) -> dict[str, str]:
  """Generate the complete PBIP file map (relative to power_bi_root -> text).

  Args:
    power_bi_root: The dashboards/power_bi directory (default: this checkout).
    looker_root: The dashboards/looker_studio directory (default: this
      checkout).
    enforce_oracle_lock: Refuse to generate when the cited oracle SQL differs
      from the reviewed lock (spec/oracle_lock.json).  Only the drift check and
      the explicit --accept-oracle-changes flow turn this off.

  Returns:
    Relative path -> file text for every generated file.

  Raises:
    FileNotFoundError: A required spec is missing.
    OracleLockError: The oracle SQL no longer matches the reviewed lock.
  """
  pbi_root = Path(
      power_bi_root if power_bi_root is not None else pbip_common.POWER_BI_ROOT
  )
  lkr_root = Path(
      looker_root if looker_root is not None else pbip_common.LOOKER_ROOT
  )

  for spec_rel in REQUIRED_SPEC_RELS:
    spec_path = pbi_root / spec_rel
    if not spec_path.is_file():
      raise FileNotFoundError(f"Missing required spec: {spec_path}")
  pbip_model = _require_sibling_module("pbip_model")
  pbip_report = _require_sibling_module("pbip_report")

  contracts = pbip_common.load_contracts(looker_root=lkr_root)
  files = build_core_files()

  model_spec = pbip_common.load_spec(
      "semantic_model.yaml", power_bi_root=pbi_root
  )
  model_files = pbip_model.build_model(contracts, model_spec)
  if enforce_oracle_lock:
    lock_problems = pbip_common.oracle_lock_problems(
        pbi_root, contracts, model_spec
    )
    if lock_problems:
      raise pbip_common.OracleLockError(lock_problems)
  for k in sorted(model_files):
    norm_k = _normalize_item_key(k, MODEL_FOLDER_NAME)
    files[norm_k] = model_files[k]

  visuals_spec = pbip_common.load_spec(
      "report_visuals.yaml", power_bi_root=pbi_root
  )
  report_files = pbip_report.build_report(contracts, visuals_spec, model_spec)
  for k in sorted(report_files):
    norm_k = _normalize_item_key(k, REPORT_FOLDER_NAME)
    files[norm_k] = report_files[k]

  files[BUNDLE_REL_PATH] = build_pbip_bundle_js(files)

  return {k: files[k] for k in sorted(files)}


def _list_existing_item_files(pbi_root: Path) -> set[str]:
  """List existing files under the two generated item folders (ignoring .pbi/)."""
  found: set[str] = set()
  pbip_file = pbi_root / "BQAA_Agent_Analytics.pbip"
  if pbip_file.is_file():
    found.add("BQAA_Agent_Analytics.pbip")
  bundle_file = pbi_root / BUNDLE_REL_PATH
  if bundle_file.is_file():
    found.add(BUNDLE_REL_PATH)

  for folder_name in (MODEL_FOLDER_NAME, REPORT_FOLDER_NAME):
    item_dir = pbi_root / folder_name
    if not item_dir.exists():
      continue
    for path in sorted(item_dir.rglob("*")):
      if not path.is_file():
        continue
      rel = path.relative_to(pbi_root).as_posix()
      if ".pbi" in rel.split("/"):
        continue
      found.add(rel)
  return found


def oracle_lock_problems(
    power_bi_root: Path | None = None,
    looker_root: Path | None = None,
) -> list[str]:
  """Compare the cited oracle SQL with the reviewed lock; empty when equal."""
  pbi_root = Path(
      power_bi_root if power_bi_root is not None else pbip_common.POWER_BI_ROOT
  )
  lkr_root = Path(
      looker_root if looker_root is not None else pbip_common.LOOKER_ROOT
  )
  contracts = pbip_common.load_contracts(looker_root=lkr_root)
  model_spec = pbip_common.load_spec(
      "semantic_model.yaml", power_bi_root=pbi_root
  )
  return pbip_common.oracle_lock_problems(pbi_root, contracts, model_spec)


def accept_oracle_changes(
    power_bi_root: Path | None = None,
    looker_root: Path | None = None,
) -> list[str]:
  """Record the cited oracle SQL as reviewed; return what the lock gained.

  This is the only code path that writes spec/oracle_lock.json.  It runs after
  the rest of the model has been generated in memory, so a model that does not
  build can never be locked in.

  Args:
    power_bi_root: The dashboards/power_bi directory (default: this checkout).
    looker_root: The dashboards/looker_studio directory (default: this
      checkout).

  Returns:
    One line per lock entry that changed (empty when nothing changed).
  """
  pbi_root = Path(
      power_bi_root if power_bi_root is not None else pbip_common.POWER_BI_ROOT
  )
  lkr_root = Path(
      looker_root if looker_root is not None else pbip_common.LOOKER_ROOT
  )
  generate_all(
      power_bi_root=pbi_root, looker_root=lkr_root, enforce_oracle_lock=False
  )
  contracts = pbip_common.load_contracts(looker_root=lkr_root)
  model_spec = pbip_common.load_spec(
      "semantic_model.yaml", power_bi_root=pbi_root
  )
  current = pbip_common.current_oracle_digests(contracts, model_spec)
  lock_path = pbi_root / pbip_common.ORACLE_LOCK_REL
  changes = oracle_lock_problems(pbi_root, lkr_root)
  lock_path.parent.mkdir(parents=True, exist_ok=True)
  with open(lock_path, "w", encoding="utf-8", newline="\n") as f:
    f.write(pbip_common.render_oracle_lock(current))
  return changes


def write_generated_files(
    power_bi_root: Path | None = None,
    looker_root: Path | None = None,
    *,
    accept_oracle: bool = False,
) -> dict[str, str]:
  """Write all generated files and remove stale files under item folders.

  Args:
    power_bi_root: The dashboards/power_bi directory (default: this checkout).
    looker_root: The dashboards/looker_studio directory (default: this
      checkout).
    accept_oracle: Re-record the reviewed oracle lock first.  Without it the
      write refuses to run when the oracle SQL differs from the lock.

  Returns:
    Relative path -> text for every file written.
  """
  pbi_root = Path(
      power_bi_root if power_bi_root is not None else pbip_common.POWER_BI_ROOT
  )
  if accept_oracle:
    accept_oracle_changes(power_bi_root=pbi_root, looker_root=looker_root)
  expected = generate_all(power_bi_root=pbi_root, looker_root=looker_root)
  existing = _list_existing_item_files(pbi_root)

  # Remove stale files under the generated item folders.
  for stale_rel in sorted(existing - set(expected)):
    stale_path = pbi_root / stale_rel
    if stale_path.is_file():
      stale_path.unlink()

  # Write expected files with LF line endings.
  for rel_path, content in expected.items():
    target = pbi_root / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    with open(target, "w", encoding="utf-8", newline="\n") as f:
      f.write(normalized)

  return expected


def check_generated_files(
    power_bi_root: Path | None = None,
    looker_root: Path | None = None,
) -> list[str]:
  """Compare generated files against disk (CRLF normalized to LF). Return diff summaries."""
  pbi_root = Path(
      power_bi_root if power_bi_root is not None else pbip_common.POWER_BI_ROOT
  )
  missing_specs = [
      f"MISSING: {spec_rel}"
      for spec_rel in REQUIRED_SPEC_RELS
      if not (pbi_root / spec_rel).is_file()
  ]
  if missing_specs:
    return missing_specs

  # The reviewed oracle lock comes first so its findings survive the cap on
  # the number of reported differences below.
  diffs: list[str] = oracle_lock_problems(pbi_root, looker_root)

  expected = generate_all(
      power_bi_root=pbi_root, looker_root=looker_root, enforce_oracle_lock=False
  )
  existing = _list_existing_item_files(pbi_root)

  for missing_rel in sorted(set(expected) - existing):
    diffs.append(f"MISSING: {missing_rel}")

  for extra_rel in sorted(existing - set(expected)):
    diffs.append(f"EXTRA: {extra_rel}")

  for rel_path in sorted(set(expected) & existing):
    disk_path = pbi_root / rel_path
    disk_text = (
        disk_path.read_text(encoding="utf-8")
        .replace("\r\n", "\n")
        .replace("\r", "\n")
    )
    exp_text = expected[rel_path].replace("\r\n", "\n").replace("\r", "\n")
    if disk_text != exp_text:
      udiff = list(
          difflib.unified_diff(
              exp_text.splitlines(keepends=True),
              disk_text.splitlines(keepends=True),
              fromfile=f"expected/{rel_path}",
              tofile=f"actual/{rel_path}",
              n=2,
          )
      )
      snippet = "".join(udiff[:20]).rstrip()
      diffs.append(f"MISMATCH: {rel_path}\n{snippet}")

  return diffs[:20]


def main(argv: Sequence[str] | None = None) -> int:
  """Run the PBIP generator CLI."""
  parser = argparse.ArgumentParser(
      description="Generate or verify the BQAA Power BI Project (PBIP)."
  )
  mode = parser.add_mutually_exclusive_group(required=True)
  mode.add_argument(
      "--write",
      action="store_true",
      help="Write generated PBIP files to disk.",
  )
  mode.add_argument(
      "--check",
      action="store_true",
      help="Check that committed PBIP files match generator output.",
  )
  mode.add_argument(
      "--print-sql",
      action="store_true",
      help="Print the rendered events_v1 BigQuery SQL for the given date window.",
  )
  mode.add_argument(
      "--output-zip",
      help="Write a pre-configured PBIP .zip archive to the given file path.",
  )
  parser.add_argument(
      "--window-end",
      help="Anchor end date in YYYY-MM-DD format (or 'auto' for --output-zip).",
  )
  parser.add_argument(
      "--lookback-days",
      type=int,
      default=90,
      help="Lookback window L in days (1..366, default: 90).",
  )
  parser.add_argument(
      "--project",
      default="test-project-0728-467323",
      help="BigQuery project ID.",
  )
  parser.add_argument(
      "--dataset",
      default="bqaa_fixture_adk_1_27_0",
      help="BigQuery dataset ID.",
  )
  parser.add_argument(
      "--table",
      default="agent_events",
      help="BigQuery table ID.",
  )
  parser.add_argument(
      "--billing-project",
      default="",
      help="BigQuery billing project ID (defaults to --project).",
  )
  parser.add_argument(
      "--use-storage-api",
      choices=("true", "false"),
      default="true",
      help="Whether GoogleBigQuery.Database uses the Storage Read API (default: true).",
  )
  parser.add_argument(
      "--accept-oracle-changes",
      action="store_true",
      help=(
          "With --write: record the current oracle SQL digests in"
          f" {pbip_common.ORACLE_LOCK_REL} after reviewing that the DAX"
          " measures still match them. Without this flag, --write and --check"
          " fail when a cited oracle query differs from the lock."
      ),
  )
  args = parser.parse_args(argv)

  if args.accept_oracle_changes and not args.write:
    parser.error("--accept-oracle-changes is only valid with --write")

  if args.print_sql:
    pbip_model = _require_sibling_module("pbip_model")
    if not args.window_end:
      parser.error("--print-sql requires --window-end YYYY-MM-DD")
    try:
      anchor = datetime.date.fromisoformat(args.window_end)
    except ValueError as exc:
      parser.error(f"Invalid --window-end '{args.window_end}': {exc}")
    lookback = int(args.lookback_days)
    if lookback < 1 or lookback > 366:
      parser.error(f"--lookback-days must be in 1..366, got {lookback}")
    current_start = anchor - datetime.timedelta(days=lookback - 1)
    load_start = current_start - datetime.timedelta(days=lookback)
    try:
      sql = pbip_model.render_events_sql(
          args.project,
          args.dataset,
          args.table,
          load_start=load_start,
          anchor=anchor,
      )
    except ValueError as exc:
      parser.error(str(exc))
    sys.stdout.write(sql)
    if not sql.endswith("\n"):
      sys.stdout.write("\n")
    return 0

  try:
    if args.output_zip:
      write_pbip_zip(
          args.output_zip,
          project=args.project,
          dataset=args.dataset,
          table=args.table,
          billing_project=args.billing_project or args.project,
          window_end=args.window_end or "auto",
          lookback_days=int(args.lookback_days),
          use_storage_api=str(args.use_storage_api).lower() == "true",
      )
      return 0
    if args.write:
      if args.accept_oracle_changes:
        changes = accept_oracle_changes()
        sys.stdout.write(
            f"Re-recorded {pbip_common.ORACLE_LOCK_REL}"
            f" ({len(changes)} change(s)).\n"
        )
        for change in changes:
          sys.stdout.write(f"  {change}\n")
      write_generated_files()
      return 0
    if args.check:
      diffs = check_generated_files()
      if diffs:
        sys.stderr.write(f"PBIP drift detected ({len(diffs)} difference(s)):\n")
        for item in diffs:
          sys.stderr.write(f"{item}\n")
        if any(item.startswith("ORACLE LOCK:") for item in diffs):
          sys.stderr.write(f"{pbip_common.ORACLE_LOCK_HINT}\n")
        return 1
      return 0
  except Exception as exc:  # pylint: disable=broad-except
    sys.stderr.write(f"Error: {exc}\n")
    return 1

  return 0


if __name__ == "__main__":
  sys.exit(main())
