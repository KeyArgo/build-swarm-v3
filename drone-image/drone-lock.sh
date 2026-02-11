#!/bin/bash
# Build Swarm v3 — Drone Lockdown Script
# Locks down a drone to prevent accidental package bloat.
#
# What it does:
#   1. Generates /etc/portage/package.mask/drone-lockdown from drone.spec
#   2. Sets immutable flag (chattr +i) on critical portage files
#   3. Runs compliance check
#
# Usage:
#   drone-lock              # Lock everything down
#   drone-lock --status     # Show current lock status
#   drone-lock --force      # Lock even if compliance check fails
#
# To temporarily unlock for maintenance: drone-unlock

set -uo pipefail

SPEC_FILE="/etc/build-swarm/drone.spec"
MASK_FILE="/etc/portage/package.mask/drone-lockdown"
WORLD_FILE="/var/lib/portage/world"
MAKE_CONF="/etc/portage/make.conf"
PKG_USE="/etc/portage/package.use/swarm-drone"
PKG_KEYWORDS="/etc/portage/package.accept_keywords/swarm-drone"
LOCK_STATE="/etc/build-swarm/.lock-state"

RED='\033[31m'; GREEN='\033[32m'; YELLOW='\033[33m'
CYAN='\033[36m'; BOLD='\033[1m'; DIM='\033[2m'; RESET='\033[0m'

info()  { echo -e "${BOLD}${CYAN}[LOCK]${RESET}  $*"; }
ok()    { echo -e "${BOLD}${GREEN}[LOCK]${RESET}  $*"; }
warn()  { echo -e "${BOLD}${YELLOW}[LOCK]${RESET}  $*"; }
error() { echo -e "${BOLD}${RED}[LOCK]${RESET}  $*"; }

# ── Check if file is immutable ──
is_immutable() {
    local f="$1"
    [ -f "$f" ] && lsattr "$f" 2>/dev/null | grep -q '^....i' && return 0
    return 1
}

# ── Status check ──
show_status() {
    echo -e "${BOLD}${CYAN}=== Drone Lock Status ===${RESET}"
    echo ""

    # Package mask
    if [ -f "$MASK_FILE" ]; then
        local count
        count=$(grep -c '^[^#]' "$MASK_FILE" 2>/dev/null || echo 0)
        echo -e "  package.mask   ${GREEN}ACTIVE${RESET}  ($count patterns blocked)"
    else
        echo -e "  package.mask   ${RED}MISSING${RESET}  (forbidden packages can be installed!)"
    fi

    # Immutable files
    for f in "$WORLD_FILE" "$MAKE_CONF" "$MASK_FILE" "$PKG_USE" "$PKG_KEYWORDS"; do
        local name
        name=$(basename "$f")
        if [ ! -f "$f" ]; then
            echo -e "  $name   ${DIM}not found${RESET}"
        elif is_immutable "$f"; then
            echo -e "  $name   ${GREEN}LOCKED${RESET}  (immutable)"
        else
            echo -e "  $name   ${YELLOW}UNLOCKED${RESET}  (mutable)"
        fi
    done

    # Lock state file
    if [ -f "$LOCK_STATE" ]; then
        echo ""
        echo -e "  ${DIM}Lock state:${RESET} $(cat "$LOCK_STATE")"
    fi

    # Auto-relock timer
    if pgrep -f 'drone-lock.*--auto-relock' &>/dev/null; then
        echo -e "  ${YELLOW}Auto-relock timer is running${RESET}"
    fi

    echo ""
}

# ── Generate package.mask from drone.spec ──
generate_mask() {
    if [ ! -f "$SPEC_FILE" ]; then
        error "drone.spec not found at $SPEC_FILE"
        return 1
    fi

    mkdir -p /etc/portage/package.mask

    python3 -c "
import json, sys
with open('$SPEC_FILE') as f:
    spec = json.load(f)

patterns = spec.get('forbidden_patterns', [])
if not patterns:
    print('WARNING: No forbidden_patterns in spec', file=sys.stderr)
    sys.exit(1)

print('# Build Swarm v3 — Drone Lockdown')
print('# Auto-generated from drone.spec — DO NOT EDIT')
print('# To temporarily allow installs: drone-unlock')
print('# To regenerate: drone-lock')
print(f'# Generated: $(date -Iseconds)')
print(f'# Patterns: {len(patterns)}')
print()
print('# === Forbidden Package Categories ===')
print('# These packages have no business on a build drone.')
print('# If you need to install something here, run drone-unlock first.')
print()
for p in patterns:
    print(p)
" > "$MASK_FILE"

    return 0
}

# ── Main lock procedure ──
do_lock() {
    local force=false
    [[ "${1:-}" == "--force" ]] && force=true

    echo -e "${BOLD}${CYAN}=== Locking Drone ===${RESET}"
    echo ""

    # 1. Generate package.mask
    info "Generating package.mask from drone.spec..."
    if generate_mask; then
        local count
        count=$(grep -c '^[^#]' "$MASK_FILE" 2>/dev/null || echo 0)
        ok "package.mask installed ($count forbidden patterns)"
    else
        error "Failed to generate package.mask"
        [ "$force" = false ] && return 1
    fi

    # 2. Kill any auto-relock timers (we're locking now)
    pkill -f 'sleep.*drone-lock.*auto-relock' 2>/dev/null || true

    # 3. Set immutable flags on critical files
    info "Setting immutable flags on critical files..."

    local locked=0
    for f in "$WORLD_FILE" "$MAKE_CONF" "$MASK_FILE" "$PKG_USE" "$PKG_KEYWORDS"; do
        if [ -f "$f" ]; then
            # Remove immutable first (in case it's already set, chattr errors on re-set)
            chattr -i "$f" 2>/dev/null || true
            chattr +i "$f" 2>/dev/null
            if is_immutable "$f"; then
                ok "  $(basename "$f") -> immutable"
                ((locked++))
            else
                warn "  $(basename "$f") -> chattr failed (filesystem may not support it)"
            fi
        fi
    done

    # 4. Record lock state
    mkdir -p "$(dirname "$LOCK_STATE")"
    echo "locked $(date -Iseconds) by $(whoami) ($locked files immutable)" > "$LOCK_STATE"

    # 5. Run compliance check
    echo ""
    info "Running compliance check..."
    if [ -f /opt/build-swarm/comply.sh ]; then
        bash /opt/build-swarm/comply.sh --spec "$SPEC_FILE" 2>&1
        local rc=$?
        echo ""
        if [ $rc -eq 0 ]; then
            ok "Drone is COMPLIANT and LOCKED"
        elif [ $rc -eq 1 ]; then
            warn "Drone is LOCKED but has warnings"
        else
            if [ "$force" = true ]; then
                warn "Drone is LOCKED but NON-COMPLIANT (forced)"
            else
                error "Drone is NON-COMPLIANT — lock applied but issues remain"
                error "Run 'drone-lock --force' to lock anyway, or fix issues first"
            fi
        fi
    else
        warn "comply.sh not found, skipping compliance check"
    fi

    echo ""
    echo -e "${BOLD}${GREEN}Drone locked.${RESET} To unlock: ${CYAN}drone-unlock${RESET}"
}

# ── Parse args ──
case "${1:-}" in
    --status|-s)
        show_status
        ;;
    --force|-f)
        do_lock --force
        ;;
    --auto-relock)
        # Called by drone-unlock timer — silent lock
        do_lock --force &>/dev/null
        ;;
    -h|--help)
        echo "Usage: drone-lock [--status|--force|--help]"
        echo ""
        echo "Locks down the drone to prevent accidental package bloat."
        echo ""
        echo "Options:"
        echo "  --status   Show current lock status"
        echo "  --force    Lock even if compliance check fails"
        echo "  --help     Show this help"
        echo ""
        echo "What gets locked:"
        echo "  /etc/portage/package.mask/drone-lockdown  (blocks forbidden packages)"
        echo "  /var/lib/portage/world                     (prevents world file changes)"
        echo "  /etc/portage/make.conf                     (prevents config changes)"
        echo "  /etc/portage/package.use/swarm-drone       (prevents USE flag changes)"
        echo "  /etc/portage/package.accept_keywords/swarm-drone"
        ;;
    *)
        do_lock
        ;;
esac
