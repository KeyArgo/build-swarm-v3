#!/bin/bash
# Build Swarm v3 — Drone Unlock Script
# Temporarily unlocks a drone for maintenance (package installs, config changes).
#
# What it does:
#   1. Removes immutable flags from critical portage files
#   2. Moves package.mask aside (so forbidden packages CAN be installed)
#   3. Optionally sets an auto-relock timer
#
# Usage:
#   drone-unlock                  # Unlock until manually relocked
#   drone-unlock --timer 30       # Auto-relock after 30 minutes
#   drone-unlock --mask-only      # Keep immutable flags, just disable package.mask
#   drone-unlock --status         # Show current lock status
#
# To relock: drone-lock

set -uo pipefail

SPEC_FILE="/etc/build-swarm/drone.spec"
MASK_FILE="/etc/portage/package.mask/drone-lockdown"
MASK_BACKUP="/etc/portage/package.mask/.drone-lockdown.disabled"
WORLD_FILE="/var/lib/portage/world"
MAKE_CONF="/etc/portage/make.conf"
PKG_USE="/etc/portage/package.use/swarm-drone"
PKG_KEYWORDS="/etc/portage/package.accept_keywords/swarm-drone"
LOCK_STATE="/etc/build-swarm/.lock-state"

RED='\033[31m'; GREEN='\033[32m'; YELLOW='\033[33m'
CYAN='\033[36m'; BOLD='\033[1m'; DIM='\033[2m'; RESET='\033[0m'

info()  { echo -e "${BOLD}${YELLOW}[UNLOCK]${RESET}  $*"; }
ok()    { echo -e "${BOLD}${GREEN}[UNLOCK]${RESET}  $*"; }
warn()  { echo -e "${BOLD}${YELLOW}[UNLOCK]${RESET}  $*"; }
error() { echo -e "${BOLD}${RED}[UNLOCK]${RESET}  $*"; }

TIMER_MINUTES=0
MASK_ONLY=false

# ── Parse args ──
while [[ $# -gt 0 ]]; do
    case "$1" in
        --timer|-t)
            TIMER_MINUTES="$2"; shift 2 ;;
        --timer=*)
            TIMER_MINUTES="${1#--timer=}"; shift ;;
        --mask-only)
            MASK_ONLY=true; shift ;;
        --status|-s)
            # Delegate to drone-lock --status
            exec drone-lock --status
            ;;
        -h|--help)
            echo "Usage: drone-unlock [--timer MINUTES] [--mask-only] [--status]"
            echo ""
            echo "Temporarily unlocks the drone for maintenance."
            echo ""
            echo "Options:"
            echo "  --timer N    Auto-relock after N minutes (default: no auto-relock)"
            echo "  --mask-only  Only disable package.mask, keep immutable flags on other files"
            echo "  --status     Show current lock status"
            echo "  --help       Show this help"
            echo ""
            echo "Examples:"
            echo "  drone-unlock                  # Unlock until you run drone-lock"
            echo "  drone-unlock --timer 30       # Unlock for 30 minutes"
            echo "  drone-unlock --mask-only      # Just let forbidden packages through"
            echo ""
            echo "To relock: drone-lock"
            exit 0
            ;;
        *)
            error "Unknown option: $1"; exit 1 ;;
    esac
done

# ── Confirmation ──
echo ""
echo -e "${BOLD}${YELLOW}=== Unlocking Drone ===${RESET}"
echo ""
echo -e "  ${YELLOW}WARNING: This removes bloat protection from this drone.${RESET}"
echo -e "  ${DIM}Forbidden packages (KDE, Firefox, Docker, games, etc.) will be installable.${RESET}"
echo -e "  ${DIM}Critical portage files will be writable.${RESET}"
echo ""

if [ "$TIMER_MINUTES" -gt 0 ]; then
    echo -e "  ${CYAN}Auto-relock in ${TIMER_MINUTES} minutes.${RESET}"
else
    echo -e "  ${YELLOW}No auto-relock. Remember to run: drone-lock${RESET}"
fi

echo ""

# Don't prompt if we're in a non-interactive shell (piped via SSH etc.)
if [ -t 0 ]; then
    read -rp "Proceed? [y/N] " confirm
    if [[ ! "$confirm" =~ ^[Yy]$ ]]; then
        echo "Aborted."
        exit 0
    fi
fi

echo ""

# ── Remove immutable flags ──
if [ "$MASK_ONLY" = false ]; then
    info "Removing immutable flags..."
    for f in "$WORLD_FILE" "$MAKE_CONF" "$MASK_FILE" "$PKG_USE" "$PKG_KEYWORDS"; do
        if [ -f "$f" ]; then
            chattr -i "$f" 2>/dev/null
            ok "  $(basename "$f") -> mutable"
        fi
    done
else
    info "Mask-only mode: keeping immutable flags on config files"
    # Only unlock the mask file
    if [ -f "$MASK_FILE" ]; then
        chattr -i "$MASK_FILE" 2>/dev/null
    fi
fi

# ── Disable package.mask ──
info "Disabling package.mask..."
if [ -f "$MASK_FILE" ]; then
    mv "$MASK_FILE" "$MASK_BACKUP"
    ok "package.mask moved to $(basename "$MASK_BACKUP")"
    ok "Forbidden packages are now installable"
else
    warn "package.mask was not present"
fi

# ── Record unlock state ──
mkdir -p "$(dirname "$LOCK_STATE")"
if [ "$TIMER_MINUTES" -gt 0 ]; then
    echo "unlocked $(date -Iseconds) by $(whoami) (auto-relock in ${TIMER_MINUTES}m)" > "$LOCK_STATE"
else
    echo "unlocked $(date -Iseconds) by $(whoami) (manual relock required)" > "$LOCK_STATE"
fi

# ── Set auto-relock timer ──
if [ "$TIMER_MINUTES" -gt 0 ]; then
    info "Setting auto-relock timer for ${TIMER_MINUTES} minutes..."

    # Kill any existing auto-relock timer
    pkill -f 'sleep.*drone-lock.*auto-relock' 2>/dev/null || true

    # Background a sleep + relock
    (
        sleep $((TIMER_MINUTES * 60))
        drone-lock --auto-relock
    ) &>/dev/null &
    disown

    ok "Auto-relock scheduled (PID: $!)"
fi

# ── Summary ──
echo ""
echo -e "${BOLD}${GREEN}Drone unlocked.${RESET}"
echo ""
echo -e "  You can now:"
echo -e "    ${CYAN}emerge <package>${RESET}          Install packages"
echo -e "    ${CYAN}nano /etc/portage/make.conf${RESET}  Edit portage config"
echo -e "    ${CYAN}echo 'pkg' >> /var/lib/portage/world${RESET}  Add world atoms"
echo ""
echo -e "  When done: ${BOLD}${CYAN}drone-lock${RESET}"
if [ "$TIMER_MINUTES" -gt 0 ]; then
    echo -e "  Or wait ${TIMER_MINUTES} minutes for auto-relock."
fi
echo ""
