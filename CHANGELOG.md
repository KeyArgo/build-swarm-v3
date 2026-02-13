# Changelog

All notable changes to Build Swarm are documented in this file.

## [0.4.0-alpha] - 2026-02-12

### Major Features

#### Self-Healing Drones (Phase 1)
- **Autonomous recovery**: Drones automatically recover from failures without manual intervention
- **4-level escalation ladder**:
  - Level 1: Service restart (`rc-service swarm-drone restart`)
  - Level 2: Hard restart (kill + restart)
  - Level 3: Container reboot (LXC/QEMU only - bare-metal protected)
  - Level 4: Admin alert (human intervention required)
- **Proof-of-life probing**: Active health checks with ping/pong latency tracking
- **Safe reboot detection**: Only LXC and QEMU containers can be auto-rebooted
- **Escalation cooldowns**: Prevents rapid-fire recovery attempts

#### Admin Terminal & Logs (Phase 2)
- **WebSocket SSH bridge**: Browser-based terminal access to drones via `/ws/ssh/<drone>`
- **Log viewer API**: View drone syslog and control plane logs from dashboard
- **Centralized logging**: All drone activity visible from one place

#### Heartbeat Visualization (Phase 3)
- **Animated topology**: SVG packets travel along connection lines showing health
- **Color-coded status**: Cyan (healthy) → Yellow → Amber → Red (critical)
- **Self-healing status cards**: Real-time counts of healthy/escalating/critical drones
- **Ping latency display**: See average response times across fleet

#### Payload Versioning (Phase 4)
- **Version tracking**: SHA256-hashed payloads with full history
- **Rolling deployments**: Deploy to one drone at a time with health checks
- **Automatic verification**: Confirm deployed files match expected hashes
- **Drift detection**: Identify when drone files have been modified
- **Payload types**: `drone_binary`, `init_script`, `config`, `portage_config`

#### Public/Admin Separation (Phase 5)
- **Access control**: Sensitive endpoints require `X-Admin-Key` header
- **Protected POST endpoints**: `/api/v1/queue`, `/api/v1/control`, `/api/v1/provision/*`
- **Protected GET endpoints**: SQL queries, drone health details, bootstrap scripts
- **Safe public API**: Read-only status, nodes, events, history remain open

### New Files
- `swarm/self_healing.py` - Self-healing monitor and proof-of-life prober
- `swarm/webssh.py` - WebSocket SSH bridge for browser terminals
- `swarm/payloads.py` - Payload version management and deployment

### Database Schema Changes
- Added to `nodes` table: `drone_type`, `last_ping_at`, `last_pong_at`, `ping_latency_ms`
- Added to `drone_health` table: `escalation_level`, `last_escalation_at`, `escalation_attempts`
- New table: `payload_versions` - Central version registry
- New table: `drone_payloads` - Per-drone payload tracking
- New table: `payload_deploy_log` - Deployment history

### New API Endpoints

#### Self-Healing
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/v1/ping` | Get ping status for all drones |
| GET | `/api/v1/ping/all` | Trigger ping to all online drones |
| GET | `/api/v1/escalation` | Get escalation status for all drones |
| POST | `/api/v1/nodes/<name>/ping` | Ping a specific drone |
| POST | `/api/v1/nodes/<name>/reset-escalation` | Reset escalation level |
| POST | `/api/v1/nodes/<name>/set-type` | Set drone type (lxc/qemu/bare-metal) |

#### Payload Versioning (Admin)
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/admin/api/payloads` | List all payload versions |
| GET | `/admin/api/payloads/status` | Deployment status matrix |
| GET | `/admin/api/payloads/<type>/versions` | Versions for a payload type |
| GET | `/admin/api/payloads/<type>/deploy-log` | Deployment history |
| POST | `/admin/api/payloads` | Register new payload version |
| POST | `/admin/api/payloads/<type>/<ver>/deploy` | Deploy to single drone |
| POST | `/admin/api/payloads/<type>/<ver>/rolling-deploy` | Rolling deploy to fleet |
| POST | `/admin/api/payloads/<type>/verify` | Verify drone payload hash |

#### Admin Logs
| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/admin/api/drones/<name>/syslog` | View drone system logs |
| GET | `/admin/api/logs/control-plane` | View control plane logs |
| GET | `/admin/api/drones/<name>/escalation` | Drone escalation details |
| POST | `/admin/api/drones/<name>/ping` | Trigger proof-of-life ping |
| GET | `/admin/api/self-healing/status` | Self-healing monitor status |

### Dashboard Updates
- **Topology tab**: Self-healing status cards, animated heartbeat visualization
- **Self-healing table**: Per-drone status with Ping/Reset actions
- **Version display**: Updated to v4.0.0 in header

### Breaking Changes
- POST endpoints `/api/v1/queue`, `/api/v1/control`, and `/api/v1/provision/*` now require `X-Admin-Key` header on public port
- GET endpoints `/api/v1/sql/*` and `/api/v1/drone-health` now require `X-Admin-Key` header

### Migration Notes
- Database migrations run automatically on startup
- No manual schema changes required
- Existing drones will have `drone_type = 'unknown'` until set via API

---

## [3.2.0] - 2026-02-12

### Added
- **Build Profiles**: Named package sets for distribution or end-user builds
- **Portage Snapshots**: Compressed tarballs of the portage tree
- **Auto-rebuild**: Profiles with `auto_rebuild` automatically queue outdated packages
- **Profile CLI**: `profile create/list/show/sync/edit/delete` commands
- **Snapshot CLI**: `snapshot list/create` commands

---

## [3.1.0] - 2026-02-10

### Added
- Stale completion filtering (fixes 82% failure rate bug)
- Smart package-drone assignment
- Cross-drone failure detection
- Persistent events (SQLite + ring buffer)
- SSH health probing
- Escalation ladder (service restart → reboot)
- SQL explorer API and dashboard tab
- Dashboard control panel

### Changed
- MAX_FAILURES: 5 → 8
- GROUNDING_TIMEOUT: 2min → 5min
- FAILURE_AGE: 60min → 30min
- Offline reclaim timeout: 4h → 2h

---

## [3.0.0] - 2026-02-08

### Added
- Initial v3 release
- Unified control plane (replaces v2 gateway + orchestrator)
- SQLite WAL-mode database
- Protocol logger (Wireshark-style capture)
- Admin dashboard on port 8093
- Pure Python stdlib implementation
