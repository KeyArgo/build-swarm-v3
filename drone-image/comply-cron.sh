#!/bin/bash
# Build Swarm v3 — Daily Compliance Check (cron wrapper)
#
# Runs comply.sh against the local drone spec and logs results.
# Silent on success (no output = no cron email).
# Logs WARN/FAIL to /var/log/build-swarm/compliance.log.
#
# Install:
#   cp comply-cron.sh /opt/build-swarm/comply-cron.sh
#   chmod +x /opt/build-swarm/comply-cron.sh
#   echo "0 6 * * * root /opt/build-swarm/comply-cron.sh" > /etc/cron.d/swarm-comply
#
# Or run manually:
#   /opt/build-swarm/comply-cron.sh
#   /opt/build-swarm/comply-cron.sh --verbose   # always show output

set -uo pipefail

COMPLY_SCRIPT="/opt/build-swarm/comply.sh"
SPEC_FILE="/etc/build-swarm/drone.spec"
LOG_FILE="/var/log/build-swarm/compliance.log"
VERBOSE=false

for arg in "$@"; do
    case "$arg" in
        --verbose|-v) VERBOSE=true ;;
    esac
done

# Verify prerequisites
if [ ! -f "$COMPLY_SCRIPT" ]; then
    echo "$(date -Iseconds) ERROR comply.sh not found at $COMPLY_SCRIPT" >> "$LOG_FILE"
    exit 1
fi

if [ ! -f "$SPEC_FILE" ]; then
    echo "$(date -Iseconds) ERROR drone.spec not found at $SPEC_FILE" >> "$LOG_FILE"
    exit 1
fi

mkdir -p "$(dirname "$LOG_FILE")"

# Run compliance check
OUTPUT=$(bash "$COMPLY_SCRIPT" --spec "$SPEC_FILE" 2>&1)
EXIT_CODE=$?

TIMESTAMP=$(date -Iseconds)
HOSTNAME_STR=$(hostname)

# Strip ANSI color codes for logging
CLEAN_OUTPUT=$(echo "$OUTPUT" | sed 's/\x1b\[[0-9;]*m//g')

if [ $EXIT_CODE -eq 0 ]; then
    # Fully compliant — log one-liner, stay silent
    echo "$TIMESTAMP $HOSTNAME_STR COMPLIANT (exit 0)" >> "$LOG_FILE"
    if [ "$VERBOSE" = true ]; then
        echo "$OUTPUT"
    fi
elif [ $EXIT_CODE -eq 1 ]; then
    # Warnings only — log summary + warning lines
    echo "$TIMESTAMP $HOSTNAME_STR WARNINGS (exit 1)" >> "$LOG_FILE"
    echo "$CLEAN_OUTPUT" | grep -E "^(WARN|SUMMARY)" >> "$LOG_FILE"
    echo "---" >> "$LOG_FILE"
    if [ "$VERBOSE" = true ]; then
        echo "$OUTPUT"
    fi
else
    # Failures detected — log full output
    echo "$TIMESTAMP $HOSTNAME_STR NON-COMPLIANT (exit $EXIT_CODE)" >> "$LOG_FILE"
    echo "$CLEAN_OUTPUT" >> "$LOG_FILE"
    echo "---" >> "$LOG_FILE"
    # Print to stderr so cron sends an email
    echo "$OUTPUT" >&2
fi

exit $EXIT_CODE
