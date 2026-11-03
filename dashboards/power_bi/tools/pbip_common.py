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

"""Shared constants, contract loading, deterministic UUIDs, and layout helpers."""

from collections.abc import Iterable, Mapping
import json
from pathlib import Path
import re
from typing import Any
import uuid

import yaml

REPO_ROOT: Path = Path(__file__).resolve().parents[3]
POWER_BI_ROOT: Path = REPO_ROOT / "dashboards" / "power_bi"
LOOKER_ROOT: Path = REPO_ROOT / "dashboards" / "looker_studio"
MODEL_DIR: Path = POWER_BI_ROOT / "BQAA_Agent_Analytics.SemanticModel"
REPORT_DIR: Path = POWER_BI_ROOT / "BQAA_Agent_Analytics.Report"
DISPLAY_NAME: str = "BQAA Agent Analytics"

PBIP_NAMESPACE: uuid.UUID = uuid.uuid5(
    uuid.NAMESPACE_URL,
    "https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/tree/main/dashboards/power_bi",
)

# Identifier validation regexes (F20).
PROJECT_RE: re.Pattern[str] = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
DATASET_RE: re.Pattern[str] = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,1023}$")
TABLE_RE: re.Pattern[str] = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_-]{0,1023}$")

# F4 User ID non-listening chart IDs (33/37 listen; these 4 do not).
NON_UID_LISTENERS: frozenset[str] = frozenset(
    {
        "usage-top-5-users-with-most-tokens-consumption",
        "usage-token-usage-split-by-agent",
        "usage-top-5-users-with-most-traces",
        "usage-traces-split-by-agent",
    }
)

# Canvas layout constants (spec §5.2).
CANVAS_PAGE_WIDTH: int = 1280
CANVAS_MARGIN: int = 16
CANVAS_GUTTER: int = 8
CANVAS_COLS: int = 24
CANVAS_UNIT_WIDTH: int = (
    CANVAS_PAGE_WIDTH - 2 * CANVAS_MARGIN
) // CANVAS_COLS  # 52
CANVAS_ROW_HEIGHT: int = 40
CANVAS_BODY_TOP: int = 128
CANVAS_MIN_HEIGHT: int = 720
CANVAS_BOTTOM_PADDING: int = 24

HEADER_RECTS: dict[str, dict[str, int]] = {
    "page-heading": {"x": 16, "y": 8, "width": 640, "height": 48},
    "slicer-date": {"x": 16, "y": 56, "width": 280, "height": 56},
    "slicer-agent": {"x": 304, "y": 56, "width": 232, "height": 56},
    "slicer-user-id": {"x": 544, "y": 56, "width": 232, "height": 56},
    "slicer-trace-id": {"x": 784, "y": 56, "width": 232, "height": 56},
    "slicer-span-id": {"x": 1024, "y": 56, "width": 232, "height": 56},
}


class OracleFilesDict(dict[str, str]):
  """Mapping of oracle query path -> SQL text with flexible key lookup."""

  @staticmethod
  def _normalize_key(key: object) -> str:
    if not isinstance(key, str):
      return ""
    clean = key.strip().replace("\\", "/")
    if clean.startswith("oracle/queries/"):
      stem = clean[len("oracle/queries/") :]
    else:
      stem = clean
    if stem.endswith(".sql"):
      stem = stem[:-4]
    return f"oracle/queries/{stem}.sql"

  def __contains__(self, key: object) -> bool:
    if super().__contains__(key):
      return True
    return super().__contains__(self._normalize_key(key))

  def __getitem__(self, key: str) -> str:
    if super().__contains__(key):
      return super().__getitem__(key)
    norm = self._normalize_key(key)
    if super().__contains__(norm):
      return super().__getitem__(norm)
    raise KeyError(key)

  def get(self, key: str, default: Any = None) -> Any:
    try:
      return self[key]
    except KeyError:
      return default


class Contracts:
  """Loaded Looker Studio contracts consumed by the Power BI generator."""

  def __init__(
      self,
      manifest: dict[str, Any],
      product: dict[str, Any],
      events_template: str,
      oracle_files: OracleFilesDict,
  ) -> None:
    self.manifest = manifest
    self.product = product
    self.events_template = events_template
    self.oracle_files = oracle_files


def _product_charts_by_id(
    product: Mapping[str, Any],
) -> dict[str, Mapping[str, Any]]:
  """Normalize product_contract.yaml#charts (list or mapping) into id -> entry."""
  raw = product.get("charts")
  if isinstance(raw, Mapping):
    return {str(k): v for k, v in raw.items() if isinstance(v, Mapping)}
  if isinstance(raw, list):
    by_id: dict[str, Mapping[str, Any]] = {}
    for entry in raw:
      if not isinstance(entry, Mapping) or "id" not in entry:
        raise ValueError("product_contract.yaml#charts entry missing 'id'")
      cid = str(entry["id"])
      if cid in by_id:
        raise ValueError(f"Duplicate chart id '{cid}' in product_contract.yaml")
      by_id[cid] = entry
    return by_id
  raise ValueError("product_contract.yaml missing 'charts' list or mapping")


def validate_contracts(contracts: Contracts) -> None:
  """Validate Looker Studio contracts fail-closed at generation time."""
  manifest = contracts.manifest
  product = contracts.product
  if not isinstance(manifest, dict):
    raise ValueError("chart_manifest.yaml must parse to a mapping")
  if not isinstance(product, dict):
    raise ValueError("product_contract.yaml must parse to a mapping")

  charts = manifest.get("charts")
  if not isinstance(charts, list) or len(charts) != 37:
    count = len(charts) if isinstance(charts, list) else 0
    raise ValueError(f"Expected 37 charts in chart_manifest.yaml, got {count}")

  product_charts = _product_charts_by_id(product)

  seen_ids: set[str] = set()
  seen_titles: set[str] = set()
  non_uid_from_manifest: set[str] = set()

  for chart in charts:
    if not isinstance(chart, Mapping):
      raise ValueError("Chart entry in chart_manifest.yaml must be a mapping")
    chart_id = chart.get("id")
    if not isinstance(chart_id, str) or not chart_id:
      raise ValueError("Chart entry missing string 'id'")
    if len(chart_id) > 50:
      raise ValueError(
          f"Chart id '{chart_id}' exceeds 50 characters ({len(chart_id)})"
      )
    if chart_id in seen_ids:
      raise ValueError(f"Duplicate chart id '{chart_id}'")
    seen_ids.add(chart_id)

    oracle_ref = chart.get("oracle_query", f"oracle/queries/{chart_id}.sql")
    if oracle_ref not in contracts.oracle_files:
      raise ValueError(
          f"Missing oracle query '{oracle_ref}' for chart '{chart_id}'"
      )

    pchart = product_charts.get(chart_id)
    if not isinstance(pchart, Mapping):
      raise ValueError(
          f"Chart '{chart_id}' missing from product_contract.yaml#charts"
      )
    title = pchart.get("title")
    if not isinstance(title, str) or not title.strip():
      raise ValueError(
          f"Chart '{chart_id}' missing non-empty product contract title"
      )
    if title in seen_titles:
      raise ValueError(f"Duplicate product contract chart title '{title}'")
    seen_titles.add(title)

    listen = chart.get("listen")
    if not isinstance(listen, Mapping):
      raise ValueError(f"Chart '{chart_id}' missing 'listen' mapping")
    listens_uid = bool(
        listen.get("User ID")
        or listen.get("user_id")
        or "agent_events.user_id" in listen.values()
    )
    if not listens_uid:
      non_uid_from_manifest.add(chart_id)

  if len(seen_titles) != 37:
    raise ValueError(
        f"Expected 37 unique product titles, got {len(seen_titles)}"
    )

  if non_uid_from_manifest != NON_UID_LISTENERS:
    raise ValueError(
        "F4 User ID listener exception set mismatch: "
        f"expected {sorted(NON_UID_LISTENERS)}, "
        f"got {sorted(non_uid_from_manifest)}"
    )

  if (
      "SELECT" not in contracts.events_template
      or "FROM `{{PROJECT}}.{{DATASET}}.{{TABLE}}`"
      not in contracts.events_template
  ):
    raise ValueError(
        "events_v1.sql.tmpl missing SELECT or FROM placeholder line"
    )


def load_contracts(looker_root: Path | str | None = None) -> Contracts:
  """Load and validate the Looker Studio contracts."""
  root = Path(looker_root) if looker_root is not None else Path(LOOKER_ROOT)
  manifest_path = root / "spec" / "chart_manifest.yaml"
  product_path = root / "spec" / "product_contract.yaml"
  template_path = root / "sql" / "events_v1.sql.tmpl"
  oracle_dir = root / "oracle" / "queries"

  with open(manifest_path, "r", encoding="utf-8") as f:
    manifest = yaml.safe_load(f)
  with open(product_path, "r", encoding="utf-8") as f:
    product = yaml.safe_load(f)
  with open(template_path, "r", encoding="utf-8") as f:
    events_template = f.read()

  oracle_files = OracleFilesDict()
  if oracle_dir.is_dir():
    for sql_file in sorted(oracle_dir.glob("*.sql")):
      rel_key = f"oracle/queries/{sql_file.name}"
      oracle_files[rel_key] = sql_file.read_text(encoding="utf-8")

  contracts = Contracts(
      manifest=manifest,
      product=product,
      events_template=events_template,
      oracle_files=oracle_files,
  )
  validate_contracts(contracts)
  return contracts


def load_spec(
    name: str, power_bi_root: Path | str | None = None
) -> dict[str, Any]:
  """Load a YAML or JSON spec file from dashboards/power_bi/spec/."""
  root = (
      Path(power_bi_root) if power_bi_root is not None else Path(POWER_BI_ROOT)
  )
  spec_dir = root / "spec"
  clean = name.strip()
  if clean.startswith("spec/"):
    clean = clean[len("spec/") :]
  candidate = spec_dir / clean
  if not candidate.exists():
    if not clean.endswith((".yaml", ".yml", ".json")):
      yaml_candidate = spec_dir / f"{clean}.yaml"
      json_candidate = spec_dir / f"{clean}.json"
      if yaml_candidate.exists():
        candidate = yaml_candidate
      elif json_candidate.exists():
        candidate = json_candidate
  with open(candidate, "r", encoding="utf-8") as f:
    if candidate.suffix == ".json":
      data = json.load(f)
    else:
      data = yaml.safe_load(f)
  if not isinstance(data, dict):
    raise ValueError(f"Spec '{name}' did not parse to a dictionary")
  return data


def stable_uuid(kind: str, *parts: str) -> str:
  """Generate a deterministic UUIDv5 for the given kind and parts."""
  if not kind:
    raise ValueError("kind must be non-empty")
  key = "|".join((str(kind), *(str(p) for p in parts)))
  return str(uuid.uuid5(PBIP_NAMESPACE, key))


def dump_json(obj: Any) -> str:
  """Serialize obj to deterministic 2-space indented JSON with '$schema' first."""
  if isinstance(obj, Mapping) and "$schema" in obj:
    ordered: dict[str, Any] = {"$schema": obj["$schema"]}
    for k, v in obj.items():
      if k != "$schema":
        ordered[k] = v
    obj = ordered
  return json.dumps(obj, indent=2, ensure_ascii=False) + "\n"


def literal(text: str) -> dict[str, Any]:
  """Build a PBIR single-quoted string Literal expression with ' -> ''."""
  escaped = str(text).replace("'", "''")
  return {"expr": {"Literal": {"Value": f"'{escaped}'"}}}


def canvas_rect(geometry: Mapping[str, int]) -> dict[str, int]:
  """Convert a 24-column manifest geometry (row, col, width, height) to pixels."""
  row = int(geometry["row"])
  col = int(geometry["col"])
  width = int(geometry["width"])
  height = int(geometry["height"])
  if row < 0:
    raise ValueError(f"Invalid row {row}: must be >= 0")
  if col < 0 or col >= CANVAS_COLS:
    raise ValueError(f"Invalid col {col}: must be in 0..{CANVAS_COLS - 1}")
  if width <= 0 or col + width > CANVAS_COLS:
    raise ValueError(
        f"Invalid width {width} at col {col}: must fit in {CANVAS_COLS} columns"
    )
  if height <= 0:
    raise ValueError(f"Invalid height {height}: must be > 0")
  return {
      "x": CANVAS_MARGIN + CANVAS_UNIT_WIDTH * col,
      "y": CANVAS_BODY_TOP + CANVAS_ROW_HEIGHT * row,
      "width": CANVAS_UNIT_WIDTH * width - CANVAS_GUTTER,
      "height": CANVAS_ROW_HEIGHT * height - CANVAS_GUTTER,
  }


def page_height(rects: Iterable[Mapping[str, int]]) -> int:
  """Compute page height PH = max(720, ceil8(max(y + h) + 24))."""
  max_bottom = 0
  for r in rects:
    y = int(r["y"])
    h = int(r["height"] if "height" in r else r["h"])
    bottom = y + h
    if bottom > max_bottom:
      max_bottom = bottom
  if max_bottom == 0:
    return CANVAS_MIN_HEIGHT
  needed = max_bottom + CANVAS_BOTTOM_PADDING
  ceil8 = ((needed + 7) // 8) * 8
  return max(CANVAS_MIN_HEIGHT, ceil8)
