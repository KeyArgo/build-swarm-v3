#!/bin/bash
# Build Swarm v3 — Drone Bootstrap Script
# Takes a fresh stage3 (or existing system) → working build drone.
# Idempotent: safe to run multiple times.
#
# Usage:
#   ./bootstrap.sh --cp-url http://10.0.0.199:8100 --name drone-new
#   ./bootstrap.sh --cp-url http://10.0.0.199:8100 --prune    # also remove extra packages
#   ./bootstrap.sh --cp-url http://10.0.0.199:8100 --dry-run  # show what would change
#
# Works on: bare metal, LXC containers, Proxmox/QEMU VMs

set -euo pipefail

# ── Defaults ──
CP_URL=""
DRONE_NAME="${HOSTNAME:-drone-$(cat /etc/machine-id 2>/dev/null | head -c 8 || echo unknown)}"
GATEWAY_URL=""  # v2 compatibility
DO_PRUNE=false
DRY_RUN=false
DO_SYNC=true
SWARM_DRONE_BIN=""

# ── Colors ──
RED='\033[31m'; GREEN='\033[32m'; YELLOW='\033[33m'
CYAN='\033[36m'; BOLD='\033[1m'; DIM='\033[2m'; RESET='\033[0m'

info()  { echo -e "${BOLD}${CYAN}[INFO]${RESET}  $*"; }
ok()    { echo -e "${BOLD}${GREEN}[OK]${RESET}    $*"; }
warn()  { echo -e "${BOLD}${YELLOW}[WARN]${RESET}  $*"; }
error() { echo -e "${BOLD}${RED}[ERROR]${RESET} $*"; }
step()  { echo -e "\n${BOLD}── $* ──${RESET}"; }

# ── Parse arguments ──
while [[ $# -gt 0 ]]; do
    case "$1" in
        --cp-url|--control-plane)   CP_URL="$2"; shift 2 ;;
        --cp-url=*)                 CP_URL="${1#--cp-url=}"; shift ;;
        --gateway-url)              GATEWAY_URL="$2"; shift 2 ;;
        --name)                     DRONE_NAME="$2"; shift 2 ;;
        --name=*)                   DRONE_NAME="${1#--name=}"; shift ;;
        --prune)                    DO_PRUNE=true; shift ;;
        --dry-run)                  DRY_RUN=true; shift ;;
        --no-sync)                  DO_SYNC=false; shift ;;
        --drone-bin)                SWARM_DRONE_BIN="$2"; shift 2 ;;
        -h|--help)
            echo "Usage: $0 --cp-url <URL> [--name NAME] [--prune] [--dry-run]"
            echo ""
            echo "Options:"
            echo "  --cp-url URL    Control plane URL (e.g. http://10.0.0.199:8100)"
            echo "  --gateway-url   V2 gateway URL (default: derived from cp-url)"
            echo "  --name NAME     Drone name (default: hostname)"
            echo "  --prune         Remove packages not in the spec (emerge --depclean)"
            echo "  --dry-run       Show what would change without doing it"
            echo "  --no-sync       Skip portage tree sync"
            echo "  --drone-bin     Path to swarm-drone binary to install"
            exit 0
            ;;
        *)  error "Unknown option: $1"; exit 1 ;;
    esac
done

if [ -z "$CP_URL" ]; then
    error "Missing --cp-url. Usage: $0 --cp-url http://10.0.0.199:8100"
    exit 1
fi

# Derive v2 gateway URL from control plane URL if not specified
if [ -z "$GATEWAY_URL" ]; then
    GATEWAY_HOST=$(echo "$CP_URL" | sed 's|http://||;s|:.*||')
    GATEWAY_URL="http://${GATEWAY_HOST}:8100"
fi

echo -e "${BOLD}${CYAN}=== Build Swarm v3 — Drone Bootstrap ===${RESET}"
echo -e "  ${DIM}Control Plane:${RESET} ${CYAN}$CP_URL${RESET}"
echo -e "  ${DIM}Drone Name:${RESET}    ${CYAN}$DRONE_NAME${RESET}"
echo -e "  ${DIM}Prune:${RESET}         ${CYAN}$DO_PRUNE${RESET}"
echo -e "  ${DIM}Dry Run:${RESET}       ${CYAN}$DRY_RUN${RESET}"
echo -e "  ${DIM}Date:${RESET}          ${CYAN}$(date -Iseconds)${RESET}"

# ── 1. Detect environment ──
step "1/10 Detecting environment"

ENV_TYPE="bare-metal"
if grep -qa 'container=lxc' /proc/1/environ 2>/dev/null; then
    ENV_TYPE="lxc"
elif [ -f /sys/class/dmi/id/product_name ]; then
    PRODUCT=$(cat /sys/class/dmi/id/product_name 2>/dev/null || true)
    case "$PRODUCT" in
        *QEMU*|*KVM*|*Virtual*|*Proxmox*) ENV_TYPE="vm" ;;
    esac
fi

CORES=$(nproc)
RAM_GB=$(awk '/MemTotal/{printf "%.0f", $2/1048576}' /proc/meminfo)
DISK_FREE=$(df -BG / | awk 'NR==2{print $4}' | tr -d 'G')

info "Environment: $ENV_TYPE"
info "CPU cores: $CORES, RAM: ${RAM_GB}GB, Disk free: ${DISK_FREE}GB"

if [ "$DISK_FREE" -lt 20 ]; then
    warn "Low disk space (${DISK_FREE}GB free). Builds may fail."
fi

# ── 2. Set profile ──
step "2/10 Setting Gentoo profile"

TARGET_PROFILE="default/linux/amd64/23.0"
CURRENT_PROFILE=$(eselect profile show 2>/dev/null | tail -1 | tr -d ' ')

if [ "$CURRENT_PROFILE" = "$TARGET_PROFILE" ]; then
    ok "Profile already set to $TARGET_PROFILE"
else
    warn "Current profile: $CURRENT_PROFILE"
    info "Switching to: $TARGET_PROFILE"
    if [ "$DRY_RUN" = true ]; then
        info "[DRY RUN] Would run: eselect profile set $TARGET_PROFILE"
    else
        # Find the profile number
        PROFILE_NUM=$(eselect profile list | grep "$TARGET_PROFILE" | grep -v desktop | grep -v systemd | head -1 | sed 's/.*\[//;s/\].*//' | tr -d ' ')
        if [ -n "$PROFILE_NUM" ]; then
            eselect profile set "$PROFILE_NUM"
            ok "Profile set to $TARGET_PROFILE"
        else
            error "Could not find profile $TARGET_PROFILE"
            exit 1
        fi
    fi
fi

# ── 3. Install make.conf ──
step "3/10 Installing make.conf"

MAKE_CONF="/etc/portage/make.conf"

# The make.conf template is embedded here by the provisioner
# (or read from __MAKE_CONF__ placeholder)
MAKE_CONF_CONTENT=$(cat <<'MAKECONF_EOF'
__MAKE_CONF__
MAKECONF_EOF
)

# If __MAKE_CONF__ wasn't substituted, use the inline default
if echo "$MAKE_CONF_CONTENT" | grep -q '__MAKE_CONF__'; then
    MAKE_CONF_CONTENT='# Build Swarm v3 Drone — /etc/portage/make.conf
COMMON_FLAGS="-O2 -pipe -march=x86-64"
CFLAGS="${COMMON_FLAGS}"
CXXFLAGS="${COMMON_FLAGS}"
FCFLAGS="${COMMON_FLAGS}"
FFLAGS="${COMMON_FLAGS}"
MAKEOPTS="-j__CORES__ -l__CORES__"
EMERGE_DEFAULT_OPTS="--jobs=2 --load-average=__CORES__"
FEATURES="buildpkg fail-clean parallel-fetch -getbinpkg -binpkg-multi-instance"
BINPKG_FORMAT="gpkg"
USE="-systemd elogind -X -wayland -gui -desktop -bluetooth -cups -pulseaudio"
L10N="en-US en"
LINGUAS="en"
VIDEO_CARDS="nvidia radeonsi intel"
INPUT_DEVICES="libinput"
ACCEPT_LICENSE="*"
ACCEPT_KEYWORDS="amd64"
PORTDIR="/var/db/repos/gentoo"
DISTDIR="/var/cache/distfiles"
PKGDIR="/var/cache/binpkgs"'
fi

# Substitute __CORES__ with actual core count
MAKE_CONF_FINAL=$(echo "$MAKE_CONF_CONTENT" | sed "s/__CORES__/$CORES/g")

if [ -f "$MAKE_CONF" ]; then
    if diff <(echo "$MAKE_CONF_FINAL") "$MAKE_CONF" &>/dev/null; then
        ok "make.conf already matches spec"
    else
        info "make.conf differs from spec"
        if [ "$DRY_RUN" = true ]; then
            info "[DRY RUN] Would overwrite $MAKE_CONF"
            diff <(echo "$MAKE_CONF_FINAL") "$MAKE_CONF" || true
        else
            BACKUP="${MAKE_CONF}.pre-swarm.$(date +%Y%m%d)"
            cp "$MAKE_CONF" "$BACKUP"
            info "Backed up to $BACKUP"
            echo "$MAKE_CONF_FINAL" > "$MAKE_CONF"
            ok "make.conf updated"
        fi
    fi
else
    if [ "$DRY_RUN" = true ]; then
        info "[DRY RUN] Would create $MAKE_CONF"
    else
        echo "$MAKE_CONF_FINAL" > "$MAKE_CONF"
        ok "make.conf created"
    fi
fi

# ── 4. Install package.use / package.accept_keywords ──
step "4/10 Installing portage package config"

mkdir -p /etc/portage/package.use /etc/portage/package.accept_keywords

# Package USE flags (embedded by provisioner or inline default)
PKG_USE_CONTENT=$(cat <<'PKGUSE_EOF'
__PACKAGE_USE__
PKGUSE_EOF
)

if echo "$PKG_USE_CONTENT" | grep -q '__PACKAGE_USE__'; then
    PKG_USE_CONTENT='# Build Swarm v3 — package.use/swarm-drone
*/* -systemd elogind
sys-libs/zlib minizip
sys-fs/fuse suid
media-libs/libsdl2 gles2
media-libs/freetype harfbuzz
dev-qt/qtbase libproxy opengl wayland icu
dev-qt/qttools opengl
dev-qt/qtdeclarative opengl
dev-qt/qtmultimedia opengl
dev-qt/qt5compat qml icu
dev-qt/qtgui egl
kde-frameworks/kwindowsystem wayland X
kde-frameworks/kconfig qml dbus
>=kde-frameworks/kguiaddons-6.22.1 wayland
>=kde-frameworks/kidletime-6.22.0 wayland
kde-plasma/kwin lock
x11-libs/libxkbcommon X
x11-libs/libdrm video_cards_amdgpu video_cards_radeon
media-libs/mesa wayland
x11-base/xwayland libei
>=x11-libs/cairo-1.18.4-r1 X
>=media-libs/libglvnd-1.7.0 X
sys-kernel/installkernel dracut
media-video/ffmpeg vpx opus
media-plugins/alsa-plugins pulseaudio
>=media-libs/babl-0.1.118 lcms
>=media-libs/gegl-0.4.66 lcms cairo
>=net-wireless/wpa_supplicant-2.11-r4 dbus
>=dev-libs/qcoro-0.12.0 dbus
dev-build/make -*
app-text/xmlto text'
fi

PKG_KEYWORDS_CONTENT=$(cat <<'PKGKW_EOF'
__PACKAGE_KEYWORDS__
PKGKW_EOF
)

if echo "$PKG_KEYWORDS_CONTENT" | grep -q '__PACKAGE_KEYWORDS__'; then
    PKG_KEYWORDS_CONTENT='# Build Swarm v3 — package.accept_keywords/swarm-drone
dev-util/google-antigravity ~amd64
sys-kernel/installkernel ~amd64'
fi

if [ "$DRY_RUN" = true ]; then
    info "[DRY RUN] Would write /etc/portage/package.use/swarm-drone"
    info "[DRY RUN] Would write /etc/portage/package.accept_keywords/swarm-drone"
else
    echo "$PKG_USE_CONTENT" > /etc/portage/package.use/swarm-drone
    echo "$PKG_KEYWORDS_CONTENT" > /etc/portage/package.accept_keywords/swarm-drone
    ok "Portage package config installed"
fi

# ── 5. Sync portage tree ──
step "5/10 Syncing portage tree"

if [ "$DO_SYNC" = false ]; then
    info "Skipping portage sync (--no-sync)"
elif [ "$DRY_RUN" = true ]; then
    info "[DRY RUN] Would sync portage tree"
else
    if [ -d /var/db/repos/gentoo/metadata ]; then
        TREE_AGE_DAYS=$(( ($(date +%s) - $(stat -c %Y /var/db/repos/gentoo/metadata/timestamp.chk 2>/dev/null || echo 0)) / 86400 ))
        if [ "$TREE_AGE_DAYS" -le 1 ]; then
            ok "Portage tree is fresh (${TREE_AGE_DAYS} days old)"
        else
            info "Portage tree is ${TREE_AGE_DAYS} days old, syncing..."
            emerge --sync --quiet 2>/dev/null || emerge-webrsync 2>/dev/null || warn "Sync failed, continuing with existing tree"
            ok "Portage tree synced"
        fi
    else
        info "No portage tree found, running emerge-webrsync..."
        emerge-webrsync 2>/dev/null
        ok "Portage tree installed"
    fi
fi

# ── 6. Set world file ──
step "6/10 Setting @world file"

WORLD_FILE="/var/lib/portage/world"

# Package list (embedded by provisioner or inline default)
PACKAGE_LIST_CONTENT=$(cat <<'PKGLIST_EOF'
__PACKAGE_LIST__
PKGLIST_EOF
)

if echo "$PACKAGE_LIST_CONTENT" | grep -q '__PACKAGE_LIST__'; then
    PACKAGE_LIST_CONTENT='sys-apps/portage
sys-devel/gcc
sys-devel/binutils
sys-libs/glibc
dev-lang/python
net-misc/rsync
net-misc/openssh
app-misc/screen
app-portage/gentoolkit
sys-apps/openrc'
fi

# Filter comments and empty lines
WORLD_ATOMS=$(echo "$PACKAGE_LIST_CONTENT" | grep -v '^#' | grep -v '^$' | sort)

if [ -f "$WORLD_FILE" ]; then
    CURRENT_WORLD=$(sort "$WORLD_FILE")
    if [ "$CURRENT_WORLD" = "$WORLD_ATOMS" ]; then
        ok "World file already matches spec"
    else
        EXTRA=$(comm -23 <(echo "$CURRENT_WORLD") <(echo "$WORLD_ATOMS") | wc -l)
        MISSING=$(comm -13 <(echo "$CURRENT_WORLD") <(echo "$WORLD_ATOMS") | wc -l)
        info "World file differs: $EXTRA extra atoms, $MISSING missing atoms"
        if [ "$DRY_RUN" = true ]; then
            info "[DRY RUN] Extra atoms that would be removed from world:"
            comm -23 <(echo "$CURRENT_WORLD") <(echo "$WORLD_ATOMS") | while read -r a; do echo "  - $a"; done
        else
            cp "$WORLD_FILE" "${WORLD_FILE}.pre-swarm.$(date +%Y%m%d)"
            echo "$WORLD_ATOMS" > "$WORLD_FILE"
            ok "World file updated (backup saved)"
        fi
    fi
else
    if [ "$DRY_RUN" = true ]; then
        info "[DRY RUN] Would create $WORLD_FILE with $(echo "$WORLD_ATOMS" | wc -l) atoms"
    else
        mkdir -p "$(dirname "$WORLD_FILE")"
        echo "$WORLD_ATOMS" > "$WORLD_FILE"
        ok "World file created"
    fi
fi

# ── 7. Update system ──
step "7/10 Updating @world"

if [ "$DRY_RUN" = true ]; then
    info "[DRY RUN] Would run: emerge --update --newuse --deep @world"
else
    info "Running emerge --update --newuse --deep @world ..."
    info "(This may take a while on first run)"
    emerge --update --newuse --deep --quiet @world 2>&1 || {
        warn "emerge @world had errors (non-fatal, continuing)"
    }
    ok "System updated"
fi

# ── 8. Prune extra packages ──
step "8/10 Pruning extra packages"

if [ "$DO_PRUNE" = true ]; then
    if [ "$DRY_RUN" = true ]; then
        info "[DRY RUN] Packages that would be removed:"
        emerge --depclean --pretend 2>&1 | tail -20
    else
        info "Running emerge --depclean ..."
        PKG_BEFORE=$(ls -d /var/db/pkg/*/* 2>/dev/null | wc -l)
        emerge --depclean --quiet 2>&1 || warn "depclean had warnings"
        PKG_AFTER=$(ls -d /var/db/pkg/*/* 2>/dev/null | wc -l)
        REMOVED=$((PKG_BEFORE - PKG_AFTER))
        ok "Removed $REMOVED packages ($PKG_BEFORE -> $PKG_AFTER)"
    fi
else
    info "Skipping prune (use --prune to remove extra packages)"
    WOULD_REMOVE=$(emerge --depclean --pretend 2>&1 | grep "^Number to remove:" | awk '{print $NF}' || echo "?")
    if [ "$WOULD_REMOVE" != "?" ] && [ "$WOULD_REMOVE" != "0" ]; then
        warn "$WOULD_REMOVE packages could be removed with --prune"
    fi
fi

# ── 9. Install drone agent ──
step "9/10 Installing drone agent"

mkdir -p /opt/build-swarm/bin /opt/build-swarm/lib
mkdir -p /etc/build-swarm
mkdir -p /var/log/build-swarm
mkdir -p /var/lib/build-swarm
mkdir -p /var/cache/binpkgs /var/cache/distfiles

# Write drone config
DRONE_ID=$(cat /etc/machine-id 2>/dev/null || hostname | md5sum | cut -d' ' -f1)
DRONE_CONF="/etc/build-swarm/drone.conf"

if [ "$DRY_RUN" = true ]; then
    info "[DRY RUN] Would write $DRONE_CONF"
else
    cat > "$DRONE_CONF" <<DRONECONF
# Build Swarm v3 Drone Configuration
# Generated by bootstrap.sh on $(date -Iseconds)
GATEWAY_URL="$GATEWAY_URL"
HEARTBEAT_INTERVAL=30
POLL_INTERVAL=30
LOG_FILE=/var/log/build-swarm/drone.log
AUTO_REBOOT=true
DEBUG=0
NODE_NAME="$DRONE_NAME"
# ORCHESTRATOR_IP is assigned automatically by the v3 control plane
# Uncomment and set for v2 compatibility:
# ORCHESTRATOR_IP=10.0.0.201
DRONECONF
    ok "Drone config written to $DRONE_CONF"
fi

# Copy drone binary if specified or available
if [ -n "$SWARM_DRONE_BIN" ] && [ -f "$SWARM_DRONE_BIN" ]; then
    if [ "$DRY_RUN" = false ]; then
        cp "$SWARM_DRONE_BIN" /opt/build-swarm/bin/swarm-drone
        chmod +x /opt/build-swarm/bin/swarm-drone
        ok "Drone binary installed from $SWARM_DRONE_BIN"
    fi
elif [ -f /opt/build-swarm/bin/swarm-drone ]; then
    ok "Drone binary already installed"
else
    warn "No drone binary found. You may need to deploy it separately."
    warn "  Use: build-swarmv3 provision $DRONE_NAME"
    warn "  Or copy manually to /opt/build-swarm/bin/swarm-drone"
fi

# Install spec + compliance tools for local drift detection
if [ "$DRY_RUN" = false ]; then
    # Embed the spec if available, otherwise skip
    if [ -f "$(dirname "$0")/drone.spec" ]; then
        cp "$(dirname "$0")/drone.spec" /etc/build-swarm/drone.spec
    fi
    if [ -f "$(dirname "$0")/comply.sh" ]; then
        cp "$(dirname "$0")/comply.sh" /opt/build-swarm/comply.sh
        chmod +x /opt/build-swarm/comply.sh
    fi

    # Install comply-cron.sh for daily drift detection
    cat > /opt/build-swarm/comply-cron.sh <<'CRONEOF'
#!/bin/bash
# Daily compliance check — logs to /var/log/build-swarm/compliance.log
set -uo pipefail
COMPLY_SCRIPT="/opt/build-swarm/comply.sh"
SPEC_FILE="/etc/build-swarm/drone.spec"
LOG_FILE="/var/log/build-swarm/compliance.log"
VERBOSE=false
for arg in "$@"; do
    case "$arg" in --verbose|-v) VERBOSE=true ;; esac
done
[ ! -f "$COMPLY_SCRIPT" ] && echo "$(date -Iseconds) ERROR comply.sh missing" >> "$LOG_FILE" && exit 1
[ ! -f "$SPEC_FILE" ] && echo "$(date -Iseconds) ERROR drone.spec missing" >> "$LOG_FILE" && exit 1
mkdir -p "$(dirname "$LOG_FILE")"
OUTPUT=$(bash "$COMPLY_SCRIPT" --spec "$SPEC_FILE" 2>&1)
EXIT_CODE=$?
TS=$(date -Iseconds)
HN=$(hostname)
CLEAN=$(echo "$OUTPUT" | sed 's/\x1b\[[0-9;]*m//g')
if [ $EXIT_CODE -eq 0 ]; then
    echo "$TS $HN COMPLIANT" >> "$LOG_FILE"
    [ "$VERBOSE" = true ] && echo "$OUTPUT"
elif [ $EXIT_CODE -eq 1 ]; then
    echo "$TS $HN WARNINGS" >> "$LOG_FILE"
    echo "$CLEAN" | grep -E "^(WARN|SUMMARY)" >> "$LOG_FILE"
    echo "---" >> "$LOG_FILE"
    [ "$VERBOSE" = true ] && echo "$OUTPUT"
else
    echo "$TS $HN NON-COMPLIANT (exit $EXIT_CODE)" >> "$LOG_FILE"
    echo "$CLEAN" >> "$LOG_FILE"
    echo "---" >> "$LOG_FILE"
    echo "$OUTPUT" >&2
fi
exit $EXIT_CODE
CRONEOF
    chmod +x /opt/build-swarm/comply-cron.sh

    # Install daily cron job
    mkdir -p /etc/cron.d
    echo "0 6 * * * root /opt/build-swarm/comply-cron.sh" > /etc/cron.d/swarm-comply
    ok "Compliance tools installed (daily check at 06:00)"
else
    info "[DRY RUN] Would install compliance tools and daily cron job"
fi

# ── 10. SSH key and OpenRC service ──
step "10/10 Setting up SSH and service"

# SSH key
if [ ! -f /root/.ssh/id_ed25519 ] && [ ! -f /root/.ssh/id_rsa ]; then
    if [ "$DRY_RUN" = true ]; then
        info "[DRY RUN] Would generate SSH key"
    else
        ssh-keygen -t ed25519 -f /root/.ssh/id_ed25519 -N "" -q
        ok "Generated SSH key: /root/.ssh/id_ed25519"
        echo ""
        echo -e "${YELLOW}Add this public key to the control plane host's authorized_keys:${RESET}"
        cat /root/.ssh/id_ed25519.pub
        echo ""
    fi
else
    ok "SSH key already exists"
fi

# OpenRC service
SERVICE_FILE="/etc/init.d/swarm-drone"
if [ "$DRY_RUN" = true ]; then
    info "[DRY RUN] Would install OpenRC service at $SERVICE_FILE"
else
    cat > "$SERVICE_FILE" <<'SVCEOF'
#!/sbin/openrc-run
# Build Swarm Drone Service

description="Build Swarm Drone Worker"
supervisor=supervise-daemon

command="/opt/build-swarm/bin/swarm-drone"
command_user="root"

output_log="/var/log/build-swarm/drone.log"
error_log="/var/log/build-swarm/drone-error.log"

respawn_delay=5
respawn_max=10
respawn_period=60

pidfile="/run/swarm-drone.pid"

start_pre() {
    # Load configuration
    if [ -f /etc/build-swarm/drone.conf ]; then
        set -a
        . /etc/build-swarm/drone.conf
        set +a
    fi
}

depend() {
    need net
    after firewall
}
SVCEOF
    chmod +x "$SERVICE_FILE"
    rc-update add swarm-drone default 2>/dev/null || true
    ok "OpenRC service installed and enabled"

    # Start the service if the binary exists
    if [ -f /opt/build-swarm/bin/swarm-drone ]; then
        rc-service swarm-drone start 2>/dev/null && ok "Drone service started" || warn "Service start deferred"
    fi
fi

# ── Summary ──
echo ""
echo -e "${BOLD}${GREEN}=== Bootstrap Complete ===${RESET}"
echo -e "  ${DIM}Drone:${RESET}    $DRONE_NAME"
echo -e "  ${DIM}Gateway:${RESET}  $GATEWAY_URL"
echo -e "  ${DIM}Config:${RESET}   /etc/build-swarm/drone.conf"
echo -e "  ${DIM}Logs:${RESET}     /var/log/build-swarm/drone.log"
PKG_FINAL=$(ls -d /var/db/pkg/*/* 2>/dev/null | wc -l)
echo -e "  ${DIM}Packages:${RESET} $PKG_FINAL installed"
echo ""
echo -e "  ${DIM}The drone should register within ~30 seconds.${RESET}"
echo -e "  ${DIM}Check: build-swarmv3 fleet${RESET}"
