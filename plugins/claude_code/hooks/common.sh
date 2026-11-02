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
# bigquery_agent_analytics_tracing.claude_code launch below work
# without `pip install bigquery-agent-analytics-tracing` — and the
# drainer subprocess inherits the same PYTHONPATH via os.environ when
# the logger spawns it.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLUGIN_DIR="$(dirname "$SCRIPT_DIR")"
PYTHON_BIN="${BQAA_PYTHON:-python3}"

# Match config.py, which lower-cases a set value and compares it to
# "true", so TRUE / True enable tracing here too and any other set
# value, including an empty one, disables it. Only an unset variable
# defaults to on, hence ${var-true} rather than ${var:-true}. A case
# glob (not ${var,,}) keeps this working on the Bash 3.2 that macOS
# ships as /bin/bash.
case "${BQAA_TRACE_ENABLED-true}" in
  [Tt][Rr][Uu][Ee]) ;;
  *) exit 0 ;;
esac

# Hooks run in the session's working directory, usually a checked-out
# repository, so a file there such as json.py or sitecustomize.py must
# not be imported. Python would find it in two ways:
#
# * An empty PYTHONPATH entry (a leading, trailing or doubled ":")
#   stands for the working directory, and the interpreter searches
#   PYTHONPATH for sitecustomize as it starts. Rebuild PYTHONPATH as
#   vendor/ plus the caller's non-empty entries, in order. Entries set
#   explicitly, relative ones included, are kept as given.
# * `python -m` (and -c) puts the working directory first on sys.path,
#   on every Python version unless PYTHONSAFEPATH (3.11+) is set.
#   RUN_MODULE runs the module like `python -m` but drops that entry
#   before anything is imported; the logger starts the drainer the same
#   way (_utils.PYTHON_C_RUN_MODULE).
pythonpath="${PLUGIN_DIR}/vendor"
rest="${PYTHONPATH-}"
while [ -n "$rest" ]; do
  entry="${rest%%:*}"
  if [ "$entry" = "$rest" ]; then rest=""; else rest="${rest#*:}"; fi
  if [ -n "$entry" ]; then pythonpath="${pythonpath}:${entry}"; fi
done
export PYTHONPATH="$pythonpath"

RUN_MODULE='import sys
if not getattr(sys.flags, "safe_path", False):
    del sys.path[0]
del sys.argv[0]
import runpy
runpy.run_module(sys.argv[0], run_name="__main__", alter_sys=True)'

exec "$PYTHON_BIN" -c "$RUN_MODULE" \
  bigquery_agent_analytics_tracing.claude_code "$1"
