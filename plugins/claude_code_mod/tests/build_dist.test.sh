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

# Offline test of build_dist.sh --check and sync_dist.sh, in a throwaway git
# repository shaped like this one (the source, the committed copy and the
# catalog), with mod-v release tags:
#   - a first release passes, before and after its tag;
#   - with the source ahead of the copy, a deleted, edited or no longer
#     executable file in the copy fails, and the untouched copy passes;
#   - a source lower than the copy, or not X.Y.Z, fails;
#   - the release cycle (a tag, a fix after it, the copy synced from the
#     tag's archive) passes;
#   - with no tag to check the copy against, the check fails;
#   - a catalog version that is not the copy's fails;
#   - sync_dist.sh refuses a wrong checksum and an archive of another version,
#     leaving the copy as it was.
#   bash plugins/claude_code_mod/tests/build_dist.test.sh
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
mod="$(dirname "$here")"
real="$(cd "$mod/../.." && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

passed=0
failed=0
check() { # description, then a command that must succeed
  local what="$1"
  shift
  if "$@" >"$work/last.log" 2>&1; then passed=$((passed + 1)); echo "ok - $what"; else failed=$((failed + 1)); echo "not ok - $what"; sed 's/^/    /' "$work/last.log"; fi
}
fails() { ! "$@"; }

repo="$work/repo"
src="$repo/plugins/claude_code_mod"
dist="$repo/plugins/claude_code_dist/bigquery-agent-analytics-mod"
mkdir -p "$repo/plugins/claude_code_dist" "$repo/.claude-plugin"
cp -Rp "$mod" "$src"
rm -rf "$src/.claude-plugin/types"
cp -Rp "$real/plugins/claude_code_dist/bigquery-agent-analytics-mod" "$dist"
cp "$real/.claude-plugin/marketplace.json" "$repo/.claude-plugin/marketplace.json"
git -C "$repo" init -q
git -C "$repo" -c user.name=test -c user.email=test@example.com add -A
git -C "$repo" -c user.name=test -c user.email=test@example.com commit -qm "first release"
commit() { git -C "$repo" -c user.name=test -c user.email=test@example.com commit -qam "$1"; }
version() { # folder, version
  python3 - "$1/.claude-plugin/plugin.json" "$2" <<'PY'
import json, sys
path, version = sys.argv[1:3]
data = json.load(open(path))
data["version"] = version
open(path, "w").write(json.dumps(data, indent=2) + "\n")
PY
}
catalog() { # version
  python3 - "$repo/.claude-plugin/marketplace.json" "$1" <<'PY'
import json, sys
path, version = sys.argv[1:3]
data = json.load(open(path))
for entry in data["plugins"]:
    if entry["name"] == "bigquery-agent-analytics-mod":
        entry["version"] = version
open(path, "w").write(json.dumps(data, indent=2) + "\n")
PY
}
checked() { bash "$src/build_dist.sh" --check; }
# A release's archive and SHA256SUMS, built from a tag as the release
# workflow builds them.
release() { # tag, version, folder
  local name="bigquery-agent-analytics-mod-$2" tagged="$work/tagged-$1"
  rm -rf "$tagged" && mkdir -p "$tagged" "$3"
  git -C "$repo" archive "$1" plugins/claude_code_mod | tar -x -C "$tagged"
  bash "$tagged/plugins/claude_code_mod/build_dist.sh" "$work/out/$name"
  tar -C "$work/out" -czf "$3/$name.tar.gz" "$name"
  (cd "$3" && python3 -c 'import hashlib, sys; print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest() + "  " + sys.argv[1])' "$name.tar.gz" >SHA256SUMS)
}
tree() { (cd "$dist" && find . -type f -exec shasum {} + | sort | shasum); }

check 'the first release, before its tag: the copy is what the source builds now' checked
git -C "$repo" tag mod-v0.1.0
check 'the first release, tagged: the copy is what the source built at mod-v0.1.0' checked

version "$src" 0.1.1 && commit "source ahead"
check 'the source ahead: the untouched copy still checks against mod-v0.1.0' checked
rm "$dist/hooks/register.ts"
check 'the source ahead: a deleted file in the copy fails' fails checked
git -C "$repo" checkout -q -- "$dist"
echo '// edited' >>"$dist/hooks/lib/writer.ts"
check 'the source ahead: an edited file in the copy fails' fails checked
git -C "$repo" checkout -q -- "$dist"
chmod -x "$dist/setup.sh"
check 'the source ahead: setup.sh no longer executable in the copy fails' fails checked
chmod +x "$dist/setup.sh"
version "$src" 0.0.9
check 'a source lower than the copy fails' fails checked
version "$src" next
check 'a source version that is not X.Y.Z fails' fails checked
version "$src" 0.1.1

git -C "$repo" tag mod-v0.1.1
echo "A fix after the release." >>"$src/README.md" && commit "a fix after mod-v0.1.1"
release mod-v0.1.1 0.1.1 "$work/release"
before="$(tree)"
sed -i.bak 's/^[0-9a-f]*/0000000000000000000000000000000000000000000000000000000000000000/' "$work/release/SHA256SUMS" && rm "$work/release/SHA256SUMS.bak"
check 'sync: a wrong checksum is refused' fails env BQAA_MOD_RELEASE_BASE="file://$work/release" bash "$src/sync_dist.sh" 0.1.1
check 'sync: the refused archive left the copy as it was' test "$(tree)" = "$before"
release mod-v0.1.1 0.1.1 "$work/release"
# An archive named for 0.1.2 that holds the 0.1.1 mod.
mkdir -p "$work/mislabeled" && cp -Rp "$work/out/bigquery-agent-analytics-mod-0.1.1" "$work/out/bigquery-agent-analytics-mod-0.1.2"
tar -C "$work/out" -czf "$work/mislabeled/bigquery-agent-analytics-mod-0.1.2.tar.gz" bigquery-agent-analytics-mod-0.1.2
(cd "$work/mislabeled" && python3 -c 'import hashlib, sys; print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest() + "  " + sys.argv[1])' bigquery-agent-analytics-mod-0.1.2.tar.gz >SHA256SUMS)
mislabeled() { ! env BQAA_MOD_RELEASE_BASE="file://$work/mislabeled" bash "$src/sync_dist.sh" 0.1.2 >"$work/mislabeled.log" 2>&1 && grep -q 'holds version 0.1.1, not 0.1.2' "$work/mislabeled.log"; }
check 'sync: an archive that holds another version is refused' mislabeled
check 'sync: ... and left the copy as it was' test "$(tree)" = "$before"
check 'sync: the right archive replaces the copy' env BQAA_MOD_RELEASE_BASE="file://$work/release" bash "$src/sync_dist.sh" 0.1.1
catalog 0.1.1
check 'the release cycle: the synced copy checks against mod-v0.1.1, though the source changed after it' checked
catalog 0.1.0
check 'a catalog version that is not the copy'"'"'s fails' fails checked
catalog 0.1.1
git -C "$repo" tag -d mod-v0.1.1 >/dev/null
version "$src" 0.1.2
check 'no tag to check the copy against (the source ahead) fails' fails checked

echo "$passed passed, $failed failed"
((failed == 0))
