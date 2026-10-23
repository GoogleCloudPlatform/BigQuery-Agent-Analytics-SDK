#!/bin/bash
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

# Checks the host's `$.state` as tests/reload.test.mjs models it, with the
# host CLI's own test runtime: copies the small mod in tests/state-contract
# to a scratch folder, its check as tests/state.test.ts, and runs its tests.
set -u
here=$(cd "$(dirname "$0")" && pwd)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
cp -R "$here/state-contract/." "$work/"
mkdir -p "$work/tests"
mv "$work/state.contract.ts" "$work/tests/state.test.ts"
out=$(cd "$work" && claude plugin test . 2>&1)
echo "$out" | grep -E '^\((pass|fail)\)'
echo "$out" | grep -qE '^ *0 fail' && echo "ok - the host's \$.state reads one moment a dispatch, refreshed whole by a write of its own" && exit 0
echo "not ok - the host's \$.state no longer matches what the reload test models"
exit 1
