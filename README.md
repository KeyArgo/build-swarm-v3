# Gentoo Build Swarm

Distributed Gentoo binary package builder. A unified control plane that manages
a fleet of build drones to compile and distribute binary packages across a
Gentoo Linux cluster.

**Zero dependencies** -- pure Python stdlib. Works with Python 3.8+.

## Documentation

| Document | Description |
|----------|-------------|
| [QUICKSTART.md](QUICKSTART.md) | Step-by-step getting started guide |
| [docs/HANDBOOK.md](docs/HANDBOOK.md) | Complete user handbook |
| [docs/API.md](docs/API.md) | Full API reference |
| [docs/IMPLEMENTATION-STATUS.md](docs/IMPLEMENTATION-STATUS.md) | Feature implementation status |
| [CHANGELOG.md](CHANGELOG.md) | Version history |
| [GAMEPLAN-v4.md](GAMEPLAN-v4.md) | Architecture design document |

## What's New

### v0.4.0-alpha (2026-02-12) -- Self-Healing & Admin Enhancements

- **Self-Healing Drones**: Autonomous recovery with 4-level escalation ladder
  - Level 1: Service restart → Level 2: Hard restart → Level 3: Container reboot → Level 4: Admin alert
  - Safe reboot handling (LXC/QEMU only, bare-metal protected)
  - Proof-of-life ping/pong with latency tracking
- **Admin Terminal**: WebSocket SSH bridge for browser-based drone access
- **Log Viewer**: Centralized access to drone and control plane logs
- **Heartbeat Visualization**: Animated topology with color-coded health status
- **Payload Versioning**: Track and deploy drone software versions
  - Rolling deployments with health checks
  - Hash verification and drift detection
- **Access Control**: Protected endpoints require admin authentication
- **New Endpoints**: `/api/v1/ping`, `/api/v1/escalation`, `/admin/api/payloads/*`

See [CHANGELOG.md](CHANGELOG.md) for full details.

### v0.3.2 (2026-02-12) -- Build Profiles & Portage Snapshots

- **Build Profiles**: Named package sets for distribution or end-user builds
  - `profile create` / `list` / `show` / `sync` / `edit` / `delete` CLI commands
  - Distribution mode: maintain a curated world file, auto-rebuild when portage syncs
  - User mode: point at any machine's world file (local or SSH)
  - Incremental sync: only queues packages that changed since last sync
  - `fresh --profile <id>`: profile-aware full rebuild
- **Portage Snapshots**: Compressed tarballs of the portage tree, kept forever
  - Automatic snapshots on profile sync, manual via `snapshot create`
  - Tied to sessions for build reproducibility
  - `snapshot list` / `snapshot create` CLI commands
- **Auto-rebuild**: Profiles with `auto_rebuild` automatically queue outdated packages after portage sync
- **Profile API**: Full REST API for profile CRUD, world updates, sync, and snapshots
- **Backward compatible**: `fresh` without `--profile` works exactly as before

### v3.1.0 (2026-02-10) -- Resilient scheduling, persistent events, SQL explorer.

- **Stale completion filtering**: Discards false failures from rebalanced packages (root cause of 82% failure rate in v3.0)
- **Smart package-drone assignment**: Avoids assigning packages to drones that previously failed them
- **Cross-drone failure detection**: Blocks packages that fail on 2+ different drones
- **Persistent events**: Events survive control plane restarts (dual-write: memory ring buffer + SQLite)
- **SSH health probing**: Checks process status, load, disk space, stuck emerge processes
- **Escalation ladder**: Service restart → full reboot → manual intervention (OpenRC-aware)
- **SQL explorer API**: Read-only SQL queries, table/schema introspection
- **Dashboard Data tab**: SQL explorer with shortcuts, query textarea, results table
- **Dashboard control panel**: Pause/Resume/Unblock/Rebalance/Clear Failures from the Overview tab
- **Tuned circuit breaker**: MAX_FAILURES 5→8, GROUNDING_TIMEOUT 2→5min, FAILURE_AGE 60→30min
- **Faster reclaim**: Offline work reclaim timeout 4h→2h
- **Schema migrations**: Safe column additions via PRAGMA introspection

## Installation

### From source (recommended)

```bash
git clone https://git.argobox.com/KeyArgo/build-swarm-v3.git
cd build-swarm-v3
pip install -e .
```

### From PyPI (when published)

```bash
pip install build-swarm-v3
```

### Direct (no pip)

```bash
git clone https://git.argobox.com/KeyArgo/build-swarm-v3.git
cd build-swarm-v3
./build-swarmv3 serve
```

## Requirements

- **Python 3.8+** (no external packages needed)
- **Gentoo Linux** for full functionality (emerge, binary packages)
- Works on any Linux for the control plane and dashboard

## Architecture

- **Control Plane** (`swarm/control_plane.py`) -- Single HTTP server on port 8100
  that replaces the v2 gateway + orchestrator pair.
- **SQLite Database** (`swarm/schema.sql`) -- All state in a single WAL-mode database.
- **Protocol Logger** (`swarm/protocol_logger.py`) -- Wireshark-style capture of all
  HTTP interactions with write-behind queue for zero overhead.
- **Drones** -- Remote build machines that register, request work, and report results.

### v2 Compatibility

v3 reads your existing v2 `swarm.json` for node definitions and portage config.
Set `V2_SWARM_CONFIG` to point at your config if it's not in the default location:

```bash
export V2_SWARM_CONFIG=~/Development/gentoo-build-swarm/config/swarm.json
```

## Quick Start

See **[QUICKSTART.md](QUICKSTART.md)** for a complete step-by-step guide covering
control plane setup, drone deployment, test builds, and golden image strategy.

### Start the control plane

```bash
build-swarmv3 serve
```

The server listens on port 8100 by default. Override with `--port` or the
`CONTROL_PLANE_PORT` environment variable.

### Deploy a drone

```bash
build-swarmv3 drone deploy 10.0.0.175 --name drone-01
```

### Audit your fleet

```bash
build-swarmv3 drone audit
```

### Queue a fresh @world build

```bash
build-swarmv3 fresh
```

### Monitor builds in real time

```bash
build-swarmv3 monitor
```

## CLI Reference

| Command | Description |
|---------|-------------|
| `build-swarmv3 serve` | Start the control plane HTTP server |
| `build-swarmv3 status` | Show queue status |
| `build-swarmv3 fresh [--profile ID]` | Queue all @world packages (or profile packages) |
| `build-swarmv3 queue add <pkgs...>` | Add specific packages to the queue |
| `build-swarmv3 queue list` | List current queue contents |
| `build-swarmv3 fleet` | List registered drones |
| `build-swarmv3 history [--limit N]` | Show build history |
| `build-swarmv3 control <action>` | Send a control action |
| `build-swarmv3 monitor [--interval N]` | Live status display |
| `build-swarmv3 provision <ip>` | Provision a new drone via SSH |
| `build-swarmv3 bootstrap-script` | Print drone bootstrap script |
| `build-swarmv3 drone audit [names...]` | Audit drones against spec |
| `build-swarmv3 drone deploy <ip>` | Deploy drone to a target machine |
| `build-swarmv3 switch <v2\|v3>` | Switch drones between control planes |

### Build Profiles

| Command | Description |
|---------|-------------|
| `build-swarmv3 profile list` | List all build profiles |
| `build-swarmv3 profile create <id>` | Create a new profile |
| `build-swarmv3 profile show <id>` | Show profile details and packages |
| `build-swarmv3 profile sync <id> [--full]` | Resolve world, diff, queue builds |
| `build-swarmv3 profile edit <id>` | Add/remove packages from a profile |
| `build-swarmv3 profile delete <id>` | Delete a profile |

### Portage Snapshots

| Command | Description |
|---------|-------------|
| `build-swarmv3 snapshot list` | List portage tree snapshots |
| `build-swarmv3 snapshot create` | Create a manual snapshot |

### Control Actions

| Action | Effect |
|--------|--------|
| `pause` | Pause the build queue (drones stop receiving work) |
| `resume` | Resume the build queue |
| `unblock` | Move all blocked packages back to needed |
| `unground` | Clear grounded state on drones |
| `reset` | Reset all non-received packages to needed |
| `rebalance` | Reclaim all delegated packages back to needed |
| `clear_failures` | Clear all failure states |
| `retry_failures` | Re-queue all failed packages |

## Environment Variables

| Variable | Default (root) | Default (user) | Description |
|----------|---------------|----------------|-------------|
| `SWARMV3_URL` | `http://localhost:8100` | same | Server URL for CLI commands |
| `CONTROL_PLANE_PORT` | `8100` | same | Port for the serve command |
| `SWARM_DB_PATH` | `/var/lib/build-swarm-v3/swarm.db` | `~/.local/share/build-swarm-v3/swarm.db` | SQLite database path |
| `LOG_FILE` | `/var/log/build-swarm-v3/control-plane.log` | `~/.local/state/build-swarm-v3/control-plane.log` | Log file path |
| `STAGING_PATH` | `/var/cache/binpkgs-v3-staging` | same | Binary package staging area |
| `BINHOST_PATH` | `/var/cache/binpkgs-v3` | same | Final binary package host path |
| `V2_SWARM_CONFIG` | auto-detected | auto-detected | Path to v2 swarm.json |
| `PROFILES_DIR` | `/var/lib/build-swarm-v3/profiles` | `~/.local/share/build-swarm-v3/profiles` | Profile data directory |
| `PORTAGE_SNAPSHOTS_DIR` | `/var/cache/portage-snapshots` | same | Portage tree snapshot storage |

## API Endpoints

All non-serve commands talk to these endpoints on the running server:

| Method | Endpoint | Purpose |
|--------|----------|---------|
| GET | `/api/v1/health` | Health check |
| GET | `/api/v1/status` | Queue status and drone overview |
| GET | `/api/v1/nodes?all=true` | List all drones |
| GET | `/api/v1/history` | Build history |
| GET | `/api/v1/sessions` | Session list |
| GET | `/api/v1/metrics` | Metrics time-series |
| GET | `/api/v1/events` | Recent events (ring buffer) |
| GET | `/api/v1/events/history` | Persistent event log (SQLite) |
| POST | `/api/v1/queue` | Add packages to queue |
| POST | `/api/v1/control` | Send control actions |
| POST | `/api/v1/register` | Drone registration (used by drones) |
| POST | `/api/v1/complete` | Build completion (used by drones) |
| GET | `/api/v1/work` | Request work (used by drones) |
| GET | `/api/v1/protocol` | Protocol log entries (Wire tab) |
| GET | `/api/v1/protocol/stats` | Protocol traffic summary |
| GET | `/api/v1/protocol/density` | Activity histogram for replay |
| GET | `/api/v1/protocol/snapshot` | State reconstruction at timestamp |
| GET | `/api/v1/drone-health` | Drone health records with probe results |
| GET | `/api/v1/build-stats/by-package` | Per-package success/failure stats |
| GET | `/api/v1/sql/tables` | Table names and row counts |
| GET | `/api/v1/sql/schema` | Table schemas |
| GET | `/api/v1/sql/query?q=SELECT...` | Read-only SQL queries (SELECT only) |
| GET | `/api/v1/profiles` | List all build profiles |
| GET | `/api/v1/profiles/<id>` | Profile details with packages |
| POST | `/api/v1/profiles` | Create a new profile |
| POST | `/api/v1/profiles/<id>/world` | Update profile world packages |
| POST | `/api/v1/profile/sync` | Resolve, diff, and queue builds |
| DELETE | `/api/v1/profiles/<id>` | Delete a profile |
| GET | `/api/v1/snapshots` | List portage snapshots |
| POST | `/api/v1/snapshots` | Create a portage snapshot |

## Project Structure

```
build-swarm-v3/
  pyproject.toml         Python packaging config
  build-swarmv3          CLI wrapper (for running from source)
  QUICKSTART.md          Step-by-step operational guide
  LICENSE                MIT License
  swarm/
    __init__.py          Package init, version
    cli.py               CLI entry point (pip installs this)
    config.py            Configuration and env vars
    control_plane.py     HTTP server and API handlers
    db.py                SQLite database layer
    drone_audit.py       SSH-based drone audit and deployment
    health.py            Drone health monitoring / circuit breaker
    scheduler.py         Work assignment and scheduling logic
    protocol_logger.py   Wireshark-style protocol capture
    provisioner.py       Drone bootstrap and SSH provisioning
    events.py            Event system (ring buffer + SQLite persistence)
    schema.sql           SQLite schema (bundled with package)
  drone-image/           Drone image specification (golden spec)
    drone.spec           JSON spec: profile, packages, limits
    bootstrap.sh         Full drone provisioning script
    comply.sh            Compliance checker (10 checks)
    comply-cron.sh       Daily drift detection cron wrapper
    make.conf.drone      Portage make.conf template
    package.list         10 @world atoms
    package.use.drone    Per-package USE flags
    package.accept_keywords.drone  Keyword overrides
    swarm-control-plane.initd  OpenRC service for control plane
  tests/
    test_db.py           Database tests
    test_protocol_logger.py  Protocol logger tests
```

## Development

```bash
# Install in development mode with test deps
pip install -e ".[dev]"

# Run tests
pytest

# Run a single test file
pytest tests/test_protocol_logger.py -v
```

## License

MIT -- see [LICENSE](LICENSE).
