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

# Replaces the committed copy, plugins/claude_code_dist/bigquery-agent-analytics-mod,
# with a published mod-vX.Y.Z release: only after the download, the
# SHA256SUMS check, the extraction and the version check all succeed. Any
# failure leaves the copy as it was. It then prints the two steps left: the
# catalog entry's version, and build_dist.sh --check.
#   bash plugins/claude_code_mod/sync_dist.sh X.Y.Z
# BQAA_MOD_RELEASE_BASE names another place for the release files (a
# file:// URL in the tests).
set -euo pipefail
version="${1:?usage: sync_dist.sh X.Y.Z}"
here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
dist="$repo/plugins/claude_code_dist/bigquery-agent-analytics-mod"
base="${BQAA_MOD_RELEASE_BASE:-https://github.com/GoogleCloudPlatform/BigQuery-Agent-Analytics-SDK/releases/download/mod-v$version}"
name="bigquery-agent-analytics-mod-$version"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

curl -fsSL "$base/$name.tar.gz" -o "$work/$name.tar.gz"
curl -fsSL "$base/SHA256SUMS" -o "$work/SHA256SUMS"
python3 - "$work" "$name.tar.gz" <<'PY'
import hashlib, pathlib, sys
work, archive = pathlib.Path(sys.argv[1]), sys.argv[2]
listed = {}
for line in (work / "SHA256SUMS").read_text().splitlines():
    if line.strip():
        digest, _, file = line.partition("  ")
        listed[file.strip().lstrip("*")] = digest.strip().lower()
actual = hashlib.sha256((work / archive).read_bytes()).hexdigest()
if listed.get(archive) != actual:
    sys.exit(f"SHA256SUMS does not list {archive} as {actual}: nothing was replaced")
print(f"ok - {archive} matches SHA256SUMS")
PY
mkdir "$work/out"
tar -xzf "$work/$name.tar.gz" -C "$work/out"
got="$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["version"])' "$work/out/$name/.claude-plugin/plugin.json")"
if [[ "$got" != "$version" ]]; then
  echo "the archive holds version $got, not $version: nothing was replaced" >&2
  exit 1
fi
rm -rf "$dist.new"
cp -Rp "$work/out/$name" "$dist.new"
rm -rf "$dist"
mv "$dist.new" "$dist"
echo "ok - synced the committed copy to $version. Next: set the catalog entry's version to $version"
echo "in .claude-plugin/marketplace.json, then run: bash plugins/claude_code_mod/build_dist.sh --check"
