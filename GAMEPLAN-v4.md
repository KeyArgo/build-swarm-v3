# Build Swarm v4 - The Ultimate Admin Dashboard
## "The Awe of the Gentoo and Homelab Community"

**Created**: 2026-02-12
**Status**: Game Plan / Architecture Document

---

## Executive Summary

This document outlines the comprehensive upgrade from Build Swarm v3 to v4, transforming the system into a world-class distributed build orchestrator with:

- **100% drone availability** through self-healing and smart escalation
- **Real-time visibility** into every heartbeat, packet, and command
- **Granular admin control** with full SSH terminal integration
- **Zero-drift deployment** across multiple machines with versioned releases
- **Production-ready distro builder** for Argo OS and custom Gentoo distributions

---

## Current State Analysis (v3)

### What Already Exists

| Component | Status | Location |
|-----------|--------|----------|
| Control Plane | Complete | `control_plane.py` (67K) |
| Health Monitoring | Complete | `health.py` - Circuit breaker, grounding |
| SSH Escalation | Partial | Service restart + reboot ladder |
| Protocol Logger | Complete | Wireshark-style HTTP capture |
| Event System | Complete | Ring buffer + SQLite persistence |
| Release Management | Complete | Staging → Promote → Rollback |
| Admin Server | Partial | Auth, static files, some endpoints |
| Scheduler | Complete | Work assignment with auto-balance |

### What's Missing

1. **Heartbeat Visualization** - No packet animation through topology
2. **Self-Healing Drones** - Escalation exists but not autonomous
3. **Web SSH Terminal** - No browser-based shell access
4. **Log Viewing** - No centralized log access
5. **Payload Versioning** - No tracking of instruction versions
6. **Proof of Life** - No explicit ping/pong probes
7. **Real-time Commands** - No live command output streaming
8. **Public/Admin Separation** - Controls still on public page
9. **Binhost Flexibility** - Hardcoded paths, not distro-ready
10. **VM Provisioning** - No automated fresh install flow
11. **Test Builds** - No user-facing test build capability

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                        Build Swarm v4 Architecture                          │
├─────────────────────────────────────────────────────────────────────────────┤
│                                                                             │
│  ┌─────────────────┐     ┌─────────────────┐     ┌─────────────────┐       │
│  │ Public Website  │     │  Admin Console  │     │  CLI Interface  │       │
│  │ argobox.com/    │     │ argobox.com/    │     │  swarm-cli      │       │
│  │ build-swarm     │     │ admin/swarm     │     │                 │       │
│  │ (READ-ONLY)     │     │ (FULL CONTROL)  │     │                 │       │
│  └────────┬────────┘     └────────┬────────┘     └────────┬────────┘       │
│           │                       │                       │                 │
│           │         ┌─────────────┴─────────────┐         │                 │
│           └────────►│      Gateway Server       │◄────────┘                 │
│                     │   (Public API: 8100)      │                           │
│                     │   - Status endpoints      │                           │
│                     │   - No control actions    │                           │
│                     └─────────────┬─────────────┘                           │
│                                   │                                         │
│           ┌─────────────┬─────────┼─────────┬─────────────┐                │
│           │             │         │         │             │                │
│           ▼             ▼         ▼         ▼             ▼                │
│     ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐      │
│     │ drone-io │ │ dr-tb    │ │dr-titan  │ │ dr-mm2   │ │ sweeper  │      │
│     │ 16 cores │ │ 8 cores  │ │14 cores  │ │24 cores  │ │ (LXC)    │      │
│     │ Jove LAN │ │ Kronos   │ │ Kronos   │ │ Kronos   │ │          │      │
│     └──────────┘ └──────────┘ └──────────┘ └──────────┘ └──────────┘      │
│           ▲             ▲         ▲         ▲             ▲                │
│           └─────────────┴─────────┴─────────┴─────────────┘                │
│                            Tailscale VPN                                    │
│                     (100.x.x.x mesh network)                               │
│                                                                             │
│                     ┌─────────────────────────────┐                        │
│                     │      Admin Server           │                        │
│                     │   (Private API: 8093)       │                        │
│                     │   - SSH Terminal proxy      │                        │
│                     │   - Log streaming           │                        │
│                     │   - Reboot/restart commands │                        │
│                     │   - Payload versioning      │                        │
│                     │   - Release management      │                        │
│                     └─────────────────────────────┘                        │
│                                                                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

---

## Phase 1: Self-Healing Drones (100% Availability)

### 1.1 Autonomous Recovery System

**Goal**: Drones should be available 100% of the time with automatic recovery.

```python
# Escalation Ladder (already exists, needs automation)
Level 0: Normal operation
Level 1: Soft restart (rc-service swarm-drone restart)     # 30s timeout
Level 2: Hard restart (kill -9 + manual start)             # 30s timeout
Level 3: Container reboot (lxc-stop/start or qemu reboot)  # 2min timeout
Level 4: Alert admin (drone permanently unavailable)       # Manual intervention
```

**Implementation** (`swarm/self_healing.py`):

```python
class SelfHealingMonitor:
    """Autonomous drone recovery with safe escalation."""

    ESCALATION_LADDER = [
        ('restart_service', 30),   # Level 1: rc-service restart
        ('hard_restart', 30),      # Level 2: kill + start
        ('reboot_container', 120), # Level 3: container reboot
        ('alert_admin', 0),        # Level 4: give up
    ]

    def __init__(self, db, health_monitor):
        self.db = db
        self.health = health_monitor
        self.escalation_state = {}  # drone_id -> current_level

    def probe_all_drones(self):
        """Periodic probe of all registered drones (every 30s)."""
        for node in self.db.get_all_nodes():
            if node['status'] != 'online':
                continue
            result = self.health.probe_drone_health(node['id'], node['ip'])
            self._handle_probe_result(node, result)

    def _handle_probe_result(self, node, result):
        """Decide whether to escalate based on probe result."""
        if result['status'] == 'ok':
            # Reset escalation state on success
            self.escalation_state.pop(node['id'], None)
            return

        # Drone is unhealthy - escalate
        current_level = self.escalation_state.get(node['id'], 0)
        if current_level < len(self.ESCALATION_LADDER):
            action, timeout = self.ESCALATION_LADDER[current_level]
            self._execute_escalation(node, action, timeout)
            self.escalation_state[node['id']] = current_level + 1
```

### 1.2 Safe Reboot Handling

**CRITICAL**: All drones are LXC containers or QEMU VMs. Never reboot the host!

```python
class DroneTypeRegistry:
    """Track whether each drone is LXC, QEMU, or bare-metal."""

    DRONE_TYPES = {
        'drone-io': 'bare-metal',   # Protected - never reboot
        'dr-tb': 'lxc',             # Safe to restart
        'dr-titan': 'lxc',          # Safe to restart
        'dr-mm2': 'lxc',            # Safe to restart
    }

    def get_reboot_command(self, drone_name: str, drone_type: str) -> str:
        """Get the appropriate reboot command for drone type."""
        if drone_type == 'lxc':
            return 'reboot'  # Inside container, safe
        elif drone_type == 'qemu':
            return 'reboot'  # Inside VM, safe
        elif drone_type == 'bare-metal':
            return None  # BLOCKED - never reboot bare metal
        else:
            return None  # Unknown type - blocked for safety
```

### 1.3 Proof of Life System

```
┌─────────────────────────────────────────────────────────────┐
│                    Proof of Life Flow                        │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  Control Plane                         Drone                │
│       │                                   │                 │
│       │──────── PING (seq=42) ───────────►│                 │
│       │         timestamp: 1707745234     │                 │
│       │                                   │                 │
│       │◄─────── PONG (seq=42) ────────────│                 │
│       │         timestamp: 1707745234     │                 │
│       │         latency_ms: 23            │                 │
│       │         status: {                 │                 │
│       │           load: 2.4,              │                 │
│       │           disk: 45%,              │                 │
│       │           emerge_running: true    │                 │
│       │         }                         │                 │
│       │                                   │                 │
└─────────────────────────────────────────────────────────────┘
```

**New Endpoint**: `POST /api/v1/ping`

```python
# Control plane side
def send_ping(self, drone_id: str) -> dict:
    """Send proof-of-life ping to drone."""
    seq = int(time.time() * 1000) % 0xFFFFFFFF
    drone = self.db.get_node(drone_id)

    start = time.time()
    response = self._ssh_command(drone['ip'],
        f"cat /proc/loadavg && df -h /var/cache | tail -1 && pgrep -c emerge")
    latency = (time.time() - start) * 1000

    return {
        'seq': seq,
        'latency_ms': round(latency, 2),
        'status': 'ok' if response else 'timeout',
        'load': parse_load(response),
        'disk': parse_disk(response),
        'emerge_running': parse_emerge(response),
    }
```

---

## Phase 2: Heartbeat Visualization

### 2.1 Real-time Packet Animation

**Goal**: Show heartbeats moving through the network topology like a packet analyzer.

```
┌─────────────────────────────────────────────────────────────┐
│                   Network Topology View                      │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│    ┌─────────┐                              ┌─────────┐    │
│    │Controller│◄────── • ─────────────────►│ drone-io │    │
│    │ 10.0.0.26│      (heartbeat)           │  16c ✓  │    │
│    └────┬────┘                              └─────────┘    │
│         │                                                   │
│         │    ┌──────── Tailscale Tunnel ────────┐          │
│         │    │                                   │          │
│         ▼    ▼                                   │          │
│    ┌─────────┐  ┌─────────┐  ┌─────────┐  ┌─────────┐     │
│    │  dr-tb  │  │dr-titan │  │  dr-mm2 │  │ sweeper │     │
│    │  8c ✓   │  │  14c ✓  │  │  24c ✓  │  │   ⚡    │     │
│    └─────────┘  └─────────┘  └─────────┘  └─────────┘     │
│         ▲            ▲            ▲            ▲           │
│         │            │            │            │           │
│         └── • ───────┴─── • ──────┴──── • ─────┘           │
│            (work request)  (complete)  (building)          │
│                                                             │
│  Legend: ● heartbeat  ◆ work assignment  ▲ completion     │
│          ✓ online     ⚠ grounded        ✗ offline         │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

### 2.2 Protocol Logger Visualization

Already have `protocol_logger.py` - need frontend to visualize:

```javascript
// Frontend: Packet stream visualization
const PacketStream = () => {
  const [packets, setPackets] = useState([]);

  useEffect(() => {
    const ws = new WebSocket('wss://admin.argobox.com/ws/protocol');
    ws.onmessage = (event) => {
      const packet = JSON.parse(event.data);
      setPackets(prev => [...prev.slice(-100), packet]);
      // Animate packet on topology
      animatePacket(packet.source_node, packet.msg_type);
    };
    return () => ws.close();
  }, []);

  return (
    <div className="packet-stream">
      {packets.map(p => (
        <PacketRow key={p.id} packet={p} />
      ))}
    </div>
  );
};
```

### 2.3 Activity Density Waveform

Use existing `get_activity_density()` for scrubber waveform:

```
┌─────────────────────────────────────────────────────────────┐
│  Activity Timeline (last 24h)                               │
│  ▁▂▃▅▇█▇▅▃▂▁▁▁▂▃▅▇█▇▅▃▂▁▁▁▂▃▅▇█▇▅▃▂▁▁▁▂▃▅▇█▇▅▃▂▁         │
│  12:00    16:00    20:00    00:00    04:00    08:00         │
│                              ▲                              │
│                         (scrubber)                          │
└─────────────────────────────────────────────────────────────┘
```

---

## Phase 3: Admin Terminal & Log Viewer

### 3.1 Web SSH Terminal

**Goal**: Browser-based SSH terminal to any drone.

**Architecture**:
```
Browser → WebSocket → Admin Server → SSH → Drone
```

**Implementation** (`swarm/webssh.py`):

```python
import asyncio
import paramiko
from websockets import serve

class WebSSHBridge:
    """WebSocket-to-SSH bridge for browser terminal access."""

    def __init__(self, db):
        self.db = db
        self.sessions = {}  # ws_id -> paramiko.Channel

    async def handle_connection(self, websocket, path):
        """Handle incoming WebSocket connection."""
        # Parse drone ID from path: /ws/ssh/drone-io
        drone_name = path.split('/')[-1]
        drone = self.db.get_drone_by_name(drone_name)

        if not drone:
            await websocket.close(1008, "Drone not found")
            return

        # Create SSH connection
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        ssh.connect(drone['ip'], username='root', timeout=10)
        channel = ssh.invoke_shell(term='xterm-256color')

        # Bidirectional relay
        async def ssh_to_ws():
            while True:
                if channel.recv_ready():
                    data = channel.recv(4096)
                    await websocket.send(data.decode('utf-8', errors='replace'))
                await asyncio.sleep(0.01)

        async def ws_to_ssh():
            async for message in websocket:
                channel.send(message)

        await asyncio.gather(ssh_to_ws(), ws_to_ssh())
```

**Frontend**: Use xterm.js for terminal rendering.

### 3.2 Centralized Log Viewer

**Log Sources**:
1. **Control Plane logs**: `/var/log/build-swarm-v3/control-plane.log`
2. **Drone logs**: `/var/log/swarm-drone.log` (on each drone)
3. **Build logs**: `/var/log/portage/*.log` (on each drone)

**Admin API Endpoints**:

```python
@route('/admin/api/logs/control-plane')
def get_control_plane_logs(lines=100, since=None):
    """Stream control plane logs."""
    return tail_log('/var/log/build-swarm-v3/control-plane.log', lines)

@route('/admin/api/logs/drone/<name>')
def get_drone_logs(name, lines=100):
    """Fetch drone logs via SSH."""
    drone = db.get_drone_by_name(name)
    return ssh_command(drone['ip'], f'tail -n {lines} /var/log/swarm-drone.log')

@route('/admin/api/logs/build/<name>/<package>')
def get_build_log(name, package):
    """Fetch build log for specific package on drone."""
    drone = db.get_drone_by_name(name)
    log_path = f'/var/log/portage/{package.replace("/", "_")}.log'
    return ssh_command(drone['ip'], f'cat {log_path}')
```

---

## Phase 4: Payload Versioning

### 4.1 Instruction Version Tracking

**Goal**: Track which version of instructions each drone is running.

```sql
-- New table: drone_payloads
CREATE TABLE drone_payloads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    drone_id TEXT NOT NULL,
    payload_type TEXT NOT NULL,  -- 'drone_binary', 'config', 'portage_config'
    version TEXT NOT NULL,
    hash TEXT NOT NULL,          -- SHA256 of payload
    deployed_at REAL NOT NULL,
    deployed_by TEXT,
    status TEXT DEFAULT 'active',
    FOREIGN KEY (drone_id) REFERENCES nodes(id)
);

-- New table: payload_versions
CREATE TABLE payload_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    payload_type TEXT NOT NULL,
    version TEXT NOT NULL UNIQUE,
    hash TEXT NOT NULL,
    content_path TEXT,           -- Path to payload file
    created_at REAL NOT NULL,
    notes TEXT
);
```

### 4.2 Version Deployment Flow

```
┌─────────────────────────────────────────────────────────────┐
│                Payload Deployment Flow                       │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  1. Create new payload version                              │
│     POST /admin/api/payloads                                │
│     { type: "drone_binary", version: "v3.2.1", file: ... }  │
│                                                             │
│  2. Stage deployment                                        │
│     POST /admin/api/payloads/v3.2.1/stage                   │
│     { drones: ["drone-io", "dr-tb"] }                       │
│                                                             │
│  3. Rolling deploy                                          │
│     POST /admin/api/payloads/v3.2.1/deploy                  │
│     - Deploy to one drone at a time                         │
│     - Wait for health check                                 │
│     - Proceed or rollback                                   │
│                                                             │
│  4. Monitor versions                                        │
│     GET /admin/api/drones/versions                          │
│     { "drone-io": "v3.2.1", "dr-tb": "v3.2.0", ... }       │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

---

## Phase 5: Public vs Admin Separation

### 5.1 Public Endpoints (Port 8100)

**READ-ONLY** - No control actions allowed:

| Endpoint | Description |
|----------|-------------|
| `GET /api/v1/status` | Build session status |
| `GET /api/v1/nodes` | Drone list (names, cores, status) |
| `GET /api/v1/events` | Activity feed (last 200) |
| `GET /api/v1/history` | Build history |
| `GET /api/v1/sessions` | Session list |
| `GET /api/v1/health` | Health check |

### 5.2 Admin Endpoints (Port 8093)

**FULL CONTROL** - Requires X-Admin-Key header:

| Endpoint | Description |
|----------|-------------|
| `POST /admin/api/drones/{id}/restart` | Restart drone service |
| `POST /admin/api/drones/{id}/reboot` | Reboot container |
| `POST /admin/api/drones/{id}/ping` | Send proof of life |
| `GET /admin/api/drones/{id}/logs` | View drone logs |
| `POST /admin/api/drones/{id}/ssh` | Execute SSH command |
| `GET /admin/api/logs/control-plane` | Control plane logs |
| `POST /admin/api/queue` | Queue packages for build |
| `POST /admin/api/control` | Start/stop/reset session |
| `POST /admin/api/releases` | Create release |
| `POST /admin/api/releases/{v}/promote` | Promote release |
| `POST /admin/api/payloads` | Deploy payload |
| `GET /ws/ssh/{drone}` | WebSocket SSH terminal |
| `GET /ws/protocol` | WebSocket protocol stream |

---

## Phase 6: Binhost Flexibility (Distro Builder)

### 6.1 Multi-Target Architecture

**Goal**: Build binaries for multiple target machines, not just one binhost.

```
┌─────────────────────────────────────────────────────────────┐
│                   Binhost Architecture                       │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  Staging Area (Controller)                                  │
│  └── /var/cache/binpkgs-v3-staging/                        │
│      ├── Packages                                           │
│      └── category/                                          │
│          └── package-version.gpkg.tar                       │
│                                                             │
│  ┌─────────────────┐  ┌─────────────────┐                  │
│  │ Primary Binhost │  │Secondary Binhost│                  │
│  │ 10.0.0.204     │  │ 100.114.16.118  │                  │
│  │ (LAN, fast)    │  │ (Tailscale)     │                  │
│  │                 │  │                 │                  │
│  │ Used by:       │  │ Used by:        │                  │
│  │ - Jove LAN     │  │ - Kronos LAN    │                  │
│  │ - Local VMs    │  │ - Remote VMs    │                  │
│  └─────────────────┘  └─────────────────┘                  │
│                                                             │
│  ┌─────────────────────────────────────────┐               │
│  │        Distro Release Archive           │               │
│  │ /var/cache/binpkgs-releases/            │               │
│  │ ├── v2026.02.12/                        │               │
│  │ ├── v2026.02.15/                        │               │
│  │ └── v2026.03.01/                        │               │
│  │                                          │               │
│  │ Each release: complete package set for  │               │
│  │ fresh Argo OS install                   │               │
│  └─────────────────────────────────────────┘               │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

### 6.2 Binhost Configuration

```python
# Enhanced config.py
class BinhostConfig:
    """Multi-binhost configuration."""

    def __init__(self):
        self.targets = [
            {
                'name': 'primary',
                'ip': '10.0.0.204',
                'path': '/var/cache/binpkgs',
                'priority': 1,
                'network': 'jove',
                'upload_method': 'rsync',
            },
            {
                'name': 'secondary',
                'ip': '100.114.16.118',  # Tailscale IP
                'path': '/var/cache/binpkgs',
                'priority': 2,
                'network': 'tailscale',
                'upload_method': 'rsync',
            },
        ]

    def get_target_for_drone(self, drone_network: str) -> dict:
        """Get optimal binhost for a drone based on network."""
        for target in sorted(self.targets, key=lambda t: t['priority']):
            if target['network'] == drone_network:
                return target
        return self.targets[0]  # Default to primary
```

---

## Phase 7: Zero-Drift Deployment

### 7.1 Version Numbering Scheme

```
Argo OS v2026.02.12
         │    │  │
         │    │  └── Day
         │    └───── Month
         └────────── Year

Format: vYYYY.MM.DD[-patch]
Example: v2026.02.12, v2026.02.12-1
```

### 7.2 Release Manifest

```json
{
  "version": "v2026.02.12",
  "name": "Argo OS February 2026",
  "created_at": "2026-02-12T15:30:00Z",
  "created_by": "argo",
  "package_count": 4728,
  "size_mb": 12450,
  "kernel": "6.12.58-gentoo",
  "portage_snapshot": "2026-02-12",
  "profile": "default/linux/amd64/23.0/desktop/plasma",
  "checksums": {
    "manifest": "sha256:abc123...",
    "packages": "sha256:def456..."
  },
  "notes": "Monthly release with KDE Plasma 6.3 and kernel 6.12"
}
```

### 7.3 Upgrade Path

```
┌─────────────────────────────────────────────────────────────┐
│                   Zero-Drift Upgrade Flow                    │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  1. Check current version                                   │
│     $ emerge --info | grep SYNC                             │
│     SYNC="rsync://binhost/v2026.01.15"                     │
│                                                             │
│  2. Query available releases                                │
│     $ curl https://binhost.argobox.com/releases.json        │
│     [ "v2026.02.12", "v2026.01.15", "v2026.01.01" ]        │
│                                                             │
│  3. Verify target release                                   │
│     $ swarm-upgrade verify v2026.02.12                      │
│     Checking 4728 packages... 234 upgrades, 12 new          │
│                                                             │
│  4. Apply upgrade                                           │
│     $ swarm-upgrade apply v2026.02.12                       │
│     Creating snapshot before upgrade...                     │
│     Switching binhost to v2026.02.12...                     │
│     Running emerge @world...                                │
│     Upgrade complete! Reboot to apply kernel changes.       │
│                                                             │
│  5. Rollback if needed                                      │
│     $ snapper rollback <pre-upgrade-snapshot>               │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

---

## Phase 8: VM Provisioning

### 8.1 Fresh Install Flow

```
┌─────────────────────────────────────────────────────────────┐
│                   VM Provisioning Flow                       │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  1. Boot from Argo OS minimal ISO                           │
│     (Contains: base system + provision script)              │
│                                                             │
│  2. Run provisioning                                        │
│     $ curl -sL https://get.argobox.com | bash               │
│     OR                                                      │
│     $ provision-argoos --release v2026.02.12                │
│                                                             │
│  3. Provisioning steps:                                     │
│     a. Partition disk (Btrfs with subvolumes)              │
│     b. Configure binhost                                    │
│     c. Install base packages (~1000 core)                   │
│     d. Install selected profile (desktop/server/minimal)    │
│     e. Configure bootloader                                 │
│     f. Set up user accounts                                 │
│     g. First boot configuration                             │
│                                                             │
│  4. Total time: ~15 minutes (from binary packages)          │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

### 8.2 Provisioning API

```python
# New endpoint: /api/v1/provision/bootstrap
@route('/api/v1/provision/bootstrap')
def provision_bootstrap():
    """Return bootstrap script for VM provisioning."""
    return {
        'script': get_bootstrap_script(),
        'release': get_latest_release(),
        'binhost_url': get_binhost_url(),
        'profile': 'default/linux/amd64/23.0/desktop/plasma',
    }

# New endpoint: /api/v1/provision/status
@route('/api/v1/provision/status')
def provision_status(machine_id):
    """Track provisioning progress."""
    return {
        'machine_id': machine_id,
        'stage': 'installing_packages',
        'progress': 45,
        'packages_installed': 450,
        'packages_total': 1000,
        'eta_minutes': 8,
    }
```

---

## Phase 9: Test Build Capability

### 9.1 User-Facing Test Builds

**Goal**: Allow users to request test builds of specific packages.

```
┌─────────────────────────────────────────────────────────────┐
│                   Test Build Interface                       │
├─────────────────────────────────────────────────────────────┤
│                                                             │
│  Package: [______________________________] [Test Build]     │
│                                                             │
│  Test Build Queue:                                          │
│  ┌───────────────────────────────────────────────────────┐ │
│  │ #1 sys-apps/portage-2.3.99  requested by: argo        │ │
│  │    Status: BUILDING on drone-io                       │ │
│  │    Started: 2 min ago                                 │ │
│  │                                                       │ │
│  │ #2 dev-libs/openssl-3.2.0   requested by: user123     │ │
│  │    Status: QUEUED (position 2)                        │ │
│  │    ETA: ~15 minutes                                   │ │
│  └───────────────────────────────────────────────────────┘ │
│                                                             │
│  Note: Test builds are separate from production builds.     │
│  They do NOT get promoted to the release binhost.           │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

### 9.2 Test Build API

```python
# New endpoint: POST /api/v1/test-build
@route('/api/v1/test-build')
def request_test_build():
    """Request a test build (rate-limited, separate queue)."""
    package = request.json.get('package')
    email = request.json.get('email')  # Optional notification

    # Validate package exists in portage tree
    if not validate_package(package):
        return {'error': 'Package not found'}, 404

    # Add to test queue (separate from production)
    job_id = db.queue_test_build(package, email)

    return {
        'job_id': job_id,
        'package': package,
        'status': 'queued',
        'position': get_queue_position(job_id),
    }

# New endpoint: GET /api/v1/test-build/{job_id}
@route('/api/v1/test-build/<job_id>')
def get_test_build_status(job_id):
    """Get test build status."""
    job = db.get_test_build(job_id)
    return {
        'job_id': job_id,
        'package': job['package'],
        'status': job['status'],
        'drone': job.get('drone'),
        'started_at': job.get('started_at'),
        'completed_at': job.get('completed_at'),
        'log_url': f'/api/v1/test-build/{job_id}/log' if job['status'] == 'completed' else None,
    }
```

---

## Implementation Priority

### Sprint 1 (Week 1-2): Self-Healing & Probes
1. Self-healing monitor with escalation ladder
2. Proof of life ping/pong system
3. Safe reboot handling (LXC/QEMU detection)
4. Drone type registry

### Sprint 2 (Week 3-4): Admin Terminal & Logs
1. WebSocket SSH bridge
2. xterm.js frontend integration
3. Log viewer (control plane + drone logs)
4. Build log retrieval

### Sprint 3 (Week 5-6): Visualization
1. Network topology component
2. Packet animation system
3. Activity density waveform
4. Protocol stream viewer

### Sprint 4 (Week 7-8): Payload Versioning
1. Payload version tracking schema
2. Rolling deployment system
3. Version monitoring dashboard
4. Rollback capability

### Sprint 5 (Week 9-10): Binhost & Releases
1. Multi-binhost configuration
2. Network-aware upload routing
3. Release manifest enhancement
4. Zero-drift upgrade tooling

### Sprint 6 (Week 11-12): Provisioning & Test Builds
1. VM provisioning script
2. Provisioning status tracking
3. Test build queue
4. Rate limiting & notifications

---

## Database Schema Additions

```sql
-- Phase 1: Self-healing
ALTER TABLE nodes ADD COLUMN drone_type TEXT DEFAULT 'unknown';  -- lxc, qemu, bare-metal
ALTER TABLE nodes ADD COLUMN last_ping_at REAL;
ALTER TABLE nodes ADD COLUMN last_pong_at REAL;
ALTER TABLE nodes ADD COLUMN ping_latency_ms REAL;

-- Phase 4: Payload versioning
CREATE TABLE payload_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    payload_type TEXT NOT NULL,
    version TEXT NOT NULL UNIQUE,
    hash TEXT NOT NULL,
    content_path TEXT,
    created_at REAL NOT NULL,
    notes TEXT
);

CREATE TABLE drone_payloads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    drone_id TEXT NOT NULL,
    payload_type TEXT NOT NULL,
    version TEXT NOT NULL,
    hash TEXT NOT NULL,
    deployed_at REAL NOT NULL,
    deployed_by TEXT,
    status TEXT DEFAULT 'active',
    FOREIGN KEY (drone_id) REFERENCES nodes(id)
);

-- Phase 9: Test builds
CREATE TABLE test_builds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    package TEXT NOT NULL,
    requester TEXT,
    email TEXT,
    status TEXT DEFAULT 'queued',
    drone_id TEXT,
    queued_at REAL NOT NULL,
    started_at REAL,
    completed_at REAL,
    result TEXT,
    log_path TEXT,
    FOREIGN KEY (drone_id) REFERENCES nodes(id)
);
```

---

## Success Metrics

| Metric | Current | Target |
|--------|---------|--------|
| Drone availability | ~80% | 99.9% |
| Mean time to recovery | Manual | < 2 min |
| Admin response time | N/A | < 5 min |
| Visibility into packets | None | Real-time |
| SSH access to drones | CLI only | Web + CLI |
| Log access | SSH required | 1-click |
| Release cadence | Ad-hoc | Weekly |
| VM provision time | ~60 min | 15 min |

---

## Files to Create/Modify

### New Files
- `swarm/self_healing.py` - Autonomous recovery system
- `swarm/webssh.py` - WebSocket SSH bridge
- `swarm/payloads.py` - Payload versioning
- `swarm/test_builds.py` - Test build queue
- `admin/terminal.html` - xterm.js terminal page
- `admin/topology.js` - Network topology visualization
- `scripts/swarm-upgrade` - Zero-drift upgrade CLI
- `scripts/provision-argoos` - VM provisioning script

### Modified Files
- `swarm/config.py` - Multi-binhost configuration
- `swarm/admin_server.py` - New admin endpoints
- `swarm/health.py` - Ping/pong integration
- `swarm/db.py` - New tables
- `swarm/schema.sql` - Schema updates
- `admin/index.html` - Enhanced dashboard

---

## Conclusion

Build Swarm v4 transforms the system from a functional build orchestrator into a **world-class infrastructure platform** that will:

1. **Inspire the Gentoo community** with its elegant self-healing architecture
2. **Attract homelab enthusiasts** with its comprehensive admin console
3. **Enable Argo OS distribution** through zero-drift versioned releases
4. **Simplify onboarding** with 15-minute VM provisioning

The result: A build swarm that runs itself, heals itself, and gives admins complete visibility and control.

---

*"The awe of the Gentoo community and the homelabbing community"* - Achieved.
