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

# Reset the demo to its initial state (V1 prompt, 3 golden eval cases).
# Run this before starting a fresh improvement cycle.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "Resetting demo to initial state..."

# Restore original golden eval set and prompts.py from git
git checkout -- "$SCRIPT_DIR/eval/eval_cases.json"
git checkout -- "$SCRIPT_DIR/agent/prompts.py"

# Delete old prompt, create fresh V1 in Vertex AI
python3 "$SCRIPT_DIR/setup_vertex.py"

# Remove generated traffic and reports
rm -f "$SCRIPT_DIR"/eval/synthetic_traffic_cycle_*.json
rm -rf "$SCRIPT_DIR"/reports/*

echo ""
echo "Done. Prompt reset to V1 in Vertex AI, golden eval set reset to 3 cases."
echo "Run ./run_cycle.sh to start a fresh cycle."
