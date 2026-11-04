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
import hashlib
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

# Identifier validation regexes (F20).  `\Z` rather than `$` so that a trailing
# newline is never accepted, even by a caller that uses `.match()`; callers
# should still prefer `.fullmatch()`.
PROJECT_RE: re.Pattern[str] = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]\Z")
DATASET_RE: re.Pattern[str] = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,1023}\Z")
TABLE_RE: re.Pattern[str] = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_-]{0,1023}\Z")

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


def oracle_digest(
    oracle_files: Mapping[str, str], oracle_queries: Iterable[str]
) -> str:
  """Return a short stable sha256 over the cited oracle SQL texts.

  Every generated measure is stamped with this digest of the oracle queries it
  cites, so editing a cited query without regenerating the model is detected
  by the drift check.  Line endings are normalized so the digest does not
  depend on the checkout's newline convention.
  """
  digest = hashlib.sha256()
  for query in oracle_queries:
    text = oracle_files[query].replace("\r\n", "\n")
    digest.update(str(query).encode("utf-8"))
    digest.update(b"\0")
    digest.update(text.encode("utf-8"))
    digest.update(b"\0")
  return digest.hexdigest()[:16]


# Reviewed lock over the oracle SQL the DAX measures are written against.  The
# per-measure digest above is derived and is refreshed by every regeneration;
# the lock is the opposite: a committed record of the oracle SQL that was last
# reconciled with the DAX, which generation refuses to move past until a person
# re-records it with `generate_pbip.py --write --accept-oracle-changes`.
ORACLE_LOCK_REL: str = "spec/oracle_lock.json"
ORACLE_LOCK_VERSION: int = 1
ORACLE_LOCK_DESCRIPTION: str = (
    "Reviewed sha256 digests of the Looker Studio oracle SQL that the Power BI"
    " measures are written against. Changing a cited query fails"
    " generate_pbip.py --check and --write until the DAX and the semantic"
    " fixtures are reconciled and the new digests are recorded deliberately"
    " with generate_pbip.py --write --accept-oracle-changes."
)
ORACLE_LOCK_HINT: str = (
    "Reconcile the DAX measures that cite the changed oracle SQL and the"
    " semantic fixtures, then record the new digests with: python"
    " dashboards/power_bi/tools/generate_pbip.py --write"
    " --accept-oracle-changes"
)
_SHA256_HEX_RE: re.Pattern[str] = re.compile(r"[0-9a-f]{64}")


class OracleLockError(ValueError):
  """The cited oracle SQL no longer matches the reviewed lock."""

  def __init__(self, problems: Iterable[str]) -> None:
    self.problems: list[str] = list(problems)
    super().__init__("\n".join([*self.problems, ORACLE_LOCK_HINT]))


def oracle_sql_digest(sql: str) -> str:
  """Return the full sha256 of an oracle query's text (CRLF read as LF)."""
  return hashlib.sha256(sql.replace("\r\n", "\n").encode("utf-8")).hexdigest()


def referenced_oracle_queries(
    contracts: Contracts, model_spec: Mapping[str, Any]
) -> list[str]:
  """Return the sorted oracle query keys the report or the model cites."""
  normalize = OracleFilesDict._normalize_key
  refs: set[str] = set()
  for chart in contracts.manifest.get("charts", []):
    refs.add(
        normalize(
            chart.get("oracle_query", f"oracle/queries/{chart['id']}.sql")
        )
    )
  for measure in model_spec.get("measures", []):
    for query in measure.get("oracle_queries") or []:
      refs.add(normalize(query))
  return sorted(refs)


def current_oracle_digests(
    contracts: Contracts, model_spec: Mapping[str, Any]
) -> dict[str, str]:
  """Return query key -> sha256 for every oracle query that is cited."""
  digests: dict[str, str] = {}
  for query in referenced_oracle_queries(contracts, model_spec):
    if query not in contracts.oracle_files:
      raise ValueError(f"Missing oracle query {query!r}")
    digests[query] = oracle_sql_digest(contracts.oracle_files[query])
  return digests


def read_oracle_lock(path: Path | str) -> dict[str, str]:
  """Read and structurally validate the committed oracle lock."""
  with open(path, "r", encoding="utf-8") as f:
    data = json.load(f)
  if (
      not isinstance(data, dict)
      or data.get("version") != ORACLE_LOCK_VERSION
      or data.get("algorithm") != "sha256"
  ):
    raise ValueError(
        f"expected a JSON object with version {ORACLE_LOCK_VERSION} and"
        ' algorithm "sha256"'
    )
  queries = data.get("oracle_queries")
  if not isinstance(queries, dict) or not queries:
    raise ValueError("oracle_queries must be a non-empty JSON object")
  for key, digest in queries.items():
    if not isinstance(digest, str) or not _SHA256_HEX_RE.fullmatch(digest):
      raise ValueError(f"{key!r} does not map to a sha256 hex digest")
  return dict(queries)


def render_oracle_lock(digests: Mapping[str, str]) -> str:
  """Render the lock file text deterministically (sorted keys, final LF)."""
  payload = {
      "version": ORACLE_LOCK_VERSION,
      "algorithm": "sha256",
      "description": ORACLE_LOCK_DESCRIPTION,
      "oracle_queries": {key: digests[key] for key in sorted(digests)},
  }
  return json.dumps(payload, indent=2) + "\n"


def diff_oracle_lock(
    current: Mapping[str, str], locked: Mapping[str, str]
) -> list[str]:
  """Describe every way the cited oracle SQL departs from the lock."""
  problems: list[str] = []
  for query in sorted(current):
    if query not in locked:
      problems.append(
          f"ORACLE LOCK: {query} is not recorded in {ORACLE_LOCK_REL}"
      )
    elif locked[query] != current[query]:
      problems.append(
          f"ORACLE LOCK: {query} changed since it was reviewed (locked sha256"
          f" {locked[query][:12]}, now {current[query][:12]})"
      )
  for query in sorted(set(locked) - set(current)):
    problems.append(
        f"ORACLE LOCK: {ORACLE_LOCK_REL} records {query}, which no chart or"
        " measure cites"
    )
  return problems


def oracle_lock_problems(
    power_bi_root: Path | str,
    contracts: Contracts,
    model_spec: Mapping[str, Any],
) -> list[str]:
  """Compare the cited oracle SQL with the committed lock (empty when equal)."""
  lock_path = Path(power_bi_root) / ORACLE_LOCK_REL
  if not lock_path.is_file():
    return [f"MISSING: {ORACLE_LOCK_REL}"]
  try:
    locked = read_oracle_lock(lock_path)
  except (OSError, ValueError) as exc:
    return [f"ORACLE LOCK: {ORACLE_LOCK_REL} is unusable: {exc}"]
  return diff_oracle_lock(current_oracle_digests(contracts, model_spec), locked)


_SQL_BLOCK_COMMENT_RE: re.Pattern[str] = re.compile(r"/\*.*?\*/", re.DOTALL)
_SQL_LINE_COMMENT_RE: re.Pattern[str] = re.compile(r"--[^\n]*")


def has_output_alias(sql: str, column: str) -> bool:
  """Whether *sql* names *column* as an `AS <column>` output alias.

  SQL comments are ignored so an alias that only appears in a comment does not
  count as an output column.
  """
  stripped = _SQL_LINE_COMMENT_RE.sub("", _SQL_BLOCK_COMMENT_RE.sub("", sql))
  pattern = rf"\bAS\s+{re.escape(column)}\b"
  return re.search(pattern, stripped, re.IGNORECASE) is not None


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
