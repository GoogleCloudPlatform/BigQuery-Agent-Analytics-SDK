#!/bin/sh
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

# Opt-in Git pre-commit hook: scans the staged changes with CodeMender and
# blocks the commit when it finds a vulnerability with a failing severity.
# Developers can skip a hook, so .github/workflows/codemender.yml also scans
# pull requests.
#
# Install it from the repository root:
#   ln -s "$PWD/scripts/codemender_pre_commit.sh" "$(git rev-parse --git-path hooks)/pre-commit"
#
# Environment:
#   BQAA_CODEMENDER_FAIL_ON   Severities that block the commit (CRITICAL,
#                             HIGH, MEDIUM, LOW), comma-separated. Unset means
#                             CRITICAL,HIGH; "" reports without blocking.
#   BQAA_CODEMENDER_REQUIRE   1 blocks the commit when cm isn't installed;
#                             otherwise the hook skips the scan.
#
# Prerequisites and details: scripts/README.md (CodeMender pre-commit hook).

set -u

setup_url="https://docs.cloud.google.com/gemini-enterprise-agent-platform/agents/codemender/set-up-environment"

if ! command -v cm > /dev/null 2>&1; then
  if [ "${BQAA_CODEMENDER_REQUIRE:-}" = 1 ]; then
    echo "codemender_pre_commit: cm is not on PATH, and BQAA_CODEMENDER_REQUIRE=1 requires the scan. Install the CodeMender CLI: $setup_url" >&2
    exit 1
  fi
  echo "codemender_pre_commit: cm is not on PATH; skipping the CodeMender scan. Install the CodeMender CLI: $setup_url" >&2
  exit 0
fi

# Git runs the hook at the top of the work tree; a manual run may not.
top="$(git rev-parse --show-toplevel)" || exit 1
cd "$top" || exit 1

# Nothing staged (an empty commit, or an amend that only rewords): no scan.
if git diff-index --cached --quiet HEAD -- 2> /dev/null; then
  exit 0
fi

status=0
cm find . --diff --staged --fail-on="${BQAA_CODEMENDER_FAIL_ON-CRITICAL,HIGH}" \
  --yes --bypass-warning || status=$?
if [ "$status" -ne 0 ]; then
  echo "CodeMender blocked this commit (cm find exited with status $status). To list the open findings, run:" >&2
  echo "  cm report --status=OPEN" >&2
  echo "To commit anyway, run git commit --no-verify." >&2
  exit 1
fi
