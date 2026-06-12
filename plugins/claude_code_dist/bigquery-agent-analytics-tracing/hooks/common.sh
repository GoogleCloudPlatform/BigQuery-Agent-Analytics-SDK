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

# Shared hook entry. Each hooks/<hook_name>.sh execs this with its hook
# name as $1.
#
# Vendored package: bigquery_agent_analytics_tracing/ lives under
# vendor/ in the plugin tree. Prepending it to PYTHONPATH lets the
# `-m bigquery_agent_analytics_tracing.claude_code` invocation work
# without `pip install bigquery-agent-analytics-tracing` — and the
# drainer subprocess inherits the same PYTHONPATH via os.environ when
# the logger spawns it.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_DIR="$(dirname "$SCRIPT_DIR")"
PYTHON_BIN="${BQAA_PYTHON:-python3}"

if [[ "${BQAA_TRACE_ENABLED:-true}" != "true" ]]; then
  exit 0
fi

export PYTHONPATH="${PLUGIN_DIR}/vendor:${PYTHONPATH:-}"

exec "$PYTHON_BIN" -m bigquery_agent_analytics_tracing.claude_code "$1"
