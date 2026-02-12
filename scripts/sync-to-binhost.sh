#!/bin/bash
# sync-to-binhost.sh - Rsync staging packages to binhost (Io)
# Usage: ./sync-to-binhost.sh [--dry-run] [--quiet]
# Designed to run from cron every 2 minutes.

set -euo pipefail

STAGING="/var/cache/binpkgs-staging"
BINHOST_PRIMARY="10.0.0.204"
BINHOST_PATH="/var/cache/binpkgs"
LOCKFILE="/tmp/sync-to-binhost.lock"
LOGFILE="/var/log/build-swarm/binhost-sync.log"
DRY_RUN=""
QUIET=""

for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN="--dry-run" ;;
        --quiet)   QUIET="1" ;;
    esac
done

log() { [[ -z "$QUIET" ]] && echo "$@"; echo "$(date '+%Y-%m-%d %H:%M:%S') $@" >> "$LOGFILE" 2>/dev/null || true; }

# Prevent overlapping runs
if [[ -f "$LOCKFILE" ]]; then
    LOCK_PID=$(cat "$LOCKFILE" 2>/dev/null)
    if kill -0 "$LOCK_PID" 2>/dev/null; then
        log "[SKIP] Previous sync still running (pid $LOCK_PID)"
        exit 0
    fi
    rm -f "$LOCKFILE"
fi
echo $$ > "$LOCKFILE"
trap "rm -f '$LOCKFILE'" EXIT

# Count packages to sync
PKG_COUNT=$(find "$STAGING" -name "*.gpkg.tar" 2>/dev/null | wc -l)

if [[ "$PKG_COUNT" -eq 0 ]]; then
    exit 0
fi

log "[SYNC] $PKG_COUNT packages in staging → $BINHOST_PRIMARY"

# Rsync with archive mode, preserving structure
rsync -az $DRY_RUN \
    --timeout=120 \
    -e 'ssh -o ConnectTimeout=10 -o ServerAliveInterval=15 -o BatchMode=yes' \
    --include='*/' \
    --include='*.gpkg.tar' \
    --include='Packages' \
    --exclude='*' \
    "$STAGING/" \
    "root@$BINHOST_PRIMARY:$BINHOST_PATH/" 2>&1 | { [[ -z "$QUIET" ]] && cat || true; }

# Regenerate Packages index on binhost
ssh -o ConnectTimeout=10 -o BatchMode=yes root@$BINHOST_PRIMARY \
    "cd $BINHOST_PATH && emaint binhost --fix 2>/dev/null || true" 2>&1 | { [[ -z "$QUIET" ]] && cat || true; }

log "[SYNC] Done — synced $PKG_COUNT packages to http://$BINHOST_PRIMARY/packages/"
