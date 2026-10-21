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
#
# With --check, verifies the committed copy:
#   - the catalog entry's version is the copy's;
#   - the source's version is not lower than the copy's;
#   - the copy is exactly what the source at the copy's release tag,
#     mod-v<copy version>, builds with that tag's own build_dist.sh (as the
#     release workflow built its archive), whatever the source holds now:
#     the same files, contents and executable bits. Only while no mod-v tag
#     exists at all (before the first release) is it checked against what
#     the source builds now, which must be at the same version; once any
#     release is tagged, a copy whose own tag is missing fails;
#   - every mod-dist-v<version> tag, the ref an organization registers this
#     repository at, holds the copy and the catalog entry at <version>.
# The tags need a full checkout (fetch-depth: 0): a shallow one fails.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
repo="$(cd "$here/../.." && pwd)"
dist="$repo/plugins/claude_code_dist/bigquery-agent-analytics-mod"
FILES=(.claude-plugin/plugin.json hooks types sql setup.sh README.md QUICKSTART.md MIGRATION.md docs)

build() { # source folder, out folder
  rm -rf "$2"
  mkdir -p "$2/.claude-plugin"
  for item in "${FILES[@]}"; do cp -Rp "$1/$item" "$2/$item"; done
}
version_of() { python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["version"])' "$1/.claude-plugin/plugin.json"; }
# Whether the first version is lower than the second (X.Y.Z, numerically):
# 0 when it is, 1 when it is not, 2 when either is not X.Y.Z.
lower() {
  python3 - "$1" "$2" <<'PY'
import re, sys
def parse(v):
    m = re.match(r"^(\d+)\.(\d+)\.(\d+)", v)
    if not m:
        print(f"not a version: {v!r}", file=sys.stderr)
        sys.exit(2)
    return tuple(int(x) for x in m.groups())
sys.exit(0 if parse(sys.argv[1]) < parse(sys.argv[2]) else 1)
PY
}
# Whether two folders hold the same files, contents and executable bits
# (the host CLI's .claude-plugin/types left out).
same_tree() {
  python3 - "$1" "$2" <<'PY'
import os, stat, sys
def walk(root):
    found = {}
    for folder, dirs, files in os.walk(root):
        if os.path.relpath(folder, root) == ".claude-plugin":
            dirs[:] = [d for d in dirs if d != "types"]
        for name in files:
            path = os.path.join(folder, name)
            with open(path, "rb") as handle:
                found[os.path.relpath(path, root)] = (handle.read(), bool(os.lstat(path).st_mode & stat.S_IXUSR))
    return found
built, copy = walk(sys.argv[1]), walk(sys.argv[2])
differ = sorted(set(built) ^ set(copy)) + sorted(k for k in set(built) & set(copy) if built[k] != copy[k])
for name in differ[:20]:
    print(f"  differs: {name}")
sys.exit(1 if differ else 0)
PY
}

if [[ "${1:-}" != --check ]]; then
  build "$here" "${1:?usage: build_dist.sh OUT_DIR | --check}"
  exit 0
fi
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
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
compared=0
lower "$source_version" "$dist_version" || compared=$?
if [[ "$compared" == 0 ]]; then
  echo "not ok - the source is at $source_version, lower than the released copy's $dist_version"
  status=1
elif [[ "$compared" != 1 ]]; then
  echo "not ok - the source's version $source_version and the copy's $dist_version are not both X.Y.Z"
  status=1
fi
tag="mod-v$dist_version"
if git -C "$repo" rev-parse -q --verify "refs/tags/$tag^{commit}" >/dev/null; then
  mkdir -p "$work/tagged"
  git -C "$repo" archive "$tag" plugins/claude_code_mod | tar -x -C "$work/tagged"
  # With the tag's own build_dist.sh: its list of released files, not this one's.
  if bash "$work/tagged/plugins/claude_code_mod/build_dist.sh" "$work/built" >/dev/null; then
    against="the source at $tag, built by its own build_dist.sh"
  else
    echo "not ok - the source at $tag does not build with its own build_dist.sh"
    rm -rf "$work/built"
    status=1
  fi
elif [[ -n "$(git -C "$repo" tag -l 'mod-v*')" ]]; then
  echo "not ok - there is no tag $tag to check the copy against, though other mod-v releases are tagged: a copy is checked against its own release"
  status=1
elif [[ "$(git -C "$repo" rev-parse --is-shallow-repository)" == true ]]; then
  echo "not ok - this checkout is shallow, so the mod-v tags cannot be seen: fetch them (fetch-depth: 0)"
  status=1
elif [[ "$source_version" == "$dist_version" ]]; then
  build "$here" "$work/built"
  against="the source now (no mod-v release is tagged yet)"
else
  echo "not ok - there is no tag $tag to check the copy against (the source is at $source_version; fetch the tags)"
  status=1
fi
if [[ -d "$work/built" ]]; then
  if same_tree "$work/built" "$dist"; then
    echo "ok - the committed copy is exactly what $against builds: files, contents and executable bits"
  else
    echo "not ok - the committed copy is not what $against builds"
    status=1
  fi
fi
for dist_tag in $(git -C "$repo" tag -l 'mod-dist-v*'); do
  want="${dist_tag#mod-dist-v}"
  at_copy="$(git -C "$repo" show "$dist_tag:plugins/claude_code_dist/bigquery-agent-analytics-mod/.claude-plugin/plugin.json" 2>/dev/null \
    | python3 -c 'import json, sys; print(json.load(sys.stdin)["version"])' 2>/dev/null || echo none)"
  at_catalog="$(git -C "$repo" show "$dist_tag:.claude-plugin/marketplace.json" 2>/dev/null \
    | python3 -c 'import json, sys; print(next(p["version"] for p in json.load(sys.stdin)["plugins"] if p["name"] == "bigquery-agent-analytics-mod"))' 2>/dev/null || echo none)"
  if [[ "$at_copy" == "$want" && "$at_catalog" == "$want" ]]; then
    echo "ok - $dist_tag holds the copy and the catalog entry at $want"
  else
    echo "not ok - $dist_tag holds the copy at $at_copy and the catalog entry at $at_catalog, not $want: an organization pinned to it installs that"
    status=1
  fi
done
exit "$status"
