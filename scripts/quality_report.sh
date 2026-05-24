#!/bin/bash
# Quality evaluation report for agent traces in BigQuery.
#
# Usage:
#   ./quality_report.sh                          # evaluate last 100 sessions
#   ./quality_report.sh --limit 50               # evaluate last 50
#   ./quality_report.sh --no-eval                # browse Q&A only
#   ./quality_report.sh --report                 # also generate markdown report
#   ./quality_report.sh --persist                # evaluate + persist to BQ
#   ./quality_report.sh --time-period 7d         # evaluate last 7 days
#   ./quality_report.sh --samples 20             # show 20 sessions per category
#   ./quality_report.sh --samples all            # show all sessions
#   ./quality_report.sh --env path/to/.env       # use a specific .env file

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Parse --env flag before other processing (supports --env PATH and --env=PATH).
# The flag is NOT stripped — Python also accepts --env for report metadata.
ENV_FILE=""
for arg in "$@"; do
    if [ "$_NEXT_IS_ENV" = "1" ]; then
        ENV_FILE="$arg"
        _NEXT_IS_ENV=0
        continue
    fi
    if [ "$arg" = "--env" ]; then
        _NEXT_IS_ENV=1
        continue
    fi
    if [[ "$arg" == --env=* ]]; then
        ENV_FILE="${arg#--env=}"
        continue
    fi
done
unset _NEXT_IS_ENV

# Load .env: explicit --env wins, then repo root default
if [ -n "$ENV_FILE" ]; then
    if [ ! -f "$ENV_FILE" ]; then
        echo "ERROR: --env file not found: $ENV_FILE"
        exit 1
    fi
    set -a
    source "$ENV_FILE"
    set +a
elif [ -f "${SCRIPT_DIR}/../.env" ]; then
    set -a
    source "${SCRIPT_DIR}/../.env"
    set +a
fi

# Short-circuit: pass --help / -h straight to Python (no env needed)
for arg in "$@"; do
    if [ "$arg" = "--help" ] || [ "$arg" = "-h" ]; then
        python3 "${SCRIPT_DIR}/quality_report.py" "$@"
        exit $?
    fi
done

# Validate required env vars (skip for --conversations-file mode)
HAS_CONVERSATIONS_FILE=false
for arg in "$@"; do
    if [[ "$arg" == "--conversations-file" || "$arg" == --conversations-file=* ]]; then
        HAS_CONVERSATIONS_FILE=true
        break
    fi
done
if ! $HAS_CONVERSATIONS_FILE; then
    for var in PROJECT_ID DATASET_ID TABLE_ID DATASET_LOCATION; do
        if [ -z "${!var}" ]; then
            echo "ERROR: Required environment variable ${var} is not set."
            echo "Use --env /path/to/.env, or 'export ${var}=...' in your shell."
            exit 1
        fi
    done
fi

# Log eval runs (skip logging for --no-eval)
if [[ " $* " != *" --no-eval "* ]]; then
    REPORTS_DIR="${SCRIPT_DIR}/reports"
    mkdir -p "${REPORTS_DIR}"
    TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
    LOG_FILE="${REPORTS_DIR}/quality_report_${TIMESTAMP}.log"
    if [ -t 1 ]; then
        echo -e "\033[0;32mLog: ${LOG_FILE}\033[0m"
    else
        echo "Log: ${LOG_FILE}"
    fi
    python3 "${SCRIPT_DIR}/quality_report.py" "$@" 2>&1 | tee "${LOG_FILE}"
else
    REPORTS_DIR="${SCRIPT_DIR}/reports"
    mkdir -p "${REPORTS_DIR}"
    TIMESTAMP=$(date +"%Y%m%d_%H%M%S")
    LOG_FILE="${REPORTS_DIR}/quality_browse_${TIMESTAMP}.log"
    if [ -t 1 ]; then
        echo -e "\033[0;32mLog: ${LOG_FILE}\033[0m"
    else
        echo "Log: ${LOG_FILE}"
    fi
    python3 "${SCRIPT_DIR}/quality_report.py" "$@" 2>&1 | tee "${LOG_FILE}"
fi
