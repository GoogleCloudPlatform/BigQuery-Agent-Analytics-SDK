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

# The mod's version is in two places: the manifest, which the host reads, and
# PLUGIN_VERSION, which the welcome card greets and every row's writer label
# carries. They must agree: a manifest bumped alone would greet no update.
# Run from anywhere:
#   bash tests/version.test.sh
set -euo pipefail

plugin="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
manifest="$(sed -n 's/^ *"version": *"\([^"]*\)".*/\1/p' "$plugin/.claude-plugin/plugin.json" | head -n 1)"
code="$(sed -n "s/^export const PLUGIN_VERSION = '\([^']*\)'.*/\1/p" "$plugin/hooks/lib/config.ts" | head -n 1)"
if [ -n "$manifest" ] && [ "$manifest" = "$code" ]; then
  echo "ok - plugin.json and PLUGIN_VERSION agree ($manifest)"
else
  echo "not ok - plugin.json says '$manifest', PLUGIN_VERSION says '$code'"
  exit 1
fi
