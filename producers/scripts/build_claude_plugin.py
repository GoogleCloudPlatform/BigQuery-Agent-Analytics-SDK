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

"""Build the Claude Code plugin artifact.

What this does:
  1. Copies producers/src/bigquery_agent_analytics_tracing/ into
     plugins/claude_code/vendor/ so the plugin runs without a
     `pip install bigquery-agent-analytics-tracing` on `BQAA_PYTHON`.
  2. Resolves the producer package version (from installed metadata
     when available, else from producers/pyproject.toml) and stamps it
     into plugins/claude_code/.claude-plugin/plugin.json.
  3. Tars the plugin tree into
     producers/dist/bigquery-agent-analytics-tracing-claude-code-<version>.tar.gz.

Run from anywhere — paths are anchored relative to this file. CI runs
this after `pytest` and uploads the tarball as a workflow artifact.
"""

from __future__ import annotations

import argparse
from importlib import metadata
import json
from pathlib import Path
import shutil
import sys
import tarfile

# tomllib is stdlib on Python 3.11+; fall back to the `tomli`
# backport on 3.10 (declared as a conditional dev dependency).
try:
  import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised only on 3.10.
  import tomli as tomllib

PACKAGE_DIST_NAME = "bigquery-agent-analytics-tracing"
PACKAGE_IMPORT_NAME = "bigquery_agent_analytics_tracing"
PLUGIN_DIR_NAME = "claude_code"
PLUGIN_ARTIFACT_PREFIX = f"{PACKAGE_DIST_NAME}-claude-code"

REPO_ROOT = Path(__file__).resolve().parents[2]
PRODUCERS_DIR = REPO_ROOT / "producers"
PACKAGE_SRC = PRODUCERS_DIR / "src" / PACKAGE_IMPORT_NAME
PLUGIN_DIR = REPO_ROOT / "plugins" / PLUGIN_DIR_NAME
MANIFEST_PATH = PLUGIN_DIR / ".claude-plugin" / "plugin.json"
DEFAULT_DIST_DIR = PRODUCERS_DIR / "dist"


def resolve_version() -> str:
  """Return the producer package version.

  Prefers installed distribution metadata (matches what `__version__`
  resolves to at runtime). Falls back to reading the producer
  pyproject.toml when the package is not installed (CI / fresh checkout).
  """
  try:
    return metadata.version(PACKAGE_DIST_NAME)
  except metadata.PackageNotFoundError:
    pass
  pyproject = PRODUCERS_DIR / "pyproject.toml"
  with pyproject.open("rb") as fh:
    return tomllib.load(fh)["project"]["version"]


def vendor_package(*, dry_run: bool = False) -> Path:
  """Copy the producer package source into the plugin's vendor/ tree.

  Wipes ``vendor/`` first so stale ``.dist-info`` directories from an
  older version do not accumulate (``importlib.metadata.version()``
  would otherwise see two distributions and pick one nondeterministically).
  """
  vendor_root = PLUGIN_DIR / "vendor"
  target = vendor_root / PACKAGE_IMPORT_NAME
  if dry_run:
    return target
  if not PACKAGE_SRC.is_dir():
    raise FileNotFoundError(
        f"Package source not found at {PACKAGE_SRC}; "
        "run from a checkout of the producers/ tree."
    )
  if vendor_root.exists():
    shutil.rmtree(vendor_root)
  vendor_root.mkdir(parents=True, exist_ok=True)
  shutil.copytree(
      PACKAGE_SRC,
      target,
      ignore=shutil.ignore_patterns(
          "__pycache__", "*.pyc", "*.pyo", "*.egg-info"
      ),
  )
  return target


def write_vendor_dist_info(version: str) -> Path:
  """Write a minimal PEP 376 ``.dist-info/METADATA`` alongside the
  vendored package so ``importlib.metadata.version()`` resolves.

  Without this, a vendored-plugin runtime (no wheel install on
  ``BQAA_PYTHON``) leaves ``__version__`` at the ``"0.0.0+local"``
  fallback and every emitted row's ``attributes.writer.version`` is
  ``"0.0.0+local"`` — silently undermining the adoption-query
  contract for marketplace installs.

  Wheel-installed runtimes (when someone pip-installs
  ``bigquery-agent-analytics-tracing``) keep their own metadata; this
  file only matters when the package is on ``PYTHONPATH`` without
  pip's site-packages alongside.
  """
  vendor_root = PLUGIN_DIR / "vendor"
  dist_info = vendor_root / f"{PACKAGE_IMPORT_NAME}-{version}.dist-info"
  dist_info.mkdir(parents=True, exist_ok=True)
  (dist_info / "METADATA").write_text(
      f"Metadata-Version: 2.1\n"
      f"Name: {PACKAGE_DIST_NAME}\n"
      f"Version: {version}\n",
      encoding="utf-8",
  )
  return dist_info


def stamp_manifest_version(version: str, *, dry_run: bool = False) -> dict:
  """Write the resolved version into .claude-plugin/plugin.json."""
  with MANIFEST_PATH.open("r", encoding="utf-8") as fh:
    manifest = json.load(fh)
  manifest["version"] = version
  if dry_run:
    return manifest
  # Preserve trailing newline + 2-space indent so diffs stay minimal.
  rendered = json.dumps(manifest, indent=2, sort_keys=False) + "\n"
  MANIFEST_PATH.write_text(rendered, encoding="utf-8")
  return manifest


def build_tarball(version: str, dist_dir: Path) -> Path:
  """Tar the plugin tree under dist_dir, return the tarball path.

  Excludes .gitignore and __pycache__ so the artifact is exactly what
  marketplace users will install.
  """
  dist_dir.mkdir(parents=True, exist_ok=True)
  archive_name = f"{PLUGIN_ARTIFACT_PREFIX}-{version}"
  tar_path = dist_dir / f"{archive_name}.tar.gz"
  if tar_path.exists():
    tar_path.unlink()

  def _filter(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
    name = Path(info.name).name
    if name in {".gitignore", "__pycache__"}:
      return None
    if name.endswith((".pyc", ".pyo")):
      return None
    return info

  with tarfile.open(tar_path, "w:gz") as tar:
    tar.add(PLUGIN_DIR, arcname=archive_name, filter=_filter)
  return tar_path


def build(
    *,
    dist_dir: Path = DEFAULT_DIST_DIR,
    skip_tar: bool = False,
) -> dict:
  """Vendor + stamp + (optionally) tar. Returns a summary dict."""
  version = resolve_version()
  vendor_target = vendor_package()
  dist_info = write_vendor_dist_info(version)
  manifest = stamp_manifest_version(version)
  tar_path: Path | None = None
  if not skip_tar:
    tar_path = build_tarball(version, dist_dir)
  return {
      "version": version,
      "vendor_target": str(vendor_target),
      "vendor_dist_info": str(dist_info),
      "manifest_version": manifest["version"],
      "tar_path": str(tar_path) if tar_path else None,
  }


def main(argv: list[str] | None = None) -> int:
  parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
  parser.add_argument(
      "--dist-dir",
      type=Path,
      default=DEFAULT_DIST_DIR,
      help="Where to write the plugin tarball (default: producers/dist)",
  )
  parser.add_argument(
      "--skip-tar",
      action="store_true",
      help="Only vendor + stamp, skip the tarball.",
  )
  args = parser.parse_args(argv)
  summary = build(dist_dir=args.dist_dir, skip_tar=args.skip_tar)
  json.dump(summary, sys.stdout, indent=2, sort_keys=True)
  sys.stdout.write("\n")
  return 0


if __name__ == "__main__":
  raise SystemExit(main())
