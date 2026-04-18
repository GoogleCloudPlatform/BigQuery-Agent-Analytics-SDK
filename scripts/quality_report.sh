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

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Load .env from repo root if present
if [ -f "${SCRIPT_DIR}/../.env" ]; then
    set -a
    source "${SCRIPT_DIR}/../.env"
    set +a
fi

# Validate required env vars
for var in PROJECT_ID DATASET_ID TABLE_ID DATASET_LOCATION; do
    if [ -z "${!var}" ]; then
        echo "ERROR: Required environment variable ${var} is not set."
        echo "Set it in your shell or create a .env file. See scripts/README.md."
        exit 1
    fi
done

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
