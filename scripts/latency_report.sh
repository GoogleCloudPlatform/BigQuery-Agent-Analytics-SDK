#!/bin/bash
# Trace latency analyzer for agent sessions in BigQuery.
#
# Usage:
#   ./latency_report.sh                               # latest trace
#   ./latency_report.sh --limit 5                     # last 5 traces
#   ./latency_report.sh --session <session_id>         # specific session
#   ./latency_report.sh --time-period 1h               # traces from last hour
#   ./latency_report.sh --app-name my_agent            # filter by agent app
#   ./latency_report.sh --verbose                      # show questions/responses
#   ./latency_report.sh --no-stitch                    # skip A2A stitching
#   ./latency_report.sh --env path/to/.env             # use a specific .env file

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Parse --env flag before other processing
ENV_FILE=""
PASSTHROUGH_ARGS=()
while [[ $# -gt 0 ]]; do
    case "$1" in
        --env)
            ENV_FILE="$2"
            shift 2
            ;;
        *)
            PASSTHROUGH_ARGS+=("$1")
            shift
            ;;
    esac
done
set -- "${PASSTHROUGH_ARGS[@]}"

# Load .env: explicit --env wins, then repo root default
if [ -n "$ENV_FILE" ]; then
    if [ ! -f "$ENV_FILE" ]; then
        echo "ERROR: --env file not found: $ENV_FILE"
        exit 1
    fi
    set -a
    # shellcheck disable=SC1090
    source "$ENV_FILE"
    set +a
elif [ -f "${SCRIPT_DIR}/../.env" ]; then
    set -a
    source "${SCRIPT_DIR}/../.env"
    set +a
fi

# Env validation is handled by the Python script with structured logging;
# the shell wrapper only needs to load the .env file.

python3 "${SCRIPT_DIR}/latency_report.py" "$@"
