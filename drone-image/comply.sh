#!/bin/bash
# Build Swarm v3 — Drone Compliance Checker
# Audits a running system against the drone image spec.
#
# Usage:
#   ./comply.sh                          # reads spec from stdin or embedded
#   ./comply.sh --spec /path/to/drone.spec
#   echo '{"profile":"..."}' | ./comply.sh
#
# Exit codes:
#   0 = fully compliant
#   1 = warnings only
#   2 = failures detected

set -uo pipefail

# ── Colors (disabled if not a terminal) ──
if [ -t 1 ]; then
    RED='\033[31m'; GREEN='\033[32m'; YELLOW='\033[33m'
    CYAN='\033[36m'; BOLD='\033[1m'; DIM='\033[2m'; RESET='\033[0m'
else
    RED=''; GREEN=''; YELLOW=''; CYAN=''; BOLD=''; DIM=''; RESET=''
fi

PASS_COUNT=0
WARN_COUNT=0
FAIL_COUNT=0

pass() { echo -e "${GREEN}PASS${RESET}  $1"; ((PASS_COUNT++)); }
warn() { echo -e "${YELLOW}WARN${RESET}  $1"; ((WARN_COUNT++)); }
fail() { echo -e "${RED}FAIL${RESET}  $1"; ((FAIL_COUNT++)); }

# ── Load spec ──
SPEC_FILE=""
for arg in "$@"; do
    case "$arg" in
        --spec=*) SPEC_FILE="${arg#--spec=}" ;;
        --spec)   shift_next=1 ;;
        *)        [ "${shift_next:-0}" = "1" ] && SPEC_FILE="$arg" && shift_next=0 ;;
    esac
done

# Read spec JSON — from file, stdin, or embedded default
if [ -n "$SPEC_FILE" ] && [ -f "$SPEC_FILE" ]; then
    SPEC=$(cat "$SPEC_FILE")
elif [ -f /etc/build-swarm/drone.spec ]; then
    SPEC=$(cat /etc/build-swarm/drone.spec)
elif [ ! -t 0 ]; then
    SPEC=$(cat)
else
    echo "Error: No spec file found. Use --spec <path> or pipe JSON to stdin."
    exit 2
fi

# Simple JSON value extractor (no jq dependency)
# Usage: json_val "$JSON" "key"
json_val() {
    python3 -c "import json,sys; d=json.loads(sys.stdin.read()); print(d.get('$2',''))" <<< "$1" 2>/dev/null
}

# Usage: json_array "$JSON" "key" — prints one item per line
json_array() {
    python3 -c "
import json,sys
d=json.loads(sys.stdin.read())
for item in d.get('$2', []):
    print(item)
" <<< "$1" 2>/dev/null
}

echo -e "${BOLD}${CYAN}=== Build Swarm v3 Drone Compliance Check ===${RESET}"
echo -e "${DIM}$(date -Iseconds) on $(hostname)${RESET}"
echo ""

# ── 1. Profile ──
EXPECTED_PROFILE=$(json_val "$SPEC" "profile")
CURRENT_PROFILE=$(eselect profile show 2>/dev/null | tail -1 | tr -d ' ')

if [ "$CURRENT_PROFILE" = "$EXPECTED_PROFILE" ]; then
    pass "profile        $CURRENT_PROFILE"
else
    fail "profile        $CURRENT_PROFILE (expected: $EXPECTED_PROFILE)"
fi

# ── 2. Package count ──
PKG_COUNT=$(ls -d /var/db/pkg/*/* 2>/dev/null | wc -l)
MAX_PKGS=$(json_val "$SPEC" "max_packages")
WARN_PKGS=$(json_val "$SPEC" "warn_packages")
MAX_PKGS=${MAX_PKGS:-400}
WARN_PKGS=${WARN_PKGS:-350}

if [ "$PKG_COUNT" -le "$WARN_PKGS" ]; then
    pass "packages       $PKG_COUNT installed (limit: $MAX_PKGS)"
elif [ "$PKG_COUNT" -le "$MAX_PKGS" ]; then
    warn "packages       $PKG_COUNT installed (warn: $WARN_PKGS, limit: $MAX_PKGS)"
else
    fail "packages       $PKG_COUNT installed (limit: $MAX_PKGS)"
fi

# ── 3. World file ──
WORLD_FILE="/var/lib/portage/world"
if [ -f "$WORLD_FILE" ]; then
    EXTRA_ATOMS=()
    MISSING_ATOMS=()

    # Check for expected atoms
    while IFS= read -r atom; do
        [ -z "$atom" ] && continue
        if ! grep -q "^${atom}$" "$WORLD_FILE" 2>/dev/null; then
            MISSING_ATOMS+=("$atom")
        fi
    done < <(json_array "$SPEC" "world_packages")

    # Check for extra atoms
    while IFS= read -r atom; do
        [ -z "$atom" ] && continue
        [[ "$atom" =~ ^# ]] && continue
        if ! json_array "$SPEC" "world_packages" | grep -q "^${atom}$"; then
            EXTRA_ATOMS+=("$atom")
        fi
    done < "$WORLD_FILE"

    if [ ${#MISSING_ATOMS[@]} -gt 0 ]; then
        fail "world_missing  ${#MISSING_ATOMS[@]} missing: ${MISSING_ATOMS[*]}"
    fi
    if [ ${#EXTRA_ATOMS[@]} -gt 0 ]; then
        warn "world_extra    ${#EXTRA_ATOMS[@]} extra: ${EXTRA_ATOMS[*]}"
    fi
    if [ ${#MISSING_ATOMS[@]} -eq 0 ] && [ ${#EXTRA_ATOMS[@]} -eq 0 ]; then
        pass "world_file     matches spec exactly"
    fi
else
    fail "world_file     /var/lib/portage/world not found"
fi

# ── 4. Forbidden packages ──
FORBIDDEN_FOUND=()
while IFS= read -r pattern; do
    [ -z "$pattern" ] && continue
    # Convert glob to directory check
    category="${pattern%%/*}"
    pkg_glob="${pattern##*/}"

    if [ "$pkg_glob" = "*" ]; then
        # Category glob: check if any packages in category
        matches=$(ls -d /var/db/pkg/${category}/* 2>/dev/null | head -5)
    else
        matches=$(ls -d /var/db/pkg/${category}/${pkg_glob}* 2>/dev/null | head -5)
    fi

    if [ -n "$matches" ]; then
        for m in $matches; do
            pkg_name=$(echo "$m" | sed 's|/var/db/pkg/||')
            FORBIDDEN_FOUND+=("$pkg_name")
        done
    fi
done < <(json_array "$SPEC" "forbidden_patterns")

if [ ${#FORBIDDEN_FOUND[@]} -eq 0 ]; then
    pass "forbidden      no forbidden packages found"
else
    for pkg in "${FORBIDDEN_FOUND[@]}"; do
        fail "forbidden      $pkg is installed"
    done
fi

# ── 5. Required commands ──
MISSING_CMDS=()
while IFS= read -r cmd; do
    [ -z "$cmd" ] && continue
    if ! command -v "$cmd" &>/dev/null; then
        MISSING_CMDS+=("$cmd")
    fi
done < <(json_array "$SPEC" "required_commands")

if [ ${#MISSING_CMDS[@]} -eq 0 ]; then
    pass "commands       all required commands found"
else
    fail "commands       missing: ${MISSING_CMDS[*]}"
fi

# ── 6. Required directories ──
MISSING_DIRS=()
while IFS= read -r dir; do
    [ -z "$dir" ] && continue
    if [ ! -d "$dir" ]; then
        MISSING_DIRS+=("$dir")
    fi
done < <(json_array "$SPEC" "required_dirs")

if [ ${#MISSING_DIRS[@]} -eq 0 ]; then
    pass "directories    all required directories exist"
else
    fail "directories    missing: ${MISSING_DIRS[*]}"
fi

# ── 7. Required files ──
MISSING_FILES=()
while IFS= read -r f; do
    [ -z "$f" ] && continue
    if [ ! -f "$f" ]; then
        MISSING_FILES+=("$f")
    fi
done < <(json_array "$SPEC" "required_files")

if [ ${#MISSING_FILES[@]} -eq 0 ]; then
    pass "files          all required files exist"
else
    for f in "${MISSING_FILES[@]}"; do
        fail "files          missing: $f"
    done
fi

# ── 8. Required services ──
while IFS= read -r svc; do
    [ -z "$svc" ] && continue
    if [ -f "/etc/init.d/$svc" ]; then
        if rc-service "$svc" status &>/dev/null; then
            pass "service        $svc is running"
        else
            warn "service        $svc is installed but not running"
        fi
    else
        fail "service        $svc is not installed"
    fi
done < <(json_array "$SPEC" "required_services")

# ── 9. make.conf FEATURES check ──
if [ -f /etc/portage/make.conf ]; then
    CURRENT_FEATURES=$(python3 -c "
import re
with open('/etc/portage/make.conf') as f:
    for line in f:
        m = re.match(r'FEATURES=[\"'\''](.*)[\"'\'']', line.strip())
        if m:
            print(m.group(1))
            break
" 2>/dev/null)

    MISSING_FEATURES=()
    while IFS= read -r feat; do
        [ -z "$feat" ] && continue
        if ! echo "$CURRENT_FEATURES" | grep -q "$feat"; then
            MISSING_FEATURES+=("$feat")
        fi
    done < <(json_array "$SPEC" "make_conf_required_features")

    if [ ${#MISSING_FEATURES[@]} -eq 0 ]; then
        pass "make_conf      required FEATURES present"
    else
        fail "make_conf      missing FEATURES: ${MISSING_FEATURES[*]}"
    fi
else
    fail "make_conf      /etc/portage/make.conf not found"
fi

# ── 10. Portage tree freshness ──
TIMESTAMP_FILE="/var/db/repos/gentoo/metadata/timestamp.chk"
if [ -f "$TIMESTAMP_FILE" ]; then
    TREE_AGE_DAYS=$(( ($(date +%s) - $(date -d "$(cat "$TIMESTAMP_FILE")" +%s 2>/dev/null || echo 0)) / 86400 ))
    if [ "$TREE_AGE_DAYS" -le 7 ]; then
        pass "portage_tree   ${TREE_AGE_DAYS} days old"
    else
        warn "portage_tree   ${TREE_AGE_DAYS} days old (>7 days)"
    fi
else
    warn "portage_tree   timestamp not found"
fi

# ── 11. Bloat protection (package.mask + immutable files) ──
MASK_FILE="/etc/portage/package.mask/drone-lockdown"
LOCK_STATE_FILE="/etc/build-swarm/.lock-state"

# Check package.mask exists
if [ -f "$MASK_FILE" ]; then
    MASK_PATTERNS=$(grep -c '^[^#]' "$MASK_FILE" 2>/dev/null || echo 0)
    pass "package_mask   active ($MASK_PATTERNS forbidden patterns)"
else
    fail "package_mask   MISSING — forbidden packages can be installed! Run: drone-lock"
fi

# Check immutable flags on critical files
IMMUTABLE_COUNT=0
IMMUTABLE_EXPECTED=0
for f in "$WORLD_FILE" /etc/portage/make.conf "$MASK_FILE" /etc/portage/package.use/swarm-drone /etc/portage/package.accept_keywords/swarm-drone; do
    [ -f "$f" ] || continue
    ((IMMUTABLE_EXPECTED++))
    if lsattr "$f" 2>/dev/null | grep -q '^....i'; then
        ((IMMUTABLE_COUNT++))
    fi
done

if [ "$IMMUTABLE_COUNT" -eq "$IMMUTABLE_EXPECTED" ] && [ "$IMMUTABLE_EXPECTED" -gt 0 ]; then
    pass "immutable      all $IMMUTABLE_COUNT critical files locked"
elif [ "$IMMUTABLE_COUNT" -gt 0 ]; then
    warn "immutable      $IMMUTABLE_COUNT/$IMMUTABLE_EXPECTED files locked (partially unlocked)"
else
    fail "immutable      NO files locked — drone is UNPROTECTED! Run: drone-lock"
fi

# Check lock state
if [ -f "$LOCK_STATE_FILE" ]; then
    LOCK_STATUS=$(cat "$LOCK_STATE_FILE")
    if echo "$LOCK_STATUS" | grep -q '^unlocked'; then
        warn "lock_state     $LOCK_STATUS"
    else
        pass "lock_state     $LOCK_STATUS"
    fi
fi

# ── Summary ──
echo ""
TOTAL=$((PASS_COUNT + WARN_COUNT + FAIL_COUNT))
echo -e "${BOLD}SUMMARY:${RESET} ${GREEN}${PASS_COUNT} PASS${RESET}, ${YELLOW}${WARN_COUNT} WARN${RESET}, ${RED}${FAIL_COUNT} FAIL${RESET}  (${TOTAL} checks)"

if [ "$FAIL_COUNT" -gt 0 ]; then
    exit 2
elif [ "$WARN_COUNT" -gt 0 ]; then
    exit 1
else
    exit 0
fi
