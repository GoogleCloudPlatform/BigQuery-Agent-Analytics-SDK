#!/usr/bin/env bash
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

# Builds the mod's released files into OUT_DIR: what the marketplace serves
# from plugins/claude_code_dist/bigquery-agent-analytics-mod, and what a
# mod-vX.Y.Z release archives. The tests, the example rows and the
# TypeScript config stay here; so does .claude-plugin/types, which the host
# CLI writes for the editor.
#   bash plugins/claude_code_mod/build_dist.sh OUT_DIR
# With --check, builds into a temporary folder and compares it with the
# committed copy: identical when this folder's version is the copy's, and
# the catalog entry's version is the copy's.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
dist="$repo/plugins/claude_code_dist/bigquery-agent-analytics-mod"
FILES=(.claude-plugin/plugin.json hooks types sql setup.sh README.md MIGRATION.md)

build() { # out dir
  rm -rf "$1"
  mkdir -p "$1/.claude-plugin"
  for item in "${FILES[@]}"; do cp -R "$here/$item" "$1/$item"; done
}
version_of() { python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["version"])' "$1/.claude-plugin/plugin.json"; }

if [[ "${1:-}" != --check ]]; then
  build "${1:?usage: build_dist.sh OUT_DIR | --check}"
  exit 0
fi
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
build "$work/built"
source_version="$(version_of "$here")"
dist_version="$(version_of "$dist")"
catalog_version="$(python3 -c 'import json, sys; print(next(p["version"] for p in json.load(open(sys.argv[1]))["plugins"] if p["name"] == "bigquery-agent-analytics-mod"))' "$repo/.claude-plugin/marketplace.json")"
status=0
if [[ "$catalog_version" != "$dist_version" ]]; then
  echo "not ok - the catalog entry says $catalog_version, the committed copy is $dist_version"
  status=1
else
  echo "ok - the catalog entry and the committed copy are both $dist_version"
fi
if [[ "$source_version" != "$dist_version" ]]; then
  echo "ok - the source is at $source_version, past the released $dist_version: the copy is synced at the next release"
elif diff -r "$work/built" "$dist" >"$work/diff" 2>&1; then
  echo "ok - the committed copy is exactly what the source builds at $dist_version"
else
  echo "not ok - the source changed at $dist_version without a version bump or a sync of the copy:"
  head -20 "$work/diff"
  status=1
fi
exit "$status"
