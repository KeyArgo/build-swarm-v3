# Gentoo Build Swarm Handbook

**Version 0.4.0-alpha**

A comprehensive guide to deploying, operating, and maintaining a distributed
Gentoo binary package build system.

---

## Table of Contents

1. [Introduction](#introduction)
2. [Architecture](#architecture)
3. [Installation](#installation)
4. [Configuration](#configuration)
5. [Control Plane Operations](#control-plane-operations)
6. [Drone Management](#drone-management)
7. [Self-Healing System](#self-healing-system)
8. [Build Profiles](#build-profiles)
9. [Payload Versioning](#payload-versioning)
10. [Admin Dashboard](#admin-dashboard)
11. [CLI Reference](#cli-reference)
12. [API Reference](#api-reference)
13. [Troubleshooting](#troubleshooting)
14. [Security](#security)

---

## Introduction

Build Swarm is a distributed binary package builder for Gentoo Linux. It
coordinates a fleet of build machines ("drones") to compile packages in
parallel and distribute them to a central binhost.

### Key Features

- **Zero dependencies**: Pure Python stdlib, works with Python 3.8+
- **Self-healing**: Automatic recovery from drone failures
- **Live monitoring**: Real-time dashboard with topology visualization
- **Build profiles**: Named package sets for different targets
- **Payload versioning**: Track and deploy drone software versions
- **Protocol logging**: Wireshark-style HTTP traffic capture

### Terminology

| Term | Definition |
|------|------------|
| **Control Plane** | Central server that coordinates builds (port 8100) |
| **Admin Dashboard** | Web UI for monitoring and control (port 8093) |
| **Drone** | Build machine that compiles packages |
| **Binhost** | Server hosting compiled binary packages |
| **Session** | A group of packages queued together |
| **Escalation** | Recovery action level (1-4) |

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                    Build Swarm Architecture                      │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│                    ┌──────────────────────┐                     │
│                    │   Control Plane      │                     │
│                    │   :8100 (public)     │                     │
│                    │   :8093 (admin)      │                     │
│                    └──────────┬───────────┘                     │
│                               │                                  │
│              ┌────────────────┼────────────────┐                │
│              │                │                │                │
│              ▼                ▼                ▼                │
│        ┌──────────┐    ┌──────────┐    ┌──────────┐            │
│        │ drone-01 │    │ drone-02 │    │ drone-03 │            │
│        │ (LXC)    │    │ (QEMU)   │    │ (bare)   │            │
│        └────┬─────┘    └────┬─────┘    └────┬─────┘            │
│             │               │               │                   │
│             └───────────────┼───────────────┘                   │
│                             ▼                                   │
│                    ┌──────────────────┐                         │
│                    │     Binhost      │                         │
│                    │  Binary packages │                         │
│                    └──────────────────┘                         │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

### Components

#### Control Plane (`swarm/control_plane.py`)
- HTTP server handling drone registration, work assignment, build completion
- SQLite database for all state (WAL mode for concurrent access)
- Protocol logger for traffic analysis
- Self-healing monitor for autonomous recovery

#### Admin Dashboard (`admin/`)
- Browser-based monitoring and control
- Real-time topology visualization
- Build history and queue management
- SQL explorer for data analysis

#### Drones
- Remote build machines running `swarm-drone` service
- Register with control plane via heartbeat
- Request work, compile packages, report results
- Upload compiled packages to binhost

---

## Installation

### Requirements

- Python 3.8 or later
- Gentoo Linux (for full functionality)
- SSH access to drone machines
- Network connectivity between all nodes

### From Source (Recommended)

```bash
git clone https://git.argobox.com/KeyArgo/build-swarm-v3.git
cd build-swarm-v3
pip install -e .
```

### Direct (No pip)

```bash
git clone https://git.argobox.com/KeyArgo/build-swarm-v3.git
cd build-swarm-v3
./build-swarmv3 serve
```

### Verify Installation

```bash
build-swarmv3 --version
# Output: build-swarmv3 0.4.0-alpha

build-swarmv3 --help
```

---

## Configuration

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `SWARMV3_URL` | `http://localhost:8100` | Control plane URL for CLI |
| `CONTROL_PLANE_PORT` | `8100` | Public API port |
| `ADMIN_PORT` | `8093` | Admin dashboard port |
| `ADMIN_KEY` | (auto-generated) | Admin authentication key |
| `SWARM_DB_PATH` | `/var/lib/build-swarm-v3/swarm.db` | Database path |
| `LOG_FILE` | `/var/log/build-swarm-v3/control-plane.log` | Log file |
| `STAGING_PATH` | `/var/cache/binpkgs-v3-staging` | Package staging area |
| `BINHOST_PATH` | `/var/cache/binpkgs-v3` | Final binhost path |
| `V2_SWARM_CONFIG` | (auto-detected) | Path to v2 swarm.json |

### Admin Key Setup

The admin key is required for protected endpoints. Set it via environment:

```bash
export ADMIN_KEY="your-secret-key-here"
```

Or in a config file:

```bash
echo 'ADMIN_KEY=your-secret-key-here' >> /etc/build-swarm-v3/env
```

### V2 Compatibility

Build Swarm v3 can read existing v2 configuration:

```bash
export V2_SWARM_CONFIG=~/Development/gentoo-build-swarm/config/swarm.json
```

---

## Control Plane Operations

### Starting the Server

```bash
# Basic start
build-swarmv3 serve

# With custom port
build-swarmv3 serve --port 8100

# Background with logging
build-swarmv3 serve >> /var/log/swarm.log 2>&1 &
```

### OpenRC Service

```bash
# Install service
cp drone-image/swarm-control-plane.initd /etc/init.d/swarm-control-plane
chmod +x /etc/init.d/swarm-control-plane

# Enable and start
rc-update add swarm-control-plane default
rc-service swarm-control-plane start
```

### Checking Status

```bash
# Quick status
build-swarmv3 status

# Detailed fleet info
build-swarmv3 fleet

# Live monitoring
build-swarmv3 monitor
```

### Queuing Builds

```bash
# Queue all @world packages
build-swarmv3 fresh

# Queue specific packages
build-swarmv3 queue add sys-apps/portage dev-lang/python

# Queue from a profile
build-swarmv3 fresh --profile argo-distro
```

### Control Actions

```bash
# Pause builds (drones stop receiving work)
build-swarmv3 control pause

# Resume builds
build-swarmv3 control resume

# Unblock failed packages
build-swarmv3 control unblock

# Reclaim delegated packages
build-swarmv3 control rebalance

# Clear all failures
build-swarmv3 control clear_failures

# Reset grounded drones
build-swarmv3 control unground
```

---

## Drone Management

### Drone Types

| Type | Description | Auto-Reboot |
|------|-------------|-------------|
| `lxc` | LXC container | Yes |
| `qemu` | QEMU/KVM virtual machine | Yes |
| `bare-metal` | Physical machine | **No** (protected) |
| `unknown` | Type not set | No |

### Deploying a New Drone

```bash
# Deploy drone to a machine
build-swarmv3 drone deploy 10.0.0.175 --name drone-01

# With custom SSH settings
build-swarmv3 drone deploy 10.0.0.175 \
  --name drone-01 \
  --user root \
  --port 22 \
  --key ~/.ssh/id_ed25519
```

### Provisioning Drones

```bash
# Generate bootstrap script
build-swarmv3 bootstrap-script > bootstrap.sh

# Run on target machine
ssh root@drone-01 'bash -s' < bootstrap.sh
```

### Auditing Drones

```bash
# Audit all drones
build-swarmv3 drone audit

# Audit specific drones
build-swarmv3 drone audit drone-01 drone-02

# Check compliance
build-swarmv3 drone audit --compliance
```

### Setting Drone Type

Set drone type for self-healing safety:

```bash
curl -X POST "http://localhost:8100/api/v1/nodes/drone-01/set-type" \
  -H "Content-Type: application/json" \
  -H "X-Admin-Key: $ADMIN_KEY" \
  -d '{"drone_type": "lxc"}'
```

### Drone Configuration (via Dashboard)

Edit per-drone settings in the Admin Dashboard under "Drone Mgmt":

- SSH credentials (user, port, key path)
- Resource limits (cores, jobs, RAM)
- Behavior settings (auto-reboot, protected)
- Identity (display name, control plane)

---

## Self-Healing System

### Overview

The self-healing system automatically recovers drones from failures using a
4-level escalation ladder:

```
Level 0: Normal operation
    │
    ▼ (drone goes offline)
Level 1: Service restart
    │   rc-service swarm-drone restart
    │
    ▼ (still offline after 30s)
Level 2: Hard restart
    │   Kill process + restart service
    │
    ▼ (still offline after 30s)
Level 3: Container reboot
    │   Full system reboot (LXC/QEMU only)
    │
    ▼ (still offline after 120s)
Level 4: Admin alert
    │   Human intervention required
    └───────────────────────────────
```

### Proof-of-Life Probing

The control plane sends periodic health checks to drones:

```bash
# View ping status for all drones
curl http://localhost:8100/api/v1/ping

# Trigger ping to all drones
curl http://localhost:8100/api/v1/ping/all

# Ping specific drone
curl -X POST http://localhost:8100/api/v1/nodes/drone-01/ping \
  -H "X-Admin-Key: $ADMIN_KEY"
```

### Escalation Management

```bash
# View escalation status
curl http://localhost:8100/api/v1/escalation

# Reset escalation for a drone
curl -X POST http://localhost:8100/api/v1/nodes/drone-01/reset-escalation \
  -H "X-Admin-Key: $ADMIN_KEY"
```

### Protecting Bare-Metal Hosts

Bare-metal hosts are protected from automatic reboot:

```bash
# Mark drone as bare-metal (will never auto-reboot)
curl -X POST http://localhost:8100/api/v1/nodes/prod-server/set-type \
  -H "Content-Type: application/json" \
  -H "X-Admin-Key: $ADMIN_KEY" \
  -d '{"drone_type": "bare-metal"}'
```

### Viewing Self-Healing Status

In the Admin Dashboard, go to the **Topology** tab to see:

- Healthy/Escalating/Critical drone counts
- Average ping latency
- Monitor status (Running/Stopped)
- Per-drone escalation levels with Ping/Reset actions

---

## Build Profiles

### Creating a Profile

```bash
# Create distribution profile
build-swarmv3 profile create argo-distro \
  --type distribution \
  --name "Argo OS Distribution"

# Create user profile from remote machine
build-swarmv3 profile create callisto-user \
  --type user \
  --name "Callisto Workstation" \
  --source "ssh:root@10.0.0.100:/var/lib/portage/world"
```

### Managing Profiles

```bash
# List all profiles
build-swarmv3 profile list

# Show profile details
build-swarmv3 profile show argo-distro

# Sync profile (resolve deps, diff, queue builds)
build-swarmv3 profile sync argo-distro

# Full sync (ignore previous state)
build-swarmv3 profile sync argo-distro --full

# Edit profile packages
build-swarmv3 profile edit argo-distro

# Delete profile
build-swarmv3 profile delete old-profile
```

### Auto-Rebuild

Enable automatic rebuild when portage syncs:

```bash
build-swarmv3 profile create argo-distro \
  --type distribution \
  --auto-rebuild
```

---

## Payload Versioning

### Overview

Payload versioning tracks what software versions are deployed to each drone:

| Payload Type | Description | Path on Drone |
|--------------|-------------|---------------|
| `drone_binary` | The swarm-drone daemon | `/usr/local/bin/swarm-drone` |
| `init_script` | OpenRC init script | `/etc/init.d/swarm-drone` |
| `config` | Drone configuration | `/etc/swarm-drone/config.json` |
| `portage_config` | Portage settings | `/etc/portage/repos.conf/binhost.conf` |

### Registering a Payload Version

```bash
# Encode content as base64
CONTENT=$(base64 -w0 /path/to/swarm-drone)

# Register version
curl -X POST http://localhost:8093/admin/api/payloads \
  -H "Content-Type: application/json" \
  -H "X-Admin-Key: $ADMIN_KEY" \
  -d "{
    \"type\": \"drone_binary\",
    \"version\": \"0.4.0\",
    \"content\": \"$CONTENT\",
    \"description\": \"Swarm drone v0.4.0\"
  }"
```

### Deploying to a Single Drone

```bash
curl -X POST http://localhost:8093/admin/api/payloads/drone_binary/0.4.0/deploy \
  -H "Content-Type: application/json" \
  -H "X-Admin-Key: $ADMIN_KEY" \
  -d '{"drone": "drone-01"}'
```

### Rolling Deployment

Deploy to all outdated drones, one at a time:

```bash
curl -X POST http://localhost:8093/admin/api/payloads/drone_binary/0.4.0/rolling-deploy \
  -H "Content-Type: application/json" \
  -H "X-Admin-Key: $ADMIN_KEY" \
  -d '{
    "health_check": true,
    "rollback_on_fail": true
  }'
```

### Verifying Deployments

```bash
curl -X POST http://localhost:8093/admin/api/payloads/drone_binary/verify \
  -H "Content-Type: application/json" \
  -H "X-Admin-Key: $ADMIN_KEY" \
  -d '{"drone": "drone-01"}'
```

### Viewing Deployment Status

```bash
# Get deployment matrix
curl http://localhost:8093/admin/api/payloads/status \
  -H "X-Admin-Key: $ADMIN_KEY"

# Get deployment history
curl http://localhost:8093/admin/api/payloads/drone_binary/deploy-log \
  -H "X-Admin-Key: $ADMIN_KEY"
```

---

## Admin Dashboard

### Accessing the Dashboard

Open in browser: `http://<control-plane-ip>:8093/`

Enter your admin key when prompted.

### Tabs Overview

| Tab | Description |
|-----|-------------|
| **Overview** | Queue status, build progress, control panel |
| **Fleet** | Drone list with status, health, current tasks |
| **Drone Mgmt** | SSH config, allowlist, bloat audit |
| **Releases** | Binary package release management |
| **Queue** | Package queue with status and actions |
| **History** | Build history with filtering |
| **Topology** | Network visualization with heartbeat animation |
| **Wire** | Protocol inspector (Wireshark-style) |
| **Data** | SQL explorer for database queries |
| **Events** | Activity feed |

### Keyboard Shortcuts

| Key | Action |
|-----|--------|
| `1-9, 0` | Switch to tab 1-10 |
| `R` | Refresh data |

### Topology Visualization

The topology tab shows:

- **Control Plane** at top center
- **Binhosts** (primary and secondary)
- **Drones** with status colors and progress bars
- **Animated packets** showing heartbeat traffic
- **Escalation indicators** (L1-L4 badges)

Color coding:
- **Cyan**: Healthy
- **Yellow**: Escalation Level 1
- **Amber**: Escalation Level 2
- **Red**: Escalation Level 3-4

---

## CLI Reference

### General Commands

```
build-swarmv3 [OPTIONS] COMMAND [ARGS]

Options:
  --version     Show version
  --help        Show help

Commands:
  serve         Start control plane server
  status        Show queue status
  fresh         Queue all @world packages
  queue         Queue management
  fleet         List registered drones
  history       Show build history
  control       Send control actions
  monitor       Live status display
  provision     Provision a new drone
  bootstrap-script  Print drone bootstrap script
  drone         Drone management
  switch        Switch drones between control planes
  profile       Build profile management
  snapshot      Portage snapshot management
```

### Queue Commands

```
build-swarmv3 queue COMMAND

Commands:
  add <packages...>   Add packages to queue
  list               List queue contents
  status             Show queue statistics
```

### Drone Commands

```
build-swarmv3 drone COMMAND

Commands:
  audit [names...]   Audit drones against spec
  deploy <ip>        Deploy drone to target machine
  list              List all drones
```

### Profile Commands

```
build-swarmv3 profile COMMAND

Commands:
  list              List all profiles
  create <id>       Create new profile
  show <id>         Show profile details
  sync <id>         Sync profile and queue builds
  edit <id>         Edit profile packages
  delete <id>       Delete profile
```

### Control Actions

```
build-swarmv3 control ACTION

Actions:
  pause             Pause build queue
  resume            Resume build queue
  unblock           Unblock all blocked packages
  unground          Clear grounded state on drones
  reset             Reset queue to initial state
  rebalance         Reclaim delegated packages
  clear_failures    Clear all failure states
  retry_failures    Re-queue failed packages
```

---

## API Reference

See [API.md](API.md) for complete API documentation.

### Quick Reference

#### Public Endpoints (Port 8100)

```
GET  /api/v1/health          Health check
GET  /api/v1/status          Queue and session status
GET  /api/v1/nodes           List drones
GET  /api/v1/events          Recent events
GET  /api/v1/history         Build history
GET  /api/v1/sessions        Session list
```

#### Protected Endpoints (Require X-Admin-Key)

```
POST /api/v1/queue           Add packages to queue
POST /api/v1/control         Send control action
POST /api/v1/nodes/*/pause   Pause drone
GET  /api/v1/sql/*           SQL queries
GET  /api/v1/drone-health    Drone health details
```

#### Admin Endpoints (Port 8093)

```
GET  /admin/api/system/info           System information
GET  /admin/api/drones/*/syslog       Drone system logs
GET  /admin/api/logs/control-plane    Control plane logs
GET  /admin/api/payloads              Payload versions
POST /admin/api/payloads              Register payload
POST /admin/api/payloads/*/*/deploy   Deploy payload
GET  /admin/api/self-healing/status   Self-healing status
```

---

## Troubleshooting

### Drone Not Coming Online

1. Check SSH connectivity:
   ```bash
   ssh root@drone-ip "echo ok"
   ```

2. Check swarm-drone service:
   ```bash
   ssh root@drone-ip "rc-service swarm-drone status"
   ```

3. Check drone logs:
   ```bash
   ssh root@drone-ip "tail -100 /var/log/swarm-drone.log"
   ```

4. Verify control plane URL on drone:
   ```bash
   ssh root@drone-ip "cat /etc/swarm-drone/config.json"
   ```

### Self-Healing Not Working

1. Check drone type is set:
   ```bash
   curl http://localhost:8100/api/v1/escalation
   ```

2. Set drone type if unknown:
   ```bash
   curl -X POST http://localhost:8100/api/v1/nodes/drone-01/set-type \
     -H "X-Admin-Key: $ADMIN_KEY" \
     -d '{"drone_type": "lxc"}'
   ```

3. Check self-healing monitor is running:
   ```bash
   curl http://localhost:8093/admin/api/self-healing/status \
     -H "X-Admin-Key: $ADMIN_KEY"
   ```

### Packages Stuck in Delegated State

1. Check assigned drone status:
   ```bash
   build-swarmv3 fleet
   ```

2. Rebalance if drone is offline:
   ```bash
   build-swarmv3 control rebalance
   ```

### Build Failures

1. Check failure details:
   ```bash
   build-swarmv3 history --limit 50
   ```

2. View package-specific stats:
   ```bash
   curl "http://localhost:8100/api/v1/build-stats/by-package?package=dev-libs/foo"
   ```

3. Unblock and retry:
   ```bash
   build-swarmv3 control unblock
   build-swarmv3 control retry_failures
   ```

### Database Issues

1. Check database integrity:
   ```bash
   sqlite3 /var/lib/build-swarm-v3/swarm.db "PRAGMA integrity_check"
   ```

2. Backup and restart:
   ```bash
   cp /var/lib/build-swarm-v3/swarm.db ~/swarm-backup.db
   rc-service swarm-control-plane restart
   ```

---

## Security

### Access Control

- **Public port (8100)**: Read-only status, drone registration
- **Admin port (8093)**: Full control, requires `X-Admin-Key` header

### Protected Endpoints

POST endpoints that modify state require admin authentication:
- `/api/v1/queue` - Queue packages
- `/api/v1/control` - Control actions
- `/api/v1/provision/*` - Provisioning
- `/api/v1/nodes/*/pause` - Pause drones

GET endpoints exposing sensitive data require admin authentication:
- `/api/v1/sql/*` - SQL queries
- `/api/v1/drone-health` - Health details
- `/api/v1/provision/bootstrap` - Bootstrap script

### Network Recommendations

1. Run control plane on trusted network only
2. Use Tailscale or VPN for remote drones
3. Set strong `ADMIN_KEY`
4. Firewall port 8093 to admin IPs only

### SSH Security

- Use key-based authentication (no passwords)
- Restrict drone SSH to control plane IP
- Use dedicated SSH keys for swarm operations

---

## Appendix

### File Locations

| Path | Description |
|------|-------------|
| `/var/lib/build-swarm-v3/swarm.db` | SQLite database |
| `/var/log/build-swarm-v3/control-plane.log` | Server log |
| `/var/cache/binpkgs-v3-staging/` | Package staging |
| `/var/cache/binpkgs-v3/` | Final binhost |
| `/etc/build-swarm-v3/env` | Environment config |

### Database Tables

| Table | Description |
|-------|-------------|
| `nodes` | Registered drones |
| `queue` | Build queue |
| `build_history` | Build results |
| `sessions` | Build sessions |
| `drone_health` | Health and escalation |
| `drone_config` | Per-drone settings |
| `events` | Activity log |
| `protocol_log` | HTTP traffic |
| `payload_versions` | Payload registry |
| `drone_payloads` | Deployed payloads |
| `releases` | Binary releases |

### Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Success |
| 1 | General error |
| 2 | Invalid arguments |
| 3 | Connection failed |
| 4 | Authentication required |

---

*Gentoo Build Swarm - Distributed Binary Package Builder*
*Version 0.4.0-alpha*
