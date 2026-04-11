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

# Example: CI/CD evaluation pipeline using bq-agent-sdk CLI.
#
# This script runs multiple evaluations and gates the build on their
# results.  Use it in GitHub Actions, Cloud Build, or any CI system.
#
# Prerequisites:
#   pip install bigquery-agent-analytics
#   export BQ_AGENT_PROJECT=my-project
#   export BQ_AGENT_DATASET=agent_analytics
#
# Usage:
#   bash examples/ci_eval_pipeline.sh
#
# In GitHub Actions:
#   - name: Run agent evaluation gate
#     env:
#       BQ_AGENT_PROJECT: ${{ secrets.BQ_PROJECT }}
#       BQ_AGENT_DATASET: ${{ secrets.BQ_DATASET }}
#     run: bash examples/ci_eval_pipeline.sh

set -euo pipefail

echo "=== Agent Evaluation Pipeline ==="
echo ""

# Step 1: Health check
echo "--- Step 1: Health check ---"
if ! bq-agent-sdk doctor --format=text; then
  echo "FAIL: Health check failed. Aborting pipeline."
  exit 2
fi
echo ""

# Step 2: Latency evaluation (last 24h)
echo "--- Step 2: Latency gate ---"
bq-agent-sdk evaluate \
  --evaluator=latency \
  --threshold=5000 \
  --last=24h \
  --exit-code \
  --format=text
echo ""

# Step 3: Error rate evaluation
echo "--- Step 3: Error rate gate ---"
bq-agent-sdk evaluate \
  --evaluator=error_rate \
  --threshold=0.1 \
  --last=24h \
  --exit-code \
  --format=text
echo ""

# Step 4: Cost evaluation
echo "--- Step 4: Cost gate ---"
bq-agent-sdk evaluate \
  --evaluator=cost \
  --threshold=2.0 \
  --last=24h \
  --exit-code \
  --format=text
echo ""

# Step 5: Generate insights report (informational, does not gate)
echo "--- Step 5: Insights report ---"
bq-agent-sdk insights --last=24h --format=text || true
echo ""

echo "=== All evaluation gates passed ==="
