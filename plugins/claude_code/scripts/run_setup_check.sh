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

# Wrapper for the /bqaa-setup slash command. Mirrors hooks/common.sh so
# the vendored package resolves from PYTHONPATH without a wheel install.
#
# Runner vs. target distinction:
#   * RUNNER_PYTHON is the interpreter that executes the setup_check
#     module. It must work — otherwise the diagnostic itself fails at
#     the shell layer (`bash: exec: $BQAA_PYTHON: No such file`) and
#     the user sees a raw shell error instead of an actionable
#     report.
#   * BQAA_PYTHON is the interpreter the hooks will use at runtime.
#     The setup_check module probes it internally — its job is
#     specifically to diagnose a broken BQAA_PYTHON. Don't conflate
#     the two.
#
# Prefer BQAA_PYTHON as the runner when it works, fall back to
# python3 (always assumed available) so /bqaa-setup never silently
# breaks on the most common misconfiguration.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_DIR="$(dirname "$SCRIPT_DIR")"

RUNNER_PYTHON="${BQAA_PYTHON:-python3}"
# Quick liveness check: if the configured interpreter cannot even
# exec a no-op, fall back to python3. setup_check.py still probes
# BQAA_PYTHON internally and surfaces the interpreter_error in the
# report.
if ! "$RUNNER_PYTHON" -c "pass" >/dev/null 2>&1; then
  RUNNER_PYTHON="python3"
fi

export PYTHONPATH="${PLUGIN_DIR}/vendor:${PYTHONPATH:-}"

exec "$RUNNER_PYTHON" -m bigquery_agent_analytics_tracing.setup_check "$@"
